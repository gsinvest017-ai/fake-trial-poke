"""scheduler.py 複合健康檢查的離線測試。"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

import scheduler


class FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.status = 200
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class SchedulerHealthTests(unittest.TestCase):
    def health_for(self, payload: dict[str, object]) -> scheduler.ServiceHealth:
        with (
            mock.patch.object(
                scheduler,
                "service_is_listening",
                return_value=True,
            ),
            mock.patch.object(
                scheduler.urllib.request,
                "urlopen",
                return_value=FakeResponse(payload),
            ),
        ):
            return scheduler.get_service_health()

    def test_idle_live_driver_is_healthy_before_recording_window(self) -> None:
        health = self.health_for(
            {
                "service_status": "idle",
                "source_alive": True,
                "recording": False,
                "auto_record_enabled": True,
                "login_circuit_open": False,
            }
        )

        self.assertTrue(health.healthy)
        self.assertTrue(health.source_alive)
        self.assertFalse(health.recording)

    def test_tcp_or_webserver_alone_is_not_healthy(self) -> None:
        health = self.health_for(
            {
                "service_status": "idle",
                "recording": False,
                "auto_record_enabled": True,
            }
        )

        self.assertTrue(health.listening)
        self.assertTrue(health.api_ok)
        self.assertFalse(health.healthy)
        self.assertFalse(health.source_alive)

    def test_live_requires_active_recording(self) -> None:
        unhealthy = self.health_for(
            {
                "service_status": "live",
                "source_alive": True,
                "recording": False,
                "auto_record_enabled": True,
            }
        )
        healthy = self.health_for(
            {
                "service_status": "live",
                "source_alive": True,
                "recording": True,
                "auto_record_enabled": True,
            }
        )

        self.assertFalse(unhealthy.healthy)
        self.assertTrue(healthy.healthy)

    def test_error_or_login_circuit_is_not_misreported_healthy(self) -> None:
        error_health = self.health_for(
            {
                "service_status": "error",
                "source_alive": False,
                "recording": True,
                "auto_record_enabled": True,
            }
        )
        circuit_health = self.health_for(
            {
                "service_status": "error",
                "source_alive": True,
                "recording": False,
                "auto_record_enabled": True,
                "login_circuit_open": True,
            }
        )

        self.assertFalse(error_health.healthy)
        self.assertFalse(circuit_health.healthy)
        self.assertTrue(circuit_health.login_circuit_open)

    def test_ensure_service_does_not_spawn_on_unhealthy_occupied_port(
        self,
    ) -> None:
        health = scheduler.ServiceHealth(
            listening=True,
            api_ok=True,
            healthy=False,
            service_status="error",
            source_alive=False,
            recording=False,
            login_circuit_open=False,
        )
        with (
            mock.patch.object(
                scheduler.control_state,
                "get_auto_record_enabled",
                return_value=True,
            ),
            mock.patch.object(
                scheduler,
                "get_service_health",
                return_value=health,
            ),
            mock.patch.object(scheduler, "start_service") as start_service,
        ):
            exit_code = scheduler.ensure_service()

        self.assertEqual(exit_code, 1)
        start_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
