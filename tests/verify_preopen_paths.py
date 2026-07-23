#!/usr/bin/env python3
"""離線驗證 scanner.py 的 preopen 未覆蓋路徑與實收資料漏抓情形。

本腳本只讀取既有資料，透過 subprocess 黑箱執行 scanner.py，並將完整
驗證結果寫入 log/preopen-verify-out.txt。全程不匯入券商套件、不登入。
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "scanner.py"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
LOG_PATH = ROOT / "log" / "preopen-verify-out.txt"
ACTUAL_INPUT = ROOT / "data" / "auction_20260723.jsonl"
DOCUMENTED_LOCKED = {"2880", "3081", "8039"}


@dataclass(frozen=True)
class Scenario:
    label: str
    description: str
    filename: str
    code: str
    expected_status: str
    expected_open_gap_pct: float | None
    expected_bid0_dropped: bool
    logic_reference: str


SCENARIOS = (
    Scenario(
        label="A",
        description="假試撮：盤前鎖漲停，開盤價低於漲停 5%",
        filename="preopen_a_fake_gap.jsonl",
        code="9001",
        expected_status="suspected_fake",
        expected_open_gap_pct=-5.0,
        expected_bid0_dropped=False,
        logic_reference="scanner.py:514-540、561-566",
    ),
    Scenario(
        label="B",
        description="守住：盤前鎖漲停，開盤價等於漲停",
        filename="preopen_b_held.jsonl",
        code="9002",
        expected_status="locked_held",
        expected_open_gap_pct=0.0,
        expected_bid0_dropped=False,
        logic_reference="scanner.py:514-540、561-568",
    ),
    Scenario(
        label="C",
        description="撤單：盤前曾有漲停大單，末筆與 snapshot 買一量再明顯縮減",
        filename="preopen_c_bid0_drop.jsonl",
        code="9003",
        expected_status="suspected_fake",
        expected_open_gap_pct=0.0,
        expected_bid0_dropped=True,
        logic_reference="scanner.py:378-428、542-564",
    ),
    Scenario(
        label="D",
        description="未鎖：所有試撮買一價均低於漲停",
        filename="preopen_d_unlocked.jsonl",
        code="9004",
        expected_status="none",
        expected_open_gap_pct=-5.0,
        expected_bid0_dropped=False,
        logic_reference="scanner.py:479-500、561-573",
    ),
    Scenario(
        label="E",
        description="守住：開盤 open=0 視為未知，改採 snapshot 漲停報價",
        filename="preopen_e_open_zero.jsonl",
        code="9005",
        expected_status="locked_held",
        expected_open_gap_pct=None,
        expected_bid0_dropped=False,
        logic_reference="scanner.py:74-86、339-380、502-575",
    ),
)


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def run_scanner(input_path: Path, output_path: Path) -> tuple[dict[str, Any], str]:
    command = [
        sys.executable,
        str(SCANNER),
        "--in",
        str(input_path),
        "--out",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    cli_output = "\n".join(
        part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"scanner exit={completed.returncode}; output={cli_output or '(empty)'}"
        )
    if not output_path.is_file():
        raise RuntimeError(f"scanner 未產生輸出：{output_path}")
    result = load_json(output_path)
    if not isinstance(result, dict):
        raise RuntimeError("scanner 輸出不是 JSON object")
    return result, cli_output


def stock_by_code(result: dict[str, Any], code: str) -> dict[str, Any]:
    stocks = result.get("stocks")
    if not isinstance(stocks, list):
        raise RuntimeError("scanner 輸出缺少 stocks list")
    matches = [
        stock
        for stock in stocks
        if isinstance(stock, dict) and str(stock.get("code")) == code
    ]
    if len(matches) != 1:
        raise RuntimeError(f"預期代碼 {code} 恰有一筆結果，實際 {len(matches)} 筆")
    return matches[0]


def gap_matches(actual: Any, expected: float | None) -> bool:
    if expected is None:
        return actual is None
    return isinstance(actual, (int, float)) and math.isclose(
        float(actual),
        expected,
        rel_tol=0.0,
        abs_tol=1e-4,
    )


def format_gap(value: float | None) -> str:
    return "None" if value is None else f"{value:.4f}"


def format_codes(codes: set[str]) -> str:
    return "、".join(sorted(codes)) if codes else "[]"


def write_edge_fixture(
    temp_dir: Path,
    name: str,
    code: str,
    events: list[dict[str, Any]],
    *,
    limit_up: float,
) -> Path:
    input_path = temp_dir / f"{name}.jsonl"
    metadata_path = input_path.with_suffix(".meta.json")
    input_path.write_text(
        "".join(
            json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            for event in events
        ),
        encoding="utf-8",
    )
    metadata = {
        "date": "2026-07-24",
        "session": "preopen",
        "window": {
            "start": "2026-07-24T08:30:00",
            "end": "2026-07-24T09:00:00",
        },
        "universe_size": 1,
        "subscribed": 1,
        "subscribed_codes": [code],
        "stocks": [
            {
                "code": code,
                "name": name,
                "reference": 90.0,
                "limit_up": limit_up,
            }
        ],
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return input_path


def bidask_event(
    code: str,
    timestamp: str,
    bid0: float,
    volume: int,
) -> dict[str, Any]:
    return {
        "code": code,
        "name": code,
        "ts": timestamp,
        "kind": "bidask",
        "simtrade": True,
        "bid_price": [bid0],
        "bid_volume": [volume],
        "ask_price": [None],
        "ask_volume": [0],
    }


def snapshot_event(
    code: str,
    timestamp: str,
    open_price: float,
    bid0: float,
    volume: int,
) -> dict[str, Any]:
    return {
        "code": code,
        "name": code,
        "ts": timestamp,
        "kind": "snapshot",
        "simtrade": False,
        "open_price": open_price,
        "bid_price": [bid0],
        "bid_volume": [volume],
        "ask_price": [None],
        "ask_volume": [0],
    }


def validate_red_team_boundaries(temp_dir: Path, lines: list[str]) -> list[bool]:
    """以額外暫存最小案例確認已修正邊界，任一失敗即使驗收失敗。"""
    lines.extend(["", "[獨立 red-team 修正後邊界驗證]"])
    results: list[bool] = []

    cases = [
        {
            "name": "edge_zero_volume_shortcut",
            "code": "E001",
            "events": [
                bidask_event(
                    "E001",
                    "2026-07-24T08:59:50",
                    100.0,
                    1,
                ),
                snapshot_event(
                    "E001",
                    "2026-07-24T09:00:03",
                    100.0,
                    100.0,
                    0,
                ),
            ],
            "title": "snapshot 買一量為 0 仍受最低量門檻",
            "expected": "bid0_dropped=False（盤前末筆 1 < min_preopen 1000，減量 1 < 500）",
            "is_correct": lambda stock: (
                stock.get("bid0_dropped") is False
                and stock.get("status") == "locked_held"
            ),
            "actual": lambda stock: (
                f"bid0_dropped={stock.get('bid0_dropped')}, "
                f"status={stock.get('status')}"
            ),
            "reference": "scanner.py:411-420",
        },
        {
            "name": "edge_early_snapshot",
            "code": "E002",
            "events": [
                bidask_event(
                    "E002",
                    "2026-07-24T08:59:50",
                    100.0,
                    5000,
                ),
                snapshot_event(
                    "E002",
                    "2026-07-24T08:59:59",
                    95.0,
                    95.0,
                    5000,
                ),
            ],
            "title": "08:59 snapshot 不得作為開盤證據",
            "expected": "08:59:59 snapshot 不應成為開盤證據；status 應停在 touched",
            "is_correct": lambda stock: (
                stock.get("open_gap_pct") is None
                and stock.get("status") == "touched"
                and stock.get("locked_limit_up") is True
            ),
            "actual": lambda stock: (
                f"open_gap_pct={stock.get('open_gap_pct')}, "
                f"status={stock.get('status')}"
            ),
            "reference": "scanner.py:349-355、516-550",
        },
        {
            "name": "edge_postopen_lock",
            "code": "E003",
            "events": [
                bidask_event(
                    "E003",
                    "2026-07-24T08:59:50",
                    99.0,
                    5000,
                ),
                snapshot_event(
                    "E003",
                    "2026-07-24T09:00:03",
                    100.0,
                    100.0,
                    5000,
                ),
                bidask_event(
                    "E003",
                    "2026-07-24T09:01:00",
                    100.0,
                    5000,
                ),
            ],
            "title": "09:01 simtrade 不得算盤前 locked",
            "expected": "唯一 09:01 simtrade 命中不應算盤前 locked；status 應為 none",
            "is_correct": lambda stock: (
                stock.get("locked_limit_up") is False
                and stock.get("status") == "none"
            ),
            "actual": lambda stock: (
                f"locked_limit_up={stock.get('locked_limit_up')}, "
                f"status={stock.get('status')}"
            ),
            "reference": "scanner.py:461-500",
        },
        {
            "name": "edge_rounded_gap",
            "code": "E004",
            "events": [
                bidask_event(
                    "E004",
                    "2026-07-24T08:59:50",
                    100.0,
                    5000,
                ),
                snapshot_event(
                    "E004",
                    "2026-07-24T09:00:03",
                    99.9999,
                    100.0,
                    5000,
                ),
            ],
            "title": "極小負 open gap 落在容差內",
            "expected": "open_gap_pct=-0.0001 在小容差內，status 應為 locked_held",
            "is_correct": lambda stock: (
                stock.get("open_gap_pct") == -0.0001
                and stock.get("status") == "locked_held"
            ),
            "actual": lambda stock: (
                f"open_gap_pct={stock.get('open_gap_pct')}, "
                f"status={stock.get('status')}"
            ),
            "reference": "scanner.py:537-566",
        },
        {
            "name": "edge_zero_limit_up",
            "code": "E005",
            "events": [
                bidask_event(
                    "E005",
                    "2026-07-24T08:59:50",
                    0.0,
                    5000,
                ),
                snapshot_event(
                    "E005",
                    "2026-07-24T09:00:03",
                    100.0,
                    100.0,
                    5000,
                ),
            ],
            "title": "limit_up=0 視為未知",
            "expected": "limit_up=None、locked_limit_up=False、status=none",
            "is_correct": lambda stock: (
                stock.get("limit_up") is None
                and stock.get("locked_limit_up") is False
                and stock.get("status") == "none"
            ),
            "actual": lambda stock: (
                f"limit_up={stock.get('limit_up')}, "
                f"locked_limit_up={stock.get('locked_limit_up')}, "
                f"status={stock.get('status')}"
            ),
            "reference": "scanner.py:301-328",
            "limit_up": 0.0,
        },
    ]

    for index, case in enumerate(cases, start=1):
        input_path = write_edge_fixture(
            temp_dir,
            case["name"],
            case["code"],
            case["events"],
            limit_up=case.get("limit_up", 100.0),
        )
        output_path = temp_dir / f"{case['name']}.result.json"
        result, _cli_output = run_scanner(input_path, output_path)
        stock = stock_by_code(result, case["code"])
        passed = case["is_correct"](stock)
        results.append(passed)
        lines.extend(
            [
                f"{index}. {case['title']}："
                f"{'PASS' if passed else 'FAIL'}",
                f"  期望語意：{case['expected']}",
                f"  實際：{case['actual'](stock)}",
                f"  邏輯定位：{case['reference']}",
                f"  重現：{sys.executable} tests/verify_preopen_paths.py",
            ]
        )

    lines.extend(
        [
            "規格備註：盤前 peak→末筆近零仍依既定規格比較盤前末筆與 snapshot；",
            "  scanner 只比較盤前最後一筆與 snapshot，不檢查盤前 peak；"
            "本次 C 以末筆 2000→snapshot 100 明確命中現行門檻。",
        ]
    )
    return results


def validate_recorder_boundaries(lines: list[str]) -> bool:
    """不連券商，直接驗證 recorder 的純函式邊界。"""
    sys.path.insert(0, str(ROOT))
    try:
        import recorder
    finally:
        sys.path.pop(0)

    observed_at = datetime(2026, 7, 24, 9, 0, 5)
    snapshots = [
        SimpleNamespace(code="R001", open_price=0.0),
        SimpleNamespace(code="R002", open_price=-1.0),
        SimpleNamespace(code="R003", open_price=None, open=0.0),
        SimpleNamespace(code="R004", open_price=100.0),
    ]
    normalized = [
        recorder.snapshot_record(snapshot, snapshot.code, {}, observed_at)
        for snapshot in snapshots
    ]
    open_prices = [record["open_price"] for record in normalized]
    expected_ts = observed_at.isoformat(timespec="microseconds")
    preopen_end = datetime(2026, 7, 24, 9, 0)
    checks = {
        "open_nonpositive_to_none": open_prices[:3] == [None, None, None],
        "open_positive_kept": open_prices[3] == 100.0,
        "snapshot_ts_is_observed_at": all(
            record["ts"] == expected_ts for record in normalized
        ),
        "preopen_capture_extended": (
            recorder.capture_end_at(preopen_end, "preopen")
            == preopen_end + timedelta(seconds=5)
        ),
        "preclose_capture_unchanged": (
            recorder.capture_end_at(preopen_end, "preclose") == preopen_end
        ),
    }
    passed = all(checks.values())
    lines.extend(
        [
            "",
            "[recorder 純函式邊界驗證]",
            f"結果：{'PASS' if passed else 'FAIL'}",
            "  "
            + ", ".join(
                f"{name}={'PASS' if ok else 'FAIL'}"
                for name, ok in checks.items()
            ),
        ]
    )
    return passed


def independent_bidask_limit_hits(
    input_path: Path,
) -> tuple[set[str], list[str]]:
    """不使用 scanner 內部函式，依使用者指定的 exact equality 獨立掃描。"""
    metadata_path = input_path.with_suffix(".meta.json")
    metadata = load_json(metadata_path)
    stocks = metadata.get("stocks") if isinstance(metadata, dict) else None
    if not isinstance(stocks, list):
        raise RuntimeError(f"{metadata_path} 缺少 stocks list")

    limit_ups: dict[str, float] = {}
    for stock in stocks:
        if not isinstance(stock, dict):
            continue
        code = str(stock.get("code") or "").strip()
        limit_up = stock.get("limit_up")
        if code and isinstance(limit_up, (int, float)) and not isinstance(limit_up, bool):
            limit_ups[code] = float(limit_up)

    hits: set[str] = set()
    missing_limit_codes: set[str] = set()
    with input_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            event = json.loads(line)
            if (
                not isinstance(event, dict)
                or event.get("kind") != "bidask"
                or event.get("simtrade") is not True
            ):
                continue
            code = str(event.get("code") or "").strip()
            bid_prices = event.get("bid_price")
            if not isinstance(bid_prices, list) or not bid_prices:
                continue
            bid0 = bid_prices[0]
            if code not in limit_ups:
                missing_limit_codes.add(code)
                continue
            if isinstance(bid0, (int, float)) and not isinstance(bid0, bool):
                if float(bid0) == limit_ups[code]:
                    hits.add(code)

    return hits, sorted(missing_limit_codes)


def validate_scenario(
    scenario: Scenario,
    temp_dir: Path,
    lines: list[str],
) -> bool:
    fixture = FIXTURE_DIR / scenario.filename
    metadata = fixture.with_suffix(".meta.json")
    output = temp_dir / f"result_{scenario.label.lower()}.json"
    try:
        if not fixture.is_file() or not metadata.is_file():
            raise RuntimeError(f"fixture 不完整：{fixture} / {metadata}")
        result, cli_output = run_scanner(fixture, output)
        stock = stock_by_code(result, scenario.code)
        checks = {
            "status": stock.get("status") == scenario.expected_status,
            "open_gap_pct": gap_matches(
                stock.get("open_gap_pct"),
                scenario.expected_open_gap_pct,
            ),
            "bid0_dropped": (
                stock.get("bid0_dropped") is scenario.expected_bid0_dropped
            ),
        }
        passed = all(checks.values())
        lines.extend(
            [
                f"{scenario.label}. {scenario.description}："
                f"{'PASS' if passed else 'FAIL'}",
                f"  fixture={fixture.relative_to(ROOT)}",
                "  期望："
                f"status={scenario.expected_status}, "
                f"open_gap_pct={format_gap(scenario.expected_open_gap_pct)}, "
                f"bid0_dropped={scenario.expected_bid0_dropped}",
                "  實際："
                f"status={stock.get('status')}, "
                f"open_gap_pct={stock.get('open_gap_pct')}, "
                f"bid0_dropped={stock.get('bid0_dropped')}, "
                f"locked_limit_up={stock.get('locked_limit_up')}",
                "  欄位比對："
                + ", ".join(
                    f"{name}={'PASS' if ok else 'FAIL'}"
                    for name, ok in checks.items()
                ),
                f"  scanner CLI：{cli_output or '(無輸出)'}",
            ]
        )
        if not passed:
            lines.extend(
                [
                    f"  邏輯定位：{scenario.logic_reference}",
                    "  重現："
                    f"{sys.executable} scanner.py --in "
                    f"{fixture.relative_to(ROOT)} --out <result.json>",
                ]
            )
        return passed
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        lines.extend(
            [
                f"{scenario.label}. {scenario.description}：FAIL",
                f"  錯誤：{exc}",
                f"  邏輯定位：{scenario.logic_reference}",
                "  重現："
                f"{sys.executable} scanner.py --in "
                f"{fixture.relative_to(ROOT)} --out <result.json>",
            ]
        )
        return False


def validate_actual_data(temp_dir: Path, lines: list[str]) -> bool:
    lines.extend(["", "[獨立 false-negative 掃描]"])
    if not ACTUAL_INPUT.is_file():
        lines.extend(
            [
                "結果：FAIL",
                f"缺少真實資料：{ACTUAL_INPUT.relative_to(ROOT)}",
            ]
        )
        return False

    try:
        independent_hits, missing_limit_codes = independent_bidask_limit_hits(
            ACTUAL_INPUT
        )
        actual_output = temp_dir / "result_actual_20260723.json"
        scanner_result, cli_output = run_scanner(ACTUAL_INPUT, actual_output)
        scanner_locked = {
            str(stock.get("code"))
            for stock in scanner_result.get("stocks", [])
            if isinstance(stock, dict) and stock.get("locked_limit_up") is True
        }
        scanner_by_code = {
            str(stock.get("code")): stock
            for stock in scanner_result.get("stocks", [])
            if isinstance(stock, dict)
        }
        expected_statuses = {
            "2880": "suspected_fake",
            "3081": "locked_held",
            "8039": "locked_held",
        }
        status_mismatches = {
            code: scanner_by_code.get(code, {}).get("status")
            for code, expected_status in expected_statuses.items()
            if scanner_by_code.get(code, {}).get("status") != expected_status
        }
        missed = independent_hits - scanner_locked
        extra = scanner_locked - independent_hits
        documented_missing = DOCUMENTED_LOCKED - scanner_locked
        documented_extra = scanner_locked - DOCUMENTED_LOCKED
        passed = (
            not missed
            and not extra
            and not documented_missing
            and not documented_extra
            and not missing_limit_codes
            and not status_mismatches
        )
        lines.extend(
            [
                f"資料：{ACTUAL_INPUT.relative_to(ROOT)}",
                "條件：kind=bidask 且 simtrade=true 且 "
                "bid_price[0] == meta per-stock limit_up（exact equality）",
                f"獨立命中 ({len(independent_hits)})："
                f"{format_codes(independent_hits)}",
                f"scanner locked_limit_up ({len(scanner_locked)})："
                f"{format_codes(scanner_locked)}",
                f"既知基準 ({len(DOCUMENTED_LOCKED)})："
                f"{format_codes(DOCUMENTED_LOCKED)}",
                f"漏抓 ({len(missed)})：{format_codes(missed)}",
                f"多抓 ({len(extra)})：{format_codes(extra)}",
                "相對既知基準缺少 "
                f"({len(documented_missing)})：{format_codes(documented_missing)}",
                "相對既知基準新增 "
                f"({len(documented_extra)})：{format_codes(documented_extra)}",
                "缺少 meta limit_up 的 simtrade bidask 代碼 "
                f"({len(missing_limit_codes)})："
                f"{'、'.join(missing_limit_codes) if missing_limit_codes else '[]'}",
                "指定狀態：2880=suspected_fake（snapshot 分類）、"
                "3081/8039=locked_held；"
                f"不符={status_mismatches or '{}'}",
                f"scanner CLI：{cli_output or '(無輸出)'}",
                f"結果：{'PASS' if passed else 'FAIL'}",
            ]
        )
        if not passed:
            lines.extend(
                [
                    "邏輯定位：scanner.py:309-325、479-500、625-650",
                    f"重現：{sys.executable} tests/verify_preopen_paths.py",
                ]
            )
        return passed
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        lines.extend(
            [
                "結果：FAIL",
                f"錯誤：{exc}",
                "邏輯定位：scanner.py:309-325、479-500、625-650",
                f"重現：{sys.executable} tests/verify_preopen_paths.py",
            ]
        )
        return False


def main() -> int:
    lines = [
        "盤前試撮 preopen 路徑離線驗證",
        f"執行時間：{datetime.now().astimezone().isoformat(timespec='seconds')}",
        "資料性質：合成 fixture + 本地既有 JSONL；不登入、不連線、不輸出金鑰",
        "",
        "[合成情境 A/B/C/D/E]",
    ]
    scenario_results: list[bool] = []
    with tempfile.TemporaryDirectory(prefix="preopen-verify-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        for scenario in SCENARIOS:
            scenario_results.append(validate_scenario(scenario, temp_dir, lines))
        actual_passed = validate_actual_data(temp_dir, lines)
        boundary_results = validate_red_team_boundaries(temp_dir, lines)
        recorder_passed = validate_recorder_boundaries(lines)

    failed_scenarios = [
        scenario.label
        for scenario, passed in zip(SCENARIOS, scenario_results)
        if not passed
    ]
    all_passed = (
        all(scenario_results)
        and actual_passed
        and all(boundary_results)
        and recorder_passed
    )
    lines.extend(
        [
            "",
            "[邏輯問題清單]",
            (
                "已知邊界修正均通過。"
                if all(boundary_results)
                else "仍有邊界修正失敗；詳見上方 red-team 最小案例。"
            ),
            (
                "核心驗收無 FAIL。"
                if all_passed
                else "核心驗收有 FAIL：請見上述邏輯定位與重現命令；"
                f"失敗情境={','.join(failed_scenarios) or '無'}，"
                f"false-negative 掃描={'PASS' if actual_passed else 'FAIL'}，"
                f"recorder={'PASS' if recorder_passed else 'FAIL'}"
            ),
            "",
            "[總結]",
            f"A/B/C/D/E：{sum(scenario_results)} PASS / "
            f"{len(scenario_results) - sum(scenario_results)} FAIL",
            f"false-negative：{'PASS' if actual_passed else 'FAIL'}",
            f"邊界回歸：{sum(boundary_results)} PASS / "
            f"{len(boundary_results) - sum(boundary_results)} FAIL",
            f"recorder：{'PASS' if recorder_passed else 'FAIL'}",
            f"核心驗收：{'PASS' if all_passed else 'FAIL'}",
        ]
    )

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n完整輸出：{LOG_PATH}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
