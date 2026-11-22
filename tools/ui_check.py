"""Exit 0 if frontend/dist is up to date with frontend/src, 1 if it is missing or older (used by start.bat / start.sh)."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
index = root / "frontend" / "dist" / "index.html"
if not index.exists():
    print("missing")
    sys.exit(1)
built = index.stat().st_mtime
watched = [p for p in (root / "frontend" / "src").rglob("*") if p.is_file()]
watched += [root / "frontend" / n for n in ("index.html", "package.json", "vite.config.ts") if (root / "frontend" / n).exists()]
newer = [p for p in watched if p.stat().st_mtime > built]
if newer:
    print(f"stale ({len(newer)} changed file(s), e.g. {newer[0].relative_to(root)})")
    sys.exit(1)
print("ok")
