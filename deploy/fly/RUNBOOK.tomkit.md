# Runbook — fork server image on Fly (`omnigent-tomkit`)

This is the fork-specific deploy runbook. It explains how the
`omnigent-tomkit` Fly app gets **our** fork code instead of the upstream image,
how to do the first manual cutover, how to verify the `OMNIGENT_CONFIG` secret
and the data volume survive, and how to roll back.

## Why this exists

The live `omnigent-tomkit` machine was running the **upstream** image
`omnigent-ai/omnigent-server:latest`. That image does **not** contain our merged
fork PRs (managed-runner REST auth, pi+Fireworks, claude-native startup), and any
redeploy pulling upstream `:latest` overwrites them. So none of the fork code
actually ran in production.

The fix is two parts:

1. **`.github/workflows/fork-publish-server.yml`** builds the server image from
   `deploy/docker/Dockerfile` on every push to fork `main` and publishes it to
   **`ghcr.io/tomkit/omnigent-server`** (`:latest` + `:sha-<short>`), using the
   built-in `GITHUB_TOKEN` — no org secrets.
2. **`deploy/fly/fly.tomkit.toml`** pins the app to that image. It mirrors the
   live machine's env, port, health check, volume mount, and size exactly, so a
   deploy only swaps the image.

```
fork main push ──▶ GitHub Actions build ──▶ ghcr.io/tomkit/omnigent-server:{latest,sha-XXXX}
                                                   │
                          (manual first cutover, or gated auto-deploy)
                                                   ▼
                                 fly deploy -c fly.tomkit.toml ──▶ omnigent-tomkit.fly.dev
```

## Build → publish → deploy flow

- **Build + publish**: automatic on push to fork `main` (or run the workflow
  manually via the Actions tab → *Publish fork server image* → *Run workflow*).
  Produces `ghcr.io/tomkit/omnigent-server:latest` and `:sha-<short>`.
- **Make the package public (ONE TIME, required)**: the first publish creates
  the GHCR package **private**. Fly's remote builder pulls it unauthenticated,
  so flip it to public:
  GitHub → your profile → *Packages* → `omnigent-server` → *Package settings* →
  *Change visibility* → **Public**. (Cannot be done from CI.)
- **Deploy**: either the manual cutover below, or the gated auto-deploy leg.

## First manual cutover (do this once, you = polly)

> Prereqs: `flyctl` authenticated (`fly auth login`), and the GHCR package set
> to **public** (above). Nothing here recreates the app or the volume.

```bash
# 0. From the repo root. Confirm the current (upstream) image + that the
#    volume and secrets are present BEFORE touching anything.
fly status   -a omnigent-tomkit          # Image should read omnigent-ai/omnigent-server:latest
fly volumes  list -a omnigent-tomkit      # artifact_data, 1GB, iad — note the vol_ id
fly secrets  list -a omnigent-tomkit      # OMNIGENT_CONFIG, OPENAI_API_KEY, OPENAI_BASE_URL, DAYTONA_API_KEY

# 1. Pick the image to cut over to. Use the immutable per-commit tag from the
#    latest successful "Publish fork server image" run (preferred over :latest).
IMG=ghcr.io/tomkit/omnigent-server:sha-<short>   # e.g. sha-66692cad
#    (or IMG=ghcr.io/tomkit/omnigent-server:latest)

# 2. Deploy. fly.tomkit.toml mirrors the live machine, so this ONLY swaps the
#    image — same volume, same internal port (8000), same /health check.
fly deploy -c deploy/fly/fly.tomkit.toml -a omnigent-tomkit --image "$IMG"

# 3. Confirm the new image is live and healthy.
fly status -a omnigent-tomkit            # Image should now read ghcr.io/tomkit/omnigent-server:...
fly logs   -a omnigent-tomkit            # watch boot; health check should pass
```

A single-machine app deploys in place (rolling). The volume stays attached by
name; the machine is updated, not destroyed.

