"""Regression guard: the fork's Fly build must bake the ``daytona`` extra.

The ``omnigent-tomkit`` Fly app runs managed ``sandbox.provider: daytona``
sessions. The launcher (``omnigent/onboarding/sandboxes/daytona.py``,
``_ensure_sdk`` -> ``import daytona``) lazily imports the ``daytona`` SDK, so the
deployed image MUST be built with the ``daytona`` extra or every managed launch
fails with "The Daytona SDK is required for the 'daytona' sandbox provider".

The fork deploys via ``deploy/fly/fly.tomkit.toml``, which builds from
``deploy/docker/Dockerfile`` and passes ``OMNIGENT_EXTRAS = "daytona"`` as a
build-arg. This test fails if a future upstream rebase (or an edit) silently
drops that wiring. It is fully hermetic — it only parses files in the repo, with
no network, Fly, Docker, or Daytona calls.
"""

from __future__ import annotations

from pathlib import Path

import tomllib

_ROOT = Path(__file__).resolve().parents[2]
_FLY_CONFIG = _ROOT / "deploy/fly/fly.tomkit.toml"


def _build_args() -> dict[str, str]:
    with _FLY_CONFIG.open("rb") as fh:
        config = tomllib.load(fh)
    args: dict[str, str] = config.get("build", {}).get("args", {})
    return args


def _extras() -> list[str]:
    """Comma-separated OMNIGENT_EXTRAS from the fork Fly build, normalized."""
    raw = _build_args().get("OMNIGENT_EXTRAS", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def test_fork_fly_config_exists() -> None:
    assert _FLY_CONFIG.is_file(), f"missing fork Fly config: {_FLY_CONFIG}"


def test_fork_fly_build_declares_daytona_extra() -> None:
    """The core invariant: the deployed image carries the ``daytona`` extra."""
    extras = _extras()
    assert "daytona" in extras, (
        "deploy/fly/fly.tomkit.toml must pass OMNIGENT_EXTRAS containing "
        f"'daytona' so the deployed image can launch managed Daytona sandboxes; "
        f"got OMNIGENT_EXTRAS={_build_args().get('OMNIGENT_EXTRAS')!r}. "
        "Do not drop this on an upstream rebase — see FORK.md."
    )


def test_fork_fly_builds_from_dockerfile() -> None:
    """A build-arg only takes effect if Fly builds from source (not a prebuilt
    image), so the ``daytona`` extra is only actually baked in when ``[build]``
    points at the Dockerfile."""
    with _FLY_CONFIG.open("rb") as fh:
        build = tomllib.load(fh).get("build", {})
    assert "image" not in build, (
        "fly.tomkit.toml must build from the Dockerfile, not pull a prebuilt "
        "image — a prebuilt image ignores OMNIGENT_EXTRAS and would lack the "
        "daytona SDK."
    )
    assert build.get("dockerfile") == "deploy/docker/Dockerfile", (
        "fly.tomkit.toml [build] dockerfile must be 'deploy/docker/Dockerfile'; "
        f"got {build.get('dockerfile')!r}."
    )


def test_dockerfile_honors_omnigent_extras() -> None:
    """The Dockerfile the fork builds from must still consume the build-arg, so
    the extra is actually installed (catches an upstream Dockerfile change that
    removes the ARG)."""
    dockerfile = (_ROOT / "deploy/docker/Dockerfile").read_text()
    assert "ARG OMNIGENT_EXTRAS" in dockerfile
    assert "uv pip install" in dockerfile and '".[${OMNIGENT_EXTRAS}]"' in dockerfile
