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

## 實證增補（2026-11-16 run#3 期間）

- **audit 三誤報解剖**：①rotate_cache 記錄的 mid 可能是已刪 dup（list_articles title-key 遮蔽）
  → backfill 改為「live mid ≠ cache mid 即刷新重抓」；②WAF 拉空 GET → audit 對 got=0 先 patient
  re-read 一次再記 drift；③edit-page thumb_fid 永遠空（平台不再回填）→ 封面檢查改清單
  img-zlg（media_default.png=缺封面）。修後沙盒實測 checked=4 bad=0。
- **title-key 遮蔽是 dup 溫床**：list_articles 同名文章互相覆蓋；dup 掃描必用 dragBox 原始掃描
  （raw_list）；本日 GA 重跑事故的 3 篇 dup 由此找出並清除。
- **斷點續傳**：sync_one 以 expected_parts（RSS duration/SEG）對 count_channel_parts 判斷
  「已存在分片→跳過」；全齊→already-complete 標記 done。reconcile_done 在 bootstrap/sync 開場
  對帳（guid 缺但分片全齊→補 done），state 遺失自癒。
- **AI 暫態結論**：BatchAiSubtitle 的「时长超过 60 分钟」在 45/48/60 分實測皆可於數分鐘後通過
  → 引擎 patient-retry 10×45s＋「其余已成功」視為部分成功；AI 失敗不再記 terminal unsupported
  （subsync 4 輪內自動重排）。
