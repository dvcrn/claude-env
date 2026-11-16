#!/usr/bin/env python3
"""Compiles src/cloud_env into the single-file bootstrap.py artifact."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "cloud_env"
HEADER_NOTE = "# Generated from src/cloud_env by scripts/build.py: do not edit directly."


@dataclass
class ModuleUnit:
    name: str
    path: Path
    source: str
    tree: ast.Module
    deps: list[str]
    imports_used: list[tuple[str, str]]
    exports: set[str]
    stdlib_imports: list[str]
    stdlib_bindings: dict[str, tuple[str, str]]
    body_text: str


def module_path(src_dir: Path, mod_name: str) -> Path:
    rel = Path(*mod_name.split("."))
    file_path = (src_dir / rel).with_suffix(".py")
    if file_path.is_file():
        return file_path
    init_path = src_dir / rel / "__init__.py"
    if init_path.is_file():
        return init_path
    raise SystemExit(f"build: cannot resolve module {mod_name!r} under {src_dir}")


def resolve_import_module(current_mod: str, current_path: Path, node: ast.ImportFrom) -> str:
    if node.level == 0:
        if node.module is None:
            raise SystemExit(f"build: invalid import in {current_path}:{node.lineno}")
        return node.module
    parts = current_mod.split(".")
    if current_path.name != "__init__.py":
        parts = parts[:-1]
    up = node.level - 1
    if up >= len(parts):
        raise SystemExit(f"build: invalid relative import in {current_path}:{node.lineno}")
    base = parts[: len(parts) - up]
    if node.module:
        base.extend(node.module.split("."))
    return ".".join(base)


def extract_target_names(target: ast.expr) -> set[str]:
    if isinstance(target, ast.Name):
        return {target.id}
    if isinstance(target, ast.Tuple | ast.List):
        names: set[str] = set()
        for elt in target.elts:
            names |= extract_target_names(elt)
        return names
    if isinstance(target, ast.Starred):
        return extract_target_names(target.value)
    return set()


def defined_names(stmt: ast.stmt) -> set[str]:
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        return {stmt.name}
    if isinstance(stmt, ast.Assign):
        names: set[str] = set()
        for target in stmt.targets:
            names |= extract_target_names(target)
        return names
    if isinstance(stmt, ast.AnnAssign):
        return extract_target_names(stmt.target)
    return set()


def match_load_asset(stmt: ast.stmt, path: Path) -> tuple[str, str] | None:
    if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
        return None
    target = stmt.targets[0]
    call = stmt.value
    if not isinstance(target, ast.Name) or not isinstance(call, ast.Call):
        return None
    if not isinstance(call.func, ast.Name) or call.func.id != "load_asset":
        return None
    if (
        len(call.args) == 2
        and not call.keywords
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "__file__"
        and isinstance(call.args[1], ast.Constant)
        and isinstance(call.args[1].value, str)
    ):
        return target.id, call.args[1].value
    raise SystemExit(
        f"build: {path}:{stmt.lineno}: unsupported load_asset() call; "
        'expected NAME = load_asset(__file__, "filename")'
    )


def check_nested_nodes(stmt: ast.stmt, current_mod: str, path: Path) -> None:
    for node in ast.walk(stmt):
        if node is stmt:
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                root_pkg = alias.name.split(".")[0]
                if root_pkg == PACKAGE or root_pkg not in sys.stdlib_module_names:
                    raise SystemExit(
                        f"build: {path}:{node.lineno}: nested import of {alias.name!r} "
                        "is not supported"
                    )
        elif isinstance(node, ast.ImportFrom):
            target_mod = resolve_import_module(current_mod, path, node)
            root_pkg = target_mod.split(".")[0]
            if root_pkg == PACKAGE or root_pkg not in sys.stdlib_module_names:
                raise SystemExit(
                    f"build: {path}:{node.lineno}: nested import from {target_mod!r} "
                    "is not supported"
                )
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "load_asset"
        ):
            raise SystemExit(
                f"build: {path}:{node.lineno}: load_asset() is only allowed in "
                'top-level assignments (NAME = load_asset(__file__, "filename"))'
            )


def check_stdlib_module(mod_name: str, path: Path, lineno: int) -> None:
    root_pkg = mod_name.split(".")[0]
    if root_pkg not in sys.stdlib_module_names:
        raise SystemExit(
            f"build: {path}:{lineno}: non-stdlib import {mod_name!r} is not allowed in bootstrap.py"
        )


def format_multiline_constant(var_name: str, content: str, path: Path) -> str:
    escaped = content.replace("\\", "\\\\").replace("\r", "\\r").replace('"""', '\\"\\"\\"')
    if escaped.endswith('"'):
        trailing_slashes = len(escaped) - 1 - len(escaped[:-1].rstrip("\\"))
        if trailing_slashes % 2 == 0:
            escaped = escaped[:-1] + '\\"'
    rhs = f'"""\\\n{escaped}"""'
    if ast.literal_eval(rhs) != content:
        raise SystemExit(f"build: failed to round-trip inlined asset for {var_name} in {path}")
    return f"{var_name} = {rhs}\n"


