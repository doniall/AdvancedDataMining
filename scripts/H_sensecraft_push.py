#!/usr/bin/env python3
"""
H_sensecraft_push.py

Pushes the latest Solis snapshot (solis_device.json, written by
G_solis_fetch.py) AND the latest IMAGE_PUSH_COUNT rendered greyscale radar
frames (1_GreyscalePNG/, written by B_ireland_radar_greyscale.py) straight
to SenseCraft's own cloud API, instead of relying on the E1003's
"External API Configuration" widget to pull either from somewhere we host.
Push doesn't need solis_device.json or the greyscale PNGs to sit anywhere
internet-reachable at a stable URL; push doesn't -- whatever machine runs
this pipeline (this laptop, a Pi, a GitHub Action) just POSTs the data to
SenseCraft directly, and the device fetches its widget data from
SenseCraft's own servers instead of from us.

SETUP
    In the SenseCraft HMI dashboard designer, add the widget and choose
    "Push to Sensecraft" instead of "External API Configuration". It gives
    you an api-key and a device_id -- set those as SENSECRAFT_API_KEY and
    SENSECRAFT_DEVICE_ID environment variables. Treat the API key like every
    other credential in this repo: don't commit it, don't paste it into
    chat. After the first successful push, use the dashboard's own
    "Test & Load Fields" button to pick which pushed fields each widget
    should display -- Solis numbers (power_kw, battery_pct,
    weather_condition, ...) as text/number fields, image_1..image_N as
    image fields (the dashboard lets a pushed field be categorised as
    either).

    SenseCraft's own `data` object is a flat key/value map (their own
    example: temperature/humidity/pressure) -- it isn't documented as
    supporting nested objects, so solis_device.json's nested "weather"
    dict is flattened to weather_* keys below rather than sent as-is.

IMAGES -- UNCONFIRMED FORMAT
    The dashboard has an image widget and lets a pushed field be tagged as
    an image, but there's no reachable documentation (sensecraft-hmi-docs.
    seeed.cc and wiki.seeedstudio.com are both blocked from where this was
    written) confirming what a pushed image VALUE needs to look like --
    plain base64, or a data: URI. This defaults to plain base64
    (IMAGE_AS_DATA_URI = False below); if the widget doesn't render after
    a real push, flip that flag and try again -- that's the one thing to
    change, nothing else about the field layout.

    Field names are stable across pushes -- image_1 (most recent) through
    image_N (oldest of the N kept) -- so a widget bound to e.g. image_1 in
    the dashboard keeps working every run; only the values rotate as new
    frames replace old ones. image_N_time is that frame's own UTC
    timestamp (parsed from its filename), for confirming freshness.

    Full E1003-resolution PNGs (1872x1404) run several hundred KB each, so
    IMAGE_PUSH_COUNT of them base64-encoded into one POST can be multiple
    MB. SenseCraft doesn't document a size limit for push_data, so
    MAX_PUSH_PAYLOAD_WARN_BYTES below is a log-only tripwire, not a hard
    cap -- if a push actually gets rejected for size, lower
    IMAGE_PUSH_COUNT.

Run:   python3 H_sensecraft_push.py
Needs: nothing beyond the standard library.
"""

import base64
import json
import os
import urllib.error
import urllib.request

API_KEY = os.environ.get("SENSECRAFT_API_KEY", "")
DEVICE_ID = os.environ.get("SENSECRAFT_DEVICE_ID", "")

PUSH_URL = "https://sensecraft-hmi-api.seeed.cc/api/v1/user/device/push_data"
REQUEST_TIMEOUT_SECONDS = 15

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_PATH = os.path.join(HERE, "solis_device.json")   # written by G_solis_fetch.py
# same literal B_ireland_radar_greyscale.py uses for GreyscaleRadarImageSubfolder --
# duplicated here rather than imported, to avoid pulling in that whole module
# (PIL, coastline/counties loading) just for a folder name
GREYSCALE_DIR = os.path.join(HERE, "1_GreyscalePNG")
LAST_PUSHED_PATH = os.path.join(HERE, "sensecraft_last_pushed.json")

IMAGE_PUSH_COUNT = 20
IMAGE_AS_DATA_URI = False   # see IMAGES -- UNCONFIRMED FORMAT above
MAX_PUSH_PAYLOAD_WARN_BYTES = 4 * 1024 * 1024


