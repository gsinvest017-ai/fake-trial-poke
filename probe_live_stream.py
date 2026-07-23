#!/usr/bin/env python
"""唯讀訂閱股票即時 Tick v1 與 BidAsk v1，驗證成交價變化及五檔。"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


# 方便直接修改的監控設定。
WATCHLIST = ("2330", "2317", "2454", "2308", "2881")
STREAM_SECONDS = 25

# 必須在匯入 Shioaji 前設定；Shioaji 本身延後到輸出守衛內才匯入。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

REDACTIONS: list[str] = []


def configure_output() -> None:
    """固定 Windows 終端輸出為 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def load_dotenv_manually(path: Path) -> dict[str, str]:
    """僅解析本檔案所需的 KEY=VALUE，不把內容寫到環境或輸出。"""
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


def safe_error(exc: BaseException) -> str:
    """錯誤僅顯示型別，避免第三方例外文字夾帶敏感資訊。"""
    return type(exc).__name__


@contextlib.contextmanager
def quiet_library_call() -> Iterable[None]:
    """抑制第三方套件在呼叫期間直接輸出的內容。"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        yield


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
    """只記錄識別值供最終守衛比對，絕不輸出帳號物件。"""
    field_names = ("person_id", "account_id", "username", "user_id", "userid")
    if any(hasattr(accounts, field_name) for field_name in field_names):
        account_items = [accounts]
    else:
        account_items = as_list(accounts)
    for account in account_items:
        for field_name in field_names:
            value = normalize_code(getattr(account, field_name, ""))
            if value and value not in REDACTIONS:
                REDACTIONS.append(value)


def wall_clock(value: Any) -> datetime | None:
    """保留 Shioaji 的台北 wall-clock；絕不再次加 UTC+8。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    # 相容數值 ts：Shioaji 已把台北 wall-clock 編碼成奈秒。
    return datetime.fromtimestamp(
        int(value) / 1e9, tz=timezone.utc
    ).replace(tzinfo=None)


def format_time(value: datetime | None) -> str:
    if value is None:
        return "N/A"
    return value.strftime("%Y-%m-%d %H:%M:%S.%f").rstrip("0").rstrip(".")


