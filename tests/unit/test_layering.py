"""Layer rules, checked by reading imports with `ast` (nothing is imported or run).

- services/*_rules.py import only the stdlib and app/core, and never call datetime.now()/utcnow()/today().
- api/ never imports models/ or repositories/ (it goes through services/).
- repositories/ never import api/.
"""

import ast
import sys
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
STDLIB = set(sys.stdlib_module_names)


def imported_modules(source: str) -> list[str]:
    """Absolute module names imported by the source (relative imports are refused: use absolute ones)."""
    mods: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                mods.append("." * node.level + (node.module or ""))
            elif node.module:
                mods.append(node.module)
                # `from app import models` imports the models package.
                mods += [f"{node.module}.{a.name}" for a in node.names if node.module in ("app", "app.core")]
    return mods


def rules_violations(source: str) -> list[str]:
    problems = []
    for mod in imported_modules(source):
        top = mod.split(".")[0]
        if mod.startswith("."):
            problems.append(f"relative import {mod}")
        elif top == "app":
            if mod == "app.core.config":
                problems.append("imports app.core.config (rules take configuration as arguments)")
            elif not (mod == "app.core" or mod.startswith("app.core.") or (mod.startswith("app.services.") and mod.endswith("_rules"))):
                problems.append(f"imports {mod} (only app.core and other *_rules modules are allowed)")
        elif top not in STDLIB:
            problems.append(f"imports third-party {mod}")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"now", "utcnow", "today"}:
                problems.append(f"calls .{node.func.attr}() (use app/core/clock.py)")
        is_name_call = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        if is_name_call and node.func.id in {"open", "print", "input"}:
            problems.append(f"calls {node.func.id}() (rules do no I/O)")
    return problems


def forbidden_imports(source: str, forbidden: tuple[str, ...]) -> list[str]:
    return [m for m in imported_modules(source) if any(m == f or m.startswith(f + ".") for f in forbidden)]


def files(pattern: str) -> list[Path]:
    return sorted(APP.glob(pattern))


RULES_FILES = files("services/*_rules.py")


def test_all_phase_1_rules_files_are_covered():
    names = {p.name for p in RULES_FILES}
    assert {"permission_rules.py", "seat_rules.py", "org_lifecycle_rules.py", "membership_rules.py",
            "email_rules.py"} <= names


@pytest.mark.parametrize("path", RULES_FILES, ids=lambda p: p.name)
def test_rules_files_are_pure(path):
    assert rules_violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", RULES_FILES, ids=lambda p: p.name)
def test_rules_files_import_nothing_from_data_or_web_layers(path):
    bad = forbidden_imports(path.read_text(encoding="utf-8"),
                            ("app.models", "app.repositories", "app.db", "app.api"))
    assert bad == []


@pytest.mark.parametrize("path", files("api/**/*.py"), ids=lambda p: str(p.relative_to(APP)))
def test_api_never_imports_models_or_repositories(path):
    assert forbidden_imports(path.read_text(encoding="utf-8"), ("app.models", "app.repositories")) == []


@pytest.mark.parametrize("path", files("repositories/**/*.py"), ids=lambda p: str(p.relative_to(APP)))
def test_repositories_never_import_api(path):
    assert forbidden_imports(path.read_text(encoding="utf-8"), ("app.api",)) == []


# The checker itself: prove it catches what it claims to catch.
@pytest.mark.parametrize("source", [
    "from app.models.org import Organisation",
    "import app.repositories.x",
    "from app.db.session import unit_of_work",
    "from app.services import seat_rules",
    "from app.api.deps import get_probes",
    "from app.core.config import settings",
    "import app.core.config",
    "from app.core import config",
    "import sqlalchemy",
    "from . import sibling",
    "from datetime import datetime\nx = datetime.now()",
    "import datetime\nx = datetime.datetime.utcnow()",
    "data = open('f').read()",
])
def test_rules_checker_flags_violations(source):
    assert rules_violations(source)


@pytest.mark.parametrize("source", [
    "import enum, re, hmac\nfrom app.core.enums import Role\nfrom app.core.clock import Clock",
    "from dataclasses import dataclass\nfrom datetime import datetime, timedelta, timezone",
])
def test_rules_checker_accepts_clean_files(source):
    assert rules_violations(source) == []


def test_other_layer_checkers_flag_violations():
    assert forbidden_imports("from app.models.org import X", ("app.models", "app.repositories"))
    assert forbidden_imports("from app import models", ("app.models",))
    assert forbidden_imports("from app.api.v1 import health", ("app.api",))
    assert not forbidden_imports("from app.services import x", ("app.models", "app.repositories"))
