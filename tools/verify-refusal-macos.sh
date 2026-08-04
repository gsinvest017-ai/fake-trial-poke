#!/usr/bin/env bash
# macOS 版的「憑證失效真的會擋住」驗證。
#
# 為什麼需要這支：`keyguard refusalcheck` / `keyguard verify-refusal` 在
# sys.platform != "win32" 時直接回報 SKIP，所以 macOS build 沒有任何自動
# 證明。而在 macOS 上手動開一次特別不可信——沒公證的 .app 被 Gatekeeper
# 擋下、缺 tkinter/pywebview 導致拒絕畫面降級成純文字、Shioaji 登入失敗，
# 這三種情況跟「授權拒絕」在畫面上長得一模一樣（都是「按了沒反應」）。
#
# 這支做的事跟 Windows 版的 refusalcheck 同一套：
#   1. 開一個暫時的 APPDATA，讓應用自己建授權池（不動你真正的啟用狀態）。
#   2. 把池子的日期推到過去，模擬「憑證失效」。
#   3. 啟動 .app，斷言：行程還活著（視窗擋著）、PORT 8900 沒有被開、
#      關掉視窗後以非零退出。
#   4. 截圖存證。
#
# 最關鍵的是 PORT 那條：蓋在一個跑著的服務上面的鎖屏是裝飾，不是閘門。
#
# 用法：
#   ./tools/verify-refusal-macos.sh \
#       --app "/Applications/Fake Trial Poke.app" \
#       --email gsinvest018@gsinvest.com.tw
set -euo pipefail

APP_BUNDLE=""
EMAIL="${FAKE_TRIAL_POKE_LICENCE_EMAIL:-}"
APP_ID="FAKE_TRIAL_POKE"
EMAIL_ENV="FAKE_TRIAL_POKE_LICENCE_EMAIL"
PORT=8900
DAYS_AGO=30
SETTLE_SECONDS=12
SHOT="refusal-macos.png"
KEEP_SCRATCH=0
CHECK_RECOVERY=0
RECOVERY_TIMEOUT=60

while [ $# -gt 0 ]; do
    case "$1" in
        --app) APP_BUNDLE="${2:-}"; shift 2 ;;
        --email) EMAIL="${2:-}"; shift 2 ;;
        --port) PORT="${2:-8900}"; shift 2 ;;
        --shot) SHOT="${2:-}"; shift 2 ;;
        --days-ago) DAYS_AGO="${2:-30}"; shift 2 ;;
        --keep-scratch) KEEP_SCRATCH=1; shift ;;
        --check-recovery) CHECK_RECOVERY=1; shift ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

[ "$(uname -s)" = "Darwin" ] || { echo "macOS only." >&2; exit 2; }
[ -n "$APP_BUNDLE" ] || { echo "--app 必填（.app 路徑）" >&2; exit 2; }
[ -n "$EMAIL" ] || { echo "--email 必填" >&2; exit 2; }

EXE="$APP_BUNDLE/Contents/MacOS/fake-trial-poke"
[ -x "$EXE" ] || { echo "找不到可執行檔：$EXE" >&2; exit 2; }

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "需要 python3 來改寫授權池。" >&2; exit 2; }

PASS=0
FAIL=0
record() {  # record PASS|FAIL "訊息"
    if [ "$1" = "PASS" ]; then PASS=$((PASS + 1)); else FAIL=$((FAIL + 1)); fi
    printf '  [%s] %s\n' "$1" "$2"
}

SCRATCH="$(mktemp -d "${TMPDIR:-/tmp}/keyguard-refusal.XXXXXX")"
cleanup() {
    [ "$KEEP_SCRATCH" = "1" ] && { echo "保留暫存 APPDATA：$SCRATCH"; return; }
    rm -rf "$SCRATCH"
}
trap cleanup EXIT

echo
echo "暫存 APPDATA：$SCRATCH"
echo "（你真正的啟用狀態在 \$HOME 底下，本腳本不會碰）"
echo

# ── 1. 讓應用自己建池子 ───────────────────────────────────────────────
# 由應用自己寫，schema 才會是這個 build 真的讀得懂的那一份；手刻一個然後
# 被應用以別的理由拒絕，證明不了任何跟到期有關的事。
echo "[1/4] 讓應用建立授權池 ..."
# 環境變數名是變數（$EMAIL_ENV），只能用 env 傳；shell 的 NAME=value
# 前綴語法不接受動態名稱。
env APPDATA="$SCRATCH" "$EMAIL_ENV=$EMAIL" KEYGUARD_NO_DIALOG=1 \
    "$EXE" --licence-status >"$SCRATCH/status-before.json" 2>&1 || true

# macOS 的 pool 位置與 Windows 相同——keyguard.pool 用的是 APPDATA 環境
# 變數，設了就一起改道，這正是這個測試能隔離的原因。
POOL="$SCRATCH/ECOPY_NEW/ssot/licenses/$APP_ID/$EMAIL/license_pool.json"
if [ ! -f "$POOL" ]; then
    echo "應用沒有在 $POOL 建立授權池。" >&2
    echo "先確認 '$EXE --licence-status' 本身跑得起來（Gatekeeper？）。" >&2
    exit 1
fi
record PASS "授權池已建立"

# ── 2. 把日期推到過去 ─────────────────────────────────────────────────
# LicenceKey 必須清掉：evaluate() 每次都重新驗簽章載荷，手改的到期日會被
# 簽章裡的值蓋回去（那正是防竄改在運作）。留著等於整個佈置自動失效。
echo "[2/4] 把授權池推過到期日（$DAYS_AGO 天前）..."
"$PYTHON" - "$POOL" "$DAYS_AGO" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone

