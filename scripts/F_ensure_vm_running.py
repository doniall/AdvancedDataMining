#!/usr/bin/env python3
"""
F_ensure_vm_running.py

E-InkVirtualMachine runs as an Azure Spot VM (cheaper, but Azure can
preempt/deallocate it at any time it needs the capacity back -- there's
no schedule and no guarantee of when it comes back on its own). This
script checks the VM's power state via the Azure REST API and starts it
if it's found deallocated, so gaps in AIS collection get closed as soon
as the next run of 0_Run_Radar_And_Greyscale.py notices, rather than
sitting off indefinitely.

This does NOT prevent eviction, and starting a Spot VM can itself be
refused if Azure still has no capacity -- it just shortens the outage in
the common case where capacity freed back up.

Needs an Azure AD app registration (service principal) with a *custom*
role scoped to only this one VM -- deliberately not "Virtual Machine
Contributor" or similar, since that would also grant delete/resize/etc.
One-time setup (run from wherever you have az cli logged in):

    SUB_ID=$(az account show --query id -o tsv)
    VM_ID="/subscriptions/$SUB_ID/resourceGroups/WestEuropeResources/providers/Microsoft.Compute/virtualMachines/E-InkVirtualMachine"

    cat > /tmp/ais-vm-starter-role.json <<EOF
    {
      "Name": "AIS VM Starter",
      "IsCustom": true,
      "Description": "Read power state and start E-InkVirtualMachine only",
      "Actions": [
        "Microsoft.Compute/virtualMachines/start/action",
        "Microsoft.Compute/virtualMachines/read",
        "Microsoft.Compute/virtualMachines/instanceView/read"
      ],
      "NotActions": [],
      "AssignableScopes": ["$VM_ID"]
    }
    EOF
    az role definition create --role-definition /tmp/ais-vm-starter-role.json
    az ad sp create-for-rbac --name ais-vm-restarter --role "AIS VM Starter" --scopes "$VM_ID"

The last command prints appId / password / tenant -- set those below as
AIS_VM_CLIENT_ID / AIS_VM_CLIENT_SECRET / AIS_VM_TENANT_ID (environment
variables, same pattern as AISSTREAM_API_KEY), plus AIS_VM_SUBSCRIPTION_ID
from `az account show --query id -o tsv`. Treat the client secret exactly
like the AIS API key or the SSH sync key -- don't commit it, don't paste
it into chat.
"""

import json
import os
import urllib.error
import urllib.request

TENANT_ID = os.environ.get("AIS_VM_TENANT_ID", "")
CLIENT_ID = os.environ.get("AIS_VM_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AIS_VM_CLIENT_SECRET", "")
SUBSCRIPTION_ID = os.environ.get("AIS_VM_SUBSCRIPTION_ID", "")

RESOURCE_GROUP = os.environ.get("AIS_VM_RESOURCE_GROUP", "WestEuropeResources")
VM_NAME = os.environ.get("AIS_VM_NAME", "E-InkVirtualMachine")

API_VERSION = "2023-09-01"
_VM_BASE = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
    f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.Compute/virtualMachines/{VM_NAME}"
)


def _request(url, method="GET", token=None, data=None):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if data is not None:
        data = data.encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read()
        return json.loads(body) if body else {}


def _get_token():
    url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    body = (
        f"grant_type=client_credentials&client_id={CLIENT_ID}"
        f"&client_secret={CLIENT_SECRET}&scope=https://management.azure.com/.default"
    )
    return _request(url, method="POST", data=body)["access_token"]


def _power_state(token):
    url = f"{_VM_BASE}/instanceView?api-version={API_VERSION}"
    view = _request(url, token=token)
    for status in view.get("statuses", []):
        code = status.get("code", "")
        if code.startswith("PowerState/"):
            return code.split("/", 1)[1]
    return "unknown"


def _start_vm(token):
    url = f"{_VM_BASE}/start?api-version={API_VERSION}"
    _request(url, method="POST", token=token)


def main():
    if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, SUBSCRIPTION_ID]):
        print("AIS_VM_TENANT_ID / AIS_VM_CLIENT_ID / AIS_VM_CLIENT_SECRET / "
              "AIS_VM_SUBSCRIPTION_ID not all set -- skipping VM power check "
              "(see the setup notes at the top of this file)")
        return

    try:
        token = _get_token()
        state = _power_state(token)
        if state == "running":
            print("E-InkVirtualMachine is running")
            return
        print(f"E-InkVirtualMachine power state is '{state}' -- requesting start "
              f"(likely a Spot eviction; may fail again if Azure still has no capacity)")
        _start_vm(token)
        print("start requested -- it can take a minute or two to come back up, "
              "and AIS data will resume once its cron catches up")
    except (urllib.error.URLError, urllib.error.HTTPError, KeyError, ValueError) as e:
        print(f"VM power check/start failed ({e})")


if __name__ == "__main__":
    main()
