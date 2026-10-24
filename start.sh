#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions
mkdir -p /data/.hermes/skills
mkdir -p /data/.hermes/workspace
mkdir -p /data/.hermes/pairing

# Complete the pinned default-profile provider transition before either child
# can open its databases. A failed preflight must not start a partial gateway.
/usr/local/lib/hermes-agent/.venv/bin/python /app/prepare_holographic.py

exec python /app/supervisor.py
