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
# Defaults: 15 and 5 minutes before, an urgent alert at the deadline, then a
# repeat every 3 minutes until 15 minutes after the deadline or the driver responds.
DEFAULT_SETTINGS = {
    "lead_minutes": [15, 5],
    "repeat_minutes": 3,
    "repeat_for_minutes": 15,
    "night_hour": 20,
    "morning_hour": 7,
    "early_before_hour": 10,
    "snooze_minutes": 10,
}
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
        device_token TEXT, settings TEXT, snooze_until TEXT);
      CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, park_id TEXT, fire_at TEXT, kind TEXT, sent INTEGER DEFAULT 0);
      CREATE TABLE IF NOT EXISTS devices (
        token TEXT PRIMARY KEY, sub_id TEXT, created TEXT, settings TEXT);
      CREATE TABLE IF NOT EXISTS photos (
        id TEXT PRIMARY KEY, scan_id TEXT, park_id TEXT, path TEXT, created TEXT);
      CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY, device_token TEXT, lat REAL, lng REAL, kind TEXT,
        note TEXT, created TEXT, expires TEXT, confirms INTEGER DEFAULT 1);
      CREATE TABLE IF NOT EXISTS report_votes (
        report_id TEXT, device_token TEXT, PRIMARY KEY (report_id, device_token));
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(parks)")}
    if "device_token" not in cols:
        conn.execute("ALTER TABLE parks ADD COLUMN device_token TEXT")
    if "settings" not in cols:
        conn.execute("ALTER TABLE parks ADD COLUMN settings TEXT")
    if "snooze_until" not in cols:
        conn.execute("ALTER TABLE parks ADD COLUMN snooze_until TEXT")
    if "snapshot" not in cols:
        conn.execute("ALTER TABLE parks ADD COLUMN snapshot TEXT")
    dcols = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "settings" not in dcols:
        conn.execute("ALTER TABLE devices ADD COLUMN settings TEXT")
    return conn


def clean_settings(raw):
    """Clamp a settings object. Unknown or out-of-range values fall back to defaults."""
    s = {
        "lead_minutes": list(DEFAULT_SETTINGS["lead_minutes"]),
        "repeat_minutes": DEFAULT_SETTINGS["repeat_minutes"],
        "repeat_for_minutes": DEFAULT_SETTINGS["repeat_for_minutes"],
        "night_hour": DEFAULT_SETTINGS["night_hour"],
        "morning_hour": DEFAULT_SETTINGS["morning_hour"],
        "early_before_hour": DEFAULT_SETTINGS["early_before_hour"],
        "snooze_minutes": DEFAULT_SETTINGS["snooze_minutes"],
    }
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = None
    if not isinstance(raw, dict):
        return s
    leads = []
    for n in raw.get("lead_minutes") or []:
        try:
            n = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= n <= 180 and n not in leads:
            leads.append(n)
    if leads:
        s["lead_minutes"] = sorted(leads, reverse=True)
    for key, lo, hi in (
        ("repeat_minutes", 1, 30), ("repeat_for_minutes", 0, 180),
        ("night_hour", 0, 23), ("morning_hour", 0, 23),
        ("early_before_hour", 0, 12), ("snooze_minutes", 1, 60),
    ):
        if key not in raw or raw[key] is None:
            continue
        try:
            v = int(raw[key])
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            s[key] = v
    return s


def restriction_family(reason):
    text = (reason or "").lower()
    if "sweep" in text:
        return "sweep"
    if "tow" in text or "rush" in text:
        return "tow"
    return ""


def _at(moment, hour):
    return moment.replace(hour=hour, minute=0, second=0, microsecond=0)


