"""
TEST MODE - exercise the whole system without the BlueROV2 or a trained model.

When enabled:
  * VIDEO   - ffmpeg streams a chosen file, looped, at real-time rate as H.264 / MPEG-TS
              over UDP (BlueOS's camera default is UDP 5600). Live Ops connects to it through
              the same network capture path as a real ROV camera - it is a continuous
              broadcast: joining later means joining mid-stream, like the real thing.
  * SENSORS - tools/bluerov_sim.py sends ArduSub MAVLink (HEARTBEAT, VFR_HUD, Bar30, sonar ...)
              to the backend's real MAVLink listener. The operator sets the dive profile and
              can move the vehicle up/down while streaming.
  * TAGGING - every live session, clip and batch ingest made while Test Mode is on is stored
              with is_test=1, sensor samples carry source="SIM", and the UI shows TEST badges.
              Test data never appears in the real registry / segment reports and vice versa.
  * STAND-IN DEFECTS (optional) - map a class of a generic model (e.g. yolo11n "person") to
              DEFORMATION or CORROSION so the clip / alert / review / report workflow can be
              rehearsed before best.pt exists. Recorded detections keep their raw class and a
              standIn flag.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from config import BASE_DIR, MAVLINK_URL, TEST_STREAM_PORT, TEST_STREAM_URL, VIDEO_EXTS
from video_utils import ffmpeg_exe, probe

sys.path.insert(0, str(BASE_DIR.parent / "tools"))

DEFECT_TYPES = ("DEFORMATION", "CORROSION")


def _sim_target_url() -> str:
    """Where the simulator must send so the backend's MAVLink listener receives it."""
    u = MAVLINK_URL
    if u.startswith("udpin:"):
        host, port = u[len("udpin:"):].rsplit(":", 1)
        host = "127.0.0.1" if host in ("0.0.0.0", "") else host
        return f"udpout:{host}:{port}"
    if u.startswith("udpout:"):           # backend dials out -> the sim must listen there
        return "udpin:" + u[len("udpout:"):]
    return u


