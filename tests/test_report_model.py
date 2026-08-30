"""``domain.report`` -- raporttimallin testit.

Malli on ``aggregate``-vaiheen ja ``render``-vaiheen jaettu sopimus, joten sen
tarkistukset ovat osa sopimusta eivätkä koristetta: epäkelpoa raporttia ei saa
voida rakentaa edes muistiin. Nämä testit eivät tarvitse tauluja, arkistoa
eivätkä demoja.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

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


def test_report_round_trips_through_json() -> None:
    """``render`` lukee juuri sen, minkä ``aggregate`` kirjoitti."""
    report = full_report()
    again = Report.model_validate_json(report.model_dump_json())
    assert again == report
    assert again.schema_version == REPORT_SCHEMA_VERSION


def test_report_rejects_an_unknown_field() -> None:
    """Tuntematon kenttä on virhe: hiljainen ohitus veisi luvun mukanaan."""
    data = full_report().model_dump(mode="json")
    data["anomalies"] = []
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
    """
    assert REPORT_SCHEMA_VERSION == "5.0.0"


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
