# tff-sync — The Tim Ferriss Show → 歐路每日英語聽力（半永久管線）

官方 RSS 全量 893 集（2014-04-18 → 今日）→ 歐路「聽力 → 我的頻道」九個分頻道，
AI 逐字稿字幕＋本集導讀＋官方逐集封面＋每小時輪換守恆。與 `htf-sync` 同構（BBR、
帳密自動重登、ntfy 兩軌告警），字幕政策為 **歐路 AI 字幕優先**（官方逐字稿存在時可核對，
不強求）。

## 頻道規劃

`The Tim Ferriss Show #001-100` … `#801-900`（依集數自動建立；`python3 tfs_engine.py channels --ensure`）
集號解析支援 `#889:` / `Ep 20:` / `Episode 1:` / `Ep. 10:` / `Lucky #888`。無編號特別篇入 `#001-100`。

## 子命令（九）

auth / sync / bootstrap / rotate / keepalive / audit / subsync / login / channels

- **bootstrap**：積壓全集最舊優先逐集搬運（`--limit N`；每集下載→32k mono→≤60min 分片→建文→AI 字幕→高亮→導讀）
- **sync**：RSS diff 抓新集（`--newest`；`--force <kw>` 為受控重試）
- **subsync**：AI 字幕 finishing queue（小時任務收尾：輪詢→導讀→高亮→註冊輪換）
- **rotate**：2 分片 LRU 重 POST（新鮮 fid 防封面 GC；`TFS_ROT_BOOTSTRAP_LIMIT` 增量回填）
- **audit**：每月漂移稽核（LRC 漂移/可見時間戳/缺導讀/缺封面）
- **login**：手動帳密重登→自我更新 secret（auto_relogin 亦會自動執行）
- **channels**：`--ensure` 建齊頻道；`--dry-run` 預演

## GA Workflows

| workflow | 排程 | 內容 |
|---|---|---|
| tff-sync | `:27` 每小時 | channels ensure → bootstrap（batch 15）→ sync（新集）→ subsync |
| tff-rotate | `:41` 每小時 | 2 分片輪換守恆（BBR） |
| tff-keepalive | `37 */6` | 靜默心跳＋防 60 天停用 |
| tff-audit | 每月 2 日 05:03 | 全頻道忠實度稽核 |
| tff-probe / tff-login | 手動 | 診斷 / 重登 |

ntfy 前綴 `TFF GA:`（與 `HTF GA:`／`HTF box` 共用 topic，分頻道不互擾）。

## 已下架集政策

資料廣度優先：已下架集以 Wayback/舊 feed/第三方索引回收 metadata（`recovered/`）。
**#519（Françoise Bourzat, 2021-06-23）**：發佈方 2021 年撤回，Art19 CDN 404、Apple 0 筆、
第三方僅 5 分鐘摘要——音檔永久消失，引擎以 `recovered/audio_lost.json` 誠實跳過（不偽造），
metadata＋存檔頁面已保全於 `recovered/`。

## Secrets（五件套）

EUDIC_COOKIE / EUDIC_USER / EUDIC_PASS / GH_PAT / NTFY_TOPIC

## 狀態與快取

GA Actions cache `tff-state-*`（done.json / sync_ledger / rotate_cache / ct_cache）＋
`tff-rot-{shard}-*`（LRU）。ledger 公開 trail 由 keepalive/sync commit。
