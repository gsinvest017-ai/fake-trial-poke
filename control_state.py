"""Persistent recording-control settings shared by the service and scheduler."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
CONTROL_PATH = BASE_DIR / "data" / "control.json"
TAIPEI = ZoneInfo("Asia/Taipei")

_LOCK = threading.RLock()


class ControlStateError(RuntimeError):
    """The persistent control file could not be read or written safely."""


def _default_state() -> dict[str, Any]:
    return {
        "auto_record_enabled": True,
        "updated_at": None,
    }


def read_control_state() -> dict[str, Any]:
    """Return a validated snapshot; a missing file means auto-record is on."""
    with _LOCK:
        try:
            raw = CONTROL_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _default_state()
        except OSError as exc:
            raise ControlStateError("無法讀取錄製控制狀態") from exc

        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ControlStateError("錄製控制狀態不是有效 JSON") from exc
        if not isinstance(payload, dict):
            raise ControlStateError("錄製控制狀態必須是 JSON object")
        enabled = payload.get("auto_record_enabled")
        if type(enabled) is not bool:
            raise ControlStateError(
                "auto_record_enabled 必須是 boolean"
            )
        updated_at = payload.get("updated_at")
        if updated_at is not None and not isinstance(updated_at, str):
            raise ControlStateError("updated_at 必須是字串或 null")
        return {
            "auto_record_enabled": enabled,
            "updated_at": updated_at,
        }


def get_auto_record_enabled() -> bool:
    return bool(read_control_state()["auto_record_enabled"])


def set_auto_record_enabled(enabled: bool) -> dict[str, Any]:
    """Atomically persist the setting and return the written snapshot."""
    if type(enabled) is not bool:
        raise TypeError("enabled 必須是 boolean")
    payload = {
        "auto_record_enabled": enabled,
        "updated_at": datetime.now(TAIPEI).isoformat(timespec="seconds"),
    }

    with _LOCK:
        CONTROL_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=".control-",
                suffix=".tmp",
                dir=CONTROL_PATH.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, CONTROL_PATH)
        except OSError as exc:
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass
            raise ControlStateError("無法寫入錄製控制狀態") from exc
    return dict(payload)
