#!/usr/bin/env bash
# macOS / Linux 對應 launch.vbs（一鍵啟動.vbs）的一鍵入口。
# macOS 上副檔名取 .command，Finder 雙擊即可執行（需先 chmod +x）。
#
# 與 VBS 版同樣的四件事：沿用既有服務、以背景模式啟動、等健康檢查通過、
# 開瀏覽器。同樣以本資料夾為工作目錄，因此讀得到同資料夾的 .env。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

VENV_PYTHON="$PROJECT_DIR/.venv/bin/python"
SERVICE="$PROJECT_DIR/service.py"
HOST="127.0.0.1"
PORT="${PORT:-8900}"
URL="http://$HOST:$PORT/"
HEALTH="$URL""api/state"
LOG_DIR="$PROJECT_DIR/log"
LAUNCH_LOG="$LOG_DIR/launch.log"
LOCK_DIR="$PROJECT_DIR/.launch.lock"
WAIT_SECONDS=30

die() {
    printf '%s\n' "$1" >&2
    # 雙擊執行時終端機會隨腳本結束而關閉，錯誤要停在畫面上才看得到。
    [ -t 0 ] && read -r -p "按 Enter 關閉..." _ || true
    exit 1
}

open_browser() {
    if command -v open >/dev/null 2>&1; then
        open "$URL"
    elif command -v xdg-open >/dev/null 2>&1; then
        xdg-open "$URL"
    else
        echo "請自行以瀏覽器開啟 $URL"
    fi
}

healthy() {
    curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1
}

# 重複點擊時擋掉第二份啟動流程。mkdir 是 POSIX 上的原子操作，因此不會
# 出現兩份同時判定「鎖是空的」。
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    if healthy; then
        open_browser
        exit 0
    fi
    die "另一份啟動流程正在進行中。若確定沒有，請刪除 $LOCK_DIR 後重試。"
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# 服務原本就在跑就直接沿用，不開第二份。
if healthy; then
    open_browser
    exit 0
fi

[ -x "$VENV_PYTHON" ] || die "找不到 .venv/bin/python。請先執行 ./install.sh。"
[ -f "$SERVICE" ] || die "找不到 service.py。"

mkdir -p "$LOG_DIR"
# nohup + setsid 等價於 VBS 的隱藏視窗啟動：關掉這個終端機不會一起帶走服務。
nohup "$VENV_PYTHON" "$SERVICE" --host "$HOST" --port "$PORT" \
    >>"$LAUNCH_LOG" 2>&1 &
SERVICE_PID=$!

deadline=$((SECONDS + WAIT_SECONDS))
while [ "$SECONDS" -lt "$deadline" ]; do
    if healthy; then
        open_browser
        exit 0
    fi
    if ! kill -0 "$SERVICE_PID" 2>/dev/null; then
        die "服務啟動後隨即結束。詳見 ${LAUNCH_LOG}（該檔不會列印金鑰）。"
    fi
    sleep 1
done

die "服務在 ${WAIT_SECONDS} 秒內未通過 $HEALTH 健康檢查。詳見 ${LAUNCH_LOG}。"
