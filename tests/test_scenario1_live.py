"""
SCENARIO 1 - Active Dive Monitoring (Live Ops)

TC-1.1  Initialization: connect to the ROV stream; feed is a broadcast (no seeking)
TC-1.2  Digital Twin sync: BlueROV2 depth (MAVLink) moves the node Splash -> Mid-Water -> Touchdown;
        no vehicle = explicit offline placeholder, never simulated numbers; stale/lost detection;
        stale readings are never recorded with an incident clip (3.6.11)
TC-1.3  Critical anomaly: DEFORMATION detected at 88 % -> Critical, at the ROV's real depth
TC-1.4  Automated DVR buffer: exactly 5 s / 150 frames around the event, written off-thread,
        named <LINE>_Incident_<depth>m_<date>_<time>.mp4, coordinates + sensors logged
TC-1.5  Direct hand-off: alert carries the clip id; clip is registered without the ingest queue
TC-1.6  Live slider = clip TRIGGER threshold (default 0.60); every change is recorded
TC-1.7  Detection modes ARMED / AUTO (depth-gated) / STANDBY
TC-1.8  Dive log: sensor samples in the separate sensor DB + raw MAVLink .tlog per dive; a gap, never a
        repeated depth, while messages stop (3.6.11)
TC-1.9  The clip trigger must hold (3 of the last 5 inferred frames): flickers never save a clip
"""
import re
import subprocess
import time

import cv2
import pytest

from conftest import BOOT_ROV, GT, MEDIA, make_dive, run_live_until_incident, set_depth, wait_for
from vv_media import ffmpeg


# ---------------------------------------------------------------- TC-1.1
def test_tc_1_1_connects_and_streams(live_incident, evidence):
    states = live_incident["states"]
    first_live = next(s for s in states if s["status"] == "LIVE")
    assert first_live["_wall"] < 5.0, "stream should be LIVE within 5 s"
    fps = [s["captureFps"] for s in states if s["status"] == "LIVE" and s["captureFps"]]
    assert sum(fps[-20:]) / len(fps[-20:]) > 27, f"capture fps too low: {fps[-20:]}"
    evidence["TC-1.1"] = {"live_after_s": round(first_live["_wall"], 2),
                          "capture_fps_avg": round(sum(fps[-20:]) / len(fps[-20:]), 1)}


def test_tc_1_1_network_stream_and_reconnect(client, evidence):
    """A real network stream (MPEG-TS over TCP, same OpenCV/FFmpeg path as RTSP):
    connects, survives the source dropping, and reconnects."""
    src = make_dive(MEDIA / "net_src.mp4", seconds=6, boxes=False)
    port = 18555

    def serve():
        return subprocess.Popen([ffmpeg(), "-v", "error", "-re", "-stream_loop", "-1", "-i", str(src), "-c", "copy",
                                 "-f", "mpegts", f"tcp://127.0.0.1:{port}?listen=1"])
    server = serve()
    time.sleep(1.0)
    try:
        client.post("/api/live/start", json={"source": f"tcp://127.0.0.1:{port}", "line_id": "ML-01-NORTH"})
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 15, what="network LIVE")
        assert client.get("/api/live/state").json()["sourceKind"] == "network"
        seq1 = wait_for(lambda: client.get("/api/live/state").json().get("frameSeq", 0) > 30 and
                        client.get("/api/live/state").json()["frameSeq"], 10, what="frames flowing")
        server.kill(); server.wait()
        wait_for(lambda: client.get("/api/live/state").json()["status"] in ("RECONNECTING", "CONNECTING"), 15,
                 what="drop detected")
        server = serve()
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 30, what="reconnected")
        evidence["TC-1.1-network"] = {"frames_before_drop": seq1, "reconnected": True}
    finally:
        client.post("/api/live/stop")
        server.kill()


