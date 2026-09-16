"""The conformal gate: coverage holds, calibration improves, and Rule 9 is enforced.

Rule 9 — "calibration and benchmark sets MUST share zero workflow IDs" — is
tested here as PRD §12 requires: the ID-set disjointness check must pass before
any calibration metric is emitted. It is enforced in three places (fit, use, and
evaluation) because leakage is silent and inflates exactly the number this
project is trying to make trustworthy.

The records are synthetic (`*_synthetic_test.py`) on purpose: a coverage
guarantee is a property of the *estimator*, and testing it needs a generator
whose ground truth is known. The estimator is then applied to real labelled runs
in Phase 4.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from agent.state import Region, Route
from verify.conformal import (
    COMMIT,
    ESCALATE,
    FEATURES,
    CalibrationRecord,
    Calibrator,
    LeakageError,
    NotCalibrated,
    avg_region_size,
    brier,
    coverage,
    ece,
    evaluate,
    route,
    signals,
    unanimous_region,
)

ALPHA = 0.1


def _records(n: int, seed: int, prefix: str = "wf") -> list[CalibrationRecord]:
    """A world where a latent quality drives the truth, and the verifier is a
    noisy, systematically over-confident witness to it.

    Over-confidence is the realistic failure: an 8B model that says 0.95 whatever
    it thinks. That is what gives the "pre" curve a bad ECE for conformal to fix.
    """
    rng = random.Random(seed)
    out: list[CalibrationRecord] = []
    for i in range(n):
        q = rng.random()
        truth = COMMIT if q > 0.4 else ESCALATE
        verifier_pass = (q > 0.45) != (rng.random() < 0.10)  # 10% flip noise
        out.append(
            CalibrationRecord(
                workflow_id=f"{prefix}-{seed}-{i:04d}",
                step="S4",
                signals=signals(
                    executor_proceed=q > 0.30,
                    verifier_passed=verifier_pass,
                    verifier_confidence=0.95,  # always sure, right or wrong
                    rule_violations=0,
                    facts={"a": q > 0.5, "b": q > 0.35, "c": q > 0.6, "d": q > 0.25},
                    amount_at_stake=round(50 + q * 9000, 2),
                    attempt=1,
                ),
                truth=truth,
            )
        )
    return out


@pytest.fixture(scope="module")
def fitted() -> tuple[object, list[CalibrationRecord]]:
    cal = Calibrator(alpha=ALPHA, seed=7).fit(_records(400, seed=1))
    holdout = _records(300, seed=2)
    return cal, holdout


# --- PRD §9.3 route() ----------------------------------------------------------
def test_confident_commit_region_commits() -> None:
    assert route(Region(labels=frozenset({COMMIT}), calibrated=True)) is Route.COMMIT


def test_confident_escalate_region_escalates() -> None:
    assert route(Region(labels=frozenset({ESCALATE}), calibrated=True)) is Route.ESCALATE


def test_ambiguous_region_goes_to_a_human_never_commits() -> None:
    """The single most important line in §9.3."""
    assert route(Region(labels=frozenset({COMMIT, ESCALATE}), calibrated=True)) is Route.ESCALATE


def test_empty_region_goes_to_a_human() -> None:
    assert route(Region(labels=frozenset())) is Route.ESCALATE


# --- coverage ------------------------------------------------------------------
def test_coverage_meets_the_target_on_held_out_data(fitted: tuple) -> None:
    cal, holdout = fitted
    regions = [cal.region(r.signals) for r in holdout]
    got = coverage(regions, [r.truth for r in holdout])
    # Conformal guarantees >= 1 - alpha marginally; allow finite-sample slack
    # downward only, and report the actual number on failure.
    assert got >= 1 - ALPHA - 0.04, f"coverage {got:.3f} below target {1 - ALPHA}"


def test_regions_are_decisive_not_trivially_wide(fitted: tuple) -> None:
    """A region containing every label always "covers" the truth. Coverage without
    this check is free and meaningless (METRICS §3.4)."""
    cal, holdout = fitted
    regions = [cal.region(r.signals) for r in holdout]
    size = avg_region_size(regions)
    assert size < 1.5, f"average region size {size:.2f} — the gate is not deciding anything"
    singletons = sum(1 for r in regions if r.is_singleton()) / len(regions)
    assert singletons > 0.5, f"only {singletons:.0%} of regions decide anything"


def test_an_atypical_point_yields_an_empty_region_and_goes_to_a_human() -> None:
    """Both labels can score above qhat. Conformal calls that an empty region —
    "no label is plausible here" — and it is a real outcome, not a bug. The only
    safe reading is that the evidence is unlike anything calibrated on, so §9.3
    routes it to a person."""
    cal = Calibrator(alpha=0.35, seed=7).fit(_records(400, seed=1))
    holdout = _records(300, seed=2)
    empties = [cal.region(r.signals) for r in holdout if not cal.region(r.signals).labels]
    assert empties, "expected some atypical points at this alpha"
    assert all(route(r) is Route.ESCALATE for r in empties)


def test_calibration_improves_ece_over_the_raw_verifier_confidence(fitted: tuple) -> None:
    cal, holdout = fitted
    curves = evaluate(cal, holdout)
    assert curves.ece_post < curves.ece_pre, (
        f"conformal made calibration worse: pre={curves.ece_pre:.3f} post={curves.ece_post:.3f}"
    )
    assert curves.brier_post < curves.brier_pre


def test_a_tighter_alpha_widens_regions() -> None:
    """Demanding more coverage must cost decisiveness, not come free."""
    records = _records(400, seed=1)
    holdout = _records(200, seed=3)
    loose = Calibrator(alpha=0.20, seed=7).fit(records)
    tight = Calibrator(alpha=0.01, seed=7).fit(records)
    assert tight.qhat >= loose.qhat
    wide = avg_region_size([tight.region(r.signals) for r in holdout])
    narrow = avg_region_size([loose.region(r.signals) for r in holdout])
    assert wide >= narrow


def test_unreachable_alpha_yields_the_widest_region_not_a_tightened_threshold() -> None:
    """With too few points for the requested alpha, the honest answer is "I cannot
    be that sure" — every label in the region — not a quietly relaxed quantile."""
    cal = Calibrator(alpha=0.001, seed=7, min_records=20).fit(_records(40, seed=5))
    assert cal.qhat == float("inf")
    region = cal.region(_records(1, seed=6)[0].signals)
    assert region.labels == frozenset({COMMIT, ESCALATE})
    assert route(region) is Route.ESCALATE


