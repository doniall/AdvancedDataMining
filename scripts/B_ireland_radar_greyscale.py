#!/usr/bin/env python3
"""
B_ireland_radar_greyscale.py

Turn a Met Eireann rainfall-radar frame into the greyscale "device" image:
rain over sea and land shaded by intensity (light -> dark), the coastline drawn
on top so Ireland's shape shows through the rain, and a dot at your location.

USAGE
    python3 B_ireland_radar_greyscale.py                 # fetch the latest frame, render
    python3 B_ireland_radar_greyscale.py frame.png       # render a local frame
    python3 B_ireland_radar_greyscale.py frame.png landscape   # override the view
    python3 B_ireland_radar_greyscale.py portrait        # fetch latest, portrait view

    Edit LOCATION_* and VIEW below to set your defaults.

REQUIRES
    pip3 install numpy pillow certifi
    Keep ireland_coastline.json and ireland_counties.json in the same folder.
    A_met_radar_probe.py must also be in the same folder (used for fetching).
    Optional: run D_ship_ais.py repeatedly (it appends to a history file) to
    draw ships as they were at each frame's own timestamp, with a trail back
    to their position ~15 minutes earlier -- if ship_history.json doesn't
    exist yet, ships are just skipped.

NOTE
    The projection is exact, not fitted: gdal.met.ie serves standard Web
    Mercator XYZ map tiles (the same scheme OpenStreetMap uses), so pixel
    <-> lon/lat conversion follows directly from the zoom level and which
    tiles cover Ireland -- both defined in A_met_radar_probe.py. If met.ie
    ever changes the tile grid it covers Ireland with, update TILE_X0/X1/
    Y0/Y1 there and this file picks up the change automatically.
"""

import os, sys, io, json, math, datetime as dt
from zoneinfo import ZoneInfo
import numpy as np
from PIL import Image, ImageDraw, ImageFont

import A_met_radar_probe as radar_probe

# ===================== EDIT THESE =====================
LOCATION_LAT = 53.3498      # your home latitude   (default: Dublin)
LOCATION_LON = -6.2603      # your home longitude
VIEW         = "portrait"   # "portrait"  = Ireland fills the frame (Atlantic margin)
                            # "landscape" = full Met Eireann extent, out to Wales
# ======================================================

# --- tile-grid projection (exact; derived from A_met_radar_probe's tile grid) ---
TILE_ZOOM = radar_probe.TILE_ZOOM
TILE_X0, TILE_X1 = radar_probe.TILE_X0, radar_probe.TILE_X1
TILE_Y0, TILE_Y1 = radar_probe.TILE_Y0, radar_probe.TILE_Y1
TILE_SIZE = radar_probe.TILE_SIZE

IMG_W = TILE_SIZE * (TILE_X1 - TILE_X0 + 1)
IMG_H = TILE_SIZE * (TILE_Y1 - TILE_Y0 + 1)

_n = 2 ** TILE_ZOOM
S  = _n * TILE_SIZE / (2 * math.pi)
BX = _n * TILE_SIZE / 2 - TILE_X0 * TILE_SIZE
BY = _n * TILE_SIZE / 2 - TILE_Y0 * TILE_SIZE

# manually measured against a saved frame: strip this many source pixels off
# the west and south edges before the view window is allowed to reach them
WEST_CUTOFF_PX  = 130
SOUTH_CUTOFF_PX = 55

IRELAND_BOUNDS = (-10.4782, 51.4457, -5.4308, 55.3864)   # minlon, minlat, maxlon, maxlat

# --- greyscale look (0 = black, 255 = white) ---
BACKGROUND  = 255   # no distinction between sea and land -- only coastline/
                     # county lines, radar extent, and rain levels carry meaning
COAST_HALO  = 248
COAST_LINE  = 22
DOT         = 20
COUNTY_LINE = 150   # mid grey

RAMP = np.array([
    [120,180,248],[72,164,240],[56,160,216],[48,150,208],[40,146,198],
    [32,138,188],[40,150,170],[30,148,152],[36,160,132],[40,172,108],
    [ 80,180, 90],[150,200, 60],[230,205, 50],[235,150, 40],[215,60,40],[190,40,120]
], float)   # Met rain colours, light -> heavy

