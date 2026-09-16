"""The headline plot: the compounding-failure curve (PRD §1, §5.3).

    uv run python -m bench.plots --results-dir results --out docs/compounding_curve.svg

Written as plain SVG rather than through a plotting library. The chart is one
line per configuration plus a reference curve, and a 30 MB dependency to draw
three polylines would be a worse trade than eighty lines of string formatting —
which also stays deterministic, diffable in review, and renders inline in
GitHub markdown without a build step.

The reference line is ``p^k`` for the measured per-step reliability ``p``: what
you would expect if each step's failures were independent. The whole argument
for verification is that real workflows do *not* decay that gracefully, so the
reference is drawn to be argued with rather than to flatter the result.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench.metrics import summarise  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CAPS = {"baseline": 18, "verified": 36}

W, H = 720, 420
PAD_L, PAD_R, PAD_T, PAD_B = 70, 150, 40, 55
PLOT_W, PLOT_H = W - PAD_L - PAD_R, H - PAD_T - PAD_B

BASELINE_COLOUR = "#b45309"
VERIFIED_COLOUR = "#1d4ed8"
REFERENCE_COLOUR = "#9ca3af"


def _x(k: int, n: int = 6) -> float:
    return PAD_L + (k - 1) / max(1, n - 1) * PLOT_W


def _y(rate: float) -> float:
    return PAD_T + (1 - rate) * PLOT_H


def _curve(summary: dict[str, Any]) -> list[float]:
    return [
        summary["survive"][f"k{k}"]["rate"] or 0.0
        for k in range(1, 7)
        if summary["survive"].get(f"k{k}")
    ]


def _polyline(points: list[float], colour: str, dashed: bool = False) -> str:
    coords = " ".join(f"{_x(i + 1):.1f},{_y(v):.1f}" for i, v in enumerate(points))
    dash = ' stroke-dasharray="6 5"' if dashed else ""
    marks = "".join(
        f'<circle cx="{_x(i + 1):.1f}" cy="{_y(v):.1f}" r="3.5" fill="{colour}"/>'
        for i, v in enumerate(points)
    )
    return (
        f'<polyline points="{coords}" fill="none" stroke="{colour}" '
        f'stroke-width="2.5"{dash}/>{marks}'
    )


def render_svg(baseline: list[float] | None, verified: list[float] | None, caption: str) -> str:
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif">',
        f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
        f'<text x="{PAD_L}" y="24" font-size="15" font-weight="600" fill="#111827">'
        "Compounding-failure curve</text>",
    ]

    # axes and gridlines
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = _y(frac)
        parts.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L + PLOT_W}" y2="{y:.1f}" '
            'stroke="#e5e7eb" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{PAD_L - 10}" y="{y + 4:.1f}" font-size="11" fill="#6b7280" '
            f'text-anchor="end">{frac * 100:.0f}%</text>'
        )
    for k in range(1, 7):
        parts.append(
            f'<text x="{_x(k):.1f}" y="{PAD_T + PLOT_H + 20:.1f}" font-size="11" '
            f'fill="#6b7280" text-anchor="middle">S{k}</text>'
        )
    parts.append(
        f'<text x="{PAD_L + PLOT_W / 2:.1f}" y="{H - 16}" font-size="12" fill="#374151" '
        'text-anchor="middle">steps completed correctly (k)</text>'
    )

    # reference p^k, from the measured first-step reliability
    anchor = (verified or baseline or [1.0])[0]
    parts.append(_polyline([anchor**k for k in range(1, 7)], REFERENCE_COLOUR, dashed=True))

    legend: list[tuple[str, str]] = [(REFERENCE_COLOUR, "independent-failure reference (p^k)")]
    if baseline:
        parts.append(_polyline(baseline, BASELINE_COLOUR))
        legend.append((BASELINE_COLOUR, "baseline (no gate)"))
    if verified:
        parts.append(_polyline(verified, VERIFIED_COLOUR))
        legend.append((VERIFIED_COLOUR, "verified (three gates)"))

    for i, (colour, label) in enumerate(legend):
        y = PAD_T + 14 + i * 22
        parts.append(
            f'<line x1="{PAD_L + PLOT_W + 14}" y1="{y}" x2="{PAD_L + PLOT_W + 40}" y2="{y}" '
            f'stroke="{colour}" stroke-width="2.5"/>'
        )
        parts.append(
            f'<text x="{PAD_L + PLOT_W + 46}" y="{y + 4}" font-size="11" fill="#374151">'
            f"{label}</text>"
        )

    parts.append(f'<text x="{PAD_L}" y="{H - 2}" font-size="10" fill="#9ca3af">{caption}</text>')
    parts.append("</svg>")
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--out", default="docs/compounding_curve.svg")
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.is_absolute():
        results_dir = ROOT / results_dir

    def load(name: str, cap: int) -> list[float] | None:
        path = results_dir / name
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _curve(summarise(payload["results"], cap))

    baseline = load("baseline_results.json", CAPS["baseline"])
    verified = load("verified_results.json", CAPS["verified"])
    if not baseline and not verified:
        print("no results to plot — run bench.run first", file=sys.stderr)
        return 1

    caption = "measured; see docs/results.md for citations and docs/limitations.md for caveats"
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_svg(baseline, verified, caption), encoding="utf-8")
    print(f"wrote {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
