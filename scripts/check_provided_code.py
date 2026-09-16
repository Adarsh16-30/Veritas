#!/usr/bin/env python
"""Assert every PRD §9 "PROVIDED — DO NOT MODIFY" block is byte-identical in source.

PRD §9 hands over four primitives and says not to modify them. Nothing enforced
that, so the guarantee rested on nobody running a formatter — and a formatter is
exactly what broke it: `ruff format` collapsed comment alignment inside the
provided code, and `ruff`'s isort split a provided combined import. Neither
changed behaviour, and neither should have happened.

Each block now sits behind a bare `# fmt: off` directive, and this script diffs
it against VERITAS_PRD.md on every rule check. Drift is a build failure.

Exit code 0 = all blocks verbatim; 1 = drift (with a diff); 2 = could not check.
"""

from __future__ import annotations

import difflib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRD = ROOT / "VERITAS_PRD.md"

#: PRD §9 heading -> the module that must reproduce it verbatim.
PROVIDED: dict[str, str] = {
    "9.1 Idempotency Key + Guarded ERPNext Write (Rule 5)": "erp/idempotent.py",
    "9.2 Checkpoint Before Side Effect (Rule 6)": "orchestrator/checkpoint.py",
    "9.3 Conformal Routing (Phase 3)": "verify/conformal.py",
    "9.4 Verifier Independence Guard (Rule 3)": "verify/verifier.py",
}


def prd_block(text: str, section: str) -> str | None:
    """The fenced python block under a §9 heading.

    The PRD labels each block with a `# src/...` path comment. PRD §11 mandates
    root-level packages, so that label is a pointer, not code, and is dropped.
    """
    match = re.search(r"### " + re.escape(section) + r".*?```python\n(.*?)```", text, re.S)
    if not match:
        return None
    lines = [ln for ln in match.group(1).splitlines() if not ln.strip().startswith("# src/")]
    return "\n".join(lines).strip("\n")


def main() -> int:
    if not PRD.exists():
        print(f"FAIL  cannot find {PRD}", file=sys.stderr)
        return 2
    prd_text = PRD.read_text(encoding="utf-8")

    drifted = 0
    for section, relpath in PROVIDED.items():
        path = ROOT / relpath
        label = f"PRD §{section.split()[0]} -> {relpath}"
        block = prd_block(prd_text, section)
        if block is None:
            print(f"FAIL  {label}: no fenced block under that heading in the PRD")
            drifted += 1
            continue
        if not path.exists():
            # Not yet built is not drift — §9.3/§9.4 land in Phase 3.
            print(f"SKIP  {label}: not present yet")
            continue
        source = path.read_text(encoding="utf-8")
        if block in source:
            print(f"OK    {label}")
            continue
        drifted += 1
        print(f"FAIL  {label}: provided code has been modified")
        # Show the drift against the closest run of lines in the file.
        src_lines = source.splitlines()
        want = block.splitlines()
        best, best_score = 0, -1.0
        for i in range(max(1, len(src_lines) - len(want) + 1)):
            window = src_lines[i : i + len(want)]
            score = difflib.SequenceMatcher(None, want, window).ratio()
            if score > best_score:
                best, best_score = i, score
        diff = difflib.unified_diff(
            want,
            src_lines[best : best + len(want)],
            fromfile=f"VERITAS_PRD.md §{section.split()[0]}",
            tofile=relpath,
            lineterm="",
        )
        for line in diff:
            print("      " + line)

    if drifted:
        print(f"\n{drifted} provided block(s) drifted from the PRD.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
