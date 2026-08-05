"""Structural test for the fork's OWN agent bundles (``deploy/agents/``).

These bundles are fork-owned and are NOT baked into the server image: they are
delivered to the Fly volume and registered via ``OMNIGENT_BUILTIN_AGENT_DIRS``,
which shadows the image's ``examples/`` copies. They live outside ``examples/``
precisely so upstream never edits the same paths — that keeps them off the
rebase conflict surface.

What breaks if this fails:
- the enabled roster drifts from claude_code / codex / pi (the three workers
  these deployments actually have CLIs for),
- the router-first worker policy regresses to a hardcoded per-purpose table,
- pi loses its GLM pin, so the no-router fallback dispatches an unroutable
  model.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/deploy/test_fork_agent_bundles.py -> repo root is 2 parents up.
_AGENTS = Path(__file__).resolve().parents[2] / "deploy" / "agents"
_POLLY = _AGENTS / "polly"
_POLLY_FW = _AGENTS / "polly-fw"

# The Fireworks GLM the fork pins for pi on the no-router fallback path.
_GLM = "accounts/fireworks/models/glm-5p2"


@pytest.fixture(scope="module")
def polly_spec() -> AgentSpec:
    """Load and validate the fork's polly bundle once for the module."""
    return load(_POLLY)


def test_both_fork_bundles_load() -> None:
    """Both fork bundles parse — a broken bundle silently disables the agent."""
    assert load(_POLLY).name == "polly"
    assert load(_POLLY_FW).name == "polly-fw"


def test_enabled_roster_is_the_three_workers_with_clis(polly_spec: AgentSpec) -> None:
    """Only claude_code / codex / pi are registered.

    opencode / cursor / hermes specs still ship under ``agents/`` (upstream's
    shape), but registering a worker whose CLI is absent just produces a
    boot-failure inbox item mid-run.
    """
    assert sorted(polly_spec.tools.agents) == ["claude_code", "codex", "pi"]


def test_worker_routing_defers_to_the_router(polly_spec: AgentSpec) -> None:
    """The prompt asks the v0.7.0 router to pick, and pins the fallback order.

    v0.7.0's ``sys_advise_models`` sizes each dispatch against the live
    per-worker model catalog, so a hardcoded per-purpose table both duplicates
    it and goes stale. The fallback order still has to be explicit for
    deployments with no ``llm:`` judge configured.
    """
    prompt = yaml.safe_load((_POLLY / "config.yaml").read_text(encoding="utf-8"))["prompt"]

    assert "sys_advise_models" in prompt
    assert "Let the ROUTER do it" in prompt
    # Fallback preference order, used when no router is configured.
    assert "`claude_code` first, then `codex`, then `pi`" in prompt
    # pi needs an explicit model on that path — the env names none.
    assert _GLM in prompt
    # The superseded fixed table must not creep back.
    assert "PRIMARY reviewer for ALL diffs" not in prompt


def test_cross_vendor_independence_still_overrides_the_router(
    polly_spec: AgentSpec,
) -> None:
    """Review is never routed to the implementer's own vendor.

    This is a correctness rule, not a cost heuristic, so it has to win over
    whatever the router recommends — otherwise a diff can review itself.
    """
    prompt = yaml.safe_load((_POLLY / "config.yaml").read_text(encoding="utf-8"))["prompt"]

    assert "Cross-vendor independence WINS over any recommendation" in prompt
    assert "review is ALWAYS done by a DIFFERENT" in prompt


def test_skills_reference_only_enabled_workers() -> None:
    """No skill dispatches to a worker the roster no longer registers."""
    for skill in sorted(_POLLY.glob("skills/*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        for dropped in ("opencode", "cursor", "hermes"):
            assert dropped not in text, f"{skill.name} still dispatches to {dropped}"


def test_bundles_do_not_point_at_the_examples_tree() -> None:
    """Self-references name deploy/agents, not the upstream examples path.

    polly authors its own skills, so a stale ``examples/polly/skills`` path in
    the prompt would have it write into the upstream tree we no longer own.
    """
    for bundle in (_POLLY, _POLLY_FW):
        config = (bundle / "config.yaml").read_text(encoding="utf-8")
        assert "examples/polly" not in config
        assert f"deploy/agents/{bundle.name}/skills" in config
