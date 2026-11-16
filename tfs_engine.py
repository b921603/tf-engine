#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TFS engine — The Tim Ferriss Show → Eudic 每日英語聽力 semi-permanent pipeline.
Derived from the proven htf_engine (BBR-era). Multi-channel, AI-subtitle-first,
live-fetch rotation, password auto-relogin. See docs/DESIGN.md.
"""
import argparse
import difflib
import gzip
import html
import io
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

import requests

# ─────────────────────────── configuration ───────────────────────────
EUDIC = "https://my.eudic.net"
CH_PREFIX = "The Tim Ferriss Show"
RSS = "https://rss.art19.com/tim-ferriss-show"
PS_BASE = "https://podscripts.co/podcasts/the-tim-ferriss-show"   # legacy verify
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0"}
SEG = 3600                      # eudic single-article audio limit (s)

GAP = 2.4                       # min seconds between eudic requests
ROT_BUDGET = int(os.environ.get("ROT_BUDGET", "1400"))   # rotate run budget (s)

BASE = os.environ.get("TFS_BASE", os.path.join(os.getcwd(), "state"))
ROTDIR = os.environ.get("TFS_ROTDIR", BASE)   # per-shard rotate state (cache-isolated)
os.makedirs(BASE, exist_ok=True)
os.makedirs(ROTDIR, exist_ok=True)

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()

FINE_TS = re.compile(r"^\d{1,2}:\d{2}([.:]\d{1,3})?$")    # bare "00:00" chips
PS_RE = re.compile(r'data-seconds="(\d+)"[^>]*>(.*?)</span>', re.S)


def log(*a):
    s = " ".join(str(x) for x in a)
    print(f"[{time.strftime('%F %T')}] {s}", flush=True)


def ntfy(title, body, prio=3):
    if not NTFY_TOPIC:
        log("(ntfy disabled — no topic)")
        return
    try:
        requests.post("https://ntfy.sh/", timeout=30, data=json.dumps({
            "topic": NTFY_TOPIC, "title": title[:80], "message": body,
            "priority": prio, "tags": ["robot"]}).encode(),
            headers={"Content-Type": "application/json"})
        log(f"→ ntfy: {title}")
    except Exception as e:
        log(f"ntfy err: {str(e)[:80]}")


# ─────────────────────────── eudic HTTP layer ───────────────────────────
class AuthError(Exception):
    pass


def _cookie_header():
    c = os.environ.get("EUDIC_COOKIE", "").strip()
    if not c:
        sys.exit("✗ EUDIC_COOKIE env missing — set it to the raw Cookie header "
                 "from a logged-in my.eudic.net session (see README)")
    return c


SES = requests.Session()
SES.headers.update({"Cookie": _cookie_header(),
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{EUDIC}/ting/index",
                    "Origin": EUDIC, **UA})

_last_req = [0.0]


def req(method, url, *, data=None, files=None, timeout=60, tries=6, auth_check=True):
    """Rate-gated, WAF-tolerant request. SSL EOF / conn resets → backoff retry."""
    for i in range(tries):
        wait = _last_req[0] + GAP - time.time()
        if wait > 0:
            time.sleep(wait + 0.15)
        _last_req[0] = time.time()
        try:
            r = SES.request(method, url, data=data, files=files, timeout=timeout)
            if auth_check and r.status_code in (302, 401):
                raise AuthError(f"HTTP {r.status_code} — cookie expired?")
            txt = r.text if not files else (r.text or "")
            if auth_check and r.status_code == 200 and "EudicWebSession" in txt[:4000] \
                    and "uploadprivatemedia" not in url:
                raise AuthError("redirected to login — cookie expired?")
            return r
        except AuthError:
            raise
        except Exception as e:
            if i == tries - 1:
                raise
            log(f"  http retry {i}: {type(e).__name__} {str(e)[:70]}")
            time.sleep(min(4 + 5 * i, 25))
    raise RuntimeError("unreachable")


def eudic_upload(kind, filename, payload, mime):
    """UploadTingFile → {success, fileId}. kind ∈ thumb|media.
    GitHub runners sit far from eudic — measured uplink ~23 KB/s (Azure→CN),
    so a 32 kbps episode (~12 MB) needs ~8-9 min: media gets a long per-try
    socket timeout and fewer tries to fit the workflow budget."""
    files = {"file": (filename, payload, mime)}
    big = kind != "thumb"
    timeout, tries = ((int(os.environ.get("TFS_MEDIA_TIMEOUT", "900")), 2)
                      if big else (180, 6))
    for i in range(tries):
        try:
            r = req("POST", f"{EUDIC}/ting/UploadTingFile?type={kind}",
                    files=files, timeout=timeout)
            j = json.loads(r.text or "{}")
            if j.get("success") and (j.get("fileId") or j.get("result")):
                return j.get("fileId") or j.get("result").split("/")[-1]
            log(f"  upload try{i}: {(r.text or '')[:100]}")
        except AuthError:
            raise
        except Exception as e:
            log(f"  upload try{i} err: {str(e)[:70]}")
        time.sleep(min(3 + 3 * i, 18))
    return None


TFS_DESC = "Tim Ferriss 深度訪談全集（2014–），自官方 RSS 全量同步，含 AI 逐字稿字幕。"


def channel_list():
    r = req("GET", f"{EUDIC}/ting/GetPrivateChannels", timeout=45)
    return r.json()["channelList"]["UploadedChannels"]


def channel_cid(pref=""):
    pref = pref or CH_PREFIX
    for c in channel_list():
        if pref in html.unescape(c["Name"]):
            return c["Id"]
    raise RuntimeError(f"channel '{pref}' not found — run: tfs.py channels ensure")


CH_SIZE = 100                  # episodes per channel


def ch_range_for(num):
    a = ((max(num, 1) - 1) // CH_SIZE) * CH_SIZE + 1
    return a, a + CH_SIZE - 1


def ch_name(a, b):
    return f"{CH_PREFIX} #{a:03d}-{b:03d}"


def ep_num(title):
    t = title or ""
    m = re.match(r"\s*#(\d+)", t)
    if m:
        return int(m.group(1))
    m = re.match(r"\s*Ep(?:isode)?\.?\s*(\d+)", t, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)", t)
    return int(m.group(1)) if m else 0


def tfs_channels():
    """sorted [(a, b, cid)] for all deployed TFS range channels."""
    out = []
    for c in channel_list():
        nm = html.unescape(c["Name"])
        m = re.match(rf"^{re.escape(CH_PREFIX)} #0*(\d+)-0*(\d+)$", nm)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), c["Id"]))
    out.sort()
    return out


def cid_for(num):
    a, b = ch_range_for(num)
    pref = ch_name(a, b)
    for c in channel_list():
        if html.unescape(c["Name"]) == pref:
            return c["Id"]
    raise RuntimeError(f"channel '{pref}' missing — run: tfs.py channels ensure")


def ensure_channels(dry=False):
    """Create every TFS range channel needed to cover the RSS ep-number span
    (IsBannedWords precheck → CreatePrivateChannel)."""
    rss = rss_items()
    nums = sorted({ep_num(e["title"]) for e in rss if e["url"] and ep_num(e["title"])})
    hi = max(nums) if nums else 0
    need = [ch_range_for(n) for n in range(1, hi + 1, CH_SIZE)]
    have = {html.unescape(c["Name"]) for c in channel_list()}
    made = []
    for a, b in need:
        nm = ch_name(a, b)
        if nm in have:
            continue
        if dry:
            print(f"[dry] would create channel {nm}")
            continue
        try:
            r = req("POST", f"{EUDIC}/ting/IsBannedWords",
                    data={"name": nm, "channelId": ""}, timeout=30)
            if r.json().get("success") is False:
                raise RuntimeError(f"channel name rejected by IsBannedWords: {nm}")
        except RuntimeError:
            raise
        except Exception:
            pass
        r = req("POST", f"{EUDIC}/ting/CreatePrivateChannel",
                data={"name": nm, "thumb": "", "channelType": "",
                      "description": TFS_DESC}, timeout=45)
        ok = r.status_code == 200
        made.append(nm)
        log(f"  create {nm}: {r.status_code} {(r.text or '')[:60]}")
        time.sleep(GAP)
    return made



def list_articles(cid):
    """title → {id} (both sort orders; the list API has a known -20 offset quirk)."""
    arts = {}
    for sb in (0, 1):
        for pg in range(24):
            r = req("GET", f"{EUDIC}/ting/GetPrivateMedias"
                           f"?sortby={sb}&page={pg}&channelId={cid}", timeout=45)
            if not r.text:
                break
            got = 0
            for x in re.split(r'<div class="dragBox">', r.text)[1:]:
                mid = re.search(r'media_id"[^>]*data="([^"]+)"', x)
                tm = re.search(r'rl-title"[^>]*title="([^"]*)"', x)
                if tm:
                    arts[html.unescape(tm.group(1))] = mid.group(1) if mid else ""
                    got += 1
            if not got:
                break
    return arts


def norm_title(s):
    s = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", s or "")
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-")]:
        s = s.replace(a, b)
    return re.sub(r"[^0-9a-z一-鿿]+", "", s.lower())


def loose_hit(nt, have_norms):
    """eudic truncates long titles & appends（i/n）→ exact OR prefix/fuzzy match."""
    if nt in have_norms:
        return True
    cands = [h for h in have_norms
             if h[:14] == nt[:14] or (len(nt) > len(h) and h and nt.startswith(h))]
    if not cands:
        return False
    for h in cands:
        r = difflib.SequenceMatcher(None, h, nt[:max(len(h), 1)]).quick_ratio()
        if r >= 0.85:
            return True
    return False


def seed_done():
    """Fresh state → mark every RSS episode that already exists in any TFS
    channel (exact or loose) as done. Returns (done, suspect)."""
    arts = {}
    for _a, _b, c in tfs_channels():
        arts.update(list_articles(c))
    have = {norm_title(t) for t in arts}
    done = {}
    suspect = []
    for e in rss_items():
        nt = norm_title(e["title"])
        if not e["url"]:
            continue
        if nt in have or loose_hit(nt, have):
            done[e["guid"]] = {"title": e["title"], "at": time.strftime("%F %T"),
                               "how": "seed-channel"}
        else:
            suspect.append(e["title"])
    return done, suspect


def get_edit_page(cid, mid):
    r = req("GET", f"{EUDIC}/Ting/upload?channelid={cid}&mediaid={mid}&itemType=0",
            timeout=60)
    return (r.status_code, r.text) if r.status_code == 200 else (r.status_code, "")


def parse_edit_page(text):
    out = {}
    m = re.search(r'class="[^"]*media_name[^"]*"[^>]*value="([^"]*)"', text) or \
        re.search(r'name="title"[^>]*value="([^"]*)"', text)
    out["title"] = html.unescape(m.group(1)) if m else ""
    m = re.search(r'<textarea[^>]*id="media_textarea"[^>]*>(.*?)</textarea>', text, re.S)
    lrc = m.group(1) if m else ""
    lrc = lrc.replace("&#xD;", "\r").replace("&#xA;", "\n") \
             .replace("&#13;", "\r").replace("&#10;", "\n")
    out["lrc"] = html.unescape(lrc)
    m = re.search(r'class="[^"]*add_channel_textarea_last[^"]*"[^>]*>(.*?)</textarea>',
                  text, re.S)
    out["desc"] = html.unescape(m.group(1)).strip() if m else ""
    m = re.search(r'class="ueditor_text[^"]*"[^>]*>(.*?)</div>', text, re.S)
    out["intros"] = m.group(1).strip() if m else ""
    m = re.search(r'class="[^"]*text_add_img_last[^"]*"[^>]*data-fileid="([^"]*)"', text)
    if not m:
        m = re.search(r'data-fileid="([^"]+)"[^>]*class="[^"]*text_add_img_last', text)
    out["thumb_fid"] = m.group(1) if m else ""
    return out


def post_article(cid, mid, title, lrc, desc, thumb_fid, intros=""):
    """Full-field POST (the ONLY writer in this engine; lrc must be byte-identical)."""
    data = {"id": mid, "title": title, "channelId": cid,
            "thumb": thumb_fid or "", "lrc": lrc or "",
            "needProcessLrc": "false", "needKeepformat": "false",
            "reformatParagraph": "false", "splitLongSentence": "true",
            "needMachineTranslation": "false", "allowMachineTranslation": "false",
            "needAISubtitle": "false", "description": desc or "",
            "mediatype": "0", "intros": intros or "", "istiming": "false",
            "timing": "", "html": "", "insertImage": "true", "mdChapterId": ""}
    for k in range(10):
        try:
            r = req("POST", f"{EUDIC}/ting/uploadprivatemedia", data=data, timeout=90)
            if r.status_code == 200 and '"code":200' in (r.text or "").replace(" ", ""):
                return True
            busy = "还在处理" in (r.text or "")
            log(f"  post try{k}: {r.status_code} {(r.text or '')[:80]}")
        except AuthError:
            raise
        except Exception as e:
            busy = False
            log(f"  post try{k} err: {str(e)[:70]}")
        # server still transcoding a freshly-created media: wait patiently —
        # fast uploads (BBR) reach the edit step before the resource is ready
        time.sleep(45 if busy else 10 + 10 * k)
    return False


# ─────────────────────────── AI subtitle + login helpers ───────────────────────────
AI_RE = re.compile(r"\[\d{2}:\d{2}:\d{2}[.,]\d{1,3}\]\[\d{2}:\d{2}:\d{2}[.,]\d{1,3}\]")
SUB_SOURCE = os.environ.get("TFS_SUB_SOURCE", "ai")   # ai | official | podscripts
AI_WAIT = int(os.environ.get("TFS_AI_WAIT", "900"))               # seconds to poll AI gen


def strip_ts(line):
    return re.sub(r"(\[[^\]]*\])+", "", line).strip()


def batch_ai_subtitle(cid, mids):
    """Queue eudic AI subtitles. Empirical (2026-11-16): freshly-created
    articles transiently return the '时长超过 60 分钟' template even when
    eligible (server transcode/queue window) — patient-retry resolves it
    (10 × 45 s, same pattern as the create-transcode window). Returns True
    only when every round failed (treated as unsupported)."""
    last = ""
    for k in range(10):
        try:
            r = req("POST", f"{EUDIC}/ting/BatchAiSubtitle",
                    data={"mediaIds": mids, "channelId": cid}, timeout=60)
            last = (r.text or "")[:400]
            # NOTE: batch responses can be per-resource: "…无法创建 AI 字幕，
            # 其余已成功创建任务" means SOME parts queued → still a win.
            if 'success":true' in last or "其余已成功" in last:
                log(f"  BatchAiSubtitle {len(mids)} → queued (round {k})")
                return False
        except Exception as ex:
            last = str(ex)[:120]
        time.sleep(45)
    log(f"  BatchAiSubtitle {len(mids)} → {last[:140]}")
    return True


def batch_highlight(cid, mids):
    r = req("POST", f"{EUDIC}/ting/BatchHighlightSubtitle",
            data={"mediaIds": mids, "channelId": cid}, timeout=60)
    log(f"  BatchHighlightSubtitle {len(mids)} → {r.status_code} {(r.text or '')[:100]}")


def official_transcript_lines(url):
    """Parse an official transcript file (RSS podcast:transcript) to plain
    sentence lines. SRT/VTT → drop cue lines; JSON → segment text; else treat
    as plain text. Returns [] when unusable."""
    try:
        r = requests.get(url, headers=UA, timeout=60)
        txt = r.text or ""
        if url.lower().endswith(".json") or txt.lstrip()[:1] in "{[":
            try:
                j = json.loads(txt)
                segs = (j.get("segments") or j.get("transcript") or
                        j.get("subtitles") or [])
                out = []
                for s in segs:
                    if isinstance(s, dict):
                        for k in ("text", "body", "content"):
                            if s.get(k):
                                out.append(str(s[k]).strip())
                                break
                    elif isinstance(s, str) and s.strip():
                        out.append(s.strip())
                return [x for x in out if x]
            except Exception:
                pass
        out = []
        for ln in txt.splitlines():
            ln = ln.strip()
            if not ln or re.match(r"^\d+$", ln) or ln.upper().startswith(("WEBVTT", "NOTE", "HEADER")):
                continue
            ln = re.sub(r"^\[[^\]]*\]", "", ln)              # [00:00:00]
            ln = re.sub(r"^\d{2}:\d{2}(:\d{2})?[.,]\d*\s*-->\s*\S+\s*$", "", ln)
            ln = re.sub(r"<[^>]+>", "", ln).strip()
            if ln and not re.match(r"^\d{2}:\d{2}", ln):
                out.append(ln)
        return out
    except Exception as e:
        log(f"  official transcript fetch err: {str(e)[:70]}")
        return []


def find_created_mids(cid, full_title, nparts=1):
    """Title-lookup with list-lag retries → list of mids in part order."""
    for attempt in range(4):
        arts = list_articles(cid)
        mids = []
        for i in range(1, nparts + 1):
            t = full_title if nparts == 1 else f"{full_title}（{i}/{nparts}）"
            hit = next((k for k in arts if norm_title(k) == norm_title(t)), None)
            if hit and arts[hit]:
                mids.append(arts[hit])
        if len(mids) == nparts:
            return mids
        time.sleep(25 * (attempt + 1))
    return mids if mids else []


def eudic_login(user, pwd):
    """Password login → raw Cookie header for my.eudic.net (the auto-refresh
    path). Verified against GetPrivateChannels before returning."""
    s = requests.Session()
    s.headers.update({**UA, "Referer": "https://dict.eudic.net/account/login"})
    lp = s.get("https://dict.eudic.net/account/login",
               params={"returnUrl": "http://my.eudic.net/Account/Login"}, timeout=45)
    if "input-username" not in lp.text:
        raise RuntimeError(f"login page unexpected: HTTP {lp.status_code}")
    r = s.post("https://dict.eudic.net/Account/Login",
               params={"returnUrl": "http://my.eudic.net/Account/Login"},
               data={"UserName": user, "Password": pwd, "returnUrl":
                     "http://my.eudic.net/Account/Login", "RememberMe": "true"},
               timeout=45, allow_redirects=True)
    ck = {}
    for c in s.cookies:
        if c.domain.endswith(("eudic.net", ".eudic.net")) and c.name not in (
                "__cf_bm", "cf_clearance", "__cf_chl"):
            ck[c.name] = c.value
    raw = "; ".join(f"{k}={v}" for k, v in sorted(ck.items()))
    if not raw or "EudicWebSession" not in raw:
        raise RuntimeError(f"login did not yield a session cookie "
                           f"(HTTP {r.status_code}; url={r.url[:80]})")
    check = requests.get(f"{EUDIC}/ting/GetPrivateChannels",
                         headers={"Cookie": raw, "X-Requested-With": "XMLHttpRequest",
                                  "Referer": f"{EUDIC}/ting/index", **UA}, timeout=45)
    if check.status_code != 200 or "UploadedChannels" not in check.text:
        raise RuntimeError(f"cookie verify failed: HTTP {check.status_code}")
    return raw


def update_cookie_secret(raw):
    """Write the refreshed cookie back into the repo secret EUDIC_COOKIE
    (needs GH_PAT with Secrets:write)."""
    pat, repo = os.environ.get("GH_PAT", ""), os.environ.get("GH_REPO",
                                                              "b921603/htf-sync")
    if not pat:
        log("  GH_PAT missing — secret not updated (cookie valid for this run only)")
        return False
    from nacl import encoding, public as _pub
    import base64 as _b64
    pk = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                      headers={"Authorization": f"Bearer {pat}"}, timeout=30).json()
    box = _pub.SealedBox(_pub.PublicKey(pk["key"].encode(), encoding.Base64Encoder()))
    enc = box.encrypt(raw.encode())
    r = requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/EUDIC_COOKIE",
                     headers={"Authorization": f"Bearer {pat}"},
                     json={"encrypted_value": _b64.b64encode(enc).decode(),
                           "key_id": pk["key_id"]}, timeout=30)
    log(f"  secret EUDIC_COOKIE updated → {r.status_code}")
    return r.status_code in (201, 204)


def auto_relogin():
    """Cookie expired → password re-login (EUDIC_USER/EUDIC_PASS), then patch
    this run's session and the repo secret. True = continue working."""
    u, p = os.environ.get("EUDIC_USER", ""), os.environ.get("EUDIC_PASS", "")
    if not u or not p:
        return False
    log("auth failed — attempting password re-login …")
    try:
        raw = eudic_login(u, p)
        SES.headers["Cookie"] = raw
        try:
            update_cookie_secret(raw)
        except Exception as e:
            log(f"  secret update err: {str(e)[:70]}")
        ntfy("TFF GA: cookie auto-refreshed",
             "🔄 cookie 過期已由帳號密碼自動重新登入修復，同步繼續。", 3)
        return True
    except Exception as e:
        log(f"  re-login failed: {str(e)[:120]}")
        return False


