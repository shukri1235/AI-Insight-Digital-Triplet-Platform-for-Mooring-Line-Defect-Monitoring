"""
V&V test harness.

Every test runs against an isolated, throw-away environment (temp DBs, media, clips,
models) - your real databases are never touched. Detection uses the colour-blob test
double in tests/fakes so every scenario has known ground truth. ROV telemetry comes from
tools/bluerov_sim.py speaking real MAVLink over UDP to the backend's BlueROV2 link, so
the same ingest path as the vehicle is exercised (needs: pip install pymavlink).

Run:   pip install pytest   then   python -m pytest tests -v
Fast:  python -m pytest tests -v -m "not slow"
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FAKES = Path(__file__).resolve().parent / "fakes"

# ---- isolated environment (must be set BEFORE the backend is imported) ----------
RUN_DIR = Path(tempfile.mkdtemp(prefix="mooring_vv_"))
MAV_PORT = 14650 + os.getpid() % 300
ENV = {
    "FYP_MEDIA_DIR": str(RUN_DIR / "managed_media"),
    "FYP_MODELS_DIR": str(RUN_DIR / "models"),
    "FYP_DB_PATH": str(RUN_DIR / "vv.db"),
    "FYP_LOGS_DIR": str(RUN_DIR / "logs"),
    "FYP_SETTINGS_FILE": str(RUN_DIR / "settings.json"),
    "FYP_REPORT_TEMPLATES_DIR": str(RUN_DIR / "report_templates"),
    "FYP_CONFIG": str(RUN_DIR / "config.toml"),          # never read a developer's own config.toml
    "FYP_UI_DIR": str(RUN_DIR / "ui_dist"),              # a stand-in built UI (UI-14)
    "FYP_DEFAULT_MODEL": "best.pt",
    "FYP_MAVLINK_URL": f"udpin:127.0.0.1:{MAV_PORT}",
    "FYP_TEST_STREAM_PORT": str(15600 + os.getpid() % 300),
    "GEMINI_API_KEY": "test-key",
    "GEMINI_COOLDOWN_SEC": "30",
    "FAKE_YOLO_LATENCY_MS": "8",
}
# a developer's own settings never leak in: their FYP_* / Gemini variables, app-root .env and (Windows) registry key.
# The model acceptance inputs (set by tools/acceptance.bat) are test inputs, not app settings: they are kept.
ACCEPTANCE_VARS = {"FYP_REAL_WEIGHTS", "FYP_VAL_DATA", "FYP_VAL_SPLIT", "FYP_CLEAN_FRAMES", "FYP_TRIGGER",
                   "FYP_MIN_MAP50", "FYP_MIN_DEFORM_RECALL", "FYP_MAX_FALSE_ALARM"}
for _k in [k for k in os.environ if (k.startswith("FYP_") or k.startswith("GEMINI_") or k == "GOOGLE_API_KEY")
           and k not in ENV and k not in ACCEPTANCE_VARS]:
    os.environ.pop(_k)
os.environ.update(ENV)
(RUN_DIR / "ui_dist" / "assets").mkdir(parents=True, exist_ok=True)
(RUN_DIR / "ui_dist" / "index.html").write_text("<!doctype html><title>Mooring Twin</title><div id=root></div>"
                                               "<script type=module src=/assets/app.js></script>", encoding="utf-8")
(RUN_DIR / "ui_dist" / "assets" / "app.js").write_text("console.log('ui')", encoding="utf-8")
(RUN_DIR / "models").mkdir(parents=True, exist_ok=True)
(RUN_DIR / "models" / "best.pt").write_bytes(b"test-double-weights")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(FAKES))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))

MEDIA = RUN_DIR / "media"
MEDIA.mkdir(exist_ok=True)

from vv_media import COLORS, GT, make_dive  # noqa: E402,F401

import config as _config  # noqa: E402

_config.ENV_FILES = ()                                  # tests that need a .env file point this at a temp file
_config._from_windows_registry = lambda name: None      # the key saved with setx on this PC is not the test's key


def wait_for(pred, timeout=30.0, step=0.2, what="condition"):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = pred()
        if r:
            return r
        time.sleep(step)
    raise TimeoutError(f"timed out after {timeout}s waiting for {what}")


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    import main
    with TestClient(main.app) as c:
        wait_for(lambda: c.get("/api/model/status").json()["is_loaded"], 20, what="model boot")
        # Evidence for the offline placeholder: before any vehicle talks, the link says NO_ROV
        st = c.get("/api/rov/status").json()
        BOOT_ROV.update(state=st["state"], telemetry=st["telemetry"])
        yield c
        main.live.stop()


BOOT_ROV: dict = {}


@pytest.fixture(scope="session")
def rov(client):
    """A simulated BlueROV2 (ArduSub MAVLink over UDP) holding 120.5 m on ML-01-NORTH."""
    from bluerov_sim import BlueRovSim
    sim = BlueRovSim(f"udpout:127.0.0.1:{MAV_PORT}", start_depth=120.5, target=120.5, rate=500, heading=135.0).start()
    wait_for(lambda: client.get("/api/rov/status").json()["state"] == "CONNECTED", 10, what="ROV heartbeat")
    set_depth(client, sim, 120.5)
    yield sim
    sim.stop()


def set_depth(client, sim, depth: float, timeout=5.0):
    """Moves the simulated vehicle and waits until the backend reports the new depth."""
    sim.depth = sim.target = depth
    return wait_for(lambda: (lambda t: t if t and t.get("depth") is not None and abs(t["depth"] - depth) < 0.05 else None)(
        client.get("/api/rov/status").json()["telemetry"]), timeout, 0.1, f"ROV depth {depth}")


@pytest.fixture(scope="session")
def app_module(client):
    import main
    return main


@pytest.fixture(scope="session")
def scenario_dive():
    return make_dive(MEDIA / "scenario_dive_30fps.mp4")


def upload(client, path: Path, name: str, line_id: str = "ML-01-NORTH", conf: float = 0.25) -> dict:
    """Pipeline B managed upload. The threshold is mandatory (chosen before processing)."""
    with open(path, "rb") as f:
        r = client.post(f"/api/videos/upload-stream?filename={path.name}&name={name}&line_id={line_id}&conf={conf}",
                        content=f.read())
    assert r.status_code == 200, r.text
    return r.json()


def wait_completed(client, vid: str, timeout=120) -> dict:
    def done():
        v = client.get(f"/api/videos/{vid}").json()
        if v["status"] == "FAILED":
            raise AssertionError(f"batch failed: {v['error']}")
        return v if v["status"] == "COMPLETED" else None
    return wait_for(done, timeout, 0.5, f"video {vid} COMPLETED")


@pytest.fixture(scope="session")
def processed_dive(client, scenario_dive):
    """The scenario dive uploaded through Pipeline B and fully processed at 0.25."""
    res = upload(client, scenario_dive, "ML-01 Scenario Dive")
    vid = res["video"]["id"]
    return wait_completed(client, vid)


# ---- evidence log: tests append measurements; the report generator reads it ------
EVIDENCE = ROOT / "tests" / "results" / "evidence.json"


@pytest.fixture(scope="session")
def evidence():
    data: dict = {}
    yield data
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else {}
    old.update(data)
    EVIDENCE.write_text(json.dumps(old, indent=2, default=str))


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(RUN_DIR, ignore_errors=True)


def run_live_until_incident(client, sim, source: str, depth: float, line_id: str = "ML-01-NORTH",
                            trigger_conf: float = 0.60, timeout: float = 40.0) -> dict:
    """Moves the (simulated) BlueROV2 to `depth`, connects a live session, records the WS
    stream and capture health until the incident clip is SAVED, then disconnects."""
    import main
    rec = {"states": [], "incidents": [], "health": []}
    set_depth(client, sim, depth)
    with client.websocket_connect("/ws/telemetry") as ws:
        assert ws.receive_json()["type"] == "connection_status"
        t_start = time.time()
        r = client.post("/api/live/start", json={"source": source, "line_id": line_id, "trigger_conf": trigger_conf,
                                                 "detection_mode": "ARMED", "recording": True})
        assert r.status_code == 200, r.text
        rec["start_response"] = r.json()
        saved = None
        while time.time() - t_start < timeout:
            m = ws.receive_json()
            m["_wall"] = time.time() - t_start
            if m["type"] == "live_state":
                rec["states"].append({k: m.get(k) for k in ("status", "t", "detections", "telemetry", "captureFps",
                                                              "bufferFrames", "bufferCapacity", "lateFrames",
                                                              "sourceFps", "_wall", "triggerConf")})
                rec.setdefault("late_timeline", []).append((time.monotonic(), main.live.late_frames,
                                                            len(main.live.frame_intervals) and max(list(main.live.frame_intervals)[-10:])))
                if len(rec["states"]) % 15 == 0:
                    rec["health"].append(main.live.capture_health())
            elif m["type"] == "incident":
                rec["incidents"].append(m)
                if m["stage"] == "triggered":
                    rec["late_at_trigger"] = main.live.late_frames
                    rec["t_trigger"] = time.monotonic()
                if m["stage"] in ("saved", "failed"):
                    saved = m
                    rec["late_after_save"] = main.live.late_frames
                    rec["t_saved"] = time.monotonic()
                    rec["intervals_ms"] = [round(x * 1000, 1) for x in list(main.live.frame_intervals)]
                    break
        rec["buffer_bytes"] = sum(len(e.jpeg) for e in list(main.live.buffer))
        rec["buffer_len"] = len(main.live.buffer)
        rec["buffer_capacity"] = main.live.buffer.maxlen
        rec["capture_health"] = main.live.capture_health()
        rec["clip_write_ms"] = main.live.last_clip_write_ms
        rec["session_id"] = main.live.session_id
        time.sleep(1.0)  # keep streaming briefly after the write
        rec["capture_health_after"] = main.live.capture_health()
        rec["frame_log"] = list(main.live.frame_log)
        rec["nominal_s"] = 1.0 / main.live.source_fps
        main.live.flush_db()
        client.post("/api/live/stop")
    rec["saved"] = saved
    return rec


@pytest.fixture(scope="session")
def live_incident(client, rov, processed_dive):
    """Scenario 1 executed once: ROV at 120.5 m on ML-01-NORTH passes the warped link."""
    rec = run_live_until_incident(client, rov, f"video:{processed_dive['id']}", depth=120.5)
    assert rec["saved"] and rec["saved"]["stage"] == "saved", rec["incidents"]
    rec["clip"] = client.get(f"/api/videos/{rec['saved']['clipId']}").json()
    return rec
