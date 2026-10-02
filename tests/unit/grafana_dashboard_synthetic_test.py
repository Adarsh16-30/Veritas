"""The Grafana dashboard and Prometheus config agree with the exporter.

PRD Phase 6's success criterion is "no null-datasource panels". A panel can be
empty in two quiet ways: it names no datasource (or one that is not
provisioned), or its query names a metric the exporter never produces -- a
renamed series leaves a blank panel and no error anywhere. Both are checked
here. Synthetic: the exporter is scraped over the in-memory scenario and a
stand-in calibration artifact, so every family it can emit is present.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import yaml
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.parser import text_string_to_metric_families

from tests.trace_scenario import MemoryStore, write_baseline, write_verified
from trace.metrics import VeritasCollector

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "infra" / "observability"
DASHBOARD = json.loads((OBS / "grafana" / "dashboards" / "veritas.json").read_text("utf-8"))
DATASOURCES = yaml.safe_load(
    (OBS / "grafana" / "provisioning" / "datasources" / "veritas.yml").read_text("utf-8")
)
UIDS = {d["uid"] for d in DATASOURCES["datasources"]}
METRIC = re.compile(r"\bveritas_[a-z_]+\b")
SUFFIXES = ("_bucket", "_count", "_sum", "_total", "_created")


def _panels() -> list[dict]:
    return [p for p in DASHBOARD["panels"] if p["type"] != "row"]


def _exported(tmp: Path) -> set[str]:
    """Every sample name the exporter can produce, calibration included."""
    for name in ("baseline_results.json", "verified_results.json"):
        shutil.copy(ROOT / "results" / "archive" / "phase4-pre-gapfix" / name, tmp / name)
    (tmp / "calibration.json").write_text("{}", encoding="utf-8")
    (tmp / "calibration_curves.json").write_text(
        json.dumps({"ece_pre": 0.2, "ece_post": 0.04, "coverage": 0.9, "alpha": 0.1}),
        encoding="utf-8",
    )
    store = MemoryStore()
    write_verified(store, "v4-bench-x-00")
    write_baseline(store, "b4-bench-x-00")

    from contextlib import contextmanager

    @contextmanager
    def source():  # type: ignore[no-untyped-def]
        yield store

    registry = CollectorRegistry()
    registry.register(VeritasCollector(source, tmp))
    names: set[str] = set()
    for family in text_string_to_metric_families(generate_latest(registry).decode()):
        names |= {s.name for s in family.samples}
    return names


def test_every_panel_and_query_uses_the_provisioned_datasource() -> None:
    assert _panels()
    for p in _panels():
        assert p.get("datasource", {}).get("uid") in UIDS, p["title"]
        assert p["targets"], p["title"]
        for t in p["targets"]:
            assert t.get("datasource", {}).get("uid") in UIDS, p["title"]
            assert t.get("expr"), p["title"]
    for var in DASHBOARD["templating"]["list"]:
        assert var["datasource"]["uid"] in UIDS


def test_every_queried_metric_is_exported(tmp_path: Path) -> None:
    exported = _exported(tmp_path)
    queried = {m for p in _panels() for t in p["targets"] for m in METRIC.findall(t["expr"])}
    queried |= {m for v in DASHBOARD["templating"]["list"] for m in METRIC.findall(v["definition"])}
    assert queried
    missing = {m for m in queried if m not in exported}
    assert not missing, f"dashboard queries metrics the exporter never produces: {missing}"


def test_layout_is_sane() -> None:
    ids = [p["id"] for p in DASHBOARD["panels"]]
    assert len(ids) == len(set(ids))
    for p in DASHBOARD["panels"]:
        g = p["gridPos"]
        assert g["x"] >= 0 and g["x"] + g["w"] <= 24, p["title"]


def test_prometheus_scrapes_the_exporters_default_port() -> None:
    prom = yaml.safe_load((OBS / "prometheus.yml").read_text("utf-8"))
    targets = [
        t for job in prom["scrape_configs"] for sc in job["static_configs"] for t in sc["targets"]
    ]
    exporter = (ROOT / "scripts" / "metrics_exporter.py").read_text("utf-8")
    port = re.search(r'"--port", type=int, default=(\d+)', exporter)
    assert port and f"host.docker.internal:{port.group(1)}" in targets
