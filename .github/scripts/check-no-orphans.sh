#!/usr/bin/env bash
# check-no-orphans.sh — fork-only "orphan guard" for the daily fork rebase.
#
# Defense-in-depth for ONE narrow, silent failure mode of the daily fork sync
# (see .github/workflows/daily-fork-sync.yml):
#
#   When upstream RENAMES-AWAY or DELETES a directory (e.g. ap-web/ -> web/),
#   git's rename detection correctly relocates every file the fork also MODIFIED
#   and produces normal content conflicts the resolver handles. The single case
#   that slips through is a file the fork ADDED in a brand-new subdirectory under
#   the renamed/deleted tree: it has no upstream counterpart, so the rebase
#   replays it with NO conflict. It lands tracked under a directory that no
#   longer exists upstream. Nothing flags it — resolve-rebase.sh only sees
#   git-unmerged ("U") files, and the workflow's verify step only runs on the
#   conflict path. On a CLEAN rebase the tree is then force-pushed to main and
#   auto-deployed with the orphan silently along for the ride.
#
# This check closes exactly that gap: given the pre-rebase fork head and the new
# upstream tip, it computes which directory prefixes upstream removed (rename
# sources + deletions) between their merge-base and the upstream tip, keeps only
# the prefixes that genuinely no longer exist upstream, and fails if any
# currently-tracked file still lives under one of them.
#
# It is entirely fork-only and touches NO upstream-shared files; it is NOT a
# rename-map `git mv` — it never moves anything, it only refuses to publish a
# tree that contains an orphan and asks a human to relocate it.
#
# Inputs (env):
#   UPSTREAM_REF  — the new upstream tip to compare against (e.g. "upstream/main").
#   HEAD_BEFORE   — the fork HEAD *before* the rebase (rebase-fork.sh emits this
#                   as `head_before`).
#
# Exit: 0 = no orphans (or nothing vanished upstream — a clean no-op);
#       1 = orphan(s) found, or inputs/refs are invalid.
set -euo pipefail

: "${UPSTREAM_REF:?check-no-orphans: UPSTREAM_REF must be set (e.g. upstream/main)}"
: "${HEAD_BEFORE:?check-no-orphans: HEAD_BEFORE must be set (pre-rebase fork head; rebase-fork.sh emits head_before)}"

git rev-parse --verify "${UPSTREAM_REF}^{commit}" >/dev/null 2>&1 \
  || { echo "::error::check-no-orphans: UPSTREAM_REF '${UPSTREAM_REF}' is not a valid commit"; exit 1; }
git rev-parse --verify "${HEAD_BEFORE}^{commit}" >/dev/null 2>&1 \
  || { echo "::error::check-no-orphans: HEAD_BEFORE '${HEAD_BEFORE}' is not a valid commit"; exit 1; }

BASE="$(git merge-base "$UPSTREAM_REF" "$HEAD_BEFORE")" \
  || { echo "::error::check-no-orphans: no merge-base between '${UPSTREAM_REF}' and '${HEAD_BEFORE}'"; exit 1; }

# Directory prefixes upstream RENAMED-AWAY (rename SOURCE paths; -M turns on
# rename detection) or DELETED between the merge-base and the new upstream tip.
# -F'\t' on the rename pass keeps paths-with-spaces intact (name-status is
# "R100<TAB>old<TAB>new", so $2 is the old/source path). `sed` drops the final
# /component to get the containing directory.
gone="$(
  {
    git diff -M --diff-filter=R --name-status "$BASE" "$UPSTREAM_REF" | awk -F'\t' '{print $2}'
    git diff    --diff-filter=D --name-only    "$BASE" "$UPSTREAM_REF"
  } | sed 's#/[^/]*$##' | sort -u
)"

# Empty gone-set => nothing was renamed-away/deleted upstream => clean no-op.
# This guard is load-bearing: it stops us from ever building a match pattern
# against an empty set, which could otherwise flag every tracked file.
if [ -z "$gone" ]; then
  echo "check-no-orphans: no upstream directory renames/deletions in this sync; nothing to check."
  exit 0
fi

# Expand each removed leaf-directory to itself plus every ancestor directory.
# A directory rename like ap-web/ -> web/ is reported by git per-file, so the
# raw gone-set may only contain deep prefixes (ap-web/app, ap-web/lib) and not
# the bare parent (ap-web). Walking up to the root lets the vanished-upstream
# filter below settle on the SHALLOWEST directory that truly disappeared, which
# is what catches a fork file added in a brand-new sibling subdir (ap-web/new/).
# We never emit "." so the repo root can never become a prefix.
prefixes="$(
  while IFS= read -r d; do
    [ -n "$d" ] || continue
    cur="$d"
    while [ -n "$cur" ] && [ "$cur" != "." ]; do
      printf '%s\n' "$cur"
      parent="$(dirname "$cur")"
      [ "$parent" = "$cur" ] && break
      cur="$parent"
    done
  done <<<"$gone" | sort -u
)"

# For each candidate prefix, only treat it as "vanished" if it holds NO file in
# the new upstream tree. This avoids false positives from a PARTIAL rename (one
# file moved out of a directory upstream still populates) — there the surviving
# directory is legitimate and a fork file under it is not orphaned. For a
# genuinely-vanished prefix, every tracked file still under it is an orphan; we
# match with a LITERAL pathspec (`-- "$prefix/"`), never a regex, so directory
# names containing regex metacharacters are matched exactly.
orphans=""
while IFS= read -r prefix; do
  [ -n "$prefix" ] || continue
  if [ -n "$(git ls-tree -r --name-only "$UPSTREAM_REF" -- "$prefix/" 2>/dev/null)" ]; then
    continue   # still exists upstream — not vanished.
  fi
  hits="$(git ls-files -- "$prefix/")"
  [ -z "$hits" ] || orphans+="${hits}"$'\n'
done <<<"$prefixes"

orphans="$(printf '%s' "$orphans" | sed '/^$/d' | sort -u)"

if [ -n "$orphans" ]; then
  echo "::error::check-no-orphans: tracked files orphaned under upstream-removed directories:"
  printf '%s\n' "$orphans"
  echo "::error::Upstream renamed-away or deleted the directory these files live under, but they did not move with the rebase (a fork-added file under a vanished path). Relocate them to the new upstream path — or delete them — then re-run. Refusing to publish the orphaned tree."
  exit 1
fi

echo "check-no-orphans: no orphaned files under upstream-removed directories. OK."
