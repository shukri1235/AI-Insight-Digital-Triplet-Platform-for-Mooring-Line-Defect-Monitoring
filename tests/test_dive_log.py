"""
LOG-1  A live dive's log is available while it runs and after it ends: sensor series, vehicle events, sightings with
       depth and zone and whether they reached the trigger, incident clips on the dive's timeline, operator changes
       (trigger, detection mode, dive-log recording), summary, and its .tlog files.
LOG-2  The chart series is thinned to the requested number of points without losing the deepest / shallowest reading.
LOG-3  Downloads: every sensor reading as CSV (not thinned), sightings CSV, events CSV (vehicle + operator + clips),
       the raw .tlog; unknown dives / files are 404.
"""
import csv
import io
import time

import pytest

from conftest import set_depth, wait_for


@pytest.fixture(scope="module")
def dive(client, rov, processed_dive):
    import main
    set_depth(client, rov, 40.0)
    seq0 = main.live.incident_seq
    r = client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                             "trigger_conf": 0.60, "detection_mode": "ARMED", "recording": True})
    assert r.status_code == 200, r.text
    sid = main.live.session_id
    out = {"id": sid}
    try:
        wait_for(lambda: main.live._latest is not None and main.live._latest.t > 1.5, 15, 0.05, "dive running")
        listed = client.get("/api/dives").json()
        out["listed_live"] = next(d for d in listed if d["id"] == sid)
        out["log_live"] = client.get(f"/api/dives/{sid}/log").json()
        # operator changes during the dive
        client.post("/api/live/detection-mode", json={"mode": "AUTO"})
        client.post("/api/live/detection-mode", json={"mode": "ARMED"})
        client.post("/api/live/config", json={"trigger_conf": 0.5})
        client.post("/api/live/recording", json={"enabled": False})
        time.sleep(0.4)
        client.post("/api/live/recording", json={"enabled": True})
        rov.target = 44.0                                           # a depth excursion for the thinning check
        wait_for(lambda: any(e["seq"] > seq0 and e["stage"] == "saved" for e in main.live.incidents), 60, 0.2, "clip saved")
        # run past the whole defect window (deformation 8-11 s, pitting 8-12 s) whatever the PC's speed
        wait_for(lambda: main.live._latest.t > 15.0, 30, 0.1, "dive past the defects")
        time.sleep(0.5)
        main.live.flush_db()
    finally:
        client.post("/api/live/stop")
        set_depth(client, rov, 120.5)
    out["log"] = client.get(f"/api/dives/{sid}/log").json()
    return out


def test_log_1_dive_log_live_and_after(client, dive, evidence):
    live_row, live_log = dive["listed_live"], dive["log_live"]
    assert live_row["isLive"] and live_row["status"] == "LIVE" and live_log["dive"]["isLive"]
    assert live_log["series"] and live_log["dive"]["durationSec"] > 1.0, "readable while the dive runs"

    log = dive["log"]
    d, s = log["dive"], log["summary"]
    assert not d["isLive"] and d["status"] == "ENDED" and d["lineId"] == "ML-01-NORTH" and d["model"]
    assert log["zones"] and log["zones"][1]["name"] == "Mid-Water Zone"
    assert len(log["series"]) > 20 and {"t", "depth", "heading", "battery_v"} <= set(log["series"][0])
    # incident clip on the dive's own timeline
    assert log["clips"] and log["clips"][0]["start"] is not None and log["clips"][0]["start"] <= log["clips"][0]["trigger"] <= log["clips"][0]["end"]
    # sightings: clustered, with depth + zone at the peak, trigger at the time, linked to the clip
    deform = [x for x in log["sightings"] if x["type"] == "DEFORMATION"]
    assert deform and deform[0]["depth"] is not None and 39 <= deform[0]["depth"] <= 45 and deform[0]["zone"] == "Splash Zone"   # 0-50 m (3.6.8)
    assert any(x["aboveTrigger"] and x["clipId"] == log["clips"][0]["id"] for x in deform)
    assert any(x["type"] == "CORROSION" and not x["aboveTrigger"] for x in log["sightings"]), "pitting is logged, never a trigger"
    # operator changes, in order, with the starting values
    kinds = [(c["kind"], c["value"]) for c in log["settings"]]
    for item in [("TRIGGER", 0.6), ("MODE", "ARMED"), ("RECORDING", True), ("MODE", "AUTO"), ("TRIGGER", 0.5), ("RECORDING", False)]:
        assert item in kinds, (item, kinds)
    assert [c["t"] for c in log["settings"]] == sorted(c["t"] for c in log["settings"])
    assert s["settingsChanges"] >= 5 and s["clips"] >= 1 and s["sightings"]["DEFORMATION"] >= 1
    assert 39 <= s["maxDepth"] <= 45 and s["zoneTimeSec"].get("Splash Zone", 0) > 0
    assert s["battery"] and s["linkLosses"] == 0
    # recording off/on started a second .tlog
    assert len(log["tlogs"]) >= 2
    evidence["LOG-1"] = {"samples": s["samples"], "sightings": s["sightings"], "clips": s["clips"],
                         "settings": kinds, "tlogs": len(log["tlogs"])}


def test_log_2_thinning_keeps_the_extremes(client, dive):
    full = client.get(f"/api/dives/{dive['id']}/log?points=20000").json()["series"]
    thin = client.get(f"/api/dives/{dive['id']}/log?points=60").json()["series"]
    assert len(full) > 60 and len(thin) < len(full) and len(thin) <= 70
    d_full = [r["depth"] for r in full if r["depth"] is not None]
    d_thin = [r["depth"] for r in thin if r["depth"] is not None]
    assert max(d_thin) == max(d_full) and min(d_thin) == min(d_full)
    assert thin[0]["t"] == full[0]["t"] and thin[-1]["t"] == full[-1]["t"]


