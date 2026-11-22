"""Synthetic ROV footage with known ground truth (shared by pytest and the E2E server).
Uses ffmpeg on PATH, or the copy bundled with imageio-ffmpeg (backend requirement) - so Windows needs no extra install."""
import shutil
import subprocess
from pathlib import Path


def ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        raise RuntimeError("ffmpeg not found: pip install imageio-ffmpeg (backend requirements) or put ffmpeg on PATH")

# Ground truth of the synthetic scenario dive (960x540, 30 fps, 20 s)
GT = {
    "deformation": {"t": (8.0, 11.0), "box": (480, 220, 80, 80), "conf": 0.88},
    "pitting": {"t": (8.0, 12.0), "box": (600, 250, 30, 30), "conf": 0.32},
    "corrosion": {"t": (14.0, 17.0), "box": (200, 330, 120, 70), "conf": 0.80},
    "mooringline": {"t": (2.0, 4.0), "box": (100, 100, 40, 40), "conf": 0.70},
}
COLORS = {"deformation": "0xE60000", "pitting": "0xE6E600", "corrosion": "0x00E6E6", "mooringline": "0x00C800"}


def _boxes_filter(w=960, h=540):
    parts = ["noise=alls=10:allf=t"]
    for name, g in GT.items():
        x, y, bw, bh = g["box"]
        t0, t1 = g["t"]
        parts.append(f"drawbox=x={x}:y={y}:w={bw}:h={bh}:color={COLORS[name]}@1:t=fill:enable='between(t,{t0},{t1 - 0.001})'")
    return ",".join(parts)


def make_dive(path: Path, fps: int = 30, seconds: int = 20, size=(960, 540), codec="libx264", boxes=True) -> Path:
    if path.exists():
        return path
    vf = _boxes_filter(*size) if boxes else "noise=alls=10:allf=t"
    cmd = [ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i",
           f"color=c=0x0a1e3c:s={size[0]}x{size[1]}:r={fps}:d={seconds}", "-vf", vf]
    cmd += (["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"] if codec == "libx264"
            else ["-c:v", codec, "-q:v", "3"])
    subprocess.run(cmd + [str(path)], check=True)
    return path


def make_tlog(path: Path, seconds: float, depth_fn, start_wall: float = 1_760_000_000.0, hz: float = 10.0,
              heading: float = 200.0, temp_c: float = 9.5) -> Path:
    """Writes a BlueROV2-style telemetry log (.tlog: 8-byte big-endian µs timestamp +
    raw MAVLink frame, as QGroundControl / BlueOS record it) with a known depth profile."""
    import os
    import struct
    os.environ.setdefault("MAVLINK20", "1")
    from pymavlink.dialects.v20 import ardupilotmega as mav
    m = mav.MAVLink(None, srcSystem=1, srcComponent=1)
    with open(path, "wb") as f:
        n = int(seconds * hz) + 1
        for i in range(n):
            t = i / hz
            ts = struct.pack(">Q", int((start_wall + t) * 1e6))
            d = depth_fn(t)
            msgs = [mav.MAVLink_heartbeat_message(12, 3, 128 | 81, 2, 4, 3),
                    mav.MAVLink_vfr_hud_message(0, 0, int(heading), 0, -d, 0.0),
                    mav.MAVLink_scaled_pressure2_message(int(t * 1000), 1013.25 + d * 100.5, 0, int(temp_c * 100), 0)]
            for msg in msgs:
                f.write(ts + msg.pack(m))
    return path
