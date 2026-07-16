#!/usr/bin/env python3
"""
Nightly housekeeping: build one lossless WebP animation per day from the
individual frame files, then record the completed animation in the day
index so the frontend can switch to video-based playback.

- Reads each day's index, loads all its frame images (in chronological
  order), and saves them as a single radar-YYYY-MM-DD.webp animation.
- Adds an "animation" key to the day index pointing at the new file.
- Optionally deletes the loose per-frame files (currently disabled – you
  can enable it later if you want to save disk space).
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import List

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAMES_DIR = REPO_ROOT / "data" / "frames"
INDEX_DIR = REPO_ROOT / "data" / "index"
ANIMATION_DIR = REPO_ROOT / "data" / "animation"
AVAILABLE_DAYS_FILE = INDEX_DIR / "available-days.json"

FRAME_DURATION_MS = 250       # 4 frames per second
WEBP_LOSSLESS = True
WEBP_METHOD = 6               # slowest / best compression

# --------------------------------------------------------------------------
def load_day_index(d: date) -> dict:
    """Return the JSON index for a day (empty dict if missing)."""
    idx_path = INDEX_DIR / f"{d.isoformat()}.json"
    if idx_path.exists():
        return json.loads(idx_path.read_text())
    return {"date": d.isoformat(), "frames": []}

def save_day_index(d: date, idx: dict) -> None:
    """Write an updated day index back to disk."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    idx["frames"].sort(key=lambda f: f["time"])
    (INDEX_DIR / f"{d.isoformat()}.json").write_text(
        json.dumps(idx, separators=(",", ":"))
    )

def consolidate_day(d: date) -> bool:
    """
    Build a daily WebP animation for day `d`.
    Returns True if something was created, False otherwise.
    """
    idx = load_day_index(d)
    frames_meta = idx.get("frames", [])

    # Already consolidated?
    if "animation" in idx:
        print(f"  {d} already consolidated → skip")
        return False

    if len(frames_meta) < 2:
        print(f"  {d} has fewer than 2 frames → skip")
        return False

    # Load images in chronological order
    images: List[Image.Image] = []
    for meta in frames_meta:
        file_path = meta.get("file")
        if file_path is None:
            print(f"  WARNING: {d} frame {meta['time']} has no 'file' entry")
            continue
        full_path = REPO_ROOT / file_path
        if not full_path.exists():
            print(f"  WARNING: {d} frame {meta['time']} missing file {file_path}")
            continue
        try:
            img = Image.open(full_path).convert("RGB")
            images.append(img)
        except Exception as exc:
            print(f"  ERROR loading {file_path}: {exc}")
            return False

    if len(images) < 2:
        print(f"  {d} not enough valid images → skip")
        return False

    # Save as animation
    ANIMATION_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ANIMATION_DIR / f"{d.isoformat()}.webp"

    first = images[0]
    rest = images[1:] if len(images) > 1 else []
    first.save(
        out_path,
        "WEBP",
        save_all=True,
        append_images=rest,
        duration=FRAME_DURATION_MS,
        loop=0,                # infinite loop
        lossless=WEBP_LOSSLESS,
        quality=100 if WEBP_LOSSLESS else 70,
        method=WEBP_METHOD,
    )

    # Record the animation in the day index
    relative_path = str(out_path.relative_to(REPO_ROOT))
    idx["animation"] = relative_path

    # Optional: delete loose frame files after successful consolidation.
    # Uncomment this block when you're ready to reclaim disk space.
    # =========================================================================
    # for meta in frames_meta:
    #     if "file" in meta:
    #         loose_file = REPO_ROOT / meta.pop("file")
    #         if loose_file.exists():
    #             loose_file.unlink()
    #             print(f"    deleted {loose_file}")
    # =========================================================================

    save_day_index(d, idx)

    file_size_kb = out_path.stat().st_size / 1024
    print(f"  ✓ {d}: {len(images)} frames → {out_path.name} ({file_size_kb:.0f} KB)")
    return True


def consolidate_all_yesterday_and_before() -> int:
    """
    Consolidate every available day that is **strictly before today**
    (i.e. the day is fully complete). Returns the number of days processed.
    """
    if not AVAILABLE_DAYS_FILE.exists():
        print("No available-days.json yet – nothing to do.")
        return 0

    days_data = json.loads(AVAILABLE_DAYS_FILE.read_text())
    all_days = sorted(days_data.get("days", []))

    today = date.today()
    count = 0

    for day_iso in all_days:
        d = date.fromisoformat(day_iso)
        if d >= today:
            print(f"  {d} is today or future → skip")
            continue
        if consolidate_day(d):
            count += 1

    return count


# --------------------------------------------------------------------------
if __name__ == "__main__":
    # Optional: pass a specific date as YYYY-MM-DD
    if len(sys.argv) > 1:
        target = date.fromisoformat(sys.argv[1])
        if target >= date.today():
            print(f"Error: {target} is not a completed day (must be before today)")
            sys.exit(1)
        consolidate_day(target)
    else:
        # Default: all days up to yesterday
        n = consolidate_all_yesterday_and_before()
        print(f"\nDone – consolidated {n} day(s).")