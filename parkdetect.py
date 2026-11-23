"""Decide when to ask "Did you just park here?" from location samples.

A sample is {"t": seconds, "lat": float, "lng": float, "speed": m/s or None}.
iOS Safari often omits speed, so a stop can also be a stretch of almost no movement
after a stretch of driving. This does not see Bluetooth, CarPlay, or the phone
once the page is in the background.
"""
import math

DRIVE_SPEED = 4.0       # m/s, about 9 mph
STOP_SPEED = 1.0        # m/s
DRIVE_WINDOW = 20       # seconds of recent driving
STOP_WINDOW = 18        # seconds of being stopped
DRIVE_DISTANCE = 40     # meters, used when speed is missing
STOP_DISTANCE = 12      # meters


def _dist(a, b):
    r = math.pi / 180
    lat1, lng1, lat2, lng2 = a["lat"], a["lng"], b["lat"], b["lng"]
    h = math.sin((lat2 - lat1) * r / 2) ** 2 + math.cos(lat1 * r) * math.cos(lat2 * r) * math.sin((lng2 - lng1) * r / 2) ** 2
    return 12742000 * math.asin(math.sqrt(min(1, h)))


def _span(samples, start, end):
    return [s for s in samples if start <= s["t"] <= end]


def should_prompt(samples):
    """True once, when the latest samples show driving and then a stop."""
    if len(samples) < 4:
        return False
    samples = sorted(samples, key=lambda s: s["t"])
    now = samples[-1]["t"]
    recent = _span(samples, now - STOP_WINDOW, now)
    earlier = _span(samples, now - STOP_WINDOW - DRIVE_WINDOW, now - STOP_WINDOW)
    if len(recent) < 2 or len(earlier) < 2:
        return False
    def moving(group, speed_at_least, distance_at_least):
        speeds = [s["speed"] for s in group if isinstance(s.get("speed"), (int, float)) and s["speed"] >= 0]
        if speeds and max(speeds) >= speed_at_least:
            return True
        if any(s.get("speed") is not None for s in group) and speeds and max(speeds) < speed_at_least:
            return False
        return _dist(group[0], group[-1]) >= distance_at_least
    def stopped(group):
        speeds = [s["speed"] for s in group if isinstance(s.get("speed"), (int, float)) and s["speed"] >= 0]
        if len(speeds) >= 2 and max(speeds) < STOP_SPEED:
            return True
        if speeds and max(speeds) >= STOP_SPEED:
            return False
        return _dist(group[0], group[-1]) <= STOP_DISTANCE
    return moving(earlier, DRIVE_SPEED, DRIVE_DISTANCE) and stopped(recent)
