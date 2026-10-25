"""Download Los Angeles open data and load it into a local SQLite file.
Run:  python ingest.py        (needs internet; re-run nightly)
Field names in the city's datasets change, so every parser below is tolerant
and anything it cannot read becomes a low-confidence rule instead of a crash."""
import json, re, sqlite3, urllib.error, urllib.request
DB = "parking.db"
METERS = "https://data.lacity.org/resource/s49e-q6j2.json?$limit=50000"
SWEEP = ("https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/ArcGIS/rest/services/"
         "Biweekly_Routes_2021/FeatureServer/0/query?where=1%3D1&outFields=*&outSR=4326&f=geojson")
PPD = "https://data.lacity.org/resource/s3st-6nwi.geojson?$limit=5000"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "la-parking-app/1.0", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url[:100]}: {e.read()[:200]!r}")

SWEEP_ALT = "https://data.lacity.org/resource/krk7-ayq2.geojson?$limit=5000"   # city open-data copy of posted routes
SWEEP_BASE = "https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/ArcGIS/rest/services/ADAPT_AMRoutes/FeatureServer"   # major-street sweeping routes (has a Day field, no times)

def esri_to_geo(f):
    """This service only speaks Esri JSON (rings), so convert it to a GeoJSON-style feature."""
    rings = (f.get("geometry") or {}).get("rings")
    geom = {"type": "MultiPolygon", "coordinates": [[r] for r in rings]} if rings else None
    return {"properties": f.get("attributes") or {}, "geometry": geom}

def sweep_features():
    """Try the first few layers and page through results. Never crashes the build."""
    for layer in (0, 1, 2):
        feats, offset, seen = [], 0, set()
        try:
            while True:
                data = get(f"{SWEEP_BASE}/{layer}/query?where=1%3D1&outFields=*&outSR=4326&f=json"
                           f"&resultOffset={offset}&resultRecordCount=1000")
                if "error" in data: raise RuntimeError(str(data["error"])[:200])
                page = [esri_to_geo(f) for f in data.get("features", [])]
                keys = {json.dumps(f.get("properties"), sort_keys=True) for f in page}
                if not page or keys <= seen: break          # server repeated itself: stop
                seen |= keys
                feats += page
                print(f"Sweeping layer {layer}: {len(feats)} routes so far", flush=True)
                if len(page) < 1000 or offset >= 40000: break
                offset += 1000
            if feats: return feats
        except Exception as e:
            print(f"Warning: sweeping layer {layer} failed: {e}")
    try:                       # fallback: the city\'s open-data copy of the posted routes
        feats = get(SWEEP_ALT).get("features", [])
        if feats:
            print(f"Sweeping: loaded {len(feats)} routes from the open-data portal copy", flush=True)
            return feats
    except Exception as e:
        print(f"Warning: sweeping fallback failed: {e}")
    print("Warning: no sweeping data loaded")
    return []

def latlng(rec):
    v = rec.get("latlng") or rec.get("location") or {}
    if isinstance(v, dict):
        lat = v.get("latitude") or (v.get("coordinates") or [None, None])[1]
        lng = v.get("longitude") or (v.get("coordinates") or [None, None])[0]
        if lat and lng: return float(lat), float(lng)
    m = re.findall(r"-?\d+\.\d+", str(v) if v else "")
    if len(m) >= 2: return float(m[0]), float(m[1])
    if rec.get("latitude") and rec.get("longitude"): return float(rec["latitude"]), float(rec["longitude"])

def mins(txt):
    m = re.match(r"\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", txt.lower())
    if not m: return None
    h = int(m[1]) % 12 + (12 if m[3] == "pm" else 0) if m[3] else int(m[1])
    return h * 60 + int(m[2] or 0)

