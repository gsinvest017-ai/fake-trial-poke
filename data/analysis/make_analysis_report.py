#!/usr/bin/env python3
"""Build the self-contained offline fake-auction focus-stock research report."""

from __future__ import annotations

import argparse
import html
import json
import math
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


ORDER = ("8039", "2392", "2201")
NAMES = {"8039": "台虹", "2392": "正崴", "2201": "裕隆"}
MA_COLORS = {
    "ma5": "#d97706",
    "ma20": "#2563eb",
    "ma60": "#7c3aed",
    "ma120": "#0891b2",
    "ma240": "#475569",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis-dir", type=Path, default=Path("data/analysis"))
    parser.add_argument("--output", type=Path, default=Path("analysis_report.html"))
    return parser.parse_args()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} is not a JSON object")
    return payload


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def fmt_num(value: Any, digits: int = 1, dash: str = "未取得") -> str:
    number = finite(value)
    if number is None:
        return dash
    if abs(number) >= 1_000:
        return f"{number:,.{digits}f}"
    return f"{number:.{digits}f}"


def fmt_price(value: Any) -> str:
    number = finite(value)
    if number is None:
        return "未取得"
    if abs(number) >= 1000:
        return f"{number:,.2f}"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def signed(value: Any, digits: int = 1, suffix: str = "") -> str:
    number = finite(value)
    if number is None:
        return "未取得"
    return f"{number:+,.{digits}f}{suffix}"


def pct_from(a: Any, b: Any) -> float | None:
    numerator, denominator = finite(a), finite(b)
    if numerator is None or denominator in (None, 0):
        return None
    return (numerator / denominator - 1.0) * 100.0


def stock_or_error(payload: dict[str, Any], code: str, source: str) -> dict[str, Any]:
    item = payload.get("stocks", {}).get(code)
    if not isinstance(item, dict):
        raise ValueError(f"{source}: missing stock {code}")
    return item


def polyline(
    values: list[float | None],
    x0: float,
    y0: float,
    width: float,
    height: float,
    low: float,
    high: float,
) -> str:
    if len(values) < 2 or high <= low:
        return ""
    points = []
    denom = max(1, len(values) - 1)
    for index, value in enumerate(values):
        if value is None:
            continue
        x = x0 + width * index / denom
        y = y0 + height * (high - value) / (high - low)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def placeholder_svg(title: str, message: str) -> str:
    return (
        '<svg class="chart" viewBox="0 0 960 360" role="img" '
        f'aria-label="{esc(title)}">'
        '<rect width="960" height="360" fill="#f8fafc"/>'
        f'<text x="480" y="165" text-anchor="middle" class="svg-title">{esc(title)}</text>'
        f'<text x="480" y="202" text-anchor="middle" class="svg-muted">{esc(message)}</text>'
        "</svg>"
    )


def date_labels(series: list[dict[str, Any]], indices: Iterable[int], y: int) -> str:
    result = []
    denom = max(1, len(series) - 1)
    for index in indices:
        if index < 0 or index >= len(series):
            continue
        x = 66 + 860 * index / denom
        label = str(series[index].get("date", ""))[5:]
        result.append(
            f'<text x="{x:.1f}" y="{y}" text-anchor="middle" class="svg-axis">{esc(label)}</text>'
        )
    return "".join(result)


