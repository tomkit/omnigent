#!/usr/bin/env bash
# sync-fork.sh — rebuild the fork on top of an upstream release tag.
#
# The fork carries ZERO modified upstream files, only additions at fork-owned
# paths, so an upgrade is not a rebase: check out the tag, copy the fork-owned
# paths from the current fork main, and refuse if any copied path also exists
# in the tag (that would silently pin an upstream file at an old version).
#
# Usage:  .github/scripts/sync-fork.sh v0.15.0 [fork-ref]
#   fork-ref defaults to fork/main. Leaves you on branch upgrade-<tag> with the
#   result staged (not committed) so you can review `git diff --cached <tag>`.
#
# After it lands (fast-forward force-push of main), do the deploy half in
# deploy/fly/RUNBOOK.tomkit.md: in-container migrate, then `fly deploy`.
set -euo pipefail

TAG="${1:?usage: sync-fork.sh <upstream-tag> [fork-ref]}"
FORK_REF="${2:-fork/main}"

# Every path the fork owns. Anything not listed here is upstream's and must
# come from the tag untouched.
FORK_PATHS=(
  deploy/agents/polly
  deploy/fly
  deploy/FORK_SURFACE.md
  tests/deploy/test_fork_agent_bundles.py
  .github/scripts/sync-fork.sh
  .github/workflows/fork-publish-server.yml
)

git fetch --quiet origin --tags
git rev-parse --verify --quiet "refs/tags/${TAG}" >/dev/null \
  || { echo "sync-fork: no such upstream tag: ${TAG}" >&2; exit 1; }
git rev-parse --verify --quiet "${FORK_REF}" >/dev/null \
  || { echo "sync-fork: no such fork ref: ${FORK_REF}" >&2; exit 1; }

git checkout --quiet -B "upgrade-${TAG}" "refs/tags/${TAG}"
git checkout --quiet "${FORK_REF}" -- "${FORK_PATHS[@]}"

# Refuse to carry a stale copy of anything upstream ships in this tag.
stale=()
while IFS= read -r path; do
  if git cat-file -e "refs/tags/${TAG}:${path}" 2>/dev/null; then
    stale+=("${path}")
  fi
done < <(git diff --cached --name-only "refs/tags/${TAG}")
if [ "${#stale[@]}" -gt 0 ]; then
  echo "sync-fork: these copied paths also exist in ${TAG} — the fork must not shadow upstream files:" >&2
  printf '  %s\n' "${stale[@]}" >&2
  exit 1
fi

added=$(git diff --cached --name-only "refs/tags/${TAG}" | wc -l | tr -d ' ')
echo "sync-fork: upgrade-${TAG} = ${TAG} + ${added} fork-owned files (staged, uncommitted)."
echo "sync-fork: next — update deploy/FORK_SURFACE.md, run pre-commit + pytest tests/deploy, commit,"
echo "           then land: git push --force-with-lease=refs/heads/main:<old-sha> fork HEAD:main"
