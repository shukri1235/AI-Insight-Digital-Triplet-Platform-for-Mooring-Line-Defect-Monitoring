# Verification & Validation Report: Mooring Line Digital Twin

*Iteration 3.4 (UI redesign) · scope: the three operational scenarios (Live, Review, Batch),
BlueROV2 telemetry, single-GPU sharing, threshold handling, in-place ingest, inspection reports,
Test Mode and general (non-defect) detections*

> **Verification** asks *did we build it right?* Each requirement derived from the use-case
> scenarios is checked by an automated test, a measurement or an inspection.
> **Validation** asks *did we build the right thing?* That needs (a) the real model and the real
> BlueROV2 on the target hardware and (b) the inspection engineer using the system (§7).
> Live results: `tests/results/RESULTS.md` (regenerate with `python tests/make_report.py`).

---

## 1. Approach

| Level | What | How | Where |
|---|---|---|---|
| Use-case review | Each scenario step read against the code, then against itself | Inspection | §2 |
| Integration / system | Every scenario step, replayed exactly as written (120.5 m, 88 %, 0.60 → 0.25, 4-hour file) | `pytest`, real FastAPI app + threads + both SQLite DBs + ffmpeg + MAVLink over UDP | `tests/test_scenario{1,2,3}_*.py` |
| Component | GPU arbiter scheduling (priority, chunking, result equivalence) | `pytest` against the arbiter alone | `tests/test_gpu_arbiter.py` |
| UI acceptance | Same scenarios driven through the browser | Playwright | `tests/e2e/scenarios.mjs` |
| Non-functional | Frame pacing during disk writes, live vs batch on one GPU, RAM, 4-hour scale, throughput, crash recovery | Measurement with pass thresholds | `test_nfr_*`, `test_ga_*`, `test_tc_3_4_*` |
| AI component | Real `best.pt`: loads on CUDA, < 33 ms/frame, mAP@0.5 and deformation recall | `model.val()` on the held-out split | `tests/test_model_acceptance.py` |

**Ground truth by construction.**
- **Synthetic dive footage.** The scenario tests use coloured targets at known times and positions, plus a deterministic colour-blob detector (`tests/fakes/ultralytics`):
  - red: "DEFORMATION 88 %" at 8.0–11.0 s
  - small yellow: "pitting 32 %" at 8.0–12.0 s
  - cyan: "corrosion 80 %" at 14–17 s
  - green: a non-defect "mooringline" class
- **BlueROV2 telemetry.** It comes from `tools/bluerov_sim.py`, which sends the same ArduSub MAVLink messages the vehicle sends through BlueOS (HEARTBEAT, VFR_HUD, ATTITUDE, SCALED_PRESSURE2, SYS_STATUS, RANGEFINDER, STATUSTEXT) over UDP to the backend's real ingest path. Nothing in the backend is test-specific. Historical-dive tests use generated `.tlog` files with a known depth profile.
- **Gemini.** It is replaced by a recording test double (`tests/fakes/google/genai`). It can answer correctly, hallucinate once, or always hallucinate. The tests can then inspect the exact facts packet and images sent, and no tokens are spent.

---

## 2. Use-case review: discrepancies and design decisions

| # | Scenario text | Problem | Resolution |
|---|---|---|---|
| D1 | S2.4 "engineer selects the **Touchdown Zone** node" for the defect found "at **120 meters**" | On ML-01 the Mid-Water Zone is 50.1–145 m (30.1–145 m before 3.6.8), so the report would be attributed to the wrong segment. | Reports now take the segment from the **ROV depth** at each event, never from the UI selection. A Touchdown report contains no 120 m events (FR-2.7). Correct the scenario text to *Mid-Water Zone*. |
| D2 | S1.4 "exact 150 frames (5 seconds)" | These are only equal at 30 fps. PAL subsea cameras run at 25 fps. | The buffer is sized by time: 150 frames @ 30 fps, 125 @ 25 fps (TC-1.4-25fps). |
| D3 | S1.4 file name without a time of day | Two incidents at the same depth on the same day would collide. | `_HHMMSS` suffix added. |
| D4 | S2.1 "the exact **millisecond**" | Video resolves to one frame (33 ms), not 1 ms. | Timestamps are stored to 1 ms and the marker is **frame-exact** (TC-2.2). |
| D5 | S3.2 "FastAPI accepts the file instantly" | SHA-256 reads every byte of a multi-GB file. | **Import in place**: the file is registered without a copy and hashed in the background (HASHING state). Uploads from another device are hashed while they are received. |
| D6 | S3.2 batch "using **100 % of the GPU**" | This contradicts S2, where live keeps running. A single PC has one GPU. | **GPU arbiter** (§2.1). Batch gets the whole GPU only when no live dive is running. |
| D7 | S1.2 "reads the incoming depth telemetry" | Depth is not carried in the video stream. | The BlueROV2 is read directly over MAVLink (`rov_link.py`). With no vehicle, the UI shows *ROV offline* and **no simulated values**. |
| D8 | S2.3 lowering the slider "reveals secondary pitting" in Review | A slider in Review rewrites history: the alert and clip were decided at another threshold. | Review **shows the recorded threshold** (an indicator, not a slider). **Re-analyse at…** creates a *new* analysis and keeps the original (FR-2.3/2.4). The live slider is the clip **trigger** (default 0.60). Batch requires a threshold before processing. |
| D9 | S2.4 Gemini "analysis" of whatever is on screen | It was ungrounded: free text, UI-supplied numbers, and no check of what the model claims. | **Inspection Report Assistant** (§2.2). |
| D10 | S2.5 "physically preventing spamming" | A lock in the browser is bypassed by a refresh or a second tab. | Server-side cooldown (HTTP 429 + `Retry-After`). Concurrent requests spend tokens once. |
| D11 | S1.5 alert when not on the Live tab | Not specified. | A global incident banner appears on every tab (E2E-1.6). |