## Verify the secret + volume survive the deploy

The deploy must NOT disturb `OMNIGENT_CONFIG` (app config) or the data volume
(artifact store + minted cookie secret + SQLite DB at
`/data/artifacts/chat.db`). Verify after step 3:

```bash
# Secret still present (digest unchanged from the pre-deploy listing):
fly secrets list -a omnigent-tomkit | grep OMNIGENT_CONFIG

# Volume still the SAME vol_ id, still attached to the machine:
fly volumes list -a omnigent-tomkit

# Data intact: the SQLite DB and admin credentials are still on the volume.
fly ssh console -a omnigent-tomkit -C "ls -la /data/artifacts"   # chat.db, admin-credentials present

# App answers and the login/session you had before still works:
curl -fsS https://omnigent-tomkit.fly.dev/health      # -> ok
```

`fly deploy` never clears secrets and never detaches a named volume, so both
carry over. If `OMNIGENT_CONFIG` ever needs re-setting it is
`fly secrets set OMNIGENT_CONFIG="$(cat config.json)" -a omnigent-tomkit` — but
that is NOT part of a normal image swap.

## Managed-sandbox config (`/data/artifacts/config.yaml`)

The server reads its app config from the file the `OMNIGENT_CONFIG` env var
points at — `/data/artifacts/config.yaml`, **on the persistent volume** (not in
git, not the secret store). It carries the `sandbox:` block that drives managed
Daytona hosts. The live shape:

```yaml
sandbox:
  provider: daytona
  server_url: https://omnigent-daytona-relay.zz957kkf2k.workers.dev
  daytona:
    env: [OPENAI_API_KEY, OPENAI_BASE_URL, GIT_TOKEN, ANTHROPIC_API_KEY]
    image: ghcr.io/tomkit/omnigent-host@sha256:<digest>   # the fork host image
    idle_minutes: 30        # REQUIRED on free tier — see below
```

> [!IMPORTANT]
> **`idle_minutes: 30` is not optional on the Daytona free tier.** Without it,
> every managed host is created always-on (`auto_stop_interval=0`) and never
> releases its slice of the **10 GiB org memory cap** — a few live sessions
> exhaust the quota and *new* sandbox launches fail with
> `Total memory limit exceeded. Maximum allowed: 10GiB`. With it, Daytona stops
> an idle host after 30 min (freeing the memory) and the server's wake path
> resumes it in place on the next message, reattaching the same workspace disk
> (`auto_delete_interval` is pinned to disabled so the disk survives the stop).

Editing it (the change is **not** picked up until the machine restarts):

```bash
# Back up, then edit in place over SSH (or fly ssh sftp get/put):
fly ssh console -a omnigent-tomkit -C \
  "/bin/sh -c 'cp /data/artifacts/config.yaml /data/artifacts/config.yaml.bak.$(date +%s)'"
# …write the new file… then restart to load it:
fly machine restart $(fly machines list -a omnigent-tomkit --json | jq -r '.[0].id') -a omnigent-tomkit
```

Verify it took effect by creating a managed session and inspecting the new
sandbox: `auto_stop_interval` should read `30` (was `0`) and
`auto_delete_interval` `-1` (disabled) via the Daytona SDK / dashboard.

The host `image:` digest is bumped here whenever a new `omnigent-host` image is
published (see the host-image half of `fork-publish-server.yml`); pin the
immutable `@sha256:` digest, not `:latest`.

## Known issue (upstream v0.7.0): smart routing strands polly's native children

Keep the per-session **smart-routing (cost) toggle OFF for polly sessions** on
this release. With the toggle on, the server force-routes every child session
polly spawns (`_force_auto_for_child` in
`omnigent/server/routes/_sessions/orchestration.py`): the judge picks from the
SDK candidate set (`claude-sdk` / `codex` / `pi`) and persists the pick as the
child's `harness_override`, so a worker declared `codex-native` can actually
run `pi`. Turn-end status then branches on the *declared* harness — upstream's
`_publish_turn_status` early-returns for native harnesses on non-failed
statuses, `_on_proxy_stream_end` isn't in a `finally`, and the `pi` harness has
no `external_session_status` delivery path — so the child's idle edge and the
parent wake are both dropped and the sub-agent session sits at
`status: running` forever (observed on the pr66/pr67 review children).

