# fake-trial-poke

Windows 假試撮盤前監控工具。原始碼與操作說明位於
[`系統檔案/README.md`](系統檔案/README.md)。

## 授權版 release

正式 release 透過 KEYGUARD 驗證 `FAKE_TRIAL_POKE` licence：

- Python 原始碼開發模式只回報授權狀態，不阻擋開發。
- PyInstaller frozen release 會在開啟 port、登入 Shioaji、啟動背景工作前
  強制驗證。
- licence 的 email、plan、席次與 expiration 都在 Ed25519 簽章載荷內；
  手改本機狀態檔不能延長期限。
- 未啟用時只有 Keyguard 的 14 天有界 trial；trial 或正式 licence 到期後，
  release 會拒絕啟動，並跳出一個說明狀態、machine id 與啟用指令的視窗。
- 打包流程必須通過 `keyguard.packagecheck` 與 HTTP 啟動 smoke test，
  否則不得產生 release。閘門包含 `--require-console-output`（啟用失敗在真實
  終端機看得見）與 `--require-window`（過期會跳視窗，不是裸 MessageBox）。

### 驗證「憑證失效會被擋住」

不要用手動開一次來確認——少了 tkinter 時，那個失敗看起來跟「應用程式就是
打不開」一模一樣。

```powershell
python C:\Users\User\KEYGUARD\scripts\demo_expired_refusal.py `
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
