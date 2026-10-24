"""GET /check?lat=34.10&lng=-118.33[&at=2026-10-23T14:00]  ->  JSON answer.
Run:  python server.py   (port 8080; stdlib only)"""
import json, math, os, re, sqlite3
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
from zoneinfo import ZoneInfo
from rules import Rule, move_by, current, to_dict
from ingest import DB, DAYS

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

def sweep_rule(props):
    text = " ".join(str(v) for v in props.values()).lower()
    day = next((i for i, d in enumerate(DAYS) if re.search(r"\b" + d, text)), None)
    t = re.search(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\D+(\d{1,2})(?::(\d{2}))?\s*([ap]m)", text)
    if day is None or not t: return None
    def m(h, mi, ap): return (int(h) % 12 + (12 if ap == "pm" else 0)) * 60 + int(mi or 0)
    return Rule("no", [day], m(t[1], t[2], t[3]), m(t[4], t[5], t[6]), "Street sweeping (check sign)",
                source="StreetsLA posted routes", confidence="low")

def lookup(lat, lng, now):
    db = sqlite3.connect(DB)
    box = 0.0006  # about 60 m
    rules, address, conf = [], None, "low"
    rows = db.execute("SELECT lat,lng,address,rules FROM meters WHERE lat BETWEEN ? AND ? AND lng BETWEEN ? AND ?",
                      (lat - box, lat + box, lng - box, lng + box)).fetchall()
    if rows:
        best = min(rows, key=lambda r: dist(lat, lng, r[0], r[1]))
        if dist(lat, lng, best[0], best[1]) <= 40:
            rules += [Rule(**r) for r in json.loads(best[3])]
            address, conf = best[2], "high"
    for props, geom in db.execute("SELECT props,geom FROM sweep WHERE ? BETWEEN minlat AND maxlat AND ? BETWEEN minlng AND maxlng", (lat, lng)):
        if inside(lng, lat, json.loads(geom)):
            r = sweep_rule(json.loads(props))
            if r: rules.append(r)
    warnings = []
    try:
        for props, geom in db.execute("SELECT props,geom FROM ppd WHERE ? BETWEEN minlat AND maxlat AND ? BETWEEN minlng AND maxlng", (lat, lng)):
            if inside(lng, lat, json.loads(geom)):
                warnings.append("This area is in a preferential parking district. You may need a permit; check the sign for hours.")
                break
    except sqlite3.OperationalError:
        pass                   # database built before permit districts were added
    return rules, address, conf, warnings

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
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
                                    "confidence": "none", "warnings": warnings, "disclaimer": DISCLAIMER})
        out = move_by(rules, now)
        out.update(address=address, confidence=conf, warnings=warnings, rules=[to_dict(r) for r in rules], disclaimer=DISCLAIMER)
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
