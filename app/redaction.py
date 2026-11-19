"""PDF PII redaction.

Both routes build a TextLayout (page text plus the position of every character or word, in
PDF points), send the text to Azure Language PII detection, and map the PII back to boxes.

Route "text": text-based PDFs, layout preserved.
    Text and positions come from the PDF itself (PyMuPDF). PII is removed with true
    redactions: the characters are deleted from the page content and a black box is drawn
    where they were. Fonts, line breaks, images and everything else stay unchanged.

Route "ocr": scanned / image-based PDFs.
    Document Intelligence prebuilt-read OCR provides words and positions. Each page is
    rendered to an image with the PII areas painted black, so no hidden text survives.
"""
import asyncio
import logging
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import httpx
import pymupdf
from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
from azure.core.exceptions import ServiceRequestError

from . import config
from .auth import credential
from .categories import AZURE_IDS, validate_categories

log = logging.getLogger(__name__)
COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
CONNECT_ATTEMPTS = 4

Box = tuple[float, float, float, float]


@dataclass
class Piece:
    """A character (text route) or word (OCR route) with its content offsets and position."""
    start: int
    end: int
    page: int                 # 1-based
    rect: Box                 # PDF points, page coordinates
    line: int | None = None   # pieces on the same line merge into one box


@dataclass
class TextLayout:
    content: str = ""
    pages: list[tuple[int, int, int]] = field(default_factory=list)   # (page, start, end)
    pieces: list[Piece] = field(default_factory=list)


# ─────────────────────────────── public entry point ───────────────────────────────
async def redact_pdf(pdf_bytes: bytes, categories: list[str], mode: str = "auto") -> dict:
    """Redact a PDF given as bytes. Nothing is stored; the caller decides what to keep.

    mode: "auto" (detect), "text", or "ocr".
    Returns {"method", "pdf" (redacted bytes), "counts" {category: n}}. Never contains PII values.
    """
    categories = validate_categories(categories)
    use_ocr = mode == "ocr" or (mode == "auto" and await asyncio.to_thread(needs_ocr, pdf_bytes))
    log.info("Redacting via %s route, categories=%s", "ocr" if use_ocr else "text", categories)
    if use_ocr:
        return await _redact_scanned(pdf_bytes, categories)
    return await _redact_text(pdf_bytes, categories)


def needs_ocr(pdf_bytes: bytes, image_coverage: float = 0.5, min_chars: int = 20) -> bool:
    """True if any page looks scanned.

    A large image on a page means the visible content is pixels. Even if the PDF has an
    invisible OCR text layer, redacting that text would leave the visible image untouched,
    so such documents must take the OCR route.
    """
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page in doc:
            page_area = abs(page.rect) or 1
            for info in page.get_image_info():
                if abs(pymupdf.Rect(info["bbox"]) & page.rect) / page_area >= image_coverage:
                    return True
            if page.get_images() and len(page.get_text().strip()) < min_chars:
                return True
    return False


def _result(method: str, pdf: bytes, entities: list[dict]) -> dict:
    return {"method": method, "pdf": pdf, "counts": dict(Counter(e["category"] for e in entities))}


# ─────────────────────── Route "text": layout-preserving redaction ───────────────────────
async def _redact_text(pdf_bytes: bytes, categories: list[str]) -> dict:
    flat_pdf, layout = await asyncio.to_thread(_flatten_and_extract, pdf_bytes)
    entities = await _detect_pii(layout, categories)
    boxes = _entity_boxes(layout, entities)
    redacted_pdf = await asyncio.to_thread(_apply_redactions, flat_pdf, layout, boxes)
    return _result("text", redacted_pdf, entities)


_EXTRACT_FLAGS = pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_MEDIABOX_CLIP


