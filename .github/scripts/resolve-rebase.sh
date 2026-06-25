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
# Auth: `claude` reads ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN from the
# environment; the caller must export one.
set -uo pipefail

MAX_ITERS="${MAX_ITERS:-20}"
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

# Advance a paused rebase by one step, tolerating the "became empty" case.
continue_rebase() {
  if GIT_EDITOR=true git rebase --continue >"$CONT_LOG" 2>&1; then
    return 0
  fi
  # Match git's specific "this commit became empty" phrasings only, so an
  # unrelated error that merely contains the word "empty" doesn't get silently
  # skipped. Git emits one of: "No changes - did you forget ...", "nothing to
  # commit", or "... is now empty" / "would make it empty" / "becomes empty".
  if grep -qiE 'no changes|nothing to commit|(is now|becomes|make it) empty' "$CONT_LOG"; then
    echo "resolve-rebase: step became empty; skipping."
    git rebase --skip >"$CONT_LOG" 2>&1 || {
      echo "::error::git rebase --skip failed"; cat "$CONT_LOG"; return 1; }
    return 0
  fi
  echo "::error::git rebase --continue failed unexpectedly"; cat "$CONT_LOG"
  return 1
}

if ! in_rebase; then
  echo "resolve-rebase: no rebase in progress; nothing to resolve."
  exit 0
fi

if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  echo "::error::resolve-rebase: neither ANTHROPIC_API_KEY nor CLAUDE_CODE_OAUTH_TOKEN is set." >&2
  exit 1
fi

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
    echo "resolve-rebase: [iter ${iter}] resolving ${#conflicts[@]} conflicted file(s):"
    printf '  %s\n' "${conflicts[@]}"

    file_list=""
    for f in "${conflicts[@]}"; do
      file_list+="  - ${f}"$'\n'
    done

    prompt="You are resolving git rebase conflicts inside a CI runner. A rebase of
the fork tomkit/omnigent onto upstream omnigent-ai/omnigent is paused. Resolve
ONLY these conflicted files by editing their contents in place:

${file_list}
Requirements:
- Open each file, remove EVERY conflict marker (lines starting with <<<<<<<,
  =======, or >>>>>>>), and produce a correct merged result.
- Preserve the fork's commit INTENT (the feature/behavior the fork added) while
  taking upstream's changes wherever they do not conflict with that intent. If
  upstream refactored code the fork also changed, re-apply the fork's change on
  top of upstream's new shape rather than reverting upstream.
- Fork custom work to preserve: daytona managed-sandbox idle-suspend/resume +
  bidirectional context sync, the Fly server image / deploy config, and the
  polly worker-routing policy.
- Edit files ONLY. Do NOT run git or any shell command. Do NOT add, commit,
  continue, abort, or push — the surrounding script does all git operations.
When done, every listed file must contain zero conflict markers."

    # File-editing tools ONLY — no Bash — so Claude cannot touch git.
    if ! claude -p "$prompt" \
      --permission-mode acceptEdits \
      --allowedTools "Read,Edit,MultiEdit,Write,Grep,Glob" \
      --max-turns 40; then
      echo "::error::resolve-rebase: headless claude invocation failed at iter ${iter}."
      exit 1
    fi

    # Trust nothing: the SHELL verifies the markers are gone before staging.
    bad=0
    for f in "${conflicts[@]}"; do
      if [ -f "$f" ] && grep -qE '^(<<<<<<<|=======|>>>>>>>)' "$f"; then
        echo "::error::conflict markers remain in ${f} after resolution"; bad=1
      fi
    done
    [ "$bad" -eq 0 ] || { echo "::error::resolve-rebase: unresolved markers; aborting."; exit 1; }

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
