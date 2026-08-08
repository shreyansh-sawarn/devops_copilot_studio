"""The closed set of root causes the agent may return.

Deliberately dependency-free so the scenario validator can import it without
installing an inference stack — the validator has to run in seconds, before
CI spends money on a cluster or a model.

Adding an entry here is a breaking change to the eval baseline: every scenario
scored against the old taxonomy should be re-run before the baseline is updated.
"""

from __future__ import annotations

ROOT_CAUSES: tuple[str, ...] = (
    "container-oomkilled",
    "image-pull-failure",
    "readiness-probe-misconfigured",
    "liveness-probe-misconfigured",
    "pvc-unbound",
    "resource-quota-exceeded",
    "insufficient-node-resources",
    "crashloop-application-error",
    "config-or-secret-missing",
    "dns-resolution-failure",
    "network-policy-blocking",
    "service-selector-mismatch",
)
