#!/usr/bin/env python3
"""Issac dashboard sync: Instantly + HeyReach + GHL APIs + manual.json -> data.json.

The front end (index.html) only ever reads data.json. Keys never leave this script.
The public Pages repo only ever gets data.enc: data.json sealed with AES-GCM under a key
derived from DASH_PASSWORD (PBKDF2). data.json itself stays local / gitignored, which is
what lets it carry Issac's calendar (event titles) as well as the aggregates.

    python3 scripts/issac/dashboard/sync.py            # write data.json next to this file
    python3 scripts/issac/dashboard/sync.py --publish  # also copy index.html + data.json to the Pages clone

Env (from AIOS .env or GitHub Actions secrets), each optional:
    INSTANTLY_API_KEY
    HEYREACH_API_KEY
    GHL_API_KEY + GHL_LOCATION_ID        (private integration token, v2 API)
A source with no key, or whose call fails, falls back to its last good block in
data.json, then to the baseline in manual.json. One broken source never blanks the page.
"""
import base64
import hashlib
import json
import os
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OUT = HERE / "data.json"
ENC = HERE / "data.enc"
PAGES = ROOT / "outputs" / "evolv-decks" / "issac-dashboard"
DAYS = 30
ISSAC_USER_ID = "hP0Vd26Ni203ZsJfqcOa"  # GHL user; his Google Calendar syncs in as blocked slots
SALT = bytes.fromhex("6e783159918ef66a7293861904d6dd70")  # fixed, so a key remembered in the browser survives every sync
KDF_ITER = 250_000

CAMPAIGN_PREFIX = ""  # Instantly workspace is Issac-only; campaigns are named BOTH/BUSINESS/PROPERTY A-B
INBOX_DOMAINS = ["issacnewtontx.com", "issacnewtoncre.com", "newtoncre.com"]
BASE_CAMPAIGNS = ["ISSAC · C1 · BOTH", "ISSAC · C1 · BUSINESS", "ISSAC · C1 · PROPERTY"]
# Seller journey as written with Issac (ISSAC-CUSTOMER-JOURNEY-BASE.md). Live stage names come from GHL.
BASE_SELLER = ["Identified", "First Contact Made", "Conversation Started", "Discovery Call Booked",
               "Discovery Call Held", "Valuation in Progress", "Listing Presentation", "Listing Agreement Sent",
               "Signed — Listing Live", "Offers / Negotiation", "Under Contract", "Closed", "Past Client / Referral"]
BASE_DEAL = ["Inquiry Received", "Qualified", "Criteria Defined", "Properties Presented", "Tour / Site Visit",
             "LOI Submitted", "Under Contract", "Closed", "Repeat Buyer / Investor"]


