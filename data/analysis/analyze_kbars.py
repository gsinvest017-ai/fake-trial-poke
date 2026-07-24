#!/usr/bin/env python3
"""建立三檔重點股與 TAIEX 的還原日 K、均線及量價摘要。

資料紀律：
1. 個股優先使用 FinMind TaiwanStockPriceAdj。
2. 若該會員資料表不可用，使用 FinMind TaiwanStockPrice 原始 OHLCV，逐日乘上
   Yahoo Finance 同日 adjclose / close 的調整因子；不以未還原價格計算均線。
3. TAIEX 使用 FinMind TaiwanStockPrice(data_id=TAIEX)。指數不是公司證券，
   不適用個股除權息調整。
4. 所有網路 response 都先寫進 data/analysis/cache，再由快取重跑。
5. 分析硬截止 2026-07-23，排除 2026-07-24 未完成日 K。

只依賴 Python 標準函式庫，可獨立執行：
    python data/analysis/analyze_kbars.py
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ANALYSIS_DIR = Path(__file__).resolve().parent
PROJECT_DIR = ANALYSIS_DIR.parent.parent
CACHE_DIR = ANALYSIS_DIR / "cache"
OUTPUT_PATH = ANALYSIS_DIR / "kbars.json"
ENV_PATH = PROJECT_DIR / ".env"

START_DATE = date(2025, 5, 1)
CUTOFF_DATE = date(2026, 7, 23)
TAIPEI = ZoneInfo("Asia/Taipei")

STOCKS = {
    "8039": {"name": "台虹", "yahoo_symbol": "8039.TW"},
    "2392": {"name": "正崴", "yahoo_symbol": "2392.TW"},
    "2201": {"name": "裕隆", "yahoo_symbol": "2201.TW"},
}
MA_WINDOWS = (5, 20, 60, 120, 240)
MINIMUM_REQUIRED_ROWS = max(MA_WINDOWS)


@dataclass
class FetchResult:
    ok: bool
    payload: dict[str, Any] | None
    source: str
    cache_file: str
    status_code: int | None
    from_cache: bool
    error: str | None
    fetched_at: str | None


class DataQualityError(RuntimeError):
    """資料品質檢查未通過。"""


def now_iso() -> str:
    return datetime.now(TAIPEI).isoformat(timespec="seconds")


def safe_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def safe_int(value: Any) -> int | None:
    number = safe_float(value)
    if number is None:
        return None
    return int(round(number))


def rounded(value: float | None, digits: int = 4) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    temporary.replace(path)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_dotenv_values(path: Path) -> dict[str, str]:
    """只載入鍵值，不輸出任何內容；支援目前專案的簡單 .env 格式。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
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
        if key:
            values[key] = value
    return values


def sanitize_error(message: str, limit: int = 500) -> str:
    collapsed = " ".join(message.split())
    return collapsed[:limit]


