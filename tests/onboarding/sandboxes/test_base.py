"""Tests for :mod:`omnigent.onboarding.sandboxes.base`.

These exercise the provider-agnostic default ``run_background`` /
``start_host`` path through a REAL ``/bin/sh``, rather than a fake that
only records command strings. That distinction matters: the launch
command is an env-prefixed string (``OMNIGENT_HOST_TOKEN=… omnigent host
…``), and whether those env vars actually reach the host depends on how a
real POSIX shell parses the produced command line — something a
string-recording fake cannot catch.
"""

from __future__ import annotations

import os
import stat
import subprocess
import time
from pathlib import Path
from typing import ClassVar

import pytest

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import RemoteCommandResult, SandboxLauncher


def _write_executable(path: Path, body: str) -> None:
    """Write *body* to *path* and mark it executable (a PATH shim)."""
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class _LocalShellLauncher(SandboxLauncher):
    """
    Minimal launcher whose ``run`` executes commands through the real
    local ``/bin/sh``.

    Unlike the recording fakes used elsewhere, this actually runs the
    command line ``run_background`` / ``start_host`` produce, so the
    env-prefix-vs-shell-parsing behaviour is genuinely exercised. A
    caller-supplied directory is prepended to ``PATH`` so the test can
    drop in ``setsid`` / ``nohup`` / ``omnigent`` shims and keep the run
    hermetic (no real backgrounding daemons, no real host binary).

    :param shim_bin: Directory prepended to ``PATH`` for the executed
        command (holds the test's ``setsid`` / ``nohup`` / ``omnigent``
        shims).
    :param home: Value the ``$HOME`` probe in ``start_host`` resolves to.
    :param extra_env: Extra environment variables visible to the executed
        command (e.g. where a probe shim should write its capture).
    """

    provider: ClassVar[str] = "localshell"

    def __init__(
        self,
        *,
        shim_bin: Path,
        home: str,
        extra_env: dict[str, str] | None = None,
    ) -> None:
        self._shim_bin = shim_bin
        self._home = home
        self._extra_env = extra_env or {}
        self.commands: list[str] = []

    def prepare(self) -> None:  # pragma: no cover - unused in these tests
        raise NotImplementedError

    def provision(self, name: str) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Execute *command* through the local ``/bin/sh`` with shims on PATH."""
        self.commands.append(command)
        env = {
            **os.environ,
            **self._extra_env,
            "HOME": self._home,
            "PATH": f"{self._shim_bin}{os.pathsep}{os.environ['PATH']}",
        }
        proc = subprocess.run(
            command,
            shell=True,
            executable="/bin/sh",
            capture_output=True,
            text=True,
            env=env,
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"command failed ({proc.returncode}): {proc.stderr}")
        return RemoteCommandResult(
            returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )


@pytest.fixture
def shim_bin(tmp_path: Path) -> Path:
    """
    A PATH directory holding hermetic ``setsid`` / ``nohup`` shims.

    Both shims just ``exec "$@"``: ``setsid`` is absent on macOS (so a
    real call would fail spuriously), and reducing both to a transparent
    exec keeps the test deterministic while STILL exercising the exact
    argument-vs-env parsing that the production command relies on — the
    bug reproduces identically whether ``nohup`` is real or a passthrough.
    """
    shim_bin = tmp_path / "shims"
    shim_bin.mkdir()
    for name in ("setsid", "nohup"):
        _write_executable(shim_bin / name, '#!/bin/sh\nexec "$@"\n')
    return shim_bin


def _read_capture(capture: Path, *, timeout: float = 5.0) -> dict[str, str]:
    """Poll for the probe shim's capture file and parse its ``K=V`` lines."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if capture.exists() and capture.read_text():
            break
        time.sleep(0.02)
    else:
        raise AssertionError(f"probe never wrote {capture} — host command did not run")
    return dict(line.split("=", 1) for line in capture.read_text().splitlines() if "=" in line)


def test_run_background_delivers_env_prefix_to_process(tmp_path: Path, shim_bin: Path) -> None:
    """
    ``run_background`` must deliver a leading ``VAR=value`` env prefix to
    the backgrounded process.

    Regression: the default wrapped the command as
    ``setsid nohup VAR=value cmd …``. In POSIX shell, ``VAR=value`` tokens
    AFTER a command word (``nohup``) are arguments, not assignments, so
    ``nohup`` tried to exec a program named ``VAR=value`` (exit 127) and
    the env vars never reached the process — masked by the trailing
    ``echo launched``. This runs the produced command through a real shell
    and asserts the var actually arrives.
    """
    capture = tmp_path / "env-capture.txt"
    launcher = _LocalShellLauncher(shim_bin=shim_bin, home=str(tmp_path))
    # `printenv` reads the var from the PROCESS environment — the same way
    # the real `omnigent host` does — so it sees the value only if the env
    # prefix was parsed as an assignment rather than swallowed as a nohup
    # argument. (Reading `$VAR` inline would expand from the current shell,
    # not the assignment, and miss the bug entirely.)
    probe = f"printenv {HOST_TOKEN_ENV_VAR} > {capture}"

    launcher.run_background("sb-1", f"{HOST_TOKEN_ENV_VAR}=secret-tok {probe}")

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (capture.exists() and capture.read_text()):
        time.sleep(0.02)
    assert capture.read_text().strip() == "secret-tok"


def test_start_host_delivers_identity_env_to_host(tmp_path: Path, shim_bin: Path) -> None:
    """
    End-to-end: ``start_host`` must launch ``omnigent host`` with the
    ``OMNIGENT_HOST_*`` identity vars present in its environment.

    Drives the full default ``start_host`` (the ``$HOME`` probe, the
    mkdir, the detached launch) through a real ``/bin/sh`` with an
    ``omnigent`` shim standing in for the host binary. The shim records
    the env it was launched with; the assertion proves the token / id /
    name all arrive — the exact failure that left managed hosts stuck at
    "did not come online within 120s".
    """
    capture = tmp_path / "host-env.txt"
    omnigent_shim = (
        "#!/bin/sh\n"
        "{\n"
        f'  printf "TOKEN=%s\\n" "${HOST_TOKEN_ENV_VAR}"\n'
        f'  printf "ID=%s\\n" "${HOST_ID_ENV_VAR}"\n'
        f'  printf "NAME=%s\\n" "${HOST_NAME_ENV_VAR}"\n'
        '  printf "ARGS=%s\\n" "$*"\n'
        f"}} > {capture}\n"
    )
    _write_executable(shim_bin / "omnigent", omnigent_shim)
    launcher = _LocalShellLauncher(shim_bin=shim_bin, home=str(tmp_path))

    workspace = launcher.start_host(
        "sb-1",
        token="tok-abc",
        host_id="host_deadbeef",
        host_name="managed-deadbeef",
        server_url="https://srv.example.com",
    )

    captured = _read_capture(capture)
    assert captured == {
        "TOKEN": "tok-abc",
        "ID": "host_deadbeef",
        "NAME": "managed-deadbeef",
        "ARGS": "host --server https://srv.example.com",
    }
    assert workspace == f"{tmp_path}/workspace"