# Met Eireann's dry-but-in-range olive, sampled directly from a saved frame
# (see conversation history). Its nearest RAMP colour is 70 RGB units away
# (vivid green, [80,180,90]), so a tolerance well under that rejects only
# this specific background hue without touching real rain colours, however
# far they've drifted from their exact RAMP value due to blending/anti-
# aliasing -- unlike a blanket "must be close to some RAMP entry" rule,
# which was rejecting genuine yellow/green rain too.
BACKGROUND_RGB       = np.array([71, 112, 76], float)
BACKGROUND_TOLERANCE = 30

GREY = {i+1: v for i, v in enumerate(
    [224,198,186,174,162,150,138,126,112,98,84,70,58,46,36,30])}  # light -> dark

# lightest thing in the whole palette -- barely off white, so the radar's
# coverage extent reads as a faint tint rather than a visible grey. GREY[1]
# (lightest rain) sits two ladder-steps (~12 each) below it.
RANGE_GREY = 250

RadarImageSubfolder = "0_RadarPNG"
GreyscaleRadarImageSubfolder = "1_GreyscalePNG"

# ships are drawn near-black with a light halo so they read clearly whether
# they sit over pale background or dark (heavy-rain) pixels -- the same
# halo-then-line trick used for the coastline
SHIP_MARK = 0
SHIP_HALO = 255
SHIP_SIZE = 5   # centre-to-nose length, in output px


def mercY(lat):     return np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
def inv_mercY(y):   return np.degrees(2 * np.arctan(np.exp(y)) - np.pi / 2)


def fetch_latest():
    """Grab the most recent live radar frame via the gdal.met.ie tile API."""
    frames = radar_probe.fetch_frame_list()
    if not frames:
        raise SystemExit("Could not fetch a live frame. Pass a local PNG path instead.")
    latest = frames[-1]
    img = radar_probe.fetch_frame(latest["src"], latest["modifiedTime"])
    if img is None:
        raise SystemExit("Could not fetch a live frame. Pass a local PNG path instead.")
    print("fetched", latest.get("toolTipDate", latest["src"]))
    return img


def load_coastline():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "ireland_coastline.json")) as f:
        return [np.array(r, float) for r in json.load(f)]

def load_counties():
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "ireland_counties.json")) as f:
        return [np.array(r, float) for r in json.load(f)]

SHIP_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_history.json")

# how a ship's "now" and "15 minutes ago" positions are picked out of its
# recorded history, relative to the timestamp of the frame being rendered
# (not wall-clock time -- see ships_at())
SHIP_MAX_AGE_MINUTES = 20          # ignore a ship with no sample this fresh --
                                    # stale AIS is worse than no marker at all
SHIP_TRAIL_MINUTES = 15
SHIP_TRAIL_TOLERANCE_MINUTES = 5   # how far a sample may sit from exactly
                                    # -15min and still count as the trail start


def load_ship_history():
    """Per-ship position history recorded by D_ship_ais.py -- {} (no markers)
    if that script hasn't been run yet, so ship display is entirely optional."""
    if not os.path.exists(SHIP_HISTORY_PATH):
        return {}
    with open(SHIP_HISTORY_PATH) as f:
        return json.load(f)


def _nearest_sample(samples, target):
    """The sample whose timestamp is closest to `target` (before or after)."""
    if not samples:
        return None
    return min(samples, key=lambda s: abs((dt.datetime.fromisoformat(s["t"]) - target).total_seconds()))


