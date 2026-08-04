#!/usr/bin/env bash
# macOS 出貨產線。對應 Windows 的 gs-app-pack + pack.config.ps1。
#
# 為什麼需要這支
# --------------
# pack.config.ps1 的價值不在「產生檔案」，而在 $PostBuildCheck 那三道閘門：
# packagecheck（keyguard 真的被凍進去了）、smoke_launch（做完的 app 真的
# 供得出 HTTP UI）、refusalcheck（憑證失效真的擋得住，而且沒有偷偷開 port）。
# 那三道跑在 PowerShell + Inno Setup 上，macOS 一道都沒有。結果是 macOS 的
# .app 只能算開發產物：它可能少了 keyguard、可能拒絕畫面降級成看不見的純
# 文字、可能鎖屏底下服務照跑——而這三種瑕疵在客戶機器上都長得像「應用程式
# 打不開」，沒有人會回報成授權問題。
#
# 這支把同樣三道閘門搬到 macOS，並補上 Windows 不需要的第四道：Gatekeeper。
# 沒有簽章與公證的 .app 在對方機器上會被直接擋下，那個失敗同樣長得像授權
# 拒絕。少了這道，前面三道證明的事在客戶那端都還沒發生就結束了。
#
# 順序是刻意的：先簽章公證，再跑行為閘門。反過來的話，通過測試的產物與
# 實際出貨的產物就不是同一個——而 codesign 會改寫 bundle 內容。
#
# 用法：
#   # 開發建置（不簽章，明確標記為不可出貨）
#   ./tools/pack-macos.sh --clean --allow-unsigned --skip-notarize \
#       --licence-email you@example.com
#
#   # 正式出貨
#   ./tools/pack-macos.sh --clean \
#       --sign-identity "Developer ID Application: GS Invest (TEAMID)" \
#       --notarize-profile gs-notary \
#       --licence-email buyer@example.com
#
# notarytool 的 keychain profile 先建一次：
#   xcrun notarytool store-credentials gs-notary \
#       --apple-id <apple id> --team-id <team id> --password <app 專用密碼>
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$REPO_DIR/系統檔案"
VENV_PYTHON="$SOURCE_DIR/.venv/bin/python"
SPEC="$REPO_DIR/fake-trial-poke-macos.spec"
ENTITLEMENTS="$REPO_DIR/tools/entitlements-macos.plist"
REFUSAL_CHECK="$REPO_DIR/tools/verify-refusal-macos.sh"
ICON_TOOL="$REPO_DIR/tools/make-icns.py"
DIST_DIR="$REPO_DIR/dist"
APP_NAME="Fake Trial Poke.app"
APP_BUNDLE="$DIST_DIR/$APP_NAME"
APP_ID="FAKE_TRIAL_POKE"
EMAIL_ENV="FAKE_TRIAL_POKE_LICENCE_EMAIL"
BUNDLE_ID="com.gsinvest.fake-trial-poke"
PORT=8900
SMOKE_TIMEOUT=90

CLEAN=0
SIGN_IDENTITY=""
NOTARY_PROFILE=""
ALLOW_UNSIGNED=0
SKIP_NOTARIZE=0
LICENCE_EMAIL="${FAKE_TRIAL_POKE_LICENCE_EMAIL:-}"
SKIP_BUILD=0
MAKE_DMG=0

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) CLEAN=1; shift ;;
        --sign-identity) SIGN_IDENTITY="${2:-}"; shift 2 ;;
        --notarize-profile) NOTARY_PROFILE="${2:-}"; shift 2 ;;
        --allow-unsigned) ALLOW_UNSIGNED=1; shift ;;
        --skip-notarize) SKIP_NOTARIZE=1; shift ;;
        --licence-email) LICENCE_EMAIL="${2:-}"; shift 2 ;;
        --port) PORT="${2:-8900}"; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --dmg) MAKE_DMG=1; shift ;;
        -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
        *) printf 'unknown argument: %s\n' "$1" >&2; exit 2 ;;
    esac
done

# ── 記帳 ──────────────────────────────────────────────────────────────
PASS=0
FAIL=0
WAIVED=0
FAILURES=()

pass()   { PASS=$((PASS + 1)); printf '  \033[32m[PASS]\033[0m %s\n' "$1"; }
fail()   { FAIL=$((FAIL + 1)); FAILURES+=("$1"); printf '  \033[31m[FAIL]\033[0m %s\n' "$1"; }
waive()  { WAIVED=$((WAIVED + 1)); printf '  \033[33m[WAIVED]\033[0m %s\n' "$1"; }
step()   { printf '\n\033[1m== %s\033[0m\n' "$1"; }
die()    { printf '\n\033[31m建置中止：%s\033[0m\n' "$1" >&2; exit 1; }