def format_number(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return "N/A"


def wait_for_contracts(api: Any, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if bool(api.Contracts):
            return
        time.sleep(0.1)
    raise TimeoutError("contracts not fetched")


def get_stock_contract(api: Any, code: str) -> Any | None:
    contract = api.Contracts.Stocks.get(code)
    if contract is None:
        return None
    if normalize_code(getattr(contract, "code", "")) != code:
        return None
    return contract


def five_levels(bidask: dict[str, Any] | None) -> tuple[int, int, bool]:
    if bidask is None:
        return 0, 0, False
    bid_levels = min(len(bidask["bid_price"]), len(bidask["bid_volume"]))
    ask_levels = min(len(bidask["ask_price"]), len(bidask["ask_volume"]))
    complete = (
        len(bidask["bid_price"]) == 5
        and len(bidask["bid_volume"]) == 5
        and len(bidask["ask_price"]) == 5
        and len(bidask["ask_volume"]) == 5
    )
    return bid_levels, ask_levels, complete


def compact_book(prices: list[Any], volumes: list[Any]) -> str:
    levels = [
        f"{index}:{format_number(price)}/{format_number(volume)}"
        for index, (price, volume) in enumerate(zip(prices, volumes), start=1)
    ]
    return "，".join(levels) if levels else "無"


def price_change_samples(ticks: list[dict[str, Any]]) -> tuple[list[str], bool]:
    samples: list[str] = []
    previous: str | None = None
    distinct_prices: set[str] = set()
    for tick in ticks:
        price = tick["price"]
        distinct_prices.add(price)
        if price != previous:
            if len(samples) < 10:
                samples.append(price)
            previous = price
    return samples, len(distinct_prices) >= 2


def print_detail(code: str, item: dict[str, Any]) -> dict[str, bool]:
    ticks = item["ticks"]
    bidask = item["bidask"]
    bid_levels, ask_levels, book_complete = five_levels(bidask)
    samples, price_changed = price_change_samples(ticks)

    print(f"\n[{code}]")
    print(f"tick 筆數={len(ticks)}")
    if ticks:
        first = ticks[0]
        last = ticks[-1]
        print(
            "首筆："
            f"時間={format_time(first['time'])}、成交價={first['price']}、"
            f"量={first['volume']}、simtrade={first['simtrade']}"
        )
        print(
            "末筆："
            f"時間={format_time(last['time'])}、成交價={last['price']}、"
            f"量={last['volume']}、simtrade={last['simtrade']}"
        )
    else:
        print("首筆：無")
        print("末筆：無")
    print(
        "價格變化樣本序列（去除連續重複，最多 10 個）="
        + (" → ".join(samples) if samples else "無")
    )

    print(f"五檔={bid_levels}/{ask_levels}")
    if bidask is None:
        print("最新五檔：無")
    else:
        print(
            "買方 5 檔（價/量）="
            + compact_book(bidask["bid_price"], bidask["bid_volume"])
        )
        print(
            "賣方 5 檔（價/量）="
            + compact_book(bidask["ask_price"], bidask["ask_volume"])
        )

    return {
        "tick_received": bool(ticks),
        "book_complete": book_complete,
        "price_changed": price_changed,
    }


def run_probe() -> int:
    # 延後匯入，讓匯入期輸出也納入 stdout/stderr 守衛。
    import shioaji as sj
    from shioaji.constant import QuoteType, QuoteVersion

    env_path = Path(__file__).resolve().with_name(".env")
    try:
        env_values = load_dotenv_manually(env_path)
        api_key = env_values.get("SHIOAJI_API_KEY", "").strip()
        secret_key = env_values.get("SHIOAJI_SECRET_KEY", "").strip()
        if not api_key or not secret_key:
            raise RuntimeError("missing credentials")
        REDACTIONS.extend((api_key, secret_key))
    except Exception as exc:
        print(f"login FAILED（{safe_error(exc)}）")
        return 1

    api: Any | None = None
    active_subscriptions: list[tuple[Any, Any, Any]] = []
    unsubscribe_counts = {QuoteType.Tick: 0, QuoteType.BidAsk: 0}
    results: dict[str, dict[str, Any]] = {
        code: {"ticks": [], "bidask": None} for code in WATCHLIST
    }
    lock = threading.Lock()
    callback_errors: set[str] = set()

    def on_tick(exchange: Any, tick: Any) -> None:
        del exchange
        try:
            code = normalize_code(getattr(tick, "code", ""))
            if code not in results:
                return
            raw_time = getattr(tick, "datetime", None)
            if raw_time is None:
                raw_time = getattr(tick, "ts", None)
            record = {
                "time": wall_clock(raw_time),
                "price": format_number(getattr(tick, "close", None)),
                "volume": format_number(getattr(tick, "volume", None)),
                "simtrade": bool(getattr(tick, "simtrade", False)),
            }
            with lock:
                results[code]["ticks"].append(record)
        except Exception as exc:
            with lock:
                callback_errors.add(f"Tick:{safe_error(exc)}")

    def on_bidask(exchange: Any, bidask: Any) -> None:
        del exchange
        try:
            code = normalize_code(getattr(bidask, "code", ""))
            if code not in results:
                return
            record = {
                "time": wall_clock(getattr(bidask, "datetime", None)),
                "bid_price": as_list(getattr(bidask, "bid_price", None)),
                "bid_volume": as_list(getattr(bidask, "bid_volume", None)),
                "ask_price": as_list(getattr(bidask, "ask_price", None)),
                "ask_volume": as_list(getattr(bidask, "ask_volume", None)),
            }
            with lock:
                results[code]["bidask"] = record
        except Exception as exc:
            with lock:
                callback_errors.add(f"BidAsk:{safe_error(exc)}")

    try:
        try:
            with quiet_library_call():
                api = sj.Shioaji()
                accounts = api.login(
                    api_key=api_key,
                    secret_key=secret_key,
                    fetch_contract=True,
                    contracts_timeout=30_000,
                    subscribe_trade=False,
                )
                remember_sensitive_identifiers(accounts)
                del accounts
            print("login OK")
        except Exception as exc:
            print(f"login FAILED（{safe_error(exc)}）")
            return 1

        try:
            wait_for_contracts(api)
        except Exception as exc:
            print(f"合約載入 FAILED（{safe_error(exc)}）")
            return 1

        api.quote.set_on_tick_stk_v1_callback(on_tick)
        api.quote.set_on_bidask_stk_v1_callback(on_bidask)

        contracts: dict[str, Any] = {}
        for code in WATCHLIST:
            contract = get_stock_contract(api, code)
            if contract is None:
                print(f"{code}：合約找不到")
                continue
            contracts[code] = contract
            for quote_type in (QuoteType.Tick, QuoteType.BidAsk):
                try:
                    with quiet_library_call():
                        api.quote.subscribe(
                            contract,
                            quote_type=quote_type,
                            intraday_odd=False,
                            version=QuoteVersion.v1,
                        )
                    active_subscriptions.append(
                        (contract, quote_type, QuoteVersion.v1)
                    )
                except Exception as exc:
                    print(
                        f"{code}：{quote_type.value} 訂閱 FAILED"
                        f"（{safe_error(exc)}）"
                    )

        print(
            f"即時串流蒐集開始：標的={len(contracts)} 檔、"
            f"視窗={STREAM_SECONDS} 秒"
        )
        deadline = time.monotonic() + STREAM_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))
        print("即時串流蒐集結束")

        with lock:
            frozen_results = {
                code: {
                    "ticks": list(item["ticks"]),
                    "bidask": (
                        dict(item["bidask"]) if item["bidask"] is not None else None
                    ),
                }
                for code, item in results.items()
            }
            frozen_callback_errors = sorted(callback_errors)

        statuses: dict[str, dict[str, bool]] = {}
        for code in WATCHLIST:
            statuses[code] = print_detail(code, frozen_results[code])

        print("\n================ 摘要 ================")
        for code in WATCHLIST:
            status = statuses[code]
            print(
                f"{code}："
                f"即時 tick 收到={'Y' if status['tick_received'] else 'N'}、"
                f"五檔收到={'Y' if status['book_complete'] else 'N'}、"
                f"視窗內價格有變動={'Y' if status['price_changed'] else 'N'}"
            )
        if frozen_callback_errors:
            print("callback 狀態=" + "、".join(frozen_callback_errors))
        else:
            print("callback 狀態=OK")

        acceptance_passed = any(
            status["tick_received"] and status["book_complete"]
            for status in statuses.values()
        )
        print(
            "驗收：至少一檔 tick 筆數>0 且五檔=5/5："
            + ("PASS" if acceptance_passed else "FAIL")
        )
        print("======================================")
        return 0 if acceptance_passed else 2
    finally:
        if api is not None:
            for contract, quote_type, quote_version in reversed(
                active_subscriptions
            ):
                try:
                    with quiet_library_call():
                        api.quote.unsubscribe(
                            contract,
                            quote_type=quote_type,
                            intraday_odd=False,
                            version=quote_version,
                        )
                    unsubscribe_counts[quote_type] += 1
                except Exception:
                    pass
            active_subscriptions.clear()
            print(
                "退訂完成："
                f"Tick={unsubscribe_counts[QuoteType.Tick]}、"
                f"BidAsk={unsubscribe_counts[QuoteType.BidAsk]}"
            )
            try:
                with quiet_library_call():
                    api.logout()
                print("logout OK")
            except Exception:
                print("logout FAILED")


def run_with_output_guard() -> int:
    """先掃描完整 stdout/stderr；有敏感值或敏感標籤就抑制整份報告。"""
    configure_output()
    original_stdout = sys.stdout
    captured_output = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_output), contextlib.redirect_stderr(
            captured_output
        ):
            exit_code = run_probe()
    except Exception as exc:
        with contextlib.redirect_stdout(captured_output):
            print(f"probe FAILED（{safe_error(exc)}）")
        exit_code = 1

    report = captured_output.getvalue()
    contains_known_identifier = any(
        sensitive and sensitive in report for sensitive in REDACTIONS
    )
    contains_labeled_identifier = bool(
        re.search(
            r"(?i)\b("
            r"person[_ -]?id|account(?:[_ -]?id)?|"
            r"user(?:name|[_ -]?id)|userid|"
            r"api[_ -]?key|secret[_ -]?key"
            r")\s*[:=]",
            report,
        )
    )
    if contains_known_identifier or contains_labeled_identifier:
        original_stdout.write("SECURITY FAIL：報告含敏感資訊，已抑制輸出。\n")
        original_stdout.flush()
        return 3

    original_stdout.write(report)
    original_stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_with_output_guard())
