# Runbook — fork server image on Fly (`omnigent-tomkit`)

This is the fork-specific deploy runbook. It explains how the
`omnigent-tomkit` Fly app gets **our** fork code instead of the upstream image,
how to do the first manual cutover, how to verify the `OMNIGENT_CONFIG` secret
and the data volume survive, and how to roll back.

## Why this exists

The live `omnigent-tomkit` machine was running the **upstream** image
`omnigent-ai/omnigent-server:latest`. That image does **not** contain our merged
fork PRs (idle-suspend / reclaim-retention, bidirectional context sync), and any
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
