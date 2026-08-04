#!/usr/bin/env bash
# macOS 對應 schedule_morning.ps1 的每日 08:25 錄製排程，載體是 launchd
# LaunchAgent。
#
# 與 Windows 版的對照與差異：
#   Register/Status/Unregister/Execute 四個 Mode 一一對應。
#   Task Scheduler 的「每 2 分鐘重複、持續 45 分鐘」在 launchd 沒有等價
#   設定，改以 08:25 到 09:09 之間每 2 分鐘一個 StartCalendarInterval
#   項目展開；每次觸發都會先做健康檢查，服務已在跑就直接結束。
#   Windows 的 S4U（開機未登入也錄製）在 macOS 需要 root 安裝
#   LaunchDaemon，本腳本刻意不做——它只裝使用者層 LaunchAgent，等同
#   Windows 降級後的 Interactive 排程：08:25 時該使用者必須已登入。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.gsinvest.fake-trial-poke"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
SERVICE="$PROJECT_DIR/service.py"
LOG_DIR="$PROJECT_DIR/log"
HOST="127.0.0.1"
PORT=8900
MODE="register"

START_HOUR=8
START_MINUTE=25
REPEAT_EVERY_MINUTES=2
REPEAT_FOR_MINUTES=45

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="$(printf '%s' "${2:-}" | tr '[:upper:]' '[:lower:]')"; shift 2 ;;
        --port) PORT="${2:-8900}"; shift 2 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done

if [ "$(uname -s)" != "Darwin" ]; then
    echo "本腳本只支援 macOS（launchd）。Linux 請自行掛 systemd --user timer 或 cron。" >&2
    exit 2
fi

resolve_python() {
    local local_python="$PROJECT_DIR/.venv/bin/python"
    if [ -x "$local_python" ]; then
        printf '%s' "$local_python"
        return 0
    fi
    # 找不到不能讓 set -e 直接把腳本殺掉——呼叫端要能印出「找不到 Python」
    # 這句話，而不是靜靜地什麼都沒發生。
    command -v python3.12 || command -v python3 || true
}

healthy() {
    curl -fsS --max-time 2 "http://$HOST:$PORT/api/state" >/dev/null 2>&1
}

calendar_intervals() {
    # 展開成 launchd 吃得下的 StartCalendarInterval 陣列：週一到週五，
    # 08:25 起每 REPEAT_EVERY_MINUTES 分鐘一次，共 REPEAT_FOR_MINUTES 分鐘。
    local weekday minute_offset total hour minute
    total=$((START_HOUR * 60 + START_MINUTE))
    for weekday in 1 2 3 4 5; do
        minute_offset=0
        while [ "$minute_offset" -le "$REPEAT_FOR_MINUTES" ]; do
            hour=$(((total + minute_offset) / 60))
            minute=$(((total + minute_offset) % 60))
            printf '        <dict>\n'
            printf '            <key>Weekday</key><integer>%d</integer>\n' "$weekday"
            printf '            <key>Hour</key><integer>%d</integer>\n' "$hour"
            printf '            <key>Minute</key><integer>%d</integer>\n' "$minute"
            printf '        </dict>\n'
            minute_offset=$((minute_offset + REPEAT_EVERY_MINUTES))
        done
    done
}

write_plist() {
    mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
    cat >"$PLIST" <<PLIST_HEAD
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$PROJECT_DIR/schedule_morning.sh</string>
        <string>--mode</string>
        <string>execute</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>StandardOutPath</key>
    <string>$LOG_DIR/schedule.log</string>
    <key>StandardErrorPath</key>
    <string>$LOG_DIR/schedule.log</string>
    <key>StartCalendarInterval</key>
    <array>
PLIST_HEAD
    calendar_intervals >>"$PLIST"
    cat >>"$PLIST" <<'PLIST_TAIL'
    </array>
</dict>
</plist>
PLIST_TAIL
}

case "$MODE" in
    register)
        [ -f "$SERVICE" ] || { echo "找不到 service.py" >&2; exit 1; }
        write_plist
        launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
        launchctl bootstrap "gui/$UID" "$PLIST"
        echo "已註冊 launchd agent：$LABEL"
        echo "排程：週一至週五 08:25 起每 ${REPEAT_EVERY_MINUTES} 分鐘重試，共 ${REPEAT_FOR_MINUTES} 分鐘。"
        echo "限制：這是使用者層 agent，08:25 時該 macOS 帳號必須已登入；"
        echo "      電腦需開機或在『節能』設定允許排程喚醒。"
        ;;
    status)
        launchctl print "gui/$UID/$LABEL"
        ;;
    unregister)
        launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
        rm -f "$PLIST"
        echo "已移除 launchd agent：$LABEL"
        ;;
    execute|run)
        if healthy; then
            echo "$(date '+%F %T') 服務已在執行，略過本次觸發。"
            exit 0
        fi
        python_exe="$(resolve_python)"
        [ -n "$python_exe" ] || { echo "找不到可用的 Python。" >&2; exit 1; }
        mkdir -p "$LOG_DIR"
        echo "$(date '+%F %T') 啟動 service.py（PORT $PORT）"
        exec "$python_exe" "$SERVICE" --host "$HOST" --port "$PORT"
        ;;
    *)
        echo "unknown mode: $MODE（可用：register/status/unregister/execute）" >&2
        exit 2
        ;;
esac
