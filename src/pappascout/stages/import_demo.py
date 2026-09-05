"""``import`` -- käsin ladattu demo arkistoon (Story 3.6).

Sama lopputulos kuin ``fetch``illä, eri lähde::

    import/{match_id}-{map_no}-{instance}.dem.zst
        ->  <demos_dir>/<map_demo_id>.dem.zst
            <demos_dir>/<map_demo_id>.meta.json

Vaihe on olemassa siksi, että **verkkopolku voi olla suljettu**. Mitattu
2026-09-05: Downloads API -oikeutta ei ole (hakemus jonossa, arvio ~25.9.), ja
``fetch`` on valmis muttei pääse verkkoon. Selaimella kirjautuneena demon saa
silti, ja tämä vaihe ottaa sen vastaan.

Kuusi sääntöä, jotka tämä moduuli pitää voimassa
------------------------------------------------

**Vajaa demo ei pääse arkistoon, eikä sen lähdettä poisteta.** Tämä on
moduulin tärkein sääntö, koska sen rikkoutuminen maksaa korvaamatonta
aineistoa: ``import/``issa on kuusi kauden 12 liigademoa, joita FACEIT ei enää
tarjoa. Mitattu 2026-09-05: puoliväliin katkaistu ``.dem.zst`` purkautuu
**hiljaa** 104 464 384 tavuksi (kehys ilmoittaa 208 561 416), ja purettu alku
on kelvollinen CS2-demo, jonka otsikosta kartan nimikin luetaan oikein. Vajaus
ei siis näy mistään, mitä tiedostosta katsomalla voisi todeta. Vartijoita on
kolme ja ne vastaavat eri kysymykseen:

``Oliko lähde kokonainen?``
    :func:`~pappascout.adapters.decompress.declared_size` -- zstd-kehyksen
    ilmoittama purettu koko, jota purku vertaa tulokseen. Gzipillä sama
    tehtävä on virran lopetusmerkillä. Pakkaamattomalla ``.dem``:llä ei ole
    kumpaakaan, ja silloin ``length_verified`` on **epätosi** -- ei tosi.
``Muuttuiko lähde kesken siirron?``
    Koko luetaan ennen kopiota ja sen jälkeen. Explorer kirjoittaa lopullisella
    nimellä kesken kopioinnin ja OneDrive synkronoi taustalla, joten kasvava
    tiedosto on arkinen tilanne eikä poikkeus.
``Vastaako kopio lähdettä?``
    Kirjoitettu tavumäärä vastaan lähteen koko. Vasta tämän jälkeen
    lähdetiedosto saa kadota.

**Tuotu demo on erottamaton ladatusta.** Tiedostonimi, hakemisto,
metatiedoston kentät ja kirjoitusjärjestys ovat samat kuin ``fetch``illä;
ainoa ero on ``source``-kentän arvo, ja se on **jäljitettävyystietoa eikä
ohjausta**. Mikään myöhempi vaihe ei lue sitä.

**Tiedostonimen on kuvattava sisältöä.** Pääte päätetään **taikatavuista**
eikä annetusta nimestä. Pakkaamaton ``.dem`` ei saa päätyä arkistoon nimellä
``.dem.zst``: nimi on se, mistä jokainen myöhempi lukija päättelee miten
tiedosto avataan.

**Karttanimi on havainto, vetotieto on ristiintarkistus.** Poikkeama on
vahvistuskysymys, jota ``--kylla`` ei ohita, ja se on projektin ainoa kohta
jossa lippu ei ohita kysymystä: väärin nimetty demo ei kaada mitään, se
pilaisi raportin hiljaa.

**Kirjoitus menee sinne, missä demo jo on.** Sama sääntö kuin
``stages.fetch``issä ja samasta syystä: jos tuonti kirjoittaisi aina
``demos_dir()``iin, arkistossa (OneDrive, jaettu) oleva demo jäisi paikalleen
ja sen viereen syntyisi toinen kopio -- tai pahemmin, se poistettaisiin
"korvattuna", vaikka kyse on eri hakemistosta eikä eri tiedostosta.

**Demo ensin, metatiedosto vasta sitten, ja tiiviste lasketaan kerran.**

Mitä tämä vaihe **ei** tee
--------------------------
Se ei lataa mitään: ainoa ulospäin menevä kutsu on ottelun vetotiedon haku
``MatchSource``ilta, ja se tulee välimuistista. Se ei kysy mitään -- kysymykset
ovat :class:`ImportPlan`in kentässä ja komentorivi esittää ne. Se ei poista
``import/``ista muuta kuin sen tiedoston, jonka se juuri siirsi. Se ei tuo
``match_id``:ttömiä demoja: niille ei voi johtaa FACEIT-muotoista
``map_demo_id``:tä, ja oma tunnistekäytäntö on oma päätöksensä eikä tämän
sivuvaikutus.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from pappascout.adapters.decompress import (
    DEMO_MAGIC,
    GZIP_MAGIC,
    ZSTD_MAGIC,
    declared_size,
)
from pappascout.adapters.protocols import DemoParser, Match, MatchSource
from pappascout.archive.atomic_write import atomic_path, atomic_write_json
from pappascout.archive.paths import DEMO_SUFFIXES, ArchivePaths, safe_component
from pappascout.domain.models import Settings
from pappascout.domain.selection import map_demo_id as build_map_demo_id
from pappascout.errors import ApiError, PappascoutError, ParseError
from pappascout.stages import StageResult
from pappascout.stages.fetch import DISK_RESERVE_BYTES, size_fi

__all__ = [
    "STAGE",
    "DEMO_SOURCE",
    "MAX_MAP_NO",
    "Confirmation",
    "ImportPlan",
    "plan",
    "run",
    "candidates",
    "target_suffix",
    "same_map",
    "length_source",
    "unanswered",
    "default_source",
    "default_parser",
]

STAGE = "import"

#: ``.meta.json``in ``source``-kentän arvo tälle vaiheelle.
#:
#: Toinen mahdollinen arvo on ``downloads_api``
#: (:data:`~pappascout.stages.fetch.DEMO_SOURCE`). Kenttä on olemassa, jotta
#: eron **näkee jälkikäteen** -- mutta mikään putken vaihe ei saa käyttäytyä
#: eri tavalla sen mukaan.
DEMO_SOURCE = "import"

#: Suurin kartan numero, jonka ``--map`` hyväksyy.
#:
#: **Yläraja on olemassa siksi, että ilman vetotietoa ei ole muuta rajaa.**
#: Kun ottelun ``map_picks`` on tyhjä (tuleva ottelu, vetotietoa ei ole),
#: mikään ei muuten estäisi arvoa ``--map 99``, ja arkistoon syntyisi
#: ``{match_id}-98`` -- tunniste, jota mikään vaihe ei osaa liittää mihinkään.
#: Yhdeksän on sama luku ja sama peruste kuin
#: ``adapters.faceit.MAX_BEST_OF``illa: se kattaa BO1:n, BO3:n, BO5:n ja
#: BO7:n sekä yhden tuntemattoman muodon. Vakio on tässä eikä tuotuna, koska
#: adapteri on portin toisella puolella eikä vaihe saa tuoda sitä.
MAX_MAP_NO = 9

#: Taikatavut ja niitä vastaava arkiston pääte.
_SUFFIX_BY_MAGIC: tuple[tuple[bytes, str], ...] = (
    (ZSTD_MAGIC, ".dem.zst"),
    (GZIP_MAGIC, ".dem.gz"),
    (DEMO_MAGIC, ".dem"),
)

#: Järjestys, jossa monitulkintaisen tilanteen ehdokkaita suositellaan.
#:
#: **Pakattu ensin, ja se on tilaneuvo eikä makuasia.** Mitattu 2026-09-05:
#: sama ottelu on tuontikansiossa sekä ``.dem`` (233 118 010 tavua) että
#: ``.dem.zst`` (169 443 262 tavua). Aakkosjärjestys nimeäisi aina
#: pakkaamattoman, eli neuvo ohjaisi kirjoittamaan OneDriveen 64 MB enemmän
#: joka kerta -- eikä kukaan huomaisi, koska neuvo näyttää neutraalilta.
_SUFFIX_PREFERENCE: tuple[str, ...] = (".dem.zst", ".dem.gz", ".dem")

#: Siirron lohkokoko.
_COPY_CHUNK = 1024 * 1024


# -- Suunnitelma -------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """Yksi kysymys, joka on esitettävä ennen kuin mitään siirretään.

    **Kysymykset ovat dataa eivätkä kutsuja**, koska vaihe ei saa tulostaa
    eikä lukea näppäimistöä: sama vaihe ajetaan myöhemmin web-kuoren takaa, ja
    ``input()`` vaiheen sisällä jumittaisi sen.

    Attributes:
        question: Kysymys suomeksi, vastattavissa kyllä/ei.
        detail: Se havainto, jonka takia kysytään. Ilman sitä kysymys olisi
            vastaamiskelvoton -- käyttäjä ei voi punnita poikkeamaa, jota
            hänelle ei näytetä.
        forced: Onko kysymys esitettävä **myös ``--kylla``-lipulla**. Tosi vain
            karttatarkistuksella, ja se on epicin oma vaatimus.
    """

    question: str
    detail: str
    forced: bool = False


@dataclass(frozen=True)
class ImportPlan:
    """Mitä yksi tuonti aikoo tehdä -- **ennen kuin se tekee mitään**.

    Attributes:
        map_demo_id: Arkiston tunniste ``{match_id}-{map_index}``.
        match_id: Ottelun tunniste.
        map_index: Kartan **0-pohjainen** järjestysluku.
        source_path: Tiedosto, josta tuodaan.
        target_path: Tiedosto, joka syntyy. Pääte on päätetty taikatavuista ja
            hakemisto sen mukaan, missä saman tunnisteen demo jo on.
        meta_path: Metatiedosto, aina demon vieressä.
        replaces: Saman tunnisteen demo **samassa hakemistossa mutta eri
            päätteellä**, jonka tämä tuonti korvaa, tai ``None``. Ei koskaan
            toisessa hakemistossa oleva tiedosto eikä koskaan ``import/``in
            tiedosto -- kumpikin olisi eri tiedosto eikä korvattava versio.
        orphan_meta: Metatiedosto, joka jäisi kuvaamaan tiedostoa jota ei ole,
            tai ``None``.
        move: Siirretäänkö (tosi) vai kopioidaanko (epätosi).
        size_bytes: Lähdetiedoston koko suunnitelman tekohetkellä. Sama luku
            tarkistetaan uudelleen siirron molemmissa päissä.
        declared_bytes: Purettu koko, jonka tiedosto itse ilmoittaa, tai
            ``None``.
        length_verified: Voitiinko lähteen **kokonaisuus** todeta. Epätosi
            pakkaamattomalla ``.dem``:llä, jolla ei ole yhtään riippumatonta
            pituuslähdettä.
        header_map_name: Kartan nimi demon otsikosta **havaintona**.
        expected_map_name: Kartan nimi FACEITin vetotiedosta, tai ``None``.
        map_picks: Ottelun koko vetotieto, jotta poikkeaman selitys voi kertoa
            **mikä numero olisi oikea** eikä vain että tämä on väärä.
        confirmations: Kysymykset siinä järjestyksessä kuin ne esitetään.
    """

    map_demo_id: str
    match_id: str
    map_index: int
    source_path: Path
    target_path: Path
    meta_path: Path
    replaces: Path | None = None
    orphan_meta: Path | None = None
    move: bool = True
    size_bytes: int = 0
    declared_bytes: int | None = None
    length_verified: bool = False
    header_map_name: str | None = None
    expected_map_name: str | None = None
    map_picks: tuple[str, ...] = ()
    confirmations: tuple[Confirmation, ...] = ()

    @property
    def map_no(self) -> int:
        """Kartan numero **käyttäjän kielellä** eli 1-pohjaisena."""
        return self.map_index + 1

    @property
    def map_matches(self) -> bool:
        """Vastaako otsikon kartta vetotietoa? Tuntematon ei ole vastaavuus."""
        if self.header_map_name is None or self.expected_map_name is None:
            return False
        return same_map(self.header_map_name, self.expected_map_name)


def plan(
    archive: ArchivePaths,
    match_id: str,
    map_no: int | str,
    *,
    source: MatchSource,
    parser: DemoParser,
    file: Path | str | None = None,
    reserve_bytes: int = DISK_RESERVE_BYTES,
    disk_free: Callable[[Path], int | None] | None = None,
) -> ImportPlan:
    """Ratkaise tuonti kokonaan -- **koskematta yhteenkään tiedostoon**.

    Työjärjestys on halvin ensin: käyttäjän antamat luvut, sitten ottelun
    vetotieto välimuistista, sitten lähdetiedoston haku, sitten levytila, ja
    **vasta viimeisenä** otsikon luku. Viimeinen on ainoa kallis askel
    (pakattu demo puretaan kokonaan väliaikaistiedostoon), eikä sitä pidä
    tehdä tuonnille, joka kaatuu numerovirheeseen -- eikä levylle, jolle
    purettu demo ei mahdu.

    Args:
        archive: Arkiston polut.
        match_id: FACEITin ottelutunniste.
        map_no: Kartan numero **1-pohjaisena**. Merkkijono kelpaa, koska
            komentoriviltä tuleva arvo voi olla mitä tahansa ja väärän arvon
            on saatava suomenkielinen virhe eikä kirjaston oma.
        source: :class:`~pappascout.adapters.protocols.MatchSource`-portti.
        parser: :class:`~pappascout.adapters.protocols.DemoParser`-portti.
        file: Lähdetiedosto nimenomaisesti, tai ``None``.
        reserve_bytes: Levytilan varmuusvara.
        disk_free: Vapaan tilan kysyjä, tai ``None`` = :func:`_free_space`.

    Returns:
        :class:`ImportPlan`.

    Raises:
        ~pappascout.errors.PappascoutError: Jos tuontia ei voi tehdä lainkaan.
            Jokainen tällainen virhe kantaa oman neuvonsa; ks. :func:`_reject`.
    """
    free = disk_free if disk_free is not None else _free_space
    match_id = _match_id(match_id)
    map_index = _map_index(map_no)
    unit = _unit(match_id, map_index)

    expected, picks = _veto(source, match_id, map_index)
    source_path, move = _source_file(archive, match_id, map_index, file)

    suffix = target_suffix(source_path)
    size_bytes = _size(source_path)

    existing = _existing_demo(archive, unit)
    if existing is not None and _same_file(existing, source_path):
        raise _reject(
            f"Lähde ja kohde ovat sama tiedosto ({source_path}), joten "
            "tuotavaa ei ole.",
            advice=(
                f"Demo {unit} on jo paikallaan. Aja sille suoraan: "
                f"uv run pappascout parse {unit}"
            ),
        )

    # **Kirjoitus menee sinne, missä demo jo on** (sama sääntö kuin
    # ``stages.fetch``issä). Jos kohde valittaisiin aina ``demos_dir()``in
    # mukaan, arkistossa oleva demo jäisi paikalleen ja saisi viereensä
    # toisen kopion -- tai poistettaisiin "korvattuna", vaikka kyse on eri
    # hakemistosta eikä eri tiedostosta.
    target_dir = existing.parent if existing is not None else archive.demos_dir()
    target = target_dir / f"{unit}{suffix}"
    meta = target_dir / f"{unit}.meta.json"

    declared = declared_size(source_path)
    _check_space(
        target_dir, size_bytes, declared, reserve_bytes=reserve_bytes, free=free
    )

    header = _read_map_name(parser, source_path)

    confirmations: list[Confirmation] = []
    cross_check = _cross_check(unit, map_index, header, expected, picks)
    if cross_check is not None:
        confirmations.append(cross_check)
    if existing is not None:
        confirmations.append(_overwrite_question(unit, existing))

    existing_meta = _existing_meta(archive, unit)
    return ImportPlan(
        map_demo_id=unit,
        match_id=match_id,
        map_index=map_index,
        source_path=source_path,
        target_path=target,
        meta_path=meta,
        replaces=existing if existing is not None and existing != target else None,
        orphan_meta=(
            existing_meta
            if existing_meta is not None and existing_meta != meta
            else None
        ),
        move=move,
        size_bytes=size_bytes,
        declared_bytes=declared,
        length_verified=length_source(source_path) is not None,
        header_map_name=header,
        expected_map_name=expected,
        map_picks=tuple(picks),
        confirmations=tuple(confirmations),
    )


# -- Siirto ------------------------------------------------------------------


def run(
    archive: ArchivePaths,
    todo: ImportPlan,
    *,
    now: Callable[[], datetime] | None = None,
) -> StageResult:
    """Siirrä suunnitelman demo paikalleen ja kirjoita sen metatiedosto.

    **Kysymyksiä ei esitetä täällä eikä niiden vastauksia tarkisteta.** Luvan
    kysyminen kuuluu komentoriville, joka on ainoa kerros jolla on käyttäjä.

    Args:
        archive: Arkiston polut.
        todo: :func:`plan`in tuottama suunnitelma.
        now: Kello ``fetched_at``-kenttää varten.

    Returns:
        :class:`~pappascout.stages.StageResult`, jonka ``status`` on ``ok``.
        Muuta tilaa ei ole: yksiköitä on yksi, ja sen epäonnistuminen on ajon
        epäonnistuminen.

    Raises:
        ~pappascout.errors.PappascoutError: Jos siirto epäonnistuu. **Myös
            levyvirheet**: täysi levy ja OneDriven tiedostolukko ovat
            käyttäjän tilanteita eivätkä ohjelmavirheitä, ja ilman käännöstä
            ne näkyisivät ruudulla tekstinä "Odottamaton virhe: [Errno 28]"
            ja neuvona "Tämä on ohjelmavirhe" -- eli väärä diagnoosi ja väärä
            toimenpide.
    """
    started = time.perf_counter()
    clock = now if now is not None else (lambda: datetime.now(UTC))

    try:
        return _run(archive, todo, clock, started)
    except OSError as exc:
        raise _reject(
            f"Demon {todo.map_demo_id} tuonti epäonnistui levyvirheeseen "
            f"({type(exc).__name__}: {exc}).\n"
            f"Kohde oli {todo.target_path}.\n"
            "Tavallisimmat syyt: levy täyttyi kesken kirjoituksen, OneDrive "
            "piti tiedostoa lukittuna, tai verkkolevy katkesi.\n"
            f"Lähdetiedostoon {todo.source_path} ei koskettu.",
            advice=(
                "Vapauta levytilaa tai odota kunnes OneDrive vapauttaa "
                "tiedostolukon, ja aja komento sitten uudelleen."
            ),
        ) from exc


def _run(
    archive: ArchivePaths,
    todo: ImportPlan,
    clock: Callable[[], datetime],
    started: float,
) -> StageResult:
    """Ks. :func:`run`. Erillään vain, jotta ``OSError``-kääre on yksi lohko."""
    digest, size = _transfer(todo.source_path, todo.target_path, todo.size_bytes)

    # **Vasta tässä.** Demo on paikallaan ja luettu loppuun; metatiedosto on
    # väite juuri tästä tiedostosta, ja se saa syntyä vasta kun tiedosto on.
    atomic_write_json(
        todo.meta_path,
        {
            "map_demo_id": todo.map_demo_id,
            "sha256": digest,
            "size": size,
            "source": DEMO_SOURCE,
            # **``fetched_at`` eikä ``imported_at``.** Oma nimi tekisi tuodusta
            # demosta tunnistettavan metatiedoston rakenteesta, ja
            # tunnistettavuudesta seuraa ennen pitkää haara. Ero on
            # ``source``-kentässä ja vain siellä.
            "fetched_at": clock().isoformat(),
            # **Sama kenttä kuin ``fetch``illä ja samassa merkityksessä:**
            # voitiinko lähteen kokonaisuus todeta riippumatonta lukua vasten.
            # Pakkaamattomalla ``.dem``:llä sellaista lukua ei ole, ja silloin
            # tämä on epätosi -- vaiettu epävarmuus näyttäisi varmuudelta.
            "length_verified": todo.length_verified,
        },
    )

    notes: list[str] = []
    if todo.replaces is not None:
        notes.append(_replaced_note(todo))
    if todo.orphan_meta is not None:
        notes.append(_orphan_note(todo))
    notes.append(_source_note(todo))
    if not todo.length_verified:
        notes.append(_unverified_note(todo))
    if not todo.map_matches:
        notes.append(_mismatch_note(todo))

    return StageResult(
        stage=STAGE,
        unit=todo.map_demo_id,
        status="ok",
        skipped=False,
        outputs=_archive_outputs(archive, todo.target_path, todo.meta_path),
        # **Ei manifestia**, samasta syystä kuin ``fetch``issä.
        manifest_path=None,
        reason=" ".join(notes) if notes else None,
        duration_s=time.perf_counter() - started,
        stats={
            "map_demo_id": todo.map_demo_id,
            "sha256": digest,
            "size": size,
            "imported_bytes": size,
            # **Nimi on ``demo_source`` eikä ``source``, ja se on vartijan
            # ehto.** ``tests/test_stage_import.py`` kieltää metatiedoston
            # ``source``-kentän **lukemisen** koko lähdekoodista -- ja kielto,
            # jolla on poikkeuslista, ei ole kielto.
            "demo_source": DEMO_SOURCE,
            "moved": todo.move,
            "length_verified": todo.length_verified,
            "declared_bytes": todo.declared_bytes,
            "demo_path": str(todo.target_path),
            "meta_path": str(todo.meta_path),
            "demos_dir": str(todo.target_path.parent),
            "source_path": str(todo.source_path),
            "header_map_name": todo.header_map_name,
            "expected_map_name": todo.expected_map_name,
            "map_matches": todo.map_matches,
            "notes": tuple(notes),
        },
    )


# -- Portit ------------------------------------------------------------------


def default_source(settings: Settings, archive: ArchivePaths) -> MatchSource:
    """Tuotannon FACEIT-toteutus otteluportille.

    Sama asiakas ja sama välimuisti kuin ``discover``illa: vetotieto on jo
    haettu, eikä tuonti saa tehdä siitä toista kutsua eikä toista
    välimuistiavainta. Tuonti on funktion sisällä, jotta tämän moduulin
    tuominen ei lataa ``requests``ia.

    **Downloads-tokenia ei tarvita.** Tämä komento ei lataa mitään, joten sen
    puuttuminen ei saa estää tuontia -- se on juuri se tilanne, jota varten
    koko vaihe on olemassa.
    """
    from pappascout.adapters.faceit import FaceitClient

    return FaceitClient.from_settings(settings, archive.raw_faceit())


def default_parser() -> DemoParser:
    """Tuotannon demoparser2-toteutus otsikon lukuun.

    **Ei asetuksia, ja se on sopimus eikä laiskuus.** ``[parse]``-osion arvot
    ohjaavat ensikontaktia, näytepisteitä ja pistepilveä -- eli sitä työtä,
    jota tämä komento ei tee. Osion antaminen tänne sitoisi tuonnin
    asetuksiin, jotka eivät voi vaikuttaa sen tulokseen (AD-3).
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    return Demoparser2Adapter()


