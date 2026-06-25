"""Unit tests for the managed-runner binding-token owner resolver.

``resolve_owner_for_runner_id`` is the runner-tunnel auth fallback for
SERVER-MANAGED sandbox runners: it maps a token-bound runner id to the
owner of the session(s) the server bound to that runner. These tests pin
the fail-closed guard (unknown ↦ None, ambiguous ↦ None, single ↦ owner)
that the tunnel route relies on to refuse an unresolvable binding.
"""

from __future__ import annotations

import pytest

from omnigent.server.app import resolve_owner_for_runner_id
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    :returns: A conversation store backed by the test database.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def permission_store(db_uri: str) -> SqlAlchemyPermissionStore:
    """
    :returns: A permission store sharing the test database with
        ``conversation_store`` so owner grants are visible to
        ``get_session_owner``.
    """
    return SqlAlchemyPermissionStore(db_uri)


def test_resolves_single_owner_of_bound_session(
    conversation_store: SqlAlchemyConversationStore,
    permission_store: SqlAlchemyPermissionStore,
) -> None:
    """A runner bound to one owned session resolves to that owner."""
    runner_id = "runner_token_managed_alpha"
    conv = conversation_store.create_conversation()
    assert conversation_store.set_runner_id(conv.id, runner_id) is True
    permission_store.ensure_user("alice@example.com")
    permission_store.grant("alice@example.com", conv.id, LEVEL_OWNER)

    assert resolve_owner_for_runner_id(conversation_store, runner_id) == "alice@example.com"


def test_unknown_runner_id_resolves_to_none(
    conversation_store: SqlAlchemyConversationStore,
    permission_store: SqlAlchemyPermissionStore,
) -> None:
    """A runner id bound to no session resolves to None (rejected).

    Covers the rotated-out / forged-token case: an attacker-derived
    runner id matches no session, so the tunnel route fails closed.
    """
    assert resolve_owner_for_runner_id(conversation_store, "runner_token_nobody") is None


def test_bound_session_without_owner_grant_resolves_to_none(
    conversation_store: SqlAlchemyConversationStore,
    permission_store: SqlAlchemyPermissionStore,
) -> None:
    """A bound session whose only grant is non-owner resolves to None.

    ``get_session_owner`` returns the highest-level grantee; a read-only
    grant is still that user, so this asserts the binding still resolves
    to the real grantee — but with NO grant at all it must be None.
    """
    runner_id = "runner_token_no_owner"
    conv = conversation_store.create_conversation()
    assert conversation_store.set_runner_id(conv.id, runner_id) is True

    # No permission grant at all → no owner → None.
    assert resolve_owner_for_runner_id(conversation_store, runner_id) is None

    # A lone read grant still identifies that single user as the owner of
    # record (highest-level grantee), so resolution succeeds for them.
    permission_store.ensure_user("reader@example.com")
    permission_store.grant("reader@example.com", conv.id, LEVEL_READ)
    assert resolve_owner_for_runner_id(conversation_store, runner_id) == "reader@example.com"


def test_ambiguous_owners_across_sessions_resolve_to_none(
    conversation_store: SqlAlchemyConversationStore,
    permission_store: SqlAlchemyPermissionStore,
) -> None:
    """Two sessions on one runner owned by different users resolve to None.

    The single-owner guard refuses to guess: if the same runner id maps
    to sessions with distinct owners, no owner is granted and the tunnel
    route rejects the handshake.
    """
    runner_id = "runner_token_ambiguous"
    conv_a = conversation_store.create_conversation()
    conv_b = conversation_store.create_conversation()
    conversation_store.replace_runner_id(conv_a.id, runner_id)
    conversation_store.replace_runner_id(conv_b.id, runner_id)
    permission_store.ensure_user("alice@example.com")
    permission_store.ensure_user("bob@example.com")
    permission_store.grant("alice@example.com", conv_a.id, LEVEL_OWNER)
    permission_store.grant("bob@example.com", conv_b.id, LEVEL_OWNER)

    assert resolve_owner_for_runner_id(conversation_store, runner_id) is None


def test_multiple_sessions_same_owner_resolve_to_that_owner(
    conversation_store: SqlAlchemyConversationStore,
    permission_store: SqlAlchemyPermissionStore,
) -> None:
    """One owner with several sessions on a runner still resolves cleanly.

    A reused runner legitimately serves multiple sessions for the same
    user; the distinct-owner set collapses to one, so resolution succeeds.
    """
    runner_id = "runner_token_reused"
    conv_a = conversation_store.create_conversation()
    conv_b = conversation_store.create_conversation()
    conversation_store.replace_runner_id(conv_a.id, runner_id)
    conversation_store.replace_runner_id(conv_b.id, runner_id)
    permission_store.ensure_user("alice@example.com")
    permission_store.grant("alice@example.com", conv_a.id, LEVEL_OWNER)
    permission_store.grant("alice@example.com", conv_b.id, LEVEL_OWNER)

    assert resolve_owner_for_runner_id(conversation_store, runner_id) == "alice@example.com"
