
import glob
from PIL import Image


def ListFilesInDirectory (Address):
    #list all files in a location
    from os import walk
    results = next(walk(Address), (None, None, []))[2]  # [] if no file
    return results


GreyscaleRadarImageSubfolder = "1_GreyscalePNG"

files = sorted(ListFilesInDirectory(GreyscaleRadarImageSubfolder))
#remove any entries that are not .png files
png_files = [f for f in files if f.lower().endswith(".png")]
#files = sorted(glob.glob(f"{GreyscaleRadarImageSubfolder}*.png"))          # your frames, in name order
frames = [Image.open(f"{GreyscaleRadarImageSubfolder}/{f}").convert("P", palette=Image.ADAPTIVE) for f in png_files]
frames[0].save(
    "radar.gif",
    save_all=True,
    append_images=frames[1:],
    duration=300,        # milliseconds per frame
    loop=0,              # 0 = loop forever
    disposal=2,
)
