"""Server-launched sandbox hosts for ``host_type="managed"`` sessions.

The external host flow has a human run ``omnigent host`` on their own
machine. The managed flow replaces the human: when a session is created
with ``host_type="managed"``, the server provisions a cloud sandbox,
starts ``omnigent host`` inside it, and waits for that host to
register — after which the session rides the exact same host-launch
machinery an external host uses (binding token, ``host.launch_runner``
frame, runner tunnel).

The host's identity is DURABLE while its sandbox is not: the ``hosts``
row carries the managed columns (launch-token digest + expiry,
provider, sandbox id), and a relaunch overwrites them in place — a new
sandbox generation under the same ``host_id``, so session bindings
survive a sandbox dying at the provider's lifetime cap.

The sandbox host authenticates back with a dedicated launch token the
server mints per launch (see
:meth:`omnigent.stores.host_store.HostStore.register_managed_host` and
the managed-token branch in
:mod:`omnigent.server.routes.host_tunnel`) — the user's own
credentials never enter the sandbox.

How a deployment supplies the sandbox backend (two paths, one seam —
:class:`ManagedSandboxConfig` carries a launcher FACTORY, so embedding
deployments inject custom launchers the same way they inject custom
stores into ``create_app``):

1. **Server YAML** (OSS / self-hosted): :func:`parse_sandbox_config`
   builds the config from the ``sandbox:`` section
   (``omnigent server -c`` / ``OMNIGENT_CONFIG`` /
   ``<data_dir>/config.yaml``)::

       sandbox:
         provider: modal          # lakebox|modal|daytona|boxlite|cwsandbox|islo|e2b|openshell
         server_url: https://omnigent.example.com
         modal:                   # optional block
           image: docker.io/me/omnigent-host:latest  # default: official image
           secrets: [omnigent-llm]  # Modal secrets injected as sandbox env
                                     # (harness LLM keys, gateway URLs)
         boxlite:                 # optional block (provider: boxlite)
           image: docker.io/me/omnigent-host:latest    # shared; default: official
           env: [OPENAI_API_KEY, GIT_TOKEN]            # shared; SERVER env var NAMES
           # exactly one mode (mutually exclusive):
           cloud: {endpoint: https://boxlite.example.com:8100}  # CLOUD; key: BOXLITE_API_KEY env
           # local: {home_dir: /data/boxlite, registry: {...}}  # LOCAL (default if omitted)
         daytona:                 # optional block (provider: daytona)
           image: docker.io/me/omnigent-host:latest  # default: official image
           env: [OPENAI_API_KEY, GIT_TOKEN]  # SERVER env var NAMES whose
                                             # values are injected as
                                             # sandbox env
           idle_minutes: 30        # optional: stop the host after this many
                                   # idle minutes and resume it in place on
                                   # the next message (the stop keeps the
                                   # workspace disk). Omit to keep the host
                                   # always-on (auto-stop disabled).
           archive_minutes: 4320   # optional (idle-suspend only): cap how
                                   # long a stopped host may sit before
                                   # Daytona archives its disk. Omit for the
                                   # provider's maximal retention so an
                                   # idle-suspended host is not archived
                                   # before its resume.
           git_sync:               # optional: bidirectional repo push-back.
                                   # Presence sets a sandbox git identity (so
                                   # an agent can commit) and widens every
                                   # clone's fetch refspec (so it can pull a
                                   # branch the other host pushed). Push/fetch
                                   # AUTH rides the host image's GIT_TOKEN
                                   # credential helper — by-reference env, no
                                   # token written to disk.
             user_name: Omni Agent #   optional; default "Omnigent Managed Host"
             user_email: a@ex.com  #   optional; default = session owner
             token_env: GIT_TOKEN  #   optional; if set, must be in env above
           context_repos:          # optional: the skills / memory-files git
                                   # bus — extra repos cloned beside the
                                   # workspace at a path the harness reads,
                                   # pushed back the same way.
             - url: https://github.com/me/skills#main
               path: .claude/skills   # workspace-relative or absolute
         islo:                    # optional block (provider: islo)
           image: docker.io/me/omnigent-host:latest  # default: official image
           env: [OPENAI_API_KEY, GIT_TOKEN]  # SERVER env var NAMES injected
                                             # as sandbox env
           base_url: https://api.islo.dev    # optional API override
           gateway_profile: default          # optional Islo gateway profile
           snapshot_name: warm-host          # optional Islo snapshot name
           workdir: /root/workspace          # optional sandbox workdir
           vcpus: 2
           memory_mb: 4096
           disk_gb: 20
         openshell:               # optional block (provider: openshell)
           image: docker.io/me/omnigent-host:latest  # default: official image
           env: [OPENAI_API_KEY, GIT_TOKEN]  # SERVER env var NAMES injected
                                             # as sandbox env
           cluster: my-gateway              # optional OpenShell gateway name

   The image defaults to the official prebaked host image
   (``ghcr.io/omnigent-ai/omnigent-host:latest``; see
   :data:`omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` and
   the per-provider env overrides), so ``provider`` + ``server_url``
   is a complete config. Provider credentials are NOT in this file
   (12-factor): the Modal launcher reads ``MODAL_TOKEN_ID`` /
   ``MODAL_TOKEN_SECRET`` (or ``~/.modal.toml``) and the Daytona
   launcher reads ``DAYTONA_API_KEY`` (plus optional
   ``DAYTONA_API_URL`` / ``DAYTONA_TARGET``), and the Islo launcher
   reads ``ISLO_API_KEY`` (plus optional ``ISLO_BASE_URL``) from the
   server process environment. The OpenShell launcher needs no API key:
   it connects to the gateway made active with ``openshell gateway
   select`` (``$OPENSHELL_GATEWAY`` / ``~/.config/openshell/active_gateway``,
   or ``sandbox.openshell.cluster``), so the server process needs
   OpenShell gateway access. ``modal``, ``daytona``, ``cwsandbox``,
   ``islo``, and ``openshell`` have managed-launch support; ``lakebox``
   parses but rejects at launch.

2. **Direct construction** (embedding deployments): build
   :class:`ManagedSandboxConfig` with a custom
   :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
   factory and pass it to ``create_app(sandbox_config=…)``::

       ManagedSandboxConfig(
           server_url=public_url,
           launcher_factory=lambda: MySandboxLauncher(...),
           token_ttl_s=7 * 24 * 3600,
       )

   A managed-only launcher implements ``prepare`` / ``provision`` /
   ``run`` / ``terminate``; the CLI-bootstrap primitives default to
   capability errors and need no overrides.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click
from fastapi import HTTPException

from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.base import ContextRepo
from omnigent.stores.host_store import Host, HostStore

if TYPE_CHECKING:
    from omnigent.onboarding.sandboxes import SandboxLauncher

_logger = logging.getLogger(__name__)

# Providers the YAML `sandbox:` section accepts. Parsing accepts all
# known providers so a deployment can stage config ahead of support
# landing, but only PROVIDERS_WITH_MANAGED_LAUNCH can actually serve a
# managed session today. (Deployments that construct
# ManagedSandboxConfig directly are not constrained by either set —
# their launcher factory IS the support.)
SUPPORTED_SANDBOX_PROVIDERS: frozenset[str] = frozenset(
    {
        "lakebox",
        "modal",
        "daytona",
        "boxlite",
        "cwsandbox",
        "islo",
        "e2b",
        "openshell",
        "kubernetes",
    }
)
PROVIDERS_WITH_MANAGED_LAUNCH: frozenset[str] = frozenset(
    {"modal", "daytona", "boxlite", "cwsandbox", "islo", "e2b", "openshell", "kubernetes"}
)

# How long a managed launch waits for the sandboxed host to register
# before declaring failure. The image is pre-baked (no pip install at
# boot), so a healthy launch registers in seconds; the budget covers a
# cold registry pull of the image on first use.
MANAGED_HOST_ONLINE_TIMEOUT_S = 120
_ONLINE_POLL_INTERVAL_S = 1.0

# Launch-token lifetime for the YAML modal path: Modal's 24h sandbox
# cap plus an hour of slack, so a live sandbox can always
# re-authenticate its tunnel across reconnects, while a token leaked
# from a long-dead sandbox cannot. Scoped to the token, not the host:
# the host row is durable, and a relaunch mints a fresh token + expiry.
# Deployments injecting their own launcher choose their own TTL on
# ManagedSandboxConfig.
MODAL_MANAGED_TOKEN_TTL_S = 25 * 3600

# Launch-token lifetime for the YAML daytona path. Daytona sandboxes
# have no platform lifetime cap (idle auto-stop is disabled at provision
# unless sandbox.daytona.idle_minutes opts into idle-suspend, whose wake
# path mints a fresh token anyway), so the bound is policy, not
# platform: 7 days keeps a
# long-lived sandbox re-authenticating across tunnel reconnects while
# still expiring tokens of sandboxes nobody deleted. A relaunch (or a
# session past 7 days going through the dead-host relaunch path) mints
# a fresh token.
DAYTONA_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for the YAML boxlite path. Boxlite boxes have no
# platform lifetime cap and persist across restarts, so the bound is policy,
# not platform: 7 days mirrors Daytona — long enough for a live box to
# re-authenticate its tunnel across reconnects while still expiring tokens of
# boxes nobody removed. A relaunch mints a fresh token.
BOXLITE_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for the YAML islo path. Islo sandboxes are
# deleted by managed-session teardown; use the same 7-day policy bound
# as Daytona for long-lived hosts and stale-token cleanup.
ISLO_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for the YAML openshell path. OpenShell sandboxes
# run until deleted (no platform lifetime cap), so the bound is policy,
# not platform: the same 7-day window as Daytona/Islo keeps a long-lived
# sandbox re-authenticating across tunnel reconnects while still expiring
# tokens of sandboxes nobody deleted. A relaunch mints a fresh token.
OPENSHELL_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for the YAML kubernetes path. Runner Pods have no
# platform lifetime cap (they run until deleted by managed-session teardown),
# so the bound is policy, not platform: the same 7-day window as
# Daytona/Islo/OpenShell keeps a long-lived host re-authenticating across tunnel
# reconnects while still expiring tokens of Pods nobody deleted. A relaunch
# mints a fresh token (and the per-Pod token Secret is replaced).
KUBERNETES_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# The cwsandbox launch-token TTL is NOT a constant: CW Sandbox's lifetime is
# operator-overridable (OMNIGENT_CWSANDBOX_MAX_LIFETIME_S), so the TTL is
# derived from the resolved lifetime at parse time via
# cwsandbox.managed_token_ttl_s() — always above the cap, so a live sandbox
# can re-authenticate its tunnel across reconnects while a leaked token can't.

# Where the in-sandbox host process logs — named in launch-failure
# errors so an operator knows where to look inside the sandbox.
_HOST_LOG_PATH = "/tmp/omnigent-host.log"

# How long a message POST waits for an in-flight managed launch to
# settle before giving up (see ManagedLaunchTracker). Covers the full
# launch/wake pipeline ON TOP OF the host-registration wait
# (MANAGED_HOST_ONLINE_TIMEOUT_S): the provider's provision/resume call
# (StartSandbox has no fixed upper bound), the host-tunnel reconnect on
# this replica, and the runner spawn/connect. The 120s slack must cover
# all of those so a slow cold launch/wake doesn't time the parked message
# out before the background launch settles — otherwise the first
# post-dormancy turn is lost even though the wake later succeeds. The wait
# resolves as soon as the launch settles, so this bound only bites a
# genuinely slow launch.
MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S = MANAGED_HOST_ONLINE_TIMEOUT_S + 120

# Session label recording the repository-URL workspace a managed
# session was created with (the raw ``<url>[#<branch>]`` request
# value). ``conversations.workspace`` is overwritten with the CLONED
# path at bind time, so this label is what a sandbox RELAUNCH parses
# to re-clone the repository into the fresh generation's workspace.
MANAGED_REPO_LABEL_KEY = "omnigent.sandbox.repo"


@dataclass
class ManagedLaunch:
    """
    One session's in-flight (or failed) managed-host launch.

    Created by :meth:`ManagedLaunchTracker.begin` when
    ``POST /v1/sessions`` schedules the background launch, and settled
    by the background task via :meth:`ManagedLaunchTracker.finish` /
    :meth:`ManagedLaunchTracker.fail`.

    :param settled: Set once the launch reaches a terminal state —
        either success (host bound, runner launched) or failure.
        Waiters (a message POST racing the provision) block on this.
    :param error: Failure detail once settled unsuccessfully, e.g.
        ``"managed sandbox launch failed: …"``. ``None`` while
        in flight and on success.
    """

    settled: asyncio.Event
    error: str | None = None


class ManagedLaunchTracker:
    """
    In-memory index of managed-host launches keyed by session id.

    ``POST /v1/sessions`` with ``host_type="managed"`` returns before
    the sandbox exists; this tracker is how the rest of the server
    observes that window. A message POST that arrives mid-provision
    waits on the session's :class:`ManagedLaunch` instead of failing
    with "no runner bound"; a launch failure is recorded here so the
    waiting POST (and any later one) reports the real reason.

    Successful launches are removed on settle — from then on the
    session looks like any host-bound session. Failed launches are
    retained (the session row never got a host, so the recorded error
    is the only trace of why) until the process restarts or a new
    launch for the same session begins.
    """

    def __init__(self) -> None:
        """Initialize the empty session-id → launch index."""
        self._by_session: dict[str, ManagedLaunch] = {}

    def begin(self, session_id: str) -> None:
        """
        Register a new in-flight launch for *session_id*.

        Replaces any prior entry (e.g. a retained failure from an
        earlier attempt).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        self._by_session[session_id] = ManagedLaunch(settled=asyncio.Event())

    def get(self, session_id: str) -> ManagedLaunch | None:
        """
        Look up the launch state for *session_id*.

        :param session_id: Session/conversation identifier.
        :returns: The launch entry, or ``None`` when no managed launch
            is in flight or recorded as failed for this session.
        """
        return self._by_session.get(session_id)

    def finish(self, session_id: str) -> None:
        """
        Settle *session_id*'s launch as successful and forget it.

        Waiters holding the entry observe ``settled`` with
        ``error is None``; later readers find no entry and take the
        normal host-bound paths.

        :param session_id: Session/conversation identifier.
        """
        entry = self._by_session.pop(session_id, None)
        if entry is not None:
            entry.settled.set()

    def fail(self, session_id: str, error: str) -> None:
        """
        Settle *session_id*'s launch as failed, retaining the reason.

        :param session_id: Session/conversation identifier.
        :param error: Human-readable failure detail, e.g.
            ``"managed sandbox launch failed: spend limit reached"``.
        """
        entry = self._by_session.get(session_id)
        if entry is None:
            return
        entry.error = error
        entry.settled.set()


