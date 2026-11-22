"""
ROV sensor time-series store (separate SQLite file: fyp_sensors.db).

Why a separate database file:
  * Different write profile - 5 Hz rows for the whole dive vs. low-rate, precious
    inspection records. SQLite allows one writer per file, so high-rate sensor
    inserts can never block state-machine/defect writes.
  * Different lifecycle - a dive's sensor log can be archived or purged on its own.
  * The raw MAVLink stream is ALSO kept as a standard .tlog per dive (opens in
    QGroundControl), so this table is a decoded, queryable copy - never the only copy.

Rows are keyed by `media_id` (a live session, an incident clip, or a batch video) and
`t` = seconds on that media's own timeline, so Review can look up the sensor state at
any video time with one indexed query.
"""
from __future__ import annotations

import bisect

from sqlalchemy import text, Boolean, Column, Float, Integer, String, Text, create_engine, event, func
from sqlalchemy.orm import declarative_base, sessionmaker

from config import SENSOR_DATABASE_URL, SENSOR_DB_PATH  # noqa: F401  (SENSOR_DB_PATH kept for imports)

IS_SQLITE = SENSOR_DATABASE_URL.startswith("sqlite")
engine = create_engine(SENSOR_DATABASE_URL, pool_pre_ping=True,
                       **({"connect_args": {"check_same_thread": False, "timeout": 15}} if IS_SQLITE
                          else {"pool_size": 5, "max_overflow": 10, "pool_recycle": 1800}))

if IS_SQLITE:
    @event.listens_for(engine, "connect")
    def _pragma(conn, _):
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.close()


SensorSession = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()

FIELDS = ("depth", "heading", "roll", "pitch", "temperature", "pressure_hpa", "altitude", "climb",
          "battery_v", "battery_pct", "armed", "mode")


class SensorSample(Base):
    __tablename__ = "sensor_samples"
    id = Column(Integer, primary_key=True, autoincrement=True)
    media_id = Column(String(36), index=True)
    t = Column(Float, index=True)                 # seconds on the media's timeline
    wall_time = Column(Float, nullable=True)      # unix epoch of the reading
    source = Column(String(16))                   # MAVLINK | TLOG
    depth = Column(Float, nullable=True)          # m, positive down
    heading = Column(Float, nullable=True)        # deg
    roll = Column(Float, nullable=True)
    pitch = Column(Float, nullable=True)
    temperature = Column(Float, nullable=True)    # water, deg C (Bar30 / SCALED_PRESSURE2)
    pressure_hpa = Column(Float, nullable=True)
    altitude = Column(Float, nullable=True)       # m above seabed (Ping sonar), if fitted
    climb = Column(Float, nullable=True)          # m/s, +up
    battery_v = Column(Float, nullable=True)
    battery_pct = Column(Float, nullable=True)
    armed = Column(Boolean, nullable=True)
    mode = Column(String(20), nullable=True)


class RovEvent(Base):
    __tablename__ = "rov_events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    media_id = Column(String(36), index=True, nullable=True)
    t = Column(Float, nullable=True)
    wall_time = Column(Float)
    kind = Column(String(20))                     # CONNECTED | LOST | ARMED | DISARMED | MODE | LEAK | STATUSTEXT
    text = Column(Text)


class ObjectDetection(Base):
    """Every NON-defect detection (e.g. "person", "fish", "rope" from a generic model).
    Kept here with the other machine logs, never in the inspection/defect database:
    the defect registry, incident clips and reports only ever see real defect classes."""
    __tablename__ = "object_detections"
    id = Column(Integer, primary_key=True, autoincrement=True)
    media_id = Column(String(36), index=True)
    run_id = Column(String(36), index=True, nullable=True)
    t = Column(Float)
    cls = Column(String(60))
    conf = Column(Float)
    x = Column(Float); y = Column(Float); w = Column(Float); h = Column(Float)


OBJ_COLS = ("t", "cls", "conf", "x", "y", "w", "h")


def insert_objects(media_id: str, run_id: str | None, rows: list[tuple]):
    """rows: (t, cls, conf, x, y, w, h)"""
    if not rows:
        return
    with engine.begin() as c:
        c.execute(text("INSERT INTO object_detections (media_id, run_id, t, cls, conf, x, y, w, h) "
                       "VALUES (:m, :r, :t, :cls, :conf, :x, :y, :w, :h)"),
                  [{"m": media_id, "r": run_id, **dict(zip(OBJ_COLS, r))} for r in rows])


def objects_for(media_id: str, run_id: str | None = None) -> list[dict]:
    sql = "SELECT t, cls, conf, x, y, w, h FROM object_detections WHERE media_id = :m"
    p = {"m": media_id}
    if run_id:
        sql += " AND run_id = :r"; p["r"] = run_id
    with engine.connect() as c:
        return [dict(zip(OBJ_COLS, r)) for r in c.execute(text(sql + " ORDER BY t"), p).fetchall()]