# -- Lähdetiedoston haku -----------------------------------------------------


def candidates(
    archive: ArchivePaths, match_id: str, map_index: int
) -> tuple[Path, ...]:
    """Tuontikansion tiedostot, jotka voisivat olla tämä kartta.

    Haku tehdään **FACEITin omalla nimikaavalla** ``{match_id}-{round}-*``,
    jossa ``round`` on 1-pohjainen kartan numero -- eli ``map_index + 1``.

    **Tässä on kirjattava oletus (Story 3.6, katselmuskohta B1).** Kaava
    olettaa, että FACEITin ``instances[].round`` on sama luku kuin
    ``voting.map.pick`` -listan positio + 1. ``mittaus-faceit-aineisto.md``
    luku 9 sanoo, että ``round`` on **luettu arvo eikä listapositio**, ja että
    2-0 päättyneen BO3:n pick-listan sisältö on **yhä mittaamatta** -- jos
    pelaamaton kartta jää pick-listaan, positio ja ``round`` eroavat. Oikea
    korjaus olisi kantaa ``instances``
    :class:`~pappascout.adapters.protocols.Match`in läpi, mitä portti ei tällä
    hetkellä tee; se on porttimuutos eikä tämän tarinan asia. Siihen asti
    oletus on tässä **näkyvänä**, ja karttatarkistus on se, joka kaataa
    turvalliseen suuntaan: väärä positio tuottaa poikkeaman, ja poikkeama on
    kysymys jota ``--kylla`` ei ohita -- kysymys, joka lisäksi kertoo millä
    numerolla otsikon kartta vedosta löytyy (:func:`_other_pick`).

    Palauttaa **kaikki** osumat eikä valitse niistä. Mitattu 2026-09-05: yksi
    ottelu on tuontikansiossa sekä ``.dem`` että ``.dem.zst`` -muodossa.
    """
    directory = archive.import_dir()
    if not directory.is_dir():
        return ()
    pattern = f"{safe_component(match_id, 'match_id')}-{map_index + 1}-*"
    return tuple(
        sorted(
            path
            for path in directory.glob(pattern)
            if path.is_file() and _is_demo_name(path.name)
        )
    )


