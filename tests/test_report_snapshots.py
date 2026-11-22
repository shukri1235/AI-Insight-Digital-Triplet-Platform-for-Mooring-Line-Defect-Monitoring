"""
REPORT SNAPSHOTS + AI TOKEN USAGE

RS-1  A live-dive report states what it covers; key frames from the dive's incident clips are
      linked to the dive's own events (so the AI can give a visual verdict on them)
RS-2  Finished live dives are listed so they can be reported after the stream stops
RS-3  Actual Gemini token use is stored per report (and summed when a draft is regenerated)
"""
import pytest

from conftest import wait_for


@pytest.fixture()
def ai_ready(app_module, monkeypatch):
    app_module._ai_state["last_ok"] = 0.0
    monkeypatch.setenv("FAKE_GEMINI_MODE", "ok")
    yield
    app_module._ai_state["last_ok"] = 0.0


def test_rs_1_live_dive_report_coverage_and_keyframes(client, live_incident, ai_ready, evidence):
    sess = live_incident["session_id"]
    rep = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": sess}).json()
    cov = rep["content"]["scope"]["coverage"]
    assert cov["live"] is False and cov["text"].startswith("Whole live dive")
    kfs = rep["content"]["keyframes"]
    event_ids = {f["event_ref"] for f in rep["content"]["findings"]}
    assert kfs and all(k["event_ref"] in event_ids for k in kfs), "key frames must point at the dive's own events"
    deform = next(f for f in rep["content"]["findings"] if f["defect_type"] == "DEFORMATION")
    assert deform["event"]["keyframe"] and deform["visual_assessment"] == "CONFIRMED"
    assert rep["grounding"]["passed"]
    evidence["RS-1"] = {"coverage": cov["text"], "keyframes": [(k["id"], k["event_ref"]) for k in kfs]}


def test_rs_1b_in_progress_dive_says_up_to_when(client, app_module, rov, processed_dive, ai_ready):
    client.post("/api/live/start", json={"source": f"video:{processed_dive['id']}", "line_id": "ML-01-NORTH",
                                         "recording": True, "detection_mode": "STANDBY"})
    try:
        wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 10)
        sess = app_module.live.session_id
        import time
        time.sleep(1.5)
        rep = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": sess, "use_ai": False}).json()
        cov = rep["content"]["scope"]["coverage"]
        assert cov["live"] is True and "data up to T+0:0" in cov["text"] and cov["until_s"] >= 1.0
        dives = client.get("/api/live/dives").json()
        assert dives[0]["id"] == sess and dives[0]["status"] == "LIVE"
    finally:
        client.post("/api/live/stop")
        client.post("/api/live/detection-mode", json={"mode": "ARMED"})
    d = next(x for x in client.get("/api/live/dives").json() if x["id"] == sess)
    assert d["status"] == "ENDED" and d["durationSec"] > 1


def test_rs_2_finished_dives_listed(client, live_incident):
    dives = client.get("/api/live/dives?limit=20").json()   # max; other tests may run live dives first
    d = next(x for x in dives if x["id"] == live_incident["session_id"])
    assert d["status"] == "ENDED" and d["clips"] >= 1 and d["defectCount"] > 0


def test_rs_3_token_usage_recorded(client, live_incident, ai_ready, monkeypatch, app_module, evidence):
    clip = live_incident["clip"]["id"]
    rep = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": clip}).json()
    u = rep["content"]["usage"]
    assert u["requests"] == 1 and u["images"] >= 2 and u["input"] > 1120 * u["images"] and u["output"] > 0
    assert u["total"] == u["input"] + u["output"] + u["thinking"]
    assert "AI usage: 1 request(s)" in client.get(f"/api/reports/{rep['id']}/html").text
    app_module._ai_state["last_ok"] = 0.0
    monkeypatch.setenv("FAKE_GEMINI_MODE", "hallucinate")
    rep2 = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": clip}).json()
    u2 = rep2["content"]["usage"]
    assert u2["requests"] == 2 and u2["input"] == sum(p["input"] for p in u2["perRequest"])
    rb = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": clip, "use_ai": False}).json()
    assert rb["content"]["usage"] is None
    evidence["RS-3"] = {"single": {k: u[k] for k in ("input", "output", "thinking", "total", "images")},
                        "with_regeneration_requests": u2["requests"]}
