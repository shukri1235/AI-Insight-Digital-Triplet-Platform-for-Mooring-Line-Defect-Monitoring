"""Video helpers: hashing, metadata probing, and browser-playable encoding."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import cv2

# Codecs that Chrome/Edge/Firefox can play inside <video>
BROWSER_CODECS = {"avc1", "h264", "x264", "vp08", "vp80", "vp09", "vp90", "av01"}


def sha256_file(path: str | Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def probe(path: str | Path) -> dict:
    meta = {"fps": None, "frames": None, "duration_sec": None, "width": None, "height": None, "codec": None}
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return meta
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        fourcc = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        meta.update({
            "fps": round(fps, 3) if fps > 0 else None,
            "frames": int(frames) if frames > 0 else None,
            "duration_sec": round(frames / fps, 2) if fps > 0 and frames > 0 else None,
            "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None,
            "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None,
            "codec": "".join(chr((fourcc >> 8 * i) & 0xFF) for i in range(4)).strip("\x00 ").lower() or None,
        })
    finally:
        cap.release()
    return meta


def is_browser_playable(codec: str | None) -> bool:
    return bool(codec) and codec.lower() in BROWSER_CODECS


def ffmpeg_exe() -> str | None:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:  # pip install imageio-ffmpeg ships a static binary (handy on Windows)
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


_NVENC_OK: bool | None = None


def _low_priority_kwargs() -> dict:
    """Clip encoding is not latency-critical: run ffmpeg below the live loop's priority
    so it cannot steal CPU from capture/inference on the edge PC."""
    import os
    if os.name == "nt":
        return {"creationflags": 0x00004000}  # BELOW_NORMAL_PRIORITY_CLASS
    return {"preexec_fn": lambda: os.nice(10)}


def _encoder_args(exe: str) -> list[str]:
    """NVENC (dedicated encoder silicon - zero CPU, not used by YOLO) when present,
    otherwise x264 capped to 2 threads."""
    global _NVENC_OK
    if _NVENC_OK is None:
        try:
            r = subprocess.run([exe, "-v", "error", "-f", "lavfi", "-i", "color=s=256x256:d=0.1", "-c:v", "h264_nvenc",
                                "-f", "null", "-"], capture_output=True, timeout=15)
            _NVENC_OK = r.returncode == 0
        except Exception:
            _NVENC_OK = False
    if _NVENC_OK:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-threads", "2"]


def write_jpegs_to_mp4(jpegs: list[bytes], fps: float, out_path: Path) -> Path:
    """Encodes a list of JPEG frames to a browser-playable clip.

    Preferred: pipe the JPEG bytes straight into ffmpeg -> H.264 (no Python-side decode).
    Fallback: decode with OpenCV and try avc1 (.mp4) -> VP8 (.webm) -> mp4v (.mp4, NOT
    browser-playable; the old code always used mp4v, which is why clips would not play)."""
    fps = max(1.0, min(60.0, fps))
    exe = ffmpeg_exe()
    ffmpeg_error = "ffmpeg not found"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if exe:
        out_path = out_path.with_suffix(".mp4")
        cmd = [exe, "-y", "-loglevel", "error", "-threads", "2", "-f", "image2pipe", "-c:v", "mjpeg",
               "-framerate", f"{fps:.3f}", "-i", "-", *_encoder_args(exe), "-pix_fmt", "yuv420p",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-movflags", "+faststart", str(out_path)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, **_low_priority_kwargs())
        try:
            for jb in jpegs:
                proc.stdin.write(jb)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        err = proc.stderr.read().decode(errors="ignore")
        if proc.wait() == 0 and out_path.exists() and out_path.stat().st_size > 0:
            return out_path
        ffmpeg_error = err.strip()[:300]
        print(f"[CLIP] ffmpeg failed ({ffmpeg_error}); falling back to OpenCV writer")

    import numpy as np
    first = cv2.imdecode(np.frombuffer(jpegs[0], np.uint8), cv2.IMREAD_COLOR)
    h, w = first.shape[:2]
    for fourcc, ext in (("avc1", ".mp4"), ("VP80", ".webm"), ("mp4v", ".mp4")):
        target = out_path.with_suffix(ext)
        writer = cv2.VideoWriter(str(target), cv2.VideoWriter_fourcc(*fourcc), fps, (w, h))
        if not writer.isOpened():
            writer.release()
            continue
        for jb in jpegs:
            frame = cv2.imdecode(np.frombuffer(jb, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                writer.write(frame)
        writer.release()
        if target.exists() and target.stat().st_size > 0:
            if fourcc == "mp4v":
                print("[CLIP WARNING] Saved with mp4v - browsers cannot play this. Install ffmpeg or "
                      "`pip install imageio-ffmpeg` for H.264 clips.")
            return target
    raise RuntimeError(f"Could not write {out_path.name}: ffmpeg: {ffmpeg_error}; OpenCV writers also failed "
                       f"(check disk space / folder permissions, or install ffmpeg / imageio-ffmpeg)")


def make_browser_proxy(src: Path, dst: Path) -> Path | None:
    """Transcodes an upload whose codec browsers cannot decode (e.g. HEVC, mp4v) into H.264."""
    exe = ffmpeg_exe()
    if not exe:
        return None
    common = ["-an", "-pix_fmt", "yuv420p", "-vf", "scale='min(1280,iw)':-2", "-movflags", "+faststart", str(dst)]
    # NVENC first (a 4-hour HEVC dive transcodes many times faster on the GPU's encoder
    # block, which YOLO does not use), then CPU x264.
    for enc in (["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26"],
                ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26"]):
        cmd = [exe, "-y", "-loglevel", "error", "-hwaccel", "auto", "-i", str(src), *enc, *common]
        try:
            subprocess.run(cmd, check=True, timeout=6 * 3600, capture_output=True, **_low_priority_kwargs())
            if dst.exists() and dst.stat().st_size > 0:
                return dst
        except Exception as e:
            print(f"[PROXY] {enc[1]} failed: {str(e)[:120]}")
    return None


def quick_fingerprint(path: str | Path, chunk: int = 8 << 20) -> str:
    """size + SHA-256(first 8 MB + last 8 MB). Cheap pre-check for duplicates; the full
    SHA-256 remains the authority. The browser computes the same value before uploading."""
    import os
    size = os.path.getsize(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read(chunk))
        if size > chunk:
            f.seek(max(chunk, size - chunk))
            h.update(f.read(chunk))
    return f"{size}:{h.hexdigest()}"
