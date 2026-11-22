# Settings reference

<!-- Generated from backend/config_spec.py - do not edit by hand (python tools/make_config_docs.py). -->

The app runs with sensible defaults. A site changes them in **`config.toml`** in the app folder (next to `backend/` and `frontend/`). Start from `config.example.toml`, or press *System drawer → Settings → Create config.toml*.

**How to change a setting**

1. Open `config.toml` in a text editor (Notepad is fine).
2. Find the setting under its `[section]`, delete the `# ` at the start of its line, and edit the value.
3. Save and restart the app (`start.bat` / `start.sh`).
4. Open *System drawer → Settings*. Every value in use is listed with where it came from. A value the app cannot use (a typo, out of range) is shown in orange with the reason, and the default is used instead, so a mistake never stops the app.

**Which value wins:** environment variable › `config.toml` › built-in default. The environment variable (listed with each setting) is for passwords and one-off tests. A Gemini model picked in the app wins over the file.

**Writing values:** text in quotes (`"cpu"`), numbers and `true`/`false` without. Windows paths with `/` (`"D:/MooringTwin/media"`). Relative paths start from the folder of `config.toml`.

**Not here:** the Gemini API key (`.env` file) and the PDF look (*System → PDF report templates*).

## Common situations

Each recipe is a starting point - change one thing, test, and keep a note of what you changed and why.

### Clean chain keeps saving incident clips

*When:* False alarms: normal links seen at an angle, glare or marine growth reach the trigger.

```toml
[live]
trigger_hits = 4
trigger_window = 6
```

Or raise live.trigger_default to 0.70. Check afterwards that real deformations still clip (FT-10).

### Real deformations pass without a clip

*When:* The alert comes late or not at all although the deformation is visible.

```toml
[live]
trigger_hits = 2
trigger_default = 0.5
```

Expect more false clips; the defect log still records everything above live.log_conf either way.

### Live picture lags behind the camera

*When:* The stopwatch test (FT-6) shows more than about 0.3 s delay.

```toml
[live]
decoder_threads = 1
```

Each decoder thread adds one frame of delay. If capture then drops below the camera's frame rate, go back to 2.

### 60 fps or 4K camera

*When:* The System drawer shows the capture rate below the camera's, or clips trigger on very brief glimpses.

```toml
[live]
decoder_threads = 4
trigger_hits = 5
trigger_window = 8
```

Twice the frames per second: twice the hold window keeps the same 0.1-0.2 s rule. Batch keeps analysing batch.sample_fps frames per second of video, but decoding takes about twice as long.

### Shallow test site (tank, pool, lake)

*When:* Auto mode never starts detecting because the ROV stays above 2 m.

```toml
[live]
auto_arm_depth_m = 0.3
```

Or switch the Detection card to Armed for the session.

### Batch has to be faster

*When:* Imported dives queue up after a long field day.

```toml
[batch]
sample_fps = 1.0
```

Halves the detection work but not the decoding, and a defect must stay in view for 2 s to be seen twice. Check the Processing time hover first: if it says decoding is the bottleneck, a faster disk or GPU video decoding helps more than this.

### Laptop without an NVIDIA GPU (rehearsal only)

*When:* Trying the app on a laptop with no CUDA GPU.

```toml
[model]
device = "cpu"
[batch]
sample_fps = 1.0
```

Live detection will skip many frames on a CPU; use Test Mode to rehearse the workflow, not to judge speed.

### Records on a database server (cloud / AWS)

*When:* Several people or a cloud deployment share one set of inspection records.

```toml
[database]
url = "postgresql+psycopg://mooring:PASSWORD@my-db.xxxx.rds.amazonaws.com:5432/mooring"
```

Put the address with its password in the FYP_DATABASE_URL environment variable instead of this file. Copy existing data once with tools/migrate_db.py. Clips and .tlogs stay in paths.media_dir.

### Gemini is slow or often busy

*When:* Report drafts take minutes or keep falling back to the rule-based draft.

```toml
[gemini]
deadline_sec = 90.0
thinking = "low"
```

A shorter deadline gets you the rule-based draft sooner; you can press Redraft with Gemini later.

### System disk is filling up

*When:* Clips, .tlogs and report images grow over many dives.

```toml
[paths]
media_dir = "D:/MooringTwin/media"
```

Copy the existing media folder to the new place first, then change the path and restart.

## All settings

### Folders and files — `[paths]`

