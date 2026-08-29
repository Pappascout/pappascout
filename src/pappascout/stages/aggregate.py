"""``aggregate`` -- putken kolmas vaihe: luokitelluista kierroksista raportti.

Vaihe lukee arkistosta joukkueen luokitellut kierrokset
(``classified/<team_key>/<map_demo_id>.parquet``) sekä niiden näytepiste- ja
tapahtumataulut (``parsed/<map_demo_id>/{ticks,events}.parquet``) ja kirjoittaa
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

Kartan nimi on johdettu
-----------------------
Kartan nimeä ei ole yhdessäkään taulussa -- ``parse`` ei kirjoita sitä. Se
päätellään ``map_demo_id``:stä karttapoolia vasten, ja ``map_name_source``
kertoo onnistuiko päättely. Tuntematon kartta ei sulaudu toisen kartan haaraan
vaan jää omakseen tunnisteensa nimellä.

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
from collections.abc import Sequence
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
from pappascout.domain.aggregate import (
    LEAGUE_BUCKETS,
    build_report,
    lineups_of_same_team,
    team_slug,
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
from pappascout.domain.schemas import CLASSIFIED, EVENTS, TICKS, Schema, validate
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

    # Kokoonpano, jonka yhdeltäkään demolta ei saatu näytepistetaulua, ei ole
    # vertailtavissa muihin -- eikä siis liitettävissä joukkueeseen. Sen demot
    # eivät saa silti kadota jäljettömiin: ne kirjataan puuttuviksi syyn
    # kanssa, jotta lukija näkee että otannasta puuttuu jotain, vaikka
    # aggregointi ei voi tietää kenelle se kuului.
    for lineup, lineup_demos in demos_by_lineup.items():
        if lineup in known:
            continue
        for demo in lineup_demos:
            missing.append(
                MissingDemo(
                    match=demo,
                    reason=(
                        f"Kokoonpanon {lineup} näytepistetaulua ei saatu "
                        "luettua, joten demoa ei voitu liittää joukkueeseen. "
                        f"Aja parsinta uudelleen: uv run pappascout parse {demo}"
                    ),
                )
            )

    if team_key not in known:
        raise PappascoutError(
            f"Joukkueen {team_key} kokoonpanoa ei saatu luettua: yhdenkään sen "
            "demon näytepistetaulua ei löytynyt arkistosta.\n"
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


def _lineup_members(
    archive: ArchivePaths, map_demo_id: str
) -> dict[str, set[str]] | None:
    """Demon kokoonpanot pelaajineen, tai ``None`` jos taulua ei ole."""
    path = archive.resolve(parsed_table(map_demo_id, "ticks"))
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
    for table in ("ticks", "events"):
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

    for lineup, demo in sources.demos:
        classified_frames.append(_read_classified(archive, lineup, demo))
        tick_frames.append(_read_parsed(archive, demo, "ticks", TICKS))
        event_frames.append(_read_parsed(archive, demo, "events", EVENTS))

    lineups = set(sources.lineup_keys)
    ticks = pl.concat(tick_frames).filter(pl.col("lineup_key").is_in(lineups))
    events = pl.concat(event_frames).filter(pl.col("lineup_key").is_in(lineups))

    team = TeamReport(
        key=sources.team_key,
        slug=team_slug(sources.team_key),
        # Joukkueen nimi tulee joukkueindeksistä (Epic 3). Ennen sitä tunniste
        # on ainoa nimi, joka on olemassa -- keksitty nimi olisi arvaus.
        display_name=sources.team_key,
        lineup_keys=sources.lineup_keys,
        roster=sources.roster,
        roster_source="lineups",
    )
    return build_report(
        classified=pl.concat(classified_frames),
        ticks=ticks,
        events=events,
        team=team,
        thresholds=thresholds,
        aggregate=aggregate_settings,
        map_pool=league.map_pool,
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
        "roster": list(report.team.roster),
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