# ─────────────────────────── sources ───────────────────────────
_RSS_MEMO = {"ts": 0.0, "items": None}


# ─── co-worker claims: git-as-queue (GA oldest-first ⇄ box newest-first) ───
GH_REPO = os.environ.get("TFS_GH_REPO", "b921603/tff-sync")
CLAIM_TTL = 2 * 3600          # a claim older than this is expired


def _gh_headers():
    h = {"Accept": "application/vnd.github+json"}
    if os.environ.get("GH_PAT"):
        h["Authorization"] = "Bearer " + os.environ["GH_PAT"]
    return h


def fetch_claims():
    """guid → (worker, ts) from trail/sandbox_claims.jsonl (repo HEAD)."""
    url = f"https://raw.githubusercontent.com/{GH_REPO}/main/trail/sandbox_claims.jsonl"
    try:
        r = requests.get(url, headers=_gh_headers(), timeout=30)
        if r.status_code != 200 or not r.text.strip():
            return {}
        out = {}
        for line in r.text.splitlines():
            try:
                d = json.loads(line)
                out[d["guid"]] = (d.get("worker", "?"), float(d.get("ts", 0)))
            except Exception:
                continue
        return out
    except Exception:
        return {}


def write_claim(guid, worker=None):
    """Append a claim line and commit (1 retry after rebase)."""
    worker = worker or os.environ.get("TFS_WORKER", "ga")
    api = f"https://api.github.com/repos/{GH_REPO}/contents/trail/sandbox_claims.jsonl"
    line = json.dumps({"guid": guid, "worker": worker, "ts": time.time()},
                      ensure_ascii=False) + "\n"
    for attempt in (1, 2):
        gr = requests.get(api + "?ref=main", headers=_gh_headers(), timeout=30)
        if gr.status_code == 200:
            body = base64.b64decode(gr.json().get("content", "") or "").decode("utf-8", "ignore")
            body += line
            sha = gr.json().get("sha")
        elif gr.status_code == 404:
            body, sha = line, None
        else:
            time.sleep(3); continue
        pr = requests.put(api, headers=_gh_headers(), timeout=60, json={
            "message": f"claim: {guid[:24]} by {worker}",
            "content": base64.b64encode(body.encode()).decode(),
            "sha": sha, "branch": "main"})
        if pr.status_code in (200, 201):
            return True
        time.sleep(4)
    return False


