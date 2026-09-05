"""``stages.fetch`` -- vaiheen testit (Story 3.4).

**Ei verkkoa.** Vaihe näkee vain
:class:`~pappascout.adapters.protocols.DemoSource`-portin, ja tässä sen takana
on :class:`FakeDemoSource`, joka rakentaa tavut käsin.

Feikki **tarkistaa saamansa tunnisteen**, eikä se ole yksityiskohta vaan koko
sen arvo vartijana: lähde, joka palauttaa samat tavut kysyttiin mitä tahansa,
läpäisisi jokaisen testin myös silloin, kun vaihe kysyy väärää demoa -- ja
väärän kartan tallentuminen oikean nimellä on juuri se vika, jonka
``instances``-rakenne poistaa. Tuntematon tunniste nostaa siksi
``DemoUnavailable``in.

I/O-matriisin rivit ovat tässä tiedostossa yksi testi kutakin kohden, ja
nimestä tunnistaa rivin.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import has_temp_leftovers

from pappascout.adapters.decompress import ZSTD_MAGIC
from pappascout.adapters.protocols import DemoSource, DemoStream
from pappascout.archive.paths import ArchivePaths
from pappascout.errors import ApiError, DemoUnavailable, PappascoutError
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import select as select_stage
from pappascout.stages.fetch import MIN_PLAUSIBLE_DEMO_BYTES

#: Oikean muotoinen tunniste: ``{match_id}-{map_index}``, match_id ``1-<uuid>``.
MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"
UNIT = f"{MATCH}-0"
OTHER = f"{MATCH}-1"

def demo_bytes(marker: bytes = b"PAPPASCOUT", *, size: int | None = None) -> bytes:
    """Uskottavan näköinen pakattu demo: zstd-taikatavut ja riittävä koko.

    **Ei koristetta vaan testiaineiston vaatimus.** Vaihe hylkää nyt sisällön,
    joka ei ala zstd-taikatavuilla tai on liian pieni ollakseen CS2-demo --
    juuri siksi, ettei HTML-virhesivu tai tyhjä vastaus tallentuisi demoksi ja
    jäisi idempotenssin ohittamaksi ikuisesti. Testiaineiston on siis
    läpäistävä sama portti kuin oikean demon, muuten testit mittaisivat
    torjuntaa eivätkä latausta.
    """
    target = MIN_PLAUSIBLE_DEMO_BYTES + 4096 if size is None else size
    body = (marker + bytes(range(256))) * (target // (len(marker) + 256) + 1)
    return (ZSTD_MAGIC + body)[:target]


#: Kelvollinen demo, jota useimmat testit käyttävät.
DEMO_BYTES = demo_bytes()

#: Sisältö, joka **ei** ole demo: HTML-virhesivu 200-statuksella.
HTML_ERROR_PAGE = b"<!doctype html><html><body>403 Forbidden</body></html>" * (
    MIN_PLAUSIBLE_DEMO_BYTES // 50
)


# -- Kiinnikkeet -------------------------------------------------------------


@dataclass
class FakeDemo:
    """Yksi demo lähteessä.

    Attributes:
        data: Tavut, jotka virta antaa.
        announce: ``content_length``, jonka lähde **väittää**. ``None`` =
            lähde ei kerro pituutta lainkaan. Väitteen ja tavujen ero on oma
            kenttänsä, koska vajaa lataus on juuri se tila, jossa ne eroavat.
        break_after: Katkaise virta tämän palamäärän jälkeen
            (``ApiError``), tai ``None``.
        chunk: Palan koko tavuina.
    """

    data: bytes
    announce: int | None = -1
    break_after: int | None = None
    chunk: int = 64

    def content_length(self) -> int | None:
        return len(self.data) if self.announce == -1 else self.announce


@dataclass
class FakeDemoSource:
    """Portin feikki: tunnisteesta tavuiksi, ilman verkkoa.

    ``asked`` on se, mitä vartijalta odotetaan: testi voi todeta, ettei
    latausta edes aloitettu (levytilaportti, idempotenssi).
    """

    demos: dict[str, FakeDemo | Exception] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    closed: int = 0
    reads: dict[str, int] = field(default_factory=dict)

    def get_demo(self, map_demo_id: str) -> DemoStream:
        self.asked.append(map_demo_id)
        entry = self.demos.get(map_demo_id)
        if entry is None:
            raise DemoUnavailable(
                f"Lähteessä ei ole demoa {map_demo_id}. Tunniste ei täsmää "
                "yhteenkään otteluun."
            )
        if isinstance(entry, Exception):
            raise entry
        return DemoStream(
            chunks=self._chunks(map_demo_id, entry),
            content_length=entry.content_length(),
            on_close=self._close,
        )

    def _chunks(self, map_demo_id: str, entry: FakeDemo):
        self.reads[map_demo_id] = self.reads.get(map_demo_id, 0) + 1
        sent = 0
        for start in range(0, len(entry.data), entry.chunk):
            if entry.break_after is not None and sent >= entry.break_after:
                raise ApiError(
                    f"Demon {map_demo_id} lataus katkesi kesken "
                    "(ChunkedEncodingError)."
                )
            yield entry.data[start : start + entry.chunk]
            sent += 1

    def _close(self) -> None:
        self.closed += 1


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


@pytest.fixture
def local_archive(tmp_path: Path) -> ArchivePaths:
    """Arkisto, jonka demot menevät **arkiston ulkopuolelle** (2026-09-05)."""
    return ArchivePaths(root=tmp_path / "arkisto", demos_root=tmp_path / "demot")


@pytest.fixture
def source() -> FakeDemoSource:
    return FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES)})


def run(archive: ArchivePaths, source: DemoSource, unit: str = UNIT, **kwargs: Any):
    kwargs.setdefault("disk_free", lambda _archive: 100 * 1024**3)
    return fetch_stage.run(archive, unit, source=source, **kwargs)


def read_meta(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def place(directory: Path, unit: str, *, meta: bool = True) -> Path:
    """Aseta valmis demo (ja metatiedosto) hakemistoon."""
    directory.mkdir(parents=True, exist_ok=True)
    demo = directory / f"{unit}.dem.zst"
    demo.write_bytes(DEMO_BYTES)
    if meta:
        (directory / f"{unit}.meta.json").write_text(
            json.dumps({"sha256": "vanha", "size": len(DEMO_BYTES)}),
            encoding="utf-8",
        )
    return demo


# -- Matriisi: valittu MapDemo, ei arkistossa --------------------------------


def test_selected_map_demo_is_written_with_its_meta(archive, source) -> None:
    result = run(archive, source)

    demo = archive.demo(UNIT)
    assert result.status == "ok"
    assert result.skipped is False
    assert demo.read_bytes() == DEMO_BYTES

    meta = read_meta(archive.demo_meta(UNIT))
    assert meta["sha256"] == hashlib.sha256(DEMO_BYTES).hexdigest()
    assert meta["size"] == len(DEMO_BYTES)
    assert meta["source"] == "downloads_api"
    # Kelvollinen ISO-hetki eikä mikä tahansa merkkijono.
    assert datetime.fromisoformat(meta["fetched_at"]).tzinfo is not None


def test_the_source_is_asked_for_exactly_the_unit_that_was_requested(
    archive,
) -> None:
    """Vaihe ei saa kysyä muuta karttaa kuin sitä, joka sille annettiin."""
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES), OTHER: FakeDemo(demo_bytes(b"VAARA"))})
    run(archive, source, UNIT)
    assert source.asked == [UNIT]
    assert archive.demo(UNIT).read_bytes() == DEMO_BYTES


def test_the_stream_is_closed_even_though_the_stage_never_saw_a_connection(
    archive, source
) -> None:
    run(archive, source)
    assert source.closed == 1


# -- Matriisi: demo jo levyllä (kolme sijaintia, kolme testiä) ---------------


def test_demo_already_in_the_archive_is_not_downloaded(archive, source) -> None:
    place(archive.archive_demos_dir(), UNIT)

    result = run(archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []


def test_demo_already_in_the_local_demos_root_is_not_downloaded(
    local_archive, source
) -> None:
    place(local_archive.demos_root, UNIT)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []


def test_demo_in_the_archive_is_not_redownloaded_when_demos_root_is_set(
    local_archive, source
) -> None:
    """Asetuksen käyttöönotto ei saa ladata koko otantaa uudelleen.

    Jos idempotenssi katsoisi vain sinne, minne kirjoitetaan, jokainen jo
    OneDriveen ladattu demo haettaisiin toistamiseen -- 2,3 GB ja koko
    Downloads-kiintiö siitä hyvästä, ettei asetusta ollut ennen olemassa.
    """
    place(local_archive.archive_demos_dir(), UNIT)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []
    assert not (local_archive.demos_root / f"{UNIT}.dem.zst").exists()


def test_a_demo_in_import_with_the_canonical_name_is_not_downloaded(
    local_archive, source
) -> None:
    """``import/`` on kolmas hakusijainti -- **kanonisella nimellä**.

    Tämä on se tila, jonka Story 3.6 tuottaa: tunniste on arkiston muodossa ja
    metatiedosto on kirjoitettu. Silloin tuotu demo käyttäytyy täsmälleen kuten
    ladattu, eikä mikään vaihe erota niitä.
    """
    place(local_archive.import_dir(), UNIT)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []


def test_a_browser_downloaded_demo_in_import_is_still_fetched_again(
    local_archive, source
) -> None:
    """**Tiedostettu puute, ei väite että näin ei kävisi (A9).**

    Selaimella haettu demo on ``import/``issa FACEITin omalla nimellä
    ``{match_id}-{round}-{instance}.dem`` -- eri tunniste kuin arkiston
    ``{match_id}-{map_index}`` -- eikä sillä ole ``.meta.json``ia, jota
    idempotenssi vaatii. Se latautuu siis uudelleen.

    Testi lukitsee tämän **nykytilaksi eikä tavoitteeksi**: aiempi versio
    asetti ``import/``iin tiedoston kanonisella nimellä ja metalla, eli tilan,
    jota käsin tuonti ei tuota, ja antoi siten väärän turvan. Kun Story 3.6
    nimeää tuodut demot ``instances[].id``:n avulla, tämä testi kääntyy
    ympäri -- ja sen kääntäminen on silloin tietoinen muutos.
    """
    faceit_name = local_archive.import_dir() / f"{MATCH}-1-1.dem"
    faceit_name.parent.mkdir(parents=True, exist_ok=True)
    faceit_name.write_bytes(DEMO_BYTES)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is False
    assert source.asked == [UNIT]


def test_a_demo_in_import_without_a_meta_is_fetched_again(
    local_archive, source
) -> None:
    """Käsin kopioidulla demolla ei ole metatiedostoa -- eikä siis tiivistettä."""
    place(local_archive.import_dir(), UNIT, meta=False)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is False
    assert source.asked == [UNIT]


# -- Matriisi: vajaa tila levyllä (demo ilman metaa, meta ilman demoa) -------


def test_demo_without_meta_is_downloaded_again_and_the_reason_says_why(
    archive, source
) -> None:
    place(archive.archive_demos_dir(), UNIT, meta=False)

    result = run(archive, source)

    assert result.status == "ok"
    assert result.skipped is False
    assert source.asked == [UNIT]
    assert "metatiedosto puuttui" in (result.reason or "")
    assert read_meta(archive.demo_meta(UNIT))["sha256"] == (
        hashlib.sha256(DEMO_BYTES).hexdigest()
    )


def test_meta_without_demo_is_downloaded_again_and_the_old_meta_is_replaced(
    archive, source
) -> None:
    directory = archive.archive_demos_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{UNIT}.meta.json").write_text(
        json.dumps({"sha256": "vanha-tiiviste", "size": 1}), encoding="utf-8"
    )

    result = run(archive, source)

    assert result.status == "ok"
    assert source.asked == [UNIT]
    meta = read_meta(archive.demo_meta(UNIT))
    assert meta["sha256"] == hashlib.sha256(DEMO_BYTES).hexdigest()
    assert meta["size"] == len(DEMO_BYTES)


# -- Matriisi: demoa ei ole ---------------------------------------------------


def test_a_demo_the_source_does_not_have_is_no_demo_and_writes_nothing(
    archive,
) -> None:
    source = FakeDemoSource({})

    result = run(archive, source)

    assert result.status == "no_demo"
    assert archive.find_demo(UNIT) is None
    assert not archive.demo_meta(UNIT).exists()
    assert result.reason and UNIT in result.reason


def test_no_demo_is_a_different_status_from_download_failed(archive) -> None:
    """Poissa oleva demo ja katkennut yhteys eivät saa näyttää samalta.

    Ne johtavat eri jatkoon: toinen ei enää koskaan onnistu, toinen
    todennäköisesti onnistuu heti seuraavalla ajolla.
    """
    gone = run(archive, FakeDemoSource({UNIT: DemoUnavailable("Poistettu.")}))
    broken = run(
        archive,
        FakeDemoSource({UNIT: ApiError("Rajapinta ei vastannut.", status_code=503)}),
    )
    assert gone.status == "no_demo"
    assert broken.status == "download_failed"


# -- Matriisi: katkennut virta ja vajaa Content-Length ------------------------


def test_a_broken_stream_leaves_neither_a_demo_nor_a_temp_file(archive) -> None:
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, break_after=2)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert not archive.demo_meta(UNIT).exists()
    assert not has_temp_leftovers(archive.root)


def test_a_short_download_names_both_numbers_and_is_not_moved_into_place(
    archive,
) -> None:
    # Lähde lupaa enemmän kuin antaa: juuri se on vajaa lataus.
    source = FakeDemoSource(
        {UNIT: FakeDemo(DEMO_BYTES, announce=len(DEMO_BYTES) + 4096)}
    )

    result = run(archive, source)

    assert result.status == "download_failed"
    assert str(len(DEMO_BYTES) + 4096) in (result.reason or "")
    assert str(len(DEMO_BYTES)) in (result.reason or "")
    assert archive.find_demo(UNIT) is None
    assert not has_temp_leftovers(archive.root)


def test_a_source_that_does_not_announce_a_length_says_so_out_loud(
    archive,
) -> None:
    """``None`` on "ei kertonut" eikä "nolla tavua" -- **ja se sanotaan**.

    Ilman ``Content-Length``iä katkennut virta näyttää täsmälleen samalta kuin
    ehjä: tiedosto on paikallaan, alku on zstd-muotoa, koko on uskottava.
    Vaihe ei voi erottaa niitä, joten se ei saa vaieta: vaiettu epävarmuus
    näyttäisi varmuudelta, ja katkennut demo ohitettaisiin idempotenssin
    nojalla ikuisesti.

    Aiempi versio tästä testistä lukitsi pelkän ``status == "ok"``:n
    varmistamatta mitään ehjyydestä -- se antoi väärän turvan.
    """
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, announce=None)})

    result = run(archive, source)

    assert result.status == "ok"
    assert archive.demo(UNIT).read_bytes() == DEMO_BYTES
    # 1) Metatiedosto kertoo koneelle, ettei pituutta tarkistettu.
    assert read_meta(archive.demo_meta(UNIT))["length_verified"] is False
    assert result.stats["length_verified"] is False
    # 2) Syy kertoo ihmiselle saman ja sen, mitä tehdä jos parse kaatuu.
    reason = result.reason or ""
    assert "Content-Length" in reason
    assert "parsinnassa" in reason


def test_a_verified_download_does_not_carry_the_uncertainty_note(
    archive, source
) -> None:
    """Varoitus vain silloin kun on syytä: muuten se muuttuisi taustakohinaksi."""
    result = run(archive, source)

    assert read_meta(archive.demo_meta(UNIT))["length_verified"] is True
    assert result.stats["length_verified"] is True
    assert "Content-Length" not in (result.reason or "")


# -- Roska ei kelpaa demoksi (2026-09-05) ------------------------------------


def test_an_html_error_page_with_status_200_is_not_stored_as_a_demo(
    archive,
) -> None:
    """200-statuksella tullut virhesivu on onnistunut HTTP-vastaus.

    Se ei erotu mistään muusta kuin sisällöstään -- ja jos se kirjoittuisi
    demoksi, idempotenssi ohittaisi sen **joka ajolla ikuisesti**: metatiedosto
    antaisi sille sha256:n ja ``source: downloads_api``, eikä mikään enää
    yrittäisi hakea oikeaa demoa.
    """
    source = FakeDemoSource({UNIT: FakeDemo(HTML_ERROR_PAGE)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert archive.find_demo_meta(UNIT) is None
    assert not has_temp_leftovers(archive.root)
    assert "zstd" in (result.reason or "")


def test_an_empty_response_is_not_stored_as_a_demo(archive) -> None:
    """``Content-Length: 0`` läpäisee pituustarkistuksen (0 == 0)."""
    source = FakeDemoSource({UNIT: FakeDemo(b"", announce=0)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert "liian vähän" in (result.reason or "")


def test_a_truncated_but_correctly_labelled_file_is_not_stored(archive) -> None:
    """Muutaman kilotavun zstd-alku ei ole CS2-demo."""
    source = FakeDemoSource({UNIT: FakeDemo(ZSTD_MAGIC + b"a" * 5000)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None


def test_the_size_estimate_covers_the_largest_measured_demo() -> None:
    """Arvio on levytilaportin syöte: liian pieni päästäisi läpi liikaa.

    Arkiston suurin pakattu demo on 234 163 493 tavua (mitattu 2026-09-05).
    Arvion on oltava sen yläpuolella, tai kuvauksen on lakattava kutsumasta
    sitä ylärajaksi.
    """
    largest_measured = 234_163_493
    assert fetch_stage.DEMO_SIZE_ESTIMATE_BYTES > largest_measured


def test_the_minimum_size_is_far_below_a_real_demo() -> None:
    """Vartija ei saa hylätä oikeaa demoa: pienin arkistossa on yli 140 MB."""
    smallest_measured = 142 * 1024 * 1024
    assert fetch_stage.MIN_PLAUSIBLE_DEMO_BYTES < smallest_measured


# -- Matriisi: levytila -------------------------------------------------------


def test_a_full_disk_stops_the_download_before_it_starts(archive, source) -> None:
    result = run(
        archive,
        source,
        disk_free=lambda _archive: 100 * 1024 * 1024,
        size_estimate=200 * 1024 * 1024,
        reserve_bytes=2 * 1024**3,
    )

    assert result.status == "download_failed"
    # **Tärkein väite:** latausta ei aloitettu, joten kiintiötä ei kulunut
    # eikä levylle kirjoitettu tavuakaan.
    assert source.asked == []
    assert archive.find_demo(UNIT) is None
    assert result.reason and "Levytila ei riitä" in result.reason


def test_the_disk_message_tells_the_user_what_to_do_in_finnish(
    archive, source
) -> None:
    result = run(archive, source, disk_free=lambda _a: 1024, size_estimate=2048)
    reason = result.reason or ""
    assert "Vapaana on" in reason
    assert "tarvitaan" in reason
    assert str(archive.demos_dir()) in reason


def test_unknown_free_space_does_not_block_the_download(archive, source) -> None:
    """``None`` on "ei saatu selville"; se ei saa pysäyttää työkalua."""
    result = run(archive, source, disk_free=lambda _archive: None)
    assert result.status == "ok"


def test_free_space_is_measured_where_the_demos_are_written(
    local_archive, monkeypatch
) -> None:
    """Levytila kysytään kohdeasemalta, ei arkiston asemalta.

    Tässä molemmat ovat sama levy, joten testi ei voi väittää eri lukuja --
    mutta se voi väittää, että kysytty hakemisto on kohde eikä arkiston juuri.
    """
    local_archive.demos_root.mkdir(parents=True)
    asked: list[Path] = []
    real = fetch_stage.shutil.disk_usage

    def spy(path):
        asked.append(Path(path))
        return real(path)

    monkeypatch.setattr(fetch_stage.shutil, "disk_usage", spy)
    fetch_stage.free_space(local_archive)

    assert asked == [local_archive.demos_root]


# -- Paikallinen demohakemisto (lisäys 2026-09-05) ---------------------------


def test_with_demos_root_the_demo_never_lands_in_the_archive(
    local_archive, source
) -> None:
    result = run(local_archive, source)

    assert result.status == "ok"
    assert (local_archive.demos_root / f"{UNIT}.dem.zst").read_bytes() == DEMO_BYTES
    assert not (local_archive.archive_demos_dir() / f"{UNIT}.dem.zst").exists()
    assert not local_archive.archive_demos_dir().exists()


@pytest.mark.parametrize("local", [False, True])
def test_the_meta_is_always_written_next_to_the_demo(
    tmp_path, source, local: bool
) -> None:
    """Metatiedosto on väite juuri siitä tiedostosta; eri hakemistoissa ne
    erkanisivat heti kun toinen kopioidaan tai poistetaan."""
    archive = ArchivePaths(
        root=tmp_path / "arkisto",
        demos_root=(tmp_path / "demot") if local else None,
    )

    run(archive, source)

    demo = archive.find_demo(UNIT)
    assert demo is not None
    assert (demo.parent / f"{UNIT}.meta.json").is_file()


def test_outputs_never_claim_an_outside_path_is_an_archive_path(
    local_archive, source
) -> None:
    """``outputs`` on sopimuksen mukaan arkiston sisäinen suhteellinen polku.

    Paikallinen demo ei ole arkistossa, joten sitä ei ole siellä -- ja
    absoluuttiset polut kulkevat ``stats``issa, jossa ne eivät väitä mitään
    arkistosta.
    """
    result = run(local_archive, source)

    assert result.outputs == ()
    assert result.stats["demo_path"] == str(local_archive.demo(UNIT))
    assert result.stats["meta_path"] == str(local_archive.demo_meta(UNIT))
    for output in result.outputs:
        assert not Path(str(output)).is_absolute()


def test_outputs_are_archive_relative_when_the_demo_is_in_the_archive(
    archive, source
) -> None:
    result = run(archive, source)

    assert [str(p) for p in result.outputs] == [
        f"demos/{UNIT}.dem.zst",
        f"demos/{UNIT}.meta.json",
    ]


# -- Kirjoitusjärjestys ja tiiviste ------------------------------------------


def test_the_meta_is_written_only_after_the_demo_is_in_place(
    archive, source, monkeypatch
) -> None:
    """Metatiedosto ei saa koskaan kuvata tiedostoa, jota ei ole.

    Järjestys mitataan siitä, missä järjestyksessä atomiset siirrot tapahtuvat
    -- se on sama tapahtuma, joka tekee tiedostosta näkyvän muille.
    """
    order: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        order.append(Path(dst).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    run(archive, source)

    assert order == [f"{UNIT}.dem.zst", f"{UNIT}.meta.json"]


def test_an_interrupted_run_leaves_the_demo_without_a_meta_not_the_reverse(
    archive, source, monkeypatch
) -> None:
    """Keskeytyneen ajon jälki on korjattavissa; päinvastainen ei olisi.

    Demo ilman metaa ladataan uudelleen ja korjaantuu. Meta ilman demoa
    väittäisi tiivisteen tiedostosta, jota ei ole -- ja ``parse`` liittäisi sen
    manifestiinsa.
    """

    def boom(*args, **kwargs):
        raise OSError("levy irtosi kesken metatiedoston kirjoituksen")

    monkeypatch.setattr(fetch_stage, "atomic_write_json", boom)

    result = run(archive, source)

    # Levyvirhe on yksikön tila, ei ohjelmavirhe (ks. A4).
    assert result.status == "download_failed"
    assert archive.demo(UNIT).is_file()
    assert not archive.demo_meta(UNIT).exists()


def test_the_demo_is_never_read_back_to_compute_its_hash(
    archive, source, monkeypatch
) -> None:
    """200 MB:n uudelleenlukeminen hashausta varten on kielletty.

    Todiste on kaksiosainen: virta luetaan tasan kerran, eikä yhtäkään
    demotiedostoa avata lukutilassa.
    """
    opened: list[tuple[str, str]] = []
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        opened.append((str(file), mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    result = run(archive, source)
    monkeypatch.undo()

    assert result.status == "ok"
    assert source.reads[UNIT] == 1

    demo_opens = [
        (path, mode) for path, mode in opened if f"{UNIT}.dem.zst" in path
    ]
    assert demo_opens, "demoa ei avattu lainkaan -- testi ei mittaa mitään"
    for path, mode in demo_opens:
        assert "r" not in mode, f"demo avattiin lukutilassa: {path} ({mode})"


def test_the_hash_is_the_hash_of_the_bytes_that_were_written(archive) -> None:
    """Tiiviste on todiste tiedostosta eikä virrasta, jos ne eroaisivat."""
    payload = demo_bytes(b"TIIVISTE")
    source = FakeDemoSource({UNIT: FakeDemo(payload, chunk=7919)})

    run(archive, source)

    written = archive.demo(UNIT).read_bytes()
    assert read_meta(archive.demo_meta(UNIT))["sha256"] == (
        hashlib.sha256(written).hexdigest()
    )


def test_writes_are_atomic(archive, source) -> None:
    run(archive, source)
    assert not has_temp_leftovers(archive.root)


# -- Sarja: yksi vika ei kaada ajoa ------------------------------------------


def test_one_missing_demo_does_not_stop_the_others(archive) -> None:
    third = f"{MATCH}-2"
    source = FakeDemoSource(
        {
            UNIT: FakeDemo(DEMO_BYTES),
            OTHER: DemoUnavailable("FACEIT on poistanut tallenteen."),
            third: FakeDemo(demo_bytes(b"KOLMAS")),
        }
    )

    results = fetch_stage.run_many(
        archive,
        [UNIT, OTHER, third],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["ok", "no_demo", "ok"]
    assert archive.demo(UNIT).is_file()
    assert archive.demo(third).is_file()


def test_a_programming_error_is_not_swallowed_by_the_loop(archive) -> None:
    """``PappascoutError`` on yksikön tila; muu poikkeus on koodin vika.

    Jos silmukka nielaisisi kaiken, ``TypeError`` piiloutuisi yhdentoista
    onnistuneen latauksen sekaan tilana ``download_failed``.
    """

    class Broken:
        def get_demo(self, map_demo_id: str):
            raise TypeError("ohjelmavirhe")

    with pytest.raises(TypeError):
        fetch_stage.run_many(
            archive, [UNIT], source=Broken(), disk_free=lambda _a: 100 * 1024**3
        )


def test_a_bad_identifier_is_rejected_before_anything_is_asked(
    archive, source
) -> None:
    with pytest.raises(PappascoutError):
        fetch_stage.run(archive, "../pako", source=source)
    assert source.asked == []


# -- Suunnitelma --------------------------------------------------------------


def selection_document(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": select_stage.SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "team_key": "joukkue",
        "selections": rows,
    }


def write_selection(archive: ArchivePaths, rows: list[dict[str, Any]]) -> None:
    path = archive.selection("joukkue")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(selection_document(rows), ensure_ascii=False), encoding="utf-8"
    )


def test_the_plan_takes_only_the_maps_that_passed_the_roster_threshold(
    archive,
) -> None:
    write_selection(
        archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": OTHER, "roster_ok": False},
        ],
    )

    todo = fetch_stage.plan(archive, "joukkue")

    assert todo.pending == (UNIT,)
    assert todo.present == ()
    assert todo.selected == 1


def test_the_plan_separates_what_is_already_on_disk(local_archive) -> None:
    write_selection(
        local_archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": OTHER, "roster_ok": True},
        ],
    )
    place(local_archive.archive_demos_dir(), UNIT)

    todo = fetch_stage.plan(local_archive, "joukkue")

    assert todo.pending == (OTHER,)
    assert todo.present == (UNIT,)
    assert todo.selected == 2
    assert todo.estimated_bytes == fetch_stage.DEMO_SIZE_ESTIMATE_BYTES


def test_the_plan_says_what_to_run_when_the_selection_file_is_missing(
    archive,
) -> None:
    with pytest.raises(PappascoutError, match="select"):
        fetch_stage.plan(archive, "joukkue")


# -- Levyvirhe on yksikön tila, ei ohjelmavirhe (A4, 2026-09-05) -------------


def _full_disk(monkeypatch, target_name: str) -> None:
    """Anna ``ENOSPC`` jokaiselle kirjoitukselle, joka osuu kohteeseen."""
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        if target_name in str(file) and "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)


def test_a_full_disk_mid_write_is_download_failed_not_a_crash(
    archive, source, monkeypatch
) -> None:
    """``[Errno 28]`` ei saa päätyä ruudulle sanojen "ohjelmavirhe" kanssa.

    Levy on yksikön ominaisuus siinä missä verkkokin: se voi täyttyä kesken
    kirjoituksen, OneDrive voi lukita tiedoston ja verkkolevy katketa. Kaikki
    kolme nostavat ``OSError``in, eikä yksikään niistä ole koodin vika.
    """
    _full_disk(monkeypatch, UNIT)

    result = run(archive, source)

    assert result.status == "download_failed"
    reason = result.reason or ""
    assert "levy" in reason.lower()
    assert "Muut demot haettiin silti" in reason
    assert archive.find_demo(UNIT) is None
    assert not has_temp_leftovers(archive.root)


def test_a_full_disk_does_not_stop_the_rest_of_the_run(
    archive, monkeypatch
) -> None:
    """Jäädytetty rajoite: yhden demon epäonnistuminen ei keskeytä ajoa.

    Ilman ``OSError``-haaraa ``run_many``issa seuraavaa demoa ei yritettäisi
    lainkaan -- todennettu simuloidulla täydellä levyllä.
    """
    second = f"{MATCH}-1"
    source = FakeDemoSource(
        {UNIT: FakeDemo(DEMO_BYTES), second: FakeDemo(demo_bytes(b"TOINEN"))}
    )
    _full_disk(monkeypatch, f"{UNIT}.dem")

    results = fetch_stage.run_many(
        archive,
        [UNIT, second],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["download_failed", "ok"]
    assert source.asked == [UNIT, second]
    assert archive.demo(second).is_file()


def test_run_many_turns_a_pappascout_error_into_a_unit_result(archive) -> None:
    """``run_many``in oma virhehaara: yksikkö ei saa kadota hiljaa.

    ``run`` nostaa poikkeuksen kelvottomasta tunnisteesta, ja ilman tätä haaraa
    tulosrivi jäisi syntymättä -- luettelo näyttäisi lyhyemmältä kuin
    suunnitelma, eikä mikään kertoisi kummasta yksiköstä on kyse.
    """
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES)})

    results = fetch_stage.run_many(
        archive,
        ["../pako", UNIT],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["download_failed", "ok"]
    assert results[0].unit == "../pako"
    assert results[0].reason and "map_demo_id" in results[0].reason


# -- Kohdehakemiston kirjoitettavuus (A5) ------------------------------------


def test_a_demos_root_that_is_a_file_is_reported_in_finnish(
    tmp_path, source
) -> None:
    """Tiedosto hakemiston paikalla ei näy levytilassa mitenkään."""
    blocker = tmp_path / "demot"
    blocker.write_text("en ole hakemisto", encoding="utf-8")
    archive = ArchivePaths(root=tmp_path / "arkisto", demos_root=blocker)

    result = run(archive, source, disk_free=lambda _a: 100 * 1024**3)

    assert result.status == "download_failed"
    assert "demos_root" in (result.reason or "") + result.stats["next_step"]
    # Neuvo on omassa kentässään eikä otsikossa (D1).
    assert "settings.toml" in result.stats["next_step"]
    # Tärkein väite: yhteyttä ei avattu, joten kiintiötä ei kulunut.
    assert source.asked == []


def test_an_unwritable_demos_root_is_reported_before_any_call(
    tmp_path, source, monkeypatch
) -> None:
    """Kirjoitussuojattu hakemisto: kokeillaan, ei päätellä oikeuksista."""
    local = tmp_path / "demot"
    archive = ArchivePaths(root=tmp_path / "arkisto", demos_root=local)
    real_write = Path.write_bytes

    def refuse(self, data):
        if "kirjoituskoe" in self.name:
            raise PermissionError(13, "Access is denied")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", refuse)

    result = run(archive, source, disk_free=lambda _a: 100 * 1024**3)

    assert result.status == "download_failed"
    assert "ei voi kirjoittaa" in (result.reason or "")
    assert "Downloads-kiintiötä" in (result.reason or "")
    assert "kirjoitusoikeudet" in result.stats["next_step"]
    assert source.asked == []


def test_the_write_probe_leaves_nothing_behind(tmp_path, source) -> None:
    local = tmp_path / "demot"
    archive = ArchivePaths(root=tmp_path / "arkisto", demos_root=local)

    run(archive, source, disk_free=lambda _a: 100 * 1024**3)

    assert [p.name for p in local.glob("*kirjoituskoe*")] == []


# -- Ei kahta kopiota samasta demosta (A10) ---------------------------------


def test_a_demo_missing_its_meta_is_refetched_in_place_not_duplicated(
    local_archive, source
) -> None:
    """Vajaa demo arkistossa + paikallinen hakemisto käytössä.

    Oletuskohteeseen kirjoittaminen jättäisi arkiston 190 MB paikalleen ja
    tekisi toisen kopion viereen -- eli kaksinkertaistaisi juuri sen, mitä
    paikallisella hakemistolla vältetään.
    """
    place(local_archive.archive_demos_dir(), UNIT, meta=False)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert source.asked == [UNIT]
    in_archive_path = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    assert in_archive_path.read_bytes() == DEMO_BYTES
    assert (local_archive.archive_demos_dir() / f"{UNIT}.meta.json").is_file()
    # Ei kopiota paikalliseen hakemistoon.
    assert not (local_archive.demos_root / f"{UNIT}.dem.zst").exists()
    assert not (local_archive.demos_root / f"{UNIT}.meta.json").exists()


def test_an_orphan_meta_elsewhere_is_removed_not_left_behind(
    local_archive, source
) -> None:
    """Meta ilman demoa toisessa hakemistossa (A9).

    Pelkkä uuden metan kirjoitus jättäisi vanhan paikalleen väittämään
    tiivistettä tiedostosta jota siellä ei ole -- ja ``parse`` lukee tiivisteen
    juuri ensimmäisestä löytyneestä metatiedostosta.
    """
    orphan_dir = local_archive.archive_demos_dir()
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / f"{UNIT}.meta.json"
    orphan.write_text(json.dumps({"sha256": "orpo", "size": 1}), encoding="utf-8")

    result = run(local_archive, source)

    assert result.status == "ok"
    assert not orphan.exists()
    assert (local_archive.demos_root / f"{UNIT}.meta.json").is_file()
    assert read_meta(local_archive.find_demo_meta(UNIT))["sha256"] != "orpo"
    assert "poistettiin" in (result.reason or "")


# -- Levytila todellisella koolla (A12) --------------------------------------


def test_the_announced_length_is_checked_against_free_space_before_writing(
    archive,
) -> None:
    """``Content-Length`` on tiedossa ennen kirjoitusta -- käytä sitä.

    Arvio riittää portiksi vain siihen asti, kunnes lähde kertoo koon. Sen
    jälkeen arvion käyttäminen olisi tahallista epätarkkuutta.
    """
    huge = 8 * 1024**3
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, announce=huge)})

    result = run(
        archive,
        source,
        # Arvio mahtuu, todellinen koko ei.
        size_estimate=1024,
        reserve_bytes=1024,
        disk_free=lambda _a: 4 * 1024**3,
    )

    assert result.status == "download_failed"
    assert "Levytila ei riitä" in (result.reason or "")
    assert size_of(archive, UNIT) is None
    assert not has_temp_leftovers(archive.root)


def size_of(archive: ArchivePaths, unit: str) -> int | None:
    found = archive.find_demo(unit)
    return None if found is None else found.stat().st_size


# -- Atomisuus myös paikallisessa moodissa (B8) ------------------------------


def test_writes_are_atomic_in_the_local_demos_root_too(
    local_archive, source
) -> None:
    """``settings.toml`` ottaa juuri tämän moodin käyttöön."""
    run(local_archive, source)
    assert not has_temp_leftovers(local_archive.demos_root)
    assert not has_temp_leftovers(local_archive.root)


def test_a_broken_stream_leaves_no_temp_file_in_the_local_demos_root(
    local_archive,
) -> None:
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, break_after=2)})

    result = run(local_archive, source)

    assert result.status == "download_failed"
    assert not has_temp_leftovers(local_archive.demos_root)
    assert local_archive.find_demo(UNIT) is None


# -- Suunnitelman rivivartija (B5) -------------------------------------------


def test_the_plan_skips_rows_that_are_not_usable(archive) -> None:
    """Rikkinäinen rivi ei saa päätyä tunnisteeksi, joka indeksoi polkua."""
    write_selection(
        archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": "", "roster_ok": True},
            {"map_demo_id": None, "roster_ok": True},
            {"roster_ok": True},
            "en ole rivi",
        ],
    )

    todo = fetch_stage.plan(archive, "joukkue")

    assert todo.pending == (UNIT,)


def test_a_selection_file_without_a_selections_list_says_what_to_run(
    archive,
) -> None:
    path = archive.selection("joukkue")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": select_stage.SCHEMA_VERSION,
                "team_key": "joukkue",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PappascoutError, match="select"):
        fetch_stage.plan(archive, "joukkue")


# -- Tuotannon portti (B1) ----------------------------------------------------


def test_default_source_really_builds_a_port(
    settings_file, env_file, tmp_path, monkeypatch
) -> None:
    """``default_source`` ajetaan oikeasti -- **verkkoon menemättä**.

    Se on vaiheen ainoa rivi, joka liittää sen FACEITiin, ja jokainen muu testi
    korvaa sen feikillä. Ilman tätä testiä väärä välimuistihakemisto tai väärä
    Downloads-osoite menisi läpi koko sarjasta, ja ensimmäinen oikea ajo hakisi
    olemattomasta osoitteesta.

    Portti **rakennetaan, ei käytetä**: yhtään pyyntöä ei lähde.
    """
    from pappascout.adapters.faceit import FACEIT_DOWNLOADS_API_BASE
    from pappascout.adapters.protocols import DemoSource as DemoSourcePort
    from pappascout.domain.models import load_settings

    env = env_file(
        ".env",
        FACEIT_API_KEY="salainen-avain-XYZZY-42",
        FACEIT_DOWNLOADS_TOKEN="downloads-token-QUUX-77",
    )
    settings = load_settings(settings_file, env_files=(env,))
    archive = ArchivePaths(root=tmp_path / "arkisto")

    port = fetch_stage.default_source(settings, archive)

    assert isinstance(port, DemoSourcePort)
    assert port.downloads_base_url == FACEIT_DOWNLOADS_API_BASE
    assert port._client.cache_dir == archive.raw_faceit()
    # Kumpikaan salaisuus ei näy esityksessä; tämä on se kohta, jossa portti
    # syntyy, ja siksi myös se kohta, jossa väite on mitattavissa.
    assert "XYZZY" not in repr(port)
    assert "QUUX" not in repr(port)
    port._client.close()


# -- Toistuvuus lopettaa sarjan, ei syyn arvaus (D3, 2026-09-05) ------------
#
# Ensimmäiseen 400:aan pysähtyminen olisi houkuttelevaa mutta perustelematonta:
# 400 tarkoittaa joko epämuodostunutta tunnistetta (kaikki kaatuvat) tai
# epämuodostunutta resource_urlia (vain yksi kaatuu). C2:n perustelu
# ("ei yksikään voi onnistua") ei siis päde. Toistuvuus sen sijaan on
# mitattavissa ilman että syytä tarvitsee tietää.


def failing(unit: str, status: int = 400) -> ApiError:
    return ApiError(f"Yksikkö {unit} kaatui.", status_code=status, advice="X")


def test_three_identical_failures_stop_the_run(archive) -> None:
    limit = fetch_stage.IDENTICAL_FAILURE_LIMIT
    units = [f"{MATCH}-{i}" for i in range(limit + 3)]
    source = FakeDemoSource({u: failing(u) for u in units})

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    # Vain kolme yritettiin; loput saivat rivin muttei kutsua.
    assert source.asked == units[:limit]
    assert len(results) == len(units), "jokaiselle yksikölle on rivi"
    assert all(r.status == "download_failed" for r in results)
    assert all(r.stats.get("not_attempted") for r in results[limit:])


def test_the_units_that_were_not_attempted_say_so_and_say_why(archive) -> None:
    """Hiljainen lyhennys jättäisi käyttäjän arvaamaan mihin loput katosivat."""
    limit = fetch_stage.IDENTICAL_FAILURE_LIMIT
    units = [f"{MATCH}-{i}" for i in range(limit + 2)]
    source = FakeDemoSource({u: failing(u) for u in units})

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    skipped = results[limit:]
    assert skipped
    for result in skipped:
        assert "Ei yritetty" in (result.reason or "")
        assert "http-400" in (result.reason or "")
        assert "yhä hakematta" in result.stats["next_step"]


def test_different_failures_do_not_stop_the_run(archive) -> None:
    """Toistuvuus on **sama** vika peräkkäin, ei mikä tahansa kolme vikaa.

    Eri koodit tarkoittavat eri syitä, eikä niistä voi päätellä yhteistä
    vikaa -- ja sarjan lopettaminen silloin olisi juuri se arvaus, jota tämä
    sääntö välttää.
    """
    units = [f"{MATCH}-{i}" for i in range(5)]
    codes = [400, 500, 400, 500, 400]
    source = FakeDemoSource(
        {u: failing(u, c) for u, c in zip(units, codes, strict=True)}
    )

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert not any(r.stats.get("not_attempted") for r in results)


def test_a_success_between_failures_resets_the_run(archive) -> None:
    """Onnistuminen todistaa, ettei vika ole yhteinen."""
    units = [f"{MATCH}-{i}" for i in range(6)]
    source = FakeDemoSource(
        {
            units[0]: failing(units[0]),
            units[1]: failing(units[1]),
            units[2]: FakeDemo(DEMO_BYTES),
            units[3]: failing(units[3]),
            units[4]: failing(units[4]),
            units[5]: failing(units[5]),
        }
    )

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert [r.status for r in results][2] == "ok"


def test_repeated_missing_demos_do_not_stop_the_run(archive) -> None:
    """Kolme peräkkäistä poistettua demoa on normaali havainto, ei vika.

    Vanhassa otannassa ``no_demo`` on odotettu lopputulos -- ja jos se
    lopettaisi sarjan, uudemmat demot jäisivät hakematta juuri silloin kun
    niitä eniten tarvitaan.
    """
    units = [f"{MATCH}-{i}" for i in range(5)]
    source = FakeDemoSource(
        {u: DemoUnavailable(f"Demoa {u} ei ole.") for u in units}
    )

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert all(r.status == "no_demo" for r in results)


def test_a_run_shorter_than_the_limit_is_never_cut_short(archive) -> None:
    """Kahden yksikön ajossa ei ole mitään lopetettavaa."""
    units = [UNIT, OTHER]
    source = FakeDemoSource({u: failing(u) for u in units})

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert len(results) == 2


def test_the_limit_is_above_two_so_a_coincidence_does_not_stop_the_run() -> None:
    """Kaksi peräkkäistä samaa koodia on uskottavaa sattumaa.

    Esimerkiksi kaksi poistettua demoa samasta ottelusta. Kolmen kutsun hinta
    on pieni verrattuna siihen, että sarja lopetettaisiin väärin perustein.
    """
    assert fetch_stage.IDENTICAL_FAILURE_LIMIT >= 3


# -- Jokainen epäonnistuminen kantaa neuvon (D1, vaihetaso) -----------------


def test_every_failure_from_the_stage_carries_a_next_step(archive) -> None:
    """Vartija: epäonnistumista ei voi rakentaa ilman seuraavaa toimenpidettä.

    Ilman tätä uusi vikapolku voisi tuottaa rivin ilman neuvoa, ja tuloste
    joutuisi keksimään sellaisen -- eli palaisi täsmälleen siihen oletukseen,
    jonka koko korjaus poistaa.
    """
    cases = {
        "levy": FakeDemo(DEMO_BYTES),
        "poissa": DemoUnavailable("Ei ole."),
        "verkko": ApiError("Ei vastannut.", status_code=503),
        "roska": FakeDemo(HTML_ERROR_PAGE),
        "vajaa": FakeDemo(DEMO_BYTES, announce=len(DEMO_BYTES) + 4096),
    }
    for name, entry in cases.items():
        unit = f"{MATCH}-0"
        source = FakeDemoSource({unit: entry})
        free = 1024 if name == "levy" else 100 * 1024**3
        result = run(archive, source, unit, disk_free=lambda _a, f=free: f)
        if result.status == "ok":
            continue
        step = str(result.stats.get("next_step", "")).strip()
        assert step, f"tapaus {name!r} tuotti epäonnistumisen ilman neuvoa"


def test_a_failure_built_without_a_next_step_is_refused() -> None:
    """Vartija on vartija vasta kun se laukeaa."""
    with pytest.raises(AssertionError, match="next_step"):
        fetch_stage._result(
            UNIT,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason="jotain meni pieleen",
            started=0.0,
            stats={"downloaded_bytes": 0},
        )


def test_an_unknown_cause_does_not_default_to_run_it_again() -> None:
    """Oletusneuvo oli molempien live-vikojen juurisyy.

    Tuntemattoman vian oikea neuvo on sanoa, ettei sitä tiedetä -- ei arvata
    että uudelleenajo auttaa.
    """
    assert "aja komento uudelleen" not in fetch_stage.DEFAULT_NEXT_STEP.lower()
    assert fetch_stage.next_step(ValueError("x")) == fetch_stage.DEFAULT_NEXT_STEP


def test_the_next_step_is_read_from_the_error_not_guessed() -> None:
    """Neuvo tulee siitä paikasta, jossa syy tiedetään."""
    exc = ApiError("x", status_code=418, advice="Keitä teetä.")
    assert fetch_stage.next_step(exc) == "Keitä teetä."
