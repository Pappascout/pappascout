"""Testien koneriippumattomuus.

Nama testit valvovat conftestin autouse-fixturea. Jos se lakkaa toimimasta,
muut testit alkaisivat lukea taman koneen oikeaa avaintiedostoa ja rglobata
satojen megatavujen OneDrive-arkiston lapi -- ja niiden tulos riippuisi siita,
kenen koneella ne ajetaan.
"""

from __future__ import annotations

import os
from pathlib import Path

from conftest import REAL_SETTINGS, LEAKY_ENV_VARS
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.models import load_settings, secrets_env_path


def test_home_is_redirected_to_tmp(tmp_path: Path) -> None:
    """Path.home() osoittaa vaeliaikaishakemistoon, ei oikeaan profiiliin."""
    assert Path.home() == tmp_path / "koti"
    assert "AppData" in str(Path.home()) or "Temp" in str(Path.home())


def test_real_secrets_file_is_out_of_reach() -> None:
    """secrets_env_path() ei osu taman koneen oikeaan avaintiedostoon."""
    polku = secrets_env_path()
    assert not polku.exists()
    assert ".pappascout" in str(polku)


def test_leaky_env_vars_are_cleared() -> None:
    for nimi in LEAKY_ENV_VARS:
        assert nimi not in os.environ, nimi


def test_settings_fixture_points_away_from_the_real_archive(
    settings_file: Path, tmp_path: Path
) -> None:
    s = load_settings(settings_file, env_files=())
    juuri = ArchivePaths.from_settings(s.project.archive_root).root
    assert juuri == tmp_path / "arkisto"
    assert "OneDrive" not in str(juuri)


def test_even_the_real_settings_cannot_reach_the_real_archive() -> None:
    """Oikea settings.toml laajenee patchatun kodin alle, ei OneDriveen.

    Talla varmistetaan, etta arkistopolun siirtaminen %USERPROFILE%-muotoon
    teki testeista koneriippumattomia myos silloin, kun testi lataa oikean
    asetustiedoston.
    """
    s = load_settings(REAL_SETTINGS, env_files=())
    juuri = ArchivePaths.from_settings(s.project.archive_root).root
    assert juuri.is_relative_to(Path.home())
    assert not juuri.exists()
