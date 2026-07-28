"""盤前試撮判定的 Telegram 純文字通知。

訊息組字與網路送出刻意分開，方便在不連網的情況下驗證內容。模組不會
輸出 bot token 或 chat id；呼叫端只需捕捉 ``TelegramNotifyError`` 並記錄
例外型別或固定文案即可。
"""

from __future__ import annotations

import json
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib import error, parse, request


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ENV_PATH = BASE_DIR / ".env"
TELEGRAM_MESSAGE_LIMIT = 4096

STATUS_LABELS = {
    "suspected_fake": "疑似假試撮",
    "locked_held": "鎖漲停守住",
    "touched": "曾觸漲停",
}
STATUS_RANK = {
    "suspected_fake": 0,
    "locked_held": 1,
    "touched": 2,
}


class TelegramNotifyError(RuntimeError):
    """Telegram 通知的可安全記錄基底例外。"""


class TelegramConfigError(TelegramNotifyError):
    """本機 Telegram 憑證缺失或無法讀取。"""


class TelegramSendError(TelegramNotifyError):
    """Telegram API 未接受訊息或回應無法判讀。"""


def load_dotenv_manually(path: Path) -> dict[str, str]:
    """解析簡單的 KEY=VALUE；不注入 process environment，也不輸出內容。"""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in {"'", '"'}
                ):
                    value = value[1:-1]
                values[key] = value
    except OSError as exc:
        raise TelegramConfigError("無法讀取 Telegram 憑證檔") from exc
    return values


def load_telegram_config(
    env_path: Path | str = DEFAULT_ENV_PATH,
) -> tuple[str, str] | None:
    """取得 bot token 與 chat id；缺漏或空白時回傳 ``None``。"""
    values = load_dotenv_manually(Path(env_path))
    token = values.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = values.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return None
    return token, chat_id


def _as_number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _number_text(value: Any, *, decimals: int = 4) -> str:
    number = _as_number(value)
    if number is None:
        return "—"
    quantized = f"{number:.{decimals}f}".rstrip("0").rstrip(".")
    return quantized or "0"


def _volume_text(value: Any) -> str:
    number = _as_number(value)
    if number is None:
        return "—"
    if number == number.to_integral_value():
        return f"{int(number):,}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def _percent_text(value: Any) -> str:
    number = _as_number(value)
    if number is None:
        return "—"
    return f"{number:.2f}%"


def _duration_text(value: Any) -> str:
    number = _as_number(value)
    if number is None or number < 0:
        return "—"
    return f"{number:.1f}".rstrip("0").rstrip(".") + " 秒"


def _time_text(value: Any) -> str:
    if not value:
        return "—"
    text = str(value).strip()
    if not text:
        return "—"
    if "T" in text:
        text = text.split("T", 1)[1]
    elif " " in text:
        text = text.rsplit(" ", 1)[-1]
    return text[:8] if len(text) >= 8 else text


def _date_text(value: Any) -> str:
    text = str(value or "").strip()
    digits = text.replace("-", "").replace("/", "")
    if len(digits) == 8 and digits.isdigit():
        return f"{digits[:4]}/{digits[4:6]}/{digits[6:]}"
    return text or "日期未明"


def _is_locked(stock: dict[str, Any]) -> bool:
    """相容 scanner 歷史 result 與 detector live state。"""
    if "locked_limit_up" in stock:
        return stock.get("locked_limit_up") is True
    return stock.get("locked") is True


def _lock_duration(stock: dict[str, Any]) -> Decimal | None:
    return _as_number(stock.get("lock_duration_sec"))


def locked_limit_up_stocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """取出曾鎖漲停個股並以結局、時長、代碼形成穩定排序。"""
    raw_stocks = result.get("stocks")
    if not isinstance(raw_stocks, list):
        return []
    stocks = [
        stock
        for stock in raw_stocks
        if isinstance(stock, dict) and _is_locked(stock)
    ]

    def sort_key(stock: dict[str, Any]) -> tuple[int, Decimal, str]:
        duration = _lock_duration(stock)
        return (
            STATUS_RANK.get(str(stock.get("status") or ""), 99),
            -(duration if duration is not None else Decimal("-1")),
            str(stock.get("code") or ""),
        )

    return sorted(stocks, key=sort_key)


