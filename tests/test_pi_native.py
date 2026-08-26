"""Unit tests for the pi-native-ui wrapper agent spec materialization."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnigent.pi_native import _materialize_pi_agent_spec


def test_materialize_pi_agent_spec_pins_model(tmp_path: Path) -> None:
    """A supplied model lands in ``executor.model`` so the runner-owned Pi
    process resolves the injected OpenAI-compatible provider (managed sandboxes
    ship no ~/.omnigent/config.yaml, so the model is the only surface signal)."""
    path = _materialize_pi_agent_spec(tmp_path, model="accounts/fireworks/models/glm-5p2")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["name"] == "pi-native-ui"
    assert raw["executor"] == {
        "harness": "pi-native",
        "model": "accounts/fireworks/models/glm-5p2",
    }
    assert raw["spawn"] is True


def test_materialize_pi_agent_spec_no_model(tmp_path: Path) -> None:
    """Without a model the executor carries only the harness (prior behaviour) —
    the CLI resolves its own model / a session ``model_override`` supplies one."""
    path = _materialize_pi_agent_spec(tmp_path, model=None)

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    assert raw["executor"] == {"harness": "pi-native"}
    assert "model" not in raw["executor"]


def test_materialized_pi_agent_spec_passes_current_validator(tmp_path: Path) -> None:
    """The generated spec (with a pinned model) must not be rejected at upload."""
    from omnigent.spec._omnigent_compat import load_omnigent_yaml

    path = _materialize_pi_agent_spec(tmp_path, model="accounts/fireworks/models/glm-5p2")

    spec = load_omnigent_yaml(path)

    assert spec.executor.config["harness"] == "pi-native"