# Look-alike characters PDFs often contain, mapped one-to-one (so offsets don't change) to the
# plain ones PII detection and the app's patterns expect: "555\u00ad0142" -> "555-0142".
_NORMALIZE = str.maketrans({
    "\u00a0": " ", "\u2007": " ", "\u202f": " ", "\u2009": " ", "\u200a": " ",   # spaces
    "\u00ad": "-", "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2212": "-",  # hyphens
})


def _page_chars(page) -> list[tuple[str, Box, int]]:
    """Every character on the page as (char, bbox, line_number), in reading order."""
    chars, line_no = [], 0
    for block in page.get_text("rawdict", flags=_EXTRACT_FLAGS)["blocks"]:
        for line in block.get("lines", []):
            line_no += 1
            chars.extend((ch["c"].translate(_NORMALIZE), tuple(ch["bbox"]), line_no)
                         for span in line["spans"] for ch in span["chars"])
    return chars


def _flatten_and_extract(pdf_bytes: bytes) -> tuple[bytes, TextLayout]:
    """Bake form fields and annotations into the page content (so their text is detected and
    redacted like any other text), then extract every character with its position."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        doc.bake()
        flat = doc.tobytes()
    with pymupdf.open(stream=flat, filetype="pdf") as doc:
        layout, parts, pos, line_base = TextLayout(), [], 0, 0
        for page_no, page in enumerate(doc, start=1):
            page_start, last_line = pos, None
            for c, bbox, line_no in _page_chars(page):
                if last_line is not None and line_no != last_line:
                    parts.append("\n")
                    pos += 1
                last_line = line_no
                if not c.isspace():
                    layout.pieces.append(Piece(pos, pos + len(c), page_no, bbox, line_base + line_no))
                parts.append(c)
                pos += len(c)
            parts.append("\n")
            pos += 1
            line_base += (last_line or 0)
            layout.pages.append((page_no, page_start, pos))
        layout.content = "".join(parts)
    return flat, layout


def _apply_redactions(pdf_bytes: bytes, layout: TextLayout, boxes: dict[int, list[Box]]) -> bytes:
    """Delete the characters under each box and draw the box in black, then verify each page:
    every other character must keep its exact position and no redacted character may remain.
    A page that fails verification is replaced by an image of itself with the boxes painted
    black (layout identical, but not selectable). Finally strip side channels (metadata,
    links, attachments, JavaScript, hidden text) that could also carry PII."""
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for page_no, rects in boxes.items():
            page = doc[page_no - 1]
            for x0, y0, x1, y1 in rects:
                # MuPDF deletes every character whose box touches the area. Kerned neighbours
                # overlap by up to ~1.2 pt ("F." in DEUTDEFF.), so shrink the deletion area;
                # the redacted characters themselves still lie well inside it.
                dx = min(KERN_ALLOWANCE, (x1 - x0) / 4)
                dy = (y1 - y0) * 0.2
                page.add_redact_annot(pymupdf.Rect(x0 + dx, y0 + dy, x1 - dx, y1 - dy), fill=False, cross_out=False)
            # Remove only the redacted characters; keep images and line art around them.
            # PyMuPDF 2.x leaves the first removed glyph's width out of the gap it writes, shifting
            # the rest of the line; 1.26 is correct. Verification below catches any such shift.
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                                  graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                                  text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            for x0, y0, x1, y1 in rects:   # the visible black box covers the full area
                page.draw_rect(pymupdf.Rect(x0, y0, x1, y1), color=None, fill=(0, 0, 0), overlay=True)

        failed = [n for n in boxes if not _page_verified(doc[n - 1], n, layout, boxes[n])]
        if failed:
            log.warning("Text redaction could not be verified on page(s) %s; using image fallback", failed)
            with pymupdf.open(stream=pdf_bytes, filetype="pdf") as src:
                for page_no in failed:
                    _replace_with_image(doc, src, page_no, boxes[page_no])

        doc.scrub(redactions=False, reset_fields=False)
        return doc.tobytes(garbage=4, deflate=True, clean=True)


POSITION_TOLERANCE = 0.25   # points
KERN_ALLOWANCE = 1.25       # points


def _page_verified(page, page_no: int, layout: TextLayout, rects: list[Box]) -> bool:
    def covered(b: Box) -> bool:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in rects)

    expected = [(layout.content[p.start:p.end], p.rect) for p in layout.pieces if p.page == page_no]
    remaining: dict[str, list[Box]] = defaultdict(list)
    for c, bbox, _ in _page_chars(page):
        if not c.isspace():
            remaining[c].append(bbox)

    def present(c: str, b: Box) -> bool:
        return any(abs(r[0] - b[0]) <= POSITION_TOLERANCE and abs(r[1] - b[1]) <= POSITION_TOLERANCE
                   for r in remaining.get(c, ()))

    for c, b in expected:
        if covered(b) == present(c, b):   # redacted but still there, or kept but moved/missing
            return False
    return True


def _replace_with_image(doc, src, page_no: int, rects: list[Box], pad_pt: float = 1.0) -> None:
    page = src[page_no - 1]
    zoom = pymupdf.Matrix(config.OCR_OUTPUT_DPI / 72, config.OCR_OUTPUT_DPI / 72)
    pix = page.get_pixmap(matrix=zoom, colorspace=pymupdf.csRGB, alpha=False)
    for x0, y0, x1, y1 in rects:
        r = pymupdf.Rect(x0 - pad_pt, y0 - pad_pt, x1 + pad_pt, y1 + pad_pt) * zoom
        pix.set_rect(r.irect & pix.irect, (0, 0, 0))
    width, height = page.rect.width, page.rect.height
    doc.delete_page(page_no - 1)
    new_page = doc.new_page(pno=page_no - 1, width=width, height=height)
    new_page.insert_image(new_page.rect, stream=pix.tobytes("jpeg", jpg_quality=90))


# ─────────────────────── Route "ocr": scanned PDFs ───────────────────────
async def _redact_scanned(pdf_bytes: bytes, categories: list[str]) -> dict:
    async with DocumentIntelligenceClient(config.DOCINTEL_ENDPOINT, credential) as di:
        # The SDK never retries a POST after a connection error, so one dropped connection
        # would fail the job. ServiceRequestError means the request wasn't sent, so retry it.
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                poller = await di.begin_analyze_document(
                    "prebuilt-read",
                    AnalyzeDocumentRequest(bytes_source=pdf_bytes),
                    string_index_type="unicodeCodePoint",   # offsets match Python string indexing
                )
                break
            except ServiceRequestError:
                if attempt == CONNECT_ATTEMPTS - 1:
                    raise
                log.warning("Document Intelligence connection failed; retrying (%d)", attempt + 1)
                await asyncio.sleep(2 ** attempt)
        ocr = await poller.result()

    layout = await asyncio.to_thread(_ocr_layout, ocr, pdf_bytes)
    entities = await _detect_pii(layout, categories)
    boxes = _entity_boxes(layout, entities)
    redacted_pdf = await asyncio.to_thread(_rasterize_with_boxes, pdf_bytes, boxes)
    return _result("ocr", redacted_pdf, entities)


def _ocr_layout(ocr, pdf_bytes: bytes) -> TextLayout:
    """Convert Document Intelligence output to a TextLayout in PDF points."""
    layout = TextLayout(content=ocr.content or "")
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        for p in ocr.pages:
            for span in p.spans or []:
                layout.pages.append((p.page_number, span.offset, span.offset + span.length))
            if not (p.width and p.height) or p.page_number > doc.page_count:
                continue
            # DI reports PDF coordinates in inches (pixels for images); scale to PDF points.
            rect = doc[p.page_number - 1].rect
            sx, sy = rect.width / p.width, rect.height / p.height
            for w in p.words or []:
                if w.polygon:
                    xs, ys = w.polygon[0::2], w.polygon[1::2]
                    layout.pieces.append(Piece(w.span.offset, w.span.offset + w.span.length, p.page_number,
                                               (min(xs) * sx, min(ys) * sy, max(xs) * sx, max(ys) * sy)))
    return layout


def _rasterize_with_boxes(pdf_bytes: bytes, boxes: dict[int, list[Box]], pad_pt: float = 2.0) -> bytes:
    """Render every page to an image, paint boxes black, and build an image-only PDF."""
    zoom = pymupdf.Matrix(config.OCR_OUTPUT_DPI / 72, config.OCR_OUTPUT_DPI / 72)
    src, out = pymupdf.open(stream=pdf_bytes, filetype="pdf"), pymupdf.open()
    try:
        for page_no, page in enumerate(src, start=1):
            pix = page.get_pixmap(matrix=zoom, colorspace=pymupdf.csRGB, alpha=False)
            for x0, y0, x1, y1 in boxes.get(page_no, []):
                r = pymupdf.Rect(x0 - pad_pt, y0 - pad_pt, x1 + pad_pt, y1 + pad_pt) * zoom
                pix.set_rect(r.irect & pix.irect, (0, 0, 0))
            new_page = out.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=pix.tobytes("jpeg", jpg_quality=85))
        return out.tobytes(garbage=4, deflate=True)
    finally:
        src.close()
        out.close()


# ─────────────────────── shared: PII detection and box mapping ───────────────────────
MAX_CHARS, BATCH_SIZE = 5000, 5   # Language sync limits: ~5,120 chars/document, 5 documents/request


def _text_chunks(layout: TextLayout):
    """Yield (global_offset, page_number, text): per page, split at line/space boundaries."""
    content = layout.content
    for page_no, start, end in layout.pages:
        while start < end:
            stop = min(start + MAX_CHARS, end)
            if stop < end:
                cut = max(content.rfind("\n", start, stop), content.rfind(" ", start, stop))
                if cut > start:
                    stop = cut
            if content[start:stop].strip():
                yield start, page_no, content[start:stop]
            start = stop


# Azure's driver's license, passport and ITIN detectors also flag ordinary numbers (ages, dates,
# dosages, parts of IP addresses) with the same confidence as real IDs. Keep those hits only
# when a matching label appears shortly before them, e.g. "Driver's license D1234567".
CONTEXT_REQUIRED = {
    "USDriversLicenseNumber": re.compile(r"licen[cs]e|\bDL\b|driver", re.I),
    "USUKPassportNumber": re.compile(r"passport", re.I),
    "USIndividualTaxpayerIdentification": re.compile(r"\bITIN\b|\bTIN\b|taxpayer|tax\s*id", re.I),
}
CONTEXT_WINDOW = 40   # characters before the entity

# A date directly after one of these labels is a birth date. In form tables the label and the
# date sit in separate cells (separate lines), and Azure then reports a plain Date.
BIRTH_LABEL = re.compile(r"\b(?:date\s+of\s+birth|birth\s*date|D\.?O\.?B\.?|born(?:\s+on)?)", re.I)

# Labelled values found by pattern: categories Azure doesn't have (record and member numbers),
# plus a backup for ages ("Age" and "39" in separate table cells). Only the value is redacted;
# the label stays readable. ID values must contain a digit and may wrap after a hyphen.
_ID_VALUE = r"(?=[A-Z0-9\n-]*\d)[A-Z0-9](?:[A-Z0-9]|-(?!\n)){1,}(?:-\n[A-Z0-9](?:[A-Z0-9]|-(?!\n))*)?"
PATTERNS = {
    "Age": re.compile(r"\bage\b[\s:]*(\d{1,3})\b(?!\s*(?:mg|ml|%|kg|lb))", re.I),
    "MedicalRecordNumber": re.compile(
        r"\b(?:medical\s+record\s*(?:number|no\.?|#)?|MRN)(?:\s*(?:number|no\.?|#))?[\s:#-]*"
        r"((?:MRN[-\s]?)?" + _ID_VALUE + ")", re.I),
    "InsuranceMemberId": re.compile(
        r"\b(?:member|subscriber|policy|insurance)\s*(?:ID|number|no\.?|#)(?:\s*(?:number|no\.?|#))?[\s:#-]*"
        r"(" + _ID_VALUE + ")", re.I),
}


async def _detect_pii(layout: TextLayout, categories: list[str]) -> list[dict]:
    azure_cats = [c for c in categories if c in AZURE_IDS]
    # Birth dates in tables come back as plain dates, so ask for those too and sort them below.
    request_cats = sorted(set(azure_cats) | ({"Date"} if "DateOfBirth" in azure_cats else set()))
    entities = _pattern_entities(layout, [c for c in categories if c in PATTERNS])
    chunks = list(_text_chunks(layout))
    if not chunks or not azure_cats:
        return _dedupe(entities)
    headers = await _auth_headers()
    async with httpx.AsyncClient(timeout=60) as http:
        for i in range(0, len(chunks), BATCH_SIZE):
            batch = chunks[i:i + BATCH_SIZE]
            body = {
                "kind": "PiiEntityRecognition",
                "parameters": {"modelVersion": "latest", "piiCategories": request_cats,
                               "stringIndexType": "UnicodeCodePoint"},
                "analysisInput": {"documents": [
                    {"id": str(j), "language": config.DOCUMENT_LANGUAGE, "text": text}
                    for j, (_, _, text) in enumerate(batch)]},
            }
            r = await _post_with_retry(http, f"{config.LANGUAGE_ENDPOINT}/language/:analyze-text",
                                       {"api-version": config.LANGUAGE_TEXT_API_VERSION}, body, headers)
            results = r.json()["results"]
            if results.get("errors"):
                raise RuntimeError(f"PII detection errors: {results['errors']}")
            for doc in results["documents"]:
                base, page_no, _ = batch[int(doc["id"])]
                for e in doc["entities"]:
                    category = _requested_category(e, request_cats)
                    start, length = base + e["offset"], _trimmed_length(layout.content, base + e["offset"], e["length"])
                    if category in CONTEXT_REQUIRED:
                        category = _labelled_category(layout.content, start, azure_cats)
                    elif (category == "Date" and "DateOfBirth" in azure_cats
                          and _label_directly_before(BIRTH_LABEL, layout.content, start)):
                        category = "DateOfBirth"   # (if only "All dates" is selected it stays a Date)
                    if category not in azure_cats:
                        continue
                    entities.append({"category": category, "offset": start, "length": length,
                                     "confidence": e["confidenceScore"], "page": page_no})
    return _dedupe(entities)


# Between a label and its number only connectors may appear: "Passport number: 1234",
# "DL# S530-...", "Driver's license No. D123".
_LABEL_GAP = re.compile(r"(?:[\s:#.()'’-]|\b(?:number|num|no|id|s)\b)*", re.I)


def _labelled_category(content: str, start: int, requested: list[str]) -> str | None:
    """For ID numbers that need a label: the category of the label directly before the number
    ("Passport 912803456" -> passport even if Azure said ITIN). None if there is no such label
    or it belongs to a category the user didn't select."""
    window = content[max(0, start - CONTEXT_WINDOW):start]
    best, best_end = None, -1
    for category, label in CONTEXT_REQUIRED.items():
        for m in label.finditer(window):
            if m.end() > best_end and _LABEL_GAP.fullmatch(window[m.end():]):
                best, best_end = category, m.end()
    return best if best in requested else None


