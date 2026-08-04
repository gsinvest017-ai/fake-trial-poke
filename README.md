# fake-trial-poke

假試撮盤前監控工具。原始碼與操作說明位於
[`系統檔案/README.md`](系統檔案/README.md)。

正式出貨對象是 Windows；macOS 另有一組對應的原始碼模式、排程與打包產線，
Linux 只跑原始碼模式。狀態與限制見
[macOS / Linux 支援](#macos--linux-支援)。

## 授權版 release

正式 release 透過 KEYGUARD 驗證 `FAKE_TRIAL_POKE` licence：

- Python 原始碼開發模式只回報授權狀態，不阻擋開發。
- PyInstaller frozen release 會在開啟 port、登入 Shioaji、啟動背景工作前
  強制驗證。
- licence 的 email、plan、席次與 expiration 都在 Ed25519 簽章載荷內；
  手改本機狀態檔不能延長期限。
- 未啟用時只有 Keyguard 的 14 天有界 trial；trial 或正式 licence 到期後，
  release 會拒絕啟動，並在**應用程式自己的視窗裡**疊上一個說明狀態、machine id
  與啟用指令的面板；真實 UI 在後面可辨識但完全惰性（網路被切斷、標記 `inert`）。
  服務本身沒有啟動——沒有開 port、沒有登入 Shioaji、沒有背景掃描。
- 打包流程必須通過 `keyguard.packagecheck` 與 HTTP 啟動 smoke test，
  否則不得產生 release。閘門包含 `--require-console-output`（啟用失敗在真實
  終端機看得見）與 `--require-window`（過期會跳視窗，不是裸 MessageBox）。

### 自動取得續期

應用程式在每次啟動、**判斷是否放行之前**，會自己找有沒有更新的授權：

- `%APPDATA%\fake-trial-poke\licences\`
- 安裝目錄下的 `licences\`

把廠商寄來的金鑰檔（原信轉寄存檔也可以，會自動從內文找出 `KG1.`）存進其中一個
資料夾，下次啟動就生效——客戶不需要打任何指令。

**只會延長，不會縮短。** 取得的金鑰只有在到期日比目前更晚時才套用；
偽造的、別的 app 的、別人 email 的、綁在別台機器的一律原地拒絕。
網路或檔案有問題就當作沒發生，授權維持原狀。

### 驗證「憑證失效會被擋住」

不要用手動開一次來確認——少了 tkinter 時，那個失敗看起來跟「應用程式就是
打不開」一模一樣。

```powershell
keyguard verify-refusal `
  --exe "$env:LOCALAPPDATA\Fake Trial Poke\fake-trial-poke.exe" `
  --app FAKE_TRIAL_POKE --app-name fake-trial-poke `
  --email-env FAKE_TRIAL_POKE_LICENCE_EMAIL --shot refusal.png
```

它會在一個暫時的 `APPDATA` 裡把授權池推過到期日（**不動你真正的啟用狀態**），
啟動已安裝的 exe，抓到拒絕視窗、截圖，並確認關掉視窗後行程以非零退出。

macOS 上 `verify-refusal` 會直接回報 SKIP，等價物是
[`tools/verify-refusal-macos.sh`](tools/verify-refusal-macos.sh)（`--check-recovery`
會一併驗證續期後服務回得來）。它已經是
[`tools/pack-macos.sh`](tools/pack-macos.sh) 的 Gate 4，正常出貨流程不需要
自己跑：

```bash
./tools/verify-refusal-macos.sh \
  --app "/Applications/Fake Trial Poke.app" \
  --email buyer@example.com --check-recovery
```

### 客戶啟用

在安裝目錄的 PowerShell 執行：

```powershell
.\fake-trial-poke.exe --activate "<KG1 licence key>" `
  --licence-email "buyer@example.com"

.\fake-trial-poke.exe --licence-status
.\fake-trial-poke.exe --machine-id
```

啟用資料寫入使用者自己的 `%APPDATA%`，不會寫進安裝包或 Git。

### 建置

建置環境的 Keyguard 必須是一般安裝，不能是 editable install：

```powershell
Set-Location C:\Users\User\fake-trial-poke
$python = '.\系統檔案\.venv\Scripts\python.exe'

& $python -m pip install pyinstaller pywebview
& $python -m pip uninstall -y keyguard
& $python -m pip install C:\Users\User\KEYGUARD

C:\Users\User\gs-app-pack\pack.ps1 -Clean
```

發布 `v0.1.0`：

```powershell
C:\Users\User\gs-app-pack\pack.ps1 -Tag v0.1.0 -Clean
```

`pack.config.ps1` 內的兩道出貨閘門會確認 Keyguard 確實被凍結進 exe，
且完成的應用程式真的能提供本機 HTTP UI。

### 簽發有期限的 licence

私鑰只能留在廠商端，不能 commit 或附加到 GitHub Release：

```powershell
keyguard issue FAKE_TRIAL_POKE buyer@example.com `
  --key C:\Users\User\.keyguard-vendor\gs_private.key `
  --expires 2026-08-29 --seats 1 --plan TRIAL_RELEASE
```

簽發出的 `KG1...` key 應以私密管道交付，不應當作公開 release asset。

## macOS / Linux 支援

| 層次 | macOS | Linux | 說明 |
| --- | --- | --- | --- |
| 相依套件 | 可 | 可 | `shioaji==1.3.2`、`pysolace==0.9.53` 都有 `macosx_11_0_arm64`／`macosx_10_15_x86_64`／`manylinux` 的 cp312 wheel |
| 原始碼模式 | 可 | 可 | `./install.sh` + `./launch.command` |
| 每日排程 | 可（launchd） | 需自理 | `schedule_morning.sh`；Linux 請自掛 systemd timer / cron |
| 授權閘門 | 可 | 可 | keyguard 的 machine id、拒絕視窗、狀態抓取都是跨平台的 |
| 打包成 app | 可 | 未支援 | `fake-trial-poke-macos.spec` + [`tools/pack-macos.sh`](tools/pack-macos.sh) 產線與四道閘門（見下） |
| 安裝程式 | 可（DMG） | 未支援 | `pack-macos.sh --dmg`；Inno Setup 只有 Windows |

**已在實機驗證（Apple Silicon、macOS 26）。** 原始碼模式、launchd 排程與
`.app` 建置都在 Mac 上實際跑過。當初預期的「第一次在 Mac 上跑要修一些
東西」確實發生，共四處，都已修掉並補上回歸測試：

| 症狀 | 真正的原因 |
| --- | --- |
| 每日 08:25 排程安靜地什麼都沒錄 | launchd 的 PATH 沒有 Homebrew，`resolve_python` 退回系統內建的 Python 3.9，`service.py` 死在 `TypeError`，只留一行在 `log/schedule.log` |
| 中文錯誤訊息變成 `MODE?: unbound variable` | macOS 只有 bash 3.2，UTF-8 locale 下會把 `$VAR` 後面的中文字吃進變數名（C locale 反而正常，所以 launchd 過、手動跑壞） |
| `.app` 起得來但視窗從不出現 | `bottle` / `proxy_tools` 沒被收進包裡——`collect_all()` 不跟進套件自己的第三方相依 |
| `Symbol not found: _X509_STORE_get1_objects` | `pysolace` 的 wheel 自帶 OpenSSL 3.0.8，PyInstaller 依檔名去重時留下它、丟掉 `_ssl` 編譯時對著的 3.6.3 |

尚未驗證的只剩需要 KEYGUARD 與 Apple Developer 憑證的環節：Gate 0 的
keyguard 檢查、Gate 2、Gate 4，以及簽章與公證。那些在有憑證的機器上才跑
得起來。

### macOS 快速開始（原始碼模式）

```bash
cd 系統檔案
chmod +x install.sh launch.command schedule_morning.sh   # 從 Windows 帶過來的 checkout 需要
./install.sh          # 找 Python 3.12 → .venv → 相依套件 → 驗金鑰 → 掛 launchd 排程
./launch.command      # 起服務並開瀏覽器（Finder 雙擊亦可）
./schedule_morning.sh --mode status
./schedule_morning.sh --mode unregister
```

排程限制與 Windows 不同：`schedule_morning.sh` 裝的是**使用者層
LaunchAgent**，08:25 時該 macOS 帳號必須已登入。Windows 那條「開機未登入
也能錄」的 S4U 路徑在 macOS 需要 root 安裝 LaunchDaemon，本專案刻意不做。

### macOS 打包

[`tools/pack-macos.sh`](tools/pack-macos.sh) 是 macOS 的產線，位置等同
Windows 的 `gs-app-pack` + `pack.config.ps1`。它不只產生檔案——它的價值跟
Windows 版一樣在閘門：**任何一道過不了就不會留下可出貨的產物。**

```bash
# 正式出貨
./tools/pack-macos.sh --clean --dmg \
    --sign-identity "Developer ID Application: GS Invest (TEAMID)" \
    --notarize-profile gs-notary \
    --licence-email buyer@example.com

# 開發建置（豁免必須明講，且結果會標記為不可出貨）
./tools/pack-macos.sh --clean --allow-unsigned --skip-notarize \
    --licence-email you@example.com
```

`--notarize-profile` 指的是 notarytool 的 keychain profile，先建一次：

```bash
xcrun notarytool store-credentials gs-notary \
    --apple-id <apple id> --team-id <team id> --password <app 專用密碼>
```

四道閘門，對應 `pack.config.ps1` 的 `$PostBuildCheck`：

| 閘門 | 問的問題 | Windows 對應 |
| --- | --- | --- |
| Gate 0 前置 | keyguard 是非 editable 安裝嗎？`AppGate.refusal_ui()` 是 `window` 嗎？ | `$RequireNonEditable` |
| Gate 1 Gatekeeper | `spctl` 會放行嗎？ | 無（Windows 不需要） |
| Gate 2 凍結 | 做完的 `.app` 裡真的有 keyguard 嗎？ | `keyguard.packagecheck` |
| Gate 3 smoke | 供得出 HTTP UI，而且**持續**供得出嗎？ | `smoke_launch.py` |
| Gate 4 拒絕 | 憑證失效擋得住、而且沒有偷偷開 port 嗎？續期後又回得來嗎？ | `keyguard.refusalcheck` |

兩個刻意的設計：

- **`packagecheck` 的 SKIP 不算通過。** `keyguard.refusalcheck` 與
  `packagecheck` 的視窗檢查在非 Windows 會直接回報 SKIP，所以 Gate 2 與
  Gate 4 另外做行為檢查（問產物 `--licence-status`、跑
  [`tools/verify-refusal-macos.sh`](tools/verify-refusal-macos.sh)）。
  把 SKIP 當成 PASS 的話，這整支腳本就沒有存在意義。
- **先簽章公證，再跑行為閘門。** `codesign` 會改寫 bundle，反過來做的話，
  通過測試的產物跟實際出貨的產物就不是同一個。

圖示由 [`tools/make-icns.py`](tools/make-icns.py) 產生（產線會自動呼叫），
十個尺寸都畫，不需要任何來源圖檔，配色與 Windows 版的 `.ico` 相同：

```bash
python3 tools/make-icns.py          # → static/gs-icon.icns
```

打包相依（pywebview / pyobjc；tkinter 需另裝）見
[`系統檔案/requirements-macos.txt`](系統檔案/requirements-macos.txt)。

**還沒有實際跑過完整產線。** Gate 0 的 keyguard 檢查、Gate 2、Gate 4 需要
建置環境裝好 KEYGUARD，Gate 1 需要 Apple Developer 憑證，這台驗證機兩者
都沒有。已驗證的是：閘門在缺 keyguard 時確實擋下建置（不是放行），`.app`
本身建得出來、跑得起來、持續供得出 HTTP UI。第一次帶著憑證跑完整產線時，
仍請預期 Gate 1 與 Gate 4 要調整。

### 撤銷／復權實測

要在 Mac 上驗證 admin console 的撤銷與復權會不會讓拒絕視窗出現／消失，
完整的逐步操作指示在
[`docs/macos-revocation-test.md`](docs/macos-revocation-test.md)。
