"""The HTTP API (app/main.py) end to end, against in-memory Azure fakes."""
import time

import pytest
from fastapi.testclient import TestClient

from app import config, main, redaction
from app.jobs import JobStore
from app.workspace import Workspaces
from tests.fakes import FakeDocIntel, encrypted_pdf, page_text, scanned_pdf, text_pdf


@pytest.fixture(autouse=True)
def fresh_server_state(monkeypatch):
    monkeypatch.setattr(main, "workspaces", Workspaces())
    monkeypatch.setattr(main, "jobs", JobStore())


@pytest.fixture
def client(language):
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def other_client(language):
    """A second browser (its own cookie, so its own workspace)."""
    with TestClient(main.app) as c:
        yield c


def upload(client, name: str, data: bytes | None = None):
    return client.post("/api/documents", files={"file": (name, data if data is not None else text_pdf(), "application/pdf")})


def run_job(client, doc_id: str, categories: list[str]) -> dict:
    r = client.post("/api/jobs", json={"document_id": doc_id, "categories": categories})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]
    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise TimeoutError("job did not finish")


def files_url(doc_id, which="original", path="file"):
    return f"/api/documents/{doc_id}/files/{which}/{path}"


# ─────────────────────────────── workspace & privacy ───────────────────────────────
def test_new_browser_gets_an_empty_private_workspace(client):
    r = client.get("/api/workspace")
    assert r.json()["documents"] == [] and r.json()["idle_minutes"] == config.WORKSPACE_IDLE_MINUTES
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_browsers_cannot_see_each_others_documents(client, other_client):
    doc = upload(client, "mine.pdf").json()
    job_id = client.post("/api/jobs", json={"document_id": doc["id"], "categories": ["Person"]}).json()["job_id"]

    assert other_client.get("/api/workspace").json()["documents"] == []
    assert other_client.get(files_url(doc["id"])).status_code == 404
    assert other_client.get(f"/api/jobs/{job_id}").status_code == 404
    assert other_client.delete(f"/api/documents/{doc['id']}").status_code == 404
    assert other_client.post("/api/jobs", json={"document_id": doc["id"], "categories": ["Person"]}).status_code == 404
    assert [d["name"] for d in client.get("/api/workspace").json()["documents"]] == ["mine.pdf"]


def test_files_are_never_cached(client):
    doc = upload(client, "a.pdf").json()
    assert client.get(files_url(doc["id"])).headers["cache-control"] == "no-store"
    assert client.get(files_url(doc["id"], path="page/1")).headers["cache-control"] == "no-store"


def test_idle_workspaces_are_dropped(monkeypatch):
    workspaces = Workspaces()
    ws = workspaces.get_or_create(None)
    workspaces.add_document(ws, "a.pdf", b"%PDF", 1)
    ws.last_used -= config.WORKSPACE_IDLE_MINUTES * 60 + 1
    assert workspaces.get_or_create(ws.id).id != ws.id      # expired: a fresh, empty workspace
    assert workspaces.total_size() == 0


