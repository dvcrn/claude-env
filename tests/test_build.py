from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cloud_env.config import VERSION
from cloud_env.update import install_self, script_version, verify_signature
from tests.helpers import generate_signing_key, sign_script

ROOT = Path(__file__).resolve().parent.parent


def load_module_from_path(name: str, path: Path):
    prev = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = prev


class BuildTests(unittest.TestCase):
    def test_compiled_bootstrap_matches_src(self) -> None:
        res = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts" / "build.py"), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_compiled_version_and_inlined_assets(self) -> None:
        raw = (ROOT / "bootstrap.py").read_bytes()
        self.assertEqual(script_version(raw), VERSION)

        mod = load_module_from_path("bootstrap_compiled", ROOT / "bootstrap.py")
        self.assertEqual(
            mod.GLOBAL_TOOLS,
            (ROOT / "src/cloud_env/apps/mise/global_tools.toml").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            mod.CLAUDE_MD,
            (ROOT / "src/cloud_env/claude/CLAUDE.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(
            mod.GEMINI_BASE_MD,
            (ROOT / "src/cloud_env/agy/GEMINI.md").read_text(encoding="utf-8"),
        )
        self.assertFalse(hasattr(mod, "load_asset"))

    def test_multiline_constant_escaping_edge_cases(self) -> None:
        build_mod = load_module_from_path("build_script", ROOT / "scripts" / "build.py")
        cases = [
            'ends with triple quotes """',
            'ends with single quote "',
            'ends with backslash and quote \\"',
            "crlf\r\nlines\r\n",
            'middle """ and \\n escapes',
        ]
        for content in cases:
            stmt_text = build_mod.format_multiline_constant("TEST_CONST", content, Path("dummy.py"))
            tree = ast.parse(stmt_text)
            rhs = tree.body[0].value  # type: ignore[attr-defined]
            self.assertEqual(ast.literal_eval(rhs), content)

    def test_check_detects_drift_collisions_cycles_and_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            base = tmp / "repo"

            def reset_repo() -> None:
                shutil.rmtree(base, ignore_errors=True)
                base.mkdir()
                shutil.copytree(ROOT / "src", base / "src")
                shutil.copytree(ROOT / "scripts", base / "scripts")
                shutil.copy(ROOT / "ruff.toml", base / "ruff.toml")
                shutil.copy(ROOT / "bootstrap.py", base / "bootstrap.py")

            def run_build(*extra: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [
                        sys.executable,
                        "-B",
                        str(base / "scripts" / "build.py"),
                        "--root",
                        str(base),
                        *extra,
                    ],
                    capture_output=True,
                    text=True,
                )

            reset_repo()
            (base / "bootstrap.py").write_text("# drifted\n", encoding="utf-8")
            drift = run_build("--check")
            self.assertNotEqual(drift.returncode, 0)
            self.assertIn("out of date", drift.stderr)

            reset_repo()
            parse_py = base / "src/cloud_env/lib/parse.py"
            parse_py.write_text(
                "from __future__ import annotations\n\nimport urllib.error\n\n"
                "def clean_text(value: object) -> bool:\n"
                "    return isinstance(value, str) and bool(value) and not isinstance(value, urllib.error.URLError)\n",
                encoding="utf-8",
            )
            multi_submod = run_build()
            self.assertEqual(multi_submod.returncode, 0, multi_submod.stderr)

            reset_repo()
            with (base / "src/cloud_env/lib/parse.py").open("a", encoding="utf-8") as f:
                f.write("\nimport urllib.error as req\n_ = req\n")
            with (base / "src/cloud_env/lib/http.py").open("a", encoding="utf-8") as f:
                f.write("\nimport urllib.request as req\n_ = req\n")
            alias_conflict = run_build()
            self.assertNotEqual(alias_conflict.returncode, 0)
            self.assertIn("conflicting stdlib import binding 'req'", alias_conflict.stderr)

            reset_repo()
            with (base / "src/cloud_env/lib/parse.py").open("a", encoding="utf-8") as f:
                f.write("\nfrom .fs import atomic_write\n")
            rel_import = run_build()
            self.assertNotEqual(rel_import.returncode, 0)
            self.assertIn("relative imports are not supported", rel_import.stderr)

            reset_repo()
            with (base / "src/cloud_env/lib/parse.py").open("a", encoding="utf-8") as f:
                f.write("\nVERSION = 99\n")
            dup = run_build()
            self.assertNotEqual(dup.returncode, 0)
            self.assertIn("duplicate top-level symbol 'VERSION'", dup.stderr)

            reset_repo()
            with (base / "src/cloud_env/lib/parse.py").open("a", encoding="utf-8") as f:
                f.write("\nPath = 123\n")
            shadow = run_build()
            self.assertNotEqual(shadow.returncode, 0)
            self.assertIn("shadows stdlib import", shadow.stderr)

            reset_repo()
            with (base / "src/cloud_env/lib/parse.py").open("a", encoding="utf-8") as f:
                f.write("\nimport third_party_pkg\n")
            third = run_build()
            self.assertNotEqual(third.returncode, 0)
            self.assertIn("non-stdlib import", third.stderr)

            reset_repo()
            with (base / "src/cloud_env/lib/parse.py").open("a", encoding="utf-8") as f:
                f.write("\ndef bad() -> None:\n    from cloud_env.config import VERSION\n")
            nested = run_build()
            self.assertNotEqual(nested.returncode, 0)
            self.assertIn("nested import", nested.stderr)

            reset_repo()
            (base / "src/cloud_env/orphan.py").write_text("ORPHAN = 1\n", encoding="utf-8")
            orphan = run_build()
            self.assertNotEqual(orphan.returncode, 0)
            self.assertIn("unreferenced non-empty module", orphan.stderr)

            reset_repo()
            config_py = base / "src/cloud_env/config.py"
            config_py.write_text(
                config_py.read_text(encoding="utf-8") + "\nfrom cloud_env.cli import main\n",
                encoding="utf-8",
            )
            cycle = run_build()
            self.assertNotEqual(cycle.returncode, 0)
            self.assertIn("import cycle", cycle.stderr)

    def test_compiled_cli_smoke_and_signature_roundtrip(self) -> None:
        bad = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "bootstrap.py"), "unknown"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("usage: bootstrap.py", bad.stderr)

        event = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "gh pr create --body 'hi\n\nGenerated with [Claude Code](https://claude.ai/code)'"
            },
        }
        pre = subprocess.run(
            [sys.executable, "-I", "-B", str(ROOT / "bootstrap.py"), "pre-tool-use"],
            input=json.dumps(event),
            capture_output=True,
            text=True,
        )
        self.assertEqual(pre.returncode, 0)
        parsed = json.loads(pre.stdout)
        self.assertEqual(parsed["hookSpecificOutput"]["permissionDecision"], "deny")

        with tempfile.TemporaryDirectory() as tmp_name:
            tmp = Path(tmp_name)
            key, pub = generate_signing_key(tmp)
            script = tmp / "bootstrap.py"
            shutil.copy(ROOT / "bootstrap.py", script)
            sign_script(key, script)
            install_dir = tmp / "installed"
            install_self(pub, source_script=script, install_dir=install_dir)
            self.assertTrue(
                verify_signature(
                    install_dir / "bootstrap.py",
                    install_dir / "bootstrap.py.sig",
                    install_dir / "allowed_signers",
                )
            )
            (install_dir / "bootstrap.py").write_text("# tampered\n", encoding="utf-8")
            self.assertFalse(
                verify_signature(
                    install_dir / "bootstrap.py",
                    install_dir / "bootstrap.py.sig",
                    install_dir / "allowed_signers",
                )
            )
