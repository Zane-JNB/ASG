"""Project rules from CLAUDE.md that are easy to break without any other test noticing."""
import ast
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = sorted((ROOT / "scheduler").glob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def test_the_logic_never_reads_the_clock_itself():
    """now/today are passed in, so tests drive them. Only db stamps its rows (db._now) and the
    task menu's loop reads the clock before each action (its injectable default)."""
    reads = Counter(path.name for path in APP for node in ast.walk(_tree(path))
                    if isinstance(node, ast.Attribute) and node.attr in ("now", "today")
                    and isinstance(node.value, ast.Name) and node.value.id in ("datetime", "date"))
    assert reads == {"db.py": 1, "task_manager.py": 1}


def test_no_module_imports_another_modules_private_names():
    """A name starting with _ is private to its module: share it by making it public."""
    leaks = [f"{path.relative_to(ROOT)}: {node.module}.{alias.name}"
             for path in APP for node in ast.walk(_tree(path))
             if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("scheduler")
             for alias in node.names if alias.name.startswith("_")]
    assert leaks == []
