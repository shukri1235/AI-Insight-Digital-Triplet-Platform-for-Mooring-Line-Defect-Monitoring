"""
AI insight tier - grounded inspection report assistant.

Design (why it is built this way):
  1. FACTS ARE COMPUTED, NOT GENERATED. Code builds a compact facts packet from the
     database: defect events (clustered detections) with time, confidence, box, and the
     ROV depth / segment / temperature at that moment (sensor DB), the analysis
     threshold and model, and the history of earlier inspections of the same line.
  2. THE MODEL SEES THE EVIDENCE. Key frames (the event frame + a crop around the box)
     are attached, so Gemini gives a visual second opinion per event
     (CONFIRMED / UNCERTAIN / LIKELY_FALSE_POSITIVE ...).
  3. STRUCTURED OUTPUT ONLY. A fixed JSON schema: condition grade on a fixed scale
     (ACCEPTABLE / MONITOR / REPAIR / REPLACE) instead of an invented "RUL in days",
     actions from a fixed list, every finding citing an event id (EV-n).
  4. GROUNDING CHECK IN CODE. Every event id, defect type, depth, segment, and every
     depth/percentage number in the prose is checked against the facts. One automatic
     retry with the errors fed back; if it still fails the report is NEEDS_REVIEW.
  5. HUMAN SIGN-OFF. Draft -> engineer edits -> approved (locked), exported as HTML/PDF.
Without an API key a rule-based draft is produced from the same facts (no AI claims).
"""
from __future__ import annotations

import base64
import json
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import List, Literal, Optional

import cv2
from pydantic import BaseModel, Field

import sensors_db
from config import (CRITICAL_CONF, GEMINI_DEADLINE_SEC, GEMINI_FALLBACK_MODEL, GEMINI_RETRIES, GEMINI_THINKING, GEMINI_TIMEOUT_SEC, REPORT_MAX_KEYFRAMES, REPORTS_DIR, gemini_key,
                    gemini_model)
from sqlalchemy import or_

from database import AnalysisRun, DefectRecord, ReportRecord, VideoRecord, local_str, utcnow
from telemetry import assets, segment_for

EVENT_GAP_SEC = 2.0
# 3.6.8: printed on every report (HTML and PDF), so the basis of the defect classes is in the signed deliverable
BASIS_NOTE = ("Defect classes: deformation, based on the discard criteria of API RP 2I §2.4; pitting, grades 6–7 of "
              "Lone et al. (2023). Confidence labels state how certain the detector is, not how severe a defect is. "
              "Findings are visual and support, but do not replace, the engineer's assessment. Stretch, wear and "
              "early-stage corrosion require measurement.")
GRADES = ("ACCEPTABLE", "MONITOR", "REPAIR", "REPLACE")
ACTIONS = ("CONTINUE_ROUTINE_MONITORING", "REINSPECT_HD_VIDEO", "CLEAN_AND_REINSPECT", "MEASURE_LINK_DIMENSIONS",
           "CATHODIC_PROTECTION_SURVEY", "REVIEW_PREVIOUS_INSPECTIONS", "PLAN_SEGMENT_REPLACEMENT",
           "VERIFY_DETECTION_MANUALLY")
VISUAL = ("CONFIRMED", "UNCERTAIN", "NOT_VISIBLE", "LIKELY_FALSE_POSITIVE", "NOT_ASSESSED")


# ------------------------------------------------------------------ output schema
class Finding(BaseModel):
    event_ref: str = Field(description="Event id from the facts, e.g. EV-1")
    defect_type: Literal["CORROSION", "DEFORMATION"]
    visual_assessment: Literal["CONFIRMED", "UNCERTAIN", "NOT_VISIBLE", "LIKELY_FALSE_POSITIVE", "NOT_ASSESSED"]
    description: str
    depth_m: Optional[float] = None
    segment: Optional[str] = None


class Action(BaseModel):
    action: Literal["CONTINUE_ROUTINE_MONITORING", "REINSPECT_HD_VIDEO", "CLEAN_AND_REINSPECT",
                    "MEASURE_LINK_DIMENSIONS", "CATHODIC_PROTECTION_SURVEY", "REVIEW_PREVIOUS_INSPECTIONS",
                    "PLAN_SEGMENT_REPLACEMENT", "VERIFY_DETECTION_MANUALLY"]
    priority: Literal["IMMEDIATE", "SHORT_TERM", "LONG_TERM"]
    justification: str
    refs: List[str] = []


class ReportOut(BaseModel):
    summary: str
    condition_grade: Literal["ACCEPTABLE", "MONITOR", "REPAIR", "REPLACE"]
    grade_rationale: str
    findings: List[Finding]
    recommended_actions: List[Action]
    limitations: List[str]


SYSTEM = """You are a subsea mooring integrity engineer drafting an inspection report from an
automated ROV inspection system. Rules - follow them exactly:
- Use ONLY the facts packet and the attached key frames. Never invent measurements, wall-loss
  values, dates, or remaining-life estimates. Do not give a remaining useful life.
- Every finding must cite exactly one event id (EV-n) from the facts and copy its defect type,
  depth and segment from the facts (null if the facts have no depth).
- visual_assessment: judge ONLY from the key frame(s) linked to that event. If the event has no
  key frame, use NOT_ASSESSED. Use LIKELY_FALSE_POSITIVE if the image does not show the defect.
- Defect types: DEFORMATION = a link deformation of the kind API RP 2I lists as discard criteria;
  CORROSION = pitting of grade 6-7 (Lone et al. 2023) - call it "pitting" in prose, not "corrosion".
  confidence_label (high/low) is the detector's certainty, NOT the severity of the defect.
- condition_grade guidance (advisory - the engineer decides): ACCEPTABLE = no defect events;
  MONITOR = pitting only or unconfirmed/low-confidence detections; REPAIR = confirmed
  deformation or extensive pitting; REPLACE = confirmed severe deformation (several events, or
  high-confidence detections confirmed in the key frames) - say why.
- When you quote a depth or a confidence in prose, use the exact values from the facts.
- State limitations honestly (e.g. no ROV telemetry, detector confidence, single pass)."""