def load_env():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "evolv-dashboard/1", **headers})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def soft(fn, default=None):
    """Optional sub-call: a failure here leaves that panel empty instead of failing the whole source."""
    try:
        return fn()
    except (urllib.error.URLError, KeyError, ValueError, TypeError, AttributeError, IndexError) as e:
        print(f"  soft-fail {getattr(fn, '__name__', 'call')}: {str(e)[:120]}")
        return default


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def day_range():
    end = date.today()
    return [(end - timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]


# ---------- Instantly (API v2) ----------
def instantly(key):
    h = {"Authorization": f"Bearer {key}"}
    base = "https://api.instantly.ai/api/v2"
    camps = http("GET", f"{base}/campaigns?limit=100", h).get("items", [])
    camps = [c for c in camps if c.get("name", "").upper().startswith(CAMPAIGN_PREFIX)]
    status_map = {0: "draft", 1: "live", 2: "paused", 3: "completed", 4: "running_subsequences"}

    rows, daily = [], {d: {"sent": 0, "replies": 0} for d in day_range()}
    start, end = day_range()[0], day_range()[-1]
    for c in camps:
        a = http("GET", f"{base}/campaigns/analytics?id={c['id']}", h)
        a = (a[0] if isinstance(a, list) and a else a) or {}
        rows.append({
            "name": c["name"].split("·")[-1].strip().title(),
            "status": status_map.get(c.get("status"), "draft"),
            "in_campaign": a.get("leads_count", 0),
            "contacted": a.get("contacted_count", 0),
            "sent": a.get("emails_sent_count", 0),
            "replies": a.get("reply_count", 0),
            "positive": a.get("total_opportunities", 0),
            "bounced": a.get("bounced_count", 0),
            "unsubscribed": a.get("unsubscribed_count", 0),
        })
        q = urllib.parse.urlencode({"campaign_id": c["id"], "start_date": start, "end_date": end})
        for d in http("GET", f"{base}/campaigns/analytics/daily?{q}", h) or []:
            if d.get("date") in daily:
                daily[d["date"]]["sent"] += d.get("sent", 0)
                daily[d["date"]]["replies"] += d.get("unique_replies", d.get("replies", 0))

    def steps():
        agg = {}
        for c in camps:
            q = urllib.parse.urlencode({"campaign_id": c["id"], "start_date": start, "end_date": end})
            for r in http("GET", f"{base}/campaigns/analytics/steps?{q}", h) or []:
                if r.get("step") is None:
                    continue
                k = int(r["step"])
                a = agg.setdefault(k, {"sent": 0, "replies": 0})
                a["sent"] += r.get("sent", 0) or 0
                a["replies"] += r.get("unique_replies", r.get("replies", 0)) or 0
        off = 1 if 0 in agg else 0
        return [{"step": k + off, **v} for k, v in sorted(agg.items())]

    inboxes = []
    for acc in http("GET", f"{base}/accounts?limit=100", h).get("items", []):
        dom = acc.get("email", "").split("@")[-1]
        if dom in INBOX_DOMAINS:
            inboxes.append({"domain": dom, "warmup": acc.get("stat_warmup_score"),
                            "ok": acc.get("status") == 1})

    live = any(r["status"] == "live" for r in rows)
    return {"status": "live" if live else "draft", "campaigns": rows, "inboxes": inboxes, "steps": soft(steps, []),
            "daily": [{"date": d, **v} for d, v in daily.items()]}


# ---------- HeyReach (public API) ----------
def heyreach(key):
    h = {"X-API-KEY": key}
    base = "https://api.heyreach.io/api/public"
    camps = http("POST", f"{base}/campaign/GetAll", h, {"offset": 0, "limit": 100}).get("items", [])
    ids = [c["id"] for c in camps]
    days = day_range()
    body = {"accountIds": [], "campaignIds": ids,
            "startDate": f"{days[0]}T00:00:00.000Z", "endDate": f"{days[-1]}T23:59:59.999Z"}
    s = http("POST", f"{base}/stats/GetOverallStats", h, body) or {}
    o = s.get("overallStats", {}) or {}
    by_day = s.get("byDayStats", {}) or {}
    daily = []
    for d in days:
        v = next((by_day[k] for k in by_day if k.startswith(d)), {}) or {}
        daily.append({"date": d, "requests": v.get("connectionsSent", 0), "replies": v.get("totalMessageReplies", 0)})
    live = any(str(c.get("status", "")).upper() in ("IN_PROGRESS", "ACTIVE", "STARTING") for c in camps)

    def progress():
        out = []
        for c in camps:
            p = c.get("progressStats") or {}
            out.append({"name": str(c.get("name", "")).split("·")[-1].strip() or "Campaign",
                        "status": "live" if str(c.get("status", "")).upper() in ("IN_PROGRESS", "ACTIVE", "STARTING") else "draft",
                        "total": p.get("totalUsers", 0), "finished": p.get("totalUsersFinished", 0),
                        "in_progress": p.get("totalUsersInProgress", 0), "pending": p.get("totalUsersPending", 0),
                        "failed": p.get("totalUsersFailed", 0)})
        return out

    def accounts():
        a = http("POST", f"{base}/li_account/GetAll", h, {"offset": 0, "limit": 100}).get("items", [])
        return {"total": len(a), "active": sum(1 for x in a if x.get("isActive", x.get("authIsValid")))} if a else None

    return {"campaigns": progress(), "accounts": soft(accounts),
            "status": "live" if live else ("draft" if camps else "waiting"),
            "requests_sent": o.get("connectionsSent", 0), "accepted": o.get("connectionsAccepted", 0),
            "messages_sent": o.get("messagesSent", 0), "replies": o.get("totalMessageReplies", 0),
            "profile_views": o.get("profileViews", 0), "daily": daily}


# ---------- GoHighLevel (API v2) ----------
def ghl(key, loc):
    h = {"Authorization": f"Bearer {key}", "Version": "2021-07-28"}
    base = "https://services.leadconnectorhq.com"
    pipes = http("GET", f"{base}/opportunities/pipelines?locationId={loc}", h).get("pipelines", [])

    def pick(word):
        # Exact "<Word> Pipeline" first: the account also has a "Seller Outreach Pipeline".
        exact = [p for p in pipes if p["name"].strip().lower() == f"{word} pipeline"]
        cands = exact or [p for p in pipes if word in p["name"].lower() and "outreach" not in p["name"].lower()]
        return next((p for p in cands if "v2" in p["name"].lower()), cands[0] if cands else None)

    def opps(pid):
        out, page = [], 1
        while True:
            q = urllib.parse.urlencode({"location_id": loc, "pipeline_id": pid, "limit": 100, "page": page})
            r = http("GET", f"{base}/opportunities/search?{q}", h)
            batch = r.get("opportunities", [])
            out += batch
            if len(batch) < 100 or page >= 50:
                return out
            page += 1

    result, activity = {"status": "live"}, []
    for key_, word in (("seller", "seller"), ("deal", "deal")):
        p = pick(word)
        if not p:
            continue
        stages = sorted(p["stages"], key=lambda s: s.get("position", 0))
        rows = {s["id"]: {"stage": s["name"], "count": 0, "value": 0} for s in stages}
        for o in opps(p["id"]):
            if o.get("status") not in ("open", "won"):
                continue
            r = rows.get(o.get("pipelineStageId"))
            if r:
                r["count"] += 1
                r["value"] += float(o.get("monetaryValue") or 0)
            ts = o.get("lastStageChangeAt") or o.get("updatedAt")
            if r and ts:
                who = "A seller" if key_ == "seller" else "A buyer"
                activity.append({"at": ts, "kind": key_, "text": f"{who} moved to {r['stage']}"})
        result[key_] = list(rows.values())

    # The other two pipelines in the account: stage counts only, no activity feed.
    for key_, name in (("marketing", "marketing pipeline"), ("outreach", "seller outreach pipeline")):
        p = next((x for x in pipes if x["name"].strip().lower() == name), None)
        if not p:
            continue
        stages = sorted(p["stages"], key=lambda s: s.get("position", 0))
        rows = []
        for st in stages:  # one call per stage, read meta.total: the outreach pipeline holds 5k+ cards
            q = urllib.parse.urlencode({"location_id": loc, "pipeline_id": p["id"], "pipeline_stage_id": st["id"],
                                        "status": "open", "limit": 1})
            rows.append({"stage": st["name"], "count": (http("GET", f"{base}/opportunities/search?{q}", h).get("meta") or {}).get("total", 0), "value": 0})
        result[key_] = rows
    activity.sort(key=lambda a: a["at"], reverse=True)
    result["activity"] = activity[:8]

    now = datetime.now(timezone.utc)
    today, since = now.date().isoformat(), now - timedelta(days=DAYS)

    def ts(v):
        if v in (None, ""):
            return None
        if isinstance(v, (int, float)) or str(v).isdigit():
            return datetime.fromtimestamp(int(v) / 1000, timezone.utc)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))

    def tasks():
        rows = http("POST", f"{base}/locations/{loc}/tasks/search", h, {"limit": 1000}).get("tasks", [])
        open_ = [t for t in rows if not t.get("completed")]
        due = [ts(t.get("dueDate")) for t in open_]
        done = [t for t in rows if t.get("completed") and (ts(t.get("updatedAt") or t.get("dueDate")) or now) >= since]
        return {"open": len(open_), "due_today": sum(1 for d in due if d and d.date().isoformat() == today),
                "overdue": sum(1 for d in due if d and d < now and d.date().isoformat() != today), "completed_30d": len(done)}

    def workflows():
        w = http("GET", f"{base}/workflows/?locationId={loc}", h).get("workflows", [])
        return [{"name": x.get("name", ""), "status": x.get("status", "draft")} for x in w]

    def appointments():
        cals = http("GET", f"{base}/calendars/?locationId={loc}", h).get("calendars", [])
        start, end = int(since.timestamp() * 1000), int((now + timedelta(days=7)).timestamp() * 1000)
        ev = []
        for c in cals:
            q = urllib.parse.urlencode({"locationId": loc, "calendarId": c["id"], "startTime": start, "endTime": end})
            ev += http("GET", f"{base}/calendars/events?{q}", h).get("events", [])
        past = [e for e in ev if ts(e.get("startTime")) and ts(e["startTime"]) <= now]
        st = lambda e: str(e.get("appointmentStatus", "")).lower()
        daily = {d: 0 for d in day_range()}
        for e in ev:
            b = ts(e.get("dateAdded") or e.get("startTime"))
            if b and b.date().isoformat() in daily and st(e) != "cancelled":
                daily[b.date().isoformat()] += 1
        upcoming = sorted(ts(e["startTime"]).isoformat() for e in ev
                          if ts(e.get("startTime")) and ts(e["startTime"]) > now and st(e) not in ("cancelled", "invalid"))
        return {"booked_30d": sum(daily.values()), "showed": sum(1 for e in past if st(e) == "showed"),
                "no_show": sum(1 for e in past if st(e) == "noshow"), "cancelled": sum(1 for e in ev if st(e) == "cancelled"),
                "upcoming": upcoming[:8], "daily": [{"date": d, "booked": v} for d, v in daily.items()]}

    def calendar():
        q = urllib.parse.urlencode({"locationId": loc, "userId": ISSAC_USER_ID,
                                    "startTime": int((now - timedelta(days=14)).timestamp() * 1000),
                                    "endTime": int((now + timedelta(days=42)).timestamp() * 1000)})
        busy = http("GET", f"{base}/calendars/blocked-slots?{q}", h).get("events", [])
        calls = http("GET", f"{base}/calendars/events?{q}", h).get("events", [])
        rows = [{"title": e.get("title") or "Busy", "start": e["startTime"], "end": e["endTime"], "kind": kind}
                for kind, evs in (("event", busy), ("call", calls)) for e in evs
                if not e.get("deleted") and e.get("startTime") and e.get("endTime")
                and str(e.get("appointmentStatus", "")).lower() not in ("cancelled", "invalid")]
        return sorted(rows, key=lambda r: r["start"])

    def conversations():
        cs, by = http("GET", f"{base}/conversations/search?locationId={loc}&limit=100", h).get("conversations", []), {}
        names = {"TYPE_EMAIL": "Email", "TYPE_SMS": "SMS", "TYPE_CALL": "Call", "TYPE_PHONE": "Call"}
        for c in cs:
            k = names.get(c.get("lastMessageType"), "Other")
            by[k] = by.get(k, 0) + 1
        return {"total": len(cs), "unread": sum(1 for c in cs if c.get("unreadCount")), "by_channel": by,
                "awaiting_us": sum(1 for c in cs if c.get("lastMessageDirection") == "inbound"),
                "awaiting_them": sum(1 for c in cs if c.get("lastMessageDirection") == "outbound")}

    def contacts():
        # Full scan via searchAfter: ~21 calls of 500 for 10k contacts. Aggregates only.
        body, n, new30, by, daily = {"locationId": loc, "pageLimit": 500}, 0, 0, {}, {d: 0 for d in day_range()}
        names = {"csv_import": "CSV import", "form": "Form", "calendar": "Calendar", "manual": "Manual",
                 "api": "API / integration", "survey": "Survey", "chat_widget": "Chat widget"}
        for _ in range(60):
            r = http("POST", f"{base}/contacts/search", h, body)
            cs = r.get("contacts", [])
            for c in cs:
                n += 1
                med = (c.get("attributionSource") or {}).get("medium") or ""
                k = names.get(med, med.replace("_", " ").capitalize() or "Unknown")
                by[k] = by.get(k, 0) + 1
                a = ts(c.get("dateAdded"))
                if a and a >= since:
                    new30 += 1
                    if a.date().isoformat() in daily:
                        daily[a.date().isoformat()] += 1
            if len(cs) < 500 or not cs[-1].get("searchAfter"):
                break
            body["searchAfter"] = cs[-1]["searchAfter"]
        return {"total": r.get("total", n), "new_30d": new30,
                "by_source": dict(sorted(by.items(), key=lambda x: -x[1])),
                "daily": [{"date": d, "added": v} for d, v in daily.items()]}

    def forms():
        fs = http("GET", f"{base}/forms/?locationId={loc}&limit=50", h).get("forms", [])
        rows = {f["id"]: {"name": f.get("name", ""), "total": 0, "last_30d": 0} for f in fs}
        daily, page = {d: 0 for d in day_range()}, 1
        while True:
            r = http("GET", f"{base}/forms/submissions?locationId={loc}&limit=100&page={page}", h)
            for x in r.get("submissions", []):
                row = rows.setdefault(x.get("formId"), {"name": "Other form", "total": 0, "last_30d": 0})
                row["total"] += 1
                a = ts(x.get("createdAt"))
                if a and a >= since:
                    row["last_30d"] += 1
                    if a.date().isoformat() in daily:
                        daily[a.date().isoformat()] += 1
            if not (r.get("meta") or {}).get("nextPage") or page >= 50:
                break
            page += 1
        return {"forms": list(rows.values()), "daily": [{"date": d, "submissions": v} for d, v in daily.items()]}

    result.update(tasks=soft(tasks), workflows=soft(workflows, []), appointments=soft(appointments), calendar=soft(calendar, []),
                  conversations=soft(conversations), contacts=soft(contacts), forms=soft(forms))
    return result