def test_tc_1_1_live_feed_cannot_be_rewound(client, app_module):
    """The live media path exposes no seekable resource: MJPEG has no timeline and the
    API has no seek/rewind operation for a live session."""
    routes = {r.path for r in app_module.app.routes}
    assert "/api/live/mjpeg" in routes
    assert not any("seek" in p or "rewind" in p for p in routes if p.startswith("/api/live"))
    assert client.get("/api/live/mjpeg").status_code == 409  # nothing to stream when not live


# ---------------------------------------------------------------- TC-1.2
def test_tc_1_2_no_rov_shows_offline_not_fake_values(client, evidence):
    """Before any vehicle talks, the backend reports NO_ROV and NO telemetry values."""
    assert BOOT_ROV["state"] == "NO_ROV", BOOT_ROV
    assert BOOT_ROV["telemetry"] is None
    evidence["TC-1.2-offline"] = dict(BOOT_ROV)


def test_tc_1_2_digital_twin_follows_rov_depth(client, rov, processed_dive, evidence):
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "detection_mode": "STANDBY"})
    try:
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        seen = []
        for depth in (5, 25, 60, 120.5, 150, 190):
            set_depth(client, rov, depth)
            tel = wait_for(lambda: (lambda t: t if t and abs(t["depth"] - depth) < 0.05 else None)(
                client.get("/api/live/state").json().get("telemetry")), 5, 0.1, f"depth {depth}")
            seen.append((depth, tel["segment"]))
        assert [s for _, s in seen] == ["Splash Zone", "Splash Zone", "Mid-Water Zone", "Mid-Water Zone",
                                        "Touchdown Zone", "Touchdown Zone"]
        st = client.get("/api/live/state").json()
        tel, link = st["telemetry"], st["rov"]
        assert link["state"] == "CONNECTED" and link["vehicle"]["isSubmarine"]
        assert tel["heading"] == pytest.approx(135.0, abs=2.6)          # VFR_HUD (sim sways +-2 deg)
        assert tel["temperature"] is not None and tel["pressure_hpa"] > 1013   # Bar30 (SCALED_PRESSURE2)
        # Ping sonar (RANGEFINDER) arrives at a lower rate than depth, so wait for it to catch up
        alt = wait_for(lambda: (lambda t: t["altitude"] if t and t.get("altitude") is not None and abs(t["altitude"] - 10) < 0.2 else None)(
            client.get("/api/live/state").json().get("telemetry")), 5, 0.1, "sonar altitude 10 m")
        assert alt == pytest.approx(200 - 190, abs=0.2)
        assert tel["mode"] == "ALT_HOLD" and tel["armed"] is True           # HEARTBEAT
        evidence["TC-1.2"] = {"depth_to_segment": seen, "msg_rate_hz": link["msgRateHz"],
                              "sample": {k: tel[k] for k in ("depth", "heading", "temperature", "pressure_hpa", "altitude", "mode")}}
    finally:
        client.post("/api/live/stop")
        set_depth(client, rov, 120.5)


def test_tc_1_2_link_loss_goes_stale_then_offline(client, rov, monkeypatch, evidence):
    """Tether loss: values are flagged STALE after 3 s and withdrawn (offline) after the
    lost timeout; the link recovers by itself when heartbeats return."""
    import rov_link
    monkeypatch.setattr(rov_link, "ROV_LOST_SEC", 5.0)
    rov.silent = True
    try:
        t0 = time.time()
        st = wait_for(lambda: (lambda s: s if s["state"] == "STALE" else None)(client.get("/api/rov/status").json()), 8, 0.2, "STALE")
        t_stale = time.time() - t0
        assert st["telemetry"]["stale"] is True
        st = wait_for(lambda: (lambda s: s if s["state"] == "NO_ROV" else None)(client.get("/api/rov/status").json()), 8, 0.2, "NO_ROV")
        t_lost = time.time() - t0
        assert st["telemetry"] is None
    finally:
        rov.silent = False
    wait_for(lambda: client.get("/api/rov/status").json()["state"] == "CONNECTED", 5, 0.2, "recovered")
    events = [e["kind"] for e in client.get("/api/rov/status").json()["events"]]
    evidence["TC-1.2-linkloss"] = {"stale_after_s": round(t_stale, 1), "offline_after_s": round(t_lost, 1), "events": events}


