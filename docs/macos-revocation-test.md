# macOS 撤銷／復權實測：逐步操作指示

目標：在一台 macOS 上安裝帶 `gsinvest018@gsinvest.com.tw` 憑證的 release
版，驗證 KEYGUARD admin console 的「撤銷 / 復權」會讓拒絕視窗出現 / 消失。

Windows 上同一件事已做過（見 commit `KG-00023 撤銷實測` / `KG-00023/00024
復權`）。這份是 macOS 的對應流程。

**兩台機器交替進行**，每一步都標了在哪台做：

- 🪟 = 廠商機器（Windows，握有簽章私鑰）
- 🍎 = 受測 Mac

每步都有「預期看到」。**對不上就停下來看該步的排除說明，不要往下做** ——
後面的步驟會把前面的錯誤偽裝成別的症狀。

---

## 開始前：四件會讓實測失真的事

**1. 判定只在啟動當下。** `_refuse_if_unlicensed()` 一輩子只跑一次。撤銷後
**已開著的視窗不會自己變成拒絕畫面**，復權後也不會自己恢復。每次都要
⌘Q 完全結束再重開（關視窗不算）。

**2. CDN 有 5 分鐘快取。** 狀態檔的回應標頭實測是 `Cache-Control:
max-age=300`。push 完馬上重開很可能拿到舊的，看起來像「撤銷沒生效」。
**每次 push 後等 5 分鐘再測。**

**3. 沒啟用就測不到撤銷。** 沒啟用時跑 14 天 trial，此時
`installed_licence_id()` 是空字串，`record_statement()` 直接 return False。
針對某張授權的撤銷對跑 trial 的機器不生效（刻意設計）。所以 STEP 7 不能跳。

**4. 簽發時千萬不要用 `--no-record` 或 `--id`。** `--id` 會隱含
`--no-record`，而沒有記錄進 registry 的授權**永遠無法撤銷也無法續期** ——
整個實測會從第一步就注定失敗，而且失敗方式是「按了撤銷但什麼都沒發生」。

---

## STEP 0 🪟 確認廠商端家當還在

```powershell
keyguard --help                                  # keyguard CLI 可用
Get-ChildItem $HOME\.keyguard-vendor             # 私鑰 + registry
```

**預期看到**：`gs_private.key`（或類似名稱的私鑰）與 registry 檔。

私鑰不見就無法簽發，也無法簽狀態聲明 —— 這個實測做不了，必須先處理金鑰。

---

## STEP 1 🍎 準備 Mac 上的原始碼與環境

```bash
git clone https://github.com/gsinvest017-ai/fake-trial-poke.git
cd fake-trial-poke/系統檔案

# 從 Windows 開發機帶過來的 checkout 沒有執行權限位元
chmod +x install.sh launch.command schedule_morning.sh ../tools/verify-refusal-macos.sh

./install.sh
```

`install.sh` 會找 Python 3.12 → 建 `.venv` → 裝相依 → 驗永豐金鑰 → 掛
launchd 排程。

**預期看到**：`[SUCCESS] 環境就緒。`

**排除**：
- 找不到 Python 3.12 → `brew install python@3.12` 後重跑。
- 卡在「驗證永豐金鑰」→ `.env` 沒放進 `系統檔案/`。從 Windows 那台安全地
  帶過來（不要走聊天軟體 / 郵件）。這一步跟授權無關，但沒過的話 STEP 8
  的正常啟動會失敗，你會分不清是授權還是金鑰的問題。

---

## STEP 2 🍎 裝打包相依

```bash
./.venv/bin/python -m pip install -r requirements-macos.txt

# keyguard 必須是一般安裝，不能是 editable
./.venv/bin/python -m pip uninstall -y keyguard
./.venv/bin/python -m pip install <KEYGUARD 原始碼路徑>
```

**檢查點（很重要，不要跳）**：

```bash
./.venv/bin/python -c "import tkinter; print('tkinter OK')"
./.venv/bin/python -c "
from keyguard.appgate import AppGate
print(AppGate('FAKE_TRIAL_POKE').refusal_ui())"
```

**預期看到**：`tkinter OK` 與 `window`。

**排除**：印出 `text` 就代表**拒絕畫面會完全看不見**（只寫到 stderr，GUI
模式下等於沒有）。整個實測的判準就是那個視窗，這裡不過就沒有意義。
Homebrew 的 Python 預設不含 tkinter：

```bash
brew install python-tk@3.12
```

---

## STEP 3 🍎 建 .app

PyInstaller 不能跨平台編譯，`.app` 只能在 macOS 上產生。

```bash
cd ..                                   # 回 repo 根目錄
./系統檔案/.venv/bin/python -m PyInstaller fake-trial-poke-macos.spec --clean
```

**預期看到**：`dist/Fake Trial Poke.app` 出現。

**排除**：spec 若中止並要你裝 `requirements-macos.txt`，表示 STEP 2 沒做完。

安裝到 Applications 並解除隔離：

```bash
cp -R "dist/Fake Trial Poke.app" /Applications/
xattr -dr com.apple.quarantine "/Applications/Fake Trial Poke.app"
```

