"""Offline acceptance tests for the final correctness/observability batch."""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from datetime import date, datetime, time as datetime_time, timedelta
from types import SimpleNamespace
from unittest import mock


sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

import control_state
import recorder
import scanner
import service
import telegram_notify
import webserver


def bidask_event(code: str, event_at: datetime) -> dict[str, object]:
    return {
        "kind": "bidask",
        "code": code,
        "ts": service.iso_taipei(event_at),
        "simtrade": True,
        "bid_price": [100.0, None, None, None, None],
        "bid_volume": [1000, None, None, None, None],
        "ask_price": [None, None, None, None, None],
        "ask_volume": [None, None, None, None, None],
    }


def write_meta(path: pathlib.Path, codes: list[str]) -> None:
    payload = {
        "date": "20260730",
        "session": "preopen",
        "window": {
            "start": "2026-07-30T08:30:00+08:00",
            "end": "2026-07-30T09:00:00+08:00",
        },
        "subscribed": len(codes),
        "subscribed_codes": codes,
        "stocks": [
            {
                "code": code,
                "name": code,
                "reference": 90.9,
                "limit_up": 100.0,
                "limit_down": 81.8,
            }
            for code in codes
        ],
    }
    path.with_suffix(".meta.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


class JsonlAndScannerAcceptanceTests(unittest.TestCase):
    def test_single_bad_line_is_skipped_by_scanner_and_replay(self) -> None:
        event_at = datetime(2026, 7, 30, 8, 45, tzinfo=service.TAIPEI)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "auction_20260730.jsonl"
            path.write_text(
                json.dumps(bidask_event("9001", event_at), ensure_ascii=False)
                + "\n{broken-tail\n",
                encoding="utf-8",
            )
            write_meta(path, ["9001"])
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                events = scanner.read_jsonl(path)
                _metadata, rows = service.load_replay(path)
            self.assertEqual(len(events), 1)
            self.assertEqual(len(rows), 1)
            self.assertIn(f"{path.name}:2", stderr.getvalue())

            output = path.with_name("result.json")
            with contextlib.redirect_stderr(io.StringIO()):
                exit_code = scanner.main(
                    ["--in", str(path), "--out", str(output)]
                )
            self.assertEqual(exit_code, 0)
            self.assertTrue(output.is_file())

    def test_material_bad_line_ratio_fails(self) -> None:
        event_at = datetime(2026, 7, 30, 8, 45, tzinfo=service.TAIPEI)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "auction_20260730.jsonl"
            path.write_text(
                json.dumps(bidask_event("9001", event_at))
                + "\n{bad-one\n{bad-two\n",
                encoding="utf-8",
            )
            write_meta(path, ["9001"])
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(ValueError, "超過容錯門檻"):
                    scanner.read_jsonl(path)
                with self.assertRaisesRegex(ValueError, "超過容錯門檻"):
                    service.load_replay(path)

    def test_missing_meta_and_unavailable_limits_is_explicit_failure(self) -> None:
        event_at = datetime(2026, 7, 30, 8, 45, tzinfo=service.TAIPEI)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = pathlib.Path(temp_dir) / "auction_20260730.jsonl"
            path.write_text(
                json.dumps(bidask_event("9001", event_at)),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                exit_code = scanner.main(["--in", str(path)])
            self.assertNotEqual(exit_code, 0)
            self.assertIn(
                "limit_up 不可得、判定不可信",
                stderr.getvalue(),
            )


class StateAcceptanceTests(unittest.TestCase):
    def test_holiday_is_skipped_by_scheduler_and_control(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            holiday_path = pathlib.Path(temp_dir) / "holidays.txt"
            holiday_path.write_text("2026-01-01\n", encoding="utf-8")
            with mock.patch.object(service, "HOLIDAYS_PATH", holiday_path):
                start_at, _end_at = service.upcoming_window(
                    datetime(
                        2026,
                        1,
                        1,
                        8,
                        0,
                        tzinfo=service.TAIPEI,
                    ),
                    datetime_time(8, 30),
                    datetime_time(9, 0),
                )
                self.assertEqual(start_at.date(), date(2026, 1, 2))
                runtime = service.ServiceRuntime(
                    session="preopen",
                    window_start="08:30",
                    window_end="09:00",
                )
                control = service.RecordingControl(
                    runtime,
                    mode="live",
                    auto_record_enabled=True,
                    session="preopen",
                    start_clock=datetime_time(8, 30),
                    end_clock=datetime_time(9, 0),
                )
                self.assertFalse(control.should_run_live(date(2026, 1, 1)))

    def test_no_data_is_distinct_and_visible_in_api_state(self) -> None:
        runtime = service.ServiceRuntime(
            session="preopen",
            window_start="08:30",
            window_end="09:00",
        )
        metadata = {
            "date": "20260730",
            "subscribed": 2,
            "subscribed_codes": ["9001", "9002"],
            "stocks": [
                {"code": "9001", "limit_up": 100.0},
                {"code": "9002", "limit_up": 100.0},
            ],
        }
        runtime.reset_detector(
            metadata,
            session="preopen",
            window_start="08:30",
            window_end="09:00",
            universe=2,
            subscribed=2,
        )
        runtime.process_event(
            bidask_event(
                "9001",
                datetime(2026, 7, 30, 8, 45, tzinfo=service.TAIPEI),
            )
        )
        self.assertEqual(runtime.finalize_no_data(), ["9002"])
        state = runtime.publish()
        statuses = {
            stock["code"]: stock["status"] for stock in state["stocks"]
        }
        self.assertEqual(statuses["9002"], "no_data")
        self.assertEqual(state["counts"]["no_data"], 1)

    def test_postopen_record_count_stays_cumulative(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            main_path = root / "auction.jsonl"
            aux_path = root / "auction_postopen.jsonl"
            runtime = service.ServiceRuntime(
                session="preopen",
                window_start="08:30",
                window_end="09:00",
            )
            runtime.start_recording(main_path)
            runtime.record_event({"kind": "bidask", "code": "9001"})
            runtime.finish_recording()
            aux_writer = service.ServiceJsonlWriter(aux_path)
            aux_writer.start()
            runtime.set_aux_recording(aux_writer, aux_path)
            aux_writer.put({"kind": "bidask", "code": "9001"})
            deadline = datetime.now().timestamp() + 1
            while aux_writer.count < 1 and datetime.now().timestamp() < deadline:
                threading.Event().wait(0.01)
            state = runtime.publish()
            self.assertEqual(state["phase"], "postopen")
            self.assertEqual(state["record_count"], 2)
            aux_writer.close()
            runtime.finish_aux_recording(aux_writer)
            self.assertEqual(runtime.publish()["record_count"], 2)

    def test_corrupt_control_file_is_fail_closed_and_visible_via_http(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bad_path = pathlib.Path(temp_dir) / "control.json"
            bad_path.write_text("{broken", encoding="utf-8")
            with mock.patch.object(control_state, "CONTROL_PATH", bad_path):
                with self.assertRaises(control_state.ControlStateError) as caught:
                    control_state.get_auto_record_enabled()
            runtime = service.ServiceRuntime(
                session="preopen",
                window_start="08:30",
                window_end="09:00",
                auto_record_enabled=False,
                control_error=True,
                control_error_reason=str(caught.exception),
            )
            control = service.RecordingControl(
                runtime,
                mode="live",
                auto_record_enabled=False,
                control_error=True,
                session="preopen",
                start_clock=datetime_time(8, 30),
                end_clock=datetime_time(9, 0),
            )
            with self.assertRaises(service.ControlActionError) as denied:
                control.handle_request("start", None)
            self.assertEqual(denied.exception.error_code, "control_file_error")

            runtime.publish()
            server = webserver.start_server(
                runtime.shared,
                host="127.0.0.1",
                port=0,
            )
            self.addCleanup(webserver.stop_server, server)
            port = server.server_address[1]
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state",
                timeout=3,
            ) as response:
                state = json.load(response)
            self.assertTrue(state["control_error"])
            self.assertFalse(state["auto_record_enabled"])
            self.assertIn("有效 JSON", state["control_error_reason"])

    def test_control_error_notification_is_triggered_only_once(self) -> None:
        started: list[str] = []

        class ImmediateThread:
            def __init__(
                self,
                *,
                target: object,
                name: str,
                daemon: bool,
            ) -> None:
                self.target = target
                self.name = name
                self.daemon = daemon

            def start(self) -> None:
                started.append(self.name)
                self.target()

        self.addCleanup(
            setattr,
            service,
            "_CONTROL_ERROR_NOTIFICATION_STARTED",
            False,
        )
        with (
            mock.patch.object(
                service,
                "_CONTROL_ERROR_NOTIFICATION_STARTED",
                False,
            ),
            mock.patch.object(
                service.threading,
                "Thread",
                ImmediateThread,
            ),
            mock.patch.object(
                telegram_notify,
                "send_telegram_message",
                return_value=1,
            ) as send_message,
        ):
            self.assertTrue(
                service.start_control_error_notification("測試設定錯誤")
            )
            self.assertFalse(
                service.start_control_error_notification("測試設定錯誤")
            )
        self.assertEqual(started, ["telegram-control-error-notify"])
        send_message.assert_called_once()


class LiveAcceptanceTests(unittest.TestCase):
    def _modules(self, api: object) -> tuple[types.ModuleType, types.ModuleType]:
        fake_shioaji = types.ModuleType("shioaji")
        fake_shioaji.Shioaji = lambda: api
        fake_constant = types.ModuleType("shioaji.constant")
        fake_constant.QuoteType = SimpleNamespace(BidAsk="bidask")
        fake_constant.QuoteVersion = SimpleNamespace(v1="v1")
        return fake_shioaji, fake_constant

    def _live_patches(
        self,
        api: object,
        universe: list[tuple[str, object]],
    ) -> tuple[object, ...]:
        fake_shioaji, fake_constant = self._modules(api)
        return (
            mock.patch.dict(
                sys.modules,
                {
                    "shioaji": fake_shioaji,
                    "shioaji.constant": fake_constant,
                },
            ),
            mock.patch.object(
                service,
                "_configure_quiet_solace",
                return_value=None,
            ),
            mock.patch.object(
                service,
                "load_live_credentials",
                return_value=("placeholder", "placeholder"),
            ),
            mock.patch.object(
                recorder,
                "wait_for_contracts",
                return_value=None,
            ),
            mock.patch.object(
                recorder,
                "requested_universe",
                return_value=(universe, [], len(universe)),
            ),
            mock.patch.object(
                service,
                "CAPACITY_EVENT_SETTLE_SECONDS",
                0,
            ),
            mock.patch.object(
                service,
                "SNAPSHOT_AFTER_END_SECONDS",
                0,
            ),
        )

    def test_session_down_callback_resets_login_and_subscription(self) -> None:
        class Quote:
            def __init__(self) -> None:
                self.session_down_callback = None

            def set_event_callback(self, _callback: object) -> None:
                return None

            def set_session_down_callback(self, callback: object) -> None:
                self.session_down_callback = callback

            def set_on_bidask_stk_v1_callback(
                self, _callback: object
            ) -> None:
                return None

            def subscribe(self, *_args: object, **_kwargs: object) -> None:
                assert self.session_down_callback is not None
                self.session_down_callback()

            def unsubscribe(self, *_args: object, **_kwargs: object) -> None:
                return None

        class Api:
            def __init__(self) -> None:
                self.quote = Quote()

            def login(self, **_kwargs: object) -> list[object]:
                return []

            def logout(self) -> None:
                return None

        contract = SimpleNamespace(
            code="9001",
            name="測試股",
            reference=90.9,
            limit_up=100.0,
            limit_down=81.8,
        )
        api = Api()
        runtime = service.ServiceRuntime(
            session="preclose",
            window_start="13:25",
            window_end="13:30",
        )
        now = service.taipei_now()
        patches = self._live_patches(api, [("9001", contract)])
        with contextlib.ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertRaisesRegex(service.LiveSourceError, "session 中斷"):
                service.run_one_live_window(
                    runtime,
                    session="preclose",
                    start_at=now - timedelta(milliseconds=10),
                    end_at=now + timedelta(milliseconds=20),
                    universe_spec=None,
                    stop_event=threading.Event(),
                    record_enabled=False,
                    record_out=None,
                )
        state = runtime.publish()
        self.assertFalse(state["login_ok"])
        self.assertFalse(state["subscribe_ok"])

    def test_zero_session_writes_early_meta_reconciles_capacity_and_skips_notify(
        self,
    ) -> None:
        class Quote:
            def __init__(self, meta_path: pathlib.Path) -> None:
                self.meta_path = meta_path
                self.event_callback = None
                self.early_meta = None
                self.calls = 0

            def set_event_callback(self, callback: object) -> None:
                self.event_callback = callback

            def set_session_down_callback(self, _callback: object) -> None:
                return None

            def set_on_bidask_stk_v1_callback(
                self, _callback: object
            ) -> None:
                return None

            def subscribe(self, *_args: object, **_kwargs: object) -> None:
                self.calls += 1
                if self.calls == 1:
                    self.early_meta = json.loads(
                        self.meta_path.read_text(encoding="utf-8")
                    )
                if self.calls == 2:
                    assert self.event_callback is not None
                    self.event_callback(
                        1,
                        1,
                        "subscription limit",
                        "quote subscription quota",
                    )

            def unsubscribe(self, *_args: object, **_kwargs: object) -> None:
                return None

        class Api:
            def __init__(self, quote: Quote) -> None:
                self.quote = quote

            def login(self, **_kwargs: object) -> list[object]:
                return []

            def snapshots(
                self, _contracts: object, **_kwargs: object
            ) -> list[object]:
                return []

            def logout(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            root = pathlib.Path(temp_dir)
            record_path = root / "auction_20260730.jsonl"
            result_path = root / "result_20260730.json"
            quote = Quote(record_path.with_suffix(".meta.json"))
            api = Api(quote)
            contracts = [
                (
                    f"900{index}",
                    SimpleNamespace(
                        code=f"900{index}",
                        name=f"測試股{index}",
                        reference=90.9,
                        limit_up=100.0,
                        limit_down=81.8,
                    ),
                )
                for index in range(1, 4)
            ]
            runtime = service.ServiceRuntime(
                session="preopen",
                window_start="08:30",
                window_end="09:00",
            )
            now = service.taipei_now()
            patches = self._live_patches(api, contracts)
            with contextlib.ExitStack() as stack:
                for patcher in patches:
                    stack.enter_context(patcher)
                stack.enter_context(
                    mock.patch.object(service, "POSTOPEN_MINUTES", 0)
                )
                notify = stack.enter_context(
                    mock.patch.object(
                        service,
                        "start_telegram_result_notification",
                    )
                )
                completed = service.run_one_live_window(
                    runtime,
                    session="preopen",
                    start_at=now - timedelta(milliseconds=20),
                    end_at=now + timedelta(milliseconds=10),
                    universe_spec=None,
                    stop_event=threading.Event(),
                    record_enabled=True,
                    record_out=record_path,
                    result_out=result_path,
                    notification_owner=True,
                )

            self.assertTrue(completed)
            notify.assert_not_called()
            self.assertEqual(
                quote.early_meta["metadata_phase"],
                "window_start",
            )
            self.assertIn("limit_down", quote.early_meta["stocks"][0])
            final_meta = json.loads(
                record_path.with_suffix(".meta.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(final_meta["subscribed_codes"], ["9001"])
            self.assertEqual(final_meta["dropped"], ["9002", "9003"])
            self.assertTrue(final_meta["sub_limit_exact"])
            self.assertEqual(final_meta["session_state"], "no_session")
            state = runtime.publish()
            self.assertEqual(state["today_recording"]["state"], "no_session")
            self.assertEqual(state["counts"]["no_data"], 1)
            self.assertEqual(state["dropped"], ["9002", "9003"])


if __name__ == "__main__":
    unittest.main()