def target_suffix(path: Path) -> str:
    """Arkiston pääte tiedostolle **sen sisällön perusteella**.

    Taikatavut eivätkä annettu nimi: käsin ladattu tiedosto voi olla nimetty
    miten tahansa, ja nimi on se, mistä jokainen myöhempi lukija päättelee
    miten tiedosto avataan.

    Returns:
        ``".dem.zst"``, ``".dem.gz"`` tai ``".dem"``.

    Raises:
        ~pappascout.errors.PappascoutError: Jos alkutavut eivät ole minkään
            tunnetun muodon.
    """
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(DEMO_MAGIC))
    except OSError as exc:
        raise _reject(
            f"Tiedostoa {path} ei voitu avata: {exc}",
            advice=(
                "Tarkista polku ja se, ettei tiedosto ole OneDriven "
                "pilvipaikkamerkki -- avaa se kerran Resurssienhallinnassa."
            ),
        ) from exc

    for magic, suffix in _SUFFIX_BY_MAGIC:
        if head.startswith(magic):
            return suffix

    raise _reject(
        f"Tiedosto {path.name} ei ole demo eikä pakattu demo: sen alkutavut "
        f"ovat {head!r}.\n"
        f"Odotettiin joko zstd ({ZSTD_MAGIC!r}), gzip ({GZIP_MAGIC!r}) tai "
        f"CS2-demon otsikko ({DEMO_MAGIC!r}).\n"
        "Mitään ei siirretty.",
        advice=(
            "Tarkista, että lataus onnistui ja että tiedosto on FACEITin "
            "demotallenne eikä esimerkiksi kirjautumissivu tai tyhjä tiedosto."
        ),
    )


