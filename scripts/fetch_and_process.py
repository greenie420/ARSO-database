#!/usr/bin/env python3
"""
ARSO rain-radar timelapse collector.

What this does, once per run:
  1. Fetch the animated GIF from ARSO.
  2. If it's byte-identical to what we saw last time, wait a bit and retry
     (a few times) instead of giving up immediately -- ARSO doesn't always
     refresh exactly on schedule.
  3. Once we have a genuinely new GIF, walk every frame (not just the last
     one), OCR the timestamp burned into each frame, and save ONLY the
     frames whose timestamp we don't already have on disk.
  4. Update a small per-day JSON index so the frontend can ask "what frames
     exist for 2026-07-14?" without ever loading the whole archive.

Why per-frame dedup instead of "concatenate + recompress the whole archive
every run": each fetch mostly overlaps the previous one (same ~110 minutes
of radar, shifted a few frames forward). Re-decoding and re-encoding a
growing blob on every run is O(archive size) work forever. Extracting only
the handful of frames you haven't seen yet and writing them once, as
individually addressable files, is O(new frames) work forever -- and it's
exactly what a "go to this date/time" frontend wants to fetch anyway.
See README.md for the full rationale and the optional nightly consolidation
step if you want compact "play the whole day" video files too.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

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

# --- OCR crop box -----------------------------------------------------------
# Pixel box (left, top, right, bottom) of the timestamp overlay within a
# single decoded frame. This is a STARTING GUESS based on the sample corner
# crop -- calibrate it against a real frame before relying on this in
# production. Run:
#   python scripts/fetch_and_process.py --dump-frame 0 --debug-crop-box
# which saves /tmp/frame_debug.png with the current crop box drawn on it in
# red, so you can visually check/adjust the numbers below.
TIMESTAMP_CROP_BOX = (0, 0, 230, 46)

# Retry-if-unchanged behaviour (matches the "every 1.5h, retry every 1 min
# if nothing changed" spec). See README.md for the scheduling discussion --
# with 23 frames at 5-minute spacing the GIF covers ~110 minutes, so a
# 90-minute cron already has slack built in even before these retries.
RETRY_SLEEP_SECONDS = 60
MAX_RETRIES = 15  # ~15 minutes of retrying before giving up for this run

REQUEST_TIMEOUT = 30

TIMESTAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})\D{1,3}(\d{2}:\d{2})\D{0,6}UTC", re.IGNORECASE)

WEBP_QUALITY = 90  # lossless=False, quality=90 is a good size/fidelity tradeoff for radar PNGs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar")


# --------------------------------------------------------------------------
# HTTP fetch
# --------------------------------------------------------------------------

@dataclass
class FetchState:
    etag: Optional[str] = None
    last_modified: Optional[str] = None
    last_gif_md5: Optional[str] = None
    latest_frame_ts: Optional[str] = None  # e.g. "2026-07-14 15:05 UTC"


def load_state() -> FetchState:
    if STATE_FILE.exists():
        data = json.loads(STATE_FILE.read_text())
        return FetchState(**data)
    return FetchState()


def save_state(state: FetchState) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state.__dict__, indent=2))


def fetch_gif(state: FetchState) -> tuple[Optional[bytes], FetchState]:
    """GET the GIF. Uses conditional headers when the server supports them
    so an "unchanged" check is cheap; falls back to a content hash
    comparison otherwise (ARSO's server doesn't reliably send validators for
    this endpoint, so treat conditional headers as a nice-to-have, not the
    only line of defense)."""
    headers = {}
    if state.etag:
        headers["If-None-Match"] = state.etag
    if state.last_modified:
        headers["If-Modified-Since"] = state.last_modified

    resp = requests.get(GIF_URL, headers=headers, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    if resp.status_code == 304:
        return None, state  # server confirms: unchanged

    content = resp.content
    md5 = hashlib.md5(content).hexdigest()

    new_state = FetchState(
        etag=resp.headers.get("ETag", state.etag),
        last_modified=resp.headers.get("Last-Modified", state.last_modified),
        last_gif_md5=md5,
        latest_frame_ts=state.latest_frame_ts,
    )

    if md5 == state.last_gif_md5:
        return None, new_state  # byte-identical to last time we looked

    return content, new_state


# --------------------------------------------------------------------------
# Frame extraction + OCR
# --------------------------------------------------------------------------

def extract_frames(gif_bytes: bytes) -> list[Image.Image]:
    """Return every frame of the GIF as a fully-composited RGB image, in
    playback order. Uses ImageSequence so GIF disposal handling is done by
    Pillow rather than by us re-implementing frame compositing."""
    im = Image.open(io.BytesIO(gif_bytes))
    frames = [frame.convert("RGB") for frame in ImageSequence.Iterator(im)]
    return frames


def _preprocess_for_ocr(crop: Image.Image) -> Image.Image:
    gray = crop.convert("L")
    gray = gray.resize((gray.width * 3, gray.height * 3), Image.LANCZOS)
    # Simple binarization. The overlay in the sample is dark text on a
    # light box, which this threshold handles; if ARSO changes the overlay
    # style you may need an adaptive threshold instead (see README).
    return gray.point(lambda p: 255 if p > 140 else 0)


def ocr_timestamp(frame: Image.Image) -> Optional[str]:
    """Crop the overlay region, OCR it, and parse a timestamp like
    '2026-07-14 15:05 UTC'. Returns None if nothing usable was read."""
    import pytesseract  # imported here so --help works even if tesseract isn't installed

    crop = frame.crop(TIMESTAMP_CROP_BOX)
    processed = _preprocess_for_ocr(crop)
    text = pytesseract.image_to_string(
        processed,
        config="--psm 6 -c tessedit_char_whitelist='ARSOIDUTC0123456789:- '",
    )
    match = TIMESTAMP_RE.search(text.replace("\n", " "))
    if not match:
        log.warning("OCR could not parse a timestamp from frame (raw: %r)", text.strip())
        return None
    date_part, time_part = match.groups()
    return f"{date_part} {time_part} UTC"


@dataclass
class ParsedFrame:
    dt: datetime
    image: Image.Image


def parse_all_frames(gif_bytes: bytes) -> list[ParsedFrame]:
    parsed: list[ParsedFrame] = []
    for frame in extract_frames(gif_bytes):
        ts = ocr_timestamp(frame)
        if ts is None:
            continue
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M UTC")
        parsed.append(ParsedFrame(dt=dt, image=frame))
    parsed.sort(key=lambda p: p.dt)  # don't trust playback order, trust the OCR'd clock
    return parsed


# --------------------------------------------------------------------------
# Storage: per-day index + individual frame files
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
    return FRAMES_DIR / f"{dt:%Y}" / f"{dt:%m}" / f"{dt:%d}" / f"{dt:%H%M}.webp"


def save_new_frames(parsed: list[ParsedFrame]) -> int:
    """Save whichever frames aren't already on disk. Returns count saved."""
    saved = 0
    by_day: dict[date, dict] = {}

    for pf in parsed:
        d = pf.dt.date()
        if d not in by_day:
            by_day[d] = load_day_index(d)
        idx = by_day[d]
        time_str = f"{pf.dt:%H:%M}"
        already_have = any(f["time"] == time_str for f in idx["frames"])
        if already_have:
            continue

        out_path = frame_file_path(pf.dt)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pf.image.save(out_path, "WEBP", quality=WEBP_QUALITY, method=6)

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
    """One fetch-with-retries cycle. Returns number of new frames saved."""
    state = load_state()

    for attempt in range(1, MAX_RETRIES + 1):
        log.info("fetch attempt %d/%d", attempt, MAX_RETRIES)
        gif_bytes, state = fetch_gif(state)
        save_state(state)  # persist etag/hash progress even if we retry

        if gif_bytes is not None:
            break

        if attempt == MAX_RETRIES:
            log.info("no change after %d attempts, giving up for this run", MAX_RETRIES)
            return 0

        log.info("GIF unchanged, retrying in %ds", RETRY_SLEEP_SECONDS)
        time.sleep(RETRY_SLEEP_SECONDS)
    else:
        return 0

    parsed = parse_all_frames(gif_bytes)
    if not parsed:
        log.warning("fetched a new GIF but OCR couldn't read any timestamps -- "
                     "check TIMESTAMP_CROP_BOX, the overlay style may have changed")
        return 0

    newest = parsed[-1].dt
    saved = save_new_frames(parsed)

    state.latest_frame_ts = f"{newest:%Y-%m-%d %H:%M} UTC"
    save_state(state)

    log.info("done: %d new frame(s) saved, latest is %s", saved, state.latest_frame_ts)
    return saved


def _debug_dump_frame(frame_index: int, draw_crop_box: bool) -> None:
    """Fetch the current GIF and save one frame to /tmp for visual
    calibration of TIMESTAMP_CROP_BOX. Doesn't touch stored state."""
    resp = requests.get(GIF_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    frames = extract_frames(resp.content)
    frame = frames[frame_index]
    if draw_crop_box:
        from PIL import ImageDraw
        frame = frame.copy()
        ImageDraw.Draw(frame).rectangle(TIMESTAMP_CROP_BOX, outline="red", width=2)
    out = Path("/tmp/frame_debug.png")
    frame.save(out)
    print(f"saved {out} ({len(frames)} frames total in this GIF)")


if __name__ == "__main__":
    if "--dump-frame" in sys.argv:
        idx = int(sys.argv[sys.argv.index("--dump-frame") + 1])
        _debug_dump_frame(idx, draw_crop_box="--debug-crop-box" in sys.argv)
        sys.exit(0)

    n = run_once()
    # Exit code 0 always -- "no new frames this run" is a normal outcome,
    # not a failure. The workflow step after this checks `git status` to
    # decide whether there's anything to commit.
    sys.exit(0)