def plan_alerts(move_by, now, reason="", settings=None):
    """Lead alerts, the deadline, and night-before / morning alerts for sweeping and tow-away."""
    s = clean_settings(settings)
    out = []
    family = restriction_family(reason)
    if family and s["night_hour"] is not None:
        night = _at(move_by, s["night_hour"]) - timedelta(days=1)
        if night >= now - timedelta(seconds=5) and night < move_by:
            out.append({"at": night, "kind": "night", "minutes_before": None, "level": "soon"})
    if family and s["morning_hour"] is not None and move_by.hour < s["early_before_hour"]:
        morning = _at(move_by, s["morning_hour"])
        if morning >= move_by:
            morning = move_by - timedelta(hours=1)
        if morning >= now - timedelta(seconds=5) and morning < move_by:
            out.append({"at": morning, "kind": "morning", "minutes_before": None, "level": "soon"})
    for minutes in s["lead_minutes"]:
        fire = move_by - timedelta(minutes=minutes)
        if fire >= now - timedelta(seconds=5):
            level = "loud" if family == "sweep" and minutes == 5 else "soon"
            out.append({"at": fire, "kind": str(minutes), "minutes_before": minutes, "level": level})
    if move_by >= now - timedelta(seconds=5):
        out.append({"at": move_by, "kind": "now", "minutes_before": 0, "level": "final"})
    out.sort(key=lambda item: item["at"])
    kept = []
    for item in out:
        if item["kind"] in ("night", "morning") and any(
                abs(item["at"] - other["at"]) < timedelta(minutes=10) for other in kept):
            continue
        kept.append(item)
    return kept


def reminder_times(move_by, now, reason="", settings=None):
    """Scheduled alerts that are still in the future. Repeats are added when the deadline alert is sent."""
    return plan_alerts(move_by, now, reason, settings)


def next_repeat(deadline, now, settings=None):
    """Next urgent repeat, or None once the post-deadline window has passed."""
    s = clean_settings(settings)
    if not deadline or s["repeat_minutes"] <= 0 or s["repeat_for_minutes"] <= 0:
        return None
    nxt = now + timedelta(minutes=s["repeat_minutes"])
    if nxt <= deadline + timedelta(minutes=s["repeat_for_minutes"]):
        return nxt
    return None


def valid_token(token):
    return bool(TOKEN_RE.match(token or ""))


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


def save_park(lat, lng, address, move_by, reason, headline, sub_id, now, device_token=None, settings=None, snapshot=None):
    """move_by is an aware datetime or None. Reminders are skipped when there is no deadline."""
    pid = uuid.uuid4().hex
    cleaned = clean_settings(settings)
    fires = plan_alerts(move_by, now, reason, cleaned) if move_by else []
    with LOCK:
        conn = connect()
        conn.execute("""INSERT INTO parks
            (id, lat, lng, address, move_by, reason, headline, sub_id, created, cancelled, device_token, settings, snooze_until, snapshot)
            VALUES (?,?,?,?,?,?,?,?,?,0,?,?,NULL,?)""", (
            pid, lat, lng, address or "", move_by.isoformat() if move_by else None,
            reason or "", headline or "", sub_id, now.isoformat(), device_token, json.dumps(cleaned), snapshot))
        for item in fires:
            conn.execute("INSERT INTO reminders(park_id, fire_at, kind, sent) VALUES(?,?,?,0)",
                         (pid, item["at"].isoformat(), item["kind"]))
        conn.commit()
    return {"id": pid, "alarm_at": move_by.isoformat() if move_by else None,
            "reminders": [{"at": i["at"].isoformat(), "kind": i["kind"], "minutes_before": i["minutes_before"],
                           "level": i["level"]} for i in fires]}


def ensure_device(token=None, sub=None, settings=None):
    """Return this phone's token, creating it if needed. A token the phone already
    holds is stored again after a server restart so the Shortcut keeps working."""
    sub_id = upsert_sub(sub) if sub else None
    stored = json.dumps(clean_settings(settings)) if settings else None
    with LOCK:
        conn = connect()
        if valid_token(token):
            row = conn.execute("SELECT token, sub_id FROM devices WHERE token=?", (token,)).fetchone()
            if row:
                if sub_id and sub_id != row[1]:
                    conn.execute("UPDATE devices SET sub_id=? WHERE token=?", (sub_id, token))
                if stored:
                    conn.execute("UPDATE devices SET settings=? WHERE token=?", (stored, token))
                conn.commit()
                return token
            conn.execute("INSERT INTO devices (token, sub_id, created, settings) VALUES(?,?,?,?)",
                         (token, sub_id, datetime.now(TZ).isoformat(), stored))
            conn.commit()
            return token
        fresh = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO devices (token, sub_id, created, settings) VALUES(?,?,?,?)",
                     (fresh, sub_id, datetime.now(TZ).isoformat(), stored))
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
        conn.execute("INSERT INTO devices (token, sub_id, created, settings) VALUES(?,?,?,?)",
                     (fresh, sub_id, datetime.now(TZ).isoformat(), None))
        conn.commit()
    return fresh


