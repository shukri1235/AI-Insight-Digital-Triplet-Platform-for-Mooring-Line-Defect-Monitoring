"""
GPU arbiter - single-GPU scheduling between Pipeline A (live) and Pipeline B (batch).

One thread owns the YOLO model and the GPU. Work arrives as jobs on a priority queue:

    priority 0  LIVE   one frame, latest-frame-wins upstream
    priority 1  BATCH  mini-batches of sampled frames / re-analysis runs

Rules:
  * A waiting LIVE job is always served before any BATCH work.
  * While a live dive is active (a live job seen in the last ARBITER_LIVE_RECENT_SEC),
    batch jobs are executed in chunks of ARBITER_BATCH_CHUNK_WHEN_LIVE frames and the
    queue is re-checked between chunks, so a live frame never waits behind more than
    one small chunk. With no live dive, batch gets the whole GPU in full batches.
  * One model instance, one CUDA context -> no VRAM duplication, no contention inside
    the driver, and weight hot-swaps happen in exactly one place.

This is how a single edge GPU is shared deterministically; in an enterprise deployment
the same contract would be met by separate containers on separate GPUs.
"""
from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import deque

from config import ARBITER_BATCH_CHUNK_WHEN_LIVE, ARBITER_LIVE_RECENT_SEC
from model_manager import ModelManager, parse_result

LIVE, BATCH = 0, 1


class _Job:
    __slots__ = ("priority", "frames", "conf", "iou", "event", "result", "error", "submitted", "started")

    def __init__(self, priority, frames, conf, iou):
        self.priority, self.frames, self.conf, self.iou = priority, frames, conf, iou
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.submitted = time.perf_counter()
        self.started = None


class GpuArbiter:
    def __init__(self, mm: ModelManager):
        self.mm = mm
        self._q: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._model, self._ver, self._device, self._half = None, -1, "cpu", False
        self._last_live = 0.0
        self._lock = threading.Lock()
        # metrics
        self._busy = {LIVE: deque(maxlen=4000), BATCH: deque(maxlen=4000)}   # (end_time, busy_seconds)
        self._live_wait_ms: deque[float] = deque(maxlen=500)
        self._live_infer_ms: deque[float] = deque(maxlen=500)
        self.frames_done = {LIVE: 0, BATCH: 0}
        threading.Thread(target=self._loop, name="gpu-arbiter", daemon=True).start()

    # ------------------------------------------------------------------ API
    @property
    def live_active(self) -> bool:
        return time.monotonic() - self._last_live < ARBITER_LIVE_RECENT_SEC

    @property
    def model_names(self) -> dict:
        return dict(self._model.names) if self._model is not None else {}

    def infer(self, frames: list, conf: float, iou: float, priority: int, timeout: float | None = None) -> list[list[dict]]:
        """Blocking call: returns parsed detections per frame."""
        if priority == LIVE:
            self._last_live = time.monotonic()
        job = _Job(priority, frames, conf, iou)
        self._q.put((priority, next(self._seq), job))
        if not job.event.wait(timeout):
            raise TimeoutError("GPU arbiter timeout")
        if job.error:
            raise job.error
        return job.result

    # ------------------------------------------------------------------ worker
    def _ensure_model(self):
        if self.mm.version != self._ver:
            self._model, self._ver, self._device, self._half = self.mm.create_instance()
            print(f"[ARBITER] model {'ready' if self._model else 'unavailable'} (v{self._ver}, {self._device})")
        if self._model is None:
            raise RuntimeError("No YOLO weights loaded")
        return self._model

    def _run(self, frames, job):
        model = self._ensure_model()
        t0 = time.perf_counter()
        res = model.predict(frames, conf=job.conf, iou=job.iou, device=self._device, half=self._half, verbose=False)
        dt = time.perf_counter() - t0
        self._busy[job.priority].append((time.monotonic(), dt))
        self.frames_done[job.priority] += len(frames)
        return [parse_result(r, model.names) for r in res], dt

    def _loop(self):
        while True:
            _, _, job = self._q.get()
            job.started = time.perf_counter()
            try:
                if job.priority == LIVE:
                    self._live_wait_ms.append((job.started - job.submitted) * 1000)
                    out, dt = self._run(job.frames, job)
                    self._live_infer_ms.append(dt * 1000)
                    job.result = out
                else:
                    out = []
                    i = 0
                    while i < len(job.frames):
                        chunk = ARBITER_BATCH_CHUNK_WHEN_LIVE if self.live_active else len(job.frames)
                        part = job.frames[i:i + chunk]
                        res, _ = self._run(part, job)
                        out.extend(res)
                        i += len(part)
                        self._serve_waiting_live()   # !!! live frames jump in between chunks
                    job.result = out
            except Exception as e:
                job.error = e
            finally:
                job.event.set()

    def _serve_waiting_live(self):
        """Run any LIVE jobs that queued up while a batch chunk was on the GPU."""
        held = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item[0] == LIVE:
                job = item[2]
                job.started = time.perf_counter()
                try:
                    self._live_wait_ms.append((job.started - job.submitted) * 1000)
                    job.result, dt = self._run(job.frames, job)
                    self._live_infer_ms.append(dt * 1000)
                except Exception as e:
                    job.error = e
                finally:
                    job.event.set()
            else:
                held.append(item)
        for item in held:
            self._q.put(item)

    # ------------------------------------------------------------------ metrics
    def stats(self, window: float = 10.0) -> dict:
        now = time.monotonic()
        busy = {p: sum(b for t, b in self._busy[p] if now - t <= window) for p in (LIVE, BATCH)}
        tot = busy[LIVE] + busy[BATCH]
        w = sorted(self._live_wait_ms)
        pct = lambda arr, q: round(arr[min(len(arr) - 1, int(q * len(arr)))], 1) if arr else None
        return {
            "device": self._device,
            "liveActive": self.live_active,
            "utilisationPct": round(min(100.0, tot / window * 100), 1),
            "liveSharePct": round(busy[LIVE] / tot * 100, 1) if tot else 0.0,
            "batchSharePct": round(busy[BATCH] / tot * 100, 1) if tot else 0.0,
            "liveWaitMsP50": pct(w, 0.5), "liveWaitMsP95": pct(w, 0.95), "liveWaitMsMax": round(w[-1], 1) if w else None,
            "liveInferMsP50": pct(sorted(self._live_infer_ms), 0.5),
            "queueDepth": self._q.qsize(),
            "framesLive": self.frames_done[LIVE], "framesBatch": self.frames_done[BATCH],
        }

    def reset_stats(self):
        self._live_wait_ms.clear()
        self._live_infer_ms.clear()
