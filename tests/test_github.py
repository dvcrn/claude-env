import contextlib
import io
import json
import os
import subprocess
import time
import unittest
from unittest import mock

from helpers import TempDirTest, bootstrap

APP = bootstrap.GitHubApp("ghs_acme", "acme-bot[bot]", "1+acme-bot[bot]@users.noreply.github.com")


def git_global(*args: str) -> str:
    result = subprocess.run(
        ["git", "config", "--global", *args], capture_output=True, text=True, check=True
    )
    return result.stdout


class TokenStoreTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.token_dir = self.patch_token_dir()
        bootstrap.store_github_tokens({"acme": APP}, "ghp_default")

    def expire_stamp(self) -> None:
        expired = time.time() - bootstrap.TOKEN_REFRESH_AFTER - 10
        os.utime(bootstrap.TOKEN_STAMP, (expired, expired))

    def test_token_of_the_owner_or_the_default(self) -> None:
        self.assertEqual(bootstrap.github_token("acme"), "ghs_acme")
        self.assertEqual(bootstrap.github_token("other"), "ghp_default")
        self.assertEqual(bootstrap.github_token("../acme"), "ghp_default")
        self.assertEqual((self.token_dir / "acme").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.token_dir.stat().st_mode & 0o777, 0o700)

    def test_credential_helper_answers_get_with_the_owner_token(self) -> None:
        request = "protocol=https\nhost=github.com\npath=Acme/app.git\n\n"
        for args, want in (
            (["get"], "username=x-access-token\npassword=ghs_acme\n"),
            (["store"], ""),
        ):
            out = io.StringIO()
            with (
                self.subTest(args=args),
                mock.patch("sys.stdin", io.StringIO(request)),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(bootstrap.cmd_git_credential(args), 0)
                self.assertEqual(out.getvalue(), want)

    def test_fresh_tokens_are_not_refreshed(self) -> None:
        with mock.patch.object(bootstrap, "fetch_broker_env") as fetch:
            bootstrap.github_token("acme")
        fetch.assert_not_called()

    def test_failed_refresh_keeps_the_tokens_and_retries_sooner(self) -> None:
        self.expire_stamp()
        with mock.patch.object(bootstrap, "fetch_broker_env", return_value=None):
            self.assertEqual(bootstrap.github_token("acme"), "ghs_acme")
        until_retry = bootstrap.TOKEN_REFRESH_AFTER - (
            time.time() - bootstrap.TOKEN_STAMP.stat().st_mtime
        )
        self.assertAlmostEqual(until_retry, bootstrap.TOKEN_RETRY_AFTER, delta=30)

    def test_due_refresh_stores_the_new_tokens(self) -> None:
        self.expire_stamp()
        minted = {"acme": {"token": "ghs_new", "name": APP.name, "email": APP.email}}
        env = {"GITHUB_APP_TOKENS": json.dumps(minted), "GITHUB_TOKEN": "ghp_new"}
        with mock.patch.object(bootstrap, "fetch_broker_env", return_value=env):
            self.assertEqual(bootstrap.github_token("acme"), "ghs_new")
        self.assertEqual(bootstrap.github_token("other"), "ghp_new")
        self.assertLess(time.time() - bootstrap.TOKEN_STAMP.stat().st_mtime, 30)


class GitConfigTest(TempDirTest):
    def setUp(self) -> None:
        super().setUp()
        self.identity_dir = self.tmp / "github-apps"
        self.legacy_file = self.tmp / "boticorn"
        self.patch(GIT_IDENTITY_DIR=self.identity_dir, LEGACY_IDENTITY_FILE=self.legacy_file)
        self.patch_env(GIT_CONFIG_GLOBAL=str(self.tmp / "gitconfig"))

    def includes(self) -> str:
        found = subprocess.run(
            ["git", "config", "--global", "--get-regexp", r"^includeif\."],
            capture_output=True,
            text=True,
        )
        return found.stdout

    def test_sets_the_user_identity_and_one_include_per_app_owner(self) -> None:
        git_global(
            "includeIf.hasconfig:remote.*.url:**/gone/**.path", str(self.identity_dir / "gone")
        )
        git_global("includeIf.hasconfig:remote.*.url:**/old/**.path", str(self.legacy_file))
        git_global("includeIf.gitdir:~/work/.path", "~/work.gitconfig")
        self.patch_env(GIT_AUTHOR_NAME="Ada", GIT_AUTHOR_EMAIL="ada@example.com")

        bootstrap.set_git_identity({"acme": APP})

        self.assertEqual(git_global("user.name"), "Ada\n")
        self.assertEqual(git_global("user.email"), "ada@example.com\n")
        self.assertEqual(git_global("commit.gpgsign"), "false\n")
        path = self.identity_dir / "acme"
        self.assertEqual(
            self.includes().splitlines(),
            [
                "includeif.gitdir:~/work/.path ~/work.gitconfig",
                f"includeif.hasconfig:remote.*.url:**/acme/**.path {path}",
                f"includeif.hasconfig:remote.*.url:git@github.com:acme/**.path {path}",
            ],
        )
        name = subprocess.run(
            ["git", "config", "--file", str(path), "user.name"], capture_output=True, text=True
        )
        self.assertEqual(name.stdout, APP.name + "\n")

    def test_without_apps_keeps_the_includes_of_earlier_starts(self) -> None:
        git_global(
            "includeIf.hasconfig:remote.*.url:**/acme/**.path", str(self.identity_dir / "acme")
        )
        with mock.patch.dict(os.environ):
            os.environ.pop("GIT_AUTHOR_NAME", None)
            bootstrap.set_git_identity({})
        self.assertIn("**/acme/**", self.includes())
        self.assertEqual(git_global("--default", "", "--get", "user.name"), "\n")

    def test_use_github_directly_installs_the_helper_shim_and_env_lines(self) -> None:
        token_dir = self.patch_token_dir()
        shim = self.tmp / "bin" / "gh"
        self.patch(GH_SHIM=shim)
        self.patch_env(NO_PROXY="localhost")
        unset = "unset GH_TOKEN GITHUB_TOKEN"

        with mock.patch.object(bootstrap, "ANTIGRAVITY", True):
            self.assertEqual(bootstrap.use_github_directly({"acme": APP}, None), [unset])
        self.assertEqual(os.environ["NO_PROXY"], "localhost")

        with mock.patch.object(bootstrap, "ANTIGRAVITY", False):
            lines = bootstrap.use_github_directly({"acme": APP}, "ghp_default")
        hosts = bootstrap.GITHUB_HOSTS
        self.assertEqual(
            lines,
            [
                f'export NO_PROXY="${{NO_PROXY:+$NO_PROXY,}}{hosts}"',
                f'export no_proxy="${{no_proxy:+$no_proxy,}}{hosts}"',
                unset,
            ],
        )
        self.assertEqual(os.environ["NO_PROXY"], f"localhost,{hosts}")
        self.assertEqual((token_dir / "acme").read_text(), "ghs_acme")
        self.assertEqual((token_dir / "_default").read_text(), "ghp_default")
        self.assertEqual(shim.stat().st_mode & 0o777, 0o755)
        self.assertEqual(shim.read_text(), f'#!/bin/sh\nexec {bootstrap.hook_command("gh")} "$@"\n')
        helpers = git_global("--get-all", "credential.https://github.com.helper").splitlines()
        self.assertEqual(helpers, ["", "!" + bootstrap.hook_command("git-credential")])
        self.assertEqual(git_global("credential.https://github.com.useHttpPath"), "true\n")

    def test_without_any_token_github_stays_on_the_agent_proxy(self) -> None:
        self.patch_token_dir()
        self.assertEqual(bootstrap.use_github_directly({}, None), [])
        self.assertFalse(bootstrap.TOKEN_DIR.exists())


if __name__ == "__main__":
    unittest.main()
