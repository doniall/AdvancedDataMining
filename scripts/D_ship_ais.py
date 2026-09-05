#!/usr/bin/env python3
"""
D_ship_ais.py

Fetches live ship positions (AIS) in Irish coastal waters and appends them
to ship_history.json -- a per-ship log of recent positions -- so
B_ireland_radar_greyscale.py can draw both a ship's current position and a
trail back to where it was ~15 minutes earlier.

Run this repeatedly (e.g. every few minutes, alongside A_met_radar_probe.py)
so the history actually has a ~15-minute-old sample to draw a trail from --
a single one-off run only ever has "now", so no trail will show until it's
been running for a while. Calling it more often than that doesn't help
though: main() no-ops unless at least MIN_FETCH_INTERVAL_MINUTES has passed
since the last attempt, so it's safe to call from a tighter loop than you
actually want to hit aisstream.io with. Each successful run appends one
snapshot per ship; a ship's most recent position is kept for
HISTORY_RETENTION_MINUTES after it was last seen, then pruned.

AIS ("Automatic Identification System") is the shipborne transponder itself --
every commercial/large vessel broadcasts its own GPS position over VHF. This
script uses aisstream.io, a free aggregator that relays that broadcast from a
network of volunteer-run coastal receivers. That means coverage is good in
shipping lanes and near the coast but thins out well offshore, since it
depends on a receiver being in VHF range -- there's no free way around that.
Full-ocean coverage means satellite AIS, which is a paid commercial data feed
(Spire, exactEarth, MarineTraffic's satellite tier), priced for shipping and
insurance companies rather than a home project.

Needs a free API key from https://aisstream.io (sign up, key emailed).
Set it as the AISSTREAM_API_KEY environment variable, or paste it into
API_KEY below.

Run:   python3 D_ship_ais.py
Needs: pip install websockets
"""

import asyncio
import datetime as dt
import json
import os
import time

try:
    import websockets
except ImportError:
    websockets = None

AISSTREAM_URL = "wss://stream.aisstream.io/v0/stream"
API_KEY = os.environ.get("AISSTREAM_API_KEY", "")

# south-west and north-east corners of the box to watch -- wide enough to
# cover Irish coastal waters and the shipping lanes in and out of them.
# Widen or narrow this to taste; it doesn't need to match the rendered
# view exactly -- ships outside the frame just won't show.
BOUNDING_BOX = [[[49.5, -11.5], [56.0, -3.5]]]

LISTEN_SECONDS = 60   # how long to sit on the stream before saving what arrived --
                       # meant to be run independently (its own cron/scheduler
                       # entry) every MIN_FETCH_INTERVAL_MINUTES, not called from
                       # 0_Run_Radar_And_Greyscale.py's own cadence

# a ship's last recorded position is kept for this long after it was last
# seen, then pruned -- still comfortably past the 15-minute trail
# B_ireland_radar_greyscale.py draws, and any backlog of unrendered frames
HISTORY_RETENTION_MINUTES = 120

# don't hit aisstream.io more often than this -- calling main() more
# frequently (e.g. from a tight cron loop or every time 0_Run_* fires) just
# no-ops until enough time has passed since the last attempt
MIN_FETCH_INTERVAL_MINUTES = 15

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_history.json")
LAST_FETCH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_ais_last_fetch.txt")


def _handle_message(raw, ships):
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    if msg.get("MessageType") != "PositionReport":
        return
    report = msg.get("Message", {}).get("PositionReport", {})
    meta = msg.get("MetaData", {})
    mmsi = meta.get("MMSI", report.get("UserID"))
    if mmsi is None:
        return
    ships[mmsi] = {
        "name": (meta.get("ShipName") or "").strip(),
        "lat": report.get("Latitude"),
        "lon": report.get("Longitude"),
        "heading": report.get("TrueHeading"),   # 511 = not available
        "cog": report.get("Cog"),               # course over ground, degrees
    }


async def collect():
    """Subscribe to the bounding box and collect position reports for
    LISTEN_SECONDS, keeping only the latest report per ship (by MMSI)."""
    ships = {}
    deadline = time.monotonic() + LISTEN_SECONDS
    try:
        async with websockets.connect(AISSTREAM_URL) as ws:
            await ws.send(json.dumps({
                "APIKey": API_KEY,
                "BoundingBoxes": BOUNDING_BOX,
                "FilterMessageTypes": ["PositionReport"],
            }))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                _handle_message(raw, ships)
    except Exception as e:
        print(f"    (error: {e})")
    return ships


def _load_history():
    if not os.path.exists(HISTORY_PATH):
        return {}
    with open(HISTORY_PATH) as f:
        return json.load(f)


def _prune(history, now):
    """Drop samples older than HISTORY_RETENTION_MINUTES, and any ship left
    with none -- keeps the file from growing without bound."""
    cutoff = now - dt.timedelta(minutes=HISTORY_RETENTION_MINUTES)
    for mmsi in list(history):
        kept = [p for p in history[mmsi] if dt.datetime.fromisoformat(p["t"]) >= cutoff]
        if kept:
            history[mmsi] = kept
        else:
            del history[mmsi]
    return history


def _last_fetch_time():
    if not os.path.exists(LAST_FETCH_PATH):
        return None
    with open(LAST_FETCH_PATH) as f:
        s = f.read().strip()
    return dt.datetime.fromisoformat(s) if s else None


def _record_fetch_time(t):
    with open(LAST_FETCH_PATH, "w") as f:
        f.write(t.isoformat())


def main():
    if websockets is None:
        print("The 'websockets' package isn't installed -- run: pip3 install websockets")
        return
    if not API_KEY:
        print("No AISSTREAM_API_KEY set -- sign up for a free key at https://aisstream.io "
              "and set it as an environment variable (or paste it into API_KEY in this "
              "file). Nothing fetched; rendering will proceed without ship markers.")
        return

    last_fetch = _last_fetch_time()
    if last_fetch is not None:
        elapsed = (dt.datetime.now(dt.timezone.utc) - last_fetch).total_seconds() / 60
        if elapsed < MIN_FETCH_INTERVAL_MINUTES:
            print(f"last AIS fetch was {elapsed:.1f} min ago -- minimum cadence is "
                  f"{MIN_FETCH_INTERVAL_MINUTES} min, skipping this run")
            return

    print(f"listening for AIS position reports for {LISTEN_SECONDS}s...")
    ships = asyncio.run(collect())

    # timestamp every ship caught in this run with receipt time, rather than
    # trying to parse aisstream's own reported time_utc string -- receipt
    # lag is seconds, negligible against the 15-minute trail this feeds
    now = dt.datetime.now(dt.timezone.utc)
    stamp = now.isoformat()
    _record_fetch_time(now)   # recorded even on a connection failure (ships
                               # empty) -- a broken key/network won't retry
                               # more often than the same 15-min cadence

    history = _load_history()
    for mmsi, ship in ships.items():
        entry = dict(ship, t=stamp)
        history.setdefault(str(mmsi), []).append(entry)
    history = _prune(history, now)

    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2)

    n_samples = sum(len(v) for v in history.values())
    print(f"recorded {len(ships)} ship position(s) this run; history now holds "
          f"{n_samples} samples across {len(history)} ships")


if __name__ == "__main__":
    main()
