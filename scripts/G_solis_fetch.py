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
    - today's production, consumption, and grid export (kWh)
    - yesterday's production, consumption, and grid export (kWh)
    - lifetime production (kWh)

SETUP
    1. Log into https://www.soliscloud.com, then Service Management ->
       API Management and apply for API access. Solis has to approve this
       (sometimes takes a day); you get back a Key ID and Key Secret.
    2. Set SOLIS_KEY_ID and SOLIS_KEY_SECRET as environment variables.
       Treat the secret like the AIS API key / VM client secret elsewhere
       in this repo -- don't commit it, don't paste it into chat.
    3. Set SOLIS_STATION_ID to the numeric station ID shown in the
       SolisCloud portal URL when you open your plant (also printed by this
       script on a successful run, or found by searching solis_status.json's
       "raw" section for a matching "id"). If you leave it unset, this
       script asks userStationList for your first station and uses that --
       fine for a single-site account, but every run then pays for an extra
       API round-trip to re-discover an ID that never changes, so set it
       once you know it.

Run:   python3 G_solis_fetch.py
Needs: nothing beyond the standard library.

RETRIES AND BACKOFF
    A failed request is retried once immediately (REQUEST_RETRIES). If
    SolisCloud keeps failing across separate runs of this script (e.g. it's
    called every few minutes from 0_Run_Radar_And_Greyscale.py), the wait
    before trying again grows via BACKOFF_SCHEDULE_MINUTES instead of
    hammering an unhealthy backend on every cycle -- state for this lives in
    solis_fetch_state.json, separate from solis_status.json (which only
    updates on success, so the display always shows the last real data).

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

# don't hit the API more often than this when things are healthy --
# SolisCloud enforces its own rate limits, and inverter output doesn't
# change fast enough to need a fresh pull every time
# 0_Run_Radar_And_Greyscale.py fires. See BACKOFF_SCHEDULE_MINUTES below for
# what happens when it's NOT healthy.
MIN_FETCH_INTERVAL_MINUTES = 5

# after N consecutive failed attempts (network timeout, 502, or Solis's own
# "try again later"), wait this many minutes before trying again, instead of
# the normal MIN_FETCH_INTERVAL_MINUTES -- during a sustained outage every
# attempt still costs up to REQUEST_TIMEOUT_SECONDS * (REQUEST_RETRIES + 1),
# and without this a pipeline firing every few minutes would eat that cost
# on every single cycle for as long as Solis stays down. Index = consecutive
# failures so far, clamped to the last entry.
BACKOFF_SCHEDULE_MINUTES = [5, 10, 20, 40, 60]

# SolisCloud's authenticated endpoints (stationDetail/inverterDetail
# especially) are reported elsewhere as slow or intermittently flaky under
# load, independent of your own network -- confirmed here by curl getting
# an instant reply for an unsigned request (rejected before touching the
# backend) while a properly-signed stationDetail call hung or 502'd.
# Every failure seen so far has been either an instant error (502, app-level
# "try again") or a full hang to the timeout -- no case yet of a slow-but-
# real response arriving late, so a shorter timeout mainly cuts wasted wait
# on hangs rather than risking a real response getting cut off. One retry
# covers a lone blip within a single run; BACKOFF_SCHEDULE_MINUTES above
# covers a sustained outage across runs.
REQUEST_TIMEOUT_SECONDS = 15
REQUEST_RETRIES = 1
REQUEST_RETRY_DELAY_SECONDS = 5

# see "Yesterday" note above
DAY_ENERGY_IS_CUMULATIVE = True

HERE = os.path.dirname(os.path.abspath(__file__))
STATUS_PATH = os.path.join(HERE, "solis_status.json")
STATE_PATH = os.path.join(HERE, "solis_fetch_state.json")   # last attempt time + consecutive failures, see BACKOFF_SCHEDULE_MINUTES

