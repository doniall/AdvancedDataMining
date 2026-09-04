#!/usr/bin/env python3
"""
D_ship_ais.py

Fetches live ship positions (AIS) in Irish coastal waters and saves them to
ship_positions.json so B_ireland_radar_greyscale.py can draw them as
markers on the rendered map.

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

LISTEN_SECONDS = 45   # how long to sit on the stream before saving what arrived
OUTPUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_positions.json")


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
        "mmsi": mmsi,
        "name": (meta.get("ShipName") or "").strip(),
        "lat": report.get("Latitude"),
        "lon": report.get("Longitude"),
        "heading": report.get("TrueHeading"),   # 511 = not available
        "cog": report.get("Cog"),               # course over ground, degrees
        "time_utc": meta.get("time_utc"),
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
    return list(ships.values())


def main():
    if websockets is None:
        print("The 'websockets' package isn't installed -- run: pip3 install websockets")
        return
    if not API_KEY:
        print("No AISSTREAM_API_KEY set -- sign up for a free key at https://aisstream.io "
              "and set it as an environment variable (or paste it into API_KEY in this "
              "file). Nothing fetched; rendering will proceed without ship markers.")
        return

    print(f"listening for AIS position reports for {LISTEN_SECONDS}s...")
    ships = asyncio.run(collect())
    with open(OUTPUT_PATH, "w") as f:
        json.dump(ships, f, indent=2)
    print(f"saved {len(ships)} ship position(s) to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
