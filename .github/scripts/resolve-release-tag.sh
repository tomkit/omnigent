#!/usr/bin/env bash
# resolve-release-tag.sh — resolve upstream's latest STABLE release tag and fetch
# it into the local repo so the daily fork sync can rebase onto a released tag
# instead of the moving tip of upstream/main.
#
# This is a fork-only helper for .github/workflows/daily-fork-sync.yml. It runs
# in the workflow step that has `gh` + GITHUB_TOKEN available (GH_TOKEN env).
#
# Why /releases/latest (and not "highest semver tag"): the GitHub
# `repos/{owner}/{repo}/releases/latest` endpoint returns the most recent
# release that is NOT a draft and NOT a prerelease — i.e. the latest STABLE
# release. Picking the highest semver tag could select a prerelease/RC tag; this
# endpoint never does.
#
# Contract (written to $GITHUB_OUTPUT when set, also echoed):
#   tag=<tag>   — the resolved stable release tag, already fetched as
#                 refs/tags/<tag> in the local repo.
#   tag=        — (empty) upstream has NO published stable release; the caller
#                 must treat this as a no-op and do NOTHING (no rebase onto main).
#
# Env in:
#   UPSTREAM_REMOTE   (default: upstream)  — remote to fetch the tag from.
#   UPSTREAM_REPO     (default: omnigent-ai/omnigent) — owner/repo for the API.
#   GH_TOKEN / GITHUB_TOKEN — used by `gh api`.
set -uo pipefail

UPSTREAM_REMOTE="${UPSTREAM_REMOTE:-upstream}"
UPSTREAM_REPO="${UPSTREAM_REPO:-omnigent-ai/omnigent}"

emit() {
  echo "fork-sync: $1"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1" >>"$GITHUB_OUTPUT"
  fi
}

no_release() {
  emit "tag="
  echo "fork-sync: upstream has no stable releases — nothing to do."
  if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
    echo "### Daily fork sync: upstream has no stable releases :white_check_mark:" >>"$GITHUB_STEP_SUMMARY"
  fi
  exit 0
}

errfile="$(mktemp)"
trap 'rm -f "$errfile"' EXIT

# Query the latest STABLE release. `gh api` exits non-zero on HTTP 404 (the repo
# has no non-draft/non-prerelease release yet). We distinguish a 404 (legit
# no-release => no-op) from any other failure (auth/network => hard error).
tag="$(gh api "repos/${UPSTREAM_REPO}/releases/latest" --jq '.tag_name // empty' 2>"$errfile")"
rc=$?
if [ "$rc" -ne 0 ]; then
  if grep -qiE 'HTTP 404|Not Found' "$errfile"; then
    no_release
  fi
  echo "::error::fork-sync: failed to query ${UPSTREAM_REPO} releases/latest (gh exit ${rc})." >&2
  cat "$errfile" >&2
  exit 1
fi

# 200 OK but an empty/absent tag_name also means "no stable release to rebase on".
if [ -z "$tag" ]; then
  no_release
fi

echo "fork-sync: latest upstream stable release: ${tag}"

# Fetch ONLY that tag (explicit refspec defeats --no-tags' auto-follow disable
# for this one tag) into refs/tags/<tag>.
if ! git fetch --no-tags "$UPSTREAM_REMOTE" "refs/tags/${tag}:refs/tags/${tag}"; then
  echo "::error::fork-sync: failed to fetch release tag '${tag}' from '${UPSTREAM_REMOTE}'." >&2
  exit 1
fi

git rev-parse --verify "refs/tags/${tag}^{commit}" >/dev/null 2>&1 \
  || { echo "::error::fork-sync: release tag '${tag}' not present after fetch." >&2; exit 1; }

emit "tag=${tag}"
