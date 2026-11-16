import base64
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import bootstrap, local_http_server


class BrokerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.object(bootstrap, "log"))
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.body: object = {"env": {}}

        def respond(path: str, headers: dict[str, str]) -> tuple[int, bytes]:
            self.requests.append((path, headers))
            return 200, json.dumps(self.body).encode()

        url = self.enterContext(local_http_server(respond))
        self.enterContext(
            mock.patch.multiple(
                bootstrap,
                BROKER_URL=url,
                BROKER_TOKEN_FILE=self.tmp / "broker-token",
                INGRESS_TOKEN_FILE=self.tmp / "ingress-token",
            )
        )
        session = {
            "CLAUDE_CODE_REMOTE_SESSION_ID": "cse_abc",
            "NO_PROXY": "127.0.0.1",
            "no_proxy": "127.0.0.1",
        }
        self.enterContext(mock.patch.dict(os.environ, session))

    def test_sends_the_session_and_returns_the_env(self) -> None:
        self.body = {"env": {"LINEAR_API_KEY": "lin_1"}}
        header = base64.urlsafe_b64encode(b'{"kid":"k1"}').decode().rstrip("=")
        bootstrap.INGRESS_TOKEN_FILE.write_text(f"sk-ant-si-{header}.payload.sig\n")
        access = {"CF-Access-Client-Id": "id", "CF-Access-Client-Secret": "secret"}
        bootstrap.BROKER_TOKEN_FILE.write_text(json.dumps(access))

        self.assertEqual(bootstrap.fetch_broker_env(), {"LINEAR_API_KEY": "lin_1"})

        path, headers = self.requests[0]
        self.assertEqual(path, "/v1/session-env")
        self.assertEqual(headers["x-session-id"], "cse_abc")
        self.assertEqual(headers["cf-access-client-id"], "id")
        self.assertEqual(headers["cf-access-client-secret"], "secret")
        digest = hashlib.sha256(f"{header}.payload".encode()).hexdigest()
        self.assertEqual(headers["x-ingress-sig-sample"], f"k1.{digest}.sig")

    def test_one_invalid_entry_rejects_the_response(self) -> None:
        bodies = [
            {"env": {"OK": "1", "PATH": "/evil"}},
            {"env": {"OK": "1", "BASH_ENV": "/evil"}},
            {"env": {"bad-name": "1"}},
            {"env": {"OK": 1}},
            {"env": ["OK"]},
            {},
        ]
        for body in bodies:
            self.body = body
            with self.subTest(body=body):
                self.assertIsNone(bootstrap.fetch_broker_env())

    def test_unreachable_broker_returns_none(self) -> None:
        with mock.patch.object(bootstrap, "BROKER_URL", "http://127.0.0.1:1"):
            self.assertIsNone(bootstrap.fetch_broker_env(timeout=2))


class OnePasswordTest(unittest.TestCase):
    def test_returns_only_the_variables_op_added(self) -> None:
        entries = [
            f"HOME={bootstrap.HOME}",
            "OP_SERVICE_ACCOUNT_TOKEN=ops_1",
            "PWD=/",
            "API_KEY=abc",
            "PRIVATE_KEY=line 1\nline 2",
            "LD_PRELOAD=/evil.so",
            "lower=1",
        ]
        which = subprocess.CompletedProcess([], 0, stdout="/tools/op\n")
        op = subprocess.CompletedProcess([], 0, stdout="\0".join(entries) + "\0", stderr="")
        with (
            mock.patch.object(bootstrap, "log"),
            mock.patch.object(subprocess, "run", side_effect=[which, op]) as run,
        ):
            variables = bootstrap.load_1password("ops_1", "env_1")
        self.assertEqual(variables, {"API_KEY": "abc", "PRIVATE_KEY": "line 1\nline 2"})
        command = run.call_args_list[1].args[0]
        self.assertEqual(
            command[:5], ["/tools/op", "run", "--no-masking", "--environment", "env_1"]
        )
        self.assertEqual(run.call_args_list[1].kwargs["env"]["OP_SERVICE_ACCOUNT_TOKEN"], "ops_1")

    def test_failures_return_none(self) -> None:
        which = subprocess.CompletedProcess([], 0, stdout="/tools/op\n")
        failures = [
            [subprocess.CompletedProcess([], 1, stdout="")],
            [which, subprocess.CompletedProcess([], 1, stdout="", stderr="denied")],
            [which, subprocess.TimeoutExpired("op", 30)],
        ]
        for results in failures:
            with (
                self.subTest(results=results),
                mock.patch.object(bootstrap, "log"),
                mock.patch.object(subprocess, "run", side_effect=results),
            ):
                self.assertIsNone(bootstrap.load_1password("ops_1", "env_1"))


class LoadSecretsTest(unittest.TestCase):
    def load(self, broker: dict[str, str] | None, onepassword: dict[str, str] | None):
        with (
            mock.patch.object(bootstrap, "log"),
            mock.patch.object(bootstrap, "fetch_broker_env", return_value=broker),
            mock.patch.object(bootstrap, "load_1password", return_value=onepassword),
        ):
            return bootstrap.load_secrets()

    def test_broker_values_win_and_non_environment_secrets_are_split_off(self) -> None:
        app = {"token": "ghs_a", "name": "bot[bot]", "email": "bot@example.com"}
        broker = {
            "OP_SERVICE_ACCOUNT_TOKEN": "ops_1",
            "OP_ENVIRONMENT_ID": "env_1",
            "TS_AUTHKEY": "tskey-1",
            "GITHUB_APP_TOKENS": json.dumps({"acme": app}),
            "SHARED": "broker",
        }
        secrets = self.load(broker, {"SHARED": "1password", "API_KEY": "abc"})
        self.assertEqual(secrets.env, {"SHARED": "broker", "API_KEY": "abc"})
        self.assertEqual(secrets.apps, {"acme": bootstrap.GitHubApp(**app)})
        self.assertEqual(secrets.tailscale_authkey, "tskey-1")
        self.assertTrue(secrets.complete)

    def test_incomplete_without_the_broker_or_1password(self) -> None:
        credentials = {"OP_SERVICE_ACCOUNT_TOKEN": "ops_1", "OP_ENVIRONMENT_ID": "env_1"}
        cases = [
            (None, {"API_KEY": "abc"}, {}),
            ({"A": "1"}, {"API_KEY": "abc"}, {"A": "1"}),
            ({"A": "1", **credentials}, None, {"A": "1"}),
        ]
        for broker, onepassword, env in cases:
            with self.subTest(broker=broker):
                self.assertEqual(
                    self.load(broker, onepassword), bootstrap.Secrets(env, {}, None, False)
                )

    def test_session_env_lines_quote_values_and_skip_excluded_names(self) -> None:
        env = {"API_KEY": "a b", "GITHUB_TOKEN": "ghp_1", "GIT_AUTHOR_NAME": "Ada", "N": "1"}
        self.assertEqual(bootstrap.session_env_lines(env), ["export API_KEY='a b'", "export N=1"])


if __name__ == "__main__":
    unittest.main()
