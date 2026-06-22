"""Shared conversion between native-harness hooks and Omnigent policy events.

Both Claude Code and Codex expose a command-hook system whose
``PreToolUse`` / ``PostToolUse`` payloads use the same field names
(``hook_event_name``, ``tool_name``, ``tool_input``, ``tool_output``)
and whose ``UserPromptSubmit`` payload carries the user prompt under
``prompt``. This module owns the harness-neutral translation between
that hook shape and the server's proto-compatible ``EvaluationRequest``
/ ``EvaluationResponse`` schema served by
``POST /v1/sessions/{id}/policies/evaluate``, so the per-harness hook
entrypoints (:mod:`omnigent.claude_native_hook`,
:mod:`omnigent.codex_native_hook`) share one implementation.

The output contract differs by hook event: ``PreToolUse`` enforces via
``hookSpecificOutput.permissionDecision``, while ``UserPromptSubmit``
enforces via the top-level ``decision`` / ``reason`` fields (both
harnesses parse ``decision: "block"`` to drop the prompt before the
model sees it).
"""

from __future__ import annotations

import json
import sys
import time

import httpx

# Hook event names that gate tool execution and therefore carry policy
# meaning. ``PreToolUse`` fires before the tool runs (can block);
# ``PostToolUse`` fires after (observational — can only warn).
_PRE_TOOL_USE = "PreToolUse"
_POST_TOOL_USE = "PostToolUse"
# ``UserPromptSubmit`` fires when a new user prompt reaches the harness —
# for native sessions this is the request-phase gate (the server-level
# ``_evaluate_input_policy`` is bypassed for native message events, so
# this hook is the sole REQUEST gate and covers both web-UI-injected and
# direct-terminal prompts). It can block the prompt before the model runs.
_USER_PROMPT_SUBMIT = "UserPromptSubmit"

# Reason surfaced when a tool call is denied because its policy verdict
# could not be obtained (server unreachable / non-2xx / empty or malformed
# body). Mirrors the runner-side fail-closed default in
# ``omnigent.runner.app._evaluate_policy_via_omnigent`` (PR #163).
_EVAL_UNAVAILABLE_REASON = (
    "Omnigent policy evaluation unavailable; failing closed for this tool call."
)

# Bounded retry for the synchronous ``POST /policies/evaluate`` gate that
# fronts every native tool call. A shared Omnigent server under load returns
# intermittent 5xx, and the gate used to fail CLOSED on the *first* such
# error — turning a momentary blip into a denied tool call (the user then
# had to retry by hand). A couple of quick retries absorb those blips while
# still failing closed when the server is genuinely down. Kept small and
# fast: this runs inline before the tool, not as the day-long permission
# long-poll.
_EVALUATE_RETRY_MAX_ATTEMPTS = 3
_EVALUATE_RETRY_INITIAL_BACKOFF_S = 0.25
_EVALUATE_RETRY_MAX_BACKOFF_S = 2.0
# Connect fast so an unreachable server drops into the retry/backoff loop
# instead of inheriting the day-long read budget (that budget exists only
# for a server-side ASK park, which happens *after* a successful connect).
_EVALUATE_CONNECT_TIMEOUT_S = 30.0


