"""``fetch`` -- putken alkupään kolmas vaihe: valitut demot arkistoon.

``select`` osaa sanoa, mitkä MapDemot kuuluvat otantaan. Tämä vaihe hakee ne::

    index/selections/<team_key>.json  ->  <demos_dir>/<map_demo_id>.dem.zst
                                          <demos_dir>/<map_demo_id>.meta.json

``<demos_dir>`` on ``[project].demos_root``, jos se on asetettu, muuten arkiston
oma ``demos/``. **Päätetty 2026-09-05:** demo on iso (142-223 MB) ja
uudelleen haettavissa, sen parsittu tulos pieni (noin 1 MB) ja korvaamaton --
joten iso jää paikalliselle levylle ja pieni pysyy OneDrivessa. Metatiedosto
menee **aina demon viereen**: se on väite juuri siitä tiedostosta, ja eri
hakemistoissa ne erkanisivat. Idempotenssi katsoo kumpaakin sijaintia, joten
asetuksen käyttöönotto ei lataa mitään uudelleen.

Vaihe on **yksikkökohtainen**: yksi kutsu, yksi MapDemo, yksi
:class:`~pappascout.stages.StageResult`. Sarjan ajaa :func:`run_many`, ja juuri
siksi yhden demon epäonnistuminen ei voi kaataa muita -- epäonnistuminen on
paluuarvo eikä poikkeus (AD-9).

Kuusi sääntöä, jotka tämä moduuli pitää voimassa
------------------------------------------------

**Signattu latauslinkki ei ole täällä lainkaan.** Portti
(:class:`~pappascout.adapters.protocols.DemoSource`) ottaa ``map_demo_id``:n ja
palauttaa tavut; osoitteen ratkaisee adapteri eikä se kulje portin läpi. Tämä
vaihe ei siis voi kirjoittaa linkkiä metatiedostoon, lokiin tai
virheilmoitukseen -- ei siksi, että se muistaa olla tekemättä niin, vaan siksi
ettei sillä ole linkkiä.

**Tiiviste lasketaan kirjoitusvirran aikana.** Sama tavupala menee samalla
kertaa sekä ``hashlib.sha256``-olioon että tiedostoon. 200 MB:n uudelleen
lukeminen hashausta varten olisi toinen levyläpikäynti jokaista demoa kohden --
ja OneDrive-arkistossa myös toinen pilvestä nouto. ``parse`` lukee tiivisteen
``.meta.json``ista eikä laske sitä uudelleen.

**Kirjoitusjärjestys on demo ensin, metatiedosto vasta sitten.** Näin
metatiedosto ei koskaan kuvaa tiedostoa, jota ei ole. Keskeytynyt ajo jättää
demon ilman metaa, ja se on korjattavissa yhdellä uudelleenlatauksella;
päinvastainen jättäisi arkistoon metatiedoston, joka väittää tiivisteen
tiedostosta jota ei ole -- ja ``parse``n syötelistaan tunnisteen, joka ei
vastaa mitään.

**Keskeneräistä latausta ei siirretä paikalleen.** Kirjoitus menee
:func:`~pappascout.archive.atomic_write.atomic_path`in väliaikaistiedostoon, ja
``os.replace`` tehdään vasta kun virta on luettu loppuun ja pituus tarkistettu.
Katkennut yhteys nostaa poikkeuksen **ennen** siirtoa, jolloin
väliaikaistiedosto siivotaan eikä ``demos/``iin ilmesty mitään.

**Levytila tarkistetaan ennen latausta, ei sen jälkeen.** Mitattu 2026-09-05:
C-asemalla oli 9,9 GB vapaana 236 GB:sta (96 % käytössä) ja yksi pakattu demo
on 142-223 MB. Jälkikäteen tehty tarkistus kertoisi, että levy täyttyi -- se ei
ole tarkistus vaan raportti vahingosta.

**Arkistossa jo oleva demo ohitetaan.** Vaihe on turvallinen ajaa milloin
tahansa: se on koko keräyskomennon (Story 3.5) edellytys.

Mitä tämä vaihe **ei** tee
--------------------------
Se ei kutsu ``parse``a eikä mitään muuta vaihetta -- järjestyksestä päättää
``pipeline``. Se ei muuta ``index/``in tiedostoja: se on niiden lukija.
Se ei pura ``.dem.zst``:ää ``.dem``:iksi (kaksi kopiota samasta demosta
kaksinkertaistaisi levytilan; ``parse`` purkaa tarvittaessa itse) eikä poista
demoja parsinnan jälkeen -- se on oma tarinansa (``prune``).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pappascout.adapters.decompress import ZSTD_MAGIC
from pappascout.adapters.protocols import DemoSource
from pappascout.archive.atomic_write import (
    atomic_path,
    atomic_write_json,
    temp_suffix,
)
from pappascout.archive.paths import ArchivePaths, safe_component
from pappascout.domain.models import Settings
from pappascout.errors import (
    ApiError,
    DemoUnavailable,
    DownloadsAccessDenied,
    PappascoutError,
    SettingsError,
)
from pappascout.stages import StageResult
from pappascout.stages.select import read_selection

__all__ = [
    "STAGE",
    "DEMO_SOURCE",
    "DEMO_SIZE_ESTIMATE_BYTES",
    "MIN_PLAUSIBLE_DEMO_BYTES",
    "DISK_RESERVE_BYTES",
    "FetchPlan",
    "plan",
    "resolve_team_key",
    "in_archive",
    "free_space",
    "run",
    "run_many",
    "default_source",
    "size_fi",
]

STAGE = "fetch"

#: ``.meta.json``in ``source``-kentän arvo tälle vaiheelle.
#:
#: Toinen mahdollinen arvo on ``import`` (Story 3.6). Kenttä on olemassa, jotta
#: käsin tuodun ja ladatun demon eron **näkee jälkikäteen** -- mutta mikään
#: putken vaihe ei saa käyttäytyä eri tavalla sen mukaan: tuotu demo
#: käyttäytyy täsmälleen kuten ladattu.
DEMO_SOURCE = "downloads_api"

#: Yhden pakatun demon koon arvio tavuina levytilatarkistusta varten.
#:
#: Mitattu 2026-09-05 arkiston demoista: suurin pakattu tiedosto on
#: 234 163 493 tavua eli 223,3 MiB. **Arvio on mitatun ylärajan yläpuolella
#: eikä keskiarvo**, koska tarkistuksen virheen hinta on epäsymmetrinen: liian
#: pieni arvio päästää latauksen alkuun levylle, jolle se ei mahdu, ja kirjoitus
#: katkeaa täyteen levyyn; liian suuri arvio kieltäytyy latauksesta, joka olisi
#: mahtunut, ja käyttäjä vapauttaa tilaa. Jälkimmäinen on korjattavissa,
#: edellinen ei ole.
#:
#: **Arvio on vain portin syöte, ei lupaus.** Heti kun lähde kertoo
#: ``Content-Length``in, tarkistus tehdään uudelleen sillä luvulla ennen kuin
#: tavuakaan on kirjoitettu (:func:`_download`).
DEMO_SIZE_ESTIMATE_BYTES = 256 * 1024 * 1024

#: Levytilan varmuusvara tavuina: se, mitä ei saa käyttää demoihin.
#:
#: **Ei kohteliaisuus vaan käyttökelpoisuuden raja.** Windows tarvitsee
#: sivutustilaa ja OneDrive synkronointipuskurin; täyteen kirjoitettu
#: järjestelmälevy ei ole "hieman ahdas" vaan kone, jolla ei voi työskennellä.
#: Kaksi gigatavua on noin kymmenen demon verran mitatusta koosta, eli se
#: kestää sen, että arvio osuu väärin muutaman kerran peräkkäin.
DISK_RESERVE_BYTES = 2 * 1024 * 1024 * 1024

#: Pienin koko, jonka pakattu CS2-demo voi uskottavasti olla.
#:
#: **Vartija roskaa vastaan, ei kokorajoite.** HTML-virhesivu 200-statuksella
#: ja tyhjä vastaus ovat molemmat "onnistuneita" latauksia, jotka kirjoittuisivat
#: demoksi -- ja koska idempotenssi katsoo vain tiedoston olemassaoloa, roska
#: ohitettaisiin sen jälkeen **ikuisesti**. Arkiston pienin pakattu demo on
#: yli 140 MB, joten megatavu on kolme kertaluokkaa sen alapuolella: se ei voi
#: hylätä oikeaa demoa, mutta se pysäyttää jokaisen virhesivun.
MIN_PLAUSIBLE_DEMO_BYTES = 1024 * 1024

_SIZE_UNITS = ("kt", "Mt", "Gt", "Tt")


def size_fi(num_bytes: int) -> str:
    """Tavumäärä luettavana suomalaisittain (desimaalipilkku)."""
    if num_bytes < 1024:
        return f"{num_bytes} tavua"
    value = float(num_bytes)
    unit = _SIZE_UNITS[0]
    for unit in _SIZE_UNITS:
        value /= 1024
        if value < 1024:
            break
    return f"{value:.1f} {unit}".replace(".", ",")


# -- Suunnitelma -------------------------------------------------------------


@dataclass(frozen=True)
class FetchPlan:
    """Mitä yksi ajo aikoo ladata -- **ennen kuin se lataa mitään**.

    Suunnitelma on oma tuotoksensa eikä ajon sivutuote, koska käyttäjältä
    kysytään lupa: kysymys, joka ei kerro montako tiedostoa ja paljonko
    levytilaa, ei ole kysymys vaan muodollisuus. Sama suunnitelma on myös
    levytilatarkistuksen syöte, joten ruudulla näkyvä luku ja tarkistuksen luku
    ovat samasta lähteestä.

    Attributes:
        team_key: Joukkue, jonka valintatiedosto luettiin.
        pending: Ladattavat MapDemot valintatiedoston järjestyksessä.
        present: Otantaan kuuluvat MapDemot, jotka ovat jo arkistossa
            (demo **ja** metatiedosto). Ne eivät lataudu uudelleen, mutta ne
            kuuluvat lukuun -- muuten "2 / 12" näyttäisi otannan kutistuneen.
        estimated_bytes: Ladattavien yhteiskoon arvio.
    """

    team_key: str
    pending: tuple[str, ...] = ()
    present: tuple[str, ...] = ()
    estimated_bytes: int = 0

    @property
    def selected(self) -> int:
        """Montako MapDemoa otantaan kuuluu kaikkiaan."""
        return len(self.pending) + len(self.present)


def plan(
    archive: ArchivePaths,
    team_key: str,
    *,
    size_estimate: int = DEMO_SIZE_ESTIMATE_BYTES,
) -> FetchPlan:
    """Lue valintatiedosto ja päätä, mitkä demot puuttuvat arkistosta.

    Mukaan otetaan rivit, joilla ``roster_ok`` on tosi: ne ovat otanta, ja
    hylätyn kartan lataaminen kuluttaisi kiintiötä ja levytilaa aineistoon,
    jota mikään raportti ei lue.

    Args:
        archive: Arkiston polut.
        team_key: Kanoninen joukkuetunniste.
        size_estimate: Yhden demon koon arvio tavuina.

    Returns:
        :class:`FetchPlan`.

    Raises:
        ~pappascout.errors.PappascoutError: Jos valintatiedostoa ei ole tai sen
            muoto on tuntematon. Viesti kehottaa ajamaan ``select``in.
    """
    document = read_selection(archive, team_key)
    rows = document.get("selections")
    if not isinstance(rows, list):
        raise PappascoutError(
            f"Joukkueen {team_key} valintatiedostossa ei ole selections-listaa.\n"
            f'Aja uudelleen: uv run pappascout select --team "{team_key}"'
        )

    pending: list[str] = []
    present: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("roster_ok"):
            continue
        map_demo_id = row.get("map_demo_id")
        if not isinstance(map_demo_id, str) or not map_demo_id:
            continue
        if in_archive(archive, map_demo_id):
            present.append(map_demo_id)
        else:
            pending.append(map_demo_id)

    return FetchPlan(
        team_key=team_key,
        pending=tuple(pending),
        present=tuple(present),
        estimated_bytes=len(pending) * int(size_estimate),
    )


def resolve_team_key(archive: ArchivePaths, team: str) -> str:
    """Tulkitse ``--team`` kanoniseksi tunnisteeksi joukkueindeksin avulla.

    Sama lukija kuin ``discover``illa ja ``select``illa
    (:func:`~pappascout.stages.discover.resolve_team`), koska monitulkintaisen
    nimen on tuotettava sama luettelo riippumatta siitä, minkä komennon
    käyttäjä ajoi. Tuonti on funktion sisällä, jottei ``discover``in
    polars-riippuvuus lataudu, kun tästä moduulista tarvitaan vain
    :func:`run`.

    Raises:
        ~pappascout.errors.PappascoutError: Jos indeksiä ei ole tai nimi ei osu
            yhteen ainoaan joukkueeseen.
    """
    from pappascout.stages.discover import (
        read_teams_index,
        resolve_team,
        teams_from_index,
    )

    teams = teams_from_index(read_teams_index(archive))
    return resolve_team(teams, team).team_key


def in_archive(archive: ArchivePaths, map_demo_id: str) -> bool:
    """Onko demo arkistossa **kokonaisena** eli tiedosto ja metatiedosto?

    Molemmat, koska kumpikin yksin on keskeneräinen tila eikä valmis tulos:
    demo ilman metaa on katkennut ajo (tiivistettä ei ole), meta ilman demoa on
    käsin poistettu tiedosto. Kummassakin oikea jatko on ladata uudelleen, ja
    juuri siksi kumpikaan ei saa näyttää ohitettavalta.
    """
    return (
        archive.find_demo(map_demo_id) is not None
        and archive.find_demo_meta(map_demo_id) is not None
    )


def free_space(archive: ArchivePaths) -> int | None:
    """Vapaa levytila tavuina **siltä levyltä, jolle demot kirjoitetaan**.

    Kohde on :meth:`~pappascout.archive.paths.ArchivePaths.demos_dir` eikä
    arkiston juuri: paikallinen demohakemisto voi olla eri asemalla kuin
    OneDrive-arkisto, ja väärän aseman tila on väärä vastaus -- se joko estäisi
    latauksen, joka mahtuu, tai päästäisi läpi latauksen, joka ei mahdu. Tällä
    koneella asemat sattuvat olemaan samat, eikä sitä saa olettaa koodissa.

    Kysytään **lähimmältä olemassa olevalta hakemistolta**: kohdehakemisto
    syntyy vasta ensimmäisen latauksen myötä, eikä olemattoman polun levytilaa
    voi kysyä. ``None`` tarkoittaa "ei saatu selville" -- ja se on eri asia kuin
    nolla: tuntematon tila ei saa estää latausta, koska silloin poikkeuksellinen
    tiedostojärjestelmä pysäyttäisi koko työkalun.
    """
    path = archive.demos_dir()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return int(shutil.disk_usage(path).free)
    except OSError:  # pragma: no cover - riippuu tiedostojärjestelmästä
        return None


# -- Yhden demon lataus ------------------------------------------------------


def run(
    archive: ArchivePaths,
    map_demo_id: str,
    *,
    source: DemoSource,
    size_estimate: int = DEMO_SIZE_ESTIMATE_BYTES,
    reserve_bytes: int = DISK_RESERVE_BYTES,
    min_bytes: int = MIN_PLAUSIBLE_DEMO_BYTES,
    disk_free: Callable[[ArchivePaths], int | None] | None = None,
    now: Callable[[], datetime] | None = None,
) -> StageResult:
    """Lataa yksi MapDemo levylle, tai kerro miksi et.

    Args:
        archive: Arkiston polut.
        map_demo_id: Yksikön tunniste ``{match_id}-{map_index}``.
        source: :class:`~pappascout.adapters.protocols.DemoSource`-portti.
            Tuotannossa :func:`default_source`, testeissä feikki.
        size_estimate: Yhden demon koon arvio levytilatarkistukseen. Käytetään
            vain siihen asti, kunnes lähde kertoo todellisen koon.
        reserve_bytes: Varmuusvara, jota ei käytetä demoihin.
        min_bytes: Pienin koko, jonka ladattu tiedosto saa olla.
        disk_free: Vapaan tilan kysyjä, tai ``None`` = :func:`free_space`.
            **``None`` eikä oletusarvoksi sidottu funktio**: oletusarvo
            sitoutuisi määrittelyhetkellä, jolloin ``monkeypatch``illa korvattu
            ``free_space`` ei osuisi tänne lainkaan ja testi menisi läpi vain
            siksi, että koneella sattuu olemaan tilaa.
        now: Kello ``fetched_at``-kenttää varten.

    Returns:
        :class:`~pappascout.stages.StageResult`, jonka ``status`` on

        ``ok``
            Demo on levyllä. ``skipped`` kertoo, ladattiinko se nyt.
        ``no_demo``
            Demoa ei ole olemassa: ottelua ei ole pelattu, karttaa ei
            pelattu, tai FACEIT on jo poistanut tallenteen. ``reason``
            erottaa nämä toisistaan.
        ``download_failed``
            Demo on todennäköisesti olemassa muttei tullut perille -- verkko,
            rajoitus, vajaa vastaus, roskaa vastauksena, levytila tai
            kirjoitusvirhe. Uusi ajo on mielekäs.

    Raises:
        ~pappascout.errors.PappascoutError: Vain jos tunniste ei kelpaa polun
            osaksi. Kaikki muu on ``status``, ei poikkeus (AD-9) -- **myös
            ``OSError``**.
    """
    started = time.perf_counter()
    safe_component(map_demo_id, "map_demo_id")
    clock = now if now is not None else (lambda: datetime.now(UTC))
    free_bytes = disk_free if disk_free is not None else free_space

    existing = archive.find_demo(map_demo_id)
    existing_meta = archive.find_demo_meta(map_demo_id)

    if existing is not None and existing_meta is not None:
        return _result(
            map_demo_id,
            status="ok",
            skipped=True,
            outputs=_archive_outputs(archive, existing, existing_meta),
            reason=(
                f"Demo {existing.name} on jo hakemistossa "
                f"{existing.parent} metatietoineen, joten sitä ei ladattu "
                "uudelleen."
            ),
            started=started,
            stats=_location_stats(existing, existing_meta, downloaded_bytes=0),
        )

    # **Kirjoitus menee sinne, missä demo jo on.** Jos vajaa demo on arkistossa
    # ja uudet lataukset menisivät paikalliseen hakemistoon, oletuskohde
    # jättäisi arkiston 190 MB paikalleen ja kirjoittaisi toisen kopion
    # viereen -- eli kaksinkertaistaisi juuri sen, mitä paikallisella
    # hakemistolla vältetään.
    demo_path = existing if existing is not None else archive.demo(map_demo_id)
    meta_path = demo_path.parent / f"{map_demo_id}.meta.json"

    # Miksi ladataan, vaikka jotain on jo levyllä. Syy kulkee tulokseen asti:
    # "ladattiin uudelleen" ilman perustetta näyttäisi turhalta työltä.
    redo: str | None = None
    orphan: Path | None = None
    if existing is not None:
        redo = (
            f"Demo {existing.name} oli hakemistossa {existing.parent} mutta "
            "metatiedosto puuttui (edellinen ajo katkesi kirjoitusten "
            "välissä), joten se ladattiin uudelleen samaan paikkaan."
        )
    elif existing_meta is not None:
        # **Orpo metatiedosto poistetaan, eikä vain korvata.** Se voi olla eri
        # hakemistossa kuin uusi demo (arkisto vs. paikallinen), jolloin pelkkä
        # kirjoitus jättäisi jälkeensä toisen metatiedoston, joka väittää
        # tiivisteen tiedostosta jota siellä ei ole -- ja ``parse`` lukee
        # tiivisteen juuri ensimmäisestä löytyneestä metatiedostosta.
        orphan = existing_meta if existing_meta != meta_path else None
        where = (
            f" Vanha metatiedosto hakemistossa {existing_meta.parent} "
            "poistettiin, koska se kuvasi tiedostoa jota ei ole."
            if orphan is not None
            else " Vanha metatiedosto korvattiin."
        )
        redo = (
            "Metatiedosto oli levyllä mutta demo puuttui, joten demo "
            f"ladattiin.{where}"
        )

    blocked = _preflight(
        archive,
        demo_path,
        map_demo_id,
        size_estimate=size_estimate,
        reserve_bytes=reserve_bytes,
        disk_free=free_bytes,
    )
    if blocked is not None:
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=str(blocked),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": next_step(blocked),
                "failure_key": _failure_key(blocked),
            },
        )

    def guard(announced: int) -> None:
        """Levytila uudelleen **todellisella** koolla ennen kirjoitusta.

        Arvio riittää portiksi vain siihen asti, kunnes lähde kertoo koon.
        Sen jälkeen arvion käyttäminen olisi tahallista epätarkkuutta: luku on
        tiedossa, eikä kirjoitusta pidä aloittaa jos se ei mahdu.
        """
        free = free_bytes(archive)
        need = announced + reserve_bytes
        if free is not None and free < need:
            raise _space_error(archive, map_demo_id, free, need, announced)

    try:
        digest, size, verified = _download(
            source, map_demo_id, demo_path, min_bytes=min_bytes, guard=guard
        )
    except DownloadsAccessDenied:
        # **Ainoa vika, joka nousee tämän vaiheen läpi poikkeuksena.**
        # Kaikki muu on yksikön tila (AD-9), koska kaikki muu koskee yhtä
        # demoa. Puuttuva Downloads-scope koskee tunnistetta: jokainen yksikkö
        # epäonnistuisi identtisesti, eikä yksikään voisi onnistua. Sen
        # kirjaaminen yksikön tilaksi tuottaisi otannan kokoisen luettelon
        # samaa virhettä ja saman määrän tuomittuja kutsuja.
        raise
    except DemoUnavailable as exc:
        # Poissa oleva demo on tosiasia eikä häiriö: sitä ei yritetä uudelleen
        # eikä sen takia keskeytetä sarjaa.
        return _result(
            map_demo_id,
            status="no_demo",
            skipped=False,
            outputs=(),
            reason=str(exc),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": next_step(exc),
                "failure_key": _failure_key(exc),
            },
        )
    except PappascoutError as exc:
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=str(exc),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": next_step(exc),
                "failure_key": _failure_key(exc),
            },
        )
    except OSError as exc:
        # **Levy on yksikön ominaisuus siinä missä verkkokin.** Täysi levy,
        # OneDriven tiedostolukko ja katkennut verkkolevy nostavat kaikki
        # ``OSError``in; ilman tätä haaraa ne karkaisivat vaiheen ohi ja
        # kaataisivat koko ajon "ohjelmavirheenä" -- eli rikkoisivat
        # rajoitteen "yhden demon epäonnistuminen ei keskeytä ajoa".
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=_os_error_message(demo_path, map_demo_id, exc),
            started=started,
            stats={
                "downloaded_bytes": 0,
                "next_step": _DISK_NEXT_STEP,
                "failure_key": _failure_key(exc),
            },
        )

    if orphan is not None:
        try:
            orphan.unlink()
        except OSError:  # pragma: no cover - riippuu levystä
            pass

    # **Vasta tässä.** Demo on paikallaan ja luettu loppuun; metatiedosto saa
    # syntyä vasta nyt, koska se on väite juuri tästä tiedostosta.
    try:
        atomic_write_json(
            meta_path,
            {
                "map_demo_id": map_demo_id,
                "sha256": digest,
                "size": size,
                "source": DEMO_SOURCE,
                "fetched_at": clock().isoformat(),
                # **Kertoo, tarkistettiinko pituus lähteen omaa lukua vasten.**
                # ``false`` ei tarkoita rikkinäistä tiedostoa vaan sitä, ettei
                # ehjyyttä voitu todeta latauksen aikana. Kenttä on aina, jotta
                # sen puuttumisesta ei tarvitse päätellä mitään.
                "length_verified": verified,
            },
        )
    except OSError as exc:
        return _result(
            map_demo_id,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=_os_error_message(meta_path, map_demo_id, exc),
            started=started,
            stats={
                "downloaded_bytes": size,
                "next_step": _DISK_NEXT_STEP,
                "failure_key": _failure_key(exc),
            },
        )

    note = redo
    if not verified:
        # Ei virhe, mutta ei myöskään vaiettava: tämä on se tapaus, jossa
        # katkennut lataus ei erotu ehjästä.
        note = " ".join(filter(None, (note, _unverified_note(map_demo_id))))

    return _result(
        map_demo_id,
        status="ok",
        skipped=False,
        outputs=_archive_outputs(archive, demo_path, meta_path),
        reason=note,
        started=started,
        stats=_location_stats(demo_path, meta_path, downloaded_bytes=size)
        | {"sha256": digest, "length_verified": verified},
    )


def run_many(
    archive: ArchivePaths,
    map_demo_ids: Iterable[str],
    *,
    source: DemoSource,
    **kwargs: Any,
) -> tuple[StageResult, ...]:
    """Aja :func:`run` jokaiselle tunnisteelle. **Yksikään vika ei keskeytä.**

    Silmukka ei ole mukavuus vaan sääntö: jos yhden demon 404 lopettaisi ajon,
    yhden poistetun tallenteen takia jäisi hakematta yksitoista muuta -- ja
    juuri niiden takia komento ajetaan.

    Napattavia tyyppejä on kaksi eikä yksi. :class:`~pappascout.errors.PappascoutError`
    on työkalun oma virhe, ``OSError`` levyn -- ja levy on yksikön ominaisuus
    siinä missä verkkokin: täysi levy, OneDriven tiedostolukko ja irronnut
    verkkolevy ovat kaikki tilanteita, joissa **seuraava demo voi hyvinkin
    onnistua**. Ilman ``OSError``ia rajoite "yhden demon epäonnistuminen ei
    keskeytä ajoa" pitäisi vain puolittain, ja käyttäjä näkisi ruudulla
    "ohjelmavirhe" täydestä levystä.

    Ohjelmavirhe (``TypeError`` ja muut) nousee edelleen läpi: se ei ole
    yksikön ominaisuus vaan koodin vika, eikä sitä saa piilottaa yhdentoista
    onnistuneen latauksen sekaan.
    """
    results: list[StageResult] = []
    units = list(map_demo_ids)
    for index, map_demo_id in enumerate(units):
        started = time.perf_counter()
        try:
            results.append(run(archive, map_demo_id, source=source, **kwargs))
        except DownloadsAccessDenied as exc:
            # **Keskeytys ensimmäiseen valtuutusvirheeseen.** Ks. luokan
            # dokumentaatio: tämä ei ole poikkeus säännöstä "yhden demon
            # epäonnistuminen ei keskeytä ajoa", vaan eri asia -- kyse ei ole
            # yhden demon epäonnistumisesta vaan siitä, ettei yksikään voi
            # onnistua.
            raise DownloadsAccessDenied(
                f"{exc}\n\n{_progress_note(results, units, index)}"
            ) from None
        except PappascoutError as exc:
            results.append(
                _result(
                    map_demo_id,
                    status="download_failed",
                    skipped=False,
                    outputs=(),
                    reason=str(exc),
                    started=started,
                    stats={
                        "downloaded_bytes": 0,
                        "next_step": next_step(exc),
                        "failure_key": _failure_key(exc),
                    },
                )
            )
        except OSError as exc:
            results.append(
                _result(
                    map_demo_id,
                    status="download_failed",
                    skipped=False,
                    outputs=(),
                    reason=_os_error_message(
                        archive.demos_dir(), map_demo_id, exc
                    ),
                    started=started,
                    stats={
                        "downloaded_bytes": 0,
                        "next_step": _DISK_NEXT_STEP,
                        "failure_key": _failure_key(exc),
                    },
                )
            )
        repeated = _repeated_failure(results)
        if repeated is not None and index + 1 < len(units):
            results.extend(_not_attempted(units[index + 1 :], repeated))
            break
    return tuple(results)


def default_source(settings: Settings, archive: ArchivePaths) -> DemoSource:
    """Tuotannon FACEIT-toteutus demoportille.

    Tuonti on funktion sisällä samasta syystä kuin
    ``stages.discover.default_source``issa: vaihe itse tuntee vain portin, ja
    tämän moduulin tuominen ei saa ladata ``requests``ia.

    **Downloads-token luetaan tässä**, ennen ensimmäistäkään latausta. Puuttuva
    token pysäyttää ajon suomenkieliseen ohjeeseen -- eikä kesken sarjan, kun
    puolet demoista on jo haettu.

    Raises:
        ~pappascout.errors.SettingsError: Jos avain tai token puuttuu.
    """
    from pappascout.adapters.faceit import FaceitClient, FaceitDemoSource

    client = FaceitClient.from_settings(settings, archive.raw_faceit())
    return FaceitDemoSource.from_settings(settings, client)


# -- Sisäiset ----------------------------------------------------------------


def _download(
    source: DemoSource,
    map_demo_id: str,
    demo_path: Path,
    *,
    min_bytes: int,
    guard: Callable[[int], None],
) -> tuple[str, int, bool]:
    """Kirjoita virta väliaikaistiedostoon ja laske tiiviste samalla kertaa.

    Kolme sisäkkäistä ``with``-lausetta, ja niiden **järjestys on osa sääntöä**:

    1. Virta avataan ensin. Jos demoa ei ole, poikkeus nousee ennen kuin
       yhtäkään tiedostoa on luotu -- 404 ei jätä levylle roskaa.
    2. Atominen polku toisena. Sen ``finally`` siivoaa väliaikaistiedoston, ja
       ``os.replace`` tehdään vain jos lohko päättyy ilman poikkeusta.
    3. Tiedosto vasta kolmantena, ja se suljetaan (``fsync``) ennen kuin
       tarkistukset tehdään.

    **Kaikki tarkistukset ovat ``atomic_path``-lohkon sisällä.** Ulkopuolella ne
    huomaisivat vian vasta kun tiedosto on jo siirretty paikalleen, ja korjaus
    olisi poisto -- eli juuri se tila, jonka atominen kirjoitus on olemassa
    estämään. Erityisen paha se olisi tässä, koska idempotenssi katsoo vain
    tiedoston olemassaoloa: paikalleen siirretty roska ohitettaisiin **joka
    ajolla ikuisesti**.

    Tarkistuksia on kolme, ja ne vastaavat kolmeen eri kysymykseen:

    ``Onko sisältö zstd?``
        Neljä ensimmäistä tavua. HTML-virhesivu 200-statuksella on
        onnistunut HTTP-vastaus ja täysin kelvollinen tiedosto -- se ei
        erotu mistään muusta kuin sisällöstään.
    ``Onko koko uskottava?``
        ``min_bytes``. Nollatavuinen vastaus läpäisisi pituustarkistuksen
        (``written == expected == 0``), ja tyhjä tiedosto ohitettaisiin sen
        jälkeen ikuisesti.
    ``Vastaako pituus lupausta?``
        ``Content-Length``. Kolmas paluuarvo kertoo, **tehtiinkö** tämä
        tarkistus -- lähde ei aina kerro pituutta, eikä sitä saa esittää
        tarkistetuksi.

    Returns:
        ``(sha256-heksana, tavuja, pituus_tarkistettiin)``.

    Raises:
        ~pappascout.errors.DemoUnavailable: Demoa ei ole.
        ~pappascout.errors.ApiError: Lataus ei onnistunut, jäi vajaaksi tai
            tuotti jotain muuta kuin demon.
        OSError: Levy on täynnä tai tiedostoa ei voi kirjoittaa.
    """
    digest = hashlib.sha256()
    written = 0
    head = b""

    with source.get_demo(map_demo_id) as stream:
        expected = stream.content_length
        if expected is not None:
            # Todellinen koko on nyt tiedossa: tarkistetaan tila uudelleen
            # ennen kuin tavuakaan on kirjoitettu.
            guard(expected)

        with atomic_path(demo_path) as tmp:
            with open(tmp, "wb") as handle:
                for chunk in stream.chunks:
                    if len(head) < len(ZSTD_MAGIC):
                        head += bytes(chunk[: len(ZSTD_MAGIC) - len(head)])
                    # **Sama pala, sama kerta.** Tiivisteen laskeminen
                    # jälkikäteen vaatisi 200 MB:n lukemisen uudelleen.
                    digest.update(chunk)
                    handle.write(chunk)
                    written += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())

            if expected is not None and written != expected:
                raise ApiError(
                    f"Demon {map_demo_id} lataus jäi vajaaksi: lähde lupasi "
                    f"{expected} tavua ({size_fi(expected)}) mutta perille "
                    f"tuli {written} tavua ({size_fi(written)}).\n"
                    "Keskeneräistä tiedostoa ei jätetty levylle.",
                    advice=_RETRY_NEXT_STEP,
                )
            if written < min_bytes:
                raise ApiError(
                    f"Demon {map_demo_id} lataus tuotti vain {written} tavua "
                    f"({size_fi(written)}), mikä on liian vähän ollakseen "
                    f"CS2-demo (vähintään {size_fi(min_bytes)}).\n"
                    "Lähde vastasi todennäköisesti virhesivulla tai "
                    "tyhjällä rungolla. Tiedostoa ei jätetty levylle.",
                    advice=_RETRY_NEXT_STEP,
                )
            if head[: len(ZSTD_MAGIC)] != ZSTD_MAGIC:
                raise ApiError(
                    f"Demon {map_demo_id} lataus ei ole zstd-pakattu tiedosto: "
                    f"alkutavut ovat {head[: len(ZSTD_MAGIC)]!r}, ei "
                    f"{ZSTD_MAGIC!r}.\n"
                    "Lähde vastasi todennäköisesti virhesivulla tai "
                    "kirjautumissivulla, vaikka tilakoodi oli 200. Tiedostoa "
                    "ei jätetty levylle.",
                    advice=_RETRY_NEXT_STEP,
                )

    return digest.hexdigest(), written, expected is not None


def _preflight(
    archive: ArchivePaths,
    demo_path: Path,
    map_demo_id: str,
    *,
    size_estimate: int,
    reserve_bytes: int,
    disk_free: Callable[[ArchivePaths], int | None],
) -> PappascoutError | None:
    """Kaikki kohdehakemistoa koskeva ennen ensimmäistäkään kutsua.

    Kaksi tarkistusta yhdessä paikassa, koska niillä on sama ajoitusvaatimus:
    molempien on tapahduttava **ennen** kuin latausta aloitetaan. Yhteys, joka
    avataan hakemistoon jota ei voi kirjoittaa, kuluttaa Downloads-kiintiötä ja
    kaatuu vasta ensimmäiseen tavuun.

    Returns:
        Virhe, joka kantaa sekä selityksen että neuvon, tai ``None`` jos
        kaikki on kunnossa. **Virhe eikä merkkijono**: pelkkä teksti
        pakottaisi kutsujan keksimään neuvon itse, ja juuri se keksiminen on
        se, mitä ``PappascoutError.advice`` on olemassa estämään.
    """
    unwritable = _writable_problem(demo_path.parent)
    if unwritable is not None:
        return unwritable
    free = disk_free(archive)
    need = int(size_estimate) + int(reserve_bytes)
    if free is None or free >= need:
        return None
    return _space_error(archive, map_demo_id, free, need, size_estimate)


def _writable_problem(directory: Path) -> PappascoutError | None:
    """Onko hakemisto luotavissa ja kirjoitettavissa? ``None`` = on.

    **Kokeilee, ei päättele.** Oikeudet, kytkemätön verkkoasema,
    kirjoitussuojaus ja tiedosto hakemiston paikalla ovat neljä eri syytä,
    joista mikään ei näy ``disk_usage``ista -- ja jokainen niistä kaataisi
    ajon vasta kesken kirjoituksen, siinä vaiheessa kun Downloads-kiintiötä on
    jo kulutettu. ``os.access`` ei kelpaa Windowsilla, koska se ei näe ACL:iä
    eikä kirjoitussuojattua asemaa; ainoa luotettava tapa on kirjoittaa.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SettingsError(
            f"Demohakemistoa {directory} ei voitu luoda: {exc}\n"
            "Latausta ei aloitettu.",
            advice=(
                "Tarkista settings.tomlin rivi [project].demos_root: "
                "osoittaako se olemassa olevalle asemalle, ja onko polulla "
                "samanniminen tiedosto?"
            ),
        )
    probe = directory / f".pappascout-kirjoituskoe{temp_suffix()}"
    try:
        probe.write_bytes(b"1")
    except OSError as exc:
        return SettingsError(
            f"Demohakemistoon {directory} ei voi kirjoittaa: {exc}\n"
            "Latausta ei aloitettu, jottei Downloads-kiintiötä kuluisi "
            "turhaan.",
            advice=(
                "Tarkista settings.tomlin rivi [project].demos_root sekä "
                "hakemiston kirjoitusoikeudet, ja aja komento sitten "
                "uudelleen."
            ),
        )
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - riippuu levystä
            pass
    return None