def length_source(path: Path) -> str | None:
    """Mikä kertoo, oliko lähde **kokonainen**? ``None`` = ei mikään.

    Kolme muotoa, kaksi vastausta:

    ``.dem.zst``
        Kehyksen ``Frame_Content_Size``, jos se on ilmoitettu. Purku vertaa
        tulosta siihen (:func:`~pappascout.adapters.decompress.decompress_to`),
        joten katkennut tiedosto kaatuu ennen kuin sitä siirretään.
    ``.dem.gz``
        Gzip-virran lopetusmerkki. Mitattu 2026-09-05: katkaistu gzip nostaa
        ``EOFError``in purussa, joten sama vartija toimii eri mekanismilla.
    ``.dem``
        **Ei mitään.** Pakkaamattomassa demossa ei ole pituutta, tarkistetta
        eikä lopetusmerkkiä, joten puolikas tiedosto on erottamaton
        kokonaisesta. Oikea vastaus on ``None`` ja ``length_verified: false``
        -- ei tosi. ``fetch``illä sama rooli on ``Content-Length``illä, ja se
        kirjoittaa saman kentän epätodeksi kun otsaketta ei ollut.
    """
    path = Path(path)
    if declared_size(path) is not None:
        return "zstd-kehyksen ilmoittama purettu koko"
    try:
        with open(path, "rb") as handle:
            head = handle.read(len(GZIP_MAGIC))
    except OSError:  # pragma: no cover - riippuu tiedostojärjestelmästä
        return None
    if head.startswith(GZIP_MAGIC):
        return "gzip-virran lopetusmerkki"
    return None


def same_map(observed: str, expected: str) -> bool:
    """Tarkoittavatko otsikon ja vetotiedon karttanimet samaa karttaa?

    Vertailu on kirjainkoosta ja reunavälilyönneistä riippumaton, **mutta ei
    mistään muusta**. Se ei riisu ``de_``-etuliitettä eikä hae synonyymejä:
    mitattu 2026-09-05, että ``voting.map.pick`` antaa nimet samassa muodossa
    kuin demon otsikko (``de_ancient``, ``de_nuke``), joten kaikki muu
    "siivous" olisi arvausta -- ja arvaus on juuri se, mitä tämä tarkistus on
    olemassa estämään.
    """
    return observed.strip().casefold() == expected.strip().casefold()


def unanswered(
    confirmations: Sequence[Confirmation], *, kylla: bool
) -> tuple[Confirmation, ...]:
    """Kysymykset, jotka on **esitettävä** annetulla lipulla.

    Yksi paikka eikä ehto kutsupaikassa, koska sääntö on epicin oma vaatimus
    ja siksi juuri se, jonka on oltava testattavissa yksinään: ``--kylla``
    ohittaa kysymykset **paitsi** ne, joissa ``forced`` on tosi.
    """
    if not kylla:
        return tuple(confirmations)
    return tuple(item for item in confirmations if item.forced)


# -- Sisäiset: torjunnat -----------------------------------------------------


def _reject(message: str, *, advice: str) -> PappascoutError:
    """Torjunta, joka **ei voi syntyä ilman neuvoa**.

    Sama vartija kuin ``stages.fetch._result``illa ja samasta juurisyystä
    (Story 3.4): kun neuvo tulee otsikosta tai oletuksesta, jokainen uusi
    vikaluokka perii sen hiljaa -- ja peritty neuvo on neuvo, jota kukaan ei
    ole harkinnut juuri tälle vialle.

    **Jokaisen tästä moduulista nousevan virheen on kuljettava tämän kautta.**
    Story 3.6:n katselmus löysi polun, joka ei kulkenut: tunniste rakennettiin
    ``safe_component``illa suoraan, ja ``archive.paths`` nostaa oman virheensä
    ilman neuvoa. Ks. :func:`_unit`.
    """
    if not advice.strip():
        raise AssertionError(
            f"Torjunta ilman neuvoa: {message!r}. Epäonnistuminen ilman "
            "seuraavaa toimenpidettä jättäisi käyttäjän arvaamaan."
        )
    return PappascoutError(message, advice=advice)


def _match_id(value: str) -> str:
    """Tarkista ottelutunniste polun osaksi kelpaavaksi."""
    text = str(value).strip()
    if not text:
        raise _reject(
            "Ottelutunniste puuttuu.",
            advice=(
                "Anna ottelun tunniste: --match 1-<uuid>. Tunnisteet ovat "
                "index/matches.json-tiedostossa."
            ),
        )
    try:
        return safe_component(text, "match_id")
    except PappascoutError as exc:
        raise _reject(
            str(exc),
            advice=(
                "Kopioi tunniste sellaisenaan index/matches.json-tiedostosta "
                "tai FACEITin ottelusivun osoitteesta."
            ),
        ) from exc


