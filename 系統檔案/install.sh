#!/usr/bin/env bash
# macOS / Linux 對應 install.bat 的一鍵環境安裝。
#
# 與 Windows 版同樣的五個步驟、同樣的冪等性：可以重複執行，失敗會明確
# 說明原因。差別只在排程層——Windows 用 Task Scheduler，這裡用 launchd
# （macOS）；Linux 沒有等價的使用者層排程，會明確跳過而不是假裝成功。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
REQUIREMENTS="$PROJECT_DIR/requirements.txt"
SCHEDULE_SCRIPT="$PROJECT_DIR/schedule_morning.sh"
ENV_FILE="$PROJECT_DIR/.env"
LOGIN_SMOKE="$PROJECT_DIR/scripts/verify_login.py"
INSTALL_MARKER="$PROJECT_DIR/.install_complete"
PORT="${PORT:-8900}"

fail() {
    printf '[FAILED] %s\n' "$1" >&2
    printf '\n安裝未完成。修正後可直接重跑，本安裝器是冪等的。\n' >&2
    exit 1
}

echo
echo "========================================"
echo "  假試撮盤前監控 - 一鍵安裝 (macOS/Linux)"
echo "========================================"
echo

[ -f "$REQUIREMENTS" ] || fail "找不到 requirements.txt。"
[ -f "$LOGIN_SMOKE" ] || fail "找不到 scripts/verify_login.py。"
[ -f "$ENV_FILE" ] || fail "找不到內建的 .env。請向系統提供者索取完整資料夾；.env 不可外流。"

# [1/5] 找 64 位元 Python 3.12。
# 不像 Windows 版會自動 winget/python.org 安裝——macOS 上代替使用者裝
# Python 會踩到 Homebrew / pyenv / 系統 Python 三者的權限與 PATH 衝突，
# 失敗的方式比缺 Python 本身更難診斷。這裡只偵測並給出明確指示。
echo "[1/5] 尋找 64 位元 Python 3.12 ..."
PYTHON312=""
for candidate in \
    "${PYTHON312_EXE:-}" \
    "$(command -v python3.12 || true)" \
    /opt/homebrew/bin/python3.12 \
    /usr/local/bin/python3.12 \
    "$HOME/.pyenv/versions/3.12"*/bin/python3.12 \
    /Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12
do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c 'import sys,struct; raise SystemExit(0 if sys.version_info[:2]==(3,12) and struct.calcsize("P")*8==64 else 1)' 2>/dev/null; then
        PYTHON312="$candidate"
        break
    fi
done
if [ -z "$PYTHON312" ]; then
    fail "找不到 64 位元 Python 3.12。macOS 請執行 'brew install python@3.12'（或到 python.org 下載安裝），Linux 請用發行版套件管理員安裝，然後重跑本安裝器。"
fi
echo "[1/5] Python 3.12 就緒：$PYTHON312"

if [ -x "$VENV_PYTHON" ] && "$VENV_PYTHON" -c 'import sys,struct; raise SystemExit(0 if sys.version_info[:2]==(3,12) and struct.calcsize("P")*8==64 else 1)' 2>/dev/null; then
    echo "[1/5] 沿用既有的 Python 3.12 .venv。"
else
    if [ -e "$VENV_DIR" ]; then
        echo "[1/5] 既有 .venv 不可用，重建中 ..."
        rm -rf "$VENV_DIR"
    fi
    echo "[1/5] 建立資料夾內的 .venv ..."
    "$PYTHON312" -m venv "$VENV_DIR" || fail "無法建立 .venv（偵測到的 Python 是 ${PYTHON312}）。"
fi

echo "[2/5] 升級 pip ..."
"$VENV_PYTHON" -m pip install --upgrade pip || fail "pip 升級失敗，請檢查網路與 Python 安裝。"

echo "[3/5] 安裝 requirements.txt ..."
"$VENV_PYTHON" -m pip install -r "$REQUIREMENTS" || fail "相依套件安裝失敗，請看上方訊息。"

echo "[4/5] 驗證永豐金鑰（唯讀登入後立即登出）..."
"$VENV_PYTHON" "$LOGIN_SMOKE" || fail "金鑰驗證失敗。請檢查 .env，但不要把內容貼給任何人。"

echo "[5/5] 註冊每日 08:25 錄製排程 ..."
case "$(uname -s)" in
    Darwin)
        [ -f "$SCHEDULE_SCRIPT" ] || fail "找不到 schedule_morning.sh。"
        bash "$SCHEDULE_SCRIPT" --mode register --port "$PORT" \
            || fail "環境已就緒，但 launchd 排程註冊失敗。請保留上方錯誤訊息重試；不要貼出 .env。"
        bash "$SCHEDULE_SCRIPT" --mode status --port "$PORT" >/dev/null \
            || fail "註冊回報成功，但查詢不到該 launchd agent。"
        ;;
    *)
        echo "[5/5] 略過：Linux 沒有本專案支援的使用者層排程。"
        echo "      需要每日自動錄製請自行掛 systemd --user timer 或 cron，"
        echo "      指向：$VENV_PYTHON $PROJECT_DIR/service.py --port $PORT"
        ;;
esac

echo "installed" >"$INSTALL_MARKER"
echo
echo "[SUCCESS] 環境就緒。"
echo "下一步：執行 ./launch.command（macOS 可直接雙擊）開啟前端。"
echo "排程當下電腦必須開機或允許喚醒，否則不會錄製。"
echo