def baseline(manual):
    b = manual["baseline"]
    return {
        "instantly": {**b["instantly"], "campaigns": [
            {"name": n.split("·")[-1].strip().title(), "status": "draft", "in_campaign": 0, "contacted": 0,
             "sent": 0, "replies": 0, "positive": 0, "bounced": 0, "unsubscribed": 0} for n in BASE_CAMPAIGNS],
            "inboxes": [{"domain": d, "warmup": None, "ok": True} for d in INBOX_DOMAINS for _ in range(3)],
            "daily": [{"date": d, "sent": 0, "replies": 0} for d in day_range()]},
        "heyreach": {**b["heyreach"], "requests_sent": 0, "accepted": 0, "messages_sent": 0, "replies": 0,
                     "profile_views": 0, "daily": [{"date": d, "requests": 0, "replies": 0} for d in day_range()]},
        "ghl": {**b["ghl"], "seller": [{"stage": s, "count": 0, "value": 0} for s in BASE_SELLER],
                "deal": [{"stage": s, "count": 0, "value": 0} for s in BASE_DEAL], "activity": []},
    }


def _key(pw):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), SALT, KDF_ITER)


def seal(text, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = os.urandom(12)
    ct = AESGCM(_key(pw)).encrypt(iv, text.encode(), None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": KDF_ITER, "salt": SALT.hex(), "iv": iv.hex(),
            "ct": base64.b64encode(ct).decode()}


def unseal(d, pw):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(_key(pw)).decrypt(bytes.fromhex(d["iv"]), base64.b64decode(d["ct"]), None).decode()


def previous(pw):
    if OUT.exists():
        return json.loads(OUT.read_text())
    if ENC.exists() and pw:
        try:
            return json.loads(unseal(json.loads(ENC.read_text()), pw))
        except Exception:
            pass
    return {}


def main():
    load_env()
    manual = json.loads((HERE / "manual.json").read_text())
    pw = os.getenv("DASH_PASSWORD")
    prev = previous(pw)
    base = baseline(manual)

    jobs = {
        "instantly": (lambda: instantly(os.environ["INSTANTLY_API_KEY"])) if os.getenv("INSTANTLY_API_KEY") else None,
        "heyreach": (lambda: heyreach(os.environ["HEYREACH_API_KEY"])) if os.getenv("HEYREACH_API_KEY") else None,
        "ghl": (lambda: ghl(os.environ["GHL_API_KEY"], os.environ["GHL_LOCATION_ID"]))
        if os.getenv("GHL_API_KEY") and os.getenv("GHL_LOCATION_ID") else None,
    }
    data, log = {}, []
    for name, job in jobs.items():
        if job is None:
            data[name] = {**base[name], "connected": False, "synced_at": None}
            log.append(f"{name}: no key, baseline")
            continue
        try:
            data[name] = {**job(), "connected": True, "synced_at": now_iso()}
            log.append(f"{name}: ok")
        except (urllib.error.URLError, KeyError, ValueError, TypeError) as e:
            last = prev.get(name) if (prev.get(name) or {}).get("connected") else None
            data[name] = {**(last or base[name]), "connected": bool(last), "error": str(e)[:200]}
            log.append(f"{name}: FAILED ({e}), kept {'last good' if last else 'baseline'}")

    out = {"generated_at": now_iso(), "client": "Issac Newton", "brokerage": "eXp Commercial",
           "machine_live_since": manual.get("machine_live_since"),
           **data, "list": manual["list"], "content": manual["content"], "website": manual["website"]}
    blob = json.dumps(out, indent=1, ensure_ascii=False)
    OUT.write_text(blob)
    if pw:
        ENC.write_text(json.dumps(seal(blob, pw)))
        log.append(f"sealed -> {ENC.name}")
    else:
        log.append("DASH_PASSWORD missing: data.enc NOT updated")
    print("\n".join(log), f"\n-> {OUT}")

    if "--publish" in sys.argv:
        PAGES.mkdir(parents=True, exist_ok=True)
        shutil.copy(HERE / "index.html", PAGES / "index.html")
        shutil.copy(ENC, PAGES / "data.enc")  # never the plaintext
        print(f"-> {PAGES}")


if __name__ == "__main__":
    main()
