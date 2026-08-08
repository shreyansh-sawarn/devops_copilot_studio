"""Static validation of the scenario corpus.

Runs in seconds with no cluster and no model, so CI fails fast and free on a
malformed scenario instead of discovering it forty minutes and several API
calls later.

The check that matters most: a distractor id outside the taxonomy is one the
agent can never emit, so `distractor_hit` would silently never fire and the
scenario would look like it was passing a test it was not running.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from agent.taxonomy import ROOT_CAUSES

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
REQUIRED = ("id", "title", "category", "manifests", "expected")


def validate_one(path: Path, seen_ids: dict[str, Path]) -> list[str]:
    errs: list[str] = []
    prefix = path.name

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        return [f"{prefix}: unparseable YAML — {exc}"]

    if not isinstance(doc, dict):
        return [f"{prefix}: top level must be a mapping"]

    for field in REQUIRED:
        if field not in doc:
            errs.append(f"{prefix}: missing required field '{field}'")
    if errs:
        return errs

    sid = doc["id"]
    if sid in seen_ids:
        errs.append(f"{prefix}: duplicate id '{sid}' (also in {seen_ids[sid].name})")
    else:
        seen_ids[sid] = path
    if path.stem != sid:
        errs.append(f"{prefix}: id '{sid}' does not match filename stem '{path.stem}'")

    expected = doc["expected"]
    cause = expected.get("root_cause")
    if cause not in ROOT_CAUSES:
        errs.append(f"{prefix}: expected.root_cause '{cause}' is not in the taxonomy")
    if not expected.get("evidence"):
        errs.append(f"{prefix}: expected.evidence is empty — grounding cannot be scored")
    if not expected.get("remediation_targets"):
        errs.append(f"{prefix}: expected.remediation_targets is empty")

    distractors = doc.get("distractors") or []
    if not distractors:
        errs.append(
            f"{prefix}: no distractors — a scenario with no plausible wrong answer "
            f"does not discriminate between agents"
        )
    for d in distractors:
        did = d.get("id")
        if did not in ROOT_CAUSES:
            errs.append(
                f"{prefix}: distractor '{did}' is not in the taxonomy, so the agent "
                f"can never emit it and the check would never fire"
            )
        if did == cause:
            errs.append(f"{prefix}: distractor '{did}' is also the expected root cause")
        if not d.get("why"):
            errs.append(f"{prefix}: distractor '{did}' has no 'why' rationale")

    for rel in doc["manifests"]:
        manifest = SCENARIO_DIR / rel
        if not manifest.exists():
            errs.append(f"{prefix}: manifest not found — {rel}")
            continue
        try:
            docs = [d for d in yaml.safe_load_all(manifest.read_text(encoding="utf-8")) if d]
        except yaml.YAMLError as exc:
            errs.append(f"{prefix}: manifest {rel} is unparseable — {exc}")
            continue
        if not docs:
            errs.append(f"{prefix}: manifest {rel} contains no objects")
        for obj in docs:
            if not isinstance(obj, dict) or "kind" not in obj:
                errs.append(f"{prefix}: manifest {rel} has an object with no 'kind'")

    settle = doc.get("settle_seconds", 45)
    if not isinstance(settle, int) or not 5 <= settle <= 300:
        errs.append(f"{prefix}: settle_seconds={settle} outside the sane range 5-300")

    return errs


def main() -> int:
    paths = sorted(SCENARIO_DIR.glob("*.yaml"))
    if not paths:
        print("No scenarios found.", file=sys.stderr)
        return 1

    seen_ids: dict[str, Path] = {}
    all_errs: list[str] = []
    for path in paths:
        all_errs.extend(validate_one(path, seen_ids))

    if all_errs:
        print(f"{len(all_errs)} problem(s) in the scenario corpus:\n", file=sys.stderr)
        for e in all_errs:
            print(f"  - {e}", file=sys.stderr)
        return 1

    causes = {yaml.safe_load(p.read_text(encoding="utf-8"))["expected"]["root_cause"] for p in paths}
    print(f"{len(paths)} scenarios valid, covering {len(causes)}/{len(ROOT_CAUSES)} root causes.")
    uncovered = sorted(set(ROOT_CAUSES) - causes)
    if uncovered:
        print(f"Not yet covered: {', '.join(uncovered)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