# 閘門失敗要立刻停，不要繼續往下做出一個「看起來完成了」的產物。
gate_or_die() {
    [ "$FAIL" -eq 0 ] || die "$1（失敗 $FAIL 項）"
}

[ "$(uname -s)" = "Darwin" ] || die "這支只在 macOS 上有意義。"

# ── Gate 0：前置條件 ──────────────────────────────────────────────────
# 全部在建置之前檢查完。這些是「建了也不能出貨」的條件，讓它們在花十分鐘
# 打包之後才爆出來沒有任何好處。
step "Gate 0 — 前置條件"

[ -f "$SPEC" ] || die "找不到 $SPEC"
[ -x "$VENV_PYTHON" ] || die "找不到 ${VENV_PYTHON}。請先在 系統檔案/ 執行 ./install.sh。"

if "$VENV_PYTHON" -c 'import sys,struct; raise SystemExit(0 if sys.version_info[:2]==(3,12) and struct.calcsize("P")*8==64 else 1)'; then
    pass "建置用 Python 是 3.12（64 位元）"
else
    fail "建置用 Python 不是 3.12／64 位元"
fi

# 對應 pack.config.ps1 的 $RequireNonEditable = @("keyguard")。
# editable install 只在 site-packages 留一個指回原始碼的指標，PyInstaller
# 收不進去，做出來的 .app 會是一個沒有授權閘門的完整可用程式。
keyguard_state="$("$VENV_PYTHON" - <<'PY' 2>&1 || true
import importlib.util, json, pathlib, sys

spec = importlib.util.find_spec("keyguard")
if spec is None or not spec.origin:
    print("MISSING"); raise SystemExit(0)

origin = pathlib.Path(spec.origin).resolve()
# editable install 的兩種形跡：__editable__ finder，或 dist-info 的
# direct_url.json 標了 editable=true。
if "__editable__" in str(origin):
    print(f"EDITABLE {origin}"); raise SystemExit(0)
for site in map(pathlib.Path, sys.path):
    for info in site.glob("keyguard-*.dist-info/direct_url.json"):
        try:
            data = json.loads(info.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("dir_info", {}).get("editable"):
            print(f"EDITABLE {info}"); raise SystemExit(0)
print(f"OK {origin}")
PY
)"
case "$keyguard_state" in
    OK\ *)       pass "keyguard 是一般安裝（${keyguard_state#OK }）" ;;
    EDITABLE\ *) fail "keyguard 是 editable install（${keyguard_state#EDITABLE }）；PyInstaller 收不進去，做出來的 .app 不會有授權閘門。請 pip uninstall keyguard 後改用一般安裝。" ;;
    *)           fail "建置環境沒有安裝 keyguard；沒有它做出來的是一個沒有授權閘門的 .app。" ;;
esac

# tkinter 與 pywebview 決定拒絕畫面是「視窗」還是「只印到 stderr」。
# GUI 模式下後者等於完全看不見——客戶只會看到按了沒反應。
for module in tkinter webview; do
    if "$VENV_PYTHON" -c "import $module" >/dev/null 2>&1; then
        pass "$module 可 import"
    else
        fail "$module 缺席；拒絕畫面會降級成看不見的純文字。見 系統檔案/requirements-macos.txt。"
    fi
done

# 直接問 keyguard 自己：這個環境做得出視窗嗎。這是 pack.config.ps1 註解裡
# 指名的判準（refusal_ui() == "window"），比逐一猜相依套件可靠。
if refusal_ui="$("$VENV_PYTHON" -c 'from keyguard.appgate import AppGate; print(AppGate.refusal_ui())' 2>/dev/null)"; then
    if [ "$refusal_ui" = "window" ]; then
        pass "AppGate.refusal_ui() == window"
    else
        fail "AppGate.refusal_ui() == ${refusal_ui}（需要 window）；拒絕會降級，客戶看不見。"
    fi
else
    fail "問不到 AppGate.refusal_ui()（keyguard 不可用）"
fi

[ -n "$LICENCE_EMAIL" ] || fail "需要 --licence-email（或設 ${EMAIL_ENV}）：smoke test 與拒絕驗證都要它。"

# 簽章與公證的豁免必須是明講的，不能因為沒帶參數就安靜地跳過。
if [ -z "$SIGN_IDENTITY" ] && [ "$ALLOW_UNSIGNED" -eq 0 ]; then
    fail "沒有 --sign-identity。未簽章的 .app 會被 Gatekeeper 擋下，而那個失敗跟授權拒絕在畫面上分不出來。確定只是開發建置請明確加 --allow-unsigned。"
