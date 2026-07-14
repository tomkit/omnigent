"""E2E: a dormant resumable managed host keeps the composer open.

When a session is bound to a managed host whose sandbox idle-stopped, the
open-session view must NOT dead-end on the ``host_offline`` reconnect banner:
the host is resumable, so the composer stays ENABLED and its placeholder tells
the user the next message will bring the sandbox back online. This drives the
``host_asleep`` liveness variant (see ``web/src/hooks/useSessionLiveness.ts``
row 3) end to end — host-bound + ``host_online=false`` + ``host_resumable=true``
+ the runner offline, and outside the startup grace.

The server fixture seeds a normal runner-bound ``hello_world`` session; the
harness has no real stop/resume managed provider, so the browser's view of the
session is patched into the ``host_asleep`` shape via route interception:

- ``GET /v1/sessions/{id}`` (snapshot) → ``host_id`` set, ``host_resumable``
  true, and an old ``created_at`` (so the session is past the startup grace, or
  a fresh session reads as ``starting`` and masks ``host_asleep``).
- ``GET /v1/sessions?...`` (sidebar list) → the session is dropped from the
  list so the open-session row resolves off-sidebar straight from the patched
  snapshot (host-bound), instead of the real runner-bound sidebar row.
- ``GET /health`` → the session reports ``runner_online=false`` +
  ``host_online=false``; the open-session poll overrides the WS stream.
- ``WS /v1/sessions/updates`` → blocked so a stream push can't re-add the
  session to the sidebar or revert its liveness to the real (online) values.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

_ASLEEP_PLACEHOLDER = (
    "Session host is offline — your next message brings its sandbox "
    "back online (can take a minute or two)"
)
_FAKE_HOST_ID = "host_test_managed"
# Unix seconds well before now so the session is outside the startup grace
# (STARTING_GRACE_S) — see useSessionLiveness row 2.
_OLD_CREATED_AT = 1_700_000_000


def _force_host_asleep(
    page: Page,
    session_id: str,
    *,
    resumable: bool = True,
    sandbox_provider: str | None = None,
) -> None:
    """Patch the browser's view of ``session_id`` into the host_asleep state.

    Registered before navigation: three HTTP route patches plus one WS block.

    :param page: Playwright page before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :param resumable: Value for the snapshot's ``host_resumable`` — ``True`` for
        a resume-in-place provider (Daytona), ``False`` for a relaunch-only
        managed provider (E2B/Modal).
    :param sandbox_provider: Value for the snapshot's ``sandbox_provider`` — a
        non-null provider marks a MANAGED host, which keeps the session
        host_asleep (relaunch-on-message) even when ``resumable`` is False.
    """

    def _patch_snapshot(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != f"/v1/sessions/{session_id}":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["host_id"] = payload.get("host_id") or _FAKE_HOST_ID
        payload["host_resumable"] = resumable
        payload["sandbox_provider"] = sandbox_provider
        # Age the session out of the startup grace (STARTING_GRACE_S): a
        # freshly-created session whose runner has never been seen online reads
        # as `starting` (cold-boot) and would mask the host_asleep row.
        payload["created_at"] = _OLD_CREATED_AT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_list(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/v1/sessions":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(rows, list):
            payload["data"] = [
                r for r in rows if not (isinstance(r, dict) and r.get("id") == session_id)
            ]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    def _patch_health(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != "/health":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        offline = {"runner_online": False, "host_online": False}
        # Plural shape used by the open-session fallback poll:
        # {"sessions": {"<id>": {...}}}.
        if isinstance(payload.get("sessions"), dict):
            payload["sessions"][session_id] = offline
        # Singular shape ({"session": {...}}) for the session_id= variant.
        if isinstance(payload.get("session"), dict):
            payload["session"] = {**payload["session"], **offline}
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    # Snapshot route registered last so it wins for /v1/sessions/{id} (Playwright
    # matches most-recently-registered first); the list/health handlers fall
    # through via continue_() for anything they don't own.
    page.route(re.compile(r"/v1/sessions(\?|$)"), _patch_list)
    page.route(re.compile(r"/health(\?|$)"), _patch_health)
    page.route(re.compile(rf"/v1/sessions/{re.escape(session_id)}(\?|$)"), _patch_snapshot)
    page.route_web_socket(re.compile(r"/v1/sessions/updates"), lambda ws: None)


def test_host_asleep_keeps_composer_open_with_resume_placeholder(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """In ``host_asleep`` the composer stays enabled with the wake placeholder.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the host_asleep shape.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _force_host_asleep(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=15_000)
    # The placeholder is the host_asleep tell: composer open, message resumes
    # the sandbox host. NOT the host_offline "Session offline — reconnect"
    # dead-end.
    expect(composer).to_have_attribute("placeholder", _ASLEEP_PLACEHOLDER, timeout=15_000)
    # Key behavior: a resumable dormant host keeps the composer usable.
    expect(composer).not_to_be_disabled()


def test_managed_nonresumable_host_keeps_composer_open_for_relaunch(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A dormant MANAGED, non-resume-in-place host (e.g. E2B) stays host_asleep.

    E2B/Modal have a hard lifetime cap and no persistent volume, so
    ``host_resumable`` is false — but the server still RELAUNCHES a fresh sandbox
    seeded from prior items on the next message (``relaunch_managed_host``), so
    the composer must stay ENABLED (host_asleep), NOT dead-end on the
    ``host_offline`` "Session offline — reconnect below" banner. This is the web
    half of that contract: an offline host with a non-null ``sandbox_provider``
    but ``host_resumable=false`` is driven to ``host_asleep`` via
    ``LivenessRow.host_managed`` (see ``web/src/hooks/useSessionLiveness.ts`` row
    3) rather than ``host_offline`` — the bug that made an expired E2B session
    look like it needed the user's laptop back online.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser view is patched to the managed-but-not-resumable
        offline shape (``host_resumable=false`` + ``sandbox_provider="e2b"``).
    :returns: None.
    """
    base_url, session_id = seeded_session
    _force_host_asleep(page, session_id, resumable=False, sandbox_provider="e2b")

    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    # Managed + offline + NOT resume-in-place → still host_asleep (relaunch on
    # the next message), so the composer keeps the wake placeholder rather than
    # the host_offline "Session offline — reconnect below" dead-end.
    expect(composer).to_have_attribute("placeholder", _ASLEEP_PLACEHOLDER, timeout=30_000)
    # Key behavior of the fix: a dormant MANAGED host (E2B) keeps the composer
    # usable so the next message can trigger the server-side relaunch.
    expect(composer).not_to_be_disabled()
