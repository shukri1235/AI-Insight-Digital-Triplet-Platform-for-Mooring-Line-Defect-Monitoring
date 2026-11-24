# AI Insights Digital Triplet Platform: Mooring Line Defect Monitoring

YOLOv11 + FastAPI + React digital twin for offshore mooring-line inspection with a BlueROV2.
Two isolated pipelines (live / batch) share one GPU through an arbiter. Inspection state lives in
SQLite (WAL), and ROV sensor data lives in a separate sensor database.

```
backend/
  main.py           FastAPI routes (REST + WebSocket + MJPEG)
  config.py         every path/tunable; paths are relative to this file, never the cwd
  database.py       inspection DB: videos, analysis runs, detections, reports (+ auto-migration)
  sensors_db.py     SEPARATE sensor DB (fyp_sensors.db): ROV samples + vehicle events
  rov_link.py       BlueROV2 / ArduSub MAVLink link: state machine, telemetry, per-dive .tlog
  tlog_import.py    attach a QGC/BlueOS .tlog to a historical dive (aligned to the video)
  gpu_arbiter.py    one thread owns YOLO + GPU; live frames first, batch in small chunks
  model_manager.py  best.pt discovery, GPU warm-up, CPU fallback, class mapping
  live_stream.py    PIPELINE A: capture / inference / clip-writer / DB-writer threads
  batch_worker.py   PIPELINE B: analysis runs (threshold recorded), checkpoint + resume
  ingest.py         in-place import (no copy), background SHA-256, server file browser
  reports.py        Inspection Report Assistant (facts packet, Gemini, grounding check, HTML)
  report_pdf.py     PDF export (reportlab) and client report templates (report_templates/*.json)
  test_mode.py      Test Mode: video streamed as the ROV camera (UDP) + BlueROV2 simulator, TEST tagging
  telemetry.py      mooring-line assets + depth → segment
  segments.py       registry counts per depth zone (event → ROV depth at its peak frame), cached
  video_utils.py    SHA-256, probing, H.264 encoding (NVENC / x264 at low priority)
tools/bluerov_sim.py  BlueROV2 MAVLink simulator for tests and demos without the vehicle
frontend/src/
  App.tsx           state orchestration only
  components/       LiveOps, BatchIngest, ReviewPipeline, DigitalTwin, Panels, Reports,
                    FileBrowser, ThresholdPicker, TestMode, shared
```

## Run

