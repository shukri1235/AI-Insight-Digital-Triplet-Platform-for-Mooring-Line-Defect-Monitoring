"""
Inspection database: the local SQLite file (WAL) by default, or any SQLAlchemy URL - PostgreSQL on a server or
AWS RDS - via database.url in config.toml / FYP_DATABASE_URL. High-rate ROV sensor data lives separately in
sensors_db.py. Only portable SQL is used (checked against PostgreSQL 16, see tests/test_postgres_compat.py).

videos.source_type : LOCAL_UPLOAD (managed copy) | LOCAL_IMPORT (in place) | INCIDENT_CLIP | LIVE_SESSION
videos.status      : HASHING | UNPROCESSED | QUEUED | PROCESSING | COMPLETED | FAILED | DUPLICATE  (files)
                     LIVE | ENDED                                                                (live sessions)

Every analysis is an AnalysisRun with a recorded threshold and model. Detections belong
to a run, so re-analysing at a different threshold never overwrites the original
record (audit trail). videos.active_run_id is the run shown by default.
"""
import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, event, inspect, text)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from config import CRITICAL_CONF, DATABASE_URL, DB_PATH, redact_url

IS_SQLITE = DATABASE_URL.startswith("sqlite")
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       **({"connect_args": {"check_same_thread": False, "timeout": 15}} if IS_SQLITE
                          else {"pool_size": 10, "max_overflow": 20, "pool_recycle": 1800}))

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_str(dt, fmt: str = "%Y-%m-%d %H:%M") -> str | None:
    """Stored timestamps are naive UTC; format them in the backend PC's local time zone."""
    return dt.replace(tzinfo=timezone.utc).astimezone().strftime(fmt) if dt else None


# --- PHYSICAL ASSETS -----------------------------------------------------------

class MooringLineRecord(Base):
    __tablename__ = "mooring_lines"
    id = Column(String(50), primary_key=True)
    name = Column(String(100))
    seabed_depth_meters = Column(Float)
    segments = relationship("LineSegmentRecord", back_populates="mooring_line",
                            cascade="all, delete-orphan", order_by="LineSegmentRecord.depth_start_m")
    videos = relationship("VideoRecord", back_populates="mooring_line")


class LineSegmentRecord(Base):
    __tablename__ = "line_segments"
    id = Column(Integer, primary_key=True, autoincrement=True)
    line_id = Column(String(50), ForeignKey("mooring_lines.id"))
    segment_name = Column(String(100))
    depth_start_m = Column(Float)
    depth_end_m = Column(Float)
    mooring_line = relationship("MooringLineRecord", back_populates="segments")


# --- MEDIA + STATE MACHINE -----------------------------------------------------

class VideoRecord(Base):
    __tablename__ = "videos"

    id = Column(String(36), primary_key=True)
    file_hash = Column(String(64), unique=True, index=True, nullable=True)
    original_filename = Column(Text)
    display_name = Column(Text, nullable=True)
    current_filepath = Column(Text, nullable=True)
    playback_path = Column(Text, nullable=True)
    codec = Column(String(16), nullable=True)
    is_managed = Column(Boolean, default=True)          # False = imported in place (never deleted by us)

    status = Column(String(50), default="UNPROCESSED")
    source_type = Column(String(50), default="LOCAL_UPLOAD")
    progress = Column(Float, default=0.0)
    error_message = Column(Text, nullable=True)
    pending_conf = Column(Float, nullable=True)          # threshold chosen at ingest, used once hashing ends

    duration_sec = Column(Float, nullable=True)
    fps = Column(Float, nullable=True)
    width = Column(Integer, nullable=True)
    height = Column(Integer, nullable=True)
    frames_analyzed = Column(Integer, default=0)

    parent_video_id = Column(String(36), nullable=True)
    trigger_offset_sec = Column(Float, nullable=True)
    quick_hash = Column(String(80), nullable=True, index=True)
    file_size = Column(Integer, nullable=True)
    started_at = Column(DateTime, nullable=True)
    resume_from_sec = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    completed_at = Column(DateTime, nullable=True)
    active_run_id = Column(String(36), nullable=True)

    # ROV telemetry log attached to a historical dive (.tlog) and its alignment
    tlog_path = Column(Text, nullable=True)
    tlog_offset_sec = Column(Float, nullable=True)       # seconds into the log at which the video starts
    tlog_samples = Column(Integer, nullable=True)
    # Test Mode: media recorded/ingested with simulated ROV video + telemetry. Kept apart from
    # real inspections (registry, segment reports). test_profile = {"standIn": {"person": "DEFORMATION"}, ...}
    parent_offset_sec = Column(Float, nullable=True)     # incident clip: session time of its first frame
    is_test = Column(Boolean, default=False)
    pinned_at = Column(DateTime, nullable=True)          # Review library: pinned to the top (shared by all operators)
    test_profile = Column(Text, nullable=True)

    line_id = Column(String(50), ForeignKey("mooring_lines.id"), nullable=True)
    mooring_line = relationship("MooringLineRecord", back_populates="videos")
    defects = relationship("DefectRecord", back_populates="video", cascade="all, delete-orphan")
    runs = relationship("AnalysisRun", back_populates="video", cascade="all, delete-orphan")


