#!/usr/bin/env python3
"""Extract auditable pre-open auction details for the three focus stocks.

Inputs are the detector summary and the raw JSONL recorder output.  No network
access is required.  The raw bid-one series is retained for the offline replay
chart, while detector fields are cross-checked rather than silently trusted.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


STOCKS = {"8039": "台虹", "2392": "正崴", "2201": "裕隆"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--result",
        type=Path,
        default=Path("data/result_20260724.json"),
    )
    parser.add_argument(
        "--auction",
        type=Path,
        default=Path("data/auction_20260724.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/analysis/auction_detail.json"),
    )
    return parser.parse_args()


def read_detector_summary(path: Path) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    stocks = {
        str(item.get("code")): item
        for item in payload.get("stocks", [])
        if str(item.get("code")) in STOCKS
    }
    missing = sorted(set(STOCKS) - set(stocks))
    if missing:
        raise ValueError(f"detector summary missing target stocks: {missing}")
    return payload, stocks


def number_at(value: Any, index: int = 0) -> float | int | None:
    if isinstance(value, list):
        value = value[index] if len(value) > index else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    return value


def raw_bid_one_series(path: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int]]:
    series: dict[str, list[dict[str, Any]]] = defaultdict(list)
    raw_counts: dict[str, int] = defaultdict(int)
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            code = str(row.get("code", ""))
            if code not in STOCKS:
                continue
            raw_counts[code] += 1
            # Only pre-open simulated bid/ask observations belong in replay.
            if row.get("kind") != "bidask" or row.get("simtrade") is not True:
                continue
            ts = row.get("ts")
            bid_price = number_at(row.get("bid_price"))
            bid_volume = number_at(row.get("bid_volume"))
            if not isinstance(ts, str) or bid_price is None or bid_volume is None:
                continue
            point = {
                "time": ts,
                "bid_price": float(bid_price),
                "bid_volume_lots": int(bid_volume),
            }
            # The recorder can repeat an identical callback.  Preserve time
            # progression but remove exact duplicate timestamp/value triples.
            if not series[code] or point != series[code][-1]:
                series[code].append(point)

    for code, points in series.items():
        points.sort(key=lambda item: item["time"])
        times = [item["time"] for item in points]
        if times != sorted(times):
            raise AssertionError(f"{code}: raw series is not monotonic")
        if len(times) != len(set(times)):
            # Keep the last callback for duplicate timestamps deterministically.
            by_time = {item["time"]: item for item in points}
            series[code] = [by_time[key] for key in sorted(by_time)]
    return series, raw_counts


def seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def build_stock_detail(
    code: str,
    summary: dict[str, Any],
    raw_series: list[dict[str, Any]],
    raw_count: int,
) -> dict[str, Any]:
    limit_up = float(summary["limit_up"])
    tolerance = max(1e-9, limit_up * 1e-8)
    lock_points = [
        item for item in raw_series if abs(float(item["bid_price"]) - limit_up) <= tolerance
    ]
    lock_start = lock_points[0]["time"] if lock_points else summary.get("first_lock_time")
    lock_end = lock_points[-1]["time"] if lock_points else summary.get("last_lock_time")
    lock_duration = seconds_between(lock_start, lock_end)
    if lock_duration is None:
        lock_duration = summary.get("lock_duration_sec")

    max_lock_volume = max(
        (int(item["bid_volume_lots"]) for item in lock_points),
        default=int(summary.get("max_bid0_volume") or 0),
    )
    last_lock_volume = int(lock_points[-1]["bid_volume_lots"]) if lock_points else None
    withdrawal_lots = (
        max_lock_volume - last_lock_volume
        if last_lock_volume is not None
        else None
    )
    withdrawal_pct = (
        withdrawal_lots / max_lock_volume * 100.0
        if withdrawal_lots is not None and max_lock_volume > 0
        else None
    )
    left_limit_after_lock = any(
        item["time"] > lock_end
        and abs(float(item["bid_price"]) - limit_up) > tolerance
        for item in raw_series
    ) if lock_end else None

    open_price = float(summary["open_price"])
    gap_pct = (open_price / limit_up - 1.0) * 100.0
    detector_gap = float(summary.get("open_gap_pct", gap_pct))
    if abs(gap_pct - detector_gap) > 0.02:
        raise ValueError(
            f"{code}: computed auction/open gap {gap_pct:.4f}% "
            f"does not match detector {detector_gap:.4f}%"
        )

    if withdrawal_pct is None:
        withdrawal_state = "未取得"
        withdrawal_reason = "鎖漲停期間買一量序列不足，無法判定撤單幅度。"
    elif left_limit_after_lock and withdrawal_pct >= 80:
        withdrawal_state = "強烈撤單跡象"
        withdrawal_reason = (
            f"鎖漲停買一量由高峰 {max_lock_volume:,} 張降至 "
            f"{last_lock_volume:,} 張，縮減 {withdrawal_pct:.1f}%，其後買一離開漲停價。"
        )
    elif left_limit_after_lock and withdrawal_pct >= 30:
        withdrawal_state = "明顯撤單/退單跡象"
        withdrawal_reason = (
            f"鎖漲停買一量由高峰 {max_lock_volume:,} 張降至 "
            f"{last_lock_volume:,} 張，縮減 {withdrawal_pct:.1f}%，其後買一離開漲停價。"
        )
    elif left_limit_after_lock:
        withdrawal_state = "價格撤離漲停，量縮證據較弱"
        withdrawal_reason = (
            f"買一其後離開漲停價；鎖漲停買一量高峰至末筆縮減 "
            f"{withdrawal_pct:.1f}%。"
        )
    else:
        withdrawal_state = "未見撤離漲停"
        withdrawal_reason = "原始序列在觀測窗內未見買一離開漲停價。"

    return {
        "code": code,
        "name": summary.get("name") or STOCKS[code],
        "availability": "available",
        "reference_price": summary.get("reference"),
        "limit_up_price": limit_up,
        "simulated_high": summary.get("sim_high"),
        "first_lock_time": lock_start,
        "last_lock_time": lock_end,
        "lock_duration_seconds": round(float(lock_duration), 3) if lock_duration is not None else None,
        "max_limit_bid_volume_lots": max_lock_volume,
        "last_limit_bid_volume_lots": last_lock_volume,
        "withdrawn_from_peak_lots": withdrawal_lots,
        "withdrawn_from_peak_pct": round(withdrawal_pct, 3) if withdrawal_pct is not None else None,
        "left_limit_after_lock": left_limit_after_lock,
        "withdrawal_assessment": withdrawal_state,
        "withdrawal_reason": withdrawal_reason,
        "open_price": open_price,
        "auction_to_open_gap_pct": round(gap_pct, 4),
        "detector_status": summary.get("status"),
        "detector_status_label": summary.get("status_label"),
        "detector_bid0_dropped_flag": summary.get("bid0_dropped"),
        "raw_target_rows": raw_count,
        "replay_points": len(raw_series),
        "bid_one_series": raw_series,
        "quality_notes": [
            "買一序列直接取自 auction_20260724.jsonl 之 simtrade=true bidask 事件。",
            "開盤價與偵測狀態取自 result_20260724.json，並重算試撮高點至開盤落差交叉檢查。",
            "撤單判定使用鎖漲停買一量之高峰到末筆縮減，並要求其後買一離開漲停價；這是行為跡象，不等同交易所委託單級別稽核結論。",
        ],
    }


def main() -> None:
    args = parse_args()
    detector, summaries = read_detector_summary(args.result)
    raw_series, raw_counts = raw_bid_one_series(args.auction)
    stocks = {}
    for code in STOCKS:
        if not raw_series.get(code):
            raise ValueError(f"{code}: no usable raw pre-open bid-one series")
        stocks[code] = build_stock_detail(
            code, summaries[code], raw_series[code], raw_counts.get(code, 0)
        )

    payload = {
        "report_date": detector.get("date", "2026-07-24"),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": {
            "detector_summary": str(args.result).replace("\\", "/"),
            "raw_auction": str(args.auction).replace("\\", "/"),
            "session": detector.get("session"),
            "window": detector.get("window"),
        },
        "availability": "available",
        "stocks": stocks,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"wrote {args.output} ({args.output.stat().st_size:,} bytes)")
    for code, detail in stocks.items():
        print(
            f"{code} {detail['name']}: {detail['limit_up_price']} -> "
            f"{detail['open_price']} ({detail['auction_to_open_gap_pct']:.2f}%), "
            f"lock {detail['lock_duration_seconds']:.1f}s, "
            f"withdraw {detail['withdrawn_from_peak_pct']:.1f}%"
        )


if __name__ == "__main__":
    main()
