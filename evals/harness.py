"""Eval suite runner.

Seeds a real failure into a real cluster, snapshots it, asks the agent, scores
the answer, tears the namespace down. Resumable and cached: a re-run that
changes no prompt replays stored responses and costs nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

from agent import rca
from evals.scoring import aggregate, score_attempt

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"


def _sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=check, timeout=180)


def load_scenarios(selector: str) -> list[dict]:
    paths = sorted(SCENARIO_DIR.glob("*.yaml"))
    scenarios = [yaml.safe_load(p.read_text(encoding="utf-8")) for p in paths]
    if selector and selector != "all":
        wanted = {s.strip() for s in selector.split(",")}
        scenarios = [s for s in scenarios if s["id"] in wanted]
        missing = wanted - {s["id"] for s in scenarios}
        if missing:
            sys.exit(f"Unknown scenario id(s): {', '.join(sorted(missing))}")
    if not scenarios:
        sys.exit("No scenarios selected.")
    return scenarios


def seed(scenario: dict, namespace: str) -> None:
    _sh("kubectl", "create", "namespace", namespace)
    for rel in scenario["manifests"]:
        manifest = (SCENARIO_DIR / rel).resolve()
        _sh("kubectl", "apply", "-n", namespace, "-f", str(manifest))


def teardown(namespace: str) -> None:
    # --wait=false: namespace deletion is slow and nothing downstream depends
    # on it completing, since every scenario gets a fresh name.
    _sh("kubectl", "delete", "namespace", namespace, "--wait=false", check=False)


def run_attempt(scenario: dict, run_idx: int, cache_dir: Path) -> dict:
    key = hashlib.sha256(f"{scenario['id']}:{run_idx}".encode()).hexdigest()[:16]
    cached = cache_dir / f"{key}.json"
    if cached.exists():
        result = json.loads(cached.read_text(encoding="utf-8"))
        result["cached"] = True
        return result

    namespace = f"eval-{scenario['id'][:40]}-{run_idx}"
    started = time.monotonic()
    try:
        seed(scenario, namespace)
        time.sleep(scenario.get("settle_seconds", 45))

        state = rca.collect_state(namespace)
        raw_state = "\n".join(state.values())

        hypothesis, usage = rca.analyze(state)
        scored = score_attempt(hypothesis.model_dump(), scenario, raw_state)

        result = {
            "scenario": scenario["id"],
            "run": run_idx,
            "cached": False,
            "latency_s": round(time.monotonic() - started, 1),
            "usage": usage,
            "hypothesis": hypothesis.model_dump(),
            **scored,
        }
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result
    finally:
        teardown(namespace)


def _load_local_env() -> None:
    """Local runs read .env; CI gets the same variables from Actions secrets.

    Never overrides an already-set variable, so CI's secrets always win over a
    stray .env that happens to exist in the workspace.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)


def main() -> int:
    _load_local_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="all")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", type=Path, default=Path("eval_results/current.json"))
    ap.add_argument("--cache-dir", type=Path, default=Path(".eval_cache"))
    args = ap.parse_args()

    scenarios = load_scenarios(args.scenarios)
    per_scenario: dict[str, list[dict]] = {}
    all_attempts: list[dict] = []
    total_cost = 0.0

    for scenario in scenarios:
        attempts = []
        for run_idx in range(args.runs):
            try:
                result = run_attempt(scenario, run_idx, args.cache_dir)
            except Exception as exc:  # noqa: BLE001 - one bad scenario must not abort the suite
                print(f"  run {run_idx}: ERROR {exc}", file=sys.stderr)
                result = {
                    "scenario": scenario["id"], "run": run_idx, "error": str(exc),
                    "correct": False, "evidence_recall": 0.0, "hallucination_rate": 0.0,
                    "distractor_hit": False, "composite": 0.0,
                }
            attempts.append(result)
            total_cost += result.get("usage", {}).get("cost_usd", 0.0)
            flag = "cache" if result.get("cached") else f"${result.get('usage', {}).get('cost_usd', 0):.4f}"
            mark = "PASS" if result.get("correct") else "FAIL"
            print(f"[{mark}] {scenario['id']} run={run_idx} ({flag})")

        per_scenario[scenario["id"]] = attempts
        all_attempts.extend(attempts)

    report = {
        "model": os.environ.get("RCA_MODEL", "gemini/gemini-2.0-flash"),
        "runs_per_scenario": args.runs,
        "total_cost_usd": round(total_cost, 4),
        "overall": aggregate(all_attempts),
        "scenarios": {sid: aggregate(a) for sid, a in per_scenario.items()},
        "attempts": all_attempts,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    o = report["overall"]
    print(
        f"\naccuracy={o['accuracy']:.1%}  grounding={o['evidence_recall']:.1%}  "
        f"hallucination={o['hallucination_rate']:.1%}  cost=${total_cost:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
