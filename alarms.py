"""Parked-car sessions, Web Push reminders, and calendar alarms.

Sessions live in sessions.db, separate from the city data file, so a data reload
does not erase them. On Render's free plan the disk is ephemeral and the process
sleeps, so push is best-effort. The .ics alarms do not depend on this process.
"""
import json, os, re, secrets, sqlite3, threading, time, uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SESSION_DB = os.environ.get("SESSION_DB", "sessions.db")
TZ = ZoneInfo("America/Los_Angeles")
LOCK = threading.Lock()
REMINDERS = ((15, "15"), (5, "5"), (0, "now"))
# Device tokens are created on the phone and stored only in sessions.db. Never commit one.
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


def connect():
    conn = sqlite3.connect(SESSION_DB)
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS subs (
        id TEXT PRIMARY KEY, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT, created TEXT);
      CREATE TABLE IF NOT EXISTS parks (
        id TEXT PRIMARY KEY, lat REAL, lng REAL, address TEXT, move_by TEXT,
        reason TEXT, headline TEXT, sub_id TEXT, created TEXT, cancelled INTEGER DEFAULT 0,
        device_token TEXT);
      CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, park_id TEXT, fire_at TEXT, kind TEXT, sent INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS devices (
        token TEXT PRIMARY KEY, sub_id TEXT, created TEXT);
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(parks)")}
    if "device_token" not in cols:
        conn.execute("ALTER TABLE parks ADD COLUMN device_token TEXT")
    return conn


def valid_token(token):
    return bool(TOKEN_RE.match(token or ""))


def reminder_times(move_by, now):
    """15 min, 5 min, and the deadline. Times already past are omitted."""
    out = []
    for minutes, kind in REMINDERS:
        fire = move_by - timedelta(minutes=minutes)
        if fire >= now - timedelta(seconds=5):
            out.append({"at": fire, "kind": kind, "minutes_before": minutes})
    return out


def upsert_sub(sub):
    endpoint = (sub or {}).get("endpoint") or ""
    keys = (sub or {}).get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise ValueError("subscription needs endpoint, p256dh, and auth")
    if len(endpoint) > 2000:
        raise ValueError("subscription endpoint is too long")
    sid = uuid.uuid4().hex
    with LOCK:
        conn = connect()
        row = conn.execute("SELECT id FROM subs WHERE endpoint=?", (endpoint,)).fetchone()
        if row:
            conn.execute("UPDATE subs SET p256dh=?, auth=? WHERE id=?", (keys["p256dh"], keys["auth"], row[0]))
            sid = row[0]
        else:
            conn.execute("INSERT INTO subs VALUES(?,?,?,?,?)",
                         (sid, endpoint, keys["p256dh"], keys["auth"], datetime.now(TZ).isoformat()))
        conn.commit()
    return sid


def save_park(lat, lng, address, move_by, reason, headline, sub_id, now, device_token=None):
    """move_by is an aware datetime or None. Reminders are skipped when there is no deadline."""
    pid = uuid.uuid4().hex
    fires = reminder_times(move_by, now) if move_by else []
    with LOCK:
        conn = connect()
        conn.execute("""INSERT INTO parks
            (id, lat, lng, address, move_by, reason, headline, sub_id, created, cancelled, device_token)
            VALUES (?,?,?,?,?,?,?,?,?,0,?)""", (
            pid, lat, lng, address or "", move_by.isoformat() if move_by else None,
            reason or "", headline or "", sub_id, now.isoformat(), device_token))
        for item in fires:
            conn.execute("INSERT INTO reminders(park_id, fire_at, kind, sent) VALUES(?,?,?,0)",
                         (pid, item["at"].isoformat(), item["kind"]))
        conn.commit()
    return {"id": pid, "alarm_at": move_by.isoformat() if move_by else None,
            "reminders": [{"at": i["at"].isoformat(), "kind": i["kind"], "minutes_before": i["minutes_before"]} for i in fires]}


def ensure_device(token=None, sub=None):
    """Return this phone's token, creating it if needed. A token the phone already
    holds is stored again after a server restart so the Shortcut keeps working."""
    sub_id = upsert_sub(sub) if sub else None
    with LOCK:
        conn = connect()
        if valid_token(token):
            row = conn.execute("SELECT token, sub_id FROM devices WHERE token=?", (token,)).fetchone()
            if row:
                if sub_id and sub_id != row[1]:
                    conn.execute("UPDATE devices SET sub_id=? WHERE token=?", (sub_id, token))
                    conn.commit()
                return token
            conn.execute("INSERT INTO devices VALUES(?,?,?)", (token, sub_id, datetime.now(TZ).isoformat()))
            conn.commit()
            return token
        fresh = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO devices VALUES(?,?,?)", (fresh, sub_id, datetime.now(TZ).isoformat()))
        conn.commit()
        return fresh


def rotate_device(old, sub=None):
    fresh = secrets.token_urlsafe(32)
    sub_id = upsert_sub(sub) if sub else None
    with LOCK:
        conn = connect()
        if valid_token(old):
            prev = conn.execute("SELECT sub_id FROM devices WHERE token=?", (old,)).fetchone()
            if prev and not sub_id:
                sub_id = prev[0]
            conn.execute("DELETE FROM devices WHERE token=?", (old,))
        conn.execute("INSERT INTO devices VALUES(?,?,?)", (fresh, sub_id, datetime.now(TZ).isoformat()))
        conn.commit()
    return fresh


