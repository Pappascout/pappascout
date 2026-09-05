"""``stages.import_demo`` -- vaiheen testit (Story 3.6).

**Ei verkkoa eikä demoparser2:ta.** Vaihe näkee kaksi porttia,
:class:`~pappascout.adapters.protocols.MatchSource`in ja
:class:`~pappascout.adapters.protocols.DemoParser`in, ja molempien takana on
täällä feikki.

Molemmat feikit **tarkistavat saamansa syötteen**, eikä se ole yksityiskohta
vaan koko niiden arvo vartijana. Otteluportti, joka palauttaa saman vetotiedon
kysyttiin mitä tahansa, läpäisisi jokaisen testin myös silloin kun vaihe kysyy
väärää ottelua; otsikkoportti, joka palauttaa saman kartan nimen mille tahansa
polulle, läpäisisi ne myös silloin kun vaihe lukee väärää tiedostoa -- tai
tiedostoa, jota ei ole olemassa. Katselmus todisti jälkimmäisen:
``read_map_name``in polun korvaaminen olemattomalla läpäisi 70 testiä.
:class:`FakeMapNameParser` vaatii nyt, että polku on olemassa ja että sen
sisältö on tunnistettavissa demoksi.

Testiaineisto on **aitoja pakkauskehyksiä** eikä pelkkiä taikatavuja: zstd
kirjoitetaan ``zstandard``illa ja gzip ``gzip``illä, jotta
``declared_size``, ``length_source`` ja ehjyyden tarkistus kulkevat
oletusajossa oikeaa polkua eivätkä vain testin omaa.

I/O-matriisin rivit ovat tässä tiedostossa yksi testi kutakin kohden, ja
nimestä tunnistaa rivin.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import zstandard
from conftest import has_temp_leftovers

from pappascout.adapters.decompress import DEMO_MAGIC, GZIP_MAGIC, ZSTD_MAGIC
from pappascout.adapters.protocols import DemoParser, Match, MatchSource, MatchTeam
from pappascout.archive.paths import ArchivePaths
from pappascout.errors import ApiError, PappascoutError, ParseError
from pappascout.stages import import_demo as import_stage

#: Oikean muotoinen ottelutunniste: FACEITin ``match_id`` on itse muotoa
#: ``1-<uuid>``, eli siinä on väliviivoja ennen kartan numeroa.
MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"

#: Arkiston tunniste ensimmäiselle kartalle. ``--map 1`` -> ``map_index 0``.
UNIT = f"{MATCH}-0"
UNIT_TWO = f"{MATCH}-1"

#: FACEITin oma tiedostonimi samalle kartalle: ``{match_id}-{round}-{instance}``
#: jossa ``round`` on **1-pohjainen**. Yhden ero arkiston tunnisteeseen on koko
#: haun syy.
FACEIT_NAME = f"{MATCH}-1-1.dem.zst"
FACEIT_NAME_PLAIN = f"{MATCH}-1-1.dem"

PICKS = ("de_ancient", "de_nuke")

#: Kello, joka ei riipu ajohetkestä -- metatiedostojen vertailu vaatii sen.
CLOCK = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

#: Levytilaa on testeissä aina, ellei testi mittaa juuri sitä.
ROOMY = 100 * 1024**3


def plain_demo(size: int = 4096) -> bytes:
    """Pakkaamaton demo: ``PBDEMS2`` ja täytettä."""
    return (DEMO_MAGIC + bytes(range(256)) * 64)[:size]


PLAIN_BYTES = plain_demo()

#: **Aito zstd-kehys**, ei pelkät taikatavut. Kehys ilmoittaa puretun koon, ja
#: juuri se on se riippumaton pituuslähde, jota ehjyyden tarkistus käyttää.
ZSTD_BYTES = zstandard.ZstdCompressor().compress(PLAIN_BYTES)

#: Aito gzip-virta lopetusmerkkeineen.
GZIP_BYTES = gzip.compress(PLAIN_BYTES)

#: Sisältö, joka ei ole demo: HTML-virhesivu 200-statuksella.
HTML_PAGE = b"<!doctype html><html><body>403 Forbidden</body></html>"

#: Sama zstd-kehys puoliväliin katkaistuna.
#:
#: **Tätä ei koskaan pureta tässä tiedostossa, ja se on kerrosjaon seuraus.**
#: Vaihe näkee otsikon vain :class:`FakeMapNameParser`in takaa, joten katkennut
#: tiedosto mallinnetaan feikin nostamana ``ParseError``ina -- täällä
#: testataan, mitä vaihe tekee kun **portti kertoo** tiedoston olevan vajaa.
#: Se, että purku *havaitsee* vajauden, on eri väite ja eri kerros:
#: ``tests/test_demo_parser.py`` (purku) ja ``tests/test_demo_parser_logic.py``
#: (``read_map_name``). Ne käyttävät tarkoituksella **isoa** hyötykuormaa,
#: koska pieni purkautuisi katkaistuna nollaksi ja osuisi vanhaan
#: tyhjätarkistukseen; täällä koolla ei ole merkitystä, koska tavuja ei lueta.
TRUNCATED_ZSTD = ZSTD_BYTES[: len(ZSTD_BYTES) // 2]


# -- Kiinnikkeet -------------------------------------------------------------


@dataclass
class FakeMatchSource:
    """Otteluportin feikki: tunnisteesta vetotiedoksi, ilman verkkoa.

    Tuntematon tunniste nostaa ``ApiError``in kuten oikea adapteri. Se on
    vartija eikä kohteliaisuus: lähde, joka antaa vetotiedon mille tahansa
    merkkijonolle, kertoisi ettei vaihe koskaan tarkista mitä se kysyy.
    """

    matches: dict[str, Match] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        raise AssertionError(
            "Tuonti ei saa hakea kilpailun ottelulistaa -- se tarvitsee yhden "
            "ottelun vetotiedon."
        )

    def get_match(self, match_id: str) -> Match:
        self.asked.append(match_id)
        try:
            return self.matches[match_id]
        except KeyError:
            raise ApiError(
                f"Ottelua {match_id} ei löytynyt.", status_code=404
            ) from None


@dataclass
class FakeMapNameParser:
    """Otsikkoportin feikki: **tiedostosta** kartan nimeksi.

    Kolme vartijaa, ja jokainen niistä kaatuu eri mutaatioon:

    ``Polun on oltava olemassa``
        Katselmus korvasi vaiheen lukeman polun olemattomalla ja läpäisi 70
        testiä. Oikea adapteri nostaa puuttuvasta tiedostosta
        ``ParseError``in; feikki, joka ei välitä polusta, ei voi todistaa
        vaiheen lukevan sitä tiedostoa jonka se aikoo siirtää.
    ``Sisällön on oltava tunnistettavissa demoksi``
        Muuten feikki hyväksyisi minkä tahansa tiedoston -- myös
        HTML-virhesivun, jonka oikea adapteri torjuu.
    ``Nimen on oltava tunnettu``
        Tuntematon tiedosto nostaa ``ParseError``in, koska se on täsmälleen
        se, mitä oikea adapteri tekee sisällölle jota se ei tunnista.

    ``errors`` antaa nimeämälleen tiedostolle valmiin poikkeuksen: niin
    katkennut pakattu tiedosto voidaan mallintaa ilman purkukirjastoa.
    """

    names: dict[str, str | None] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    asked: list[Path] = field(default_factory=list)

    def read_map_name(self, path: Path) -> str | None:
        path = Path(path)
        self.asked.append(path)
        if not path.is_file():
            raise AssertionError(
                f"Otsikkoa yritettiin lukea polusta, jota ei ole: {path}. "
                "Oikea adapteri nostaisi tästä ParseErrorin."
            )
        head = path.read_bytes()[: len(DEMO_MAGIC)]
        if not any(
            head.startswith(magic)
            for magic in (ZSTD_MAGIC, GZIP_MAGIC, DEMO_MAGIC)
        ):
            raise ParseError(
                f"Tiedosto {path.name} ei ole CS2-demo: alkutavut {head!r}."
            )
        if path.name in self.errors:
            raise self.errors[path.name]
        if path.name not in self.names:
            raise ParseError(
                f"Tiedosto {path.name} ei ole CS2-demo: sen otsikkoa ei voitu "
                "lukea."
            )
        return self.names[path.name]

    def parse_demo(self, path: Path, sample_seconds: Any):
        raise AssertionError(
            "Tuonti ei saa parsia demoa: kartan nimi luetaan otsikosta."
        )


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


@pytest.fixture
def local_archive(tmp_path: Path) -> ArchivePaths:
    """Arkisto, jonka demot menevät **arkiston ulkopuolelle** (Story 3.4)."""
    return ArchivePaths(root=tmp_path / "arkisto", demos_root=tmp_path / "demot")


def match(picks: tuple[str, ...] = PICKS, match_id: str = MATCH) -> Match:
    return Match(
        match_id=match_id,
        status="FINISHED",
        teams=(MatchTeam(team_id="a"), MatchTeam(team_id="b")),
        map_picks=picks,
        best_of=2,
    )


@pytest.fixture
def source() -> FakeMatchSource:
    return FakeMatchSource({MATCH: match()})


@pytest.fixture
def parser() -> FakeMapNameParser:
    return FakeMapNameParser({FACEIT_NAME: "de_ancient"})


def place(archive: ArchivePaths, name: str, data: bytes = ZSTD_BYTES) -> Path:
    """Aseta tiedosto arkiston tuontikansioon."""
    directory = archive.import_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def read_meta(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_plan(
    archive: ArchivePaths,
    source: FakeMatchSource,
    parser: FakeMapNameParser,
    map_no: int | str = 1,
    **kwargs: Any,
):
    kwargs.setdefault("disk_free", lambda _path: ROOMY)
    return import_stage.plan(
        archive, MATCH, map_no, source=source, parser=parser, **kwargs
    )


def do_import(
    archive: ArchivePaths,
    source: FakeMatchSource,
    parser: FakeMapNameParser,
    map_no: int | str = 1,
    **kwargs: Any,
):
    todo = make_plan(archive, source, parser, map_no, **kwargs)
    return todo, import_stage.run(archive, todo, now=lambda: CLOCK)


# -- Matriisi: FACEIT-niminen .dem.zst, kartta täsmää ------------------------


def test_a_faceit_named_archive_is_moved_and_gets_its_meta(
    archive, source, parser
) -> None:
    origin = place(archive, FACEIT_NAME)

    todo, result = do_import(archive, source, parser)

    target = archive.demos_dir() / f"{UNIT}.dem.zst"
    assert target.read_bytes() == ZSTD_BYTES
    assert result.status == "ok"
    assert result.unit == UNIT
    assert todo.confirmations == ()
    meta = read_meta(archive.demos_dir() / f"{UNIT}.meta.json")
    assert meta["source"] == "import"
    assert meta["map_demo_id"] == UNIT
    assert meta["sha256"] == hashlib.sha256(ZSTD_BYTES).hexdigest()
    assert meta["size"] == len(ZSTD_BYTES)
    assert meta["fetched_at"] == CLOCK.isoformat()
    # Siirto eikä kopio: tuontikansio on saapuvien kansio, ei säilö.
    assert not origin.exists()


def test_the_map_number_the_user_gives_is_one_based(archive, source) -> None:
    """``--map 2`` on ottelun toinen kartta eli ``map_index`` 1."""
    place(archive, f"{MATCH}-2-1.dem.zst")
    parser = FakeMapNameParser({f"{MATCH}-2-1.dem.zst": "de_nuke"})

    todo, result = do_import(archive, source, parser, map_no=2)

    assert todo.map_index == 1
    assert result.unit == UNIT_TWO
    assert (archive.demos_dir() / f"{UNIT_TWO}.dem.zst").is_file()


def test_the_source_file_the_header_was_read_from_is_the_one_that_moved(
    archive, source, parser
) -> None:
    """Otsikko luettiin **siitä** tiedostosta, joka siirrettiin."""
    origin = place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser)

    assert parser.asked == [origin]
    assert todo.source_path == origin


# -- A1: vajaa demo ei pääse arkistoon --------------------------------------


def test_a_truncated_archive_is_refused_and_the_source_survives(
    archive, source, parser
) -> None:
    """**Vakavin mahdollinen vika, ja se on mitattu oikealla demolla.**

    Puoliväliin katkaistu ``.dem.zst`` purkautuu hiljaa vajaaksi, ja purettu
    alku on kelvollinen CS2-demo -- mitattu 2026-09-05:
    ``ANCIENT_vs_RCAVE_VETERANS.dem.zst`` antoi oikean päätteen ja oikean
    kartan nimen ``de_ancient`` 148 871 905 tavun tiedostosta katkaistuna
    puoleen. Ilman vartijaa ketju sanoisi "Karttatarkistus täsmää", ei kysyisi
    mitään, kirjoittaisi ``length_verified: true`` ja **poistaisi lähteen**.

    ``import/``issa on kuusi kauden 12 liigademoa, joita FACEIT ei enää
    tarjoa. Laukaisin on arkinen: Explorer kirjoittaa lopullisella nimellä
    kesken kopioinnin, tai OneDrive synkronoi yhä.
    """
    origin = place(archive, FACEIT_NAME, TRUNCATED_ZSTD)
    parser.errors[FACEIT_NAME] = ParseError(
        "Demon purku jäi vajaaksi: tiedosto ilmoittaa purettuna 4096 tavua, "
        "mutta purkautui 2048 tavuksi.",
        advice="Odota kunnes kopiointi on valmis ja aja komento uudelleen.",
    )

    with pytest.raises(ParseError) as err:
        make_plan(archive, source, parser)

    assert "vajaaksi" in str(err.value)
    assert "lähdetiedostoon koskettu" in str(err.value)
    assert origin.read_bytes() == TRUNCATED_ZSTD
    assert archive.find_demo(UNIT) is None


def test_the_advice_of_a_truncated_archive_survives_the_wrapping(
    archive, source, parser
) -> None:
    """Vajaan demon oma neuvo on **odota**, ei "tarkista että se on demo".

    Yleinen neuvo lähettäisi käyttäjän etsimään vikaa tiedostosta, joka on
    kunnossa ja jonka kopiointi on vain kesken. Neuvo kuuluu vikaan, ja
    tarkin neuvo tulee siitä kerroksesta, joka tietää mikä meni pieleen.
    """
    place(archive, FACEIT_NAME, TRUNCATED_ZSTD)
    parser.errors[FACEIT_NAME] = ParseError(
        "purku jäi vajaaksi", advice="Odota kunnes kopiointi on valmis."
    )

    with pytest.raises(ParseError) as err:
        make_plan(archive, source, parser)

    assert err.value.advice == "Odota kunnes kopiointi on valmis."


def test_a_compressed_source_is_length_verified(archive, source, parser) -> None:
    """zstd-kehys ilmoittaa puretun koon, joten kokonaisuus on todettavissa."""
    place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser)

    assert todo.declared_bytes == len(PLAIN_BYTES)
    assert todo.length_verified is True
    assert read_meta(todo.meta_path)["length_verified"] is True


def test_a_gzip_source_is_length_verified_by_its_end_marker(
    archive, source
) -> None:
    """Eri mekanismi, sama lupaus: katkennut gzip kaatuu purussa."""
    name = f"{MATCH}-1-1.dem"
    place(archive, name, GZIP_BYTES)
    parser = FakeMapNameParser({name: "de_ancient"})

    todo, _result = do_import(archive, source, parser)

    assert todo.declared_bytes is None
    assert todo.length_verified is True


def test_an_uncompressed_source_is_not_length_verified(archive, source) -> None:
    """**Pakkaamattomassa demossa ei ole pituustietoa, ja se sanotaan.**

    Aiemmin ``length_verified`` kirjoitettiin todeksi aina, ja se oli väärä
    väite: ``.dem``-tiedostossa ei ole kehyksen kokoa, tarkistetta eikä
    lopetusmerkkiä, joten puolikas tiedosto on erottamaton kokonaisesta.
    ``fetch`` kirjoittaa saman kentän epätodeksi kun ``Content-Length``iä ei
    ollut; tuonnissa on sama tilanne ja sama vastaus.
    """
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})

    todo, result = do_import(archive, source, parser)

    assert todo.length_verified is False
    assert read_meta(todo.meta_path)["length_verified"] is False
    # Ja epävarmuus sanotaan ääneen eikä vaieta.
    assert "ei voitu todeta" in str(result.reason)


def test_a_source_that_grows_during_the_transfer_is_refused(
    archive, source, parser, monkeypatch
) -> None:
    """**Kasvava tiedosto on arkinen tilanne, ei poikkeus.**

    Explorer kirjoittaa lopullisella nimellä kesken kopioinnin ja OneDrive
    synkronoi taustalla. Ilman kokotarkistusta molemmissa päissä kopioitaisiin
    puolikas ja poistettaisiin ehjä lähde -- ja lähde on korvaamaton.
    """
    origin = place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)
    # Tiedosto kasvaa suunnitelman ja siirron välissä.
    origin.write_bytes(ZSTD_BYTES + b"lisaa dataa")

    with pytest.raises(PappascoutError) as err:
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert "muuttui kesken siirron" in str(err.value)
    assert err.value.advice
    assert origin.exists()
    assert not todo.target_path.exists()
    assert not todo.meta_path.exists()


def test_the_source_is_removed_only_after_the_target_is_in_place(
    archive, source, parser
) -> None:
    """Lähdetiedostoa ei tuhota ennen kuin kohde ja meta ovat paikallaan."""
    origin = place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser)

    assert not origin.exists()
    assert todo.target_path.is_file()
    assert todo.meta_path.is_file()


# -- Matriisi: karttanimi ei täsmää vetotietoon ------------------------------


def test_a_map_that_does_not_match_the_veto_is_a_forced_question(
    archive, source
) -> None:
    """Poikkeama kysytään, eikä sitä voi ohittaa lipulla."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    todo = make_plan(archive, source, parser)

    assert len(todo.confirmations) == 1
    kysymys = todo.confirmations[0]
    assert kysymys.forced is True
    assert "de_nuke" in kysymys.detail
    assert "de_ancient" in kysymys.detail
    assert import_stage.unanswered(todo.confirmations, kylla=True) == (kysymys,)


