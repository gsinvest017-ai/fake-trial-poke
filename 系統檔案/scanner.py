#!/usr/bin/env python
"""將盤前試撮 JSONL 掃描成 dashboard 共用的 result JSON。

本程式只讀本地資料，不登入 Shioaji、不啟用 CA、不選帳號、不下單。
09:00 後的真實 tick 只用來事後標記開盤結果，不會倒灌成盤前訊號。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import jsonl_quality


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_BID0_REMAIN_RATIO = 0.30
DEFAULT_DROP_LOOKBACK_SECONDS = 30 * 60
SNAPSHOT_LIMIT_TOLERANCE_RATIO = 0.001
OPEN_GAP_TOLERANCE_PCT = 0.0001

TOP_LEVEL_FIELDS = {
    "date",
    "session",
    "window",
    "universe_size",
    "subscribed",
    "sub_limit",
    "generated_at",
    "stocks",
}
STOCK_FIELDS = {
    "code",
    "name",
    "reference",
    "limit_up",
    "locked_limit_up",
    "first_lock_time",
    "last_lock_time",
    "lock_duration_sec",
    "sim_high",
    "max_bid0_volume",
    "chg_type_hit_1",
    "open_price",
    "open_gap_pct",
    "bid0_dropped",
    "status",
    "status_label",
    "sim_price_series",
    "bid0_series",
}
STATUS_LABELS = {
    "suspected_fake": "疑似假試撮",
    "locked_held": "鎖漲停守住",
    "touched": "曾觸漲停",
    "none": "未觸及",
}


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def to_positive_float(value: Any) -> float | None:
    number = to_float(value)
    return number if number is not None and number > 0 else None


def to_int(value: Any) -> int | None:
    number = to_float(value)
    return None if number is None else int(number)


def first_item(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return None


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(TAIPEI).replace(tzinfo=None)
    return parsed


def iso_seconds(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def same_price(left: Any, right: Any) -> bool:
    a = to_float(left)
    b = to_float(right)
    if a is None or b is None:
        return False
    return math.isclose(a, b, rel_tol=0.0, abs_tol=1e-7)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    nonempty_lines = 0
    bad_lines = 0
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            nonempty_lines += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                bad_lines += 1
                jsonl_quality.warn_bad_line(
                    path,
                    line_number,
                    "不是合法 JSON",
                )
                continue
            if not isinstance(item, dict):
                bad_lines += 1
                jsonl_quality.warn_bad_line(
                    path,
                    line_number,
                    "不是 JSON object",
                )
                continue
            if item.get("kind") not in {"tick", "bidask", "snapshot"}:
                bad_lines += 1
                jsonl_quality.warn_bad_line(
                    path,
                    line_number,
                    "kind 不是 tick/bidask/snapshot",
                )
                continue
            code = str(item.get("code") or "").strip()
            if not code:
                bad_lines += 1
                jsonl_quality.warn_bad_line(
                    path,
                    line_number,
                    "缺少 code",
                )
                continue
            item["_dt"] = parse_ts(item.get("ts"))
            if item["_dt"] is None:
                bad_lines += 1
                jsonl_quality.warn_bad_line(
                    path,
                    line_number,
                    "ts 不是合法 ISO 時間",
                )
                continue
            item["code"] = code
            events.append(item)
    jsonl_quality.enforce_quality(
        path,
        nonempty_lines=nonempty_lines,
        bad_lines=bad_lines,
    )
    return events


def normalize_date(value: Any, fallback: datetime | None = None) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return (fallback or datetime.now()).strftime("%Y-%m-%d")


def history_directory(date_value: Any = None) -> Path:
    normalized = normalize_date(date_value, datetime.now(TAIPEI))
    return HISTORY_DIR / normalized.replace("-", "")


def default_input_path(date_value: Any = None) -> Path:
    directory = history_directory(date_value)
    date_key = directory.name
    return directory / f"auction_{date_key}.jsonl"


def default_output_path(date_value: Any = None) -> Path:
    directory = history_directory(date_value)
    return directory / f"result_{directory.name}.json"


def infer_date_from_filename(path: Path) -> str | None:
    match = re.search(r"(\d{8})", path.stem)
    return normalize_date(match.group(1)) if match else None


def infer_session(events: Iterable[dict[str, Any]]) -> str:
    hours = [event["_dt"].hour for event in events if event.get("_dt")]
    return "preclose" if hours and min(hours) >= 12 else "preopen"


def default_window(date_text: str, session: str) -> dict[str, str]:
    if session == "preclose":
        return {"start": f"{date_text}T13:25:00", "end": f"{date_text}T13:30:00"}
    return {"start": f"{date_text}T08:30:00", "end": f"{date_text}T09:00:00"}


def parse_window_endpoint(value: Any, session_date: datetime) -> datetime | None:
    parsed = parse_ts(value)
    if parsed is not None:
        return parsed
    text = str(value or "").strip()
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            clock = datetime.strptime(text, pattern).time()
        except ValueError:
            continue
        return datetime.combine(session_date.date(), clock)
    return None


def session_open_at(session_date: datetime, session: str) -> datetime:
    clock = time(13, 30) if session == "preclose" else time(9, 0)
    return datetime.combine(session_date.date(), clock)


def load_metadata(input_path: Path) -> dict[str, Any]:
    sidecar = input_path.with_suffix(".meta.json")
    if not sidecar.exists():
        return {}
    payload = read_json(sidecar)
    if not isinstance(payload, dict):
        raise ValueError(f"{sidecar.name} 必須是 JSON object")
    return payload


def metadata_by_code(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    stocks = metadata.get("stocks", [])
    if not isinstance(stocks, list):
        return result
    for item in stocks:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if code:
            result[code] = item
    return result


def select_result_codes(
    event_groups: dict[str, list[dict[str, Any]]],
    stock_metadata: dict[str, dict[str, Any]],
    metadata: dict[str, Any],
) -> list[str]:
    """只輸出實際納入監控的標的，避免把 dropped universe 當成未觸及。

    recorder 若提供 subscribed_codes，零 callback 的已訂閱標的仍保留；
    沒有這份清單時，有串流事件就以實收 callback codes 為準。snapshot
    僅是開盤證據，不得把未訂閱標的偽裝成 status=none。唯有完全沒有
    串流事件且無 subscribed_codes 時，才以 metadata stocks 保留空結果。
    """
    stream_codes = {
        code
        for code, events in event_groups.items()
        if any(event.get("kind") in {"tick", "bidask"} for event in events)
    }
    raw_subscribed_codes = metadata.get("subscribed_codes")
    if isinstance(raw_subscribed_codes, list):
        subscribed_codes = {
            str(code).strip() for code in raw_subscribed_codes if str(code).strip()
        }
        return sorted(stream_codes | subscribed_codes)
    if stream_codes:
        return sorted(stream_codes)
    if event_groups:
        return []
    return sorted(stock_metadata)


def assert_coverage_selection() -> None:
    """Red-team：dropped 不得偽裝成 status=none，零 callback 訂閱仍保留。"""
    event_groups = {
        "1101": [{"code": "1101", "kind": "bidask"}],
        "9999": [{"code": "9999", "kind": "snapshot"}],
    }
    stock_metadata = {"1101": {}, "1102": {}, "9999": {}}
    metadata = {"subscribed_codes": ["1101", "1102"]}
    selected = select_result_codes(event_groups, stock_metadata, metadata)
    assert selected == ["1101", "1102"]
    assert "9999" not in selected
    assert select_result_codes(event_groups, stock_metadata, {}) == ["1101"]
    assert select_result_codes(
        {"9999": [{"code": "9999", "kind": "snapshot"}]},
        stock_metadata,
        {},
    ) == []
    assert select_result_codes({}, stock_metadata, {}) == [
        "1101",
        "1102",
        "9999",
    ]


def load_snapshot_prices(input_path: Path) -> dict[str, float]:
    """讀取唯讀 snapshot sidecar；不為補開盤價而登入券商。

    支援：
      {"2330": 1045}
      {"stocks": [{"code": "2330", "open_price": 1045}]}
      [{"code": "2330", "open_price": 1045}]
    """
    path = input_path.with_suffix(".snapshot.json")
    if not path.exists():
        return {}
    payload = read_json(path)
    if isinstance(payload, dict) and isinstance(payload.get("stocks"), list):
        payload = payload["stocks"]
    prices: dict[str, float] = {}
    if isinstance(payload, dict):
        for code, value in payload.items():
            price = to_positive_float(value)
            if price is not None:
                prices[str(code)] = price
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            price = to_positive_float(item.get("open_price", item.get("open")))
            if code and price is not None:
                prices[code] = price
    return prices


def first_known(
    stock_meta: dict[str, Any],
    events: list[dict[str, Any]],
    field: str,
) -> Any:
    if stock_meta.get(field) is not None:
        return stock_meta[field]
    for event in events:
        if event.get(field) is not None:
            return event[field]
    return None


def infer_limit_up(
    stock_meta: dict[str, Any],
    events: list[dict[str, Any]],
) -> float | None:
    explicit = to_float(first_known(stock_meta, events, "limit_up"))
    if explicit is not None:
        return explicit if explicit > 0 else None
    # chg_type==1 是行情源直接給出的漲停旗標，可安全用該筆試撮價
    # 還原 limit_up；沒有直接證據時維持 null，不用 10% 反推偽造。
    candidates = [
        to_float(event.get("price"))
        for event in events
        if event.get("kind") == "tick"
        and bool(event.get("simtrade"))
        and to_int(event.get("chg_type")) == 1
    ]
    return next(
        (price for price in candidates if price is not None and price > 0),
        None,
    )


def opening_tick_price(
    events: list[dict[str, Any]],
    session_date: datetime,
    grace_seconds: int,
) -> float | None:
    start = datetime.combine(session_date.date(), time(9, 0))
    end = start + timedelta(seconds=grace_seconds)
    candidates = sorted(
        (
            event
            for event in events
            if event.get("kind") == "tick"
            and event.get("simtrade") is False
            and start <= event["_dt"] <= end
            and to_float(event.get("price")) is not None
        ),
        key=lambda event: event["_dt"],
    )
    return to_float(candidates[0]["price"]) if candidates else None


def latest_snapshot(
    events: list[dict[str, Any]],
    not_before: datetime,
    legacy_observed_at: datetime | None,
) -> dict[str, Any] | None:
    snapshots: list[tuple[datetime, datetime, dict[str, Any]]] = []
    for event in events:
        if event.get("kind") != "snapshot":
            continue
        evidence_at = event["_dt"]
        # 舊 recorder 把行情本身的 stale ts 寫成 snapshot ts；僅在 sidecar
        # 明確證明整批 snapshot 於開盤後完成時，以 generated_at 相容舊檔。
        if (
            evidence_at < not_before
            and legacy_observed_at is not None
            and legacy_observed_at >= not_before
        ):
            evidence_at = legacy_observed_at
        if evidence_at >= not_before:
            snapshots.append((evidence_at, event["_dt"], event))
    return max(snapshots, key=lambda item: (item[0], item[1]), default=(None, None, None))[2]


def snapshot_limit_status(
    snapshot: dict[str, Any] | None,
    limit_up: float | None,
) -> str | None:
    """用 snapshot 最佳買賣價判斷無成交價時是否仍貼近漲停。"""
    if snapshot is None or limit_up is None or limit_up <= 0:
        return None
    quotes = [
        to_float(first_item(snapshot.get(field)))
        for field in ("bid_price", "ask_price")
    ]
    valid_quotes = [price for price in quotes if price is not None and price > 0]
    if not valid_quotes:
        return None
    held_threshold = limit_up * (1.0 - SNAPSHOT_LIMIT_TOLERANCE_RATIO)
    if any(price >= held_threshold for price in valid_quotes):
        return "locked_held"
    return "suspected_fake"


def bid0_drop_evidence(
    events: list[dict[str, Any]],
    snapshot: dict[str, Any] | None,
    session_date: datetime,
    *,
    drop_ratio: float,
    min_preopen: int,
    min_absolute: int,
    lookback_seconds: int,
) -> bool:
    """比較盤前末筆與開盤後 snapshot 的買一量，判斷是否明顯撤單。"""
    if snapshot is None:
        return False
    cutoff = datetime.combine(session_date.date(), time(9, 0))
    window_start = cutoff - timedelta(seconds=lookback_seconds)
    observations: list[tuple[datetime, int]] = []
    for event in events:
        if (
            event.get("kind") != "bidask"
            or event.get("simtrade") is not True
            or not window_start <= event["_dt"] < cutoff
        ):
            continue
        bid_volume = max(0, to_int(first_item(event.get("bid_volume"))) or 0)
        observations.append((event["_dt"], bid_volume))
    observations.sort(key=lambda item: item[0])
    if not observations:
        return False
    preopen_final = observations[-1][1]
    raw_snapshot_volume = to_int(first_item(snapshot.get("bid_volume")))
    if raw_snapshot_volume is None:
        return False
    snapshot_volume = max(0, raw_snapshot_volume)
    if preopen_final <= 0:
        return False
    absolute_drop = preopen_final - snapshot_volume
    remain_ratio = 1.0 - drop_ratio
    actual_remain_ratio = snapshot_volume / preopen_final
    return (
        preopen_final >= min_preopen
        and absolute_drop >= min_absolute
        and actual_remain_ratio < remain_ratio
        and not math.isclose(
            actual_remain_ratio,
            remain_ratio,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    )


def stock_record(
    code: str,
    stock_meta: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    session: str,
    session_date: datetime,
    window_start: datetime,
    window_end: datetime,
    opening_at: datetime,
    legacy_snapshot_at: datetime | None,
    snapshot_prices: dict[str, float],
    open_source: str,
    open_grace_seconds: int,
    drop_ratio: float,
    drop_min_peak: int,
    drop_min_absolute: int,
    drop_lookback_seconds: int,
) -> dict[str, Any]:
    events.sort(key=lambda event: event["_dt"])
    name = str(first_known(stock_meta, events, "name") or code)
    reference = to_float(first_known(stock_meta, events, "reference"))
    limit_up = infer_limit_up(stock_meta, events)
    snapshot = latest_snapshot(events, opening_at, legacy_snapshot_at)

    bidask_lock_times: list[datetime] = []
    tick_lock_times: list[datetime] = []
    has_simtrade_bidask = False
    sim_prices: list[float] = []
    bid0_volumes: list[int] = []
    chg_type_hit_1 = False
    sim_price_series: list[dict[str, Any]] = []
    bid0_series: list[dict[str, Any]] = []

    for event in events:
        simtrade = event.get("simtrade") is True
        in_session_window = window_start <= event["_dt"] < window_end
        preopen_simtrade = simtrade and in_session_window
        if event.get("kind") == "tick":
            price = to_float(event.get("price"))
            chg_type_one = (
                preopen_simtrade and to_int(event.get("chg_type")) == 1
            )
            chg_type_hit_1 = chg_type_hit_1 or chg_type_one
            if price is not None:
                sim_price_series.append(
                    {
                        "t": iso_seconds(event["_dt"]),
                        "price": price,
                        "simtrade": simtrade,
                    }
                )
                if preopen_simtrade:
                    sim_prices.append(price)
            if preopen_simtrade and (
                chg_type_one or same_price(price, limit_up)
            ):
                tick_lock_times.append(event["_dt"])
        elif event.get("kind") == "bidask":
            bid_price = to_float(first_item(event.get("bid_price")))
            bid_volume = max(0, to_int(first_item(event.get("bid_volume"))) or 0)
            bid0_series.append(
                {
                    "t": iso_seconds(event["_dt"]),
                    "bid0_price": bid_price,
                    "bid0_volume": bid_volume,
                }
            )
            if preopen_simtrade:
                has_simtrade_bidask = True
                bid0_volumes.append(bid_volume)
                if same_price(bid_price, limit_up):
                    bidask_lock_times.append(event["_dt"])

    # 有 BidAsk 試撮串流時，鎖漲停完全由方法 B 決定；僅舊資料沒有
    # BidAsk 試撮時，才保留 tick/chg_type 的相容 fallback。
    lock_times = (
        bidask_lock_times if has_simtrade_bidask else tick_lock_times
    )
    locked_limit_up = bool(lock_times)
    first_lock = min(lock_times) if lock_times else None
    last_lock = max(lock_times) if lock_times else None
    duration = (
        round((last_lock - first_lock).total_seconds(), 3)
        if first_lock is not None and last_lock is not None
        else 0.0
    )
    bid0_prices = [
        point["bid0_price"]
        for point in bid0_series
        if point["bid0_price"] is not None
    ]

    open_price: float | None = None
    if session == "preopen":
        if open_source == "auto" and snapshot is not None:
            # snapshot 記錄存在但 open_price=null 代表仍無開盤價，不倒退
            # 採用 tick，避免把不同時間來源混為同一份開盤證據。
            open_price = to_positive_float(snapshot.get("open_price"))
        elif open_source == "auto":
            open_price = opening_tick_price(
                events, session_date, open_grace_seconds
            )
            if open_price is None:
                open_price = snapshot_prices.get(code)
        elif open_source == "snapshot":
            open_price = (
                to_positive_float(snapshot.get("open_price"))
                if snapshot is not None
                else snapshot_prices.get(code)
            )
        elif open_source == "tick":
            open_price = opening_tick_price(
                events, session_date, open_grace_seconds
            )
        open_price = to_positive_float(open_price)

    open_gap_pct = (
        round((open_price / limit_up - 1.0) * 100.0, 4)
        if open_price is not None and limit_up is not None and limit_up > 0
        else None
    )
    bid0_dropped = (
        bid0_drop_evidence(
            events,
            snapshot,
            session_date,
            drop_ratio=drop_ratio,
            min_preopen=drop_min_peak,
            min_absolute=drop_min_absolute,
            lookback_seconds=drop_lookback_seconds,
        )
        if session == "preopen"
        else False
    )
    snapshot_status = (
        snapshot_limit_status(snapshot, limit_up)
        if open_price is None
        else None
    )

    gap_below_tolerance = (
        open_gap_pct is not None
        and open_gap_pct < -OPEN_GAP_TOLERANCE_PCT
    )
    if locked_limit_up and (gap_below_tolerance or bid0_dropped):
        status = "suspected_fake"
    elif locked_limit_up and open_gap_pct is not None:
        status = "locked_held"
    elif locked_limit_up and snapshot_status is not None:
        status = snapshot_status
    elif locked_limit_up:
        # 沒有 09:00 後證據時不宣稱「守住」，保守停在曾觸漲停。
        status = "touched"
    else:
        status = "none"

    return {
        "code": code,
        "name": name,
        "reference": reference,
        "limit_up": limit_up,
        "locked_limit_up": locked_limit_up,
        "first_lock_time": iso_seconds(first_lock) if first_lock else None,
        "last_lock_time": iso_seconds(last_lock) if last_lock else None,
        "lock_duration_sec": duration,
        "sim_high": max(sim_prices or bid0_prices, default=0.0),
        "max_bid0_volume": max(bid0_volumes) if bid0_volumes else 0,
        "chg_type_hit_1": chg_type_hit_1,
        "open_price": open_price,
        "open_gap_pct": open_gap_pct,
        "bid0_dropped": bid0_dropped,
        "status": status,
        "status_label": STATUS_LABELS[status],
        "sim_price_series": sim_price_series,
        "bid0_series": bid0_series,
    }


def build_result(
    events: list[dict[str, Any]],
    metadata: dict[str, Any],
    *,
    input_path: Path,
    open_source: str,
    open_grace_seconds: int,
    drop_ratio: float,
    drop_min_peak: int,
    drop_min_absolute: int,
    drop_lookback_seconds: int,
) -> dict[str, Any]:
    first_dt = min(
        (event["_dt"] for event in events),
        default=datetime.now().replace(tzinfo=None),
    )
    date_text = normalize_date(
        metadata.get("date") or infer_date_from_filename(input_path),
        first_dt,
    )
    session = str(metadata.get("session") or infer_session(events))
    if session not in {"preopen", "preclose"}:
        raise ValueError("session 必須為 preopen 或 preclose")
    try:
        session_date = datetime.strptime(date_text, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("date 必須為 YYYY-MM-DD 或 YYYYMMDD") from exc

    raw_window = metadata.get("window")
    window = (
        {
            "start": str(raw_window.get("start")),
            "end": str(raw_window.get("end")),
        }
        if isinstance(raw_window, dict)
        and raw_window.get("start")
        and raw_window.get("end")
        else default_window(date_text, session)
    )
    window_start = parse_window_endpoint(window["start"], session_date)
    window_end = parse_window_endpoint(window["end"], session_date)
    if window_start is None or window_end is None:
        raise ValueError("window start/end 必須為 ISO 時間或 HH:MM[:SS]")
    if window_end <= window_start:
        window_end += timedelta(days=1)
    opening_at = session_open_at(session_date, session)
    legacy_snapshot_at = None
    if (to_int(metadata.get("snapshot_count")) or 0) > 0:
        legacy_snapshot_at = parse_ts(metadata.get("generated_at"))

    event_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        event_groups[event["code"]].append(event)
    stock_metadata = metadata_by_code(metadata)
    codes = select_result_codes(event_groups, stock_metadata, metadata)
    snapshot_prices = (
        load_snapshot_prices(input_path)
        if open_source in {"auto", "snapshot"}
        else {}
    )
    stocks = [
        stock_record(
            code,
            stock_metadata.get(code, {}),
            event_groups.get(code, []),
            session=session,
            session_date=session_date,
            window_start=window_start,
            window_end=window_end,
            opening_at=opening_at,
            legacy_snapshot_at=legacy_snapshot_at,
            snapshot_prices=snapshot_prices,
            open_source=open_source,
            open_grace_seconds=open_grace_seconds,
            drop_ratio=drop_ratio,
            drop_min_peak=drop_min_peak,
            drop_min_absolute=drop_min_absolute,
            drop_lookback_seconds=drop_lookback_seconds,
        )
        for code in codes
    ]
    status_rank = {
        "suspected_fake": 0,
        "locked_held": 1,
        "touched": 2,
        "none": 3,
    }
    stocks.sort(
        key=lambda stock: (
            status_rank[stock["status"]],
            stock["open_gap_pct"] is None,
            stock["open_gap_pct"] or 0.0,
            stock["code"],
        )
    )

    subscribed_codes = {event["code"] for event in events}
    result = {
        "date": date_text,
        "session": session,
        "window": window,
        "universe_size": to_int(metadata.get("universe_size"))
        or len(stock_metadata)
        or len(codes),
        "subscribed": to_int(metadata.get("subscribed"))
        or len(subscribed_codes),
        # 沒有 recorder 實測 sidecar 時維持 null，避免把觀察檔數偽稱配額。
        "sub_limit": to_int(metadata.get("sub_limit")),
        "generated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
        "stocks": stocks,
    }
    return result


def validate_result(result: dict[str, Any], *, sample: bool = False) -> None:
    if set(result) != TOP_LEVEL_FIELDS:
        raise AssertionError(
            f"result 頂層欄位不符：{sorted(set(result) ^ TOP_LEVEL_FIELDS)}"
        )
    if result["session"] not in {"preopen", "preclose"}:
        raise AssertionError("session enum 不合法")
    if set(result["window"]) != {"start", "end"}:
        raise AssertionError("window 欄位不合法")
    if not isinstance(result["stocks"], list):
        raise AssertionError("stocks 必須為陣列")
    for stock in result["stocks"]:
        if set(stock) != STOCK_FIELDS:
            raise AssertionError(
                f"{stock.get('code')} 欄位不符："
                f"{sorted(set(stock) ^ STOCK_FIELDS)}"
            )
        if stock["status"] not in STATUS_LABELS:
            raise AssertionError(f"{stock['code']} status enum 不合法")
        if stock["status_label"] != STATUS_LABELS[stock["status"]]:
            raise AssertionError(f"{stock['code']} status_label 不一致")
        if not isinstance(stock["locked_limit_up"], bool):
            raise AssertionError(f"{stock['code']} locked_limit_up 非 bool")
        if not isinstance(stock["bid0_dropped"], bool):
            raise AssertionError(f"{stock['code']} bid0_dropped 非 bool")
        for point in stock["sim_price_series"]:
            if set(point) != {"t", "price", "simtrade"}:
                raise AssertionError(f"{stock['code']} 試撮價 series 欄位不符")
        for point in stock["bid0_series"]:
            if set(point) != {"t", "bid0_price", "bid0_volume"}:
                raise AssertionError(f"{stock['code']} bid0 series 欄位不符")
    if sample:
        statuses = {stock["status"] for stock in result["stocks"]}
        if statuses != set(STATUS_LABELS):
            raise AssertionError(f"sample 四狀態不齊：{sorted(statuses)}")
        fake_count = sum(
            stock["status"] == "suspected_fake" for stock in result["stocks"]
        )
        if fake_count < 2:
            raise AssertionError("sample 至少需要 2 檔 suspected_fake")
        if not any(
            stock["status"] == "locked_held" for stock in result["stocks"]
        ):
            raise AssertionError("sample 至少需要 1 檔 locked_held")
        if not all(
            stock["sim_price_series"] and stock["bid0_series"]
            for stock in result["stocks"]
        ):
            raise AssertionError("sample 每檔都必須有可畫的兩條 series")
        by_code = {stock["code"]: stock for stock in result["stocks"]}
        if by_code["2330"]["open_gap_pct"] != -7.0:
            raise AssertionError("sample 未優先採用 inline snapshot 開盤價")
        if by_code["2330"]["bid0_dropped"]:
            raise AssertionError("sample 保留大單案例不應誤判撤單")
        if not by_code["2317"]["bid0_dropped"]:
            raise AssertionError("sample 開盤後買一歸零案例應判定撤單")
        if by_code["2454"]["max_bid0_volume"] != 59000:
            raise AssertionError("sample max_bid0_volume 必須只來自 BidAsk 串流")
        if any(
            point["t"].endswith("09:00:03")
            for stock in result["stocks"]
            for point in stock["bid0_series"]
        ):
            raise AssertionError("snapshot 不得混入 bid0_series")


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def sample_events(date_text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """建立完全離線的擬真合成樣本；資料不代表任何真實交易日。"""
    specs = [
        ("2330", "台積電", 1000.0, 1100.0, "fake_gap"),
        ("2317", "鴻海", 220.0, 242.0, "fake_drop"),
        ("2454", "聯發科", 1500.0, 1650.0, "held"),
        ("2308", "台達電", 900.0, 990.0, "fallback_held"),
        ("2603", "長榮", 210.0, 231.0, "touched"),
        ("2382", "廣達", 300.0, 330.0, "none"),
        ("3231", "緯創", 120.0, 132.0, "none"),
        ("2881", "富邦金", 90.0, 99.0, "none"),
    ]
    day = datetime.strptime(date_text, "%Y-%m-%d")
    groups: list[dict[str, Any]] = []

    def add_tick(
        code: str,
        hhmmss: str,
        price: float,
        simtrade: bool,
        chg_type: int,
    ) -> None:
        groups.append(
            {
                "code": code,
                "name": next(spec[1] for spec in specs if spec[0] == code),
                "ts": f"{date_text}T{hhmmss}",
                "kind": "tick",
                "simtrade": simtrade,
                "price": price,
                "chg_type": chg_type,
                "bid_price": [None] * 5,
                "bid_volume": [None] * 5,
                "ask_price": [None] * 5,
                "ask_volume": [None] * 5,
                "volume": 0 if simtrade else 1,
            }
        )

    def add_bid(
        code: str,
        hhmmss: str,
        bid_price: float,
        bid_volume: int,
        ask_price: float | None,
    ) -> None:
        groups.append(
            {
                "code": code,
                "name": next(spec[1] for spec in specs if spec[0] == code),
                "ts": f"{date_text}T{hhmmss}",
                "kind": "bidask",
                "simtrade": True,
                "price": None,
                "chg_type": None,
                "bid_price": [bid_price, None, None, None, None],
                "bid_volume": [bid_volume, 0, 0, 0, 0],
                "ask_price": [ask_price, None, None, None, None],
                "ask_volume": [300 if ask_price else 0, 0, 0, 0, 0],
                "volume": None,
            }
        )

    def add_snapshot(
        code: str,
        open_price: float | None,
        bid_price: float | None,
        bid_volume: int,
    ) -> None:
        groups.append(
            {
                "code": code,
                "name": next(spec[1] for spec in specs if spec[0] == code),
                "ts": f"{date_text}T09:00:03",
                "kind": "snapshot",
                "open_price": open_price,
                "bid_price": [bid_price, None, None, None, None],
                "bid_volume": [bid_volume, 0, 0, 0, 0],
                "ask_price": [None] * 5,
                "ask_volume": [0] * 5,
            }
        )

    for code, _name, reference, limit_up, scenario in specs:
        early = round(reference * 1.02, 2)
        middle = round(reference * 1.06, 2)
        if scenario == "fake_gap":
            prices = [early, middle, limit_up, limit_up, limit_up]
            # 保持漲停買單，讓此案例只因開盤價跌離漲停而列可疑。
            bids = [1200, 9000, 48000, 126000, 98000]
            bid_prices = [early, middle, limit_up, limit_up, limit_up]
        elif scenario == "fake_drop":
            prices = [early, middle, limit_up, limit_up, limit_up]
            bids = [1200, 9000, 48000, 126000, 2800]
            bid_prices = [early, middle, limit_up, limit_up, reference * 1.04]
        elif scenario == "held":
            prices = [middle, limit_up, limit_up, limit_up, limit_up]
            bids = [2400, 12000, 35000, 51000, 59000]
            bid_prices = [middle, limit_up, limit_up, limit_up, limit_up]
        elif scenario in {"fallback_held", "touched"}:
            prices = [early, middle, limit_up, limit_up, limit_up]
            bids = [600, 1400, 4600, 6200, 5900]
            bid_prices = [early, middle, limit_up, limit_up, limit_up]
        else:
            prices = [
                reference,
                round(reference * 1.01, 2),
                round(reference * 1.025, 2),
                round(reference * 1.035, 2),
                round(reference * 1.03, 2),
            ]
            bids = [420, 760, 1100, 940, 880]
            bid_prices = prices

        times = ["08:42:00", "08:50:00", "08:56:00", "08:58:30", "08:59:45"]
        for index, hhmmss in enumerate(times):
            price = float(prices[index])
            hit = price == limit_up and scenario != "none"
            add_tick(code, hhmmss, price, True, 1 if hit else 2)
            add_bid(
                code,
                hhmmss,
                float(bid_prices[index]),
                bids[index],
                None if hit else round(float(bid_prices[index]) + 0.5, 2),
            )

        if scenario == "fake_gap":
            add_tick(code, "09:00:01", round(limit_up * 0.95, 2), False, 4)
            # 與 opening tick 刻意不同，用來驗證 inline snapshot 優先。
            add_snapshot(code, round(limit_up * 0.93, 2), limit_up, 98000)
        elif scenario == "fake_drop":
            # 即使開盤價仍在漲停，開盤前大單驟撤也按 OR 準則列可疑。
            add_tick(code, "09:00:01", limit_up, False, 1)
            add_snapshot(code, limit_up, reference * 1.02, 0)
        elif scenario == "held":
            add_tick(code, "09:00:01", limit_up, False, 1)
            add_snapshot(code, limit_up, limit_up, 60000)
        elif scenario == "fallback_held":
            # 沒有成交價，但 snapshot 買一仍鎖漲停，應 fallback 為守住。
            add_snapshot(code, None, limit_up, bids[-1])
        elif scenario == "touched":
            # snapshot 完全沒有成交／買賣價，才保守維持 touched。
            add_snapshot(code, None, None, bids[-1])
        elif scenario == "none":
            add_tick(code, "09:00:01", float(prices[-1]), False, 2)
            add_snapshot(
                code,
                float(prices[-1]),
                float(bid_prices[-1]),
                bids[-1],
            )

    for event in groups:
        event["_dt"] = parse_ts(event["ts"])
    metadata = {
        "date": date_text,
        "session": "preopen",
        "window": {
            "start": f"{date_text}T08:30:00",
            "end": f"{date_text}T09:00:00",
        },
        "universe_size": 268,
        "subscribed": len(specs),
        "sub_limit": 100,
        "generated_at": iso_seconds(day),
        "stocks": [
            {
                "code": code,
                "name": name,
                "reference": reference,
                "limit_up": limit_up,
            }
            for code, name, reference, limit_up, _scenario in specs
        ],
    }
    return groups, metadata


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="掃描試撮鎖漲停與開盤撤單，輸出 dashboard result JSON"
    )
    parser.add_argument(
        "--in",
        dest="input_path",
        type=Path,
        help=(
            "auction JSONL；預設 "
            "data/history/今日日期/auction_今日日期.jsonl"
        ),
    )
    parser.add_argument(
        "--out",
        dest="output_path",
        type=Path,
        help=(
            "result JSON；預設依輸入資料日期寫入 "
            "data/history/YYYYMMDD/result_YYYYMMDD.json"
        ),
    )
    parser.add_argument(
        "--open-source",
        choices=("auto", "tick", "snapshot", "none"),
        default="auto",
        help=(
            "開盤價來源：auto=優先 JSONL inline snapshot；完全沒有該記錄時 "
            "才依序回退 09:00 tick、snapshot sidecar"
        ),
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="不登入，產生含四狀態的合成 data/result_sample.json",
    )
    parser.add_argument(
        "--open-grace-sec",
        type=int,
        default=300,
        help="真實開盤 tick 最晚容許秒數（預設 300）",
    )
    parser.add_argument(
        "--drop-ratio",
        type=float,
        default=1.0 - DEFAULT_BID0_REMAIN_RATIO,
        help="snapshot 買一量相對盤前末筆減少比例門檻（預設 0.70）",
    )
    parser.add_argument(
        "--drop-min-peak",
        type=int,
        default=1000,
        help="啟用撤單判定的盤前末筆買一最低張數（預設 1000）",
    )
    parser.add_argument(
        "--drop-min-absolute",
        type=int,
        default=500,
        help="買一絕對減少張數門檻（預設 500）",
    )
    parser.add_argument(
        "--drop-lookback-sec",
        type=int,
        default=DEFAULT_DROP_LOOKBACK_SECONDS,
        help="尋找盤前末筆買一的回看秒數（預設 1800）",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if args.sample and args.input_path is not None:
        raise ValueError("--sample 不需且不可搭配 --in")
    if args.open_grace_sec < 0:
        raise ValueError("--open-grace-sec 不可為負數")
    if not 0.0 < args.drop_ratio <= 1.0:
        raise ValueError("--drop-ratio 必須介於 0（不含）與 1")
    if args.drop_min_peak < 0 or args.drop_min_absolute < 0:
        raise ValueError("撤單張數門檻不可為負數")
    if args.drop_lookback_sec <= 0:
        raise ValueError("--drop-lookback-sec 必須大於 0")


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = parse_args(argv)
    try:
        validate_args(args)
        assert_coverage_selection()
        if args.sample:
            date_text = datetime.now(TAIPEI).strftime("%Y-%m-%d")
            events, metadata = sample_events(date_text)
            input_path = DATA_DIR / (
                f"auction_{date_text.replace('-', '')}_sample.jsonl"
            )
            output_path = args.output_path or DATA_DIR / "result_sample.json"
        else:
            input_path = (args.input_path or default_input_path()).resolve()
            if not input_path.is_file():
                raise FileNotFoundError(input_path)
            events = read_jsonl(input_path)
            metadata = load_metadata(input_path)
            inferred_date = (
                metadata.get("date")
                or infer_date_from_filename(input_path)
                or datetime.now(TAIPEI).strftime("%Y-%m-%d")
            )
            output_path = args.output_path or default_output_path(inferred_date)

        result = build_result(
            events,
            metadata,
            input_path=input_path,
            open_source=args.open_source,
            open_grace_seconds=args.open_grace_sec,
            drop_ratio=args.drop_ratio,
            drop_min_peak=args.drop_min_peak,
            drop_min_absolute=args.drop_min_absolute,
            drop_lookback_seconds=args.drop_lookback_sec,
        )
        if (
            not args.sample
            and (
                not input_path.with_suffix(".meta.json").is_file()
                or not metadata_by_code(metadata)
            )
            and all(stock.get("limit_up") is None for stock in result["stocks"])
        ):
            raise ValueError(
                "缺少 meta.json，且全體 limit_up 不可得、判定不可信"
            )
        validate_result(result, sample=args.sample)
        write_result(output_path, result)
    except (OSError, ValueError, AssertionError, json.JSONDecodeError) as exc:
        print(f"scanner FAILED：{exc}", file=sys.stderr)
        return 2

    counts = {
        status: sum(stock["status"] == status for stock in result["stocks"])
        for status in STATUS_LABELS
    }
    print(
        f"scanner OK：{output_path.resolve()}；stocks={len(result['stocks'])}；"
        f"疑似假試撮={counts['suspected_fake']}；"
        f"鎖漲停守住={counts['locked_held']}；"
        f"曾觸漲停={counts['touched']}；未觸及={counts['none']}"
    )
    if args.open_source == "tick":
        missing_tick_codes = [
            stock["code"]
            for stock in result["stocks"]
            if stock["locked_limit_up"] and stock["open_price"] is None
        ]
        if missing_tick_codes:
            print(
                "scanner WARNING：--open-source tick 缺少開盤 tick，"
                "以下標的保守維持 touched："
                + ",".join(missing_tick_codes),
                file=sys.stderr,
            )
    if args.sample:
        print("資料性質=合成樣本（完全離線、未登入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