def is_claimed_elsewhere(guid, claims=None):
    claims = fetch_claims() if claims is None else claims
    if guid not in claims:
        return False
    worker, ts = claims[guid]
    return worker != os.environ.get("TFS_WORKER", "ga") and time.time() - ts < CLAIM_TTL


def rss_items():
    """[{title,guid,ts,date,url,image,duration,desc}] oldest-first (Art19 feed).
    Wrapped: in-process memo + state ETag/304 cache + stale fallback.
    Empirical (2026-11-16 run#4): the 30 MB feed double-fetched per step
    under WAF throttling stalls bootstrap 20+ min before its first log line."""
    now = time.time()
    if _RSS_MEMO["items"] and now - _RSS_MEMO["ts"] < 600:
        return _RSS_MEMO["items"]
    cache = rj(art_path("rss_cache.json"), {})
    hdr = dict(UA)
    if cache.get("etag"):
        hdr["If-None-Match"] = cache["etag"]
    if cache.get("lastmod"):
        hdr["If-Modified-Since"] = cache["lastmod"]
    try:
        r = requests.get(RSS, headers=hdr, timeout=300)
        if r.status_code == 304 and cache.get("items"):
            _RSS_MEMO.update(ts=now, items=cache["items"])
            log("  rss 304 — cached catalog reused")
            return cache["items"]
        txt = r.text
        items = _parse_rss(txt)
        wj(art_path("rss_cache.json"), {
            "etag": r.headers.get("ETag", ""),
            "lastmod": r.headers.get("Last-Modified", ""),
            "ts": now, "items": items})
        _RSS_MEMO.update(ts=now, items=items)
        return items
    except Exception as ex:
        if cache.get("items"):
            log(f"  rss fetch err ({str(ex)[:60]}) — stale cache fallback")
            _RSS_MEMO.update(ts=now, items=cache["items"])
            return cache["items"]
        raise


def _parse_rss(txt):
    items = []
    for block in re.findall(r"<item>(.*?)</item>", txt, re.S):
        def g(tag, blk=block):
            m = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
                          blk, re.S)
            return html.unescape(m.group(1)).strip() if m else ""
    items = []
    for block in re.findall(r"<item>(.*?)</item>", txt, re.S):
        def g(tag, blk=block):
            m = re.search(rf"<{tag}[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
                          blk, re.S)
            return html.unescape(m.group(1)).strip() if m else ""
        title = g("title")
        if not title:
            continue
        enc = re.search(r'<enclosure[^>]*url="([^"]+)"', block)
        img = re.search(r'<itunes:image[^>]*href="([^"]+)"', block)
        dm = re.search(r"<itunes:duration>([^<]+)</itunes:duration>", block)
        dur = 0
        if dm:
            pp = dm.group(1).split(":")
            try:
                dur = int(pp[-1]) + 60 * int(pp[-2]) + (3600 * int(pp[-3]) if len(pp) > 2 else 0)
            except Exception:
                dur = 0
        pub = g("pubDate")
        try:
            import email.utils
            ts = email.utils.parsedate_to_datetime(pub).timestamp()
        except Exception:
            ts = 0
        items.append({"title": title, "guid": g("guid"), "ts": ts,
                      "date": time.strftime("%Y-%m-%d", time.gmtime(ts)) if ts else "",
                      "url": enc.group(1) if enc else "",
                      "image": img.group(1) if img else "", "duration": dur,
                      "desc": g("description")})
    items.sort(key=lambda e: e["ts"] or 0)
    return items



def ps_slugs():
    p = f"{BASE}/ps_slugs.json"
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    found, seen = [], set()
    for pg in range(1, 30):
        try:
            r = requests.get(f"{PS_BASE}?page={pg}", headers=UA, timeout=45)
        except Exception:
            continue
        if r.status_code != 200:
            break
        links = set(re.findall(
            r'href="(/podcasts/how-to-fail-with-elizabeth-day/[a-z0-9\-]+)"', r.text))
        new = [l.rsplit("/", 1)[-1] for l in links
               if l.rsplit("/", 1)[-1] not in seen]
        for x in new:
            seen.add(x)
            found.append(x)
        if not links:
            break
    json.dump(found, open(p, "w", encoding="utf-8"))
    log(f"podscripts slugs: {len(found)}")
    return found


def _base(s):
    s = unicodedata.normalize("NFKD", s or "").lower()
    for a, b in [("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-")]:
        s = s.replace(a, b)
    return s


def _keys(s):
    b = _base(s)
    b2 = re.sub(r"^s\d+[, ]*ep\d+[, ]*", "", b)
    b3 = re.sub(r"^howtofail[:\- ]*", "", b2)
    return {re.sub(r"[^a-z0-9]+", "", x) for x in (b, b2, b3)} - {""}


