#!/usr/bin/env bash
# Resolve the newest Fireworks GLM model id for Claude Code.
set -euo pipefail

fallback="${FIREWORKS_GLM_FALLBACK:-accounts/fireworks/models/glm-5p2}"
models_url="${FIREWORKS_MODELS_URL:-https://api.fireworks.ai/inference/v1/models}"
model=""
tmp="$(mktemp)"
err="$(mktemp)"
trap 'rm -f "$tmp" "$err"' EXIT

warn() {
  echo "::warning::resolve-fireworks-glm-model: $*" >&2
}

if [ -z "${FIREWORKS_API_KEY:-}" ]; then
  warn "FIREWORKS_API_KEY is unset; falling back to ${fallback}."
else
  http_code="$(
    curl -sS \
      --connect-timeout 10 \
      --max-time 30 \
      -o "$tmp" \
      -w '%{http_code}' \
      -H "Authorization: Bearer ${FIREWORKS_API_KEY}" \
      "$models_url" 2>"$err" || true
  )"

  if [ "$http_code" = "200" ]; then
    if command -v jq >/dev/null 2>&1; then
      # Fireworks currently returns {"object":"list","data":[{"id":"..."}]}.
      # Accept a few common list shapes so a harmless envelope tweak does not
      # break the daily sync, then sort glm-XpY numerically (5p10 > 5p2).
      if latest="$(
        jq -r '
          if type == "object" then (.data // .models // .items // [])
          else .
          end
          | if type == "array" then .[] else empty end
          | if type == "string" then .
            else (.id // .name // empty)
            end
        ' "$tmp" 2>/dev/null \
          | awk -F'[-p]' '/^accounts\/fireworks\/models\/glm-[0-9]+p[0-9]+$/ { printf "%d\t%d\t%s\n", $(NF-1), $NF, $0 }' \
          | sort -t "$(printf '\t')" -k1,1nr -k2,2nr \
          | head -n 1 \
          | cut -f3-
      )" && [ -n "$latest" ]; then
        model="$latest"
      else
        warn "models API returned no usable glm-XpY ids; falling back to ${fallback}."
      fi
    else
      warn "jq is unavailable; falling back to ${fallback}."
    fi
  else
    detail="$(tr '\n' ' ' <"$err" | tr -s '[:space:]' ' ' | cut -c1-240)"
    warn "models API failed with HTTP ${http_code:-curl-error}${detail:+ (${detail})}; falling back to ${fallback}."
  fi
fi

model="${model:-$fallback}"

if [ -n "${GITHUB_ENV:-}" ]; then
  {
    echo "ANTHROPIC_MODEL=${model}"
    echo "ANTHROPIC_SMALL_FAST_MODEL=${model}"
  } >>"$GITHUB_ENV"
fi

echo "Resolved Fireworks GLM model: ${model}"