def post_policy_evaluation(
    url: str,
    headers: dict[str, str],
    eval_request: dict[str, object],
    *,
    read_timeout_s: float,
    log_prefix: str,
) -> dict[str, object] | None:
    """
    POST one ``EvaluationRequest`` to ``/policies/evaluate`` with bounded retry.

    Retries only the *safe* transient failures so a brief server blip no
    longer hard-denies the tool call:

    - **5xx** — the server errored before producing a verdict (overload /
      crash), so no ASK gate has parked; a retry usually lands on a healthy
      backend.
    - **connect-phase failures** (``ConnectError`` / ``ConnectTimeout`` /
      ``PoolTimeout``) — the request never reached the server, so nothing
      parked; safe to retry.

    Everything else is final (returns ``None`` so the caller fails closed):

    - **4xx** — a deterministic rejection; retrying cannot change it.
    - **read/write/protocol transport errors** — these can mean a long-poll
      ASK park was severed mid-wait. Unlike the permission long-poll
      (:func:`omnigent.claude_native_hook._post_hook_with_reattach`), this
      path carries no stable elicitation id, so re-POSTing would re-park a
      *duplicate* approval card. Fail closed instead of retrying.
    - **empty / malformed / non-object body** — the server answered but the
      verdict is unusable.

    :param url: Absolute evaluate endpoint, e.g.
        ``"http://127.0.0.1:8787/v1/sessions/conv_x/policies/evaluate"``.
    :param headers: Outbound Omnigent auth headers.
    :param eval_request: ``EvaluationRequest`` envelope to POST.
    :param read_timeout_s: Read budget in seconds. Held at the day-long
        ASK-park budget by callers; the connect phase uses the shorter
        :data:`_EVALUATE_CONNECT_TIMEOUT_S` so unreachable servers fail fast.
    :param log_prefix: Diagnostic prefix for stderr lines, e.g.
        ``"omnigent evaluate-policy hook"``.
    :returns: The parsed ``EvaluationResponse`` dict, or ``None`` when no
        usable verdict could be obtained (caller fails closed).
    """
    timeout = httpx.Timeout(read_timeout_s, connect=_EVALUATE_CONNECT_TIMEOUT_S)
    backoff_s = _EVALUATE_RETRY_INITIAL_BACKOFF_S
    last_error = ""
    for attempt in range(1, _EVALUATE_RETRY_MAX_ATTEMPTS + 1):
        try:
            with httpx.Client(headers=headers, timeout=timeout) as client:
                resp = client.post(url, json=eval_request)
                resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status < 500:
                # Deterministic rejection — retrying won't help.
                print(f"{log_prefix}: Omnigent returned {status}", file=sys.stderr)
                return None
            last_error = f"Omnigent returned {status}"
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            last_error = f"Omnigent connect failed: {exc}"
        except httpx.HTTPError as exc:
            # Read/write/protocol error — may be a severed ASK park; do not
            # retry (would risk a duplicate elicitation). Fail closed.
            print(f"{log_prefix}: Omnigent request failed: {exc}", file=sys.stderr)
            return None
        else:
            if not resp.content:
                print(f"{log_prefix}: empty Omnigent response", file=sys.stderr)
                return None
            try:
                parsed = resp.json()
            except (json.JSONDecodeError, ValueError):
                print(f"{log_prefix}: malformed Omnigent response", file=sys.stderr)
                return None
            if not isinstance(parsed, dict):
                print(f"{log_prefix}: unexpected Omnigent response shape", file=sys.stderr)
                return None
            return parsed
        # Reached only on a retryable failure (5xx or connect-phase).
        if attempt >= _EVALUATE_RETRY_MAX_ATTEMPTS:
            print(
                f"{log_prefix}: {last_error}; retries exhausted, failing closed",
                file=sys.stderr,
            )
            return None
        print(
            f"{log_prefix}: {last_error}; retrying "
            f"(attempt {attempt}/{_EVALUATE_RETRY_MAX_ATTEMPTS})",
            file=sys.stderr,
        )
        time.sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _EVALUATE_RETRY_MAX_BACKOFF_S)
    return None


