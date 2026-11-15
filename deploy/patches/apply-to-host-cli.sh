#!/usr/bin/env bash
# apply-to-host-cli.sh — apply the fork's host-side patches to the uv-installed
# omnigent CLI (the Mac host runner is PyPI omnigent, not the fork image).
#
# Re-run after every `uv tool install/upgrade omnigent`; an upgrade silently
# drops the patches. Idempotent: an already-applied patch is skipped. A patch
# that neither applies nor reverses means upstream changed the code: re-check
# whether upstream fixed it, then delete or regenerate the patch.
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
site="$(echo "$(uv tool dir)"/omnigent/lib/python3.*/site-packages)"
[ -d "${site}/omnigent" ] || { echo "apply: no uv-installed omnigent at ${site}" >&2; exit 1; }

for p in "${here}"/*.patch; do
  name="$(basename "${p}")"
  # Only the package files exist in site-packages; tests ride along for the repo.
  if (cd "${site}" && git apply --include='omnigent/*' --reverse --check "${p}" 2>/dev/null); then
    echo "apply: ${name} already applied"
  elif (cd "${site}" && git apply --include='omnigent/*' "${p}"); then
    echo "apply: ${name} applied"
  else
    echo "apply: ${name} does not apply to $(omnigent --version 2>/dev/null)" >&2
    exit 1
  fi
done
echo "apply: restart the host: launchctl kickstart -k gui/\$(id -u)/ai.omnigent.host"