class AnalysisRun(Base):
    __tablename__ = "analysis_runs"

    id = Column(String(36), primary_key=True)
    video_id = Column(String(36), ForeignKey("videos.id"), index=True)
    kind = Column(String(20))                 # LIVE | CLIP | BATCH | REANALYSIS | LEGACY
    conf = Column(Float)                      # detection threshold: what was logged / shown
    trigger_conf = Column(Float, nullable=True)   # live/clip: incident trigger threshold
    threshold_log = Column(Text, nullable=True)   # live: JSON [[session_t, trigger_conf], ...]
    ops_log = Column(Text, nullable=True)         # live: JSON {"changes": [[t, kind, value], ...], "flickers": n}
    timing = Column(Text, nullable=True)          # batch: JSON processing time / speed (see batch_worker._timing)
    sample_fps = Column(Float, nullable=True)
    model_name = Column(String(255), nullable=True)
    status = Column(String(20), default="QUEUED")  # QUEUED | PROCESSING | COMPLETED | FAILED | CANCELLED | LIVE
    progress = Column(Float, default=0.0)
    checkpoint_sec = Column(Float, nullable=True)  # last committed video time (crash resume)
    frames_analyzed = Column(Integer, default=0)
    frames_with_defects = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    parent_run_id = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    video = relationship("VideoRecord", back_populates="runs")

    def threshold_history(self) -> list:
        try:
            return json.loads(self.threshold_log or "[]")
        except ValueError:
            return []


class DefectRecord(Base):
    __tablename__ = "defects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(String(36), ForeignKey("videos.id"), index=True)
    run_id = Column(String(36), nullable=True, index=True)
    defect_type = Column(String(50))
    raw_class = Column(String(50), nullable=True)
    timestamp_sec = Column(Float, index=True)
    confidence = Column(Float)
    severity = Column(String(20))
    x = Column(Float, nullable=True)
    y = Column(Float, nullable=True)
    width = Column(Float, nullable=True)
    height = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    video = relationship("VideoRecord", back_populates="defects")


class ReportRecord(Base):
    """Inspection report: AI draft -> engineer edits -> approval. Every version keeps the
    facts packet it was generated from, so a report can always be re-checked."""
    __tablename__ = "reports"

    id = Column(String(36), primary_key=True)
    scope_type = Column(String(20))           # INCIDENT | DIVE | SEGMENT
    scope_ref = Column(String(255), index=True)
    title = Column(String(255))
    status = Column(String(20), default="DRAFT")   # DRAFT | NEEDS_REVIEW | EDITED | APPROVED
    generator = Column(String(40))            # gemini model name | RULE_BASED
    facts_json = Column(Text)
    ai_json = Column(Text, nullable=True)     # raw structured output from the model
    report_json = Column(Text)                # current content (after engineer edits)
    grounding_json = Column(Text, nullable=True)
    engineer = Column(String(120), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow)


# --- INITIALISATION + NON-DESTRUCTIVE MIGRATION ------------------------------------

_EXTRA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_defects_run_ts ON defects (run_id, timestamp_sec)",
    "CREATE INDEX IF NOT EXISTS ix_defects_video_ts ON defects (video_id, timestamp_sec)",
]
_COLUMN_DEFAULTS = {
    ("videos", "source_type"): "'LOCAL_UPLOAD'",
    ("videos", "status"): "'UNPROCESSED'",
    ("videos", "progress"): "0",
    ("videos", "frames_analyzed"): "0",
    ("videos", "is_managed"): "TRUE",          # TRUE / FALSE work in SQLite (3.23+) and PostgreSQL
    ("videos", "is_test"): "FALSE",
}


