#!/usr/bin/env python
"""資料夾內常駐排程器：平日依台北時間啟動試撮 session。"""

from __future__ import annotations

import argparse
import json
import logging
import msvcrt
import os
import subprocess
import sys
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
RUN_SESSION_PATH = BASE_DIR / "run_session.py"
STATE_PATH = BASE_DIR / ".scheduler_state.json"
LOCK_PATH = BASE_DIR / ".scheduler.lock"
LOG_DIR = BASE_DIR / "log"
LOG_PATH = LOG_DIR / "scheduler.log"
ONCE_DASHBOARD_PATH = LOG_DIR / "scheduler_once_dashboard.html"

TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")
DEFAULT_TRIGGER_AT = clock_time(8, 25)
DEFAULT_SESSION = "preopen"
SESSION_CHOICES = ("preopen", "preclose")
POLL_SECONDS = 30.0
ERROR_RETRY_SECONDS = 10.0
# 若程式在 08:25 後才啟動，預設仍可在 09:00 前補啟動當日盤前 session。
TRIGGER_GRACE = timedelta(minutes=35)

LOGGER = logging.getLogger("folder_scheduler")


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(
                encoding="utf-8",
                errors="backslashreplace",
                line_buffering=True,
            )


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)
    LOGGER.addHandler(stream_handler)


def parse_clock(value: str) -> clock_time:
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "時間必須為有效的 24 小時制 HH:MM"
        ) from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="資料夾內常駐排程器（預設平日台北時間 08:25）"
    )
    parser.add_argument(
        "--at",
        dest="trigger_at",
        type=parse_clock,
        default=DEFAULT_TRIGGER_AT,
        metavar="HH:MM",
        help="每日觸發時間（台北時間；預設 08:25）",
    )
    parser.add_argument(
        "--session",
        choices=SESSION_CHOICES,
        default=DEFAULT_SESSION,
        help="傳給 run_session.py 的 session（預設 preopen）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="立刻執行一次離線 sample 串接驗證，不等待且不改每日狀態",
    )
    mode.add_argument(
        "--dry",
        action="store_true",
        help="只印出下一次觸發時間後結束",
    )
    return parser.parse_args(argv)


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def scheduled_datetime(day: date, trigger_at: clock_time) -> datetime:
    return datetime.combine(day, trigger_at, tzinfo=TAIPEI)


def next_trigger(now: datetime, trigger_at: clock_time) -> datetime:
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        candidate = scheduled_datetime(day, trigger_at)
        if candidate > now:
            return candidate
    raise RuntimeError("無法計算下一個平日觸發時間")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.is_file():
        return {}
    try:
        with STATE_PATH.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.error("無法讀取狀態檔，將視為尚未觸發：%s", exc)
        return {}
    if not isinstance(payload, dict):
        LOGGER.error("狀態檔不是 JSON object，將視為尚未觸發")
        return {}
    return payload