def test_tc_1_2_stale_telemetry_never_recorded_with_a_clip(client, rov, processed_dive, monkeypatch, evidence):
    """A clip saved while the link is STALE (no heartbeat for ROV_STALE_SEC, not yet lost) carries no depth:
    the alert has no telemetry, the clip is named NOROV and gets no per-frame sensor copy. Before 3.6.11
    the last known (old) readings were stored with the clip as if current."""
    import conftest
    import rov_link
    monkeypatch.setattr(rov_link, "ROV_STALE_SEC", 1.0)      # stale quickly ...
    monkeypatch.setattr(rov_link, "ROV_LOST_SEC", 120.0)     # ... and stay stale for the whole dive
    set_depth(client, rov, 120.5)
    monkeypatch.setattr(conftest, "set_depth", lambda *a, **k: None)   # the link is about to go silent
    rov.silent = True
    try:
        st = wait_for(lambda: (lambda s: s if s["state"] == "STALE" else None)(client.get("/api/rov/status").json()),
                      5, 0.2, "STALE")
        assert st["telemetry"]["stale"] is True and st["telemetry"]["depth"] == pytest.approx(120.5, abs=0.05)
        rec = run_live_until_incident(client, rov, f"video:{processed_dive['id']}", depth=120.5)
        still_stale = client.get("/api/rov/status").json()["state"] == "STALE"
    finally:
        rov.silent = False
    wait_for(lambda: client.get("/api/rov/status").json()["state"] == "CONNECTED", 5, 0.2, "recovered")
    assert still_stale, "the link must still be STALE when the clip is saved"
    trig = next(i for i in rec["incidents"] if i["stage"] == "triggered")
    assert trig["telemetry"] is None, "stale telemetry must not be attached to the alert"
    saved = rec["saved"]
    assert saved and saved["stage"] == "saved", rec["incidents"]
    assert "_Incident_NOROV_" in saved["clipName"], saved["clipName"]
    rows = client.get(f"/api/videos/{saved['clipId']}/telemetry").json()
    assert rows == [], f"{len(rows)} stale sensor rows stored with the clip"
    evidence["TC-1.2-stale-clip"] = {"clip": saved["clipName"], "alert_telemetry": trig["telemetry"],
                                     "clip_sensor_rows": len(rows)}


# ---------------------------------------------------------------- TC-1.3
def test_tc_1_3_deformation_detected_as_critical(live_incident, evidence):
    trig = next(i for i in live_incident["incidents"] if i["stage"] == "triggered")
    d = trig["defect"]
    assert d["type"] == "DEFORMATION" and d["rawClass"] == "deformation"
    assert d["confidence"] == pytest.approx(0.88, abs=1e-3)
    assert d["severity"] == "High confidence"   # certainty, not severity (3.6.8)
    # defect first appears at video t=8.0 s; detection latency from appearance to trigger
    latency = trig["sessionTime"] - GT["deformation"]["t"][0]
    assert 0 <= latency < 0.25, f"trigger {trig['sessionTime']} s, expected ~8.0 s"
    assert trig["telemetry"]["depth"] == pytest.approx(120.5, abs=0.01) and trig["telemetry"]["segment"] == "Mid-Water Zone"
    assert trig["triggerConf"] == pytest.approx(0.60)
    evidence["TC-1.3"] = {"confidence": d["confidence"], "severity": d["severity"],
                          "detection_latency_s": round(latency, 3), "depth": trig["telemetry"]["depth"]}


