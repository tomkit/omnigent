# Fork maintenance

This repository is a **fork** of upstream Omnigent. It tracks upstream closely;
the fork's own changes live as a small stack of commits **on top of** upstream
and are **rebased** (not merged) whenever we pull upstream in.

## Remotes

| Remote   | URL                                          | Role                         |
| -------- | -------------------------------------------- | ---------------------------- |
| `origin` | `https://github.com/omnigent-ai/omnigent.git` | **upstream** — never PR here |
| `fork`   | `git@github.com:tomkit/omnigent.git`          | this fork — PRs go here (base `main`) |

Update flow: fetch `origin`, rebase the fork's commits onto `origin/main`,
force-push the fork's `main`. Because the customizations are commits-on-top
(ideally additive, new files only), rebases stay clean and conflict-free.

## The deploy image must carry the `daytona` extra

The fork runs on Fly as the **`omnigent-tomkit`** app and uses managed
`sandbox.provider: daytona` sessions. The Daytona launcher
(`omnigent/onboarding/sandboxes/daytona.py`, `_ensure_sdk` → `import daytona`)
**lazily imports the `daytona` SDK**, so the deployed server image must be built
with the `daytona` Python extra (`pyproject`: `daytona = ["daytona>=0.180,<1"]`).
Without it, every managed Daytona launch fails with:

> The Daytona SDK is required for the 'daytona' sandbox provider.

Upstream's `deploy/fly/fly.toml` pulls the **prebuilt** public image
(`ghcr.io/omnigent-ai/omnigent-server:latest`), which is built with **no**
extras. The fork therefore deploys with its own additive config,
**`deploy/fly/fly.tomkit.toml`**, which builds the image from
`deploy/docker/Dockerfile` and passes the build-arg `OMNIGENT_EXTRAS = "daytona"`
(the Dockerfile runs `uv pip install -e ".[${OMNIGENT_EXTRAS}]"` only when that
arg is non-empty). Deploy with:

```bash
fly deploy -c deploy/fly/fly.tomkit.toml -a omnigent-tomkit
```

This builds from source via Fly's remote builder and swaps the image in place;
the named `artifact_data` volume and the fly secrets are untouched.

`DAYTONA_API_KEY` is a **Fly secret** (`fly secrets set DAYTONA_API_KEY=… -a
omnigent-tomkit`), already set on the app — it is **not** stored in this repo.

### Regression guard

`tests/deploy/test_fly_fork_daytona_extra.py` parses `fly.tomkit.toml` and fails
if it stops declaring `daytona` in `OMNIGENT_EXTRAS` (or stops building from the
Dockerfile). This keeps an upstream rebase from silently dropping the extra. Run
it with:

```bash
pytest tests/deploy/test_fly_fork_daytona_extra.py
```

### Keep this consistent with the daily fork-sync workflow

A **separate** PR adds `.github/workflows/daily-fork-sync.yml`, a GitHub Actions
workflow that automates the daily upstream rebase + redeploy. That automated
redeploy path **must also carry the `daytona` extra** (same `OMNIGENT_EXTRAS =
"daytona"` build-arg / same `fly.tomkit.toml`). If you change how the image is
built here, update the workflow too, and vice versa — the guard test above
protects the config this PR introduces.