def test_a_mismatch_names_the_map_number_that_would_be_right(
    archive, source
) -> None:
    """**Kysymys ilman vastausvaihtoehtoa on huono kysymys.**

    Kun otsikon kartta on vedossa toisella numerolla, se numero on juuri se,
    jonka käyttäjä tarvitsee. Ilman sitä hänellä ei ole yhtään oikeaa arvoa
    -- ja houkutus vastata "k" on suuri, koska kysymys ei tarjoa muuta tietä
    eteenpäin.
    """
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    todo = make_plan(archive, source, parser)

    assert "--map 2" in todo.confirmations[0].detail


def test_a_map_that_is_in_no_pick_says_so(archive, source) -> None:
    """Kartta, jota ottelussa ei pelattu, on eri vika kuin väärä numero."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_mirage"})

    todo = make_plan(archive, source, parser)

    assert "eri ottelusta" in todo.confirmations[0].detail


def test_the_plan_alone_moves_nothing(archive, source) -> None:
    """Suunnitelman rakentaminen ei kirjoita mitään."""
    origin = place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    make_plan(archive, source, parser)

    assert origin.read_bytes() == ZSTD_BYTES
    assert archive.find_demo(UNIT) is None


def test_a_confirmed_mismatch_is_written_into_the_result(archive, source) -> None:
    """Vahvistettu poikkeama ei katoa vastauksen myötä."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    _todo, result = do_import(archive, source, parser)

    assert "de_nuke" in str(result.reason)
    assert "de_ancient" in str(result.reason)
    assert result.stats["map_matches"] is False


