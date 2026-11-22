"""
REGISTRY BY DEPTH ZONE

SZ-1  Without ROV depth, a dive's events are "unknown depth" (line total only, no zone)
SZ-2  With a sensor log, each event is placed in the zone of the ROV depth at its peak frame
SZ-3  Zone counts + unknown-depth always add up to the line totals (same event rule)
"""
import pytest

from conftest import MEDIA
from vv_media import make_tlog


def _breakdown(app_module, vid):
    import segments
    from telemetry import assets
    with app_module.SessionLocal() as db:
        v = db.get(app_module.VideoRecord, vid)
        return segments.video_breakdown(db, vid, v.active_run_id, assets.get(v.line_id))


def _consistent(h):
    for t in ("CORROSION", "DEFORMATION"):
        assert sum(z["counts"][t] for z in h["segments"].values()) + h["unknownDepth"]["counts"][t] == h["counts"][t], t
        assert sum(z["detections"][t] for z in h["segments"].values()) + h["unknownDepth"]["detections"][t] == h["detections"][t], t


def test_sz_1_and_2_events_follow_rov_depth(client, app_module, processed_dive, evidence):
    vid = processed_dive["id"]
    import sensors_db
    sensors_db.delete_samples(vid)
    b = _breakdown(app_module, vid)
    assert all(sum(z["events"].values()) == 0 for z in b["segments"].values())
    assert b["unknown"]["events"] == {"CORROSION": 2, "DEFORMATION": 1}      # pitting 8-12 s, corrosion 14-17 s, deformation 8-11 s
    _consistent(client.get("/api/history?line_id=ML-01-NORTH").json())

    # ROV descends 20 m + 10 m/s from video start: 8 s -> 100 m (Mid-Water), 14 s -> 160 m (Touchdown)
    tl = make_tlog(MEDIA / "zones.tlog", 22, lambda t: 20.0 + 10.0 * t)
    assert client.post(f"/api/videos/{vid}/tlog-upload?offset_sec=0", content=tl.read_bytes()).status_code == 200
    b = _breakdown(app_module, vid)
    assert b["segments"]["Mid-Water Zone"]["events"] == {"CORROSION": 1, "DEFORMATION": 1}
    assert b["segments"]["Touchdown Zone"]["events"] == {"CORROSION": 1, "DEFORMATION": 0}
    assert b["segments"]["Splash Zone"]["events"] == {"CORROSION": 0, "DEFORMATION": 0}
    assert b["unknown"]["events"] == {"CORROSION": 0, "DEFORMATION": 0}
    assert set(b["covered"]) == {"Splash Zone", "Mid-Water Zone", "Touchdown Zone"}
    h = client.get("/api/history?line_id=ML-01-NORTH").json()
    _consistent(h)
    assert h["segments"]["Touchdown Zone"]["counts"]["CORROSION"] >= 1
    evidence["SZ-2"] = {k: v["events"] for k, v in b["segments"].items()}


def test_sz_3_no_line_means_no_zones(client):
    h = client.get("/api/history").json()
    assert "segments" not in h and "counts" in h
