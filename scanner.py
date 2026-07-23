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


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TAIPEI = ZoneInfo("Asia/Taipei")

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
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path.name} 第 {line_number} 行不是合法 JSON"
                ) from exc
            if not isinstance(item, dict):
                raise ValueError(
                    f"{path.name} 第 {line_number} 行必須是 JSON object"
                )
            if item.get("kind") not in {"tick", "bidask"}:
                raise ValueError(
                    f"{path.name} 第 {line_number} 行 kind 必須為 tick/bidask"
                )
            code = str(item.get("code") or "").strip()
            if not code:
                raise ValueError(f"{path.name} 第 {line_number} 行缺少 code")
            item["_dt"] = parse_ts(item.get("ts"))
            if item["_dt"] is None:
                raise ValueError(
                    f"{path.name} 第 {line_number} 行 ts 不是合法 ISO 時間"
                )
            item["code"] = code
            events.append(item)
    return events


def normalize_date(value: Any, fallback: datetime | None = None) -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{8}", text):
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return (fallback or datetime.now()).strftime("%Y-%m-%d")


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
    沒有這份清單時，有事件就以實收 callback codes 為準。唯有完全
    沒事件且無 subscribed_codes 時，才以 metadata stocks 保留空結果。
    """
    raw_subscribed_codes = metadata.get("subscribed_codes")
    if isinstance(raw_subscribed_codes, list):
        subscribed_codes = {
            str(code).strip() for code in raw_subscribed_codes if str(code).strip()
        }
        return sorted(set(event_groups) | subscribed_codes)
    if event_groups:
        return sorted(event_groups)
    return sorted(stock_metadata)


def assert_coverage_selection() -> None:
    """Red-team：dropped 不得偽裝成 status=none，零 callback 訂閱仍保留。"""
    event_groups = {"1101": [{"code": "1101"}]}
    stock_metadata = {"1101": {}, "1102": {}, "9999": {}}
    metadata = {"subscribed_codes": ["1101", "1102"]}
    selected = select_result_codes(event_groups, stock_metadata, metadata)
    assert selected == ["1101", "1102"]
    assert "9999" not in selected
    assert select_result_codes(event_groups, stock_metadata, {}) == ["1101"]
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
            price = to_float(value)
            if price is not None:
                prices[str(code)] = price
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            price = to_float(item.get("open_price", item.get("open")))
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
        return explicit
    # chg_type==1 是行情源直接給出的漲停旗標，可安全用該筆試撮價
    # 還原 limit_up；沒有直接證據時維持 null，不用 10% 反推偽造。
    candidates = [
        to_float(event.get("price"))
        for event in events
        if event.get("kind") == "tick"
        and bool(event.get("simtrade"))
        and to_int(event.get("chg_type")) == 1
    ]
    return next((price for price in candidates if price is not None), None)


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


def bid0_drop_evidence(
    events: list[dict[str, Any]],
    limit_up: float | None,
    session_date: datetime,
    *,
    drop_ratio: float,
    min_peak: int,
    min_absolute: int,
    lookback_seconds: int,
) -> bool:
    if limit_up is None:
        return False
    cutoff = datetime.combine(session_date.date(), time(9, 0))
    window_start = cutoff - timedelta(seconds=lookback_seconds)
    observations: list[tuple[datetime, int]] = []
    for event in events:
        if (
            event.get("kind") != "bidask"
            or not bool(event.get("simtrade"))
            or not window_start <= event["_dt"] < cutoff
        ):
            continue
        bid_price = first_item(event.get("bid_price"))
        bid_volume = max(0, to_int(first_item(event.get("bid_volume"))) or 0)
        # 買一已離開漲停價，表示漲停買單在該次五檔更新中已不存在。
        effective_volume = bid_volume if same_price(bid_price, limit_up) else 0
        observations.append((event["_dt"], effective_volume))
    observations.sort(key=lambda item: item[0])
    if len(observations) < 2:
        return False
    peak = max(volume for _, volume in observations)
    final = observations[-1][1]
    absolute_drop = peak - final
    return (
        peak >= min_peak
        and absolute_drop >= min_absolute
        and final <= peak * (1.0 - drop_ratio)
    )


def stock_record(
    code: str,
    stock_meta: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    session: str,
    session_date: datetime,
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

    lock_times: list[datetime] = []
    sim_prices: list[float] = []
    bid0_volumes: list[int] = []
    chg_type_hit_1 = False
    sim_price_series: list[dict[str, Any]] = []
    bid0_series: list[dict[str, Any]] = []

    for event in events:
        simtrade = bool(event.get("simtrade"))
        if event.get("kind") == "tick":
            price = to_float(event.get("price"))
            chg_type_one = simtrade and to_int(event.get("chg_type")) == 1
            chg_type_hit_1 = chg_type_hit_1 or chg_type_one
            if price is not None:
                sim_price_series.append(
                    {
                        "t": iso_seconds(event["_dt"]),
                        "price": price,
                        "simtrade": simtrade,
                    }
                )
                if simtrade:
                    sim_prices.append(price)
            if simtrade and (chg_type_one or same_price(price, limit_up)):
                lock_times.append(event["_dt"])
        else:
            bid_price = to_float(first_item(event.get("bid_price")))
            bid_volume = max(0, to_int(first_item(event.get("bid_volume"))) or 0)
            bid0_series.append(
                {
                    "t": iso_seconds(event["_dt"]),
                    "bid0_price": bid_price,
                    "bid0_volume": bid_volume,
                }
            )
            if simtrade:
                bid0_volumes.append(bid_volume)
                if same_price(bid_price, limit_up):
                    lock_times.append(event["_dt"])

    locked_limit_up = bool(lock_times)
    first_lock = min(lock_times) if lock_times else None
    last_lock = max(lock_times) if lock_times else None
    duration = (
        round((last_lock - first_lock).total_seconds(), 3)
        if first_lock is not None and last_lock is not None
        else 0.0
    )

    open_price: float | None = None
    if session == "preopen":
        if open_source in {"auto", "tick"}:
            open_price = opening_tick_price(
                events, session_date, open_grace_seconds
            )
        if open_price is None and open_source in {"auto", "snapshot"}:
            open_price = snapshot_prices.get(code)

    open_gap_pct = (
        round((open_price / limit_up - 1.0) * 100.0, 4)
        if open_price is not None and limit_up is not None and limit_up > 0
        else None
    )
    bid0_dropped = (
        bid0_drop_evidence(
            events,
            limit_up,
            session_date,
            drop_ratio=drop_ratio,
            min_peak=drop_min_peak,
            min_absolute=drop_min_absolute,
            lookback_seconds=drop_lookback_seconds,
        )
        if session == "preopen"
        else False
    )

    if locked_limit_up and (
        (open_gap_pct is not None and open_gap_pct < -0.0001) or bid0_dropped
    ):
        status = "suspected_fake"
    elif locked_limit_up and open_gap_pct is not None:
        status = "locked_held"
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
        "sim_high": max(sim_prices) if sim_prices else 0.0,
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
        ("2308", "台達電", 900.0, 990.0, "touched"),
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
        elif scenario == "touched":
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
        elif scenario == "fake_drop":
            # 即使開盤價仍在漲停，開盤前大單驟撤也按 OR 準則列可疑。
            add_tick(code, "09:00:01", limit_up, False, 1)
        elif scenario == "held":
            add_tick(code, "09:00:01", limit_up, False, 1)
        elif scenario == "none":
            add_tick(code, "09:00:01", float(prices[-1]), False, 2)

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
    parser.add_argument("--in", dest="input_path", type=Path, help="auction JSONL")
    parser.add_argument("--out", dest="output_path", type=Path, help="result JSON")
    parser.add_argument(
        "--open-source",
        choices=("auto", "tick", "snapshot", "none"),
        default="auto",
        help=(
            "開盤價來源：auto=09:00 後首筆真實 tick，缺時讀 "
            "auction_YYYYMMDD.snapshot.json；snapshot=只讀 sidecar"
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
        default=0.70,
        help="買一堆量相對峰值減少比例門檻（預設 0.70）",
    )
    parser.add_argument(
        "--drop-min-peak",
        type=int,
        default=1000,
        help="啟用撤單判定的買一峰值最低張數（預設 1000）",
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
        default=300,
        help="撤單檢查的開盤前回看秒數（預設 300）",
    )
    return parser.parse_args(argv)


def validate_args(args: argparse.Namespace) -> None:
    if not args.sample and args.input_path is None:
        raise ValueError("一般掃描必須提供 --in")
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
            input_path = args.input_path.resolve()
            if not input_path.is_file():
                raise FileNotFoundError(input_path)
            events = read_jsonl(input_path)
            metadata = load_metadata(input_path)
            inferred_date = (
                metadata.get("date")
                or infer_date_from_filename(input_path)
                or datetime.now(TAIPEI).strftime("%Y-%m-%d")
            )
            output_path = args.output_path or DATA_DIR / (
                f"result_{normalize_date(inferred_date).replace('-', '')}.json"
            )

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
    if args.sample:
        print("資料性質=合成樣本（完全離線、未登入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
