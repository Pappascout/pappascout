"""Tests for the layering rule.

The spine's dependency diagram is ``cli -> stages -> {domain, adapters,
archive, render}``, ``render -> domain`` and ``adapters -> domain``. Every one
of its rules is now machine-enforceable:

* ``domain`` imports nothing from the other pappascout packages,
* ``archive`` does not depend on ``domain`` -- it is a pipe, not a store of
  domain models,
* ``adapters`` knows nothing of the archive, the stages or the command line,
* ``render`` (Story 2.4) sees only ``domain``: it must not touch the archive,
  the adapters or the stages, which makes "render computes nothing" a
  structural promise and not merely a habit,
* ``stages`` does not call back into the command line, and
* ``cli`` **does not call the adapters or the archive directly**.

The last rule was relaxed in Story 1.1: ``info`` needed the archive path, and
there was no ``stages`` package. Story 1.2 removed the relaxation. Paths are
now asked of ``stages.archive_paths`` and the demo port of
``stages.parse.default_parser``, so the command line sees only the ``stages``
and ``domain`` packages.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pappascout"

#: The packages a package must NOT import.
#:
#: ``render`` (Story 2.4) is the presentation layer: it reads ``domain``'s
#: report model and formats it as Markdown. It must not touch the archive, the
#: adapters or the stages -- otherwise "render computes nothing" would stop
#: being a structural promise and would be merely a habit. The arrow is
#: ``stages -> render -> domain``.
FORBIDDEN = {
    "domain": {"archive", "adapters", "stages", "cli", "render"},
    "archive": {"domain", "adapters", "stages", "cli", "render"},
    "adapters": {"archive", "stages", "cli", "render"},
    "render": {"archive", "adapters", "stages", "cli"},
    "stages": {"cli"},
    "cli": {"adapters", "archive"},
}

#: Only the packages that exist -- the list grows by itself as stages arrive.
EXISTING = sorted(p for p in FORBIDDEN if (SRC / p).is_dir())

#: Every pappascout subpackage, including the ones that do not exist yet. A
#: relative import is recognised only by these names.
PACKAGES = {
    "cli",
    "stages",
    "domain",
    "adapters",
    "archive",
    "render",
    "templates",
}


def _imported_packages(path: Path) -> set[str]:
    """Read the file and collect the pappascout subpackages it imports."""
    own_package = path.relative_to(SRC).parts[0] if path.parent != SRC else ""
    return _scan(path.read_text(encoding="utf-8"), own_package, str(path))


def _scan(source: str, own_package: str, filename: str = "<source>") -> set[str]:
    """Collect the pappascout subpackages a file imports.

    Covers three forms:

    * ``import pappascout.archive``
    * ``from pappascout.archive import x``
    * ``from ..archive import x`` (relative)

    Without handling relative imports the rule would be easy to sidestep
    by accident.
    """
    tree = ast.parse(source, filename=filename)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "pappascout" and len(parts) > 1:
                    found.add(parts[1])
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level == 0:
            if node.module:
                parts = node.module.split(".")
                if parts[0] == "pappascout" and len(parts) > 1:
                    found.add(parts[1])
            continue

        # Relative import. level 1 = own package, level 2 = the pappascout root.
        if node.module:
            top_package = node.module.split(".")[0]
            if node.level >= 2 and top_package in PACKAGES:
                found.add(top_package)
            elif node.level == 1 and own_package in PACKAGES:
                found.add(own_package)
        elif node.level >= 2:
            # from .. import archive
            for alias in node.names:
                if alias.name in PACKAGES:
                    found.add(alias.name)

    found.discard(own_package)
    return found


@pytest.mark.parametrize("package", EXISTING)
def test_package_respects_dependency_arrows(package: str) -> None:
    directory = SRC / package
    forbidden = FORBIDDEN[package]
    for path in directory.rglob("*.py"):
        violations = _imported_packages(path) & forbidden
        assert not violations, (
            f"{path.relative_to(SRC)} imports a forbidden package: "
            f"{', '.join(sorted(violations))}"
        )


@pytest.mark.parametrize(
    "source_code,expected",
    [
        ("from ..archive import manifest", {"archive"}),
        ("from ..stages.parse import run", {"stages"}),
        ("from .. import archive", {"archive"}),
        ("from pappascout.archive import manifest", {"archive"}),
        ("import pappascout.stages.parse", {"stages"}),
        ("from .schemas import ROUNDS", set()),
        ("import polars as pl", set()),
    ],
)
def test_import_forms_are_all_detected(source_code: str, expected: set) -> None:
    """The rule must not be sidestepped with a relative import.

    The source is given as a string, so that the test writes nothing into the
    src tree.
    """
    assert _scan(source_code, "domain") == expected


def test_absolute_imports_are_detected() -> None:
    """The current code imports domain and archive only absolutely."""
    found_imports = _imported_packages(SRC / "archive" / "manifest.py")
    assert "archive" not in found_imports  # own package is not a dependency
    assert "domain" not in found_imports


def test_domain_does_no_file_io_except_settings_loading() -> None:
    """``domain`` is pure: only settings loading touches the disk.

    ``schemas`` holds the rules of the game and the table contracts and must
    not open files; ``models`` loads the settings, which is its only job.
    """
    source_code = (SRC / "domain" / "schemas.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "read_text", "write_text", "read_parquet"):
        assert forbidden not in source_code, forbidden
