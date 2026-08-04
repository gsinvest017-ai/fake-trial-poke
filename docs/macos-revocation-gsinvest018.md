# 實測：從 IT admin 的 console 讓這台 Mac 的 fake-trial-poke 立即失效

對象：`gsinvest018@gsinvest.com.tw` 在 `FAKE_TRIAL_POKE` 的 release 授權。
目標：IT admin 在 Windows 上的 keyguard admin console 按「撤銷」→ 這台
Mac Studio 上的 fake-trial-poke 下次開啟時跳出憑證失效遮擋視窗；按「復權」
→ 遮擋視窗消失、服務恢復。

這份是**針對這一組帳號與這台機器**的操作書。通用的撤銷機制說明在
[`macos-revocation-test.md`](macos-revocation-test.md)。

---

## 1. 這條鏈實際上長什麼樣

console **不會**連到客戶端，也不能。它持有簽發私鑰，開放到網路等於把簽發
能力送人（`CONSOLE_HOST` 寫死 `127.0.0.1`，有測試守著）。所以是**本地簽章、
靜態託管**：

```
IT admin 的 Windows PC                     這台 Mac Studio
┌────────────────────────┐                ┌─────────────────────────┐
│ admin console          │                │ fake-trial-poke         │
│  · 持私鑰              │                │  · 只有公鑰，只能驗證   │
│  · 標記 REVOKED        │                │                         │
│  · 簽出 KGS1 狀態聲明  │                │  啟動時 HTTPS GET ──┐   │
└───────────┬────────────┘                └─────────────────────┼───┘
            │ publish-status                                    │
            ▼                                                   │
   licence-status/FAKE_TRIAL_POKE/<sha256(email)>.txt            │
            │ git commit + push（master）                       │
            ▼                                                   │
   raw.githubusercontent.com ─────────────────────────────────────
            （CDN max-age=300，實測傳播 1–4 分鐘）
```

**撤銷不是「即時」，是「下一次啟動、而且 CDN 已更新之後」。** 要更即時就得
換一個快取時間短的靜態主機。

這台 Mac 要抓的檔案路徑是固定的：

```
https://raw.githubusercontent.com/gsinvest017-ai/fake-trial-poke/master/
  licence-status/FAKE_TRIAL_POKE/
  06b82fee3ba41c4bbc81f7bc6b5384d68d475cc7e552b1fb0dbf7583304b299d.txt
```

那串 hex 是 `sha256("gsinvest018@gsinvest.com.tw")`。

---

## 2. 先看現況：為什麼「現在直接去按撤銷」一定不會成功

在這台 Mac 上跑：

```bash
cd ~/fake-trial-poke
FAKE_TRIAL_POKE_LICENCE_EMAIL=gsinvest018@gsinvest.com.tw \
  ./系統檔案/.venv/bin/python app.py --licence-refresh
```

目前實際輸出（2026-08-04）：

```
licence status: https://.../06b82fee….txt unavailable (HTTP Error 404: Not Found)
licence refresh: skipping … -- no existing licence to renew (status trial)
{
  "state": { "allowed": true, "status": "trial", "days_remaining": 13,
             "enforced": false, "email": "gsinvest018@gsinvest.com.tw" },
  "statement_cache": null
}
```

四件事同時擋著，**每一件都足以讓撤銷靜靜地沒有作用**：

| # | 現況 | 為什麼撤銷不會生效 |
|---|---|---|
| A | `status: trial` — 這台機器上沒有安裝 gsinvest018 的正式授權 | `_apply_statement()` 開頭就是 `if not installed_licence_id(): return False`。**沒有已安裝的授權，狀態聲明沒有指涉對象，會被直接丟掉。** 這是刻意的：否則「同一個 email 的另一張授權被撤銷」會誤傷正在跑 trial 的機器 |
| B | 404 — 發布樹裡沒有 gsinvest018 的檔案 | 取不到就 fail-open，當作沒發生 |
| C | `enforced: false` — 這是原始碼模式 | 原始碼模式不阻擋開發。要看到遮擋視窗只有兩條路：跑 frozen 的 `.app`，或設 `FAKE_TRIAL_POKE_LICENCE_ENFORCE=1` |
| D | 授權必須**綁定這台機器** | `publish-status` **只發布已綁定機器的金鑰**。未綁定的金鑰誰先兌換誰擁有，而發布路徑只是 email 的 sha256——對知道那個信箱的人不是秘密，發出去等於把席次送人 |

