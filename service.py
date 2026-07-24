#!/usr/bin/env python
"""常駐台股試撮偵測器服務。

資料源有兩種：

* live：行情唯讀登入 Shioaji，單通道訂閱最多 254 檔 BidAsk，在試撮
  窗口內把逐筆事件交給 detector，窗口結束後以 snapshot 收口。
* replay：讀取 recorder 產生的 JSONL 與同名 meta sidecar，依事件時間
  加速重播，供離線端到端驗證。

本模組不啟用 CA、不選交易帳號、不下單。HTTP 僅綁定使用者指定的本機
介面，預設為 http://127.0.0.1:8900/。
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import signal
import sys
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


# 必須在匯入 Shioaji 前降低第三方套件輸出敏感連線資訊的風險。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

BASE_DIR = Path(__file__).resolve().parent
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_HOST = "127.0.0.1"
MAX_SUBSCRIBED_STOCKS = 254
PREARM_MINUTES = 5
SNAPSHOT_AFTER_END_SECONDS = 5
PUBLISH_INTERVAL_SECONDS = 0.2
SUBSCRIBE_EVENT_GRACE_SECONDS = 0.01
LOGIN_RETRY_SECONDS = 30.0
DEFAULT_STALE_AFTER_SECONDS = 10.0


def _default_port() -> int:
    try:
        port = int(os.environ.get("AUCTION_PORT", "8900"))
    except ValueError:
        return 8900
    return port if 1 <= port <= 65535 else 8900


DEFAULT_PORT = _default_port()


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def iso_taipei(value: datetime | None = None) -> str:
    current = value or taipei_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI)
    else:
        current = current.astimezone(TAIPEI)
    return current.isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=TAIPEI)
    return parsed.astimezone(TAIPEI)


def parse_hhmm(value: str) -> datetime_time:
    try:
        parsed = datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("時間必須為有效 HH:MM") from exc
    return parsed


def relative_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path.resolve()


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def first_item(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return value[0] if value else None
    return value


def default_clock(session: str) -> tuple[datetime_time, datetime_time]:
    if session == "preclose":
        return datetime_time(13, 25), datetime_time(13, 30)
    return datetime_time(8, 30), datetime_time(9, 0)


def window_on(
    day: date,
    start_clock: datetime_time,
    end_clock: datetime_time,
) -> tuple[datetime, datetime]:
    start_at = datetime.combine(day, start_clock, tzinfo=TAIPEI)
    end_at = datetime.combine(day, end_clock, tzinfo=TAIPEI)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    return start_at, end_at


def next_weekday(day: date) -> date:
    candidate = day + timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def upcoming_window(
    now: datetime,
    start_clock: datetime_time,
    end_clock: datetime_time,
) -> tuple[datetime, datetime]:
    start_at, end_at = window_on(now.date(), start_clock, end_clock)
    if now < end_at + timedelta(seconds=SNAPSHOT_AFTER_END_SECONDS):
        return start_at, end_at
    return window_on(next_weekday(now.date()), start_clock, end_clock)


def next_window_after(
    end_at: datetime,
    start_clock: datetime_time,
    end_clock: datetime_time,
) -> tuple[datetime, datetime]:
    return window_on(next_weekday(end_at.date()), start_clock, end_clock)


def stop_wait(stop_event: threading.Event, seconds: float) -> bool:
    """可中止等待，且單次不阻塞超過一秒。"""
    deadline = time.monotonic() + max(0.0, seconds)
    while not stop_event.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        stop_event.wait(min(1.0, remaining))
    return True


def empty_state(
    *,
    status: str,
    session: str,
    window_start: str,
    window_end: str,
    next_window_at: str | None,
) -> dict[str, Any]:
    return {
        "service_status": status,
        "session": session,
        "window": {"start": window_start, "end": window_end},
        "now": iso_taipei(),
        "next_window_at": next_window_at,
        "universe": 0,
        "subscribed": 0,
        "recording": False,
        "record_path": None,
        "record_count": 0,
        "last_event_age_sec": None,
        "login_ok": False,
        "subscribe_ok": False,
        "counts": {
            "suspected_fake": 0,
            "locked_held": 0,
            "touched": 0,
            "watching": 0,
        },
        "stocks": [],
        "alerts": [],
    }


class ServiceJsonlWriter:
    """以單一背景執行緒依序落地 recorder 相容的 UTF-8 JSONL。"""

    _STOP = object()

    def __init__(self, path: Path, *, append: bool = False) -> None:
        self.path = path
        self.append = append
        self._items: queue.Queue[dict[str, Any] | object] = queue.Queue()
        self._lock = threading.Lock()
        self._count = 0
        self._handle: Any | None = None
        self.error: BaseException | None = None
        self._started = False
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="auction-service-jsonl-writer",
            daemon=True,
        )

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def active(self) -> bool:
        return (
            self._started
            and not self._closed
            and self.error is None
            and self._thread.is_alive()
        )

    def start(self) -> None:
        if self._started:
            raise RuntimeError("JSONL writer 已啟動")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if self.append else "w"
        # 先在呼叫端同步開檔，讓路徑/權限錯誤不會被背景執行緒吞掉。
        self._handle = self.path.open(
            mode,
            encoding="utf-8",
            newline="\n",
        )
        self._started = True
        self._thread.start()

    def put(self, record: dict[str, Any]) -> None:
        if not self._started or self._closed:
            raise RuntimeError("JSONL writer 未啟動或已關閉")
        if self.error is not None:
            raise RuntimeError("JSONL writer 已失效") from self.error
        self._items.put(copy.deepcopy(record))

    def close(self) -> None:
        if not self._started or self._closed:
            return
        self._closed = True
        self._items.put(self._STOP)
        self._thread.join()

    def _run(self) -> None:
        handle = self._handle
        if handle is None:  # pragma: no cover - start() 已保證
            self.error = RuntimeError("JSONL writer 缺少檔案 handle")
            return
        try:
            while True:
                item = self._items.get()
                if item is self._STOP:
                    break
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")
                handle.flush()
                with self._lock:
                    self._count += 1
        except BaseException as exc:  # pragma: no cover - 磁碟/權限防禦
            self.error = exc
        finally:
            try:
                handle.close()
            except BaseException as exc:  # pragma: no cover - 關檔防禦
                if self.error is None:
                    self.error = exc


def write_record_metadata(
    record_path: Path,
    metadata: dict[str, Any],
) -> Path:
    """寫 recorder/scanner 共用的同名 .meta.json sidecar。"""
    meta_path = record_path.with_suffix(".meta.json")
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    with meta_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return meta_path


class SharedState:
    """提供 webserver 無鎖外洩風險的 thread-safe JSON snapshot。"""

    def __init__(self, initial: dict[str, Any]) -> None:
        self._lock = threading.Lock()
        self._state = copy.deepcopy(initial)

    def replace(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._state = copy.deepcopy(state)

    def get_state(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    snapshot = get_state

    def __call__(self) -> dict[str, Any]:
        return self.get_state()


class ServiceRuntime:
    """序列化 detector 寫入，並把最新完整 state 發布給 HTTP 層。"""

    def __init__(
        self,
        *,
        session: str,
        window_start: str,
        window_end: str,
        service_status: str = "idle",
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        record_path: Path | None = None,
        record_append: bool = False,
    ) -> None:
        from detector import AuctionDetector

        self._detector_type = AuctionDetector
        self._lock = threading.RLock()
        self._detector = AuctionDetector(
            session=session,
            window_start=window_start,
            window_end=window_end,
        )
        self._service_status = service_status
        self._next_window_at: str | None = None
        self._now_override: str | None = None
        self._universe = 0
        self._subscribed = 0
        self._login_ok = False
        self._subscribe_ok = False
        self._last_event_ts: datetime | None = None
        self._stale_after_seconds = stale_after_seconds
        self._record_writer: ServiceJsonlWriter | None = None
        self._record_path: Path | None = None
        self._record_count = 0
        self._record_error: BaseException | None = None
        self.shared = SharedState(
            empty_state(
                status=service_status,
                session=session,
                window_start=window_start,
                window_end=window_end,
                next_window_at=None,
            )
        )
        if record_path is not None:
            self.start_recording(record_path, append=record_append)

    def reset_detector(
        self,
        metadata: dict[str, Any],
        *,
        session: str,
        window_start: str,
        window_end: str,
        universe: int,
        subscribed: int,
    ) -> None:
        detector = self._detector_type(
            session=session,
            window_start=window_start,
            window_end=window_end,
        )
        detector.register_stocks(metadata)
        with self._lock:
            self._detector = detector
            self._universe = universe
            self._subscribed = subscribed
            self._last_event_ts = None

    def set_context(
        self,
        *,
        service_status: str | None = None,
        next_window_at: str | None | object = ...,
        now_override: str | None | object = ...,
        universe: int | None = None,
        subscribed: int | None = None,
        login_ok: bool | None = None,
        subscribe_ok: bool | None = None,
        last_event_ts: datetime | str | None | object = ...,
    ) -> None:
        with self._lock:
            if service_status is not None:
                self._service_status = service_status
            if next_window_at is not ...:
                self._next_window_at = next_window_at  # type: ignore[assignment]
            if now_override is not ...:
                self._now_override = now_override  # type: ignore[assignment]
            if universe is not None:
                self._universe = universe
            if subscribed is not None:
                self._subscribed = subscribed
            if login_ok is not None:
                self._login_ok = bool(login_ok)
            if subscribe_ok is not None:
                self._subscribe_ok = bool(subscribe_ok)
            if last_event_ts is not ...:
                if isinstance(last_event_ts, datetime):
                    parsed_last_event = last_event_ts
                    if parsed_last_event.tzinfo is None:
                        parsed_last_event = parsed_last_event.replace(
                            tzinfo=TAIPEI
                        )
                    else:
                        parsed_last_event = parsed_last_event.astimezone(
                            TAIPEI
                        )
                    self._last_event_ts = parsed_last_event
                else:
                    self._last_event_ts = parse_iso(last_event_ts)

    def start_recording(
        self,
        path: Path,
        *,
        append: bool = False,
    ) -> None:
        resolved_path = relative_path(path)
        writer = ServiceJsonlWriter(resolved_path, append=append)
        writer.start()
        with self._lock:
            if self._record_writer is not None:
                writer.close()
                raise RuntimeError("已有進行中的 JSONL 錄製")
            self._record_writer = writer
            self._record_path = resolved_path
            self._record_count = 0
            self._record_error = None

    def finish_recording(
        self,
        metadata: dict[str, Any] | None = None,
    ) -> int:
        with self._lock:
            writer = self._record_writer
            self._record_writer = None
        if writer is None:
            return self._record_count
        writer.close()
        final_metadata: dict[str, Any] | None = None
        with self._lock:
            self._record_count = writer.count
            self._record_error = writer.error
            if metadata is not None:
                final_metadata = copy.deepcopy(metadata)
                final_metadata["generated_at"] = iso_taipei()
                final_metadata["record_count"] = writer.count
        if final_metadata is not None:
            write_record_metadata(writer.path, final_metadata)
        return writer.count

    def process_event(
        self,
        event: dict[str, Any],
        *,
        market_event: bool = True,
    ) -> None:
        with self._lock:
            writer = self._record_writer
            if writer is not None:
                try:
                    writer.put(event)
                except Exception as exc:
                    self._record_error = exc
            self._detector.process_event(event)
            if market_event:
                self._last_event_ts = parse_iso(event.get("ts")) or taipei_now()

    def publish(self) -> dict[str, Any]:
        with self._lock:
            now_value = self._now_override or iso_taipei()
            wall_now = taipei_now()
            active_writer = self._record_writer
            if active_writer is not None and active_writer.error is not None:
                self._record_error = active_writer.error
            last_event_age_sec = (
                round(
                    max(
                        0.0,
                        (wall_now - self._last_event_ts).total_seconds(),
                    ),
                    1,
                )
                if self._last_event_ts is not None
                else None
            )
            service_status = self._service_status
            if service_status in {"armed", "live"}:
                if not self._login_ok or not self._subscribe_ok:
                    service_status = "error"
                elif service_status == "live" and (
                    last_event_age_sec is None
                    or last_event_age_sec > self._stale_after_seconds
                ):
                    service_status = "degraded"
            if (
                service_status != "replay"
                and self._record_error is not None
            ):
                service_status = "error"
            detector_status = (
                self._service_status
                if self._service_status
                in {"idle", "armed", "live", "closed", "replay"}
                else "idle"
            )
            state = self._detector.build_state(
                service_status=detector_status,
                now=now_value,
                next_window_at=self._next_window_at,
                universe=self._universe,
                subscribed=self._subscribed,
            )
            # detector 維持既有 enum；HTTP 契約由 service 額外揭露
            # error/degraded，不迫使判定模組認識傳輸層健康狀態。
            state["service_status"] = service_status
            writer = self._record_writer
            state.update(
                {
                    "recording": bool(writer is not None and writer.active),
                    "record_path": (
                        str(self._record_path)
                        if self._record_path is not None
                        else None
                    ),
                    "record_count": (
                        writer.count if writer is not None else self._record_count
                    ),
                    "last_event_age_sec": last_event_age_sec,
                    "login_ok": self._login_ok,
                    "subscribe_ok": self._subscribe_ok,
                }
            )
        self.shared.replace(state)
        return state


def publish_loop(runtime: ServiceRuntime, stop_event: threading.Event) -> None:
    last_error_at = 0.0
    while not stop_event.is_set():
        try:
            runtime.publish()
        except Exception as exc:  # pragma: no cover - 防禦 UI 發布不中斷資料源
            current = time.monotonic()
            if current - last_error_at >= 30:
                print(
                    f"state 發布 FAILED（{type(exc).__name__}）",
                    flush=True,
                )
                last_error_at = current
        stop_event.wait(PUBLISH_INTERVAL_SECONDS)


def load_replay(
    input_path: Path,
) -> tuple[dict[str, Any], list[tuple[datetime, dict[str, Any]]]]:
    if not input_path.is_file():
        raise FileNotFoundError(input_path)
    meta_path = input_path.with_suffix(".meta.json")
    metadata: dict[str, Any] = {}
    if meta_path.is_file():
        with meta_path.open("r", encoding="utf-8-sig") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            raise ValueError(f"{meta_path.name} 必須是 JSON object")
        metadata = loaded

    snapshot_observed_at = parse_iso(metadata.get("generated_at"))
    subscribed_codes = {
        str(code).strip()
        for code in metadata.get("subscribed_codes", [])
        if str(code).strip()
    }
    rows: list[tuple[datetime, dict[str, Any]]] = []
    stream_codes: set[str] = set()
    with input_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{input_path.name}:{line_number} 不是有效 JSON"
                ) from exc
            if not isinstance(event, dict):
                raise ValueError(
                    f"{input_path.name}:{line_number} 必須是 JSON object"
                )
            code = str(event.get("code") or "").strip()
            kind = str(event.get("kind") or "").strip()
            if not code or kind not in {"bidask", "tick", "snapshot"}:
                continue
            if kind != "snapshot":
                stream_codes.add(code)
            if subscribed_codes and code not in subscribed_codes:
                continue

            event = dict(event)
            event["code"] = code
            source_time = parse_iso(event.get("ts"))
            if kind == "snapshot" and snapshot_observed_at is not None:
                if "source_ts" not in event:
                    event["source_ts"] = str(event.get("ts") or "")
                event["ts"] = iso_taipei(snapshot_observed_at)
                event_time = snapshot_observed_at
            else:
                event_time = source_time
            if event_time is None:
                raise ValueError(
                    f"{input_path.name}:{line_number} 缺少有效 ts"
                )
            rows.append((event_time, event))

    if not subscribed_codes:
        subscribed_codes = stream_codes
        metadata = dict(metadata)
        metadata["subscribed_codes"] = sorted(subscribed_codes)
    rows.sort(key=lambda item: item[0])
    return metadata, rows


def infer_replay_context(
    metadata: dict[str, Any],
    rows: list[tuple[datetime, dict[str, Any]]],
) -> tuple[str, str, str, int, int]:
    session = str(metadata.get("session") or "").strip()
    if session not in {"preopen", "preclose"}:
        first_hour = rows[0][0].hour if rows else 8
        session = "preclose" if first_hour >= 12 else "preopen"
    default_start, default_end = default_clock(session)
    raw_window = metadata.get("window")
    window_start = (
        str(raw_window.get("start"))
        if isinstance(raw_window, dict) and raw_window.get("start")
        else default_start.strftime("%H:%M")
    )
    window_end = (
        str(raw_window.get("end"))
        if isinstance(raw_window, dict) and raw_window.get("end")
        else default_end.strftime("%H:%M")
    )
    stocks = metadata.get("stocks")
    stock_count = len(stocks) if isinstance(stocks, list) else 0
    subscribed_codes = metadata.get("subscribed_codes")
    subscribed_code_count = (
        len(subscribed_codes) if isinstance(subscribed_codes, list) else 0
    )
    universe = safe_int(metadata.get("universe_size"), stock_count)
    subscribed = safe_int(
        metadata.get("subscribed"),
        subscribed_code_count,
    )
    return session, window_start, window_end, universe, subscribed


def replay_worker(
    runtime: ServiceRuntime,
    rows: list[tuple[datetime, dict[str, Any]]],
    *,
    speed: float,
    stop_event: threading.Event,
    record_metadata: dict[str, Any] | None = None,
) -> None:
    replay_started_at = rows[0][0] if rows else None
    wall_started_at = time.monotonic()
    processed = 0
    try:
        for event_at, event in rows:
            if stop_event.is_set():
                return
            if replay_started_at is not None:
                # 以絕對 replay clock 排程，避免 Windows 對上萬個極短 wait
                # 各自向上取整，造成 --speed 20 反而拖成數分鐘。
                elapsed = max(
                    0.0,
                    (event_at - replay_started_at).total_seconds(),
                )
                target_at = wall_started_at + elapsed / speed
                remaining = target_at - time.monotonic()
                if remaining > 0 and stop_wait(stop_event, remaining):
                    return
            runtime.set_context(
                service_status="replay",
                now_override=iso_taipei(event_at),
                next_window_at=None,
            )
            runtime.process_event(event)
            processed += 1
    finally:
        if record_metadata is not None:
            runtime.finish_recording(record_metadata)

    state = runtime.publish()
    counts = state.get("counts", {})
    stocks = state.get("stocks", [])
    statuses = {
        str(stock.get("code")): stock.get("status")
        for stock in stocks
        if isinstance(stock, dict)
        and str(stock.get("code")) in {"2880", "3081", "8039"}
    }
    print(
        "replay 完成："
        f"事件={processed}；stocks={len(stocks)}；"
        "counts="
        + json.dumps(counts, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
    print(
        "replay 三檔="
        + json.dumps(statuses, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )


class LiveSourceError(RuntimeError):
    pass


def _configure_quiet_solace() -> None:
    import pysolace

    if getattr(pysolace.SolClient, "_auction_quiet_init", False):
        return
    original_init = pysolace.SolClient.__init__

    def quiet_init(
        instance: Any,
        log_level: Any = None,
        debug: bool = False,
    ) -> None:
        del log_level, debug
        original_init(
            instance,
            pysolace.SolLogLevel.SOLCLIENT_LOG_ERROR,
            False,
        )

    pysolace.SolClient.__init__ = quiet_init
    pysolace.SolClient._auction_quiet_init = True


def load_live_credentials() -> tuple[str, str]:
    import recorder

    try:
        env = recorder.load_dotenv_manually(BASE_DIR / ".env")
    except Exception as exc:
        raise LiveSourceError("missing credentials") from exc
    api_key = env.get("SHIOAJI_API_KEY", "").strip()
    secret_key = env.get("SHIOAJI_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        raise LiveSourceError("missing credentials")
    for secret in (api_key, secret_key):
        if secret not in recorder.REDACTIONS:
            recorder.REDACTIONS.append(secret)
    return api_key, secret_key


def live_metadata(
    *,
    session: str,
    start_at: datetime,
    end_at: datetime,
    universe: list[tuple[str, Any]],
    subscribed_codes: Iterable[str],
) -> dict[str, Any]:
    import recorder

    codes = list(subscribed_codes)
    return {
        "date": start_at.strftime("%Y%m%d"),
        "session": session,
        "window": {
            "start": start_at.isoformat(timespec="seconds"),
            "end": end_at.isoformat(timespec="seconds"),
        },
        "universe_size": len(universe),
        "universe": [code for code, _contract in universe],
        "subscribed": len(codes),
        "channels": "bidask",
        "channels_per_stock": 1,
        "sub_limit": MAX_SUBSCRIBED_STOCKS,
        "sub_limit_unit": "streams",
        "sub_limit_stocks": MAX_SUBSCRIBED_STOCKS,
        "subscribed_codes": codes,
        "stocks": [
            recorder.contract_meta(code, contract)
            for code, contract in universe
        ],
    }


def run_one_live_window(
    runtime: ServiceRuntime,
    *,
    session: str,
    start_at: datetime,
    end_at: datetime,
    universe_spec: str | None,
    stop_event: threading.Event,
    record_enabled: bool,
    record_out: Path | None,
) -> bool:
    """連線並處理單一窗口；成功完成 snapshot 回傳 True。"""
    import recorder

    _configure_quiet_solace()
    import shioaji as sj
    from shioaji.constant import QuoteType, QuoteVersion

    api_key, secret_key = load_live_credentials()
    api: Any | None = None
    active_subscriptions: list[tuple[Any, Any]] = []
    candidate_codes: set[str] = set()
    subscription_limit = threading.Event()
    detector_ready = threading.Event()
    universe: list[tuple[str, Any]] = []
    recording_metadata: dict[str, Any] | None = None
    snapshot_count = 0

    def on_event(
        resp_code: Any,
        event_code: Any,
        info: Any,
        event: Any,
    ) -> None:
        if recorder.is_subscription_limit_event(
            resp_code, event_code, info, event
        ):
            subscription_limit.set()

    def on_bidask(exchange: Any, bidask: Any) -> None:
        del exchange
        try:
            if not detector_ready.is_set():
                return
            code = recorder.normalize_code(getattr(bidask, "code", ""))
            if code not in candidate_codes:
                return
            raw_time = (
                getattr(bidask, "ts", None)
                if getattr(bidask, "ts", None) is not None
                else getattr(bidask, "datetime", None)
            )
            event_at = recorder.event_datetime(raw_time)
            if event_at is None:
                return
            aware_event_at = event_at.replace(tzinfo=TAIPEI)
            # locked 證據只允許窗口內事件，且不把窗口後 callback 倒灌。
            if not start_at <= aware_event_at < end_at:
                return
            record = recorder.bidask_record(bidask, code)
            runtime.process_event(record)
        except Exception as exc:  # pragma: no cover - 真實 callback 防禦
            print(
                f"BidAsk callback FAILED（{recorder.safe_error(exc)}）",
                flush=True,
            )

    try:
        try:
            with recorder.quiet_library_call():
                api = sj.Shioaji()
                accounts = api.login(
                    api_key=api_key,
                    secret_key=secret_key,
                    fetch_contract=True,
                    contracts_timeout=30_000,
                    subscribe_trade=False,
                )
                recorder.remember_sensitive_identifiers(accounts)
                del accounts
            print(
                "login OK（行情唯讀；CA=未啟用；未選帳號；未下單）",
                flush=True,
            )
            runtime.set_context(login_ok=True, subscribe_ok=False)
        except Exception as exc:
            raise LiveSourceError(
                f"login FAILED（{recorder.safe_error(exc)}）"
            ) from exc

        try:
            recorder.wait_for_contracts(api)
            universe, unresolved, _requested = recorder.requested_universe(
                api, universe_spec
            )
        except Exception as exc:
            raise LiveSourceError(
                f"標的宇宙 FAILED（{recorder.safe_error(exc)}）"
            ) from exc
        if not universe:
            raise LiveSourceError("標的宇宙為空")
        if unresolved:
            print(f"找不到合約 {len(unresolved)} 檔", flush=True)

        candidates = universe[:MAX_SUBSCRIBED_STOCKS]
        candidate_codes.update(code for code, _contract in candidates)
        metadata = live_metadata(
            session=session,
            start_at=start_at,
            end_at=end_at,
            universe=universe,
            subscribed_codes=candidate_codes,
        )
        if record_enabled:
            output_path = record_out or (
                BASE_DIR
                / "data"
                / f"auction_{start_at:%Y%m%d}.jsonl"
            )
            try:
                runtime.start_recording(output_path, append=True)
            except OSError as exc:
                raise LiveSourceError(
                    f"recording FAILED（{recorder.safe_error(exc)}）"
                ) from exc
            recording_metadata = metadata
        runtime.reset_detector(
            metadata,
            session=session,
            window_start=start_at.isoformat(timespec="seconds"),
            window_end=end_at.isoformat(timespec="seconds"),
            universe=len(universe),
            subscribed=len(candidate_codes),
        )
        runtime.set_context(
            service_status=(
                "live" if start_at <= taipei_now() < end_at else "armed"
            ),
            next_window_at=iso_taipei(start_at),
            now_override=None,
        )
        runtime.publish()
        detector_ready.set()

        api.quote.set_event_callback(on_event)
        api.quote.set_on_bidask_stk_v1_callback(on_bidask)
        successful_codes: list[str] = []
        for code, contract in candidates:
            if stop_event.is_set() or subscription_limit.is_set():
                break
            try:
                with recorder.quiet_library_call():
                    api.quote.subscribe(
                        contract,
                        quote_type=QuoteType.BidAsk,
                        intraday_odd=False,
                        version=QuoteVersion.v1,
                    )
                active_subscriptions.append((contract, QuoteType.BidAsk))
                successful_codes.append(code)
            except Exception as exc:
                print(
                    "BidAsk 訂閱停止"
                    f"（第 {len(successful_codes) + 1} 檔；"
                    f"{recorder.safe_error(exc)}）",
                    flush=True,
                )
                break
            stop_event.wait(SUBSCRIBE_EVENT_GRACE_SECONDS)

        if not successful_codes:
            candidate_codes.clear()
            if recording_metadata is not None:
                recording_metadata = live_metadata(
                    session=session,
                    start_at=start_at,
                    end_at=end_at,
                    universe=universe,
                    subscribed_codes=[],
                )
            runtime.set_context(subscribed=0, subscribe_ok=False)
            raise LiveSourceError("成功訂閱檔數為 0")
        if len(successful_codes) != len(candidate_codes):
            candidate_codes.intersection_update(successful_codes)
            metadata = live_metadata(
                session=session,
                start_at=start_at,
                end_at=end_at,
                universe=universe,
                subscribed_codes=successful_codes,
            )
            runtime.reset_detector(
                metadata,
                session=session,
                window_start=start_at.isoformat(timespec="seconds"),
                window_end=end_at.isoformat(timespec="seconds"),
                universe=len(universe),
                subscribed=len(successful_codes),
            )
        else:
            metadata = live_metadata(
                session=session,
                start_at=start_at,
                end_at=end_at,
                universe=universe,
                subscribed_codes=successful_codes,
            )
        if recording_metadata is not None:
            recording_metadata = metadata
        runtime.set_context(
            subscribed=len(successful_codes),
            subscribe_ok=(
                len(successful_codes) == len(universe)
                and len(universe) > 0
            ),
        )
        print(
            f"標的宇宙={len(universe)}；BidAsk 單通道訂閱="
            f"{len(successful_codes)} 檔",
            flush=True,
        )

        remaining = (start_at - taipei_now()).total_seconds()
        if remaining > 0:
            runtime.set_context(
                service_status="armed",
                next_window_at=iso_taipei(start_at),
            )
            if stop_wait(stop_event, remaining):
                return False
        if taipei_now() < end_at:
            runtime.set_context(
                service_status="live",
                next_window_at=None,
            )
            if stop_wait(
                stop_event,
                (end_at - taipei_now()).total_seconds(),
            ):
                return False

        detector_ready.clear()
        snapshot_at = end_at + timedelta(
            seconds=SNAPSHOT_AFTER_END_SECONDS
        )
        if stop_wait(
            stop_event,
            max(0.0, (snapshot_at - taipei_now()).total_seconds()),
        ):
            return False
        observed_at = taipei_now().replace(tzinfo=None)
        try:
            with recorder.quiet_library_call():
                raw_snapshots = api.snapshots(
                    [contract for _code, contract in universe],
                    timeout=30_000,
                )
            names = {
                code: recorder.normalize_code(getattr(contract, "name", ""))
                for code, contract in universe
            }
            for index, snapshot in enumerate(recorder.as_list(raw_snapshots)):
                fallback_code = (
                    universe[index][0] if index < len(universe) else ""
                )
                record = recorder.snapshot_record(
                    snapshot,
                    fallback_code,
                    names,
                    observed_at,
                )
                if record.get("code") not in candidate_codes:
                    continue
                runtime.process_event(record)
                snapshot_count += 1
        except Exception as exc:
            print(
                f"snapshot FAILED（{recorder.safe_error(exc)}）",
                flush=True,
            )
        next_start, _next_end = next_window_after(
            end_at,
            start_at.timetz().replace(tzinfo=None),
            end_at.timetz().replace(tzinfo=None),
        )
        runtime.set_context(
            service_status="closed",
            next_window_at=iso_taipei(next_start),
        )
        state = runtime.publish()
        print(
            f"窗口收口：snapshot={snapshot_count}/{len(candidate_codes)}；"
            "counts="
            + json.dumps(
                state.get("counts", {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        return True
    finally:
        detector_ready.clear()
        if recording_metadata is not None:
            final_metadata = dict(recording_metadata)
            final_metadata.update(
                {
                    "snapshot_count": snapshot_count,
                    "snapshot_requested": len(candidate_codes),
                    "snapshot_complete": (
                        snapshot_count == len(candidate_codes)
                    ),
                }
            )
            try:
                runtime.finish_recording(final_metadata)
            except Exception as exc:
                runtime.set_context(service_status="error")
                print(
                    f"record meta FAILED（{recorder.safe_error(exc)}）",
                    flush=True,
                )
        if api is not None:
            for contract, quote_type in reversed(active_subscriptions):
                try:
                    with recorder.quiet_library_call():
                        api.quote.unsubscribe(
                            contract,
                            quote_type=quote_type,
                            intraday_odd=False,
                            version=QuoteVersion.v1,
                        )
                except Exception:
                    pass
            try:
                with recorder.quiet_library_call():
                    api.logout()
                print("logout OK", flush=True)
            except Exception:
                print("logout FAILED", flush=True)


def live_worker(
    runtime: ServiceRuntime,
    *,
    session: str,
    start_clock: datetime_time,
    end_clock: datetime_time,
    universe_spec: str | None,
    stop_event: threading.Event,
    record_enabled: bool,
    record_out: Path | None,
) -> None:
    completed_a_window = False
    while not stop_event.is_set():
        now = taipei_now()
        start_at, end_at = upcoming_window(
            now,
            start_clock,
            end_clock,
        )
        prearm_at = start_at - timedelta(minutes=PREARM_MINUTES)
        if now < prearm_at:
            runtime.set_context(
                service_status=(
                    "closed" if completed_a_window else "idle"
                ),
                next_window_at=iso_taipei(start_at),
                now_override=None,
            )
            if stop_wait(
                stop_event,
                (prearm_at - now).total_seconds(),
            ):
                return

        while not stop_event.is_set():
            try:
                runtime.set_context(
                    login_ok=False,
                    subscribe_ok=False,
                    last_event_ts=None,
                )
                completed_a_window = run_one_live_window(
                    runtime,
                    session=session,
                    start_at=start_at,
                    end_at=end_at,
                    universe_spec=universe_spec,
                    stop_event=stop_event,
                    record_enabled=record_enabled,
                    record_out=record_out,
                )
                break
            except LiveSourceError as exc:
                print(str(exc), flush=True)
                runtime.set_context(
                    service_status="error",
                    next_window_at=iso_taipei(start_at),
                    now_override=None,
                )
                if taipei_now() >= (
                    end_at
                    + timedelta(seconds=SNAPSHOT_AFTER_END_SECONDS)
                ):
                    break
                if stop_wait(stop_event, LOGIN_RETRY_SECONDS):
                    return


def positive_speed(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--speed 必須為數字") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("--speed 必須大於 0")
    return result


def positive_seconds(value: str) -> float:
    try:
        result = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("秒數必須為數字") from exc
    if result <= 0:
        raise argparse.ArgumentTypeError("秒數必須大於 0")
    return result


def valid_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--port 必須為整數") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("--port 必須介於 1 與 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="台股試撮即時偵測器常駐服務（行情唯讀）"
    )
    parser.add_argument(
        "--replay",
        metavar="PATH",
        help="重播 recorder JSONL；未提供時使用 live Shioaji",
    )
    parser.add_argument(
        "--speed",
        type=positive_speed,
        default=20.0,
        help="replay 加速倍率（預設 20）",
    )
    recording = parser.add_mutually_exclusive_group()
    recording.add_argument(
        "--record-out",
        metavar="PATH",
        help=(
            "live 覆寫自動錄製路徑；replay 僅在明確提供此參數時錄製"
        ),
    )
    recording.add_argument(
        "--no-record",
        action="store_true",
        help="關閉 live 自動錄製（replay 預設即不錄製）",
    )
    parser.add_argument(
        "--stale-after-sec",
        type=positive_seconds,
        default=DEFAULT_STALE_AFTER_SECONDS,
        help=(
            "live 最後事件超過幾秒即標示 degraded"
            f"（預設 {DEFAULT_STALE_AFTER_SECONDS:g}）"
        ),
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"HTTP 綁定介面（預設 {DEFAULT_HOST}）",
    )
    parser.add_argument(
        "--port",
        type=valid_port,
        default=DEFAULT_PORT,
        help=f"HTTP port（預設 {DEFAULT_PORT}；可用 AUCTION_PORT 覆蓋）",
    )
    parser.add_argument(
        "--session",
        choices=("preopen", "preclose"),
        default="preopen",
        help="live 試撮窗口（預設 preopen）",
    )
    parser.add_argument("--start", type=parse_hhmm, help="live 開始 HH:MM")
    parser.add_argument("--end", type=parse_hhmm, help="live 結束 HH:MM")
    parser.add_argument(
        "--universe",
        help="live 自訂代碼或相對路徑清單；預設股期對應現貨",
    )
    args = parser.parse_args(argv)
    if (args.start is None) != (args.end is None):
        parser.error("--start 與 --end 必須一起提供")
    return args


def install_stop_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = parse_args(argv)
    stop_event = threading.Event()
    install_stop_handlers(stop_event)
    record_out = (
        relative_path(args.record_out) if args.record_out else None
    )

    if args.replay:
        input_path = relative_path(args.replay)
        try:
            metadata, rows = load_replay(input_path)
        except Exception as exc:
            print(
                f"replay 載入 FAILED（{type(exc).__name__}）",
                flush=True,
            )
            return 2
        (
            session,
            window_start,
            window_end,
            universe,
            subscribed,
        ) = infer_replay_context(metadata, rows)
        runtime = ServiceRuntime(
            session=session,
            window_start=window_start,
            window_end=window_end,
            service_status="replay",
            stale_after_seconds=args.stale_after_sec,
            record_path=record_out,
        )
        runtime.reset_detector(
            metadata,
            session=session,
            window_start=window_start,
            window_end=window_end,
            universe=universe,
            subscribed=subscribed,
        )
        runtime.set_context(
            service_status="replay",
            next_window_at=None,
            now_override=iso_taipei(rows[0][0]) if rows else None,
        )
        source_target = lambda: replay_worker(
            runtime,
            rows,
            speed=args.speed,
            stop_event=stop_event,
            record_metadata=metadata if record_out is not None else None,
        )
        source_name = "auction-replay-source"
        mode_label = f"replay={input_path.name}；speed={args.speed:g}"
    else:
        start_clock, end_clock = default_clock(args.session)
        if args.start is not None and args.end is not None:
            start_clock, end_clock = args.start, args.end
        start_at, _end_at = upcoming_window(
            taipei_now(),
            start_clock,
            end_clock,
        )
        runtime = ServiceRuntime(
            session=args.session,
            window_start=start_clock.strftime("%H:%M"),
            window_end=end_clock.strftime("%H:%M"),
            service_status="idle",
            stale_after_seconds=args.stale_after_sec,
        )
        runtime.set_context(next_window_at=iso_taipei(start_at))
        source_target = lambda: live_worker(
            runtime,
            session=args.session,
            start_clock=start_clock,
            end_clock=end_clock,
            universe_spec=args.universe,
            stop_event=stop_event,
            record_enabled=not args.no_record,
            record_out=record_out,
        )
        source_name = "auction-live-source"
        mode_label = (
            f"live；session={args.session}；"
            f"window={start_clock:%H:%M}-{end_clock:%H:%M}；"
            + (
                "recording=off"
                if args.no_record
                else "recording=auto"
            )
        )

    publisher = threading.Thread(
        target=publish_loop,
        args=(runtime, stop_event),
        name="auction-state-publisher",
        daemon=True,
    )
    publisher.start()

    try:
        import webserver

        server = webserver.start_server(
            runtime.shared,
            host=args.host,
            port=args.port,
        )
    except Exception as exc:
        stop_event.set()
        print(
            f"HTTP 啟動 FAILED（{type(exc).__name__}）",
            flush=True,
        )
        return 2

    source = threading.Thread(
        target=source_target,
        name=source_name,
        daemon=True,
    )
    source.start()
    print(
        f"試撮偵測器已啟動：http://{args.host}:{args.port}/；{mode_label}",
        flush=True,
    )

    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        stop_event.set()
        stopper = getattr(webserver, "stop_server", None)
        if callable(stopper):
            stopper(server)
        else:  # pragma: no cover - 舊介面相容
            server.shutdown()
            server.server_close()
        source.join(timeout=10)
        publisher.join(timeout=2)
    print("試撮偵測器已停止", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