def write_state(payload: dict[str, Any]) -> None:
    temporary_path = STATE_PATH.with_name(f"{STATE_PATH.name}.tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary_path, STATE_PATH)


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def acquire_instance_lock() -> Any:
    """取得 Windows 檔案鎖；handle 必須在常駐期間保持開啟。"""
    handle = LOCK_PATH.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        handle.close()
        raise RuntimeError(
            "已有另一個 scheduler.py 常駐中，請勿重複啟動"
        ) from exc
    return handle


def release_instance_lock(handle: Any) -> None:
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def run_session(session: str, *, sample: bool) -> int:
    if not RUN_SESSION_PATH.is_file():
        raise FileNotFoundError(f"找不到 {RUN_SESSION_PATH.name}")

    command = [
        sys.executable,
        str(RUN_SESSION_PATH),
        "--session",
        session,
    ]
    if sample:
        command.extend(
            [
                "--sample",
                "--dashboard-out",
                str(ONCE_DASHBOARD_PATH),
            ]
        )

    LOGGER.info(
        "啟動 run_session.py：session=%s，sample=%s",
        session,
        sample,
    )
    completed = subprocess.run(
        command,
        cwd=BASE_DIR,
        env=child_environment(),
        check=False,
    )
    if completed.returncode == 0:
        LOGGER.info("run_session.py 完成（exit=0）")
    else:
        LOGGER.error(
            "run_session.py 失敗（exit=%s）；本排程器會繼續常駐",
            completed.returncode,
        )
    return completed.returncode


def is_due(
    now: datetime,
    trigger_at: clock_time,
    last_trigger_date: str | None,
) -> bool:
    if now.weekday() >= 5 or last_trigger_date == now.date().isoformat():
        return False
    scheduled = scheduled_datetime(now.date(), trigger_at)
    return scheduled <= now < scheduled + TRIGGER_GRACE


def run_forever(trigger_at: clock_time, session: str) -> int:
    try:
        lock_handle = acquire_instance_lock()
    except (OSError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1

    LOGGER.info(
        "資料夾內常駐排程已啟動：平日 %s（Asia/Taipei），session=%s",
        trigger_at.strftime("%H:%M"),
        session,
    )
    announced_trigger: datetime | None = None

    try:
        while True:
            try:
                now = taipei_now()
                state = load_state()
                last_trigger_date = state.get("last_trigger_date")

                if is_due(now, trigger_at, last_trigger_date):
                    scheduled = scheduled_datetime(now.date(), trigger_at)
                    state = {
                        "last_trigger_date": now.date().isoformat(),
                        "scheduled_at": scheduled.isoformat(timespec="seconds"),
                        "started_at": now.isoformat(timespec="seconds"),
                        "finished_at": None,
                        "session": session,
                        "status": "started",
                        "returncode": None,
                    }
                    # 先記錄已觸發，再啟動子程序，確保同一天只嘗試一次。
                    write_state(state)
                    try:
                        returncode = run_session(session, sample=False)
                    except Exception as exc:
                        state["status"] = "exception"
                        state["finished_at"] = taipei_now().isoformat(
                            timespec="seconds"
                        )
                        state["error_type"] = type(exc).__name__
                        write_state(state)
                        LOGGER.exception(
                            "執行 session 時發生例外；排程器繼續常駐"
                        )
                    else:
                        state["status"] = (
                            "success" if returncode == 0 else "failed"
                        )
                        state["returncode"] = returncode
                        state["finished_at"] = taipei_now().isoformat(
                            timespec="seconds"
                        )
                        write_state(state)
                    announced_trigger = None
                    continue

                upcoming = next_trigger(now, trigger_at)
                if upcoming != announced_trigger:
                    LOGGER.info(
                        "下一次觸發時間：%s（Asia/Taipei）",
                        upcoming.isoformat(sep=" ", timespec="seconds"),
                    )
                    announced_trigger = upcoming
                remaining = (upcoming - now).total_seconds()
                time.sleep(max(1.0, min(POLL_SECONDS, remaining)))
            except KeyboardInterrupt:
                LOGGER.info("收到中止指令，排程器已停止")
                return 130
            except Exception:
                LOGGER.exception(
                    "排程迴圈發生例外；%s 秒後繼續",
                    int(ERROR_RETRY_SECONDS),
                )
                time.sleep(ERROR_RETRY_SECONDS)
    finally:
        release_instance_lock(lock_handle)


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = parse_args(argv)
    configure_logging()

    if args.dry:
        upcoming = next_trigger(taipei_now(), args.trigger_at)
        print(
            "下一次觸發時間："
            f"{upcoming.isoformat(sep=' ', timespec='seconds')}"
            f"（Asia/Taipei，session={args.session}）"
        )
        return 0

    if args.once:
        LOGGER.info("--once：開始離線 sample 串接驗證")
        try:
            returncode = run_session(args.session, sample=True)
        except Exception:
            LOGGER.exception("--once 驗證發生例外")
            return 1
        if returncode == 0:
            LOGGER.info("--once：離線 sample 串接驗證成功")
        return returncode

    return run_forever(args.trigger_at, args.session)


if __name__ == "__main__":
    raise SystemExit(main())
