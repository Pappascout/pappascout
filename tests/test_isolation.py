"""The tests do not depend on the machine.

These tests watch conftest's autouse fixture. If it stopped working, the
other tests would read this machine's real credentials file and rglob
through a synchronised folder holding hundreds of megabytes of archive --
and their result would depend on whose machine they were run on.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import (
    ARCHIVE_ROOT,
    LEAKY_ENV_VARS,
    REAL_SETTINGS,
    _MACHINE_ROOT_SET,
)
from pappascout.archive.paths import ARCHIVE_ROOT_ENV_VAR, ArchivePaths
from pappascout.domain.models import load_settings, secrets_env_path
from pappascout.errors import PappascoutError


def test_home_is_redirected_to_tmp(tmp_path: Path) -> None:
    """Path.home() points at a temporary directory, not at the real profile."""
    assert Path.home() == tmp_path / "koti"
    assert "AppData" in str(Path.home()) or "Temp" in str(Path.home())


def test_real_secrets_file_is_out_of_reach() -> None:
    """secrets_env_path() does not hit this machine's real credentials file."""
    path = secrets_env_path()
    assert not path.exists()
    assert ".pappascout" in str(path)


def test_leaky_env_vars_are_cleared() -> None:
    for name in LEAKY_ENV_VARS:
        assert name not in os.environ, name


def test_settings_fixture_points_away_from_the_real_archive(
    settings_file: Path, tmp_path: Path
) -> None:
    """The fixture's archive is this test's own directory, not the machine's.

    **The claim is an identity, so it is asserted as an identity.** Until
    2026-09-10 the second assertion here spelled out the sync product's name
    and checked that the resolved path did not contain it. That was wrong
    twice over.

    It breached AD-12, which allows the product name only where the code
    depends on the product's **observable convention** -- AD-7's
    conflict-copy naming pattern, which ``pipeline`` really does check --
    and never where it depends on the author's environment. "The archive is
    in that product's folder" is the author's environment, and this
    assertion depended on nothing else.

    And it was a weak guard. Move the archive to another sync product, or
    to a plain local disk, and the assertion goes on passing while checking
    nothing: green would mean "not checked", which is the shape this
    repository has found four times in three days. What the test means is
    that the fixture's archive is **not the machine's archive**, and the
    machine's archive is a path conftest already resolves -- with the same
    resolver the commands use -- so the assertion compares against that.

    Neither path may contain the other, either. A fixture root above the
    real archive would have the tests rglob straight through it, which is
    the whole cost the isolation exists to avoid.

    The comparison against the real root is conditional, because
    ``ARCHIVE_ROOT`` is ``None`` on a machine that has no archive (CI, or
    this one with the variable unset). That is not a silent skip: the first
    assertion is unconditional and is by itself the isolation proof, and
    where no real archive exists there is nothing for the fixture to be
    confused with. Whether a machine that *has* an archive fails to resolve
    it is a separate defect, and
    ``test_the_marked_tests_are_not_silently_skipped_on_this_machine``
    below is the test for it.
    """
    s = load_settings(settings_file, env_files=())
    root = ArchivePaths.from_settings(s.project.archive_root).root
    assert root == tmp_path / "arkisto", root
    if ARCHIVE_ROOT is not None:
        assert root != ARCHIVE_ROOT, root
        assert ARCHIVE_ROOT not in root.parents, root
        assert root not in ARCHIVE_ROOT.parents, root


def test_even_the_real_settings_cannot_reach_the_real_archive() -> None:
    """The real settings.toml resolves to no archive without the variable.

    This is what makes the archive path absent from the versioned file (Story
    3.10): the real path lives in the machine's PAPPASCOUT_ARCHIVE_ROOT, which
    the autouse fixture deletes. The versioned value is a placeholder, so the
    load fails -- stronger isolation than the previous claim (a path under the
    patched home), because no directory is created anywhere at all.
    """
    s = load_settings(REAL_SETTINGS, env_files=())
    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings(s.project.archive_root)
    assert ARCHIVE_ROOT_ENV_VAR in str(exc.value)


def test_the_marked_tests_are_not_silently_skipped_on_this_machine() -> None:
    """On a machine with an archive, the marked tests must actually run.

    **This is the one guard against the whole marked suite evaporating.**
    ``conftest._real_archive_root`` turns a resolution failure into ``None``,
    and ``None`` makes ``require_demo`` and ``require_parsed`` skip. A skip is
    not a failure, so 103 marker sites across four files could all stop
    running and pytest would still exit zero -- nothing else in the suite
    would notice.

    The condition is deliberately machine-local: it fires only when
    PAPPASCOUT_ARCHIVE_ROOT was set at import time, which is exactly the case
    where a skip would be wrong. CI has no variable and no archive, so it
    skips this test instead of failing it.

    The other Story 3.10 tests call ``ArchivePaths.from_settings`` directly and
    therefore never touch conftest's module-level resolution -- which is the
    new part, and the part that can break this way.
    """
    if not _MACHINE_ROOT_SET:
        pytest.skip(
            "PAPPASCOUT_ARCHIVE_ROOT was not set at import time, so this "
            "machine has no archive to skip over."
        )
    assert ARCHIVE_ROOT is not None, (
        "PAPPASCOUT_ARCHIVE_ROOT is set, but conftest could not resolve the "
        "archive root -- every marked test would skip silently"
    )
    assert ARCHIVE_ROOT.is_dir(), (
        f"The archive root {ARCHIVE_ROOT} is not a directory, so the "
        "archive-dependent tests would skip even though the variable is set"
    )