# ---------------------------------------------------------------- TC-1.4
def _frame(path, idx):
    cap = cv2.VideoCapture(path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
    ok, f = cap.read()
    cap.release()
    assert ok
    return f


def _is_red_at_box(frame, scale=1.0):
    x, y, w, h = GT["deformation"]["box"]
    b, g, r = frame[int((y + h / 2) * scale), int((x + w / 2) * scale)]
    return r > 180 and g < 70 and b < 70


def test_tc_1_4_dvr_clip_is_5s_150_frames_named_and_logged(app_module, live_incident, evidence):
    clip, saved = live_incident["clip"], live_incident["saved"]
    path = next(v.current_filepath for v in [app_module.SessionLocal().get(app_module.VideoRecord, clip["id"])])
    # exact frame count / duration / codec
    # counted with OpenCV, not ffprobe: the ffmpeg bundled for Windows has no ffprobe
    cap = cv2.VideoCapture(path)
    fourcc = int(cap.get(cv2.CAP_PROP_FOURCC))
    codec = "".join(chr((fourcc >> (8 * i)) & 0xFF) for i in range(4)).lower()
    codec = "h264" if codec in ("avc1", "h264", "x264") else codec
    frames = 0
    while cap.read()[0]:
        frames += 1
    cap.release()
    assert frames == 150, f"clip has {frames} frames"
    assert codec == "h264", "clip must be browser-playable H.264"
    assert clip["durationSec"] == pytest.approx(5.0, abs=0.1)
    # naming: ML-01-NORTH_Incident_120_5m_2026-10-21_HHMMSS.mp4
    assert re.fullmatch(r"ML-01-NORTH_Incident_120_5m_\d{4}-\d{2}-\d{2}_\d{6}\.mp4", clip["name"]), clip["name"]
    # "surrounding" the event: ~3 s before + ~2 s after the trigger
    trig = clip["triggerOffsetSec"]
    assert 2.7 <= trig <= 3.3, f"trigger offset {trig}"
    fps = clip["fps"]
    idx = int(round(trig * fps))
    assert _is_red_at_box(_frame(path, idx)), "trigger frame must show the deformation"
    assert not _is_red_at_box(_frame(path, 0)), "first frame is pre-event footage"
    assert _is_red_at_box(_frame(path, 149)), "post-roll still shows the defect"
    evidence["TC-1.4"] = {"frames": frames, "codec": codec, "duration": clip["durationSec"],
                          "name": clip["name"], "trigger_offset_s": trig,
                          "write_time_ms_excl_postroll": live_incident["clip_write_ms"]}


def test_tc_1_4_defect_coordinates_logged_to_sqlite(client, live_incident):
    clip = live_incident["clip"]
    rows = client.get(f"/api/videos/{clip['id']}/defects").json()["defects"]
    at_trigger = [r for r in rows if abs(r["timestamp_sec"] - clip["triggerOffsetSec"]) < 1e-3]
    deform = next(r for r in at_trigger if r["defect_type"] == "DEFORMATION")
    x, y, w, h = GT["deformation"]["box"]
    assert deform["x"] == pytest.approx((x + w / 2) / 960 * 100, abs=0.2)
    assert deform["y"] == pytest.approx((y + h / 2) / 540 * 100, abs=0.2)
    assert deform["width"] == pytest.approx(w / 960 * 100, abs=0.2)
    # the clip's own analysis record: logged at >= 0.40, triggered at >= 0.60
    d = client.get(f"/api/videos/{clip['id']}/defects").json()["run"]
    assert d["kind"] == "CLIP" and d["conf"] == pytest.approx(0.40) and d["triggerConf"] == pytest.approx(0.60)
    # one ROV sensor sample per clip frame, from the separate sensor DB
    tel = client.get(f"/api/videos/{clip['id']}/telemetry").json()
    assert len(tel) >= 140 and {round(t["depth"], 1) for t in tel} == {120.5} and {t["source"] for t in tel} == {"MAVLINK"}
    # the live session itself also logged the defect (parent of the clip)
    sess = client.get(f"/api/videos/{clip['id']}").json()["parentId"]
    assert sess == live_incident["session_id"]
    sess_rows = client.get(f"/api/videos/{sess}/defects").json()["defects"]
    assert any(r["defect_type"] == "DEFORMATION" and r["confidence"] >= 0.88 for r in sess_rows)


def test_tc_1_4_buffer_is_time_based_for_25fps_cameras(client, rov, evidence):
    """A PAL (25 fps) subsea camera: the buffer must still hold 5 s (125 frames)."""
    path = make_dive(MEDIA / "dive_25fps.mp4", fps=25)
    with open(path, "rb") as f:
        vid = client.post(f"/api/videos/upload-stream?filename=dive25.mp4&name=PAL&line_id=ML-01-NORTH&conf=0.5",
                          content=f.read()).json()["video"]["id"]
    client.post(f"/api/videos/{vid}/cancel")
    rec = run_live_until_incident(client, rov, f"video:{vid}", depth=80.0)
    clip = client.get(f"/api/videos/{rec['saved']['clipId']}").json()
    assert rec["buffer_capacity"] == 125
    assert clip["durationSec"] == pytest.approx(5.0, abs=0.12)
    evidence["TC-1.4-25fps"] = {"buffer_capacity": rec["buffer_capacity"], "clip_duration": clip["durationSec"]}


# ---------------------------------------------------------------- TC-1.5
def test_tc_1_5_alert_hands_off_clip_without_ingest_queue(client, live_incident):
    saved = live_incident["saved"]
    assert saved["clipId"] and saved["clipName"].endswith(".mp4")
    stages = [i["stage"] for i in live_incident["incidents"]]
    assert stages[:2] == ["triggered", "saved"]
    clip = client.get(f"/api/videos/{saved['clipId']}").json()
    assert clip["sourceType"] == "INCIDENT_CLIP" and clip["status"] == "COMPLETED" and clip["playable"]
    assert clip["streamUrl"]
    r = client.get(clip["streamUrl"], headers={"Range": "bytes=0-1023"})
    assert r.status_code == 206


# ---------------------------------------------------------------- TC-1.6
def test_tc_1_6_live_slider_is_the_clip_trigger(client, rov, processed_dive, evidence):
    """At trigger 0.95 the 88 % deformation is shown (dashed) and logged but NOT clipped;
    moving the slider to 0.60 while it is still in view triggers the clip, and both
    settings are recorded in the dive's threshold log."""
    import main
    seq0 = main.live.incident_seq
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "trigger_conf": 0.95, "detection_mode": "ARMED"})
    try:
        def deform_visible():
            dets = client.get("/api/live/state").json().get("detections") or []
            return next((d for d in dets if d["type"] == "DEFORMATION"), None)
        d = wait_for(deform_visible, 25, 0.05, "deformation on screen")
        assert d["triggering"] is False and d["confidence"] == pytest.approx(0.88, abs=1e-3)
        time.sleep(0.5)
        assert main.live.incident_seq == seq0, "no clip below the trigger threshold"
        t_change = time.time()
        client.post("/api/live/config", json={"trigger_conf": 0.60})
        wait_for(lambda: main.live.incident_seq > seq0, 3, 0.05, "incident after lowering the trigger")
        latency = time.time() - t_change
        trig = next(e for e in main.live.incidents if e["seq"] > seq0 and e["stage"] == "triggered")
        assert trig["triggerConf"] == pytest.approx(0.60)
        log = client.get("/api/live/state").json()["thresholdLog"]
        assert [c for _, c in log] == [0.95, 0.60]
        wait_for(lambda: any(e["seq"] > seq0 and e["stage"] == "saved" for e in main.live.incidents), 15, 0.2, "clip saved")
        sess = main.live.session_id
        main.live.flush_db()
    finally:
        client.post("/api/live/stop")
    run = client.get(f"/api/videos/{sess}/runs").json()["runs"][0]
    assert run["kind"] == "LIVE" and [c for _, c in run["thresholdLog"]] == [0.95, 0.60]
    evidence["TC-1.6"] = {"below_trigger_clipped": False, "trigger_after_change_s": round(latency, 2), "threshold_log": run["thresholdLog"]}


