#!/usr/bin/env python
"""手動備援看門狗；正式主入口是 schedule_morning.ps1 的 Windows 排程。

請勿讓本程式與「假試撮盤前監控」同時常駐。沒有 Task Scheduler 的環境
才使用本程式確保同一個 service.py 已在 PORT 8900 提供 UI 與 live 錄製。
"""

from __future__ import annotations

import argparse
import json
import logging
import msvcrt
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Any

import control_state


BASE_DIR = Path(__file__).resolve().parent
SERVICE_PATH = BASE_DIR / "service.py"
LOCK_PATH = BASE_DIR / ".scheduler.lock"
LOG_DIR = BASE_DIR / "log"
LOG_PATH = LOG_DIR / "scheduler.log"

TAIPEI = timezone(timedelta(hours=8), name="Asia/Taipei")
DEFAULT_CHECK_AT = clock_time(8, 20)
SERVICE_HOST = "127.0.0.1"
SERVICE_PORT = 8900
POLL_SECONDS = 30.0
ERROR_RETRY_SECONDS = 10.0
STARTUP_WAIT_SECONDS = 10.0
HEALTHY_DRIVER_STATUSES = {"idle", "armed", "closed"}

LOGGER = logging.getLogger("service_watchdog")


@dataclass(frozen=True)
class ServiceHealth:
    listening: bool
    api_ok: bool
    healthy: bool
    service_status: str | None
    source_alive: bool
    recording: bool
    login_circuit_open: bool
    detail: str | None = None


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
        description=(
            "平日定時確認即時偵測器服務的 live 驅動與錄製健康，"
            "必要時啟動 service.py"
        )
    )
    parser.add_argument(
        "--at",
        dest="check_at",
        type=parse_clock,
        default=DEFAULT_CHECK_AT,
        metavar="HH:MM",
        help="每日檢查時間（台北時間；預設 08:20）",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--once",
        action="store_true",
        help="立即確保服務啟動一次後結束",
    )
    mode.add_argument(
        "--dry",
        action="store_true",
        help="只印出下一次檢查／必要時啟動服務的時點後結束",
    )
    return parser.parse_args(argv)


def taipei_now() -> datetime:
    return datetime.now(TAIPEI)


def scheduled_datetime(day: date, check_at: clock_time) -> datetime:
    return datetime.combine(day, check_at, tzinfo=TAIPEI)


def next_check(now: datetime, check_at: clock_time) -> datetime:
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        candidate = scheduled_datetime(day, check_at)
        if candidate > now:
            return candidate
    raise RuntimeError("無法計算下一個平日檢查時間")


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def service_is_listening() -> bool:
    try:
        with socket.create_connection(
            (SERVICE_HOST, SERVICE_PORT),
            timeout=1.0,
        ):
            return True
    except OSError:
        return False


def get_service_health() -> ServiceHealth:
    listening = service_is_listening()
    try:
        with urllib.request.urlopen(
            f"http://{SERVICE_HOST}:{SERVICE_PORT}/api/state",
            timeout=2.0,
        ) as response:
            if int(getattr(response, "status", 200)) != 200:
                raise RuntimeError("api/state did not return HTTP 200")
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        urllib.error.URLError,
    ) as exc:
        return ServiceHealth(
            listening=listening,
            api_ok=False,
            healthy=False,
            service_status=None,
            source_alive=False,
            recording=False,
            login_circuit_open=False,
            detail=type(exc).__name__,
        )

    if not isinstance(payload, dict):
        return ServiceHealth(
            listening=listening,
            api_ok=False,
            healthy=False,
            service_status=None,
            source_alive=False,
            recording=False,
            login_circuit_open=False,
            detail="InvalidStatePayload",
        )

    raw_status = payload.get("service_status")
    service_status = (
        str(raw_status).strip().lower()
        if isinstance(raw_status, str)
        else None
    )
    source_alive = payload.get("source_alive") is True
    recording = payload.get("recording") is True
    login_circuit_open = payload.get("login_circuit_open") is True
    auto_record_enabled = payload.get("auto_record_enabled") is not False
    status_healthy = (
        service_status in HEALTHY_DRIVER_STATUSES
        or (service_status == "live" and recording)
    )
    healthy = bool(
        source_alive
        and auto_record_enabled
        and status_healthy
        and not login_circuit_open
    )
    return ServiceHealth(
        listening=listening,
        api_ok=True,
        healthy=healthy,
        service_status=service_status,
        source_alive=source_alive,
        recording=recording,
        login_circuit_open=login_circuit_open,
    )


def acquire_instance_lock() -> Any:
    """取得 Windows 檔案鎖；handle 必須在看門狗常駐期間保持開啟。"""
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
            "已有另一個 scheduler.py 看門狗常駐中，請勿重複啟動"
        ) from exc
    return handle


def release_instance_lock(handle: Any) -> None:
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


