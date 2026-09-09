#!/usr/bin/env python3
"""
G_solis_fetch.py

Pulls key numbers from your SolisCloud account (current PV output, today's
generation, lifetime generation) and writes them to solis_status.json, so
B_ireland_radar_greyscale.py can draw them onto the e-ink image alongside
the radar. If solis_status.json doesn't exist yet, the solar block is just
skipped -- same "optional" pattern as ship_history.json.

SETUP
    1. Log into https://www.soliscloud.com, then Service Management ->
       API Management and apply for API access. Solis has to approve this
       (sometimes takes a day); you get back a Key ID and Key Secret.
    2. Set SOLIS_KEY_ID and SOLIS_KEY_SECRET as environment variables.
       Treat the secret like the AIS API key / VM client secret elsewhere
       in this repo -- don't commit it, don't paste it into chat.
    3. Optional: set SOLIS_STATION_ID to the numeric station ID shown in
       the SolisCloud portal URL when you open your plant. If you leave it
       unset, this script asks userStationList for your first station and
       uses that -- fine for a single-site account, but pin it explicitly
       if you ever have more than one.

Run:   python3 G_solis_fetch.py
Needs: nothing beyond the standard library.

NOTE ON FIELD NAMES
    SolisCloud's stationDetail response isn't consistently documented, and
    different accounts/regions have been seen to disagree on the exact key
    for the same number (e.g. "dayEnergy" vs "energyToday"). Rather than
    hard-code one guess and silently show nothing (or crash) if it's wrong,
    this script tries a short list of known candidate keys for each metric
    and keeps the full raw response in solis_status.json under "raw" --
    open that file after your first real run to confirm the right keys got
    picked, and add to the candidate lists below if not.
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from email.utils import formatdate

KEY_ID = os.environ.get("SOLIS_KEY_ID", "")
KEY_SECRET = os.environ.get("SOLIS_KEY_SECRET", "")
STATION_ID = os.environ.get("SOLIS_STATION_ID", "")

DOMAIN = "https://www.soliscloud.com:13333"
USER_STATION_LIST_PATH = "/v1/api/userStationList"
STATION_DETAIL_PATH = "/v1/api/stationDetail"

# don't hit the API more often than this -- SolisCloud enforces its own
# rate limits, and inverter output doesn't change fast enough to need a
# fresh pull every time 0_Run_Radar_And_Greyscale.py fires
MIN_FETCH_INTERVAL_MINUTES = 5

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "solis_status.json")
LAST_FETCH_PATH = os.path.join(HERE, "solis_last_fetch.txt")

# (value key, unit key) candidates, tried in order, for each metric --
# see NOTE ON FIELD NAMES above
POWER_CANDIDATES = [
    ("power", "powerUnit"),
    ("pac", "pacUnit"),
    ("acPower", "acPowerUnit"),
]
TODAY_ENERGY_CANDIDATES = [
    ("dayEnergy", "dayEnergyUnit"),
    ("energyToday", "energyTodayUnit"),
    ("todayEnergy", "todayEnergyUnit"),
]
TOTAL_ENERGY_CANDIDATES = [
    ("allEnergy", "allEnergyUnit"),
    ("energyTotalLife", "energyTotalLifeUnit"),
    ("totalEnergy", "totalEnergyUnit"),
]


def _sign_and_post(path, payload):
    body = json.dumps(payload, separators=(",", ":")).encode()
    content_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
    content_type = "application/json"
    date = formatdate(timeval=None, localtime=False, usegmt=True)

    string_to_sign = f"POST\n{content_md5}\n{content_type}\n{date}\n{path}"
    signature = base64.b64encode(
        hmac.new(KEY_SECRET.encode(), string_to_sign.encode(), hashlib.sha1).digest()
    ).decode()

    headers = {
        "Content-MD5": content_md5,
        "Content-Type": content_type,
        "Date": date,
        "Authorization": f"API {KEY_ID}:{signature}",
    }
    req = urllib.request.Request(DOMAIN + path, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def _find_dicts_with_key(obj, key, out):
    """Walk an arbitrarily-nested API response looking for dicts that have
    `key` -- used to locate the station list's records without depending on
    exactly which pagination wrapper SolisCloud nests them in."""
    if isinstance(obj, dict):
        if key in obj:
            out.append(obj)
        for v in obj.values():
            _find_dicts_with_key(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _find_dicts_with_key(v, key, out)


def discover_station_id():
    resp = _sign_and_post(USER_STATION_LIST_PATH, {"pageNo": 1, "pageSize": 10})
    if str(resp.get("code")) not in ("0", "0000"):
        raise RuntimeError(f"userStationList returned {resp.get('code')}: {resp.get('msg')}")
    candidates = []
    _find_dicts_with_key(resp.get("data"), "id", candidates)
    if not candidates:
        raise RuntimeError("no station found in userStationList response -- set SOLIS_STATION_ID instead")
    return candidates[0]["id"]


def fetch_station_detail(station_id):
    resp = _sign_and_post(STATION_DETAIL_PATH, {"id": station_id})
    if str(resp.get("code")) not in ("0", "0000"):
        raise RuntimeError(f"stationDetail returned {resp.get('code')}: {resp.get('msg')}")
    data = resp.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("stationDetail response had no usable 'data' object")
    return data


def _pick(data, candidates):
    for value_key, unit_key in candidates:
        if value_key in data and data[value_key] is not None:
            try:
                value = float(data[value_key])
            except (TypeError, ValueError):
                continue
            return value, data.get(unit_key)
    return None, None


def _due():
    if not os.path.exists(LAST_FETCH_PATH):
        return True
    age_minutes = (time.time() - os.path.getmtime(LAST_FETCH_PATH)) / 60
    return age_minutes >= MIN_FETCH_INTERVAL_MINUTES


def main():
    if not (KEY_ID and KEY_SECRET):
        print("SOLIS_KEY_ID / SOLIS_KEY_SECRET not set -- skipping SolisCloud fetch "
              "(see the setup notes at the top of this file)")
        return

    if not _due():
        print(f"last SolisCloud fetch was under {MIN_FETCH_INTERVAL_MINUTES} min ago -- skipping")
        return

    try:
        station_id = STATION_ID or discover_station_id()
        data = fetch_station_detail(station_id)
    except (urllib.error.URLError, urllib.error.HTTPError, RuntimeError, ValueError, KeyError) as e:
        print(f"SolisCloud fetch failed ({e}); leaving existing solis_status.json in place")
        return

    power_kw, power_unit = _pick(data, POWER_CANDIDATES)
    today_kwh, today_unit = _pick(data, TODAY_ENERGY_CANDIDATES)
    total_kwh, total_unit = _pick(data, TOTAL_ENERGY_CANDIDATES)

    if power_kw is None and today_kwh is None:
        known = ", ".join(sorted(data.keys()))
        print("warning: none of the expected power/energy keys were found in stationDetail; "
              f"actual keys were: {known}. Add the right one to G_solis_fetch.py's candidate "
              "lists -- the raw response is saved in solis_status.json either way.")

    status = {
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "station_id": station_id,
        "power_kw": power_kw,
        "power_unit": power_unit,
        "today_kwh": today_kwh,
        "today_unit": today_unit,
        "total_kwh": total_kwh,
        "total_unit": total_unit,
        "raw": data,
    }

    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)
    with open(LAST_FETCH_PATH, "w") as f:
        f.write(status["fetched_at"])

    print(f"SolisCloud: {power_kw} {power_unit or 'kW'} now, {today_kwh} {today_unit or 'kWh'} today")


if __name__ == "__main__":
    main()
