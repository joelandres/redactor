"""The UI in a real browser (Chrome via Playwright), against the app running with an in-memory
Azure Language fake. Skipped when Playwright or Chrome isn't available."""
import socket
import threading
import time

import pytest
import uvicorn

from app import main, redaction
from app.jobs import JobStore
from app.workspace import Workspaces
from tests.fakes import FakeLanguage, encrypted_pdf, page_text, text_pdf

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def server():
    mp = pytest.MonkeyPatch()
    language = FakeLanguage()

    async def no_token():
        return {}
    mp.setattr(redaction, "_post_with_retry", language.post)
    mp.setattr(redaction, "_auth_headers", no_token)
    mp.setattr(main, "workspaces", Workspaces())
    mp.setattr(main, "jobs", JobStore())

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)
    mp.undo()


@pytest.fixture
def browser():
    try:
        with sync_api.sync_playwright() as p:
            b = p.chromium.launch(channel="chrome", headless=True)
            yield b
            b.close()
    except sync_api.Error as e:
        if "Executable doesn't exist" in str(e) or "is not found" in str(e):
            pytest.skip(f"Chrome not available: {e}")
        raise


def open_app(browser, url):
    """A fresh browser profile (own cookies = own workspace) with the app loaded."""
    page = browser.new_context(viewport={"width": 1400, "height": 1000}).new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(url)
    page.wait_for_selector(".cat-group")
    page.wait_for_function("document.getElementById('privacy-note').textContent.length > 0")
    return page, errors


def wait_idle(page):
    page.wait_for_function(
        "!document.getElementById('upload-btn').disabled && !document.getElementById('output').textContent.startsWith('⏳')",
        timeout=60_000)


def test_upload_redact_compare_download_remove(browser, server, tmp_path):
    page, errors = open_app(browser, server)
    assert page.text_content("#compare-status") == "Upload a PDF to start."
    assert page.is_disabled("#go") and page.is_disabled("#delete-doc")
    assert "never saved" in page.text_content("#privacy-note")

    # Upload: becomes the selected document.
    pdf = tmp_path / "Intake form.pdf"
    pdf.write_bytes(text_pdf())
    page.set_input_files("#upload", str(pdf))
    wait_idle(page)
    assert "Uploaded Intake form.pdf" in page.inner_text("#output")
    assert page.eval_on_selector("#doc", "s => s.selectedOptions[0].text") == "Intake form.pdf"
    assert page.text_content("#compare-status").startswith("This document has not been redacted yet")

    # Grouped checkboxes; redact names + birth dates.
    groups = page.eval_on_selector_all(".cat-group-title", "els => els.map(e => e.textContent)")
    assert groups == ["Personal", "Contact & online", "Financial", "Government IDs", "Medical", "Other"]
    page.click("#select-none")
    for cat in ("Person", "DateOfBirth"):
        page.check(f"#cats input[value='{cat}']")
    page.click("#go")
    wait_idle(page)
    output = page.inner_text("#output")
    assert "Intake form-redacted.pdf" in output and "Names: 4" in output and "Birth dates: 1" in output

    # Comparison shows the new version, with friendly details and rendered pages.
    assert page.eval_on_selector("#cmp-version", "s => s.selectedOptions[0].text") == "Intake form-redacted.pdf"
    assert page.text_content("#version-info").startswith("Names, Birth dates · ")
    page.wait_for_function("[...document.querySelectorAll('#compare img')].every(i => i.complete && i.naturalWidth > 0)")
    assert page.locator("#compare img").count() == 2

    # Download the redacted file.
    with page.expect_download() as dl:
        page.click("#download-redacted")
    assert dl.value.suggested_filename == "Intake form-redacted.pdf"
    assert "Jane" not in page_text(open(dl.value.path(), "rb").read())

    # Remove the version, then the document.
    page.once("dialog", lambda d: d.accept())
    page.click("#delete-version")
    wait_idle(page)
    assert page.locator("#cmp-version option").count() == 0

    messages = []
    page.once("dialog", lambda d: (messages.append(d.message), d.accept()))
    page.click("#delete-doc")
    wait_idle(page)
    assert messages == ["Remove Intake form.pdf? Download anything you need first."]
    assert page.text_content("#compare-status") == "Upload a PDF to start."
    assert errors == []


def test_each_browser_has_its_own_workspace(browser, server, tmp_path):
    pdf = tmp_path / "private.pdf"
    pdf.write_bytes(text_pdf())
    alice, _ = open_app(browser, server)
    alice.set_input_files("#upload", str(pdf))
    wait_idle(alice)

    bob, _ = open_app(browser, server)
    assert bob.eval_on_selector_all("#doc option", "os => os.map(o => o.text)") == ["No documents yet: upload a PDF"]
    alice.reload()
    alice.wait_for_function("document.querySelector('#doc option')?.text === 'private.pdf'")


def test_errors_are_shown(browser, server, tmp_path):
    page, errors = open_app(browser, server)
    bad = tmp_path / "locked.pdf"
    bad.write_bytes(encrypted_pdf())
    page.set_input_files("#upload", str(bad))
    wait_idle(page)
    assert "password-protected" in page.inner_text("#output")

    good = tmp_path / "doc.pdf"
    good.write_bytes(text_pdf())
    page.set_input_files("#upload", str(good))
    wait_idle(page)
    page.click("#select-none")
    page.click("#go")
    page.wait_for_function("document.getElementById('output').textContent.includes('Select at least one')")
    assert errors == []
