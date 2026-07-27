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
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

import scanner
import recorder
import service
import webserver


STATE_CONTRACT_FIELDS = {
    "recording",
    "record_path",
    "record_count",
    "last_event_age_sec",
    "login_ok",
    "subscribe_ok",
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
            service.replay_worker(
                runtime,
                rows,
                speed=1_000_000_000.0,
                stop_event=threading.Event(),
            )
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
                    "SNAPSHOT_AFTER_END_SECONDS",
                    0.01,
                ),
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
                )

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
        self.assertTrue(api_state["login_ok"])
        self.assertTrue(api_state["subscribe_ok"])
        self.assertFalse(api_state["recording"])
        self.assertIn("anomaly", api_state["counts"])
        self.assertEqual(api_state["counts"]["anomaly"], 0)
        self.assertFalse(api_state["anomaly_thresholds"]["calibrated"])
        self.assertEqual(len(api_state["stocks"]), 1)
        self.assertEqual(api_state["stocks"][0]["anomalies"], [])
        self.assertEqual(api_state["stocks"][0]["anomaly_score"], 0)


if __name__ == "__main__":
    unittest.main()
