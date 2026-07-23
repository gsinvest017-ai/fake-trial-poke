from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
TAIPEI = timezone(timedelta(hours=8))
SESSION_WINDOWS = {
    "preopen": ("08:30", "09:00"),
    "preclose": ("13:25", "13:30"),
}
STATUS_LABELS = {
    "suspected_fake": "疑似假試撮",
    "locked_held": "鎖漲停守住",
    "touched": "曾觸漲停",
    "none": "未觸及",
}


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(
                encoding="utf-8",
                errors="backslashreplace",
                line_buffering=True,
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="串接試撮 recorder → scanner → 離線 dashboard"
    )
    parser.add_argument(
        "--session",
        choices=tuple(SESSION_WINDOWS),
        required=True,
        help="preopen=08:30–09:00；preclose=13:25–13:30",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="完全離線 dry-run：略過登入與 recorder，產生 sample 後串到 dashboard",
    )
    parser.add_argument(
        "--dashboard-out",
        type=Path,
        default=BASE_DIR / "dashboard.html",
        help="dashboard 輸出路徑（預設專案根目錄 dashboard.html）",
    )
    return parser.parse_args(argv)


def parse_clock(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def target_session_date(session: str, now: datetime | None = None) -> date:
    """與 recorder 的顯式時窗規則一致：時窗已過就排到次日。"""
    current = now or datetime.now(TAIPEI)
    _start_text, end_text = SESSION_WINDOWS[session]
    target = current.date()
    if current.time() >= parse_clock(end_text):
        target += timedelta(days=1)
    return target


def child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def run_step(label: str, arguments: list[str]) -> None:
    print(f"\n[{label}] 開始", flush=True)
    completed = subprocess.run(
        [sys.executable, *arguments],
        cwd=BASE_DIR,
        env=child_environment(),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} 失敗（exit={completed.returncode}）")
    print(f"[{label}] 完成", flush=True)


def read_result(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get("stocks"), list):
        raise ValueError(f"結果格式不合法：{path}")
    return payload


def print_summary(
    result_path: Path,
    dashboard_path: Path,
    payload: dict[str, Any],
    *,
    sample: bool,
) -> None:
    stocks = payload["stocks"]
    counts = {
        key: sum(stock.get("status") == key for stock in stocks)
        for key in STATUS_LABELS
    }
    print("\n=== 試撮監控摘要 ===")
    print(f"模式：{'離線合成 dry-run' if sample else payload.get('session', 'unknown')}")
    print(
        "股票狀態："
        + "、".join(
            f"{label} {counts[key]} 檔" for key, label in STATUS_LABELS.items()
        )
    )
    print(
        f"監控/訂閱：{payload.get('universe_size', 0)}/"
        f"{payload.get('subscribed', 0)} 檔；"
        f"sub_limit={payload.get('sub_limit', 0)}"
    )
    print(f"結果 JSON：{result_path.resolve()}")
    print(f"離線 dashboard：{dashboard_path.resolve()}")


def main(argv: list[str] | None = None) -> int:
    configure_output()
    args = parse_args(argv)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    dashboard_path = args.dashboard_out.expanduser().resolve()

    try:
        if args.sample:
            result_path = DATA_DIR / "result_sample.json"
            run_step(
                "scanner sample",
                ["scanner.py", "--sample", "--out", str(result_path)],
            )
        else:
            target_date = target_session_date(args.session)
            date_token = target_date.strftime("%Y%m%d")
            start_text, end_text = SESSION_WINDOWS[args.session]
            auction_path = DATA_DIR / f"auction_{date_token}.jsonl"
            result_path = DATA_DIR / f"result_{date_token}.json"

            run_step(
                "recorder",
                [
                    "recorder.py",
                    "--start",
                    start_text,
                    "--end",
                    end_text,
                    "--out",
                    str(auction_path),
                ],
            )
            run_step(
                "scanner",
                [
                    "scanner.py",
                    "--in",
                    str(auction_path),
                    "--out",
                    str(result_path),
                    "--open-source",
                    "auto",
                ],
            )

        run_step(
            "dashboard",
            [
                "generate_dashboard.py",
                "--in",
                str(result_path),
                "--out",
                str(dashboard_path),
            ],
        )
        payload = read_result(result_path)
        print_summary(
            result_path,
            dashboard_path,
            payload,
            sample=args.sample,
        )
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"run_session FAILED：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("run_session 已由使用者中止", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