def test_tc_1_6_trigger_default_is_0_60():
    """Backend default and the UI default (Live control board) agree on 0.60."""
    from config import LIVE_TRIGGER_DEFAULT
    from conftest import ROOT
    assert LIVE_TRIGGER_DEFAULT == 0.60
    assert "LIVE_TRIGGER_DEFAULT = 0.6;" in (ROOT / "frontend/src/components/Panels.tsx").read_text()


# ---------------------------------------------------------------- TC-1.7
def test_tc_1_7_detection_modes(client, rov, processed_dive, evidence):
    import main
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "detection_mode": "STANDBY"})
    try:
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        time.sleep(0.5)
        n0 = client.get("/api/live/state").json()["framesInferred"]
        time.sleep(1.0)
        st = client.get("/api/live/state").json()
        assert st["framesInferred"] == n0 and st["armed"] is False and st["captureFps"] > 25, "STANDBY: video only"
        client.post("/api/live/detection-mode", json={"mode": "AUTO"})
        set_depth(client, rov, 1.0)                       # on the surface / deck
        time.sleep(0.6)
        st = client.get("/api/live/state").json()
        assert st["armed"] is False and st["armWait"] == "SHALLOW", "AUTO says why it waits"
        set_depth(client, rov, 30.0)                      # submerged
        wait_for(lambda: client.get("/api/live/state").json()["armed"], 3, 0.1, "AUTO arms below 2 m")
        n1 = client.get("/api/live/state").json()["framesInferred"]
        time.sleep(0.8)
        assert client.get("/api/live/state").json()["framesInferred"] > n1
        rov.silent = True                                  # telemetry lost -> AUTO disarms (never guesses)
        wait_for(lambda: not client.get("/api/live/state").json()["armed"], 6, 0.2, "AUTO disarms on stale link")
        assert client.get("/api/live/state").json()["armWait"] == "NO_DEPTH"
        rov.silent = False
        assert client.post("/api/live/detection-mode", json={"mode": "BOGUS"}).status_code == 400
        evidence["TC-1.7"] = {"standby_inference": 0, "auto_surface_armed": False, "auto_submerged_armed": True,
                              "auto_stale_armed": False}
    finally:
        rov.silent = False
        client.post("/api/live/detection-mode", json={"mode": "ARMED"})
        client.post("/api/live/stop")
        set_depth(client, rov, 120.5)


