#!/usr/bin/env python3
"""
Optional storage housekeeping: pack one day's individually-saved frames into
a single compact WebM (VP9) file, then remove the loose per-frame files.

Why this is optional and separate from fetch_and_process.py:
  - The frontend's "jump to a specific date/time" feature works directly off
    the loose WebP frames + the day's JSON index. That's the source of
    truth and needs no video decoding.
  - Loose frames are cheap for a while (a full day at 5-min resolution is
    288 frames; at ~8-15KB each that's roughly 2-4MB/day, ~1GB/year). Git
    handles that fine for a year or two. This script exists for when you'd
    rather trade a bit of the archive's browsability for a much smaller
    repo -- e.g. once you're keeping years of history, or you want a smooth
    "play this whole day" scrubber without fetching 288 separate files.
  - Run it (see consolidate.yml) only for days that are fully in the past
    (yesterday or older), never for today, since today's index is still
    being appended to by fetch_and_process.py.

After consolidation, the day's index gains a "video" field pointing at the
packed file; frame-level "file" entries are kept in the index (so old links
don't break) but the loose files on disk are deleted. If you'd rather keep
loose files forever and never run this, that's a perfectly reasonable
choice too -- just don't wire up consolidate.yml.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAMES_DIR = REPO_ROOT / "data" / "frames"
INDEX_DIR = REPO_ROOT / "data" / "index"
VIDEOS_DIR = REPO_ROOT / "data" / "videos"


def consolidate_day(d: date) -> None:
    idx_path = INDEX_DIR / f"{d.isoformat()}.json"
    if not idx_path.exists():
        print(f"no index for {d}, nothing to do")
        return

    idx = json.loads(idx_path.read_text())
    frames = sorted(idx["frames"], key=lambda f: f["time"])
    if len(frames) < 2:
        print(f"{d}: fewer than 2 frames, skipping consolidation")
        return

    if "video" in idx:
        print(f"{d}: already consolidated")
        return

    # Build an ffmpeg concat list (frames may have gaps -- e.g. a missed
    # 5-min slot -- so we don't assume a fixed frame count, just fixed
    # per-frame duration).
    list_file = VIDEOS_DIR / f"{d.isoformat()}.txt"
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    with open(list_file, "w") as f:
        for fr in frames:
            f.write(f"file '{(REPO_ROOT / fr['file']).resolve()}'\n")
            f.write("duration 0.2\n")
        # ffmpeg concat demuxer needs the last file repeated without a duration
        f.write(f"file '{(REPO_ROOT / frames[-1]['file']).resolve()}'\n")

    out_path = VIDEOS_DIR / f"{d.isoformat()}.webm"
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "32", "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    print("running:", " ".join(cmd))
    subprocess.run(cmd, check=True)
    list_file.unlink()

    # Update index: record the video, drop the loose files (keep their
    # timestamps in the index so consumers still know exactly what's
    # covered, just without a per-frame file link).
    idx["video"] = str(out_path.relative_to(REPO_ROOT))
    for fr in frames:
        loose = REPO_ROOT / fr["file"]
        if loose.exists():
            loose.unlink()
        fr.pop("file", None)
    idx["frames"] = frames
    idx_path.write_text(json.dumps(idx, separators=(",", ":")))
    print(f"{d}: consolidated {len(frames)} frames -> {out_path} "
          f"({out_path.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    # Default: consolidate yesterday (UTC). Pass an explicit YYYY-MM-DD to
    # do a different day, e.g. for backfilling.
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
    else:
        target = date.today() - timedelta(days=1)
    consolidate_day(target)
