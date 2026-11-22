"""
AI Insights Digital Triplet Platform - FastAPI backend (BlueROV2 edition).

Run from anywhere:   python backend/main.py        API docs: http://localhost:8500/docs
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import Optional

import anyio
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

import event_log
import reports as rpt
import segments
import sensors_db
import tlog_import
from batch_worker import BatchWorker, create_run
from config import (gemini_key, gemini_model, save_gemini_model, GEMINI_KNOWN_MODELS, TEST_STREAM_URL, ALLOW_REMOTE_FS, API_PORT, BASE_DIR, BATCH_SAMPLE_FPS, GEMINI_COOLDOWN_SEC, GEMINI_MODEL,
                    INFERENCE_CONF_FLOOR, LIVE_LOG_CONF, MEDIA_DIR, REPORT_CONF, REPORTS_DIR, TLOG_DIR, VIDEO_EXTS)
from database import (AnalysisRun, DefectRecord, ReportRecord, SessionLocal, VideoRecord, init_db, local_str, seed_physical_assets,
                      utcnow)
from gpu_arbiter import GpuArbiter
from ingest import Hasher, fs_list, fs_roots, register_in_place
from live_stream import LiveStreamWorker
from model_manager import model_manager
from rov_link import rov
from telemetry import assets
from test_mode import TestMode
from video_utils import is_browser_playable, probe, quick_fingerprint

BOOT_TIME = time.time()
EVENT_GAP_SEC = 2.0
FILE_SOURCES = ("LOCAL_UPLOAD", "LOCAL_IMPORT", "INCIDENT_CLIP")

arbiter = GpuArbiter(model_manager)
live = LiveStreamWorker(model_manager, arbiter, rov)
batch = BatchWorker(model_manager, arbiter, live)
test_mode = TestMode(rov)
live.test_mode = test_mode


def _tag_if_test(db: Session, video_id: str):
    """Media ingested while Test Mode is on is test data (kept apart from real inspections)."""
    if test_mode.enabled:
        v = db.get(VideoRecord, video_id)
        v.is_test, v.test_profile = True, test_mode.profile_json()
        db.commit()


def _queue_first_run(video_id: str, conf: float):
    batch.enqueue_run(create_run(video_id, "BATCH", conf, BATCH_SAMPLE_FPS, model_name=model_manager.display_name))


hasher = Hasher(on_ready=_queue_first_run)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    sensors_db.init_sensor_db()
    seed_physical_assets()
    assets.refresh()
    rov.start()

    def _boot():
        model_manager.load_default()
        batch.requeue_interrupted()
        db = SessionLocal()
        try:
            for v in db.query(VideoRecord).filter(VideoRecord.status == "HASHING"):
                hasher.submit(v.id)
        finally:
            db.close()
    threading.Thread(target=_boot, name="boot", daemon=True).start()
    import config as _cfg_mod
    event_log.app("System", f"Session started - backend on port {API_PORT}, database "
                            f"{'local SQLite' if _cfg_mod.DATABASE_URL.startswith('sqlite') else _cfg_mod.redact_url(_cfg_mod.DATABASE_URL)}, "
                            f"settings {'from ' + str(_cfg_mod.CONFIG_FILE) if _cfg_mod.CONFIG_STATUS.get('loaded') else 'at defaults'}")
    for p in _cfg_mod.CONFIG_STATUS.get("problems", []):
        event_log.app("Config", f"Setting problem: {p['text']}")
    yield
    live.stop()
    live.wait_for_writers(20)
    test_mode.stop()
    rov.stop()
    event_log.app("System", "Session ended - backend stopped")


app = FastAPI(title="AI Insights Digital Triplet Platform", version="3.6.11", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["*"],
                   allow_headers=["*"], expose_headers=["Retry-After"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _check_conf(conf: Optional[float]) -> float:
    if conf is None:
        raise HTTPException(400, "A detection threshold must be chosen before processing")
    if not INFERENCE_CONF_FLOOR <= conf <= 0.99:
        raise HTTPException(400, f"Threshold must be between {INFERENCE_CONF_FLOOR} and 0.99")
    return round(conf, 3)


@app.get("/api/health")
def health():
    """Service status (the UI itself is served at /)."""
    return {"status": "online", "service": "AI Insights Digital Triplet Platform", "uptime_sec": round(time.time() - BOOT_TIME),
            "model": model_manager.status()["model_path"], "rov": rov.status()["state"], "docs": "/docs"}


@app.get("/api/assets")
def get_assets():
    return assets.all()


# ===========================================================================
# VIDEO LIBRARY
# ===========================================================================

def _fmt_duration(sec):
    if not sec:
        return "--:--"
    sec = int(round(sec))
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _run_json(r: AnalysisRun | None) -> dict | None:
    if not r:
        return None
    return {"id": r.id, "kind": r.kind, "conf": r.conf, "triggerConf": r.trigger_conf,
            "thresholdLog": r.threshold_history(), "model": r.model_name, "status": r.status,
            "progress": round(r.progress or 0, 1), "sampleFps": r.sample_fps, "framesAnalyzed": r.frames_analyzed,
            "framesWithDefects": r.frames_with_defects, "error": r.error_message, "parentRunId": r.parent_run_id,
            "createdAt": r.created_at.isoformat() if r.created_at else None,
            "completedAt": r.completed_at.isoformat() if r.completed_at else None,
            "timing": json.loads(r.timing) if r.timing else None}


def _video_json(db: Session, v: VideoRecord, counts: dict | None = None, events: dict | None = None) -> dict:
    path = v.current_filepath
    exists = bool(path) and os.path.exists(path)
    playable_path = v.playback_path if v.playback_path and os.path.exists(v.playback_path) else path
    active = db.get(AnalysisRun, v.active_run_id) if v.active_run_id else None
    pending = (db.query(AnalysisRun).filter(AnalysisRun.video_id == v.id,
                                            AnalysisRun.status.in_(["QUEUED", "PROCESSING"]))
               .order_by(AnalysisRun.created_at.desc()).first())
    if counts is None:
        counts = _defect_counts(db, [v.active_run_id] if v.active_run_id else [])
    size = os.path.getsize(path) if exists else (v.file_size or 0)
    return {
        "id": v.id, "name": v.display_name or v.original_filename, "sourceType": v.source_type,
        "status": v.status, "progress": round(v.progress or 0, 1),
        "etaSec": batch.eta_sec.get(v.id) if v.id == batch.current_video else None,
        "elapsedSec": batch.elapsed_sec.get(v.id),
        "error": v.error_message, "lineId": v.line_id, "parentId": v.parent_video_id,
        "triggerOffsetSec": v.trigger_offset_sec, "durationSec": v.duration_sec, "duration": _fmt_duration(v.duration_sec),
        "resolution": f"{v.width}x{v.height}" if v.width else "Unknown", "fps": v.fps, "codec": v.codec,
        "playable": bool(v.playback_path) or is_browser_playable(v.codec),
        "fileSize": f"{size / (1024 * 1024):.1f} MB" if size else "—",
        "path": path if v.source_type == "LOCAL_IMPORT" else None,
        "isManaged": bool(v.is_managed), "missing": not exists, "mediaOffline": not exists and v.source_type == "LOCAL_IMPORT",
        "framesAnalyzed": v.frames_analyzed or 0,
        "defectCount": counts.get(v.active_run_id, 0) if v.active_run_id else 0,
        "defectEvents": (events or _event_counts(db, [v.active_run_id])).get(v.active_run_id) if v.active_run_id else None,
        "incident": _clip_incident(db, v) if v.source_type == "INCIDENT_CLIP" else None,
        "parentName": _parent_name(db, v.parent_video_id) if v.parent_video_id else None,
        "activeRun": _run_json(active), "pendingRun": _run_json(pending), "pendingConf": v.pending_conf,
        "telemetrySamples": sensors_db.count_for(v.id),
        "tlog": {"path": v.tlog_path, "offsetSec": v.tlog_offset_sec, "samples": v.tlog_samples} if v.tlog_path else None,
        "dateCreated": local_str(v.created_at),
        "createdTs": v.created_at.replace(tzinfo=timezone.utc).timestamp() if v.created_at else None,
        "pinned": v.pinned_at is not None,
        "pinnedTs": v.pinned_at.replace(tzinfo=timezone.utc).timestamp() if v.pinned_at else None,
        "isTest": bool(v.is_test), "testProfile": json.loads(v.test_profile) if v.test_profile else None,
        "streamUrl": f"/api/videos/stream/{v.id}" if playable_path else None,
    }


def _parent_name(db: Session, pid: str) -> str | None:
    p = db.get(VideoRecord, pid)
    return (p.display_name or p.original_filename) if p else None


def _defect_counts(db: Session, run_ids: list) -> dict:
    run_ids = [r for r in run_ids if r]
    if not run_ids:
        return {}
    return dict(db.query(DefectRecord.run_id, func.count(DefectRecord.id))
                .filter(DefectRecord.run_id.in_(run_ids)).group_by(DefectRecord.run_id).all())


def _event_counts(db: Session, run_ids: list) -> dict:
    """{run_id: {"DEFORMATION": events, "CORROSION": events}} - same grouping rule as the registry."""
    run_ids = [r for r in run_ids if r]
    if not run_ids:
        return {}
    marks = ",".join(f":r{i}" for i in range(len(run_ids)))
    rows = db.execute(text(f"""SELECT run_id, defect_type, COUNT(*) FROM (
        SELECT run_id, defect_type, timestamp_sec - LAG(timestamp_sec) OVER (
            PARTITION BY run_id, defect_type ORDER BY timestamp_sec) AS gap
        FROM defects WHERE run_id IN ({marks})) AS g
        WHERE gap IS NULL OR gap > :gap GROUP BY run_id, defect_type"""),
                      {"gap": EVENT_GAP_SEC, **{f"r{i}": r for i, r in enumerate(run_ids)}}).all()
    out: dict = {}
    for rid, typ, n in rows:
        out.setdefault(rid, {"DEFORMATION": 0, "CORROSION": 0})[typ] = n
    return out


_incident_cache: dict[str, dict] = {}


def _clip_incident(db: Session, v: VideoRecord) -> dict | None:
    """What triggered an incident clip (from its original CLIP analysis, so a re-analysis never
    changes it): defect, confidence, ROV depth and time into the live dive."""
    if v.id in _incident_cache:
        return _incident_cache[v.id]
    run = db.query(AnalysisRun).filter(AnalysisRun.video_id == v.id, AnalysisRun.kind == "CLIP").first()
    if not run:
        return None
    trig = v.trigger_offset_sec or 0.0
    rows = (db.query(DefectRecord.defect_type, DefectRecord.confidence, DefectRecord.timestamp_sec)
            .filter(DefectRecord.run_id == run.id).all())
    if not rows:
        return None
    near = [r for r in rows if abs(r[2] - trig) <= 0.25] or rows
    typ, conf, t = max(near, key=lambda r: (r[0] == "DEFORMATION", r[1]))
    samples = sensors_db.samples_for(v.id, trig - 0.5, trig + 0.5)
    depth = min(samples, key=lambda r: abs(r["t"] - trig))["depth"] if samples else None
    out = {"type": typ, "confidence": round(conf, 3), "depth": depth, "triggerConf": run.trigger_conf,
           "sessionTime": round((v.parent_offset_sec or 0.0) + trig, 2) if v.parent_offset_sec is not None else None}
    _incident_cache[v.id] = out
    return out


@app.get("/api/videos")
def list_videos(db: Session = Depends(get_db)):
    vids = (db.query(VideoRecord).filter(VideoRecord.source_type.in_(FILE_SOURCES))
            .order_by(VideoRecord.created_at.desc()).all())
    run_ids = [v.active_run_id for v in vids]
    counts, events = _defect_counts(db, run_ids), _event_counts(db, run_ids)
    return [_video_json(db, v, counts, events) for v in vids]


@app.get("/api/videos/precheck")
def precheck(fingerprint: str, db: Session = Depends(get_db)):
    v = db.query(VideoRecord).filter(VideoRecord.quick_hash == fingerprint).first()
    return {"match": _video_json(db, v) if v else None}


@app.get("/api/videos/{video_id}")
def get_video(video_id: str, db: Session = Depends(get_db)):
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    return _video_json(db, v)


def _register_upload(db: Session, tmp: str, digest: str, filename: str, name: str, line_id: str, conf: float):
    dup = db.query(VideoRecord).filter(VideoRecord.file_hash == digest).first()
    if dup:
        os.remove(tmp)
        return None, dup
    ext = Path(filename).suffix.lower() or ".mp4"
    vid = str(uuid.uuid4())
    final = MEDIA_DIR / f"{vid}{ext}"
    shutil.move(tmp, final)
    meta = probe(final)
    rec = VideoRecord(id=vid, file_hash=digest, original_filename=filename, display_name=(name.strip() or filename),
                      current_filepath=str(final), is_managed=True, status="QUEUED", source_type="LOCAL_UPLOAD",
                      line_id=line_id, pending_conf=conf, quick_hash=quick_fingerprint(final),
                      file_size=final.stat().st_size, duration_sec=meta["duration_sec"], fps=meta["fps"],
                      width=meta["width"], height=meta["height"], codec=meta["codec"])
    db.add(rec)
    db.commit()
    return rec, None


@app.post("/api/videos/upload-stream")
async def upload_stream(request: Request, background_tasks: BackgroundTasks, filename: str,
                        conf: Optional[float] = None, name: str = "", line_id: str = "ML-02-WEST",
                        db: Session = Depends(get_db)):
    """API-only ingest (scripts, other machines): the raw body is written into media/ while
    being SHA-256 hashed in one pass. The UI imports files in place instead (no copy).
    The threshold is required up-front."""
    conf = _check_conf(conf)
    if Path(filename).suffix.lower() not in VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported file type '{Path(filename).suffix}'")
    sha = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=MEDIA_DIR, suffix=".part")
    out = os.fdopen(fd, "wb")
    buf, received = bytearray(), 0

    def sink(chunk: bytes):
        sha.update(chunk)
        out.write(chunk)
    try:
        async for chunk in request.stream():
            buf += chunk
            received += len(chunk)
            if len(buf) >= 4 << 20:
                block, buf = bytes(buf), bytearray()
                await anyio.to_thread.run_sync(sink, block)
        if buf:
            await anyio.to_thread.run_sync(sink, bytes(buf))
        out.close()
        expected = request.headers.get("content-length")
        if expected and int(expected) != received:
            raise HTTPException(400, f"Upload truncated ({received} of {expected} bytes)")
        if received == 0:
            raise HTTPException(400, "Empty upload")
        rec, dup = await anyio.to_thread.run_sync(
            lambda: _register_upload(db, tmp, sha.hexdigest(), os.path.basename(filename), name, line_id, conf))
    except (ClientDisconnect, HTTPException, OSError):
        if not out.closed:
            out.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    if dup:
        return {"success": True, "duplicate": True, "video": _video_json(db, dup),
                "message": f"Identical file already ingested as '{dup.display_name or dup.original_filename}'"}
    _tag_if_test(db, rec.id)
    background_tasks.add_task(_queue_first_run, rec.id, conf)
    return {"success": True, "duplicate": False, "video": _video_json(db, rec)}


class ImportRequest(BaseModel):
    path: str
    name: str = ""
    line_id: str = "ML-02-WEST"
    conf: Optional[float] = None
    tlog_path: Optional[str] = None
    tlog_offset_sec: Optional[float] = None


def _local_only(request: Request):
    host = request.client.host if request.client else ""
    if not ALLOW_REMOTE_FS and host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "The file browser is only available on the backend PC itself "
                                 "(set FYP_ALLOW_REMOTE_FS=1 to allow remote browsing)")


@app.post("/api/videos/import")
def import_in_place(req: ImportRequest, request: Request, db: Session = Depends(get_db)):
    """Registers a file on this PC WITHOUT copying it. Hashing runs in the background."""
    _local_only(request)
    conf = _check_conf(req.conf)
    try:
        rec, dup = register_in_place(req.path, req.name, req.line_id, conf)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if dup:
        return JSONResponse(status_code=409, content={
            "duplicate": True, "message": f"Already ingested as '{dup.display_name or dup.original_filename}'",
            "video": _video_json(db, dup)})
    _tag_if_test(db, rec.id)
    if req.tlog_path:
        rec.tlog_path = req.tlog_path
        _attach_tlog(db, rec.id, req.tlog_path, req.tlog_offset_sec)
    hasher.submit(rec.id)
    return {"success": True, "video": _video_json(db, db.get(VideoRecord, rec.id))}


class ConfRequest(BaseModel):
    conf: Optional[float] = None


@app.post("/api/videos/{video_id}/process")
def process_video(video_id: str, req: ConfRequest, db: Session = Depends(get_db)):
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if v.source_type not in ("LOCAL_UPLOAD", "LOCAL_IMPORT"):
        raise HTTPException(400, "Only dive videos can be batch processed (use re-analyse for clips)")
    if v.status in ("QUEUED", "PROCESSING", "HASHING"):
        return {"success": True, "status": v.status}
    if v.status == "DUPLICATE":
        raise HTTPException(409, v.error_message or "Duplicate file")
    conf = _check_conf(req.conf)
    v.pending_conf = conf
    v.status = "UNPROCESSED"
    db.commit()
    _queue_first_run(video_id, conf)
    return {"success": True, "status": "QUEUED"}


@app.post("/api/videos/{video_id}/analyse")
def reanalyse(video_id: str, req: ConfRequest, db: Session = Depends(get_db)):
    """Creates a NEW analysis run at another threshold. The original run is kept untouched."""
    v = db.get(VideoRecord, video_id)
    if not v or v.source_type not in FILE_SOURCES:
        raise HTTPException(404, "Video not found")
    if v.status != "COMPLETED":
        raise HTTPException(409, "The video must be processed before it can be re-analysed")
    conf = _check_conf(req.conf)
    sample_fps = v.fps if v.source_type == "INCIDENT_CLIP" else BATCH_SAMPLE_FPS   # clips: every frame
    rid = create_run(video_id, "REANALYSIS", conf, sample_fps, parent_run_id=v.active_run_id,
                     model_name=model_manager.display_name)
    batch.enqueue_run(rid)
    return {"success": True, "runId": rid}


@app.post("/api/videos/{video_id}/cancel")
def cancel_video(video_id: str):
    batch.cancel_video(video_id)
    return {"success": True}


class PinRequest(BaseModel):
    pinned: bool


@app.put("/api/videos/{video_id}/pin")
def pin_video(video_id: str, req: PinRequest, db: Session = Depends(get_db)):
    """Pins a dive or clip to the top of the Review library. Stored in the database so pins
    survive restarts and are the same for every operator on this backend."""
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if req.pinned and v.pinned_at is None:
        v.pinned_at = utcnow()
    elif not req.pinned:
        v.pinned_at = None
    db.commit()
    return {"success": True, "pinned": v.pinned_at is not None}


@app.post("/api/runs/{run_id}/activate")
def activate_run(run_id: str, db: Session = Depends(get_db)):
    r = db.get(AnalysisRun, run_id)
    if not r or r.status != "COMPLETED":
        raise HTTPException(404, "Completed run not found")
    db.get(VideoRecord, r.video_id).active_run_id = r.id
    db.commit()
    return {"success": True}


@app.delete("/api/videos/{video_id}")
def delete_video(video_id: str, db: Session = Depends(get_db)):
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    if v.status in ("QUEUED", "PROCESSING", "LIVE"):
        raise HTTPException(409, "Cancel processing before deleting")
    if v.is_managed:   # never delete the operator's own files that were imported in place
        for p in {v.current_filepath, v.playback_path}:
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError as e:
                    print(f"[DELETE] Could not remove {p}: {e}")
    sensors_db.delete_media(v.id)
    db.delete(v)
    db.commit()
    return {"success": True, "fileDeleted": bool(v.is_managed)}


class LocateRequest(BaseModel):
    path: str


@app.post("/api/videos/{video_id}/locate")
def locate_video(video_id: str, req: LocateRequest, db: Session = Depends(get_db)):
    """Point an in-place dive at its file's new location (drive letter changed, folder moved).
    The file must be the SAME recording: size + quick fingerprint, or the full SHA-256 for
    records that have no fingerprint. Records are never deleted to "fix" a missing file."""
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    p = Path(req.path)
    if not p.is_file():
        raise HTTPException(400, "File not found")
    if v.quick_hash:
        same = quick_fingerprint(p) == v.quick_hash
    elif v.file_hash:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(8 << 20), b""):
                h.update(chunk)
        same = h.hexdigest() == v.file_hash
    else:
        raise HTTPException(409, "This record has no fingerprint to check the file against")
    if not same:
        raise HTTPException(409, "That is a different file (fingerprint does not match the recorded dive)")
    v.current_filepath = str(p)
    if v.error_message and "offline" in v.error_message.lower():
        v.error_message = None
    db.commit()
    return _video_json(db, v)


@app.get("/api/videos/{video_id}/runs")
def get_runs(video_id: str, db: Session = Depends(get_db)):
    v = db.get(VideoRecord, video_id)
    runs = db.query(AnalysisRun).filter(AnalysisRun.video_id == video_id).order_by(AnalysisRun.created_at).all()
    counts = _defect_counts(db, [r.id for r in runs])
    return {"activeRunId": v.active_run_id if v else None,
            "runs": [{**_run_json(r), "defectCount": counts.get(r.id, 0)} for r in runs]}


@app.get("/api/videos/{video_id}/defects")
def get_video_defects(video_id: str, run_id: Optional[str] = None, db: Session = Depends(get_db)):
    v = db.get(VideoRecord, video_id)
    rid = run_id or (v.active_run_id if v else None)
    run = db.get(AnalysisRun, rid) if rid else None
    rows = (db.query(DefectRecord).filter(DefectRecord.run_id == rid).order_by(DefectRecord.timestamp_sec).all()
            if rid else [])
    return {"video_id": video_id, "run": _run_json(run), "defects": [{
        "id": d.id, "defect_type": d.defect_type, "raw_class": d.raw_class, "timestamp_sec": d.timestamp_sec,
        "confidence": d.confidence, "severity": d.severity,
        "x": d.x, "y": d.y, "width": d.width, "height": d.height} for d in rows]}


@app.get("/api/videos/{video_id}/telemetry")
def get_video_telemetry(video_id: str, max_hz: Optional[float] = None):
    """ROV sensor readings on this media's timeline (empty = no telemetry recorded).
    max_hz thins long dives for the review timeline (e.g. 1 Hz for a 4 h dive)."""
    return sensors_db.samples_for(video_id, max_hz=max_hz if max_hz and max_hz > 0 else None)


@app.get("/api/videos/{video_id}/objects")
def get_video_objects(video_id: str, run_id: Optional[str] = None, db: Session = Depends(get_db)):
    """General (non-defect) detections of one analysis - e.g. "person" from yolo11n. They live in
    the sensor DB's object log and never enter the defect registry."""
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    rid = run_id or v.active_run_id
    return {"runId": rid, "objects": sensors_db.objects_for(video_id, rid) if rid else [],
            "counts": sensors_db.object_counts(video_id, rid) if rid else {}}