def get_device(token):
    if not valid_token(token):
        return None
    conn = connect()
    row = conn.execute("""
        SELECT devices.token, devices.sub_id, devices.settings, subs.endpoint, subs.p256dh, subs.auth
        FROM devices LEFT JOIN subs ON subs.id = devices.sub_id WHERE devices.token=?""", (token,)).fetchone()
    if not row:
        return None
    return {"token": row[0], "sub_id": row[1], "settings": clean_settings(row[2]) if row[2] else None,
            "endpoint": row[3], "p256dh": row[4], "auth": row[5]}


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
    """End the session and drop every alert that has not been sent."""
    with LOCK:
        conn = connect()
        cur = conn.execute("UPDATE parks SET cancelled=1 WHERE id=?", (park_id,))
        conn.execute("DELETE FROM reminders WHERE park_id=? AND sent=0", (park_id,))
        conn.commit()
        return cur.rowcount > 0


def snooze_park(park_id, now, minutes=None):
    """Pause the repeating deadline alert. The 15- and 5-minute warnings still fire."""
    with LOCK:
        conn = connect()
        row = conn.execute(
            "SELECT move_by, settings, cancelled FROM parks WHERE id=?", (park_id,)).fetchone()
        if not row or row[2]:
            return None
        s = clean_settings(row[1])
        try:
            minutes = int(minutes) if minutes else s["snooze_minutes"]
        except (TypeError, ValueError):
            minutes = s["snooze_minutes"]
        minutes = min(60, max(1, minutes))
        until = now + timedelta(minutes=minutes)
        conn.execute("UPDATE parks SET snooze_until=? WHERE id=?", (until.isoformat(), park_id))
        conn.execute("DELETE FROM reminders WHERE park_id=? AND sent=0 AND kind='repeat'", (park_id,))
        deadline = datetime.fromisoformat(row[0]) if row[0] else None
        if deadline and until <= deadline + timedelta(minutes=s["repeat_for_minutes"]):
            conn.execute("INSERT INTO reminders(park_id, fire_at, kind, sent) VALUES(?,?,?,0)",
                         (park_id, until.isoformat(), "repeat"))
        conn.commit()
    return {"snooze_until": until.isoformat(), "minutes": minutes}


REPORT_HOURS = {"temp_no_parking": 8, "broken_meter": 24 * 14, "enforcement": 2}


def create_report(token, lat, lng, kind, note, now, hours=None):
    if not valid_token(token) or not get_device(token):
        raise PermissionError("unknown token")
    if kind not in REPORT_HOURS:
        raise ValueError("kind must be temp_no_parking, broken_meter, or enforcement")
    try:
        hours = int(hours) if hours else REPORT_HOURS[kind]
    except (TypeError, ValueError):
        raise ValueError("bad duration")
    cap = 24 * 30 if kind == "broken_meter" else 48
    if not 1 <= hours <= cap:
        raise ValueError("duration is out of range")
    note = (note or "").strip()[:240]
    with LOCK:
        conn = connect()
        since = (now - timedelta(hours=1)).isoformat()
        recent = conn.execute(
            "SELECT COUNT(*) FROM reports WHERE device_token=? AND created>=?", (token, since)).fetchone()[0]
        if recent >= 5:
            raise ValueError("Too many reports from this phone in the last hour.")
        rid = uuid.uuid4().hex
        expires = now + timedelta(hours=hours)
        conn.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,1)",
                     (rid, token, lat, lng, kind, note, now.isoformat(), expires.isoformat()))
        conn.execute("INSERT INTO report_votes VALUES (?,?)", (rid, token))
        conn.commit()
    return {"id": rid, "kind": kind, "expires": expires.isoformat(), "confirms": 1, "note": note,
            "lat": lat, "lng": lng}


