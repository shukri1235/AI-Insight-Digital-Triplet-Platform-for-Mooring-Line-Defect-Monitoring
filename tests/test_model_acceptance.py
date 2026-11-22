"""
AI COMPONENT ACCEPTANCE - run on the target machine with the REAL trained weights.
Skipped automatically unless FYP_REAL_WEIGHTS is set. Runs in a subprocess so the
real ultralytics package is used instead of the test double.

    set FYP_REAL_WEIGHTS=D:\\FYP Proto\\runs\\detect\\train\\weights\\best.pt
    set FYP_VAL_DATA=D:\\FYP Proto\\datasets\\mooring\\data.yaml      (optional: accuracy test)
    set FYP_MIN_MAP50=0.80
    set FYP_VAL_SPLIT=test     (final result on the real held-out test videos; default: val)
    set FYP_CLEAN_FRAMES=D:\\mooring-dataset-kit\\data\\yolo_v2\\images\\test   (optional: false-alarm test)
    python -m pytest tests/test_model_acceptance.py -v -s
  or simply:  tools\\acceptance.bat <best.pt> [data.yaml] [clean frames folder]

MA-1  best.pt resolves and loads on CUDA (RTX 5060 / sm_120) - no silent CPU fallback
MA-2  per-frame inference at 720p fits the 30 fps budget (< 33 ms median)
MA-3  accuracy on the held-out validation split meets the agreed mAP@0.5 target and the
      DEFORMATION class has adequate recall (it triggers incident clips)
MA-4  false alarms on real defect-free frames: share of frames with a DEFORMATION detection at or above the clip
      trigger (default 0.60) stays below FYP_MAX_FALSE_ALARM (default 1 %); per-class rates are printed
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1] / "backend"
WEIGHTS = os.environ.get("FYP_REAL_WEIGHTS")
pytestmark = pytest.mark.skipif(not WEIGHTS, reason="set FYP_REAL_WEIGHTS to run model acceptance tests")

PROBE = r'''
import json, os, sys, time, shutil, tempfile, numpy as np
from pathlib import Path
tmp = Path(tempfile.mkdtemp()); (tmp / "models").mkdir()
shutil.copy(os.environ["FYP_REAL_WEIGHTS"], tmp / "models" / "best.pt")
os.environ.update({"FYP_MODELS_DIR": str(tmp / "models"), "FYP_DB_PATH": str(tmp / "db.sqlite"),
                   "FYP_UPLOAD_DIR": str(tmp / "up")})
sys.path.insert(0, sys.argv[1])
from model_manager import ModelManager, parse_result
mm = ModelManager(); st = mm.load("best.pt")
out = {"load": {k: st.get(k) for k in ("success", "device", "half", "last_error", "class_names", "defect_classes", "warmup_ms")},
       "diagnostics": mm.diagnostics}
if st.get("success"):
    model, _, dev, half = mm.create_instance()
    frame = (np.random.rand(720, 1280, 3) * 255).astype(np.uint8)
    for _ in range(5): model.predict(frame, device=dev, half=half, verbose=False)
    lat = []
    for _ in range(50):
        t0 = time.perf_counter(); model.predict(frame, conf=0.1, device=dev, half=half, verbose=False)
        lat.append((time.perf_counter() - t0) * 1000)
    lat.sort(); out["latency_ms"] = {"median": round(lat[25], 2), "p95": round(lat[47], 2)}
    data = os.environ.get("FYP_VAL_DATA")
    if data:
        m = model.val(data=data, split=os.environ.get('FYP_VAL_SPLIT', 'val'), device=dev, half=half, verbose=False, plots=False)
        names = model.names
        out["val"] = {"map50": float(m.box.map50), "map50_95": float(m.box.map),
                      "per_class_map50": {names[int(c)]: float(v) for c, v in zip(m.box.ap_class_index, m.box.ap50)},
                      "per_class_recall": {names[int(c)]: float(v) for c, v in zip(m.box.ap_class_index, m.box.r)}}
    clean = os.environ.get("FYP_CLEAN_FRAMES")
    if clean:
        import glob
        files = sorted(p for e in ("jpg", "jpeg", "png") for p in glob.glob(os.path.join(clean, f"*.{e}")))
        trig = float(os.environ.get("FYP_TRIGGER", "0.60"))
        hits, flagged = {}, 0
        for i in range(0, len(files), 16):
            for r in model.predict(files[i:i + 16], conf=min(0.25, trig), device=dev, half=half, verbose=False):
                cls = [r.names[int(c)] for c, p in zip(r.boxes.cls.tolist(), r.boxes.conf.tolist()) if p >= trig]
                for c in set(cls):
                    hits[c] = hits.get(c, 0) + 1
                flagged += any("deform" in c.lower() for c in cls)
        out["false_alarms"] = {"frames": len(files), "trigger": trig, "deformation_frames": flagged,
                               "rate": flagged / max(1, len(files)), "per_class_frames": hits}
print("@@" + json.dumps(out))
'''


@pytest.fixture(scope="module")
def probe():
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}  # no test doubles
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})    # ultralytics prints UTF-8 (progress bars);
    r = subprocess.run([sys.executable, "-c", PROBE, str(BACKEND)], capture_output=True, env=env,  # Windows' default
                       encoding="utf-8", errors="replace", timeout=3600)                          # cp1252 cannot read it
    out, err = r.stdout or "", r.stderr or ""
    line = next((l for l in out.splitlines() if l.startswith("@@")), None)
    assert line, f"the model probe did not finish (exit code {r.returncode}):\n{out[-2000:]}\n{err[-3000:]}"
    res = json.loads(line[2:])
    print(json.dumps(res, indent=2))
    return res


def test_ma_1_loads_on_gpu(probe):
    assert probe["load"]["success"], probe["load"]["last_error"]
    assert probe["load"]["device"].startswith("cuda"), \
        f"fell back to {probe['load']['device']}: {probe['load']['last_error']} / {probe['diagnostics'].get('hint')}"
    assert probe["load"]["defect_classes"], f"no corrosion/deformation classes in {probe['load']['class_names']}"


def test_ma_2_realtime_latency(probe):
    assert probe["latency_ms"]["median"] < 33.3, probe["latency_ms"]


@pytest.mark.skipif(not os.environ.get("FYP_VAL_DATA"), reason="set FYP_VAL_DATA to a dataset yaml")
def test_ma_3_accuracy_targets(probe):
    target = float(os.environ.get("FYP_MIN_MAP50", "0.80"))   # raised from 0.50 after review (3.6.10)
    v = probe["val"]
    assert v["map50"] >= target, f"mAP@0.5 {v['map50']:.3f} < target {target} ({v})"
    deform = {k: r for k, r in v["per_class_recall"].items() if "deform" in k.lower() or "bent" in k.lower()}
    need = float(os.environ.get("FYP_MIN_DEFORM_RECALL", "0.90"))  # raised from 0.70 after review (3.6.10); read first: a failing assert must not print os.environ
    got = min(deform.values()) if deform else 0.0
    assert deform and got >= need, f"deformation recall {got:.3f} < target {need} ({v})"


@pytest.mark.skipif(not os.environ.get("FYP_CLEAN_FRAMES"), reason="set FYP_CLEAN_FRAMES to a folder of defect-free frames")
def test_ma_4_false_alarms_on_clean_chain(probe):
    fa = probe["false_alarms"]
    assert fa["frames"] > 0, "no images in FYP_CLEAN_FRAMES"
    limit = float(os.environ.get("FYP_MAX_FALSE_ALARM", "0.01"))
    assert fa["rate"] <= limit, f"false-alarm rate {fa['rate']:.3%} > limit {limit:.1%} ({fa})"

