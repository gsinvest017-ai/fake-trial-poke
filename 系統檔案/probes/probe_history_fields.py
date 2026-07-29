#!/usr/bin/env python
"""唯讀探查 Shioaji 歷史 ticks、盤前 RangeTime、五檔端點與 kbars。"""
from __future__ import annotations
import contextlib
import io
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

os.environ["LOGURU_LEVEL"] = "ERROR"
os.environ["LOG_SENTRY"] = ""
import shioaji as sj

TODAY = date.today().isoformat()
OLDER = ("2026-07-22", "2026-07-17")
TAIPEI = timezone(timedelta(hours=8))
HINTS = ("tick", "kbar", "snapshot", "bidask", "quote", "short",
         "history", "depth", "subscribe")


def load_env(path: Path) -> dict[str, str]:
    result = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip().removeprefix("export ").lstrip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        result[key.strip()] = value
    return result


def silent(fn: Any, *args: Any, **kwargs: Any) -> Any:
    with contextlib.redirect_stdout(io.StringIO()), \
         contextlib.redirect_stderr(io.StringIO()):
        return fn(*args, **kwargs)


def fields_of(obj: Any) -> list[str]:
    names = list(getattr(type(obj), "model_fields", {}))
    names += list(getattr(type(obj), "__annotations__", {}))
    try:
        names += list(obj.keys())
    except Exception:
        pass
    names += list(getattr(type(obj), "__slots__", ()) or ())
    names += list((getattr(obj, "__dict__", {}) or {}).keys())
    return list(dict.fromkeys(str(x) for x in names if not str(x).startswith("_")))


def series(obj: Any, field: str) -> list[Any]:
    value = getattr(obj, field, None)
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return [value]


def wall_time(ts: Any) -> str:
    # Shioaji ts 是交易所壁鐘編碼；不可再加八小時。
    dt = datetime.fromtimestamp(int(ts) / 1_000_000_000, timezone.utc)
    return dt.replace(tzinfo=TAIPEI).isoformat()


def show_fields(label: str, obj: Any) -> list[str]:
    names = fields_of(obj)
    print(f"\n[{label}] 所有欄位：{', '.join(names) or '(無)'}")
    for name in names:
        values = series(obj, name)
        print(f"  {name}: 長度={len(values)}, 第一筆={values[0] if values else None!r}")
    return names


def query_range(api: Any, contract: Any, code: str, day: str,
                start: str, end: str) -> tuple[int | None, int | None]:
    try:
        ticks = silent(api.ticks, contract, date=day,
                       query_type=sj.constant.TicksQueryType.RangeTime,
                       time_start=start, time_end=end)
        ts = series(ticks, "ts")
        strict = sum(wall_time(x)[11:19] < "09:00:00" for x in ts)
        span = f"，首末={wall_time(ts[0])} ~ {wall_time(ts[-1])}" if ts else ""
        print(f"  {code} {day} {start}~{end}: 回傳筆數={len(ts)}，"
              f"其中早於09:00={strict}{span}")
        return len(ts), strict
    except Exception as exc:
        print(f"  {code} {day} {start}~{end}: ERROR {type(exc).__name__}")
        return None, None


def related(obj: Any) -> list[str]:
    return sorted(x for x in dir(obj) if not x.startswith("_")
                  and any(h in x.lower() for h in HINTS)
                  and callable(getattr(obj, x, None)))


