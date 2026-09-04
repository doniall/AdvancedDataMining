#!/usr/bin/env python3
"""
C_build_clutter_baseline.py

Builds a per-pixel "usual rain level" baseline from the accumulated frame
history in 0_RadarPNG/, used by B_ireland_radar_greyscale.py to suppress
Met Eireann's permanent radar clutter (chronically-lit spots, e.g. the
three known ones around Dublin) without also hiding real rain that
exceeds the usual level at those same spots.

For each source pixel, the baseline is the MODE (most frequently seen)
classified rain level across every saved frame. A location that shows
drizzle in most frames regardless of what else is happening gets a
nonzero baseline; a location that's usually dry gets 0.
B_ireland_radar_greyscale.render() then only treats a pixel as rain if
its current level is strictly above its own baseline.

Run this periodically as more frames accumulate in 0_RadarPNG/ -- the
more history, the more reliable the baseline. Re-running overwrites
clutter_baseline.npy.
"""

import os
import numpy as np
from PIL import Image

import B_ireland_radar_greyscale as B

NUM_LEVELS = len(B.GREY) + 1  # 0 (no rain) through the highest RAMP level

# a clutter blob's reliable core hits the same level almost every frame, but
# its fringe is noisier (resampling jitter, intensity falloff at the edge)
# and can fall just short of majority -- dilating the baseline by this many
# pixels covers that fuzzy edge instead of leaving a suppressed core with an
# unsuppressed ring around it
DILATE_PX = 2


def dilate_max(a, iterations):
    """Grow each cell to the max of its 3x3 neighbourhood, `iterations` times."""
    out = a.astype(np.uint8)
    for _ in range(iterations):
        p = np.pad(out, 1, mode="edge")
        out = np.maximum.reduce([
            p[0:-2, 0:-2], p[0:-2, 1:-1], p[0:-2, 2:],
            p[1:-1, 0:-2], p[1:-1, 1:-1], p[1:-1, 2:],
            p[2:,   0:-2], p[2:,   1:-1], p[2:,   2:],
        ])
    return out


def main():
    files = sorted(f for f in os.listdir(B.RadarImageSubfolder) if f.lower().endswith(".png"))
    if not files:
        raise SystemExit(f"No saved frames in {B.RadarImageSubfolder}/ to build a baseline from.")
    print(f"building baseline from {len(files)} frames")

    counts = np.zeros((NUM_LEVELS, B.IMG_H, B.IMG_W), np.int32)
    used = 0
    for i, fn in enumerate(files):
        src = Image.open(os.path.join(B.RadarImageSubfolder, fn)).convert("RGB")
        A = np.asarray(src).astype(float)
        if (src.width, src.height) != (B.IMG_W, B.IMG_H):
            print(f"  skipping {fn}: unexpected size {src.width}x{src.height}")
            continue
        A = B.fill_black(A)   # match what render() actually sees
        lvl = B.classify(A[:, :, 0], A[:, :, 1], A[:, :, 2])
        for k in range(NUM_LEVELS):
            counts[k] += (lvl == k)
        used += 1
        if used % 50 == 0:
            print(f"  ...{used}/{len(files)}")

    if used == 0:
        raise SystemExit("No usable frames (all wrong size?) -- nothing to build.")

    baseline = counts.argmax(axis=0).astype(np.uint8)
    n_core = int((baseline > 0).sum())

    baseline = dilate_max(baseline, DILATE_PX)
    n_clutter = int((baseline > 0).sum())
    print(f"used {used} frames; {n_core} core clutter pixels, {n_clutter} after "
          f"{DILATE_PX}px dilation to cover the fringe")

    np.save(B.CLUTTER_BASELINE_PATH, baseline)
    print(f"wrote {B.CLUTTER_BASELINE_PATH}")


if __name__ == "__main__":
    main()
