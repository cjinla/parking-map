# Parking backend (Los Angeles)

    python ingest.py     # download LA meters + posted sweeping routes into parking.db (needs internet)
    python server.py     # serves http://localhost:8080/check?lat=34.1016&lng=-118.3387
    python test_backend.py

Python 3.9+, no third-party packages.

## What it returns
status (ok / limit / permit / no / unknown), a headline, `move_by` (ISO time or null), the rules used,
a confidence flag, and a disclaimer. Meter rules are "high" confidence (block-level, per space).
Sweeping rules are "low": the city publishes route areas, not curb sides, so the app should say "check the sign".

## Before launch
1. Run ingest.py once and print a few raw records. The parsers in ingest.py (meter hours, time limit, sweeping day/time)
   were written without access to the live data, so confirm the field names and adjust.
2. Not covered yet: preferential parking districts (dataset s3st-6nwi), rush-hour tow-away, red curbs, holidays,
   street-side matching, and sweeping outside posted routes (e.g. Downtown).
3. Hosting: see render.yaml (Render.com). Push these files to a GitHub repo, then create a Blueprint on Render pointing at it. The build runs ingest.py; redeploy to refresh the data.