> `xattr` 那行是因為 `.app` 未簽章公證。沒做的話雙擊完全沒反應，**症狀跟
> 授權拒絕一模一樣**，會讓你誤判。正式交付客戶才需要 Developer ID 簽章 +
> `notarytool` 公證，這次實測不需要。

---

## STEP 4 🍎 取得這台 Mac 的 machine id

```bash
cd "/Applications/Fake Trial Poke.app/Contents/MacOS"
./fake-trial-poke --machine-id
```

**預期看到**：`MID_` 開頭、共 28 字元，例如 `MID_4bea8d68b39f9d7cdf5cd32e`。

把它抄下來，STEP 5 要用。

**排除**：這行沒有輸出或彈出錯誤 → 回 STEP 3 確認 `xattr` 做了。

---

## STEP 5 🪟 簽發授權

把 `<MID_...>` 換成上一步抄到的值：

```powershell
keyguard issue FAKE_TRIAL_POKE gsinvest018@gsinvest.com.tw `
  --key $HOME\.keyguard-vendor\gs_private.key `
  --expires 2027-08-04 `
  --seats 1 `
  --plan PRO `
  --machine <MID_...>
```

**預期看到**：一長串 `KG1.` 開頭的 licence key。

**注意**：
- `--seats` 預設是 5，這裡明確給 1。
- 在簽發時就 `--machine` 綁定，好處是 STEP 6 發布時 key 本身也會一起被
  放進狀態檔（未綁定的 key 會被 withhold，因為路徑只是 email 的雜湊、不是
  秘密，誰先兌換誰擁有）。撤銷本身不需要綁定，但綁了比較乾淨。
- **不要**加 `--no-record` 或 `--id`。

把這串 key 用私密管道送到 Mac（不要當公開 release asset）。

---

## STEP 6 🪟 發布狀態檔並推上 GitHub

```powershell
cd C:\Users\User\fake-trial-poke
keyguard publish-status -o licence-status --app FAKE_TRIAL_POKE
git add licence-status
git commit -m "chore: 發布狀態更新（新增 gsinvest018 授權）"
git push
```

**預期看到**：新檔
`licence-status/FAKE_TRIAL_POKE/06b82fee3ba41c4bbc81f7bc6b5384d68d475cc7e552b1fb0dbf7583304b299d.txt`

那串雜湊就是 `sha256("gsinvest018@gsinvest.com.tw")`，也就是那台 Mac 會去
抓的檔名。內容應含 `# <licence_id>  PRO  ACTIVE` 與一段 `KGS1.` 聲明。

**驗證 Mac 抓得到**（🍎 或任何一台都可以）：

```bash
curl -sS "https://raw.githubusercontent.com/gsinvest017-ai/fake-trial-poke/master/licence-status/FAKE_TRIAL_POKE/06b82fee3ba41c4bbc81f7bc6b5384d68d475cc7e552b1fb0dbf7583304b299d.txt"
```

**預期看到**：上面那份內容。拿到 404 就是 push 沒成功或還沒生效，**等 5
分鐘再試一次**。

---

## STEP 7 🍎 啟用（這步不能跳）

```bash
cd "/Applications/Fake Trial Poke.app/Contents/MacOS"
./fake-trial-poke --activate "<KG1 licence key>" \
  --licence-email "gsinvest018@gsinvest.com.tw"
./fake-trial-poke --licence-status
```

**預期看到**：

```json
{
  "allowed": true,
  "status": "active",
  "enforced": true,
  "email": "gsinvest018@gsinvest.com.tw"
}
```

**排除**：
- `enforced: false` → 你跑的不是打包後的 `.app`（原始碼模式不強制）。
- `status` 是 `trial` → 啟用沒成功，撤銷測試會無效。重看 `--activate` 的
  輸出訊息。
- 授權狀態檔在 `~/fake-trial-poke/licence.json`（macOS 沒有 `APPDATA`，
  `state_dir()` 退回 `$HOME`）。**不是** `~/Library/Application Support/`
  —— 那裡放的是錄製資料。

---

## STEP 8 🍎 基準：正常啟動長什麼樣

雙擊 `/Applications/Fake Trial Poke.app`。

**預期看到**：正常 UI，且

```bash
lsof -nP -iTCP:8900 -sTCP:LISTEN
```

**有**輸出（服務起來了）。

記住這個畫面 —— 後面就是要看它變成拒絕畫面、再變回來。看完 **⌘Q 完全結束**。

---

## STEP 9 撤銷輪

### 9a 🪟 在 admin console 撤銷

```powershell
keyguard admin --key $HOME\.keyguard-vendor\gs_private.key
```

瀏覽器開 `http://127.0.0.1:8770`，找到 `gsinvest018@gsinvest.com.tw` 的那張
授權 → 「Mark revoked」→ 填理由 → 確認。

**預期看到**：`<licence_id> marked revoked — publish the status files to act
on it`，接著自動跳出 publish 對話框，告訴你檔案寫到
`C:\Users\User\.keyguard-vendor\status`。