# -- Matriisi: ottelulla ei vetotietoa ---------------------------------------


def test_a_match_without_veto_data_cannot_be_cross_checked_so_it_asks(
    archive,
) -> None:
    """Tyhjä ``map_picks`` on "ei vetotietoa", ei "kartta täsmää"."""
    place(archive, FACEIT_NAME)
    source = FakeMatchSource({MATCH: match(picks=())})
    parser = FakeMapNameParser({FACEIT_NAME: "de_ancient"})

    todo = make_plan(archive, source, parser)

    assert todo.expected_map_name is None
    assert len(todo.confirmations) == 1
    assert todo.confirmations[0].forced is True
    assert "vetotieto" in todo.confirmations[0].detail
    assert import_stage.unanswered(todo.confirmations, kylla=True) != ()


def test_a_demo_without_a_map_name_in_its_header_also_asks(
    archive, source
) -> None:
    """Otsikon puuttuva nimi on sama lopputulos, eri syy."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: None})

    todo = make_plan(archive, source, parser)

    assert todo.header_map_name is None
    assert len(todo.confirmations) == 1
    assert todo.confirmations[0].forced is True


def test_a_map_outside_the_pool_is_kept_as_observed(archive) -> None:
    """Workshop-versio tai ``de_train`` on aito havainto eikä virhe."""
    place(archive, FACEIT_NAME)
    source = FakeMatchSource({MATCH: match(picks=("de_train",))})
    parser = FakeMapNameParser({FACEIT_NAME: "de_train"})

    todo = make_plan(archive, source, parser)

    assert todo.header_map_name == "de_train"
    assert todo.confirmations == ()


# -- Matriisi: pakkaamaton .dem ----------------------------------------------


def test_an_uncompressed_demo_keeps_the_dem_suffix(archive, source) -> None:
    """Pääte tulee taikatavuista, ei annetusta nimestä."""
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})

    _todo, result = do_import(archive, source, parser)

    assert (archive.demos_dir() / f"{UNIT}.dem").read_bytes() == PLAIN_BYTES
    assert not (archive.demos_dir() / f"{UNIT}.dem.zst").exists()
    assert result.stats["demo_path"].endswith(f"{UNIT}.dem")


def test_a_zst_named_file_that_is_not_compressed_gets_the_dem_suffix(
    archive, source
) -> None:
    """**Nimi valehtelee, sisältö ei.**"""
    name = f"{MATCH}-1-1.dem.zst"
    place(archive, name, PLAIN_BYTES)
    parser = FakeMapNameParser({name: "de_ancient"})

    do_import(archive, source, parser)

    assert (archive.demos_dir() / f"{UNIT}.dem").is_file()
    assert not (archive.demos_dir() / f"{UNIT}.dem.zst").exists()


def test_a_gzip_demo_gets_the_gz_suffix(archive, source) -> None:
    """Kolmas tunnettu muoto: käsin tuotujen varamuoto ``.dem.gz``."""
    name = f"{MATCH}-1-1.dem"
    place(archive, name, GZIP_BYTES)
    parser = FakeMapNameParser({name: "de_ancient"})

    do_import(archive, source, parser)

    assert (archive.demos_dir() / f"{UNIT}.dem.gz").is_file()


# -- Matriisi: tiedosto ei ole demo lainkaan ---------------------------------


@pytest.mark.parametrize(
    "data", [HTML_PAGE, b""], ids=["html", "tyhja"]
)
def test_a_file_that_is_not_a_demo_is_refused_and_nothing_moves(
    archive, source, parser, data: bytes
) -> None:
    """Roska torjutaan **ennen kuin mitään on siirretty**."""
    origin = place(archive, FACEIT_NAME, data)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    assert "ei ole demo" in str(err.value)
    assert origin.exists()
    assert archive.find_demo(UNIT) is None


def test_a_zstd_file_that_is_not_a_demo_inside_is_refused(archive, source) -> None:
    """Oikeat alkutavut eivät riitä: otsikko luetaan puretusta sisällöstä."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser()  # tuntematon tiedosto -> ParseError

    with pytest.raises(ParseError) as err:
        make_plan(archive, source, parser)

    assert "Mitään ei siirretty" in str(err.value)
    assert err.value.advice
    assert archive.find_demo(UNIT) is None