def init_db():
    Base.metadata.create_all(bind=engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {col.type.compile(dialect=engine.dialect)}'
                default = _COLUMN_DEFAULTS.get((table.name, col.name))
                if default is not None:
                    ddl += f" DEFAULT {default}"
                print(f"[DB] Migration: adding {table.name}.{col.name}")
                conn.execute(text(ddl))
        for ddl in _EXTRA_INDEXES:
            conn.execute(text(ddl))
        conn.execute(text("UPDATE videos SET source_type='LOCAL_UPLOAD' WHERE source_type IS NULL"))
        conn.execute(text("UPDATE videos SET status='UNPROCESSED' WHERE status IS NULL"))
        conn.execute(text("UPDATE videos SET status='ENDED' WHERE source_type='LIVE_SESSION' AND status='LIVE'"))
        conn.execute(text("UPDATE analysis_runs SET status='COMPLETED' WHERE kind='LIVE' AND status='LIVE'"))
        # 3.6.8: confidence labels replace Critical / High / Medium (they never meant severity)
        n = conn.execute(text("UPDATE defects SET severity = CASE WHEN confidence >= :c THEN 'High confidence' "
                              "ELSE 'Low confidence' END WHERE defect_type IN ('DEFORMATION', 'CORROSION') "
                              "AND severity IN ('Critical', 'High', 'Medium', 'Major')"), {"c": CRITICAL_CONF}).rowcount
        if n:
            print(f"[DB] Migration: {n} detections relabelled High / Low confidence")
        # Detections from before analysis runs existed -> one LEGACY run per video
        legacy = conn.execute(text("SELECT DISTINCT video_id FROM defects WHERE run_id IS NULL")).fetchall()
        for (vid,) in legacy:
            rid = f"legacy-{vid}"[:36]
            if conn.execute(text("SELECT 1 FROM analysis_runs WHERE id = :id"), {"id": rid}).first() is None:
                conn.execute(text("INSERT INTO analysis_runs (id, video_id, kind, conf, status, progress, "
                                  "created_at) VALUES (:id, :v, 'LEGACY', 0.10, 'COMPLETED', 100, :now)"),
                             {"id": rid, "v": vid, "now": utcnow()})
            conn.execute(text("UPDATE defects SET run_id=:id WHERE video_id=:v AND run_id IS NULL"), {"id": rid, "v": vid})
            conn.execute(text("UPDATE videos SET active_run_id=:id WHERE id=:v AND active_run_id IS NULL"),
                         {"id": rid, "v": vid})
    print(f"[DB] Ready at {DB_PATH} (WAL mode). Historical data preserved." if IS_SQLITE
          else f"[DB] Ready at {redact_url(DATABASE_URL)}. Historical data preserved.")


def _ml01_splash_50m(db):
    """3.6.8: ML-01's splash zone is the first 50 m (API RP 2I), not 30 m. Only a database still holding the old
    seeded boundaries is changed; a line someone has edited is left alone."""
    segs = {s.segment_name: s for s in db.query(LineSegmentRecord).filter(LineSegmentRecord.line_id == "ML-01-NORTH")}
    sp, mw = segs.get("Splash Zone"), segs.get("Mid-Water Zone")
    if sp and mw and (sp.depth_end_m, mw.depth_start_m) == (30.0, 30.1):
        sp.depth_end_m, mw.depth_start_m = 50.0, 50.1
        db.commit()
        print("[DB] Migration: ML-01 splash zone now 0-50 m (was 0-30 m)")


def seed_physical_assets():
    db = SessionLocal()
    try:
        if db.query(MooringLineRecord).first():
            _ml01_splash_50m(db)
            return
        print("[DB] Seeding representative offshore asset architecture...")
        db.add_all([
            MooringLineRecord(id="ML-01-NORTH", name="Mooring Line A (North Bow FPSO)", seabed_depth_meters=200.0),
            MooringLineRecord(id="ML-02-WEST", name="Mooring Line B (West Stern)", seabed_depth_meters=1240.0),
        ])
        db.add_all([
            LineSegmentRecord(line_id="ML-01-NORTH", segment_name="Splash Zone", depth_start_m=0.0, depth_end_m=50.0),
            LineSegmentRecord(line_id="ML-01-NORTH", segment_name="Mid-Water Zone", depth_start_m=50.1, depth_end_m=145.0),
            LineSegmentRecord(line_id="ML-01-NORTH", segment_name="Touchdown Zone", depth_start_m=145.1, depth_end_m=200.0),
            LineSegmentRecord(line_id="ML-02-WEST", segment_name="Splash Zone", depth_start_m=0.0, depth_end_m=50.0),
            LineSegmentRecord(line_id="ML-02-WEST", segment_name="Mid-Water Zone", depth_start_m=50.1, depth_end_m=1000.0),
            LineSegmentRecord(line_id="ML-02-WEST", segment_name="Touchdown Zone", depth_start_m=1000.1, depth_end_m=1240.0),
        ])
        db.commit()
    finally:
        db.close()