所以下面的一次性設定不是繁文縟節，A 和 D 是**機制上的硬前提**。

---

## 3. 一次性設定

### 3.1 這台 Mac：取得 machine id

兩條都可以，輸出一樣：

```bash
cd ~/fake-trial-poke/系統檔案

./.venv/bin/keyguard machine                    # keyguard 自己的子指令
./.venv/bin/python ../app.py --machine-id       # 應用程式包裝過的同一個值
```

目前這台是：

```
MID_494cc46470dd14f6b80e0621
```

把這串給 IT admin。它是硬體衍生的，重裝系統前不會變。

> **三個容易踩的坑：**
>
> * `--machine-id`（有連字號）是 **fake-trial-poke 的 `app.py`** 的旗標；
>   keyguard 這邊是 **`machine` 子指令**。兩個專案都有一支 `app.py`。
> * `KEYGUARD/src/admin/app.py` 是 console 的 web app，不是 CLI，而且它是套件
>   內的模組——直接 `python3 src/admin/app.py` 一定會是
>   `ImportError: attempted relative import with no known parent package`。
>   要跑就 `python -m keyguard …`。
> * **不要用系統的 `python3`**（macOS 內建是 3.9），keyguard 需要 3.12。
>   一律走 `系統檔案/.venv/bin/` 底下那支。

> ⚠️ **從 ssh 問 machine id 要先確認 keyguard 版本。** `ioreg` 在 `/usr/sbin`，
> 而非互動 ssh 與 launchd 的 PATH 沒有它。舊版 keyguard 以裸名呼叫，讀不到就
> 靜默回 `NO_MACHINEGUID`，於是**同一台 Mac 從終端機問和從 ssh 問會得到兩個
> 不同的 id**，用其中一條路綁定的授權換另一條路就被拒。更糟的是退化後的
> 原始字串是 `MG=NO_MACHINEGUID|HN=<hostname>`，機器綁定實際上只綁 hostname。
> 已在 KEYGUARD `fix/macos-state-dir` 修掉（改用絕對路徑）。驗證方式：
>
> ```bash
> env -i PATH=/usr/bin:/bin HOME="$HOME" /bin/sh -c \
>   '"$HOME/fake-trial-poke/系統檔案/.venv/bin/keyguard" machine'
> ```
>
> 這條的輸出必須和互動 shell 完全一樣。不一樣就是 keyguard 還沒更新。

### 3.2 IT admin（Windows）：登記 licensee 並簽一張綁機器的授權

console：`keyguard admin --key C:\Users\User\.keyguard-vendor\gs_private.key`

Issue 分頁 → 找到 `gsinvest018`（可用模糊搜尋，打 `018` 或 `Ray` 都找得到）
→ **Machine id 欄填入上一步那串 `MID_…`** → Application 選 `FAKE_TRIAL_POKE`
→ Plan 選 `PRO`（或 `TRIAL_RELEASE`）→ 天數給足夠長（例如 365）→ Issue。

CLI 等價寫法：

```powershell
keyguard issue FAKE_TRIAL_POKE gsinvest018@gsinvest.com.tw `
  --key C:\Users\User\.keyguard-vendor\gs_private.key `
  --machine-id MID_494cc46470dd14f6b80e0621 `
  --expires 2027-08-04 --seats 1 --plan PRO
