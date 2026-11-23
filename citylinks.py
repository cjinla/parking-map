"""Official Los Angeles payment, citation, and tow pages.

Meter inventory rows have a space id, not a ParkMobile zone. The zone is printed
on the meter sticker. Text-to-pay (77447) is the number LADOT publishes.
"""

METER_PAY = "https://ladotparking.org/parking-meters/la-express-park-and-parking-in-la/"
PARKMOBILE_LA = "https://parkmobile.io/parking/locations/ca/los-angeles-parking"
TEXT_PAY_NUMBER = "77447"
PAY_CITATION = "https://ladotparking.org/adjudication-division/pay-a-citation/"
PAYTIX = "https://paytix.etimsportal.com/"
CONTEST = "https://ladotparking.org/contact-us/"
TOW_LOOKUP = "https://ladotparking.org/adjudication-division/booted-towed-vehicles/"
ENFORCEMENT = "https://ladotparking.org/parking-enforcement/"


def text_pay_url(zone):
    zone = "".join(ch for ch in str(zone or "") if ch.isdigit())
    if not zone:
        return None
    return "sms:" + TEXT_PAY_NUMBER + "&body=" + zone


PARKMOBILE_SEARCH = "https://app.parkmobile.io/search/"
SPOTHERO = "https://spothero.com/"
PAYMENT_NEEDS = (
    "This app does not charge a card. A real payment would need a merchant account, "
    "a ParkMobile or LADOT partner API, and a map from the city's space id to the pay-by-phone zone. "
    "None of those are connected. No API key is stored here."
)


def payment_options(space_id=None, zone=None, lat=None, lng=None):
    links = [
        {"name": "ParkMobile search", "url": PARKMOBILE_SEARCH, "kind": "meter"},
        {"name": "LADOT: Park Smarter and ParkMobile", "url": METER_PAY, "kind": "meter"},
        {"name": "ParkMobile in Los Angeles", "url": PARKMOBILE_LA, "kind": "meter"},
        {"name": "SpotHero lots and garages", "url": SPOTHERO, "kind": "lot"},
    ]
    sms = text_pay_url(zone)
    if sms:
        links.insert(0, {"name": "Text zone " + "".join(ch for ch in str(zone) if ch.isdigit()) + " to " + TEXT_PAY_NUMBER,
                         "url": sms, "kind": "meter"})
    return {
        "space_id": space_id,
        "zone": "".join(ch for ch in str(zone) if ch.isdigit()) if zone else None,
        "lat": lat, "lng": lng,
        "links": links,
        "note": "The city meter list has a space id, not the pay-by-phone zone. The text is prefilled when you type the zone from the sticker. SpotHero is for lots and garages, not the curb meter.",
        "integration": PAYMENT_NEEDS,
    }


def help_links():
    return [
        {"name": "Pay a parking citation", "url": PAY_CITATION},
        {"name": "PayTix (LADOT app payment portal)", "url": PAYTIX},
        {"name": "Contest a citation (initial review)", "url": CONTEST},
        {"name": "Booted and towed vehicles", "url": TOW_LOOKUP},
        {"name": "Parking enforcement", "url": ENFORCEMENT},
    ]
