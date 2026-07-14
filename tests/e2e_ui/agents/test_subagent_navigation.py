"""UI journey: spin up two sub-agents, see them in the Agents tab, and
navigate into one and back to the parent.

The user asks the joke director (the session's top-level agent) to get a
joke from each of its two comedian sub-agents. The director's prompt
forbids telling jokes itself; it must `sys_session_send` to `comic_one`
and `comic_two`, which run as two separate child sessions and auto-wake
the director to relay their jokes back into the SPA.

This single journey covers two requested behaviors, kept together so the
heavy multi-agent spin-up (one dispatch turn + two sub-agent turns + the
auto-wake continuation) runs only once:

1. After the relay, the right-rail Agents tab lists BOTH comedians as
   sub-agent rows and its count badge grows past the lone-agent ``1``.
2. Clicking a sub-agent row swaps the chat page to that child's own
   conversation (``/c/<child-id>``), and the header carries a
   "Back to parent session" affordance that returns to the parent.

The load-bearing assertions are the per-run nonces, which exist ONLY in
each comedian's prompt (embedded at registration by the
`joke_subagents_session` fixture): a nonce in the director's bubble can
only have reached it via the real parent -> sub-agent -> inbox -> parent
-> SSE -> UI pipeline, never from a model-invented joke.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.agents.conftest import JokeSubagentsSession
from tests.e2e_ui.conftest import open_right_rail

_COMPOSER = "Ask the agent anything…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SUBAGENT_ROW = '[data-testid="subagent-row"]'
_SUBAGENT_STATUS_DOT = '[data-testid="subagent-status-dot"]'

# One relay = dispatch turn + two sub-agent turns + the auto-wake
# continuation, several serial real-LLM calls, so the nonce assertions
# get a generous budget (matches test_two_agent_chat.py).
_RELAY_TIMEOUT_MS = 240_000


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    # Initial SPA cold-load gate (bundle parse + hydrate + first session fetch)
    # can run past the 15s/30s ceiling when the e2e_ui shard is under xdist load,
    # which is what made this test flaky. Give it the same 60s budget the
    # browser-tab de-flake uses; to_be_visible auto-waits, so it returns the
    # instant the composer renders and the larger timeout only raises the ceiling.
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill(text)
    # Web-first gate before the click so it doesn't race a not-yet-mounted /
    # not-yet-enabled Send control (the button enables once the composer has text).
    send_button = page.get_by_role("button", name="Send", exact=True)
    expect(send_button).to_be_visible(timeout=30_000)
    send_button.click()


# Nightly: this exercises the full dispatch + two-child + auto-wake UI
# journey. Scripted LLM queues keep it deterministic, while the nightly
# marker keeps the heavier multi-session browser coverage off the PR gate.
@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_two_joke_subagents_appear_and_navigate(
    page: Page,
    joke_subagents_session: JokeSubagentsSession,
) -> None:
    chat = joke_subagents_session
    parent_url = f"{chat.base_url}/c/{chat.session_id}"
    page.goto(parent_url)

    # Ask the director to gather a joke from each comedian sub-agent.
    _send(
        page,
        "Please get one joke from comic_one and one joke from comic_two, "
        "then tell me both jokes exactly as they said them, including each "
        f"joke code. Routing marker: {chat.routing_token}",
    )

    # Both comedians' jokes (identified by their nonces) reached the
    # parent transcript — proof that both sub-agents really ran and
    # relayed back, not that the director invented jokes.
    expect(page.locator(_ASSISTANT, has_text=chat.code_one).first).to_be_visible(
        timeout=_RELAY_TIMEOUT_MS
    )
    expect(page.locator(_ASSISTANT, has_text=chat.code_two).first).to_be_visible(
        timeout=_RELAY_TIMEOUT_MS
    )

    # (1) The Agents tab lists BOTH comedians as sub-agent rows. The rail
    # defaults closed per session, so it is expanded first; lookups are
    # scoped to the desktop "Workspace" rail so they don't match the
    # hidden mobile drawer that mirrors the same testids.
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    agents_tab = rail.get_by_role("tab", name=re.compile("^Agents"))
    # The rail's tabs can mount a beat after the rail container, so wait for the
    # Agents tab (web-first, auto-waits) before clicking — otherwise the click
    # races a not-yet-rendered tab under xdist load.
    expect(agents_tab).to_be_visible(timeout=30_000)
    agents_tab.click()
    rows = rail.locator(_SUBAGENT_ROW)
    expect(rows).to_have_count(2, timeout=30_000)
    # Row text / status dot / count badge fill in as the two sub-agents register;
    # bump these off the 15s default to 30s so a slow relay render under xdist
    # load can't trip them (all auto-wait, so the happy path is unaffected).
    expect(rail).to_contain_text("comic_one", timeout=30_000)
    expect(rail).to_contain_text("comic_two", timeout=30_000)
    # Each row carries a status dot and its own child session id.
    expect(rows.first.locator(_SUBAGENT_STATUS_DOT)).to_be_visible(timeout=30_000)
    # The count badge grew past the lone-agent baseline of 1 (main + 2).
    expect(agents_tab).to_contain_text("3", timeout=30_000)

    # (2) Clicking a sub-agent row swaps the chat to that child's session.
    target_row = rows.first
    # Web-first gate before the raw get_attribute snapshot and the click: wait
    # for the row to be visible AND to carry a populated data-child-session-id,
    # so neither the (non-waiting) snapshot reads a half-mounted row (returning
    # None) nor the click races a not-yet-rendered row.
    expect(target_row).to_be_visible(timeout=30_000)
    expect(target_row).to_have_attribute(
        "data-child-session-id", re.compile(r".+"), timeout=30_000
    )
    child_session_id = target_row.get_attribute("data-child-session-id")
    assert child_session_id, "subagent row is missing data-child-session-id"
    target_row.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{child_session_id}")))

    # The header carries the back-to-parent affordance: a "Back to parent
    # session" link pointing at the parent conversation, beside the
    # "Sub-agent" identity caption.
    back_link = page.get_by_role("link", name="Back to parent session")
    expect(back_link).to_be_visible(timeout=30_000)
    # Header just mounted after the client-side nav; keep the href / identity
    # caption assertions off the 15s default (30s) so they can settle under load.
    expect(back_link).to_have_attribute(
        "href", re.compile(re.escape(f"/c/{chat.session_id}")), timeout=30_000
    )
    expect(page.get_by_text("Sub-agent", exact=True)).to_be_visible(timeout=30_000)

    # Following it returns to the parent session.
    back_link.click()
    page.wait_for_url(re.compile(re.escape(f"/c/{chat.session_id}")))
