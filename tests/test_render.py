"""``render`` -- raportin valinnan ja muotoilun testit.

Vaihe ei lue demoa eikä arkistoa, joten koko raportti testataan käsin
rakennetuista :class:`~pappascout.domain.report.Report`-olioista. Testit
vastaavat spec-2-4:n I/O-matriisin rivejä ja sen hyväksymiskriteerejä.

Rakennusfunktiot ovat tässä tiedostossa, koska ``test_stage_render`` ja
``test_cli_report`` käyttävät niitä samoina: kolme kopiota erkanisi, ja
silloin vaiheen testi voisi mennä läpi raportilla, jota renderöinnin testi ei
koskaan näe.

**Fikstuuri kattaa molemmat variantit jokaisesta haarasta, jossa raportti
valitsee.** Ensikontaktin näytepiste on eri laji kuin aikanäytepiste, ja
havaittu räjähdysalue on eri asia kuin pistepilvestä johdettu; kumpaakin paria on
oltava fikstuurissa, tai vain toinen suunta on suojattu ja väärä oletusarvo
menee läpi kaikista väitteistä huomaamatta.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta, timezone

import pytest

from pappascout.constants import (
    ANOMALY_RULE_FI,
    ANOMALY_RULES,
    ROUND_TYPES,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
)
from pappascout.domain.report import (
    Anomaly,
    AnomalyRound,
    AnomalyScan,
    AreaDistribution,
    AreaOrientation,
    ArmedCount,
    ArmedPlayers,
    ArmoredCount,
    ArmoredPlayers,
    DeathReport,
    FirstContactArea,
    FirstDeathArea,
    GrenadeCount,
    KillArea,
    MapReport,
    MissingDemo,
    PlayersCount,
    Position,
    Report,
    RosterEntry,
    RoundTypeReport,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
    slugify,
)
from pappascout.errors import PappascoutError
from pappascout.render import view as view_module
from pappascout.render import (
    build_view,
    render_report,
    round_list_demo_ids,
    template_digest,
    template_text,
)
from pappascout.render.view import (
    ANOMALY_HEADING,
    GRENADE_ORDER,
    GRENADE_TYPE_FI,
    MAX_ANOMALY_LINES,
    MAX_DEATH_LINES,
    PATTERN_ROUND_TYPES,
    ROUND_TYPE_ORDER,
    TRACEABILITY_HEADING,
    UNKNOWN_AREA,
    UNNAMED_PLAYER,
    Claim,
    pattern_min_rounds,
)

# Yksityinen, mutta tuotu sisään tarkoituksella: jäljitettävyysluvun selitys
# on vakio, ja katkelman kopioiminen testiin tekisi siitä kaksi totuutta --
# sama peruste kuin TEAM_SLUGilla ja TRACEABILITY_HEADINGilla.
from pappascout.render.view import _TRACEABILITY_NOTE

TEAM_KEY = "aaaaaaaaaaaaaaaa"
TEAM_NAME = "MatureMayhem"

#: Slug, jonka oletusraportti saa: se johdetaan NIMESTÄ eikä
#: tunnisteesta, koska nimi on havainto. Tiedostonimen testit lukevat
#: sen täältä, jottei sääntö ole kahdessa paikassa eri muodossa.
TEAM_SLUG = "maturemayhem"

#: Rosteri, jossa jokaisella on nimi ja SteamID64. Runko puhuu nimillä ja
#: jäljitettävyysluku kantaa tunnisteet (Story 2.12), joten kiinnikkeessä on
#: oltava molemmat -- pelkillä nimillä liitteen väitteitä ei voisi kirjoittaa.
DEFAULT_ROSTER = [
    RosterEntry(player_id=str(n), display_name=f"pelaaja{n}")
    for n in range(1, 6)
]

#: Rosteri, jonka tunnisteet ovat SteamID64:n **muotoisia mutta keksittyjä**.
#:
#: Oletusrosterin ``1``..``5`` eivät kelpaa siihen väitteeseen, että rungossa
#: ei esiinny yhtään tunnistetta: ne eivät täsmää :data:`IDENTIFIER_SHAPE`en,
#: joten väite menisi läpi myös silloin kun numerot ovat yhä rungossa. Muoto
#: on siis se, mitä tämä kiinnike tuo -- ei kenenkään oikea tunniste.
#:
#: **Oikeat tunnisteet eivät kuulu tähän tiedostoon.** Arkisto ei ole gitissä
#: mutta testit ovat, ja aidon pelaajanimen ja SteamID64:n pari on
#: henkilötieto, jonka committoiminen ei tuo yhtään väitettä lisää: mittaus
#: koskee muotoa (``7656119`` + 10 numeroa), eikä muoto tarvitse ketään
#: oikeaa.
STEAM_ROSTER = [
    RosterEntry(
        player_id=f"765611900000000{number:02d}", display_name=f"pelaaja{number}"
    )
    for number in range(1, 8)
]

#: Tunnisteen **hahmo**: 16 merkin tiiviste tai 17 numeron SteamID64.
#:
#: Hahmo eikä luettelo, jotta väite "rungossa ei ole tunnisteita" ei nojaa
#: siihen, mitkä merkkijonot testi sattuu tuntemaan. Kolme laajennusta
#: spec-2-12:n käsintarkistuksen ``grep``iin nähden, jokainen omasta syystään:
#: isot kirjaimet mukaan (tiiviste voi tulla eteen kumpaa tahansa kirjainkokoa
#: käyttävästä lähteestä), ``7656119``-etuliite pois (SteamID64:n muoto on 17
#: numeroa, eikä pelaaja ole vähemmän pelaaja koska hänen tilinsä on uudempi)
#: ja rajaus pois hahmon reunoilta (pidempi heksajono on yhä tunniste).
#:
#: **Demotunnisteita hahmo ei tunnista eikä voi tunnistaa.**
#: ``ANCIENT_vs_RCAVE_VETERANS`` ei muistuta tiivistettä, ja FACEIT-tunnisteen
#: pisimmät heksajonot ovat 12 merkkiä. Ne todennetaan **kirjaimellisesti**:
#: testi tietää fikstuurinsa demotunnisteet ja väittää niistä nimeltä.
IDENTIFIER_SHAPE = re.compile(r"[0-9a-fA-F]{16}|[0-9]{17}")

#: Markdownin koodijakso. Tarvitaan, koska kierrosliitteen poikkeus on
#: **kapeampi kuin luku**: tunniste saa olla polussa, ja polku on koodijakso.
CODE_SPAN = re.compile(r"`[^`]*`")

#: FACEIT-tunniste, jossa ei ole kartan nimeä (Story 2.11). Kun kartan nimeä ei
#: tunnisteta, ``map_name`` **on** tämä merkkijono -- eli karttaluvun otsikko
#: on tunniste, ja se on rungon kolmas poikkeus.
FACEIT_DEMO_ID = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1"

#: Demo, joka ei päässyt otantaan. Tunniste on rungossa tarkoituksella: syy
#: sisältää komennon, jonka lukija kopioi.
MISSING_DEMO_ID = "ANCIENT_vs_RCAVE_VETERANS"

DEMO_ID = "Ancient_vs_kaljukostaja"

#: Kynnys, jonka raportti kantaa mukanaan. Sama luku kuin
#: ``settings.toml``issa; testit lukevat sen raportista, eivät asetuksista --
#: juuri kuten ``render`` itse.
SMALL_SAMPLE = 3

#: Kokoonpanojen liittämisen kynnys (``[thresholds].team_identity_min_common``,
#: AD-6). Samassa oletuskiinnikkeessä kuin :data:`SMALL_SAMPLE`, koska
#: kokoonpanorivi kirjoittaa sen näkyviin samalla tavalla kuin pienen otannan
#: rivi omansa -- ja ilman sitä kiinnike ei näyttäisi oikealta raportilta.
MIN_COMMON = 3

#: Kierroslistojen polut, jotka vaihe antaisi. Absoluuttisia, koska raportti
#: liitetään Discordiin eikä lukija tiedä missä arkiston juuri on.
ROUND_LISTS = (rf"C:\arkisto\classified\{TEAM_KEY}\{DEMO_ID}.md",)


# --- Rakennusfunktiot -----------------------------------------------------------


def sample(rounds: int, demos: int = 1, bucket: str = "unknown") -> Sample:
    """Otanta yhdessä lokerossa; muut jäävät nolliksi."""
    zero = SampleBucket(demos=0, rounds=0)
    buckets = {"league": zero, "other": zero, "unknown": zero}
    buckets[bucket] = SampleBucket(demos=demos, rounds=rounds)
    return Sample(
        demos=sum(b.demos for b in buckets.values()),
        rounds=sum(b.rounds for b in buckets.values()),
        **buckets,
    )


def area(name: str | None, m: int, bars: dict[int, int]) -> AreaDistribution:
    """Alueen jakauma. ``bars`` on ``pelaajamäärä -> kierroksia``."""
    return AreaDistribution(
        area=name,
        m=m,
        players_dist=[
            PlayersCount(players=players, n=n) for players, n in bars.items() if n
        ],
    )


def position(
    seconds: float | None,
    areas: list[AreaDistribution],
    m: int,
    *,
    kind: str = "time",
    median: float | None = None,
    missing: int = 0,
) -> Position:
    return Position(
        sample_kind=kind,
        seconds=seconds,
        seconds_median=median,
        m=m,
        rounds_missing=missing,
        areas=areas,
    )


def first_contact_position(
    areas: list[AreaDistribution], m: int, *, median: float | None = 9.05
) -> Position:
    """Ensikontaktin näytepiste: ei nimellistä sekuntilukua, vaan mediaani."""
    return position(None, areas, m, kind="first_contact", median=median)


def armed(m: int, bars: dict[int, int], unknown: int = 0) -> ArmedPlayers:
    return ArmedPlayers(
        m=m,
        rounds_unknown=unknown,
        counts=[ArmedCount(armed=count, n=n) for count, n in bars.items() if n],
    )


def armored(m: int, bars: dict[int, int], unknown: int = 0) -> ArmoredPlayers:
    return ArmoredPlayers(
        m=m,
        rounds_unknown=unknown,
        counts=[ArmoredCount(armored=count, n=n) for count, n in bars.items() if n],
    )


def counts(grenade_type: str, m: int, bars: dict[int, int]) -> UtilityCounts:
    return UtilityCounts(
        grenade_type=grenade_type,
        m=m,
        counts=[GrenadeCount(thrown=thrown, n=n) for thrown, n in bars.items() if n],
    )


def use(
    grenade_type: str,
    throw: str | None,
    detonate: str | None,
    *,
    n: int,
    m: int,
    throws: int | None = None,
    bucket: str = "0-5",
    source: str | None = "point_cloud",
) -> UtilityUse:
    return UtilityUse(
        grenade_type=grenade_type,
        throw_area=throw,
        detonate_area=detonate,
        area_source=source if detonate is not None else None,
        seconds_bucket=bucket,
        n=n,
        throws=throws if throws is not None else n,
        m=m,
    )


def deaths(
    *,
    first: dict[str | None, int] | None = None,
    rounds_missing: int = 0,
    median: float | None = None,
    kills: dict[str | None, int] | None = None,
) -> DeathReport:
    """Kuolemaosuus raporttiin.

    ``first`` on alue -> kierrokset, ``kills`` alue -> tapot. Nimittäjät
    lasketaan summista, koska juuri se on mallin sopimus: ensimmäisen
    kuoleman jakauman ``m`` on kierroksia ja tappojakauman ``m`` tappoja.

    ``rounds_missing`` on annettava niin, että ``m + rounds_missing`` on
    kierrostyypin otanta -- malli tarkistaa sen. :func:`round_type` täyttää
    sen puolestasi, kun kuolemaosuutta ei anneta.
    """
    first = first or {}
    kills = kills or {}
    m = sum(first.values())
    total = sum(kills.values())
    return DeathReport(
        m=m,
        rounds_missing=rounds_missing,
        first_death_seconds_median=median,
        first_death_areas=[
            FirstDeathArea(area=area, n=n, m=m) for area, n in first.items() if n
        ],
        kills_total=total,
        kills=[KillArea(area=area, n=n, m=total) for area, n in kills.items() if n],
    )


def round_type(
    name: str,
    rounds: int,
    *,
    positions: list[Position] | None = None,
    utility: list[UtilityUse] | None = None,
    utility_counts: list[UtilityCounts] | None = None,
    players_armed: ArmedPlayers | None = None,
    players_armored: ArmoredPlayers | None = None,
    first_contact: list[FirstContactArea] | None = None,
    death_report: DeathReport | None = None,
    small_sample: bool | None = None,
) -> RoundTypeReport:
    return RoundTypeReport(
        round_type=name,
        sample=sample(rounds),
        small_sample=rounds < SMALL_SAMPLE if small_sample is None else small_sample,
        positions=positions or [],
        utility=utility or [],
        utility_counts=utility_counts or [],
        players_armed=players_armed or armed(0, {}),
        players_armored=players_armored or armored(0, {}),
        first_contact=first_contact or [],
        # Ilman kuolemia jokainen kierros on "ei omia kuolemia" -- ja mallin
        # ristiintarkistus vaatii, että kuolemat kattavat koko otannan.
        deaths=(
            death_report
            if death_report is not None
            else deaths(rounds_missing=rounds)
        ),
    )


def side(name: str, round_types: list[RoundTypeReport]) -> SideReport:
    return SideReport(
        side=name,
        sample=sample(sum(rt.sample.rounds for rt in round_types)),
        round_types=round_types,
    )


def map_report(
    name: str,
    sides: list[SideReport],
    *,
    demo_ids: list[str] | None = None,
    source: str = "map_demo_id",
) -> MapReport:
    ids = demo_ids or [DEMO_ID]
    return MapReport(
        map_name=name,
        map_name_source=source,
        map_demo_ids=ids,
        sample=sample(sum(s.sample.rounds for s in sides), demos=len(ids)),
        sides=sides,
    )


def scan(**overrides) -> AnomalyScan:
    """Poikkeamasääntöjen kattavuus kiinnikkeeseen.

    Oletus on **täysi kattavuus ilman sokeita pisteitä**, koska se on se
    tila, jossa tyhjä poikkeamaluku on mitattu negatiivinen. Sokeat pisteet
    rakennetaan erikseen niitä koskevissa testeissä -- muuten jokainen muu
    testi mittaisi vahingossa varoituksen tekstiä.
    """
    values: dict[str, object] = {
        "rules": ["ct_advance", "crunch"],
        "rules_deferred": ["stack"],
        "rounds_scanned": 2,
        "crunch_rounds": 1,
        "advance_rounds": 0,
    }
    values.update(overrides)
    return AnomalyScan(**values)


def report(
    maps: list[MapReport] | None = None,
    *,
    anomalies: list[Anomaly] | None = None,
    scan_: AnomalyScan | None = None,
    missing_demos: list[MissingDemo] | None = None,
    unclassified: int = 0,
    unpaired: int = 0,
    thresholds_used: dict | None = None,
    classify_thresholds: dict | None = None,
    display_name: str = TEAM_NAME,
    display_name_source: str | None = None,
    name_alternatives: list[str] | None = None,
    roster: list[RosterEntry] | None = None,
    lineup_keys: list[str] | None = None,
    generated_at: datetime | None = None,
) -> Report:
    entries = maps or []
    rounds = sum(m.sample.rounds for m in entries)
    demos = sum(m.sample.demos for m in entries)
    # Lähde on kiinnikkeen OMA parametri eikä johdos nimestä. Jos se
    # johdettaisiin säännöllä "nimi == tunniste", tapaus jota varten
    # ``_has_name`` on kirjoitettu -- havaittu nimi, joka sattuu olemaan
    # tunnisteen näköinen -- ei tulisi ajetuksi kertaakaan.
    source = display_name_source or (
        "team_key" if display_name == TEAM_KEY else "clan_name"
    )
    return Report(
        generated_at=generated_at or datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        tool_versions={"pappascout": "0.1.0"},
        team=TeamReport(
            key=TEAM_KEY,
            # Slug johdetaan näytettävästä nimestä, kuten ``aggregate`` sen
            # johtaa; malli vartioi tämän parin.
            slug=slugify(display_name) or slugify(TEAM_KEY),
            display_name=display_name,
            display_name_source=source,
            display_name_alternatives=list(name_alternatives or []),
            lineup_keys=list(lineup_keys or [TEAM_KEY]),
            roster=list(roster if roster is not None else DEFAULT_ROSTER),
            roster_source="lineups",
        ),
        sample=sample(rounds, demos=demos),
        thresholds_used=(
            {
                "thresholds": {
                    "small_sample_rounds": SMALL_SAMPLE,
                    "team_identity_min_common": MIN_COMMON,
                    # Poikkeamakynnykset kiinnikkeeseen, koska lukuohje lukee
                    # ne raportista eikä keksi niitä. Samat luvut kuin
                    # settings.tomlissa.
                    "advance_t_share": 0.80,
                    "advance_area_min_observations": 20,
                    "advance_max_sample_s": 30.0,
                    "advance_min_players": 1,
                    "crunch_min_players": 2,
                    "crunch_min_sources": 2,
                }
            }
            if thresholds_used is None
            else thresholds_used
        ),
        classify_thresholds=(
            {"full_equip_min": 4000}
            if classify_thresholds is None
            else classify_thresholds
        ),
        unpaired_detonations=unpaired,
        missing_demos=missing_demos or [],
        unclassified_rounds=unclassified,
        # Oletus on tyhjä: poikkeamaluku on olemassa myös silloin, kun
        # poikkeamia ei ole, ja juuri se on jokaisen muun testin taustatila.
        anomalies=anomalies or [],
        anomaly_scan=scan_ if scan_ is not None else scan(),
        maps=entries,
    )


def pistol_map() -> MapReport:
    """Yksi kartta, jolla on pistoolikierros molemmilla puolilla.

    T-puolen lohko kattaa **kaikki** rivilajit, jotka raportti osaa
    kirjoittaa: aikanäytepiste, ensikontaktin näytepiste, kranaattimäärät,
    kranaattien kohteet (sekä havaittu että johdettu alue) ja aseistetut.
    """
    return map_report(
        "de_ancient",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[
                            position(
                                15.0,
                                [
                                    area("Middle", 1, {3: 1}),
                                    area("MainHall", 1, {2: 1}),
                                ],
                                1,
                            ),
                            first_contact_position([area("Middle", 1, {2: 1})], 1),
                        ],
                        utility_counts=[
                            counts("smoke", 1, {2: 1}),
                            counts("flashbang", 1, {2: 1}),
                        ],
                        utility=[
                            use("smoke", "TSpawn", "Middle", n=1, m=1),
                            use(
                                "flashbang",
                                "TSpawn",
                                "MainHall",
                                n=1,
                                m=1,
                                source="observed",
                            ),
                        ],
                        players_armed=armed(1, {0: 1}),
                        # Pistoolikierroksen koko juoni yhdellä rivillä:
                        # aseistettuja 0, kevlareita 5.
                        players_armored=armored(1, {5: 1}),
                    )
                ],
            ),
            side("CT", [round_type("pistol", 1)]),
        ],
    )


def default_map(rounds: int = 8) -> MapReport:
    """Kartta, jonka default-lohkossa on sekä kuvio että yksittäinen havainto."""
    return map_report(
        "de_inferno",
        [
            side(
                "T",
                [
                    round_type(
                        "full",
                        rounds,
                        positions=[
                            position(
                                15.0,
                                [
                                    # Kuvio: toistuu 4 kierroksella.
                                    area("Apartments", rounds, {3: 4, 0: rounds - 4}),
                                    # Yksittäinen: 1 kierros, ei kuvio.
                                    area("Banana", rounds, {2: 1, 0: rounds - 1}),
                                ],
                                rounds,
                            )
                        ],
                        utility_counts=[counts("smoke", rounds, {2: 5, 0: rounds - 5})],
                        utility=[
                            use("smoke", "TSpawn", "BombsiteB", n=4, m=rounds),
                            use("flashbang", "TSpawn", "Banana", n=1, m=rounds),
                        ],
                    )
                ],
            )
        ],
        demo_ids=["inferno_vs_ryhmarama"],
    )


def demo_map(
    demo_ids: list[str],
    *,
    name: str = "de_nuke",
    source: str = "map_demo_id",
) -> MapReport:
    """Kartta, jonka **demotunnisteet** ovat testin kohde.

    Sisältö on tarkoituksella pienin mahdollinen: näitä testejä kiinnostaa
    kartan otsikko ja jäljitettävyysluvun karttarivi, eivät havaintorivit.
    """
    return map_report(
        name,
        [side("T", [round_type("eco", 2)])],
        demo_ids=demo_ids,
        source=source,
    )


def unknown_map(demo_id: str = FACEIT_DEMO_ID) -> MapReport:
    """Kartta, jonka nimeä ei tunnistettu: **nimi on demotunniste**.

    Näin ``aggregate`` sen rakentaa (ks.
    :class:`~pappascout.domain.report.MapReport`): ilman havaintoa ja ilman
    poolista päättelyä nimi on ``map_demo_id`` itse ja lähde ``unknown``.
    Kiinnike ei siis kuvittele tilannetta vaan toistaa sen.
    """
    return demo_map([demo_id], name=demo_id, source="unknown")


def missing_demo(demo_id: str = MISSING_DEMO_ID) -> MissingDemo:
    """Puuttuva demo syynä, joka sisältää ajettavan komennon.

    Sanamuoto on ``aggregate``n oma (``stages/aggregate.py``): juuri se
    komento on syy, jonka takia tunniste jää rungon riville.
    """
    return MissingDemo(
        match=demo_id,
        reason=(
            "Kokoonpanotaulua (lineups.parquet) ei saatu luettua, joten ei "
            "tiedetä kuuluuko demo tälle joukkueelle. Aja parsinta uudelleen: "
            f"uv run pappascout parse {demo_id}"
        ),
    )


def render(entry: Report) -> str:
    """Raportti kierroslistojen polkuineen -- kuten vaihe sen kirjoittaa."""
    return render_report(entry, round_list_paths=ROUND_LISTS)


def report_sections(text: str) -> list[tuple[str, str]]:
    """Raportti luvuittain: ``(otsikko, sisältö ilman otsikkoriviä)``.

    Otsikko ja sisältö erikseen, koska tunnisteen poikkeukset osuvat eri
    kohtiin: karttaluvun **otsikko** voi olla demotunniste (kun kartan nimeä
    ei tunnistettu), mutta sen sisällön on silti oltava puhdas. Yhtenä
    merkkijonona kumpikaan väite ei olisi tehtävissä.

    Ensimmäinen alkio on dokumentin alku ennen yhtäkään ``## ``-otsikkoa, ja
    sen otsikko on tyhjä merkkijono: siinä on raportin ``# ``-otsikko, joka on
    yhtä lailla runkoa.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((line[3:], []))
        else:
            sections[-1][1].append(line)
    return [(heading, "\n".join(lines)) for heading, lines in sections]


