#!/usr/bin/env python3
"""下載並彙整指定台股近 60 個交易日的三大法人買賣超。

資料來源為 FinMind TaiwanStockInstitutionalInvestorsBuySell。所有 API 回應都會
快取於 data/analysis/cache/；預設重跑會優先讀取快取，避免免費層頻率限制。

本程式刻意只使用 Python 標準函式庫，方便在專案既有虛擬環境中獨立重跑。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


API_URL = "https://api.finmindtrade.com/api/v4/data"
DOCS_URL = "https://finmind.github.io/tutor/TaiwanMarket/Chip/"
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
OUTPUT_PATH = SCRIPT_DIR / "chips.json"

PROJECT_DIR = SCRIPT_DIR.parent.parent
ENV_PATH = PROJECT_DIR / ".env"

STOCKS = {
    "8039": "台虹",
    "2392": "正崴",
    "2201": "裕隆",
    "6488": "環球晶",
    "2481": "強茂",
    "6147": "頎邦",
}

# FinMind / TWSE 官方口徑：
# - 三大法人中的「外資及陸資」為 Foreign_Investor，不含 Foreign_Dealer_Self。
# - Foreign_Dealer_Self 已計入證券自營商買賣，僅另列觀察，不能再加進三大法人合計。
# - 自營商跨年代合計為 Dealer + Dealer_self + Dealer_Hedging。
CATEGORY_COMPONENTS = {
    "foreign": ("Foreign_Investor",),
    "investment_trust": ("Investment_Trust",),
    "dealer": ("Dealer", "Dealer_self", "Dealer_Hedging"),
}
SUPPLEMENTARY_COMPONENTS = {
    "foreign_dealer_self": ("Foreign_Dealer_Self",),
}
ALL_RAW_CATEGORIES = tuple(
    dict.fromkeys(
        component
        for values in (*CATEGORY_COMPONENTS.values(), *SUPPLEMENTARY_COMPONENTS.values())
        for component in values
    )
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--as-of",
        default="2026-07-23",
        help="資料截止日，格式 YYYY-MM-DD；預設為 2026-07-23。",
    )
    parser.add_argument(
        "--trading-days",
        type=int,
        default=60,
        help="輸出最近幾個有法人資料的交易日，預設 60。",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="忽略既有快取重新下載。一般重跑不建議使用。",
    )
    return parser.parse_args()


def parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"無效日期 {value!r}，請使用 YYYY-MM-DD。") from exc


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def sanitized_message(value: Any, limit: int = 500) -> str:
    """避免把回應中的 token_tail 等敏感欄位寫入最終報告。"""
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    text = re.sub(r"https?://\S+", "[URL omitted]", text)
    return text[:limit]


def cache_filename(dataset: str, tag: str) -> Path:
    safe_tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tag)
    return CACHE_DIR / f"chips_finmind_{dataset}_{safe_tag}.json"


def dotenv_token() -> str:
    if not ENV_PATH.exists():
        return ""
    for raw_line in ENV_PATH.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in {"FINMIND_API_TOKEN", "FINMIND_TOKEN"}:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            return value
    return ""


def request_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "fake-auction-stock-analysis/1.0",
    }
    token = (
        os.environ.get("FINMIND_API_TOKEN")
        or os.environ.get("FINMIND_TOKEN")
        or dotenv_token()
    )
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def save_attempt_response(
    canonical_path: Path,
    payload: dict[str, Any],
    attempt: int,
    http_status: int | None,
) -> None:
    """保存非最終的 402 回應；重跑不會把它誤認為有效快取。"""
    error_path = canonical_path.with_name(
        f"{canonical_path.stem}_attempt{attempt:02d}_http{http_status or 0}.json"
    )
    # FinMind 錯誤回應可能含 token_tail；錯誤快取不保存該欄位。
    safe_payload = {key: value for key, value in payload.items() if key != "token_tail"}
    atomic_write_json(error_path, safe_payload)


def load_cached_response(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def fetch_finmind(
    params: dict[str, str],
    cache_tag: str,
    *,
    refresh: bool,
    max_attempts: int = 5,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """取得一個 FinMind response，遇認證、限流或暫時錯誤時指數退避。"""
    dataset = params["dataset"]
    cache_path = cache_filename(dataset, cache_tag)
    if not refresh and cache_path.exists():
        cached = load_cached_response(cache_path)
        if cached is not None:
            return cached, {
                "transport": "cache",
                "cache_file": cache_path.relative_to(SCRIPT_DIR.parent.parent).as_posix(),
                "http_status": None,
            }

    query = urllib.parse.urlencode(params)
    url = f"{API_URL}?{query}"
    last_payload: dict[str, Any] = {}
    last_http_status: int | None = None

    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(url, headers=request_headers())
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                last_http_status = int(response.status)
                raw = response.read()
                decoded = json.loads(raw.decode("utf-8"))
                last_payload = decoded if isinstance(decoded, dict) else {
                    "status": last_http_status,
                    "msg": "FinMind response was not a JSON object.",
                    "data": [],
                }
        except urllib.error.HTTPError as exc:
            last_http_status = int(exc.code)
            raw = exc.read()
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {
                    "status": last_http_status,
                    "msg": f"HTTP {last_http_status}",
                    "data": [],
                }
            last_payload = decoded if isinstance(decoded, dict) else {
                "status": last_http_status,
                "msg": f"HTTP {last_http_status}",
                "data": [],
            }
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_payload = {
                "status": 0,
                "msg": f"{type(exc).__name__}: {sanitized_message(exc)}",
                "data": [],
            }
            last_http_status = None

        api_status = last_payload.get("status")
        is_rate_limited = (
            last_http_status in {401, 402, 429}
            or api_status in {401, 402, 429}
        )
        is_transient_failure = (
            is_rate_limited
            or api_status == 0
            or (last_http_status is not None and last_http_status >= 500)
        )
        if is_transient_failure:
            save_attempt_response(cache_path, last_payload, attempt, last_http_status)
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))
                continue
            # 不把認證／限流／網路／伺服器暫時錯誤寫成 canonical cache，讓下次重跑能重試。
            return last_payload, {
                "transport": "network",
                "cache_file": None,
                "http_status": last_http_status,
                "attempts": attempt,
            }

        # 成功及非暫時性的確定性錯誤都快取；後者可避免反覆打相同付費端點。
        safe_payload = {key: value for key, value in last_payload.items() if key != "token_tail"}
        atomic_write_json(cache_path, safe_payload)
        return safe_payload, {
            "transport": "network",
            "cache_file": cache_path.relative_to(SCRIPT_DIR.parent.parent).as_posix(),
            "http_status": last_http_status,
            "attempts": attempt,
        }

    # 理論上不會走到這裡；保留防禦性回傳。
    return last_payload, {
        "transport": "network",
        "cache_file": cache_path.relative_to(SCRIPT_DIR.parent.parent).as_posix(),
        "http_status": last_http_status,
        "attempts": max_attempts,
    }


def api_result_status(payload: dict[str, Any]) -> tuple[bool, str]:
    status = payload.get("status")
    data = payload.get("data")
    if status == 200 and isinstance(data, list):
        return True, "success"
    message = sanitized_message(payload.get("msg") or f"FinMind status={status}")
    lowered = message.lower()
    if status == 400 and ("level is free" in lowered or "update your user level" in lowered):
        message = "FinMind free 層無權存取此會員限定資料集。"
    return False, message


def net_for_components(component_values: dict[str, int], components: tuple[str, ...]) -> int:
    return sum(component_values.get(component, 0) for component in components)


def streak(values: list[int]) -> dict[str, Any]:
    if not values or values[-1] == 0:
        return {
            "direction": "flat",
            "days": 0,
            "net_shares": 0,
            "net_lots": 0.0,
        }
    sign = 1 if values[-1] > 0 else -1
    count = 0
    accumulated = 0
    for value in reversed(values):
        if value == 0 or (1 if value > 0 else -1) != sign:
            break
        count += 1
        accumulated += value
    return {
        "direction": "buy" if sign > 0 else "sell",
        "days": count,
        "net_shares": accumulated,
        "net_lots": round(accumulated / 1000, 3),
    }


def window_summary(values: list[int], dates: list[str], window: int) -> dict[str, Any]:
    actual = min(window, len(values))
    selected = values[-actual:] if actual else []
    total = sum(selected)
    return {
        "requested_days": window,
        "actual_days": actual,
        "start_date": dates[-actual] if actual else None,
        "end_date": dates[-1] if actual else None,
        "net_shares": total,
        "net_lots": round(total / 1000, 3),
    }


def summarize_series(values: list[int], dates: list[str]) -> dict[str, Any]:
    latest = values[-1] if values else None
    recent_5 = window_summary(values, dates, 5)
    recent_20 = window_summary(values, dates, 20)
    prior_5_values = values[-10:-5] if len(values) >= 10 else []
    prior_5_total = sum(prior_5_values) if prior_5_values else None
    recent_5_total = recent_5["net_shares"] if recent_5["actual_days"] else None
    acceleration = (
        recent_5_total - prior_5_total
        if recent_5_total is not None and prior_5_total is not None
        else None
    )
    return {
        "latest_date": dates[-1] if dates else None,
        "latest_net_shares": latest,
        "latest_net_lots": round(latest / 1000, 3) if latest is not None else None,
        "last_5d": recent_5,
        "last_20d": recent_20,
        "prior_5d_net_shares": prior_5_total,
        "prior_5d_net_lots": (
            round(prior_5_total / 1000, 3) if prior_5_total is not None else None
        ),
        "five_day_acceleration_vs_prior_5d_shares": acceleration,
        "five_day_acceleration_vs_prior_5d_lots": (
            round(acceleration / 1000, 3) if acceleration is not None else None
        ),
        "streak": streak(values),
    }


def unavailable_stock(
    stock_id: str,
    reason: str,
    request_meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "stock_id": stock_id,
        "stock_name": STOCKS[stock_id],
        "availability": "unavailable",
        "reason": reason,
        "as_of": None,
        "trading_days": 0,
        "summary": {},
        "daily": [],
        "classification": {
            "availability": "unavailable",
            "reason": "法人資料未取得，分類資料另見頂層 source_status.stock_info。",
        },
        "disposition": {
            "availability": "unavailable",
            "reason": "法人資料未取得時未推論處置狀態。",
        },
        "full_cash_settlement": {
            "availability": "unavailable",
            "reason": "本資料管線沒有可可靠驗證的全額交割資料源；未以市場別推論。",
        },
        "request_meta": request_meta or {},
    }


def aggregate_stock(
    stock_id: str,
    payload: dict[str, Any],
    *,
    trading_days: int,
    request_meta: dict[str, Any],
) -> dict[str, Any]:
    ok, message = api_result_status(payload)
    rows = payload.get("data") if ok else []
    if not ok or not rows:
        reason = message if not ok else "FinMind 回傳成功但 data 為空。"
        return unavailable_stock(stock_id, reason, request_meta)

    by_date: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    unexpected_names: set[str] = set()
    malformed_rows = 0
    for row in rows:
        try:
            row_stock_id = str(row["stock_id"])
            row_date = str(row["date"])
            name = str(row["name"])
            buy = int(row["buy"])
            sell = int(row["sell"])
        except (KeyError, TypeError, ValueError):
            malformed_rows += 1
            continue
        if row_stock_id != stock_id:
            continue
        if name not in ALL_RAW_CATEGORIES:
            unexpected_names.add(name)
            continue
        by_date[row_date][name] += buy - sell

    dates = sorted(by_date)
    if not dates:
        return unavailable_stock(
            stock_id,
            "FinMind response 沒有可解析的指定股票法人資料。",
            request_meta,
        )
    dates = dates[-trading_days:]

    cumulative = {
        category: 0 for category in (*CATEGORY_COMPONENTS, *SUPPLEMENTARY_COMPONENTS)
    }
    cumulative["total"] = 0
    series: dict[str, list[int]] = {
        category: []
        for category in (
            *CATEGORY_COMPONENTS.keys(),
            *SUPPLEMENTARY_COMPONENTS.keys(),
            "total",
        )
    }
    daily: list[dict[str, Any]] = []

    for row_date in dates:
        components = {name: by_date[row_date].get(name, 0) for name in ALL_RAW_CATEGORIES}
        category_net = {
            category: net_for_components(components, names)
            for category, names in CATEGORY_COMPONENTS.items()
        }
        category_net.update(
            {
                category: net_for_components(components, names)
                for category, names in SUPPLEMENTARY_COMPONENTS.items()
            }
        )
        category_net["total"] = sum(category_net.values())
        # 外資自營商已包含於證券自營商，不能在三大法人合計再加一次。
        category_net["total"] -= category_net["foreign_dealer_self"]

        entry: dict[str, Any] = {
            "date": row_date,
            "foreign_net_shares": category_net["foreign"],
            "foreign_net_lots": round(category_net["foreign"] / 1000, 3),
            "investment_trust_net_shares": category_net["investment_trust"],
            "investment_trust_net_lots": round(
                category_net["investment_trust"] / 1000, 3
            ),
            "dealer_net_shares": category_net["dealer"],
            "dealer_net_lots": round(category_net["dealer"] / 1000, 3),
            "foreign_dealer_self_net_shares": category_net["foreign_dealer_self"],
            "foreign_dealer_self_net_lots": round(
                category_net["foreign_dealer_self"] / 1000, 3
            ),
            "total_net_shares": category_net["total"],
            "total_net_lots": round(category_net["total"] / 1000, 3),
            "components_net_shares": components,
        }
        for category, value in category_net.items():
            series[category].append(value)
            cumulative[category] += value
            entry[f"{category}_cumulative_net_shares"] = cumulative[category]
            entry[f"{category}_cumulative_net_lots"] = round(
                cumulative[category] / 1000, 3
            )
        daily.append(entry)

    notes = [
        "買賣超原始單位為股；net_lots 以 1 張=1,000 股換算，可能含零股小數。",
        "外資 = Foreign_Investor（外資及陸資，不含外資自營商）。",
        "投信 = Investment_Trust。",
        "自營商合計 = Dealer + Dealer_self + Dealer_Hedging；2026 年資料通常由後兩者構成。",
        "Foreign_Dealer_Self（外資自營商）另列觀察；依 TWSE 口徑已計入證券自營商，不重複納入三大法人合計。",
        "連買/連賣遇淨額 0 即中斷；僅在本次可得交易日序列內計算。",
        "2026-07-24 為盤前報告日，法人買賣超最新完整資料預期截止前一交易日 2026-07-23。",
    ]
    if len(dates) < trading_days:
        notes.append(
            f"僅取得 {len(dates)} 個可解析交易日，少於要求的 {trading_days} 日。"
        )
    if malformed_rows:
        notes.append(f"略過 {malformed_rows} 筆欄位不完整或型別無法解析的原始資料。")
    if unexpected_names:
        notes.append(
            "未納入未知法人分類：" + ", ".join(sorted(unexpected_names)) + "。"
        )

    return {
        "stock_id": stock_id,
        "stock_name": STOCKS[stock_id],
        "availability": "available",
        "reason": None,
        "as_of": dates[-1],
        "period_start": dates[0],
        "trading_days": len(dates),
        "summary": {
            category: summarize_series(values, dates)
            for category, values in series.items()
        },
        "daily": daily,
        "quality_notes": notes,
        "request_meta": request_meta,
    }


def attach_stock_info(
    stocks: dict[str, dict[str, Any]],
    payload: dict[str, Any],
    request_meta: dict[str, Any],
) -> dict[str, Any]:
    ok, message = api_result_status(payload)
    rows = payload.get("data") if ok else []
    status = {
        "availability": "available" if ok and rows else "unavailable",
        "reason": None if ok and rows else (message if not ok else "FinMind data 為空。"),
        "request_meta": request_meta,
    }
    for stock_id, stock in stocks.items():
        matches = [
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("stock_id")) == stock_id
        ]
        if not matches:
            stock["classification"] = {
                "availability": "unavailable",
                "reason": status["reason"] or "TaiwanStockInfo 無此股票。",
            }
            continue
        industries = sorted(
            {
                str(row.get("industry_category")).strip()
                for row in matches
                if row.get("industry_category")
            }
        )
        markets = sorted(
            {str(row.get("type")).strip() for row in matches if row.get("type")}
        )
        names = sorted(
            {str(row.get("stock_name")).strip() for row in matches if row.get("stock_name")}
        )
        dates = sorted({str(row.get("date")) for row in matches if row.get("date")})
        stock["classification"] = {
            "availability": "available",
            "stock_names": names,
            "industry_categories": industries,
            "market_types": markets,
            "as_of": dates[-1] if dates else None,
            "quality_note": (
                "FinMind TaiwanStockInfo 可能同時提供上位類別與細產業，故保留全部唯一分類。"
            ),
        }
    return status


def attach_disposition(
    stock: dict[str, Any],
    payload: dict[str, Any],
    request_meta: dict[str, Any],
    requested_as_of: date,
) -> None:
    ok, message = api_result_status(payload)
    rows = payload.get("data") if ok else []
    if not ok:
        stock["disposition"] = {
            "availability": "unavailable",
            "reason": message,
            "request_meta": request_meta,
        }
        return

    valid_rows = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("stock_id")) == stock["stock_id"]
    ]
    active_rows = []
    for row in valid_rows:
        try:
            period_start = date.fromisoformat(str(row["period_start"]))
            period_end = date.fromisoformat(str(row["period_end"]))
        except (KeyError, ValueError):
            continue
        if period_start <= requested_as_of <= period_end:
            active_rows.append(row)
    stock["disposition"] = {
        "availability": "available",
        "is_active_as_of": bool(active_rows),
        "as_of": requested_as_of.isoformat(),
        "active_records": active_rows,
        "records_in_lookback": valid_rows,
        "request_meta": request_meta,
        "quality_note": "狀態依 period_start <= 截止日 <= period_end 判定。",
    }


def main() -> int:
    args = parse_args()
    requested_as_of = parse_iso_date(args.as_of)
    if args.trading_days <= 0:
        raise SystemExit("--trading-days 必須大於 0。")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    start_date = requested_as_of - timedelta(days=max(120, args.trading_days * 2))
    end_date_exclusive = requested_as_of + timedelta(days=1)

    stocks: dict[str, dict[str, Any]] = {}
    institutional_status: dict[str, Any] = {}
    for stock_id in STOCKS:
        tag = f"{stock_id}_{start_date:%Y%m%d}_{end_date_exclusive:%Y%m%d}"
        payload, request_meta = fetch_finmind(
            {
                "dataset": "TaiwanStockInstitutionalInvestorsBuySell",
                "data_id": stock_id,
                "start_date": start_date.isoformat(),
                "end_date": end_date_exclusive.isoformat(),
            },
            tag,
            refresh=args.refresh,
        )
        stock = aggregate_stock(
            stock_id,
            payload,
            trading_days=args.trading_days,
            request_meta=request_meta,
        )
        stocks[stock_id] = stock
        institutional_status[stock_id] = {
            "availability": stock["availability"],
            "reason": stock.get("reason"),
            "request_meta": request_meta,
        }

    stock_info_payload, stock_info_meta = fetch_finmind(
        {"dataset": "TaiwanStockInfo"},
        "all",
        refresh=args.refresh,
    )
    stock_info_status = attach_stock_info(stocks, stock_info_payload, stock_info_meta)

    disposition_start = requested_as_of - timedelta(days=365)
    disposition_status: dict[str, Any] = {}
    for stock_id, stock in stocks.items():
        tag = (
            f"{stock_id}_{disposition_start:%Y%m%d}_{end_date_exclusive:%Y%m%d}"
        )
        payload, request_meta = fetch_finmind(
            {
                "dataset": "TaiwanStockDispositionSecuritiesPeriod",
                "data_id": stock_id,
                "start_date": disposition_start.isoformat(),
                "end_date": end_date_exclusive.isoformat(),
            },
            tag,
            refresh=args.refresh,
        )
        attach_disposition(stock, payload, request_meta, requested_as_of)
        disposition_status[stock_id] = {
            "availability": stock["disposition"]["availability"],
            "reason": stock["disposition"].get("reason"),
            "request_meta": request_meta,
        }

    for stock in stocks.values():
        stock["full_cash_settlement"] = {
            "availability": "unavailable",
            "reason": (
                "本次指定的 FinMind 公開資料中沒有可可靠驗證「全額交割股」當日狀態的欄位；"
                "未以市場別、處置或名稱自行猜測。"
            ),
        }

    available_as_of = sorted(
        {
            stock["as_of"]
            for stock in stocks.values()
            if stock.get("availability") == "available" and stock.get("as_of")
        }
    )
    payload = {
        "schema_version": "1.0",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_date": "2026-07-24",
        "requested_data_as_of": requested_as_of.isoformat(),
        "as_of": available_as_of[-1] if available_as_of else None,
        "availability": (
            "available"
            if all(stock["availability"] == "available" for stock in stocks.values())
            else "partial"
            if any(stock["availability"] == "available" for stock in stocks.values())
            else "unavailable"
        ),
        "source": {
            "provider": "FinMind",
            "api_url": API_URL,
            "documentation_url": DOCS_URL,
            "institutional_dataset": "TaiwanStockInstitutionalInvestorsBuySell",
            "classification_dataset": "TaiwanStockInfo",
            "disposition_dataset": "TaiwanStockDispositionSecuritiesPeriod",
            "cache_policy": (
                "每個 API query 的 JSON response 均快取；預設重跑優先讀 cache。"
                "HTTP/API 401/402/429 與伺服器暫時錯誤使用 1/2/4/8 秒"
                "指數退避，共最多 5 次。"
            ),
        },
        "source_status": {
            "institutional": institutional_status,
            "stock_info": stock_info_status,
            "disposition": disposition_status,
            "full_cash_settlement": {
                "availability": "unavailable",
                "reason": "沒有可可靠驗證的指定資料源，未推論。",
            },
        },
        "classification_method": {
            "foreign": ["Foreign_Investor"],
            "investment_trust": ["Investment_Trust"],
            "dealer": ["Dealer", "Dealer_self", "Dealer_Hedging"],
            "total": "foreign + investment_trust + dealer",
            "supplementary_foreign_dealer_self": {
                "components": ["Foreign_Dealer_Self"],
                "included_in_total": False,
                "reason": "依 TWSE 口徑已計入證券自營商，不可重複加總。",
            },
            "unit": "shares；另提供除以 1,000 的 lots",
        },
        "quality_notes": [
            "分析 8039、2392、2201、6488、2481、6147，法人序列各保留最近 60 個可得交易日。",
            "未把 2026-07-24 盤中或盤前資訊混入法人買賣超；最新完整法人資料為實際 API 回傳末日。",
            "FinMind 法人日資料可能在盤後因鉅額／定價交易補登；本檔保存的是產生時 API 最新版本。",
            "處置資料為 FinMind 會員限制資料；若權限不足會明確標 unavailable，不以空資料代表未處置。",
            "全額交割狀態沒有可靠欄位，明確標 unavailable。",
        ],
        "stocks": stocks,
    }
    atomic_write_json(OUTPUT_PATH, payload)

    print(f"wrote {OUTPUT_PATH}")
    for stock_id, stock in stocks.items():
        if stock["availability"] != "available":
            print(f"{stock_id} {stock['stock_name']}: unavailable - {stock.get('reason')}")
            continue
        summaries = stock["summary"]
        print(
            f"{stock_id} {stock['stock_name']} as_of={stock['as_of']} "
            f"foreign_5d={summaries['foreign']['last_5d']['net_lots']:+.3f} lots "
            f"trust_5d={summaries['investment_trust']['last_5d']['net_lots']:+.3f} lots "
            f"dealer_5d={summaries['dealer']['last_5d']['net_lots']:+.3f} lots"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