def _label_directly_before(label: re.Pattern, content: str, start: int) -> bool:
    window = content[max(0, start - CONTEXT_WINDOW):start]
    return any(_LABEL_GAP.fullmatch(window[m.end():]) for m in label.finditer(window))


def _dedupe(entities: list[dict]) -> list[dict]:
    """Keep one entity per piece of text: Azure can report the same text twice (as passport and
    as license), and the age pattern can find "39" inside Azure's "Age 39". Longer spans win."""
    kept: list[dict] = []
    for e in sorted(entities, key=lambda e: (-e["length"], e["offset"])):
        start, end = e["offset"], e["offset"] + e["length"]
        if not any(k["offset"] <= start and end <= k["offset"] + k["length"] for k in kept):
            kept.append(e)
    return sorted(kept, key=lambda e: e["offset"])


def _pattern_entities(layout: TextLayout, categories: list[str]) -> list[dict]:
    page_starts = [start for _, start, _ in layout.pages]
    entities = []
    for category in categories:
        for m in PATTERNS[category].finditer(layout.content):
            start, end = m.span(1)
            page_no = layout.pages[max(bisect_right(page_starts, start) - 1, 0)][0]
            entities.append({"category": category, "offset": start, "length": end - start,
                             "confidence": 1.0, "page": page_no})
    return entities