def section_text(text: str, heading: str) -> str:
    """Yhden luvun sisältö. Puuttuva luku on virhe eikä tyhjä merkkijono."""
    for name, content in report_sections(text):
        if name == heading:
            return content
    raise AssertionError(f"raportissa ei ole lukua {heading!r}")


def summary_text(text: str) -> str:
    """Yhteenvedon sisältö -- se osa, jonka lukija lukee ensimmäisenä."""
    return section_text(text, "Yhteenveto")


def traceability_text(text: str) -> str:
    """Jäljitettävyysluvun sisältö."""
    return section_text(text, TRACEABILITY_HEADING)


# --- Perusmuoto -----------------------------------------------------------------


def test_report_has_the_structure_the_spec_asks_for() -> None:
    """Otsikko, yhteenveto, kartta, puoli, kierrostyyppi, kolme viimeistä lukua."""
    text = render(report([pistol_map()]))
    for expected in (
        f"# {TEAM_NAME} -- scouting-raportti",
        "## Yhteenveto",
        "## de_ancient -- 2 kierrosta, 1 demo",
        "### T-puoli -- 1 kierros",
        "### CT-puoli -- 1 kierros",
        "**Pistooli** (1 kierros)",
        "## Kierrosliite",
        "## Lukuohje",
        f"## {TRACEABILITY_HEADING}",
    ):
        assert expected in text, expected


def test_report_ends_with_exactly_one_newline() -> None:
    text = render(report([pistol_map()]))
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_positions_utility_and_first_contact_are_bullets_not_paragraphs() -> None:
    """Veetin analyysi on ranskalaisia viivoja; raportti on samaa muotoa."""
    text = render(report([pistol_map()]))
    body = text.split("**Pistooli** (1 kierros)")[1].split("\n\n")[0]
    # Ensimmäinen rivi on otsikon loppu ("-- pieni otanta"), ei havainto.
    rows = [row for row in body.splitlines()[1:] if row.strip()]
    assert rows, body
    assert all(row.startswith("- ") for row in rows), rows


def test_areas_stay_in_english_and_text_is_finnish() -> None:
    """Calloutit englanniksi, teksti suomeksi -- suomennoskerrosta ei ole."""
    text = render(report([pistol_map()]))
    assert "Middle 3" in text
    assert "MainHall 2" in text
    assert "kierroksesta" in text
    assert "utility:" in text


# --- Ensikontaktin näytepiste ---------------------------------------------------


def test_first_contact_position_is_labelled_by_its_median_not_by_zero_seconds() -> None:
    """Ensikontakti ei ole aikanäytepiste, eikä sillä ole sekuntilukua.

    Ilman tätä testiä ``sample_kind``-vartijan poistaminen otsikoisi jokaisen
    ensikontaktilohkon muotoon ``0 s`` ja mediaani katoaisi -- kaikissa
    oikeissa raporteissa, ilman että yksikään muu väite huomaisi mitään.
    """
    text = render(report([pistol_map()]))
    assert "- ensikontakti (mediaani 9,1 s): Middle 2 (1/1 kierroksesta)" in text
    assert "- 0 s:" not in text


def test_first_contact_without_a_median_is_still_labelled_first_contact() -> None:
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[
                            first_contact_position(
                                [area("Ramp", 1, {2: 1})], 1, median=None
                            )
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    assert "- ensikontakti: Ramp 2 (1/1 kierroksesta)" in text


def test_time_and_first_contact_positions_are_told_apart() -> None:
    """Kaksi näytepistelajia samassa lohkossa saavat eri otsikon."""
    view = build_view(report([pistol_map()]), round_list_paths=ROUND_LISTS)
    labels = [
        line.label
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label and ("s" in line.label or "ensikontakti" in line.label)
    ]
    assert "15 s" in labels
    assert any(label.startswith("ensikontakti") for label in labels)


# --- Jokainen väite kantaa otantansa --------------------------------------------

#: Yhden väitteen otanta sellaisena kuin malli sen latoo.
_SAMPLE = re.compile(r" \(\d+/\d+ kierroksesta(?:, [^)]*)?\)")


def observation_rows(text: str) -> list[str]:
    """Havaintorivit: ranskalaiset viivat, jotka eivät ole kursivoituja huomioita."""
    body = text.split("## de_")[1].split("## Kierrosliite")[0]
    return [
        row
        for row in body.splitlines()
        if row.startswith("- ") and not row.startswith("- *")
    ]


def claim_segments(row: str) -> list[str]:
    """Pilko rivi väitteiksi poistamalla otannat.

    Palauttaa väitteiden tekstit ilman otantoja. Rivin lopun huomautus
    (``" -- ..."``) ja alun otsikko (``"- 15 s: "``) leikataan pois, joten
    jäljelle jää vain se osa, jonka jokaisen palan **on** kannettava otantansa.
    """
    part = row[2:]
    if " -- " in part:
        part = part.split(" -- ")[0]
    if ": " in part:
        part = part.split(": ", 1)[1]
    return _SAMPLE.sub("\x00", part).split("\x00")


def test_every_claim_on_every_line_carries_its_own_sample() -> None:
    """Spec-2-4:n hyväksymiskriteeri sanatarkasti.

    Tarkistus on kaksisuuntainen, koska pelkkä "rivillä on otanta" menisi läpi
    rivistä, jossa on kolme väitettä ja otanta vain yhdellä:

    1. Jokainen rivi **päättyy** otantaan, eli viimeistä väitettä ei ole
       jätetty ilman.
    2. Otantojen väliin jäävät palat ovat kokonaisia väitteitä eivätkä sisällä
       erotinta ``", "`` -- jos väite jäisi ilman otantaa, se sulautuisi
       naapuriinsa ja erotin jäisi palan sisään.
    """
    text = render(report([pistol_map(), default_map()]))
    rows = observation_rows(text)
    assert rows
    for row in rows:
        segments = claim_segments(row)
        if len(segments) == 1:
            # Rivi ilman väitteitä on pelkkä huomautus (esim. puuttuva näyte).
            assert "(" not in segments[0], row
            continue
        assert segments[-1] == "", row
        for index, segment in enumerate(segments[:-1]):
            text_only = segment[2:] if index else segment
            assert text_only, row
            assert ", " not in text_only, row


def test_the_number_of_samples_matches_the_number_of_claims() -> None:
    """Toinen suunta samasta säännöstä, laskettuna näkymämallista.

    Jos yksi väite jäisi ilman otantaa, otantojen määrä olisi väitteiden
    määrää pienempi -- riippumatta siitä, missä kohtaa riviä se on.
    """
    entry = report([pistol_map(), default_map()])
    text = render(entry)
    view = build_view(entry, round_list_paths=ROUND_LISTS)
    claims = sum(
        len(line.claims)
        for map_view in view.maps
        for side_view in map_view.sides
        for round_view in side_view.round_types
        for line in round_view.lines
    )
    assert claims > 0
    assert len(re.findall(r"\(\d+/\d+ kierroksesta", text)) == claims


def test_sample_is_written_as_n_of_m_rounds() -> None:
    text = render(report([pistol_map()]))
    assert "Middle 3 (1/1 kierroksesta)" in text


# --- Säästökierrokset vs. default -----------------------------------------------


def test_default_shows_only_repeating_patterns() -> None:
    """Täysillä ostoilla kerrotaan suuria viivoja, ei kierroskohtaista."""
    text = render(report([default_map()]))
    assert "Apartments 3 (4/8 kierroksesta)" in text
    assert "Banana 2" not in text
    assert "vain toistuvat kuviot" in text


def test_default_says_how_many_observations_it_left_out() -> None:
    """Suodatus ei ole hiljainen: pois jääneiden määrä kirjoitetaan näkyviin."""
    text = render(report([default_map()]))
    assert re.search(
        r"toistuvat vähintään 3 kierroksella; \d+ harvinaisempaa havaintoa jäi pois",
        text,
    ), text


def test_saving_rounds_show_every_observation() -> None:
    """Pistooli, eco, force ja puoliosto kuvataan kierroksen tarkkuudella."""
    single = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "eco",
                        4,
                        positions=[position(6.0, [area("Ramp", 4, {2: 1, 0: 3})], 4)],
                    )
                ],
            )
        ],
    )
    text = render(report([single]))
    assert "Ramp 2 (1/4 kierroksesta)" in text
    assert "vain toistuvat kuviot" not in text


def test_saving_rounds_never_claim_that_a_threshold_dropped_anything() -> None:
    """Säästökierroksilla kynnys on 1, eikä yksikään pylväs voi alittaa sitä."""
    text = render(report([pistol_map()]))
    assert "jäi pois" not in text
    assert "kynnyksen alta" not in text


def test_the_pattern_threshold_also_applies_to_counts_and_armed_players() -> None:
    """Kynnys koskee kaikkia rivilajeja, ei vain sijoittumisia.

    Kun yksikään pylväs ei toistu, koko rivi jää pois isostakin otannasta --
    ja se on tarkoitus: hajonnut jakauma ei ole kuvio. Pois jääneet lasketaan
    mukaan lukuun, jonka lohko kertoo.

    **Panssarirvi on mukana**, koska kynnys puree oikeasti juuri
    ``full``-haaroilla ja ne ovat raportin yleisimmät. Ilman sitä
    panssarirvin suodatus olisi kokonaan testaamatta: jokaisessa muussa
    testissä kierrostyyppi on ``pistol`` tai ``eco``, joissa ``min_n`` on 1.
    """
    scattered = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "full",
                        8,
                        # Kahdeksan kierrosta, kahdeksan eri lukemaa: mikään
                        # ei toistu kolmesti.
                        players_armed=armed(8, dict.fromkeys(range(8), 1)),
                        players_armored=armored(8, dict.fromkeys(range(8), 1)),
                        utility_counts=[
                            counts("smoke", 8, dict.fromkeys(range(8), 1))
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([scattered]))
    assert "aseistettuja ostoajan lopussa" not in text
    assert "panssaroituja ostoajan lopussa" not in text
    assert "utility:" not in text
    # 8 aseistettujen pylvästä + 8 panssarin + 7 kranaatin (nolla ei ole
    # havainto eikä siis pudotettava).
    assert "23 harvinaisempaa havaintoa jäi pois" in text