Where the app keeps its data. Point these at a bigger or backed-up drive; existing files are not moved for you - copy them across first, then change the path and restart.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Media folder**<br>`media_dir`<br><sub>env `FYP_MEDIA_DIR`</sub> | `"backend/media"` | text | Incident clips, browser uploads, review copies, .tlog files and report images. Dive videos imported from this PC stay where they are and are not copied here. | Move it to a larger drive if clips and .tlogs fill the system disk (roughly 1-3 MB per incident clip). |
| **Event log folder**<br>`logs_dir`<br><sub>env `FYP_LOGS_DIR`</sub> | `"logs"` | text | One text file per app session (session_<date>_<time>.txt): every event-log entry from the browser plus the backend's own record of live dives, incident clips, batch analyses and approved reports. A paper trail: files are only ever added to, never changed or deleted by the app. | Point it at a backed-up or shared drive if the trail must be kept with the inspection records. |
| **Model folder**<br>`models_dir`<br><sub>env `FYP_MODELS_DIR` · rarely changed</sub> | `"backend/models"` | text | Folder searched for detection weights (.pt files). | — |
| **Database file**<br>`database`<br><sub>env `FYP_DB_PATH` · rarely changed</sub> | `"backend/fyp_database.db"` | text | The local inspection database (dives, detections, reports). The ROV sensor log is a second file next to it (fyp_sensors.db). Ignored when database.url is set. | Keep it on a local drive, not a network share - SQLite over a network share can corrupt. |

### Database — `[database]`

Empty = two local files next to the app (the normal setup on one PC). Set an address to keep the inspection records on a database server instead (cloud / AWS RDS, several users).

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Database server address**<br>`url`<br><sub>env `FYP_DATABASE_URL`</sub> | `""` | text | Address of a PostgreSQL server for the inspection records, e.g. postgresql+psycopg://mooring:PASSWORD@my-db.xxxx.rds.amazonaws.com:5432/mooring . Empty = the local file. | Needs: pip install "psycopg[binary]", and tools/migrate_db.py to copy existing data across once. Put the address in the FYP_DATABASE_URL environment variable rather than this file when it contains a password. |
| **Sensor database address**<br>`sensor_url`<br><sub>env `FYP_SENSOR_DATABASE_URL` · rarely changed</sub> | `""` | text | Where the 5 Hz ROV sensor log goes. Empty = the same server as database.url (or the local sensor file). | Only set it to put the high-volume sensor log on a different server. |

### Detection model — `[model]`

Which trained weights are loaded and on which processor.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Weights loaded at start**<br>`default`<br><sub>env `FYP_DEFAULT_MODEL`</sub> | `"best.pt"` | text | File name in the model folder (or a full path). Another model can still be loaded from the System drawer. | Point it at the newest training run after retraining (or copy that best.pt into the model folder). |
| **Processor**<br>`device`<br><sub>env `FYP_DEVICE`</sub> | `"auto"` | "auto" / "cuda:0" / "cpu" | "auto" uses the NVIDIA GPU when one works, otherwise the CPU. | Force "cpu" only to test without a GPU - live detection on a CPU is several times slower. |
| **Half precision on the GPU**<br>`half`<br><sub>env `FYP_HALF` · rarely changed</sub> | `true` | true / false | FP16 maths on the GPU: about twice as fast, same detections in practice. | Turn off only if a GPU driver problem shows wrong or no detections. |

### Live dive — `[live]`