# --- Rule 9 --------------------------------------------------------------------
def test_fit_refuses_when_calibration_overlaps_the_benchmark() -> None:
    records = _records(60, seed=1)
    benchmark = {records[0].workflow_id, records[5].workflow_id}
    with pytest.raises(LeakageError, match="Rule 9"):
        Calibrator(alpha=ALPHA).fit(records, benchmark_ids=benchmark)


def test_a_fitted_model_refuses_to_be_scored_against_its_own_ids(fitted: tuple) -> None:
    cal, _ = fitted
    with pytest.raises(LeakageError, match="Rule 9"):
        cal.assert_disjoint_from([cal.workflow_ids[0]])


def test_disjoint_sets_are_accepted(fitted: tuple) -> None:
    cal, holdout = fitted
    cal.assert_disjoint_from([r.workflow_id for r in holdout])  # must not raise
    assert not set(cal.workflow_ids) & {r.workflow_id for r in holdout}


def test_evaluate_refuses_a_leaky_holdout(fitted: tuple) -> None:
    """The check PRD §12 asks for: disjointness passes *before* a metric is emitted."""
    cal, holdout = fitted
    leaky = [
        *holdout[:10],
        CalibrationRecord(cal.workflow_ids[0], "S4", holdout[0].signals, COMMIT),
    ]
    with pytest.raises(LeakageError):
        evaluate(cal, leaky)


def test_the_train_calibration_split_is_by_workflow_not_by_row() -> None:
    """Two steps of one workflow are not independent; splitting between them
    leaks the workflow across the boundary."""
    rows = []
    for i in range(60):
        for step in ("S4", "S6"):
            r = _records(1, seed=100 + i)[0]
            rows.append(CalibrationRecord(f"wf-{i:03d}", step, r.signals, r.truth))
    cal = Calibrator(alpha=ALPHA, seed=7).fit(rows)
    assert cal.n_train + cal.n_calibration == len(rows)
    assert len(cal.workflow_ids) == 60


