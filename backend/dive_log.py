"""
Live dive log: everything the app recorded about one live dive, in one place.

A live dive is not saved as one long video (the topside recorder - QGroundControl, a DVR, an onboard camera - keeps
the full footage; this app keeps 5 s incident clips). What the app does keep for the whole dive is assembled here:
  - ROV sensor readings (sensors DB, ~5 Hz): depth, heading, temperature, battery, mode, armed
  - vehicle events (sensors DB): link lost / recovered, armed, mode, leak, status text
  - sightings: logged detections (>= the live log threshold) clustered into events, each with depth + zone
  - incident clips and where they sit on the dive's timeline
  - operator changes: clip trigger, detection mode, dive-log recording; one-off trigger hits ignored
  - the raw MAVLink .tlog file(s)
For the chart the sensor series is thinned on the server, keeping the shallowest and deepest reading of every time
bucket so no depth excursion disappears.
"""
from __future__ import annotations

import csv
import io
import json
import time
from datetime import timezone
from pathlib import Path

from sqlalchemy import func

import sensors_db
from config import CRITICAL_CONF, DIVE_IDLE_TAIL_SEC, TLOG_DIR
from database import AnalysisRun, DefectRecord, VideoRecord, local_str
from telemetry import segment_for

EVENT_GAP_SEC = 2.0
SERIES_FIELDS = ("depth", "heading", "temperature", "battery_v", "battery_pct", "mode", "armed")


def _now_duration(v: VideoRecord, live) -> float:
    if live is not None and live.is_active and live.session_id == v.id:
        return round(time.monotonic() - live._session_t0, 1)
    return float(v.duration_sec or 0.0)


def is_live(v: VideoRecord, live) -> bool:
    return live is not None and live.is_active and live.session_id == v.id


def tlog_files(v: VideoRecord) -> list[Path]:
    """Every .tlog written for this dive (recording off/on during a dive starts a new file)."""
    return sorted(Path(TLOG_DIR).glob(f"dive-*-{v.id[:8]}.tlog"))


def _durations(v: VideoRecord, run, live, last_data: float | None, samples: int) -> tuple[float, float, float]:
    """(duration, idle tail, open time) in seconds.

    The duration is the part of the dive that has data. A dive left open with nothing coming in - typically the PC
    went to sleep, or Stop was pressed the next morning - is not hours long: since 3.6.5 Stop ends it at its last video
    frame or sensor reading and keeps the idle time in the ops log. Dives recorded before that are trimmed here, at
    their last sensor reading / sighting / clip, but only when they have a sensor log (otherwise the end is unknown)."""
    if is_live(v, live):
        d = _now_duration(v, live)
        return d, 0.0, d
    stored = float(v.duration_sec or 0.0)
    ops = json.loads(run.ops_log) if run is not None and run.ops_log else None
    if ops is not None and "idleTailSec" in ops:
        idle = float(ops.get("idleTailSec") or 0.0)
        return stored, idle, round(stored + idle, 1)
    if samples and last_data is not None and stored - last_data > DIVE_IDLE_TAIL_SEC:
        return round(last_data, 1), round(stored - last_data, 1), stored
    return stored, 0.0, stored


def summary_row(db, v: VideoRecord, live=None) -> dict:
    """Light row for the library: no sensor series."""
    clip_rows = db.query(VideoRecord.parent_offset_sec, VideoRecord.duration_sec).filter(VideoRecord.parent_video_id == v.id).all()
    clips = len(clip_rows)
    rows = db.query(DefectRecord.defect_type, DefectRecord.timestamp_sec).filter(
        DefectRecord.run_id == v.active_run_id).order_by(DefectRecord.defect_type, DefectRecord.timestamp_sec).all() \
        if v.active_run_id else []
    sightings = {"DEFORMATION": 0, "CORROSION": 0}
    last: dict = {}
    for typ, t in rows:
        if typ not in last or t - last[typ] > EVENT_GAP_SEC:
            sightings[typ] = sightings.get(typ, 0) + 1
        last[typ] = t
    s = sensors_db.SensorSession()
    try:
        n, dmax, tmax = s.query(func.count(sensors_db.SensorSample.id), func.max(sensors_db.SensorSample.depth),
                                func.max(sensors_db.SensorSample.t)).filter(sensors_db.SensorSample.media_id == v.id).one()
    finally:
        s.close()
    ends = [t for _, t in rows] + [o + (d or 0) for o, d in clip_rows if o is not None] + ([tmax] if tmax is not None else [])
    run = db.get(AnalysisRun, v.active_run_id) if v.active_run_id else None
    duration, idle, open_sec = _durations(v, run, live, max(ends) if ends else None, n or 0)
    return {"id": v.id, "name": v.display_name or v.original_filename, "lineId": v.line_id,
            "status": "LIVE" if is_live(v, live) else (v.status or "ENDED"), "isLive": is_live(v, live),
            "startedAt": local_str(v.started_at or v.created_at),
            "createdTs": (v.started_at or v.created_at).replace(tzinfo=timezone.utc).timestamp() if (v.started_at or v.created_at) else None,
            "durationSec": duration, "idleTailSec": idle, "openSec": open_sec, "clips": clips, "sightings": sightings,
            "samples": n or 0, "maxDepth": round(dmax, 1) if dmax is not None else None, "isTest": bool(v.is_test),
            "source": v.original_filename}


