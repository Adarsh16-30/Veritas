"""Static checks on the Helm chart and the laptop compose file.

``helm lint`` and ``kubeconform`` run in CI (.github/workflows/release.yml). These
catch, without a cluster or a Helm binary, the mistakes that render silently
wrong: a template reading a values key that does not exist (Helm renders it
empty), an environment variable no code reads, a weakened security default, or
a memory limit over the laptop budget (docs/operations.md).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator.settings import Settings

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "veritas"
VALUES = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
LAPTOP = yaml.safe_load((CHART / "values-laptop.yaml").read_text(encoding="utf-8"))
TEMPLATES = sorted((CHART / "templates").glob("*"))


def _has(tree: Any, path: list[str]) -> bool:
    for key in path:
        if not isinstance(tree, dict) or key not in tree:
            return False
        tree = tree[key]
    return True


def test_every_values_path_a_template_reads_exists() -> None:
    missing = set()
    for t in TEMPLATES:
        for ref in re.findall(r"\.Values\.([A-Za-z0-9_.]+)", t.read_text(encoding="utf-8")):
            path = ref.rstrip(".").split(".")
            if not _has(VALUES, path):
                missing.add(f"{t.name}: .Values.{ref}")
    assert not missing, sorted(missing)


def _leaves(tree: Any, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    if isinstance(tree, dict):
        return [leaf for k, v in tree.items() for leaf in _leaves(v, (*prefix, k))]
    return [prefix]


def test_laptop_values_only_override_known_keys() -> None:
    unknown = [
        ".".join(p) for p in _leaves(LAPTOP) if p[0] != "config" and not _has(VALUES, list(p))
    ]
    assert not unknown, unknown


def _env_names_read_by_code() -> set[str]:
    names: set[str] = set()
    for path in ROOT.glob("*/**/*.py"):
        if ".venv" in path.parts or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        names |= set(re.findall(r"os\.environ(?:\.get)?[\[(]\s*\"([A-Z0-9_]+)\"", text))
    return names


@pytest.mark.parametrize("values", [VALUES, LAPTOP], ids=["values", "values-laptop"])
def test_every_config_variable_is_read_by_something(values: dict[str, Any]) -> None:
    """A misspelt variable is silently ignored and the default wins."""
    known = {name.upper() for name in Settings.model_fields} | _env_names_read_by_code()
    unknown = sorted(set(values.get("config", {})) - known)
    assert not unknown, unknown


def test_security_defaults() -> None:
    pod, ctr = VALUES["podSecurityContext"], VALUES["securityContext"]
    assert pod["runAsNonRoot"] is True and pod["runAsUser"] == 10001
    assert pod["seccompProfile"]["type"] == "RuntimeDefault"
    assert ctr["readOnlyRootFilesystem"] is True
    assert ctr["allowPrivilegeEscalation"] is False
    assert ctr["capabilities"]["drop"] == ["ALL"]
    assert VALUES["networkPolicy"]["enabled"] is True
    assert VALUES["config"]["VERITAS_ENV"] == "production"
    assert VALUES["config"]["POSTGRES_SSLMODE"] in ("require", "verify-ca", "verify-full")


def test_no_secret_is_configured_through_values() -> None:
    secret_like = re.compile(r"(PASSWORD|SECRET|API_KEY|TOKEN)$")
    for values in (VALUES, LAPTOP):
        leaked = [k for k in values.get("config", {}) if secret_like.search(k)]
        assert not leaked, leaked


def test_the_chart_dashboard_is_the_maintained_one() -> None:
    source = ROOT / "infra" / "observability" / "grafana" / "dashboards" / "veritas.json"
    assert (CHART / "files" / "veritas-dashboard.json").read_bytes() == source.read_bytes()


def _mb(limit: str) -> int:
    n, unit = int(limit[:-1]), limit[-1].lower()
    return n * 1024 if unit == "g" else n


def test_laptop_compose_fits_the_memory_budget() -> None:
    """New long-running services stay within ~400 MB (docs/operations.md)."""
    compose = yaml.safe_load(
        (ROOT / "infra" / "docker-compose" / "service.yml").read_text(encoding="utf-8")
    )
    services = compose["services"]
    long_running = [s for name, s in services.items() if name != "veritas-migrate"]
    assert sum(_mb(s["mem_limit"]) for s in long_running) <= 400
    for name, s in services.items():
        assert s.get("read_only") is True and s.get("cap_drop") == ["ALL"], name
