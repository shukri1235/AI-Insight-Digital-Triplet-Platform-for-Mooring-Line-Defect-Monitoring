"""
BACKEND SUPPORT FOR THE REDESIGNED UI (iteration 3.4)

UI-1  Live detection defaults to AUTO and says why it is waiting (no ROV depth / too shallow)
UI-2  Locate file: an offline in-place dive is re-pointed only to the SAME recording; records are never deleted
UI-3  Per-type event counts per dive, and the incident summary of a clip (defect, confidence, depth, dive time)
UI-4  Registry by zone lists which dives have events in each zone (Batch zone filter)
UI-5  Cancelling a run also clears its general-object log
UI-9  Review library: pin / unpin is stored on the backend; clips carry their dive name
UI-10 Report jobs: background drafting with stages + progress; directory fields on every report
UI-11 Gemini requests: timeout + retry budget, low thinking on Gemini 3, per-stage timings on every report
UI-12 Gemini busy (503) / not answering: quick retry, then the fallback model (Flash-Lite), then the rules - one deadline
UI-13 Redraft with Gemini: a rule-based report is redrafted in place (same id, notes kept); still busy = untouched; approved = 409
UI-14 One address: the backend serves the built UI at / (and its assets) without shadowing /api, /docs or the WebSocket
"""
import shutil
from pathlib import Path

from conftest import MEDIA, make_dive, wait_completed


def test_ui_1_auto_is_the_default_mode():
    import live_stream
    src = open(live_stream.__file__, encoding="utf-8").read()
    assert 'self.detection_mode = "AUTO"' in src


def test_ui_2_locate_file_checks_identity(client, evidence):
    folder = MEDIA / "drive_E" / "campaign"
    folder.mkdir(parents=True, exist_ok=True)
    src = make_dive(folder / "ML02_dive_7.mp4", seconds=6)
    other = make_dive(folder / "unrelated.mp4", seconds=7)
    v = client.post("/api/videos/import", json={"path": str(src), "name": "ML-02 Dive 7", "line_id": "ML-02-WEST",
                                                "conf": 0.5}).json()["video"]
    done = wait_completed(client, v["id"])
    new_home = MEDIA / "drive_F" / "campaign"
    new_home.mkdir(parents=True, exist_ok=True)
    moved = Path(shutil.move(str(src), new_home / "ML02_dive_7.mp4"))
    assert client.get(f"/api/videos/{v['id']}").json()["mediaOffline"]
    bad = client.post(f"/api/videos/{v['id']}/locate", json={"path": str(other)})
    assert bad.status_code == 409, bad.text
    assert client.post(f"/api/videos/{v['id']}/locate", json={"path": str(new_home / 'nope.mp4')}).status_code == 400
    ok = client.post(f"/api/videos/{v['id']}/locate", json={"path": str(moved)})
    assert ok.status_code == 200, ok.text
    back = ok.json()
    assert back["path"] == str(moved) and not back["mediaOffline"] and back["defectCount"] == done["defectCount"]
    assert back["activeRun"]["id"] == done["activeRun"]["id"], "results are kept"
    evidence["UI-2"] = {"wrong_file": 409, "missing_file": 400, "same_file": 200, "results_kept": True}


def test_ui_3_event_counts_and_clip_incident(client, live_incident, processed_dive, evidence):
    vids = {v["id"]: v for v in client.get("/api/videos").json()}
    dive = vids[processed_dive["id"]]
    assert set(dive["defectEvents"]) == {"DEFORMATION", "CORROSION"}
    assert dive["defectEvents"]["DEFORMATION"] >= 1 and dive["incident"] is None
    clip = vids[live_incident["saved"]["clipId"]]
    inc = clip["incident"]
    assert inc["type"] == "DEFORMATION" and inc["confidence"] >= inc["triggerConf"]
    assert abs(inc["depth"] - 120.5) < 3.0
    assert inc["sessionTime"] is not None and abs(inc["sessionTime"] - live_incident["saved"]["sessionTime"]) < 0.5
    evidence["UI-3"] = {"dive_events": dive["defectEvents"], "clip_incident": inc}


