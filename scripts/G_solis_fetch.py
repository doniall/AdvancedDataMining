#!/usr/bin/env python3
"""
G_solis_fetch.py

Pulls key numbers from your SolisCloud account and writes them to
solis_status.json, so B_ireland_radar_greyscale.py can draw a stats strip
alongside the radar. If solis_status.json doesn't exist yet, the strip
just shows placeholders -- same "optional" pattern as ship_history.json.

Metrics fetched (all best-effort -- see NOTE ON FIELD NAMES):
    - PV power right now (kW)
    - household consumption right now (kW)
    - grid import/export right now (kW, signed)
    - battery state of charge (%)
    - today's production and consumption (kWh)
    - yesterday's production and consumption (kWh)
    - lifetime production (kWh)

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
    different accounts/API versions have been seen to disagree on the
    exact key for the same number -- e.g. one real captured stationDetail
    response used "eToday" for today's generation, while Solis's own bug
    tracker says "dayEnergy" is the one that doesn't reset early. "power"
    is the plant's rated capacity in kWp, NOT live output -- "pac" is.
    Rather than hard-code one guess and silently show nothing (or the
    wrong number) if it's wrong, this script tries a short list of known
    candidate keys per metric and keeps the full raw response in
    solis_status.json under "raw" -- open that file after your first real
    run to confirm the right keys got picked, and add to the candidate
    lists below if not.

    "Yesterday" isn't in stationDetail at all in the captures seen -- this
    script first tries a couple of direct candidate keys in case your
    account has them, then falls back to querying the stationDay
    time-series endpoint for yesterday's date and taking the day's final
    (highest) cumulative reading. If that turns out to be interval deltas
    rather than a running total on your account, flip DAY_ENERGY_IS_CUMULATIVE
    below to sum them instead.

    Grid import/export sign: "psum" was seen positive = exporting to the
    grid. If your account reports the opposite, flip GRID_POSITIVE_MEANS_EXPORT
    in B_ireland_radar_greyscale.py (that's where the sign turns into an
    Import/Export label, not here).
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
from zoneinfo import ZoneInfo

KEY_ID = os.environ.get("SOLIS_KEY_ID", "")
KEY_SECRET = os.environ.get("SOLIS_KEY_SECRET", "")
STATION_ID = os.environ.get("SOLIS_STATION_ID", "")
MONEY_CODE = os.environ.get("SOLIS_MONEY_CODE", "EUR")   # required by stationDay, doesn't affect energy values
LOCAL_TZ = ZoneInfo("Europe/Dublin")                      # matches B_ireland_radar_greyscale.py's own local day boundary

DOMAIN = "https://www.soliscloud.com:13333"
USER_STATION_LIST_PATH = "/v1/api/userStationList"
STATION_DETAIL_PATH = "/v1/api/stationDetail"
STATION_DAY_PATH = "/v1/api/stationDay"

# don't hit the API more often than this -- SolisCloud enforces its own
# rate limits, and inverter output doesn't change fast enough to need a
# fresh pull every time 0_Run_Radar_And_Greyscale.py fires
MIN_FETCH_INTERVAL_MINUTES = 5

# SolisCloud's authenticated endpoints (stationDetail/inverterDetail
# especially) are reported elsewhere as slow or intermittently flaky under
# load, independent of your own network -- confirmed here by curl getting
# an instant reply for an unsigned request (rejected before touching the
# backend) while a properly-signed stationDetail call hung past the old
# 20s timeout. One retry after a real timeout, or after Solis's own
# "Communication error ... try again later" response, covers that without
# hammering the API on a hard failure (bad signature, unknown station id).
REQUEST_TIMEOUT_SECONDS = 30
REQUEST_RETRIES = 1
REQUEST_RETRY_DELAY_SECONDS = 5

# see "Yesterday" note above
DAY_ENERGY_IS_CUMULATIVE = True

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "solis_status.json")
LAST_FETCH_PATH = os.path.join(HERE, "solis_last_fetch.txt")

# (value key, unit key) candidates, tried in order, for each metric --
# see NOTE ON FIELD NAMES above. "power" deliberately excluded from
# POWER_NOW_CANDIDATES -- confirmed to be rated capacity, not live output.
POWER_NOW_CANDIDATES = [
    ("pac", "pacUnit"),
    ("acPower", "acPowerUnit"),
]
CONSUMPTION_NOW_CANDIDATES = [
    ("familyLoadPower", "familyLoadPowerUnit"),
    ("homeLoadPower", "homeLoadPowerUnit"),
    ("loadPower", "loadPowerUnit"),
]
GRID_NOW_CANDIDATES = [
    ("psum", "psumUnit"),
    ("gridPower", "gridPowerUnit"),
]
BATTERY_SOC_CANDIDATES = [
    ("batteryCapacitySoc", None),
    ("batteryPercent", None),
    ("remainingCapacity", None),
]
TODAY_PRODUCTION_CANDIDATES = [
    ("dayEnergy", "dayEnergyUnit"),
    ("eToday", "eTodayUnit"),
    ("energyToday", "energyTodayUnit"),
]
TODAY_CONSUMPTION_CANDIDATES = [
    ("homeLoadTodayEnergy", "homeLoadTodayEnergyUnit"),
    ("consumeTodayEnergy", "consumeTodayEnergyUnit"),
]
YESTERDAY_PRODUCTION_CANDIDATES = [
    ("eYesterday", "eYesterdayUnit"),
    ("yesterdayEnergy", "yesterdayEnergyUnit"),
    ("dayEnergyLastDay", "dayEnergyLastDayUnit"),
]
YESTERDAY_CONSUMPTION_CANDIDATES = [
    ("homeLoadYesterdayEnergy", "homeLoadYesterdayEnergyUnit"),
    ("consumeYesterdayEnergy", "consumeYesterdayEnergyUnit"),
]
TOTAL_ENERGY_CANDIDATES = [
    ("allEnergy", "allEnergyUnit"),
    ("eTotal", "eTotalUnit"),
    ("energyTotalLife", "energyTotalLifeUnit"),
    ("totalEnergy", "totalEnergyUnit"),
]

# stationDay time-series record field candidates for the yesterday fallback
DAY_RECORD_PRODUCE_CANDIDATES = ["produceEnergy", "energy", "eToday"]
DAY_RECORD_CONSUME_CANDIDATES = ["consumeEnergy", "useEnergy", "homeLoadEnergy"]


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
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read())


def _call(path, payload):
    """_sign_and_post, but retries once on a network-level failure or on
    Solis's own retryable "code" != success -- see REQUEST_RETRIES above."""
    last_error = None
    for attempt in range(REQUEST_RETRIES + 1):
        try:
            resp = _sign_and_post(path, payload)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
        else:
            if str(resp.get("code")) in ("0", "0000"):
                return resp
            last_error = RuntimeError(f"{path} returned {resp.get('code')}: {resp.get('msg')}")
        if attempt < REQUEST_RETRIES:
            print(f"SolisCloud call to {path} failed ({last_error}); retrying in {REQUEST_RETRY_DELAY_SECONDS}s...")
            time.sleep(REQUEST_RETRY_DELAY_SECONDS)
    raise last_error


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
    resp = _call(USER_STATION_LIST_PATH, {"pageNo": 1, "pageSize": 10})
    candidates = []
    _find_dicts_with_key(resp.get("data"), "id", candidates)
    if not candidates:
        raise RuntimeError("no station found in userStationList response -- set SOLIS_STATION_ID instead")
    return candidates[0]["id"]