# ---------------------------------------------------------------- TC-1.8
def test_tc_1_8_dive_log_sensor_db_and_tlog(client, rov, processed_dive, evidence):
    import main, sensors_db, tlog_import
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "detection_mode": "STANDBY", "recording": True})
    try:
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        sess = main.live.session_id
        time.sleep(3.0)
        st = client.get("/api/rov/status").json()
        tlog = st["tlog"]
        assert st["recording"] and tlog
        client.post("/api/live/recording", json={"enabled": False})
        n_off = sensors_db.count_for(sess)
        time.sleep(1.5)
        assert sensors_db.count_for(sess) == n_off, "no samples while the dive log is stopped"
    finally:
        client.post("/api/live/stop")
        client.post("/api/live/detection-mode", json={"mode": "ARMED"})
    rows = client.get(f"/api/videos/{sess}/telemetry").json()
    rate = len(rows) / (rows[-1]["t"] - rows[0]["t"])
    assert 4.0 <= rate <= 6.0, f"sensor log at {rate:.1f} Hz"
    assert all(abs(r["depth"] - 120.5) < 0.05 for r in rows)
    from config import TLOG_DIR
    replay = tlog_import.read_tlog(str(TLOG_DIR / tlog))
    assert len(replay) >= 10 and abs(replay[-1]["depth"] - 120.5) < 0.05, "the .tlog replays the same readings"
    import sqlite3
    from config import DB_PATH, SENSOR_DB_PATH
    assert SENSOR_DB_PATH != DB_PATH
    assert "sensor_samples" in {r[0] for r in sqlite3.connect(SENSOR_DB_PATH).execute("select name from sqlite_master")}
    evidence["TC-1.8"] = {"samples": len(rows), "rate_hz": round(rate, 2), "tlog_messages": len(replay),
                          "sensor_db": SENSOR_DB_PATH.name}


