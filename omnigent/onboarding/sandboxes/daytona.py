"""
Daytona sandbox launcher.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`Daytona <https://www.daytona.io>`_ sandboxes. This module ships in the
OSS build; the Daytona SDK itself is an optional dependency
(``pip install 'omnigent[daytona]'``) imported lazily, so the provider
can be listed and the module probed without it.

Supports both server-managed hosts (``host_type="managed"`` sessions —
``prepare`` / ``provision`` / ``run`` / ``terminate``) and the CLI
bootstrap flow (``omnigent sandbox create`` / ``connect`` — file
shipping via the SDK's filesystem API, foreground attach via a PTY
session). The one unimplemented primitive is ``stream_exec``: its only
consumer is the in-sandbox App OAuth login, which requires
local-to-sandbox port forwarding that Daytona doesn't have — the flow
fails fast on :attr:`SandboxLauncher.supports_local_port_forward`
before ``stream_exec`` would ever run.

Platform notes that shape this launcher:

- **No hard lifetime cap, but idle auto-stop.** Daytona stops sandboxes
  after 15 idle minutes BY DEFAULT — fatal for a session host that may
  sit between turns — so :meth:`DaytonaSandboxLauncher.provision`
  disables auto-stop by default. Sandboxes then live until the session
  is deleted (or the dead-sandbox relaunch path replaces a crashed one).
  Operators who prefer to reclaim idle compute can opt into idle-suspend
  with ``sandbox.daytona.idle_minutes``: the sandbox is provisioned with
  that auto-stop interval and a stop RETAINS its disk (only auto-delete,
  off by default, removes a stopped sandbox), so the server's wake path
  (:meth:`resume`) revives it in place onto the same workspace.
- **Workload env rides sandbox creation.** Daytona has no named-secret
  store to attach at create time; harness credentials are injected as
  literal ``env_vars``, resolved BY NAME from the server process
  environment (``sandbox.daytona.env`` config /
  :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`) so secret values never live
  in the server config file.
- **No inbound port forwarding.** Daytona preview links expose sandbox
  ports publicly but provide no local→sandbox path, so
  ``supports_local_port_forward`` stays ``False``.
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.inner import ui
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from pathlib import Path

    import daytona as daytona_sdk
    from daytona._sync.sandbox import Sandbox as DaytonaSandbox
    from daytona.handle.pty_handle import PtyHandle


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_DAYTONA_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for
Daytona sandboxes, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``)."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_DAYTONA_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process
environment variables whose values are injected into every sandbox this
launcher creates — typically the harness LLM credentials
(``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, gateway base URLs, …) and
``GIT_TOKEN`` that the in-sandbox host forwards to runners. Names, not
values: the values are read from the server's own environment at
provision time, so secrets never live in config files. The server's
managed-host config (``sandbox.daytona.env``) takes precedence when
set."""

# Resources for the sandbox. Matches the Modal launcher's sizing: 2
# vCPU / 4 GiB is enough for a host running one interactive session
# (Daytona's Resources units are vCPUs and GiB).
_SANDBOX_CPU: int = 2
_SANDBOX_MEMORY_GIB: int = 4

# Sandbox-creation timeout. The first create from a given image makes
# Daytona pull the image and build an internal snapshot, which for the
# ~1.4 GiB host image takes minutes; later creates reuse the snapshot
# and take seconds. The SDK default (60 s) only covers the warm path.
_CREATE_TIMEOUT_S: float = 900.0

# Daytona's idle auto-stop default is 15 minutes; 0 disables it. By
# DEFAULT an Omnigent host is always-on (auto-stop disabled): the host
# must survive arbitrary idle gaps between turns and its lifecycle is
# owned by the managed-session machinery (session delete / relaunch
# terminate it). Operators can instead opt into idle-suspend by setting
# a non-zero idle interval (``sandbox.daytona.idle_minutes`` →
# ``DaytonaSandboxLauncher(idle_minutes=…)``): Daytona then stops the
# sandbox after that idle period and the server's wake path
# (:func:`~omnigent.server.managed_hosts.resume_managed_host`) resumes
# it IN PLACE, reattaching its persistent disk. The stop retains the
# filesystem — only auto-delete (disabled by Daytona's default) removes
# a stopped sandbox — so the cloned workspace + omnigent host install
# survive the resume.
_AUTO_STOP_DISABLED: int = 0