def parse_limit(txt):
    """'2HR' -> 120. Ranges like '2HR-4HR' or '4HR-30MIN' vary by time of day, so use the smallest."""
    vals = [int(float(n) * (60 if u in ("hr", "hour") else 1))
            for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(hr|hour|min)", (txt or "").lower())]
    return min(vals) if vals else None

def parse_hours(txt):
    m = re.search(r"(\d{1,2}(?::\d{2})?\s*[ap]m?)\s*-\s*(\d{1,2}(?::\d{2})?\s*[ap]m?)", (txt or "").lower().replace("a ", "am ").replace("p ", "pm "))
    if m:
        a, b = mins(m[1].replace("a", "am").replace("amm", "am").replace("p", "pm").replace("pmm", "pm")), mins(m[2].replace("a", "am").replace("amm", "am").replace("p", "pm").replace("pmm", "pm"))
        if a is not None and b is not None: return a, b
    return 0, 1440

def meter_rules(rec):
    """The city's public feed has a limit but no enforced hours, so hours_known stays False
    unless a future version of the data adds them."""
    lim = parse_limit(rec.get("timelimit"))
    hrs = rec.get("operatinghours") or rec.get("operating_hours") or rec.get("meter_hours")
    a, b = parse_hours(hrs)
    known = bool(hrs) and (a, b) != (0, 1440)
    if not lim: return []
    label = f"{lim}-minute limit" if lim < 60 else f"{lim // 60}-hour limit"
    return [{"kind": "limit", "days": [0, 1, 2, 3, 4, 5, 6], "start": a, "end": b, "label": label,
             "limit_min": lim, "source": "LADOT meter inventory",
             "confidence": "high" if known else "low", "hours_known": known}]

def flat(c):
    """Yield every [lng, lat] pair in nested GeoJSON coordinates."""
    if c and isinstance(c[0], (int, float)):
        yield c
    else:
        for x in c: yield from flat(x)

def load():
    print("Downloading meters...", flush=True)
    db = sqlite3.connect(DB)
    db.executescript("""DROP TABLE IF EXISTS meters; DROP TABLE IF EXISTS sweep;
      CREATE TABLE meters(id TEXT, lat REAL, lng REAL, address TEXT, rules TEXT);
      CREATE INDEX m_ll ON meters(lat, lng);
      CREATE TABLE sweep(id INTEGER, props TEXT, geom TEXT, minlat REAL, maxlat REAL, minlng REAL, maxlng REAL);""")
    n = 0
    for rec in get(METERS):
        p = latlng(rec)
        if p:
            db.execute("INSERT INTO meters VALUES(?,?,?,?,?)",
                       (rec.get("spaceid"), p[0], p[1], rec.get("blockface") or "", json.dumps(meter_rules(rec))))
            n += 1
    s = 0
    types = {}
    for i, f in enumerate(sweep_features()):
        g = f.get("geometry") or {}
        types[g.get("type")] = types.get(g.get("type"), 0) + 1
        if i == 0: print("Sample sweeping properties:", json.dumps(f.get("properties"))[:400], flush=True)
        if g.get("type") not in ("Polygon", "MultiPolygon", "LineString", "MultiLineString"): continue
        pts = list(flat(g["coordinates"]))
        pad = 0.0004   # about 40 m, so streets (lines) still match nearby points
        db.execute("INSERT INTO sweep VALUES(?,?,?,?,?,?,?)",
                   (i, json.dumps(f["properties"]), json.dumps(g),
                    min(p[1] for p in pts) - pad, max(p[1] for p in pts) + pad,
                    min(p[0] for p in pts) - pad, max(p[0] for p in pts) + pad))
        s += 1
    print("Sweeping geometry types:", types, flush=True)
    db.execute("DROP TABLE IF EXISTS ppd")
    db.execute("CREATE TABLE ppd(props TEXT, geom TEXT, minlat REAL, maxlat REAL, minlng REAL, maxlng REAL)")
    p = 0
    try:                       # permit districts are optional: a failure here must not break the rest
        for f in get(PPD)["features"]:
            g = f.get("geometry") or {}
            if g.get("type") not in ("Polygon", "MultiPolygon"): continue
            rings = g["coordinates"] if g["type"] == "Polygon" else [r for poly in g["coordinates"] for r in poly]
            pts = [pt for ring in rings for pt in ring]
            db.execute("INSERT INTO ppd VALUES(?,?,?,?,?,?)", (json.dumps(f["properties"]), json.dumps(g),
                       min(q[1] for q in pts), max(q[1] for q in pts), min(q[0] for q in pts), max(q[0] for q in pts)))
            p += 1
    except Exception as e:
        print("Warning: permit districts not loaded:", e)
    db.commit()
    print(f"Loaded {n} meters, {s} sweeping routes and {p} permit districts into {DB}")

if __name__ == "__main__":
    load()
