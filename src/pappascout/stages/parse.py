"""``parse`` -- putken ensimmäinen vaihe: demosta viisi taulua.

Vaihe lukee yhden demon portin takaa ja kirjoittaa arkistoon
``parsed/<map_demo_id>/rounds.parquet``, ``.../ticks.parquet``,
``.../events.parquet``, ``.../lineups.parquet``, ``.../deaths.parquet``
sekä niiden yhteisen manifestin.

``rounds`` on kaksi riviä jokaista **pelattua** kierrosta kohden, yksi
kummallekin joukkueelle, ja kaikki arvot ovat demosta *havaittuja*: raha ja
varustearvo ostoajan lopussa, kierroksen alun varustearvo, eloonjääneet,
voittaja ja voiton syy. Kierrostyyppi, loss count ja muut johdokset syntyvät
vasta ``classify``-vaiheessa, joka laskee ne joka ajolla uudelleen.

``ticks`` on rivi per (pelaaja, kierros, näytepiste): alue, koordinaatit ja
elossaolo muutamassa hetkessä kierroksen alusta (``[parse].snapshot_seconds``)
sekä ensikontaktin hetkellä. Kymmenen pelaajaa tallentuu joka näytepisteessä
``is_alive``-lipulla; kuolleiden suodatus ja aggregointi ovat myöhempien
vaiheiden työtä (AD-10).

``events`` on rivi per utility-tapahtuma: heitto ja räjähdys ovat kaksi riviä,
jotka yhdistää ``grenade_no``, lentoradan demokohtainen tunniste. Kahden demon
taulut yhdistetään parilla ``(map_demo_id, grenade_no)``. Utility mitataan
**heitoista, ei ostoista** -- utilityä dropataan, joten ostaja ja heittäjä
voivat olla eri pelaajat. **Tyhjä tapahtumataulu on kelvollinen tulos**,
toisin kuin tyhjä kierros- tai näytepistetaulu: demossa on voitu jättää utility
heittämättä, mutta pelattuja kierroksia ja asetelmia siinä on aina.

``lineups`` on rivi per (kokoonpano, pelaaja): pelaajan nimi ja hänen
klaaninimensä. Se on **identiteettitaulu**, ei kierrostaulu -- nimi on sama
koko kartan ajan, joten sillä ei ole ``round_no``:ta eikä sen rivejä pudoteta
puukkokierroksen mukana. Nimi on havainto: puuttuva klaani on ``null``, ja
raportti sanoo puuttumisen ääneen sen sijaan että keksisi korvikkeen.

``deaths`` on rivi per kuolema: uhri ja ampuja molemmat alueineen ja
koordinaatteineen. Kuolemalla on **kaksi toimijaa**, joten se ei mahdu
``EVENTS``-taulun yhden toimijan muotoon -- eikä sen alue ole napsautus vaan
havainto samalta tapahtumalta. Ampujaton kuolema (putoaminen, pommi) on aito
tapaus: ampujan kentät ovat silloin tyhjiä eikä riviä pudoteta.

**Tyhjä kuolemataulu on virhe, tyhjä tapahtumataulu ei.** Epäsymmetria on
tarkoituksellinen, ja se seuraa siitä mitä tyhjyys kummassakin tarkoittaa.
Utility voi aidosti puuttua: joukkue voi jättää kranaatit ostamatta, ja
"nolla heittoa" on silloin havainto kierroksesta. Kuolema ei ole valinta. CS2:n
kierros ratkeaa tappamalla tai pommilla, ja pommikin tappaa; koko ottelu ilman
yhtään kuolemaa ei ole pelattu ottelu. Tyhjä kuolemataulu tarkoittaa siis aina
lukuvirhettä -- käytännössä uudelleennimettyä ``player_death``-tapahtumaa --
eikä havaintoa, ja ok-tuloksena se jäisi manifestin perusteella pysyvästi
ohitetuksi. Virheilmoitus nimeää adapterin omat pudotuslaskurit, joten syy on
luettavissa eikä arvattavissa.

Mitä tauluihin päätyy
---------------------
Warmup-kierrokset, puukkokierros ja ``mp_restartgame``-nollaukset **eivät ole
pelattuja kierroksia**: ne eivät saa kierrosnumeroa eivätkä päädy tauluun.
``round_raw`` on demon oma kierroslaskuri, joten ohitetut kierrokset näkyvät
taulussa aukkona (Ancient: ``round_no`` 1..21 vastaa ``round_raw`` 2..22) --
niiden lukumäärä kerrotaan myös ajon yhteenvedossa. Päätöksen tekee yksi ainoa
funktio, :func:`~pappascout.domain.rounds.mark_played_rounds`, jota vain tämä
vaihe kutsuu.

Sama päätös rajaa myös näytepisteet, utility-tapahtumat ja kuolemat:
adapteri tuottaa rivejä kaikilta ankkuroiduilta kierrosrajoilta, ja vaihe
pudottaa numeroimattomien kierrosten rivit samalla, kun se liittää
``round_no``:n avaimella ``round_raw``. Näin puukkokierros ei tuota tick-,
tapahtuma- eikä kuolemarivejä, eikä adapterin tarvitse tuntea
numerointisääntöä. **Puukkokierroksella kuollaan oikeasti**, joten juuri tämä
liitos on ainoa paikka, jossa ne kuolemat putoavat -- erillistä
puukkokierrossääntöä ei ole eikä saa olla.

Yhden tapauksen ratkaisee jo adapteri: **ottelun uudelleenaloitus**. Sillä on
freezetime-ankkuri mutta ei ``round_end``iä, ja demon oma kierrosnumerointi
jatkuu sen yli yhdellä -- se ei siis ole kierros lainkaan, eikä sillä ole edes
``round_raw``:ta, jonka varassa se voisi kulkea tänne asti. Adapteri ei tuota
siitä yhtään riviä, ja kertoo lukumäärän diagnostiikassaan
(``match_restarts``); vaihe välittää sen ajon yhteenvetoon. Luku on eri asia
kuin yllä mainittu ohitettujen kierrosten määrä: ohitettu kierros on taulussa
ilman ``round_no``:ta, uudelleenaloitus ei ole taulussa lainkaan.

Tarkistukset ennen kirjoitusta
------------------------------
Vaihe validoi sekä lukemansa että kirjoittamansa taulun (AD-2), ja lisäksi
kaksi asiaa, jotka pelkkä skeema ei näe:

* :func:`~pappascout.domain.rounds.check_win_reasons` -- CS2:n sääntö siitä,
  kuka voi voittaa kierroksen millä tavalla. Rikkomus tarkoittaa lähes aina,
  että puolet ovat menneet väärin päin.
* Rivimäärä on tasan kaksi per kierros. Yksi tai kolme riviä menisi skeemasta
  läpi mutta vääristäisi jokaisen myöhemmän joukkuekohtaisen summan.

Uudelleenajo
------------
Manifestin ``params_hash`` lasketaan **vain** ``[parse]``-osiosta, ja
``tool_versions`` sisältää vain demoparser2:n. ``[thresholds]``-arvon
muuttaminen ei siis voi invalidoida parsintaa -- se on koko AD-3:n
asetuspartition tarkoitus, ja tämä vaihe ei edes näe muita osioita.

**Skeemamuutos pakottaa uudelleenparsinnan.** Manifestin parametrihash ei
liiku, kun tauluihin tulee uusi sarake tai kokonaan uusi taulu, joten pelkkä
manifestin täsmäys ei riitä ohituksen ehdoksi: vaihe vaatii lisäksi, että
**jokainen** tämän version tuottama taulutiedosto on paikallaan
(``expected_outputs``) ja että ne vastaavat yhä voimassa olevaa sopimusta
(:func:`_schema_is_current`). Arkiston demo, jolta ``lineups.parquet``
puuttuu, ei siis ole ajan tasalla, ja se parsitaan uudelleen ilman
``--pakota``-lippua.

Demon tiiviste luetaan sen omasta ``.meta.json``-tiedostosta. Jos sitä ei ole
(käsin arkistoon kopioitu demo), tunnisteena on tiedoston **koko ja
muokkausaika**: 233 MB:n sha256 jokaisella ajolla olisi hitaampi kuin itse
parsinta. Muokkausaika on mukana, koska pelkkä koko ei huomaa muuttunutta
demoa. Jos OneDrive-synkronointi muuttaa aikaleimaa, seurauksena on yksi turha
uudelleenparsinta -- vaarattomampi virhe kuin vanhentunut tulos. Manifestin voi
aina ohittaa lipulla ``force``.

Virhepolitiikka
---------------
Sääntö on yksi: **kelvollista tulosta ei koskaan ylikirjoiteta
epäonnistumisella.** Jos parsinta kaatuu ja arkistossa on jo ehjät taulut ja
niiden ``ok``-manifesti, kaikki jätetään koskematta. Muussa tapauksessa
kirjoitetaan ``parse_failed``-manifesti, joka estää ohituksen ja kertoo syyn.
Vajaata taulua ei synny kummassakaan tapauksessa, koska kirjoitukset ovat
atomisia ja tapahtuvat vasta kaikkien tarkistusten jälkeen. Manifesti
kirjoitetaan viimeisenä, joten keskeytynyt ajo näkyy seuraavalla kerralla
puuttuvana tuloksena eikä ajantasaisena.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import polars as pl

from pappascout.adapters.protocols import (
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoParser,
    DemoTables,
)
from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    DEMO_SUFFIXES,
    ArchivePaths,
    parsed_manifest,
    parsed_table,
    safe_component,
)
from pappascout.constants import weapon_classification_digest
from pappascout.domain.models import ParseSettings
from pappascout.domain.rounds import check_win_reasons, mark_played_rounds
from pappascout.domain.sampling import FIRST_CONTACT_SAMPLE
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    DEATHS,
    EVENTS,
    LINEUPS,
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.domain.utility import DETONATE, THROWN
from pappascout.errors import DemoUnavailable, ParseError, SchemaError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TABLE",
    "TICKS_TABLE",
    "EVENTS_TABLE",
    "LINEUPS_TABLE",
    "DEATHS_TABLE",
    "TOOLS",
    "run",
    "resolve_demo",
    "map_demo_id_from_path",
    "default_parser",
]

@dataclass(frozen=True)
class _ParsedTables:
    """Yhden demon valmiit, tarkistetut taulut ja niiden pudotusluvut.

    Dataclass eikä monikko: paluuarvossa on viisi kehystä ja kolme paljasta
    kokonaislukua, ja kaksi vierekkäistä ``int``:iä on vaihdettavissa
    keskenään ilman että mikään tyyppitarkistus huomaa. Sama kuvio kuin
    :class:`~pappascout.adapters.protocols.DemoTables`illa ja adapterin
    laskuriluokilla -- ja seuraava taulu ei kasvata monikkoa yhdeksään.

    Attributes:
        skipped_rounds: Numeroimattomat kierrosrajat (warmup, puukkokierros).
        unnumbered_utility: Numeroimattomilta kierroksilta pudonneet heitot.
        unnumbered_deaths: Samoin pudonneet kuolemat. Liigademossa aina
            epätyhjä: puukkokierroksella kuollaan oikeasti.
    """

    rounds: pl.DataFrame
    ticks: pl.DataFrame
    events: pl.DataFrame
    lineups: pl.DataFrame
    deaths: pl.DataFrame
    skipped_rounds: int
    unnumbered_utility: int
    unnumbered_deaths: int


STAGE = "parse"
TABLE = "rounds"
TICKS_TABLE = "ticks"
EVENTS_TABLE = "events"
LINEUPS_TABLE = "lineups"
DEATHS_TABLE = "deaths"

#: Työkalut, joiden versio muuttaa tämän vaiheen tuloksen (manifest-moduulin
#: sääntö). Pappascoutin omaa versiota ei merkitä: korjauspäivitys ei saa
#: pakottaa koko arkiston uudelleenparsintaa.
TOOLS = ("demoparser2",)

#: Virheet, joista kirjataan yksikkökohtainen tila manifestiin (AD-9).
_RECORDED_ERRORS = (ParseError, SchemaError, OSError, pl.exceptions.PolarsError)


def default_parser(settings: ParseSettings) -> DemoParser:
    """Tuotannon demoparser2-toteutus.

    Tuonti on funktion sisällä, jotta tämän moduulin tuominen ei lataa
    demoparser2:ta -- vaihe itse tuntee vain portin, ja testit antavat sille
    feikin.

    Args:
        settings: ``[parse]``-osio. Ensikontaktin säännön parametrit ovat
            asetuksia eivätkä koodia, joten adapteri saa ne kutsussa eikä lue
            niitä itse.
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    return Demoparser2Adapter(
        exclude_weapons=settings.first_contact_exclude_weapons,
        fallback_death=settings.first_contact_fallback_death,
        area_snap_units=settings.area_snap_units,
        buy_window_seconds=settings.buy_window_seconds,
    )


