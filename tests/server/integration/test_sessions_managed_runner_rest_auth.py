"""Integration tests for the managed-runner REST auth fallback.

A server-managed sandbox runner never runs ``omnigent login``, so its
in-sandbox REST callbacks carry no user identity — only the per-runner
tunnel binding token the server minted (the
``X-Omnigent-Runner-Tunnel-Token`` header). These tests exercise the
fallback that lets such a request READ *its own* session
(``GET /v1/sessions/{id}``, ``.../agent`` and ``.../agent/contents``) when
the token's ``token_bound_runner_id`` equals the session's persisted
``runner_id`` — and that every mismatch fails closed exactly as the
unauthenticated user path does.

Full middleware -> route -> store pipeline against an auth-enabled app
(``UnifiedAuthProvider`` in strict header mode + a real permission store),
mirroring ``test_sessions_permissions.py``.
"""

from __future__ import annotations

import json as _json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio


# ── Fixtures ─────────────────────────────────────────────────


@pytest.fixture()
def auth_app(
    runtime_init: None,
    db_uri: str,
    tmp_path: Path,
) -> FastAPI:
    """Auth-enabled app (strict header mode + permission store).

    Same multi-user posture as ``test_sessions_permissions.auth_app``:
    requests without ``X-Forwarded-Email`` carry no identity and are
    rejected with 401 unless the managed-runner fallback admits them.

    :param runtime_init: Initializes the runtime with a mock LLM.
    :param db_uri: Test database URI.
    :param tmp_path: Pytest temporary directory fixture.
    """
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the auth-enabled app (mirrors permissions test)."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


# ── Helpers ──────────────────────────────────────────────────


async def _create_session_as(
    client: httpx.AsyncClient,
    user: str,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Create a session owned by *user* and return its snapshot dict."""
    bundle = build_agent_bundle(name="test-agent")
    metadata: dict[str, Any] = {}
    if title is not None:
        metadata["title"] = title
    headers = {"X-Forwarded-Email": user}
    resp = await client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers=headers,
    )
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    session_id = resp.json()["session_id"]
    snap = await client.get(f"/v1/sessions/{session_id}", headers=headers)
    assert snap.status_code == 200, f"session snapshot failed: {snap.text}"
    return snap.json()


def _bind_runner_token(db_uri: str, session_id: str, token: str) -> str:
    """Pin the session's ``runner_id`` to ``token_bound_runner_id(token)``.

    Mirrors what the server does when it mints a binding token for a
    managed sandbox runner. Returns the bound runner id.
    """
    runner_id = token_bound_runner_id(token)
    store = SqlAlchemyConversationStore(db_uri)
    store.replace_runner_id(session_id, runner_id)
    return runner_id


# ── Tests: fallback grants READ on the runner's own session ──


async def test_managed_runner_token_grants_read_on_own_session(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A valid binding token matching the session runner_id gets READ on
    get_session AND agent AND agent/contents — with no user identity."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "managed-runner-secret-token-abc123"
    _bind_runner_token(db_uri, session_id, token)

    runner_headers = {RUNNER_TUNNEL_TOKEN_HEADER: token}

    # GET /v1/sessions/{id}
    snap = await auth_client.get(f"/v1/sessions/{session_id}", headers=runner_headers)
    assert snap.status_code == 200, snap.text
    assert snap.json()["id"] == session_id

    # GET /v1/sessions/{id}/agent
    agent = await auth_client.get(f"/v1/sessions/{session_id}/agent", headers=runner_headers)
    assert agent.status_code == 200, agent.text
    assert agent.json()["name"] == "test-agent"

    # GET /v1/sessions/{id}/agent/contents
    contents = await auth_client.get(
        f"/v1/sessions/{session_id}/agent/contents", headers=runner_headers
    )
    assert contents.status_code == 200, contents.text
    assert contents.headers["content-type"].startswith("application/gzip")
    assert contents.content  # non-empty bundle bytes


# ── Tests: every mismatch fails closed ───────────────────────


async def test_mismatched_runner_token_is_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A token whose digest != the session's runner_id is rejected (no
    user identity present)."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    # Bind to one token, present a DIFFERENT one.
    _bind_runner_token(db_uri, session_id, "the-real-bound-token")
    wrong_headers = {RUNNER_TUNNEL_TOKEN_HEADER: "some-other-token-not-bound"}

    for path in (
        f"/v1/sessions/{session_id}",
        f"/v1/sessions/{session_id}/agent",
        f"/v1/sessions/{session_id}/agent/contents",
    ):
        resp = await auth_client.get(path, headers=wrong_headers)
        assert resp.status_code in (401, 403), f"{path}: {resp.status_code} {resp.text}"


async def test_token_for_other_session_cannot_cross_access(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A token bound to session A's runner_id must not read session B."""
    sess_a = await _create_session_as(auth_client, "alice@example.com")
    sess_b = await _create_session_as(auth_client, "bob@example.com")
    token_a = "alices-runner-token"
    _bind_runner_token(db_uri, sess_a["id"], token_a)
    # Session B is bound to a different runner.
    _bind_runner_token(db_uri, sess_b["id"], "bobs-runner-token")

    # Alice's runner token is valid for A but must be rejected for B.
    headers_a = {RUNNER_TUNNEL_TOKEN_HEADER: token_a}
    ok = await auth_client.get(f"/v1/sessions/{sess_a['id']}", headers=headers_a)
    assert ok.status_code == 200, ok.text
    cross = await auth_client.get(f"/v1/sessions/{sess_b['id']}", headers=headers_a)
    assert cross.status_code in (401, 403), cross.text


async def test_missing_token_without_user_still_401(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """No user identity and no/empty token still 401s (no regression)."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "a-bound-token")

    # No token header at all.
    no_token = await auth_client.get(f"/v1/sessions/{session_id}")
    assert no_token.status_code == 401, no_token.text

    # Empty token header.
    empty = await auth_client.get(
        f"/v1/sessions/{session_id}", headers={RUNNER_TUNNEL_TOKEN_HEADER: "   "}
    )
    assert empty.status_code == 401, empty.text


async def test_valid_token_but_session_has_no_bound_runner_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A token presented against a session with no bound runner_id fails
    closed — there is nothing to match against."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    # Deliberately do NOT bind a runner_id.
    headers = {RUNNER_TUNNEL_TOKEN_HEADER: "any-token"}
    resp = await auth_client.get(f"/v1/sessions/{session_id}", headers=headers)
    assert resp.status_code in (401, 403), resp.text


# ── Tests: user-auth path is unchanged ───────────────────────


async def test_user_auth_path_unchanged(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An authenticated owner still reads their session; a stranger is
    still denied — with or without a runner binding present."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "alices-runner-token")

    # Owner reads fine.
    owner = await auth_client.get(
        f"/v1/sessions/{session_id}", headers={"X-Forwarded-Email": "alice@example.com"}
    )
    assert owner.status_code == 200, owner.text

    # A different authenticated user has no grant -> 404 (existence hidden).
    stranger = await auth_client.get(
        f"/v1/sessions/{session_id}", headers={"X-Forwarded-Email": "mallory@example.com"}
    )
    assert stranger.status_code in (403, 404), stranger.text

    # A stranger cannot widen access by ALSO presenting an unrelated token.
    stranger_with_token = await auth_client.get(
        f"/v1/sessions/{session_id}",
        headers={
            "X-Forwarded-Email": "mallory@example.com",
            RUNNER_TUNNEL_TOKEN_HEADER: "mallorys-token",
        },
    )
    assert stranger_with_token.status_code in (403, 404), stranger_with_token.text
