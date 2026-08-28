"""Testien jaetut apurit.

Testit eivät tarvitse demoja, verkkoa **eivätkä tätä konetta**: taulut
rakennetaan käsin, ja sekä kotihakemisto että arkiston juuri ohjataan
väliaikaishakemistoon. Ilman sitä testit lukisivat oikeaa
``%USERPROFILE%\\.pappascout\\.env``-tiedostoa ja kävisivät läpi satojen
megatavujen OneDrive-arkiston -- tulos riippuisi koneesta.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from pappascout.archive.paths import ARCHIVE_ROOT_ENV_VAR
from pappascout.domain.schemas import Schema

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SETTINGS = REPO_ROOT / "settings.toml"

#: Ympäristömuuttujat, jotka eivät saa vuotaa koneelta testeihin.
LEAKY_ENV_VARS = (
    "FACEIT_API_KEY",
    "FACEIT_DOWNLOADS_TOKEN",
    "PAPPASCOUT_SETTINGS",
    ARCHIVE_ROOT_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _isolate_from_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Eristä jokainen testi koneen omista tiedostoista ja avaimista."""
    for name in LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    # Path.home() lukee Windowsilla USERPROFILE:n ja muualla HOME:n. Kumpikin
    # ohjataan tyhjään hakemistoon, jotta secrets_env_path() ei osu oikeaan
    # avaintiedostoon.
    koti = tmp_path / "koti"
    koti.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(koti))
    monkeypatch.setenv("USERPROFILE", str(koti))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def empty_frame(schema: Schema) -> pl.DataFrame:
    """Rakenna tyhjä DataFrame, joka vastaa täsmälleen annettua sopimusta."""
    return pl.DataFrame(schema=dict(schema))


@pytest.fixture
def env_file(tmp_path: Path):
    """Tehdas väliaikaisille .env-tiedostoille."""

    def _make(name: str = ".env", **values: str) -> Path:
        path = tmp_path / name
        path.write_text(
            "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8"
        )
        return path

    return _make


def settings_text(archive_root: Path | str, **replacements: str) -> str:
    """Oikean ``settings.toml``in sisältö arkistopolku vaihdettuna.

    Testit käyttävät samoja lukuja kuin tuotanto -- muuten ne eivät todistaisi
    mitään oikeasta asetustiedostosta -- mutta eivät koskaan oikeaa arkistoa.
    """
    teksti = REAL_SETTINGS.read_text(encoding="utf-8")
    rivi = next(r for r in teksti.splitlines() if r.startswith("archive_root"))
    teksti = teksti.replace(rivi, f"archive_root = '{archive_root}'")
    for vanha, uusi in replacements.items():
        assert vanha in teksti, f"korvattavaa ei löydy: {vanha}"
        teksti = teksti.replace(vanha, uusi)
    return teksti


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    """Kopio oikeasta ``settings.toml``ista, arkisto ohjattuna tmp_pathiin."""
    arkisto = tmp_path / "arkisto"
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(arkisto), encoding="utf-8")
    return target


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Vaihda työhakemisto tyhjään väliaikaishakemistoon."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def has_temp_leftovers(directory: Path) -> bool:
    """Onko hakemistoon jäänyt atomisen kirjoituksen väliaikaistiedostoja."""
    return any(p.name for p in directory.rglob("*.tmp-*"))