def is_main_guard(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.If)
        and isinstance(stmt.test, ast.Compare)
        and isinstance(stmt.test.left, ast.Name)
        and stmt.test.left.id == "__name__"
        and len(stmt.test.ops) == 1
        and isinstance(stmt.test.ops[0], ast.Eq)
        and len(stmt.test.comparators) == 1
        and isinstance(stmt.test.comparators[0], ast.Constant)
        and stmt.test.comparators[0].value == "__main__"
    )


def parse_module(src_dir: Path, mod_name: str) -> ModuleUnit:
    path = module_path(src_dir, mod_name)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    lines = source.splitlines(keepends=True)

    replacements: dict[int, tuple[int, str]] = {}
    deps: list[str] = []
    imports_used: list[tuple[str, str]] = []
    exports: set[str] = set()
    stdlib_imports: list[str] = []
    stdlib_bindings: dict[str, tuple[str, str]] = {}

    for idx, stmt in enumerate(tree.body):
        if stmt.end_lineno is None:
            raise SystemExit(f"build: missing end_lineno in {path}:{stmt.lineno}")
        start, end = stmt.lineno - 1, stmt.end_lineno

        # Strip per-module docstring (cli.py's docstring is emitted separately at file top)
        if (
            idx == 0
            and isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            replacements[start] = (end, "")
            continue

        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == PACKAGE or alias.name.startswith(f"{PACKAGE}."):
                    raise SystemExit(
                        f"build: {path}:{stmt.lineno}: use 'from {alias.name} import ...' "
                        "instead of dotted module imports"
                    )
                check_stdlib_module(alias.name, path, stmt.lineno)
                bound = alias.asname or alias.name.split(".")[0]
                stdlib_bindings[bound] = ("import", alias.name)
            stdlib_imports.append("".join(lines[start:end]))
            replacements[start] = (end, "")
            continue

        if isinstance(stmt, ast.ImportFrom):
            if stmt.level == 0 and stmt.module == "__future__":
                replacements[start] = (end, "")
                continue
            target_mod = resolve_import_module(mod_name, path, stmt)
            if target_mod == PACKAGE or target_mod.startswith(f"{PACKAGE}."):
                for alias in stmt.names:
                    if alias.name == "*" or alias.asname is not None:
                        raise SystemExit(
                            f"build: {path}:{stmt.lineno}: star or aliased internal imports "
                            "are not supported"
                        )
                    if alias.name != "load_asset":
                        imports_used.append((target_mod, alias.name))
                if target_mod not in deps:
                    deps.append(target_mod)
                replacements[start] = (end, "")
            else:
                check_stdlib_module(target_mod, path, stmt.lineno)
                for alias in stmt.names:
                    if alias.name == "*":
                        raise SystemExit(
                            f"build: {path}:{stmt.lineno}: star stdlib imports are not supported"
                        )
                    bound = alias.asname or alias.name
                    stdlib_bindings[bound] = ("from", f"{target_mod}.{alias.name}")
                stdlib_imports.append("".join(lines[start:end]))
                replacements[start] = (end, "")
            continue

        if isinstance(stmt, ast.FunctionDef) and stmt.name == "load_asset":
            replacements[start] = (end, "")
            continue

        if asset := match_load_asset(stmt, path):
            var_name, asset_name = asset
            asset_path = path.with_name(asset_name)
            if not asset_path.is_file():
                raise SystemExit(f"build: {path}:{stmt.lineno}: missing asset {asset_path}")
            content = asset_path.read_text(encoding="utf-8")
            replacements[start] = (end, format_multiline_constant(var_name, content, path))
            exports.add(var_name)
            continue

        if is_main_guard(stmt):
            replacements[start] = (end, "")
            continue

        check_nested_nodes(stmt, mod_name, path)
        exports |= defined_names(stmt)

    out_chunks: list[str] = []
    line_idx = 0
    while line_idx < len(lines):
        if line_idx in replacements:
            end_idx, replacement = replacements[line_idx]
            if replacement:
                out_chunks.append(replacement)
            line_idx = end_idx
        else:
            out_chunks.append(lines[line_idx])
            line_idx += 1

    body_text = "".join(out_chunks).strip()
    return ModuleUnit(
        name=mod_name,
        path=path,
        source=source,
        tree=tree,
        deps=deps,
        imports_used=imports_used,
        exports=exports,
        stdlib_imports=stdlib_imports,
        stdlib_bindings=stdlib_bindings,
        body_text=body_text,
    )


def collect_modules(src_dir: Path) -> list[ModuleUnit]:
    units: dict[str, ModuleUnit] = {}
    visiting: list[str] = []
    ordered: list[str] = []

    def visit(mod_name: str) -> None:
        if mod_name in ordered:
            return
        if mod_name in visiting:
            cycle = " -> ".join([*visiting[visiting.index(mod_name) :], mod_name])
            raise SystemExit(f"build: import cycle detected: {cycle}")
        visiting.append(mod_name)
        unit = parse_module(src_dir, mod_name)
        units[mod_name] = unit
        for dep in unit.deps:
            visit(dep)
        visiting.pop()
        ordered.append(mod_name)

    # Seed config first so VERSION stays at the top of bootstrap.py
    visit(f"{PACKAGE}.config")
    visit(f"{PACKAGE}.cli")

    visited_paths = {u.path.resolve() for u in units.values()}
    for candidate in sorted((src_dir / PACKAGE).rglob("*.py")):
        if candidate.resolve() in visited_paths:
            continue
        if candidate.read_text(encoding="utf-8").strip():
            raise SystemExit(
                f"build: unreferenced non-empty module {candidate} is not reachable from cli.py"
            )

    return [units[name] for name in ordered]


def validate_symbols(modules: list[ModuleUnit]) -> None:
    by_name = {m.name: m for m in modules}
    stdlib_targets: dict[str, tuple[tuple[str, str], Path]] = {}
    for mod in modules:
        for bound, origin in mod.stdlib_bindings.items():
            prev = stdlib_targets.get(bound)
            if prev and prev[0] != origin:
                raise SystemExit(
                    f"build: conflicting stdlib import binding {bound!r} in "
                    f"{prev[1]} ({prev[0][1]}) and {mod.path} ({origin[1]})"
                )
            stdlib_targets[bound] = (origin, mod.path)

    defined_in: dict[str, Path] = {}
    for mod in modules:
        for symbol in sorted(mod.exports):
            if symbol in stdlib_targets:
                origin, stdlib_path = stdlib_targets[symbol]
                raise SystemExit(
                    f"build: top-level symbol {symbol!r} in {mod.path} shadows "
                    f"stdlib import {origin[1]} from {stdlib_path}"
                )
            if symbol in defined_in:
                raise SystemExit(
                    f"build: duplicate top-level symbol {symbol!r} in "
                    f"{defined_in[symbol]} and {mod.path}"
                )
            defined_in[symbol] = mod.path

    for mod in modules:
        for target_mod, symbol in mod.imports_used:
            target = by_name.get(target_mod)
            if target is None or symbol not in target.exports:
                raise SystemExit(
                    f"build: {mod.path} imports {symbol!r} from {target_mod}, "
                    "which is not defined at module top level"
                )


def extract_docstring_block(unit: ModuleUnit) -> str:
    if (
        unit.tree.body
        and isinstance(unit.tree.body[0], ast.Expr)
        and isinstance(unit.tree.body[0].value, ast.Constant)
        and isinstance(unit.tree.body[0].value.value, str)
    ):
        stmt = unit.tree.body[0]
        assert stmt.end_lineno is not None
        lines = unit.source.splitlines(keepends=True)
        return "".join(lines[stmt.lineno - 1 : stmt.end_lineno]).strip()
    return ""


def run_ruff(args: list[str], source: str, cwd: Path) -> str:
    proc = subprocess.run(
        ["ruff", *args],
        cwd=cwd,
        input=source,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise SystemExit(f"build: ruff {' '.join(args)} failed:\n{detail}")
    return proc.stdout


def build_source(root: Path) -> str:
    src_dir = root / "src"
    ruff_config = root / "ruff.toml"
    modules = collect_modules(src_dir)
    validate_symbols(modules)

    cli_unit = next(m for m in modules if m.name == f"{PACKAGE}.cli")
    docstring = extract_docstring_block(cli_unit)

    stdlib_block = "".join(imp for mod in modules for imp in mod.stdlib_imports)

    parts = [f"#!/usr/bin/env python3\n{docstring}" if docstring else "#!/usr/bin/env python3"]
    parts.append(HEADER_NOTE)
    parts.append("from __future__ import annotations\n\n" + stdlib_block)

    for mod in modules:
        if not mod.body_text:
            continue
        short = mod.name.removeprefix(f"{PACKAGE}.")
        if short != "config":
            label = short.replace(".", "/")
            banner = f"# --- {label} ".ljust(92, "-")
            parts.append(f"{banner}\n\n\n{mod.body_text}")
        else:
            parts.append(mod.body_text)

    parts.append('if __name__ == "__main__":\n    sys.exit(main())\n')
    raw = "\n\n".join(parts)

    cfg = ["--config", str(ruff_config)]
    sorted_src = run_ruff(
        ["check", "--fix", "--select", "I", *cfg, "--stdin-filename", "bootstrap.py", "-"],
        raw,
        root,
    )
    formatted = run_ruff(
        ["format", *cfg, "--stdin-filename", "bootstrap.py", "-"],
        sorted_src,
        root,
    )
    run_ruff(
        ["check", *cfg, "--stdin-filename", "bootstrap.py", "-"],
        formatted,
        root,
    )

    if not re.search(rb"^VERSION = (\d+)$", formatted.encode("utf-8"), re.MULTILINE):
        raise SystemExit("build: generated bootstrap.py is missing top-level 'VERSION = <int>'")
    return formatted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify bootstrap.py matches src/cloud_env without writing",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root directory",
    )
    args = parser.parse_args()

    root = args.root.resolve()
    target = root / "bootstrap.py"
    expected = build_source(root)

    if args.check:
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            print(
                "bootstrap.py is out of date with src/cloud_env; "
                "run 'mise run build' (or 'mise run sign')",
                file=sys.stderr,
            )
            return 1
        return 0

    target.write_text(expected, encoding="utf-8")
    target.chmod(0o755)
    return 0


if __name__ == "__main__":
    sys.exit(main())
