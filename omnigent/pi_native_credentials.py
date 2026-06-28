"""Translate the omnigent-configured model provider into native Pi config.

A native Pi session launches the ``pi`` CLI, which authenticates from its own
config directory (``~/.pi/agent``). Without help, a user who ran ``omnigent
setup`` would still have to run ``pi`` ``/login`` separately — unlike
claude-native / codex-native, which route through the provider that ``omnigent
setup`` configured.

This module closes that gap. It resolves the provider configured for the Pi
surface (``~/.omnigent/config.yaml``) and writes a per-session ``models.json``
into a *managed* Pi config dir (selected via ``PI_CODING_AGENT_DIR``), so the
runner-owned ``pi`` process authenticates exactly like the configured harness —
mirroring how codex-native routes through the Databricks AI Gateway.

The managed config dir is per-session (like codex-native's managed
``CODEX_HOME``), so this never mutates the user's global ``~/.pi/agent``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    CHAT_WIRE_API,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LOCAL_KIND,
    OPENAI_FAMILY,
    PI_SURFACE,
    RESPONSES_WIRE_API,
    ProviderEntry,
    get_default_provider,
    load_config,
)

# Env var the ``pi`` CLI reads to relocate its config dir (default
# ``~/.pi/agent``). Setting it per session gives Pi a managed, isolated
# config dir we own — the analog of codex-native's ``CODEX_HOME``.
PI_CODING_AGENT_DIR_ENV_VAR = "PI_CODING_AGENT_DIR"

# Provider id registered in the generated ``models.json``. Stable so
# ``--provider`` can select it.
_PI_PROVIDER_ID = "omnigent"

# Default model for the Databricks AI Gateway's Anthropic surface — the same
# default the in-process Databricks executor pins. Used when the session
# carries no explicit model override.
_DATABRICKS_PI_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"

# Databricks AI Gateway Anthropic Messages surface. Pi speaks this protocol
# natively (``api: anthropic-messages``); the gateway authenticates with a
# workspace bearer token, so we set ``authHeader`` (Authorization: Bearer).
_DATABRICKS_ANTHROPIC_GATEWAY_PATH = "/ai-gateway/anthropic"

# Canonical vendor endpoints, used by the env-var fallback when the injected
# ``*_BASE_URL`` override is unset (mirrors the onboarding family defaults).
_OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com"


def _openai_api_for_base_url(base_url: str) -> str:
    """Pick Pi's OpenAI ``api`` type for an endpoint with no explicit wire.

    The OpenAI Responses API is served only by OpenAI's own endpoint; every
    other OpenAI-compatible vendor (Fireworks, Groq, OpenRouter, …) speaks
    Chat Completions only and 404s on ``/responses``. Infer from the base URL
    so a provider configured without an explicit ``wire_api`` still works.

    :param base_url: The OpenAI-family endpoint base URL.
    :returns: ``"openai-responses"`` for OpenAI's own endpoint, else
        ``"openai-completions"``.
    """
    return "openai-responses" if "api.openai.com" in base_url else "openai-completions"


@dataclass(frozen=True)
class PiProviderConfig:
    """A resolved native-Pi provider, ready to render into ``models.json``.

    :param provider_id: Provider id used in ``models.json`` and ``--provider``.
    :param base_url: Endpoint base URL the ``pi`` CLI talks to.
    :param api: Pi API type, e.g. ``"anthropic-messages"`` or
        ``"openai-responses"``.
    :param model: Model id to select, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param api_key: Credential value for ``models.json`` ``apiKey`` — a literal
        key, an env-var name, or a ``"!command"`` shell form (resolved by Pi at
        request time, used for short-lived gateway tokens).
    :param auth_header: When ``True``, Pi sends ``Authorization: Bearer
        <apiKey>`` (gateways) instead of a provider-native key header.
    """

    provider_id: str
    base_url: str
    api: str
    model: str
    api_key: str
    auth_header: bool

    def to_models_config(self) -> dict[str, Any]:
        """Render this provider as a Pi ``models.json`` mapping."""
        provider: dict[str, Any] = {
            "baseUrl": self.base_url,
            "api": self.api,
            "apiKey": self.api_key,
            "models": [{"id": self.model}],
        }
        if self.auth_header:
            provider["authHeader"] = True
        return {"providers": {self.provider_id: provider}}


def _databricks_pi_provider(entry: ProviderEntry, *, model: str | None) -> PiProviderConfig | None:
    """Resolve a Databricks-profile provider into Pi gateway config.

    :param entry: The resolved default provider entry (``kind="databricks"``).
    :param model: Session model override, or ``None`` to use the default.
    :returns: The Pi provider config, or ``None`` when the profile's host
        can't be resolved (caller falls back to Pi's own login).
    """
    # Imported lazily: codex_executor pulls in heavy inner deps, and this
    # module is imported on the runner's session-create path.
    from omnigent.inner.codex_executor import _databricks_codex_auth_command
    from omnigent.inner.databricks_executor import _read_databrickscfg_host

    host = _read_databrickscfg_host(entry.profile)
    if not host:
        return None
    host = host.rstrip("/")
    auth_command = _databricks_codex_auth_command(host, entry.profile)
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=f"{host}{_DATABRICKS_ANTHROPIC_GATEWAY_PATH}",
        api="anthropic-messages",
        model=model or _DATABRICKS_PI_DEFAULT_MODEL,
        # Pi resolves a "!command" apiKey at request time, so the gateway
        # bearer token is refreshed per request (the auth command itself
        # force-refreshes), matching codex-native's refresh semantics.
        api_key=f"!{auth_command}",
        auth_header=True,
    )


def _inline_family_pi_provider(
    entry: ProviderEntry, *, model: str | None
) -> PiProviderConfig | None:
    """Resolve a key/gateway/local provider into Pi config from its family.

    Prefers the Anthropic family (Pi speaks ``anthropic-messages`` natively),
    falling back to the OpenAI family — whose wire (Responses vs Chat
    Completions) follows ``wire_api``, inferred from the base URL when unset.

    :param entry: The resolved default provider entry.
    :param model: Session model override, or ``None`` to use the family default.
    :returns: The Pi provider config, or ``None`` when no usable family with a
        base URL and credential is configured.
    """
    for family_name in ("anthropic", "openai"):
        family = entry.family(family_name)
        if family is None or not family.base_url:
            continue
        # Determine the API type based on family and wire_api setting.
        if family_name == "anthropic":
            api = "anthropic-messages"
        elif family.wire_api == CHAT_WIRE_API:
            api = "openai-completions"
        elif family.wire_api == RESPONSES_WIRE_API:
            api = "openai-responses"
        else:
            # wire_api unset: infer from the base URL so a third-party provider
            # configured without an explicit wire_api still works (the
            # pi+Fireworks bug) rather than defaulting every gateway to
            # Responses, which only OpenAI's own endpoint serves.
            api = _openai_api_for_base_url(family.base_url)
        # A static key (or $VAR) — Pi reads a literal/env apiKey directly; an
        # auth_command becomes a "!command" Pi resolves at request time.
        if family.api_key:
            api_key = family.api_key
            auth_header = False
        elif family.auth_command:
            api_key = f"!{family.auth_command}"
            auth_header = True
        else:
            continue
        resolved_model = model or entry.family_default_model(family_name)
        if not resolved_model:
            continue
        return PiProviderConfig(
            provider_id=_PI_PROVIDER_ID,
            base_url=family.base_url,
            api=api,
            model=resolved_model,
            api_key=api_key,
            auth_header=auth_header,
        )
    return None


def _looks_like_anthropic_model(model: str) -> bool:
    """Whether *model* names a Claude model (so the Anthropic surface fits).

    Covers the plain (``claude-opus-4-8``), Databricks
    (``databricks-claude-sonnet-4-6``), and Bedrock (``us.anthropic.claude-…``)
    spellings. Used to pick the right family in the env-var fallback when a
    sandbox injects both Anthropic and OpenAI keys.
    """
    return "claude" in model.lower()


def _anthropic_env_pi_provider(*, model: str, env: Mapping[str, str]) -> PiProviderConfig | None:
    """Build an Anthropic-surface Pi provider from env, or None if no key."""
    # ``ANTHROPIC_AUTH_TOKEN`` is the gateway (bearer) form; ``ANTHROPIC_API_KEY``
    # is the native key (x-api-key). Either drives the Anthropic surface.
    anthropic_key = env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN")
    if not anthropic_key:
        return None
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=env.get("ANTHROPIC_BASE_URL") or _ANTHROPIC_DEFAULT_BASE_URL,
        api="anthropic-messages",
        model=model,
        api_key=anthropic_key,
        # A native key uses x-api-key; a bare bearer token goes in Authorization
        # (authHeader), the gateway form.
        auth_header=not env.get("ANTHROPIC_API_KEY"),
    )


def _openai_env_pi_provider(*, model: str, env: Mapping[str, str]) -> PiProviderConfig | None:
    """Build an OpenAI-compatible Pi provider from env, or None if no key."""
    openai_key = env.get("OPENAI_API_KEY")
    if not openai_key:
        return None
    base_url = env.get("OPENAI_BASE_URL") or _OPENAI_DEFAULT_BASE_URL
    return PiProviderConfig(
        provider_id=_PI_PROVIDER_ID,
        base_url=base_url,
        api=_openai_api_for_base_url(base_url),
        model=model,
        api_key=openai_key,
        auth_header=False,
    )


def _env_var_pi_provider(*, model: str | None, env: Mapping[str, str]) -> PiProviderConfig | None:
    """Build a Pi provider from credentials injected into the environment.

    Managed sandboxes (Daytona / Modal) ship no ``~/.omnigent/config.yaml`` —
    they inject credentials as environment variables instead — so config-based
    resolution finds nothing and Pi would fall back to a ``/login`` that does
    not exist in a fresh sandbox. This mirrors the documented managed-sandbox
    contract (see ``deploy/modal/README.md``): ``ANTHROPIC_API_KEY`` (plus an
    optional ``ANTHROPIC_BASE_URL``) drives Pi's native Anthropic surface;
    ``OPENAI_API_KEY`` (plus an optional ``OPENAI_BASE_URL``) drives any
    OpenAI-compatible endpoint — Fireworks, Groq, a gateway, or self-hosted.

    A sandbox commonly injects BOTH families' keys (a deployment-wide set), so
    the *model* picks the surface: a Claude model id uses the Anthropic surface,
    anything else uses the OpenAI-compatible endpoint (e.g. a Fireworks model id
    like ``accounts/fireworks/...`` → the OpenAI surface, NOT Anthropic). When
    only one family's key is present, that one is used regardless.

    :param model: The model id to pin — the session's ``model_override`` or the
        agent spec's model. Required: Pi's generated provider carries a single
        pinned model and the environment names no model, so this returns
        ``None`` without one (which keeps non-managed callers, who pass no
        model, on Pi's own login rather than mis-resolving from a stray key).
    :param env: The environment mapping to read (injection seam for tests).
    :returns: The resolved provider config, or ``None`` when no usable
        credential is present in the environment.
    """
    if not model:
        return None
    # Order the two surfaces by which fits the model, then take the first whose
    # key is present (so a missing preferred key still falls to the other).
    if _looks_like_anthropic_model(model):
        builders = (_anthropic_env_pi_provider, _openai_env_pi_provider)
    else:
        builders = (_openai_env_pi_provider, _anthropic_env_pi_provider)
    for build in builders:
        provider = build(model=model, env=env)
        if provider is not None:
            return provider
    return None


def resolve_pi_native_provider(
    *,
    model: str | None = None,
    config_loader: Callable[[], dict[str, Any]] = load_config,
    env: Mapping[str, str] | None = None,
) -> PiProviderConfig | None:
    """Resolve the omnigent-configured provider for a native Pi session.

    Reads the default provider for the Pi surface from
    ``~/.omnigent/config.yaml`` and translates it into Pi ``models.json``
    config. When no usable config provider is found — including a managed
    sandbox that ships no config file — falls back to credentials injected
    into the environment (:func:`_env_var_pi_provider`). Returns ``None`` —
    leaving Pi to use its own ``/login`` — only when neither yields a usable
    provider (e.g. a subscription / CLI-login default and no injected keys).

    :param model: Session model override (``model_override`` or the agent
        spec's model), or ``None`` to use the config provider's default model.
        Required for the env-var fallback (see :func:`_env_var_pi_provider`).
    :param config_loader: Injection seam for tests; defaults to
        :func:`load_config`.
    :param env: Environment mapping for the fallback; defaults to
        ``os.environ`` (injection seam for tests).
    :returns: The resolved provider config, or ``None`` to fall back to Pi's
        own credentials.
    """
    env = os.environ if env is None else env
    provider: PiProviderConfig | None = None
    try:
        config = config_loader()
        # Pi is multi-family; ``omnigent setup`` marks defaults per family, not
        # for ``pi``. Prefer an explicit pi default, then Anthropic (Pi's native
        # surface), then OpenAI.
        entry = (
            get_default_provider(config, PI_SURFACE)
            or get_default_provider(config, ANTHROPIC_FAMILY)
            or get_default_provider(config, OPENAI_FAMILY)
        )
        if entry is not None:
            if entry.kind == DATABRICKS_KIND:
                provider = _databricks_pi_provider(entry, model=model)
            elif entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
                provider = _inline_family_pi_provider(entry, model=model)
            # subscription / cli-config: a CLI's own login can't be reused
            # outside that CLI — leave ``provider`` None and try the env below.
    except Exception:  # noqa: BLE001 — any resolution failure must not break launch
        # Any failure (malformed config, duplicate per-family default, or an
        # unresolved ``api_key: $VAR``) falls back rather than failing launch.
        provider = None
    if provider is not None:
        return provider
    # No usable config provider (commonly: a managed sandbox ships no
    # config.yaml). Fall back to credentials injected into the environment.
    return _env_var_pi_provider(model=model, env=env)


def write_pi_models_config(agent_dir: Path, provider: PiProviderConfig) -> Path:
    """Write *provider* as ``models.json`` into a managed Pi config dir.

    :param agent_dir: The managed Pi config dir (``PI_CODING_AGENT_DIR``).
    :param provider: The resolved provider config to render.
    :returns: Path to the written ``models.json``.
    """
    agent_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(agent_dir, 0o700)
    models_path = agent_dir / "models.json"
    # 0o600: the apiKey may be a literal token (key-kind providers).
    fd = os.open(models_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(provider.to_models_config(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return models_path


def pi_native_provider_launch(
    agent_dir: Path, provider: PiProviderConfig
) -> tuple[dict[str, str], list[str]]:
    """Write the managed config and return the launch env + CLI args for Pi.

    :param agent_dir: The managed Pi config dir for this session.
    :param provider: The resolved provider config.
    :returns: ``(env, args)`` — the env vars to merge into the terminal spec
        (relocating Pi's config dir) and the ``--provider``/``--model`` args to
        append to the Pi command.
    """
    write_pi_models_config(agent_dir, provider)
    env = {PI_CODING_AGENT_DIR_ENV_VAR: str(agent_dir)}
    args = ["--provider", provider.provider_id, "--model", provider.model]
    return env, args
