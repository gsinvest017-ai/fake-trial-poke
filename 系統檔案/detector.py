#!/usr/bin/env python
"""試撮逐筆偵測器的純邏輯核心。

本模組不讀寫檔案、不連線，也不建立執行緒。輸入沿用 ``recorder.py``
正規化後的 bidask/tick/snapshot dict，輸出符合 ``/api/state`` 的
stocks/counts/alerts 契約。

判定語意刻意沿用 ``scanner.py`` 已修正的邊界：

* ``open_price <= 0`` 視為未知，改看 snapshot 最佳買賣價。
* 撤單比較窗口內末筆買一量與 snapshot，並同時套用比例、盤前最低量、
  絕對減量三個門檻。
* snapshot／真實 tick 只有在窗口結束後才是結果證據。
* locked 只接受 ``[window_start, window_end)`` 內的 simtrade BidAsk。
* ``limit_up <= 0`` 視為未知。
* 開盤價相對漲停價的極小負 gap 使用既有容差。
* 舊 recorder snapshot 的行情 ts 可由 meta.generated_at 證明實際查詢時點；
  新 recorder 則直接使用其 observed ts。
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_BID0_REMAIN_RATIO = 0.30
DEFAULT_DROP_LOOKBACK_SECONDS = 30 * 60
SNAPSHOT_LIMIT_TOLERANCE_RATIO = 0.001
OPEN_GAP_TOLERANCE_PCT = 0.0001

# 其他異常維度的暫定門檻。尚未經真實資料校準，集中於此以便調整。
# 百分比常數採 API 顯示單位（5.0 代表 5%）。
ANOM_BID_PEAK_LOTS = 300
ANOM_BID_REMAIN_RATIO = 0.30
ANOM_SWING_PCT = 5.0
ANOM_GAP_PCT = 4.0

ANOM_BIG_BID_WITHDRAW = "ANOM_BIG_BID_WITHDRAW"
ANOM_BID0_SWING = "ANOM_BID0_SWING"
ANOM_OPEN_GAP = "ANOM_OPEN_GAP"
ANOMALY_DIMENSIONS = (
    ANOM_BIG_BID_WITHDRAW,
    ANOM_BID0_SWING,
    ANOM_OPEN_GAP,
)
ANOMALY_LABELS = {
    ANOM_BIG_BID_WITHDRAW: "大單驟撤",
    ANOM_BID0_SWING: "試撮劇震",
    ANOM_OPEN_GAP: "開盤大跳空",
}
# 目前三個維度等權；未來校準後可直接在此調整權重。
ANOMALY_WEIGHTS = {
    ANOM_BIG_BID_WITHDRAW: 1,
    ANOM_BID0_SWING: 1,
    ANOM_OPEN_GAP: 1,
}
ANOMALY_THRESHOLDS = {
    "bid_peak_lots": ANOM_BID_PEAK_LOTS,
    "bid_remain_ratio": ANOM_BID_REMAIN_RATIO,
    "bid_withdraw_pct": (1.0 - ANOM_BID_REMAIN_RATIO) * 100.0,
    "swing_pct": ANOM_SWING_PCT,
    "gap_pct": ANOM_GAP_PCT,
    "calibrated": False,
}

# 疑似假試撮買一堆量分級；目前僅供排序與視覺提示，尚未校準。
FAKE_GRADE_T1_MIN_LOTS = 500
FAKE_GRADE_T2_MIN_LOTS = 200
FAKE_GRADE_T3_MIN_LOTS = 100
FAKE_GRADE_LABELS = {
    "T1": "大單",
    "T2": "中量",
    "T3": "邊緣",
    "T4": "薄量",
}
FAKE_GRADE_THRESHOLDS = {
    "t1_min_lots": FAKE_GRADE_T1_MIN_LOTS,
    "t2_min_lots": FAKE_GRADE_T2_MIN_LOTS,
    "t3_min_lots": FAKE_GRADE_T3_MIN_LOTS,
    "unit": "lots",
    "calibrated": False,
}

STATUS_LABELS = {
    "suspected_fake": "疑似假試撮",
    "locked_held": "鎖漲停守住",
    "touched": "曾觸漲停",
    "watching": "觀察中",
    "no_data": "未收到報價",
    "none": "無法判定",
}
STATUS_RANK = {
    "suspected_fake": 0,
    "locked_held": 1,
    "touched": 2,
    "watching": 3,
    "no_data": 4,
    "none": 5,
}
SERVICE_STATUSES = {"idle", "armed", "live", "closed", "replay"}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _to_positive_float(value: Any) -> float | None:
    number = _to_float(value)
    return number if number is not None and number > 0 else None


def _to_int(value: Any) -> int | None:
    number = _to_float(value)
    return None if number is None else int(number)


def _first(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    # snapshot 的最佳一檔有時是 scalar，直接接受以相容 Shioaji 欄位。
    return value


def _same_price(left: Any, right: Any) -> bool:
    a = _to_float(left)
    b = _to_float(right)
    return (
        a is not None
        and b is not None
        and math.isclose(a, b, rel_tol=0.0, abs_tol=1e-7)
    )


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TAIPEI)
    return parsed.astimezone(TAIPEI)


def _parse_window_spec(value: Any, fallback: str) -> datetime | time:
    if value is None:
        value = fallback
    if isinstance(value, datetime):
        return _parse_datetime(value)  # type: ignore[return-value]
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        parsed_datetime = _parse_datetime(value)
        if parsed_datetime is not None and "T" in value:
            return parsed_datetime
        for pattern in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(value.strip(), pattern).time()
            except ValueError:
                continue
    raise ValueError("window start/end 必須是 datetime、ISO 或 HH:MM[:SS]")


def _iso_seconds(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def _normalize_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.strptime(text, "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _fake_grade(max_bid0_volume: int) -> str:
    if max_bid0_volume >= FAKE_GRADE_T1_MIN_LOTS:
        return "T1"
    if max_bid0_volume >= FAKE_GRADE_T2_MIN_LOTS:
        return "T2"
    if max_bid0_volume >= FAKE_GRADE_T3_MIN_LOTS:
        return "T3"
    return "T4"


@dataclass
class _StockState:
    code: str
    name: str
    limit_up: float | None
    reference: float | None = None
    # 2=metadata 明列，1=event 明列，0=尚無明列值（可由 chg_type 推得）。
    limit_priority: int = 0
    reference_priority: int = 0
    watching: bool = False
    seen_stream: bool = False
    locked: bool = False
    first_lock: datetime | None = None
    last_lock: datetime | None = None
    lock_duration_sec: float = 0.0
    sim_high: float = 0.0
    bid0_price: float | None = None
    bid0_volume: int | None = None
    latest_quote_at: datetime | None = None
    final_window_bid0_at: datetime | None = None
    final_window_bid0_volume: int = 0
    anomaly_final_bid0_at: datetime | None = None
    anomaly_final_bid0_volume: int | None = None
    bid0_peak_volume: int = 0
    bid0_min_price: float | None = None
    bid0_max_price: float | None = None
    snapshot: dict[str, Any] | None = None
    snapshot_at: datetime | None = None
    snapshot_source_at: datetime | None = None
    opening_tick: float | None = None
    opening_tick_at: datetime | None = None
    open_price: float | None = None
    open_gap_pct: float | None = None
    open_gap_ref_pct: float | None = None
    bid0_withdraw_pct: float | None = None
    bid0_swing_pct: float | None = None
    reference_open_gap_pct: float | None = None
    open_gap_direction: str | None = None
    anomalies: list[str] = field(default_factory=list)
    anomaly_score: int = 0
    bid0_dropped: bool = False
    status: str = "none"
    locked_alerted: bool = False
    fake_alerted: bool = False
    spark: deque[dict[str, Any]] = field(default_factory=deque)


class Detector:
    """逐筆試撮狀態機。

    Parameters are deliberately standard-library values so the service and tests
    do not need Shioaji. ``window_start``/``window_end`` accept HH:MM, ISO string,
    ``time`` or ``datetime``.
    """

    def __init__(
        self,
        session: str = "preopen",
        window_start: Any = None,
        window_end: Any = None,
        *,
        stocks: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
        spark_points: int = 60,
        alerts_limit: int = 200,
        drop_ratio: float = 1.0 - DEFAULT_BID0_REMAIN_RATIO,
        drop_min_preopen: int = 1000,
        drop_min_absolute: int = 500,
        drop_lookback_seconds: int = DEFAULT_DROP_LOOKBACK_SECONDS,
        open_grace_seconds: int = 300,
    ) -> None:
        if session not in {"preopen", "preclose"}:
            raise ValueError("session 必須為 preopen 或 preclose")
        if spark_points <= 0 or alerts_limit <= 0:
            raise ValueError("spark_points/alerts_limit 必須大於 0")
        if not 0.0 < drop_ratio <= 1.0:
            raise ValueError("drop_ratio 必須介於 0（不含）與 1")
        if drop_min_preopen < 0 or drop_min_absolute < 0:
            raise ValueError("撤單張數門檻不可為負數")
        if drop_lookback_seconds <= 0 or open_grace_seconds < 0:
            raise ValueError("lookback 必須大於 0，open grace 不可為負")

        default_start = "13:25" if session == "preclose" else "08:30"
        default_end = "13:30" if session == "preclose" else "09:00"
        self.session = session
        self._window_start = _parse_window_spec(window_start, default_start)
        self._window_end = _parse_window_spec(window_end, default_end)
        self.spark_points = spark_points
        self.drop_ratio = drop_ratio
        self.drop_min_preopen = drop_min_preopen
        self.drop_min_absolute = drop_min_absolute
        self.drop_lookback_seconds = drop_lookback_seconds
        self.open_grace_seconds = open_grace_seconds
        self._stocks: dict[str, _StockState] = {}
        self._alerts: deque[dict[str, Any]] = deque(maxlen=alerts_limit)
        self._legacy_snapshot_at: datetime | None = None
        self._legacy_snapshot_count = 0
        self._date: str | None = None
        self._universe_size: int | None = None
        self._subscribed_count: int | None = None
        # 完整 recorder meta 有 subscribed_codes 時，snapshot 仍可能含整個
        # universe；此集合避免把配額 dropped 標的重新自動註冊。
        self._allowed_codes: set[str] | None = None

        if stocks is not None:
            self.register_stocks(stocks)

    def _window_for(self, anchor: datetime) -> tuple[datetime, datetime]:
        def resolve(spec: datetime | time) -> datetime:
            if isinstance(spec, datetime):
                return spec.astimezone(TAIPEI)
            return datetime.combine(anchor.date(), spec, tzinfo=TAIPEI)

        start = resolve(self._window_start)
        end = resolve(self._window_end)
        if end <= start:
            end += timedelta(days=1)
        return start, end

    def _window_text(self, spec: datetime | time) -> str:
        return spec.strftime("%H:%M")

    def _set_metadata_window(self, metadata: Mapping[str, Any]) -> None:
        self._date = _normalize_date(metadata.get("date")) or self._date
        raw_session = metadata.get("session")
        if raw_session in {"preopen", "preclose"}:
            self.session = str(raw_session)
        raw_window = metadata.get("window")
        if isinstance(raw_window, Mapping):
            start = raw_window.get("start")
            end = raw_window.get("end")
            if start is not None and end is not None:
                self._window_start = _parse_window_spec(start, "08:30")
                self._window_end = _parse_window_spec(end, "09:00")

        self._universe_size = _to_int(metadata.get("universe_size"))
        self._subscribed_count = _to_int(metadata.get("subscribed"))
        self._legacy_snapshot_count = max(
            0, _to_int(metadata.get("snapshot_count")) or 0
        )
        if self._legacy_snapshot_count > 0:
            self._legacy_snapshot_at = _parse_datetime(
                metadata.get("generated_at")
            )

    def register_stocks(
        self,
        stocks: Iterable[Mapping[str, Any]] | Mapping[str, Any],
    ) -> None:
        """註冊監控標的。

        傳入完整 recorder meta 時會嚴格採 ``subscribed_codes``，不把因配額
        dropped 的股票偽裝成 watching。直接傳 list 時則視每一檔為已訂閱。
        """

        items: list[Mapping[str, Any]]
        if isinstance(stocks, Mapping) and isinstance(
            stocks.get("stocks"), list
        ):
            metadata = stocks
            self._set_metadata_window(metadata)
            raw_items = [
                item
                for item in metadata["stocks"]
                if isinstance(item, Mapping)
            ]
            raw_subscribed = metadata.get("subscribed_codes")
            if isinstance(raw_subscribed, list):
                self._allowed_codes = {
                    str(raw_code or "").strip()
                    for raw_code in raw_subscribed
                    if str(raw_code or "").strip()
                }
                by_code = {
                    str(item.get("code") or "").strip(): item
                    for item in raw_items
                    if str(item.get("code") or "").strip()
                }
                items = []
                for raw_code in raw_subscribed:
                    code = str(raw_code or "").strip()
                    if not code:
                        continue
                    items.append(by_code.get(code, {"code": code}))
            else:
                items = raw_items
        elif isinstance(stocks, Mapping):
            items = [stocks]
        else:
            items = [item for item in stocks if isinstance(item, Mapping)]

        for item in items:
            self.register_stock(item)
        if self._subscribed_count is None:
            self._subscribed_count = len(items)

    def register_stock(self, metadata: Mapping[str, Any]) -> None:
        code = str(metadata.get("code") or "").strip()
        if not code:
            raise ValueError("stock metadata 缺少 code")
        state = self._stocks.get(code)
        raw_name = str(metadata.get("name") or "").strip()
        has_explicit_limit = (
            "limit_up" in metadata and metadata.get("limit_up") is not None
        )
        has_explicit_reference = (
            "reference" in metadata and metadata.get("reference") is not None
        )
        limit_up = (
            _to_positive_float(metadata.get("limit_up"))
            if has_explicit_limit
            else None
        )
        reference = (
            _to_positive_float(metadata.get("reference"))
            if has_explicit_reference
            else None
        )
        if state is None:
            state = _StockState(
                code=code,
                name=raw_name or code,
                limit_up=limit_up,
                reference=reference,
                limit_priority=2 if has_explicit_limit else 0,
                reference_priority=2 if has_explicit_reference else 0,
                watching=True,
                spark=deque(maxlen=self.spark_points),
            )
            self._stocks[code] = state
        else:
            state.watching = True
            if raw_name:
                state.name = raw_name
            if has_explicit_limit:
                # metadata 與 scanner.first_known 一樣優先於 event。
                state.limit_up = limit_up
                state.limit_priority = 2
            if has_explicit_reference:
                state.reference = reference
                state.reference_priority = 2
        self._recompute(state)

    def _state_for_event(self, event: Mapping[str, Any]) -> _StockState:
        code = str(event.get("code") or "").strip()
        if not code:
            raise ValueError("event 缺少 code")
        state = self._stocks.get(code)
        if state is None:
            name = str(event.get("name") or code).strip() or code
            has_explicit_limit = (
                "limit_up" in event and event.get("limit_up") is not None
            )
            has_explicit_reference = (
                "reference" in event and event.get("reference") is not None
            )
            state = _StockState(
                code=code,
                name=name,
                limit_up=(
                    _to_positive_float(event.get("limit_up"))
                    if has_explicit_limit
                    else None
                ),
                reference=(
                    _to_positive_float(event.get("reference"))
                    if has_explicit_reference
                    else None
                ),
                limit_priority=1 if has_explicit_limit else 0,
                reference_priority=1 if has_explicit_reference else 0,
                spark=deque(maxlen=self.spark_points),
            )
            self._stocks[code] = state
        elif event.get("name"):
            state.name = str(event["name"]).strip() or state.name

        if (
            state.limit_priority < 2
            and "limit_up" in event
            and event.get("limit_up") is not None
        ):
            state.limit_up = _to_positive_float(event.get("limit_up"))
            state.limit_priority = 1
        if (
            state.reference_priority < 2
            and "reference" in event
            and event.get("reference") is not None
        ):
            state.reference = _to_positive_float(event.get("reference"))
            state.reference_priority = 1
        if (
            state.limit_priority == 0
            and state.limit_up is None
            and str(event.get("kind") or "").lower() == "tick"
            and event.get("simtrade") is True
            and _to_int(event.get("chg_type")) == 1
        ):
            # scanner 的舊 Tick/chg_type 相容用途只用來補 limit_up；locked
            # 仍由 BidAsk 方法 B 決定，避免雙通道抵達順序造成假警報。
            state.limit_up = _to_positive_float(event.get("price"))
        return state

    def _snapshot_evidence_at(
        self,
        event: Mapping[str, Any],
        event_at: datetime,
        boundary: datetime,
    ) -> datetime | None:
        explicit_observed = _parse_datetime(
            event.get("observed_at", event.get("snapshot_at"))
        )
        evidence_at = explicit_observed or event_at
        if evidence_at >= boundary:
            return evidence_at
        # 舊檔把行情本身的 stale ts 寫成 snapshot ts。只有 sidecar 明確
        # 記錄 snapshot_count 且 generated_at 已過邊界時才允許相容。
        if (
            self._legacy_snapshot_count > 0
            and self._legacy_snapshot_at is not None
            and self._legacy_snapshot_at >= boundary
        ):
            return self._legacy_snapshot_at
        return None

    def _update_current_quote(
        self,
        state: _StockState,
        event: Mapping[str, Any],
        effective_at: datetime,
    ) -> tuple[float | None, int | None]:
        bid0 = _to_float(_first(event.get("bid_price")))
        raw_volume = _to_int(_first(event.get("bid_volume")))
        bid0_volume = None if raw_volume is None else max(0, raw_volume)
        if state.latest_quote_at is None or effective_at >= state.latest_quote_at:
            state.latest_quote_at = effective_at
            state.bid0_price = bid0
            state.bid0_volume = bid0_volume
        return bid0, bid0_volume

    def _process_bidask(
        self,
        state: _StockState,
        event: Mapping[str, Any],
        event_at: datetime,
        window_start: datetime,
        window_end: datetime,
    ) -> None:
        bid0, bid0_volume = self._update_current_quote(
            state, event, event_at
        )
        state.spark.append(
            {
                "t": _iso_seconds(event_at),
                "bid0_price": bid0,
                "bid0_volume": bid0_volume,
            }
        )

        simtrade = event.get("simtrade") is True
        in_window = window_start <= event_at < window_end
        if simtrade and in_window:
            is_locked = (
                state.limit_up is not None
                and _same_price(bid0, state.limit_up)
            )
            state.seen_stream = True
            if bid0 is not None:
                state.sim_high = max(state.sim_high, bid0)
            if bid0 is not None and bid0 > 0:
                state.bid0_min_price = (
                    bid0
                    if state.bid0_min_price is None
                    else min(state.bid0_min_price, bid0)
                )
                state.bid0_max_price = (
                    bid0
                    if state.bid0_max_price is None
                    else max(state.bid0_max_price, bid0)
                )
            if bid0 is not None and bid0_volume is not None:
                state.bid0_peak_volume = max(
                    state.bid0_peak_volume,
                    bid0_volume,
                )
                if (
                    state.anomaly_final_bid0_at is None
                    or event_at >= state.anomaly_final_bid0_at
                ):
                    state.anomaly_final_bid0_at = event_at
                    state.anomaly_final_bid0_volume = bid0_volume
            if is_locked:
                if not state.locked:
                    state.locked = True
                    self._push_alert(state, "locked", event_at)
                # 與 scanner.py 一致：鎖秒為窗口內 bid0==limit_up
                # 事件的首末時間差，不外推到下一筆非鎖定報價。
                state.first_lock = (
                    event_at
                    if state.first_lock is None
                    else min(state.first_lock, event_at)
                )
                state.last_lock = (
                    event_at
                    if state.last_lock is None
                    else max(state.last_lock, event_at)
                )
                state.lock_duration_sec = (
                    state.last_lock - state.first_lock
                ).total_seconds()

        lookback_start = window_end - timedelta(
            seconds=self.drop_lookback_seconds
        )
        if simtrade and lookback_start <= event_at < window_end:
            if (
                state.final_window_bid0_at is None
                or event_at >= state.final_window_bid0_at
            ):
                state.final_window_bid0_at = event_at
                state.final_window_bid0_volume = bid0_volume or 0

    def _process_snapshot(
        self,
        state: _StockState,
        event: Mapping[str, Any],
        event_at: datetime,
        boundary: datetime,
    ) -> None:
        evidence_at = self._snapshot_evidence_at(event, event_at, boundary)
        quote_at = evidence_at or event_at
        self._update_current_quote(state, event, quote_at)
        if evidence_at is None:
            return
        current_key = (
            state.snapshot_at or datetime.min.replace(tzinfo=TAIPEI),
            state.snapshot_source_at or datetime.min.replace(tzinfo=TAIPEI),
        )
        candidate_key = (evidence_at, event_at)
        if candidate_key >= current_key:
            state.snapshot = dict(event)
            state.snapshot_at = evidence_at
            state.snapshot_source_at = event_at

    def _process_opening_tick(
        self,
        state: _StockState,
        event: Mapping[str, Any],
        event_at: datetime,
        boundary: datetime,
    ) -> None:
        if self.session != "preopen" or event.get("simtrade") is not False:
            return
        if not (
            boundary
            <= event_at
            <= boundary + timedelta(seconds=self.open_grace_seconds)
        ):
            return
        price = _to_positive_float(event.get("price"))
        if price is not None and (
            state.opening_tick_at is None or event_at < state.opening_tick_at
        ):
            state.opening_tick = price
            state.opening_tick_at = event_at

    def process_event(
        self, event: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """處理一筆 recorder event，並回傳該檔最新公開狀態。"""

        if not isinstance(event, Mapping):
            raise TypeError("event 必須是 mapping")
        code = str(event.get("code") or "").strip()
        if not code:
            raise ValueError("event 缺少 code")
        if (
            self._allowed_codes is not None
            and code not in self._allowed_codes
        ):
            return None
        event_at = _parse_datetime(event.get("ts"))
        if event_at is None:
            raise ValueError("event.ts 必須是合法 ISO 時間或 datetime")
        kind = str(event.get("kind") or "").strip().lower()
        if kind not in {"bidask", "snapshot", "tick"}:
            raise ValueError("event.kind 必須為 bidask/snapshot/tick")

        state = self._state_for_event(event)
        window_start, window_end = self._window_for(event_at)
        if (
            kind in {"bidask", "tick"}
            and window_start <= event_at < window_end
        ):
            state.seen_stream = True
        if kind == "bidask":
            self._process_bidask(
                state, event, event_at, window_start, window_end
            )
        elif kind == "snapshot":
            self._process_snapshot(state, event, event_at, window_end)
        else:
            self._process_opening_tick(state, event, event_at, window_end)
        self._recompute(state)
        return self._public_stock(state)

    def finalize_no_data(self) -> list[str]:
        """Mark subscribed stocks with zero in-window quote events."""
        missing_codes: list[str] = []
        for state in self._stocks.values():
            if state.watching and not state.seen_stream:
                state.status = "no_data"
                missing_codes.append(state.code)
        return sorted(missing_codes)

    # 讓 service/replay 可以採較語意化的命名，不複製任何判定路徑。
    ingest = process_event

    def process_events(
        self, events: Iterable[Mapping[str, Any]]
    ) -> dict[str, Any]:
        for event in events:
            self.process_event(event)
        return self.get_state()

    def _snapshot_quote_status(
        self,
        snapshot: Mapping[str, Any] | None,
        limit_up: float | None,
    ) -> str | None:
        if snapshot is None or limit_up is None or limit_up <= 0:
            return None
        quotes = (
            _to_float(_first(snapshot.get("bid_price"))),
            _to_float(_first(snapshot.get("ask_price"))),
        )
        valid_quotes = [
            price for price in quotes if price is not None and price > 0
        ]
        if not valid_quotes:
            return None
        held_threshold = limit_up * (
            1.0 - SNAPSHOT_LIMIT_TOLERANCE_RATIO
        )
        return (
            "locked_held"
            if any(price >= held_threshold for price in valid_quotes)
            else "suspected_fake"
        )

    def _drop_evidence(
        self, state: _StockState, snapshot: Mapping[str, Any] | None
    ) -> bool:
        if snapshot is None or state.final_window_bid0_at is None:
            return False
        raw_snapshot_volume = _to_int(_first(snapshot.get("bid_volume")))
        if raw_snapshot_volume is None:
            return False
        preopen_final = max(0, state.final_window_bid0_volume)
        snapshot_volume = max(0, raw_snapshot_volume)
        if preopen_final <= 0:
            return False
        absolute_drop = preopen_final - snapshot_volume
        remain_ratio = 1.0 - self.drop_ratio
        actual_remain_ratio = snapshot_volume / preopen_final
        return (
            preopen_final >= self.drop_min_preopen
            and absolute_drop >= self.drop_min_absolute
            and actual_remain_ratio < remain_ratio
            and not math.isclose(
                actual_remain_ratio,
                remain_ratio,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        )

    def _recompute_anomalies(self, state: _StockState) -> None:
        """重算與既有試撮狀態並存的觀察型異常維度。"""

        final_volume = state.anomaly_final_bid0_volume
        if state.bid0_peak_volume > 0 and final_volume is not None:
            remain_ratio = final_volume / state.bid0_peak_volume
            state.bid0_withdraw_pct = round(
                (1.0 - remain_ratio) * 100.0,
                4,
            )
        else:
            remain_ratio = None
            state.bid0_withdraw_pct = None

        if (
            state.reference is not None
            and state.reference > 0
            and state.bid0_min_price is not None
            and state.bid0_max_price is not None
        ):
            state.bid0_swing_pct = round(
                (
                    (state.bid0_max_price - state.bid0_min_price)
                    / state.reference
                )
                * 100.0,
                4,
            )
        else:
            state.bid0_swing_pct = None

        snapshot_open = (
            _to_positive_float(state.snapshot.get("open_price"))
            if self.session == "preopen" and state.snapshot is not None
            else None
        )
        if (
            snapshot_open is not None
            and state.reference is not None
            and state.reference > 0
        ):
            state.reference_open_gap_pct = round(
                (snapshot_open / state.reference - 1.0) * 100.0,
                4,
            )
            if state.reference_open_gap_pct > 0:
                state.open_gap_direction = "up"
            elif state.reference_open_gap_pct < 0:
                state.open_gap_direction = "down"
            else:
                state.open_gap_direction = "flat"
        else:
            state.reference_open_gap_pct = None
            state.open_gap_direction = None

        anomalies: list[str] = []
        if (
            state.bid0_peak_volume >= ANOM_BID_PEAK_LOTS
            and remain_ratio is not None
            and remain_ratio < ANOM_BID_REMAIN_RATIO
            and not math.isclose(
                remain_ratio,
                ANOM_BID_REMAIN_RATIO,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
        ):
            anomalies.append(ANOM_BIG_BID_WITHDRAW)
        if (
            state.bid0_swing_pct is not None
            and state.bid0_swing_pct >= ANOM_SWING_PCT
        ):
            anomalies.append(ANOM_BID0_SWING)
        if (
            state.reference_open_gap_pct is not None
            and abs(state.reference_open_gap_pct) >= ANOM_GAP_PCT
        ):
            anomalies.append(ANOM_OPEN_GAP)

        state.anomalies = anomalies
        state.anomaly_score = sum(
            ANOMALY_WEIGHTS[dimension] for dimension in anomalies
        )

    def _recompute(self, state: _StockState) -> None:
        snapshot = state.snapshot
        state.open_price = None
        if snapshot is not None and self.session == "preopen":
            # <=0 一律轉成 None，接著才走 snapshot bid/ask fallback。
            state.open_price = _to_positive_float(snapshot.get("open_price"))
        elif snapshot is None and self.session == "preopen":
            state.open_price = state.opening_tick

        state.open_gap_pct = (
            round(
                (state.open_price / state.limit_up - 1.0) * 100.0,
                4,
            )
            if (
                state.open_price is not None
                and state.limit_up is not None
                and state.limit_up > 0
            )
            else None
        )
        state.open_gap_ref_pct = (
            round(
                (state.open_price / state.reference - 1.0) * 100.0,
                4,
            )
            if (
                state.open_price is not None
                and state.reference is not None
                and state.reference > 0
            )
            else None
        )
        state.bid0_dropped = self._drop_evidence(state, snapshot)
        self._recompute_anomalies(state)

        if state.limit_up is None or state.limit_up <= 0:
            status = "none"
        elif not state.locked:
            status = (
                "watching"
                if state.watching or state.seen_stream
                else "none"
            )
        elif snapshot is not None:
            gap_below_tolerance = (
                state.open_gap_pct is not None
                and state.open_gap_pct < -OPEN_GAP_TOLERANCE_PCT
            )
            if gap_below_tolerance or state.bid0_dropped:
                status = "suspected_fake"
            elif state.open_gap_pct is not None:
                status = "locked_held"
            else:
                status = (
                    self._snapshot_quote_status(snapshot, state.limit_up)
                    or "touched"
                )
        elif state.open_price is not None:
            status = (
                "suspected_fake"
                if (
                    state.open_gap_pct is not None
                    and state.open_gap_pct < -OPEN_GAP_TOLERANCE_PCT
                )
                else "locked_held"
            )
        else:
            # 沒有窗口結束後證據，不提前宣稱「守住」。
            status = "touched"

        state.status = status
        if status == "suspected_fake" and not state.fake_alerted:
            state.fake_alerted = True
            self._push_alert(
                state,
                "suspected_fake",
                state.snapshot_at
                or state.opening_tick_at
                or state.first_lock,
            )

    def _push_alert(
        self,
        state: _StockState,
        alert_type: str,
        timestamp: datetime | None,
    ) -> None:
        if alert_type == "locked":
            if state.locked_alerted:
                return
            state.locked_alerted = True
            message = f"{state.code} {state.name} 試撮買一鎖漲停"
        elif alert_type == "suspected_fake":
            message = (
                f"{state.code} {state.name} 疑似假試撮："
                "窗口後未守住漲停"
            )
        else:  # pragma: no cover - 內部呼叫守衛
            raise ValueError("不支援的 alert type")
        self._alerts.appendleft(
            {
                "ts": _iso_seconds(timestamp),
                "code": state.code,
                "name": state.name,
                "type": alert_type,
                "message": message,
            }
        )

    def _public_stock(self, state: _StockState) -> dict[str, Any]:
        lock_duration_sec = round(state.lock_duration_sec, 3)
        grade = (
            _fake_grade(state.bid0_peak_volume)
            if state.status == "suspected_fake"
            else None
        )
        return {
            "code": state.code,
            "name": state.name,
            "reference": state.reference,
            "limit_up": state.limit_up,
            "status": state.status,
            "status_label": STATUS_LABELS[state.status],
            # locked 代表窗口內曾鎖過，對應舊 scanner.locked_limit_up。
            "locked": state.locked,
            "locked_limit_up": state.locked,
            "bid0_price": state.bid0_price,
            "bid0_volume": state.bid0_volume,
            "sim_high": state.sim_high,
            "first_lock_time": _iso_seconds(state.first_lock),
            "last_lock_time": _iso_seconds(state.last_lock),
            "lock_duration_sec": lock_duration_sec,
            "open_price": state.open_price,
            "open_gap_pct": state.open_gap_pct,
            "open_gap_ref_pct": state.open_gap_ref_pct,
            "grade": grade,
            "grade_label": FAKE_GRADE_LABELS.get(grade),
            "bid0_dropped": state.bid0_dropped,
            "anomalies": list(state.anomalies),
            "anomaly_score": state.anomaly_score,
            "bid0_peak_volume": state.bid0_peak_volume,
            "max_bid0_volume": state.bid0_peak_volume,
            "final_window_bid0_volume": state.anomaly_final_bid0_volume,
            "bid0_withdraw_pct": state.bid0_withdraw_pct,
            "bid0_min_price": state.bid0_min_price,
            "bid0_max_price": state.bid0_max_price,
            "bid0_swing_pct": state.bid0_swing_pct,
            "reference_open_gap_pct": state.reference_open_gap_pct,
            "open_gap_direction": state.open_gap_direction,
            "spark": [dict(point) for point in state.spark],
        }

    def get_state(self) -> dict[str, Any]:
        stocks = [self._public_stock(state) for state in self._stocks.values()]
        stocks.sort(
            key=lambda stock: (
                STATUS_RANK[stock["status"]],
                -stock["anomaly_score"],
                stock["open_gap_pct"] is None,
                stock["open_gap_pct"] or 0.0,
                stock["code"],
            )
        )
        counts = {
            status: sum(stock["status"] == status for stock in stocks)
            for status in (
                "suspected_fake",
                "locked_held",
                "touched",
                "watching",
                "no_data",
            )
        }
        counts["anomaly"] = sum(bool(stock["anomalies"]) for stock in stocks)
        return {
            "counts": counts,
            "stocks": stocks,
            "alerts": [dict(alert) for alert in self._alerts],
            "anomaly_thresholds": dict(ANOMALY_THRESHOLDS),
            "fake_grade_thresholds": dict(FAKE_GRADE_THRESHOLDS),
        }

    snapshot = get_state
    state = get_state

    def build_state(
        self,
        service_status: str = "idle",
        now: datetime | str | None = None,
        next_window_at: datetime | str | None = None,
        universe: int | None = None,
        subscribed: int | None = None,
    ) -> dict[str, Any]:
        """包裝成完整 ``/api/state`` 契約，供 service 的鎖內複製。"""

        if service_status not in SERVICE_STATUSES:
            raise ValueError("service_status enum 不合法")
        parsed_now = (
            datetime.now(TAIPEI) if now is None else _parse_datetime(now)
        )
        if parsed_now is None:
            raise ValueError("now 必須是合法 datetime/ISO")
        parsed_next = (
            None
            if next_window_at is None
            else _parse_datetime(next_window_at)
        )
        if next_window_at is not None and parsed_next is None:
            raise ValueError("next_window_at 必須是合法 datetime/ISO")
        detector_state = self.get_state()
        return {
            "date": self._date or parsed_now.date().isoformat(),
            "service_status": service_status,
            "session": self.session,
            "window": {
                "start": self._window_text(self._window_start),
                "end": self._window_text(self._window_end),
            },
            "now": _iso_seconds(parsed_now),
            "next_window_at": _iso_seconds(parsed_next),
            "universe": (
                int(universe)
                if universe is not None
                else self._universe_size or len(self._stocks)
            ),
            "subscribed": (
                int(subscribed)
                if subscribed is not None
                else self._subscribed_count or len(self._stocks)
            ),
            **detector_state,
        }


# 較完整的名稱留給純邏輯單元測試/外部使用；service 可用短名 Detector。
AuctionDetector = Detector


__all__ = [
    "ANOMALY_LABELS",
    "ANOMALY_THRESHOLDS",
    "ANOMALY_WEIGHTS",
    "ANOM_BID0_SWING",
    "ANOM_BID_PEAK_LOTS",
    "ANOM_BID_REMAIN_RATIO",
    "ANOM_BIG_BID_WITHDRAW",
    "ANOM_GAP_PCT",
    "ANOM_OPEN_GAP",
    "ANOM_SWING_PCT",
    "AuctionDetector",
    "Detector",
    "FAKE_GRADE_LABELS",
    "FAKE_GRADE_THRESHOLDS",
    "FAKE_GRADE_T1_MIN_LOTS",
    "FAKE_GRADE_T2_MIN_LOTS",
    "FAKE_GRADE_T3_MIN_LOTS",
    "OPEN_GAP_TOLERANCE_PCT",
    "SNAPSHOT_LIMIT_TOLERANCE_RATIO",
    "STATUS_LABELS",
]