### 2.1 Sharing one GPU (FYP demo PC)

In production, live and batch would run in separate containers on separate GPUs. On one laptop GPU, a
single **arbiter thread** owns the model and the GPU. Work arrives on a priority queue:
- **Live frames (priority 0) are always served first.** While a live dive is active, batch frames are processed in chunks of 2, and any waiting live frame is served between chunks. A live frame therefore waits behind at most one small chunk, instead of a whole batch.
- **With no live dive, batch gets full batches.**
- **One model and one CUDA context**, so no VRAM is duplicated.
- **Visible in the UI.** The Hardware card shows each pipeline's share and the live wait time. This is the evidence to show in the presentation.

### 2.2 Inspection Report Assistant (replaces "Run Gemini Analysis")

1. **Facts packet built by the backend.** It contains defect events (clustered detections with peak confidence), depth, segment, water temperature and altitude from the sensor DB, the analysis threshold, the asset geometry, and the history of previous inspections.
2. **Key frames.** For each event, a full frame with the box drawn and a 2× crop are sent as images, so the model can give a *visual* second opinion.
3. **Structured output.** Grade on a fixed scale (ACCEPTABLE / MONITOR / REPAIR / REPLACE; no remaining-life guesses). Actions from a fixed list, citing event IDs. A visual verdict per event.
4. **Grounding check in code.**
   - Every cited event must exist.
   - Type, depth (±0.5 m) and segment must match.
   - A visual verdict is only allowed for events that have a key frame.
   - Every depth or percentage quoted in the prose must exist in the facts.
   - On failure, the model is asked once to correct itself. If it still fails, the report is marked **NEEDS_REVIEW** and the errors are listed.
5. **Engineer in the loop.** Grade, verdicts and notes are editable. **Approve** records the engineer's name and locks the report. Export to print as a PDF. Clicking a cited event opens that moment in Review.
6. **No API key.** A rule-based draft goes through the same check.

### 2.3 Test Mode and general detections (iteration 3.1)

**Constraint.** The vehicle is not available, and `best.pt` is not ready because data collection is still in progress. The official defect scope is **deformation (breakage)** and **pitting corrosion**.

**How Test Mode works:**
- **Camera.** A chosen video is broadcast as the "ROV camera" over UDP (the BlueOS default). It is continuous, so connecting later joins mid-stream, like a real camera.
- **Telemetry.** The BlueROV2 simulator feeds the real MAVLink link.
- **Tagging.** Every record made while Test Mode is on is tagged TEST (sensor rows: `SIM`). Test and real data are never aggregated together.

**Generic models (`yolo11n`).**
- **Drawing.** Boxes are drawn for every class.
- **Logging.** Non-defect classes are logged in an object log in the sensor DB. The defect DB, registry, clips, alerts and reports only ever see the two defect types.

**Stand-in defects (opt-in, Test Mode only).** A generic class can rehearse a defect. The raw class and the mapping are kept on every record, so a rehearsal can never be mistaken for a finding.

**Where this sits in the evidence chain.** Test Mode verifies the *system* end to end without hardware. It does **not** validate detection quality or the vehicle interface; that remains MA-1..4 and the field test (§6, `docs/FIELD_TEST.md`).

---

## 3. Defects found *by* V&V (and fixed)

