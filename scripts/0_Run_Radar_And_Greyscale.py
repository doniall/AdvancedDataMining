import A_met_radar_probe
import B_ireland_radar_greyscale
import D_ship_ais

#A_met_radar_probe

print("Gathering Met Éireann's rainfall radar snapshots")
try:
    A_met_radar_probe.main()
except Exception as e:
    print(f"Met Éireann radar fetch failed ({e})")


print("Gathering AIS data for ship positions")
try:
    D_ship_ais.main()
except Exception as e:
    print(f"ship position fetch failed ({e}); rendering without ship markers")

print("Starting Greyscale")
B_ireland_radar_greyscale.VIEW = "landscape"
try:
    B_ireland_radar_greyscale.main()
except Exception as e:
    print(f"Greyscale production failed ({e})")

print("Goodbye!")
