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


def rj(p, d=None):
    try:
        return json.load(open(p))
    except Exception:
        return d


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
        "notes": "completion = distinct official episodes in done ledger / available catalog; parts inflate article counts",
    }
    os.makedirs("trail", exist_ok=True)
    json.dump(rep, open("trail/pit-status.json", "w"), ensure_ascii=False, indent=1)
    print(f"pit-status: {rep['completed_eps']}/{rep['official_available']} "
          f"completion={rep['completion']} missing={rep['true_missing']}")


if __name__ == "__main__":
    main()