| ID | Found by | Defect | Fix |
|---|---|---|---|
| B1 | TC-3.4 (kill -9 mid-job) | A crashed batch job restarted from 0 % (rows were committed every 400 rows only). | Commit every 400 rows **or every 5 s**. The resume point is the last commit. |
| B2 | NFR-1 | Encoding a clip on all cores caused late capture frames on a 2-core host. | The encoder runs below normal priority, x264 is limited to 2 threads, and NVENC is used when available. |
| B3 | TC-3.1 race analysis | Watch-folder hashing inside `GET /api/videos` could double-hash a file and collide on the unique hash. | The watch folder was removed: in-place import + a background hasher. |
| B5 | TC-3.1b | `.tlog` resampling drifted (≈ 4 Hz instead of 5 Hz), because each next sample time was based on the jittery arrival time. | Samples are taken on a fixed time grid. |
| B6 | TC-3.1b | `.tlog` readings from *before* the video start were clamped to t = 0 and stacked there, so the review showed a stale depth at 0 s. | Pre-roll readings are dropped. |
| B7 | TC-1.8 | A missing `.tlog` path was handed to pymavlink, which treats unknown names as **serial ports**. | The file is checked first, with a clear error. |
| B8 | NFR-1b | The live "YOLO fps" was 1/latency (it ignored idle time) and carried over between sessions. | It is now measured as throughput, reset per session, and reported as 0 in Standby. |
| B9 | TC-3.1b | A bad `.tlog` upload overwrote the previously attached log before validation. | It is written to a temp file, validated, then swapped in. |
| B10 | NFR-5 | A 4-hour sensor log (72 000 rows) took 5 s to load through the ORM. | Plain SQL (≈ 2.9 s), plus `?max_hz=1` thinning for long dives (≈ 0.7 s). Review uses thinning above 1 h. |
| B11 | E2E-2.4a | The report scope chosen on one tab carried over to another media file. | The scope defaults to the most specific item for the current tab and file. |
| B12 | Screenshot inspection | Times were shown in UTC (8 h off in Malaysia). | All UI and report times are shown in local time. |
| B13 | TM-2 (full-suite run) | Attaching a `.tlog` cleared *all* of a media's rows in the sensor DB, including its general-detection log. | Re-attaching a log now only replaces sensor samples. |
| B14 | Screenshot inspection (Test Mode) | An alert from the previous dive stayed on screen when a new dive started, so it looked current. | Alerts are cleared when a new live session begins. |
| B15 | E2E-T.5 (3.4) | The alert stack listed an incident twice and kept "Clipping incident…" after the clip was saved: each stage of an incident has its own event number. | Stages are grouped by the trigger frame (`triggerSeq`); only the latest stage is shown. |
| B16 | Screenshot inspection (3.4) | The report modal opened inside the narrow Review sidebar (the cards' entrance animation made it the containing box). | Modals render at page level (React portal). |
| B17 | Code review (3.4) | Cancelling a batch run left that run's general-object log behind. | `_finish_cancelled` also clears the run's object log (UI-5). |
| B18 | Field use on Windows (3.4) | Test Mode's ROV simulator failed with WinError 10048: on Windows pymavlink's send-only UDP socket binds the destination port (14551), which the backend already listens on. | The simulator sends from its own socket on a free local port (TM-9 regression test). |
| B19 | Field use (3.5) | During long detections the incident list could not be browsed: the alert card was re-mounted at every new incident, resetting its scroll. | Only the headline re-animates; the list is stable, scrolls visibly, and "Show all" lists every incident (E2E-UI.3). |
| B20 | Field use (3.5) | Full screen in Review showed the bare video without bounding boxes. | The player box (video + overlay + controls) goes full screen (E2E-UI.7). |
| B21 | Field use (3.5) | A Gemini failure surfaced only as a generic error; with the new AQ. keys the cause was invisible. | Failure → rule-based draft with the reason and a fix; key diagnostics and a connection test (UI-6, E2E-UI.5). |
| B22 | Field use (3.5.1), google-genai 2.32 | The connection test used a temporary Gemini client; the SDK closes it when garbage-collected, so the request failed ("client has been closed"). The test double did not model this. | The client is held for the whole call and closed afterwards; the test double now closes on garbage collection like the SDK (UI-6). |
| B23 | Field use (3.5.2) | The default model gemini-2.5-flash is closed to new users (404). | Default gemini-3.8-flash; model chosen in the app; 404 names the replacement with a one-click switch; Gemini 3 keeps its default temperature (UI-8). |
| B24 | Field use (3.5.5) | The Review start screen was a short, unsorted list; hard to find a dive among many clips. | Media library: newest first by day, search, filters, sort, pins stored on the server (UI-9, E2E-UI.10–UI.12). |
| B25 | Field use (3.6) | Live sightings and Review lanes looked clickable but were not; no sign a report was being drafted; no place to find past reports; no dates in lists; fixed sort orders; one file per import. | Clickable clip spans and lane bars, background report jobs with staged progress, Reports tab, date columns, sortable headers, multi-file import with per-file overrides (UI-10, E2E-UI.13–UI.18). |
| B26 | Field use (3.6.1–3.6.2), Gemini 3.8 Flash at high demand | Drafts took ~4 min, then failed with 503 "high demand": the SDK call had no time limit, and a busy model had no alternative except the rules. The test double never simulated a busy model, and it counted images at the Gemini 2 rate (258 tokens instead of 1,120). | 90 s request timeout and low thinking; one quick retry, then Flash-Lite, then the rules, all within one 150 s deadline; *Redraft with Gemini* on rule-based reports; per-stage timings on every report. The test double simulates 503s and timeouts and uses Gemini 3 image token counts (UI-11..13, E2E-UI.19). |
| B27 | Field use (3.6.3) | A single frame at or above the trigger saved a clip; v2 put 0.68 on one frame of a normal edge-on link. Test Mode's stand-in "Add" was silently disabled with a trained defect model. "Export" was a print page, not a PDF. | Trigger must hold on 3 of 5 analysed frames (TC-1.9). The stand-in editor explains why no stand-in is needed. Server-side PDF with client templates (PDF-1..3, E2E-UI.21–22). Optional `config.toml` (CFG-1..3, E2E-UI.20). |
| B28 | Deployment review (3.6.4) | The database address was fixed to a SQLite file. Three statements were valid only in SQLite (`INSERT OR IGNORE`, `DEFAULT 1` on a boolean column, `COALESCE(boolean, 0)`), and PostgreSQL rejected each of them when tested. The UI needed a second server (`npm run dev`). | `database.url` setting, portable SQL, data copy tool (CFG-4, PG-1). The backend serves the built UI; `start.bat` / `start.sh` (UI-14). |
| B29 | Dive log (3.6.5) | Opened during a dive, the log showed no depth trace for the first ~2 s, and the latest ~2 s were always missing, because sensor readings are written to the database in 2 s batches. Detection-mode and recording changes were not saved with the dive (only trigger changes were). | Unwritten readings are merged from memory while the dive runs; operator changes are saved as they happen (LOG-1). |
| B30 | User report (3.6.5) | A 1-minute Test Mode dive showed a duration of 9 h 33 min. The dive had been left open overnight (the PC slept), and Stop set the duration to the whole open time even though nothing was recorded after the first minute. | The dive ends at its last frame or reading and the idle time is reported apart; old dives are trimmed when shown; app suspensions are logged (LOG-4, LOG-5). |
| B31 | Paper-trail work (3.6.6) | Re-reading the stop code showed the real cause of B30: a later stop (Stop again, Test Mode off, app shutdown) re-ran the end of the dive and overwrote its end time and duration. Separately, a Test Mode clip byte-identical to an earlier one failed to save on the unique fingerprint. | A dive is closed exactly once; an identical clip is saved without a fingerprint (LOGS-3, LOGS-4). |
| B32 | First full runs on the Windows target PC (3.6.7) | 94 then 95 of 101 passed. (1) Attaching a .tlog to a historical dive failed on Windows: the check copy `….tlog.part` was taken by pymavlink for a network address (a Windows path has a colon and the name did not end in `.tlog`), and the file was left open, which Windows will not replace. A real field bug, invisible on Linux. (2) The "no API key" tests found the developer's own Gemini key (app `.env`, Windows registry). (3) The dive-log test stopped its dive right after the clip, which on the faster PC was before the pitting was seen. (4) NFR-2's live-rate target (60 %) was missed by 0.9 fps in a run with a YouTube video playing on the same PC. | The log is opened as a file and always closed; tests ignore the developer's key and variables; the dive-log test runs past the defect window; NFR-2 re-run on an idle PC on mains power passed at 62 % (18.9 of 30.4 fps), target unchanged; final Windows run 101 passed, 4 skipped; `RESULTS.md` shows each failure's reason. |
| B33 | Browser walkthrough against the UI served by the backend (3.6.8, for the target PC) | Until now the walkthrough ran with a separate UI server on port 3000. Served by the backend itself (how the app is used), the walkthrough's own test-only endpoint was shadowed by the UI's catch-all route, so one scenario could not reset the AI cooldown and stopped (55 of 56 checks). A fault in the test harness, not the app. | The test backend registers the UI route last; 76/76 against the served UI. |

---

## 4. Results summary

See `tests/results/RESULTS.md` for the full matrix with per-test evidence.

**Overall: pytest 104 passed / 0 failed / 4 skipped · browser E2E 76 / 76 · PostgreSQL replay 118,436 statements, 0 incompatibilities.** The 4 skipped tests are the model acceptance tests (MA-1..4), which need the target GPU and your weights.

**Iteration 3.4 (UI redesign), affected areas re-run:** live, Test Mode, batch, segments, report-snapshot and the new UI-support tests: 55 passed (`test_scenario1_live`, `test_test_mode`, `test_scenario3_batch`, `test_segments`, `test_report_snapshots`, `test_ui_support`). Browser walkthrough rewritten for the new layout: **E2E 54 / 54** (3.5), including:
- UI-1 the System drawer;
- UI-2 Report from a live alert, scoped to that incident, while the dive keeps running;
- UI-3 the alert stack;
- UI-4 the registry zone filter;
- CB-1 Auto as the default mode.

| Req | Check | Result |
|---|---|---|
| UI-1 | Detection defaults to AUTO; live state says why it waits (`armWait` SHALLOW at 1 m, NO_DEPTH on a silent link) | ✅ (TC-1.7 extended) |
| UI-2 | Locate file: a different file → 409; a missing path → 400; the same file moved → accepted, results and analysis kept | ✅ |
| UI-3 | `/api/videos` gives defect events per type; an incident clip reports its trigger (deformation, confidence ≥ trigger, 120.5 m, dive time) | ✅ |
| UI-4 | `/api/history?line_id=` lists which dives have events in which zone (`byVideo`) | ✅ |
| UI-5 | Cancelling a run clears its general-object log | ✅ |

The measurements below come from a sandbox with 2 vCPU and no GPU, so treat them as *lower bounds* for the RTX 5060 PC.

| Req | Check | Acceptance | Measured | Result |
|---|---|---|---|---|
| FR-1.1 | Stream LIVE after connect; capture rate | < 5 s; > 27 fps | 0.07 s; 30.2 fps; network drop → auto-reconnect | ✅ |
| FR-1.3 | BlueROV2 telemetry over MAVLink; twin follows depth 5 → 190 m | Splash → Mid-Water → Touchdown; heading, Bar30, sonar, mode decoded | exact sequence; 37 msg/s; heading 133°, altitude 10.0 m at 190 m, ALT_HOLD | ✅ |
| FR-1.3b | No vehicle; tether loss | No values before a heartbeat; STALE at 3 s; offline at timeout; auto-recover | NO_ROV + `null` telemetry at boot; STALE at 2.6 s, offline at 4.7 s (timeout 5 s in test), reconnected | ✅ |
| FR-1.4 | 88 % deformation → High confidence (certainty, not severity; "Critical" before 3.6.8), at the ROV's depth | High confidence; < 250 ms from first visible frame; 120.5 m Mid-Water | High confidence; 50 ms; 120.5 m, Mid-Water | ✅ |
| FR-1.5 | Clip | 150 frames, 5.0 s, H.264, named | 150 frames, 5.00 s, h264, `ML-01-NORTH_Incident_120_5m_<date>_<time>.mp4`, trigger at 2.97 s | ✅ |
| FR-1.6 | Clip record | CLIP analysis logged ≥ 0.40 / trigger 0.60; one sensor sample per frame | as specified; 150 MAVLink samples, all 120.5 m | ✅ |
| FR-1.8 | Trigger slider | 88 % not clipped at 0.95; clipped when lowered to 0.60; both recorded | not clipped; clipped 0.05 s after the change; log `[0 s → 0.95, 8.6 s → 0.60]` | ✅ |
| FR-1.9 | Detection modes | Standby: no inference; Auto: armed only when submerged and on fresh telemetry | 0 inferences in Standby (video at 30 fps); Auto off at 1 m, on at 30 m, off on stale link | ✅ |
| FR-1.10 | Dive log | ~5 Hz into the separate sensor DB; `.tlog` replays the same readings; stop → no samples | 5.4 Hz; `fyp_sensors.db`; `.tlog` decodes to 120.5 m; 0 samples while stopped | ✅ |
| FR-2.2 | Marker → frame | Snapped frame shows the defect, previous frame does not | frame 89 shows it, frame 88 clean | ✅ |
| FR-2.3/2.4 | Recorded threshold; re-analyse at 0.25 | Clip shows deformation only (recorded ≥ 0.40); re-analysis adds pitting on the same frame; original kept | CLIP 0.40: deformation; REANALYSIS 0.25: + pitting at the trigger frame; both runs listed and switchable | ✅ |
| FR-2.6 | Report grounding | Facts from DB/sensors; key frames sent; hallucination caught | depth 120.5 m / Mid-Water from the sensor DB; 4 images; "EV-99" and "999 m" caught → regenerated → grounded; persistent → NEEDS_REVIEW | ✅ |
| FR-2.7 | Touchdown vs 120 m | A Touchdown report has no 120 m events | 0 Touchdown findings; 6 Mid-Water | ✅ |
| FR-2.8 | Token saver | 429 + Retry-After 30; 4 concurrent → 1 spend; survives refresh | as specified | ✅ |
| FR-2.9 | Sign-off | Edit → EDITED; approve → locked (409 on edit); export contains name | as specified | ✅ |
| FR-3.1 | Threshold mandatory; duplicates | 400 without a threshold; SHA-256 duplicate rejected; caught *before* upload | as specified | ✅ |
| FR-3.2 | Import in place | No copy; HASHING → COMPLETED; duplicate 409; media offline flagged; file kept on delete; browser local-only | as specified | ✅ |
| FR-3.4 | `.tlog` for a historical dive | Aligned to the video; depths correct; events get depth + segment | 5 Hz; 145.0 m at 0 s, 155.0 m at 10 s; deformation at 152.9 m → Touchdown | ✅ |
| FR-3.6 | Batch vs ground truth at the chosen threshold | At 0.25: all classes in their windows; at 0.60: no pitting | 6 / 8 / 6 detections, all inside; 0.60 run: deformation + corrosion only | ✅ |
| FR-3.8 | kill -9 at 40 % then restart | Resumes; no duplicates; complete | resumed from checkpoint; 240/240 frames; 0 duplicate detections | ✅ |
| TM-1 | Class mapping | breakage/deformation and pitting variants map correctly; no COCO class becomes a defect | 7 + 5 variants mapped; 0 of 80 COCO classes | ✅ |
| TM-2 | General detections | Logged in the sensor DB object log, never in the defect DB | `mooringline` ×4 in the object log (2–4 s at 2 fps); 0 defect rows | ✅ |
| TM-4 | Test Mode end to end | UDP camera + simulated ROV at 120.5 m; dive, clip and sensors tagged TEST/SIM; real registry unchanged | clip saved from the UDP stream at 120.5 m; sensor source SIM; real counts unchanged | ✅ |
| TM-5/6 | Stand-in classes | Rehearsal detections recorded as DEFORMATION with raw class kept; mapping fixed per dive; batch uses it | as specified; change during a test dive → 409 | ✅ |
| TM-7/8 | Separation and stop | Test reports marked; real reports exclude test media; stop ends the dive and the simulator | `[TEST]` title + banner; real report clean; ROV → offline | ✅ |
| GA-2 | Arbiter: live at 30 fps + 120-frame batch | Live wait p95 < 60 ms | 49 ms (without the arbiter the worst case is 2.4 s) | ✅ |
| LOG-1 | Live dive log while running and after | Series, zones, clips on the dive timeline, sightings with depth / zone / trigger at the time, operator changes in order, ≥ 2 `.tlog` after recording off → on | readable while live (unwritten readings merged); deformation 39–45 m Mid-Water linked to its clip; pitting logged, never a trigger; 6 changes in order; 2 `.tlog` | ✅ |
| LOG-2 | Chart thinning | ≤ ~2,000 points for 4 h at 5 Hz; deepest / shallowest kept | 72,000 → ≤ 2,001; one-sample spikes to 180 m and 3 m kept | ✅ |
| LOG-3 | Downloads | sensors CSV = every reading; sightings / events CSV; `.tlog`; path tricks 404 | as specified | ✅ |
| LOG-4 | Dive left open | Stop pressed 9 h after the last data → duration = data span, idle time reported apart; old dives trimmed when shown | old dive stored as 9 h + 44 s → shown 44 s, idle 9 h; new dive with no frames for 2.5 s (limit set to 1 s) → 0 s + 2.5 s idle | ✅ |
| LOG-5 | App suspended | A jump of minutes in the 0.1 s tick is logged as *App paused* with the open dive | "9 h 31 min" event on the dive; a normal tick logs nothing | ✅ |
| TIME-1 | Batch processing time | Recorded per analysis; speed and frames/s consistent; re-analysis timed; CSV of every batch run, no live/clip rows; a crash-resumed run adds up without the downtime | 20 s video in 1.28 s (15.6×, 31 fps, ffmpeg reader) on this sandbox; TC-3.4 resumed run 16.3 s of processing over 19.3 s wall with 0.9 s down, 1 resume | ✅ |
| TIME-2 | Read-ahead | Same dive with read-ahead off and on: identical detections and frames; less waiting for frames; not slower | 240 frames, identical detections; wait 1.19 s → 0.42 s; 11.95 s → 11.29 s (−5.5 %) on this sandbox, where the stand-in detector (40 ms/frame) dominates. The gain is bounded by the smaller of decode and detection time per frame. | ✅ |
| LAT-1 | Live decoder delay | H.264/MPEG-TS stream with the frame number coded in each frame; delay with 16 decoder threads (OpenCV default on a 16-thread PC) vs the app's 2 | 2 threads: 0.54 s less delay than 16 at 30 fps; 27 ms more than 1 thread. (Input-buffering flags: no measurable change, 0.4 s slower start, not used.) | ✅ |
| CFG-5/6 | Settings checked and explained | 8 bad values (type, range, choice, address, env var) → each reported with the reason and the default used; hits > window and log > trigger flagged; typo → "did you mean"; docs/CONFIG.md and config.example.toml generated from the same list; every recipe valid | as specified; E2E-UI.23 finds a setting by search and shows its explanation | ✅ |
| LOGS-1..4 | Event log paper trail; dive closed once; identical clip saved | browser entries appended one per line with date, type and source (a line break cannot forge a line); listing, download, no other file reachable; backend records dive start/end, batch run, approval; a second stop leaves the dive's end time and duration unchanged; a clip with an existing fingerprint is saved | as specified (E2E-UI.24: the page's entries reach the file) | ✅ |
| LBL-1 | ML-01 splash zone (3.6.8) | 0–50 m per API RP 2I; old 0–30 m databases migrated at start-up; an edited line left alone | 25 / 49.9 m Splash, 50.5 / 120.5 m Mid-Water, 152.9 m Touchdown; migration and keep-edited both checked | ✅ |
| LBL-2 | Labels state certainty, not severity (3.6.8) | High / Low confidence at `critical_conf` for deformation and pitting; old labels relabelled; Gemini facts carry `confidence_label`; rule-based grade unchanged; alert claims no severity | All checked; TC-2.4 grade still REPAIR | ✅ |
| LBL-3 | Pitting name and basis note (3.6.8) | CORROSION shown as "Pitting (grade 6–7)"; basis of the defect classes in HTML and PDF reports | Both present in HTML and PDF | ✅ |
| E2E-LOG | Browser | Live → Dive log; clip marker → Review at the trigger → back to the log; library filter | 4 / 4 | ✅ |
| NFR-1 | Capture timing inside the clip-write window | No gap ≥ 2 frame periods; late ≤ baseline + 1 | max gap 54.6 ms vs 66.7 ms limit; 1 late (baseline 0) | ✅ |
| NFR-1b | Inference 3× slower than the frame period | Capture stays at camera rate | capture 30.1 fps, YOLO 9.5 fps | ✅ |
| NFR-2 | Live + batch end to end | Capture > 25 fps; live YOLO ≥ 60 % of solo rate; live wait p95 < 120 ms | capture ≥ 29.7 fps; YOLO 30.4 → 20.6 fps; wait p95 55 ms; GPU split 62 % live / 38 % batch | ✅ |
| NFR-4 | RAM buffer | < 50 MB | 9.4 MB (raw would be 222 MB) | ✅ |
| NFR-5 | 4-hour dive: 72 000 sensor + 20 000 defect rows | registry < 1.5 s; review data < 3–4 s | registry 116 ms; defects 0.9 s; sensors 2.9 s (0.7 s at 1 Hz); report facts 2.2 s | ✅ |
| NFR-6 | 1080p30 H.264 decode ceiling, 2 vCPU | > 1× real time | 3.9× → **~62 min for a 4-hour dive on this sandbox** | ✅ (re-measure on target) |