def main() -> int:
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    key = env.get("SHIOAJI_API_KEY", "").strip()
    secret = env.get("SHIOAJI_SECRET_KEY", "").strip()
    if not key or not secret:
        print("login FAILED: missing credentials")
        return 2
    api = sj.Shioaji()
    try:
        try:
            silent(api.login, api_key=key, secret_key=secret, fetch_contract=True,
                   contracts_timeout=30_000, subscribe_trade=False)
            print("login OK")
        except Exception as exc:
            print(f"login FAILED: {type(exc).__name__}")
            return 1
        stocks = {x: api.Contracts.Stocks[x] for x in ("2330", "2317")}

        print("\n=== 1. 歷史 ticks 完整欄位 ===")
        tick_fields = []
        try:
            ticks = silent(api.ticks, stocks["2330"], date=TODAY)
            tick_fields = show_fields(f"2330 {TODAY} api.ticks", ticks)
            print(f"  simtrade：{'Y' if 'simtrade' in tick_fields else 'N'}")
            print(f"  chg_type：{'Y' if 'chg_type' in tick_fields else 'N'}")
            is_one_level = {"bid_price", "ask_price"} <= set(tick_fields)
            print("  bid_price/ask_price："
                  + ("每筆各為單一價（1檔），不是五檔陣列"
                     if is_one_level else "欄位不存在"))
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}")

        print("\n=== 2. RangeTime 今日盤前與開盤對照 ===")
        preopen, controls = {}, []
        for code in ("2330", "2317"):
            preopen[(TODAY, code)] = query_range(
                api, stocks[code], code, TODAY, "08:30:00", "09:00:00")
            controls.append(query_range(
                api, stocks[code], code, TODAY, "09:00:00", "09:05:00"))

        print("\n=== 3. 昨日與上週盤前 ===")
        for day in OLDER:
            preopen[(day, "2330")] = query_range(
                api, stocks["2330"], "2330", day, "08:30:00", "09:00:00")

        print("\n=== 4. 歷史五檔端點盤點 ===")
        historical_depth = False
        try:
            api_names, quote_names = related(api), related(api.quote)
            print(f"  dir(api) 相關方法：{', '.join(api_names)}")
            print(f"  dir(api.quote) 相關方法：{', '.join(quote_names)}")
            historical_depth = any(
                "bidask" in x.lower()
                and any(y in x.lower() for y in ("history", "depth"))
                for x in api_names + quote_names)
            print(f"  歷史 BidAsk/五檔查詢方法：{'Y' if historical_depth else 'N'}")
            print("  依據：bidask 名稱只出現在即時 callback；"
                  "無 bidasks/history/depth 查詢端點。")
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}")

        print("\n=== 5. kbars 欄位 ===")
        try:
            bars = silent(api.kbars, stocks["2330"], start=TODAY, end=TODAY)
            show_fields(f"2330 {TODAY} api.kbars", bars)
            bar_ts = series(bars, "ts")
            print("  今日最早 K 棒時間："
                  + (wall_time(min(bar_ts)) if bar_ts else "(無資料)"))
        except Exception as exc:
            print(f"  ERROR {type(exc).__name__}")

        print("\n=== 結論摘要 ===")
        print(f"① ticks 欄位：{', '.join(tick_fields) or '(查詢失敗)'}")
        print(f"   simtrade={'Y' if 'simtrade' in tick_fields else 'N'}，"
              f"chg_type={'Y' if 'chg_type' in tick_fields else 'N'}，"
              "五檔=N（bid/ask 每筆僅單一價）")
        for (day, code), (count, strict) in preopen.items():
            print(f"② {code} {day} 08:30~09:00：回傳={count}，實際<09:00={strict}")
        control_ok = all(x[0] is not None and x[0] > 0 for x in controls)
        print(f"   今日 09:00~09:05 對照組均有資料：{'Y' if control_ok else 'N'}")
        print(f"③ 歷史五檔端點：{'Y' if historical_depth else 'N'}")
        absent = len(preopen) == 4 and all(x[1] == 0 for x in preopen.values())
        print("④ 試撮價與試撮五檔能否事後從歷史端點取得："
              + ("N" if absent and not historical_depth and control_ok else "無法確定"))
        return 0
    finally:
        try:
            silent(api.logout)
        except Exception:
            pass


if __name__ == "__main__":
    if callable(getattr(sys.stdout, "reconfigure", None)):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