# ------------------------------------------------------------------ facts
def _cluster(rows: list[DefectRecord]) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[r.defect_type].append(r)
    events = []
    for typ, lst in by.items():
        lst.sort(key=lambda r: r.timestamp_sec)
        cur = None
        for r in lst:
            if cur and r.timestamp_sec - cur["end"] <= EVENT_GAP_SEC:
                cur["end"] = r.timestamp_sec
                cur["n"] += 1
                cur["raw"].add(r.raw_class or typ.lower())
                if r.confidence > cur["peak"]:
                    cur.update(peak=r.confidence, peak_t=r.timestamp_sec, peak_row=r)
            else:
                if cur:
                    events.append(cur)
                cur = {"type": typ, "start": r.timestamp_sec, "end": r.timestamp_sec, "n": 1, "peak": r.confidence,
                       "peak_t": r.timestamp_sec, "peak_row": r, "raw": {r.raw_class or typ.lower()}}
        if cur:
            events.append(cur)
    return sorted(events, key=lambda e: e["start"])


def _events_for_video(db, v: VideoRecord, line: dict, id_prefix: str = "EV") -> tuple[list[dict], dict, dict | None]:
    run = db.get(AnalysisRun, v.active_run_id) if v.active_run_id else None
    rows = db.query(DefectRecord).filter(DefectRecord.run_id == run.id).all() if run else []
    look = sensors_db.Lookup(v.id)
    events = []
    for i, e in enumerate(_cluster(rows), 1):
        s = look.at(e["peak_t"])
        depth = round(s["depth"], 1) if s and s.get("depth") is not None else None
        r = e["peak_row"]
        events.append({
            "id": f"{id_prefix}-{i}", "video_id": v.id, "type": e["type"], "raw_classes": sorted(e["raw"]),
            "t_start": round(e["start"], 2), "t_end": round(e["end"], 2), "t_peak": round(e["peak_t"], 3),
            "detections": e["n"], "peak_confidence": round(e["peak"], 2),
            "confidence_label": "high" if e["peak"] >= CRITICAL_CONF else "low",
            "box_pct": {"x": r.x, "y": r.y, "w": r.width, "h": r.height},
            "depth_m": depth, "segment": segment_for(line, depth) if depth is not None else None,
            "water_temp_c": round(s["temperature"], 1) if s and s.get("temperature") is not None else None,
            "altitude_m": round(s["altitude"], 1) if s and s.get("altitude") is not None else None,
        })
    dr, tr = look.depth_range(), look.temp_range()
    sensors = {"source": look.rows[0]["source"] if look.rows else "NONE",
               "samples": len(look.rows), "depth_range_m": [round(dr[0], 1), round(dr[1], 1)] if dr else None,
               "water_temp_range_c": [round(tr[0], 1), round(tr[1], 1)] if tr else None}
    analysis = None
    if run:
        analysis = {"run_id": run.id, "kind": run.kind, "threshold": run.conf, "trigger_threshold": run.trigger_conf,
                    "model": run.model_name, "frames_analysed": run.frames_analyzed,
                    "frames_with_defects": run.frames_with_defects,
                    "date": local_str(run.completed_at or run.created_at)}
    return events, sensors, analysis


def _same_kind(test: bool):
    """Test Mode data and real inspections are never mixed in one report."""
    return VideoRecord.is_test.is_(True) if test else or_(VideoRecord.is_test.is_(False), VideoRecord.is_test.is_(None))


def _coverage(v: VideoRecord) -> dict:
    """What part of the media the snapshot covers - a live dive grows until it ends."""
    now = utcnow()
    if v.source_type == "LIVE_SESSION" and v.status == "LIVE" and v.started_at:
        until = (now - v.started_at).total_seconds()
        return {"live": True, "until_s": round(until, 1), "snapshot_at": local_str(now, "%Y-%m-%d %H:%M:%S"),
                "text": f"Live dive in progress - data up to T+{int(until // 60)}:{int(until % 60):02d} "
                        f"(snapshot {local_str(now, '%H:%M:%S')})"}
    dur = v.duration_sec
    if v.source_type == "LIVE_SESSION":
        txt = f"Whole live dive (ended {local_str(v.completed_at)}, duration {int((dur or 0) // 60)}:{int((dur or 0) % 60):02d})"
    elif v.source_type == "INCIDENT_CLIP":
        txt = f"{dur or 0:.1f} s incident clip saved {local_str(v.created_at)}"
    else:
        txt = f"Whole video ({int((dur or 0) // 60)}:{int((dur or 0) % 60):02d})"
    return {"live": False, "until_s": dur, "snapshot_at": local_str(now, "%Y-%m-%d %H:%M:%S"), "text": txt}


def _session_evidence(db, session: VideoRecord, line: dict, events: list[dict]) -> list[tuple[dict, str]]:
    """A live dive has no full recording: take key frames from its incident clips, and link each
    clip event to the dive event it belongs to (same type, overlapping in dive time)."""
    out = []
    for c in db.query(VideoRecord).filter(VideoRecord.parent_video_id == session.id).all():
        if not c.current_filepath or not os.path.exists(c.current_filepath) or c.parent_offset_sec is None:
            continue
        cev, _, _ = _events_for_video(db, c, line, id_prefix="CLIP")
        for ce in cev:
            t = c.parent_offset_sec + ce["t_peak"]
            match = next((e for e in events if e["type"] == ce["type"] and e["t_start"] - 1.0 <= t <= e["t_end"] + 1.0
                          and not any(o[0]["id"] == e["id"] for o in out)), None)
            if match:   # clip-relative frame/box for extraction, dive event id for linking
                out.append(({**ce, "id": match["id"]}, c.current_filepath))
    return out


def _history(db, line_id: str, exclude: set[str], segment: str | None = None, test: bool = False) -> list[dict]:
    """Earlier inspections of the same line (trend context), oldest first."""
    line = assets.get(line_id)
    out = []
    vids = (db.query(VideoRecord).filter(VideoRecord.line_id == line_id, VideoRecord.active_run_id.isnot(None),
                                         VideoRecord.source_type != "INCIDENT_CLIP", _same_kind(test))
            .order_by(VideoRecord.created_at).all())
    for v in vids:
        if v.id in exclude:
            continue
        evs, _, an = _events_for_video(db, v, line)
        if segment:
            evs = [e for e in evs if e["segment"] == segment]
        out.append({"inspection": v.display_name, "date": local_str(v.created_at, "%Y-%m-%d"),
                    "corrosion_events": sum(e["type"] == "CORROSION" for e in evs),
                    "deformation_events": sum(e["type"] == "DEFORMATION" for e in evs),
                    "threshold": an["threshold"] if an else None})
    return out[-8:]