def _flatten(device_view):
    """solis_device.json is already flat except for its "weather" dict --
    pull that out to weather_* keys, and drop None values (a live SolisCloud
    field candidate that came back empty), since a null in SenseCraft's
    field selector isn't something their "Test & Load Fields" flow is known
    to handle."""
    flat = {}
    for key, value in device_view.items():
        if key == "weather" and isinstance(value, dict):
            for wkey, wvalue in value.items():
                if wvalue is not None:
                    flat[f"weather_{wkey}"] = wvalue
        elif value is not None:
            flat[key] = value
    return flat


def _latest_greyscale_filenames(n=IMAGE_PUSH_COUNT):
    """Filenames are UTC timestamp stems (see B_ireland_radar_greyscale.py's
    own "src" timestamp comment), so a plain lexicographic sort is
    chronological -- same assumption that script's own frame-diffing already
    relies on. Returns the n most recent, newest first."""
    if not os.path.isdir(GREYSCALE_DIR):
        return []
    names = sorted(f for f in os.listdir(GREYSCALE_DIR) if f.lower().endswith(".png"))
    return list(reversed(names[-n:]))


def _encode_image(path):
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/png;base64,{b64}" if IMAGE_AS_DATA_URI else b64


def _image_fields(filenames):
    """Builds image_1.._N (+ _time) fields -- see IMAGES -- UNCONFIRMED
    FORMAT above for the field-naming and encoding rationale."""
    fields = {}
    for i, filename in enumerate(filenames, start=1):
        path = os.path.join(GREYSCALE_DIR, filename)
        try:
            fields[f"image_{i}"] = _encode_image(path)
        except OSError as e:
            print(f"couldn't read {filename} for SenseCraft push ({e}); skipping it")
            continue
        fields[f"image_{i}_time"] = os.path.splitext(filename)[0]
    return fields


def _load_last_pushed():
    if not os.path.exists(LAST_PUSHED_PATH):
        return {}
    try:
        with open(LAST_PUSHED_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_last_pushed(fetched_at, image_filenames):
    with open(LAST_PUSHED_PATH, "w") as f:
        json.dump({"fetched_at": fetched_at, "image_filenames": image_filenames}, f)


def push(data):
    """POST one combined reading to SenseCraft. Raises on failure -- callers
    (main() here, or 0_Run_Radar_And_Greyscale.py) decide whether that's
    fatal to the overall run."""
    payload = {"device_id": int(DEVICE_ID), "data": data}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        PUSH_URL,
        data=body,
        headers={"api-key": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def main():
    if not (API_KEY and DEVICE_ID):
        print("SENSECRAFT_API_KEY / SENSECRAFT_DEVICE_ID not set -- skipping SenseCraft push "
              "(see the setup notes at the top of this file)")
        return

    device_view = {}
    if os.path.exists(DEVICE_PATH):
        with open(DEVICE_PATH) as f:
            device_view = json.load(f)
    else:
        print("solis_device.json not found -- pushing images only, if any "
              "(run G_solis_fetch.py for the Solis panel data)")

    image_filenames = _latest_greyscale_filenames()
    if not device_view and not image_filenames:
        print("nothing to push yet -- no solis_device.json and no images in "
              "1_GreyscalePNG/ (run G_solis_fetch.py and/or B_ireland_radar_greyscale.py first)")
        return

    fetched_at = device_view.get("fetched_at")
    last_pushed = _load_last_pushed()
    if (fetched_at and fetched_at == last_pushed.get("fetched_at")
            and image_filenames == last_pushed.get("image_filenames")):
        print("SenseCraft already has this data (nothing new since the last push) -- skipping")
        return

    data = _flatten(device_view)
    data.update(_image_fields(image_filenames))

    payload_size = len(json.dumps(data))
    if payload_size > MAX_PUSH_PAYLOAD_WARN_BYTES:
        print(f"warning: this push is {payload_size / 1024 / 1024:.1f} MB -- SenseCraft doesn't "
              "document a size limit for push_data, so this may get rejected. Lower IMAGE_PUSH_COUNT "
              "if it does.")

    try:
        resp = push(data)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"SenseCraft push failed ({e})")
        return

    _save_last_pushed(fetched_at, image_filenames)
    print(f"pushed {len(data)} field(s) to SenseCraft (device {DEVICE_ID}), "
          f"including {len(image_filenames)} image(s): {resp}")


if __name__ == "__main__":
    main()