def object_counts(media_id: str, run_id: str | None = None) -> dict:
    sql = "SELECT cls, COUNT(*) FROM object_detections WHERE media_id = :m"
    p = {"m": media_id}
    if run_id:
        sql += " AND run_id = :r"; p["r"] = run_id
    with engine.connect() as c:
        return dict(c.execute(text(sql + " GROUP BY cls ORDER BY 2 DESC"), p).fetchall())


def delete_objects(run_id: str | None = None, after_t: float | None = None, all_runs: bool = False):
    """Clears object detections of one run (optionally only after a checkpoint), or of every run."""
    with engine.begin() as c:
        if all_runs:
            c.execute(text("DELETE FROM object_detections"))
        elif after_t is not None:
            c.execute(text("DELETE FROM object_detections WHERE run_id = :r AND t > :t"), {"r": run_id, "t": after_t})
        else:
            c.execute(text("DELETE FROM object_detections WHERE run_id = :r"), {"r": run_id})


def init_sensor_db():
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_samples_media_t ON sensor_samples (media_id, t)")


def insert_samples(rows: list[dict]):
    if not rows:
        return
    s = SensorSession()
    try:
        s.bulk_insert_mappings(SensorSample, rows)
        s.commit()
    finally:
        s.close()


def insert_event(**kw):
    s = SensorSession()
    try:
        s.add(RovEvent(**kw))
        s.commit()
    finally:
        s.close()


def samples_for(media_id: str, t0: float | None = None, t1: float | None = None, max_hz: float | None = None) -> list[dict]:
    """Sensor rows on the media timeline. Plain SQL (no ORM objects): a 4 h dive at 5 Hz
    is 72 000 rows. `max_hz` thins the series for long dives (keeps the first row per bin)."""
    cols = ["t", "source", *FIELDS]
    sql = f"SELECT {', '.join(cols)} FROM sensor_samples WHERE media_id = :m"
    p: dict = {"m": media_id}
    if t0 is not None:
        sql += " AND t >= :t0"; p["t0"] = t0
    if t1 is not None:
        sql += " AND t <= :t1"; p["t1"] = t1
    if max_hz:
        sql += " AND id IN (SELECT MIN(id) FROM sensor_samples WHERE media_id = :m GROUP BY CAST(t * :hz AS INTEGER))"
        p["hz"] = max_hz
    sql += " ORDER BY t"
    with engine.connect() as c:
        rows = c.execute(text(sql), p).fetchall()
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        if d.get("armed") is not None:
            d["armed"] = bool(d["armed"])
        out.append(d)
    return out


def events_for(media_id: str) -> list[dict]:
    s = SensorSession()
    try:
        return [{"t": e.t, "kind": e.kind, "text": e.text}
                for e in s.query(RovEvent).filter(RovEvent.media_id == media_id).order_by(RovEvent.wall_time)]
    finally:
        s.close()


def count_for(media_id: str) -> int:
    s = SensorSession()
    try:
        return s.query(func.count(SensorSample.id)).filter(SensorSample.media_id == media_id).scalar() or 0
    finally:
        s.close()


def delete_samples(media_id: str):
    """Only the sensor time-series (e.g. before re-attaching a .tlog); detections are kept."""
    s = SensorSession()
    try:
        s.query(SensorSample).filter(SensorSample.media_id == media_id).delete()
        s.commit()
    finally:
        s.close()


def delete_media(media_id: str):
    """Everything this DB holds for a media record (used when the record is deleted)."""
    s = SensorSession()
    try:
        s.query(SensorSample).filter(SensorSample.media_id == media_id).delete()
        s.query(RovEvent).filter(RovEvent.media_id == media_id).delete()
        s.query(ObjectDetection).filter(ObjectDetection.media_id == media_id).delete()
        s.commit()
    finally:
        s.close()


class Lookup:
    """Nearest-sample lookup over a media's sensor timeline (used by reports)."""

    def __init__(self, media_id: str, max_gap: float = 2.0):
        self.rows = samples_for(media_id)
        self.ts = [r["t"] for r in self.rows]
        self.max_gap = max_gap

    def at(self, t: float) -> dict | None:
        if not self.rows:
            return None
        i = bisect.bisect_left(self.ts, t)
        best = min((j for j in (i - 1, i) if 0 <= j < len(self.ts)), key=lambda j: abs(self.ts[j] - t))
        return self.rows[best] if abs(self.ts[best] - t) <= self.max_gap else None

    def depth_range(self):
        d = [r["depth"] for r in self.rows if r["depth"] is not None]
        return (min(d), max(d)) if d else None

    def temp_range(self):
        d = [r["temperature"] for r in self.rows if r["temperature"] is not None]
        return (min(d), max(d)) if d else None
