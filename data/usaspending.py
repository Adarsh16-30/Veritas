"""Fetch real federal procurement awards from USAspending.gov (Rule 8).

    uv run python -m data.usaspending --limit 200

Rule 8 says evaluation data must come from real, cited public procurement
datasets — no invented vendor names, amounts, or line items. This module is the
only place that reaches the network, and it does three things that make the
result defensible rather than merely real-looking:

1. **Cites.** The manifest records the exact endpoint, the exact request body,
   the API's documentation URL and the data's public-domain status. Anyone can
   re-issue the request and see what we saw.
2. **Caches.** The raw response is written to ``data/raw/`` and every downstream
   consumer reads *that file*, never the network. A benchmark that silently
   re-fetches is a benchmark whose corpus changes underneath its own results.
3. **Checksums.** ``dataset_manifest.json`` carries a sha256 over the canonical
   bytes of the cached file, so a corpus that drifts is detectable rather than
   assumed stable.

The selection is bounded to awards between $2,000 and $60,000 — a documented,
reproducible filter that keeps records in the range a purchase order plausibly
covers. That is a stated selection criterion over real records, not a
fabrication: no field of any retrieved award is altered.

Source: https://api.usaspending.gov/ — U.S. federal contract award data,
published by the Department of the Treasury as a public work of the U.S.
Government (17 U.S.C. §105).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
RAW_PATH = RAW_DIR / "usaspending_awards.json"
MANIFEST_PATH = ROOT / "data" / "dataset_manifest.json"

ENDPOINT = "https://api.usaspending.gov/api/v2/search/spending_by_award/"
DOCS_URL = "https://api.usaspending.gov/docs/endpoints"

#: Field names as USAspending returns them (spaces and all).
FIELDS: tuple[str, ...] = (
    "Award ID",
    "Recipient Name",
    "Award Amount",
    "Description",
    "Start Date",
    "End Date",
)

#: The selection, stated once and recorded in the manifest. Contract award types
#: A-D are definitive contracts and delivery orders — actual purchases, not
#: grants or loans. The amount band keeps records in purchase-order territory.
LOWER_BOUND = 2_000
UPPER_BOUND = 60_000
TIME_PERIOD = {"start_date": "2022-01-01", "end_date": "2024-12-31"}
AWARD_TYPE_CODES = ["A", "B", "C", "D"]


def request_body(page: int, page_size: int) -> dict[str, Any]:
    """The exact request issued, reproduced verbatim in the manifest."""
    return {
        "filters": {
            "award_type_codes": AWARD_TYPE_CODES,
            "time_period": [TIME_PERIOD],
            "award_amounts": [{"lower_bound": LOWER_BOUND, "upper_bound": UPPER_BOUND}],
        },
        "fields": list(FIELDS),
        "page": page,
        "limit": page_size,
        # A fixed sort is what makes paging reproducible: without it the API is
        # free to return the same rows in a different order on the next call.
        # Sorting by Award ID rather than by amount matters — sorting by amount
        # against the upper bound returns page after page of records sitting at
        # exactly the ceiling, a corpus with no amount variance at all.
        "sort": "Award ID",
        "order": "asc",
        "subawards": False,
    }


# USAspending answers this filter slowly and returns a 504 often enough that a
# single-shot fetch is unreliable. Retrying is safe here: the request is a
# read-only search with a fixed sort, so a retried page returns the same rows.
@retry(
    retry=retry_if_exception_type((requests.HTTPError, requests.Timeout, requests.ConnectionError)),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _post(session: requests.Session, body: dict[str, Any], timeout: int) -> dict[str, Any]:
    response = session.post(ENDPOINT, json=body, timeout=timeout)
    response.raise_for_status()
    return dict(response.json())


def fetch(limit: int = 200, page_size: int = 25, timeout: int = 120) -> list[dict[str, Any]]:
    """Page through the award search until ``limit`` records are collected."""
    session = requests.Session()
    collected: list[dict[str, Any]] = []
    page = 1
    while len(collected) < limit:
        body = request_body(page, min(page_size, limit - len(collected)))
        payload = _post(session, body, timeout)
        results = payload.get("results") or []
        if not results:
            break
        collected.extend(results)
        if not payload.get("page_metadata", {}).get("hasNext"):
            break
        page += 1
    return collected[:limit]


def canonical_bytes(records: list[dict[str, Any]]) -> bytes:
    """Stable serialisation, so the checksum is over content and not key order."""
    return json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")


def sha256_of(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def write_manifest(records: list[dict[str, Any]], raw_path: Path, checksum: str) -> dict[str, Any]:
    """``dataset_manifest.json`` — source + checksum for every seeded corpus (Rule 8)."""
    suppliers = sorted({str(r.get("Recipient Name") or "").strip() for r in records} - {""})
    manifest = {
        "name": "usaspending_contract_awards",
        "description": (
            "U.S. federal contract awards (award types A-D) between "
            f"${LOWER_BOUND:,} and ${UPPER_BOUND:,}, awarded "
            f"{TIME_PERIOD['start_date']}..{TIME_PERIOD['end_date']}."
        ),
        "source_url": ENDPOINT,
        "source_docs": DOCS_URL,
        "publisher": "USAspending.gov — U.S. Department of the Treasury",
        "license": "Public domain (17 U.S.C. §105, work of the U.S. Government)",
        "request_body": request_body(page=1, page_size=25),
        "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "raw_path": str(raw_path.relative_to(ROOT)).replace("\\", "/"),
        "sha256": checksum,
        "record_count": len(records),
        "distinct_suppliers": len(suppliers),
        "fields": list(FIELDS),
        "selection_note": (
            "Amount band and award-type filter are a stated, reproducible selection over "
            "real records. No field of any retrieved award is altered, derived or filled in."
        ),
    }
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200, help="records to retrieve")
    parser.add_argument("--page-size", type=int, default=25)
    args = parser.parse_args()

    records = fetch(limit=args.limit, page_size=args.page_size)
    if not records:
        print("USAspending returned no records for this filter", flush=True)
        return 1

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    payload = canonical_bytes(records)
    RAW_PATH.write_bytes(payload)
    checksum = sha256_of(payload)
    manifest = write_manifest(records, RAW_PATH, checksum)

    print(f"retrieved {manifest['record_count']} awards")
    print(f"  distinct suppliers : {manifest['distinct_suppliers']}")
    print(f"  raw                : {manifest['raw_path']}")
    print(f"  sha256             : {checksum}")
    print(f"  manifest           : {MANIFEST_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
