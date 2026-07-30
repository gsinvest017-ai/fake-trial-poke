"""service.py live 錄製與誠實狀態契約的離線測試。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import types
import unittest
import urllib.request
from datetime import datetime, time as datetime_time, timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

import scanner
import recorder
import service
import telegram_notify
import webserver


STATE_CONTRACT_FIELDS = {
    "recording",
    "record_path",
    "record_count",
    "last_event_age_sec",
    "login_ok",
    "subscribe_ok",
    "source_alive",
    "source_thread_alive",
    "source_heartbeat_at",
    "source_heartbeat_age_sec",
    "source_restart_count",
    "source_error",
    "today_recording",
}
TODAY_RECORDING_FIELDS = {
    "date",
    "window",
    "record_count",
    "has_data",
    "state",
}


def metadata_for(
    now: object,
    *,
    start: str,
    end: str,
) -> dict[str, object]:
    return {
        "date": now.strftime("%Y%m%d"),
        "session": "preopen",
        "window": {"start": start, "end": end},
        "universe": ["9001"],
        "universe_size": 1,
        "subscribed": 1,
        "subscribed_codes": ["9001"],
        "generated_at": service.iso_taipei(now),
        "stocks": [
            {
                "code": "9001",
                "name": "測試股",
                "reference": 90.9,
                "limit_up": 100.0,
            }
        ],
    }


def synthetic_events(now: object) -> list[dict[str, object]]:
    timestamps = [
        service.iso_taipei(now - timedelta(seconds=2)),
        service.iso_taipei(now - timedelta(seconds=1)),
        service.iso_taipei(now),
    ]
    empty_levels: list[None] = [None] * 5
    return [
        {
            "code": "9001",
            "name": "測試股",
            "ts": timestamps[0],
            "kind": "bidask",
            "simtrade": True,
            "price": None,
            "chg_type": None,
            "bid_price": [100.0, None, None, None, None],
            "bid_volume": [2500, None, None, None, None],
            "ask_price": empty_levels,
            "ask_volume": empty_levels,
            "volume": None,
        },
        {
            "code": "9001",
            "name": "測試股",
            "ts": timestamps[1],
            "kind": "tick",
            "simtrade": True,
            "price": 100.0,
            "chg_type": 1,
            "bid_price": empty_levels,
            "bid_volume": empty_levels,
            "ask_price": empty_levels,
            "ask_volume": empty_levels,
            "volume": 12,
        },
        {
            "code": "9001",
            "name": "測試股",
            "ts": timestamps[2],
            "source_ts": timestamps[2],
            "kind": "snapshot",
            "open_price": 100.0,
            "bid_price": [100.0, None, None, None, None],
            "bid_volume": [1800, None, None, None, None],
            "ask_price": empty_levels,
            "ask_volume": empty_levels,
        },
    ]


class ServiceRecordTests(unittest.TestCase):
    def test_today_recording_state_classification(self) -> None:
        start_clock = datetime_time(8, 30)
        end_clock = datetime_time(9, 0)
        cases = [
            (
                "weekday_before_window",
                datetime(2026, 7, 28, 8, 0, tzinfo=service.TAIPEI),
                False,
                0,
                "idle",
                False,
                "waiting",
            ),
            (
                "live_recording",
                datetime(2026, 7, 28, 8, 45, tzinfo=service.TAIPEI),
                False,
                42,
                "live",
                True,
                "recording",
            ),
            (
                "window_open_not_recording",
                datetime(2026, 7, 28, 8, 45, tzinfo=service.TAIPEI),
                False,
                0,
                "idle",
                False,
                "waiting",
            ),
            (
                "completed_with_data",
                datetime(2026, 7, 28, 9, 1, tzinfo=service.TAIPEI),
                True,
                101,
                "closed",
                False,
                "success",
            ),
            (
                "completed_without_data",
                datetime(2026, 7, 28, 9, 1, tzinfo=service.TAIPEI),
                False,
                0,
                "closed",
                False,
                "missed",
            ),
            (
                "completed_below_threshold",
                datetime(2026, 7, 28, 9, 1, tzinfo=service.TAIPEI),
                True,
                100,
                "closed",
                False,
                "missed",
            ),
            (
                "weekend",
                datetime(2026, 8, 1, 10, 0, tzinfo=service.TAIPEI),
                False,
                0,
                "closed",
                False,
                "waiting",
            ),
        ]

        for (
            label,
            now,
            has_data,
            record_count,
            service_status,
            recording,
            expected,
        ) in cases:
            with self.subTest(label=label):
                actual = service.classify_today_recording_state(
                    now=now,
                    start_clock=start_clock,
                    end_clock=end_clock,
                    has_data=has_data,
                    record_count=record_count,
                    service_status=service_status,
                    recording=recording,
                )
                self.assertEqual(actual, expected)

    def test_today_recording_rebuilds_from_landed_main_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            history_dir = pathlib.Path(temp_dir) / "history"
            record_dir = history_dir / "20260728"
            record_dir.mkdir(parents=True)
            record_path = record_dir / "auction_20260728.jsonl"
            record_path.write_text(
                "\n".join("{}" for _ in range(101)) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(service, "HISTORY_DIR", history_dir):
                result = service.build_today_recording(
                    now=datetime(
                        2026,
                        7,
                        28,
                        9,
                        1,
                        tzinfo=service.TAIPEI,
                    ),
                    window_start="08:30",
                    window_end="09:00",
                    service_status="replay",
                    recording=False,
                )
                missing_result = service.build_today_recording(
                    now=datetime(
                        2026,
                        7,
                        30,
                        9,
                        1,
                        tzinfo=service.TAIPEI,
                    ),
                    window_start="08:30",
                    window_end="09:00",
                    service_status="closed",
                    recording=False,
                )

            self.assertTrue(
                TODAY_RECORDING_FIELDS.issubset(result),
                sorted(TODAY_RECORDING_FIELDS - set(result)),
            )
            self.assertEqual(result["date"], "20260728")
            self.assertEqual(result["window"], "08:30–09:00")
            self.assertEqual(result["record_count"], 101)
            self.assertTrue(result["has_data"])
            self.assertEqual(result["state"], "success")
            self.assertEqual(missing_result["date"], "20260730")
            self.assertEqual(missing_result["record_count"], 0)
            self.assertFalse(missing_result["has_data"])
            self.assertEqual(missing_result["state"], "missed")

    def test_default_storage_paths_are_partitioned_by_date(self) -> None:
        recording_at = service.taipei_now().replace(
            year=2026,
            month=7,
            day=24,
            hour=8,
            minute=30,
            second=0,
            microsecond=0,
        )
        expected_dir = (
            pathlib.Path(__file__).resolve().parent.parent
            / "data"
            / "history"
            / "20260724"
        )
        expected_auction = expected_dir / "auction_20260724.jsonl"
        expected_postopen = (
            expected_dir / "auction_20260724_postopen.jsonl"
        )
        expected_result = expected_dir / "result_20260724.json"

        self.assertEqual(recorder.default_output_path(recording_at), expected_auction)
        self.assertEqual(
            service.default_live_record_path(recording_at),
            expected_auction,
        )
        self.assertEqual(
            service.default_live_postopen_path(recording_at),
            expected_postopen,
        )
        self.assertEqual(
            service.default_live_result_path(recording_at),
            expected_result,
        )
        self.assertEqual(
            service.paired_postopen_path(
                expected_dir / "validation.jsonl"
            ),
            expected_dir / "validation_postopen.jsonl",
        )
        self.assertEqual(
            scanner.default_input_path("2026-07-24"),
            expected_auction,
        )
        self.assertEqual(
            scanner.default_output_path("2026-07-24"),
            expected_dir / "result_20260724.json",
        )

        smoke_args = recorder.parse_args(["--smoke", "15"])
        self.assertIsNone(
            recorder.resolve_output_path(smoke_args, recording_at)
        )

    def test_smoke_callback_requirement_tracks_market_hours(self) -> None:
        market_open = service.taipei_now().replace(
            year=2026,
            month=7,
            day=24,
            hour=9,
            minute=0,
            second=0,
            microsecond=0,
        )
        after_hours = market_open.replace(hour=17)
        weekend = market_open.replace(day=25)

        self.assertTrue(recorder.smoke_callbacks_expected(market_open))
        self.assertFalse(recorder.smoke_callbacks_expected(after_hours))
        self.assertFalse(recorder.smoke_callbacks_expected(weekend))

    def make_runtime(
        self,
        *,
        record_path: pathlib.Path | None = None,
        stale_after_seconds: float = 10.0,
    ) -> tuple[service.ServiceRuntime, dict[str, object]]:
        now = service.taipei_now()
        start = service.iso_taipei(now - timedelta(minutes=1))
        end = service.iso_taipei(now + timedelta(minutes=1))
        metadata = metadata_for(now, start=start, end=end)
        runtime = service.ServiceRuntime(
            session="preopen",
            window_start=start,
            window_end=end,
            service_status="live",
            stale_after_seconds=stale_after_seconds,
            record_path=record_path,
        )
        runtime.reset_detector(
            metadata,
            session="preopen",
            window_start=start,
            window_end=end,
            universe=1,
            subscribed=1,
        )
        return runtime, metadata

    def test_live_events_are_recorded_and_scanner_replay_compatible(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            output_path = temp_path / "live_rec.jsonl"
            result_path = temp_path / "result.json"
            runtime, metadata = self.make_runtime(record_path=output_path)
            runtime.set_context(login_ok=True, subscribe_ok=True)

            events = synthetic_events(service.taipei_now())
            for event in events:
                runtime.process_event(event)

            active_state = runtime.publish()
            self.assertTrue(active_state["recording"])
            self.assertTrue(
                STATE_CONTRACT_FIELDS.issubset(active_state),
                sorted(STATE_CONTRACT_FIELDS - set(active_state)),
            )

            runtime.finish_recording(metadata)
            final_state = runtime.publish()
            detector_result_path = temp_path / "detector_result.json"
            service.write_detector_result(
                detector_result_path,
                final_state,
                metadata,
            )

            self.assertFalse(final_state["recording"])
            self.assertEqual(final_state["record_count"], len(events))
            self.assertGreater(final_state["record_count"], 0)
            self.assertEqual(
                pathlib.Path(str(final_state["record_path"])).resolve(),
                output_path.resolve(),
            )

            rows = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), len(events))
            self.assertEqual(
                [(row["code"], row["kind"]) for row in rows],
                [(event["code"], event["kind"]) for event in events],
            )

            meta_path = output_path.with_suffix(".meta.json")
            recorded_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            stock_meta = {
                stock["code"]: stock for stock in recorded_meta["stocks"]
            }
            self.assertEqual(stock_meta["9001"]["limit_up"], 100.0)
            self.assertEqual(recorded_meta["universe"], ["9001"])
            detector_result = json.loads(
                detector_result_path.read_text(encoding="utf-8")
            )
            self.assertEqual(
                detector_result["detector"],
                "AuctionDetector",
            )
            self.assertEqual(
                detector_result["date"],
                service.taipei_now().strftime("%Y-%m-%d"),
            )
            self.assertEqual(
                detector_result["stocks"][0]["status"],
                final_state["stocks"][0]["status"],
            )

            self.assertEqual(
                scanner.main(
                    [
                        "--in",
                        str(output_path),
                        "--out",
                        str(result_path),
                    ]
                ),
                0,
            )
            self.assertTrue(result_path.is_file())

            replay_meta, replay_rows = service.load_replay(output_path)
            self.assertEqual(len(replay_rows), len(events))
            self.assertEqual(replay_meta["stocks"][0]["limit_up"], 100.0)

    def test_replay_is_read_only_without_explicit_record_out(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            source_path = temp_path / "auction_20260724.jsonl"
            now = service.taipei_now()
            start = service.iso_taipei(now - timedelta(minutes=1))
            end = service.iso_taipei(now + timedelta(minutes=1))
            metadata = metadata_for(now, start=start, end=end)
            source_path.write_text(
                "".join(
                    json.dumps(event, ensure_ascii=False) + "\n"
                    for event in synthetic_events(now)
                ),
                encoding="utf-8",
            )
            source_path.with_suffix(".meta.json").write_text(
                json.dumps(metadata, ensure_ascii=False),
                encoding="utf-8",
            )
            before = {
                path.name: path.read_bytes()
                for path in temp_path.iterdir()
                if path.is_file()
            }

            replay_meta, rows = service.load_replay(source_path)
            runtime, _unused = self.make_runtime()
            runtime.set_context(service_status="replay")
            with mock.patch.object(
                service,
                "start_telegram_result_notification",
            ) as start_notification:
                service.replay_worker(
                    runtime,
                    rows,
                    speed=1_000_000_000.0,
                    stop_event=threading.Event(),
                )
            start_notification.assert_not_called()
            state = runtime.publish()
            after = {
                path.name: path.read_bytes()
                for path in temp_path.iterdir()
                if path.is_file()
            }

            self.assertEqual(replay_meta["session"], "preopen")
            self.assertEqual(after, before)
            self.assertFalse(state["recording"])
            self.assertEqual(state["record_count"], 0)
            self.assertIsNone(state["record_path"])

            default_args = service.parse_args(
                ["--replay", str(source_path)]
            )
            opt_in_args = service.parse_args(
                [
                    "--replay",
                    str(source_path),
                    "--record-out",
                    str(temp_path / "explicit.jsonl"),
                ]
            )
            self.assertIsNone(default_args.record_out)
            self.assertIsNotNone(opt_in_args.record_out)

    def test_postopen_writer_remains_visible_as_active_recording(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = (
                pathlib.Path(temp_dir) / "auction_test_postopen.jsonl"
            )
            runtime, _metadata = self.make_runtime()
            writer = service.ServiceJsonlWriter(output_path)
            writer.start()
            runtime.set_aux_recording(writer, output_path)
            writer.put(synthetic_events(service.taipei_now())[0])

            active_state = runtime.publish()
            self.assertTrue(active_state["recording"])
            self.assertEqual(
                pathlib.Path(active_state["record_path"]),
                output_path.resolve(),
            )

            writer.close()
            runtime.finish_aux_recording(writer)
            final_state = runtime.publish()
            self.assertFalse(final_state["recording"])
            self.assertEqual(final_state["record_count"], 1)
            self.assertEqual(
                pathlib.Path(final_state["record_path"]),
                output_path.resolve(),
            )

    def test_short_live_window_writes_preopen_postopen_and_result(
        self,
    ) -> None:
        class FakeQuote:
            def __init__(self) -> None:
                self.bidask_callback = None
                self.preopen_event = None

            def set_event_callback(self, _callback: object) -> None:
                return None

            def set_on_bidask_stk_v1_callback(
                self,
                callback: object,
            ) -> None:
                self.bidask_callback = callback

            def subscribe(self, *_args: object, **_kwargs: object) -> None:
                if self.bidask_callback is not None:
                    self.bidask_callback(None, self.preopen_event)

            def unsubscribe(
                self,
                *_args: object,
                **_kwargs: object,
            ) -> None:
                return None

        class FakeApi:
            def __init__(self, quote: FakeQuote) -> None:
                self.quote = quote
                self.snapshot_calls = 0
                self.postopen_event = None
                self.snapshot = None

            def login(self, **_kwargs: object) -> list[object]:
                return []

            def snapshots(
                self,
                _contracts: object,
                **_kwargs: object,
            ) -> list[object]:
                self.snapshot_calls += 1
                if (
                    self.snapshot_calls == 1
                    and self.quote.bidask_callback is not None
                ):
                    self.quote.bidask_callback(
                        None,
                        self.postopen_event,
                    )
                return [self.snapshot]

            def logout(self) -> None:
                return None

        now = service.taipei_now()
        start_at = now - timedelta(milliseconds=50)
        end_at = now + timedelta(milliseconds=50)
        contract = SimpleNamespace(
            code="9001",
            name="測試股",
            reference=90.9,
            limit_up=100.0,
        )
        empty_levels = [None] * 5
        quote = FakeQuote()
        quote.preopen_event = SimpleNamespace(
            code="9001",
            ts=(start_at + timedelta(milliseconds=10)).replace(
                tzinfo=None
            ),
            simtrade=True,
            bid_price=[100.0, *empty_levels[:4]],
            bid_volume=[2500, *empty_levels[:4]],
            ask_price=empty_levels,
            ask_volume=empty_levels,
        )
        fake_api = FakeApi(quote)
        fake_api.postopen_event = SimpleNamespace(
            code="9001",
            ts=(end_at + timedelta(milliseconds=50)).replace(
                tzinfo=None
            ),
            simtrade=False,
            bid_price=[99.5, *empty_levels[:4]],
            bid_volume=[1000, *empty_levels[:4]],
            ask_price=[100.0, *empty_levels[:4]],
            ask_volume=[500, *empty_levels[:4]],
        )
        fake_api.snapshot = SimpleNamespace(
            code="9001",
            name="測試股",
            ts=end_at.replace(tzinfo=None),
            open_price=100.0,
            bid_price=[100.0, *empty_levels[:4]],
            bid_volume=[1800, *empty_levels[:4]],
            ask_price=empty_levels,
            ask_volume=empty_levels,
        )
        fake_shioaji = types.ModuleType("shioaji")
        fake_shioaji.Shioaji = lambda: fake_api
        fake_constant = types.ModuleType("shioaji.constant")
        fake_constant.QuoteType = SimpleNamespace(BidAsk="bidask")
        fake_constant.QuoteVersion = SimpleNamespace(v1="v1")
        notification_started = threading.Event()
        notification_release = threading.Event()
        notification_finished = threading.Event()
        notification_daemon: list[bool] = []
        self.addCleanup(notification_release.set)

        def slow_notification(_result_path: pathlib.Path) -> None:
            notification_daemon.append(threading.current_thread().daemon)
            notification_started.set()
            notification_release.wait(timeout=5)
            notification_finished.set()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = pathlib.Path(temp_dir)
            record_path = temp_path / "auction_20260727.jsonl"
            result_path = temp_path / "result_20260727.json"
            runtime = service.ServiceRuntime(
                session="preopen",
                window_start=service.iso_taipei(start_at),
                window_end=service.iso_taipei(end_at),
                service_status="armed",
            )

            with (
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
                    return_value=("test-key", "test-secret"),
                ),
                mock.patch.object(
                    recorder,
                    "wait_for_contracts",
                    return_value=None,
                ),
                mock.patch.object(
                    recorder,
                    "requested_universe",
                    return_value=([(contract.code, contract)], [], 1),
                ),
                mock.patch.object(service, "POSTOPEN_MINUTES", 0.003),
                mock.patch.object(
                    service,
                    "CAPACITY_EVENT_SETTLE_SECONDS",
                    0,
                ),
                mock.patch.object(
                    service,
                    "MIN_SIMTRADE_BIDASK_EVENTS_FOR_SESSION",
                    1,
                ),
                mock.patch.object(
                    service,
                    "SNAPSHOT_AFTER_END_SECONDS",
                    0.01,
                ),
                mock.patch.object(
                    service,
                    "notify_telegram_result",
                    side_effect=slow_notification,
                ) as notify_result,
            ):
                completed = service.run_one_live_window(
                    runtime,
                    session="preopen",
                    start_at=start_at,
                    end_at=end_at,
                    universe_spec=None,
                    stop_event=threading.Event(),
                    record_enabled=True,
                    record_out=record_path,
                    result_out=result_path,
                    notification_owner=True,
                )

            self.assertTrue(notification_started.wait(timeout=1))
            self.assertFalse(notification_finished.is_set())
            self.assertEqual(notification_daemon, [True])
            notify_result.assert_called_once_with(result_path.resolve())
            postopen_path = service.paired_postopen_path(record_path)
            self.assertTrue(completed)
            for artifact in (
                record_path,
                record_path.with_suffix(".meta.json"),
                postopen_path,
                postopen_path.with_suffix(".meta.json"),
                result_path,
            ):
                self.assertTrue(artifact.is_file(), artifact)
            main_rows = [
                json.loads(line)
                for line in record_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            postopen_rows = [
                json.loads(line)
                for line in postopen_path.read_text(
                    encoding="utf-8"
                ).splitlines()
            ]
            self.assertEqual(
                [row["kind"] for row in main_rows],
                ["bidask", "snapshot"],
            )
            self.assertEqual(
                [row["kind"] for row in postopen_rows],
                ["bidask", "snapshot"],
            )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["detector"], "AuctionDetector")
            self.assertEqual(result["subscribed"], 1)
            self.assertFalse(runtime.publish()["recording"])
            notification_release.set()
            self.assertTrue(notification_finished.wait(timeout=1))

    def test_preclose_and_no_record_do_not_start_telegram(self) -> None:
        class FakeQuote:
            def set_event_callback(self, _callback: object) -> None:
                return None

            def set_on_bidask_stk_v1_callback(
                self,
                _callback: object,
            ) -> None:
                return None

            def subscribe(self, *_args: object, **_kwargs: object) -> None:
                return None

            def unsubscribe(
                self,
                *_args: object,
                **_kwargs: object,
            ) -> None:
                return None

        class FakeApi:
            def __init__(self) -> None:
                self.quote = FakeQuote()

            def login(self, **_kwargs: object) -> list[object]:
                return []

            def snapshots(
                self,
                _contracts: object,
                **_kwargs: object,
            ) -> list[object]:
                return []

            def logout(self) -> None:
                return None

        contract = SimpleNamespace(
            code="9001",
            name="測試股",
            reference=90.9,
            limit_up=100.0,
        )
        fake_shioaji = types.ModuleType("shioaji")
        fake_shioaji.Shioaji = FakeApi
        fake_constant = types.ModuleType("shioaji.constant")
        fake_constant.QuoteType = SimpleNamespace(BidAsk="bidask")
        fake_constant.QuoteVersion = SimpleNamespace(v1="v1")

        with (
            tempfile.TemporaryDirectory() as temp_dir,
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
                return_value=([(contract.code, contract)], [], 1),
            ),
            mock.patch.object(
                service,
                "SNAPSHOT_AFTER_END_SECONDS",
                0,
            ),
            mock.patch.object(
                service,
                "CAPACITY_EVENT_SETTLE_SECONDS",
                0,
            ),
            mock.patch.object(
                service,
                "start_telegram_result_notification",
            ) as start_notification,
        ):
            temp_path = pathlib.Path(temp_dir)
            scenarios = (
                ("preclose", True, "preclose-record"),
                ("preopen", False, "preopen-no-record"),
            )
            for session, record_enabled, label in scenarios:
                with self.subTest(label=label):
                    now = service.taipei_now()
                    start_at = now - timedelta(milliseconds=20)
                    end_at = now + timedelta(milliseconds=10)
                    record_path = temp_path / f"{label}.jsonl"
                    result_path = temp_path / f"{label}.json"
                    runtime = service.ServiceRuntime(
                        session=session,
                        window_start=service.iso_taipei(start_at),
                        window_end=service.iso_taipei(end_at),
                        service_status="armed",
                    )
                    completed = service.run_one_live_window(
                        runtime,
                        session=session,
                        start_at=start_at,
                        end_at=end_at,
                        universe_spec=None,
                        stop_event=threading.Event(),
                        record_enabled=record_enabled,
                        record_out=(
                            record_path if record_enabled else None
                        ),
                        result_out=result_path,
                    )

                    self.assertTrue(completed)
                    start_notification.assert_not_called()

    def test_unconfigured_telegram_is_a_normal_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = pathlib.Path(temp_dir) / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "date": "2026-07-24",
                        "stocks": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    telegram_notify,
                    "send_telegram_message",
                    return_value=None,
                ) as send_message,
                mock.patch.object(
                    service.time,
                    "monotonic",
                    return_value=100.0,
                ),
                mock.patch("builtins.print") as print_output,
            ):
                service.notify_telegram_result(result_path)

        send_message.assert_called_once_with(
            mock.ANY,
            deadline=160.0,
        )
        print_output.assert_called_once_with(
            "TEL 憑證未設定，略過發送",
            flush=True,
        )

    def test_telegram_overall_deadline_returns_while_send_is_stuck(
        self,
    ) -> None:
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        self.addCleanup(release.set)

        def stuck_send(*_args: object, **_kwargs: object) -> int:
            started.set()
            release.wait(timeout=1)
            finished.set()
            return 1

        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = pathlib.Path(temp_dir) / "result.json"
            result_path.write_text(
                '{"date":"2026-07-24","stocks":[]}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    telegram_notify,
                    "send_telegram_message",
                    side_effect=stuck_send,
                ),
                mock.patch.object(
                    service.time,
                    "monotonic",
                    side_effect=(100.0, 100.0),
                ),
                mock.patch("builtins.print") as print_output,
            ):
                service.notify_telegram_result(
                    result_path,
                    deadline_seconds=0.01,
                )
                self.assertTrue(started.is_set())
                print_output.assert_called_once_with(
                    "TEL 通知失敗：TimeoutError",
                    flush=True,
                )
                release.set()
                self.assertTrue(finished.wait(timeout=1))

    def test_telegram_thread_start_failure_does_not_escape(self) -> None:
        with (
            mock.patch.object(
                service.threading,
                "Thread",
                side_effect=RuntimeError("模擬執行緒啟動失敗"),
            ),
            mock.patch("builtins.print") as print_output,
        ):
            service.start_telegram_result_notification(
                pathlib.Path("result.json")
            )

        print_output.assert_called_once_with(
            "TEL 通知失敗：RuntimeError",
            flush=True,
        )

    def test_telegram_failure_does_not_escape_or_log_credentials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = pathlib.Path(temp_dir) / "result.json"
            result_path.write_text(
                '{"date":"2026-07-24","stocks":[]}',
                encoding="utf-8",
            )
            with (
                mock.patch.object(
                    telegram_notify,
                    "send_telegram_message",
                    side_effect=telegram_notify.TelegramSendError(
                        "安全的模擬失敗"
                    ),
                ),
                mock.patch("builtins.print") as print_output,
            ):
                service.notify_telegram_result(result_path)

        print_output.assert_called_once_with(
            "TEL 通知失敗：TelegramSendError",
            flush=True,
        )

    def test_login_or_subscription_failure_reports_error(self) -> None:
        for login_ok, subscribe_ok in ((False, False), (True, False)):
            with self.subTest(
                login_ok=login_ok,
                subscribe_ok=subscribe_ok,
            ):
                runtime, _metadata = self.make_runtime()
                runtime.set_context(
                    service_status="live",
                    login_ok=login_ok,
                    subscribe_ok=subscribe_ok,
                )
                state = runtime.publish()
                self.assertEqual(state["service_status"], "error")
                self.assertIs(state["login_ok"], login_ok)
                self.assertIs(state["subscribe_ok"], subscribe_ok)

    def test_live_worker_preserves_login_success_on_subscription_error(
        self,
    ) -> None:
        runtime, _metadata = self.make_runtime()
        stop_event = threading.Event()
        now = service.taipei_now()
        start_at = now - timedelta(minutes=1)
        end_at = now + timedelta(minutes=1)
        start_clock, end_clock = service.default_clock("preopen")

        def fail_after_login(
            current_runtime: service.ServiceRuntime,
            **_kwargs: object,
        ) -> bool:
            self.assertIs(current_runtime, runtime)
            current_runtime.set_context(
                login_ok=True,
                subscribe_ok=False,
            )
            stop_event.set()
            raise service.LiveSourceError("subscription FAILED")

        with (
            mock.patch.object(
                service,
                "upcoming_window",
                return_value=(start_at, end_at),
            ),
            mock.patch.object(
                service,
                "run_one_live_window",
                side_effect=fail_after_login,
            ) as run_window,
        ):
            service.live_worker(
                runtime,
                session="preopen",
                start_clock=start_clock,
                end_clock=end_clock,
                universe_spec=None,
                stop_event=stop_event,
                record_enabled=False,
                record_out=None,
            )

        run_window.assert_called_once()
        state = runtime.publish()
        self.assertEqual(state["service_status"], "error")
        self.assertTrue(state["login_ok"])
        self.assertFalse(state["subscribe_ok"])

    def test_source_supervisor_restarts_non_live_source_error(
        self,
    ) -> None:
        runtime, _metadata = self.make_runtime()
        stop_event = threading.Event()
        calls: list[int] = []

        def flaky_worker() -> None:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                raise ValueError("受控的一般例外")
            stop_event.set()

        with mock.patch("builtins.print") as print_output:
            service.supervise_source_worker(
                runtime,
                flaky_worker,
                stop_event,
                max_consecutive_failures=3,
                retry_base_seconds=0,
                retry_max_seconds=0,
                stable_reset_seconds=60,
                heartbeat_interval_seconds=0.01,
            )

        self.assertEqual(calls, [1, 2])
        state = runtime.publish()
        self.assertEqual(state["source_restart_count"], 1)
        self.assertTrue(
            any(
                "ValueError" in str(call)
                for call in print_output.call_args_list
            )
        )

    def test_dead_live_source_is_error_through_api(self) -> None:
        runtime, _metadata = self.make_runtime()
        source_thread = threading.Thread(target=lambda: None)
        runtime.bind_source_thread(source_thread, required=True)
        source_thread.start()
        runtime.mark_source_thread_started()
        source_thread.join(timeout=1)
        runtime.update_source_health(
            worker_alive=False,
            error_type="InjectedWorkerDeath",
        )
        state = runtime.publish()

        self.assertEqual(state["service_status"], "error")
        self.assertFalse(state["source_alive"])
        self.assertFalse(state["source_thread_alive"])
        self.assertEqual(state["source_error"], "InjectedWorkerDeath")

        server = webserver.start_server(runtime.shared, port=0)
        try:
            port = int(server.server_address[1])
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state",
                timeout=2,
            ) as response:
                api_state = json.loads(response.read().decode("utf-8"))
        finally:
            webserver.stop_server(server)

        self.assertEqual(api_state["service_status"], "error")
        self.assertFalse(api_state["source_alive"])
        self.assertFalse(api_state["source_thread_alive"])

    def test_stop_wait_ignores_recording_control_changed_event(self) -> None:
        runtime, _metadata = self.make_runtime()
        control = service.RecordingControl(
            runtime,
            mode="live",
            auto_record_enabled=True,
            session="preopen",
            start_clock=datetime_time(8, 30),
            end_clock=datetime_time(9, 0),
        )
        session_stop = control.begin_live_session(service.taipei_now().date())
        self.assertIsNotNone(session_stop)
        control.end_session(session_stop)

        stop_event = threading.Event()
        started_at = service.time.monotonic()
        interrupted = service.stop_wait(stop_event, 0.05)
        elapsed = service.time.monotonic() - started_at

        self.assertFalse(interrupted)
        self.assertGreaterEqual(elapsed, 0.04)
        self.assertEqual(service.LOGIN_RETRY_SECONDS, 30.0)

    def test_login_failure_uses_stop_only_exponential_backoff(
        self,
    ) -> None:
        runtime, _metadata = self.make_runtime()
        control = service.RecordingControl(
            runtime,
            mode="live",
            auto_record_enabled=True,
            session="preopen",
            start_clock=datetime_time(8, 30),
            end_clock=datetime_time(9, 0),
        )
        stop_event = threading.Event()
        now = service.taipei_now()
        start_at = now - timedelta(minutes=1)
        end_at = now + timedelta(minutes=1)
        observed_waits: list[float] = []

        def stop_after_first_backoff(
            event: threading.Event,
            seconds: float,
        ) -> bool:
            self.assertIs(event, stop_event)
            observed_waits.append(seconds)
            event.set()
            return True

        with (
            mock.patch.object(
                service,
                "upcoming_window",
                return_value=(start_at, end_at),
            ),
            mock.patch.object(
                service,
                "run_one_live_window",
                side_effect=service.LiveLoginError("login FAILED"),
            ) as run_window,
            mock.patch.object(
                service,
                "stop_wait",
                side_effect=stop_after_first_backoff,
            ),
            mock.patch.object(
                control,
                "wait_for_change",
                wraps=control.wait_for_change,
            ) as changed_wait,
        ):
            service.live_worker(
                runtime,
                session="preopen",
                start_clock=datetime_time(8, 30),
                end_clock=datetime_time(9, 0),
                universe_spec=None,
                stop_event=stop_event,
                record_enabled=False,
                record_out=None,
                control=control,
            )

        run_window.assert_called_once()
        changed_wait.assert_not_called()
        self.assertEqual(observed_waits, [30.0])

    def test_login_circuit_opens_after_max_failures_and_persists(
        self,
    ) -> None:
        runtime, _metadata = self.make_runtime()
        control = service.RecordingControl(
            runtime,
            mode="live",
            auto_record_enabled=True,
            session="preopen",
            start_clock=datetime_time(8, 30),
            end_clock=datetime_time(9, 0),
        )
        stop_event = threading.Event()
        now = service.taipei_now()
        start_at = now - timedelta(minutes=1)
        end_at = now + timedelta(minutes=1)
        observed_waits: list[float] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = pathlib.Path(temp_dir) / "auction.jsonl"
            lock_manager = service.LiveRecordingLockManager.acquire_for(
                record_path
            )

            def fake_stop_wait(
                event: threading.Event,
                seconds: float,
            ) -> bool:
                observed_waits.append(seconds)
                if len(observed_waits) > 4:
                    event.set()
                    return True
                return False

            try:
                with (
                    mock.patch.object(
                        service,
                        "upcoming_window",
                        return_value=(start_at, end_at),
                    ),
                    mock.patch.object(
                        service,
                        "run_one_live_window",
                        side_effect=service.LiveLoginError(
                            "受控登入失敗"
                        ),
                    ) as run_window,
                    mock.patch.object(
                        service,
                        "stop_wait",
                        side_effect=fake_stop_wait,
                    ),
                ):
                    service.live_worker(
                        runtime,
                        session="preopen",
                        start_clock=datetime_time(8, 30),
                        end_clock=datetime_time(9, 0),
                        universe_spec=None,
                        stop_event=stop_event,
                        record_enabled=True,
                        record_out=record_path,
                        control=control,
                        lock_manager=lock_manager,
                    )
            finally:
                lock_manager.release()

            marker = service.read_login_circuit(
                record_path,
                start_at.date(),
            )

        self.assertEqual(
            run_window.call_count,
            service.LOGIN_MAX_CONSECUTIVE_FAILURES,
        )
        self.assertEqual(observed_waits[:4], [30.0, 60.0, 120.0, 240.0])
        self.assertIsNotNone(marker)
        self.assertEqual(
            marker["failure_count"],
            service.LOGIN_MAX_CONSECUTIVE_FAILURES,
        )
        state = runtime.publish()
        self.assertEqual(state["service_status"], "error")
        self.assertTrue(state["login_circuit_open"])
        self.assertEqual(
            state["login_failure_count"],
            service.LOGIN_MAX_CONSECUTIVE_FAILURES,
        )

    def test_live_recording_lock_rejects_second_process_path(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = pathlib.Path(temp_dir) / "auction.jsonl"
            first = service.LiveRecordingLockManager.acquire_for(record_path)
            try:
                with self.assertRaises(
                    service.LiveRecordingAlreadyActive
                ):
                    service.LiveRecordingLockManager.acquire_for(record_path)
            finally:
                first.release()

            self.assertFalse(record_path.exists())
            self.assertTrue(
                (record_path.parent / ".record.lock").exists()
            )

    def test_duplicate_live_main_yields_before_http_write_or_notify(
        self,
    ) -> None:
        now = service.taipei_now()
        start_at = now.replace(second=0, microsecond=0)
        end_at = start_at + timedelta(minutes=30)
        with tempfile.TemporaryDirectory() as temp_dir:
            record_path = pathlib.Path(temp_dir) / "auction.jsonl"
            first = service.LiveRecordingLockManager.acquire_for(record_path)
            try:
                with (
                    mock.patch.object(
                        service,
                        "upcoming_window",
                        return_value=(start_at, end_at),
                    ),
                    mock.patch.object(
                        service,
                        "prune_history",
                    ),
                    mock.patch.object(
                        service.control_state,
                        "get_auto_record_enabled",
                        return_value=True,
                    ),
                    mock.patch.object(
                        webserver,
                        "start_server",
                    ) as start_server,
                    mock.patch.object(
                        service,
                        "start_telegram_result_notification",
                    ) as start_notification,
                ):
                    exit_code = service.main(
                        [
                            "--host",
                            "127.0.0.1",
                            "--port",
                            "8927",
                            "--record-out",
                            str(record_path),
                        ]
                    )
            finally:
                first.release()
            record_created_before_cleanup = record_path.exists()

        self.assertEqual(exit_code, 0)
        start_server.assert_not_called()
        start_notification.assert_not_called()
        self.assertFalse(record_created_before_cleanup)

    def test_stale_stream_reports_degraded_through_api(self) -> None:
        runtime, _metadata = self.make_runtime(stale_after_seconds=1.0)
        old_event_at = service.taipei_now() - timedelta(seconds=30)
        runtime.set_context(
            service_status="live",
            login_ok=True,
            subscribe_ok=True,
            last_event_ts=old_event_at,
        )
        state = runtime.publish()
        self.assertEqual(state["service_status"], "degraded")
        self.assertGreaterEqual(state["last_event_age_sec"], 29.0)

        server = webserver.start_server(runtime.shared, port=0)
        try:
            port = int(server.server_address[1])
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/state",
                timeout=2,
            ) as response:
                api_state = json.loads(response.read().decode("utf-8"))
        finally:
            webserver.stop_server(server)

        self.assertEqual(api_state["service_status"], "degraded")
        self.assertTrue(
            STATE_CONTRACT_FIELDS.issubset(api_state),
            sorted(STATE_CONTRACT_FIELDS - set(api_state)),
        )
        self.assertTrue(
            TODAY_RECORDING_FIELDS.issubset(api_state["today_recording"]),
            sorted(
                TODAY_RECORDING_FIELDS
                - set(api_state["today_recording"])
            ),
        )
        self.assertTrue(api_state["login_ok"])
        self.assertTrue(api_state["subscribe_ok"])
        self.assertFalse(api_state["recording"])
        self.assertIn("anomaly", api_state["counts"])
        self.assertEqual(api_state["counts"]["anomaly"], 0)
        self.assertFalse(api_state["anomaly_thresholds"]["calibrated"])
        self.assertFalse(
            api_state["fake_grade_thresholds"]["calibrated"]
        )
        self.assertEqual(len(api_state["stocks"]), 1)
        self.assertEqual(api_state["stocks"][0]["anomalies"], [])
        self.assertEqual(api_state["stocks"][0]["anomaly_score"], 0)
        self.assertIn("max_bid0_volume", api_state["stocks"][0])
        self.assertIn("lock_duration_sec", api_state["stocks"][0])
        self.assertIn("open_gap_ref_pct", api_state["stocks"][0])
        self.assertIn("grade", api_state["stocks"][0])


if __name__ == "__main__":
    unittest.main()
