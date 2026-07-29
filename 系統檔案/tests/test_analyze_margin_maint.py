"""融資維持率公式、風險帶與資料鏈中斷測試。"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from data.analysis.analyze_margin_maint import (
    DayResult,
    build_stock_result,
    maintenance_rate,
    risk_level,
    roll_cost,
)


class MarginMaintenanceTests(unittest.TestCase):
    def test_roll_cost_matches_cmoney_recursion(self) -> None:
        cost = roll_cost(
            previous_cost=40.0,
            close=36.0,
            buy=100,
            sell=50,
            cash_repayment=10,
            previous_balance=1_000,
            balance=1_040,
        )
        self.assertAlmostEqual(
            cost,
            (40.0 * (1_000 - 50 - 10) + 36.0 * 100) / 1_040,
        )
        self.assertAlmostEqual(
            maintenance_rate(close=36.0, cost=cost),
            36.0 / (cost * 0.6) * 100,
        )

    def test_roll_cost_zero_balance_and_new_position(self) -> None:
        self.assertEqual(roll_cost(None, 25.0, 0, 100, 0, 100, 0), 0.0)
        self.assertEqual(roll_cost(None, 25.0, 20, 0, 0, 0, 20), 25.0)

    def test_risk_band_boundaries(self) -> None:
        self.assertEqual(risk_level(129.999), "追繳")
        self.assertEqual(risk_level(130.0), "警戒")
        self.assertEqual(risk_level(149.999), "警戒")
        self.assertEqual(risk_level(150.0), "正常")
        self.assertEqual(risk_level(200.0), "正常")
        self.assertEqual(risk_level(200.001), "安全")
        self.assertEqual(risk_level(None), "未取得")

    def test_twenty_day_chain_is_auditable(self) -> None:
        start = date(2026, 1, 2)
        days: list[DayResult] = []
        balance = 100
        for index in range(20):
            day = start + timedelta(days=index)
            previous = balance
            buy = 10 if index == 1 else 0
            balance = previous + buy
            close = 10.0 + index * 0.1
            days.append(
                DayResult(
                    market="twse",
                    date=day.isoformat(),
                    status="ok",
                    source="test",
                    stocks={
                        "8039": {
                            "stock_id": "8039",
                            "name": "台虹",
                            "market": "twse",
                            "date": day.isoformat(),
                            "close": close,
                            "buy_lots": buy,
                            "sell_lots": 0,
                            "cash_repayment_lots": 0,
                            "previous_balance_lots": previous,
                            "balance_lots": balance,
                            "flow_identity_ok": True,
                        }
                    },
                )
            )
        result = build_stock_result("8039", days, [], required_sessions=20)
        self.assertEqual(result["availability"], "available")
        self.assertEqual(len(result["series"]), 20)
        expected_second_cost = (10.0 * 100 + 10.1 * 10) / 110
        self.assertAlmostEqual(
            result["series"][1]["financing_cost"], expected_second_cost, places=6
        )
        self.assertEqual(result["trend_20d"]["sample_count"], 20)

    def test_unconfirmed_gap_blocks_recursion(self) -> None:
        day = DayResult(
            market="twse",
            date="2026-01-02",
            status="ok",
            source="test",
            stocks={
                "8039": {
                    "stock_id": "8039",
                    "name": "台虹",
                    "market": "twse",
                    "date": "2026-01-02",
                    "close": 10.0,
                    "buy_lots": 0,
                    "sell_lots": 0,
                    "cash_repayment_lots": 0,
                    "previous_balance_lots": 100,
                    "balance_lots": 100,
                    "flow_identity_ok": True,
                }
            },
        )
        result = build_stock_result(
            "8039", [day], ["2026-01-02"], required_sessions=1
        )
        self.assertEqual(result["availability"], "unavailable")
        self.assertIn("未確認資料缺口", result["reason"])

    def test_official_previous_balance_restatement_is_audited_not_blocked(self) -> None:
        days = [
            DayResult(
                market="twse",
                date="2026-06-17",
                status="ok",
                source="test",
                stocks={
                    "8039": {
                        "stock_id": "8039",
                        "name": "台虹",
                        "market": "twse",
                        "date": "2026-06-17",
                        "close": 100.0,
                        "buy_lots": 0,
                        "sell_lots": 0,
                        "cash_repayment_lots": 0,
                        "previous_balance_lots": 100,
                        "balance_lots": 100,
                        "flow_identity_ok": True,
                    }
                },
            ),
            DayResult(
                market="twse",
                date="2026-06-18",
                status="ok",
                source="test",
                stocks={
                    "8039": {
                        "stock_id": "8039",
                        "name": "台虹",
                        "market": "twse",
                        "date": "2026-06-18",
                        "close": 110.0,
                        "buy_lots": 10,
                        "sell_lots": 0,
                        "cash_repayment_lots": 0,
                        "previous_balance_lots": 99,
                        "balance_lots": 109,
                        "flow_identity_ok": True,
                    }
                },
            ),
        ]
        result = build_stock_result("8039", days, [], required_sessions=2)
        self.assertEqual(result["availability"], "available")
        self.assertEqual(result["quality"]["balance_continuity_failures"], 1)
        self.assertAlmostEqual(
            result["series"][1]["financing_cost"],
            (100.0 * 99 + 110.0 * 10) / 109,
            places=6,
        )

    def test_zero_buy_suspension_carries_cost_without_inventing_close(self) -> None:
        days = [
            DayResult(
                market="twse",
                date="2026-05-12",
                status="ok",
                source="test",
                stocks={
                    "8039": {
                        "stock_id": "8039",
                        "name": "台虹",
                        "market": "twse",
                        "date": "2026-05-12",
                        "close": 50.0,
                        "buy_lots": 0,
                        "sell_lots": 0,
                        "cash_repayment_lots": 0,
                        "previous_balance_lots": 100,
                        "balance_lots": 100,
                        "flow_identity_ok": True,
                    }
                },
            ),
            DayResult(
                market="twse",
                date="2026-05-13",
                status="ok",
                source="test",
                stocks={
                    "8039": {
                        "stock_id": "8039",
                        "name": "台虹",
                        "market": "twse",
                        "date": "2026-05-13",
                        "close": None,
                        "buy_lots": 0,
                        "sell_lots": 0,
                        "cash_repayment_lots": 5,
                        "previous_balance_lots": 100,
                        "balance_lots": 95,
                        "flow_identity_ok": True,
                    }
                },
            ),
        ]
        result = build_stock_result("8039", days, [], required_sessions=2)
        self.assertEqual(result["availability"], "available")
        self.assertEqual(result["series"][1]["financing_cost"], 50.0)
        self.assertIsNone(result["series"][1]["maintenance_rate_pct"])
        self.assertEqual(result["quality"]["no_close_cost_carries"], 1)


if __name__ == "__main__":
    unittest.main()
