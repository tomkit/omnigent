# Fork surface atop upstream v0.7.0

How much we've forked, where the cost sits, and what the next upstream upgrade
should take. Measured 2026-07-31 at fork `main` = `d54b2f58`.

Regenerate any number here with:

```bash
git fetch origin --tags && git diff --numstat v0.7.0..HEAD
```

## The number that matters

A fork only costs you where it **edits files upstream also edits**. Pure
additions at fork-owned paths are free — upstream never touches them, so they
never conflict. Split the delta that way:

| | files | lines | rebase cost |
|---|---|---|---|
| **Modified upstream files** | **36** | **+1,712 −289** | **the real cost** |
| Pure additions to upstream dirs | 20 | +2,912 | low — git applies cleanly |
| Fork-owned paths (`deploy/`, `tests/deploy/`, fork workflows) | 29 | +3,703 | none |
| **Total** | **85** | **+8,327 −289** | — |

72 fork commits sit atop `v0.7.0`.

So the honest headline is **36 files with real conflict potential**, not 85 —
and about a fifth of the raw line count.

For comparison, the same measure just before the v0.7.0 upgrade work was
**78 files / +7,302**. Dropping dead code and moving fork-only content to
fork-only paths cut the conflict surface roughly in half.

## What the fork actually carries

| Feature | files | lines | why upstream doesn't cover it |
|---|---|---|---|
| Managed-runner REST auth | 12 | +1,889 −68 | Upstream's `_resolve_managed_runner_owner` covers only the **WS tunnel** handshake. The plain-HTTP callbacks an in-sandbox runner makes (snapshot / labels / agent / events / hooks) have no upstream fallback. Needed by any managed provider, e2b included. |
| claude-native managed startup | 3 | +748 −34 | `ensure_env_api_key_approved` + SessionStart injection gating — a headless managed launch otherwise hangs on Claude Code's custom-API-key prompt. |
| pi + Fireworks / model catalog | 8 | +593 −41 | Upstream still defaults OpenAI-family providers to the Responses API (Fireworks 404s on `/responses`); the env-var provider fallback for config-less sandboxes is fork-only. |
| Build provenance / `/v1/info` typing | 9 | +586 −58 | Fork-only; upstream's `UpdatesSection` is Electron-only. **The most droppable item here** if the fork ever needs to shrink further. |
| E2B composer resume | 10 | +242 −48 | Threads `sandbox_provider` through the snapshot so an aged-out E2B sandbox reads as relaunchable rather than dead-ending at "host offline". |

Plus ~15 upstream **test** files adapted to fork-changed signatures. Individually
cheap, collectively the most frequent source of small conflicts.

Fork-owned (free) content: the polly / polly-fw bundles under `deploy/agents/`,
the fly configs and runbooks under `deploy/fly/`, and the fork publish /
daily-sync workflows.

## Not fork reasons

Worth restating, because it's counter-intuitive:

- **Multi-user** — entirely upstream. The fork changes zero lines of
  `server/auth.py` / `routes/accounts_auth.py`. Pure config
  (`OMNIGENT_AUTH_PROVIDER=accounts` + a cookie secret).
- **e2b** — `onboarding/sandboxes/e2b.py` is upstream and untouched. Only the
  `e2b` extra in the image build was fork work.
- **Session environment tag** — upstream's `HostBadge` already shows it.

## Recent trend

The fork has been shrinking, and two of the last three changes landed at zero
rebase cost:

| PR | change | conflict-surface effect |
|---|---|---|
| #48 | drop Daytona idle-suspend + git-sync; move polly bundles to `deploy/agents/` | **−22 files** (78 → 56) |
| #49 | polly hermetic workers, codex-first review | **0** — entirely in `deploy/agents/` |
| #50 | drop the smart-routing verdict-parser patch | **−2 files** (back to vanilla) |
| #51 | bump the local-circuits image pin | **0** — fork-owned toml |

#49 is the pattern to keep repeating: agent-behaviour changes cost nothing
because the bundles live at a fork-owned path and ship via the volume
(`OMNIGENT_BUILTIN_AGENT_DIRS`), not the image.

## Next upgrade: expected effort

Grounded in the measured v0.5.1 → v0.7.0 upgrade (57 commits, 78 files,
+7,302 lines — a two-minor jump).

| Phase | v0.5.1 → v0.7.0 (actual) | v0.7.0 → next (estimate) |
|---|---|---|
| Rebase + conflict resolution | ~15 conflicted commits, 2 structural | 1.5–3 h |
| Re-port work orphaned by upstream file moves | 1 large commit (`sessions.py` split) | 0–2 h — **the swing factor** |
| Adapt upstream tests to fork signatures | 3 rounds | 0.5–1 h |
| Validation (lint, `npm run build`, suites) | ~11 min per sweep, several | 0.5–1 h |
| DB migration + deploy, per app | 950 s migrate + 440 s VACUUM on the 861 MB DB | 0.5–1 h each |

**Realistic total: half a day to a full day** for a comparable jump — versus
roughly a full day-plus last time. A single-minor jump with no file
restructuring could be ~2–3 hours.

### What drives the variance

The dominant risk is not line count — it's **upstream restructuring a file the
fork modifies heavily**. Last time, upstream exploded a 21k-line
`server/routes/sessions.py` into a package, which orphaned nine fork commits and
was the single most expensive item. The current equivalents to watch:

- `omnigent/claude_native_bridge.py` (+241 −34) — largest single modified file
- `omnigent/server/routes/sessions/**` + `_sessions/**` — already split once;
  a second reorganisation would hurt again
- `omnigent/runner/native/orchestration.py` — upstream moved this once already
- `openapi.json` (+177 −23) — conflicts on nearly every upgrade, but is
  **generated**: resolve by running `scripts/dump_openapi.py`, never by hand

### Known upstream change already scheduled

v0.7.0 deprecates `HARNESS_<NAME>_PATH` in favour of `OMNIGENT_<NAME>_PATH`,
**slated for removal in v0.8.0**. The fork adds no uses of it (verified: zero
occurrences in fork-added lines), so that removal should pass through cleanly.

### Cheapest ways to shrink further

1. Drop build provenance (9 files, +586) if the Settings panel isn't worth it.
2. Upstream the managed-runner REST auth — it's the largest block (12 files,
   +1,889) and is a genuine gap in upstream, not a local preference.
3. Keep putting agent-behaviour changes in `deploy/agents/`, per #49.

## Procedure

The upgrade mechanics — landing a rebase by fast-forward, the in-container DB
migration, the `fly ssh sftp put` throughput ceiling — are in
[`fly/RUNBOOK.tomkit.md`](fly/RUNBOOK.tomkit.md).