def hook_payload_to_evaluation_request(
    hook_event: str,
    payload: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert a native-harness tool-hook payload into a proto ``EvaluationRequest``.

    Maps ``PreToolUse`` to a ``PHASE_TOOL_CALL`` event, ``PostToolUse``
    to a ``PHASE_TOOL_RESULT`` event, and ``UserPromptSubmit`` to a
    ``PHASE_REQUEST`` event (the prompt text from the payload's
    ``prompt`` field becomes the request content). Omnigent MCP tools
    (``mcp__omnigent__*``) are skipped because they are already
    policy-checked by the relay path (``ProxyMcpManager`` → Omnigent
    ``/mcp`` endpoint → ``_evaluate_tool_call_policy``); evaluating
    them here would double-count. Connector-native MCP tools
    (for example ``mcp__github__*``) still need this pre-call gate.

    :param hook_event: Hook event name from the payload's
        ``hook_event_name`` field, e.g. ``"PreToolUse"``,
        ``"PostToolUse"``, or ``"UserPromptSubmit"``.
    :param payload: Raw hook JSON from the harness, e.g.
        ``{"hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"}}``.
    :returns: An ``EvaluationRequest`` dict suitable for POSTing to
        ``/policies/evaluate``, or ``None`` when the event is not
        policy-relevant (unknown event or an ``mcp__omnigent__*`` tool).
    """
    if hook_event == _USER_PROMPT_SUBMIT:
        # Request-phase gate for native sessions. The server reads REQUEST
        # content from ``data.text`` (see ``_build_evaluation_context``).
        prompt = payload.get("prompt", "")
        return {
            "event": {
                "type": "PHASE_REQUEST",
                "target": "",
                "data": {
                    "text": prompt if isinstance(prompt, str) else json.dumps(prompt),
                },
                "context": {},
            },
        }
    tool_name = payload.get("tool_name", "")
    # Omnigent MCP tools are already policy-checked by the relay path
    # (ProxyMcpManager → Omnigent /mcp endpoint → _evaluate_tool_call_policy).
    # Skip only those here to avoid double evaluation; connector-native MCP
    # tools such as mcp__github__* must still go through this hook.
    if isinstance(tool_name, str) and tool_name.startswith("mcp__omnigent__"):
        return None
    tool_input = payload.get("tool_input") or {}
    if hook_event == _PRE_TOOL_USE:
        return {
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
                "context": {},
            },
        }
    if hook_event == _POST_TOOL_USE:
        tool_output = payload.get("tool_output", "")
        return {
            "event": {
                "type": "PHASE_TOOL_RESULT",
                "target": "",
                "data": {
                    "result": tool_output,
                },
                "context": {},
                "request_data": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
            },
        }
    return None


def evaluation_response_to_hook_output(
    hook_event: str,
    eval_response: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert an ``EvaluationResponse`` into native-harness hook output JSON.

    For ``PreToolUse`` the policy layer only *enforces* — it emits a
    ``hookSpecificOutput.permissionDecision`` solely for verdicts that
    constrain the tool: ``POLICY_ACTION_DENY`` → ``"deny"`` (with
    ``permissionDecisionReason``). ``POLICY_ACTION_ASK`` is resolved
    server-side now (URL-based elicitation: ``POST /policies/evaluate``
    holds the gate and returns a hard ALLOW/DENY), so the hook should
    never see ASK; if it does, it fails closed with ``"deny"`` rather
    than the old ``"defer"`` — ``defer`` handed control back to the
    harness's ``permission_mode``, which ``acceptEdits`` /
    ``bypassPermissions`` would auto-approve, bypassing the human.
    ``POLICY_ACTION_ALLOW`` — which is the engine's default verdict when
    no policy matches a tool call, not just an explicit author allow —
    returns ``None`` ("no opinion") so the harness's *own* permission
    system still runs. Emitting ``"allow"`` here would auto-approve the
    tool and suppress the harness's native permission prompt (and, for
    Claude Code, the ``PermissionRequest`` hook that routes that prompt
    to the web UI), collapsing two independent gates — the deployment's
    policy gate and the user's own consent gate — into one. The policy
    layer may block (DENY) or demand approval (ASK); it must not silence
    the user's consent. For ``PostToolUse`` a ``DENY`` is surfaced as
    ``additionalContext`` because the tool result is already committed
    — PostToolUse hooks cannot block.

    For ``UserPromptSubmit`` the output uses the top-level ``decision`` /
    ``reason`` contract (not ``permissionDecision``): ``DENY`` → ``{"decision":
    "block", "reason": ...}``, which drops the prompt before the model sees
    it. ASK is resolved server-side (``_hold_native_ask_gate`` collapses it
    to a hard ALLOW/DENY before the response reaches the hook), so the hook
    should never see ASK; if it somehow does, it fails closed by blocking.
    ALLOW (and the engine's no-match default) returns ``None`` so the prompt
    proceeds. Unlike ``PreToolUse``, there is no separate user-consent gate
    on a prompt, so ALLOW need not preserve one.

    Both Claude Code and Codex consume these exact output shapes, so the
    ``hookEventName`` echoed back is the harness-supplied ``hook_event``.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"``,
        ``"PostToolUse"``, or ``"UserPromptSubmit"``.
    :param eval_response: Parsed ``EvaluationResponse`` from AP, e.g.
        ``{"result": "POLICY_ACTION_DENY", "reason": "blocked by policy"}``.
    :returns: Hook output dict for the harness to read on stdout, or
        ``None`` when there is no verdict to express (allow with no
        rewrite on PostToolUse, or an unknown action).
    """
    action = eval_response.get("result", "POLICY_ACTION_UNSPECIFIED")
    reason = eval_response.get("reason")

    if hook_event == _USER_PROMPT_SUBMIT:
        # DENY blocks the prompt; a stray ASK fails closed (also block) since
        # ASK is meant to be resolved server-side before reaching the hook.
        # ALLOW / no-match → None so the prompt proceeds. A non-empty reason
        # is required for the block to take effect (both harnesses drop a
        # block with an empty reason), so default one in.
        if action in ("POLICY_ACTION_DENY", "POLICY_ACTION_ASK"):
            return {
                "decision": "block",
                "reason": reason or "Denied by policy",
            }
        return None

    if hook_event == _PRE_TOOL_USE:
        # ALLOW (the engine default when no policy matches) is omitted → None,
        # so the harness's own permission prompt still fires; see docstring.
        decision_map = {
            "POLICY_ACTION_DENY": "deny",
            # ASK is resolved server-side now (URL-based elicitation:
            # POST /policies/evaluate holds the gate and returns a hard
            # ALLOW/DENY), so the hook should never see ASK here. If it
            # somehow does, fail closed with ``deny`` rather than the old
            # ``defer`` — ``defer`` returns control to the harness's
            # permission_mode, which acceptEdits / bypassPermissions would
            # auto-approve, re-opening the very bypass this closes.
            "POLICY_ACTION_ASK": "deny",
        }
        decision = decision_map.get(str(action))
        if decision is None:
            return None
        output: dict[str, object] = {
            "hookEventName": _PRE_TOOL_USE,
            "permissionDecision": decision,
        }
        if decision == "deny" and reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}

    if hook_event == _POST_TOOL_USE:
        if action == "POLICY_ACTION_DENY" and reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": _POST_TOOL_USE,
                    "additionalContext": f"[Policy violation] {reason}",
                },
            }
        return None

    return None


