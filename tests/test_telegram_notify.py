"""Telegram 盤前試撮通知的離線單元測試。"""

from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock
from urllib import parse

sys.path.insert(
    0,
    str(pathlib.Path(__file__).resolve().parent.parent),
)

import telegram_notify


ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_history_result(
    date_key: str,
    fallback: dict[str, object],
) -> dict[str, object]:
    """優先驗證本機歷史 result；乾淨 clone 則使用等價的小型 fixture。"""
    result_path = (
        ROOT
        / "data"
        / "history"
        / date_key
        / f"result_{date_key}.json"
    )
    if not result_path.is_file():
        return fallback
    with result_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class TelegramNotifyTests(unittest.TestCase):
    def test_history_result_contains_all_locked_codes_and_outcomes(self) -> None:
        result = load_history_result(
            "20260724",
            {
                "date": "2026-07-24",
                "stocks": [
                    {
                        "code": "8039",
                        "name": "台虹",
                        "locked_limit_up": True,
                        "status": "suspected_fake",
                        "lock_duration_sec": 125.4,
                    },
                    {
                        "code": "2201",
                        "name": "裕隆",
                        "locked_limit_up": True,
                        "status": "suspected_fake",
                        "lock_duration_sec": 10.0,
                    },
                    {
                        "code": "2392",
                        "name": "正崴",
                        "locked_limit_up": True,
                        "status": "suspected_fake",
                        "lock_duration_sec": 0.0,
                    },
                ],
            },
        )

        message = telegram_notify.build_telegram_message(result)

        self.assertIn("2026/07/24 盤前試撮鎖漲停判定", message)
        self.assertIn("摘要：曾鎖漲停 3 檔", message)
        for code in ("8039", "2392", "2201"):
            self.assertIn(code, message)
        self.assertEqual(message.count("疑似假試撮"), 4)
        self.assertIn("首次鎖住：", message)
        self.assertIn("鎖住時長：", message)
        self.assertIn("掛單峰量：", message)
        self.assertIn("撤單：", message)
        self.assertIn("開盤：", message)
        self.assertIn("缺口：", message)
        self.assertLess(message.index("8039"), message.index("2201"))
        self.assertLess(message.index("2201"), message.index("2392"))

    def test_history_result_supports_touched_outcome(self) -> None:
        result = load_history_result(
            "20260723",
            {
                "date": "2026-07-23",
                "stocks": [
                    {
                        "code": code,
                        "locked_limit_up": True,
                        "status": "touched",
                    }
                    for code in ("2880", "3081", "8039")
                ],
            },
        )

        message = telegram_notify.build_telegram_message(result)

        for code in ("2880", "3081", "8039"):
            self.assertIn(code, message)
        self.assertEqual(message.count("曾觸漲停"), 4)

    def test_no_locked_stock_builds_short_message(self) -> None:
        result = {
            "date": "20260725",
            "stocks": [
                {
                    "code": "2330",
                    "name": "台積電",
                    "status": "watching",
                    "locked_limit_up": False,
                }
            ],
        }

        message = telegram_notify.build_telegram_message(result)

        self.assertIn("摘要：曾鎖漲停 0 檔", message)
        self.assertIn("今日試撮無個股鎖漲停", message)
        self.assertNotIn("2330 台積電", message)

    def test_live_state_aliases_and_status_order_are_supported(self) -> None:
        result = {
            "date": "2026-07-26",
            "stocks": [
                {
                    "code": "3003",
                    "name": "曾觸",
                    "locked": True,
                    "status": "touched",
                    "bid0_peak_volume": 30,
                },
                {
                    "code": "3002",
                    "name": "守住",
                    "locked": True,
                    "status": "locked_held",
                    "bid0_peak_volume": 20,
                },
                {
                    "code": "3001",
                    "name": "疑似",
                    "locked": True,
                    "status": "suspected_fake",
                    "bid0_peak_volume": 10,
                },
            ],
        }

        message = telegram_notify.build_telegram_message(result)

        self.assertLess(message.index("3001"), message.index("3002"))
        self.assertLess(message.index("3002"), message.index("3003"))
        self.assertIn("掛單峰量：10", message)
        self.assertIn("鎖住時長：—", message)

    def test_send_uses_mocked_urlopen_and_returns_safe_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = pathlib.Path(temp_dir) / ".env"
            env_path.write_text(
                "TELEGRAM_BOT_TOKEN=fake-token\n"
                "TELEGRAM_CHAT_ID=fake-chat\n",
                encoding="utf-8",
            )
            response = io.BytesIO(b'{"ok": true, "result": {"message_id": 1}}')
            with mock.patch(
                "telegram_notify.request.urlopen",
                return_value=response,
            ) as urlopen:
                sent_count = telegram_notify.send_telegram_message(
                    "離線測試",
                    env_path=env_path,
                )

        self.assertEqual(sent_count, 1)
        request_arg = urlopen.call_args.args[0]
        body = parse.parse_qs(request_arg.data.decode("utf-8"))
        self.assertEqual(body["text"], ["離線測試"])
        self.assertEqual(body["chat_id"], ["fake-chat"])

    def test_empty_credentials_are_normal_and_do_not_touch_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = pathlib.Path(temp_dir) / ".env"
            env_path.write_text(
                "TELEGRAM_BOT_TOKEN=\n"
                "TELEGRAM_CHAT_ID=\n",
                encoding="utf-8",
            )
            with mock.patch("telegram_notify.request.urlopen") as urlopen:
                result = telegram_notify.send_telegram_message(
                    "不應送出",
                    env_path=env_path,
                )

        self.assertIsNone(result)
        urlopen.assert_not_called()

    def test_missing_env_file_is_also_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = pathlib.Path(temp_dir) / "missing.env"
            with mock.patch("telegram_notify.request.urlopen") as urlopen:
                result = telegram_notify.send_telegram_message(
                    "不應送出",
                    env_path=env_path,
                )

        self.assertIsNone(result)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