def test_the_pattern_threshold_keeps_a_repeating_armored_bar() -> None:
    """Kynnys ei saa syödä kuviota, joka oikeasti toistuu.

    Pari edellisen kanssa: pelkkä "rivi katosi" -väite menisi läpi myös
    toteutuksella, joka pudottaa panssarirvin aina ``full``-haarasta.
    """
    repeating = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "full",
                        8,
                        players_armored=armored(8, {5: 6, 4: 1, 3: 1}),
                    )
                ],
            )
        ],
    )
    text = render(report([repeating]))
    assert "panssaroituja ostoajan lopussa: 5 (6/8 kierroksesta)" in text
    assert "4 (1/8" not in text
    assert "2 harvinaisempaa havaintoa jäi pois" in text


def test_pattern_threshold_comes_from_the_report_not_from_code() -> None:
    """Kynnys luetaan ``thresholds_used``ista; ilman sitä ei suodateta."""
    without = report([default_map()], thresholds_used={})
    assert pattern_min_rounds(without) is None
    text = render(without)
    assert "Banana 2 (1/8 kierroksesta)" in text
    assert "Toistumisen kynnystä ei ollut raportissa" in text


@pytest.mark.parametrize("value", [0, -1, "3", True, None])
def test_unusable_threshold_is_treated_as_missing(value: object) -> None:
    """Kelvoton kynnys ei saa muuttua hiljaa suodattimeksi."""
    entry = report(
        [default_map()], thresholds_used={"thresholds": {"small_sample_rounds": value}}
    )
    assert pattern_min_rounds(entry) is None


# --- I/O-matriisi ---------------------------------------------------------------


def test_small_sample_is_marked_not_hidden() -> None:
    """Alle kynnyksen jäävä otanta merkitään, mutta havainto näkyy silti."""
    text = render(report([pistol_map()]))
    assert "**Pistooli** (1 kierros) -- pieni otanta" in text
    assert "Middle 3" in text


def test_unknown_league_status_is_said_out_loud() -> None:
    """Kun yhdenkään demon lajia ei tiedetä, yhteenveto sanoo sen."""
    text = render(report([pistol_map()]))
    assert "**Liigatieto:**" in text
    assert "tuntematon" in text


def in_league(entry: Report) -> Report:
    """Siirrä raportin koko otanta ``unknown``-lokerosta ``league``-lokeroon.

    Lokero on rakenteessa jokaisella tasolla, ja malli tarkistaa summat
    **lokero kerrallaan**, joten pelkkä ylätason vaihtaminen ei kelpaa. Kierto
    mallin läpi (``model_dump`` -> ``model_validate``) varmistaa samalla, että
    raportti selviää sarjallistuksesta -- juuri sen ``render`` tekee lukiessaan
    ``report.json``in.
    """
    data = entry.model_dump(mode="json")

    def swap(node: object) -> None:
        if isinstance(node, dict):
            if {"league", "other", "unknown"} <= set(node):
                node["league"] = node["unknown"]
                node["unknown"] = {"demos": 0, "rounds": 0}
            for value in node.values():
                swap(value)
        elif isinstance(node, list):
            for value in node:
                swap(value)

    swap(data)
    return Report.model_validate(data)


def test_confirmed_league_demos_do_not_trigger_the_warning() -> None:
    entry = in_league(report([pistol_map()]))
    assert entry.sample.league.demos == 1
    assert "**Liigatieto:**" not in render(entry)


def test_missing_demos_get_their_own_section_with_reasons() -> None:
    text = render(
        report(
            [pistol_map()],
            missing_demos=[MissingDemo(match="Nuke_vs_imuaijat", reason="ei parsittu")],
        )
    )
    assert "## Puuttuvat demot" in text
    assert "Nuke_vs_imuaijat" in text
    assert "ei parsittu" in text


def test_unclassified_rounds_are_mentioned_in_the_summary() -> None:
    text = render(report([pistol_map()], unclassified=4))
    assert "**Luokittelemattomat:**" in text
    assert "4 kierrosta" in text


def test_unpaired_detonations_are_mentioned_in_the_summary() -> None:
    text = render(report([pistol_map()], unpaired=7))
    assert "**Parittomat räjähdykset:**" in text
    assert "7 kpl" in text


def test_a_round_type_that_does_not_exist_gets_no_empty_heading() -> None:
    """Kartta ilman forcea: otsikkoa ei kirjoiteta tyhjänä."""
    text = render(report([pistol_map()]))
    assert "**Force**" not in text
    assert "**Eco**" not in text


def test_a_round_type_without_observations_says_so_rather_than_going_silent() -> None:
    """Kierrostyyppi ilman havaintoja ja kynnyksen syömä lohko ovat eri asia.

    Kierrosmäärä on itsessään havainto: jos kierroksia on, joukkue joko
    menetti pelaajia tai ei, ja kummastakin kerrotaan. "Ei havaintoja" on
    siksi varattu lohkolle, jolla ei ole yhtään kierrosta -- muuten se
    väittäisi tietämättömyyttä tilanteessa, josta tiedetään jotain.
    """
    nothing = map_report(
        "de_nuke",
        [side("T", [round_type("eco", 0)])],
    )
    assert "Ei havaintoja tältä kierrostyypiltä." in render(report([nothing]))

    # Sama lohko kierroksineen kertoo, ettei kukaan kuollut -- eikä väitä
    # olevansa havainnoton.
    played = map_report("de_nuke", [side("T", [round_type("eco", 4)])])
    text = render(report([played]))
    assert "ei omia kuolemia 4 kierroksella" in text
    assert "Ei havaintoja tältä kierrostyypiltä." not in text

    # Täysillä ostoilla suodatussääntö kerrotaan yhä, vaikka lohkossa on rivi.
    default = map_report("de_nuke", [side("T", [round_type("full", 8)])])
    assert "Vain kuviot, jotka toistuvat vähintään 3 kierroksella" in render(
        report([default])
    )


def test_a_side_without_round_types_is_not_a_bare_heading() -> None:
    entry = map_report("de_nuke", [side("CT", [])])
    text = render(report([entry]))
    assert "### CT-puoli" in text
    assert "Ei yhtään luokiteltua kierrostyyppiä" in text


def test_a_map_without_sides_is_not_a_bare_heading() -> None:
    entry = map_report("de_nuke", [])
    text = render(report([entry]))
    assert "## de_nuke" in text
    assert "Ei havaintoja kummaltakaan puolelta" in text


def test_area_without_a_name_is_named_and_explained() -> None:
    """``area = null`` on tuntematon alue, ja koordinaattien puute todetaan."""
    unknown = map_report(
        "de_anubis",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[position(6.0, [area(None, 1, {4: 1})], 1)],
                    )
                ],
            )
        ],
    )
    text = render(report([unknown]))
    assert f"{UNKNOWN_AREA} 4 (1/1 kierroksesta)" in text
    assert "Koordinaatteja ei ole report.jsonissa" in text


def test_the_unknown_area_note_is_absent_when_every_area_is_known() -> None:
    assert "Koordinaatteja ei ole" not in render(report([pistol_map()]))


def test_empty_report_still_writes_a_summary_and_says_there_is_no_data() -> None:
    """Tyhjä raportti: yhteenveto kirjoitetaan, ja aineiston puute todetaan."""
    text = render(report([]))
    assert "## Yhteenveto" in text
    assert "Aineistoa ei ole" in text
    assert "## de_" not in text


def test_unknown_map_name_is_flagged_in_the_heading() -> None:
    entry = map_report(
        "1-uuid-1-1",
        [side("T", [round_type("pistol", 1)])],
        demo_ids=["1-uuid-1-1"],
        source="unknown",
    )
    assert "kartan nimeä ei tunnistettu" in render(report([entry]))


@pytest.mark.parametrize("source", ["demo_header", "map_demo_id"])
def test_a_known_map_name_is_not_flagged(source: str) -> None:
    """Merkintä kuuluu vain lähteelle ``unknown`` (Story 2.11).

    ``demo_header`` on havainto demon otsikosta ja ``map_demo_id`` päättely
    tunnisteesta; kumpikin on tunnistettu nimi. Ehto, joka luettelee tunnetut
    lähteet, tekisi jokaisesta uudesta lähteestä hiljaa "tuntemattoman".
    """
    entry = map_report(
        "de_ancient",
        [side("T", [round_type("pistol", 1)])],
        source=source,
    )
    text = render(report([entry]))
    assert "kartan nimeä ei tunnistettu" not in text
    assert "de_ancient" in text


def test_missing_sample_point_is_reported_not_dropped() -> None:
    """45 s puuttuu kierrokselta, joka ratkesi aiemmin -- ero kirjoitetaan."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "eco",
                        4,
                        positions=[
                            position(45.0, [area("Ramp", 3, {1: 3})], 3, missing=1)
                        ],
                    )
                ],
            )
        ],
    )
    assert "näyte puuttuu 1 kierrokselta" in render(report([entry]))


# --- Utility --------------------------------------------------------------------


def test_grenade_counts_answer_how_many_were_thrown() -> None:
    """Tavoiteanalyysin rivi "2 savua 2 valoo"."""
    text = render(report([pistol_map()]))
    assert "savu 2 kpl (1/1 kierroksesta)" in text
    assert "valo 2 kpl (1/1 kierroksesta)" in text


def test_grenade_uses_answer_where_it_went() -> None:
    """Tavoiteanalyysin rivi "T-spawnista CT-savu B sitelle"."""
    text = render(report([pistol_map()]))
    assert "savu: TSpawn -> Middle (arvio) 0-5 s (1/1 kierroksesta)" in text


def test_only_a_derived_area_is_marked_as_an_estimate() -> None:
    """Havaittu alue on havainto, eikä sitä saa merkitä arvioksi.

    Fikstuurissa on molemmat suunnat: savun räjähdysalue on johdettu
    (``point_cloud``) ja valon havaittu (``observed``). Ilman havaittua tapausta
    ehdon poistaminen merkitsisi **jokaisen** alueen arvioksi, ja legenda
    väittäisi havainnot arvioiksi -- ilman että yksikään väite kaatuisi.
    """
    text = render(report([pistol_map()]))
    assert "savu: TSpawn -> Middle (arvio)" in text
    assert "valo: TSpawn -> MainHall 0-5 s" in text
    assert "MainHall (arvio)" not in text


def test_the_estimate_note_appears_only_when_something_was_estimated() -> None:
    observed_only = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[
                            use("smoke", "TSpawn", "Ramp", n=1, m=1, source="observed")
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([observed_only]))
    assert "(arvio)" not in text
    assert "pistepilvestä" not in text


def test_estimated_detonation_area_is_marked_and_explained() -> None:
    text = render(report([pistol_map()]))
    assert "(arvio)" in text
    assert "alue on luettu demon pistepilvestä" in text


def test_throws_are_reported_when_they_outnumber_the_rounds() -> None:
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[use("flashbang", "TSpawn", None, n=1, m=1, throws=2)],
                    )
                ],
            )
        ],
    )
    assert "2 heittoa" in render(report([entry]))


def test_one_line_per_grenade_type_not_per_throw() -> None:
    """Viisi savuriviä veisi viisi riviä kertoakseen yhden asian."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[
                            use("smoke", "TSpawn", "BombsiteA", n=1, m=1),
                            use("smoke", "TSpawn", "BombsiteB", n=1, m=1),
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    assert len([row for row in text.splitlines() if row.startswith("- savu:")]) == 1
    assert "BombsiteA" in text
    assert "BombsiteB" in text


@pytest.mark.parametrize(
    "bucket,expected,unexpected",
    [
        ("10-20", "10-20 s", None),
        (UTILITY_BUCKET_ALL, None, "kaikki s"),
        (UTILITY_BUCKET_UNKNOWN, "(heittoaika tuntematon)", "tuntematon s"),
    ],
)
def test_special_bucket_names_are_not_glued_to_the_word_seconds(
    bucket: str, expected: str | None, unexpected: str | None
) -> None:
    """``kaikki`` ja ``tuntematon`` eivät ole aikavälejä."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        utility=[
                            use("smoke", "TSpawn", "Ramp", n=1, m=1, bucket=bucket)
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    if expected is not None:
        assert expected in text
    if unexpected is not None:
        assert unexpected not in text


def test_finnish_names_cover_every_grenade_type_the_parser_knows() -> None:
    """Uusi kranaattityyppi ei saa jäädä ilman nimeä eikä järjestystä."""
    from pappascout.adapters.demo_parser import FIRE_ITEM_TYPES, GRENADE_TYPES

    known = set(GRENADE_TYPES.values()) | set(FIRE_ITEM_TYPES.values())
    assert known <= set(GRENADE_TYPE_FI)
    assert known <= set(GRENADE_ORDER)


# --- Aseistetut -----------------------------------------------------------------


def test_armed_players_are_shown_with_the_caveat_that_they_are_not_kevlar() -> None:
    """Laskuri on "panssari JA parannettu ase" -- se ei ole kevlarien määrä."""
    text = render(report([pistol_map()]))
    assert "aseistettuja ostoajan lopussa: 0 (1/1 kierroksesta)" in text
    assert "kevlarien määrän" in text


def test_the_kevlar_caveat_is_absent_when_no_armed_line_was_written() -> None:
    entry = map_report("de_nuke", [side("T", [round_type("pistol", 1)])])
    assert "kevlarien määrä" not in render(report([entry]))


def test_an_armed_line_alone_still_gets_its_own_definition() -> None:
    """Vain aseistettujen rivi: selitys on sen oma eikä parin yhteinen.

    Kolmas haara :func:`_player_counter_legend`istä. Ilman tätä testiä
    yksinäinen aseistettujen rivi voisi jäädä ilman määritelmää tai saada
    lauseen, joka puhuu panssarirvistä jota raportissa ei ole.
    """
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("pistol", 1, players_armed=armed(1, {0: 1}))])],
    )
    text = render(report([entry]))
    assert "Aseistettu = panssari JA parannettu ase" in text
    assert "panssaroitu = panssari, aseesta riippumatta" not in text


def test_rounds_without_an_inventory_reading_are_reported() -> None:
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("eco", 4, players_armed=armed(3, {0: 3}, unknown=1))])],
    )
    assert "havainto puuttuu 1 kierrokselta" in render(report([entry]))


# --- Panssaroidut (Story 2.8) ---------------------------------------------------


def test_the_armored_line_reads_veetis_five_kevlars() -> None:
    """*"5 kevlaria"*: panssaririvi on raportissa omana rivinään otantoineen."""
    text = render(report([pistol_map()]))
    assert "panssaroituja ostoajan lopussa: 5 (1/1 kierroksesta)" in text


def test_the_two_counters_stand_side_by_side_and_differ() -> None:
    """Molemmat rivit samassa lohkossa, eri luvut -- se ero on havainto.

    Ilman tätä testiä toteutus, joka renderöi saman jakauman kahdesti, menisi
    läpi jokaisesta muusta väitteestä.
    """
    view = build_view(report([pistol_map()]))
    lines = {
        line.label: tuple(claim.text for claim in line.claims)
        for line in view.maps[0].sides[0].round_types[0].lines
    }
    assert lines["aseistettuja ostoajan lopussa"] == ("0",)
    assert lines["panssaroituja ostoajan lopussa"] == ("5",)


def test_the_armored_line_follows_the_armed_line() -> None:
    """Järjestys on osa havaintoa: rivit luetaan parina."""
    view = build_view(report([pistol_map()]))
    labels = [line.label for line in view.maps[0].sides[0].round_types[0].lines]
    assert (
        labels.index("panssaroituja ostoajan lopussa")
        == labels.index("aseistettuja ostoajan lopussa") + 1
    )


def test_the_legend_explains_the_two_counters_as_one_nested_pair() -> None:
    """Selitys on **yksi kappale**, koska luvut ovat sisäkkäisiä.

    Kaksi erillistä lausetta jättäisi "aseistettuja 0" ja "panssaroituja 5"
    kahdeksi irralliseksi luvuksi. Lukuohjeen on sanottava osajoukkosuhde,
    yhteinen tick ja yhteinen jakaja, koska niistä rivien ero syntyy.
    """
    text = render(report([pistol_map()]))
    assert "aseistetut ovat panssaroitujen osajoukko" in text
    assert "samalta tickiltä samasta pelaajajoukosta" in text
    assert "jakaja on sama" in text


def test_the_legend_says_the_counters_are_holdings_not_purchases() -> None:
    """Panssari säilyy kierroksen yli, joten luku ei ole ostohavainto.

    Poikkeus on pistoolikierros, ja juuri se pelastaa rivin *"5 kevlaria"*.
    Molemmat puolet kuuluvat lukuohjeeseen: ilman ensimmäistä lukija lukee
    jokaisen econ ostoksena, ilman jälkimmäistä hän epäilee myös pistoolia.
    """
    text = render(report([pistol_map()]))
    assert "hallussapitoa eivätkä ostoja" in text
    assert "Poikkeus on pistoolikierros" in text


def test_the_armored_line_alone_still_gets_its_own_definition() -> None:
    """Vain panssarirvi: selitys ei saa puhua aseistetuista."""
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("pistol", 1, players_armored=armored(1, {5: 1}))])],
    )
    text = render(report([entry]))
    assert "Panssaroitu = panssari ostoajan lopussa, aseesta riippumatta" in text
    assert "Aseistettu = panssari JA parannettu ase" not in text


def test_the_armored_legend_is_absent_when_no_armored_line_was_written() -> None:
    entry = map_report("de_nuke", [side("T", [round_type("pistol", 1)])])
    assert "aseesta riippumatta" not in render(report([entry]))


def test_the_ancient_ct_row_reads_no_kevlars() -> None:
    """*"Kitit ja duelit takaboksille piiloon (ei kevuja)"*: 1/5 kevlaria."""
    entry = map_report(
        "de_ancient",
        [side("CT", [round_type("pistol", 1, players_armored=armored(1, {1: 1}))])],
    )
    assert "panssaroituja ostoajan lopussa: 1 (1/1 kierroksesta)" in render(
        report([entry])
    )


def test_a_wholly_unreadable_armor_observation_still_gets_a_line() -> None:
    """``m=0, rounds_unknown=n``: rivi kirjoitetaan, vaikka väitteitä ei ole.

    Ilman riviä lukija ei erottaisi haaraa "kenelläkään ei ollut panssaria"
    (joka näkyisi nollana) haarasta "panssaria ei saatu luettua" (joka vain
    puuttuisi) -- ja juuri sen eron säilyttäminen on tämän sarakkeen
    olemassaolon syy. Rivi on pelkkä otsikko ja huomautus, sama muoto kuin
    kuolemattomalla kierrostyypillä.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [round_type("eco", 4, players_armored=armored(0, {}, unknown=4))],
            )
        ],
    )
    view = build_view(report([entry]))
    lines = [
        line
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label == "panssaroituja ostoajan lopussa"
    ]

    assert len(lines) == 1
    assert lines[0].claims == ()
    assert lines[0].note == "havainto puuttuu 4 kierrokselta"


