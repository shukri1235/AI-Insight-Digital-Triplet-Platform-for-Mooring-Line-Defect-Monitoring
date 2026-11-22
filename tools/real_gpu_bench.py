"""
Real-model GPU benchmark: the app's own GpuArbiter and ModelManager, with the real weights (v4) on the GPU and real
video frames, instead of the stand-in detector used by pytest. Run on the target PC with the app CLOSED:

  cd "D:\\FYP Prog"
  python tools\\real_gpu_bench.py --live-video D:\\Videos\\prep\\X1.mp4 --batch-video D:\\Videos\\prep\\C2.mp4

Four phases (each live phase paces frames at the camera rate, 30 fps):
  1. live alone              - live round trip (frame handed to the arbiter -> detections back) and inference rate
  2. batch alone             - batch jobs of batch.batch_size frames (8), as the batch pipeline sends them
  3. live + batch (arbiter)  - the shipped behaviour: batch cut into chunks of 2 while live is active
  4. live + batch, no chunks - the same load with chunking switched off (a batch job goes to the GPU in one piece):
                               the measured comparison for the arbiter, replacing the calculated 2.4 s
  plus one 120-frame job with live running and no chunks, the scenario the GA-2 test models (120 x 20 ms = 2.4 s).
  0. detection latency       - the TC-1.3 measurement on the real live video: the model first analyses EVERY frame to
                               find each onset (first frame it can flag a defect at the trigger), then the video is
                               replayed in real time with latest-frame-wins; latency = onset frame captured -> first
                               live flag (and -> the 3-of-5 alert). Add --onset-frame N ... to score from frames you
                               chose by eye as well (first frame where the defect is visible enough to label).

Then, with --lake-video, the WHOLE APP is measured: the real backend is started as a separate process with these
weights (temporary database and media folders, default settings; your own data is not touched):
  5. Test Mode dive with v4  - the live video streamed as the ROV camera (UDP) at 120.5 m, detection armed, trigger
                               0.60: capture rate, inference rate and time, skipped / late frames, GPU wait (NFR-1b,
                               NFR-2 and GA-2 with the real model, through the real live pipeline)
  5b. ... while a batch runs - the lake video imported during the dive: the same live figures under batch load, and
                               the batch's own processing time while sharing the GPU. Only the stretch while the
                               batch is on the GPU counts (the arbiter's batch frame counter is moving); what follows,
                               e.g. building a browser proxy for an HEVC GoPro file, is reported apart as 5c.
                               Rates in 5-5c are counted from the app's frame counters over wall time; the on-screen
                               captureFps / inferenceFps (an average of 1/interval) overstate them when frames arrive
                               unevenly, so they are kept only as display_* for reference.
  6. batch speed with v4     - the lake video analysed alone with read-ahead off, then on (TIME-1 / TIME-2 / NFR-6
                               with the real model): processing time, x real time, detection vs waiting for frames

Writes tests\\results\\real_gpu_bench.json and prints a summary. In phases 1-4 frames are read into memory first, so
disk and decoding are not part of those timings; phases 5-6 include everything the app does.
"""
import subprocess
import tempfile
import urllib.error
import urllib.request
import argparse
import json
import os
import platform
import statistics as st
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def frames_from(path, n, max_side):
    import cv2
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        sys.exit(f"Cannot open {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or n
    step = max(1, total // n)
    out, i = [], 0
    while len(out) < n:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            if max_side and max(f.shape[:2]) > max_side:
                s = max_side / max(f.shape[:2])
                f = cv2.resize(f, (int(f.shape[1] * s), int(f.shape[0] * s)), interpolation=cv2.INTER_AREA)
            out.append(f)
        i += 1
    cap.release()
    if not out:
        sys.exit(f"No frames read from {path}")
    return out


def pct(a, q):
    a = sorted(a)
    return round(a[min(len(a) - 1, int(q * len(a)))], 1) if a else None


class Live(threading.Thread):
    """Live loop at the camera rate: one frame at a time, LIVE priority, conf 0.40 (the logging threshold)."""

    def __init__(self, arb, frames, fps, live_prio, conf):
        super().__init__(daemon=True)
        self.arb, self.frames, self.period, self.LIVE, self.conf = arb, frames, 1.0 / fps, live_prio, conf
        self.stop = threading.Event()
        self.rt_ms, self.done_t, self.hits = [], [], 0

    def run(self):
        i = 0
        while not self.stop.is_set():
            t0 = time.perf_counter()
            dets = self.arb.infer([self.frames[i % len(self.frames)]], self.conf, 0.45, self.LIVE, timeout=30)[0]
            t1 = time.perf_counter()
            self.rt_ms.append((t1 - t0) * 1000)
            self.done_t.append(t1)
            self.hits += any(d.get("type") == "DEFORMATION" and d.get("confidence", 0) >= 0.60 for d in dets)
            i += 1
            # time.sleep, not Event.wait: on Windows Event.wait rounds up to the 15.6 ms system tick, which paced the
            # first run at ~19-24 fps instead of 30. time.sleep uses a high-resolution timer (Python 3.11+).
            rest = self.period - (time.perf_counter() - t0)
            if rest > 0:
                time.sleep(rest)

    def fps_between(self, a, b):
        n = sum(1 for t in self.done_t if a <= t <= b)
        return round(n / (b - a), 1) if b > a else None


def run_batch_jobs(arb, frames, job, BATCH, until=None, count=None):
    """Batch jobs of `job` frames back to back; returns (frames done, seconds, per-job seconds)."""
    t0, done, per, k = time.perf_counter(), 0, [], 0
    while True:
        if count is not None and k >= count:
            break
        if until is not None and time.perf_counter() >= until:
            break
        part = [frames[(k * job + j) % len(frames)] for j in range(job)]
        a = time.perf_counter()
        arb.infer(part, 0.25, 0.45, BATCH, timeout=120)
        per.append(time.perf_counter() - a)
        done += len(part)
        k += 1
    return done, time.perf_counter() - t0, per


def live_phase(arb, live_frames, seconds, fps, LIVE, batch=None):
    """Live loop for `seconds`; optional batch(callable) runs alongside. Returns the live and arbiter figures."""
    arb.reset_stats()
    lv = Live(arb, live_frames, fps, LIVE, 0.40)
    lv.start()
    time.sleep(0.5)                                   # live is now 'active' for the arbiter
    a = time.perf_counter()
    extra = batch() if batch else time.sleep(seconds)
    b = time.perf_counter()
    lv.stop.set(); lv.join()
    stats = arb.stats(window=b - a + 0.5)
    rt = [r for r, t in zip(lv.rt_ms, lv.done_t) if a <= t <= b]
    return {
        "live_fps": lv.fps_between(a, b),
        "live_roundtrip_ms_p50": pct(rt, 0.5), "live_roundtrip_ms_p95": pct(rt, 0.95), "live_roundtrip_ms_max": pct(rt, 1.0),
        "live_wait_ms_p50": stats["liveWaitMsP50"], "live_wait_ms_p95": stats["liveWaitMsP95"], "live_wait_ms_max": stats["liveWaitMsMax"],
        "live_infer_ms_p50": stats["liveInferMsP50"],
        "live_share_pct": stats["liveSharePct"], "batch_share_pct": stats["batchSharePct"],
        "live_frames": len(rt), "live_frames_with_deformation_ge_0.60": lv.hits,
        "live_stalls_over_500ms": sum(1 for r in rt if r > 500),
    }, extra


# ---------------------------------------------------------------------------------------------- detection latency
def deform_conf(dets):
    return max([d.get("confidence", 0) for d in dets if d.get("type") == "DEFORMATION"], default=0.0)


def latency_phase(arb, video, LIVE, BATCH, trigger, hold, window, onset_frames, max_side):
    """Detection latency on a real video with the real model (the TC-1.3 measurement, which pytest makes with the
    stand-in on a synthetic video).
    1. Offline: the model analyses EVERY frame; an onset is a frame with a deformation >= trigger after >= 1 s without
       one - the first frame on which the model can flag that defect.
    2. Live replay: the video is decoded at its own frame rate in real time while the live loop analyses only the
       latest frame (as the app does). Latency = capture of the onset frame -> first live result >= trigger at or
       after it (skipped frames + GPU processing included); alert = the hold rule (hits of the last window results).
    Optional human onsets (--onset-frame) are scored the same way."""
    import cv2
    cap = cv2.VideoCapture(str(video))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    def resized(f):
        if max_side and max(f.shape[:2]) > max_side:
            s_ = max_side / max(f.shape[:2])
            f = cv2.resize(f, (int(f.shape[1] * s_), int(f.shape[0] * s_)), interpolation=cv2.INTER_AREA)
        return f

    # 1. every frame, in order
    confs, buf = [], []
    while True:
        ok, f = cap.read()
        if ok:
            buf.append(resized(f))
        if (not ok and buf) or len(buf) == 8:
            for dets in arb.infer(buf, 0.25, 0.45, BATCH, timeout=300):
                confs.append(deform_conf(dets))
            buf = []
        if not ok:
            break
    cap.release()
    n = len(confs)
    gap = int(round(fps))                                   # >= 1 s without a detection starts a new onset
    onsets, last_hit = [], -10 ** 9
    for i, c in enumerate(confs):
        if c >= trigger:
            if i - last_hit > gap:
                onsets.append(i)
            last_hit = i

    # 2. real-time replay, latest frame wins
    cap = cv2.VideoCapture(str(video))
    latest = {"i": -1, "f": None, "t": 0.0}
    cap_t = {}
    lock, done = threading.Lock(), threading.Event()

    def capture():
        t0 = time.perf_counter()
        i = 0
        while True:
            ok, f = cap.read()
            if not ok:
                break
            due = t0 + i / fps
            rest = due - time.perf_counter()
            if rest > 0:
                time.sleep(rest)
            t = time.perf_counter()
            with lock:
                latest.update(i=i, f=resized(f), t=t)
            cap_t[i] = t
            i += 1
        done.set()

    results = []                                            # (frame index, result time, conf)
    th = threading.Thread(target=capture, daemon=True)
    th.start()
    seen = -1
    while not (done.is_set() and latest["i"] == seen):
        with lock:
            i, f = latest["i"], latest["f"]
        if i == seen or f is None:
            time.sleep(0.001)
            continue
        seen = i
        c = deform_conf(arb.infer([f], 0.40, 0.45, LIVE, timeout=30)[0])
        results.append((i, time.perf_counter(), c))
    th.join()
    cap.release()

    def score(f0):
        t0 = cap_t.get(f0)
        if t0 is None:
            return None
        # only this pass of the defect: a flag more than 5 s later would belong to another link
        after = [r for r in results if f0 <= r[0] <= f0 + int(5 * fps)]
        first = next((r for r in after if r[2] >= trigger), None)
        alert, hist = None, []
        for r in after:
            hist = (hist + [r[2] >= trigger])[-window:]
            if sum(hist) >= hold:
                alert = r
                break
        return {"onset_frame": f0, "onset_s": round(f0 / fps, 3), "flagged_within_5s": first is not None,
                "first_flag_frame": first[0] if first else None,
                "flag_latency_ms": round((first[1] - t0) * 1000, 1) if first else None,
                "frames_skipped_before_flag": (sum(1 for r in after if r[0] < first[0]) if first else None),
                "alert_latency_ms": round((alert[1] - t0) * 1000, 1) if alert else None}

    model_on = [x for x in (score(f0) for f0 in onsets) if x]
    human_on = [x for x in (score(f0) for f0 in onset_frames) if x]
    fl = [x["flag_latency_ms"] for x in model_on if x["flag_latency_ms"] is not None]
    al = [x["alert_latency_ms"] for x in model_on if x["alert_latency_ms"] is not None]
    live_n = len(results)
    dur = (max(cap_t.values()) - min(cap_t.values())) if cap_t else 0
    return {"video": str(video), "fps": round(fps, 2), "frames": n, "trigger": trigger, "hold": f"{hold} of {window}",
            "frames_with_deformation_ge_trigger": sum(1 for c in confs if c >= trigger),
            "live_frames_analysed": live_n, "live_fps": round(live_n / dur, 1) if dur else None,
            "model_onsets": model_on, "human_onsets": human_on,
            "flag_latency_ms_median": round(st.median(fl), 1) if fl else None,
            "flag_latency_ms_max": max(fl) if fl else None,
            "alert_latency_ms_median": round(st.median(al), 1) if al else None,
            "flagged_onsets": f"{len(fl)} of {len(model_on)}"}


# ---------------------------------------------------------------------------------------------- whole-app phases
def http(api, path, data=None, method=None, timeout=30):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(api + path, data=body, method=method or ("POST" if body else "GET"),
                                 headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:                       # show the app's own reason, not just the code
        detail = e.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"{method or ('POST' if body else 'GET')} {path} -> HTTP {e.code}: {detail}") from None


def wait_until(fn, timeout, what, step=0.5):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    raise TimeoutError(f"timed out waiting for {what}")


class App:
    """The real backend in its own process, with temporary folders and default settings."""

    def __init__(self, port, weights, prefetch=None, fake=False):
        self.dir = Path(tempfile.mkdtemp(prefix="fyp_bench_"))
        (self.dir / "models").mkdir()
        import shutil
        shutil.copy(weights, self.dir / "models" / "best.pt")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("FYP_", "GEMINI_", "GOOGLE_API"))}
        env.update({"FYP_CONFIG": str(self.dir / "none.toml"), "FYP_PORT": str(port),
                    "FYP_MODELS_DIR": str(self.dir / "models"), "FYP_DEFAULT_MODEL": "best.pt",
                    "FYP_MEDIA_DIR": str(self.dir / "media"), "FYP_DB_PATH": str(self.dir / "db.sqlite"),
                    "FYP_LOGS_DIR": str(self.dir / "logs"), "FYP_MAVLINK_URL": f"udpin:127.0.0.1:{port + 1}",
                    "FYP_TEST_STREAM_PORT": str(port + 2), "PYTHONUNBUFFERED": "1",
                    "PYTHONPATH": os.pathsep.join(([str(ROOT / "tests" / "fakes")] if fake else [])
                                                  + [str(ROOT / "backend"), os.environ.get("PYTHONPATH", "")])})
        if prefetch is not None:
            env["FYP_BATCH_PREFETCH"] = str(prefetch)
        if fake:
            env["FAKE_YOLO_LATENCY_MS"] = "20"
        self.api = f"http://127.0.0.1:{port}"
        self.log = open(self.dir / "server.log", "w")
        self.p = subprocess.Popen([sys.executable, str(ROOT / "backend" / "main.py")], env=env,
                                  stdout=self.log, stderr=subprocess.STDOUT, cwd=str(ROOT))
        try:
            st_ = wait_until(lambda: (lambda m: m if m.get("is_loaded") else None)(http(self.api, "/api/model/status")),
                             180, "backend + model load")
        except Exception:
            self.close()
            raise SystemExit(f"Backend did not start - see {self.dir / 'server.log'}")
        self.device = st_.get("device")

    def import_video(self, path, name):
        r = http(self.api, "/api/videos/import", {"path": str(path), "name": name, "line_id": "ML-01-NORTH", "conf": 0.25})
        return (r.get("video") or r)["id"]

    def video(self, vid):
        return http(self.api, f"/api/videos/{vid}")

    def close(self):
        try:
            self.p.kill(); self.p.wait(timeout=20)
        except Exception:
            pass
        self.log.close()


