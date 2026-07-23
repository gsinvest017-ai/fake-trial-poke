# Shioaji 盤前試撮偵測器：接手人安裝手冊

本專案的排程器是資料夾內的 `scheduler.py` 常駐程式，不會向 Windows 工作排程器註冊任務。整個資料夾可複製到另一台 Windows 電腦；接手人在新位置執行 `setup.bat` 後即可使用。

正式流程會在週一至週五台北時間 08:25 呼叫：

```text
run_session.py --session preopen
  ├─ recorder.py
  ├─ scanner.py
  └─ generate_dashboard.py
```

## 1. 系統需求

- Windows 10 或 Windows 11，64 位元。
- 64 位元 Python 3.12。此版本已在 Windows 11、Python 3.12.10 實測。
- 第一次安裝相依套件時需要網路。
- 可使用 Shioaji API 的永豐金證券帳號與該使用者自己的 API key／secret key。

不需系統管理員權限。`scheduler.py`、`.venv`、狀態檔、log 與輸出資料都位於本專案資料夾內。

## 2. 一鍵初始化

1. 將整個專案資料夾複製到接手人的電腦。
2. 雙擊 `setup.bat`。
3. `setup.bat` 會在本資料夾建立 `.venv`，再執行 `pip install -r requirements.txt`。
4. 若尚無 `.env`，它會從 `.env.example` 複製一份；若已有 `.env` 則不會覆寫。

若畫面顯示找不到 Python，請先安裝 64 位元 Python 3.12，安裝時建議勾選「Add Python to PATH」，再重新執行 `setup.bat`。

## 3. 填入接手人的 Shioaji 金鑰

用文字編輯器開啟專案根目錄的 `.env`，填入接手人自己的資料：

```dotenv
SHIOAJI_API_KEY=接手人自己的_api_key
SHIOAJI_SECRET_KEY=接手人自己的_secret_key
```

不要加多餘空格，也不要把 `.env` 上傳到 Git、寄到群組或貼到問題回報。專案的 `.gitignore` 已排除 `.env`，但仍應由人員確認交付內容。

## 金鑰安全警告：交付前必讀

**把資料夾交給別人以前，務必刪除自己的 `.env`；該檔含真實金鑰。**

交付包只能保留不含金鑰的 `.env.example`。接手人應從 `.env.example` 建立新的 `.env`，並填入接手人自己的 Shioaji 金鑰。不要沿用、複製或傳送原持有人的金鑰。

## 4. 先做離線驗證

完成初始化後，可在專案資料夾開啟 PowerShell：

```powershell
.\.venv\Scripts\python.exe scheduler.py --dry
```

此命令只印出下一次平日觸發時間，不會登入 Shioaji。

接著執行：

```powershell
.\.venv\Scripts\python.exe scheduler.py --once
```

`--once` 會立即呼叫 `run_session.py --session preopen --sample`，完成 scanner 與 dashboard 的離線串接驗證，不登入 Shioaji，也不改寫每日正式觸發狀態。驗證用 dashboard 會放在 `log/scheduler_once_dashboard.html`。

## 5. 啟動資料夾內常駐排程

雙擊 `start.bat`。它會：

1. 優先使用本資料夾的 `.venv\Scripts\python.exe`。
2. 若 `.venv` 不存在，回退到系統的 `python`。
3. 啟動本資料夾內的 `scheduler.py` 並保持常駐。

命令視窗必須保持開啟；關閉視窗或關機後，常駐排程就會停止。重新開機後可再次雙擊 `start.bat`，或使用下一節的選用自動啟動。

預設排程：

- 時區：Asia/Taipei（UTC+8）。
- 日期：週一至週五。
- 時間：08:25。
- session：`preopen`。
- 08:25 後才啟動時，預設可在 09:00 前補觸發；09:00 後會等下一個平日。
- 每日只嘗試一次，記錄於 `.scheduler_state.json`。
- `.scheduler.lock` 會防止同一資料夾同時啟動兩個常駐排程器。
- 例外與執行結果寫入 `log/scheduler.log`；子程序失敗後排程器仍會繼續常駐。

臨時調整時間可由命令列傳入：

```powershell
.\start.bat --at 08:20 --session preopen
```

也可調整 `scheduler.py` 開頭的 `DEFAULT_TRIGGER_AT` 與 `DEFAULT_SESSION` 常數。若誤開第二個 `start.bat`，單一實例鎖會拒絕第二個常駐程序。

> 此排程只判斷週一至週五，不含台灣證券交易所休市日或補班／補交易日行事曆。

## 6. 開機登入後自動啟動（選用）

雙擊 `install_autostart.bat`，它會在「目前使用者」的 Windows 啟動資料夾建立 `Shioaji Preopen Scheduler.lnk`，捷徑指向本資料夾內的 `start.bat`。此動作不需要系統管理員權限。

這是整套可攜流程中唯一會在專案資料夾以外建立內容、也就是唯一會碰到「這台電腦」的步驟。排程邏輯、Python、狀態與 log 仍全部留在專案資料夾；捷徑只負責登入後啟動它。

移除自動啟動時雙擊：

```text
uninstall_autostart.bat
```

移除程式只會刪除上述捷徑，不會刪除專案或資料。若專案資料夾改名或搬家，請先執行 `uninstall_autostart.bat`，搬移後再從新位置執行 `install_autostart.bat`。

## 7. 每日產出與檔案位置

- `data/auction_YYYYMMDD.jsonl`：盤前試撮原始事件。
- `data/auction_YYYYMMDD.meta.json`：錄製 metadata。
- `data/result_YYYYMMDD.json`：scanner 分析結果。
- `dashboard.html`：最新的自包含離線 dashboard，可直接以瀏覽器開啟。
- `log/`：recorder 與 scheduler 執行紀錄。
- `.scheduler_state.json`：當日是否已觸發的資料夾內狀態檔。
- `.scheduler.lock`：防止重複開啟常駐排程的資料夾內鎖檔。

`data/`、`log/`、`.venv/`、`.env`、`.scheduler_state.json` 與 `.scheduler.lock` 都不會由 Git 追蹤。若需要把每日資料一併交接，請另外確認資料保存與個資／機敏資訊政策。

## 8. 可攜性與舊排程說明

- 所有執行路徑都由 `__file__`、`%~dp0` 或 `$PSScriptRoot` 推導，沒有寫死某位使用者的磁碟路徑。
- 複製資料夾後，建議在新電腦重新執行 `setup.bat`，不要直接搬用舊電腦建立的 `.venv`。
- 新流程不需要執行 `schedule_morning.ps1`；該檔是舊版 Windows 工作排程器流程，保留僅供既有環境辨識。新接手人應使用 `start.bat`／`scheduler.py`。
- 若原電腦曾用舊版 `schedule_morning.ps1` 註冊工作排程，應在原電腦另行移除舊任務，避免與新常駐排程重複執行。

## 9. 常見問題

### `scheduler.py --once` 成功，但正式執行登入失敗

`--once` 是離線 sample，不會驗證真實 Shioaji 金鑰。請檢查 `.env` 是否填入接手人自己的有效 key 與 secret，且檔名確實為 `.env`。

### 雙擊 `start.bat` 後立刻關閉

先執行 `setup.bat`，再於 PowerShell 執行以下命令查看錯誤：

```powershell
.\.venv\Scripts\python.exe scheduler.py --dry
```

### 今天不想執行

在 08:25 前關閉常駐視窗即可。再次啟動後，只要仍在當日補觸發時窗內且 `.scheduler_state.json` 尚未記錄當日，就會執行一次。
