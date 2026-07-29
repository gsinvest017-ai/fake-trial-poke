#!/usr/bin/env python3
"""Fetch and summarize FinMind margin/short-sale data for the three focus stocks.

The script intentionally caps all source data at 2026-07-23.  Each successful
FinMind response is cached verbatim before analysis so reruns do not consume
the free-tier request quota.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DATASET = "TaiwanStockMarginPurchaseShortSale"
ENDPOINT = "https://api.finmindtrade.com/api/v4/data"
DOCUMENTATION = "https://finmind.github.io/tutor/TaiwanMarket/Chip/"
AS_OF = date(2026, 7, 23)
REQUEST_START = AS_OF - timedelta(days=120)
SERIES_TRADING_DAYS = 60
STOCKS = {
    "8039": "台虹",
    "2392": "正崴",
    "2201": "裕隆",
}
REQUIRED_FIELDS = {
    "date",
    "stock_id",
    "MarginPurchaseBuy",
    "MarginPurchaseCashRepayment",
    "MarginPurchaseLimit",
    "MarginPurchaseSell",
    "MarginPurchaseTodayBalance",
    "MarginPurchaseYesterdayBalance",
    "OffsetLoanAndShort",
    "ShortSaleBuy",
    "ShortSaleCashRepayment",
    "ShortSaleLimit",
    "ShortSaleSell",
    "ShortSaleTodayBalance",
    "ShortSaleYesterdayBalance",
}


class FetchError(RuntimeError):
    """Raised when FinMind cannot be fetched or returns an invalid response."""


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write UTF-8 JSON atomically to avoid leaving a truncated cache/output."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def cache_path_for(cache_dir: Path, stock_id: str) -> Path:
    return cache_dir / (
        f"finmind_margin_{stock_id}_{REQUEST_START.isoformat()}_"
        f"{AS_OF.isoformat()}.json"
    )


def load_cached_response(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FetchError(f"快取無法讀取：{exc}") from exc
    if not isinstance(payload, dict):
        raise FetchError("快取根節點不是 JSON object")
    return payload


def request_finmind(stock_id: str, max_attempts: int = 5) -> dict[str, Any]:
    params = {
        "dataset": DATASET,
        "data_id": stock_id,
        "start_date": REQUEST_START.isoformat(),
        "end_date": AS_OF.isoformat(),
    }
    headers = {
        "Accept": "application/json",
        "User-Agent": "fake-auction-analysis/1.0",
    }
    token = os.environ.get("FINMIND_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{ENDPOINT}?{urlencode(params)}"
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with urlopen(Request(url, headers=headers), timeout=30) as response:
                raw = response.read()
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise FetchError("FinMind response 根節點不是 JSON object")
            api_status = payload.get("status")
            if api_status == 402:
                raise HTTPError(url, 402, str(payload.get("msg", "")), None, None)
            if api_status not in (None, 200):
                raise FetchError(
                    f"FinMind API status={api_status}: {payload.get('msg', '未知錯誤')}"
                )
            if not isinstance(payload.get("data"), list):
                raise FetchError("FinMind response 缺少 data list")
            return payload
        except HTTPError as exc:
            last_error = exc
            if exc.code != 402 or attempt == max_attempts - 1:
                break
        except (URLError, TimeoutError, json.JSONDecodeError, FetchError) as exc:
            last_error = exc
            break

        # FinMind free tier may answer 402 when requests arrive too quickly.
        time.sleep(2**attempt)

    raise FetchError(f"FinMind 抓取失敗：{last_error}")


def get_response(
    stock_id: str, cache_dir: Path, refresh: bool
) -> tuple[dict[str, Any], str]:
    cache_path = cache_path_for(cache_dir, stock_id)
    if cache_path.exists() and not refresh:
        return load_cached_response(cache_path), "cache"
    payload = request_finmind(stock_id)
    atomic_write_json(cache_path, payload)
    return payload, "network"


def integer_value(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool):
        raise ValueError(f"{field} 不可為 boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise ValueError(f"{field} 不是整數")


def normalize_rows(
    stock_id: str, raw_rows: list[Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    by_date: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    rejected = 0
    duplicate_dates = 0

    for raw in raw_rows:
        if not isinstance(raw, dict):
            rejected += 1
            continue
        if str(raw.get("stock_id", "")) != stock_id:
            rejected += 1
            continue
        missing = REQUIRED_FIELDS.difference(raw)
        if missing:
            rejected += 1
            continue
        try:
            row_date = date.fromisoformat(str(raw["date"]))
            if row_date > AS_OF:
                continue
            row = {
                "date": row_date.isoformat(),
                "stock_id": stock_id,
                "margin_buy_lots": integer_value(raw, "MarginPurchaseBuy"),
                "margin_cash_repayment_lots": integer_value(
                    raw, "MarginPurchaseCashRepayment"
                ),
                "margin_limit_lots": integer_value(raw, "MarginPurchaseLimit"),
                "margin_sell_lots": integer_value(raw, "MarginPurchaseSell"),
                "margin_balance_lots": integer_value(
                    raw, "MarginPurchaseTodayBalance"
                ),
                "margin_previous_balance_lots": integer_value(
                    raw, "MarginPurchaseYesterdayBalance"
                ),
                "offset_loan_and_short_lots": integer_value(
                    raw, "OffsetLoanAndShort"
                ),
                "short_buy_lots": integer_value(raw, "ShortSaleBuy"),
                "short_cash_repayment_lots": integer_value(
                    raw, "ShortSaleCashRepayment"
                ),
                "short_limit_lots": integer_value(raw, "ShortSaleLimit"),
                "short_sell_lots": integer_value(raw, "ShortSaleSell"),
                "short_balance_lots": integer_value(
                    raw, "ShortSaleTodayBalance"
                ),
                "short_previous_balance_lots": integer_value(
                    raw, "ShortSaleYesterdayBalance"
                ),
                "note": str(raw.get("Note", "") or ""),
            }
        except (TypeError, ValueError):
            rejected += 1
            continue

        if row["date"] in by_date:
            duplicate_dates += 1
        by_date[row["date"]] = row

    rows = [by_date[key] for key in sorted(by_date)]
    if rejected:
        notes.append(f"{rejected} 筆回應因代碼、欄位或型別不符而排除。")
    if duplicate_dates:
        notes.append(f"{duplicate_dates} 筆重複日期以最後一筆覆蓋。")
    if len(rows) > SERIES_TRADING_DAYS:
        rows = rows[-SERIES_TRADING_DAYS:]
    return rows, notes


def safe_ratio_pct(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100.0, 4)


def add_derived_series_fields(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["short_margin_ratio_pct"] = safe_ratio_pct(
            row["short_balance_lots"], row["margin_balance_lots"]
        )
        row["margin_utilization_pct"] = safe_ratio_pct(
            row["margin_balance_lots"], row["margin_limit_lots"]
        )


def window_change(
    rows: list[dict[str, Any]], field: str, periods: int
) -> dict[str, Any]:
    if len(rows) <= periods:
        return {
            "availability": "unavailable",
            "reason": f"需要至少 {periods + 1} 個交易日，實得 {len(rows)}。",
            "periods": periods,
            "from_date": None,
            "to_date": rows[-1]["date"] if rows else None,
            "lots": None,
            "pct": None,
        }
    start_row = rows[-(periods + 1)]
    end_row = rows[-1]
    start = start_row[field]
    end = end_row[field]
    return {
        "availability": "available",
        "reason": None,
        "periods": periods,
        "from_date": start_row["date"],
        "to_date": end_row["date"],
        "lots": end - start,
        "pct": safe_ratio_pct(end - start, start),
    }


def trend_label(change: dict[str, Any]) -> str:
    value = change.get("lots")
    if value is None:
        return "unavailable"
    if value > 0:
        return "increasing"
    if value < 0:
        return "decreasing"
    return "flat"


def direction_counts(
    rows: list[dict[str, Any]], field: str, periods: int
) -> dict[str, int] | None:
    if len(rows) <= periods:
        return None
    values = [row[field] for row in rows[-(periods + 1) :]]
    changes = [new - old for old, new in zip(values, values[1:])]
    return {
        "up_days": sum(change > 0 for change in changes),
        "down_days": sum(change < 0 for change in changes),
        "flat_days": sum(change == 0 for change in changes),
    }


def unavailable_stock(stock_id: str, stock_name: str, reason: str) -> dict[str, Any]:
    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "source": {
            "provider": "FinMind",
            "dataset": DATASET,
        },
        "as_of": None,
        "availability": "unavailable",
        "field_availability": {
            "margin_balance_lots": "unavailable",
            "margin_change_5d": "unavailable",
            "margin_change_20d": "unavailable",
            "short_balance_lots": "unavailable",
            "short_margin_ratio_pct": "unavailable",
            "margin_utilization_pct": "unavailable",
        },
        "quality_notes": [reason],
        "trading_days": 0,
        "latest": None,
        "changes": None,
        "trend": None,
        "series": [],
    }


def analyze_stock(
    stock_id: str,
    stock_name: str,
    payload: dict[str, Any],
    fetch_mode: str,
) -> dict[str, Any]:
    raw_rows = payload.get("data")
    if not isinstance(raw_rows, list):
        return unavailable_stock(stock_id, stock_name, "FinMind response 沒有 data list。")

    rows, quality_notes = normalize_rows(stock_id, raw_rows)
    if not rows:
        return unavailable_stock(
            stock_id,
            stock_name,
            f"FinMind 在 {REQUEST_START.isoformat()} 至 {AS_OF.isoformat()} 無可用資料。",
        )
    add_derived_series_fields(rows)

    latest = rows[-1]
    margin_5d = window_change(rows, "margin_balance_lots", 5)
    margin_20d = window_change(rows, "margin_balance_lots", 20)
    short_5d = window_change(rows, "short_balance_lots", 5)
    short_20d = window_change(rows, "short_balance_lots", 20)

    availability = "available" if len(rows) >= 21 else "partial"
    if latest["date"] != AS_OF.isoformat():
        availability = "partial"
        quality_notes.append(
            f"最新資料日為 {latest['date']}，早於指定截止日 {AS_OF.isoformat()}。"
        )
    if len(rows) < SERIES_TRADING_DAYS:
        availability = "partial"
        quality_notes.append(
            f"要求近約 {SERIES_TRADING_DAYS} 個交易日，實得 {len(rows)} 日。"
        )
    quality_notes.extend(
        [
            "數量單位依 FinMind/TWSE 個股融資融券表為張。",
            "5日/20日變化以最新餘額減去 5/20 個交易期前餘額；不是買賣增減欄位加總。",
            "券資比＝融券餘額／融資餘額；融資使用率＝融資餘額／FinMind MarginPurchaseLimit。",
            "資料截止 2026-07-23，未假設 2026-07-24 盤後融資融券已發布。",
        ]
    )

    utilization_available = (
        "available" if latest["margin_utilization_pct"] is not None else "unavailable"
    )
    ratio_available = (
        "available" if latest["short_margin_ratio_pct"] is not None else "unavailable"
    )
    if utilization_available == "unavailable":
        quality_notes.append("融資限額為零或缺乏有效分母，融資使用率未取得。")
    if ratio_available == "unavailable":
        quality_notes.append("融資餘額為零，券資比無法計算。")

    latest_summary = {
        "date": latest["date"],
        "margin_balance_lots": latest["margin_balance_lots"],
        "margin_limit_lots": latest["margin_limit_lots"],
        "margin_utilization_pct": latest["margin_utilization_pct"],
        "short_balance_lots": latest["short_balance_lots"],
        "short_limit_lots": latest["short_limit_lots"],
        "short_margin_ratio_pct": latest["short_margin_ratio_pct"],
        "margin_buy_lots": latest["margin_buy_lots"],
        "margin_sell_lots": latest["margin_sell_lots"],
        "margin_cash_repayment_lots": latest["margin_cash_repayment_lots"],
        "short_sell_lots": latest["short_sell_lots"],
        "short_buy_lots": latest["short_buy_lots"],
        "short_cash_repayment_lots": latest["short_cash_repayment_lots"],
        "offset_loan_and_short_lots": latest["offset_loan_and_short_lots"],
        "note": latest["note"],
    }
    return {
        "stock_id": stock_id,
        "stock_name": stock_name,
        "source": {
            "provider": "FinMind",
            "dataset": DATASET,
            "fetch_mode": fetch_mode,
        },
        "as_of": latest["date"],
        "availability": availability,
        "field_availability": {
            "margin_balance_lots": "available",
            "margin_change_5d": margin_5d["availability"],
            "margin_change_20d": margin_20d["availability"],
            "short_balance_lots": "available",
            "short_margin_ratio_pct": ratio_available,
            "margin_utilization_pct": utilization_available,
        },
        "quality_notes": quality_notes,
        "trading_days": len(rows),
        "latest": latest_summary,
        "changes": {
            "margin_5d": margin_5d,
            "margin_20d": margin_20d,
            "short_5d": short_5d,
            "short_20d": short_20d,
        },
        "trend": {
            "margin_5d": trend_label(margin_5d),
            "margin_20d": trend_label(margin_20d),
            "short_5d": trend_label(short_5d),
            "short_20d": trend_label(short_20d),
            "margin_direction_days_5d": direction_counts(
                rows, "margin_balance_lots", 5
            ),
            "margin_direction_days_20d": direction_counts(
                rows, "margin_balance_lots", 20
            ),
        },
        "series": rows,
    }


def build_output(cache_dir: Path, refresh: bool) -> dict[str, Any]:
    stocks: dict[str, Any] = {}
    global_notes: list[str] = []
    for stock_id, stock_name in STOCKS.items():
        try:
            payload, fetch_mode = get_response(stock_id, cache_dir, refresh)
            stocks[stock_id] = analyze_stock(
                stock_id, stock_name, payload, fetch_mode
            )
        except FetchError as exc:
            reason = str(exc)
            stocks[stock_id] = unavailable_stock(stock_id, stock_name, reason)
            global_notes.append(f"{stock_id} {stock_name}：{reason}")

    states = [stock["availability"] for stock in stocks.values()]
    if states and all(state == "available" for state in states):
        overall_availability = "available"
    elif states and all(state == "unavailable" for state in states):
        overall_availability = "unavailable"
    else:
        overall_availability = "partial"

    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": {
            "provider": "FinMind",
            "dataset": DATASET,
            "endpoint": ENDPOINT,
            "documentation": DOCUMENTATION,
            "requested_start": REQUEST_START.isoformat(),
            "requested_end": AS_OF.isoformat(),
            "cache_policy": "successful response cached per stock; cache read first",
            "retry_policy": "HTTP/API 402 exponential backoff: 1, 2, 4, 8 seconds",
        },
        "as_of": AS_OF.isoformat(),
        "availability": overall_availability,
        "quality_notes": global_notes
        + [
            "本檔只涵蓋 8039、2392、2201，不含 2026-07-24 尚未確認發布的盤後資料。",
            "融資使用率僅由明確的 MarginPurchaseTodayBalance / MarginPurchaseLimit 計算。",
        ],
        "stocks": stocks,
    }


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=base_dir / "margin.json",
        help="分析 JSON 輸出路徑",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=base_dir / "cache",
        help="FinMind 原始回應快取目錄",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="略過既有快取並重新請求（一般重跑不應使用）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = build_output(args.cache_dir.resolve(), args.refresh)
    atomic_write_json(args.output.resolve(), output)
    print(
        f"Wrote {args.output.resolve()} "
        f"(availability={output['availability']}, as_of={output['as_of']})"
    )
    return 0 if output["availability"] != "unavailable" else 1


if __name__ == "__main__":
    sys.exit(main())
