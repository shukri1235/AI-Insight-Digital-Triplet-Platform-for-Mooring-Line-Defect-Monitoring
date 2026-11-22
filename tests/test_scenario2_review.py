"""
SCENARIO 2 - Deep-Dive Analysis & Reporting (Review Pipeline)

TC-2.1  The incident clip loads with a DB-backed trigger marker at the exact event time
TC-2.2  The marker maps to the exact analysed frame (frame-accurate timestamps)
TC-2.3  Review shows the threshold the clip was RECORDED with (indicator, not a slider);
        re-analysing at 0.25 creates a new analysis that reveals the secondary pitting on
        the same frame, keeps the original, and never touches the live stream
TC-2.4  Report assistant: Gemini gets a facts packet built from the DB + sensor DB and the
        key frames; its claims are checked in code (wrong depth/event -> regenerated, still
        wrong -> NEEDS_REVIEW); segments come from the ROV depth, not the user's selection
TC-2.5  Token-saver: 30 s cooldown enforced by the SERVER (refresh / second tab cannot bypass);
        engineer edit / approve locks the report
"""
import threading

import cv2
import pytest

from conftest import GT, wait_for


def _defects(client, vid, run_id=None):
    return client.get(f"/api/videos/{vid}/defects" + (f"?run_id={run_id}" if run_id else "")).json()["defects"]


def _at(rows, t, min_conf):
    return sorted((r["defect_type"], r["raw_class"]) for r in rows
                  if abs(r["timestamp_sec"] - t) < 1e-3 and r["confidence"] >= min_conf)


# ---------------------------------------------------------------- TC-2.1
def test_tc_2_1_clip_has_trigger_marker(client, live_incident, evidence):
    clip = live_incident["clip"]
    assert clip["triggerOffsetSec"] is not None
    rows = _defects(client, clip["id"])
    first_deform = min(r["timestamp_sec"] for r in rows if r["defect_type"] == "DEFORMATION")
    assert first_deform == pytest.approx(clip["triggerOffsetSec"], abs=1e-3), \
        "the trigger marker must sit on the first deformation detection"
    evidence["TC-2.1"] = {"trigger_offset_s": clip["triggerOffsetSec"], "first_deformation_s": first_deform}


