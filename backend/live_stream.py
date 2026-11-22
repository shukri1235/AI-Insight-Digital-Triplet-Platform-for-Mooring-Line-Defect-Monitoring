"""
PIPELINE A - Real-time ROV stream monitoring (BlueROV2).

Decoupled threads, so no stage can stall another:

  capture thread    reads the ROV's RTSP stream (BlueOS) / webcam / a replayed file
                    -> downscales to <=720p, JPEG-encodes ONCE
                    -> rolling RAM buffer sized to 5 s of video (150 frames at 30 fps)
                    -> MJPEG to the browser
  inference thread  newest frame only (latest-frame-wins) -> GPU arbiter (priority LIVE)
  clip threads      fire-and-forget: post-roll wait, buffer snapshot, H.264 encode, DB
  db writer thread  batched inserts so SQLite commits never block YOLO

Operator controls (Live tab only):
  trigger threshold  clip trigger: DEFORMATION >= threshold -> incident clip (default 0.60).
                     Changes are recorded on the live analysis run with their session time.
  detection mode     ARMED (always detect) | STANDBY (no inference, no alerts - e.g. surface
                     transit; frees the GPU for batch) | AUTO (armed while the ROV is below
                     AUTO_ARM_DEPTH_M, standby otherwise or when no ROV telemetry)
  record dive log    sensor samples + raw .tlog + detection log for this dive.
                     Incident clips are ALWAYS saved, recording or not.
  freeze frame       purely a viewer feature in the UI - the pipeline never pauses.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2

from config import (AUTO_ARM_DEPTH_M, DIVE_IDLE_TAIL_SEC, LIVE_DECODER_THREADS, CLIP_BUFFER_MAX_FRAMES, CLIP_BUFFER_NOMINAL_FRAMES, CLIP_SECONDS, CLIPS_DIR,
                    INCIDENT_COOLDOWN_SEC, INCIDENT_POST_ROLL_SEC, INFERENCE_CONF_FLOOR, LIVE_DEFECT_LOG_INTERVAL,
                    LIVE_JPEG_QUALITY, LIVE_LOG_CONF, LIVE_MAX_WIDTH, LIVE_TRIGGER_DEFAULT, TELEMETRY_RECORD_MAX_AGE_SEC,
                    TRIGGER_CLASSES, TRIGGER_HITS, TRIGGER_WINDOW)
from database import AnalysisRun, DefectRecord, SessionLocal, VideoRecord, utcnow
from gpu_arbiter import LIVE, GpuArbiter
from model_manager import ModelManager, apply_stand_in
from rov_link import RovLink
import event_log
import sensors_db
from telemetry import enrich
from video_utils import probe, sha256_file, write_jpegs_to_mp4

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")


class FrameEntry:
    __slots__ = ("seq", "t", "jpeg", "detections", "telemetry")

    def __init__(self, seq: int, t: float, jpeg: bytes):
        self.seq, self.t, self.jpeg = seq, t, jpeg
        self.detections: list | None = None
        self.telemetry: dict | None = None


class _Ema:
    def __init__(self, alpha=0.1):
        self.alpha, self.value = alpha, None

    def update(self, x):
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value


def decoder_params() -> list[int]:
    """Open-time parameters for a live network stream: few decoder threads, because each one adds a frame of delay."""
    prop = getattr(cv2, "CAP_PROP_N_THREADS", None)            # OpenCV >= 4.6
    return [prop, LIVE_DECODER_THREADS] if prop is not None and LIVE_DECODER_THREADS > 0 else []


def buffer_len_for(fps: float) -> int:
    return max(10, min(CLIP_BUFFER_MAX_FRAMES, int(round(fps * CLIP_SECONDS))))


def recordable(tel: dict | None) -> dict | None:
    """Telemetry that may be recorded with an incident: None while the link is STALE (heartbeat older than
    ROV_STALE_SEC), or when the last depth message is older than TELEMETRY_RECORD_MAX_AGE_SEC (messages have
    stopped, e.g. a cut tether, before the link is declared stale). Old values stay on screen, greyed once
    stale, so the pilot sees the last known depth, but they are never stored as if current - not in the
    alert, the clip's name or the clip's per-frame sensor copy (3.6.11). Within the 1 s window a clip frame
    holds the latest reading, as every frame does between 5 Hz depth messages."""
    if not tel or tel.get("stale"):
        return None
    age = tel.get("depthAgeSec")
    return None if age is None or age > TELEMETRY_RECORD_MAX_AGE_SEC else tel


class LiveStreamWorker:
    def __init__(self, model_manager: ModelManager, arbiter: GpuArbiter, rov: RovLink):
        self.mm, self.arbiter, self.rov = model_manager, arbiter, rov
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._writers: list[threading.Thread] = []
        self._control_lock = threading.Lock()
        self._event_lock = threading.Lock()

        self.buffer: deque[FrameEntry] = deque(maxlen=CLIP_BUFFER_NOMINAL_FRAMES)
        self._latest: FrameEntry | None = None
        self._latest_raw = None

        self.status = "IDLE"
        self.error: str | None = None
        self.session_id: str | None = None
        self.run_id: str | None = None
        self.source = self.source_label = self.source_kind = None
        self.recording = True
        # AUTO by default: detection arms itself once the ROV reports depth below AUTO_ARM_DEPTH_M,
        # so deck and surface footage never raise incidents
        self.detection_mode = "AUTO"
        self.line_id = "ML-01-NORTH"
        self.trigger_conf = LIVE_TRIGGER_DEFAULT
        self.iou = 0.45
        self.source_fps = 30.0
        self.frame_w = self.frame_h = None
        self._session_t0 = time.monotonic()
        self._threshold_log: list = []
        self._ops_changes: list = []          # [[session_t, MODE|RECORDING, value], ...] for the dive log
        self._last_data_mono, self._idle_tail = 0.0, 0.0
        self._finalized = True                   # no dive open yet

        self.state_version = 0
        self._state: dict = {}
        self.incidents: deque[dict] = deque(maxlen=50)
        self.incident_seq = 0
        self._last_incident_at = 0.0
        self._trig_hist: deque = deque(maxlen=max(1, TRIGGER_WINDOW))   # (seq, t, best triggering detection | None)
        self.trigger_hits, self.trigger_window = max(1, min(TRIGGER_HITS, TRIGGER_WINDOW)), max(1, TRIGGER_WINDOW)
        self.trigger_flickers = 0                                          # single hits that did not hold
        self.clips_written = 0
        self.last_clip_write_ms: float | None = None

        self._capture_fps = _Ema()
        self._infer_fps = _Ema()
        self._infer_ms = _Ema()
        self._last_infer_done = None
        self.frames_inferred = 0
        self.frames_with_defects = 0
        self.frame_intervals: deque[float] = deque(maxlen=900)
        self.frame_log: deque[tuple[float, float]] = deque(maxlen=3000)
        self.late_frames = 0
        self._last_cap_t: float | None = None

        self._db_queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._db_writer, name="live-db-writer", daemon=True).start()
        # Non-defect detections ("person", "fish" ...) go to the object log in the sensor DB
        self._obj_queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._obj_writer, name="live-obj-writer", daemon=True).start()
        self.test_mode = None            # set by main: TestMode instance
        self.is_test = False
        self.test_profile: dict | None = None

    # ------------------------------------------------------------------ control
    @property
    def is_active(self) -> bool:
        return self.status in ("CONNECTING", "LIVE", "RECONNECTING")

    @property
    def armed(self) -> bool:
        if self.detection_mode == "ARMED":
            return True
        if self.detection_mode == "STANDBY":
            return False
        return self.arm_wait is None

    @property
    def arm_wait(self) -> str | None:
        """AUTO only: why detection is not armed yet - NO_DEPTH (no fresh ROV depth) or SHALLOW."""
        if self.detection_mode != "AUTO":
            return None
        tel = self.rov.telemetry()            # AUTO: armed only with fresh ROV depth below the threshold
        if not tel or tel.get("stale") or tel.get("depth") is None:
            return "NO_DEPTH"
        return None if tel["depth"] >= AUTO_ARM_DEPTH_M else "SHALLOW"

    def start(self, source: str, label: str | None = None, line_id: str | None = None,
              trigger_conf: float | None = None, iou: float | None = None, recording: bool = True,
              detection_mode: str | None = None) -> dict:
        with self._control_lock:
            if self.is_active:
                self._stop_locked()
            self._stop.clear()
            with self._cond:
                self.buffer = deque(maxlen=CLIP_BUFFER_NOMINAL_FRAMES)
                self._latest, self._latest_raw = None, None
            self.source, self.source_label = source, label or source
            self.source_kind = self._classify(source)
            # A session started while Test Mode is on is TEST data, whatever its source
            self.is_test = bool(self.test_mode and self.test_mode.enabled)
            self.test_profile = self.test_mode.profile() if self.is_test else None
            self.line_id = line_id or self.line_id
            if trigger_conf is not None:
                self.trigger_conf = max(INFERENCE_CONF_FLOOR, min(0.99, trigger_conf))
            if iou is not None:
                self.iou = iou
            if detection_mode in ("ARMED", "STANDBY", "AUTO"):
                self.detection_mode = detection_mode
            self.error, self.status = None, "CONNECTING"
            self.frames_inferred = self.frames_with_defects = 0
            self._infer_fps, self._infer_ms, self._last_infer_done = _Ema(), _Ema(), None
            self.frame_intervals.clear()
            self.frame_log.clear()
            self.late_frames, self._last_cap_t, self._last_incident_at = 0, None, 0.0
            self._trig_hist.clear()
            self.trigger_flickers = 0
            self._session_t0 = time.monotonic()
            self._last_data_mono, self._idle_tail = self._session_t0, 0.0   # last video frame (see _stop_locked)
            self._finalized = False
            self._threshold_log = [[0.0, self.trigger_conf]]
            self._ops_changes = [[0.0, "MODE", self.detection_mode], [0.0, "RECORDING", bool(recording)]]

            self.session_id, self.run_id = str(uuid.uuid4()), str(uuid.uuid4())
            db = SessionLocal()
            try:
                db.add(VideoRecord(id=self.session_id, file_hash=None, original_filename=self.source_label,
                                   display_name=f"Live dive {datetime.now():%Y-%m-%d %H:%M}", current_filepath=None,
                                   status="LIVE", source_type="LIVE_SESSION", line_id=self.line_id,
                                   started_at=utcnow(), active_run_id=self.run_id, is_managed=True,
                                   is_test=self.is_test,
                                   test_profile=json.dumps(self.test_profile) if self.test_profile else None))
                db.flush()
                db.add(AnalysisRun(id=self.run_id, video_id=self.session_id, kind="LIVE", conf=LIVE_LOG_CONF,
                                   trigger_conf=self.trigger_conf, threshold_log=json.dumps(self._threshold_log),
                                   model_name=self.mm.display_name, status="LIVE", started_at=utcnow()))
                db.commit()
            finally:
                db.close()
            self.recording = recording
            if recording:
                self.rov.start_session(self.session_id, self._session_t0)
            event_log.app("Live", f"Live dive started: {self.source_label} on {self.line_id or 'no line'}, trigger "
                                  f"{self.trigger_conf:.2f}, detection {self.detection_mode.lower()}, dive log "
                                  f"{'on' if recording else 'off'}{' (TEST)' if self.is_test else ''} - id {self.session_id[:8]}")

            self._threads = [threading.Thread(target=self._capture_loop, name="live-capture", daemon=True),
                             threading.Thread(target=self._inference_loop, name="live-inference", daemon=True)]
            for th in self._threads:
                th.start()
            self._publish(detections=[], telemetry=enrich(self.rov.telemetry(), self.line_id), t=0.0)
            return self.snapshot()

    def stop(self) -> dict:
        with self._control_lock:
            self._stop_locked()
            return self.snapshot()

    def _stop_locked(self):
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        for th in self._threads:
            th.join(timeout=5)
        self._threads = []
        self.rov.stop_session()
        # A dive is closed ONCE. A later stop - pressing Stop again, Test Mode off, or closing the app hours after the
        # dive ended - must not reopen it: it used to overwrite the end time and duration with the time of that later
        # call (a 1-minute dive closed by the app's shutdown the next morning read 9 h 33 min).
        if self.session_id and not self._finalized:
            self._finalized = True
            # A dive left open with nothing coming in (PC asleep overnight, stream and vehicle gone) ends at its last
            # video frame or sensor reading, not when Stop was finally pressed; the idle time is kept in the ops log.
            end = time.monotonic()
            rov_last = getattr(self.rov, "last_sample_mono", 0.0)
            last = max(self._last_data_mono, rov_last if rov_last >= self._session_t0 else 0.0)
            self._idle_tail = round(end - last, 1) if end - last > DIVE_IDLE_TAIL_SEC else 0.0
            db = SessionLocal()
            try:
                rec = db.get(VideoRecord, self.session_id)
                if rec:
                    rec.status, rec.completed_at, rec.frames_analyzed = "ENDED", utcnow(), self.frames_inferred
                    rec.duration_sec = round((last if self._idle_tail else end) - self._session_t0, 1)
                run = db.get(AnalysisRun, self.run_id) if self.run_id else None
                if run:
                    run.status, run.completed_at, run.progress = "COMPLETED", utcnow(), 100.0
                    run.frames_analyzed, run.frames_with_defects = self.frames_inferred, self.frames_with_defects
                    run.threshold_log = json.dumps(self._threshold_log)
                    run.ops_log = json.dumps(self.ops_log())
                db.commit()
            finally:
                db.close()
            event_log.app("Live", f"Live dive ended - id {self.session_id[:8]}, {round(((last if self._idle_tail else end) - self._session_t0) / 60, 1)} min"
                                  f" with data, {self.frames_inferred} frames analysed"
                                  f"{f', left open {round(self._idle_tail / 60, 1)} min more with no data' if self._idle_tail else ''}")
        if self.status != "ERROR":
            self.status = "IDLE"
        with self._cond:
            self._latest, self._latest_raw = None, None
        self._publish(detections=[], telemetry=None, t=0.0)

    def ops_log(self) -> dict:
        """Operator changes during the dive (detection mode, dive-log recording) + one-off trigger hits ignored."""
        return {"changes": list(self._ops_changes), "flickers": self.trigger_flickers, "idleTailSec": self._idle_tail}

    def _log_change(self, kind: str, value):
        if not (self.is_active and self.session_id):
            return
        self._ops_changes.append([round(time.monotonic() - self._session_t0, 2), kind, value])
        db = SessionLocal()                       # persisted at once: a crashed dive keeps its change history
        try:
            run = db.get(AnalysisRun, self.run_id) if self.run_id else None
            if run:
                run.ops_log = json.dumps(self.ops_log())
                run.threshold_log = json.dumps(self._threshold_log)
                db.commit()
        finally:
            db.close()

    def set_recording(self, enabled: bool):
        if enabled != self.recording:
            self._log_change("RECORDING", bool(enabled))
        if enabled and not self.recording and self.is_active and self.session_id:
            self.rov.start_session(self.session_id, self._session_t0)
        elif not enabled and self.recording:
            self.rov.stop_session()
        self.recording = enabled
        self._publish_meta()

    def set_detection_mode(self, mode: str):
        if mode not in ("ARMED", "STANDBY", "AUTO"):
            raise ValueError("mode must be ARMED, STANDBY or AUTO")
        if mode != self.detection_mode:
            self.detection_mode = mode
            self._log_change("MODE", mode)
        self.detection_mode = mode
        self._publish_meta()

    def configure(self, line_id: str | None = None, trigger_conf: float | None = None, iou: float | None = None):
        if line_id:
            self.line_id = line_id
        if trigger_conf is not None:
            new = round(max(INFERENCE_CONF_FLOOR, min(0.99, trigger_conf)), 3)
            if new != self.trigger_conf:
                self.trigger_conf = new
                if self.is_active:   # audit trail: when the operator moved the trigger threshold
                    self._threshold_log.append([round(time.monotonic() - self._session_t0, 2), new])
                    self._log_change("TRIGGER", new)
        if iou is not None:
            self.iou = max(0.01, min(0.99, iou))
        self._publish_meta()

    # ------------------------------------------------------------------ capture
    @staticmethod
    def _classify(source: str) -> str:
        s = source.strip().lower()
        if s.isdigit():
            return "webcam"
        if s.startswith(("rtsp://", "rtsps://", "rtmp://", "http://", "https://", "udp://", "tcp://", "srt://")) \
                or s.endswith(".sdp"):
            return "network"
        return "replay"

    def _resolve_source(self):
        src = self.source.strip()
        if self.source_kind == "webcam":
            idx = int(src)
            return cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
        if src.startswith("video:"):
            db = SessionLocal()
            try:
                rec = db.get(VideoRecord, src[6:])
                path = rec.current_filepath if rec else None
            finally:
                db.close()
            if not path or not Path(path).exists():
                raise FileNotFoundError(f"Replay source not found on disk: {path}")
            return cv2.VideoCapture(path)
        if self.source_kind == "network":
            return cv2.VideoCapture(src, cv2.CAP_FFMPEG, decoder_params())
        return cv2.VideoCapture(src)

    def _open(self):
        cap = self._resolve_source()
        if not cap or not cap.isOpened():
            raise ConnectionError(f"Could not open stream '{self.source_label}'")
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        fps = cap.get(cv2.CAP_PROP_FPS)
        self.source_fps = fps if fps and 1 <= fps <= 120 else 30.0
        with self._cond:
            n = buffer_len_for(self.source_fps)
            if self.buffer.maxlen != n:
                self.buffer = deque(self.buffer, maxlen=n)
        return cap

    def _capture_loop(self):
        cap, retries, seq = None, 0, 0
        pace_t0, pace_n = time.monotonic(), 0
        replay = self.source_kind == "replay"
        try:
            while not self._stop.is_set():
                if cap is None:
                    try:
                        cap = self._open()
                        retries, self.status, self.error = 0, "LIVE", None
                        pace_t0, pace_n, self._last_cap_t = time.monotonic(), 0, None
                        self._publish_meta()
                        threads = cap.get(getattr(cv2, "CAP_PROP_N_THREADS", -1)) if self.source_kind == "network" else None
                        print(f"[LIVE] Connected to {self.source_label} ({self.source_kind}, {self.source_fps:.1f} fps, "
                              f"buffer {self.buffer.maxlen} frames{f', {threads:.0f} decoder threads' if threads else ''})")
                    except Exception as e:
                        retries += 1
                        self.error = str(e)
                        if retries > 8:
                            self.status = "ERROR"
                            self._publish_meta()
                            return
                        self.status = "RECONNECTING" if retries > 1 else "CONNECTING"
                        self._publish_meta()
                        self._stop.wait(min(5.0, 0.5 * 2 ** retries))
                        continue
                ok, frame = cap.read()
                if not ok or frame is None:
                    if replay:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        pace_t0, pace_n = time.monotonic(), 0
                        continue
                    cap.release()
                    cap, self.status, self.error = None, "RECONNECTING", "Stream dropped - reconnecting"
                    self._publish_meta()
                    continue
                if replay:   # a file reads as fast as the disk allows; pace it like a camera
                    pace_n += 1
                    lag = pace_t0 + pace_n / self.source_fps - time.monotonic()
                    if lag > 0:
                        self._stop.wait(lag)
                h, w = frame.shape[:2]
                if w > LIVE_MAX_WIDTH:
                    frame = cv2.resize(frame, (LIVE_MAX_WIDTH, int(h * LIVE_MAX_WIDTH / w)), interpolation=cv2.INTER_AREA)
                self.frame_h, self.frame_w = frame.shape[:2]
                ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, LIVE_JPEG_QUALITY])
                if not ok:
                    continue
                now = time.monotonic()
                seq += 1
                entry = FrameEntry(seq, now - self._session_t0, jpg.tobytes())
                with self._cond:
                    self.buffer.append(entry)
                    self._latest, self._latest_raw = entry, frame
                    self._cond.notify_all()
                if self._last_cap_t is not None:
                    dt = now - self._last_cap_t
                    if dt > 0:
                        self._capture_fps.update(1.0 / dt)
                        self.frame_intervals.append(dt)
                        self.frame_log.append((now, dt))     # timestamped, for windowed timing analysis
                        if dt > 1.5 / self.source_fps:
                            self.late_frames += 1
                self._last_cap_t = self._last_data_mono = now
        finally:
            if cap is not None:
                cap.release()

    # ---------------------------------------------------------------- inference
    def _inference_loop(self):
        last_seq, last_pub = -1, 0.0
        last_defect_log: dict[str, float] = {}
        while not self._stop.is_set():
            with self._cond:
                self._cond.wait_for(lambda: self._stop.is_set() or
                                    (self._latest is not None and self._latest.seq != last_seq), timeout=0.5)
                if self._stop.is_set():
                    break
                entry, frame = self._latest, self._latest_raw
            if entry is None or entry.seq == last_seq:
                continue
            last_seq = entry.seq
            telemetry = enrich(self.rov.telemetry(), self.line_id)

            detections: list[dict] = []
            if self.armed and self.mm.is_loaded:
                t0 = time.perf_counter()
                try:
                    detections = self.arbiter.infer([frame], conf=INFERENCE_CONF_FLOOR, iou=self.iou, priority=LIVE,
                                                    timeout=10)[0]
                except Exception as e:
                    self.error = f"Inference error: {e}"
                    self._stop.wait(0.5)
                    continue
                dt = time.perf_counter() - t0
                self._infer_ms.update(dt * 1000)
                now = time.perf_counter()          # throughput (completions per second), not 1/latency
                if self._last_infer_done is not None:
                    self._infer_fps.update(1.0 / max(now - self._last_infer_done, 1e-3))
                self._last_infer_done = now
                self.frames_inferred += 1
                # every class is shown and logged from LIVE_LOG_CONF; generic classes are never defects
                detections = [d for d in detections if d["confidence"] >= LIVE_LOG_CONF]
                if self.is_test and self.test_profile:
                    apply_stand_in(detections, self.test_profile.get("standIn"))
                for d in detections:
                    d["triggering"] = d["isDefect"] and d["type"] in TRIGGER_CLASSES and d["confidence"] >= self.trigger_conf
            elif time.monotonic() - last_pub < 0.2:
                continue  # standby / no weights: keep telemetry + twin alive at ~5 Hz, no GPU use

            defects = [d for d in detections if d["isDefect"]]
            if defects:
                self.frames_with_defects += 1
            entry.detections, entry.telemetry = detections, telemetry
            self._publish(detections=detections, telemetry=telemetry, t=entry.t)
            last_pub = time.monotonic()

            if self.recording and self.run_id and defects:
                rows = []
                for d in defects:
                    key = f'{d["type"]}:{d.get("triggering")}'
                    if entry.t - last_defect_log.get(key, -1e9) < LIVE_DEFECT_LOG_INTERVAL:
                        continue
                    last_defect_log[key] = entry.t
                    rows.append(_defect_row(self.session_id, self.run_id, entry.t, d))
                if rows:
                    self._db_queue.put(rows)

            if self.recording and self.run_id:
                objs = []
                for d in detections:
                    if d["isDefect"]:
                        continue
                    key = f'obj:{d.get("rawClass")}'
                    if entry.t - last_defect_log.get(key, -1e9) < LIVE_DEFECT_LOG_INTERVAL:
                        continue
                    last_defect_log[key] = entry.t
                    objs.append(_obj_row(entry.t, d))
                if objs:
                    self._obj_queue.put((self.session_id, self.run_id, objs))

            triggering = [d for d in defects if d.get("triggering")]
            if self.armed and self.mm.is_loaded:           # only inferred frames count towards the hold window
                gone = self._trig_hist[0] if len(self._trig_hist) == self._trig_hist.maxlen else None
                self._trig_hist.append((entry.seq, entry.t, max(triggering, key=lambda d: d["confidence"]) if triggering else None))
                if gone and gone[2] is not None and sum(1 for h in self._trig_hist if h[2] is not None) == 0:
                    self.trigger_flickers += 1            # a hit left the window without ever holding
            hits = [h for h in self._trig_hist if h[2] is not None]
            if triggering and len(hits) >= self.trigger_hits and time.monotonic() - self._last_incident_at > INCIDENT_COOLDOWN_SEC:
                self._last_incident_at = time.monotonic()
                first = hits[0]                              # the clip is centred on when the defect first appeared
                best = max((h[2] for h in hits), key=lambda d: d["confidence"])
                event = self._incident_event(
                    {"stage": "triggered", "sessionTime": round(first[1], 3), "triggerSeq": first[0],
                     "sessionId": self.session_id, "triggerConf": self.trigger_conf,
                     "held": {"hits": len(hits), "window": self.trigger_window, "need": self.trigger_hits},
                     "defect": best, "telemetry": recordable(telemetry),
                     "lineId": self.line_id, "at": datetime.now().isoformat()})
                self._trig_hist.clear()
                th = threading.Thread(target=self._write_incident, args=(event,), name="incident-writer", daemon=True)
                self._writers = [w for w in self._writers if w.is_alive()] + [th]
                th.start()

    # ---------------------------------------------------------------- incidents
    def _write_incident(self, event: dict):
        t_start = time.perf_counter()
        try:
            if INCIDENT_POST_ROLL_SEC > 0:
                self._stop.wait(INCIDENT_POST_ROLL_SEC)
            with self._cond:
                snapshot = list(self.buffer)
            if len(snapshot) < 2:
                raise RuntimeError("buffer empty")
            span = snapshot[-1].t - snapshot[0].t
            fps = (len(snapshot) - 1) / span if span > 0 else self.source_fps
            tel = event["telemetry"]
            depth = f"{tel['depth']:.1f}".replace(".", "_") + "m" if tel and tel.get("depth") is not None else "NOROV"
            display = f"{event['lineId']}_Incident_{depth}_{datetime.now():%Y-%m-%d_%H%M%S}"
            clip_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
            out = write_jpegs_to_mp4([e.jpeg for e in snapshot], fps, CLIPS_DIR / f"incident-{clip_id[:8]}.mp4")
            meta, ext = probe(out), out.suffix
            index_of = {e.seq: i for i, e in enumerate(snapshot)}
            trig_i = index_of.get(event["triggerSeq"])
            trigger_offset = round(trig_i / fps, 3) if trig_i is not None else None

            analysed = [e for e in snapshot if e.detections is not None]
            # A clip can be byte-identical to an earlier one (Test Mode looping the same video, the same footage
            # replayed). The hash is unique because it identifies imported files; an identical clip is still a new
            # incident, so it is saved without a hash instead of failing.
            clip_hash = sha256_file(out)
            db = SessionLocal()
            try:
                if db.query(VideoRecord.id).filter(VideoRecord.file_hash == clip_hash).first():
                    clip_hash = None
                db.add(VideoRecord(
                    id=clip_id, file_hash=clip_hash, original_filename=display + ext, display_name=display + ext,
                    current_filepath=str(out), status="COMPLETED", source_type="INCIDENT_CLIP", is_managed=True,
                    parent_video_id=event["sessionId"], line_id=event["lineId"], progress=100.0,
                    duration_sec=meta["duration_sec"] or round(len(snapshot) / fps, 2), fps=round(fps, 3),
                    width=meta["width"], height=meta["height"], codec=meta["codec"], trigger_offset_sec=trigger_offset,
                    file_size=out.stat().st_size, frames_analyzed=len(analysed), completed_at=utcnow(),
                    active_run_id=run_id, is_test=self.is_test, parent_offset_sec=round(snapshot[0].t, 3),
                    test_profile=json.dumps(self.test_profile) if self.test_profile else None))
                db.flush()
                db.add(AnalysisRun(id=run_id, video_id=clip_id, kind="CLIP", conf=LIVE_LOG_CONF,
                                   trigger_conf=event["triggerConf"], sample_fps=round(fps, 3),
                                   model_name=self.mm.display_name, status="COMPLETED", progress=100.0,
                                   frames_analyzed=len(analysed),
                                   frames_with_defects=sum(1 for e in analysed if any(d["isDefect"] for d in e.detections)),
                                   started_at=utcnow(), completed_at=utcnow()))
                for i, e in enumerate(snapshot):
                    for d in (e.detections or []):
                        if d["isDefect"]:
                            db.add(_defect_row(clip_id, run_id, i / fps, d))
                db.commit()
            finally:
                db.close()
            # The clip carries its own copy of the sensor readings, one per frame
            sensors_db.insert_samples([
                {"media_id": clip_id, "t": round(i / fps, 3), "source": self.rov.source,
                 **{f: e.telemetry.get(f) for f in sensors_db.FIELDS}}
                for i, e in enumerate(snapshot) if recordable(e.telemetry)])
            sensors_db.insert_objects(clip_id, run_id, [
                _obj_row(i / fps, d) for i, e in enumerate(snapshot) for d in (e.detections or []) if not d["isDefect"]])
            self.clips_written += 1
            self.last_clip_write_ms = round((time.perf_counter() - t_start) * 1000 - INCIDENT_POST_ROLL_SEC * 1000, 1)
            print(f"[INCIDENT] Saved {len(snapshot)}-frame clip ({len(snapshot) / fps:.2f}s, trigger {trigger_offset}s) -> {out}")
            d = event.get("defect") or {}
            depth = f", depth {tel['depth']:.1f} m" if tel and tel.get("depth") is not None else ""
            event_log.app("Clip", f"Incident clip saved: {display + ext} - {str(d.get('type', '')).lower()} "
                                  f"{round((d.get('confidence') or 0) * 100)}% at dive time {event.get('sessionTime')} s"
                                  f"{depth}{' (TEST)' if self.is_test else ''}")
            self._incident_event(event, stage="saved", clipId=clip_id, clipName=display + ext,
                                 triggerOffset=trigger_offset, frames=len(snapshot))
        except Exception as e:
            print(f"[INCIDENT ERROR] Failed to write clip: {e}")
            event_log.app("Error", f"Incident clip could not be written: {e}")
            self._incident_event(event, stage="failed", error=str(e))

    def wait_for_writers(self, timeout: float = 20.0):
        end = time.monotonic() + timeout
        for th in list(self._writers):
            th.join(max(0.0, end - time.monotonic()))

    def _incident_event(self, base: dict, **extra) -> dict:
        with self._event_lock:
            self.incident_seq += 1
            ev = {**base, **extra, "seq": self.incident_seq}
            self.incidents.append(ev)
            return ev

    # ---------------------------------------------------------------- db writer
    def _db_writer(self):
        while True:
            batch = list(self._db_queue.get())
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and len(batch) < 500:
                try:
                    batch.extend(self._db_queue.get(timeout=0.1))
                except queue.Empty:
                    break
            db = SessionLocal()
            try:
                db.add_all(batch)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"[LIVE DB] Write failed ({len(batch)} rows): {e}")
            finally:
                db.close()

    def _obj_writer(self):
        while True:
            media_id, run_id, rows = self._obj_queue.get()
            try:
                sensors_db.insert_objects(media_id, run_id, rows)
            except Exception as e:
                print(f"[LIVE OBJ] Write failed ({len(rows)} rows): {e}")

    def flush_db(self, timeout: float = 5.0):
        end = time.monotonic() + timeout
        while (not self._db_queue.empty() or not self._obj_queue.empty()) and time.monotonic() < end:
            time.sleep(0.05)
        time.sleep(0.7)

    # ---------------------------------------------------------------- publish
    def _publish(self, detections, telemetry, t):
        self._state = {"detections": detections, "telemetry": telemetry, "t": round(t, 3),
                       "frameSeq": self._latest.seq if self._latest else 0}
        self.state_version += 1

    def _publish_meta(self):
        self.state_version += 1

    def capture_health(self) -> dict:
        iv = sorted(self.frame_intervals)
        if not iv:
            return {"samples": 0}
        p = lambda q: round(iv[min(len(iv) - 1, int(q * len(iv)))] * 1000, 1)
        return {"samples": len(iv), "nominalMs": round(1000 / self.source_fps, 1), "p50Ms": p(0.5),
                "p95Ms": p(0.95), "p99Ms": p(0.99), "maxMs": round(iv[-1] * 1000, 1), "lateFrames": self.late_frames}

    def snapshot(self) -> dict:
        cooldown = max(0.0, INCIDENT_COOLDOWN_SEC - (time.monotonic() - self._last_incident_at)) \
            if self._last_incident_at else 0.0
        buf = list(self.buffer)
        return {
            "type": "live_state", "status": self.status, "error": self.error,
            "sessionId": self.session_id, "runId": self.run_id,
            "source": self.source_label, "sourceKind": self.source_kind,
            "isTest": self.is_test, "testProfile": self.test_profile,
            "recording": self.recording, "detectionMode": self.detection_mode,
            "armed": self.armed if self.is_active else False,
            "armWait": self.arm_wait if self.is_active else None, "autoArmDepth": AUTO_ARM_DEPTH_M,
            "lineId": self.line_id, "triggerConf": self.trigger_conf, "logConf": LIVE_LOG_CONF, "iou": self.iou,
            "triggerDefault": LIVE_TRIGGER_DEFAULT,
            "triggerHold": {"hits": self.trigger_hits, "window": self.trigger_window,
                            "now": sum(1 for h in self._trig_hist if h[2] is not None), "flickers": self.trigger_flickers},
            "thresholdLog": self._threshold_log,
            "engineStatus": "ONLINE" if self.mm.is_loaded else "STANDBY",
            "captureFps": round(self._capture_fps.value or 0, 1), "sourceFps": round(self.source_fps, 2),
            "inferenceFps": round(self._infer_fps.value or 0, 1) if self.armed else 0.0, "inferenceMs": round(self._infer_ms.value or 0, 1),
            "framesInferred": self.frames_inferred,
            "framesSkipped": max(0, (self._latest.seq if self._latest else 0) - self.frames_inferred)
            if self.mm.is_loaded else 0,
            "lateFrames": self.late_frames,
            "bufferFrames": len(buf), "bufferCapacity": self.buffer.maxlen,
            "bufferSeconds": round(buf[-1].t - buf[0].t, 2) if len(buf) > 1 else 0.0,
            "bufferTargetSeconds": CLIP_SECONDS, "frameWidth": self.frame_w, "frameHeight": self.frame_h,
            "incidentCooldown": round(cooldown, 1), "incidentPostRollSec": INCIDENT_POST_ROLL_SEC,
            "clipsWritten": self.clips_written, "rov": self.rov.status(),
            **self._state,
        }

    # ---------------------------------------------------------------- MJPEG
    def mjpeg(self):
        last = -1
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._stop.is_set() or
                                    (self._latest is not None and self._latest.seq != last), timeout=1.0)
                entry, stopped = self._latest, self._stop.is_set()
            if stopped and not self.is_active:
                return
            if entry is None or entry.seq == last:
                continue
            last = entry.seq
            yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " +
                   str(len(entry.jpeg)).encode() + b"\r\n\r\n" + entry.jpeg + b"\r\n")

    def latest_jpeg(self) -> bytes | None:
        return self._latest.jpeg if self._latest else None


def _defect_row(video_id: str, run_id: str, t: float, d: dict) -> DefectRecord:
    return DefectRecord(video_id=video_id, run_id=run_id, defect_type=d["type"], raw_class=d.get("rawClass"),
                        timestamp_sec=round(t, 3), confidence=d["confidence"], severity=d["severity"],
                        x=d["x"], y=d["y"], width=d["width"], height=d["height"])


def _obj_row(t: float, d: dict) -> tuple:
    """(t, cls, conf, x, y, w, h) for the sensor-DB object log."""
    return (round(t, 3), d.get("rawClass") or d["type"], d["confidence"], d["x"], d["y"], d["width"], d["height"])
