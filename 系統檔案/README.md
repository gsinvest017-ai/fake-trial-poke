# 假試撮盤前監控：接手人操作指南

這是一套可攜式盤前資料錄製系統，正式出貨對象是 Windows。安裝後，使用者
只需雙擊啟動檔，即可在背景啟動服務並開啟當日前端：

```text
http://127.0.0.1:8900/
```

macOS 有一組一一對應的入口（`install.sh` / `launch.command` /
`schedule_morning.sh`），詳見本檔最後的〈macOS 操作路徑〉與根目錄 README
的支援矩陣。以下未特別註明處皆為 Windows 說明。

## 最短操作路徑

### Release 版授權

GitHub Release 的 `fake-trial-poke.exe` 會在啟動服務、開啟本機 port
或登入 Shioaji 前驗證 KEYGUARD licence。第一次收到 licence key 時，
請在安裝目錄的 PowerShell 執行：

```powershell
.\fake-trial-poke.exe --activate "<KG1 licence key>" `
  --licence-email "buyer@example.com"
.\fake-trial-poke.exe --licence-status
```

正式 licence 和未啟用 trial 都有 expiration；到期後 release 不會啟動。
原始碼開發模式不會阻擋，避免開發環境因本機授權狀態中斷。

### 1. 第一次使用：直接一鍵啟動

雙擊上層資料夾的 `一鍵啟動.vbs`。第一次使用時會出現一次 Windows UAC
確認；同意後可把每日排程註冊為 S4U，讓電腦開機但尚未登入時仍可錄製。
若拒絕 UAC，安裝與前端仍會完成，但排程會安全降級為 Interactive，屆時
08:25 必須保持該 Windows 帳號已登入。

安裝程式會依序：

1. 尋找 64 位元 Python 3.12；若沒有，先嘗試用 winget 靜默安裝。
2. winget 不可用或失敗時，改由 python.org 下載官方安裝程式並做
   per-user 靜默安裝。
3. 使用偵測到的絕對 `python.exe` 路徑建立或沿用 `.venv`。
4. 升級 pip 並安裝 `requirements.txt`。
5. 唯讀登入永豐驗證金鑰，成功後立即登出。
6. 註冊 Windows 週一至週五 08:25 自動錄製排程並查詢確認。

安裝可安全重複執行。安裝完成時會明確顯示成功；任何步驟失敗也會顯示
失敗原因與處理方向。首次安裝的終端機視窗全程隱藏，只會短暫顯示一個
會自動關閉、無需點擊的進度提示；完成後會自動開啟前端。詳細安裝輸出
保存在 `log\install.log`，內容不會列印 `.env` 金鑰。

若 winget 與 python.org 下載都失敗（例如沒有網路），會明確提示先安裝
64 位元 Python 3.12，且不會在背景無限等待。需要人工診斷時仍可直接執行
`安裝環境.bat`；英文檔名的相同入口是 `install.bat`。

### 2. 每次要打開前端：一鍵啟動

雙擊 `一鍵啟動.vbs`。英文檔名的相同入口是 `launch.vbs`。

啟動器會：

- 使用本資料夾的 `.venv\Scripts\pythonw.exe`。
- 以背景模式執行 `service.py --host 127.0.0.1 --port 8900`。
- 全程以本資料夾為工作目錄，因此會讀取同資料夾內的 `.env`。
- 等待服務可用後，自動用預設瀏覽器開啟前端。
- 若服務原本已在執行，直接沿用，不會再開第二份。

VBS 是正式的一鍵入口，包含首次安裝、掛排程、隱藏啟動服務與開啟前端，
全程不會出現黑色終端機視窗。重複點擊時會由本機鎖阻止第二份安裝或啟動
流程；鎖會在完成或失敗後清除。`start.bat`、`setup.bat` 保留作為舊版
英文相容入口；日常使用請優先點 VBS。

### 3. 前端錄製控制

前端的「錄製控制」會顯示後端真實狀態，可進行：

- 開啟或關閉「明天起自動錄製」。
- 停止今日正在執行的錄製。
- 在合理錄製時間內立即開始今日錄製。

按下控制後，畫面會重新向 `/api/state` 取得狀態；不是只改畫面文字。

## 每日自動錄製

安裝程序會呼叫：

```powershell
.\schedule_morning.ps1 -Mode Register -Port 8900
```

工作排程名稱為「假試撮盤前監控」，週一至週五 08:25 執行。若第一次
啟動時同意 UAC，排程會優先使用 Windows 工作排程器的 S4U 背景工作階段，
不需使用者登入，也不儲存 Windows 密碼；若未取得管理員權限或 S4U
註冊不可用，會保留既有警告並降級為 Interactive 排程。兩種模式都會
保留每 2 分鐘重複、持續 45 分鐘的設定。排程之後由 PowerShell 以
`Start-Process -WindowStyle Hidden` 啟動服務，因此不會跳出錄製終端機。

重要限制：

- 08:25 時電腦必須開機，或硬體與 Windows 電源設定允許從睡眠喚醒。
- 電腦若已完全關機，工作排程無法自行開機。
- 若錯過時間，排程已設定為電腦恢復可用時補跑；也可雙擊一鍵啟動後，
  在前端按「立即開始」。

手動查詢排程：

```powershell
.\schedule_morning.ps1 -Mode Status
```

如需完整移除自動啟動與每日排程，雙擊 `解除安裝.bat`；亦可只移除每日
排程：

```powershell
.\schedule_morning.ps1 -Mode Unregister
```

## 金鑰與交付安全

本交付資料夾已內建執行所需的 `.env`。安裝器會在套件安裝完成後，於
記憶體中讀取 Shioaji 金鑰做一次唯讀登入驗證，隨即登出；不啟用 CA、
不選帳號且不下單。安裝器與啟動器都不會顯示或複製金鑰內容。

**`.env` 含真實密鑰，不得外流。**

- 不要把整個資料夾傳給未授權的人。
- 不要把 `.env` 貼到聊天、郵件、問題回報或螢幕截圖。
- 不要提交 `.env` 到 Git。
- 問題回報只需提供錯誤訊息；任何密鑰欄位都要遮蔽。

### 開發原型警告

`probes/` 內是開發診斷原型，不是日常操作入口，其中程式會使用 `.env`
實際登入永豐券商。直接執行任何登入原型時，互動終端必須精確輸入
`YES` 才會繼續；從排程、管線或其他非互動環境啟動時會直接中止，不會
登入。除非正在進行受控診斷，請勿執行 `probes/` 內程式。

## 常見問題

### 雙擊一鍵啟動後未開啟前端

先確認 `log\install.log` 沒有失敗訊息，且以下檔案存在：

```text
.venv\Scripts\pythonw.exe
service.py
.env
```

啟動器會等待最多 30 秒。若後端仍未通過
`http://127.0.0.1:8900/api/state` 健康檢查，會顯示不含金鑰的錯誤對話框。