# ---------------------------------------------------------------- NFR (Pipeline A)
def test_nfr_1_no_capture_stall_while_clip_is_written(live_incident, evidence):
    """Disk I/O must not stall the live loop. Measured on timestamped capture intervals.
    Acceptance, inside the clip-write window (trigger -> clip saved: post-roll + encode +
    DB insert): no gap reaches 2 frame periods (no frame slot lost), and the number of late
    frames (gap > 1.5 periods) is no worse than an equal-length window before the trigger
    (+1 frame tolerance)."""
    rec = live_incident
    tt, ts, nom = rec["t_trigger"], rec["t_saved"], rec["nominal_s"]
    win = ts - tt
    in_write = [dt for (t, dt) in rec["frame_log"] if tt <= t <= ts]
    in_base = [dt for (t, dt) in rec["frame_log"] if tt - win - 0.2 <= t < tt - 0.2]
    late = lambda xs: sum(1 for d in xs if d > 1.5 * nom)
    max_write = max(in_write) * 1000
    assert len(in_write) > 0.9 * win / nom, "capture kept running through the write"
    assert max_write < 2 * nom * 1000, f"capture stalled {max_write:.1f} ms during the clip write"
    assert late(in_write) <= late(in_base) + 1, f"write window: {late(in_write)} late frames vs baseline {late(in_base)}"
    evidence["NFR-1"] = {"write_window_s": round(win, 2), "frames_in_window": len(in_write),
                         "late_frames_during_write": late(in_write), "late_frames_baseline": late(in_base),
                         "max_gap_during_write_ms": round(max_write, 1), "nominal_ms": round(nom * 1000, 1),
                         "clip_write_ms": rec["clip_write_ms"],
                         "session_max_gap_ms": rec["capture_health_after"].get("maxMs")}


def test_nfr_1b_slow_inference_never_slows_capture(client, processed_dive, evidence, monkeypatch):
    """Latest-frame-wins: with inference 3x slower than the frame period, capture and
    display still run at camera rate; YOLO just analyses fewer frames."""
    import main
    monkeypatch.setenv("FAKE_YOLO_LATENCY_MS", "100")
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "detection_mode": "ARMED"})
    try:
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        time.sleep(4)
        s = client.get("/api/live/state").json()
        h = main.live.capture_health()
        assert s["captureFps"] > 27, s["captureFps"]
        assert s["inferenceFps"] < 12
        assert s["framesSkipped"] > 0
        evidence["NFR-1b"] = {"capture_fps": s["captureFps"], "inference_fps": s["inferenceFps"],
                              "frames_skipped": s["framesSkipped"], "p99_ms": h["p99Ms"]}
    finally:
        client.post("/api/live/stop")


def test_nfr_4_ram_buffer_footprint(live_incident, evidence):
    mb = live_incident["buffer_bytes"] / 2 ** 20
    assert live_incident["buffer_len"] == 150
    assert mb < 50, f"buffer uses {mb:.1f} MB"
    evidence["NFR-4"] = {"buffer_frames": live_incident["buffer_len"], "buffer_mb": round(mb, 2),
                         "raw_bgr_equivalent_mb": round(150 * 960 * 540 * 3 / 2 ** 20, 1)}


