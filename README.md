# Shioaji 盤前試撮即時偵測器服務

本專案是常駐在本機的即時偵測器服務，不是產生靜態報告的批次工具。服務啟動後會先提供本機網頁並保持待命；到了預設的盤前試撮窗口 08:30–09:00，會自動接上 Shioaji 行情、即時更新偵測狀態。

預設網頁位址：

```text
http://127.0.0.1:8900/
```

服務只做行情唯讀登入，不啟用憑證、不選交易帳號，也不下單。

## 1. 系統需求

- Windows 10 或 Windows 11，64 位元。
- 64 位元 Python 3.12。
- 第一次安裝 Python 套件時需要網路。
- 可使用 Shioaji API 的永豐金證券帳號，以及使用者自己的 API key／secret key。

整個專案可以複製到其他資料夾或電腦。請勿直接搬用別台電腦建立的 `.venv`；在新位置重新執行 `setup.bat` 即可。

## 2. 初始化與填寫金鑰

1. 將整個專案資料夾複製到接手人的電腦。
2. 雙擊 `setup.bat`。
3. 安裝程式會在專案資料夾建立 `.venv`，並安裝 `requirements.txt`。
4. 若專案內尚無 `.env`，安裝程式會從 `.env.example` 建立一份。
5. 用文字編輯器開啟 `.env`，填入接手人自己的資料：

```dotenv
SHIOAJI_API_KEY=接手人自己的_api_key
SHIOAJI_SECRET_KEY=接手人自己的_secret_key
```

不要在等號前後加入多餘空格。若安裝時顯示找不到 Python，請先安裝 64 位元 Python 3.12，並建議在安裝畫面勾選「Add Python to PATH」。

## 3. 啟動即時服務

填妥 `.env` 後，雙擊 `start.bat`。它會：

1. 優先使用本資料夾的 `.venv\Scripts\python.exe`。
2. 若 `.venv` 不存在，回退到系統的 `python`。
3. 以 live 模式啟動 `service.py`。
4. 在畫面提示以瀏覽器開啟 `http://127.0.0.1:8900/`。

命令視窗必須保持開啟。關閉視窗或按 `Ctrl+C` 會停止服務。

服務一啟動就會提供網頁；未進入試撮窗口時顯示待命狀態。預設盤前試撮窗口為台北時間 08:30–09:00，服務會自動準備行情連線並在窗口內即時偵測，09:00 後以 snapshot 收口判定，再由同一個登入、訂閱與程序續錄到約 09:05，之後繼續待命下一個窗口。

> 目前的日期判斷以平日為主，不包含台灣證券交易所休市日或補交易日行事曆。

### Live 自動錄製與誠實狀態

Live 模式預設由同一個 `service.py` 同時提供 PORT 8900 UI 與每日完整落地。日期資料夾不存在時會自動建立，產物為：

- `data\history\YYYYMMDD\auction_YYYYMMDD.jsonl` 與 `.meta.json`：08:30–09:00 盤前 BidAsk，加上 09:00 後全宇宙 snapshot。
- `data\history\YYYYMMDD\auction_YYYYMMDD_postopen.jsonl` 與 `.meta.json`：09:00–約 09:05 的 BidAsk 續錄，加上 09:05 後全宇宙 snapshot。
- `data\history\YYYYMMDD\result_YYYYMMDD.json`：直接將既有 `AuctionDetector` 在開盤 snapshot 後的判定原子落地；不另改判定邏輯。

主檔與續錄採 append，服務若在窗口內重啟會保留先前已寫事件；meta 與 result 在收口時更新。這些檔案可供後續 replay、掃描與分析，不會因即時窗口結束而遺失。

`recorder.py` 只保留為獨立診斷工具，不是正式排程入口；它的 `--smoke`
可驗證登入、訂閱、callback、snapshot 與清理。正式每日錄製一律由
`service.py` 協調 UI 與上述三組日檔。

如明確不需要落地，可用 `--no-record` 關閉：

```powershell
python service.py --no-record
```

Replay 模式預設只讀取既有檔案，不會再次寫入或覆蓋錄製檔。只有明確提供 `--record-out` 才會把 replay 事件另存到指定路徑；例如可寫入系統暫存目錄，再交給 scanner 驗證：

```powershell
python service.py --replay data\history\YYYYMMDD\auction_YYYYMMDD.jsonl --speed 50 --record-out "$env:TEMP\live_rec.jsonl" --result-out "$env:TEMP\live_rec_detector_result.json"
python scanner.py --in "$env:TEMP\live_rec.jsonl" --out "$env:TEMP\live_rec_scanner_result.json"
```

`scanner.py` 未提供 `--in` 時，預設讀取今日的 `data\history\YYYYMMDD\auction_YYYYMMDD.jsonl`；未提供 `--out` 時，會依輸入資料的日期將結果寫成 `data\history\YYYYMMDD\result_YYYYMMDD.json`。因此掃描指定歷史檔時可只寫：

```powershell
python scanner.py --in data\history\20260724\auction_20260724.jsonl
```

