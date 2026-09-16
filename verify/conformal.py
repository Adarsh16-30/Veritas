"""ConformalRouter — gate 3 of 3 (PRD §4.1, §9.3, Rules 7 & 9).

Split conformal prediction, implemented directly rather than pulled from a
library, because the whole value of this gate is that its coverage guarantee is
inspectable.

How it works:

1. A small logistic model turns the gate's signals into ``p(commit)``. It is
   fitted, never hand-tuned — hand-tuned thresholds are exactly the "calibrated"
   confidence this project exists to avoid.
2. That model is fitted on a *proper training* subset and the nonconformity
   scores are taken on a *disjoint calibration* subset of the labelled split.
   Scoring on the data the model was fitted to inflates coverage; the split is
   made inside :meth:`Calibrator.fit` so it cannot be forgotten.
3. ``qhat`` is the ``ceil((n+1)(1-alpha))/n`` empirical quantile of the true
   label's nonconformity. The prediction region is every label scoring at or
   below it, which yields marginal coverage ``>= 1 - alpha``.
4. ``route()`` (PRD §9.3, provided verbatim) turns the region into a decision. A
   region that is not a confident singleton goes to a human. Ambiguity never
   silently commits.

A region may come back **empty** when every label scores above ``qhat``. That is
a normal conformal outcome meaning "this evidence is unlike anything I was
calibrated on", not a defect — and ``route()`` already sends it to a human,
which is the only safe reading of it.

Rule 9 is enforced in code, not by convention: a fitted model carries the
workflow IDs it was calibrated on, and :meth:`CalibrationModel.assert_disjoint_from`
raises before any coverage number can be emitted against an overlapping set.

Until a labelled split exists, there is no fitted model and nothing here
pretends otherwise — see :func:`unanimous_region`.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.state import Region, Route

# --- PRD §9.3 — PROVIDED, DO NOT MODIFY ---------------------------------------


# PROVIDED VERBATIM (PRD §9.3). `fmt: off` must stay a bare directive —
# a trailing comment on the same line silently disables it.
# fmt: off
def route(region) -> Route:
    # Prediction region derived from calibrated nonconformity scores.
    # Singleton, confident region -> commit; ambiguous region -> escalate.
    if region.is_singleton() and region.covers("commit"):
        return Route.COMMIT
    if region.is_singleton() and region.covers("escalate"):
        return Route.ESCALATE
    return Route.ESCALATE   # ambiguity defaults to human, never silent commit
# fmt: on


# --- end provided code ---------------------------------------------------------

COMMIT = "commit"
ESCALATE = "escalate"
LABELS: tuple[str, ...] = (COMMIT, ESCALATE)

#: The signal vector, in fixed order. Every entry is available at gate time and
#: none of them is the executor's rationale (Rule 3) — the router sees what the
#: executor *chose*, never why it says it chose it.
FEATURES: tuple[str, ...] = (
    "executor_proceed",
    "verifier_pass",
    "verifier_confidence",
    "rule_violations",
    "facts_clean",
    "log_amount",
    "attempt",
)


class LeakageError(RuntimeError):
    """Rule 9 violation: calibration and benchmark sets share a workflow ID."""


class NotCalibrated(RuntimeError):
    """A calibrated quantity was asked for before a model was fitted."""


def signals(
    *,
    executor_proceed: bool,
    verifier_passed: bool,
    verifier_confidence: float,
    rule_violations: int,
    facts: Mapping[str, object],
    amount_at_stake: object,
    attempt: int,
) -> dict[str, float]:
    """The gate's evidence, as numbers. Deterministic and rationale-free."""
    booleans = [v for v in facts.values() if isinstance(v, bool)]
    clean = (sum(1 for v in booleans if v) / len(booleans)) if booleans else 1.0
    try:
        amount = float(str(amount_at_stake))
    except (TypeError, ValueError):
        amount = 0.0
    return {
        "executor_proceed": 1.0 if executor_proceed else 0.0,
        "verifier_pass": 1.0 if verifier_passed else 0.0,
        "verifier_confidence": float(verifier_confidence),
        "rule_violations": float(rule_violations),
        "facts_clean": float(clean),
        "log_amount": math.log10(1.0 + max(amount, 0.0)),
        "attempt": float(attempt),
    }


@dataclass(frozen=True)
class CalibrationRecord:
    """One labelled gate decision. ``truth`` is what *should* have happened."""

    workflow_id: str
    step: str
    signals: dict[str, float]
    truth: str  # "commit" | "escalate"

    def vector(self) -> list[float]:
        return [float(self.signals.get(f, 0.0)) for f in FEATURES]