# Terminate retries when Daytona reports a state-change conflict (e.g.
# a deletion another cleanup path already started). 3 attempts × 2 s
# covers the observed settle time without stalling best-effort
# teardown callers.
_TERMINATE_CONFLICT_RETRIES: int = 3
_TERMINATE_CONFLICT_BACKOFF_S: float = 2.0


def _ensure_sdk() -> None:
    """
    Verify the Daytona SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is
    an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``daytona`` package is not
        installed.
    """
    try:
        import daytona  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The Daytona SDK is required for the 'daytona' sandbox "
            "provider. Install it with `pip install 'omnigent[daytona]'`, "
            "then set DAYTONA_API_KEY (create a key at "
            "https://app.daytona.io)."
        ) from exc


def _drive_foreground_pty(pty: PtyHandle, sandbox_id: str, command: str) -> int:
    """
    Drive a freshly-created PTY session through one foreground command.

    Sends the command (``TERM`` forced for tmux-spawning harnesses,
    ``exec`` so the PTY's close frame carries the command's own exit
    code), echoes output to the local terminal until exit, and tears
    the websocket down.

    :param pty: Handle for a just-created PTY session (already
        connected; the SDK waits for the connection during creation).
    :param sandbox_id: Sandbox the session runs in, for error messages.
    :param command: Shell command to execute remotely, e.g.
        ``"omnigent host --server https://…"``.
    :returns: The remote command's exit code.
    :raises click.ClickException: When the session ends without
        reporting an exit code (e.g. a dropped websocket).
    :raises KeyboardInterrupt: Re-raised after killing the remote
        process when the user detaches with Ctrl-C.
    """
    try:
        pty.send_input(f"TERM=xterm-256color exec {command}\n")
        result = pty.wait(
            on_data=lambda data: click.echo(data.decode("utf-8", errors="replace"), nl=False)
        )
    except KeyboardInterrupt:
        click.echo("\n  → detaching; stopping the remote process")
        pty.kill()
        raise
    finally:
        pty.disconnect()
    if result.exit_code is None:
        # The websocket dropped (or the daemon reported an error)
        # before the close frame carried an exit code — fail loud
        # rather than inventing a status.
        raise click.ClickException(
            f"The PTY session on sandbox '{sandbox_id}' ended without "
            f"an exit code{f': {result.error}' if result.error else ''}."
        )
    return result.exit_code


class DaytonaSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Daytona sandboxes.

    All transport rides the Daytona SDK: ``sandbox.process.exec`` for
    commands (the Daytona toolbox runs them through a shell, with the
    two output streams merged into one result), ``sandbox.fs`` for
    file shipping, PTY sessions for the foreground attach, and
    ``Daytona.create`` / ``delete`` for lifecycle. Handles are cached
    per sandbox id to avoid a server round-trip on every primitive.
    """

    provider: ClassVar[str] = "daytona"
    # Daytona preview links are sandbox→public only; there is no
    # local→sandbox path for the App OAuth callback port.
    supports_local_port_forward: ClassVar[bool] = False
    # A stopped Daytona sandbox keeps its persistent disk (only
    # auto-delete, disabled by default, removes it), so the server's wake
    # path can resume it in place onto the same workspace rather than
    # provisioning a fresh box. See :meth:`resume` / :meth:`suspend`.
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        idle_minutes: int | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param image: Optional registry image reference to provision
            sandboxes from, e.g. ``"docker.io/me/omnigent-host:latest"``
            — the server's managed-host ``sandbox.daytona.image``
            config. ``None`` resolves :data:`HOST_IMAGE_ENV_VAR` and
            falls back to the official
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param env: Optional names of server-process environment
            variables to inject into every sandbox, e.g.
            ``["OPENAI_API_KEY", "GIT_TOKEN"]`` — the server's
            managed-host ``sandbox.daytona.env`` config. ``None``
            resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`
            (comma-separated) and falls back to no injected env.
        :param idle_minutes: Optional idle auto-stop interval in minutes
            — the server's managed-host ``sandbox.daytona.idle_minutes``
            config. When set (a positive integer), sandboxes are
            provisioned with that auto-stop interval so Daytona stops a
            host idle for that long; the server's wake path resumes it in
            place on the next message. ``None`` (the default) keeps the
            always-on behavior (auto-stop disabled), so existing
            deployments do not regress.
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._idle_minutes = idle_minutes
        self._client: daytona_sdk.Daytona | None = None
        self._sandboxes: dict[str, DaytonaSandbox] = {}

    def _autostop_interval(self) -> int:
        """
        Resolve the idle auto-stop interval (minutes) for this launcher.

        Returns the configured ``idle_minutes`` when idle-suspend is
        enabled, otherwise :data:`_AUTO_STOP_DISABLED` (always-on, the
        default). Used by both :meth:`provision` (at create time) and
        :meth:`keep_alive` (re-asserting it on attached sandboxes), so a
        single source decides whether a host idle-suspends.

        :returns: The auto-stop interval in minutes (0 = disabled).
        """
        return self._idle_minutes if self._idle_minutes is not None else _AUTO_STOP_DISABLED

    def _daytona(self) -> daytona_sdk.Daytona:
        """
        Return the (lazily created) Daytona API client.

        The client reads ``DAYTONA_API_KEY`` / ``DAYTONA_API_URL`` /
        ``DAYTONA_TARGET`` from the process environment — the same
        12-factor posture as the Modal launcher's credentials.

        :returns: The shared client instance.
        """
        if self._client is None:
            import daytona

            self._client = daytona.Daytona()
        return self._client

    def _resolve(self, sandbox_id: str) -> DaytonaSandbox:
        """
        Return the cached handle for *sandbox_id*, looking it up on
        first use.

        :param sandbox_id: Daytona sandbox id (a UUID string).
        :returns: The sandbox handle.
        :raises click.ClickException: When the SDK is not installed or
            the sandbox does not exist.
        """
        # The CLI connect flow reaches primitives without a prepare()
        # preflight — ensure the missing-SDK error stays the friendly
        # install hint rather than a raw ImportError.
        _ensure_sdk()
        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            import daytona

            try:
                handle = self._daytona().get(sandbox_id)
            except daytona.DaytonaNotFoundError as exc:
                raise click.ClickException(
                    f"Daytona sandbox '{sandbox_id}' not found — it may have "
                    "been deleted. Managed sessions provision a replacement "
                    "on the next message."
                ) from exc
            self._sandboxes[sandbox_id] = handle
        return handle

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
        applies; an empty resolution injects nothing. Values come from
        the server's own environment — a configured name that is unset
        there fails loud (an operator listed a credential the
        deployment never provided; silently launching without it would
        surface much later as an opaque harness auth failure).

        :returns: Name → value mapping for ``env_vars`` at creation.
        :raises click.ClickException: When a configured name is not set
            in the server process environment.
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    "sandbox.daytona.env / "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def prepare(self) -> None:
        """
        Local preflight: the Daytona SDK must be installed and an API
        key available.

        :raises click.ClickException: When the SDK is missing or
            ``DAYTONA_API_KEY`` is not set.
        """
        _ensure_sdk()
        if not os.environ.get("DAYTONA_API_KEY"):
            raise click.ClickException(
                "No Daytona credentials found. Create an API key at "
                "https://app.daytona.io and set DAYTONA_API_KEY."
            )

    def provision(self, name: str) -> str:
        """
        Create a new Daytona sandbox from the host image.

        Idle auto-stop is disabled by default (Daytona's 15-minute
        default would kill a host sitting between turns); the sandbox
        then lives until the managed-session machinery terminates it.
        When the launcher is configured with ``idle_minutes``, the
        sandbox is instead created with that auto-stop interval so it
        idle-suspends and the server's wake path resumes it in place.
        The first creation from a given image is slow (Daytona pulls it
        and builds an internal snapshot); later creations reuse the
        snapshot.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
            Recorded as a label; the returned id is the canonical
            reference.
        :returns: The sandbox id (a UUID string).
        """
        _ensure_sdk()
        import daytona

        resolved_ref = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        env_vars = self._resolve_sandbox_env()
        click.echo(f"▸ Creating Daytona sandbox '{name}' from {resolved_ref}")
        try:
            handle = self._daytona().create(
                daytona.CreateSandboxFromImageParams(
                    image=resolved_ref,
                    env_vars=env_vars or None,
                    labels={"omnigent-name": name},
                    # Idle auto-stop: disabled by default (Daytona's
                    # 15-minute default would kill the host between turns)
                    # so the managed-session machinery owns termination;
                    # a configured idle_minutes opts into idle-suspend,
                    # whose stop retains the disk for an in-place resume.
                    auto_stop_interval=self._autostop_interval(),
                    resources=daytona.Resources(cpu=_SANDBOX_CPU, memory=_SANDBOX_MEMORY_GIB),
                ),
                timeout=_CREATE_TIMEOUT_S,
                # First-use image pulls stream build logs; echo them so a
                # slow cold create is visibly progressing in the server log.
                on_snapshot_create_logs=click.echo,
            )
        except daytona.DaytonaError as exc:
            # SDK boundary: surface the provider's reason (quota, image
            # pull failure, "verify your email" account suspensions, …)
            # as the launcher-contract error type so the managed-launch
            # 502 — and a waiting message POST — carries it verbatim
            # instead of a generic "internal error".
            raise click.ClickException(f"Daytona sandbox creation failed: {exc}") from exc
        self._sandboxes[handle.id] = handle
        click.echo(f"  → created {handle.id}")
        return handle.id

    def attach(self, sandbox_id: str) -> None:
        """
        Validate access to an existing sandbox, starting it if stopped.

        Unlike Modal (whose terminated sandboxes are gone for good),
        a stopped Daytona sandbox can be restarted — e.g. one created
        outside this flow whose idle auto-stop kicked in — so attach
        starts it rather than rejecting it.

        :param sandbox_id: The sandbox to attach to (a UUID string).
        :raises click.ClickException: When the sandbox does not exist
            or cannot be started.
        """
        click.echo(f"▸ Reusing existing Daytona sandbox '{sandbox_id}'")
        self._start_if_stopped(sandbox_id, action="attach to")

    def resume(self, sandbox_id: str) -> None:
        """
        Resume a stopped sandbox in place, reattaching its persistent
        disk, so a dormant managed host can be revived under the SAME
        sandbox id.

        Called by the server's managed-host wake path
        (:func:`~omnigent.server.managed_hosts.resume_managed_host`) when
        a host that idle-suspended gets a new message. Mechanically the
        same as :meth:`attach`'s restart of a stopped box — a stopped
        Daytona sandbox keeps its disk (only auto-delete, disabled by
        default, removes it), so the cloned workspace + omnigent install
        survive — but kept as its own method so the capability is
        explicit: :attr:`can_resume` advertises it and the wake path
        restarts ``omnigent host`` separately afterward.

        :param sandbox_id: The stopped sandbox to resume (a UUID string).
        :raises click.ClickException: When the sandbox does not exist or
            cannot be started.
        """
        click.echo(f"▸ Resuming stopped Daytona sandbox '{sandbox_id}'")
        self._start_if_stopped(sandbox_id, action="resume")

    def _start_if_stopped(self, sandbox_id: str, *, action: str) -> None:
        """
        Start *sandbox_id* if it is not already running.

        Shared by :meth:`attach` and :meth:`resume`: refreshes the
        (possibly stale) cached state and issues exactly one start when
        the box is stopped, leaving an already-running box untouched (a
        pointless state-change request the provider may reject as a
        conflict).

        :param sandbox_id: The sandbox to start.
        :param action: Verb for the error message, e.g. ``"attach to"``
            or ``"resume"``.
        :raises click.ClickException: When the sandbox does not exist or
            cannot be started.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.refresh_data()
            if handle.state != daytona.SandboxState.STARTED:
                click.echo(f"  → starting sandbox (state: {handle.state})")
                handle.start()
        except daytona.DaytonaError as exc:
            # SDK boundary: surface the provider's reason (e.g. a
            # sandbox stuck in ERROR state) through the launcher
            # contract instead of a raw SDK traceback.
            raise click.ClickException(
                f"Could not {action} Daytona sandbox '{sandbox_id}': {exc}"
            ) from exc

    def suspend(self, sandbox_id: str) -> None:
        """
        Stop a running sandbox, releasing its compute while RETAINING
        its persistent disk.

        The inverse of :meth:`resume` and the explicit/programmatic
        counterpart to Daytona's own idle auto-stop (provisioned via
        ``idle_minutes``): both leave the sandbox STOPPED, not deleted,
        so the cloned workspace + omnigent install survive for a later
        wake (only auto-delete, disabled by default, would remove a
        stopped sandbox).

        :param sandbox_id: The sandbox to stop (a UUID string).
        :raises click.ClickException: When the sandbox does not exist or
            cannot be stopped.
        """
        click.echo(f"▸ Stopping Daytona sandbox '{sandbox_id}'")
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.stop()
        except daytona.DaytonaError as exc:
            # SDK boundary: surface the provider's reason through the
            # launcher contract instead of a raw SDK traceback.
            raise click.ClickException(
                f"Could not stop Daytona sandbox '{sandbox_id}': {exc}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Reconcile Daytona's idle auto-stop to this launcher's configured
        interval.

        By default this DISABLES idle auto-stop (interval 0) so the host
        survives idle gaps between turns; when the launcher is configured
        with ``idle_minutes`` it instead sets that interval, so attached
        sandboxes created outside this flow idle-suspend consistently
        with provisioned ones (rather than being forced always-on).
        ``provision`` already applies the same interval at create time;
        this re-asserts it. Soft-fail per the launcher contract: a
        rejected setting warns rather than aborting the bootstrap.

        :param sandbox_id: The sandbox to configure.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        interval = self._autostop_interval()
        try:
            handle.set_autostop_interval(interval)
        except daytona.DaytonaError as exc:
            # Interval 0 is always-on: a rejection risks an UNWANTED
            # idle-stop; a non-zero interval is idle-suspend: a rejection
            # risks the wrong (likely Daytona-default) idle timeout. Name
            # the failure either way so the operator knows.
            detail = (
                "could not disable idle auto-stop"
                if interval == _AUTO_STOP_DISABLED
                else f"could not set idle auto-stop to {interval} minutes"
            )
            ui.console.print(
                f"  → warning: {detail} on '{sandbox_id}' ({exc}); the "
                "sandbox may stop after Daytona's idle timeout.",
                style="omni.warning",
                markup=False,
            )
        else:
            message = (
                "idle auto-stop disabled (sandbox lives until deleted)"
                if interval == _AUTO_STOP_DISABLED
                else f"idle auto-stop set to {interval} minutes (host resumes on next message)"
            )
            click.echo(f"  → {message}")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        Daytona's toolbox merges stdout and stderr into one stream, so
        the combined output lands in ``stdout`` and ``stderr`` is
        always empty (the documented
        :class:`~omnigent.onboarding.sandboxes.base.RemoteCommandResult`
        merged-streams convention).

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured combined output.
        :raises click.ClickException: If *check* is ``True`` and the
            command exits non-zero.
        """
        import daytona

        handle = self._resolve(sandbox_id)
        try:
            response = handle.process.exec(command)
        except daytona.DaytonaError as exc:
            # SDK boundary: a stopped/deleted sandbox or toolbox outage
            # must surface its provider reason through the launcher
            # contract, not as a raw SDK exception the managed flow
            # reports as "internal error".
            raise click.ClickException(
                f"Remote command failed to execute on sandbox '{sandbox_id}': {exc}"
            ) from exc
        output = response.result or ""
        for line in output.splitlines():
            if line.strip():
                click.echo(line)
        if check and response.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {response.exit_code}): {command}"
            )
        return RemoteCommandResult(returncode=response.exit_code, stdout=output, stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via the SDK's filesystem
        API.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path on the sandbox,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the transfer fails.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        try:
            handle.fs.upload_file(str(local_path), remote_path)
        except daytona.DaytonaError as exc:
            # SDK boundary: a stopped sandbox or toolbox outage must
            # surface its provider reason through the launcher contract.
            raise click.ClickException(
                f"File upload to sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox over a PTY session, echoing its
        output to the local terminal until it exits; Ctrl-C kills the
        remote process and re-raises.

        The PTY session spawns a shell; the command is sent as a
        single input line with ``exec`` so the shell is replaced and
        the PTY closes (carrying the command's exit code in its close
        frame) when the command exits. ``TERM`` is forced to
        ``xterm-256color`` for the same reason as the Modal launcher:
        native harnesses spawn tmux, which refuses to start under a
        dumb/unset TERM.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises click.ClickException: When the PTY session cannot be
            created or ends without reporting an exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote
            process when the user detaches with Ctrl-C.
        """
        # _resolve runs first and owns the missing-SDK preflight, so the
        # function-local import below can only succeed.
        handle = self._resolve(sandbox_id)
        import daytona

        # PTY session ids must be unique within the sandbox; a fresh
        # suffix per call lets connect be re-run after a detach.
        session_id = f"oa-foreground-{uuid.uuid4().hex[:8]}"
        try:
            pty = handle.process.create_pty_session(id=session_id)
        except daytona.DaytonaError as exc:
            raise click.ClickException(
                f"Could not open a PTY session on sandbox '{sandbox_id}': {exc}"
            ) from exc
        return _drive_foreground_pty(pty, sandbox_id, command)

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the
        prebaked host image — see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        for the flag rationale.

        :param remote_tgz_path: Sandbox path of the shipped tarball,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a sandbox, releasing its compute.

        Idempotent from the caller's perspective: a sandbox that no
        longer exists is treated as success — the desired end state
        holds. A delete that races another state change (Daytona
        reports ``DaytonaConflictError: Sandbox state change in
        progress`` — observed live when two cleanup paths overlap) is
        retried briefly; a deletion already in flight resolves to
        not-found on a later attempt.

        :param sandbox_id: The sandbox to delete.
        :raises daytona.DaytonaError: When the delete still conflicts
            after the retries (callers in the managed teardown path
            are best-effort and log it).
        """
        _ensure_sdk()
        import daytona

        # Hand-rolled bounded retry on purpose: the retry condition is
        # one provider-specific exception in one place, and tenacity is
        # not an omnigent dependency — pulling it in for a 3-iteration
        # loop fails the cost/benefit test.
        for attempt in range(_TERMINATE_CONFLICT_RETRIES):
            try:
                handle = self._daytona().get(sandbox_id)
            except daytona.DaytonaNotFoundError:
                break
            try:
                self._daytona().delete(handle)
                break
            except daytona.DaytonaConflictError:
                if attempt == _TERMINATE_CONFLICT_RETRIES - 1:
                    raise
                time.sleep(_TERMINATE_CONFLICT_BACKOFF_S)
        self._sandboxes.pop(sandbox_id, None)