def test_log_3_downloads(client, dive):
    sid = dive["id"]
    sensors = client.get(f"/api/dives/{sid}/export/sensors.csv")
    assert sensors.status_code == 200 and "text/csv" in sensors.headers["content-type"] and "attachment" in sensors.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(sensors.text)))
    assert rows[0][:2] == ["t_sec", "depth"] and len(rows) - 1 == dive["log"]["summary"]["samples"], "every reading, not thinned"
    sight = list(csv.reader(io.StringIO(client.get(f"/api/dives/{sid}/export/sightings.csv").text)))
    assert sight[0][0] == "id" and any(r[1] == "deformation" for r in sight[1:]) and any(r[1] == "pitting" for r in sight[1:])
    ev = client.get(f"/api/dives/{sid}/export/events.csv").text
    assert "INCIDENT_CLIP" in ev and "MODE" in ev and "RECORDING" in ev and "operator" in ev
    name = dive["log"]["tlogs"][0]["name"]
    t = client.get(f"/api/dives/{sid}/tlog/{name}")
    assert t.status_code == 200 and len(t.content) > 100
    assert client.get(f"/api/dives/{sid}/tlog/..%2Fvv.db").status_code == 404
    assert client.get(f"/api/dives/{sid}/export/other.csv").status_code == 404
    assert client.get("/api/dives/not-a-dive/log").status_code == 404


def test_log_2b_thinning_a_four_hour_dive():
    """4 h at 5 Hz = 72,000 readings -> ~2,000 points; one-sample spikes up and down survive."""
    import math
    import dive_log
    rows = [{"t": i / 5, "depth": 100 + 20 * math.sin(i / 3000)} for i in range(72_000)]
    rows[12_345]["depth"], rows[50_001]["depth"] = 180.0, 3.0
    thin = dive_log._thin(rows, 2000)
    assert len(thin) <= 2001 and {180.0, 3.0} <= {r["depth"] for r in thin}
    assert thin[0] is rows[0] and thin[-1] is rows[-1] and [r["t"] for r in thin] == sorted(r["t"] for r in thin)


def test_log_4_a_dive_left_open_is_not_counted(client, dive, evidence):
    """The PC slept / Stop was pressed hours later: the dive ends at its last data, the idle time is reported apart."""
    import json as _json
    import main
    import live_stream
    from database import AnalysisRun, SessionLocal, VideoRecord
    sid = dive["id"]
    real = dive["log"]["summary"]["durationSec"]
    # (a) a dive recorded before 3.6.5: no idle time in its ops log, duration = start -> Stop pressed 9 h later
    db = SessionLocal()
    v = db.get(VideoRecord, sid)
    run = db.get(AnalysisRun, v.active_run_id)
    saved = (v.duration_sec, run.ops_log)
    v.duration_sec = real + 9 * 3600
    run.ops_log = None
    db.commit()
    try:
        log = client.get(f"/api/dives/{sid}/log").json()
        row = next(d for d in client.get("/api/dives").json() if d["id"] == sid)
    finally:
        v.duration_sec, run.ops_log = saved
        db.commit()
        db.close()
    s = log["summary"]
    assert abs(s["durationSec"] - real) < 3 and s["idleTailSec"] > 9 * 3600 - 5 and s["openSec"] == real + 9 * 3600
    assert abs(row["durationSec"] - real) < 3 and row["idleTailSec"] > 9 * 3600 - 5

    # (b) a dive now: no video frame and no sensor reading for longer than the limit -> Stop ends it at its last data
    old = live_stream.DIVE_IDLE_TAIL_SEC
    live_stream.DIVE_IDLE_TAIL_SEC = 1.0
    try:
        main.live.start("/no/such/stream.mp4", line_id="ML-01-NORTH", recording=False)
        nid = main.live.session_id
        time.sleep(2.5)
        main.live.stop()
    finally:
        live_stream.DIVE_IDLE_TAIL_SEC = old
    log2 = client.get(f"/api/dives/{nid}/log").json()
    assert log2["summary"]["durationSec"] < 1.0 and log2["summary"]["idleTailSec"] >= 2.0
    with SessionLocal() as db2:
        r = db2.get(AnalysisRun, db2.get(VideoRecord, nid).active_run_id)
        assert _json.loads(r.ops_log)["idleTailSec"] >= 2.0
    evidence["LOG-4"] = {"legacy": {"stored_s": real + 9 * 3600, "shown_s": s["durationSec"], "idle_s": s["idleTailSec"]},
                         "new": {"duration_s": log2["summary"]["durationSec"], "idle_s": log2["summary"]["idleTailSec"]}}


def test_log_5_app_pause_is_logged_with_the_dive():
    """The app's 0.1 s housekeeping tick jumping by minutes = the PC slept: logged as an event of the open dive."""
    import main
    import sensors_db
    link = main.live.rov
    mid = "pause-test-0000-0000-0000-000000000000"
    link.start_session(mid, time.monotonic())
    try:
        assert link._check_pause(1000.0, 1000.3) == 1000.3          # normal tick: nothing
        link._check_pause(1000.0, 1000.0 + 9 * 3600 + 31 * 60)     # 9 h 31 min asleep
    finally:
        link.stop_session()
    evs = [e for e in sensors_db.events_for(mid) if e["kind"] == "PAUSED"]
    assert len(evs) == 1 and "9 h 31 min" in evs[0]["text"]
