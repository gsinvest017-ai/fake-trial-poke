# 假試撮：盤前試撮鎖漲停偵測器

這套工具會在盤前把永豐 Shioaji 的「試撮成交價」和「買賣五檔」即時錄下來，再找出曾經試撮鎖漲停、但在開盤前撤掉大買單或開盤沒有守住漲停的股票。

它是市場鑑識工具，不是下單程式。程式不啟用 CA、不選交易帳號、不送單，也不會把 API key、帳號或 `person_id` 寫進輸出。

## 先用白話理解

「假試撮拉漲停」是指：盤前試撮時，看起來有大量買單把價格推到漲停，讓市場以為很強；但接近 09:00 時大單突然消失，或正式開盤價明顯掉離漲停。這不等於已證明操縱，只是值得人工複核的異常跡象。

四種狀態：

- `疑似假試撮`：盤前曾鎖漲停，而且開盤沒守住或買一大單在開盤前驟撤。
- `鎖漲停守住`：盤前曾鎖漲停，開盤仍守在漲停，也沒有符合門檻的驟撤。
- `曾觸漲停`：碰過漲停，但沒有形成完整鎖定證據或無法確認正式開盤。
- `未觸及`：本次錄到的試撮資料沒有碰到漲停。

## 每天會怎麼跑

```text
08:25 Windows 排程啟動
  → recorder.py 於 08:30–09:00 錄 Tick + BidAsk
  → scanner.py 計算每檔狀態與證據
  → generate_dashboard.py 產生單檔、離線 dashboard.html
```

正常盤前執行：

```powershell
python run_session.py --session preopen
```

完全不登入、先驗證整條串接：

```powershell
python run_session.py --session preopen --sample
```

收盤前試撮：

```powershell
python run_session.py --session preclose
```

## 四支程式

### `recorder.py`

```powershell
python recorder.py --start 08:30 --end 09:00
python recorder.py --smoke 15
python recorder.py --start 08:30 --end 09:00 --out data/auction_20260724.jsonl
python recorder.py --smoke 15 --universe 2330,2317,2454
```

- 沒給 `--start/--end` 時，13:20 前預設錄 13:25–13:30；13:20 後預設下一日 08:30–09:00。
- 可提前啟動，程式會等到時窗前再訂閱。
- 預設 universe 是股期對應現貨；也可傳逗號清單或 UTF-8 txt/JSON。
- Tick 與 BidAsk 都要訂，所以一檔股票會占兩條串流。
- 撞到 Shioaji 配額時，會保留實際成功數、上限單位與完整丟棄清單，絕不靜默截斷。
- 2026-07-23 的 15 秒 smoke 實測：第 255 條串流回報配額，因此上限為 254 條串流，也就是 127 檔完整 Tick+BidAsk；268 檔 universe 中有 141 檔被明確列入 dropped。

### `scanner.py`

```powershell
python scanner.py --in data/auction_20260724.jsonl --out data/result_20260724.json
python scanner.py --sample
```

預設判定：

- 試撮鎖漲停：`tick.simtrade == true` 且 `chg_type == 1` 或 `price == limit_up`；或試撮五檔的買一價等於漲停。
- 開盤價：09:00 後第一筆 `simtrade == false` 的真實 Tick；若缺少，再讀同名 `.snapshot.json`。
- 大單驟撤：開盤前 300 秒內，買一峰值至少 1,000 張、絕對減少至少 500 張，而且相對峰值減少至少 70%。
- `open_gap_pct = (open_price / limit_up - 1) × 100`。小於 0 代表正式開盤沒有守住盤前漲停。

撤單門檻可調：

```powershell
python scanner.py --in data/auction_20260724.jsonl `
  --drop-ratio 0.70 `
  --drop-min-peak 1000 `
  --drop-min-absolute 500 `
  --drop-lookback-sec 300
```

09:00 的真實 Tick 只做事後標記，不會倒灌成盤前訊號。

### `generate_dashboard.py`

```powershell
python generate_dashboard.py --in data/result_sample.json --out dashboard.html
```

產物是自包含 HTML：資料、CSS、JavaScript、SVG 圖表都內嵌，沒有 CDN、外部字型或外部網址。直接雙擊 `dashboard.html` 就能離線打開。