# --- the probability model ------------------------------------------------------
@dataclass
class LogisticModel:
    """Logistic regression, fitted by deterministic gradient descent.

    Deliberately tiny and dependency-free: seven features and a bias. Anything
    larger would overfit a calibration split of this size, and the point of the
    model is to be a well-behaved score for the conformal layer to wrap, not to
    be a classifier in its own right.
    """

    weights: list[float]
    bias: float
    mean: list[float]
    std: list[float]

    def _z(self, x: Sequence[float]) -> list[float]:
        return [(xi - m) / s for xi, m, s in zip(x, self.mean, self.std, strict=True)]

    def p_commit(self, x: Sequence[float]) -> float:
        z = sum(w * xi for w, xi in zip(self.weights, self._z(x), strict=True)) + self.bias
        # Clamp before exp so a confident model cannot overflow to NaN.
        z = max(-30.0, min(30.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    @classmethod
    def fit(
        cls,
        rows: Sequence[Sequence[float]],
        ys: Sequence[int],
        *,
        iterations: int = 4000,
        lr: float = 0.15,
        l2: float = 1e-3,
    ) -> LogisticModel:
        if not rows:
            raise NotCalibrated("cannot fit a probability model on zero rows")
        n, d = len(rows), len(rows[0])
        mean = [sum(r[j] for r in rows) / n for j in range(d)]
        var = [sum((r[j] - mean[j]) ** 2 for r in rows) / n for j in range(d)]
        # A constant feature has zero variance; 1.0 leaves it untouched rather
        # than dividing by zero.
        std = [math.sqrt(v) if v > 1e-12 else 1.0 for v in var]
        model = cls(weights=[0.0] * d, bias=0.0, mean=mean, std=std)
        zs = [model._z(r) for r in rows]

        for _ in range(iterations):
            gw = [0.0] * d
            gb = 0.0
            for z, y in zip(zs, ys, strict=True):
                s = sum(w * zi for w, zi in zip(model.weights, z, strict=True)) + model.bias
                s = max(-30.0, min(30.0, s))
                err = 1.0 / (1.0 + math.exp(-s)) - y
                for j in range(d):
                    gw[j] += err * z[j]
                gb += err
            for j in range(d):
                model.weights[j] -= lr * (gw[j] / n + l2 * model.weights[j])
            model.bias -= lr * (gb / n)
        return model

    def as_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "bias": self.bias,
            "mean": self.mean,
            "std": self.std,
        }


# --- the conformal layer --------------------------------------------------------
@dataclass
class CalibrationModel:
    """A fitted split-conformal router, and the provenance to defend it."""

    model: LogisticModel
    alpha: float
    qhat: float
    workflow_ids: tuple[str, ...]
    n_train: int
    n_calibration: int
    created_at: str = ""
    commit: str = ""
    labels: tuple[str, ...] = LABELS

    def scores(self, x: Sequence[float]) -> dict[str, float]:
        """Nonconformity: how poorly each label fits this evidence."""
        p = self.model.p_commit(x)
        return {COMMIT: 1.0 - p, ESCALATE: p}

    def region(self, sig: Mapping[str, float]) -> Region:
        x = [float(sig.get(f, 0.0)) for f in FEATURES]
        sc = self.scores(x)
        labels = frozenset(lbl for lbl, s in sc.items() if s <= self.qhat)
        return Region(
            labels=labels,
            p_commit=self.model.p_commit(x),
            calibrated=True,
            alpha=self.alpha,
        )

    # --- Rule 9 ----------------------------------------------------------------
    def assert_disjoint_from(self, benchmark_ids: Sequence[str] | set[str]) -> None:
        """Refuse to be used against a set it was calibrated on.

        Called before any coverage/ECE number is emitted. Leakage inflates the
        result and is a build failure, so this raises rather than warns.
        """
        overlap = set(self.workflow_ids) & set(benchmark_ids)
        if overlap:
            raise LeakageError(
                f"Rule 9: calibration and benchmark sets share {len(overlap)} workflow "
                f"id(s), e.g. {sorted(overlap)[:5]}"
            )

    # --- persistence (Rule 10) --------------------------------------------------
    def as_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "alpha": self.alpha,
            "qhat": self.qhat,
            "features": list(FEATURES),
            "labels": list(self.labels),
            "model": self.model.as_dict(),
            "n_train": self.n_train,
            "n_calibration": self.n_calibration,
            "workflow_ids": list(self.workflow_ids),
            "created_at": self.created_at,
            "commit": self.commit,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict(), indent=2, sort_keys=True), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> CalibrationModel:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if list(raw.get("features", [])) != list(FEATURES):
            raise NotCalibrated(
                "calibration artifact was fitted on a different signal vector "
                f"({raw.get('features')}); refit before using it"
            )
        m = raw["model"]
        return cls(
            model=LogisticModel(
                weights=list(m["weights"]),
                bias=float(m["bias"]),
                mean=list(m["mean"]),
                std=list(m["std"]),
            ),
            alpha=float(raw["alpha"]),
            qhat=float(raw["qhat"]),
            workflow_ids=tuple(raw.get("workflow_ids", [])),
            n_train=int(raw.get("n_train", 0)),
            n_calibration=int(raw.get("n_calibration", 0)),
            created_at=str(raw.get("created_at", "")),
            commit=str(raw.get("commit", "")),
        )