```

> **一定要帶 `--machine-id`。** 少了它，這張金鑰不會出現在發布樹裡（見 §2 D），
> 後面的撤銷就沒有東西可以指。

把產生的 `KG1…` 金鑰用私密管道給這台 Mac（不要當公開 release asset）。

### 3.3 這台 Mac：啟用

```bash
cd ~/fake-trial-poke
./系統檔案/.venv/bin/python app.py \
  --activate "<KG1 金鑰>" --licence-email gsinvest018@gsinvest.com.tw
```

驗證——`status` 必須從 `trial` 變成有 licence id 的正式狀態：

```bash
FAKE_TRIAL_POKE_LICENCE_EMAIL=gsinvest018@gsinvest.com.tw \
  ./系統檔案/.venv/bin/python app.py --licence-refresh
```

**要看到 `"status"` 不再是 `trial`。** 還是 trial 就表示啟用沒成功，
後面全部都不用做了。

### 3.4 IT admin：發布狀態樹並推上 master

```powershell
cd C:\Users\User\fake-trial-poke
keyguard publish-status -o licence-status --app FAKE_TRIAL_POKE
git add licence-status
git commit -m "chore: 發布 FAKE_TRIAL_POKE 授權狀態"
git push origin master
```

驗證檔案真的上線了（在任一台機器）：

```bash
curl -sI https://raw.githubusercontent.com/gsinvest017-ai/fake-trial-poke/master/licence-status/FAKE_TRIAL_POKE/06b82fee3ba41c4bbc81f7bc6b5384d68d475cc7e552b1fb0dbf7583304b299d.txt \
  | head -1
```

要看到 `HTTP/2 200`。還是 404 就是 §2 D 沒滿足——回去確認金鑰有綁 machine id。

---

## 4. 撤銷實測

### 4.1 IT admin：在 console 撤銷

Registry / Issued 分頁 → 找到 gsinvest018 的那張 `KG-000xx` → **Revoke**，
填理由。console 會**直接跳出發布對話框**，按下去就等於重跑
`publish-status`（點按鈕和打指令產生完全相同的檔案）。

然後照 §3.4 `git add / commit / push`。

> 撤銷對話框如果還寫著「這不會停用客戶機器上的金鑰」，那句話是舊的，已在
> keyguard 端改掉。撤銷**會**到達客戶機器。

### 4.2 這台 Mac：確認聲明抵達

等 1–4 分鐘（CDN），然後：

```bash
FAKE_TRIAL_POKE_LICENCE_EMAIL=gsinvest018@gsinvest.com.tw \
  ./系統檔案/.venv/bin/python app.py --licence-refresh; echo "exit=$?"
```

要看到：

```json
"state":  { "allowed": false, "status": "revoked",
            "message": "Licence withdrawn by the supplier …" },
"statement_cache": { "status": "REVOKED", "licence_id": "KG-000xx",
                     "statement_utc": "2026…Z", "not_after": "2026…Z" }
```

`exit=2`。

**`statement_utc` 是關鍵欄位。** 它是判斷「新聲明到了」與「還在看舊快取」
唯一的依據——聲明只有在 `statement_utc` 比快取裡的更新時才會被接受
（這條規則同時擋掉重放舊 ACTIVE 來解除撤銷）。數字沒動就是 CDN 還沒更新，
再等一下，不要以為是壞了。

### 4.3 這台 Mac：確認真的被擋

原始碼模式預設不阻擋，要強制：

```bash
FAKE_TRIAL_POKE_LICENCE_EMAIL=gsinvest018@gsinvest.com.tw \
FAKE_TRIAL_POKE_LICENCE_ENFORCE=1 \
  ./系統檔案/.venv/bin/python app.py