The trigger was the fork's smart-routing verdict-parsing fix (`7988cace`,
since reverted): vanilla upstream fails to parse the judge verdict behind a
reasoning-model gateway and routing fails open, so with the fork back on the
vanilla parser the toggle is harmless on this deployment — the judge never
returns a verdict, children keep their declared harnesses.

The defect re-arms if routing ever starts succeeding again before upstream
fixes turn-end delivery: configuring `routing: {provider: external}` in
`/data/artifacts/config.yaml`, pointing the `llm:` judge at a non-reasoning
model, or an upstream release fixing the verdict parser alone. Until turn-end
delivery is fixed upstream, keep the routing toggle off for polly sessions in
any of those configurations. The verdict-parsing fix should be re-landed
UPSTREAM together with the turn-end fixes, not re-forked.

The polly bundle keeps `codex` on `codex-native` deliberately (watchable
terminal, human takeover); do not "fix" a stuck child by moving workers onto
SDK harnesses.

## Optional: enable auto-deploy on every fork `main` build

The workflow has a `deploy-fly` job that is **inert unless a `FLY_API_TOKEN`
repo secret exists**. To turn it on:

```bash
# Scoped deploy token for just this app:
fly tokens create deploy -a omnigent-tomkit
# Add the printed token as a GitHub Actions repo secret named FLY_API_TOKEN
# (Settings → Secrets and variables → Actions → New repository secret).
```

Once set, every published build also runs `fly deploy --image
ghcr.io/tomkit/omnigent-server:sha-<short>`. With no secret the job logs
"auto-deploy disabled" and skips — safe by default.

## Rollback to the upstream image

If the fork image misbehaves, revert to upstream `:latest` immediately:

```bash
fly deploy -c deploy/fly/fly.tomkit.toml -a omnigent-tomkit \
  --image omnigent-ai/omnigent-server:latest
```

This swaps only the image; the volume and secrets are untouched, so the rollback
is non-destructive and reversible. To roll back to a *previous fork* build
instead, deploy an earlier `ghcr.io/tomkit/omnigent-server:sha-<short>`. You can
also use `fly releases -a omnigent-tomkit` to see the release history and
`fly deploy ... --image <prev>` to pin any prior image.

## Upgrading across a schema change (the v0.7.0 lesson)

A release with table-rebuilding migrations must NOT be upgraded by letting the
new image boot and run `alembic upgrade head`: `deploy/docker/entrypoint.py`
migrates synchronously before binding `:8000`, so a long rebuild flaps the
health check into a crash loop, and an interrupted `batch_alter_table` strands
an `_alembic_tmp_<table>` that blocks every retry.

**Migrate in-container on an IDLE machine booted from the TARGET image.**

```bash
APP=omnigent-tomkit; M=<machine-id>; IMG=ghcr.io/tomkit/omnigent-server:sha-<short>
fly volume snapshots create <vol> -a $APP           # safety net
fly machine update $M -a $APP --image "$IMG" \
  --command "sleep infinity" --skip-health-checks --yes
# ...upload a migrate script, then run it with SQLite temp pinned to the volume:
#   SQLITE_TMPDIR=/data/artifacts TMPDIR=/data/artifacts python3 /data/artifacts/migrate.py
fly deploy -c deploy/fly/fly.tomkit.toml -a $APP --image "$IMG"   # restores CMD
```