def _trimmed_length(content: str, start: int, length: int) -> int:
    """Drop trailing sentence punctuation Azure sometimes includes ("05/02/2024.")."""
    while length > 1 and content[start + length - 1] in ".,;:":
        length -= 1
    return length


def _entity_boxes(layout: TextLayout, entities: list[dict]) -> dict[int, list[Box]]:
    """Map entity character ranges to boxes in PDF points, keyed by page. Any piece that
    partly overlaps an entity is fully covered; consecutive pieces on one line merge."""
    pieces = sorted(layout.pieces, key=lambda p: p.start)
    starts = [p.start for p in pieces]
    boxes: dict[int, list[Box]] = defaultdict(list)
    for e in entities:
        start, end = e["offset"], e["offset"] + e["length"]
        i = max(bisect_right(starts, start) - 1, 0)
        current: list | None = None   # [page, line, x0, y0, x1, y1] being merged
        while i < len(pieces) and pieces[i].start < end:
            p = pieces[i]
            i += 1
            if p.end <= start:
                continue
            x0, y0, x1, y1 = p.rect
            if current and p.line is not None and (current[0], current[1]) == (p.page, p.line):
                current[2:] = [min(current[2], x0), min(current[3], y0), max(current[4], x1), max(current[5], y1)]
                continue
            if current:
                boxes[current[0]].append(tuple(current[2:]))
            current = [p.page, p.line, x0, y0, x1, y1]
        if current:
            boxes[current[0]].append(tuple(current[2:]))
    return boxes