def test_a_wholly_unreadable_armed_observation_still_gets_a_line() -> None:
    """Sama haara aseistettujen rivillä -- se oli todentamatta jo ennen tätä."""
    entry = map_report(
        "de_nuke",
        [side("T", [round_type("eco", 4, players_armed=armed(0, {}, unknown=4))])],
    )
    view = build_view(report([entry]))
    lines = [
        line
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label == "aseistettuja ostoajan lopussa"
    ]

    assert len(lines) == 1
    assert lines[0].claims == ()
    assert lines[0].note == "havainto puuttuu 4 kierrokselta"


def test_a_note_only_counter_line_still_gets_its_legend() -> None:
    """Otsikko ilman määritelmää olisi pahempi kuin puuttuva rivi.

    Lippu nousee rivin kirjoittamisesta eikä väitteiden olemassaolosta: rivi
    "panssaroituja ostoajan lopussa: havainto puuttuu 4 kierrokselta" on
    lukijalle yhtä uusi käsite kuin väitteellinen rivi.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [round_type("eco", 4, players_armored=armored(0, {}, unknown=4))],
            )
        ],
    )
    text = render(report([entry]))
    assert "panssaroituja ostoajan lopussa" in text
    assert "Panssaroitu = panssari ostoajan lopussa, aseesta riippumatta" in text


def test_rounds_without_an_armor_reading_are_reported() -> None:
    """Puuttuva havainto sanotaan ääneen -- se ei ole nolla kevlaria."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [round_type("eco", 4, players_armored=armored(3, {0: 3}, unknown=1))],
            )
        ],
    )
    assert "havainto puuttuu 1 kierrokselta" in render(report([entry]))


# --- Ensikontaktin läsnäololista ------------------------------------------------


def test_presence_only_first_contact_areas_are_not_lost() -> None:
    """Läsnäolo ilman vastaavaa jakaumariviä kirjoitetaan omalle rivilleen.

    Raportti näyttää ensikontaktin näytepisteestä, koska se on yliaineisto.
    Oletus ei saa jäädä oletukseksi: jos aggregointi tuottaa alueen vain
    läsnäololistaan, havainto katoaisi muuten jäljettömiin.
    """
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[first_contact_position([area("Ramp", 1, {2: 1})], 1)],
                        first_contact=[
                            FirstContactArea(area="Ramp", n=1, m=1),
                            FirstContactArea(area="Heaven", n=1, m=1),
                        ],
                    )
                ],
            )
        ],
    )
    text = render(report([entry]))
    assert "ensikontakti, vain läsnäolo: Heaven (1/1 kierroksesta)" in text
    # Ramp on jo jakaumassa, joten sitä ei toisteta.
    assert text.count("Ramp") == 1


def test_no_presence_line_when_the_distribution_covers_everything() -> None:
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "pistol",
                        1,
                        positions=[first_contact_position([area("Ramp", 1, {2: 1})], 1)],
                        first_contact=[FirstContactArea(area="Ramp", n=1, m=1)],
                    )
                ],
            )
        ],
    )
    assert "vain läsnäolo" not in render(report([entry]))


# --- Yhteenvedon luettavuus -----------------------------------------------------


def test_a_team_without_a_name_says_so_instead_of_repeating_the_hash() -> None:
    """Lähde ``team_key`` tarkoittaa, ettei nimeä havaittu.

    Otsikkoon ei kirjoiteta tiivistettä nimen paikalle: se lukisi kuin joukkue
    olisi nimeltään niin. Story 2.12: tiivistettä ei kirjoiteta myöskään
    yhteenvedon riville -- runko kertoo puuttumisen **syyn kanssa** ja sanoo
    mistä tunniste löytyy, ja tunniste itse on jäljitettävyysluvussa.
    """
    entry = report(
        [pistol_map()], display_name=TEAM_KEY, display_name_source="team_key"
    )
    text = render(entry)
    summary = summary_text(text)

    assert text.startswith("# Scouting-raportti -- joukkueen nimi ei tiedossa")
    assert "nimi ei ole tiedossa." in summary
    assert "team_clan_name" in summary
    assert TEAM_KEY not in summary
    # Rivi kertoo myös mistä tunniste löytyy: nimettömältä joukkueelta se on
    # ainoa, mitä lukijalla on.
    assert TRACEABILITY_HEADING in summary
    assert f"**Joukkueen tunniste:** `{TEAM_KEY}`" in traceability_text(text)


def test_an_observed_name_that_looks_like_the_key_is_still_a_name() -> None:
    """Lähde ratkaisee, ei vertailu tunnisteeseen (``_has_name``).

    Tämä on se ainoa tapaus, jota varten ``_has_name`` on olemassa: joukkueen
    klaaninimi voi olla täsmälleen tunnisteensa näköinen, ja vertailuun
    perustuva sääntö väittäisi silloin havaintoa puuttuvaksi -- eli piilottaisi
    demosta luetun nimen.
    """
    entry = report(
        [pistol_map()], display_name=TEAM_KEY, display_name_source="clan_name"
    )
    text = render(entry)
    assert text.startswith(f"# {TEAM_KEY} -- scouting-raportti")
    assert "nimi ei ole tiedossa" not in text
    assert f"- **Joukkue:** {TEAM_KEY}" in text


def test_a_name_with_markdown_characters_cannot_break_the_report() -> None:
    """Demon antama merkkijono ei saa muuttua rakenteeksi (P1).

    CS2:n nimissä esiintyy kaikkia Markdownin rakennemerkkejä. Escapetus
    tehdään esityshetkellä, ei datassa: ``report.json`` säilyttää havainnon
    sellaisenaan.
    """
    entry = report(
        [pistol_map()],
        display_name="*|LOL|*",
        display_name_source="clan_name",
        name_alternatives=["<b>hax</b>"],
        roster=[RosterEntry(player_id="1", display_name="a_b  c" + chr(10) + "d")],
    )
    text = render(entry)

    assert text.startswith("# " + chr(92) + "*" + chr(92) + "|LOL" + chr(92) + "|" + chr(92) + "*")
    assert chr(92) + "<b" + chr(92) + ">hax" in text
    # Rivinvaihto ja peräkkäiset välilyönnit siivotaan näkyvästi. Nimi on
    # yhteenvedossa yksinään ja jäljitettävyysluvussa tunnisteensa nimiönä
    # järjestysluvun jälkeen, joten sama escapetus on todennettava molemmista.
    assert "a" + chr(92) + "_b c d" in summary_text(text)
    assert "**1. a" + chr(92) + "_b c d:** `1`" in traceability_text(text)
    # Malli itse säilyttää havainnon sellaisenaan.
    assert entry.team.display_name == "*|LOL|*"


def test_a_known_team_name_is_used_in_the_title() -> None:
    """Nimi otsikkoon ja yhteenvetoon **ilman tunnistetta** (Story 2.12)."""
    text = render(report([pistol_map()]))

    assert text.startswith(f"# {TEAM_NAME} -- scouting-raportti")
    assert f"- **Joukkue:** {TEAM_NAME}" in summary_text(text)
    assert TEAM_KEY not in summary_text(text)
    assert f"**Joukkueen tunniste:** `{TEAM_KEY}`" in traceability_text(text)


def test_the_roster_speaks_names_and_the_chapter_carries_the_ids() -> None:
    """Yhteenveto nimillä, jäljitettävyysluku pareilla nimi -> SteamID64.

    Story 2.6 kirjoitti molemmat samalle riville. Peruste ei muuttunut
    vääräksi -- tunniste on yhä ainoa jäljitettävä arvo -- mutta paikka
    muuttui: seitsemän 17-numeroista lukua nimien rinnalla tekee rungon
    rivistä luettelon, jota ottelua edeltävässä kiireessä ei lueta.
    """
    text = render(report([pistol_map()], roster=STEAM_ROSTER))
    summary = summary_text(text)
    traceability = traceability_text(text)

    expected = f"- **Rosteri:** {len(STEAM_ROSTER)} pelaajaa "
    assert expected + "(havaittu demoista): " in summary
    for index, player in enumerate(STEAM_ROSTER, start=1):
        assert player.display_name in summary
        assert player.player_id not in summary
        assert (
            f"**{index}. {player.display_name}:** `{player.player_id}`"
            in traceability
        )


def test_a_player_without_a_name_keeps_the_row_and_says_the_name_is_missing() -> None:
    """Nimetön pelaaja ei katoa: lukumäärä täsmää ja tunniste on omassa luvussaan.

    Kolme väitettä yhdessä, koska ne ovat sama sääntö: hiljaa pudotettu
    pelaaja kutistaisi rosterin, nimetön paikka rivillä tekisi hänestä
    nimettömän vahingossa, ja ilman jäljitettävyysluvun riviä hänen
    tunnisteensa katoaisi raportista kokonaan.
    """
    named, nameless = STEAM_ROSTER[0], STEAM_ROSTER[1]
    entry = report(
        [pistol_map()],
        roster=[named, RosterEntry(player_id=nameless.player_id)],
    )
    text = render(entry)
    summary = summary_text(text)

    assert "- **Rosteri:** 2 pelaajaa (havaittu demoista): " in summary
    assert f"{named.display_name}, {UNNAMED_PLAYER}" in summary
    assert nameless.player_id not in summary
    assert (
        f"**2. {UNNAMED_PLAYER}:** `{nameless.player_id}`"
        in traceability_text(text)
    )


def test_conflicting_team_names_are_listed_instead_of_disappearing() -> None:
    """Useimmin havaittu otsikkoon, muut lueteltuina -- ristiriita ei katoa."""
    entry = report([pistol_map()], name_alternatives=["MM Academy"])
    text = render(entry)
    assert text.startswith(f"# {TEAM_NAME} -- scouting-raportti")
    assert "Muut havaitut nimet" in text
    assert "MM Academy" in text


def test_thresholds_are_listed_once_and_without_raw_dictionaries() -> None:
    """Kynnykset ovat alkuperätieto; aaltosulkeet ja toisto vievät tilaa turhaan."""
    entry = report(
        [pistol_map()],
        classify_thresholds={"full_equip_min": 4000, "armed_players_min": 3},
        thresholds_used={
            "thresholds": {"full_equip_min": 4000, "small_sample_rounds": 3},
            "aggregate": {"utility_seconds_buckets": [5.0, 10.0]},
        },
    )
    text = render(entry)
    summary = text.split("## Yhteenveto")[1].split("##")[0]
    assert "{" not in summary
    assert "}" not in summary
    assert summary.count("full_equip_min") == 1
    assert "small_sample_rounds 3" in summary
    assert "utility_seconds_buckets 5/10" in summary


def test_a_threshold_that_differs_between_the_two_records_is_shown_twice() -> None:
    """Identtinen pari pudotetaan, eroava ei: juuri ero on se, joka kertoo jotain."""
    entry = report(
        [pistol_map()],
        classify_thresholds={"full_equip_min": 4000},
        thresholds_used={"thresholds": {"full_equip_min": 3000}},
    )
    summary = render(entry).split("## Yhteenveto")[1].split("##")[0]
    assert "full_equip_min 4000" in summary
    assert "full_equip_min 3000" in summary


def test_a_naive_timestamp_is_not_labelled_utc() -> None:
    entry = report([pistol_map()], generated_at=datetime(2026, 8, 30, 12, 0))
    text = render(entry)
    assert "2026-08-30 12:00 (aikavyöhyke tuntematon)" in text
    assert "12:00 UTC" not in text


def test_an_offset_timestamp_is_converted_to_utc_not_relabelled() -> None:
    helsinki = timezone(timedelta(hours=3))
    entry = report(
        [pistol_map()], generated_at=datetime(2026, 8, 30, 12, 0, tzinfo=helsinki)
    )
    assert "2026-08-30 09:00 UTC" in render(entry)


# --- Rajaukset ------------------------------------------------------------------


def test_report_contains_no_interpretation() -> None:
    """Ei "fake", ei "rush", ei vastastrategiaa -- vain havainnot."""
    text = render(report([pistol_map(), default_map()])).lower()
    for word in ("fake", "rush", "antisträt", "kannattaa", "suositel"):
        assert word not in text, word


def test_normal_buying_is_never_explained_by_the_previous_round() -> None:
    """R-1: normaalia ostoa ei perustella edellisen kierroksen voitolla."""
    text = render(report([default_map()])).lower()
    for word in ("voitti", "edellisen kierroksen", "hävisi"):
        assert word not in text, word


def test_no_html_and_no_images() -> None:
    text = render(report([pistol_map()]))
    assert "<" not in text
    assert "![" not in text


# --- Kierrosliite ---------------------------------------------------------------


def test_round_appendix_lists_the_paths_the_stage_resolved() -> None:
    """Kierroskohtaisia rivejä ei ole report.jsonissa; render ei laske niitä."""
    text = render(report([pistol_map()]))
    assert "## Kierrosliite" in text
    assert "eivät ole report.jsonissa" in text
    assert ROUND_LISTS[0] in text


def test_round_appendix_without_paths_does_not_dangle_a_colon() -> None:
    """Tyhjä luettelo kaksoispisteen jälkeen lukisi kuin lista olisi kadonnut."""
    text = render_report(report([pistol_map()]))
    appendix = text.split("## Kierrosliite")[1].split("## Lukuohje")[0].strip()
    assert not appendix.endswith(":")
    assert "polkuja ei annettu" in appendix


