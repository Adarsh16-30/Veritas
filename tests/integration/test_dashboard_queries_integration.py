"""Every Grafana panel query, executed against a live Prometheus (Phase 6).

PRD Phase 6: "no null-datasource panels". The unit test proves each panel names
the provisioned datasource and each queried metric is exported; this proves the
PromQL itself evaluates against what Prometheus actually scraped, and returns
data. Skipped unless a Prometheus is reachable at PROMETHEUS_URL (default
http://localhost:9090) with the veritas job up.

The one panel allowed to be empty is the calibration-numbers panel while no
calibration has been fitted: it shows "not fitted" rather than a number.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
URL = os.environ.get("PROMETHEUS_URL", "http://localhost:9090").rstrip("/")
DASHBOARD = json.loads(
    (ROOT / "infra" / "observability" / "grafana" / "dashboards" / "veritas.json").read_text(
        "utf-8"
    )
)


def _query(expr: str) -> dict:
    r = requests.get(f"{URL}/api/v1/query", params={"query": expr}, timeout=5)
    r.raise_for_status()
    return dict(r.json())


def _scraping() -> bool:
    try:
        res = _query('up{job="veritas"}')["data"]["result"]
        return any(s["value"][1] == "1" for s in res)
    except (requests.RequestException, KeyError, ValueError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _scraping(), reason=f"no Prometheus scraping veritas at {URL}"),
]

TARGETS = [
    (p["title"], t["expr"].replace("$run_tag", ".*"))
    for p in DASHBOARD["panels"]
    if p["type"] != "row"
    for t in p["targets"]
]


@pytest.mark.parametrize(("title", "expr"), TARGETS, ids=[t for t, _ in TARGETS])
def test_panel_query_returns_data(title: str, expr: str) -> None:
    body = _query(expr)
    assert body["status"] == "success", body
    if "veritas_calibration{" in expr:
        fitted = _query("veritas_calibration_fitted")["data"]["result"]
        if fitted and fitted[0]["value"][1] == "0":
            assert body["data"]["result"] == []  # nothing invented while uncalibrated
            return
    assert body["data"]["result"], f"{title!r} would render empty: {expr}"