### 8900 埠已被占用

若占用者正是本系統，啟動器會沿用現有服務。若是其他程式，請先關閉該
程式，再重新雙擊 `一鍵啟動.vbs`。

### 安裝一半失敗

不需自行刪除 `.venv`；修正網路問題後直接重跑 `一鍵啟動.vbs` 即可。
若自動安裝 Python 的兩條路徑都失敗，先人工安裝 64 位元 Python 3.12
再重跑；啟動器會重新偵測絕對路徑，不依賴同一個命令視窗的 PATH 更新。

## macOS 操作路徑

三個入口與 Windows 一一對應：

| Windows | macOS | 作用 |
| --- | --- | --- |
| `安裝環境.bat` / `install.bat` | `install.sh` | 建 `.venv`、裝相依、驗金鑰、掛排程 |
| `一鍵啟動.vbs` / `launch.vbs` | `launch.command` | 沿用或啟動服務、等健康檢查、開瀏覽器 |
| `schedule_morning.ps1` | `schedule_morning.sh` | 每日 08:25 排程（launchd） |

```bash
chmod +x install.sh launch.command schedule_morning.sh
./install.sh
./launch.command
```

行為差異（都是平台限制，不是疏漏）：

- **不會自動幫你裝 Python。** Windows 版會走 winget 或 python.org；macOS
  上代替使用者裝 Python 會撞上 Homebrew / pyenv / 系統 Python 的 PATH 與
  權限衝突，失敗方式比「缺 Python」本身更難查。找不到就明講並要你
  `brew install python@3.12`。
- **排程需要登入。** LaunchAgent 只在使用者已登入的 GUI session 生效，
  等同 Windows 未取得 UAC 時降級的 Interactive 排程。Windows 的 S4U
  對應物是 root 安裝的 LaunchDaemon，本專案不做。
- **重試方式不同。** Task Scheduler 的「每 2 分鐘重複、持續 45 分鐘」在
  launchd 沒有等價欄位，改以 08:25–09:09 之間每 2 分鐘一個
  `StartCalendarInterval` 展開；每次觸發都先做健康檢查，服務已在跑就
  直接結束，不會開第二份。
- **排程只認絕對路徑的 Python。** launchd 給的 `PATH` 只有
  `/usr/bin:/bin:/usr/sbin:/sbin`，不含 Homebrew 與 pyenv。所以
  `schedule_morning.sh` 不靠 `command -v` 找直譯器，而是逐一試
  `.venv/bin/python`、`$PYTHON312_EXE`、Homebrew、pyenv、python.org 的
  絕對路徑，且每個都要通過 3.12／64 位元檢查。**刻意不退回
  `/usr/bin/python3`**——那是系統內建的 3.9，跑不動本專案，而它失敗的方式
  是在 `log/schedule.log` 裡留一行 `TypeError` 然後每日錄製安靜地不發生。
  排程沒動作時，第一個該看的就是這個檔。
- `.env` 的保密要求完全相同，見上方〈金鑰與交付安全〉。