def test_ui_4_history_lists_dives_per_zone(client, processed_dive):
    h = client.get("/api/history", params={"line_id": "ML-01-NORTH"}).json()
    assert "byVideo" in h
    for vid, zones in h["byVideo"].items():
        for name, n in zones.items():
            assert name in h["segments"] and n > 0


def test_ui_5_cancel_clears_object_log(app_module):
    import uuid
    import sensors_db
    from batch_worker import _finish_cancelled
    from database import AnalysisRun, VideoRecord
    vid, rid = str(uuid.uuid4()), str(uuid.uuid4())
    with app_module.SessionLocal() as db:
        db.add(VideoRecord(id=vid, original_filename="x.mp4", display_name="x", status="PROCESSING",
                           source_type="LOCAL_IMPORT"))
        db.flush()
        db.add(AnalysisRun(id=rid, video_id=vid, kind="BATCH", conf=0.5, status="PROCESSING"))
        db.commit()
    sensors_db.insert_objects(vid, rid, [(1.0, "fish", 0.7, 50, 50, 10, 10)])
    assert sensors_db.objects_for(vid, rid)
    _finish_cancelled(rid)
    assert sensors_db.objects_for(vid, rid) == []
    with app_module.SessionLocal() as db:
        assert db.get(AnalysisRun, rid).status == "CANCELLED"
        db.delete(db.get(VideoRecord, vid))
        db.commit()


def test_ui_6_gemini_failure_is_explained_and_never_loses_the_report(client, app_module, live_incident, monkeypatch, evidence):
    """A rejected key (e.g. the new AQ. auth keys) must not just fail: the engineer gets a
    rule-based draft, the exact reason and what to do; the status endpoint and the connection
    test say the same."""
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.test-key-not-real")
    app_module._ai_state["last_ok"] = 0.0
    monkeypatch.setenv("FAKE_GEMINI_MODE", "auth_error")
    try:
        r = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"], "use_ai": True})
        assert r.status_code == 200, r.text
        rep = r.json()
        assert rep["generator"] == "RULE_BASED" and rep["content"]["ai_error"]
        assert "AQ." in rep["content"]["ai_error"]["hint"] and "AQ.test-key" not in rep["content"]["ai_error"]["message"]
        assert rep["content"]["limitations"][0].startswith("Gemini was unavailable")
        st = client.get("/api/ai/status").json()
        assert st["keyKind"] == "auth key (AQ.)" and st["lastError"]["hint"]
        t = client.post("/api/ai/test").json()
        assert t["ok"] is False and "AQ." in t["hint"]
        monkeypatch.setenv("FAKE_GEMINI_MODE", "ok")
        app_module._ai_test_at[0] = 0.0
        ok = client.post("/api/ai/test").json()
        assert ok["ok"] is True and ok["reply"] == "OK"
        assert client.get("/api/ai/status").json()["lastError"] is None
        evidence["UI-6"] = {"fallback": rep["generator"], "hint": rep["content"]["ai_error"]["hint"][:80]}
    finally:
        app_module._ai_state["last_ok"] = 0.0
        app_module._ai_test_at[0] = 0.0


