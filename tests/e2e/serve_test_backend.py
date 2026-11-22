"""
Starts the backend in TEST MODE for the browser walkthrough (tests/e2e/scenarios.mjs):
isolated temp databases and media, colour-blob detector test double, Gemini test double,
and generated scenario footage. Your real databases are never touched.

The ROV link listens on the normal BlueOS port (udp 14551). scenarios.mjs starts
tools/bluerov_sim.py itself, so the "ROV offline" state is verified first.

    python tests/e2e/serve_test_backend.py          # API on :8500
    npm --prefix frontend run dev                   # UI on :3000
    node tests/e2e/scenarios.mjs                     # needs: npm i -D playwright
"""
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
run = Path(tempfile.mkdtemp(prefix="mooring_e2e_"))
(run / "models").mkdir()
(run / "models" / "best.pt").write_bytes(b"test-double")
os.environ.update({
    "FYP_MEDIA_DIR": str(run / "managed_media"), "FYP_LOGS_DIR": str(run / "logs"), "FYP_MODELS_DIR": str(run / "models"),
    "FYP_DB_PATH": str(run / "e2e.db"), "FYP_SETTINGS_FILE": str(run / "settings.json"), "FYP_REPORT_TEMPLATES_DIR": str(run / "report_templates"), "FYP_CONFIG": str(run / "config.toml"), "FYP_MAVLINK_URL": os.environ.get("FYP_MAVLINK_URL", "udpin:0.0.0.0:14551"),
    "GEMINI_API_KEY": "test-key", "FAKE_YOLO_LATENCY_MS": "8",
})
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tests" / "fakes"))
sys.path.insert(0, str(ROOT / "backend"))

from vv_media import make_dive  # noqa: E402  (same ground-truth footage as the pytest suite)

media = HERE / ".media"   # stands in for the operator's "external drive" folder
media.mkdir(exist_ok=True)
make_dive(media / "ML01_dive_last_month.mp4")
make_dive(media / "ML02_dive_a.mp4", seconds=4)   # multi-file batch import (3.6)
make_dive(media / "ML02_dive_b.mp4", seconds=5)   # different length = a different file (fingerprint)
print(f"[E2E] scenario footage (import in place from here): {media}")
print(f"[E2E] isolated run dir: {run}")

import uvicorn  # noqa: E402
import main  # noqa: E402



@main.app.post("/__e2e/fake-gemini")
def _e2e_fake_gemini(cfg: dict):
    """E2E only (never in the product): make the fake Gemini busy for some models, and clear the AI cooldown
    so a scenario can run several AI reports back to back."""
    os.environ["FAKE_GEMINI_BUSY"] = cfg.get("busy", "")
    main._ai_state["last_ok"] = 0.0
    return {"busy": os.environ["FAKE_GEMINI_BUSY"]}


# When the built UI exists, the backend serves it from "/" (a catch-all mount). Routes added here come after that
# mount and would never be reached, so the mount goes last (seen when the walkthrough first ran against the served UI).
_routes = main.app.router.routes
_routes.sort(key=lambda r: getattr(r, "path", None) == "" and type(r).__name__ == "Mount")

uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("FYP_PORT", "8500")), log_level="warning")