fi
if [ -n "$SIGN_IDENTITY" ] && [ -z "$NOTARY_PROFILE" ] && [ "$SKIP_NOTARIZE" -eq 0 ]; then
    fail "有簽章但沒有 --notarize-profile。只簽章不公證，第一次在別台機器開啟仍會被 Gatekeeper 擋。確定不需要請明確加 --skip-notarize。"
fi

gate_or_die "前置條件不足"

# ── 圖示 ──────────────────────────────────────────────────────────────
# spec 找不到 .icns 就會靜靜地不給圖示。那不會讓 build 失敗，但會出貨一個
# 沒有辨識度的通用圖示，所以這裡先補齊。
step "圖示"
if [ -f "$REPO_DIR/static/gs-icon.icns" ]; then
    pass "沿用既有的 static/gs-icon.icns"
else
    "$VENV_PYTHON" "$ICON_TOOL" >/dev/null && pass "已產生 static/gs-icon.icns" \
        || fail "產生 .icns 失敗"
fi
gate_or_die "圖示準備失敗"

# ── 建置 ──────────────────────────────────────────────────────────────
step "建置 .app"
if [ "$SKIP_BUILD" -eq 1 ]; then
    waive "--skip-build：沿用既有的 $APP_BUNDLE"
    [ -d "$APP_BUNDLE" ] || die "--skip-build 但 $APP_BUNDLE 不存在"
else
    if [ "$CLEAN" -eq 1 ]; then
        rm -rf "$REPO_DIR/build" "$DIST_DIR"
    fi
    ( cd "$REPO_DIR" && "$VENV_PYTHON" -m PyInstaller "$SPEC" --clean --noconfirm ) \
        || die "PyInstaller 失敗"
    pass "PyInstaller 完成"
fi

APP_EXE="$APP_BUNDLE/Contents/MacOS/fake-trial-poke"
[ -x "$APP_EXE" ] || die "做不出可執行檔：$APP_EXE"
pass "產物存在：$APP_BUNDLE"

# ── Gate 1：簽章與公證 ────────────────────────────────────────────────
# 放在行為閘門之前，因為 codesign 會改寫 bundle。先測後簽等於測的不是出貨
# 的那一個。
step "Gate 1 — 簽章與公證（Gatekeeper）"

if [ -n "$SIGN_IDENTITY" ]; then
    # --deep 會連同 Frameworks 底下所有 .so/.dylib 一起簽；PyInstaller 的
    # 產物有數百個，漏簽任何一個都會讓 Gatekeeper 整包判定失敗。
    codesign --force --deep --timestamp --options runtime \
        --entitlements "$ENTITLEMENTS" \
        --sign "$SIGN_IDENTITY" "$APP_BUNDLE" \
        && pass "已簽章（${SIGN_IDENTITY}）" || fail "codesign 失敗"

    codesign --verify --deep --strict --verbose=2 "$APP_BUNDLE" 2>/dev/null \
        && pass "codesign --verify 通過" || fail "codesign --verify 不通過"
    gate_or_die "簽章失敗"

    if [ "$SKIP_NOTARIZE" -eq 1 ]; then
        waive "--skip-notarize：未公證，第一次在別台機器開啟會被 Gatekeeper 擋"
    else
        # notarytool 吃的是壓縮檔，不是資料夾。ditto 是唯一會完整保留
        # bundle 內符號連結與延伸屬性的打包方式；用 zip 會弄壞簽章。
        zip_path="$DIST_DIR/fake-trial-poke-notarize.zip"
        ditto -c -k --keepParent "$APP_BUNDLE" "$zip_path" || die "ditto 打包失敗"
        if xcrun notarytool submit "$zip_path" \
                --keychain-profile "$NOTARY_PROFILE" --wait; then
            pass "公證通過"
            # staple 之後，客戶機器離線也能通過 Gatekeeper；沒 staple 的話
            # 第一次開啟必須連得上 Apple。
            xcrun stapler staple "$APP_BUNDLE" \
                && pass "已 staple 公證票證" || fail "stapler staple 失敗"
        else
            fail "公證被退回。用 xcrun notarytool log <submission-id> --keychain-profile $NOTARY_PROFILE 看原因。"
        fi
        rm -f "$zip_path"
    fi
else
    waive "--allow-unsigned：未簽章，這個 .app 只能自己用"
fi

# spctl 是最終判準：它問的正是「Gatekeeper 會不會放行」，而不是「我們有沒有
# 執行過簽章指令」。
if spctl --assess --type execute --verbose=2 "$APP_BUNDLE" 2>&1 | grep -q "accepted"; then
    pass "spctl 判定 accepted（Gatekeeper 會放行）"