def sample_live(app, seconds, until=None):
    """Poll the live pipeline every 0.5 s for `seconds` (or until until() is true). Returns the raw rows."""
    rows, t_end = [], time.time() + seconds
    while time.time() < t_end and not (until and until()):
        s_ = http(app.api, "/api/live/state")
        h = http(app.api, "/api/live/health")
        rows.append((time.time(), s_, h))
        time.sleep(0.5)
    return rows


def summarise_live(rows, arb_row=None):
    """Live figures over `rows`, from the app's own counters.

    Rates are counted, not averaged: frames captured = change in the latest frame number (frames inferred + frames
    skipped), divided by the wall time between the first and last row. The app's on-screen captureFps/inferenceFps
    are an average of 1/interval, which overstates the rate whenever frames arrive unevenly (a 5 ms and a 61 ms
    interval average to 108 fps, not 30), so they are kept only as 'display_*' for reference."""
    if len(rows) < 2:
        return {"samples": len(rows)}
    (t0, a, _), (t1, b, h1) = rows[0], rows[-1]
    dt = max(t1 - t0, 1e-6)
    seq = lambda s: (s.get("framesSkipped") or 0) + (s.get("framesInferred") or 0)
    cap_n, inf_n = seq(b) - seq(a), (b.get("framesInferred") or 0) - (a.get("framesInferred") or 0)
    skip_n = (b.get("framesSkipped") or 0) - (a.get("framesSkipped") or 0)
    med = lambda k: round(st.median([r[1].get(k) or 0 for r in rows]), 1)
    arb = (arb_row or rows[-1])[2]["arbiter"]
    cap = h1.get("capture") or {}
    return {"samples": len(rows), "seconds": round(dt, 1),
            "capture_fps_counted": round(cap_n / dt, 1), "inference_fps_counted": round(inf_n / dt, 1),
            "frames_captured": cap_n, "frames_inferred": inf_n, "frames_skipped": skip_n,
            "frames_skipped_pct": round(100 * skip_n / cap_n, 1) if cap_n else None,
            "late_frames": (b.get("lateFrames") or 0) - (a.get("lateFrames") or 0),
            "capture_interval_ms_p50_last900": cap.get("p50Ms"), "capture_interval_ms_p95_last900": cap.get("p95Ms"),
            "capture_interval_ms_max_last900": cap.get("maxMs"), "source_fps": b.get("sourceFps"),
            "inference_ms_median": med("inferenceMs"),
            "display_capture_fps_median": med("captureFps"), "display_inference_fps_median": med("inferenceFps"),
            "gpu_live_wait_ms_p50": arb.get("liveWaitMsP50"), "gpu_live_wait_ms_p95": arb.get("liveWaitMsP95"),
            "gpu_live_wait_ms_max": arb.get("liveWaitMsMax"), "gpu_live_infer_ms_p50": arb.get("liveInferMsP50"),
            "gpu_live_share_pct_last10s": arb.get("liveSharePct"), "gpu_batch_share_pct_last10s": arb.get("batchSharePct"),
            "device": arb.get("device")}


