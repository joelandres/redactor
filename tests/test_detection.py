"""PII detection rules (app/redaction.py) on top of a fake Azure Language service."""
import asyncio

import pytest

from app import redaction as R
from app.categories import ALLOWED_IDS, AZURE_IDS, CATEGORIES, validate_categories


def detect(text: str, categories) -> dict[str, list[str]]:
    """Run the app's detection on plain text; returns {category: [redacted text, ...]}."""
    layout = R.TextLayout(content=text, pages=[(1, 0, len(text))])
    found: dict[str, list[str]] = {}
    for e in asyncio.run(R._detect_pii(layout, sorted(categories))):
        found.setdefault(e["category"], []).append(text[e["offset"]:e["offset"] + e["length"]])
    return found


# ─────────────────────────────── categories ───────────────────────────────
def test_category_ids_unique_and_grouped():
    ids = [c["id"] for c in CATEGORIES]
    assert len(ids) == len(set(ids))
    assert all(c["group"] and c["label"] for c in CATEGORIES)
    assert {"MedicalRecordNumber", "InsuranceMemberId"} == ALLOWED_IDS - AZURE_IDS


def test_validate_categories():
    assert validate_categories(["Person", "Person", "Address"]) == ["Address", "Person"]
    with pytest.raises(ValueError, match="at least one"):
        validate_categories([])
    with pytest.raises(ValueError, match="Unsupported"):
        validate_categories(["Password"])


# ─────────────────────────────── Azure results ───────────────────────────────
def test_counts_subtypes_under_the_selected_category(language):
    assert detect("Insurance & records.", ["Organization"]) == {"Organization": ["Insurance"]}


def test_trailing_full_stop_is_not_redacted(language):
    assert detect("Portal https://portal.example.org/jdoe.", ["URL"]) == {"URL": ["https://portal.example.org/jdoe"]}


def test_pattern_only_selection_does_not_call_azure(language):
    assert detect("MRN-0048213", ["MedicalRecordNumber"]) == {"MedicalRecordNumber": ["0048213"]}
    assert language.calls == []


# ─────────────────────────────── ID numbers need a label ───────────────────────────────
ID_TEXT = "Age 42. Take 500 mg. Driver's license D1234567. Passport 912803456."


def test_unlabelled_numbers_are_not_ids(language):
    found = detect(ID_TEXT, ["USDriversLicenseNumber"])
    assert found == {"USDriversLicenseNumber": ["D1234567"]}       # not "42" or "500"


def test_label_decides_the_id_type(language):
    # Azure reports the passport number as an ITIN; the "Passport" label wins.
    found = detect(ID_TEXT, ["USUKPassportNumber", "USIndividualTaxpayerIdentification"])
    assert found == {"USUKPassportNumber": ["912803456"]}


def test_id_of_an_unselected_type_stays_visible(language):
    assert detect(ID_TEXT, ["USIndividualTaxpayerIdentification"]) == {}


@pytest.mark.parametrize("text, expected", [
    ("Driver's license D1234567", "USDriversLicenseNumber"),
    ("DL# D1234567", "USDriversLicenseNumber"),
    ("Driver License No: D1234567", "USDriversLicenseNumber"),
    ("Passport number: D1234567", "USUKPassportNumber"),
    ("ITIN D1234567", "USIndividualTaxpayerIdentification"),
    ("Age 39. D1234567", None),            # a label from another sentence doesn't count
    ("Order D1234567", None),
])
def test_label_must_sit_directly_before_the_number(text, expected):
    start = text.index("D1234567")
    assert R._labelled_category(text, start, list(R.CONTEXT_REQUIRED)) == expected


# ─────────────────────────────── birth dates ───────────────────────────────
TABLE_TEXT = "Date of birth\nMarch 14, 1985\nVisit date\n05/02/2024\n"


def test_birth_date_in_a_table_is_a_birth_date(language):
    # Azure reports both as plain dates; only the one after "Date of birth" is a birth date.
    assert detect(TABLE_TEXT, ["DateOfBirth"]) == {"DateOfBirth": ["March 14, 1985"]}


def test_other_dates_only_when_selected(language):
    assert detect(TABLE_TEXT, ["DateOfBirth", "Date"]) == {"DateOfBirth": ["March 14, 1985"], "Date": ["05/02/2024"]}
    assert detect(TABLE_TEXT, ["Date"]) == {"Date": ["March 14, 1985", "05/02/2024"]}


@pytest.mark.parametrize("label", ["Date of birth:", "DOB", "D.O.B.", "Birth date", "born on"])
def test_birth_labels(label):
    text = f"{label} March 14, 1985"
    assert R._label_directly_before(R.BIRTH_LABEL, text, text.index("March"))


# ─────────────────────────────── app patterns ───────────────────────────────
@pytest.mark.parametrize("text, expected", [
    ("Medical record number: MRN-0048213.", ["MRN-0048213"]),
    ("MRN-0048213 on file", ["0048213"]),
    ("MRN: 0048213", ["0048213"]),
])
def test_medical_record_numbers(text, expected):
    assert [m.group(1) for m in R.PATTERNS["MedicalRecordNumber"].finditer(text)] == expected


@pytest.mark.parametrize("text, expected", [
    ("Insurance Member ID ZXH902334871.", ["ZXH902334871"]),
    ("Policy #: PL-\n7781-22. Medicare", ["PL-\n7781-22"]),       # wrapped after a hyphen
    ("Subscriber No. 44-\n9182", ["44-\n9182"]),
    ("Insurance member ID on file.", []),                          # words, not IDs
    ("Member ID number: pending.", []),
    ("Member ID: AB-\nCD", []),                                    # no digit
])
def test_insurance_ids(text, expected):
    assert [m.group(1) for m in R.PATTERNS["InsuranceMemberId"].finditer(text)] == expected


@pytest.mark.parametrize("text, expected", [
    ("Age\n39\nDate", ["39"]), ("age 42, male", ["42"]), ("Age 104.", ["104"]),
    ("Page 3 of 4", []), ("Average 120", []), ("age: 7 mg", []),
])
def test_age_pattern(text, expected):
    assert [m.group(1) for m in R.PATTERNS["Age"].finditer(text)] == expected


def test_overlapping_hits_count_once():
    entities = [{"offset": 0, "length": 6, "category": "Age"}, {"offset": 4, "length": 2, "category": "Age"},
                {"offset": 4, "length": 2, "category": "Date"}]
    assert R._dedupe(entities) == [{"offset": 0, "length": 6, "category": "Age"}]


# ─────────────────────────────── boxes ───────────────────────────────
def test_entity_boxes_merge_per_line_and_split_across_lines():
    P = R.Piece
    layout = R.TextLayout(content="ab\ncd", pages=[(1, 0, 5)], pieces=[
        P(0, 1, 1, (0, 0, 5, 10), 1), P(1, 2, 1, (5, 0, 10, 10), 1),
        P(3, 4, 1, (0, 20, 5, 30), 2), P(4, 5, 1, (5, 20, 10, 30), 2)])
    boxes = R._entity_boxes(layout, [{"offset": 0, "length": 5, "page": 1}])
    assert boxes == {1: [(0, 0, 10, 10), (0, 20, 10, 30)]}
