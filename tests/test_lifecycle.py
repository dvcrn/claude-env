from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from cloud_env.agy.harness import AgyHarness
from cloud_env.session import session_start
from cloud_env.update import install_self, self_update


def sign_script(key: Path, script: Path) -> bytes:
    subprocess.run(
        ["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", "claude-env", str(script)],
        check=True,
    )
    return script.with_name("bootstrap.py.sig").read_bytes()


class LifecycleTests(unittest.TestCase):
    def test_self_update_ratchet_and_signature_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            key = tmp / "id_ed25519"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
            )
            pub = key.with_suffix(".pub").read_text(encoding="utf-8").strip()

            seed_dir = tmp / "seed"
            seed_dir.mkdir()
            seed_script = seed_dir / "bootstrap.py"
            seed_script.write_text("VERSION = 2\n", encoding="utf-8")
            sign_script(key, seed_script)

            install_dir = tmp / "install"
            install_self(pub, source_script=seed_script, install_dir=install_dir)

            served: dict[str, bytes] = {}

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    data = served.get(self.path.lstrip("/"), b"")
                    self.send_response(200 if data else 404)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, format: str, *args: object) -> None:
                    return

            server = HTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with mock.patch("os.execv") as execv_mock:
                    served["bootstrap.py"] = b"VERSION = 3\n# tampered\n"
                    served["bootstrap.py.sig"] = b"not-a-valid-sig"
                    self_update(install_dir, base_url=base_url, current_version=2)
                    execv_mock.assert_not_called()
                    self.assertEqual(
                        (install_dir / "bootstrap.py").read_text(encoding="utf-8"),
                        "VERSION = 2\n",
                    )

                    downgrade_dir = tmp / "v1"
                    downgrade_dir.mkdir()
                    v1_script = downgrade_dir / "bootstrap.py"
                    v1_script.write_text("VERSION = 1\n", encoding="utf-8")
                    served["bootstrap.py"] = v1_script.read_bytes()
                    served["bootstrap.py.sig"] = sign_script(key, v1_script)
                    self_update(install_dir, base_url=base_url, current_version=2)
                    execv_mock.assert_not_called()

                    upgrade_dir = tmp / "v3"
                    upgrade_dir.mkdir()
                    v3_script = upgrade_dir / "bootstrap.py"
                    v3_script.write_text("VERSION = 3\n", encoding="utf-8")
                    served["bootstrap.py"] = v3_script.read_bytes()
                    served["bootstrap.py.sig"] = sign_script(key, v3_script)
                    self_update(install_dir, base_url=base_url, current_version=2)
                    execv_mock.assert_called_once()
                    self.assertEqual(
                        (install_dir / "bootstrap.py").read_text(encoding="utf-8"),
                        "VERSION = 3\n",
                    )
            finally:
                server.shutdown()
                server.server_close()

    def test_session_start_fresh_and_incomplete_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            home = tmp / "home"
            shm = tmp / "shm"
            home.mkdir()
            shm.mkdir()
            harness = AgyHarness(home=home, shm_dir=shm)
            init_marker = shm / ".cloud-env-initialized"

            secrets = {
                "LINEAR_API_KEY": "lin_xyz",
                "TS_AUTHKEY": "tskey-auth-123",
                "GIT_AUTHOR_NAME": "David Mohl",
                "GIT_AUTHOR_EMAIL": "git@d.sh",
                "GITHUB_TOKEN": "ghp_default",
                "GITHUB_APP_TOKENS": json.dumps(
                    {"dvcrn": {"token": "ghs_app", "name": "boticorn[bot]", "email": "1+bot@nr"}}
                ),
            }
            clean_env = {"PATH": "/usr/bin:/bin", "BASH_ENV": str(home / ".bashrc")}
            with (
                mock.patch.dict(os.environ, clean_env, clear=True),
                mock.patch("cloud_env.session.trust_repo"),
                mock.patch("cloud_env.session.install_repo_tools"),
                mock.patch("cloud_env.session.set_git_identity"),
                mock.patch(
                    "cloud_env.session.use_github_directly",
                    return_value=["unset GH_TOKEN GITHUB_TOKEN"],
                ),
                mock.patch("cloud_env.session.join_tailscale") as ts_mock,
                mock.patch("cloud_env.session.load_secrets", return_value=(dict(secrets), True)),
            ):
                session_start(update=False, harness=harness, init_marker=init_marker)
                self.assertNotIn("BASH_ENV", os.environ)
                ts_mock.assert_called_once_with("tskey-auth-123", supported=False)

            self.assertTrue(init_marker.exists())
            env_text = harness.env_file.read_text(encoding="utf-8")
            self.assertIn("export LINEAR_API_KEY=lin_xyz", env_text)
            self.assertNotIn("TS_AUTHKEY", env_text)
            self.assertNotIn("GIT_AUTHOR_NAME", env_text)
            self.assertNotIn("GITHUB_TOKEN=", env_text)
            self.assertIn("unset GH_TOKEN GITHUB_TOKEN", env_text)
            self.assertIn(
                "Co-authored-by: David Mohl <git@d.sh>",
                harness.gemini_md.read_text(encoding="utf-8"),
            )

            with (
                mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True),
                mock.patch("cloud_env.session.trust_repo"),
                mock.patch("cloud_env.session.install_repo_tools"),
                mock.patch("cloud_env.session.set_git_identity"),
                mock.patch("cloud_env.session.use_github_directly", return_value=[]),
                mock.patch("cloud_env.session.join_tailscale"),
                mock.patch("cloud_env.session.tailscale_needs_rejoin", return_value=True),
                mock.patch(
                    "cloud_env.session.load_secrets",
                    return_value=({"PARTIAL_VAR": "1"}, False),
                ),
            ):
                session_start(update=False, harness=harness, init_marker=init_marker)

            self.assertEqual(harness.env_file.read_text(encoding="utf-8"), env_text)