def split_batch_rows(rows):
    """Split the rows of a live+batch phase into (on GPU, after) by the arbiter's batch frame counter: the batch is on
    the GPU from the row before its counter first moves to the last row where it moved. What follows (e.g. building a
    browser proxy for an HEVC file) is CPU/encoder work, not GPU sharing."""
    fb = [r[2]["arbiter"].get("framesBatch") or 0 for r in rows]
    moved = [i for i in range(1, len(rows)) if fb[i] > fb[i - 1]]
    if not moved:
        return [], rows
    return rows[moved[0] - 1:moved[-1] + 1], rows[moved[-1] + 1:]


def timing_of(v):
    t = (v.get("activeRun") or {}).get("timing") or {}
    keep = ("videoSec", "processingSec", "speedX", "framesPerSec", "frames", "detectSec", "waitSec", "prefetch",
            "resolution", "model", "device", "gpuSharedWithLive", "liveSharedSec", "proxySec", "totalSec", "codec")
    return {k: t.get(k) for k in keep if k in t}


def app_phases(a, res):
    port = 18760
    print("5/6 whole app: Test Mode dive with these weights ...")
    app = App(port, a.weights, fake=a.fake)
    try:
        res["app_device"] = app.device
        http(app.api, "/api/test-mode/start", {"path": str(Path(a.live_video).resolve()), "target_depth": 120.5,
                                                "start_depth": 120.5, "descent_rate": 1.0})
        wait_until(lambda: http(app.api, "/api/test-mode").get("streaming"), 30, "test camera streaming")
        src = http(app.api, "/api/live/sources")[0]["value"]
        http(app.api, "/api/live/start", {"source": src, "line_id": "ML-01-NORTH", "trigger_conf": 0.60,
                                          "detection_mode": "ARMED", "recording": True})
        wait_until(lambda: (http(app.api, "/api/live/state").get("inferenceFps") or 0) > 0, 60, "live inference running")
        time.sleep(3)                                                      # settle (decoder start, first shapes)
        res["5_app_live_alone"] = summarise_live(sample_live(app, a.seconds))
        print("   + the lake video imported as a batch during the dive ...")
        vid = app.import_video(a.lake_video, "bench-during-live")
        wait_until(lambda: app.video(vid)["status"] in ("PROCESSING", "COMPLETED"), 120, "batch to start")
        done = lambda: app.video(vid)["status"] in ("COMPLETED", "FAILED")
        rows = sample_live(app, 900, until=done)
        on_gpu, after = split_batch_rows(rows)
        r5b = summarise_live(on_gpu) if on_gpu else {"samples": 0, "note": "batch counter never moved"}
        if on_gpu:
            win = on_gpu[-1][0] - on_gpu[0][0]
            full = [r for r in on_gpu if r[0] - on_gpu[0][0] >= 10]           # rows whose 10 s share window is all batch
            shares = [r[2]["arbiter"].get("batchSharePct") for r in (full or on_gpu)]
            r5b["gpu_batch_share_pct_median"] = round(st.median(shares), 1)
            r5b["gpu_live_share_pct_median"] = round(100 - st.median(shares), 1)
            r5b["share_rows_have_full_10s_window"] = bool(full)
            # the wait percentiles cover the app's last 500 live frames; they describe the batch only if those
            # 500 frames all arrived while the batch was on the GPU
            span = 500 / max(r5b.get("inference_fps_counted") or 1e-6, 1e-6)
            r5b["gpu_wait_span_s"] = round(span, 1)
            r5b["gpu_wait_figures_cover_batch_only"] = span <= win
        r5b["batch_on_gpu_window_s"] = round(on_gpu[-1][0] - on_gpu[0][0], 1) if on_gpu else 0.0
        res["5b_app_live_during_batch"] = r5b
        if len(after) >= 2:
            res["5c_app_live_after_batch_inference"] = summarise_live(after)   # e.g. while the review proxy is built
        wait_until(done, 900, "batch to finish", 1)
        v = app.video(vid)
        res["5b_app_live_during_batch"]["batch_status"] = v["status"]
        res["5b_app_live_during_batch"]["batch_timing"] = timing_of(v)
        http(app.api, "/api/live/stop", {})
        http(app.api, "/api/test-mode/stop", {})
    finally:
        app.close()

    for k, pf in (("6_app_batch_read_ahead_off", 0), ("6_app_batch_read_ahead_on", None)):
        print(f"6/6 whole app: lake video alone, read-ahead {'off' if pf == 0 else 'on (default)'} ...")
        port += 10
        app = App(port, a.weights, prefetch=pf, fake=a.fake)
        try:
            vid = app.import_video(a.lake_video, "bench-alone")
            wait_until(lambda: app.video(vid)["status"] in ("COMPLETED", "FAILED"), 900, "batch to finish", 1)
            v = app.video(vid)
            res[k] = {"status": v["status"], **timing_of(v),
                      "detections_logged": len(http(app.api, f"/api/videos/{vid}/defects")["defects"])}
        finally:
            app.close()