def map_demo_id_from_path(path: Path) -> str:
    """Päättele ``map_demo_id`` demotiedoston nimestä.

    FACEITin tiedostonimi on tunniste sellaisenaan, joten pääte riisutaan ja
    loppu tarkistetaan polun osaksi kelpaavaksi.
    """
    name = Path(path).name
    for suffix in sorted(DEMO_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        name = Path(name).stem
    return safe_component(name, "map_demo_id")


def resolve_demo(archive: ArchivePaths, target: str) -> tuple[str, Path]:
    """Tulkitse käyttäjän antama kohde tiedostoksi ja tunnisteeksi.

    Kohde saa olla joko polku demotiedostoon tai pelkkä ``map_demo_id``, jolloin
    demo etsitään arkiston ``demos/``- ja ``import/``-hakemistoista.

    Raises:
        DemoUnavailable: Jos demoa ei löydy. Viesti kertoo, mistä etsittiin.
    """
    candidate = Path(target).expanduser()
    if candidate.is_file():
        return map_demo_id_from_path(candidate), candidate

    map_demo_id = safe_component(target, "map_demo_id")
    found = archive.find_demo(map_demo_id)
    if found is not None:
        return map_demo_id, found

    import_dir = archive.import_dir()
    for suffix in DEMO_SUFFIXES:
        path = import_dir / f"{map_demo_id}{suffix}"
        if path.is_file():
            return map_demo_id, path

    searched = [str(archive.demo(map_demo_id, s)) for s in DEMO_SUFFIXES]
    searched += [str(import_dir / f"{map_demo_id}{s}") for s in DEMO_SUFFIXES]
    listing = "\n".join(f"    {p}" for p in searched)
    raise DemoUnavailable(
        f"Demoa {map_demo_id} ei löytynyt.\n"
        "Etsin näistä poluista:\n"
        f"{listing}\n"
        "Kopioi demo arkiston import-hakemistoon tai anna tiedoston polku "
        "suoraan komennolle."
    )


def _demo_fingerprint(archive: ArchivePaths, map_demo_id: str, demo_path: Path) -> str:
    """Demon tunniste manifestin syötelistaan.

    Ensisijaisesti ``demos/<id>.meta.json``-tiedoston ``sha256``; muuten
    tiedoston koko ja muokkausaika. Tiivistettä ei lasketa demosta uudelleen
    (ks. moduulin docstring).

    Raises:
        DemoUnavailable: Jos tiedoston tietoja ei saada luettua. Yhteinen
            varakonstantti olisi vaarallinen: kaksi eri lukukelvotonta demoa
            saisi saman tunnisteen, jolloin toisen tulos näyttäisi toisen
            ajantasaiselta tulokselta.
    """
    meta_path = archive.demo_meta(map_demo_id)
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        sha = meta.get("sha256")
        if isinstance(sha, str) and sha:
            return sha
    try:
        stat = demo_path.stat()
    except OSError as exc:
        raise DemoUnavailable(
            f"Demon {demo_path} tietoja ei voitu lukea: {exc}\n"
            "Tarkista, ettei tiedosto ole OneDriven pilvipaikkamerkki (avaa se "
            "kerran Resurssienhallinnassa) ja että polku on oikea."
        ) from exc
    return f"size-{stat.st_size}-mtime-{stat.st_mtime_ns}"


def _params_hash(settings: ParseSettings) -> str:
    """Parametrihash: ``[parse]``-osio **ja** aseluokittelun tiiviste.

    Kalustolaskuri lukee aseluettelon koodista eikä asetuksista, joten pelkkä
    osion hash jättäisi luettelon muutoksen näkymättömäksi: arkiston vanha
    laskuri jäisi voimaan eikä mikään kertoisi siitä. Tiiviste luokittelun
    sisällöstä pakottaa uudelleenparsinnan ilman että kenenkään tarvitsee
    muistaa nostaa versionumeroa.

    Osiot ovat **kaksitasoisessa rakenteessa** eivätkä sisaruksina samassa
    sanakirjassa: sisarusavaimena tiivisteen voisi peittää samanniminen
    ``[parse]``-asetus, ja se pitäisi torjua vartijalla, jota mikään ei voi
    laukaista. Sisäkkäisyys tekee törmäyksen mahdottomaksi rakenteen tasolla.
    """
    return compute_params_hash(
        {
            "parse": settings.model_dump(mode="json"),
            "weapon_classification": weapon_classification_digest(),
        }
    )


def _check_port_columns(
    df: pl.DataFrame,
    expected_columns: tuple[str, ...],
    table_name: str,
    contract: str,
) -> None:
    """Tarkista adapterin tuottama taulu ennen jatkokäsittelyä.

    Vaatimus on **täsmällinen sarakejoukko**, ei osajoukko: ylimääräinen sarake
    tarkoittaa, että portin toteutus ja sopimus ovat erkaantuneet, ja se
    päätyisi hiljaa mukaan tai kaatuisi vasta domain-kerroksessa.
    """
    received = set(df.columns)
    expected = set(expected_columns)
    missing = sorted(expected - received)
    extra = sorted(received - expected)
    if not missing and not extra:
        return
    parts = []
    if missing:
        parts.append(f"puuttuu: {', '.join(missing)}")
    if extra:
        parts.append(f"ylimääräisiä: {', '.join(extra)}")
    raise SchemaError(
        f"Demoportti palautti sopimuksen vastaisen {table_name} -- "
        f"{'; '.join(parts)}.\n"
        f"Portin sopimus on adapters/protocols.py:n {contract}."
    )


def _check_two_rows_per_round(df: pl.DataFrame) -> None:
    """Jokaisella kierroksella on oltava tasan kaksi riviä.

    Taulu on pitkä: yksi rivi kummallekin joukkueelle. Kolmas rivi tai
    puuttuva pari läpäisisi skeeman mutta kaksinkertaistaisi tai puolittaisi
    joukkuekohtaiset summat kaikessa myöhemmässä analyysissa.
    """
    round_count = df["round_no"].n_unique()
    if df.height == 2 * round_count:
        return
    deviating = (
        df.group_by("round_no")
        .len()
        .filter(pl.col("len") != 2)
        .sort("round_no")
        .head(5)
    )
    raise SchemaError(
        f"Kierrostaulussa on {df.height} riviä {round_count} kierrokselle; "
        "pitäisi olla tasan kaksi riviä per kierros (yksi kummallekin "
        "joukkueelle).\n"
        f"Poikkeavat kierrokset: {deviating.to_dicts()}"
    )


def _check_player_counters(df: pl.DataFrame) -> None:
    """Varmista, että pelaajalaskurit pysyvät jakajassaan ja sisäkkäin.

    ``validate`` tarkistaa sarakkeet ja tyypit muttei arvoja, ja juuri arvot
    ovat näiden kahden sarakkeen koko lupaus. Kaksi invarianttia, jotka
    seuraavat siitä että molemmat lasketaan **samasta pelaajajoukosta samalta
    tickiltä**:

    * ``0 <= laskuri <= players_buy_end`` -- ylitys tarkoittaisi kahta eri
      jakajaa samalla rivillä, ja raportti näyttäisi "6/5".
    * ``players_armed_buy_end <= players_armored_buy_end`` -- aseistetun ehto
      sisältää panssarin, joten aseistetut ovat panssaroitujen **osajoukko**.
      Ylitys tarkoittaisi, että laskurit lukevat eri tickiä tai eri joukkoa.

    Tarkistus tehdään **lukuhetkellä** eikä vain luvata skeeman docstringissä:
    ilman sitä mahdoton luku kirjoittuisi arkistoon ja paljastuisi vasta
    raportissa, jos silloinkaan. Null-arvot ohitetaan -- ne ovat rehellinen
    "ei havaintoa" eivätkä rikkoutunut invariantti.

    Raises:
        SchemaError: Nimeää rikkoutuneen invariantin ja enintään viisi riviä.
    """
    checks = (
        (
            (pl.col(ARMED_COLUMN) < 0)
            | (pl.col(ARMED_COLUMN) > pl.col("players_buy_end")),
            f"{ARMED_COLUMN} on rajojen 0..players_buy_end ulkopuolella",
        ),
        (
            (pl.col(ARMORED_COLUMN) < 0)
            | (pl.col(ARMORED_COLUMN) > pl.col("players_buy_end")),
            f"{ARMORED_COLUMN} on rajojen 0..players_buy_end ulkopuolella",
        ),
        (
            pl.col(ARMED_COLUMN) > pl.col(ARMORED_COLUMN),
            f"{ARMED_COLUMN} > {ARMORED_COLUMN}, vaikka aseistetun ehto "
            "sisältää panssarin -- aseistettujen on oltava panssaroitujen "
            "osajoukko",
        ),
    )
    for condition, complaint in checks:
        broken = df.filter(condition.fill_null(False))
        if broken.is_empty():
            continue
        rows = broken.select(
            "round_no", "side", "players_buy_end", ARMED_COLUMN, ARMORED_COLUMN
        ).head(5)
        raise SchemaError(
            f"Kierrostaulussa {complaint}: {broken.height} riviä.\n"
            f"Ensimmäiset: {rows.to_dicts()}"
        )


def _check_grenade_key(events: pl.DataFrame) -> None:
    """Varmista, että ``(map_demo_id, grenade_no, event_kind)`` yksilöi rivin.

    ``validate`` tarkistaa sarakkeet ja tyypit muttei avainta, ja juuri avain
    on tämän taulun koko lupaus: aggregointi liittää utilityn kierroksiin sillä
    oletuksella, ettei liitos monista rivejä. Rikkoutunut avain läpäisisi
    skeeman ja näkyisi vasta raportin luvuissa kaksinkertaisena savuna --
    täsmälleen se hiljainen vika, jonka takia ``grenade_no`` ylipäätään on
    olemassa.

    ``map_demo_id`` on avaimessa mukana, koska ``grenade_no`` juoksee **demon
    sisällä**: kahden demon taulut yhdistävä ``aggregate`` tarvitsee parin.
    Yhden demon taulussa se on vakio eikä muuta tulosta, mutta väite on
    kirjoitettava siinä muodossa, jossa sitä käytetään.

    Raises:
        SchemaError: Jos numero puuttuu tai jos sama avain esiintyy kahdesti.
    """
    if events.is_empty():
        return

    missing = int(events["grenade_no"].null_count())
    if missing:
        raise SchemaError(
            f"Tapahtumataulussa on {missing} riviä ilman grenade_no-numeroa.\n"
            "Numero on heiton ja räjähdyksen ainoa side; ilman sitä riviä ei "
            "voi liittää mihinkään."
        )

    key = events.select("map_demo_id", "grenade_no", "event_kind")
    if key.height == key.unique().height:
        return
    duplicates = (
        events.group_by("map_demo_id", "grenade_no", "event_kind")
        .len()
        .filter(pl.col("len") > 1)
        .sort("grenade_no")
        .head(5)
    )
    raise SchemaError(
        "Tapahtumataulun avain (map_demo_id, grenade_no, event_kind) ei ole "
        f"yksikäsitteinen: {key.height - key.unique().height} riviä on "
        "kaksoiskappaleita.\n"
        f"Ensimmäiset toistuvat avaimet: {duplicates.to_dicts()}"
    )


def _round_stats(df: pl.DataFrame, skipped_rounds: int = 0) -> dict[str, object]:
    """Kierrostaulun luvut."""
    if df.is_empty():
        stats: dict[str, object] = {
            "rounds": 0,
            "rows": 0,
            "max_round_no": 0,
            "skipped_rounds": skipped_rounds,
            "no_freeze_end": 0,
        }
    else:
        stats = {
            "rounds": int(df["round_no"].n_unique()),
            "rows": int(df.height),
            "max_round_no": int(df["round_no"].max() or 0),
            "skipped_rounds": skipped_rounds,
            "no_freeze_end": int(
                df.filter(pl.col("status") == "no_freeze_end")["round_no"].n_unique()
            ),
        }
    # Myös tyhjästä taulusta: muut luvut palautetaan nollina, joten laskurin
    # puuttuminen tekisi tyhjästä ajosta samannäköisen kuin laskurittomasta
    # versiosta.
    stats.update(_armed_stats(df))
    stats.update(_armored_stats(df))
    return stats


def _column_distribution(df: pl.DataFrame, column: str) -> tuple[dict[int, int], int]:
    """Yhden laskurisarakkeen arvojakauma ja puuttuvien rivien määrä.

    Jaettu kahden laskurin kesken tarkoituksella: aseistetut ja panssaroidut
    ovat **eri havaintoja samasta tickistä**, ja kaksi erillistä laskentaa
    voisi erkaantua toisistaan -- toinen laskisi ankkurittoman kierroksen
    nollaksi ja toinen puuttuvaksi, eikä lukuja voisi enää verrata.
    """
    counts = (
        df.filter(pl.col(column).is_not_null()).group_by(column).len().sort(column)
    )
    return (
        {int(value): int(rows) for value, rows in zip(counts[column], counts["len"])},
        int(df[column].null_count()),
    )


def _armed_stats(df: pl.DataFrame) -> dict[str, object]:
    """Kalustolaskurin **arvojakauma** kierrostaulusta.

    Jakauma kerrotaan ajon yhteydessä, koska laskuri on ainoa havainto, jonka
    oikeellisuuden voi tarkistaa vain katsomalla sitä: väärä sääntö tai
    vanhentunut aseluettelo tuottaisi taulun, joka läpäisee jokaisen
    skeematarkistuksen.

    Nimenomaan jakauma eikä ääripäät: 41 riviä nollaa ja yksi viitonen antaisi
    ``0-5``, joka näyttää terveeltä. ``{0: 41, 5: 1}`` ei näytä.

    Returns:
        ``armed_distribution`` (arvo -> rivien määrä, arvon mukaan
        järjestettynä) ja ``armed_missing`` (rivit, joilta havainto puuttuu).
    """
    distribution, missing = _column_distribution(df, ARMED_COLUMN)
    return {"armed_distribution": distribution, "armed_missing": missing}


def _armored_stats(df: pl.DataFrame) -> dict[str, object]:
    """Panssarilaskurin **arvojakauma** kierrostaulusta.

    Oma rivinsä eikä aseistettujen jakauman jatke: ne ovat eri havaintoja ja
    eroavat eniten pistoolikierroksella, jolla aseistettuja on käytännössä 0.
    Yksi rivi kahdesta luvusta peittäisi juuri sen eron, jonka vuoksi
    laskureita on kaksi.

    Jakauma toimii samalla itsetarkistuksena: jos panssarilaskuri ajautuisi
    lukemaan aseistettujen ehtoa, jakaumat olisivat identtiset -- ja se näkyy
    ajon tulosteessa heti eikä vasta raportissa.

    Returns:
        ``armored_distribution`` (arvo -> rivien määrä) ja ``armored_missing``
        (rivit, joilta havainto puuttuu).
    """
    distribution, missing = _column_distribution(df, ARMORED_COLUMN)
    return {"armored_distribution": distribution, "armored_missing": missing}


def _buy_window_stats(
    df: pl.DataFrame, diagnostics: object
) -> dict[str, object]:
    """Ostoikkunan luvut **pelatuista kierroksista**, ei kaikista kierrosrajoista.

    Adapteri antaa katkaisut ``round_raw``-numeroina eikä valmiina lukuina, ja
    syy on puukkokierros: se saa oman ``round_raw``:nsa mutta ei ole kierros
    eikä päädy tauluun. Adapterin laskema luku sisältäisi sen, eikä sitä voisi
    enää vähentää pois -- käyttäjä näkisi "13 katkaisua" taulussa, jossa niitä
    on 12. Sama koskee mittaushetkien jakaumaa: puukkokierros ratkeaa ennen
    ikkunan loppua, joten sen mukanaolo vetäisi jakauman alarajan sekunnin
    murto-osiin ja väittäisi mittausta, jota kierroslistalla ei ole.

    Mittaushetkien jakauma lasketaan kokonaan täällä, suoraan kirjoitettavasta
    taulusta: se on ``buy_end_tick``-sarakkeen ainoa näkyvä muoto, ja jos
    sarake ja tuloste voisivat erkaantua, tarkistettavuus olisi näennäistä.

    Args:
        df: Valmis kierrostaulu, vain pelatut kierrokset.
        diagnostics: Portin diagnostiikka, tai ``None``.

    Returns:
        Vain ne avaimet, jotka voidaan laskea. Puuttuva avain tarkoittaa
        ohitettua ajoa, ja ``cli`` jättää rivin silloin pois -- "ei yhtään"
        olisi väite, jota mikään ei tue.
    """
    if diagnostics is None:
        return {}

    played = set(df["round_raw"].to_list()) if not df.is_empty() else set()
    cuts = [
        (round_raw, missed)
        for round_raw, missed in getattr(diagnostics, "buy_window_cuts", ()) or ()
        if round_raw in played
    ]
    unchecked = [
        round_raw
        for round_raw in getattr(diagnostics, "buy_window_unchecked_cuts", ()) or ()
        if round_raw in played
    ]

    stats: dict[str, object] = {
        "buy_window_seconds": getattr(diagnostics, "buy_window_seconds", None),
        "buy_end_offsets_s": _buy_end_offsets(df),
        "buy_window_truncated_by_death": len(cuts),
        "buy_window_purchases_after_cut": sum(missed for _, missed in cuts),
        "buy_window_cuts_unchecked": len(unchecked),
        "buy_window_rounds_with_lost_purchases": tuple(
            round_raw for round_raw, missed in cuts if missed
        ),
    }
    for name in (
        "buy_window_ticks_without_players",
        "buy_window_players_lost",
        "buy_window_sides_without_rows",
        "buy_window_refunds",
        "buy_window_stale_equipment",
    ):
        stats[name] = getattr(diagnostics, name, 0)
    return stats


def _buy_end_offsets(df: pl.DataFrame) -> tuple[float, float, float] | None:
    """Mittaushetkien jakauma sekunteina ankkurista: ``(pienin, mediaani, suurin)``.

    Asetus lupaa ikkunan pituuden; nämä kolme lukua kertovat mihin mittaus
    oikeasti osui. Jos ikkuna on 20 s mutta mediaani 12 s, kuolema katkaisee
    ikkunan useammin kuin ei -- eikä sitä näe asetuksesta.

    Returns:
        Kolmikko sekunteina, tai ``None`` jos yhdelläkään rivillä ei ole
        molempia tickejä eikä kelvollista tickratea.
    """
    if df.is_empty():
        return None
    frame = df.filter(
        pl.col("buy_end_tick").is_not_null()
        & pl.col("freeze_end_tick").is_not_null()
        & (pl.col("tick_rate") > 0)
    )
    if frame.is_empty():
        return None
    offsets = (frame["buy_end_tick"] - frame["freeze_end_tick"]) / frame["tick_rate"]
    return float(offsets.min()), float(offsets.median()), float(offsets.max())


def _stats(
    df: pl.DataFrame,
    ticks: pl.DataFrame,
    events: pl.DataFrame,
    lineups: pl.DataFrame,
    deaths: pl.DataFrame,
    skipped_rounds: int = 0,
) -> dict[str, object]:
    """Käyttäjälle näytettävät luvut valmiista tauluista."""
    stats = _round_stats(df, skipped_rounds)
    stats.update(_tick_stats(ticks))
    stats.update(_event_stats(events))
    stats.update(_lineup_stats(lineups))
    stats.update(_death_stats(deaths))
    return stats


def _tick_stats(ticks: pl.DataFrame) -> dict[str, object]:
    """Näytepisteiden luvut.

    ``sample_points`` on kierros x hetki, ei rivimäärä: kymmenen pelaajan rivit
    ovat sama näytepiste. ``first_contact_rounds`` kertoo, monellako
    kierroksella kontakti ylipäätään löytyi -- pelkkä rivimäärä ei paljastaisi,
    jos sääntö jäisi puremattomaksi.
    """
    if ticks.is_empty():
        return {
            "tick_rows": 0,
            "sample_points": 0,
            "sample_rounds": 0,
            "first_contact_rounds": 0,
        }
    contact = ticks.filter(pl.col("sample_kind") == FIRST_CONTACT_SAMPLE)
    return {
        "tick_rows": int(ticks.height),
        "sample_points": int(
            ticks.select("round_no", "sample_kind", "sample_t_s").n_unique()
        ),
        "sample_rounds": int(ticks["round_no"].n_unique()),
        "first_contact_rounds": int(contact["round_no"].n_unique()),
    }


def _event_stats(events: pl.DataFrame) -> dict[str, object]:
    """Utility-tapahtumien luvut.

    Heitot ja räjähdykset erikseen, koska niiden **erotus** on itsessään
    havainto: räjähtämättömiä kranaatteja on aina muutama, mutta iso ero
    tarkoittaisi, että radan loppu jää tunnistamatta.

    Alueesta kerrotaan kolme lukua, koska ne ovat kolmea eri laatua olevaa
    tietoa eivätkä saa niputtua yhdeksi:

    ``utility_area_observed``
        Heittäjän oma alue. Havainto.
    ``utility_area_snapped``
        Räjähdyksen alue lähimmältä elossa olevalta pelaajalta. Arvio, jonka
        luotettavuuden ``snap_distance`` kertoo rivikohtaisesti.
    ``utility_area_unnamed``
        Napsautus osui, mutta lähimmällä pelaajalla ei ole aluenimeä. Sekin on
        havainto -- pelin nimeämätön alue -- eikä sama asia kuin "kukaan ei
        ollut lähellä".

    ``utility_without_area`` on näiden ulkopuolelle jäävä loppu: alue puuttuu
    kokonaan.
    """
    if events.is_empty():
        return {
            "event_rows": 0,
            "utility_throws": 0,
            "utility_detonations": 0,
            "utility_rounds": 0,
            "utility_area_observed": 0,
            "utility_area_snapped": 0,
            "utility_area_unnamed": 0,
            "utility_without_area": 0,
        }
    kinds = events["event_kind"]
    sources = events["area_source"]
    return {
        "event_rows": int(events.height),
        "utility_throws": int((kinds == THROWN).sum()),
        "utility_detonations": int((kinds == DETONATE).sum()),
        "utility_rounds": int(events["round_no"].n_unique()),
        "utility_area_observed": int((sources == "observed").sum()),
        "utility_area_snapped": int((sources == "snapped").sum()),
        "utility_area_unnamed": int(
            events.filter(
                pl.col("area").is_null() & pl.col("snap_distance").is_not_null()
            ).height
        ),
        "utility_without_area": int(events["area"].null_count()),
    }


def _death_stats(deaths: pl.DataFrame) -> dict[str, object]:
    """Kuolemataulun luvut.

    Viisi lukua, viisi eri kysymystä. ``death_rows`` ja ``death_rounds``
    kertovat, syntyikö aineistoa lainkaan. Kolme muuta ovat kattavuuslukuja,
    ja ne ovat erikseen siksi, että ne tarkoittavat eri asioita:

    ``deaths_without_attacker``
        Ampujaton kuolema -- putoaminen tai pommi. **Havainto eikä vika**.
        Mitattu ``Ancient_vs_kaljukostaja``: kaksi ``planted_c4``-riviä
        151:stä, joista toinen osui kierrosten väliin eikä siis päädy tähän
        tauluun lainkaan -- valmiissa taulussa luku on 1.
    ``deaths_without_victim_area``
        Uhrin alue puuttuu. Mitatussa aineistossa **nolla**, ja juuri siksi
        luku on olemassa: nollasta poikkeava arvo tarkoittaa, että
        aluehavainto on rikkoutunut.
    ``deaths_without_attacker_area``
        Ampujan alue puuttuu **vaikka ampuja tunnetaan**. Ampujattomat rivit
        eivät ole tässä luvussa: ne on jo laskettu yllä, ja yhteinen luku
        näyttäisi aluevialta.
    """
    if deaths.is_empty():
        return {
            "death_rows": 0,
            "death_rounds": 0,
            "deaths_without_attacker": 0,
            "deaths_without_victim_area": 0,
            "deaths_without_attacker_area": 0,
        }
    return {
        "death_rows": int(deaths.height),
        "death_rounds": int(deaths["round_no"].n_unique()),
        "deaths_without_attacker": int(deaths["attacker_id"].null_count()),
        "deaths_without_victim_area": int(deaths["victim_area"].null_count()),
        "deaths_without_attacker_area": int(
            deaths.filter(
                pl.col("attacker_id").is_not_null()
                & pl.col("attacker_area").is_null()
            ).height
        ),
    }


def _lineup_stats(lineups: pl.DataFrame) -> dict[str, object]:
    """Kokoonpanotaulun luvut, **kokoonpano kerrallaan**.

    Erittely ei ole koristetta. Demo sisältää molempien joukkueiden rivit, ja
    yhteisluku vastaisi eri kysymykseen kuin se, jonka käyttäjä esittää:
    "onko *tällä* joukkueella nimi" ei ratkea listasta, joka on epätyhjä heti
    kun vastustajalla on klaani. Sama koskee nimettömiä pelaajia -- ne rivit
    voisivat kaikki kuulua vastustajalle.

    ``lineup_key`` on mukana jokaisella rivillä, koska käyttäjän seuraava
    komento on ``classify --team <lineup_key>`` ja vaiheella on molemmat arvot
    kädessä. Ilman sitä tuloste kertoisi nimen mutta ei sitä, mitä nimen
    tilalle kirjoitetaan komentoriville.

    Returns:
        ``lineup_rows`` ja ``lineups``, jälkimmäinen monikkona
        ``(lineup_key, klaani tai None, pelaajia, nimettömiä)``
        ``lineup_key``-järjestyksessä.
    """
    if lineups.is_empty():
        return {"lineup_rows": 0, "lineups": ()}
    grouped = (
        lineups.group_by("lineup_key")
        .agg(
            # Klaani on kokoonpanon ominaisuus: yksi arvo per kokoonpano.
            # ``max`` on vain deterministinen valinta yhden alkion joukosta --
            # useampi arvo olisi vika, jonka adapterin
            # ``lineup_clan_conflicts`` paljastaa.
            pl.col("clan_name").drop_nulls().unique().sort().alias("clans"),
            pl.len().alias("players"),
            pl.col("player_name").null_count().alias("without_name"),
        )
        .sort("lineup_key")
    )
    return {
        "lineup_rows": int(lineups.height),
        "lineups": tuple(
            (
                str(row["lineup_key"]),
                str(row["clans"][0]) if row["clans"] else None,
                int(row["players"]),
                int(row["without_name"]),
            )
            for row in grouped.iter_rows(named=True)
        ),
    }


def _read_table(path: Path) -> pl.DataFrame | str:
    """Lue taulu tai palauta virheen kuvaus merkkijonona."""
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _schema_is_current(
    table_abs: Path,
    ticks_abs: Path,
    events_abs: Path,
    lineups_abs: Path,
    deaths_abs: Path,
) -> bool:
    """Vastaavatko arkiston valmiit taulut yhä voimassa olevaa sopimusta.

    Täsmäävä manifesti ei yksin riitä -- sama syy kuin
    :func:`pappascout.stages.classify._usable_result`issa. Tulostaulun skeema
    voi muuttua ilman että manifestin sisältö muuttuu: parametrihash lasketaan
    ``[parse]``-osiosta ja demoparser2:n versiosta (AD-3), eikä kumpikaan
    liiku, kun ``EVENTS`` saa uuden sarakkeen. Ilman tätä tarkistusta vanha
    taulu jäisi hiljaa voimaan ja näyttäisi ajantasaiselta, kunnes joku
    myöhempi vaihe kaatuisi siihen.

    Lukukelvoton taulu on **eri vika eikä tämän funktion asia**: se ei parane
    demon uudelleenluvusta sen todennäköisemmin kuin ilman sitä, ja sillä on
    jo oma raportointinsa :func:`_existing_stats`issa. Tässä ratkaistaan vain
    se, onko ehjä taulu edelleen sopimuksen mukainen.

    Returns:
        ``False`` heti ensimmäisestä sopimusrikosta -- silloin vaihe ajetaan
        uudelleen ja taulut kirjoitetaan nykyisillä sarakkeilla.
    """
    for path, schema, name in (
        (table_abs, ROUNDS, TABLE),
        (ticks_abs, TICKS, TICKS_TABLE),
        (events_abs, EVENTS, EVENTS_TABLE),
        (lineups_abs, LINEUPS, LINEUPS_TABLE),
        (deaths_abs, DEATHS, DEATHS_TABLE),
    ):
        df = _read_table(path)
        if isinstance(df, str):
            continue
        try:
            validate(df, schema, name)
        except SchemaError:
            return False
    return True


def _existing_stats(
    table_abs: Path,
    ticks_abs: Path,
    events_abs: Path,
    lineups_abs: Path,
    deaths_abs: Path,
) -> dict[str, object]:
    """Luvut ohitettuun ajoon: luetaan valmiit taulut, ei parsita demoa.

    Taulut luetaan **erikseen**. Yhteinen try-lohko hukkaisi kierrosluvut
    silloin, kun vain näytepistetaulu on lukukelvoton, ja käyttäjä näkisi
    "lukuja ei saatu" myös siitä mikä oli aivan ehjä.

    Jos tulosta ei saada luettua, palautetaan tieto siitä eikä nollia --
    nollarivi näyttäisi siltä, että demossa ei ollut yhtään kierrosta.
    """
    rounds = _read_table(table_abs)
    ticks = _read_table(ticks_abs)
    events = _read_table(events_abs)
    lineups = _read_table(lineups_abs)
    deaths = _read_table(deaths_abs)

    # Kierros- ja näytepistetaulu ovat vaiheen ydintulos. Jos **kumpikaan** ei
    # aukea, koko tulos on lukukelvoton eikä siitä koota osittaista
    # yhteenvetoa: pelkkien utility-lukujen näyttäminen antaisi vaikutelman
    # ajantasaisesta tuloksesta.
    if isinstance(rounds, str) and isinstance(ticks, str):
        return {"unreadable": rounds}

    stats: dict[str, object] = {}
    if isinstance(rounds, str):
        stats["unreadable"] = rounds
    else:
        stats.update(_round_stats(rounds))
    if isinstance(ticks, str):
        stats["ticks_unreadable"] = ticks
    else:
        stats.update(_tick_stats(ticks))
    if isinstance(events, str):
        stats["events_unreadable"] = events
    else:
        stats.update(_event_stats(events))
    if isinstance(lineups, str):
        stats["lineups_unreadable"] = lineups
    else:
        stats.update(_lineup_stats(lineups))
    if isinstance(deaths, str):
        stats["deaths_unreadable"] = deaths
    else:
        stats.update(_death_stats(deaths))
    return stats


def run(
    settings: ParseSettings,
    archive: ArchivePaths,
    map_demo_id: str,
    parser: DemoParser,
    *,
    demo_path: Path | None = None,
    force: bool = False,
) -> StageResult:
    """Parsi yksi demo viideksi tauluksi.

    Args:
        settings: ``[parse]``-osio -- ainoa osio, jonka tämä vaihe näkee.
        archive: Arkiston polut.
        map_demo_id: Yksikön tunniste, ``{match_id}-{map_index}``.
        parser: Demoportti (AD-8). Tuotannossa :func:`default_parser`.
        demo_path: Demotiedosto. Oletuksena etsitään arkistosta.
        force: Ohita manifestin täsmäys ja parsi joka tapauksessa.

    Returns:
        :class:`~pappascout.stages.StageResult`, jossa ``stats`` kertoo
        kierrosten määrän, rivimäärän, suurimman kierrosnumeron, ohitettujen ja
        ankkurittomien kierrosten lukumäärän, näytepisteiden ja
        ensikontaktien määrän sekä utility-heittojen, räjähdysten ja
        aluettomien tapahtumien määrän.

    Raises:
        DemoUnavailable: Jos demoa ei löydy tai sitä ei voi lukea.
        ~pappascout.errors.ParseError: Jos demo ei ole luettavissa tai sen
            sisältö rikkoo CS2:n sääntöjä.
        ~pappascout.errors.SchemaError: Jos tulos ei vastaa sopimusta.
    """
    started = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    if demo_path is None:
        _, demo_path = resolve_demo(archive, map_demo_id)
    demo_path = Path(demo_path)

    table_rel = parsed_table(map_demo_id, TABLE)
    ticks_rel = parsed_table(map_demo_id, TICKS_TABLE)
    events_rel = parsed_table(map_demo_id, EVENTS_TABLE)
    lineups_rel = parsed_table(map_demo_id, LINEUPS_TABLE)
    deaths_rel = parsed_table(map_demo_id, DEATHS_TABLE)
    manifest_rel = parsed_manifest(map_demo_id)
    table_abs = archive.resolve(table_rel)
    ticks_abs = archive.resolve(ticks_rel)
    events_abs = archive.resolve(events_rel)
    lineups_abs = archive.resolve(lineups_rel)
    deaths_abs = archive.resolve(deaths_rel)
    manifest_abs = archive.resolve(manifest_rel)

    result_id = str(PurePosixPath("parsed") / map_demo_id)
    inputs = [
        ManifestInput(
            result_id=f"demo/{map_demo_id}",
            sha256=_demo_fingerprint(archive, map_demo_id, demo_path),
        )
    ]
    params_hash = _params_hash(settings)
    versions = tool_versions(*TOOLS)

    # Tulokset, jotka **tämän version** ajo tuottaa. Manifestin oma
    # outputs-lista ei riita ohituksen ehdoksi: vanha manifesti nimeaa vain ne
    # taulut, jotka silloinen koodi osasi kirjoittaa, joten uusi taulu ei
    # koskaan syntyisi arkistoon jo parsituille demoille.
    expected_outputs = (
        (table_rel, table_abs),
        (ticks_rel, ticks_abs),
        (events_rel, events_abs),
        (lineups_rel, lineups_abs),
        (deaths_rel, deaths_abs),
    )

    existing = Manifest.read_if_exists(manifest_abs)
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions=versions,
            root=archive.root,
        )
        and all(path.is_file() for _, path in expected_outputs)
        # Sopimus viimeisenä: se lukee taulut, ja halvemmat ehdot karsivat
        # suurimman osan ajoista jo ennen sitä.
        and _schema_is_current(
            table_abs, ticks_abs, events_abs, lineups_abs, deaths_abs
        )
    ):
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in existing.outputs),
            manifest_path=manifest_rel,
            reason=(
                "Tulos on ajan tasalla: manifesti täsmää eikä demoa tarvitse "
                "parsia uudelleen."
            ),
            duration_s=time.perf_counter() - started,
            stats=_existing_stats(
                table_abs, ticks_abs, events_abs, lineups_abs, deaths_abs
            ),
        )

    try:
        parsed = _parse_tables(parser, settings, demo_path, map_demo_id)
        df = parsed.rounds
        # Kirjoitus on saman virhekäsittelyn sisällä kuin parsinta: levy voi
        # täyttyä tai OneDrive lukita tiedoston, ja silloinkin manifestiin on
        # jäätävä merkintä epäonnistumisesta -- muuten seuraava ajo ohittaisi
        # vaiheen puolikkaan tuloksen päältä.
        _write_tables(
            (
                (table_abs, df),
                (ticks_abs, parsed.ticks),
                (events_abs, parsed.events),
                (lineups_abs, parsed.lineups),
                (deaths_abs, parsed.deaths),
            )
        )
    except _RECORDED_ERRORS as exc:
        _record_failure(
            archive=archive,
            manifest_abs=manifest_abs,
            tables_abs=(
                table_abs,
                ticks_abs,
                events_abs,
                lineups_abs,
                deaths_abs,
            ),
            existing=existing,
            result_id=result_id,
            params_hash=params_hash,
            inputs=inputs,
            versions=versions,
            reason=str(exc),
        )
        raise

    diagnostics = getattr(parser, "diagnostics", None)

    # Manifesti viimeisenä: keskeytynyt ajo näkyy seuraavalla kerralla
    # puuttuvana tuloksena eikä ajantasaisena.
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versions,
        status="ok",
        outputs=(
            str(table_rel),
            str(ticks_rel),
            str(events_rel),
            str(lineups_rel),
            str(deaths_rel),
        ),
    ).write(manifest_abs)

    stats = _stats(
        df,
        parsed.ticks,
        parsed.events,
        parsed.lineups,
        parsed.deaths,
        parsed.skipped_rounds,
    )
    # Ostoikkunan luvut lasketaan **valmiista taulusta**, jotta puukkokierros
    # ei ole niissä mukana; ks. _buy_window_stats.
    stats.update(_buy_window_stats(df, diagnostics))
    # Tuntemattomat tavaraluettelon nimet: ne eivät ole taulussa, koska ne
    # eivät aseista ketään -- ilman tätä riviä uusi ase näyttäisi täsmälleen
    # samalta kuin uusi veitsiskini. Avain asetetaan **jokaisessa tuoreessa
    # ajossa**, myös silloin kun portti ei raportoi niitä: puuttuva avain
    # tarkoittaa ohitettua ajoa, ja ``None`` porttia joka ei osaa kertoa.
    # Ilman eroa nämä kolme tilaa näyttäisivät tulosteessa samalta.
    stats["armed_unknown_items"] = (
        None
        if diagnostics is None
        else tuple(getattr(diagnostics, "unknown_inventory_items", ()) or ())
    )
    # Ottelun uudelleenaloitus ei tuota riviä yhteenkään tauluun, joten sen
    # määrää **ei voi laskea valmiista tuloksesta**. Kolme tilaa on pidettävä
    # erillään täsmälleen kuten yllä: avain puuttuu (ohitettu ajo), ``None``
    # (tuore ajo, portti ei kerro) ja luku (tuore ajo, portti kertoo). Ilman
    # eroa välimuistista ajettu demo väittäisi hiljaa "ei uudelleenaloitusta".
    stats["match_restarts"] = (
        None if diagnostics is None else getattr(diagnostics, "match_restarts", None)
    )
    # Vain tuoreesta ajosta: numeroimattomien kierrosten rivit eivät ole
    # taulussa, joten ohitetusta ajosta lukua ei voi lukea takaisin.
    stats["utility_unnumbered_rounds"] = parsed.unnumbered_utility
    # Sama sääntö kuin utilityllä, ja tässä se on erityisen tarpeen:
    # puukkokierroksella kuollaan oikeasti, joten pudotus on aina epätyhjä
    # liigademossa. Ilman lukua se näyttäisi siltä, ettei kuolemia ollut.
    stats["deaths_unnumbered_rounds"] = parsed.unnumbered_deaths
    if diagnostics is not None:
        stats["tick_rate"] = diagnostics.tick_rate
        stats["tick_rate_measured"] = diagnostics.tick_rate_measured
        # Näitä ei voi laskea valmiista taulusta: vajaa näytepiste, ohitettu
        # vahinkotapahtuma ja pudotettu kranaatti näkyvät vain siinä hetkessä,
        # kun demoa luetaan. Ohitetussa ajossa ne siis puuttuvat, ja se on
        # oikein.
        stats["partial_samples"] = getattr(diagnostics, "partial_samples", 0)
        # Kokoonpanotaulun oletus "yksi nimi ja yksi klaani per pelaaja" ei ole
        # luettavissa valmiista taulusta: se kirjoittaa moodin, joten
        # rikkoutunut oletus näyttää siellä ehjältä. Vain tuore ajo tietää.
        for name in ("lineup_name_conflicts", "lineup_clan_conflicts"):
            stats[name] = getattr(diagnostics, name, 0)
        stats["armed_unreadable_rows"] = getattr(
            diagnostics, "armed_unreadable_rows", 0
        )
        # Oma lukunsa, koska laskureiden luettavuusehdot eroavat: erotus
        # kertoo montako riviä kaatui pelkkään tavaraluetteloon, eikä sitä
        # voi lukea valmiista taulusta.
        stats["armored_unreadable_rows"] = getattr(
            diagnostics, "armored_unreadable_rows", 0
        )
        # Ostoikkuna: mistä hetkestä luvut on luettu, kuinka usein kuolema
        # katkaisi ikkunan ja **maksoiko katkaisu jotain**. Viimeinen on
        # tämän tarinan tärkein luku: se on ainoa merkki siitä, että
        # kompromissi puri, eikä sitä voi laskea valmiista taulusta.
        stats["unknown_side_events"] = getattr(
            diagnostics, "unknown_side_events", 0
        )
        stats["grenades_without_thrower"] = getattr(
            diagnostics, "grenades_without_thrower", 0
        )
        stats["grenades_outside_rounds"] = getattr(
            diagnostics, "grenades_outside_rounds", 0
        )
        for name in (
            "deaths_without_tick",
            "deaths_outside_rounds",
            "deaths_without_victim",
            "deaths_without_victim_side",
            "deaths_attacker_without_side",
            "grenades_unknown_side",
            "grenades_unknown_type",
            "grenades_fire_type_unresolved",
            "grenades_detonating_after_round",
            "grenade_ticks_without_players",
            "grenades_sharing_an_entity_id",
        ):
            stats[name] = getattr(diagnostics, name, 0)

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(table_rel, ticks_rel, events_rel, lineups_rel, deaths_rel),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def _write_tables(tables: tuple[tuple[Path, pl.DataFrame], ...]) -> None:
    """Kirjoita kaikki taulut yhtenä tapahtumana.

    Jokainen taulu menee ensin omaan väliaikaistiedostoonsa, ja vasta kun
    **kaikki** on kirjoitettu, ne siirretään kohteisiinsa. Peräkkäiset
    ``atomic_path``-lohkot eivät riitä: jos toinen kirjoitus kaatuu, ensimmäinen
    olisi jo paikallaan ja arkistoon jäisi kierrostaulu, joka kertoo 21
    kierrosta, sekä näytepistetaulu, joka tuntee niistä kaksi -- yhdistelmä,
    joka läpäisisi jokaisen skeematarkistuksen.

    Siirto itse ei ole yksi atominen operaatio (yksi ``os.replace`` taulua
    kohden), mutta niiden väliin jäävä ikkuna on mikrosekunteja eikä sisällä
    I/O:ta.
    """
    with ExitStack() as stack:
        pending_writes = [
            (stack.enter_context(atomic_path(target)), frame)
            for target, frame in tables
        ]
        for tmp, frame in pending_writes:
            frame.write_parquet(tmp)
    # ExitStack purkaa lohkot vasta tässä, ja jokainen atomic_path tekee oman
    # renamensa. Poikkeus missä tahansa kirjoituksessa siivoaa kaikki
    # väliaikaistiedostot eikä koske kohteisiin.


