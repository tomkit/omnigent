"""Integration tests for the managed-runner REST auth fallback on WRITE routes.

PR #14 added the runner-token fallback for the three session GET routes only,
so a server-managed sandbox runner (no user credential — it proves identity
solely via the ``X-Omnigent-Runner-Tunnel-Token`` binding token) still 401'd on
every WRITE callback. That broke the core loop: ``POST /v1/sessions/{id}/events``
401'd, so the agent could not stream output back and tasks never completed.

These tests exercise the consolidated authorizer
(``_authorize_session_with_runner_fallback``) now wired into the same-session,
``<= LEVEL_EDIT`` write paths:

- ``POST /v1/sessions/{id}/events`` (the 401 that broke task completion),
- the non-privileged ``PATCH /v1/sessions/{id}`` fields (title/labels/model/
  effort) — but NOT the owner-only ``archived`` branch,
- ``POST /v1/sessions/{id}/policies/evaluate`` at READ,

and pin the security cap: a binding token can never satisfy a MANAGE/OWNER
level even when the token itself is valid for the session.

Full middleware -> route -> store pipeline against an auth-enabled app
(``UnifiedAuthProvider`` in strict header mode + a real permission store),
mirroring ``test_sessions_managed_runner_rest_auth.py``.
"""

from __future__ import annotations

import asyncio
import json as _json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import (
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
)
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER, LEVEL_READ
from omnigent.server.routes.sessions import (
    _ALLOWED_EVENT_TYPES,
    _APPROVAL_TYPE,
    _RUNNER_OWNED_EVENT_TYPES,
    _USER_CONTROL_EVENT_TYPES,
    _authorize_session_with_runner_fallback,
)
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

    Requests without ``X-Forwarded-Email`` carry no identity and are
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
    snapshot: dict[str, Any] = snap.json()
    return snapshot


def _bind_runner_token(db_uri: str, session_id: str, token: str) -> str:
    """Pin the session's ``runner_id`` to ``token_bound_runner_id(token)``.

    Mirrors what the server does when it mints a binding token for a
    managed sandbox runner. Returns the bound runner id.
    """
    runner_id = token_bound_runner_id(token)
    store = SqlAlchemyConversationStore(db_uri)
    store.replace_runner_id(session_id, runner_id)
    return runner_id


# A runner-owned event: ``external_output_text_delta`` is a pure streaming
# observation (publish-only, returns 202, no DB writes, no bound-runner
# dependency) — exactly the kind of callback the in-sandbox runner emits to
# report the agent's work. The managed-runner token fallback IS permitted for
# it. A 202 proves auth passed via the fallback; a 401 proves it was rejected.
_RUNNER_OWNED_EVENT = {
    "type": "external_output_text_delta",
    "data": {"delta": "stream chunk from the runner"},
}


# A user-control event: ``approval`` resolves a human elicitation / approval
# gate and applies deferred policy-ask writes. The runner token fallback must
# NEVER satisfy it — it requires a real user identity.
def _approval_event(elicitation_id: str) -> dict[str, Any]:
    return {"type": "approval", "data": {"elicitation_id": elicitation_id, "action": "accept"}}


# A bogus (unclassified) event type: not in ``_RUNNER_OWNED_EVENT_TYPES``, so it
# must fail safe to user-only auth. Used to pin the fail-safe default.
_BOGUS_EVENT = {"type": "definitely-not-a-real-event-type", "data": {}}


# ── post_event: the WRITE route that broke task completion ───


async def test_managed_runner_token_grants_edit_on_post_event(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A valid binding token clears auth on POST .../events for its OWN
    session (no user identity) when the event is runner-owned — the fix for
    the 96x 401."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "managed-runner-secret-token-events"
    _bind_runner_token(db_uri, session_id, token)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )
    # 202 (event accepted) proves the token fallback authorized the runner-owned
    # streaming callback; a regression would be 401.
    assert resp.status_code == 202, resp.text


async def test_post_event_mismatched_token_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A token whose digest != the session's runner_id is rejected on the
    write path (no user identity present)."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "the-real-bound-token")

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "some-other-token-not-bound"},
    )
    assert resp.status_code == 401, resp.text


async def test_post_event_cross_session_token_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A token bound to session A must not write to session B."""
    sess_a = await _create_session_as(auth_client, "alice@example.com")
    sess_b = await _create_session_as(auth_client, "bob@example.com")
    token_a = "alices-runner-token"
    _bind_runner_token(db_uri, sess_a["id"], token_a)
    _bind_runner_token(db_uri, sess_b["id"], "bobs-runner-token")

    # Valid for A (token fallback authorizes -> 202), rejected for B (401).
    ok = await auth_client.post(
        f"/v1/sessions/{sess_a['id']}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token_a},
    )
    assert ok.status_code == 202, ok.text
    cross = await auth_client.post(
        f"/v1/sessions/{sess_b['id']}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token_a},
    )
    assert cross.status_code == 401, cross.text


async def test_post_event_missing_or_empty_token_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """No user identity and no/empty token still 401s on the write path."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "a-bound-token")

    no_token = await auth_client.post(
        f"/v1/sessions/{session_id}/events", json=_RUNNER_OWNED_EVENT
    )
    assert no_token.status_code == 401, no_token.text

    empty = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "   "},
    )
    assert empty.status_code == 401, empty.text


