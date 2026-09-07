# mypy: disable-error-code="arg-type,assignment,attr-defined,comparison-overlap,func-returns-value,index,misc,no-any-return,no-untyped-def,operator,override,return-value,unreachable,unused-ignore,var-annotated"
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_DIRS = (ROOT / "oslab", ROOT / "scripts")


def _is_true_node(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_subprocess_run(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "run":
        if isinstance(func.value, ast.Name) and func.value.id == "subprocess":
            return True
        if (
            isinstance(func.value, ast.Call)
            and isinstance(func.value.func, ast.Name)
            and func.value.func.id == "__import__"
            and func.value.args
            and isinstance(func.value.args[0], ast.Constant)
            and func.value.args[0].value == "subprocess"
        ):
            return True
    return False


def test_captured_text_subprocesses_use_replacement_utf8() -> None:
    violations: list[str] = []
    for directory in PRODUCTION_DIRS:
        for path in directory.rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not _is_subprocess_run(node):
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                if not (
                    _is_true_node(keywords.get("capture_output"))
                    and _is_true_node(keywords.get("text"))
                ):
                    continue
                encoding = keywords.get("encoding")
                errors = keywords.get("errors")
                if not (
                    isinstance(encoding, ast.Constant)
                    and encoding.value == "utf-8"
                    and isinstance(errors, ast.Constant)
                    and errors.value == "replace"
                ):
                    relative = path.relative_to(ROOT)
                    violations.append(f"{relative}:{node.lineno}")
    assert violations == []