@dataclass
class ManagedSandboxConfig:
    """
    Everything the managed-host flow needs from a deployment.

    Built by :func:`parse_sandbox_config` from the server YAML, or
    constructed directly by embedding deployments to inject a custom
    launcher (see the module docstring).

    :param server_url: Public URL of THIS server that the sandboxed
        host dials back to, e.g. ``"https://omnigent.example.com"``
        (no trailing slash). Explicit — the server cannot reliably
        infer its own public URL behind proxies.
    :param launcher_factory: Zero-argument factory producing the
        :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
        each launch uses, e.g.
        ``lambda: ModalSandboxLauncher(image=…)``. Called per launch
        (launchers may cache provider handles internally). May raise
        ``HTTPException`` to report an unusable backend — the YAML
        path uses this for providers without managed support.
    :param token_ttl_s: Launch-token lifetime in seconds, e.g.
        ``90000`` (25h) for Modal. Must comfortably exceed the
        provider's maximum sandbox lifetime so a live sandbox can
        always re-authenticate its tunnel across reconnects.
    :param managed_launch_supported: Whether ``launcher_factory`` can
        actually serve a managed launch. The YAML path sets this from
        :data:`PROVIDERS_WITH_MANAGED_LAUNCH` — staged providers
        (``lakebox``) parse but get ``False``, since their factory
        rejects at launch. Defaults to ``True`` for
        directly-constructed configs (an embedding deployment's
        custom factory IS the support). Drives the unauthenticated
        ``managed_sandboxes_enabled`` capability flag on
        ``GET /v1/info``, which gates the web UI's sandbox option.
    :param provider: Short provider name surfaced to the web UI so the
        new-session sandbox option can be labeled per provider (e.g.
        ``"modal"`` → "Modal Sandbox", ``"lakebox"`` → "Databricks
        Sandbox"). The YAML path sets it from the parsed
        ``sandbox.provider``. ``None`` for directly-constructed
        embedding configs that don't name a provider — the UI then
        falls back to the generic "New Sandbox" label. Exposed (when
        managed launch is supported) on the unauthenticated
        ``GET /v1/info`` as ``sandbox_provider``.
    :param git_user_name: ``git config user.name`` for in-sandbox commits
        when bidirectional git sync is enabled (the ``sandbox.daytona.git_sync``
        block), or ``None`` to leave git identity unset. Without it an agent
        cannot ``git commit`` to push work back.
    :param git_user_email: ``git config user.email`` for in-sandbox commits,
        or ``None`` to default to the session owner's address at launch.
    :param context_repos: Extra repositories cloned into the sandbox beside
        the primary workspace — the skills / memory-files git bus
        (``sandbox.daytona.context_repos``). Empty when not configured.
    """

    server_url: str
    launcher_factory: Callable[[], SandboxLauncher]
    token_ttl_s: int
    managed_launch_supported: bool = True
    provider: str | None = None
    git_user_name: str | None = None
    git_user_email: str | None = None
    context_repos: tuple[ContextRepo, ...] = ()


@dataclass
class ManagedHostLaunch:
    """
    Result of a successful managed host launch.

    :param host_id: The registered host's identifier, e.g.
        ``"host_a1b2c3d4..."`` — feed this to the same launch-runner
        path an external ``host_id`` takes.
    :param workspace: Absolute workspace path created inside the
        sandbox, e.g. ``"/root/workspace"`` — or the cloned repository
        directory (e.g. ``"/root/workspace/myrepo"``) when the session
        requested a repository-URL workspace.
    """

    host_id: str
    workspace: str


@dataclass
class RepoWorkspace:
    """
    Parsed repository-URL workspace for a managed session.

    A managed create's ``workspace`` is a git repository URL with an
    optional ``#<branch>`` fragment (Docker build-context style): the
    URL fully describes what the server materializes inside the
    sandbox. Built by :func:`parse_repo_workspace` — construct via the
    parser, not directly, so every field has been validated.

    :param url: The clone URL with any fragment stripped, e.g.
        ``"https://github.com/org/repo.git"`` or
        ``"git@github.com:org/repo.git"``.
    :param branch: Branch to clone (``--branch … --single-branch``),
        e.g. ``"release-1.2"``, or ``None`` for the default branch.
    :param repo_name: Directory name the clone lands in under the
        sandbox workspace, derived from the URL's last path segment
        with ``.git`` stripped, e.g. ``"repo"``.
    """

    url: str
    branch: str | None
    repo_name: str


# A full 40-hex object id — rejected as a clone fragment: cloning a
# commit lands the agent on a detached HEAD it cannot push from.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Directory names a repo URL may resolve to. Conservative on purpose:
# the name is interpolated into an in-sandbox shell path.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Characters git forbids in ref names (plus ``#``, which can never
# reach the fragment since the workspace splits on its FIRST ``#`` —
# a second ``#`` means the branch itself contains one, which the
# fragment form does not support).
_BRANCH_FORBIDDEN_CHARS = set(" \t~^:?*[\\#")


def is_repo_workspace(workspace: str) -> bool:
    """
    Return whether *workspace* is a repository-URL workspace.

    Used by the create-session schema to tell the managed form (a git
    URL) apart from the external form (an absolute host path) without
    fully parsing it.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo"`` or ``"/Users/me/repo"``.
    :returns: ``True`` for the ``https://`` / ``git@`` URL forms.
    """
    return workspace.startswith(("https://", "git@"))


def _validate_clone_branch(fragment: str) -> str:
    """
    Validate a ``#<branch>`` fragment as a clonable branch name.

    :param fragment: The fragment text after the first ``#``, e.g.
        ``"release-1.2"``.
    :returns: The validated branch name, unchanged.
    :raises ValueError: When the fragment is empty, is a commit SHA
        (detached HEAD — pin commits via git worktree options
        instead), or violates git ref-name rules.
    """
    if not fragment:
        raise ValueError("the '#' fragment must name a branch, e.g. '#main'")
    if _COMMIT_SHA_RE.fullmatch(fragment):
        raise ValueError(
            "the '#' fragment must be a branch, not a commit SHA — a commit "
            "checkout would leave the agent on a detached HEAD it cannot push"
        )
    if (
        any(c in _BRANCH_FORBIDDEN_CHARS or ord(c) < 0x20 for c in fragment)
        or fragment.startswith(("-", "/"))
        or fragment.endswith(("/", "."))
        or ".." in fragment
        or "@{" in fragment
    ):
        raise ValueError(f"'{fragment}' is not a valid git branch name")
    return fragment


def _derive_repo_name(url: str) -> str:
    """
    Derive the clone directory name from a repository URL.

    :param url: The fragment-stripped clone URL, e.g.
        ``"https://github.com/org/repo.git"``.
    :returns: The last path segment with ``.git`` stripped, e.g.
        ``"repo"``.
    :raises ValueError: When no usable name can be derived (empty
        path, or a name that is not filesystem-safe).
    """
    last = url.rstrip("/").split("/")[-1]
    # scp-style URLs with a single-segment path ("git@host:repo.git")
    # have no "/" after the colon — take what follows it.
    if ":" in last:
        last = last.rsplit(":", 1)[-1]
    name = last[: -len(".git")] if last.endswith(".git") else last
    if not name or name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        raise ValueError(
            f"could not derive a repository directory name from '{url}' — "
            "the URL must end in the repository name, e.g. "
            "'https://github.com/org/repo'"
        )
    return name


