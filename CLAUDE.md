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
