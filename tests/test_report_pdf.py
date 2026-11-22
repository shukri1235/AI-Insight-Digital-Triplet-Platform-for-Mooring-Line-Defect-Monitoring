"""
PDF-1  A report exports as a real PDF (not a print page): cover, overview, findings with event ids / depth / segment,
       key-frame images, grade, sign-off, page numbers, DRAFT watermark until approved; a download file name.
PDF-2  Client templates: create, logo upload (PNG/JPG only), colours and company in the PDF, validation errors,
       default template chosen in the app, preview, delete (not the default one).
PDF-3  The approved report carries the engineer and date, and no DRAFT watermark.
"""
import io

import pytest

pypdf = pytest.importorskip("pypdf")


def _text(pdf: bytes) -> str:
    r = pypdf.PdfReader(io.BytesIO(pdf))
    return " ".join(" ".join(p.extract_text() or "" for p in r.pages).split())   # table cells wrap: compare by words


def _png(color=(31, 78, 140)) -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (300, 100), color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture(scope="module")
def ai_report(client, app_module, live_incident):
    import os
    os.environ["GEMINI_API_KEY"] = "AIza-test"
    os.environ.pop("FAKE_GEMINI_BUSY", None)
    os.environ["FAKE_GEMINI_MODE"] = "ok"
    app_module._ai_state["last_ok"] = 0.0
    r = client.post("/api/reports", json={"scope_type": "INCIDENT", "scope_ref": live_incident["saved"]["clipId"], "use_ai": True})
    assert r.status_code == 200, r.text
    return r.json()


def test_pdf_1_report_exports_as_pdf(client, ai_report, evidence):
    r = client.get(f"/api/reports/{ai_report['id']}/pdf")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-" and "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].split("filename=")[1].strip('"').endswith("_draft.pdf")
    reader = pypdf.PdfReader(io.BytesIO(r.content))
    text = _text(r.content)
    assert len(reader.pages) >= 2
    for must in ("Mooring Line Inspection Report", "Inspection overview", "Findings (", "EV-1", "120.5 m", "Mid-Water Zone",
                 "Evidence (key frames)", "Recommended actions", "Sign-off", "Page 1 of", "DRAFT", ai_report["conditionGrade"]):
        assert must in text, f"'{must}' missing from the PDF"
    images = sum(len(p.images) for p in reader.pages)
    assert images >= 2, "key frames are embedded"
    assert reader.metadata.title and "Incident" in reader.metadata.title
    inline = client.get(f"/api/reports/{ai_report['id']}/pdf?download=0")
    assert inline.headers["content-disposition"].startswith("inline")
    assert client.get("/api/reports/nope/pdf").status_code == 404
    assert client.get(f"/api/reports/{ai_report['id']}/pdf?template=missing").status_code == 404
    evidence["PDF-1"] = {"pages": len(reader.pages), "images": images, "bytes": len(r.content)}


def test_pdf_2_client_templates(client, ai_report, evidence):
    lst = client.get("/api/report-templates").json()
    assert any(t["id"] == "default" for t in lst["templates"]) and lst["default"] == "default"
    body = {"name": "ACME Offshore", "company": "ACME Offshore Services", "company_lines": ["Kuala Lumpur"],
            "accent": "#1F4E8C", "title": "Chain Integrity Report", "footer_text": "ACME confidential",
            "header_text": "Client: North Bow FPSO", "cover": True, "sections": {"ai_usage": True}}
    r = client.put("/api/report-templates/acme", json=body)
    assert r.status_code == 200, r.text
    # validation
    assert client.put("/api/report-templates/acme", json={**body, "accent": "blue"}).status_code == 400
    assert client.put("/api/report-templates/Bad Name", json=body).status_code in (400, 404)
    # logo: PNG accepted, text file rejected
    ok = client.post("/api/report-templates/acme/logo", content=_png(), headers={"X-Filename": "acme.png"})
    assert ok.status_code == 200 and ok.json()["logo"] == "acme-logo.png"
    bad = client.post("/api/report-templates/acme/logo", content=b"not an image", headers={"X-Filename": "logo.txt"})
    assert bad.status_code == 400
    assert client.get("/api/report-templates/acme/logo").status_code == 200
    pdf = client.get(f"/api/reports/{ai_report['id']}/pdf?template=acme").content
    text = _text(pdf)
    assert "Chain Integrity Report" in text and "ACME Offshore Services" in text and "ACME confidential" in text
    assert "Client: North Bow FPSO" in text and "Report metadata" in text and "tokens" in text
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    assert sum(len(p.images) for p in reader.pages) >= 3, "logo on the cover and in the page headers"
    # default chosen in the app
    assert client.post("/api/report-templates/default", json={"id": "acme"}).json()["default"] == "acme"
    assert "Chain Integrity Report" in _text(client.get(f"/api/reports/{ai_report['id']}/pdf").content)
    prev = client.get("/api/report-templates/acme/preview")
    assert prev.status_code == 200 and prev.content[:5] == b"%PDF-" and prev.headers["content-disposition"].startswith("inline")
    # remove the logo, delete the template; the default template cannot be deleted
    assert client.delete("/api/report-templates/acme/logo").json()["logo"] is None
    assert client.delete("/api/report-templates/default").status_code == 400
    assert client.delete("/api/report-templates/acme").status_code == 200
    lst = client.get("/api/report-templates").json()
    assert lst["default"] == "default" and not any(t["id"] == "acme" for t in lst["templates"])
    evidence["PDF-2"] = {"template": "acme", "logo": True, "default_switch": True}


def test_pdf_3_approved_report(client, ai_report):
    rid = ai_report["id"]
    assert client.post(f"/api/reports/{rid}/approve", json={"engineer": "S. Ajed"}).status_code == 200
    r = client.get(f"/api/reports/{rid}/pdf")
    text = _text(r.content)
    assert "S. Ajed" in text and "DRAFT" not in text and r.headers["content-disposition"].split("filename=")[1].strip('"').endswith("_approved.pdf")