def parse_repo_workspace(workspace: str) -> RepoWorkspace:
    """
    Parse and validate a managed session's repository-URL workspace.

    Grammar (Docker build-context style)::

        <repo>[#<branch>]
        <repo> := https://<host>/<path>  |  git@<host>:<path>

    The fragment splits on the FIRST ``#``; branches containing ``#``
    are not supported in this form. Fails loud on anything malformed
    so a bad workspace 422s at validation instead of surfacing as a
    mid-provision clone error.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo#release-1.2"``.
    :returns: The parsed, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL or branch fragment is malformed.
    """
    url, sep, fragment = workspace.partition("#")
    if any(ch.isspace() for ch in workspace):
        raise ValueError("a repository workspace must not contain whitespace")
    if url.startswith("https://"):
        host, slash, path = url[len("https://") :].partition("/")
        if not host or not slash or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable https repository URL — expected "
                "'https://<host>/<org>/<repo>'"
            )
    elif url.startswith("git@"):
        host, colon, path = url[len("git@") :].partition(":")
        if not host or not colon or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<org>/<repo>'"
            )
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<org>/<repo>' or 'git@<host>:<org>/<repo>'"
        )
    branch = _validate_clone_branch(fragment) if sep else None
    return RepoWorkspace(url=url, branch=branch, repo_name=_derive_repo_name(url))


def _modal_launcher_factory(
    image: str | None,
    secrets: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: modal`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        use the official prebaked host image (env-overridable; see
        :func:`omnigent.onboarding.sandboxes.modal._build_sandbox_image`).
    :param secrets: Modal secret names whose env vars (harness LLM
        credentials, gateway URLs) are injected into every sandbox,
        e.g. ``["omnigent-llm"]``, or ``None`` to resolve from the
        launcher's env-var fallback / inject nothing.
    :returns: A factory producing parameterized Modal launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Modal launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.modal import ModalSandboxLauncher

        return ModalSandboxLauncher(image=image, secrets=secrets)

    return _build


def _unsupported_launcher_factory(provider: str) -> Callable[[], SandboxLauncher]:
    """
    Build a factory that rejects launch for a not-yet-supported provider.

    Lets a deployment stage ``sandbox:`` config for a provider before
    managed-launch support lands: parsing succeeds, and the clear 400
    only surfaces if a managed session is actually requested.

    :param provider: The configured provider name, e.g. ``"daytona"``.
    :returns: A factory that raises ``HTTPException`` 400 when called.
    """

    def _reject() -> SandboxLauncher:
        """Reject the launch with the provider named."""
        raise HTTPException(
            status_code=400,
            detail=(
                f"managed hosts are not yet supported for the "
                f"'{provider}' sandbox provider — only "
                f"{', '.join(sorted(PROVIDERS_WITH_MANAGED_LAUNCH))} is implemented"
            ),
        )

    return _reject


def parse_sandbox_config(raw: object) -> ManagedSandboxConfig | None:
    """
    Parse and validate the server config's ``sandbox:`` section.

    Fails loud on malformed config (an operator typo should stop server
    startup, not surface as a runtime 502 on the first managed session).

    :param raw: The raw ``sandbox`` value from the server config YAML,
        e.g. ``{"provider": "modal", "server_url": "https://…",
        "modal": {"image": "docker.io/me/omnigent-host:latest"}}``.
        ``None`` when the section is absent.
    :returns: The parsed config, or ``None`` when *raw* is ``None``
        (managed hosts not configured).
    :raises ValueError: When the section is present but malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("server config 'sandbox' must be a mapping")
    provider = raw.get("provider")
    if provider not in SUPPORTED_SANDBOX_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_SANDBOX_PROVIDERS))
        raise ValueError(
            f"server config 'sandbox.provider' must be one of: {supported} (got {provider!r})"
        )
    server_url = raw.get("server_url")
    if not isinstance(server_url, str) or not server_url.strip():
        raise ValueError(
            "server config 'sandbox.server_url' is required — the public URL "
            "of this server that sandboxed hosts connect back to"
        )
    # Bidirectional git-sync surface (Daytona only today). Defaults keep every
    # other provider's config — and a Daytona config without the git_sync /
    # context_repos blocks — behaving exactly as before.
    git_user_name: str | None = None
    git_user_email: str | None = None
    context_repos: tuple[ContextRepo, ...] = ()
    if provider == "modal":
        launcher_factory = _modal_launcher_factory(
            _parse_modal_image(raw), _parse_modal_secrets(raw)
        )
        token_ttl_s = MODAL_MANAGED_TOKEN_TTL_S
    elif provider == "daytona":
        daytona_env = _parse_daytona_env(raw)
        launcher_factory = _daytona_launcher_factory(
            _parse_daytona_image(raw),
            daytona_env,
            _parse_provider_positive_int(raw, "daytona", "idle_minutes"),
            _parse_provider_positive_int(raw, "daytona", "archive_minutes"),
        )
        token_ttl_s = DAYTONA_MANAGED_TOKEN_TTL_S
        git_user_name, git_user_email = _parse_daytona_git_sync(raw, daytona_env)
        context_repos = _parse_daytona_context_repos(raw)
    elif provider == "boxlite":
        section = _boxlite_section(raw)
        _reject_unknown_boxlite_keys(
            section, {"image", "env", "local", "cloud"}, "sandbox.boxlite"
        )
        endpoint, home_dir, registry = _parse_boxlite_mode(section)
        launcher_factory = _boxlite_launcher_factory(
            endpoint,
            _parse_boxlite_image(section),
            _parse_boxlite_env(section),
            home_dir,
            registry,
        )
        token_ttl_s = BOXLITE_MANAGED_TOKEN_TTL_S
    elif provider == "cwsandbox":
        from omnigent.onboarding.sandboxes.cwsandbox import managed_token_ttl_s

        launcher_factory = _cwsandbox_launcher_factory(
            _parse_cwsandbox_image(raw), _parse_cwsandbox_env(raw)
        )
        # Derived from OMNIGENT_CWSANDBOX_MAX_LIFETIME_S so the token always
        # outlives the (operator-overridable) sandbox lifetime.
        token_ttl_s = managed_token_ttl_s()
    elif provider == "islo":
        launcher_factory = _islo_launcher_factory(
            image=_parse_provider_image(raw, "islo"),
            env=_parse_provider_env(raw, "islo"),
            base_url=_parse_provider_string(raw, "islo", "base_url"),
            gateway_profile=_parse_provider_string(raw, "islo", "gateway_profile"),
            snapshot_name=_parse_provider_string(raw, "islo", "snapshot_name"),
            workdir=_parse_provider_string(raw, "islo", "workdir"),
            vcpus=_parse_provider_positive_int(raw, "islo", "vcpus"),
            memory_mb=_parse_provider_positive_int(raw, "islo", "memory_mb"),
            disk_gb=_parse_provider_positive_int(raw, "islo", "disk_gb"),
        )
        token_ttl_s = ISLO_MANAGED_TOKEN_TTL_S
    elif provider == "e2b":
        from omnigent.onboarding.sandboxes.e2b import managed_token_ttl_s

        launcher_factory = _e2b_launcher_factory(
            _parse_e2b_template(raw), _parse_provider_env(raw, "e2b")
        )
        # Derived from OMNIGENT_E2B_MAX_LIFETIME_S so the token always
        # outlives the (operator-overridable) sandbox lifetime — mirrors
        # the cwsandbox path.
        token_ttl_s = managed_token_ttl_s()
    elif provider == "openshell":
        launcher_factory = _openshell_launcher_factory(
            image=_parse_provider_image(raw, "openshell"),
            env=_parse_provider_env(raw, "openshell"),
            cluster=_parse_provider_string(raw, "openshell", "cluster"),
        )
        token_ttl_s = OPENSHELL_MANAGED_TOKEN_TTL_S
    elif provider == "kubernetes":
        launcher_factory = _kubernetes_launcher_factory(
            image=_parse_provider_image(raw, "kubernetes"),
            env=_parse_provider_env(raw, "kubernetes"),
            namespace=_parse_provider_string(raw, "kubernetes", "namespace"),
            secret_name=_parse_provider_string(raw, "kubernetes", "secret_name"),
            service_account=_parse_provider_string(raw, "kubernetes", "service_account"),
            node_selector=_parse_provider_str_mapping(raw, "kubernetes", "node_selector"),
            kubeconfig=_parse_provider_string(raw, "kubernetes", "kubeconfig"),
            in_cluster=_parse_provider_bool(raw, "kubernetes", "in_cluster"),
            resources=_parse_kubernetes_resources(raw),
        )
        token_ttl_s = KUBERNETES_MANAGED_TOKEN_TTL_S
    else:
        launcher_factory = _unsupported_launcher_factory(provider)
        # Never consulted (the factory rejects before any token is
        # minted); the conservative modal TTL keeps the field total.
        token_ttl_s = MODAL_MANAGED_TOKEN_TTL_S
    return ManagedSandboxConfig(
        server_url=server_url.strip().rstrip("/"),
        launcher_factory=launcher_factory,
        token_ttl_s=token_ttl_s,
        managed_launch_supported=provider in PROVIDERS_WITH_MANAGED_LAUNCH,
        provider=provider,
        git_user_name=git_user_name,
        git_user_email=git_user_email,
        context_repos=context_repos,
    )


def _parse_modal_image(raw: dict[str, object]) -> str | None:
    """
    Extract and validate the modal image from the raw ``sandbox`` dict.

    The ``modal`` section and its ``image`` field are OPTIONAL — when
    absent, sandboxes boot from the official prebaked host image
    (env-overridable; see
    :func:`omnigent.onboarding.sandboxes.modal._build_sandbox_image`).
    A present-but-malformed value still fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"modal"``).
    :returns: The validated image reference, or ``None`` to use the
        official default.
    :raises ValueError: When ``sandbox.modal`` is present but not a
        mapping, or ``sandbox.modal.image`` is present but not a
        non-empty string.
    """
    modal_raw = raw.get("modal")
    if modal_raw is None:
        return None
    if not isinstance(modal_raw, dict):
        raise ValueError("server config 'sandbox.modal' must be a mapping")
    image = modal_raw.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.modal.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the "
            "official image)"
        )
    return image.strip()


def _parse_modal_secrets(raw: dict[str, object]) -> list[str] | None:
    """
    Extract and validate the modal secret names from the ``sandbox`` dict.

    ``sandbox.modal.secrets`` names the Modal secrets whose env vars
    (harness LLM credentials, gateway base URLs) are injected into
    every managed sandbox. OPTIONAL — absent means the launcher's
    env-var fallback applies (or nothing is injected). A
    present-but-malformed value fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"modal"``).
    :returns: The validated secret names, e.g. ``["omnigent-llm"]``,
        or ``None`` when not configured.
    :raises ValueError: When ``sandbox.modal`` is present but not a
        mapping, or ``sandbox.modal.secrets`` is present but not a
        list of non-empty strings.
    """
    modal_raw = raw.get("modal")
    if modal_raw is None:
        return None
    if not isinstance(modal_raw, dict):
        raise ValueError("server config 'sandbox.modal' must be a mapping")
    secrets = modal_raw.get("secrets")
    if secrets is None:
        return None
    if not isinstance(secrets, list) or not all(
        isinstance(name, str) and name.strip() for name in secrets
    ):
        raise ValueError(
            "server config 'sandbox.modal.secrets' must be a list of Modal "
            "secret names, e.g. ['omnigent-llm']"
        )
    return [name.strip() for name in secrets]


def _daytona_launcher_factory(
    image: str | None,
    env: list[str] | None,
    idle_minutes: int | None,
    archive_minutes: int | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: daytona`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        use the official prebaked host image (env-overridable; see
        :class:`omnigent.onboarding.sandboxes.daytona.DaytonaSandboxLauncher`).
    :param env: Names of server-process environment variables (harness
        LLM credentials, gateway URLs, ``GIT_TOKEN``) injected into
        every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or
        ``None`` to resolve from the launcher's env-var fallback /
        inject nothing.
    :param idle_minutes: Idle auto-stop interval in minutes — the host
        idle-suspends after this long and the server's wake path resumes
        it in place — or ``None`` to keep the always-on default
        (auto-stop disabled), so existing deployments do not regress.
    :param archive_minutes: Auto-archive ceiling in minutes for an
        idle-suspended (stopped) host — how long it may stay stopped
        before Daytona archives its disk — or ``None`` to use the
        provider's maximal retention so an idle-suspended host is
        effectively never archived before its resume. Only meaningful
        alongside ``idle_minutes``.
    :returns: A factory producing parameterized Daytona launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Daytona launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.daytona import DaytonaSandboxLauncher

        return DaytonaSandboxLauncher(
            image=image,
            env=env,
            idle_minutes=idle_minutes,
            archive_minutes=archive_minutes,
        )

    return _build


def _parse_daytona_image(raw: dict[str, object]) -> str | None:
    """
    Extract and validate the daytona image from the ``sandbox`` dict.

    The ``daytona`` section and its ``image`` field are OPTIONAL —
    when absent, sandboxes boot from the official prebaked host image
    (env-overridable; see
    :mod:`omnigent.onboarding.sandboxes.daytona`). A
    present-but-malformed value still fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"daytona"``).
    :returns: The validated image reference, or ``None`` to use the
        official default.
    :raises ValueError: When ``sandbox.daytona`` is present but not a
        mapping, or ``sandbox.daytona.image`` is present but not a
        non-empty string.
    """
    daytona_raw = raw.get("daytona")
    if daytona_raw is None:
        return None
    if not isinstance(daytona_raw, dict):
        raise ValueError("server config 'sandbox.daytona' must be a mapping")
    image = daytona_raw.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.daytona.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the "
            "official image)"
        )
    return image.strip()


