"""
Per-segment defect counts for the Defect Registry.

A defect has no depth of its own - the depth comes from the ROV sensor log at the moment it
was seen. Each event (detections of one class less than EVENT_GAP_SEC apart, same rule as the
line totals and the reports) is assigned to the segment of the ROV depth at its peak frame.
Events with no sensor sample within MAX_GAP_SEC have "unknown depth": they count in the line
total but not in any segment.

Results are cached per (analysis run, #detections, #sensor samples), so polling is cheap and a
live dive is recomputed only when something new was logged.
"""
from __future__ import annotations

import bisect
import threading

from sqlalchemy import text

import sensors_db
from telemetry import segment_for

EVENT_GAP_SEC = 2.0
MAX_GAP_SEC = 2.0
TYPES = ("CORROSION", "DEFORMATION")

_cache: dict[str, tuple[tuple, dict]] = {}
_lock = threading.Lock()


def _depth_series(media_id: str) -> tuple[list[float], list[float]]:
    with sensors_db.engine.connect() as c:
        rows = c.execute(text("SELECT t, depth FROM sensor_samples WHERE media_id = :m AND depth IS NOT NULL ORDER BY t"),
                         {"m": media_id}).fetchall()
    return [r[0] for r in rows], [r[1] for r in rows]


def _depth_at(ts: list[float], ds: list[float], t: float) -> float | None:
    if not ts:
        return None
    i = bisect.bisect_left(ts, t)
    j = min((k for k in (i - 1, i) if 0 <= k < len(ts)), key=lambda k: abs(ts[k] - t))
    return ds[j] if abs(ts[j] - t) <= MAX_GAP_SEC else None


def video_breakdown(db, video_id: str, run_id: str, line: dict) -> dict:
    """{"segments": {name: {"events": {...}, "detections": {...}}}, "unknown": {...}, "covered": [names]}"""
    n_def = db.execute(text("SELECT COUNT(*) FROM defects WHERE run_id = :r"), {"r": run_id}).scalar() or 0
    n_sen = sensors_db.count_for(video_id)
    key = (run_id, n_def, n_sen, line["id"])
    with _lock:
        hit = _cache.get(video_id)
        if hit and hit[0] == key:
            return hit[1]

    rows = db.execute(text("SELECT defect_type, timestamp_sec, confidence FROM defects WHERE run_id = :r "
                           "ORDER BY defect_type, timestamp_sec"), {"r": run_id}).fetchall()
    ts, ds = _depth_series(video_id)
    empty = lambda: {"events": dict.fromkeys(TYPES, 0), "detections": dict.fromkeys(TYPES, 0)}
    out = {"segments": {s["name"]: empty() for s in line["segments"]}, "unknown": empty(),
           "covered": sorted({segment_for(line, d) for d in ds}) if ds else []}

    def close(ev):
        depth = _depth_at(ts, ds, ev["peak_t"])
        bucket = out["segments"].get(segment_for(line, depth)) if depth is not None else out["unknown"]
        if bucket is None:
            bucket = out["unknown"]
        bucket["events"][ev["type"]] += 1
        bucket["detections"][ev["type"]] += ev["n"]

    cur = None
    for typ, t, conf in rows:
        if typ not in TYPES:
            continue
        if cur and cur["type"] == typ and t - cur["end"] <= EVENT_GAP_SEC:
            cur["end"], cur["n"] = t, cur["n"] + 1
            if conf > cur["peak"]:
                cur["peak"], cur["peak_t"] = conf, t
        else:
            if cur:
                close(cur)
            cur = {"type": typ, "end": t, "n": 1, "peak": conf, "peak_t": t}
    if cur:
        close(cur)

    with _lock:
        _cache[video_id] = (key, out)
    return out


def line_breakdown(db, videos: list[tuple[str, str]], line: dict) -> dict:
    """Sums video_breakdown over (video_id, run_id) pairs; adds how many inspections covered each segment."""
    segs = {s["name"]: {"counts": dict.fromkeys(TYPES, 0), "detections": dict.fromkeys(TYPES, 0), "inspections": 0}
            for s in line["segments"]}
    unknown = {"counts": dict.fromkeys(TYPES, 0), "detections": dict.fromkeys(TYPES, 0), "inspections": 0}
    by_video: dict[str, dict[str, int]] = {}      # video -> {segment: events}; lets the UI filter dives by zone
    for vid, rid in videos:
        b = video_breakdown(db, vid, rid, line)
        by_video[vid] = {name: sum(v["events"].values()) for name, v in b["segments"].items() if any(v["events"].values())}
        for name, v in b["segments"].items():
            for t in TYPES:
                segs[name]["counts"][t] += v["events"][t]
                segs[name]["detections"][t] += v["detections"][t]
        for name in b["covered"]:
            if name in segs:
                segs[name]["inspections"] += 1
        u = b["unknown"]
        if any(u["events"].values()):
            unknown["inspections"] += 1
        for t in TYPES:
            unknown["counts"][t] += u["events"][t]
            unknown["detections"][t] += u["detections"][t]
    return {"segments": segs, "unknownDepth": unknown, "byVideo": by_video}
