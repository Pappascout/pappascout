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


def _real_archive_root() -> Path | None:
    """Oikean arkiston juuri -- ratkaistaan **tuontihetkellä**.

    Polku on laskettava ennen kuin ``_isolate_from_machine`` ohjaa
    ``USERPROFILE``:n väliaikaishakemistoon; muuten koneella olevaa arkistoa
    tarvitsevat testit eivät löytäisi mitään edes koneella, jolla se on.

    Returns:
        Hakemisto tai ``None``, jos asetustiedostoa ei voitu lukea. ``None`` on
        tarkoituksella eri asia kuin olemassa oleva polku: keksitty
        paikkamerkkipolku kertoisi ohitusviestissä hakemistosta, jota ei ole
        koskaan ollut olemassakaan.
    """
    try:
        data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
        raw = str(data["project"]["archive_root"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):  # pragma: no cover
        return None
    return Path(os.path.expandvars(raw)).expanduser()


def _real_import_dir() -> Path | None:
    """Oikeiden demojen hakemisto arkiston juuren alla."""
    root = _real_archive_root()
    return None if root is None else root / "import"


#: Oikean arkiston juuri, tai ``None`` jos sitä ei voitu päätellä.
ARCHIVE_ROOT: Path | None = _real_archive_root()

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


#: Story 2.10:n vikademo: pelaaja, jonka kontrolleri on tallella mutta pawn ei.
#:
#: ``anubis_vs_RCAVE_VETERANS`` **ei parsiutunut lainkaan** ennen Story 2.10:tä:
#: yhdellä pelaajalla yhdellä kierroksella ei ollut hahmoa kartalla, ja
#: näytepistelukijan elossaolovartija kaatoi koko demon. Se maksoi kolmanneksen
#: uuden vastustajan aineistosta.
#:
#: Demo on tässä siksi, että korjauksen mittaus olisi **toistettavissa
#: reposta** eikä vain kirjattuna docstringiin. Se on arkiston ``import/``in
#: ainoa demo, jossa pawnittomia rivejä on, ja siksi ainoa, joka voi kertoa
#: ohituksen lakanneen toimimasta.
#:
#: Se ei ole :data:`LEAGUE_DEMOS`issa eikä ``ALL_DEMOS``issa: se on
#: Europe 5v5 Queue -ottelu eikä liigaottelu, eivätkä sen luvut kuulu
#: uudelleenaloituksen tai kalibroinnin regressiosarjoihin.
PAWNLESS_DEMO = "anubis_vs_RCAVE_VETERANS.dem.zst"

#: Vikademon koko ja SHA-256, mitattu 2026-08-31. Sama sääntö kuin
#: :data:`LEAGUE_DEMO_FILES`illa: puuttuva demo ohitetaan, väärä ei saa mennä
#: läpi hiljaa.
PAWNLESS_DEMO_FILE: tuple[int, str] = (
    204_420_133,
    "e9dcf35da6836f6d81d30d14029c62b8fc7661861ba685c0b89a18511035b7b5",
)

#: Vikademon mitatut luvut (2026-08-31, tuotannon ``[parse]``-asetuksilla).
#:
#: ``PAWNLESS_DEMO_ROUNDS``
#:     Pelatut kierrokset. Tulos 13-9, eli MR12:n mukainen ottelu.
#: ``PAWNLESS_DEMO_ROWS``
#:     Ohitetut pawnittomat pelaajarivit: **yksi pelaaja** (``egerrrrr``,
#:     76561199635619622) **yhdellä kierroksella** (round_no 19). Viisi
#:     näytepisteiden tickeiltä ja kymmenen utilityn heittotickeiltä; sama
#:     tick lasketaan kerran.
#: ``PAWNLESS_DEMO_POINTS``
#:     Kokonaan väliin jääneet näytepisteet. Nolla: yksi puuttuva pelaaja
#:     kymmenestä jättää pisteen vajaaksi muttei tyhjäksi.
PAWNLESS_DEMO_ROUNDS = 22
PAWNLESS_DEMO_ROWS = 15
PAWNLESS_DEMO_POINTS = 0


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


#: Taulut, jotka arkistoriippuvainen testi tarvitsee jokaiselta demolta.
#:
#: **Koko luettelo eikä vain se, jota testi lukee suoraan.** Kalibroinnin
#: testit rakentavat raportin ``stages.aggregate``in läpi, ja vaihe lukee
#: nämä kaikki; osittain parsittu arkisto kaataisi testin sen sijaan että
#: ohittaisi sen, eikä virhe kertoisi että vika on koneen aineistossa.
REQUIRED_PARSED_TABLES = (
    "rounds",
    "ticks",
    "events",
    "lineups",
    "deaths",
    "match",
    "callouts",
)


def require_parsed(*map_demo_ids: str) -> Path:
    """Palauta arkiston juuri tai ohita testi, jos demoja ei ole parsittu.

    **Eri vaatimus kuin :func:`require_demo`illa.** Kalibroinnin luvut
    lasketaan ``parsed/``- ja ``classified/``-tauluista, ei demotiedostoista:
    ne ovat megatavuja siinä missä demot ovat satoja megatavuja, ja ne ovat
    juuri se aineisto, jota vasten kynnykset mitattiin. Testi **ei kirjoita
    arkistoon mitään** -- se lukee taulut ja rakentaa raportin muistiin.

    Ohitusviesti nimeää puuttuvan taulun ja komennon, jolla se saadaan, jotta
    puuttuva parsinta erottuu puuttuvasta arkistosta ja luokittelusta.
    """
    if ARCHIVE_ROOT is None:  # pragma: no cover - riippuu koneesta
        pytest.skip("Arkiston juurta ei voitu päätellä settings.tomlista.")
    if not ARCHIVE_ROOT.is_dir():  # pragma: no cover - riippuu koneesta
        pytest.skip(f"Arkistoa ei ole tällä koneella: {ARCHIVE_ROOT}")
    for map_demo_id in map_demo_ids:
        for name in REQUIRED_PARSED_TABLES:
            table = ARCHIVE_ROOT / "parsed" / map_demo_id / f"{name}.parquet"
            if not table.is_file():  # pragma: no cover - riippuu koneesta
                pytest.skip(
                    f"Demon {map_demo_id} taulua {name}.parquet ei ole tässä "
                    f"arkistossa ({table}). Aja: uv run pappascout parse "
                    f"{map_demo_id}"
                )
        # Luokittelu on oma vaiheensa: parsittu demo ilman luokittelua ei
        # kelpaa aggregoinnille, ja ohitusviesti nimeää eri komennon.
        classified = list(
            (ARCHIVE_ROOT / "classified").glob(f"*/{map_demo_id}.parquet")
        )
        if not classified:  # pragma: no cover - riippuu koneesta
            pytest.skip(
                f"Demoa {map_demo_id} ei ole luokiteltu tähän arkistoon. "
                f"Aja: uv run pappascout classify {map_demo_id} "
                "--kaikki-joukkueet"
            )
    return ARCHIVE_ROOT

#: Pistepilvi, jonka siteet erottuvat toisistaan: A on ruudun 0 ympärillä ja
#: B ruudun 100. Ruutu on ``(alue, cell_x, cell_y, cell_z)``.
#:
#: **Kolme ruutua per site**, koska yhden ruudun alueen säde on 0 -- ja
#: erottuvuusvartija jakaa siteiden etäisyyden juuri siteiden koolla, joten
#: yhden ruudun siteillä se vaientaa demon (ja on oikeassa: kahden ruudun
#: pilvi ei kerro kartan siterakenteesta mitään).
#:
#: Muut alueet ovat samalla akselilla, jotta jokainen ryhmä on luettavissa
#: silmällä: ``House`` 10 (A), ``SideEntrance`` 90 ja ``Ramp`` 85 (B),
#: ``Middle`` 50 (yhtä kaukana, siis jaettu keski). Spawnit ovat mukana
#: **tarkoituksella ryhmässä** -- ``CTSpawn`` A:n ja ``TSpawn`` B:n puolella,
#: kuten oikeilla kartoilla -- koska juuri se tekee spawnrajauksesta
#: määritelmän eikä siivousta.
#:
#: Yksi kopio kolmen sijaan: sääntö (``test_sampling``), aggregointi
#: (``test_aggregate``) ja vaihe (``test_stage_aggregate``) mittaavat samaa
#: geometriaa, ja kolmesta kopiosta ne voisivat ajautua erilleen.
SITE_CLOUD: tuple[tuple[str, int, int, int], ...] = (
    ("BombsiteA", 0, 0, 0),
    ("BombsiteA", 2, 0, 0),
    ("BombsiteA", -2, 0, 0),
    ("BombsiteB", 100, 0, 0),
    ("BombsiteB", 102, 0, 0),
    ("BombsiteB", 98, 0, 0),
    ("House", 10, 0, 0),
    ("SideEntrance", 90, 0, 0),
    ("Ramp", 85, 0, 0),
    ("Middle", 50, 0, 0),
    ("CTSpawn", 5, 0, 0),
    ("TSpawn", 95, 0, 0),
)

#: Sama pilvi, mutta siteet ovat päällekkäin: keskipisteiden ero on 2 ruutua
#: ja siteiden oma koko 20 + 20, eli suhde 0,05. Nukella mitattu suhde on
#: 0,47-0,54 ja kynnys 2,0, joten tämä on sama tila kärjistettynä.
OVERLAPPING_SITE_CLOUD: tuple[tuple[str, int, int, int], ...] = (
    ("BombsiteA", 0, 0, 0),
    ("BombsiteA", 20, 0, 0),
    ("BombsiteA", -20, 0, 0),
    ("BombsiteB", 2, 0, 0),
    ("BombsiteB", 22, 0, 0),
    ("BombsiteB", -18, 0, 0),
    ("House", 10, 0, 0),
)

#: Ympäristömuuttujat, jotka eivät saa vuotaa koneelta testeihin.
LEAKY_ENV_VARS = (
    "FACEIT_API_KEY",
    "FACEIT_DOWNLOADS_TOKEN",
    "PAPPASCOUT_SETTINGS",
    "PAPPASCOUT_DEMOS_ROOT",
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


def even_split(total: int, players: int) -> list[int]:
    """Jaa joukkuesumma pelaajille mahdollisimman tasan, laskevasti.

    **Oletus, ei havainto.** Kierrostaulu kirjaa pelaajakohtaisen jakauman,
    mutta käsin rakennetut rivit ja vanhat totuustaulut kirjaavat vain
    joukkuesumman. Tasajako pitää rivin sisäisesti johdonmukaisena (jakauman
    summa on ``money_buy_end``) ilman että jokainen testi kirjoittaa viisi
    lukua.

    Yksi paikka, koska tämä on juuri se oletus, jonka Story 1.10 sanoo
    oletukseksi: kaksi kopiota erkanisi toisistaan ja kumpikin näyttäisi
    itsenäiseltä todisteelta. Testi, joka tutkii nimenomaan jakaumaa, antaa
    sen itse.
    """
    base, extra = divmod(int(total), players)
    return [base + 1] * extra + [base] * (players - extra)


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
    """Kopio oikeasta ``settings.toml``ista, arkisto ohjattuna tmp_pathiin.

    Demot menevät arkiston omaan ``demos/``iin, koska niin tekee myös
    versioitu asetustiedosto. Toinen moodi on
    :func:`settings_file_local_demos`, ja **molemmat on ajettava komennon
    läpi**: se moodi, jota fixture ei kata, ei kulje CLI:stä kertaakaan, ja sen
    rikkoutuminen näkyisi vain vaihetesteissä.
    """
    archive_dir = tmp_path / "arkisto"
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    return target


#: Missä ladatut demot ovat, kun ``[project].demos_root`` on käytössä.
LOCAL_DEMOS_DIRNAME = "paikalliset-demot"


@pytest.fixture
def settings_file_local_demos(tmp_path: Path) -> Path:
    """Sama asetustiedosto, mutta demot arkiston **ulkopuolelle**.

    Rivi on versioidussa tiedostossa kommentoituna (arkisto on oletus, koska se
    seuraa koneelta toiselle ja OneDrive vapauttaa parsitun demon tilan
    poistamatta tiedostoa). Se on silti tuettu moodi -- ja tuettu moodi, jota
    mikään komentotesti ei aja, on moodi jonka rikkoutumisen huomaa vasta
    käyttäjä.
    """
    archive_dir = tmp_path / "arkisto"
    target = tmp_path / "settings.toml"
    text = settings_text(archive_dir)
    marker = "# demos_root = "
    assert marker in text, "versioidusta settings.tomlista puuttuu demos_root-rivi"
    line = next(r for r in text.splitlines() if r.startswith(marker))
    text = text.replace(
        line, f"demos_root = '{tmp_path / LOCAL_DEMOS_DIRNAME}'", 1
    )
    target.write_text(text, encoding="utf-8")
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
