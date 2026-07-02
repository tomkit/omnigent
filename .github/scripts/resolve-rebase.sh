#!/usr/bin/env bash
# resolve-rebase.sh — drive a PAUSED git rebase to completion using headless
# Claude Code to resolve ONLY the conflicted file CONTENTS.
#
# Why this shape: the GitHub `anthropics/claude-code-action` refuses destructive
# git operations (rebase / merge / rebase --continue / force-push) — the refusal
# is baked into its system prompt and fires even when Bash/git tools are allowed
# (see anthropics/claude-code-action docs/faq.md). So we do NOT ask Claude to run
# git. Instead the SHELL owns every git operation (add / rebase --continue /
# --skip) and Claude is invoked headless (`claude -p`) with FILE-EDITING tools
# ONLY (no Bash at all) — it edits the conflicted files and literally cannot run
# git. Deterministic, and the action's restriction is irrelevant.
#
# Loop: while a rebase is in progress, resolve the currently-conflicted files,
# then let the shell advance the rebase, until it completes or we stall / hit
# MAX_ITERS (fail loudly either way).
#
# Model/auth: the headless `claude` CLI speaks the Anthropic Messages API, and
# Fireworks serves an Anthropic-compatible endpoint at
# ${ANTHROPIC_BASE_URL}/v1/messages — so we point Claude Code straight at
# Fireworks (no translation proxy) and resolve conflicts on GLM, not Anthropic.
# The caller (daily-fork-sync.yml) normally sets ANTHROPIC_BASE_URL /
# ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL / ANTHROPIC_SMALL_FAST_MODEL explicitly;
# the defaults below are an overridable backstop targeting Fireworks' latest GLM.
# This wiring is scoped to THIS resolver only — it does NOT affect the deployed
# managed/Daytona agents, which stay on Claude.
set -uo pipefail

MAX_ITERS="${MAX_ITERS:-20}"
# Per-file headless turn budget. We resolve one conflicted file per `claude -p`
# call (see the inner loop below), which keeps each invocation's scope small —
# but a single large file can still need plenty of agent turns, so give it real
# headroom. This is deliberately well above the old hardcoded 40: a weaker model
# (e.g. Fireworks GLM) burns turns faster, and per-file isolation only helps if
# each file actually has the budget to finish. Overridable via CLAUDE_MAX_TURNS.
CLAUDE_MAX_TURNS="${CLAUDE_MAX_TURNS:-80}"
# Bounded per-file retry budget. The resolver model (Fireworks GLM) is weaker and
# flakier than Claude and OCCASIONALLY leaves conflict markers after a single pass
# on a file that is perfectly resolvable (and that it resolved cleanly on other
# iterations). Rather than abort the whole sync on the first such flake, re-invoke
# `claude -p` on the SAME still-conflicted file up to this many ADDITIONAL times.
# Total attempts per file = CLAUDE_FILE_RETRIES + 1 (default 3). Overridable.
CLAUDE_FILE_RETRIES="${CLAUDE_FILE_RETRIES:-2}"
CONT_LOG="$(mktemp)"
trap 'rm -f "$CONT_LOG"' EXIT

in_rebase() {
  [ -d "$(git rev-parse --git-path rebase-merge)" ] ||
    [ -d "$(git rev-parse --git-path rebase-apply)" ]
}

# A signature that strictly advances as the rebase makes progress: last applied
# commit + the current step number. Used to detect a stall (no progress).
# Merge-style rebases track the step in rebase-merge/msgnum; apply-style rebases
# (e.g. `git rebase --apply`) use rebase-apply/next — read whichever exists so
# an apply-style rebase doesn't look stalled at a constant 0.
progress_sig() {
  local msgnum
  msgnum="$(cat "$(git rev-parse --git-path rebase-merge/msgnum)" 2>/dev/null \
    || cat "$(git rev-parse --git-path rebase-apply/next)" 2>/dev/null \
    || echo 0)"
  echo "$(git rev-parse HEAD 2>/dev/null || echo none)-${msgnum}"
}

