#!/usr/bin/env python
"""只讀探針：檢查股票即時 Tick/BidAsk 的試撮欄位與漲停價判斷。"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

import shioaji as sj
from shioaji.constant import QuoteType, QuoteVersion


CODES = ("2330", "2317")
REDACTIONS: set[str] = set()


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


@contextlib.contextmanager
def quiet() -> Any:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        yield


def fields_of(obj: Any | None, cls: type[Any]) -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if obj is not None and isinstance(getattr(obj, "__dict__", None), dict):
        raw.update(obj.__dict__)
    if obj is not None and callable(getattr(obj, "dict", None)):
        try:
            raw.update(obj.dict())
        except Exception:
            pass
    names: list[str] = []
    for base in reversed(cls.__mro__):
        names.extend(getattr(base, "__annotations__", {}).keys())
    names.extend(name for name in raw if name not in names and not name.startswith("_"))
    missing = "<未收到即時值>"
    return {
        name: getattr(obj, name, raw.get(name, missing)) if obj is not None else missing
        for name in names
    }


def decimal_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except (InvalidOperation, TypeError, ValueError):
        return False


def print_object(code: str, label: str, obj: Any | None, cls: type[Any]) -> None:
    values = fields_of(obj, cls)
    print(f"\n=== {code} {label} 完整欄位 ===")
    if obj is None:
        print("10 秒內未收到實例；以下仍列完整類別欄位，值以未收到標示。")
    for name, value in values.items():
        print(f"{name} = {value!r}")
    simtrade = values.get("simtrade", "<欄位不存在>")
    print(f"[欄位確認] simtrade={'Y' if 'simtrade' in values else 'N'}，目前值={simtrade!r}")
    if label == "TickSTKv1":
        print(
            f"[價格欄位] close={'Y' if 'close' in values else 'N'}，"
            f"目前成交/試撮價={values.get('close')!r}"
        )
        print(
            f"[非布林漲跌註記] chg_type={values.get('chg_type')!r}；"
            "1=漲停，但判斷試撮仍須同時確認 simtrade=True"
        )
    else:
        for name in ("bid_price", "ask_price", "bid_volume", "ask_volume"):
            levels = values.get(name)
            length = len(levels) if isinstance(levels, (list, tuple)) else 0
            print(f"[五檔] {name} 長度={length}，內容={levels!r}")
    direct_flags = [
        name
        for name in values
        if "limit" in name.lower()
        and getattr(cls, "__annotations__", {}).get(name) is bool
    ]
    print(
        "[直接漲停布林欄位] "
        + (f"Y：{direct_flags}" if direct_flags else "N；需以 price == contract.limit_up 自行計算")
    )


def main() -> int:
    env = load_env(Path(__file__).with_name(".env"))
    api_key = env.get("SHIOAJI_API_KEY", "").strip()
    secret_key = env.get("SHIOAJI_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        print("login FAILED: missing credentials")
        return 1
    REDACTIONS.update((api_key, secret_key))

    api = sj.Shioaji()
    subscriptions: list[tuple[Any, QuoteType]] = []
    ticks: dict[str, Any] = {}
    bidasks: dict[str, Any] = {}
    lock = threading.Lock()

    def on_tick(_: Any, tick: Any) -> None:
        with lock:
            ticks.setdefault(str(getattr(tick, "code", "")), tick)

    def on_bidask(_: Any, bidask: Any) -> None:
        with lock:
            bidasks.setdefault(str(getattr(bidask, "code", "")), bidask)

    try:
        try:
            with quiet():
                accounts = api.login(
                    api_key=api_key,
                    secret_key=secret_key,
                    fetch_contract=True,
                    contracts_timeout=30_000,
                    subscribe_trade=False,
                )
            for account in accounts or []:
                for name in ("account_id", "person_id", "username"):
                    value = getattr(account, name, None)
                    if value and len(str(value)) >= 5:
                        REDACTIONS.add(str(value))
            del accounts
            print("login OK")
        except Exception as exc:
            print(f"login FAILED: {type(exc).__name__}")
            return 1

        contracts = {code: api.Contracts.Stocks[code] for code in CODES}
        api.quote.set_on_tick_stk_v1_callback(on_tick)
        api.quote.set_on_bidask_stk_v1_callback(on_bidask)
        for contract in contracts.values():
            for quote_type in (QuoteType.Tick, QuoteType.BidAsk):
                with quiet():
                    api.quote.subscribe(
                        contract,
                        quote_type=quote_type,
                        intraday_odd=False,
                        version=QuoteVersion.v1,
                    )
                subscriptions.append((contract, quote_type))

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with lock:
                if all(code in ticks and code in bidasks for code in CODES):
                    break
            time.sleep(0.1)

        missing_ticks = [contracts[code] for code in CODES if code not in ticks]
        snapshots: dict[str, Any] = {}
        if missing_ticks:
            with quiet():
                snapshot_rows = api.snapshots(missing_ticks, timeout=10_000)
            snapshots = {str(row.code): row for row in snapshot_rows}

        for code in CODES:
            print_object(code, "TickSTKv1", ticks.get(code), sj.TickSTKv1)
            if code not in ticks:
                snapshot = snapshots.get(code)
                print(f"\n--- {code} Snapshot fallback 完整欄位 ---")
                if snapshot is None:
                    print("Snapshot 亦未取得。")
                else:
                    for name, value in fields_of(snapshot, type(snapshot)).items():
                        print(f"{name} = {value!r}")
                print(
                    "Snapshot 僅補 close/change_type/buy_price/sell_price 等快照；"
                    "沒有 simtrade 與五檔，不能代替 TickSTKv1 試撮證據。"
                )
            print_object(code, "BidAskSTKv1", bidasks.get(code), sj.BidAskSTKv1)

        print("\n=== 欄位對照與試撮漲停判斷示範 ===")
        print("即時串流 TickSTKv1 與歷史 api.ticks 是不同物件；試撮看 simtrade=True。")
        for code, contract in contracts.items():
            tick = ticks.get(code)
            bidask = bidasks.get(code)
            tick_simtrade = getattr(tick, "simtrade", None)
            bidask_simtrade = getattr(bidask, "simtrade", None)
            close_source = "TickSTKv1.close" if tick is not None else "Snapshot.close"
            close = getattr(tick, "close", None)
            if close is None:
                close = getattr(snapshots.get(code), "close", None)
            bids = getattr(bidask, "bid_price", []) if bidask is not None else []
            bid_volumes = (
                getattr(bidask, "bid_volume", []) if bidask is not None else []
            )
            best_bid = bids[0] if bids else None
            best_bid_volume = bid_volumes[0] if bid_volumes else None
            limit_up = getattr(contract, "limit_up", None)
            limit_down = getattr(contract, "limit_down", None)
            print(
                f"{code}: limit_up={limit_up!r}, limit_down={limit_down!r}; "
                f"tick.simtrade={tick_simtrade!r}, {close_source}={close!r}, "
                f"close==limit_up="
                f"{'Y' if decimal_equal(close, limit_up) else 'N'}; "
                f"bidask.simtrade={bidask_simtrade!r}, bid_price[0]={best_bid!r}, "
                f"bid_volume[0]={best_bid_volume!r}, bid_price[0]==limit_up="
                f"{'Y' if decimal_equal(best_bid, limit_up) else 'N'}"
            )
        print(
            "主判準：TickSTKv1.simtrade=True 且 close==limit_up ⇒ 試撮價為漲停。"
        )
        print(
            "五檔輔證：BidAskSTKv1.simtrade=True 且 bid_price[0]==limit_up "
            "⇒ 買一鎖漲停；用 bid_volume[0] 看堆量，但它不等同 Tick 試撮價。"
        )
        return 0
    finally:
        for contract, quote_type in reversed(subscriptions):
            try:
                with quiet():
                    api.quote.unsubscribe(
                        contract,
                        quote_type=quote_type,
                        intraday_odd=False,
                        version=QuoteVersion.v1,
                    )
            except Exception:
                pass
        try:
            with quiet():
                api.logout()
        except Exception:
            pass


def run_guarded() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        try:
            status = main()
        except Exception as exc:
            print(f"probe FAILED: {type(exc).__name__}")
            status = 1
    output = captured.getvalue()
    for sensitive in sorted(REDACTIONS, key=len, reverse=True):
        output = output.replace(sensitive, "[REDACTED]")
    sys.stdout.write(output)
    return status


if __name__ == "__main__":
    raise SystemExit(run_guarded())
