"""LA parking check, parked-car alarms, and the phone web app.

GET  /check?lat=&lng=[&at=][&address=][&has_permit=1]
POST /check   {"lat","lng","at","sign","has_permit"}
POST /park    same body; saves the spot and schedules 15 / 5 / 0 reminders
GET  /park/<id>  and  /park/<id>.ics
POST /park/<id>/cancel
GET  /push/public-key
POST /push/subscribe  {"subscription","park_id"}

Run:  python server.py   (port from $PORT, default 8080)
"""
import json, math, os, re, sqlite3, threading, urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlencode, urlparse, parse_qs
from zoneinfo import ZoneInfo

import alarms
import citylinks
import parkdetect
import signread
from ingest import DB as DATA_DB
from rules import (Rule, WEEKDAYS, address_side, answer, apply_curb_side, apply_sign, clock_label,
                   holiday_calendar, parse_days, permit_covers, rule_from_json, to_dict)
from signparse import sign_conflicts

DB = os.environ.get("PARKING_DB", DATA_DB)
TZ = ZoneInfo("America/Los_Angeles")
ROOT = os.path.dirname(os.path.abspath(__file__))
COVERAGE = ("Uses LADOT meter locations, rates, and time limits; posted sweeping areas that include "
            "days and hours; major-street sweeping areas; and permit-district boundaries. "
            "Meter hours, tow-away zones, and red curbs are not in those feeds.")
DISCLAIMER = "Posted signs always override this app. Data may be out of date."
SOURCES = [
    {"name": "LADOT metered parking inventory and policies", "url": "https://data.lacity.org/Transportation/LADOT-Metered-Parking-Inventory-Policies/s49e-q6j2"},
    {"name": "Posted street sweeping areas", "url": "https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/arcgis/rest/services/Posted_Street_Sweeping_Routes_Update/FeatureServer"},
    {"name": "Posted street sweeping route list", "url": "https://data.lacity.org/Transportation/Posted-Street-Sweeping-Routes/krk7-ayq2"},
    {"name": "BSS major-street sweeping areas (ADAPT)", "url": "https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/arcgis/rest/services/ADAPT_AMRoutes/FeatureServer"},
    {"name": "LADOT preferential parking districts", "url": "https://data.lacity.org/dataset/LADOT_PPD/s3st-6nwi"},
    {"name": "LADOT holiday parking exemptions", "url": "https://ladotparking.org/parking-meters/parking-restrictions-holiday-parking/"},
]
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/sw.js": ("sw.js", "text/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("static/icon-192.png", "image/png"),
    "/icon-512.png": ("static/icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("static/apple-touch-icon.png", "image/png"),
}

def dist(a, b, c, d):
    r = math.pi / 180
    h = math.sin((c - a) * r / 2) ** 2 + math.cos(a * r) * math.cos(c * r) * math.sin((d - b) * r / 2) ** 2
    return 12742000 * math.asin(math.sqrt(h))

def inside(lng, lat, geom):
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    for poly in polys:
        c = False
        ring = poly[0]
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lng < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
                c = not c
        if c:
            return True
    return False

FULL = r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
ABBR = r"^(mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?$"

def sweep_day(props):
    """Prefer fields named like 'day'; otherwise only accept full day names (avoids 'Sunset', 'Sun Valley')."""
    for k, val in props.items():
        if "day" in k.lower() or "dow" in k.lower():
            m = re.match(ABBR, str(val).strip().lower())
            if m:
                return [d for d in range(7) if WEEKDAYS[d][:3].lower() == m[1][:3]][0]
    m = re.search(FULL, " ".join(str(x) for x in props.values()).lower())
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].index(m[1][:3]) if m else None

def near(lng, lat, geom, meters=30):
    """True if the point is within `meters` of a street line."""
    k = math.cos(lat * math.pi / 180) * 111320
    px, py = lng * k, lat * 110574
    lines = [geom["coordinates"]] if geom["type"] == "LineString" else geom["coordinates"]
    for line in lines:
        for (x1, y1), (x2, y2) in zip(line, line[1:]):
            ax, ay, bx, by = x1 * k, y1 * 110574, x2 * k, y2 * 110574
            dx, dy = bx - ax, by - ay
            t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            if math.hypot(px - ax - t * dx, py - ay - t * dy) <= meters:
                return True
    return False