def match_slug(title, slugs):
    idx = {}
    for s in slugs:
        for k in _keys(s):
            idx.setdefault(k, s)
    for k in _keys(title):
        if k in idx:
            return idx[k]
    cand = difflib.get_close_matches(_base(title), [_base(s) for s in slugs],
                                     n=1, cutoff=0.86)
    if cand:
        return next(s for s in slugs if _base(s) == cand[0])
    m = re.split(r"\s+[-–—:]\s+", _base(title))
    if m:
        gk = re.sub(r"[^a-z0-9]+", "", re.sub(r"^s\d+[, ]*ep\d+[, ]*", "", m[0]))
        gk = re.sub(r"^howtofail[:\- ]*", "", gk)
        if len(gk) >= 5:
            for s in slugs:
                if re.sub(r"[^a-z0-9]+", "", _base(s)).startswith(gk):
                    return s
    return ""


def transcript(title, slugs):
    """→ ([(sec, body)], slug). Bare timestamp chips filtered (11-13 site change)."""
    slug = match_slug(title, slugs)
    if not slug:
        return [], ""
    try:
        r = requests.get(f"{PS_BASE}/{slug}", headers=UA, timeout=45)
        if r.status_code == 200 and 'class="psb-ep__sentence"' in r.text:
            out = []
            for sec, body in PS_RE.findall(r.text):
                b = re.sub(r"<[^>]+>", "", body)
                b = html.unescape(b)
                b = re.sub(r"\s+", " ", b).strip()
                if b and not FINE_TS.match(b):
                    out.append((int(sec), b))
            if out:
                return out, slug
    except Exception as e:
        log(f"  transcript err {slug[:40]}: {str(e)[:60]}")
    return [], slug


def to_lrc(lines, s0, s1):
    out = []
    for t, body in lines:
        if s0 <= t < s1 + 1:
            r = t - s0
            out.append(f"[{r // 60:02d}:{r % 60:02d}.00]{body}")
    return "\n".join(out)


def full_lrc(lines):
    return "\n".join(f"[{t // 60:02d}:{t % 60:02d}.00]{b}" for t, b in lines)


# ─────────────────────────── description composer ───────────────────────────
SKIP_RE = re.compile(
    r"(brought to you by|sponsor|support of|squarespace|patreon|supportingcast|"
    r"advertis|promoted by|our sponsor|this episode is supported|discount code|"
    r"peek behind the curtain|listeners and viewers|community of subscribers|"
    r"subscribe|subscription|sign ?up|go to |link in the description|"
    r"failing with friends|on my youtube|on patreon|newsletter|episode notes|"
    r"show notes|see you there|join us|follow the link|welcome to|we will see you|"
    r"transcript|sponsor of)", re.I)
EP_KIND = re.compile(
    r"(BONUS EPISODE|MID-SEASON|LIVE|SPECIAL|TWIXMAS|F-Word|INTRO|TEASER|"
    r"TRAILER|REPLAY|ANNOUNCE)", re.I)
MARK = "【本集導讀】"


def strip_html(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def lrc_sentences(lrc):
    out = []
    for ln in (lrc or "").splitlines():
        ln = re.sub(r"(\[\d{2}:\d{2}:\d{2}[.,]\d{1,3}\])+", "", ln)
        ln = re.sub(r"(\[\d{1,2}:\d{2}(?:[.:]\d{1,3})?\])+", "", ln)
        ln = re.sub(r"^\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?(?:[.,]\d{1,3})?\s*", "", ln)
        if ln.strip():
            out.append(ln.strip())
    return out


def pick_opening(lrc):
    for s in lrc_sentences(lrc)[:70]:
        if SKIP_RE.search(s) or len(s) < 40:
            continue
        if re.match(r"^(hi|hello|hey|good morning|good evening|welcome)\b",
                    s.strip(), re.I) and len(s) < 70:
            continue
        return s
    return ""


def split_guest(title):
    t = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", title or "").strip()
    t = re.sub(r"^#\d+[:.:]?\s*", "", t)
    m = re.match(r"^(.{4,60}?)\s+[–—-]\s+(.+)$", t)
    if m and len(m.group(1)) < 60:
        return m.group(1).strip(), m.group(2).strip()
    return "", t.strip()



def compose_desc(title, lrc, rss_desc):
    guest, topic = split_guest(title)
    head = "Tim Ferriss 深度訪談節目《The Tim Ferriss Show》"
    if guest:
        head += f"｜來賓：{guest}"
    if topic:
        head += f"｜主題：{topic}"
    lines = [MARK + head]
    op = pick_opening(lrc)
    if op:
        op = op.replace("’", "'").replace("“", '"').replace("”", '"')
        if len(op) > 200:
            op = op[:200].rsplit(" ", 1)[0] + "…"
        lines.append(f"\n「{op}」")
    if lrc.strip():
        lines.append("\n本集附完整逐字稿字幕，時間軸對齊原音檔——可以邊聽邊跟，"
                     "複習時直接跳段落。")
    else:
        lines.append("\n本集為官方完整節目：Tim Ferriss 與頂尖來賓拆解其方法論、"
                     "日常慣例與工具，並討論如何實際應用到生活裡。")
    body = strip_html(rss_desc)
    return "\n".join(lines) + (("\n\n" + body) if body else "")

# ─────────────────────────── audio pipeline ───────────────────────────
def ffpath():
    p = shutil_ff()
    if not p:
        p = ff_bootstrap()
    if not p:
        sys.exit("✗ ffmpeg not found and bootstrap download failed")
    return p


def ff_bootstrap():
    """Runner images may lack ffmpeg (ubuntu-26.04 dropped it) — fetch a
    static build once per run (~40 MB, seconds on runners)."""
    import tarfile, tempfile, urllib.request
    tgt = os.path.join(tempfile.gettempdir(), "htf_ffmpeg", "ffmpeg")
    if os.path.isfile(tgt):
        return tgt
    url = ("https://johnvansickle.com/ffmpeg/releases/"
           "ffmpeg-release-amd64-static.tar.xz")
    try:
        log("ffmpeg missing — fetching static build …")
        d = os.path.dirname(tgt)
        os.makedirs(d, exist_ok=True)
        t0 = os.path.join(d, "ff.tar.xz")
        urllib.request.urlretrieve(url, t0)
        with tarfile.open(t0) as tf:
            for m in tf.getmembers():
                if m.name.endswith("/ffmpeg"):
                    with open(tgt, "wb") as f:
                        f.write(tf.extractfile(m).read())
                    break
        os.chmod(tgt, 0o755)
        log(f"ffmpeg ready: {tgt}")
        return tgt
    except Exception as e:
        log(f"ffmpeg bootstrap failed: {e}")
        return None


def shutil_ff():
    import shutil
    return shutil.which("ffmpeg")


def prep_audio(url, key):
    """download → 32k mono → [(path, s0, dur)] (≤3600s parts)."""
    tmp = os.path.join(BASE, "audio")
    os.makedirs(tmp, exist_ok=True)
    src = os.path.join(tmp, f"{key}.mp3")
    conv = os.path.join(tmp, f"{key}_32k.mp3")
    if not (os.path.isfile(conv) and os.path.getsize(conv) > 10000):
        last = None
        for i in range(4):
            try:
                r = requests.get(url, headers=UA, timeout=600, stream=True)
                r.raise_for_status()
                with open(src, "wb") as f:
                    for ch in r.iter_content(1 << 20):
                        f.write(ch)
                if os.path.getsize(src) > 10000:
                    last = None
                    break
            except Exception as e:
                last = e
                log(f"  download retry {i}: {str(e)[:60]}")
                time.sleep(5 + 8 * i)
        if last is not None:
            log(f"  ✗ download failed: {str(last)[:80]}")
            return []
        subprocess.run([ffpath(), "-y", "-i", src, "-vn", "-ac", "1",
                        "-b:a", "32k", "-ar", "32000", conv],
                       capture_output=True, timeout=1800)
        try:
            os.remove(src)
        except Exception:
            pass
    import subprocess as sp
    out = sp.run([ffpath(), "-i", conv], capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)", out)
    if not m:
        return []
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    if dur <= SEG:
        return [(conv, 0, dur)]
    parts = []
    n = dur // SEG + (1 if dur % SEG else 0)
    for i in range(n):
        s0 = i * SEG
        pp = os.path.join(tmp, f"{key}_p{i+1}.mp3")
        if not os.path.isfile(pp) or os.path.getsize(pp) < 10000:
            sp.run([ffpath(), "-y", "-ss", str(s0), "-t", str(min(SEG, dur - s0)),
                    "-i", conv, "-ac", "1", "-b:a", "32k", "-ar", "32000", pp],
                   capture_output=True, timeout=1800)
        if os.path.isfile(pp) and os.path.getsize(pp) > 10000:
            parts.append((pp, s0, min(SEG, dur - s0)))
    return parts


# ─────────────────────────── state helpers ───────────────────────────
def rj(p, d=None):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return d


def wj(p, o):
    tmp = p + ".tmp"
    json.dump(o, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, p)


def art_path(name):
    return os.path.join(BASE, name)


def ledger_append(rec):
    with open(art_path("sync_ledger.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def audio_lost():
    """guid → reason, for episodes officially purged (no audio anywhere).
    The engine skips them WITHOUT fabricating media (HTF precedent)."""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "recovered", "audio_lost.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def official_map():
    """norm_title → official artwork URL (RSS + baked extras)."""
    m = {}
    try:
        for e in rss_items():
            if e["image"]:
                m[norm_title(e["title"])] = e["image"]
    except Exception as e:
        log(f"rss map err: {e}")
    try:
        ex = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "extras_art.json"), encoding="utf-8"))
        for t, v in ex.items():
            m.setdefault(norm_title(t), v["image"])
    except Exception:
        pass
    return m


