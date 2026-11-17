"""Turn the words on a Los Angeles parking sign into the same rules the form uses.

The vision API only transcribes the sign. This parser is what the tests lock down,
so a photo is never required to check the reading.
"""
import re
from rules import rules_from_sign, rule_topic, sign_replaces

_DAY = (r"MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY|"
        r"MON|TUE|WED|THU|FRI|SAT|SUN|TH")
_DAY_INDEX = {
    "MONDAY": 0, "MON": 0, "TUESDAY": 1, "TUE": 1, "WEDNESDAY": 2, "WED": 2,
    "THURSDAY": 3, "THU": 3, "TH": 3, "FRIDAY": 4, "FRI": 4,
    "SATURDAY": 5, "SAT": 5, "SUNDAY": 6, "SUN": 6,
}
_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_TIME = r"(\d{1,2})(?::(\d{2}))?\s*([AP]M)?"
_RANGE = re.compile(_TIME + r"\s*(?:-|TO)\s*" + _TIME)
_SPAN = re.compile(r"\b(" + _DAY + r")\b\s*(?:-|TO|THRU|THROUGH)\s*\b(" + _DAY + r")\b")


def parse_sign_text(text):
    """Sample-sign text -> sign dict, plain-language summary, and arrow direction."""
    raw = _normalize(text or "")
    sign = {"sweeping": [], "tow": [], "permit": [], "red_curb": False,
            "holidays_enforced": "HOLIDAYS ENFORCED" in raw, "arrows": _arrows(raw), "summary": []}
    if not raw.strip():
        return sign
    for panel in _panels(raw):
        _read_panel(panel, sign)
    sign["summary"] = _summarize(sign)
    return sign


def sign_conflicts(city_rules, sign):
    """Where the sign and the city feed name the same kind of rule but disagree.
    The caller still applies the sign."""
    try:
        signed = rules_from_sign(sign)
    except ValueError:
        return []
    labels = {"meter": "the time limit", "sweep": "street sweeping", "tow": "tow-away",
              "permit": "the permit rule", "curb": "no parking anytime"}
    out = []
    for topic, name in labels.items():
        if topic not in sign_replaces(sign):
            continue
        city = [r for r in city_rules if rule_topic(r) == topic]
        new = [r for r in signed if rule_topic(r) == topic]
        if not city or not new or _sig(city) == _sig(new):
            continue
        out.append("The sign and the city data disagree about " + name + ". Using the sign: "
                   + _brief(new) + ". City data said: " + _brief(city) + ".")
    return out


def _normalize(text):
    text = text.upper().replace("–", "-").replace("—", "-").replace("−", "-")
    text = text.replace("A.M.", "AM").replace("P.M.", "PM").replace("A.M", "AM").replace("P.M", "PM")
    return text


def _arrows(text):
    left = bool(re.search(r"←|⇐|<--|<-|LEFT ARROW", text))
    right = bool(re.search(r"→|⇒|-->|->|RIGHT ARROW", text))
    both = "BOTH ARROW" in text or "<-->" in text or "<—>" in text or (left and right)
    if both:
        return "both"
    if left:
        return "left"
    if right:
        return "right"
    return ""


def _panel_type(text):
    if re.search(r"ANY\s*TIME", text) and re.search(r"NO PARKING|NO STOPPING", text) and not re.search(r"SWEEP|CLEAN", text):
        return "anytime"
    if re.search(r"SWEEP|STREET CLEANING", text):
        return "sweep"
    if re.search(r"\bTOW\b|TOW-AWAY|TOW AWAY", text):
        return "tow"
    if re.search(r"PERMIT", text) and re.search(r"ONLY|HOLDERS", text):
        return "permit"
    if re.search(r"METER|PAY AT|\d+\s*(HOUR|HR|MIN)", text):
        return "limit"
    return ""


def _panels(text):
    panels, buf, kind = [], [], ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            if buf:
                panels.append("\n".join(buf))
                buf, kind = [], ""
            continue
        line_kind = _panel_type(line)
        if buf and line_kind and kind and line_kind != kind:
            panels.append("\n".join(buf))
            buf, kind = [], ""
        buf.append(line)
        kind = _panel_type("\n".join(buf)) or kind
    if buf:
        panels.append("\n".join(buf))
    return panels


def _read_panel(panel, sign):
    kind = _panel_type(panel)
    ranges = _ranges(panel)
    days = _days(panel)
    if kind == "anytime":
        sign["red_curb"] = True
        return
    if kind == "sweep":
        for start, end in ranges:
            sign["sweeping"].append({"days": days, "start": start, "end": end})
        return
    if kind == "tow":
        for start, end in ranges:
            sign["tow"].append({"days": days, "start": start, "end": end})
        return
    if kind == "permit":
        for start, end in ranges or [(0, 1440)]:
            sign["permit"].append({"days": days, "start": start, "end": end})
        return
    if kind == "limit":
        limit = _limit_minutes(panel)
        if limit:
            sign["time_limit_min"] = limit
        if ranges:
            sign["limit_start"], sign["limit_end"] = ranges[0]
        if days != list(range(7)):
            sign["limit_days"] = days
        rate = _rate(panel)
        if rate:
            sign["rate"] = rate
        if re.search(r"METER|PAY AT", panel):
            sign["meter_sign"] = True
        if re.search(r"EXCEPT (BY|WITH) PERMIT", panel):
            sign["permit_exempts_limit"] = True


