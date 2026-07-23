#!/usr/bin/env python
"""Shioaji 歷史行情欄位與歷史五檔能力探針。

本程式只查詢 2330 今日歷史 ticks 與公開 API 介面；不啟用 CA、
不選帳號、不下單，也不建立即時行情訂閱。
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import io
import os
import re
import sys
import warnings
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# 必須在匯入 Shioaji 前降低套件日誌量。
os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""

import shioaji as sj


TAIPEI_TZ = timezone(timedelta(hours=8))
TARGET_CODE = "2330"
OUT_PATH = Path(__file__).resolve().parent / "log" / "hist-capability-out.txt"
REDACTIONS: list[str] = []

HISTORY_CANDIDATE_NAMES = (
    "history",
    "historical",
    "historical_ticks",
    "historical_bidask",
    "history_bidask",
    "orderbook",
    "order_book",
    "orderbooks",
    "market_depth",
    "depth",
    "bidask",
    "bidasks",
    "five_level",
    "five_levels",
    "auction",
    "auction_ticks",
    "simtrade",
)


def configure_stream(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="replace")


def load_dotenv_manually(path: Path) -> dict[str, str]:
    """安全解析簡單 KEY=VALUE；永不輸出值。"""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


@contextlib.contextmanager
def quiet_library_call() -> Iterable[None]:
    """吞掉第三方套件 stdout/stderr，避免登入資訊或雜訊外洩。"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        yield


def safe_error(exc: BaseException) -> str:
    return type(exc).__name__


def normalize_text(value: Any) -> str:
    return str(value or "").strip()


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, bytes, bytearray)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def remember_sensitive_identifiers(accounts: Any) -> None:
    """只記憶供輸出守衛掃描，不輸出、不序列化。"""
    for account in as_list(accounts):
        for field_name in ("person_id", "account_id", "username"):
            value = normalize_text(getattr(account, field_name, ""))
            if len(value) >= 6 and value not in REDACTIONS:
                REDACTIONS.append(value)


def get_stock_contract(api: sj.Shioaji, code: str) -> Any | None:
    contract = api.Contracts.Stocks.get(code)
    if contract is None:
        return None
    if normalize_text(getattr(contract, "code", "")) != code:
        return None
    return contract


def public_names(obj: Any) -> list[str]:
    return sorted(name for name in dir(obj) if not name.startswith("_"))


def get_data_field_names(obj: Any) -> list[str]:
    """從各種 namedtuple/dataclass/Pydantic 表示取得完整資料欄位名。"""
    names: set[str] = set()

    namedtuple_fields = getattr(obj, "_fields", ())
    if isinstance(namedtuple_fields, (tuple, list)):
        names.update(str(name) for name in namedtuple_fields)

    # Pydantic v2 要從 class 讀 model_fields，從 instance 讀會產生棄用警告。
    model_fields = getattr(type(obj), "model_fields", None)
    if isinstance(model_fields, Mapping):
        names.update(str(name) for name in model_fields)
    elif not names:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            legacy_fields = getattr(type(obj), "__fields__", None)
        if isinstance(legacy_fields, Mapping):
            names.update(str(name) for name in legacy_fields)

    if dataclasses.is_dataclass(obj):
        names.update(field.name for field in dataclasses.fields(obj))

    for dump_method_name in ("model_dump", "dict"):
        dump_method = getattr(obj, dump_method_name, None)
        if not callable(dump_method):
            continue
        try:
            dumped = dump_method()
        except Exception:
            continue
        if isinstance(dumped, Mapping):
            names.update(str(name) for name in dumped)

    object_dict = getattr(obj, "__dict__", None)
    if isinstance(object_dict, Mapping):
        names.update(
            str(name) for name in object_dict if not str(name).startswith("_")
        )

    # 若上述模型協定皆不存在，才由 dir() 補上非 callable 的公開資料屬性。
    if not names:
        for name in public_names(obj):
            try:
                value = getattr(obj, name)
            except Exception:
                continue
            if not callable(value):
                names.add(name)

    return sorted(names)


def qualified_type_name(value: Any) -> str:
    value_type = type(value)
    module = value_type.__module__
    if module in {"builtins", "__builtin__"}:
        return value_type.__qualname__
    return f"{module}.{value_type.__qualname__}"


def sequence_length(value: Any) -> int | None:
    if value is None or isinstance(value, (str, bytes, bytearray)):
        return None
    try:
        return len(value)
    except (TypeError, AttributeError):
        return None