def _attach_tlog(db: Session, video_id: str, tlog_path: str, offset: float | None) -> dict:
    v = db.get(VideoRecord, video_id)
    res = tlog_import.attach(video_id, v.current_filepath, v.duration_sec, tlog_path, offset)
    v.tlog_path, v.tlog_offset_sec, v.tlog_samples = tlog_path, res["offsetSec"], res["samples"]
    db.commit()
    return res


class TlogImport(BaseModel):
    path: str
    offset_sec: Optional[float] = None


@app.post("/api/videos/{video_id}/tlog-import")
def tlog_import_local(video_id: str, req: TlogImport, request: Request, db: Session = Depends(get_db)):
    _local_only(request)
    try:
        return _attach_tlog(db, video_id, req.path, req.offset_sec)
    except (ValueError, RuntimeError, OSError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/videos/{video_id}/tlog-upload")
async def tlog_upload(video_id: str, request: Request, offset_sec: Optional[float] = None,
                      db: Session = Depends(get_db)):
    if not db.get(VideoRecord, video_id):
        raise HTTPException(404, "Video not found")
    data = await request.body()
    TLOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp, path = TLOG_DIR / f"{video_id}.part.tlog", TLOG_DIR / f"{video_id}.tlog"
    tmp.write_bytes(data)
    try:   # validate before replacing a previously attached log
        if not await anyio.to_thread.run_sync(lambda: tlog_import.read_tlog(str(tmp))):
            raise ValueError("No depth telemetry found in this .tlog")
        os.replace(tmp, path)
        return await anyio.to_thread.run_sync(lambda: _attach_tlog(db, video_id, str(path), offset_sec))
    except (ValueError, RuntimeError, OSError) as e:
        raise HTTPException(400, str(e))
    finally:
        if tmp.exists():
            tmp.unlink()


_MIME = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
         ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}


