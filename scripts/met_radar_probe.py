#!/usr/bin/env python3
"""
met_radar_probe.py

Confirms Met Eireann's rainfall-radar image endpoint, finds the latest live
frame, saves it, and reports its dimensions so we can georeference it for the
LED map.

Verified endpoint pattern (from a working community scraper):
    https://www.met.ie/images/radar/web17_radar15_YYYYMMDDHHMM.png
Timestamps sit on the quarter hour and are treated as UTC here.

This also probes two speculative 5-minute filename guesses and simply reports
which, if any, respond. It does not assume they exist.

Run:   python3 met_radar_probe.py
Needs: Python 3.8+ standard library. Pillow is optional (for image size):
       pip install pillow
"""

import datetime as dt
import io
import os
import urllib.error
import urllib.request

BASE = "https://www.met.ie/images/radar"

# (filename_prefix, cadence_minutes). The 15-min entry is verified.
# The others are guesses at a possible 5-min set; the script only reports them.
PATTERNS = [
    ("web17_radar15_", 15),   # verified
    #("web17_radar05_", 5),     # speculative
    #("web17_radar_", 5),      # speculative
]

MAX_STEPS_BACK = 12  # intervals to walk back while hunting for a live frame
TIMEOUT = 15
RadarImageSubfolder = "0_RadarPNG"


def utc_floor(now, minutes):
    """Round a datetime down to the nearest `minutes` boundary."""
    discard = now.minute % minutes
    return now.replace(second=0, microsecond=0) - dt.timedelta(minutes=discard)


def try_url(url):
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "radar-probe/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return getattr(r, "status", r.getcode()), r.read()
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        print(f"    (error: {e})")
        return None, None


def probe():
    now = dt.datetime.now(dt.timezone.utc)
    print(f"UTC now: {now:%Y-%m-%d %H:%M}\n")
    found = []
    for prefix, step in PATTERNS:
        print(f"Testing pattern '{prefix}*' ({step}-min steps):")
        t = utc_floor(now, step)
        hit = None
        for _ in range(MAX_STEPS_BACK):
            stamp = t.strftime("%Y%m%d%H%M")
            url = f"{BASE}/{prefix}{stamp}.png"
            status, data = try_url(url)
            print(f"  {status}  {url}")
            if status == 200 and data:
                hit = (url, data)
                break
            t -= dt.timedelta(minutes=step)
        if hit:
            found.append((prefix, step, *hit))
        print()
    return found


def report(prefix, step, url, data):
    os.makedirs(RadarImageSubfolder, exist_ok=True)
    fname = url.rsplit("/", 1)[-1]
    path = os.path.join(RadarImageSubfolder, fname)
    with open(path, "wb") as f:
        f.write(data)
    print(f"LIVE  {url}")
    print(f"      saved as {path}  ({len(data)} bytes, ~{step}-min cadence)")
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        print(f"      image: {im.size[0]} x {im.size[1]} px, mode {im.mode}")
    except ImportError:
        print("      (install Pillow to read dimensions: pip install pillow)")
    except Exception as e:
        print(f"      (could not read image: {e})")
    print()


def main():
    results = probe()
    if not results:
        print(
            "No live frame on any pattern. The endpoint may have changed.\n"
            "Capture the real URL from met.ie in browser DevTools: open the\n"
            "radar map, F12 -> Network tab, filter 'radar' or 'png', and watch\n"
            "the request that loads as the map refreshes."
        )
        return
    print("=" * 60)
    for r in results:
        report(*r)
    print(
        "Next: send me the working URL, the image dimensions, and (if you can)\n"
        "upload the saved PNG. I'll georeference it and write the ESP32 firmware."
    )


if __name__ == "__main__":
    main()
