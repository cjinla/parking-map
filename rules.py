"""Rule model and evaluator. Weekdays use Python's convention: Monday=0."""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta

RANK = {"no": 3, "permit": 2, "limit": 1}

@dataclass
class Rule:
    kind: str                 # "no" | "permit" | "limit"
    days: list                # weekdays, Monday=0
    start: int                # minutes after midnight
    end: int                  # may be < start for overnight rules
    label: str
    limit_min: int = None     # for kind == "limit"
    source: str = ""
    confidence: str = "high"  # "high" (sign-level data) | "low" (area-level data)

def active(r: Rule, dt: datetime) -> bool:
    m, d = dt.hour * 60 + dt.minute, dt.weekday()
    if r.start < r.end:
        return d in r.days and r.start <= m < r.end
    return (d in r.days and m >= r.start) or ((d - 1) % 7 in r.days and m < r.end)

def current(rules, dt):
    hits = [r for r in rules if active(r, dt)]
    return max(hits, key=lambda r: RANK[r.kind]) if hits else None

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
    return {"status": "limit" if cur else "ok",
            "headline": "You can park, but move your car by " + deadline.strftime("%a %I:%M %p"),
            "move_by": deadline.isoformat(), "reason": why}

def to_dict(r): return asdict(r)
