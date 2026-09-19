# Fork surface atop upstream v0.14.0

How much we've forked, where the cost sits, and what the next upstream upgrade
should take. Measured 2026-09-19, after pruning every remaining patch to an
upstream-owned file.

Regenerate any number here with:

```bash
git fetch origin --tags && git diff --numstat v0.14.0..HEAD
```

## The number that matters

A fork only costs you where it **edits files upstream also edits**. Pure
additions at fork-owned paths are free — upstream never touches them, so they
never conflict.

| | files | lines | rebase cost |
|---|---|---|---|
| **Modified upstream files** | **0** | **0** | **none** |
| Fork-owned paths (`deploy/agents`, `deploy/fly`, `tests/deploy`, fork workflow + script) | 17 | +2,200 | none |

For comparison: **12 files / +361** at v0.11.0, **56 / +4,664** at v0.9.0, and
**78 / +7,302** before the v0.7.0 upgrade.

## What the fork actually carries

Only content at fork-owned paths:

- **`deploy/agents/polly`** — the polly bundle, delivered to the Fly volume via
  `OMNIGENT_BUILTIN_AGENT_DIRS`. (`polly-fw`, the Pi-only sandbox variant, was
  deleted here: zero conversations on either app since sandboxes were retired.)
- **`deploy/fly/`** — the two app configs and `RUNBOOK.tomkit.md`.
- **`.github/workflows/fork-publish-server.yml`** — builds the fork's server
  image to GHCR from upstream's unmodified `Dockerfile`.
- **`.github/scripts/sync-fork.sh`** — the whole upgrade recipe (below).
- **`tests/deploy/test_fork_agent_bundles.py`** — parses the bundles.

## Pruned 2026-09-19 (v0.11.0 → v0.14.0)

Everything that touched an upstream file, and why each was safe to drop:

| Patch | Why it's gone |
|---|---|
| Terminal-first Chat/Terminal pill (+ iOS insets, 2 tests) | It re-added an in-band pill upstream had moved to the header `ViewModeToggle`; at v0.14.0 that toggle covers every shell, iOS included, and `ConnectionIndicator` moved to `ChatIndicators.tsx`. |
| Built-in bundle config guard (`app.py`) | Belt-and-braces: the runbook's untar step already refuses a tree with zero-byte files. Upstream churned `app.py` by +683 this cycle. |
| Smart-routing judge output scan | Upstream still parses `output[0]`, so the judge fails open behind a reasoning-model gateway. Accepted: polly's `smart_routing_harness: auto` is inert. Fix by pointing the `llm:` judge at a non-reasoning model, or upstream the scan. |
| e2e_ui flake-timeout bumps | Local timing; two already conflicted. |

## Fixed by config instead of by fork

The pi+Fireworks `wire_api` patch was deleted at v0.11.0 in favour of one
config line. At v0.14.0 even that line is redundant: `pi_executor` defaults
any generic OpenAI-compatible `base_url` to chat completions (verified live on
pure upstream — a Fireworks GLM turn completes with or without `wire_api:
chat`). Only an explicit `wire_api: responses` sends pi to `/responses`.

Note the host-side floor: v0.14.0 requires **pi ≥ 0.84.2**; older binaries read
as "harness 'pi' is not configured".

This is the rule to keep applying: fix by config, or at a fork-owned path,
before patching an upstream file.

## Next upgrade: expected effort

Zero conflicts by construction. Run `.github/scripts/sync-fork.sh vX.Y.Z`: it
checks out the tag, copies the fork-owned paths from `fork/main`, and refuses
if any copied path also exists in the tag (`deploy/docker`, `deploy/databricks`
and `.github/scripts/homebrew` are upstream's — never carry them). Then update
this file, run `pre-commit` + `pytest tests/deploy`, commit, and land by
fast-forwarding main.

The remaining upgrade work is deployment, not code: image build, in-container
migrate + `ANALYZE`, and the Mac host CLI / harness binaries — see
`deploy/fly/RUNBOOK.tomkit.md`.
