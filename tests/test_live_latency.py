"""
LAT-1  Live picture delay from the stream decoder. FFmpeg decodes one frame per thread in parallel and every thread
       adds a frame of delay; OpenCV's default is one thread per CPU. The live stream is opened with
       live.decoder_threads (default 2). Measured on a real H.264 / MPEG-TS UDP stream: frame k shows gray 2*(k mod 128),
       so the delay of every received frame is known.
"""
import statistics
import subprocess
import time

import cv2
import numpy as np
import pytest

FPS = 30


def _lag(port: int, params: list[int], seconds: float = 8.0) -> tuple[float, int]:
    from vv_media import ffmpeg
    src = subprocess.Popen([ffmpeg(), "-v", "error", "-re", "-f", "lavfi", "-i",
                            f"color=c=black:s=640x360:r={FPS}:d={seconds + 5},geq=lum='2*mod(N,128)':cb=128:cr=128",
                            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "30",
                            "-pix_fmt", "yuv420p", "-f", "mpegts", f"udp://127.0.0.1:{port}?pkt_size=1316"])
    t0 = time.monotonic()
    try:
        cap = cv2.VideoCapture(f"udp://127.0.0.1:{port}?overrun_nonfatal=1&fifo_size=5000000", cv2.CAP_FFMPEG, params)
        lags, prev, wraps = [], None, 0
        while time.monotonic() - t0 < seconds:
            ok, f = cap.read()
            if not ok:
                break
            now = time.monotonic()
            g = int(round(float(np.median(f[100:260, 200:440, 1])) / 2))
            if prev is not None and g < prev - 64:
                wraps += 1
            prev = g
            lags.append(now - t0 - (wraps * 128 + g) / FPS)   # + a constant (stream start), the same for every run
        cap.release()
    finally:
        src.kill()
        src.wait()
    steady = lags[len(lags) // 3:]
    return statistics.median(steady), len(lags)


@pytest.mark.slow
def test_lat_1_few_decoder_threads_cut_the_picture_delay(evidence):
    import live_stream
    from config import LIVE_DECODER_THREADS
    assert LIVE_DECODER_THREADS == 2 and live_stream.decoder_params() == [cv2.CAP_PROP_N_THREADS, 2]
    many, n_many = _lag(5741, [cv2.CAP_PROP_N_THREADS, 16])       # OpenCV's default on a 16-thread PC
    app, n_app = _lag(5742, live_stream.decoder_params())
    one, _ = _lag(5743, [cv2.CAP_PROP_N_THREADS, 1])
    assert n_many > 100 and n_app > 100
    saved = many - app
    assert saved > 0.3, f"16 threads {many:.3f} s vs 2 threads {app:.3f} s"   # ~14 frames at 30 fps = 0.47 s
    assert app - one < 0.1, "2 threads add at most about one frame over a single thread"
    evidence["LAT-1"] = {"delay_saved_s_vs_16_threads": round(saved, 3), "two_vs_one_thread_s": round(app - one, 3),
                         "relative_delay_s": {"16": round(many, 3), "2": round(app, 3), "1": round(one, 3)}}


def test_lat_2_live_network_streams_open_with_few_decoder_threads(monkeypatch):
    import live_stream
    seen = []

    class FakeCap:
        def __init__(self, *a):
            seen.append(a)

        def isOpened(self):
            return False
    monkeypatch.setattr(live_stream.cv2, "VideoCapture", FakeCap)
    w = live_stream.LiveStreamWorker.__new__(live_stream.LiveStreamWorker)
    w.source, w.source_kind = "rtsp://192.168.2.2:8554/", "network"
    w._resolve_source()
    assert seen[-1] == ("rtsp://192.168.2.2:8554/", cv2.CAP_FFMPEG, [cv2.CAP_PROP_N_THREADS, 2])
