"""The real-data layer (Rule 8): cited, cached, checksummed.

Rule 8 says evaluation data must come from real, cited public procurement
datasets. The parts of that worth testing are the parts that could rot silently:
a corpus that drifts from its recorded checksum, a manifest missing its
provenance, or a selection helper that quietly reclassifies records and so
changes which fault class each one carries.

The dataset itself is real and cached (`data/raw/`), so these read it rather
than a fixture — checking the loader against invented rows would not check the
thing that matters.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from data.corpus import AwardRecord, DatasetError, load_records
from data.usaspending import MANIFEST_PATH, RAW_PATH, canonical_bytes, sha256_of


@pytest.fixture(scope="module")
def manifest() -> dict[str, object]:
    return dict(json.loads(MANIFEST_PATH.read_text(encoding="utf-8")))


# --- Rule 8: cited ----------------------------------------------------------------
def test_manifest_records_source_licence_and_request(manifest: dict[str, object]) -> None:
    """ "Cited" means someone else can re-issue the request and see what we saw."""
    for field in ("source_url", "publisher", "license", "request_body", "retrieved_at", "sha256"):
        assert manifest.get(field), f"dataset_manifest.json is missing {field!r} (Rule 8)"
    assert "usaspending.gov" in str(manifest["source_url"])


def test_manifest_states_the_selection_rather_than_hiding_it(manifest: dict[str, object]) -> None:
    """An amount band is a legitimate selection over real records — but only if
    it is written down, because it changes which records are in the corpus."""
    body = manifest["request_body"]
    assert isinstance(body, dict)
    assert body["filters"]["award_amounts"]
    assert manifest.get("selection_note")


# --- Rule 8: checksummed -----------------------------------------------------------
def test_cached_corpus_matches_its_recorded_checksum(manifest: dict[str, object]) -> None:
    """A benchmark whose corpus changes underneath its own results is not a
    benchmark. This is the check that makes drift loud."""
    raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    assert sha256_of(canonical_bytes(raw)) == manifest["sha256"]


def test_loader_refuses_a_corpus_that_does_not_match(tmp_path: Path) -> None:
    """Drift must raise rather than silently evaluate against different data."""
    import data.usaspending as us

    original = us.RAW_PATH
    tampered = tmp_path / "usaspending_awards.json"
    rows = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    rows[0]["Award Amount"] = 999_999.99
    tampered.write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
    try:
        us.RAW_PATH = tampered
        import data.corpus as corpus

        corpus.RAW_PATH = tampered  # type: ignore[attr-defined]
        with pytest.raises(DatasetError, match="checksum"):
            corpus.load_records(verify=True)
    finally:
        us.RAW_PATH = original
        import data.corpus as corpus

        corpus.RAW_PATH = original  # type: ignore[attr-defined]


def test_checksum_is_over_content_not_key_order() -> None:
    rows = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    reordered = [dict(reversed(list(r.items()))) for r in rows]
    assert sha256_of(canonical_bytes(rows)) == sha256_of(canonical_bytes(reordered))


# --- the records themselves ---------------------------------------------------------
def test_records_load_with_real_suppliers_and_amounts() -> None:
    records = load_records()
    assert len(records) >= 30
    assert all(r.supplier for r in records)
    assert all(r.amount > 0 for r in records)
    assert len({r.supplier for r in records}) > 10, "a corpus of one vendor tests nothing"


def test_amounts_have_real_variance() -> None:
    """Sorting by amount against the filter's ceiling once returned 200 records
    all sitting at exactly $60,000 — a corpus with no variance at all."""
    amounts = {r.amount for r in load_records()}
    assert len(amounts) > 50


def test_item_codes_are_unique_and_traceable_to_the_source() -> None:
    records = load_records()
    codes = [r.item_code for r in records]
    assert len(codes) == len(set(codes))
    assert all(c.startswith("USA-") for c in codes)


# --- the natural fault pools --------------------------------------------------------
def test_the_corpus_contains_genuinely_empty_descriptions() -> None:
    """The *missing data* class selects these; it does not manufacture them."""
    assert any(not r.has_description for r in load_records())


def test_the_corpus_contains_genuinely_uninformative_descriptions() -> None:
    """`IGF::OT::IGF`, `BLOCK`, `TERMINATOR` — real federal award descriptions
    that say nothing a buyer could act on. The *ambiguity* class selects these."""
    code_only = [r for r in load_records() if r.is_code_only]
    assert code_only, "no code-only descriptions found; the ambiguity pool is empty"


def test_a_real_description_is_not_treated_as_ambiguous() -> None:
    record = AwardRecord(
        internal_id=1,
        award_id="X",
        supplier="ACME",
        amount=Decimal("100.00"),
        description="PURCHASE AND DELIVERY OF ONE HIGH SPEED CAMERA SYSTEM",
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    assert record.has_description
    assert not record.is_code_only


def test_an_igf_routing_code_is_treated_as_ambiguous() -> None:
    record = AwardRecord(
        internal_id=2,
        award_id="Y",
        supplier="ACME",
        amount=Decimal("100.00"),
        description="IGF::OT::IGF",
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    assert record.has_description, "the field is populated — it just says nothing"
    assert record.is_code_only


def test_an_empty_description_is_missing_rather_than_ambiguous() -> None:
    """The two classes are distinct: one has no data, the other has data that
    carries no meaning."""
    record = AwardRecord(
        internal_id=3,
        award_id="Z",
        supplier="ACME",
        amount=Decimal("100.00"),
        description="   ",
        start_date="2024-01-01",
        end_date="2024-12-31",
    )
    assert not record.has_description
    assert not record.is_code_only
