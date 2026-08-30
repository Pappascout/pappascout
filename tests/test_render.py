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
    ROUND_TYPES,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
)
from pappascout.domain.report import (
    AreaDistribution,
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
    GRENADE_ORDER,
    GRENADE_TYPE_FI,
    MAX_DEATH_LINES,
    PATTERN_ROUND_TYPES,
    ROUND_TYPE_ORDER,
    UNKNOWN_AREA,
    Claim,
    pattern_min_rounds,
)

TEAM_KEY = "aaaaaaaaaaaaaaaa"
TEAM_NAME = "MatureMayhem"

#: Slug, jonka oletusraportti saa: se johdetaan NIMESTÄ eikä
#: tunnisteesta, koska nimi on havainto. Tiedostonimen testit lukevat
#: sen täältä, jottei sääntö ole kahdessa paikassa eri muodossa.
TEAM_SLUG = "maturemayhem"

#: Rosteri, jossa jokaisella on nimi ja SteamID64 rinnakkain. Molemmat, aina:
#: nimi on luettavuutta varten, tunniste on ainoa jäljitettävä arvo.
DEFAULT_ROSTER = [
    RosterEntry(player_id=str(n), display_name=f"pelaaja{n}")
    for n in range(1, 6)
]
DEMO_ID = "Ancient_vs_kaljukostaja"

#: Kynnys, jonka raportti kantaa mukanaan. Sama luku kuin
#: ``settings.toml``issa; testit lukevat sen raportista, eivät asetuksista --
#: juuri kuten ``render`` itse.
SMALL_SAMPLE = 3

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


def report(
    maps: list[MapReport] | None = None,
    *,
    missing_demos: list[MissingDemo] | None = None,
    unclassified: int = 0,
    unpaired: int = 0,
    thresholds_used: dict | None = None,
    classify_thresholds: dict | None = None,
    display_name: str = TEAM_NAME,
    display_name_source: str | None = None,
    name_alternatives: list[str] | None = None,
    roster: list[RosterEntry] | None = None,
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
            lineup_keys=[TEAM_KEY],
            roster=list(roster if roster is not None else DEFAULT_ROSTER),
            roster_source="lineups",
        ),
        sample=sample(rounds, demos=demos),
        thresholds_used=(
            {"thresholds": {"small_sample_rounds": SMALL_SAMPLE}}
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


def render(entry: Report) -> str:
    """Raportti kierroslistojen polkuineen -- kuten vaihe sen kirjoittaa."""
    return render_report(entry, round_list_paths=ROUND_LISTS)


# --- Perusmuoto -----------------------------------------------------------------


def test_report_has_the_structure_the_spec_asks_for() -> None:
    """Otsikko, yhteenveto, kartta, puoli, kierrostyyppi, liite, lukuohje."""
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
    olisi nimeltään niin. Tunniste on yhteenvedossa, jossa se on tunniste.
    """
    entry = report(
        [pistol_map()], display_name=TEAM_KEY, display_name_source="team_key"
    )
    text = render(entry)
    assert text.startswith("# Scouting-raportti -- joukkueen nimi ei tiedossa")
    assert "nimi ei ole tiedossa; tunniste " + TEAM_KEY in text


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
    assert f"{TEAM_KEY} (tunniste {TEAM_KEY})" in text


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
    # Rivinvaihto ja peräkkäiset välilyönnit siivotaan näkyvästi.
    assert "a" + chr(92) + "_b c d (1)" in text
    # Malli itse säilyttää havainnon sellaisenaan.
    assert entry.team.display_name == "*|LOL|*"


def test_a_known_team_name_is_used_in_the_title() -> None:
    text = render(report([pistol_map()]))
    assert text.startswith(f"# {TEAM_NAME} -- scouting-raportti")
    assert f"{TEAM_NAME} (tunniste {TEAM_KEY})" in text


def test_the_roster_shows_the_name_and_the_steamid_side_by_side() -> None:
    """Nimi ja tunniste rinnakkain, kumpikaan ei korvaa toista (Story 2.6).

    Nimi on luettavuutta varten, SteamID64 on ainoa jäljitettävä arvo.
    Pelkkä nimi tekisi rosterista tarkistuskelvottoman, pelkkä tunniste
    lukukelvottoman.
    """
    text = render(report([pistol_map()]))
    assert "nimi ja SteamID64 rinnakkain" in text
    for number in range(1, 6):
        assert f"pelaaja{number} ({number})" in text


def test_a_player_without_a_name_keeps_the_row_and_says_the_name_is_missing() -> None:
    """Rivi kirjoitetaan silti: hiljaa pudotettu pelaaja kutistaisi rosterin."""
    entry = report(
        [pistol_map()],
        roster=[
            RosterEntry(player_id="1", display_name="pelaaja1"),
            RosterEntry(player_id="2"),
        ],
    )
    text = render(entry)
    assert "pelaaja1 (1)" in text
    assert "2 (nimi ei luettavissa)" in text


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

- **Joukkue:** MatureMayhem (tunniste aaaaaaaaaaaaaaaa)
- **Rosteri:** 5 pelaajaa (havaittu demoista); nimi ja SteamID64 rinnakkain: pelaaja1 (1), pelaaja2 (2), pelaaja3 (3), pelaaja4 (4), pelaaja5 (5)
- **Otanta:** 1 demo, 4 kierrosta (demoa/kierrosta: liiga 0 / 0, muut 0 / 0, tuntematon 1 / 4)
- **Liigatieto:** yhdenkään demon lajia ei ole vahvistettu: kaikki ovat lokerossa tuntematon, eikä otannassa ole yhtään varmistettua liigaottelua
- **Pieni otanta:** alle 3 kierrosta merkitään (pieni otanta); havaintoa ei silti piiloteta
- **Luokittelun kynnykset:** full_equip_min 4000
- **Aggregoinnin kynnykset:** small_sample_rounds 3
- **Aineisto koottu:** 2026-08-30 12:00 UTC (pappascout 0.1.0)

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
- Aseistettu = panssari JA parannettu ase ostoajan lopussa; panssaroitu = panssari, aseesta riippumatta. Luvut ovat **sisäkkäisiä**: aseistetut ovat panssaroitujen osajoukko, molemmat on luettu samalta tickiltä samasta pelaajajoukosta, ja jakaja on sama. Rivien ero on siis se havainto -- pistoolikierroksella aseistettuja on tyypillisesti 0 (800 $ ei riitä sekä kevlariin että parannettuun aseeseen), joten panssaririvi on se, joka kertoo kevlarien määrän.
- Molemmat luvut ovat **hallussapitoa eivätkä ostoja**: panssari ja ase säilyvät kierroksen yli hengissä selvinneellä, eikä vaurioitunutta panssaria eroteta ehjästä. Poikkeus on pistoolikierros -- puoliaika alkaa puhtaalta pöydältä, joten siellä luvut kertovat mitä ostettiin.
- Tapot alueittain: alue on **ampujan** oma alue tappohetkellä, ja otanta (n/m taposta) laskee tappoja eikä kierroksia -- kierrostyypillä on yleensä enemmän tappoja kuin kierroksia.
- Raportti kuvaa vain havainnot. Tulkinta ja vastastrategia ovat lukijan.
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
    return report([entry])


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
