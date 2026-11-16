from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cloud_env.apps.github.tokens import GitHubApp
from cloud_env.session import format_session_env, unpack_session_secrets
from cloud_env.update import install_self, self_update
from tests.helpers import generate_signing_key, local_http_server, sign_script


class LifecycleTests(unittest.TestCase):
    def test_self_update_ratchet_and_signature_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            key, pub = generate_signing_key(tmp)

            seed_dir = tmp / "seed"
            seed_dir.mkdir()
            seed_script = seed_dir / "bootstrap.py"
            seed_script.write_text("VERSION = 2\n", encoding="utf-8")
            sign_script(key, seed_script)

            install_dir = tmp / "install"
            install_self(pub, source_script=seed_script, install_dir=install_dir)

            served: dict[str, bytes] = {}

            def respond(path: str, _headers: dict[str, str]) -> tuple[int, bytes, str]:
                data = served.get(path.lstrip("/"), b"")
                return (200 if data else 404), data, "application/octet-stream"

            with local_http_server(respond) as base_url, mock.patch("os.execv") as execv_mock:
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

    def test_unpack_session_secrets_and_format_session_env(self) -> None:
        raw_secrets = {
            "LINEAR_API_KEY": "lin_xyz",
            "QUOTED_VALUE": "hello world",
            "TS_AUTHKEY": "tskey-auth-123",
            "GIT_AUTHOR_NAME": "David Mohl",
            "GIT_AUTHOR_EMAIL": "git@d.sh",
            "GIT_COMMITTER_NAME": "David Mohl",
            "GIT_COMMITTER_EMAIL": "git@d.sh",
            "GITHUB_TOKEN": "ghp_default",
            "GH_TOKEN": "ghp_other",
            "GITHUB_APP_TOKENS": json.dumps(
                {"DVCRN": {"token": "ghs_app", "name": "boticorn[bot]", "email": "1+bot@nr"}}
            ),
            "BOTICORN_TOKENS": json.dumps({"ignored": "ghs_old"}),
        }
        snapshot = dict(raw_secrets)
        secrets, tailscale_authkey, apps = unpack_session_secrets(raw_secrets)

        self.assertEqual(raw_secrets, snapshot)
        self.assertEqual(tailscale_authkey, "tskey-auth-123")
        self.assertEqual(
            apps,
            {"dvcrn": GitHubApp("ghs_app", "boticorn[bot]", "1+bot@nr")},
        )
        self.assertNotIn("TS_AUTHKEY", secrets)
        self.assertNotIn("GITHUB_APP_TOKENS", secrets)
        self.assertNotIn("BOTICORN_TOKENS", secrets)
        self.assertEqual(secrets["GITHUB_TOKEN"], "ghp_default")
        self.assertEqual(secrets["GIT_AUTHOR_NAME"], "David Mohl")

        lines = format_session_env(
            secrets,
            preamble=['export PATH="/tools:$PATH"'],
            github_lines=["unset GH_TOKEN GITHUB_TOKEN"],
        )
        self.assertEqual(
            lines,
            [
                'export PATH="/tools:$PATH"',
                "export LINEAR_API_KEY=lin_xyz",
                "export QUOTED_VALUE='hello world'",
                "unset GH_TOKEN GITHUB_TOKEN",
            ],
        )
