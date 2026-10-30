#!/usr/bin/env python3
"""Nagebootste ffmpeg voor de tests van clips.py: schrijft een bestand en meldt time= zoals ffmpeg."""
import os
import sys
import time

args = sys.argv[1:]
out = args[-1]
if "concat" in args:
    lst = args[args.index("-i") + 1]
    folder = os.path.dirname(lst)
    with open(out, "wb") as o:
        for line in open(lst):
            o.write(open(os.path.join(folder, line.strip()[6:-1]), "rb").read())
    sys.exit(0)
busy = os.environ.get("FAKE_BUSY_FILE")
if busy and os.path.exists(busy):   # eerste poging: recorder bezet
    os.remove(busy)
    sys.stderr.write("Server returned 4XX Client Error, but not one of 40{0,1,3,4}\n")
    sys.exit(1)
dur = float(args[args.index("-t") + 1])
sys.stderr.write("Stream #0:0: Video: hevc (Main), yuv420p, 3288x1850\n")
sys.stderr.flush()
for t in range(1, int(dur) + 1):
    sys.stderr.write(f"frame=  1 time=00:00:{t:02d}.00 bitrate= speed=1x\n")
sys.stderr.flush()
with open(out, "wb") as f:
    f.write(b"x" * int(dur))
