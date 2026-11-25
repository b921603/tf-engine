#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pit_report.py — engine pit-status emitter（pit-status/1 協議）。

獨立於 tfs_engine：只讀 state/done.json（guid 帳本）＋ recovered/tfs_catalog.json
（893 集官方目錄）＋ recovered/audio_lost.json（音檔遺失例外），產出
trail/pit-status.json 供 patrol 讀取。完成度以「官方集數」為分子分母
（分段膨脹不計入）。任何失敗都不影響主流程（倉庫審計 trail 而已）。
"""
import json, os, time

BASE = os.environ.get("TFS_BASE", "state")
RSS = "https://rss.art19.com/tim-ferriss-show"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}


def rj(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d


def rss_guids_live():
    """活 RSS guid 集——烤目錄快照看不到官方下架，這裡補盲。"""
    import re, requests
    r = requests.get(RSS, headers=UA, timeout=90)
    out = set()
    for blk in re.findall(r"<item>(.*?)</item>", r.text or "", re.S):
        g = re.search(r"<guid[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</guid>", blk, re.S)
        if g:
            out.add(g.group(1).strip())
    return out


def main():
    done = rj(os.path.join(BASE, "done.json"), {}) or {}
    cat = rj("recovered/tfs_catalog.json", []) or []
    lost = rj("recovered/audio_lost.json", []) or []
    lost_ids = set()
    for x in lost:
        if isinstance(x, dict):
            lost_ids.add(x.get("guid") or x.get("num"))
        elif isinstance(x, (int, str)):
            lost_ids.add(x)
    total = len(cat)
    have = 0
    missing = []
    for e in cat:
        g = e.get("guid")
        if g in done:
            have += 1
        elif e.get("num") in lost_ids or g in lost_ids:
            continue          # 官方音檔已遺失——例外不計債
        else:
            missing.append({"num": e.get("num"), "title": (e.get("title") or "")[:60]})
    # 下架偵測：done 帳上有、活 RSS 已消失（唯讀報告——不動頻道任何檔案）
    retired, retired_check = None, "ok"
    try:
        rg = rss_guids_live()
        if len(rg) < len(done) - 5:
            retired_check = "inconclusive (feed smaller than ledger — guard)"
        else:
            retired = sorted(set(done) - rg)
    except Exception as ex:
        retired_check = f"skip ({str(ex)[:40]})"
    avail = total - len(lost_ids)
    rep = {
        "protocol": "pit-status/1",
        "family": "tff",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "backlog",
        "official_total": total,
        "official_available": avail,
        "completed_eps": have,
        "true_missing": len(missing),
        "completion": round(have / avail, 3) if avail else None,
        "status": "complete" if not missing else "in_progress",
        "blocked_reason": f"audio-lost {len(lost_ids)} eps excluded" if lost_ids else None,
        "missing_sample": missing[:10],
        "retired_count": len(retired) if retired is not None else None,
        "retired_sample": [str((done.get(g) or {}).get("title") or g)[:50]
                           for g in (retired or [])][:10],
        "retired_check": retired_check,
        "notes": "completion = distinct official episodes in done ledger / available catalog; parts inflate article counts",
    }
    trail = os.path.join(BASE, "..", "trail")   # 跟隨 BASE——沙盒測試（TFS_BASE=/tmp/…）不再污染 repo（11-25 教訓：git add -A 掃進測試產物）
    os.makedirs(trail, exist_ok=True)
    json.dump(rep, open(os.path.join(trail, "pit-status.json"), "w"), ensure_ascii=False, indent=1)
    print(f"pit-status: {rep['completed_eps']}/{rep['official_available']} "
          f"completion={rep['completion']} missing={rep['true_missing']} "
          f"retired={rep['retired_count'] if rep['retired_count'] is not None else '?'}")


if __name__ == "__main__":
    main()