# -- Matriisi: kohde on jo arkistossa ----------------------------------------


def test_an_existing_target_is_a_question_that_kylla_may_skip(
    archive, source, parser
) -> None:
    """Ylikirjoitus kysytään, mutta ``--kylla`` saa ohittaa sen.

    Rivi on jäädytetyssä I/O-matriisissa. Perustelu ei ole "haettavissa
    uudelleen" -- Downloads-oikeutta ei ole -- vaan se, että korvattava on
    **saman yksikön** demo samassa hakemistossa ja korvaava sisältö on
    tarkistettu ehjäksi ennen siirtoa.
    """
    place(archive, FACEIT_NAME)
    existing = archive.demos_dir() / f"{UNIT}.dem.zst"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"vanha sisalto")

    todo = make_plan(archive, source, parser)

    assert len(todo.confirmations) == 1
    assert todo.confirmations[0].forced is False
    assert str(existing) in todo.confirmations[0].detail
    assert import_stage.unanswered(todo.confirmations, kylla=True) == ()
    assert import_stage.unanswered(todo.confirmations, kylla=False) != ()
    assert existing.read_bytes() == b"vanha sisalto"


def test_replacing_a_demo_with_another_suffix_removes_the_old_file(
    archive, source
) -> None:
    """Kaksi tiedostoa samalla tunnisteella ei saa jäädä arkistoon."""
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    old = archive.demos_dir() / f"{UNIT}.dem.zst"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})

    _todo, result = do_import(archive, source, parser)

    assert not old.exists()
    assert (archive.demos_dir() / f"{UNIT}.dem").read_bytes() == PLAIN_BYTES
    assert old.name in str(result.reason)


def test_a_failed_removal_warns_instead_of_claiming_success(
    archive, source, monkeypatch
) -> None:
    """**Huomio ei saa kertoa poistosta, jota ei tehty.**

    ``_remove``in oma dokumentaatio sanoo epäonnistumisen olevan Windowsilla
    tavallista (OneDriven tiedostolukko, virustorjunnan avoin kahva). Jos
    paluuarvo heitettäisiin menemään, arkistoon jäisi kaksi tiedostoa samalle
    tunnisteelle, ``find_demo`` palauttaisi niistä ``DEMO_SUFFIXES``-
    järjestyksessä **vanhan**, ja ``parse`` lukisi tiivisteen metasta
    tarkistamatta sitä tiedostoa vasten -- eli raportti syntyisi väärästä
    demosta, hiljaa.
    """
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    old = archive.demos_dir() / f"{UNIT}.dem.zst"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})
    todo = make_plan(archive, source, parser)

    def refuse(path: Path) -> bool:
        return False if path == todo.replaces else True

    monkeypatch.setattr(import_stage, "_remove", refuse)

    result = import_stage.run(archive, todo, now=lambda: CLOCK)

    varoitus = str(result.reason).split("VAROITUS")[1]
    assert "kaksi tiedostoa" in varoitus
    # Molemmat tiedostot nimeltä, jotta käyttäjä tietää kumman poistaa.
    assert old.name in varoitus
    assert todo.target_path.name in varoitus
    # Ja vanha on yhä paikallaan -- huomio ei väitä muuta.
    assert old.is_file()


# -- A3: paikallinen demohakemisto ------------------------------------------


def test_the_import_writes_where_the_demo_already_is(
    local_archive, source, parser
) -> None:
    """**Kirjoitus menee sinne, missä demo jo on** -- kuten ``fetch``issä.

    Jos kohde valittaisiin aina ``demos_dir()``in mukaan, arkistossa
    (OneDrive, jaettu) oleva demo poistettaisiin "korvattuna", vaikka kyse on
    eri hakemistosta eikä eri tiedostosta -- ja ``--kylla`` ohittaisi
    kysymyksen. Perustelu "haettavissa uudelleen" ei päde: Downloads-oikeutta
    ei ole, ja juuri siksi tämä komento on olemassa.
    """
    place(local_archive, FACEIT_NAME)
    arkistossa = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    arkistossa.parent.mkdir(parents=True, exist_ok=True)
    arkistossa.write_bytes(b"vanha arkistodemo")

    todo, _result = do_import(local_archive, source, parser)

    # Uusi sisältö meni arkistoon, koska demo oli siellä.
    assert todo.target_path == arkistossa
    assert arkistossa.read_bytes() == ZSTD_BYTES
    assert todo.replaces is None
    # Paikalliseen hakemistoon ei syntynyt toista kopiota.
    assert not (local_archive.demos_root / f"{UNIT}.dem.zst").exists()


def test_the_meta_goes_beside_the_demo_not_into_the_write_directory(
    local_archive, source, parser
) -> None:
    """Metatiedosto on väite juuri siitä tiedostosta, ja se seuraa sitä."""
    place(local_archive, FACEIT_NAME)
    arkistossa = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    arkistossa.parent.mkdir(parents=True, exist_ok=True)
    arkistossa.write_bytes(b"vanha")

    todo, _result = do_import(local_archive, source, parser)

    assert todo.meta_path.parent == arkistossa.parent
    assert not (local_archive.demos_root / f"{UNIT}.meta.json").exists()


