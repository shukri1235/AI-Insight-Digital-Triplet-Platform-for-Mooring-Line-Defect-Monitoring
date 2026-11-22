"""
SCENARIO 3 - Data Ingestion & Queue Management (Batch Ingest)

TC-3.1  Ingest: threshold is mandatory before processing; SHA-256 de-duplication (+ pre-upload
        fingerprint); response before processing starts; in-place import from any folder on
        the PC (no copy, background hashing) with duplicate / offline-media handling
TC-3.1b A dive's BlueROV2 .tlog is attached and aligned to the video timeline
TC-3.2  Headless processing: QUEUED -> PROCESSING -> COMPLETED with progress/ETA, results
        match ground truth at the chosen threshold; the GPU arbiter keeps live first
TC-3.3  Transition: completed dive is reviewable (markers + range streaming)
TC-3.4  Robustness: a crash mid-job resumes from the last checkpoint
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import json
from pathlib import Path

import pytest

from conftest import BACKEND, FAKES, GT, MEDIA, make_dive, upload, wait_completed, wait_for
from vv_media import make_tlog


# ---------------------------------------------------------------- TC-3.1
def test_tc_3_1_upload_returns_before_processing_and_dedupes(client, scenario_dive, processed_dive, evidence):
    from video_utils import quick_fingerprint
    # the threshold must be chosen before anything is accepted
    r = client.post(f"/api/videos/upload-stream?filename=x.mp4&name=x", content=scenario_dive.read_bytes())
    assert r.status_code == 400 and "threshold" in r.json()["detail"]
    # re-uploading identical bytes is rejected by the full SHA-256
    res = upload(client, scenario_dive, "same file again")
    assert res["duplicate"] is True and res["video"]["id"] == processed_dive["id"]
    # the browser's fingerprint pre-check finds it BEFORE any upload
    fp = quick_fingerprint(scenario_dive)
    match = client.get(f"/api/videos/precheck?fingerprint={fp}").json()["match"]
    assert match and match["id"] == processed_dive["id"]
    # a new file is accepted and returned as QUEUED - processing has not happened yet
    fresh = make_dive(MEDIA / "fresh.mp4", seconds=12)
    t1 = time.perf_counter()
    res = upload(client, fresh, "fresh dive")
    accept_s = time.perf_counter() - t1
    assert res["duplicate"] is False and res["video"]["status"] == "QUEUED"
    assert res["video"]["framesAnalyzed"] == 0 and res["video"]["pendingConf"] == 0.25
    wait_completed(client, res["video"]["id"])
    evidence["TC-3.1"] = {"duplicate_rejected": True, "precheck_match": True, "accept_time_s": round(accept_s, 3)}


def test_tc_3_1_streaming_upload_hashes_in_one_pass(client, evidence):
    """Upload from another device: the body is written and SHA-256 hashed in one pass."""
    import hashlib, main
    big = make_dive(MEDIA / "big_upload.mp4", seconds=60, size=(1920, 1080))
    data = big.read_bytes()
    t0 = time.perf_counter()
    r1 = client.post(f"/api/videos/upload-stream?filename=big.mp4&name=big&conf=0.5", content=data)
    t_stream = time.perf_counter() - t0
    vid = r1.json()["video"]["id"]
    client.post(f"/api/videos/{vid}/cancel")
    stored = main.SessionLocal().get(main.VideoRecord, vid)
    assert stored.file_hash == hashlib.sha256(data).hexdigest() and stored.is_managed
    assert Path(stored.current_filepath).parent == main.MEDIA_DIR
    evidence["TC-3.1-stream"] = {"file_mb": round(len(data) / 2 ** 20, 1), "stream_upload_s": round(t_stream, 3),
                                 "mb_per_s": round(len(data) / 2 ** 20 / t_stream, 1)}
    wait_for(lambda: client.delete(f"/api/videos/{vid}").status_code in (200, 404), 10)
    assert not Path(stored.current_filepath).exists(), "managed copies are removed with their record"


def test_tc_3_1_in_place_import_no_copy(client, evidence):
    """Files on the PC (e.g. an external drive) are registered where they are: nothing is
    copied, hashing runs in the background, the job starts once the hash is known."""
    import main
    folder = MEDIA / "external_drive" / "2026-09 campaign"
    folder.mkdir(parents=True, exist_ok=True)
    src = make_dive(folder / "ML01_last_month.mp4", seconds=10)
    listing = client.get("/api/fs/list", params={"path": str(folder), "kind": "video"}).json()
    assert [f["name"] for f in listing["files"]] == ["ML01_last_month.mp4"]
    before = set(os.listdir(main.MEDIA_DIR))
    assert client.post("/api/videos/import", json={"path": str(src)}).status_code == 400, "threshold required"
    r = client.post("/api/videos/import", json={"path": str(src), "name": "ML-01 last month", "line_id": "ML-01-NORTH", "conf": 0.6})
    assert r.status_code == 200, r.text
    v = r.json()["video"]
    assert v["sourceType"] == "LOCAL_IMPORT" and not v["isManaged"] and v["path"] == str(src)
    assert v["status"] in ("HASHING", "QUEUED", "PROCESSING")
    done = wait_completed(client, v["id"])
    assert set(os.listdir(main.MEDIA_DIR)) - before <= {"incidents", "tlogs", "reports"}, "no copy of the video was made"
    assert done["activeRun"]["conf"] == 0.6 and done["activeRun"]["kind"] == "BATCH"
    # same bytes again (any path) -> duplicate
    dup = client.post("/api/videos/import", json={"path": str(src), "conf": 0.6})
    assert dup.status_code == 409 and dup.json()["video"]["id"] == v["id"]
    # moving the drive -> media offline, record + results kept
    moved = src.with_suffix(".moved")
    src.rename(moved)
    try:
        off = client.get(f"/api/videos/{v['id']}").json()
        assert off["mediaOffline"] and off["defectCount"] == done["defectCount"]
    finally:
        moved.rename(src)
    # removing the record never deletes the operator's file
    assert client.delete(f"/api/videos/{v['id']}").status_code == 200
    assert src.exists()
    evidence["TC-3.1-inplace"] = {"copied": False, "threshold": 0.6, "duplicate_status": 409, "offline_flag": True,
                                  "file_kept_after_delete": True}


def test_tc_3_1_file_browser_is_local_only(app_module):
    from types import SimpleNamespace
    from fastapi import HTTPException
    remote = SimpleNamespace(client=SimpleNamespace(host="192.168.1.50"))
    with pytest.raises(HTTPException) as e:
        app_module._local_only(remote)
    assert e.value.status_code == 403
    app_module._local_only(SimpleNamespace(client=SimpleNamespace(host="127.0.0.1")))


# ---------------------------------------------------------------- TC-3.1b
def test_tc_3_1b_tlog_attached_and_aligned(client, processed_dive, evidence):
    """A dive recorded without this app: its BlueOS/QGC .tlog gives depth on the video timeline."""
    vid = processed_dive["id"]
    assert client.get(f"/api/videos/{vid}/telemetry").json() == [], "no telemetry until a log is attached"
    tl = make_tlog(MEDIA / "dive.tlog", 30, lambda t: 140.0 + t)        # descending 1 m/s from 140 m
    data = tl.read_bytes()
    r = client.post(f"/api/videos/{vid}/tlog-upload?offset_sec=5", content=data)   # video starts 5 s into the log
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["alignment"] == "manual offset" and res["offsetSec"] == 5
    rows = client.get(f"/api/videos/{vid}/telemetry").json()
    assert {x["source"] for x in rows} == {"TLOG"}
    at = lambda t: min(rows, key=lambda x: abs(x["t"] - t))
    assert at(0)["depth"] == pytest.approx(145.0, abs=0.15) and at(10)["depth"] == pytest.approx(155.0, abs=0.15)
    assert 4.5 <= len(rows) / 20 <= 5.5, "resampled to 5 Hz over the 20 s video"
    # the dive's events now carry depth + segment for the report (deformation at 8 s -> 153 m Touchdown)
    rep = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": vid, "use_ai": False}).json()
    deform = next(f for f in rep["content"]["findings"] if f["defect_type"] == "DEFORMATION")
    assert deform["depth_m"] == pytest.approx(153.0, abs=0.3) and deform["segment"] == "Touchdown Zone"
    assert client.post(f"/api/videos/{vid}/tlog-upload", content=b"not a tlog").status_code == 400
    evidence["TC-3.1b"] = {"samples": len(rows), "alignment": res["alignment"], "depth_at_0s": at(0)["depth"],
                           "deformation_depth": deform["depth_m"], "segment": deform["segment"]}


# ---------------------------------------------------------------- TC-3.2
def test_tc_3_2_state_machine_progress_and_eta(client, evidence, monkeypatch):
    monkeypatch.setenv("FAKE_YOLO_LATENCY_MS", "40")
    src = make_dive(MEDIA / "medium_dive.mp4", seconds=90)
    vid = upload(client, src, "medium")["video"]["id"]
    seen, progress, etas = [], [], []
    def poll():
        v = client.get(f"/api/videos/{vid}").json()
        if not seen or seen[-1] != v["status"]:
            seen.append(v["status"])
        if v["status"] == "PROCESSING":
            progress.append(v["progress"])
            if v["etaSec"] is not None:
                etas.append(v["etaSec"])
        return v["status"] == "COMPLETED"
    wait_for(poll, 120, 0.25, "medium dive completed")
    assert seen[0] in ("QUEUED", "PROCESSING") and seen[-1] == "COMPLETED" and "PROCESSING" in seen
    assert progress == sorted(progress), "progress must be monotonic"
    assert etas and etas[-1] < etas[0], "ETA must count down"
    evidence["TC-3.2-state"] = {"states": seen, "progress_samples": len(progress), "eta_first_last": [etas[0], etas[-1]]}


def test_tc_3_2_results_match_ground_truth(client, processed_dive, evidence):
    rows = client.get(f"/api/videos/{processed_dive['id']}/defects").json()["defects"]
    by = {}
    for r in rows:
        by.setdefault(r["raw_class"], []).append(r["timestamp_sec"])
    for raw, gt_name in (("deformation", "deformation"), ("pitting", "pitting"), ("corrosion", "corrosion")):
        t0, t1 = GT[gt_name]["t"]
        ts = by[raw]
        assert all(t0 - 1e-6 <= t < t1 for t in ts), f"{raw} logged outside its ground-truth window: {ts}"
        expected = int((t1 - t0) * 2)   # sampled at 2 fps
        assert len(ts) == expected, f"{raw}: {len(ts)} detections, expected {expected}"
    assert "mooringline" not in by, "non-defect classes must never be logged as defects"
    run = client.get(f"/api/videos/{processed_dive['id']}").json()["activeRun"]
    assert run["kind"] == "BATCH" and run["conf"] == 0.25 and run["framesAnalyzed"] == 40   # 20 s x 2 fps
    evidence["TC-3.2-accuracy"] = {k: len(v) for k, v in by.items()}


def test_tc_3_2_threshold_is_applied_and_recorded(client, processed_dive, evidence):
    """The same dive analysed at 0.60: pitting (32 %) is not recorded; both analyses kept."""
    vid = processed_dive["id"]
    rid = client.post(f"/api/videos/{vid}/analyse", json={"conf": 0.6}).json()["runId"]
    wait_for(lambda: client.get(f"/api/videos/{vid}").json()["activeRun"]["id"] == rid, 60, 0.3, "0.60 analysis")
    raw = {r["raw_class"] for r in client.get(f"/api/videos/{vid}/defects").json()["defects"]}
    assert raw == {"deformation", "corrosion"}
    runs = client.get(f"/api/videos/{vid}/runs").json()["runs"]
    first = next(r for r in runs if r["kind"] == "BATCH")
    client.post(f"/api/runs/{first['id']}/activate")       # restore for the other tests
    assert {r["raw_class"] for r in client.get(f"/api/videos/{vid}/defects").json()["defects"]} >= {"pitting"}
    evidence["TC-3.2-threshold"] = {"at_0.60": sorted(raw), "runs": [(r["kind"], r["conf"], r["defectCount"]) for r in runs]}


def test_nfr_2_live_keeps_priority_while_batch_runs(client, processed_dive, evidence, monkeypatch):
    import main
    monkeypatch.setenv("FAKE_YOLO_LATENCY_MS", "20")
    src = make_dive(MEDIA / "concurrent_dive.mp4", seconds=60)
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "detection_mode": "ARMED"})
    try:
        # The file is registered first (in production the upload arrives from another
        # process - the browser); the measured window is pure batch processing.
        vid = upload(client, src, "concurrent")["video"]["id"]
        client.post(f"/api/videos/{vid}/cancel")
        wait_for(lambda: client.get(f"/api/videos/{vid}").json()["status"] == "UNPROCESSED", 30)
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        time.sleep(1.5)
        main.live.frame_intervals.clear()
        late0 = main.live.late_frames
        inf_before = client.get("/api/live/state").json()["inferenceFps"]
        main.arbiter.reset_stats()
        client.post(f"/api/videos/{vid}/process", json={"conf": 0.25})
        fps_samples, inf_samples = [], []
        def poll():
            st = client.get("/api/live/state").json()
            fps_samples.append(st["captureFps"]); inf_samples.append(st["inferenceFps"])
            return client.get(f"/api/videos/{vid}").json()["status"] == "COMPLETED"
        wait_for(poll, 180, 0.5, "batch under live load")
        arb = main.arbiter.stats()
        late = main.live.late_frames - late0
        h = main.live.capture_health()
        assert min(fps_samples[2:]) > 25, fps_samples
        assert h["p99Ms"] < 1.5 * h["nominalMs"]
        assert h["maxMs"] < 2 * h["nominalMs"], f"capture stalled {h['maxMs']} ms while batch ran"
        mid = sorted(inf_samples[2:-2] or inf_samples)[len(inf_samples[2:-2] or inf_samples) // 2]
        assert mid >= 0.6 * inf_before, f"live inference {mid} fps during batch vs {inf_before} before"
        assert arb["liveWaitMsP95"] is not None and arb["liveWaitMsP95"] < 120, arb
        evidence["NFR-2"] = {"live_capture_fps_min": min(fps_samples[2:]), "late_frames": late,
                             "live_inference_fps_before": inf_before, "live_inference_fps_during_median": mid,
                             "arbiter": arb, "batch_job": main.batch.last_job, **h}
    finally:
        client.post("/api/live/stop")


def test_nfr_3_sqlite_wal_and_no_lock_errors(app_module, capsys):
    from sqlalchemy import text
    import sensors_db
    with app_module.SessionLocal() as s:
        assert s.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert s.execute(text("PRAGMA busy_timeout")).scalar() >= 5000
    with sensors_db.engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
    assert "database is locked" not in capsys.readouterr().out


# ---------------------------------------------------------------- TC-3.3
def test_tc_3_3_completed_dive_is_reviewable(client, processed_dive):
    v = client.get(f"/api/videos/{processed_dive['id']}").json()
    assert v["status"] == "COMPLETED" and v["playable"] and v["defectCount"] > 0
    r = client.get(v["streamUrl"], headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and len(r.content) == 100
    assert client.get(v["streamUrl"], headers={"Range": "bytes=-10"}).status_code == 206


# ---------------------------------------------------------------- NFR-5: 4-hour scale
def test_nfr_5_review_queries_at_4_hour_scale(client, app_module, evidence):
    """A 4 h dive: 72 000 sensor samples (5 Hz) and a defect-rich analysis with 20 000
    detections. Review / registry queries must stay interactive."""
    import uuid, random
    import sensors_db
    from database import AnalysisRun, DefectRecord, VideoRecord
    vid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    with app_module.SessionLocal() as s:
        s.add(VideoRecord(id=vid, file_hash=vid, original_filename="4h.mp4", display_name="4h synthetic",
                          status="COMPLETED", source_type="LOCAL_UPLOAD", line_id="ML-02-WEST", duration_sec=14400,
                          active_run_id=rid))
        s.add(AnalysisRun(id=rid, video_id=vid, kind="BATCH", conf=0.25, status="COMPLETED", frames_analyzed=28800))
        s.flush()
        rnd = random.Random(1)
        s.bulk_save_objects([DefectRecord(video_id=vid, run_id=rid, defect_type=rnd.choice(["CORROSION", "DEFORMATION"]),
                                          raw_class="x", timestamp_sec=rnd.randrange(28800) / 2,
                                          confidence=round(rnd.uniform(0.25, 0.95), 2), severity="High confidence",
                                          x=50, y=50, width=5, height=5) for _ in range(20000)])
        s.commit()
    sensors_db.insert_samples([{"media_id": vid, "t": i / 5, "source": "TLOG", "depth": 50 + (i % 4500) / 5,
                                "heading": 90.0, "temperature": 8.0} for i in range(72000)])
    timings = {}
    for name, url in (("history", "/api/history"), ("defects", f"/api/videos/{vid}/defects"),
                      ("telemetry", f"/api/videos/{vid}/telemetry"), ("videos", "/api/videos")):
        t0 = time.perf_counter()
        assert client.get(url).status_code == 200
        timings[name] = round((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    thin = client.get(f"/api/videos/{vid}/telemetry?max_hz=1").json()
    timings["telemetry_1hz"] = round((time.perf_counter() - t0) * 1000)
    assert len(thin) == 14400
    t0 = time.perf_counter()
    rep = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": vid, "use_ai": False})
    timings["report_facts"] = round((time.perf_counter() - t0) * 1000)
    assert rep.status_code == 200, rep.text
    evidence["NFR-5"] = {"sensor_rows": 72000, "defect_rows": 20000, "ms": timings}
    assert timings["history"] < 1500 and timings["defects"] < 3000 and timings["telemetry"] < 4000, timings
    client.delete(f"/api/videos/{vid}")
    assert sensors_db.count_for(vid) == 0, "sensor rows are removed with the media record"


# ---------------------------------------------------------------- TC-3.4 (separate process)
def _http(url, data=None, method=None, headers=None, timeout=10):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


@pytest.mark.slow
def test_tc_3_4_crash_mid_job_resumes_from_checkpoint(evidence):
    """Start the real server, kill -9 it mid-way through a dive, restart: the job must
    resume from the last committed timestamp, finish, and contain no duplicate rows."""
    tmp = Path(tempfile.mkdtemp(prefix="vv_crash_"))
    (tmp / "models").mkdir()
    (tmp / "models" / "best.pt").write_bytes(b"x")
    port = 18601
    env = {**os.environ, "FYP_MEDIA_DIR": str(tmp / "media"), "FYP_MAVLINK_URL": "udpin:127.0.0.1:18602",
           "FYP_MODELS_DIR": str(tmp / "models"), "FYP_DB_PATH": str(tmp / "db.sqlite"), "FYP_PORT": str(port),
           "FAKE_YOLO_LATENCY_MS": "60", "PYTHONUNBUFFERED": "1",
           "PYTHONPATH": os.pathsep.join([str(FAKES), str(BACKEND), os.environ.get("PYTHONPATH", "")])}
    src = make_dive(MEDIA / "crash_dive.mp4", seconds=120)
    api = f"http://127.0.0.1:{port}"

    def boot(log):
        p = subprocess.Popen([sys.executable, str(BACKEND / "main.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
        wait_for(lambda: _try(lambda: _http(f"{api}/api/model/status")["is_loaded"]), 20, 0.3, "server boot")
        return p
    log1 = open(tmp / "run1.log", "w")
    p = boot(log1)
    try:
        res = _http(f"{api}/api/videos/upload-stream?filename=crash.mp4&name=crash&conf=0.25", data=src.read_bytes(),
                    method="POST", headers={"Content-Type": "application/octet-stream"}, timeout=60)
        vid = res["video"]["id"]
        t_begin = time.monotonic()
        wait_for(lambda: _http(f"{api}/api/videos/{vid}")["progress"] >= 35, 120, 0.3, "35 % progress")
        before = _http(f"{api}/api/videos/{vid}")
        p.kill(); p.wait()                       # power cut
        t_down = time.monotonic()
        log2 = open(tmp / "run2.log", "w")
        p = boot(log2)
        downtime = time.monotonic() - t_down
        done = wait_for(lambda: (lambda v: v if v["status"] == "COMPLETED" else None)(_http(f"{api}/api/videos/{vid}")),
                        180, 0.5, "resumed job completes")
        rows = _http(f"{api}/api/videos/{vid}/defects")["defects"]
        keys = [(r["defect_type"], r["raw_class"], round(r["timestamp_sec"], 3)) for r in rows]
        run = done["activeRun"]
        wall = time.monotonic() - t_begin
        log2.flush()
        resumed_log = (tmp / "run2.log").read_text()
        assert "resuming from checkpoint" in resumed_log, resumed_log[-2500:]
        assert len(keys) == len(set(keys)), "duplicate detections after resume"
        assert run["framesAnalyzed"] == 240, f"expected 240 frames (120 s x 2 fps), got {run['framesAnalyzed']}"
        assert sum(1 for k in keys if k[1] == "deformation") == 6, "ground-truth deformation 8-11 s at 2 fps"
        # processing time adds up across the restart, without the time the app was down
        tm = run["timing"]
        assert tm["resumes"] == 1 and tm["frames"] == 240 and tm["processingSec"] > 0
        assert tm["processingSec"] <= wall - downtime + 1.0, (tm["processingSec"], wall, downtime)
        evidence["TC-3.4"] = {"killed_at_progress": before["progress"], "frames": run["framesAnalyzed"],
                              "detections": len(keys), "duplicates": 0, "final_status": done["status"],
                              "processing_s": tm["processingSec"], "wall_s": round(wall, 1), "downtime_s": round(downtime, 1)}
    finally:
        p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _try(fn):
    try:
        return fn()
    except Exception:
        return None


# ---------------------------------------------------------------- NFR-6: throughput
@pytest.mark.slow
def test_nfr_6_batch_throughput_and_4h_estimate(client, evidence, monkeypatch):
    """Decode path throughput on a 1080p30 H.264 dive with inference cost removed, i.e.
    the ceiling set by the frame reader. Compares the ffmpeg and OpenCV readers."""
    import batch_worker, main
    monkeypatch.setenv("FAKE_YOLO_LATENCY_MS", "0")
    src = make_dive(MEDIA / "throughput_1080p.mp4", seconds=180, size=(1920, 1080))
    results = {}
    for reader, flag in (("ffmpeg", True), ("opencv", False)):
        monkeypatch.setattr(batch_worker, "BATCH_USE_FFMPEG_READER", flag)
        vid = upload(client, src, f"tp-{reader}")["video"]["id"] if reader == "ffmpeg" else vid
        if reader == "opencv":
            client.post(f"/api/videos/{vid}/process", json={"conf": 0.25})
        wait_completed(client, vid, 600)
        job = main.batch.last_job
        results[reader] = {**job, "est_4h_minutes": round(4 * 3600 / job["speedX"] / 60, 1)}
    evidence["NFR-6"] = {"cpu_cores": os.cpu_count(), **results}
    assert results["ffmpeg"]["speedX"] > 1 and results["opencv"]["speedX"] > 1
