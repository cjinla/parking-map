"""Offline test: builds a tiny database by hand and checks the answers."""
import base64, json, sqlite3, os
from datetime import datetime
from zoneinfo import ZoneInfo
import ingest
ingest.DB = "test.db"
import server
server.DB = "test.db"
from rules import (Rule, move_by, answer, windows, apply_sign, rules_from_sign, holiday_name,
                   sweeping_holiday_name, parse_days)
import alarms
import signparse
import signread
import vapid_keys
from datetime import date

tz = ZoneInfo("America/Los_Angeles")
def at(s): return datetime.fromisoformat(s).replace(tzinfo=tz)

# 2-hour meter 8am-6pm every day, sweeping Wednesday 10am-12pm
rules = [Rule("limit", list(range(7)), 480, 1080, "2-hour meter", 120),
         Rule("no", [2], 600, 720, "Street sweeping")]
assert move_by(rules, at("2026-10-23T14:00"))["move_by"].startswith("2026-10-23T16:00")   # Fri 2pm -> 4pm
r = move_by(rules, at("2026-10-23T17:00"))                                                  # Fri 5pm -> 6pm meter ends, limit 7pm? no: free after 6pm
assert r["move_by"].startswith("2026-10-28T10:00"), r   # meter ends at 6pm, so next deadline is Wednesday sweeping
assert move_by(rules, at("2026-10-21T10:30"))["status"] == "no"                              # Wed sweeping
r = move_by(rules, at("2026-10-20T20:00"))                                                   # Tue 8pm free, sweeping next day 10am
assert r["move_by"].startswith("2026-10-21T10:00"), r
# end-to-end through sqlite
if os.path.exists("test.db"): os.remove("test.db")
db = sqlite3.connect("test.db")
db.executescript("CREATE TABLE meters(id,lat,lng,address,rules); CREATE TABLE sweep(id,props,geom,minlat,maxlat,minlng,maxlng);")
db.execute("INSERT INTO meters VALUES('1',34.1016,-118.3387,'HOLLYWOOD BLVD',?)",
           (json.dumps([{"kind":"limit","days":list(range(7)),"start":480,"end":1080,"label":"2-hour meter","limit_min":120,"source":"t","confidence":"high"}]),))
db.execute("CREATE TABLE ppd(props,geom,minlat,maxlat,minlng,maxlng)")
db.execute("INSERT INTO ppd VALUES('{}',?,34.0,34.2,-118.4,-118.3)",(json.dumps({"type":"Polygon","coordinates":[[[-118.4,34.0],[-118.3,34.0],[-118.3,34.2],[-118.4,34.2],[-118.4,34.0]]]}),))
db.commit()
rs, addr, conf, warns = server.lookup(34.1016, -118.3387, at("2026-10-23T14:00"))
assert len(warns) == 1
assert addr == "HOLLYWOOD BLVD" and conf == "high" and len(rs) == 1
assert ingest.parse_limit("2HR") == 120 and ingest.parse_limit("30 MIN") == 30
assert ingest.parse_hours("8AM-6PM") == (480, 1080), ingest.parse_hours("8AM-6PM")
assert ingest.parse_limit("2HR-4HR") == 120 and ingest.parse_limit("4HR-30MIN") == 30 and ingest.parse_limit("10HR") == 600
mr = ingest.meter_rules({"timelimit": "2HR"})[0]
assert mr["hours_known"] is False and mr["confidence"] == "low"
res = move_by([Rule(**mr)], at("2026-10-23T14:00"))
assert res["hours_known"] is False and "Check the sign" in res["headline"] and res["move_by"].startswith("2026-10-23T16:00"), res
body = server.assemble(34.1016, -118.3387, at("2026-10-23T14:00"))
assert body["can_park"] is True and body["answer_word"] == "Yes", body
assert body["move_by"].startswith("2026-10-23T16:00") and body["facts"]["time_limit"] == "2 hours"
assert body["timeline"][0]["date"] == "2026-10-23"
assert any(s["status"] == "limit" for s in body["timeline"][0]["segments"])
assert {g["id"] for g in body["gaps"]} >= {"sweep", "tow", "curb"}
assert "meter" not in {g["id"] for g in body["gaps"]}
os.remove("test.db")

