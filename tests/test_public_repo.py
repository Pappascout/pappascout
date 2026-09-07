"""Guards for a repository that is public.

The archive path was the one leak that identified a person and an employer,
and Story 3.10 moved it into the machine's environment. The guard that
replaced it, though, only ever read **one line** of one file -- while the leak
is repository-shaped: the same path could be written back into any docstring,
comment or README and every test would stay green.

These tests walk the whole tree instead. The names they look for are **not in
this repository**: they are read from a machine-local file, the same
"the machine knows, the repository does not" shape as
:func:`conftest.require_demo`. A committed denylist would be the leak it
guards against.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import (
    FORBIDDEN_NAMES,
    FORBIDDEN_NAMES_ENV_VAR,
    FORBIDDEN_NAMES_FILE,
    REPO_ROOT,
)

#: File kinds that carry prose a leak could hide in.
#:
#: Not every file in the tree: ``uv.lock`` is generated, and binary content
#: cannot be read as text. These four cover source, settings, documentation
#: and the ignore file -- everything a human writes here.
WATCHED_GLOBS = ("*.py", "*.toml", "*.md")
WATCHED_NAMES = (".gitignore",)

#: Directories that are not part of the repository's own content.
SKIPPED_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
)


def _watched_files() -> list[Path]:
    """Every text file in the repository a leak could be written into."""
    found: list[Path] = []
    for pattern in (*WATCHED_GLOBS, *WATCHED_NAMES):
        for path in REPO_ROOT.rglob(pattern):
            if SKIPPED_DIRS.isdisjoint(path.parts):
                found.append(path)
    return sorted(set(found))


def _require_denylist() -> dict[str, int]:
    """The denylist, or a clean skip on a machine that has no list."""
    if FORBIDDEN_NAMES is None:
        pytest.skip(
            f"Kiellettyjen nimien luetteloa ei ole tällä koneella: "
            f"{FORBIDDEN_NAMES_FILE}. Luettelo on tarkoituksella repon "
            f"ulkopuolella; toisen polun voi antaa muuttujalla "
            f"{FORBIDDEN_NAMES_ENV_VAR}."
        )
    return FORBIDDEN_NAMES


def test_the_watched_set_is_not_empty() -> None:
    """A walker that finds nothing would pass every guard below.

    Without this the whole file could silently become a no-op -- a renamed
    directory or a bad glob and the leak guard would report success over an
    empty set.
    """
    files = _watched_files()
    assert len(files) > 50, len(files)
    names = {path.name for path in files}
    assert "settings.toml" in names
    assert "README.md" in names
    assert ".gitignore" in names
    assert "conftest.py" in names


def test_no_forbidden_name_exceeds_its_ceiling() -> None:
    """No identifying name may appear more often than its recorded ceiling.

    A name that is already gone has a ceiling of 0, so writing the old path
    back into any file in the tree fails here. A name still present in prose
    awaiting the per-package translation work carries its measured count, so
    it cannot spread in the meantime.

    The failure names the files, because a count alone does not tell the
    reader what to fix.
    """
    denylist = _require_denylist()
    hits: dict[str, list[str]] = {name: [] for name in denylist}
    for path in _watched_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):  # pragma: no cover - machine
            continue
        lowered = text.lower()
        relative = path.relative_to(REPO_ROOT).as_posix()
        for name in denylist:
            count = lowered.count(name.lower())
            if count:
                hits[name].append(f"{relative} ({count})")

    over = {
        name: places
        for name, places in hits.items()
        if sum(int(place.rsplit("(", 1)[1].rstrip(")")) for place in places)
        > denylist[name]
    }
    assert not over, "kielletty nimi ylittää kattonsa: " + "; ".join(
        f"{len(places)} tiedostossa -> {', '.join(places)}"
        for places in over.values()
    )


def test_the_archive_root_line_is_covered_by_the_walker() -> None:
    """The versioned ``archive_root`` line is inside the watched set.

    The line-level guard in :mod:`test_settings` and this repository-level one
    have to overlap on the line that actually leaked; otherwise a future
    refactor could move the setting somewhere the walker does not read and
    both guards would still pass.
    """
    settings = REPO_ROOT / "settings.toml"
    assert settings in _watched_files()
    line = next(
        row
        for row in settings.read_text(encoding="utf-8").splitlines()
        if row.startswith("archive_root")
    )
    assert "PAPPASCOUT_ARCHIVE_ROOT" in line
