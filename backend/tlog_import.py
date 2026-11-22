"""
Attach a BlueROV2 telemetry log (.tlog, as written by QGroundControl, BlueOS or this
app) to a historical dive video, so batch-processed dives get REAL depth / heading /
temperature instead of nothing.

Alignment: the video's t=0 corresponds to `log_start + offset_sec`. If no offset is
given we try the MP4's `creation_time` tag (camera clock); if that falls outside the
log, offset 0 (video and log started together) is assumed and reported.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from datetime import datetime

os.environ.setdefault("MAVLINK20", "1")

import sensors_db
from rov_link import ARDUSUB_MODES, PYMAVLINK, mavutil
from video_utils import ffmpeg_exe

RESAMPLE_HZ = 5.0


def _creation_epoch(video_path: str) -> float | None:
    import shutil
    probe = shutil.which("ffprobe")
    exe = ffmpeg_exe()
    if not probe and exe:
        sibling = os.path.join(os.path.dirname(exe), "ffprobe" + (".exe" if os.name == "nt" else ""))
        probe = sibling if os.path.exists(sibling) else None
    if not probe:
        return None  # imageio-ffmpeg ships no ffprobe -> caller falls back to offset 0
    try:
        out = subprocess.run([probe, "-v", "error", "-show_entries", "format_tags=creation_time", "-of", "json",
                              video_path], capture_output=True, encoding="utf-8", errors="replace", timeout=20).stdout
        ct = json.loads(out or "{}").get("format", {}).get("tags", {}).get("creation_time")
        if ct:
            return datetime.fromisoformat(ct.replace("Z", "+00:00")).timestamp()
    except Exception:
        pass
    return None


def read_tlog(path: str) -> list[dict]:
    """Decodes a .tlog into time-stamped readings (wall time from the tlog record)."""
    if not PYMAVLINK:
        raise RuntimeError("pymavlink is not installed (pip install pymavlink)")
    if not os.path.isfile(path):          # mavutil would otherwise treat it as a serial port name
        raise ValueError(f"tlog file not found: {path}")
    # Opened as a log file directly: mavlink_connection() guesses from the name, and a Windows path ("C:\\...")
    # whose extension is not .tlog is taken for a network address ("host:port").
    mavutil.set_dialect("ardupilotmega")
    m = mavutil.mavlogfile(path)
    try:   # always closed: Windows cannot replace or delete a file that is still open
        return _decode(m)
    finally:
        try:
            m.close()
        except Exception:  # noqa: BLE001
            pass


def _decode(m) -> list[dict]:
    state: dict = {}
    rows: list[dict] = []
    next_t = None
    while True:
        msg = m.recv_match()
        if msg is None:
            break
        mt = msg.get_type()
        ts = getattr(msg, "_timestamp", None)
        if ts is None:
            continue
        if mt == "VFR_HUD":
            state["depth"] = max(0.0, -float(msg.alt)); state["heading"] = float(msg.heading) % 360
            state["climb"] = float(msg.climb); state["_vfr"] = True
        elif mt == "ATTITUDE":
            state["roll"] = math.degrees(msg.roll); state["pitch"] = math.degrees(msg.pitch)
        elif mt == "SCALED_PRESSURE2":
            state["pressure_hpa"] = float(msg.press_abs); state["temperature"] = msg.temperature / 100.0
            if not state.get("_vfr"):
                state["depth"] = max(0.0, (msg.press_abs * 100 - 101325) / (1025 * 9.80665))
        elif mt == "SYS_STATUS" and msg.voltage_battery not in (0, 65535):
            state["battery_v"] = msg.voltage_battery / 1000.0
            if msg.battery_remaining >= 0:
                state["battery_pct"] = float(msg.battery_remaining)
        elif mt == "RANGEFINDER":
            state["altitude"] = float(msg.distance)
        elif mt == "HEARTBEAT" and msg.type == 12:
            state["armed"] = bool(msg.base_mode & 128); state["mode"] = ARDUSUB_MODES.get(msg.custom_mode)
        else:
            continue
        if "depth" not in state:
            continue
        if next_t is None or ts >= next_t - 1e-3:      # resample to RESAMPLE_HZ on a fixed grid (no drift)
            rows.append({"wall_time": ts, **{f: state.get(f) for f in sensors_db.FIELDS}})
            step = 1.0 / RESAMPLE_HZ
            next_t = ts + step if next_t is None or ts - next_t >= step else next_t + step
    return rows


def attach(video_id: str, video_path: str, duration: float | None, tlog_path: str,
           offset_sec: float | None = None) -> dict:
    rows = read_tlog(tlog_path)
    if not rows:
        raise ValueError("No depth telemetry found in this .tlog")
    log_start, log_end = rows[0]["wall_time"], rows[-1]["wall_time"]
    method = "manual offset"
    if offset_sec is None:
        created = _creation_epoch(video_path)
        if created is not None and log_start - 5 <= created <= log_end:
            offset_sec, method = created - log_start, "video creation_time"
        else:
            offset_sec, method = 0.0, "assumed simultaneous start (no usable creation_time)"
    t0 = log_start + offset_sec
    end = duration if duration else (log_end - t0)
    out = []
    for r in rows:
        t = r["wall_time"] - t0
        if -0.05 <= t <= end + 0.5:     # no pre-roll rows: clamping them to 0 would stack stale readings at t=0
            out.append({"media_id": video_id, "t": round(max(0.0, t), 3), "source": "TLOG", **r})
    sensors_db.delete_samples(video_id)
    sensors_db.insert_samples(out)
    return {"samples": len(out), "offsetSec": round(offset_sec, 3), "alignment": method,
            "logDurationSec": round(log_end - log_start, 1),
            "coverage": round(min(1.0, (out[-1]["t"] - out[0]["t"]) / end), 3) if out and end else 0.0}
