"""service.py live 錄製與誠實狀態契約的離線測試。"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import timedelta
from unittest import mock

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

import scanner
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
