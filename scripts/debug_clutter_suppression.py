#!/usr/bin/env python3
"""
debug_clutter_suppression.py

Diagnoses exactly why clutter-flagged pixels are or aren't being suppressed
for one specific saved radar frame, instead of guessing at thresholds blind.
Produces a colour-coded overlay (clutter_debug.png) plus a console report.

Colours in the overlay:
    grey   = ordinary geography, not part of the clutter mask at all
    black  = clutter, correctly suppressed (no real evidence found)
    red    = clutter, showing because it BEATS ITS OWN HISTORICAL CEILING
             (no real rain needs to be nearby for this one -- if this lights
             up with zero real rain anywhere on the frame, the reading is
             genuinely unprecedented at that exact spot)
    blue   = clutter, showing because a small coherent patch of real rain is
             TOUCHING it (TOUCH_RADIUS_PX / TOUCH_MIN_COUNT)
    green  = clutter, showing because a wider rain system SURROUNDS it
             (NEARBY_RADIUS_PX / NEARBY_MIN_FRACTION)

Run:
    python3 debug_clutter_suppression.py 0_RadarPNG/<timestamp>.png
"""

import sys

import numpy as np
from PIL import Image

import B_ireland_radar_greyscale as B


def main():
    if len(sys.argv) != 2:
        print("usage: python3 debug_clutter_suppression.py 0_RadarPNG/<timestamp>.png")
        return

    src = Image.open(sys.argv[1]).convert("RGB")
    A = np.asarray(src).astype(float)
    A = B.fill_black(A)
    full_lvl = B.classify(A[:, :, 0], A[:, :, 1], A[:, :, 2])

    baseline = B._load_clutter_baseline()
    ceiling = B._load_clutter_ceiling()
    clutter = baseline > 0
    exceeds_ceiling = full_lvl > ceiling
    real_rain = (full_lvl > 0) & (~clutter | exceeds_ceiling)

    window_area = (2 * B.NEARBY_RADIUS_PX + 1) ** 2
    nearby_frac = B._box_sum(real_rain, B.NEARBY_RADIUS_PX) / window_area
    surrounded = clutter & (nearby_frac >= B.NEARBY_MIN_FRACTION)
    touching = clutter & (B._box_sum(real_rain, B.TOUCH_RADIUS_PX) >= B.TOUCH_MIN_COUNT)
    exceeds = clutter & exceeds_ceiling

    out = np.full((B.IMG_H, B.IMG_W, 3), 200, np.uint8)   # grey: not clutter
    out[clutter] = (0, 0, 0)                              # black: suppressed
    out[surrounded] = (0, 200, 0)
    out[touching] = (0, 100, 255)
    out[exceeds] = (255, 0, 0)                             # highest priority, drawn last
    Image.fromarray(out, "RGB").save("clutter_debug.png")
    print("wrote clutter_debug.png")

    total_rain_elsewhere = int(((full_lvl > 0) & ~clutter).sum())
    print(f"\n{int(clutter.sum())} clutter-flagged pixels total")
    print(f"{total_rain_elsewhere} real-rain pixels elsewhere on this frame (outside the clutter mask)")

    # a pixel counts as "leaking" only if it's both un-suppressed AND
    # actually showing a nonzero level -- a corroborated pixel that reads
    # 0 anyway shows nothing regardless, and isn't a real leak
    leaking = clutter & (full_lvl > 0) & (exceeds | touching | surrounded)
    print(f"{int(leaking.sum())} clutter pixels are showing a nonzero level (not suppressed) in this frame\n")

    ys, xs = np.where(leaking)
    for y, x in list(zip(ys, xs))[:30]:
        reasons = []
        if exceeds[y, x]:
            reasons.append("EXCEEDS_CEILING")
        if touching[y, x]:
            reasons.append("touching")
        if surrounded[y, x]:
            reasons.append("surrounded")
        print(f"  (x={x}, y={y}): level={full_lvl[y, x]}  baseline={baseline[y, x]}  "
              f"ceiling={ceiling[y, x]}  -> {', '.join(reasons)}")
    if leaking.sum() > 30:
        print(f"  ...and {int(leaking.sum()) - 30} more")


if __name__ == "__main__":
    main()