def _parse_daytona_env(raw: dict[str, object]) -> list[str] | None:
    """
    Extract and validate the daytona env names from the ``sandbox`` dict.

    ``sandbox.daytona.env`` names the SERVER-process environment
    variables whose values (harness LLM credentials, gateway base
    URLs, ``GIT_TOKEN``) are injected into every managed sandbox —
    names only, so secret values never live in the config file.
    OPTIONAL — absent means the launcher's env-var fallback applies
    (or nothing is injected). A present-but-malformed value fails
    loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"daytona"``).
    :returns: The validated env var names, e.g.
        ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or ``None`` when not
        configured.
    :raises ValueError: When ``sandbox.daytona`` is present but not a
        mapping, or ``sandbox.daytona.env`` is present but not a list
        of non-empty strings.
    """
    daytona_raw = raw.get("daytona")
    if daytona_raw is None:
        return None
    if not isinstance(daytona_raw, dict):
        raise ValueError("server config 'sandbox.daytona' must be a mapping")
    env = daytona_raw.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            "server config 'sandbox.daytona.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['OPENAI_API_KEY', "
            "'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


# Default in-sandbox commit identity name when bidirectional git sync is
# enabled but the operator didn't set ``git_sync.user_name`` — git needs SOME
# ``user.name`` or every agent ``git commit`` aborts with "Author identity
# unknown". The email defaults to the session owner at launch (see
# :func:`_arm_and_start_host`), which is the more meaningful per-session value.
DEFAULT_MANAGED_GIT_USER_NAME = "Omnigent Managed Host"


def _optional_str_field(mapping: dict[str, object], key: str, path: str) -> str | None:
    """
    Read an OPTIONAL non-empty string field from a config mapping.

    :param mapping: The enclosing config mapping.
    :param key: The field name within *mapping*.
    :param path: Dotted config path for the error message, e.g.
        ``"sandbox.daytona.git_sync.user_name"``.
    :returns: The stripped value, or ``None`` when the key is absent.
    :raises ValueError: When present but not a non-empty string.
    """
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"server config '{path}' must be a non-empty string")
    return value.strip()


def _parse_daytona_git_sync(
    raw: dict[str, object], env: list[str] | None
) -> tuple[str | None, str | None]:
    """
    Parse the OPTIONAL ``sandbox.daytona.git_sync`` block.

    Presence of the block opts a managed Daytona host into bidirectional
    git sync: the launch sets a git identity in the sandbox so an agent can
    commit, and widens every clone's fetch refspec so it can pull branches
    the other host pushed. Auth for clone / push / fetch rides the host
    image's GIT_TOKEN credential helper (by-reference env, never a token on
    disk), so the optional ``token_env`` here only NAMES the env var that
    must be forwarded — validated against ``sandbox.daytona.env`` so a
    misconfiguration fails at startup, not as an opaque mid-clone auth error.

    Shape::

        git_sync:
          user_name: Omnigent Agent      # optional; default "Omnigent Managed Host"
          user_email: agent@example.com  # optional; default = session owner
          token_env: GIT_TOKEN           # optional; must appear in daytona.env

    :param raw: The raw ``sandbox`` mapping (provider known to be daytona).
    :param env: The parsed ``sandbox.daytona.env`` names, for ``token_env``
        validation, or ``None`` when no env passthrough is configured.
    :returns: ``(user_name, user_email)`` — ``user_name`` defaulted to
        :data:`DEFAULT_MANAGED_GIT_USER_NAME` when the block is present but
        unset; both ``None`` when the block is absent (git sync off).
    :raises ValueError: When the block or a field is malformed, or
        ``token_env`` is not present in ``sandbox.daytona.env``.
    """
    daytona_raw = raw.get("daytona")
    if not isinstance(daytona_raw, dict):
        return None, None
    git_sync = daytona_raw.get("git_sync")
    if git_sync is None:
        return None, None
    if not isinstance(git_sync, dict):
        raise ValueError("server config 'sandbox.daytona.git_sync' must be a mapping")
    allowed = {"user_name", "user_email", "token_env"}
    unknown = sorted(set(git_sync) - allowed)
    if unknown:
        raise ValueError(
            f"server config 'sandbox.daytona.git_sync' has unknown key(s): "
            f"{', '.join(unknown)} (allowed: {', '.join(sorted(allowed))})"
        )
    user_name = _optional_str_field(git_sync, "user_name", "sandbox.daytona.git_sync.user_name")
    user_email = _optional_str_field(git_sync, "user_email", "sandbox.daytona.git_sync.user_email")
    token_env = _optional_str_field(git_sync, "token_env", "sandbox.daytona.git_sync.token_env")
    if token_env is not None and token_env not in (env or []):
        raise ValueError(
            f"server config 'sandbox.daytona.git_sync.token_env' names "
            f"'{token_env}' but it is not in 'sandbox.daytona.env' — add it so "
            "the token is forwarded into the sandbox by reference (the host "
            "image's git credential helper reads it for private-repo "
            "clone / push / fetch)"
        )
    return (user_name or DEFAULT_MANAGED_GIT_USER_NAME), user_email


def _validate_context_path(path: str, index: int) -> str:
    """
    Validate a context-repo destination path (absolute or workspace-relative).

    The path is interpolated into an in-sandbox shell command (``shlex``-quoted
    there, so shell-safe), but a ``..`` segment could still escape the workspace
    — reject those and any whitespace so a typo fails loud at config parse.

    :param path: The raw ``path`` value, e.g. ``".claude/skills"`` or
        ``"/root/.claude/skills"``.
    :param index: The repo's index in the list, for the error message.
    :returns: The path, unchanged.
    :raises ValueError: When the path contains whitespace or a ``..`` segment.
    """
    if any(ch.isspace() for ch in path) or ".." in path.split("/"):
        raise ValueError(
            f"server config 'sandbox.daytona.context_repos[{index}].path' "
            f"('{path}') must be a clean absolute or workspace-relative path "
            "with no whitespace or '..' segments"
        )
    return path


def _parse_daytona_context_repos(raw: dict[str, object]) -> tuple[ContextRepo, ...]:
    """
    Parse the OPTIONAL ``sandbox.daytona.context_repos`` list.

    Each entry designates a git repository (the skills / memory-files bus)
    cloned into the sandbox at a path the harness reads, with the same
    clone-at-branch + all-branches-refspec + credential-helper-auth machinery
    as the primary workspace repo, so edits round-trip via commit / push / pull.

    Shape::

        context_repos:
          - url: https://github.com/me/skills#main   # <repo>[#<branch>]
            path: .claude/skills                       # workspace-relative or absolute

    :param raw: The raw ``sandbox`` mapping (provider known to be daytona).
    :returns: The validated context repos, empty when none configured.
    :raises ValueError: When the list or any entry is malformed.
    """
    daytona_raw = raw.get("daytona")
    if not isinstance(daytona_raw, dict):
        return ()
    items = daytona_raw.get("context_repos")
    if items is None:
        return ()
    if not isinstance(items, list) or not items:
        raise ValueError(
            "server config 'sandbox.daytona.context_repos' must be a non-empty "
            "list of {url, path} mappings"
        )
    repos: list[ContextRepo] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(
                f"server config 'sandbox.daytona.context_repos[{index}]' must be "
                "a mapping with 'url' and 'path'"
            )
        unknown = sorted(set(item) - {"url", "path"})
        if unknown:
            raise ValueError(
                f"server config 'sandbox.daytona.context_repos[{index}]' has "
                f"unknown key(s): {', '.join(unknown)} (allowed: path, url)"
            )
        url = item.get("url")
        path = item.get("path")
        if not isinstance(url, str) or not url.strip():
            raise ValueError(
                f"server config 'sandbox.daytona.context_repos[{index}].url' must "
                "be a non-empty repository URL, e.g. "
                "'https://github.com/me/skills#main'"
            )
        if not isinstance(path, str) or not path.strip():
            raise ValueError(
                f"server config 'sandbox.daytona.context_repos[{index}].path' must "
                "be a non-empty path the harness reads, e.g. '.claude/skills'"
            )
        # Reuse the primary-workspace URL grammar (validates the URL form and any
        # '#branch' fragment, rejecting commit SHAs that would detach HEAD).
        parsed = parse_repo_workspace(url.strip())
        repos.append(
            ContextRepo(
                url=parsed.url,
                branch=parsed.branch,
                dest=_validate_context_path(path.strip(), index),
            )
        )
    return tuple(repos)