**Reading NFR-2.** On this CPU-only sandbox, "GPU" time is CPU time. The arbiter still keeps live first: live
inference drops by about a third while a batch runs, and capture and display are untouched. On the RTX 5060
both inferences are ~10× faster, so the same scheduling leaves far more headroom.

---

## 5. Test environment & limits of this evidence

- **No GPU and no vehicle in the verification sandbox.**
  - GPU loading, FP16, NVDEC/NVENC and real inference latency are covered by MA-1/MA-2, which must be run on the RTX 5060 machine.
  - The BlueROV2 link was verified with a MAVLink simulator that sends the real ArduSub message set. Repeat TC-1.2 once with the vehicle on the bench (§6).
- **Detector test double.** Pipeline behaviour is verified; *detection quality* is not (that is MA-3).
- **Gemini test double.** The grounding check, retry, cooldown and sign-off are verified. The *quality* of Gemini's visual second opinion must be judged by the engineer on real footage (§7, T5).
- **Video stream.** Verified through the same OpenCV/FFmpeg path using MPEG-TS over TCP, including drop and reconnect. Repeat against the BlueOS RTSP stream.
- **Browser codecs.** The test Chromium lacks H.264, so the frame-exact seek was verified by decoding with OpenCV (TC-2.2). Repeat the E2E run in Chrome or Edge.
- **Windows specifics** (DirectShow, `BELOW_NORMAL_PRIORITY_CLASS`, drive letters in the file browser) are implemented but were exercised only on Linux here.

