# Fork image + bidirectional context sync — Fly runbook

How this Fly deployment (`omnigent-tomkit`) runs a **fork-built** server image and
how **bidirectional git context sync** is turned on. Everything here is opt-in:
a stock deployment needs none of it.

Two independent capabilities are covered:

1. **Fork server image** — Fly runs an image built from *your fork's* `main`
   (carrying local patches) instead of upstream `ghcr.io/omnigent-ai/...:latest`,
   so your changes survive instead of being overwritten by upstream publishes.
2. **Bidirectional context sync** — a managed Daytona session clones a `workspace`
   repo into the sandbox, the agent works *inside* it, and pushes its branch back,
   authenticated by a `GIT_TOKEN` forwarded into the sandbox. Skills and memory
   ride along inside that same repo under `.claude/`.

---

## Part 1 — Fork server image

### What publishes the image

`.github/workflows/fork-publish-server.yml` builds and pushes on every push to
`main` of the fork:

- `ghcr.io/<owner>/omnigent-server:latest`
- `ghcr.io/<owner>/omnigent-server:sha-<short>`  ← immutable, deploy from this

The first publish creates the GHCR package **private**. Flip it to **Public**
once (CI cannot): *GitHub → your profile → Packages → omnigent-server →
Package settings → Danger Zone → Change visibility → Public*. Fly then pulls it
unauthenticated. (Alternatively keep it private and set Fly registry creds.)

> The workflow short SHA is git's default 7 chars, e.g. `sha-68a96a1` — not 8.
> Read the real tag from the registry before deploying (below).

### Fly config

`deploy/fly/fly.tomkit.toml` mirrors the live machine exactly (mounts, health
check, port, VM size, region) and differs from `fly.toml` only by pointing
`[build].image` at the fork's GHCR repo. Before any cutover, confirm the toml's
`[env]` block still matches the running machine — `fly deploy` applies `[env]`
wholesale, so a dropped/changed key silently alters prod.

```bash
# Verify the live machine's env (read-only) and compare against fly.tomkit.toml [env]
fly machine list -a omnigent-tomkit
fly machine status <machine-id> -a omnigent-tomkit --json | jq '.config.env'
```

### Cutover (deliberate, never auto unless you opt in)

```bash
APP=omnigent-tomkit

# 1. Find the real immutable tag actually published
TOKEN=$(curl -s "https://ghcr.io/token?scope=repository:<owner>/omnigent-server:pull" | jq -r .token)
curl -s -H "Authorization: Bearer $TOKEN" \
  https://ghcr.io/v2/<owner>/omnigent-server/tags/list | jq .tags
# -> ["sha-XXXXXXX", "latest"]; use the sha- tag

# 2. Snapshot BEFORE touching anything (rollback baseline)
fly status -a $APP
fly secrets list -a $APP        # names + digests only
fly volumes list -a $APP

# 3. Cut over, pinned to the immutable sha tag (NOT :latest)
fly deploy -c deploy/fly/fly.tomkit.toml -a $APP \
  --image ghcr.io/<owner>/omnigent-server:sha-XXXXXXX

# 4. Verify it only swapped the image
fly machine status <machine-id> -a $APP --json | jq '.config.image'   # -> fork sha tag
fly secrets list -a $APP        # OMNIGENT_CONFIG + others: digests UNCHANGED
fly volumes list -a $APP        # same volume still attached to same machine
curl -s https://$APP.fly.dev/health   # -> {"status":"ok"}
```

### Rollback

```bash
fly deploy -c deploy/fly/fly.tomkit.toml -a omnigent-tomkit \
  --image ghcr.io/omnigent-ai/omnigent-server:latest
```

### Optional: auto-deploy on publish

The publish workflow has a `deploy-fly` job that stays **inert** unless a
`FLY_API_TOKEN` repo secret exists. Add that secret to enable hands-off deploys
of every `main` push. Leave it unset to keep cutovers manual (current posture).

---

## Part 2 — Activating bidirectional context sync

The sync code ships in the server but does nothing until two things are set:
`GIT_TOKEN` is on the Daytona env allowlist, and the `GIT_TOKEN` secret exists.

### Where the config actually lives

`OMNIGENT_CONFIG` is a **path**, not inline YAML. On this app it points at a file
on the persistent volume:

```
OMNIGENT_CONFIG = /data/artifacts/config.yaml
```

So you edit that **volume file** — do NOT try to re-set the secret with inline
YAML. Edit over SSH, always backing up first:

```bash
fly ssh console -a omnigent-tomkit
# inside the machine:
cp /data/artifacts/config.yaml /data/artifacts/config.yaml.bak.$(date +%Y%m%d%H%M%S)
vi /data/artifacts/config.yaml
```

Add `GIT_TOKEN` to the Daytona env allowlist (forwards the secret *by name* into
every sandbox — the value comes from the Fly secret, never from this file):

```yaml
sandbox:
  provider: daytona
  server_url: https://omnigent-daytona-relay.<...>.workers.dev
  daytona:
    env: [OPENAI_API_KEY, OPENAI_BASE_URL, GIT_TOKEN]   # <- add GIT_TOKEN
```

This edit is inert until the next restart and carries no secret (only the var
name), so it is safe to make ahead of time.

### Set the token (triggers the reload)

Mint a fine-grained GitHub PAT — **Contents: Read and write** on the repos you'll
use as managed-session workspaces (Metadata auto-included; everything else No
access). Then set it (the restart reloads the edited config):

```bash
fly secrets set GIT_TOKEN=github_pat_xxxxxxxx -a omnigent-tomkit
```

A secret-set on this app restarts the machine (~30s) but preserves
`OMNIGENT_CONFIG` and the volume — verified.

### Verify

```bash
fly secrets list -a omnigent-tomkit                       # GIT_TOKEN present
fly ssh console -a omnigent-tomkit -C 'sh -lc "echo ${#GIT_TOKEN} ${GIT_TOKEN%%_*}"'  # length + github prefix
fly ssh console -a omnigent-tomkit -C 'cat /data/artifacts/config.yaml'  # env includes GIT_TOKEN
curl -s https://omnigent-tomkit.fly.dev/health            # {"status":"ok"}
```

### Using it — workspace + shared context convention

Launch a managed Daytona session with a **`workspace`** = a git repo URL, with an
optional `#branch`:

```
https://github.com/<owner>/<repo>#<branch>
```

The repo is cloned into the sandbox, the agent runs *inside* the clone, and
pushes its branch back (authenticated by the injected `GIT_TOKEN`). You push from
your laptop; the sandbox sees it on the branch; the sandbox pushes; you pull.
Bidirectional, over plain git.

Because the host process runs inside the clone, the Claude Code harness discovers
skills and memory natively from that one repo — put them **inside each project
repo**, mirroring the harness layout:

```
<repo>/
  CLAUDE.md                       # shared project memory (auto-discovered)
  .claude/skills/<name>/SKILL.md  # shared skills (laptop + sandbox both see them)
```

No separate context repo and no extra config: a session has one workspace, and
both hosts resolve the same `.claude/skills/` and `CLAUDE.md` from it.

### Rollback (deactivate sync)

Remove `GIT_TOKEN` from `daytona.env` in `/data/artifacts/config.yaml` (restore a
`.bak`) and/or `fly secrets unset GIT_TOKEN -a omnigent-tomkit`. Sandboxes simply
stop receiving the token; everything else is unaffected.
