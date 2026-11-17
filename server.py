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
from ingest import DB as DATA_DB
from rules import Rule, WEEKDAYS, answer, apply_sign, clock_label, parse_days, rule_from_json, to_dict

DB = os.environ.get("PARKING_DB", DATA_DB)
TZ = ZoneInfo("America/Los_Angeles")
ROOT = os.path.dirname(os.path.abspath(__file__))
COVERAGE = ("Uses LADOT meter locations, rates, and time limits; major-street sweeping areas; "
            "posted sweeping hours when a route id matches; and permit-district boundaries. "
            "Meter hours, tow-away zones, and red curbs are not in those feeds.")
DISCLAIMER = "Posted signs always override this app. Data may be out of date."
SOURCES = [
    {"name": "LADOT metered parking inventory and policies", "url": "https://data.lacity.org/Transportation/LADOT-Metered-Parking-Inventory-Policies/s49e-q6j2"},
    {"name": "Posted street sweeping routes", "url": "https://data.lacity.org/Transportation/Posted-Street-Sweeping-Routes/krk7-ayq2"},
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

def sweep_from_props(props):
    """Return (rules, warning). Hours are attached only when the data actually has them."""
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
        box = 150 * 1.5e-5
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
            elif distance_m <= 150:
                warnings.append(f"The nearest meter is {distance_m} m away ({best[2] or 'unknown block'}), so its rules were not applied. It may be the next block.")
        seen = set()
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
            if warn and warn not in warnings and len([w for w in warnings if "swept" in w or "Sweeping" in w]) < 3:
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

def assemble(lat, lng, now, sign=None, has_permit=False, radius=40, searched=None):
    spot = find_spot(lat, lng, radius)
    rules = [r for r in spot["rules"] if not (has_permit and r.kind == "permit")]
    if sign:
        rules = apply_sign(rules, sign)
    out = answer(rules, now)
    if spot["meter_used"] and spot["distance_m"] and spot["distance_m"] > 25:
        spot["warnings"] = list(spot["warnings"]) + [
            f"This meter is {spot['distance_m']} m from the GPS point. Confirm the space number on the meter."]
    note = out.get("holiday_note")
    warnings = list(spot["warnings"])
    if note and note not in warnings:
        warnings.insert(0, note)
    if not rules and not warnings:
        warnings.append("Nothing in the city data matches this curb. Read the sign, or the block may be outside Los Angeles.")
    out.update(
        searched=searched, address=spot["address"], segment=spot["segment"], space_id=spot["space_id"],
        distance_m=spot["distance_m"] if spot["meter_used"] else None,
        nearest_meter_m=spot["distance_m"], meter_used=spot["meter_used"],
        permit_district=spot["permit_district"], confidence=spot["confidence"] if not sign else "high",
        warnings=warnings, coverage=COVERAGE, rules=[to_dict(r) for r in rules],
        disclaimer=DISCLAIMER, sources=SOURCES, lat=lat, lng=lng,
        checked_at=now.isoformat())
    if out.get("hours_known") is False:
        out["confidence"] = "low"
    return out

def _alarm_at(result, now):
    if result["status"] in ("no", "permit"):
        return now
    if result.get("move_by"):
        return datetime.fromisoformat(result["move_by"])
    return None

def park(lat, lng, now, sign=None, has_permit=False, radius=40, sub_id=None, searched=None, device_token=None, settings=None):
    result = assemble(lat, lng, now, sign, has_permit, radius, searched)
    when = _alarm_at(result, now)
    saved = alarms.save_park(lat, lng, result.get("address") or result.get("segment"), when,
                             result.get("reason"), result.get("headline"), sub_id, now, device_token, settings)
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
        if path in FILES:
            rel, ctype = FILES[path]
            return self.send_file(rel, ctype, "no-cache" if rel.endswith((".html", ".js", ".webmanifest")) else "public, max-age=86400")
        if path in ("/autopark",):
            return self.send_file("index.html", "text/html; charset=utf-8", "no-cache")
        if path == "/push/public-key":
            return self.reply(200, {"publicKey": os.environ.get("VAPID_PUBLIC_KEY", ""), "configured": alarms.push_configured()})
        if path == "/api/device/latest":
            return self.latest_park()
        ics = re.fullmatch(r"/park/([a-f0-9]{32})\.ics", path)
        if ics:
            return self.calendar(ics.group(1))
        one = re.fullmatch(r"/park/([a-f0-9]{32})", path)
        if one:
            row = alarms.get_park(one.group(1))
            return self.reply(200, row) if row else self.reply(404, {"error": "That parked session is gone. Park again."})
        if path != "/check":
            return self.reply(404, {"error": "use /check?lat=&lng="})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            lat, lng, searched, radius = self.locate(q)
        except LookupError as e:
            return self.reply(200, {"status": "unknown", "headline": str(e), "answer_word": "Unknown",
                                    "can_park": None, "confidence": "none", "warnings": [], "gaps": [],
                                    "coverage": COVERAGE, "disclaimer": DISCLAIMER, "sources": SOURCES})
        except ValueError as e:
            return self.reply(400, {"error": str(e)})
        now = self.when(q.get("at"))
        self.reply(200, assemble(lat, lng, now, has_permit=q.get("has_permit") == "1", radius=radius, searched=searched))

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = self.read_json()
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
                           radius=radius, searched=searched)
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
                       radius=radius, sub_id=sub_id, searched=searched, settings=body.get("settings"))
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

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 100_000:
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