def build_facts(db, scope_type: str, scope_ref: str, test: bool = False) -> tuple[dict, list[tuple[dict, str]]]:
    """Returns (facts, evidence) where evidence = [(event, video_path)] for key frames.
    `test` selects Test Mode data for SEGMENT scopes (incident/dive scopes follow their media)."""
    evidence: list[tuple[dict, str]] = []
    test_profile = None
    if scope_type in ("INCIDENT", "DIVE"):
        v = db.get(VideoRecord, scope_ref)
        if not v:
            raise ValueError("Unknown video")
        line = assets.get(v.line_id)
        events, sensors, analysis = _events_for_video(db, v, line)
        if v.current_filepath and os.path.exists(v.current_filepath):
            evidence = [(e, v.current_filepath) for e in events]
        elif v.source_type == "LIVE_SESSION":   # no recording of the whole dive -> key frames come from its clips
            evidence = _session_evidence(db, v, line, events)
        scope = {"type": scope_type, "name": v.display_name, "media_type": v.source_type,
                 "duration_s": v.duration_sec, "trigger_offset_s": v.trigger_offset_sec,
                 "coverage": _coverage(v)}
        test = bool(v.is_test)
        test_profile = json.loads(v.test_profile) if v.test_profile else ({} if test else None)
        history = _history(db, line["id"], {v.id, v.parent_video_id or ""}, test=test)
    elif scope_type == "SEGMENT":
        line_id, segment = scope_ref.split("|", 1)
        line = assets.get(line_id)
        events, n = [], 0
        vids = (db.query(VideoRecord).filter(VideoRecord.line_id == line_id, VideoRecord.active_run_id.isnot(None),
                                             VideoRecord.source_type != "INCIDENT_CLIP", _same_kind(test)).all())
        if test:
            test_profile = {"standIn": {k: c for v in vids if v.test_profile
                                        for k, c in (json.loads(v.test_profile).get("standIn") or {}).items()}}
        for v in vids:
            evs, _, _ = _events_for_video(db, v, line, id_prefix=f"V{n}")
            n += 1
            for e in evs:
                if e["segment"] == segment:
                    e["inspection"] = v.display_name
                    events.append(e)
                    if v.current_filepath and os.path.exists(v.current_filepath):
                        evidence.append((e, v.current_filepath))
        for i, e in enumerate(events, 1):
            e["id"] = f"EV-{i}"
        sensors = {"source": "MULTIPLE", "samples": None, "depth_range_m": None, "water_temp_range_c": None}
        analysis = None
        scope = {"type": "SEGMENT", "name": f"{line_id} / {segment}", "inspections": len(vids)}
        history = _history(db, line_id, set(), segment, test=test)
    else:
        raise ValueError("scope_type must be INCIDENT, DIVE or SEGMENT")

    facts = {
        "scope": scope,
        "asset": {"line_id": line["id"], "line_name": line["name"], "seabed_depth_m": line["seabed_depth"],
                  "segments": [{"name": s["name"], "from_m": s["start_m"], "to_m": s["end_m"]} for s in line["segments"]]},
        "analysis": analysis,
        "sensors": sensors,
        "events": events,
        "counts": {"CORROSION": sum(e["type"] == "CORROSION" for e in events),
                   "DEFORMATION": sum(e["type"] == "DEFORMATION" for e in events)},
        "history": history,
        "keyframes": [],
    }
    if test_profile is not None:
        facts["test_data"] = {
            "note": "TEST MODE: simulated ROV video and simulated BlueROV2 telemetry - not an inspection of a real asset.",
            "stand_in_classes": test_profile.get("standIn") or {},
            "stand_in_note": "Events whose raw class is listed here are a generic object rehearsing the defect "
                             "workflow, not real damage." if test_profile.get("standIn") else None}
    return facts, evidence


