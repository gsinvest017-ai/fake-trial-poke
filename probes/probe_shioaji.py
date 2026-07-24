#!/usr/bin/env python
"""Shioaji 盤前試撮資料能力診斷探針。

只讀取行情與合約資料；不啟用 CA、不選帳號、不下單。
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import sys
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


# 在匯入 Shioaji 前降低套件日誌量，避免登入流程將非必要資訊寫到畫面。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

import shioaji as sj
from shioaji.constant import QuoteType, QuoteVersion


TAIPEI_TZ = timezone(timedelta(hours=8))
TICK_CODES = ("2330", "2317", "2454")
BIDASK_CODES = ("2330", "2317", "2454", "2308", "2881")
REDACTIONS: list[str] = []


def configure_stdout() -> None:
    """讓重新導向與 Windows 終端都穩定輸出 UTF-8。"""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


def load_dotenv_manually(path: Path) -> dict[str, str]:
    """僅解析本探針需要的簡單 KEY=VALUE 格式，不輸出內容。"""
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
    """只回報例外型別；不把可能含識別資訊的 API 訊息帶到 stdout。"""
    return type(exc).__name__


@contextlib.contextmanager
def quiet_library_call() -> Iterable[None]:
    hidden_stdout = io.StringIO()
    hidden_stderr = io.StringIO()
    with contextlib.redirect_stdout(hidden_stdout), contextlib.redirect_stderr(
        hidden_stderr
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


def iter_future_contracts(futures_root: Any) -> Iterable[Any]:
    """走訪 Futures -> 商品群組 -> Future；同時容忍直接回傳 Future。"""
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


def get_stock_contract(api: sj.Shioaji, code: str) -> Any | None:
    contract = api.Contracts.Stocks.get(code)
    # 部分期交所 underlying_code 會在現貨代碼後加 S，例如 00679BS。
    if contract is None and code.endswith("S"):
        contract = api.Contracts.Stocks.get(code[:-1])
    if contract is None:
        return None
    resolved_code = normalize_code(getattr(contract, "code", ""))
    valid_codes = {code, code[:-1] if code.endswith("S") else code}
    if resolved_code not in valid_codes:
        return None
    return contract


def wait_for_contracts(api: sj.Shioaji, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if bool(api.Contracts):
            return
        time.sleep(0.1)
    raise TimeoutError("contracts not fetched")


def remember_sensitive_identifiers(accounts: Any) -> None:
    """只記住識別值供最終輸出掃描，不顯示也不序列化帳戶物件。"""
    for account in as_list(accounts):
        for field_name in ("person_id", "account_id", "username"):
            value = normalize_code(getattr(account, field_name, ""))
            if len(value) >= 6 and value not in REDACTIONS:
                REDACTIONS.append(value)


def epoch_to_taipei(value: Any) -> datetime:
    """自動辨識秒／毫秒／微秒／奈秒 epoch，轉為台北時間。"""
    timestamp = int(value)
    magnitude = abs(timestamp)
    if magnitude >= 100_000_000_000_000_000:
        seconds = timestamp / 1_000_000_000
    elif magnitude >= 100_000_000_000_000:
        seconds = timestamp / 1_000_000
    elif magnitude >= 100_000_000_000:
        seconds = timestamp / 1_000
    else:
        seconds = timestamp
    return datetime.fromtimestamp(seconds, tz=timezone.utc).astimezone(TAIPEI_TZ)


def format_datetime(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f").rstrip("0").rstrip(".")


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def format_number(value: Any) -> str:
    number = to_decimal(value)
    if number is None:
        return "N/A"
    return format(number, "f")


def format_bytes(value: Any) -> str:
    try:
        size = int(value)
    except (TypeError, ValueError):
        return "N/A"
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    scaled = float(size)
    unit = units[0]
    for unit in units:
        if abs(scaled) < 1024 or unit == units[-1]:
            break
        scaled /= 1024
    return f"{size} bytes ({scaled:.2f} {unit})"


def probe_stock_future_underlyings(api: sj.Shioaji) -> list[str]:
    print("\n[1] 個股期貨對應現貨清單")
    underlying_codes = {
        normalize_code(getattr(contract, "underlying_code", ""))
        for contract in iter_future_contracts(api.Contracts.Futures)
        if normalize_code(getattr(contract, "underlying_kind", "")).upper() == "S"
        and normalize_code(getattr(contract, "underlying_code", ""))
    }
    result = sorted(underlying_codes)
    print(f"股期對應現貨檔數：{len(result)}")
    print(f"前 10 個代碼：{', '.join(result[:10]) if result else '無'}")
    return result


def probe_historical_auction_ticks(
    api: sj.Shioaji, query_date: date
) -> tuple[bool, bool, dict[str, dict[str, Any]]]:
    print("\n[2] 今日盤前試撮 tick 事後保留性（核心）")
    details: dict[str, dict[str, Any]] = {}
    preopen_start = datetime_time(8, 30)
    market_open = datetime_time(9, 0)

    for code in TICK_CODES:
        contract = get_stock_contract(api, code)
        if contract is None:
            print(f"{code}：找不到現貨合約")
            details[code] = {"ok": False, "preopen_count": 0}
            continue

        try:
            ticks = api.ticks(contract, date=query_date.isoformat())
            raw_timestamps = as_list(getattr(ticks, "ts", None))
            tick_field_lengths = {
                field_name: len(as_list(getattr(ticks, field_name, None)))
                for field_name in (
                    "ts",
                    "close",
                    "volume",
                    "bid_price",
                    "bid_volume",
                    "ask_price",
                    "ask_volume",
                    "tick_type",
                )
            }
            arrays_aligned = len(set(tick_field_lengths.values())) == 1
            local_timestamps = [epoch_to_taipei(ts) for ts in raw_timestamps]
            before_nine = [
                ts
                for ts in local_timestamps
                if ts.date() == query_date and ts.time() < market_open
            ]
            preopen = [
                ts
                for ts in local_timestamps
                if ts.date() == query_date
                and preopen_start <= ts.time() < market_open
            ]

            simtrade_field = None
            for field_name in ("simtrade", "SimTrade"):
                if hasattr(ticks, field_name):
                    simtrade_field = field_name
                    break
            simtrade_values = (
                as_list(getattr(ticks, simtrade_field))
                if simtrade_field is not None
                else []
            )
            simtrade_count = sum(
                1 for value in simtrade_values if value is True or value == 1
            )

            earliest = min(local_timestamps) if local_timestamps else None
            latest = max(local_timestamps) if local_timestamps else None
            print(
                f"{code}：tick 筆數={len(raw_timestamps)}；"
                f"最早={format_datetime(earliest) if earliest else 'N/A'}；"
                f"最晚={format_datetime(latest) if latest else 'N/A'}；"
                f"欄位長度一致={'Y' if arrays_aligned else 'N'}"
            )
            print(
                f"  timestamp < 09:00:00={'Y' if before_nine else 'N'}"
                f"（筆數={len(before_nine)}；其中 08:30–09:00={len(preopen)}）"
            )
            print(
                f"  simtrade 欄位={'Y' if simtrade_field else 'N'}；"
                "simtrade==1 筆數="
                f"{simtrade_count if simtrade_field else 'N/A（欄位不存在）'}"
            )
            details[code] = {
                "ok": True,
                "arrays_aligned": arrays_aligned,
                "tick_count": len(raw_timestamps),
                "earliest": earliest,
                "latest": latest,
                "before_nine_count": len(before_nine),
                "preopen_count": len(preopen),
                "simtrade_field": simtrade_field is not None,
                "simtrade_count": simtrade_count,
            }
        except Exception as exc:
            print(f"{code}：查詢失敗（{safe_error(exc)}）")
            details[code] = {"ok": False, "preopen_count": 0}

    # 08:30–09:00 是本次要驗證的試撮時段；有該區間歷史 tick 才判定 Y。
    history_has_preopen = any(
        item.get("ok")
        and item.get("arrays_aligned")
        and item.get("preopen_count", 0) > 0
        for item in details.values()
    )
    conclusive = history_has_preopen or all(
        details.get(code, {}).get("ok")
        and details.get(code, {}).get("arrays_aligned")
        and details.get(code, {}).get("tick_count", 0) > 0
        for code in TICK_CODES
    )
    print(
        "明確結論：今日盤前(08:30–09:00)試撮 tick "
        f"事後可從歷史 API 撈回：{'Y' if history_has_preopen else 'N'}"
    )
    print(
        f"證據狀態：{'CONCLUSIVE' if conclusive else 'INCONCLUSIVE'}；"
        "歷史 Ticks 是否能以 simtrade 明確辨識試撮："
        f"{'Y' if any(item.get('simtrade_field') for item in details.values()) else 'N'}"
    )
    return history_has_preopen, conclusive, details


def probe_realtime_bidask(
    api: sj.Shioaji, active_subscriptions: list[Any], timeout_seconds: float = 8.0
) -> tuple[bool, dict[str, dict[str, Any]]]:
    print("\n[3] 即時 BidAsk 五檔")
    contracts: dict[str, Any] = {}
    for code in BIDASK_CODES:
        contract = get_stock_contract(api, code)
        if contract is not None:
            contracts[code] = contract
    received: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    completed = threading.Event()
    target_codes = set(contracts)

    def on_bidask(exchange: Any, bidask: Any) -> None:
        del exchange
        code = normalize_code(getattr(bidask, "code", ""))
        if code not in target_codes:
            return
        bid_prices = as_list(getattr(bidask, "bid_price", None))
        bid_volumes = as_list(getattr(bidask, "bid_volume", None))
        ask_prices = as_list(getattr(bidask, "ask_price", None))
        ask_volumes = as_list(getattr(bidask, "ask_volume", None))
        with lock:
            received[code] = {
                "bid_levels": len(bid_prices),
                "bid_volume_levels": len(bid_volumes),
                "ask_levels": len(ask_prices),
                "ask_volume_levels": len(ask_volumes),
                "valid_bid_levels": sum(
                    1
                    for price in bid_prices
                    if (number := to_decimal(price)) is not None and number > 0
                ),
                "valid_ask_levels": sum(
                    1
                    for price in ask_prices
                    if (number := to_decimal(price)) is not None and number > 0
                ),
                "intraday_odd": bool(getattr(bidask, "intraday_odd", False)),
            }
            if target_codes.issubset(received):
                completed.set()

    api.quote.set_on_bidask_stk_v1_callback(on_bidask)
    subscribed_here: list[Any] = []
    try:
        for contract in contracts.values():
            api.quote.subscribe(
                contract,
                quote_type=QuoteType.BidAsk,
                intraday_odd=False,
                version=QuoteVersion.v1,
            )
            subscribed_here.append(contract)
            active_subscriptions.append(contract)

        completed.wait(timeout_seconds)
        with lock:
            result = dict(received)
    finally:
        for contract in reversed(subscribed_here):
            try:
                api.quote.unsubscribe(
                    contract,
                    quote_type=QuoteType.BidAsk,
                    intraday_odd=False,
                    version=QuoteVersion.v1,
                )
            except Exception:
                pass
            try:
                active_subscriptions.remove(contract)
            except ValueError:
                pass

    for code in BIDASK_CODES:
        levels = result.get(code)
        if levels is None:
            if code not in contracts:
                print(f"{code}：找不到現貨合約")
            else:
                print(
                    f"{code}：TIMEOUT/UNKNOWN（{timeout_seconds:.0f} 秒無更新，"
                    "不代表不支援五檔）"
                )
        else:
            print(
                f"{code}：實收買方價格/量檔數="
                f"{levels['bid_levels']}/{levels['bid_volume_levels']}；"
                f"實收賣方價格/量檔數="
                f"{levels['ask_levels']}/{levels['ask_volume_levels']}；"
                f"有效正價買/賣檔數="
                f"{levels['valid_bid_levels']}/{levels['valid_ask_levels']}；"
                f"零股={levels['intraday_odd']}"
            )

    def is_standard_five_levels(item: dict[str, Any]) -> bool:
        return (
            item["bid_levels"] == 5
            and item["bid_volume_levels"] == 5
            and item["ask_levels"] == 5
            and item["ask_volume_levels"] == 5
            and not item["intraday_odd"]
        )

    five_levels_ok = any(is_standard_five_levels(item) for item in result.values())
    all_targets_received = target_codes == set(result) and all(
        is_standard_five_levels(item) for item in result.values()
    )
    print(f"即時五檔格式可確認：{'Y' if five_levels_ok else 'N'}")
    print(
        "5 檔標的全數實收五檔："
        + (
            "Y"
            if all_targets_received
            else "UNKNOWN（未全數收到或欄位不完整）"
        )
    )
    return five_levels_ok, result


def probe_limit_up_fields(
    api: sj.Shioaji,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    print("\n[4] 漲停偵測欄位")
    print("判斷邏輯：試撮價 == contract.limit_up ⇒ 試撮漲停")
    contracts = [
        contract
        for code in BIDASK_CODES
        if (contract := get_stock_contract(api, code)) is not None
    ]
    snapshots = api.snapshots(contracts) if contracts else []
    snapshot_by_code = {
        normalize_code(getattr(snapshot, "code", "")): snapshot
        for snapshot in snapshots
    }
    details: dict[str, dict[str, Any]] = {}

    for contract in contracts:
        code = normalize_code(getattr(contract, "code", ""))
        reference = getattr(contract, "reference", None)
        limit_up = getattr(contract, "limit_up", None)
        limit_down = getattr(contract, "limit_down", None)
        update_date = getattr(contract, "update_date", None)
        snapshot = snapshot_by_code.get(code)
        current = getattr(snapshot, "close", None) if snapshot is not None else None
        current_decimal = to_decimal(current)
        limit_up_decimal = to_decimal(limit_up)
        at_limit_up = (
            current_decimal is not None
            and limit_up_decimal is not None
            and current_decimal == limit_up_decimal
        )
        distance = (
            limit_up_decimal - current_decimal
            if current_decimal is not None and limit_up_decimal is not None
            else None
        )
        print(
            f"{code}：reference={format_number(reference)}；"
            f"limit_up={format_number(limit_up)}；"
            f"limit_down={format_number(limit_down)}；"
            f"update_date={update_date or 'N/A'}；"
            f"現價={format_number(current)}；"
            f"現價==limit_up={'Y' if at_limit_up else 'N'}；"
            f"距漲停={format_number(distance)}"
        )
        details[code] = {
            "reference": reference,
            "limit_up": limit_up,
            "limit_down": limit_down,
            "update_date": update_date,
            "current": current,
            "at_limit_up": at_limit_up,
        }

    fields_ok = bool(details) and all(
        (to_decimal(item["reference"]) or Decimal(0)) > 0
        and (to_decimal(item["limit_up"]) or Decimal(0)) > 0
        and (to_decimal(item["limit_down"]) or Decimal(0)) > 0
        for item in details.values()
    )
    print(f"漲跌停欄位可用：{'Y' if fields_ok else 'N'}")
    print(
        "注意：Snapshot 僅示範盤中判斷；正式盤前需同時保存 "
        "TickSTKv1.close（simtrade=True）與 BidAskSTKv1 五檔。"
    )
    return fields_ok, details


def probe_snapshot_capacity(
    api: sj.Shioaji, underlying_codes: list[str]
) -> tuple[bool, dict[str, Any]]:
    print("\n[5] 全部股期現貨標的單次 snapshot 與用量")
    contracts: list[Any] = []
    missing_codes: list[str] = []
    for code in underlying_codes:
        contract = get_stock_contract(api, code)
        if contract is None:
            missing_codes.append(code)
        else:
            contracts.append(contract)

    usage_before = None
    try:
        usage_before = api.usage()
    except Exception as exc:
        print(f"snapshot 前 api.usage() 失敗（{safe_error(exc)}）")

    print("官方單次 snapshot 上限=500 檔")
    if len(contracts) > 500:
        raise ValueError("snapshot contract count exceeds 500")

    requested_codes = {
        normalize_code(getattr(contract, "code", "")) for contract in contracts
    }
    started = time.perf_counter()
    snapshots = api.snapshots(contracts) if contracts else []
    elapsed = time.perf_counter() - started
    success_count = len(snapshots)
    returned_code_list = [
        normalize_code(getattr(snapshot, "code", "")) for snapshot in snapshots
    ]
    returned_codes = set(returned_code_list)
    missing_return_codes = requested_codes - returned_codes
    extra_return_codes = returned_codes - requested_codes
    duplicate_return_count = len(returned_code_list) - len(returned_codes)
    full_return = (
        bool(contracts)
        and requested_codes == returned_codes
        and duplicate_return_count == 0
    )
    over_200_ok = len(contracts) >= 200 and full_return

    print(
        f"單次 snapshots() 請求檔數={len(contracts)}；"
        f"成功回傳檔數={success_count}；耗時={elapsed:.3f} 秒"
    )
    print(
        f"找不到現貨合約檔數={len(missing_codes)}；"
        "回傳缺少/額外/重複="
        f"{len(missing_return_codes)}/{len(extra_return_codes)}/"
        f"{duplicate_return_count}"
    )
    print(f"一次掃描 200+ 檔且完整回傳：{'Y' if over_200_ok else 'N'}")

    usage_after = None
    try:
        usage_after = api.usage()
    except Exception as exc:
        print(f"snapshot 後 api.usage() 失敗（{safe_error(exc)}）")
    used_bytes = getattr(usage_after, "bytes", None)
    limit_bytes = getattr(usage_after, "limit_bytes", None)
    remaining_bytes = getattr(usage_after, "remaining_bytes", None)
    connections = getattr(usage_after, "connections", None)
    before_bytes = getattr(usage_before, "bytes", None)
    approximate_delta = (
        int(used_bytes) - int(before_bytes)
        if used_bytes is not None and before_bytes is not None
        else None
    )
    print(
        f"api.usage()：bytes={format_bytes(used_bytes)}；"
        f"limit_bytes={format_bytes(limit_bytes)}；"
        f"remaining_bytes={format_bytes(remaining_bytes)}；"
        f"connections={connections if connections is not None else 'N/A'}；"
        f"本次近似增量={format_bytes(approximate_delta)}"
    )
    return over_200_ok, {
        "requested": len(contracts),
        "returned": success_count,
        "elapsed": elapsed,
        "missing": len(missing_codes),
        "missing_return": len(missing_return_codes),
        "extra_return": len(extra_return_codes),
        "duplicate_return": duplicate_return_count,
        "full_return": full_return,
        "over_200_ok": over_200_ok,
        "used_bytes": used_bytes,
        "limit_bytes": limit_bytes,
        "remaining_bytes": remaining_bytes,
        "approximate_delta": approximate_delta,
    }


def cleanup_subscriptions(api: sj.Shioaji, subscriptions: list[Any]) -> None:
    for contract in reversed(subscriptions):
        try:
            api.quote.unsubscribe(
                contract,
                quote_type=QuoteType.BidAsk,
                intraday_odd=False,
                version=QuoteVersion.v1,
            )
        except Exception:
            pass
    subscriptions.clear()


def print_summary(results: dict[str, Any]) -> None:
    print("\n================ 結論摘要 ================")

    underlyings = results.get("underlyings")
    if isinstance(underlyings, list):
        print(f"1. 股期對應現貨清單：Y（{len(underlyings)} 檔）")
    else:
        print("1. 股期對應現貨清單：N（本次探針失敗）")

    historical = results.get("historical")
    historical_conclusive = results.get("historical_conclusive")
    if historical is None:
        print("2. 今日盤前試撮歷史 API 事後可撈回：N（本次未取得證據）")
    else:
        print(
            "2. 今日盤前試撮歷史 API 事後可撈回："
            f"{'Y' if historical else 'N'}"
            f"（證據狀態："
            f"{'CONCLUSIVE' if historical_conclusive else 'INCONCLUSIVE'}）"
        )

    bidask = results.get("bidask")
    print(
        "3. 即時 BidAsk 五檔："
        f"{'Y' if bidask is True else 'N'}"
        + ("" if bidask is not None else "（本次未完成驗證）")
    )

    limit_fields = results.get("limit_fields")
    print(
        "4. reference/limit_up/limit_down 可用："
        f"{'Y' if limit_fields is True else 'N'}"
        + ("" if limit_fields is not None else "（本次未完成驗證）")
    )

    capacity = results.get("capacity")
    capacity_details = results.get("capacity_details") or {}
    if capacity is None:
        print("5. 全清單單次 snapshot：N（本次未完成驗證）")
    else:
        print(
            "5. 全清單單次 snapshot 200+ 完整回傳："
            f"{'Y' if capacity else 'N'}"
            f"（{capacity_details.get('returned', 0)}/"
            f"{capacity_details.get('requested', 0)} 檔，"
            f"{capacity_details.get('elapsed', 0.0):.3f} 秒）"
        )
        print(
            "   用量："
            f"{format_bytes(capacity_details.get('used_bytes'))} / "
            f"{format_bytes(capacity_details.get('limit_bytes'))}"
        )
    print("==========================================")


def main() -> int:
    configure_stdout()
    results: dict[str, Any] = {
        "underlyings": None,
        "historical": None,
        "historical_conclusive": None,
        "bidask": None,
        "limit_fields": None,
        "capacity": None,
        "capacity_details": None,
    }
    api: sj.Shioaji | None = None
    active_subscriptions: list[Any] = []

    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        env_values = load_dotenv_manually(env_path)
        api_key = env_values.get("SHIOAJI_API_KEY", "").strip()
        secret_key = env_values.get("SHIOAJI_SECRET_KEY", "").strip()
        if not api_key or not secret_key:
            raise RuntimeError("missing credentials")
        REDACTIONS.extend((api_key, secret_key))
    except Exception as exc:
        print(f"login FAILED（設定讀取失敗：{safe_error(exc)}）")
        print_summary(results)
        return 1

    try:
        api = sj.Shioaji()
        try:
            with quiet_library_call():
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
            print_summary(results)
            return 1

        try:
            wait_for_contracts(api)
            results["underlyings"] = probe_stock_future_underlyings(api)
        except Exception as exc:
            print(f"\n[1] 執行失敗（{safe_error(exc)}）")

        try:
            historical, historical_conclusive, _ = probe_historical_auction_ticks(
                api, datetime.now(TAIPEI_TZ).date()
            )
            results["historical"] = historical
            results["historical_conclusive"] = historical_conclusive
        except Exception as exc:
            print(f"\n[2] 執行失敗（{safe_error(exc)}）")

        try:
            bidask, _ = probe_realtime_bidask(api, active_subscriptions)
            results["bidask"] = bidask
        except Exception as exc:
            print(f"\n[3] 執行失敗（{safe_error(exc)}）")

        try:
            limit_fields, _ = probe_limit_up_fields(api)
            results["limit_fields"] = limit_fields
        except Exception as exc:
            print(f"\n[4] 執行失敗（{safe_error(exc)}）")

        try:
            underlyings = results.get("underlyings") or []
            capacity, capacity_details = probe_snapshot_capacity(api, underlyings)
            results["capacity"] = capacity
            results["capacity_details"] = capacity_details
        except Exception as exc:
            print(f"\n[5] 執行失敗（{safe_error(exc)}）")

        print_summary(results)
        return 0
    finally:
        if api is not None:
            cleanup_subscriptions(api, active_subscriptions)
            try:
                with quiet_library_call():
                    api.logout()
            except Exception:
                pass


def run_with_output_guard() -> int:
    """先在記憶體產生白名單報告，確認無敏感值後才送到 stdout。"""
    configure_stdout()
    original_stdout = sys.stdout
    captured_stdout = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured_stdout):
            exit_code = main()
    except Exception as exc:
        with contextlib.redirect_stdout(captured_stdout):
            print(f"探針未預期失敗（{safe_error(exc)}）")
        exit_code = 1

    report = captured_stdout.getvalue()
    contains_known_identifier = any(
        secret and secret in report for secret in REDACTIONS
    )
    contains_labeled_identifier = bool(
        re.search(
            r"(?i)\b(person[_ -]?id|account(?:[_ -]?id)?)\s*[:=]",
            report,
        )
    )
    if contains_known_identifier or contains_labeled_identifier:
        original_stdout.write("SECURITY FAIL：輸出含敏感識別資訊，報告已抑制。\n")
        original_stdout.flush()
        return 3

    original_stdout.write(report)
    original_stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_with_output_guard())