def _boxlite_launcher_factory(
    endpoint: str | None,
    image: str | None,
    env: list[str] | None,
    home_dir: str | None,
    registry: dict[str, object] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: boxlite`` path.

    :param endpoint: Remote ``boxlite serve`` URL (cloud mode), or ``None`` for
        LOCAL mode — boxes run on the omnigent-server host as embedded micro-VMs
        (no daemon, no ``boxlite serve``).
    :param image: Registry image reference with omnigent pre-installed, or
        ``None`` to use the official prebaked host image (env-overridable; see
        :class:`omnigent.onboarding.sandboxes.boxlite.BoxliteSandboxLauncher`).
    :param env: Names of server-process environment variables (harness LLM
        credentials, gateway URLs, ``GIT_TOKEN``) injected into every box, e.g.
        ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or ``None``.
    :param home_dir: LOCAL-mode boxlite data directory, or ``None`` for the
        default (``~/.boxlite``).
    :param registry: LOCAL-mode private-registry config for the host image
        (``host`` + optional ``transport`` / ``skip_verify`` / ``*_env``
        credential names), or ``None`` for anonymous pulls.
    :returns: A factory producing parameterized boxlite launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the boxlite launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.boxlite import BoxliteSandboxLauncher

        return BoxliteSandboxLauncher(
            endpoint=endpoint, image=image, env=env, home_dir=home_dir, registry=registry
        )

    return _build


def _boxlite_section(raw: dict[str, object]) -> dict[str, object]:
    """
    Return the validated ``sandbox.boxlite`` mapping (empty when absent).

    :raises ValueError: When ``sandbox.boxlite`` is present but not a mapping.
    """
    section = raw.get("boxlite")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ValueError("server config 'sandbox.boxlite' must be a mapping")
    return section


def _reject_unknown_boxlite_keys(mapping: dict[str, object], allowed: set[str], path: str) -> None:
    """
    Fail loud on any key outside *allowed* — catches typos and misplaced keys
    (e.g. ``endpoint`` at the section level instead of under ``cloud:``, or a
    misspelled ``passwrod_env``) that would otherwise be silently ignored and
    surface much later as a confusing runtime failure.
    """
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ValueError(
            f"server config '{path}' has unknown key(s): {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _parse_boxlite_mode(
    section: dict[str, object],
) -> tuple[str | None, str | None, dict[str, object] | None]:
    """
    Resolve the boxlite runtime MODE from the mutually-exclusive ``local`` /
    ``cloud`` sub-blocks and return the launcher's ``(endpoint, home_dir,
    registry)``.

    - ``cloud:`` present → CLOUD mode (a remote ``boxlite serve``).
      ``cloud.endpoint`` is required; the API key is read from
      ``BOXLITE_API_KEY`` in the server env (12-factor, not config).
    - else → LOCAL mode (embedded micro-VMs on the server host). The optional
      ``local:`` block carries ``home_dir`` / ``registry``.

    Setting both ``local`` and ``cloud`` is rejected — they are two different
    configurations and a session runs in exactly one mode.

    :returns: ``(endpoint, home_dir, registry)`` — only *endpoint* (cloud) or
        the *home_dir*/*registry* pair (local) is ever populated.
    :raises ValueError: On a malformed or ambiguous mode config.
    """
    # Test for KEY PRESENCE, not value: a bare `cloud:`/`local:` YAML key
    # parses to None, which must be rejected as malformed — not silently
    # fall through to LOCAL mode (a `cloud:` typo would then run locally).
    local_present = "local" in section
    cloud_present = "cloud" in section
    local_block = section.get("local")
    cloud_block = section.get("cloud")
    if local_present and cloud_present:
        raise ValueError(
            "server config 'sandbox.boxlite' must set at most one of 'local' or "
            "'cloud' — the two modes are mutually exclusive"
        )
    if cloud_present:
        if not isinstance(cloud_block, dict):
            raise ValueError("server config 'sandbox.boxlite.cloud' must be a mapping")
        _reject_unknown_boxlite_keys(cloud_block, {"endpoint"}, "sandbox.boxlite.cloud")
        endpoint = cloud_block.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError(
                "server config 'sandbox.boxlite.cloud.endpoint' is required — the "
                "boxlite REST URL, e.g. 'https://boxlite.example.com:8100'"
            )
        return endpoint.strip(), None, None
    # Local mode (the default when neither block is present).
    if not local_present:
        return None, None, None
    if not isinstance(local_block, dict):
        raise ValueError("server config 'sandbox.boxlite.local' must be a mapping")
    _reject_unknown_boxlite_keys(local_block, {"home_dir", "registry"}, "sandbox.boxlite.local")
    return None, _parse_boxlite_home_dir(local_block), _parse_boxlite_registry(local_block)


def _parse_boxlite_image(section: dict[str, object]) -> str | None:
    """
    Extract the optional shared ``sandbox.boxlite.image`` (default: official
    host image). Shared by both modes.

    :returns: The validated image reference, or ``None`` to use the default.
    :raises ValueError: When present but not a non-empty string.
    """
    image = section.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.boxlite.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the official image)"
        )
    return image.strip()


def _parse_boxlite_env(section: dict[str, object]) -> list[str] | None:
    """
    Extract the optional shared ``sandbox.boxlite.env`` — SERVER-process
    environment variable NAMES whose values are injected into every box (names
    only, so secret values never live in the config file). Shared by both modes.

    :returns: The validated env var names, or ``None`` when not configured.
    :raises ValueError: When present but not a list of non-empty strings.
    """
    env = section.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            "server config 'sandbox.boxlite.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['OPENAI_API_KEY', 'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


def _parse_boxlite_home_dir(local: dict[str, object]) -> str | None:
    """
    Extract the optional ``sandbox.boxlite.local.home_dir`` (boxlite data dir).

    :returns: The validated path, or ``None`` to use boxlite's default.
    :raises ValueError: When present but not a non-empty string.
    """
    home_dir = local.get("home_dir")
    if home_dir is None:
        return None
    if not isinstance(home_dir, str) or not home_dir.strip():
        raise ValueError(
            "server config 'sandbox.boxlite.local.home_dir' must be a non-empty path string"
        )
    return home_dir.strip()


def _parse_boxlite_registry(local: dict[str, object]) -> dict[str, object] | None:
    """
    Extract the optional ``sandbox.boxlite.local.registry`` block — private-
    registry config for pulling the host image in LOCAL mode.

    Shape: ``host`` (required) plus optional ``transport`` / ``skip_verify`` and
    the credential-NAME keys ``username_env`` / ``password_env`` / ``token_env``
    (which name server env vars holding the values — 12-factor, so secrets never
    live in the config file).

    :returns: The validated registry mapping, or ``None`` when not configured.
    :raises ValueError: When present but malformed.
    """
    registry = local.get("registry")
    if registry is None:
        return None
    if not isinstance(registry, dict):
        raise ValueError("server config 'sandbox.boxlite.local.registry' must be a mapping")
    _reject_unknown_boxlite_keys(
        registry,
        {"host", "transport", "skip_verify", "username_env", "password_env", "token_env"},
        "sandbox.boxlite.local.registry",
    )
    host = registry.get("host")
    if not isinstance(host, str) or not host.strip():
        raise ValueError(
            "server config 'sandbox.boxlite.local.registry.host' is required — the "
            "registry hostname, e.g. 'ghcr.io'"
        )
    out: dict[str, object] = {"host": host.strip()}
    for key in ("transport", "username_env", "password_env", "token_env"):
        value = registry.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                f"server config 'sandbox.boxlite.local.registry.{key}' must be a non-empty string"
            )
        out[key] = value.strip()
    skip_verify = registry.get("skip_verify")
    if skip_verify is not None:
        if not isinstance(skip_verify, bool):
            raise ValueError(
                "server config 'sandbox.boxlite.local.registry.skip_verify' must be a boolean"
            )
        out["skip_verify"] = skip_verify
    if "token_env" in out and ("username_env" in out or "password_env" in out):
        raise ValueError(
            "server config 'sandbox.boxlite.local.registry': token_env is mutually "
            "exclusive with username_env/password_env — boxlite uses the bearer token "
            "and silently ignores basic auth, so set exactly one auth method"
        )
    return out


def _cwsandbox_launcher_factory(
    image: str | None,
    env: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """Build the launcher factory for the YAML ``provider: cwsandbox`` path."""

    def _build() -> SandboxLauncher:
        from omnigent.onboarding.sandboxes.cwsandbox import CWSandboxLauncher

        return CWSandboxLauncher(image=image, env=env)

    return _build


def _parse_cwsandbox_image(raw: dict[str, object]) -> str | None:
    """Extract and validate ``sandbox.cwsandbox.image`` (optional)."""
    section = raw.get("cwsandbox")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("server config 'sandbox.cwsandbox' must be a mapping")
    image = section.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.cwsandbox.image' must be a registry image "
            "reference with omnigent pre-installed (omit it to use the official image)"
        )
    return image.strip()


def _parse_cwsandbox_env(raw: dict[str, object]) -> list[str] | None:
    """Extract and validate ``sandbox.cwsandbox.env`` — server env var NAMES (optional)."""
    section = raw.get("cwsandbox")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("server config 'sandbox.cwsandbox' must be a mapping")
    env = section.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            "server config 'sandbox.cwsandbox.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['ANTHROPIC_API_KEY', 'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