def _space_error(
    archive: ArchivePaths,
    map_demo_id: str,
    free: int,
    need: int,
    demo_bytes: int,
) -> ApiError:
    """Levytilan loppumisen selitys. Sama teksti ennen ja kesken latauksen.

    ``ApiError`` eikä oma tyyppi, koska vaiheen päätös on sama kuin
    verkkovialla: ``download_failed`` eli tilanne, joka voi korjaantua uudella
    ajolla -- tässä sen jälkeen kun käyttäjä on vapauttanut tilaa.
    """
    return ApiError(
        f"Levytila ei riitä demon {map_demo_id} lataukseen, joten latausta ei "
        "aloitettu.\n"
        f"Vapaana on {size_fi(free)} ja tarvitaan {size_fi(need)} "
        f"(demo {size_fi(demo_bytes)} + varmuusvara "
        f"{size_fi(need - demo_bytes)}).\n"
        "Tee jokin näistä ja aja komento uudelleen:\n"
        f"  1. Poista jo parsittuja demoja hakemistosta "
        f"{archive.demos_dir()} -- parsed-taulut säilyvät, eikä raportti "
        "tarvitse demotiedostoa uudelleen.\n"
        "  2. Vapauta levytilaa muualta koneelta.\n"
        "  3. Osoita demot toiselle levylle asetuksella "
        "[project].demos_root.",
        advice=(
            f"Vapauta vähintään {size_fi(need - free)} levytilaa ja aja "
            "komento uudelleen."
        ),
    )


