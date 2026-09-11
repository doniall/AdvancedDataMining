#!/usr/bin/env python3
"""
H_sensecraft_push.py

Pushes the latest Solis snapshot (solis_device.json, written by
G_solis_fetch.py) straight to SenseCraft's own cloud API, instead of
relying on the E1003's "External API Configuration" widget to pull it from
somewhere we host. This is the other half of the same problem: that pull
approach needs solis_device.json to sit somewhere internet-reachable at a
stable URL; push doesn't -- whatever machine runs this pipeline (this
laptop, a Pi, a GitHub Action) just POSTs the numbers to SenseCraft
directly, and the device fetches its widget data from SenseCraft's own
servers instead of from us. Either mechanism can be used for the Solis
panel; this one avoids needing to self-host anything reachable from the
device.

SETUP
    In the SenseCraft HMI dashboard designer, add the widget and choose
    "Push to Sensecraft" instead of "External API Configuration". It gives
    you an api-key and a device_id -- set those as SENSECRAFT_API_KEY and
    SENSECRAFT_DEVICE_ID environment variables. Treat the API key like every
    other credential in this repo: don't commit it, don't paste it into
    chat. After the first successful push, use the dashboard's own
    "Test & Load Fields" button to pick which pushed fields (e.g.
    power_kw, battery_pct, weather_condition) each widget should display.

    SenseCraft's own `data` object is a flat key/value map (their own
    example: temperature/humidity/pressure) -- it isn't documented as
    supporting nested objects, so solis_device.json's nested "weather"
    dict is flattened to weather_* keys below rather than sent as-is.

Run:   python3 H_sensecraft_push.py
Needs: nothing beyond the standard library.
"""

import datetime as dt
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
LAST_PUSHED_PATH = os.path.join(HERE, "sensecraft_last_pushed.json")


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


def _last_pushed_fetched_at():
    if not os.path.exists(LAST_PUSHED_PATH):
        return None
    try:
        with open(LAST_PUSHED_PATH) as f:
            return json.load(f).get("fetched_at")
    except (json.JSONDecodeError, OSError):
        return None


def _record_pushed(fetched_at):
    with open(LAST_PUSHED_PATH, "w") as f:
        json.dump({"fetched_at": fetched_at}, f)


def push(data):
    """POST one flattened reading to SenseCraft. Raises on failure -- callers
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

    if not os.path.exists(DEVICE_PATH):
        print("solis_device.json not found -- nothing to push yet (run G_solis_fetch.py first)")
        return

    with open(DEVICE_PATH) as f:
        device_view = json.load(f)

    fetched_at = device_view.get("fetched_at")
    if fetched_at and fetched_at == _last_pushed_fetched_at():
        print("SenseCraft already has this reading (nothing new since the last push) -- skipping")
        return

    flat = _flatten(device_view)
    try:
        resp = push(flat)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        print(f"SenseCraft push failed ({e})")
        return

    if fetched_at:
        _record_pushed(fetched_at)
    print(f"pushed {len(flat)} field(s) to SenseCraft (device {DEVICE_ID}): {resp}")


if __name__ == "__main__":
    main()
