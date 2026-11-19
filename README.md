# Document Redactor

A web app that redacts PII from PDFs: upload a PDF, pick the categories to redact, compare
the original and redacted versions side by side, and download the result.

**Nothing is stored.** Each browser gets a private workspace in the server's memory (tied to an
HttpOnly cookie, so browsers can't see each other's files). Files stay there until the user
removes them or after 30 minutes of inactivity (`WORKSPACE_IDLE_MINUTES`). Documents are sent
to Azure Language (and, for scans, Document Intelligence) for processing only.

- **Text PDFs**: Azure Language PII detection on the PDF's own text, then true redaction with
  PyMuPDF: the PII characters are deleted and covered with black boxes, and everything else
  (fonts, line breaks, tables, images) is left exactly as it was. Form fields are flattened
  first so their values are checked too, and metadata, links and attachments are stripped.
  Each page is verified afterwards; a page that fails verification is replaced by an image
  of itself with the boxes painted (same look, but not selectable).
- **Scanned PDFs**: Document Intelligence OCR, then Language text PII detection, then the
  pages are rendered to images with the PII areas painted black (no hidden text survives).

Each redaction of a document is named `<name>-redacted.pdf`, then `<name>-redacted-2.pdf` and
so on (numbers are never reused). Memory limits: `MAX_UPLOAD_MB` per file, `MAX_WORKSPACE_MB`
per browser, `MAX_TOTAL_MB` for the whole server (see `app/config.py`).

Detection is Azure Language PII detection plus a few app-side rules (`app/redaction.py`):
- Driver's license, passport and ITIN numbers count only when a matching label sits directly
  before them ("Passport 912803456"). Azure's detectors for these also flag ordinary numbers.
- A date directly after "Date of birth" / "DOB" / "born" is a birth date, even in form tables
  where Azure sees only a plain date.
- Medical record numbers and insurance member/policy IDs (no Azure category) and ages are
  found by label patterns. Only the value is redacted; the label stays readable.

## Layout

```
app/
  config.py       settings from .env
  categories.py   PII categories shared by the UI and API (allowlist)
  auth.py         Azure credential (Entra ID via DefaultAzureCredential, no keys)
  workspace.py    private in-memory workspaces per browser (idle expiry, memory limits)
  redaction.py    redact_pdf(): text route + scanned/OCR route
  jobs.py         in-memory background jobs
  main.py         FastAPI: upload, jobs, comparison rendering, download, remove
  try_redact.py   command-line redaction of a local file
static/           UI (index.html, app.js, style.css)
infra/setup.sh    provisions Azure resources and roles
scripts/make_samples.py   generates fake test PDFs (text + scanned)
```

## Setup

```bash
cd ~/apps/redactor
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

az login
./infra/setup.sh
cp .env.example .env           # paste the values printed by setup.sh

python scripts/make_samples.py # creates samples/sample-text.pdf and samples/sample-scanned.pdf
```

## Test without the UI

```bash
python -m app.try_redact samples/sample-text.pdf auto Person Address DateOfBirth
python -m app.try_redact samples/sample-scanned.pdf auto Person Address DateOfBirth
# writes samples/sample-text-redacted.pdf etc. next to the input
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest                         # offline: a few seconds, no Azure calls, no cost
RUN_LIVE=1 pytest -m live      # real Azure services from .env (small cost)
```

Offline tests replace Azure Language and Document Intelligence with in-memory fakes (`tests/fakes.py`) that reproduce the real service's quirks. They cover the detection
rules, both redaction routes on generated PDFs (layout preserved, PII really removed, form
fields, rotated pages, kerning, image fallback, the PyMuPDF 2.x guard), every API endpoint
(including that browsers can't see each other's workspaces), and the UI in Chrome (skipped if
Chrome isn't installed). Live tests check every category against the real service and run a
full redaction through both routes.

## Run the web app

```bash
uvicorn app.main:app --reload
# open http://localhost:8000
```

## Notes

- **Add authentication** (for example App Service Easy Auth with Entra ID) before you deploy
  it anywhere: otherwise anyone who can reach it can use your Azure Language and Document
  Intelligence quota, and their documents pass through your server.
- Workspaces are in memory, so run a single instance (or add sticky sessions). A restart
  clears all workspaces; users simply upload again.
- PyMuPDF is pinned below 2.0: 2.x writes the wrong gap width when it deletes text, which
  shifts the rest of the line. The per-page verification would catch it (image fallback).
- Check `LANGUAGE_TEXT_API_VERSION` against the current REST reference. Older versions
  (2024-11-01) silently ignore the DateOfBirth category.
- The OCR route doesn't redact photos, signatures or handwriting that OCR can't read.
  Review high-stakes documents before releasing them.

## License

MIT. See [LICENSE](LICENSE).