How the live feed is analysed, when an incident clip is saved, and how much delay the picture has.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Clip trigger (starting value)**<br>`trigger_default`<br><sub>env `FYP_LIVE_TRIGGER`</sub> | `0.6` | 0.1 to 0.99 | Deformation confidence that saves an incident clip and raises the alert. This is where the Live slider starts; the operator can still move it during a dive, and every change is recorded with the dive. | 0.60 was chosen for model v4 by the threshold sweep on the validation video (the lowest threshold with false alarms <= 1 % of clean frames; threshold_sweep.py). With a different model, re-run the sweep rather than guess. Raise it (e.g. 0.70) if clean chain keeps saving clips; lower it (e.g. 0.50) if real deformations are missed. Pitting never saves a clip, whatever this is. |
| **Show and log from**<br>`log_conf`<br><sub>env `FYP_LIVE_LOG_CONF`</sub> | `0.4` | 0.1 to 0.99 | Detections at or above this are drawn on the live feed (dashed below the trigger) and saved as sightings. | Lower = more weak sightings for the engineer to review (and more noise); higher = a cleaner log that may miss faint defects. Must be at or below the clip trigger. |
| **Hold rule: hits needed**<br>`trigger_hits`<br><sub>env `TRIGGER_HITS`</sub> | `3` frames | 1 to 30 | A clip is saved only when the trigger is reached on this many of the last trigger_window analysed frames, so a one-frame flicker (e.g. a normal link seen edge-on) does not save a clip. | More hits = fewer false clips but a defect must stay in view longer. 1 hit of 1 = any single frame (the old behaviour). At 60 fps use about twice the numbers (e.g. 5 of 8) to keep the same time span. |
| **Hold rule: out of the last**<br>`trigger_window`<br><sub>env `TRIGGER_WINDOW`</sub> | `5` frames | 1 to 60 | Window for trigger_hits: 3 hits out of the last 5 analysed frames means the defect must be seen for about 0.1-0.2 s at 30 fps. | Must be at least trigger_hits. |
| **Gap between clips**<br>`incident_cooldown_sec`<br><sub>env `FYP_INCIDENT_COOLDOWN`</sub> | `10.0` s | 0 to 600 | Minimum time between two incident clips, so one defect held in view does not save a clip every few seconds. | Shorter if defects are close together along the chain and each needs its own clip. |
| **Auto mode starts detecting below**<br>`auto_arm_depth_m`<br><sub>env `FYP_AUTO_ARM_DEPTH`</sub> | `2.0` m | 0 to 100 | In Auto mode detection runs only while the ROV is deeper than this, so launch and recovery at the surface do not raise alerts. | Use 0.3-0.5 in a test tank or shallow lake, or simply switch to Armed. |
| **Video decoder threads**<br>`decoder_threads`<br><sub>env `FYP_LIVE_DECODER_THREADS`</sub> | `2` threads | 0 to 32 | Threads that decode the camera stream. Each extra thread holds the picture back by one more frame (33 ms at 30 fps); 2 keeps the delay low and decodes 1080p30 easily. | Raise to 4 only if the System drawer shows the capture rate below the camera's (4K or 60 fps cameras). 0 = one per CPU core (OpenCV's default: about 0.5 s more delay on a 16-thread PC). |
| **High-confidence label from**<br>`critical_conf`<br><sub>env `FYP_CRITICAL_CONF` · rarely changed</sub> | `0.6` | 0.1 to 0.99 | A deformation or pitting detection at or above this confidence is labelled High confidence in the app and the report facts (below: Low confidence). The label states how certain the model is, not how severe the defect is. The rule-based report proposes REPAIR when a deformation event reaches it. | Keep it equal to live.trigger_default, so every deformation that saves a clip is High confidence. |

### BlueROV2 link — `[rov]`

Where the vehicle's telemetry (depth, heading, battery ...) arrives.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Telemetry address**<br>`mavlink_url`<br><sub>env `FYP_MAVLINK_URL`</sub> | `"udpin:0.0.0.0:14551"` | text | Where the app listens for the vehicle's MAVLink messages. In BlueOS -> MAVLink Endpoints add a UDP client to <this PC's IP>:14551. QGroundControl keeps its own port (14550), so both work at once. | Change the port only if 14551 is already used on this PC. |

### Batch (imported dive videos) — `[batch]`

How recorded videos are analysed after the dive. None of these change live detection.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Frames analysed per second of video**<br>`sample_fps`<br><sub>env `FYP_BATCH_SAMPLE_FPS`</sub> | `2.0` frames/s | 0.2 to 60 | Batch looks at this many frames per second of footage (2 = every 15th frame of a 30 fps video). A defect in view for 1 s is seen on at least 2 frames. | Higher finds short glimpses but takes longer (decoding is the slow part, so not proportionally); lower is faster but can miss defects that pass quickly. |
| **Frames per GPU batch**<br>`batch_size`<br><sub>env `FYP_BATCH_SIZE` · rarely changed</sub> | `8` frames | 1 to 64 | Frames sent to the GPU together. While a live dive runs, batch automatically uses chunks of 2 so live goes first. | Larger can be slightly faster on a big GPU; smaller if the GPU runs out of memory. |
| **Read videos with ffmpeg**<br>`ffmpeg_reader`<br><sub>env `FYP_BATCH_FFMPEG` · rarely changed</sub> | `true` | true / false | Uses ffmpeg (with GPU video decoding when available) to read dive videos; off = OpenCV. | Turn off only if a video fails to import with an ffmpeg error. |
| **Read-ahead (batches)**<br>`prefetch_batches`<br><sub>env `FYP_BATCH_PREFETCH` · rarely changed</sub> | `2` batches | 0 to 8 | Batches decoded in the background while the GPU analyses the current one, so the two overlap. Each batch holds batch_size frames in memory (about 2.7 MB per 1280x720 frame). | 0 = off (the old behaviour, for comparison). More than 2 rarely helps. |

