# Hermes Agent Railway Template

## Architecture

Python supervisor that runs the official Hermes dashboard and keeps the messaging gateway alive.

- `supervisor.py` — PID-1 child: starts the official authenticated dashboard and restarts `hermes gateway` if it exits
- `server.py` and `templates/index.html` — retained legacy wrapper UI; not used by `start.sh`
- Hermes state is persistent under `/data/.hermes`
- The official dashboard listens on `$PORT`; Railway terminates HTTPS

## Key patterns

- Dashboard authentication fails closed when no admin password is configured
- Existing `ADMIN_USERNAME` / `ADMIN_PASSWORD` are mapped to Hermes dashboard Basic Auth
- Dashboard session signing is stable without logging or persisting the plaintext password
- The gateway runs in an independent process group and restarts after unexpected exits
- `/api/health` is the unauthenticated Railway health endpoint

## Image build

- `Dockerfile` pins Hermes with `ARG HERMES_COMMIT`. To upgrade: bump it on a branch, wait for the `Validate Railway image` CI, then merge (`main` auto-deploys)
- Hermes lives in `/usr/local/lib/hermes-agent`: sealed `.venv`, plus the PM tool store `tools/` (Python 3.14.7, node, ffmpeg, Chromium, agent-browser)
- Hermes's Python is pinned by its own PM lock. The base image's Python only runs `supervisor.py` and the PM setup, so don't change it on its own
- `.git` is stripped at build time, so `hermes --version` reads `v0.0.0 · upstream <HERMES_COMMIT>`
- `uv` is internal PM tooling: `pm.installed_package("uv")` raises. Never link it onto PATH
- `.dockerignore` lets only `Dockerfile`, `supervisor.py` and `start.sh` into the build. `server.py`/`templates/` are not in the image
- The CI smoke test covers imports and dashboard `/api/health` only. It never starts the Telegram gateway

## Volume gotchas (/data)

- Hermes prefers an environment recorded in `/data/.hermes/installs/<key>/facts.json` over the image `.venv` (the key comes from the install path). If `/proc/<pid>/maps` shows `.so` files loaded from `/data/.hermes/installs/…`, move `installs` aside and restart
- Never run `hermes pm repair` in the container: it records a ~1.7 GB environment in `installs/<key>` that takes over on the next restart. Undo by removing `installs/<key>/facts.json` and `environments/` before restarting
- Dashboard action buttons (Backup, gateway restart) refuse with "no dependency environment is committed": `runtime_command()` launches PM's bare store Python, which needs a recorded environment. Back up with `railway ssh -- hermes backup` (CLI uses the image `.venv`; zip lands in `/data`)
- A stale `/data/.hermes/hermes-agent` git checkout makes `hermes --version` report the wrong upstream. It should not exist
- The Browser Use CLI (`browser-use==0.13.10`) lives in `/data/.hermes/environments/browser-use` by Hermes design. Reinstall with `tools.browser_use_cli.install_cli()` after an image Python change
- `browser.backend` is unset: Hermes uses Browser Use (`browser_exec`) when the CLI exists, otherwise its built-in browser tools

## Railway config

- Service settings live in `.railway/railway.ts` (IaC); `railway.toml` is gone
- To change settings: edit the file → `railway config plan` → `railway config apply`. Pushing the file alone applies nothing, and `apply` restarts the service
- Run `npm install` at the repo root first: `config plan` needs the `railway` SDK from devDependencies
- `build.builder: "DOCKERFILE"` is load-bearing: the service's own saved builder is `RAILPACK`
- Don't declare restart policy `ON_FAILURE`/10. It's Railway's default, stored as null, so declaring it shows as drift on every plan

## Ops

- Inspect the live container with `railway ssh -- <cmd>`. Never print `/proc/*/environ` unfiltered: it contains `ADMIN_PASSWORD`
- To run Hermes Python in the container, mirror `libexec/hermes`: `PYTHONPATH=$R:$S HERMES_SITE=$S $R/.venv/bin/python -P -c "import os,site; site.addsitedir(os.environ['HERMES_SITE']); import hermes_cli.main; …"`, with `R=/usr/local/lib/hermes-agent` and `S=$R/.venv/lib/python3.14/site-packages`
- `railway restart -y` may not return. Verify with `/api/health` and "Connected to Telegram" in `railway logs`