def ships_at(history, at_time):
    """Current position + ~15-minutes-ago trail start for each ship, as of
    at_time -- the frame's own timestamp, not whenever this script happens to
    run. That's what lets a backlog of radar frames each show ships as they
    actually were at that frame's time, instead of all showing today's
    living AIS position stamped onto every one of them."""
    out = []
    for samples in history.values():
        now_s = _nearest_sample(samples, at_time)
        if now_s is None:
            continue
        if abs((dt.datetime.fromisoformat(now_s["t"]) - at_time).total_seconds()) / 60 > SHIP_MAX_AGE_MINUTES:
            continue

        trail_target = at_time - dt.timedelta(minutes=SHIP_TRAIL_MINUTES)
        before_s = _nearest_sample(samples, trail_target)
        trail_lat = trail_lon = None
        if before_s is not None:
            before_off = abs((dt.datetime.fromisoformat(before_s["t"]) - trail_target).total_seconds()) / 60
            if before_off <= SHIP_TRAIL_TOLERANCE_MINUTES:
                trail_lat, trail_lon = before_s["lat"], before_s["lon"]

        out.append({
            "lat": now_s["lat"], "lon": now_s["lon"],
            "heading": now_s.get("heading"), "cog": now_s.get("cog"),
            "trail_lat": trail_lat, "trail_lon": trail_lon,
        })
    return out

def is_background(r, g, b):
    """True where a pixel matches Met Eireann's dry-but-in-range olive,
    within BACKGROUND_TOLERANCE RGB units. The one place this exact colour
    is defined -- classify() excludes it from rain matching, in_range_mask()
    uses the same test to positively identify it, so the two can't disagree."""
    dd = ((r - BACKGROUND_RGB[0]) ** 2 + (g - BACKGROUND_RGB[1]) ** 2
          + (b - BACKGROUND_RGB[2]) ** 2)
    return dd < BACKGROUND_TOLERANCE ** 2


def classify(r, g, b):
    """0 = no rain; 1..16 = Met intensity (light..heavy), nearest point on RAMP."""
    r, g, b = r.astype(float),g.astype(float),b.astype(float)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    rain = (sat > 0.30) & (mx > 70) & ~is_background(r, g, b)
    px = np.stack([r, g, b], -1)[rain]                 # rain pixels only
    best = np.full(len(px), 1e18); idx = np.zeros(len(px), int)
    for k, c in enumerate(RAMP):                       # 16 passes, tiny memory
        dd = ((px - c) ** 2).sum(1)
        m = dd < best; best[m] = dd[m]; idx[m] = k
    lvl = np.zeros(r.shape, int)
    lvl[rain] = idx + 1

    from PIL import ImageFilter
    for _ in range(5):
        med = np.asarray(Image.fromarray(lvl.astype(np.uint8)).filter(ImageFilter.MedianFilter(5)))
        gap = (lvl == 0) & (med > 0)
        lvl[gap] = med[gap]

    return lvl


def in_range_mask(r, g, b, lvl):
    """True where a pixel is Met Eireann's dry-but-in-range green -- checked only
    among pixels classify() already ruled out as rain, so a loose colour match
    here can never steal a real rain pixel."""
    return (lvl == 0) & is_background(r, g, b)


CLUTTER_BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clutter_baseline.npy")
_clutter_baseline = None
_warned_no_clutter_baseline = False


def _load_clutter_baseline():
    """Per-source-pixel 'usual rain level', built by C_build_clutter_baseline.py
    from saved frame history. Cached per process; a bare zeros array (no
    suppression) if the file hasn't been built yet."""
    global _clutter_baseline, _warned_no_clutter_baseline
    if _clutter_baseline is None:
        if os.path.exists(CLUTTER_BASELINE_PATH):
            _clutter_baseline = np.load(CLUTTER_BASELINE_PATH)
        else:
            _clutter_baseline = np.zeros((IMG_H, IMG_W), np.uint8)
            if not _warned_no_clutter_baseline:
                print("note: no clutter_baseline.npy yet -- run C_build_clutter_baseline.py "
                      "to suppress Met Eireann's permanent radar clutter")
                _warned_no_clutter_baseline = True
    return _clutter_baseline


CLUTTER_CEILING_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "clutter_ceiling.npy")
_clutter_ceiling = None
_warned_no_clutter_ceiling = False


def _load_clutter_ceiling():
    """Per-source-pixel highest level ever actually observed there, built by
    C_build_clutter_baseline.py. A noisy, strongly-active clutter spot (high
    mode) can swing several levels above its own mode on an ordinary bad
    frame, so the unconditional "trust it outright" escape hatch requires
    beating this historical ceiling, not just the mode -- falls back to the
    mode itself (the older, more easily tripped behaviour) if the ceiling
    file hasn't been built yet."""
    global _clutter_ceiling, _warned_no_clutter_ceiling
    if _clutter_ceiling is None:
        if os.path.exists(CLUTTER_CEILING_PATH):
            _clutter_ceiling = np.load(CLUTTER_CEILING_PATH)
        else:
            _clutter_ceiling = _load_clutter_baseline()
            if not _warned_no_clutter_ceiling:
                print("note: no clutter_ceiling.npy yet -- run C_build_clutter_baseline.py "
                      "again to reduce false triggers from noisy clutter spots")
                _warned_no_clutter_ceiling = True
    return _clutter_ceiling


# radius (source pixels) checked around each clutter pixel for corroborating
# rain elsewhere -- a real rain system doesn't have a precise hole exactly at
# a clutter spot, so if enough real rain surrounds it, trust this pixel too
# even at its usual (otherwise-suppressed) level
NEARBY_RADIUS_PX = 12
NEARBY_MIN_FRACTION = 0.05

# a *much* tighter radius for literal contact: a small, localised shower
# genuinely touching the clutter spot can easily be well under
# NEARBY_MIN_FRACTION of the wide NEARBY_RADIUS_PX window (a shower a few
# pixels across is a tiny fraction of a 25x25 area), even though it's
# unambiguously real rain right at the edge. A small coherent patch of real
# rain within this small radius is trusted outright, independent of the
# wider-area check -- this is the literal "include them when other rain has
# hit" case.
TOUCH_RADIUS_PX = 4

# ...but not just ANY real-rain pixel: classify()'s median-filter pass only
# ever fills gaps *toward* rain, never removes an isolated false-positive
# pixel (anti-aliasing noise, a tile-seam artifact), so a single stray
# speckle can and does show up in real, live data. Requiring a small
# coherent cluster rather than one pixel tells an actual shower apart from
# that kind of noise, without falling back to the wide-area's much higher bar.
TOUCH_MIN_COUNT = 3


def _box_sum(a, radius):
    """Sum of `a` over a (2*radius+1) square window centred on each cell,
    via an integral image -- no scipy dependency."""
    pad = np.pad(a.astype(np.int32), radius, mode="constant")
    csum = np.pad(pad.cumsum(0).cumsum(1), ((1, 0), (1, 0)), mode="constant")
    s = radius * 2 + 1
    return csum[s:, s:] - csum[:-s, s:] - csum[s:, :-s] + csum[:-s, :-s]


def suppress_clutter_source(full_lvl):
    """Zero out a clutter pixel's level if it's at or below its own historical
    baseline AND there's no real rain corroborating it nearby. Real rain that
    beats the spot's historical ceiling, is touching the clutter spot
    directly, or is backed by a wider rain system surrounding it, still
    shows -- operates on the full source-resolution classification so
    "nearby" means real geography, not output pixels."""
    baseline = _load_clutter_baseline()
    ceiling = _load_clutter_ceiling()
    clutter = baseline > 0

    # a clutter pixel already beating its own historical ceiling is real
    # evidence of weather on its own (it's never suppressed below -- see
    # `suppress`), so it should still be able to corroborate its neighbours
    # even though it's inside the (dilated) clutter mask. Without this,
    # growing DILATE_PX in C_build_clutter_baseline.py to cover a noisy
    # fringe also shrinks the pool of nearby evidence real rain can use to
    # prove itself, making a wider dilation actively worse at letting real
    # rain through right where the fringe is widest.
    #
    # the escape hatch itself checks the CEILING (highest ever observed),
    # not the mode: a noisy, strongly-active spot (e.g. mode 9) can swing
    # several levels above its own mode on an ordinary bad frame with no
    # real rain involved at all, so "exceeds the mode" alone tripped on
    # nothing but the artifact's normal noise. Beating the ceiling means a
    # genuinely unprecedented reading at that exact spot.
    exceeds_ceiling = full_lvl > ceiling
    real_rain = (full_lvl > 0) & (~clutter | exceeds_ceiling)

    window_area = (2 * NEARBY_RADIUS_PX + 1) ** 2
    nearby_rain_frac = _box_sum(real_rain, NEARBY_RADIUS_PX) / window_area
    surrounded = nearby_rain_frac >= NEARBY_MIN_FRACTION
    touching = _box_sum(real_rain, TOUCH_RADIUS_PX) >= TOUCH_MIN_COUNT
    corroborated = surrounded | touching

    suppress = clutter & ~exceeds_ceiling & ~corroborated
    return np.where(suppress, 0, full_lvl)


_FONT_CACHE = {}

_FONT_CANDIDATES = [
    "DejaVuSans-Bold.ttf",                                   # found via fontconfig on some Linux setups
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux, explicit path
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",     # macOS
    "/Library/Fonts/Arial Bold.ttf",                         # macOS (older)
    "C:\\Windows\\Fonts\\arialbd.ttf",                       # Windows
]


def _load_font(size):
    """Try known bold-font locations across platforms; fall back to Pillow's
    own scalable default font (bundled, always available) if none exist."""
    if size in _FONT_CACHE:
        return _FONT_CACHE[size]
    font = None
    for path in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            font = ImageFont.load_default()   # older Pillow: fixed size, no scaling
    _FONT_CACHE[size] = font
    return font


def _ship_triangle(x, y, direction_deg, size):
    """Points of a small triangle centred on (x, y), nose pointing
    direction_deg clockwise from north/up -- or a plain diamond if no
    heading/course is known for that ship."""
    if direction_deg is None:
        return [(x, y - size), (x + size, y), (x, y + size), (x - size, y)]
    a = math.radians(direction_deg)
    fx, fy = math.sin(a), -math.cos(a)   # forward unit vector, 0 deg = up
    bx, by = -fy, fx                     # perpendicular unit vector
    nose  = (x + fx * size * 1.6, y + fy * size * 1.6)
    left  = (x + bx * size - fx * size * 0.6, y + by * size - fy * size * 0.6)
    right = (x - bx * size - fx * size * 0.6, y - by * size - fy * size * 0.6)
    return [nose, left, right]


def _draw_ship(d, x, y, direction_deg):
    d.polygon(_ship_triangle(x, y, direction_deg, SHIP_SIZE + 2), fill=SHIP_HALO)
    d.polygon(_ship_triangle(x, y, direction_deg, SHIP_SIZE), fill=SHIP_MARK)


def fill_black(A, thresh=95, max_iter=16):
    """Replace any near-black pixels (tile-stitch seams, if any) with the average
    of the nearest non-black pixels, so rain reads continuous across them.
    A is an HxWx3 float array; returns the same, filled."""
    A = A.astype(np.float32); H, W, _ = A.shape
    known = A.max(2) >= thresh          # True = keep; False = black line to fill
    out = A.copy(); out[~known] = 0.0
    kn = known.astype(np.float32)
    for _ in range(max_iter):
        if known.all(): break
        sp = np.pad(out, ((1,1),(1,1),(0,0))); kp = np.pad(kn, ((1,1),(1,1)))
        num = (sp[0:H,0:W]+sp[0:H,1:W+1]+sp[0:H,2:W+2]
              +sp[1:H+1,0:W]+sp[1:H+1,1:W+1]+sp[1:H+1,2:W+2]
              +sp[2:H+2,0:W]+sp[2:H+2,1:W+1]+sp[2:H+2,2:W+2])
        den = (kp[0:H,0:W]+kp[0:H,1:W+1]+kp[0:H,2:W+2]
              +kp[1:H+1,0:W]+kp[1:H+1,1:W+1]+kp[1:H+1,2:W+2]
              +kp[2:H+2,0:W]+kp[2:H+2,1:W+1]+kp[2:H+2,2:W+2])
        newly = (~known) & (den > 0)
        out[newly] = num[newly] / den[newly][:, None]
        known |= newly; kn = known.astype(np.float32); out[~known] = 0.0
    return np.clip(out, 0, 255)

def build_window():
    """Return canvas size and the mercator view window for the chosen VIEW."""
    if VIEW == "landscape":
        W, H = 1872, 1404
        lon_l = math.degrees((0 - BX) / S)
        lon_r = math.degrees((IMG_W - BX) / S)
        Xmn, Xmx = math.radians(lon_l), math.radians(lon_r)
        lat_t = float(inv_mercY((BY - 0) / S))
        lat_b = float(inv_mercY((BY - IMG_H) / S))
        cx = (Xmn + Xmx) / 2
        cy = float((mercY(lat_t) + mercY(lat_b)) / 2)
        Wwin = Xmx - Xmn
        Hwin = Wwin / (W / H)                 # crop N/S to fill the landscape frame
    else:  # portrait: Ireland fills the frame, shifted west for the Atlantic approach
        W, H = 1404, 1872
        mnlon, mnlat, mxlon, mxlat = IRELAND_BOUNDS
        Xmn, Xmx = math.radians(mnlon), math.radians(mxlon)
        Ymn, Ymx = float(mercY(mnlat)), float(mercY(mxlat))
        cx = (Xmn + Xmx) / 2 - math.radians(0.9)
        cy = (Ymn + Ymx) / 2
        m = 1.34
        hw, hh = m * (Xmx - Xmn) / 2, m * (Ymx - Ymn) / 2
        ac = W / H
        if hw / hh < ac: hw = hh * ac
        else:            hh = hw / ac
        Wwin, Hwin = 2 * hw, 2 * hh

    hw, hh = Wwin / 2, Hwin / 2

    # never request geography past the usable data edge -- beyond it there's
    # no data (or, on the west/south, the measured dead strip), and PX/PY
    # clipping would just stretch the edge row/column across the gap instead
    # of showing anything real.
    tile_x_min, tile_x_max = (WEST_CUTOFF_PX - BX) / S, (IMG_W - BX) / S
    tile_y_min, tile_y_max = (BY - (IMG_H - SOUTH_CUTOFF_PX)) / S, (BY - 0) / S
    west, east = max(cx - hw, tile_x_min), min(cx + hw, tile_x_max)
    south, north = max(cy - hh, tile_y_min), min(cy + hh, tile_y_max)
    cx, hw = (west + east) / 2, (east - west) / 2
    cy, hh = (south + north) / 2, (north - south) / 2

    # clamping can unbalance the aspect ratio; shrink whichever axis is now
    # oversized to restore it -- this only ever shrinks, so it can't
    # re-violate the tile bounds just enforced above
    ac = W / H
    if hw / hh > ac: hw = hh * ac
    else:            hh = hw / ac
    Wwin, Hwin = 2 * hw, 2 * hh

    return W, H, cx, cy, Wwin / 2, Hwin / 2, Wwin, Hwin


