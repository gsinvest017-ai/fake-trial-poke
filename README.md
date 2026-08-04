# fake-trial-poke

假試撮盤前監控工具。原始碼與操作說明位於
[`系統檔案/README.md`](系統檔案/README.md)。

正式出貨對象是 Windows；macOS 與 Linux 可跑原始碼模式，狀態與限制見
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
| 打包成 app | spec 已備妥 | 未支援 | `fake-trial-poke-macos.spec`，但產線與出貨閘門仍是 Windows-only（見下） |
| 安裝程式 | 未支援 | 未支援 | Inno Setup 只有 Windows |

**尚未在實機驗證。** 以上 macOS 路徑是照著 Windows 版逐項對應寫出來的，
在這台 Windows 開發機上只能驗證到「語法正確、Windows 行為不變、跨平台
靜態檢查通過」。第一次在 Mac 上跑請預期要修一些東西。

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

### macOS 打包（未完成的部分）

`fake-trial-poke-macos.spec` 可以產出 `.app`：

```bash
./系統檔案/.venv/bin/python -m PyInstaller fake-trial-poke-macos.spec --clean
```

需要圖示時先產生 `static/gs-icon.icns`（沒有的話 spec 會自動略過圖示）：

```bash
mkdir -p gs-icon.iconset && sips -z 512 512 static/gs-icon.png \
  --out gs-icon.iconset/icon_512x512.png && iconutil -c icns gs-icon.iconset \
  -o static/gs-icon.icns
```

打包相依（pywebview / pyobjc；tkinter 需另裝）見
[`系統檔案/requirements-macos.txt`](系統檔案/requirements-macos.txt)。

但**還不足以出貨**，缺的是閘門而不是產物：

- `pack.config.ps1` 的兩道閘門跑在 PowerShell + Inno Setup 的 Windows 產線
  （gs-app-pack）上，macOS 沒有對應物。
- `keyguard.refusalcheck` 在非 Windows 直接回報 SKIP，`packagecheck` 的視窗
  檢查同理。[`tools/verify-refusal-macos.sh`](tools/verify-refusal-macos.sh)
  補了同一件事（暫時 APPDATA → 推過到期日 → 斷言視窗在、PORT 沒開），
  但它是手動執行，不像 Windows 那樣是 build 過不了就不給出貨的閘門。
- 未處理簽章與公證（codesign / notarytool）。沒公證的 .app 在對方機器上會
  被 Gatekeeper 擋下，那個失敗看起來會跟授權拒絕一模一樣。

在把拒絕驗證接進產線之前，`.app` 應視為開發／實測用產物，不要當授權版
發給客戶。

### 撤銷／復權實測

要在 Mac 上驗證 admin console 的撤銷與復權會不會讓拒絕視窗出現／消失，
完整的逐步操作指示在
[`docs/macos-revocation-test.md`](docs/macos-revocation-test.md)。
