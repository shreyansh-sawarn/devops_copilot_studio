"""Scoring for a single RCA attempt.

Three independent signals, deliberately not collapsed into one number until the
very end — a run can be right for the wrong reasons, and that has to be visible.
"""

from __future__ import annotations

import re
from typing import Any


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def score_attempt(
    hypothesis: dict[str, Any],
    scenario: dict[str, Any],
    raw_state: str,
) -> dict[str, Any]:
    expected = scenario["expected"]
    state_norm = _normalize(raw_state)
    quotes = [q for q in hypothesis.get("evidence", []) if q.strip()]
    quotes_norm = [_normalize(q) for q in quotes]

    # 1. Did it name the right upstream cause? Exact match, no partial credit.
    correct = hypothesis.get("root_cause") == expected["root_cause"]

    # 2. Did it cite the evidence that actually proves the cause?
    wanted = [_normalize(e) for e in expected.get("evidence", [])]
    found = [w for w in wanted if any(w in q for q in quotes_norm)]
    evidence_recall = len(found) / len(wanted) if wanted else 1.0

    # 3. Did it fabricate any quote? This is the check that separates reasoning
    #    from confabulation, and it is the one most likely to catch a regression
    #    that accuracy alone would miss.
    fabricated = [q for q, qn in zip(quotes, quotes_norm) if qn not in state_norm]
    hallucination_rate = len(fabricated) / len(quotes) if quotes else 0.0

    # 4. Did it blame a known-downstream symptom? Scored separately so a
    #    distractor hit is diagnosable, not just a lower number.
    distractor_ids = {d["id"] for d in scenario.get("distractors", [])}
    distractor_hit = hypothesis.get("root_cause") in distractor_ids

    targets = [_normalize(t) for t in expected.get("remediation_targets", [])]
    target_norm = _normalize(hypothesis.get("remediation_target", ""))
    remediation_ok = any(t in target_norm or target_norm in t for t in targets) if targets else True

    # Composite is for ranking runs, never for reporting on its own.
    composite = (
        0.60 * float(correct)
        + 0.25 * evidence_recall
        + 0.15 * float(remediation_ok)
    ) * (1.0 - hallucination_rate)

    return {
        "correct": correct,
        "evidence_recall": round(evidence_recall, 3),
        "hallucination_rate": round(hallucination_rate, 3),
        "fabricated_quotes": fabricated,
        "distractor_hit": distractor_hit,
        "remediation_ok": remediation_ok,
        "composite": round(composite, 3),
        "confidence": hypothesis.get("confidence"),
        "predicted": hypothesis.get("root_cause"),
        "expected": expected["root_cause"],
    }


def aggregate(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    if not attempts:
        return {"n": 0}
    n = len(attempts)

    def mean(key: str) -> float:
        return round(sum(float(a[key]) for a in attempts) / n, 3)

    return {
        "n": n,
        "accuracy": mean("correct"),
        "evidence_recall": mean("evidence_recall"),
        "hallucination_rate": mean("hallucination_rate"),
        "distractor_rate": mean("distractor_hit"),
        "composite": mean("composite"),
    }
