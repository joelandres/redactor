"""Shared fixtures. Offline by default: Azure Language and Document Intelligence are
replaced with in-memory fakes (tests/fakes.py).

    pytest                   offline tests (unit, PDF, API, browser if Chrome is available)
    RUN_LIVE=1 pytest -m live   only the tests that call the real Azure services
"""
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Real values from .env when present (needed by live tests); harmless placeholders otherwise.
load_dotenv(ROOT / ".env")
for key, value in {"LANGUAGE_ENDPOINT": "https://fake-language.cognitiveservices.azure.com",
                   "DOCINTEL_ENDPOINT": "https://fake-docintel.cognitiveservices.azure.com"}.items():
    os.environ.setdefault(key, value)

from app import redaction  # noqa: E402  (needs the environment above)
from tests.fakes import FakeLanguage  # noqa: E402


def pytest_collection_modifyitems(config, items):
    if os.getenv("RUN_LIVE"):
        return
    skip = pytest.mark.skip(reason="calls real Azure services; set RUN_LIVE=1 to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def language(monkeypatch) -> FakeLanguage:
    """In-memory Azure Language PII detection."""
    fake = FakeLanguage()

    async def no_token():
        return {}
    monkeypatch.setattr(redaction, "_post_with_retry", fake.post)
    monkeypatch.setattr(redaction, "_auth_headers", no_token)
    return fake