def test_ui_7_key_found_outside_the_process_environment(tmp_path, monkeypatch, client):
    """A key set after the backend started (Windows) or kept in a .env file is still found."""
    import config
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text('# app secrets\nOTHER=1\nGEMINI_API_KEY = "AQ.from-dotenv"\n', encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILES", (tmp_path / "missing.env", env))
    key, src = config.gemini_key()
    assert key == "AQ.from-dotenv" and src.startswith(".env file")
    st = client.get("/api/ai/status").json()
    assert st["configured"] and st["keyKind"] == "auth key (AQ.)" and st["keySource"].startswith(".env file")
    monkeypatch.setattr(config, "ENV_FILES", ())
    assert config.gemini_key() == (None, None)          # (registry lookup is Windows-only)
    assert client.get("/api/ai/status").json()["configured"] is False


def test_ui_8_retired_model_suggests_the_new_one_and_switching_works(client, app_module, live_incident, monkeypatch, evidence):
    """Google retires models for new users (404 'use models/X'): the app names the replacement,
    the model can be switched in the app (saved), and Gemini 3 calls keep the default temperature."""
    from google import genai
    monkeypatch.setenv("FAKE_GEMINI_MODE", "ok")
    monkeypatch.setenv("FAKE_GEMINI_RETIRED", "1")
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    app_module._ai_test_at[0] = 0.0
    try:
        assert client.put("/api/ai/model", json={"model": "gemini-2.5-flash"}).json()["modelSource"] == "chosen in the app"
        t = client.post("/api/ai/test").json()
        assert t["ok"] is False and t["suggestedModel"] == "gemini-3.8-flash" and "gemini-3.8-flash" in t["hint"]
        models = client.get("/api/ai/models").json()
        assert "gemini-3.8-flash" in models["models"] and not any("embedding" in m for m in models["models"])
        assert client.put("/api/ai/model", json={"model": "gemini-3.8-flash"}).status_code == 200
        assert client.get("/api/ai/status").json()["model"] == "gemini-3.8-flash"
        app_module._ai_test_at[0] = 0.0
        assert client.post("/api/ai/test").json()["ok"] is True
        assert client.put("/api/ai/model", json={"model": "rm -rf"}).status_code == 400
        app_module._ai_state["last_ok"] = 0.0
        n0 = len(genai.REQUESTS)
        rep = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"]}).json()
        assert rep["generator"] == "gemini-3.8-flash"
        assert all(r["temperature"] is None for r in genai.REQUESTS[n0:]), "Gemini 3: default temperature"
        evidence["UI-8"] = {"suggested": t["suggestedModel"], "generator": rep["generator"]}
    finally:
        app_module._ai_state["last_ok"] = 0.0
        app_module._ai_test_at[0] = 0.0


def test_ui_9_pin_media_for_the_review_library(client, live_incident, processed_dive, evidence):
    """Review library (3.5.5): pins are stored on the backend, survive a re-list, and clips name their dive."""
    vid, clip_id = processed_dive["id"], live_incident["saved"]["clipId"]
    vids = {v["id"]: v for v in client.get("/api/videos").json()}
    assert vids[vid]["pinned"] is False and vids[vid]["createdTs"] > 0
    assert vids[clip_id]["parentName"], "incident clips carry the name of their live dive"
    r = client.put(f"/api/videos/{vid}/pin", json={"pinned": True})
    assert r.status_code == 200 and r.json()["pinned"] is True
    first = client.get(f"/api/videos/{vid}").json()
    assert first["pinned"] and first["pinnedTs"] > 0
    client.put(f"/api/videos/{vid}/pin", json={"pinned": True})          # pinning twice keeps the original time
    assert client.get(f"/api/videos/{vid}").json()["pinnedTs"] == first["pinnedTs"]
    assert client.put(f"/api/videos/{vid}/pin", json={"pinned": False}).json()["pinned"] is False
    assert client.get(f"/api/videos/{vid}").json()["pinnedTs"] is None
    assert client.put("/api/videos/no-such-id/pin", json={"pinned": True}).status_code == 404
    evidence["UI-9"] = {"pin": True, "idempotent": True, "unpin": True, "unknown": 404,
                        "clip_parent": vids[clip_id]["parentName"]}


def _wait_job(client, jid, timeout=60):
    import time as _t
    seen, t0 = [], _t.time()
    while _t.time() - t0 < timeout:
        j = client.get(f"/api/reports/jobs/{jid}").json()
        if not seen or seen[-1] != j["stage"]:
            seen.append(j["stage"])
        assert 0.0 <= j["progress"] <= 1.0
        if j["status"] != "running":
            return j, seen
        _t.sleep(0.05)
    raise AssertionError(f"report job did not finish: {seen}")


def test_ui_10_report_jobs_show_progress_and_feed_the_directory(client, app_module, processed_dive, live_incident, monkeypatch, evidence):
    """3.6: a report is drafted by a background job with real stages and progress; the reports
    directory lists every report with scope name, line, finding count and date."""
    r = client.post("/api/reports/jobs", json={"scope_type": "DIVE", "scope_ref": processed_dive["id"], "use_ai": False})
    assert r.status_code == 202, r.text
    first = r.json()
    assert first["status"] == "running" and first["progress"] < 0.5 and first["model"] == "RULE_BASED"
    job, seen = _wait_job(client, first["id"])
    assert job["status"] == "done" and job["progress"] == 1.0 and job["reportId"] and job["generator"] == "RULE_BASED"
    assert job["elapsedSec"] >= 0
    # an AI job goes through the Gemini stages; a second AI job while one runs is refused (token saver)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.setenv("FAKE_GEMINI_MODE", "ok")
    app_module._ai_state["last_ok"] = 0.0
    ai = client.post("/api/reports/jobs", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"], "use_ai": True})
    assert ai.status_code == 202, ai.text
    busy = client.post("/api/reports/jobs", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"], "use_ai": True})
    ai_job, ai_seen = _wait_job(client, ai.json()["id"])
    assert busy.status_code == 429
    assert ai_job["status"] == "done" and ai_job["generator"] != "RULE_BASED"
    assert client.get("/api/reports/jobs").json()[0]["id"] == ai_job["id"]          # newest first, kept after finishing
    # errors are reported on the job, not lost
    bad = client.post("/api/reports/jobs", json={"scope_type": "DIVE", "scope_ref": "no-such-video", "use_ai": False}).json()
    bad_job, _ = _wait_job(client, bad["id"])
    assert bad_job["status"] == "error" and bad_job["errorStatus"] in (400, 503)
    assert client.post("/api/reports/jobs", json={"scope_type": "NOPE", "scope_ref": "x"}).status_code == 400
    assert client.get("/api/reports/jobs/nope").status_code == 404
    # directory fields
    lst = client.get("/api/reports", params={"limit": 500}).json()
    mine = next(x for x in lst if x["id"] == job["reportId"])
    assert mine["scopeName"] and mine["lineId"] == processed_dive["lineId"] and isinstance(mine["findings"], int)
    assert mine["createdAt"] and mine["isTest"] is False
    evidence["UI-10"] = {"rule_stages_seen": seen, "ai_stages_seen": ai_seen, "second_ai_job": busy.status_code,
                         "directory_fields": {k: mine[k] for k in ("scopeName", "lineId", "findings")}}


def test_ui_11_gemini_call_is_bounded_fast_and_timed(client, app_module, live_incident, monkeypatch, evidence):
    """3.6.1: a Gemini report took ~4 min in the field, all of it inside one request. Requests now have a
    timeout and a small retry budget, Gemini 3 runs with low thinking, and every report says how long each
    stage and each Gemini request took."""
    from google import genai
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    monkeypatch.setenv("FAKE_GEMINI_MODE", "ok")
    app_module._ai_state["last_ok"] = 0.0
    r = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"], "use_ai": True})
    assert r.status_code == 200, r.text
    c = r.json()["content"]
    t = c["timings"]
    assert {"facts", "keyframes", "ai", "check", "total"} <= set(t) and t["total"] >= t["ai"] >= 0
    assert c["usage"]["seconds"] >= 0 and c["thinking"] == "low"
    assert genai.REQUESTS[-1]["thinking"] == "LOW"
    opts = genai.Client.last_http_options
    # the SDK does not retry on its own any more: the app's loop retries / falls back inside one deadline (UI-12)
    assert opts is not None and opts.timeout == 90_000 and opts.retry_options.attempts == 1
    rb = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"], "use_ai": False}).json()
    assert "rules" in rb["content"]["timings"] and "ai" not in rb["content"]["timings"] and rb["content"]["thinking"] is None
    html = client.get(f"/api/reports/{r.json()['id']}/html").text
    assert "Time taken:" in html and "Gemini" in html and "LOW thinking" in html.replace("low thinking", "LOW thinking")
    evidence["UI-11"] = {"timings_ai": t, "timeout_ms": opts.timeout, "sdk_attempts": opts.retry_options.attempts, "thinking": "LOW"}


def _set_ai(app_module, monkeypatch, **env):
    import reports
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    for k in ("FAKE_GEMINI_MODE", "FAKE_GEMINI_BUSY", "FAKE_GEMINI_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(reports, "RETRY_PAUSE_SEC", 0.0)
    app_module._ai_state["last_ok"] = 0.0


def test_ui_12_busy_gemini_falls_back_to_flash_lite_then_rules(client, app_module, live_incident, monkeypatch, evidence):
    """3.6.2: in the field Gemini 3.8 Flash answered 503 "high demand". The app asks once more, then lets
    Flash-Lite (separate capacity) draft it, and only then uses the rules. A timeout skips straight to Flash-Lite."""
    from google import genai
    import reports
    clip = live_incident["saved"]["clipId"]
    body = {"scope_type": "INCIDENT", "scope_ref": clip, "use_ai": True}

    # 1. Flash busy -> one quick retry -> Flash-Lite drafts it; labelled as a fallback
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_BUSY="gemini-3.8-flash")
    genai.BUSY_CALLS.clear()
    r = client.post("/api/reports", json=body).json()
    c = r["content"]
    assert r["generator"] == "gemini-3.5-flash-lite" and genai.BUSY_CALLS == ["gemini-3.8-flash", "gemini-3.8-flash"]
    assert c["fallback"]["from"] == "gemini-3.8-flash" and c["fallback"]["reason"] == "busy"
    assert [a["kind"] for a in c["ai_attempts"]] == ["busy", "busy"] and "503" in c["ai_attempts"][0]["message"]
    assert c["limitations"][0].startswith("Drafted by Gemini 3.5 Flash Lite because Gemini 3.8 Flash was busy")
    assert c["usage"]["perRequest"][0]["model"] == "gemini-3.5-flash-lite" and c["thinking"] == "low"
    fallback_report = r

    # 2. Flash does not answer in time -> no second wait on it, straight to Flash-Lite
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_TIMEOUT="gemini-3.8-flash")
    genai.BUSY_CALLS.clear()
    r = client.post("/api/reports", json=body).json()
    assert r["generator"] == "gemini-3.5-flash-lite" and genai.BUSY_CALLS == ["gemini-3.8-flash"]
    assert r["content"]["fallback"]["reason"] == "not answering in time"

    # 3. both busy -> rule-based draft that names what was tried
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_BUSY="gemini-3.8-flash,gemini-3.5-flash-lite")
    genai.BUSY_CALLS.clear()
    r = client.post("/api/reports", json=body).json()
    assert r["generator"] == "RULE_BASED" and len(genai.BUSY_CALLS) == 3
    assert "Gemini 3.8 Flash, Gemini 3.5 Flash Lite tried" in r["content"]["limitations"][0]
    assert r["content"]["ai_error"] and len(r["content"]["ai_attempts"]) == 3

    # 4. a bad key is not "busy": no retry, no fallback
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_MODE="auth_error")
    genai.BUSY_CALLS.clear()
    n0 = len(genai.REQUESTS)
    r = client.post("/api/reports", json=body).json()
    assert r["generator"] == "RULE_BASED" and len(r["content"]["ai_attempts"]) == 1 and r["content"]["ai_attempts"][0]["kind"] == "fatal"

    # 5. the overall deadline holds: with no time left after a failure nothing else is tried
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_BUSY="gemini-3.8-flash")
    monkeypatch.setattr(reports, "GEMINI_DEADLINE_SEC", 10.0)
    genai.BUSY_CALLS.clear()
    r = client.post("/api/reports", json=body).json()
    assert r["generator"] == "RULE_BASED" and genai.BUSY_CALLS == ["gemini-3.8-flash"]
    monkeypatch.setattr(reports, "GEMINI_DEADLINE_SEC", 150.0)

    # 6. fallback switched off -> rules straight after the chosen model
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_BUSY="gemini-3.8-flash")
    monkeypatch.setattr(reports, "GEMINI_FALLBACK_MODEL", "")
    r = client.post("/api/reports", json=body).json()
    assert r["generator"] == "RULE_BASED"
    evidence["UI-12"] = {"fallback": fallback_report["content"]["fallback"], "attempts": fallback_report["content"]["ai_attempts"]}