完成 replay 後可按 `Ctrl+C` 停止服務。要在不登入 Shioaji、也不碰 `data\` 的情況下自動驗證錄製 schema、scanner/replay 相容性與狀態契約，也可直接執行：

```powershell
python tests\test_service_record.py
```

網頁與 `/api/state` 顯示的是實際連線與資料流狀況，不是固定文案：

- `live`：登入成功、實際訂閱完成，而且窗口內持續收到真實市場事件。
- `degraded`：已登入並訂閱，但窗口內超過容許秒數沒有新事件，代表資料可能停流；預設門檻為 10 秒，可用 `--stale-after-sec` 調整。
- `error`：登入失敗或實際訂閱未達應訂閱檔數。

`/api/state` 同時提供 `recording`、`record_path`、`record_count`、`last_event_age_sec`、`login_ok`、`subscribe_ok`，可直接確認是否正在錄製、已落地筆數、最後事件距今秒數，以及登入／訂閱是否成功。窗口外仍可能顯示 `idle`、`armed`、`closed`；Replay 則顯示 `replay`。

## 4. Replay 離線測試

Replay 模式會讀取既有的試撮 JSONL，不登入 Shioaji。請在專案資料夾開啟 PowerShell，執行：

```powershell
.\.venv\Scripts\python.exe service.py --replay data\history\YYYYMMDD\auction_YYYYMMDD.jsonl --speed 20
```

將 `YYYYMMDD` 換成實際檔案日期，`--speed` 是重播加速倍率。Replay 啟動後同樣用瀏覽器開啟：

```text
http://127.0.0.1:8900/
```

若不使用專案虛擬環境，也可改用：

```powershell
python service.py --replay data\history\YYYYMMDD\auction_YYYYMMDD.jsonl --speed 20
```

## 5. 開機登入後自動啟動（選用）

雙擊 `install_autostart.bat`，會在目前使用者的 Windows「啟動」資料夾建立捷徑，登入後自動呼叫本專案的 `start.bat`。此動作不需要系統管理員權限。

若專案資料夾改名或搬家，請先雙擊 `uninstall_autostart.bat` 移除舊捷徑，搬移完成後再從新位置執行 `install_autostart.bat`。

移除自動啟動只會刪除捷徑，不會刪除專案、資料或金鑰檔。

## 6. 盤前排程與定時看門狗

Windows 排程任務 `假試撮盤前監控` 是唯一正式主入口：平日
08:25 呼叫 `schedule_morning.ps1 -Mode Execute -Port 8900`。腳本只負責
啟動並健康檢查 `service.py`；同一個 service 程序會保持常駐，同時在
8900 提供 UI，並產生上述盤前、開盤後續錄與 detector result。若健康的
service 已存在，Action 會直接沿用，不會重複啟動。重新註冊或查詢可使用：

```powershell
.\schedule_morning.ps1 -Mode Register
.\schedule_morning.ps1 -Mode Status
```

註冊流程會明確允許使用電池時啟動、不因切換到電池而停止、喚醒電腦
執行，並在錯過 08:25 後儘快補啟動。Windows 使用者仍須保持登入
（鎖定畫面可以），因為本機 UI 任務採目前使用者的互動式登入模式。

在非盤前時段手動觸發同一 Action，service 會先提供 UI 並待命到下一個
平日窗口；Action 在 `/api/state` 健康後即以 0 結束，service 程序不會
跟著結束。驗收時可暫時用 `-Port 8901` 註冊與實跑，確認後再以預設
8900 重新註冊。

### 選用的 UI 服務看門狗

`scheduler.py` 不是正式主入口，也不應與 Windows 排程同時常駐。它只保留
為沒有 Task Scheduler 時的手動看門狗備援：平日設定時間檢查本機 PORT
8900，未監聽才啟動同一個 `service.py`。正式環境請使用
`schedule_morning.ps1` 註冊的 Windows 排程。

查看下一次檢查時點，不啟動服務：

```powershell
.\.venv\Scripts\python.exe scheduler.py --dry
```

立即確保服務啟動一次：

```powershell
.\.venv\Scripts\python.exe scheduler.py --once
```

讓看門狗保持常駐，預設於平日 08:20 檢查：

```powershell
.\.venv\Scripts\python.exe scheduler.py
```

需要調整檢查時間時可使用：

```powershell
.\.venv\Scripts\python.exe scheduler.py --at 08:15
```

## 7. 金鑰安全

`.env` 內含真實 Shioaji 金鑰，不可上傳到 Git、寄到群組、貼到問題回報或放進交付包。

**把資料夾交給別人以前，務必刪除自己的 `.env`。**

交付包只保留不含金鑰的 `.env.example`。接手人應執行 `setup.bat` 建立新的 `.env`，並填入自己的金鑰；不要沿用、複製或傳送原持有人的金鑰。

## 8. 常見問題

### 瀏覽器無法開啟

先確認啟動服務的命令視窗仍在執行，並查看畫面是否出現 `HTTP 啟動 FAILED`。PORT 8900 若已被其他程式占用，服務會無法啟動。

### Live 模式登入失敗

確認 `.env` 位於專案根目錄，變數名稱正確，且填入的是目前使用者自己的有效 API key 與 secret key。Replay 成功不代表真實金鑰一定有效。

### 如何直接驗證偵測器單元測試

在專案根目錄執行：

```powershell
python tests\test_detector.py
```

此測試不需設定 `PYTHONPATH`，也不會登入 Shioaji。
