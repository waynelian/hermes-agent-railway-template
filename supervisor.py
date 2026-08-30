#!/usr/bin/env python3
"""Supervise the Hermes gateway and authenticated official dashboard."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import sys
from contextlib import suppress


def build_dashboard_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return child environment with fail-closed dashboard authentication."""
    env = dict(base_env if base_env is not None else os.environ)
    password = (
        env.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "").strip()
        or env.get("ADMIN_PASSWORD", "").strip()
    )
    if not password:
        raise RuntimeError(
            "ADMIN_PASSWORD or HERMES_DASHBOARD_BASIC_AUTH_PASSWORD is required"
        )

    username = (
        env.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "").strip()
        or env.get("ADMIN_USERNAME", "").strip()
        or "admin"
    )
    secret = env.get("HERMES_DASHBOARD_BASIC_AUTH_SECRET", "").strip()
    if not secret:
        seed = env.get("DASHBOARD_SESSION_SECRET", "").strip() or password
        secret = hashlib.sha256(
            ("hermes-dashboard-session:" + seed).encode("utf-8")
        ).hexdigest()

    env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"] = username
    env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"] = password
    env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"] = secret
    env.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))
    return env


def _validated_port(env: dict[str, str]) -> str:
    raw = env.get("PORT", "8080").strip() or "8080"
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"PORT must be between 1 and 65535, got {port}")
    return str(port)


def dashboard_command(env: dict[str, str]) -> list[str]:
    binary = env.get("HERMES_BIN", "hermes")
    return [
        binary,
        "dashboard",
        "--host",
        "0.0.0.0",
        "--port",
        _validated_port(env),
        "--skip-build",
        "--no-open",
    ]


def gateway_command(env: dict[str, str]) -> list[str]:
    return [env.get("HERMES_BIN", "hermes"), "gateway"]


async def terminate_process(proc: asyncio.subprocess.Process | None) -> None:
    """Terminate a child process group, then kill it if it does not stop."""
    if proc is None or proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=10)
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=5)


class HermesSupervisor:
    def __init__(self, env: dict[str, str]) -> None:
        self.env = env
        self.stop_event = asyncio.Event()
        self.gateway_process: asyncio.subprocess.Process | None = None
        self.dashboard_process: asyncio.subprocess.Process | None = None
        self.restart_delay = float(env.get("HERMES_GATEWAY_RESTART_DELAY", "3"))

    def request_stop(self) -> None:
        self.stop_event.set()

    async def gateway_loop(self) -> None:
        """Keep the messaging gateway alive until the dashboard stops."""
        while not self.stop_event.is_set():
            self.gateway_process = await asyncio.create_subprocess_exec(
                *gateway_command(self.env),
                env=self.env,
                start_new_session=True,
            )
            return_code = await self.gateway_process.wait()
            self.gateway_process = None
            if self.stop_event.is_set():
                return
            print(
                f"Hermes gateway exited with code {return_code}; restarting",
                flush=True,
            )
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=max(0.05, self.restart_delay)
                )
            except asyncio.TimeoutError:
                pass

    async def run(self) -> int:
        gateway_task = asyncio.create_task(self.gateway_loop())
        self.dashboard_process = await asyncio.create_subprocess_exec(
            *dashboard_command(self.env),
            env=self.env,
            start_new_session=True,
        )
        dashboard_wait = asyncio.create_task(self.dashboard_process.wait())
        stop_wait = asyncio.create_task(self.stop_event.wait())

        done, pending = await asyncio.wait(
            {dashboard_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        if dashboard_wait in done:
            return_code = dashboard_wait.result()
            if not self.stop_event.is_set():
                print(
                    f"Hermes dashboard exited with code {return_code}",
                    flush=True,
                )
        else:
            return_code = 0

        self.stop_event.set()
        for task in pending:
            task.cancel()
        await terminate_process(self.dashboard_process)
        await terminate_process(self.gateway_process)
        gateway_task.cancel()
        with suppress(asyncio.CancelledError):
            await gateway_task
        return return_code


async def async_main() -> int:
    env = build_dashboard_env()
    supervisor = HermesSupervisor(env)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, supervisor.request_stop)
    return await supervisor.run()


def main() -> int:
    try:
        return asyncio.run(async_main())
    except RuntimeError as exc:
        print(f"Startup refused: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