#: Neuvo vialle, joka **todella** korjaantuu uudella yrityksellä.
#:
#: Tämä on se lause, joka ennen tuli otsikosta jokaiselle epäonnistumiselle.
#: Nyt se annetaan nimenomaisesti ja vain silloin, kun se on totta.
_RETRY_NEXT_STEP = "Aja komento uudelleen."

#: Neuvo levy- ja tiedostojärjestelmävioille.
_DISK_NEXT_STEP = (
    "Vapauta levytilaa tai odota kunnes OneDrive vapauttaa tiedostolukon, ja "
    "aja komento sitten uudelleen."
)


#: Montako peräkkäistä samalla tavalla epäonnistunutta yksikköä lopettaa sarjan.
#:
#: **Havainto toistuvuudesta, ei oletus syystä.** Story 3.4:n live-ajo
#: 2026-09-05 tuotti kaksi identtistä 400:aa; kahdellatoista demolla se olisi
#: ollut kaksitoista turhaa kutsua. Houkutus olisi pysäyttää ajo heti
#: ensimmäiseen 400:aan, mutta **sitä ei voi perustella**: 400 tarkoittaa joko
#: epämuodostunutta tunnistetta (kaikki epäonnistuvat) tai epämuodostunutta
#: ``resource_url``ia (vain tämä epäonnistuu), eikä vastauksesta voi päätellä
#: kumpaa. C2:n perustelu ("ei yksikään voi onnistua") ei siis päde tähän.
#:
#: Toistuvuus on eri asia kuin syy, ja se on **mitattavissa**: kun kolme
#: peräkkäistä yksikköä kaatuu samaan tunnisteeseen, vika on yhteinen
#: riippumatta siitä, mikä se on. Kolme eikä kaksi, koska kaksi peräkkäistä
#: samaa koodia on täysin uskottavaa sattumaa (kaksi poistettua demoa samasta
#: ottelusta), ja kolmen kutsun hinta on pieni verrattuna siihen, että sarja
#: lopetettaisiin väärin perustein.
IDENTICAL_FAILURE_LIMIT = 3