def test_ui_13_redraft_with_gemini_in_place(client, app_module, live_incident, monkeypatch, evidence):
    import time as _t
    clip = live_incident["saved"]["clipId"]

    def wait(jid):
        for _ in range(200):
            j = client.get(f"/api/reports/jobs/{jid}").json()
            if j["status"] != "running":
                return j
            _t.sleep(0.05)
        raise AssertionError("job did not finish")

    # a rule-based draft made while both models were busy, with the engineer's notes added
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_BUSY="gemini-3.8-flash,gemini-3.5-flash-lite")
    rep = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": clip, "use_ai": True}).json()
    rid = rep["id"]
    assert rep["generator"] == "RULE_BASED"
    assert client.put(f"/api/reports/{rid}", json={"engineer_notes": "Diver confirmed bend at link 14."}).status_code == 200
    n_reports = len(client.get("/api/reports?limit=2000").json())

    # still busy -> the job fails and the report is untouched
    _set_ai(app_module, monkeypatch, FAKE_GEMINI_BUSY="gemini-3.8-flash,gemini-3.5-flash-lite")
    j = client.post(f"/api/reports/{rid}/redraft")
    assert j.status_code == 202 and j.json()["redraftOf"] == rid
    j = wait(j.json()["id"])
    assert j["status"] == "error" and "still unavailable" in j["error"] and j["reportId"] == rid
    assert client.get(f"/api/reports/{rid}").json()["generator"] == "RULE_BASED"

    # cooldown applies to redrafts like any AI request
    r = client.post(f"/api/reports/{rid}/redraft")
    assert r.status_code == 429

    # Gemini back -> same report id, Gemini draft, notes kept, no extra report in the list
    _set_ai(app_module, monkeypatch)
    j = wait(client.post(f"/api/reports/{rid}/redraft").json()["id"])
    assert j["status"] == "done" and j["reportId"] == rid
    after = client.get(f"/api/reports/{rid}").json()
    assert after["generator"] == "gemini-3.8-flash" and after["content"]["engineer_notes"] == "Diver confirmed bend at link 14."
    assert after["content"]["redrafts"] == 1 and not after["content"]["ai_error"] and after["content"]["fallback"] is None
    assert len(client.get("/api/reports?limit=2000").json()) == n_reports

    # approved reports are locked
    _set_ai(app_module, monkeypatch)
    assert client.post(f"/api/reports/{rid}/approve", json={"engineer": "S. Ajed"}).status_code == 200
    assert client.post(f"/api/reports/{rid}/redraft").status_code == 409
    assert client.post("/api/reports/nope/redraft").status_code == 404
    evidence["UI-13"] = {"report": rid, "generator_after": after["generator"], "notes_kept": True}


def test_ui_14_backend_serves_the_built_ui(client, evidence):
    root = client.get("/")
    assert root.status_code == 200 and "text/html" in root.headers["content-type"] and "Mooring Twin" in root.text
    js = client.get("/assets/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    # the API, the docs and unknown API paths are not swallowed by the UI mount
    h = client.get("/api/health")
    assert h.status_code == 200 and h.json()["status"] == "online"
    assert client.get("/docs").status_code == 200
    assert client.get("/api/does-not-exist").status_code == 404
    with client.websocket_connect("/ws/telemetry") as ws:
        assert ws.receive_json() is not None
    evidence["UI-14"] = {"index": True, "assets": True, "api_not_shadowed": True}