echo "exit=$?"
```

預期：**跳出遮擋視窗**（真實 UI 在後面可辨識但完全惰性），關掉後 `exit=2`，
而且**整個過程 PORT 8900 不會被開啟**。那條 port 斷言才是真正的閘門——
蓋在一個跑著的服務上面的鎖屏是裝飾。另開一個終端機確認：

```bash
lsof -nP -iTCP:8900 -sTCP:LISTEN    # 應該沒有輸出
```

要用出貨形式測就改跑 `.app`（不需要 `ENFORCE` 環境變數，frozen 自動強制）：

```bash
"dist/Fake Trial Poke.app/Contents/MacOS/fake-trial-poke"
```

### 4.4 確認撤銷是黏著的

拔網路線（或關 Wi-Fi）再開一次。**還是要被擋。**

一旦收到通過驗證的撤銷就會被存下來並從此遵守，即使之後永遠連不上。少了
這條，「撤銷」等於「請不要編輯 hosts 檔」。

快取位置（macOS）：

```
~/Library/Application Support/fake-trial-poke/licence-status.json
```

---

## 5. 復權實測

### 5.1 IT admin

console 同一張授權 → **Reinstate** → 發布 → `git push origin master`。

這會簽一份 `statement_utc` 更新的 `ACTIVE` 聲明，蓋掉快取裡的撤銷。

### 5.2 這台 Mac

等 1–4 分鐘，然後：

```bash
FAKE_TRIAL_POKE_LICENCE_EMAIL=gsinvest018@gsinvest.com.tw \
  ./系統檔案/.venv/bin/python app.py --licence-refresh; echo "exit=$?"
```

要看到 `"allowed": true`、`statement_cache.status` 變回 `ACTIVE`、
`statement_utc` 比撤銷那次更新，`exit=0`。

再開一次應用程式，遮擋視窗應該消失、服務正常起來。

---

## 6. 排錯表

| 症狀 | 先確認 |
|---|---|
| `--licence-refresh` 一直 404 | 金鑰有沒有綁 machine id？沒綁的不會被發布（§2 D） |
| 200 了但 `state` 沒變 | `statement_cache.statement_utc` 有沒有前進？沒有＝CDN 還沒更新，再等 |
| 撤銷了但 `allowed` 還是 true | `status` 是不是 `trial`？trial 沒有 licence id，聲明會被丟掉（§2 A） |
| 應用程式照常開 | 是不是原始碼模式？`enforced` 要是 true，否則加 `FAKE_TRIAL_POKE_LICENCE_ENFORCE=1` |
| 雙擊 `.app` 完全沒事 | Gatekeeper 隔離 → `xattr -dr com.apple.quarantine "dist/Fake Trial Poke.app"` |
| 有跑但沒視窗 | 缺 pywebview／tkinter。跑 `tools/pack-macos.sh` 的 Gate 0 會直接講 |
| 撤銷過一陣子自己失效 | 聲明有 `not_after`（預設 30 天）。**停止發布＝停止撤銷**，那是安全的方向，所以至少每 30 天重新 publish 一次 |

---

## 7. 從 licence server 用 SSH 遠端佈署

licence server（Windows，持私鑰）對這台 Mac 有免密 ssh，想讓 server 端的
coding agent 一路把憑證設定完。可以，但有三條界線先講清楚。

### 7.1 三條不能越的界線

**私鑰永遠不過來。** ssh 過來的只有簽好的 `KG1…` 金鑰。`gs_private.key` 留在
Windows，console 在那邊簽完再送結果。一旦私鑰複製到第二台機器，簽發能力就
不只一份了。

**`KG1` 金鑰不可以落進 repo。** `~/fake-trial-poke` 是 **public repository** 的
工作目錄。金鑰要放進投放資料夾，不是專案目錄：

```
~/Library/Application Support/fake-trial-poke/licences/
```

（這個路徑是 KEYGUARD `fix/macos-state-dir` 修正後的位置。修正前 keyguard 會
把整個授權狀態寫進 `~/fake-trial-poke`，也就是 git working tree——`licence.json`
離被 commit 只差一個 `git add`。）

**release 裡沒有憑證。** 每個客戶下載的是**同一個** binary，授權不會被編譯
進去。所謂「簽發專用憑證的 release」其實是兩件事：同一份 release ＋ 單獨投遞
一把綁該機器的 `KG1`。server 端要做的是後者，不是為每個客戶產一個 build。

### 7.2 目前的路障：沒有 macOS release asset

```bash
gh release view v0.1.4 -R gsinvest017-ai/fake-trial-poke \
  --json assets --jq '.assets[].name'