def _repeated_failure(results: Sequence[StageResult]) -> str | None:
    """Kaatuiko :data:`IDENTICAL_FAILURE_LIMIT` viimeisintä samaan vikaan?

    Palauttaa vian tunnisteen tai ``None``. Katsoo vain ``download_failed``-
    tiloja: ``no_demo`` on odotettu lopputulos vanhalle ottelulle eikä merkki
    siitä, että jokin on rikki -- kolme peräkkäistä poistettua demoa on
    normaali havainto, ei syy lopettaa.
    """
    if len(results) < IDENTICAL_FAILURE_LIMIT:
        return None
    last = results[-IDENTICAL_FAILURE_LIMIT:]
    if any(r.status != "download_failed" for r in last):
        return None
    keys = {str(r.stats.get("failure_key", "")) for r in last}
    if len(keys) != 1:
        return None
    key = keys.pop()
    return key or None


def _not_attempted(
    units: Sequence[str], failure_key: str
) -> list[StageResult]:
    """Tulos yksiköille, joita ei enää yritetty.

    **Rivi jokaiselle, ei hiljaista lyhennystä.** Suunnitelma lupasi tietyn
    määrän yksiköitä, ja luettelo, joka on lyhyempi kuin suunnitelma, jättäisi
    käyttäjän arvaamaan mihin loput katosivat. Jokainen rivi kertoo myös oman
    seuraavan toimenpiteensä, kuten jokainen muukin epäonnistuminen.
    """
    reason = (
        f"Ei yritetty: {IDENTICAL_FAILURE_LIMIT} edellistä demoa epäonnistui "
        f"samaan vikaan ({failure_key}), joten vika on yhteinen eikä "
        "demokohtainen. Kutsua ei tehty, jottei Downloads-kiintiö kuluisi "
        "varmasti turhaan."
    )
    step = (
        "Korjaa yllä lueteltu vika ja aja komento uudelleen -- tämä demo on "
        "yhä hakematta."
    )
    return [
        _result(
            unit,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason=reason,
            started=time.perf_counter(),
            stats={
                "downloaded_bytes": 0,
                "next_step": step,
                "failure_key": failure_key,
                "not_attempted": True,
            },
        )
        for unit in units
    ]


