"""Physical-asset lookup (mooring lines and their depth segments).

ROV telemetry itself comes from the BlueROV2 over MAVLink - see rov_link.py. There is no
simulated telemetry in the application any more."""
from __future__ import annotations

import threading

from database import MooringLineRecord, SessionLocal


class AssetCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._lines: dict[str, dict] = {}

    def refresh(self):
        db = SessionLocal()
        try:
            lines = {}
            for line in db.query(MooringLineRecord).all():
                lines[line.id] = {
                    "id": line.id,
                    "name": line.name,
                    "seabed_depth": line.seabed_depth_meters or 200.0,
                    "segments": [{"name": s.segment_name, "start_m": s.depth_start_m, "end_m": s.depth_end_m}
                                 for s in line.segments],
                }
            with self._lock:
                self._lines = lines
        finally:
            db.close()

    def get(self, line_id: str | None) -> dict:
        with self._lock:
            if line_id in self._lines:
                return self._lines[line_id]
            if self._lines:
                return next(iter(self._lines.values()))
        return {"id": line_id or "UNKNOWN", "name": "Unknown", "seabed_depth": 200.0, "segments": []}

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._lines.values())


assets = AssetCache()


def segment_for(line: dict, depth: float) -> str:
    segs = line["segments"]
    if not segs:
        return "Unknown"
    for seg in segs:
        if seg["start_m"] <= depth <= seg["end_m"]:
            return seg["name"]
    # in a boundary gap (e.g. 30.05 m between 30.0 and 30.1) or beyond the ends: nearest segment
    return min(segs, key=lambda s: min(abs(depth - s["start_m"]), abs(depth - s["end_m"])))["name"]


def enrich(tel: dict | None, line_id: str | None) -> dict | None:
    """Adds the mooring-line context (segment, seabed) to a raw ROV telemetry reading."""
    if tel is None:
        return None
    line = assets.get(line_id)
    out = dict(tel)
    out["lineId"] = line["id"]
    out["seabedDepth"] = line["seabed_depth"]
    out["segment"] = segment_for(line, tel["depth"]) if tel.get("depth") is not None else None
    return out
