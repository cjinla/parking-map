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


def payment_options(space_id=None, zone=None):
    links = [
        {"name": "LADOT: Park Smarter and ParkMobile", "url": METER_PAY},
        {"name": "ParkMobile in Los Angeles", "url": PARKMOBILE_LA},
    ]
    sms = text_pay_url(zone)
    if sms:
        links.insert(0, {"name": "Text zone " + "".join(ch for ch in str(zone) if ch.isdigit()) + " to " + TEXT_PAY_NUMBER,
                         "url": sms})
    return {
        "space_id": space_id,
        "zone": "".join(ch for ch in str(zone) if ch.isdigit()) if zone else None,
        "links": links,
        "note": "The city meter list has a space id, not the pay-by-phone zone. Read the zone on the meter sticker or screen.",
    }


def help_links():
    return [
        {"name": "Pay a parking citation", "url": PAY_CITATION},
        {"name": "PayTix (LADOT app payment portal)", "url": PAYTIX},
        {"name": "Contest a citation (initial review)", "url": CONTEST},
        {"name": "Booted and towed vehicles", "url": TOW_LOOKUP},
        {"name": "Parking enforcement", "url": ENFORCEMENT},
    ]