def _parse_tables(
    parser: DemoParser,
    settings: ParseSettings,
    demo_path: Path,
    map_demo_id: str,
) -> _ParsedTables:
    """Lue demo ja rakenna valmiit, tarkistetut taulut."""
    tables: DemoTables = parser.parse_demo(demo_path, settings.snapshot_seconds)
    _check_port_columns(
        tables.rounds, ROUNDS_ADAPTER_COLUMNS, "kierrostaulun", "ROUNDS_ADAPTER_COLUMNS"
    )
    _check_port_columns(
        tables.ticks, TICKS_ADAPTER_COLUMNS, "näytepistetaulun", "TICKS_ADAPTER_COLUMNS"
    )
    _check_port_columns(
        tables.events,
        EVENTS_ADAPTER_COLUMNS,
        "tapahtumataulun",
        "EVENTS_ADAPTER_COLUMNS",
    )
    _check_port_columns(
        tables.lineups,
        LINEUPS_ADAPTER_COLUMNS,
        "kokoonpanotaulun",
        "LINEUPS_ADAPTER_COLUMNS",
    )
    _check_port_columns(
        tables.deaths,
        DEATHS_ADAPTER_COLUMNS,
        "kuolemataulun",
        "DEATHS_ADAPTER_COLUMNS",
    )

    numbered = mark_played_rounds(tables.rounds)
    skipped_rounds = int(
        numbered.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    played = numbered.filter(pl.col("round_no").is_not_null())

    df = played.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    ).sort("round_no", "side")

    if df.is_empty():
        raise ParseError(
            f"Demosta {demo_path.name} ei löytynyt yhtään pelattua kierrosta "
            f"({skipped_rounds} kierrosrajaa oli warmupia tai "
            "puukkokierros).\n"
            "Tyhjää tulosta ei kirjoiteta -- muuten se jäisi manifestin "
            "perusteella pysyvästi ohitetuksi. Tarkista, että demo on koko "
            "ottelun tallenne."
        )

    validate(df, ROUNDS, TABLE)
    check_win_reasons(df)
    _check_two_rows_per_round(df)
    _check_player_counters(df)

    ticks = _number_ticks(tables.ticks, numbered, map_demo_id)
    validate(ticks, TICKS, TICKS_TABLE)

    if ticks.is_empty():
        raise ParseError(
            f"Demosta {demo_path.name} syntyi {df['round_no'].n_unique()} "
            "pelattua kierrosta mutta ei yhtään näytepistettä.\n"
            "Tyhjää asetelmataulua ei kirjoiteta ok-tuloksena: se jäisi "
            "manifestin perusteella pysyvästi ohitetuksi ja aggregointi "
            "raportoisi kartan ilman yhtään asetelmaa. Tarkista "
            "[parse].snapshot_seconds ja se, löytyykö kierroksilta "
            "freezetime-ankkuri."
        )

    # Tyhjää tapahtumataulua **ei** kohdella virheenä, toisin kuin kahta muuta:
    # demossa on aina pelattuja kierroksia ja asetelmia, mutta utility voi
    # aidosti puuttua (harjoitusottelu, pelkkiä pistoolikierroksia). Virhe
    # estäisi koko demon parsinnan tiedosta, joka on itsessään havainto.
    events, unnumbered = _number_events(tables.events, numbered, map_demo_id)
    validate(events, EVENTS, EVENTS_TABLE)
    _check_grenade_key(events)

    lineups = _build_lineups(tables.lineups, map_demo_id)
    validate(lineups, LINEUPS, LINEUPS_TABLE)
    _check_lineup_key(lineups, demo_path)

    # Eheysvartijat ajetaan **ennen numerointia**, eli adapterin koko
    # tuotokselle. Numerointi pudottaa lämmittelyn ja puukkokierroksen rivit,
    # ja juuri ne ovat kierroksia, joilta kirjaston puolinainen rivi
    # todennäköisimmin tulee -- numeroinnin jälkeen vartija katsoisi vain
    # sitä osaa aineistoa, jossa vikaa ei odoteta.
    _check_victim_is_whole(tables.deaths)
    _check_attacker_is_whole(tables.deaths)

    deaths, unnumbered_deaths = _number_deaths(
        tables.deaths, numbered, map_demo_id
    )
    validate(deaths, DEATHS, DEATHS_TABLE)

    if deaths.is_empty():
        raise ParseError(
            f"Demosta {demo_path.name} syntyi {df['round_no'].n_unique()} "
            "pelattua kierrosta mutta ei yhtään kuolemaa.\n"
            "Tyhjää kuolemataulua ei kirjoiteta ok-tuloksena: se jäisi "
            "manifestin perusteella pysyvästi ohitetuksi, ja raportti "
            "kertoisi kartan, jolla kukaan ei kuollut.\n"
            + _death_drop_reasons(parser, unnumbered_deaths)
        )

    return _ParsedTables(
        rounds=df,
        ticks=ticks,
        events=events,
        lineups=lineups,
        deaths=deaths,
        skipped_rounds=skipped_rounds,
        unnumbered_utility=unnumbered,
        unnumbered_deaths=unnumbered_deaths,
    )


