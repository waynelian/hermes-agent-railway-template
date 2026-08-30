# Hermes Agent Railway Template

Deploy [Hermes Agent](https://github.com/nousresearch/hermes-agent) on [Railway](https://railway.app) with the official authenticated Hermes dashboard and a supervised messaging gateway.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent)

## What you get

- **Official Hermes Dashboard** — profiles, Kanban boards, sessions, skills, configuration, cron jobs, plugins, chat, and system status
- **Authenticated HTTPS** — Railway terminates HTTPS; Hermes requires username/password authentication on the public bind
- **Gateway Supervision** — `hermes gateway` restarts after an unexpected exit
- **Persistent Storage** — configuration, OAuth credentials, sessions, memories, profiles, skills, and Kanban state survive container restarts under `/data`
- **Fail-Closed Startup** — the service refuses to start without a dashboard password

## Quick Start

### Deploy to Railway

1. Click the "Deploy on Railway" button above or connect this repository to a Railway service.
2. Set a strong `ADMIN_PASSWORD` Railway variable. Do not use a generated or shared password.
3. Optionally set `ADMIN_USERNAME`; the default is `admin`.
4. Attach a persistent volume mounted at `/data`.
5. Generate a Railway domain for service port `8080`. Railway provides HTTPS automatically.
6. Open the HTTPS URL and sign in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`.
7. Configure Hermes from the official dashboard.

The dashboard holds high-value access to provider credentials, messages, files, sessions, profiles, and tools. Keep the URL private and protect the Railway account with MFA.

### Run Locally with Docker

```bash
docker build -t hermes-agent .
docker run --rm -it \
  -p 8080:8080 \
  -e PORT=8080 \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD='replace-with-a-long-random-password' \
  -v hermes-data:/data \
  hermes-agent
```

Open `http://localhost:8080` and sign in with the configured credentials.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Official dashboard port; Railway routes HTTPS traffic here |
| `ADMIN_USERNAME` | `admin` | Dashboard username |
| `ADMIN_PASSWORD` | *(required)* | Dashboard password; startup fails if it is absent |
| `DASHBOARD_SESSION_SECRET` | derived | Optional independent seed for stable signed dashboard sessions |
| `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` | from `ADMIN_USERNAME` | Optional direct Hermes override |
| `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` | from `ADMIN_PASSWORD` | Optional direct Hermes override |
| `HERMES_DASHBOARD_BASIC_AUTH_SECRET` | derived | Optional direct session-signing-secret override |
| `HERMES_GATEWAY_RESTART_DELAY` | `3` | Seconds before restarting an unexpectedly exited gateway |

Secrets stay in Railway variables or the persistent Hermes store. The supervisor does not print dashboard credentials.

## Architecture

```text
Railway HTTPS
    │
    ▼
Official Hermes Dashboard ($PORT)
    ├── authenticated web UI
    ├── Profiles and Kanban
    ├── Sessions, skills, cron, plugins, and config
    └── /api/health (Railway health check)

supervisor.py
    ├── official dashboard process
    └── hermes gateway process (automatic restart)

/data/.hermes
    └── persistent config, credentials, sessions, profiles, skills, memory, and Kanban state
```

`tini` runs `/app/start.sh`, which executes `supervisor.py`. The dashboard is the critical process. If it exits, the service exits and Railway applies its restart policy. The gateway runs in its own process group and is restarted by the supervisor after an unexpected exit.

## Authentication behavior

The dashboard binds to `0.0.0.0`, which makes Hermes engage its public-dashboard authentication gate. The supervisor maps the existing Railway admin credentials to the official Hermes Basic Auth provider:

- `ADMIN_USERNAME` → `HERMES_DASHBOARD_BASIC_AUTH_USERNAME`
- `ADMIN_PASSWORD` → `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD`

A stable HMAC signing secret is derived without logging or storing the plaintext password in `config.yaml`. Set `DASHBOARD_SESSION_SECRET` when you want an independent session-signing seed.

## Health and verification

Railway checks:

```text
GET /api/health
```

Expected response includes `"ok": true` and `"auth_required": true`.

After deployment, verify:

1. `/` redirects to `/login` when no session exists.
2. Invalid credentials are rejected.
3. Valid credentials open the dashboard.
4. Profiles and the intended Kanban board appear.
5. `hermes gateway` is running and the messaging channel responds.
6. A gateway exit is followed by an automatic restart.

## Legacy wrapper UI

`server.py` and `templates/index.html` remain in the repository for migration reference. `start.sh` no longer runs the legacy Starlette wrapper. The official Hermes dashboard is the supported web interface.