def fetch_station_detail(station_id):
    resp = _call(STATION_DETAIL_PATH, {"id": station_id})
    data = resp.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("stationDetail response had no usable 'data' object")
    return data


def fetch_day_totals(station_id, date_str):
    """Best-effort fallback for a specific past date's production/consumption,
    via the stationDay time-series endpoint -- see "Yesterday" note above.
    Unlike fetch_station_detail, failure here isn't fatal to the whole run,
    so it swallows its own errors (after _call's retry) and returns
    (None, None) rather than raising."""
    try:
        resp = _call(STATION_DAY_PATH, {"id": station_id, "money": MONEY_CODE, "timezone": 0, "time": date_str})
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
        print(f"stationDay fetch for {date_str} failed ({e})")
        return None, None
    records = resp.get("data")
    if not isinstance(records, list) or not records:
        return None, None

    def _series(candidates):
        for key in candidates:
            values = [float(r[key]) for r in records if isinstance(r, dict) and r.get(key) is not None]
            if values:
                return max(values) if DAY_ENERGY_IS_CUMULATIVE else sum(values)
        return None

    return _series(DAY_RECORD_PRODUCE_CANDIDATES), _series(DAY_RECORD_CONSUME_CANDIDATES)


def _pick(data, candidates):
    for value_key, unit_key in candidates:
        if value_key in data and data[value_key] is not None:
            try:
                value = float(data[value_key])
            except (TypeError, ValueError):
                continue
            return value, (data.get(unit_key) if unit_key else None)
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
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, ValueError, KeyError) as e:
        print(f"SolisCloud fetch failed ({e}); leaving existing solis_status.json in place")
        return

    power_kw, power_unit = _pick(data, POWER_NOW_CANDIDATES)
    consumption_kw, consumption_unit = _pick(data, CONSUMPTION_NOW_CANDIDATES)
    grid_kw, grid_unit = _pick(data, GRID_NOW_CANDIDATES)
    battery_pct, _ = _pick(data, BATTERY_SOC_CANDIDATES)
    today_kwh, today_unit = _pick(data, TODAY_PRODUCTION_CANDIDATES)
    today_consumption_kwh, today_consumption_unit = _pick(data, TODAY_CONSUMPTION_CANDIDATES)
    yesterday_kwh, _ = _pick(data, YESTERDAY_PRODUCTION_CANDIDATES)
    yesterday_consumption_kwh, _ = _pick(data, YESTERDAY_CONSUMPTION_CANDIDATES)
    total_kwh, total_unit = _pick(data, TOTAL_ENERGY_CANDIDATES)

    if yesterday_kwh is None or yesterday_consumption_kwh is None:
        yesterday_date = (dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)).isoformat()
        try:
            day_produce, day_consume = fetch_day_totals(station_id, yesterday_date)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            print(f"stationDay fallback for yesterday's totals failed ({e})")
            day_produce, day_consume = None, None
        if yesterday_kwh is None:
            yesterday_kwh = day_produce
        if yesterday_consumption_kwh is None:
            yesterday_consumption_kwh = day_consume

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
        "consumption_kw": consumption_kw,
        "consumption_unit": consumption_unit,
        "grid_kw": grid_kw,
        "grid_unit": grid_unit,
        "battery_pct": battery_pct,
        "today_kwh": today_kwh,
        "today_unit": today_unit,
        "today_consumption_kwh": today_consumption_kwh,
        "today_consumption_unit": today_consumption_unit,
        "yesterday_kwh": yesterday_kwh,
        "yesterday_consumption_kwh": yesterday_consumption_kwh,
        "total_kwh": total_kwh,
        "total_unit": total_unit,
        "raw": data,
    }

    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)
    with open(LAST_FETCH_PATH, "w") as f:
        f.write(status["fetched_at"])

    print(f"SolisCloud: {power_kw} kW now, {today_kwh} kWh today, battery {battery_pct}%")


if __name__ == "__main__":
    main()
