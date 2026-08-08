"""Compare an eval run against the committed baseline and emit a Markdown report.

Exits non-zero when accuracy regresses beyond the allowed threshold, so a prompt
change that quietly makes the agent worse cannot merge.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _delta(current: float, baseline: float | None) -> str:
    if baseline is None:
        return "—"
    d = current - baseline
    if abs(d) < 0.0005:
        return "±0.0"
    return f"{'+' if d > 0 else ''}{d:+.1%}".replace("++", "+")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--current", type=Path, required=True)
    ap.add_argument("--baseline", type=Path, required=True)
    ap.add_argument("--max-regression", type=float, default=0.05)
    ap.add_argument("--markdown", action="store_true")
    args = ap.parse_args()

    current = json.loads(args.current.read_text(encoding="utf-8"))
    baseline = (
        json.loads(args.baseline.read_text(encoding="utf-8"))
        if args.baseline.exists()
        else None
    )

    cur = current["overall"]
    base = baseline["overall"] if baseline else {}
    lines: list[str] = []

    lines.append("## Eval results\n")
    lines.append(
        f"**Model** `{current['model']}` · "
        f"**{cur['n']} attempts** ({current['runs_per_scenario']} per scenario) · "
        f"**cost** ${current['total_cost_usd']:.4f}\n"
    )

    lines.append("| Metric | Current | Baseline | Δ |")
    lines.append("|---|---:|---:|---:|")
    for label, key in [
        ("Root-cause accuracy", "accuracy"),
        ("Evidence grounding", "evidence_recall"),
        ("Hallucinated quotes", "hallucination_rate"),
        ("Blamed a distractor", "distractor_rate"),
    ]:
        b = base.get(key)
        lines.append(
            f"| {label} | {cur[key]:.1%} | "
            f"{f'{b:.1%}' if b is not None else '—'} | {_delta(cur[key], b)} |"
        )

    lines.append("\n<details><summary>Per scenario</summary>\n")
    lines.append("| Scenario | Accuracy | Grounding | Δ accuracy |")
    lines.append("|---|---:|---:|---:|")
    for sid, agg in sorted(current["scenarios"].items()):
        b = (baseline or {}).get("scenarios", {}).get(sid, {}).get("accuracy")
        lines.append(
            f"| `{sid}` | {agg['accuracy']:.1%} | {agg['evidence_recall']:.1%} "
            f"| {_delta(agg['accuracy'], b)} |"
        )
    lines.append("\n</details>")

    regressed = False
    if baseline:
        drop = base.get("accuracy", 0.0) - cur["accuracy"]
        if drop > args.max_regression:
            regressed = True
            lines.append(
                f"\n> **Blocked:** accuracy dropped {drop:.1%}, "
                f"over the {args.max_regression:.0%} threshold."
            )
    else:
        lines.append(
            "\n> No baseline committed yet. Run the workflow on `main` with "
            "`update_baseline: true` to record one."
        )

    output = "\n".join(lines)
    print(output if args.markdown else json.dumps(current["overall"], indent=2))
    return 1 if regressed else 0


if __name__ == "__main__":
    raise SystemExit(main())
