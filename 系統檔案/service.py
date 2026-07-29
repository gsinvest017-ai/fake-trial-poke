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
import shutil
import sys
import threading
import time
from datetime import date, datetime, time as datetime_time, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import control_state


# 必須在匯入 Shioaji 前降低第三方套件輸出敏感連線資訊的風險。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
TAIPEI = ZoneInfo("Asia/Taipei")
DEFAULT_HOST = "127.0.0.1"
MAX_SUBSCRIBED_STOCKS = 254
PREARM_MINUTES = 5
SNAPSHOT_AFTER_END_SECONDS = 5
POSTOPEN_MINUTES = 5
PUBLISH_INTERVAL_SECONDS = 0.2
SUBSCRIBE_EVENT_GRACE_SECONDS = 0.01
LOGIN_RETRY_SECONDS = 30.0
DEFAULT_STALE_AFTER_SECONDS = 10.0
TELEGRAM_NOTIFY_DEADLINE_SECONDS = 60.0
TODAY_RECORDING_SUCCESS_THRESHOLD = 100
HISTORY_RETENTION_DAYS = 3


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


def prune_history(
    history_dir: Path | None = None,
    retention_days: int | None = None,
) -> list[str]:
    """只回收 history 直屬的 YYYYMMDD 日期資料夾。"""
    root = Path(history_dir) if history_dir is not None else HISTORY_DIR
    keep_count = (
        HISTORY_RETENTION_DAYS
        if retention_days is None
        else retention_days
    )
    if keep_count < 1:
        raise ValueError("history retention days 必須大於 0")
    if not root.exists():
        return []

    resolved_root = root.resolve()
    date_dirs: list[Path] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:
        print(
            f"歷史資料回收 FAILED（{type(exc).__name__}）",
            flush=True,
        )
        return []
    for child in children:
        name = child.name
        if (
            len(name) != 8
            or not name.isascii()
            or not name.isdigit()
            or not child.is_dir()
            or child.is_symlink()
        ):
            continue
        try:
            if child.resolve().parent != resolved_root:
                continue
        except OSError:
            continue
        date_dirs.append(child)

    deleted: list[str] = []
    for old_dir in sorted(date_dirs, key=lambda path: path.name)[:-keep_count]:
        try:
            shutil.rmtree(old_dir)
        except OSError as exc:
            print(
                "歷史資料回收 FAILED："
                f"日期={old_dir.name}（{type(exc).__name__}）",
                flush=True,
            )
            continue
        deleted.append(old_dir.name)
        print(
            f"歷史資料回收：已刪除日期={old_dir.name}",
            flush=True,
        )
    return deleted


def default_live_record_path(recording_at: datetime) -> Path:
    """回傳 live 正式錄製按交易日分區的預設 JSONL 路徑。"""
    date_key = recording_at.strftime("%Y%m%d")
    return HISTORY_DIR / date_key / f"auction_{date_key}.jsonl"


def default_live_postopen_path(recording_at: datetime) -> Path:
    """回傳 live 開盤後續錄按交易日分區的預設 JSONL 路徑。"""
    date_key = recording_at.strftime("%Y%m%d")
    return HISTORY_DIR / date_key / f"auction_{date_key}_postopen.jsonl"


def default_live_result_path(recording_at: datetime) -> Path:
    """回傳既有 detector 每日收口判定的預設 JSON 路徑。"""
    date_key = recording_at.strftime("%Y%m%d")
    return HISTORY_DIR / date_key / f"result_{date_key}.json"


def paired_postopen_path(record_path: Path) -> Path:
    """明確覆寫主錄製路徑時，將續錄放在同目錄的配對檔。"""
    return record_path.with_name(
        f"{record_path.stem}_postopen{record_path.suffix}"
    )