### Report assistant (Gemini) — `[gemini]`

Limits for the AI draft. When Gemini is slow or busy the app falls back to a rule-based draft, so a report is never lost. The API key is NOT set here - it stays in the .env file.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Gemini model (starting choice)**<br>`model`<br><sub>env `GEMINI_MODEL`</sub> | `"gemini-3.8-flash"` | text | The model used for AI drafts until someone picks another in the app (the app's choice then wins). | Retired models are flagged in the System drawer with the suggested replacement. |
| **Fallback model**<br>`fallback_model`<br><sub>env `GEMINI_FALLBACK_MODEL`</sub> | `"gemini-3.5-flash-lite"` | text | Tried when the main model is busy, times out or runs out of quota, before the rule-based draft. | "" = no fallback (go straight to the rule-based draft). |
| **Thinking level**<br>`thinking`<br><sub>env `GEMINI_THINKING`</sub> | `"low"` | "low" / "high" | How much Gemini 3 models reason before answering. | "low" is much faster and enough for writing up given facts; "high" is slower - only to compare drafts. |
| **Overall deadline**<br>`deadline_sec`<br><sub>env `GEMINI_DEADLINE_SEC`</sub> | `150.0` s | 20 to 900 | Total time for an AI draft (all attempts and the fallback). After this the rule-based draft is used and can be redrafted with Gemini later. | — |
| **Gap between AI reports**<br>`cooldown_sec`<br><sub>env `GEMINI_COOLDOWN_SEC`</sub> | `30` s | 0 to 3600 | Minimum time between two AI drafts, to save tokens / quota. Rule-based drafts are not limited. | — |
| **Time limit per request**<br>`timeout_sec`<br><sub>env `GEMINI_TIMEOUT_SEC` · rarely changed</sub> | `90.0` s | 10 to 600 | Longest wait for one Gemini answer. | Raise on a very slow connection; the overall deadline below still applies. |
| **Quick retries when busy**<br>`retries`<br><sub>env `GEMINI_RETRIES` · rarely changed</sub> | `1` attempts | 0 to 5 | Extra attempts on the same model when it answers "busy", before trying the fallback model. | — |

### Reports and PDF — `[report]`

What goes into a report and which PDF template it uses.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Events with key frames**<br>`max_keyframes`<br><sub>env `FYP_REPORT_MAX_KEYFRAMES`</sub> | `4` events | 0 to 20 | How many events per report get pictures (a full frame and a close-up each). The images are sent to Gemini and printed in the PDF. | More = a fuller report but more tokens and a longer PDF. |
| **PDF template**<br>`template`<br><sub>env `FYP_REPORT_TEMPLATE`</sub> | `"default"` | text | Template used when none is chosen at export (a file name in the templates folder, without .json). | Templates are edited in System -> PDF report templates. |
| **Templates folder**<br>`templates_dir`<br><sub>env `FYP_REPORT_TEMPLATES_DIR` · rarely changed</sub> | `"report_templates"` | text | Folder of PDF templates (one .json and logo per client). | — |

### Server and network — `[server]`

Port and who may use the server from other PCs.

| Setting | Default | Allowed | What it does | When to change it |
|---|---|---|---|---|
| **Port**<br>`port`<br><sub>env `FYP_PORT`</sub> | `8500` | 1024 to 65535 | The app is opened at http://<this PC>:<port>. | Change it if 8500 is used by another program. Other PCs also need this port allowed in the firewall. |
| **Let other PCs browse this PC's folders**<br>`allow_remote_fs`<br><sub>env `FYP_ALLOW_REMOTE_FS`</sub> | `false` | true / false | Off: only someone at this PC can browse its drives to import dive videos. Others can still watch, review and write reports. | Turn on only on a trusted network where other PCs must import videos from this PC's drives. |
| **Test Mode camera port**<br>`test_stream_port`<br><sub>env `FYP_TEST_STREAM_PORT` · rarely changed</sub> | `5600` | 1024 to 65535 | UDP port of Test Mode's simulated camera (the BlueROV2's camera uses 5600 too). | Change it if QGroundControl on this PC is already receiving video on 5600 while you use Test Mode. |

## Fixed in the code

Kept constant so results stay comparable between dives and sites: the 5 s clip length with ~2 s after the trigger, 5 Hz sensor logging, the ROV link timeouts (stale after 3 s, offline after 10 s), the live picture width (1280 px) and the batch overlap rule (IoU 0.45). Change them in `backend/config.py` only with a reason, and re-run the tests.