def _map_index(map_no: int | str) -> int:
    """``--map`` 1-pohjaisesta 0-pohjaiseksi, tai suomenkielinen torjunta.

    **Arvo otetaan vastaan merkkijonona, ja se on tarkoituksellista.** Jos
    komentorivi julistaisi sen kokonaisluvuksi, ``--map abc`` kaatuisi
    ``typer``in omaan englanninkieliseen viestiin ennen kuin tämä funktio
    näkee mitään -- ja tämän funktion oma tarkistus olisi kuollutta koodia,
    saavuttamattomissa komentoriviltä. Story 3.6:n katselmus löysi täsmälleen
    sen tilan.

    Numerointi alkaa **käyttäjälle** ykkösestä ja **tallenteissa** nollasta.
    Nolla on siksi tyypillisin virhe, ja sen on saatava oma lauseensa.
    Yläraja on :data:`MAX_MAP_NO`; ks. sen perustelu.
    """
    try:
        number = int(str(map_no).strip())
    except (TypeError, ValueError):
        raise _reject(
            f"Kartan numero {map_no!r} ei ole kokonaisluku.",
            advice="Anna kartan numero numerona, esimerkiksi --map 1.",
        ) from None
    if number < 1:
        raise _reject(
            f"Kartan numero on {number}, mutta numerointi alkaa ykkösestä: "
            "ottelun ensimmäinen kartta on --map 1.",
            advice="Aja komento uudelleen arvolla --map 1 tai suuremmalla.",
        )
    if number > MAX_MAP_NO:
        raise _reject(
            f"Kartan numero on {number}, mutta ottelussa pelataan enintään "
            f"{MAX_MAP_NO} karttaa.\n"
            "Ilman ottelun vetotietoa numeroa ei voi tarkistaa sitä vasten, "
            "joten tämä on ainoa raja joka siihen on.",
            advice=(
                f"Anna --map väliltä 1..{MAX_MAP_NO}. Jos numero oli oikea, "
                "ottelutunniste on väärä."
            ),
        )
    return number - 1


def _unit(match_id: str, map_index: int) -> str:
    """Rakenna ``map_demo_id`` **domainin kanonisella rakentajalla**.

    :func:`pappascout.domain.selection.map_demo_id` on se paikka, jossa
    tunniste syntyy, ja sen oma dokumentaatio sanoo niin. Tuonti oli Story
    3.6:n katselmuksessa projektin ainoa kohta, joka rakensi tunnisteen ohi
    sen -- ja ohitus maksoi kaksi asiaa: domainin oman ``index < 0``
    -tarkistuksen, ja sen ettei polkutarkistus kulkenut :func:`_reject`in
    kautta. Jälkimmäinen tuotti **aidosti saavutettavan** torjunnan ilman
    neuvoa: 119-merkkinen ``--match`` läpäisee :func:`_match_id`in mutta
    ylittää polun osan pituusrajan tunnistetta rakennettaessa, ja viesti nimesi
    tunnisteen ``map_demo_id``, jota käyttäjä ei antanut.
    """
    try:
        unit = build_map_demo_id(match_id, map_index)
    except ValueError as exc:  # pragma: no cover - _map_index estää tämän
        raise _reject(
            f"Tunnistetta ei voitu rakentaa: {exc}",
            advice="Tarkista --match ja --map.",
        ) from exc
    try:
        return safe_component(unit, "map_demo_id")
    except PappascoutError as exc:
        raise _reject(
            f"Ottelutunnisteesta {match_id!r} ja kartasta {map_index + 1} "
            f"syntyvä arkiston tunniste ei kelpaa tiedostonimeksi: {exc}",
            advice=(
                "Tarkista --match: FACEITin ottelutunniste on muotoa 1-<uuid> "
                "eli 38 merkkiä, eikä siinä ole välilyöntejä eikä "
                "polkuerottimia."
            ),
        ) from exc


# -- Sisäiset: ottelu ja vetotieto -------------------------------------------


def _veto(
    source: MatchSource, match_id: str, map_index: int
) -> tuple[str | None, tuple[str, ...]]:
    """Kartan nimi vetotiedosta **ja koko vetotieto**.

    Molemmat palautetaan, koska poikkeaman selitys tarvitsee listan: jos
    otsikon kartta on vedossa jollain toisella numerolla, se numero on juuri
    se, jonka käyttäjä tarvitsee (:func:`_other_pick`).

    Kutsu menee välimuistin läpi eikä ole demon lataus.

    **Positio-oletus on sama kuin :func:`candidates`illa ja se on kirjattu
    sinne.** Tässä luetaan ``picks[map_index]``, eli oletetaan että vedon
    listapositio vastaa kartan numeroa.

    Returns:
        ``(kartan nimi tai None, koko pick-lista)``. Tyhjä lista tarkoittaa
        "ei vetotietoa", ei "ei karttoja".
    """
    match = _match(source, match_id)
    picks = tuple(match.map_picks)
    if not picks:
        return None, ()
    if map_index >= len(picks):
        listing = ", ".join(f"{i + 1}. {name}" for i, name in enumerate(picks))
        raise _reject(
            f"Ottelussa {match_id} on {len(picks)} karttaa ({listing}), joten "
            f"karttaa {map_index + 1} ei ole olemassa.\n"
            "Mitään ei siirretty.",
            advice=(
                f"Anna --map väliltä 1..{len(picks)}. Jos kartta puuttuu "
                "listalta, ottelun vetotieto on eri kuin luulit -- tarkista "
                "ottelutunniste."
            ),
        )
    return picks[map_index], picks


def _match(source: MatchSource, match_id: str) -> Match:
    """Hae ottelu portilta ja käännä sen puuttuminen käyttäjän virheeksi."""
    try:
        return source.get_match(match_id)
    except ApiError as exc:
        raise _reject(
            f"Ottelua {match_id} ei saatu haettua: {exc}\n"
            "Mitään ei siirretty.",
            advice=(
                "Tarkista ottelutunniste. Tunnetut ottelut ovat arkiston "
                "tiedostossa index/matches.json; jos sitä ei ole, aja ensin "
                "uv run pappascout discover."
            ),
        ) from exc


# -- Sisäiset: lähdetiedosto -------------------------------------------------


def _source_file(
    archive: ArchivePaths,
    match_id: str,
    map_index: int,
    file: Path | str | None,
) -> tuple[Path, bool]:
    """Mistä tuodaan ja siirretäänkö vai kopioidaanko.

    Returns:
        ``(polku, siirretaanko)``. ``import/``-kansion tiedosto siirretään --
        se on saapuvien kansio eikä säilö. Muualta annettu tiedosto
        **kopioidaan**: se on käyttäjän oma tiedosto omassa paikassaan, ja sen
        poistaminen on oma päätöksensä eikä tuonnin sivuvaikutus.
    """
    if file is not None:
        path = Path(file).expanduser()
        if not path.is_file():
            raise _reject(
                f"Tiedostoa {path} ei ole.",
                advice=(
                    "Tarkista polku, tai jätä --file pois niin tiedosto "
                    "etsitään arkiston import-kansiosta."
                ),
            )
        return path, _inside(path, archive.import_dir())

    found = candidates(archive, match_id, map_index)
    if not found:
        raise _reject(
            f"Tuontikansiosta {archive.import_dir()} ei löytynyt tiedostoa "
            f"nimellä {match_id}-{map_index + 1}-*.dem[.zst|.gz].\n"
            f"{_import_listing(archive)}",
            advice=(
                "Kopioi selaimella ladattu demo tuontikansioon alkuperäisellä "
                "nimellään, tai anna sen polku suoraan: --file <polku>."
            ),
        )
    if len(found) > 1:
        listing = "\n".join(f"    {p.name} ({size_fi(_size(p))})" for p in found)
        raise _reject(
            f"Tuontikansiossa on {len(found)} tiedostoa kartalle "
            f"{map_index + 1} eikä niistä voi valita puolestasi:\n"
            f"{listing}\n"
            "Mitään ei siirretty.",
            advice=_pick_advice(found),
        )
    return found[0], True


