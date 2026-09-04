import A_met_radar_probe
import B_ireland_radar_greyscale
import D_ship_ais

#A_met_radar_probe

A_met_radar_probe.main()

try:
    D_ship_ais.main()
except Exception as e:
    print(f"ship position fetch failed ({e}); rendering without ship markers")

print("hello")
B_ireland_radar_greyscale.VIEW = "landscape"
B_ireland_radar_greyscale.main()

print("goodbye")