**One click (demo / everyday use):** double-click **`start.bat`** (Windows) or run `./start.sh` (Linux / macOS).
- It builds the web UI the first time and after an update (needs [Node.js](https://nodejs.org) once).
- It then starts the backend, which serves the UI itself and opens it at `http://localhost:8500`. Other PCs on the network use `http://<this PC>:8500`.
- Close the window to stop. `start.bat --rebuild` forces a fresh UI build.
- Python packages are still installed once with the commands below.

**Development** (UI hot reload on port 3000, API on 8500):

```bash
# 1) Backend (Python 3.10+)
cd backend
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128   # RTX 50-series needs cu128+
pip install -r requirements.txt
python main.py                        # http://localhost:8500/docs

# 2) Frontend
cd frontend
npm install
npm run dev                           # http://localhost:3000
```

**Weights:** copy your trained `best.pt` into `backend/models/`, or leave it where Ultralytics put it
(`runs/detect/<run>/weights/best.pt` next to `backend/` or inside it). It is auto-loaded on boot.

**Gemini (optional):** the backend reads `GEMINI_API_KEY` from its environment, from a `.env` file in the app folder (one line: `GEMINI_API_KEY=your-key`), or straight from your Windows user/system variables, so a key added in Windows works even if the backend was started earlier. Keep `.env` private. Without it, the report assistant writes rule-based drafts. Optional tuning: `GEMINI_THINKING` (`low` by default; `high` for more reasoning, but slower), `GEMINI_TIMEOUT_SEC` (90), `GEMINI_RETRIES` (1), `GEMINI_FALLBACK_MODEL` (`gemini-3.5-flash-lite`, used when the chosen model is busy; empty = off) and `GEMINI_DEADLINE_SEC` (150, total time before the rules take over). A report drafted by the rules while Gemini was busy has a **Redraft with Gemini** button. Each report shows how long it took and how much of that was the Gemini request. Check it in the System drawer (gear) → *Gemini report assistant* → **Test connection**; the model (default `gemini-3.8-flash`) is chosen there too. If a request fails, the report is still drafted by the rules and tells you why. Keys created in AI Studio since May 2026 start with `AQ.`; some setups reject them with `401 … ACCESS_TOKEN_TYPE_UNSUPPORTED`. If you see that, update the SDK (`pip install -U google-genai`), try a fresh key, and report it to Google from AI Studio.

**Settings file (optional):** `config.toml` in the app folder sets this site's defaults: paths, model and device, Live trigger and hold rule, Batch sampling, Gemini limits, the default PDF template, and the port. `config.example.toml` lists every setting, all commented out, each with what it does, when to change it, the allowed range and its environment variable. Copy it to `config.toml` (or press **Create config.toml** in the System drawer), uncomment what you want and restart. **`docs/CONFIG.md`** is the operator's reference with *common situations* (false clips, picture lag, 60 fps camera, shallow test site, cloud database, slow Gemini …) and the settings to change for each. **System drawer → Settings → All settings** shows every value in use, where it came from, and any value the app could not use (it keeps the default and says why).
- Precedence: environment variable > `config.toml` > built-in default.
- The Gemini API key stays in `.env`.
- A file with a mistake is reported in the System drawer and ignored; the app starts with defaults.
- Choices made in the app (e.g. the Gemini model, the default PDF template) are kept in `backend/settings.json` and win over the file.

**PDF reports and client templates:** **Download PDF** in a report gives a real PDF (reportlab, no browser printing), named after the scope and status.
- The PDF has a cover page, overview, condition and summary, findings, key frames, actions, limitations, engineer notes, grounding check and a sign-off block, with page numbers and a DRAFT watermark until approved.
- Its look comes from a **template**: logo, company name and details, title, accent colour, paper size (A4 / Letter), font (auto / Helvetica / Times / your own `.ttf`), text size, header and footer text, disclaimer, which sections to include, and sign-off labels.
- Manage templates in System → *PDF report templates* → **Manage templates**: edit, upload a logo, **Copy as new** (one per client), **Use by default**, and a live preview on the newest report.
- Templates are plain JSON files in `report_templates/` (move or back them up like any file). The PDF template can also be picked per export next to *Download PDF*.

**Database on a server (PostgreSQL / AWS RDS):** the app uses the local SQLite files by default. Point it at PostgreSQL by setting one address, then restart:
```toml
# config.toml  (or the environment variable FYP_DATABASE_URL - better for a password)
[database]
url = "postgresql+psycopg://mooring:PASSWORD@my-db.xxxx.rds.amazonaws.com:5432/mooring"
```
- Install the driver: `pip install "psycopg[binary]"`.
- The tables are created on first start. The ROV sensor tables go to the same server unless `sensor_url` is set.
- To move existing data, copy it once into the empty server database: `python tools/migrate_db.py --to "<url>"`. Row counts are checked.
- Media files (clips, key frames, `.tlog`) stay on the backend's disk.
- Every SQL statement the test suite runs has been replayed on PostgreSQL 16 without an error (see `docs/VV_REPORT.md`, PG-1).

**Your existing `fyp_database.db`:** drop it into `backend/`. Missing columns are added on boot, and old detections become a `LEGACY` analysis.

## Updating to a new version (no reinstall needed)

Copy the new code over your project folder. **Keep these, they hold your data:**

| Keep | What it is |
|---|---|
| `backend/fyp_database.db` (+ `-wal`, `-shm`) | inspections, analyses, defects, reports |
| `backend/fyp_sensors.db` (+ `-wal`, `-shm`) | ROV sensor logs and the general detection log |
| `backend/media/` | incident clips, `.tlog` files, report key frames, uploaded copies |
| `backend/models/` | your `best.pt` / other weights |
| `config.toml`, `report_templates/`, `backend/settings.json` | your settings, PDF templates and logos, choices made in the app |
| `logs/` | event-log paper trail, one text file per session |
| `frontend/node_modules/` | installed npm packages |

New database columns are added automatically on the next start. Re-run the installers **only** when
`CHANGES.md` says a dependency changed:
`pip install -r backend/requirements.txt` (Python side) or `npm install` in `frontend/` (UI side).
Then restart the backend and refresh the browser (with `npm run dev` the UI reloads by itself).

## Connecting the BlueROV2

1. **Telemetry.** In BlueOS, go to *Vehicle → MAVLink Endpoints* and add a **UDP Client** to `<this PC's IP>:14551`.
   QGroundControl keeps using 14550. To use another address, set `FYP_MAVLINK_URL` (default `udpin:0.0.0.0:14551`).
2. **Video.** In BlueOS *Video Streams*, enable an **RTSP** stream. In Live Ops, pick *BlueROV2 camera* and enter the stream path
   (for example `rtsp://192.168.2.2:8554/video_stream__dev_video2`).
3. **What you get.**
   - Telemetry: depth, heading, roll and pitch, water temperature and pressure from the Bar30, altitude from the Ping sonar, battery, armed state, flight mode, and leak alarms.
   - Every incident clip and every recorded dive carries these readings.
   - **No vehicle means no numbers.** The UI shows *ROV offline*. If the vehicle goes silent, values are marked *stale* after 3 s and withdrawn after 10 s.
4. **No vehicle at hand?** `python tools/bluerov_sim.py` (add `--target 190`, `--leak-at 60`, …) sends the same MAVLink messages.

## Test Mode: no ROV and no trained model yet

**Test Mode** is in the **System drawer** (gear icon, top right; the Live standby screen also has an *Open Test Mode* button). Use it to work without the vehicle and before `best.pt` is ready.

1. **Pick a video.** Choose a dive from the vault, or browse to any file on this PC.
2. **Pick a dive profile.** Set the depth to dive to and the descent speed. You can also add stand-in classes (step 6).
3. **Start Test Mode.**
   - **Video:** the video is broadcast continuously as H.264 over **UDP** (`udp://127.0.0.1:5600`, the BlueOS camera default port). Live Ops opens it through the same network path as the real camera, and it is pre-selected as the source.
   - **Telemetry:** the BlueROV2 simulator sends real MAVLink to the ROV link. Drag *Move the ROV to* to send the vehicle through Splash → Mid-Water → Touchdown.
   - **Labels:** an amber banner appears across the app, the telemetry shows **ROV SIM**, and sensor rows are stored as `SIM`.
4. **Everything recorded is tagged TEST:** live dives, incident clips, batch ingests and reports. Test data is counted in its own registry view and never mixed into real segment reports. Reports get a `[TEST]` title and a banner.
5. **Use any YOLO model.** `yolo11n.pt` is downloaded automatically by Ultralytics if `best.pt` is missing.
   - **All classes are drawn.** Every class (person, boat, …) is drawn on the live feed and in Review, as grey boxes.
   - **Non-defects are logged separately.** They are saved in the **general detection log** (`object_detections` in `fyp_sensors.db`).
   - **Only defects are defects.** Only `deformation` / `breakage` and `pitting` / `corrosion` classes ever reach the defect database, the alerts, the clips and the reports. None of the 80 COCO classes maps to a defect (tested).
6. **Stand-in defects (optional, Test Mode only).** To rehearse incident clips, alerts, review and reports with a generic model, map one of its classes to a defect, e.g. *person → DEFORMATION*. Records keep the raw class (`person`) and the mapping, and the mapping cannot change during a test dive.

Test Mode will not start while a real BlueROV2 is connected, and stopping it ends the test dive.

## Using each pipeline

The header holds the four tabs (**Live · Review · Batch · Reports**), the mooring-line selector, and status pills: ROV link, YOLO model, GPU share (live / batch), and TEST MODE when on. The **gear** opens the System drawer: detection model + IoU, Test Mode, GPU sharing details, the full event log, and the demo reset. Event logs show *time · type tag · message* so they can be skimmed by type (CLIP, ZONE, LINK, CONFIG, IMPORT, DONE …).

| Tab | What happens |
|---|---|
| **Live** | Left: the camera feed (no playback controls), the last-2-minute defect sightings strip, and the event log. Right: the **incident alert stack** (latest incident on top, "N this dive", earlier incidents listed, each with **Review** and **Report** so a report can be drafted without stopping the dive), **ROV depth** (MAVLink, never invented), the **digital twin** (line through its depth zones, the ROV at its real depth, defect counts per zone, *Report…*), and **Detection**. **Detection** defaults to *Auto*: it arms only while the ROV reports ≥ 2 m depth and says why it is waiting (*waiting for ROV depth* / *ROV above 2 m*); *Armed* and *Standby* (video only, launch / recovery) are one click away. **Clip trigger** (default 0.60): a deformation at or above it on **3 of the last 5 analysed frames** saves the 5 s clip and raises the alert (a one-frame flicker does not; `live.trigger_hits` / `live.trigger_window` in `config.toml`); detections ≥ 0.40 are drawn dashed and logged; every change is recorded with the dive. **Freeze frame** freezes your view only. **Stop / Record log** turns 5 Hz sensor samples and the raw `.tlog` off or on; incident clips are always saved. |
| **Batch** | The import bar: **Browse this PC** (external drives and NAS mounts included), name, line, **threshold (required, presets or custom)**, optional **+ ROV log (.tlog)**, *Import & queue*. Tick several files to import them together: the line and threshold apply to all, and any file can override them. The *Dives & clips* table has an **Added** date column, a **Processing time** column (time and × real time; hover for the breakdown; **Processing times (CSV)** exports every analysis for benchmarking) and sortable headers. Files are registered **in place, not copied**, then fingerprinted in the background (Fingerprinting → Queued → Processing → Completed). The *Dives & clips* table shows each dive's defects per type, its depth log, and actions: **Review**, **Re-analyse** (incident clips too; new analysis, original kept), **Cancel** while processing. If a drive is unplugged or a folder moved, the row says *media offline* and offers **Locate file…**: pick the file's new location and it is accepted only if its fingerprint matches the recorded dive; the results are kept. Records with results cannot be deleted (integrity); only never-analysed imports can be removed, and your file is never touched. Below: the **defect registry** and the event log. (`POST /api/videos/upload-stream` remains as an API for scripts; the UI does not use it.) |
| **Reports** | Every report drafted so far, newest first, with scope, line, grade, status, who or what drafted it, and when. Search, filters, sortable columns; click to open, edit, approve or export. Reports being drafted show their progress at the top; the panel on the right starts a new one. |
| **Review** | Opens on the **media library**: every dive and incident clip, newest first and grouped by day, with line, date and time, length and defect events per type. Search (**/**), filter (All / Dives / Incident clips / Pinned), click a column header to sort (again to reverse; by line groups by line), and **pin** rows to the top (pins are stored on the server). Nothing plays until you choose; **Library** returns to the list. The overlay comes from the database, with no re-inference. Under the player: play/pause, previous / next defect, and labelled lanes (**Position**, **Deformation**, **Pitting**, plus other objects when logged) showing when each defect is in view — click a bar to jump to the start of that sighting; the trigger marker snaps to the exact frame. A chip shows **how the media was recorded** (e.g. *Recorded live · ≥ 0.40 · trigger 0.60*); there is no slider. Right: **Analysis shown** (switch between analyses) with **Re-analyse at** presets or a custom value (keeps both), **ROV at playhead** (depth, zone, heading from the sensor log), and the report assistant. **Live dive logs** (own filter; the newest also appear at the top of *All*): one page per live dive with depth over time across the zones, incident clips (click → Review at the trigger), sightings vs the trigger, vehicle and operator events, a summary, *Report on this dive*, and downloads (sensor CSV, sightings CSV, events CSV, `.tlog`). Also opened from the Live tab's **Dive log** link while the dive runs. |

**Defect registry by depth zone (Batch tab).**
- **Whole line** or a line segment (Splash / Mid-Water / Touchdown). Clicking a zone filters the dive table to the dives with defects in that zone.
- Each defect event is placed in the zone of the ROV depth at its strongest detection. That depth comes from the live sensor log or from an attached `.tlog`.
- Each zone button shows its event count, and a dot marks the zone the ROV is in now.
- Events from dives with no depth data are counted in the line total only, and are flagged as "no ROV depth".
- The selected zone also sets which "Segment history" report is offered.

**Sharing one GPU.** All inference goes through `gpu_arbiter.py`:
- Live frames are always served first.
- While a live dive runs, batch frames go in chunks of 2, so a live frame never waits behind a whole batch.
- With no live dive, batch gets full batches.

The GPU pill in the header shows the live / batch share; the System drawer adds the live wait time, temperature, VRAM and throughput, which you can point at during the demo.

**Inspection Report Assistant.**
- Open it from a Live alert (**Report**, scoped to that incident), the twin's *Report…* (this dive so far, finished dives, segments), the Batch registry's *Report…*, or the Review sidebar. Choose the scope and press *Generate draft report*.
- The backend builds the facts: events with depth and segment from the sensor DB, the threshold used, the asset, and previous inspections. It adds key frames (full frame + crop).
- Gemini returns a structured draft: a fixed condition grade, actions from a fixed list, and a visual verdict per event.
- Every claim is checked against the database. Unsupported claims are regenerated once, then flagged *Needs review*.
- You edit the draft, **approve** it with your name (this locks it), and **Download PDF** in your client's template. Clicking an event jumps to it in Review.
- The 30 s AI cooldown is enforced by the server.

To test RTSP without an ROV: run [MediaMTX](https://github.com/bluenviron/mediamtx), then
`ffmpeg -re -stream_loop -1 -i dive.mp4 -c copy -f rtsp rtsp://localhost:8554/rov` and connect to `rtsp://localhost:8554/rov`.

## Training the model

Dataset building, Blender renders and YOLO11n training live in the separate **Mooring Dataset Kit** (`mooring-dataset-kit.zip`). The hand-over is the trained `best.pt`. Copy it into `models\`, load it from the System drawer, and run `tests/test_model_acceptance.py` with `FYP_VAL_SPLIT=test` against the kit's held-out real videos.

## Verification & validation

```bash
pip install pytest
pip install pytest pymavlink
python -m pytest -v                     # full suite (~8 min, includes crash/resume + throughput)
python -m pytest -v -m "not slow"       # ~4 min
```
The UI needs internet once for its fonts (Manrope, IBM Plex, Saira from Google Fonts); offline it falls back to the system fonts and still works.
Results go to `tests/results/` (JUnit XML + `evidence.json`). The suite runs in an isolated temp environment, so your databases are never touched.
Scenario tests use a deterministic colour-blob detector (`tests/fakes`) so every step has known ground truth, and the BlueROV2 simulator over real MAVLink/UDP.
The real model is checked by `tests/test_model_acceptance.py` (set `FYP_REAL_WEIGHTS`, optionally `FYP_VAL_DATA`).
Browser walkthrough: `tests/e2e/` (see the header of `scenarios.mjs`). Full traceability and results: **`docs/VV_REPORT.md`**.

## Iteration to-do list: status

| Item | Done |
|---|---|
| Nav overlap | Status pill + tabs are an in-flow row (`absolute` removed, `pt-20` removed). |
| Player size | `max-w-5xl` / `aspect-video` removed; the player is `flex-1` and fills the card. Overlays are drawn on the real `object-contain` frame rect, so boxes stay aligned at any size. |
| Contextual control board | Live: trigger slider, detection mode, dive log, IoU. Batch: no controls (threshold is chosen per dive). Review: the analysis record, analyses list, re-analyse. |
| Dynamic Defect Registry | `142` baseline removed. Counts come from `/api/history` (SQL `LAG()` groups detections <2 s apart into events; incident clips excluded to avoid double counting). Bars scale to the largest class. |
| Empty-state boot | Nothing is auto-selected. Live shows *NO SIGNAL · STANDBY*; Review shows the media library until the operator picks. Live and Review no longer share `videoRef`/`selectedVideoId`. |
| `best.pt` loading | Root causes and fixes below. |

## Why `best.pt` failed (and what changed)

1. `models/`, the DB and `uploads/` were resolved from `os.getcwd()`. Launching from another folder or an IDE looked in the wrong place.
2. When the file wasn't found, the code fell back to the bare string `"best.pt"`. Ultralytics treats unknown `*.pt` names as GitHub release assets and tries to **download** them, which fails.
3. Training output lives in `runs/detect/<run>/weights/`, and nothing searched there.
4. `YOLO(path)` loads onto the CPU, and the GPU move happens on the first `predict()`, so a CUDA/arch mismatch only shows up *during playback*. RTX 50-series cards (sm_120) need a CUDA 12.8+ PyTorch build.

Now: cwd-independent search of `models/` and `runs/**/weights/`, no silent download for custom names, and a warm-up inference at load time that reports GPU problems immediately and falls back to CPU. The exact error and a fix hint are shown in the System drawer (Detection model).

## Bugs fixed beyond the list

- `_save_incident_clip` was called with 4 args but took 3, and `display_name` was undefined, so **no incident clip was ever saved**.
- Clips were written as `mp4v`, which browsers can't play. Clips are now H.264 (ffmpeg), and non-web uploads get an H.264 review proxy.
- `/api/history` was declared twice; FastAPI matched the dummy `return []` first, so the registry was always empty.
- History was ordered by a random hex ID; it is now ordered by `created_at`.
- The Review sync effect checked `activeTab !== 'video'` (a tab that doesn't exist), so review never received detections.
- Batch inference reopened the video file for every sampled frame and seeked per sample. It now decodes sequentially and runs YOLO in mini-batches.
- `MOORINGLINE` detections were logged as defects. Raw class names are now mapped to `CORROSION`/`DEFORMATION`; other classes are shown but never logged.
- Multiple uploads ran concurrent YOLO loops on one GPU. They now go through one FIFO worker that yields to the live stream.
- Added `busy_timeout` so concurrent SQLite writers wait instead of throwing "database is locked".
- Hardware card, uptime and random "sensor" log lines were fabricated client-side. Real values now come from `nvidia-ml-py`/`torch`, shown as `n/a` when unavailable.

## Design notes

- **RAM buffer:** 150 frames are stored as JPEG bytes (~10 MB) instead of raw BGR (~220 MB); the same JPEG feeds the MJPEG stream.
- **Incident clips:** the trigger spawns a thread that waits a 2 s post-roll, snapshots the buffer and encodes off-thread. Each clip is ~3 s before plus ~2 s after the defect. The clip gets its own analysis record (logged ≥ 0.40, trigger 0.60) and one sensor reading per frame.
- **Latest-frame-wins inference:** if YOLO is slower than the camera, frames are skipped for inference only, never for capture or display.
- **Analyses are immutable:** a different threshold is a new analysis linked to its parent, so an alert can always be explained by the threshold that produced it.
- **Two databases:** `fyp_database.db` holds inspections and reports (small, the thing you back up and hand in). `fyp_sensors.db` holds high-rate ROV samples (5 Hz → 72 000 rows per 4-hour dive). The raw MAVLink `.tlog` per dive is kept in `media/tlogs/`, so sensor data can always be rebuilt.
- **Local times:** timestamps are stored in UTC and shown in the PC's time zone.

The dissertation quotes tests/results/RESULTS - real.md (22 Nov 2026, 02:01). RESULTS.md is a later repeat run (03:10) with the same verdicts and small run-to-run variation
