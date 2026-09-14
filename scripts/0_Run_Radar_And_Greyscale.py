import A_met_radar_probe
import B_ireland_radar_greyscale
import E_fetch_ship_history
import F_ensure_vm_running
import G_solis_fetch
import H_sensecraft_push

print("Checking the AIS VM is running")
try:
    F_ensure_vm_running.main()
except Exception as e:
    print(f"VM power check failed ({e})")

print("Fetching AIS ship history from the Azure VM")
try:
    E_fetch_ship_history.main()
except Exception as e:
    print(f"ship history fetch failed ({e}); rendering with whatever's already on disk")

print("Fetching SolisCloud solar status")
try:
    G_solis_fetch.main()
except Exception as e:
    print(f"SolisCloud fetch failed ({e}); rendering with whatever's already on disk")

# runs right before Greyscale, not at the very start -- keeps the radar
# frames as fresh as possible relative to when they're actually rendered,
# rather than possibly sitting around while the AIS/Solis fetches above run
print("Gathering Met Éireann's rainfall radar snapshots")
try:
    A_met_radar_probe.main()
except Exception as e:
    print(f"Met Éireann radar fetch failed ({e})")

print("Starting Greyscale")
B_ireland_radar_greyscale.VIEW = "landscape"
try:
    B_ireland_radar_greyscale.main()
except Exception as e:
    print(f"Greyscale production failed ({e})")

# runs AFTER Greyscale, not before -- H_sensecraft_push.py reads whatever's
# newest in 1_GreyscalePNG/, and B_ireland_radar_greyscale.py above is what
# actually writes new frames there. Pushing first would always push last
# cycle's frames, missing whatever this run's own Greyscale step just
# produced.
print("Pushing SolisCloud status to SenseCraft")
try:
    H_sensecraft_push.main()
except Exception as e:
    print(f"SenseCraft push failed ({e})")

print("Goodbye!")