---

### PG-1: PostgreSQL compatibility

Every statement the app executes during the full pytest run was recorded, compiled by SQLAlchemy's PostgreSQL dialect with its values, and replayed in order through `psql` on **PostgreSQL 16.15**, against the schema the app creates there (`tests/postgres/run_pg_compat.sh`).

**Result:** 118,436 statements, **0 incompatibilities**. Of the 24 errors, all are artefacts of the replay itself, not of the app:
- 2 are the SQLite WAL check in the NFR-3 test.
- 21 are "already exists": the suite's own `create_all` was replayed after the schema had been created.
- 1 is an id-based delete. SQLite reuses row ids after deletes, PostgreSQL sequences do not, so the replayed delete targets different rows.

Run against PostgreSQL, the old forms fail (`COALESCE types boolean and integer cannot be matched`; `syntax error at or near "OR"`; `default expression is of type integer`), which shows the check catches this class of fault.

**Not covered here:** the psycopg driver itself, connection pooling against RDS, and network latency. Check these with a short smoke test on the real server: start the app with `database.url` set, import one dive, and draft one report.

## 6. Re-running on the target PC

On Windows, `tools\run_tests.bat` does the first two lines and regenerates `RESULTS.md`, which records the PC it ran
on (OS, CPU threads, GPU) and when each part was last run. Report the results from the target PC.

