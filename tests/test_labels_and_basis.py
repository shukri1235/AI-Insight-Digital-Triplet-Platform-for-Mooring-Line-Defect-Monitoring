"""
LBL-1  ML-01's splash zone is the first 50 m of the water column (API RP 2I): a fresh database is seeded so, a database
       still holding the old 0-30 m boundary is moved to 0-50 m at start-up, and a line someone has edited is left alone.
LBL-2  Labels state the detector's certainty, never a severity: "High confidence" at or above live.critical_conf,
       "Low confidence" below, the same rule for deformation and pitting; old Critical / High / Medium labels are
       relabelled from their stored confidence; the facts sent to Gemini carry confidence_label, not severity; the
       rule-based grade still comes from the number (REPAIR at >= critical_conf); the live alert claims no severity.
LBL-3  Reports call the CORROSION class "Pitting (grade 6-7)" and carry the basis of the defect classes (HTML and PDF).
"""
import io
import json

import pytest

from conftest import ROOT


def test_lbl_1_splash_zone_is_50m(client, evidence):
    import telemetry
    from database import LineSegmentRecord, SessionLocal, _ml01_splash_50m
    with SessionLocal() as db:
        segs = {s.segment_name: s for s in db.query(LineSegmentRecord).filter(LineSegmentRecord.line_id == "ML-01-NORTH")}
        assert (segs["Splash Zone"].depth_start_m, segs["Splash Zone"].depth_end_m) == (0.0, 50.0)
        assert segs["Mid-Water Zone"].depth_start_m == 50.1
        line = {"segments": [{"name": n, "start_m": s.depth_start_m, "end_m": s.depth_end_m} for n, s in segs.items()]}
        assert [telemetry.segment_for(line, d) for d in (25, 49.9, 50.5, 120.5, 152.9)] == \
            ["Splash Zone", "Splash Zone", "Mid-Water Zone", "Mid-Water Zone", "Touchdown Zone"]
        # an existing database with the old boundary is migrated ...
        segs["Splash Zone"].depth_end_m, segs["Mid-Water Zone"].depth_start_m = 30.0, 30.1
        db.commit()
        _ml01_splash_50m(db)
        db.refresh(segs["Splash Zone"]); db.refresh(segs["Mid-Water Zone"])
        assert (segs["Splash Zone"].depth_end_m, segs["Mid-Water Zone"].depth_start_m) == (50.0, 50.1)
        # ... but a boundary someone chose is not
        segs["Splash Zone"].depth_end_m, segs["Mid-Water Zone"].depth_start_m = 40.0, 40.1
        db.commit()
        _ml01_splash_50m(db)
        db.refresh(segs["Splash Zone"])
        assert segs["Splash Zone"].depth_end_m == 40.0
        segs["Splash Zone"].depth_end_m, segs["Mid-Water Zone"].depth_start_m = 50.0, 50.1
        db.commit()
    telemetry.assets.refresh()
    evidence["LBL-1"] = {"splash_m": "0-50", "mid_water_from_m": 50.1, "old_db_migrated": True, "edited_line_kept": True}


def test_lbl_2_labels_are_certainty_not_severity(client, processed_dive, evidence):
    import reports
    from config import CRITICAL_CONF
    from database import DefectRecord, SessionLocal, init_db
    from model_manager import severity_for
    c = CRITICAL_CONF
    assert [severity_for(t, x) for t, x in (("DEFORMATION", c), ("DEFORMATION", c - 0.01), ("CORROSION", c),
                                            ("CORROSION", c - 0.01), ("mooringline", 0.99))] == \
        ["High confidence", "Low confidence", "High confidence", "Low confidence", "Info"]
    # old labels in an existing database are relabelled from the stored confidence
    with SessionLocal() as db:
        rows = db.query(DefectRecord).filter(DefectRecord.video_id == processed_dive["id"]).limit(2).all()
        keep = [(r.id, r.severity) for r in rows]
        rows[0].severity, rows[0].confidence = "Critical", 0.88
        rows[1].severity, rows[1].confidence = "Medium", 0.30
        db.commit()
        ids = [r.id for r in rows]
    init_db()
    with SessionLocal() as db:
        assert [db.get(DefectRecord, i).severity for i in ids] == ["High confidence", "Low confidence"]
        old = db.query(DefectRecord).filter(DefectRecord.severity.in_(["Critical", "Major", "Medium", "High"])).count()
        assert old == 0
        for i, s in keep:
            db.get(DefectRecord, i).severity = s
        db.commit()
    # facts sent to Gemini: a certainty label, no severity word
    with SessionLocal() as db:
        facts, _ = reports.build_facts(db, "DIVE", processed_dive["id"])
    ev = facts["events"]
    assert ev and all("severity" not in e and e["confidence_label"] in ("high", "low") for e in ev)
    assert all(e["confidence_label"] == ("high" if e["peak_confidence"] >= c else "low") for e in ev)
    assert "critical" not in reports.SYSTEM.lower() and "NOT the severity" in reports.SYSTEM
    # the rule-based grade still comes from the number
    draft = reports.rule_based(facts)
    has_high = any(e["type"] == "DEFORMATION" and e["peak_confidence"] >= c for e in ev)
    assert draft["condition_grade"] == ("REPAIR" if has_high else "MONITOR")
    assert "pitting events" in draft["summary"] and "corrosion" not in draft["summary"].lower()
    # the live alert claims no severity
    live = (ROOT / "frontend/src/components/LiveOps.tsx").read_text()
    assert "Deformation clipped" in live and "Critical defect" not in live
    evidence["LBL-2"] = {"labels": "High / Low confidence at %.2f" % c, "grade": draft["condition_grade"],
                         "facts_field": "confidence_label"}


def test_lbl_3_reports_name_pitting_and_carry_the_basis(client, processed_dive, evidence):
    pypdf = pytest.importorskip("pypdf")
    rep = client.post("/api/reports", json={"scope_type": "DIVE", "scope_ref": processed_dive["id"], "use_ai": False}).json()
    html = client.get(f"/api/reports/{rep['id']}/html").text
    assert "Basis of the findings" in html and "API RP 2I" in html and "Lone et al. (2023)" in html
    assert "Pitting (grade 6–7)" in html and "<td>CORROSION</td>" not in html
    pdf = client.get(f"/api/reports/{rep['id']}/pdf").content
    text = " ".join(" ".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(pdf)).pages).split())
    assert "Basis of the findings" in text and "API RP 2I" in text and "not how severe a defect is" in text
    assert "Pitting (grade 6" in text and "Pitting corrosion" not in text
    evidence["LBL-3"] = {"html_basis": True, "pdf_basis": True, "pitting_name": "Pitting (grade 6–7)"}
