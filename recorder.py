#!/usr/bin/env python
"""即時錄製台股試撮五檔，並於時窗後補一輪全宇宙 snapshot。

本程式只登入行情、讀取合約及訂閱報價；不啟用 CA、不選帳號、不下單。
盤前試撮沒有可回補的歷史來源，因此 raw JSONL 逐筆保留時窗內收到的
simtrade=True/False 訊息。預設只訂 BidAsk 以提高覆蓋率；必要時可用
both 模式保留舊有 Tick+BidAsk 行為。
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


# 必須在匯入 Shioaji 前設定，降低第三方套件輸出敏感連線資訊的風險。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "log"
MAX_STREAM_ATTEMPTS = 600
PREP_SECONDS = 45
CAPACITY_EVENT_SETTLE_SECONDS = 1.5
SUBSCRIBE_EVENT_GRACE_SECONDS = 0.01
SNAPSHOT_AFTER_OPEN_SECONDS = 3
PRIORITY_CODES = (
    "2330",
    "2317",
    "2454",
    "2308",
    "2382",
    "3231",
    "2881",
    "2882",
    "2891",
    "2886",
    "2603",
    "2618",
    "2002",
    "1301",
    "1303",
)
PRIORITY_RANK = {
    code: index for index, code in enumerate(PRIORITY_CODES)
}

REDACTIONS: list[str] = []
PENDING_REPORT_PATH: Path | None = None
PENDING_META_PATH: Path | None = None
PENDING_META: dict[str, Any] | None = None


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def load_dotenv_manually(path: Path) -> dict[str, str]:
    """只解析需要的 KEY=VALUE，不顯示也不注入 process environment。"""
    values: dict[str, str] = {}
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]
            values[key] = value
    return values


@contextlib.contextmanager
def quiet_library_call() -> Iterable[None]:
    """攔住 Python 層第三方 stdout/stderr；原生 Solace 另於登入前降級。"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        yield


def safe_error(exc: BaseException) -> str:
    """例外訊息可能夾帶連線或識別資訊，所以只回報型別。"""
    return type(exc).__name__


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def normalize_code(value: Any) -> str:
    return str(value or "").strip()


def remember_sensitive_identifiers(accounts: Any) -> None:
    """只把識別值留在記憶體供輸出守衛比對，絕不顯示或序列化帳戶。"""
    fields = ("person_id", "account_id", "username", "user_id", "userid")
    items = (
        [accounts]
        if any(hasattr(accounts, field) for field in fields)
        else as_list(accounts)
    )
    for account in items:
        for field in fields:
            value = normalize_code(getattr(account, field, ""))
            if value and value not in REDACTIONS:
                REDACTIONS.append(value)


def contains_sensitive_output(text: str) -> bool:
    if any(secret and secret in text for secret in REDACTIONS):
        return True
    labeled_identity = re.search(
        r"(?i)\b("
        r"person[_ -]?id|account(?:[_ -]?id)?|"
        r"user(?:name|[_ -]?id)|userid|"
        r"api[_ -]?key|secret[_ -]?key"
        r")\s*[:=]",
        text,
    )
    native_connection_identity = re.search(
        r"(?i)\b(client name|peer host|local address|vpn name)\b",
        text,
    )
    return bool(labeled_identity or native_connection_identity)


