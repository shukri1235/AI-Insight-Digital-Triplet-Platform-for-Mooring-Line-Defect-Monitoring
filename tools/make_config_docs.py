"""Regenerate config.example.toml and docs/CONFIG.md from backend/config_spec.py (run after editing a setting)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ["FYP_CONFIG"] = str(ROOT / "__no_config__.toml")      # describe the defaults, not this PC's config.toml
import config  # noqa: E402

(ROOT / "config.example.toml").write_text(config.config_example(), encoding="utf-8")
(ROOT / "docs" / "CONFIG.md").write_text(config.config_reference_md(), encoding="utf-8")
print("wrote config.example.toml and docs/CONFIG.md")