def _death_drop_reasons(parser: DemoParser, unnumbered: int) -> str:
    """Miksi kuolemataulu jäi tyhjäksi -- luvut, jotka on jo laskettu.

    Adapteri erittelee jokaisen pudotussyyn omaan laskuriinsa, ja ne ovat
    kädessä juuri tässä. Ilman niitä virheilmoitus nimeäisi kaksi arvausta
    ("rikkinäinen portti tai uudelleennimetty tapahtuma") tilanteessa, jossa
    todellinen syy on luettavissa.
    """
    diagnostics = getattr(parser, "diagnostics", None)
    counts = [
        ("tick puuttui", "deaths_without_tick"),
        ("kierrosten ulkopuolella", "deaths_outside_rounds"),
        ("uhri puuttui", "deaths_without_victim"),
        ("uhrin puolta ei saatu", "deaths_without_victim_side"),
    ]
    named = [
        f"{label} {value}"
        for label, name in counts
        if diagnostics is not None and (value := getattr(diagnostics, name, 0))
    ]
    if unnumbered:
        named.append(f"numeroimattomilla kierroksilla {unnumbered}")
    if named:
        return (
            "Pudotetut kuolemat: " + ", ".join(named) + ".\n"
            "Jos jokainen luku on nolla, demoportti ei tuottanut yhtään "
            "kuolemaa; muussa tapauksessa syy on nimetty yllä."
        )
    return (
        "Yksikään pudotuslaskuri ei ole nollasta poikkeava, joten demoportti "
        "ei tuottanut yhtään kuolemaa. Tarkista, ettei "
        "player_death-tapahtumaa ole nimetty uudelleen demoparser2:ssa."
    )


