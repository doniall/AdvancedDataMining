#!/usr/bin/env python3
"""
E_fetch_ship_history.py

Gets ship_history.json up to date for B_ireland_radar_greyscale.py to
render, from whichever source is actually available:

    1. If AIS_VM_HOST is set, pull the file over SSH from a VM running
       D_ship_ais.py on its own schedule (see VM SETUP below).
    2. If AIS_VM_HOST isn't set, OR the VM fetch fails for any reason (host
       unreachable, deleted, key rejected, timeout), fall back to running
       D_ship_ais.py's own direct AIS pull right here instead -- this is
       the same script the VM itself runs, so nothing else needs to change
       to switch between "a VM does this" and "this device does this"
       setups. D_ship_ais.main() rate-limits itself independently
       (MIN_FETCH_INTERVAL_MINUTES there), so calling it from here on every
       0_Run_Radar_And_Greyscale.py cycle is harmless -- see its own
       docstring.

So: no VM at all (this device does the AIS pull itself) needs nothing set
here -- just AISSTREAM_API_KEY and `pip install websockets`, per
D_ship_ais.py's own docstring. A VM that's merely offline right now still
falls back the same way, same as one that was never configured.

VM SETUP (only relevant if you're using a VM -- skip entirely otherwise)
    Connects over SSH using a key that's restricted, via a `command=` clause
    in the VM's authorized_keys, to running exactly one read-only command --
    so a leaked key can only ever dump that one file, nothing else.

    One-time setup on the VM (as the VM's own login user, e.g. doniall):
        ssh-keygen -t ed25519 -f ~/.ssh/ais_sync -N ""   # run on THIS device, not the VM
        # copy the printed contents of ~/.ssh/ais_sync.pub, then on the VM:
        mkdir -p ~/.ssh && chmod 700 ~/.ssh
        echo 'command="cat /home/doniall/AdvancedDataMining/scripts/ship_history.json",no-agent-forwarding,no-X11-forwarding,no-port-forwarding,no-pty ssh-ed25519 AAAA...paste-the-pub-key-here...' >> ~/.ssh/authorized_keys
        chmod 600 ~/.ssh/authorized_keys

    Then on this device, set AIS_VM_HOST (a static IP or DNS name -- if the
    VM's public IP is dynamic, switch it to a Standard static Public IP in
    the Azure portal first, or this will break whenever the VM restarts):
        export AIS_VM_HOST=20.166.89.192
        python3 E_fetch_ship_history.py
"""

import os
import subprocess

import D_ship_ais

VM_HOST = os.environ.get("AIS_VM_HOST", "")
VM_USER = os.environ.get("AIS_VM_USER", "doniall")
VM_SSH_KEY = os.environ.get("AIS_VM_SSH_KEY", os.path.expanduser("~/.ssh/ais_sync"))

LOCAL_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_history.json")


def _fetch_from_vm():
    """Try the SSH pull from AIS_VM_HOST. Returns True on success, False on
    any failure (host unreachable, key rejected, timeout, empty response) --
    never raises, so main() can always fall back to the local pull."""
    tmp_path = LOCAL_HISTORY_PATH + ".tmp"
    try:
        with open(tmp_path, "wb") as f:
            subprocess.run(
                [
                    "ssh",
                    "-i", VM_SSH_KEY,
                    "-o", "BatchMode=yes",
                    "-o", "ConnectTimeout=15",
                    f"{VM_USER}@{VM_HOST}",
                ],
                stdout=f,
                stderr=subprocess.PIPE,
                check=True,
                timeout=30,
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        stderr = getattr(e, "stderr", b"") or b""
        print(f"ship history fetch from VM failed ({e}); "
              f"{stderr.decode(errors='replace').strip()}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False

    if os.path.getsize(tmp_path) == 0:
        print("ship history fetch from VM returned no data")
        os.remove(tmp_path)
        return False

    os.replace(tmp_path, LOCAL_HISTORY_PATH)
    print(f"fetched latest ship history from {VM_HOST}")
    return True


def main():
    if VM_HOST:
        if _fetch_from_vm():
            return
        print("falling back to a direct local AIS pull instead")
    else:
        print("AIS_VM_HOST not set -- running a direct local AIS pull")

    D_ship_ais.main()


if __name__ == "__main__":
    main()
