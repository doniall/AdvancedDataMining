import A_met_radar_probe
import B_ireland_radar_greyscale
import E_fetch_ship_history

#A_met_radar_probe

print("Gathering Met Éireann's rainfall radar snapshots")
try:
    A_met_radar_probe.main()
except Exception as e:
    print(f"Met Éireann radar fetch failed ({e})")


print("Fetching AIS ship history from the Azure VM")
try:
    E_fetch_ship_history.main()
except Exception as e:
    print(f"ship history fetch failed ({e}); rendering with whatever's already on disk")

print("Starting Greyscale")
B_ireland_radar_greyscale.VIEW = "landscape"
try:
    B_ireland_radar_greyscale.main()
except Exception as e:
    print(f"Greyscale production failed ({e})")

print("Goodbye!")
