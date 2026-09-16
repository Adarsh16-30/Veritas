"""The labelled corpus: real workflows, injected faults, ground truth (PRD §5.1).

Builds two splits that share **zero** workflow IDs (Rule 9) — one to calibrate
the conformal router on, one to benchmark against. The disjointness is
structural (distinct ID prefixes) and then asserted, because a leak here inflates
the calibration result and is a build failure rather than a warning.

Ground truth is written to the `labels` table as the harness builds each case,
so `expected_terminal_action` and `fault_step` come from the injector that
*created* the condition. Nothing infers a label by looking at what the agent did.

Record selection is deterministic given a seed, and matched to what each fault
class actually needs: the *missing* class draws from awards whose description
field is genuinely empty, *ambiguity* from awards whose description is nothing
but an internal routing code. Those pools are small because that is how common
the conditions are in the real data — the harness selects real records, it never
manufactures one to fill a quota.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.context import WorkflowSpec
from data.corpus import AwardRecord, load_records
from harness.faults import INJECTORS, Injection, base_spec

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PLAN_PATH = ROOT / "results" / "corpus_plan.json"

#: The ten scenarios the corpus covers: PRD §6.3's seven classes (two of which
#: have two distinct variants) plus the unfaulted control.
VARIANTS: tuple[str, ...] = (
    "clean",
    "missing",
    "ambiguity",
    "conflicting",
    "adversarial_duplicate",
    "adversarial_injection",
    "boundary_budget_zero",
    "boundary_at_tolerance",
    "temporal",
    "compounding",
)

#: Approval authority for the ordinary case. Real awards in the corpus run to
#: about $60k, so this clears them — the *boundary* class is what deliberately
#: puts an amount up against the limit, rather than every workflow tripping it.
APPROVAL_THRESHOLD = 250_000.0

BENCH_PREFIX = "bench"
CAL_PREFIX = "cal"


class CorpusError(RuntimeError):
    """The corpus cannot be built as specified."""


@dataclass(frozen=True)
class WorkflowCase:
    """One labelled workflow: what to run, and what should happen."""

    workflow_id: str
    variant: str
    record: AwardRecord
    injection: Injection

    @property
    def spec(self) -> WorkflowSpec:
        return self.injection.spec

    def label_row(self) -> dict[str, Any]:
        inj = self.injection
        return {
            "workflow_id": self.workflow_id,
            "fault_class": inj.fault_class,
            "expected_terminal_action": inj.expected_terminal_action,
            "fault_step": inj.fault_step.value if inj.fault_step else None,
        }

    def as_dict(self) -> dict[str, Any]:
        inj = self.injection
        return {
            **self.label_row(),
            "variant": self.variant,
            "rule_detectable": inj.rule_detectable,
            "note": inj.note,
            "item_code": self.record.item_code,
            "supplier": self.record.supplier,
            "award_id": self.record.award_id,
            "amount": str(self.record.amount),
        }


def _pools(records: list[AwardRecord]) -> dict[str, list[AwardRecord]]:
    """Which real records can carry which fault class."""
    empty = [r for r in records if not r.has_description]
    code_only = [r for r in records if r.is_code_only]
    ordinary = [r for r in records if r.has_description and not r.is_code_only]
    if not ordinary:
        raise CorpusError("no usable records with a real description")
    return {
        "missing": empty or code_only,
        "ambiguity": code_only or empty,
        "_ordinary": ordinary,
    }


def _pick(pool: list[AwardRecord], index: int) -> AwardRecord:
    """Deterministic draw that cycles when the real pool is smaller than needed."""
    return pool[index % len(pool)]


def build_split(
    records: list[AwardRecord], prefix: str, per_variant: int, rng: random.Random
) -> list[WorkflowCase]:
    pools = _pools(records)
    ordinary = list(pools["_ordinary"])
    rng.shuffle(ordinary)

    cases: list[WorkflowCase] = []
    ordinary_cursor = 0
    for variant in VARIANTS:
        pool = pools.get(variant)
        for n in range(per_variant):
            if pool is not None:
                offset = 0 if prefix.endswith(BENCH_PREFIX) else per_variant
                record = _pick(pool, n + offset)
            else:
                record = ordinary[ordinary_cursor % len(ordinary)]
                ordinary_cursor += 1
            workflow_id = f"{prefix}-{variant}-{n:02d}"
            spec = base_spec(workflow_id, record, APPROVAL_THRESHOLD)
            injection = INJECTORS[variant](spec, record)
            cases.append(WorkflowCase(workflow_id, variant, record, injection))
    return cases


@dataclass(frozen=True)
class CorpusPlan:
    benchmark: list[WorkflowCase]
    calibration: list[WorkflowCase]
    seed: int

    def assert_disjoint(self) -> None:
        """Rule 9, checked rather than assumed."""
        bench = {c.workflow_id for c in self.benchmark}
        cal = {c.workflow_id for c in self.calibration}
        overlap = bench & cal
        if overlap:
            raise CorpusError(
                f"Rule 9: benchmark and calibration splits share {len(overlap)} workflow "
                f"id(s), e.g. {sorted(overlap)[:5]}"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "variants": list(VARIANTS),
            "approval_threshold": APPROVAL_THRESHOLD,
            "benchmark_ids": [c.workflow_id for c in self.benchmark],
            "calibration_ids": [c.workflow_id for c in self.calibration],
            "benchmark": [c.as_dict() for c in self.benchmark],
            "calibration": [c.as_dict() for c in self.calibration],
        }

    def save(self, path: Path = DEFAULT_PLAN_PATH) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
        return path


def build(
    bench_per_variant: int = 4,
    cal_per_variant: int = 3,
    seed: int = 7,
    run_tag: str = "",
) -> CorpusPlan:
    """The whole corpus, deterministic given ``seed``.

    ``run_tag`` namespaces the workflow IDs. Both configurations run the same
    *scenarios* — same real award, same injected fault, same spec — but they must
    not run the same *workflow IDs*, for two reasons that both silently destroy
    the measurement:

    * Workflows are resumable by design (Rule 6). A verified run over IDs the
      baseline already completed would skip every committed step and measure
      nothing at all.
    * The supplier bill number is derived from the workflow ID. Sharing IDs would
      have the second configuration trip the duplicate-bill guard on the first
      configuration's own invoices, scoring a fault that the harness never
      injected.
    """
    records = load_records()
    bench_prefix = f"{run_tag}-{BENCH_PREFIX}" if run_tag else BENCH_PREFIX
    cal_prefix = f"{run_tag}-{CAL_PREFIX}" if run_tag else CAL_PREFIX
    plan = CorpusPlan(
        benchmark=build_split(records, bench_prefix, bench_per_variant, random.Random(seed)),
        calibration=build_split(records, cal_prefix, cal_per_variant, random.Random(seed + 1)),
        seed=seed,
    )
    plan.assert_disjoint()
    return plan


def persist_labels(store: Any, cases: list[WorkflowCase]) -> int:
    """Write ground truth to the `labels` table (PRD §3.3)."""
    for case in cases:
        row = case.label_row()
        store.conn.execute(
            """
            INSERT INTO labels (workflow_id, fault_class, expected_terminal_action, fault_step)
                 VALUES (%s, %s, %s, %s)
            ON CONFLICT (workflow_id)
              DO UPDATE SET fault_class = EXCLUDED.fault_class,
                            expected_terminal_action = EXCLUDED.expected_terminal_action,
                            fault_step = EXCLUDED.fault_step
            """,
            (
                row["workflow_id"],
                row["fault_class"],
                row["expected_terminal_action"],
                row["fault_step"],
            ),
        )
    return len(cases)