def test_workspace_memory_limit(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_WORKSPACE_MB", 0)
    r = upload(client, "a.pdf")
    assert r.status_code == 413 and "workspace is full" in r.json()["detail"]


# ─────────────────────────────── upload ───────────────────────────────
def test_upload_and_rename_on_duplicate(client):
    first = upload(client, "report.pdf").json()
    second = upload(client, "report.pdf").json()
    assert (first["name"], first["renamed"], first["pages"]) == ("report.pdf", False, 1)
    assert (second["name"], second["renamed"]) == ("report-2.pdf", True)
    assert [d["name"] for d in client.get("/api/workspace").json()["documents"]] == ["report.pdf", "report-2.pdf"]


@pytest.mark.parametrize("filename, cleaned", [
    ("C:\\Users\\x\\Résumé – José (final).pdf", "Résumé _ José (final).pdf"),
    ("../../etc/passwd.pdf", "passwd.pdf"),
    ("Report.PDF", "Report.pdf"),
    (".pdf", "document.pdf"),
])
def test_upload_names_are_cleaned(client, filename, cleaned):
    assert upload(client, filename).json()["name"] == cleaned


@pytest.mark.parametrize("name, data, message", [
    ("notes.txt", b"hello", "only PDF files"),
    ("fake.pdf", b"not a pdf", "isn't a valid PDF"),
    ("locked.pdf", None, "password-protected"),
])
def test_upload_rejects_bad_files(client, name, data, message):
    r = upload(client, name, data if data is not None else encrypted_pdf())
    assert r.status_code == 400 and message in r.json()["detail"]
    assert client.get("/api/workspace").json()["documents"] == []


def test_upload_size_limit(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 0)
    r = upload(client, "big.pdf")
    assert r.status_code == 413 and "larger than" in r.json()["detail"]


# ─────────────────────────────── redaction jobs ───────────────────────────────
def test_job_validation(client):
    doc = upload(client, "doc.pdf").json()
    assert client.post("/api/jobs", json={"document_id": doc["id"], "categories": []}).status_code == 400
    assert client.post("/api/jobs", json={"document_id": doc["id"], "categories": ["Password"]}).status_code == 400
    assert client.post("/api/jobs", json={"document_id": "nope", "categories": ["Person"]}).status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_text_redaction_and_versions(client):
    doc = upload(client, "intake.pdf").json()
    job = run_job(client, doc["id"], ["Person", "Address", "DateOfBirth"])
    assert job["status"] == "succeeded", job
    first = job["redaction"]
    assert (first["name"], first["method"]) == ("intake-redacted.pdf", "text")
    assert first["counts"] == {"Person": 4, "Address": 1, "DateOfBirth": 1}

    second = run_job(client, doc["id"], ["Person"])["redaction"]
    assert second["name"] == "intake-redacted-2.pdf"

    versions = client.get("/api/workspace").json()["documents"][0]["redactions"]
    assert [(v["name"], v["categories"]) for v in versions] == [
        ("intake-redacted-2.pdf", ["Person"]), ("intake-redacted.pdf", ["Address", "DateOfBirth", "Person"])]
    pdf = client.get(files_url(doc["id"], first["id"])).content
    assert "Jane" not in page_text(pdf) and "Contoso" in page_text(pdf)


def test_scanned_redaction(client, monkeypatch):
    twin = text_pdf()
    monkeypatch.setattr(redaction, "DocumentIntelligenceClient", FakeDocIntel(twin))
    doc = upload(client, "scan.pdf", scanned_pdf(twin)).json()
    result = run_job(client, doc["id"], ["Person"])["redaction"]
    assert (result["method"], result["name"], result["counts"]) == ("ocr", "scan-redacted.pdf", {"Person": 4})


def test_failed_job_reports_the_error(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("Language request failed (403): denied")
    monkeypatch.setattr(redaction, "_post_with_retry", boom)
    doc = upload(client, "doc.pdf").json()
    job = run_job(client, doc["id"], ["Person"])
    assert job["status"] == "failed" and "403" in job["error"]


def test_document_removed_during_redaction():
    workspaces = Workspaces()
    ws = workspaces.get_or_create(None)
    doc = workspaces.add_document(ws, "a.pdf", b"%PDF", 1)
    del ws.documents[doc.id]
    assert workspaces.add_redaction(ws, doc.id, b"%PDF", "text", ["Person"], {}) is None


# ─────────────────────────────── viewing & download ───────────────────────────────
def test_page_rendering_and_download(client):
    doc = upload(client, "José.pdf", text_pdf(pages=2)).json()
    assert client.get(files_url(doc["id"], path="info")).json()["pages"] == 2
    png = client.get(files_url(doc["id"], path="page/2"))
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert client.get(files_url(doc["id"], path="page/3")).status_code == 404

    inline = client.get(files_url(doc["id"]))
    attach = client.get(files_url(doc["id"]), params={"download": "true"})
    assert inline.headers["content-disposition"].startswith("inline;")
    assert attach.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''Jos%C3%A9.pdf" in attach.headers["content-disposition"]
    assert attach.content[:5] == b"%PDF-"


def test_unknown_files(client):
    doc = upload(client, "a.pdf").json()
    assert client.get(files_url("nope")).status_code == 404
    assert client.get(files_url(doc["id"], "nope")).status_code == 404


# ─────────────────────────────── remove ───────────────────────────────
def test_remove_version(client):
    doc = upload(client, "a.pdf").json()
    first = run_job(client, doc["id"], ["Person"])["redaction"]
    second = run_job(client, doc["id"], ["Person"])["redaction"]
    r = client.delete(f"/api/documents/{doc['id']}/redactions/{second['id']}")
    assert r.json() == {"deleted": "a-redacted-2.pdf"}
    versions = client.get("/api/workspace").json()["documents"][0]["redactions"]
    assert [v["id"] for v in versions] == [first["id"]]
    assert client.delete(f"/api/documents/{doc['id']}/redactions/{second['id']}").status_code == 404


def test_remove_document_and_its_versions(client):
    a, b = upload(client, "a.pdf").json(), upload(client, "b.pdf").json()
    run_job(client, a["id"], ["Person"])
    assert client.delete(f"/api/documents/{a['id']}").json() == {"deleted": "a.pdf", "redactions_deleted": ["a-redacted.pdf"]}
    assert [d["id"] for d in client.get("/api/workspace").json()["documents"]] == [b["id"]]
    assert client.delete(f"/api/documents/{a['id']}").status_code == 404


def test_version_numbers_are_never_reused(client):
    doc = upload(client, "a.pdf").json()
    versions = [run_job(client, doc["id"], ["Person"])["redaction"] for _ in range(3)]
    client.delete(f"/api/documents/{doc['id']}/redactions/{versions[1]['id']}")
    assert run_job(client, doc["id"], ["Person"])["redaction"]["name"] == "a-redacted-4.pdf"