def _e2b_launcher_factory(
    template: str | None,
    env: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: e2b`` path.

    :param template: E2B template NAME the Omnigent host image was built
        into (``e2b template build``), or ``None`` to use the launcher's
        env-var fallback / the default template. Unlike the other
        providers' ``image`` field this is NOT a registry reference —
        E2B boots from templates (see
        :class:`omnigent.onboarding.sandboxes.e2b.E2BSandboxLauncher`).
    :param env: Names of server-process environment variables (harness
        LLM credentials, gateway URLs, ``GIT_TOKEN``) injected into
        every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or
        ``None`` to resolve from the launcher's env-var fallback /
        inject nothing.
    :returns: A factory producing parameterized E2B launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the E2B launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.e2b import E2BSandboxLauncher

        return E2BSandboxLauncher(template=template, env=env)

    return _build


def _parse_e2b_template(raw: dict[str, object]) -> str | None:
    """
    Extract and validate the e2b template from the ``sandbox`` dict.

    ``sandbox.e2b.template`` names the pre-built E2B template the
    Omnigent host image was built into — NOT a registry image reference
    (the wording every other provider's ``image`` field uses), because
    E2B cannot boot an arbitrary registry image. OPTIONAL — when absent,
    the launcher resolves :data:`~omnigent.onboarding.sandboxes.e2b.TEMPLATE_ENV_VAR`
    then the default template. A present-but-malformed value fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"e2b"``).
    :returns: The validated template name, or ``None`` to use the
        launcher's fallback / default.
    :raises ValueError: When ``sandbox.e2b`` is present but not a
        mapping, or ``sandbox.e2b.template`` is present but not a
        non-empty string.
    """
    section = _parse_provider_section(raw, "e2b")
    if section is None:
        return None
    template = section.get("template")
    if template is None:
        return None
    if not isinstance(template, str) or not template.strip():
        raise ValueError(
            "server config 'sandbox.e2b.template' must be the NAME of a pre-built "
            "E2B template the omnigent host image was built into (e.g. "
            "'omnigent-host'; see deploy/e2b/README.md) — NOT a registry image "
            "reference (omit it to use the default template)"
        )
    return template.strip()


def _islo_launcher_factory(
    *,
    image: str | None,
    env: list[str] | None,
    base_url: str | None,
    gateway_profile: str | None,
    snapshot_name: str | None,
    workdir: str | None,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: islo`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        use the official prebaked host image (env-overridable; see
        :class:`omnigent.onboarding.sandboxes.islo.IsloSandboxLauncher`).
    :param env: Names of server-process environment variables injected
        into every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``,
        or ``None`` to resolve from the launcher's env-var fallback /
        inject nothing.
    :param base_url: Optional Islo API base URL override.
    :param gateway_profile: Optional Islo gateway profile name.
    :param snapshot_name: Optional Islo snapshot name.
    :param workdir: Optional sandbox working directory.
    :param vcpus: Optional vCPU count.
    :param memory_mb: Optional memory allocation in MiB.
    :param disk_gb: Optional disk allocation in GiB.
    :returns: A factory producing parameterized Islo launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Islo launcher."""
        from omnigent.onboarding.sandboxes.islo import IsloSandboxLauncher

        return IsloSandboxLauncher(
            image=image,
            env=env,
            base_url=base_url,
            gateway_profile=gateway_profile,
            snapshot_name=snapshot_name,
            workdir=workdir,
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
        )

    return _build


def _openshell_launcher_factory(
    *,
    image: str | None,
    env: list[str] | None,
    cluster: str | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: openshell`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to use
        the official prebaked host image (env-overridable).
    :param env: Names of server-process environment variables injected
        into every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or
        ``None`` to resolve from the launcher's env-var fallback.
    :param cluster: OpenShell gateway name to connect to, or ``None`` to
        use the active gateway (``$OPENSHELL_GATEWAY`` /
        ``~/.config/openshell/active_gateway``).
    :returns: A factory producing parameterized OpenShell launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the OpenShell launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.openshell import OpenShellSandboxLauncher

        return OpenShellSandboxLauncher(image=image, env=env, cluster=cluster)

    return _build


def _parse_provider_section(raw: dict[str, object], provider: str) -> dict[str, object] | None:
    """
    Extract a provider-specific optional config block.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :returns: The provider mapping, or ``None`` when omitted.
    :raises ValueError: When the block is present but not a mapping.
    """
    section = raw.get(provider)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError(f"server config 'sandbox.{provider}' must be a mapping")
    return section


def _parse_provider_image(raw: dict[str, object], provider: str) -> str | None:
    """
    Extract and validate a provider image from the raw ``sandbox`` dict.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :returns: The validated image reference, or ``None`` to use the
        official default.
    :raises ValueError: When the provider block or image value is
        malformed.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    image = section.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            f"server config 'sandbox.{provider}.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the "
            "official image)"
        )
    return image.strip()


def _parse_provider_env(raw: dict[str, object], provider: str) -> list[str] | None:
    """
    Extract and validate provider env passthrough names.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :returns: Validated environment variable names, or ``None`` when
        not configured.
    :raises ValueError: When the provider block or env list is
        malformed.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    env = section.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            f"server config 'sandbox.{provider}.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['OPENAI_API_KEY', "
            "'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


def _parse_provider_string(raw: dict[str, object], provider: str, key: str) -> str | None:
    """
    Extract and validate an optional provider string field.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :param key: Field name under the provider block.
    :returns: The stripped string, or ``None`` when omitted.
    :raises ValueError: When the field is present but not a non-empty
        string.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a non-empty string")
    return value.strip()


def _parse_provider_positive_int(raw: dict[str, object], provider: str, key: str) -> int | None:
    """
    Extract and validate an optional positive integer provider field.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :param key: Field name under the provider block.
    :returns: The integer, or ``None`` when omitted.
    :raises ValueError: When the field is present but is not a positive
        integer.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a positive integer")
    return value


def _parse_provider_bool(raw: dict[str, object], provider: str, key: str) -> bool | None:
    """
    Extract and validate an optional boolean provider field.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"kubernetes"``.
    :param key: Field name under the provider block, e.g. ``"in_cluster"``.
    :returns: The boolean, or ``None`` when omitted.
    :raises ValueError: When the field is present but is not a real boolean (a
        YAML ``"true"`` string or an int are rejected — a silently-coerced flag
        would change the cluster-config source).
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a boolean")
    return value


def _parse_provider_str_mapping(
    raw: dict[str, object], provider: str, key: str
) -> dict[str, str] | None:
    """
    Extract and validate an optional provider string→string mapping field.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"kubernetes"``.
    :param key: Field name under the provider block, e.g. ``"node_selector"``.
    :returns: The validated mapping, or ``None`` when omitted.
    :raises ValueError: When the field is present but not a mapping of non-empty
        string keys to non-empty string values.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in value.items()
    ):
        raise ValueError(
            f"server config 'sandbox.{provider}.{key}' must be a mapping of "
            "non-empty string keys to non-empty string values, e.g. "
            "{'disktype': 'ssd'}"
        )
    return {k.strip(): v.strip() for k, v in value.items()}


# RFC 1123 / Kubernetes identifier forms for parse-time validation of
# ``sandbox.kubernetes`` names (mirrored, fixed-by-spec, in the launcher for its
# env-var overrides — see omnigent.onboarding.sandboxes.kubernetes).
_DNS1123_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_SUBDOMAIN_RE = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)
_K8S_LABEL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
# Kubernetes resource quantity, e.g. "500m", "2", "1Gi", "1.5" — a number with
# an optional binary/decimal suffix.
_K8S_QUANTITY_RE = re.compile(r"^\d+(\.\d+)?([eE][-+]?\d+)?[a-zA-Z]{0,2}i?$")


def _validate_dns1123_label(value: str | None, field: str) -> None:
    """Reject a ``sandbox.kubernetes.<field>`` that is not a DNS-1123 label."""
    if value is None:
        return
    if len(value) > 63 or not _DNS1123_LABEL_RE.fullmatch(value):
        raise ValueError(
            f"server config 'sandbox.kubernetes.{field}' is not a valid "
            f"Kubernetes name (RFC 1123 DNS label, max 63 chars): {value!r}"
        )


def _validate_dns1123_subdomain(value: str | None, field: str) -> None:
    """Reject a ``sandbox.kubernetes.<field>`` that is not a DNS-1123 subdomain."""
    if value is None:
        return
    if len(value) > 253 or not _DNS1123_SUBDOMAIN_RE.fullmatch(value):
        raise ValueError(
            f"server config 'sandbox.kubernetes.{field}' is not a valid "
            f"Kubernetes name (RFC 1123 DNS subdomain): {value!r}"
        )


def _validate_label_key(key: str) -> bool:
    """Return whether *key* is a valid Kubernetes label key (optional prefix)."""
    prefix, slash, name = key.rpartition("/")
    if slash and (not prefix or len(prefix) > 253 or not _DNS1123_SUBDOMAIN_RE.match(prefix)):
        return False
    return bool(name) and len(name) <= 63 and bool(_K8S_LABEL_SEGMENT_RE.match(name))


def _validate_kubernetes_identifiers(
    namespace: str | None,
    secret_name: str | None,
    service_account: str | None,
    node_selector: dict[str, str] | None,
) -> None:
    """
    Validate the YAML ``sandbox.kubernetes`` identifiers at parse time.

    :raises ValueError: When a name is not an RFC 1123 DNS subdomain/label or a
        node-selector entry is not a valid Kubernetes label key/value.
    """
    _validate_dns1123_label(namespace, "namespace")
    _validate_dns1123_subdomain(secret_name, "secret_name")
    _validate_dns1123_subdomain(service_account, "service_account")
    for key, value in (node_selector or {}).items():
        if not _validate_label_key(key):
            raise ValueError(
                f"server config 'sandbox.kubernetes.node_selector' has an "
                f"invalid label key: {key!r}"
            )
        if value and (len(value) > 63 or not _K8S_LABEL_SEGMENT_RE.match(value)):
            raise ValueError(
                f"server config 'sandbox.kubernetes.node_selector[{key}]' has "
                f"an invalid label value: {value!r}"
            )


def _parse_kubernetes_resources(raw: dict[str, object]) -> dict[str, object] | None:
    """
    Extract and validate the optional ``sandbox.kubernetes.resources`` block.

    Shape: ``{requests?: {cpu?, memory?}, limits?: {cpu?, memory?}}`` — every
    level optional, each ``cpu`` / ``memory`` a non-empty Kubernetes quantity
    string. Validated at parse time so an operator typo fails server startup
    instead of the first managed launch; an omitted field keeps the default.

    :param raw: The raw ``sandbox`` mapping.
    :returns: The validated resources block, or ``None`` when omitted.
    :raises ValueError: When the block or any field has the wrong shape.
    """
    section = _parse_provider_section(raw, "kubernetes")
    if section is None:
        return None
    value = section.get("resources")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(
            "server config 'sandbox.kubernetes.resources' must be a mapping with "
            "optional 'requests' / 'limits' blocks"
        )
    normalized: dict[str, object] = {}
    for tier, tier_value in value.items():
        if tier not in ("requests", "limits"):
            raise ValueError(
                f"server config 'sandbox.kubernetes.resources' has an unknown key "
                f"{tier!r} (expected 'requests' or 'limits')"
            )
        if not isinstance(tier_value, dict):
            raise ValueError(
                f"server config 'sandbox.kubernetes.resources.{tier}' must be a "
                "mapping of 'cpu' / 'memory' to quantity strings"
            )
        norm_tier: dict[str, str] = {}
        for field, field_value in tier_value.items():
            if field not in ("cpu", "memory"):
                raise ValueError(
                    f"server config 'sandbox.kubernetes.resources.{tier}' has an "
                    f"unknown key {field!r} (expected 'cpu' or 'memory')"
                )
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(
                    f"server config 'sandbox.kubernetes.resources.{tier}.{field}' "
                    "must be a non-empty quantity string, e.g. '500m' or '2Gi'"
                )
            quantity = field_value.strip()
            if not _K8S_QUANTITY_RE.match(quantity):
                raise ValueError(
                    f"server config 'sandbox.kubernetes.resources.{tier}.{field}' "
                    f"is not a valid Kubernetes quantity: {field_value!r} "
                    "(e.g. '500m', '2', '1Gi')"
                )
            norm_tier[field] = quantity
        normalized[tier] = norm_tier
    return normalized