def build_telegram_message(
    result: dict[str, Any],
    *,
    prefix: str | None = None,
) -> str:
    """由 result_YYYYMMDD.json 同構 dict 建立 Telegram 純文字摘要。"""
    if not isinstance(result, dict):
        raise TypeError("result 必須是 dict")

    raw_stocks = result.get("stocks")
    if not isinstance(raw_stocks, list):
        title = f"{_date_text(result.get('date'))} 盤前試撮鎖漲停判定"
        if prefix:
            title = f"{prefix.strip()}｜{title}"
        return "\n".join(
            (
                title,
                "",
                "result 格式異常，無法判定鎖漲停清單",
            )
        )

    stocks = locked_limit_up_stocks(result)
    counts = {
        status: sum(stock.get("status") == status for stock in stocks)
        for status in STATUS_LABELS
    }
    title = f"{_date_text(result.get('date'))} 盤前試撮鎖漲停判定"
    if prefix:
        title = f"{prefix.strip()}｜{title}"
    lines = [
        title,
        (
            f"摘要：曾鎖漲停 {len(stocks)} 檔｜"
            f"疑似假試撮 {counts['suspected_fake']}｜"
            f"鎖漲停守住 {counts['locked_held']}｜"
            f"曾觸漲停 {counts['touched']}"
        ),
    ]
    if not stocks:
        lines.extend(("", "今日試撮無個股鎖漲停"))
        return "\n".join(lines)

    for stock in stocks:
        status = str(stock.get("status") or "")
        status_label = STATUS_LABELS.get(
            status,
            str(stock.get("status_label") or "結局未明"),
        )
        code = str(stock.get("code") or "代碼未明").strip()
        name = str(stock.get("name") or "").strip()
        identity = f"{code} {name}".rstrip()
        peak_volume = stock.get("max_bid0_volume")
        if peak_volume is None:
            peak_volume = stock.get("bid0_peak_volume")
        lines.extend(
            (
                "",
                f"{identity}｜{status_label}",
                (
                    f"首次鎖住：{_time_text(stock.get('first_lock_time'))}｜"
                    f"鎖住時長：{_duration_text(stock.get('lock_duration_sec'))}"
                ),
                (
                    f"掛單峰量：{_volume_text(peak_volume)}｜"
                    f"撤單：{_percent_text(stock.get('bid0_withdraw_pct'))}"
                ),
                (
                    f"開盤：{_number_text(stock.get('open_price'))}｜"
                    f"缺口：{_percent_text(stock.get('open_gap_pct'))}"
                ),
            )
        )
    return "\n".join(lines)


def _split_message(message: str) -> list[str]:
    """依 Telegram 字數限制切段，優先保留個股段落邊界。"""
    if len(message) <= TELEGRAM_MESSAGE_LIMIT:
        return [message]

    chunks: list[str] = []
    current = ""
    for paragraph in message.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= TELEGRAM_MESSAGE_LIMIT:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > TELEGRAM_MESSAGE_LIMIT:
            chunks.append(paragraph[:TELEGRAM_MESSAGE_LIMIT])
            paragraph = paragraph[TELEGRAM_MESSAGE_LIMIT:]
        current = paragraph
    if current:
        chunks.append(current)
    return chunks


def _send_chunk(
    message: str,
    *,
    token: str,
    chat_id: str,
    timeout: float,
) -> None:
    encoded_token = parse.quote(token, safe=":")
    url = f"https://api.telegram.org/bot{encoded_token}/sendMessage"
    body = parse.urlencode(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise TelegramSendError(
            f"Telegram API HTTP {exc.code}"
        ) from None
    except (error.URLError, TimeoutError, OSError):
        raise TelegramSendError("Telegram API 連線失敗") from None
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise TelegramSendError("Telegram API 回應格式錯誤") from None
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramSendError("Telegram API 回報送出失敗")


def send_telegram_message(
    message: str,
    *,
    env_path: Path | str = DEFAULT_ENV_PATH,
    timeout: float = 15.0,
    deadline: float | None = None,
) -> int | None:
    """送出純文字訊息。

    成功時回傳 API 請求數；憑證缺漏或空白時回傳 ``None`` 且不連網。
    ``deadline`` 是 ``time.monotonic()`` 絕對秒，逾期即停止後續分段。
    讀檔、網路與 API 失敗會拋出不含憑證的通知器例外，交由呼叫端處理。
    """
    if not isinstance(message, str) or not message.strip():
        raise ValueError("message 不得為空")
    if timeout <= 0:
        raise ValueError("timeout 必須大於 0")
    config = load_telegram_config(env_path)
    if config is None:
        return None
    token, chat_id = config
    chunks = _split_message(message)
    for chunk in chunks:
        chunk_timeout = timeout
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TelegramSendError("Telegram 通知超過整體期限")
            chunk_timeout = min(timeout, remaining)
        _send_chunk(
            chunk,
            token=token,
            chat_id=chat_id,
            timeout=chunk_timeout,
        )
    return len(chunks)


__all__ = [
    "TelegramConfigError",
    "TelegramNotifyError",
    "TelegramSendError",
    "build_telegram_message",
    "load_telegram_config",
    "locked_limit_up_stocks",
    "send_telegram_message",
]