def _failure_key(exc: BaseException) -> str:
    """Vian **tunniste toistuvuuden havaitsemiseen**, ei sen syy.

    Kaksi peräkkäistä yksikköä, joilla on sama avain, epäonnistuivat samalla
    tavalla. Se on havainto eikä päätelmä siitä *miksi* -- ja juuri siksi se
    kelpaa perusteeksi lopettaa sarja: toistuvuus on mitattavissa, syy ei aina
    ole (ks. :data:`IDENTICAL_FAILURE_LIMIT`).

    Tilakoodi silloin kun se on, muuten poikkeuksen tyyppi. ``OSError``eilla
    myös ``errno``, koska täysi levy (28) ja käyttöoikeus (13) ovat eri vika ja
    eri korjaus.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return f"http-{status}"
    if isinstance(exc, OSError) and exc.errno is not None:
        return f"errno-{exc.errno}"
    return f"tyyppi-{type(exc).__name__}"


def _progress_note(
    results: Sequence[StageResult], units: Sequence[str], index: int
) -> str:
    """Mitä ehdittiin tehdä ennen keskeytystä.

    Ilman tätä riviä käyttäjä ei tiedä, onko arkistoon tullut mitään -- ja
    keskeytynyt sarja näyttäisi samalta kuin sarja, joka ei alkanut lainkaan.
    """
    done = sum(1 for r in results if r.status == "ok")
    remaining = len(units) - index
    return (
        f"Ajo keskeytettiin ensimmäiseen valtuutusvirheeseen: {done} demoa "
        f"ehdittiin hakea, {remaining} jäi hakematta. Sama vika toistuisi "
        "jokaisella niistä, joten yritystä ei tehty."
    )


def _os_error_message(path: Path, map_demo_id: str, exc: OSError) -> str:
    """Levyvirhe suomeksi ja **käyttäjän kielellä**, ei errno-numerona.

    Kolme tavallisinta syytä nimetään, koska ``[Errno 28] No space left`` ei
    kerro suomeksi mitä tehdä -- ja koska juuri tämä virhe näytettiin ennen
    "ohjelmavirheenä", mikä on väärä diagnoosi.
    """
    return (
        f"Demon {map_demo_id} kirjoitus epäonnistui levyvirheeseen "
        f"({type(exc).__name__}: {exc}).\n"
        f"Kohde oli {path}.\n"
        "Tavallisimmat syyt: levy täyttyi kesken kirjoituksen, OneDrive piti "
        "tiedostoa lukittuna, tai verkkolevy katkesi.\n"
        "Muut demot haettiin silti. Vapauta tilaa tai odota hetki ja aja "
        "komento uudelleen."
    )


def _unverified_note(map_demo_id: str) -> str:
    """Se, mitä latauksesta **ei** voitu todeta -- sanottuna ääneen.

    Ilman ``Content-Length``iä katkennut virta näyttää täsmälleen samalta kuin
    ehjä: tiedosto on paikallaan, alku on zstd-muotoa ja koko on uskottava.
    Vaihe ei voi erottaa niitä, ja **sen on sanottava se** -- muuten vaiettu
    epävarmuus näyttäisi varmuudelta, ja katkennut demo ohitettaisiin
    idempotenssin nojalla ikuisesti.

    Miksei ehjyyttä tarkisteta loppuun asti: se vaatisi koko virran
    purkamisen zstd:llä, eli noin gigatavun työtä demoa kohden. ``parse`` tekee
    sen joka tapauksessa, ja siellä katkennut tiedosto kaatuu -- tämä huomio on
    se, joka kertoo käyttäjälle mitä silloin pitää tehdä.
    """
    return (
        f"Lähde ei kertonut demon {map_demo_id} kokoa (ei Content-Length-"
        "otsaketta), joten latauksen ehjyyttä ei voitu todeta pituudesta. "
        "Alku on zstd-muotoa ja koko uskottava, mutta katkennut loppu "
        "paljastuisi vasta parsinnassa. Jos parse epäonnistuu tähän demoon, "
        "poista se ja aja fetch uudelleen."
    )

def _archive_outputs(
    archive: ArchivePaths, *paths: Path
) -> tuple[PurePosixPath, ...]:
    """Kirjoitetut tiedostot ``StageResult.outputs``in muodossa.

    **``outputs`` on sopimuksen mukaan arkiston sisäinen suhteellinen polku**,
    ja se on tarkoituksellinen rajoite: absoluuttinen polku rikkoisi arkiston
    toisella koneella. Kun demot menevät paikalliseen ``demos_root``iin, ne
    ovat arkiston ulkopuolella eikä sellaista polkua ole olemassa.

    Ratkaisu on **jättää ne pois eikä valehdella**: ulkopuolinen tiedosto ei
    saa esiintyä listassa, joka lupaa olevansa arkistopolkuja. Absoluuttiset
    polut kulkevat sen sijaan ``stats["demo_path"]``issa ja
    ``stats["meta_path"]``issa (ks. :func:`_location_stats`) -- ne ovat
    **tämän ajon tietoa** eivätkä arkistoon tallennettavaa tilaa, ja komento
    tulostaa ne käyttäjälle. Vaihtoehdot, jotka hylättiin: absoluuttinen polku
    ``outputs``iin (rikkoisi sopimuksen, jonka manifestit ja indeksit
    lukevat) ja arkiston juureen nähden suhteellinen ``..``-polku (jonka
    :meth:`~pappascout.archive.paths.ArchivePaths.resolve` hylkää -- ja
    perustellusti).
    """
    inside: list[PurePosixPath] = []
    for path in paths:
        try:
            inside.append(archive.relative(path))
        except ValueError:
            # Arkiston ulkopuolella. Ks. docstring: ei arvausta, ei riviä.
            continue
    return tuple(inside)


def _location_stats(
    demo_path: Path, meta_path: Path, *, downloaded_bytes: int
) -> dict[str, Any]:
    """Missä demo ja metatiedosto ovat -- **absoluuttisina, molemmissa moodeissa**.

    Aina mukana eikä vain paikallisessa moodissa: jos kenttä ilmestyisi vain
    silloin, kun demo on arkiston ulkopuolella, tulosteen olisi pääteltävä
    sijainti kentän olemassaolosta -- ja sitä päättelyä ei tarvitse tehdä
    kertaakaan, kun luku on aina siinä.
    """
    return {
        "downloaded_bytes": downloaded_bytes,
        "demo_path": str(demo_path),
        "meta_path": str(meta_path),
        "demos_dir": str(demo_path.parent),
    }


#: Neuvo, kun mikään muu ei tiedä parempaa.
#:
#: **Ei "aja komento uudelleen".** Juuri se oletus oli molempien live-ajossa
#: löytyneiden vikojen juurisyy: neuvo, joka tulee oletuksena, on neuvo jota
#: kukaan ei ole harkinnut tälle vialle. Tuntemattoman vian oikea neuvo on
#: sanoa, ettei sitä tiedetä.
DEFAULT_NEXT_STEP = (
    "Syy ei ole työkalun tuntema. Lue yllä oleva viesti ja tarkista sen "
    "perusteella, onko kyseessä verkko, asetukset vai levy."
)


def next_step(exc: BaseException) -> str:
    """Mitä käyttäjän pitää tehdä seuraavaksi tämän vian takia.

    **Neuvo tulee virheestä, ei otsikosta.** Ks.
    :class:`~pappascout.errors.PappascoutError`. Tämä funktio ei päättele
    neuvoa tilakoodista tai viestin sanoista -- se lukee sen kentästä, jonka
    virheen nostaja täytti. Päättely tekisi samaa arvausta, jonka tämä korjaus
    poistaa, ja se olisi kaukana siitä paikasta, jossa syy tiedetään.
    """
    advice = getattr(exc, "advice", None)
    if isinstance(advice, str) and advice.strip():
        return advice.strip()
    return DEFAULT_NEXT_STEP


def _result(
    map_demo_id: str,
    *,
    status: str,
    skipped: bool,
    outputs: Sequence[PurePosixPath],
    reason: str | None,
    started: float,
    stats: dict[str, Any],
) -> StageResult:
    """Vaiheen tulos, ja **epäonnistuneelle myös seuraava toimenpide**.

    Vartija eikä muotoilu: ilman sitä uusi vikapolku voisi tuottaa
    epäonnistumisen ilman neuvoa, ja tuloste joutuisi keksimään sellaisen --
    eli palaisi täsmälleen siihen oletukseen, jonka tämä rakenne poistaa.
    """
    if status != "ok" and not str(stats.get("next_step", "")).strip():
        raise AssertionError(
            f"Yksikön {map_demo_id} tila on {status!r} mutta seuraavaa "
            "toimenpidettä ei ole. Epäonnistuminen ilman neuvoa jättäisi "
            "käyttäjän arvaamaan -- lisää next_step."
        )
    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status=status,  # type: ignore[arg-type]
        skipped=skipped,
        outputs=tuple(outputs),
        # **Ei manifestia.** Manifesti vastaa kysymykseen "onko tulos ajan
        # tasalla syötteisiinsä nähden", mutta tämän vaiheen syöte on FACEITin
        # tallenne, jota ei voi verrata mihinkään ilman että se ladataan --
        # eli tekemällä juuri se työ, jonka ohitus säästäisi. Idempotenssi
        # ratkeaa tiedoston olemassaolosta, ja se on halvempi ja rehellisempi.
        manifest_path=None,
        reason=reason,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )
