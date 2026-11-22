# Changes per iteration


## 3.6.11 (2026-11-22) — telemetry never stale in a clip; model warmed for every input shape
- **Fix: a clip saved while the ROV link was stale stored old telemetry (A9).** A clip now stores the trigger
  telemetry and per-frame sensor rows only while the link is not stale and the depth is under 1 s old
  (`TELEMETRY_RECORD_MAX_AGE_SEC = 1.0`); otherwise it stores none and its name says `_NOROV_`.
- **Fix: the dive log repeated the last depth for about 3 s after a link cut (A10).** Each depth reading is stamped
  with its arrival time, and the 5 Hz sampler writes a row only when a newer depth has arrived, so a cut leaves a gap
  instead of a repeated value.
- **Review: re-analyse at the same threshold.** The button is no longer greyed out when the threshold matches the run
  shown ("Re-analyse at X again"); the heading reads "Re-analyse (keeps both)". A new run is always created and the
  original kept, as in Batch.
- **Model warmed for every input shape at load.** The warm-up used one 640×640 frame on a copy that was then
  discarded, so the first batch at each new shape held the GPU and delayed live frames. The model is now warmed at
  1, 2 and 8 frames, landscape and portrait, and the warmed instance is handed to the first pipeline. Loading the
  model takes about 20–30 s longer, once. With v4 in the whole app, live analysis kept 64.5 % of its solo rate while
  a 1080p HEVC dive was analysed (NFR-2, target 60 %), and live frames waited 26.8 ms for the GPU at the 95th
  percentile (GA-2, target 60 ms).
- Tests: new `test_tc_1_2_stale_telemetry_never_recorded_with_a_clip` (TC-1.2-stale-clip) and
  `test_tc_1_8_no_repeated_depth_when_messages_stop` (TC-1.8-cut). pytest 106 passed, 4 skipped; E2E 76/76;
  model acceptance MA-1, MA-2 pass (median 19.0 ms, p95 25.8 ms per frame), MA-3, MA-4 fail as before.
- **Version number** 3.6.11 in the API and the UI package.
- Installers: **none.** Rebuild the UI once (`start.bat` does it automatically).

## 3.6.10 (2026-11-13) — Test Mode card tidied; easier-to-read small text
- **Fix: Batch table "Re-analyse" / "Process" threshold panel was cut off on the last rows.** The table scrolls
  sideways, and a scrolling box clips whatever sticks out of it, so the panel was clipped or scrolled inside the table.
  It is now drawn at page level next to its button, opens upwards when there is no room below, and follows the button
  when the page scrolls. Found by the user while preparing screenshots (13 Nov).
- **Model acceptance target MA-3 raised to mAP@0.5 >= 0.80 and deformation recall >= 0.90** (was 0.50 / 0.70), to match
  Table 3.5 after review: defaults in `tests/test_model_acceptance.py` and `tools/acceptance.bat`. No verdict changes:
  v4 failed MA-3 at both levels (on X1: mAP@0.5 0.458, deformation recall 0.40 at the best-F1 point). The
  dataset kit's `threshold_sweep.py` keeps its 0.70 default and now says the 12 Nov sweeps were run against 0.70.
- **Version number** set to 3.6.10 in the API (`/docs`) and the UI package (they still read 3.0.0 and 2.0.0).
- **Name:** "AI Insights Digital Triplet Platform" in the header, browser tab, PDF footer default and PDF properties, API title and event-log header
  (was "AI Insights Triplet Twin Platform" / "Mooring Twin"). PDF templates saved earlier keep their own footer text.
- **Live tab:** the zone panel is now "Mooring line · defects per zone" (was "Digital twin · …").
- **Zone panel labels no longer overlap:** each zone's defect tags go on the side away from the line; the "ROV … m"
  label takes the first free spot beside the vehicle, on a dark backing (checked for every depth 0–200 m); zone names
  are drawn above the line with an outline, on the side away from it (Touchdown on the right).
- **Stand-in defects hidden when the loaded model has its own defect classes** (e.g. best.pt: deformation, pitting).
  The editor only appears for a generic model, which is the only case it applies to; the note explaining this is removed.
- Shorter intro text; small labels enlarged from 10–11 px to 12 px; status rows 13 px.
- No behaviour change: stand-ins still work as before for a generic model (E2E-T.1 unchanged).
- **Readability across the app (for long shifts):** small text one step larger (Tailwind xs 12 → 13 px with looser line
  spacing; 11 → 12 px; 10–10.5 → 12 px); secondary greys brightened, keeping their order: secondary 9.8 → 11:1, muted
  6.8 → 9.1:1, dim 5.2 → 8.0:1 on cards (WCAG AAA is 7:1). Status, defect and accent colours unchanged.

## 3.6.9 (2026-11-12) — model and trigger chosen from data
- **Model: v4** (`v4_mixed`: v3 renders + 287 frames of the real specimen + clean lake frames). Copy `best.pt` to `backend/models/v4_mixed.pt` and choose it in the System drawer.
- **Default clip trigger and High-confidence label stay at 0.60, now with a basis:** `threshold_sweep.py` on the validation video V1 chose t* = 0.60 for v4 (the lowest threshold with false alarms ≤ 1 % of 263 clean frames; recall there 0.24). APP_CHANGES item 5 therefore changes no value, only the setting's explanation (`docs/CONFIG.md`, `config.example.toml`).
- Follow-up models (v5_real without renders, v5_ratio50 with 50 % renders) were compared on V1 and the best (v5_real) scored once on X1. It was not clearly better than v4 on X1 (same recall at its t*, lower mAP@0.5, fewer false alarms on C2), so v4 stays, as decided before scoring.
- **Fix: `tools\acceptance.bat` skipped all four model tests (MA-1..4).** Since 3.6.7 the test set-up clears the developer's own `FYP_*` variables, and that also cleared the acceptance inputs the batch file sets (weights, data, clean frames, targets). Those are now kept.
- Installers: **none.**

## 3.6.8 (2026-11-10) — aligned with the dissertation
- **Splash zone of ML-01 is now 0–50 m** (was 0–30 m), as API RP 2I treats the first 50 m of the water column. Mid-Water is 50–145 m, Touchdown below 145 m. An existing database still holding the old boundary is moved at start-up; a line whose zones someone has edited is left alone. Approved reports keep the zone they were signed with.
- **Labels state the detector's certainty, not a severity.** "Critical" / "High" / "Medium" become **High confidence** (at or above `live.critical_conf`, default 0.60) and **Low confidence** (below), one rule for deformation and pitting (pitting used 0.75 before). Detections already in the database are relabelled from their stored confidence. Nothing that depends on the number changes: clip trigger, hold rule and report grades are as before.
  - The live alert reads **"Deformation clipped"** (was "Critical defect clipped", which showed even when the operator had moved the trigger below 0.60).
  - The facts sent to Gemini carry `confidence_label: high | low` instead of `severity: Critical`, and Gemini's instructions say the label is certainty, not severity.
  - The setting `live.critical_conf` keeps its name (config files still work); its title is now "High-confidence label from". The old documentation said deformations below it were "Major"; the code labelled them "High".
  - Rule-based grade, unchanged: **REPAIR** if any deformation event peaks at or above `live.critical_conf`; **MONITOR** if there are only lower-confidence deformations or any pitting; **ACCEPTABLE** if there are no events. It never proposes REPLACE.
- **Pitting is shown as "Pitting (grade 6–7)"** in Review, reports, the HTML export and the PDF. The internal type stays CORROSION. The rule-based draft and Gemini's instructions say "pitting" (grades 6–7 of Lone et al. 2023).
- **Every report (HTML and PDF) carries a "Basis of the findings" note:** deformation based on the API RP 2I discard criteria, pitting grades 6–7 of Lone et al. (2023); labels are certainty, not severity; findings support, not replace, the engineer; stretch, wear and early corrosion need measurement.
- **Title:** "AI Insights Triplet Twin Platform" in the browser tab and the header.
- Tests: new LBL-1..3; LOG-1 expects its 40–44 m dive in the Splash Zone; TC-1.3 expects "High confidence"; E2E-1.5a expects "Deformation clipped". pytest 104 passed, 4 skipped; E2E 76/76 (development environment).
- **Browser walkthrough on your own PC: `tools\run_e2e.bat`.** Installs Playwright and Chromium once (about 150 MB), runs the 76 checks against the UI as the backend serves it, and records the PC in `tests\results\e2e_host.json`; the `RESULTS.md` header then says where the walkthrough ran. The test backend now registers the UI's catch-all route last, so the walkthrough's test-only endpoint is reachable (V&V finding B33).
- Installers: **none.** Rebuild the UI once (`start.bat` does it automatically). The walkthrough needs Node.js, which `start.bat` already requires.

## 3.6.7 (2026-11-08)
- **Fix (Windows): attaching a .tlog to a dive failed.** The log was read for checking and left open, and Windows cannot replace a file that is still open, so the upload was refused. Two causes, both Windows-only: the log was read for checking and left open (Windows cannot replace an open file), and the check copy was named `….tlog.part`, which pymavlink took for a network address because a Windows path has a colon (`C:\…`) and the name did not end in `.tlog`. The log is now opened as a file whatever its name and always closed. Found by running the tests on the Windows PC (TC-3.1b, SZ-2).
- **Fix: switching dive-log recording off and on within the same second** overwrote the first .tlog file. The second file now gets its own name.
- **Tests on a developer PC:** the tests no longer pick up your own Gemini key (app `.env` file or the key saved with `setx`) or your own `FYP_*` / `GEMINI_*` variables, which made the "no API key" tests fail on a PC that has a key (TC-2.4, UI-7).
- `tools\run_tests.bat` clears old measurements before the run, and `RESULTS.md` now prints the reason under each failed test. The dive-log test now runs its dive past the whole defect window instead of stopping right after the clip (on a fast PC it stopped before the pitting was seen). NFR-2 (live detections while batch runs, at least 60 % of the solo rate) missed by 0.9 fps in one Windows run with a YouTube video playing; on an otherwise idle PC it passes (62 %), so the 60 % target stands. Timing results should be taken with nothing else running, on mains power.
- Installers: **none.**

## 3.6.6 (2026-11-07)
- **Event log paper trail.** Each app session writes a plain-text file in `logs\` named after the time the backend started, e.g. `logs\session_2026-11-07_19-40-12.txt`.
  - Every entry of the browser's event log goes in, marked `[ui]`.
  - The backend adds its own record, marked `[app]`, so the key facts are kept even with no browser open: session start and end, live dive started / ended, incident clip saved, batch analysis finished / failed / cancelled, report approved, settings problems.
  - One entry is one line (`2026-11-07 19:40:12 | Clip | [app] …`). The file is only ever added to.
  - System drawer → **Event log paper trail**: the file's path, view or download it, and earlier sessions. The folder is `paths.logs_dir` in `config.toml`.
- **Fix: a live dive could be reopened by a later stop.** Pressing Stop again, switching Test Mode off, or closing the app hours later re-ran the end of the dive and overwrote its end time and duration. This is what made a 1-minute test dive read 9 h 33 min (the app was closed the next morning). A dive is now closed exactly once. (3.6.5's trimming still corrects dives recorded before this fix.)
- **Fix: an incident clip identical to an earlier one was lost.** With Test Mode looping one video, two clips can be byte-identical, and saving the second failed on the unique file fingerprint. It is now saved; only the fingerprint is left empty.
- **Re-running the tests on Windows:** `tools\run_tests.bat` runs the whole suite on your PC and rebuilds `tests\results\RESULTS.md`, which now records the PC (OS, CPU threads, GPU) and when each part was last run. The tests no longer need `ffmpeg`/`ffprobe` on the PATH: they use the ffmpeg bundled with `imageio-ffmpeg` and count clip frames with OpenCV.
- Installers: **none.**

## 3.6.5 (2026-10-27)
- **Live dive log.** Every live dive now has a log page: the app's own record of the whole dive. Open it from the Live tab (**Dive log**, next to the sightings strip, also while the dive runs) or from Review → *Live dive logs*.
  - **Chart:** depth over time across the line's depth zones, with incident clips (click one to open it in Review at the trigger), sightings (filled when a deformation reached the clip trigger, hollow when only logged) and a hover readout of depth, heading, water temperature, battery and mode. Drag to zoom.
  - **Lanes** under the chart: deformation, pitting, vehicle events (link lost, armed, leak…), operator changes.
  - **Tabs:** sightings (sortable; a row zooms the chart), vehicle events, settings changes (clip trigger, detection mode, dive-log recording, each with the time), summary (time per zone, battery, link losses, one-off hits ignored).
  - **Report on this dive**, and **Download**: every sensor reading as CSV, sightings CSV, vehicle + operator events CSV, the raw `.tlog` file(s).
  - A clip opened from the log returns to the log when closed.
  - Long dives stay fast: the chart is thinned on the server to ~2,400 points, keeping the deepest and shallowest reading of every interval (a 4-hour dive at 5 Hz is 72,000 readings).
- The full dive **video** is still the topside recorder's job (QGroundControl, a DVR or the onboard camera), imported in Batch with its `.tlog`. The app keeps the 5 s clips and this log. `docs/FIELD_TEST.md` now says to press Record in QGroundControl before the inspection runs, checks that QGroundControl and the app can play the stream at the same time (FT-5b), and checks the dive log (FT-12b).
- Operator changes (detection mode, dive-log recording) are now saved with the dive as they happen, alongside the trigger changes that were already recorded.
- New API: `GET /api/dives`, `/api/dives/{id}/log`, `/api/dives/{id}/export/{sensors|sightings|events}.csv`, `/api/dives/{id}/tlog/{file}`.
- **A dive left open is no longer hours long.** A Test Mode clip loops, and a live dive runs until Stop or Disconnect. If the PC sleeps or the dive is left running, the next Stop (or closing the app) used to set the duration to the whole time it was open: a 1-minute test dive showed 9 h 33 min.
  - Stop now ends the dive at its last video frame or sensor reading whenever nothing has come in for over 60 s, and records the idle time separately.
  - Dives recorded before this fix are trimmed the same way when shown, at their last sensor reading, sighting or clip.
  - The dive log says what happened: "Nothing was recorded after T+0:48… stayed open for another 9 h 32 min". The library row adds "then left open …".
  - If the whole app stops running (PC asleep or hibernating, process suspended), an **App paused** event is logged with the dive when it resumes, so the gap in the depth trace is explained.
- **Batch processing time, for benchmarking.** Every batch import and re-analysis now records how long it took.
  - The *Dives & clips* table has a sortable **Processing time** column, e.g. "1 min 52 s · 10.7× real time" ("… so far" while running). Hover for the breakdown: frames and frames/s, the H.264 browser copy if one was built, time waiting in the queue (not counted), restarts, time the GPU was shared with a live dive, reader, batch size, model and device.
  - **Processing times (CSV)** downloads every analysis with these figures, ready for a results table.
  - A run resumed after a crash adds its time up without the time the app was down. The event log's "completed" line includes the time.
  - Analyses made before this version show "—"; re-analyse to measure them.
- **Batch read-ahead.** The next frames are now decoded in the background while the GPU runs detection on the current batch, so the two overlap instead of taking turns.
  - Results are identical; memory is capped at 16 frames (about 45 MB at 1280×720).
  - The hover and the CSV now split the time into **detection (GPU)** and **waiting for decoded video**, and name the bottleneck.
  - `batch.prefetch_batches` in `config.toml` (default 2; 0 = off, the old behaviour).
- **Less live picture delay.** OpenCV decodes a network stream with one thread per CPU, and every decoder thread holds back one frame, so on a 16-thread PC the live picture ran about 0.5 s behind the camera before the app even saw it. The live stream now uses 2 decoder threads (`live.decoder_threads` in `config.toml`; 0 = the old behaviour).
  - Measured on an H.264 stream: 16 threads → 2 threads cut the delay by 0.54 s at 30 fps; 2 threads add only 27 ms over 1 (LAT-1).
  - 2 threads decode a 1080p30 stream comfortably on a desktop CPU. For 4K or high-frame-rate streams, raise it if the System drawer shows the capture rate below the camera's.
  - FFmpeg's input buffering flags were also tried. They made no measurable difference and joined the stream about 0.4 s later, so they were left out.
- **Settings that operators can tune with confidence.**
  - Every setting is described once (`backend/config_spec.py`): what it does, when to change it and what you give up, the unit and allowed range. `config.example.toml`, the new reference **`docs/CONFIG.md`** and the app are all generated from that description, and tests keep the three in sync.
  - `config.example.toml` is rewritten for operators: how to use it step by step, settings grouped with an explanation per section, rarely changed settings listed last.
  - `docs/CONFIG.md` adds **common situations**, each with the settings to change, as a ready-to-paste snippet, and the trade-off. Examples: clean chain saving clips, deformations missed, picture lag, a 60 fps / 4K camera, a shallow test site, faster batch, a laptop without a GPU, a cloud database, slow Gemini, a full disk.
  - Values are checked when the app starts. A wrong type, an out-of-range value, an unknown choice or contradicting rules are reported with the reason, and the default is used: for example `trigger_hits` above `trigger_window`, which could never save a clip. A misspelt name gets "did you mean …". Before, some typos stopped the app and others were silently ignored.
  - System drawer → **Settings**: problems at the top; **All settings** lists every value in use with where it came from (default / config.toml / environment variable / chosen in the app), with search, "only changed", and the explanation for each.
  - After editing a setting's description: `python tools/make_config_docs.py`.
- Installers: **none.** Two new database columns (`analysis_runs.ops_log`, `analysis_runs.timing`) are added automatically at start-up. Rebuild the UI (`start.bat` does it).

## 3.6.4 (2026-10-26)
- **One-click start.** `start.bat` (Windows) / `start.sh` (Linux, macOS, a cloud VM):
  - builds the web UI when it is missing or older than the source (Node.js needed once),
  - starts the backend, which now **serves the UI itself** at `http://<pc>:8500` (one process, one address),
  - opens the browser when the app is ready.

  `npm run dev` on port 3000 still works for development. The JSON status that used to be at `/` is now `/api/health`.