The browser walkthrough runs on the target PC with `tools\run_e2e.bat`. It installs Playwright and Chromium once
(about 150 MB; needs Node.js, which `start.bat` already uses), starts the test backend serving the built UI on port 8500,
runs the 76 checks, and records the PC in `tests/results/e2e_host.json`, so the `RESULTS.md` header states where the
walkthrough ran. Close the app first.

```bash
pip install pytest pymavlink pypdf          # pypdf: reads the exported PDFs back (PDF-1..3)
python -m pytest -v --junitxml=tests/results/junit.xml               # functional + NFR (~8 min)
set FYP_REAL_WEIGHTS=path\to\best.pt
set FYP_VAL_DATA=path\to\data.yaml                                      # held-out split
python -m pytest tests/test_model_acceptance.py -v -s                   # MA-1..4  (or: tools\acceptance.bat best.pt data.yaml clean_frames)
python tests/e2e/serve_test_backend.py   &   npm --prefix frontend run dev
node tests/e2e/scenarios.mjs                                            # browser walkthrough
python tests/make_report.py                                             # -> tests/results/RESULTS.md
```

**Field test:** `docs/FIELD_TEST.md` is the full procedure (MA-1..4, vehicle link, camera, a dive along the artificial-defect chain, report and PDF, persistence) with a results table to fill in on site. The short bench test below covers the vehicle link only.

