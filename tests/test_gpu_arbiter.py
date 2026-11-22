"""
GPU ARBITER (single-GPU sharing between Pipeline A and Pipeline B)

GA-1  With no live dive, a batch job runs as one full batch (whole GPU)
GA-2  While live is active, batch is chunked and a live frame never waits behind more
      than one small chunk (bounded live latency), and every batch frame is still processed
GA-3  Live and batch results are identical to direct inference (the arbiter only schedules)
"""
import threading
import time

import numpy as np
import pytest

import conftest  # noqa: F401  (environment + test doubles on sys.path)


class _MM:
    version = 1

    def create_instance(self):
        from ultralytics import YOLO
        return YOLO(conftest.RUN_DIR / "models" / "best.pt"), 1, "cpu", False


def _frame(red=False):
    f = np.full((540, 960, 3), (60, 30, 10), np.uint8)
    if red:
        f[220:300, 480:560] = (0, 0, 230)
    return f


@pytest.fixture()
def arbiter(monkeypatch):
    monkeypatch.setenv("FAKE_YOLO_LATENCY_MS", "20")
    from gpu_arbiter import GpuArbiter
    return GpuArbiter(_MM())


def test_ga_1_batch_gets_whole_gpu_without_live(arbiter):
    from ultralytics import CALLS
    from gpu_arbiter import BATCH
    p0 = CALLS["predict"]
    out = arbiter.infer([_frame()] * 16, 0.25, 0.45, BATCH, timeout=10)
    assert len(out) == 16 and CALLS["predict"] - p0 == 1


def test_ga_2_live_latency_bounded_while_batch_runs(arbiter, evidence):
    from gpu_arbiter import BATCH, LIVE
    stop = threading.Event()
    waits = []

    def live_loop():                       # a 30 fps camera, latest-frame-wins
        while not stop.is_set():
            t0 = time.perf_counter()
            arbiter.infer([_frame(True)], 0.1, 0.45, LIVE, timeout=5)
            waits.append((time.perf_counter() - t0) * 1000)
            stop.wait(max(0.0, 1 / 30 - (time.perf_counter() - t0)))
    th = threading.Thread(target=live_loop)
    th.start()
    time.sleep(0.3)
    t0 = time.perf_counter()
    out = arbiter.infer([_frame()] * 120, 0.25, 0.45, BATCH, timeout=60)     # 120 frames x 20 ms = 2.4 s of GPU work
    batch_s = time.perf_counter() - t0
    stop.set(); th.join()
    st = arbiter.stats()
    p95 = sorted(waits)[int(len(waits) * 0.95)]
    assert len(out) == 120
    # live round trip = own 20 ms + at most one 2-frame chunk (40 ms) + scheduling
    assert p95 < 90, f"live p95 {p95:.0f} ms"
    assert st["liveWaitMsP95"] < 60, st
    evidence["GA-2"] = {"live_roundtrip_p95_ms": round(p95, 1), "live_wait_p95_ms": st["liveWaitMsP95"],
                        "batch_120_frames_s": round(batch_s, 2), "naive_worst_case_live_wait_ms": 120 * 20,
                        "live_share_pct": st["liveSharePct"], "batch_share_pct": st["batchSharePct"]}


def test_ga_3_results_unchanged_by_scheduling(arbiter):
    from ultralytics import YOLO
    from gpu_arbiter import BATCH, LIVE
    from model_manager import parse_result
    m = YOLO(conftest.RUN_DIR / "models" / "best.pt")
    direct = parse_result(m.predict(_frame(True), conf=0.1)[0], m.names)
    assert direct and direct[0]["type"] == "DEFORMATION"
    assert arbiter.infer([_frame(True)], 0.1, 0.45, LIVE)[0] == direct
    assert arbiter.infer([_frame(True)] * 3, 0.1, 0.45, BATCH)[2] == direct