def cached_get_json(
    *,
    source: str,
    cache_key: str,
    url: str,
    headers: dict[str, str] | None = None,
    refresh: bool = False,
    max_attempts: int = 5,
) -> FetchResult:
    """GET JSON 並快取 response；HTTP/業務狀態 402 採指數退避。"""
    cache_path = CACHE_DIR / f"kbars__{cache_key}.json"
    if cache_path.exists() and not refresh:
        try:
            envelope = read_json(cache_path)
            status_code = safe_int(envelope.get("status_code"))
            business_status = safe_int(envelope.get("business_status"))
            # 402 不視為可重用快取，讓重跑有機會恢復。
            if status_code != 402 and business_status != 402:
                return FetchResult(
                    ok=bool(envelope.get("ok")),
                    payload=envelope.get("response_json"),
                    source=source,
                    cache_file=str(cache_path.relative_to(PROJECT_DIR)),
                    status_code=status_code,
                    from_cache=True,
                    error=envelope.get("error"),
                    fetched_at=envelope.get("fetched_at"),
                )
        except (OSError, ValueError, TypeError):
            # 損壞快取不採用，重新抓取並覆蓋。
            pass

    request_headers = {
        "Accept": "application/json",
        "User-Agent": "auction-analysis/1.0 (+offline research report)",
    }
    if headers:
        request_headers.update(headers)

    last_result: FetchResult | None = None
    for attempt in range(1, max_attempts + 1):
        response_json: dict[str, Any] | None = None
        status_code: int | None = None
        business_status: int | None = None
        error: str | None = None
        response_text: str | None = None
        fetched_at = now_iso()
        try:
            request = Request(url, headers=request_headers, method="GET")
            with urlopen(request, timeout=45) as response:
                status_code = int(response.status)
                raw = response.read()
            response_text = raw.decode("utf-8", errors="replace")
            parsed = json.loads(response_text)
            if not isinstance(parsed, dict):
                raise ValueError("response JSON root is not an object")
            response_json = parsed
            business_status = safe_int(parsed.get("status"))
            if status_code >= 400:
                error = f"HTTP {status_code}"
            elif business_status == 402:
                error = sanitize_error(str(parsed.get("msg") or "FinMind status 402"))
            else:
                error = None
        except HTTPError as exc:
            status_code = int(exc.code)
            try:
                raw = exc.read()
                response_text = raw.decode("utf-8", errors="replace")
                if response_text.strip():
                    parsed = json.loads(response_text)
                    if isinstance(parsed, dict):
                        response_json = parsed
                        business_status = safe_int(parsed.get("status"))
            except (OSError, UnicodeError, ValueError, TypeError):
                pass
            detail = ""
            if response_json:
                detail = str(response_json.get("msg") or response_json.get("detail") or "")
            error = sanitize_error(f"HTTP {status_code}: {detail or exc.reason}")
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            error = sanitize_error(f"{type(exc).__name__}: {exc}")

        retry_402 = status_code == 402 or business_status == 402
        ok = error is None and isinstance(response_json, dict)
        envelope = {
            "schema_version": 1,
            "source": source,
            "request_url": url,
            "fetched_at": fetched_at,
            "attempt": attempt,
            "status_code": status_code,
            "business_status": business_status,
            "ok": ok,
            "error": error,
            "response_json": response_json,
            # 非 JSON 錯誤本文只保留短片段，避免產生巨大診斷檔。
            "response_text_excerpt": (
                response_text[:1000] if response_json is None and response_text else None
            ),
        }
        atomic_write_json(cache_path, envelope)
        last_result = FetchResult(
            ok=ok,
            payload=response_json,
            source=source,
            cache_file=str(cache_path.relative_to(PROJECT_DIR)),
            status_code=status_code,
            from_cache=False,
            error=error,
            fetched_at=fetched_at,
        )
        if ok or not retry_402 or attempt >= max_attempts:
            return last_result
        time.sleep(min(2 ** (attempt - 1), 16))

    assert last_result is not None
    return last_result


def finmind_get(
    dataset: str,
    data_id: str,
    *,
    token: str,
    refresh: bool,
) -> FetchResult:
    params = {
        "dataset": dataset,
        "data_id": data_id,
        "start_date": START_DATE.isoformat(),
        "end_date": CUTOFF_DATE.isoformat(),
    }
    url = "https://api.finmindtrade.com/api/v4/data?" + urlencode(params)
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    key = (
        f"finmind__{dataset}__{data_id}__"
        f"{START_DATE.isoformat()}__{CUTOFF_DATE.isoformat()}"
    )
    return cached_get_json(
        source=f"FinMind {dataset}",
        cache_key=key,
        url=url,
        headers=headers,
        refresh=refresh,
    )


def yahoo_get(symbol: str, *, refresh: bool) -> FetchResult:
    start_dt = datetime.combine(START_DATE, datetime_time.min, tzinfo=TAIPEI)
    # Yahoo period2 為 exclusive；多取一天但後續仍以 cutoff 硬過濾。
    end_dt = datetime.combine(
        CUTOFF_DATE + timedelta(days=1), datetime_time.min, tzinfo=TAIPEI
    )
    params = {
        "period1": int(start_dt.timestamp()),
        "period2": int(end_dt.timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "includeAdjustedClose": "true",
    }
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?" + urlencode(
        params
    )
    key = (
        f"yahoo__chart__{symbol.replace('^', 'INDEX_')}__"
        f"{START_DATE.isoformat()}__{CUTOFF_DATE.isoformat()}"
    )
    return cached_get_json(
        source=f"Yahoo Finance chart {symbol}",
        cache_key=key,
        url=url,
        refresh=refresh,
    )


def source_attempt(result: FetchResult) -> dict[str, Any]:
    return {
        "source": result.source,
        "available": result.ok,
        "status_code": result.status_code,
        "from_cache": result.from_cache,
        "cache_file": result.cache_file,
        "fetched_at": result.fetched_at,
        "error": result.error,
    }


def finmind_rows(result: FetchResult) -> list[dict[str, Any]]:
    if not result.ok or not result.payload:
        return []
    rows = result.payload.get("data")
    return rows if isinstance(rows, list) else []


