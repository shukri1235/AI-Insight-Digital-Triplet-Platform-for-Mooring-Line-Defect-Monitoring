"""
TEST DOUBLE for ultralytics.YOLO - a deterministic colour-blob "detector".

Scenario tests need ground truth: we must know exactly which frame contains which
defect at which confidence. Synthetic dive videos draw coloured shapes; this double
"detects" them with fixed classes/confidences:

    pure red   (BGR 0,0,230)   -> 'deformation'  conf 0.88   (Scenario 1.3: "88% confidence")
    yellow     (BGR 0,230,230) -> 'pitting'      conf 0.32   (Scenario 2.3: secondary pitting < 0.60)
    cyan       (BGR 230,230,0) -> 'corrosion'    conf 0.80
    green      (BGR 0,200,0)   -> 'mooringline'  conf 0.70   (non-defect class: shown, never logged)

The real model is validated separately (tests/test_model_acceptance.py).
Set FAKE_YOLO_LATENCY_MS to emulate GPU inference time per frame.
"""
import os
import time

import cv2
import numpy as np

__version__ = "test-double"

RANGES = {
    0: ((0, 0, 180), (60, 60, 255), 0.88),
    1: ((0, 180, 180), (60, 255, 255), 0.32),
    2: ((180, 180, 0), (255, 255, 60), 0.80),
    3: ((0, 150, 0), (60, 255, 60), 0.70),
}
NAMES = {0: "deformation", 1: "pitting", 2: "corrosion", 3: "mooringline"}
CALLS = {"predict": 0, "frames": 0}


class _T:
    def __init__(self, a):
        self.a = np.asarray(a)

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _Boxes:
    def __init__(self, xyxy, conf, cls):
        self.xyxy, self.conf, self.cls = _T(xyxy), _T(conf), _T(cls)

    def __len__(self):
        return len(self.xyxy.a)


class _Res:
    def __init__(self, shape, boxes):
        self.orig_shape, self.boxes = shape, boxes


class YOLO:
    def __init__(self, path):
        if not os.path.exists(str(path)):
            raise FileNotFoundError(path)
        self.path = str(path)
        self.task = "detect"
        self.names = dict(NAMES)

    def predict(self, source, conf=0.25, iou=0.45, device="cpu", half=False, verbose=False):
        frames = source if isinstance(source, list) else [source]
        CALLS["predict"] += 1
        CALLS["frames"] += len(frames)
        latency = float(os.environ.get("FAKE_YOLO_LATENCY_MS", "8")) / 1000.0
        out = []
        for f in frames:
            if latency:
                time.sleep(latency)
            xyxy, cf, cl = [], [], []
            for c, (lo, hi, c_conf) in RANGES.items():
                if c_conf < conf:
                    continue
                m = cv2.inRange(f, np.array(lo), np.array(hi))
                cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                for cnt in cnts:
                    x, y, w, h = cv2.boundingRect(cnt)
                    if w * h < 150:
                        continue
                    xyxy.append([x, y, x + w, y + h]); cf.append(c_conf); cl.append(c)
            out.append(_Res(f.shape, _Boxes(np.array(xyxy, dtype=float).reshape(-1, 4), np.array(cf), np.array(cl))))
        return out