path, days_ago = sys.argv[1], int(sys.argv[2])
data = json.load(open(path, encoding="utf-8"))
expired_on = datetime.now(timezone.utc) - timedelta(days=days_ago)
data["Plan"] = "PRO"
data["Provisioned"] = True          # 到期的正式授權，不是用完的 trial
data["IssuedUtc"] = (expired_on - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
data["ExpiresUtc"] = expired_on.strftime("%Y-%m-%dT%H:%M:%SZ")
data["LicenceKey"] = ""
json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
PY
record PASS "授權池已標記為過期"

# ── 3. 啟動並斷言 ─────────────────────────────────────────────────────
echo "[3/4] 啟動應用，等待拒絕視窗 ..."
env APPDATA="$SCRATCH" "$EMAIL_ENV=$EMAIL" \
    "$EXE" >"$SCRATCH/run.log" 2>&1 &
APP_PID=$!

port_open() {
    # -n 不解析主機名，-P 不解析服務名：慢的那部分跟這裡要問的事無關。
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

# 固定觀察窗，不是輪詢到出現為止：這裡要證明的是「停在拒絕畫面」，
# 而停著的行程不會有任何事件可以等。給足時間讓它要嘛開窗、要嘛退出。
sleep "$SETTLE_SECONDS"

if kill -0 "$APP_PID" 2>/dev/null; then
    record PASS "行程仍在執行（視窗擋著，沒有直接退出）"
else
    record FAIL "行程沒有停在拒絕畫面——可能降級成純文字後直接退出。見 $SCRATCH/run.log"
fi

# 這一條才是真正的閘門：服務絕不能已經起來。
if port_open; then
    record FAIL "PORT $PORT 已被監聽——服務在鎖屏後面跑起來了，這是裝飾不是閘門"
else
    record PASS "PORT $PORT 沒有被監聽（服務確實沒有啟動）"
fi

# 視窗標題偵測需要「輔助使用」權限；沒授權就只警告，不當成失敗。
if osascript -e 'tell application "System Events" to get name of every process' \
        >/dev/null 2>&1; then
    if osascript -e 'tell application "System Events" to get title of every window of (every process whose background only is false)' 2>/dev/null \
            | grep -q "假試撮盤前監控"; then
        record PASS "找到應用自己的視窗標題"
    else
        echo "  [WARN] 沒抓到預期的視窗標題；請看截圖人工確認。"
    fi
else
    echo "  [WARN] 沒有輔助使用權限，略過視窗標題偵測。"
    echo "         系統設定 → 隱私權與安全性 → 輔助使用 → 允許終端機。"
fi

echo "[4/4] 截圖 → $SHOT"
screencapture -x "$SHOT" 2>/dev/null && record PASS "已存證 $SHOT" \
    || echo "  [WARN] 截圖失敗（需要「螢幕錄製」權限）。"

# ── 收尾：關掉視窗，確認非零退出 ─────────────────────────────────────
if kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    wait "$APP_PID" 2>/dev/null || true
    echo "  （由本腳本關閉視窗；退出碼在此情境下不具判別力）"
else
    wait "$APP_PID" 2>/dev/null
    code=$?
    if [ "$code" -ne 0 ]; then
        record PASS "行程以非零退出（${code}）"
    else
        record FAIL "行程以 0 退出——被拒絕卻回報成功"
    fi
fi

# ── 5. 復權：把授權推回未來，服務必須回得來 ───────────────────────────
# 對應 Windows refusalcheck 的 --check-recovery。少了這一條，一支「無論如何
# 都拒絕」的閘門也會拿到滿分——那不是防盜，那是永久卡死。廠商續期之後客戶
# 開得起來，跟過期之後開不起來，是同一個機制的兩面，要一起證明。
if [ "$CHECK_RECOVERY" = "1" ]; then
    echo
    echo "[復權] 把授權池推回未來，確認服務回得來 ..."
    "$PYTHON" - "$POOL" <<'PY'
import json, sys
from datetime import datetime, timedelta, timezone

path = sys.argv[1]
data = json.load(open(path, encoding="utf-8"))
renewed = datetime.now(timezone.utc) + timedelta(days=30)
data["Plan"] = "PRO"
data["Provisioned"] = True
data["IssuedUtc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
data["ExpiresUtc"] = renewed.strftime("%Y-%m-%dT%H:%M:%SZ")
data["LicenceKey"] = ""
json.dump(data, open(path, "w", encoding="utf-8"), indent=2)
PY

    env APPDATA="$SCRATCH" "$EMAIL_ENV=$EMAIL" \
        "$EXE" --port "$PORT" >"$SCRATCH/recovery.log" 2>&1 &
    RECOVERY_PID=$!

    recovered=0
    deadline=$((SECONDS + RECOVERY_TIMEOUT))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if port_open; then
            recovered=1
            break
        fi
        kill -0 "$RECOVERY_PID" 2>/dev/null || break
        sleep 1
    done

    if [ "$recovered" = "1" ]; then
        record PASS "續期後服務回得來（PORT $PORT 已監聽）"
    else
        record FAIL "續期後服務仍起不來——這個閘門會把付費客戶鎖在門外。見 $SCRATCH/recovery.log"
    fi

    kill "$RECOVERY_PID" 2>/dev/null || true
    wait "$RECOVERY_PID" 2>/dev/null || true
fi

echo
echo "PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "拒絕行為驗證通過。"