def test_tc_1_8_no_repeated_depth_when_messages_stop(client, rov, processed_dive, evidence):
    """Tether cut: the dive log stops writing rows from the first missed depth message - a gap, never the last
    good depth repeated. Before 3.6.11 rows repeated it until the link turned STALE (3 s)."""
    import main
    set_depth(client, rov, 120.5)
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "detection_mode": "STANDBY", "recording": True})
    try:
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        sess = main.live.session_id
        time.sleep(1.5)
        rov.silent = True
        t_cut = time.monotonic() - main.live._session_t0
        time.sleep(2.0)                                     # shorter than ROV_STALE_SEC: not yet STALE
        state_in_gap = client.get("/api/rov/status").json()["state"]
        rov.silent = False
        t_back = time.monotonic() - main.live._session_t0
        time.sleep(1.5)
    finally:
        rov.silent = False
        client.post("/api/live/stop")
        client.post("/api/live/detection-mode", json={"mode": "ARMED"})
    assert state_in_gap == "CONNECTED", "the cut must end before the link turns STALE"
    rows = client.get(f"/api/videos/{sess}/telemetry").json()
    in_gap = [r["t"] for r in rows if t_cut + 0.3 < r["t"] < t_back]
    assert not in_gap, f"{len(in_gap)} rows repeated the last depth during the cut"
    assert any(r["t"] < t_cut for r in rows) and any(r["t"] > t_back for r in rows), "rows before and after the cut"
    evidence["TC-1.8-cut"] = {"cut_s": round(t_back - t_cut, 2), "rows_in_cut": len(in_gap), "rows": len(rows)}


# ---------------------------------------------------------------- TC-1.9 (3.6.3)
def test_tc_1_9_clip_trigger_must_hold(client, rov, evidence):
    """A deformation above the trigger for one or two frames (a flicker, e.g. a normal link edge-on for an instant)
    does not save a clip; one that holds on 3 of the last 5 inferred frames does. The clip is centred on the
    first frame of the run, and the event says how it held."""
    import main
    path = MEDIA / "flicker_then_hold.mp4"
    if not path.exists():
        red = "drawbox=x=480:y=220:w=80:h=80:color=0xE60000@1:t=fill"
        vf = (f"noise=alls=10:allf=t,{red}:enable='between(n,60,61)',{red}:enable='between(n,120,121)',"
              f"{red}:enable='between(t,5.0,6.5)'")
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x0a1e3c:s=960x540:r=30:d=8",
                        "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(path)], check=True)
    seq0 = main.live.incident_seq
    client.post("/api/live/start", json={"source": str(path), "line_id": "ML-01-NORTH", "trigger_conf": 0.60,
                                         "detection_mode": "ARMED"})
    try:
        st = client.get("/api/live/state").json()
        assert st["triggerHold"]["hits"] == 3 and st["triggerHold"]["window"] == 5
        # the two flickers (2 frames each at t = 2.0 s and 4.0 s) come and go without a clip
        wait_for(lambda: main.live._latest is not None and main.live._latest.t >= 4.5, 15, 0.05, "past the flickers")
        assert main.live.incident_seq == seq0, "a one- or two-frame flicker must not save a clip"
        flickers = main.live.trigger_flickers
        # the held deformation (5.0 - 6.5 s) saves one
        wait_for(lambda: main.live.incident_seq > seq0, 10, 0.05, "incident for the held deformation")
        trig = next(e for e in main.live.incidents if e["seq"] > seq0 and e["stage"] == "triggered")
        assert trig["held"]["hits"] >= 3 and trig["held"]["window"] == 5
        assert 4.9 <= trig["sessionTime"] <= 5.6, "clip centred on the first frame of the run"
    finally:
        client.post("/api/live/stop")
    assert flickers >= 1, "the flickers were seen and ignored"
    evidence["TC-1.9"] = {"flickers_ignored": flickers, "held": trig["held"], "first_hit_t": trig["sessionTime"]}