def _pick_advice(found: Sequence[Path]) -> str:
    """Neuvo, joka on **kopioitavissa sellaisenaan ja ohjaa pakattuun**.

    Kaksi vikaa yhdessä rivissä, ja molemmat osuvat ensimmäiseen oikeaan
    ajoon:

    **Lainausmerkit.** Arkiston polussa on kolme välilyöntiä (``Claude code``,
    ``Finnpark Oy``), joten lainaamaton polku hajoaa komentotulkissa useaksi
    argumentiksi ja tuottaa englanninkielisen ``typer``-virheen. Neuvo, jota
    ei voi kopioida, ei ole neuvo.

    **Järjestys.** Aakkosjärjestys nimeäisi aina ``.dem``:n ennen
    ``.dem.zst``:ää, eli neuvo ohjaisi tuomaan pakkaamattoman -- mitattu
    2026-09-05: sama ottelu on 233 MB pakkaamattomana ja 169 MB pakattuna, ja
    ero jää OneDriveen pysyvästi. Ks. :data:`_SUFFIX_PREFERENCE`.
    """
    ordered = sorted(found, key=_suffix_rank)
    return f'Kerro kumpi tuodaan: --file "{ordered[0]}"'


def _suffix_rank(path: Path) -> int:
    """Sijoitus :data:`_SUFFIX_PREFERENCE`issä; tuntematon viimeiseksi."""
    lowered = path.name.lower()
    for index, suffix in enumerate(_SUFFIX_PREFERENCE):
        if lowered.endswith(suffix):
            return index
    return len(_SUFFIX_PREFERENCE)


def _import_listing(archive: ArchivePaths) -> str:
    """Mitä tuontikansiossa on -- **nimeltä, ei lukumääränä**."""
    directory = archive.import_dir()
    if not directory.is_dir():
        return f"Tuontikansiota {directory} ei ole vielä olemassa."
    names = sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file() and _is_demo_name(path.name)
    )
    if not names:
        return "Tuontikansiossa ei ole yhtään demotiedostoa."
    listing = "\n".join(f"    {name}" for name in names)
    return f"Tuontikansiossa on nämä demot:\n{listing}"


def _read_map_name(parser: DemoParser, path: Path) -> str | None:
    """Kartan nimi otsikosta, ja lukuvirhe **neuvon kanssa**.

    Tämä on myös se kohta, jossa katkennut ``.dem.zst`` kaatuu: purku vertaa
    tulosta kehyksen ilmoittamaan kokoon, ja ero nousee ``ParseError``ina
    **ennen kuin mitään on siirretty**. Vajaan demon oma neuvo tulee purusta ja
    on eri kuin tämän yleinen neuvo ("odota kunnes kopiointi on valmis" vs.
    "tarkista että tiedosto on demo"), joten se säilytetään sellaisenaan.
    """
    try:
        return parser.read_map_name(path)
    except ParseError as exc:
        advice = getattr(exc, "advice", None)
        raise ParseError(
            f"Tiedostosta {path.name} ei saatu luettua demon otsikkoa:\n{exc}\n"
            "Mitään ei siirretty, eikä lähdetiedostoon koskettu.",
            advice=advice
            or (
                "Tarkista, että lataus onnistui kokonaan ja että tiedosto on "
                "CS2-demo. Odota tarvittaessa kunnes OneDriven synkronointi on "
                "valmis."
            ),
        ) from exc


# -- Sisäiset: arkiston nykytila ---------------------------------------------


def _writable_demo_dirs(archive: ArchivePaths) -> tuple[Path, ...]:
    """Hakemistot, joissa **arkiston hallitsema** demo voi olla.

    Sama järjestys kuin
    :meth:`~pappascout.archive.paths.ArchivePaths.demo_dirs`illa mutta
    **ilman ``import/``ia**, ja se ero on koko funktion syy. ``demo_dirs`` on
    hakujärjestys ``parse``a varten: sille käsin kannettu demo tuontikansiossa
    on yhtä hyvä kuin arkistossa oleva. Tuonnille se on eri asia. ``import/``
    on saapuvien kansio, ei kohde:

    * sinne ei kirjoiteta, joten se ei voi olla ``target_dir``,
    * siellä oleva samanniminen tiedosto ei ole "korvattava versio" vaan
      toinen tiedosto, jota tuonti ei omista -- ja ``--file``illä muualta
      tuotaessa vanha koodi olisi **poistanut sen**, mikä rikkoo speksin
      Never-sääntöä "ei kirjoiteta muualle kuin demos/iin".
    """
    return tuple(
        directory
        for directory in archive.demo_dirs()
        if directory != archive.import_dir()
    )


def _existing_demo(archive: ArchivePaths, unit: str) -> Path | None:
    """Saman tunnisteen demo arkiston hallitsemissa hakemistoissa."""
    for directory in _writable_demo_dirs(archive):
        for suffix in DEMO_SUFFIXES:
            candidate = directory / f"{unit}{suffix}"
            if candidate.is_file():
                return candidate
    return None


def _existing_meta(archive: ArchivePaths, unit: str) -> Path | None:
    """Saman tunnisteen metatiedosto arkiston hallitsemissa hakemistoissa."""
    for directory in _writable_demo_dirs(archive):
        candidate = directory / f"{unit}.meta.json"
        if candidate.is_file():
            return candidate
    return None


# -- Sisäiset: levytila ------------------------------------------------------


def _free_space(directory: Path) -> int | None:
    """Vapaa tila **lähimmältä olemassa olevalta hakemistolta**.

    Sama kuvio kuin ``stages.fetch.free_space``illa: kohdehakemisto voi syntyä
    vasta kirjoituksen myötä, eikä olemattoman polun tilaa voi kysyä. ``None``
    tarkoittaa "ei saatu selville" eikä nollaa -- tuntematon tila ei saa estää
    tuontia.
    """
    path = Path(directory)
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:  # pragma: no cover - riippuu tiedostojärjestelmästä
        return None


def _check_space(
    target_dir: Path,
    size_bytes: int,
    declared: int | None,
    *,
    reserve_bytes: int,
    free: Callable[[Path], int | None],
) -> None:
    """Tila **kahdelle** eri kirjoitukselle, ja ne voivat olla eri levyillä.

    Tuonti kirjoittaa satoja megatavuja kahdesti, ja vain toinen niistä on
    ilmeinen:

    1. **TEMP**, kun otsikko luetaan: pakattu demo puretaan kokonaan koneen
       omaan temp-hakemistoon. Purettu koko on 208-316 MB, ja se on tiedossa
       etukäteen (:func:`~pappascout.adapters.decompress.declared_size`).
    2. **Kohdehakemisto**, kun tiedosto kopioidaan paikalleen.

    Ilman tarkistusta täysi levy tuottaa ``OSError``in kesken kirjoituksen ja
    ruudulle tekstin "Odottamaton virhe: [Errno 28]" -- ja jos TEMP täyttyy
    otsikon luvussa, purun oma neuvo on "lataa demo uudelleen", mikä lähettää
    käyttäjän hakemaan 230 MB:n tiedostoa joka on kunnossa. Molemmat ovat
    väärä diagnoosi ja väärä toimenpide.
    """
    if declared is not None:
        temp_dir = Path(tempfile.gettempdir())
        available = free(temp_dir)
        if available is not None and available < declared + reserve_bytes:
            raise _reject(
                "Levytila ei riitä demon otsikon lukemiseen, joten tuontia ei "
                "aloitettu.\n"
                f"Pakattu demo puretaan väliaikaisesti hakemistoon {temp_dir}, "
                f"ja purettuna se on {size_fi(declared)}.\n"
                f"Vapaana on {size_fi(available)} ja tarvitaan "
                f"{size_fi(declared + reserve_bytes)} varmuusvaroineen.\n"
                "Lähdetiedostoon ei koskettu.",
                advice=(
                    f"Vapauta levytilaa hakemiston {temp_dir} levyltä tai "
                    "osoita TEMP toiselle levylle, ja aja komento uudelleen."
                ),
            )

    available = free(target_dir)
    if available is not None and available < size_bytes + reserve_bytes:
        raise _reject(
            f"Levytila ei riitä demon tuontiin hakemistoon {target_dir}, joten "
            "tuontia ei aloitettu.\n"
            f"Vapaana on {size_fi(available)} ja tarvitaan "
            f"{size_fi(size_bytes + reserve_bytes)} (demo {size_fi(size_bytes)} "
            f"+ varmuusvara {size_fi(reserve_bytes)}).\n"
            "Lähdetiedostoon ei koskettu.",
            advice=(
                "Vapauta vähintään "
                f"{size_fi(size_bytes + reserve_bytes - available)} levytilaa "
                "ja aja komento uudelleen."
            ),
        )


