"""In-memory stand-ins for the Azure services, plus PDF builders, so tests run offline.

FakeLanguage        Azure Language PII detection: returns entities for known strings,
                    including Azure's quirks (subtypes, noisy ID detectors, plain dates).
FakeDocIntel        Document Intelligence prebuilt-read: OCR words taken from a "text twin".
"""
import re
from types import SimpleNamespace

import pymupdf


# ─────────────────────────────── Azure Language ───────────────────────────────
# (text, type, extra tags). An entity is returned when the request asks for its type or a tag,
# like the real service. Quirks copied from what the real service did in testing:
#   - the birth date comes back as a plain Date (as it does when label and date are in
#     separate table cells), so the app's birth-label rule has to reclassify it;
#   - the driver's license detector also flags bare numbers ("42", "500");
#   - the passport number is reported as an ITIN;
#   - "Insurance" is reported as the subtype OrganizationSports;
#   - a URL is reported with the sentence's full stop included.
DEFAULT_RULES = [
    ("Jane Q. Doe", "Person", []), ("John Doe", "Person", []), ("Jane Doe", "Person", []),
    ("Alan Smith", "Person", []),
    ("March 14, 1985", "Date", ["Date", "DateTime"]),
    ("05/02/2024", "Date", ["Date", "DateTime"]),
    ("1234 Maple Avenue, Springfield, IL 62704", "Address", []),
    ("(217) 555-0142", "PhoneNumber", []), ("(217) 555-0199", "PhoneNumber", []),
    ("jane.doe@example.com", "Email", []),
    ("Contoso Manufacturing", "Organization", []),
    ("Insurance", "OrganizationSports", ["OrganizationSports", "Organization"]),
    ("123-45-6789", "USSocialSecurityNumber", []),
    ("D1234567", "USDriversLicenseNumber", []),
    ("42", "USDriversLicenseNumber", []), ("500", "USDriversLicenseNumber", []),
    ("912803456", "USIndividualTaxpayerIdentification", []),
    ("https://portal.example.org/jdoe.", "URL", []),
]


class FakeLanguage:
    def __init__(self, rules=None):
        self.rules = DEFAULT_RULES if rules is None else rules
        self.calls: list[dict] = []

    async def post(self, http, url, params, body, headers):
        self.calls.append(body)
        requested = set(body["parameters"]["piiCategories"])
        docs = []
        for doc in body["analysisInput"]["documents"]:
            entities = []
            for text, etype, tags in self.rules:
                if not requested & ({etype} | set(tags)):
                    continue
                for m in re.finditer(r"(?<![\w.])" + re.escape(text) + r"(?!\w)", doc["text"]):
                    entities.append({"text": text, "type": etype, "category": etype,
                                     "tags": [{"name": t} for t in tags or [etype]],
                                     "offset": m.start(), "length": len(text), "confidenceScore": 0.9})
            docs.append({"id": doc["id"], "entities": entities, "warnings": []})
        return SimpleNamespace(json=lambda: {"results": {"documents": docs, "errors": []}},
                               status_code=200, is_error=False)


# ─────────────────────────────── Document Intelligence ───────────────────────────────
class FakeDocIntel:
    """OCR that "reads" the words of a text PDF (the scan's twin), reported like DI does for
    PDFs: page size and word polygons in inches. fail_first makes the first call raise a
    connection error, to exercise the retry."""

    def __init__(self, twin_pdf: bytes, fail_first: int = 0):
        self.twin_pdf, self.fail_first, self.calls = twin_pdf, fail_first, 0

    def __call__(self, endpoint, credential):   # stands in for the client class
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def begin_analyze_document(self, model, request, string_index_type=None):
        from azure.core.exceptions import ServiceRequestError
        self.calls += 1
        if self.calls <= self.fail_first:
            raise ServiceRequestError("Connection reset")
        result = ocr_result(self.twin_pdf)

        class _Poller:
            async def result(self):
                return result
        return _Poller()