def first_element(value: Any) -> Any | None:
    length = sequence_length(value)
    if length is None or length == 0:
        return None
    try:
        return value[0]
    except (KeyError, TypeError):
        try:
            return next(iter(value))
        except (StopIteration, TypeError):
            return None


def describe_price_field(ticks: Any, field_name: str, tick_count: int) -> str:
    if not hasattr(ticks, field_name):
        return f"{field_name}：欄位不存在"

    value = getattr(ticks, field_name)
    outer_length = sequence_length(value)
    sample = first_element(value)
    inner_length = sequence_length(sample)
    sample_type = "N/A（空陣列）" if sample is None else qualified_type_name(sample)

    if outer_length is None:
        classification = "scalar（單一值；不是五檔）"
    elif inner_length == 5:
        classification = "逐筆五檔 array"
    elif inner_length is not None:
        classification = f"逐筆巢狀 array（每筆 {inner_length} 個值；非標準五檔）"
    elif outer_length == tick_count:
        classification = "逐筆 array；每筆為單一最佳檔 scalar（不是五檔）"
    else:
        classification = "一維 array；元素為 scalar（不是五檔）"

    return (
        f"{field_name}：實際型別={qualified_type_name(value)}；"
        f"長度={outer_length if outer_length is not None else 'N/A'}；"
        f"首元素型別={sample_type}；"
        f"首元素長度={inner_length if inner_length is not None else 'N/A'}；"
        f"判定={classification}"
    )


def classify_public_candidate(owner_name: str, name: str) -> str:
    lowered = name.lower()
    if lowered in {"ticks", "kbars"}:
        return "既知歷史逐筆/分 K API（本題排除）"
    if lowered == "snapshots":
        return "即時快照，無歷史日期維度，非歷史五檔"
    if (
        lowered.startswith("set_on_")
        or lowered.startswith("on_")
        or lowered in {"subscribe", "unsubscribe", "connect", "disconnect"}
    ):
        return "即時串流/回呼介面，非歷史 API"
    return "可能的歷史五檔/orderbook/試撮候選"


def looks_like_discovered_candidate(name: str) -> bool:
    lowered = name.lower()
    keywords = (
        "histor",
        "orderbook",
        "order_book",
        "depth",
        "bidask",
        "auction",
        "simtrade",
        "five_level",
    )
    return lowered in {"ticks", "kbars", "snapshots"} or any(
        keyword in lowered for keyword in keywords
    )


def is_potential_history_method(owner_name: str, name: str) -> bool:
    return (
        classify_public_candidate(owner_name, name)
        == "可能的歷史五檔/orderbook/試撮候選"
    )


def safe_test_history_candidate(
    owner_name: str,
    owner: Any,
    method_name: str,
    contract: Any,
    query_date: date,
) -> str:
    """僅嘗試名稱明確指向唯讀行情的候選，不傳帳戶或交易參數。"""
    method = getattr(owner, method_name)
    if not callable(method):
        return "非 callable，無法作為查詢 API"

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return "無法解析安全簽章，未呼叫"

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    date_text = query_date.isoformat()
    known_values = {
        "contract": contract,
        "contracts": [contract],
        "code": TARGET_CODE,
        "symbol": TARGET_CODE,
        "date": date_text,
        "start": date_text,
        "end": date_text,
        "start_date": date_text,
        "end_date": date_text,
    }

    for parameter in signature.parameters.values():
        if parameter.kind in {
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        }:
            continue
        if parameter.default is not inspect.Parameter.empty:
            continue
        if parameter.name not in known_values:
            return f"簽章含未知必要參數 {parameter.name!r}，基於安全未呼叫"
        if parameter.kind == inspect.Parameter.POSITIONAL_ONLY:
            args.append(known_values[parameter.name])
        else:
            kwargs[parameter.name] = known_values[parameter.name]

    try:
        with quiet_library_call():
            result = method(*args, **kwargs)
        return f"安全實測呼叫成功；回傳型別={qualified_type_name(result)}"
    except Exception as exc:
        return f"已安全實測一次；呼叫結果={safe_error(exc)}"