def print_app(res):
    def line(lbl, L):
        if not L:
            return
        print(f"{lbl}: capture {L.get('capture_fps_counted')} fps counted (screen shows {L.get('display_capture_fps_median')}), "
              f"inference {L.get('inference_fps_counted')} fps counted, {L.get('frames_skipped_pct')} % of frames skipped, "
              f"{L.get('inference_ms_median')} ms per frame, late frames {L.get('late_frames')}, "
              f"GPU wait p95 {L.get('gpu_live_wait_ms_p95')} ms (max {L.get('gpu_live_wait_ms_max')}), over {L.get('seconds')} s")
    line("5  app live alone      ", res.get("5_app_live_alone"))
    L5b = res.get("5b_app_live_during_batch", {})
    line("5b app live + batch GPU", L5b)
    if L5b:
        print(f"   batch on GPU for {L5b.get('batch_on_gpu_window_s')} s, GPU share live/batch "
              f"{L5b.get('gpu_live_share_pct_median')} / {L5b.get('gpu_batch_share_pct_median')} %, batch "
              f"{L5b.get('batch_timing', {}).get('speedX')}x real time"
              + ("" if L5b.get("gpu_wait_figures_cover_batch_only") else
                 f"  [GPU wait figures span {L5b.get('gpu_wait_span_s')} s, longer than the batch: use a longer video]"))
    line("5c app live after batch inference (proxy build etc.)", res.get("5c_app_live_after_batch_inference"))
    for k, lbl in (("6_app_batch_read_ahead_off", "off"), ("6_app_batch_read_ahead_on", "on ")):
        b = res.get(k, {})
        print(f"6 app batch, read-ahead {lbl}: {b.get('processingSec')} s for {b.get('videoSec')} s of video "
              f"({b.get('speedX')}x), detect {b.get('detectSec')} s, waiting for frames {b.get('waitSec')} s, "
              f"{b.get('frames')} frames, {b.get('detections_logged')} detections logged")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", default=str(ROOT / "backend" / "models" / "best.pt"))
    ap.add_argument("--live-video", required=True, help="e.g. D:\\Videos\\prep\\X1.mp4")
    ap.add_argument("--batch-video", required=True, help="e.g. D:\\Videos\\prep\\C2.mp4")
    ap.add_argument("--seconds", type=float, default=15.0, help="length of each live phase")
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--max-side", type=int, default=0, help="resize frames so the long side is at most this (0 = as recorded)")
    ap.add_argument("--out", default=str(ROOT / "tests" / "results" / "real_gpu_bench.json"))
    ap.add_argument("--lake-video", help="a recorded dive (e.g. one of the held-out lake videos) for phases 5-6; "
                                         "leave it out to run phases 1-4 only")
    ap.add_argument("--onset-frame", type=int, nargs="*", default=[],
                    help="optional: frame numbers (0 = first frame of --live-video) where a defect first becomes "
                         "visible enough to label, found by stepping through the video; latency is also scored from these")
    ap.add_argument("--app-only", action="store_true",
                    help="run only the whole-app phases 5-6 and add them to the existing --out file (after a run "
                         "that stopped there)")
    ap.add_argument("--allow-cpu", action="store_true", help=argparse.SUPPRESS)      # dry runs only
    ap.add_argument("--fake", action="store_true", help=argparse.SUPPRESS)           # dry runs only (stand-in detector)
    a = ap.parse_args()

    from config import VIDEO_EXTS
    for flag, v in (("--live-video", a.live_video), ("--batch-video", a.batch_video), ("--lake-video", a.lake_video)):
        if v is None:
            continue
        if "<" in v or not Path(v).is_file():
            sys.exit(f"{flag}: no such file: {v}")
        if Path(v).suffix.lower() not in VIDEO_EXTS:
            sys.exit(f"{flag}: the app only imports {', '.join(VIDEO_EXTS)} files: {v}")
    if a.app_only:
        if not a.lake_video:
            sys.exit("--app-only needs --lake-video")
        res = json.load(open(a.out, encoding="utf-8")) if Path(a.out).is_file() else {}
        app_phases(a, res)
        json.dump(res, open(a.out, "w", encoding="utf-8"), indent=2)
        print_app(res)
        print(f"\nWritten: {a.out}")
        return

    import gpu_arbiter
    from config import BATCH_SIZE, ARBITER_BATCH_CHUNK_WHEN_LIVE
    from gpu_arbiter import BATCH, LIVE, GpuArbiter
    from model_manager import ModelManager

    mm = ModelManager()
    s = mm.load(str(Path(a.weights).resolve()))
    if not s.get("success"):
        sys.exit(f"Model did not load: {mm.last_error}")
    if not str(mm.device).startswith("cuda") and not a.allow_cpu:
        sys.exit(f"Model is on {mm.device}, not the GPU - stopping (this benchmark is for the GPU).")
    try:
        import torch
        gpu, tv, cv = torch.cuda.get_device_name(0), torch.__version__, torch.version.cuda
    except Exception:
        gpu, tv, cv = "n/a", "n/a", "n/a"
    import ultralytics
    print(f"Model {Path(mm.weights_path).name} on {mm.device} (half={mm.half}), classes {mm.class_names}")
    print("Reading frames ...")
    live_frames = frames_from(a.live_video, 90, a.max_side)
    batch_frames = frames_from(a.batch_video, 120, a.max_side)
    arb = GpuArbiter(mm)
    # Warm every input shape the phases use, twice each: the first call with a new batch size / orientation takes
    # ~2.5 s on this GPU (kernel selection), which the first run counted as a live stall and as the 120-frame job time.
    print("Warming up every batch shape ...")
    for _ in range(2):
        arb.infer(live_frames[:1], 0.40, 0.45, LIVE, timeout=300)
        for n in sorted({ARBITER_BATCH_CHUNK_WHEN_LIVE, BATCH_SIZE, 120}):
            arb.infer(batch_frames[:n], 0.25, 0.45, BATCH, timeout=300)
    time.sleep(1.2)                                                    # let 'live active' lapse

    res = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "host": platform.platform(),
           "gpu": gpu, "torch": tv, "cuda": cv,
           "ultralytics": getattr(ultralytics, "__version__", "?"), "weights": mm.weights_path, "half": mm.half,
           "classes": mm.class_names, "live_video": a.live_video, "batch_video": a.batch_video,
           "frame_size": list(live_frames[0].shape[1::-1]), "batch_job_frames": BATCH_SIZE,
           "chunk_when_live": ARBITER_BATCH_CHUNK_WHEN_LIVE, "fps_target": a.fps}

    print(f"1/4 live alone ({a.seconds:.0f} s) ...")
    res["1_live_alone"], _ = live_phase(arb, live_frames, a.seconds, a.fps, LIVE)

    print("2/4 batch alone ...")
    time.sleep(1.2)
    n, secs, per = run_batch_jobs(arb, batch_frames, BATCH_SIZE, BATCH, count=30)
    res["2_batch_alone"] = {"frames": n, "seconds": round(secs, 2), "frames_per_s": round(n / secs, 1),
                            "job_ms_p50": round(st.median(per) * 1000, 1), "job_ms_max": round(max(per) * 1000, 1)}

    print(f"3/4 live + batch with the arbiter ({a.seconds:.0f} s) ...")
    time.sleep(1.2)
    box = {}
    def batch_for_phase():
        box["b"] = run_batch_jobs(arb, batch_frames, BATCH_SIZE, BATCH, until=time.perf_counter() + a.seconds)
    res["3_live_plus_batch_arbiter"], _ = live_phase(arb, live_frames, a.seconds, a.fps, LIVE, batch_for_phase)
    n, secs, _ = box["b"]
    res["3_live_plus_batch_arbiter"]["batch_frames_per_s"] = round(n / secs, 1)

    print(f"4/4 live + batch, chunking off ({a.seconds:.0f} s) ...")
    time.sleep(1.2)
    gpu_arbiter.ARBITER_BATCH_CHUNK_WHEN_LIVE = 10 ** 9                # a batch job goes to the GPU in one piece
    try:
        res["4_live_plus_batch_no_chunks"], _ = live_phase(arb, live_frames, a.seconds, a.fps, LIVE, batch_for_phase)
        n, secs, _ = box["b"]
        res["4_live_plus_batch_no_chunks"]["batch_frames_per_s"] = round(n / secs, 1)
        print("   + one 120-frame job, live running, no chunks (the GA-2 scenario) ...")
        time.sleep(1.2)
        def one_big():
            t0 = time.perf_counter()
            arb.infer(batch_frames[:120], 0.25, 0.45, BATCH, timeout=300)
            box["big_s"] = time.perf_counter() - t0
        res["4b_120_frame_job_no_chunks"], _ = live_phase(arb, live_frames, 0, a.fps, LIVE, one_big)
        res["4b_120_frame_job_no_chunks"]["job_seconds"] = round(box["big_s"], 2)
    finally:
        gpu_arbiter.ARBITER_BATCH_CHUNK_WHEN_LIVE = ARBITER_BATCH_CHUNK_WHEN_LIVE

    print("   detection latency on the live video (every frame offline, then a real-time replay) ...")
    time.sleep(1.2)
    from config import LIVE_TRIGGER_DEFAULT
    try:
        from config import TRIGGER_HITS, TRIGGER_WINDOW
    except ImportError:
        TRIGGER_HITS, TRIGGER_WINDOW = 3, 5
    res["0_detection_latency"] = latency_phase(arb, a.live_video, LIVE, BATCH, LIVE_TRIGGER_DEFAULT, TRIGGER_HITS,
                                               TRIGGER_WINDOW, a.onset_frame, a.max_side)

    l1, l3, l4 = res["1_live_alone"], res["3_live_plus_batch_arbiter"], res["4_live_plus_batch_no_chunks"]
    res["summary"] = {
        "v4_live_roundtrip_ms_p95_alone": l1["live_roundtrip_ms_p95"],
        "v4_live_inference_ms_p50": l1["live_infer_ms_p50"],
        "live_wait_ms_p95_with_arbiter": l3["live_wait_ms_p95"],
        "live_wait_ms_p95_without_chunks": l4["live_wait_ms_p95"],
        "live_wait_ms_max_120_frame_job": res["4b_120_frame_job_no_chunks"]["live_wait_ms_max"],
        "live_fps_ratio_with_arbiter_pct": round(100 * l3["live_fps"] / l1["live_fps"], 1) if l1["live_fps"] else None,
        "batch_share_pct_with_arbiter": l3["batch_share_pct"],
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(res, open(a.out, "w", encoding="utf-8"), indent=2)      # saved now, in case phases 5-6 fail
    if a.lake_video:
        app_phases(a, res)
        json.dump(res, open(a.out, "w", encoding="utf-8"), indent=2)

    print("\n=== Real-model GPU benchmark ===")
    print(f"{res['gpu']} | torch {res['torch']} CUDA {res['cuda']} | {Path(mm.weights_path).name} half={mm.half} | "
          f"frames {res['frame_size'][0]}x{res['frame_size'][1]}")
    L0 = res["0_detection_latency"]
    print(f"0 detection latency : {L0['flagged_onsets']} model onsets flagged live; flag median {L0['flag_latency_ms_median']} ms "
          f"(max {L0['flag_latency_ms_max']}), alert ({L0['hold']}) median {L0['alert_latency_ms_median']} ms; "
          f"replay {L0['live_fps']} fps analysed")
    for h in L0["human_onsets"]:
        print(f"    from your onset frame {h['onset_frame']} ({h['onset_s']} s): flag {h['flag_latency_ms']} ms, alert {h['alert_latency_ms']} ms")
    print(f"1 live alone        : {l1['live_fps']} fps, round trip p50 {l1['live_roundtrip_ms_p50']} / p95 "
          f"{l1['live_roundtrip_ms_p95']} ms, inference p50 {l1['live_infer_ms_p50']} ms")
    b2 = res["2_batch_alone"]
    print(f"2 batch alone       : {b2['frames_per_s']} frames/s, one {BATCH_SIZE}-frame job p50 {b2['job_ms_p50']} ms")
    print(f"3 live+batch arbiter: live {l3['live_fps']} fps ({res['summary']['live_fps_ratio_with_arbiter_pct']} % of alone), "
          f"wait p95 {l3['live_wait_ms_p95']} ms (max {l3['live_wait_ms_max']}), round trip p95 {l3['live_roundtrip_ms_p95']} ms, "
          f"batch {l3['batch_frames_per_s']} frames/s, GPU share live {l3['live_share_pct']} % / batch {l3['batch_share_pct']} %")
    print(f"4 live+batch no chunk: live {l4['live_fps']} fps, wait p95 {l4['live_wait_ms_p95']} ms (max {l4['live_wait_ms_max']}), "
          f"batch {l4['batch_frames_per_s']} frames/s")
    b4 = res["4b_120_frame_job_no_chunks"]
    print(f"4b one 120-frame job : took {b4['job_seconds']} s; live wait max {b4['live_wait_ms_max']} ms")
    if a.lake_video:
        print_app(res)
    print(f"\nWritten: {a.out}")


if __name__ == "__main__":
    main()
