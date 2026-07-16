#!/usr/bin/env python3
"""
ARSO rain-radar frame collector.

Runs every 15 minutes via GitHub Actions:
  1. Fetch the animated GIF from ARSO.
  2. If it's byte-identical to last time, we're done (no new frames).
  3. Otherwise, extract ALL frames, OCR the timestamps, and save only
     frames whose timestamp we don't already have on disk.
  4. Update per-day JSON indexes.

This handles irregular GIF updates gracefully — if ARSO skips an hour,
the next fetch that gets a new GIF will backfill all the new frames at once.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from typing import Optional, List

import requests
from PIL import Image, ImageSequence

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

GIF_URL = "https://meteo.arso.gov.si/uploads/probase/www/observ/radar/si0-rm-anim.gif"

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAMES_DIR = REPO_ROOT / "data" / "frames"
INDEX_DIR = REPO_ROOT / "data" / "index"
STATE_FILE = REPO_ROOT / "data" / "state" / "latest.json"
AVAILABLE_DAYS_FILE = INDEX_DIR / "available-days.json"

# OCR crop box (left, top, right, bottom) of the timestamp overlay
TIMESTAMP_CROP_BOX = (0, 0, 230, 46)

# Retry settings — with 3 retries every 60s, we wait up to 3 min for a
# new GIF. With 15-min cron intervals, this gives us plenty of slack.
RETRY_SLEEP_SECONDS = 60
MAX_RETRIES = 3

REQUEST_TIMEOUT = 45

WEBP_LOSSLESS = True
WEBP_METHOD = 6

# Regex handles OCR misreads (e.g. colon read as digit "3")
TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\D{1,3}(\d{2}).{0,2}?(\d{2})\D{0,6}UTC",
    re.IGNORECASE,
)

SAVE_OCR_DEBUG_IMAGES = False
DEBUG_OCR_IMAGES_DIR = Path("test/_ocr_debug")
_ocr_debug_counter = 0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar")


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_gif_md5": None, "latest_frame_ts": None}

def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# HTTP fetch with MD5-based change detection
# --------------------------------------------------------------------------

def fetch_gif(last_md5: Optional[str]) -> Optional[tuple[bytes, str]]:
    """GET the GIF. Returns (content, md5) if different from last_md5, else None."""
    resp = requests.get(GIF_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    content = resp.content
    md5 = hashlib.md5(content).hexdigest()
    if md5 == last_md5:
        return None
    return content, md5


# --------------------------------------------------------------------------
# Frame extraction + OCR
# --------------------------------------------------------------------------

@dataclass
class ParsedFrame:
    dt: datetime
    image: Image.Image

def extract_frames(gif_bytes: bytes) -> List[Image.Image]:
    """Return all frames of the GIF as RGB images in playback order."""
    im = Image.open(io.BytesIO(gif_bytes))
    return [frame.convert("RGB") for frame in ImageSequence.Iterator(im)]

def _preprocess_for_ocr(crop: Image.Image) -> Image.Image:
    gray = crop.convert("L")
    gray = gray.resize((gray.width * 3, gray.height * 3), Image.LANCZOS)
    return gray.point(lambda p: 255 if p > 140 else 0)

def ocr_timestamp(frame: Image.Image) -> Optional[str]:
    """OCR the timestamp overlay. Returns 'YYYY-MM-DD HH:MM UTC' or None."""
    import pytesseract
    crop = frame.crop(TIMESTAMP_CROP_BOX)
    processed = _preprocess_for_ocr(crop)

    if SAVE_OCR_DEBUG_IMAGES:
        global _ocr_debug_counter
        DEBUG_OCR_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        debug_path = DEBUG_OCR_IMAGES_DIR / f"frame_{_ocr_debug_counter:03d}.png"
        processed.save(debug_path)
        log.info("OCR debug image #%d -> %s", _ocr_debug_counter, debug_path)
        _ocr_debug_counter += 1

    text = pytesseract.image_to_string(processed, config="--psm 6")
    match = TIMESTAMP_RE.search(text.replace("\n", " "))
    if not match:
        log.warning("OCR could not parse timestamp (raw: %r)", text.strip())
        return None
    date_part, hour_part, minute_part = match.groups()
    return f"{date_part} {hour_part}:{minute_part} UTC"


# --------------------------------------------------------------------------
# Per-frame storage
# --------------------------------------------------------------------------

def day_index_path(d: date) -> Path:
    return INDEX_DIR / f"{d.isoformat()}.json"

def load_day_index(d: date) -> dict:
    p = day_index_path(d)
    if p.exists():
        return json.loads(p.read_text())
    return {"date": d.isoformat(), "frames": []}

def save_day_index(d: date, idx: dict) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    idx["frames"].sort(key=lambda f: f["time"])
    day_index_path(d).write_text(json.dumps(idx, separators=(",", ":")))

def mark_day_available(d: date) -> None:
    AVAILABLE_DAYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    days = []
    if AVAILABLE_DAYS_FILE.exists():
        days = json.loads(AVAILABLE_DAYS_FILE.read_text()).get("days", [])
    iso = d.isoformat()
    if iso not in days:
        days.append(iso)
        days.sort()
        AVAILABLE_DAYS_FILE.write_text(json.dumps({"days": days}, separators=(",", ":")))

def frame_file_path(dt: datetime) -> Path:
    return FRAMES_DIR / f"{dt:%Y}" / f"{dt:%m}" / f"{dt:%d}" / f"{dt:%Hh%Mm}.webp"

def save_new_frames(parsed_frames: List[ParsedFrame]) -> int:
    """Save whichever frames aren't already on disk. Returns count saved."""
    saved = 0
    by_day: dict[date, dict] = {}

    for pf in parsed_frames:
        d = pf.dt.date()
        if d not in by_day:
            by_day[d] = load_day_index(d)
        idx = by_day[d]
        time_str = f"{pf.dt:%H:%M}"
        if any(f["time"] == time_str for f in idx["frames"]):
            continue  # already stored

        out_path = frame_file_path(pf.dt)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if WEBP_LOSSLESS:
            pf.image.save(out_path, "WEBP", lossless=True, quality=100, method=WEBP_METHOD)
        else:
            pf.image.save(out_path, "WEBP", quality=70, method=WEBP_METHOD)

        idx["frames"].append({
            "time": time_str,
            "file": str(out_path.relative_to(REPO_ROOT)),
        })
        saved += 1
        log.info("saved new frame %s -> %s", pf.dt.isoformat(), out_path)

    for d, idx in by_day.items():
        save_day_index(d, idx)
        mark_day_available(d)

    return saved


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def run_once() -> int:
    """Fetch GIF, extract frames, save new ones. Returns count of new frames."""
    state = load_state()
    last_md5 = state["last_gif_md5"]

    # 1. Fetch with retries
    gif_bytes = None
    new_md5 = None
    for attempt in range(1, MAX_RETRIES + 1):
        log.info("fetch attempt %d/%d", attempt, MAX_RETRIES)
        result = fetch_gif(last_md5)
        if result is not None:
            gif_bytes, new_md5 = result
            break
        if attempt == MAX_RETRIES:
            log.info("GIF unchanged after %d attempts, nothing to do", MAX_RETRIES)
            return 0
        log.info("GIF unchanged, sleeping %ds", RETRY_SLEEP_SECONDS)
        time.sleep(RETRY_SLEEP_SECONDS)

    log.info("got new GIF, MD5=%s", new_md5)

    # 2. Extract all frames, OCR, deduplicate by timestamp
    frames = extract_frames(gif_bytes)
    if not frames:
        log.error("GIF contains no frames")
        return 0

    parsed: List[ParsedFrame] = []
    seen_ts = set()
    for f in frames:
        ts = ocr_timestamp(f)
        if ts and ts not in seen_ts:
            seen_ts.add(ts)
            dt = datetime.strptime(ts, "%Y-%m-%d %H:%M UTC")
            parsed.append(ParsedFrame(dt=dt, image=f))

    if not parsed:
        log.warning("could not read any timestamps — check TIMESTAMP_CROP_BOX")
        return 0

    parsed.sort(key=lambda pf: pf.dt)

    # 3. Save only new frames (per-frame dedup by time)
    saved = save_new_frames(parsed)

    # 4. Update state
    state["last_gif_md5"] = new_md5
    state["latest_frame_ts"] = f"{parsed[-1].dt:%Y-%m-%d %H:%M} UTC"
    save_state(state)

    log.info("done: %d new frame(s) saved, latest=%s", saved, state["latest_frame_ts"])
    return saved


if __name__ == "__main__":
    if "--dump-frame" in sys.argv:
        # Debug helper kept for calibration
        idx = int(sys.argv[sys.argv.index("--dump-frame") + 1])
        resp = requests.get(GIF_URL, timeout=REQUEST_TIMEOUT)
        frames = extract_frames(resp.content)
        frame = frames[idx]
        if "--debug-crop-box" in sys.argv:
            from PIL import ImageDraw
            frame = frame.copy()
            ImageDraw.Draw(frame).rectangle(TIMESTAMP_CROP_BOX, outline="red", width=2)
        out = Path("/tmp/frame_debug.png")
        frame.save(out)
        print(f"saved {out} ({len(frames)} frames total)")
        sys.exit(0)

    n = run_once()
    sys.exit(0)