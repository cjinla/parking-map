# Can I park here? (Los Angeles)

A phone web app for one question at the curb: **can I park here right now, and when do I have to move?**

Keith uses an iPhone only. The app is a Safari page that can be added to the home screen. The API stays on Render (`la-parking`, https://la-parking-yvn1.onrender.com). `render.yaml` is unchanged as a host: same service, same build (`pip install` then `python ingest.py`), same start (`python server.py`).

```bash
python ingest.py      # download city data into parking.db (needs internet)
python server.py      # http://localhost:8080
python test_backend.py
```

Python 3.9+. Runtime packages are `tzdata` and `pywebpush` (Web Push). City data is loaded with the standard library.

## On an iPhone

1. Open **Safari** (not Chrome) to https://la-parking-yvn1.onrender.com. The first open can take about a minute while the free Render instance wakes up.
2. Tap **Check this spot** and allow location. Or type an address.
3. Read the big answer: **Yes**, **No**, **Permit**, or **Unknown**. Under it: move-by time, time limit, meter price if the city published one, and a day-by-day timeline (today and the next three days).
4. If a line says the city data does not have that rule, open **What does the sign say?** and enter the sign. Posted signs override the app.
5. Tap **I parked here**.
6. Tap **Add to Calendar** and add the event. It carries three alarms: 15 minutes before, 5 minutes before, and at the deadline. This is the alarm that still rings if notifications are off or the server is asleep.
7. For lock-screen alerts from the app itself (iOS 16.4 or newer):
   - Tap the **Share** button, then **Add to Home Screen**.
   - Open **Park LA** from the home screen icon (alerts do not work in a normal Safari tab).
   - Tap **Allow iPhone alerts**, then Allow.
8. The beep and vibration work only while the app is on screen. A locked phone needs the calendar alarm or the notification. iPhones do not vibrate from a web page.

To turn alerts on for a deploy, set the three environment variables below and redeploy. Until those are set, the app says so and the calendar alarm still works.

## Auto-park when the car disconnects

A web app cannot see Bluetooth or CarPlay. Keith’s car uses Bluetooth and sometimes CarPlay, so both disconnects should start the timer. Build **one** shortcut named Park LA, then add **two** automations that run it: CarPlay disconnects, and the car’s Bluetooth device disconnects. Each one gets Current Location and calls `POST /api/autopark`. The server saves the spot, schedules the 15-minute, 5-minute, and deadline alerts, and answers with a confirmation: `Parked at 5301 HOLLYWOOD BLVD, move by Mon 6:10 PM`. If this phone has allowed notifications, that same line is pushed immediately.

If both automations fire within 3 minutes, the second call keeps the first parking session and does not add another set of alarms, even when the two GPS readings disagree. A departure after that window gets its own timer.

The token is created on the phone (Set up auto-park) and stored in `sessions.db` on the server. It is not in the source code. Treat it like a password for this phone. After a Render restart the phone can send the same token again and the server will remember it.

Apple creates an iCloud Shortcut link only from the phone (Shortcuts → Share → Copy iCloud Link). These steps are the recipe.

1. In the parking app, tap **Set up auto-park**. Leave that screen open. It shows the token and the address to call.
2. Open the **Shortcuts** app. Tap the **Shortcuts** tab (the one that is not Automation).
3. Tap **+**. Tap the name at the top and name it **Park LA**.
4. Add **Get Current Location**.
5. Add **Get Contents of URL**.
   - URL: paste the address from the app (`https://la-parking-yvn1.onrender.com/api/autopark` on the live site).
   - Method: **POST**.
   - Header: `Content-Type` = `application/json`.
   - Request Body: **JSON**, with three fields:
     - `token` (Text): paste the token from the app.
     - `lat` (Number): tap the value and choose **Latitude** from Get Current Location.
     - `lng` (Number): tap the value and choose **Longitude**.
6. Add **Get Dictionary from Input** (Contents of URL).
7. Add **Get Dictionary Value**. Key: `message`.
8. Add **Show Notification**. Title: Parked. Body: the Dictionary Value. Tap **Done**.
9. Tap the **Automation** tab, then **+**, then **Create Personal Automation**.
10. Tap **CarPlay**, then **Disconnects**. Turn off **Ask Before Running**. If the choice says **Run Immediately**, pick that.
11. Tap **Next**. Add **Run Shortcut** and choose **Park LA**. Tap **Done**.
12. Tap **+** again, then **Create Personal Automation**. Tap **Bluetooth**, tap **Choose**, pick the car (the name that shows while the phone is in the car), and choose **Is Disconnected**.
13. Turn off **Ask Before Running** when that switch is offered. Tap **Next**. Add **Run Shortcut** and choose **Park LA**. Tap **Done**.

A token can also be sent as `Authorization: Bearer <token>` instead of the JSON field.

Backup: the shortcut can **Open URL** `https://la-parking-yvn1.onrender.com/autopark`. That page uses the token saved on this phone and the GPS. It needs the screen on. The POST works with the phone locked.

CarPlay “Disconnects” can run with the phone locked. Apple sometimes still asks you to confirm the Bluetooth automation. If Ask Before Running cannot be turned off, tap the Shortcuts banner once when you park and the timer starts. The other automation still covers that trip.

The free Render service sleeps. The Shortcut wakes it. The first call after a long idle can take about a minute, and Shortcuts may report a timeout; run it once more.

## What the check returns

`GET /check?lat=&lng=` or `POST /check` with a sign. `POST /park` saves the spot and schedules reminders.

The answer includes: can you park right now, until when, the time limit, whether there is a meter and the price if known, and allowed vs blocked spans. The move-by time is the earliest of the time limit, a meter deadline, street sweeping, and a tow-away window.

`No` means do not park (or move now if you already did). `Permit` means the district requires a permit unless you tap **I have the permit**. `Unknown` means this curb is not in the city files.

## Data sources

Wired to live city files (refreshed when Render builds, via `ingest.py`):

| Source | What it actually contains | How the app uses it |
| --- | --- | --- |
| [LADOT Metered Parking Inventory & Policies](https://data.lacity.org/Transportation/LADOT-Metered-Parking-Inventory-Policies/s49e-q6j2) | Space id, block face, rate type (`FLAT`, `TOD`, `JUMP`), rate range, time limit, coordinates | Nearest meter within 40 m (75 m for a typed address) is the curb segment. Price and time limit are shown. |
| [Posted Street Sweeping Routes](https://data.lacity.org/Transportation/Posted-Street-Sweeping-Routes/krk7-ayq2) | Route id, day (encoded on the route number), start, end, text boundaries. No GPS lines. | Hours are attached only when the route id matches a sweeping area. |
| [BSS ADAPT major-street sweeping areas](https://services1.arcgis.com/PTh9WC0Sf2WS7AAq/arcgis/rest/services/ADAPT_AMRoutes/FeatureServer) | Polygons, a day field (`Tu`, `Th,F`, …), boundaries text. Usually no hours. | If you are inside an area, the app names the days. It does not invent a clock time. |
| [LADOT preferential parking districts](https://data.lacity.org/dataset/LADOT_PPD/s3st-6nwi) | District polygon and name. No hours. | A warning, not a ban. Many districts still allow a short stay without a permit. |
| [LADOT holiday exemptions](https://ladotparking.org/parking-meters/parking-restrictions-holiday-parking/) | Which holidays suspend which rules. | See below. |

Meter rows do **not** include operating hours. The feed’s own description mentions hours, but the published columns are space, block, meter type, rate type, rate range, time limit, and coordinates. When hours are missing, the app says so and still offers a cautious “if the meter is running, the time limit ends at …” deadline. It does not pretend every meter runs 8 AM to 8 PM.

Sweeping areas are blocks or corridors, not a curb side. The app says to check the sign.

### Holidays

From LADOT’s holiday page, meters, time limits, and no-parking / no-stopping rules that name days and hours are treated as not enforced on:

New Year’s Day, Martin Luther King Jr. Day, Presidents Day, Memorial Day, Juneteenth, Independence Day, Labor Day, Columbus Day (the second Monday in October), Veterans Day, Thanksgiving, and Christmas.

If the holiday is a Saturday, Friday is also an observed holiday. If it is a Sunday, Monday is also observed. Both the calendar day and the observed day are off.

Street sweeping is also off on **Cesar Chavez Day** (March 31, with the same weekend observation) and the **day after Thanksgiving**. Meters and time limits are still counted on those two days. LADOT’s page lists Cesar Chavez with the other holidays, and other write-ups say only sweeping stops. Skipping a real meter limit would cause a ticket, so the app keeps the limit and says why.

Red curbs and “no parking anytime” are never suspended. A sign that says **Holidays Enforced** can be entered in the sign form; those rules stay on.

After a holiday, check the spot again. A time limit can start the next morning, and the app does not carry yesterday’s free parking into today’s meter.

## Still not in the open data

These are placeholders. The screen says they are missing and the sign form is how you fill them in:

- **Meter hours.** Not in the inventory feed.
- **Tow-away and rush-hour zones.** No public curb geometry with hours was found. The LADOT “Regulatory Signs” layer is speed limits and turn signs, not parking plates. Do not assume a citywide 7–9 AM tow zone.
- **Red curbs / painted no-parking.** The layer named “LADOT Red Curb” on the city map server is a painting-program status map (years completed), not curb segments. If the curb is red, do not park.
- **Which side of the street**, and residential sweeping block by block. Posted routes describe an area in words (“Adams Blvd to Jefferson Blvd”), not a line you can match to a GPS point.
- **“Holidays Enforced”** and the exact permit hours. Those are on the sign.
- **Real-time meter occupancy.** Not used. This app answers the rule, not whether the space is empty.

## Alarms

Reminders are 15 minutes before, 5 minutes before, and at the deadline.

- **In the open app:** a countdown, a beep, and vibration where the phone allows it (iPhone Safari does not vibrate).
- **Web Push:** service worker `sw.js`, manifest, and a scheduler thread inside `server.py`. Works for a home-screen app on iOS 16.4+. The server sends the push; the phone must already have allowed notifications.
- **Calendar:** `GET /park/<id>.ics` with three `VALARM`s. This does not depend on the server staying awake.

Render’s free web service sleeps after a period with no traffic, and its disk is ephemeral. A push is sent only while the process is running, and parked sessions are lost on restart. The phone keeps the countdown in local storage and will save the spot again if the server forgot it. **Add to Calendar is the dependable alarm.**

### VAPID keys (do not commit them)

```bash
python vapid_keys.py
```

Put the three lines in the Render service environment:

- `VAPID_PUBLIC_KEY`
- `VAPID_PRIVATE_KEY` (one line, with `\n` between the PEM lines)
- `VAPID_SUBJECT` — a real `mailto:` address. Apple requires one.

`render.yaml` declares the names with `sync: false`, so a blueprint sync does not overwrite dashboard values. Redeploy after saving them. `GET /push/public-key` returns `configured: false` until both keys are set.

## Rules the evaluator already covers

Weekdays are Monday = 0. A rule with `start > end` runs past midnight. `no` beats `permit` beats `limit` when they overlap. A time limit stops when its own hours end, and the next deadline can be sweeping or a tow window. Tests in `test_backend.py` cover those cases plus sweeping weekdays, holiday observation (including a Saturday holiday’s Friday), Cesar Chavez, the day after Thanksgiving, a red curb that stays in force on July 4, sign entry, and the 15 / 5 / 0 reminders.

Posted signs always override this app. Data may be out of date.