def wall_clock_from_ns(value: Any) -> datetime | None:
    """依已驗證規則把台北 wall-clock 奈秒轉成 naive datetime。

    Shioaji 的即時 ts 不能再 +8；此公式刻意不呼叫 astimezone。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromtimestamp(
        int(value) / 1e9, tz=timezone.utc
    ).replace(tzinfo=None)


def event_datetime(value: Any) -> datetime | None:
    try:
        return wall_clock_from_ns(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def iso_local(value: datetime | None) -> str:
    if value is None:
        # 契約要求 ts 為字串；無法解析的訊息用空字串保留而不偽造時間。
        return ""
    return value.isoformat(timespec="microseconds")


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def to_float(value: Any) -> float | None:
    number = to_decimal(value)
    return float(number) if number is not None else None


def to_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        number = to_decimal(value)
        return int(number) if number is not None else None


def bool_simtrade(value: Any) -> bool:
    return value is True or value == 1


def five_levels(value: Any, converter: Any) -> list[Any]:
    result = [converter(item) for item in as_list(value)[:5]]
    return result + [None] * (5 - len(result))


def parse_hhmm(value: str) -> datetime_time:
    if not re.fullmatch(r"\d{2}:\d{2}", value):
        raise argparse.ArgumentTypeError("時間必須為 HH:MM")
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("時間必須為有效 HH:MM") from exc


def make_schedule(
    start_arg: datetime_time | None,
    end_arg: datetime_time | None,
) -> tuple[datetime, datetime, str, str]:
    """建立錄製時窗；13:20 前預設收盤試撮，之後預設次日盤前。"""
    now = datetime.now().replace(tzinfo=None)
    if (start_arg is None) != (end_arg is None):
        raise ValueError("--start 與 --end 必須一起提供")

    if start_arg is None:
        if now.time() < datetime_time(13, 20):
            start_time = datetime_time(13, 25)
            end_time = datetime_time(13, 30)
            session = "preclose"
            source = "預設收盤試撮"
            start_at = datetime.combine(now.date(), start_time)
        else:
            start_time = datetime_time(8, 30)
            end_time = datetime_time(9, 0)
            session = "preopen"
            source = "預設次日盤前試撮"
            start_at = datetime.combine(now.date() + timedelta(days=1), start_time)
    else:
        start_time = start_arg
        end_time = end_arg
        session = "preopen" if start_time < datetime_time(12, 0) else "preclose"
        source = "命令列指定"
        start_at = datetime.combine(now.date(), start_time)
        if start_at < now and end_time <= now.time():
            start_at += timedelta(days=1)

    end_at = datetime.combine(start_at.date(), end_time)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    return start_at, end_at, session, source


def wait_until(target: datetime, label: str) -> None:
    last_reported_minute: int | None = None
    while True:
        remaining = (target - datetime.now().replace(tzinfo=None)).total_seconds()
        if remaining <= 0:
            return
        minute = int(remaining // 60)
        if remaining > 60 and minute != last_reported_minute:
            print(f"{label}：尚餘約 {minute + 1} 分鐘", flush=True)
            last_reported_minute = minute
        time.sleep(min(1.0, remaining))


def wait_for_contracts(api: Any, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if bool(api.Contracts):
            return
        time.sleep(0.1)
    raise TimeoutError("contracts not fetched")


def iter_future_contracts(futures_root: Any) -> Iterable[Any]:
    for product_or_contract in futures_root:
        if getattr(product_or_contract, "underlying_code", None) is not None:
            yield product_or_contract
            continue
        try:
            for contract in product_or_contract:
                if getattr(contract, "underlying_code", None) is not None:
                    yield contract
        except TypeError:
            continue


def get_stock_contract(api: Any, code: str) -> Any | None:
    contract = api.Contracts.Stocks.get(code)
    if contract is None and code.endswith("S"):
        contract = api.Contracts.Stocks.get(code[:-1])
    if contract is None:
        return None
    resolved = normalize_code(getattr(contract, "code", ""))
    valid_codes = {code, code[:-1] if code.endswith("S") else code}
    return contract if resolved in valid_codes else None


def universe_sort_key(item: tuple[str, Any]) -> tuple[bool, int, str]:
    code = item[0]
    return (
        code.startswith("00"),
        PRIORITY_RANK.get(code, len(PRIORITY_RANK)),
        code,
    )


def stock_future_universe(api: Any) -> list[tuple[str, Any]]:
    raw_codes = {
        normalize_code(getattr(contract, "underlying_code", ""))
        for contract in iter_future_contracts(api.Contracts.Futures)
        if normalize_code(getattr(contract, "underlying_kind", "")).upper() == "S"
        and normalize_code(getattr(contract, "underlying_code", ""))
    }
    contracts: dict[str, Any] = {}
    for raw_code in sorted(raw_codes):
        contract = get_stock_contract(api, raw_code)
        if contract is not None:
            contracts[normalize_code(getattr(contract, "code", ""))] = contract
    return sorted(
        contracts.items(),
        # 00 開頭是 ETF 型標的；串流不足時先讓位，確保個股優先。
        # 同一類別內保留原有的大型股優先、再依代碼排序。
        key=universe_sort_key,
    )


def parse_universe_spec(value: str) -> list[str]:
    candidate = Path(value)
    if candidate.is_file():
        with candidate.open("r", encoding="utf-8-sig") as handle:
            content = handle.read()
        if candidate.suffix.lower() == ".json":
            parsed = json.loads(content)
            if not isinstance(parsed, list):
                raise ValueError("universe JSON 必須是代碼陣列")
            raw_codes = parsed
        else:
            raw_codes = re.split(r"[\s,;]+", content)
    else:
        raw_codes = re.split(r"[\s,;]+", value)
    result: list[str] = []
    seen: set[str] = set()
    for raw_code in raw_codes:
        code = normalize_code(raw_code)
        if code and code not in seen:
            seen.add(code)
            result.append(code)
    if not result:
        raise ValueError("--universe 未提供有效代碼")
    return result


def requested_universe(
    api: Any, universe_spec: str | None
) -> tuple[list[tuple[str, Any]], list[str], int]:
    if universe_spec is None:
        universe = stock_future_universe(api)
        return universe, [], len(universe)

    requested_codes = parse_universe_spec(universe_spec)
    result: list[tuple[str, Any]] = []
    unresolved: list[str] = []
    seen_resolved: set[str] = set()
    for code in requested_codes:
        contract = get_stock_contract(api, code)
        if contract is None:
            unresolved.append(code)
            continue
        resolved = normalize_code(getattr(contract, "code", ""))
        if resolved and resolved not in seen_resolved:
            seen_resolved.add(resolved)
            result.append((resolved, contract))
    return result, unresolved, len(requested_codes)


def is_subscription_limit_event(
    resp_code: Any,
    event_code: Any,
    info: Any,
    event: Any,
) -> bool:
    """只在記憶體判斷配額事件，原始 payload 不輸出。"""
    payload = " ".join(
        (str(resp_code), str(event_code), str(info), str(event))
    ).lower()
    has_limit = any(
        token in payload
        for token in (
            "limit",
            "quota",
            "exceed",
            "maximum",
            "too many",
            "upper bound",
            "已達",
            "上限",
            "超過",
            "配額",
        )
    )
    has_subscription = any(
        token in payload
        for token in ("subscr", "quote", "topic", "行情", "訂閱")
    )
    return has_limit and has_subscription


class JsonlWriter:
    """讓行情 callback 只入 queue，由單一背景執行緒依序寫 UTF-8 JSONL。"""

    _STOP = object()

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self.count = 0
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run, name="auction-jsonl-writer", daemon=True
        )

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thread.start()

    def put(self, record: dict[str, Any]) -> None:
        self.items.put(record)

    def close(self) -> None:
        self.items.put(self._STOP)
        self.thread.join()

    def _run(self) -> None:
        try:
            with self.path.open("w", encoding="utf-8", newline="\n") as handle:
                while True:
                    item = self.items.get()
                    if item is self._STOP:
                        break
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                    self.count += 1
                handle.flush()
        except BaseException as exc:  # pragma: no cover - 防禦磁碟/權限錯誤
            self.error = exc


def tick_record(tick: Any, code: str) -> dict[str, Any]:
    raw_time = getattr(tick, "ts", None)
    if raw_time is None:
        raw_time = getattr(tick, "datetime", None)
    return {
        "code": code,
        "name": "",
        "ts": iso_local(event_datetime(raw_time)),
        "kind": "tick",
        "simtrade": bool_simtrade(getattr(tick, "simtrade", False)),
        "price": to_float(getattr(tick, "close", None)),
        "chg_type": to_int(getattr(tick, "chg_type", None)),
        "bid_price": [None] * 5,
        "bid_volume": [None] * 5,
        "ask_price": [None] * 5,
        "ask_volume": [None] * 5,
        "volume": to_int(getattr(tick, "volume", None)),
    }


def bidask_record(bidask: Any, code: str) -> dict[str, Any]:
    raw_time = getattr(bidask, "ts", None)
    if raw_time is None:
        raw_time = getattr(bidask, "datetime", None)
    return {
        "code": code,
        "name": "",
        "ts": iso_local(event_datetime(raw_time)),
        "kind": "bidask",
        "simtrade": bool_simtrade(getattr(bidask, "simtrade", False)),
        "price": None,
        "chg_type": None,
        "bid_price": five_levels(getattr(bidask, "bid_price", None), to_float),
        "bid_volume": five_levels(getattr(bidask, "bid_volume", None), to_int),
        "ask_price": five_levels(getattr(bidask, "ask_price", None), to_float),
        "ask_volume": five_levels(getattr(bidask, "ask_volume", None), to_int),
        "volume": None,
    }


def snapshot_levels(
    snapshot: Any,
    levels_field: str,
    best_field: str,
    converter: Any,
) -> list[Any]:
    """相容五檔陣列及 Snapshot 僅提供最佳一檔的 scalar 欄位。"""
    values = as_list(getattr(snapshot, levels_field, None))
    if not values:
        values = [getattr(snapshot, best_field, None)]
    return five_levels(values, converter)


def snapshot_record(
    snapshot: Any,
    fallback_code: str,
    name_by_code: dict[str, str],
    observed_at: datetime,
) -> dict[str, Any]:
    raw_time = getattr(snapshot, "ts", None)
    if raw_time is None:
        raw_time = getattr(snapshot, "datetime", None)
    snapshot_time = event_datetime(raw_time)
    if snapshot_time is None:
        snapshot_time = observed_at

    code = normalize_code(getattr(snapshot, "code", "")) or fallback_code
    open_price = getattr(snapshot, "open_price", None)
    if open_price is None:
        open_price = getattr(snapshot, "open", None)
    return {
        "code": code,
        "name": name_by_code.get(code, ""),
        "ts": iso_local(snapshot_time),
        "kind": "snapshot",
        "open_price": to_float(open_price),
        "bid_price": snapshot_levels(
            snapshot, "bid_price", "buy_price", to_float
        ),
        "bid_volume": snapshot_levels(
            snapshot, "bid_volume", "buy_volume", to_int
        ),
        "ask_price": snapshot_levels(
            snapshot, "ask_price", "sell_price", to_float
        ),
        "ask_volume": snapshot_levels(
            snapshot, "ask_volume", "sell_volume", to_int
        ),
    }


def contract_meta(code: str, contract: Any) -> dict[str, Any]:
    return {
        "code": code,
        "name": normalize_code(getattr(contract, "name", "")),
        "reference": to_float(getattr(contract, "reference", None)),
        "limit_up": to_float(getattr(contract, "limit_up", None)),
    }


def run_recorder(args: argparse.Namespace) -> int:
    global PENDING_META, PENDING_META_PATH, PENDING_REPORT_PATH

    # 延後匯入，使 Shioaji/pysolace 的 Python 輸出也落在最外層守衛中。
    import pysolace

    if not getattr(pysolace.SolClient, "_auction_quiet_init", False):
        original_solclient_init = pysolace.SolClient.__init__

        def quiet_solclient_init(
            instance: Any,
            log_level: Any = None,
            debug: bool = False,
        ) -> None:
            del log_level, debug
            original_solclient_init(
                instance, pysolace.SolLogLevel.SOLCLIENT_LOG_ERROR, False
            )

        pysolace.SolClient.__init__ = quiet_solclient_init
        pysolace.SolClient._auction_quiet_init = True

    import shioaji as sj
    from shioaji.constant import QuoteType, QuoteVersion

    if args.smoke is not None and args.smoke <= 0:
        print("--smoke 必須大於 0")
        return 2

    try:
        start_at, end_at, session, schedule_source = make_schedule(
            args.start, args.end
        )
    except ValueError as exc:
        print(str(exc))
        return 2

    if args.smoke is not None:
        start_at = datetime.now().replace(tzinfo=None)
        end_at = start_at + timedelta(seconds=float(args.smoke))
        PENDING_REPORT_PATH = LOG_DIR / "recorder-smoke2-out.txt"
        print(f"smoke={float(args.smoke):g} 秒；立即驗證訂閱管線")
    else:
        PENDING_REPORT_PATH = (
            LOG_DIR / f"recorder-{start_at:%Y%m%d}-{session}.txt"
        )
        print(
            f"排程={schedule_source}；session={session}；"
            f"start={start_at.isoformat(timespec='seconds')}；"
            f"end={end_at.isoformat(timespec='seconds')}"
        )
        wait_until(start_at - timedelta(seconds=PREP_SECONDS), "等待訂閱準備時點")

    if args.out:
        out_path = Path(args.out).expanduser().resolve()
    elif args.smoke is None:
        out_path = DATA_DIR / f"auction_{start_at:%Y%m%d}.jsonl"
    else:
        out_path = None
    if out_path is not None:
        PENDING_META_PATH = out_path.with_suffix(".meta.json")

    try:
        env = load_dotenv_manually(BASE_DIR / ".env")
        credential_a = env.get("SHIOAJI_API_KEY", "").strip()
        credential_b = env.get("SHIOAJI_SECRET_KEY", "").strip()
        if not credential_a or not credential_b:
            raise RuntimeError("missing credentials")
        REDACTIONS.extend((credential_a, credential_b))
    except Exception as exc:
        print(f"login FAILED（{safe_error(exc)}）")
        return 1

    api: Any | None = None
    active_subscriptions: list[tuple[Any, Any]] = []
    writer: JsonlWriter | None = None
    recording = threading.Event()
    subscription_limit = threading.Event()
    lock = threading.Lock()
    callback_counts = {"tick": 0, "bidask": 0}
    callback_codes = {"tick": set(), "bidask": set()}
    callback_errors: set[str] = set()
    simtrade_counts = {"true": 0, "false": 0}
    candidate_codes: set[str] = set()
    successful_codes: list[str] = []
    name_by_code: dict[str, str] = {}
    first_failed_stream: list[int] = []
    current_attempt = {"index": 0}
    capacity_basis = "未開始"
    unsubscribe_counts = {"tick": 0, "bidask": 0}
    logout_ok = False
    universe: list[tuple[str, Any]] = []
    unresolved: list[str] = []
    requested_count = 0
    dropped_codes: list[str] = []
    attempted_streams = 0
    capacity_streams = 0
    capacity_exact = False
    exit_code = 1
    unsubscribe_failures = 0
    snapshot_count = 0
    snapshot_requested = 0
    snapshot_error = ""
    channels_per_stock = 1 if args.channels == "bidask" else 2

    def should_record(event_time: datetime | None) -> bool:
        if not recording.is_set() or event_time is None:
            return False
        if args.smoke is not None:
            return True
        return start_at <= event_time <= end_at

    def on_event(resp_code: Any, event_code: Any, info: Any, event: Any) -> None:
        if is_subscription_limit_event(resp_code, event_code, info, event):
            with lock:
                if not first_failed_stream:
                    first_failed_stream.append(max(1, current_attempt["index"]))
            subscription_limit.set()

    def on_tick(exchange: Any, tick: Any) -> None:
        del exchange
        try:
            code = normalize_code(getattr(tick, "code", ""))
            if code not in candidate_codes:
                return
            record = tick_record(tick, code)
            record["name"] = name_by_code.get(code, "")
            event_time = event_datetime(
                getattr(tick, "ts", None)
                if getattr(tick, "ts", None) is not None
                else getattr(tick, "datetime", None)
            )
            with lock:
                callback_counts["tick"] += 1
                callback_codes["tick"].add(code)
                simtrade_counts[
                    "true" if record["simtrade"] else "false"
                ] += 1
            if should_record(event_time) and writer is not None:
                writer.put(record)
        except Exception as exc:
            with lock:
                callback_errors.add(f"Tick:{safe_error(exc)}")

    def on_bidask(exchange: Any, bidask: Any) -> None:
        del exchange
        try:
            code = normalize_code(getattr(bidask, "code", ""))
            if code not in candidate_codes:
                return
            record = bidask_record(bidask, code)
            record["name"] = name_by_code.get(code, "")
            event_time = event_datetime(
                getattr(bidask, "ts", None)
                if getattr(bidask, "ts", None) is not None
                else getattr(bidask, "datetime", None)
            )
            with lock:
                callback_counts["bidask"] += 1
                callback_codes["bidask"].add(code)
                simtrade_counts[
                    "true" if record["simtrade"] else "false"
                ] += 1
            if should_record(event_time) and writer is not None:
                writer.put(record)
        except Exception as exc:
            with lock:
                callback_errors.add(f"BidAsk:{safe_error(exc)}")

    try:
        try:
            with quiet_library_call():
                api = sj.Shioaji()
                accounts = api.login(
                    api_key=credential_a,
                    secret_key=credential_b,
                    fetch_contract=True,
                    contracts_timeout=30_000,
                    subscribe_trade=False,
                )
                remember_sensitive_identifiers(accounts)
                del accounts
            print("login OK（行情唯讀；CA=未啟用；未選帳號；未下單）")
        except Exception as exc:
            print(f"login FAILED（{safe_error(exc)}）")
            return 1

        try:
            wait_for_contracts(api)
            universe, unresolved, requested_count = requested_universe(
                api, args.universe
            )
        except Exception as exc:
            print(f"標的宇宙 FAILED（{safe_error(exc)}）")
            return 1

        print(
            f"標的宇宙={len(universe)} 檔"
            + ("（預設股期對應現貨）" if args.universe is None else "（自訂）")
        )
        print(
            f"channels={args.channels}；"
            f"每檔串流數={channels_per_stock}"
        )
        if unresolved:
            print(
                f"找不到合約 {len(unresolved)} 檔=" + ",".join(unresolved)
            )
        if not universe:
            print("宇宙為空，無法訂閱")
            return 2

        for code, contract in universe:
            name_by_code[code] = normalize_code(getattr(contract, "name", ""))

        api.quote.set_event_callback(on_event)
        api.quote.set_on_tick_stk_v1_callback(on_tick)
        api.quote.set_on_bidask_stk_v1_callback(on_bidask)

        fully_issued_codes: list[str] = []
        hard_candidate_count = min(
            len(universe), MAX_STREAM_ATTEMPTS // channels_per_stock
        )
        candidates = universe[:hard_candidate_count]
        stop_subscriptions = False
        quote_types = (
            (QuoteType.BidAsk,)
            if args.channels == "bidask"
            else (QuoteType.Tick, QuoteType.BidAsk)
        )

        for code, contract in candidates:
            candidate_codes.add(code)
            pair_issued = True
            for quote_type in quote_types:
                if (
                    subscription_limit.is_set()
                    or attempted_streams >= MAX_STREAM_ATTEMPTS
                ):
                    pair_issued = False
                    stop_subscriptions = True
                    break
                attempted_streams += 1
                with lock:
                    current_attempt["index"] = attempted_streams
                try:
                    with quiet_library_call():
                        api.quote.subscribe(
                            contract,
                            quote_type=quote_type,
                            intraday_odd=False,
                            version=QuoteVersion.v1,
                        )
                    active_subscriptions.append((contract, quote_type))
                except Exception as exc:
                    with lock:
                        if not first_failed_stream:
                            first_failed_stream.append(attempted_streams)
                    capacity_basis = (
                        f"第 {attempted_streams} 條訂閱呼叫發生 "
                        f"{safe_error(exc)}"
                    )
                    subscription_limit.set()
                    pair_issued = False
                    stop_subscriptions = True
                    break
                time.sleep(SUBSCRIBE_EVENT_GRACE_SECONDS)
                if subscription_limit.is_set():
                    pair_issued = False
                    stop_subscriptions = True
                    break
            if pair_issued:
                fully_issued_codes.append(code)
            if stop_subscriptions:
                break

        settle_deadline = time.monotonic() + CAPACITY_EVENT_SETTLE_SECONDS
        while time.monotonic() < settle_deadline:
            if subscription_limit.is_set():
                break
            time.sleep(0.05)

        with lock:
            failed_at = (
                min(first_failed_stream) if first_failed_stream else None
            )
        if failed_at is not None:
            capacity_streams = max(0, failed_at - 1)
            subscribed_count = min(
                len(fully_issued_codes),
                capacity_streams // channels_per_stock,
            )
            successful_codes = fully_issued_codes[:subscribed_count]
            capacity_exact = True
            if capacity_basis == "未開始":
                capacity_basis = (
                    f"伺服器於第 {failed_at} 條串流回報配額事件"
                )
        else:
            capacity_streams = len(active_subscriptions)
            subscribed_count = len(fully_issued_codes)
            successful_codes = fully_issued_codes
            capacity_exact = False
            capacity_basis = (
                f"未撞上限；本次只驗證至少 {capacity_streams} 條串流"
            )

        successful_set = set(successful_codes)
        capacity_dropped_codes = [
            code for code, _ in universe if code not in successful_set
        ]
        dropped_etf_codes = [
            code for code in capacity_dropped_codes if code.startswith("00")
        ]
        dropped_stock_codes = [
            code
            for code in capacity_dropped_codes
            if not code.startswith("00")
        ]
        # meta.dropped 延續舊契約：包含無法解析的自訂代碼。
        dropped_codes = list(capacity_dropped_codes)
        dropped_codes.extend(
            code for code in unresolved if code not in dropped_codes
        )

        print(
            f"成功訂閱檔數={subscribed_count}；"
            f"成功串流={subscribed_count * channels_per_stock}"
        )
        print(
            f"解析出的真實訂閱上限={capacity_streams} 條串流；"
            f"channels={args.channels} 可涵蓋="
            f"{capacity_streams // channels_per_stock} 檔；"
            f"是否撞到邊界={'Y' if capacity_exact else 'N'}；佐證={capacity_basis}"
        )
        if capacity_dropped_codes:
            print(
                f"超限截斷/丟棄 {len(capacity_dropped_codes)} 檔="
                + ",".join(capacity_dropped_codes)
            )
        else:
            print("超限截斷/丟棄 0 檔=無")
        print(
            f"被讓位 ETF 型清單 {len(dropped_etf_codes)} 檔="
            + (",".join(dropped_etf_codes) if dropped_etf_codes else "無")
        )
        print(
            f"未涵蓋非 ETF 個股 {len(dropped_stock_codes)} 檔="
            + (",".join(dropped_stock_codes) if dropped_stock_codes else "無")
        )
        if subscribed_count <= 0:
            print("驗收 FAIL：成功訂閱檔數不是正數")
            return 2

        if out_path is not None:
            writer = JsonlWriter(out_path)
            writer.start()

        if args.smoke is None:
            wait_until(start_at, "等待 start 才開錄")
            recording.set()
            print(f"錄製開始={start_at.isoformat(timespec='seconds')}")
            while True:
                remaining = (
                    end_at - datetime.now().replace(tzinfo=None)
                ).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))
            recording.clear()
            print(f"錄製結束={end_at.isoformat(timespec='seconds')}")
        else:
            # smoke 的計時從完成訂閱後起算，確保 callback 真正觀察滿 N 秒。
            recording.set()
            deadline = time.monotonic() + float(args.smoke)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))
            recording.clear()

        if args.smoke is None and session == "preopen":
            after_open = datetime.combine(
                end_at.date(), datetime_time(9, 0)
            ) + timedelta(seconds=SNAPSHOT_AFTER_OPEN_SECONDS)
            snapshot_at = max(end_at, after_open)
            if datetime.now().replace(tzinfo=None) < snapshot_at:
                wait_until(snapshot_at, "等待 09:00 後 snapshot")

        snapshot_requested = len(universe)
        snapshot_observed_at = datetime.now().replace(tzinfo=None)
        try:
            with quiet_library_call():
                raw_snapshots = api.snapshots(
                    [contract for _, contract in universe],
                    timeout=30_000,
                )
            for index, snapshot in enumerate(as_list(raw_snapshots)):
                fallback_code = (
                    universe[index][0] if index < len(universe) else ""
                )
                record = snapshot_record(
                    snapshot,
                    fallback_code,
                    name_by_code,
                    snapshot_observed_at,
                )
                if not record["code"]:
                    continue
                snapshot_count += 1
                if writer is not None:
                    writer.put(record)
        except Exception as exc:
            snapshot_error = safe_error(exc)
        print(
            f"snapshot 掃描檔數={snapshot_count}/{snapshot_requested}"
            + (
                f"；FAILED（{snapshot_error}）"
                if snapshot_error
                else ""
            )
        )

        if writer is not None:
            writer.close()

        with lock:
            frozen_counts = dict(callback_counts)
            frozen_callback_codes = {
                "tick": len(callback_codes["tick"]),
                "bidask": len(callback_codes["bidask"]),
                "both": len(
                    callback_codes["tick"].intersection(
                        callback_codes["bidask"]
                    )
                ),
            }
            frozen_simtrade = dict(simtrade_counts)
            frozen_errors = sorted(callback_errors)
        if writer is not None and writer.error is not None:
            frozen_errors.append(f"Writer:{safe_error(writer.error)}")

        callback_ok = (
            frozen_counts["bidask"] > 0
            and (
                args.channels == "bidask"
                or frozen_counts["tick"] > 0
            )
            and not frozen_errors
        )
        print(
            "callback OK="
            + ("Y" if callback_ok else "N")
            + f"（Tick={frozen_counts['tick']}、"
            f"BidAsk={frozen_counts['bidask']}）"
        )
        print(
            "實收 callback 代碼數："
            f"Tick={frozen_callback_codes['tick']} 檔、"
            f"BidAsk={frozen_callback_codes['bidask']} 檔、"
            f"雙通道={frozen_callback_codes['both']} 檔"
        )
        print(
            f"simtrade=True 筆數={frozen_simtrade['true']}；"
            f"simtrade=False 筆數={frozen_simtrade['false']}"
        )
        if args.smoke is not None and frozen_simtrade["true"] == 0:
            print("此刻 simtrade 全為 False：非試撮時段 smoke 屬正常")
        if frozen_errors:
            print("callback/寫檔錯誤=" + ",".join(frozen_errors))
        if out_path is not None and writer is not None:
            print(f"raw JSONL={out_path}；筆數={writer.count}")

        PENDING_META = {
            "date": start_at.strftime("%Y%m%d"),
            "session": session,
            "window": {
                "start": start_at.strftime("%H:%M"),
                "end": end_at.strftime("%H:%M"),
            },
            "universe_size": len(universe),
            "requested_universe_size": requested_count,
            "subscribed": subscribed_count,
            "channels": args.channels,
            "channels_per_stock": channels_per_stock,
            # sub_limit 保留 Shioaji 原始「串流通道」單位；另明列股票檔數。
            "sub_limit": capacity_streams,
            "sub_limit_unit": "streams",
            "sub_limit_stocks": capacity_streams // channels_per_stock,
            "sub_limit_exact": capacity_exact,
            "capacity_basis": capacity_basis,
            "snapshot_count": snapshot_count,
            "snapshot_requested": snapshot_requested,
            "snapshot_complete": snapshot_count == snapshot_requested,
            "generated_at": datetime.now()
            .replace(tzinfo=None)
            .isoformat(timespec="seconds"),
            "dropped": dropped_codes,
            "subscribed_codes": successful_codes,
            "stocks": [
                contract_meta(code, contract) for code, contract in universe
            ],
        }

        snapshot_ok = snapshot_count == snapshot_requested
        acceptance = callback_ok and subscribed_count > 0 and snapshot_ok
        print(
            "管線驗收="
            + ("PASS" if acceptance else "FAIL")
            + "（成功訂閱>0、模式所需 callback 正常、snapshot 完整）"
        )
        if args.smoke is not None:
            print("是否遇 0xC0000142=N（本次程序正常啟動）")
        exit_code = 0 if acceptance else 2
    finally:
        recording.clear()
        if writer is not None and writer.thread.is_alive():
            writer.close()
        if api is not None:
            for contract, quote_type in reversed(active_subscriptions):
                try:
                    with quiet_library_call():
                        api.quote.unsubscribe(
                            contract,
                            quote_type=quote_type,
                            intraday_odd=False,
                            version=QuoteVersion.v1,
                        )
                    if quote_type == QuoteType.Tick:
                        unsubscribe_counts["tick"] += 1
                    elif quote_type == QuoteType.BidAsk:
                        unsubscribe_counts["bidask"] += 1
                except Exception:
                    unsubscribe_failures += 1
            active_subscriptions.clear()
            print(
                "退訂完成（含被拒通道的防禦性退訂）："
                f"Tick={unsubscribe_counts['tick']}、"
                f"BidAsk={unsubscribe_counts['bidask']}、"
                f"失敗={unsubscribe_failures}"
            )
            try:
                with quiet_library_call():
                    api.logout()
                logout_ok = True
            except Exception:
                logout_ok = False
            print("logout OK" if logout_ok else "logout FAILED")
            cleanup_ok = logout_ok and unsubscribe_failures == 0
            print(
                "退訂+logout OK"
                if cleanup_ok
                else "退訂或 logout FAILED"
            )
            if exit_code == 0 and not cleanup_ok:
                exit_code = 2
    return exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="即時錄製股票試撮 BidAsk／選配 Tick（行情唯讀）"
    )
    parser.add_argument("--start", type=parse_hhmm, help="開始時間 HH:MM")
    parser.add_argument("--end", type=parse_hhmm, help="結束時間 HH:MM")
    parser.add_argument(
        "--smoke",
        type=float,
        metavar="N",
        help="立即訂閱並觀察 N 秒，不等待正式時窗",
    )
    parser.add_argument(
        "--out",
        help="raw JSONL 路徑；正式錄製預設 data/auction_YYYYMMDD.jsonl",
    )
    parser.add_argument(
        "--universe",
        help="逗號分隔代碼，或 UTF-8 txt/JSON 代碼清單；預設股期對應現貨",
    )
    parser.add_argument(
        "--channels",
        choices=("bidask", "both"),
        default="bidask",
        help="bidask=單通道提高覆蓋率（預設）；both=Tick+BidAsk 舊行為",
    )
    return parser.parse_args(argv)


def write_guarded_outputs(report: str) -> bool:
    """守衛通過後才用 Python UTF-8 open 寫 log 與 sidecar。"""
    meta_text = (
        json.dumps(PENDING_META, ensure_ascii=False, indent=2)
        if PENDING_META is not None
        else ""
    )
    if contains_sensitive_output(report) or contains_sensitive_output(meta_text):
        return False

    if PENDING_REPORT_PATH is not None:
        PENDING_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_REPORT_PATH.open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(report)
    if PENDING_META_PATH is not None and PENDING_META is not None:
        PENDING_META_PATH.parent.mkdir(parents=True, exist_ok=True)
        with PENDING_META_PATH.open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            json.dump(PENDING_META, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    return True


def run_with_output_guard() -> int:
    configure_output()
    original_stdout = sys.stdout
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(
            captured
        ):
            args = parse_args()
            exit_code = run_recorder(args)
    except SystemExit as exc:
        exit_code = int(exc.code or 0)
    except KeyboardInterrupt:
        with contextlib.redirect_stdout(captured):
            print("使用者中止；已進入 finally 清理")
        exit_code = 130
    except Exception as exc:
        with contextlib.redirect_stdout(captured):
            print(f"recorder FAILED（{safe_error(exc)}）")
        exit_code = 1

    report = captured.getvalue()
    if not write_guarded_outputs(report):
        original_stdout.write(
            "SECURITY FAIL：輸出含金鑰或帶標籤識別值，log/meta 已抑制。\n"
        )
        original_stdout.flush()
        return 3
    original_stdout.write(report)
    original_stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_with_output_guard())