# ─────────────────────────── job: auth ───────────────────────────
def job_auth():
    cid = channel_cid()
    arts = list_articles(cid)
    log(f"✓ auth OK ｜ channel {cid[:12]}… ｜ articles {len(arts)}")
    return cid


def job_login():
    """Manual/first-time password login: verifies, patches this run's session,
    and updates the repo secret EUDIC_COOKIE (needs GH_PAT)."""
    raw = eudic_login(os.environ.get("EUDIC_USER", ""),
                      os.environ.get("EUDIC_PASS", ""))
    SES.headers["Cookie"] = raw
    updated = False
    try:
        updated = update_cookie_secret(raw)
    except Exception as e:
        log(f"  secret update err: {str(e)[:80]}")
    cid = channel_cid()
    log(f"✓ login OK ｜ articles {len(list_articles(cid))} ｜ secret updated={updated}")
    ntfy("TFF GA: login OK",
         f"🔑 帳號密碼登入成功，secret {'已更新' if updated else '未更新（無 GH_PAT）'}。", 3)


# ─────────────────────────── job: sync ───────────────────────────
def raw_list(cid):
    """[(title, mid)] unkeyed — dup-safe (list_articles is title-keyed and
    silently merges same-title articles)."""
    out, seen = [], set()
    for sb in (0, 1):
        for pg in range(24):
            r = req("GET", f"{EUDIC}/ting/GetPrivateMedias"
                           f"?sortby={sb}&page={pg}&channelId={cid}", timeout=45)
            if not r.text:
                break
            got = 0
            for x in re.split(r'<div class="dragBox">', r.text)[1:]:
                tm = re.search(r'rl-title"[^>]*title="([^"]*)"', x)
                mid = re.search(r'media_id"[^>]*data="([^"]+)"', x)
                if tm and mid and mid.group(1) not in seen:
                    out.append((html.unescape(tm.group(1)), mid.group(1)))
                    seen.add(mid.group(1))
                    got += 1
            if not got:
                break
    return out


def count_channel_parts(cid, title):
    """How many parts of `title` already exist in the channel (any suffix)."""
    want = norm_title(title)
    return sum(1 for t, _m in raw_list(cid)
               if norm_title(loose_rss_title(t)) == want)


