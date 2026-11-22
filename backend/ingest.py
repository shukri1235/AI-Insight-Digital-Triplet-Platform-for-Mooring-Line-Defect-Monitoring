"""
Ingest - two ways a dive video enters the system.

1. IMPORT IN PLACE (backend PC): the operator browses the PC's drives in the UI
   (server-side file browser) and picks a file. Nothing is copied - only the path, the
   SHA-256 and the metadata are stored. A 20 GB dive on an external drive is ready as
   soon as it has been hashed. If the drive is unplugged later the video shows
   "media offline" and works again when the drive comes back.
2. UPLOAD (another device): the browser streams the bytes into the managed media/
   library (hashed on the fly). A browser can never give the server a file path, so a
   copy is unavoidable here.

Either way the SHA-256 is the identity: the same file can never be ingested twice.
Hashing a large in-place import runs on a background thread (status HASHING with
progress); the threshold chosen at ingest is stored and the analysis run is queued as
soon as hashing finishes.
"""
from __future__ import annotations

import hashlib
import os
import queue
import string
import threading
from pathlib import Path

from config import VIDEO_EXTS
from database import SessionLocal, VideoRecord
from video_utils import probe, quick_fingerprint


# ------------------------------------------------------------------ file browser
def fs_roots() -> list[dict]:
    roots = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            p = f"{letter}:\\"
            if os.path.exists(p):
                roots.append({"name": p, "path": p})
    else:
        home = str(Path.home())
        roots.append({"name": "Home", "path": home})
        for base in ("/media", "/mnt", "/Volumes"):
            if os.path.isdir(base):
                for d in sorted(os.listdir(base)):
                    full = os.path.join(base, d)
                    if os.path.isdir(full):
                        roots.append({"name": full, "path": full})
        roots.append({"name": "/", "path": "/"})
    return roots


def fs_list(path: str, kinds: tuple[str, ...] = VIDEO_EXTS) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.is_dir():
        raise NotADirectoryError(str(p))
    dirs, files = [], []
    try:
        entries = list(os.scandir(p))
    except PermissionError:
        entries = []
    for e in sorted(entries, key=lambda e: e.name.lower()):
        if e.name.startswith((".", "$")):
            continue
        try:
            if e.is_dir():
                dirs.append({"name": e.name, "path": e.path})
            elif e.name.lower().endswith(kinds):
                files.append({"name": e.name, "path": e.path, "sizeBytes": e.stat().st_size})
        except OSError:
            continue
    return {"path": str(p), "parent": str(p.parent) if p.parent != p else None, "dirs": dirs, "files": files}


# ------------------------------------------------------------------ hashing worker
class Hasher:
    """Serial background SHA-256 for in-place imports (one disk read at a time)."""

    def __init__(self, on_ready):
        self.q: queue.Queue[str] = queue.Queue()
        self.on_ready = on_ready     # callback(video_id, conf) when the file is hashed and unique
        threading.Thread(target=self._loop, name="hasher", daemon=True).start()

    def submit(self, video_id: str):
        self.q.put(video_id)

    def _loop(self):
        while True:
            vid = self.q.get()
            try:
                self._hash(vid)
            except Exception as e:
                _set(vid, status="FAILED", error_message=f"Hashing failed: {e}")

    def _hash(self, vid: str):
        db = SessionLocal()
        try:
            v = db.get(VideoRecord, vid)
            path, size = v.current_filepath, v.file_size or 0
        finally:
            db.close()
        h, done, last = hashlib.sha256(), 0, -1
        with open(path, "rb") as f:
            while block := f.read(4 << 20):
                h.update(block)
                done += len(block)
                pct = int(done * 100 / size) if size else 0
                if pct != last:
                    last = pct
                    _set(vid, progress=float(pct))
        digest = h.hexdigest()
        db = SessionLocal()
        try:
            dup = db.query(VideoRecord).filter(VideoRecord.file_hash == digest, VideoRecord.id != vid).first()
            v = db.get(VideoRecord, vid)
            if dup:
                v.status, v.progress = "DUPLICATE", 0.0
                v.error_message = f"Identical to '{dup.display_name or dup.original_filename}' (SHA-256) - not processed"
                db.commit()
                return
            v.file_hash, v.status, v.progress = digest, "UNPROCESSED", 0.0
            conf = v.pending_conf
            db.commit()
        finally:
            db.close()
        if conf is not None:
            self.on_ready(vid, conf)


def register_in_place(path: str, name: str, line_id: str, conf: float | None) -> tuple[VideoRecord | None, VideoRecord | None]:
    """Returns (new_record, probable_duplicate). Uses the quick fingerprint to refuse an
    obvious duplicate instantly, before spending minutes hashing a multi-GB file."""
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in VIDEO_EXTS:
        raise ValueError("Not a supported video file")
    fp = quick_fingerprint(p)
    db = SessionLocal()
    try:
        dup = db.query(VideoRecord).filter(VideoRecord.quick_hash == fp).first()
        if dup is None:
            dup = db.query(VideoRecord).filter(VideoRecord.current_filepath == str(p)).first()
        if dup:
            return None, dup
        import uuid
        meta = probe(p)
        rec = VideoRecord(id=str(uuid.uuid4()), file_hash=None, original_filename=p.name,
                          display_name=name.strip() or p.name, current_filepath=str(p), is_managed=False,
                          status="HASHING", source_type="LOCAL_IMPORT", line_id=line_id, pending_conf=conf,
                          quick_hash=fp, file_size=p.stat().st_size, duration_sec=meta["duration_sec"],
                          fps=meta["fps"], width=meta["width"], height=meta["height"], codec=meta["codec"])
        db.add(rec)
        db.commit()
        db.refresh(rec)
        db.expunge(rec)
        return rec, None
    finally:
        db.close()


def _set(vid: str, **fields):
    db = SessionLocal()
    try:
        v = db.get(VideoRecord, vid)
        if v:
            for k, val in fields.items():
                setattr(v, k, val)
            db.commit()
    finally:
        db.close()