def ocr_result(pdf: bytes):
    parts, pages, pos = [], [], 0
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, start=1):
            start, words = pos, []
            for x0, y0, x1, y1, text, *_ in page.get_text("words", sort=True):
                words.append(SimpleNamespace(
                    content=text, span=SimpleNamespace(offset=pos, length=len(text)),
                    polygon=[v / 72 for v in (x0, y0, x1, y0, x1, y1, x0, y1)]))
                parts.append(text + " ")
                pos += len(text) + 1
            pages.append(SimpleNamespace(page_number=page_no, width=page.rect.width / 72,
                                         height=page.rect.height / 72, words=words,
                                         spans=[SimpleNamespace(offset=start, length=pos - start)]))
    return SimpleNamespace(content="".join(parts), pages=pages)


# ─────────────────────────────── PDF builders ───────────────────────────────
INTAKE_TEXT = """PATIENT INTAKE FORM
Patient name: Jane Q. Doe
Date of birth: March 14, 1985
Home address: 1234 Maple Avenue, Springfield, IL 62704
Phone: (217) 555-0142        Email: jane.doe@example.com
Employer: Contoso Manufacturing
Emergency contact: John Doe (spouse), phone (217) 555-0199
Notes: Patient Jane Doe was referred by Dr. Alan Smith on 05/02/2024 for a
follow-up visit. Insurance member ID on file.
"""


def text_pdf(text: str = INTAKE_TEXT, pages: int = 1, rotate: int = 0, fontfile: str | None = None,
             metadata: dict | None = None, field_value: str | None = None) -> bytes:
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        font = {"fontname": "F0", "fontfile": fontfile} if fontfile else {"fontname": "helv"}
        page.insert_textbox(pymupdf.Rect(60, 60, 550, 780), text, fontsize=11, **font)
        if rotate:
            page.set_rotation(rotate)
        if field_value:
            w = pymupdf.Widget()
            w.field_type, w.field_name = pymupdf.PDF_WIDGET_TYPE_TEXT, "extra"
            w.rect, w.field_value = pymupdf.Rect(72, 400, 400, 420), field_value
            page.add_widget(w)
    if metadata:
        doc.set_metadata(metadata)
    return doc.tobytes()


def kerned_pdf(kern: int = 100) -> bytes:
    """One line whose final "F." pair is kerned together (the period's box overlaps the F's by
    kern/1000 * 12 pt), like real fonts do. Redacting "DEUTDEFF" must not delete the period."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "x", fontname="helv", fontsize=12)   # registers the /helv font
    xref = page.get_contents()[0]
    doc.update_stream(xref, b"BT /helv 12 Tf 72 700 Td [(SWIFT code DEUTDEF)0(F)%d(.)0( Next line)]TJ ET" % kern)
    return doc.tobytes()


def scanned_pdf(text_twin: bytes, dpi: int = 150) -> bytes:
    """An image-only PDF: each page of the twin rendered to a picture (no text layer)."""
    out = pymupdf.open()
    with pymupdf.open(stream=text_twin, filetype="pdf") as doc:
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, stream=pix.tobytes("png"))
    return out.tobytes()


def encrypted_pdf() -> bytes:
    with pymupdf.open(stream=text_pdf(), filetype="pdf") as doc:
        return doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="secret")


def chars(pdf: bytes, page_no: int = 1) -> list[tuple[str, tuple]]:
    """Non-space characters with their boxes, as extracted by the app."""
    from app.redaction import _page_chars
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [(c, b) for c, b, _ in _page_chars(doc[page_no - 1]) if not c.isspace()]


def page_text(pdf: bytes) -> str:
    """All text in the PDF, with look-alike spaces and hyphens normalized like the app does."""
    from app.redaction import _NORMALIZE
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return "".join(p.get_text() for p in doc).translate(_NORMALIZE)
