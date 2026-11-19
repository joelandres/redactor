"""FastAPI web app: upload, redact, compare side by side, download.

Documents are never stored: each browser gets a private in-memory workspace (app/workspace.py)
that is dropped after WORKSPACE_IDLE_MINUTES of inactivity.
"""
import asyncio
import logging
import re
from pathlib import Path
from urllib.parse import quote

import pymupdf
from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .categories import CATEGORIES, validate_categories
from .jobs import JobStore
from .redaction import redact_pdf
from .workspace import MB, LimitError, Workspace, Workspaces

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("azure").setLevel(logging.WARNING)

app = FastAPI(title="Document Redactor")
jobs = JobStore()
workspaces = Workspaces()
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
COOKIE = "redactor_workspace"
NO_STORE = {"Cache-Control": "no-store"}   # documents contain PII: never cache them


# ─────────────────────────────── workspace per browser ───────────────────────────────
@app.middleware("http")
async def attach_workspace(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)
    sent = request.cookies.get(COOKIE)
    ws = workspaces.get_or_create(sent)
    request.state.workspace = ws
    response = await call_next(request)
    if sent != ws.id:
        response.set_cookie(COOKIE, ws.id, httponly=True, samesite="strict",
                            secure=request.url.scheme == "https")
    return response


@app.exception_handler(LimitError)
async def limit_error(request: Request, exc: LimitError):
    return JSONResponse({"detail": str(exc)}, status_code=413)


def _ws(request: Request) -> Workspace:
    return request.state.workspace


def _document(request: Request, doc_id: str):
    doc = _ws(request).documents.get(doc_id)
    if doc is None:
        raise HTTPException(404, "Document not found. It may have expired; please upload it again.")
    return doc


# ─────────────────────────────── catalog & workspace ───────────────────────────────
@app.get("/api/categories")
async def categories() -> list[dict]:
    return CATEGORIES


@app.get("/api/workspace")
async def workspace(request: Request) -> dict:
    return {"documents": [d.summary() for d in _ws(request).documents.values()],
            "idle_minutes": config.WORKSPACE_IDLE_MINUTES, "max_upload_mb": config.MAX_UPLOAD_MB}


# ─────────────────────────────── documents ───────────────────────────────
@app.post("/api/documents", status_code=201)
async def upload_document(request: Request, file: UploadFile) -> dict:
    name = _clean_upload_name(file.filename or "")
    limit = config.MAX_UPLOAD_MB * MB
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, f"{name} is larger than {config.MAX_UPLOAD_MB} MB.")
    problem, pages = await asyncio.to_thread(_inspect_pdf, data)
    if problem:
        raise HTTPException(400, f"{name}: {problem}")
    doc = workspaces.add_document(_ws(request), name, data, pages)
    return {**doc.summary(), "renamed": doc.name != name}


@app.delete("/api/documents/{doc_id}")
async def delete_document(request: Request, doc_id: str) -> dict:
    doc = _document(request, doc_id)
    del _ws(request).documents[doc.id]
    return {"deleted": doc.name, "redactions_deleted": [r.name for r in doc.redactions]}


@app.delete("/api/documents/{doc_id}/redactions/{redaction_id}")
async def delete_redaction(request: Request, doc_id: str, redaction_id: str) -> dict:
    doc = _document(request, doc_id)
    redaction = next((r for r in doc.redactions if r.id == redaction_id), None)
    if redaction is None:
        raise HTTPException(404, "Redacted version not found.")
    doc.redactions.remove(redaction)
    return {"deleted": redaction.name}


def _clean_upload_name(filename: str) -> str:
    base = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if not base.lower().endswith(".pdf"):
        raise HTTPException(400, f"{base or 'File'}: only PDF files can be uploaded.")
    stem = re.sub(r"[^\w ._()-]+", "_", base[:-4]).strip(" ._")[:100] or "document"
    return f"{stem}.pdf"


def _inspect_pdf(data: bytes) -> tuple[str | None, int]:
    """(problem for the user, or None; page count)."""
    try:
        with pymupdf.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                return "password-protected PDFs can't be redacted. Remove the password and upload again.", 0
            if doc.page_count == 0:
                return "the PDF has no pages.", 0
            return None, doc.page_count
    except Exception:
        return "this isn't a valid PDF file.", 0


# ─────────────────────────────── redaction jobs ───────────────────────────────
class JobRequest(BaseModel):
    document_id: str
    categories: list[str] = []


@app.post("/api/jobs", status_code=202)
async def start_job(request: Request, req: JobRequest) -> dict:
    ws = _ws(request)
    doc = _document(request, req.document_id)
    try:
        cats = validate_categories(req.categories)
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def work() -> dict:
        result = await redact_pdf(doc.pdf, cats)
        redaction = workspaces.add_redaction(ws, doc.id, result["pdf"], result["method"], cats, result["counts"])
        if redaction is None:
            raise RuntimeError("The document was removed while it was being redacted.")
        return {"document_id": doc.id, "redaction": redaction.summary()}
    return {"job_id": jobs.start(work, owner=ws.id)}


@app.get("/api/jobs/{job_id}")
async def get_job(request: Request, job_id: str) -> dict:
    job = jobs.get(job_id, owner=_ws(request).id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


# ─────────────────────────────── viewing & download ───────────────────────────────
def _file(request: Request, doc_id: str, which: str) -> tuple[str, bytes]:
    found = _document(request, doc_id).file(which)
    if found is None:
        raise HTTPException(404, "Redacted version not found.")
    return found


def _page_info(pdf: bytes) -> dict:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return {"pages": doc.page_count,
                "sizes": [[round(p.rect.width, 1), round(p.rect.height, 1)] for p in doc]}


def _render_page(pdf: bytes, page_no: int, dpi: int) -> bytes:
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        if not 1 <= page_no <= doc.page_count:
            raise IndexError(page_no)
        return doc[page_no - 1].get_pixmap(dpi=dpi, alpha=False).tobytes("png")


@app.get("/api/documents/{doc_id}/files/{which}/info")
async def file_info(request: Request, doc_id: str, which: str) -> dict:
    return await asyncio.to_thread(_page_info, _file(request, doc_id, which)[1])


@app.get("/api/documents/{doc_id}/files/{which}/page/{page_no}")
async def file_page(request: Request, doc_id: str, which: str, page_no: int,
                    dpi: int = Query(110, ge=50, le=200)) -> Response:
    pdf = _file(request, doc_id, which)[1]
    try:
        png = await asyncio.to_thread(_render_page, pdf, page_no, dpi)
    except IndexError:
        raise HTTPException(404, "Page out of range")
    return Response(png, media_type="image/png", headers=NO_STORE)


@app.get("/api/documents/{doc_id}/files/{which}/file")
async def file_download(request: Request, doc_id: str, which: str, download: bool = False) -> Response:
    name, pdf = _file(request, doc_id, which)
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "").replace("?", "_")
    mode = "attachment" if download else "inline"
    disposition = f"{mode}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": disposition, **NO_STORE})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
