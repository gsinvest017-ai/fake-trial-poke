"""detector.py 的可重跑邊界測試（不登入、不讀寫行情檔）。"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

from detector import (
    ANOM_BID0_SWING,
    ANOM_BIG_BID_WITHDRAW,
    ANOM_OPEN_GAP,
    Detector,
)


def bidask(
    code: str,
    ts: str,
    bid0: float,
    volume: int,
    *,
    simtrade: bool = True,
) -> dict[str, object]:
    return {
        "code": code,
        "name": code,
        "ts": ts,
        "kind": "bidask",
        "simtrade": simtrade,
        "bid_price": [bid0],
        "bid_volume": [volume],
        "ask_price": [None],
        "ask_volume": [0],
    }


def snapshot(
    code: str,
    ts: str,
    open_price: float | None,
    bid0: float | None,
    volume: int | None,
    *,
    ask0: float | None = None,
) -> dict[str, object]:
    return {
        "code": code,
        "name": code,
        "ts": ts,
        "kind": "snapshot",
        "simtrade": False,
        "open_price": open_price,
        "bid_price": [bid0],
        "bid_volume": [volume],
        "ask_price": [ask0],
        "ask_volume": [0],
    }


def stock(detector: Detector, code: str) -> dict[str, object]:
    matches = [
        item
        for item in detector.get_state()["stocks"]
        if item["code"] == code
    ]
    if len(matches) != 1:
        raise AssertionError(f"{code} 結果筆數不是 1：{len(matches)}")
    return matches[0]


class DetectorTests(unittest.TestCase):
    def make_detector(
        self,
        *stocks: dict[str, object],
        session: str = "preopen",
        start: str = "08:30",
        end: str = "09:00",
    ) -> Detector:
        return Detector(
            session=session,
            window_start=start,
            window_end=end,
            stocks=list(stocks),
            spark_points=4,
        )

    def test_locked_detection_records_first_lock_and_alert(self) -> None:
        detector = self.make_detector(
            {"code": "9001", "name": "鎖定", "limit_up": 100.0}
        )
        detector.process_event(
            bidask("9001", "2026-07-24T08:59:50", 100.0, 5000)
        )

        result = stock(detector, "9001")
        self.assertTrue(result["locked"])
        self.assertTrue(result["locked_limit_up"])
        self.assertEqual(result["status"], "touched")
        self.assertEqual(result["sim_high"], 100.0)
        self.assertTrue(
            str(result["first_lock_time"]).startswith(
                "2026-07-24T08:59:50"
            )
        )
        self.assertTrue(
            str(result["last_lock_time"]).startswith(
                "2026-07-24T08:59:50"
            )
        )
        self.assertEqual(result["lock_duration_sec"], 0.0)
        self.assertEqual(result["max_bid0_volume"], 5000)
        self.assertEqual(detector.get_state()["counts"]["touched"], 1)
        self.assertEqual(detector.get_state()["alerts"][0]["type"], "locked")

        detector.process_event(
            bidask("9001", "2026-07-24T08:59:55", 100.0, 4000)
        )
        result = stock(detector, "9001")
        self.assertTrue(
            str(result["last_lock_time"]).startswith(
                "2026-07-24T08:59:55"
            )
        )
        self.assertEqual(result["lock_duration_sec"], 5.0)

    def test_lock_duration_matches_scanner_first_to_last_lock(self) -> None:
        detector = self.make_detector(
            {"code": "DURATION", "name": "累計時長", "limit_up": 100.0}
        )
        events = [
            # 鎖 60 秒。
            bidask(
                "DURATION", "2026-07-24T08:30:00", 100.0, 5000
            ),
            bidask(
                "DURATION", "2026-07-24T08:31:00", 99.0, 4000
            ),
            # 解鎖 120 秒後再鎖 30 秒。
            bidask(
                "DURATION", "2026-07-24T08:33:00", 100.0, 3000
            ),
            bidask(
                "DURATION", "2026-07-24T08:33:30", 100.0, 2000
            ),
        ]
        detector.process_events(events)

        result = stock(detector, "DURATION")
        # scanner.py 以所有鎖定 BidAsk 的 first/last 相減，包含中途解鎖區段。
        self.assertEqual(result["lock_duration_sec"], 210.0)
        self.assertTrue(
            str(result["first_lock_time"]).startswith(
                "2026-07-24T08:30:00"
            )
        )
        self.assertTrue(
            str(result["last_lock_time"]).startswith(
                "2026-07-24T08:33:30"
            )
        )

        # 窗口後 snapshot 不得把最後鎖住狀態外推到 snapshot 時間。
        detector.process_event(
            snapshot(
                "DURATION",
                "2026-07-24T09:00:03",
                100.0,
                100.0,
                2000,
            )
        )
        self.assertEqual(
            stock(detector, "DURATION")["lock_duration_sec"],
            210.0,
        )

    def test_open_zero_is_unknown_and_snapshot_quote_means_held(self) -> None:
        detector = self.make_detector(
            {"code": "9002", "name": "零開盤", "limit_up": 100.0}
        )
        detector.process_event(
            bidask("9002", "2026-07-24T08:59:50", 100.0, 5000)
        )
        detector.process_event(
            snapshot(
                "9002",
                "2026-07-24T09:00:03",
                0.0,
                100.0,
                5000,
            )
        )

        result = stock(detector, "9002")
        self.assertIsNone(result["open_price"])
        self.assertIsNone(result["open_gap_pct"])
        self.assertEqual(result["status"], "locked_held")
        self.assertEqual(detector.get_state()["counts"]["locked_held"], 1)

    def test_bid0_drop_uses_all_three_existing_thresholds(self) -> None:
        detector = self.make_detector(
            {"code": "9003", "name": "撤單", "limit_up": 88.0}
        )
        detector.process_event(
            bidask("9003", "2026-07-24T08:59:50", 88.0, 2000)
        )
        detector.process_event(
            snapshot(
                "9003",
                "2026-07-24T09:00:03",
                88.0,
                88.0,
                100,
            )
        )

        result = stock(detector, "9003")
        self.assertTrue(result["bid0_dropped"])
        self.assertEqual(result["status"], "suspected_fake")
        self.assertEqual(
            [alert["type"] for alert in detector.get_state()["alerts"]],
            ["suspected_fake", "locked"],
        )

        exact_ratio = self.make_detector(
            {"code": "9004", "name": "等於門檻", "limit_up": 88.0}
        )
        exact_ratio.process_event(
            bidask("9004", "2026-07-24T08:59:50", 88.0, 2000)
        )
        exact_ratio.process_event(
            snapshot(
                "9004",
                "2026-07-24T09:00:03",
                88.0,
                88.0,
                600,
            )
        )
        self.assertFalse(stock(exact_ratio, "9004")["bid0_dropped"])
        self.assertEqual(stock(exact_ratio, "9004")["status"], "locked_held")

    def test_locked_is_strictly_inside_preopen_window(self) -> None:
        detector = self.make_detector(
            {"code": "START", "name": "起點", "limit_up": 100.0},
            {"code": "END", "name": "終點", "limit_up": 100.0},
            {"code": "AFTER", "name": "盤後", "limit_up": 100.0},
        )
        detector.process_event(
            bidask("START", "2026-07-24T08:30:00", 100.0, 5000)
        )
        detector.process_event(
            bidask("END", "2026-07-24T09:00:00", 100.0, 5000)
        )
        detector.process_event(
            bidask("AFTER", "2026-07-24T09:01:00", 100.0, 5000)
        )

        self.assertTrue(stock(detector, "START")["locked"])
        self.assertFalse(stock(detector, "END")["locked"])
        self.assertFalse(stock(detector, "AFTER")["locked"])
        self.assertEqual(stock(detector, "END")["status"], "watching")

    def test_snapshot_before_open_is_not_outcome_evidence(self) -> None:
        detector = self.make_detector(
            {"code": "EARLY", "name": "過早", "limit_up": 100.0}
        )
        detector.process_event(
            bidask("EARLY", "2026-07-24T08:59:50", 100.0, 5000)
        )
        detector.process_event(
            snapshot(
                "EARLY",
                "2026-07-24T08:59:59",
                95.0,
                95.0,
                5000,
            )
        )

        result = stock(detector, "EARLY")
        self.assertIsNone(result["open_price"])
        self.assertIsNone(result["open_gap_pct"])
        self.assertEqual(result["status"], "touched")

    def test_invalid_limit_up_stays_unknown(self) -> None:
        detector = self.make_detector(
            {"code": "ZERO", "name": "未知漲停", "limit_up": 0.0}
        )
        detector.process_event(
            {
                "code": "ZERO",
                "ts": "2026-07-24T08:59:50",
                "kind": "tick",
                "simtrade": True,
                "price": 100.0,
                "chg_type": 1,
            }
        )
        detector.process_event(
            bidask("ZERO", "2026-07-24T08:59:51", 100.0, 5000)
        )

        result = stock(detector, "ZERO")
        self.assertIsNone(result["limit_up"])
        self.assertFalse(result["locked"])
        self.assertEqual(result["status"], "none")

    def test_tiny_negative_gap_is_within_existing_tolerance(self) -> None:
        detector = self.make_detector(
            {"code": "TINY", "name": "微負", "limit_up": 100.0}
        )
        detector.process_event(
            bidask("TINY", "2026-07-24T08:59:50", 100.0, 5000)
        )
        detector.process_event(
            snapshot(
                "TINY",
                "2026-07-24T09:00:03",
                99.9999,
                100.0,
                5000,
            )
        )

        result = stock(detector, "TINY")
        self.assertEqual(result["open_gap_pct"], -0.0001)
        self.assertEqual(result["status"], "locked_held")

    def test_preclose_legacy_snapshot_uses_observed_time_and_quotes(self) -> None:
        metadata = {
            "session": "preclose",
            "window": {"start": "13:25", "end": "13:30"},
            "universe_size": 2,
            "subscribed": 1,
            "subscribed_codes": ["2880"],
            "snapshot_count": 2,
            "generated_at": "2026-07-23T13:30:00",
            "stocks": [
                {"code": "2880", "name": "華南金", "limit_up": 45.35},
                {"code": "DROP", "name": "未訂閱", "limit_up": 10.0},
            ],
        }
        detector = Detector()
        detector.register_stocks(metadata)
        # 模擬舊檔順序：stale snapshot 先到，之後才 replay 盤前事件。
        detector.process_event(
            snapshot(
                "2880",
                "2026-07-23T13:24:58",
                41.3,
                40.65,
                1,
            )
        )
        # recorder snapshot 涵蓋 universe 268 檔；未訂閱的 dropped code
        # 不得因此混回 stocks/watch counts。
        detector.process_event(
            snapshot(
                "DROP",
                "2026-07-23T13:24:59",
                9.0,
                9.0,
                10,
            )
        )
        detector.process_event(
            bidask("2880", "2026-07-23T13:25:29", 45.35, 1647)
        )

        result = stock(detector, "2880")
        self.assertTrue(result["locked"])
        # preclose 不可拿當日 open=41.3 當窗口結果，但最佳買價已離漲停。
        self.assertIsNone(result["open_price"])
        self.assertEqual(result["status"], "suspected_fake")
        self.assertEqual(len(detector.get_state()["stocks"]), 1)
        full = detector.build_state(
            "replay", now="2026-07-23T13:30:01"
        )
        self.assertEqual(full["universe"], 2)
        self.assertEqual(full["subscribed"], 1)
        self.assertEqual(full["counts"]["suspected_fake"], 1)

    def test_all_anomaly_dimensions_coexist_with_existing_status(self) -> None:
        detector = self.make_detector(
            {
                "code": "ANOM",
                "name": "多維異常",
                "reference": 100.0,
                "limit_up": 110.0,
            }
        )
        detector.process_event(
            bidask("ANOM", "2026-07-24T08:40:00", 100.0, 300)
        )
        detector.process_event(
            bidask("ANOM", "2026-07-24T08:50:00", 110.0, 400)
        )
        detector.process_event(
            bidask("ANOM", "2026-07-24T08:59:59", 99.0, 80)
        )
        detector.process_event(
            snapshot(
                "ANOM",
                "2026-07-24T09:00:03",
                104.0,
                99.0,
                80,
            )
        )

        result = stock(detector, "ANOM")
        self.assertEqual(result["status"], "suspected_fake")
        self.assertTrue(result["locked"])
        self.assertEqual(
            result["anomalies"],
            [
                ANOM_BIG_BID_WITHDRAW,
                ANOM_BID0_SWING,
                ANOM_OPEN_GAP,
            ],
        )
        self.assertEqual(result["anomaly_score"], 3)
        self.assertEqual(result["reference"], 100.0)
        self.assertEqual(result["bid0_peak_volume"], 400)
        self.assertEqual(result["final_window_bid0_volume"], 80)
        self.assertEqual(result["bid0_withdraw_pct"], 80.0)
        self.assertEqual(result["bid0_min_price"], 99.0)
        self.assertEqual(result["bid0_max_price"], 110.0)
        self.assertEqual(result["bid0_swing_pct"], 11.0)
        self.assertEqual(result["reference_open_gap_pct"], 4.0)
        self.assertEqual(result["open_gap_ref_pct"], 4.0)
        self.assertEqual(result["open_gap_direction"], "up")
        self.assertEqual(result["grade"], "T2")
        self.assertEqual(result["grade_label"], "中量")
        self.assertEqual(detector.get_state()["counts"]["anomaly"], 1)
        self.assertFalse(
            detector.get_state()["fake_grade_thresholds"]["calibrated"]
        )

    def test_anomaly_boundaries_and_zero_bid_price_handling(self) -> None:
        detector = self.make_detector(
            {
                "code": "EDGE",
                "name": "門檻邊界",
                "reference": 100.0,
                "limit_up": 110.0,
            }
        )
        detector.process_event(
            bidask("EDGE", "2026-07-24T08:39:00", 0.0, 0)
        )
        detector.process_event(
            bidask("EDGE", "2026-07-24T08:40:00", 100.0, 300)
        )
        detector.process_event(
            bidask("EDGE", "2026-07-24T08:59:59", 105.0, 90)
        )
        detector.process_event(
            snapshot(
                "EDGE",
                "2026-07-24T09:00:03",
                96.0,
                105.0,
                90,
            )
        )

        result = stock(detector, "EDGE")
        # 殘量恰為 30% 不符合「<30%」；振幅與跳空恰等於門檻則命中。
        self.assertNotIn(ANOM_BIG_BID_WITHDRAW, result["anomalies"])
        self.assertEqual(
            result["anomalies"],
            [ANOM_BID0_SWING, ANOM_OPEN_GAP],
        )
        self.assertEqual(result["anomaly_score"], 2)
        self.assertEqual(result["bid0_withdraw_pct"], 70.0)
        self.assertEqual(result["bid0_min_price"], 100.0)
        self.assertEqual(result["bid0_max_price"], 105.0)
        self.assertEqual(result["bid0_swing_pct"], 5.0)
        self.assertEqual(result["reference_open_gap_pct"], -4.0)
        self.assertEqual(result["open_gap_direction"], "down")

    def test_open_gap_anomaly_requires_snapshot_open(self) -> None:
        detector = self.make_detector(
            {
                "code": "TICKOPEN",
                "name": "只有開盤 tick",
                "reference": 100.0,
                "limit_up": 110.0,
            }
        )
        detector.process_event(
            {
                "code": "TICKOPEN",
                "ts": "2026-07-24T09:00:01",
                "kind": "tick",
                "simtrade": False,
                "price": 104.0,
            }
        )

        result = stock(detector, "TICKOPEN")
        # 既有狀態仍可使用 opening tick，但新跳空維度只接受 snapshot open。
        self.assertEqual(result["open_price"], 104.0)
        self.assertEqual(result["open_gap_ref_pct"], 4.0)
        self.assertIsNone(result["reference_open_gap_pct"])
        self.assertNotIn(ANOM_OPEN_GAP, result["anomalies"])

    def test_fake_grade_boundaries_and_state_date(self) -> None:
        metadata = {
            "date": "20260728",
            "stocks": [
                {
                    "code": code,
                    "name": code,
                    "reference": 90.0,
                    "limit_up": 100.0,
                }
                for code in ("T1", "T2", "T3", "T4")
            ],
        }
        detector = Detector(
            stocks=metadata,
            window_start="08:30",
            window_end="09:00",
        )
        for code, volume in (
            ("T1", 500),
            ("T2", 200),
            ("T3", 100),
            ("T4", 99),
        ):
            detector.process_event(
                bidask(code, "2026-07-28T08:59:50", 100.0, volume)
            )
            detector.process_event(
                snapshot(
                    code,
                    "2026-07-28T09:00:03",
                    90.0,
                    90.0,
                    volume,
                )
            )

        state = detector.build_state(
            "replay",
            now="2026-07-28T09:00:03",
        )
        by_code = {item["code"]: item for item in state["stocks"]}
        self.assertEqual(state["date"], "2026-07-28")
        self.assertEqual(
            {code: by_code[code]["grade"] for code in by_code},
            {"T1": "T1", "T2": "T2", "T3": "T3", "T4": "T4"},
        )
        for item in by_code.values():
            self.assertEqual(item["open_gap_ref_pct"], 0.0)


if __name__ == "__main__":
    unittest.main()
