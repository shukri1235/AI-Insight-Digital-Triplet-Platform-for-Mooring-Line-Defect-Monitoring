"""
LOGS-1  Event log paper trail: one text file per app session (logs/session_<date>_<time>.txt). Entries from the
        browser are appended with date, time, type and source; one entry is one line (line breaks cannot forge extra
        lines); files are listed and downloadable; no other file can be fetched through the endpoint.
LOGS-2  The backend writes its own record too (even with no browser open): live dive started / ended, batch analysis
        finished, report approved.
LOGS-4  An incident clip byte-identical to an earlier file (Test Mode looping one video) is saved, not lost.
LOGS-3  A live dive is closed once. Stopping again later (Stop pressed twice, Test Mode off, the app closed the next
        morning) no longer overwrites its end time and duration (the cause of a 1-minute dive reading 9 h 33 min).
"""
import re
import time

from conftest import set_depth, wait_for


def _text(client):
    cur = client.get("/api/logs").json()["current"]
    r = client.get(f"/api/logs/{cur}")
    assert r.status_code == 200 and "text/plain" in r.headers["content-type"]
    return cur, r.text


def test_logs_1_browser_entries_are_appended(client, evidence):
    info = client.get("/api/logs").json()
    assert re.fullmatch(r"session_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt", info["current"])
    ts = int(time.mktime((2026, 11, 7, 19, 40, 12, 0, 0, -1)) * 1000)
    r = client.post("/api/logs", json={"client": "ab12", "entries": [
        {"ts": ts, "kind": "Clip", "text": "Critical deformation 88% at 120.5 m"},
        {"ts": ts + 1000, "kind": "Config", "text": "trigger set to 0.55\n2026-01-01 00:00:00 | Fake | [app] forged line"},
        {"kind": "Info", "text": "   "},                                     # empty: skipped
        "not an entry",                                                        # wrong shape: skipped
    ]})
    assert r.status_code == 200 and r.json()["written"] == 2
    cur, text = _text(client)
    lines = text.splitlines()
    assert lines[0].startswith("# AI Insights Digital Triplet Platform event log - session started")
    assert "2026-11-07 19:40:12 | Clip     | [ui ab12] Critical deformation 88% at 120.5 m" in lines
    forged = [l for l in lines if "forged line" in l]
    assert len(forged) == 1 and forged[0].startswith("2026-11-07 19:40:13 | Config"), "a line break must not create a line"
    assert not any(l.startswith("2026-01-01") for l in lines)
    # download, listing, and nothing else reachable
    d = client.get(f"/api/logs/{cur}?download=1")
    assert "attachment" in d.headers["content-disposition"] and cur in d.headers["content-disposition"]
    assert any(s["current"] and s["name"] == cur for s in client.get("/api/logs").json()["sessions"])
    for bad in ("..%2Fconfig.toml", "session_2026-11-07_19-40-12.txt.bak", "vv.db", "session_x.txt"):
        assert client.get(f"/api/logs/{bad}").status_code == 404, bad
    evidence["LOGS-1"] = {"file": cur, "sample": lines[2]}


def test_logs_2_and_3_backend_record_and_dive_closed_once(client, rov, processed_dive, evidence):
    import main
    from database import SessionLocal, VideoRecord
    set_depth(client, rov, 40.0)
    r = client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH"})
    assert r.status_code == 200, r.text
    sid = main.live.session_id
    try:
        wait_for(lambda: main.live._latest is not None and main.live._latest.t > 1.0, 15, 0.05, "dive running")
    finally:
        client.post("/api/live/stop")
        set_depth(client, rov, 120.5)
    with SessionLocal() as db:
        v = db.get(VideoRecord, sid)
        first = (v.completed_at, v.duration_sec)
    time.sleep(1.5)
    client.post("/api/live/stop")                       # a second stop, like the app closing later
    main.live.stop()
    with SessionLocal() as db:
        v = db.get(VideoRecord, sid)
        assert (v.completed_at, v.duration_sec) == first, "a later stop must not reopen the dive"
    _, text = _text(client)
    started = [l for l in text.splitlines() if "[app] Live dive started" in l and sid[:8] in l]
    ended = [l for l in text.splitlines() if "[app] Live dive ended" in l and sid[:8] in l]
    assert len(started) == 1 and len(ended) == 1, (started, ended)
    assert any("[app] Batch analysis finished" in l and "frames in" in l for l in text.splitlines()), \
        "the processed dive's batch run is on record"
    # report approval
    rep = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": processed_dive["id"], "use_ai": False}).json()
    assert client.post(f"/api/reports/{rep['id']}/approve", json={"engineer": "S. Ajed"}).status_code == 200
    _, text = _text(client)
    assert any("[app] Report approved by S. Ajed" in l and rep["id"][:8] in l for l in text.splitlines())
    evidence["LOGS-2"] = {"live_started": started[0], "live_ended": ended[0]}


def test_logs_4_identical_clip_is_still_saved(client, rov, processed_dive, monkeypatch):
    """Test Mode loops one video, so two incident clips can be byte-identical. The file hash is unique (it finds
    duplicate imports); the second clip used to fail with 'UNIQUE constraint failed' and the incident was lost."""
    import live_stream
    from conftest import run_live_until_incident
    from database import SessionLocal, VideoRecord
    with SessionLocal() as db:
        taken = db.get(VideoRecord, processed_dive["id"]).file_hash
    monkeypatch.setattr(live_stream, "sha256_file", lambda p: taken)      # the new clip "matches" an existing file
    rec = run_live_until_incident(client, rov, f"video:{processed_dive['id']}", depth=120.5)
    assert rec["saved"] and rec["saved"]["stage"] == "saved", rec["incidents"]
    with SessionLocal() as db:
        clip = db.get(VideoRecord, rec["saved"]["clipId"])
        assert clip is not None and clip.file_hash is None and clip.source_type == "INCIDENT_CLIP"