def _kubernetes_launcher_factory(
    *,
    image: str | None,
    env: list[str] | None,
    namespace: str | None,
    secret_name: str | None,
    service_account: str | None,
    node_selector: dict[str, str] | None,
    kubeconfig: str | None,
    in_cluster: bool | None,
    resources: dict[str, object] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: kubernetes`` path.

    :param image: Registry image with omnigent pre-installed, or ``None`` for
        the official prebaked host image (env-overridable).
    :param env: Names of server-process environment variables injected into
        every Pod as literal ``env``, or ``None``. Prefer *secret_name* for
        credentials.
    :param namespace: Namespace to create Pods in, or ``None`` for the default.
    :param secret_name: Pre-created Secret projected into every Pod via
        ``envFrom`` (harness credentials), or ``None``.
    :param service_account: ServiceAccount the Pods run as, or ``None``.
    :param node_selector: Extra node selector labels merged with the mandatory
        amd64 constraint, or ``None``.
    :param kubeconfig: Explicit kubeconfig path for the out-of-cluster fallback,
        or ``None``.
    :param in_cluster: Force the cluster-config source, or ``None`` to try
        in-cluster then fall back to kubeconfig.
    :param resources: Validated ``resources`` block, or ``None`` for defaults.
    :returns: A factory producing parameterized Kubernetes launchers.
    :raises ValueError: When a name or node-selector label is malformed.
    """
    _validate_kubernetes_identifiers(namespace, secret_name, service_account, node_selector)

    def _build() -> SandboxLauncher:
        """Construct the Kubernetes launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher

        return KubernetesSandboxLauncher(
            image=image,
            env=env,
            namespace=namespace,
            secret_name=secret_name,
            service_account=service_account,
            node_selector=node_selector,
            kubeconfig=kubeconfig,
            in_cluster=in_cluster,
            resources=resources,
        )

    return _build


async def launch_managed_host(
    *,
    config: ManagedSandboxConfig,
    owner: str,
    host_store: HostStore,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> ManagedHostLaunch:
    """
    Provision a sandbox, start a host in it, and wait until it registers.

    Sequence: provision sandbox → pre-register the host row with its
    launch-token digest (so the credential resolves by the time the
    host dials the tunnel) → optionally clone the requested repository
    → start ``omnigent host`` inside the sandbox with the token +
    identity in its environment → poll the hosts table until the host
    is online. Any failure after provisioning terminates the sandbox
    and deletes the host row (which revokes the token) before
    re-raising.

    :param config: The deployment's sandbox config (YAML-parsed or
        directly constructed with a custom launcher factory).
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on single-user servers).
    :param host_store: Persistent host registrations — receives the
        pre-registered host row and is polled for the sandbox host
        coming online.
    :param repo: Parsed repository-URL workspace to clone into the
        sandbox as the session's working directory, or ``None`` for
        an empty workspace. Private repositories authenticate via the
        host image's git credential helper when the sandbox env
        carries ``GIT_TOKEN`` (injected through Modal secrets — see
        deploy/modal/README.md "Git credentials").
    :param on_stage: Progress observer invoked as the launch pipeline
        advances, with the stage just entered: ``"cloning"`` (when
        *repo* is set) then ``"starting"``. May be called from a
        worker thread (the sandbox exec steps run via
        ``asyncio.to_thread``), so it must be thread-safe. ``None``
        disables progress reporting.
    :returns: The registered host id + in-sandbox workspace path
        (the cloned repository directory when *repo* is set).
    :raises HTTPException: 400 when the configured provider lacks
        managed-launch support; 502 when provisioning, cloning, host
        startup, or registration fails.
    """
    launcher = config.launcher_factory()
    host_id = f"host_{uuid.uuid4().hex}"
    # Visible label in the host picker; (owner, name) is the hosts
    # table PK, so embed the host_id's leading hex for uniqueness
    # across a user's managed sandboxes.
    host_name = f"managed-{host_id[len('host_') : len('host_') + 8]}"
    try:
        await asyncio.to_thread(launcher.prepare)
        sandbox_id = await asyncio.to_thread(launcher.provision, host_name)
    except click.ClickException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox launch failed: {exc.message}",
        ) from exc
    workspace = await _arm_and_start_host(
        launcher=launcher,
        config=config,
        host_store=host_store,
        host_id=host_id,
        host_name=host_name,
        owner=owner,
        sandbox_id=sandbox_id,
        repo=repo,
        on_stage=on_stage,
    )
    return ManagedHostLaunch(host_id=host_id, workspace=workspace)


async def relaunch_managed_host(
    *,
    config: ManagedSandboxConfig,
    host: Host,
    host_store: HostStore,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> ManagedHostLaunch:
    """
    Provision a NEW sandbox generation for an existing managed host.

    The host identity is durable while its sandbox is not: when the
    sandbox dies (the provider's lifetime cap, a crash, a manual
    terminate), the host row and the sessions bound to it remain.
    This relaunch keeps that identity — terminate the old sandbox
    (best-effort; it is usually already gone), provision a fresh one,
    and re-arm the SAME host row with a new token + sandbox id (which
    atomically revokes the previous generation's token).

    The new sandbox starts from the image — workspace contents of the
    dead generation are gone. Passing *repo* re-clones the session's
    repository so the workspace is restored to its create-time state.

    Unlike a first launch, a failure here keeps the host row (only the
    new sandbox is torn down and the armed token revoked), so the
    session binding survives and a later attempt can retry.

    :param config: The deployment's sandbox config.
    :param host: The existing managed host row to relaunch
        (``sandbox_provider`` set; callers guard on that).
    :param host_store: Persistent host registrations.
    :param repo: Repository to re-clone as the workspace, or ``None``
        for an empty workspace.
    :param on_stage: Progress observer forwarded to
        :func:`_arm_and_start_host`; see :func:`launch_managed_host`.
        ``None`` disables progress reporting.
    :returns: The (unchanged) host id + fresh in-sandbox workspace.
    :raises HTTPException: 400 when the host's recorded provider no
        longer matches the configured launcher; 502 when
        provisioning, cloning, host startup, or registration fails.
    """
    launcher = _launcher_for_teardown(host, config)
    if launcher is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"the '{host.sandbox_provider}' sandbox provider this host "
                "was launched with is no longer configured on this server"
            ),
        )
    # The old generation is normally already dead (that is why we are
    # here), but terminate defensively so a transient tunnel outage
    # can never leave two live sandboxes claiming one host identity.
    await _terminate_sandbox_best_effort(launcher, host)
    try:
        await asyncio.to_thread(launcher.prepare)
        sandbox_id = await asyncio.to_thread(launcher.provision, host.name)
    except click.ClickException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox relaunch failed: {exc.message}",
        ) from exc
    workspace = await _arm_and_start_host(
        launcher=launcher,
        config=config,
        host_store=host_store,
        host_id=host.host_id,
        host_name=host.name,
        owner=host.owner,
        sandbox_id=sandbox_id,
        repo=repo,
        on_stage=on_stage,
        keep_host_on_failure=True,
    )
    return ManagedHostLaunch(host_id=host.host_id, workspace=workspace)


def _resolve_git_identity(
    config: ManagedSandboxConfig, owner: str
) -> tuple[str | None, str | None]:
    """
    Resolve the in-sandbox commit identity for a managed launch.

    Bidirectional git sync is ON when the deployment configured a git
    identity (``sandbox.daytona.git_sync``) OR any context repos — either
    needs a committable identity for the round-trip to work. When on, the
    ``user.name`` falls back to :data:`DEFAULT_MANAGED_GIT_USER_NAME` and the
    ``user.email`` to the session *owner* (the more meaningful per-session
    value than any static config). When off, both are ``None`` so
    :meth:`SandboxLauncher.start_host` leaves git untouched — preserving the
    prior behavior for sessions that don't opt in.

    :param config: The deployment's sandbox config.
    :param owner: The session owner, e.g. ``"alice@example.com"``, used as
        the default commit email.
    :returns: ``(user_name, user_email)`` — both ``None`` when git sync is
        not enabled for this deployment.
    """
    sync_enabled = config.git_user_name is not None or bool(config.context_repos)
    if not sync_enabled:
        return None, None
    return (
        config.git_user_name or DEFAULT_MANAGED_GIT_USER_NAME,
        config.git_user_email or owner,
    )


async def _arm_and_start_host(
    *,
    launcher: SandboxLauncher,
    config: ManagedSandboxConfig,
    host_store: HostStore,
    host_id: str,
    host_name: str,
    owner: str,
    sandbox_id: str,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
    keep_host_on_failure: bool = False,
) -> str:
    """
    Arm the credential, start the in-sandbox host, and await its
    registration — tearing the sandbox down on any failure.

    The credential is registered BEFORE the host process starts, so
    the token is resolvable by the time the host first dials the
    tunnel. A failure in any later step terminates the sandbox and
    revokes the armed token before re-raising — by deleting the host
    row (first launch: the row would otherwise be an unusable picker
    ghost) or, on a relaunch, by clearing the credential columns only
    (the durable row keeps the session binding alive for a retry).

    :param launcher: The launcher holding the provisioned sandbox.
    :param config: The deployment's sandbox config.
    :param host_store: Persistent host registrations.
    :param host_id: Server-chosen host identity, e.g.
        ``"host_a1b2c3d4..."``.
    :param host_name: Server-chosen host display name, e.g.
        ``"managed-a1b2c3d4"``.
    :param owner: User the managed host acts for, e.g.
        ``"alice@example.com"``.
    :param sandbox_id: The provisioned sandbox, e.g. ``"sb-a1b2c3"``.
    :param repo: Repository to clone as the workspace, or ``None``
        for an empty workspace.
    :param on_stage: Progress observer forwarded to the launcher's
        ``start_host``; see :func:`launch_managed_host`. ``None``
        disables progress reporting.
    :param keep_host_on_failure: ``True`` on a relaunch — failure
        cleanup terminates the new sandbox and revokes the token but
        keeps the host row. ``False`` (first launch) deletes the row.
    :returns: The absolute in-sandbox workspace path.
    :raises HTTPException: 502 when cloning, host startup, or
        registration fails.
    """
    token = secrets.token_urlsafe(32)
    record = await asyncio.to_thread(
        host_store.register_managed_host,
        host_id=host_id,
        name=host_name,
        owner=owner,
        token=token,
        provider=launcher.provider,
        sandbox_id=sandbox_id,
        token_expires_at=now_epoch() + config.token_ttl_s,
    )
    try:
        # Uniform across providers: provision() fixed the sandbox id and the
        # token was armed against it above, so start_host starts the host with
        # a token that already resolves. The exec-model default execs in; the
        # entrypoint model (k8s) creates the Pod that boots the host. *repo* is
        # unpacked into primitives — the launcher API takes no RepoWorkspace.
        git_user_name, git_user_email = _resolve_git_identity(config, owner)
        workspace = await asyncio.to_thread(
            launcher.start_host,
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=config.server_url,
            repo_url=repo.url if repo is not None else None,
            repo_branch=repo.branch if repo is not None else None,
            repo_name=repo.repo_name if repo is not None else None,
            git_user_name=git_user_name,
            git_user_email=git_user_email,
            context_repos=config.context_repos or None,
            on_stage=on_stage,
        )
        await _wait_for_host_online(host_store, host_id)
    except Exception as exc:
        # Broad on purpose: any post-provision failure — launcher CLI
        # errors, provider SDK exceptions (e.g. Modal's
        # SandboxTerminated), raw network errors from the in-sandbox
        # exec — must tear down the sandbox and revoke the armed token,
        # or the sandbox leaks running until the provider's lifetime
        # cap. Cleanup-then-reraise at a system boundary, not a
        # swallow: every path below re-raises as an HTTPException.
        if keep_host_on_failure:
            await _terminate_sandbox_best_effort(launcher, record)
            await asyncio.to_thread(host_store.revoke_launch_token, host_id)
        else:
            await terminate_managed_host(record, host_store, config)
        if isinstance(exc, HTTPException):
            raise
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox host startup failed: {message}",
        ) from exc
    return workspace