# Overlap: tow-away beats a meter, and the earlier deadline wins.
tow = [Rule("limit", list(range(7)), 8*60, 18*60, "2-hour meter", 120, topic="meter"),
       Rule("no", list(range(5)), 8*60, 10*60, "Rush-hour tow-away", topic="tow")]
assert move_by(tow, at("2026-10-23T09:00"))["status"] == "no"
assert answer(tow, at("2026-10-23T10:30"))["move_by"].startswith("2026-10-23T12:30")
assert move_by(tow, at("2026-10-23T07:00"))["move_by"].startswith("2026-10-23T08:00")

# Overnight rule crosses midnight. Monday 2026-10-19.
night = [Rule("no", [0], 22*60, 6*60, "Overnight tow-away", topic="tow")]
assert move_by(night, at("2026-10-19T21:00"))["move_by"].startswith("2026-10-19T22:00")
assert move_by(night, at("2026-10-19T23:00"))["status"] == "no"
late = answer(night, at("2026-10-20T01:00"))
assert late["status"] == "no" and late["until"].startswith("2026-10-20T06:00"), late
assert answer(night, at("2026-10-20T06:00"))["status"] != "no"
mon = windows(night, at("2026-10-19T12:00"), 2)
assert any(s["status"] == "no" and s["start_min"] == 22*60 for s in mon[0]["segments"])
assert any(s["status"] == "no" and s["start_min"] == 0 and s["end_min"] == 6*60 for s in mon[1]["segments"])

# Sweeping is only on its weekday, and the timeline is three blocks that day.
sweep = [Rule("no", [2], 600, 720, "Street sweeping", topic="sweep")]
tl = windows(sweep, at("2026-10-20T08:00"), 4)  # Tue, Wed, Thu, Fri
assert tl[0]["weekday"] == "Tuesday" and all(s["status"] != "no" for s in tl[0]["segments"])
assert tl[1]["weekday"] == "Wednesday"
assert [(s["start_min"], s["end_min"], s["status"]) for s in tl[1]["segments"]] == [(0, 600, "ok"), (600, 720, "no"), (720, 1440, "ok")]

# Holidays. July 4 2026 is a Saturday, observed Friday July 3 as well.
assert holiday_name(date(2026, 1, 19)) == "Martin Luther King Jr. Day"
assert holiday_name(date(2026, 2, 16)) == "Presidents Day"
assert holiday_name(date(2026, 5, 25)) == "Memorial Day"
assert holiday_name(date(2026, 9, 7)) == "Labor Day"
assert holiday_name(date(2026, 10, 12)) == "Columbus Day"
assert holiday_name(date(2026, 11, 26)) == "Thanksgiving Day"
assert holiday_name(date(2026, 11, 11)) == "Veterans Day"
assert holiday_name(date(2026, 7, 4)) == "Independence Day"
assert holiday_name(date(2026, 7, 3)) == "Independence Day (observed)"
assert holiday_name(date(2026, 7, 2)) is None
assert holiday_name(date(2021, 7, 4)) == "Independence Day"
assert holiday_name(date(2021, 7, 5)) == "Independence Day (observed)"
assert holiday_name(date(2027, 12, 31)) == "New Year's Day (observed)"
assert sweeping_holiday_name(date(2026, 3, 31)) == "Cesar Chavez Day"
assert sweeping_holiday_name(date(2026, 11, 27)) == "Day after Thanksgiving"
assert holiday_name(date(2026, 3, 31)) is None