def start_service() -> subprocess.Popen[bytes]:
    if not SERVICE_PATH.is_file():
        raise FileNotFoundError(f"找不到 {SERVICE_PATH.name}")

    command = [sys.executable, str(SERVICE_PATH)]
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    LOGGER.info(
        "未偵測到健康 live 錄製服務，啟動 %s（PORT %s）",
        SERVICE_PATH.name,
        SERVICE_PORT,
    )
    return subprocess.Popen(
        command,
        cwd=BASE_DIR,
        env=child_environment(),
        creationflags=creationflags,
    )


def wait_until_healthy(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if get_service_health().healthy:
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.2)
    return get_service_health().healthy


def ensure_service() -> int:
    try:
        auto_record_enabled = control_state.get_auto_record_enabled()
    except control_state.ControlStateError:
        LOGGER.exception(
            "錄製控制狀態無法讀取；基於安全考量不啟動 service.py"
        )
        return 1
    if not auto_record_enabled:
        LOGGER.info("明日自動錄製已關閉；今日不啟動 service.py")
        return 0

    health = get_service_health()
    if health.healthy:
        LOGGER.info(
            "live 錄製服務健康；PORT=%s status=%s "
            "source_alive=%s recording=%s；跳過",
            SERVICE_PORT,
            health.service_status,
            health.source_alive,
            health.recording,
        )
        return 0
    if health.listening:
        LOGGER.error(
            "PORT %s 雖在監聽但 live 錄製不健康；"
            "status=%s source_alive=%s recording=%s "
            "login_circuit_open=%s；拒絕誤判健康或重複綁埠",
            SERVICE_PORT,
            health.service_status,
            health.source_alive,
            health.recording,
            health.login_circuit_open,
        )
        return 1

    try:
        process = start_service()
    except Exception:
        LOGGER.exception("無法啟動 service.py")
        return 1

    if wait_until_healthy(process):
        started_health = get_service_health()
        LOGGER.info(
            "服務已健康啟動 http://%s:%s/；status=%s "
            "source_alive=%s recording=%s",
            SERVICE_HOST,
            SERVICE_PORT,
            started_health.service_status,
            started_health.source_alive,
            started_health.recording,
        )
        return 0

    returncode = process.poll()
    if returncode is None:
        LOGGER.error(
            "啟動後 %s 秒內仍未通過 live 驅動／錄製健康檢查（PORT %s）",
            int(STARTUP_WAIT_SECONDS),
            SERVICE_PORT,
        )
    else:
        LOGGER.error(
            "service.py 在開始監聽前結束（exit=%s）",
            returncode,
        )
    return 1


def run_forever(check_at: clock_time) -> int:
    try:
        lock_handle = acquire_instance_lock()
    except (OSError, RuntimeError) as exc:
        LOGGER.error("%s", exc)
        return 1

    LOGGER.info(
        "服務看門狗已啟動：平日 %s（Asia/Taipei）檢查 PORT %s",
        check_at.strftime("%H:%M"),
        SERVICE_PORT,
    )
    checked_date: date | None = None
    announced_check: datetime | None = None

    try:
        while True:
            try:
                now = taipei_now()
                is_weekday = now.weekday() < 5
                scheduled = scheduled_datetime(now.date(), check_at)

                if (
                    is_weekday
                    and now >= scheduled
                    and checked_date != now.date()
                ):
                    if ensure_service() == 0:
                        checked_date = now.date()
                        announced_check = None
                    else:
                        LOGGER.info(
                            "%s 秒後重試",
                            int(ERROR_RETRY_SECONDS),
                        )
                        time.sleep(ERROR_RETRY_SECONDS)
                    continue

                upcoming = next_check(now, check_at)
                if upcoming != announced_check:
                    LOGGER.info(
                        "下一次檢查／必要時啟動服務的時點：%s"
                        "（Asia/Taipei）",
                        upcoming.isoformat(sep=" ", timespec="seconds"),
                    )
                    announced_check = upcoming
                remaining = (upcoming - now).total_seconds()
                time.sleep(max(1.0, min(POLL_SECONDS, remaining)))
            except KeyboardInterrupt:
                LOGGER.info("收到中止指令，服務看門狗已停止")
                return 130
            except Exception:
                LOGGER.exception(
                    "看門狗迴圈發生例外；%s 秒後繼續",
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
        upcoming = next_check(taipei_now(), args.check_at)
        print(
            "下一次檢查／必要時啟動服務的時點："
            f"{upcoming.isoformat(sep=' ', timespec='seconds')}"
            f"（Asia/Taipei，PORT {SERVICE_PORT}）"
        )
        return 0

    if args.once:
        LOGGER.info("--once：立即確認即時偵測器服務")
        return ensure_service()

    return run_forever(args.check_at)


if __name__ == "__main__":
    raise SystemExit(main())
