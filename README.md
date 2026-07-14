# ARSO radar timelapse archiver + viewer

Collects the ARSO rain-radar animated GIF over time, extracts it into
individually addressable, timestamped frames, and serves them to a small
frontend that lets you scrub back to any date/time that's been archived.

## Quick facts this design is based on

From the GIF you fetched: **23 frames, 5-minute spacing → ~110 minutes of
coverage per fetch**, not 90. That matters for scheduling: it means a
90-minute cron already has ~20 minutes of built-in overlap before you even
count the "retry every minute if unchanged" loop, so nothing is lost to
normal GitHub Actions scheduling jitter. The workflow ships with a 90-minute
schedule (matching what you originally asked for) with a one-line note on
how to shorten it if you want more margin. See `.github/workflows/fetch-radar.yml`.

## Why frames-not-blobs (the storage question)

You asked whether to decompress → concatenate → recompress the archive on
every run, or just zip the GIFs up. Neither, and here's why:

- **Concatenate + recompress every run** is O(archive size) of work, every
  90 minutes, forever. By the time you have a year of data, every run is
  re-encoding a year of video just to add 20 minutes of new radar. It also
  makes "give me the frame for 2026-03-04 14:20" awkward -- you'd have to
  decode into the middle of a huge file to find it.
- **Zip the raw GIFs** is simple but wasteful: consecutive GIFs overlap by
  ~20 of their 23 frames, so you'd be storing the same frame's pixels
  20+ times over.
- **What this repo does instead**: every fetch is OCR'd frame-by-frame, and
  only frames whose timestamp isn't already on disk get saved, as
  individual WebP files (`data/frames/YYYY/MM/DD/HHMM.webp`). Work per run
  is O(new frames), i.e. constant, regardless of how big the archive gets.
  It's also exactly what a "jump to this date/time" frontend wants: fetch
  one small file, not a giant video.
- A **per-day JSON index** (`data/index/YYYY-MM-DD.json`) lists what frames
  exist for that day, plus `data/index/available-days.json` lists which
  days have any data at all. The frontend loads a ~10-20KB index for the
  day you're viewing, never the whole archive.
- If you later want compact "play the whole day" video instead of fetching
  288 loose files, `scripts/consolidate_day.py` (wired up as the optional
  `consolidate.yml` workflow) packs a finished day into one small VP9/WebM
  file with ffmpeg and removes the loose frames. This is opt-in -- delete
  `consolidate.yml` if you'd rather keep every frame individually
  browsable forever. It costs some size (WebP frames only cost ~2-4MB/day,
  which git handles fine for a year or more before it's worth bothering).

### If the repo eventually gets *really* big

Git isn't a great fit for millions of small binary files long-term (repo
clone time grows, and binary diffs don't compress across commits). Two
upgrade paths if/when that matters, in order of effort:

1. **Git LFS** for `data/frames/**` -- minimal code change, keeps the same
   repo structure, GitHub's free tier gives 1GB storage / 1GB bandwidth a
   month (paid tiers beyond that).
2. **Move frame storage to object storage** (Cloudflare R2, S3, etc.) and
   keep only the JSON indices in git. More setup (a bucket + credentials
   as a repo secret + a few lines changed in `fetch_and_process.py`'s save
   step) but scales indefinitely and is usually cheaper than LFS bandwidth
   at real scale. Not implemented here since it adds an external
   dependency you may not want yet -- flagged as a clear upgrade path.

## Setup

1. Push this repo to GitHub.
2. **Settings → Actions → General → Workflow permissions → "Read and write
   permissions"**. Without this, the bot commit step in `fetch-radar.yml`
   will fail with a 403 -- this is the single most common gotcha.
3. **Calibrate the OCR crop box.** The coordinates in
   `TIMESTAMP_CROP_BOX` (top of `scripts/fetch_and_process.py`) are a
   starting guess, not a verified value -- fetch a live frame and check:
   ```bash
   pip install -r scripts/requirements.txt
   python scripts/fetch_and_process.py --dump-frame 0 --debug-crop-box
   ```
   This saves `/tmp/frame_debug.png` with the current crop box drawn in
   red. Adjust the four numbers until the red box tightly frames just the
   two overlay text lines, then rerun to confirm OCR reads it correctly.
4. Trigger the workflow manually once (Actions tab → "Fetch ARSO radar
   frames" → Run workflow) to confirm the whole pipeline works before
   waiting for the schedule.
5. (Optional) enable `consolidate.yml` once you're comfortable with the
   fetch pipeline.

## Data layout

```
data/
  frames/2026/07/14/1505.webp      # one file per archived frame
  index/2026-07-14.json            # {"date": "...", "frames": [{"time": "15:05", "file": "..."}]}
  index/available-days.json        # {"days": ["2026-07-13", "2026-07-14", ...]}
  videos/2026-07-13.webm           # only present for days consolidate.yml has processed
  state/latest.json                # internal dedup state (etag/hash/latest timestamp seen)
```

This is the contract the frontend (`web/index.html`) is built against --
any other script/app can read the same JSON files directly (e.g. via
`raw.githubusercontent.com` or GitHub Pages) with no API server needed.

## Known gaps / things left as-is on purpose

- OCR preprocessing uses a fixed brightness threshold, validated against a
  synthetic overlay in testing but not against a real downloaded frame
  (this sandbox can't reach `meteo.arso.gov.si`). If real frames render the
  overlay differently (e.g. semi-transparent background), you may need to
  loosen `_preprocess_for_ocr`'s threshold or crop box.
- The frontend's "is it currently raining at this location" icon feature is
  intentionally left as empty stub functions, per your request -- see
  `initWeatherIconState()` in `web/index.html`.
