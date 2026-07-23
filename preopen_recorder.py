#!/usr/bin/env python
"""股票試撮即時錄製器：只訂閱行情，不啟 CA、不選帳號、不下單。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


# 必須在匯入 Shioaji 前設定，避免第三方套件輸出非必要資訊。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "log"
DATA_DIR = BASE_DIR / "data"

# 客戶端安全上限（以 Tick/BidAsk 各一條串流計）；高於 268 * 2，
# 可讓伺服器自行回報真實配額邊界，同時防止異常宇宙無限擴張。
MAX_SUBS = 600
PREP_SECONDS = 45
CAPACITY_EVENT_SETTLE_SECONDS = 1.5
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

REDACTIONS: list[str] = []
PENDING_EVENTS: list[dict[str, Any]] = []
PENDING_REPORT_PATH: Path | None = None
PENDING_DATA_DATE: str | None = None


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def load_dotenv_manually(path: Path) -> dict[str, str]:
    """安全解析簡單 KEY=VALUE；不顯示、不注入 process environment。"""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


@contextlib.contextmanager
def quiet_library_call() -> Iterable[None]:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        yield


def safe_error(exc: BaseException) -> str:
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
    """識別值只供最終守衛比對，絕不輸出或落地。"""
    field_names = ("person_id", "account_id", "username", "user_id", "userid")
    account_items = (
        [accounts]
        if any(hasattr(accounts, name) for name in field_names)
        else as_list(accounts)
    )
    for account in account_items:
        for field_name in field_names:
            value = normalize_code(getattr(account, field_name, ""))
            if value and value not in REDACTIONS:
                REDACTIONS.append(value)


def contains_sensitive_output(text: str) -> bool:
    if any(secret and secret in text for secret in REDACTIONS):
        return True
    labeled_identity = bool(
        re.search(
            r"(?i)\b("
            r"person[_ -]?id|account(?:[_ -]?id)?|"
            r"user(?:name|[_ -]?id)|userid|"
            r"api[_ -]?key|secret[_ -]?key"
            r")\s*[:=]",
            text,
        )
    )
    native_connection_identity = bool(
        re.search(
            r"(?i)\b(client name|peer host|local address|vpn name)\b",
            text,
        )
    )
    return labeled_identity or native_connection_identity


def wall_clock(value: Any) -> datetime | None:
    """Shioaji ts 是台北 wall-clock 奈秒；依既定規則不可再加八小時。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return datetime.fromtimestamp(
        int(value) / 1e9, tz=timezone.utc
    ).replace(tzinfo=None)


def event_datetime(value: Any) -> datetime | None:
    try:
        return wall_clock(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "N/A"
    return value.strftime("%Y-%m-%d %H:%M:%S.%f").rstrip("0").rstrip(".")


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_number(value: Any) -> str | None:
    number = to_decimal(value)
    if number is None:
        return None
    return format(number, "f")


def bool_simtrade(value: Any) -> bool:
    return value is True or value == 1


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
) -> tuple[datetime, datetime, str]:
    now = datetime.now().replace(tzinfo=None)
    if (start_arg is None) != (end_arg is None):
        raise ValueError("--start 與 --end 必須一起提供")

    if start_arg is None:
        if now.time() < datetime_time(13, 25):
            start_time = datetime_time(13, 25)
            end_time = datetime_time(13, 30)
            source = "預設收盤試撮"
        else:
            start_time = datetime_time(8, 30)
            end_time = datetime_time(9, 0)
            source = "預設次日盤前試撮"
    else:
        start_time = start_arg
        end_time = end_arg
        source = "命令列指定"

    start_at = datetime.combine(now.date(), start_time)
    end_at = datetime.combine(now.date(), end_time)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    if end_at <= now:
        start_at += timedelta(days=1)
        end_at += timedelta(days=1)
    return start_at, end_at, source


def wait_until(target: datetime, label: str) -> None:
    while True:
        remaining = (target - datetime.now().replace(tzinfo=None)).total_seconds()
        if remaining <= 0:
            return
        if remaining > 10:
            print(f"{label}：尚餘 {int(remaining)} 秒", flush=True)
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
        if contract is None:
            continue
        contracts[normalize_code(getattr(contract, "code", ""))] = contract

    priority_rank = {code: index for index, code in enumerate(PRIORITY_CODES)}
    return sorted(
        contracts.items(),
        key=lambda item: (priority_rank.get(item[0], len(priority_rank)), item[0]),
    )


