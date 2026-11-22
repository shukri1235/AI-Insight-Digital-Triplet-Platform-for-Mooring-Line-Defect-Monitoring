"""
Event log paper trail: one plain-text file per app session in the logs folder (paths.logs_dir, default <app>/logs).

  logs/session_2026-11-07_19-40-12.txt      (named after the time the backend started)

Two sources, tagged on every line so a reader can tell them apart:
  [app]  written by the backend itself, so the key facts are on record even with no browser open:
         session start / end, live dive start / stop, incident clip saved, batch analysis finished / failed,
         report approved, settings problems.
  [ui]   every entry of the event log shown in the browser (operator actions, alerts, link changes ...),
         sent by the page in small batches. A second browser tab is told apart by its client id.

Lines are appended and flushed at once, never rewritten: the file is a paper trail, not a database.
Format:  2026-11-07 19:40:12 | Clip     | [app] Incident clip saved: ...
"""
from __future__ import annotations

import re
import threading
from datetime import datetime
from pathlib import Path

from config import LOGS_DIR

_lock = threading.Lock()
_started = datetime.now()
SESSION_NAME = f"session_{_started:%Y-%m-%d_%H-%M-%S}.txt"
_NAME_RE = re.compile(r"^session_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt$")
_header_written = False


def current_path() -> Path:
    return Path(LOGS_DIR) / SESSION_NAME


def _clean(text: str, limit: int = 2000) -> str:
    """One line per entry: no line breaks or control characters (they would forge extra lines)."""
    text = re.sub(r"[\r\n\t]+", " ", str(text or ""))
    text = re.sub(r"[\x00-\x1f\x7f]", "", text).strip()
    return text[:limit]


def _write(lines: list[str]):
    global _header_written
    with _lock:
        path = current_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if not _header_written:
                f.write(f"# AI Insights Digital Triplet Platform event log - session started {_started:%Y-%m-%d %H:%M:%S}\n"
                        f"# date time | type | [source] message      ([app] = backend, [ui] = the browser's event log)\n")
                _header_written = True
            f.write("".join(l + "\n" for l in lines))
            f.flush()


def _line(when: datetime, kind: str, source: str, text: str) -> str:
    return f"{when:%Y-%m-%d %H:%M:%S} | {_clean(kind, 12) or 'Info':<8} | [{source}] {_clean(text)}"


def app(kind: str, text: str):
    """A backend event. Never raises: the paper trail must not break the feature that logs."""
    try:
        _write([_line(datetime.now(), kind, "app", text)])
    except Exception as e:  # noqa: BLE001
        print(f"[LOG] could not write the event log: {e}")


def ui(entries: list[dict], client: str = "") -> int:
    """Entries from the browser: {ts (ISO or epoch ms), kind, text}. Returns how many were written."""
    lines = []
    src = "ui" + (f" {_clean(client, 12)}" if client else "")
    for e in entries[:500]:
        if not isinstance(e, dict) or not str(e.get("text") or "").strip():
            continue
        when = _when(e.get("ts"))
        lines.append(_line(when, str(e.get("kind") or "Info"), src, str(e["text"])))
    if lines:
        _write(lines)
    return len(lines)


def _when(ts) -> datetime:
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts)
        if isinstance(ts, str) and ts:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return d.astimezone().replace(tzinfo=None) if d.tzinfo else d
    except (ValueError, OverflowError, OSError):
        pass
    return datetime.now()


def sessions(limit: int = 50) -> list[dict]:
    folder = Path(LOGS_DIR)
    if not folder.is_dir():
        return []
    files = sorted((p for p in folder.iterdir() if _NAME_RE.match(p.name)), key=lambda p: p.name, reverse=True)
    return [{"name": p.name, "sizeKb": round(p.stat().st_size / 1024, 1), "current": p.name == SESSION_NAME}
            for p in files[:limit]]


def path_for(name: str) -> Path | None:
    """A session file by name - only names this module makes, so no other file can be fetched."""
    if not _NAME_RE.match(name or ""):
        return None
    p = Path(LOGS_DIR) / name
    return p if p.is_file() else None