class TestMode:
    def __init__(self, rov):
        self.rov = rov
        self.enabled = False
        self.video_path: str | None = None
        self.video_name: str | None = None
        self.stand_in: dict[str, str] = {}
        self.target_depth = 120.5
        self.descent_rate = 1.0
        self.error: str | None = None
        self._ff: subprocess.Popen | None = None
        self._sim = None
        self._stop = threading.Event()
        self._sup: threading.Thread | None = None
        self.stream_restarts = 0
        self.started_at: float | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public
    @property
    def stream_url(self) -> str:
        return TEST_STREAM_URL

    def profile(self) -> dict:
        """Stored on every media recorded in Test Mode."""
        return {"standIn": dict(self.stand_in), "video": self.video_name, "simulatedRov": True,
                "targetDepth": self.target_depth}

    def profile_json(self) -> str:
        return json.dumps(self.profile())

    def start(self, video_path: str, target_depth: float = 120.5, descent_rate: float = 1.0,
              stand_in: dict | None = None, start_depth: float = 0.0, heading: float = 135.0,
              seabed: float = 200.0) -> dict:
        with self._lock:
            if self.enabled:
                self._stop_locked()
            p = Path(video_path)
            if not p.is_file():
                raise ValueError(f"Video not found: {video_path}")
            if p.suffix.lower() not in VIDEO_EXTS:
                raise ValueError(f"Unsupported video type '{p.suffix}'")
            if not ffmpeg_exe():
                raise RuntimeError("ffmpeg is required for Test Mode streaming (pip install imageio-ffmpeg)")
            if self.rov.state in ("CONNECTED", "STALE") and not self.rov.simulated:
                raise RuntimeError("A real BlueROV2 is connected - Test Mode would mix simulated and real data")
            self.stand_in = {}
            for cls, canon in (stand_in or {}).items():
                canon = str(canon).upper()
                if canon not in DEFECT_TYPES:
                    raise ValueError(f"Stand-in target must be one of {DEFECT_TYPES}")
                if cls.strip():
                    self.stand_in[cls.strip().lower()] = canon
            self.video_path, self.video_name = str(p), p.name
            self.target_depth, self.descent_rate = float(target_depth), max(0.1, float(descent_rate))
            self.error, self.stream_restarts = None, 0
            self._stop.clear()
            self.enabled = True
            self.started_at = time.time()
            # telemetry first, so the first frames already have a depth
            try:
                from bluerov_sim import BlueRovSim
                self._sim = BlueRovSim(_sim_target_url(), start_depth=start_depth, target=self.target_depth,
                                       rate=self.descent_rate, seabed=seabed, heading=heading).start()
                self.rov.simulated = True
            except Exception as e:      # pymavlink missing: video still works, telemetry stays offline
                self._sim = None
                self.error = f"ROV simulator unavailable: {e}"
            self._sup = threading.Thread(target=self._supervise, name="test-stream", daemon=True)
            self._sup.start()
            print(f"[TEST MODE] streaming {p.name} -> udp://127.0.0.1:{TEST_STREAM_PORT}; "
                  f"ROV sim -> {_sim_target_url()}; stand-in {self.stand_in or 'none'}")
            return self.status()

    def stop(self) -> dict:
        with self._lock:
            self._stop_locked()
            return self.status()

    def set_depth(self, depth: float, rate: float | None = None) -> dict:
        if not self.enabled or not self._sim:
            raise RuntimeError("Test Mode is not running")
        self.target_depth = max(0.0, float(depth))
        self._sim.target = self.target_depth
        if rate:
            self.descent_rate = self._sim.rate = max(0.1, float(rate))
        return self.status()

    def set_stand_in(self, mapping: dict) -> dict:
        clean = {}
        for cls, canon in (mapping or {}).items():
            canon = str(canon).upper()
            if canon not in DEFECT_TYPES:
                raise ValueError(f"Stand-in target must be one of {DEFECT_TYPES}")
            if cls.strip():
                clean[cls.strip().lower()] = canon
        self.stand_in = clean
        return self.status()

    def status(self) -> dict:
        ff = self._ff
        return {"enabled": self.enabled, "video": self.video_name, "videoPath": self.video_path,
                "streamUrl": self.stream_url, "streaming": bool(ff and ff.poll() is None),
                "streamRestarts": self.stream_restarts, "standIn": dict(self.stand_in),
                "targetDepth": self.target_depth, "descentRate": self.descent_rate,
                "simDepth": round(self._sim.depth, 2) if self._sim else None,
                "simRunning": self._sim is not None, "error": self.error,
                "uptimeSec": round(time.time() - self.started_at, 1) if self.enabled and self.started_at else 0}

    # ------------------------------------------------------------------ internals
    def _stop_locked(self):
        self._stop.set()
        self.enabled = False
        ff, self._ff = self._ff, None
        if ff and ff.poll() is None:
            ff.terminate()
            try:
                ff.wait(3)
            except subprocess.TimeoutExpired:
                ff.kill()
        if self._sim:
            self._sim.stop()
            self._sim = None
        self.rov.simulated = False

    def _cmd(self) -> list[str]:
        meta = probe(self.video_path)
        fps = meta.get("fps") or 30
        return [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-re", "-stream_loop", "-1", "-i", self.video_path,
                "-an", "-vf", "scale='min(1280,iw)':-2", "-c:v", "libx264", "-preset", "ultrafast",
                "-tune", "zerolatency", "-g", str(max(1, round(fps))), "-pix_fmt", "yuv420p", "-threads", "2",
                "-f", "mpegts", f"udp://127.0.0.1:{TEST_STREAM_PORT}?pkt_size=1316"]

    def _supervise(self):
        """Keeps the 'camera' broadcasting; restarts ffmpeg if it exits."""
        while not self._stop.is_set():
            try:
                self._ff = subprocess.Popen(self._cmd(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.PIPE)   # a camera runs at normal priority
            except Exception as e:
                self.error = f"Could not start the test stream: {e}"
                return
            while not self._stop.is_set() and self._ff and self._ff.poll() is None:
                self._stop.wait(0.5)
            if self._stop.is_set():
                break
            err = (self._ff.stderr.read() or b"").decode(errors="replace")[-300:] if self._ff and self._ff.stderr else ""
            self.stream_restarts += 1
            self.error = f"Test stream restarted ({self.stream_restarts}x){': ' + err.strip() if err.strip() else ''}"
            if self.stream_restarts > 5:
                self.error = "Test stream keeps failing: " + err.strip()
                return
            self._stop.wait(1.0)