def confirm_report(token, report_id, now):
    if not valid_token(token) or not get_device(token):
        raise PermissionError("unknown token")
    with LOCK:
        conn = connect()
        row = conn.execute("SELECT id, expires, confirms FROM reports WHERE id=?", (report_id,)).fetchone()
        if not row or row[1] <= now.isoformat():
            return None
        if conn.execute("SELECT 1 FROM report_votes WHERE report_id=? AND device_token=?",
                        (report_id, token)).fetchone():
            return {"id": report_id, "confirms": row[2], "already": True}
        since = (now - timedelta(hours=1)).isoformat()
        votes = conn.execute(
            "SELECT COUNT(*) FROM report_votes WHERE device_token=? AND report_id IN "
            "(SELECT id FROM reports WHERE created>=?)", (token, since)).fetchone()[0]
        if votes >= 30:
            raise ValueError("Too many confirms from this phone in the last hour.")
        conn.execute("INSERT INTO report_votes VALUES (?,?)", (report_id, token))
        conn.execute("UPDATE reports SET confirms=confirms+1 WHERE id=?", (report_id,))
        conn.commit()
        confirms = row[2] + 1
    return {"id": report_id, "confirms": confirms, "already": False}


def active_reports(lat, lng, now, radius=150):
    conn = connect()
    box = radius * 1.6e-5
    rows = conn.execute(
        "SELECT id, lat, lng, kind, note, expires, confirms FROM reports WHERE expires>? "
        "AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
        (now.isoformat(), lat - box, lat + box, lng - box, lng + box)).fetchall()
    return [{"id": r[0], "lat": r[1], "lng": r[2], "kind": r[3], "note": r[4],
             "expires": r[5], "confirms": r[6]} for r in rows]


def reschedule_paid(park_id, paid_until, now):
    """Point alerts at the time the meter is paid through. Sweeping and tow-away stay sooner."""
    with LOCK:
        conn = connect()
        row = conn.execute(
            "SELECT move_by, reason, settings, cancelled FROM parks WHERE id=?", (park_id,)).fetchone()
        if not row or row[3]:
            return None
        old = datetime.fromisoformat(row[0]) if row[0] else None
        if restriction_family(row[1]) and old and paid_until > old:
            return {"ok": False, "error": "A sweeping or tow-away time is sooner than this payment. Alerts stay at that earlier deadline.",
                    "alarm_at": row[0]}
        if paid_until <= now:
            return {"ok": False, "error": "Paid-until has to be later than now."}
        conn.execute("DELETE FROM reminders WHERE park_id=? AND sent=0", (park_id,))
        conn.execute("UPDATE parks SET move_by=?, reason=? WHERE id=?",
                     (paid_until.isoformat(), "Meter payment ends", park_id))
        fires = plan_alerts(paid_until, now, "Meter payment ends", row[2])
        for item in fires:
            conn.execute("INSERT INTO reminders(park_id, fire_at, kind, sent) VALUES(?,?,?,0)",
                         (park_id, item["at"].isoformat(), item["kind"]))
        conn.commit()
    return {"ok": True, "alarm_at": paid_until.isoformat(), "reason": "Meter payment ends",
            "reminders": [{"at": i["at"].isoformat(), "kind": i["kind"], "level": i["level"]} for i in fires]}


def get_park(park_id):
    conn = connect()
    row = conn.execute(
        "SELECT id, lat, lng, address, move_by, reason, headline, cancelled, created, settings, snooze_until, snapshot FROM parks WHERE id=?",
        (park_id,)).fetchone()
    if not row:
        return None
    keys = ("id", "lat", "lng", "address", "move_by", "reason", "headline", "cancelled", "created", "settings", "snooze_until", "snapshot")
    return dict(zip(keys, row))


def _ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _ics_stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(uid, start, summary, description, leads=None):
    """One event at the deadline with an alarm for each lead time and one at the deadline."""
    leads = clean_settings({"lead_minutes": leads} if leads else None)["lead_minutes"]
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
    alarms = [(f"-PT{m}M", f"Move your car in {m} minutes") for m in leads]
    alarms.append(("PT0S", "Move your car now"))
    for trigger, text in alarms:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"TRIGGER:{trigger}", "DESCRIPTION:" + _ics_escape(text), "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def push_configured():
    return bool(os.environ.get("VAPID_PUBLIC_KEY") and os.environ.get("VAPID_PRIVATE_KEY"))


