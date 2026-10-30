#!/usr/bin/env python
"""Assert every commit a results file records can still be checked out (Rule 10).

A results file records the commit it was produced on, and every row of
``docs/results.md`` cites it. History rewrites (squashes, scrubs, a linearized
merge) silently orphan those ids: the citation is still there, and points at
nothing. This script finds every recorded commit and requires it to be either

* reachable from ``HEAD``, or
* listed in ``docs/provenance.md`` with a code-equivalent commit that is itself
  reachable from ``HEAD``.

Rows marked ``inferred`` pass with a warning: the mapping is stated, but nobody
has yet diffed it against the original.

In a shallow clone, reachability cannot be decided and the check reports
``could not check`` (exit 2) instead of failing on missing history.

Exit code 0 = every commit traceable; 1 = an untraceable commit; 2 = could not check.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "results"
REPORT = ROOT / "docs" / "results.md"
PROVENANCE = ROOT / "docs" / "provenance.md"

SHA = re.compile(r"^[0-9a-f]{7,40}$")
CITED = re.compile(r"\(commit ([0-9a-f]{7,40})")
ROW = re.compile(
    r"^\|\s*`([0-9a-f]{7,40})`\s*\|.*?\|\s*`([0-9a-f]{7,40})`\s*\|.*\|\s*(\w+)\s*\|\s*$"
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
    )


def _commits_in(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "commit" and isinstance(inner, str) and SHA.match(inner):
                yield inner
            else:
                yield from _commits_in(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _commits_in(inner)


def recorded() -> dict[str, set[str]]:
    """Recorded commit -> the files that record or cite it."""
    found: dict[str, set[str]] = {}
    for path in sorted(RESULTS.rglob("*.json")):
        rel = path.relative_to(ROOT).as_posix()
        for sha in _commits_in(json.loads(path.read_text(encoding="utf-8"))):
            found.setdefault(sha, set()).add(rel)
    if REPORT.is_file():
        for cited in CITED.findall(REPORT.read_text(encoding="utf-8")):
            # The report cites an abbreviated id; fold it into the full one.
            full = next((sha for sha in found if sha.startswith(cited)), cited)
            found.setdefault(full, set()).add("docs/results.md")
    return found


def mapping() -> dict[str, tuple[str, str]]:
    """Recorded-commit prefix -> (equivalent commit, status) from docs/provenance.md."""
    if not PROVENANCE.is_file():
        return {}
    rows: dict[str, tuple[str, str]] = {}
    for line in PROVENANCE.read_text(encoding="utf-8").splitlines():
        m = ROW.match(line)
        if m:
            rows[m.group(1)] = (m.group(2), m.group(3).lower())
    return rows


def reachable(sha: str) -> bool:
    if _git("cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        return False
    return _git("merge-base", "--is-ancestor", sha, "HEAD").returncode == 0


def _lookup(sha: str, rows: dict[str, tuple[str, str]]) -> tuple[str, str] | None:
    for prefix, row in rows.items():
        if sha.startswith(prefix) or prefix.startswith(sha):
            return row
    return None


def main() -> int:
    if _git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        print("could not check: shallow clone (fetch full history to check provenance)")
        return 2
    rows = mapping()
    failures = 0
    for sha, files in sorted(recorded().items()):
        where = ", ".join(sorted(files))
        if reachable(sha):
            continue
        row = _lookup(sha, rows)
        if row is None:
            print(f"UNTRACEABLE {sha[:12]}: not in history and not in docs/provenance.md ({where})")
            failures += 1
        elif not reachable(row[0]):
            print(f"UNTRACEABLE {sha[:12]}: mapped to {row[0]}, which is not in history ({where})")
            failures += 1
        elif row[1] != "equivalent":
            print(f"inferred    {sha[:12]} -> {row[0]} (confirm per docs/provenance.md)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