# Advance a paused rebase by one step.
#
# CRITICAL: `git rebase --continue` exits NON-ZERO in the normal multi-commit
# case. After it commits the step we just resolved, it keeps replaying and, if
# the NEXT commit also conflicts, it stops there and returns non-zero. That is
# not a failure — it is progress, and our rebases ALWAYS have several conflicting
# commits. So a non-zero exit is only fatal when the rebase did NOT advance.
# We distinguish three outcomes:
#   * exit 0                          -> this step (and any trailing clean ones)
#                                        applied; may or may not still be rebasing.
#   * non-zero, "became empty"        -> the resolved commit is now empty; skip it.
#   * non-zero, but the rebase moved  -> it paused at the NEXT conflicting commit;
#     (HEAD/step advanced, fresh U's)    let the outer loop resolve that batch.
#   * non-zero, no progress           -> a genuine error (e.g. still-unmerged
#                                        paths); fail loudly.
#
# GIT_EDITOR / GIT_SEQUENCE_EDITOR are forced to `true` so --continue never hangs
# waiting on the commit-message editor (or, defensively, any todo-list editor).
continue_rebase() {
  local before after
  before="$(progress_sig)"
  if GIT_EDITOR=true GIT_SEQUENCE_EDITOR=true git rebase --continue >"$CONT_LOG" 2>&1; then
    return 0
  fi
  # Match git's specific "this commit became empty" phrasings only, so an
  # unrelated error that merely contains the word "empty" doesn't get silently
  # skipped. Git emits one of: "No changes - did you forget ...", "nothing to
  # commit", or "... is now empty" / "would make it empty" / "becomes empty".
  if grep -qiE 'no changes|nothing to commit|(is now|becomes|make it) empty' "$CONT_LOG"; then
    echo "resolve-rebase: step became empty; skipping."
    GIT_EDITOR=true GIT_SEQUENCE_EDITOR=true git rebase --skip >"$CONT_LOG" 2>&1 || {
      echo "::error::git rebase --skip failed"; cat "$CONT_LOG"; return 1; }
    return 0
  fi
  # The resolved step committed and the rebase advanced, then paused at the NEXT
  # conflicting commit. Require real forward motion (the progress signature moved)
  # AND a still-active rebase with a fresh batch of unmerged files, so a genuine
  # "cannot continue" error (no movement) still falls through to the fatal path.
  after="$(progress_sig)"
  if in_rebase && [ "$after" != "$before" ] \
     && [ -n "$(git diff --name-only --diff-filter=U)" ]; then
    echo "resolve-rebase: advanced; paused at the next conflicting commit."
    cat "$CONT_LOG"
    return 0
  fi
  echo "::error::git rebase --continue failed unexpectedly (no progress)"; cat "$CONT_LOG"
  return 1
}

if ! in_rebase; then
  echo "resolve-rebase: no rebase in progress; nothing to resolve."
  exit 0
fi

# ---- Resolver model wiring (Fireworks / Anthropic-compatible) ----
# Overridable defaults target Fireworks' latest GLM. A bare FIREWORKS_API_KEY is
# mapped to ANTHROPIC_AUTH_TOKEN (the bearer-token Fireworks expects) as a
# convenience for callers that only export the raw key.
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.fireworks.ai/inference}"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-accounts/fireworks/models/glm-5p2}"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-$ANTHROPIC_MODEL}"
if [ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] && [ -n "${FIREWORKS_API_KEY:-}" ]; then
  export ANTHROPIC_AUTH_TOKEN="${FIREWORKS_API_KEY}"
fi

# Require some auth source. ANTHROPIC_AUTH_TOKEN (bearer) is the Fireworks path;
# ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN stay accepted as a fallback if the
# caller ever rewires the resolver back to Anthropic. The key is never echoed.
if [ -z "${ANTHROPIC_AUTH_TOKEN:-}" ] && [ -z "${ANTHROPIC_API_KEY:-}" ] \
   && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "::error::resolve-rebase: no resolver auth set (need ANTHROPIC_AUTH_TOKEN or FIREWORKS_API_KEY)." >&2
  exit 1
fi
echo "resolve-rebase: resolver model=${ANTHROPIC_MODEL} via ${ANTHROPIC_BASE_URL}"

iter=0
while in_rebase; do
  iter=$((iter + 1))
  if [ "$iter" -gt "$MAX_ITERS" ]; then
    echo "::error::resolve-rebase: exceeded MAX_ITERS=${MAX_ITERS} without finishing the rebase."
    git status; exit 1
  fi

  # Files git marked as conflicted at THIS paused step.
  mapfile -t conflicts < <(git diff --name-only --diff-filter=U)

  before_sig="$(progress_sig)"

  if [ "${#conflicts[@]}" -eq 0 ]; then
    # Paused with nothing conflicted (e.g. an emptied commit): just advance.
    echo "resolve-rebase: [iter ${iter}] no conflicted files; advancing rebase."
    continue_rebase || exit 1
  else
    echo "resolve-rebase: [iter ${iter}] resolving ${#conflicts[@]} conflicted file(s), one per claude call:"
    printf '  %s\n' "${conflicts[@]}"

    # PER-FILE resolution: invoke `claude -p` separately for EACH conflicted
    # file rather than once for the whole batch. One file per prompt bounds the
    # scope each invocation has to reason about, so the turn budget is enough to
    # actually finish (the old single-call-for-all-files shape made a weaker
    # model burn through every turn without converging on a 5-file batch). It
    # also pins any genuinely-unresolvable file to a single, named failure
    # instead of an opaque whole-iteration one.
    for f in "${conflicts[@]}"; do
      echo "resolve-rebase: [iter ${iter}] resolving ${f}"

      prompt="You are resolving git rebase conflicts inside a CI runner. A rebase of
