"""Checks against the real Azure services in .env. Opt-in, small cost:

    RUN_LIVE=1 pytest -m live

Nothing is stored anywhere: documents are sent to Azure Language / Document Intelligence for
processing only.
"""
import asyncio

import pytest

from app import redaction as R
from app.categories import ALLOWED_IDS
from tests.fakes import page_text, scanned_pdf, text_pdf

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def fresh_credential(monkeypatch):
    """The app's credential lives for the whole server process (one event loop). Each test
    here runs its own event loop, so give each test its own credential."""
    from azure.identity.aio import DefaultAzureCredential
    monkeypatch.setattr(R, "credential", DefaultAzureCredential())


RICH_TEXT = """PATIENT INTAKE FORM
Patient name: Jane Q. Doe    Age: 39    Date of birth: March 14, 1985
Home address: 1234 Maple Avenue, Springfield, IL 62704
Phone: (217) 555-0142    Email: jane.doe@example.com    Portal: https://portal.example.org/jdoe
Last login from IP 192.168.10.24.
SSN 123-45-6789. ITIN 912-70-1234. Driver's license D1234567. Passport 912803456.
Card 4111 1111 1111 1111. Account 000123456789, routing 021000021. IBAN GB82WEST12345698765432, SWIFT code DEUTDEFF.
Medical record number: MRN-0048213. Insurance Member ID ZXH902334871. Medicare 1EG4-TE5-MK73.
Notes: Seen on 05/02/2024 by Dr. Alan Smith. Take 500 mg twice daily. Room 214.
"""
EXPECTED = {
    "Person": "Jane Q. Doe", "Age": "39", "DateOfBirth": "March 14, 1985", "Date": "05/02/2024",
    "Address": "1234 Maple Avenue, Springfield, IL 62704", "PhoneNumber": "(217) 555-0142",
    "Email": "jane.doe@example.com", "URL": "https://portal.example.org/jdoe", "IPAddress": "192.168.10.24",
    "USSocialSecurityNumber": "123-45-6789", "USIndividualTaxpayerIdentification": "912-70-1234",
    "USDriversLicenseNumber": "D1234567", "USUKPassportNumber": "912803456",
    "CreditCardNumber": "4111 1111 1111 1111", "BankAccountNumber": "000123456789",
    "ABARoutingNumber": "021000021", "InternationalBankingAccountNumber": "GB82WEST12345698765432",
    "SWIFTCode": "DEUTDEFF", "MedicalRecordNumber": "MRN-0048213", "InsuranceMemberId": "ZXH902334871",
    "USMedicareBeneficiaryId": "1EG4-TE5-MK73",
}


def detect(text, categories):
    layout = R.TextLayout(content=text, pages=[(1, 0, len(text))])
    found = {}
    for e in asyncio.run(R._detect_pii(layout, sorted(categories))):
        found.setdefault(e["category"], set()).add(text[e["offset"]:e["offset"] + e["length"]])
    return found


def test_every_category_is_detected():
    found = detect(RICH_TEXT, ALLOWED_IDS)
    missing = {c: v for c, v in EXPECTED.items() if v not in found.get(c, set())}
    assert missing == {}, f"not detected: {missing}"


def test_look_alike_numbers_stay_visible():
    redacted = set().union(*detect(RICH_TEXT, ALLOWED_IDS).values())
    assert not {"500", "214"} & redacted


def test_api_version_honours_date_of_birth():
    # 2024-11-01 accepted DateOfBirth but silently ignored it.
    assert detect("Date of birth: March 14, 1985", ["DateOfBirth"]) == {"DateOfBirth": {"March 14, 1985"}}, \
        f"LANGUAGE_TEXT_API_VERSION={config.LANGUAGE_TEXT_API_VERSION} doesn't detect birth dates"


@pytest.mark.parametrize("route", ["text", "ocr"])
def test_full_redaction(route):
    twin = text_pdf()
    pdf = twin if route == "text" else scanned_pdf(twin)
    result = asyncio.run(R.redact_pdf(pdf, ["Person", "Address", "DateOfBirth"]))
    assert result["method"] == route
    assert result["counts"] == {"Person": 4, "Address": 1, "DateOfBirth": 1}
    assert "Jane" not in page_text(result["pdf"])