def fail_closed_hook_output(hook_event: str) -> dict[str, object] | None:
    """
    Build the fail-closed hook output for an unobtainable policy verdict.

    Called by the per-harness hooks when the ``/policies/evaluate``
    round-trip cannot produce a usable verdict for an *already-governed*
    session — the server is unreachable, returns a non-2xx status, or
    returns an empty / malformed body. Without this the hooks emitted "no
    opinion" on those paths, silently letting the gated tool run: for
    native harnesses this hook is the sole enforcement point (it gates
    Bash / Write / Edit / the native Skill tool / connector-native
    ``mcp__*`` tools), so a transient outage disabled all DENY/ASK
    enforcement.

    The default is phase-aware, matching
    :data:`omnigent.policies.types.FAIL_CLOSED_PHASES` (the runner-side
    precedent from PR #163) — but expressed in hook-event terms so the
    lightweight hook subprocess need not import the policy package:

    - ``PreToolUse`` (``PHASE_TOOL_CALL``) fails CLOSED → ``deny``. This is
      the authoritative pre-execution gate; an unevaluable policy must not
      let the call through.
    - ``UserPromptSubmit`` (``PHASE_REQUEST``) and ``PostToolUse``
      (``PHASE_TOOL_RESULT``) fail OPEN → ``None``. The request gate is
      advisory (the tool-call gate still catches dangerous actions) and by
      the result phase the tool has already executed, so denying would only
      block an already-incurred side effect.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"``.
    :returns: A ``permissionDecision: "deny"`` hook output for
        ``PreToolUse``; ``None`` for every other event (fail open).
    """
    if hook_event == _PRE_TOOL_USE:
        return {
            "hookSpecificOutput": {
                "hookEventName": _PRE_TOOL_USE,
                "permissionDecision": "deny",
                "permissionDecisionReason": _EVAL_UNAVAILABLE_REASON,
            },
        }
    return None
