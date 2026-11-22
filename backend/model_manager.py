"""
YOLO weight resolution, loading, device selection and detection parsing.

WHY best.pt USED TO FAIL
------------------------
1. Path resolution used os.getcwd(). Starting the server from any folder other than
   the backend folder meant models/best.pt was never found.
2. When the file was not found, the code fell back to the bare name "best.pt".
   Ultralytics treats an unknown bare *.pt name as a GitHub release asset and tries to
   DOWNLOAD it - which fails because best.pt is your own file, not an official asset.
3. Ultralytics training writes weights to runs/detect/<run>/weights/best.pt, not to
   models/. Nothing searched there.
4. YOLO(path) only loads onto the CPU. The model is moved to the GPU on the FIRST
   predict() call - i.e. during playback. If the installed PyTorch build has no kernels
   for the GPU (an RTX 50-series / Blackwell card needs a CUDA 12.8+ build, sm_120),
   loading "succeeds" and inference then crashes mid-stream.

This module fixes all four: cwd-independent search across models/ and runs/,
no silent download for custom names, and a warm-up inference at load time so a
CUDA problem is reported when you click LOAD (with automatic CPU fallback).
"""
from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from config import (BASE_DIR, CLASS_ALIASES, CRITICAL_CONF, DEFAULT_MODEL, DEVICE, FALLBACK_MODEL,
                    MODELS_DIR, USE_HALF)

try:
    from ultralytics import YOLO
    ULTRALYTICS_AVAILABLE = True
except Exception as _e:  # ImportError or a broken torch install
    YOLO = None
    ULTRALYTICS_AVAILABLE = False
    print(f"[YOLO WARNING] ultralytics unavailable: {_e}")

# Official Ultralytics asset names (safe to auto-download)
_OFFICIAL_ASSET = re.compile(r"^yolo(v?\d+)[nsmlx]?(-(seg|pose|obb|cls|world|worldv2))?\.pt$", re.I)
_SEARCH_ROOTS = [BASE_DIR, BASE_DIR.parent]


# ---------------------------------------------------------------------------
# Class mapping / detection parsing
# ---------------------------------------------------------------------------

def canonical_class(raw_name: str) -> str | None:
    """Maps a raw model class name to CORROSION / DEFORMATION, or None if it is not a defect."""
    key = re.sub(r"[^A-Z]", "", str(raw_name).upper())
    for canon, aliases in CLASS_ALIASES.items():
        if any(a in key for a in aliases):
            return canon
    return None


HIGH, LOW = "High confidence", "Low confidence"


def severity_for(canon: str | None, conf: float) -> str:
    """The label states how certain the model is, never how severe the defect is (one rule for both classes)."""
    if canon in ("DEFORMATION", "CORROSION"):
        return HIGH if conf >= CRITICAL_CONF else LOW
    return "Info"


def parse_result(result, names: dict) -> list[dict]:
    """Converts one Ultralytics Results object to the JSON shape the React overlay uses.
    Coordinates are centre-based percentages of the frame."""
    detections: list[dict] = []
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return detections
    h, w = result.orig_shape[:2]
    xyxy = boxes.xyxy.cpu().numpy()
    confs = boxes.conf.cpu().numpy()
    clss = boxes.cls.cpu().numpy().astype(int)
    for i, ((x1, y1, x2, y2), conf, cls_id) in enumerate(zip(xyxy, confs, clss)):
        raw = str(names.get(int(cls_id), cls_id))
        canon = canonical_class(raw)
        bw, bh = x2 - x1, y2 - y1
        detections.append({
            "id": f"det-{cls_id}-{i}",
            "type": canon or raw.upper(),
            "rawClass": raw,
            "isDefect": canon is not None,
            "confidence": round(float(conf), 3),
            "severity": severity_for(canon, float(conf)),
            "x": round(float((x1 + bw / 2) / w * 100), 2),
            "y": round(float((y1 + bh / 2) / h * 100), 2),
            "width": round(float(bw / w * 100), 2),
            "height": round(float(bh / h * 100), 2),
        })
    return detections


# ---------------------------------------------------------------------------
# Device diagnostics
# ---------------------------------------------------------------------------

