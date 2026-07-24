# 資料索引（一目了然）

> 盤前試撮鎖漲停 / 假試撮偵測器 — 歷史錄製資料總覽
> 最後整理：2026-07-24

---

## 📁 history/ — 各交易日錄製資料（一天一個資料夾，資料夾名＝日期）

### ⭐ history/20260724/ — 2026-07-24（週五）｜首次「真實盤前試撮」錄製【重點】
| 檔案 | 內容 |
|---|---|
| `auction_20260724.jsonl` | **盤前試撮 08:39–09:00 逐筆五檔**（254 檔股期標的，simtrade=True）＋ 09:00:05 開盤 snapshot |
| `auction_20260724.meta.json` | 當日 meta：每檔前收 / 漲停 / 跌停 / 訂閱清單 |
| `auction_20260724_postopen.jsonl` | **開盤後 09:03–09:05 續錄**（看崩盤/撤單有沒有回漲停） |
| `auction_20260724_postopen.meta.json` | 盤後 meta |
| `result_20260724.json` | **判定結果：3 檔疑似假試撮 → 8039 台虹 / 2392 正崴 / 2201 裕隆** |

### history/20260723/ — 2026-07-23｜收盤試撮「系統彩排」（測試用，非真盤前）
| 檔案 | 內容 |
|---|---|
| `auction_20260723.jsonl` / `.meta.json` | 收盤試撮 13:25–13:30 錄製（用來測管線） |
| `result_20260723.json` | 該次判定（3 檔曾鎖漲停） |

---

## 📁 analysis/ — 2026-07-24 重點股票深入分析
- 6 檔（假試撮 8039/2392/2201 ＋ 異常 6488環球晶/2481強茂/6147頎邦）的日K均線、量、籌碼、**融資維持率（CMoney 遞迴法）**
- 產出報告：`../analysis_report.html`

## 📁 _archive/ — 開發過程的 smoke / sample / 中間版檔（可安全刪除）

## result_sample.json — 合成範例資料（供 `--sample` 展示用，非真實盤面）

---

## 自動落地慣例

```text
data/
├─ INDEX.md
├─ history/
│  └─ YYYYMMDD/
│     ├─ auction_YYYYMMDD.jsonl
│     ├─ auction_YYYYMMDD.meta.json
│     ├─ auction_YYYYMMDD_postopen.jsonl
│     ├─ auction_YYYYMMDD_postopen.meta.json
│     └─ result_YYYYMMDD.json
├─ analysis/
├─ _archive/
└─ result_sample.json
```

- `recorder.py` 正式錄製與 `service.py` live 錄製都會自動建立當日 `history/YYYYMMDD/`。
- `scanner.py` 不給 `--in` 時讀取今日檔；不給 `--out` 時依資料日期將結果寫入對應日期夾。
- `service.py --replay PATH` 永遠使用明確指定的既有路徑，不會自行改寫來源。
- `recorder.py --smoke` 與 scanner 的 `--sample` 是測試資料，不會混入每日歷史錄製。

**今日（2026-07-24）抓取／錄製的真實資料已清楚收在 `history/20260724/`；未來每日新資料也會自動遵循相同慣例，不再散落於 `data/` 根目錄。**