def expected_parts(e):
    d = e.get("duration") or 0
    return -(-d // SEG) if d else 0


def reconcile_done(done, items=None):
    """Mark RSS episodes done when all their parts already exist in any TFS
    channel but the guid is missing from done.json (heals state loss and
    partial crashes without dup risk)."""
    if items is None:
        items = rss_items()
    by_c = {}
    for _a, _b, c in tfs_channels():
        by_c[c] = raw_list(c)
    added, partial = 0, []
    for e in items:
        if not e["url"] or e["guid"] in done:
            continue
        want = norm_title(e["title"])
        exp = expected_parts(e)
        lst = by_c.get(cid_for(ep_num(e["title"]) or 1), [])
        have = sum(1 for t, _m in lst if norm_title(loose_rss_title(t)) == want)
        if exp and have >= exp:
            done[e["guid"]] = {"title": f"{e['date']} {e['title']}".strip(),
                               "at": time.strftime("%F %T"), "how": "reconcile"}
            added += 1
        elif have:
            partial.append(f"#{ep_num(e['title'])} {have}/{exp or '?'}")
    if added or partial:
        wj(art_path("done.json"), done)
        log(f"reconcile: +{added} done ｜ partial pending resume: {partial[:8]}")
    return added


def sync_one(e, done, omap, dry=False):
    """Full pipeline for one RSS episode: route → download → create →
    AI subtitle path → guard. Mutates `done`. Returns True if processed."""
    title = e["title"]
    num = ep_num(title) or 1
    try:
        cid = cid_for(num)
    except RuntimeError as ex:
        log(f"  ✗ {ex}")
        if not dry:
            ntfy("TFF GA: channel missing", f"⚠️ {ex}\n集數 {num} 本輪跳過。", 4)
        return False
    log(f"▶ [#{num} → {ch_name(*ch_range_for(num))}] {title[:70]}")
    lines, slug, official = [], "", ""
    if SUB_SOURCE == "podscripts":
        slugs = slugs or ps_slugs()
        lines, slug = transcript(title, slugs)
    elif e.get("transcript"):
        official = e["transcript"]
        lines = official_transcript_lines(official)
        if len(lines) < 5:                     # unusable file → AI path
            lines, official, slug = [], "", ""
        else:
            slug = "official"
    key = re.sub(r"[^A-Za-z0-9]+", "_", e["guid"])[:40] or str(int(e["ts"]))
    if e["guid"] in audio_lost():
        log(f"  ⊘ audio officially lost (no fabrication) — skipped, cataloged")
        ledger_append({"ts": time.strftime("%F %T"), "title": title,
                       "how": "audio-lost-skip", "guid": e["guid"]})
        done.setdefault(e["guid"], {"title": f"{e['date']} {title}".strip(),
                                    "at": time.strftime("%F %T"),
                                    "how": "audio-lost", "cid": cid})
        wj(art_path("done.json"), done)
        return False
    parts = prep_audio(e["url"], key)
    if not parts:
        ledger_append({"ts": time.strftime("%F %T"), "title": title,
                       "how": "audio-failed"})
        ntfy("TFF GA: audio failed", f"⚠️ 音檔下載/轉檔失敗：《{title[:60]}》（#{num}）", 4)
        return False
    # official cover — immediately-then-use (fid GC ~80 s)
    thumb = ""
    img_url = e["image"] or omap.get(norm_title(title), "")
    if img_url and not dry:
        try:
            img = requests.get(img_url, headers=UA, timeout=60).content
            if img:
                thumb = eudic_upload("thumb", f"{key}.jpg", img, "image/jpeg") or ""
                log(f"  cover fid={thumb[:40]}")
        except Exception as ex:
            log(f"  cover err: {str(ex)[:70]}")
    flrc = full_lrc(lines)
    desc = compose_desc(title, flrc, e["desc"])
    date = e["date"]
    ok_all = True
    media_first = ""
    # crash-resume: skip parts that already exist in the channel
    exp = expected_parts(e)
    have = count_channel_parts(cid, title)
    if exp and have >= exp:
        done[e["guid"]] = {"title": f"{date} {title}".strip(),
                           "at": time.strftime("%F %T"), "cid": cid,
                           "how": "already-complete"}
        wj(art_path("done.json"), done)
        log(f"  ⊚ all {have} parts already exist — marked done")
        return False
    skip = have if (exp and have) else 0
    if skip:
        log(f"  ↻ resume: skipping existing parts 1..{skip} (have {have}/{exp or '?'})")
    for i, (pf, s0, plen) in enumerate(parts):
        if i < skip:
            continue
        t = title if len(parts) == 1 else f"{title}（{i+1}/{len(parts)}）"
        full = f"{date} {t}".strip()
        if dry:
            log(f"  【dry】part {i+1}/{len(parts)} {plen}s ｜ "
                f"lrc={len(lines)}句 ｜ desc={len(desc)}字 ｜ title={full[:60]}"
                f" ｜ cid={cid}")
            continue
        fid = eudic_upload("media", f"{key}_{i+1}.mp3",
                           open(pf, "rb").read(), "audio/mpeg")
        if not fid:
            ok_all = False
            ledger_append({"ts": time.strftime("%F %T"), "title": title,
                           "how": "media-upload-failed", "part": i + 1})
            return False
        media_first = media_first or fid
        lrc_part = to_lrc(lines, s0, s0 + plen)
        data = {"id": "", "title": full, "channelId": cid, "thumb": thumb,
                "media": fid, "voice": "", "textAudioMode": "standard",
                "podcastVoices": "", "podcastSpeakers": "", "lrc": lrc_part,
                "needProcessLrc": "true", "needKeepformat": "false",
                "reformatParagraph": "true", "splitLongSentence": "true",
                "needMachineTranslation": "false",
                "needAISubtitle": "false" if lrc_part else "true",
                "description": desc[:3000], "mediatype": "0", "intros": "",
                "istiming": "false", "timing": "", "html": "",
                "insertImage": "false", "mdChapterId": ""}
        okp = False
        for k in range(6):
            try:
                r = req("POST", f"{EUDIC}/ting/uploadprivatemedia", data=data,
                        timeout=90)
            except AuthError:
                raise
            except Exception as ex:
                log(f"  create try{k}: net err {str(ex)[:70]}")
                time.sleep(20 + 15 * k)
                continue
            if r.status_code == 200 and '"code":200' in (r.text or "").replace(" ", ""):
                okp = True
                break
            log(f"  create try{k}: {r.status_code} {(r.text or '')[:70]}")
            time.sleep(20 + 15 * k)
        ok_all = ok_all and okp
        log(f"  ✔ part {i+1}/{len(parts)} create={'OK' if okp else 'FAIL'}")
    if ok_all and parts:
        try:
            os.remove(parts[0][0])
        except Exception:
            pass
    if dry:
        src = ("official 字幕檔" if official else
               f"podscripts {len(lines)} 句" if lines else "歐路 AI 字幕（政策）")
        log(f"  【dry】summary: {len(parts)} parts ｜ subtitle={src}")
        return False
    full_title = f"{date} {title}".strip()
    if ok_all and not lines:
        # ── AI path (2026-11-15 policy: official file > eudic AI; no
        #    third-party transcripts). Create already ran with
        #    needAISubtitle=true + empty lrc → queue AI server-side, poll,
        #    enrich desc from AI text, re-post byte-identical, highlight.
        mids = find_created_mids(cid, full_title, len(parts))
        long60, lrc_ok = False, False
        if mids:
            long60 = batch_ai_subtitle(cid, mids)
        stage = "queued"
        if long60 or not mids:
            # transient AI queue window → RETRYABLE (subsync requeues ≤4 rounds)
            stage = "queued"
            long60 = False
            if not mids:
                stage = "no-mids"
        else:
            t0 = time.time()
            infos = []
            while time.time() - t0 < AI_WAIT:
                infos = []
                for mid in mids:
                    stt, txt = get_edit_page(cid, mid)
                    infos.append(parse_edit_page(txt)
                                 if stt == 200 and txt else {})
                    time.sleep(1)
                if all(AI_RE.search((i.get("lrc") or "")) for i in infos):
                    lrc_ok = True
                    break
                time.sleep(30)
            if lrc_ok:
                stage = "highlighted"
                for mid, info in zip(mids, infos):
                    lrc = info["lrc"]
                    if MARK not in (info["desc"] or ""):
                        text = "\n".join(strip_ts(b) for b in lrc.splitlines())
                        nd = compose_desc(full_title, text, e["desc"])
                        post_article(cid, mid, info["title"] or full_title,
                                     lrc, nd, info["thumb_fid"], info["intros"])
                    batch_highlight(cid, [mid])
                    time.sleep(2)
                guard_inline(cid, title, [], "")
        done[e["guid"]] = {"title": full_title, "at": time.strftime("%F %T"),
                           "how": "ai", "ai_stage": stage, "mids": mids,
                           "cid": cid,
                           "ai_queued": bool(mids and not long60),
                           "rss_desc": (e["desc"] or "")[:500]}
        wj(art_path("done.json"), done)
        ledger_append({"ts": time.strftime("%F %T"), "title": title,
                       "how": "AI字幕" + ("＋高亮" if lrc_ok else "(排程中)"),
                       "parts": len(parts), "desc_len": len(desc)})
        sub = ("歐路 AI 字幕已生成＋高亮＋導讀" if stage == "highlighted" else
               "歐路 AI 字幕排程中（小時任務會自動完成導讀＋高亮）"
               if stage == "queued" else
               "⚠️ AI 字幕排程未果 → 本集暫無字幕（subsync 會接手）")
        ntfy("TFF GA: new episode",
             f"🆕 新集已同步：《{title[:64]}》\n🎙 字幕：{sub}\n"
             "🖼 封面：官方圖已套用\n"
             f"🎧 歐路 App → 聽力 → 頻道「{ch_name(*ch_range_for(num))}」")
        time.sleep(3)
        return False
    if ok_all:
        done[e["guid"]] = {"title": full_title,
                           "at": time.strftime("%F %T"), "cid": cid,
                           "how": "official" if official else "transcript"}
        wj(art_path("done.json"), done)
        ledger_append({"ts": time.strftime("%F %T"), "title": title,
                       "how": "official" if official else "transcript",
                       "lrc_lines": len(lines), "desc_len": len(desc),
                       "slug": slug, "parts": len(parts)})
        # inline guard: verify + repair + register for rotation
        guard_inline(cid, title, lines, slug)
        sub = (f"官方字幕檔 {len(lines)} 句" if official else
               f"podscripts 逐字稿 {len(lines)} 句")
        ntfy("TFF GA: new episode",
             f"🆕 新集已同步並通過驗收：《{title[:64]}》\n"
             f"🎙 字幕：{sub}\n🖼 封面：官方圖已套用\n"
             f"🎧 歐路 App → 聽力 → 頻道「{ch_name(*ch_range_for(num))}」")
    else:
        ledger_append({"ts": time.strftime("%F %T"), "title": title,
                       "how": "create-failed"})
        ntfy("TFF GA: create failed", f"⚠️ 建文章未完成：《{title[:60]}》"
             "（部分段落失敗）——將於下一輪由頻道清單複核", 4)
    time.sleep(3)
    return True


def job_sync(force="", dry=False, newest=False):
    items = rss_items()
    done = rj(art_path("done.json"), None)
    if done is None:
        done, suspect = seed_done()
        wj(art_path("done.json"), done)
        log(f"seed: {len(done)} episodes matched from channel ｜ "
            f"suspect {len(suspect)}: {suspect[:3]}")
        if suspect and os.environ.get("TFS_WORKER", "ga") == "ga":
            ntfy("TFF GA: seed suspects",
                 "首次部署種子完成；以下 RSS 集未能在頻道標題比對（請人工複核）：\n" +
                 "\n".join(t[:60] for t in suspect[:12]), prio=3)
    items = rss_items()
    try:
        reconcile_done(done, items)
    except Exception as ex:
        log(f"reconcile skipped: {str(ex)[:100]}")
    ct = rj(art_path("ct_cache.json"), {"ts": 0, "titles": []})
    if time.time() - ct.get("ts", 0) > 1800:
        arts = {}
        for _a, _b, c in tfs_channels():
            arts.update(list_articles(c))
        ct = {"ts": time.time(), "titles": sorted(arts)}
        wj(art_path("ct_cache.json"), ct)
    have_norms = {norm_title(t) for t in ct["titles"]}
    todo = []
    if force:
        todo = [e for e in items if force.lower() in e["title"].lower()]
    else:
        for e in items:
            if not e["url"]:
                continue
            nt = norm_title(e["title"])
            if e["guid"] in done or loose_hit(nt, have_norms):
                continue
            todo.append(e)
    if newest:
        todo.reverse()
    log(f"RSS {len(items)} ｜ done {len(done)} ｜ todo {len(todo)}")
    omap = official_map()
    slugs = None            # podscripts index — lazily fetched (legacy mode only)
    rc = rj(art_path("rotate_cache.json"), {})
    for e in todo:
        try:
            sync_one(e, done, omap, dry)
        except Exception as ex:
            log(f"  ✗ episode error (skip, retried next run): {str(ex)[:120]}")
            ledger_append({"ts": time.strftime("%F %T"), "title": e["title"],
                           "how": "episode-error", "err": str(ex)[:150]})
            wj(art_path("done.json"), done)
    wj(art_path("rotate_cache.json"), rc)
    log("sync done")


def guard_inline(cid, title, lines, slug):
    """Verify the freshly-created article (lrc/desc/thumb) and repair; register rotation."""
    time.sleep(20)
    arts = list_articles(cid)
    hit = next((t for t in arts if norm_title(t) == norm_title(title)), None)
    if not hit:
        log("  guard: article not listed yet (list lag) — skip verify")
        return
    mid = arts[hit]
    stt, txt = get_edit_page(cid, mid)
    if stt != 200 or not txt:
        log("  guard: edit page fetch failed")
        return
    info = parse_edit_page(txt)
    fixes = []
    lrc = info["lrc"]
    body_n = len([b for b in lrc.splitlines() if re.sub(r"(\[[^\]]*\])+", "", b).strip()])
    if lines and body_n < 5:
        lrc = full_lrc(lines)
        fixes.append("lrc rebuilt")
    desc = info["desc"]
    if MARK not in desc and lines:
        desc = compose_desc(hit, lrc, "")
        fixes.append("desc rebuilt")
    thumb = info["thumb_fid"]
    rc = rj(art_path("rotate_cache.json"), {})
    if not thumb:
        e = next((e for e in rss_items() if norm_title(e["title"]) == norm_title(title)), None)
        if e and e["image"]:
            try:
                img = requests.get(e["image"], headers=UA, timeout=60).content
                fid = eudic_upload("thumb", "guard.jpg", img, "image/jpeg")
                if fid:
                    thumb = fid
                    fixes.append("cover immediate-use")
            except Exception as ex:
                log(f"  guard cover err: {str(ex)[:70]}")
    if fixes:
        okp = post_article(cid, mid, info["title"] or hit, lrc, desc, thumb,
                           info["intros"])
        log(f"  guard fixes: {fixes} ｜ post={'OK' if okp else 'FAIL'}")
    rc[hit] = {"mid": mid, "title": hit, "lrc": lrc, "desc": desc,
               "intros": info["intros"], "cid": cid,
               "image": next((e["image"] for e in rss_items()
                              if norm_title(e["title"]) == norm_title(title)), "")}
    wj(art_path("rotate_cache.json"), rc)
    log(f"  guard: registered ｜ rotate_cache={len(rc)}")


# ─────────────────────────── job: subsync ───────────────────────────
def job_subsync():
    """Hourly AI-subtitle finishing queue (2026-11-15 policy). For episodes
    created via the AI path: retry title lookup, (re)queue AI when lrc is
    still empty, and when the server-side AI lrc is ready → enrich desc from
    the AI text → re-post byte-identical → highlight → register rotation."""
    done = rj(art_path("done.json"), {})
    changed = False
    for guid, rec in list(done.items()):
        if rec.get("how") != "ai" or rec.get("ai_stage") in (
                "highlighted", "failed"):
            continue
        title = rec.get("title", "")
        cid = rec.get("cid") or cid_for(ep_num(title) or 1)
        mids = rec.get("mids") or []
        if not mids:
            mids = find_created_mids(cid, title, 1)
            if mids:
                rec["mids"] = mids
                changed = True
            else:
                continue
        ready, waitq = [], []
        for mid in mids:
            stt, txt = get_edit_page(cid, mid)
            info = parse_edit_page(txt) if stt == 200 and txt else {}
            lrc = info.get("lrc", "")
            (ready if AI_RE.search(lrc) else waitq).append((mid, info, lrc))
            time.sleep(1)
        if waitq and any(not lrc.strip() for _m, _i, lrc in waitq):
            if not rec.get("ai_queued"):
                if batch_ai_subtitle(cid, [m for m, _i, lrc in waitq
                                           if not lrc.strip()]):
                    rec["ai_stage"] = "unsupported"
                    changed = True
                    continue
                rec["ai_queued"] = True
                changed = True
            rec["ai_tries"] = rec.get("ai_tries", 0) + 1
            if rec["ai_tries"] >= 4:
                rec["ai_stage"] = "failed"
                changed = True
                ntfy("TFF GA: AI subtitle failed",
                     f"⚠️ AI 字幕 4 輪未就緒（可能超長或不支援）：《{title[:60]}》\n"
                     "本集無字幕（誠實標注）；如需字幕請回覆訊息指定處理方式。", 3)
        for mid, info, lrc in ready:
            if mid in (rec.get("hl_mids") or []):
                continue
            if MARK not in (info["desc"] or ""):
                text = "\n".join(strip_ts(b) for b in lrc.splitlines())
                nd = compose_desc(title, text, rec.get("rss_desc", ""))
                post_article(cid, mid, info["title"] or title,
                             lrc, nd, info["thumb_fid"], info["intros"])
            batch_highlight(cid, [mid])
            rec.setdefault("hl_mids", []).append(mid)
            changed = True
        if len(rec.get("hl_mids") or []) == len(mids):
            rec["ai_stage"] = "highlighted"
            changed = True
            guard_inline(cid, title, [], "")
            ntfy("TFF GA: AI subtitle done",
                 f"✅ AI 字幕＋高亮＋導讀完成：《{title[:60]}》", 3)
    if changed:
        wj(art_path("done.json"), done)
    log("subsync pass done")


# ─────────────────────────── job: bootstrap ───────────────────────────
def job_bootstrap(limit=0, dry=False, reverse=False):
    """Drain the backlog oldest-first: full create pipeline for every RSS
    episode not yet in done.json. rotate_cache self-fills via guard_inline
    registrations inside sync_one (no separate edit-page pass needed)."""
    batch = limit or int(os.environ.get("TFS_BOOTSTRAP_BATCH", "15"))
    made = ensure_channels(dry)
    if made:
        log(f"channels ensured: +{len(made)} ({', '.join(made[:3])}…)" if len(made) > 3
            else f"channels ensured: +{len(made)}")
    items = rss_items()
    done = rj(art_path("done.json"), None)
    if done is None:
        done, suspect = seed_done()
        wj(art_path("done.json"), done)
        log(f"seed: {len(done)} episodes matched from channels ｜ suspect {len(suspect)}")
    omap = official_map()
    try:
        reconcile_done(done, items)
    except Exception as ex:
        log(f"reconcile skipped: {str(ex)[:100]}")
    undone = [e for e in items if e["url"] and e["guid"] not in done]
    if reverse:
        undone = list(reversed(undone))       # box drains from the newest side
    todo = undone[:batch]
    claims = fetch_claims()
    if claims:
        todo = [e for e in todo if not is_claimed_elsewhere(e["guid"], claims)]
        log(f"  claims honored: {len(claims)} active")
    log(f"bootstrap: {len(todo)} to build ({'newest-first' if reverse else 'oldest-first'}, "
        f"done {len(done)})")
    t0 = time.time()
    for i, e in enumerate(todo):
        if is_claimed_elsewhere(e["guid"]):
            log(f"  ⚭ {e['title'][:40]} claimed by co-worker — skip")
            continue
        if not dry:
            write_claim(e["guid"])
        try:
            sync_one(e, done, omap, dry)
        except Exception as ex:
            log(f"  ✗ episode error (skip, retried next run): {str(ex)[:120]}")
            ledger_append({"ts": time.strftime("%F %T"), "title": e["title"],
                           "how": "episode-error", "err": str(ex)[:150]})
            wj(art_path("done.json"), done)
        if time.time() - t0 > 5.2 * 3600:      # runner headroom
            log(f"bootstrap time budget reached at {i+1}/{len(todo)}")
            break
    log(f"bootstrap pass done: done total {len(done)}")


def loose_rss_title(ch_title):
    """Channel article title ('2014-04-18 Ep X（1/2）') → RSS-ish title for
    artwork lookup (strip date prefix + part suffix)."""
    t = re.sub(r"^\d{4}-\d{2}-\d{2}\s*", "", ch_title or "")
    t = re.sub(r"（\d+/\d+）\s*$", "", t)
    return t


def rss_image_for(ch_title, omap):
    return omap.get(norm_title(loose_rss_title(ch_title)), "")


def rotate_cache_backfill(limit=0):
    """Build rotate_cache for articles that already exist across all TFS
    channels (edit-page scan). This is the ROTATE self-heal path — distinct
    from job_bootstrap (which uploads the episode backlog)."""
    arts = {}
    for _a, _b, c in tfs_channels():
        for t, mid in list_articles(c).items():
            arts[t] = (c, mid)
    rc = rj(art_path("rotate_cache.json"), {})
    omap = official_map()
    live_mid = {t: m for t, (c, m) in arts.items()}
    todo = [t for t in sorted(arts)
            if t not in rc or rc[t].get("mid") != live_mid.get(t)
            or not rc[t].get("lrc")]
    if limit:
        todo = todo[:limit]
    log(f"rotate backfill: {len(todo)} to fetch (have {len(rc)})")
    for i, t in enumerate(todo):
        c, mid = arts[t]
        old_rec = rc.get(t) or {}
        stale = (old_rec.get("mid") != mid or not old_rec.get("lrc"))
        if not stale:
            rc[t]["mid"] = mid              # keep fresh pointer anyway
            continue
        stt, txt = get_edit_page(c, mid)
        if stt != 200 or not txt or len(txt) < 3000:
            log(f"  [{i+1}] ✗ {t[:44]}")
            continue
        info = parse_edit_page(txt)
        rc[t] = {"mid": mid, "cid": c, "title": t, "lrc": info["lrc"],
                 "desc": info["desc"], "intros": info["intros"],
                 "image": old_rec.get("image") or rss_image_for(t, omap)}
        if (i + 1) % 25 == 0:
            wj(art_path("rotate_cache.json"), rc)
            log(f"  [{i+1}/{len(todo)}] cached")
        time.sleep(0.3)
    wj(art_path("rotate_cache.json"), rc)
    log(f"rotate backfill done: {len(rc)} entries")


# ─────────────────────────── job: rotate ───────────────────────────
def shrink_bytes(b, maxside=1280, q=88):
    from PIL import Image
    im = Image.open(io.BytesIO(b))
    im = im.convert("RGB") if im.mode != "RGB" else im
    if max(im.size) > maxside:
        im.thumbnail((maxside, maxside), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=q, optimize=True)
    return out.getvalue()


def img_key(u):
    m = re.search(r"/image/([A-Za-z0-9_\-]+)\.(?:jpg|jpeg|png)", u or "")
    return m.group(1) if m else ""


def job_rotate(shard="0/1", budget=None):
    budget = budget or ROT_BUDGET
    n, m = (int(x) for x in shard.split("/"))
    rc = rj(art_path("rotate_cache.json"), {})
    # incremental self-bootstrap: fill ≤40 missing entries per run (~150s on
    # runners), so a fresh deployment completes its cache within ~8-11 hourly
    # runs. Shard 0 only — both shards share the cache file, fetching in
    # parallel would duplicate work and race the cache save.
    if os.environ.get("TFS_ROT_BOOTSTRAP", "1") == "1" and n == 0:
        try:
            arts = {}
            for _a, _b, c in tfs_channels():
                arts.update(list_articles(c))
            miss = [t for t in sorted(arts) if t not in rc]
            if miss:
                lim = int(os.environ.get("TFS_ROT_BOOTSTRAP_LIMIT", "40"))
                log(f"rotate cache missing {len(miss)} — incremental backfill ≤{lim}")
                rotate_cache_backfill(limit=min(lim, len(miss)))
                rc = rj(art_path("rotate_cache.json"), {})
        except Exception as e:
            log(f"incremental bootstrap skipped: {e}")
    stp = os.path.join(ROTDIR, f"rotate_state_{n}.json")
    st = rj(stp, {})
    titles = [t for t in sorted(rc.keys())[n::m] if rc[t].get("mid")]
    # LRU: least-recently-POSTed first — maximum coverage when over budget
    titles.sort(key=lambda t: ((st.get(t) or {}).get("ts") or 0, t))
    log(f"rotate shard {n}/{m}: {len(titles)} titles ｜ budget {budget}s")
    t0 = time.time()
    done = 0
    fid_by_key, shrunk = {}, {}
    dl_cache = os.path.join(BASE, "imgcache")
    os.makedirs(dl_cache, exist_ok=True)
    for t in titles:
        if time.time() - t0 > budget:
            log(f"budget reached — {done} re-POSTed")
            break
        rec = rc[t]
        k = img_key(rec.get("image", ""))
        now = time.time()
        hit = fid_by_key.get(k)
        fid = hit if hit and now - hit[1] < 72 else None
        try:
            if fid is None:
                p = os.path.join(dl_cache, f"{k or 'x'}.jpg")
                img = open(p, "rb").read() if os.path.isfile(p) else None
                if img is None and rec.get("image"):
                    r = requests.get(rec["image"], headers=UA, timeout=60)
                    img = r.content if r.status_code == 200 else None
                    if img:
                        open(p, "wb").write(img)
                if not img:
                    st[t] = {"ts": time.time(), "err": "img"}
                    continue
                payload = shrink_bytes(img)
                fid = eudic_upload("thumb", "rot.jpg", payload, "image/jpeg")
                if not fid:
                    st[t] = {"ts": time.time(), "err": "upload"}
                    continue
                fid_by_key[k] = (fid, time.time())
            cid_r = rec.get("cid") or cid_for(ep_num(rec.get("title") or t) or 1)
            okp = post_article(cid_r, rec["mid"], rec.get("title") or t,
                               rec.get("lrc", ""), rec.get("desc", ""),
                               fid[0] if isinstance(fid, tuple) else fid,
                               rec.get("intros", ""))
            st[t] = {"ts": time.time(), "code": 200 if okp else 0}
            done += 1 if okp else 0
            if not okp:
                log(f"  ✗ post {t[:44]}")
        except AuthError:
            ntfy("TFF GA: cookie expired",
                 "⚠️ EUDIC_COOKIE 已失效——輪換/同步暫停。\n"
                 "修復：登入 my.eudic.net → DevTools 複製 Cookie header → "
                 "提供給 agent 更新 repo secret。", 5)
            wj(stp, st)
            raise
        except Exception as e:
            st[t] = {"ts": time.time(), "err": str(e)[:40]}
            log(f"  err {t[:40]}: {str(e)[:60]}")
        wj(stp, st)
        time.sleep(GAP)
    wj(stp, st)
    log(f"rotate shard {n}/{m} done: {done} re-POSTed in {time.time()-t0:.0f}s")


# ─────────────────────────── job: keepalive ───────────────────────────
def job_keepalive():
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    tok = os.environ.get("GITHUB_TOKEN", "")
    lines = []
    if repo and tok:
        try:
            for wf in ("sync.yml", "rotate.yml"):
                r = requests.get(
                    f"https://api.github.com/repos/{repo}/actions/workflows/{wf}/runs"
                    f"?status=success&per_page=1",
                    headers={"Authorization": f"Bearer {tok}",
                             "Accept": "application/vnd.github+json"}, timeout=30)
                runs = r.json().get("workflow_runs", [])
                if runs:
                    lines.append(f"{wf}: last OK {runs[0]['created_at']}")
        except Exception as e:
            lines.append(f"gh api err: {str(e)[:50]}")
    body = (f"💓 keepalive {time.strftime('%F %T')} UTC\n"
            f"engine=GitHub Actions（半永久，非沙盒）｜repo=tfs-sync\n" + "\n".join(lines) +
            "\n（靜音訊號：沒收到＝引擎停了 → 回對話找 agent）")
    ntfy("TFF GA: keepalive", body, prio=1)
    log("keepalive sent")


# ─────────────────────────── job: audit ───────────────────────────
def job_audit(limit=0):
    rc = rj(art_path("rotate_cache.json"), {})
    if not rc:
        log("rotate_cache missing — run bootstrap first")
        return
    n = {"checked": 0, "lrc_drift": [], "visible_ts": [], "no_desc": [],
         "no_cover": [], "missing": []}
    # cover truth = listing img-zlg (edit-page thumb_fid is unreliable)
    nocover = set()
    try:
        for _a, _b, c in tfs_channels():
            r = req("GET", f"{EUDIC}/ting/GetPrivateMedias"
                           f"?sortby=0&page=0&channelId={c}", timeout=45)
            for x in re.split(r'<div class="dragBox">', r.text or "")[1:]:
                mid = re.search(r'media_id"[^>]*data="([^"]+)"', x)
                z = re.search(r'class="img-zlg"[^>]*src="([^"]*)"', x)
                if mid and z and "media_default.png" in z.group(1):
                    nocover.add(mid.group(1))
    except Exception as ex:
        log(f"cover scan skipped: {str(ex)[:80]}")
    t0 = time.time()
    budget = 2700
    for i, (title, rec) in enumerate(sorted(rc.items())):
        if limit and i >= limit:
            break
        if time.time() - t0 > budget:
            log("audit budget reached")
            break
        mid = rec.get("mid")
        if not mid:
            continue
        cid = rec.get("cid") or cid_for(ep_num(title) or 1)
        stt, txt = get_edit_page(cid, mid)
        if stt != 200 or not txt or len(txt) < 3000:
            n["missing"].append(title)
            continue
        info = parse_edit_page(txt)
        n["checked"] += 1
        want = len(lrc_sentences(rec.get("lrc", "")))
        got = len(lrc_sentences(info["lrc"]))
        if want >= 5 and got == 0:
            # WAF flake → one patient re-read before flagging drift
            time.sleep(15)
            stt, txt = get_edit_page(cid, mid)
            info = parse_edit_page(txt) if stt == 200 and txt else {}
            got = len(lrc_sentences(info["lrc"]))
        if want >= 5 and abs(got - want) / max(want, 1) > 0.05:
            n["lrc_drift"].append({"title": title, "want": want, "got": got})
        bad = [b for b in lrc_sentences(info["lrc"])[:200] if FINE_TS.match(b)]
        if bad:
            n["visible_ts"].append({"title": title, "lines": bad[:3]})
        if MARK not in info["desc"]:
            n["no_desc"].append(title)
        if mid in nocover:
            n["no_cover"].append(title)
        time.sleep(0.3)
    bad = sum(len(v) for v in (n["lrc_drift"], n["visible_ts"],
                               n["no_desc"], n["no_cover"], n["missing"]))
    body = (f"🔬 每月漂移稽核 {time.strftime('%F %T')}\n"
            f"檢查 {n['checked']}/{len(rc)}｜LRC 漂移 {len(n['lrc_drift'])}"
            f"｜可見時間戳 {len(n['visible_ts'])}｜缺導讀 {len(n['no_desc'])}"
            f"｜缺封面 {len(n['no_cover'])}｜缺 {len(n['missing'])}\n"
            + ("✅ 全綠——頻道忠實度驗證通過。" if not bad else
               "⚠️ 有偏差——回對話請 agent 處理：" +
               json.dumps({k: v[:3] for k, v in n.items()
                           if isinstance(v, list) and v}, ensure_ascii=False)[:400]))
    ntfy("TFF GA: audit", body, prio=4 if bad else 3)
    wj(art_path("audit.json"), n)
    log(f"audit done: checked={n['checked']} bad={bad}")


# ─────────────────────────── CLI ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("auth")
    sp = sub.add_parser("sync")
    sp.add_argument("--force", default="")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--newest", action="store_true")
    sp = sub.add_parser("bootstrap")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--reverse", action="store_true")
    sp = sub.add_parser("rotate")
    sp.add_argument("--shard", default="0/1")
    sp.add_argument("--budget", type=int, default=0)
    sp = sub.add_parser("keepalive")
    sp = sub.add_parser("audit")
    sp.add_argument("--limit", type=int, default=0)
    sp = sub.add_parser("subsync")
    sp = sub.add_parser("login")
    sp = sub.add_parser("channels")
    sp.add_argument("--ensure", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    t0 = time.time()
    DISPATCH = {
        "auth": lambda: job_auth(),
        "sync": lambda: job_sync(force=a.force, dry=a.dry_run, newest=a.newest),
        "bootstrap": lambda: job_bootstrap(a.limit, a.dry_run, a.reverse),
        "rotate": lambda: job_rotate(a.shard, a.budget or None),
        "keepalive": lambda: job_keepalive(),
        "audit": lambda: job_audit(a.limit),
        "subsync": lambda: job_subsync(),
        "login": lambda: job_login(),
        "channels": lambda: (print(json.dumps(ensure_channels(a.dry_run),
                                              ensure_ascii=False, indent=1))
                             if a.ensure or a.dry_run else
                             print(json.dumps(
                                 [(a_, b_, c_) for a_, b_, c_ in tfs_channels()],
                                 ensure_ascii=False, indent=1))),
    }
    try:
        DISPATCH[a.cmd]()
    except AuthError as e:
        if auto_relogin():
            try:
                DISPATCH[a.cmd]()
            except AuthError as e2:
                ntfy("TFF GA: cookie expired",
                     f"⚠️ {e2}\n自動重新登入也失敗——請檢查帳密或提供新 cookie。", 5)
                sys.exit(2)
        else:
            ntfy("TFF GA: cookie expired",
                 f"⚠️ {e}\n請提供新 cookie 或設定 EUDIC_USER/EUDIC_PASS 以啟用自動重登。", 5)
            sys.exit(2)
    log(f"== {a.cmd} finished in {time.time()-t0:.0f}s ==")


if __name__ == "__main__":
    main()
