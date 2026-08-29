"""Testien jaetut apurit.

Testit eivät tarvitse demoja, verkkoa **eivätkä tätä konetta**: taulut
rakennetaan käsin, ja sekä kotihakemisto että arkiston juuri ohjataan
väliaikaishakemistoon. Ilman sitä testit lukisivat oikeaa
``%USERPROFILE%\\.pappascout\\.env``-tiedostoa ja kävisivät läpi satojen
megatavujen OneDrive-arkiston -- tulos riippuisi koneesta.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import polars as pl
import pytest

from pappascout.archive.paths import ARCHIVE_ROOT_ENV_VAR
from pappascout.domain.schemas import Schema

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SETTINGS = REPO_ROOT / "settings.toml"


def _real_import_dir() -> Path | None:
    """Oikeiden demojen hakemisto -- ratkaistaan **tuontihetkellä**.

    Polku on laskettava ennen kuin ``_isolate_from_machine`` ohjaa
    ``USERPROFILE``:n väliaikaishakemistoon; muuten demo-testit eivät löytäisi
    mitään edes koneella, jolla demot ovat.

    Returns:
        Hakemisto tai ``None``, jos asetustiedostoa ei voitu lukea. ``None`` on
        tarkoituksella eri asia kuin olemassa oleva polku: keksitty
        paikkamerkkipolku kertoisi demo-testin ohitusviestissä hakemistosta,
        jota ei ole koskaan ollut olemassakaan.
    """
    try:
        data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
        raw = str(data["project"]["archive_root"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):  # pragma: no cover
        return None
    return Path(os.path.expandvars(raw)).expanduser() / "import"


#: Oikeiden demojen hakemisto, tai ``None`` jos sitä ei voitu päätellä.
#: ``PAPPASCOUT_TEST_DEMOS`` ylikirjoittaa sen.
_FROM_ENV = os.environ.get("PAPPASCOUT_TEST_DEMOS")
DEMO_DIR: Path | None = Path(_FROM_ENV) if _FROM_ENV else _real_import_dir()

#: Testiaineisto (``_bmad-output/implementation-artifacts/testiaineisto.md``).
ANCIENT_DEM = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1.dem"
ANCIENT_ZST = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1.dem.zst"
NUKE_ZST = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1.dem.zst"

#: Odotetut tulokset oikeista demoista (FACEIT Data API, haettu 2026-08-28).
ANCIENT_ROUNDS = 21
NUKE_ROUNDS = 28

#: Pappaliigan viime kauden demot ja niiden pelatut kierrokset.
#:
#: **Korvaamatonta aineistoa.** FACEIT ei enää tarjoa näitä (säilytys ~30 pv),
#: eikä uusintaa ole. Ne ovat ainoa liigadata, jota vasten ottelun
#: uudelleenaloituksen käsittely on todennettu, eivätkä ne ole repossa vaan
#: arkiston ``import/``-hakemistossa. :func:`require_demo` ohittaa testin
#: siististi, jos niitä ei ole -- eli toisella koneella koko regressiosarja
#: haihtuu äänettömästi. Siksi koko ja tiiviste ovat kirjattuina
#: :data:`LEAGUE_DEMO_FILES`iin: väärä tai typistynyt kopio erottuu
#: puuttuvasta.
#:
#: Kaikissa neljässä on puukkokierroksen jälkeen ottelun uudelleenaloitus: oma
#: ``round_freeze_end`` ilman ``round_end``iä, ja demon oma kierrosnumerointi
#: jatkuu sen yli yhdellä. Se pelataan, mutta se ei ole kierros. Kuvion mittaus
#: on tallessa BMAD-projektin tiedostossa
#: ``_bmad-output/implementation-artifacts/vika-kierrosnumerointi.md``, joka on
#: **tämän repon ulkopuolella**; olennainen sisältö on toistettu
#: :mod:`pappascout.adapters.demo_parser`in moduulidokumentaatiossa, jottei
#: testi nojaa tiedostoon jota täällä ei ole.
#:
#: **Kierrosmäärän oraakkeli ei ole oman koodimme tuotos.** Se on demon omien
#: ``round_end``-tapahtumien määrä miinus puukkokierros, luettuna suoraan
#: demoparser2:n tapahtumavirrasta ohi kierrosnumeroinnistamme. Sama johdos
#: ajetaan testinä
#: (``test_league_round_count_matches_the_demos_own_event_stream``), joten luku
#: ei voi ajautua yhtä matkaa numeroinnin kanssa.
#:
#: **Älä käytä puolikohtaisia voittoja ottelun tuloksena.** Puolet vaihtuvat
#: puoliajalla, joten ``round_end``in ``winner``-kentän T/CT-jakauma ei ole
#: joukkueen tulos vaan puolen tulos.
LEAGUE_DEMOS: tuple[tuple[str, int], ...] = (
    ("Ancient_vs_kaljukostaja.dem", 20),
    ("Anubis_vs_ryhmarama.dem", 22),
    ("Nuke_vs_imuaijat.dem", 23),
    ("inferno_vs_ryhmarama.dem", 20),
)

#: Liigademojen koko tavuina ja SHA-256, mitattu 2026-08-29.
#:
#: Nämä eivät ole varmuuskopio vaan **tunniste**: jos arkiston tiedosto ei
#: täsmää, testien luvut eivät koske sitä tiedostoa. Puuttuva demo ohitetaan
#: siististi, mutta väärä demo ei saa mennä läpi hiljaa.
LEAGUE_DEMO_FILES: dict[str, tuple[int, str]] = {
    "Ancient_vs_kaljukostaja.dem": (
        379_946_762,
        "286e3f79fb192386e1fa9fea1503b91fa17ee24efde9aa11d35e63348fc8ecff",
    ),
    "Anubis_vs_ryhmarama.dem": (
        437_437_483,
        "4e1525551c9be68ee2ea66a5dce60b75a38aca19e97be89f0e37f58d8ccf336f",
    ),
    "Nuke_vs_imuaijat.dem": (
        444_162_824,
        "7290f40bd0ff7721ca6f3c989d357b5d8d47396f1ccba6916a1f7bfda3616e9f",
    ),
    "inferno_vs_ryhmarama.dem": (
        453_514_645,
        "a33e8bbf1054dc6b17b030a9b86f015b0e9138444a3d42e70fea1446e94021b1",
    ),
}


def require_demo(name: str) -> Path:
    """Palauta oikean demon polku tai ohita testi selkeällä syyllä.

    Demot ovat 100-230 MB eivätkä kuulu repoon, joten toisella koneella tai
    CI:ssä testin on ohituttava -- ei kaaduttava. Ohitusviesti kertoo aina,
    mistä etsittiin, jotta puuttuva demo erottuu väärästä polusta.
    """
    if DEMO_DIR is None:
        pytest.skip(
            "Demohakemistoa ei voitu päätellä settings.tomlista. Aseta "
            "ympäristömuuttuja PAPPASCOUT_TEST_DEMOS."
        )
    path = DEMO_DIR / name
    if not path.is_file():
        pytest.skip(f"Oikeaa demoa ei ole tällä koneella: {path}")
    return path

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
    home_dir = tmp_path / "koti"
    home_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
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
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    line = next(r for r in text.splitlines() if r.startswith("archive_root"))
    text = text.replace(line, f"archive_root = '{archive_root}'")
    for old, new in replacements.items():
        assert old in text, f"korvattavaa ei löydy: {old}"
        text = text.replace(old, new)
    return text


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    """Kopio oikeasta ``settings.toml``ista, arkisto ohjattuna tmp_pathiin."""
    archive_dir = tmp_path / "arkisto"
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
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