def test_the_view_names_the_demos_but_not_the_paths() -> None:
    """Polun rakentaminen kuuluu vaiheelle, joka näkee arkiston."""
    assert round_list_demo_ids(report([pistol_map(), default_map()])) == [
        DEMO_ID,
        "inferno_vs_ryhmarama",
    ]


# --- Tekninen jäljitettävyys (Story 2.12) ---------------------------------------

#: Neljä kokoonpanotunnistetta, jotka liitettiin samaksi joukkueeksi.
#:
#: Muoto on mitattu RCAVE-raportista 31.8. (16 merkin tiiviste, neljä
#: kokoonpanoa yhdellä joukkueella); arvot ovat keksittyjä samasta syystä kuin
#: :data:`STEAM_ROSTER`in. **Viimeinen on tarkoituksella** :data:`TEAM_KEY`:
#: ``lineups_of_same_team`` palauttaa kohteen aina mukana, joten oikeassa
#: raportissa joukkueen oma kokoonpano on listalla -- ja juuri se tekee
#: rungon lukumäärästä helposti yhden liian suuren.
LINEUP_KEYS = ["0f1e2d3c4b5a6978", "1a2b3c4d5e6f7081", "2b3c4d5e6f708192", TEAM_KEY]


def crowded_report() -> Report:
    """Raportti, jossa on **jokainen** tunnisteen lähde ja jokainen poikkeus.

    Neljä liitettyä kokoonpanoa, seitsemän pelaajaa SteamID64-muotoisilla
    tunnisteilla, kaksi nimettyä karttaa, yksi kartta jonka nimeä ei
    tunnistettu, ja yksi puuttuva demo. Ilman jokaista näistä puhtausväite
    olisi tosi vain siitä, mitä fikstuuri sattuu sisältämään.
    """
    return report(
        [pistol_map(), default_map(), unknown_map()],
        roster=STEAM_ROSTER,
        lineup_keys=LINEUP_KEYS,
        missing_demos=[missing_demo()],
    )


def literal_identifiers(entry: Report) -> list[str]:
    """Fikstuurin tunnisteet **nimeltä**, niin kuin ne raportissa esiintyisivät.

    Hahmo (:data:`IDENTIFIER_SHAPE`) ei tunnista demotunnisteita eikä voi:
    ``ANCIENT_vs_RCAVE_VETERANS`` ei muistuta tiivistettä. Ne on siis
    lueteltava, ja luettelo johdetaan raportista eikä kirjoiteta käsin --
    käsin kirjoitettu jäisi jälkeen heti kun fikstuuri saa uuden demon.
    """
    names = [entry.team.key, *entry.team.lineup_keys]
    names += [row.player_id for row in entry.team.roster]
    for map_report in entry.maps:
        names += map_report.map_demo_ids
    names += [missing.match for missing in entry.missing_demos]
    return sorted(set(names))


def test_no_identifier_appears_in_the_body_outside_the_three_exceptions() -> None:
    """Hyväksymiskriteeri kokonaisena, poikkeukset mukaan luettuina.

    Väite tehdään **luvuittain** eikä yhtenä merkkijonona, ja jokainen luku
    joko tarkistetaan tai vapautetaan nimetyllä säännöllä. Aiempi versio tästä
    testistä leikkasi kierrosliitteen pois ennen tarkistusta, eli se ei voinut
    nähdä vuotoa juuri siitä paikasta, jossa tunnisteita yhä on -- ja sen
    sääntö oli siksi näkymätön.

    Kolme poikkeusta, ja kumpi sääntö vapauttaa minkä:

    1. ``Kierrosliite`` -- tunniste on **polussa**, ja polku on koodijakso.
       Poikkeus on siis kapeampi kuin luku: koodijaksojen ulkopuolella
       kierrosliite tarkistetaan kuten mikä tahansa runko.
    2. ``Puuttuvat demot`` -- tunniste on osa komentoa, jonka lukija kopioi,
       eikä komento toimi ilman sitä. Koko luku vapautuu.
    3. Tunnistamattoman kartan **otsikko** -- silloin ``map_name`` *on*
       ``map_demo_id``, eli tunniste on kartan ainoa nimi. Vain otsikko
       vapautuu; luvun sisällön on oltava puhdas.
    """
    entry = crowded_report()
    text = render(entry)
    view = build_view(entry, round_list_paths=ROUND_LISTS)
    unnamed_headings = {m.heading for m in view.maps if m.name_unknown}
    literals = literal_identifiers(entry)

    def assert_clean(what: str, where: str) -> None:
        assert IDENTIFIER_SHAPE.search(what) is None, (
            where,
            IDENTIFIER_SHAPE.findall(what),
        )
        for literal in literals:
            assert literal not in what, (where, literal)

    checked_headings = 0
    for heading, content in report_sections(text):
        if heading not in unnamed_headings:  # poikkeus 3 koskee vain otsikkoa
            assert_clean(heading, f"otsikko {heading!r}")
            checked_headings += 1
        if heading in ("Puuttuvat demot", TRACEABILITY_HEADING):
            continue  # poikkeus 2, ja luku joka on tunnisteita varten
        if heading == "Kierrosliite":
            content = CODE_SPAN.sub("", content)  # poikkeus 1 on kapea
        assert_clean(content, f"luku {heading!r}")

    # Fikstuuri kattaa poikkeuksen 3 -- ilman tätä silmukka olisi voinut
    # ohittaa sen kertaakaan kohtaamatta.
    assert len(unnamed_headings) == 1
    # Tarkka luku eikä alaraja: silmukka, joka ei tarkista mitään, menisi
    # alarajalla läpi. Kymmenen lukua, joista tunnistamattoman kartan otsikko
    # on vapautettu.
    assert checked_headings == 9


def test_every_identifier_the_body_dropped_is_in_the_chapter() -> None:
    """Poistaminen on **siirto**: mikään ei putoa matkalla.

    Puuttuvan demon tunniste ei ole luvussa eikä kuulukaan: se ei ole
    yhdenkään karttahaaran demo vaan demo, joka jäi otannan ulkopuolelle, ja
    se on rungossa komentonsa kanssa. Väite luettelee siis tarkalleen ne
    lähteet, jotka rungosta poistuivat.
    """
    entry = crowded_report()
    traceability = traceability_text(render(entry))

    moved = [entry.team.key, *entry.team.lineup_keys]
    moved += [row.player_id for row in entry.team.roster]
    for map_report in entry.maps:
        moved += map_report.map_demo_ids
    for identifier in moved:
        assert identifier in traceability, identifier
    assert missing_demo().match not in traceability


def test_the_exceptions_are_not_vacuous() -> None:
    """Kolme poikkeusta ovat todellisia, eivät varmuuden vuoksi kirjoitettuja.

    Ilman tätä puhtaustesti voisi mennä läpi siksi, ettei vapautetuissa
    luvuissa ole yhtään tunnistetta -- ja lukuohjeen lause poikkeuksista olisi
    väärä toiseen suuntaan.
    """
    entry = crowded_report()
    text = render(entry)

    paths = section_text(text, "Kierrosliite")
    assert TEAM_KEY in paths
    assert IDENTIFIER_SHAPE.search(CODE_SPAN.sub("", paths)) is None

    missing = section_text(text, "Puuttuvat demot")
    assert missing_demo().match in missing
    assert f"uv run pappascout parse {missing_demo().match}" in missing

    headings = [heading for heading, _ in report_sections(text)]
    assert FACEIT_DEMO_ID in " ".join(headings)


def test_the_legend_names_all_three_exceptions() -> None:
    """Raportti ei saa väittää itsestään enemmän kuin on totta.

    Lukuohje on se paikka, jossa raportti kertoo omat sääntönsä. Jos se sanoo
    "runko puhuu vain nimillä", kolme lukua sen yläpuolella tekevät lauseesta
    valheen.
    """
    legend = section_text(render(crowded_report()), "Lukuohje")

    assert TRACEABILITY_HEADING in legend
    assert "kierrosliitteen polut" in legend
    assert "puuttuvan demon rivi" in legend
    assert "nimeä ei tunnistettu" in legend
    assert "vain nimillä" not in legend


def test_the_chapter_note_separates_identifiers_from_thresholds() -> None:
    """Kynnys ei ole tunniste, ja luvun selitys kertoo miksi.

    Kynnykset täyttävät kirjaimellisesti saman kriteerin kuin tunnisteet
    (kone tarvitsee, ihminen ei), mutta ero on aito: kynnys kertoo **miten
    luku laskettiin**, joten väitettä ei voi arvioida ilman sitä. Ilman tätä
    lausetta seuraava lukija siirtäisi nekin.
    """
    traceability = traceability_text(render(report([pistol_map()])))

    assert _TRACEABILITY_NOTE in traceability
    assert "Kynnykset" in _TRACEABILITY_NOTE
    assert "miten luku laskettiin" in _TRACEABILITY_NOTE


def test_the_traceability_chapter_is_the_last_one() -> None:
    """Tunnisteet lakkaavat olemasta ensimmäinen asia, jonka lukija näkee.

    Luku on viimeisenä eikä missä tahansa: lukuohje kertoo mistä tunnisteet
    löytyvät, ja lukua ennen sitä ei voi olla -- silloin tunnisteet olisivat
    taas rungon välissä.
    """
    headings = [heading for heading, _ in report_sections(render(crowded_report()))]

    assert headings[-3:] == ["Kierrosliite", "Lukuohje", TRACEABILITY_HEADING]


def test_the_chapter_name_is_the_same_in_the_template_and_in_the_code() -> None:
    """Otsikon omistaa malli, mutta raportin oma teksti viittaa siihen.

    Ilman tätä väitettä luvun uudelleennimeäminen jättäisi yhteenvetoon ja
    lukuohjeeseen kaksi viittausta lukuun, jota ei ole -- eikä mikään testi
    huomaisi, koska molemmat viittaukset lukevat saman vakion.
    """
    assert "## " + TRACEABILITY_HEADING in template_text()


def test_the_roster_rows_are_in_the_same_order_as_the_names_in_the_body() -> None:
    """Järjestyslupaus, jonka kaksi docstringia antavat, on tarkistettavissa.

    Rivin nimiö on ``<järjestysluku>. <sama merkkijono kuin rungossa>``, joten
    lukija löytää pelaajansa laskemalla. Ilman tätä testiä lupaus olisi
    pelkkä lause: silmukoiden järjestyksen vaihtaminen ei kaataisi mitään.
    """
    roster = [
        RosterEntry(player_id="76561190000000101", display_name="cee"),
        RosterEntry(player_id="76561190000000102"),
        RosterEntry(player_id="76561190000000103", display_name="aaa"),
    ]
    text = render(report([pistol_map()], roster=roster))

    roster_row = next(
        line
        for line in summary_text(text).splitlines()
        if line.startswith("- **Rosteri:**")
    )
    listed = roster_row.split("(havaittu demoista): ")[1].split(", ")
    labels = [
        line.split("**")[1].rstrip(":")
        for line in traceability_text(text).splitlines()
        if line.startswith("- **") and line[4].isdigit()
    ]

    assert listed == ["cee", UNNAMED_PLAYER, "aaa"]
    assert labels == [f"{n}. {name}" for n, name in enumerate(listed, start=1)]


def test_two_players_without_a_name_get_two_distinguishable_rows() -> None:
    """Nimi ei ole yksikäsitteinen avain, joten nimiö ei voi olla pelkkä nimi.

    Kaksi nimetöntä pelaajaa tuottaisivat ilman järjestyslukua kaksi
    identtistä riviä, ja lukija ei voisi sanoa kumpi SteamID64 on kumman.
    Sama koskee kahta samannimistä, mikä on CS2:ssa tavallista.
    """
    roster = [
        RosterEntry(player_id="76561190000000201"),
        RosterEntry(player_id="76561190000000202"),
        RosterEntry(player_id="76561190000000203", display_name="kaksoset"),
        RosterEntry(player_id="76561190000000204", display_name="kaksoset"),
    ]
    traceability = traceability_text(render(report([pistol_map()], roster=roster)))

    for index, player in enumerate(roster, start=1):
        name = player.display_name or UNNAMED_PLAYER
        assert f"- **{index}. {name}:** `{player.player_id}`" in traceability
    rows = [line for line in traceability.splitlines() if line.startswith("- **")]
    assert len(rows) == len(set(rows))


def test_several_lineups_are_a_checkable_count_with_its_threshold() -> None:
    """I/O-matriisi: neljä ``lineup_keys``.

    Rivin luku on **tarkistettavissa**: ``lineup_keys`` sisältää kohteen oman
    kokoonpanon, joten pelkkä lukumäärä lukisi kuin liitettyjä olisi yksi
    enemmän kuin oli. Rivi kertoo liitettyjen määrän ja kokonaismäärän, ja
    kynnyksen jolla päätös tehtiin -- kuten naapuririvi kertoo omansa.
    """
    entry = report([pistol_map()], lineup_keys=LINEUP_KEYS)
    text = render(entry)
    summary = summary_text(text)
    traceability = traceability_text(text)

    assert (
        "- **Kokoonpanot:** 3 muuta kokoonpanoa liitetty samaksi joukkueeksi "
        f"vähintään {MIN_COMMON} yhteisen pelaajan perusteella; yhteensä 4 "
        f"kokoonpanoa, tunnisteet luvussa {TRACEABILITY_HEADING}" in summary
    )
    for key in LINEUP_KEYS:
        assert key not in summary
        assert "`" + key + "`" in traceability
    assert "**Kokoonpanotunnisteet:**" in traceability


def test_the_lineup_row_says_the_rule_in_words_when_the_threshold_is_absent(
) -> None:
    """Puuttuva kynnys ei saa saada renderöintiä keksimään lukua.

    Sama sääntö kuin kuvion rajalla: arvo luetaan raportista, ja jos sitä ei
    ole, rivi kertoo perusteen sanoina. Kovakoodattu kynnys olisi laskentaa
    väärässä kerroksessa.
    """
    entry = report(
        [pistol_map()], lineup_keys=LINEUP_KEYS, thresholds_used={"thresholds": {}}
    )
    summary = summary_text(render(entry))

    assert (
        "- **Kokoonpanot:** 3 muuta kokoonpanoa liitetty samaksi joukkueeksi "
        "yhteisten pelaajien perusteella; yhteensä 4 kokoonpanoa, tunnisteet "
        f"luvussa {TRACEABILITY_HEADING}" in summary
    )


def test_a_single_lineup_is_the_team_key_and_is_not_printed_twice() -> None:
    """I/O-matriisi: yksi ``lineup_key``.

    Rungossa ei ole riviä lainkaan -- yhden kokoonpanon "liittäminen samaksi
    joukkueeksi" ei ole havainto. Jäljitettävyysluvussa ei ole omaa riviä
    myöskään, mutta eri syystä kuin aiemmin luultiin: ``team.key`` **on** se
    kokoonpanotunniste, joten oma rivi toistaisi joukkuerivin sanasta sanaan.
    Tunniste ei siis katoa, ja joukkuerivi sanoo olevansa molempia.
    """
    text = render(report([pistol_map()]))
    summary = summary_text(text)
    traceability = traceability_text(text)

    assert "- **Kokoonpanot:**" not in summary
    # Rivin ETULIITE eikä pelkkä sana: joukkuerivin oma teksti mainitsee
    # kokoonpanotunnisteen pienellä, ja pelkkä sanahaku menisi läpi
    # vahingossa -- tai kaatuisi jos lause kirjoitettaisiin isolla.
    assert "- **Kokoonpanotunniste" not in traceability
    assert (
        f"- **Joukkueen tunniste:** `{TEAM_KEY}` -- sama arvo kuin joukkueen "
        "ainoa kokoonpanotunniste" in traceability
    )
    assert traceability.count(TEAM_KEY) == 1


def test_an_empty_roster_says_the_source_was_empty_and_lists_nobody() -> None:
    """I/O-matriisi: tyhjä ``roster``.

    Runko kertoo lähteen olevan tyhjä kuten ennenkin, ja jäljitettävyysluvussa
    ei ole yhtään rosteririviä -- tyhjä pari nimi -> tunniste olisi keksitty
    rivi.
    """
    entry = report([pistol_map()], roster=[])
    text = render(entry)

    assert (
        "- **Rosteri:** ei pelaajia (havaittu demoista -- lähde tyhjä)"
        in summary_text(text)
    )
    traceability = traceability_text(text)
    assert f"`{TEAM_KEY}`" in traceability
    assert UNNAMED_PLAYER not in traceability


