#!/usr/bin/env python3
"""
E_fetch_ship_history.py

D_ship_ais.py now runs on its own schedule on the Azure VM, not on this
device -- this script pulls the ship_history.json it's been building there
and writes it to this device's own copy of the same file, so
B_ireland_radar_greyscale.py renders it. This device no longer needs to
run D_ship_ais.py itself.

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
VM's public IP is dynamic, switch it to a Standard static Public IP in the
Azure portal first, or this will break whenever the VM restarts) and run
this script, e.g. from 0_Run_Radar_And_Greyscale.py or its own cron entry:
    export AIS_VM_HOST=20.166.89.192
    python3 E_fetch_ship_history.py
"""

import os
import subprocess

VM_HOST = os.environ.get("AIS_VM_HOST", "")
VM_USER = os.environ.get("AIS_VM_USER", "doniall")
VM_SSH_KEY = os.environ.get("AIS_VM_SSH_KEY", os.path.expanduser("~/.ssh/ais_sync"))

LOCAL_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_history.json")


def main():
    if not VM_HOST:
        print("AIS_VM_HOST not set -- skipping ship history fetch; rendering will use "
              "whatever ship_history.json is already on disk (or none).")
        return

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
        return

    if os.path.getsize(tmp_path) == 0:
        print("ship history fetch from VM returned no data -- leaving existing file in place")
        os.remove(tmp_path)
        return

    os.replace(tmp_path, LOCAL_HISTORY_PATH)
    print(f"fetched latest ship history from {VM_HOST}")


if __name__ == "__main__":
    main()