the fork tomkit/omnigent onto upstream omnigent-ai/omnigent is paused. Resolve
ONLY this one conflicted file by editing its contents in place:

  - ${f}

Requirements:
- Open the file, remove EVERY conflict marker (lines starting with <<<<<<<,
  =======, or >>>>>>>), and produce a correct merged result.
- Preserve the fork's commit INTENT (the feature/behavior the fork added) while
  taking upstream's changes wherever they do not conflict with that intent. If
  upstream refactored code the fork also changed, re-apply the fork's change on
  top of upstream's new shape rather than reverting upstream.
- Fork custom work to preserve: daytona managed-sandbox idle-suspend/resume +
  bidirectional context sync, the Fly server image / deploy config, and the
  polly worker-routing policy.
- If upstream renamed a directory, this file is already at the NEW path; resolve
  it here and do not recreate the old path.
- Edit this file ONLY. Do NOT run git or any shell command. Do NOT add, commit,
  continue, abort, or push — the surrounding script does all git operations.
When done, this file must contain zero conflict markers."

      # BOUNDED per-file retry: an occasional flaky GLM pass can leave conflict
      # markers on an otherwise-resolvable file, so give the SAME still-conflicted
      # working-tree file up to CLAUDE_FILE_RETRIES additional `claude -p` passes
      # before failing loud. Total attempts = CLAUDE_FILE_RETRIES + 1. The file
      # CONTENTS (marker check) are the source of truth: a transient CLI/API error
      # is only a warning and feeds a retry, but if the file nonetheless ends up
      # marker-free we accept it. We only reach the fatal marker-remain path once
      # every attempt is spent on a genuinely stuck file.
      max_attempts=$((CLAUDE_FILE_RETRIES + 1))
      resolved=0
      for ((attempt = 1; attempt <= max_attempts; attempt++)); do
        if [ "$attempt" -gt 1 ]; then
          echo "resolve-rebase: [iter ${iter}] retry ${attempt}/${max_attempts} for ${f} (markers remained)."
        fi

        # File-editing tools ONLY — no Bash — so Claude cannot touch git.
        if ! claude -p "$prompt" \
          --model "$ANTHROPIC_MODEL" \
          --permission-mode acceptEdits \
          --allowedTools "Read,Edit,MultiEdit,Write,Grep,Glob" \
          --max-turns "$CLAUDE_MAX_TURNS"; then
          echo "::warning::resolve-rebase: headless claude invocation failed for ${f} at iter ${iter} (attempt ${attempt}/${max_attempts})."
          # Fall through to the marker check — the file contents decide, not the
          # exit code. If markers remain, the loop retries; if not, we accept it.
        fi

        # Trust nothing: the SHELL verifies the markers are gone for THIS file.
        if [ -f "$f" ] && grep -qE '^(<<<<<<<|=======|>>>>>>>)' "$f"; then
          echo "resolve-rebase: [iter ${iter}] conflict markers still present in ${f} after attempt ${attempt}/${max_attempts}."
          continue
        fi

        resolved=1
        break
      done

      # Fail loud only after every attempt is exhausted on a stuck file — a
      # per-file failure names the offending file rather than leaving it for the
      # batch check.
      if [ "$resolved" -ne 1 ]; then
        echo "::error::resolve-rebase: conflict markers remain in ${f} after resolution; aborting."
        exit 1
      fi
    done

    git add -A
    continue_rebase || exit 1
  fi

  # Stall guard: if we are still mid-rebase but nothing advanced, bail.
  if in_rebase && [ "$(progress_sig)" = "$before_sig" ]; then
    echo "::error::resolve-rebase: no progress at iter ${iter} (stalled)."
    git status; exit 1
  fi
done

echo "resolve-rebase: rebase completed in ${iter} iteration(s)."