def _check_victim_is_whole(deaths: pl.DataFrame) -> None:
    """Uhri on rivin identiteetti, eikä sitä saa puuttua.

    Kuolema ilman uhria ei ole kuolema. Tyhjä ``victim_id``,
    ``victim_lineup_key`` tai ``victim_side`` läpäisisi ``validate``in --
    jokainen niistä on nullable-sarake -- ja aggregointi laskisi rivin
    **ei kuolemaksi eikä tapoksi**: se katoaisi hiljaa, koska molemmat
    suodattimet vertaavat kokoonpanoon.

    Ampujalla on oma vartijansa (:func:`_check_attacker_is_whole`), ja se on
    löysempi tarkoituksella: ampujaton kuolema on aito havainto, uhriton ei
    ole mitään.

    Raises:
        SchemaError: Jos yhdeltäkin riviltä puuttuu uhrin tunniste, kokoonpano
            tai puoli.
    """
    if deaths.is_empty():
        return
    required = ("victim_id", "victim_lineup_key", "victim_side")
    broken = deaths.filter(
        pl.any_horizontal([pl.col(name).is_null() for name in required])
    )
    if broken.is_empty():
        return
    empty = ", ".join(
        f"{name}: {broken[name].null_count()}"
        for name in required
        if broken[name].null_count()
    )
    raise SchemaError(
        f"Kuolemataulussa on {broken.height} riviä, joilta puuttuu uhrin "
        f"tieto ({empty}).\n"
        "Uhri on rivin identiteetti: kuolema ilman uhria ei kuulu "
        "kummallekaan joukkueelle, joten se katoaisi aggregoinnissa hiljaa "
        "-- ei kuolemana eikä tappona.\n"
        "Ensimmäiset rivit: "
        f"{broken.select('round_raw', 't_s').head(3).to_dicts()}"
    )