# ------------------------------------------------------------------ key frames
def _keyframes(report_dir: Path, evidence: list[tuple[dict, str]], facts: dict) -> list[dict]:
    """Event frame with the box drawn + a 2x crop around the box, for the top events."""
    ranked = sorted(evidence, key=lambda ev: (ev[0]["type"] != "DEFORMATION", -ev[0]["peak_confidence"]))
    out = []
    by_id = {e["id"]: e for e in facts["events"]}
    for event, path in ranked[:REPORT_MAX_KEYFRAMES]:
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(event["t_peak"] * fps + 0.25))
        ok, frame = cap.read()
        cap.release()
        if not ok:
            continue
        h, w = frame.shape[:2]
        b = event["box_pct"]
        x0, y0 = int((b["x"] - b["w"] / 2) / 100 * w), int((b["y"] - b["h"] / 2) / 100 * h)
        x1, y1 = int((b["x"] + b["w"] / 2) / 100 * w), int((b["y"] + b["h"] / 2) / 100 * h)
        bw, bh = x1 - x0, y1 - y0
        crop = frame[max(0, y0 - bh):min(h, y1 + bh), max(0, x0 - bw):min(w, x1 + bw)].copy()
        full = frame.copy()
        cv2.rectangle(full, (x0, y0), (x1, y1), (60, 60, 255), 2)
        if full.shape[1] > 960:
            full = cv2.resize(full, (960, int(full.shape[0] * 960 / full.shape[1])))
        kid = f"KF-{len(out) + 1}"
        for suffix, img in (("full", full), ("crop", crop)):
            if img.size:
                cv2.imwrite(str(report_dir / f"{kid}-{suffix}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 85])
        out.append({"id": kid, "event_ref": event["id"], "t": event["t_peak"], "files": [f"{kid}-full.jpg", f"{kid}-crop.jpg"]})
        if event["id"] in by_id:
            by_id[event["id"]]["keyframe"] = kid
    return out


# ------------------------------------------------------------------ grounding check
_NUM_DEPTH = re.compile(r"(\d+(?:\.\d+)?)\s?m\b")
_NUM_PCT = re.compile(r"(\d+(?:\.\d+)?)\s?%")


def ground_check(report: dict, facts: dict) -> list[str]:
    errs: list[str] = []
    events = {e["id"]: e for e in facts["events"]}
    seen = set()
    for f in report.get("findings", []):
        e = events.get(f.get("event_ref"))
        if not e:
            errs.append(f"finding cites unknown event '{f.get('event_ref')}'")
            continue
        seen.add(e["id"])
        if f.get("defect_type") != e["type"]:
            errs.append(f"{e['id']}: type {f.get('defect_type')} but facts say {e['type']}")
        if e["depth_m"] is None:
            if f.get("depth_m") is not None:
                errs.append(f"{e['id']}: depth {f.get('depth_m')} m given but no telemetry exists for this event")
        elif f.get("depth_m") is None or abs(float(f["depth_m"]) - e["depth_m"]) > 0.5:
            errs.append(f"{e['id']}: depth {f.get('depth_m')} m does not match {e['depth_m']} m")
        if e["segment"] and f.get("segment") and f["segment"] != e["segment"]:
            errs.append(f"{e['id']}: segment '{f['segment']}' does not match '{e['segment']}'")
        if not e.get("keyframe") and f.get("visual_assessment") not in ("NOT_ASSESSED", None):
            errs.append(f"{e['id']}: visual assessment '{f['visual_assessment']}' claimed without a key frame")
    for e in facts["events"]:
        if e["type"] == "DEFORMATION" and e["id"] not in seen:
            errs.append(f"{e['id']} (DEFORMATION) is not addressed by any finding")
    for a in report.get("recommended_actions", []):
        for ref in a.get("refs", []):
            if ref not in events:
                errs.append(f"action {a.get('action')} cites unknown event '{ref}'")
    if facts["counts"]["DEFORMATION"] == 0 and report.get("condition_grade") == "REPLACE":
        errs.append("grade REPLACE without any deformation event")
    if not facts["events"] and report.get("condition_grade") not in ("ACCEPTABLE", "MONITOR"):
        errs.append("no defect events but grade is above MONITOR")
    # every depth / percentage quoted in prose must exist in the facts
    depths = {e["depth_m"] for e in facts["events"] if e["depth_m"] is not None}
    depths |= {s["from_m"] for s in facts["asset"]["segments"]} | {s["to_m"] for s in facts["asset"]["segments"]}
    depths.add(facts["asset"]["seabed_depth_m"])
    for rng in (facts["sensors"].get("depth_range_m") or []):
        depths.add(rng)
    pcts = {round(e["peak_confidence"] * 100) for e in facts["events"]}
    if facts.get("analysis"):
        for k in ("threshold", "trigger_threshold"):
            if facts["analysis"].get(k):
                pcts.add(round(facts["analysis"][k] * 100))
    prose = " ".join([report.get("summary", ""), report.get("grade_rationale", "")] +
                     [f.get("description", "") for f in report.get("findings", [])] +
                     [a.get("justification", "") for a in report.get("recommended_actions", [])])
    for m in _NUM_DEPTH.finditer(prose):
        val = float(m.group(1))
        if not any(abs(val - d) <= 0.5 for d in depths):
            errs.append(f"prose quotes {m.group(0)} - not a depth in the facts")
    for m in _NUM_PCT.finditer(prose):
        if round(float(m.group(1))) not in {p + d for p in pcts for d in (-1, 0, 1)}:
            errs.append(f"prose quotes {m.group(0)} - not a confidence in the facts")
    return errs


# ------------------------------------------------------------------ generators
def rule_based(facts: dict) -> dict:
    ev = facts["events"]
    deform = [e for e in ev if e["type"] == "DEFORMATION"]
    corr = [e for e in ev if e["type"] == "CORROSION"]
    if any(e["peak_confidence"] >= CRITICAL_CONF for e in deform):
        grade = "REPAIR"
    elif deform or corr:
        grade = "MONITOR"
    else:
        grade = "ACCEPTABLE"
    findings = [{"event_ref": e["id"], "defect_type": e["type"], "visual_assessment": "NOT_ASSESSED",
                 "description": f"{'Pitting' if e['type'] == 'CORROSION' else e['type'].title()} detected by YOLO ({e['detections']} detections, peak "
                                f"{round(e['peak_confidence'] * 100)}%)" +
                                (f" at {e['depth_m']} m in the {e['segment']}." if e["depth_m"] is not None else
                                 " - no ROV depth recorded for this event."),
                 "depth_m": e["depth_m"], "segment": e["segment"]} for e in ev]
    actions = []
    if deform:
        actions += [{"action": "VERIFY_DETECTION_MANUALLY", "priority": "IMMEDIATE",
                     "justification": "Confirm the automated deformation detection on the key frames.",
                     "refs": [e["id"] for e in deform]},
                    {"action": "MEASURE_LINK_DIMENSIONS", "priority": "SHORT_TERM",
                     "justification": "Quantify deformation of the affected links.", "refs": [e["id"] for e in deform]}]
    if corr:
        actions.append({"action": "CLEAN_AND_REINSPECT", "priority": "SHORT_TERM",
                        "justification": "Remove growth to assess the extent of the pitting.", "refs": [e["id"] for e in corr]})
    if not ev:
        actions.append({"action": "CONTINUE_ROUTINE_MONITORING", "priority": "LONG_TERM",
                        "justification": "No defect events were detected in this scope.", "refs": []})
    lim = ["Rule-based draft: no AI review of the images was performed."]
    if facts["sensors"].get("samples") == 0:
        lim.append("No ROV telemetry was recorded, so depths and segments are unknown.")
    return {"summary": f"{len(deform)} deformation and {len(corr)} pitting events detected in {facts['scope']['name']}.",
            "condition_grade": grade,
            "grade_rationale": "Derived by fixed rules from detector events; an engineer must confirm.",
            "findings": findings, "recommended_actions": actions, "limitations": lim}


def _client(timeout_sec: float | None = None):
    """Gemini client with a request timeout. Retries are done by draft_with_fallback (not the SDK), so the
    overall deadline holds and a busy model can hand over to the fallback model (older SDKs: plain client)."""
    from google import genai
    from google.genai import types
    key = gemini_key()[0] or ""
    try:
        retry = getattr(types, "HttpRetryOptions", None)
        opts = types.HttpOptions(timeout=int((timeout_sec or GEMINI_TIMEOUT_SEC) * 1000),
                                 **({"retry_options": retry(attempts=1)} if retry else {}))
        return genai.Client(api_key=key, http_options=opts)
    except (TypeError, AttributeError, ValueError):
        return genai.Client(api_key=key)


def _thinking(types, model: str) -> dict:
    """Gemini 3: thinking level (default low = fast). Older models / SDKs: leave the default."""
    if not model.startswith("gemini-3") or GEMINI_THINKING not in ("low", "high"):
        return {}
    try:
        return {"thinking_config": types.ThinkingConfig(thinking_level=GEMINI_THINKING.upper())}
    except Exception:                                    # noqa: BLE001 - SDK without thinking_level
        return {}


def gemini(facts: dict, report_dir: Path, feedback: list[str] | None = None, model: str | None = None,
           timeout_sec: float | None = None) -> dict:
    client = _client(timeout_sec)
    try:
        return _gemini_call(client, facts, report_dir, feedback, model)
    finally:
        _close(client)


class AiUnavailable(Exception):
    """Every Gemini attempt failed; .failures lists them (model, kind, seconds, message), .cause is the last error."""

    def __init__(self, cause: Exception, failures: list[dict]):
        super().__init__(str(cause))
        self.cause, self.failures = cause, failures


def _failure_kind(e: Exception) -> str:
    """busy (5xx / high demand) | timeout | quota (429) | fatal (key, model name, no internet... - another model won't help)."""
    low = f"{e.__class__.__name__} {e}".lower()
    if "timeout" in low or "timed out" in low or "deadline_exceeded" in low:
        return "timeout"
    if "resource_exhausted" in low or re.search(r"\b429\b", low) or "quota" in low:
        return "quota"
    if re.search(r"\b50[0234]\b", low) or any(w in low for w in ("unavailable", "overloaded", "high demand")):
        return "busy"
    return "fatal"


def short_error(msg: str) -> str:
    """Google's errors carry a JSON blob; keep '503 UNAVAILABLE: This model is currently experiencing high demand...'."""
    m = re.search(r"'message':\s*'([^']+)", msg or "")
    head = re.match(r"\s*(\d{3}\s+[A-Z_]+)", msg or "")
    return f"{head.group(1) + ': ' if head else ''}{m.group(1).strip()}" if m else (msg or "")


_KIND_TEXT = {"busy": "busy", "timeout": "not answering in time", "quota": "out of quota", "fatal": "failing"}
RETRY_PAUSE_SEC = 2.0


def nice_model(m: str) -> str:
    return " ".join(w.capitalize() for w in re.split(r"[-_]", m)) if m and m != "RULE_BASED" else "rules"


def draft_with_fallback(facts: dict, report_dir: Path, step=None, deadline_sec: float | None = None):
    """First Gemini draft: the chosen model (plus GEMINI_RETRIES quick retries when it answers busy), then the
    fallback model, all inside one deadline. Returns (draft, usage, model used, failed attempts)."""
    primary = gemini_model()[0]
    fallback = GEMINI_FALLBACK_MODEL if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != primary else None
    plan = [primary] * (1 + max(0, GEMINI_RETRIES)) + ([fallback] if fallback else [])
    deadline = time.time() + (deadline_sec or GEMINI_DEADLINE_SEC)
    failures: list[dict] = []
    i, last = 0, None
    while i < len(plan):
        model = plan[i]
        left = deadline - time.time()
        if failures and left < 15:
            break
        t0 = time.time()
        try:
            out, usage = gemini(facts, report_dir, model=model, timeout_sec=max(15.0, min(GEMINI_TIMEOUT_SEC, left)))
            return out, usage, model, failures
        except Exception as e:                       # noqa: BLE001
            kind, last = _failure_kind(e), e
            msg = short_error(explain_ai_error(e)["message"])
            failures.append({"model": model, "kind": kind, "seconds": round(time.time() - t0, 1), "message": msg[:200]})
            print(f"[REPORT] {model}: {_KIND_TEXT[kind]} after {failures[-1]['seconds']} s - {msg[:120]}")
            if kind == "fatal":
                break
            # a model that timed out or is out of quota will not do better seconds later: go to the fallback
            nxt = i + 1 if kind == "busy" or model != primary else (plan.index(fallback) if fallback else len(plan))
            if nxt < len(plan):
                if plan[nxt] == model:
                    time.sleep(RETRY_PAUSE_SEC)
                    if step:
                        step("ai", f"{nice_model(model)} is busy - asking again")
                elif step:
                    step("ai", f"{nice_model(model)} is {_KIND_TEXT[kind]} - {nice_model(plan[nxt])} is writing the draft")
            i = nxt
    raise AiUnavailable(last or RuntimeError("Gemini deadline reached"), failures)


def _gemini_call(client, facts: dict, report_dir: Path, feedback: list[str] | None = None, model: str | None = None) -> dict:
    from google.genai import types
    model = model or gemini_model()[0]
    parts = [("FACTS PACKET (JSON):\n" + json.dumps(facts, default=str, indent=1))]
    for kf in facts["keyframes"]:
        parts.append(f"Key frame {kf['id']} for event {kf['event_ref']} at t={kf['t']} s (full frame, box drawn; then 2x crop):")
        for fn in kf["files"]:
            p = report_dir / fn
            if p.exists():
                parts.append(types.Part.from_bytes(data=p.read_bytes(), mime_type="image/jpeg"))
    if feedback:
        parts.append("Your previous draft failed these checks against the facts - fix them:\n- " + "\n- ".join(feedback))
    t_call = time.time()
    resp = client.models.generate_content(
        model=model, contents=parts,
        # Gemini 3 models should keep their default temperature (Google: lowering it can cause looping);
        # Gemini 2.x keeps the low, factual setting used before
        config=types.GenerateContentConfig(system_instruction=SYSTEM, **({"temperature": 0.2} if model.startswith("gemini-2") else {}),
                                           **_thinking(types, model),
                                           response_mime_type="application/json", response_schema=ReportOut))
    um = getattr(resp, "usage_metadata", None)
    usage = {k: (getattr(um, f, None) or 0) for k, f in (("input", "prompt_token_count"), ("output", "candidates_token_count"),
                                                          ("thinking", "thoughts_token_count"), ("total", "total_token_count"))}
    usage["images"] = sum(1 for p in parts if not isinstance(p, str))
    usage["seconds"] = round(time.time() - t_call, 1)            # Google's response time for this request
    return ReportOut.model_validate_json(resp.text).model_dump(), usage


_last_ai_error: dict | None = None


def last_ai_error() -> dict | None:
    return _last_ai_error


def explain_ai_error(e: Exception) -> dict:
    """Turns a Gemini / SDK exception into a short reason + what to do. Never echoes the key."""
    key = (gemini_key()[0] or "")
    msg = str(e) or e.__class__.__name__
    if key:
        msg = msg.replace(key, "***")
    low = msg.lower()
    if "access_token_type_unsupported" in low or "expected oauth 2 access token" in low or \
            (("401" in low or "unauthenticated" in low) and key.startswith("AQ.")):
        hint = ("Google rejected the new-style \"AQ.\" key for this call. This is a known issue with keys AI Studio has "
                "issued since May 2026. Try: pip install -U google-genai, restart the backend; create a fresh key in AI Studio; "
                "if it still fails, report it through AI Studio's feedback. Rule-based drafts keep working meanwhile.")
    elif "api_key_invalid" in low or "api key not valid" in low or "invalid api key" in low:
        hint = "The key is not valid. Copy it again from AI Studio (no spaces or quotes), put it in GEMINI_API_KEY (or the app's .env file), restart the backend."
    elif "permission_denied" in low or "403" in low:
        hint = "The key's project is not allowed to use the Gemini API (API disabled, key restricted, or region not supported)."
    elif "resource_exhausted" in low or "429" in low or "quota" in low:
        hint = "Gemini quota or rate limit reached (free tier limits). Wait and try again, or enable billing on the key's project."
    elif ("not found" in low or "no longer available" in low) and ("model" in low or "404" in low):
        m = re.search(r"use (?:models/)?(gemini-[\w.\-]+)", msg)
        suggested = m.group(1) if m else None
        hint = (f"Google no longer offers {gemini_model()[0]} to this key. Switch the model to {suggested} "
                f"in System (gear) -> Gemini report assistant." if suggested else
                "This model is not available to this key. Choose another model in System (gear) -> Gemini report assistant.")
        return {"message": msg[:600], "hint": hint, "suggestedModel": suggested, "at": datetime.now().isoformat(timespec="seconds")}
    elif "no module named" in low and "google" in low:
        hint = "The Gemini SDK is not installed on the backend PC: pip install google-genai"
    elif "client has been closed" in low:
        hint = "Internal error in the app's Gemini call (the SDK client was closed too early). Update the app; this is not a key problem."
    elif any(w in low for w in ("getaddrinfo", "connecterror", "connection", "timed out", "timeout", "ssl")):
        hint = "The backend PC could not reach Google (no internet, proxy or firewall)."
    else:
        hint = "Gemini returned an error; the message above is Google's own."
    return {"message": msg[:600], "hint": hint, "at": datetime.now().isoformat(timespec="seconds")}


def _close(client):
    try:
        close = getattr(client, "close", None)
        if close:
            close()
    except Exception:                            # noqa: BLE001
        pass


def test_ai() -> dict:
    """Smallest possible call: is the key accepted and the model reachable?"""
    global _last_ai_error
    t0 = time.monotonic()
    try:
        from google import genai
        # keep the client referenced while it is used: recent google-genai closes its HTTP client
        # as soon as the Client object is garbage-collected ("client has been closed")
        client = _client()
        try:
            resp = client.models.generate_content(model=gemini_model()[0], contents="Reply with the single word OK.")
        finally:
            _close(client)
        _last_ai_error = None
        return {"ok": True, "ms": round((time.monotonic() - t0) * 1000), "reply": (resp.text or "").strip()[:40]}
    except Exception as e:                       # noqa: BLE001
        _last_ai_error = explain_ai_error(e)
        return {"ok": False, "ms": round((time.monotonic() - t0) * 1000), **_last_ai_error}


class RedraftFailed(Exception):
    """A redraft could not get a Gemini draft; the existing report is left as it was."""


def generate(db, scope_type: str, scope_ref: str, use_ai: bool, test: bool = False, progress=None,
             replace: ReportRecord | None = None) -> ReportRecord:
    """progress(stage, label) is called as the work moves on (facts, keyframes, ai, check, retry, rules, save).
    replace: redraft that (unapproved) report in place with Gemini - same id, the engineer's notes are kept;
    if Gemini is still unavailable, RedraftFailed is raised and the report is not touched."""
    marks: list[tuple[str, float]] = []

    def step(stage: str, label: str):
        marks.append((stage, time.time()))
        if progress:
            progress(stage, label)

    step("facts", "Collecting the recorded facts")
    facts, evidence = build_facts(db, scope_type, scope_ref, test)
    rid = replace.id if replace is not None else str(uuid.uuid4())
    rdir = REPORTS_DIR / rid
    rdir.mkdir(parents=True, exist_ok=True)
    step("keyframes", f"Extracting key frames for {len(evidence)} event{'s' if len(evidence) != 1 else ''}")
    facts["keyframes"] = _keyframes(rdir, evidence, facts)

    global _last_ai_error
    ai_raw, attempts, errors, calls, ai_error, failures, fallback = None, [], [], [], None, [], None
    if use_ai:
        try:
            step("ai", f"{nice_model(gemini_model()[0])} is writing the draft")
            out, u, used, failures = draft_with_fallback(facts, rdir, step)
            u["model"] = used
            calls.append(u)
            if used != gemini_model()[0]:
                fallback = {"from": gemini_model()[0], "to": used, "reason": _KIND_TEXT[failures[-1]["kind"]] if failures else "",
                            "message": failures[-1]["message"] if failures else ""}
            step("check", "Checking every claim against the database")
            attempts.append(ground_check(out, facts))
            if attempts[-1]:
                step("retry", f"The draft failed {len(attempts[-1])} check(s) - Gemini is rewriting it")
                try:
                    out2, u2 = gemini(facts, rdir, feedback=attempts[-1], model=used)
                    u2["model"] = used
                    calls.append(u2)
                    step("check", "Checking the rewritten draft")
                    attempts.append(ground_check(out2, facts))
                    out = out2
                except Exception as e2:          # noqa: BLE001 - keep the first draft, flagged by its failed checks
                    print(f"[REPORT] regeneration failed, keeping the first draft: {str(e2)[:160]}")
                    failures.append({"model": used, "kind": _failure_kind(e2), "seconds": 0, "message": str(e2)[:200], "stage": "rewrite"})
            ai_raw, errors, generator = out, attempts[-1], used
            _last_ai_error = None
        except Exception as e:                   # noqa: BLE001 - never lose the report because the AI failed
            cause = e.cause if isinstance(e, AiUnavailable) else e
            failures = e.failures if isinstance(e, AiUnavailable) else failures
            ai_error = _last_ai_error = explain_ai_error(cause)
            attempts = []
            print(f"[REPORT] Gemini failed, rule-based draft instead: {ai_error['message'][:200]}")
            if replace is not None:
                raise RedraftFailed(f"Gemini is still unavailable ({ai_error['message'][:160]}). The report was not changed.") from e
    if ai_raw is None:
        step("rules", "Writing the rule-based draft")
        out, generator = rule_based(facts), "RULE_BASED"
        errors = ground_check(out, facts)
        attempts.append(errors)
        if ai_error:
            tried = ", ".join(dict.fromkeys(nice_model(f["model"]) for f in failures)) or "Gemini"
            out["limitations"] = [f"Gemini was unavailable ({tried} tried; {short_error(ai_error['message'])[:160]}), so this is a rule-based draft."] + \
                                 out.get("limitations", [])
    elif fallback:
        out["limitations"] = [f"Drafted by {nice_model(fallback['to'])} because {nice_model(fallback['from'])} was {fallback['reason']}; "
                              f"its visual check is less thorough, so look at the key frames yourself."] + out.get("limitations", [])

    ev = {e["id"]: e for e in facts["events"]}
    for f in out["findings"]:
        f["event"] = ev.get(f["event_ref"])
    second = defaultdict(int)
    for f in out["findings"]:
        second[f["visual_assessment"]] += 1
    if facts.get("test_data"):          # deterministic, never left to the model
        td = facts["test_data"]
        note = "TEST DATA: simulated ROV video and telemetry - not a real inspection."
        if td.get("stand_in_classes"):
            note += " Stand-in classes rehearse defects: " + ", ".join(f"{k} as {v}" for k, v in td["stand_in_classes"].items()) + "."
        out["limitations"] = [note] + [x for x in out.get("limitations", []) if x != note]
    # actual token use as reported by Gemini (summed over the first draft and any regeneration)
    usage = None
    if calls:
        usage = {k: sum(c[k] for c in calls) for k in ("input", "output", "thinking", "total", "images")}
        usage["seconds"] = round(sum(c.get("seconds", 0) for c in calls), 1)
        usage["requests"] = len(calls)
        usage["perRequest"] = calls
    # how long each stage took (shown on the report; slow Gemini calls are visible at a glance)
    marks.append(("end", time.time()))
    timings: dict = {}
    for (st, t0), (_, t1) in zip(marks, marks[1:]):
        key = "ai" if st in ("ai", "retry") else "check" if st == "check" else st
        timings[key] = round(timings.get(key, 0.0) + (t1 - t0), 2)
    timings["total"] = round(marks[-1][1] - marks[0][1], 2)
    if ai_error:
        timings["ai_failed"] = True
    print(f"[REPORT] {scope_type} {scope_ref[:8]}: " + ", ".join(f"{k} {v:.1f}s" for k, v in timings.items() if isinstance(v, float)))
    content = {**out, "keyframes": facts["keyframes"], "engineer_notes": "", "test_data": facts.get("test_data"), "usage": usage,
               "timings": timings, "thinking": GEMINI_THINKING if generator != "RULE_BASED" and generator.startswith("gemini-3") else None,
               "ai_error": ai_error, "ai_attempts": failures, "fallback": fallback,
               "redrafts": (json.loads(replace.report_json).get("redrafts", 0) + 1) if replace is not None else 0,
               "second_opinion": dict(second), "scope": facts["scope"], "asset": facts["asset"],
               "analysis": facts["analysis"], "sensors": facts["sensors"], "history": facts["history"]}
    if replace is not None:
        old = json.loads(replace.report_json)
        content["engineer_notes"] = old.get("engineer_notes", "")
        replace.status = "NEEDS_REVIEW" if errors else "DRAFT"
        replace.generator, replace.facts_json = generator, json.dumps(facts, default=str)
        replace.ai_json, replace.report_json = json.dumps(ai_raw) if ai_raw else None, json.dumps(content, default=str)
        replace.grounding_json = json.dumps({"passed": not errors, "errors": errors, "attempts": len(attempts),
                                             "firstAttemptErrors": attempts[0] if attempts else []})
        step("save", "Saving the report")
        db.commit()
        return replace
    rec = ReportRecord(id=rid, scope_type=scope_type, scope_ref=scope_ref,
                       title=f"{'[TEST] ' if facts.get('test_data') else ''}{scope_type.title()} report - {facts['scope']['name']}",
                       status="NEEDS_REVIEW" if errors else "DRAFT", generator=generator,
                       facts_json=json.dumps(facts, default=str), ai_json=json.dumps(ai_raw) if ai_raw else None,
                       report_json=json.dumps(content, default=str),
                       grounding_json=json.dumps({"passed": not errors, "errors": errors, "attempts": len(attempts),
                                                  "firstAttemptErrors": attempts[0] if attempts else []}))
    step("save", "Saving the report")
    db.add(rec)
    db.commit()
    return rec


def to_dict(r: ReportRecord, full: bool = True) -> dict:
    base = {"id": r.id, "scopeType": r.scope_type, "scopeRef": r.scope_ref, "title": r.title, "status": r.status,
            "generator": r.generator, "engineer": r.engineer,
            "approvedAt": r.approved_at.isoformat() if r.approved_at else None,
            "createdAt": r.created_at.isoformat() if r.created_at else None}
    content = json.loads(r.report_json)
    base["conditionGrade"] = content.get("condition_grade")
    base["scopeName"] = (content.get("scope") or {}).get("name")
    base["lineId"] = (content.get("asset") or {}).get("line_id")
    base["isTest"] = bool(content.get("test_data"))
    base["findings"] = len(content.get("findings") or [])
    if full:
        base["content"] = content
        base["grounding"] = json.loads(r.grounding_json or "{}")
    return base


def apply_edit(r: ReportRecord, edit: dict):
    c = json.loads(r.report_json)
    for k in ("summary", "grade_rationale", "engineer_notes", "limitations"):
        if k in edit:
            c[k] = edit[k]
    if "condition_grade" in edit:
        if edit["condition_grade"] not in GRADES:
            raise ValueError("invalid grade")
        c["condition_grade"] = edit["condition_grade"]
    for fe in edit.get("findings", []):   # engineer overrides the visual verdict / text per event
        for f in c["findings"]:
            if f["event_ref"] == fe.get("event_ref"):
                if fe.get("visual_assessment") in VISUAL:
                    f["visual_assessment"] = fe["visual_assessment"]
                if "description" in fe:
                    f["description"] = fe["description"]
    if "recommended_actions" in edit:
        for a in edit["recommended_actions"]:
            if a.get("action") not in ACTIONS:
                raise ValueError(f"invalid action {a.get('action')}")
        c["recommended_actions"] = edit["recommended_actions"]
    c["edited_by_engineer"] = True
    r.report_json = json.dumps(c, default=str)
    r.status = "EDITED"
    r.updated_at = utcnow()


def render_html(r: ReportRecord) -> str:
    c = json.loads(r.report_json)
    g = json.loads(r.grounding_json or "{}")
    rdir = REPORTS_DIR / r.id
    esc = lambda s: (str(s) if s is not None else "—").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def img(fn):
        p = rdir / fn
        return f'<img src="data:image/jpeg;base64,{base64.b64encode(p.read_bytes()).decode()}">' if p.exists() else ""
    kfs = {k["event_ref"]: k for k in c.get("keyframes", [])}
    rows = "".join(
        f"<tr><td>{esc(f['event_ref'])}</td><td>{esc('Pitting (grade 6–7)' if f['defect_type'] == 'CORROSION' else f['defect_type'].title())}</td><td>{esc(f.get('depth_m'))}</td>"
        f"<td>{esc(f.get('segment'))}</td><td>{esc((f.get('event') or {}).get('peak_confidence'))}</td>"
        f"<td>{esc(f['visual_assessment'])}</td><td>{esc(f['description'])}</td></tr>" for f in c["findings"])
    acts = "".join(f"<li><b>{esc(a['priority'])}</b> — {esc(a['action'].replace('_', ' ').title())}: "
                   f"{esc(a['justification'])} <small>{esc(', '.join(a.get('refs', [])))}</small></li>"
                   for a in c["recommended_actions"])
    frames = "".join(f"<figure>{img(k['files'][0])}{img(k['files'][1])}<figcaption>{esc(k['id'])} · "
                     f"{esc(k['event_ref'])} · t={esc(k['t'])} s</figcaption></figure>" for k in c.get("keyframes", []))
    an = c.get("analysis") or {}
    grade_color = {"ACCEPTABLE": "#059669", "MONITOR": "#d97706", "REPAIR": "#ea580c",
                   "REPLACE": "#dc2626"}.get(c.get("condition_grade"), "#475569")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>{esc(r.title)}</title><style>
body{{font-family:Inter,Arial,sans-serif;max-width:900px;margin:32px auto;color:#1e293b;padding:0 16px}}
h1{{font-size:22px;margin:0}} .meta{{color:#64748b;font-size:13px;margin:6px 0 18px}}
.grade{{display:inline-block;padding:4px 10px;border-radius:6px;font-weight:700;color:#fff;background:{grade_color}}}
table{{border-collapse:collapse;width:100%;font-size:13px}} td,th{{border:1px solid #cbd5e1;padding:6px;text-align:left;vertical-align:top}}
figure{{display:inline-block;margin:6px;width:420px}} figure img{{max-width:100%;display:block;margin-bottom:4px}}
figcaption{{font-size:12px;color:#64748b}} .box{{background:#f1f5f9;padding:10px 14px;border-radius:8px;font-size:13px}}
</style></head><body>
{'<div style="background:#f59e0b;color:#111;font-weight:700;padding:8px 14px;border-radius:6px;margin-bottom:12px">TEST DATA - simulated ROV video and telemetry. Not a record of a real inspection.</div>' if c.get('test_data') else ''}
<h1>{esc(r.title)}</h1>
<div class="meta">Asset {esc(c['asset']['line_name'])} · Status <b>{esc(r.status)}</b>{' · Approved by ' + esc(r.engineer) + ' on ' + esc(local_str(r.approved_at)) if r.approved_at else ''}
· Generated by {esc(r.generator)} · {esc(local_str(r.created_at) or '')}<br>
{('Covers: ' + esc(c['scope']['coverage']['text']) + '<br>') if (c.get('scope') or {}).get('coverage') else ''}
{('AI usage: ' + esc(c['usage']['requests']) + ' request(s), ' + esc(c['usage']['input']) + ' input / ' + esc(c['usage']['output']) + ' output / ' + esc(c['usage']['thinking']) + ' thinking tokens<br>') if c.get('usage') else ''}
{('Time taken: ' + esc(c['timings'].get('total')) + ' s' + ((' (Gemini ' + esc(c['timings']['ai']) + ' s' + (', ' + esc(c.get('thinking')) + ' thinking' if c.get('thinking') else '') + ')') if isinstance(c['timings'].get('ai'), (int, float)) else '') + '<br>') if isinstance(c.get('timings'), dict) and c['timings'].get('total') is not None else ''}
Analysis: {esc(an.get('kind'))} run, threshold {esc(an.get('threshold'))}{', trigger ' + esc(an.get('trigger_threshold')) if an.get('trigger_threshold') else ''}, model {esc(an.get('model'))}</div>
<p>Condition grade: <span class="grade">{esc(c['condition_grade'])}</span></p>
<h3>Summary</h3><p>{esc(c['summary'])}</p><p><i>{esc(c['grade_rationale'])}</i></p>
<h3>Findings</h3><table><tr><th>Event</th><th>Type</th><th>Depth (m)</th><th>Segment</th><th>Peak conf.</th><th>Visual check</th><th>Description</th></tr>{rows}</table>
<h3>Recommended actions</h3><ul>{acts}</ul>
<h3>Evidence</h3>{frames or '<p>No key frames.</p>'}
<h3>Limitations</h3><ul>{''.join(f'<li>{esc(x)}</li>' for x in c.get('limitations', []))}</ul>
<h3>Basis of the findings</h3><p>{esc(BASIS_NOTE)}</p>
{('<h3>Engineer notes</h3><p>' + esc(c['engineer_notes']) + '</p>') if c.get('engineer_notes') else ''}
<div class="box">Grounding check: {'all statements verified against the inspection database' if g.get('passed') else 'FAILED - ' + esc('; '.join(g.get('errors', [])))}.
Every finding cites an event id; depths, segments and confidences are checked by code, not taken from the AI.</div>
</body></html>"""


def list_models() -> dict:
    """Gemini models this key can use for generateContent (falls back to the known list)."""
    from config import GEMINI_KNOWN_MODELS
    try:
        from google import genai
        client = _client()
        try:
            names = []
            for m in client.models.list():
                name = (getattr(m, "name", "") or "").replace("models/", "")
                actions = getattr(m, "supported_actions", None) or getattr(m, "supported_generation_methods", None) or []
                if name.startswith("gemini") and (not actions or "generateContent" in actions) \
                        and not any(x in name for x in ("embedding", "image", "tts", "audio", "live")):
                    names.append(name)
        finally:
            _close(client)
        return {"models": sorted(set(names), reverse=True) or list(GEMINI_KNOWN_MODELS), "source": "your key"}
    except Exception as e:                       # noqa: BLE001
        return {"models": list(GEMINI_KNOWN_MODELS), "source": f"built-in list ({str(e)[:80]})"}