elif [ "$ALLOW_UNSIGNED" -eq 1 ] || [ "$SKIP_NOTARIZE" -eq 1 ]; then
    waive "spctl 判定 rejected（已豁免；此產物不可當授權版發給客戶）"
else
    fail "spctl 判定 rejected——客戶開不起來，而那個失敗看起來會跟授權拒絕一樣"
fi
gate_or_die "Gatekeeper 閘門未通過"

# ── Gate 2：keyguard 真的被凍結進去了 ─────────────────────────────────
step "Gate 2 — keyguard 已凍結進 .app"

# 先跑 keyguard 自己的 packagecheck。它在非 Windows 會把視窗那部分回報成
# SKIP，所以底下還有一條行為檢查——SKIP 不能當成 PASS，那正是這整支腳本
# 存在的理由。
if "$VENV_PYTHON" -m keyguard.packagecheck "$APP_BUNDLE" \
        --email-env "$EMAIL_ENV" --require-console-output 2>&1 | tee /dev/stderr \
        | grep -qi "skip"; then
    waive "packagecheck 有 SKIP 項目（非 Windows）；改由下方行為檢查認定"
else
    pass "keyguard.packagecheck 通過"
fi

# 行為檢查：問做完的 .app 自己。licensing.py 在收不到 keyguard 時會回報
# "keyguard not installed"，那是 fail-open 的執行期備援——出現在這裡就代表
# 這包做壞了。
status_json="$(env "$EMAIL_ENV=$LICENCE_EMAIL" KEYGUARD_NO_DIALOG=1 \
    "$APP_EXE" --licence-status 2>&1 || true)"
if printf '%s' "$status_json" | grep -qi "keyguard not installed"; then
    fail "做完的 .app 回報 keyguard not installed——授權閘門根本沒被凍進去"
elif printf '%s' "$status_json" | grep -q '"status"'; then
    pass ".app 回報得出授權狀態（keyguard 在包裡）"
else
    fail ".app 的 --licence-status 沒有給出可解析的結果：$status_json"
fi
gate_or_die "keyguard 未正確凍結"

# ── Gate 3：做完的 app 真的供得出 HTTP UI ─────────────────────────────
# 對應 Windows 的 smoke_launch.py。gs-app-pack 那支是 PowerShell 產線的一
# 部分，這裡就地實作。
step "Gate 3 — HTTP 啟動 smoke test"

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    die "PORT $PORT 已被占用，smoke test 無法判別是誰在回應。請先關掉它。"
fi

env "$EMAIL_ENV=$LICENCE_EMAIL" "$APP_EXE" --port "$PORT" \
    >"$DIST_DIR/smoke.log" 2>&1 &
SMOKE_PID=$!
smoke_cleanup() {
    kill "$SMOKE_PID" 2>/dev/null || true
    wait "$SMOKE_PID" 2>/dev/null || true
}
trap smoke_cleanup EXIT

deadline=$((SECONDS + SMOKE_TIMEOUT))
smoke_ok=0
while [ "$SECONDS" -lt "$deadline" ]; do
    if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
        smoke_ok=1
        break
    fi
    if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done

if [ "$smoke_ok" -eq 1 ]; then
    pass "HTTP UI 在 ${SMOKE_TIMEOUT}s 內回應 /api/state"
else
    fail "做完的 .app 沒有供出 HTTP UI。見 $DIST_DIR/smoke.log"
fi

# 回應一次不算數。app.py 是先把服務跑起來、再開 pywebview 視窗的，所以一個
# 缺了視窗相依的 build 會「先答得出 HTTP、然後整個行程退出」。只問一次的
# smoke test 剛好會在那個空窗期拿到 200 然後宣告通過。
# 2026-08-04 實機建置就是這個情形：bottle 沒被收進 .app，服務起得來、
# curl 得到回應，接著行程以 1 退出，視窗從來沒出現過。
if [ "$smoke_ok" -eq 1 ]; then
    settle=5
    sleep "$settle"
    if ! kill -0 "$SMOKE_PID" 2>/dev/null; then
        fail "回應過 /api/state 之後 ${settle}s 內行程就退出了——視窗那一段掛了（常見原因是視窗相依沒被收進 .app）。見 $DIST_DIR/smoke.log"
    elif curl -fsS --max-time 2 "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1; then
        pass "行程在 ${settle}s 後仍活著且持續供得出 HTTP"
    else
        fail "行程還在，但 ${settle}s 後 HTTP 已經不回應了。見 $DIST_DIR/smoke.log"
    fi