async def _wait_for_host_online(host_store: HostStore, host_id: str) -> None:
    """
    Poll the hosts table until the sandbox host registers, or time out.

    :param host_store: Persistent host registrations.
    :param host_id: The launched host's identifier.
    :raises HTTPException: 502 when the host does not come online
        within :data:`MANAGED_HOST_ONLINE_TIMEOUT_S`.
    """
    deadline = time.monotonic() + MANAGED_HOST_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if await asyncio.to_thread(host_store.is_online, host_id):
            return
        await asyncio.sleep(_ONLINE_POLL_INTERVAL_S)
    raise HTTPException(
        status_code=502,
        detail=(
            f"managed host did not come online within "
            f"{MANAGED_HOST_ONLINE_TIMEOUT_S}s — check {_HOST_LOG_PATH} "
            "inside the sandbox"
        ),
    )


def _launcher_for_teardown(
    host: Host,
    config: ManagedSandboxConfig | None,
) -> SandboxLauncher | None:
    """
    Resolve the launcher that can terminate a managed host's sandbox.

    The deployment's CURRENT launcher factory is only usable when its
    provider matches the provider recorded on the host row at launch —
    a config change between launch and teardown must not aim a
    different provider's terminate at a stale sandbox id.

    :param host: The managed host being torn down.
    :param config: The deployment's current sandbox config, or ``None``
        when the ``sandbox:`` section has been removed since launch.
    :returns: A launcher whose provider matches the row, or ``None``
        when no matching launcher is available.
    """
    if config is None:
        return None
    try:
        launcher = config.launcher_factory()
    except HTTPException:
        # The YAML path's unsupported-provider factory raises; there is
        # no launcher to terminate with.
        return None
    if launcher.provider != host.sandbox_provider:
        return None
    return launcher


def host_resume_supported(
    host: Host,
    config: ManagedSandboxConfig | None,
) -> bool:
    """
    Whether :func:`resume_managed_host` could wake this host in place.

    ``True`` iff the host is bound to a sandbox whose provider has a
    stop/resume lifecycle with a persistent volume
    (:attr:`SandboxLauncher.can_resume`) and still matches the deployment's
    current launcher. This is the SAME gate :func:`resume_managed_host`
    applies before a wake, exposed so the open-session snapshot
    (``SessionResponse.host_resumable``) can render a dormant such host as a
    wakeable "asleep" state instead of the terminal ``host_offline``
    dead-end.

    :param host: The session's bound managed host.
    :param config: The deployment's current sandbox config, or ``None``
        when the ``sandbox:`` section has been removed since launch.
    :returns: ``True`` when a wake would be attempted; ``False`` for a
        non-managed / non-resumable provider, a dropped config, or a
        host with no recorded ``sandbox_id``.
    """
    launcher = _launcher_for_teardown(host, config)
    return launcher is not None and launcher.can_resume and host.sandbox_id is not None


# ── Managed-host wake (resume a dormant host on demand) ─────────────────────

# Per-host resume single-flight: one in-flight resume per host_id on this
# replica, else two host processes flap the tunnel registration. Reused across a
# host's many idle-stop/resume cycles, so not reaped — a .pop() could also race
# a resume still holding it; one idle Lock per host woken is negligible.
_resume_locks: dict[str, asyncio.Lock] = {}


async def resume_managed_host(
    host_id: str,
    host_store: HostStore,
    config: ManagedSandboxConfig | None,
) -> None:
    """
    Wake a dormant managed host so a session bound to it can run again.

    The send-message relaunch path calls this when a host-bound session has no
    live runner. If the host is a *resumable* managed host — a provider whose
    sandbox idle-stops but retains its persistent volume
    (:attr:`SandboxLauncher.can_resume`) — and is currently offline, this
    resumes the sandbox under the SAME sandbox id, re-arms its launch token,
    re-execs ``omnigent host``, and waits for it to re-register. The caller's
    existing relaunch then spawns a fresh runner.

    No-op when the host is already online, is unknown, or its provider cannot
    resume (e.g. Modal — the caller falls through to its normal host-offline
    behavior, i.e. the user starts a new session). Single-flight and
    idempotent: concurrent callers serialize on a per-host lock and re-check
    liveness under it, so only the first wakes the host.

    Unlike a launch, a failed wake does NOT tear the sandbox down — the volume
    + workspace are the user's and must survive for a retry.

    :param host_id: The session's bound host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations (cross-replica liveness).
    :param config: The deployment's managed-sandbox config, or ``None`` when
        the ``sandbox:`` section has been removed since launch.
    :raises HTTPException: 502 when the resume or host restart fails.
    """
    if config is None:
        return
    # Cross-replica DB liveness (freshness-gated): never trust the per-replica
    # registry alone. Cheap gate before taking the lock.
    if await asyncio.to_thread(host_store.is_online, host_id):
        return
    host = await asyncio.to_thread(host_store.get_host, host_id)
    if host is None:
        return
    # Provider-matched launcher (None if config dropped / provider changed).
    # Resume needs a reattachable volume; others (e.g. Modal) fall through to
    # the caller's host-offline path (the user starts a new session).
    launcher = _launcher_for_teardown(host, config)
    if launcher is None or not launcher.can_resume or host.sandbox_id is None:
        return
    sandbox_id = host.sandbox_id
    # Single-flight per host (see _resume_locks).
    resume_lock = _resume_locks.setdefault(host_id, asyncio.Lock())
    async with resume_lock:
        # Re-check under the lock: a concurrent waker may have brought the host
        # online while we waited.
        if await asyncio.to_thread(host_store.is_online, host_id):
            return
        _logger.info(
            "Waking dormant managed host %s (sandbox %s, provider %s)",
            host.host_id,
            sandbox_id,
            launcher.provider,
        )
        try:
            await asyncio.to_thread(launcher.resume, sandbox_id)
            # Mint a fresh token: the old one died with the host process's env
            # (only its hash persists). register_managed_host's relaunch branch
            # overwrites it in place, keeping the host_id's session bindings.
            token = secrets.token_urlsafe(32)
            await asyncio.to_thread(
                host_store.register_managed_host,
                host_id=host.host_id,
                name=host.name,
                owner=host.owner,
                token=token,
                provider=launcher.provider,
                sandbox_id=sandbox_id,
                token_expires_at=now_epoch() + config.token_ttl_s,
            )
            # The persistent volume already holds the cloned workspace, every
            # context repo, and the ~/.gitconfig identity from the original
            # launch, so resume re-clones NOTHING: passing repo_url /
            # context_repos here would re-run `git clone` onto non-empty
            # directories and fail. (A context repo added to config AFTER the
            # original launch therefore won't appear until the next fresh
            # launch — the same persistence trade-off the primary repo makes.)
            # The identity, by contrast, IS re-applied: a `git config --global`
            # is cheap and idempotent (a no-op when unchanged), so a host whose
            # home volume predates git sync being enabled still gets one.
            git_user_name, git_user_email = _resolve_git_identity(config, host.owner)
            await asyncio.to_thread(
                launcher.start_host,
                sandbox_id,
                token=token,
                host_id=host.host_id,
                host_name=host.name,
                server_url=config.server_url,
                repo_url=None,  # the persistent volume already holds the workspace
                git_user_name=git_user_name,
                git_user_email=git_user_email,
            )
            await _wait_for_host_online(host_store, host.host_id)
        except Exception as exc:
            # A failed wake must NOT tear the sandbox down (the volume is the
            # user's); just surface it.
            if isinstance(exc, HTTPException):
                raise
            message = exc.message if isinstance(exc, click.ClickException) else str(exc)
            raise HTTPException(
                status_code=502, detail=f"managed host wake failed: {message}"
            ) from exc


async def terminate_managed_host(
    host: Host,
    host_store: HostStore,
    config: ManagedSandboxConfig | None,
) -> None:
    """
    Terminate a managed host's sandbox and delete its host row.

    Deleting the row is both teardown and revocation in one operation:
    the host disappears from the picker AND its launch token stops
    resolving. Best-effort on the sandbox side: termination failures
    (or a missing/mismatched launcher after a config change) are
    logged, not raised — the provider's lifetime cap reaps stragglers,
    and the caller (session delete / launch-failure cleanup) must not
    be blocked by provider hiccups.

    :param host: The managed host to tear down (``sandbox_provider`` /
        ``sandbox_id`` set; callers guard on that).
    :param host_store: Store holding the host row.
    :param config: The deployment's current sandbox config (supplies
        the launcher for the provider-side terminate), or ``None``
        when managed hosts are no longer configured.
    """
    launcher = _launcher_for_teardown(host, config)
    await _terminate_sandbox_best_effort(launcher, host)
    await asyncio.to_thread(host_store.delete_host, host.host_id)


async def _terminate_sandbox_best_effort(
    launcher: SandboxLauncher | None,
    host: Host,
) -> None:
    """
    Terminate a managed host's sandbox without touching its row.

    Best-effort by design: termination failures (or a
    missing/mismatched launcher after a config change) are logged, not
    raised — the provider's lifetime cap reaps stragglers, and callers
    (session delete, launch-failure cleanup, relaunch) must not be
    blocked by provider hiccups.

    :param launcher: Provider-matched launcher from
        :func:`_launcher_for_teardown`, or ``None`` when no matching
        launcher is available (logged, nothing terminated).
    :param host: The host whose ``sandbox_id`` names the sandbox.
    """
    if launcher is not None and host.sandbox_id is not None:
        try:
            await asyncio.to_thread(launcher.terminate, host.sandbox_id)
        except Exception:  # noqa: BLE001 — deliberate broad catch: this is a
            # provider-API boundary on a cleanup path. The provider SDK can
            # fail here in many shapes (auth/config ClickException, network
            # errors, SDK-internal exceptions), the sandbox may already be
            # gone past its lifetime cap, and NONE of those may block the
            # caller's remaining cleanup (deleting the host row / revoking
            # the launch token), which only we can do.
            _logger.warning(
                "Failed to terminate managed sandbox %s (provider=%s) for host %s",
                host.sandbox_id,
                host.sandbox_provider,
                host.host_id,
                exc_info=True,
            )
    else:
        _logger.warning(
            "No launcher available for managed sandbox provider %s; "
            "sandbox %s must be deleted with the provider's own tooling",
            host.sandbox_provider,
            host.sandbox_id,
        )