**Bench test with the real BlueROV2 (≈ 15 min, record the results in the report):**
1. In BlueOS, go to *MAVLink Endpoints* and add a **UDP client** to `<laptop IP>:14551`.
2. Start the backend. The ROV pill should turn to *ROV link* within a second.
3. Check the depth reading with the vehicle in a bucket or tank against the BlueOS depth.
4. Unplug the tether. The values should turn STALE after 3 s, and the card should go offline after 10 s.
5. Arm and disarm, and change the flight mode. Each should show on the telemetry card.
6. Connect the camera (BlueOS *Video Streams* → RTSP) and confirm that an incident clip carries the vehicle depth.

Agree the MA-3 acceptance targets with your supervisor **before** running (e.g. mAP@0.5 ≥ 0.50,
DEFORMATION recall ≥ 0.70). Recall matters most for deformation, because a miss means no incident clip.

---

## 7. Validation with users (FYP 2 "System Testing & Validation")

Suggested protocol: 5–8 participants, such as your supervisor, classmates acting as inspection engineers, and ideally one industry engineer.

| Task | Scenario | Success criterion | Measure |
|---|---|---|---|
| T1 Connect to the ROV and identify the current segment | S1.1–1.2 | Correct segment named; understands the offline state | completion, time |
| T2 React to an incident and open the clip | S1.5 | Clip open in Review < 15 s after the alert | time, errors |
| T3 Confirm the defect on the exact frame | S2.2 | Paused on the deformed link | completion |
| T4 Find secondary corrosion | S2.3 | Reads the recorded threshold, re-analyses lower, finds the pitting | completion, understanding of "recorded vs re-analysed" |
| T5 Produce an approved report | S2.4 | Checks the key frames and AI verdicts, corrects at least one, approves | correctness, trust rating (1–5) |
| T6 Import a historical dive with its `.tlog` | S3 | Opens the first defect with the correct depth | completion, time |

After the session, run the **System Usability Scale** (10 items; a score above 68 is above average). Also ask one
open question per tab: "what would stop you trusting this screen offshore?" The *90 % reduction in review time* claim
needs a measured baseline. A straightforward comparison is the time to locate N known defects in a 30-min dive by
manual scrubbing vs. with the marker timeline.
