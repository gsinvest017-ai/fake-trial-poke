#!/usr/bin/env python3
"""計算六檔股票的 CMoney 式融資維持率。

資料來源只使用證交所／櫃買中心每日公開資料；每個市場、日期的六檔子集會
快取到 ``data/analysis/cache``。計算遇到未確認的資料缺口、餘額不連續或
餘額守恆失敗時，不會跨洞硬算，該檔會誠實標示為未取得。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ANALYSIS_DIR = Path(__file__).resolve().parent
CACHE_DIR = ANALYSIS_DIR / "cache"
OUTPUT_PATH = ANALYSIS_DIR / "margin_maint.json"
TAIPEI = ZoneInfo("Asia/Taipei")

TRADING_SESSIONS = 90
TREND_SESSIONS = 20
MAX_CALENDAR_DAYS = 180
FINANCING_RATIO = 0.60
REQUEST_DELAY_SECONDS = 0.45
CACHE_SCHEMA_VERSION = 2
READABLE_CACHE_SCHEMA_VERSIONS = {1, CACHE_SCHEMA_VERSION}

STOCKS: dict[str, dict[str, str]] = {
    "8039": {"name": "台虹", "market": "twse"},
    "2392": {"name": "正崴", "market": "twse"},
    "2201": {"name": "裕隆", "market": "twse"},
    "6488": {"name": "環球晶", "market": "tpex"},
    "2481": {"name": "強茂", "market": "twse"},
    "6147": {"name": "頎邦", "market": "tpex"},
}

MARKET_ENDPOINTS = {
    "twse": {
        "margin": "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN",
        "close": "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX",
    },
    "tpex": {
        "margin": "https://www.tpex.org.tw/www/zh-tw/margin/balance",
        "close": "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes",
    },
}


class FetchError(RuntimeError):
    """官方資料無法可靠取得或解析。"""


@dataclass
class RunStats:
    network_requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    encountered_http_401: bool = False
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DayResult:
    market: str
    date: str
    status: str
    stocks: dict[str, dict[str, Any]]
    source: str
    reason: str | None = None


class PoliteRequester:
    """在所有官方 HTTP 請求之間維持最小間隔並做有限重試。"""

    def __init__(self, delay_seconds: float, stats: RunStats) -> None:
        self.delay_seconds = max(0.0, delay_seconds)
        self.stats = stats
        self._last_request_at: float | None = None

    def _wait(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.delay_seconds - (time.monotonic() - self._last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def get_json(
        self,
        url: str,
        params: Mapping[str, str],
        max_attempts: int = 3,
    ) -> dict[str, Any]:
        query_url = f"{url}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(max_attempts):
            self._wait()
            try:
                request = Request(
                    query_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "fake-auction-margin-maint/1.0 (research; cached)",
                    },
                )
                self.stats.network_requests += 1
                with urlopen(request, timeout=30) as response:
                    raw = response.read()
                self._last_request_at = time.monotonic()
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise FetchError("官方回傳根節點不是 JSON object")
                return payload
            except HTTPError as exc:
                self._last_request_at = time.monotonic()
                if exc.code == 401:
                    self.stats.encountered_http_401 = True
                last_error = exc
            except (URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, FetchError) as exc:
                self._last_request_at = time.monotonic()
                last_error = exc
            if attempt < max_attempts - 1:
                self.stats.retries += 1
                time.sleep(1.0 + attempt)
        raise FetchError(f"官方資料請求失敗（重試 {max_attempts} 次）：{last_error}")


def now_iso() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def default_end_date() -> date:
    """21:00 前只採前一日，避免把尚未完整發布的盤後資料混入。"""

    now = datetime.now(TAIPEI)
    return now.date() if now.hour >= 21 else now.date() - timedelta(days=1)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def finite_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    cleaned = re.sub(r"<[^>]+>", "", str(value)).replace(",", "").strip()
    if cleaned in {"", "--", "-", "N/A"}:
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def integer_number(value: Any) -> int | None:
    number = finite_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def clean_field(value: Any) -> str:
    return re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", str(value)))


def field_index(fields: Iterable[Any], label: str, fallback: int | None = None) -> int | None:
    target = clean_field(label)
    for index, field_name in enumerate(fields):
        normalized = clean_field(field_name)
        if normalized == target or normalized.startswith(target):
            return index
    return fallback


def row_value(row: list[Any], index: int | None) -> Any:
    return row[index] if index is not None and index < len(row) else None


def is_twse_no_data(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("stat", "")).strip()
    return "沒有符合條件的資料" in status


def tpex_has_rows(payload: Mapping[str, Any]) -> bool:
    tables = payload.get("tables")
    return isinstance(tables, list) and any(
        isinstance(table, Mapping) and bool(table.get("data")) for table in tables
    )


def is_tpex_no_data(payload: Mapping[str, Any]) -> bool:
    status = str(payload.get("stat", "")).strip().lower()
    return (status and status != "ok") or (
        status == "ok" and isinstance(payload.get("tables"), list) and not tpex_has_rows(payload)
    )


def payload_date_matches(payload: Mapping[str, Any], day: date) -> bool:
    """確認官方回傳不是忽略查詢日期後送回其他交易日。"""

    digits = re.sub(r"\D", "", str(payload.get("date", "")))
    return digits == day.strftime("%Y%m%d")


def cache_path(cache_dir: Path, market: str, day: date) -> Path:
    return cache_dir / f"margin_maint_{market}_{day.isoformat()}.json"


def load_cached_day(path: Path, market: str, day: date) -> DayResult:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FetchError(f"快取無法讀取：{path.name}：{exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") not in READABLE_CACHE_SCHEMA_VERSIONS
        or payload.get("market") != market
        or payload.get("date") != day.isoformat()
        or payload.get("status") not in {"ok", "no_data"}
        or not isinstance(payload.get("stocks"), dict)
    ):
        raise FetchError(f"快取格式或日期不符：{path.name}")
    return DayResult(
        market=market,
        date=day.isoformat(),
        status=str(payload["status"]),
        stocks=payload["stocks"],
        source="cache",
        reason=payload.get("reason"),
    )


def parse_twse_day(
    day: date,
    margin_payload: Mapping[str, Any],
    close_payload: Mapping[str, Any],
    wanted: set[str],
) -> dict[str, dict[str, Any]]:
    if margin_payload.get("stat") != "OK" or close_payload.get("stat") != "OK":
        raise FetchError(f"TWSE {day}: stat 不是 OK")
    if not payload_date_matches(margin_payload, day) or not payload_date_matches(
        close_payload, day
    ):
        raise FetchError(f"TWSE {day}: 官方回傳日期與查詢日期不符")

    margin_table: Mapping[str, Any] | None = None
    for table in margin_payload.get("tables", []):
        if not isinstance(table, Mapping):
            continue
        fields = table.get("fields") or []
        if len(fields) >= 7 and clean_field(fields[2]) == "買進":
            margin_table = table
            break
    close_table: Mapping[str, Any] | None = None
    for table in close_payload.get("tables", []):
        if not isinstance(table, Mapping):
            continue
        fields = table.get("fields") or []
        if field_index(fields, "收盤價") is not None and field_index(fields, "證券代號") is not None:
            close_table = table
            break
    if margin_table is None or close_table is None:
        raise FetchError(f"TWSE {day}: 找不到融資或收盤資料表")

    margin_rows: dict[str, dict[str, Any]] = {}
    for raw in margin_table.get("data", []):
        if not isinstance(raw, list) or not raw:
            continue
        code = str(raw[0]).strip()
        if code not in wanted:
            continue
        values = [integer_number(row_value(raw, index)) for index in range(2, 7)]
        if any(value is None for value in values):
            raise FetchError(f"TWSE {day} {code}: 融資欄位缺漏")
        buy, sell, cash, previous, balance = values
        margin_rows[code] = {
            "stock_id": code,
            "name": str(raw[1]).strip() or STOCKS[code]["name"],
            "buy_lots": buy,
            "sell_lots": sell,
            "cash_repayment_lots": cash,
            "previous_balance_lots": previous,
            "balance_lots": balance,
        }

    fields = close_table.get("fields") or []
    close_index = field_index(fields, "收盤價", 8)
    closes: dict[str, float] = {}
    for raw in close_table.get("data", []):
        if not isinstance(raw, list) or not raw:
            continue
        code = str(raw[0]).strip()
        if code not in wanted:
            continue
        close = finite_number(row_value(raw, close_index))
        if close is not None and close > 0:
            closes[code] = close
    return combine_daily_rows(day, "twse", wanted, margin_rows, closes)


def parse_tpex_day(
    day: date,
    margin_payload: Mapping[str, Any],
    close_payload: Mapping[str, Any],
    wanted: set[str],
) -> dict[str, dict[str, Any]]:
    if str(margin_payload.get("stat", "")).strip().lower() != "ok":
        raise FetchError(f"TPEx {day}: 融資 stat 不是 ok")
    if str(close_payload.get("stat", "")).strip().lower() != "ok":
        raise FetchError(f"TPEx {day}: 收盤 stat 不是 ok")
    if not payload_date_matches(margin_payload, day) or not payload_date_matches(
        close_payload, day
    ):
        raise FetchError(f"TPEx {day}: 官方回傳日期與查詢日期不符")

    margin_table: Mapping[str, Any] | None = None
    for table in margin_payload.get("tables", []):
        if not isinstance(table, Mapping):
            continue
        fields = table.get("fields") or []
        if field_index(fields, "前資餘額") is not None and field_index(fields, "資餘額") is not None:
            margin_table = table
            break
    close_table: Mapping[str, Any] | None = None
    for table in close_payload.get("tables", []):
        if not isinstance(table, Mapping):
            continue
        fields = table.get("fields") or []
        if field_index(fields, "收盤") is not None and field_index(fields, "代號") is not None:
            close_table = table
            break
    if margin_table is None or close_table is None:
        raise FetchError(f"TPEx {day}: 找不到融資或收盤資料表")

    fields = margin_table.get("fields") or []
    indices = {
        "previous": field_index(fields, "前資餘額", 2),
        "buy": field_index(fields, "資買", 3),
        "sell": field_index(fields, "資賣", 4),
        "cash": field_index(fields, "現償", 5),
        "balance": field_index(fields, "資餘額", 6),
    }
    margin_rows: dict[str, dict[str, Any]] = {}
    for raw in margin_table.get("data", []):
        if not isinstance(raw, list) or not raw:
            continue
        code = str(raw[0]).strip()
        if code not in wanted:
            continue
        parsed = {key: integer_number(row_value(raw, index)) for key, index in indices.items()}
        if any(value is None for value in parsed.values()):
            raise FetchError(f"TPEx {day} {code}: 融資欄位缺漏")
        margin_rows[code] = {
            "stock_id": code,
            "name": str(raw[1]).strip() or STOCKS[code]["name"],
            "buy_lots": parsed["buy"],
            "sell_lots": parsed["sell"],
            "cash_repayment_lots": parsed["cash"],
            "previous_balance_lots": parsed["previous"],
            "balance_lots": parsed["balance"],
        }

    close_fields = close_table.get("fields") or []
    close_index = field_index(close_fields, "收盤", 2)
    closes: dict[str, float] = {}
    for raw in close_table.get("data", []):
        if not isinstance(raw, list) or not raw:
            continue
        code = str(raw[0]).strip()
        if code not in wanted:
            continue
        close = finite_number(row_value(raw, close_index))
        if close is not None and close > 0:
            closes[code] = close
    return combine_daily_rows(day, "tpex", wanted, margin_rows, closes)


def combine_daily_rows(
    day: date,
    market: str,
    wanted: set[str],
    margin_rows: dict[str, dict[str, Any]],
    closes: dict[str, float],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for code in sorted(wanted):
        margin = margin_rows.get(code)
        close = closes.get(code)
        if margin is None:
            continue
        flow_balance = (
            margin["previous_balance_lots"]
            + margin["buy_lots"]
            - margin["sell_lots"]
            - margin["cash_repayment_lots"]
        )
        result[code] = {
            **margin,
            "market": market,
            "date": day.isoformat(),
            "close": close,
            "flow_identity_ok": flow_balance == margin["balance_lots"],
        }
    return result


def fetch_day(
    market: str,
    day: date,
    wanted: set[str],
    cache_dir: Path,
    requester: PoliteRequester,
    stats: RunStats,
    refresh: bool,
) -> DayResult:
    path = cache_path(cache_dir, market, day)
    if path.exists() and not refresh:
        result = load_cached_day(path, market, day)
        # Schema v1 only retained rows that also had a printable close.  A
        # suspended stock can still have an official zero-buy margin row, which
        # is enough to carry the cost safely.  Re-fetch only such incomplete old
        # cache days; complete v1 days remain reusable.
        if result.status != "ok" or wanted.issubset(result.stocks):
            stats.cache_hits += 1
            return result

    endpoints = MARKET_ENDPOINTS[market]
    if market == "twse":
        margin_params = {
            "date": day.strftime("%Y%m%d"),
            "selectType": "ALL",
            "response": "json",
        }
        close_params = {
            "date": day.strftime("%Y%m%d"),
            "type": "ALLBUT0999",
            "response": "json",
        }
    else:
        margin_params = close_params = {
            "date": day.strftime("%Y/%m/%d"),
            "response": "json",
        }

    margin_payload = requester.get_json(endpoints["margin"], margin_params)
    close_payload = requester.get_json(endpoints["close"], close_params)
    if market == "twse":
        margin_no_data = is_twse_no_data(margin_payload)
        close_no_data = is_twse_no_data(close_payload)
    else:
        margin_no_data = is_tpex_no_data(margin_payload)
        close_no_data = is_tpex_no_data(close_payload)

    if margin_no_data and close_no_data:
        status, stocks, reason = "no_data", {}, "兩個官方端點均明確回覆無資料"
    elif margin_no_data != close_no_data:
        raise FetchError(f"{market} {day}: 融資與收盤端點的有無資料狀態不一致")
    else:
        parser = parse_twse_day if market == "twse" else parse_tpex_day
        stocks = parser(day, margin_payload, close_payload, wanted)
        if not stocks:
            raise FetchError(f"{market} {day}: 目標股票沒有可配對的融資與收盤資料")
        status, reason = "ok", None

    atomic_write_json(
        path,
        {
            "schema_version": CACHE_SCHEMA_VERSION,
            "market": market,
            "date": day.isoformat(),
            "status": status,
            "fetched_at": now_iso(),
            "source": "TWSE" if market == "twse" else "TPEx",
            "stocks": stocks,
            "reason": reason,
        },
    )
    return DayResult(
        market=market,
        date=day.isoformat(),
        status=status,
        stocks=stocks,
        source="network",
        reason=reason,
    )


def collect_market_history(
    market: str,
    end_date: date,
    sessions: int,
    max_calendar_days: int,
    cache_dir: Path,
    requester: PoliteRequester,
    stats: RunStats,
    refresh: bool,
) -> tuple[list[DayResult], list[str]]:
    wanted = {code for code, meta in STOCKS.items() if meta["market"] == market}
    results: list[DayResult] = []
    unresolved_dates: list[str] = []
    for offset in range(max_calendar_days):
        day = end_date - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            result = fetch_day(
                market, day, wanted, cache_dir, requester, stats, refresh
            )
        except FetchError as exc:
            message = str(exc)
            stats.errors.append(message)
            unresolved_dates.append(day.isoformat())
            print(f"WARNING {message}", flush=True)
            continue
        if result.status == "ok":
            results.append(result)
            if len(results) % 10 == 0:
                print(
                    f"{market.upper()}: 已取得 {len(results)}/{sessions} 個交易日 "
                    f"（目前回溯至 {day.isoformat()}）",
                    flush=True,
                )
            if len(results) >= sessions:
                break
    results.sort(key=lambda item: item.date)
    if len(results) < sessions:
        stats.errors.append(
            f"{market}: {max_calendar_days} 個日曆日內僅取得 "
            f"{len(results)}/{sessions} 個交易日"
        )
    return results, unresolved_dates


def roll_cost(
    previous_cost: float | None,
    close: float,
    buy: int,
    sell: int,
    cash_repayment: int,
    previous_balance: int,
    balance: int,
) -> float:
    """依參考專案公式遞迴融資成本。"""

    if balance == 0:
        return 0.0
    if previous_balance == 0:
        return close
    if previous_cost is None:
        raise ValueError("前一日融資成本不可用，但前一日融資餘額大於零")
    return (
        previous_cost * (previous_balance - sell - cash_repayment) + close * buy
    ) / balance


def maintenance_rate(close: float | None, cost: float | None) -> float | None:
    if close is None or cost in (None, 0):
        return None
    return close / (cost * FINANCING_RATIO) * 100.0


def risk_level(rate: float | None) -> str:
    if rate is None:
        return "未取得"
    if rate < 130:
        return "追繳"
    if rate < 150:
        return "警戒"
    if rate <= 200:
        return "正常"
    return "安全"


def rounded(value: float | None, digits: int = 10) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def linear_slope(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    x_mean = (len(values) - 1) / 2
    y_mean = sum(values) / len(values)
    denominator = sum((index - x_mean) ** 2 for index in range(len(values)))
    if denominator == 0:
        return None
    return sum(
        (index - x_mean) * (value - y_mean) for index, value in enumerate(values)
    ) / denominator


def trend_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [
        row for row in series if isinstance(row.get("maintenance_rate_pct"), (int, float))
    ][-TREND_SESSIONS:]
    if len(usable) < 2:
        return {
            "availability": "unavailable",
            "reason": "可用維持率不足 2 筆",
            "sample_count": len(usable),
        }
    values = [float(row["maintenance_rate_pct"]) for row in usable]
    change = values[-1] - values[0]
    slope = linear_slope(values)
    if slope is None:
        direction = "持平"
    elif slope > 0.05:
        direction = "上升"
    elif slope < -0.05:
        direction = "下降"
    else:
        direction = "持平"
    return {
        "availability": "available",
        "sample_count": len(usable),
        "start_date": usable[0]["date"],
        "end_date": usable[-1]["date"],
        "start_rate_pct": rounded(values[0]),
        "end_rate_pct": rounded(values[-1]),
        "change_pct_points": rounded(change),
        "slope_pct_points_per_session": rounded(slope),
        "minimum_rate_pct": rounded(min(values)),
        "maximum_rate_pct": rounded(max(values)),
        "direction": direction,
    }


def unavailable_stock(
    code: str,
    reason: str,
    raw_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "stock_id": code,
        "name": STOCKS[code]["name"],
        "market": STOCKS[code]["market"],
        "availability": "unavailable",
        "reason": reason,
        "as_of": raw_rows[-1]["date"] if raw_rows else None,
        "current": {
            "maintenance_rate_pct": None,
            "buffer_to_call_pct_points": None,
            "risk_level": "未取得",
        },
        "trend_20d": {
            "availability": "unavailable",
            "reason": reason,
            "sample_count": 0,
        },
        "series": raw_rows,
    }


def build_stock_result(
    code: str,
    market_days: list[DayResult],
    unresolved_dates: list[str],
    required_sessions: int,
) -> dict[str, Any]:
    raw_rows = [day.stocks[code] for day in market_days if code in day.stocks]
    missing_stock_dates = [day.date for day in market_days if code not in day.stocks]
    if missing_stock_dates:
        return unavailable_stock(
            code,
            "官方交易日缺少股票融資資料：" + "、".join(missing_stock_dates),
            raw_rows,
        )
    if len(raw_rows) < required_sessions:
        return unavailable_stock(
            code,
            f"可用交易日不足：{len(raw_rows)}/{required_sessions}",
            raw_rows,
        )

    first_day = raw_rows[0]["date"]
    last_day = raw_rows[-1]["date"]
    unresolved_in_window = sorted(
        day for day in unresolved_dates if first_day <= day <= last_day
    )
    if unresolved_in_window:
        return unavailable_stock(
            code,
            "計算窗內有未確認資料缺口：" + "、".join(unresolved_in_window),
            raw_rows,
        )

    series: list[dict[str, Any]] = []
    previous_cost: float | None = None
    previous_balance: int | None = None
    chain_error: str | None = None
    for index, raw in enumerate(raw_rows):
        close_value = raw.get("close")
        close = float(close_value) if close_value is not None else None
        current_previous_balance = int(raw["previous_balance_lots"])
        balance = int(raw["balance_lots"])
        continuity_ok = (
            previous_balance is None or previous_balance == current_previous_balance
        )
        if raw.get("flow_identity_ok") is not True:
            chain_error = f"{raw['date']} 官方融資餘額不符合前餘額＋買進－賣出－現償"

        cost: float | None
        if chain_error is not None:
            cost = None
        elif index == 0:
            # 90 日窗的基準化：把最早完整日的既有融資部位成本設為當日收盤。
            # 後續每一日均嚴格走 CMoney roll_cost，不以未觀測資料猜測。
            if balance == 0:
                cost = 0.0
            elif close is None:
                chain_error = f"{raw['date']} 基準日有融資餘額但無有效收盤價"
                cost = None
            else:
                cost = close
        else:
            try:
                if close is None and int(raw["buy_lots"]) > 0 and balance > 0:
                    raise ValueError("無有效收盤價但有融資買進，無法定價新增部位")
                cost = roll_cost(
                    previous_cost,
                    close if close is not None else 0.0,
                    int(raw["buy_lots"]),
                    int(raw["sell_lots"]),
                    int(raw["cash_repayment_lots"]),
                    current_previous_balance,
                    balance,
                )
            except ValueError as exc:
                chain_error = f"{raw['date']} {exc}"
                cost = None

        rate = maintenance_rate(close, cost)
        series.append(
            {
                "date": raw["date"],
                "close": rounded(close),
                "buy_lots": int(raw["buy_lots"]),
                "sell_lots": int(raw["sell_lots"]),
                "cash_repayment_lots": int(raw["cash_repayment_lots"]),
                "previous_balance_lots": current_previous_balance,
                "balance_lots": balance,
                "flow_identity_ok": raw.get("flow_identity_ok") is True,
                "balance_continuity_ok": continuity_ok,
                "financing_cost": rounded(cost),
                "maintenance_rate_pct": rounded(rate),
                "buffer_to_call_pct_points": rounded(rate - 130 if rate is not None else None),
                "risk_level": risk_level(rate),
            }
        )
        previous_cost = cost
        previous_balance = balance

    if chain_error is not None:
        return unavailable_stock(code, chain_error, series)
    latest = series[-1]
    return {
        "stock_id": code,
        "name": raw_rows[-1].get("name") or STOCKS[code]["name"],
        "market": STOCKS[code]["market"],
        "availability": "available",
        "reason": None,
        "as_of": latest["date"],
        "current": {
            "close": latest["close"],
            "financing_cost": latest["financing_cost"],
            "maintenance_rate_pct": latest["maintenance_rate_pct"],
            "buffer_to_call_pct_points": latest["buffer_to_call_pct_points"],
            "risk_level": latest["risk_level"],
            "balance_lots": latest["balance_lots"],
        },
        "trend_20d": trend_summary(series),
        "quality": {
            "requested_trading_sessions": required_sessions,
            "available_trading_sessions": len(series),
            "seed_date": series[0]["date"],
            "seed_close": series[0]["close"],
            "seed_method": "最早完整交易日收盤基準化；其後逐日 CMoney 式遞迴",
            "flow_identity_failures": sum(
                row["flow_identity_ok"] is not True for row in series
            ),
            "balance_continuity_failures": sum(
                row["balance_continuity_ok"] is not True for row in series[1:]
            ),
            "balance_continuity_policy": (
                "前一筆今日餘額與次筆官方前日餘額不一致時列為重編提示；"
                "計算仍依當日官方前日餘額，不把可驗證的當日守恆列誤判為缺口"
            ),
            "no_close_cost_carries": sum(
                row["close"] is None
                and row["buy_lots"] == 0
                and row["financing_cost"] is not None
                for row in series
            ),
        },
        "series": series,
    }


def build_output(
    end_date: date,
    sessions: int,
    max_calendar_days: int,
    cache_dir: Path,
    refresh: bool,
    request_delay: float,
) -> dict[str, Any]:
    stats = RunStats()
    requester = PoliteRequester(request_delay, stats)
    market_histories: dict[str, list[DayResult]] = {}
    unresolved: dict[str, list[str]] = {}
    for market in ("twse", "tpex"):
        history, gaps = collect_market_history(
            market=market,
            end_date=end_date,
            sessions=sessions,
            max_calendar_days=max_calendar_days,
            cache_dir=cache_dir,
            requester=requester,
            stats=stats,
            refresh=refresh,
        )
        market_histories[market] = history
        unresolved[market] = gaps

    stocks = {
        code: build_stock_result(
            code,
            market_histories[meta["market"]],
            unresolved[meta["market"]],
            sessions,
        )
        for code, meta in STOCKS.items()
    }
    available_as_of = [
        item["as_of"]
        for item in stocks.values()
        if item.get("availability") == "available" and item.get("as_of")
    ]
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "as_of": min(available_as_of) if available_as_of else None,
        "availability": (
            "available"
            if all(item.get("availability") == "available" for item in stocks.values())
            else "partial"
        ),
        "methodology": {
            "formula": "收盤價 / (融資成本 × 0.60) × 100",
            "financing_ratio": FINANCING_RATIO,
            "cost_recursion": (
                "(前成本×(前餘額－賣出－現償)＋當日收盤×買進)／當日餘額；"
                "餘額為0時成本為0，前餘額為0時成本為當日收盤"
            ),
            "window_seed": (
                "最早完整交易日的既有融資部位以當日收盤基準化；"
                "其後使用約90交易日真實買進、賣出、現償與餘額逐日遞迴"
            ),
            "risk_bands": [
                {"minimum_inclusive": None, "maximum_exclusive": 130, "label": "追繳"},
                {"minimum_inclusive": 130, "maximum_exclusive": 150, "label": "警戒"},
                {"minimum_inclusive": 150, "maximum_inclusive": 200, "label": "正常"},
                {"minimum_exclusive": 200, "maximum_inclusive": None, "label": "安全"},
            ],
            "sources": {
                "twse": "臺灣證券交易所每日融資融券 MI_MARGN＋每日收盤 MI_INDEX",
                "tpex": "證券櫃檯買賣中心融資融券餘額＋每日收盤行情",
            },
            "missing_data_policy": (
                "休市日須由融資與收盤兩端點同時確認無資料才跳過；"
                "未確認缺口、餘額不連續或守恆失敗不跨洞計算"
            ),
            "trading_sessions_requested": sessions,
            "requested_end_date": end_date.isoformat(),
        },
        "run_diagnostics": {
            "network_requests": stats.network_requests,
            "cache_hits": stats.cache_hits,
            "retries": stats.retries,
            "encountered_http_401": stats.encountered_http_401,
            "error_count": len(stats.errors),
            "errors": stats.errors,
            "unresolved_dates": unresolved,
        },
        "stocks": stocks,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--end-date",
        type=date.fromisoformat,
        default=default_end_date(),
        help="最後候選日期（YYYY-MM-DD）；預設依臺北 21:00 盤後界線判定",
    )
    parser.add_argument("--sessions", type=int, default=TRADING_SESSIONS)
    parser.add_argument("--max-calendar-days", type=int, default=MAX_CALENDAR_DAYS)
    parser.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    parser.add_argument("--refresh", action="store_true", help="忽略既有成功／休市快取")
    parser.add_argument(
        "--request-delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help="官方 HTTP 請求的最小間隔秒數",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.sessions < TREND_SESSIONS:
        raise SystemExit(f"--sessions 不可少於 {TREND_SESSIONS}")
    if args.max_calendar_days < args.sessions:
        raise SystemExit("--max-calendar-days 不可少於 --sessions")
    output = build_output(
        end_date=args.end_date,
        sessions=args.sessions,
        max_calendar_days=args.max_calendar_days,
        cache_dir=args.cache_dir,
        refresh=args.refresh,
        request_delay=args.request_delay,
    )
    atomic_write_json(args.output, output)
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")
    for code, item in output["stocks"].items():
        current = item.get("current", {})
        print(
            f"{code} {item['name']}: "
            f"{current.get('maintenance_rate_pct')}% "
            f"{current.get('risk_level')} "
            f"(as_of={item.get('as_of')}, availability={item.get('availability')})"
        )
    diagnostics = output["run_diagnostics"]
    print(
        "diagnostics:",
        f"network={diagnostics['network_requests']}",
        f"cache={diagnostics['cache_hits']}",
        f"retries={diagnostics['retries']}",
        f"http401={diagnostics['encountered_http_401']}",
        f"errors={diagnostics['error_count']}",
    )
    return 0 if output["availability"] == "available" else 2


if __name__ == "__main__":
    raise SystemExit(main())
