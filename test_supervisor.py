from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import supervisor


class DashboardEnvironmentTests(unittest.TestCase):
    def test_requires_password(self):
        with self.assertRaisesRegex(RuntimeError, "ADMIN_PASSWORD"):
            supervisor.build_dashboard_env({"HOME": "/tmp"})

    def test_maps_existing_admin_credentials(self):
        env = supervisor.build_dashboard_env(
            {
                "HOME": "/tmp",
                "ADMIN_USERNAME": "wayne",
                "ADMIN_PASSWORD": "correct-horse-battery-staple",
            }
        )
        self.assertEqual(env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"], "wayne")
        self.assertEqual(
            env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"],
            "correct-horse-battery-staple",
        )
        self.assertEqual(len(env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"]), 64)

    def test_session_secret_is_stable_but_not_plaintext(self):
        source = {
            "HOME": "/tmp",
            "ADMIN_PASSWORD": "correct-horse-battery-staple",
        }
        first = supervisor.build_dashboard_env(source)
        second = supervisor.build_dashboard_env(source)
        self.assertEqual(
            first["HERMES_DASHBOARD_BASIC_AUTH_SECRET"],
            second["HERMES_DASHBOARD_BASIC_AUTH_SECRET"],
        )
        self.assertNotEqual(
            first["HERMES_DASHBOARD_BASIC_AUTH_SECRET"],
            source["ADMIN_PASSWORD"],
        )

    def test_explicit_dashboard_credentials_win(self):
        env = supervisor.build_dashboard_env(
            {
                "HOME": "/tmp",
                "ADMIN_USERNAME": "legacy",
                "ADMIN_PASSWORD": "legacy-password",
                "HERMES_DASHBOARD_BASIC_AUTH_USERNAME": "dashboard-user",
                "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD": "dashboard-password",
                "HERMES_DASHBOARD_BASIC_AUTH_SECRET": "s" * 32,
            }
        )
        self.assertEqual(
            env["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"], "dashboard-user"
        )
        self.assertEqual(
            env["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"], "dashboard-password"
        )
        self.assertEqual(env["HERMES_DASHBOARD_BASIC_AUTH_SECRET"], "s" * 32)

    def test_dashboard_command_is_public_bind_with_auth_env(self):
        env = supervisor.build_dashboard_env(
            {"HOME": "/tmp", "ADMIN_PASSWORD": "password", "PORT": "9123"}
        )
        self.assertEqual(
            supervisor.dashboard_command(env),
            [
                "hermes",
                "dashboard",
                "--host",
                "0.0.0.0",
                "--port",
                "9123",
                "--skip-build",
                "--no-open",
            ],
        )

    def test_dashboard_command_rejects_invalid_port(self):
        env = supervisor.build_dashboard_env(
            {"HOME": "/tmp", "ADMIN_PASSWORD": "password", "PORT": "70000"}
        )
        with self.assertRaisesRegex(RuntimeError, "between 1 and 65535"):
            supervisor.dashboard_command(env)


class SupervisorIntegrationTests(unittest.TestCase):
    def test_gateway_restarts_and_shutdown_terminates_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake = root / "fake_hermes.py"
            count_path = root / "gateway-count"
            ready_path = root / "dashboard-ready"
            stopped_path = root / "dashboard-stopped"
            fake.write_text(
                """#!/usr/bin/env python3
import os
import signal
import sys
import time
from pathlib import Path

root = Path(os.environ["TEST_STATE_DIR"])
mode = sys.argv[1]
stop = False

def handle_stop(_signum, _frame):
    global stop
    stop = True

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)

if mode == "gateway":
    path = root / "gateway-count"
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    if count == 1:
        raise SystemExit(7)
    while not stop:
        time.sleep(0.02)
    raise SystemExit(0)

if mode == "dashboard":
    required = [
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME",
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD",
        "HERMES_DASHBOARD_BASIC_AUTH_SECRET",
    ]
    assert all(os.environ.get(name) for name in required)
    assert "--host" in sys.argv and "0.0.0.0" in sys.argv
    (root / "dashboard-ready").write_text("ready")
    while not stop:
        time.sleep(0.02)
    (root / "dashboard-stopped").write_text("stopped")
    raise SystemExit(0)

raise SystemExit(9)
""",
                encoding="utf-8",
            )
            fake.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "ADMIN_USERNAME": "wayne",
                    "ADMIN_PASSWORD": "integration-password",
                    "HERMES_BIN": str(fake),
                    "HERMES_GATEWAY_RESTART_DELAY": "0.05",
                    "PORT": "9123",
                    "TEST_STATE_DIR": str(root),
                }
            )
            proc = subprocess.Popen(
                [sys.executable, str(Path(supervisor.__file__))],
                cwd=Path(supervisor.__file__).parent,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            try:
                deadline = time.time() + 5
                while time.time() < deadline:
                    count = int(count_path.read_text()) if count_path.exists() else 0
                    if ready_path.exists() and count >= 2:
                        break
                    if proc.poll() is not None:
                        early_output = proc.stdout.read() if proc.stdout else ""
                        self.fail(f"supervisor exited early: {early_output}")
                    time.sleep(0.05)
                else:
                    self.fail("supervisor did not start dashboard and restart gateway")

                proc.send_signal(signal.SIGTERM)
                output, _ = proc.communicate(timeout=5)
                self.assertEqual(proc.returncode, 0, output)
                self.assertTrue(stopped_path.exists(), output)
                self.assertGreaterEqual(int(count_path.read_text()), 2)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