def is_subscription_limit_event(
    resp_code: Any,
    event_code: Any,
    info: Any,
    event: Any,
) -> bool:
    """只在記憶體檢查事件文字；原始內容不輸出，避免夾帶識別值。"""
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


def event_record_base(
    event_type: str,
    code: str,
    event_time: datetime | None,
    limit_up: Any,
) -> dict[str, Any]:
    return {
        "event_type": event_type,
        "code": code,
        "time": format_datetime(event_time),
        "simtrade": True,
        "close": None,
        "chg_type": None,
        "bid_price": None,
        "bid_volume": None,
        "ask_price": None,
        "ask_volume": None,
        "limit_up": format_number(limit_up),
    }


def render_auction_report(
    date_text: str,
    universe_count: int,
    subscribed_stock_count: int,
    capacity_streams: int,
    capacity_basis: str,
    dropped_codes: list[str],
    states: dict[str, dict[str, Any]],
    chg_type_codes: set[str],
    callback_counts: dict[str, int],
    callback_code_counts: dict[str, int],
    callback_errors: list[str],
) -> None:
    print("\n================ 試撮鎖漲停清單 ================")
    print(f"日期={date_text}")
    print(f"股期對應現貨宇宙={universe_count} 檔")
    print(f"成功訂閱檔數={subscribed_stock_count}")
    print(
        f"解析出的真實訂閱上限={capacity_streams} 條串流；"
        f"完整 Tick+BidAsk={subscribed_stock_count} 檔；佐證={capacity_basis}"
    )
    print(
        "callback OK="
        + (
            "Y"
            if callback_counts["tick"] > 0
            and callback_counts["bidask"] > 0
            and not callback_errors
            else "N"
        )
        + f"（Tick={callback_counts['tick']}、BidAsk={callback_counts['bidask']}）"
    )
    print(
        "實收 callback 代碼數："
        f"Tick={callback_code_counts['tick']} 檔、"
        f"BidAsk={callback_code_counts['bidask']} 檔、"
        f"雙通道={callback_code_counts['both']} 檔"
    )
    if callback_errors:
        print("callback 錯誤=" + ",".join(callback_errors))
    if dropped_codes:
        print(f"截斷/未訂閱 {len(dropped_codes)} 檔=" + ",".join(dropped_codes))
    else:
        print("截斷/未訂閱 0 檔=無")

    print(
        "代碼 | 最早鎖漲停時間 | 試撮價 | limit_up | "
        "買一堆量 bid_volume[0] | 是否曾 chg_type==1"
    )
    ordered = sorted(
        states.values(),
        key=lambda item: (
            item["earliest"] is None,
            item["earliest"] or datetime.max,
            item["code"],
        ),
    )
    if not ordered:
        print("（本時窗未偵測到試撮鎖漲停標的）")
    for item in ordered:
        print(
            f"{item['code']} | {format_datetime(item['earliest'])} | "
            f"{format_number(item['auction_price']) or 'N/A'} | "
            f"{format_number(item['limit_up']) or 'N/A'} | "
            f"{format_number(item['bid_volume']) or 'N/A'} | "
            f"{'Y' if item['code'] in chg_type_codes else 'N'}"
        )
    print("==================================================")


