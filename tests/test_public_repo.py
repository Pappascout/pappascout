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

import subprocess
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
            f"This machine has no denylist at {FORBIDDEN_NAMES_FILE}. The list is "
            f"deliberately outside the repository -- a committed one would be "
            f"the leak it guards against. Another path can be given with "
            f"{FORBIDDEN_NAMES_ENV_VAR}."
        )
    return FORBIDDEN_NAMES


#: Prefix marking a denylist entry as being about **commit metadata** rather
#: than file contents.
#:
#: One file, two guards. The names differ: an employer's mail domain never
#: appears in the prose but sits in every commit's author field, and the two
#: ceilings fall on different schedules -- file contents shrink tranche by
#: tranche, metadata only when the history is rewritten. Keeping them in one
#: machine-local file means one thing to copy to the second machine; keeping
#: them in separate key spaces means neither guard silently inherits the
#: other's entries.
COMMIT_PREFIX = "commit:"


def _file_ceilings() -> dict[str, int]:
    """Denylist entries about the contents of files."""
    return {
        name: ceiling
        for name, ceiling in _require_denylist().items()
        if not name.startswith(COMMIT_PREFIX)
    }


def _commit_ceilings() -> dict[str, int]:
    """Denylist entries about author and committer identity."""
    return {
        name[len(COMMIT_PREFIX) :].strip(): ceiling
        for name, ceiling in _require_denylist().items()
        if name.startswith(COMMIT_PREFIX)
    }


def _published_by_git() -> list[str]:
    """Everything git publishes about a commit besides its content.

    Author and committer name and mail, **and the message**. The message half
    was added 2026-09-10, after the first history rewrite: the guard had read
    identities only, and Story 3.10's own commit message still named the
    employer -- in a sentence explaining that it had removed the employer's
    name. Neither guard could see it. The file walker does not read commit
    data at all and this one was not reading messages, so a name could sit in
    the one place both of them looked past.
    """
    result = subprocess.run(
        ["git", "log", "--all", "--format=%an%n%ae%n%cn%n%ce%n%B"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:  # pragma: no cover - machine without git
        pytest.skip(f"git log failed: {result.stderr.strip()}")
    return [line for line in result.stdout.splitlines() if line.strip()]


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
    denylist = _file_ceilings()
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
    assert not over, "a forbidden name is over its ceiling: " + "; ".join(
        f"in {len(places)} files -> {', '.join(places)}"
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


def test_the_history_is_not_empty() -> None:
    """A history that reads as empty would pass the guard below for nothing.

    ``git log`` returning nothing -- a shallow clone, a detached worktree, a
    changed format string -- would make the metadata guard report success over
    no commits at all. That is the same no-op this file already refuses for
    the file walker.
    """
    fields = _published_by_git()
    assert len(fields) >= 40, len(fields)
    assert any("@" in field for field in fields), "no mail address in any commit"
    assert any("Story" in field for field in fields), "no message text in any commit"


def test_no_forbidden_name_is_in_the_commit_metadata() -> None:
    """Identity **and message** carry no name the denylist forbids.

    **This guard exists because its absence was the leak.** Every other check
    here walks files, and files were clean: the sweep that removed the
    author's name from the tree reported zero, and it was right about the
    tree. It said nothing about ``git log``, where 43 of 45 commits carried
    the employer's mail domain in author and committer fields, public on
    every commit page. Green meant "not checked".

    Metadata cannot be edited in place -- correcting it means rewriting the
    history -- so the ceiling starts at the measured count and falls to zero
    when that rewrite lands. Until then this guard's job is to stop the count
    rising, which is exactly what it would do if a machine committed without
    the per-repository identity override.
    """
    ceilings = _commit_ceilings()
    assert ceilings, (
        "the denylist records no commit: entries, so this guard checks "
        "nothing -- see COMMIT_PREFIX"
    )
    fields = [field.lower() for field in _published_by_git()]
    over: dict[str, tuple[int, int]] = {}
    for name, ceiling in ceilings.items():
        found = sum(field.count(name.lower()) for field in fields)
        if found > ceiling:
            over[name] = (found, ceiling)
    assert not over, "commit metadata over ceiling: " + "; ".join(
        f"{found} occurrences against a ceiling of {ceiling}"
        for found, ceiling in over.values()
    )
