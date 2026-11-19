"""Both redaction routes on real PDFs (generated here). No Azure: entities are found by
searching the extracted text, so these tests check the PDF side only: PII really removed,
everything else unchanged."""
import asyncio
import os

import pymupdf
import pytest

from app import redaction as R
from tests.fakes import FakeDocIntel, chars, kerned_pdf, ocr_result, page_text, scanned_pdf, text_pdf

PII = ["Jane Q. Doe", "John Doe", "Jane Doe", "Alan Smith", "March 14, 1985",
       "1234 Maple Avenue, Springfield, IL 62704"]
ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"


def entities_for(layout, values):
    """Entities for every occurrence of the given strings in the extracted text."""
    out = []
    for value in values:
        start = layout.content.find(value)
        while start >= 0:
            page = next(p for p, s, e in layout.pages if s <= start < e)
            out.append({"category": "Test", "offset": start, "length": len(value), "page": page, "confidence": 1.0})
            start = layout.content.find(value, start + 1)
    return out


def redact_text_pdf(pdf, values=PII):
    flat, layout = R._flatten_and_extract(pdf)
    entities = entities_for(layout, values)
    assert entities, "test values not found in the PDF"
    boxes = R._entity_boxes(layout, entities)
    return flat, R._apply_redactions(flat, layout, boxes)


def assert_layout_preserved(before: bytes, after: bytes, page_no: int = 1):
    """Every character still in the output sits exactly where it was."""
    original = chars(before, page_no)
    remaining = chars(after, page_no)
    assert remaining, "page has no text: the image fallback was used"
    moved = [c for c, b in remaining
             if not any(c == c2 and abs(b[0] - b2[0]) < 0.25 and abs(b[1] - b2[1]) < 0.25 for c2, b2 in original)]
    assert moved == [], f"characters moved: {moved[:10]}"


# ─────────────────────────────── text route ───────────────────────────────
@pytest.mark.parametrize("variant", ["base14", "rotated", "embedded-font"])
def test_text_route_removes_pii_and_keeps_layout(variant):
    if variant == "embedded-font" and not os.path.exists(ARIAL):
        pytest.skip("Arial not available")
    pdf = text_pdf(rotate=90 if variant == "rotated" else 0, fontfile=ARIAL if variant == "embedded-font" else None)
    flat, out = redact_text_pdf(pdf)
    text = page_text(out)
    assert not [v for v in ("Jane", "John", "Alan Smith", "Maple", "March 14") if v in text]
    for kept in ("PATIENT INTAKE FORM", "(spouse), phone (217) 555-0199", "05/02/2024", "Contoso Manufacturing"):
        assert kept in text.replace("\n", " ")
    assert_layout_preserved(flat, out)


def test_redacted_text_is_gone_from_the_file_not_just_covered():
    _, out = redact_text_pdf(text_pdf())
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        raw = b"".join(doc.xref_stream(x) or b"" for x in range(1, doc.xref_length()))
    assert b"Jane" not in raw and b"Maple" not in raw


def test_black_boxes_are_drawn():
    flat, layout = R._flatten_and_extract(text_pdf())
    boxes = R._entity_boxes(layout, entities_for(layout, ["Jane Q. Doe"]))
    out = R._apply_redactions(flat, layout, boxes)
    (x0, y0, x1, y1), = boxes[1]
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        pix = doc[0].get_pixmap(dpi=72)
    assert pix.pixel(int((x0 + x1) / 2), int((y0 + y1) / 2)) == (0, 0, 0)


def test_kerned_neighbour_survives():
    # The period after "DEUTDEFF" overlaps the F by 1.2 pt. It must stay.
    pdf = kerned_pdf(kern=100)
    flat, out = redact_text_pdf(pdf, ["DEUTDEFF"])
    assert "DEUTDEFF" not in page_text(out)
    assert "code ." in page_text(out).replace("  ", " ") or ". Next" in page_text(out)
    assert_layout_preserved(flat, out)


def test_form_field_values_are_redacted():
    flat, out = redact_text_pdf(text_pdf(field_value="Guardian: Robert Johnson"), PII + ["Robert Johnson"])
    assert "Robert" not in page_text(out)
    assert "Guardian:" in page_text(out)


def test_metadata_is_stripped():
    _, out = redact_text_pdf(text_pdf(metadata={"author": "Jane Q. Doe", "title": "Intake for Jane Q. Doe"}))
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        assert not any("Jane" in (v or "") for v in doc.metadata.values())


def test_multi_page_documents():
    pdf = text_pdf(pages=3)
    flat, out = redact_text_pdf(pdf)
    for page_no in (1, 2, 3):
        assert_layout_preserved(flat, out, page_no)
    assert "Jane" not in page_text(out)


def test_failed_verification_falls_back_to_an_image(monkeypatch):
    real = R._page_verified
    monkeypatch.setattr(R, "_page_verified", lambda page, n, *a: False if n == 2 else real(page, n, *a))
    _, out = redact_text_pdf(text_pdf(pages=3))
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        assert doc.page_count == 3
        assert doc[1].get_text().strip() == "" and doc[1].get_images()    # page 2: image
        assert doc[0].get_text().strip() and doc[2].get_text().strip()    # others: text
    assert "Jane" not in page_text(out)


def test_pymupdf_version_keeps_text_in_place():
    """PyMuPDF 2.x shifts the rest of a line after a redaction (see requirements.txt). If this
    fails after an upgrade, every redacted page would fall back to an image."""
    assert int(pymupdf.VersionBind.split(".")[0]) < 2, "PyMuPDF 2.x mis-positions text after redactions"
    flat, out = redact_text_pdf(text_pdf(), ["John Doe"])
    assert_layout_preserved(flat, out)


# ─────────────────────────────── scanned route ───────────────────────────────
def test_scanned_pdf_takes_the_ocr_route():
    assert R.needs_ocr(scanned_pdf(text_pdf()))
    assert not R.needs_ocr(text_pdf())


def test_ocr_route_paints_boxes_and_leaves_no_text():
    twin = text_pdf()
    scan = scanned_pdf(twin)
    layout = R._ocr_layout(ocr_result(twin), scan)
    boxes = R._entity_boxes(layout, entities_for(layout, ["Jane"]))
    out = R._rasterize_with_boxes(scan, boxes)
    with pymupdf.open(stream=out, filetype="pdf") as doc:
        assert doc[0].get_text().strip() == ""
        pix = doc[0].get_pixmap(dpi=72)
    for x0, y0, x1, y1 in boxes[1]:
        assert pix.pixel(int((x0 + x1) / 2), int((y0 + y1) / 2)) == (0, 0, 0)


def test_ocr_route_retries_a_dropped_connection(monkeypatch, language):
    twin = text_pdf()
    fake_di = FakeDocIntel(twin, fail_first=1)
    monkeypatch.setattr(R, "DocumentIntelligenceClient", fake_di)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(R.asyncio, "sleep", lambda s: real_sleep(0))
    result = asyncio.run(R._redact_scanned(scanned_pdf(twin), ["Person"]))
    assert fake_di.calls == 2
    assert result["method"] == "ocr" and result["counts"] == {"Person": 4}