def _check_attacker_is_whole(deaths: pl.DataFrame) -> None:
    """Ampujaton kuolema on **kokonaan** ampujaton.

    Putoaminen ja pommi tuottavat rivin, jolla ampujaa ei ole, ja se on aito
    havainto. Puolikas ampuja ei ole: rivi, jolla ``attacker_id`` on tyhjä
    mutta koordinaatit tai alue eivät, väittäisi paikkaa toimijalle, jota ei
    ole -- ja aggregointi laskisi sen alueen "tapoiksi", joita kukaan ei
    tehnyt. Skeema ei näe tätä, koska jokainen kenttä on erikseen kelvollinen.

    ``attacker_side`` ja ``attacker_lineup_key`` **eivät** ole tarkistuksessa
    mukana: ne ovat kierroksen puolikuvauksesta johdettuja eivätkä
    tapahtuman omia, ja ne voivat puuttua ampujalta, joka tunnetaan.
    ``attacker_area`` on mukana vain tässä suunnassa -- se saa puuttua
    yksinään, mutta ei olla olemassa ilman ampujaa.

    Raises:
        SchemaError: Jos yhdelläkin ampujattomalla rivillä on ampujan
            havaintoja.
    """
    if deaths.is_empty():
        return
    observations = ("attacker_x", "attacker_y", "attacker_z", "attacker_area")
    broken = deaths.filter(
        pl.col("attacker_id").is_null()
        & pl.any_horizontal(
            [pl.col(name).is_not_null() for name in observations]
        )
    )
    if broken.is_empty():
        return
    raise SchemaError(
        f"Kuolemataulussa on {broken.height} riviä, joilla ei ole ampujaa "
        "mutta on ampujan havaintoja "
        f"({', '.join(observations)}).\n"
        "Ampujaton kuolema (putoaminen, pommi) on aito tapaus, mutta silloin "
        "jokainen ampujan kenttä on tyhjä: paikka ilman toimijaa laskeutuisi "
        "raportissa tapoksi, jota kukaan ei tehnyt.\n"
        "Ensimmäiset rivit: "
        f"{broken.select('round_raw', 't_s', 'victim_id').head(3).to_dicts()}"
    )