# -- Sisäiset: kysymykset ----------------------------------------------------


def _cross_check(
    unit: str,
    map_index: int,
    header: str | None,
    expected: str | None,
    picks: Sequence[str],
) -> Confirmation | None:
    """Kysymys karttatäsmäyksestä, tai ``None`` jos kaikki täsmää.

    Kolme eri tilannetta, **yksi yhteinen seuraus**: kysytään, eikä
    ``--kylla`` ohita. Ne pidetään silti erillään tekstissä, koska käyttäjän
    seuraava askel on eri.

    Ainoa tapaus, jossa ei kysytä, on se jossa **molemmat havainnot ovat
    olemassa ja samaa mieltä**.
    """
    map_no = map_index + 1
    if expected is None:
        return Confirmation(
            question=f"Tuodaanko {unit} silti?",
            detail=(
                f"Ottelulla ei ole vetotietoa, joten kartan {map_no} nimeä ei "
                "voitu ristiintarkistaa. Demon otsikko sanoo "
                f"{_map_text(header)}, eikä sitä voi verrata mihinkään.\n"
                "Jos numero on väärä, demo tallentuu väärän kartan nimellä "
                "eikä virhe näy ennen kuin raportin karttajakauma on väärin."
            ),
            forced=True,
        )
    if header is None:
        return Confirmation(
            question=f"Tuodaanko {unit} silti?",
            detail=(
                "Demon otsikossa ei ole kartan nimeä, joten sitä ei voitu "
                f"verrata vetotietoon (kartta {map_no} on {expected}).\n"
                "Tiedosto voi silti olla oikea, mutta sitä ei voi todeta "
                "tästä."
            ),
            forced=True,
        )
    if same_map(header, expected):
        return None

    other = _other_pick(header, picks)
    hint = (
        f"\nOttelun vetotiedossa {header} on kartta {other} -- jos tuot juuri "
        f"sen, oikea arvo on --map {other}."
        if other is not None
        else "\nOtsikon karttaa ei ole ottelun vetotiedossa lainkaan, joten "
        "tiedosto on todennäköisesti eri ottelusta."
    )
    return Confirmation(
        question=f"Tuodaanko {unit} silti?",
        detail=(
            f"Kartta ei täsmää: demon otsikko sanoo {header}, mutta ottelun "
            f"vetotiedon mukaan kartta {map_no} on {expected}.{hint}\n"
            "Väärällä nimellä tallennettu demo ei kaada mitään -- se pilaa "
            "raportin hiljaa."
        ),
        forced=True,
    )


def _other_pick(header: str | None, picks: Sequence[str]) -> int | None:
    """Mistä kohtaa vetotietoa otsikon kartta löytyy, jos muualta?

    **Kysymys ilman vastausvaihtoehtoa on huono kysymys.** Kun kartta ei
    täsmää, todennäköisin syy on väärä ``--map`` -- ja jos otsikon kartta on
    vedossa jollain toisella numerolla, se numero on juuri se, jonka käyttäjä
    tarvitsee. Ilman tätä hänellä ei ole yhtään oikeaa arvoa, ja houkutus
    vastata "k" on suuri.

    Returns:
        1-pohjainen kartan numero, tai ``None``.
    """
    if header is None:
        return None
    for index, name in enumerate(picks):
        if same_map(header, name):
            return index + 1
    return None


def _overwrite_question(unit: str, existing: Path) -> Confirmation:
    """Kysymys arkistossa jo olevan demon korvaamisesta.

    ``forced`` on **epätosi**, ja se on jäädytetyn I/O-matriisin oma rivi
    ("Kohde on jo arkistossa ... ``--kylla`` ohittaa tämän"). Katselmus
    huomautti perustelusta aiheellisesti: aiempi teksti sanoi korvattavan
    tiedoston olevan "haettavissa uudelleen", eikä se pidä paikkaansa --
    Downloads-oikeutta ei ole, ja juuri siksi tämä komento on olemassa.

    Oikea perustelu on toinen ja se on rakenteellinen: korvattava tiedosto on
    **saman yksikön** demo samassa hakemistossa, korvaava sisältö on
    tarkistettu ehjäksi ennen siirtoa, ja siirto on atominen -- kysymys on siis
    "korvataanko tämä versio tällä versiolla", ei "tuhotaanko jotain muuta".
    Karttatarkistus on eri asia: siinä väärä vastaus tallentaa demon **väärän
    kartan nimellä**, eikä sitä huomaa mistään.
    """
    return Confirmation(
        question=f"Korvataanko {unit}?",
        detail=(
            f"Demo {unit} on jo levyllä: {existing}\n"
            f"Koko {size_fi(_size(existing))}."
        ),
        forced=False,
    )


# -- Sisäiset: huomiot -------------------------------------------------------


def _replaced_note(todo: ImportPlan) -> str:
    """Korvatun tiedoston poisto -- **ja sen todellinen syy**.

    Kaksi katselmuslöydöstä samassa rivissä. Ensinnäkin poiston paluuarvo
    luetaan: :func:`_remove`in oma dokumentaatio sanoo epäonnistumisen olevan
    Windowsilla tavallista, ja jos huomio kertoisi poistosta jota ei tehty,
    arkistoon jäisi kaksi tiedostoa samalle tunnisteelle -- ``find_demo``
    palauttaisi niistä ``DEMO_SUFFIXES``-järjestyksessä **vanhan**, ja
    ``parse`` lukisi tiivisteen metasta tarkistamatta sitä tiedostoa vasten.

    Toiseksi syy on nyt tosi. Aiempi teksti sanoi "koska uusi tiedosto on eri
    päätteellä" myös silloin, kun päätteet olivat identtiset ja ero oli
    hakemisto. Nyt kohde valitaan sen mukaan missä demo jo on, joten ero **on**
    pääte -- ja vain se.
    """
    assert todo.replaces is not None
    if _remove(todo.replaces):
        return (
            f"Korvattu demo {todo.replaces.name} poistettiin, koska uusi "
            f"tiedosto on samassa hakemistossa eri päätteellä "
            f"({todo.target_path.name}) eivätkä ne saa jäädä rinnakkain "
            "samalle tunnisteelle."
        )
    return (
        f"VAROITUS: vanhaa demoa {todo.replaces} ei saatu poistettua "
        "(esimerkiksi OneDriven tiedostolukko), joten samalla tunnisteella on "
        f"nyt kaksi tiedostoa: {todo.replaces.name} ja "
        f"{todo.target_path.name}. Metatiedosto kuvaa uutta. Poista vanha "
        "käsin ennen kuin ajat parse-komennon."
    )


def _orphan_note(todo: ImportPlan) -> str:
    """Orvon metatiedoston poisto.

    Sama sääntö kuin ``stages.fetch``issä: metatiedosto, joka jää kuvaamaan
    tiedostoa jota ei ole, on väite tiivisteestä -- ja ``parse`` lukee
    tiivisteen **ensimmäisestä löytyneestä** metatiedostosta. Väärä meta
    väärässä hakemistossa tekisi tuoreesta demosta ajantasaisen vanhan demon
    tulokselle.
    """
    assert todo.orphan_meta is not None
    if _remove(todo.orphan_meta):
        return (
            f"Vanha metatiedosto hakemistossa {todo.orphan_meta.parent} "
            "poistettiin, koska se kuvasi tiedostoa jota siellä ei ole."
        )
    return (
        f"VAROITUS: vanhaa metatiedostoa {todo.orphan_meta} ei saatu "
        "poistettua, ja se kuvaa tiedostoa jota ei ole. Poista se käsin -- "
        "muuten parse voi lukea tiivisteen väärästä tiedostosta."
    )