def clock_span(text):
    """'10:00 am - 12:00 pm' -> (600, 720). None when the text has no range."""
    def m(h, mi, ap):
        return (int(h) % 12 + (12 if ap == "pm" else 0)) * 60 + int(mi or 0)
    t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\D+?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?", text or "")
    if t:
        return m(t[1], t[2], t[3] + "m"), m(t[4], t[5], t[6] + "m")
    h = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)", text or "")
    if len(h) >= 2:
        return int(h[0][0]) * 60 + int(h[0][1]), int(h[1][0]) * 60 + int(h[1][1])
    return None

def _sweep_side(props):
    raw = str(props.get("Odd_Even") or props.get("odd_even") or props.get("side") or "").strip().lower()
    if raw in ("odd", "even", "left", "right"):
        return raw
    return ""


def sweep_from_props(props):
    """Return (rules, warning). Hours are attached only when the data actually has them."""
    posted_time = props.get("Posted_Time") or props.get("posted_time") or ""
    day_text = props.get("Day_Short") or props.get("Posted_Day") or ""
    if posted_time and day_text:
        days = parse_days(day_text)
        span = clock_span(posted_time)
        if days and span and span[0] != span[1]:
            route = str(props.get("Route") or props.get("route") or "").strip()
            label = "Street sweeping" + (f" ({route})" if route else "")
            bounds = str(props.get("Boundaries") or props.get("boundaries") or "").strip()
            warn = "Sweeping area, not one curb side."
            if bounds:
                warn += " " + bounds + "."
            warn += " Check the sign."
            return [Rule("no", days, span[0], span[1], label,
                         source="Posted street sweeping areas", confidence="low", skip_holidays=True,
                         also_sweep_holidays=True, topic="sweep", side=_sweep_side(props))], warn
    posted = props.get("posted_times") or []
    if posted:
        rules = []
        for w in posted:
            rules.append(Rule(
                "no", list(w["days"]), int(w["start"]), int(w["end"]), "Street sweeping (check the sign)",
                source="Posted sweeping routes", confidence="low", skip_holidays=True,
                also_sweep_holidays=True, topic="sweep"))
        return rules, "Sweeping hours were matched by route id. The map is an area, not one curb, so check the sign."
    days = parse_days(props.get("Day") or props.get("day") or "")
    if not days:
        legacy = sweep_day(props)
        days = [legacy] if legacy is not None else []
    blob = " ".join(str(v) for v in props.values() if not isinstance(v, (dict, list))).lower()
    span = clock_span(blob) if days else None
    if days and span and span[0] != span[1]:
        return [Rule("no", days, span[0], span[1], "Street sweeping (check the sign)",
                     source="Posted sweeping routes", confidence="low", skip_holidays=True,
                     also_sweep_holidays=True, topic="sweep")], None
    if days:
        names = " and ".join(WEEKDAYS[d] for d in days)
        return [], f"This major-street area is swept on {names}, but the city map has no hours for this segment. Check the sign."
    return [], None