def _number_deaths(
    deaths: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Liitä kuolemiin ``round_no`` ja pudota numeroimattomat kierrokset.

    Sama päätös ja sama liitos kuin näytepisteillä ja utilityllä. Tässä
    taulussa pudotus on **aina epätyhjä liigademossa**: puukkokierroksella
    kuollaan oikeasti, ja mitatussa aineistossa se tuottaa kymmenkunta
    ``player_death``-riviä. Juuri siksi niitä ei suodateta erikseen -- yksi
    puukkokierrossääntö kahdessa paikassa erkanisi muusta numeroinnista.

    Lajitteluavain on ``(round_no, t_s, victim_id)``: kaksi joukkuekaveria voi
    kuolla samalla tickillä, ja ilman uhrin tunnistetta niiden järjestys
    riippuisi liitoksen vakaudesta.

    ``nulls_last=True`` on sama sääntö kuin
    :func:`~pappascout.domain.aggregate._death_order`issa: **puuttuva aika ei
    ole nolla**. Ilman sitä ankkuriton kuolema johtaisi kierrostaan
    parquetissa mutta olisi viimeisenä aggregoinnissa, ja sama asia
    järjestyisi kahdella eri tavalla riippuen siitä kumpaa katsoo.

    Returns:
        ``(taulu, pudonneet kuolemat)``. Jälkimmäinen raportoidaan, koska
        hiljainen pudotus näyttäisi demolta, jossa kuolemia oli vähemmän.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        deaths.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in DEATHS if name != "map_demo_id"],
        )
        .sort("round_no", "t_s", "victim_id", nulls_last=True)
    )
    return joined, int(deaths.height - joined.height)


