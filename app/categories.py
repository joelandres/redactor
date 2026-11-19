"""The single list of redaction categories shared by the UI and the API.

source "azure":   an Azure Language PII category ID (checked against api-version 2026-05-01).
source "pattern": detected by the app itself (see redaction.PATTERNS); Azure has no such category.
"""

CATEGORIES = [
    # Personal
    {"id": "Person",              "label": "Names",                  "group": "Personal",         "default": True},
    {"id": "Age",                 "label": "Ages",                   "group": "Personal",         "default": False},
    {"id": "DateOfBirth",         "label": "Birth dates",            "group": "Personal",         "default": True},
    # Contact & online
    {"id": "Address",             "label": "Addresses",              "group": "Contact & online", "default": True},
    {"id": "PhoneNumber",         "label": "Phone numbers",          "group": "Contact & online", "default": False},
    {"id": "Email",               "label": "Email addresses",        "group": "Contact & online", "default": False},
    {"id": "IPAddress",           "label": "IP addresses",           "group": "Contact & online", "default": False},
    {"id": "URL",                 "label": "Web addresses",          "group": "Contact & online", "default": False},
    # Financial
    {"id": "CreditCardNumber",    "label": "Credit cards",           "group": "Financial",        "default": False},
    {"id": "BankAccountNumber",   "label": "Bank accounts",          "group": "Financial",        "default": False},
    {"id": "ABARoutingNumber",    "label": "Routing numbers",        "group": "Financial",        "default": False},
    {"id": "InternationalBankingAccountNumber", "label": "IBANs",    "group": "Financial",        "default": False},
    {"id": "SWIFTCode",           "label": "SWIFT codes",            "group": "Financial",        "default": False},
    # Government IDs
    {"id": "USSocialSecurityNumber",             "label": "Social Security numbers", "group": "Government IDs", "default": False},
    {"id": "USIndividualTaxpayerIdentification", "label": "Taxpayer IDs (ITIN)",     "group": "Government IDs", "default": False},
    {"id": "USDriversLicenseNumber",             "label": "Driver's licenses",       "group": "Government IDs", "default": False},
    {"id": "USUKPassportNumber",                 "label": "Passports",               "group": "Government IDs", "default": False},
    # Medical
    {"id": "MedicalRecordNumber", "label": "Medical record numbers", "group": "Medical",          "default": False, "source": "pattern"},
    {"id": "InsuranceMemberId",   "label": "Insurance member IDs",   "group": "Medical",          "default": False, "source": "pattern"},
    {"id": "USMedicareBeneficiaryId", "label": "Medicare IDs",       "group": "Medical",          "default": False},
    # Other
    {"id": "Date",                "label": "All dates (incl. birth dates)", "group": "Other",            "default": False},
    {"id": "Organization",        "label": "Organizations",          "group": "Other",            "default": False},
]
for _c in CATEGORIES:
    _c.setdefault("source", "azure")

ALLOWED_IDS = {c["id"] for c in CATEGORIES}
AZURE_IDS = {c["id"] for c in CATEGORIES if c["source"] == "azure"}
PATTERN_IDS = ALLOWED_IDS - AZURE_IDS


def validate_categories(categories: list[str]) -> list[str]:
    unknown = set(categories) - ALLOWED_IDS
    if unknown:
        raise ValueError(f"Unsupported categories: {sorted(unknown)}. Allowed: {sorted(ALLOWED_IDS)}")
    if not categories:
        raise ValueError("Select at least one category.")
    return sorted(set(categories))