# ─────────────────────────────────── helpers ───────────────────────────────────
async def _auth_headers() -> dict:
    token = await credential.get_token(COGNITIVE_SCOPE)
    return {"Authorization": f"Bearer {token.token}"}


async def _post_with_retry(http, url, params, body, headers, attempts: int = 5) -> httpx.Response:
    for attempt in range(attempts):
        try:
            r = await http.post(url, params=params, json=body, headers=headers)
        except httpx.ConnectError:   # nothing was sent, so retrying is safe
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code != 429 and r.status_code < 500:
            break
        if attempt < attempts - 1:
            await asyncio.sleep(float(r.headers.get("retry-after", 2 ** attempt)))
    if r.is_error:
        raise RuntimeError(f"Language request failed ({r.status_code}): {r.text[:2000]}")
    return r


def _requested_category(e: dict, requested: list[str]) -> str:
    """Report each entity under the category the user selected. Azure may answer with a
    subtype ("OrganizationSports" for Organization, "DateOfBirth" when only Date was asked
    for) and lists broader names in "tags", "subcategory" and "category"."""
    candidates = [e.get("type"), *(t.get("name") for t in e.get("tags") or []),
                  e.get("subcategory"), e.get("category")]
    for name in candidates:
        if name in requested:
            return name
    return e.get("type") or e.get("subcategory") or e.get("category") or "Unknown"