def _build_lineups(lineups: pl.DataFrame, map_demo_id: str) -> pl.DataFrame:
    """Liitä kokoonpanotauluun ``map_demo_id`` ja järjestä rivit.

    Kierrosnumerointia **ei tehdä**: kokoonpano ja nimi ovat kartan
    ominaisuuksia eivätkä kierroksen, joten puukkokierroksen pudottaminen
    veisi tästä taulusta pelaajan, joka pelasi kartan.
    """
    return lineups.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in LINEUPS if name != "map_demo_id"],
    ).sort("lineup_key", "player_id")


def _check_lineup_key(lineups: pl.DataFrame, demo_path: Path) -> None:
    """Varmista, että ``(lineup_key, player_id)`` yksilöi rivin -- ja on olemassa.

    Kaksi vikaa, jotka läpäisisivät skeeman mutta rikkoisivat raportin:

    * **Tyhjä taulu.** Kokoonpanot tunnistetaan jokaisesta demosta, joten
      tyhjä taulu tarkoittaa rikkinäistä porttia. Se jäisi manifestin
      perusteella pysyvästi ohitetuksi, ja jokainen raportti puhuisi
      tiivisteistä ilman että mikään kertoisi miksi.
    * **Kaksoisrivi.** ``aggregate`` liittää rosterin tällä avaimella; kahdesti
      esiintyvä pelaaja näkyisi rosterissa kahdesti.
    """
    if lineups.is_empty():
        raise SchemaError(
            f"Demosta {demo_path.name} ei syntynyt yhtään kokoonpanoriviä.\n"
            "Kokoonpanot tunnistetaan jokaisesta demosta, joten tyhjä taulu "
            "tarkoittaa rikkinäistä demoporttia. Tyhjää tulosta ei kirjoiteta: "
            "se jäisi manifestin perusteella pysyvästi ohitetuksi ja raportti "
            "puhuisi tiivisteistä kertomatta miksi."
        )
    key = lineups.select("lineup_key", "player_id")
    if key.height == key.unique().height:
        return
    duplicates = (
        lineups.group_by("lineup_key", "player_id")
        .len()
        .filter(pl.col("len") > 1)
        .sort("lineup_key", "player_id")
        .head(5)
    )
    raise SchemaError(
        "Kokoonpanotaulun avain (lineup_key, player_id) ei ole "
        f"yksikäsitteinen: {key.height - key.unique().height} riviä on "
        "kaksoiskappaleita.\n"
        f"Ensimmäiset toistuvat avaimet: {duplicates.to_dicts()}"
    )


def _number_ticks(
    ticks: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> pl.DataFrame:
    """Liitä näytepisteisiin ``round_no`` ja pudota numeroimattomat kierrokset.

    Adapteri näytteistää kaikki ankkuroidut kierrosrajat, koska se ei tunne
    numerointisääntöä -- sen omistaa
    :func:`~pappascout.domain.rounds.mark_played_rounds`. Warmupin ja
    puukkokierroksen rivit poistuvat siis vasta tässä, samalla päätöksellä kuin
    kierrostaulusta, jolloin taulut eivät voi olla eri mieltä siitä mikä
    kierros pelattiin.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        ticks.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in TICKS if name != "map_demo_id"],
        )
        .sort("round_no", "sample_t_s", "sample_kind", "side", "player_id")
    )
    return joined


def _number_events(
    events: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Liitä tapahtumiin ``round_no`` ja pudota numeroimattomat kierrokset.

    Sama päätös ja sama liitos kuin näytepisteillä (:func:`_number_ticks`):
    lämmittelyssä ja puukkokierroksella heitetty utility poistuu tässä, jolloin
    taulut eivät voi olla eri mieltä siitä mikä kierros pelattiin.

    Lajitteluavain on ``(round_no, grenade_no, event_kind, t_s)``.
    ``grenade_no`` on **ennen** ``event_kind``ia, jotta radan kaksi riviä
    pysyvät vierekkäin: pelin tunnisteella lajiteltuna kierrätetyn tunnisteen
    kaikki heitot tulisivat ennen sen kaikkia räjähdyksiä, ja pari hajoaisi
    taulun eri kohtiin. ``event_kind`` on ennen ``t_s``:ää, jotta heitto tulee
    aina ennen räjähdystään silloinkin, kun molemmilla on sama ``t_s``; se on
    Enum, joten järjestys on luettelon järjestys eikä aakkosjärjestys.
    ``grenade_no`` on yksikäsitteinen, joten avain määrää järjestyksen
    täysin -- pelin oma tunniste ei ole lajittelussa lainkaan, koska se ei
    erota kahta rataa toisistaan.

    Returns:
        ``(taulu, pudonneet heitot)``. Jälkimmäinen lasketaan, koska kolme
        muuta utilityn pudotussyytä raportoidaan nimenomaan siksi, ettei
        utility katoa hiljaa -- eikä tämä saa olla poikkeus.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        events.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in EVENTS if name != "map_demo_id"],
        )
        .sort("round_no", "grenade_no", "event_kind", "t_s")
    )
    before = int(events.filter(pl.col("event_kind") == THROWN).height)
    after = int(joined.filter(pl.col("event_kind") == THROWN).height)
    return joined, before - after


def _record_failure(
    *,
    archive: ArchivePaths,
    manifest_abs: Path,
    tables_abs: tuple[Path, ...],
    existing: Manifest | None,
    result_id: str,
    params_hash: str,
    inputs: list[ManifestInput],
    versions: dict[str, str],
    reason: str,
) -> None:
    """Kirjaa epäonnistuminen manifestiin -- mutta älä tuhoa ehjää tulosta.

    Jos arkistossa on jo kelvolliset taulut ja niiden ``ok``-manifesti, kaikki
    jätetään paikalleen. Muuten manifestiin kirjataan ``parse_failed``, joka
    sekä kertoo syyn että estää ohituksen seuraavalla ajolla.

    **Kaikkien** taulujen on oltava paikallaan: vanha ``rounds`` uuden
    ``ticks``-taulun kanssa olisi puolikas tulos, joka näyttäisi ehjältä.
    """
    intact_result = (
        existing is not None
        and existing.status == "ok"
        and existing.outputs_present(archive.root)
        and all(path.is_file() for path in tables_abs)
    )
    if intact_result:
        return
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versions,
        status="parse_failed",
        reason=reason,
        outputs=(),
    ).write(manifest_abs)