def render(src, rings_County, rings_Coast, frame_time=None, ships=None):
    A = np.asarray(src).astype(float).copy()
    if (src.width, src.height) != (IMG_W, IMG_H):
        print("warning: expected a %dx%d tile mosaic, got %dx%d; "
              "the projection may be off." % (IMG_W, IMG_H, src.width, src.height))

    A = fill_black(A)

    W, H, cx, cy, hw, hh, Wwin, Hwin = build_window()

    def ll2r(lon, lat):
        return ((math.radians(lon) - (cx - hw)) / Wwin * W,
                (cy + hh - float(mercY(lat))) / Hwin * H)

    xs = cx - hw + (np.arange(W) + 0.5) / W * Wwin
    ys = cy + hh - (np.arange(H) + 0.5) / H * Hwin
    XX, YY = np.meshgrid(xs, ys)
    LON, LAT = np.degrees(XX), inv_mercY(YY)
    PX = np.clip((S * np.radians(LON) + BX).astype(int), 0, IMG_W - 1)
    PY = np.clip((BY - S * mercY(LAT)).astype(int), 0, IMG_H - 1)
    full_lvl = classify(A[:, :, 0], A[:, :, 1], A[:, :, 2])
    full_lvl = suppress_clutter_source(full_lvl)
    lvl = full_lvl[PY, PX]
    Rc, Gc, Bc = A[PY, PX, 0], A[PY, PX, 1], A[PY, PX, 2]
    rangem = in_range_mask(Rc, Gc, Bc, lvl)



    out = np.full((H, W), BACKGROUND, np.uint8)
    out[rangem] = RANGE_GREY          # radar coverage extent, under any rain
    for k, gv in GREY.items():
        out[lvl == k] = gv


    img = Image.fromarray(out, "L")
    d = ImageDraw.Draw(img)
    for ring in rings_County:
        d.line([ll2r(lo, la) for lo, la in ring], fill=COUNTY_LINE, width=1)
    for ring in rings_Coast:                        # halo, then line, drawn over the rain
        d.line([ll2r(lo, la) for lo, la in ring], fill=COAST_HALO, width=3, joint="curve")
    for ring in rings_Coast:
        d.line([ll2r(lo, la) for lo, la in ring], fill=COAST_LINE, width=3, joint="curve")

    for ship in ships or []:
        lon, lat = ship.get("lon"), ship.get("lat")
        if lon is None or lat is None:
            continue
        x, y = ll2r(lon, lat)

        tlon, tlat = ship.get("trail_lon"), ship.get("trail_lat")
        trail = tlon is not None and tlat is not None
        if trail:
            tx, ty = ll2r(tlon, tlat)
            d.line([(tx, ty), (x, y)], fill=SHIP_HALO, width=3)
            d.line([(tx, ty), (x, y)], fill=SHIP_MARK, width=1)

        heading, cog = ship.get("heading"), ship.get("cog")
        direction = heading if heading not in (None, 511) else (
            cog if cog is not None and cog < 360 else None)
        if direction is None and trail:
            # no reported heading/course -- the trail's own bearing is more
            # informative than an undirected mark
            direction = math.degrees(math.atan2(x - tx, ty - y)) % 360
        _draw_ship(d, x, y, direction)

    mx, my = ll2r(LOCATION_LON, LOCATION_LAT)
    rr = 3
    d.ellipse([mx-rr-1, my-rr-1, mx+rr+1, my+rr+1], fill=250)
    d.ellipse([mx-rr, my-rr, mx+rr, my+rr], outline=DOT, width=2)

    if frame_time is not None:
        label_font = _load_font(18)
        time_font = _load_font(30)
        d.text((16, 12), "Met Éireann", fill=COAST_LINE, font=label_font)
        d.text((16, 34), frame_time.strftime("%d/%m/%Y, %H:%M"), fill=COAST_LINE, font=time_font)

    return img


def ListFilesInDirectory (Address):
    #list all files in a location
    from os import walk
    results = next(walk(Address), (None, None, []))[2]  # [] if no file
    return results


def main():
    global VIEW
    args = sys.argv[1:]
    if "landscape" in args: VIEW = "landscape"
    if "portrait"  in args: VIEW = "portrait"

    #list all images already loaded and those processed
    LoadedRadarImages = ListFilesInDirectory(RadarImageSubfolder)
    GreyscaleRadarImages = ListFilesInDirectory(GreyscaleRadarImageSubfolder)

    #list all those missing
    NotListed = sorted(list(set(LoadedRadarImages).difference(GreyscaleRadarImages)))

    #remove any entries that are not .png files
    png_files = [f for f in NotListed if f.lower().endswith(".png")]

    ship_history = load_ship_history()
    
    for imgpath in png_files:
        print(f"{imgpath}")
        src = Image.open(f"{RadarImageSubfolder}/{imgpath}").convert("RGB") if imgpath else fetch_latest()
        stamp = os.path.splitext(imgpath)[0] if imgpath else None
        # the "src" timestamp in the filename is UTC (confirmed against the
        # manifest); convert to Irish local time for display.
        frame_time = (
            dt.datetime.strptime(stamp, "%Y%m%d%H%M")
            .replace(tzinfo=dt.timezone.utc)
            .astimezone(ZoneInfo("Europe/Dublin"))
            if stamp else None
        )
        at_time = frame_time.astimezone(dt.timezone.utc) if frame_time else dt.datetime.now(dt.timezone.utc)
        ships = ships_at(ship_history, at_time)
        img = render(src, load_counties(), load_coastline(), frame_time, ships)
        img.save(f"{GreyscaleRadarImageSubfolder}/{imgpath}")
        #print(f "wrote {GreyscaleRadarImageSubfolder}/{imgpath}", img.size, "view=" + VIEW)


if __name__ == "__main__":
    main()