def gpu_diagnostics() -> dict:
    info: dict[str, Any] = {"torch": None, "cuda_available": False, "device_name": None,
                            "compute_capability": None, "arch_supported": None, "hint": None}
    try:
        import torch
        info["torch"] = torch.__version__
        info["torch_cuda"] = torch.version.cuda
        if torch.cuda.is_available():
            info["cuda_available"] = True
            info["device_name"] = torch.cuda.get_device_name(0)
            major, minor = torch.cuda.get_device_capability(0)
            arch = f"sm_{major}{minor}"
            info["compute_capability"] = arch
            arch_list = torch.cuda.get_arch_list()
            supported = arch in arch_list or any(
                a.startswith("compute_") and int(a.split("_")[1]) <= major * 10 + minor for a in arch_list)
            info["arch_supported"] = supported
            if not supported:
                info["hint"] = (f"This PyTorch build ({torch.__version__}, CUDA {torch.version.cuda}) has no kernels "
                                f"for {arch}. For RTX 50-series GPUs install a CUDA 12.8+ build: "
                                "pip install --upgrade torch torchvision --index-url https://download.pytorch.org/whl/cu128")
        else:
            info["hint"] = ("torch.cuda.is_available() is False - you probably have the CPU-only PyTorch wheel. "
                            "Install the CUDA build: pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128")
    except Exception as e:
        info["hint"] = f"PyTorch import failed: {e}"
    return info


def pick_device() -> str:
    if DEVICE != "auto":
        return DEVICE
    try:
        import torch
        return "cuda:0" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


# ---------------------------------------------------------------------------
# Model manager
# ---------------------------------------------------------------------------