Why in-container is safe here, despite the old "never migrate on Fly" rule:
that rule targets two specific causes, and both are removed. SQLite spills
table-rebuild temp files into the container **overlay** (slow) unless
`SQLITE_TMPDIR` points at the volume — pin it, and the rebuild runs on the same
fast device as the DB. And an idle machine has no health check to trip.

> [!IMPORTANT]
> **Do not plan on `fly ssh sftp put` for a multi-hundred-MB DB.** Measured at
> **~3.6 KB/s** against this app — ~26h for a 344 MB upload — while `sftp get`
> managed ~2.4 MB/s. Pulling the DB down to migrate locally is fine; pushing it
> back is not. Small files (a migrate script) upload fine.

Always finish with `ANALYZE` (the migrate script above does): table rebuilds and
`VACUUM` wipe `sqlite_stat1`, and without stats the planner picks catastrophic
plans — the observed failure was a child-session query taking >2min and pegging
every worker thread. Verify before restoring service: `PRAGMA integrity_check`
== ok, `alembic_version` == head, row counts unchanged, no `_alembic_tmp_*`
tables, and `sqlite_stat1` non-empty. Keep the pre-upgrade DB on the volume
(`chat.db.pre-v070`) until the new version has run clean for a while.

## Fork-owned agent bundles (`deploy/agents/`)

polly and polly-fw are **not** baked into the server image. They are delivered
to the Fly volume and registered with:

```
OMNIGENT_BUILTIN_AGENT_DIRS=/data/artifacts/polly-fw:/data/artifacts/polly
```

That env var (a Fly secret on both apps) **shadows** the `examples/` copies the
image bakes — so editing upstream's `examples/polly` changes nothing in
production while conflicting on every rebase. The fork's copies therefore live
at `deploy/agents/`, a path upstream never touches.

Syncing a bundle change to a deployment:

```bash
cd deploy/agents
# COPYFILE_DISABLE=1 is REQUIRED on macOS — a plain `tar` scatters ._* files
# (AppleDouble) across the volume, which then show up inside skills/ dirs.
COPYFILE_DISABLE=1 tar --no-xattrs -czf /tmp/polly.tar.gz polly
fly ssh sftp put /tmp/polly.tar.gz /data/artifacts/polly.new.tar.gz -a <app>
fly ssh console -a <app> -C "/bin/sh -c '
  cd /data/artifacts && rm -rf polly && tar xzf polly.new.tar.gz &&
  test -z \"$(find polly -type f -size 0)\" &&
  mv -f polly.new.tar.gz polly.tar.gz'"
fly machine restart <machine-id> -a <app>   # bundles are read at boot
```

> [!WARNING]
> **The `-size 0` guard is load-bearing.** An interrupted `tar xzf` leaves the
> files created but empty, and every step downstream still succeeds: the
> extract exits 0, the server boots, the agent registers. The damage only
> surfaces later, per session, as
> `turn setup failed: config.yaml must be a YAML mapping, got NoneType`.
> This happened on omnigent-tomkit (2026-08-04) — every file under
> `/data/artifacts/polly/` was zero-length while the tarball beside it was
> intact, and local-circuits was unaffected by the same sync. Recovery is to
> re-extract from that tarball and restart. The server now also refuses to
> register a bundle whose `config.yaml` is empty or unparseable
> (`_reject_unparseable_bundle_configs`), so a repeat is one loud boot error
> instead of a silent per-session failure — but keep the guard: it stops the
> bad tree from ever reaching the volume.

Verify by comparing **per-file hashes**, not just the parsed config — a partial
extract or a stray `._*` file will still parse fine:

```bash
cd deploy/agents/polly && find . -type f | sort | \
  while read f; do printf "%s  %s\n" "$(shasum -a 256 "$f" | cut -c1-16)" "$f"; done
fly ssh console -a <app> -C "/bin/sh -c 'cd /data/artifacts/polly && find . -type f | sort |
  while read f; do printf \"%s  %s\n\" \"\$(sha256sum \"\$f\" | cut -c1-16)\" \"\$f\"; done'"
```