def alert_content(kind, address, reason, pid):
    """Copy and urgency for one push. iOS cannot set a custom sound; high urgency is the strongest Web Push option."""
    place = address or "your spot"
    why = reason or "Time to move the car."
    urgent = kind in ("now", "repeat")
    loud_sweep = str(kind) == "5" and restriction_family(reason) == "sweep"
    if loud_sweep:
        title, body = "Street sweeping in 5 minutes", f"Move before the sweeper. {place}. {why}"
        return {
            "title": title, "body": body, "tag": f"park-{pid}-sweep5-{int(time.time())}",
            "url": "/?park=" + pid, "level": "loud", "requireInteraction": True, "urgent": True,
            "renotify": True, "vibrate": [600, 200, 600, 200, 800],
        }
    if kind == "night":
        title, body = "Tomorrow: move the car", f"{why} {place}."
    elif kind == "morning":
        title, body = "This morning: move the car", f"Move it before the restriction starts. {why} {place}."
    elif kind == "repeat":
        title, body = "Still there — move the car", f"You have not confirmed the car was moved. {place}. {why}"
    elif kind == "now":
        title, body = "Move your car now", f"The deadline is here. {place}. {why}"
    elif str(kind).isdigit():
        title, body = f"Move your car in {kind} minutes", f"{place}. {why}"
    else:
        title, body = "Move your car", f"{place}. {why}"
    # A new tag on each urgent alert so iOS plays a sound instead of silently replacing the last one.
    tag = f"park-{pid}-{kind}-{int(time.time())}" if urgent else f"park-{pid}-{kind}"
    return {
        "title": title, "body": body, "tag": tag, "url": "/?park=" + pid,
        "level": "final" if urgent else "soon", "requireInteraction": urgent, "urgent": urgent,
        "renotify": True, "vibrate": [500, 200, 500, 200, 700] if urgent else [200, 100, 200],
    }


def send_web_push(sub, payload):
    from pywebpush import webpush, WebPushException
    pem = os.environ["VAPID_PRIVATE_KEY"].replace("\\n", "\n")
    headers = {"Urgency": "high"} if payload.get("urgent") else {"Urgency": "normal"}
    try:
        webpush(
            subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
            data=json.dumps(payload),
            vapid_private_key=pem,
            vapid_claims={"sub": os.environ.get("VAPID_SUBJECT") or "mailto:la-parking@example.com"},
            ttl=600 if payload.get("urgent") else 3600,
            headers=headers,
        )
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return "gone"
        print("push failed:", code or e, flush=True)
        return False


def send_due(now, sender=None):
    """Send reminders whose time has come. A lead that is more than 10 minutes late is dropped.
    Night-before and morning alerts are still sent if the deadline has not passed."""
    sender = sender or send_web_push
    if sender is send_web_push and not push_configured():
        return 0
    with LOCK:
        conn = connect()
        rows = conn.execute("""
            SELECT reminders.id, reminders.kind, reminders.fire_at, parks.id, parks.address, parks.reason,
                   parks.sub_id, parks.move_by, parks.settings, parks.snooze_until,
                   subs.endpoint, subs.p256dh, subs.auth
            FROM reminders
            JOIN parks ON parks.id = reminders.park_id
            LEFT JOIN subs ON subs.id = parks.sub_id
            WHERE reminders.sent=0 AND parks.cancelled=0 AND reminders.fire_at<=?
        """, (now.isoformat(),)).fetchall()
    sent = 0
    for rid, kind, fire_at, pid, address, reason, sub_id, move_by, settings, snooze_until, endpoint, p256dh, auth in rows:
        fire = datetime.fromisoformat(fire_at)
        deadline = datetime.fromisoformat(move_by) if move_by else None
        grace = timedelta(hours=6) if kind in ("night", "morning") else timedelta(minutes=10)
        if now - fire > grace or (kind in ("night", "morning") and deadline and now >= deadline):
            with LOCK:
                c = connect()
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
                c.commit()
            continue
        if kind == "repeat" and snooze_until and datetime.fromisoformat(snooze_until) > now:
            continue
        if not endpoint:
            continue
        payload = alert_content(kind, address, reason, pid)
        result = sender({"endpoint": endpoint, "p256dh": p256dh, "auth": auth}, payload)
        if result:
            with LOCK:
                c = connect()
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (rid,))
                if result == "gone" and sub_id:
                    c.execute("DELETE FROM subs WHERE id=?", (sub_id,))
                if kind in ("now", "repeat") and result is True:
                    nxt = next_repeat(deadline, now if now > fire else fire, settings)
                    snoozed = snooze_until and datetime.fromisoformat(snooze_until) > now
                    if nxt and not snoozed:
                        c.execute("INSERT INTO reminders(park_id, fire_at, kind, sent) VALUES(?,?,?,0)",
                                  (pid, nxt.isoformat(), "repeat"))
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