class ModelManager:
    """Holds the *selected* weights. Each pipeline creates its OWN model instance via
    create_instance(), so the live and batch threads never share a predictor object.
    When new weights are loaded, `version` increments and workers hot-swap on their
    next loop iteration."""

    def __init__(self):
        self._lock = threading.RLock()
        self.version = 0
        self.weights_path: str | None = None
        self.display_name: str | None = None
        self.is_loaded = False
        self.device = pick_device()
        self.half = False
        self.class_names: dict = {}
        self.last_error: str | None = None
        self.warmup_ms: float | None = None
        self.diagnostics = gpu_diagnostics()

    # --- discovery ---------------------------------------------------------
    def list_available(self) -> list[dict]:
        found: dict[str, dict] = {}
        for p in sorted(MODELS_DIR.glob("*.pt")):
            found[p.name] = {"name": p.name, "path": str(p), "source": "models/"}
        for root in _SEARCH_ROOTS:
            runs = root / "runs"
            if not runs.exists():
                continue
            for p in sorted(runs.glob("**/weights/*.pt"), key=lambda x: x.stat().st_mtime, reverse=True):
                rel = p.relative_to(root).as_posix()
                found.setdefault(rel, {"name": rel, "path": str(p), "source": "training run"})
        if FALLBACK_MODEL not in found:
            found[FALLBACK_MODEL] = {"name": FALLBACK_MODEL, "path": None, "source": "official (auto-download)"}
        return list(found.values())

    def resolve(self, name: str) -> tuple[str | None, list[str]]:
        """Returns (path_to_load, searched_locations)."""
        searched: list[str] = []
        name = name.strip().replace("\\", "/")
        p = Path(name)

        candidates: list[Path] = []
        if p.is_absolute():
            candidates.append(p)
        else:
            candidates += [MODELS_DIR / name, MODELS_DIR / p.name]
            for root in _SEARCH_ROOTS:
                candidates.append(root / name)
        for c in candidates:
            searched.append(str(c))
            if c.is_file():
                return str(c.resolve()), searched

        # Newest matching file in any Ultralytics training output
        for root in _SEARCH_ROOTS:
            runs = root / "runs"
            searched.append(str(runs / "**" / "weights" / p.name))
            if runs.exists():
                hits = sorted(runs.glob(f"**/weights/{p.name}"), key=lambda x: x.stat().st_mtime, reverse=True)
                if hits:
                    return str(hits[0].resolve()), searched

        # Only official asset names may be downloaded (into models/, not the cwd)
        if _OFFICIAL_ASSET.match(p.name):
            return str(MODELS_DIR / p.name), searched
        return None, searched

    # --- loading -----------------------------------------------------------
    def _build(self, path: str):
        model = YOLO(path)
        task = getattr(model, "task", "detect")
        if task != "detect":
            raise ValueError(f"'{Path(path).name}' is a '{task}' model; a detection model is required.")
        return model

    def _warmup(self, model, device: str, half: bool) -> float:
        """Warm-up at load: one call per input shape the pipelines send (1, 2 and 8 frames, landscape and
        portrait), twice each, so no live or batch job pays the first-shape cost later."""
        t0 = time.perf_counter()
        for h, w in ((1080, 1920), (1920, 1080)):
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            for n in (1, 2, 8):
                for _ in range(2):
                    model.predict([frame] * n, device=device, half=half, verbose=False)
        return (time.perf_counter() - t0) * 1000

    def load(self, name: str) -> dict:
        with self._lock:
            if not ULTRALYTICS_AVAILABLE:
                self.last_error = "The 'ultralytics' package is not installed (pip install -U ultralytics)."
                self.is_loaded = False
                return self.status()

            path, searched = self.resolve(name)
            if path is None:
                self.last_error = (f"Weights '{name}' not found. Put the file in {MODELS_DIR} "
                                   f"(or keep it under runs/detect/<run>/weights/). Searched: " + "; ".join(searched[:6]))
                print(f"[YOLO ERROR] {self.last_error}")
                return {**self.status(), "success": False}

            print(f"[YOLO] Loading weights: {path}")
            try:
                model = self._build(path)
            except Exception as e:
                self.last_error = f"Could not read '{Path(path).name}': {e}"
                print(f"[YOLO ERROR] {self.last_error}")
                return {**self.status(), "success": False}

            device = pick_device()
            half = USE_HALF and device.startswith("cuda")
            try:
                warm = self._warmup(model, device, half)
            except Exception as e:
                # Typical on an unsupported GPU arch: "no kernel image is available"
                print(f"[YOLO WARNING] Warm-up on {device} failed ({e}); falling back to CPU.")
                self.last_error = f"GPU warm-up failed on {device}: {e}. Running on CPU. {self.diagnostics.get('hint') or ''}"
                device, half = "cpu", False
                try:
                    warm = self._warmup(model, device, half)
                except Exception as e2:
                    self.last_error = f"Warm-up failed on CPU too: {e2}"
                    return {**self.status(), "success": False}
            else:
                self.last_error = None

            self.weights_path = path
            self.display_name = name if not Path(name).is_absolute() else Path(path).name
            self.device, self.half = device, half
            self.class_names = dict(model.names)
            self.warmup_ms = round(warm, 1)
            self.is_loaded = True
            self.version += 1
            self._warm_model = model          # hand the warmed instance to the first pipeline that asks
            print(f"[YOLO] Ready: {self.display_name} on {device} (half={half}) classes={self.class_names}")
            return {**self.status(), "success": True}

    def load_default(self):
        """Boot-time: prefer best.pt, otherwise the COCO fallback (so the pipelines still run)."""
        result = self.load(DEFAULT_MODEL)
        if not result.get("success") and DEFAULT_MODEL != FALLBACK_MODEL and ULTRALYTICS_AVAILABLE:
            print(f"[YOLO] '{DEFAULT_MODEL}' unavailable at boot, loading '{FALLBACK_MODEL}' instead.")
            first_error = self.last_error
            result = self.load(FALLBACK_MODEL)
            if result.get("success"):
                self.last_error = f"Booted with {FALLBACK_MODEL} because: {first_error}"
        return result

    def create_instance(self):
        """Returns (model, version, device, half) for a worker thread, or (None, version, ...).
        The first caller gets the instance warmed at load (every input shape), so no job pays the first-shape cost."""
        with self._lock:
            if not self.is_loaded or not self.weights_path:
                return None, self.version, self.device, self.half
            model = getattr(self, "_warm_model", None)
            self._warm_model = None
            return (model or self._build(self.weights_path)), self.version, self.device, self.half

    def status(self) -> dict:
        defect_classes = {k: v for k, v in self.class_names.items() if canonical_class(v)}
        return {
            "model_path": self.display_name,
            "resolved_path": self.weights_path,
            "is_loaded": self.is_loaded,
            "ultralytics_available": ULTRALYTICS_AVAILABLE,
            "device": self.device,
            "half": self.half,
            "class_names": self.class_names,
            "defect_classes": defect_classes,
            "warmup_ms": self.warmup_ms,
            "last_error": self.last_error,
            "diagnostics": self.diagnostics,
        }


model_manager = ModelManager()


def apply_stand_in(detections: list[dict], mapping: dict | None) -> list[dict]:
    """TEST MODE ONLY: lets a generic model rehearse the defect workflow by treating a chosen
    class (e.g. "person") as DEFORMATION or CORROSION. Such detections keep their raw class and
    carry standIn=True, and the media they are recorded on is tagged as test data."""
    if not mapping:
        return detections
    for d in detections:
        if d.get("isDefect"):
            continue
        canon = mapping.get(str(d.get("rawClass", "")).lower())
        if canon in ("DEFORMATION", "CORROSION"):
            d.update(type=canon, isDefect=True, standIn=True, severity=severity_for(canon, d["confidence"]))
    return detections
