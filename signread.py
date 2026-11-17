"""Photograph a parking sign, transcribe it, and keep the photo with the session.

Vision uses OpenAI's chat completions API (a vision model such as gpt-4o-mini).
Set SIGN_VISION_API_KEY on the server. Optional: SIGN_VISION_MODEL (default
gpt-4o-mini) and SIGN_VISION_URL (default https://api.openai.com/v1/chat/completions).
Nothing is committed. With no key, the photo is still saved and the driver types the sign.
"""
import base64, json, os, uuid, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import alarms
from signparse import parse_sign_text, sign_conflicts

PHOTO_DIR = os.environ.get("PHOTO_DIR", "sign-photos")
TZ = ZoneInfo("America/Los_Angeles")
PROMPT = ("Transcribe every Los Angeles parking sign in this photo, top to bottom. "
          "Separate stacked signs with a blank line. Write arrows as LEFT ARROW, RIGHT ARROW, "
          "or BOTH ARROWS. Copy only words and numbers that are printed. Do not invent rules.")


def configured():
    return bool(os.environ.get("SIGN_VISION_API_KEY"))


def decode_images(items):
    """Data-URL or raw base64 strings -> [(mime, bytes)]."""
    if not items or not isinstance(items, list):
        raise ValueError("Send one or more photos")
    if len(items) > 4:
        raise ValueError("Send at most 4 photos")
    out = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("Each photo must be an image")
        mime = "image/jpeg"
        raw_b64 = item.strip()
        if raw_b64.startswith("data:"):
            header, raw_b64 = raw_b64.split(",", 1)
            mime = header[5:].split(";")[0] or mime
        if not mime.startswith("image/"):
            raise ValueError("Send an image")
        try:
            raw = base64.b64decode(raw_b64, validate=False)
        except Exception:
            raise ValueError("Could not read that photo")
        if not raw or len(raw) > 5_000_000:
            raise ValueError("Each photo must be under 5 MB")
        out.append((mime, raw))
    return out


def save_photos(images):
    """Write the bytes and remember them until a parking session claims the scan."""
    scan_id = uuid.uuid4().hex
    os.makedirs(PHOTO_DIR, exist_ok=True)
    conn = alarms.connect()
    for i, (mime, raw) in enumerate(images):
        ext = "png" if "png" in mime else "webp" if "webp" in mime else "jpg"
        path = os.path.join(PHOTO_DIR, f"{scan_id}-{i}.{ext}")
        with open(path, "wb") as f:
            f.write(raw)
        conn.execute("INSERT INTO photos (id, scan_id, park_id, path, created) VALUES (?,?,?,?,?)",
                     (uuid.uuid4().hex, scan_id, None, path, datetime.now(TZ).isoformat()))
    conn.commit()
    return scan_id


def attach_scan(scan_id, park_id):
    if not scan_id or not park_id:
        return 0
    conn = alarms.connect()
    cur = conn.execute("UPDATE photos SET park_id=? WHERE scan_id=? AND park_id IS NULL", (park_id, scan_id))
    conn.commit()
    return cur.rowcount


def transcribe(images):
    """Ask the vision model for the printed words. images is [(mime, bytes)]."""
    key = os.environ.get("SIGN_VISION_API_KEY")
    if not key:
        raise RuntimeError("SIGN_VISION_API_KEY is not set")
    url = os.environ.get("SIGN_VISION_URL") or "https://api.openai.com/v1/chat/completions"
    model = os.environ.get("SIGN_VISION_MODEL") or "gpt-4o-mini"
    content = [{"type": "text", "text": PROMPT}]
    for mime, raw in images:
        content.append({"type": "image_url", "image_url": {
            "url": "data:" + mime + ";base64," + base64.b64encode(raw).decode()}})
    payload = {"model": model, "temperature": 0,
               "messages": [{"role": "user", "content": content}], "max_tokens": 800}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as f:
        data = json.load(f)
    return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""


def read_sign(images, city_rules):
    """Save photos, transcribe when a key is set, and return rules to confirm."""
    scan_id = save_photos(images)
    base = {"scan_id": scan_id, "text": "", "sign": {}, "summary": [], "arrows": "", "conflicts": []}
    if not configured():
        base.update(available=False, message=(
            "Sign reading is not turned on for this server. The photo is saved with this spot. "
            "Type what the sign says."))
        return base
    try:
        text = transcribe(images).strip()
    except Exception as e:
        base.update(available=True, message=(
            "The photo is saved, but the sign reader did not answer. Type what the sign says."),
            error=str(e)[:180])
        return base
    parsed = parse_sign_text(text)
    summary = parsed.pop("summary", [])
    arrows = parsed.get("arrows") or ""
    base.update(available=True, text=text, sign=parsed, summary=summary, arrows=arrows,
                conflicts=sign_conflicts(city_rules, parsed), message="")
    return base