### 9b 🪟 把狀態檔搬進 repo 並推上去

**console 只寫檔，不上傳**（那個行程握有簽章私鑰、刻意綁在 loopback 不
對外連線），所以這步一定要手動：

```powershell
Copy-Item $HOME\.keyguard-vendor\status\* C:\Users\User\fake-trial-poke\licence-status\ -Recurse -Force
cd C:\Users\User\fake-trial-poke
git add licence-status
git commit -m "chore: 發布狀態更新（gsinvest018 撤銷實測）"
git push
```

**預期看到**：`06b82fee….txt` 內容從 `ACTIVE` 變成 `REVOKED`，且 key 那行
消失（撤銷的授權不會連 key 一起送）。

### 9c ⏱ 等 5 分鐘

CDN 快取。不等的話你很可能測到舊的那一份。

### 9d 🍎 重開 App

⌘Q 完全結束後重新雙擊。

**預期看到**：
1. 應用自己的視窗裡疊出鎖屏，真 UI 在後面但完全惰性。
2. `lsof -nP -iTCP:8900 -sTCP:LISTEN` **沒有輸出**。

第 2 條比第 1 條重要 —— 蓋在跑著的服務上面的鎖屏是裝飾，不是閘門。

```bash
cat ~/fake-trial-poke/licence-status.json         # status 應為 REVOKED
"/Applications/Fake Trial Poke.app/Contents/MacOS/fake-trial-poke" --licence-status
```

**排除**：仍然正常啟動 →
- 有等滿 5 分鐘嗎？
- `licence-status.json` 的 `status` 是什麼？還是 ACTIVE 表示沒抓到新聲明。
- `--licence-status` 顯示 `trial` 嗎？表示 STEP 7 其實沒成功。
- 那張授權是不是用 `--no-record` 簽的？那樣撤不掉。

---

## STEP 10 復權輪

### 10a 🪟 undo

同一個畫面按「undo」。

**預期看到**：`<licence_id> reinstated — publish to apply`

### 10b 🪟 一樣要複製 + commit + push

```powershell
Copy-Item $HOME\.keyguard-vendor\status\* C:\Users\User\fake-trial-poke\licence-status\ -Recurse -Force
cd C:\Users\User\fake-trial-poke
git add licence-status
git commit -m "chore: 發布狀態更新（gsinvest018 復權）"
git push
```

### 10c ⏱ 等 5 分鐘 → 10d 🍎 ⌘Q 後重開

**預期看到**：回到 STEP 8 的正常 UI，PORT 8900 又被監聽。

能復權是因為 `record_statement()` 只接受**時間戳更新**的聲明：較新的
ACTIVE 會蓋掉快取裡的 REVOKED。反過來不成立 —— 舊的 ACTIVE 不能拿來回放
撤銷。

---

## STEP 11 🍎 順帶驗一次「到期會被擋住」

跟撤銷是兩件事：這支測的是**到期**，用暫時的 `APPDATA`，**不會動到你剛剛
的啟用狀態**。

```bash
cd /path/to/fake-trial-poke
./tools/verify-refusal-macos.sh \
  --app "/Applications/Fake Trial Poke.app" \
  --email gsinvest018@gsinvest.com.tw
```

**預期看到**：`PASS=n FAIL=0` 與 `拒絕行為驗證通過。`，並產出
`refusal-macos.png`。

視窗標題偵測與截圖需要「輔助使用」與「螢幕錄製」權限（系統設定 → 隱私權
與安全性）。沒授權會降級成 `[WARN]`，不會誤判成失敗。

---

## 一頁排除表

macOS 上「按了沒反應」有五種來源，畫面上長得都一樣：

| 症狀 | 先確認 |
| --- | --- |
| 雙擊完全沒事 | Gatekeeper 隔離 → `xattr -dr com.apple.quarantine` |
| 有跑但沒視窗 | `refusal_ui()` 是不是回 `text`（缺 tkinter / pywebview） |
| 撤銷後仍放行 | 等滿 5 分鐘沒有？`~/fake-trial-poke/licence-status.json` 的 status？ |
| 撤銷聲明被忽略 | 是不是在跑 trial（沒啟用）？授權是不是 `--no-record` 簽的？ |
| UI 開了但沒資料 | `.env` / Shioaji 登入問題，與授權無關 |

## 已知限制（設計如此，不是 bug）

- **只在啟動時判定。** 撤銷不會打斷正在跑的行程。
- **撤銷是黏的。** 收到簽章撤銷後即使離線也持續生效，否則「撤銷」等於
  「請不要編輯 hosts 檔」。
- **聲明 30 天後失效。** 停止發布＝停止撤銷（安全方向），所以至少每 30 天
  要重新 publish 一次。
- **只會延長，不會縮短。** 自動撿到的 key 只有在到期日更晚時才套用。
- **macOS 沒有 `keyguard refusalcheck`。** 它在 `sys.platform != "win32"`
  直接 SKIP，所以 macOS build 少了 Windows 產線那道自動出貨閘門。
  `tools/verify-refusal-macos.sh` 補的是同一件事，但它不在 CI 裡，要自己跑。