def geocode(addr):
    """Free US Census geocoder. Returns (lat, lng, matched address) or None."""
    if "," not in addr:
        addr += ", Los Angeles, CA"
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + urlencode(
        {"address": addr, "benchmark": "Public_AR_Current", "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "la-parking-app/1.0"})
    with urllib.request.urlopen(req, timeout=20) as f:
        m = json.load(f)["result"]["addressMatches"]
    if m:
        return float(m[0]["coordinates"]["y"]), float(m[0]["coordinates"]["x"]), m[0]["matchedAddress"]

def find_spot(lat, lng, radius=40):
    """Nearest meter (the curb segment), sweeping areas, and a permit district."""
    rules, address, conf, warnings = [], None, "low", []
    distance_m = space_id = None
    meter_used = False
    permit_district = None
    try:
        db = sqlite3.connect(DB)
    except sqlite3.Error as e:
        return _empty_spot([f"Parking data could not be opened ({e})."])
    try:
        box = 300 / 111000
        try:
            rows = db.execute(
                "SELECT lat,lng,address,rules,id FROM meters WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                (lat - box, lat + box, lng - box, lng + box)).fetchall()
        except sqlite3.OperationalError:
            rows = db.execute(
                "SELECT lat,lng,address,rules,NULL FROM meters WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                (lat - box, lat + box, lng - box, lng + box)).fetchall()
        if rows:
            best = min(rows, key=lambda r: dist(lat, lng, r[0], r[1]))
            distance_m = int(round(dist(lat, lng, best[0], best[1])))
            space_id = best[4]
            if distance_m <= radius:
                rules += [rule_from_json(r) for r in json.loads(best[3])]
                address, conf, meter_used = best[2], "high" if rules else "low", True
            elif distance_m <= 300:
                warnings.append(f"The nearest meter is {distance_m} m away ({best[2] or 'unknown block'}), so its time limit was not applied to this curb.")
        seen = set()
        bare_sweep = []
        try:
            sweep_rows = db.execute(
                "SELECT props,geom FROM sweep WHERE ? BETWEEN minlat AND maxlat AND ? BETWEEN minlng AND maxlng",
                (lat, lng))
        except sqlite3.OperationalError:
            sweep_rows = []
        for props, geom in sweep_rows:
            g = json.loads(geom)
            if not (inside(lng, lat, g) if g["type"] in ("Polygon", "MultiPolygon") else near(lng, lat, g)):
                continue
            found, warn = sweep_from_props(json.loads(props))
            for r in found:
                key = (tuple(r.days), r.start, r.end, r.label)
                if key not in seen:
                    seen.add(key)
                    rules.append(r)
            if not warn or warn in warnings:
                continue
            if found and len([w for w in warnings if "swept" in w or "Sweeping" in w]) < 3:
                warnings.append(warn)
            elif not found:
                bare_sweep.append(warn)
        if bare_sweep and not any(r.topic == "sweep" for r in rules):
            for warn in bare_sweep:
                if warn not in warnings and len([w for w in warnings if "swept" in w or "Sweeping" in w]) < 3:
                    warnings.append(warn)
        try:
            for props, geom in db.execute(
                    "SELECT props,geom FROM ppd WHERE ? BETWEEN minlat AND maxlat AND ? BETWEEN minlng AND maxlng",
                    (lat, lng)):
                if inside(lng, lat, json.loads(geom)):
                    p = json.loads(props)
                    permit_district = p.get("ppdname") or p.get("PPDNAME") or "a preferential parking district"
                    warnings.append(
                        f"{permit_district}: a permit may be required. Hours are on the sign, not in the city data. "
                        "Many districts still allow a short stay without a permit.")
                    break
        except sqlite3.OperationalError:
            pass
    except sqlite3.OperationalError:
        warnings.append("Parking data is not loaded on the server yet.")
    finally:
        db.close()
    if rules and any(not r.hours_known for r in rules if r.kind == "limit"):
        conf = "low"
    return {"rules": rules, "address": address, "confidence": conf if rules else "none", "warnings": warnings,
            "distance_m": distance_m if meter_used else distance_m, "meter_used": meter_used,
            "space_id": space_id if meter_used else None, "segment": address,
            "permit_district": permit_district}

def _empty_spot(warnings):
    return {"rules": [], "address": None, "confidence": "none", "warnings": warnings,
            "distance_m": None, "meter_used": False, "space_id": None, "segment": None, "permit_district": None}

def lookup(lat, lng, now, radius=40):
    s = find_spot(lat, lng, radius)
    return s["rules"], s["address"], s["confidence"], s["warnings"]

def block_spaces(address, lat, lng, limit=8):
    """Other meters on the same block face, so the screen can show the zone."""
    if not address:
        return []
    try:
        db = sqlite3.connect(DB)
        rows = db.execute(
            "SELECT id, lat, lng, rules FROM meters WHERE address=? AND lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
            (address, lat - 0.002, lat + 0.002, lng - 0.002, lng + 0.002)).fetchall()
        db.close()
    except sqlite3.Error:
        return []
    out = []
    for space, mlat, mlng, raw in rows:
        try:
            rules = json.loads(raw or "[]")
        except (TypeError, ValueError):
            rules = []
        limit_min = next((r.get("limit_min") for r in rules if r.get("limit_min")), None)
        rate = next((r.get("rate") for r in rules if r.get("rate")), "")
        out.append({"space_id": space, "distance_m": int(round(dist(lat, lng, mlat, mlng))),
                    "time_limit_min": limit_min, "rate": rate})
    out.sort(key=lambda item: item["distance_m"])
    return out[:limit]


def assemble(lat, lng, now, sign=None, has_permit=False, radius=40, searched=None, permits=None, curb_side=None):
    spot = find_spot(lat, lng, radius)
    saved_permit = permit_covers(spot.get("permit_district"), permits)
    if saved_permit:
        has_permit = True
    rules = [r for r in spot["rules"] if not (has_permit and r.kind == "permit")]
    conflict_notes = sign_conflicts(rules, sign) if sign else []
    if sign:
        rules = apply_sign(rules, sign)
    rules, side_note = apply_curb_side(rules, curb_side)
    out = answer(rules, now)
    if spot["meter_used"] and spot["distance_m"] and spot["distance_m"] > 25:
        spot["warnings"] = list(spot["warnings"]) + [
            f"This meter is {spot['distance_m']} m from the GPS point. Confirm the space number on the meter."]
    note = out.get("holiday_note")
    warnings = list(spot["warnings"]) + [n for n in conflict_notes if n not in spot["warnings"]]
    if note and note not in warnings:
        warnings.insert(0, note)
    if not rules and not warnings:
        warnings.append("Nothing in the city data matches this curb. Read the sign, or the block may be outside Los Angeles.")
    if saved_permit and spot.get("permit_district"):
        warnings.append("Your saved permit matches " + spot["permit_district"] + ", so that permit rule is not applied to you.")
    if side_note:
        warnings.append(side_note)
    reports = _report_view(alarms.active_reports(lat, lng, now), lat, lng)
    blocks = [r for r in reports if r["kind"] == "temp_no_parking" and r["distance_m"] <= 40]
    if blocks:
        warnings.insert(0, "Temporary no-parking reported nearby until " + blocks[0]["expires"] + ". " + (blocks[0].get("note") or ""))
        out.update(status="no", can_park=False, answer_word="No", headline="Do not park here",
                   reason="Temporary no-parking reported nearby", move_by=None, move_by_text="Now")
    out.update(
        searched=searched, address=spot["address"], segment=spot["segment"], space_id=spot["space_id"],
        distance_m=spot["distance_m"] if spot["meter_used"] else None,
        nearest_meter_m=spot["distance_m"], meter_used=spot["meter_used"],
        permit_district=spot["permit_district"], confidence=spot["confidence"] if not sign else "high",
        warnings=warnings, coverage=COVERAGE, rules=[to_dict(r) for r in rules],
        disclaimer=DISCLAIMER, sources=SOURCES, lat=lat, lng=lng,
        checked_at=now.isoformat(), reports=reports,
        payment=citylinks.payment_options(spot.get("space_id"), lat=lat, lng=lng) if spot.get("meter_used") or lat else citylinks.payment_options(lat=lat, lng=lng),
        saved_permit=saved_permit, curb_side=(curb_side or "").strip().lower(),
        address_side=address_side(spot.get("address")),
        block=block_spaces(spot.get("address"), lat, lng) if spot.get("meter_used") else [])
    if out.get("hours_known") is False:
        out["confidence"] = "low"
    return out

def _report_view(rows, lat, lng):
    out = []
    for r in rows:
        item = dict(r)
        item["distance_m"] = int(round(dist(lat, lng, r["lat"], r["lng"])))
        if item["distance_m"] <= 150:
            out.append(item)
    out.sort(key=lambda r: r["distance_m"])
    return out


def legal_nearby(lat, lng, now, radius=350):
    """Curb segments within a few blocks that are legal right now, nearest first."""
    try:
        db = sqlite3.connect(DB)
    except sqlite3.Error:
        return []
    box = radius * 1.6e-5
    try:
        rows = db.execute(
            "SELECT lat,lng,address,rules,id FROM meters WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
            (lat - box, lat + box, lng - box, lng + box)).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        db.close()
    best = {}
    for row in rows:
        meters = dist(lat, lng, row[0], row[1])
        if meters > radius:
            continue
        key = row[2] or row[4] or (row[0], row[1])
        if key not in best or meters < best[key][0]:
            best[key] = (meters, row)
    blocked = alarms.active_reports(lat, lng, now, radius)
    spots = []
    for meters, row in list(best.values())[:40]:
        if any(r["kind"] == "temp_no_parking" and dist(row[0], row[1], r["lat"], r["lng"]) <= 40 for r in blocked):
            continue
        try:
            rules = [rule_from_json(item) for item in json.loads(row[3] or "[]")]
        except (TypeError, ValueError):
            continue
        ans = answer(rules, now)
        if ans.get("can_park") is not True:
            continue
        stay = None
        if ans.get("move_by"):
            stay = max(0, int((datetime.fromisoformat(ans["move_by"]) - now).total_seconds() // 60))
        spots.append({"lat": row[0], "lng": row[1], "address": row[2], "space_id": row[4],
                      "distance_m": int(round(meters)), "stay_minutes": stay,
                      "move_by_text": ans.get("move_by_text"), "headline": ans.get("headline")})
    spots.sort(key=lambda s: (s["distance_m"], -(s["stay_minutes"] if s["stay_minutes"] is not None else 10**9)))
    return spots[:12]


def evidence_text(park_id):
    row = alarms.get_park(park_id)
    if not row:
        return None
    conn = alarms.connect()
    photos = conn.execute("SELECT path, created FROM photos WHERE park_id=?", (park_id,)).fetchall()
    lines = [
        "Los Angeles parking session evidence",
        "Session: " + row["id"],
        "Saved: " + (row.get("created") or ""),
        "Location: " + str(row.get("lat")) + ", " + str(row.get("lng")),
        "Place: " + (row.get("address") or ""),
        "Move by: " + (row.get("move_by") or "none"),
        "Reason: " + (row.get("reason") or ""),
        "Headline: " + (row.get("headline") or ""),
        "",
        "Rules and warnings shown:",
        row.get("snapshot") or "(no snapshot was stored)",
        "",
        "Sign photos stored with this session:",
    ]
    if photos:
        for path, created in photos:
            lines.append("- " + os.path.basename(path) + " saved " + (created or ""))
    else:
        lines.append("(none)")
    lines += ["", "Posted signs override this app. This export is a record of what the app showed, not a copy of the citation."]
    return "\n".join(lines) + "\n"


def _alarm_at(result, now):
    if result["status"] in ("no", "permit"):
        return now
    if result.get("move_by"):
        return datetime.fromisoformat(result["move_by"])
    return None

def park(lat, lng, now, sign=None, has_permit=False, radius=40, sub_id=None, searched=None, device_token=None, settings=None, permits=None, curb_side=None):
    result = assemble(lat, lng, now, sign, has_permit, radius, searched, permits, curb_side)
    when = _alarm_at(result, now)
    snapshot = json.dumps({"rules": result.get("rules"), "warnings": result.get("warnings"),
                           "headline": result.get("headline"), "checked_at": result.get("checked_at")})
    saved = alarms.save_park(lat, lng, result.get("address") or result.get("segment"), when,
                             result.get("reason"), result.get("headline"), sub_id, now, device_token, settings, snapshot)
    result.update(saved)
    result["alarm_at"] = saved["alarm_at"]
    return result

def parked_message(place, when, now):
    """Confirmation line: 'Parked at X, move by Y'."""
    place = place or "this curb"
    if when is None:
        return f"Parked at {place}. No move-by time is in the city data."
    if when <= now + timedelta(minutes=1):
        return f"Parked at {place}, move now"
    return f"Parked at {place}, move by {clock_label(when)}"

# CarPlay and Bluetooth often both disconnect. A couple of minutes covers both
# arrivals, including a GPS reading that has drifted from the first one.
AUTOPARK_DEDUPE = timedelta(minutes=3)

def autopark(lat, lng, token, now, sender=None):
    """Save the spot from a Shortcut and send 'Parked at X, move by Y'."""
    device = alarms.get_device(token)
    if not device:
        raise PermissionError("unknown token")
    prev = alarms.latest_device_park(token)
    if prev and prev.get("created"):
        created = datetime.fromisoformat(prev["created"])
        if abs(now - created) <= AUTOPARK_DEDUPE:
            when = datetime.fromisoformat(prev["move_by"]) if prev.get("move_by") else None
            return {"ok": True, "duplicate": True, "id": prev["id"], "alarm_at": prev.get("move_by"),
                    "address": prev.get("address"), "reason": prev.get("reason"), "headline": prev.get("headline"),
                    "lat": prev["lat"], "lng": prev["lng"], "reminders": [],
                    "message": parked_message(prev.get("address"), when, now), "push_sent": False}
    result = park(lat, lng, now, sub_id=device.get("sub_id"), device_token=token, settings=device.get("settings"))
    when = datetime.fromisoformat(result["alarm_at"]) if result.get("alarm_at") else None
    place = result.get("address") or result.get("segment")
    message = parked_message(place, when, now)
    payload = {"title": "Parked", "body": message, "tag": "autopark-" + result["id"], "url": "/?park=" + result["id"]}
    sent = alarms.push_to_sub(
        {"endpoint": device.get("endpoint"), "p256dh": device.get("p256dh"), "auth": device.get("auth")},
        payload, sender)
    return {"ok": True, "duplicate": False, "id": result["id"], "alarm_at": result.get("alarm_at"),
            "address": place, "reason": result.get("reason"), "headline": result.get("headline"),
            "lat": lat, "lng": lng, "reminders": result.get("reminders") or [],
            "message": message, "push_sent": bool(sent), "move_by_text": result.get("move_by_text")}

def api_autopark(body, authorization, now, sender=None):
    """HTTP contract for POST /api/autopark. Returns (status, json)."""
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization.split(None, 1)[1].strip()
    body_token = str((body or {}).get("token") or "").strip()
    token = bearer if alarms.get_device(bearer) else body_token if alarms.get_device(body_token) else ""
    if not token:
        return 401, {"error": "Send a device token from Set up auto-park."}
    try:
        lat, lng = float(body["lat"]), float(body["lng"])
    except (KeyError, TypeError, ValueError):
        return 400, {"error": "Send lat and lng"}
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return 400, {"error": "lat or lng is out of range"}
    return 200, autopark(lat, lng, token, now, sender)

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(self.address_string(), fmt % args, flush=True)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if path in FILES:
            rel, ctype = FILES[path]
            return self.send_file(rel, ctype, "no-cache" if rel.endswith((".html", ".js", ".webmanifest")) else "public, max-age=86400")
        if path in ("/autopark",):
            return self.send_file("index.html", "text/html; charset=utf-8", "no-cache")
        if path == "/holidays":
            try:
                year = int(q["year"]) if "year" in q else datetime.now(TZ).year
            except ValueError:
                return self.reply(400, {"error": "bad year"})
            return self.reply(200, {"year": year, "holidays": holiday_calendar(year)})
        if path == "/nearby":
            try:
                lat, lng = float(q["lat"]), float(q["lng"])
            except (KeyError, ValueError):
                return self.reply(400, {"error": "give lat and lng"})
            return self.reply(200, {"spots": legal_nearby(lat, lng, self.when(q.get("at")))})
        if path == "/pay":
            try:
                lat = float(q["lat"]) if q.get("lat") else None
                lng = float(q["lng"]) if q.get("lng") else None
            except ValueError:
                lat = lng = None
            return self.reply(200, citylinks.payment_options(q.get("space"), q.get("zone"), lat, lng))
        if path == "/help":
            return self.reply(200, {"links": citylinks.help_links()})
        if path == "/push/public-key":
            return self.reply(200, {"publicKey": os.environ.get("VAPID_PUBLIC_KEY", ""), "configured": alarms.push_configured()})
        if path == "/api/device/latest":
            return self.latest_park()
        ics = re.fullmatch(r"/park/([a-f0-9]{32})\.ics", path)
        if ics:
            return self.calendar(ics.group(1))
        evidence = re.fullmatch(r"/park/([a-f0-9]{32})/evidence", path)
        if evidence:
            text = evidence_text(evidence.group(1))
            if text is None:
                return self.reply(404, {"error": "That parked session is gone."})
            data = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=parking-evidence.txt")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return self.wfile.write(data)
        one = re.fullmatch(r"/park/([a-f0-9]{32})", path)
        if one:
            row = alarms.get_park(one.group(1))
            return self.reply(200, row) if row else self.reply(404, {"error": "That parked session is gone. Park again."})
        if path != "/check":
            return self.reply(404, {"error": "use /check?lat=&lng="})
        try:
            lat, lng, searched, radius = self.locate(q)
        except LookupError as e:
            return self.reply(200, {"status": "unknown", "headline": str(e), "answer_word": "Unknown",
                                    "can_park": None, "confidence": "none", "warnings": [], "gaps": [],
                                    "coverage": COVERAGE, "disclaimer": DISCLAIMER, "sources": SOURCES})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        now = self.when(q.get("at"))
        permits = [p for p in (q.get("permits") or "").split("|") if p.strip()]
        self.reply(200, assemble(lat, lng, now, has_permit=q.get("has_permit") == "1", radius=radius, searched=searched, permits=permits, curb_side=q.get("side")))

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = self.read_json(12_000_000 if path == "/api/sign" else 100_000)
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        if path == "/check":
            return self.check_body(body)
        if path == "/park":
            return self.park_body(body)
        if path == "/api/autopark":
            code, payload = api_autopark(body, self.headers.get("Authorization"), self.when(body.get("at")))
            return self.reply(code, payload)
        if path == "/api/device":
            return self.device_http(body)
        if path == "/api/sign":
            return self.sign_body(body)
        if path == "/api/reports":
            return self.report_body(body)
        if path == "/api/park-detect":
            return self.detect_body(body)
        confirm = re.fullmatch(r"/api/reports/([a-f0-9]{32})/confirm", path)
        if confirm:
            return self.confirm_body(confirm.group(1), body)
        paid = re.fullmatch(r"/park/([a-f0-9]{32})/paid", path)
        if paid:
            return self.paid_body(paid.group(1), body)
        if path == "/push/subscribe":
            return self.subscribe(body)
        cancel = re.fullmatch(r"/park/([a-f0-9]{32})/cancel", path)
        if cancel:
            ok = alarms.cancel_park(cancel.group(1))
            return self.reply(200, {"cancelled": ok})
        snooze = re.fullmatch(r"/park/([a-f0-9]{32})/snooze", path)
        if snooze:
            try:
                result = alarms.snooze_park(snooze.group(1), self.when(body.get("at")), body.get("minutes"))
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            return self.reply(200, result) if result else self.reply(404, {"error": "That parked session is gone."})
        self.reply(404, {"error": "unknown request"})

    def locate(self, q):
        searched, radius = None, 40
        if q.get("address"):
            try:
                g = geocode(str(q["address"])[:200])
            except Exception:
                g = None
            if not g:
                raise LookupError("Couldn’t find that address")
            return g[0], g[1], g[2], 75
        try:
            return float(q["lat"]), float(q["lng"]), None, radius
        except (KeyError, TypeError, ValueError):
            raise ValueError("give lat and lng, or an address")

    def when(self, raw):
        if not raw:
            return datetime.now(TZ)
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("bad time")
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ)

    def check_body(self, body):
        try:
            lat, lng, searched, radius = self.locate(body)
            now = self.when(body.get("at"))
            out = assemble(lat, lng, now, sign=body.get("sign"), has_permit=bool(body.get("has_permit")),
                           radius=radius, searched=searched, permits=body.get("permits"), curb_side=body.get("side"))
        except LookupError as e:
            return self.reply(200, {"status": "unknown", "headline": str(e), "answer_word": "Unknown", "can_park": None,
                                    "warnings": [], "gaps": [], "coverage": COVERAGE, "disclaimer": DISCLAIMER})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        self.reply(200, out)

    def park_body(self, body):
        try:
            lat, lng, searched, radius = self.locate(body)
            now = self.when(body.get("at"))
            sub_id = None
            if body.get("subscription"):
                sub_id = alarms.upsert_sub(body["subscription"])
            out = park(lat, lng, now, sign=body.get("sign"), has_permit=bool(body.get("has_permit")),
                       radius=radius, sub_id=sub_id, searched=searched, settings=body.get("settings"),
                       permits=body.get("permits"), curb_side=body.get("side"))
            if body.get("scan_id"):
                out["photos_saved"] = signread.attach_scan(str(body["scan_id"])[:64], out["id"])
        except LookupError as e:
            return self.reply(200, {"status": "unknown", "headline": str(e)})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        self.reply(200, out)

    def subscribe(self, body):
        try:
            sub = body.get("subscription") or body
            alarms.upsert_sub(sub)
            attached = False
            if body.get("park_id"):
                attached = alarms.attach_sub(body["park_id"], sub)
            if body.get("token"):
                alarms.ensure_device(body["token"], sub)
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        self.reply(200, {"ok": True, "attached": attached, "configured": alarms.push_configured()})

    def device_http(self, body):
        sub = body.get("subscription")
        try:
            if body.get("rotate"):
                token = alarms.rotate_device(str(body.get("token") or ""), sub)
            else:
                token = alarms.ensure_device(str(body.get("token") or "") or None, sub, body.get("settings"))
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        base = self.public_base()
        self.reply(200, {"token": token, "autopark_url": base + "/api/autopark", "open_url": base + "/autopark",
                         "push_configured": alarms.push_configured()})

    def latest_park(self):
        auth = self.headers.get("Authorization") or ""
        token = auth.split(None, 1)[1].strip() if auth.lower().startswith("bearer ") else ""
        row = alarms.latest_device_park(token)
        if not row:
            return self.reply(404, {"error": "No parked session for that token."})
        self.reply(200, row)

    def public_base(self):
        proto = self.headers.get("X-Forwarded-Proto") or "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return proto + "://" + host

    def calendar(self, park_id):
        row = alarms.get_park(park_id)
        if not row or not row.get("move_by"):
            return self.reply(404, {"error": "No deadline to put on the calendar. Enter the sign first."})
        start = datetime.fromisoformat(row["move_by"])
        why = row.get("reason") or "Move the car"
        where = row.get("address") or "your parking spot"
        leads = alarms.clean_settings(row.get("settings"))["lead_minutes"] if row.get("settings") else None
        body = alarms.build_ics(park_id, start, "Move your car — " + where, why + ". Posted signs override this reminder.", leads)
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/calendar; charset=utf-8")
        self.send_header("Content-Disposition", "inline; filename=move-car.ics")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def sign_body(self, body):
        try:
            images = signread.decode_images(body.get("images"))
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        city = []
        if body.get("lat") is not None and body.get("lng") is not None:
            try:
                lat, lng = float(body["lat"]), float(body["lng"])
                city = find_spot(lat, lng, 40)["rules"]
            except (TypeError, ValueError):
                city = []
        self.reply(200, signread.read_sign(images, city))

    def detect_body(self, body):
        samples = body.get("samples")
        if not isinstance(samples, list) or len(samples) > 80:
            return self.reply(400, {"error": "send a short list of samples"})
        clean = []
        for item in samples:
            if not isinstance(item, dict):
                continue
            try:
                speed = item.get("speed")
                clean.append({"t": float(item["t"]), "lat": float(item["lat"]), "lng": float(item["lng"]),
                              "speed": None if speed is None else float(speed)})
            except (KeyError, TypeError, ValueError):
                continue
        return self.reply(200, {"prompt": parkdetect.should_prompt(clean)})

    def report_body(self, body):
        try:
            lat, lng = float(body["lat"]), float(body["lng"])
        except (KeyError, TypeError, ValueError):
            return self.reply(400, {"error": "give lat and lng"})
        try:
            out = alarms.create_report(body.get("token") or "", lat, lng, body.get("kind") or "",
                                       body.get("note") or "", self.when(body.get("at")), body.get("hours"))
        except PermissionError:
            return self.reply(401, {"error": "Open the app once so this phone has a token, then report again."})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        return self.reply(200, out)

    def confirm_body(self, report_id, body):
        try:
            out = alarms.confirm_report(body.get("token") or "", report_id, self.when(body.get("at")))
        except PermissionError:
            return self.reply(401, {"error": "Open the app once so this phone has a token."})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        return self.reply(200, out) if out else self.reply(404, {"error": "That report has expired."})

    def paid_body(self, park_id, body):
        raw = body.get("paid_until")
        if not raw:
            return self.reply(400, {"error": "give paid_until"})
        try:
            when = datetime.fromisoformat(str(raw))
        except ValueError:
            return self.reply(400, {"error": "paid_until must be a time"})
        if when.tzinfo is None:
            when = when.replace(tzinfo=TZ)
        else:
            when = when.astimezone(TZ)
        out = alarms.reschedule_paid(park_id, when, self.when(body.get("at")))
        if out is None:
            return self.reply(404, {"error": "That parked session is gone."})
        return self.reply(200 if out.get("ok") else 409, out)

    def read_json(self, limit=100_000):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit:
            raise ValueError("request is too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            raise ValueError("Send JSON")
        if not isinstance(body, dict):
            raise ValueError("Send a JSON object")
        return body

    def send_file(self, rel, ctype, cache):
        try:
            data = open(os.path.join(ROOT, rel), "rb").read()
        except OSError:
            return self.reply(404, {"error": rel + " is missing"})
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if rel.endswith(".js"):
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(data)

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

def main():
    threading.Thread(target=alarms.scheduler_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    print(f"listening on {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()

if __name__ == "__main__":
    main()
