# 進度：讓 IT admin 的 console 撤銷能到達這台 Mac

## 目標

IT admin 在另一台 Windows PC 的 keyguard admin console 對
`gsinvest018@gsinvest.com.tw` 的 `FAKE_TRIAL_POKE` 授權按撤銷 → 這台 Mac
Studio 上的 fake-trial-poke 下次開啟跳出遮擋視窗；按復權 → 視窗消失。

操作書：[`macos-revocation-gsinvest018.md`](macos-revocation-gsinvest018.md)

## 結論先講

機制本身是完整的，Windows 端已經端到端跑通過（keyguard
`docs/progress-remote-status.md` 的 M4）。**這台 Mac 現在做不到，不是因為
機制缺東西，是因為四個前提沒滿足**，其中兩個是機制上的硬前提，不是設定
疏漏：

| # | 前提 | 現況 |
|---|---|---|
| A | 必須有**已安裝的正式授權**——`_apply_statement()` 開頭 `if not installed_licence_id(): return False` | gsinvest018 在這台是 `trial`，聲明會被直接丟掉 |
| B | 發布樹必須有那個 email 的檔案 | 404 |
| C | 必須是強制模式 | 原始碼模式 `enforced: false` |
| D | 金鑰必須**綁定 machine id**——`publish-status` 只發布已綁機器的金鑰 | 尚未簽發 |

A 和 D 都是刻意的安全設計，不能繞過。操作書 §3 就是滿足它們的順序。

## 這輪修掉的程式面缺口

### KEYGUARD（分支 `fix/macos-state-dir`，commit `b3985bd`）

1. **`AppGate.state_dir()` 在 macOS 解析到 git working tree。**
   `Path(os.environ.get("APPDATA") or Path.home()) / app_name` 在 macOS 沒有
   `APPDATA`，於是變成 `~/fake-trial-poke`——正好是 checkout 目錄。
   `licence.json` 離「被 commit 進 public repo」只差一個 `git add`；更嚴重的是
   撤銷黏著快取 `licence-status.json` 也在那裡，`git clean -fdx` 一掃就沒了。
   **撤銷若能被例行清理解除，它就不是黏著的**，而黏著是整個遠端撤銷設計唯一
   站得住的性質。改為 macOS 用 `~/Library/Application Support/<app>`。
   `APPDATA` 仍優先於一切——`verify-refusal` 就是靠設它做隔離。

2. **`_stderr_is_visible()` 讓 macOS 的拒絕永遠隱形。**
   `if not self.is_frozen() or sys.platform != "win32": return True` 這條短路
   使非 Windows 一律判定 stderr 看得見。但 PyInstaller 在 macOS 不像 Windows
   `--windowed` 會把 `sys.stderr` 設成 None，它是真物件、寫進 unified log。
   結果 `refusal_ui()` 在每個 macOS build 都回 `text`，拒絕訊息印到沒有人會看
   的地方——正是這個機制要防的失敗。改為 frozen 且非 Windows 時看 `isatty()`。

3. **`refusalcheck.py` 在 macOS 連 import 都不行。** 模組層無防護地取
   `ctypes.windll.user32`，連帶讓 `keyguard.demolock` 一起載不動。文件說它在
   非 Windows 回報 SKIP，但根本走不到；兩個測試檔是在 collection 階段爆掉。

測試：708 passed / 1 skipped（修好前有 2 個測試檔無法 collect）。

### fake-trial-poke（分支 `macos-completion`，commit `08d4b67`）

4. **`--licence-refresh`。** `--licence-status` 只讀本機快取——那是對的，判定
   不該依賴網路——但也因此在「剛發布撤銷，到了沒？」時完全幫不上忙，只能反覆
   開 GUI 猜「沒跳視窗」代表什麼。新旗標實際抓一次遠端再回報，並印出
   `statement_cache`：**`statement_utc` 是唯一能分辨「新聲明到了」與「還在看
   舊快取」的欄位**（聲明只在它前進時被接受，這條同時擋掉重放舊 ACTIVE）。

5. **`pack-macos.sh` Gate 0 的 refusal_ui 檢查是壞的。** 照抄
   `pack.config.ps1` 註解裡的 `AppGate.refusal_ui()`，但那是 instance method，
   當 classmethod 呼叫直接 TypeError。而且就算呼叫正確也沒意義：答案取決於
   stderr 有沒有人看得到，在 build 終端機裡永遠是「看得到」，所以永遠回 text。
   改為模擬出貨情境（frozen + 非 tty）再問。

## 尚未完成

- **實測本身還沒跑。** 需要 IT admin 那台簽出一張綁 `MID_494cc46470dd14f6b80e0621`
  的授權。在那之前這台只能是 trial，撤銷不會有作用。
- **KEYGUARD 的 PR 還沒開。** 依 `.github/BRANCH-PROTECTION-POLICY.md`，
  master 受保護、交付物是 PR。分支 `fix/macos-state-dir` 已 commit 在本機，
  尚未 push——開 PR 是不可逆的對外動作，等使用者確認。
- **fake-trial-poke 的分支名不合政策。** 現在是 `macos-completion`，
  政策要求 `dev/` `feat/` `fix/` 等前綴。要 push 前應改名。
