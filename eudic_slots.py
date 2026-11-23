"""
eudic_slots.py v1 — 歐路帳號級上傳槽位統御層（跨 repo 共享）

背景（T-063 實證 2026-11-23）：Eusoft 平台 per-account 併發處理進程 ≤5；
滿時 create 被拒「你已有 5 个资源进程在上传…」。三個上傳源（tf-engine/htf-engine/
routines podcast）同帳號盲跑→窗口重疊→create=FAIL。

機制：真相源=本 repo 的 state/upload_slots.json（GitHub Contents API 樂觀鎖）。
上傳前 claim 一槽 → create → release。claim 帶 TTL（崩潰自動過期）。
三防線：①claim 層（本模組，把撞擊機率降到趨零）②平台拒絕（兜底）
       ③各源自帶槽位感知退避（60-90s×k，已在 T-029 後部署）
降級原則：統御層故障（API 403/網路）→ 直接上傳，絕不阻塞生產線。
"""
import base64, json, time

REPO = "b921603/routines"
PATH = "state/upload_slots.json"
N_SLOTS = 5
CLAIM_TTL = 3600          # claim 1 小時自動過期（崩潰不洩漏槽位）
API = "https://api.github.com"


def _req(method, url, token, payload=None, timeout=40):
    import requests
    h = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    if method == "GET":
        r = requests.get(url, headers=h, timeout=timeout)
    else:
        r = requests.put(url, headers=h, json=payload, timeout=timeout)
    return r


def _get_state(token):
    """讀真相源；404=檔案未建（視為空）。回 (state, sha|None) 或 (None, None)=API 故障。"""
    try:
        r = _req("GET", f"{API}/repos/{REPO}/contents/{PATH}", token)
        if r.status_code == 404:
            return {"v": 1, "slots": [], "updated": int(time.time())}, None
        if r.status_code != 200:
            return None, None
        j = r.json()
        body = json.loads(base64.b64decode(j.get("content") or "").decode() or "{}")
        body.setdefault("v", 1)
        body.setdefault("slots", [])
        return body, j.get("sha")
    except Exception:
        return None, None


def _put_state(state, sha, token):
    """寫真相源（樂觀鎖：sha 不符=409 → 呼叫者重讀重算）。"""
    import base64 as b64
    state["updated"] = int(time.time())
    payload = {"message": f"slots: {len(state['slots'])} active",
               "content": b64.b64encode(json.dumps(state, ensure_ascii=False).encode()).decode()}
    if sha:
        payload["sha"] = sha
    r = _req("PUT", f"{API}/repos/{REPO}/contents/{PATH}", token, payload)
    return r.status_code in (200, 201), r.status_code


def _sweep(state):
    now = time.time()
    state["slots"] = [s for s in state.get("slots", []) if s.get("exp", 0) > now]
    return state


def claim(owner, media, token, wait_min=10, ttl=300, log=None):
    """佔一槽。成功→slot_id（如 't3'）；額滿等待 wait_min 後仍滿→None（呼叫者降級直上）。
    ttl=create 後平台實際處理時長估計（純轉碼 240s／AI 字幕 600s）——到點自動釋放，
    故成功上傳後**不需主動 release**（release 僅用於 create 失敗的立即歸還）。"""
    dl = 60 if log is None else 60
    deadline = time.time() + wait_min * 60
    while True:
        state, sha = _get_state(token)
        if state is None:
            return None                      # API 故障→降級（絕不阻塞）
        state = _sweep(state)
        used = {s.get("slot") for s in state["slots"]}
        free = [n for n in range(1, N_SLOTS + 1) if n not in used]
        if free:
            ttl_eff = int(ttl if ttl and ttl > 0 else CLAIM_TTL)
            slot = {"slot": free[0], "owner": owner, "media": str(media)[:40],
                    "at": int(time.time()), "exp": int(time.time() + ttl_eff)}
            state["slots"].append(slot)
            ok, code = _put_state(state, sha, token)
            if ok:
                sid = f"{owner[0]}{slot['slot']}"
                if log: log(f"slot claim {sid} ({len(state['slots'])}/{N_SLOTS})")
                return sid
            if code == 409:
                time.sleep(2)                # 樂觀鎖衝突→重讀重算
            continue
        if time.time() >= deadline:
            if log: log(f"slots full {N_SLOTS}/{N_SLOTS} after {wait_min}m — degrade to direct")
            return None
        if log: log(f"slots full, waiting 60s ({len(state['slots'])}/{N_SLOTS})")
        time.sleep(dl)


def release(owner, sid, token, log=None):
    """立即釋放槽位（create 失敗時用；成功上傳者讓 TTL 自動過期）。"""
    state, sha = _get_state(token)
    if state is None:
        return False
    before = len(state["slots"])
    state["slots"] = [s for s in state["slots"]
                      if not (s.get("owner") == owner and f"{owner[0]}{s.get('slot')}" == sid)]
    if len(state["slots"]) == before:
        return True
    ok, _ = _put_state(state, sha, token)
    if ok and log: log(f"slot release {sid} ({len(state['slots'])}/{N_SLOTS})")
    return ok