def _thin(rows: list[dict], points: int) -> list[dict]:
    """At most ~points rows: per time bucket the first, the shallowest and the deepest reading (in time order)."""
    if len(rows) <= points:
        return rows
    t0, t1 = rows[0]["t"], rows[-1]["t"]
    n_buckets = max(1, points // 3)
    width = (t1 - t0) / n_buckets or 1.0
    out, bucket, cur = [], [], None
    def flush():
        if not bucket:
            return
        keep = {0}
        with_depth = [i for i, r in enumerate(bucket) if r.get("depth") is not None]
        if with_depth:
            keep.add(min(with_depth, key=lambda i: bucket[i]["depth"]))
            keep.add(max(with_depth, key=lambda i: bucket[i]["depth"]))
        out.extend(bucket[i] for i in sorted(keep))
    for r in rows:
        b = int((r["t"] - t0) / width)
        if b != cur:
            flush()
            bucket, cur = [], b
        bucket.append(r)
    flush()
    if out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def _trigger_at(threshold_log: list, t: float, default: float | None) -> float | None:
    cur = default
    for ts, conf in threshold_log or []:
        if ts <= t + 1e-6:
            cur = conf
    return cur


def build(db, v: VideoRecord, line: dict | None, live=None, points: int = 2000) -> dict:
    run = db.get(AnalysisRun, v.active_run_id) if v.active_run_id else None
    row = summary_row(db, v, live)
    duration = row["durationSec"]
    live_now = is_live(v, live)

    # sensor series
    rows = sensors_db.samples_for(v.id)
    if live_now:                                               # readings not yet written (batched every ~2 s)
        sess = getattr(getattr(live, "rov", None), "_session", None)
        if sess and sess.get("media_id") == v.id:
            last = rows[-1]["t"] if rows else -1.0
            rows = rows + [r for r in list(sess.get("buffer", [])) if r.get("t", -1) > last]
    series = [{"t": round(r["t"], 2), **{k: r.get(k) for k in SERIES_FIELDS}} for r in rows]
    thin = _thin(series, max(60, points))

    # vehicle events
    events = [{"t": round(e["t"], 2) if e["t"] is not None else None, "kind": e["kind"], "text": e["text"]}
              for e in sensors_db.events_for(v.id)]

    # incident clips on the dive timeline
    clips = []
    for c in (db.query(VideoRecord).filter(VideoRecord.parent_video_id == v.id)
              .order_by(VideoRecord.parent_offset_sec).all()):
        start = c.parent_offset_sec
        clips.append({"id": c.id, "name": c.display_name or c.original_filename, "start": start,
                      "end": (start + (c.duration_sec or 0)) if start is not None else None,
                      "trigger": (start + c.trigger_offset_sec) if start is not None and c.trigger_offset_sec is not None else None,
                      "durationSec": c.duration_sec, "playable": bool(c.current_filepath)})

    # sightings = logged detections clustered into events, with depth + zone at the peak
    threshold_log = json.loads(run.threshold_log) if run and run.threshold_log else []
    sightings = []
    if run:
        import reports                                         # same clustering + depth lookup as the reports
        evs, _, _ = reports._events_for_video(db, v, line or {"segments": []}, id_prefix="S")
        for e in evs:
            trig = _trigger_at(threshold_log, e["t_peak"], run.trigger_conf)
            clip = next((c for c in clips if c["start"] is not None and c["end"] is not None
                         and c["start"] - 0.5 <= e["t_peak"] <= c["end"] + 0.5), None)
            sightings.append({"id": e["id"], "type": e["type"], "start": e["t_start"], "end": e["t_end"],
                              "peakT": e["t_peak"], "peak": e["peak_confidence"], "detections": e["detections"],
                              "depth": e["depth_m"], "zone": e["segment"], "severity": "High confidence" if e["confidence_label"] == "high" else "Low confidence",
                              "trigger": trig, "aboveTrigger": e["type"] == "DEFORMATION" and trig is not None and e["peak_confidence"] >= trig,
                              "clipId": clip["id"] if clip else None})

    # operator changes
    ops = json.loads(run.ops_log) if run and run.ops_log else {"changes": [], "flickers": 0}
    if live_now:
        ops = live.ops_log()
        threshold_log = list(live._threshold_log)
    settings = [{"t": t, "kind": "TRIGGER", "value": c} for t, c in threshold_log]
    settings += [{"t": t, "kind": k, "value": val} for t, k, val in ops.get("changes", []) if k != "TRIGGER"]
    settings.sort(key=lambda s: (s["t"], s["kind"]))

    # summary
    depths = [(r["t"], r["depth"]) for r in series if r.get("depth") is not None]
    zone_time: dict = {}
    for (ta, da), (tb, _) in zip(depths, depths[1:]):
        dt = tb - ta
        if 0 < dt <= 5:                                        # gaps (log off, link lost) are not counted
            z = segment_for(line, da) if line else None
            zone_time[z or "Unknown"] = zone_time.get(z or "Unknown", 0.0) + dt
    batt = [r for r in series if r.get("battery_v") is not None]
    summary = {
        "durationSec": duration, "idleTailSec": row["idleTailSec"], "openSec": row["openSec"],
        "pauses": [e for e in events if e["kind"] == "PAUSED"], "samples": len(series),
        "loggedSec": round(sum(zone_time.values()), 1),
        "maxDepth": round(max(d for _, d in depths), 1) if depths else None,
        "zoneTimeSec": {k: round(val, 1) for k, val in zone_time.items()},
        "clips": len(clips),
        "sightings": {t: sum(1 for s in sightings if s["type"] == t) for t in ("DEFORMATION", "CORROSION")},
        "aboveTrigger": sum(1 for s in sightings if s["aboveTrigger"]),
        "flickersIgnored": ops.get("flickers", 0),
        "battery": {"startV": batt[0]["battery_v"], "endV": batt[-1]["battery_v"],
                    "startPct": batt[0].get("battery_pct"), "endPct": batt[-1].get("battery_pct")} if batt else None,
        "linkLosses": sum(1 for e in events if e["kind"] == "LOST"),
        "settingsChanges": max(0, len(settings) - 3),          # the 3 starting values are not changes
    }
    return {
        "dive": {**row, "model": run.model_name if run else None,
                 "logConf": run.conf if run else None, "triggerConf": run.trigger_conf if run else None,
                 "lineName": (line or {}).get("name"), "endedAt": local_str(v.completed_at) if v.completed_at else None},
        "zones": [{"name": s["name"], "from": s["start_m"], "to": s["end_m"]} for s in (line or {}).get("segments", [])],
        "seabed": (line or {}).get("seabed_depth"),
        "series": thin, "seriesTotal": len(series),
        "events": events, "clips": clips, "sightings": sightings, "settings": settings, "summary": summary,
        "tlogs": [{"name": p.name, "sizeMb": round(p.stat().st_size / 1e6, 2)} for p in tlog_files(v)],
        "critical": CRITICAL_CONF,
    }


def to_csv(kind: str, log: dict, full_series: list[dict] | None = None) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    if kind == "sensors":
        w.writerow(["t_sec", *SERIES_FIELDS])
        for r in full_series or []:
            w.writerow([r["t"], *[r.get(k) for k in SERIES_FIELDS]])
    elif kind == "sightings":
        w.writerow(["id", "type", "start_sec", "end_sec", "peak_sec", "peak_confidence", "detections", "depth_m", "zone",
                    "trigger_at_the_time", "above_trigger", "incident_clip"])
        for s in log["sightings"]:
            w.writerow([s["id"], "pitting" if s["type"] == "CORROSION" else s["type"].lower(), s["start"], s["end"], s["peakT"],
                        s["peak"], s["detections"], s["depth"], s["zone"], s["trigger"], s["aboveTrigger"], s["clipId"] or ""])
    elif kind == "events":
        w.writerow(["t_sec", "source", "kind", "detail"])
        rows = [(e["t"], "vehicle", e["kind"], e["text"]) for e in log["events"]]
        rows += [(s["t"], "operator", s["kind"], s["value"]) for s in log["settings"]]
        rows += [(c["trigger"], "app", "INCIDENT_CLIP", c["name"]) for c in log["clips"]]
        for r in sorted(rows, key=lambda r: (r[0] is None, r[0] or 0)):
            w.writerow(r)
    else:
        raise ValueError(kind)
    return buf.getvalue()