# ---------------------------------------------------------------- TC-2.2
def test_tc_2_2_marker_snaps_to_exact_frame(app_module, live_incident, evidence):
    """Frame i of the clip is displayed for t in [i/fps, (i+1)/fps). The UI seeks to
    marker + 0.25 frame, so the displayed frame must be the analysed frame and the
    previous frame must not yet contain the defect."""
    clip = live_incident["clip"]
    path = app_module.SessionLocal().get(app_module.VideoRecord, clip["id"]).current_filepath
    fps = clip["fps"]
    i = int((clip["triggerOffsetSec"] + 0.25 / fps) * fps)  # what the browser shows after seekExact()
    cap = cv2.VideoCapture(path)
    frames = {}
    for k in (i - 1, i):
        cap.set(cv2.CAP_PROP_POS_FRAMES, k)
        frames[k] = cap.read()[1]
    x, y, w, h = GT["deformation"]["box"]
    red = lambda f: f[y + h // 2, x + w // 2][2] > 180 and f[y + h // 2, x + w // 2][1] < 70
    assert red(frames[i]), "snapped frame must show the deformed link"
    assert not red(frames[i - 1]), "previous frame is before the event -> marker is frame-exact"
    evidence["TC-2.2"] = {"frame_index": i, "fps": fps, "prev_frame_clean": True}


# ---------------------------------------------------------------- TC-2.3
@pytest.fixture(scope="module")
def reanalysed_clip(client, live_incident):
    clip = live_incident["clip"]
    original = client.get(f"/api/videos/{clip['id']}").json()["activeRun"]
    r = client.post(f"/api/videos/{clip['id']}/analyse", json={"conf": 0.25})
    assert r.status_code == 200, r.text
    rid = r.json()["runId"]
    wait_for(lambda: client.get(f"/api/videos/{clip['id']}").json()["activeRun"]["id"] == rid, 60, 0.3, "re-analysis")
    return {"clip": clip, "original": original, "new_run": rid}


def test_tc_2_3_review_shows_recorded_threshold(client, live_incident, evidence):
    clip = live_incident["clip"]
    run = clip["activeRun"]
    assert run["kind"] == "CLIP" and run["conf"] == pytest.approx(0.40) and run["triggerConf"] == pytest.approx(0.60)
    t = clip["triggerOffsetSec"]
    assert _at(_defects(client, clip["id"]), t, 0) == [("DEFORMATION", "deformation")], \
        "pitting (32 %) is below the recorded 0.40 and must not appear in this analysis"
    evidence["TC-2.3-indicator"] = {"kind": run["kind"], "recorded_conf": run["conf"], "trigger_conf": run["triggerConf"]}


def test_tc_2_3_reanalyse_at_0_25_reveals_pitting(client, reanalysed_clip, evidence):
    clip, orig, rid = reanalysed_clip["clip"], reanalysed_clip["original"], reanalysed_clip["new_run"]
    t = clip["triggerOffsetSec"]
    new_rows = _defects(client, clip["id"])
    assert _at(new_rows, t, 0) == [("CORROSION", "pitting"), ("DEFORMATION", "deformation")]
    # the new analysis is frame-exact too: pitting first appears on the trigger frame
    assert min(r["timestamp_sec"] for r in new_rows if r["raw_class"] == "pitting") == pytest.approx(t, abs=1e-3)
    # audit trail: the original analysis is untouched and still selectable
    runs = client.get(f"/api/videos/{clip['id']}/runs").json()["runs"]
    assert [(r["kind"], r["conf"]) for r in runs] == [("CLIP", 0.4), ("REANALYSIS", 0.25)]
    assert next(r for r in runs if r["id"] == rid)["parentRunId"] == orig["id"]
    assert _at(_defects(client, clip["id"], orig["id"]), t, 0) == [("DEFORMATION", "deformation")]
    assert client.post(f"/api/runs/{orig['id']}/activate").status_code == 200
    assert client.get(f"/api/videos/{clip['id']}").json()["activeRun"]["id"] == orig["id"]
    client.post(f"/api/runs/{rid}/activate")
    evidence["TC-2.3"] = {"original": _at(_defects(client, clip["id"], orig["id"]), t, 0),
                          "reanalysed_0.25": _at(new_rows, t, 0), "runs": [(r["kind"], r["conf"], r["defectCount"]) for r in runs]}


def test_tc_2_3_review_never_changes_live_settings(client, reanalysed_clip):
    before = client.get("/api/live/state").json()
    client.post(f"/api/videos/{reanalysed_clip['clip']['id']}/analyse", json={"conf": 0.5})
    client.get("/api/history")
    after = client.get("/api/live/state").json()
    assert (after["triggerConf"], after["iou"]) == (before["triggerConf"], before["iou"])


def test_tc_2_3_threshold_is_required_and_bounded(client, live_incident):
    vid = live_incident["clip"]["id"]
    assert client.post(f"/api/videos/{vid}/analyse", json={}).status_code == 400
    assert client.post(f"/api/videos/{vid}/analyse", json={"conf": 0.05}).status_code == 400


# ---------------------------------------------------------------- TC-2.4
def _report(client, scope_type, scope_ref, use_ai=True):
    return client.post("/api/reports", json={"scope_type": scope_type, "scope_ref": scope_ref, "use_ai": use_ai})


@pytest.fixture()
def ai_ready(app_module, monkeypatch):
    app_module._ai_state["last_ok"] = 0.0
    monkeypatch.setenv("FAKE_GEMINI_MODE", "ok")
    yield
    app_module._ai_state["last_ok"] = 0.0


def test_tc_2_4_report_is_grounded_in_db_and_key_frames(client, reanalysed_clip, ai_ready, evidence):
    from google import genai
    clip = reanalysed_clip["clip"]
    n0 = len(genai.REQUESTS)
    r = _report(client, "INCIDENT", clip["id"])
    assert r.status_code == 200, r.text
    rep = r.json()
    req = genai.REQUESTS[n0]
    facts = __import__("json").loads(req["texts"][0].split("\n", 1)[1])
    ev = {e["type"]: e for e in facts["events"]}
    # facts come from the database + sensor DB, not from the UI
    assert ev["DEFORMATION"]["depth_m"] == pytest.approx(120.5, abs=0.05) and ev["DEFORMATION"]["segment"] == "Mid-Water Zone"
    assert ev["DEFORMATION"]["peak_confidence"] == pytest.approx(0.88, abs=1e-3)
    assert ev["CORROSION"]["peak_confidence"] == pytest.approx(0.32, abs=1e-3)      # from the 0.25 re-analysis
    assert facts["analysis"]["threshold"] == pytest.approx(0.25) and facts["asset"]["seabed_depth_m"] == 200
    assert facts["sensors"]["source"] == "MAVLINK"
    # the model sees the evidence: full frame + crop for each event's key frame
    assert req["images"] == 2 * len(rep["content"]["keyframes"]) and req["images"] >= 2
    assert req["schema"] is not None and req["system"]
    assert rep["status"] == "DRAFT" and rep["grounding"]["passed"] and rep["conditionGrade"] == "REPAIR"
    for f in rep["content"]["keyframes"][0]["files"]:
        assert client.get(f"/api/reports/{rep['id']}/frames/{f}").status_code == 200
    evidence["TC-2.4"] = {"events": [(e["id"], e["type"], e["depth_m"], e["segment"]) for e in facts["events"]],
                          "images_sent": req["images"], "grade": rep["conditionGrade"], "status": rep["status"]}


def test_tc_2_4_segment_comes_from_rov_depth_not_selection(client, processed_dive, live_incident, ai_ready, evidence):
    """The use case asks for a 'Touchdown Zone' report about a defect seen at 120 m. The
    120 m events belong to the Mid-Water Zone, so a Touchdown report must not contain them."""
    td = _report(client, "SEGMENT", "ML-01-NORTH|Touchdown Zone", use_ai=False).json()
    mw = _report(client, "SEGMENT", "ML-01-NORTH|Mid-Water Zone", use_ai=False).json()
    assert all(f["segment"] == "Touchdown Zone" for f in td["content"]["findings"])
    assert not any(abs((f["depth_m"] or 0) - 120.5) < 0.1 for f in td["content"]["findings"])
    assert any(f["defect_type"] == "DEFORMATION" and abs(f["depth_m"] - 120.5) < 0.1 for f in mw["content"]["findings"])
    evidence["TC-2.4-segment"] = {"touchdown_findings": len(td["content"]["findings"]),
                                  "midwater_findings": len(mw["content"]["findings"])}


def test_tc_2_4_hallucination_is_caught_and_regenerated(client, live_incident, ai_ready, monkeypatch, evidence):
    monkeypatch.setenv("FAKE_GEMINI_MODE", "hallucinate")
    rep = _report(client, "INCIDENT", live_incident["clip"]["id"]).json()
    g = rep["grounding"]
    assert g["attempts"] == 2 and g["passed"] and rep["status"] == "DRAFT"
    assert any("EV-99" in e for e in g["firstAttemptErrors"]) and any("999" in e for e in g["firstAttemptErrors"])
    evidence["TC-2.4-hallucination"] = {"first_attempt_errors": g["firstAttemptErrors"], "final": "grounded"}


def test_tc_2_4_persistent_hallucination_needs_review(client, live_incident, ai_ready, monkeypatch):
    monkeypatch.setenv("FAKE_GEMINI_MODE", "always_bad")
    rep = _report(client, "INCIDENT", live_incident["clip"]["id"]).json()
    assert rep["status"] == "NEEDS_REVIEW" and not rep["grounding"]["passed"] and rep["grounding"]["errors"]


def test_tc_2_4_rule_based_without_api_key(client, app_module, live_incident, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY")
    app_module._ai_state["last_ok"] = 0.0
    rep = _report(client, "INCIDENT", live_incident["clip"]["id"]).json()
    assert rep["generator"] == "RULE_BASED" and rep["grounding"]["passed"] and rep["conditionGrade"] in ("MONITOR", "REPAIR")
    assert client.get("/api/ai/status").json()["cooldownRemaining"] == 0, "fallback must not start the cooldown"


# ---------------------------------------------------------------- TC-2.5
def test_tc_2_5_token_saver_enforced_server_side(client, app_module, live_incident, ai_ready, evidence):
    vid = live_incident["clip"]["id"]
    assert _report(client, "INCIDENT", vid).status_code == 200
    r2 = _report(client, "INCIDENT", vid)                         # "second tab" / refresh
    assert r2.status_code == 429
    retry = int(r2.headers["Retry-After"])
    assert 28 <= retry <= 30
    st = client.get("/api/ai/status").json()
    assert 27 < st["cooldownRemaining"] <= 30
    assert _report(client, "INCIDENT", vid, use_ai=False).status_code == 200, "rule-based drafts are never blocked"
    app_module._ai_state["last_ok"] -= 31                         # 31 s later
    assert _report(client, "INCIDENT", vid).status_code == 200
    evidence["TC-2.5"] = {"second_call": 429, "retry_after_s": retry, "after_30s": 200}


def test_tc_2_5_concurrent_requests_only_one_spends_tokens(client, live_incident, ai_ready):
    from google import genai
    n0 = len(genai.REQUESTS)
    codes = []
    ths = [threading.Thread(target=lambda: codes.append(_report(client, "INCIDENT", live_incident["clip"]["id"]).status_code))
           for _ in range(4)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert sorted(codes) == [200, 429, 429, 429]
    assert len(genai.REQUESTS) - n0 == 1


def test_tc_2_5_engineer_edit_approve_lock_and_export(client, live_incident, ai_ready, evidence):
    rep = _report(client, "INCIDENT", live_incident["clip"]["id"]).json()
    ev = rep["content"]["findings"][0]["event_ref"]
    r = client.put(f"/api/reports/{rep['id']}", json={"condition_grade": "MONITOR", "engineer_notes": "Measured OK",
                                                       "findings": [{"event_ref": ev, "visual_assessment": "UNCERTAIN"}]})
    assert r.status_code == 200 and r.json()["status"] == "EDITED"
    assert client.put(f"/api/reports/{rep['id']}", json={"condition_grade": "BROKEN"}).status_code == 400
    assert client.put(f"/api/reports/{rep['id']}", json={"recommended_actions": [{"action": "PAINT_IT"}]}).status_code == 400
    assert client.post(f"/api/reports/{rep['id']}/approve", json={"engineer": " "}).status_code == 400
    a = client.post(f"/api/reports/{rep['id']}/approve", json={"engineer": "S. Ajed"}).json()
    assert a["status"] == "APPROVED" and a["engineer"] == "S. Ajed" and a["conditionGrade"] == "MONITOR"
    assert client.put(f"/api/reports/{rep['id']}", json={"summary": "changed"}).status_code == 409
    html = client.get(f"/api/reports/{rep['id']}/html").text
    assert "S. Ajed" in html and "MONITOR" in html and "Measured OK" in html
    evidence["TC-2.5-signoff"] = {"edited": True, "approved_by": a["engineer"], "locked": True, "html_bytes": len(html)}