def probe_ticks(
    api: sj.Shioaji, contract: Any, query_date: date
) -> tuple[bool, bool, int]:
    print("[1] 2330 今日 api.ticks 欄位能力")
    with quiet_library_call():
        ticks = api.ticks(contract, date=query_date.isoformat())

    field_names = get_data_field_names(ticks)
    lowered_names = {name.lower() for name in field_names}
    tick_count = sequence_length(getattr(ticks, "ts", None)) or 0
    simtrade_available = "simtrade" in lowered_names
    chg_type_available = "chg_type" in lowered_names

    print(f"查詢日期：{query_date.isoformat()}")
    print(f"回傳物件型別：{qualified_type_name(ticks)}")
    print(f"歷史 tick 筆數：{tick_count}")
    print(
        "回傳物件所有資料欄位名："
        + (", ".join(field_names) if field_names else "（無法辨識）")
    )
    print(f"simtrade 欄位：{'Y' if simtrade_available else 'N'}")
    print(f"chg_type 欄位：{'Y' if chg_type_available else 'N'}")
    print(describe_price_field(ticks, "bid_price", tick_count))
    print(describe_price_field(ticks, "ask_price", tick_count))
    return simtrade_available, chg_type_available, tick_count


def probe_public_interfaces(
    api: sj.Shioaji, contract: Any, query_date: date
) -> tuple[bool, list[str]]:
    print("\n[2] api / api.quote 公開介面與歷史五檔候選")
    owners = (("api", api), ("api.quote", api.quote))
    checked_candidates: list[str] = []
    potential_candidates: list[tuple[str, Any, str]] = []

    for owner_name, owner in owners:
        names = public_names(owner)
        print(f"{owner_name} 公開方法/屬性（dir）：{', '.join(names)}")
        exact_checks = []
        for candidate_name in HISTORY_CANDIDATE_NAMES:
            exists = candidate_name in names
            exact_checks.append(f"{candidate_name}={'Y' if exists else 'N'}")
            checked_candidates.append(f"{owner_name}.{candidate_name}")
        print(f"{owner_name} 候選名稱逐一檢查：{'; '.join(exact_checks)}")

        discovered = [name for name in names if looks_like_discovered_candidate(name)]
        print(
            f"{owner_name} 關鍵字掃描命中："
            + (", ".join(discovered) if discovered else "無")
        )
        for name in discovered:
            classification = classify_public_candidate(owner_name, name)
            print(f"  {owner_name}.{name}：{classification}")
            if is_potential_history_method(owner_name, name):
                potential_candidates.append((owner_name, owner, name))

    if not potential_candidates:
        print(
            "除 ticks/kbars 外的歷史五檔/orderbook/試撮 API：N；"
            "dir() 與候選名稱/關鍵字掃描均未找到。"
        )
        return False, checked_candidates

    successful_query = False
    seen: set[tuple[str, str]] = set()
    print("候選安全實測：")
    for owner_name, owner, method_name in potential_candidates:
        identity = (owner_name, method_name)
        if identity in seen:
            continue
        seen.add(identity)
        result = safe_test_history_candidate(
            owner_name, owner, method_name, contract, query_date
        )
        print(f"  {owner_name}.{method_name}：{result}")
        if result.startswith("安全實測呼叫成功"):
            successful_query = True

    print(
        "除 ticks/kbars 外的歷史五檔/orderbook/試撮 API："
        f"{'Y' if successful_query else 'N'}；"
        + (
            "至少一個候選安全實測成功。"
            if successful_query
            else "無候選可成功證明有此歷史能力。"
        )
    )
    return successful_query, checked_candidates


def print_conclusions(
    simtrade_available: bool,
    history_orderbook_available: bool,
    chg_type_available: bool,
    tick_count: int,
) -> None:
    print("\n[3] 三項明確結論")
    print(
        "A 歷史試撮 simtrade tick 可得？"
        f"{'Y' if simtrade_available else 'N'}｜佐證：2330 今日 api.ticks "
        f"回傳 {tick_count} 筆，"
        f"{'欄位名含 simtrade' if simtrade_available else '完整欄位名不含 simtrade'}。"
    )
    print(
        "B 歷史五檔 orderbook 可得？"
        f"{'Y' if history_orderbook_available else 'N'}｜佐證："
        + (
            "除 ticks/kbars 外至少一個歷史五檔候選查詢安全實測成功。"
            if history_orderbook_available
            else "dir(api) 與 dir(api.quote) 的公開介面、候選名稱及關鍵字掃描"
            "均未證明存在歷史五檔；ticks 的 bid/ask 僅為逐筆單一最佳檔。"
        )
    )
    print(
        "C 歷史 chg_type/漲停旗標可得？"
        f"{'Y' if chg_type_available else 'N'}｜佐證：2330 今日 api.ticks "
        f"{'欄位名含 chg_type' if chg_type_available else '完整欄位名不含 chg_type'}。"
    )


