"""Rule model and evaluator. Weekdays use Python's convention: Monday=0.

Holiday policy follows LADOT's published exemption list
(https://ladotparking.org/parking-meters/parking-restrictions-holiday-parking/):
meters, time limits, and day-and-time no-parking / no-stopping rules are not
enforced on the listed city holidays, including the observed weekday when the
holiday falls on a weekend. Street sweeping is also off on Cesar Chavez Day and
the day after Thanksgiving. Cesar Chavez Day is NOT treated as a meter holiday:
sources disagree, and skipping a real time limit would cause a ticket.
Red curbs and anytime bans are never suspended. A sign that says
"Holidays Enforced" is entered by the driver (skip_holidays stays false).
"""
import re
from dataclasses import dataclass, asdict, fields
from datetime import date, datetime, timedelta

RANK = {"no": 3, "permit": 2, "limit": 1}
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_TOKENS = (
    ("thursday", 3), ("thur", 3), ("thu", 3), ("th", 3),
    ("tuesday", 1), ("tues", 1), ("tue", 1), ("tu", 1),
    ("saturday", 5), ("sat", 5), ("sa", 5),
    ("sunday", 6), ("sun", 6), ("su", 6),
    ("monday", 0), ("mon", 0),
    ("wednesday", 2), ("wed", 2),
    ("friday", 4), ("fri", 4),
    ("m", 0), ("w", 2), ("f", 4),
)


@dataclass
class Rule:
    kind: str                 # "no" | "permit" | "limit"
    days: list                # weekdays, Monday=0
    start: int                # minutes after midnight
    end: int                  # may be < start for overnight rules; 1440 means midnight
    label: str
    limit_min: int = None     # for kind == "limit"
    source: str = ""
    confidence: str = "high"  # "high" (sign-level data) | "low" (area-level data)
    hours_known: bool = True  # False when the city data gives a limit but not the enforced hours
    skip_holidays: bool = False
    also_sweep_holidays: bool = False  # also skip Cesar Chavez and the day after Thanksgiving
    rate: str = ""
    rate_type: str = ""       # FLAT | TOD | JUMP | ""
    topic: str = ""           # meter | sweep | tow | permit | curb | ""


def nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    return d + timedelta(days=(weekday - d.weekday()) % 7 + 7 * (n - 1))