async def test_post_event_user_auth_path_unchanged(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Owner still clears auth on the write path; a stranger is still
    denied and cannot widen access by ALSO presenting an unrelated token."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "alices-runner-token")

    owner = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert owner.status_code == 202, owner.text  # user path unchanged

    stranger = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={"X-Forwarded-Email": "mallory@example.com"},
    )
    assert stranger.status_code in (403, 404), stranger.text

    stranger_with_token = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_RUNNER_OWNED_EVENT,
        headers={
            "X-Forwarded-Email": "mallory@example.com",
            RUNNER_TUNNEL_TOKEN_HEADER: "mallorys-token",
        },
    )
    assert stranger_with_token.status_code in (403, 404), stranger_with_token.text


# ── post_event: user-control (`approval`) must require a real user ──


async def test_post_event_approval_rejected_for_runner_token(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A managed-runner token POSTing an `approval` event is rejected (401)
    and the parked elicitation is NOT resolved — the rejection lands BEFORE
    `_resolve_elicitation` / `_apply_pending_policy_ask_writes` run."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "managed-runner-secret-token-approval"
    _bind_runner_token(db_uri, session_id, token)

    # Park a server-side elicitation Future owned by THIS session. The approval
    # branch would set its result via `_resolve_elicitation` if it ran.
    elicitation_id = "elicit_runner_must_not_resolve"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{session_id}/events",
            json=_approval_event(elicitation_id),
            headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
        )
        assert resp.status_code == 401, resp.text
        # Side effect NOT applied: the human verdict Future is still pending.
        assert not future.done(), "runner token resolved a human approval gate"
    finally:
        _harness_elicitation_registry.pop(elicitation_id, None)
        _harness_elicitation_owners.pop(elicitation_id, None)
        if not future.done():
            future.cancel()


async def test_post_event_approval_allowed_for_real_user(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A real owner CAN still resolve an approval via post_event (unchanged):
    the parked elicitation Future is resolved — positive control proving the
    rejection above is specific to the credential-less runner."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    # A binding is present, but the owner uses their user identity.
    _bind_runner_token(db_uri, session_id, "alices-runner-token")

    elicitation_id = "elicit_user_resolves_ok"
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    _harness_elicitation_registry[elicitation_id] = future
    _harness_elicitation_owners[elicitation_id] = session_id
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{session_id}/events",
            json=_approval_event(elicitation_id),
            headers={"X-Forwarded-Email": "alice@example.com"},
        )
        assert resp.status_code == 202, resp.text
        # The owner's verdict resolved the parked Future.
        assert future.done(), "owner approval did not resolve the elicitation"
    finally:
        _harness_elicitation_registry.pop(elicitation_id, None)
        _harness_elicitation_owners.pop(elicitation_id, None)
        if not future.done():
            future.cancel()