# → fake-trial-poke-setup.exe
```

**只有 Windows installer。** 所以「遠端指令讓這台 Mac 下載 release」現在下載
不到東西。要先跑 `tools/pack-macos.sh --dmg` 產出 `.app` / DMG 並掛上 release，
而那條產線的 Gate 1 需要 Apple Developer 憑證——沒有簽章與公證的話，下載來的
`.app` 會被 Gatekeeper 擋下，**而那個失敗看起來跟授權拒絕一模一樣**。

在補上 macOS asset 之前，server 端能做的是「佈署授權」，不是「佈署 release」。

### 7.3 指令序列

在 Windows 上（`mac` 是 ssh host alias）：

```bash
# 1. 取 machine id。絕對路徑，因為非互動 ssh 的 PATH 很小。
MID=$(ssh mac '"$HOME/fake-trial-poke/系統檔案/.venv/bin/keyguard" machine')
echo "$MID"          # 必須是 MID_494cc46470dd14f6b80e0621

# 2. 在 Windows 上簽（私鑰不離開這裡）
keyguard issue FAKE_TRIAL_POKE gsinvest018@gsinvest.com.tw \
  --key ~/.keyguard-vendor/gs_private.key \
  --machine-id "$MID" --expires 2027-08-04 --seats 1 --plan PRO
# → KG1...

# 3. 投遞到 inbox（不是專案目錄）。下次啟動自動撿走，客戶不用打指令。
INBOX='$HOME/Library/Application Support/fake-trial-poke/licences'
ssh mac "mkdir -p \"$INBOX\""
printf '%s\n' "$KG1" | ssh mac "cat > \"$INBOX/gsinvest018.txt\""

# 4. 發布狀態樹（撤銷／復權都靠這棵樹）
keyguard publish-status -o licence-status --app FAKE_TRIAL_POKE
git add licence-status && git commit -m "chore: 發布授權狀態" && git push origin master

# 5. 驗證。exit 0 = 放行，exit 2 = 被拒——agent 直接用退出碼判斷，
#    不需要解析 JSON，也不需要開 GUI 猜「沒跳視窗」是什麼意思。
ssh mac 'FAKE_TRIAL_POKE_LICENCE_EMAIL=gsinvest018@gsinvest.com.tw \
  "$HOME/fake-trial-poke/系統檔案/.venv/bin/python" \
  "$HOME/fake-trial-poke/app.py" --licence-refresh'
echo "exit=$?"
```

撤銷之後重跑第 5 步，等到 `exit=2` 就是聲明已經抵達並生效。復權後等到
`exit=0`。中間卡住時看輸出裡的 `statement_cache.statement_utc` 有沒有前進——
沒前進就是 CDN 還沒更新（1–4 分鐘），不是壞掉。

### 7.4 ssh 環境的兩個陷阱

* **一律用絕對路徑。** 非互動 ssh 不會載入 fish/zsh 的 rc，`keyguard` 與
  `python` 都不在 PATH 上。
* **不要用 `python3`。** 那是系統內建的 3.9，跑不動本專案。這與 launchd 排程
  踩過的是同一個坑。

## 8. 已知限制

- **只在啟動時判定。** 撤銷不會打斷正在跑的行程。
- **在你撤銷之前就把網路擋掉的客戶**，會一直用到金鑰自然到期。遠端撤銷提高
  濫用成本，它不是 DRM 保證。
- **傳播延遲 1–4 分鐘**，來自 raw.githubusercontent 的 `max-age=300`。
- **發布樹在 public repo。** 路徑是 email 的 sha256，對知道信箱的人不是秘密，
  所以只發布已綁機器的金鑰——這也是 §2 D 那條限制的來源。
