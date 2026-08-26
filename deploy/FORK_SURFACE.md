# Fork surface atop upstream v0.11.0

How much we've forked, where the cost sits, and what the next upstream upgrade
should take. Measured 2026-08-25, after retiring managed sandbox hosting.

Regenerate any number here with:

```bash
git fetch origin --tags && git diff --numstat v0.11.0..HEAD
```

## The number that matters

A fork only costs you where it **edits files upstream also edits**. Pure
additions at fork-owned paths are free — upstream never touches them, so they
never conflict.

| | files | lines | rebase cost |
|---|---|---|---|
| **Modified upstream files** | **12** | **+361 −43** | **the real cost** |
| Fork-owned paths (`deploy/`, `tests/deploy/`, fork workflows) | 29 | +3,800 | none |

For comparison, the same measure one release earlier was **56 files / +4,664**,
and before the v0.7.0 upgrade **78 files / +7,302**.

## What the fork actually carries

| Feature | files | lines | why upstream doesn't cover it |
|---|---|---|---|
| Smart-routing judge output scan | 2 | +75 | Upstream reads the verdict from `output[0]`, which is a reasoning item behind a GLM gateway — the judge then fails open on every call. |
| Built-in bundle config guard | 1 | +35 | A truncated `tar xzf` onto the Fly volume leaves zero-byte spec files; without this the server registers the bundle and fails per-session at turn setup instead of once, loudly, at boot. |
| Terminal-first switcher pill (+ its iOS insets) | 4 | +172 | Fork UI for the mobile/desktop shell. |
| e2e_ui flake-timeout bumps | 5 | +79 | Local timing, not behaviour. |

Fork-owned (free) content: the polly / polly-fw bundles under `deploy/agents/`,
the fly configs and runbooks under `deploy/fly/`, and the fork publish /
daily-sync workflows.

## Retired 2026-08-25 — managed sandbox hosting

The single biggest cut the fork has taken. Both Fly apps ran
`sandbox.provider: e2b`, but the newest managed host row on either was ~6 weeks
old, so the capability was paying rebase tax for nothing. Gone:

- **Managed-runner REST auth** (~12 files, +1,900) — upstream's
  `_resolve_managed_runner_owner` covers only the WS tunnel handshake; the
  plain-HTTP callbacks were fork-only. Dead once no runner runs in a sandbox.
- **E2B composer resume** (10 files) — `host_managed` / `sandboxProvider`
  threading so an aged-out sandbox read as relaunchable.
- **claude-native managed startup** (3 files, +750) — `ensure_env_api_key_approved`
  pre-approved Claude Code's custom-API-key prompt, which only ever fired where
  keys were injected as env vars; the SessionStart/splash readiness gate went
  with it now that upstream maintains its own readiness gate.
- **pi env-var provider fallback** — existed for config-less sandboxes.
- **Build provenance** (9 files, +586) — `/health` version, `/v1/info.build` and
  the Settings "Build & deployment" panel. `fly status` already names the live
  image tag.
- The `daytona,e2b` image extras (`OMNIGENT_EXTRAS` build-arg).

Deployment-side companions: drop the `sandbox:` block from each app's
`/data/artifacts/config.yaml` and the now-unused provider secrets.

## Fixed by config instead of by fork

The pi+Fireworks patch (`wire_api` inferred from the base URL, so a
chat-completions-only gateway isn't sent to `/responses`) was **deleted** in
favour of one line in `~/.omnigent/config.yaml`:

```yaml
providers:
  fireworks:
    openai:
      base_url: https://api.fireworks.ai/inference/v1
      wire_api: chat        # <- what the fork patch used to infer
```

This is the rule to keep applying: fix by config, or at a fork-owned path,
before patching an upstream file.

## Next upgrade: expected effort

With 12 modified upstream files and no large single-file patch left, a
single-minor rebase should be conflict resolution on the order of minutes, not
hours. The dominant remaining risk is upstream restructuring `ChatPage.tsx`
(the largest remaining patch, +97) or the smart-routing client.

The cheapest further cut is the terminal-first switcher pill, if the mobile
shell can live with upstream's chrome.