fi

smoke_cleanup
trap - EXIT
gate_or_die "smoke test 未通過"

# ── Gate 4：憑證失效真的擋得住 ────────────────────────────────────────
# 這就是把 tools/verify-refusal-macos.sh 從「手動跑一下」變成「過不了就不
# 給出貨」的地方。它內含最關鍵的那條斷言：拒絕當下 PORT 不能是開的——蓋在
# 一個跑著的服務上面的鎖屏是裝飾，不是閘門。
step "Gate 4 — 拒絕驗證（憑證失效擋得住，且沒有偷偷開 port）"

if [ ! -x "$REFUSAL_CHECK" ]; then
    fail "找不到可執行的 $REFUSAL_CHECK"
elif "$REFUSAL_CHECK" --app "$APP_BUNDLE" --email "$LICENCE_EMAIL" \
        --port "$PORT" --shot "$DIST_DIR/refusal-macos.png" --check-recovery; then
    pass "拒絕與復權行為驗證通過"
else
    fail "拒絕驗證未通過——這包不可以發給客戶"
fi
gate_or_die "拒絕驗證未通過"

# ── 出貨載體：DMG ─────────────────────────────────────────────────────
# 對應 Windows 的 Inno Setup 安裝程式。macOS 沒有安裝程式這個概念，等價物
# 是一個掛載後把 .app 拖進 /Applications 的 DMG。
#
# 一定要放在所有閘門之後：DMG 裡包的必須是通過驗證的那一份 .app。
if [ "$MAKE_DMG" -eq 1 ]; then
    step "出貨載體 — DMG"
    DMG_PATH="$DIST_DIR/fake-trial-poke-macos.dmg"
    dmg_stage="$(mktemp -d "${TMPDIR:-/tmp}/fake-trial-poke-dmg.XXXXXX")"

    # cp -R 對 .app 會壞掉符號連結與延伸屬性（也就壞掉簽章）；ditto 不會。
    ditto "$APP_BUNDLE" "$dmg_stage/$APP_NAME" || die "ditto 複製 .app 失敗"
    # 拖放安裝的另一半：視窗裡要有 /Applications 的捷徑，不然使用者只會
    # 直接在 DMG 裡點開來跑，然後每次重開機都找不到程式。
    ln -s /Applications "$dmg_stage/Applications"

    rm -f "$DMG_PATH"
    hdiutil create -volname "Fake Trial Poke" -srcfolder "$dmg_stage" \
        -ov -format UDZO -quiet "$DMG_PATH" \
        && pass "已產生 $DMG_PATH" || fail "hdiutil 產生 DMG 失敗"
    rm -rf "$dmg_stage"

    if [ -n "$SIGN_IDENTITY" ] && [ -f "$DMG_PATH" ]; then
        codesign --force --timestamp --sign "$SIGN_IDENTITY" "$DMG_PATH" \
            && pass "DMG 已簽章" || fail "DMG codesign 失敗"

        if [ "$SKIP_NOTARIZE" -eq 1 ]; then
            waive "DMG 未公證"
        else
            # .app 內的票證管不到外層 DMG：使用者下載的是 DMG，Gatekeeper
            # 第一個檢查的也是 DMG，所以它得自己有一張票。
            if xcrun notarytool submit "$DMG_PATH" \
                    --keychain-profile "$NOTARY_PROFILE" --wait; then
                xcrun stapler staple "$DMG_PATH" \
                    && pass "DMG 已公證並 staple" || fail "DMG stapler staple 失敗"
            else
                fail "DMG 公證被退回"
            fi
        fi
    elif [ -f "$DMG_PATH" ]; then
        waive "DMG 未簽章（開發用）"
    fi
fi

# ── 結果 ──────────────────────────────────────────────────────────────
step "結果"
printf '  PASS=%d  FAIL=%d  WAIVED=%d\n' "$PASS" "$FAIL" "$WAIVED"

if [ "$FAIL" -ne 0 ]; then
    printf '\n未通過的項目：\n'
    printf '  - %s\n' "${FAILURES[@]}"
    die "有閘門未通過"
fi

if [ "$WAIVED" -gt 0 ]; then
    cat <<BANNER

  ⚠️  有 $WAIVED 項閘門被豁免。
      這個 .app 是開發／實測用產物，**不要當授權版發給客戶**。
      正式出貨請帶 --sign-identity 與 --notarize-profile 重跑。
BANNER
else
    printf '\n  全部閘門通過。%s 可以出貨。\n' "$APP_NAME"
fi
printf '\n  產物：%s\n\n' "$APP_BUNDLE"
