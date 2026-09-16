#!/usr/bin/env python
"""Verifier-independence report (Rule 3, METRICS §4) — a Phase 3 deliverable.

    uv run python scripts/independence_report.py --out results/independence_report.json

Rule 3 says a verifier that echoes the executor is theatre, and PRD §12 asks for
two pieces of evidence:

* `verifier_payload_clean` — the payload never carries the executor's rationale.
  Checkable now, from the prompts of real recorded runs.
* `ev_corr` — executor↔verifier agreement on **known-wrong** cases, which must
  stay below a ceiling. This needs ground-truth labels saying which proposals
  were wrong. Those come from the fault-injection harness in Phase 4.

So this reports what the recorded traces can actually support, and reports the
rest as *unavailable* rather than substituting the overall agreement rate for
the known-wrong one. They are not the same number: on a clean corpus the
executor and verifier agree constantly and correctly, and quoting that as
evidence of independence would be exactly backwards.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from orchestrator.db import Store  # noqa: E402
from verify.verifier import agreement_phi, model_family  # noqa: E402


def _commit() -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def collect(store: Store) -> dict[str, Any]:
    rows = store.conn.execute(
        """
        SELECT t.workflow_id, t.step, t.attempt, t.model, t.prompt,
               t.provenance, l.expected_terminal_action, l.fault_step
          FROM traces t
     LEFT JOIN labels l ON l.workflow_id = t.workflow_id
         ORDER BY t.workflow_id, t.step, t.attempt, t.id
        """
    ).fetchall()

    executor: dict[tuple[str, str, int], dict[str, Any]] = {}
    verifier: dict[tuple[str, str, int], dict[str, Any]] = {}
    for r in rows:
        stage = dict(r["provenance"]).get("stage")
        key = (r["workflow_id"], r["step"], r["attempt"])
        if stage == "executor":
            executor[key] = r
        elif stage == "verifier":
            verifier[key] = r

    paired = sorted(set(executor) & set(verifier))
    if not paired:
        return {"paired_steps": 0}

    leaks: list[str] = []
    families: set[tuple[str, str]] = set()
    agreement: list[tuple[bool, bool]] = []
    known_wrong: list[tuple[bool, bool]] = []

    for key in paired:
        ex, ve = executor[key], verifier[key]
        ex_prov, ve_prov = dict(ex["provenance"]), dict(ve["provenance"])

        # The rationale must not appear in the verifier's prompt. A substring
        # search for the rationale *text* would false-positive, because the
        # executor is told to cite the DELTA fact that decided it and the
        # evidence legitimately contains that string. What is checkable is that
        # the verifier's prompt carries no rationale field and no retry feedback.
        prompt = str(ve["prompt"] or "")
        if "rationale" in prompt.lower() or "PREVIOUS ATTEMPT REJECTED" in prompt:
            leaks.append("/".join(str(k) for k in key))

        families.add((model_family(str(ex["model"])), model_family(str(ve["model"]))))

        proposed_commit = str(ex_prov.get("action", "")) == "proceed"
        verifier_passed = str(ve_prov.get("verdict", "")) == "pass"
        agreement.append((proposed_commit, verifier_passed))

        fault_step = ex["fault_step"]
        if fault_step and str(key[1]) >= str(fault_step):
            # "Known-wrong" describes the STEP, not the executor's specific
            # answer on it: this step's label says the correct action is not to
            # commit, full stop. `x` must still vary within the subset — some
            # instances the executor correctly refused, others it wrongly
            # committed — or phi is computing correlation against a constant
            # and is undefined by construction. Filtering this list down to
            # `proposed_commit=True` only, as an earlier version of this script
            # did, makes `x` constant and `ev_corr` permanently `None` even once
            # real labels exist — silently defeating the one measurement Rule 3
            # actually needs ("a verifier that passes whatever the executor
            # proposes drives ev_corr -> 1 and fails the build" can never be
            # observed if `x` never takes the value 0).
            known_wrong.append((proposed_commit, verifier_passed))

    distinct = all(e != v for e, v in families)
    agree_rate = sum(1 for a, b in agreement if a == b) / len(agreement)

    # The phi coefficient needs BOTH variables to vary. In practice the executor
    # proposes commit on essentially every known-wrong step — that is precisely
    # why a verifier exists — so `x` is constant and phi stays undefined however
    # much data is collected. Fixing the subset filter (an earlier bug) was
    # necessary but not sufficient: the degeneracy is in the phenomenon, not the
    # filter.
    #
    # The conditional disagreement rate *is* defined in exactly that situation,
    # and answers what Rule 3 actually asks: of the steps the label says must not
    # commit and the executor wanted to commit anyway, how often did the verifier
    # say no? A verifier that echoes the executor scores 0.0 here, so this
    # distinguishes the failure Rule 3 names even when phi cannot.
    executor_wanted_commit = [(x, v) for x, v in known_wrong if x]
    caught = sum(1 for _, v in executor_wanted_commit if not v)
    disagreement = caught / len(executor_wanted_commit) if executor_wanted_commit else None
    x_varies = len({x for x, _ in known_wrong}) > 1

    return {
        "paired_steps": len(paired),
        "verifier_payload_clean": not leaks,
        "payload_leaks": leaks,
        "distinct_model_family": distinct,
        "model_pairs": sorted(f"{e} -> {v}" for e, v in families),
        "overall_agreement_rate": round(agree_rate, 4),
        "ev_corr": agreement_phi(known_wrong),
        "ev_corr_n": len(known_wrong),
        "ev_corr_available": bool(known_wrong) and x_varies,
        "ev_corr_undefined_reason": (
            None
            if x_varies or not known_wrong
            else (
                "the executor proposed commit on every known-wrong step, so the phi "
                "coefficient has a constant variable and is undefined by definition"
            )
        ),
        "verifier_disagreement_on_known_wrong": disagreement,
        "verifier_disagreement_n": len(executor_wanted_commit),
        "verifier_caught_count": caught,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="results/independence_report.json")
    args = ap.parse_args()

    load_dotenv()
    with Store() as store:
        report = collect(store)

    if not report.get("paired_steps"):
        print(
            "no paired executor/verifier traces found — run the verified pipeline first "
            "(the baseline configuration has no verifier by design).",
            file=sys.stderr,
        )
        return 1

    report |= {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"), "commit": _commit()}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(f"paired executor/verifier steps : {report['paired_steps']}")
    print(f"verifier payload clean (Rule 3): {report['verifier_payload_clean']}")
    print(f"distinct model family          : {report['distinct_model_family']}")
    print(f"  {', '.join(report['model_pairs'])}")
    print(f"overall agreement rate         : {report['overall_agreement_rate']}")
    if report["ev_corr_available"]:
        print(f"ev_corr on known-wrong (n={report['ev_corr_n']}): {report['ev_corr']}")
    elif report.get("ev_corr_undefined_reason"):
        rate = report["verifier_disagreement_on_known_wrong"]
        print(f"ev_corr on known-wrong (n={report['ev_corr_n']}): UNDEFINED")
        print(f"  {report['ev_corr_undefined_reason']}")
        print(
            f"verifier disagreement on known-wrong: {rate:.3f} "
            f"({report['verifier_caught_count']}/{report['verifier_disagreement_n']})"
        )
        print("  a verifier that echoed the executor would score 0.000 here")
    else:
        print(
            "ev_corr on known-wrong cases   : UNAVAILABLE — needs ground-truth labels\n"
            "  from the Phase 4 fault-injection harness. The overall agreement rate\n"
            "  above is NOT a substitute: on a clean corpus both components agree\n"
            "  constantly and correctly, which says nothing about independence."
        )
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