meter_h = Rule("limit", list(range(7)), 8*60, 18*60, "2-hour meter", 120, skip_holidays=True, hours_known=True, topic="meter")
sweep_h = Rule("no", [2], 600, 720, "Street sweeping", skip_holidays=True, also_sweep_holidays=True, topic="sweep")
vet = answer([meter_h, sweep_h], at("2026-11-11T10:30"))
assert vet["status"] != "no" and vet["holiday"] == "Veterans Day", vet
assert vet["move_by"].startswith("2026-11-18T10:00"), vet
mid = next(s for s in windows([meter_h, sweep_h], at("2026-11-11T10:30"), 1)[0]["segments"] if s["start_min"] <= 630 < s["end_min"])
assert mid["status"] == "ok" and "Not enforced" in mid["label"]
july3 = answer([meter_h], at("2026-07-03T10:00"))
assert july3["can_park"] is True and july3["move_by"] is None and "Independence" in july3["holiday"]
assert answer([meter_h], at("2026-07-02T10:00"))["move_by"].startswith("2026-07-02T12:00")
red = Rule("no", list(range(7)), 0, 1440, "Red curb", topic="curb")
assert answer([red], at("2026-07-04T15:00"))["status"] == "no"
cesar_meter = Rule("limit", list(range(7)), 8*60, 18*60, "2-hour meter", 120, skip_holidays=True, hours_known=True, topic="meter")
cesar_sweep = Rule("no", [1], 8*60, 10*60, "Street sweeping", skip_holidays=True, also_sweep_holidays=True, topic="sweep")
cesar = answer([cesar_meter, cesar_sweep], at("2026-03-31T09:00"))
assert cesar["status"] == "limit" and cesar["move_by"].startswith("2026-03-31T11:00"), cesar
assert "Cesar Chavez" in cesar["holiday_note"]
friday_after = answer([
    Rule("limit", list(range(7)), 8*60, 18*60, "2-hour meter", 120, skip_holidays=True, hours_known=True, topic="meter"),
    Rule("no", [4], 8*60, 12*60, "Street sweeping", skip_holidays=True, also_sweep_holidays=True, topic="sweep"),
], at("2026-11-27T10:00"))
assert friday_after["status"] == "limit" and friday_after["move_by"].startswith("2026-11-27T12:00"), friday_after
assert "Thanksgiving" in friday_after["holiday_note"]

assert parse_days("Th,F") == [3, 4] and parse_days("Tu") == [1] and parse_days("W") == [2]
assert parse_days("M") == [0] and parse_days("weekdays") == [0, 1, 2, 3, 4] and parse_days("Sunset") == []
rules_s, warn = server.sweep_from_props({"Day": "Th,F", "Boundaries": "Sherman Way"})
assert rules_s == [] and "Thursday" in warn and "Friday" in warn
rules_s, warn = server.sweep_from_props({"posted_times": [{"days": [2, 3], "start": 600, "end": 720}]})
assert rules_s[0].days == [2, 3] and rules_s[0].start == 600 and rules_s[0].also_sweep_holidays
rules_s, warn = server.sweep_from_props({"notes": "Wednesday 10:00 am - 12:00 pm"})
assert rules_s[0].days == [2] and rules_s[0].start == 600 and rules_s[0].end == 720

base, day = ingest.split_route("10P136 Th")
assert (base, day) == ("10P136", 3)
assert ingest.split_route("10P136 W")[1] == 2
assert ingest.split_route("EV231-9")[1] is None
posted = ingest.index_posted([
    {"route_no": "10P136 Th", "time_start": "8:00 AM", "time_end": "10:00 AM"},
    {"route_no": "10P136 W", "time_start": "8:00 AM", "time_end": "10:00 AM"},
])
assert posted["10P136"] == [{"days": [2, 3], "start": 480, "end": 600}]
priced = ingest.meter_rules({"timelimit": "2HR", "raterange": "$1.50", "ratetype": "FLAT"})[0]
assert priced["rate"] == "$1.50" and priced["rate_type"] == "FLAT" and priced["skip_holidays"] is True
tod = answer([Rule(**ingest.meter_rules({"timelimit": "2HR", "raterange": "$1.00 - $2.00", "ratetype": "TOD"})[0])], at("2026-10-23T14:00"))
assert tod["facts"]["meter"] == "Yes — $1.00 - $2.00 (changes by time of day)", tod["facts"]

sign = {"time_limit_min": 60, "limit_start": 480, "limit_end": 20*60, "limit_days": [0, 1, 2, 3, 4], "rate": "$1.50",
        "sweeping": [{"days": [2], "start": 600, "end": 720}],
        "tow": [{"days": [0, 1, 2, 3, 4], "start": 7*60, "end": 9*60}]}