async def test_post_event_unknown_type_fails_safe_to_user_only(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An unclassified event type defaults to user-only auth: a runner token
    is rejected (401, NOT 400), while a real user reaches the route's
    unknown-type validation (400)."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "managed-runner-secret-token-unknown"
    _bind_runner_token(db_uri, session_id, token)

    # Token + unclassified type -> fail safe to user-only -> 401 (not 400).
    runner = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_BOGUS_EVENT,
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )
    assert runner.status_code == 401, runner.text

    # Real user + unclassified type -> auth passes -> 400 unknown event type.
    user = await auth_client.post(
        f"/v1/sessions/{session_id}/events",
        json=_BOGUS_EVENT,
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert user.status_code == 400, user.text


# ── Classification invariant: allowlist partitions the allowed set ──


async def test_event_type_classification_partitions_allowed_types() -> None:
    """The runner-owned allowlist and the user-control set are disjoint and
    together partition every allowed event type — so a NEW allowed type must
    be consciously classified (else it fails safe to user-only AND trips
    this test)."""
    assert _APPROVAL_TYPE in _USER_CONTROL_EVENT_TYPES
    assert _APPROVAL_TYPE not in _RUNNER_OWNED_EVENT_TYPES
    assert _RUNNER_OWNED_EVENT_TYPES.isdisjoint(_USER_CONTROL_EVENT_TYPES)
    assert (_RUNNER_OWNED_EVENT_TYPES | _USER_CONTROL_EVENT_TYPES) == _ALLOWED_EVENT_TYPES


# ── update_session: non-privileged EDIT fields get the fallback ──


async def test_managed_runner_token_grants_edit_on_update_title(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A valid binding token may PATCH a non-privileged field (title) on its
    own session and the write actually lands."""
    sess = await _create_session_as(auth_client, "alice@example.com", title="old")
    session_id = sess["id"]
    token = "managed-runner-secret-token-patch"
    _bind_runner_token(db_uri, session_id, token)

    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "new-title-from-runner"},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "new-title-from-runner"


async def test_update_session_mismatched_token_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A mismatched token is rejected on the PATCH edit path."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "the-real-bound-token")

    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"title": "nope"},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "some-other-token-not-bound"},
    )
    assert resp.status_code == 401, resp.text


async def test_update_session_archived_branch_has_no_runner_fallback(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The owner-only ``archived`` branch must NOT accept a binding token,
    even one validly bound to the session — archiving stays owner-only."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "validly-bound-but-still-not-owner"
    _bind_runner_token(db_uri, session_id, token)

    resp = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )
    assert resp.status_code == 401, resp.text

    # And the owner CAN still archive — the privileged path is unchanged.
    owner = await auth_client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
        headers={"X-Forwarded-Email": "alice@example.com"},
    )
    assert owner.status_code == 200, owner.text


# ── evaluate_policy: READ-level fallback ─────────────────────


async def test_managed_runner_token_grants_read_on_evaluate_policy(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A valid binding token clears auth on POST .../policies/evaluate at
    READ for its own session."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "managed-runner-secret-token-policy"
    _bind_runner_token(db_uri, session_id, token)

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={"event": "tool_call", "data": {"name": "noop"}},
        headers={
            RUNNER_TUNNEL_TOKEN_HEADER: token,
            "Content-Type": "application/json",
        },
    )
    # Whatever the policy verdict, auth must have PASSED (not 401/403).
    assert resp.status_code not in (401, 403), resp.text


async def test_evaluate_policy_mismatched_token_rejected(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A mismatched token is rejected on policy evaluate."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    _bind_runner_token(db_uri, session_id, "the-real-bound-token")

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json={"event": "tool_call", "data": {"name": "noop"}},
        headers={
            RUNNER_TUNNEL_TOKEN_HEADER: "some-other-token-not-bound",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401, resp.text


# ── Security cap: a token can never satisfy MANAGE / OWNER ────


async def test_create_session_stays_user_only(
    auth_client: httpx.AsyncClient,
) -> None:
    """create_session is NOT routed through the fallback — a token-only
    request (no user) cannot create a session."""
    bundle = build_agent_bundle(name="test-agent")
    resp = await auth_client.post(
        "/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "some-runner-token"},
    )
    assert resp.status_code == 401, resp.text


async def test_grant_permission_stays_user_only(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """grant_permission (PUT .../permissions) is NOT routed through the
    fallback — a token validly bound to the session still cannot grant."""
    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "validly-bound-runner-token"
    _bind_runner_token(db_uri, session_id, token)

    resp = await auth_client.put(
        f"/v1/sessions/{session_id}/permissions",
        json={"user_id": "mallory@example.com", "level": LEVEL_EDIT},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
    )
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("level", [LEVEL_MANAGE, LEVEL_OWNER])
async def test_helper_caps_privileged_level_even_with_valid_token(
    auth_app: FastAPI,
    auth_client: httpx.AsyncClient,
    db_uri: str,
    level: int,
) -> None:
    """Focused cap test: the consolidated helper rejects any level > EDIT
    with 401 BEFORE consulting the proof — even when the token is valid for
    the session — while the SAME token still satisfies READ/EDIT."""
    from starlette.requests import Request

    from omnigent.server.auth import UnifiedAuthProvider

    sess = await _create_session_as(auth_client, "alice@example.com")
    session_id = sess["id"]
    token = "a-genuinely-bound-runner-token"
    _bind_runner_token(db_uri, session_id, token)

    auth_provider = UnifiedAuthProvider(source="header", local_single_user=False)
    permission_store = SqlAlchemyPermissionStore(db_uri)
    conversation_store = SqlAlchemyConversationStore(db_uri)

    def _request_with_token() -> Request:
        # No X-Forwarded-Email -> no user identity; only the binding token.
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/v1/sessions/{session_id}",
            "headers": [(RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(), token.encode())],
            "query_string": b"",
        }
        return Request(scope)

    # The cap rejects MANAGE / OWNER with 401 even though the token is valid.
    with pytest.raises(OmnigentError) as exc_info:
        await _authorize_session_with_runner_fallback(
            _request_with_token(),
            session_id,
            level=level,
            auth_provider=auth_provider,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
    assert exc_info.value.code == ErrorCode.UNAUTHORIZED

    # The SAME token DOES satisfy READ and EDIT — proving it is genuinely
    # valid and only the cap blocks the privileged levels.
    for ok_level in (LEVEL_READ, LEVEL_EDIT):
        access = await _authorize_session_with_runner_fallback(
            _request_with_token(),
            session_id,
            level=ok_level,
            auth_provider=auth_provider,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        assert access.level == ok_level
        assert access.conversation is not None
        assert access.conversation.id == session_id