def _limit_minutes(text):
    m = re.search(r"\b(\d+)\s*(HOURS|HOUR|HR|MINUTES|MINUTE|MIN)\b", text)
    if not m:
        return None
    n = int(m.group(1))
    return n if m.group(2).startswith("MIN") else n * 60


def _rate(text):
    m = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    return ("$" + m.group(1)) if m else ""


def _clock(hour, minute, ap):
    hour = int(hour)
    minute = int(minute or 0)
    if ap:
        hour = hour % 12 + (12 if ap == "PM" else 0)
    return hour * 60 + minute


def _ranges(text):
    found = []
    for m in _RANGE.finditer(text):
        start_ap, end_ap = m.group(3), m.group(6)
        if start_ap and not end_ap:
            end_ap = start_ap
        elif end_ap and not start_ap:
            start_hour = int(m.group(1))
            end_hour = int(m.group(4)) % 12
            start_ap = "AM" if end_ap == "PM" and start_hour > end_hour else end_ap
        start, end = _clock(m.group(1), m.group(2), start_ap), _clock(m.group(4), m.group(5), end_ap)
        if start != end:
            found.append((start, end))
    return found


def _day_num(word):
    return _DAY_INDEX[word]


def _days(text):
    """Days the panel names. EXCEPT SUNDAY removes Sunday instead of selecting it."""
    m = re.search(r"\bEXCEPT\b(?!\s+(?:BY|WITH)\b)\s+(.+)", text)
    body = text[:m.start()] if m and not m.group(1).startswith("HOLIDAY") else text
    span = _SPAN.search(body)
    if re.search(r"\b(DAILY|EVERY DAY)\b", body):
        base = list(range(7))
    elif span:
        a, b = _day_num(span.group(1)), _day_num(span.group(2))
        base = list(range(a, b + 1)) if a <= b else list(range(a, 7)) + list(range(0, b + 1))
    else:
        seen = []
        for word in re.findall(r"\b(" + _DAY + r")\b", body):
            n = _day_num(word)
            if n not in seen:
                seen.append(n)
        base = seen or list(range(7))
    if m and not m.group(1).startswith("HOLIDAY"):
        for word in re.findall(r"\b(" + _DAY + r")\b", m.group(1)):
            n = _day_num(word)
            if n in base:
                base.remove(n)
    return base


def _fmt_time(m):
    h, mi = divmod(int(m), 60)
    ap = "AM" if h < 12 else "PM"
    h = h % 12 or 12
    return f"{h}:{mi:02d} {ap}"


def _fmt_days(days):
    days = list(days)
    if days == list(range(7)):
        return "every day"
    if days == list(range(5)):
        return "Monday–Friday"
    if days == list(range(6)):
        return "Monday–Saturday"
    if len(days) == 1:
        return _NAMES[days[0]]
    return ", ".join(_NAMES[d] for d in days)


def _window_phrase(windows):
    bits = []
    for w in windows:
        bits.append(_fmt_time(w["start"]) + "–" + _fmt_time(w["end"]))
    days = windows[0]["days"] if windows else list(range(7))
    return " and ".join(bits) + ", " + _fmt_days(days)


def _summarize(sign):
    lines = []
    if sign.get("red_curb"):
        lines.append("No parking at any time.")
    if sign.get("time_limit_min"):
        n = sign["time_limit_min"]
        label = f"{n // 60}-hour" if n % 60 == 0 else f"{n}-minute"
        hours = ""
        if sign.get("limit_start") is not None and sign.get("limit_end") is not None:
            hours = ", " + _fmt_time(sign["limit_start"]) + "–" + _fmt_time(sign["limit_end"])
        days = ", " + _fmt_days(sign.get("limit_days") or list(range(7)))
        rate = (", " + sign["rate"] + " per hour") if sign.get("rate") else ""
        kind = "meter" if sign.get("meter_sign") or sign.get("rate") else "parking"
        lines.append(f"{label} {kind}{hours}{days}{rate}.")
    if sign.get("permit_exempts_limit"):
        lines.append("A permit exempts you from the time limit.")
    if sign.get("sweeping"):
        lines.append("Street sweeping " + _window_phrase(sign["sweeping"]) + ".")
    if sign.get("tow"):
        lines.append("Tow-away, no stopping, " + _window_phrase(sign["tow"]) + ".")
    if sign.get("permit"):
        lines.append("Permit holders only, " + _window_phrase(sign["permit"]) + ".")
    if sign.get("holidays_enforced"):
        lines.append("Holidays are enforced.")
    if sign.get("arrows") == "left":
        lines.append("Arrow points left. The rule applies that direction from the sign.")
    elif sign.get("arrows") == "right":
        lines.append("Arrow points right. The rule applies that direction from the sign.")
    elif sign.get("arrows") == "both":
        lines.append("Arrows point both ways. The rule applies in both directions from the sign.")
    return lines


def _sig(rules):
    return tuple(sorted((r.kind, r.limit_min, r.start, r.end, tuple(sorted(r.days))) for r in rules))


def _brief(rules):
    bits = []
    for r in rules:
        if r.kind == "limit" and r.limit_min:
            label = f"{r.limit_min // 60}-hour limit" if r.limit_min % 60 == 0 else f"{r.limit_min}-minute limit"
        else:
            label = r.label
        bits.append(label + ", " + _fmt_time(r.start) + "–" + _fmt_time(r.end) + ", " + _fmt_days(list(r.days)))
    return "; ".join(bits)
