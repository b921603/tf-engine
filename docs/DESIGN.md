# tff-sync 設計筆記（引擎內部）

- **分片**：SEG=3600s（歐路單篇音訊上限）；empirical 2026-11-16：AI 字幕 API 的
  「时长超过 60 分钟」回應多為**建文後暫態**（轉碼/佇列窗），patient-retry 10×45s 即解，
  3600s 分片實測可排程；批次回應「其余已成功创建任务」＝部分排程成功 → 視為已排程。
- **頻道路由**：ep_num → ch_range_for → cid_for；缺頻道 → ntfy 提示（非 dry）。
- **AI 路徑**：create(needAISubtitle=true) → BatchAiSubtitle(patient) → AI_RE 輪詢
  (TFS_AI_WAIT, 預設 900s) → AI 文字導讀 → byte-identical re-POST → BatchHighlightSubtitle
  → guard_inline 註冊輪換。未竟者由 subsync 每小時接手（4 輪上限 → 誠實通知）。
- **封面**：create 帶官方圖 fid；rotate 每小時以新鮮 fid 重 POST（fid GC ~80s 防線）。
  清單 img-zlg=UserMediaPool/{MID}/index.png 為封面真值（draglist.png 是裝飾圖，勿誤判）。
  parse_edit_page thumb_fid 不可靠（常空）→ 空 thumb=不變更，伺服器語意安全。
- **rotate 自舉**：rotate_cache 缺項 → rotate_cache_backfill（edit page 掃描）≤TFS_ROT_BOOTSTRAP_LIMIT。
  **勿與 job_bootstrap（逐集上傳）混淆**。
- **自動重登**：AuthError → auto_relogin（eudic_login 表單 → GetPrivateChannels 驗證 →
  update_cookie_secret GH sealed box 204）→ 修補內部 cookie 後重試原命令。
- **budget**：每集下載→轉檔→上傳→刪 conv（ok_all 後）；32k mono ≈ 24MB/h。
- **wayback feed 快照**：`web.archive.org/web/{ts}id_/https://rss.art19.com/tim-ferriss-show`
  抓已刪集（CDX filter=statuscode:200 正常運作；此前誤判語法錯誤）。
