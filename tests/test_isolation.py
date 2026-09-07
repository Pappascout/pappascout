"""Testien koneriippumattomuus.

Nama testit valvovat conftestin autouse-fixturea. Jos se lakkaa toimimasta,
muut testit alkaisivat lukea taman koneen oikeaa avaintiedostoa ja rglobata
satojen megatavujen OneDrive-arkiston lapi -- ja niiden tulos riippuisi siita,
kenen koneella ne ajetaan.
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
    """Path.home() osoittaa vaeliaikaishakemistoon, ei oikeaan profiiliin."""
    assert Path.home() == tmp_path / "koti"
    assert "AppData" in str(Path.home()) or "Temp" in str(Path.home())


def test_real_secrets_file_is_out_of_reach() -> None:
    """secrets_env_path() ei osu taman koneen oikeaan avaintiedostoon."""
    path = secrets_env_path()
    assert not path.exists()
    assert ".pappascout" in str(path)


def test_leaky_env_vars_are_cleared() -> None:
    for name in LEAKY_ENV_VARS:
        assert name not in os.environ, name


def test_settings_fixture_points_away_from_the_real_archive(
    settings_file: Path, tmp_path: Path
) -> None:
    s = load_settings(settings_file, env_files=())
    root = ArchivePaths.from_settings(s.project.archive_root).root
    assert root == tmp_path / "arkisto"
    assert "OneDrive" not in str(root)


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
            "PAPPASCOUT_ARCHIVE_ROOT ei ollut asetettu tuontihetkella, "
            "joten talla koneella ei ole arkistoa ohitettavaksi."
        )
    assert ARCHIVE_ROOT is not None, (
        "PAPPASCOUT_ARCHIVE_ROOT on asetettu, mutta conftest ei saanut "
        "arkiston juurta ratkaistua -- merkityt testit ohittuisivat kaikki "
        "aanettomasti"
    )
    assert ARCHIVE_ROOT.is_dir(), (
        f"Arkiston juuri {ARCHIVE_ROOT} ei ole hakemisto, joten "
        "arkistoriippuvaiset testit ohittuisivat vaikka muuttuja on asetettu"
    )
