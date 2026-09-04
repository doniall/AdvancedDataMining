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

GREY = {i+1: v for i, v in enumerate(
    [224,198,186,174,162,150,138,126,112,98,84,70,58,46,36,30])}  # light -> dark

# lightest thing in the whole palette -- barely off white, so the radar's
# coverage extent reads as a faint tint rather than a visible grey. GREY[1]
# (lightest rain) sits two ladder-steps (~12 each) below it.
RANGE_GREY = 250

# The muted olive-green Met Eireann draws over the radar's coverage circle,
# distinguished from real rain by low saturation (RAMP rain colours are all
# vivid). Tune these against a real saved frame if the circle doesn't show.
RANGE_G_MIN_OVER_R = 10
RANGE_G_MIN_OVER_B = 10
RANGE_G_MIN         = 60
RANGE_G_MAX         = 200

RadarImageSubfolder = "0_RadarPNG"
GreyscaleRadarImageSubfolder = "1_GreyscalePNG"


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

def classify(r, g, b):
    """0 = no rain; 1..16 = Met intensity (light..heavy), nearest point on RAMP."""
    r, g, b = r.astype(float),g.astype(float),b.astype(float)
    mx = np.maximum(np.maximum(r, g), b)
    mn = np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    rain = (sat > 0.30) & (mx > 70)
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
    g_dominant = (g > r + RANGE_G_MIN_OVER_R) & (g > b + RANGE_G_MIN_OVER_B)
    bright_enough = (g > RANGE_G_MIN) & (g < RANGE_G_MAX)
    return (lvl == 0) & g_dominant & bright_enough


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


def render(src, rings_County, rings_Coast, frame_time=None):
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
    Rc, Gc, Bc = A[PY, PX, 0], A[PY, PX, 1], A[PY, PX, 2]
    lvl = classify(Rc, Gc, Bc)
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

    mx, my = ll2r(LOCATION_LON, LOCATION_LAT)
    rr = 3
    d.ellipse([mx-rr-1, my-rr-1, mx+rr+1, my+rr+1], fill=250)
    d.ellipse([mx-rr, my-rr, mx+rr, my+rr], outline=DOT, width=2)

    if frame_time is not None:
        label_font = _load_font(18)
        time_font = _load_font(30)
        d.text((16, 12), "Met Éireann", fill=COAST_LINE, font=label_font)
        d.text((16, 34), frame_time.strftime("%H:%M, %d/%m/%Y"), fill=COAST_LINE, font=time_font)

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

    print(f"test")
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
        img = render(src, load_counties(), load_coastline(), frame_time)
        img.save(f"{GreyscaleRadarImageSubfolder}/{imgpath}")
        #print(f "wrote {GreyscaleRadarImageSubfolder}/{imgpath}", img.size, "view=" + VIEW)


if __name__ == "__main__":
    main()
