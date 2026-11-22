"""
TEST MODE + GENERAL DETECTIONS (working without the BlueROV2 and before best.pt exists)

TM-1  Class mapping: deformation/breakage and pitting variants map to the two defect types;
      none of the 80 COCO classes (yolo11n) can ever be mistaken for a defect
TM-2  Non-defect detections are recorded in the object log (sensor DB), never in the defect DB
TM-3  Test Mode refuses to start while a real vehicle is connected (no mixed data)
TM-4  Test Mode streams a video as the ROV camera (UDP, network capture path) + simulated
      BlueROV2 telemetry; the live dive, its clips and sensor rows are tagged TEST / SIM
TM-5  Stand-in classes rehearse the defect workflow (clip / alert) and are recorded as such;
      the mapping cannot change during a test dive
TM-6  Batch ingest during Test Mode is tagged TEST and uses the stand-in mapping
TM-7  Registry + segment reports never mix test and real data; test reports are marked
TM-8  Stopping Test Mode stops the test dive and the simulator
"""
import json
import time

import pytest

from conftest import MEDIA, make_dive, upload, wait_completed, wait_for

COCO = ("person bicycle car motorcycle airplane bus train truck boat traffic_light fire_hydrant stop_sign parking_meter "
        "bench bird cat dog horse sheep cow elephant bear zebra giraffe backpack umbrella handbag tie suitcase frisbee "
        "skis snowboard sports_ball kite baseball_bat baseball_glove skateboard surfboard tennis_racket bottle "
        "wine_glass cup fork knife spoon bowl banana apple sandwich orange broccoli carrot hot_dog pizza donut cake "
        "chair couch potted_plant bed dining_table toilet tv laptop mouse remote keyboard cell_phone microwave oven "
        "toaster sink refrigerator book clock vase scissors teddy_bear hair_drier toothbrush").split()


# ---------------------------------------------------------------- TM-1
def test_tm_1_class_mapping():
    from model_manager import canonical_class
    for raw in ("deformation", "Deformed_Link", "breakage", "broken-link", "bent", "fracture", "chain_break"):
        assert canonical_class(raw) == "DEFORMATION", raw
    for raw in ("pitting", "pitting_corrosion", "Pitting Corrosion", "pit", "corrosion"):
        assert canonical_class(raw) == "CORROSION", raw
    assert [c for c in COCO if canonical_class(c)] == [], "a generic model must never produce defects"


# ---------------------------------------------------------------- TM-2
def test_tm_2_general_detections_go_to_object_log_only(client, processed_dive, evidence):
    vid = processed_dive["id"]
    objs = client.get(f"/api/videos/{vid}/objects").json()
    assert objs["counts"] == {"mooringline": 4}, objs["counts"]          # green target 2-4 s at 2 fps
    o = objs["objects"][0]
    assert 2.0 <= o["t"] < 4.0 and o["conf"] == pytest.approx(0.70, abs=1e-3)
    raws = {d["raw_class"] for d in client.get(f"/api/videos/{vid}/defects").json()["defects"]}
    assert "mooringline" not in raws
    import sqlite3
    from config import DB_PATH, SENSOR_DB_PATH
    assert sqlite3.connect(SENSOR_DB_PATH).execute("select count(*) from object_detections").fetchone()[0] >= 4
    assert "object_detections" not in {r[0] for r in sqlite3.connect(DB_PATH).execute("select name from sqlite_master")}
    evidence["TM-2"] = {"object_log": objs["counts"], "defect_raw_classes": sorted(raws)}


# ---------------------------------------------------------------- Test Mode fixture
@pytest.fixture(scope="module")
def no_vehicle(client, rov):
    """The session's simulated 'real' vehicle goes quiet so Test Mode may start."""
    import rov_link
    old = rov_link.ROV_LOST_SEC
    rov_link.ROV_LOST_SEC = 2.0
    rov.silent = True
    def gone_for_good():
        # UDP packets already queued in the socket can still arrive after the vehicle goes quiet
        # under heavy load, so require the link to stay offline for a while
        if client.get("/api/rov/status").json()["state"] != "NO_ROV":
            return False
        time.sleep(1.5)
        return client.get("/api/rov/status").json()["state"] == "NO_ROV"
    wait_for(gone_for_good, 20, 0.2, "vehicle gone")
    yield
    client.post("/api/test-mode/stop")
    rov_link.ROV_LOST_SEC = old
    rov.silent = False
    wait_for(lambda: client.get("/api/rov/status").json()["state"] == "CONNECTED", 8, 0.2, "vehicle back")