# --- refusing to fake it --------------------------------------------------------
def test_too_few_records_refuses_rather_than_producing_a_number() -> None:
    with pytest.raises(NotCalibrated, match="too few"):
        Calibrator(alpha=ALPHA).fit(_records(5, seed=1))


def test_uncalibrated_region_carries_no_coverage_claim() -> None:
    region = unanimous_region(rule_ok=True, verifier_passed=True, executor_proceed=True)
    assert region.calibrated is False
    assert route(region) is Route.COMMIT
    with pytest.raises(NotCalibrated, match="uncalibrated"):
        coverage([region], [COMMIT])


def test_unanimity_is_required_for_the_uncalibrated_commit() -> None:
    for rule_ok, vpass, proceed in [
        (False, True, True),
        (True, False, True),
        (True, True, False),
        (False, False, False),
    ]:
        region = unanimous_region(rule_ok=rule_ok, verifier_passed=vpass, executor_proceed=proceed)
        assert route(region) is Route.ESCALATE


# --- metric definitions (METRICS §3) --------------------------------------------
def test_ece_is_zero_for_a_perfectly_calibrated_predictor() -> None:
    probs = [1.0] * 50 + [0.0] * 50
    truths = [COMMIT] * 50 + [ESCALATE] * 50
    assert ece(probs, truths) == pytest.approx(0.0)


def test_ece_is_one_for_a_confidently_wrong_predictor() -> None:
    assert ece([1.0] * 20, [ESCALATE] * 20) == pytest.approx(1.0)


def test_brier_matches_its_definition() -> None:
    assert brier([1.0, 0.0], [COMMIT, ESCALATE]) == pytest.approx(0.0)
    assert brier([0.0, 1.0], [COMMIT, ESCALATE]) == pytest.approx(1.0)


def test_metrics_refuse_an_empty_sample() -> None:
    for fn in (ece, brier):
        with pytest.raises(ValueError):
            fn([], [])
    with pytest.raises(ValueError):
        avg_region_size([])


# --- the artifact (Rule 10) ------------------------------------------------------
def test_calibration_artifact_round_trips(fitted: tuple, tmp_path: Path) -> None:
    from verify.conformal import CalibrationModel

    cal, holdout = fitted
    path = tmp_path / "calibration.json"
    cal.save(path)
    loaded = CalibrationModel.load(path)

    assert loaded.qhat == cal.qhat
    assert loaded.alpha == cal.alpha
    assert loaded.workflow_ids == cal.workflow_ids
    for r in holdout[:25]:
        assert loaded.region(r.signals).labels == cal.region(r.signals).labels


def test_artifact_records_its_provenance(fitted: tuple) -> None:
    cal, _ = fitted
    d = cal.as_dict()
    assert d["features"] == list(FEATURES)
    assert d["n_calibration"] > 0
    assert "created_at" in d


def test_artifact_from_a_different_signal_vector_is_refused(tmp_path: Path) -> None:
    """Silently scoring new signals with an old model is how a calibration claim
    stops meaning anything."""
    from verify.conformal import CalibrationModel

    path = tmp_path / "stale.json"
    path.write_text(
        json.dumps(
            {
                "alpha": 0.1,
                "qhat": 0.5,
                "features": ["some", "older", "vector"],
                "model": {"weights": [0.0] * 3, "bias": 0.0, "mean": [0.0] * 3, "std": [1.0] * 3},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NotCalibrated, match="different signal vector"):
        CalibrationModel.load(path)


def test_signals_never_include_the_executor_rationale() -> None:
    """Rule 3 reaches into the router too: it sees the choice, not the argument."""
    sig = signals(
        executor_proceed=True,
        verifier_passed=True,
        verifier_confidence=0.9,
        rule_violations=0,
        facts={"a": True},
        amount_at_stake="50.00",
        attempt=1,
    )
    assert set(sig) == set(FEATURES)
    assert not any("rational" in k for k in sig)