def get_device(token):
    if not valid_token(token):
        return None
    conn = connect()
    row = conn.execute("""
        SELECT devices.token, devices.sub_id, subs.endpoint, subs.p256dh, subs.auth
        FROM devices LEFT JOIN subs ON subs.id = devices.sub_id WHERE devices.token=?""", (token,)).fetchone()
    if not row:
        return None
    return {"token": row[0], "sub_id": row[1], "endpoint": row[2], "p256dh": row[3], "auth": row[4]}


def latest_device_park(token):
    if not valid_token(token):
        return None
    conn = connect()
    row = conn.execute("""
        SELECT id, lat, lng, address, move_by, reason, headline, cancelled, created
        FROM parks WHERE device_token=? AND cancelled=0 ORDER BY created DESC LIMIT 1""", (token,)).fetchone()
    if not row:
        return None
    keys = ("id", "lat", "lng", "address", "move_by", "reason", "headline", "cancelled", "created")
    return dict(zip(keys, row))


def push_to_sub(sub, payload, sender=None):
    """sub is {endpoint, p256dh, auth}. sender overrides Web Push in tests."""
    if not sub or not sub.get("endpoint"):
        return False
    sender = sender or send_web_push
    if sender is send_web_push and not push_configured():
        return False
    return bool(sender(sub, payload))


def attach_sub(park_id, sub):
    sid = upsert_sub(sub)
    with LOCK:
        conn = connect()
        cur = conn.execute("UPDATE parks SET sub_id=? WHERE id=? AND cancelled=0", (sid, park_id))
        conn.commit()
        return cur.rowcount > 0


def cancel_park(park_id):
    with LOCK:
        conn = connect()
        cur = conn.execute("UPDATE parks SET cancelled=1 WHERE id=?", (park_id,))
        conn.commit()
        return cur.rowcount > 0


def get_park(park_id):
    conn = connect()
    row = conn.execute(
        "SELECT id, lat, lng, address, move_by, reason, headline, cancelled, created FROM parks WHERE id=?",
        (park_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "lat", "lng", "address", "move_by", "reason", "headline", "cancelled", "created")
    return dict(zip(keys, row))


def _ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _ics_stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(uid, start, summary, description):
    """One event at the deadline with alarms 15 min before, 5 min before, and at the time."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//LA Parking//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@la-parking",
        f"DTSTAMP:{_ics_stamp(datetime.now(timezone.utc))}",
        f"DTSTART:{_ics_stamp(start)}",
        f"DTEND:{_ics_stamp(start + timedelta(minutes=15))}",
        "SUMMARY:" + _ics_escape(summary),
        "DESCRIPTION:" + _ics_escape(description),
    ]
    for trigger, text in (("-PT15M", "Move your car in 15 minutes"), ("-PT5M", "Move your car in 5 minutes"), ("PT0S", "Move your car now")):
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER:{trigger}", "DESCRIPTION:" + _ics_escape(text), "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def push_configured():
    return bool(os.environ.get("VAPID_PUBLIC_KEY") and os.environ.get("VAPID_PRIVATE_KEY"))


def _title(kind):
    return {"15": "Move your car in 15 minutes", "5": "Move your car in 5 minutes"}.get(kind, "Move your car now")


def send_web_push(sub, payload):
    from pywebpush import webpush, WebPushException
    pem = os.environ["VAPID_PRIVATE_KEY"].replace("\\n", "\n")
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=pem,
            vapid_claims={"sub": os.environ.get("VAPID_SUBJECT") or "mailto:la-parking@example.com"},
            ttl=3600,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return "gone"
        print("push failed:", code or e, flush=True)
        return False


def send_due(now, sender=None):
    """Send reminders whose time has come. A reminder more than 10 minutes late is dropped."""
    sender = sender or send_web_push
    if sender is send_web_push and not push_configured():
        return 0
    with LOCK:
        conn = connect()
        rows = conn.execute("""
            SELECT reminders.id, reminders.kind, reminders.fire_at, parks.id, parks.address, parks.reason,
                   parks.sub_id, subs.endpoint, subs.p256dh, subs.auth
            FROM reminders
            JOIN parks ON parks.id = reminders.park_id
            LEFT JOIN subs ON subs.id = parks.sub_id
            WHERE reminders.sent=0 AND parks.cancelled=0 AND reminders.fire_at<=?
        """, (now.isoformat(),)).fetchall()
    sent = 0
    for rid, kind, fire_at, pid, address, reason, sub_id, endpoint, p256dh, auth in rows:
        fire = datetime.fromisoformat(fire_at)
        if now - fire > timedelta(minutes=10):
            with LOCK:
                c = connect()
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
                c.commit()
            continue
        if not endpoint:
            continue   # a subscription may arrive before the 10-minute grace ends
        payload = {"title": _title(kind), "body": " ".join(x for x in (address, reason) if x) or "Time to move the car.",
                   "tag": f"park-{pid}-{kind}", "url": "/?park=" + pid}
        result = sender({"endpoint": endpoint, "p256dh": p256dh, "auth": auth}, payload)
        if result:
            with LOCK:
                c = connect()
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
                if result == "gone" and sub_id:
                    c.execute("DELETE FROM subs WHERE id=?", (sub_id,))
                c.commit()
            if result is True:
                sent += 1
                print(f"push {kind} for {pid[:8]}", flush=True)
    return sent


def scheduler_loop(interval=30):
    while True:
        try:
            send_due(datetime.now(TZ))
        except Exception as e:
            print("reminder loop:", e, flush=True)
        time.sleep(interval)