def test_a_map_shows_only_the_demo_count_and_the_chapter_names_the_demos() -> None:
    """I/O-matriisi: ``map_demo_ids`` per kartta.

    Kartan otsikko kertoo demojen **määrän**; se, mitkä demot summautuivat
    yhdeksi haaraksi, on jäljitettävyyskysymys ja siksi omassa luvussaan.
    """
    demos = [MISSING_DEMO_ID, FACEIT_DEMO_ID]
    text = render(report([demo_map(demos, name="de_ancient")]))
    traceability = traceability_text(text)

    assert "de_ancient -- 2 kierrosta, 2 demoa" in text
    for demo_id in demos:
        assert demo_id not in summary_text(text)
        assert "`" + demo_id + "`" in traceability
    assert "- **`de_ancient`:** " in traceability


def test_a_map_whose_name_was_not_recognised_gets_a_label_that_says_so() -> None:
    """I/O-matriisi: ``map_name_source`` on ``unknown``.

    Silloin ``map_name`` on ``map_demo_id`` itse, joten nimi ei kelpaa
    nimiöksi: rivi olisi ``- **<tunniste>:** `<sama tunniste>``` eli se ei
    kertoisi mitään. Nimiö sanoo sen sijaan mistä on kyse, ja järjestysluku
    kertoo minkä karttaluvun rivi koskee.
    """
    entry = report([pistol_map(), unknown_map()])
    traceability = traceability_text(render(entry))

    assert f"- **kartta 2, nimeä ei tunnistettu:** `{FACEIT_DEMO_ID}`" in traceability
    assert f"**`{FACEIT_DEMO_ID}`:**" not in traceability


def test_two_unrecognised_maps_do_not_get_the_same_label() -> None:
    """Nimiö on nimiö vain jos se yksilöi rivinsä.

    Ilman järjestyslukua kaksi tunnistamatonta karttaa tuottaisivat kaksi
    riviä samalla nimiöllä, eli sama vika kuin kahdella nimettömällä
    pelaajalla.
    """
    other = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"
    entry = report([unknown_map(), unknown_map(other)])
    traceability = traceability_text(render(entry))

    assert f"- **kartta 1, nimeä ei tunnistettu:** `{FACEIT_DEMO_ID}`" in traceability
    assert f"- **kartta 2, nimeä ei tunnistettu:** `{other}`" in traceability


def test_a_map_name_with_markdown_characters_keeps_the_row_a_single_pair() -> None:
    """Aito vikatapaus: workshop-kartta rikkoi rivin.

    Story 2.11 päätti, ettei otsikosta luettua nimeä validoida karttapoolia
    vasten, joten ``*|Aim|* Botz [beta]`` on laillinen havainto. Paljaana
    nimiön lihavointi jäi sulkeutumatta ja rivi lakkasi lukeutumasta parina
    label/value -- ja juuri se rivi kantaa demotunnisteet. Koodijakso pitää
    rivin yhtenä parina **millä tahansa** merkkijonolla, ilman että kartta saa
    toisen kirjoitusasun kuin karttaluvun otsikossa.
    """
    name = "*|Aim|* Botz [beta]"
    entry = report([demo_map([MISSING_DEMO_ID], name=name)])
    traceability = traceability_text(render(entry))

    assert f"- **`{name}`:** `{MISSING_DEMO_ID}`" in traceability
    # Sama kirjoitusasu kuin karttaluvun otsikossa: escapetus tuottaisi
    # toisen, ja raportti luetaan myös raakana.
    assert f"## {name} -- " in render(entry)
    row = next(
        line for line in traceability.splitlines() if line.startswith("- **")
    )
    assert row.count("**") == 2


def test_a_demo_id_is_a_code_span_so_it_stays_usable() -> None:
    """Tunnisteen arvo on se, että sen voi kopioida raportista sellaisenaan.

    :func:`markdown_text` suojaisi alaviivat mutta tekisi arvosta eri
    merkkijonon, joka ei enää täsmää yhteenkään arkiston hakemistoon.
    """
    traceability = traceability_text(render(report([demo_map([MISSING_DEMO_ID])])))

    assert "`" + MISSING_DEMO_ID + "`" in traceability
    assert chr(92) + "_" not in traceability


def test_a_backtick_in_an_identifier_falls_back_to_escaping() -> None:
    """Gravis on ainoa merkki, jota koodijakso ei voi sisältää.

    Rikkinäinen koodijakso latoisi loppuraportin väärin, mikä on pahempi kuin
    kopioitavuuden menetys yhdellä rivillä. Windowsissa gravis on laillinen
    tiedostonimessä eli mahdollinen demotunnisteessa.
    """
    demo_id = "demo" + chr(96) + "vs" + chr(96) + "toinen"
    traceability = traceability_text(render(report([demo_map([demo_id])])))

    assert (
        "demo" + chr(92) + chr(96) + "vs" + chr(92) + chr(96) + "toinen"
        in traceability
    )
    assert chr(96) + "demo" not in traceability


def test_an_empty_report_still_gets_the_traceability_chapter() -> None:
    """Joukkueella on tunniste myös silloin, kun karttoja ei ole.

    Tyhjä raportti on juuri se tapaus, jossa lukija kysyy "mistä joukkueesta
    tässä oli kyse" -- ja tunniste on ainoa vastaus, joka siihen on. Malli
    latoo luvun ehdoitta, joten tyhjä jono tuottaisi paljaan otsikon; sitä ei
    vartioida, koska :func:`build_view` ei voi tuottaa sitä.
    """
    view = build_view(report([]))

    assert view.traceability
    assert view.traceability[0].label == "Joukkueen tunniste"
    assert f"`{TEAM_KEY}`" in traceability_text(render(report([])))


# --- Rakenteen kattavuus --------------------------------------------------------


def test_every_round_type_has_a_place_in_the_report() -> None:
    """Uusi kierrostyyppi ei voi kadota raportista hiljaa."""
    assert set(ROUND_TYPE_ORDER) == set(ROUND_TYPES)
    assert PATTERN_ROUND_TYPES <= set(ROUND_TYPES)


def test_pistol_and_saving_rounds_come_before_default() -> None:
    """Järjestys on spec-2-4:n rakenne: pistooli, säästöt, default."""
    order = list(ROUND_TYPE_ORDER)
    assert order.index("pistol") == 0
    for saving in ("eco", "force", "half"):
        assert order.index(saving) < order.index("full")


@pytest.mark.parametrize(
    "raw,expected",
    [("eco", "Eco"), ("OT", "OT"), ("HE", "HE"), ("puoliosto", "Puoliosto"), ("", "")],
)
def test_capitalising_a_heading_leaves_the_other_letters_alone(
    raw: str, expected: str
) -> None:
    """``str.capitalize`` muuttaisi lyhenteen "OT" muotoon "Ot".

    Kierrostyyppien suomennokset ovat tavallisia sanoja tänään, mutta luettelo
    on ``constants``issa eikä täällä -- sääntö ei saa nojata siihen, mitä
    siellä nyt sattuu olemaan.
    """
    from pappascout.render.view import _capitalise

    assert _capitalise(raw) == expected


def test_a_round_type_heading_uses_the_finnish_name_capitalised() -> None:
    entry = map_report("de_nuke", [side("T", [round_type("ot", 4)])])
    text = render(report([entry]))
    assert "**Jatkoaika** (4 kierrosta)" in text


def test_view_is_built_without_touching_the_report() -> None:
    """``Report`` on jäädytetty sopimus; näkymä ei saa korjailla sitä."""
    entry = report([pistol_map()])
    before = entry.model_dump_json()
    build_view(entry, round_list_paths=ROUND_LISTS)
    assert entry.model_dump_json() == before


# --- Malli ----------------------------------------------------------------------


def test_template_digest_is_the_sha256_of_the_template_itself() -> None:
    """Tiivisteen on oltava sidottu mallin sisältöön, ei mihin tahansa arvoon.

    Ilman tätä väitettä tiivisteen voisi korvata vakiolla ja molemmat sitä
    koskevat testit menisivät läpi -- jolloin mallin muokkaus tuottaisi eri
    raportin muuttumattomalla ``params_hash``illa, eli täsmälleen sen tilan,
    jonka tiiviste on lisätty estämään.
    """
    expected = hashlib.sha256(template_text().encode("utf-8")).hexdigest()
    assert template_digest() == expected
    assert re.fullmatch(r"[0-9a-f]{64}", template_digest())


def test_editing_the_template_changes_both_the_digest_and_the_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Tiiviste ja renderöinti lukevat saman tiedoston -- eivät eri välimuisteja."""
    import pappascout.render as render_pkg

    original = template_text()
    edited = tmp_path / "muokattu.md.j2"
    edited.write_text(original + "\nLISÄRIVI\n", encoding="utf-8")

    before_digest = template_digest()
    before_text = render(report([pistol_map()]))

    monkeypatch.setattr(render_pkg, "template_path", lambda: edited)
    assert template_digest() != before_digest
    assert render(report([pistol_map()])) != before_text
    assert "LISÄRIVI" in render(report([pistol_map()]))


def test_a_broken_template_is_a_finnish_error_not_a_jinja_traceback(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Jinjan ``TemplateError`` ei periydy ``PappascoutError``ista."""
    from pappascout.errors import PappascoutError

    import pappascout.render as render_pkg

    broken = tmp_path / "rikki.md.j2"
    broken.write_text("{% for x in %}", encoding="utf-8")
    monkeypatch.setattr(render_pkg, "template_path", lambda: broken)

    with pytest.raises(PappascoutError, match="Raporttimallin"):
        render(report([pistol_map()]))


# --- Koko dokumentin muoto ------------------------------------------------------

#: Golden-tuloste pienestä raportista.
#:
#: Osamerkkijonoväitteet eivät näe dokumentin muotoa: tyhjiä rivejä, otsikoiden
#: välejä eikä sitä, mitä ``render._tidy`` tekee mallin tuotokselle. Muoto
#: syntyy kahdessa paikassa (malli ja jälkikäsittely), joten se lukitaan
#: kokonaisena. Testin kaatuminen tarkoittaa, että raportin ulkoasu muuttui --
#: se on joskus oikein, ja silloin tämä teksti päivitetään.
GOLDEN = """\
# MatureMayhem -- scouting-raportti

## Yhteenveto

- **Joukkue:** MatureMayhem
- **Rosteri:** 5 pelaajaa (havaittu demoista): pelaaja1, pelaaja2, pelaaja3, pelaaja4, pelaaja5
- **Otanta:** 1 demo, 4 kierrosta (demoa/kierrosta: liiga 0 / 0, muut 0 / 0, tuntematon 1 / 4)
- **Liigatieto:** yhdenkään demon lajia ei ole vahvistettu: kaikki ovat lokerossa tuntematon, eikä otannassa ole yhtään varmistettua liigaottelua
- **Pieni otanta:** alle 3 kierrosta merkitään (pieni otanta); havaintoa ei silti piiloteta
- **Luokittelun kynnykset:** full_equip_min 4000
- **Aggregoinnin kynnykset:** advance_area_min_observations 20, advance_max_sample_s 30, advance_min_players 1, advance_t_share 0,8, crunch_min_players 2, crunch_min_sources 2, small_sample_rounds 3, team_identity_min_common 3
- **Aineisto koottu:** 2026-08-30 12:00 UTC (pappascout 0.1.0)

## Poikkeamat

- CT-eteneminen (de_nuke, CT-puoli, eco): Lobby (1/4 kierroksesta, T-osuus 0,89 alueen 64 havainnosta)
  - kierros 23: 2 pelaajaa 30 s kohdalla

## de_nuke -- 4 kierrosta, 1 demo

### T-puoli -- 4 kierrosta

**Eco** (4 kierrosta)
- 6 s: Ramp 2 (1/4 kierroksesta)
- ensikontakti (mediaani 9,1 s): Ramp 1 (2/4 kierroksesta)
- utility: savu 1 kpl (1/4 kierroksesta)
- aseistettuja ostoajan lopussa: 0 (4/4 kierroksesta)
- panssaroituja ostoajan lopussa: 0 (3/4 kierroksesta), 5 (1/4 kierroksesta)
- ensimmäinen kuolema (mediaani 24,0 s): Cave (2/3 kierroksesta), Long (1/3 kierroksesta) -- ei omia kuolemia 1 kierroksella
- tapot alueittain: Middle (4/6 taposta), BombsiteB (2/6 taposta)

## Kierrosliite

Kierros, tyyppi ja perustelu eivät ole report.jsonissa: se sisältää reunajakaumia, ei kierroskohtaisia rivejä. Liite on classify-vaiheen kierroslistassa, jossa jokaisella kierroksella on päätös ja sen lähtöarvot:

- `C:\\arkisto\\classified\\aaaaaaaaaaaaaaaa\\Ancient_vs_kaljukostaja.md`

## Lukuohje

- Jokainen väite kantaa otantansa muodossa (n/m kierroksesta): n on kierrokset, joissa havainto tehtiin, m kyseisen kierrostyypin kaikki kierrokset.
- Ensikontaktin rivi kertoo elossa olevat pelaajat alueittain sillä hetkellä, kun kierroksen ensimmäinen ristiinpuolinen osuma tapahtui.
- Luvun Poikkeamat T-osuus on **demon oma havainto** siitä, kumman puolen aluetta alue on: se on alueen elossa-havainnoista aikanäytepisteillä laskettu T-puolen osuus, **molempien joukkueiden** riveistä. Ei karttatietokantaa eikä käsin annettua aluejakoa -- ja eri demo voi antaa samalle alueelle eri osuuden, joten havaintomäärä on osuuden vieressä. Alue on T:n aluetta, kun osuus on vähintään 0,80 ja alueella on vähintään 20 havaintoa; sitä vähemmällä alue ei ole kummankaan puolen aluetta eikä tuota poikkeamaa.
- **CT-eteneminen**: subjektin CT-pelaaja alueella, joka on siinä demossa T:n hallussa, **säästökierroksella** (eco, force tai puoliosto). Vähintään 1 pelaaja alueella ja havainto enintään 30 sekunnin kohdalla kierroksen alusta.
- **Crunch**: sama T:n alue, mutta pelaajien on **saavuttava** sinne yhtä aikaa eri suunnista -- lähtösuunta on pelaajan oma alue edellisellä näytepisteellä. Vähintään 2 pelaajaa ja 2 eri suuntaa. **Crunchia ei ole rajattu kierrostyyppiin**, toisin kuin etenemistä, joten sen otanta on puolen kaikki kierrokset ja nimiö kertoo millä kierrostyypeillä se havaittiin. Sama kierros voi siis tuottaa molemmat rivit, ja täysi osto vain crunchin.
- Aseistettu = panssari JA parannettu ase ostoajan lopussa; panssaroitu = panssari, aseesta riippumatta. Luvut ovat **sisäkkäisiä**: aseistetut ovat panssaroitujen osajoukko, molemmat on luettu samalta tickiltä samasta pelaajajoukosta, ja jakaja on sama. Rivien ero on siis se havainto -- pistoolikierroksella aseistettuja on tyypillisesti 0 (800 $ ei riitä sekä kevlariin että parannettuun aseeseen), joten panssaririvi on se, joka kertoo kevlarien määrän.
- Molemmat luvut ovat **hallussapitoa eivätkä ostoja**: panssari ja ase säilyvät kierroksen yli hengissä selvinneellä, eikä vaurioitunutta panssaria eroteta ehjästä. Poikkeus on pistoolikierros -- puoliaika alkaa puhtaalta pöydältä, joten siellä luvut kertovat mitä ostettiin.
- Tapot alueittain: alue on **ampujan** oma alue tappohetkellä, ja otanta (n/m taposta) laskee tappoja eikä kierroksia -- kierrostyypillä on yleensä enemmän tappoja kuin kierroksia.
- Runko puhuu nimillä: joukkueen ja kokoonpanojen tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet ovat raportin viimeisessä luvussa Tekninen jäljitettävyys. Kolme poikkeusta, joissa tunniste on rungossa siksi että se on siellä ainoa käyttökelpoinen muoto: kierrosliitteen polut, puuttuvan demon rivi (tunniste on osa komentoa, jonka voi kopioida) ja kartta, jonka nimeä ei tunnistettu (tunniste on kartan ainoa nimi).
- Raportti kuvaa vain havainnot. Tulkinta ja vastastrategia ovat lukijan.

## Tekninen jäljitettävyys

Tunnisteet, jotka eivät ole rungossa: joukkueen ja kokoonpanojen tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet. Mitään ei ole poistettu -- ne ovat täällä, koska ne palvelevat vain jäljittämistä. Kynnykset, työkaluversiot ja aikaleima jäivät yhteenvetoon, koska ne kertovat miten luku laskettiin, eikä väitettä voi arvioida ilman niitä; tunniste ei muuta yhtäkään raportin lukua. Rungossa tunniste on vain siellä, missä se on ainoa käyttökelpoinen muoto: kierrosliitteen polussa, puuttuvan demon komennossa ja kartassa, jonka nimeä ei tunnistettu.

- **Joukkueen tunniste:** `aaaaaaaaaaaaaaaa` -- sama arvo kuin joukkueen ainoa kokoonpanotunniste
- **1. pelaaja1:** `1`
- **2. pelaaja2:** `2`
- **3. pelaaja3:** `3`
- **4. pelaaja4:** `4`
- **5. pelaaja5:** `5`
- **`de_nuke`:** `Ancient_vs_kaljukostaja`
"""