def test_tm_3_refuses_while_real_vehicle_connected(client, rov, processed_dive):
    wait_for(lambda: client.get("/api/rov/status").json()["state"] == "CONNECTED", 8, 0.2, "vehicle")
    r = client.post("/api/test-mode/start", json={"video_id": processed_dive["id"]})
    assert r.status_code == 409 and "real BlueROV2" in r.json()["detail"]
    assert client.post("/api/test-mode/start", json={}).status_code == 400
    assert not client.get("/api/test-mode").json()["enabled"]


def _run_until(client, ws, pred, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        m = ws.receive_json()
        r = pred(m)
        if r:
            return r
    return None


def test_tm_4_simulated_rov_camera_and_telemetry(client, no_vehicle, processed_dive, evidence):
    import main
    real_before = client.get("/api/history").json()["counts"]
    r = client.post("/api/test-mode/start", json={"video_id": processed_dive["id"], "target_depth": 120.5,
                                                  "descent_rate": 400})
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["enabled"] and st["simRunning"]
    wait_for(lambda: client.get("/api/test-mode").json()["streaming"], 10, 0.2, "test camera streaming")
    rs = wait_for(lambda: (lambda s: s if s["state"] == "CONNECTED" and s["telemetry"] and abs(s["telemetry"]["depth"] - 120.5) < 0.1 else None)(
        client.get("/api/rov/status").json()), 10, 0.2, "simulated ROV at depth")
    assert rs["simulated"] and rs["telemetry"]["simulated"]
    src = client.get("/api/live/sources").json()[0]
    assert src["kind"] == "test" and src["value"].startswith("udp://")
    with client.websocket_connect("/ws/telemetry") as ws:
        ws.receive_json()
        r = client.post("/api/live/start", json={"source": src["value"], "line_id": "ML-01-NORTH", "trigger_conf": 0.6,
                                                 "detection_mode": "ARMED"}).json()
        assert r["isTest"] and r["sourceKind"] == "network" and r["source"].startswith("TEST ROV camera")
        seen = set()

        def watch(m):
            if m["type"] == "live_state":
                for d in m.get("detections") or []:
                    seen.add((d["type"], d.get("rawClass"), d["isDefect"]))
            return m if m["type"] == "incident" and m["stage"] == "saved" else None
        saved = _run_until(client, ws, watch, 45)
        sess = main.live.session_id
        main.live.flush_db()
        client.post("/api/live/stop")
    assert saved and saved["defect"]["type"] == "DEFORMATION" and saved["defect"]["rawClass"] == "deformation"
    assert ("MOORINGLINE", "mooringline", False) in seen, "general classes are drawn on the live feed"
    clip = client.get(f"/api/videos/{saved['clipId']}").json()
    sess_v = client.get(f"/api/videos/{sess}").json()
    assert clip["isTest"] and sess_v["isTest"] and clip["testProfile"]["simulatedRov"]
    tel = client.get(f"/api/videos/{saved['clipId']}/telemetry").json()
    assert tel and {t["source"] for t in tel} == {"SIM"} and abs(tel[0]["depth"] - 120.5) < 0.1
    assert client.get(f"/api/videos/{sess}/objects").json()["counts"].get("mooringline", 0) > 0
    assert "mooringline" not in {d["raw_class"] for d in client.get(f"/api/videos/{sess}/defects").json()["defects"]}
    assert client.get("/api/history").json()["counts"] == real_before, "test dives never enter the real registry"
    assert client.get("/api/history?test=1").json()["counts"]["DEFORMATION"] >= 1
    evidence["TM-4"] = {"stream": src["value"].split("?")[0], "clip": clip["name"], "telemetry_source": "SIM",
                        "session_objects": client.get(f"/api/videos/{sess}/objects").json()["counts"]}


def test_tm_5_stand_in_rehearses_defects(client, no_vehicle, processed_dive, evidence):
    import main
    assert client.get("/api/test-mode").json()["enabled"]
    r = client.put("/api/test-mode/stand-in", json={"stand_in": {"mooringline": "DEFORMATION"}})
    assert r.status_code == 200 and r.json()["standIn"] == {"mooringline": "DEFORMATION"}
    assert client.put("/api/test-mode/stand-in", json={"stand_in": {"x": "RUST"}}).status_code == 400
    src = client.get("/api/live/sources").json()[0]["value"]
    with client.websocket_connect("/ws/telemetry") as ws:
        ws.receive_json()
        client.post("/api/live/start", json={"source": src, "line_id": "ML-01-NORTH", "trigger_conf": 0.6})
        assert client.put("/api/test-mode/stand-in", json={"stand_in": {}}).status_code == 409, \
            "mapping is fixed for the duration of a test dive"
        stand = _run_until(client, ws, lambda m: next((d for d in (m.get("detections") or []) if d.get("standIn")), None)
                           if m["type"] == "live_state" else None, 30)
        sess = main.live.session_id
        time.sleep(1.0)
        main.live.flush_db()
        client.post("/api/live/stop")
    assert stand["type"] == "DEFORMATION" and stand["rawClass"] == "mooringline" and stand["isDefect"]
    v = client.get(f"/api/videos/{sess}").json()
    assert v["testProfile"]["standIn"] == {"mooringline": "DEFORMATION"}
    rows = client.get(f"/api/videos/{sess}/defects").json()["defects"]
    assert any(d["raw_class"] == "mooringline" and d["defect_type"] == "DEFORMATION" for d in rows)
    evidence["TM-5"] = {"stand_in": v["testProfile"]["standIn"], "logged_as": "DEFORMATION (raw class mooringline)"}


def test_tm_6_batch_during_test_mode(client, no_vehicle, evidence):
    assert client.get("/api/test-mode").json()["standIn"] == {"mooringline": "DEFORMATION"}
    src = make_dive(MEDIA / "test_mode_dive.mp4", seconds=16)
    res = upload(client, src, "test dive", conf=0.25)
    assert res["video"]["isTest"] and res["video"]["testProfile"]["standIn"] == {"mooringline": "DEFORMATION"}
    v = wait_completed(client, res["video"]["id"])
    rows = client.get(f"/api/videos/{v['id']}/defects").json()["defects"]
    assert sum(1 for d in rows if d["raw_class"] == "mooringline" and d["defect_type"] == "DEFORMATION") == 4
    assert client.get(f"/api/videos/{v['id']}/objects").json()["counts"] == {}, "stand-in class is not a general object any more"
    evidence["TM-6"] = {"tagged": True, "stand_in_rows": 4}


def test_tm_7_reports_keep_test_and_real_apart(client, no_vehicle, processed_dive, evidence):
    test_rep = client.post("/api/reports", json={"scope_type": "SEGMENT", "scope_ref": "ML-01-NORTH|Mid-Water Zone",
                                                 "use_ai": False}).json()
    assert test_rep["title"].startswith("[TEST]")
    assert test_rep["content"]["limitations"][0].startswith("TEST DATA")
    assert "mooringline as DEFORMATION" in test_rep["content"]["limitations"][0]
    assert "TEST DATA" in client.get(f"/api/reports/{test_rep['id']}/html").text
    real_dive = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": processed_dive["id"], "use_ai": False}).json()
    assert not real_dive["title"].startswith("[TEST]") and not real_dive["content"].get("test_data")
    evidence["TM-7"] = {"test_report": test_rep["title"], "test_findings": len(test_rep["content"]["findings"])}


def test_tm_8_stop_ends_test_dive_and_simulator(client, no_vehicle, evidence):
    import main
    src = client.get("/api/live/sources").json()[0]["value"]
    client.post("/api/live/start", json={"source": src, "line_id": "ML-01-NORTH"})
    wait_for(lambda: client.get("/api/live/state").json()["status"] == "LIVE", 15, 0.2, "test dive live")
    st = client.post("/api/test-mode/stop").json()
    assert not st["enabled"] and not st["simRunning"]
    assert client.get("/api/live/state").json()["status"] == "IDLE"
    assert not main.rov.simulated
    assert all(s["kind"] != "test" for s in client.get("/api/live/sources").json())
    assert client.post("/api/live/start", json={"source": src}).status_code == 409
    wait_for(lambda: client.get("/api/rov/status").json()["state"] == "NO_ROV", 8, 0.2, "sim gone -> offline")
    # real data after test mode: segment report contains no test media
    real = client.post("/api/reports", json={"scope_type": "SEGMENT", "scope_ref": "ML-01-NORTH|Mid-Water Zone",
                                             "use_ai": False}).json()
    assert not real["title"].startswith("[TEST]")
    assert all(f.get("event", {}).get("inspection") != "test dive" for f in real["content"]["findings"])
    evidence["TM-8"] = {"stopped": True, "rov_after": "NO_ROV"}


def test_tm_9_sim_never_binds_the_backend_port():
    """Windows regression (WinError 10048): pymavlink's udpout socket binds the destination port on
    Windows, colliding with the backend's MAVLink listener on the same PC. The simulator must send
    from a free local port instead, even while another socket holds the destination port."""
    import socket as _s
    from bluerov_sim import BlueRovSim
    hold = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
    hold.bind(("0.0.0.0", 0))                       # stands in for the backend (exclusive bind)
    port = hold.getsockname()[1]
    sim = BlueRovSim(f"udpout:127.0.0.1:{port}", start_depth=5, target=5).start()
    try:
        assert sim._sock.sock.getsockname()[1] != port
        hold.settimeout(3)
        data, _ = hold.recvfrom(4096)               # MAVLink v2 frames arrive at the "backend"
        assert data[:1] == b"\xfd"
    finally:
        sim.stop()
        hold.close()