# (value key, unit key) candidates, tried in order, for each metric --
# see NOTE ON FIELD NAMES above. Confirmed against a real stationDetail
# capture: "power" is live output (kW), distinct from the separate
# "capacity" field (rated kWp) -- an earlier assumption that "power" meant
# capacity, based on a second-hand discussion summary, turned out wrong (or
# at least not universal). "pac"/"acPower" kept as fallbacks in case some
# accounts use those instead.
POWER_NOW_CANDIDATES = [
    ("power", "powerStr"),
    ("pac", "pacUnit"),
    ("acPower", "acPowerUnit"),
]
CONSUMPTION_NOW_CANDIDATES = [
    ("familyLoadPower", "familyLoadPowerStr"),   # confirmed against a real capture -- unit key is *Str, not *Unit
    ("homeLoadPower", "homeLoadPowerUnit"),
    ("loadPower", "loadPowerUnit"),
]
GRID_NOW_CANDIDATES = [
    ("psum", "psumStr"),   # unit key confirmed *Str not *Unit, same pattern as power/familyLoadPower.
                            # Sign confirmed positive=export by energy balance on a real account:
                            # power - familyLoadPower - batteryPower == psum
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
TODAY_GRID_EXPORT_CANDIDATES = [
    ("gridSellDayEnergy", "gridSellDayEnergyStr"),   # confirmed against a real capture
    ("gridSellEnergy", "gridSellEnergyStr"),
    ("gridSellTodayEnergy", "gridSellTodayEnergyUnit"),
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
YESTERDAY_GRID_EXPORT_CANDIDATES = [
    ("gridSellYesterdayEnergy", "gridSellYesterdayEnergyStr"),   # reported elsewhere, unconfirmed on this account
                                                                   # (not present in this account's own stationDetail
                                                                   # capture -- falls back to stationDay if absent)
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
DAY_RECORD_GRID_EXPORT_CANDIDATES = ["gridSellEnergy", "sellEnergy", "onGridEnergy"]


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
    """Best-effort fallback for a specific past date's production/consumption/
    grid-export, via the stationDay time-series endpoint -- see "Yesterday"
    note above. One call covers all three metrics (rather than a separate
    request per metric) since they're all in the same per-day record set.
    Unlike fetch_station_detail, failure here isn't fatal to the whole run,
    so it swallows its own errors (after _call's retry) and returns
    (None, None, None) rather than raising."""
    try:
        resp = _call(STATION_DAY_PATH, {"id": station_id, "money": MONEY_CODE, "timezone": 0, "time": date_str})
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError) as e:
        print(f"stationDay fetch for {date_str} failed ({e})")
        return None, None, None
    records = resp.get("data")
    if not isinstance(records, list) or not records:
        return None, None, None

    def _series(candidates):
        for key in candidates:
            values = [float(r[key]) for r in records if isinstance(r, dict) and r.get(key) is not None]
            if values:
                return max(values) if DAY_ENERGY_IS_CUMULATIVE else sum(values)
        return None

    return (
        _series(DAY_RECORD_PRODUCE_CANDIDATES),
        _series(DAY_RECORD_CONSUME_CANDIDATES),
        _series(DAY_RECORD_GRID_EXPORT_CANDIDATES),
    )


def _pick(data, candidates):
    for value_key, unit_key in candidates:
        if value_key in data and data[value_key] is not None:
            try:
                value = float(data[value_key])
            except (TypeError, ValueError):
                continue
            return value, (data.get(unit_key) if unit_key else None)
    return None, None


def _load_state():
    if not os.path.exists(STATE_PATH):
        return {"last_attempt_at": None, "consecutive_failures": 0}
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_attempt_at": None, "consecutive_failures": 0}


def _save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f)


def _wait_minutes_for(consecutive_failures):
    if not consecutive_failures:
        return MIN_FETCH_INTERVAL_MINUTES
    return BACKOFF_SCHEDULE_MINUTES[min(consecutive_failures, len(BACKOFF_SCHEDULE_MINUTES)) - 1]


def _due(state):
    if not state.get("last_attempt_at"):
        return True
    wait_minutes = _wait_minutes_for(state.get("consecutive_failures", 0))
    last_attempt = dt.datetime.fromisoformat(state["last_attempt_at"])
    age_minutes = (dt.datetime.now(dt.timezone.utc) - last_attempt).total_seconds() / 60
    return age_minutes >= wait_minutes


def main():
    if not (KEY_ID and KEY_SECRET):
        print("SOLIS_KEY_ID / SOLIS_KEY_SECRET not set -- skipping SolisCloud fetch "
              "(see the setup notes at the top of this file)")
        return

    state = _load_state()
    if not _due(state):
        wait_minutes = _wait_minutes_for(state.get("consecutive_failures", 0))
        print(f"last SolisCloud attempt was under {wait_minutes} min ago "
              f"(consecutive failures: {state.get('consecutive_failures', 0)}) -- skipping")
        return

    state["last_attempt_at"] = dt.datetime.now(dt.timezone.utc).isoformat()

    try:
        if STATION_ID:
            station_id = STATION_ID
            print(f"using station id {station_id!r} (from SOLIS_STATION_ID)")
        else:
            station_id = discover_station_id()
            print(f"using station id {station_id!r} (discovered via userStationList)")
        data = fetch_station_detail(station_id)
    except (urllib.error.URLError, TimeoutError, OSError, RuntimeError, ValueError, KeyError) as e:
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        _save_state(state)
        next_wait = _wait_minutes_for(state["consecutive_failures"])
        print(f"SolisCloud fetch failed ({e}); leaving existing solis_status.json in place "
              f"(consecutive failures: {state['consecutive_failures']}, next attempt in {next_wait} min)")
        return

    power_kw, power_unit = _pick(data, POWER_NOW_CANDIDATES)
    consumption_kw, consumption_unit = _pick(data, CONSUMPTION_NOW_CANDIDATES)
    grid_kw, grid_unit = _pick(data, GRID_NOW_CANDIDATES)
    battery_pct, _ = _pick(data, BATTERY_SOC_CANDIDATES)
    today_kwh, today_unit = _pick(data, TODAY_PRODUCTION_CANDIDATES)
    today_consumption_kwh, today_consumption_unit = _pick(data, TODAY_CONSUMPTION_CANDIDATES)
    today_export_kwh, today_export_unit = _pick(data, TODAY_GRID_EXPORT_CANDIDATES)
    yesterday_kwh, _ = _pick(data, YESTERDAY_PRODUCTION_CANDIDATES)
    yesterday_consumption_kwh, _ = _pick(data, YESTERDAY_CONSUMPTION_CANDIDATES)
    yesterday_export_kwh, _ = _pick(data, YESTERDAY_GRID_EXPORT_CANDIDATES)
    total_kwh, total_unit = _pick(data, TOTAL_ENERGY_CANDIDATES)

    if yesterday_kwh is None or yesterday_consumption_kwh is None or yesterday_export_kwh is None:
        yesterday_date = (dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)).isoformat()
        try:
            day_produce, day_consume, day_export = fetch_day_totals(station_id, yesterday_date)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            print(f"stationDay fallback for yesterday's totals failed ({e})")
            day_produce, day_consume, day_export = None, None, None
        if yesterday_kwh is None:
            yesterday_kwh = day_produce
        if yesterday_consumption_kwh is None:
            yesterday_consumption_kwh = day_consume
        if yesterday_export_kwh is None:
            yesterday_export_kwh = day_export

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
        "today_export_kwh": today_export_kwh,
        "today_export_unit": today_export_unit,
        "yesterday_kwh": yesterday_kwh,
        "yesterday_consumption_kwh": yesterday_consumption_kwh,
        "yesterday_export_kwh": yesterday_export_kwh,
        "total_kwh": total_kwh,
        "total_unit": total_unit,
        "raw": data,
    }

    with open(STATUS_PATH, "w") as f:
        json.dump(status, f, indent=2)

    state["consecutive_failures"] = 0
    _save_state(state)

    print(f"SolisCloud: {power_kw} kW now, {today_kwh} kWh today, battery {battery_pct}%")


if __name__ == "__main__":
    main()