def _source_note(todo: ImportPlan) -> str:
    """Mitä lähdetiedostolle tehtiin."""
    if not todo.move:
        return (
            f"Lähdetiedosto {todo.source_path} jätettiin paikalleen, koska se "
            "ei ole arkiston import-kansiossa -- se kopioitiin, ei siirretty."
        )
    if _remove(todo.source_path):
        return (
            f"Lähdetiedosto {todo.source_path.name} poistettiin "
            "tuontikansiosta."
        )
    return (
        f"Lähdetiedostoa {todo.source_path} ei saatu poistettua (esimerkiksi "
        "OneDriven tiedostolukko). Demo on silti arkistossa; poista lähde "
        "käsin, jos haluat vapauttaa tilan."
    )


def _unverified_note(todo: ImportPlan) -> str:
    """Se, mitä lähteestä **ei** voitu todeta -- sanottuna ääneen.

    Sama sääntö ja sama peruste kuin ``stages.fetch._unverified_note``illa:
    vaiettu epävarmuus näyttäisi varmuudelta. Pakkaamattomassa ``.dem``:ssä ei
    ole pituutta, tarkistetta eikä lopetusmerkkiä, joten puolikas tiedosto on
    erottamaton kokonaisesta -- ja se paljastuisi vasta parsinnassa.
    """
    return (
        f"Tiedostossa {todo.source_path.name} ei ole pituustietoa "
        "(pakkaamattomassa demossa ei ole kehyksen kokoa, tarkistetta eikä "
        "lopetusmerkkiä), joten sen kokonaisuutta ei voitu todeta. Kopio "
        "vastaa lähdettä tavulleen, mutta jos lähde oli vajaa, se paljastuu "
        "vasta parsinnassa. Metatiedostossa length_verified on false."
    )


def _mismatch_note(todo: ImportPlan) -> str:
    """Vahvistettu karttapoikkeama tuloksen ``reason``iin.

    Kysymykseen vastattiin kyllä, mutta havainto ei katoa vastauksen myötä:
    jos raportin karttajakauma näyttää myöhemmin oudolta, tämä rivi on se,
    joka kertoo miksi.
    """
    return (
        "Kartta jäi tarkistamatta tai poikkesi: otsikko "
        f"{_map_text(todo.header_map_name)}, vetotieto "
        f"{_map_text(todo.expected_map_name)}. Tuonti tehtiin käyttäjän "
        "vahvistuksella."
    )


def _map_text(value: str | None) -> str:
    """Karttanimi tulosteeseen; puuttuva on sanottava eikä jätettävä tyhjäksi."""
    return value if value else "(ei nimeä)"


# -- Sisäiset: siirto --------------------------------------------------------


def _transfer(source: Path, target: Path, expected_size: int) -> tuple[str, int]:
    """Kopioi lähde kohteeseen ja laske tiiviste **samasta tavupalasta**.

    Neljä sääntöä yhdessä lohkossa:

    1. **Tiiviste kerran.** Sama pala menee sekä ``hashlib.sha256``-olioon
       että tiedostoon. Jälkikäteen laskeminen olisi toinen 230 MB:n
       levyläpikäynti.
    2. **Kirjoitus on atominen.** Väliaikaistiedosto siirretään vasta kun
       virta on luettu loppuun.
    3. **``os.rename`` ei kelpaa** vaikka lähde ja kohde olisivat samalla
       levyllä: se olisi nopeampi, mutta tiiviste jäisi laskematta ja vaatisi
       oman lukukierroksensa -- eli juuri sen työn, jonka sääntö 1 kieltää.
    4. **Lähteen koko tarkistetaan molemmissa päissä.** Explorer kirjoittaa
       lopullisella nimellä kesken kopioinnin ja OneDrive synkronoi
       taustalla, joten kasvava tiedosto on arkinen tilanne. Ilman tätä
       kopioitaisiin puolikas ja **poistettaisiin ehjä lähde** -- ja
       ``import/``issa on kuusi demoa, joita FACEIT ei enää tarjoa.

    Args:
        source: Lähdetiedosto.
        target: Kohde.
        expected_size: Lähteen koko suunnitelman tekohetkellä.

    Returns:
        ``(sha256-heksana, tavuja)``.

    Raises:
        ~pappascout.errors.PappascoutError: Jos lähde muuttui siirron aikana.
            Mitään ei jää kohteeseen eikä lähteeseen kosketa.
    """
    digest = hashlib.sha256()
    written = 0
    with atomic_path(target) as tmp:
        with open(source, "rb") as src, open(tmp, "wb") as dst:
            while chunk := src.read(_COPY_CHUNK):
                digest.update(chunk)
                dst.write(chunk)
                written += len(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        after = _size(source)
        if written != expected_size or after != expected_size:
            raise _reject(
                f"Lähdetiedosto {source.name} muuttui kesken siirron: koko oli "
                f"{expected_size} tavua ennen kopiota, {after} tavua sen "
                f"jälkeen, ja kopioitiin {written} tavua.\n"
                "Tiedostoa kirjoitetaan todennäköisesti parhaillaan "
                "(Resurssienhallinnan kopiointi tai OneDriven synkronointi "
                "kesken).\n"
                "Mitään ei siirretty arkistoon eikä lähdetiedostoon koskettu.",
                advice=(
                    "Odota kunnes tiedoston koko lakkaa muuttumasta ja aja "
                    "komento uudelleen."
                ),
            )
    return digest.hexdigest(), written


def _size(path: Path) -> int:
    """Tiedoston koko tavuina."""
    return Path(path).stat().st_size


def _remove(path: Path) -> bool:
    """Poista tiedosto; epäonnistuminen on **tieto** eikä poikkeus.

    OneDriven tiedostolukko ja virustorjunnan avoin kahva ovat molemmat
    tavallisia Windowsilla, eikä kumpikaan tarkoita että tuonti epäonnistui:
    demo on arkistossa ja metatiedosto sen vieressä.

    **Paluuarvo on luettava.** Katselmus löysi kutsupaikan, joka heitti sen
    menemään ja kirjoitti huomion "poistettiin" ennen yritystä; ks.
    :func:`_replaced_note`.
    """
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:  # pragma: no cover - riippuu tiedostojärjestelmästä
        return False


def _inside(path: Path, directory: Path) -> bool:
    """Onko ``path`` hakemiston ``directory`` sisällä?"""
    try:
        resolved = path.resolve()
        base = directory.resolve()
    except OSError:  # pragma: no cover - riippuu tiedostojärjestelmästä
        return False
    return base == resolved.parent or base in resolved.parents


def _same_file(left: Path, right: Path) -> bool:
    """Osoittavatko kaksi polkua samaan tiedostoon?"""
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - riippuu tiedostojärjestelmästä
        return False


def _is_demo_name(name: str) -> bool:
    """Onko nimessä demon pääte? Kirjainkoko ei ratkaise mitään.

    Tämä on nimen tarkistus eikä sisällön: se rajaa hakua ja luetteloa, ja
    sisällön päättää :func:`target_suffix` taikatavuista.
    """
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in DEMO_SUFFIXES)


def _archive_outputs(
    archive: ArchivePaths, *paths: Path
) -> tuple[PurePosixPath, ...]:
    """Kirjoitetut tiedostot ``StageResult.outputs``in muodossa.

    Sama sääntö ja sama peruste kuin ``stages.fetch._archive_outputs``illa:
    ``outputs`` on sopimuksen mukaan **arkiston sisäinen suhteellinen polku**,
    ja ``[project].demos_root`` on arkiston ulkopuolella.
    """
    inside: list[PurePosixPath] = []
    for path in paths:
        try:
            inside.append(archive.relative(path))
        except ValueError:
            continue
    return tuple(inside)