def kbar_svg(stock: dict[str, Any]) -> str:
    raw = stock.get("series")
    if stock.get("available") is not True or not isinstance(raw, list) or len(raw) < 20:
        return placeholder_svg("還原日 K 與均線", stock.get("error") or "未取得")
    series = raw[-120:]
    price_values: list[float] = []
    for row in series:
        for key in ("low", "high", "ma5", "ma20", "ma60", "ma120", "ma240"):
            value = finite(row.get(key))
            if value is not None:
                price_values.append(value)
    if not price_values:
        return placeholder_svg("還原日 K 與均線", "沒有可繪製價格")
    low, high = min(price_values), max(price_values)
    padding = max((high - low) * 0.06, abs(high) * 0.005, 0.1)
    low, high = low - padding, high + padding
    x0, width = 66.0, 860.0
    price_y, price_h = 34.0, 272.0
    volume_y, volume_h = 326.0, 76.0
    denom = max(1, len(series) - 1)
    candle_width = max(1.4, min(5.4, width / len(series) * 0.58))

    parts = [
        '<svg class="chart" viewBox="0 0 960 445" role="img" aria-label="還原日K均線與成交量">',
        '<rect width="960" height="445" fill="#ffffff"/>',
        '<text x="66" y="20" class="svg-title">還原日 K ＋ MA5/20/60/120/240</text>',
    ]
    for level in range(5):
        y = price_y + price_h * level / 4
        value = high - (high - low) * level / 4
        parts.append(
            f'<line x1="{x0}" x2="{x0 + width}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
            f'<text x="58" y="{y + 4:.1f}" text-anchor="end" class="svg-axis">{fmt_price(value)}</text>'
        )

    max_volume = max(finite(row.get("volume_shares")) or 0 for row in series) or 1.0
    for index, row in enumerate(series):
        x = x0 + width * index / denom
        open_, close = finite(row.get("open")), finite(row.get("close"))
        high_, low_ = finite(row.get("high")), finite(row.get("low"))
        if None not in (open_, close, high_, low_):
            y_high = price_y + price_h * (high - high_) / (high - low)
            y_low = price_y + price_h * (high - low_) / (high - low)
            y_open = price_y + price_h * (high - open_) / (high - low)
            y_close = price_y + price_h * (high - close) / (high - low)
            color = "#dc2626" if close >= open_ else "#059669"
            body_y, body_h = min(y_open, y_close), max(1.0, abs(y_close - y_open))
            parts.append(
                f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y_high:.1f}" y2="{y_low:.1f}" '
                f'stroke="{color}" stroke-width="1"/>'
                f'<rect x="{x - candle_width / 2:.1f}" y="{body_y:.1f}" '
                f'width="{candle_width:.1f}" height="{body_h:.1f}" fill="{color}" opacity=".78"/>'
            )
        volume = finite(row.get("volume_shares")) or 0
        bar_h = volume_h * volume / max_volume
        vol_color = "#ef4444" if close is not None and open_ is not None and close >= open_ else "#10b981"
        parts.append(
            f'<rect x="{x - candle_width / 2:.1f}" y="{volume_y + volume_h - bar_h:.1f}" '
            f'width="{candle_width:.1f}" height="{bar_h:.1f}" fill="{vol_color}" opacity=".38"/>'
        )

    for key, color in MA_COLORS.items():
        values = [finite(row.get(key)) for row in series]
        points = polyline(values, x0, price_y, width, price_h, low, high)
        if points:
            parts.append(
                f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="1.8" '
                'stroke-linejoin="round" stroke-linecap="round"/>'
            )

    parts.append(f'<line x1="{x0}" x2="{x0 + width}" y1="316" y2="316" class="grid"/>')
    parts.append(
        date_labels(series, (0, len(series) // 3, 2 * len(series) // 3, len(series) - 1), 430)
    )
    legend_x = 510
    for index, (key, color) in enumerate(MA_COLORS.items()):
        x = legend_x + index * 84
        parts.append(
            f'<line x1="{x}" x2="{x + 18}" y1="18" y2="18" stroke="{color}" stroke-width="3"/>'
            f'<text x="{x + 23}" y="22" class="svg-axis">{key.upper()}</text>'
        )
    parts.append('<text x="66" y="322" class="svg-axis">成交量</text></svg>')
    return "".join(parts)


def auction_svg(stock: dict[str, Any]) -> str:
    series = stock.get("bid_one_series")
    if not isinstance(series, list) or len(series) < 2:
        return placeholder_svg("試撮買一價量回放", "未取得買一價量序列")
    prices = [finite(row.get("bid_price")) for row in series]
    valid_prices = [value for value in prices if value is not None and value > 0]
    if not valid_prices:
        return placeholder_svg("試撮買一價量回放", "買一價格皆無效")
    limit_up = finite(stock.get("limit_up_price"))
    open_price = finite(stock.get("open_price"))
    domain = valid_prices + [v for v in (limit_up, open_price) if v is not None]
    low, high = min(domain), max(domain)
    padding = max((high - low) * 0.08, high * 0.01)
    low, high = low - padding, high + padding
    x0, width = 66.0, 860.0
    price_y, price_h = 36.0, 222.0
    volume_y, volume_h = 282.0, 92.0
    denom = max(1, len(series) - 1)
    volumes = [finite(row.get("bid_volume_lots")) or 0 for row in series]
    max_volume = max(volumes) or 1
    parts = [
        '<svg class="chart" viewBox="0 0 960 420" role="img" aria-label="試撮買一價量回放">',
        '<rect width="960" height="420" fill="#ffffff"/>',
        '<text x="66" y="20" class="svg-title">試撮期買一價／量回放</text>',
    ]
    for level in range(4):
        y = price_y + price_h * level / 3
        value = high - (high - low) * level / 3
        parts.append(
            f'<line x1="{x0}" x2="{x0 + width}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
            f'<text x="58" y="{y + 4:.1f}" text-anchor="end" class="svg-axis">{fmt_price(value)}</text>'
        )
    for index, volume in enumerate(volumes):
        x = x0 + width * index / denom
        bar_h = volume_h * volume / max_volume
        bar_w = max(1, width / len(series) * 0.7)
        parts.append(
            f'<rect x="{x - bar_w / 2:.1f}" y="{volume_y + volume_h - bar_h:.1f}" '
            f'width="{bar_w:.1f}" height="{bar_h:.1f}" fill="#64748b" opacity=".35"/>'
        )
    points = polyline(prices, x0, price_y, width, price_h, low, high)
    parts.append(
        f'<polyline points="{points}" fill="none" stroke="#0f766e" stroke-width="2.4" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
    )
    for value, color, label in (
        (limit_up, "#dc2626", "試撮漲停"),
        (open_price, "#1d4ed8", "實際開盤"),
    ):
        if value is None:
            continue
        y = price_y + price_h * (high - value) / (high - low)
        parts.append(
            f'<line x1="{x0}" x2="{x0 + width}" y1="{y:.1f}" y2="{y:.1f}" '
            f'stroke="{color}" stroke-width="1.5" stroke-dasharray="7 5"/>'
            f'<text x="{x0 + width - 4}" y="{y - 6:.1f}" text-anchor="end" '
            f'fill="{color}" class="svg-axis">{label} {fmt_price(value)}</text>'
        )
    first_time = str(series[0].get("time", ""))[11:19]
    last_time = str(series[-1].get("time", ""))[11:19]
    parts.extend(
        [
            f'<text x="{x0}" y="405" class="svg-axis">{esc(first_time)}</text>',
            f'<text x="{x0 + width}" y="405" text-anchor="end" class="svg-axis">{esc(last_time)}</text>',
            '<text x="66" y="278" class="svg-axis">買一量（張）</text>',
            "</svg>",
        ]
    )
    return "".join(parts)


def margin_svg(stock: dict[str, Any]) -> str:
    series = stock.get("series")
    if stock.get("availability") != "available" or not isinstance(series, list) or len(series) < 2:
        return placeholder_svg("融資融券趨勢", stock.get("reason") or "未取得")
    series = series[-60:]
    balance = [finite(row.get("margin_balance_lots")) for row in series]
    utilization = [finite(row.get("margin_utilization_pct")) for row in series]
    short_ratio = [finite(row.get("short_margin_ratio_pct")) for row in series]
    valid_balance = [v for v in balance if v is not None]
    if not valid_balance:
        return placeholder_svg("融資融券趨勢", "融資餘額未取得")
    bal_low, bal_high = min(valid_balance), max(valid_balance)
    if bal_high == bal_low:
        bal_high += 1
    util_values = [v for v in utilization + short_ratio if v is not None]
    util_high = max(util_values + [1.0]) * 1.12
    parts = [
        '<svg class="chart" viewBox="0 0 960 400" role="img" aria-label="融資融券趨勢">',
        '<rect width="960" height="400" fill="#ffffff"/>',
        '<text x="66" y="20" class="svg-title">融資餘額與信用指標（60 交易日）</text>',
    ]
    x0, width = 66.0, 860.0
    for y in (42, 110, 178, 246):
        parts.append(f'<line x1="{x0}" x2="{x0 + width}" y1="{y}" y2="{y}" class="grid"/>')
    balance_points = polyline(balance, x0, 42, width, 204, bal_low, bal_high)
    parts.append(
        f'<polyline points="{balance_points}" fill="none" stroke="#b45309" stroke-width="2.5"/>'
        f'<text x="58" y="48" text-anchor="end" class="svg-axis">{fmt_num(bal_high, 0)}</text>'
        f'<text x="58" y="248" text-anchor="end" class="svg-axis">{fmt_num(bal_low, 0)}</text>'
        '<text x="72" y="62" fill="#b45309" class="svg-axis">融資餘額（張）</text>'
    )
    parts.append('<line x1="66" x2="926" y1="276" y2="276" class="grid"/>')
    util_points = polyline(utilization, x0, 286, width, 72, 0, util_high)
    ratio_points = polyline(short_ratio, x0, 286, width, 72, 0, util_high)
    parts.append(
        f'<polyline points="{util_points}" fill="none" stroke="#2563eb" stroke-width="2"/>'
        f'<polyline points="{ratio_points}" fill="none" stroke="#7c3aed" stroke-width="2"/>'
        '<text x="72" y="301" fill="#2563eb" class="svg-axis">融資使用率</text>'
        '<text x="158" y="301" fill="#7c3aed" class="svg-axis">券資比</text>'
    )
    parts.append(
        date_labels(series, (0, len(series) // 2, len(series) - 1), 386)
    )
    parts.append("</svg>")
    return "".join(parts)


def chips_svg(stock: dict[str, Any]) -> str:
    series = stock.get("daily")
    if stock.get("availability") != "available" or not isinstance(series, list) or len(series) < 2:
        return placeholder_svg("三大法人買賣超", stock.get("reason") or "未取得")
    series = series[-20:]
    keys = (
        ("foreign_net_lots", "#2563eb", "外資"),
        ("investment_trust_net_lots", "#d97706", "投信"),
        ("dealer_net_lots", "#7c3aed", "自營"),
    )
    values = [
        finite(row.get(key)) or 0
        for row in series
        for key, _, _ in keys
    ]
    magnitude = max([abs(v) for v in values] + [1.0])
    x0, width = 66.0, 860.0
    y_mid, half_h = 198.0, 142.0
    group_width = width / len(series)
    bar_width = max(2.0, group_width / 4.5)
    parts = [
        '<svg class="chart" viewBox="0 0 960 390" role="img" aria-label="三大法人買賣超">',
        '<rect width="960" height="390" fill="#ffffff"/>',
        '<text x="66" y="20" class="svg-title">三大法人每日買賣超（近 20 交易日，張）</text>',
    ]
    for level in (-1, -0.5, 0, 0.5, 1):
        y = y_mid - half_h * level
        label = magnitude * level
        parts.append(
            f'<line x1="{x0}" x2="{x0 + width}" y1="{y:.1f}" y2="{y:.1f}" '
            f'class="{"zero" if level == 0 else "grid"}"/>'
            f'<text x="58" y="{y + 4:.1f}" text-anchor="end" class="svg-axis">{fmt_num(label, 0)}</text>'
        )
    for index, row in enumerate(series):
        center = x0 + group_width * (index + 0.5)
        for offset, (key, color, _) in enumerate(keys):
            value = finite(row.get(key)) or 0
            height = half_h * abs(value) / magnitude
            x = center + (offset - 1) * bar_width
            y = y_mid - height if value >= 0 else y_mid
            parts.append(
                f'<rect x="{x - bar_width * .44:.1f}" y="{y:.1f}" '
                f'width="{bar_width * .88:.1f}" height="{max(height, .6):.1f}" '
                f'fill="{color}" opacity=".82"/>'
            )
    for index, (_, color, label) in enumerate(keys):
        x = 676 + index * 80
        parts.append(
            f'<rect x="{x}" y="10" width="10" height="10" rx="2" fill="{color}"/>'
            f'<text x="{x + 15}" y="20" class="svg-axis">{label}</text>'
        )
    parts.append(
        date_labels(series, (0, 4, 9, 14, 19), 378)
    )
    parts.append("</svg>")
    return "".join(parts)


def market_svg(market: dict[str, Any]) -> str:
    series = market.get("series")
    if market.get("available") is not True or not isinstance(series, list) or len(series) < 20:
        return placeholder_svg("TAIEX 市場環境", market.get("error") or "未取得")
    series = series[-100:]
    keys = (("close", "#0f766e"), ("ma20", "#2563eb"), ("ma60", "#7c3aed"))
    values = [
        finite(row.get(key))
        for row in series
        for key, _ in keys
        if finite(row.get(key)) is not None
    ]
    low, high = min(values), max(values)
    padding = (high - low) * 0.08
    low, high = low - padding, high + padding
    parts = [
        '<svg class="market-chart" viewBox="0 0 960 250" role="img" aria-label="TAIEX市場環境">',
        '<rect width="960" height="250" fill="#ffffff"/>',
    ]
    for level in range(4):
        y = 22 + 170 * level / 3
        value = high - (high - low) * level / 3
        parts.append(
            f'<line x1="66" x2="926" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>'
            f'<text x="58" y="{y + 4:.1f}" text-anchor="end" class="svg-axis">{fmt_num(value, 0)}</text>'
        )
    for key, color in keys:
        points = polyline([finite(row.get(key)) for row in series], 66, 22, 860, 170, low, high)
        parts.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" '
            f'stroke-width="{"2.6" if key == "close" else "1.8"}"/>'
        )
    parts.append(date_labels(series, (0, len(series) // 2, len(series) - 1), 226))
    parts.append(
        '<text x="680" y="18" fill="#0f766e" class="svg-axis">收盤</text>'
        '<text x="740" y="18" fill="#2563eb" class="svg-axis">MA20</text>'
        '<text x="807" y="18" fill="#7c3aed" class="svg-axis">MA60</text></svg>'
    )
    return "".join(parts)


def average_range_pct(series: list[dict[str, Any]], periods: int = 20) -> float | None:
    values = []
    for row in series[-periods:]:
        high, low, close = finite(row.get("high")), finite(row.get("low")), finite(row.get("close"))
        if None not in (high, low, close) and close:
            values.append((high - low) / close * 100.0)
    return mean(values) if values else None


def liquidity_assessment(kbar: dict[str, Any]) -> dict[str, Any]:
    summary = kbar.get("summary", {})
    close = finite(summary.get("latest_adjusted_close"))
    avg_lots = finite(summary.get("volume_20d_avg_lots"))
    latest_lots = finite(summary.get("latest_volume_lots"))
    avg_turnover = (
        avg_lots * close / 1000.0
        if avg_lots is not None and close is not None
        else None
    )
    latest_turnover = (
        latest_lots * close / 1000.0
        if latest_lots is not None and close is not None
        else None
    )
    if avg_turnover is None:
        grade = "未取得"
        note = "成交值資料不足，無法評估個股可交易性。"
    elif avg_turnover >= 500:
        grade = "高流動性，但事件風險高"
        note = "20 日日均估算成交值高於 5 億元；一般單量具執行空間，但異常日需放大滑價與停損假設。"
    elif avg_turnover >= 100:
        grade = "中等流動性"
        note = "20 日日均估算成交值介於 1–5 億元；適合分批與限價，避免用市價追單。"
    elif avg_turnover >= 50:
        grade = "條件式可交易"
        note = "20 日日均估算成交值約 0.5–1 億元；小部位可考慮，需限價並避開量縮時段。"
    elif avg_turnover >= 20:
        grade = "偏低流動性"
        note = "20 日日均估算成交值低於 0.5 億元；僅適合小部位、限價與較寬滑價預算。"
    else:
        grade = "不宜一般交易"
        note = "20 日日均估算成交值低於 0.2 億元，委託衝擊與滑價風險高。"
    return {
        "grade": grade,
        "note": note,
        "avg_turnover_m_twd": avg_turnover,
        "latest_turnover_m_twd": latest_turnover,
        "range20_pct": average_range_pct(kbar.get("series", [])),
    }


def auditable_market_label(market: dict[str, Any]) -> str:
    moving = market.get("summary", {}).get("moving_averages", {})
    above = {
        key: moving.get(key, {}).get("close_above")
        for key in ("ma20", "ma60", "ma120", "ma240")
    }
    if above["ma20"] is False and all(above[key] is True for key in ("ma60", "ma120", "ma240")):
        return "短線整理、中長線仍偏多"
    if all(value is True for value in above.values()):
        return "多頭趨勢"
    if above["ma60"] is False and above["ma120"] is False:
        return "中期偏弱"
    return "多空交錯"


def kbar_quality_note(kbar: dict[str, Any]) -> str:
    quality = kbar.get("data_quality", {})
    factor = quality.get("adjustment_factor_details", {})
    parts = [
        f"有效 {fmt_num(quality.get('row_count'), 0)} 筆",
        f"期間 {quality.get('first_date', '未取得')} 至 {quality.get('last_date', '未取得')}",
        "MA240 歷史充足" if quality.get("has_ma240_history") else "MA240 歷史不足",
    ]
    interpolated = factor.get("stable_gap_interpolated_dates", [])
    if interpolated:
        parts.append(
            "調整因子穩定缺口插補 "
            + "、".join(str(value) for value in interpolated)
            + "（前後因子差在 1e-5 內）"
        )
    placeholders = quality.get("dropped_zero_ohlcv_placeholder_dates", [])
    if placeholders:
        parts.append("排除全零 OHLCV 占位列 " + "、".join(str(value) for value in placeholders))
    return "；".join(parts) + "。"


def ma_open_rows(kbar: dict[str, Any], open_price: float | None) -> str:
    moving = kbar.get("summary", {}).get("moving_averages", {})
    rows = []
    for key in ("ma5", "ma20", "ma60", "ma120", "ma240"):
        value = finite(moving.get(key, {}).get("value"))
        rows.append(
            "<tr>"
            f"<td>{key.upper()}</td><td>{fmt_price(value)}</td>"
            f"<td class=\"{tone_class(pct_from(open_price, value))}\">{signed(pct_from(open_price, value), 2, '%')}</td>"
            "</tr>"
        )
    return "".join(rows)


def tone_class(value: Any, inverse: bool = False) -> str:
    number = finite(value)
    if number is None or abs(number) < 1e-10:
        return "neutral"
    positive = number > 0
    if inverse:
        positive = not positive
    return "positive" if positive else "negative"


def institution_table(chips: dict[str, Any]) -> str:
    summary = chips.get("summary", {})
    rows = []
    labels = (
        ("foreign", "外資"),
        ("investment_trust", "投信"),
        ("dealer", "自營商"),
        ("total", "合計"),
    )
    for key, label in labels:
        item = summary.get(key, {})
        latest = finite(item.get("latest_net_lots"))
        five = finite(item.get("last_5d", {}).get("net_lots"))
        twenty = finite(item.get("last_20d", {}).get("net_lots"))
        streak = item.get("streak", {})
        direction = {"buy": "連買", "sell": "連賣"}.get(streak.get("direction"), "無連續")
        days = streak.get("days")
        streak_text = f"{direction}{days}日" if days else direction
        rows.append(
            "<tr>"
            f"<td>{label}</td>"
            f"<td class=\"{tone_class(latest)}\">{signed(latest, 1)}</td>"
            f"<td class=\"{tone_class(five)}\">{signed(five, 1)}</td>"
            f"<td class=\"{tone_class(twenty)}\">{signed(twenty, 1)}</td>"
            f"<td>{esc(streak_text)}</td></tr>"
        )
    return "".join(rows)


def evidence_and_verdict(
    code: str,
    auction: dict[str, Any],
    kbar: dict[str, Any],
    margin: dict[str, Any],
    chips: dict[str, Any],
    market: dict[str, Any],
    liquidity: dict[str, Any],
) -> dict[str, Any]:
    ksum = kbar.get("summary", {})
    mlatest = margin.get("latest", {})
    m20 = finite(margin.get("changes", {}).get("margin_20d", {}).get("pct"))
    total5 = finite(chips.get("summary", {}).get("total", {}).get("last_5d", {}).get("net_lots"))
    total20 = finite(chips.get("summary", {}).get("total", {}).get("last_20d", {}).get("net_lots"))
    foreign5 = finite(chips.get("summary", {}).get("foreign", {}).get("last_5d", {}).get("net_lots"))
    gap = finite(auction.get("auction_to_open_gap_pct"))
    withdrawal = finite(auction.get("withdrawn_from_peak_pct"))
    volume_ratio = finite(ksum.get("volume_ratio_vs_20d"))
    return20 = finite(ksum.get("returns", {}).get("20d_pct"))
    close = finite(ksum.get("latest_adjusted_close"))
    open_price = finite(auction.get("open_price"))
    real_open_gap = pct_from(open_price, close)
    average_volume_lots = finite(ksum.get("volume_20d_avg_lots"))
    withdrawn_lots = finite(auction.get("withdrawn_from_peak_lots"))
    withdrawal_vs_average_volume = (
        withdrawn_lots / average_volume_lots * 100.0
        if withdrawn_lots is not None and average_volume_lots not in (None, 0)
        else None
    )
    regime = auditable_market_label(market)
    evidence = [
        f"試撮漲停 {fmt_price(auction.get('limit_up_price'))} 元至開盤 {fmt_price(open_price)} 元，落差 {signed(gap, 2, '%')}；相對前一日收盤實際開盤 {signed(real_open_gap, 2, '%')}。",
        f"鎖漲停約 {fmt_num(auction.get('lock_duration_seconds'), 1)} 秒；鎖單高峰至末筆縮減 {fmt_num(withdrawal, 1)}%，撤減張數約為 20 日均量的 {fmt_num(withdrawal_vs_average_volume, 2)}%，判定為「{auction.get('withdrawal_assessment', '未取得')}」。",
        f"20 日報酬 {signed(return20, 2, '%')}、量比 {fmt_num(volume_ratio, 3)}，技術狀態為「{ksum.get('trend', '未取得')}」。",
        f"融資 20 日變化 {signed(m20, 2, '%')}；最新使用率 {fmt_num(mlatest.get('margin_utilization_pct'), 2)}%、券資比 {fmt_num(mlatest.get('short_margin_ratio_pct'), 2)}%。",
        f"三大法人 5 日合計 {signed(total5, 1)} 張、20 日合計 {signed(total20, 1)} 張；其中外資 5 日 {signed(foreign5, 1)} 張。",
        f"大盤為「{regime}」；TAIEX 20 日報酬 {signed(market.get('summary', {}).get('returns', {}).get('20d_pct'), 2, '%')}。",
    ]

    if code == "8039":
        title = "高風險：高檔加速、融資堆高與短線法人轉賣同時出現"
        tone = "danger"
        conclusion = (
            "疑似假試撮的解釋力很強。股價在 20 日內急漲且位於 240 日區間頂端，"
            "7/23 已明顯放量；同時融資 20 日大增、外資近 5 日轉為大幅賣超。"
            "雖然投信、自營商與 20 日法人合計仍偏多，這是反例也是主要不確定性，"
            "但它更像中期買盤尚未完全退出、短線籌碼正在劇烈換手，不能用來合理化追價。"
        )
        action = (
            "可交易但只適合事件型、嚴格風控的高流動性標的；不追試撮價或開盤反彈。"
            "若要證偽偏空判讀，至少需看到實際成交量續強、價格重新站穩 7/23 收盤，"
            "且外資賣超明顯收斂；否則把 5 日線與前一日低點視為風險界線。"
        )
        counter = (
            "反例：20 日三大法人仍為淨買超、投信連買，券資比亦達兩位數，存在軋空急彈風險；"
            f"且鎖單撤減僅約日均量 {fmt_num(withdrawal_vs_average_volume, 2)}%。"
            "因此本報告判為高風險籌碼換手，不直接斷言已完成出貨。"
        )
    elif code == "2392":
        title = "偏空：試撮拉抬未獲中期趨勢、量能或法人籌碼確認"
        tone = "warning"
        conclusion = (
            "試撮期價格失真與撤單跡象成立，但不像融資追高型出貨。試撮至開盤的 -10.33% "
            "不是實際隔夜跌幅；實際開盤相對前收僅小幅下跌。融資 20 日小幅下降且使用率低，"
            "排除「散戶槓桿極端堆積」的故事；真正的弱點是股價仍在多數均線下、"
            "20 日法人明顯賣超、最新量低於 20 日均量。近兩日法人轉為小買是反例，"
            "目前規模尚不足以扭轉 5/20 日累計供給。"
        )
        action = (
            "屬條件式可交易、但不宜用市價追單。較合理的多方確認是帶量站回 MA20/MA60，"
            "且外資 5 日累計由負轉正；未出現前，反彈較像區間交易而非趨勢啟動。"
        )
        counter = (
            "反例：法人已連續兩日小幅買超、融資沒有堆高；若後續放量站回中期均線，偏空判讀需撤回。"
            f"但本檔撤減張數約達 20 日均量 {fmt_num(withdrawal_vs_average_volume, 2)}%，是三檔中試撮委託量體最具經濟意義者。"
        )
    else:
        title = "訊號分裂：試撮撤離很強，但法人買超與融資減少提供中期支撐"
        tone = "caution"
        conclusion = (
            "不能把這檔直接歸類為拉高出貨。鎖單自高峰縮減超過九成，但絕對量小，"
            "較像低深度委託造成的試撮價格失真；"
            "但實際開盤高於前一日收盤，20 日外資與法人合計為明顯買超，融資則下降，"
            "是較健康的法人進、散戶槓桿未追高組合。限制在於股價仍低於年線附近、"
            "7/23 成交量明顯萎縮，買盤是否能轉成有效突破仍未確認。"
        )
        action = (
            "可列觀察型、條件式交易，不追逐試撮訊號。需等實際成交量回到 20 日均量附近，"
            "並有效站穩 MA120／MA240 方向的壓力帶；若法人買超中止且跌破近期區間低點，"
            "則中期支撐假設失效。"
        )
        counter = (
            "反例：籌碼面相對健康，故判斷是「可疑試撮」而非「已證實出貨」；"
            f"雖撤減比例很高，撤減張數僅約 20 日均量 {fmt_num(withdrawal_vs_average_volume, 2)}%，"
            "經濟量體小。縮量也可能只是供給收斂，須由後續真實成交確認。"
        )
    return {
        "title": title,
        "tone": tone,
        "conclusion": conclusion,
        "action": action,
        "counter": counter,
        "evidence": evidence,
        "liquidity_grade": liquidity["grade"],
        "withdrawal_vs_average_volume_pct": withdrawal_vs_average_volume,
    }


def metric(label: str, value: str, note: str = "") -> str:
    return (
        '<div class="metric">'
        f'<div class="metric-label">{esc(label)}</div>'
        f'<div class="metric-value">{value}</div>'
        f'<div class="metric-note">{esc(note)}</div>'
        "</div>"
    )


def render_stock(
    code: str,
    auction: dict[str, Any],
    kbar: dict[str, Any],
    margin: dict[str, Any],
    chips: dict[str, Any],
    market: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    name = auction.get("name") or NAMES[code]
    ksum = kbar.get("summary", {})
    mlatest = margin.get("latest", {})
    mchanges = margin.get("changes", {})
    classification = chips.get("classification", {})
    industries = classification.get("industry_categories", [])
    industry_text = "、".join(industries) if industries else "未取得"
    liquidity = liquidity_assessment(kbar)
    verdict = evidence_and_verdict(code, auction, kbar, margin, chips, market, liquidity)
    open_price = finite(auction.get("open_price"))
    latest_close = finite(ksum.get("latest_adjusted_close"))
    disposition = chips.get("disposition", {})
    full_cash = chips.get("full_cash_settlement", {})
    margin5 = mchanges.get("margin_5d", {})
    margin20 = mchanges.get("margin_20d", {})
    sector_note = (
        f"FinMind 分類：{industry_text}。本次未另建全類股橫向量價樣本，"
        "因此「可交易」只按個股成交值、量能與波動評估，不把個股結果冒充類股強弱。"
    )

    html_out = [
        f'<section class="stock-section" id="stock-{code}">',
        '<div class="section-kicker">FOCUS STOCK</div>',
        f'<h2>{esc(code)} {esc(name)}</h2>',
        f'<p class="verdict {verdict["tone"]}">{esc(verdict["title"])}</p>',
        '<div class="metrics-grid">',
        metric(
            "試撮 → 開盤",
            f'{fmt_price(auction.get("limit_up_price"))} → {fmt_price(open_price)}',
            f'落差 {signed(auction.get("auction_to_open_gap_pct"), 2, "%")}',
        ),
        metric(
            "實際開盤 vs 前收",
            signed(pct_from(open_price, latest_close), 2, "%"),
            f'前收 {fmt_price(latest_close)}',
        ),
        metric(
            "20日量比",
            fmt_num(ksum.get("volume_ratio_vs_20d"), 3),
            ksum.get("volume_signal", "未取得"),
        ),
        metric(
            "融資20日",
            signed(margin20.get("pct"), 2, "%"),
            f'{signed(margin20.get("lots"), 0)} 張',
        ),
        metric(
            "法人5日合計",
            signed(chips.get("summary", {}).get("total", {}).get("last_5d", {}).get("net_lots"), 1),
            "張",
        ),
        metric("可交易性", esc(liquidity["grade"]), f"類股：{industry_text}"),
        "</div>",
        '<div class="analysis-block">',
        '<div class="block-number">01</div><div><h3>試撮訊號</h3>',
        '<div class="two-col"><div>',
        '<table class="data-table"><tbody>',
        f'<tr><th>參考價</th><td>{fmt_price(auction.get("reference_price"))}</td></tr>',
        f'<tr><th>試撮高點／漲停價</th><td>{fmt_price(auction.get("simulated_high"))}</td></tr>',
        f'<tr><th>首鎖／末鎖</th><td>{esc(str(auction.get("first_lock_time", "未取得"))[11:19])} ／ {esc(str(auction.get("last_lock_time", "未取得"))[11:19])}</td></tr>',
        f'<tr><th>鎖漲停時間</th><td>{fmt_num(auction.get("lock_duration_seconds"), 1)} 秒</td></tr>',
        f'<tr><th>鎖單量高峰 → 末筆</th><td>{fmt_num(auction.get("max_limit_bid_volume_lots"), 0)} → {fmt_num(auction.get("last_limit_bid_volume_lots"), 0)} 張</td></tr>',
        f'<tr><th>撤減量／20日均量</th><td>{fmt_num(auction.get("withdrawn_from_peak_lots"), 0)} 張／{fmt_num(verdict.get("withdrawal_vs_average_volume_pct"), 2)}%</td></tr>',
        f'<tr><th>序列重建撤單判定</th><td>{esc(auction.get("withdrawal_assessment", "未取得"))}</td></tr>',
        f'<tr><th>原 detector bid0_dropped</th><td>{esc(str(auction.get("detector_bid0_dropped_flag", "未取得")).lower())}（未觸發不否定後續序列重建）</td></tr>',
        '</tbody></table></div>',
        f'<div class="callout"><strong>判讀</strong><br>{esc(auction.get("withdrawal_reason", "未取得"))}<br><br>'
        "此判定是買一價量行為跡象，不是交易所委託序號層級的違規認定。</div></div>",
        f'<figure>{auction_svg(auction)}<figcaption>來源：本機盤前 recorder 原始 bidask 事件；買一量單位為張。</figcaption></figure>',
        "</div></div>",
        '<div class="analysis-block">',
        '<div class="block-number">02</div><div><h3>日 K 位置與還原均線</h3>',
        '<div class="two-col"><div>',
        '<table class="data-table"><thead><tr><th>均線</th><th>還原均線值</th><th>7/24 開盤相對位置</th></tr></thead><tbody>',
        ma_open_rows(kbar, open_price),
        "</tbody></table></div>",
        f'<div class="callout"><strong>{esc(ksum.get("trend", "未取得"))}</strong><br>'
        f'{esc(ksum.get("trend_explanation", "未取得"))}<br><br>'
        f'240 日區間位置 {fmt_num(ksum.get("position_in_240d_range_pct"), 1)}%；'
        f'距 MA240 {signed(ksum.get("distance_to_ma240_pct"), 2, "%")}（以 7/23 收盤）。</div></div>',
        f'<figure>{kbar_svg(kbar)}<figcaption>價格為還原權值序列；圖顯示最近 120 個有效交易日，均線由完整歷史計算。</figcaption></figure>',
        '<p class="method-note"><strong>還原方式：</strong>'
        f'{esc(kbar.get("adjustment_method", "未取得"))}<br><strong>資料品質：</strong>{esc(kbar_quality_note(kbar))}</p>',
        "</div></div>",
        '<div class="analysis-block">',
        '<div class="block-number">03</div><div><h3>量能與流動性</h3>',
        '<div class="metrics-grid compact">',
        metric("7/23 成交量", f'{fmt_num(ksum.get("latest_volume_lots"), 1)} 張', ksum.get("latest_date", "")),
        metric("20日均量", f'{fmt_num(ksum.get("volume_20d_avg_lots"), 1)} 張'),
        metric("估算日均成交值", f'{fmt_num(liquidity["avg_turnover_m_twd"], 1)} 百萬元', "20日均量×7/23收盤"),
        metric("20日平均日內振幅", f'{fmt_num(liquidity["range20_pct"], 2)}%', "（高－低）／收盤"),
        "</div>",
        f'<p>{esc(liquidity["note"])} 最新日估算成交值約 {fmt_num(liquidity["latest_turnover_m_twd"], 1)} 百萬元；'
        "量比只回答「7/23 有沒有量」，不代表 7/24 盤中一定維持同等深度。</p>",
        "</div></div>",
        '<div class="analysis-block">',
        '<div class="block-number">04</div><div><h3>融資融券</h3>',
        '<div class="two-col"><div><table class="data-table"><tbody>',
        f'<tr><th>融資餘額</th><td>{fmt_num(mlatest.get("margin_balance_lots"), 0)} 張</td></tr>',
        f'<tr><th>融資 5 日</th><td class="{tone_class(margin5.get("pct"), inverse=True)}">{signed(margin5.get("lots"), 0)} 張（{signed(margin5.get("pct"), 2, "%")}）</td></tr>',
        f'<tr><th>融資 20 日</th><td class="{tone_class(margin20.get("pct"), inverse=True)}">{signed(margin20.get("lots"), 0)} 張（{signed(margin20.get("pct"), 2, "%")}）</td></tr>',
        f'<tr><th>融券餘額／券資比</th><td>{fmt_num(mlatest.get("short_balance_lots"), 0)} 張／{fmt_num(mlatest.get("short_margin_ratio_pct"), 3)}%</td></tr>',
        f'<tr><th>融資使用率</th><td>{fmt_num(mlatest.get("margin_utilization_pct"), 3)}%</td></tr>',
        '</tbody></table></div>',
        '<div class="callout"><strong>口徑</strong><br>融資變化為期末餘額減 5／20 個交易期前餘額；'
        "使用率＝融資餘額／FinMind 明確提供的融資限額，未把買賣增減誤當使用率。</div></div>",
        f'<figure>{margin_svg(margin)}<figcaption>FinMind 融資融券，截止 {esc(margin.get("as_of", "未取得"))}。</figcaption></figure>',
        "</div></div>",
        '<div class="analysis-block">',
        '<div class="block-number">05</div><div><h3>三大法人籌碼</h3>',
        '<table class="data-table wide"><thead><tr><th>法人</th><th>最新日</th><th>5日</th><th>20日</th><th>連續動向</th></tr></thead><tbody>',
        institution_table(chips),
        "</tbody></table>",
        f'<figure>{chips_svg(chips)}<figcaption>FinMind 法人買賣超，單位張；截止 {esc(chips.get("as_of", "未取得"))}。</figcaption></figure>',
        '<div class="unavailable-grid">',
        f'<div><strong>處置狀態：</strong>{esc("未取得" if disposition.get("availability") != "available" else "已取得")}<br>'
        f'<span>{esc(disposition.get("reason", "") if disposition.get("availability") != "available" else str(disposition.get("records", "")))}</span></div>',
        f'<div><strong>全額交割：</strong>{esc("未取得" if full_cash.get("availability") != "available" else "已取得")}<br>'
        f'<span>{esc(full_cash.get("reason", "") if full_cash.get("availability") != "available" else str(full_cash.get("value", "")))}</span></div>',
        "</div></div></div>",
        '<div class="analysis-block">',
        '<div class="block-number">06</div><div><h3>大盤環境</h3>',
        f'<p>截至 7/23，TAIEX 為「{esc(auditable_market_label(market))}」。'
        f'{esc(market.get("market_regime_explanation", "未取得"))} '
        "大盤位於長期均線之上、但短線跌破月線且 20 日報酬為負，意味個股事件交易仍有空間，"
        "但追高容錯率下降；本報告不以不完整的 7/24 日 K 事後美化盤前訊號。</p>",
        "</div></div>",
        '<div class="analysis-block">',
        '<div class="block-number">07</div><div><h3>可交易性與類股</h3>',
        f'<p><strong>{esc(liquidity["grade"])}</strong>。{esc(sector_note)} {esc(liquidity["note"])}</p>',
        '<ul class="check-list">',
        "<li>執行：使用限價、分批，將試撮價量排除於可成交深度估計之外。</li>",
        "<li>風險：以實際成交價、實際成交量與可驗證均線作判斷，不用試撮漲停作停損／目標價。</li>",
        "<li>時點：本報告是 7/24 開盤資訊＋7/23 盤後完整資料的事件分析，不是 7/24 收盤後的完整日 K 評估。</li>",
        "</ul></div></div>",
        '<div class="analysis-block final-block">',
        '<div class="block-number">08</div><div><h3>綜合研判</h3>',
        f'<p class="verdict {verdict["tone"]}">{esc(verdict["title"])}</p>',
        f'<p>{esc(verdict["conclusion"])}</p>',
        '<h4>數據證據鏈</h4><ol class="evidence-list">',
        "".join(f"<li>{esc(item)}</li>" for item in verdict["evidence"]),
        "</ol>",
        f'<div class="decision-box"><strong>交易結論</strong><br>{esc(verdict["action"])}</div>',
        f'<div class="counter-box"><strong>Red team／反例</strong><br>{esc(verdict["counter"])}</div>',
        "</div></div></section>",
    ]
    return "".join(html_out), verdict


def css() -> str:
    return """
    :root{--ink:#172033;--muted:#64748b;--line:#dbe3ec;--paper:#fff;--wash:#f4f7fa;
      --teal:#0f766e;--blue:#1d4ed8;--red:#b91c1c;--amber:#b45309;--green:#047857}
    *{box-sizing:border-box} html{scroll-behavior:smooth} body{margin:0;background:var(--wash);
      color:var(--ink);font-family:"Noto Sans TC","Microsoft JhengHei",Arial,sans-serif;
      font-size:15px;line-height:1.72} .report{max-width:1180px;margin:0 auto;background:var(--paper);
      box-shadow:0 0 32px rgba(15,23,42,.08)} .hero{padding:62px 66px 46px;border-bottom:1px solid var(--line);
      background:linear-gradient(135deg,#fff 0%,#f3faf9 100%)} .eyebrow,.section-kicker{
      color:var(--teal);font-size:12px;letter-spacing:.18em;font-weight:800;text-transform:uppercase}
    h1{font-size:38px;line-height:1.2;margin:12px 0 14px;letter-spacing:-.03em} h2{font-size:31px;
      line-height:1.25;margin:5px 0 15px} h3{font-size:21px;margin:0 0 14px;letter-spacing:-.01em}
    h4{font-size:15px;margin:22px 0 6px} p{margin:7px 0 12px}.subtitle{font-size:18px;color:#475569;
      max-width:880px}.asof{display:inline-block;margin-top:18px;padding:7px 12px;border:1px solid #b7dcd7;
      border-radius:999px;color:#0f766e;background:#f0fdfa;font-size:13px}.nav{padding:14px 66px;
      border-bottom:1px solid var(--line);background:#fff;position:sticky;top:0;z-index:5}
    .nav a{display:inline-block;color:#334155;text-decoration:none;margin-right:24px;font-weight:700}
    .overview,.methodology{padding:36px 66px}.overview{border-bottom:1px solid var(--line)}
    .market-panel{display:grid;grid-template-columns:320px 1fr;gap:26px;align-items:center;margin-top:18px}
    .market-card{background:#f8fafc;border:1px solid var(--line);border-radius:14px;padding:20px}
    .market-value{font-size:30px;font-weight:800;letter-spacing:-.02em}.market-label{color:var(--muted);font-size:13px}
    .summary-table,.data-table{width:100%;border-collapse:collapse}.summary-table{margin-top:24px;font-size:13px}
    th{text-align:left;color:#475569;font-weight:700;background:#f8fafc}.summary-table th,.summary-table td,
      .data-table th,.data-table td{border-bottom:1px solid var(--line);padding:10px 11px;vertical-align:top}
    .summary-table tbody tr:hover{background:#f8fafc}.stock-section{padding:52px 66px 24px;border-top:8px solid var(--wash)}
    .verdict{font-size:17px;font-weight:800;border-left:4px solid #64748b;padding:10px 14px;background:#f8fafc}
    .verdict.danger{border-color:#b91c1c;background:#fef2f2;color:#991b1b}.verdict.warning{
      border-color:#b45309;background:#fffbeb;color:#92400e}.verdict.caution{border-color:#1d4ed8;
      background:#eff6ff;color:#1e40af}.metrics-grid{display:grid;grid-template-columns:repeat(3,1fr);
      gap:10px;margin:24px 0 34px}.metrics-grid.compact{grid-template-columns:repeat(4,1fr);margin:10px 0 18px}
    .metric{border:1px solid var(--line);border-radius:10px;padding:13px 14px;background:#fff}
    .metric-label{font-size:12px;color:var(--muted);font-weight:700}.metric-value{font-size:19px;font-weight:800;
      margin-top:2px}.metric-note{font-size:11px;color:var(--muted);min-height:18px}.analysis-block{
      display:grid;grid-template-columns:44px 1fr;gap:12px;padding:28px 0;border-top:1px solid var(--line)}
    .block-number{font-size:12px;color:#94a3b8;font-weight:800;letter-spacing:.1em}.two-col{display:grid;
      grid-template-columns:1fr 1fr;gap:18px}.callout,.decision-box,.counter-box{border-radius:10px;
      padding:15px 17px;background:#f8fafc;border:1px solid var(--line)}.decision-box{background:#f0fdfa;
      border-color:#99f6e4;margin-top:16px}.counter-box{background:#fffbeb;border-color:#fde68a;margin-top:10px}
    figure{margin:20px 0 4px}.chart,.market-chart{display:block;width:100%;height:auto;border:1px solid var(--line);
      border-radius:10px;background:#fff}.svg-title{font:700 13px "Microsoft JhengHei",Arial;fill:#334155}
    .svg-axis{font:11px "Microsoft JhengHei",Arial;fill:#64748b}.svg-muted{font:13px "Microsoft JhengHei",Arial;
      fill:#94a3b8}.grid{stroke:#e8edf3;stroke-width:1}.zero{stroke:#94a3b8;stroke-width:1.2}
    figcaption,.method-note{font-size:12px;color:var(--muted)}.positive{color:var(--red);font-weight:700}
    .negative{color:var(--green);font-weight:700}.neutral{color:#64748b}.unavailable-grid{display:grid;
      grid-template-columns:1fr 1fr;gap:10px;margin-top:15px}.unavailable-grid>div{padding:12px 14px;
      border:1px dashed #cbd5e1;border-radius:8px;color:#475569}.unavailable-grid span{font-size:12px;color:#64748b}
    .check-list,.evidence-list{padding-left:20px}.check-list li,.evidence-list li{margin:5px 0}
    .final-block{padding-bottom:42px}.methodology{background:#f8fafc;border-top:1px solid var(--line)}
    .method-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.method-card{background:#fff;
      border:1px solid var(--line);border-radius:10px;padding:15px}.footer{padding:28px 66px;color:#64748b;
      font-size:12px;border-top:1px solid var(--line)}.nowrap{white-space:nowrap}
    @media(max-width:800px){.hero,.overview,.methodology,.stock-section,.nav,.footer{padding-left:22px;
      padding-right:22px}.market-panel,.two-col{grid-template-columns:1fr}.metrics-grid,
      .metrics-grid.compact{grid-template-columns:repeat(2,1fr)}.method-grid{grid-template-columns:1fr}
      .summary-scroll{overflow-x:auto}.nav{position:static}.analysis-block{grid-template-columns:30px 1fr}}
    @media print{body{background:#fff}.report{box-shadow:none}.nav{display:none}.stock-section{
      break-before:page;border-top:0}.analysis-block,figure{break-inside:avoid}.hero{padding-top:30px}}
    """


def main() -> None:
    args = parse_args()
    data_dir = args.analysis_dir
    kbars = read_json(data_dir / "kbars.json")
    margin = read_json(data_dir / "margin.json")
    chips = read_json(data_dir / "chips.json")
    auction = read_json(data_dir / "auction_detail.json")
    market = kbars.get("market", {})
    if not isinstance(market, dict):
        raise ValueError("kbars.json missing market object")

    for code in ORDER:
        stock_or_error(kbars, code, "kbars")
        stock_or_error(margin, code, "margin")
        stock_or_error(chips, code, "chips")
        stock_or_error(auction, code, "auction")

    sections = []
    verdicts: dict[str, dict[str, Any]] = {}
    for code in ORDER:
        section, verdict = render_stock(
            code,
            stock_or_error(auction, code, "auction"),
            stock_or_error(kbars, code, "kbars"),
            stock_or_error(margin, code, "margin"),
            stock_or_error(chips, code, "chips"),
            market,
        )
        sections.append(section)
        verdicts[code] = verdict

    market_summary = market.get("summary", {})
    summary_rows = []
    for code in ORDER:
        a = stock_or_error(auction, code, "auction")
        k = stock_or_error(kbars, code, "kbars")
        m = stock_or_error(margin, code, "margin")
        c = stock_or_error(chips, code, "chips")
        ks = k.get("summary", {})
        summary_rows.append(
            "<tr>"
            f'<td class="nowrap"><strong>{code} {esc(a.get("name", NAMES[code]))}</strong></td>'
            f'<td>{fmt_price(a.get("limit_up_price"))} → {fmt_price(a.get("open_price"))}<br>'
            f'<span class="negative">{signed(a.get("auction_to_open_gap_pct"), 2, "%")}</span></td>'
            f'<td>{esc(ks.get("trend", "未取得"))}<br>20日 {signed(ks.get("returns", {}).get("20d_pct"), 2, "%")}</td>'
            f'<td>{fmt_num(ks.get("volume_ratio_vs_20d"), 3)}×<br>{esc(ks.get("volume_signal", "未取得"))}</td>'
            f'<td>{signed(m.get("changes", {}).get("margin_20d", {}).get("pct"), 2, "%")}</td>'
            f'<td>{signed(c.get("summary", {}).get("total", {}).get("last_5d", {}).get("net_lots"), 1)} 張</td>'
            f'<td>{esc(verdicts[code]["title"])}</td></tr>'
        )

    unavailable = [
        "FinMind TaiwanStockPriceAdj：會員資料未取得；已以 FinMind 原始 OHLCV × Yahoo 同日 adjclose/close 因子等價還原，未使用未還原價計算均線。",
        "處置股：FinMind 免費層無權限，三檔均標示未取得。",
        "全額交割股：本次指定資料源無可靠欄位，三檔均標示未取得。",
        "完整類股橫向強弱／類股成交深度：未另抓取；只提供 FinMind 產業分類與個股可交易性。",
    ]
    generated = datetime.now().astimezone().isoformat(timespec="seconds")
    document = f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>2026-07-24 疑似假試撮重點股票完整分析</title>
<style>{css()}</style>
</head>
<body>
<main class="report">
  <header class="hero">
    <div class="eyebrow">PRE-OPEN AUCTION RESEARCH · 2026-07-24</div>
    <h1>疑似假試撮重點股票完整分析</h1>
    <p class="subtitle">只分析盤前偵測出的 3 檔重點股：8039 台虹、2392 正崴、2201 裕隆。以原始試撮價量、還原日 K、量能、融資融券、三大法人與大盤位置建立可稽核的證據鏈。</p>
    <span class="asof">事件日 2026-07-24 · 盤後資料截止 2026-07-23</span>
  </header>
  <nav class="nav" aria-label="報告導覽">
    <a href="#overview">大盤與總覽</a><a href="#stock-8039">8039 台虹</a>
    <a href="#stock-2392">2392 正崴</a><a href="#stock-2201">2201 裕隆</a>
    <a href="#method">方法與限制</a>
  </nav>
  <section class="overview" id="overview">
    <div class="section-kicker">MARKET CONTEXT</div>
    <h2>大盤環境：{esc(auditable_market_label(market))}</h2>
    <div class="market-panel">
      <div class="market-card">
        <div class="market-label">TAIEX 7/23 收盤</div>
        <div class="market-value">{fmt_num(market_summary.get("latest_adjusted_close"), 2)}</div>
        <p>{esc(market.get("market_regime_explanation", "未取得"))}</p>
        <p>20 日報酬 <strong class="{tone_class(market_summary.get("returns", {}).get("20d_pct"))}">{signed(market_summary.get("returns", {}).get("20d_pct"), 2, "%")}</strong>；
        量比 {fmt_num(market_summary.get("volume_ratio_vs_20d"), 3)}。</p>
      </div>
      {market_svg(market)}
    </div>
    <p>大盤仍高於 MA60／MA120／MA240，但低於 MA20，長多與短線修正並存。這種環境不會自動否定個股強勢，卻會放大高檔異常試撮、槓桿堆積與籌碼轉賣的尾部風險。報告不計算缺資料的正式「市場風險分數」，只做可驗證的技術環境判讀。</p>
    <div class="summary-scroll"><table class="summary-table">
      <thead><tr><th>股票</th><th>試撮→開盤</th><th>日K位置</th><th>量比</th><th>融資20日</th><th>法人5日</th><th>綜合研判</th></tr></thead>
      <tbody>{''.join(summary_rows)}</tbody>
    </table></div>
  </section>
  {''.join(sections)}
  <section class="methodology" id="method">
    <div class="section-kicker">METHOD & LIMITATIONS</div>
    <h2>方法、資料血統與未取得項目</h2>
    <div class="method-grid">
      <div class="method-card"><strong>Point-in-time</strong><br>日 K、融資與法人只用至 2026-07-23 的完整盤後資料；2026-07-24 僅使用 recorder 已記錄的盤前試撮與實際開盤，未混入未完成日 K。</div>
      <div class="method-card"><strong>還原權值</strong><br>{esc(kbars.get("methodology", {}).get("stock_adjustment", "未取得"))}</div>
      <div class="method-card"><strong>快取與重跑</strong><br>FinMind／Yahoo 每個 response 分檔快取於 data/analysis/cache；重跑優先讀快取。FinMind 402 採指數退避，不用空資料或猜測值補洞。</div>
      <div class="method-card"><strong>撤單證據</strong><br>以鎖漲停期間買一量高峰至末筆縮減，且其後買一離開漲停價作行為判定；不是委託序號級稽核，也不等同法律結論。</div>
    </div>
    <h3 style="margin-top:24px">明確未取得</h3>
    <ul>{''.join(f'<li>{esc(item)}</li>' for item in unavailable)}</ul>
    <h3 style="margin-top:24px">來源檔</h3>
    <p>本機 recorder：data/result_20260724.json、data/auction_20260724.jsonl。分析輸入：kbars.json、margin.json、chips.json、auction_detail.json。市場資料供應者名稱：FinMind、Yahoo Finance、臺灣證券交易所口徑。</p>
    <p><strong>用途限制：</strong>這是研究與風險辨識報告，不是投資建議。7/24 當日後續成交、公告與盤後籌碼尚不在本報告資訊集內。</p>
  </section>
  <footer class="footer">離線自包含報告 · 無外部腳本、字型、樣式表或圖片連結 · 產生時間 {esc(generated)}</footer>
</main>
</body>
</html>
"""
    args.output.write_text(document, encoding="utf-8")
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")
    print("stocks:", ", ".join(f"{code} {NAMES[code]}" for code in ORDER))


if __name__ == "__main__":
    main()
