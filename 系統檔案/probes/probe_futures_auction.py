#!/usr/bin/env python
"""Check whether historical futures ticks retain the pre-open auction."""
import contextlib
import io
import os
import re
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

os.environ.setdefault("LOGURU_LEVEL", "ERROR")
os.environ["LOG_SENTRY"] = ""
import shioaji as sj


def load_env(path):
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def flatten_futures(root):
    contracts = []
    for item in root:
        try:
            children = [item] if hasattr(item, "delivery_month") else list(item)
            contracts.extend(c for c in children if hasattr(c, "delivery_month"))
        except TypeError:
            pass
    return contracts


def nearest(contracts, today):
    def expiry(contract):
        raw = str(getattr(contract, "delivery_date", "")).replace("/", "-")
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return date.max

    active = [
        c for c in contracts
        if re.fullmatch(r"\d{6}", str(getattr(c, "delivery_month", "")))
        and expiry(c) >= today
    ]
    return min(active, key=lambda c: (expiry(c), str(c.symbol)), default=None)


def wall_clock(ns):
    # ts is already encoded as Taipei wall-clock time; do not add UTC+8.
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).replace(tzinfo=None)


@contextlib.contextmanager
def quiet():
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        yield


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    env = load_env(Path(__file__).resolve().parent.parent / ".env")
    api_key = env.get("SHIOAJI_API_KEY", "").strip()
    secret_key = env.get("SHIOAJI_SECRET_KEY", "").strip()
    if not api_key or not secret_key:
        print("login FAILED: missing credentials")
        return 1

    api = None
    try:
        with quiet():
            api = sj.Shioaji()
            api.login(
                api_key=api_key, secret_key=secret_key, fetch_contract=True,
                contracts_timeout=30_000, subscribe_trade=False,
            )
        print("login OK")
        today = date.today()
        contracts = flatten_futures(api.Contracts.Futures)
        txf_pool = [
            c for c in contracts
            if str(getattr(c, "category", "")).upper() == "TXF"
            or str(getattr(c, "symbol", "")).upper().startswith("TXF")
        ]
        targets = [("TXF 近月", nearest(txf_pool, today))]
        for code in ("2330", "2317"):
            pool = [
                c for c in contracts
                if str(getattr(c, "underlying_code", "")) == code
                and str(getattr(c, "underlying_kind", "")).upper() == "S"
                and "小型" not in str(getattr(c, "name", ""))
            ]
            targets.append((f"{code} 個股期近月", nearest(pool, today)))

        results = []
        for label, contract in targets:
            if contract is None:
                print(f"\n{label}: contract NOT FOUND")
                results.append((label, "N/A", 0, 0))
                continue
            print(f"\n{label}: symbol={contract.symbol}, code={contract.code}, delivery_month={contract.delivery_month}")
            print(f"limit_up={contract.limit_up}, limit_down={contract.limit_down}, reference={contract.reference}")
            try:
                with quiet():
                    ticks = api.ticks(contract, date=today.isoformat(), timeout=15_000)
                timestamps = [wall_clock(ns) for ns in list(ticks.ts)]
                before = sum(ts.date() == today and ts.time() < time(8, 45) for ts in timestamps)
                auction = sum(
                    ts.date() == today and time(8, 30) <= ts.time() < time(8, 45)
                    for ts in timestamps
                )
                has_simtrade = hasattr(ticks, "simtrade")
                simtrade = list(getattr(ticks, "simtrade", [])) if has_simtrade else []
                sim_count = sum(value is True or value == 1 for value in simtrade)
                earliest = min(timestamps).isoformat(" ", "microseconds") if timestamps else "N/A"
                latest = max(timestamps).isoformat(" ", "microseconds") if timestamps else "N/A"
                print(f"ticks={len(timestamps)}, earliest={earliest}, latest={latest}")
                print(f"time<08:45:00={'Y' if before else 'N'} (count={before}), 08:30-08:45 count={auction}")
                absent = "" if has_simtrade else " (field absent)"
                print(f"simtrade field={'Y' if has_simtrade else 'N'}, simtrade==1 count={sim_count}{absent}")
                results.append((label, earliest, before, auction))
            except Exception as exc:
                print(f"ticks FAILED: {type(exc).__name__}")
                results.append((label, "ERROR", 0, 0))

        answer = "Y" if sum(row[3] for row in results) else "N"
        evidence = "; ".join(
            f"{label} earliest={earliest}, <08:45={before}, auction={auction}"
            for label, earliest, before, auction in results
        )
        print(f"\n明確結論：個股期貨/期貨盤前(08:30–08:45)試撮 tick 事後可從歷史 API 撈回：{answer}；佐證：{evidence}")
        return 0
    except Exception as exc:
        print(f"probe FAILED: {type(exc).__name__}")
        return 1
    finally:
        if api is not None:
            with contextlib.suppress(Exception):
                with quiet():
                    api.logout()


if __name__ == "__main__":
    if __package__:
        from ._execution_guard import require_explicit_broker_login_confirmation
    else:
        from _execution_guard import require_explicit_broker_login_confirmation

    if not require_explicit_broker_login_confirmation():
        raise SystemExit(2)
    raise SystemExit(main())
