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


# --- The opponent is stated and never grouped by (Story 4.10) -----------------


#: Where the opponent's name may be **read**, as ``module -> the functions``.
#:
#: **The property is an absence, and an absence is guarded by reading the
#: source** -- the shape this file already uses for the dependency arrows and
#: ``test_stage_aggregate``'s ``THRESHOLD_READ`` uses for the settings a stage
#: touches. There is no report you can render that demonstrates the absence of
#: a grouping; there is only the fact that nothing in the tree groups.
#:
#: The rule (Story 4.10, "Never"): **the opponent is stated, never grouped or
#: filtered by.** Measured 2026-09-23
#: (``opponent-buy-measured-2026-09-23.md``): a new axis takes rounds away
#: from the groups that have them and gives them to the groups that do not.
#: The reading guide says so to the Finnish reader in one clause, and before
#: this test that clause was held up by the golden document's text alone --
#: the only place in 29 mutations where a golden was carrying work a rule
#: should do.
#:
#: What each permitted reader does, and why neither is a grouping:
#:
#: ``_played_map_line``
#:     prints the name on the map's own row. Stating it is the story.
#: ``build_view``
#:     asks whether it is ``None``, to decide whether the reading guide should
#:     explain the mark that absence prints. It reads the presence and never
#:     the value.
#: ``played_maps_for``
#:     copies it from the match's facts onto the row. A move, not a decision.
#:
#: A fourth reader is not forbidden on principle -- it is forbidden until
#: somebody names it here and says which of the two things it does.
OPPONENT_READERS: dict[str, set[str]] = {
    "render/view.py": {"_played_map_line", "build_view"},
    "domain/aggregate.py": {"played_maps_for"},
}


def _functions_reading(tree: ast.AST, attribute: str) -> set[str]:
    """Every top-level function or method that reads ``x.<attribute>``."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr == attribute:
                found.add(node.name)
    return found


@pytest.mark.parametrize("module", sorted(OPPONENT_READERS))
def test_the_opponent_is_only_read_where_it_is_stated(module: str) -> None:
    """Nothing in the tree groups, filters or sorts by the opponent.

    A new reader fails this by name, which is the point: the next person to
    add one has to decide, in this list, whether it is stating the opponent
    or splitting by it -- and the second is a spine decision and not an
    implementation detail.

    Nested functions report under their enclosing function, which is right
    here: the claim is about which piece of work touches the value.
    """
    path = SRC / module
    tree = ast.parse(path.read_text(encoding="utf-8"))
    readers = _functions_reading(tree, "opponent")
    assert readers == OPPONENT_READERS[module], (
        f"{module} reads .opponent in {sorted(readers)}, but this rule allows "
        f"{sorted(OPPONENT_READERS[module])}. The opponent is stated and "
        "never grouped or filtered by (Story 4.10); a new reader belongs in "
        "OPPONENT_READERS with a sentence saying which of the two it does."
    )


def test_the_reader_list_is_not_vacuous() -> None:
    """Each named function really does read the value.

    Without this the rule could pass because the tree stopped reading the
    opponent at all -- which is what deleting the feature looks like, and it
    must not look like compliance.
    """
    for module, expected in OPPONENT_READERS.items():
        tree = ast.parse((SRC / module).read_text(encoding="utf-8"))
        assert _functions_reading(tree, "opponent") == expected, module
        assert expected, module
