"""``domain.report`` -- raporttimallin testit.

Malli on ``aggregate``-vaiheen ja ``render``-vaiheen jaettu sopimus, joten sen
tarkistukset ovat osa sopimusta eivätkä koristetta: epäkelpoa raporttia ei saa
voida rakentaa edes muistiin. Nämä testit eivät tarvitse tauluja, arkistoa
eivätkä demoja.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

import pytest
from pydantic import ValidationError

from pappascout.domain.report import (
    MAP_NAME_SOURCES,
    Anomaly,
    MapNameSource,
    AnomalyPoint,
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
    REPORT_SCHEMA_VERSION,
    Report,
    RosterEntry,
    RoundTypeReport,
    SLUG_FALLBACK,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
)
from pappascout.errors import AggregateError


def sample(league: int = 0, other: int = 0, unknown: int = 1) -> Sample:
    """Otanta, jossa jokaisessa lokerossa on yksi demo per kierros."""
    buckets = {
        "league": SampleBucket(demos=1 if league else 0, rounds=league),
        "other": SampleBucket(demos=1 if other else 0, rounds=other),
        "unknown": SampleBucket(demos=1 if unknown else 0, rounds=unknown),
    }
    return Sample(
        demos=sum(b.demos for b in buckets.values()),
        rounds=sum(b.rounds for b in buckets.values()),
        **buckets,
    )


def team() -> TeamReport:
    return TeamReport(
        key="aaaaaaaaaaaaaaaa",
        slug="aaaaaaaaaaaaaaaa",
        display_name="aaaaaaaaaaaaaaaa",
        lineup_keys=["aaaaaaaaaaaaaaaa"],
        roster=[RosterEntry(player_id=str(n)) for n in range(1, 6)],
        roster_source="lineups",
    )


# --- Joukkueen nimi ja rosteri --------------------------------------------------


def team_kwargs(**overrides: object) -> dict[str, object]:
    """Kelvollinen ``TeamReport``, josta testi rikkoo yhden asian kerrallaan."""
    values: dict[str, object] = {
        "key": "aaaaaaaaaaaaaaaa",
        "slug": "aaaaaaaaaaaaaaaa",
        "display_name": "aaaaaaaaaaaaaaaa",
        "lineup_keys": ["aaaaaaaaaaaaaaaa"],
        "roster": [],
        "roster_source": "lineups",
    }
    values.update(overrides)
    return values


def test_a_name_without_a_source_cannot_be_a_name() -> None:
    """``team_key`` tarkoittaa ettei nimeä ole; silloin nimi on tunniste."""
    with pytest.raises(AggregateError, match="lähteeksi"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="team_key",
            )
        )


def test_alternatives_cannot_exist_without_an_observed_name() -> None:
    """Vaihtoehdot ovat havaintoja: niitä ei voi olla ilman havaittua nimeä."""
    with pytest.raises(AggregateError, match="vaihtoehtoisia nimiä"):
        TeamReport(
            **team_kwargs(
                display_name_source="team_key",
                display_name_alternatives=["MM Academy"],
            )
        )


def test_an_empty_string_cannot_be_the_observed_name() -> None:
    """Tyhjä merkkijono ei ole nimi -- silloin lähde on ``team_key``."""
    with pytest.raises(AggregateError, match="tyhjä merkkijono"):
        TeamReport(
            **team_kwargs(display_name="   ", display_name_source="clan_name")
        )


def test_a_blank_alternative_is_refused() -> None:
    with pytest.raises(AggregateError, match="tyhjää merkkijonoa"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="clan_name",
                display_name_alternatives=["  "],
            )
        )


def test_a_repeated_alternative_is_refused() -> None:
    """Sama nimi kahdesti ei ole kaksi havaintoa."""
    with pytest.raises(AggregateError, match="toistuvat"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="clan_name",
                display_name_alternatives=["MM Academy", "MM Academy"],
            )
        )


def test_the_shown_name_cannot_be_its_own_alternative() -> None:
    with pytest.raises(AggregateError, match="omien vaihtoehtojensa"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="clan_name",
                display_name_alternatives=["MatureMayhem"],
            )
        )


def test_the_slug_must_follow_the_shown_name() -> None:
    """Slug päätyy tiedostonimeen, joten se ei saa olla eri mieltä nimestä."""
    with pytest.raises(AggregateError, match="Joukkueen slug on"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="aaaaaaaaaaaaaaaa",
                display_name_source="clan_name",
            )
        )


def test_a_name_without_ascii_falls_back_to_the_key_not_to_a_shared_constant() -> None:
    """Kyrillinen klaani: varapolku on tunniste, ei jaettu vakio.

    Jaettu vakio antaisi jokaiselle tällaiselle joukkueelle saman
    tiedostonimen, jolloin raportit törmäisivät toisiinsa.
    """
    team = TeamReport(
        **team_kwargs(
            display_name="Кибер",
            slug="aaaaaaaaaaaaaaaa",
            display_name_source="clan_name",
        )
    )
    assert team.slug == "aaaaaaaaaaaaaaaa"
    assert team.slug != SLUG_FALLBACK


def test_an_empty_player_name_becomes_no_name() -> None:
    """Tyhjä nimi SteamID:n vieressä lukisi kuin nimi olisi tyhjä."""
    assert RosterEntry(player_id="1", display_name="  ").display_name is None
    assert RosterEntry(player_id="1", display_name="Sassiz").display_name == "Sassiz"


# --- Otanta ---------------------------------------------------------------------


def test_sample_totals_must_match_the_buckets() -> None:
    """Summa on lokeroiden summa; muuten kaksi lukua kertoisi eri tarinaa.

    Poikkeus on ``AggregateError`` kuten kaikissa muissakin
    summatarkistuksissa: sama vika ei saa tuottaa kahta eri poikkeuslajia,
    koska kutsuja nappaa ne yhtenä.
    """
    with pytest.raises(AggregateError, match="Otannan summat"):
        Sample(
            demos=9,
            rounds=1,
            league=SampleBucket(demos=0, rounds=0),
            other=SampleBucket(demos=0, rounds=0),
            unknown=SampleBucket(demos=1, rounds=1),
        )


def test_unknown_bucket_exists_alongside_the_other_two() -> None:
    """Kolme lokeroa, ei kahta: tyhjä ``is_league`` ei ole ``other``."""
    s = sample(unknown=12)
    assert s.unknown.rounds == 12
    assert s.other.rounds == 0
    assert s.league.rounds == 0
    assert s.rounds == 12


# --- Sigma n = m ----------------------------------------------------------------


def test_area_distribution_requires_the_sample_to_add_up() -> None:
    with pytest.raises(AggregateError, match="Otanta ei täsmää alueella"):
        AreaDistribution(
            area="BombsiteA",
            m=4,
            players_dist=[PlayersCount(players=3, n=1)],
        )


def test_area_distribution_with_a_zero_bucket_adds_up() -> None:
    """Nollalokero on se, joka tekee summasta oikean."""
    dist = AreaDistribution(
        area="BombsiteB",
        m=4,
        players_dist=[
            PlayersCount(players=0, n=3),
            PlayersCount(players=2, n=1),
        ],
    )
    assert sum(p.n for p in dist.players_dist) == dist.m


def test_area_distribution_rejects_the_same_player_count_twice() -> None:
    with pytest.raises(ValidationError, match="kahdesti"):
        AreaDistribution(
            area="Middle",
            m=2,
            players_dist=[
                PlayersCount(players=1, n=1),
                PlayersCount(players=1, n=1),
            ],
        )


def test_position_areas_must_share_the_positions_sample() -> None:
    """Kaksi eri otantaa samasta hetkestä ei olisi vertailukelpoista."""
    with pytest.raises(AggregateError, match="jaettava sama otanta"):
        Position(
            sample_kind="time",
            seconds=15.0,
            m=3,
            rounds_missing=0,
            areas=[
                AreaDistribution(
                    area="Middle", m=2, players_dist=[PlayersCount(players=0, n=2)]
                )
            ],
        )


def test_utility_counts_require_the_sample_to_add_up() -> None:
    with pytest.raises(AggregateError, match="kranaattityypillä"):
        UtilityCounts(
            grenade_type="smoke", m=5, counts=[GrenadeCount(thrown=1, n=2)]
        )


def test_armed_players_require_the_sample_to_add_up() -> None:
    with pytest.raises(AggregateError, match="aseistettujen"):
        ArmedPlayers(m=3, rounds_unknown=0, counts=[ArmedCount(armed=5, n=1)])


def test_armed_players_keep_unknown_apart_from_zero() -> None:
    """Lukukelvoton tavaraluettelo ei ole nolla aseistettua."""
    armed = ArmedPlayers(
        m=2, rounds_unknown=3, counts=[ArmedCount(armed=0, n=2)]
    )
    assert armed.m == 2
    assert armed.rounds_unknown == 3


# --- Näytepisteen laji ----------------------------------------------------------


def test_time_sample_must_carry_its_nominal_second() -> None:
    with pytest.raises(ValidationError, match="Aikanäytepisteellä"):
        Position(
            sample_kind="time", seconds=None, m=0, rounds_missing=0, areas=[]
        )


def test_first_contact_sample_has_no_nominal_second() -> None:
    """Ensikontaktin hetki on eri joka kierroksella; mediaani kertoo sen."""
    with pytest.raises(ValidationError, match="ei ole nimellistä"):
        Position(
            sample_kind="first_contact",
            seconds=12.0,
            m=0,
            rounds_missing=0,
            areas=[],
        )
    position = Position(
        sample_kind="first_contact",
        seconds=None,
        seconds_median=12.5,
        m=0,
        rounds_missing=0,
        areas=[],
    )
    assert position.seconds_median == 12.5


# --- Utility --------------------------------------------------------------------


def test_utility_use_cannot_appear_in_more_rounds_than_exist() -> None:
    with pytest.raises(AggregateError, match="vaikka kierroksia on"):
        UtilityUse(
            grenade_type="smoke",
            throw_area="TSpawn",
            detonate_area="BombsiteB",
            area_source="point_cloud",
            seconds_bucket="0-5",
            n=4,
            throws=4,
            m=2,
        )


def test_utility_use_cannot_have_fewer_throws_than_rounds() -> None:
    with pytest.raises(AggregateError, match="heittoa mutta"):
        UtilityUse(
            grenade_type="flashbang",
            throw_area=None,
            detonate_area=None,
            area_source=None,
            seconds_bucket="5-10",
            n=3,
            throws=2,
            m=5,
        )


def test_utility_use_without_an_area_keeps_its_own_bucket() -> None:
    """Alueeton kranaatti ei putoa: savu heitetään sinne, missä ei ole ketään."""
    use = UtilityUse(
        grenade_type="smoke",
        throw_area="TSpawn",
        detonate_area=None,
        area_source=None,
        seconds_bucket="0-5",
        n=2,
        throws=2,
        m=4,
    )
    assert use.detonate_area is None
    assert use.area_source is None


def test_detonate_area_cannot_appear_without_its_source() -> None:
    """Ilman lähdettä arvio näyttäisi havainnolta."""
    with pytest.raises(ValidationError, match="ristiriidassa"):
        UtilityUse(
            grenade_type="smoke",
            throw_area=None,
            detonate_area="BombsiteA",
            area_source=None,
            seconds_bucket="0-5",
            n=1,
            throws=1,
            m=1,
        )


def test_area_source_cannot_appear_without_its_area() -> None:
    """Lähde ilman aluetta väittäisi johdosta alueelle, jota ei ole."""
    with pytest.raises(ValidationError, match="ristiriidassa"):
        UtilityUse(
            grenade_type="smoke",
            throw_area="TSpawn",
            detonate_area=None,
            area_source="point_cloud",
            seconds_bucket="0-5",
            n=1,
            throws=1,
            m=1,
        )


def test_first_contact_area_cannot_exceed_its_sample() -> None:
    with pytest.raises(AggregateError, match="Ensikontaktin alue"):
        FirstContactArea(area="Banana", n=3, m=2)


# --- Koko raportti --------------------------------------------------------------


def full_report() -> Report:
    round_type = RoundTypeReport(
        round_type="pistol",
        sample=sample(unknown=1),
        small_sample=True,
        positions=[
            Position(
                sample_kind="time",
                seconds=6.0,
                m=1,
                rounds_missing=0,
                areas=[
                    AreaDistribution(
                        area="BombsiteA",
                        m=1,
                        players_dist=[PlayersCount(players=3, n=1)],
                    ),
                    AreaDistribution(
                        area="BombsiteB",
                        m=1,
                        players_dist=[PlayersCount(players=2, n=1)],
                    ),
                ],
            )
        ],
        utility=[],
        utility_counts=[],
        players_armed=ArmedPlayers(
            m=1, rounds_unknown=0, counts=[ArmedCount(armed=0, n=1)]
        ),
        players_armored=ArmoredPlayers(
            m=1, rounds_unknown=0, counts=[ArmoredCount(armored=5, n=1)]
        ),
        first_contact=[],
        deaths=DeathReport(m=0, rounds_missing=1),
    )
    return Report(
        generated_at=datetime(2026, 8, 30, 12, 0, tzinfo=UTC),
        tool_versions={"pappascout": "0.1.0"},
        team=team(),
        sample=sample(unknown=1),
        thresholds_used={"small_sample_rounds": 3},
        missing_demos=[MissingDemo(match="Nuke_vs_x", reason="ei parsittu")],
        unclassified_rounds=2,
        anomaly_scan=scan(),
        maps=[
            MapReport(
                map_name="de_anubis",
                map_name_source="map_demo_id",
                map_demo_ids=["Anubis_vs_ryhmarama"],
                sample=sample(unknown=1),
                sides=[
                    SideReport(
                        side="CT",
                        sample=sample(unknown=1),
                        round_types=[round_type],
                    )
                ],
            )
        ],
    )


def scan(**overrides) -> AnomalyScan:
    """Poikkeamasääntöjen kattavuus kiinnikkeeseen.

    Oletus on täysi kattavuus ilman sokeita pisteitä, koska se on se tila,
    jossa tyhjä poikkeamaluku on **mitattu negatiivinen** -- ja siitä
    poikkeavat tilat rakennetaan erikseen niitä koskevissa testeissä.
    """
    values: dict[str, object] = {
        "rules": ["ct_advance", "crunch", "stack"],
        "rules_deferred": [],
        "rounds_scanned": 1,
        "crunch_rounds": 1,
        "advance_rounds": 0,
        "stack_rounds": 1,
    }
    values.update(overrides)
    return AnomalyScan(**values)


def _report_with_anomalies(anomalies: list[Anomaly]) -> Report:
    """Raportti, jonka ainoa kartta on ``de_ancient`` -- poikkeamien koti.

    Kartta on rakenteessa siksi, että :class:`Report` valvoo poikkeaman
    kartan olevan raportissa. Ilman karttaa jokainen poikkeamatesti kaatuisi
    siihen vartijaan eikä siihen, mitä se mittaa.
    """
    return Report(
        generated_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        team=team(),
        sample=sample(unknown=1),
        anomaly_scan=scan(),
        anomalies=anomalies,
        maps=[
            MapReport(
                map_name="de_ancient",
                map_name_source="demo_header",
                map_demo_ids=["demo"],
                sample=sample(unknown=1),
                sides=[
                    SideReport(
                        side="CT",
                        sample=sample(unknown=1),
                        round_types=[
                            RoundTypeReport(
                                round_type="eco",
                                sample=sample(unknown=1),
                                small_sample=True,
                                positions=[],
                                utility=[],
                                utility_counts=[],
                                first_contact=[],
                                players_armed=ArmedPlayers(
                                    m=0, rounds_unknown=0, counts=[]
                                ),
                                players_armored=ArmoredPlayers(
                                    m=0, rounds_unknown=0, counts=[]
                                ),
                                deaths=DeathReport(m=0, rounds_missing=1),
                            )
                        ],
                    )
                ],
            )
        ],
    )


def test_report_round_trips_through_json() -> None:
    """``render`` lukee juuri sen, minkä ``aggregate`` kirjoitti."""
    report = full_report()
    again = Report.model_validate_json(report.model_dump_json())
    assert again == report
    assert again.schema_version == REPORT_SCHEMA_VERSION


def test_report_rejects_an_unknown_field() -> None:
    """Tuntematon kenttä on virhe: hiljainen ohitus veisi luvun mukanaan.

    Nimi vaihtui Story 2.5:ssä: ``anomalies`` on nyt mallin oma kenttä, joten
    se ei enää ole tuntematon. Vartija tarvitsee nimen, jota mallissa **ei
    ole** -- muuten se lakkaisi mittaamasta mitään sinä hetkenä, kun
    seuraava luku lisätään.
    """
    data = full_report().model_dump(mode="json")
    data["kentta_jota_ei_ole"] = []
    with pytest.raises(ValidationError):
        Report.model_validate(data)


def test_report_is_frozen() -> None:
    """Sopimus ei ole työtila: lukua ei korjata lukiessa."""
    report = full_report()
    with pytest.raises(ValidationError):
        report.unclassified_rounds = 0


def test_the_three_a_two_b_row_is_readable_without_further_arithmetic() -> None:
    """Tavoiteanalyysin rivi *"3A ja 2B"* otantoineen suoraan mallista."""
    report = full_report()
    areas = report.maps[0].sides[0].round_types[0].positions[0].areas
    found = {a.area: (a.players_dist[0].players, a.players_dist[0].n, a.m) for a in areas}
    assert found["BombsiteA"] == (3, 1, 1)
    assert found["BombsiteB"] == (2, 1, 1)


# --- Tasojen valiset summat ------------------------------------------------------


def side_with(rounds: int) -> SideReport:
    """Puoli, jolla on yksi kierrostyyppi ja annettu maara kierroksia."""
    return SideReport(
        side="T",
        sample=sample(unknown=rounds),
        round_types=[
            RoundTypeReport(
                round_type="pistol",
                sample=sample(unknown=rounds),
                small_sample=False,
                positions=[],
                utility=[],
                utility_counts=[],
                players_armed=ArmedPlayers(m=0, rounds_unknown=0, counts=[]),
                players_armored=ArmoredPlayers(m=0, rounds_unknown=0, counts=[]),
                first_contact=[],
                deaths=DeathReport(m=0, rounds_missing=rounds),
            )
        ],
    )


def test_a_side_must_be_the_sum_of_its_round_types() -> None:
    """Juuri tasojen valissa kadonnut kierros ei nay yhdessakaan lehdessa."""
    with pytest.raises(AggregateError, match="tasolla puoli"):
        SideReport(
            side="T",
            sample=sample(unknown=5),
            round_types=[
                RoundTypeReport(
                    round_type="pistol",
                    sample=sample(unknown=2),
                    small_sample=True,
                    positions=[],
                    utility=[],
                    utility_counts=[],
                    players_armed=ArmedPlayers(m=0, rounds_unknown=0, counts=[]),
                    players_armored=ArmoredPlayers(m=0, rounds_unknown=0, counts=[]),
                    first_contact=[],
                    deaths=DeathReport(m=0, rounds_missing=2),
                )
            ],
        )


def test_a_map_must_be_the_sum_of_its_sides() -> None:
    with pytest.raises(AggregateError, match="tasolla kartta"):
        MapReport(
            map_name="de_nuke",
            map_name_source="map_demo_id",
            map_demo_ids=["Nuke_vs_a"],
            sample=sample(unknown=9),
            sides=[side_with(3)],
        )


def test_a_map_must_list_exactly_its_own_demos() -> None:
    with pytest.raises(AggregateError, match="listaa"):
        MapReport(
            map_name="de_nuke",
            map_name_source="map_demo_id",
            map_demo_ids=["Nuke_vs_a", "Nuke_vs_b"],
            sample=sample(unknown=3),
            sides=[side_with(3)],
        )


def test_a_round_moving_between_buckets_is_caught() -> None:
    """Yhteissumma voi tasmata vaikka kierros vaihtaisi lokeroa."""
    with pytest.raises(AggregateError, match="lokerossa"):
        SideReport(
            side="T",
            sample=Sample(
                demos=1,
                rounds=3,
                league=SampleBucket(demos=1, rounds=3),
                other=SampleBucket(demos=0, rounds=0),
                unknown=SampleBucket(demos=0, rounds=0),
            ),
            round_types=[
                RoundTypeReport(
                    round_type="pistol",
                    sample=sample(unknown=3),
                    small_sample=False,
                    positions=[],
                    utility=[],
                    utility_counts=[],
                    players_armed=ArmedPlayers(m=0, rounds_unknown=0, counts=[]),
                    players_armored=ArmoredPlayers(m=0, rounds_unknown=0, counts=[]),
                    first_contact=[],
                    deaths=DeathReport(m=0, rounds_missing=3),
                )
            ],
        )


def test_a_report_must_be_the_sum_of_its_maps() -> None:
    with pytest.raises(AggregateError, match="tasolla raportti"):
        Report(
            generated_at=datetime(2026, 8, 30, tzinfo=UTC),
            team=team(),
            sample=sample(unknown=7),
            anomaly_scan=scan(),
            maps=[
                MapReport(
                    map_name="de_nuke",
                    map_name_source="map_demo_id",
                    map_demo_ids=["Nuke_vs_a"],
                    sample=sample(unknown=3),
                    sides=[side_with(3)],
                )
            ],
        )


def test_unclassified_rounds_stay_outside_the_sample() -> None:
    """Kierros ilman tyyppia ei mahdu rakenteeseen eika siis otantaan."""
    report = Report(
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
        team=team(),
        sample=sample(unknown=3),
        unclassified_rounds=4,
        anomaly_scan=scan(rounds_scanned=3),
        maps=[
            MapReport(
                map_name="de_nuke",
                map_name_source="map_demo_id",
                map_demo_ids=["Nuke_vs_a"],
                sample=sample(unknown=3),
                sides=[side_with(3)],
            )
        ],
    )
    assert report.sample.rounds == 3
    assert report.unclassified_rounds == 4


# --- DeathReport (Story 2.7) ---------------------------------------------------


def test_the_first_death_distribution_must_sum_to_its_sample() -> None:
    """``Σ n = m``. Ilman tätä luku ei tarkoita mitään."""
    with pytest.raises(AggregateError, match="ensimmäisen kuoleman alueissa"):
        DeathReport(
            m=3,
            rounds_missing=0,
            first_death_areas=[FirstDeathArea(area="Cave", n=2, m=3)],
        )


def test_every_first_death_area_shares_the_same_sample() -> None:
    """Kaksi eri nimittäjää samassa jakaumassa eivät ole vertailukelpoisia."""
    with pytest.raises(AggregateError, match="väittää otannakseen"):
        DeathReport(
            m=2,
            rounds_missing=0,
            first_death_areas=[
                FirstDeathArea(area="Cave", n=1, m=2),
                FirstDeathArea(area="Long", n=1, m=1),
            ],
        )


def test_the_same_first_death_area_may_not_appear_twice() -> None:
    with pytest.raises(ValueError, match="sama alue kahdesti"):
        DeathReport(
            m=2,
            rounds_missing=0,
            first_death_areas=[
                FirstDeathArea(area="Cave", n=1, m=2),
                FirstDeathArea(area="Cave", n=1, m=2),
            ],
        )


def test_a_first_death_area_cannot_exceed_the_rounds() -> None:
    with pytest.raises(AggregateError, match="Ensimmäisen kuoleman alue"):
        FirstDeathArea(area="Cave", n=3, m=2)


def test_the_kill_distribution_must_sum_to_the_kills() -> None:
    """Tappojakauman nimittäjä on **tappoja**, ja summan on täsmättävä."""
    with pytest.raises(AggregateError, match="tappoalueissa"):
        DeathReport(
            m=0,
            rounds_missing=0,
            kills_total=5,
            kills=[KillArea(area="Middle", n=4, m=5)],
        )


def test_every_kill_area_shares_the_same_denominator() -> None:
    with pytest.raises(AggregateError, match="Tappoalue"):
        DeathReport(
            m=0,
            rounds_missing=0,
            kills_total=3,
            kills=[
                KillArea(area="Middle", n=2, m=3),
                KillArea(area="Long", n=1, m=1),
            ],
        )


def test_the_same_kill_area_may_not_appear_twice() -> None:
    with pytest.raises(ValueError, match="sama alue kahdesti"):
        DeathReport(
            m=0,
            rounds_missing=0,
            kills_total=2,
            kills=[
                KillArea(area="Middle", n=1, m=2),
                KillArea(area="Middle", n=1, m=2),
            ],
        )


def test_a_kill_area_cannot_exceed_the_kills() -> None:
    with pytest.raises(AggregateError, match="Tappoalue"):
        KillArea(area="Middle", n=3, m=2)


def test_a_median_without_a_single_death_is_refused() -> None:
    """Mediaani ilman havaintoja olisi luku tyhjästä."""
    with pytest.raises(AggregateError, match="mediaani"):
        DeathReport(m=0, rounds_missing=4, first_death_seconds_median=24.0)


def test_the_kill_sample_may_exceed_the_round_count() -> None:
    """Tappoja on yleensä enemmän kuin kierroksia -- se **ei** ole virhe.

    Tämä on toinen puoli edellisistä vartijoista: jos ``Σ n = m`` sidottaisiin
    kierroksiin, oikea aineisto kaatuisi jokaisella kierrostyypillä.
    """
    entry = DeathReport(
        m=2,
        rounds_missing=0,
        first_death_areas=[FirstDeathArea(area="Cave", n=2, m=2)],
        kills_total=9,
        kills=[KillArea(area="Middle", n=9, m=9)],
    )
    assert entry.kills_total > entry.m


def test_an_empty_death_report_is_a_valid_result() -> None:
    """Kierrostyyppi, jolla joukkue ei kuollut eikä tappanut, on kelvollinen."""
    entry = DeathReport(m=0, rounds_missing=3)
    assert entry.first_death_areas == []
    assert entry.kills == []
    assert entry.first_death_seconds_median is None


def test_the_round_type_report_requires_its_death_block() -> None:
    """Ei oletusta: tyhjä oletus näyttäisi kierrostyypiltä, jolla ei kuoltu.

    Juuri se ero on syy skeemaversion nostoon -- vanha ``report.json`` ei saa
    validoitua tätä mallia vasten hiljaa.
    """
    with pytest.raises(ValidationError, match="deaths"):
        RoundTypeReport(
            round_type="pistol",
            sample=sample(unknown=1),
            small_sample=True,
            positions=[],
            utility=[],
            utility_counts=[],
            players_armed=ArmedPlayers(m=0, rounds_unknown=0, counts=[]),
            players_armored=ArmoredPlayers(m=0, rounds_unknown=0, counts=[]),
            first_contact=[],
        )


def test_the_round_type_report_requires_its_armored_block() -> None:
    """Ei oletusta panssarijakaumalle: tyhjä näyttäisi kevlarittomalta.

    Sama peruste kuin kuolemilla, ja sama seuraus: vanha ``report.json`` ei
    saa validoitua tätä mallia vasten hiljaa, joten skeemaversio nousee.
    """
    with pytest.raises(ValidationError, match="players_armored"):
        RoundTypeReport(
            round_type="pistol",
            sample=sample(unknown=1),
            small_sample=True,
            positions=[],
            utility=[],
            utility_counts=[],
            players_armed=ArmedPlayers(m=0, rounds_unknown=0, counts=[]),
            first_contact=[],
            deaths=DeathReport(m=0, rounds_missing=1),
        )


def test_the_armored_distribution_must_add_up_to_its_sample() -> None:
    """``Σ n = m`` myös panssarijakaumassa -- muuten otanta valehtelisi."""
    with pytest.raises(AggregateError, match="panssaroitujen"):
        ArmoredPlayers(
            m=3, rounds_unknown=0, counts=[ArmoredCount(armored=5, n=2)]
        )


def test_the_two_player_distributions_use_different_field_names() -> None:
    """``armed`` ja ``armored`` ovat eri kenttiä, myös JSONissa.

    ``report.json`` luetaan käsin, ja sama kenttänimi kahdessa lähes
    samannimisessä jakaumassa on juuri se sekaannus, jonka tämä story korjaa.
    """
    armed = ArmedPlayers(
        m=1, rounds_unknown=0, counts=[ArmedCount(armed=0, n=1)]
    ).model_dump(mode="json")
    armored = ArmoredPlayers(
        m=1, rounds_unknown=0, counts=[ArmoredCount(armored=5, n=1)]
    ).model_dump(mode="json")

    assert armed["counts"][0] == {"armed": 0, "n": 1}
    assert armored["counts"][0] == {"armored": 5, "n": 1}


def test_the_schema_version_says_the_structure_changed() -> None:
    """Vanha raportti ei validoidu = versio nousee.

    Ehto ei ole "tuliko uusi kenttä" vaan "validoituuko vanha tiedosto".
    Story 2.9 ei tuonut yhtään kenttää: se poisti ``AreaSource``-luettelosta
    arvon ``snapped``, ja se riittää -- vanha ``report.json`` kaatuisi
    muuten pydanticin virheeseen sen sijaan että ``render`` kertoisi
    aggregoinnin olevan ajettava uudelleen.

    Story 2.11 on sama kuvio toisin päin: ``map_name_source`` sai arvon
    ``demo_header``, joten **uusi** tiedosto ei kelpaa vanhalle mallille --
    ja vanhan tiedoston kartat on ryhmitelty eri säännöllä kuin tämän
    version, koska nimi luetaan nyt demon otsikosta.

    Story 2.5 laajentaa ehtoa kolmannella tapauksella: ``anomalies`` on
    oletukseltaan tyhjä lista, joten vanha tiedosto **validoituisi** -- ja
    juuri siksi versio nousee. Tyhjä poikkeamaluku on tässä mallissa
    havainto ("ei poikkeamia"), joten vanhasta tiedostosta renderöity
    raportti väittäisi mitatuksi tulokseksi sen, ettei sääntöjä ollut
    olemassa.

    Story 2.14 osuu molempiin haaroihin yhtä aikaa: ``Anomaly.rule`` sai
    arvon ``stack``, joten **uusi** tiedosto ei kelpaa vanhalle mallille --
    ja vanha tiedosto kelpaisi, mutta sen kattavuus nimeäisi stackin
    toteuttamattomaksi ja vaikenisi siitä, monellako kierroksella se voi
    osua.
    """
    assert REPORT_SCHEMA_VERSION == "8.0.0"


def test_the_map_name_source_covers_all_three_sources() -> None:
    """Lähteitä on kolme, ja jokainen on kelvollinen arvo (Story 2.11).

    Ensisijaisuusjärjestys on ``demo_header`` -> ``map_demo_id`` ->
    ``unknown``, ja molemmat vanhat arvot säilyvät toimivina: arkistossa on
    demoja, joiden otsikossa ei ole karttaa.
    """
    def _map(source: str) -> MapReport:
        return MapReport(
            map_name="de_ancient",
            map_name_source=source,
            map_demo_ids=["Ancient_vs_a"],
            sample=sample(unknown=1),
            sides=[
                SideReport(
                    side="T",
                    sample=sample(unknown=1),
                    round_types=[
                        _round_type_with(
                            DeathReport(m=0, rounds_missing=1), rounds=1
                        )
                    ],
                )
            ],
        )

    for source in ("demo_header", "map_demo_id", "unknown"):
        assert _map(source).map_name_source == source

    with pytest.raises(ValidationError):
        _map("tiedostonimi")


def _round_type_with(entry: DeathReport, rounds: int) -> RoundTypeReport:
    """Kierrostyyppi annetulla kuolemaosuudella ja otannalla."""
    return RoundTypeReport(
        round_type="pistol",
        sample=sample(unknown=rounds),
        small_sample=False,
        positions=[],
        utility=[],
        utility_counts=[],
        players_armed=ArmedPlayers(m=0, rounds_unknown=0, counts=[]),
        players_armored=ArmoredPlayers(m=0, rounds_unknown=0, counts=[]),
        first_contact=[],
        deaths=entry,
    )


def test_the_deaths_must_cover_the_round_types_whole_sample() -> None:
    """Kuolemien kierrokset ovat täsmälleen kierrostyypin kierrokset.

    ``Σ n = m`` pitää myös silloin, kun ``m`` on laskettu **väärästä
    kierrosjoukosta**: jakauma olisi sisäisesti johdonmukainen ja hiljaa
    väärä. Ilman tätä ristiintarkistusta väärä ``round_keys`` tuottaisi
    raportin, jonka jokainen lehti näyttää oikealta.
    """
    covers_three = DeathReport(
        m=1,
        rounds_missing=2,
        first_death_seconds_median=20.0,
        first_death_areas=[FirstDeathArea(area="Cave", n=1, m=1)],
    )
    with pytest.raises(AggregateError, match="kuolemat kattavat 3 kierrosta"):
        _round_type_with(covers_three, rounds=4)


def test_a_death_block_that_covers_the_sample_is_accepted() -> None:
    """Vartijan toinen haara: oikein laskettu osuus menee läpi.

    Ilman tätä edellinen testi todistaisi vain, että jokin nostaa
    poikkeuksen.
    """
    entry = _round_type_with(
        DeathReport(
            m=1,
            rounds_missing=3,
            first_death_seconds_median=20.0,
            first_death_areas=[FirstDeathArea(area="Cave", n=1, m=1)],
        ),
        rounds=4,
    )
    assert entry.deaths.m + entry.deaths.rounds_missing == entry.sample.rounds


def test_too_many_covered_rounds_is_refused_too() -> None:
    """Ero kumpaankin suuntaan on sama vika, ja molemmat on estettävä."""
    with pytest.raises(AggregateError, match="kuolemat kattavat 5 kierrosta"):
        _round_type_with(DeathReport(m=0, rounds_missing=5), rounds=4)


# --- Poikkeamat (Story 2.5) -----------------------------------------------------


def _point(seconds: float = 30.0, players: int = 2, alive=None) -> AnomalyPoint:
    """Yksi näytepiste havaintoineen."""
    return AnomalyPoint(sample_t_s=seconds, players=players, alive=alive)


def _round(**overrides) -> AnomalyRound:
    """Kierrosrivi, jonka oletukset ovat kalibroinnin Ancient k18.

    ``seconds`` ja ``players`` ovat apurin omia oikoteitä yhden näytepisteen
    tapaukseen; useamman pisteen testi antaa ``points``in itse.
    """
    seconds = overrides.pop("seconds", [30.0])
    players = overrides.pop("players", 2)
    alive = overrides.pop("alive", None)
    values: dict[str, object] = {
        "map_demo_id": "demo",
        "round_no": 18,
        "round_type": "eco",
        "points": [_point(value, players, alive) for value in seconds],
    }
    values.update(overrides)
    return AnomalyRound(**values)


def _anomaly(**overrides) -> Anomaly:
    """Poikkeama, jonka oletukset ovat kalibroinnin Ancient k18."""
    values: dict[str, object] = {
        "rule": "ct_advance",
        "map_name": "de_ancient",
        "map_name_source": "demo_header",
        "side": "CT",
        "area": "TSideLower",
        "round_types": ["eco"],
        "rounds": [_round()],
        "orientation": [
            AreaOrientation(map_demo_id="demo", t_share=0.88, observations=24)
        ],
        "players_max": 2,
        "n": 1,
        "m": 3,
    }
    values.update(overrides)
    return Anomaly(**values)


def _crunch(**overrides) -> Anomaly:
    """Crunch-rivi: lähtöalueet ovat pakollisia."""
    values: dict[str, object] = {
        "rule": "crunch",
        "round_types": ["eco"],
        "rounds": [_round(sources=["Ramp", "Squeaky"])],
    }
    values.update(overrides)
    return _anomaly(**values)


def test_an_anomaly_carries_everything_the_report_line_needs() -> None:
    """Mitä, missä, milloin, kuinka usein -- ja millä perusteella."""
    anomaly = _anomaly()
    assert anomaly.area == "TSideLower"
    assert anomaly.rounds[0].seconds == [30.0]
    assert anomaly.rounds[0].round_no == 18
    assert anomaly.orientation[0].t_share == 0.88
    assert (anomaly.n, anomaly.m) == (1, 3)
    assert anomaly.rounds[0].sources == []
    assert anomaly.small_sample is False


def test_an_anomaly_cannot_appear_in_more_rounds_than_exist() -> None:
    with pytest.raises(AggregateError, match="esiintyy 4 kierroksella"):
        _anomaly(
            n=4,
            m=3,
            rounds=[_round(round_no=n) for n in (1, 2, 3, 4)],
        )


def test_the_sample_must_be_the_length_of_the_round_list() -> None:
    """Luku ja sen todisteet eivät voi olla eri kokoisia."""
    with pytest.raises(AggregateError, match="kantaa 1 kierrosriviä"):
        _anomaly(n=2, m=3)


def test_the_same_round_cannot_appear_twice() -> None:
    with pytest.raises(ValidationError, match="sama kierros kahdesti"):
        _anomaly(n=2, m=3, rounds=[_round(), _round()])


def test_a_round_needs_at_least_one_sample_point() -> None:
    """Ilman näytepistettä havainnolla ei ole 'milloin'."""
    with pytest.raises(ValidationError):
        _round(seconds=[])


def test_a_repeated_sample_point_is_refused() -> None:
    with pytest.raises(ValidationError, match="näytepisteet toistuvat"):
        _round(seconds=[30.0, 30.0])


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_an_impossible_sample_point_is_refused(value: float) -> None:
    """NaN läpäisisi jokaisen vertailun ja päätyisi raporttiin muodossa 'nan'."""
    with pytest.raises(ValidationError):
        _round(seconds=[value])


def test_unsorted_sample_points_are_refused() -> None:
    """Rivi luetaan aikajärjestyksessä, joten järjestys on osa sopimusta."""
    with pytest.raises(ValidationError, match="nousevassa järjestyksessä"):
        _round(seconds=[30.0, 15.0])


def test_a_repeated_source_area_is_refused() -> None:
    with pytest.raises(ValidationError, match="lähtöalueet toistuvat"):
        _round(sources=["Ramp", "Ramp"])


def test_a_nameless_source_area_is_refused() -> None:
    """Nimetön alue ei ole suunta."""
    with pytest.raises(ValidationError, match="nimetön alue"):
        _round(sources=["Ramp", "  "])


def test_more_sources_than_players_is_refused() -> None:
    """Jokainen suunta tarvitsee oman pelaajansa -- mahdoton havainto."""
    with pytest.raises(ValidationError, match="Jokainen suunta"):
        _round(players=2, sources=["A", "B", "C"])


def test_two_sample_points_keep_their_own_player_counts() -> None:
    """**Maksimi ei saa palata.**

    Kierros, jolla 15 s kohdalla on viisi pelaajaa ja 30 s kohdalla yksi, on
    kaksi havaintoa eikä yksi. Aiemmin rakenne kantoi yhden maksimin ja
    luettelon näytepisteitä, ja raportin rivi latoi maksimin molemmille --
    mitattuna juuri tämä kierros on MatureMayhem Inferno k2.
    """
    entry = _round(points=[_point(15.0, 5), _point(30.0, 1)])
    assert [(p.sample_t_s, p.players) for p in entry.points] == [
        (15.0, 5),
        (30.0, 1),
    ]
    assert entry.players_max == 5
    assert entry.seconds == [15.0, 30.0]


def test_a_point_cannot_have_more_players_than_are_alive() -> None:
    """Ryhmässä olevat ovat osajoukko elossa olevista."""
    with pytest.raises(ValidationError, match="osajoukko elossa olevista"):
        _point(15.0, players=5, alive=4)


def test_the_alive_count_is_on_every_point_or_none() -> None:
    """Puolikas rivi latoisi kaksi eri yksikköä samalle riville."""
    with pytest.raises(ValidationError, match="joko kaikilla tai ei"):
        AnomalyRound(
            map_demo_id="demo",
            round_no=4,
            round_type="eco",
            points=[_point(15.0, 4, 5), _point(30.0, 4)],
        )


def _stack(**overrides) -> Anomaly:
    """Stack-rivi: siteryhmä ja elossa olevat pakollisia, orientaatio kielletty."""
    values: dict[str, object] = {
        "rule": "stack",
        "area": "BombsiteB",
        "site": "B",
        "orientation": [],
        "rounds": [_round(round_no=13, seconds=[15.0], players=4, alive=5)],
        "players_max": 4,
    }
    values.update(overrides)
    return _anomaly(**values)


def test_a_stack_carries_its_site_group_and_survivors() -> None:
    """Kolme kenttää, jotka erottavat stackin kahdesta muusta säännöstä."""
    stack = _stack()
    assert (stack.site, stack.area) == ("B", "BombsiteB")
    assert stack.rounds[0].points[0].alive == 5
    assert stack.orientation == []


def test_a_stack_whose_site_and_area_disagree_is_refused() -> None:
    """Kenttä on rakenteessa vain siksi, ettei lukijan tarvitse päätellä."""
    with pytest.raises(ValidationError, match="eri mieltä"):
        _stack(site="A")


def test_a_stack_without_a_site_group_is_refused() -> None:
    """Ryhmä on rivin ankkuri, eikä sitä voi jättää nimeämättä."""
    with pytest.raises(ValidationError, match="siteryhmäksi"):
        _stack(site=None)


def test_a_stack_without_the_alive_count_is_refused() -> None:
    """Neljä viidestä ja neljä neljästä ovat eri havainto."""
    with pytest.raises(ValidationError, match="montako pelaajaa oli elossa"):
        _stack(rounds=[_round(round_no=13, seconds=[15.0], players=4)])


def test_a_stack_that_carries_orientation_is_refused() -> None:
    """Sääntö ei lue T-osuutta, joten sen kantaminen olisi keksitty luku."""
    with pytest.raises(ValidationError, match="kantaa alueen orientaatiota"):
        _stack(
            orientation=[
                AreaOrientation(
                    map_demo_id="demo", t_share=0.88, observations=24
                )
            ]
        )


def test_an_orientation_rule_cannot_carry_a_site_group() -> None:
    """Vain stack lukee siteryhmiä."""
    with pytest.raises(ValidationError, match="vain stack lukee"):
        _anomaly(site="A")


def test_an_orientation_rule_cannot_carry_the_alive_count() -> None:
    """Kumpikaan orientaatiosääntö ei laske elossa olevia."""
    with pytest.raises(ValidationError, match="elossa olevien määrän"):
        _anomaly(rounds=[_round(alive=5)])


def test_a_crunch_without_source_areas_is_refused() -> None:
    """Crunch on määritelmällisesti saapumista useasta suunnasta."""
    with pytest.raises(ValidationError, match="ilman lähtöalueita"):
        _anomaly(rule="crunch")


def test_an_advance_with_source_areas_is_refused() -> None:
    """Etenemissääntö ei laske suuntia, joten se ei voi kantaa niitä."""
    with pytest.raises(ValidationError, match="kantaa lähtöalueita"):
        _anomaly(rounds=[_round(sources=["Ramp", "Squeaky"])])


def test_an_anomaly_without_an_area_is_refused() -> None:
    """Alue ilman nimeä ei voi olla T:n aluetta."""
    with pytest.raises(ValidationError):
        _anomaly(area="")


def test_an_anomaly_needs_an_orientation() -> None:
    """Poikkeaman perusteena on aina alueen mitattu T-osuus."""
    with pytest.raises(ValidationError):
        _anomaly(orientation=[])


def test_the_same_demo_cannot_give_an_area_two_shares() -> None:
    with pytest.raises(ValidationError, match="sama demo"):
        _anomaly(
            orientation=[
                AreaOrientation(map_demo_id="demo", t_share=0.88, observations=24),
                AreaOrientation(map_demo_id="demo", t_share=0.84, observations=37),
            ]
        )


def test_the_orientation_must_cover_exactly_the_observed_demos() -> None:
    """Todistuskappale ei saa kattaa enempää eikä vähempää kuin havainnot."""
    with pytest.raises(ValidationError, match="orientaatio kattaa demot"):
        _anomaly(
            orientation=[
                AreaOrientation(map_demo_id="toinen", t_share=0.88, observations=24)
            ]
        )


def test_two_demos_may_give_an_area_two_shares() -> None:
    """Vartijan toinen haara: eri demot ovat eri havaintoja."""
    anomaly = _anomaly(
        n=2,
        m=3,
        rounds=[_round(map_demo_id="a"), _round(map_demo_id="b")],
        orientation=[
            AreaOrientation(map_demo_id="a", t_share=0.88, observations=24),
            AreaOrientation(map_demo_id="b", t_share=0.84, observations=37),
        ],
    )
    assert len(anomaly.orientation) == 2


def test_the_round_types_must_match_the_rounds() -> None:
    """Yhteenveto ei voi nimetä tyyppiä, jota yksikään kierros ei ole."""
    with pytest.raises(ValidationError, match="round_types on"):
        _crunch(round_types=["eco", "full"])


def test_the_round_types_cannot_leave_out_a_type_that_is_there() -> None:
    """Vartijan toinen suunta."""
    with pytest.raises(ValidationError, match="round_types on"):
        _crunch(
            n=2,
            m=4,
            round_types=["eco"],
            rounds=[
                _round(round_no=1, sources=["A", "B"]),
                _round(round_no=2, round_type="full", sources=["A", "B"]),
            ],
        )


def test_a_crunch_may_span_two_round_types() -> None:
    """Speksimuutos 2: crunchia ei avainnella kierrostyypin mukaan."""
    anomaly = _crunch(
        n=2,
        m=4,
        round_types=["eco", "full"],
        rounds=[
            _round(round_no=1, sources=["A", "B"]),
            _round(round_no=2, round_type="full", sources=["A", "B"]),
        ],
    )
    assert anomaly.round_types == ["eco", "full"]


def test_an_advance_cannot_span_two_round_types() -> None:
    """Eteneminen ryhmitellään kierrostyypin mukaan, joten tyyppi on yksi."""
    with pytest.raises(ValidationError, match="kierrostyyppiä"):
        _anomaly(
            n=2,
            m=4,
            round_types=["eco", "full"],
            rounds=[
                _round(round_no=1),
                _round(round_no=2, round_type="full"),
            ],
        )


def test_the_player_maximum_must_match_the_rounds() -> None:
    """Yhteenveto ei voi olla eri mieltä kuin rivit joista se on koottu."""
    with pytest.raises(ValidationError, match="players_max on 5"):
        _anomaly(players_max=5)


@pytest.mark.parametrize("share", [-0.01, 1.01])
def test_an_impossible_t_share_is_refused(share: float) -> None:
    with pytest.raises(ValidationError):
        AreaOrientation(map_demo_id="demo", t_share=share, observations=24)


def test_an_orientation_without_observations_is_refused() -> None:
    """Nolla havaintoa ei ole alue vaan alueen puuttuminen."""
    with pytest.raises(ValidationError):
        AreaOrientation(map_demo_id="demo", t_share=0.88, observations=0)


def test_an_unknown_rule_name_is_refused() -> None:
    """Sääntöjen luettelo on sopimusta, ei vapaa merkkijono.

    Nimi oli ``stack`` Story 2.14:ään asti. Sen jälkeen se on kelvollinen, ja
    testi meni läpi väärästä syystä -- virhe tuli stackin omista vartijoista
    eikä sääntönimen tarkistuksesta. Nimen on siksi oltava sellainen, jota
    ``ANOMALY_RULES`` ei tunne eikä ole tulossa tuntemaan.
    """
    with pytest.raises(ValidationError):
        _anomaly(rule="rotate")


def test_an_unknown_map_name_source_is_refused() -> None:
    """Lähteiden luettelo on jaettu kahden solmun kesken."""
    with pytest.raises(ValidationError):
        _anomaly(map_name_source="arvattu")


def test_the_map_name_sources_are_the_same_list_for_both_nodes() -> None:
    """Kahtena kirjoitettuna uusi lähde kelpaisi toisessa ja kaatuisi toisessa."""
    assert set(get_args(MapNameSource)) == set(MAP_NAME_SOURCES)


# --- Kattavuus (AnomalyScan) ----------------------------------------------------


def _scan(**overrides) -> AnomalyScan:
    values: dict[str, object] = {
        "rules": ["ct_advance", "crunch", "stack"],
        "rules_deferred": [],
        "rounds_scanned": 18,
        "crunch_rounds": 9,
        "advance_rounds": 4,
        "stack_rounds": 9,
    }
    values.update(overrides)
    return AnomalyScan(**values)


def test_the_scan_records_what_was_run() -> None:
    scan = _scan()
    assert scan.rules == ["ct_advance", "crunch", "stack"]
    assert scan.rules_deferred == []
    assert scan.demos_without_orientation == []
    assert scan.demos_without_site_groups == []


def test_the_coverage_is_not_optional() -> None:
    """Kolme lukua, joista yksikään ei saa puuttua.

    Puuttuva avain luettaisiin pydanticin oletuksella nollaksi, eli sokea
    piste luettaisiin mitattuna negatiivisena -- juuri se, mitä tämä solmu on
    olemassa estämään. ``stack_rounds`` oli hetken oletuksellinen, ja tämä
    testi on se, joka pitää sen pakollisena.
    """
    for missing in ("rounds_scanned", "crunch_rounds", "advance_rounds", "stack_rounds"):
        values = {
            "rules": ["ct_advance", "crunch", "stack"],
            "rounds_scanned": 4,
            "crunch_rounds": 4,
            "advance_rounds": 4,
            "stack_rounds": 4,
        }
        del values[missing]
        with pytest.raises(ValidationError):
            AnomalyScan(**values)


def test_the_stack_coverage_cannot_exceed_the_ct_rounds() -> None:
    """Stack tutkii CT-kierroksia, joten se ei voi nähdä niitä useampaa."""
    with pytest.raises(AggregateError, match="suurempi kuin"):
        _scan(crunch_rounds=9, stack_rounds=10)


def test_a_gap_in_the_stack_coverage_must_have_a_named_cause() -> None:
    """Erotus syntyy vain vaiennetusta demosta, joten se on nimettävä.

    Nimeämätön erotus lukisi mitattuna negatiivisena: lukija näkisi pienemmän
    nimittäjän eikä mikään kertoisi, mikä siitä putosi.
    """
    with pytest.raises(AggregateError, match="siteryhmättömäksi"):
        _scan(crunch_rounds=9, stack_rounds=4)
    # Sama erotus nimetyllä syyllä kelpaa.
    assert (
        _scan(
            crunch_rounds=9,
            stack_rounds=4,
            demos_without_site_groups=["Nuke_vs_a"],
        ).stack_rounds
        == 4
    )


def test_the_scan_refuses_the_same_silenced_demo_twice() -> None:
    """Sama demo kahdesti lupaisi kaksi sokeaa pistettä yhdestä."""
    with pytest.raises(ValidationError, match="siteryhmättömien listassa"):
        _scan(demos_without_site_groups=["Nuke_vs_a", "Nuke_vs_a"])


def test_the_coverage_numbers_must_be_nested() -> None:
    """CT-säästökierrokset ⊆ CT-kierrokset ⊆ kaikki kierrokset.

    Väärä järjestys tarkoittaisi, että kattavuus lupaa säännölle enemmän
    kierroksia kuin sääntö voi tutkia.
    """
    with pytest.raises(AggregateError, match="eivät ole sisäkkäisiä"):
        _scan(rounds_scanned=4, crunch_rounds=5, advance_rounds=1)
    with pytest.raises(AggregateError, match="eivät ole sisäkkäisiä"):
        _scan(rounds_scanned=9, crunch_rounds=4, advance_rounds=5)


def test_the_scan_refuses_an_unknown_rule() -> None:
    """Nimi, jota ``ANOMALY_RULES`` ei tunne, ei kelpaa ajetuksi säännöksi.

    Nimi oli ennen Story 2.14:ää ``stack``, joka on nyt toteutettu. Se on
    juuri se syy, miksi testi ei saa nimetä sääntöä, joka **voi** joskus
    olla olemassa: kelvoton arvo on tässä nimenomaan sellainen, jota
    luettelossa ei ole eikä ole tulossa.
    """
    with pytest.raises(ValidationError):
        _scan(rules=["rotate"])


def test_the_scan_refuses_the_same_rule_twice() -> None:
    with pytest.raises(ValidationError, match="Sama sääntö kahdesti"):
        _scan(rules=["crunch", "crunch"])


def test_the_scan_refuses_the_same_blind_demo_twice() -> None:
    with pytest.raises(ValidationError, match="Sama demo kahdesti"):
        _scan(demos_without_orientation=["a", "a"])


def test_the_scan_needs_at_least_one_rule() -> None:
    """Nolla sääntöä ei ole kattavuus vaan ajon puuttuminen."""
    with pytest.raises(ValidationError):
        _scan(rules=[])


# --- Poikkeamat raportin tasolla ------------------------------------------------


def test_an_anomaly_cannot_name_a_map_that_is_not_in_the_report() -> None:
    """Lukija etsisi karttalukua, jota ei kirjoitettu."""
    with pytest.raises(AggregateError, match="nimeää kartan, jota raportissa"):
        _report_with_anomalies([_anomaly(map_name="de_train")])


def test_two_anomalies_cannot_share_the_grouping_key() -> None:
    """Sama havainto kahdesti eri otannoilla on juuri se, minkä ryhmittely estää."""
    with pytest.raises(AggregateError, match="kahdesti"):
        _report_with_anomalies([_anomaly(), _anomaly(m=4)])


def test_the_same_area_on_two_rules_is_not_a_duplicate() -> None:
    """Sääntö on osa avainta: eteneminen ja crunch ovat eri rivejä."""
    report = _report_with_anomalies([_anomaly(), _crunch()])
    assert len(report.anomalies) == 2


def test_the_same_area_on_two_round_types_is_not_a_duplicate() -> None:
    """Kierrostyyppi on osa etenemisen avainta."""
    report = _report_with_anomalies(
        [
            _anomaly(),
            _anomaly(
                round_types=["force"],
                rounds=[_round(round_type="force")],
            ),
        ]
    )
    assert len(report.anomalies) == 2


def test_a_stale_report_json_is_validated_the_same_way() -> None:
    """``model_validate`` on vanhentuneen tiedoston reitti, ei konstruktori.

    Validaattorin testaaminen vain konstruktorin kautta jättäisi juuri sen
    reitin auki, jolla rikkinäinen ``report.json`` päätyy ``render``iin.
    """
    report = _report_with_anomalies([_anomaly()])
    data = report.model_dump(mode="json")
    data["anomalies"][0]["n"] = 9
    with pytest.raises(AggregateError, match="kantaa 1 kierrosriviä"):
        Report.model_validate(data)


def test_a_report_without_the_scan_is_refused() -> None:
    """Kattavuus on pakollinen: ilman sitä tyhjä luku ei erotu ajamattomasta."""
    report = _report_with_anomalies([])
    data = report.model_dump(mode="json")
    data.pop("anomaly_scan")
    with pytest.raises(ValidationError):
        Report.model_validate(data)
