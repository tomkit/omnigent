#!/usr/bin/env bash
# rebase-fork.sh — attempt to rebase the fork's custom commits onto the
# upstream default branch, and report the outcome to the caller.
#
# This is the deterministic, non-AI part of the daily fork sync (see
# .github/workflows/daily-fork-sync.yml). It runs `git rebase` once; if that
# stops on conflicts it leaves the rebase paused (it does NOT abort) so the
# caller can hand the half-finished rebase to Claude Code for resolution.
#
# Contract (written to $GITHUB_OUTPUT when set, also echoed):
#   outcome=noop      — fork already contains everything upstream has; HEAD unmoved.
#   outcome=clean     — rebase replayed cleanly, no conflicts, HEAD moved.
#   outcome=conflict  — rebase paused on a conflict; resolution is needed.
#   outcome=error     — something unexpected (bad refs, dirty tree, etc.).
#   head_before=<sha> / head_after=<sha>
#
# Env in:
#   UPSTREAM_REMOTE   (default: upstream)
#   UPSTREAM_BRANCH   (default: main)
set -uo pipefail

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_BRANCH="${UPSTREAM_BRANCH:-main}"
UPSTREAM_REF="${UPSTREAM_REMOTE}/${UPSTREAM_BRANCH}"

emit() {
  echo "rebase-fork: $1"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1" >>"$GITHUB_OUTPUT"
  fi
}

fail() {
  echo "rebase-fork: ERROR: $1" >&2
  emit "outcome=error"
  exit 1
}

# A rebase must start from a clean tree.
if [ -n "$(git status --porcelain)" ]; then
  fail "working tree is dirty before rebase; refusing to start"
fi

git rev-parse --verify "$UPSTREAM_REF" >/dev/null 2>&1 \
  || fail "upstream ref '$UPSTREAM_REF' not found — was the remote added and fetched?"

HEAD_BEFORE="$(git rev-parse HEAD)"
emit "head_before=${HEAD_BEFORE}"

# Already up to date with upstream? (upstream is an ancestor of HEAD AND there
# is nothing to replay) — detect the common "nothing new upstream" case so we
# can skip a pointless push/deploy.
if git merge-base --is-ancestor "$UPSTREAM_REF" HEAD; then
  emit "head_after=${HEAD_BEFORE}"
  emit "outcome=noop"
  echo "rebase-fork: fork already up to date with ${UPSTREAM_REF}; nothing to do."
  exit 0
fi

echo "rebase-fork: rebasing custom commits onto ${UPSTREAM_REF} ..."
echo "rebase-fork: commits to be replayed (custom + un-synced):"
git --no-pager log --oneline "${UPSTREAM_REF}..HEAD" | sed 's/^/  /' || true

# Plain rebase: commits already in upstream are dropped by patch-id, the fork's
# own commits are replayed on top. Merge commits are flattened — that is fine,
# they are the user's own PR-merge bubbles. `--no-rerere-autoupdate` is implied;
# we just run it once and inspect the exit code.
if git rebase "$UPSTREAM_REF"; then
  HEAD_AFTER="$(git rev-parse HEAD)"
  emit "head_after=${HEAD_AFTER}"
  if [ "$HEAD_AFTER" = "$HEAD_BEFORE" ]; then
    emit "outcome=noop"
  else
    emit "outcome=clean"
  fi
  echo "rebase-fork: rebase completed cleanly."
  exit 0
fi

# Non-zero exit: the rebase is paused. Confirm it is genuinely a conflict pause
# (a rebase-merge / rebase-apply state dir exists) and not some other failure.
if [ -d "$(git rev-parse --git-path rebase-merge)" ] || \
   [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  echo "rebase-fork: rebase paused on conflicts; leaving state in place for resolution."
  echo "rebase-fork: conflicted paths:"
  git --no-pager diff --name-only --diff-filter=U | sed 's/^/  /' || true
  emit "outcome=conflict"
  exit 0
fi

fail "git rebase failed but no rebase is in progress — unexpected state"
