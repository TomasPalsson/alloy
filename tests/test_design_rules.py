"""Design rules enforced by AST/path checks, not by runtime behavior. See B23, B30."""

from __future__ import annotations

import ast
import pathlib

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "alloy"


def _module_files() -> list[pathlib.Path]:
    return sorted(PACKAGE_ROOT.glob("*.py"))


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _imported_module_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_b23_azure_and_openai_imports_confined_to_foundry() -> None:
    for path in _module_files():
        if path.name == "_foundry.py":
            continue
        roots = _imported_module_roots(_parse(path))
        assert "azure" not in roots, f"{path.name} imports azure.*, only _foundry.py may"
        assert "openai" not in roots, f"{path.name} imports openai, only _foundry.py may"


def test_b30_env_reads_confined_to_foundry() -> None:
    for path in _module_files():
        if path.name == "_foundry.py":
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Attribute) and node.attr == "environ":
                raise AssertionError(f"{path.name} reads os.environ, only _foundry.py may")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getenv"
            ):
                raise AssertionError(f"{path.name} calls os.getenv, only _foundry.py may")


def test_b30_no_utils_module_exists() -> None:
    names = {path.name for path in PACKAGE_ROOT.iterdir()}
    assert "utils.py" not in names
    assert "_utils.py" not in names
    assert "utils" not in names


def test_b30_async_defs_are_the_three_declared_entry_points() -> None:
    found = {
        node.name
        for path in _module_files()
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.AsyncFunctionDef)
    }
    # The rule is about async ENTRY POINTS, not a count. invoke_async and stream_async each
    # own a thread hop; run_stream owns none — it only consumes stream_async's iterator.
    # A fourth async def means a new hop nobody reviewed, so this stays an exact set.
    assert found == {"invoke_async", "stream_async", "run_stream"}


def test_b30_no_retry_primitives_outside_tests() -> None:
    for path in _module_files():
        text = path.read_text()
        assert "time.sleep" not in text, f"{path.name} uses time.sleep"
        assert "tenacity" not in text, f"{path.name} uses tenacity"
        assert "backoff" not in text, f"{path.name} uses backoff"