畫面包含白話說明、當日 KPI、狀態分佈、可排序清單、全體開盤落差，以及每檔可展開的試撮價與買一堆量回放圖。狀態同時使用圖示與文字，不只靠顏色。

### `run_session.py`

依序執行 recorder → scanner → dashboard，任何一步非零結束就停止並回報。`--sample` 會跳過登入及即時錄製，適合安裝後或排程動作驗證。

## Windows 每早自動跑

用 PowerShell 在專案目錄執行：

```powershell
.\schedule_morning.ps1 -Mode Register
```

一鍵註冊並立即用 `schtasks /Run` 驗證動作本體：

```powershell
.\schedule_morning.ps1 -Mode Register -RunNow
```

排程為週一至週五 08:25。立即驗證若不在盤前啟動區間，會自動跑完全離線 sample，避免誤等到下一個交易日；正式在 08:25 觸發時會執行：

```powershell
python run_session.py --session preopen
```

查詢或再次觸發：

```powershell
.\schedule_morning.ps1 -Mode Status
.\schedule_morning.ps1 -Mode Run
```

若要移除排程：

```powershell
.\schedule_morning.ps1 -Mode Unregister
```

腳本所有路徑都從 `$PSScriptRoot` 推導，不依賴啟動時的工作目錄。

## 資料契約

原始串流：`data/auction_YYYYMMDD.jsonl`，每行一筆 UTF-8 JSON：

```json
{"code":"2330","name":"台積電","ts":"2026-07-24T08:59:50","kind":"tick","simtrade":true,"price":1235.0,"chg_type":1,"bid_price":[1235.0,null,null,null,null],"bid_volume":[8200,null,null,null,null],"ask_price":[null,null,null,null,null],"ask_volume":[null,null,null,null,null],"volume":10}
```

同名 `auction_YYYYMMDD.meta.json` 保存：

- 日期、session、時窗、universe 大小、成功訂閱檔數。
- `sub_limit`（串流數）、`sub_limit_stocks`（可完整訂 Tick+BidAsk 的股票數）。
- 被配額排除的代碼與每檔 `reference/limit_up`。

掃描結果：`data/result_YYYYMMDD.json`：

```json
{
  "date": "2026-07-24",
  "session": "preopen",
  "window": {"start": "08:30", "end": "09:00"},
  "universe_size": 268,
  "subscribed": 127,
  "sub_limit": 254,
  "generated_at": "2026-07-24T09:00:05+08:00",
  "stocks": [
    {
      "code": "2330",
      "name": "台積電",
      "reference": 1125.0,
      "limit_up": 1235.0,
      "locked_limit_up": true,
      "first_lock_time": "2026-07-24T08:42:10",
      "last_lock_time": "2026-07-24T08:59:55",
      "lock_duration_sec": 1065,
      "sim_high": 1235.0,
      "max_bid0_volume": 8200,
      "chg_type_hit_1": true,
      "open_price": 1200.0,
      "open_gap_pct": -2.834,
      "bid0_dropped": true,
      "status": "suspected_fake",
      "status_label": "疑似假試撮",
      "sim_price_series": [{"t": "2026-07-24T08:42:10", "price": 1235.0, "simtrade": true}],
      "bid0_series": [{"t": "2026-07-24T08:59:55", "bid0_price": 1235.0, "bid0_volume": 200}]
    }
  ]
}
```

## 已知限制

- Shioaji 沒有可回補的盤前試撮歷史 API；今天沒即時錄到，之後就無法完整還原。
- 訂閱額度是帳戶/環境實測值，可能隨供應商調整；每次都以 meta 與 smoke log 的真實數字為準。
- 若 09:00 真實 Tick 沒落在錄製尾端、也沒有 snapshot，`open_price` 會是 `null`，狀態會保守標成「曾觸漲停」，不會硬猜。
- 「疑似假試撮」是異常偵測標籤，不是違法認定，也不是買賣建議。
- 週一至週五排程不會自動辨識台股休市日；休市時 recorder 會因沒有正常 callback 而留下失敗記錄。
- 本機時間必須正確。Shioaji 的 Tick 奈秒值依已驗證公式轉換，不再額外加 8 小時。