@dataclass
class Calibrator:
    """Fits a :class:`CalibrationModel` from a labelled, benchmark-disjoint split."""

    alpha: float = 0.1
    seed: int = 7
    train_fraction: float = 0.5
    min_records: int = 20

    def fit(
        self,
        records: Sequence[CalibrationRecord],
        benchmark_ids: Sequence[str] | set[str] = (),
    ) -> CalibrationModel:
        if len(records) < self.min_records:
            raise NotCalibrated(
                f"{len(records)} labelled records is too few to calibrate "
                f"(minimum {self.min_records}). A conformal guarantee from a handful of "
                "points is a number, not a guarantee."
            )
        ids = {r.workflow_id for r in records}
        overlap = ids & set(benchmark_ids)
        if overlap:
            raise LeakageError(
                f"Rule 9: {len(overlap)} calibration workflow id(s) are also in the "
                f"benchmark set, e.g. {sorted(overlap)[:5]}"
            )

        # Split by *workflow*, not by row: two steps of one workflow are not
        # independent, and splitting mid-workflow leaks across the boundary.
        ordered = sorted(ids)
        random.Random(self.seed).shuffle(ordered)
        cut = max(1, int(len(ordered) * self.train_fraction))
        train_ids = set(ordered[:cut])

        train = [r for r in records if r.workflow_id in train_ids]
        calib = [r for r in records if r.workflow_id not in train_ids]
        if not train or not calib:
            raise NotCalibrated(
                "the labelled split did not yield both a training and a calibration "
                "partition; more distinct workflows are needed"
            )

        model = LogisticModel.fit(
            [r.vector() for r in train],
            [1 if r.truth == COMMIT else 0 for r in train],
        )

        # Nonconformity of the TRUE label on the held-out calibration partition.
        scores = sorted(
            (1.0 - model.p_commit(r.vector())) if r.truth == COMMIT else model.p_commit(r.vector())
            for r in calib
        )
        n = len(scores)
        k = math.ceil((n + 1) * (1.0 - self.alpha))
        # k > n means this alpha is unreachable at this sample size. The honest
        # response is the widest region — never a quietly tightened threshold.
        qhat = float("inf") if k > n else scores[k - 1]

        return CalibrationModel(
            model=model,
            alpha=self.alpha,
            qhat=qhat,
            workflow_ids=tuple(sorted(ids)),
            n_train=len(train),
            n_calibration=n,
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            commit=_git_commit(),
        )


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# --- the uncalibrated path ------------------------------------------------------
def unanimous_region(*, rule_ok: bool, verifier_passed: bool, executor_proceed: bool) -> Region:
    """The region used when no calibration artifact exists yet.

    This is **not** conformal and makes no coverage claim — ``calibrated`` stays
    False and every metric path refuses to report coverage or ECE from it. It is
    a deliberately conservative stand-in: all three gates must agree before a
    commit, and any dissent routes to a human.

    It exists so the verified pipeline is runnable and testable before the
    labelled corpus lands, not so the calibration step can be skipped.
    """
    agree = rule_ok and verifier_passed and executor_proceed
    return Region(labels=frozenset({COMMIT if agree else ESCALATE}), calibrated=False)


