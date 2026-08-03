# licence-status

由 `keyguard publish-status` 產生，**不要手改**。

每個檔案是 `<APP_ID>/<sha256(licensee email)>.txt`，內含該使用者所有還有效的
授權的**簽章狀態聲明**。應用程式啟動時抓取自己的那一份，據此決定放行或擋住。

只放**已綁定機器**的授權金鑰。未綁定與 bind-on-first-use 的金鑰不會出現在這裡：
路徑是 email 的雜湊，對知道那個 email 的人來說不是秘密，而未綁定的金鑰誰先兌換
誰擁有——那等於把客戶的席次送給任何知道他信箱的人。那類金鑰仍走信件與
`licences` 投放資料夾。

更新方式：

    keyguard publish-status -o licence-status --app FAKE_TRIAL_POKE
    git add licence-status && git commit && git push

聲明 30 天後就不再被相信，所以至少要這麼頻繁地重新發布——**停止發布等於停止
撤銷**，那是安全的方向。
