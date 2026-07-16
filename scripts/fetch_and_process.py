#!/usr/bin/env python3
"""
ARSO rain-radar frame collector – per‑frame saving only.

- First run: extract all unique frames from the GIF, save as individual
  lossless WebP files, update per‑day indexes.
- Subsequent runs: fetch the GIF until it changes, extract ONLY the last
  frame, save it if it doesn't already exist.
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

TIMESTAMP_CROP_BOX = (0, 0, 230, 46)

RETRY_SLEEP_SECONDS = 30
MAX_RETRIES = 12          # 6 minutes total

REQUEST_TIMEOUT = 45

WEBP_LOSSLESS = True
WEBP_METHOD = 6

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
# State (shared with consolidation script – keep it a plain dict)
# --------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_gif_md5": None, "latest_frame_ts": None}

def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# HTTP fetch
# --------------------------------------------------------------------------

def fetch_gif(last_md5: Optional[str]) -> Optional[tuple[bytes, str]]:
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
    im = Image.open(io.BytesIO(gif_bytes))
    return [frame.convert("RGB") for frame in ImageSequence.Iterator(im)]

def _preprocess_for_ocr(crop: Image.Image) -> Image.Image:
    gray = crop.convert("L")
    gray = gray.resize((gray.width * 3, gray.height * 3), Image.LANCZOS)
    return gray.point(lambda p: 255 if p > 140 else 0)

def ocr_timestamp(frame: Image.Image) -> Optional[str]:
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
# Per‑frame storage
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
    saved = 0
    by_day: dict[date, dict] = {}

    for pf in parsed_frames:
        d = pf.dt.date()
        if d not in by_day:
            by_day[d] = load_day_index(d)
        idx = by_day[d]
        time_str = f"{pf.dt:%H:%M}"
        if any(f["time"] == time_str for f in idx["frames"]):
            continue

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

def run_once() -> bool:
    state = load_state()
    last_md5 = state["last_gif_md5"]

    # 1. Fetch genuinely new GIF
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
            return False
        log.info("GIF unchanged, sleeping %ds", RETRY_SLEEP_SECONDS)
        time.sleep(RETRY_SLEEP_SECONDS)

    log.info("got new GIF, MD5=%s", new_md5)

    # 2. Extract frame(s)
    is_first_run = state["latest_frame_ts"] is None

    if is_first_run:
        # First run: dump all unique frames
        frames = extract_frames(gif_bytes)
        stamped = []
        seen_ts = set()
        for f in frames:
            ts = ocr_timestamp(f)
            if ts and ts not in seen_ts:
                seen_ts.add(ts)
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M UTC")
                stamped.append(ParsedFrame(dt=dt, image=f))
        if not stamped:
            log.error("no timestamps readable")
            return False
        stamped.sort(key=lambda pf: pf.dt)
        saved = save_new_frames(stamped)
        latest_ts = stamped[-1].dt.strftime("%Y-%m-%d %H:%M UTC")
        log.info("first run: saved %d frames, latest=%s", saved, latest_ts)

    else:
        # Consecutive run: only last frame is new
        all_frames = extract_frames(gif_bytes)
        if not all_frames:
            log.error("empty GIF")
            return False
        new_frame = all_frames[-1]
        ts = ocr_timestamp(new_frame)
        if ts is None:
            log.warning("OCR failed on last frame, skipping")
            return False
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M UTC")
        pf = ParsedFrame(dt=dt, image=new_frame)
        saved = save_new_frames([pf])
        latest_ts = ts
        log.info("saved %d frame(s), latest=%s", saved, latest_ts)

    # 3. Update state (no animation keys touched)
    state["last_gif_md5"] = new_md5
    state["latest_frame_ts"] = latest_ts
    save_state(state)
    return True


if __name__ == "__main__":
    if "--dump-frame" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--dump-frame") + 1])
        # debug helper (keep if needed)
        sys.exit(0)
    run_once()
    sys.exit(0)