"""Application settings, loaded from environment variables (or a local .env file)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing environment variable {name}. See .env.example.")
    return value.rstrip("/")


LANGUAGE_ENDPOINT = _required("LANGUAGE_ENDPOINT")       # https://<language>.cognitiveservices.azure.com
DOCINTEL_ENDPOINT = _required("DOCINTEL_ENDPOINT")       # https://<docintel>.cognitiveservices.azure.com

# Tested 2026-11-18: 2026-05-01 detects the same as 2025-11-01 for the app's categories and labels
# expiration dates better. The older 2024-11-01 silently ignores DateOfBirth (birth dates stay visible).
LANGUAGE_TEXT_API_VERSION = os.getenv("LANGUAGE_TEXT_API_VERSION", "2026-05-01")
DOCUMENT_LANGUAGE = os.getenv("DOCUMENT_LANGUAGE", "en")

OCR_OUTPUT_DPI = int(os.getenv("OCR_OUTPUT_DPI", "200"))

# Documents live only in server memory, in a private workspace per browser session.
WORKSPACE_IDLE_MINUTES = int(os.getenv("WORKSPACE_IDLE_MINUTES", "30"))   # then everything is dropped
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))                      # per file
MAX_WORKSPACE_MB = int(os.getenv("MAX_WORKSPACE_MB", "200"))               # per browser session
MAX_TOTAL_MB = int(os.getenv("MAX_TOTAL_MB", "1024"))                      # all sessions together
