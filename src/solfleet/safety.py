"""Safety gate: policy.yaml model + the decision every mutation passes through.

Two independent protections:
  1. dry-run by default. A mutation runs only with confirm=true; otherwise
     it returns the ordered plan and changes nothing.
  2. policy checks. Even with confirm=true, per-cluster rules can deny
     (version not allow-listed, disk too full, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_POLICY_PATHS = ("policy.yaml", "policy.yml")


class PolicyRules(BaseModel):
    max_concurrent_restarts: int = 1
    allowed_versions: list[str] = ["*"]
    min_disk_free_pct: int | None = None
    # validators: refuse to restart unless a leader gap of at least this
    # many minutes is available (0 disables the wait, e.g. for RPC-only).
    require_leader_window_minutes: int = 5


class Policy(BaseModel):
    defaults: PolicyRules = PolicyRules()
    clusters: dict[str, PolicyRules] = {}

    def for_cluster(self, name: str) -> PolicyRules:
        return self.clusters.get(name, self.defaults)


def load_policy(path: str | Path | None = None) -> Policy:
    """Load policy.yaml. Absent file -> conservative defaults (one restart
    at a time, any version allowed, no disk floor)."""
    if path:
        resolved: Path | None = Path(path)
    else:
        resolved = next((Path(p) for p in DEFAULT_POLICY_PATHS if Path(p).exists()), None)
    if not resolved:
        return Policy()
    with open(resolved) as f:
        raw = yaml.safe_load(f) or {}
    return Policy.model_validate(raw)


def version_allowed(rules: PolicyRules, version: str | None) -> bool:
    if version is None:
        return False
    return any(fnmatch(version, pat) for pat in rules.allowed_versions)


def disk_free_ok(rules: PolicyRules, use_pcts: list[int]) -> bool:
    """use_pcts: list of 'used %' integers across the node's mounts."""
    if rules.min_disk_free_pct is None:
        return True
    return all((100 - used) >= rules.min_disk_free_pct for used in use_pcts)


@dataclass
class GateDecision:
    operation: str
    cluster: str
    node: str
    mode: str  # "dry-run" | "execute"
    allowed: bool
    plan: list[str]
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "cluster": self.cluster,
            "node": self.node,
            "mode": self.mode,
            "allowed": self.allowed,
            "plan": self.plan,
            "reasons": self.reasons,
        }


def gate(
    *,
    operation: str,
    cluster: str,
    node: str,
    confirm: bool,
    plan: list[str],
    checks: list[tuple[bool, str]],
) -> GateDecision:
    """Build the decision. checks are (passed, failure_message) pairs.

    allowed reflects whether the preflight passes (evaluated in both
    modes, so a dry-run truthfully predicts whether execution would be
    blocked). mode alone decides whether we actually change anything: a
    caller executes only when mode == "execute" and allowed is True."""
    mode = "execute" if confirm else "dry-run"
    failed = [message for passed, message in checks if not passed]
    allowed = not failed
    reasons: list[str] = []
    if failed:
        if not confirm:
            reasons.append("dry-run: the following checks would block execution")
        reasons.extend(failed)
    elif not confirm:
        reasons.append("dry-run: preflight checks pass; pass confirm=true to execute")
    return GateDecision(operation, cluster, node, mode, allowed, plan, reasons)