signed = rules_from_sign(sign)
assert answer(signed, at("2026-10-21T10:30"))["status"] == "no"
assert answer(signed, at("2026-10-21T12:00"))["move_by"].startswith("2026-10-21T13:00")
enforced = rules_from_sign({"time_limit_min": 60, "limit_start": 480, "limit_end": 20*60,
                            "limit_days": [0, 1, 2, 3, 4], "holidays_enforced": True})
assert answer(enforced, at("2026-11-11T10:00"))["status"] == "limit"
city = [Rule("limit", list(range(7)), 0, 1440, "4-hour limit", 240, topic="meter", hours_known=False)]
assert apply_sign(city, {"time_limit_min": 30, "limit_start": 480, "limit_end": 1080})[0].limit_min == 30
assert apply_sign(city, {"time_limit_min": 0}) == []

alarms.SESSION_DB = "test-sessions.db"
for name in ("test-sessions.db", "test-sessions.db-wal", "test-sessions.db-shm"):
    if os.path.exists(name): os.remove(name)
assert [t["minutes_before"] for t in alarms.reminder_times(at("2026-10-23T14:10"), at("2026-10-23T14:00"))] == [5, 0]
assert [t["minutes_before"] for t in alarms.reminder_times(at("2026-10-23T14:03"), at("2026-10-23T14:00"))] == [0]
sid = alarms.upsert_sub({"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}})
saved = alarms.save_park(34.1, -118.3, "HOLLYWOOD BLVD", at("2026-10-23T14:30"), "2-hour meter ends", "move", sid, at("2026-10-23T14:00"))
assert [r["minutes_before"] for r in saved["reminders"]] == [15, 5, 0]
ics = alarms.build_ics(saved["id"], at("2026-10-23T14:30"), "Move your car", "2-hour meter ends")
assert "TRIGGER:-PT15M" in ics and "TRIGGER:-PT5M" in ics and "TRIGGER:PT0S" in ics
sent = []
def fake(sub, payload):
    sent.append(payload["title"])
    return True
assert alarms.send_due(at("2026-10-23T14:15:05"), fake) == 1
assert alarms.send_due(at("2026-10-23T14:25:00"), fake) == 1
assert alarms.send_due(at("2026-10-23T14:30:00"), fake) == 1
assert sent == ["Move your car in 15 minutes", "Move your car in 5 minutes", "Move your car now"]
try:
    alarms.upsert_sub({})
    raise SystemExit("bad subscription was accepted")
except ValueError:
    pass
keys = vapid_keys.new_key_material()
assert len(keys["public_raw"]) == 65 and keys["private_pem"].startswith("-----BEGIN PRIVATE KEY-----")

# Auto-park: a Shortcut posts lat/lng with a per-phone token. No token is committed.
if os.path.exists("test.db"): os.remove("test.db")
db = sqlite3.connect("test.db")
db.executescript("""CREATE TABLE meters(id,lat,lng,address,rules);
 CREATE TABLE sweep(id,props,geom,minlat,maxlat,minlng,maxlng);
 CREATE TABLE ppd(props,geom,minlat,maxlat,minlng,maxlng);""")
db.execute("INSERT INTO meters VALUES('1',34.1016,-118.3387,'HOLLYWOOD BLVD',?)", (json.dumps([{
    "kind": "limit", "days": list(range(7)), "start": 480, "end": 1080, "label": "2-hour meter",
    "limit_min": 120, "source": "t", "confidence": "high", "hours_known": True, "topic": "meter"}]),))
db.execute("INSERT INTO meters VALUES('2',34.2,-118.2,'RED CURB',?)", (json.dumps([{
    "kind": "no", "days": list(range(7)), "start": 0, "end": 1440, "label": "Red curb", "topic": "curb"}]),))
db.commit()
server.DB = "test.db"
fresh = alarms.ensure_device()
assert alarms.valid_token(fresh) and alarms.ensure_device(fresh) == fresh
adopted = "a" * 40
assert alarms.ensure_device(adopted) == adopted
assert alarms.ensure_device("too-short") != "too-short"
code, payload = server.api_autopark({"lat": 34.1, "lng": -118.3}, "", at("2026-10-23T14:00"))
assert code == 401, payload
code, payload = server.api_autopark({"token": "b" * 40, "lat": 34.1, "lng": -118.3}, "", at("2026-10-23T14:00"))
assert code == 401
code, payload = server.api_autopark({"token": fresh}, "", at("2026-10-23T14:00"))
assert code == 400 and "lat" in payload["error"]
pushes = []
def grab(sub, payload):
    pushes.append(payload)
    return True
code, payload = server.api_autopark(
    {"token": fresh, "lat": 34.1016, "lng": -118.3387}, "", at("2026-10-23T14:00"), grab)
assert code == 200, payload
assert payload["message"] == "Parked at HOLLYWOOD BLVD, move by Fri 4:00 PM", payload
assert payload["alarm_at"].startswith("2026-10-23T16:00")
assert [r["minutes_before"] for r in payload["reminders"]] == [15, 5, 0]
assert pushes == []
linked = alarms.ensure_device(sub={"endpoint": "https://push.example/auto", "keys": {"p256dh": "k", "auth": "a"}})
code, payload = server.api_autopark(
    {"lat": 34.1016, "lng": -118.3387}, "Bearer " + linked, at("2026-10-23T14:00"), grab)
assert code == 200 and pushes[-1]["title"] == "Parked", pushes
assert pushes[-1]["body"] == "Parked at HOLLYWOOD BLVD, move by Fri 4:00 PM"
again = len(pushes)
# Bluetooth disconnect, then CarPlay a minute later with a drifted GPS fix.
code, second = server.api_autopark(
    {"token": linked, "lat": 34.1035, "lng": -118.3387}, "", at("2026-10-23T14:01"), grab)
assert code == 200 and second["duplicate"] is True and second["id"] == payload["id"], second
assert len(pushes) == again
def counts(sql, args):
    conn = sqlite3.connect(alarms.SESSION_DB)
    try:
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()
assert counts("SELECT COUNT(*) FROM parks WHERE device_token=? AND cancelled=0", (linked,)) == 1
assert counts("SELECT COUNT(*) FROM reminders WHERE park_id=?", (payload["id"],)) == 3
# After the window, a new departure gets its own session and its own three reminders.
code, later = server.api_autopark(
    {"token": linked, "lat": 34.1016, "lng": -118.3387}, "", at("2026-10-23T14:04"), grab)
assert code == 200 and later["duplicate"] is False and later["id"] != payload["id"], later
assert counts("SELECT COUNT(*) FROM reminders WHERE park_id=?", (later["id"],)) == 3
assert counts("SELECT COUNT(*) FROM reminders WHERE park_id=?", (payload["id"],)) == 3
red_token = alarms.ensure_device()
code, red = server.api_autopark(
    {"token": red_token, "lat": 34.2, "lng": -118.2}, "", at("2026-10-23T14:00"), grab)
assert code == 200 and red["message"] == "Parked at RED CURB, move now", red
empty_token = alarms.ensure_device()
code, nowhere = server.api_autopark(
    {"token": empty_token, "lat": 0, "lng": 0}, "", at("2026-10-23T14:00"), grab)
assert code == 200 and "No move-by time" in nowhere["message"]
# Escalating alerts: leads, night-before and morning for sweeping/tow, repeats, snooze, cancel.
sweep_deadline = at("2026-10-21T08:00")  # Wednesday 8 AM
planned = alarms.plan_alerts(sweep_deadline, at("2026-10-20T15:00"), "Street sweeping starts")
kinds = [a["kind"] for a in planned]
assert kinds == ["night", "morning", "15", "5", "now"], kinds
assert next(a["at"] for a in planned if a["kind"] == "night") == at("2026-10-20T20:00")
assert next(a["at"] for a in planned if a["kind"] == "morning") == at("2026-10-21T07:00")
afternoon = alarms.plan_alerts(at("2026-10-21T14:00"), at("2026-10-20T12:00"), "Tow-away / no stopping starts")
assert "night" in [a["kind"] for a in afternoon] and "morning" not in [a["kind"] for a in afternoon]
late = alarms.plan_alerts(sweep_deadline, at("2026-10-20T21:00"), "Street sweeping starts")
assert "night" not in [a["kind"] for a in late] and "morning" in [a["kind"] for a in late]
early = alarms.plan_alerts(at("2026-10-21T06:30"), at("2026-10-20T18:00"), "Rush-hour tow-away starts")
assert next(a["at"] for a in early if a["kind"] == "morning") == at("2026-10-21T05:30")
custom = alarms.plan_alerts(at("2026-10-23T16:00"), at("2026-10-23T14:00"), "2-hour meter ends", {"lead_minutes": [30, 10]})
assert [a["minutes_before"] for a in custom if a["minutes_before"] is not None] == [30, 10, 0]
meter_only = alarms.plan_alerts(at("2026-10-23T16:00"), at("2026-10-23T14:00"), "2-hour meter ends")
assert [a["kind"] for a in meter_only] == ["15", "5", "now"]
final = alarms.alert_content("now", "HOLLYWOOD BLVD", "Street sweeping starts", "abc")
assert final["title"] == "Move your car now" and final["requireInteraction"] is True and final["urgent"] is True and final["level"] == "final"
lead = alarms.alert_content("15", "HOLLYWOOD BLVD", "2-hour meter ends", "abc")
assert lead["title"] == "Move your car in 15 minutes" and lead["urgent"] is False
repeat = alarms.alert_content("repeat", "HOLLYWOOD BLVD", "Street sweeping starts", "abc")
assert repeat["title"] == "Still there — move the car" and repeat["requireInteraction"] is True
assert alarms.next_repeat(at("2026-10-23T16:00"), at("2026-10-23T16:00")) == at("2026-10-23T16:03")
assert alarms.next_repeat(at("2026-10-23T16:00"), at("2026-10-23T16:16")) is None
alarms.SESSION_DB = "test-alerts.db"
sid = alarms.upsert_sub({"endpoint": "https://push.example/nag", "keys": {"p256dh": "k", "auth": "a"}})
nag_park = alarms.save_park(34.1, -118.3, "HOLLYWOOD BLVD", at("2026-10-23T16:00"), "Street sweeping starts", "move", sid, at("2026-10-23T15:56"))
assert "night" not in [r["kind"] for r in nag_park["reminders"]]  # same afternoon
nag_msgs = []
def grab_nag(sub, payload):
    nag_msgs.append(payload)
    return True
assert alarms.send_due(at("2026-10-23T16:00:01"), grab_nag) == 1
assert nag_msgs[-1]["urgent"] is True and nag_msgs[-1]["requireInteraction"] is True
conn = sqlite3.connect(alarms.SESSION_DB)
queued = conn.execute("SELECT fire_at, kind FROM reminders WHERE park_id=? AND sent=0", (nag_park["id"],)).fetchall()
conn.close()
assert len(queued) == 1 and queued[0][1] == "repeat" and queued[0][0].startswith("2026-10-23T16:03"), queued
snoozed = alarms.snooze_park(nag_park["id"], at("2026-10-23T16:01"))
assert snoozed["snooze_until"].startswith("2026-10-23T16:11")
conn = sqlite3.connect(alarms.SESSION_DB)
left = conn.execute("SELECT fire_at FROM reminders WHERE park_id=? AND sent=0 AND kind='repeat'", (nag_park["id"],)).fetchall()
conn.close()
assert [row[0] for row in left] == [snoozed["snooze_until"]]
assert alarms.cancel_park(nag_park["id"]) is True
conn = sqlite3.connect(alarms.SESSION_DB)
assert conn.execute("SELECT COUNT(*) FROM reminders WHERE park_id=? AND sent=0", (nag_park["id"],)).fetchone()[0] == 0
conn.close()
assert alarms.send_due(at("2026-10-23T16:11"), grab_nag) == 0

# Sign text -> rules. Photos are not required; the vision API only supplies this text.
sweep = signparse.parse_sign_text("NO PARKING\nSTREET SWEEPING\nTUESDAY\n8AM - 10AM\n←")
assert sweep["summary"] == [
    "Street sweeping 8:00 AM–10:00 AM, Tuesday.",
    "Arrow points left. The rule applies that direction from the sign."]
assert sweep["sweeping"] == [{"days": [1], "start": 480, "end": 600}] and sweep["arrows"] == "left"
limit = signparse.parse_sign_text("2 HOUR PARKING\n8AM TO 6PM\nMONDAY - SATURDAY")
assert limit["time_limit_min"] == 120 and limit["limit_days"] == list(range(6))
assert limit["limit_start"] == 480 and limit["limit_end"] == 1080
assert limit["summary"] == ["2-hour parking, 8:00 AM–6:00 PM, Monday–Saturday."]
tow = signparse.parse_sign_text("TOW-AWAY\nNO STOPPING\n7AM - 9AM\n4PM - 7PM\nMONDAY THRU FRIDAY")
assert tow["tow"] == [{"days": list(range(5)), "start": 420, "end": 540},
                      {"days": list(range(5)), "start": 960, "end": 1140}]
assert "7:00 AM–9:00 AM and 4:00 PM–7:00 PM, Monday–Friday" in tow["summary"][0]
assert signparse.parse_sign_text("NO PARKING ANY TIME")["summary"] == ["No parking at any time."]
permit = signparse.parse_sign_text("PERMIT PARKING ONLY\n6PM - 8AM\nDAILY")
assert permit["permit"] == [{"days": list(range(7)), "start": 1080, "end": 480}]
meter = signparse.parse_sign_text("PAY AT METER\n$1.50 PER HOUR\n2 HOUR LIMIT\n8AM - 8PM\nMON - SAT")
assert meter["summary"] == ["2-hour meter, 8:00 AM–8:00 PM, Monday–Saturday, $1.50 per hour."]
one = signparse.parse_sign_text("1 HOUR PARKING\n8AM-6PM\nEXCEPT SUNDAY\nHOLIDAYS ENFORCED\n→")
assert one["time_limit_min"] == 60 and one["limit_days"] == list(range(6)) and one["holidays_enforced"] is True
assert one["arrows"] == "right" and any("Holidays are enforced." == line for line in one["summary"])
stacked = signparse.parse_sign_text(
    "NO PARKING\nSTREET SWEEPING\nWEDNESDAY\n8AM TO 10AM\n\n2 HOUR PARKING\n8AM TO 6PM\nMON - SAT")
assert stacked["sweeping"][0]["days"] == [2] and stacked["time_limit_min"] == 120
both = signparse.parse_sign_text("2 HOUR PARKING\n8AM - 6PM\n← →")
assert both["arrows"] == "both"
city = [Rule("limit", list(range(7)), 0, 1440, "2-hour limit", 120, topic="meter", hours_known=False)]
msgs = signparse.sign_conflicts(city, one)
assert msgs and "disagree" in msgs[0] and "Using the sign" in msgs[0] and "1-hour" in msgs[0]
same = signparse.parse_sign_text("2 HOUR PARKING\n8AM TO 6PM\nMONDAY - SATURDAY")
agree_city = [Rule("limit", list(range(6)), 480, 1080, "2-hour limit", 120, topic="meter", hours_known=True)]
assert signparse.sign_conflicts(agree_city, same) == []
merged = apply_sign(city, one)
assert len(merged) == 1 and merged[0].limit_min == 60 and merged[0].source == "Sign you entered"
os.environ.pop("SIGN_VISION_API_KEY", None)
assert signread.configured() is False
try:
    signread.decode_images([])
    raise SystemExit("empty photos were accepted")
except ValueError:
    pass
tiny = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()
saved_scan = signread.read_sign(signread.decode_images(["data:image/png;base64," + tiny]), [])
assert saved_scan["available"] is False and saved_scan["scan_id"] and "Type what the sign says" in saved_scan["message"]
assert signread.attach_scan(saved_scan["scan_id"], "park123") == 1
import shutil
shutil.rmtree(signread.PHOTO_DIR, ignore_errors=True)
os.remove("test.db")
for name in ("test-sessions.db", "test-sessions.db-wal", "test-sessions.db-shm",
             "test-alerts.db", "test-alerts.db-wal", "test-alerts.db-shm"):
    if os.path.exists(name): os.remove(name)
print("all tests passed")