def last_weekday(year, month, weekday):
    d = date(year + 1, 1, 1) - timedelta(days=1) if month == 12 else date(year, month + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def observed_date(d):
    """Saturday holidays are also observed Friday; Sunday holidays also Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return None


def _meter_holidays(year):
    """Days when LADOT suspends meters, time limits, and timed no-parking rules."""
    return {
        date(year, 1, 1): "New Year's Day",
        nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        nth_weekday(year, 2, 0, 3): "Presidents Day",
        last_weekday(year, 5, 0): "Memorial Day",
        date(year, 6, 19): "Juneteenth",
        date(year, 7, 4): "Independence Day",
        nth_weekday(year, 9, 0, 1): "Labor Day",
        nth_weekday(year, 10, 0, 2): "Columbus Day",
        date(year, 11, 11): "Veterans Day",
        nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        date(year, 12, 25): "Christmas Day",
    }


def _named(actual):
    out = dict(actual)
    for day, name in actual.items():
        obs = observed_date(day)
        if obs and obs not in out:
            out[obs] = name + " (observed)"
    return out


def holiday_name(d):
    """City holiday that suspends meters and timed restrictions, or None."""
    for year in (d.year, d.year + 1, d.year - 1):
        hit = _named(_meter_holidays(year)).get(d)
        if hit:
            return hit
    return None


def sweeping_holiday_name(d):
    """Holiday that suspends street sweeping, including sweeping-only days."""
    full = holiday_name(d)
    if full:
        return full
    for year in (d.year, d.year + 1, d.year - 1):
        hit = _named({date(year, 3, 31): "Cesar Chavez Day"}).get(d)
        if hit:
            return hit
    for year in (d.year, d.year - 1):
        if d == nth_weekday(year, 11, 3, 4) + timedelta(days=1):
            return "Day after Thanksgiving"
    return None


def holiday_calendar(year):
    """Every city holiday in `year`: meters off, or sweeping only (Cesar Chavez, day after Thanksgiving)."""
    out = []
    d = date(year, 1, 1)
    while d.year == year:
        meters = holiday_name(d)
        sweep = sweeping_holiday_name(d)
        if meters or sweep:
            out.append({"date": d.isoformat(), "name": meters or sweep,
                        "meters_off": bool(meters), "sweeping_off": bool(sweep)})
        d += timedelta(days=1)
    return out


def permit_covers(district, saved):
    """True when a saved permit name or number matches this district."""
    if not district or not saved:
        return False
    blob = str(district).lower()
    for item in saved:
        key = str(item).strip().lower()
        if len(key) >= 2 and (key in blob or blob in key):
            return True
    return False


def holiday_note(d):
    full = holiday_name(d)
    if full:
        return (full + ": LADOT does not enforce meters, time limits, or no-parking signs that "
                "name specific days and hours, unless the sign says Holidays Enforced. "
                "Red curbs still apply. Check again after the holiday; a time limit can start then.")
    extra = sweeping_holiday_name(d)
    if not extra:
        return None
    if extra.startswith("Cesar Chavez"):
        return (extra + ": street sweeping is not enforced. This app still counts meters and "
                "time limits today, because sources disagree and a missed meter is a ticket. "
                "Look at the meter screen.")
    return extra + ": street sweeping is not enforced. Meters and other restrictions still count."


def _day_index(word):
    word = word.strip(" .").lower()
    for name, idx in _DAY_TOKENS:
        if word == name:
            return idx
    return None


def parse_days(text):
    """'Th,F', 'Tu', 'weekdays', 'Wednesday' -> weekday numbers. Unknown text -> []."""
    if not text:
        return []
    t = str(text).strip().lower()
    if t in ("daily", "every day", "everyday", "all week", "7 days"):
        return list(range(7))
    if t in ("weekdays", "weekday", "m-f", "mon-fri"):
        return list(range(5))
    if t in ("weekends", "weekend"):
        return [5, 6]
    span = re.fullmatch(r"([a-z]+)\s+(?:to|through|-)\s+([a-z]+)", t)
    if span:
        a, b = _day_index(span[1]), _day_index(span[2])
        if a is not None and b is not None:
            if a <= b:
                return list(range(a, b + 1))
            return list(range(a, 7)) + list(range(0, b + 1))
    out = []
    for part in re.split(r"[,&/]|\band\b|\s+", t):
        part = part.strip(" .")
        if not part:
            continue
        for name, idx in _DAY_TOKENS:
            if part == name and idx not in out:
                out.append(idx)
                break
    return out


def active(r, dt, ignore_holidays=False):
    if not ignore_holidays:
        if r.skip_holidays and holiday_name(dt.date()):
            return False
        if r.also_sweep_holidays and sweeping_holiday_name(dt.date()):
            return False
    m, d = dt.hour * 60 + dt.minute, dt.weekday()
    if r.start < r.end:
        return d in r.days and r.start <= m < r.end
    return (d in r.days and m >= r.start) or ((d - 1) % 7 in r.days and m < r.end)


def current(rules, dt, ignore_holidays=False):
    hits = [r for r in rules if active(r, dt, ignore_holidays=ignore_holidays)]
    return max(hits, key=lambda r: RANK[r.kind]) if hits else None


def rule_topic(r):
    if r.topic:
        return r.topic
    if r.kind == "limit":
        return "meter"
    if r.kind == "permit":
        return "permit"
    blob = (r.source + " " + r.label).lower()
    if "sweep" in blob:
        return "sweep"
    if "tow" in blob or "rush" in blob:
        return "tow"
    if "red" in blob or "curb" in blob:
        return "curb"
    return ""


def move_by(rules, dt):
    """What the driver needs to know after parking at `dt`."""
    cur = current(rules, dt)
    if cur and cur.kind == "no":
        return {"status": "no", "headline": "Do not park here", "reason": cur.label}
    if cur and cur.kind == "permit":
        return {"status": "permit", "headline": "Permit holders only", "reason": cur.label}
    deadline, why = None, None
    if cur and cur.limit_min:
        deadline, why = dt + timedelta(minutes=cur.limit_min), f"{cur.label} ends"
    t = dt.replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60):          # scan one week, minute by minute
        t += timedelta(minutes=1)
        if cur and cur.limit_min and not active(cur, t):
            deadline, why, cur = None, None, None   # meter hours ended: limit no longer applies
        if deadline and t >= deadline:
            break
        c = current(rules, t)
        if c and c.kind in ("no", "permit"):
            deadline, why = t, f"{c.label} starts"
            break
    if deadline is None:
        return {"status": "ok", "headline": "No restrictions this week", "move_by": None}
    headline = "You can park, but move your car by " + deadline.strftime("%a %I:%M %p").replace(" 0", " ")
    out = {"status": "limit" if cur else "ok", "headline": headline,
           "move_by": deadline.isoformat(), "reason": why}
    if cur and cur.limit_min and not cur.hours_known and why and why.endswith("ends"):
        out["headline"] = ("If the meter is in operation, move your car by "
                           + deadline.strftime("%a %I:%M %p").replace(" 0", " ") + ". Check the sign for hours.")
        out["hours_known"] = False
    return out


def free_at(rules, dt):
    """When a no-parking or permit restriction in effect at dt lifts. None if it lasts all week."""
    cur = current(rules, dt)
    if not cur or cur.kind not in ("no", "permit"):
        return None
    t = dt.replace(second=0, microsecond=0)
    for _ in range(7 * 24 * 60):
        t += timedelta(minutes=1)
        c = current(rules, t)
        if not c or c.kind not in ("no", "permit"):
            return t
    return None


def _boundaries(rules, weekday):
    pts = {0, 1440}
    prev = (weekday - 1) % 7
    for r in rules:
        if weekday in r.days:
            if 0 <= r.start < 1440:
                pts.add(r.start)
            if r.start < r.end:
                pts.add(min(r.end, 1440))
            else:
                pts.add(1440)
        if r.start > r.end and prev in r.days and 0 < r.end < 1440:
            pts.add(r.end)
    return sorted(p for p in pts if 0 <= p <= 1440)


def windows(rules, dt, days=4):
    """Allowed and blocked spans for today and the following days."""
    out = []
    for i in range(days):
        d = dt.date() + timedelta(days=i)
        pts = _boundaries(rules, d.weekday())
        hname = holiday_name(d) or sweeping_holiday_name(d)
        segs = []
        for a, b in zip(pts, pts[1:]):
            if b <= a:
                continue
            moment = datetime(d.year, d.month, d.day, a // 60, a % 60, tzinfo=dt.tzinfo)
            if not rules:
                status, label = "unknown", "No city rule for this curb"
            else:
                cur = current(rules, moment)
                if cur:
                    status = cur.kind
                    label = cur.label + (" (hours not in the city data)" if cur.kind == "limit" and not cur.hours_known else "")
                else:
                    raw = current(rules, moment, ignore_holidays=True)
                    if raw and (holiday_name(d) or sweeping_holiday_name(d)):
                        status, label = "ok", "Not enforced today (" + (holiday_name(d) or sweeping_holiday_name(d)) + ")"
                    else:
                        status, label = "ok", "No restriction in the data"
            if segs and segs[-1]["status"] == status and segs[-1]["label"] == label:
                segs[-1]["end_min"] = b
            else:
                segs.append({"start_min": a, "end_min": b, "status": status, "label": label})
        out.append({
            "date": d.isoformat(),
            "weekday": WEEKDAYS[d.weekday()],
            "holiday": holiday_name(d),
            "sweep_holiday": sweeping_holiday_name(d),
            "segments": segs,
        })
    return out


def clock_label(dt):
    h = dt.hour % 12 or 12
    ap = "AM" if dt.hour < 12 else "PM"
    return f"{WEEKDAYS[dt.weekday()][:3]} {h}:{dt.minute:02d} {ap}"


def limit_phrase(minutes):
    if not minutes:
        return None
    if minutes % 60 == 0:
        n = minutes // 60
        return "1 hour" if n == 1 else f"{n} hours"
    return f"{minutes} minutes"


def _rate_text(rate, rate_type):
    if not rate:
        return None
    if (rate_type or "").upper() == "FLAT" and not re.search(r"/h", rate, re.I):
        return rate + "/hour"
    if (rate_type or "").upper() == "TOD":
        return rate + " (changes by time of day)"
    return rate


def gaps(rules):
    topics = {rule_topic(r) for r in rules}
    limits = [r for r in rules if rule_topic(r) == "meter" or r.kind == "limit"]
    out = []
    if not limits:
        out.append({"id": "meter", "text": "No parking meter from the LADOT inventory is close enough to this spot."})
    elif any(not r.hours_known for r in limits):
        out.append({"id": "meter_hours", "text": "The time limit is known. The hours it is enforced are not in the city feed."})
    if "sweep" not in topics:
        out.append({"id": "sweep", "text": "No street-sweeping hours for this curb. Residential routes are often missing from the map."})
    if "tow" not in topics:
        out.append({"id": "tow", "text": "Tow-away and rush-hour rules are not in the open data."})
    if "curb" not in topics:
        out.append({"id": "curb", "text": "Red curbs are not in the open data. If the curb is painted red, do not park."})
    return out


def answer(rules, dt, days=4):
    """Driver-facing answer: can I park, until when, and the next few days."""
    if not rules:
        base = {"status": "unknown", "headline": "No rules found for this spot", "move_by": None, "reason": None}
    else:
        base = dict(move_by(rules, dt))
        base.setdefault("move_by", None)
        base.setdefault("reason", None)
    status = base["status"]
    cur = current(rules, dt) if rules else None
    until = free_at(rules, dt) if rules else None
    if status in ("no", "permit"):
        word, can = ("Permit", False) if status == "permit" else ("No", False)
        move_text = "Now"
    elif status == "unknown":
        word, can, move_text = "Unknown", None, "Not in the data"
    else:
        word, can = "Yes", True
        move_text = clock_label(datetime.fromisoformat(base["move_by"])) if base.get("move_by") else "Not in the data"
    limits = [r for r in rules if r.kind == "limit"]
    if cur and cur.kind == "limit" and cur.limit_min:
        phrase = limit_phrase(cur.limit_min)
        time_limit = phrase if cur.hours_known else phrase + " (enforced hours are not in the city data)"
        limit_min = cur.limit_min
    elif limits and limits[0].limit_min:
        extra = " when the meter is running" if limits[0].hours_known else " (enforced hours are not in the city data)"
        time_limit = limit_phrase(limits[0].limit_min) + extra
        limit_min = limits[0].limit_min
    else:
        time_limit, limit_min = "Not in the city data", None
    if not limits:
        meter_line, rate, rate_type = "No meter nearby", None, None
    else:
        rate = next((r.rate for r in limits if r.rate), "") or None
        rate_type = next((r.rate_type for r in limits if r.rate_type), "") or None
        shown = _rate_text(rate or "", rate_type or "")
        meter_line = "Yes — " + shown if shown else "Yes — price not in the data"
    base.update({
        "can_park": can,
        "answer_word": word,
        "until": until.isoformat() if until else None,
        "restriction_until_text": clock_label(until) if until else None,
        "move_by_text": move_text,
        "time_limit_min": limit_min,
        "facts": {"time_limit": time_limit, "meter": meter_line, "move_by": move_text},
        "meter": {
            "present": bool(limits),
            "rate": rate,
            "rate_type": rate_type,
            "hours_known": (all(r.hours_known for r in limits) if limits else None),
            "enforced_now": bool(cur and cur.kind == "limit"),
        },
        "timeline": windows(rules, dt, days),
        "holiday": holiday_name(dt.date()),
        "sweep_holiday": sweeping_holiday_name(dt.date()),
        "holiday_note": holiday_note(dt.date()),
        "gaps": gaps(rules),
    })
    return base


def _minutes(value, name):
    if value is None or value == "":
        return None
    try:
        m = int(value)
    except (TypeError, ValueError):
        raise ValueError(name + " must be minutes after midnight")
    if not 0 <= m <= 1440:
        raise ValueError(name + " is out of range")
    return m


def _day_list(value):
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise ValueError("pick at least one day")
    out = []
    for d in value:
        try:
            d = int(d)
        except (TypeError, ValueError):
            raise ValueError("bad day")
        if not 0 <= d <= 6:
            raise ValueError("bad day")
        if d not in out:
            out.append(d)
    return out


def rules_from_sign(sign):
    """Rules the driver typed from the posted sign. Empty or invalid fields are skipped."""
    if not sign:
        return []
    if not isinstance(sign, dict):
        raise ValueError("sign must be an object")
    skip = not bool(sign.get("holidays_enforced"))
    rules = []
    lim = sign.get("time_limit_min")
    if lim not in (None, "", 0, "0"):
        try:
            lim = int(lim)
        except (TypeError, ValueError):
            raise ValueError("time limit must be minutes")
        if not 1 <= lim <= 24 * 60:
            raise ValueError("time limit is out of range")
        start, end = _minutes(sign.get("limit_start"), "start"), _minutes(sign.get("limit_end"), "end")
        known = start is not None and end is not None
        if not known:
            start, end = 0, 1440
        days = _day_list(sign.get("limit_days")) or list(range(7))
        label = f"{lim // 60}-hour limit" if lim % 60 == 0 else f"{lim}-minute limit"
        rate = str(sign.get("rate") or "").strip()[:40]
        rules.append(Rule(
            "limit", days, start, end, label, limit_min=lim, source="Sign you entered",
            confidence="high", hours_known=known, skip_holidays=skip, rate=rate,
            rate_type="FLAT" if rate else "", topic="meter"))
    def add_windows(key, kind, label, topic, sweep=False):
        rows = sign.get(key) or []
        if not isinstance(rows, list):
            raise ValueError(key + " must be a list")
        if len(rows) > 8:
            raise ValueError("too many " + key + " windows")
        for w in rows:
            if not w:
                continue
            days = _day_list(w.get("days"))
            start, end = _minutes(w.get("start"), "start"), _minutes(w.get("end"), "end")
            if not days or start is None or end is None or start == end:
                continue
            rules.append(Rule(
                kind, days, start, end, label, source="Sign you entered", confidence="high",
                skip_holidays=skip, also_sweep_holidays=bool(sweep and skip), topic=topic))
    add_windows("sweeping", "no", "Street sweeping (from the sign)", "sweep", True)
    add_windows("tow", "no", "Tow-away / no stopping (from the sign)", "tow", False)
    add_windows("permit", "permit", "Permit holders only (from the sign)", "permit", False)
    if sign.get("red_curb"):
        rules.append(Rule(
            "no", list(range(7)), 0, 1440, "Red curb / no parking anytime (from the sign)",
            source="Sign you entered", confidence="high", skip_holidays=False, topic="curb"))
    return rules


def sign_replaces(sign):
    if not sign:
        return set()
    topics = set()
    if sign.get("time_limit_min") not in (None, ""):
        topics.add("meter")
    if sign.get("sweeping"):
        topics.add("sweep")
    if sign.get("tow"):
        topics.add("tow")
    if sign.get("permit"):
        topics.add("permit")
    if sign.get("red_curb"):
        topics.add("curb")
    return topics


def apply_sign(rules, sign):
    extra = rules_from_sign(sign)
    drop = sign_replaces(sign)
    return [r for r in rules if rule_topic(r) not in drop] + extra


def rule_from_json(obj):
    names = {f.name for f in fields(Rule)}
    return Rule(**{k: v for k, v in obj.items() if k in names})


def to_dict(r):
    return asdict(r)