def write_events(events: list[dict[str, Any]], date_text: str) -> Path:
    """優先 Parquet；缺 pandas/pyarrow 時才寫 UTF-8 JSONL。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import pandas as pd
        import pyarrow  # noqa: F401

        path = DATA_DIR / f"auction_{date_text}.parquet"
        columns = (
            "event_type",
            "code",
            "time",
            "simtrade",
            "close",
            "chg_type",
            "bid_price",
            "bid_volume",
            "ask_price",
            "ask_volume",
            "limit_up",
        )
        frame = pd.DataFrame(events, columns=columns)
        frame.to_parquet(path, index=False)
        return path
    except (ImportError, ModuleNotFoundError):
        path = DATA_DIR / f"auction_{date_text}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return path


def run_recorder(args: argparse.Namespace) -> int:
    global PENDING_DATA_DATE, PENDING_EVENTS, PENDING_REPORT_PATH

    # 延後匯入，讓匯入期 stdout/stderr 也被最外層守衛捕捉。
    import pysolace

    # Shioaji 1.3.2 的底層 Solace 預設會把 NOTICE 直接寫至原生 stderr；
    # 配額拒絕 NOTICE 可能包含連線識別資訊。強制只保留 ERROR 以上，且
    # 最終報告另由白名單守衛處理。
    if not getattr(pysolace.SolClient, "_auction_quiet_init", False):
        original_solclient_init = pysolace.SolClient.__init__

        def quiet_solclient_init(
            instance: Any,
            log_level: Any = None,
            debug: bool = False,
        ) -> None:
            del log_level, debug
            original_solclient_init(
                instance,
                pysolace.SolLogLevel.SOLCLIENT_LOG_ERROR,
                False,
            )

        pysolace.SolClient.__init__ = quiet_solclient_init
        pysolace.SolClient._auction_quiet_init = True

    import shioaji as sj
    from shioaji.constant import QuoteType, QuoteVersion

    env_path = BASE_DIR / ".env"
    try:
        env = load_dotenv_manually(env_path)
        credential_a = env.get("SHIOAJI_API_KEY", "").strip()
        credential_b = env.get("SHIOAJI_SECRET_KEY", "").strip()
        if not credential_a or not credential_b:
            raise RuntimeError("missing credentials")
        REDACTIONS.extend((credential_a, credential_b))
    except Exception as exc:
        print(f"login FAILED（{safe_error(exc)}）")
        return 1

    smoke_seconds = args.smoke
    if smoke_seconds is not None and smoke_seconds <= 0:
        print("--smoke 必須大於 0")
        return 2

    if smoke_seconds is None:
        try:
            start_at, end_at, schedule_source = make_schedule(args.start, args.end)
        except ValueError as exc:
            print(str(exc))
            return 2
        date_text = start_at.strftime("%Y%m%d")
        PENDING_REPORT_PATH = LOG_DIR / f"recorder-report-{date_text}.txt"
        PENDING_DATA_DATE = date_text
        prep_at = start_at - timedelta(seconds=PREP_SECONDS)
        print(
            f"排程={schedule_source}；start={format_datetime(start_at)}；"
            f"end={format_datetime(end_at)}"
        )
        wait_until(prep_at, "等待登入/訂閱準備時點")
    else:
        start_at = datetime.now().replace(tzinfo=None)
        end_at = start_at + timedelta(seconds=smoke_seconds)
        date_text = start_at.strftime("%Y%m%d")
        PENDING_REPORT_PATH = LOG_DIR / "recorder-smoke-out.txt"
        PENDING_DATA_DATE = None
        print(f"smoke={smoke_seconds} 秒；立即驗證訂閱管線")

    api: Any | None = None
    active_subscriptions: list[tuple[Any, Any]] = []
    unsubscribe_counts = {"tick": 0, "bidask": 0}
    logout_ok = False

    lock = threading.Lock()
    recording = threading.Event()
    subscription_limit = threading.Event()
    callback_counts = {"tick": 0, "bidask": 0}
    callback_codes = {"tick": set(), "bidask": set()}
    simtrade_counts = {"true": 0, "false": 0}
    callback_errors: set[str] = set()
    events: list[dict[str, Any]] = []
    locked_states: dict[str, dict[str, Any]] = {}
    chg_type_codes: set[str] = set()
    subscribed_code_set: set[str] = set()
    limits: dict[str, Any] = {}
    current_attempt = {"index": 0, "code": "", "type": ""}
    failure_at_stream: list[int] = []
    capacity_basis = "未開始"

    def update_locked(
        code: str,
        event_time: datetime | None,
        auction_price: Any,
        limit_up: Any,
        bid_volume: Any = None,
    ) -> None:
        state = locked_states.setdefault(
            code,
            {
                "code": code,
                "earliest": event_time,
                "auction_price": auction_price,
                "limit_up": limit_up,
                "bid_volume": bid_volume,
            },
        )
        if event_time is not None and (
            state["earliest"] is None or event_time < state["earliest"]
        ):
            state["earliest"] = event_time
        if auction_price is not None:
            state["auction_price"] = auction_price
        if limit_up is not None:
            state["limit_up"] = limit_up
        if bid_volume is not None:
            state["bid_volume"] = bid_volume

    def should_record(event_time: datetime | None) -> bool:
        if not recording.is_set():
            return False
        if smoke_seconds is not None:
            return True
        return event_time is not None and start_at <= event_time <= end_at

    def on_event(resp_code: Any, event_code: Any, info: Any, event: Any) -> None:
        if is_subscription_limit_event(resp_code, event_code, info, event):
            with lock:
                if not failure_at_stream:
                    failure_at_stream.append(max(1, current_attempt["index"]))
            subscription_limit.set()

    def on_tick(exchange: Any, tick: Any) -> None:
        del exchange
        try:
            code = normalize_code(getattr(tick, "code", ""))
            if code not in subscribed_code_set:
                return
            simtrade = bool_simtrade(getattr(tick, "simtrade", False))
            raw_time = getattr(tick, "datetime", None)
            if raw_time is None:
                raw_time = getattr(tick, "ts", None)
            tick_time = event_datetime(raw_time)
            with lock:
                callback_counts["tick"] += 1
                callback_codes["tick"].add(code)
                simtrade_counts["true" if simtrade else "false"] += 1
            if not simtrade or not should_record(tick_time):
                return

            close = getattr(tick, "close", None)
            chg_type = getattr(tick, "chg_type", None)
            limit_up = limits.get(code)
            chg_type_one = to_decimal(chg_type) == Decimal("1")
            close_at_limit = (
                to_decimal(close) is not None
                and to_decimal(limit_up) is not None
                and to_decimal(close) == to_decimal(limit_up)
            )
            record = event_record_base("tick", code, tick_time, limit_up)
            record["close"] = format_number(close)
            record["chg_type"] = format_number(chg_type)
            with lock:
                events.append(record)
                if chg_type_one:
                    chg_type_codes.add(code)
                if chg_type_one or close_at_limit:
                    update_locked(code, tick_time, close, limit_up)
        except Exception as exc:
            with lock:
                callback_errors.add(f"Tick:{safe_error(exc)}")

    def on_bidask(exchange: Any, bidask: Any) -> None:
        del exchange
        try:
            code = normalize_code(getattr(bidask, "code", ""))
            if code not in subscribed_code_set:
                return
            simtrade = bool_simtrade(getattr(bidask, "simtrade", False))
            raw_time = getattr(bidask, "datetime", None)
            if raw_time is None:
                raw_time = getattr(bidask, "ts", None)
            book_time = event_datetime(raw_time)
            with lock:
                callback_counts["bidask"] += 1
                callback_codes["bidask"].add(code)
                simtrade_counts["true" if simtrade else "false"] += 1
            if not simtrade or not should_record(book_time):
                return

            bid_prices = as_list(getattr(bidask, "bid_price", None))
            bid_volumes = as_list(getattr(bidask, "bid_volume", None))
            ask_prices = as_list(getattr(bidask, "ask_price", None))
            ask_volumes = as_list(getattr(bidask, "ask_volume", None))
            limit_up = limits.get(code)
            best_bid = bid_prices[0] if bid_prices else None
            best_bid_volume = bid_volumes[0] if bid_volumes else None
            bid_at_limit = (
                to_decimal(best_bid) is not None
                and to_decimal(limit_up) is not None
                and to_decimal(best_bid) == to_decimal(limit_up)
            )
            record = event_record_base("bidask", code, book_time, limit_up)
            record["bid_price"] = [
                format_number(value) for value in bid_prices
            ]
            record["bid_volume"] = [
                format_number(value) for value in bid_volumes
            ]
            record["ask_price"] = [
                format_number(value) for value in ask_prices
            ]
            record["ask_volume"] = [
                format_number(value) for value in ask_volumes
            ]
            with lock:
                events.append(record)
                if bid_at_limit:
                    update_locked(
                        code,
                        book_time,
                        best_bid,
                        limit_up,
                        best_bid_volume,
                    )
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
            universe = stock_future_universe(api)
        except Exception as exc:
            print(f"股期現貨宇宙 FAILED（{safe_error(exc)}）")
            return 1
        print(f"股期對應現貨宇宙={len(universe)} 檔")
        if not universe:
            print("宇宙為空，無法訂閱")
            return 2

        api.quote.set_event_callback(on_event)
        api.quote.set_on_tick_stk_v1_callback(on_tick)
        api.quote.set_on_bidask_stk_v1_callback(on_bidask)

        if smoke_seconds is not None:
            recording.set()

        attempted_streams = 0
        fully_issued_codes: list[str] = []
        hard_candidate_count = min(len(universe), MAX_SUBS // 2)
        candidates = universe[:hard_candidate_count]
        static_dropped = [code for code, _ in universe[hard_candidate_count:]]
        stop_subscriptions = False

        for code, contract in candidates:
            pair_issued = True
            limits[code] = getattr(contract, "limit_up", None)
            for quote_type, type_name in (
                (QuoteType.Tick, "tick"),
                (QuoteType.BidAsk, "bidask"),
            ):
                if subscription_limit.is_set() or attempted_streams >= MAX_SUBS:
                    pair_issued = False
                    stop_subscriptions = True
                    break
                attempted_streams += 1
                with lock:
                    current_attempt.update(
                        index=attempted_streams,
                        code=code,
                        type=type_name,
                    )
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
                        if not failure_at_stream:
                            failure_at_stream.append(attempted_streams)
                    capacity_basis = (
                        f"第 {attempted_streams} 條訂閱呼叫發生 "
                        f"{safe_error(exc)}"
                    )
                    subscription_limit.set()
                    pair_issued = False
                    stop_subscriptions = True
                    break
                # 留給非同步訂閱回應一個短時間片，避免越過容量邊界太多。
                time.sleep(0.01)
                if subscription_limit.is_set():
                    pair_issued = False
                    stop_subscriptions = True
                    break
            if pair_issued:
                fully_issued_codes.append(code)
            if stop_subscriptions:
                break

        # 非同步配額事件可能稍晚抵達。
        settle_deadline = time.monotonic() + CAPACITY_EVENT_SETTLE_SECONDS
        while time.monotonic() < settle_deadline:
            if subscription_limit.is_set():
                break
            time.sleep(0.05)

        with lock:
            first_failed_stream = (
                min(failure_at_stream) if failure_at_stream else None
            )
        if first_failed_stream is not None:
            capacity_streams = max(0, first_failed_stream - 1)
            subscribed_stock_count = min(
                len(fully_issued_codes), capacity_streams // 2
            )
            successful_codes = fully_issued_codes[:subscribed_stock_count]
            if capacity_basis == "未開始":
                capacity_basis = (
                    f"伺服器於第 {first_failed_stream} 條串流回報配額事件"
                )
        else:
            capacity_streams = len(active_subscriptions)
            subscribed_stock_count = len(fully_issued_codes)
            successful_codes = fully_issued_codes
            capacity_basis = (
                f"未撞上限；本次已驗證下限 {capacity_streams} 條串流"
            )

        subscribed_code_set.update(successful_codes)
        dropped_codes = [
            code for code, _ in universe if code not in subscribed_code_set
        ]
        for code in static_dropped:
            if code not in dropped_codes:
                dropped_codes.append(code)

        print(
            f"成功訂閱檔數={subscribed_stock_count}；"
            f"成功完整串流={subscribed_stock_count * 2}"
        )
        print(
            f"解析出的真實訂閱上限={capacity_streams} 條串流；"
            f"換算完整 Tick+BidAsk={capacity_streams // 2} 檔；"
            f"佐證={capacity_basis}"
        )
        if dropped_codes:
            print(
                f"超限截斷/丟棄 {len(dropped_codes)} 檔="
                + ",".join(dropped_codes)
            )
        else:
            print("超限截斷/丟棄 0 檔=無")

        if subscribed_stock_count <= 0:
            print("驗收 FAIL：成功訂閱檔數不是正數")
            return 2

        if smoke_seconds is None:
            wait_until(start_at, "等待 start 才開錄")
            recording.set()
            print(f"錄製開始={format_datetime(start_at)}")
            while True:
                remaining = (
                    end_at - datetime.now().replace(tzinfo=None)
                ).total_seconds()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))
            recording.clear()
            print(f"錄製結束={format_datetime(end_at)}")
        else:
            deadline = time.monotonic() + smoke_seconds
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.25, remaining))
            recording.clear()

        with lock:
            frozen_counts = dict(callback_counts)
            frozen_callback_code_counts = {
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
            frozen_events = list(events)
            frozen_states = {
                code: dict(item) for code, item in locked_states.items()
            }
            frozen_chg_codes = set(chg_type_codes)

        PENDING_EVENTS = frozen_events
        callback_ok = (
            frozen_counts["tick"] > 0
            and frozen_counts["bidask"] > 0
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
            f"Tick={frozen_callback_code_counts['tick']} 檔、"
            f"BidAsk={frozen_callback_code_counts['bidask']} 檔、"
            f"雙通道={frozen_callback_code_counts['both']} 檔"
        )
        print(
            f"simtrade=True 筆數={frozen_simtrade['true']}；"
            f"simtrade=False 筆數={frozen_simtrade['false']}"
        )
        if frozen_simtrade["true"] == 0:
            print("此刻 simtrade 全為 False：盤中 smoke 屬正常")
        if frozen_errors:
            print("callback 錯誤=" + ",".join(frozen_errors))

        if smoke_seconds is None:
            render_auction_report(
                date_text,
                len(universe),
                subscribed_stock_count,
                capacity_streams,
                capacity_basis,
                dropped_codes,
                frozen_states,
                frozen_chg_codes,
                frozen_counts,
                frozen_callback_code_counts,
                frozen_errors,
            )

        acceptance = callback_ok and subscribed_stock_count > 0
        print(
            "管線驗收="
            + ("PASS" if acceptance else "FAIL")
            + "（成功訂閱>0 且 Tick/BidAsk callback 正常）"
        )
        return 0 if acceptance else 2
    finally:
        recording.clear()
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
                    pass
            active_subscriptions.clear()
            print(
                "退訂完成（含被拒通道的防禦性退訂）："
                f"Tick={unsubscribe_counts['tick']}、"
                f"BidAsk={unsubscribe_counts['bidask']}"
            )
            try:
                with quiet_library_call():
                    api.logout()
                logout_ok = True
            except Exception:
                logout_ok = False
            print("logout OK" if logout_ok else "logout FAILED")
            print(
                "退訂+logout OK"
                if logout_ok
                else "退訂完成但 logout FAILED"
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="錄製股票試撮 Tick/BidAsk 並偵測試撮鎖漲停"
    )
    parser.add_argument("--start", type=parse_hhmm, help="開始時間 HH:MM")
    parser.add_argument("--end", type=parse_hhmm, help="結束時間 HH:MM")
    parser.add_argument(
        "--smoke",
        type=float,
        metavar="N",
        help="立即跑 N 秒驗證訂閱與 callback，不等待試撮時窗",
    )
    return parser.parse_args(argv)


def run_with_output_guard() -> int:
    """先捕捉完整輸出；守衛通過才寫 UTF-8 報告、資料與 stdout。"""
    configure_output()
    original_stdout = sys.stdout
    captured = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
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
    serialized_events = "\n".join(
        json.dumps(event, ensure_ascii=False) for event in PENDING_EVENTS
    )
    if contains_sensitive_output(report) or contains_sensitive_output(
        serialized_events
    ):
        original_stdout.write(
            "SECURITY FAIL：輸出含金鑰或帶標籤識別值，報告與資料已抑制。\n"
        )
        original_stdout.flush()
        return 3

    if PENDING_REPORT_PATH is not None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with PENDING_REPORT_PATH.open(
            "w", encoding="utf-8", newline="\n"
        ) as handle:
            handle.write(report)

    if PENDING_DATA_DATE is not None:
        try:
            data_path = write_events(PENDING_EVENTS, PENDING_DATA_DATE)
            data_message = (
                f"試撮事件落地={data_path.name}；筆數={len(PENDING_EVENTS)}\n"
            )
            report += data_message
            if PENDING_REPORT_PATH is not None:
                with PENDING_REPORT_PATH.open(
                    "a", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(data_message)
        except Exception as exc:
            message = f"試撮事件落地 FAILED（{safe_error(exc)}）\n"
            report += message
            exit_code = 2 if exit_code == 0 else exit_code
            if PENDING_REPORT_PATH is not None:
                with PENDING_REPORT_PATH.open(
                    "a", encoding="utf-8", newline="\n"
                ) as handle:
                    handle.write(message)

    original_stdout.write(report)
    original_stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_with_output_guard())
