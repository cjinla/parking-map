"""GET /check?lat=34.10&lng=-118.33[&at=2026-10-23T14:00]  ->  JSON answer.
Run:  python server.py   (port 8080; stdlib only)"""
import json, math, os, re, sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo
from rules import Rule, move_by, current, to_dict
from ingest import DB, DAYS

D3 = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
COVERAGE = "Covers meter time limits and permit districts. Street sweeping is only a rough, unverified hint on major streets. Tow-away zones and curb colors are not included, so check the signs."
DISCLAIMER = "Posted signs always override this app. Data may be out of date."

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
        if c: return True
    return False

FULL = r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
ABBR = r"^(mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)(?:day|sday|nesday|rsday|urday)?$"

def sweep_day(props):
    """Prefer fields named like 'day'; otherwise only accept full day names (avoids 'Sunset', 'Sun Valley')."""
    for k, val in props.items():
        if "day" in k.lower() or "dow" in k.lower():
            m = re.match(ABBR, str(val).strip().lower())
            if m: return [d for d in range(7) if DAYS[d][:3] == m[1][:3]][0]
    m = re.search(FULL, " ".join(str(x) for x in props.values()).lower())
    return DAYS.index(m[1][:3]) if m else None

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
            if math.hypot(px - ax - t * dx, py - ay - t * dy) <= meters: return True
    return False

def sweep_rule(props):
    day = sweep_day(props)
    if day is None: return None
    pick = {k: v for k, v in props.items() if re.search(r"time|hour|start|end", k.lower())} or props
    text = " ".join(str(x) for x in pick.values()).lower()
    def m(h, mi, ap): return (int(h) % 12 + (12 if ap == "pm" else 0)) * 60 + int(mi or 0)
    t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\D+?(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?", text)
    if t: a, b = m(t[1], t[2], t[3] + "m"), m(t[4], t[5], t[6] + "m")
    else:
        h = re.findall(r"\b([01]?\d|2[0-3]):([0-5]\d)", text)
        if len(h) < 2: return None
        a, b = int(h[0][0]) * 60 + int(h[0][1]), int(h[1][0]) * 60 + int(h[1][1])
    return Rule("no", [day], a, b, "Street sweeping (check sign)", source="Posted sweeping routes", confidence="low")

def lookup(lat, lng, now):
    db = sqlite3.connect(DB)
    box = 0.0006  # about 60 m
    sweep_warn = []
    rules, address, conf = [], None, "low"
    rows = db.execute("SELECT lat,lng,address,rules FROM meters WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                      (lat - box, lat + box, lng - box, lng + box)).fetchall()
    if rows:
        best = min(rows, key=lambda r: dist(lat, lng, r[0], r[1]))
        if dist(lat, lng, best[0], best[1]) <= 40:
            rules += [Rule(**r) for r in json.loads(best[3])]
            address, conf = best[2], "high"
    for props, geom in db.execute("SELECT props,geom FROM sweep WHERE ? BETWEEN minlat AND maxlat AND ? BETWEEN minlng AND maxlng", (lat, lng)):
        g = json.loads(geom)
        if (inside(lng, lat, g) if g["type"] in ("Polygon", "MultiPolygon") else near(lng, lat, g)):
            p = json.loads(props)
            r = sweep_rule(p)
            if r: rules.append(r)
            elif sweep_day(p) is not None:
                sweep_warn.append("Unverified: this major street may be swept on " + D3[sweep_day(p)] + "s (times unknown). Check the signs for any sweeping restriction.")
    warnings = list(sweep_warn)
    try:
        for props, geom in db.execute("SELECT props,geom FROM ppd WHERE ? BETWEEN minlat AND maxlat AND ? BETWEEN minlng AND maxlng", (lat, lng)):
            if inside(lng, lat, json.loads(geom)):
                warnings.append("This area is in a preferential parking district. You may need a permit; check the sign for hours.")
                break
    except sqlite3.OperationalError:
        pass                   # database built before permit districts were added
    return rules, address, conf, warnings

class H(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            page = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            try:
                data = open(page, "rb").read()
            except OSError:
                return self.reply(404, {"error": "index.html is missing from the repository"})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            return self.wfile.write(data)
        if u.path != "/check": return self.reply(404, {"error": "use /check?lat=&lng="})
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            lat, lng = float(q["lat"]), float(q["lng"])
        except (KeyError, ValueError):
            return self.reply(400, {"error": "lat and lng are required numbers"})
        tz = ZoneInfo("America/Los_Angeles")
        now = datetime.fromisoformat(q["at"]).replace(tzinfo=tz) if "at" in q else datetime.now(tz)
        rules, address, conf, warnings = lookup(lat, lng, now)
        if not rules:
            return self.reply(200, {"status": "unknown", "headline": "No rules found for this spot",
                                    "confidence": "none", "warnings": warnings, "coverage": COVERAGE, "disclaimer": DISCLAIMER})
        out = move_by(rules, now)
        if out.get("hours_known") is False: conf = "low"   # limit known, enforced hours not
        out.update(address=address, confidence=conf, warnings=warnings, coverage=COVERAGE, rules=[to_dict(r) for r in rules], disclaimer=DISCLAIMER)
        self.reply(200, out)

    def reply(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), H).serve_forever()
