# Field test: dress rehearsal with the real BlueROV2

Everything in `tests/` runs against simulators, replays and a stand-in detector. This procedure checks the parts only
real hardware can: the BlueROV2's MAVLink and camera, your trained model on your GPU, and a live dive from connection
to approved PDF. Print it, fill in the **Result** column on site, and put the completed table in the report
(FYP 2 *System Testing & Validation*).

**Before the field day (at your desk, ~15 min)**

| ID | Check | How | Pass when | Result |
|---|---|---|---|---|
| MA-1 | Model loads on the GPU | `tools\acceptance.bat <best.pt>` | *PASSED test_ma_1*: device `cuda:0`, defect classes found | |
| MA-2 | Fast enough for live | same run | median < 33 ms per 720p frame (printed) | |
| MA-3 | Accuracy (optional) | add the dataset `data.yaml` as 2nd argument | mAP@0.5 and deformation recall meet the targets you agreed | |
| MA-4 | False alarms on clean real chain | add `data\yolo_v2\images\test` as 3rd argument | deformation at or above 0.60 on ≤ 1 % of frames | |
| FT-0 | One-click start | double-click `start.bat` | the UI opens at `http://localhost:8500`; header shows the model name | |

**Record the conditions:** date and time ______; site ______; water clarity (visible distance) ______;
light ______; chain(s) and bar diameter ______; defect links on the test chain (from your defect log) ______;
camera stream settings in BlueOS ______; laptop on mains power: yes / no.

## A. Vehicle link (on the bank, vehicle in a tub or at the edge)

| ID | Step | Expected | Result |
|---|---|---|---|
| FT-1 | BlueOS → *MAVLink Endpoints* → add a UDP client to `<laptop IP>:14551`. Start the app. | ROV pill turns to **ROV link · ~N Hz** within 5 s | |
| FT-2 | Read the depth at the surface and at two depths (e.g. hold it at 0.5 m and 1.0 m against a marked pole) | app depth = BlueOS depth ± 0.1 m; the twin moves the ROV node | |
| FT-3 | Arm / disarm; change flight mode | both show on the depth card within 1 s | |
| FT-4 | Unplug the tether (or disable the endpoint) for 15 s, then reconnect | values **STALE** after ~3 s, **offline** after ~10 s, live again within 5 s of reconnecting | |

## B. Camera and detection

| ID | Step | Expected | Result |
|---|---|---|---|
| FT-5 | Live tab → connect the BlueOS stream (UDP 5600 H.264, or the RTSP address BlueOS shows) | **LIVE** within 5 s, ~30 fps capture (System drawer) | |
| FT-5b | With the app connected, open the same stream in **QGroundControl** (video on UDP 5600) | both play at once and the app keeps ~30 fps. If one stops, use the RTSP address for the app, or a relay (MediaMTX) on the topside laptop | |
| FT-6 | Hold a phone stopwatch in front of the camera; photograph the laptop screen and the stopwatch together | screen lags the stopwatch by ≤ 0.5 s (write the measured lag). Optional: repeat with `decoder_threads = 0` under `[live]` in `config.toml` (the old behaviour) to measure what the 3.6.5 setting saves on your PC | |
| FT-7 | System drawer → GPU card during detection | live inference ≥ 15 fps; note fps and ms | |
| FT-8 | Detection on **Auto**; lower the vehicle from the surface to > 2 m | *waiting (ROV above 2 m)* at the surface, **armed** below 2 m | |

## C. Inspection runs along the test chain

Before FT-9, press **Record** in QGroundControl (or start the DVR / onboard camera). The app keeps 5 s incident clips
and the dive log; the full dive video comes from that recorder and is imported in FT-15.

| ID | Step | Expected | Result |
|---|---|---|---|
| FT-9 | 2-minute pass along **clean** chain only (both chains if you have them) | **no incident clip**; note "one-off hits ignored" from the Detection card | |
| FT-10 | Pass each **deformation** link (bent, broken) at 2 distances | a clip is saved for each, the alert shows the depth and zone; record which links were caught | |
| FT-11 | Pass each **pitted** link close up | pitting shown and logged (it never saves a clip on its own); record which were seen | |
| FT-12 | Stop the dive; Review → open the newest clip | plays in Chrome/Edge; trigger marker on the defect; lanes show the sighting; depth at the playhead | |
| FT-12b | Review → *Live dive logs* → this dive | depth trace matches the dive profile; one clip marker per FT-10 clip (click opens it); FT-11 pitting as hollow dots; the FT-4 link loss is listed under *Vehicle events*; `.tlog` downloads | |

Fill in one line per defect link for FT-10 and FT-11. This is your real-world detection result:

| Link no. | Defect (from the defect log) | Pass distance | Detected? (clip / logged / no) | Peak confidence | Notes (angle, murk, blur) |
|---|---|---|---|---|---|
| | | | | | |

## D. Report, batch, persistence

| ID | Step | Expected | Result |
|---|---|---|---|
| FT-13 | Report on the newest incident (Gemini if there is signal, otherwise the rules) | draft opens; grounding check passes; note the time taken | |
| FT-14 | Edit a line, approve with your name, **Download PDF** with your template | PDF opens; findings, key frames, approval and page numbers correct | |
| FT-15 | Batch → import the full dive recording (and its `.tlog` if recorded) at 0.40 | processes; defects per zone appear in the registry | |
| FT-16 | Start a batch analysis **during** a live dive | live inference fps stays within ~20 % of FT-7 (GPU pill shows live first) | |
| FT-17 | Close the app and run `start.bat` again | dives, clips, reports and settings are all still there | |

**Anything unexpected** (time, what happened, screenshot file name):

1. ______
2. ______

When finished, copy `backend\fyp_database.db`, `backend\fyp_sensors.db` and `backend\media\` to a backup drive.