- **Database address.** `database.url` in `config.toml` (or `FYP_DATABASE_URL`) moves the app to PostgreSQL / AWS RDS; `sensor_url` optionally separates the ROV sensor tables. SQLite stays the default.
  - Made portable for this: SQLite-only `INSERT OR IGNORE`, integer defaults on boolean columns, and boolean/integer `COALESCE` (PostgreSQL rejected all three). Subqueries are named (PostgreSQL ≤ 15).
  - File paths and names are unbounded text, because PostgreSQL enforces lengths.
  - `tools/migrate_db.py` copies the SQLite data into an empty server database and checks row counts. Passwords are masked in logs and in the System drawer, which now shows which database is in use.
- **PostgreSQL check (PG-1).** All 118,436 SQL statements the test suite executes were replayed on PostgreSQL 16; none failed apart from the replay's own artefacts. Re-run with `tests/postgres/run_pg_compat.sh`.
- **Field test procedure:** `docs/FIELD_TEST.md`, a fill-in checklist for the real BlueROV2, camera, model and a full dive-to-PDF run.
- **Model acceptance:**
  - New **MA-4**, false alarms on real defect-free frames at the clip trigger.
  - New `tools\acceptance.bat <best.pt> [data.yaml] [clean frames]` runs MA-1..4 in one go.
- **Windows fix:** subprocess output is read as UTF-8. The model acceptance tests failed before reaching the model, because Ultralytics prints UTF-8 progress bars and Windows' default cp1252 cannot decode them. The `.tlog` alignment's ffprobe call had the same latent problem with non-ASCII file names.
- Installers: **none** for SQLite. For PostgreSQL: `pip install "psycopg[binary]"`. Node.js once for the UI build (already needed for `npm`).