# --- metrics (docs/METRICS.md §3) -----------------------------------------------
def ece(probs: Sequence[float], truths: Sequence[str], bins: int = 10) -> float:
    """Expected Calibration Error, equal-width bins (METRICS §3.1).

    Confidence is ``max(p, 1-p)`` and a prediction is correct when the more
    likely label is the true one — the standard binary formulation.
    """
    if len(probs) != len(truths):
        raise ValueError("probs and truths differ in length")
    n = len(probs)
    if n == 0:
        raise ValueError("ECE is undefined for zero predictions")
    buckets: list[list[tuple[float, int]]] = [[] for _ in range(bins)]
    for p, t in zip(probs, truths, strict=True):
        conf = max(p, 1.0 - p)
        predicted = COMMIT if p >= 0.5 else ESCALATE
        idx = min(bins - 1, int(conf * bins)) if conf < 1.0 else bins - 1
        buckets[idx].append((conf, 1 if predicted == t else 0))
    total = 0.0
    for b in buckets:
        if not b:
            continue
        acc = sum(c for _, c in b) / len(b)
        conf = sum(cf for cf, _ in b) / len(b)
        total += (len(b) / n) * abs(acc - conf)
    return total


def brier(probs: Sequence[float], truths: Sequence[str]) -> float:
    """Brier score with ``y_i = 1`` when the true label is ``commit`` (METRICS §3.2)."""
    if not probs:
        raise ValueError("Brier score is undefined for zero predictions")
    return sum(
        (p - (1.0 if t == COMMIT else 0.0)) ** 2 for p, t in zip(probs, truths, strict=True)
    ) / len(probs)


def coverage(regions: Sequence[Region], truths: Sequence[str]) -> float:
    """Fraction of regions containing the true label (METRICS §3.3).

    Refuses to score an uncalibrated region: a coverage number computed from
    :func:`unanimous_region` would be a claim the method never made.
    """
    if not regions:
        raise ValueError("coverage is undefined for zero regions")
    for r in regions:
        if not r.calibrated:
            raise NotCalibrated(
                "coverage was asked of an uncalibrated region; fit a calibration "
                "model on a benchmark-disjoint labelled split first (Rule 9)"
            )
    return sum(1 for r, t in zip(regions, truths, strict=True) if r.covers(t)) / len(regions)


def avg_region_size(regions: Sequence[Region]) -> float:
    """Mean labels per region (METRICS §3.4). Reported beside coverage so a
    trivially wide region cannot masquerade as good calibration."""
    if not regions:
        raise ValueError("region size is undefined for zero regions")
    return sum(len(r.labels) for r in regions) / len(regions)


@dataclass
class CalibrationCurves:
    """Pre/post calibration figures, a Phase 3 deliverable."""

    n: int
    alpha: float
    ece_pre: float
    ece_post: float
    brier_pre: float
    brier_post: float
    coverage: float
    avg_region_size: float
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "alpha": self.alpha,
            "ece_pre": self.ece_pre,
            "ece_post": self.ece_post,
            "brier_pre": self.brier_pre,
            "brier_post": self.brier_post,
            "coverage": self.coverage,
            "avg_region_size": self.avg_region_size,
            **self.extra,
        }


def evaluate(
    cal: CalibrationModel,
    holdout: Sequence[CalibrationRecord],
    benchmark_ids: Sequence[str] | set[str] = (),
) -> CalibrationCurves:
    """Pre- vs post-calibration on a held-out split (METRICS §3).

    "Pre" is the verifier's own stated confidence in its verdict — the raw,
    uncalibrated signal. "Post" is the fitted model's ``p(commit)``. Reporting
    both is what makes the calibration claim checkable.
    """
    cal.assert_disjoint_from(benchmark_ids)
    cal.assert_disjoint_from([r.workflow_id for r in holdout])
    if not holdout:
        raise NotCalibrated("no held-out records to evaluate calibration against")

    truths = [r.truth for r in holdout]
    pre = [
        r.signals.get("verifier_confidence", 0.5)
        if r.signals.get("verifier_pass", 0.0) >= 0.5
        else 1.0 - r.signals.get("verifier_confidence", 0.5)
        for r in holdout
    ]
    post = [cal.model.p_commit(r.vector()) for r in holdout]
    regions = [cal.region(r.signals) for r in holdout]

    return CalibrationCurves(
        n=len(holdout),
        alpha=cal.alpha,
        ece_pre=ece(pre, truths),
        ece_post=ece(post, truths),
        brier_pre=brier(pre, truths),
        brier_post=brier(post, truths),
        coverage=coverage(regions, truths),
        avg_region_size=avg_region_size(regions),
        extra={"qhat": cal.qhat, "n_calibration": cal.n_calibration, "commit": cal.commit},
    )