def test_an_orphan_meta_in_another_directory_is_removed(
    local_archive, source, parser
) -> None:
    """Orpo metatiedosto väittäisi tiivisteen tiedostosta, jota ei ole.

    ``parse`` lukee tiivisteen **ensimmäisestä löytyneestä** metatiedostosta,
    joten väärä meta väärässä hakemistossa tekisi tuoreesta demosta
    ajantasaisen vanhan demon tulokselle. ``fetch`` poistaa orvon; tuonti
    tekee nyt saman.
    """
    place(local_archive, FACEIT_NAME)
    orpo = local_archive.demos_root / f"{UNIT}.meta.json"
    orpo.parent.mkdir(parents=True, exist_ok=True)
    orpo.write_text(json.dumps({"sha256": "vanha"}), encoding="utf-8")
    arkistossa = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    arkistossa.parent.mkdir(parents=True, exist_ok=True)
    arkistossa.write_bytes(b"vanha")

    todo, result = do_import(local_archive, source, parser)

    assert todo.orphan_meta == orpo
    assert not orpo.exists()
    assert "poistettiin" in str(result.reason)
    assert read_meta(todo.meta_path)["sha256"] != "vanha"


# -- A4: import/ ei ole kohde eikä korvattava --------------------------------


def test_a_file_in_the_import_folder_is_never_treated_as_the_target(
    archive, source, tmp_path
) -> None:
    """**Tuonti ei poista ``import/``ista mitään muuta kuin lähteensä.**

    ``ArchivePaths.find_demo`` käy myös ``import/``in läpi -- se on
    hakujärjestys ``parse``a varten. Tuonnille se on eri asia: siellä oleva
    samanniminen tiedosto ei ole korvattava versio vaan toinen tiedosto, jota
    tuonti ei omista. Vanha koodi olisi poistanut sen ja rikkonut speksin
    Never-sääntöä "ei kirjoiteta muualle kuin demos/iin".
    """
    kanoninen = archive.import_dir()
    kanoninen.mkdir(parents=True, exist_ok=True)
    toinen = kanoninen / f"{UNIT}.dem.zst"
    toinen.write_bytes(b"toisen demon tavut")

    ulkoa = tmp_path / "lataukset" / "oma.dem.zst"
    ulkoa.parent.mkdir(parents=True)
    ulkoa.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    todo, _result = do_import(archive, source, parser, file=ulkoa)

    assert todo.replaces is None
    assert todo.target_path.parent == archive.demos_dir()
    assert toinen.read_bytes() == b"toisen demon tavut"


def test_an_import_folder_file_is_not_a_reason_to_ask_about_overwriting(
    archive, source, tmp_path
) -> None:
    """``import/``in tiedosto ei ole "kohde on jo arkistossa" -tilanne."""
    kanoninen = archive.import_dir()
    kanoninen.mkdir(parents=True, exist_ok=True)
    (kanoninen / f"{UNIT}.dem.zst").write_bytes(b"toisen demon tavut")

    ulkoa = tmp_path / "oma.dem.zst"
    ulkoa.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    todo = make_plan(archive, source, parser, file=ulkoa)

    assert todo.confirmations == ()


# -- Matriisi: kartan numero -------------------------------------------------


@pytest.mark.parametrize("map_no", [0, -1])
def test_a_map_number_below_one_is_refused_with_the_numbering_rule(
    archive, source, parser, map_no: int
) -> None:
    """Nolla on tyypillisin virhe, ja se saa oman lauseensa."""
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no=map_no)

    assert "alkaa ykk" in str(err.value)
    assert err.value.advice
    assert source.asked == []


def test_a_map_number_that_is_not_a_number_is_refused_in_finnish(
    archive, source, parser
) -> None:
    """**Väärä arvo saa suomenkielisen virheen eikä kirjaston omaa.**

    Aiemmin komentorivi julisti ``--map``in kokonaisluvuksi, jolloin ``typer``
    kaatui englanninkieliseen viestiin ennen kuin vaihe näki mitään -- ja
    vaiheen oma tarkistus oli kuollutta koodia, saavuttamattomissa
    komentoriviltä. Nyt arvo tulee merkkijonona ja muunnos on siellä missä
    numeron muutkin säännöt.
    """
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no="abc")

    assert "ei ole kokonaisluku" in str(err.value)
    assert err.value.advice


def test_a_map_number_above_the_maximum_is_refused(
    archive, source, parser
) -> None:
    """**Ilman vetotietoa tämä on ainoa raja, joka numerolla on.**

    Tuleva ottelu ei kanna ``map_picks``ia, joten ``--map 99`` olisi muuten
    kelvollinen ja arkistoon syntyisi ``{match_id}-98`` -- tunniste, jota
    mikään vaihe ei osaa liittää mihinkään.
    """
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no=import_stage.MAX_MAP_NO + 1)

    assert str(import_stage.MAX_MAP_NO) in str(err.value)
    assert err.value.advice
    assert source.asked == []


def test_a_match_without_veto_still_cannot_take_any_number(archive) -> None:
    """Sama raja pätee myös silloin, kun vetotietoa ei ole tarkistamassa."""
    source = FakeMatchSource({MATCH: match(picks=())})
    parser = FakeMapNameParser({FACEIT_NAME: "de_ancient"})
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError):
        make_plan(archive, source, parser, map_no=99)


def test_a_map_number_beyond_the_veto_says_how_many_maps_there_are(
    archive, source, parser
) -> None:
    """BO2:ssa ei ole kolmatta karttaa, ja luettelo kertoo mitkä ovat."""
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no=3)

    message = str(err.value)
    assert "2 karttaa" in message
    assert "de_ancient" in message and "de_nuke" in message
    assert err.value.advice


# -- B3: tunniste rakennetaan domainin rakentajalla --------------------------


