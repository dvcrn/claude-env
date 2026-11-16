from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from cloud_env.lib.fs import atomic_write, replace_marked_block
from cloud_env.secrets.broker import fetch_broker_env
from cloud_env.secrets.loader import SESSION_ENV_EXCLUDED, load_secrets


class LibAndSecretsTests(unittest.TestCase):
    def test_atomic_write_permissions_and_virtiofs_no_chmod(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            secret = tmp / "secret"
            atomic_write(secret, "top-secret\n", mode=0o600)
            self.assertEqual(secret.read_text(encoding="utf-8"), "top-secret\n")
            self.assertEqual(secret.stat().st_mode & 0o777, 0o600)

            shim = tmp / "gh"
            atomic_write(shim, "#!/bin/sh\n", mode=0o755)
            self.assertEqual(shim.stat().st_mode & 0o777, 0o755)

            virtio = tmp / "GEMINI.md"
            with mock.patch.object(Path, "chmod", side_effect=PermissionError("EPERM")):
                atomic_write(virtio, "# rules\n")
            self.assertEqual(virtio.read_text(encoding="utf-8"), "# rules\n")

    def test_replace_marked_block_is_idempotent(self) -> None:
        initial = "export FOO=1"
        updated = replace_marked_block(initial, ["export BAR=2"])
        self.assertEqual(
            updated,
            "export FOO=1\n# >>> cloud-env\nexport BAR=2\n# <<< cloud-env\n",
        )
        second = replace_marked_block(updated, ["export BAR=3", "unset BAZ"])
        self.assertEqual(
            second,
            "export FOO=1\n# >>> cloud-env\nexport BAR=3\nunset BAZ\n# <<< cloud-env\n",
        )

    def test_fetch_broker_env_validates_payload_and_headers(self) -> None:
        captured_headers: dict[str, str] = {}
        payload: dict[str, object] = {"env": {"LINEAR_API_KEY": "lin_123"}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                captured_headers.update({k.lower(): v for k, v in self.headers.items()})
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}"
            env = fetch_broker_env(
                broker_url=url,
                auth_headers={"X-Session-Id": "sess-1", "CF-Access-Client-Id": "cid"},
            )
            self.assertEqual(env, {"LINEAR_API_KEY": "lin_123"})
            self.assertEqual(captured_headers.get("x-session-id"), "sess-1")
            self.assertEqual(captured_headers.get("cf-access-client-id"), "cid")

            for bad_env in (
                {"PATH": "/evil"},
                {"BASH_ENV": "/evil"},
                {"bad-name": "val"},
                {"GOOD_NAME": 123},
            ):
                payload["env"] = bad_env
                self.assertIsNone(fetch_broker_env(broker_url=url, auth_headers={}))
        finally:
            server.shutdown()
            server.server_close()

    def test_load_secrets_merges_broker_over_1password(self) -> None:
        broker_data = {
            "OP_SERVICE_ACCOUNT_TOKEN": "ops_tok",
            "OP_ENVIRONMENT_ID": "env_id",
            "SHARED_KEY": "from_broker",
            "BROKER_ONLY": "b",
        }
        op_data = {"SHARED_KEY": "from_op", "OP_ONLY": "o"}
        with (
            mock.patch("cloud_env.secrets.loader.fetch_broker_env", return_value=broker_data),
            mock.patch("cloud_env.secrets.loader.load_1password", return_value=op_data),
        ):
            merged, complete = load_secrets()
        self.assertTrue(complete)
        self.assertEqual(
            merged,
            {"SHARED_KEY": "from_broker", "BROKER_ONLY": "b", "OP_ONLY": "o"},
        )
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", merged)
        self.assertIn("GITHUB_TOKEN", SESSION_ENV_EXCLUDED)