def cleanup(api: sj.Shioaji | None, subscriptions: list[tuple[Any, dict[str, Any]]]) -> bool:
    """即使未建立訂閱也走完整退訂容器與 logout。"""
    cleanup_ok = True
    if api is None:
        return cleanup_ok
    for contract, unsubscribe_kwargs in reversed(subscriptions):
        try:
            with quiet_library_call():
                api.quote.unsubscribe(contract, **unsubscribe_kwargs)
        except Exception:
            cleanup_ok = False
    subscriptions.clear()
    try:
        with quiet_library_call():
            api.logout()
    except Exception:
        cleanup_ok = False
    return cleanup_ok


def main() -> int:
    api: sj.Shioaji | None = None
    subscriptions: list[tuple[Any, dict[str, Any]]] = []
    exit_code = 1

    try:
        env_path = Path(__file__).resolve().with_name(".env")
        env_values = load_dotenv_manually(env_path)
        api_key = env_values.get("SHIOAJI_API_KEY", "").strip()
        secret_key = env_values.get("SHIOAJI_SECRET_KEY", "").strip()
        if not api_key or not secret_key:
            raise RuntimeError("missing credentials")
        REDACTIONS.extend((api_key, secret_key))

        api = sj.Shioaji()
        with quiet_library_call():
            accounts = api.login(
                api_key=api_key,
                secret_key=secret_key,
                fetch_contract=True,
                contracts_timeout=30_000,
                subscribe_trade=False,
            )
        remember_sensitive_identifiers(accounts)
        del accounts
        print("login OK（靜音；未啟 CA、未選帳號、未下單）")

        contract = get_stock_contract(api, TARGET_CODE)
        if contract is None:
            raise RuntimeError("target contract unavailable")

        query_date = datetime.now(TAIPEI_TZ).date()
        simtrade_available, chg_type_available, tick_count = probe_ticks(
            api, contract, query_date
        )
        history_orderbook_available, checked_candidates = probe_public_interfaces(
            api, contract, query_date
        )
        print(
            "已檢查候選識別字數："
            f"{len(checked_candidates)}（api 與 api.quote 各 "
            f"{len(HISTORY_CANDIDATE_NAMES)} 個固定候選，另含 dir 關鍵字掃描）"
        )
        print_conclusions(
            simtrade_available,
            history_orderbook_available,
            chg_type_available,
            tick_count,
        )
        exit_code = 0
    except Exception as exc:
        print(f"探針失敗（{safe_error(exc)}）")
        exit_code = 1
    finally:
        cleanup_ok = cleanup(api, subscriptions)
        print(
            "清理："
            f"退訂 {len(subscriptions)} 項；logout {'OK' if cleanup_ok else 'FAILED'}"
        )
        if not cleanup_ok:
            exit_code = 1

    return exit_code


def has_sensitive_output(report: str) -> bool:
    contains_known_value = any(
        sensitive and sensitive in report for sensitive in REDACTIONS
    )
    contains_labeled_identifier = bool(
        re.search(
            r"(?i)\b(api[_ -]?key|secret[_ -]?key|person[_ -]?id|"
            r"account(?:[_ -]?id)?|username)\s*[:=]",
            report,
        )
    )
    return contains_known_value or contains_labeled_identifier


def run_with_output_guard() -> int:
    """僅在最終報告通過敏感值守衛後，才用 UTF-8 寫檔與顯示。"""
    configure_stream(sys.stdout)
    original_stdout = sys.stdout
    captured = io.StringIO()
    captured_stderr = io.StringIO()
    try:
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(
            captured_stderr
        ):
            exit_code = main()
    except Exception as exc:
        with contextlib.redirect_stdout(captured):
            print(f"探針未預期失敗（{safe_error(exc)}）")
        exit_code = 1

    report = captured.getvalue()
    hidden_stderr = captured_stderr.getvalue()
    if hidden_stderr:
        report += (
            "診斷：第三方 stderr 已抑制"
            f"（{len(hidden_stderr.splitlines())} 行，未寫入報告）。\n"
        )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if has_sensitive_output(report + hidden_stderr):
        safe_report = "SECURITY FAIL：輸出含敏感值或帶標籤識別值，完整報告已抑制。\n"
        with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(safe_report)
        original_stdout.write(safe_report)
        original_stdout.flush()
        return 3

    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as output_file:
        output_file.write(report)
    original_stdout.write(report)
    original_stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_with_output_guard())