def normalize_finmind_price_rows(
    rows: Iterable[dict[str, Any]], expected_id: str
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_id = str(row.get("stock_id", ""))
        if row_id and row_id != expected_id:
            continue
        try:
            row_date = date.fromisoformat(str(row.get("date")))
        except ValueError:
            continue
        if row_date > CUTOFF_DATE:
            continue
        open_price = safe_float(row.get("open"))
        high = safe_float(row.get("max", row.get("high")))
        low = safe_float(row.get("min", row.get("low")))
        close = safe_float(row.get("close"))
        volume = safe_int(row.get("Trading_Volume", row.get("trading_volume")))
        normalized.append(
            {
                "date": row_date.isoformat(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
            }
        )
    return normalized


def drop_zero_ohlcv_placeholders(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """排除 FinMind 以全零 OHLCV 表示的無成交／停牌占位列。

    只有 open/high/low/close/volume 全部為零才排除；任何部分異常仍交由
    validate_rows 拒收，避免把真實資料問題靜默清掉。
    """
    kept: list[dict[str, Any]] = []
    dropped_dates: list[str] = []
    for row in rows:
        values = (
            safe_float(row.get("open")),
            safe_float(row.get("high")),
            safe_float(row.get("low")),
            safe_float(row.get("close")),
            safe_float(row.get("volume")),
        )
        if all(value == 0 for value in values):
            dropped_dates.append(str(row.get("date")))
        else:
            kept.append(row)
    return kept, dropped_dates


def yahoo_adjustment_factors(
    result: FetchResult,
) -> tuple[dict[str, float], dict[str, Any]]:
    factors: dict[str, float] = {}
    details: dict[str, Any] = {
        "rows_seen": 0,
        "valid_factors": 0,
        "min_factor": None,
        "max_factor": None,
    }
    if not result.ok or not result.payload:
        return factors, details
    chart = result.payload.get("chart")
    if not isinstance(chart, dict):
        return factors, details
    results = chart.get("result")
    if not isinstance(results, list) or not results:
        return factors, details
    chart_result = results[0]
    if not isinstance(chart_result, dict):
        return factors, details
    timestamps = chart_result.get("timestamp")
    indicators = chart_result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        return factors, details
    quotes = indicators.get("quote")
    adjusted = indicators.get("adjclose")
    if (
        not isinstance(quotes, list)
        or not quotes
        or not isinstance(quotes[0], dict)
        or not isinstance(adjusted, list)
        or not adjusted
        or not isinstance(adjusted[0], dict)
    ):
        return factors, details
    closes = quotes[0].get("close")
    adj_closes = adjusted[0].get("adjclose")
    if not isinstance(closes, list) or not isinstance(adj_closes, list):
        return factors, details
    count = min(len(timestamps), len(closes), len(adj_closes))
    details["rows_seen"] = count
    for index in range(count):
        ts = safe_int(timestamps[index])
        close = safe_float(closes[index])
        adj_close = safe_float(adj_closes[index])
        if ts is None or close is None or adj_close is None or close <= 0:
            continue
        local_date = datetime.fromtimestamp(ts, timezone.utc).astimezone(TAIPEI).date()
        if local_date > CUTOFF_DATE:
            continue
        factor = adj_close / close
        if factor <= 0 or not math.isfinite(factor):
            continue
        factors[local_date.isoformat()] = factor
    factor_values = list(factors.values())
    details["valid_factors"] = len(factor_values)
    details["min_factor"] = rounded(min(factor_values), 8) if factor_values else None
    details["max_factor"] = rounded(max(factor_values), 8) if factor_values else None
    return factors, details


def quality_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    dates = [str(row.get("date")) for row in rows]
    duplicate_count = len(dates) - len(set(dates))
    invalid_close_count = sum(
        1
        for row in rows
        if safe_float(row.get("close")) is None
        or (safe_float(row.get("close")) or 0) <= 0
    )
    monotonic = all(dates[index] < dates[index + 1] for index in range(len(dates) - 1))
    invalid_ohlc_count = 0
    invalid_volume_count = 0
    for row in rows:
        open_price = safe_float(row.get("open"))
        high = safe_float(row.get("high"))
        low = safe_float(row.get("low"))
        close = safe_float(row.get("close"))
        volume = safe_int(row.get("volume"))
        if (
            open_price is None
            or high is None
            or low is None
            or close is None
            or min(open_price, high, low, close) <= 0
            or high < max(open_price, close, low)
            or low > min(open_price, close, high)
        ):
            invalid_ohlc_count += 1
        if volume is None or volume < 0:
            invalid_volume_count += 1
    return {
        "row_count": len(rows),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "strictly_monotonic_dates": monotonic,
        "duplicate_date_count": duplicate_count,
        "invalid_close_count": invalid_close_count,
        "invalid_ohlc_count": invalid_ohlc_count,
        "invalid_volume_count": invalid_volume_count,
        "cutoff_enforced": all(
            date.fromisoformat(value) <= CUTOFF_DATE for value in dates
        ),
        "has_ma240_history": len(rows) >= MINIMUM_REQUIRED_ROWS,
    }


def validate_rows(rows: list[dict[str, Any]], label: str) -> dict[str, Any]:
    rows.sort(key=lambda row: str(row.get("date")))
    quality = quality_snapshot(rows)
    failures: list[str] = []
    if quality["row_count"] == 0:
        failures.append("no rows")
    if not quality["strictly_monotonic_dates"]:
        failures.append("dates are not strictly monotonic")
    if quality["duplicate_date_count"]:
        failures.append(f"{quality['duplicate_date_count']} duplicate dates")
    if quality["invalid_close_count"]:
        failures.append(f"{quality['invalid_close_count']} invalid closes")
    if quality["invalid_ohlc_count"]:
        failures.append(f"{quality['invalid_ohlc_count']} invalid OHLC rows")
    if quality["invalid_volume_count"]:
        failures.append(f"{quality['invalid_volume_count']} invalid volumes")
    if not quality["cutoff_enforced"]:
        failures.append("rows beyond analysis cutoff")
    if failures:
        raise DataQualityError(f"{label}: " + "; ".join(failures))
    return quality


def apply_adjustment(
    raw_rows: list[dict[str, Any]], factors: dict[str, float]
) -> tuple[list[dict[str, Any]], list[str]]:
    adjusted: list[dict[str, Any]] = []
    missing_dates: list[str] = []
    for row in raw_rows:
        factor = factors.get(row["date"])
        if factor is None:
            missing_dates.append(row["date"])
            continue
        adjusted.append(
            {
                "date": row["date"],
                "open": (safe_float(row["open"]) or 0) * factor,
                "high": (safe_float(row["high"]) or 0) * factor,
                "low": (safe_float(row["low"]) or 0) * factor,
                "close": (safe_float(row["close"]) or 0) * factor,
                "volume": row["volume"],
                "raw_close": row["close"],
                "adj_factor": factor,
            }
        )
    return adjusted, missing_dates


def interpolate_stable_factor_gaps(
    raw_rows: list[dict[str, Any]],
    factors: dict[str, float],
    *,
    relative_tolerance: float = 1e-5,
) -> list[str]:
    """僅在前後因子相同（容許報價浮點誤差）時補 Yahoo 單日空洞。

    這不跨越除權息因子跳變：若前後因子差異超過 relative_tolerance，
    該日維持缺漏，後續會直接排除而不是猜測。
    """
    dates = [row["date"] for row in raw_rows]
    inferred_dates: list[str] = []
    for index, row_date in enumerate(dates):
        if row_date in factors:
            continue
        previous_factor: float | None = None
        next_factor: float | None = None
        for previous_index in range(index - 1, -1, -1):
            previous_factor = factors.get(dates[previous_index])
            if previous_factor is not None:
                break
        for next_index in range(index + 1, len(dates)):
            next_factor = factors.get(dates[next_index])
            if next_factor is not None:
                break
        if previous_factor is None or next_factor is None:
            continue
        denominator = max(abs(previous_factor), abs(next_factor), 1e-12)
        relative_difference = abs(previous_factor - next_factor) / denominator
        if relative_difference <= relative_tolerance:
            factors[row_date] = (previous_factor + next_factor) / 2.0
            inferred_dates.append(row_date)
    return inferred_dates


def rolling_average(values: list[float], window: int) -> list[float | None]:
    output: list[float | None] = [None] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= window:
            running -= values[index - window]
        if index >= window - 1:
            output[index] = running / window
    return output


def pct_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return (current / previous - 1.0) * 100.0


def add_indicators(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes = [float(row["close"]) for row in rows]
    volume_values = [float(row["volume"]) for row in rows]
    ma_series = {window: rolling_average(closes, window) for window in MA_WINDOWS}
    volume_ma20 = rolling_average(volume_values, 20)
    series: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        item = {
            "date": row["date"],
            "open": rounded(safe_float(row["open"]), 4),
            "high": rounded(safe_float(row["high"]), 4),
            "low": rounded(safe_float(row["low"]), 4),
            "close": rounded(safe_float(row["close"]), 4),
            "volume_shares": safe_int(row["volume"]),
            "volume_lots": rounded((safe_int(row["volume"]) or 0) / 1000.0, 3),
            "volume_ma20_shares": rounded(volume_ma20[index], 2),
            "raw_close": rounded(safe_float(row.get("raw_close")), 4),
            "adj_factor": rounded(safe_float(row.get("adj_factor")), 8),
        }
        for window in MA_WINDOWS:
            item[f"ma{window}"] = rounded(ma_series[window][index], 4)
        series.append(item)
    return series


def classify_trend(
    latest_close: float, mas: dict[int, float | None], return_20d: float | None
) -> str:
    ma5, ma20, ma60, ma120 = (mas.get(window) for window in (5, 20, 60, 120))
    if None not in (ma5, ma20, ma60, ma120):
        assert ma5 is not None and ma20 is not None and ma60 is not None
        assert ma120 is not None
        if latest_close > ma5 > ma20 > ma60 > ma120:
            return "強勢多頭排列"
        if latest_close < ma5 < ma20 < ma60 < ma120:
            return "弱勢空頭排列"
    above_short = sum(
        latest_close > value for value in (ma5, ma20, ma60) if value is not None
    )
    if above_short >= 2 and (return_20d is None or return_20d >= 0):
        return "震盪偏多"
    if above_short <= 1 and (return_20d is None or return_20d < 0):
        return "震盪偏空"
    return "區間整理"


def build_summary(series: list[dict[str, Any]]) -> dict[str, Any]:
    latest = series[-1]
    closes = [float(row["close"]) for row in series]
    latest_close = closes[-1]
    mas = {
        window: safe_float(latest.get(f"ma{window}")) for window in MA_WINDOWS
    }
    ma_positions = {
        f"ma{window}": {
            "value": rounded(mas[window], 4),
            "close_minus_ma_pct": rounded(pct_change(latest_close, mas[window]), 2),
            "close_above": latest_close > mas[window] if mas[window] is not None else None,
        }
        for window in MA_WINDOWS
    }
    returns: dict[str, float | None] = {}
    for sessions in (5, 20, 60, 120):
        comparison = closes[-1 - sessions] if len(closes) > sessions else None
        returns[f"{sessions}d_pct"] = rounded(pct_change(latest_close, comparison), 2)

    volume = safe_int(latest["volume_shares"])
    volume_ma20 = safe_float(latest.get("volume_ma20_shares"))
    volume_ratio = (
        volume / volume_ma20
        if volume is not None and volume_ma20 is not None and volume_ma20 > 0
        else None
    )
    if volume_ratio is None:
        volume_signal = "未取得"
    elif volume_ratio >= 1.5:
        volume_signal = "明顯放量"
    elif volume_ratio >= 1.1:
        volume_signal = "溫和放量"
    elif volume_ratio < 0.7:
        volume_signal = "明顯縮量"
    else:
        volume_signal = "量能一般"

    last_240 = closes[-240:]
    high_240 = max(last_240)
    low_240 = min(last_240)
    range_position = (
        (latest_close - low_240) / (high_240 - low_240) * 100.0
        if high_240 > low_240
        else None
    )
    latest_index = len(series) - 1
    ma20_now = safe_float(series[latest_index].get("ma20"))
    ma20_then = (
        safe_float(series[latest_index - 5].get("ma20"))
        if latest_index >= 5
        else None
    )
    ma60_now = safe_float(series[latest_index].get("ma60"))
    ma60_then = (
        safe_float(series[latest_index - 20].get("ma60"))
        if latest_index >= 20
        else None
    )
    trend = classify_trend(latest_close, mas, returns["20d_pct"])
    bullish_ma_count = sum(
        latest_close > value for value in mas.values() if value is not None
    )
    trend_explanation = (
        f"收盤價位於 {bullish_ma_count}/{len(MA_WINDOWS)} 條均線之上；"
        f"近20交易日報酬 {returns['20d_pct']}%，"
        f"MA20 近5交易日斜率 {rounded(pct_change(ma20_now, ma20_then), 2)}%。"
    )

    return {
        "latest_date": latest["date"],
        "latest_adjusted_close": rounded(latest_close, 4),
        "latest_raw_close": latest.get("raw_close"),
        "latest_adjustment_factor": latest.get("adj_factor"),
        "moving_averages": ma_positions,
        "distance_to_ma240_pct": ma_positions["ma240"]["close_minus_ma_pct"],
        "returns": returns,
        "trend": trend,
        "trend_explanation": trend_explanation,
        "ma20_slope_5sessions_pct": rounded(pct_change(ma20_now, ma20_then), 2),
        "ma60_slope_20sessions_pct": rounded(pct_change(ma60_now, ma60_then), 2),
        "bullish_ma_count": bullish_ma_count,
        "latest_volume_shares": volume,
        "latest_volume_lots": rounded((volume or 0) / 1000.0, 3),
        "volume_20d_avg_shares": rounded(volume_ma20, 2),
        "volume_20d_avg_lots": rounded(
            volume_ma20 / 1000.0 if volume_ma20 is not None else None, 3
        ),
        "volume_ratio_vs_20d": rounded(volume_ratio, 3),
        "volume_signal": volume_signal,
        "rolling_240d_high": rounded(high_240, 4),
        "rolling_240d_low": rounded(low_240, 4),
        "position_in_240d_range_pct": rounded(range_position, 2),
    }


def unavailable_stock(
    ticker: str,
    name: str,
    attempts: list[dict[str, Any]],
    error: str,
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "name": name,
        "available": False,
        "status": "unavailable",
        "error": error,
        "adjustment_method": None,
        "source_attempts": attempts,
        "data_quality": None,
        "summary": None,
        "series": [],
    }


def build_stock(
    ticker: str,
    metadata: dict[str, str],
    *,
    finmind_token: str,
    refresh: bool,
) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    adj_result = finmind_get(
        "TaiwanStockPriceAdj", ticker, token=finmind_token, refresh=refresh
    )
    attempts.append(source_attempt(adj_result))
    adjustment_method: str
    factor_details: dict[str, Any] | None = None
    missing_factor_dates: list[str] = []
    raw_quality: dict[str, Any] | None = None
    dropped_zero_ohlcv_dates: list[str] = []

    if adj_result.ok and finmind_rows(adj_result):
        rows = normalize_finmind_price_rows(finmind_rows(adj_result), ticker)
        rows, dropped_zero_ohlcv_dates = drop_zero_ohlcv_placeholders(rows)
        for row in rows:
            row["raw_close"] = None
            row["adj_factor"] = None
        adjustment_method = (
            "FinMind TaiwanStockPriceAdj：官方還原股價資料表，直接以還原 OHLC "
            "計算所有均線。"
        )
    else:
        raw_result = finmind_get(
            "TaiwanStockPrice", ticker, token=finmind_token, refresh=refresh
        )
        attempts.append(source_attempt(raw_result))
        if not raw_result.ok or not finmind_rows(raw_result):
            error = raw_result.error or "FinMind TaiwanStockPrice 無資料"
            return unavailable_stock(ticker, metadata["name"], attempts, error)
        raw_rows = normalize_finmind_price_rows(finmind_rows(raw_result), ticker)
        raw_rows, dropped_zero_ohlcv_dates = drop_zero_ohlcv_placeholders(raw_rows)
        try:
            raw_quality = validate_rows(raw_rows, f"{ticker} FinMind raw")
        except DataQualityError as exc:
            return unavailable_stock(ticker, metadata["name"], attempts, str(exc))

        yahoo_result = yahoo_get(metadata["yahoo_symbol"], refresh=refresh)
        attempts.append(source_attempt(yahoo_result))
        factors, factor_details = yahoo_adjustment_factors(yahoo_result)
        if not yahoo_result.ok or not factors:
            error = yahoo_result.error or "Yahoo adjustment factor 無資料"
            return unavailable_stock(ticker, metadata["name"], attempts, error)
        inferred_factor_dates = interpolate_stable_factor_gaps(raw_rows, factors)
        factor_details["stable_gap_interpolation_tolerance"] = 1e-5
        factor_details["stable_gap_interpolated_count"] = len(
            inferred_factor_dates
        )
        factor_details["stable_gap_interpolated_dates"] = inferred_factor_dates
        rows, missing_factor_dates = apply_adjustment(raw_rows, factors)
        adjustment_method = (
            "FinMind TaiwanStockPrice 原始 OHLCV × Yahoo Finance 同交易日 "
            "adjclose/close 調整因子；逐日對齊後才計算 MA5/20/60/120/240，"
            "Yahoo 單日空洞只在前後因子相對差 <=1e-5 時以兩側平均因子補值，"
            "否則該日期排除，絕不以未還原價遞補。"
        )

    try:
        quality = validate_rows(rows, f"{ticker} adjusted")
    except DataQualityError as exc:
        return unavailable_stock(ticker, metadata["name"], attempts, str(exc))
    quality["raw_quality"] = raw_quality
    quality["adjustment_factor_details"] = factor_details
    quality["missing_adjustment_factor_count"] = len(missing_factor_dates)
    quality["missing_adjustment_factor_dates"] = missing_factor_dates
    quality["dropped_zero_ohlcv_placeholder_count"] = len(
        dropped_zero_ohlcv_dates
    )
    quality["dropped_zero_ohlcv_placeholder_dates"] = dropped_zero_ohlcv_dates
    if len(rows) < MINIMUM_REQUIRED_ROWS:
        return unavailable_stock(
            ticker,
            metadata["name"],
            attempts,
            (
                f"只有 {len(rows)} 筆有效還原日 K，不足 MA240 所需 "
                f"{MINIMUM_REQUIRED_ROWS} 筆"
            ),
        )

    series = add_indicators(rows)
    summary = build_summary(series)
    stale_days = (CUTOFF_DATE - date.fromisoformat(summary["latest_date"])).days
    quality["latest_data_calendar_lag_days_vs_cutoff"] = stale_days
    return {
        "ticker": ticker,
        "name": metadata["name"],
        "available": True,
        "status": "available",
        "error": None,
        "adjustment_method": adjustment_method,
        "price_source": "FinMind",
        "adjustment_factor_source": (
            "FinMind TaiwanStockPriceAdj"
            if factor_details is None
            else "Yahoo Finance chart adjclose/close"
        ),
        "volume_unit": "shares（另提供張數 lots=shares/1000）",
        "source_attempts": attempts,
        "data_quality": quality,
        "summary": summary,
        "series": series,
    }


def classify_market(summary: dict[str, Any]) -> tuple[str, str]:
    ma = summary["moving_averages"]
    close = safe_float(summary["latest_adjusted_close"])
    ma20 = safe_float(ma["ma20"]["value"])
    ma60 = safe_float(ma["ma60"]["value"])
    ma120 = safe_float(ma["ma120"]["value"])
    ma240 = safe_float(ma["ma240"]["value"])
    slope20 = safe_float(summary.get("ma20_slope_5sessions_pct"))
    if (
        None not in (close, ma20, ma60, ma120, ma240, slope20)
        and close > ma20 > ma60 > ma120 > ma240
        and slope20 > 0
    ):
        regime = "多頭"
    elif (
        None not in (close, ma20, ma60, ma120, ma240, slope20)
        and close < ma20 < ma60 < ma120 < ma240
        and slope20 < 0
    ):
        regime = "空頭"
    elif close is not None and ma20 is not None and ma60 is not None:
        if close > ma20 and close > ma60:
            regime = "震盪偏多"
        elif close < ma20 and close < ma60:
            regime = "震盪偏空"
        else:
            regime = "高波動整理"
    else:
        regime = "未取得"
    explanation = (
        f"TAIEX 收盤 {close}；相對 MA20/60/120/240 為 "
        f"{ma['ma20']['close_minus_ma_pct']}% / "
        f"{ma['ma60']['close_minus_ma_pct']}% / "
        f"{ma['ma120']['close_minus_ma_pct']}% / "
        f"{ma['ma240']['close_minus_ma_pct']}%；"
        f"MA20 近5交易日斜率 {slope20}%。"
    )
    return regime, explanation


def build_market(*, finmind_token: str, refresh: bool) -> dict[str, Any]:
    result = finmind_get(
        "TaiwanStockPrice", "TAIEX", token=finmind_token, refresh=refresh
    )
    attempts = [source_attempt(result)]
    if not result.ok or not finmind_rows(result):
        return {
            "ticker": "TAIEX",
            "name": "臺灣加權股價指數",
            "available": False,
            "status": "unavailable",
            "error": result.error or "FinMind TAIEX 無資料",
            "adjustment_method": None,
            "source_attempts": attempts,
            "data_quality": None,
            "summary": None,
            "market_regime": "未取得",
            "market_regime_explanation": "未取得",
            "series": [],
        }
    rows = normalize_finmind_price_rows(finmind_rows(result), "TAIEX")
    rows, dropped_zero_ohlcv_dates = drop_zero_ohlcv_placeholders(rows)
    for row in rows:
        row["raw_close"] = row["close"]
        row["adj_factor"] = 1.0
    try:
        quality = validate_rows(rows, "TAIEX")
    except DataQualityError as exc:
        return {
            "ticker": "TAIEX",
            "name": "臺灣加權股價指數",
            "available": False,
            "status": "unavailable",
            "error": str(exc),
            "adjustment_method": None,
            "source_attempts": attempts,
            "data_quality": None,
            "summary": None,
            "market_regime": "未取得",
            "market_regime_explanation": "未取得",
            "series": [],
        }
    if len(rows) < MINIMUM_REQUIRED_ROWS:
        return {
            "ticker": "TAIEX",
            "name": "臺灣加權股價指數",
            "available": False,
            "status": "unavailable",
            "error": f"只有 {len(rows)} 筆日 K，不足 MA240",
            "adjustment_method": None,
            "source_attempts": attempts,
            "data_quality": quality,
            "summary": None,
            "market_regime": "未取得",
            "market_regime_explanation": "未取得",
            "series": [],
        }
    quality["dropped_zero_ohlcv_placeholder_count"] = len(
        dropped_zero_ohlcv_dates
    )
    quality["dropped_zero_ohlcv_placeholder_dates"] = dropped_zero_ohlcv_dates
    series = add_indicators(rows)
    summary = build_summary(series)
    regime, explanation = classify_market(summary)
    quality["latest_data_calendar_lag_days_vs_cutoff"] = (
        CUTOFF_DATE - date.fromisoformat(summary["latest_date"])
    ).days
    return {
        "ticker": "TAIEX",
        "name": "臺灣加權股價指數",
        "available": True,
        "status": "available",
        "error": None,
        "adjustment_method": (
            "不適用：使用 FinMind TaiwanStockPrice(data_id=TAIEX) 官方價格指數；"
            "指數不是公司證券，沒有個股除權息還原問題。"
        ),
        "price_source": "FinMind TaiwanStockPrice",
        "volume_unit": "shares（FinMind 指數成交量欄位；另提供 lots=shares/1000）",
        "source_attempts": attempts,
        "data_quality": quality,
        "summary": summary,
        "market_regime": regime,
        "market_regime_explanation": explanation,
        "series": series,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="忽略既有成功/非402快取並重新抓取（預設一律先讀快取）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"輸出 JSON，預設 {OUTPUT_PATH}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    env_values = load_dotenv_values(ENV_PATH)
    finmind_token = (
        os.environ.get("FINMIND_API_TOKEN")
        or env_values.get("FINMIND_API_TOKEN")
        or env_values.get("FINMIND_TOKEN")
        or ""
    ).strip()

    stocks: dict[str, dict[str, Any]] = {}
    for ticker, metadata in STOCKS.items():
        stocks[ticker] = build_stock(
            ticker,
            metadata,
            finmind_token=finmind_token,
            refresh=bool(args.refresh),
        )
    market = build_market(finmind_token=finmind_token, refresh=bool(args.refresh))

    unavailable = [
        ticker for ticker, payload in stocks.items() if not payload["available"]
    ]
    if not market["available"]:
        unavailable.append("TAIEX")
    result = {
        "schema_version": 1,
        "generated_at": now_iso(),
        "analysis_as_of": CUTOFF_DATE.isoformat(),
        "hard_cutoff_date": CUTOFF_DATE.isoformat(),
        "requested_start_date": START_DATE.isoformat(),
        "methodology": {
            "signal_timing": (
                "僅使用截至 2026-07-23 的完整日 K；明確排除 2026-07-24 "
                "盤中或未完成日 K。"
            ),
            "stock_adjustment": (
                "優先 FinMind TaiwanStockPriceAdj；不可用時，以 FinMind 原始 "
                "OHLCV 逐日乘 Yahoo adjclose/close 因子。Yahoo 空洞僅在兩側因子"
                "穩定一致時插補，其他未匹配日期直接排除；絕不使用未還原價計算"
                "長天期均線。"
            ),
            "market_index": (
                "TAIEX 使用 FinMind TaiwanStockPrice(data_id=TAIEX)；"
                "公司行動還原不適用於價格指數。"
            ),
            "moving_averages": "簡單移動平均 SMA，交易日視窗 5/20/60/120/240。",
            "volume": (
                "最新完整交易日成交股數對同日含當日在內的近20交易日平均成交股數。"
            ),
            "cache": (
                "每個 HTTP query response 分別寫入 data/analysis/cache/kbars__*.json；"
                "預設重跑先讀快取，402 快取例外並採 1/2/4/8/16 秒指數退避。"
            ),
        },
        "all_required_available": not unavailable,
        "unavailable": unavailable,
        "stocks": stocks,
        "market": market,
    }
    atomic_write_json(args.output.resolve(), result)

    print(f"wrote {args.output.resolve()}")
    for ticker, payload in stocks.items():
        if payload["available"]:
            summary = payload["summary"]
            print(
                f"{ticker} {payload['name']}: {summary['latest_date']} "
                f"close={summary['latest_adjusted_close']} "
                f"MA20={summary['moving_averages']['ma20']['value']} "
                f"MA240={summary['moving_averages']['ma240']['value']} "
                f"volume_ratio20={summary['volume_ratio_vs_20d']} "
                f"trend={summary['trend']}"
            )
        else:
            print(f"{ticker} {payload['name']}: unavailable - {payload['error']}")
    if market["available"]:
        print(
            f"TAIEX: {market['summary']['latest_date']} "
            f"close={market['summary']['latest_adjusted_close']} "
            f"regime={market['market_regime']}"
        )
    else:
        print(f"TAIEX: unavailable - {market['error']}")
    return 0 if not unavailable else 2


if __name__ == "__main__":
    raise SystemExit(main())