@app.get("/api/videos/stream/{video_id}")
def stream_video(video_id: str, range: Optional[str] = Header(None), db: Session = Depends(get_db)):
    v = db.get(VideoRecord, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    path = v.playback_path if v.playback_path and os.path.exists(v.playback_path) else v.current_filepath
    if not path or not os.path.exists(path):
        raise HTTPException(404, "Media offline - file not found on disk")
    size = os.path.getsize(path)
    mime = _MIME.get(Path(path).suffix.lower(), "application/octet-stream")
    start, end, status = 0, size - 1, 200
    if range and range.startswith("bytes="):
        s, _, e = range[6:].split(",")[0].strip().partition("-")
        try:
            if s == "":
                start = max(0, size - int(e))
            else:
                start = int(s)
                end = min(int(e), size - 1) if e else size - 1
        except ValueError:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        if start >= size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        status = 206
    length = end - start + 1

    def gen():
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 18, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
    headers = {"Accept-Ranges": "bytes", "Content-Length": str(length), "Content-Type": mime}
    if status == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return StreamingResponse(gen(), status_code=status, headers=headers, media_type=mime)


# ===========================================================================
# FILE BROWSER (backend PC only)
# ===========================================================================

@app.get("/api/fs/roots")
def get_fs_roots(request: Request):
    _local_only(request)
    return {"roots": fs_roots(), "home": str(Path.home())}


@app.get("/api/fs/list")
def get_fs_list(path: str, request: Request, kind: str = "video"):
    _local_only(request)
    try:
        return fs_list(path, (".tlog",) if kind == "tlog" else VIDEO_EXTS)
    except (NotADirectoryError, FileNotFoundError):
        raise HTTPException(404, "Folder not found")


# ===========================================================================
# BlueROV2
# ===========================================================================

@app.get("/api/rov/status")
def rov_status():
    return {**rov.status(), "telemetry": rov.telemetry(), "events": rov.recent_events(10)}


# ===========================================================================
# PIPELINE A - LIVE OPS
# ===========================================================================

class LiveStartRequest(BaseModel):
    source: str
    label: Optional[str] = None
    line_id: Optional[str] = None
    trigger_conf: Optional[float] = None
    iou: Optional[float] = None
    recording: bool = True
    detection_mode: Optional[str] = None


class LiveConfigRequest(BaseModel):
    line_id: Optional[str] = None
    trigger_conf: Optional[float] = None
    iou: Optional[float] = None


class ToggleRequest(BaseModel):
    enabled: bool


class ModeRequest(BaseModel):
    mode: str


@app.get("/api/live/sources")
def live_sources(db: Session = Depends(get_db)):
    files = (db.query(VideoRecord).filter(VideoRecord.source_type.in_(["LOCAL_UPLOAD", "LOCAL_IMPORT"]))
             .order_by(VideoRecord.created_at.desc()).all())
    reps = [{"value": f"video:{v.id}", "label": f"Replay as live - {v.display_name or v.original_filename}",
             "kind": "replay"} for v in files if v.current_filepath and os.path.exists(v.current_filepath)]
    test = [{"value": TEST_STREAM_URL, "label": f"TEST ROV camera - {test_mode.video_name} (simulated BlueOS UDP stream)",
             "kind": "test"}] if test_mode.enabled else []
    return test + [{"value": "rtsp://192.168.2.2:8554/", "label": "BlueROV2 camera (BlueOS RTSP - edit path)", "kind": "network"},
                   {"value": "0", "label": "Webcam 0 (USB / built-in)", "kind": "webcam"}] + reps


@app.post("/api/live/start")
def live_start(req: LiveStartRequest):
    if not req.source.strip():
        raise HTTPException(400, "Source is required")
    label = req.label
    if req.source == TEST_STREAM_URL:
        if not test_mode.enabled:
            raise HTTPException(409, "Test Mode is off - enable it first")
        label = label or f"TEST ROV camera ({test_mode.video_name})"
    return live.start(req.source, label, req.line_id, req.trigger_conf, req.iou, req.recording, req.detection_mode)


@app.post("/api/live/stop")
def live_stop():
    return live.stop()


@app.post("/api/live/recording")
def live_recording(req: ToggleRequest):
    live.set_recording(req.enabled)
    return live.snapshot()


@app.post("/api/live/detection-mode")
def live_detection_mode(req: ModeRequest):
    try:
        live.set_detection_mode(req.mode.upper())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return live.snapshot()


@app.post("/api/live/config")
def live_config(req: LiveConfigRequest):
    live.configure(req.line_id, req.trigger_conf, req.iou)
    return live.snapshot()


@app.get("/api/live/dives")
def recent_live_dives(limit: int = 5, db: Session = Depends(get_db)):
    """Recent live dives (sessions), newest first - so a dive can be reported after the stream stops."""
    rows = (db.query(VideoRecord).filter(VideoRecord.source_type == "LIVE_SESSION")
            .order_by(VideoRecord.created_at.desc()).limit(max(1, min(limit, 20))).all())
    counts = _defect_counts(db, [v.active_run_id for v in rows])
    out = []
    for v in rows:
        clips = db.query(func.count(VideoRecord.id)).filter(VideoRecord.parent_video_id == v.id).scalar() or 0
        out.append({"id": v.id, "name": v.display_name, "status": v.status, "lineId": v.line_id,
                    "startedAt": local_str(v.started_at), "durationSec": v.duration_sec,
                    "defectCount": counts.get(v.active_run_id, 0), "clips": clips, "isTest": bool(v.is_test)})
    return out


@app.get("/api/live/state")
def live_state():
    return live.snapshot()


@app.get("/api/live/health")
def live_health():
    return {"capture": live.capture_health(), "clipsWritten": live.clips_written,
            "lastClipWriteMs": live.last_clip_write_ms, "framesInferred": live.frames_inferred,
            "arbiter": arbiter.stats()}


@app.get("/api/live/mjpeg")
def live_mjpeg():
    if not live.is_active:
        raise HTTPException(409, "No live stream connected")
    return StreamingResponse(live.mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"})


@app.get("/api/live/snapshot.jpg")
def live_snapshot():
    jpg = live.latest_jpeg()
    if not jpg:
        raise HTTPException(404, "No frame yet")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.websocket("/ws/telemetry")
async def ws_telemetry(websocket: WebSocket):
    await websocket.accept()
    await websocket.send_json({"type": "connection_status", "status": "connected"})
    last_version, last_incident, last_rov = -1, live.incident_seq, 0.0

    async def receiver():
        while True:
            await websocket.receive_json()
    recv = asyncio.create_task(receiver())
    try:
        while not recv.done():
            now = time.monotonic()
            if live.state_version != last_version:
                last_version = live.state_version
                await websocket.send_json(live.snapshot())
            elif not live.is_active and now - last_rov >= 0.5:   # ROV panel stays live without a video stream
                last_rov = now
                await websocket.send_json({"type": "rov", **rov.status(), "telemetry": rov.telemetry()})
            if live.incident_seq != last_incident:
                for ev in list(live.incidents):
                    if ev["seq"] > last_incident:
                        await websocket.send_json({"type": "incident", **ev})
                last_incident = live.incident_seq
            await asyncio.sleep(1 / 15)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        recv.cancel()


# ===========================================================================
# MODEL
# ===========================================================================

class ModelLoadRequest(BaseModel):
    model_path: str


@app.get("/api/model/status")
def model_status():
    return {**model_manager.status(), "inferenceFloor": INFERENCE_CONF_FLOOR, "liveLogConf": LIVE_LOG_CONF}


@app.get("/api/model/files")
def model_files():
    return {"success": True, "files": [m["name"] for m in model_manager.list_available()],
            "details": model_manager.list_available()}


@app.post("/api/model/load")
def load_model(req: ModelLoadRequest):
    allowed = {m["name"] for m in model_manager.list_available()}
    name = req.model_path.strip()
    if name not in allowed and (os.path.basename(name) != name or not name.endswith(".pt")):
        raise HTTPException(400, f"'{name}' is not an available weights file")
    result = model_manager.load(name)
    msg = (f"Initialised '{result['model_path']}' on {result['device']}" if result.get("success")
           else result.get("last_error") or f"Failed to load '{name}'")
    return {**result, "message": msg}


# ===========================================================================
# DEFECT REGISTRY (active analysis runs only)
# ===========================================================================

@app.get("/api/history")
def get_history(line_id: Optional[str] = None, test: bool = False, db: Session = Depends(get_db)):
    """Counts from each inspection's ACTIVE run (live dives + processed dives). Incident
    clips are excluded - their detections duplicate the live session they came from."""
    # real and test data never mix: test=1 shows only Test Mode media, the default only real media
    lf = ("AND v.line_id = :line_id " if line_id else "") + "AND COALESCE(v.is_test, FALSE) = :test"
    p = {"gap": EVENT_GAP_SEC, "line_id": line_id, "test": bool(test)}
    base = f"FROM defects d JOIN videos v ON v.id = d.video_id AND d.run_id = v.active_run_id " \
           f"WHERE v.source_type != 'INCIDENT_CLIP' {lf}"
    events = dict(db.execute(text(f"""SELECT defect_type, COUNT(*) FROM (
        SELECT d.defect_type, d.timestamp_sec - LAG(d.timestamp_sec) OVER (
            PARTITION BY d.run_id, d.defect_type ORDER BY d.timestamp_sec) AS gap {base}) AS g
        WHERE gap IS NULL OR gap > :gap GROUP BY defect_type"""), p).all())
    dets = dict(db.execute(text(f"SELECT d.defect_type, COUNT(*) {base} GROUP BY d.defect_type"), p).all())
    fr = db.execute(text(f"""SELECT COALESCE(SUM(r.frames_analyzed),0), COALESCE(SUM(r.frames_with_defects),0)
        FROM analysis_runs r JOIN videos v ON v.active_run_id = r.id WHERE v.source_type != 'INCIDENT_CLIP' {lf}"""),
                    p).one()
    inspections = db.execute(text(f"""SELECT COUNT(*) FROM videos v WHERE v.active_run_id IS NOT NULL
        AND v.source_type != 'INCIDENT_CLIP' {lf}"""), p).scalar()
    out = {"counts": {"CORROSION": events.get("CORROSION", 0), "DEFORMATION": events.get("DEFORMATION", 0)},
           "detections": {"CORROSION": dets.get("CORROSION", 0), "DEFORMATION": dets.get("DEFORMATION", 0)},
           "framesAnalyzed": fr[0], "framesWithDefects": fr[1], "framesClean": fr[0] - fr[1],
           "inspections": inspections, "eventGapSec": EVENT_GAP_SEC}
    if line_id:   # per depth zone: each event goes to the segment of the ROV depth at its peak frame
        vids = db.execute(text(f"""SELECT v.id, v.active_run_id FROM videos v WHERE v.active_run_id IS NOT NULL
            AND v.source_type != 'INCIDENT_CLIP' {lf}"""), p).all()
        out.update(segments.line_breakdown(db, [(a, b) for a, b in vids], assets.get(line_id)))
    return out


@app.post("/api/history/clear")
def clear_history(db: Session = Depends(get_db)):
    """Demo reset: removes all analysis results (runs + detections). Media, sensor logs
    and reports' source files are kept; dive videos return to UNPROCESSED."""
    if batch.current_id or live.is_active:
        raise HTTPException(409, "Stop the live stream and wait for batch jobs to finish first")
    db.query(DefectRecord).delete()
    db.query(AnalysisRun).delete()
    sensors_db.delete_objects(all_runs=True)
    _incident_cache.clear()
    db.query(VideoRecord).update({"active_run_id": None}, synchronize_session=False)
    db.query(VideoRecord).filter(VideoRecord.source_type.in_(["LOCAL_UPLOAD", "LOCAL_IMPORT"]),
                                 VideoRecord.status.in_(["COMPLETED", "FAILED"])).update(
        {"status": "UNPROCESSED", "progress": 0.0, "frames_analyzed": 0}, synchronize_session=False)
    db.commit()
    return {"success": True}


# ===========================================================================
# SYSTEM / HARDWARE
# ===========================================================================

_nvml = None
try:
    import pynvml
    pynvml.nvmlInit()
    _nvml = pynvml.nvmlDeviceGetHandleByIndex(0)
except Exception:
    _nvml = None


@app.get("/api/system/stats")
def system_stats():
    gpu = {"name": None, "tempC": None, "powerW": None, "vramUsedGB": None, "vramTotalGB": None, "utilPct": None}
    if _nvml is not None:
        try:
            mem = pynvml.nvmlDeviceGetMemoryInfo(_nvml)
            name = pynvml.nvmlDeviceGetName(_nvml)
            gpu.update({"name": name.decode() if isinstance(name, bytes) else name,
                        "tempC": pynvml.nvmlDeviceGetTemperature(_nvml, pynvml.NVML_TEMPERATURE_GPU),
                        "powerW": round(pynvml.nvmlDeviceGetPowerUsage(_nvml) / 1000, 1),
                        "vramUsedGB": round(mem.used / 1024 ** 3, 2), "vramTotalGB": round(mem.total / 1024 ** 3, 2),
                        "utilPct": pynvml.nvmlDeviceGetUtilizationRates(_nvml).gpu})
        except Exception:
            pass
    elif model_manager.diagnostics.get("device_name"):
        gpu["name"] = model_manager.diagnostics["device_name"]
    snap = live.snapshot()
    return {"uptimeSec": round(time.time() - BOOT_TIME), "device": model_manager.device, "gpu": gpu,
            "arbiter": arbiter.stats(),
            "live": {k: snap[k] for k in ("status", "captureFps", "inferenceFps", "inferenceMs", "framesSkipped",
                                          "lateFrames", "bufferFrames", "bufferSeconds", "armed")},
            "batch": {"currentId": batch.current_id, "queued": batch.queue_length(),
                      "framesPerSec": round(batch.current_fps, 1), "lastJob": batch.last_job},
            "rov": rov.status(), "testMode": test_mode.status()}


# ===========================================================================
# AI INSPECTION REPORTS (Gemini) - token-saver enforced on the server
# ===========================================================================

_ai_lock = threading.Lock()
_ai_state = {"last_ok": 0.0, "in_flight": False}


def _cooldown_remaining() -> float:
    return max(0.0, GEMINI_COOLDOWN_SEC - (time.monotonic() - _ai_state["last_ok"])) if _ai_state["last_ok"] else 0.0


class ReportRequest(BaseModel):
    scope_type: str = Field(description="INCIDENT | DIVE | SEGMENT")
    scope_ref: str = Field(description="video id, or 'LINE_ID|Segment Name' for SEGMENT")
    use_ai: bool = True


# ===========================================================================
# TEST MODE (no vehicle / no trained model): simulated ROV camera + MAVLink telemetry
# ===========================================================================

class TestStartRequest(BaseModel):
    video_id: Optional[str] = None            # a dive already in the vault ...
    path: Optional[str] = None                # ... or any file on this PC (file browser)
    target_depth: float = 120.5
    descent_rate: float = 1.0
    start_depth: float = 0.0
    stand_in: dict[str, str] = Field(default_factory=dict, description='e.g. {"person": "DEFORMATION"}')


class TestDepthRequest(BaseModel):
    depth: float
    rate: Optional[float] = None


class StandInRequest(BaseModel):
    stand_in: dict[str, str]


@app.get("/api/test-mode")
def test_status():
    return test_mode.status()


@app.post("/api/test-mode/start")
def test_start(req: TestStartRequest, request: Request, db: Session = Depends(get_db)):
    if req.video_id:
        v = db.get(VideoRecord, req.video_id)
        path = v.current_filepath if v else None
    elif req.path:
        _local_only(request)
        path = req.path
    else:
        raise HTTPException(400, "Choose a video for the simulated ROV camera")
    if live.is_active and not live.is_test:
        raise HTTPException(409, "A real live dive is running - disconnect it before enabling Test Mode")
    try:
        return test_mode.start(path or "", req.target_depth, req.descent_rate, req.stand_in, req.start_depth)
    except (ValueError, RuntimeError) as e:
        raise HTTPException(409 if isinstance(e, RuntimeError) else 400, str(e))


@app.post("/api/test-mode/stop")
def test_stop():
    if live.is_active and live.is_test:
        live.stop()                     # a test dive cannot outlive its simulated vehicle
    return test_mode.stop()


@app.post("/api/test-mode/depth")
def test_depth(req: TestDepthRequest):
    try:
        return test_mode.set_depth(req.depth, req.rate)
    except RuntimeError as e:
        raise HTTPException(409, str(e))


@app.put("/api/test-mode/stand-in")
def test_stand_in(req: StandInRequest):
    if live.is_active and live.is_test:
        raise HTTPException(409, "Disconnect the test stream to change stand-in classes (each dive records the mapping it used)")
    try:
        return test_mode.set_stand_in(req.stand_in)
    except ValueError as e:
        raise HTTPException(400, str(e))


def _ai_key_info() -> dict:
    key, source = gemini_key()
    raw = os.environ.get("GEMINI_API_KEY") or key or ""
    key = key or ""
    kind = "none" if not key else "auth key (AQ.)" if key.startswith("AQ.") else "standard key (AIza)" if key.startswith("AIza") else "unknown format"
    warn = None
    if raw and raw != raw.strip():
        warn = "The key has spaces or a line break around it - remove them."
    elif raw and raw.strip() != key:
        warn = "The key is wrapped in quotes - remove them."
    try:
        from importlib.metadata import version
        sdk = version("google-genai")
    except Exception:                       # noqa: BLE001
        try:
            from google import genai        # the test double / unusual installs
            sdk = getattr(genai, "__version__", "installed")
        except Exception:                   # noqa: BLE001
            sdk = None
    return {"keyKind": kind, "keyWarning": warn, "sdk": sdk, "keySource": source}


@app.get("/api/ai/status")
def ai_status():
    return {"configured": bool(gemini_key()[0]), "model": gemini_model()[0], "modelSource": gemini_model()[1],
            "cooldownRemaining": round(_cooldown_remaining(), 1), "cooldownSec": GEMINI_COOLDOWN_SEC,
            **_ai_key_info(), "lastError": rpt.last_ai_error()}


_ai_test_at = [0.0]


@app.post("/api/ai/test")
def ai_test():
    """One tiny Gemini request (a few tokens) to check the key and the model."""
    if not gemini_key()[0]:
        return {"ok": False, "message": "No GEMINI_API_KEY found (environment, .env file or Windows variables).",
                "hint": "Put GEMINI_API_KEY=your-key in a file named .env in the app folder, or set it in Windows Environment Variables.", **_ai_key_info()}
    if time.monotonic() - _ai_test_at[0] < 5:
        raise HTTPException(429, "Wait a few seconds between connection tests")
    _ai_test_at[0] = time.monotonic()
    return {**rpt.test_ai(), **_ai_key_info(), "model": gemini_model()[0]}


class ModelChoice(BaseModel):
    model: str


@app.get("/api/ai/models")
def ai_models():
    if not gemini_key()[0]:
        return {"models": list(GEMINI_KNOWN_MODELS), "source": "built-in list (no key)", "current": gemini_model()[0]}
    return {**rpt.list_models(), "current": gemini_model()[0], "currentSource": gemini_model()[1]}


@app.put("/api/ai/model")
def ai_set_model(req: ModelChoice):
    m = req.model.strip().replace("models/", "")
    if not re.fullmatch(r"gemini-[\w.\-]+", m):
        raise HTTPException(400, "Not a Gemini model id")
    if os.environ.get("GEMINI_MODEL"):
        raise HTTPException(409, "GEMINI_MODEL is set as an environment variable; it overrides the app setting. Remove it to choose here.")
    save_gemini_model(m)
    _ai_test_at[0] = 0.0                 # a new model may be tested straight away
    return {"model": gemini_model()[0], "modelSource": gemini_model()[1]}


@app.post("/api/reports")
def create_report(req: ReportRequest, db: Session = Depends(get_db)):
    use_ai = req.use_ai and bool(gemini_key()[0])
    if use_ai:
        with _ai_lock:
            remaining = _cooldown_remaining()
            if remaining > 0 or _ai_state["in_flight"]:
                wait = max(1, int(remaining + 0.999))
                return JSONResponse(status_code=429, headers={"Retry-After": str(wait)},
                                    content={"detail": f"AI cooldown active - retry in {wait}s", "cooldownSec": wait})
            _ai_state["in_flight"] = True
    try:
        rec = rpt.generate(db, req.scope_type.upper(), req.scope_ref, use_ai, test=test_mode.enabled)
        if use_ai:
            with _ai_lock:
                _ai_state["last_ok"] = time.monotonic()
        return rpt.to_dict(rec)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(503, f"Report generation failed: {e}")
    finally:
        if use_ai:
            with _ai_lock:
                _ai_state["in_flight"] = False


# ---- report jobs: generation runs in the background so the UI can show real progress ----------------
# Stage -> (progress at start, progress at end, typical seconds). The AI stage uses the measured average.
_JOB_STAGES = {"queued": (0.0, 0.03, 1), "facts": (0.03, 0.12, 1.5), "keyframes": (0.12, 0.30, 4),
               "ai": (0.30, 0.82, 25), "check": (0.82, 0.86, 0.5), "retry": (0.86, 0.93, 25),
               "rules": (0.30, 0.90, 1.5), "save": (0.93, 0.99, 0.5)}
_report_jobs: dict[str, dict] = {}
_job_lock = threading.Lock()
_ai_durations: list[float] = []


def _job_json(job: dict) -> dict:
    now = time.time()
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    out["elapsedSec"] = round((job.get("_end") or now) - job["_start"], 1)
    if job["status"] == "running":
        a, b, typ = _JOB_STAGES.get(job["stage"], (0.0, 0.99, 5))
        if job["stage"] in ("ai", "retry") and _ai_durations:
            typ = sum(_ai_durations) / len(_ai_durations)
        in_stage = now - job["_stageAt"]
        out["progress"] = round(a + (b - a) * (1 - math.exp(-in_stage / max(typ, 0.1))), 3)   # never claims "done" early
        out["typicalAiSec"] = round(sum(_ai_durations) / len(_ai_durations)) if _ai_durations else None
    else:
        out["progress"] = 1.0 if job["status"] == "done" else out.get("progress", 0)
    return out


def _run_report_job(jid: str, scope_type: str, scope_ref: str, use_ai: bool, test: bool, replace_id: str | None = None):
    job = _report_jobs[jid]

    def step(stage: str, label: str):
        if stage == "check" and job.get("stage") in ("ai", "retry") and job.get("_aiAt"):   # successful AI calls only
            _ai_durations.append(time.time() - job["_aiAt"])
            del _ai_durations[:-8]
        job.update(stage=stage, label=label, _stageAt=time.time())
        if stage in ("ai", "retry"):
            job["_aiAt"] = time.time()

    db = SessionLocal()
    try:
        replace = db.get(ReportRecord, replace_id) if replace_id else None
        rec = rpt.generate(db, scope_type, scope_ref, use_ai, test=test, progress=step, replace=replace)
        if use_ai:
            with _ai_lock:
                _ai_state["last_ok"] = time.monotonic()
        d = rpt.to_dict(rec, full=False)
        job.update(status="done", stage="done", label="Draft ready", reportId=rec.id, generator=d["generator"],
                   conditionGrade=d["conditionGrade"], reportStatus=d["status"], _end=time.time())
    except rpt.RedraftFailed as e:                           # Gemini still busy: the report is left as it was
        with _ai_lock:
            _ai_state["last_ok"] = time.monotonic()
        job.update(status="error", errorStatus=503, error=str(e), reportId=replace_id, _end=time.time())
    except ValueError as e:
        job.update(status="error", errorStatus=400, error=str(e), _end=time.time())
    except Exception as e:                                   # noqa: BLE001
        job.update(status="error", errorStatus=503, error=f"Report generation failed: {e}", _end=time.time())
    finally:
        db.close()
        if use_ai:
            with _ai_lock:
                _ai_state["in_flight"] = False


@app.post("/api/reports/jobs", status_code=202)
def start_report_job(req: ReportRequest, db: Session = Depends(get_db)):
    """Starts a report in the background; poll GET /api/reports/jobs/{id} for stage and progress."""
    scope_type = req.scope_type.upper()
    if scope_type not in ("INCIDENT", "DIVE", "SEGMENT"):
        raise HTTPException(400, "scope_type must be INCIDENT, DIVE or SEGMENT")
    use_ai = req.use_ai and bool(gemini_key()[0])
    if use_ai:
        with _ai_lock:
            remaining = _cooldown_remaining()
            if remaining > 0 or _ai_state["in_flight"]:
                wait = max(1, int(remaining + 0.999))
                return JSONResponse(status_code=429, headers={"Retry-After": str(wait)},
                                    content={"detail": f"AI cooldown active - retry in {wait}s", "cooldownSec": wait})
            _ai_state["in_flight"] = True
    jid = str(uuid.uuid4())
    now = time.time()
    with _job_lock:
        for k in [k for k, j in _report_jobs.items() if j["status"] != "running" and now - j["_start"] > 3600]:
            del _report_jobs[k]                              # keep finished jobs for an hour
        _report_jobs[jid] = {"id": jid, "scopeType": scope_type, "scopeRef": req.scope_ref, "useAi": use_ai,
                             "model": gemini_model()[0] if use_ai else "RULE_BASED", "status": "running",
                             "stage": "queued", "label": "Starting", "startedAt": local_str(utcnow(), "%Y-%m-%d %H:%M:%S"),
                             "_start": now, "_stageAt": now}
    threading.Thread(target=_run_report_job, args=(jid, scope_type, req.scope_ref, use_ai, test_mode.enabled),
                     daemon=True, name=f"report-{jid[:8]}").start()
    return _job_json(_report_jobs[jid])


@app.post("/api/reports/{report_id}/redraft", status_code=202)
def redraft_report(report_id: str, db: Session = Depends(get_db)):
    """Asks Gemini again for an unapproved report (e.g. one drafted by the rules while Gemini was busy) and
    replaces its draft in place - same report, the engineer's notes are kept. Runs as a report job."""
    rec = db.get(ReportRecord, report_id)
    if not rec:
        raise HTTPException(404, "Report not found")
    if rec.status == "APPROVED":
        raise HTTPException(409, "This report is approved and locked; draft a new report instead.")
    if not gemini_key()[0]:
        raise HTTPException(400, "No Gemini key is set on the backend, so the report cannot be redrafted with Gemini.")
    if any(j["status"] == "running" and j.get("redraftOf") == report_id for j in _report_jobs.values()):
        raise HTTPException(409, "This report is already being redrafted.")
    with _ai_lock:
        remaining = _cooldown_remaining()
        if remaining > 0 or _ai_state["in_flight"]:
            wait = max(1, int(remaining + 0.999))
            return JSONResponse(status_code=429, headers={"Retry-After": str(wait)},
                                content={"detail": f"AI cooldown active - retry in {wait}s", "cooldownSec": wait})
        _ai_state["in_flight"] = True
    test = bool(json.loads(rec.facts_json or "{}").get("test_data"))
    jid = str(uuid.uuid4())
    now = time.time()
    with _job_lock:
        _report_jobs[jid] = {"id": jid, "scopeType": rec.scope_type, "scopeRef": rec.scope_ref, "useAi": True,
                             "model": gemini_model()[0], "status": "running", "redraftOf": report_id,
                             "stage": "queued", "label": "Starting", "startedAt": local_str(utcnow(), "%Y-%m-%d %H:%M:%S"),
                             "_start": now, "_stageAt": now}
    threading.Thread(target=_run_report_job, args=(jid, rec.scope_type, rec.scope_ref, True, test, report_id),
                     daemon=True, name=f"redraft-{jid[:8]}").start()
    return _job_json(_report_jobs[jid])


# ---- settings file (config.toml) ------------------------------------------------------------------
@app.get("/api/config")
def get_config():
    """Where the settings file is, whether it was read, and which values came from it (never the API key)."""
    import config as cfg
    status = {**cfg.CONFIG_STATUS, "used": {k: (cfg.redact_url(v) if isinstance(v, str) and "://" in v else v)
                                            for k, v in cfg.CONFIG_STATUS["used"].items()}}
    import database as _db
    status["database"] = {"main": cfg.redact_url(cfg.DATABASE_URL), "sensors": cfg.redact_url(cfg.SENSOR_DATABASE_URL),
                          "dialect": _db.engine.dialect.name}
    values, sources = dict(cfg.CONFIG_STATUS["values"]), dict(cfg.CONFIG_STATUS["sources"])
    model, where = cfg.gemini_model()                         # the app's own choice wins over the file
    values["gemini.model"] = model
    if where == "chosen in the app":
        sources["gemini.model"] = "app"
    problem_keys = {k for p in cfg.CONFIG_STATUS["problems"] for k in p.get("keys", [p["key"]])}
    return {**status, "example": "config.example.toml", "reference": "docs/CONFIG.md",
            "sections": [{"key": k, "title": t, "intro": i} for k, (t, i) in cfg.SECTIONS.items()],
            "spec": [{"env": s.env, "key": s.key, "section": s.section, "name": s.name, "title": s.title, "help": s.what,
                      "what": s.what, "tune": s.tune, "unit": s.unit, "allowed": s.range_text(), "advanced": s.advanced,
                      "default": cfg._shown(s, cfg._default_of(s)) if s.type != "path" else s.default,
                      "value": values.get(s.key), "source": sources.get(s.key, "default"),
                      "problem": s.key in problem_keys,
                      "fromFile": sources.get(s.key) == "file", "fromEnv": sources.get(s.key) == "env"}
                     for s in cfg.SETTINGS]}


@app.post("/api/config/create")
def create_config(request: Request):
    """Writes config.toml with every setting commented out (all defaults) - only if there is none yet."""
    _local_only(request)
    import config as cfg
    if cfg.CONFIG_FILE.exists():
        raise HTTPException(409, f"{cfg.CONFIG_FILE} already exists - edit it in a text editor.")
    cfg.CONFIG_FILE.write_text(cfg.config_example(), encoding="utf-8")
    return {"file": str(cfg.CONFIG_FILE), "created": True}


@app.get("/api/reports/jobs")
def list_report_jobs():
    """Running jobs and those finished in the last hour (so a page reload can pick them up again)."""
    return [_job_json(j) for j in sorted(_report_jobs.values(), key=lambda j: -j["_start"])]


@app.get("/api/reports/jobs/{job_id}")
def get_report_job(job_id: str):
    job = _report_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Report job not found")
    return _job_json(job)


@app.get("/api/reports")
def list_reports(scope_type: Optional[str] = None, scope_ref: Optional[str] = None, limit: int = 50,
                 db: Session = Depends(get_db)):
    q = db.query(ReportRecord)
    if scope_type:
        q = q.filter(ReportRecord.scope_type == scope_type.upper())
    if scope_ref:
        q = q.filter(ReportRecord.scope_ref == scope_ref)
    return [rpt.to_dict(r, full=False) for r in q.order_by(ReportRecord.created_at.desc()).limit(max(1, min(limit, 2000)))]


@app.get("/api/reports/{report_id}")
def get_report(report_id: str, db: Session = Depends(get_db)):
    r = db.get(ReportRecord, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    return rpt.to_dict(r)


@app.put("/api/reports/{report_id}")
def edit_report(report_id: str, edit: dict, db: Session = Depends(get_db)):
    r = db.get(ReportRecord, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    if r.status == "APPROVED":
        raise HTTPException(409, "Approved reports are locked")
    try:
        rpt.apply_edit(r, edit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.commit()
    return rpt.to_dict(r)


class ApproveRequest(BaseModel):
    engineer: str


@app.post("/api/reports/{report_id}/approve")
def approve_report(report_id: str, req: ApproveRequest, db: Session = Depends(get_db)):
    r = db.get(ReportRecord, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    if not req.engineer.strip():
        raise HTTPException(400, "Engineer name is required for sign-off")
    r.status, r.engineer, r.approved_at = "APPROVED", req.engineer.strip(), utcnow()
    db.commit()
    event_log.app("Report", f"Report approved by {r.engineer}: {r.title} ({r.scope_type}, id {r.id[:8]})")
    return rpt.to_dict(r)


@app.get("/api/reports/{report_id}/html", response_class=HTMLResponse)
def report_html(report_id: str, db: Session = Depends(get_db)):
    r = db.get(ReportRecord, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    return rpt.render_html(r)


# ---- PDF export + client templates ------------------------------------------------------------------
def _pdf_template(tid: Optional[str]) -> dict:
    import report_pdf
    tid = tid or report_pdf.default_id()[0]
    try:
        return report_pdf.load(tid)
    except KeyError:
        raise HTTPException(404, f"Report template '{tid}' not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


def _pdf_response(pdf: bytes, filename: str, download: bool) -> Response:
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{filename}"',
                             "Cache-Control": "no-store"})


@app.get("/api/reports/{report_id}/pdf")
def report_pdf_export(report_id: str, template: Optional[str] = None, download: int = 1, db: Session = Depends(get_db)):
    """The report as a real PDF, styled by a client template (default: the one chosen in System -> Report templates)."""
    import report_pdf
    r = db.get(ReportRecord, report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    t = _pdf_template(template)
    view = report_pdf.view_of(r)
    try:
        pdf = report_pdf.render_pdf(view, t)
    except ImportError:
        raise HTTPException(503, "PDF export needs reportlab on the backend PC: pip install reportlab")
    return _pdf_response(pdf, report_pdf.file_name(view, t), bool(download))


@app.get("/api/report-templates")
def report_templates_list():
    import report_pdf
    tid, src = report_pdf.default_id()
    return {"templates": report_pdf.list_templates(), "default": tid, "defaultSource": src,
            "dir": str(report_pdf.templates_dir())}


@app.put("/api/report-templates/{tid}")
def report_template_save(tid: str, body: dict):
    import report_pdf
    try:
        return report_pdf.save(tid, body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/report-templates/{tid}")
def report_template_delete(tid: str):
    import report_pdf
    try:
        report_pdf.delete(tid)
    except KeyError:
        raise HTTPException(404, "Template not found")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"deleted": tid}


@app.post("/api/report-templates/{tid}/logo")
async def report_template_logo(tid: str, request: Request, x_filename: str = Header("logo.png")):
    """Raw image body (PNG / JPG, < 3 MB); the file name comes in the X-Filename header."""
    import report_pdf
    data = await request.body()
    try:
        return report_pdf.save_logo(tid, x_filename, data)
    except KeyError:
        raise HTTPException(404, "Template not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/report-templates/{tid}/logo")
def report_template_logo_remove(tid: str):
    import report_pdf
    t = _pdf_template(tid)
    old = t.get("logo")
    t["logo"] = None
    out = report_pdf.save(tid, t)
    if old and not any(o.get("logo") == old for o in report_pdf.list_templates()):
        (report_pdf.templates_dir() / old).unlink(missing_ok=True)
    return out


@app.get("/api/report-templates/{tid}/logo")
def report_template_logo_get(tid: str):
    import report_pdf
    t = _pdf_template(tid)
    p = report_pdf.templates_dir() / (t.get("logo") or "")
    if not t.get("logo") or not p.exists():
        raise HTTPException(404, "No logo")
    return FileResponse(p, headers={"Cache-Control": "no-store"})


@app.post("/api/report-templates/default")
def report_template_default(body: dict):
    import report_pdf
    try:
        report_pdf.set_default(str(body.get("id") or ""))
    except KeyError:
        raise HTTPException(404, "Template not found")
    return {"default": report_pdf.default_id()[0]}


@app.get("/api/report-templates/{tid}/preview")
def report_template_preview(tid: str, report: Optional[str] = None, db: Session = Depends(get_db)):
    """The template applied to a report (the given one, else the newest, else a built-in sample)."""
    import report_pdf
    t = _pdf_template(tid)
    r = db.get(ReportRecord, report) if report else db.query(ReportRecord).order_by(ReportRecord.created_at.desc()).first()
    view = report_pdf.view_of(r) if r else report_pdf.sample_view()
    try:
        pdf = report_pdf.render_pdf(view, t)
    except ImportError:
        raise HTTPException(503, "PDF export needs reportlab on the backend PC: pip install reportlab")
    return _pdf_response(pdf, f"preview_{tid}.pdf", False)


@app.get("/api/reports/{report_id}/frames/{name}")
def report_frame(report_id: str, name: str):
    p = REPORTS_DIR / report_id / os.path.basename(name)
    if not p.exists():
        raise HTTPException(404, "Frame not found")
    return FileResponse(p, media_type="image/jpeg")


# ---- event log paper trail -----------------------------------------------------------------------------
class LogEntries(BaseModel):
    entries: list = Field(default_factory=list)        # bad items are skipped, not refused (a log must not lose a batch)
    client: str = ""


@app.post("/api/logs")
def write_log(req: LogEntries):
    """The browser's event-log entries, appended to this session's text file (event_log.py)."""
    return {"written": event_log.ui(req.entries, req.client), "file": event_log.SESSION_NAME}


@app.get("/api/logs")
def list_logs():
    import config as cfg
    return {"current": event_log.SESSION_NAME, "folder": str(cfg.LOGS_DIR), "sessions": event_log.sessions()}


@app.get("/api/logs/{name}")
def get_log(name: str, download: bool = False):
    p = event_log.path_for(name)
    if not p:
        raise HTTPException(404, "No such session log")
    return FileResponse(p, media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'{"attachment" if download else "inline"}; filename="{p.name}"'})


# ---- batch processing benchmark ------------------------------------------------------------------------
TIMING_COLUMNS = [("video", None), ("line", None), ("run", None), ("status", None), ("threshold", None),
                  ("video_s", "videoSec"), ("resolution", "resolution"), ("codec", "codec"), ("video_fps", "videoFps"),
                  ("sample_fps", "sampleFps"), ("frames", "frames"), ("processing_s", "processingSec"),
                  ("speed_x_realtime", "speedX"), ("frames_per_s", "framesPerSec"), ("proxy_s", "proxySec"),
                  ("total_s", "totalSec"), ("queued_s", "queuedSec"), ("resumes", "resumes"),
                  ("live_shared_s", "liveSharedSec"),
                  ("detect_s", "detectSec"), ("wait_for_video_s", "waitSec"), ("read_ahead_frames", "prefetch"), ("reader", "reader"), ("batch_size", "batchSize"),
                  ("model", "model"), ("device", "device"), ("started_utc", "startedAt"), ("finished_utc", "finishedAt")]


@app.get("/api/batch/timings.csv")
def batch_timings_csv(db: Session = Depends(get_db)):
    """Processing time of every batch analysis (first import and re-analyses), for benchmarking."""
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c for c, _ in TIMING_COLUMNS])
    rows = (db.query(AnalysisRun, VideoRecord).join(VideoRecord, VideoRecord.id == AnalysisRun.video_id)
            .filter(AnalysisRun.kind.in_(["BATCH", "REANALYSIS"]), AnalysisRun.timing.isnot(None))
            .order_by(AnalysisRun.created_at).all())
    for r, v in rows:
        t = json.loads(r.timing)
        fixed = {"video": v.display_name or v.original_filename, "line": v.line_id, "run": r.kind,
                 "status": r.status, "threshold": r.conf}
        w.writerow([fixed[c] if key is None else t.get(key) for c, key in TIMING_COLUMNS])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="batch_processing_times.csv"'})


# ---- live dive log -------------------------------------------------------------------------------------
def _live_dive(db: Session, dive_id: str) -> VideoRecord:
    v = db.get(VideoRecord, dive_id)
    if not v or v.source_type != "LIVE_SESSION":
        raise HTTPException(404, "Live dive not found")
    return v


@app.get("/api/dives")
def list_dives(limit: int = 200, db: Session = Depends(get_db)):
    """Every live dive (newest first) with a light summary - for the Review library's 'Live dive logs'."""
    import dive_log
    rows = (db.query(VideoRecord).filter(VideoRecord.source_type == "LIVE_SESSION")
            .order_by(VideoRecord.created_at.desc()).limit(max(1, min(limit, 2000))).all())
    return [dive_log.summary_row(db, v, live) for v in rows]


@app.get("/api/dives/{dive_id}/log")
def get_dive_log(dive_id: str, points: int = 2000, db: Session = Depends(get_db)):
    """Everything recorded about one live dive: thinned sensor series, vehicle events, sightings with depth and
    zone, incident clips, operator changes, summary and the .tlog files. Works while the dive is still running."""
    import dive_log
    v = _live_dive(db, dive_id)
    return dive_log.build(db, v, assets.get(v.line_id), live, points=max(60, min(points, 20000)))


@app.get("/api/dives/{dive_id}/export/{kind}.csv")
def export_dive_csv(dive_id: str, kind: str, db: Session = Depends(get_db)):
    """CSV for the report or Excel: sensors (every reading, not thinned) | sightings | events (vehicle + operator + clips)."""
    import dive_log
    if kind not in ("sensors", "sightings", "events"):
        raise HTTPException(404, "kind must be sensors, sightings or events")
    v = _live_dive(db, dive_id)
    log = dive_log.build(db, v, assets.get(v.line_id), live, points=60)
    full = [{"t": round(r["t"], 3), **{k: r.get(k) for k in dive_log.SERIES_FIELDS}} for r in sensors_db.samples_for(v.id)] \
        if kind == "sensors" else None
    body = dive_log.to_csv(kind, log, full)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", v.display_name or v.id).strip("_")
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{stem}_{kind}.csv"'})


@app.get("/api/dives/{dive_id}/tlog/{name}")
def download_dive_tlog(dive_id: str, name: str, db: Session = Depends(get_db)):
    """The raw MAVLink log of this dive (opens in QGroundControl, MAVExplorer, Mission Planner)."""
    import dive_log
    v = _live_dive(db, dive_id)
    match = next((p for p in dive_log.tlog_files(v) if p.name == os.path.basename(name)), None)
    if not match:
        raise HTTPException(404, "No such .tlog for this dive")
    return FileResponse(match, media_type="application/octet-stream", filename=match.name)


# ---- the web UI, served by the backend itself (one process, one address) -----------------------------
# `npm run build` (or start.bat / start.sh) writes frontend/dist; the backend then serves it at http://<pc>:8500/.
# This mount comes after every API route, so /api/..., /ws/... and /docs are never shadowed.
# `npm run dev` on port 3000 keeps working for development.
UI_DIR = Path(os.getenv("FYP_UI_DIR", BASE_DIR.parent / "frontend" / "dist"))
if (UI_DIR / "index.html").exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
else:
    @app.get("/", response_class=HTMLResponse)
    def ui_missing():
        return ("<!doctype html><meta charset='utf-8'><title>AI Insights Digital Triplet Platform</title>"
                "<body style='font-family:system-ui;max-width:640px;margin:60px auto;line-height:1.5'>"
                "<h2>The backend is running, but the web UI is not built yet.</h2>"
                "<p>Run <b>start.bat</b> (Windows) or <b>./start.sh</b> once - it builds the UI with Node.js - "
                "or run <code>npm install</code> and <code>npm run build</code> in the <code>frontend</code> folder, "
                "then reload this page.</p><p>API docs: <a href='/docs'>/docs</a></p>")


def _open_browser_when_ready(url: str, port: int, timeout: float = 120.0):
    """Opens the UI once the server accepts connections (model loading can take a while)."""
    import socket
    import webbrowser

    def wait():
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        webbrowser.open(url)
    threading.Thread(target=wait, daemon=True, name="open-browser").start()


if __name__ == "__main__":
    import sys
    import uvicorn
    print(f"[BOOT] Backend folder: {BASE_DIR}")
    url = f"http://localhost:{API_PORT}"
    print(f"[BOOT] Open {url} in Chrome or Edge" + ("" if (UI_DIR / "index.html").exists() else " (UI not built yet - run start.bat)"))
    if "--open" in sys.argv:
        _open_browser_when_ready(url, API_PORT)
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
