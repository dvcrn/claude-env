import json
import os
import unittest
from unittest import mock

from helpers import FOOTER, bootstrap


class GitHubAppsTest(unittest.TestCase):
    def test_parses_owners_and_skips_invalid_entries(self) -> None:
        app = {"token": "ghs_a", "name": "bot[bot]", "email": "bot@example.com"}
        cases = [
            (
                {"GITHUB_APP_TOKENS": json.dumps({"Acme": app, "bad/owner": app, "x": {}})},
                {"acme": bootstrap.GitHubApp("ghs_a", "bot[bot]", "bot@example.com")},
            ),
            (
                {"BOTICORN_TOKENS": json.dumps({"acme": "ghs_b", "evil": "a\nb"})},
                {
                    "acme": bootstrap.GitHubApp(
                        "ghs_b", bootstrap.BOTICORN_NAME, bootstrap.BOTICORN_EMAIL
                    )
                },
            ),
            (
                {
                    "GITHUB_APP_TOKENS": json.dumps({"acme": app}),
                    "BOTICORN_TOKENS": json.dumps({"acme": "ghs_b"}),
                },
                {"acme": bootstrap.GitHubApp("ghs_a", "bot[bot]", "bot@example.com")},
            ),
            ({"GITHUB_APP_TOKENS": "not json"}, {}),
            ({}, {}),
        ]
        for env, want in cases:
            with self.subTest(env=env), mock.patch.object(bootstrap, "log"):
                self.assertEqual(bootstrap.github_apps(env), want)


class GhTargetOwnerTest(unittest.TestCase):
    def test_picks_owner_gh_acts_on(self) -> None:
        cases = [
            (["pr", "view", "--repo", "Acme/app"], "acme"),
            (["pr", "view", "--repo=git@github.com:acme/app.git"], "acme"),
            (["-R", "https://github.com/acme/app"], "acme"),
            (["api", "repos/Acme/app/pulls"], "acme"),
            (["api", "/repos/acme/app/issues"], "acme"),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(bootstrap.gh_target_owner(args), want)

    def test_falls_back_to_gh_repo_then_origin(self) -> None:
        with mock.patch.dict(os.environ, {"GH_REPO": "other/app"}):
            self.assertEqual(bootstrap.gh_target_owner(["pr", "list"]), "other")
        with (
            mock.patch.dict(os.environ, clear=True),
            mock.patch.object(bootstrap, "origin_owner", return_value="origin"),
        ):
            self.assertEqual(bootstrap.gh_target_owner(["pr", "list"]), "origin")


class FooterTest(unittest.TestCase):
    def test_strip_footer_removes_it_anywhere_in_tool_input(self) -> None:
        tool_input = {"body": "Done." + FOOTER, "items": ["a" + FOOTER + "b"], "n": 1}
        self.assertEqual(
            bootstrap.strip_footer(tool_input), {"body": "Done.", "items": ["a\nb"], "n": 1}
        )

    def test_has_footer_ignores_old_string(self) -> None:
        self.assertTrue(bootstrap.has_footer({"new_string": "x" + FOOTER}))
        self.assertFalse(bootstrap.has_footer({"old_string": "x" + FOOTER, "new_string": "x"}))


class ReplaceMarkedBlockTest(unittest.TestCase):
    def test_replaces_previous_block_and_keeps_other_lines(self) -> None:
        once = bootstrap.replace_marked_block("alias ll='ls -l'", ["export A=1"])
        twice = bootstrap.replace_marked_block(once, ["export A=2"])
        self.assertEqual(twice, "alias ll='ls -l'\n# >>> cloud-env\nexport A=2\n# <<< cloud-env\n")


class ScriptVersionTest(unittest.TestCase):
    def test_reads_version_of_a_downloaded_copy(self) -> None:
        self.assertEqual(bootstrap.script_version(b"x = 1\nVERSION = 7\n"), 7)
        self.assertIsNone(bootstrap.script_version(b"VERSION = 'x'\n"))


if __name__ == "__main__":
    unittest.main()