def test_the_identifier_is_built_by_the_domain_builder(archive) -> None:
    """``domain.selection.map_demo_id`` on tunnisteen kanoninen rakentaja.

    Tuonti oli katselmuksessa projektin ainoa kohta, joka rakensi tunnisteen
    ohi sen. Väite on kutsusta: rinnakkainen ``f"{match}-{index}"`` ohittaisi
    domainin oman tarkistuksen eikä näkyisi missään.
    """
    import pappascout.stages.import_demo as moduuli

    lahde = Path(moduuli.__file__).read_text(encoding="utf-8")
    puu = ast.parse(lahde)
    kutsut = {
        node.func.id
        for node in ast.walk(puu)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_map_demo_id" in kutsut


def test_a_match_id_too_long_for_a_path_is_refused_with_advice(
    archive, source, parser
) -> None:
    """**Torjunta ilman neuvoa on aidosti saavutettava polku, ja se korjattiin.**

    119-merkkinen ``--match`` läpäisee ottelutunnisteen oman tarkistuksen
    (raja on 120) mutta ylittää sen tunnistetta rakennettaessa, koska
    ``-0`` tulee perään. ``archive.paths`` nostaa oman virheensä ilman
    neuvoa, ja viesti nimeää tunnisteen ``map_demo_id`` -- jota käyttäjä ei
    antanut.
    """
    pitka = "1" * 119

    with pytest.raises(PappascoutError) as err:
        import_stage.plan(
            archive,
            pitka,
            1,
            source=source,
            parser=parser,
            disk_free=lambda _p: ROOMY,
        )

    assert err.value.advice, "torjunta ilman neuvoa"
    assert "--match" in err.value.advice


# -- Matriisi: tuntematon ottelu ---------------------------------------------


def test_an_unknown_match_is_refused_and_names_the_index(
    archive, source, parser
) -> None:
    """Neuvo nimeää sen tiedoston, josta tunnisteet löytyvät."""
    place(archive, FACEIT_NAME)
    tuntematon = "1-00000000-0000-0000-0000-000000000000"

    with pytest.raises(PappascoutError) as err:
        import_stage.plan(
            archive,
            tuntematon,
            1,
            source=source,
            parser=parser,
            disk_free=lambda _p: ROOMY,
        )

    assert "index/matches.json" in str(err.value.advice)
    assert "discover" in str(err.value.advice)
    assert archive.find_demo(f"{tuntematon}-0") is None


# -- Matriisi: lähdetiedostoa ei löydy ---------------------------------------


def test_a_missing_source_file_lists_what_the_import_folder_holds(
    archive, source, parser
) -> None:
    """Puuttuva tiedosto on lähes aina väärä nimi tai väärä kansio."""
    place(archive, "Ancient_vs_kaljukostaja.dem", PLAIN_BYTES)
    place(archive, "muistiinpanot.txt", b"ei demo")

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    message = str(err.value)
    assert "Ancient_vs_kaljukostaja.dem" in message
    assert "muistiinpanot.txt" not in message
    assert "--file" in str(err.value.advice)


def test_a_missing_import_folder_is_said_out_loud(archive, source, parser) -> None:
    """Olematon kansio on eri asia kuin tyhjä kansio."""
    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    assert "ei ole" in str(err.value)


# -- Matriisi: kaksi kandidaattia --------------------------------------------


def test_two_candidates_are_listed_and_nothing_is_chosen_silently(
    archive, source, parser
) -> None:
    """Mitattu 2026-09-05: yksi ottelu on kansiossa kahtena eri päätteenä."""
    place(archive, FACEIT_NAME, ZSTD_BYTES)
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    message = str(err.value)
    assert FACEIT_NAME in message
    assert FACEIT_NAME_PLAIN in message
    assert "--file" in str(err.value.advice)
    assert archive.find_demo(UNIT) is None


def test_the_ambiguity_advice_points_at_the_compressed_candidate(
    archive, source, parser
) -> None:
    """**Neuvo ohjaa pakattuun, ei aakkosjärjestyksen ensimmäiseen.**

    ``.dem`` < ``.dem.zst``, joten aakkosjärjestys nimeäisi aina
    pakkaamattoman -- mitattu 2026-09-05: sama ottelu on 233 MB
    pakkaamattomana ja 169 MB pakattuna, ja ero jää OneDriveen pysyvästi.
    """
    place(archive, FACEIT_NAME, ZSTD_BYTES)
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    assert FACEIT_NAME in err.value.advice
    assert f'"{archive.import_dir() / FACEIT_NAME}"' in err.value.advice


def test_the_ambiguity_advice_quotes_a_path_with_spaces(
    tmp_path, source, parser
) -> None:
    """**Neuvo, jota ei voi kopioida, ei ole neuvo.**

    Oikean arkiston polussa on kolme välilyöntiä (``Claude code``,
    ``Finnpark Oy``), joten lainaamaton polku hajoaa komentotulkissa useaksi
    argumentiksi ja tuottaa englanninkielisen ``typer``-virheen. Tämä tulisi
    tapahtumaan ensimmäisellä oikealla ajolla, koska monitulkintainen pari on
    mitattu olemassa olevaksi.
    """
    archive = ArchivePaths(root=tmp_path / "Claude code" / "arkisto")
    place(archive, FACEIT_NAME, ZSTD_BYTES)
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    neuvo = err.value.advice
    polku = str(archive.import_dir() / FACEIT_NAME)
    assert " " in polku, "testi ei mittaa mitään ilman välilyöntiä polussa"
    assert f'--file "{polku}"' in neuvo


def test_the_search_pattern_does_not_confuse_map_1_with_map_10(
    archive, source
) -> None:
    """``{match_id}-1-*`` ei saa osua tiedostoon ``{match_id}-10-1.dem``."""
    place(archive, FACEIT_NAME)
    place(archive, f"{MATCH}-10-1.dem.zst")

    assert import_stage.candidates(archive, MATCH, 0) == (
        archive.import_dir() / FACEIT_NAME,
    )


# -- Matriisi: --file tuontikansion ulkopuolelta -----------------------------


def test_a_file_outside_the_import_folder_is_copied_not_moved(
    archive, source, tmp_path
) -> None:
    """Käyttäjän oma tiedosto omassa paikassaan jää paikalleen."""
    outside = tmp_path / "lataukset" / "oma.dem.zst"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    todo, result = do_import(archive, source, parser, file=outside)

    assert todo.move is False
    assert outside.read_bytes() == ZSTD_BYTES
    assert (archive.demos_dir() / f"{UNIT}.dem.zst").read_bytes() == ZSTD_BYTES
    assert "kopioitiin" in str(result.reason)
    # Otsikko luettiin juuri siitä tiedostosta, joka annettiin.
    assert parser.asked == [outside]


def test_a_file_inside_the_import_folder_is_moved_even_when_named(
    archive, source, parser
) -> None:
    """``--file`` ei muuta sitä, että tuontikansio on saapuvien kansio."""
    origin = place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser, file=origin)

    assert todo.move is True
    assert not origin.exists()


def test_a_named_file_that_does_not_exist_is_refused(archive, source, parser) -> None:
    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, file=archive.root / "ei-ole.dem")

    assert "ei ole" in str(err.value)
    assert "--file" in str(err.value.advice)


# -- B7: levytila ja levyvirheet ---------------------------------------------


def test_a_full_target_disk_stops_the_import_before_it_starts(
    archive, source, parser
) -> None:
    """Täysi levy on käyttäjän tilanne, ei ohjelmavirhe."""
    origin = place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, disk_free=lambda _p: 1024)

    assert "Levytila ei riitä" in str(err.value)
    assert "ei koskettu" in str(err.value)
    assert err.value.advice
    assert origin.exists()


def test_a_full_temp_disk_stops_the_import_before_the_header_is_read(
    archive, source, parser
) -> None:
    """**Tuonti tarvitsee tilaa kahdesti, ja vain toinen on ilmeinen.**

    Otsikon luku purkaa pakatun demon kokonaan koneen temp-hakemistoon
    (208-316 MB oikeilla demoilla). Jos TEMP täyttyy, purun oma neuvo on
    "lataa demo uudelleen" -- väärä toimenpide ja kehotus hakemaan 230 MB
    joka on kunnossa.
    """
    import tempfile

    place(archive, FACEIT_NAME)
    temp_root = Path(tempfile.gettempdir())

    def free(path: Path) -> int:
        return 1024 if path == temp_root else ROOMY

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, disk_free=free)

    assert "otsikon lukemiseen" in str(err.value)
    assert str(temp_root) in str(err.value)
    assert err.value.advice
    # Otsikkoa ei edes yritetty lukea.
    assert parser.asked == []


