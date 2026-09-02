"""``aggregate`` -- putken kolmas vaihe: luokitelluista kierroksista raportti.

Vaihe lukee arkistosta joukkueen luokitellut kierrokset
(``classified/<team_key>/<map_demo_id>.parquet``) sekä niiden näytepiste-,
tapahtuma-, kokoonpano- ja kuolemataulut
(``parsed/<map_demo_id>/{ticks,events,lineups,deaths}.parquet``) ja kirjoittaa
**yhden tiedoston**: ``aggregates/<team_key>/report.json``, joka on
:class:`~pappascout.domain.report.Report`-malli JSONina. Demoa ei lueta.

Se laskee kaiken, ``render`` ei laske mitään
--------------------------------------------
Vaihe **ei valitse mitä raportoidaan**. Se laskee jokaisen kierrostyypin, myös
täydet ostot ja jatkoajan, ja jättää valinnan Story 2.4:lle. Jos aggregointi
suodattaisi, esitysvalinnan muuttaminen vaatisi uudelleenlaskennan ja
``report.json`` lakkaisi olemasta täysi kuva siitä, mitä demoista tiedetään.

Vaihe ei myöskään tulkitse. Sanoja "fake" tai "rush" ei ole missään kentässä --
vain havaintoja ja lukumääriä. Tulkinnan tekee ihminen.

``team_key`` tässä storyssa
---------------------------
Joukkueindeksi (``index/teams.json``) syntyy vasta ``select``-vaiheessa
(Epic 3), joten ``--team`` on toistaiseksi ``classified/``-hakemiston nimi eli
kokoonpanotunniste, aivan kuten ``classify``-vaiheessa.

Kokoonpanotunniste on tiiviste kartalla pelanneista pelaajista, joten **yksi
vaihto tuottaa uuden tunnisteen**: MatureMayhem on neljässä demossa kahden eri
tunnisteen alla. Vaihe liittää ne yhteen säännöllä, joka on jo asetuksissa
(``[thresholds].team_identity_min_common``, AD-6): kokoonpanot ovat sama
joukkue, kun yhteisiä pelaajia on vähintään kolme. Ilman liittämistä raportti
näkisi kolme demoa neljästä eikä kertoisi menettäneensä yhtä. Liitetyt
tunnisteet kirjataan raporttiin (``team.lineup_keys``), joten päätös on
tarkistettavissa.

Joukkueen nimi on havainto
--------------------------
Nimi luetaan kokoonpanotaulun ``clan_name``-sarakkeesta, joka on demosta
havaittu arvo. Sitä ei johdeta tiedostonimestä, FACEIT-tunnisteesta eikä
mistään muusta lähteestä: ilman havaintoa ``display_name`` on ``team_key``
itse, ``display_name_source`` on ``team_key``, ja raportti sanoo puuttumisen
ääneen.

**Ristiriita ei katoa.** Jos liitetyt demot antavat joukkueelle eri nimen,
näytettäväksi valitaan useimmin havaittu ja loput päätyvät kenttään
``display_name_alternatives``. Ääni on demokohtainen eikä rivikohtainen, ja
tasatilanne ratkeaa aakkosjärjestyksessä, jotta ajo on toistettava
(:func:`~pappascout.domain.aggregate.team_identity`).

**Joukkueen avain ei muutu.** ``team_key`` on hakemistorakenne
(``classified/<team_key>/``), ja sen vaihtaminen nimeksi on Epic 3:n
``select``-vaiheen työtä. Tämä vaihe vaihtaa vain sen, mitä näytetään -- ja
tiedostonimen slugin, joka seuraa näytettävää nimeä.

Kartan nimi on havainto, päättely on varalähde
----------------------------------------------
Kartan nimi luetaan ``parsed/<map_demo_id>/match.parquet``-taulusta, johon
``parse`` kirjoittaa sen demon otsikosta (Story 2.11). Nimeä ei validoida
karttapoolia vasten: poolin ulkopuolinen kartta on aito havainto.

Vasta jos havaintoa ei ole -- ``map_name`` on ``null`` -- nimi päätellään
``map_demo_id``:stä karttapoolia vasten. ``map_name_source`` kertoo mistä nimi
tuli (``demo_header`` -> ``map_demo_id`` -> ``unknown``), eikä tuntematon kartta
sulaudu toisen kartan haaraan vaan jää omakseen tunnisteensa nimellä.

Kaksi demoa samalta kartalta on **yksi haara**: nimi on sama, kierrokset
summautuvat ja ``map_demo_ids`` luettelee demot. Juuri tämä ei toteudu ilman
otsikkoa, koska FACEIT-tunnisteessa (``1-79f71e00-...``) ei ole kartan nimeä.

Manifesti ja uudelleenajo
-------------------------
Syötteitä ovat kaikkien mukaan otettujen demojen ``classify``-manifestit, ja
niiden tunniste lasketaan funktiolla
:meth:`~pappascout.archive.manifest.Manifest.fingerprint` -- sama määritelmä,
jolla ``classify`` tunnistaa oman syötteensä. Parametrihash lasketaan vain
``[thresholds]``- ja ``[league]``-osioista (AD-3), joten kynnysten säätö ajaa
tämän vaiheen uudelleen mutta ei parsintaa.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl

from pappascout import __version__
from pappascout.archive.atomic_write import atomic_write_text
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    ArchivePaths,
    classified,
    classified_manifest,
    parsed_table,
    report_json,
    report_manifest,
    safe_component,
)
from pappascout.constants import SIDES
from pappascout.domain.aggregate import (
    LEAGUE_BUCKETS,
    SLUG_FALLBACK,
    build_report,
    lineups_of_same_team,
    roster_entries,
    slugify,
    team_identity,
)
from pappascout.domain.models import (
    AggregateSettings,
    LeagueSettings,
    ThresholdSettings,
)
from pappascout.domain.report import (
    REPORT_SCHEMA_VERSION,
    MissingDemo,
    Report,
    TeamReport,
)
from pappascout.domain.sampling import (
    TIME_SAMPLE,
    AreaObservations,
    normalize_area,
)
from pappascout.domain.schemas import (
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    ROUNDS,
    TICKS,
    Schema,
    validate,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TOOLS",
    "run",
    "team_keys",
    "resolve_team",
    "collect_team",
    "TeamSources",
]

STAGE = "aggregate"

#: Tyhjä: aggregointi on puhdasta laskentaa luetuista tauluista, eikä minkään
#: ulkopuolisen kirjaston versio muuta sen tulosta (manifest-moduulin sääntö).
#:
#: Luettelo on silti olemassa ja se luetaan :func:`_tools`illa, jotta uuden
#: riippuvuuden lisääminen on yhden rivin muutos eikä kahden kovakoodatun
#: ``{}``:n etsimistä -- sama kuvio kuin ``stages.parse``issa.
TOOLS: tuple[str, ...] = ()


def _tools() -> dict[str, str]:
    """Työkaluversiot manifestiin. Tyhjä, kunnes :data:`TOOLS` ei ole."""
    return tool_versions(*TOOLS)


@dataclass(frozen=True)
class TeamSources:
    """Joukkueen aineisto arkistossa: kokoonpanot, demot ja puuttuvat demot.

    Erotettu omaksi tyypikseen, jotta joukkueen kokoaminen on testattavissa
    ilman raportin rakentamista -- ja jotta sen tulos on luettavissa
    virheilmoituksessa silloin, kun demoja ei löytynyt yhtään.

    Attributes:
        team_key: Käyttäjän valitsema hakemistonimi; myös tuloksen hakemisto.
        lineup_keys: Samaksi joukkueeksi liitetyt kokoonpanotunnisteet.
        demos: ``(lineup_key, map_demo_id)`` jokaiselle mukaan otetulle demolle.
        roster: Havaitut pelaajat kaikista liitetyistä kokoonpanoista.
        missing: Demot, joiden dataa ei ollut. Eivät katoa hiljaa.
    """

    team_key: str
    lineup_keys: list[str]
    demos: list[tuple[str, str]]
    roster: list[str]
    missing: list[MissingDemo]


def run(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    archive: ArchivePaths,
    team: str | None,
    *,
    aggregate_settings: AggregateSettings,
    force: bool = False,
) -> StageResult:
    """Aggregoi yhden joukkueen luokitellut kierrokset raportiksi.

    Args:
        thresholds: ``[thresholds]``-osio; siitä luetaan
            ``small_sample_rounds`` ja ``team_identity_min_common``.
        league: ``[league]``-osio; siitä luetaan karttapooli kartan nimen
            päättelyä varten.
        aggregate_settings: ``[aggregate]``-osio, **avainsanaparametrina**.
            Avainsana siksi, että kolme pydantic-osiota peräkkäin menisi
            positionaalisesti vaihtaen läpi ilman että mikään huomauttaisi --
            sama syy kuin ``classify``in ``economy``-parametrissa.
        archive: Arkiston polut.
        team: Joukkueen tunniste tai sen yksikäsitteinen alkuosa. ``None``
            tuottaa suomenkielisen virheen, joka listaa arkiston joukkueet.
        force: Aggregoi vaikka manifesti täsmäisi.

    Returns:
        :class:`~pappascout.stages.StageResult`, jonka ``stats`` sisältää
        otannat, karttojen määrän ja puuttuvat demot.

    Raises:
        ~pappascout.errors.PappascoutError: Jos joukkuetta ei tunnisteta tai
            yhtään luokiteltua demoa ei löydy.
        ~pappascout.errors.AggregateError: Jos otanta ei täsmää jollakin
            tasolla.
        ~pappascout.errors.SchemaError: Jos jokin luettu taulu ei vastaa
            sopimusta.
    """
    started = time.perf_counter()
    team_key = resolve_team(archive, team)
    sources = collect_team(archive, team_key, thresholds)

    json_rel = report_json(team_key)
    manifest_rel = report_manifest(team_key)
    json_abs = archive.resolve(json_rel)
    manifest_abs = archive.resolve(manifest_rel)

    inputs = _inputs(archive, sources.demos)
    params_hash = _params_hash(thresholds, league, aggregate_settings)

    existing = Manifest.read_if_exists(manifest_abs)
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions=_tools(),
            root=archive.root,
        )
    ):
        ready = _read_report(json_abs)
        if ready is not None:
            return StageResult(
                stage=STAGE,
                unit=team_key,
                status="ok",
                skipped=True,
                outputs=tuple(PurePosixPath(o) for o in existing.outputs),
                manifest_path=manifest_rel,
                reason=(
                    "Tulos on ajan tasalla: manifesti täsmää eikä kierroksia "
                    "tarvitse aggregoida uudelleen."
                ),
                duration_s=time.perf_counter() - started,
                stats=_stats(ready, sources),
            )

    report = _aggregate(
        archive, sources, thresholds, league, aggregate_settings
    )

    atomic_write_text(json_abs, report.model_dump_json(indent=2) + "\n")
    Manifest.new(
        result_id=str(PurePosixPath("aggregates") / team_key),
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=_tools(),
        status="ok",
        outputs=(str(json_rel),),
    ).write(manifest_abs)

    return StageResult(
        stage=STAGE,
        unit=team_key,
        status="ok",
        skipped=False,
        outputs=(json_rel,),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=_stats(report, sources),
    )


# -- Joukkueen kokoaminen --------------------------------------------------------


def team_keys(archive: ArchivePaths) -> list[str]:
    """Arkiston luokitellut joukkueet eli ``classified/``-hakemistot."""
    root = archive.resolve(PurePosixPath("classified"))
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir() if d.is_dir() and _demo_ids(d)
    )


def _demo_ids(directory: Path) -> list[str]:
    """Hakemiston luokitellut demot tunnisteina."""
    return sorted(p.stem for p in directory.glob("*.parquet"))


def resolve_team(archive: ArchivePaths, team: str | None) -> str:
    """Tulkitse ``--team`` arkiston joukkuehakemistoksi.

    Hyväksyy sekä täyden tunnisteen että sen yksikäsitteisen alkuosan -- 16
    merkin tiiviste on epämukava kirjoittaa käsin.

    Raises:
        PappascoutError: Jos tunniste puuttuu, ei täsmää tai täsmää useampaan.
            Viesti listaa aina arkiston joukkueet, joten seuraava komento on
            suoraan kopioitavissa.
    """
    available = team_keys(archive)
    if not available:
        raise PappascoutError(
            "Arkistossa ei ole yhtään luokiteltua joukkuetta, joten "
            "aggregoitavaa ei ole.\n"
            "Aja ensin: uv run pappascout classify <map_demo_id> --team "
            "<tunniste>"
        )
    if team is None:
        raise PappascoutError(
            "Kerro --team-valinnalla, minkä joukkueen kierrokset "
            f"aggregoidaan.\n{_team_listing(available)}"
        )

    query = team.strip().lower()
    matches = [k for k in available if k.lower() == query]
    if not matches:
        matches = [k for k in available if k.lower().startswith(query)]
    if len(matches) == 1:
        return safe_component(matches[0], "team_key")

    problem = (
        f"Joukkuetunniste {team!r} täsmää useampaan kuin yhteen joukkueeseen."
        if matches
        else f"Joukkuetunniste {team!r} ei täsmää yhteenkään joukkueeseen."
    )
    raise PappascoutError(f"{problem}\n{_team_listing(available)}")


def _team_listing(available: Sequence[str]) -> str:
    rows = "\n".join(f"    {key}" for key in available)
    return (
        "Arkiston luokitellut joukkueet ovat:\n"
        + rows
        + "\nAnna tunniste kokonaan tai sen alkuosa, esimerkiksi:\n"
        + f"    --team {available[0][:8]}"
    )


def collect_team(
    archive: ArchivePaths, team_key: str, thresholds: ThresholdSettings
) -> TeamSources:
    """Kokoa joukkueen kokoonpanot, demot ja rosteri arkistosta.

    Kokoonpanojen jäsenet luetaan **yhdestä demosta per kokoonpano**:
    tunniste on tiiviste kartalla pelanneista pelaajista, joten sama tunniste
    tarkoittaa aina samaa joukkoa eikä useampaa demoa tarvitse lukea.

    **Haun laajuus ja sen raja.** Joukkueidentiteetti on vertailu *muihin*
    kokoonpanoihin, joten kaikkien arkiston kokoonpanojen jäsenet on
    tunnettava -- yhden joukkueen omat demot eivät riitä. Kustannus on siksi
    yksi luku per ``classified/``-hakemisto, ei per demo, ja luku on kaksi
    saraketta (``lineup_key``, ``player_id``) näytepistetaulusta. Neljällä
    demolla se on millisekunteja; sadalla joukkueella se on sata pientä
    lukua. Jos arkisto joskus kasvaa niin suureksi että tämä maksaa, oikea
    ratkaisu on Epic 3:n joukkueindeksi (``index/teams.json``), joka poistaa
    koko päättelyn -- ei tämän silmukan optimointi.

    Raises:
        PappascoutError: Jos joukkueella ei ole yhtään luettavissa olevaa
            demoa.
    """
    root = archive.resolve(PurePosixPath("classified"))
    demos_by_lineup = (
        {
            d.name: _demo_ids(d)
            for d in sorted(root.iterdir())
            if d.is_dir() and _demo_ids(d)
        }
        if root.is_dir()
        else {}
    )

    members: dict[str, set[str]] = {}
    missing: list[MissingDemo] = []
    for lineup, lineup_demos in demos_by_lineup.items():
        for demo in lineup_demos:
            found = _lineup_members(archive, demo)
            if found is None:
                continue
            members.update({k: v for k, v in found.items() if k not in members})
            if lineup in members:
                break

    known = {k: v for k, v in members.items() if k in demos_by_lineup}

    # Kokoonpano, jonka yhdeltäkään demolta ei saatu kokoonpanotaulua, ei ole
    # vertailtavissa muihin -- eikä siis liitettävissä joukkueeseen. Sen demot
    # eivät saa silti kadota jäljettömiin: ne kirjataan puuttuviksi syyn
    # kanssa, jotta lukija näkee että otannasta puuttuu jotain, vaikka
    # aggregointi ei voi tietää kenelle se kuului.
    #
    # **Rivi per demo, ei per kokoonpano.** Yksi demo sisältää molemmat
    # joukkueet, joten sama tiedosto on kahden hakemiston alla ja tuottaisi
    # kaksi riviä samasta puutteesta. Mitattu ensimmäisestä ajosta: neljä
    # riviä kahdesta demosta. Se lukee kuin otannasta puuttuisi neljä ottelua.
    for lineup, lineup_demos in demos_by_lineup.items():
        if lineup in known:
            continue
        for demo in lineup_demos:
            missing.append(
                MissingDemo(
                    match=demo,
                    # Syy sanoo mitä oikeasti tiedetään: lukukelvoton
                    # kokoonpanotaulu ei kerro kenen demo tämä on, joten sitä
                    # ei voi väittää *tämän* joukkueen menetetyksi otteluksi.
                    reason=(
                        "Kokoonpanotaulua (lineups.parquet) ei saatu luettua, "
                        "joten ei tiedetä kuuluuko demo tälle joukkueelle. "
                        f"Aja parsinta uudelleen: uv run pappascout parse {demo}"
                    ),
                )
            )

    if team_key not in known:
        raise PappascoutError(
            f"Joukkueen {team_key} kokoonpanoa ei saatu luettua: yhdenkään sen "
            "demon kokoonpanotaulua (lineups.parquet) ei löytynyt arkistosta.\n"
            "Aja parsinta uudelleen: uv run pappascout parse <map_demo_id>"
        )

    lineup_keys = lineups_of_same_team(
        team_key, known, thresholds.team_identity_min_common
    )

    demos: list[tuple[str, str]] = []
    roster: set[str] = set()
    for lineup in lineup_keys:
        roster.update(known[lineup])
        for demo in demos_by_lineup[lineup]:
            reason = _demo_unusable(archive, lineup, demo)
            if reason is None:
                demos.append((lineup, demo))
            else:
                missing.append(MissingDemo(match=demo, reason=reason))

    # Mukaan päässyt demo ei ole puuttuva, vaikka se olisi myös jonkin
    # lukukelvottoman kokoonpanon alla: sama tiedosto on aina kahden joukkueen
    # hakemistossa, ja vastustajan lukukelvoton kokoonpano ei vie meiltä
    # ottelua jonka juuri luimme.
    included = {demo for _, demo in demos}
    missing = _unique_by_match(m for m in missing if m.match not in included)

    if not demos:
        raise PappascoutError(
            f"Joukkueella {team_key} ei ole yhtäkään demoa, jonka sekä "
            "luokittelu että parsinta olisivat arkistossa.\n"
            + (
                "Puuttuvat:\n"
                + "\n".join(f"    {m.match}: {m.reason}" for m in missing)
                if missing
                else ""
            )
        )
    return TeamSources(team_key, lineup_keys, demos, sorted(roster), missing)


def _unique_by_match(entries: Iterable[MissingDemo]) -> list[MissingDemo]:
    """Yksi rivi per demo, ensimmäinen syy voittaa.

    Puuttuvat demot luetellaan **otteluina**, koska niin lukija ne laskee.
    Sama tiedosto on kahden kokoonpanon alla (molemmat joukkueet), ja kahden
    kokoonpanon alta löytyvä sama puute näyttäisi luettelossa kahdelta eri
    puuttuvalta ottelulta.
    """
    seen: dict[str, MissingDemo] = {}
    for entry in entries:
        seen.setdefault(entry.match, entry)
    return list(seen.values())


def _lineup_members(
    archive: ArchivePaths, map_demo_id: str
) -> dict[str, set[str]] | None:
    """Demon kokoonpanot pelaajineen, tai ``None`` jos taulua ei ole.

    Lähde on ``lineups.parquet`` eikä ``ticks.parquet`` (Story 2.6). Kaksi
    syytä: kokoonpanotaulu on kymmeniä rivejä siinä missä näytepistetaulu on
    kymmeniä tuhansia, ja sen pelaajajoukko on **täsmälleen se**, josta
    ``lineup_key`` on laskettu -- näytepistetaulusta puuttuisi pelaaja, joka
    ei ehtinyt yhdellekään näytepisteelle.
    """
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    if not path.is_file():
        return None
    try:
        df = pl.read_parquet(path, columns=["lineup_key", "player_id"])
    except (OSError, pl.exceptions.PolarsError):
        return None
    members: dict[str, set[str]] = {}
    for row in df.unique().iter_rows(named=True):
        members.setdefault(str(row["lineup_key"]), set()).add(str(row["player_id"]))
    return members


def _demo_unusable(
    archive: ArchivePaths, lineup: str, map_demo_id: str
) -> str | None:
    """Syy, miksi demoa ei voi ottaa mukaan -- tai ``None`` jos voi.

    Puuttuva parsinta ei kaada ajoa: demo menee ``missing_demos``-listaan syyn
    kanssa, ja raportti kertoo sen. Yksittäinen puuttuva demo ei saa viedä
    koko otantaa -- se veisi mukanaan kolme muuta, jotka ovat kunnossa.
    """
    # Kierrostaulu on mukana Story 2.8:sta lähtien: panssarilaskuri on siellä
    # eikä luokitellussa taulussa, koska se on havainto eikä luokittelun
    # päätöksen syöte. Puuttuva taulu on siis sama puute kuin muutkin --
    # ilman sitä raportti näyttäisi kierrostyypiltä, jolla kukaan ei ostanut
    # kevlaria.
    # Ottelutaulu on mukana Story 2.11:sta lähtien: kartan nimi on siellä.
    # Ilman sitä demo saisi haaransa päättelystä, eli FACEIT-demo jäisi
    # omaksi haarakseen tunnisteensa nimellä -- ja monidemo-otanta hajoaisi
    # hiljaa juuri sillä demolla, jonka taulu puuttuu.
    for table in ("rounds", "ticks", "events", "lineups", "deaths", "match"):
        if not archive.resolve(parsed_table(map_demo_id, table)).is_file():
            return (
                f"Parsittua taulua {table}.parquet ei ole arkistossa. "
                f"Aja: uv run pappascout parse {map_demo_id}"
            )
    manifest = Manifest.read_if_exists(
        archive.resolve(classified_manifest(lineup, map_demo_id))
    )
    if manifest is None:
        return (
            "Luokittelun manifestia ei ole, joten syötettä ei voi tunnistaa. "
            f"Aja: uv run pappascout classify {map_demo_id} --team {lineup}"
        )
    if manifest.status != "ok":
        return (
            f"Luokittelu on merkitty tilaan {manifest.status!r}: "
            f"{manifest.reason or 'syytä ei kirjattu'}"
        )
    return None


# -- Aggregointi -----------------------------------------------------------------


def _aggregate(
    archive: ArchivePaths,
    sources: TeamSources,
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    aggregate_settings: AggregateSettings,
) -> Report:
    """Lue taulut ja rakenna raportti."""
    classified_frames: list[pl.DataFrame] = []
    tick_frames: list[pl.DataFrame] = []
    event_frames: list[pl.DataFrame] = []
    lineup_frames: list[pl.DataFrame] = []
    death_frames: list[pl.DataFrame] = []
    round_frames: list[pl.DataFrame] = []
    # Kartan nimi demoittain: havainto tai ``None``.
    #
    # AVAIN ON **LUETTU DEMO**, EI TAULUN OMA SARAKE. ``_read_parsed``
    # validoi skeeman muttei sitä, että ``map_demo_id``-sarakkeen arvo vastaa
    # sitä demoa, jonka hakemistosta taulu luettiin. Vanhentunut tai väärään
    # hakemistoon joutunut ``match.parquet`` kirjaisi nimensä väärälle demolle
    # ja oikea demo palaisi hiljaa päättelyyn; kaksi samaa tunnistetta
    # pudottaisi toisen kokonaan. Silmukan ``demo`` on se, jonka polusta taulu
    # luettiin, joten avaimena se poistaa koko epäonnistumisluokan -- myös
    # tyhjän ja ``null``-tunnisteen tapauksen -- ilman uusia tarkistuksia.
    #
    # Taulua **ei suodateta kokoonpanoilla** kuten muita: siinä ei ole
    # ``lineup_key``-saraketta, koska kartta on ottelun ominaisuus eikä
    # kummankaan joukkueen.
    #
    # Rivi per demo on ``parse``-vaiheen valvoma sopimus, joten kartta ei voi
    # saada kahta arvoa samalle demolle.
    map_names: dict[str, str | None] = {}
    # Alueiden puoliorientaatio demoittain (Story 2.5): alue -> montako sen
    # elossa-havainnoista on T-puolelta ja montako niitä on kaikkiaan.
    #
    # LASKETAAN TÄSSÄ SILMUKASSA, ENNEN KOKOONPANOSUODATUSTA. Suodatus
    # tapahtuu vasta silmukan jälkeen (``ticks.filter(...)``), ja se on koko
    # syy sille, että orientaatio lasketaan täällä eikä domainissa: sääntö
    # tarvitsee **molempien joukkueiden** rivit. Subjektin omilla riveillä
    # laskettuna jokainen tosi positiivinen katoaa -- kun subjekti etenee
    # alueelle CT:nä, hänen omat CT-havaintonsa laskevat sen alueen
    # T-osuutta, eli poikkeama syö oman havaitsemisensa. Mitattuna kolme
    # aluetta putoaa kynnyksen alle (0,88 -> 0,79, 0,85 -> 0,75,
    # 0,84 -> 0,75), ja ne ovat täsmälleen ne kolme, jotka tuottivat kaikki
    # oikeat osumat.
    #
    # Avain on **luettu demo** samasta syystä kuin ``map_names``issa, ja
    # orientaatio on demokohtainen eikä karttakohtainen: karttuva lähde
    # antaisi samalle demolle eri tuloksen sen mukaan, mitä muita demoja
    # arkistossa sattuu olemaan (Story 2.9:n peruste).
    area_orientation: dict[str, dict[str | None, AreaObservations]] = {}

    for lineup, demo in sources.demos:
        classified_frames.append(_read_classified(archive, lineup, demo))
        demo_ticks = _read_parsed(archive, demo, "ticks", TICKS)
        tick_frames.append(demo_ticks)
        area_orientation[demo] = _area_orientation(demo_ticks)
        event_frames.append(_read_parsed(archive, demo, "events", EVENTS))
        lineup_frames.append(_read_parsed(archive, demo, "lineups", LINEUPS))
        death_frames.append(_read_parsed(archive, demo, "deaths", DEATHS))
        round_frames.append(_read_parsed(archive, demo, "rounds", ROUNDS))
        map_names[demo] = _read_map_name(archive, demo)

    lineups = set(sources.lineup_keys)
    # Kierrostaulussa on kaksi riviä per kierros, yksi kummallekin
    # joukkueelle. Oman rivin valitsee jo kolmiosainen avain (demo, kierros,
    # puoli), joten suodatus on **puolustus eikä ainoa este**: se pitää
    # vastustajan rivit pois hakukartasta, jolloin avainten törmäystarkistus
    # (``armored_by_round``) valvoo vain omia rivejä ja puoliaikojen
    # puolenvaihto ei voi tuoda kahta ehdokasta samalle avaimelle.
    rounds = pl.concat(round_frames).filter(pl.col("lineup_key").is_in(lineups))
    if rounds.is_empty():
        raise PappascoutError(
            f"Joukkueen {sources.team_key} demoista ei löytynyt yhtään "
            "kierrosriviä sen omilla kokoonpanotunnisteilla.\n"
            "``parse`` kieltäytyy kirjoittamasta tyhjää kierrostaulua, joten "
            "tyhjä tulos tarkoittaa että kokoonpanosuodatin ei osunut: "
            "kierrostaulut on kirjoitettu eri kokoonpanotunnisteilla kuin "
            "mitä tälle joukkueelle on liitetty. Aja parsinta uudelleen: "
            "uv run pappascout parse <map_demo_id> --pakota\n"
            "Ilman tätä tarkistusta jokainen kierrostyyppi raportoisi "
            "panssarijakaumakseen pelkän 'havainto puuttuu' -- eli "
            "havaintona sen, ettei havaintoa ole."
        )
    ticks = pl.concat(tick_frames).filter(pl.col("lineup_key").is_in(lineups))
    events = pl.concat(event_frames).filter(pl.col("lineup_key").is_in(lineups))
    # Kuolemataulussa suodatus on **kahdesta sarakkeesta**: rivi kuuluu
    # joukkueelle, jos joko uhri tai ampuja on sen kokoonpanossa. Pelkkä
    # ``victim_lineup_key`` pudottaisi omat tapot ja pelkkä
    # ``attacker_lineup_key`` omat kuolemat. Vastustajien keskinäinen kuolema
    # putoaa, koska kumpikaan ehto ei täyty. Rivien jako kuolemiksi ja
    # tapoiksi tehdään ``domain.aggregate.deaths_for``issa, joka näkee
    # molemmat sarakkeet.
    #
    # ``fill_null(False)`` on **välttämätön eikä koriste**: ampujaton kuolema
    # (putoaminen, pommi) jättää ``attacker_lineup_key``in tyhjäksi, ja
    # Polarsissa ``is_in`` antaa nullille nullin. Ilman täyttöä ehto nojaisi
    # siihen, että ``true | null`` on tosi -- oikein tänään, mutta hiljainen
    # riippuvuus kolmiarvoisen logiikan yksityiskohdasta. Juuri se rivi on
    # oma kuolema, jonka katoaminen näkyisi raportissa vain puuttuvana.
    deaths = pl.concat(death_frames).filter(
        pl.col("victim_lineup_key").is_in(lineups).fill_null(False)
        | pl.col("attacker_lineup_key").is_in(lineups).fill_null(False)
    )
    if deaths.is_empty():
        raise PappascoutError(
            f"Joukkueen {sources.team_key} demoista ei löytynyt yhtään "
            "kuolemaa, jossa se olisi uhrina tai ampujana.\n"
            "``parse`` kieltäytyy kirjoittamasta tyhjää kuolemataulua, joten "
            "tyhjä tulos tarkoittaa että kokoonpanosuodatin ei osunut: "
            "kuolemataulut on kirjoitettu eri kokoonpanotunnisteilla kuin "
            "mitä tälle joukkueelle on liitetty. Aja parsinta uudelleen: "
            "uv run pappascout parse <map_demo_id> --pakota\n"
            "Ilman tätä tarkistusta jokainen kierrostyyppi raportoisi "
            "'ei omia kuolemia' -- eli havaintona sen, ettei havaintoa ole."
        )
    # Vain tämän joukkueen kokoonpanot: sama demo sisältää molempien
    # joukkueiden rivit, ja suodattamatta vastustajan klaaninimi äänestäisi
    # otsikosta.
    lineup_rows = (
        pl.concat(lineup_frames)
        .filter(pl.col("lineup_key").is_in(lineups))
        .to_dicts()
    )
    identity = team_identity(lineup_rows)

    team = TeamReport(
        key=sources.team_key,
        # Tiedostonimen slug seuraa nimeä silloin kun nimi on havainto.
        #
        # Varapolku on **tunniste eikä jaettu vakio**, ja ketju on kolmiosainen
        # tarkoituksella: kyrillinen tai CJK-klaaninimi on olemassa ja
        # havaittu, mutta siitä ei jää yhtään ASCII-merkkiä. ``team_slug``in
        # oma varapolku antaisi silloin jokaiselle tällaiselle joukkueelle
        # saman tiedostonimen ``<aikaleima>-joukkue.md``, eli nimi katoaisi ja
        # tiedostot törmäisivät toisiinsa. Tunnisteesta johdettu slug on
        # yksikäsitteinen. Sama sääntö on kirjoitettu ``TeamReport``in
        # sopimukseen, jotta levyltä luettu raportti ei voi olla eri mieltä.
        slug=(
            slugify(identity.display_name or "")
            or slugify(sources.team_key)
            or SLUG_FALLBACK
        ),
        # Nimi on havainto demosta (``LINEUPS.clan_name``), ei johdos. Ilman
        # havaintoa nimi on tunniste ja lähde sanoo sen ääneen; raportti ei
        # keksi korviketta tiedostonimestä tai FACEIT-tunnisteesta.
        display_name=identity.display_name or sources.team_key,
        display_name_source=(
            "clan_name" if identity.display_name else "team_key"
        ),
        display_name_alternatives=identity.alternatives,
        lineup_keys=sources.lineup_keys,
        # Pelaajajoukko tulee ``sources.roster``ista eikä nimikartasta:
        # rosterirvi kirjoitetaan silloinkin, kun nimeä ei saatu, koska
        # SteamID on ainoa jäljitettävä arvo.
        roster=roster_entries(sources.roster, identity.names),
        roster_source="lineups",
    )
    return build_report(
        classified=pl.concat(classified_frames),
        ticks=ticks,
        events=events,
        deaths=deaths,
        rounds=rounds,
        team=team,
        thresholds=thresholds,
        aggregate=aggregate_settings,
        map_pool=league.map_pool,
        map_names=map_names,
        area_orientation=area_orientation,
        generated_at=datetime.now(UTC),
        tool_versions={"pappascout": __version__},
        missing_demos=sources.missing,
    )


def _read_classified(
    archive: ArchivePaths, lineup: str, map_demo_id: str
) -> pl.DataFrame:
    path = archive.resolve(classified(lineup, map_demo_id))
    df = _read_parquet(path, map_demo_id)
    validate(
        df,
        CLASSIFIED,
        "classified",
        advice=(
            "Taulu on luokiteltu ohjelman vanhemmalla versiolla. Aja "
            f"luokittelu uudelleen: uv run pappascout classify {map_demo_id} "
            f"--team {lineup} --pakota"
        ),
    )
    return _in_schema_order(df, CLASSIFIED)


def _area_orientation(
    ticks: pl.DataFrame,
) -> dict[str | None, AreaObservations]:
    """Alue -> puoliorientaation havainnot yhdestä demosta.

    Lähde on **suodattamaton** näytepistetaulu eli molempien joukkueiden
    rivit; ks. kutsupaikan kommentti siitä, miksi se on mitattu ehto eikä
    mieltymys.

    Neljä rajausta, ja jokainen niistä on määritelmä eikä siivous:

    * ``sample_kind == "time"`` -- ensikontaktin hetki on eri joka
      kierroksella, joten sen rivit painottaisivat orientaatiota niiden
      kierrosten mukaan, joilla satuttiin ampumaan aikaisin.
    * ``is_alive`` -- kuollut pelaaja ei ole alueella. Sama sääntö kuin
      pelaajamäärissä (``positions_for``), joten orientaatio ja asetelma
      lasketaan samasta joukosta.
    * ``side`` on ``T`` tai ``CT`` -- **tämä on jakajan rajaus, ei
      osoittajan.** Ilman sitä ``pl.len()`` laskisi mukaan rivin, jonka
      puolta ei tiedetä, mutta ``side == "T"`` ei voisi laskea sitä T:ksi:
      tuntematon puoli painaisi T-osuutta alaspäin ja voisi pudottaa T:n
      alueen kynnyksen alle. Juuri se osuus on molempien sääntöjen perusta.
    * ``area`` ei tyhjä -- nimetön alue ei voi olla kumman tahansa puolen
      aluetta, eikä poikkeamaa "tuntemattomalla alueella" voisi kertoa.

    ``fill_null(False)`` elossaolossa on **välttämätön eikä koriste**:
    Polarsissa ``null`` totuusarvona ei ole epätosi vaan null, ja se veisi
    rivin mukanaan suodattimen läpi tai pois sen mukaan, miten ehdot
    yhdistetään. Havainnon puuttuminen ei ole havainto siitä, että pelaaja
    olisi elossa.

    Aluenimi normalisoidaan **samalla funktiolla** kuin läsnäolorivi
    (:func:`~pappascout.domain.sampling.normalize_area`). Ilman sitä
    ``" Lobby "`` olisi orientaatiossa eri alue kuin läsnäolossa ja säännöt
    vaikenisivat sillä alueella; ``""`` puolestaan selviäisi T-alueiden
    joukkoon ja tuottaisi poikkeaman nimettömälle alueelle.

    Returns:
        Alue -> :class:`~pappascout.domain.sampling.AreaObservations`. Tyhjä
        kartta on kelvollinen tulos: demo, jonka näytepisteissä ei ole
        yhtäkään nimettyä aluetta, ei anna orientaatiota millekään alueelle
        -- eikä sitä silloin arvata. Tulos päätyy raportin kattavuuslukuun
        (``anomaly_scan.demos_without_orientation``), joten tyhjä ei katoa
        hiljaa.
    """
    grouped = (
        ticks.filter(
            (pl.col("sample_kind") == TIME_SAMPLE)
            & pl.col("is_alive").fill_null(False)
            & pl.col("side").is_in(list(SIDES))
            & pl.col("area").is_not_null()
        )
        .group_by("area")
        .agg(
            pl.len().alias("observations"),
            # Puoli on rivin oma havainto; "T" on TICKS-taulun arvojoukon
            # jäsen (``constants.SIDES``) eikä johdos.
            (pl.col("side") == "T").sum().alias("t"),
        )
    )
    found: dict[str | None, AreaObservations] = {}
    for row in grouped.iter_rows(named=True):
        area = normalize_area(row["area"])
        if area is None:
            continue
        observed = AreaObservations(
            t=int(row["t"]), total=int(row["observations"])
        )
        previous = found.get(area)
        # Kaksi kirjoitusasua samasta alueesta (``"Lobby"`` ja ``" Lobby "``)
        # ovat yksi alue, joten niiden havainnot LASKETAAN YHTEEN. Vaihtoehto
        # olisi pudottaa toinen, mikä laskisi orientaation osuuden osajoukosta
        # ja voisi kääntää kynnyksen.
        found[area] = (
            observed
            if previous is None
            else AreaObservations(
                t=previous.t + observed.t,
                total=previous.total + observed.total,
            )
        )
    return found


def _read_map_name(archive: ArchivePaths, map_demo_id: str) -> str | None:
    """Kartan nimi demon ``match.parquet``-taulusta, tai ``None``.

    Taulussa on ``parse``-vaiheen valvoman sopimuksen mukaan täsmälleen yksi
    rivi. Tarkistus toistetaan tässä siksi, että luettu tiedosto voi olla
    ohjelman vanhemman version kirjoittama: sopimusta valvoo se vaihe, joka
    kirjoittaa, eikä lukija saa nojata siihen että kirjoittaja oli tämä versio.

    Palautettava arvo on **nimi tai sen puuttuminen**, ei taulu: kutsuja ei
    tarvitse kehystä mihinkään, ja rivin nostaminen tässä pitää sopimuksen
    tarkistuksen yhdessä paikassa.
    """
    df = _read_parsed(archive, map_demo_id, "match", MATCH)
    if df.height != 1:
        raise PappascoutError(
            f"Demon {map_demo_id} ottelutaulussa on {df.height} riviä, "
            "vaikka niitä on oltava täsmälleen yksi.\n"
            "Taulu kuvaa yhtä ottelua. Nolla riviä tarkoittaisi, ettei kartan "
            "nimeä havaittu -- mutta se on eri asia kuin havainto ``null``, "
            "ja kahdesta rivistä nimi valikoituisi rivijärjestyksen mukaan.\n"
            f"Aja parsinta uudelleen: uv run pappascout parse {map_demo_id} "
            "--pakota"
        )
    name = df["map_name"][0]
    return None if name is None else str(name)


def _read_parsed(
    archive: ArchivePaths, map_demo_id: str, table: str, schema: Schema
) -> pl.DataFrame:
    path = archive.resolve(parsed_table(map_demo_id, table))
    df = _read_parquet(path, map_demo_id)
    validate(
        df,
        schema,
        table,
        advice=(
            "Taulu on parsittu ohjelman vanhemmalla versiolla. Aja parsinta "
            f"uudelleen: uv run pappascout parse {map_demo_id} --pakota"
        ),
    )
    return _in_schema_order(df, schema)


def _in_schema_order(df: pl.DataFrame, schema: Schema) -> pl.DataFrame:
    """Järjestä sarakkeet sopimuksen mukaiseen järjestykseen.

    :func:`~pappascout.domain.schemas.validate` hyväksyy minkä tahansa
    sarakejärjestyksen -- sopimus on nimistä ja tyypeistä. ``pl.concat`` ei:
    kaksi kehystä, joissa on samat sarakkeet eri järjestyksessä, kaatuu
    ``ShapeError``iin, joka on englanninkielinen Polars-poikkeus eikä kerro
    käyttäjälle mitään. Kahdella eri versiolla kirjoitettu arkisto on juuri
    se tilanne, jossa niin voi käydä. Järjestäminen poistaa koko
    epäonnistumistavan sen sijaan että se käännettäisiin suomeksi.
    """
    return df.select(list(schema))


def _read_parquet(path: Path, map_demo_id: str) -> pl.DataFrame:
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise PappascoutError(
            f"Taulua {path} ei voitu lukea: {exc}\n"
            f"Aja vaihe uudelleen demolle {map_demo_id}."
        ) from exc


def _read_report(path: Path) -> Report | None:
    """Lue aiemmin kirjoitettu raportti, tai ``None`` jos sitä ei voi käyttää.

    Ohitus ei saa nojata pelkkään manifestiin, ja tarkistuksia on kaksi.

    **Skeemaversio.** Vanhalla versiolla kirjoitettu ``report.json`` voi
    validoitua nykyistä mallia vasten kenttä kentältä ja tarkoittaa silti eri
    asiaa. Vertailu on sama kuin :meth:`Manifest.read`issa: eri versio =
    tuntematon tiedosto, ja vaihe ajetaan uudelleen. Ilman tätä ohitus
    palauttaisi vanhan tuloksen luvut ja väittäisi niitä tämän ajon
    tulokseksi.

    **Poikkeuslaji.** Otannan summatarkistukset nostavat
    :class:`~pappascout.errors.AggregateError`in, joka periytyy
    ``PappascoutError``ista **eikä ValueErrorista**. Pelkkä ``ValueError``
    jättäisi sen kiinni ottamatta, jolloin vanha epäkelpo raportti kaataisi
    ohitushaaran joka ajolla sen sijaan että vaihe kirjoittaisi tilalle uuden.
    Kaikki kolme lajia -- luku-, validointi- ja summavirhe -- tarkoittavat
    tässä samaa: tulosta ei voi käyttää, joten se lasketaan uudelleen.
    """
    if not path.is_file():
        return None
    try:
        report = Report.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, PappascoutError):
        return None
    if report.schema_version != REPORT_SCHEMA_VERSION:
        return None
    return report


# -- Manifesti -------------------------------------------------------------------


def _inputs(
    archive: ArchivePaths, demos: Sequence[tuple[str, str]]
) -> list[ManifestInput]:
    """Syötteet: jokaisen mukaan otetun demon ``classify``-manifesti."""
    inputs: list[ManifestInput] = []
    for lineup, demo in demos:
        path = archive.resolve(classified_manifest(lineup, demo))
        manifest = Manifest.read_if_exists(path)
        if manifest is None:
            raise PappascoutError(
                f"Luokittelun manifestia ei löytynyt polusta {path}, joten "
                "aggregoinnin syötettä ei voi tunnistaa.\n"
                f"Aja: uv run pappascout classify {demo} --team {lineup}"
            )
        inputs.append(
            ManifestInput(
                result_id=manifest.result_id, sha256=manifest.fingerprint()
            )
        )
    return inputs


#: Ne ``[thresholds]``- ja ``[league]``-avaimet, jotka **oikeasti muuttavat**
#: aggregoinnin tulosta. Vain nämä menevät parametrihashiin.
#:
#: Miksi nimetty luettelo eikä koko osio (toisin kuin ``classify``issa):
#: ``[thresholds]`` on luokittelun osio ja siinä on kolmisenkymmentä
#: kynnysarvoa, joista aggregointi lukee kaksi. Koko osion hashaaminen
#: mitätöisi jokaisen raportin aina kun mikä tahansa luokittelukynnys
#: muuttuu -- ja juuri silloin luokittelu ajetaan uudelleen, mikä näkyy jo
#: syötteiden tunnisteissa. Sama työ tehtäisiin siis kahdesti.
#:
#: Luettelon vanhenemisen estää testi
#: ``test_every_setting_the_stage_reads_is_in_the_params_hash``: se lukee
#: lähdekoodista, mitä kenttiä vaihe ja sen domain-funktiot lukevat, ja
#: vertaa tähän luetteloon.
#:
#: **Yksi tietoinen poikkeus.** :func:`~pappascout.domain.aggregate.classify_thresholds`
#: lukee kuusi luokittelun kynnysarvoa nimellä (``getattr``) verratakseen
#: niitä siihen, millä kierrokset oikeasti luokiteltiin. Ne eivät ole tässä
#: luettelossa eivätkä kuulukaan: ne eivät muuta yhtäkään raportin lukua,
#: vaan **keskeyttävät ajon** jos luokittelu on vanhentunut. Ja jos käyttäjä
#: ajaa luokittelun uudelleen, sen manifestin tunniste muuttuu -- eli
#: aggregointi ajetaan uudelleen syötteen eikä parametrin takia.
HASHED_THRESHOLD_KEYS: tuple[str, ...] = (
    "small_sample_rounds",
    "team_identity_min_common",
    # Poikkeamakynnykset (Story 2.5). Ilman näitä kynnyksen säätö ei ajaisi
    # aggregointia uudelleen, ja raportti pitäisi vanhat poikkeamat --
    # sama vika kuin Story 1.8:ssa. Vaiheella on tästä oma vartija
    # (``test_every_setting_the_stage_reads_is_in_the_params_hash``), joka
    # lukee luetut kentät lähdekoodista.
    "advance_t_share",
    "advance_area_min_observations",
    "advance_max_sample_s",
    "advance_min_players",
    "crunch_min_players",
    "crunch_min_sources",
)
HASHED_LEAGUE_KEYS: tuple[str, ...] = ("map_pool",)


def _params_hash(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    aggregate_settings: AggregateSettings,
) -> str:
    """AD-3: parametrihash vain siitä, mikä vaikuttaa tämän vaiheen tulokseen.

    ``[aggregate]`` on mukana **kokonaisena**, koska se on tämän vaiheen oma
    osio: jokainen sen arvo on määritelmän mukaan tälle vaiheelle, eikä
    luettelo voi vanheta. ``[thresholds]``- ja ``[league]``-osioista otetaan
    vain nimetyt avaimet (:data:`HASHED_THRESHOLD_KEYS`,
    :data:`HASHED_LEAGUE_KEYS`).

    ``[parse]`` ei ole mukana tarkoituksella: näytepisteet luetaan taulusta
    sellaisina kuin ne ovat, eikä niiden muuttaminen voi vaikuttaa tähän
    vaiheeseen ilman että parsinta ajetaan uudelleen -- ja se näkyy jo
    syötteiden tunnisteissa.
    """
    return compute_params_hash(
        {
            "thresholds": {
                key: getattr(thresholds, key) for key in HASHED_THRESHOLD_KEYS
            },
            "league": {key: getattr(league, key) for key in HASHED_LEAGUE_KEYS},
            "aggregate": aggregate_settings.model_dump(mode="json"),
        }
    )


# -- Tulosteen luvut -------------------------------------------------------------


def _stats(report: Report, sources: TeamSources) -> dict[str, Any]:
    """Luvut, jotka ``cli`` näyttää käyttäjälle."""
    return {
        "team_key": report.team.key,
        "lineup_keys": list(report.team.lineup_keys),
        "display_name": report.team.display_name,
        "display_name_source": report.team.display_name_source,
        "display_name_alternatives": list(report.team.display_name_alternatives),
        # Rosteri tulosteeseen pareina: nimi ja tunniste rinnakkain, ei
        # kumpaakaan yksin. Nimi voi olla ``None``, ja se on havainto.
        "roster": [
            {"player_id": entry.player_id, "display_name": entry.display_name}
            for entry in report.team.roster
        ],
        "demos": report.sample.demos,
        "rounds": report.sample.rounds,
        # Lokerot luetaan yhdestä luettelosta, jotta kolmas lokero ei voi
        # jäädä pois tulosteesta samalla kun se on rakenteessa.
        "sample": {
            name: {
                "demos": getattr(report.sample, name).demos,
                "rounds": getattr(report.sample, name).rounds,
            }
            for name in LEAGUE_BUCKETS
        },
        "unclassified": report.unclassified_rounds,
        "unpaired_detonations": report.unpaired_detonations,
        # Poikkeamat tulosteeseen, koska ne ovat epicin arvokkain tuotos: ilman
        # tätä riviä käyttäjä näkee kynnyksen säädön vaikutuksen vasta
        # avaamalla raportin. Kattavuus on rivillä mukana samasta syystä kuin
        # raportissa -- nolla poikkeamaa on havainto vain siitä, mitä
        # tutkittiin.
        "anomalies": [
            {
                "rule": entry.rule,
                "map_name": entry.map_name,
                "side": entry.side,
                "round_types": list(entry.round_types),
                "area": entry.area,
                "players_max": entry.players_max,
                "n": entry.n,
                "m": entry.m,
            }
            for entry in report.anomalies
        ],
        "anomaly_scan": {
            "rules": list(report.anomaly_scan.rules),
            "rules_deferred": list(report.anomaly_scan.rules_deferred),
            "rounds_scanned": report.anomaly_scan.rounds_scanned,
            "crunch_rounds": report.anomaly_scan.crunch_rounds,
            "advance_rounds": report.anomaly_scan.advance_rounds,
            "demos_without_orientation": list(
                report.anomaly_scan.demos_without_orientation
            ),
        },
        "classify_thresholds": dict(report.classify_thresholds),
        "maps": [
            {
                "map_name": m.map_name,
                "map_name_source": m.map_name_source,
                "demos": m.sample.demos,
                "rounds": m.sample.rounds,
                "sides": [
                    {
                        "side": s.side,
                        "round_types": {
                            rt.round_type: rt.sample.rounds
                            for rt in s.round_types
                        },
                        "small_samples": [
                            rt.round_type for rt in s.round_types if rt.small_sample
                        ],
                    }
                    for s in m.sides
                ],
            }
            for m in report.maps
        ],
        "missing_demos": [
            {"match": m.match, "reason": m.reason} for m in report.missing_demos
        ],
    }
