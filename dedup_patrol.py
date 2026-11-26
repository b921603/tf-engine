#!/usr/bin/env python3
"""dedup_patrol.py — TFF 頻道重複分片自動巡刪（2026-11-24 事故，用戶核准全程自動化）

範圍（僅此次事故）：
  - 對象＝「完全同名」的分片（norm_title 後仍相同，含（i/n）後綴）——
    不同後綴（1/4 vs 2/4）≠重複，永不觸碰；單一分片＝唯一，永不觸碰。
  - 保留規則：done.json mids 中的正主優先（帳本權威）；無帳本記錄→保留清單首筆。
  - 防護：單輪刪除上限 MAX_DEL；清單異常（raw_list raise）→跳過該頻道不刪；
    AuthError→auto_relogin；全程 audit（trail/dedup_actions.jsonl）＋ntfy 報告。

主 sync 防復發（2026-11-24 bf902a3）：raw_list 首頁空=raise（不靜默少算）、
parts ledger 即時落帳（resume=清單∪帳本）——本巡刪器與主 sync 並行安全
（帳本保護下，巡刪造成的清單波動不會誘發重傳）。
"""
import json, time, os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from collections import defaultdict

from tfs_engine import (SES, req, raw_list, norm_title, tfs_channels,
                        auto_relogin, art_path, rj, wj, log, ntfy, GAP)

MAX_DEL = 60                       # 單輪刪除上限（防爆量／防 bug 擴散）
# 11-26 繼承審計缺口 D：dedup 覆蓋擴展——ob 等非 TFF 頻道經 DEDUP_EXTRA_CIDS 納入
# （保留規則：帳本正主優先→無帳本記錄保留首筆；MAX_DEL/跳過防護同款適用）
EXTRA_CIDS = [c.strip() for c in os.environ.get("DEDUP_EXTRA_CIDS", "").split(",") if c.strip()]
DELETE_MODE = os.environ.get("DEDUP_DELETE", "") == "1"
AUTH_URL = "https://my.eudic.net/ting/DeletePriveteMedia"


def canonical_mids():
    """done.json 全部 mids 的集合（帳本正主）。
    dedup workflow 的 checkout 沒有 state/done.json（不入 repo）→
    fallback 到 trail/done.json（sync ledger step 每 run commit 的副本）。"""
    done = rj(art_path("done.json"), None)
    if done is None:
        trail = art_path("done.json").replace("state/", "trail/")
        done = rj(trail, {}) or {}
        if done:
            log(f"canonical: using trail/done.json ({len(done)} entries)")
    mids = set()
    for rec in done.values():
        for m in (rec.get("mids") or []):
            if m:
                mids.add(m)
    return mids, len(done)


def audit(rec):
    with open(art_path("dedup_actions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def scan_channel(cid):
    """[(norm_full_title, [(title, mid)…])] 僅完全同名組（n>1）。"""
    rows = raw_list(cid)                       # hardened：首頁空會 raise
    groups = defaultdict(list)
    for t, m in rows:
        groups[norm_title(t)].append((t, m))   # 全名（含後綴）＝分片身份
    return rows, {k: v for k, v in groups.items() if len(v) > 1}


def delete_media(mid):
    r = req("POST", AUTH_URL, data=[("mediaIds", mid)], timeout=60)
    return r.status_code == 200 and '"success":true' in (r.text or "").replace(" ", "")


def patrol():
    mids_kept, n_done = canonical_mids()
    log(f"dedup patrol: delete_mode={DELETE_MODE} ｜ done={n_done} ｜ canonical mids={len(mids_kept)}")
    chans = tfs_channels() + [(0, 0, c) for c in EXTRA_CIDS]
    if EXTRA_CIDS:
        log(f"dedup extra channels: {[c[:8] for c in EXTRA_CIDS]}（帳本正主僅覆蓋 TFF——extra 頻道保留首筆策略）")
    report = {"at": time.strftime("%F %T"), "mode": "delete" if DELETE_MODE else "dry",
              "channels": [], "deleted": 0, "deleted_list": [],
              "skipped": [], "remaining_dups": {}}
    quota = MAX_DEL
    for _a, _b, cid in chans:
        try:
            rows, dups = scan_channel(cid)
        except Exception as ex:
            log(f"  ⚠ {cid[:8]}: listing 失敗（{str(ex)[:60]}）→ 跳過不刪")
            report["skipped"].append({"cid": cid[:8], "reason": str(ex)[:120]})
            continue
        if not rows:
            report["skipped"].append({"cid": cid[:8], "reason": "empty listing"})
            continue
        ch_del, ch_keep = [], []
        for key, copies in sorted(dups.items()):
            keep = next((c for c in copies if c[1] in mids_kept), copies[0])
            for t, m in copies:
                if m == keep[1]:
                    continue
                rec = {"ts": time.strftime("%F %T"), "cid": cid[:8], "title": t[:90],
                       "deleted_mid": m, "kept_mid": keep[1], "done": False}
                if DELETE_MODE and quota > 0:
                    try:
                        ok = delete_media(m)
                    except Exception as ex:
                        log(f"  delete err {str(ex)[:60]}")
                        ok = False
                    rec["done"] = bool(ok)
                    if ok:
                        ch_del.append(t); quota -= 1
                    time.sleep(max(GAP, 2.0))
                else:
                    rec["done"] = False
                ch_keep.append(rec)
            log(f"  ⚑ {cid[:8]} dup x{len(copies)}: {copies[0][0][:60]} → keep {keep[1][:10]}")
        if ch_keep:
            report["channels"].append({"cid": cid[:8], "dup_groups": len(dups),
                                       "rows": len(rows)})
            report["deleted_list"].extend(ch_keep)
            audit({"ts": time.strftime("%F %T"), "cid": cid, "actions": ch_keep})
        # 刪後覆核（本輪已刪的頻道重掃）
        if ch_del:
            time.sleep(4)
            try:
                _rows, remain = scan_channel(cid)
                if remain:
                    report["remaining_dups"][cid[:8]] = len(remain)
                    log(f"  ↺ 覆核：{cid[:8]} 尚餘 {len(remain)} 組（下輪續刪）")
                else:
                    log(f"  ↺ 覆核：{cid[:8]} 重複清零 ✓")
            except Exception as ex:
                log(f"  覆核 err: {str(ex)[:60]}")
        time.sleep(3)
    report["deleted"] = sum(1 for x in report["deleted_list"] if x.get("done"))
    report["pending_delete"] = sum(1 for x in report["deleted_list"] if not x.get("done"))
    wj(art_path("dup_report.json"), report)
    body = (f"模式={report['mode']}｜刪除 {report['deleted']} 份"
            f"{'（額度用盡，下輪續）' if quota == 0 else ''}"
            f"｜待刪 {report['pending_delete']}｜跳過頻道 {len(report['skipped'])}")
    log(f"dedup done: {body}")
    try:
        ntfy("TFF dedup patrol", body, 3)
    except Exception:
        pass


if __name__ == "__main__":
    for attempt in range(2):
        try:
            patrol()
            break
        except Exception as ex:
            log(f"patrol err: {str(ex)[:110]}")
            if attempt == 0:
                try:
                    if auto_relogin():
                        continue
                except Exception as ex2:
                    log(f"relogin err: {str(ex2)[:80]}")
            sys.exit(1)
