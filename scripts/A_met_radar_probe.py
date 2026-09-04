#!/usr/bin/env python3
"""
met_radar_probe.py

Fetches Met Eireann rainfall-radar frames and saves them locally so
ireland_radar_greyscale.py can process them.

Real endpoint (found via browser DevTools -> Network tab, not guessed):
    manifest: https://gdal.met.ie/api/maps/radar
        -> JSON list of available frames, each with a "src" timestamp
           (YYYYMMDDHHMM) and a "modifiedTime" cache token.
    tiles:    https://gdal.met.ie/api/maps/radar/{src}/{x}/{y}/{z}/{modifiedTime}
        -> one 256x256 PNG tile. Ireland is covered by a fixed 4x3 grid
           at zoom 7 (x: 59-62, y: 40-42), stitched together here into
           one 1024x768 image per frame.

The old https://www.met.ie/images/radar/web17_radar15_*.png endpoint is a
different, coarser product -- it visibly smooths away small, intense rain
cores that this tile endpoint preserves.

Run:   python3 met_radar_probe.py
Needs: pip install pillow certifi
"""

import io
import json
import os
import ssl
import urllib.error
import urllib.request

from PIL import Image

MANIFEST_URL = "https://gdal.met.ie/api/maps/radar"
TILE_URL = "https://gdal.met.ie/api/maps/radar/{src}/{x}/{y}/{z}/{token}"
TILE_ZOOM = 7
TILE_X0, TILE_X1 = 59, 62   # inclusive
TILE_Y0, TILE_Y1 = 40, 42   # inclusive
TILE_SIZE = 256

TIMEOUT = 15
RadarImageSubfolder = "0_RadarPNG"
GreyscaleRadarImageSubfolder = "1_GreyscalePNG"


def ListFilesInDirectory(Address):
    #list all files in a location
    from os import walk
    results = next(walk(Address), (None, None, []))[2]  # [] if no file
    return results


def _ssl_context():
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass  # fall back to the system store
    return ctx


def fetch_url(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "radar-probe/1.0", "Referer": "https://www.met.ie/"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl_context()) as r:
            return getattr(r, "status", r.getcode()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        print(f"    (error: {e})")
        return None, None


def fetch_frame_list():
    """The manifest of currently-available frames: each has 'src' (timestamp)
    and 'modifiedTime' (cache token), both required to fetch its tiles."""
    status, data = fetch_url(MANIFEST_URL)
    if status != 200 or not data:
        print(f"could not load frame manifest (status {status})")
        return []
    return json.loads(data)


def fetch_frame(src, token):
    """Download and stitch one frame's tile grid into a single RGB image."""
    mosaic = Image.new("RGB", (TILE_SIZE * (TILE_X1 - TILE_X0 + 1),
                                TILE_SIZE * (TILE_Y1 - TILE_Y0 + 1)))
    for ty in range(TILE_Y0, TILE_Y1 + 1):
        for tx in range(TILE_X0, TILE_X1 + 1):
            url = TILE_URL.format(src=src, x=tx, y=ty, z=TILE_ZOOM, token=token)
            status, data = fetch_url(url)
            if status != 200 or not data:
                print(f"    missing tile {tx},{ty} for {src} (status {status})")
                return None
            tile = Image.open(io.BytesIO(data)).convert("RGB")
            mosaic.paste(tile, ((tx - TILE_X0) * TILE_SIZE, (ty - TILE_Y0) * TILE_SIZE))
    return mosaic


def report(filename, frame):
    os.makedirs(RadarImageSubfolder, exist_ok=True)
    img = fetch_frame(frame["src"], frame["modifiedTime"])
    if img is None:
        print(f"FAILED  {frame.get('toolTipDate', frame['src'])}")
        return
    path = os.path.join(RadarImageSubfolder, filename)
    img.save(path)
    print(f"LIVE  {frame.get('toolTipDate', frame['src'])}")
    print(f"      saved as {filename}  {img.size[0]}x{img.size[1]} px, mode {img.mode}")
    print()


def main():
    frames = fetch_frame_list()
    if not frames:
        print("No frames in manifest. The endpoint may have changed.")
        return

    #list all images already loaded
    LoadedRadarImages = ListFilesInDirectory(RadarImageSubfolder)

    #list all those missing, keyed by the filename we save them as
    wanted = {f"{fr['src']}.png": fr for fr in frames}
    missing = sorted(set(wanted) - set(LoadedRadarImages))

    if not missing:
        print("No new frames to fetch.")
        return

    for filename in missing:
        report(filename, wanted[filename])


if __name__ == "__main__":
    main()
