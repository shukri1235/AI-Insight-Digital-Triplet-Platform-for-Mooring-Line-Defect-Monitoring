"""
TIME-1  Every batch analysis records how long it took: processing time (decode + detection), video length, frames,
        speed (x real time), frames/s, time waiting in the queue, settings and device. Shown per dive in the Batch
        table; every analysis downloadable as CSV for benchmarking. Live dives and clips have none.
TIME-2  Read-ahead: the next frames are decoded while the GPU runs detection; same results, less waiting.
"""
import csv
import io

import pytest

from conftest import wait_for


def test_time_1_batch_processing_time_recorded(client, processed_dive, evidence):
    vid = processed_dive["id"]
    v = client.get(f"/api/videos/{vid}").json()
    t = v["activeRun"]["timing"]
    assert t and t["processingSec"] > 0 and t["frames"] == v["activeRun"]["framesAnalyzed"] > 0
    assert abs(t["videoSec"] - v["durationSec"]) < 0.5
    assert abs(t["speedX"] - t["videoSec"] / t["processingSec"]) < 0.05 * t["speedX"] + 0.01
    assert abs(t["framesPerSec"] - t["frames"] / t["processingSec"]) < 0.05 * t["framesPerSec"] + 0.01
    assert t["queuedSec"] is not None and t["queuedSec"] >= 0 and t["resumes"] == 0
    assert t["totalSec"] >= t["processingSec"] and t["finishedAt"] and t["resolution"] and t["model"]
    assert v["elapsedSec"] is None, "only a running job has an elapsed time"
    # re-analysis is timed too, and both show in the CSV
    r = client.post(f"/api/videos/{vid}/analyse", json={"conf": 0.5})
    assert r.status_code == 200, r.text
    try:
        wait_for(lambda: client.get(f"/api/videos/{vid}").json()["pendingRun"] is None, 60, 0.3, "re-analysis done")
    finally:                                     # other tests use this dive's original 0.25 analysis
        client.post(f"/api/runs/{v['activeRun']['id']}/activate")
    res = client.get("/api/batch/timings.csv")
    assert res.status_code == 200 and "attachment" in res.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(res.text)))
    mine = [x for x in rows if x["video"] == processed_dive["name"]]
    assert {x["run"] for x in mine} >= {"BATCH", "REANALYSIS"}
    assert all(float(x["processing_s"]) > 0 and float(x["speed_x_realtime"]) > 0 for x in mine)
    # live dives / clips are not in it
    assert not any(x["run"] in ("LIVE", "CLIP") for x in rows)
    evidence["TIME-1"] = {"video_s": t["videoSec"], "processing_s": t["processingSec"], "speed_x": t["speedX"],
                          "frames_per_s": t["framesPerSec"], "reader": t["reader"], "csv_rows": len(rows)}


def _serve_and_process(tmp, src, port, prefetch: int, latency_ms: int):
    """One backend process (separate settings) importing `src` once; returns (timing, detection keys)."""
    import json as _json
    import os
    import subprocess
    import sys
    import urllib.request
    from conftest import BACKEND, FAKES

    def http(url, data=None, method=None, headers=None, timeout=10):
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _json.loads(r.read() or b"null")

    def ok(fn):
        try:
            return fn()
        except Exception:
            return None
    d = tmp / f"pf{prefetch}"
    (d / "models").mkdir(parents=True)
    (d / "models" / "best.pt").write_bytes(b"x")
    env = {**os.environ, "FYP_MEDIA_DIR": str(d / "media"), "FYP_MAVLINK_URL": f"udpin:127.0.0.1:{port + 1}",
           "FYP_MODELS_DIR": str(d / "models"), "FYP_DB_PATH": str(d / "db.sqlite"), "FYP_PORT": str(port),
           "FYP_BATCH_PREFETCH": str(prefetch), "FAKE_YOLO_LATENCY_MS": str(latency_ms), "PYTHONUNBUFFERED": "1",
           "PYTHONPATH": os.pathsep.join([str(FAKES), str(BACKEND), os.environ.get("PYTHONPATH", "")])}
    api = f"http://127.0.0.1:{port}"
    log = open(d / "server.log", "w")
    p = subprocess.Popen([sys.executable, str(BACKEND / "main.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_for(lambda: ok(lambda: http(f"{api}/api/model/status")["is_loaded"]), 30, 0.3, "server boot")
        res = http(f"{api}/api/videos/upload-stream?filename=bench.mp4&name=bench&conf=0.25", data=src.read_bytes(),
                   method="POST", headers={"Content-Type": "application/octet-stream"}, timeout=60)
        vid = res["video"]["id"]
        done = wait_for(lambda: (lambda v: v if v["status"] == "COMPLETED" else None)(http(f"{api}/api/videos/{vid}")),
                        300, 0.5, "benchmark import")
        rows = http(f"{api}/api/videos/{vid}/defects")["defects"]
        return done["activeRun"]["timing"], sorted((r["defect_type"], round(r["timestamp_sec"], 3)) for r in rows)
    finally:
        p.kill()
        p.wait()
        log.close()


@pytest.mark.slow
def test_time_2_read_ahead_overlaps_decoding_and_detection(tmp_path, evidence):
    """Same dive with read-ahead off and on: identical detections and frame count; the time spent waiting for decoded
    frames drops, because decoding continues while the GPU runs detection."""
    from conftest import MEDIA
    from vv_media import make_dive
    src = make_dive(MEDIA / "bench_720p.mp4", seconds=120, size=(1280, 720))
    off, det_off = _serve_and_process(tmp_path, src, 18611, prefetch=0, latency_ms=40)
    on, det_on = _serve_and_process(tmp_path, src, 18621, prefetch=2, latency_ms=40)
    assert det_on == det_off and on["frames"] == off["frames"] == 240, "read-ahead must not change the results"
    assert on["prefetch"] > 0 and off["prefetch"] == 0
    # detection time is the same work both ways; what read-ahead removes is the wait for frames
    assert on["waitSec"] < off["waitSec"] * 0.7, (on, off)
    assert on["processingSec"] <= off["processingSec"] * 1.03, (on["processingSec"], off["processingSec"])
    evidence["TIME-2"] = {k: {"processing_s": t["processingSec"], "detect_s": t["detectSec"], "wait_s": t["waitSec"],
                              "speed_x": t["speedX"]} for k, t in (("read_ahead_off", off), ("read_ahead_on", on))}
    evidence["TIME-2"]["gain_pct"] = round((1 - on["processingSec"] / off["processingSec"]) * 100, 1)
