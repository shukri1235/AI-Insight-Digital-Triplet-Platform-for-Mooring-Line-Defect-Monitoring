"""
PIPELINE B - Headless batch analysis (dives and re-analysis runs).

* Every job is an AnalysisRun with an operator-chosen threshold. Re-analysing a video at
  a different threshold creates a NEW run; the original detections are never touched.
* One worker thread drains a FIFO of run ids; GPU access goes through the arbiter at
  BATCH priority, so a live dive always comes first and batch uses the idle cycles.
* Frame source: ffmpeg (-hwaccel auto -> NVDEC on NVIDIA) with an fps filter for dives;
  every frame via OpenCV for short clips (frame-exact timestamps). Sequential decode.
* Crash safety: detections and the run's checkpoint are committed in the same
  transaction (every 400 rows or 5 s), so a crash resumes from the last checkpoint.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

import cv2
import numpy as np

from config import BATCH_IOU, BATCH_PREFETCH, BATCH_SAMPLE_FPS, BATCH_SIZE, BATCH_USE_FFMPEG_READER, MEDIA_DIR
from database import AnalysisRun, DefectRecord, SessionLocal, VideoRecord, utcnow
from gpu_arbiter import BATCH, GpuArbiter
from model_manager import ModelManager, apply_stand_in
import event_log
import sensors_db
from video_utils import _low_priority_kwargs, ffmpeg_exe, is_browser_playable, make_browser_proxy, probe

PROXY_DIR = MEDIA_DIR / "proxies"
PROXY_DIR.mkdir(parents=True, exist_ok=True)
MAX_W = 1280
CHECKPOINT_SEC = 5.0


class CancelledJob(Exception):
    pass


def _target_size(w: int, h: int) -> tuple[int, int]:
    if w <= MAX_W:
        return w - w % 2, h - h % 2
    nh = int(round(h * MAX_W / w))
    return MAX_W, nh - nh % 2


def ffmpeg_frames(path: str, meta: dict, start_sec: float, fps: float, stats: dict):
    exe = ffmpeg_exe()
    if not exe or not meta.get("width"):
        raise RuntimeError("ffmpeg unavailable")
    w, h = _target_size(meta["width"], meta["height"])
    cmd = [exe, "-v", "error", "-threads", "2", "-hwaccel", "auto"]
    if start_sec > 0:
        cmd += ["-ss", f"{start_sec:.3f}"]
    cmd += ["-i", path, "-an", "-sn", "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=w * h * 3 * 4,
                            **_low_priority_kwargs())
    size, n = w * h * 3, 0
    stats["reader"] = "ffmpeg (hwaccel auto)"
    try:
        while True:
            buf = proc.stdout.read(size)
            if len(buf) < size:
                break
            yield start_sec + n / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            n += 1
    finally:
        proc.stdout.close()
        proc.kill()
        proc.wait()
    if n == 0:
        raise RuntimeError(f"ffmpeg produced no frames: {proc.stderr.read().decode(errors='ignore')[:200]}")


def opencv_frames(path: str, meta: dict, start_sec: float, fps: float, stats: dict):
    """Sequential decode; with fps >= the video's rate every frame is analysed and stamped
    with idx/video_fps (frame-exact - used for clips)."""
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("OpenCV could not open the video")
    stats["reader"] = "opencv"
    vfps = meta["fps"] or 30.0
    step = max(1, round(vfps / fps))
    idx = 0
    if start_sec > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, start_sec * 1000)
        idx = int(round(start_sec * vfps))
    try:
        while cap.grab():
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok and frame is not None:
                    hh, ww = frame.shape[:2]
                    if ww > MAX_W:
                        frame = cv2.resize(frame, _target_size(ww, hh), interpolation=cv2.INTER_AREA)
                    yield idx / vfps, frame
            idx += 1
    finally:
        cap.release()


_END = object()


def prefetch(frames, maxsize: int, stop: threading.Event):
    """Read ahead: decode the next frames in a background thread while the caller runs detection on the current batch,
    so decoding and the GPU work at the same time instead of taking turns. At most `maxsize` frames are held (bounded
    memory). Errors in the reader are raised in the caller; stopping early (cancel, error) closes the reader, which
    ends its ffmpeg process."""
    q: queue.Queue = queue.Queue(maxsize=max(1, maxsize))
    err: list[BaseException] = []

    def reader():
        try:
            for item in frames:
                while not stop.is_set():
                    try:
                        q.put(item, timeout=0.2)
                        break
                    except queue.Full:
                        continue
                if stop.is_set():
                    break
        except BaseException as e:                      # noqa: BLE001 - handed to the caller
            err.append(e)
        finally:
            close = getattr(frames, "close", None)
            if close:
                close()
            while True:                                 # the end marker must get through even if the queue is full
                try:
                    q.put(_END, timeout=0.2)
                    break
                except queue.Full:
                    if stop.is_set():
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            pass

    th = threading.Thread(target=reader, name="batch-reader", daemon=True)
    th.start()
    try:
        while True:
            item = q.get()
            if item is _END:
                if err:
                    raise err[0]
                return
            yield item
    finally:
        stop.set()
        th.join(timeout=5)


def create_run(video_id: str, kind: str, conf: float, sample_fps: float | None = None,
               parent_run_id: str | None = None, model_name: str | None = None) -> str:
    rid = str(uuid.uuid4())
    db = SessionLocal()
    try:
        db.add(AnalysisRun(id=rid, video_id=video_id, kind=kind, conf=round(conf, 3), sample_fps=sample_fps,
                           status="QUEUED", parent_run_id=parent_run_id, model_name=model_name))
        db.commit()
    finally:
        db.close()
    return rid


class BatchWorker:
    def __init__(self, model_manager: ModelManager, arbiter: GpuArbiter, live_worker=None):
        self.mm, self.arbiter, self.live = model_manager, arbiter, live_worker
        self.q: queue.Queue[str] = queue.Queue()
        self.current_run: str | None = None
        self.current_video: str | None = None
        self.current_fps = 0.0
        self.eta_sec: dict[str, float] = {}
        self.elapsed_sec: dict[str, float] = {}   # video id -> processing time so far (running job)
        self.last_job: dict | None = None
        self._cancel: set[str] = set()
        self._queued: set[str] = set()
        self._resume: set[str] = set()
        self._lock = threading.Lock()
        threading.Thread(target=self._loop, name="batch-worker", daemon=True).start()

    # back-compat name used by the API/UI
    @property
    def current_id(self):
        return self.current_video

    # ------------------------------------------------------------------ API
    def enqueue_run(self, run_id: str, resume: bool = False):
        with self._lock:
            if run_id in self._queued or run_id == self.current_run:
                return
            self._queued.add(run_id)
            self._cancel.discard(run_id)
            (self._resume.add if resume else self._resume.discard)(run_id)
        db = SessionLocal()
        try:
            run = db.get(AnalysisRun, run_id)
            if run:
                run.status = "QUEUED"
                v = db.get(VideoRecord, run.video_id)
                if v and v.status != "COMPLETED":
                    v.status, v.error_message = "QUEUED", None
                    if not resume:
                        v.progress = 0.0
                db.commit()
        finally:
            db.close()
        self.q.put(run_id)

    def cancel_video(self, video_id: str):
        db = SessionLocal()
        try:
            ids = [r.id for r in db.query(AnalysisRun).filter(AnalysisRun.video_id == video_id,
                                                               AnalysisRun.status.in_(["QUEUED", "PROCESSING"]))]
        finally:
            db.close()
        for rid in ids:
            self.cancel_run(rid)

    def cancel_run(self, run_id: str):
        with self._lock:
            self._cancel.add(run_id)
            was_queued = run_id in self._queued and run_id != self.current_run
        if was_queued:
            _finish_cancelled(run_id)

    def requeue_interrupted(self):
        db = SessionLocal()
        try:
            jobs = [(r.id, r.status == "PROCESSING" and r.checkpoint_sec is not None)
                    for r in db.query(AnalysisRun).filter(AnalysisRun.status.in_(["QUEUED", "PROCESSING"]),
                                                          AnalysisRun.kind.in_(["BATCH", "REANALYSIS"]))
                    .order_by(AnalysisRun.created_at)]
        finally:
            db.close()
        for rid, resume in jobs:
            print(f"[BATCH] Re-queueing interrupted run {rid}{' (resuming from checkpoint)' if resume else ''}")
            self.enqueue_run(rid, resume=resume)

    def queue_length(self) -> int:
        return len(self._queued)

    # ------------------------------------------------------------------ worker
    def _loop(self):
        while True:
            rid = self.q.get()
            with self._lock:
                self._queued.discard(rid)
                resume = rid in self._resume
                self._resume.discard(rid)
                if rid in self._cancel:
                    self._cancel.discard(rid)
                    continue
                self.current_run = rid
            try:
                self._process(rid, resume)
            except CancelledJob:
                print(f"[BATCH] Cancelled run {rid}")
                _finish_cancelled(rid)
            except Exception as e:
                print(f"[BATCH ERROR] run {rid}: {e}")
                _fail(rid, str(e)[:500])
            finally:
                with self._lock:
                    self.current_run = self.current_video = None
                    self._cancel.discard(rid)
                self.current_fps = 0.0
                self.eta_sec.clear()
                self.elapsed_sec.clear()

    def _process(self, rid: str, resume: bool):
        db = SessionLocal()
        try:
            run = db.get(AnalysisRun, rid)
            v = db.get(VideoRecord, run.video_id)
            path = v.current_filepath
            if not path or not Path(path).exists():
                raise FileNotFoundError(f"Media offline - file not found: {path}")
            meta = probe(path)
            v.duration_sec, v.fps, v.width, v.height, v.codec = (meta["duration_sec"], meta["fps"], meta["width"],
                                                                  meta["height"], meta["codec"])
            sample_fps = run.sample_fps or BATCH_SAMPLE_FPS
            frame_exact = sample_fps >= (meta["fps"] or 30.0) - 0.01
            start_sec = float(run.checkpoint_sec or 0.0) + (1.0 / sample_fps if run.checkpoint_sec is not None else 0) \
                if resume else 0.0
            if resume and run.checkpoint_sec is not None:
                db.query(DefectRecord).filter(DefectRecord.run_id == rid,
                                              DefectRecord.timestamp_sec > run.checkpoint_sec).delete()
                sensors_db.delete_objects(rid, after_t=run.checkpoint_sec)
            else:
                db.query(DefectRecord).filter(DefectRecord.run_id == rid).delete()
                sensors_db.delete_objects(rid)
                run.frames_analyzed = run.frames_with_defects = 0
            run.status, run.started_at, run.error_message = "PROCESSING", utcnow(), None
            run.model_name = run.model_name or self.mm.display_name
            run.sample_fps = sample_fps
            first_pass = v.status != "COMPLETED"
            if first_pass:
                v.status, v.error_message, v.started_at = "PROCESSING", None, utcnow()
            conf, video_id = run.conf, v.id
            # Test media may use stand-in classes (e.g. "person" rehearsed as DEFORMATION)
            stand_in = (json.loads(v.test_profile or "{}").get("standIn") or None) if v.is_test else None
            analysed_before, with_def_before = run.frames_analyzed or 0, run.frames_with_defects or 0
            # timing: a resumed run (crash / restart) adds to the time already spent; the outage itself is not counted
            prev = json.loads(run.timing) if resume and run.timing else {}
            queued_sec = prev.get("queuedSec")
            if queued_sec is None and run.created_at:
                queued_sec = round(max(0.0, (utcnow() - run.created_at).total_seconds()), 1)
            base_sec, resumes = float(prev.get("processingSec") or 0.0), int(prev.get("resumes") or 0) + (1 if resume else 0)
            first_started = prev.get("startedAt") or utcnow().isoformat()
            vid_info = {"resolution": f"{meta['width']}x{meta['height']}" if meta.get("width") else None,
                        "codec": meta.get("codec"), "videoFps": meta.get("fps")}
            db.commit()
        finally:
            db.close()
        if not self.mm.is_loaded:
            raise RuntimeError("No YOLO weights loaded - load a model on the Control Board first.")
        self.current_video = video_id
        duration = meta["duration_sec"] or 0.0
        print(f"[BATCH] Run {rid[:8]} on {video_id[:8]}: conf {conf}, {sample_fps} fps"
              f"{f', resuming at {start_sec:.1f}s' if start_sec else ''}")

        stats: dict = {}
        pending: list = []
        pending_objs: list = []
        frames, stamps = [], []
        analysed, with_def = 0, 0
        last_t, t_start = start_sec, time.monotonic()
        last_flush, last_progress = time.monotonic(), 0.0
        live_shared = float(prev.get("liveSharedSec") or 0.0)
        last_tick = t_start
        # where the time goes: detection (GPU) vs waiting for the next decoded frame (video reading)
        detect_sec, wait_sec = float(prev.get("detectSec") or 0.0), float(prev.get("waitSec") or 0.0)

        def timing(final: bool = False, proxy_sec: float | None = None) -> dict:
            proc = base_sec + (time.monotonic() - t_start)
            frames_total = analysed_before + analysed
            vsec = duration or last_t
            out = {"processingSec": round(proc, 2), "videoSec": round(vsec, 2), "frames": frames_total,
                   "framesPerSec": round(frames_total / max(proc, 1e-3), 2),
                   "speedX": round(vsec / max(proc, 1e-3), 2) if final else None,
                   "queuedSec": queued_sec, "proxySec": proxy_sec, "resumes": resumes,
                   "liveSharedSec": round(live_shared, 1), "detectSec": round(detect_sec, 2), "waitSec": round(wait_sec, 2),
                   "prefetch": BATCH_PREFETCH * BATCH_SIZE, "reader": stats.get("reader"), "sampleFps": sample_fps,
                   "batchSize": BATCH_SIZE, "model": self.mm.display_name, "device": getattr(self.mm, "device", None),
                   "startedAt": first_started, "finishedAt": utcnow().isoformat() if final else None, **vid_info}
            out["totalSec"] = round(proc + (proxy_sec or 0.0), 2)
            return out

        def flush(force=False):
            nonlocal pending, pending_objs, last_flush
            if force or len(pending) >= 400 or time.monotonic() - last_flush >= CHECKPOINT_SEC:
                # object log first: anything after a crash's checkpoint is deleted on resume
                sensors_db.insert_objects(video_id, rid, pending_objs)
                pending_objs = []
                s = SessionLocal()
                try:
                    s.add_all(pending)
                    r = s.get(AnalysisRun, rid)
                    r.checkpoint_sec = round(last_t, 3)
                    r.frames_analyzed, r.frames_with_defects = analysed_before + analysed, with_def_before + with_def
                    r.timing = json.dumps(timing())              # survives a crash: a resume adds to it
                    s.commit()
                finally:
                    s.close()
                pending, last_flush = [], time.monotonic()

        def run_batch():
            nonlocal frames, stamps, analysed, with_def, detect_sec
            if not frames:
                return
            t_inf = time.monotonic()
            results = self.arbiter.infer(frames, conf=conf, iou=BATCH_IOU, priority=BATCH, timeout=600)
            detect_sec += time.monotonic() - t_inf
            for dets, t in zip(results, stamps):
                apply_stand_in(dets, stand_in)
                defects = [d for d in dets if d["isDefect"] and d["confidence"] >= conf]
                pending_objs.extend((round(t, 3), d["rawClass"], d["confidence"], d["x"], d["y"], d["width"], d["height"])
                                    for d in dets if not d["isDefect"] and d["confidence"] >= conf)
                if defects:
                    with_def += 1
                for d in defects:
                    pending.append(DefectRecord(video_id=video_id, run_id=rid, defect_type=d["type"],
                                                raw_class=d["rawClass"], timestamp_sec=round(t, 3),
                                                confidence=d["confidence"], severity=d["severity"],
                                                x=d["x"], y=d["y"], width=d["width"], height=d["height"]))
            analysed += len(frames)
            frames, stamps = [], []
            flush()

        def source():
            if BATCH_USE_FFMPEG_READER and not frame_exact:
                try:
                    yield from ffmpeg_frames(path, meta, start_sec, sample_fps, stats)
                    return
                except RuntimeError as e:
                    if analysed or frames:
                        raise
                    print(f"[BATCH] ffmpeg reader unavailable ({e}); using OpenCV")
            yield from opencv_frames(path, meta, start_sec, sample_fps, stats)

        stop_reader = threading.Event()
        frames_in = prefetch(source(), BATCH_PREFETCH * BATCH_SIZE, stop_reader) if BATCH_PREFETCH else source()
        try:
            t_wait = time.monotonic()
            for t, frame in frames_in:
                wait_sec += time.monotonic() - t_wait
                if rid in self._cancel:
                    stop_reader.set()
                    raise CancelledJob()
                frames.append(frame)
                stamps.append(t)
                last_t = t
                if len(frames) >= BATCH_SIZE:
                    run_batch()
                now = time.monotonic()
                if self.live is not None and getattr(self.live, "is_active", False):
                    live_shared += now - last_tick                   # GPU shared with a live dive (benchmark context)
                last_tick = now
                self.elapsed_sec[video_id] = round(base_sec + now - t_start, 1)
                if now - last_progress > 1.0:
                    last_progress = now
                    pct = min(99.0, last_t / duration * 100) if duration else 0.0
                    self.current_fps = analysed / max(now - t_start, 1e-3)
                    done = last_t - start_sec
                    if done > 0 and duration:
                        self.eta_sec[video_id] = round((now - t_start) / done * (duration - last_t), 1)
                    _set_progress(rid, video_id, pct, first_pass)
                t_wait = time.monotonic()
        finally:
            stop_reader.set()
            if hasattr(frames_in, "close"):
                frames_in.close()                         # ends the reader thread and its ffmpeg process
        run_batch()
        flush(force=True)

        analysis_timing = timing(final=True)                     # decode + inference only
        playback, proxy_sec = None, None
        if first_pass and not is_browser_playable(meta["codec"]):
            print(f"[BATCH] Codec '{meta['codec']}' is not browser-playable; building an H.264 review proxy...")
            t_proxy = time.monotonic()
            proxy = make_browser_proxy(Path(path), PROXY_DIR / f"{video_id}.mp4")
            playback = str(proxy) if proxy else None
            proxy_sec = round(time.monotonic() - t_proxy, 2)
        if proxy_sec is not None:
            analysis_timing["proxySec"] = proxy_sec
            analysis_timing["totalSec"] = round(analysis_timing["processingSec"] + proxy_sec, 2)
        self.elapsed_sec.pop(video_id, None)

        elapsed = time.monotonic() - t_start
        processed = max(0.0, (duration or last_t) - start_sec)
        self.last_job = {"runId": rid, "videoId": video_id, "reader": stats.get("reader"), "framesAnalysed": analysed,
                         "elapsedSec": round(elapsed, 2), "videoSeconds": round(processed, 1),
                         "speedX": round(processed / max(elapsed, 1e-3), 1),
                         "framesPerSec": round(analysed / max(elapsed, 1e-3), 1), "resumedFrom": start_sec}
        db = SessionLocal()
        try:
            r = db.get(AnalysisRun, rid)
            r.status, r.progress, r.completed_at, r.checkpoint_sec = "COMPLETED", 100.0, utcnow(), None
            r.timing = json.dumps(analysis_timing)
            v = db.get(VideoRecord, video_id)
            v.active_run_id = rid
            v.frames_analyzed = r.frames_analyzed
            vname, kind, conf_used = v.display_name or v.original_filename, r.kind, r.conf
            if first_pass:
                v.status, v.progress, v.completed_at = "COMPLETED", 100.0, utcnow()
                if playback:
                    v.playback_path = playback
            db.commit()
        finally:
            db.close()
        print(f"[BATCH] Completed run {rid[:8]}: {self.last_job}")
        t = analysis_timing
        resumed = f", resumed {t['resumes']}x" if t.get("resumes") else ""
        event_log.app("Done", f"{'Re-analysis' if kind == 'REANALYSIS' else 'Batch analysis'} finished: {vname} at "
                              f"{conf_used:.2f} - {t['frames']} frames in {t['processingSec']:.1f} s "
                              f"({t['speedX']}x real time){resumed}")


def _set_progress(rid, vid, pct, first_pass):
    db = SessionLocal()
    try:
        r = db.get(AnalysisRun, rid)
        r.progress = round(pct, 1)
        if first_pass:
            v = db.get(VideoRecord, vid)
            v.progress = round(pct, 1)
        db.commit()
    finally:
        db.close()


def _fail(rid, msg):
    db = SessionLocal()
    try:
        r = db.get(AnalysisRun, rid)
        if r:
            r.status, r.error_message = "FAILED", msg
            v = db.get(VideoRecord, r.video_id)
            event_log.app("Error", f"Batch analysis failed: {(v.display_name or v.original_filename) if v else r.video_id} - {msg}")
            if v and v.status != "COMPLETED":
                v.status, v.error_message = "FAILED", msg
            db.commit()
    finally:
        db.close()


def _finish_cancelled(rid):
    db = SessionLocal()
    try:
        r = db.get(AnalysisRun, rid)
        if r:
            r.status, r.error_message = "CANCELLED", "Cancelled by operator"
            _v = db.get(VideoRecord, r.video_id)
            event_log.app("Batch", f"Batch analysis cancelled by the operator: {(_v.display_name or _v.original_filename) if _v else r.video_id}")
            db.query(DefectRecord).filter(DefectRecord.run_id == rid).delete()
            sensors_db.delete_objects(rid)    # its general-object log goes with it
            v = db.get(VideoRecord, r.video_id)
            if v and v.status != "COMPLETED":
                v.status, v.progress, v.error_message = "UNPROCESSED", 0.0, "Cancelled by operator"
            db.commit()
    finally:
        db.close()