def golden_report() -> Report:
    """Pieni raportti, jonka koko tuloste on lukittu :data:`GOLDEN`iin."""
    entry = map_report(
        "de_nuke",
        [
            side(
                "T",
                [
                    round_type(
                        "eco",
                        4,
                        positions=[
                            position(6.0, [area("Ramp", 4, {2: 1, 0: 3})], 4),
                            first_contact_position([area("Ramp", 4, {1: 2, 0: 2})], 4),
                        ],
                        utility_counts=[counts("smoke", 4, {1: 1, 0: 3})],
                        # Molemmat pelaajalaskurit ovat mukana samasta
                        # syystä kuin kuolemat: vain golden lukitsee sen,
                        # että ne ovat peräkkäin ja että lukuohjeeseen tulee
                        # kaksi eri selitystä eikä yksi.
                        players_armed=armed(4, {0: 4}),
                        players_armored=armored(4, {5: 1, 0: 3}),
                        # Kuolemat ovat mukana, koska juuri tämä kiinnike
                        # lukitsee dokumentin muodon: ilman niitä rivien
                        # paikka, huomautus ja uusi lukuohjekappale eivät
                        # olisi missään lukittuja.
                        death_report=deaths(
                            first={"Cave": 2, "Long": 1},
                            rounds_missing=1,
                            median=24.0,
                            kills={"Middle": 4, "BombsiteB": 2},
                        ),
                    )
                ],
            )
        ],
    )
    # Yksi poikkeama, jotta golden lukitsee myös poikkeamaluvun muodon ja
    # paikan. Tyhjä luku on lukittu omassa testissään -- kumpaakin varianttia
    # ei voi olla samassa tulosteessa, ja tämä on se, jossa rivin muoto on
    # nähtävissä.
    return report(
        [entry],
        anomalies=[
            anomaly(
                map_name="de_nuke",
                area="Lobby",
                rounds=[anomaly_round(round_no=23, seconds=[30.0])],
                orientation=[(DEMO_ID, 0.89, 64)],
                m=4,
            )
        ],
    )


def test_the_whole_document_matches_the_golden_output() -> None:
    assert render(golden_report()) == GOLDEN


# --- Kuolemat ja tapot (Story 2.7) ---------------------------------------------


#: Kierroksia :func:`death_report`in lohkossa.
DEATH_ROUNDS = 4


def death_report(**kwargs) -> Report:
    """Yhden pistoolikierroslohkon raportti annetulla kuolemaosuudella.

    ``rounds_missing`` täytetään niin, että kuolemat kattavat koko otannan --
    muuten mallin ristiintarkistus hylkäisi jokaisen kutsun, jossa
    kuolemattomia kierroksia ei ole laskettu käsin.
    """
    kwargs.setdefault(
        "rounds_missing", DEATH_ROUNDS - sum((kwargs.get("first") or {}).values())
    )
    entry = round_type(
        "pistol", DEATH_ROUNDS, death_report=deaths(**kwargs)
    )
    return report([map_report("de_ancient", [side("CT", [entry])])])


def test_a_round_type_gets_at_most_two_death_lines() -> None:
    """Rajaus: enintään kaksi riviä kierrostyyppiä kohden.

    Raportti on jo satoja rivejä, ja kuolemat lisättiin selittämään muita
    rivejä eivätkä olemaan oma lukunsa. Rivit lasketaan **luetteloriveistä**,
    ei merkkijonohaulla: näkymä on se, jota malli latoo.
    """
    view = build_view(
        death_report(
            first={"Cave": 3, "Long": 1},
            median=24.0,
            kills={"Middle": 4, "BombsiteB": 2},
        )
    )
    lines = view.maps[0].sides[0].round_types[0].lines
    labels = [line.label for line in lines]
    # Kaksi literaalina eikä vakiona: vakio verrattuna itseensä on tautologia,
    # joka menisi läpi myös silloin kun raja nostetaan vahingossa. Sama sääntö
    # kuin skeemaversion pinnauksella.
    assert len(lines) == 2
    assert MAX_DEATH_LINES == 2
    assert labels == ["ensimmäinen kuolema (mediaani 24,0 s)", "tapot alueittain"]


def test_the_first_death_line_reads_like_the_target_analysis() -> None:
    """*"Ensimmäinen kuolema mediaani 24 s, useimmin Cave (3/4 kierroksesta)"*."""
    text = render(death_report(first={"Cave": 3, "Long": 1}, median=24.0))
    assert "- ensimmäinen kuolema (mediaani 24,0 s): Cave (3/4 kierroksesta)" in text
    assert "Long (1/4 kierroksesta)" in text


def test_the_kill_line_reads_like_the_target_analysis() -> None:
    """*"Tapot: Middle 4, BombsiteB 2"* -- alue ja määrä, suurin ensin."""
    text = render(death_report(kills={"BombsiteB": 2, "Middle": 4}))
    assert (
        "- tapot alueittain: Middle (4/6 taposta), BombsiteB (2/6 taposta)"
        in text
    )


def test_the_kill_sample_is_kills_not_rounds() -> None:
    """Nimittäjä on tappoja, ja rivi sanoo sen itse.

    Rivi luetaan yksinään, kaukana lukuohjeesta. "4/6 kierroksesta" olisi
    neljän kierroksen lohkossa suoraan mahdoton lause.
    """
    view = build_view(death_report(kills={"Middle": 4, "BombsiteB": 2}))
    line = next(
        line
        for line in view.maps[0].sides[0].round_types[0].lines
        if line.label == "tapot alueittain"
    )
    assert [c.unit for c in line.claims] == ["taposta", "taposta"]
    assert [c.sample_text for c in line.claims] == [
        "4/6 taposta",
        "2/6 taposta",
    ]


def test_every_other_claim_still_counts_rounds() -> None:
    """Yksikkö on poikkeus eikä uusi oletus."""
    assert Claim(text="Cave", n=1, m=2).sample_text == "1/2 kierroksesta"


def test_rounds_without_an_own_death_are_said_out_loud() -> None:
    """Kierros, jolla joukkue ei menettänyt ketään, ei katoa hiljaa."""
    text = render(death_report(first={"Cave": 2}, median=20.0, rounds_missing=2))
    assert "ei omia kuolemia 2 kierroksella" in text


def test_a_first_death_line_without_a_median_is_still_labelled() -> None:
    """Ajoituksen puuttuminen ei saa viedä aluetta."""
    view = build_view(death_report(first={"Cave": 1}))
    assert (
        view.maps[0].sides[0].round_types[0].lines[0].label
        == "ensimmäinen kuolema"
    )


def test_a_round_type_where_nobody_died_still_says_so() -> None:
    """"Ei omia kuolemia 4 kierroksella" on **havainto**, ei tyhjyys.

    Se kertoo, ettei joukkue menettänyt ketään -- eri asia kuin se, ettei
    kierrostyypistä tiedetä mitään. Rivi on siis pelkkä otsikko ja huomautus
    ilman yhtään väitettä, ja juuri se on sääntö: rivi kirjoitetaan kun
    sillä on väite **tai** havainto.
    """
    view = build_view(death_report())
    lines = view.maps[0].sides[0].round_types[0].lines

    assert [line.label for line in lines] == ["ensimmäinen kuolema"]
    assert lines[0].claims == ()
    assert lines[0].note == "ei omia kuolemia 4 kierroksella"


def test_a_round_type_without_rounds_gets_no_death_line_at_all() -> None:
    """Vartijan toinen haara: pelkkä otsikko ilman kumpaakaan ei kelpaa.

    Ilman tätä edellinen testi lukisi kuin rivi kirjoitettaisiin aina.
    """
    entry = round_type("pistol", 0, death_report=deaths())
    view = build_view(report([map_report("de_ancient", [side("CT", [entry])])]))

    assert view.maps[0].sides[0].round_types[0].lines == ()


def test_the_kill_line_stands_on_its_own_without_any_deaths() -> None:
    """Tapporivi ei tarvitse kuolemariviä seurakseen.

    Kierrostyyppi, jolla joukkue tappoi mutta ei menettänyt ketään, on
    tavallinen -- eikä tapporivi saa kadota siksi, että ensimmäisen kuoleman
    rivillä ei ole väitteitä.
    """
    view = build_view(death_report(kills={"Middle": 2}))
    labels = [line.label for line in view.maps[0].sides[0].round_types[0].lines]

    assert labels == ["ensimmäinen kuolema", "tapot alueittain"]


def test_an_unknown_first_death_area_is_named_and_explained() -> None:
    """Tuntematon sijainti on eri asia kuin tyhjä alue -- ja se selitetään.

    Pelkkä ``UNKNOWN_AREA in text`` ei todistaisi selitystä: ``_area(None)``
    palauttaa juuri sen merkkijonon **väitteen tekstiksi**, joten väite
    menisi läpi vaikka lippu jäisi nostamatta. Legendalause on eri
    merkkijono, ja vain se kertoo lukijalle mitä nimi tarkoittaa.
    """
    text = render(death_report(first={None: 1}, median=9.0))
    assert f"- ensimmäinen kuolema (mediaani 9,0 s): {UNKNOWN_AREA} " in text
    assert "pelin aluenimeä ei saatu" in text


def test_an_unknown_kill_area_is_named_and_explained() -> None:
    """Sama tappojen puolella: alueeton tappo ei putoa eikä jää selittämättä.

    Oma testinsä, koska lipun nostaa eri rivi kuin ensimmäisen kuoleman
    kohdalla -- yhteinen testi jättäisi toisen suorittamatta.
    """
    text = render(death_report(kills={None: 2}))
    assert f"- tapot alueittain: {UNKNOWN_AREA} (2/2 taposta)" in text
    assert "pelin aluenimeä ei saatu" in text


def test_the_unknown_area_note_stays_away_when_every_death_area_is_known() -> None:
    """Selitys ilman tapausta olisi lukuohje asiasta, jota raportissa ei ole."""
    text = render(death_report(first={"Cave": 1}, median=9.0, kills={"Middle": 1}))
    assert "pelin aluenimeä ei saatu" not in text


def test_the_kill_note_explains_the_denominator() -> None:
    """Lukuohje kertoo kerran, mistä tappojen nimittäjä tulee."""
    text = render(death_report(kills={"Middle": 2}))
    assert "laskee tappoja eikä kierroksia" in text
    assert "ampujan" in text


def test_the_kill_note_is_absent_when_no_kill_line_was_written() -> None:
    """Selitys ilman riviä olisi lukuohje asiasta, jota raportissa ei ole."""
    text = render(death_report(first={"Cave": 1}, median=9.0))
    assert "laskee tappoja eikä kierroksia" not in text


def test_the_pattern_threshold_also_applies_to_deaths() -> None:
    """Täysillä ostoilla kerrotaan vain toistuvat kuviot -- myös kuolemista."""
    entry = round_type(
        "full",
        10,
        death_report=deaths(
            first={"Cave": 4, "Long": 1},
            rounds_missing=5,
            median=20.0,
            kills={"Middle": 5, "Pit": 1},
        ),
    )
    text = render(report([map_report("de_ancient", [side("CT", [entry])])]))
    assert "Cave (4/5 kierroksesta)" in text
    assert "Long" not in text
    assert "Middle (5/6 taposta)" in text
    assert "Pit" not in text
    assert "harvinaisempaa havaintoa jäi pois" in text


def test_saving_rounds_keep_every_death_observation() -> None:
    """Säästökierroksilla jokainen havainto kirjoitetaan."""
    text = render(death_report(first={"Cave": 3, "Long": 1}, median=20.0))
    assert "Cave" in text and "Long" in text


def test_exceeding_the_death_line_limit_is_an_error_not_a_quiet_growth(
    monkeypatch,
) -> None:
    """Vartija on olemassa ja puree.

    Rajan ylitys ei voi syntyä nykyisellä koodilla, joten se rakennetaan
    laskemalla raja alas. Ilman tätä testiä vartija olisi väite, jota mikään
    ei todenna -- ja sellainen vartija katoaa seuraavassa muokkauksessa.
    """
    monkeypatch.setattr(view_module, "MAX_DEATH_LINES", 1)
    with pytest.raises(PappascoutError, match="Kuolemarivejä syntyi 2"):
        build_view(death_report(first={"Cave": 1}, median=9.0, kills={"Middle": 1}))


# --- Poikkeamaluku (Story 2.5) --------------------------------------------------


def anomaly_text(text: str) -> str:
    """Poikkeamaluvun sisältö. Puuttuva luku on virhe eikä tyhjä merkkijono."""
    return section_text(text, ANOMALY_HEADING)


def anomaly_round(
    *,
    round_no: int = 18,
    demo: str = DEMO_ID,
    round_type: str = "eco",
    seconds: list[float] | None = None,
    players: int = 2,
    sources: list[str] | None = None,
) -> AnomalyRound:
    """Yksi kierrosrivi poikkeaman alle."""
    return AnomalyRound(
        map_demo_id=demo,
        round_no=round_no,
        round_type=round_type,
        seconds=seconds if seconds is not None else [30.0],
        players_max=players,
        sources=sources or [],
    )


def anomaly(
    *,
    rule: str = "ct_advance",
    map_name: str = "de_ancient",
    map_name_source: str = "map_demo_id",
    side: str = "CT",
    area: str = "TSideLower",
    rounds: list[AnomalyRound] | None = None,
    orientation: list[tuple[str, float, int]] | None = None,
    m: int = 3,
    small_sample: bool = False,
) -> Anomaly:
    """Yksi poikkeamarivi. Oletukset ovat kalibroinnin Ancient k18.

    ``round_types``, ``n`` ja ``players_max`` johdetaan kierroksista, koska
    malli valvoo että ne vastaavat niitä -- kiinnike ei saa pystyä
    rakentamaan riviä, joka on itsensä kanssa eri mieltä.
    """
    entries = rounds if rounds is not None else [anomaly_round()]
    types = {entry.round_type for entry in entries}
    return Anomaly(
        rule=rule,
        map_name=map_name,
        map_name_source=map_name_source,
        side=side,
        area=area,
        round_types=[name for name in ROUND_TYPES if name in types],
        rounds=entries,
        orientation=[
            AreaOrientation(map_demo_id=demo, t_share=share, observations=count)
            for demo, share, count in (
                orientation
                if orientation is not None
                else [(entry.map_demo_id, 0.88, 24) for entry in entries[:1]]
            )
        ],
        players_max=max(entry.players_max for entry in entries),
        n=len(entries),
        m=m,
        small_sample=small_sample,
    )


def crunch_anomaly(**overrides) -> Anomaly:
    """Crunch-rivi: lähtöalueet ovat pakollisia jokaisella kierroksella."""
    overrides.setdefault("rule", "crunch")
    overrides.setdefault(
        "rounds", [anomaly_round(sources=["Arch", "TopofMid"])]
    )
    return anomaly(**overrides)


def test_the_anomaly_chapter_exists_even_without_anomalies() -> None:
    """Tyhjä poikkeamaluku on havainto: säännöt ajettiin, ne vaikenivat."""
    text = render(report([pistol_map()]))
    assert f"## {ANOMALY_HEADING}" in text
    assert "Ei poikkeamia" in anomaly_text(text)