## 3.6.3 (2026-10-25)
- **The clip trigger must hold.** A deformation at or above the trigger now has to be seen on **3 of the last 5 analysed frames** before a clip is saved, so one-frame flickers no longer save clips (in v2's tests, a normal lake-chain link seen edge-on reached 0.68 on a single frame). The clip is still centred on the first frame of the run. The Detection card shows the rule and how many one-off hits were ignored this dive. Set it in `config.toml` (`live.trigger_hits`, `live.trigger_window`; 1 / 1 = the old behaviour).
- **Test Mode stand-in "Add" looked broken.** It only offers the model's *generic* classes, and a trained defect model such as `best.pt` has none (only deformation and pitting), so the button was disabled with a misleading "Load a model first". It now says the model already detects the real defect classes, so no stand-in is needed. It explains the disabled button on hover, and flags stand-in rows whose class the loaded model does not have.
- **Real PDF export with client templates.**
  - **Download PDF** gives a proper PDF file built on the server (reportlab, pure Python) instead of a page to print. It has a cover, overview, findings, key frames, actions, limitations, notes, grounding check, sign-off, page numbers and a DRAFT watermark until approved.
  - **Templates** (`report_templates/*.json`) set the logo, company details, colours, paper, font, size, header and footer, disclaimer, sections and sign-off labels.
  - Templates are managed in System → *PDF report templates*: edit, upload a logo, copy per client, set the default, live preview. Pick a template per export next to *Download PDF*.
  - New API: `GET /api/reports/{id}/pdf?template=`, `/api/report-templates…`. The printable HTML page is still there as "printable page".
- **Optional `config.toml`** for the site's defaults: paths, model and device, Live trigger and hold rule, Batch, Gemini limits, report template, port.
  - Precedence: environment variable > `config.toml` > default. Relative paths are resolved from the file's folder.
  - A broken file or unknown keys are shown in the System drawer (*Settings file*), never fatal. **Create config.toml** writes a fully commented copy of `config.example.toml`, which is generated from the same list the code reads.
  - The Gemini key stays in `.env`.
- Test double: Gemini 3 image tokens are 1,120 each (as measured), not 258.
- Installers: **`pip install -r backend/requirements.txt`** (adds `reportlab`). Restart the backend.

## 3.6.2 (2026-10-25) — when Gemini is busy
- **Fallback model.** In the field, Gemini 3.8 Flash answered `503 UNAVAILABLE` ("high demand"). The order is now:
  1. The chosen model is asked again once.
  2. If it is still busy, out of quota or not answering in time, **Gemini 3.5 Flash-Lite** drafts the report (separate capacity and quota).
  3. Only then are the rules used.
- **One deadline for all of it:** 150 s (`GEMINI_DEADLINE_SEC`). A model that timed out is not waited on twice. A bad key or model name goes straight to the rules, because another model would not help.
- A Flash-Lite draft is labelled at the top of the report and in its limitations, because its visual check is less thorough.
- Set `GEMINI_FALLBACK_MODEL` to change the fallback model, or leave it empty to turn the fallback off.
- **Redraft with Gemini.** A report drafted by the rules (or by the fallback model) because Gemini was busy has a **Redraft with Gemini** button. It asks Gemini again and replaces the draft **in the same report**, keeping the engineer notes. If Gemini is still busy, the report is left as it was and the reason is shown. The usual 30 s AI cooldown applies; approved reports cannot be redrafted. New `POST /api/reports/{id}/redraft` (a report job).
- Each report records its failed Gemini attempts (model, busy / timeout / quota, seconds). Google's error is shortened to its message (e.g. "503 UNAVAILABLE: This model is currently experiencing high demand…").
- Test double: simulates busy models and timeouts, and counts images at Gemini 3's 1,120 tokens each, as seen in the field (was 258, the Gemini 2 rate).
- Installers: **none**. Restart the backend.

## 3.6.1 (2026-10-25)
- **Faster, bounded Gemini drafts.** Reports were taking ~4 min, almost all of it waiting on one Gemini request. Now:
  - Gemini 3 models are asked for **low thinking** (`GEMINI_THINKING=low`, the default). The draft only restates checked facts, so deep reasoning adds time without adding accuracy. Set `GEMINI_THINKING=high` to compare.
  - Each request has a **90 s timeout** (`GEMINI_TIMEOUT_SEC`) and **1 retry** (`GEMINI_RETRIES`). If Gemini is overloaded, the report falls back to the rules within about 3 minutes instead of hanging, and says why.
- **Every report records its timings:** total, the Gemini request, facts, key frames and claim check (`content.timings`, `usage.seconds`, `content.thinking`). The console prints a `[REPORT]` timing line, and the HTML export shows *Time taken*.
- **Report view redesigned for reading.**
  - Larger body text (15 px).
  - One scroll for the whole report, with no boxes that scroll inside it. Text boxes grow with their text, and approved reports show as plain text.
  - A labelled header: drafted by, created, analysis, sensors, coverage, time taken and AI usage.
  - Bigger key frames; each finding as a card.
  - Export / Save / Approve are always visible at the bottom.
- Review library: the *Defect events* header now lines up with its column.
- Installers: **none**. Restart the backend.

## 3.6 (2026-10-25) — operator feedback
- **Live sightings strip is clickable.** Sightings that were saved as an incident clip are framed; click one to open that clip in Review at that moment. Clicking a logged sighting below the clip trigger says so (no clip was saved).
- **Review lanes are clickable.** Click a Deformation / Pitting bar to jump to the start of that sighting; an empty part of a lane moves the playhead there, like the position bar.
- **Report progress.** A report is drafted by a background job (`POST /api/reports/jobs`, `GET /api/reports/jobs/{id}`) with real stages — facts → key frames → draft (Gemini or rules) → claim check → save — shown as a step strip, a progress bar, elapsed time and the usual Gemini time. You can switch tabs; the panel picks the job up again. `POST /api/reports` (synchronous) still works for scripts.
- **Reports tab.** Every report ever drafted, newest first: scope, line, grade, status, drafted by, created date, approved by. Search (**/**), status / scope / line filters, sortable columns; click a row to open, edit, approve or export it. Reports being drafted show at the top with their progress. A new-report panel sits on the right.
- **Dates everywhere.** Review library, Batch table and Reports show when each item was added / clipped / created ("Today 14:02", "Yesterday 09:13", "23 Oct 14:02").
- **Sortable columns everywhere.** Click a header to sort, click again to reverse; the choice is remembered per browser. Sorting by line groups by line (newest first inside each line) — in Review it adds line headings.
- **Batch: several files at once.** Tick files in *Browse this PC* (also across folders, or *Select all*), then *Add*. The line and threshold in the import bar apply to all of them; any row can override its line or threshold and name. One click imports and queues all; files that fail (e.g. already imported) stay listed with the reason. A plain click on a file still picks just that one.
- A file rejected as a duplicate is cleared from the import bar.
- Installers: **none**. Restart the backend.

## 3.5.5 (2026-10-23)
- Review opens on a **media library** instead of a short list: every playable dive and incident clip, **newest first and grouped by day** (Today / Yesterday / date), with type, line, time, length, depth and defect events per type (Deformation / Pitting), the dive each clip came from, and the TEST badge.
- **Pin** any row to keep it at the top. Pins are saved in the database (`videos.pinned_at`, added automatically on start), so they survive restarts and are the same in every browser. New `PUT /api/videos/{id}/pin`.
- Search (press **/**), filters **All · Dives · Incident clips · Pinned**, a line filter when several lines have media, and sort by newest / oldest / most defects / name. The filter and sort are remembered per browser.
- While reviewing, **Library** (top right) goes back to the list; the *Reviewing* drop-down lists pinned items first, then dives and clips newest first.
- Installers: **none**. Restart the backend (the database column is added on start).

## 3.5.4 (2026-10-23)
- Gemini model choice is two presets: **Flash · standard** (gemini-3.8-flash, default) and **Flash-Lite · bulk** (gemini-3.5-flash-lite, ~3× cheaper and faster, weaker visual check); other models under "Other model". The report panel shows "· bulk" in amber while Flash-Lite is on.
- Installers: **none**.

## 3.5.3 (2026-10-23)
- Gemini default model is now **gemini-3.8-flash**. Google no longer offers gemini-2.5-flash to new users (404 "no longer available to new users").
- Choose the model in System (gear) → Gemini report assistant (list comes from your key; saved in `backend/settings.json`). A `GEMINI_MODEL` environment variable still overrides it.
- A "model no longer available" error names Google's suggested replacement, with a one-click **Use … and test again**.
- Gemini 3 models are called with their default temperature (Google advises against lowering it); Gemini 2.x keeps 0.2.
- Installers: **none**. Restart the backend.

## 3.5.2 (2026-10-23)
- Fix: Gemini "Test connection" failed with "Cannot send a request, as the client has been closed." Recent google-genai versions close the client as soon as it is no longer referenced; the app now keeps it for the whole request and closes it afterwards (reports too).
- Installers: **none**. Restart the backend.

## 3.5.1 (2026-10-23)
- Gemini key is found even when the backend did not inherit it: process environment, then a `.env` file in the app folder (`GEMINI_API_KEY=...`), then the Windows user / system variables read directly. The Gemini card shows where it was found.
- Installers: **none**. Restart the backend.

## 3.5 (2026-10-23) — field feedback
- Live alert card: the list of earlier incidents stays put and scrolls (it used to be rebuilt, and jump back to the top, at every new incident); a visible scrollbar; **Show all** opens every incident of the dive with Review / Report.
- Live boxes are smoothed: each box follows the same object from frame to frame and eases to its new position, labels stay readable, and the shown confidence moves in 5 % steps. Display only; recorded detections are unchanged.
- Review: Previous / Next defect are greyed out when there is no defect in that direction; full screen now shows the player with its boxes and a small control bar; the sensor line says what it is ("ROV depth / heading log: N readings over 0:05 …").
- Gemini: if a request fails the report is still drafted (rule-based) and says why, with what to do. New **Gemini** card in the System drawer: key detected / key type (AQ. auth key or AIza), SDK version, and **Test connection**. Explains the known problem with new "AQ." keys.
- Installers: **none**. Restart the backend.

## 3.4.1 (2026-10-23)
- Fix (Windows): Test Mode's ROV simulator failed with "WinError 10048 ... Only one usage of each socket address". It now sends from a free local port instead of the backend's port 14551.
- Installers: **none**. Restart the backend after copying.

## Model hand-over (2026-10-23)
- Dataset tools and the Blender / YOLO11n guide moved to the separate **Mooring Dataset Kit** (`mooring-dataset-kit.zip`).
- `tests/test_model_acceptance.py` can evaluate the kit's real test split: `FYP_VAL_SPLIT=test`.
- Installers: **none**.

## 3.4 (2026-10-23) — UI redesign
- New layout from the approved mock-up: header with Live · Review · Batch, line selector and status pills (ROV, YOLO, GPU, TEST); a System drawer (gear) for the model, Test Mode, GPU sharing, full event log and reset.
- Live: incident alerts stack ("N this dive") with Review and Report on every incident; ROV depth card; zone twin with defect counts per zone; Detection card. **Detection now defaults to Auto** and says why it is waiting (no ROV depth / ROV above 2 m).
- Review: play/pause, previous / next defect, labelled lanes (position, deformation, pitting, other objects), "Analysis shown" with re-analyse presets + custom value, ROV at playhead, report assistant in the sidebar.
- Batch: one-line import bar; dives table with defects per type, **Re-analyse on incident clips**, **Locate file…** for offline media (fingerprint-checked; results kept). Records with results can no longer be removed from the UI. Registry zone click filters the table.
- Event logs: time · type tag · message, IBM Plex Sans for skimming; bounding-box labels in a wide technical font (Saira).
- Cancelling a batch run now also clears its general-object log.
- API: `POST /api/videos/{id}/locate`; `/api/videos` adds `defectEvents` and, for clips, `incident`; `/api/history` adds `byVideo`; live state adds `armWait`, `autoArmDepth`.
- Installers: **none**. Copy the code; `frontend/node_modules` stays as it is (no new packages).

## 3.3 (2026-10-23)
- Reports state what they cover: "Live dive in progress - data up to T+12:34 (snapshot 04:41:07)", "Whole live dive (ended ...)", or the clip / video.
- Finished live dives are listed in the Report Assistant on the Live tab, so a dive can be reported after the stream stops, even if it had no incident.
- Live-dive reports now link the incident-clip key frames to the dive's own events (previously the AI could not give a visual verdict on them).
- Every AI report shows the actual Gemini token use (input / output / thinking, number of requests) in the report, the event log and the HTML export.
- Installers: **none**. One database column is added automatically on start.

## 3.2 (2026-10-22)
- The Defect Registry can be filtered by line segment (depth zone); each zone shows its own count.
- Batch Ingest imports from this PC only; "Upload from device" was removed from the UI.
- Installers: **none**.

## 3.1 (2026-10-22)
- Test Mode (simulated ROV camera + BlueROV2 telemetry, stand-in defects); a general detection log for non-defect classes.
- Installers: **none** (needs ffmpeg, already required for clips).

## 3.0 (2026-10-21)
- BlueROV2 MAVLink telemetry, separate sensor DB, GPU arbiter, recorded thresholds / re-analysis, in-place import, Report Assistant.
- Installers: **`pip install -r backend/requirements.txt`** (adds `pymavlink`).
