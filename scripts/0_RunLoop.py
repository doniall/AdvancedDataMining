#!/usr/bin/env python3
"""
0_RunLoop.py

Runs 0_Run_Radar_And_Greyscale.py repeatedly on a fixed interval for a
bounded total duration -- for an ad hoc test run (e.g. watching how the
device/dashboard behaves across many consecutive cycles) without needing
a real external scheduler (cron/launchd) set up yet.

Each run is a fresh subprocess (matching how a real scheduler would
invoke it, rather than a long-lived Python process holding state across
runs). Scheduled against absolute clock targets (start + k*INTERVAL), not
"sleep INTERVAL after each run finishes" -- a run's own duration (AIS
alone listens for LISTEN_SECONDS=90s) would otherwise stretch the real
start-to-start gap past INTERVAL_SECONDS. If a run overruns past its next
scheduled slot, the next one starts immediately rather than waiting an
extra full interval -- it catches up instead of compounding drift.

Run:   python3 0_RunLoop.py
"""

import os
import subprocess
import sys
import time

INTERVAL_SECONDS = 5 * 60 + 1   # 5 min 1 s
DURATION_SECONDS = 60 * 60      # 1 hour

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    start = time.monotonic()
    run_number = 0
    while True:
        target_start = start + run_number * INTERVAL_SECONDS
        if target_start - start >= DURATION_SECONDS:
            break
        now = time.monotonic()
        if now < target_start:
            time.sleep(target_start - now)

        run_number += 1
        elapsed = time.monotonic() - start
        print(f"=== run {run_number} starting at {elapsed:.0f}s elapsed ===")
        result = subprocess.run([sys.executable, "0_Run_Radar_And_Greyscale.py"], cwd=HERE)
        if result.returncode != 0:
            print(f"run {run_number} exited with code {result.returncode} -- continuing anyway")

    print(f"done -- ran {run_number} time(s) over {time.monotonic() - start:.0f}s")


if __name__ == "__main__":
    main()