def test_a_disk_error_during_the_transfer_is_finnish_with_advice(
    archive, source, parser, monkeypatch
) -> None:
    """``OSError`` ei saa karata ruudulle "ohjelmavirheenä".

    Täysi levy, OneDriven tiedostolukko ja irronnut verkkolevy ovat kaikki
    käyttäjän tilanteita. Ilman käännöstä ruudulla lukisi
    "Odottamaton virhe: [Errno 28]" ja neuvona "Tämä on ohjelmavirhe" -- eli
    väärä diagnoosi ja väärä toimenpide.
    """
    origin = place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(import_stage, "_transfer", boom)

    with pytest.raises(PappascoutError) as err:
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert "levyvirheeseen" in str(err.value)
    assert err.value.advice
    assert "ei koskettu" in str(err.value)
    assert origin.exists()


# -- Kirjoitusjärjestys ja atomisuus -----------------------------------------


def test_the_meta_file_is_written_only_after_the_demo(
    archive, source, parser, monkeypatch
) -> None:
    """Metatiedosto ei saa koskaan kuvata tiedostoa, jota ei ole."""
    place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)

    def boom(*args, **kwargs):
        raise OSError("levy täynnä")

    monkeypatch.setattr(import_stage, "_transfer", boom)

    with pytest.raises(PappascoutError):
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert not todo.meta_path.exists()
    assert not todo.target_path.exists()


def test_the_source_file_survives_a_failed_transfer(
    archive, source, parser, monkeypatch
) -> None:
    """Lähdetiedostoa ei tuhota ennen kuin kohde on paikallaan."""
    origin = place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)
    monkeypatch.setattr(
        import_stage, "_transfer", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )

    with pytest.raises(PappascoutError):
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert origin.read_bytes() == ZSTD_BYTES


