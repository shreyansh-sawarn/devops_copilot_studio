"""Kubernetes incident RCA agent.

Deliberately small. The interesting engineering lives in two places:
  1. `collect_state` — what slice of cluster state reaches the model.
  2. `ROOT_CAUSES`  — a closed taxonomy, which is what makes scoring exact
     rather than a fuzzy string comparison.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import litellm
from pydantic import BaseModel, Field, ValidationError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from agent.taxonomy import ROOT_CAUSES

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "rca_system.md"


class RCAHypothesis(BaseModel):
    root_cause: str = Field(description=f"Exactly one of: {', '.join(ROOT_CAUSES)}")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(
        description="Verbatim quotes from the supplied cluster state. Do not paraphrase."
    )
    reasoning: str
    remediation: str
    remediation_target: str = Field(
        description="The specific field to change, e.g. spec.containers[].resources.limits.memory"
    )


def _kubectl(*args: str) -> str:
    result = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=60
    )
    # Non-zero exit is itself diagnostic signal; hand stderr to the model
    # rather than raising and losing it.
    return result.stdout if result.returncode == 0 else f"[kubectl error] {result.stderr}"


def collect_state(namespace: str) -> dict[str, str]:
    """Snapshot the namespace.

    NOTE (known cost/accuracy lever): this dumps everything, which runs ~30-40k
    tokens on a busy namespace. Replacing it with a retrieval layer that
    pre-selects relevant objects is the single highest-leverage change to this
    project — it cuts cost ~4x and should raise accuracy by removing noise.
    Do not optimise it before there is a baseline number to compare against.
    """
    return {
        "pods": _kubectl("get", "pods", "-n", namespace, "-o", "wide"),
        "events": _kubectl(
            "get", "events", "-n", namespace, "--sort-by=.lastTimestamp"
        ),
        "describe_pods": _kubectl("describe", "pods", "-n", namespace),
        "deployments": _kubectl("get", "deploy,rs", "-n", namespace, "-o", "wide"),
        "services": _kubectl("get", "svc,endpoints", "-n", namespace),
        "pvcs": _kubectl("get", "pvc", "-n", namespace),
        "logs": _collect_logs(namespace),
    }


def _collect_logs(namespace: str) -> str:
    """Logs for every pod, current and previous.

    `--previous` is not optional here: on a crash-looping container the current
    instance may have no output yet, and the evidence that explains the crash
    lives in the terminated instance's logs.
    """
    names = _kubectl(
        "get", "pods", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"
    ).split()
    chunks: list[str] = []
    for pod in names:
        current = _kubectl(
            "logs", "-n", namespace, pod, "--all-containers", "--tail=100"
        )
        chunks.append(f"--- pod/{pod} (current) ---\n{current}")
        previous = _kubectl(
            "logs", "-n", namespace, pod, "--all-containers", "--tail=100", "--previous"
        )
        # Absent on a pod that has never restarted; that is normal, not an error.
        if not previous.startswith("[kubectl error]"):
            chunks.append(f"--- pod/{pod} (previous instance) ---\n{previous}")
    return "\n".join(chunks)


def _render_state(state: dict[str, str]) -> str:
    return "\n\n".join(f"### {k}\n```\n{v.strip()}\n```" for k, v in state.items() if v.strip())


@retry(
    retry=retry_if_exception_type((litellm.RateLimitError, litellm.ServiceUnavailableError)),
    wait=wait_exponential(multiplier=2, min=4, max=120),
    stop=stop_after_attempt(6),
    reraise=True,
)
def _call_model(model: str, system: str, user: str) -> tuple[str, dict]:
    response = litellm.completion(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    usage = {
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "cost_usd": litellm.completion_cost(response) or 0.0,
    }
    return response.choices[0].message.content, usage


def analyze(state: dict[str, str], model: str | None = None) -> tuple[RCAHypothesis, dict]:
    """Produce a ranked root-cause hypothesis for a namespace snapshot."""
    model = model or os.environ.get("RCA_MODEL", "gemini/gemini-2.0-flash")
    system = PROMPT_PATH.read_text(encoding="utf-8")
    user = _render_state(state)

    raw, usage = _call_model(model, system, user)

    try:
        return RCAHypothesis.model_validate_json(raw), usage
    except ValidationError as first_error:
        # One repair attempt. A second failure is a real defect worth failing on,
        # not something to paper over with more retries.
        repair, repair_usage = _call_model(
            model,
            system,
            f"{user}\n\nYour previous reply did not validate:\n{first_error}\n"
            f"Previous reply:\n{raw}\n\nReturn corrected JSON only.",
        )
        for k in usage:
            usage[k] += repair_usage[k]
        usage["required_repair"] = True
        return RCAHypothesis.model_validate_json(repair), usage


def budget_guard(estimated_cost: float) -> None:
    """Fail closed before spending, not after."""
    ceiling = float(os.environ.get("RCA_MAX_COST_PER_RUN", "0.25"))
    if estimated_cost > ceiling:
        raise RuntimeError(
            f"Estimated ${estimated_cost:.4f} exceeds RCA_MAX_COST_PER_RUN=${ceiling:.2f}"
        )