def test_the_empty_chapter_says_what_was_run_and_on_what() -> None:
    """**"Ei poikkeamia" on havainto vain siitä, mitä tutkittiin.**

    Kolme asiaa, joita ilman tyhjä luku väittäisi mitattua negatiivista myös
    sokeasta pisteestä: montako kierrosta säännöt näkivät, montako
    arkkitehtuurin sääntöä jäi ajamatta, ja jäikö jonkin demon orientaatio
    tyhjäksi.
    """
    text = anomaly_text(render(report([pistol_map()])))
    assert "CT-eteneminen ja Crunch" in text
    assert "2 kierrokselle" in text
    assert "stack" in text
    assert "sokeita pisteitä ei ole" in text


def test_the_empty_chapter_names_the_blind_spots() -> None:
    """Tyhjä orientaatio on sokea piste eikä mitattu negatiivinen."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                scan_=scan(demos_without_orientation=[DEMO_ID]),
            )
        )
    )
    assert "1 demo ei antanut" in text
    assert "sokea piste eikä havainto" in text


def test_the_empty_chapter_mentions_unclassified_rounds() -> None:
    """Luokittelemattomat kierrokset ovat sääntöjen ulkopuolella."""
    text = anomaly_text(render(report([pistol_map()], unclassified=4)))
    assert "4 kierrosta jäi kokonaan tutkimatta" in text


def test_the_empty_chapter_exists_in_an_empty_report() -> None:
    """Myös raportti ilman karttoja saa poikkeamaluvun."""
    assert "Ei poikkeamia" in anomaly_text(render(report()))


def test_an_advance_line_carries_area_sample_and_orientation() -> None:
    """Koontirivi: mitä, missä, kuinka usein ja millä perusteella."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "CT-eteneminen (de_ancient, CT-puoli, eco): TSideLower" in text
    assert "1/3 kierroksesta" in text
    assert "T-osuus 0,88 alueen 24 havainnosta" in text


def test_the_round_line_carries_the_round_number() -> None:
    """Scoutin seuraava teko on avata se kierros demolta."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "  - kierros 18: 2 pelaajaa 30 s kohdalla" in text


def test_a_crunch_line_names_its_source_areas_per_round() -> None:
    """Matriisin rivi 2: crunchissa myös lähtöalueet -- kierroksen sisällä."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        area="Middle",
                        rounds=[
                            anomaly_round(
                                round_no=2,
                                seconds=[15.0],
                                players=5,
                                sources=["Arch", "TopofMid"],
                            )
                        ],
                        orientation=[(DEMO_ID, 0.83, 60)],
                    )
                ],
            )
        )
    )
    assert "Crunch (de_ancient, CT-puoli, havaittu: eco): Middle" in text
    assert (
        "  - kierros 2 (eco): 5 pelaajaa 15 s kohdalla, yhtä aikaa "
        "suunnista Arch ja TopofMid"
    ) in text


def test_two_crunch_rounds_never_merge_their_directions() -> None:
    """**Yhtäaikaisuus ei ylitä kierrosrajaa.**

    Yhdiste ("suunnista A, B, C ja D") lukisi neljäksi yhtäaikaiseksi
    suunnaksi, mikä on päinvastoin kuin määritelmä. Kierrosrivit ovat
    olemassa juuri tämän estämiseksi.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        m=4,
                        rounds=[
                            anomaly_round(
                                round_no=3, seconds=[15.0], players=2,
                                sources=["Alley", "BombsiteB"],
                            ),
                            anomaly_round(
                                round_no=10, round_type="full", seconds=[15.0],
                                players=3,
                                sources=["Arch", "LowerTunnel", "TopofMid"],
                            ),
                        ],
                        orientation=[(DEMO_ID, 0.81, 54)],
                    )
                ],
            )
        )
    )
    assert "2/4 kierroksesta" in text
    assert "kierros 3 (eco): 2 pelaajaa 15 s kohdalla, yhtä aikaa suunnista Alley ja BombsiteB" in text
    assert "kierros 10 (default): 3 pelaajaa 15 s kohdalla, yhtä aikaa suunnista Arch, LowerTunnel ja TopofMid" in text
    # Neljän suunnan yhdistettä ei ole missään.
    assert "Alley, BombsiteB, Arch" not in text
    assert "Alley, Arch" not in text


def test_a_crunch_label_says_the_types_are_observations_not_a_limit() -> None:
    """Crunchia ei ole rajattu kierrostyyppiin, ja nimiö sanoo sen."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        m=4,
                        rounds=[
                            anomaly_round(round_no=1, sources=["A", "B"]),
                            anomaly_round(
                                round_no=2, round_type="full", sources=["A", "B"]
                            ),
                        ],
                    )
                ],
            )
        )
    )
    assert "havaittu: eco, default" in text


def test_an_advance_line_never_claims_source_areas() -> None:
    """Etenemisellä tyhjä lista tarkoittaa 'ei kysytty' eikä 'ei suuntia'."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "suunnista" not in text


def test_the_advance_round_line_omits_the_round_type() -> None:
    """Se on jo nimiössä; samaa sanaa ei kirjoiteta kahdesti riville."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "kierros 18: " in text
    assert "kierros 18 (eco)" not in text


def test_several_sample_points_are_listed_as_a_finnish_list() -> None:
    """Rivi luetaan lauseena, joten viimeinen erotin on 'ja'."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(rounds=[anomaly_round(seconds=[15.0, 30.0])])],
            )
        )
    )
    assert "15 ja 30 s kohdalla" in text


def test_two_demos_give_the_same_area_two_shares() -> None:
    """Keskiarvo olisi luku, jota ei ole havaittu."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    anomaly(
                        rounds=[
                            anomaly_round(round_no=1, demo="demo-a"),
                            anomaly_round(round_no=2, demo="demo-b"),
                        ],
                        orientation=[("demo-a", 0.88, 24), ("demo-b", 0.84, 37)],
                    )
                ],
            )
        )
    )
    assert (
        "T-osuus 0,88 alueen 24 havainnosta; "
        "T-osuus 0,84 alueen 37 havainnosta"
    ) in text


def test_two_demos_make_the_round_line_name_its_demo() -> None:
    """Kierrosnumero ei yksilöi, kun kartalla on kaksi demoa."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    anomaly(
                        rounds=[
                            anomaly_round(round_no=1, demo="demo-a"),
                            anomaly_round(round_no=2, demo="demo-b"),
                        ],
                        orientation=[("demo-a", 0.88, 24), ("demo-b", 0.84, 37)],
                    )
                ],
            )
        )
    )
    assert "kierros 1: 2 pelaajaa 30 s kohdalla -- `demo-a`" in text


def test_one_demo_leaves_the_identifier_out_of_the_round_line() -> None:
    """Vartijan toinen haara: yhdellä demolla tunniste ei kuulu runkoon."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert DEMO_ID not in text


def test_a_small_sample_anomaly_is_marked_not_hidden() -> None:
    """Yksi kierros on kelvollinen otanta ja merkitään pieneksi."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(m=1, small_sample=True)],
            )
        )
    )
    assert "1/1 kierroksesta" in text
    assert "pieni otanta" in text


def test_the_small_sample_mark_is_not_confused_with_the_label() -> None:
    """``--`` tarkoittaa rivillä vain yhtä asiaa: huomautusta.

    Nimiössä oli aiemmin sama erotin, joten sama merkki tarkoitti kahta eri
    asiaa samalla rivillä. Nimiön osat ovat nyt suluissa.
    """
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(m=1, small_sample=True)],
            )
        )
    )
    row = next(r for r in text.splitlines() if r.startswith("- "))
    assert row.count(" -- ") == 1
    assert row.endswith("pieni otanta")


def test_an_unrecognised_map_never_puts_its_identifier_in_the_body() -> None:
    """Runko puhuu nimillä; tunnistamaton kartta nimetään paikallaan.

    Nimiö on **sama merkkijono** kuin jäljitettävyysluvun karttarivillä,
    joten lukija voi yhdistää rivin oikeaan karttalukuun.
    """
    entry = map_report(FACEIT_DEMO_ID, [side("CT", [round_type("eco", 1)])],
                       demo_ids=[FACEIT_DEMO_ID], source="unknown")
    text = render(
        report(
            [entry],
            anomalies=[
                anomaly(
                    map_name=FACEIT_DEMO_ID,
                    map_name_source="unknown",
                    rounds=[anomaly_round(demo=FACEIT_DEMO_ID)],
                    m=1,
                )
            ],
        )
    )
    chapter = anomaly_text(text)
    assert FACEIT_DEMO_ID not in chapter
    assert "kartta 1, nimeä ei tunnistettu" in chapter
    assert "kartta 1, nimeä ei tunnistettu" in traceability_text(text)


def test_the_anomaly_chapter_comes_before_the_map_chapters() -> None:
    """Poikkeamat ovat epicin arvokkain tuotos eivätkä kuulu loppuun."""
    text = render(report([pistol_map()], anomalies=[anomaly()]))
    headings = [heading for heading, _ in report_sections(text) if heading]
    assert headings.index(ANOMALY_HEADING) < headings.index(
        "de_ancient -- 2 kierrosta, 1 demo"
    )
    assert headings.index("Yhteenveto") < headings.index(ANOMALY_HEADING)


def test_the_anomaly_chapter_follows_the_missing_demos_chapter() -> None:
    """Järjestys on yhteenveto, puuttuvat demot, poikkeamat, kartat."""
    text = render(
        report(
            [pistol_map()],
            anomalies=[anomaly()],
            missing_demos=[MissingDemo(match=MISSING_DEMO_ID, reason="ei demoa")],
        )
    )
    headings = [heading for heading, _ in report_sections(text) if heading]
    assert headings.index("Puuttuvat demot") < headings.index(ANOMALY_HEADING)


def test_the_anomaly_lines_are_bullets_not_paragraphs() -> None:
    text = anomaly_text(
        render(report([pistol_map()], anomalies=[anomaly(), crunch_anomaly()]))
    )
    rows = [row for row in text.splitlines() if row.strip()]
    assert len(rows) == 4  # kaksi koontiriviä ja kaksi kierrosriviä
    assert all(row.lstrip().startswith("- ") for row in rows), rows


def test_the_most_repeated_anomaly_comes_first() -> None:
    """Luku nostaa esiin sen, mikä toistuu."""
    once = anomaly(area="Ramp", m=4)
    twice = anomaly(
        area="TSideLower",
        m=4,
        rounds=[anomaly_round(round_no=1), anomaly_round(round_no=2)],
    )
    text = anomaly_text(render(report([pistol_map()], anomalies=[once, twice])))
    assert text.index("TSideLower") < text.index("Ramp")


def test_the_chapter_has_a_line_cap_and_says_what_it_dropped() -> None:
    """Luku on raportin ensimmäinen sisältöluku eikä saa kasvaa rajatta.

    Kuolemarivien katto on rakenteellinen, joten sen ylitys on virhe.
    Poikkeamien määrä on aineiston ominaisuus, joten virhe kaataisi ajon
    aineistosta jota ei voi valita -- rajaus tehdään ja pois jätettyjen määrä
    kirjoitetaan näkyviin, kuten kuvion kynnyksellä.
    """
    many = [
        anomaly(area=f"Alue{i:02d}", m=MAX_ANOMALY_LINES + 5)
        for i in range(MAX_ANOMALY_LINES + 3)
    ]
    text = anomaly_text(render(report([pistol_map()], anomalies=many)))
    rows = [r for r in text.splitlines() if r.startswith("- ")]
    # 20 koontiriviä + yksi huomautusrivi.
    assert len(rows) == MAX_ANOMALY_LINES + 1
    assert "3 poikkeamaa jäi pois" in text
    assert "report.jsonissa" in text


def test_the_cap_note_is_absent_when_nothing_was_dropped() -> None:
    """Vartijan toinen haara: hiljainen rajaus ei ole ainoa vaara."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    assert "jäi pois" not in text


def test_the_chapter_heading_is_the_same_in_the_code_and_the_template() -> None:
    """Sama vartija kuin jäljitettävyysluvulla: kaksi kopiota erkanisi."""
    assert f"## {ANOMALY_HEADING}" in template_text()


def test_the_legend_explains_what_the_t_share_means() -> None:
    """Lukuohje kertoo, että orientaatio on demon oma havainto."""
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "demon oma havainto" in legend
    assert "molempien joukkueiden" in legend


def test_the_legend_names_the_thresholds_from_the_report() -> None:
    """Kynnykset luetaan raportista eikä keksitä renderöinnissä.

    Säädetty ``settings.toml`` näkyy raportin tekstissä vain, jos teksti
    tulee raportista -- sama sääntö kuin kuvion kynnyksellä.
    """
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "vähintään 0,80" in legend
    assert "vähintään 20 havaintoa" in legend
    assert "enintään 30 sekunnin kohdalla" in legend


def test_the_legend_follows_a_changed_threshold() -> None:
    """Vartija sille, ettei luku ole kovakoodattu."""
    legend = section_text(
        render(
            report(
                [pistol_map()],
                thresholds_used={
                    "thresholds": {
                        "small_sample_rounds": SMALL_SAMPLE,
                        "team_identity_min_common": MIN_COMMON,
                        "advance_t_share": 0.9,
                        "advance_area_min_observations": 40,
                        "advance_max_sample_s": 15.0,
                        "advance_min_players": 2,
                        "crunch_min_players": 3,
                        "crunch_min_sources": 3,
                    }
                },
            )
        ),
        "Lukuohje",
    )
    assert "vähintään 0,90" in legend
    assert "vähintään 40 havaintoa" in legend
    assert "enintään 15 sekunnin kohdalla" in legend
    assert "Vähintään 2 pelaajaa alueella" in legend
    assert "Vähintään 3 pelaajaa ja 3 eri suuntaa" in legend


def test_the_legend_defines_both_rules() -> None:
    """Ilman määritelmiä sääntöjen epäsymmetria on näkymätön.

    Lukija ei muuten voi tietää, miksi alue esiintyy ``eco``-rivillä muttei
    ``default``-rivillä.
    """
    legend = section_text(render(report([pistol_map()])), "Lukuohje")
    assert "**CT-eteneminen**" in legend
    assert "**Crunch**" in legend
    assert "säästökierroksella" in legend
    assert "ei ole rajattu kierrostyyppiin" in legend


def test_the_legend_is_written_even_for_an_empty_chapter() -> None:
    """Puhtaan raportin lukija tarvitsee menetelmän enemmän kuin kukaan muu.

    Peruste on eri kuin muilla lukuohjeen kappaleilla: nämä eivät selitä
    riviä, joka raportissa on, vaan mitä mitattiin.
    """
    legend = section_text(render(report()), "Lukuohje")
    assert "demon oma havainto" in legend
    assert "**Crunch**" in legend


def test_the_areas_stay_in_english_in_the_anomaly_chapter() -> None:
    """Calloutit englanniksi, teksti suomeksi -- sama sääntö kuin muualla."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[
                    crunch_anomaly(
                        rounds=[
                            anomaly_round(
                                sources=["SideEntrance", "TSideUpper"]
                            )
                        ]
                    )
                ],
            )
        )
    )
    assert "SideEntrance" in text
    assert "TSideUpper" in text


def test_the_anomaly_chapter_carries_no_interpretation() -> None:
    """Ei tulkintaa eikä vastastrategiaa -- vain havainto."""
    text = anomaly_text(render(report([pistol_map()], anomalies=[anomaly()])))
    for word in ("fake", "rush", "kannattaa", "suositus", "vastaus"):
        assert word not in text.lower()


def test_a_single_player_is_not_written_in_the_plural() -> None:
    """Neljä kuudesta kalibroidusta osumasta on yhden pelaajan havainto."""
    text = anomaly_text(
        render(
            report(
                [pistol_map()],
                anomalies=[anomaly(rounds=[anomaly_round(players=1)])],
            )
        )
    )
    assert "kierros 18: 1 pelaaja 30 s kohdalla" in text


def test_every_anomaly_rule_has_a_finnish_name_in_the_view() -> None:
    """Näkymä indeksoi karttaa suoraan, joten puuttuva nimi kaataa sen."""
    for rule in ANOMALY_RULES:
        assert ANOMALY_RULE_FI[rule]