def test_the_digest_is_the_digest_of_what_was_written(
    archive, source, parser
) -> None:
    """Tiiviste lasketaan siirron aikana, ja se koskee kohdetiedostoa."""
    place(archive, FACEIT_NAME)

    _todo, result = do_import(archive, source, parser)

    target = archive.demos_dir() / f"{UNIT}.dem.zst"
    assert result.stats["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_no_temporary_files_are_left_behind(archive, source, parser) -> None:
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    assert not has_temp_leftovers(archive.root)


def test_nothing_is_written_outside_the_demo_directory(
    archive, source, parser
) -> None:
    """Tuonti kirjoittaa vain demohakemistoon."""
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    written = sorted(
        p.relative_to(archive.root).as_posix()
        for p in archive.root.rglob("*")
        if p.is_file()
    )
    assert written == [f"demos/{UNIT}.dem.zst", f"demos/{UNIT}.meta.json"]


def test_nothing_else_in_the_import_folder_is_touched(
    archive, source, parser
) -> None:
    """**Poistot ovat kirjoituksia siinä missä muutkin.**

    Edellinen testi katsoo tiedostoja, jotka syntyivät; tämä katsoo niitä,
    jotka olivat jo olemassa. Ilman sitä ``import/``ista poistaminen ei
    näkyisi missään.
    """
    place(archive, FACEIT_NAME)
    naapuri = place(archive, "Ancient_vs_kaljukostaja.dem", PLAIN_BYTES)
    muistiinpano = place(archive, "LUE-MINUT.txt", b"tarkeaa")

    do_import(archive, source, parser)

    assert naapuri.read_bytes() == PLAIN_BYTES
    assert muistiinpano.read_bytes() == b"tarkeaa"


def test_the_local_demo_directory_is_honoured(local_archive, source, parser) -> None:
    """``[project].demos_root`` ohjaa tuonnin samoin kuin latauksen."""
    place(local_archive, FACEIT_NAME)

    _todo, result = do_import(local_archive, source, parser)

    assert (local_archive.demos_root / f"{UNIT}.dem.zst").is_file()
    assert (local_archive.demos_root / f"{UNIT}.meta.json").is_file()
    assert result.outputs == ()
    assert result.stats["demo_path"].startswith(str(local_archive.demos_root))


# -- Tuotu demo on erottamaton ladatusta -------------------------------------


def test_the_only_difference_to_a_fetched_demo_is_the_source_field(
    tmp_path, source, parser
) -> None:
    """Sama tiedostonimi, sama hakemisto, sama metatiedoston muoto."""
    from test_stage_fetch import FakeDemo, FakeDemoSource
    from pappascout.stages import fetch as fetch_stage

    ladattu = ArchivePaths(root=tmp_path / "ladattu")
    tuotu = ArchivePaths(root=tmp_path / "tuotu")
    # **Sama tavujono molemmille.** Eri sisällöllä testi vertaisi kahta eri
    # demoa, eikä sha256 ja size voisi olla vertailussa mukana lainkaan.
    demo_bytes = ZSTD_BYTES + bytes(1024 * 1024)

    fetch_stage.run(
        ladattu,
        UNIT,
        source=FakeDemoSource({UNIT: FakeDemo(demo_bytes)}),
        disk_free=lambda _a: ROOMY,
        now=lambda: CLOCK,
    )

    path = tuotu.import_dir()
    path.mkdir(parents=True)
    (path / FACEIT_NAME).write_bytes(demo_bytes)
    todo = import_stage.plan(
        tuotu,
        MATCH,
        1,
        source=source,
        parser=FakeMapNameParser({FACEIT_NAME: "de_ancient"}),
        disk_free=lambda _p: ROOMY,
    )
    import_stage.run(tuotu, todo, now=lambda: CLOCK)

    a = read_meta(ladattu.demos_dir() / f"{UNIT}.meta.json")
    b = read_meta(tuotu.demos_dir() / f"{UNIT}.meta.json")
    assert set(a) == set(b)
    assert {k: v for k, v in a.items() if k != "source"} == {
        k: v for k, v in b.items() if k != "source"
    }
    assert (a["source"], b["source"]) == ("downloads_api", "import")
    assert (ladattu.demos_dir() / f"{UNIT}.dem.zst").read_bytes() == (
        tuotu.demos_dir() / f"{UNIT}.dem.zst"
    ).read_bytes()


def test_no_module_branches_on_the_source_field() -> None:
    """``source`` on jäljitettävyystietoa, ei ohjausta.

    **Väite luetaan syntaksipuusta eikä merkkijonoista.** Katselmus todisti
    merkkijononeulojen kiertämisen: yksinlainattu ``meta.get('source')`` lisäsi
    aidon haaran ``parse``en ja 2779 testiä meni läpi. Lainausmerkkien tyyli ei
    ole sääntö vaan muotoseikka, ja sääntö on "kenttää ei lueta" -- joten
    tarkistus katsoo, esiintyykö vakio ``"source"`` **hakuavaimena**:
    ``x["source"]`` tai ``x.get("source", ...)``. Molemmat lainaustyylit
    tuottavat saman ``ast.Constant``in, joten kierto ei ole mahdollinen.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "pappascout"
    lukijat: list[str] = []
    for path in sorted(src.rglob("*.py")):
        puu = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(puu):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "source"
            ):
                lukijat.append(f"{path.name}:{node.lineno} alaindeksi")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "source"
            ):
                lukijat.append(f"{path.name}:{node.lineno} .get")
    assert lukijat == [], (
        "Joku lukee metatiedoston source-kenttää: tuodun ja ladatun demon ero "
        f"on jäljitettävyystietoa eikä ohjausta. {lukijat}"
    )


# -- Portit ------------------------------------------------------------------


def test_the_fakes_satisfy_the_ports(source, parser) -> None:
    """Feikki ei saa olla löysempi kuin portti, tai testit mittaisivat väärää."""
    assert isinstance(source, MatchSource)
    assert isinstance(parser, DemoParser)


def test_default_source_really_builds_a_port(
    settings_file, env_file, tmp_path, monkeypatch
) -> None:
    """``default_source`` ajetaan oikeasti -- **verkkoon menemättä**.

    Se on vaiheen ainoa rivi, joka liittää sen FACEITiin, ja jokainen muu
    testi korvaa sen feikillä. Ilman tätä testiä sen runko ei suoritu koko
    sarjassa kertaakaan: katselmus korvasi sen ``raise RuntimeError``illa
    eikä yksikään testi kaatunut. Sama aukko on löytynyt tässä projektissa
    kolmesti, ja ``discover`` ja ``fetch`` tekevät tämän oikein.

    **Downloads-tokenia ei anneta**, ja se on osa väitettä: tuonti ei lataa
    mitään, joten sen puuttuminen ei saa estää porttia syntymästä -- se on
    juuri se tilanne, jota varten koko komento on olemassa.
    """
    from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings

    env = env_file(".env", FACEIT_API_KEY="salainen-avain-XYZZY-42")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    settings = load_settings(settings_file, env_files=(env,))
    arkisto = ArchivePaths(root=tmp_path / "arkisto")

    port = import_stage.default_source(settings, arkisto)

    assert isinstance(port, MatchSource)
    assert port.cache_dir == arkisto.raw_faceit()
    # Avain ei saa näkyä esitysmuodossa.
    assert "XYZZY" not in repr(port)


def test_default_parser_really_builds_a_port() -> None:
    """``default_parser`` ajetaan oikeasti, ja se toteuttaa portin.

    Sama aukko kuin edellä: ilman tätä sen runko ei suoritu kertaakaan.
    Portti **rakennetaan, ei käytetä**: yhtään demoa ei parsita.
    """
    port = import_stage.default_parser()

    assert isinstance(port, DemoParser)
    assert hasattr(port, "read_map_name")


def test_the_match_is_asked_exactly_once_and_nothing_else(
    archive, source, parser
) -> None:
    """Tuonti ei lataa mitään: ainoa ulospäin menevä kutsu on vetotieto."""
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    assert source.asked == [MATCH]


def test_the_parser_is_only_asked_for_the_header(archive, source, parser) -> None:
    """``parse_demo`` kaataa feikin: 230 MB:n parsinta ei kuulu tuontiin."""
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    assert len(parser.asked) == 1


# -- C7: tulokseen kirjatut luvut ovat aitoja --------------------------------


def test_every_stat_describes_what_actually_happened(
    archive, source, parser
) -> None:
    """**Jokainen ``stats``-kenttä on väite, ja väitteet tarkistetaan.**

    Katselmus mutatoi kuusi kenttää yhtaikaa (``imported_bytes``, ``moved``,
    ``demos_dir``, ``source_path``, ``header_map_name``,
    ``expected_map_name``) ja 70 testiä meni läpi. Kenttä, jota mikään ei
    lue, on koristetta -- ja koriste, joka näkyy ruudulla, on valhe joka
    odottaa vuoroaan.
    """
    origin = place(archive, FACEIT_NAME)

    todo, result = do_import(archive, source, parser)

    stats = result.stats
    assert stats["map_demo_id"] == UNIT
    assert stats["size"] == len(ZSTD_BYTES)
    assert stats["imported_bytes"] == len(ZSTD_BYTES)
    assert stats["sha256"] == hashlib.sha256(ZSTD_BYTES).hexdigest()
    assert stats["demo_source"] == "import"
    assert stats["moved"] is True
    assert stats["length_verified"] is True
    assert stats["declared_bytes"] == len(PLAIN_BYTES)
    assert stats["demo_path"] == str(todo.target_path)
    assert stats["meta_path"] == str(todo.meta_path)
    assert stats["demos_dir"] == str(archive.demos_dir())
    assert stats["source_path"] == str(origin)
    assert stats["header_map_name"] == "de_ancient"
    assert stats["expected_map_name"] == "de_ancient"
    assert stats["map_matches"] is True
    # ``notes`` ja ``reason`` ovat sama sisältö kahdessa muodossa: tuloste
    # tulostaa jokaisen huomion omalle rivilleen, ``reason`` on niiden summa.
    assert " ".join(stats["notes"]) == result.reason
    assert len(stats["notes"]) >= 1


def test_the_copy_mode_is_recorded_as_a_copy(archive, source, tmp_path) -> None:
    """``moved`` erottaa siirron kopiosta, ja se on tarkistettava erikseen."""
    outside = tmp_path / "oma.dem.zst"
    outside.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    _todo, result = do_import(archive, source, parser, file=outside)

    assert result.stats["moved"] is False
    assert result.stats["source_path"] == str(outside)


# -- Yksityiskohtia, jotka on helppo rikkoa ----------------------------------


def test_every_rejection_carries_its_own_advice(archive, source, parser) -> None:
    """Epäonnistumista ei voi rakentaa ilman neuvoa."""
    with pytest.raises(AssertionError):
        import_stage._reject("jotain meni vikaan", advice="   ")


def test_the_comparison_ignores_case_but_nothing_else() -> None:
    """Vertailu on vertailu, ei nimen siivousta."""
    assert import_stage.same_map("de_nuke", "DE_NUKE")
    assert import_stage.same_map(" de_nuke ", "de_nuke")
    assert not import_stage.same_map("nuke", "de_nuke")
    assert not import_stage.same_map("de_nuke", "de_ancient")


def test_the_target_suffix_comes_from_the_magic_bytes(tmp_path) -> None:
    for data, expected in (
        (ZSTD_BYTES, ".dem.zst"),
        (GZIP_BYTES, ".dem.gz"),
        (PLAIN_BYTES, ".dem"),
    ):
        path = tmp_path / "koe.bin"
        path.write_bytes(data)
        assert import_stage.target_suffix(path) == expected


def test_the_length_source_is_named_per_format(tmp_path) -> None:
    """Kolme muotoa, kaksi vastausta -- ja ``.dem``:n vastaus on ``None``."""
    for data, expected in (
        (ZSTD_BYTES, "zstd-kehyksen ilmoittama purettu koko"),
        (GZIP_BYTES, "gzip-virran lopetusmerkki"),
        (PLAIN_BYTES, None),
    ):
        path = tmp_path / "koe.bin"
        path.write_bytes(data)
        assert import_stage.length_source(path) == expected


def test_importing_a_file_onto_itself_is_refused(archive, source, parser) -> None:
    """Lähde ja kohde eivät voi olla sama tiedosto."""
    target = archive.demos_dir() / f"{UNIT}.dem.zst"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ZSTD_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, file=target)

    assert "sama tiedosto" in str(err.value)
    assert target.read_bytes() == ZSTD_BYTES