@lru_cache(maxsize=64)
def _count_landed_events(
    path_text: str,
    file_size: int,
    modified_ns: int,
) -> int:
    """依檔案指紋快取非空行數，避免每次發布都重掃歷史主檔。"""
    del file_size, modified_ns
    try:
        with Path(path_text).open("rb") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def landed_event_count(path: Path) -> int:
    """回傳目前已實際落地的非空 JSONL 行數。"""
    try:
        stat = path.stat()
    except OSError:
        return 0
    if not path.is_file():
        return 0
    return _count_landed_events(
        str(path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
    )


def window_clock(
    value: Any,
    fallback: datetime_time,
) -> datetime_time:
    """接受 HH:MM 或 ISO datetime，正規化成台北窗口時刻。"""
    parsed = parse_iso(value)
    if parsed is not None:
        return parsed.timetz().replace(tzinfo=None)
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except (TypeError, ValueError):
        return fallback


def classify_today_recording_state(
    *,
    now: datetime,
    start_clock: datetime_time,
    end_clock: datetime_time,
    has_data: bool,
    record_count: int,
    service_status: str,
    recording: bool,
) -> str:
    """依交易日、窗口與落地筆數分類今日錄製結果。"""
    if now.tzinfo is None:
        current = now.replace(tzinfo=TAIPEI)
    else:
        current = now.astimezone(TAIPEI)
    if current.weekday() >= 5:
        return "waiting"

    start_at, end_at = window_on(
        current.date(),
        start_clock,
        end_clock,
    )
    if current < start_at:
        return "waiting"
    if (
        start_at <= current < end_at
        and service_status == "live"
        and recording
    ):
        return "recording"
    if current >= end_at:
        if (
            has_data
            and record_count > TODAY_RECORDING_SUCCESS_THRESHOLD
        ):
            return "success"
        return "missed"

    # 窗口仍在進行但尚未開始錄製，保留等待態直到窗口收口。
    return "waiting"


def build_today_recording(
    *,
    now: datetime,
    window_start: Any,
    window_end: Any,
    service_status: str,
    recording: bool,
    active_record_path: Path | None = None,
    active_record_count: int | None = None,
) -> dict[str, Any]:
    """建立可由落地檔案重建、不依賴程序存活期的今日錄製摘要。"""
    if now.tzinfo is None:
        current = now.replace(tzinfo=TAIPEI)
    else:
        current = now.astimezone(TAIPEI)
    start_clock = window_clock(window_start, datetime_time(8, 30))
    end_clock = window_clock(window_end, datetime_time(9, 0))
    date_key = current.strftime("%Y%m%d")
    main_path = HISTORY_DIR / date_key / f"auction_{date_key}.jsonl"
    resolved_active_path = (
        active_record_path.resolve()
        if active_record_path is not None
        else None
    )
    active_main = (
        resolved_active_path is not None
        and resolved_active_path == main_path.resolve()
        and active_record_count is not None
    )

    if active_main:
        landed_count = max(0, safe_int(active_record_count))
        has_data = main_path.is_file() and landed_count > 0
    else:
        landed_count = landed_event_count(main_path)
        has_data = landed_count > 0

    # 自訂輸出路徑不改變 has_data 的定義；但 live 期間仍揭露執行中計數。
    record_count = landed_count
    if recording and active_record_count is not None and not has_data:
        record_count = max(record_count, safe_int(active_record_count))

    return {
        "date": date_key,
        "window": f"{start_clock:%H:%M}–{end_clock:%H:%M}",
        "record_count": record_count,
        "has_data": has_data,
        "state": classify_today_recording_state(
            now=current,
            start_clock=start_clock,
            end_clock=end_clock,
            has_data=has_data,
            record_count=record_count,
            service_status=service_status,
            recording=recording,
        ),
        "is_trading_day": current.weekday() < 5,
    }


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
    auto_record_enabled: bool = True,
) -> dict[str, Any]:
    today_recording = build_today_recording(
        now=taipei_now(),
        window_start=window_start,
        window_end=window_end,
        service_status=status,
        recording=False,
    )
    return {
        "service_status": status,
        "session": session,
        "window": {"start": window_start, "end": window_end},
        "now": iso_taipei(),
        "next_window_at": next_window_at,
        "universe": 0,
        "subscribed": 0,
        "auto_record_enabled": auto_record_enabled,
        "recording": False,
        "record_path": None,
        "record_count": 0,
        "today_recording": today_recording,
        "last_event_age_sec": None,
        "login_ok": False,
        "subscribe_ok": False,
        "counts": {
            "suspected_fake": 0,
            "locked_held": 0,
            "touched": 0,
            "watching": 0,
            "anomaly": 0,
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


def write_detector_result(
    result_path: Path,
    state: dict[str, Any],
    metadata: dict[str, Any],
) -> Path:
    """將既有 AuctionDetector 的收口狀態原子寫入每日 result。"""
    raw_date = str(metadata.get("date") or "").strip()
    date_text = (
        f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        if len(raw_date) == 8 and raw_date.isdigit()
        else raw_date
    )
    result = {
        "date": date_text,
        "session": state.get("session"),
        "window": copy.deepcopy(state.get("window", {})),
        "universe_size": safe_int(
            metadata.get("universe_size"),
            safe_int(state.get("universe")),
        ),
        "subscribed": safe_int(state.get("subscribed")),
        "sub_limit": safe_int(metadata.get("sub_limit")),
        "generated_at": iso_taipei(),
        "detector": "AuctionDetector",
        "counts": copy.deepcopy(state.get("counts", {})),
        "stocks": copy.deepcopy(state.get("stocks", [])),
        "alerts": copy.deepcopy(state.get("alerts", [])),
        "anomaly_thresholds": copy.deepcopy(
            state.get("anomaly_thresholds", {})
        ),
        "fake_grade_thresholds": copy.deepcopy(
            state.get("fake_grade_thresholds", {})
        ),
    }
    resolved_path = relative_path(result_path)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = resolved_path.with_suffix(
        resolved_path.suffix + ".tmp"
    )
    with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary_path.replace(resolved_path)
    return resolved_path


def notify_telegram_result(
    result_path: Path,
    *,
    deadline_seconds: float = TELEGRAM_NOTIFY_DEADLINE_SECONDS,
) -> None:
    """安全送出已落地的盤前判定；任何通知問題都不影響主流程。"""
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    completed = threading.Event()
    outcome: dict[str, Any] = {}

    def send_result() -> None:
        try:
            import telegram_notify

            result_payload = json.loads(
                result_path.read_text(encoding="utf-8")
            )
            telegram_message = telegram_notify.build_telegram_message(
                result_payload
            )
            sent_count = telegram_notify.send_telegram_message(
                telegram_message,
                deadline=deadline,
            )
            outcome.update(
                {
                    "sent_count": sent_count,
                    "locked_count": len(
                        telegram_notify.locked_limit_up_stocks(
                            result_payload
                        )
                    ),
                    "message_length": len(telegram_message),
                }
            )
        except Exception as exc:
            outcome["error_type"] = type(exc).__name__
        finally:
            completed.set()

    try:
        sender_thread = threading.Thread(
            target=send_result,
            name="telegram-result-send",
            daemon=True,
        )
        sender_thread.start()
    except Exception as exc:
        print(
            f"TEL 通知失敗：{type(exc).__name__}",
            flush=True,
        )
        return

    remaining = max(0.0, deadline - time.monotonic())
    if not completed.wait(timeout=remaining):
        print("TEL 通知失敗：TimeoutError", flush=True)
        return

    error_type = outcome.get("error_type")
    if error_type is not None:
        print(f"TEL 通知失敗：{error_type}", flush=True)
        return

    if outcome.get("sent_count") is None:
        print("TEL 憑證未設定，略過發送", flush=True)
        return

    print(
        "TEL 通知成功："
        f"曾鎖漲停={outcome['locked_count']}；"
        f"訊息字數={outcome['message_length']}",
        flush=True,
    )


def start_telegram_result_notification(result_path: Path) -> None:
    """以 daemon 執行緒送出通知，live 收口主流程不等待結果。"""
    try:
        notification_thread = threading.Thread(
            target=notify_telegram_result,
            args=(result_path,),
            name="telegram-result-notify",
            daemon=True,
        )
        notification_thread.start()
    except Exception as exc:
        print(
            f"TEL 通知失敗：{type(exc).__name__}",
            flush=True,
        )


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
        auto_record_enabled: bool = True,
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
        self._auto_record_enabled = bool(auto_record_enabled)
        self._last_event_ts: datetime | None = None
        self._stale_after_seconds = stale_after_seconds
        self._record_writer: ServiceJsonlWriter | None = None
        self._record_path: Path | None = None
        self._record_count = 0
        self._record_base_count = 0
        self._aux_record_writer: ServiceJsonlWriter | None = None
        self._aux_record_path: Path | None = None
        self._record_error: BaseException | None = None
        self.shared = SharedState(
            empty_state(
                status=service_status,
                session=session,
                window_start=window_start,
                window_end=window_end,
                next_window_at=None,
                auto_record_enabled=self._auto_record_enabled,
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

    def set_auto_record_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._auto_record_enabled = bool(enabled)

    def start_recording(
        self,
        path: Path,
        *,
        append: bool = False,
    ) -> None:
        resolved_path = relative_path(path)
        base_count = landed_event_count(resolved_path) if append else 0
        writer = ServiceJsonlWriter(resolved_path, append=append)
        writer.start()
        with self._lock:
            if self._record_writer is not None:
                writer.close()
                raise RuntimeError("已有進行中的 JSONL 錄製")
            self._record_writer = writer
            self._record_path = resolved_path
            self._record_count = 0
            self._record_base_count = base_count
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

    def set_aux_recording(
        self,
        writer: ServiceJsonlWriter,
        path: Path,
    ) -> None:
        """登記同一 live session 的開盤後 writer，供 UI 誠實顯示。"""
        with self._lock:
            if self._aux_record_writer is not None:
                raise RuntimeError("已有開盤後 JSONL 錄製")
            self._aux_record_writer = writer
            self._aux_record_path = relative_path(path)

    def finish_aux_recording(self, writer: ServiceJsonlWriter) -> None:
        with self._lock:
            if self._aux_record_writer is not writer:
                return
            self._aux_record_writer = None
            self._record_path = self._aux_record_path
            self._record_count = writer.count
            if writer.error is not None:
                self._record_error = writer.error

    def record_event(self, event: dict[str, Any]) -> None:
        """只落地事件，不送進 detector；供開盤後續錄使用。"""
        with self._lock:
            writer = self._record_writer
            if writer is not None:
                try:
                    writer.put(event)
                except Exception as exc:
                    self._record_error = exc

    def process_event(
        self,
        event: dict[str, Any],
        *,
        market_event: bool = True,
        record_event: bool = True,
    ) -> None:
        with self._lock:
            if record_event:
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
            active_writer = self._record_writer or self._aux_record_writer
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
            writer = self._record_writer or self._aux_record_writer
            active_record_path = (
                self._record_path
                if self._record_writer is not None
                else self._aux_record_path
                if self._aux_record_writer is not None
                else self._record_path
            )
            state.update(
                {
                    "recording": bool(writer is not None and writer.active),
                    "auto_record_enabled": self._auto_record_enabled,
                    "record_path": (
                        str(active_record_path)
                        if active_record_path is not None
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
            active_record_count = (
                self._record_base_count + self._record_writer.count
                if self._record_writer is not None
                else self._aux_record_writer.count
                if self._aux_record_writer is not None
                else None
            )
            state["today_recording"] = build_today_recording(
                now=parse_iso(now_value) or wall_now,
                window_start=state.get("window", {}).get("start"),
                window_end=state.get("window", {}).get("end"),
                service_status=service_status,
                recording=state["recording"],
                active_record_path=active_record_path,
                active_record_count=active_record_count,
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


class ControlActionError(RuntimeError):
    """A valid control request that cannot be performed right now."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 409,
        error_code: str = "control_rejected",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code


class RecordingControl:
    """Coordinate persistent policy and one cancellable source session."""

    def __init__(
        self,
        runtime: ServiceRuntime,
        *,
        mode: str,
        auto_record_enabled: bool,
        session: str,
        start_clock: datetime_time,
        end_clock: datetime_time,
    ) -> None:
        if mode not in {"live", "replay"}:
            raise ValueError("mode 必須是 live 或 replay")
        self.runtime = runtime
        self.mode = mode
        self.session = session
        self.start_clock = start_clock
        self.end_clock = end_clock
        self._lock = threading.RLock()
        self._changed = threading.Event()
        self._shutdown = False
        self._auto_record_enabled = bool(auto_record_enabled)
        self._decision_day = taipei_now().date()
        self._day_enabled = self._auto_record_enabled
        self._manual_day: date | None = None
        self._manual_enabled: bool | None = None
        self._replay_pending = mode == "replay"
        self._active_stop: threading.Event | None = None
        self._active_done: threading.Event | None = None

    def _refresh_current_day_locked(self, today: date) -> None:
        if self._decision_day == today:
            return
        self._decision_day = today
        self._day_enabled = self._auto_record_enabled
        if self._manual_day != today:
            self._manual_day = None
            self._manual_enabled = None

    def should_run_live(self, target_day: date) -> bool:
        with self._lock:
            today = taipei_now().date()
            self._refresh_current_day_locked(today)
            if self._manual_day == target_day:
                return bool(self._manual_enabled)
            if target_day == today:
                return self._day_enabled
            return self._auto_record_enabled

    def wait_for_change(
        self,
        service_stop_event: threading.Event,
        timeout: float,
    ) -> bool:
        """Wait interruptibly; True means policy changed or service stopped."""
        deadline = time.monotonic() + max(0.0, timeout)
        while not service_stop_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._changed.wait(min(1.0, remaining)):
                self._changed.clear()
                return True
        return True

    def _begin_session_locked(self) -> threading.Event:
        stop_event = threading.Event()
        self._active_stop = stop_event
        self._active_done = threading.Event()
        return stop_event

    def begin_live_session(self, target_day: date) -> threading.Event | None:
        with self._lock:
            if (
                self._shutdown
                or self._active_stop is not None
                or not self.should_run_live(target_day)
            ):
                return None
            return self._begin_session_locked()

    def begin_replay_session(self) -> threading.Event | None:
        with self._lock:
            if (
                self._shutdown
                or self._active_stop is not None
                or not self._replay_pending
            ):
                return None
            self._replay_pending = False
            return self._begin_session_locked()

    def end_session(self, stop_event: threading.Event) -> None:
        with self._lock:
            if self._active_stop is not stop_event:
                return
            done = self._active_done
            self._active_stop = None
            self._active_done = None
            if done is not None:
                done.set()
            self._changed.set()

    def shutdown(self) -> None:
        with self._lock:
            self._shutdown = True
            self._replay_pending = False
            active_stop = self._active_stop
            if active_stop is not None:
                active_stop.set()
            self._changed.set()

    def _set_auto(self, enabled: bool) -> dict[str, Any]:
        written = control_state.set_auto_record_enabled(enabled)
        with self._lock:
            self._auto_record_enabled = bool(
                written["auto_record_enabled"]
            )
            # 今天的決策已在啟動/換日時快照；set_auto 是「明天起」。
            self._changed.set()
        self.runtime.set_auto_record_enabled(enabled)
        return self.runtime.publish()

    def _stop(self) -> dict[str, Any]:
        with self._lock:
            today = taipei_now().date()
            self._refresh_current_day_locked(today)
            if self.mode == "live":
                self._manual_day = today
                self._manual_enabled = False
            else:
                self._replay_pending = False
            active_stop = self._active_stop
            active_done = self._active_done
            if active_stop is not None:
                active_stop.set()
            self._changed.set()

        if active_done is not None and not active_done.wait(timeout=20.0):
            raise ControlActionError(
                "錄製停止逾時；服務仍在安全收口，請稍後重試",
                status_code=503,
                error_code="stop_timeout",
            )
        self.runtime.set_context(
            service_status="closed",
            subscribed=0,
            login_ok=False,
            subscribe_ok=False,
            last_event_ts=None,
        )
        return self.runtime.publish()

    def _start(self) -> dict[str, Any]:
        now = taipei_now()
        with self._lock:
            if self._shutdown:
                raise ControlActionError(
                    "服務正在停止",
                    status_code=503,
                    error_code="service_stopping",
                )
            if self._active_stop is not None:
                return self.runtime.publish()
            if self.mode == "replay":
                self._replay_pending = True
                self._changed.set()
                self.runtime.set_context(service_status="replay")
                return self.runtime.publish()

            if now.weekday() >= 5:
                raise ControlActionError("非交易日，無法開始今日錄製")
            start_at, end_at = window_on(
                now.date(),
                self.start_clock,
                self.end_clock,
            )
            earliest = start_at - timedelta(minutes=PREARM_MINUTES)
            latest = end_at + timedelta(
                seconds=SNAPSHOT_AFTER_END_SECONDS
            )
            if now < earliest or now > latest:
                raise ControlActionError(
                    "目前不在可立即開始的錄製時間窗"
                    f"（{earliest:%H:%M}–{latest:%H:%M:%S}）"
                )
            self._manual_day = now.date()
            self._manual_enabled = True
            self._changed.set()
        self.runtime.set_context(
            service_status="idle",
            next_window_at=iso_taipei(start_at),
            now_override=None,
        )
        return self.runtime.publish()

    def handle_request(
        self,
        action: str,
        enabled: bool | None,
    ) -> dict[str, Any]:
        if action == "set_auto":
            if type(enabled) is not bool:
                raise ControlActionError(
                    "enabled 必須是 boolean",
                    status_code=400,
                    error_code="invalid_enabled",
                )
            return self._set_auto(enabled)
        if action == "stop":
            return self._stop()
        if action == "start":
            return self._start()
        raise ControlActionError(
            "不支援的控制動作",
            status_code=400,
            error_code="invalid_action",
        )


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
    result_path: Path | None = None,
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
    if result_path is not None:
        write_detector_result(
            result_path,
            state,
            record_metadata or {},
        )
    counts = state.get("counts", {})
    stocks = state.get("stocks", [])
    statuses = {
        str(stock.get("code")): stock.get("status")
        for stock in stocks
        if isinstance(stock, dict)
        and str(stock.get("code")) in {"2880", "3081", "8039"}
    }
    anomaly_evidence = {
        str(stock.get("code")): {
            "anomalies": stock.get("anomalies", []),
            "anomaly_score": stock.get("anomaly_score", 0),
            "bid0_peak_volume": stock.get("bid0_peak_volume"),
            "final_window_bid0_volume": stock.get(
                "final_window_bid0_volume"
            ),
            "bid0_withdraw_pct": stock.get("bid0_withdraw_pct"),
            "bid0_swing_pct": stock.get("bid0_swing_pct"),
            "reference_open_gap_pct": stock.get(
                "reference_open_gap_pct"
            ),
        }
        for stock in stocks
        if isinstance(stock, dict)
        and str(stock.get("code")) in {"6488", "2481", "6147"}
    }
    grade_evidence = {
        str(stock.get("code")): {
            "max_bid0_volume": stock.get("max_bid0_volume"),
            "lock_duration_sec": stock.get("lock_duration_sec"),
            "open_gap_pct": stock.get("open_gap_pct"),
            "open_gap_ref_pct": stock.get("open_gap_ref_pct"),
            "grade": stock.get("grade"),
        }
        for stock in stocks
        if isinstance(stock, dict)
        and str(stock.get("code")) in {"2303", "8039", "3653"}
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
    print(
        "replay 異常三檔="
        + json.dumps(
            anomaly_evidence,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )
    print(
        f"replay 分級佐證（日期={state.get('date')}）="
        + json.dumps(
            grade_evidence,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        flush=True,
    )


def replay_control_worker(
    runtime: ServiceRuntime,
    rows: list[tuple[datetime, dict[str, Any]]],
    metadata: dict[str, Any],
    *,
    speed: float,
    service_stop_event: threading.Event,
    control: RecordingControl,
    record_out: Path | None = None,
    result_path: Path | None = None,
) -> None:
    """Run/restart replay sessions without taking down the local HTTP UI."""
    session, window_start, window_end, universe, subscribed = (
        infer_replay_context(metadata, rows)
    )
    while not service_stop_event.is_set():
        session_stop = control.begin_replay_session()
        if session_stop is None:
            control.wait_for_change(service_stop_event, 60.0)
            continue
        try:
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
                login_ok=False,
                subscribe_ok=False,
                last_event_ts=None,
            )
            if record_out is not None:
                runtime.start_recording(record_out)
            replay_worker(
                runtime,
                rows,
                speed=speed,
                stop_event=session_stop,
                record_metadata=(
                    metadata if record_out is not None else None
                ),
                result_path=result_path,
            )
        except Exception as exc:
            runtime.set_context(service_status="error")
            print(
                f"replay 執行 FAILED（{type(exc).__name__}）",
                flush=True,
            )
        finally:
            control.end_session(session_stop)


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
    result_out: Path | None = None,
) -> bool:
    """連線並處理盤前、開盤 snapshot 與五分鐘開盤後續錄。"""
    import recorder

    _configure_quiet_solace()
    import shioaji as sj
    from shioaji.constant import QuoteType, QuoteVersion

    api_key, secret_key = load_live_credentials()
    api: Any | None = None
    active_subscriptions: list[tuple[Any, Any]] = []
    candidate_codes: set[str] = set()
    subscription_limit = threading.Event()
    callbacks_ready = threading.Event()
    universe: list[tuple[str, Any]] = []
    names: dict[str, str] = {}
    recording_metadata: dict[str, Any] | None = None
    postopen_metadata: dict[str, Any] | None = None
    postopen_writer: ServiceJsonlWriter | None = None
    postopen_path: Path | None = None
    snapshot_count = 0
    postopen_snapshot_count = 0
    postopen_end_at = end_at + timedelta(
        minutes=POSTOPEN_MINUTES if session == "preopen" else 0
    )

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
            if not callbacks_ready.is_set():
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
            record = recorder.bidask_record(bidask, code)
            # detector 只吃盤前窗口；09:00–09:05 另檔保存，不倒灌判定。
            if start_at <= aware_event_at < end_at:
                runtime.process_event(record)
            elif (
                end_at <= aware_event_at <= postopen_end_at
                and postopen_writer is not None
            ):
                postopen_writer.put(record)
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
            output_path = record_out or default_live_record_path(start_at)
            try:
                runtime.start_recording(output_path, append=True)
                recording_metadata = metadata
                if session == "preopen":
                    postopen_path = (
                        paired_postopen_path(record_out)
                        if record_out is not None
                        else default_live_postopen_path(start_at)
                    )
                    postopen_writer = ServiceJsonlWriter(
                        postopen_path,
                        append=True,
                    )
                    postopen_writer.start()
                    runtime.set_aux_recording(
                        postopen_writer,
                        postopen_path,
                    )
            except OSError as exc:
                raise LiveSourceError(
                    f"recording FAILED（{recorder.safe_error(exc)}）"
                ) from exc
            if postopen_writer is not None:
                postopen_metadata = live_metadata(
                    session=session,
                    start_at=end_at,
                    end_at=postopen_end_at,
                    universe=universe,
                    subscribed_codes=candidate_codes,
                )
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
        callbacks_ready.set()

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
        if postopen_metadata is not None:
            postopen_metadata = live_metadata(
                session=session,
                start_at=end_at,
                end_at=postopen_end_at,
                universe=universe,
                subscribed_codes=successful_codes,
            )
        runtime.set_context(
            subscribed=len(successful_codes),
            subscribe_ok=(
                len(successful_codes) == len(candidates)
                and len(candidates) > 0
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
                if not record.get("code"):
                    continue
                runtime.record_event(record)
                if record.get("code") in candidate_codes:
                    runtime.process_event(record, record_event=False)
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
        if recording_metadata is not None:
            final_metadata = dict(recording_metadata)
            final_metadata.update(
                {
                    "snapshot_count": snapshot_count,
                    "snapshot_requested": len(universe),
                    "snapshot_complete": snapshot_count == len(universe),
                }
            )
            try:
                runtime.finish_recording(final_metadata)
                recording_metadata = None
                result_path = (
                    result_out
                    if result_out is not None
                    else default_live_result_path(start_at)
                )
                written_result = write_detector_result(
                    result_path,
                    state,
                    final_metadata,
                )
            except Exception as exc:
                raise LiveSourceError(
                    "盤前 meta/result 落地 FAILED"
                ) from exc
            print(f"detector result={written_result}", flush=True)
            if session == "preopen":
                start_telegram_result_notification(written_result)
        print(
            f"窗口收口：snapshot={snapshot_count}/{len(universe)}；"
            "counts="
            + json.dumps(
                state.get("counts", {}),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )

        if postopen_writer is not None:
            postopen_snapshot_at = postopen_end_at + timedelta(
                seconds=SNAPSHOT_AFTER_END_SECONDS
            )
            if stop_wait(
                stop_event,
                max(
                    0.0,
                    (postopen_snapshot_at - taipei_now()).total_seconds(),
                ),
            ):
                return False
            callbacks_ready.clear()
            postopen_observed_at = taipei_now().replace(tzinfo=None)
            try:
                with recorder.quiet_library_call():
                    raw_postopen_snapshots = api.snapshots(
                        [contract for _code, contract in universe],
                        timeout=30_000,
                    )
                for index, snapshot in enumerate(
                    recorder.as_list(raw_postopen_snapshots)
                ):
                    fallback_code = (
                        universe[index][0] if index < len(universe) else ""
                    )
                    record = recorder.snapshot_record(
                        snapshot,
                        fallback_code,
                        names,
                        postopen_observed_at,
                    )
                    if not record.get("code"):
                        continue
                    postopen_writer.put(record)
                    postopen_snapshot_count += 1
            except Exception as exc:
                print(
                    "postopen snapshot FAILED"
                    f"（{recorder.safe_error(exc)}）",
                    flush=True,
                )

            postopen_writer.close()
            runtime.finish_aux_recording(postopen_writer)
            if postopen_metadata is not None and postopen_path is not None:
                final_postopen_metadata = dict(postopen_metadata)
                final_postopen_metadata.update(
                    {
                        "snapshot_count": postopen_snapshot_count,
                        "snapshot_requested": len(universe),
                        "snapshot_complete": (
                            postopen_snapshot_count == len(universe)
                        ),
                        "generated_at": iso_taipei(),
                        "record_count": postopen_writer.count,
                    }
                )
                try:
                    write_record_metadata(
                        postopen_path,
                        final_postopen_metadata,
                    )
                except Exception as exc:
                    raise LiveSourceError(
                        "postopen meta 落地 FAILED"
                    ) from exc
            if postopen_writer.error is not None:
                raise LiveSourceError("postopen recording FAILED")
            print(
                "開盤後續錄收口："
                f"path={postopen_path}；"
                f"records={postopen_writer.count}；"
                f"snapshot={postopen_snapshot_count}/{len(universe)}",
                flush=True,
            )
            postopen_writer = None
            postopen_metadata = None
        return True
    finally:
        callbacks_ready.clear()
        if recording_metadata is not None:
            final_metadata = dict(recording_metadata)
            final_metadata.update(
                {
                    "snapshot_count": snapshot_count,
                    "snapshot_requested": len(universe),
                    "snapshot_complete": (
                        snapshot_count == len(universe)
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
        if postopen_writer is not None:
            postopen_writer.close()
            runtime.finish_aux_recording(postopen_writer)
            if postopen_metadata is not None and postopen_path is not None:
                final_postopen_metadata = dict(postopen_metadata)
                final_postopen_metadata.update(
                    {
                        "snapshot_count": postopen_snapshot_count,
                        "snapshot_requested": len(universe),
                        "snapshot_complete": (
                            postopen_snapshot_count == len(universe)
                        ),
                        "generated_at": iso_taipei(),
                        "record_count": postopen_writer.count,
                    }
                )
                try:
                    write_record_metadata(
                        postopen_path,
                        final_postopen_metadata,
                    )
                except Exception as exc:
                    runtime.set_context(service_status="error")
                    print(
                        "postopen meta FAILED"
                        f"（{recorder.safe_error(exc)}）",
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
    result_out: Path | None = None,
    control: RecordingControl | None = None,
) -> None:
    completed_a_window = False
    while not stop_event.is_set():
        now = taipei_now()
        start_at, end_at = upcoming_window(
            now,
            start_clock,
            end_clock,
        )
        if (
            control is not None
            and not control.should_run_live(start_at.date())
        ):
            runtime.set_context(
                service_status=(
                    "closed" if completed_a_window else "idle"
                ),
                next_window_at=iso_taipei(start_at),
                now_override=None,
                subscribed=0,
                login_ok=False,
                subscribe_ok=False,
                last_event_ts=None,
            )
            runtime.publish()
            control.wait_for_change(stop_event, 30.0)
            continue
        prearm_at = start_at - timedelta(minutes=PREARM_MINUTES)
        if now < prearm_at:
            runtime.set_context(
                service_status=(
                    "closed" if completed_a_window else "idle"
                ),
                next_window_at=iso_taipei(start_at),
                now_override=None,
            )
            wait_seconds = (prearm_at - now).total_seconds()
            if control is not None:
                if control.wait_for_change(stop_event, wait_seconds):
                    continue
            elif stop_wait(stop_event, wait_seconds):
                return

        while not stop_event.is_set():
            session_stop = (
                control.begin_live_session(start_at.date())
                if control is not None
                else stop_event
            )
            if session_stop is None:
                break
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
                    stop_event=session_stop,
                    record_enabled=record_enabled,
                    record_out=record_out,
                    result_out=result_out,
                )
                if completed_a_window:
                    prune_history()
                if control is not None:
                    control.end_session(session_stop)
                break
            except LiveSourceError as exc:
                if control is not None:
                    control.end_session(session_stop)
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
                if control is not None:
                    if control.wait_for_change(
                        stop_event,
                        LOGIN_RETRY_SECONDS,
                    ):
                        break
                elif stop_wait(stop_event, LOGIN_RETRY_SECONDS):
                    return
            except BaseException:
                if control is not None:
                    control.end_session(session_stop)
                raise


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
        "--result-out",
        metavar="PATH",
        help=(
            "覆寫 detector result 路徑；live 錄製預設自動寫每日 result"
        ),
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
    try:
        auto_record_enabled = control_state.get_auto_record_enabled()
    except control_state.ControlStateError as exc:
        # 控制檔異常時 fail closed，避免未經允許連線訂閱。
        auto_record_enabled = False
        print(f"錄製控制狀態 FAILED（{type(exc).__name__}）", flush=True)
    record_out = (
        relative_path(args.record_out) if args.record_out else None
    )
    result_out = (
        relative_path(args.result_out) if args.result_out else None
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
            auto_record_enabled=auto_record_enabled,
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
        replay_start_clock, replay_end_clock = default_clock(session)
        control = RecordingControl(
            runtime,
            mode="replay",
            auto_record_enabled=auto_record_enabled,
            session=session,
            start_clock=replay_start_clock,
            end_clock=replay_end_clock,
        )
        source_target = lambda: replay_control_worker(
            runtime,
            rows,
            metadata,
            speed=args.speed,
            service_stop_event=stop_event,
            control=control,
            record_out=record_out,
            result_path=result_out,
        )
        source_name = "auction-replay-source"
        mode_label = f"replay={input_path.name}；speed={args.speed:g}"
    else:
        prune_history()
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
            auto_record_enabled=auto_record_enabled,
        )
        runtime.set_context(next_window_at=iso_taipei(start_at))
        control = RecordingControl(
            runtime,
            mode="live",
            auto_record_enabled=auto_record_enabled,
            session=args.session,
            start_clock=start_clock,
            end_clock=end_clock,
        )
        source_target = lambda: live_worker(
            runtime,
            session=args.session,
            start_clock=start_clock,
            end_clock=end_clock,
            universe_spec=args.universe,
            stop_event=stop_event,
            record_enabled=not args.no_record,
            record_out=record_out,
            result_out=result_out,
            control=control,
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
            control_handler=control.handle_request,
        )
    except Exception as exc:
        stop_event.set()
        control.shutdown()
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
        control.shutdown()
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
