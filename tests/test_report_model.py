"""``domain.report`` -- the report model's tests.

The model is the shared contract of the ``aggregate`` stage and the ``render``
stage, so its checks are part of the contract and not decoration: it must not
be possible to build an invalid report even in memory. These tests need no
tables, no archive and no demos.
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
    RosterSample,
    RoundTypeReport,
    SLUG_FALLBACK,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
)
from pappascout.constants import ROSTER_BUCKETS
from pappascout.errors import AggregateError


def sample(league: int = 0, other: int = 0, unknown: int = 1) -> Sample:
    """A sample with one demo per bucket that has rounds."""
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


def roster_sample(
    full: int = 0, partial: int = 0, unknown: int = 1
) -> RosterSample:
    """Roster breakdown with one demo per bucket that has rounds.

    Mirrors :func:`sample` so a fixture can hand ``Report`` two breakdowns
    whose totals agree without restating the arithmetic at every call site.
    """
    buckets = {
        "full": SampleBucket(demos=1 if full else 0, rounds=full),
        "partial": SampleBucket(demos=1 if partial else 0, rounds=partial),
        "unknown": SampleBucket(demos=1 if unknown else 0, rounds=unknown),
    }
    return RosterSample(
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


# --- The team's name and roster -------------------------------------------------


def team_kwargs(**overrides: object) -> dict[str, object]:
    """A valid ``TeamReport`` a test breaks one thing at a time in."""
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
    """``team_key`` means there is no name; then the name is the id."""
    with pytest.raises(AggregateError, match="but the source as"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="team_key",
            )
        )


def test_alternatives_cannot_exist_without_an_observed_name() -> None:
    """Alternatives are observations: none without an observed name."""
    with pytest.raises(AggregateError, match="alternative names recorded"):
        TeamReport(
            **team_kwargs(
                display_name_source="team_key",
                display_name_alternatives=["MM Academy"],
            )
        )


def test_an_empty_string_cannot_be_the_observed_name() -> None:
    """An empty string is not a name -- then the source is ``team_key``."""
    with pytest.raises(AggregateError, match="recorded as an empty string"):
        TeamReport(
            **team_kwargs(display_name="   ", display_name_source="clan_name")
        )


def test_a_blank_alternative_is_refused() -> None:
    with pytest.raises(AggregateError, match="empty strings among its"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="clan_name",
                display_name_alternatives=["  "],
            )
        )


def test_a_repeated_alternative_is_refused() -> None:
    """The same name twice is not two observations."""
    with pytest.raises(AggregateError, match="repeated alternative names"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="clan_name",
                display_name_alternatives=["MM Academy", "MM Academy"],
            )
        )


def test_the_shown_name_cannot_be_its_own_alternative() -> None:
    with pytest.raises(AggregateError, match="among its own alternatives"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="maturemayhem",
                display_name_source="clan_name",
                display_name_alternatives=["MatureMayhem"],
            )
        )


def test_the_slug_must_follow_the_shown_name() -> None:
    """The slug ends up in the file name, so it must not disagree with the name."""
    with pytest.raises(AggregateError, match="The team's slug is"):
        TeamReport(
            **team_kwargs(
                display_name="MatureMayhem",
                slug="aaaaaaaaaaaaaaaa",
                display_name_source="clan_name",
            )
        )


def test_a_name_without_ascii_falls_back_to_the_key_not_to_a_shared_constant() -> None:
    """A Cyrillic clan: the fallback is the id, not the shared constant.

    A shared constant would give every such team the same file name, and the
    reports would then collide with each other.
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
    """An empty name beside the SteamID would read as an empty name."""
    assert RosterEntry(player_id="1", display_name="  ").display_name is None
    assert RosterEntry(player_id="1", display_name="Sassiz").display_name == "Sassiz"


# --- The sample -----------------------------------------------------------------


def test_sample_totals_must_match_the_buckets() -> None:
    """The total is the sum of the buckets, or two figures tell two stories.

    The exception is ``AggregateError`` as in every other sum check: the same
    fault must not produce two different kinds of exception, because the
    caller catches them as one.
    """
    with pytest.raises(AggregateError, match="The sample totals"):
        Sample(
            demos=9,
            rounds=1,
            league=SampleBucket(demos=0, rounds=0),
            other=SampleBucket(demos=0, rounds=0),
            unknown=SampleBucket(demos=1, rounds=1),
        )


def test_a_bucket_cannot_hold_rounds_without_a_demo() -> None:
    """Rounds are always some demo's rounds (Story 3.9).

    ``aggregate`` counts both from the same rows and cannot produce this, so a
    bucket like it comes from a ``report.json`` edited by hand. The guard is
    in the model rather than the view, so every reader of the field is covered
    and no view has to decide what the number means.
    """
    with pytest.raises(AggregateError, match="without a single demo"):
        SampleBucket(demos=0, rounds=3)


def test_a_demo_whose_rounds_all_fell_out_is_still_allowed() -> None:
    """The converse is a real state: a pruned branch, or no ``round_type``."""
    assert SampleBucket(demos=1, rounds=0).demos == 1


def test_the_totals_error_names_which_breakdown_failed() -> None:
    """One exception type, two breakdowns -- the name is the only signal."""
    with pytest.raises(AggregateError, match="league breakdown"):
        Sample(
            demos=9,
            rounds=0,
            league=SampleBucket(demos=0, rounds=0),
            other=SampleBucket(demos=0, rounds=0),
            unknown=SampleBucket(demos=0, rounds=0),
        )
    with pytest.raises(AggregateError, match="roster breakdown"):
        RosterSample(
            demos=9,
            rounds=0,
            full=SampleBucket(demos=0, rounds=0),
            partial=SampleBucket(demos=0, rounds=0),
            unknown=SampleBucket(demos=0, rounds=0),
        )


def test_unknown_bucket_exists_alongside_the_other_two() -> None:
    """Three buckets, not two: an empty ``is_league`` is not ``other``."""
    s = sample(unknown=12)
    assert s.unknown.rounds == 12
    assert s.other.rounds == 0
    assert s.league.rounds == 0
    assert s.rounds == 12


# --- Roster breakdown (Story 3.9) -----------------------------------------------


def test_the_roster_breakdown_totals_must_match_its_buckets() -> None:
    """Same rule and same exception type as the league breakdown."""
    with pytest.raises(AggregateError, match="The sample totals"):
        RosterSample(
            demos=9,
            rounds=1,
            full=SampleBucket(demos=0, rounds=0),
            partial=SampleBucket(demos=0, rounds=0),
            unknown=SampleBucket(demos=1, rounds=1),
        )


def test_the_roster_breakdown_has_three_buckets_too() -> None:
    """``5/5``, ``4/5`` and unknown. Two would force a claim nobody measured."""
    s = roster_sample(unknown=12)
    assert s.unknown.rounds == 12
    assert s.full.rounds == 0
    assert s.partial.rounds == 0
    assert s.rounds == 12


def test_the_roster_nodes_buckets_are_the_bucket_list() -> None:
    """The field names are not a hand-written parallel list.

    ``aggregate`` builds the node with ``**{name: ... for name in
    ROSTER_BUCKETS}``, so a field renamed on one side surfaces as a runtime
    ``AttributeError`` deep in validation rather than as a failing import.
    """
    fields = set(RosterSample.model_fields) - {"demos", "rounds"}
    assert fields == set(ROSTER_BUCKETS)


def _report_with_breakdowns(league: Sample, roster: RosterSample) -> Report:
    """A report whose only map repeats the league breakdown handed in.

    The whole map tree carries ``league`` itself, bucket for bucket, so that
    the per-level sum checks pass and the test measures the cross-breakdown
    check rather than one of those.
    """
    return Report(
        generated_at=datetime(2026, 9, 7, tzinfo=UTC),
        team=team(),
        sample=league,
        roster_sample=roster,
        anomaly_scan=scan(rounds_scanned=league.rounds),
        maps=[
            MapReport(
                map_name="de_nuke",
                map_name_source="map_demo_id",
                map_demo_ids=["Nuke_vs_a"],
                sample=league,
                sides=[
                    SideReport(
                        side="T",
                        sample=league,
                        round_types=[
                            RoundTypeReport(
                                round_type="pistol",
                                sample=league,
                                small_sample=False,
                                positions=[],
                                utility=[],
                                utility_counts=[],
                                players_armed=ArmedPlayers(
                                    m=0, rounds_unknown=0, counts=[]
                                ),
                                players_armored=ArmoredPlayers(
                                    m=0, rounds_unknown=0, counts=[]
                                ),
                                first_contact=[],
                                deaths=DeathReport(
                                    m=0, rounds_missing=league.rounds
                                ),
                            )
                        ],
                    )
                ],
            )
        ],
    )


def test_the_two_breakdowns_may_bucket_the_same_demo_differently() -> None:
    """Independent dimensions: a league map can be played with a stand-in."""
    report = _report_with_breakdowns(
        sample(league=3, unknown=0), roster_sample(partial=3, unknown=0)
    )
    assert report.sample.league.rounds == 3
    assert report.roster_sample.partial.rounds == 3


def test_a_report_whose_breakdowns_disagree_on_rounds_is_rejected() -> None:
    """A round in one breakdown and not the other is the fault this catches."""
    with pytest.raises(AggregateError, match="two breakdowns speak of"):
        _report_with_breakdowns(sample(unknown=3), roster_sample(unknown=2))


def test_a_report_whose_breakdowns_disagree_on_demos_is_rejected() -> None:
    """Equal rounds are not enough: a demo can fall out of a bucket alone."""
    roster = RosterSample(
        demos=2,
        rounds=3,
        full=SampleBucket(demos=1, rounds=1),
        partial=SampleBucket(demos=1, rounds=2),
        unknown=SampleBucket(demos=0, rounds=0),
    )
    with pytest.raises(AggregateError, match="two breakdowns speak of"):
        _report_with_breakdowns(sample(unknown=3), roster)


def test_the_roster_breakdown_is_required_not_defaulted() -> None:
    """An old ``report.json`` must fail rather than read as current.

    A default would have had to invent bucket counts, and the only honest
    invention -- everything ``unknown`` -- is indistinguishable from a
    measured all-unknown archive.
    """
    with pytest.raises(ValidationError, match="roster_sample"):
        Report(
            generated_at=datetime(2026, 9, 7, tzinfo=UTC),
            team=team(),
            sample=sample(unknown=1),
            anomaly_scan=scan(),
            maps=[],
        )


# --- Sigma n = m ----------------------------------------------------------------


def test_area_distribution_requires_the_sample_to_add_up() -> None:
    with pytest.raises(AggregateError, match="does not match in area"):
        AreaDistribution(
            area="BombsiteA",
            m=4,
            players_dist=[PlayersCount(players=3, n=1)],
        )


def test_area_distribution_with_a_zero_bucket_adds_up() -> None:
    """The zero bucket is what makes the sum right."""
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
    with pytest.raises(ValidationError, match="same player count twice"):
        AreaDistribution(
            area="Middle",
            m=2,
            players_dist=[
                PlayersCount(players=1, n=1),
                PlayersCount(players=1, n=1),
            ],
        )


def test_position_areas_must_share_the_positions_sample() -> None:
    """Two different samples from the same moment would not be comparable."""
    with pytest.raises(AggregateError, match="share the same sample"):
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
    with pytest.raises(AggregateError, match="for grenade type"):
        UtilityCounts(
            grenade_type="smoke", m=5, counts=[GrenadeCount(thrown=1, n=2)]
        )


def test_armed_players_require_the_sample_to_add_up() -> None:
    with pytest.raises(AggregateError, match="armed players' distribution"):
        ArmedPlayers(m=3, rounds_unknown=0, counts=[ArmedCount(armed=5, n=1)])


def test_armed_players_refuse_the_same_bar_twice() -> None:
    """B2: three distributions had the duplicate guard, two did not.

    The same rule as :class:`AreaDistribution`'s, :class:`UtilityCounts`'s and
    :class:`DeathReport`'s: one bar per player count. Without it ``Σ n = m``
    passes (3 + 1 = 4), but the report would set the same bar twice with
    different figures -- one observation read as two.
    """
    with pytest.raises(ValidationError, match="same player count twice"):
        ArmedPlayers(
            m=4,
            rounds_unknown=0,
            counts=[ArmedCount(armed=5, n=3), ArmedCount(armed=5, n=1)],
        )


def test_armored_players_refuse_the_same_bar_twice() -> None:
    """B2: the same gap, and it was **duplicated** along with the copying.

    :class:`ArmoredPlayers` was copied from :class:`ArmedPlayers` in Story
    2.8, so the missing guard came along instead of being noticed. Two tests
    and not one for exactly that reason: a shared test would have passed if
    only one of the classes were fixed.
    """
    with pytest.raises(ValidationError, match="same player count twice"):
        ArmoredPlayers(
            m=4,
            rounds_unknown=0,
            counts=[ArmoredCount(armored=0, n=3), ArmoredCount(armored=0, n=1)],
        )


def test_the_player_counter_distributions_still_accept_distinct_bars() -> None:
    """The guard's other direction: different bars are different observations."""
    armed = ArmedPlayers(
        m=4,
        rounds_unknown=0,
        counts=[ArmedCount(armed=0, n=3), ArmedCount(armed=5, n=1)],
    )
    armored = ArmoredPlayers(
        m=4,
        rounds_unknown=0,
        counts=[ArmoredCount(armored=1, n=3), ArmoredCount(armored=5, n=1)],
    )
    assert [c.armed for c in armed.counts] == [0, 5]
    assert [c.armored for c in armored.counts] == [1, 5]


def test_armed_players_keep_unknown_apart_from_zero() -> None:
    """An unreadable inventory is not zero armed."""
    armed = ArmedPlayers(
        m=2, rounds_unknown=3, counts=[ArmedCount(armed=0, n=2)]
    )
    assert armed.m == 2
    assert armed.rounds_unknown == 3


# --- The kind of sample point ---------------------------------------------------


def test_a_position_refuses_the_same_area_twice() -> None:
    """Six distributions out of eight had the duplicate guard.

    A step higher than :class:`AreaDistribution`'s own guard: there the same
    **player count** twice on one area is forbidden, here the same **area**
    twice on one sample point. Without this the row would set
    ``Middle 3 (2/2 kierroksesta), Middle 1 (2/2 kierroksesta)`` -- one
    observation as two, and their sum exceeds the sample.
    """
    with pytest.raises(ValidationError, match="same area twice"):
        Position(
            sample_kind="time",
            seconds=15.0,
            m=2,
            rounds_missing=0,
            areas=[
                AreaDistribution(
                    area="Middle",
                    m=2,
                    players_dist=[PlayersCount(players=3, n=2)],
                ),
                AreaDistribution(
                    area="Middle",
                    m=2,
                    players_dist=[PlayersCount(players=1, n=2)],
                ),
            ],
        )


def test_a_position_still_accepts_two_different_areas() -> None:
    """The guard's other direction: two areas from one moment is normal."""
    entry = Position(
        sample_kind="time",
        seconds=15.0,
        m=2,
        rounds_missing=0,
        areas=[
            AreaDistribution(
                area="Middle", m=2, players_dist=[PlayersCount(players=3, n=2)]
            ),
            AreaDistribution(
                area="Ramp", m=2, players_dist=[PlayersCount(players=1, n=2)]
            ),
        ],
    )
    assert [a.area for a in entry.areas] == ["Middle", "Ramp"]


def test_time_sample_must_carry_its_nominal_second() -> None:
    with pytest.raises(ValidationError, match="A time sample point"):
        Position(
            sample_kind="time", seconds=None, m=0, rounds_missing=0, areas=[]
        )


def test_first_contact_sample_has_no_nominal_second() -> None:
    """The moment of first contact differs every round; the median gives it."""
    with pytest.raises(ValidationError, match="no nominal second"):
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
    with pytest.raises(AggregateError, match="although there are"):
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
    with pytest.raises(AggregateError, match="throws but"):
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
    """A grenade with no area does not drop out: a smoke goes where nobody is."""
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
    """Without a source an estimate would look like an observation."""
    with pytest.raises(ValidationError, match="contradict each other"):
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
    """A source with no area would claim a derivation for a nonexistent area."""
    with pytest.raises(ValidationError, match="contradict each other"):
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
    with pytest.raises(AggregateError, match="The first-contact area"):
        FirstContactArea(area="Banana", n=3, m=2)


def _round_type_with_first_contact(areas: list[FirstContactArea]):
    """A round type whose only content is the first-contact presence list."""
    return RoundTypeReport(
        round_type="pistol",
        sample=sample(unknown=2),
        small_sample=True,
        positions=[],
        utility=[],
        utility_counts=[],
        players_armed=ArmedPlayers(
            m=2, rounds_unknown=0, counts=[ArmedCount(armed=0, n=2)]
        ),
        players_armored=ArmoredPlayers(
            m=2, rounds_unknown=0, counts=[ArmoredCount(armored=0, n=2)]
        ),
        first_contact=areas,
        deaths=DeathReport(m=0, rounds_missing=2),
    )


def test_first_contact_refuses_the_same_area_twice() -> None:
    """The last distribution without a duplicate guard.

    ``Σ n = m`` **does not hold** in the first-contact presence list and is
    not meant to: the same round produces an observation for every area in
    which the team had a player. So a duplicate does not show up in the sum
    the way it does in the other distributions -- it would just be two rows
    about the same area with different figures:
    ``Middle (2/2 kierroksesta), Middle (1/2 kierroksesta)``.
    """
    with pytest.raises(ValidationError, match="same area twice"):
        _round_type_with_first_contact(
            [
                FirstContactArea(area="Middle", n=2, m=2),
                FirstContactArea(area="Middle", n=1, m=2),
            ]
        )


def test_first_contact_still_accepts_two_different_areas() -> None:
    """The guard's other direction: two areas from one moment is normal."""
    entry = _round_type_with_first_contact(
        [
            FirstContactArea(area="Middle", n=2, m=2),
            FirstContactArea(area="Ramp", n=1, m=2),
        ]
    )
    assert [a.area for a in entry.first_contact] == ["Middle", "Ramp"]


# --- The whole report -----------------------------------------------------------


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
        roster_sample=roster_sample(unknown=1),
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
    """The anomaly rules' coverage, for the fixture.

    The default is full coverage with no blind spots, because that is the
    state in which an empty anomaly section is a **measured negative** -- and
    the states that depart from it are built separately in the tests about
    them.
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
    """A report whose only map is ``de_ancient`` -- the anomalies' home.

    The map is in the structure because :class:`Report` enforces that an
    anomaly's map is in the report. Without the map every anomaly test would
    fail on that guard and not on what it measures.
    """
    return Report(
        generated_at=datetime(2026, 9, 2, 12, 0, tzinfo=UTC),
        team=team(),
        sample=sample(unknown=1),
        roster_sample=roster_sample(unknown=1),
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
    """``render`` reads exactly what ``aggregate`` wrote."""
    report = full_report()
    again = Report.model_validate_json(report.model_dump_json())
    assert again == report
    assert again.schema_version == REPORT_SCHEMA_VERSION


def test_report_rejects_an_unknown_field() -> None:
    """An unknown field is an error: a silent skip would take a figure with it.

    The name changed in Story 2.5: ``anomalies`` is now a field of the model,
    so it is no longer unknown. The guard needs a name the model does **not**
    have -- otherwise it would stop measuring anything the moment the next
    figure is added.
    """
    data = full_report().model_dump(mode="json")
    data["no-such-field"] = []
    with pytest.raises(ValidationError):
        Report.model_validate(data)


def test_report_is_frozen() -> None:
    """A contract is not a workspace: a figure is not touched up as it is read."""
    report = full_report()
    with pytest.raises(ValidationError):
        report.unclassified_rounds = 0


def test_the_three_a_two_b_row_is_readable_without_further_arithmetic() -> None:
    """The target analysis's line *"3A and 2B"* with its sample, from the model."""
    report = full_report()
    areas = report.maps[0].sides[0].round_types[0].positions[0].areas
    found = {a.area: (a.players_dist[0].players, a.players_dist[0].n, a.m) for a in areas}
    assert found["BombsiteA"] == (3, 1, 1)
    assert found["BombsiteB"] == (2, 1, 1)


# --- Sums between the levels -----------------------------------------------------


def side_with(rounds: int) -> SideReport:
    """A side with one round type and the given number of rounds."""
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
    """A round lost between the levels shows in no leaf at all."""
    with pytest.raises(AggregateError, match="at level side"):
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
    with pytest.raises(AggregateError, match="at level map"):
        MapReport(
            map_name="de_nuke",
            map_name_source="map_demo_id",
            map_demo_ids=["Nuke_vs_a"],
            sample=sample(unknown=9),
            sides=[side_with(3)],
        )


def test_a_map_must_list_exactly_its_own_demos() -> None:
    with pytest.raises(AggregateError, match="but lists"):
        MapReport(
            map_name="de_nuke",
            map_name_source="map_demo_id",
            map_demo_ids=["Nuke_vs_a", "Nuke_vs_b"],
            sample=sample(unknown=3),
            sides=[side_with(3)],
        )


def test_a_round_moving_between_buckets_is_caught() -> None:
    """The total can match even if a round changed bucket."""
    with pytest.raises(AggregateError, match="in bucket"):
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
    with pytest.raises(AggregateError, match="at level report"):
        Report(
            generated_at=datetime(2026, 8, 30, tzinfo=UTC),
            team=team(),
            sample=sample(unknown=7),
            roster_sample=roster_sample(unknown=7),
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
    """A round with no type does not fit the structure, nor the sample."""
    report = Report(
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
        team=team(),
        sample=sample(unknown=3),
        roster_sample=roster_sample(unknown=3),
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
    """``Σ n = m``. Without this the figure means nothing."""
    with pytest.raises(AggregateError, match="in the first-death areas"):
        DeathReport(
            m=3,
            rounds_missing=0,
            first_death_areas=[FirstDeathArea(area="Cave", n=2, m=3)],
        )


def test_every_first_death_area_shares_the_same_sample() -> None:
    """Two denominators in one distribution are not comparable."""
    with pytest.raises(AggregateError, match="claims a sample of"):
        DeathReport(
            m=2,
            rounds_missing=0,
            first_death_areas=[
                FirstDeathArea(area="Cave", n=1, m=2),
                FirstDeathArea(area="Long", n=1, m=1),
            ],
        )


def test_the_same_first_death_area_may_not_appear_twice() -> None:
    with pytest.raises(ValueError, match="same area twice"):
        DeathReport(
            m=2,
            rounds_missing=0,
            first_death_areas=[
                FirstDeathArea(area="Cave", n=1, m=2),
                FirstDeathArea(area="Cave", n=1, m=2),
            ],
        )


def test_a_first_death_area_cannot_exceed_the_rounds() -> None:
    with pytest.raises(AggregateError, match="The first-death area"):
        FirstDeathArea(area="Cave", n=3, m=2)


def test_the_kill_distribution_must_sum_to_the_kills() -> None:
    """The kill distribution's denominator is **kills**, and the sum must match."""
    with pytest.raises(AggregateError, match="in the kill areas"):
        DeathReport(
            m=0,
            rounds_missing=0,
            kills_total=5,
            kills=[KillArea(area="Middle", n=4, m=5)],
        )


def test_every_kill_area_shares_the_same_denominator() -> None:
    with pytest.raises(AggregateError, match="The kill area"):
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
    with pytest.raises(ValueError, match="same area twice"):
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
    with pytest.raises(AggregateError, match="The kill area"):
        KillArea(area="Middle", n=3, m=2)


def test_a_median_without_a_single_death_is_refused() -> None:
    """A median without observations would be a figure out of nothing."""
    with pytest.raises(AggregateError, match="median of the first death"):
        DeathReport(m=0, rounds_missing=4, first_death_seconds_median=24.0)


def test_the_kill_sample_may_exceed_the_round_count() -> None:
    """There are usually more kills than rounds -- that is **not** an error.

    This is the other side of the preceding guards: if ``Σ n = m`` were tied
    to the rounds, real material would fail on every round type.
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
    """A round type on which the team neither died nor killed is valid."""
    entry = DeathReport(m=0, rounds_missing=3)
    assert entry.first_death_areas == []
    assert entry.kills == []
    assert entry.first_death_seconds_median is None


def test_the_round_type_report_requires_its_death_block() -> None:
    """No default: an empty default would look like a round type with no deaths.

    That difference is exactly the reason for raising the schema version -- an
    old ``report.json`` must not validate quietly against this model.
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
    """No default for the armour distribution: empty would look kevlar-less.

    The same grounds as with the deaths, and the same consequence: an old
    ``report.json`` must not validate quietly against this model, so the
    schema version rises.
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
    """``Σ n = m`` in the armour distribution too -- or the sample would lie."""
    with pytest.raises(AggregateError, match="armoured players' distribution"):
        ArmoredPlayers(
            m=3, rounds_unknown=0, counts=[ArmoredCount(armored=5, n=2)]
        )


def test_the_two_player_distributions_use_different_field_names() -> None:
    """``armed`` and ``armored`` are different fields, in the JSON too.

    ``report.json`` is read by hand, and the same field name in two
    distributions with nearly the same name is exactly the confusion this
    story fixes.
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
    """An old report does not validate = the version rises.

    The condition is not "was a new field added" but "does the old file
    validate". Story 2.9 brought no field at all: it removed the value
    ``snapped`` from the ``AreaSource`` enumeration, and that is enough -- an
    old ``report.json`` would otherwise fall over on a pydantic error instead
    of ``render`` saying that aggregation has to be run again.

    Story 2.11 is the same pattern the other way round: ``map_name_source``
    gained the value ``demo_header``, so a **new** file is not valid for the
    old model -- and an old file's maps are grouped by a different rule than
    this version's, because the name is now read from the demo header.

    Story 2.5 widens the condition with a third case: ``anomalies`` is an
    empty list by default, so an old file **would validate** -- and that is
    exactly why the version rises. An empty anomaly section is an observation
    in this model ("no anomalies"), so a report rendered from an old file
    would claim as a measured result that the rules did not exist.

    Story 2.14 hits both branches at once: ``Anomaly.rule`` gained the value
    ``stack``, so a **new** file is not valid for the old model -- and an old
    file would be valid, but its coverage would name stack as not implemented
    and stay silent about how many rounds it can hit.
    """
    assert REPORT_SCHEMA_VERSION == "9.0.0"


def test_the_map_name_source_covers_all_three_sources() -> None:
    """There are three sources, and each is a valid value (Story 2.11).

    The order of precedence is ``demo_header`` -> ``map_demo_id`` ->
    ``unknown``, and both of the old values keep working: the archive holds
    demos whose header has no map in it.
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
        _map("not-a-source")


def _round_type_with(entry: DeathReport, rounds: int) -> RoundTypeReport:
    """A round type with the given death block and sample."""
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
    """The deaths' rounds are exactly the round type's rounds.

    ``Σ n = m`` holds even when ``m`` has been computed from the **wrong set
    of rounds**: the distribution would be internally consistent and quietly
    wrong. Without this cross-check a wrong ``round_keys`` would produce a
    report every leaf of which looks right.
    """
    covers_three = DeathReport(
        m=1,
        rounds_missing=2,
        first_death_seconds_median=20.0,
        first_death_areas=[FirstDeathArea(area="Cave", n=1, m=1)],
    )
    with pytest.raises(AggregateError, match="cover 3 rounds"):
        _round_type_with(covers_three, rounds=4)


def test_a_death_block_that_covers_the_sample_is_accepted() -> None:
    """The guard's other branch: a correctly computed block passes.

    Without this the preceding test would prove only that something raises an
    exception.
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
    """A difference either way is the same fault, and both must be stopped."""
    with pytest.raises(AggregateError, match="cover 5 rounds"):
        _round_type_with(DeathReport(m=0, rounds_missing=5), rounds=4)


# --- Anomalies (Story 2.5) ------------------------------------------------------


def _point(seconds: float = 30.0, players: int = 2, alive=None) -> AnomalyPoint:
    """One sample point with its observation."""
    return AnomalyPoint(sample_t_s=seconds, players=players, alive=alive)


def _round(**overrides) -> AnomalyRound:
    """A round row whose defaults are the calibration's Ancient round 18.

    ``seconds`` and ``players`` are the helper's own shortcuts for the
    one-sample-point case; a test with several points gives ``points`` itself.
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
    """An anomaly whose defaults are the calibration's Ancient round 18."""
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
    """A crunch row: the source areas are required."""
    values: dict[str, object] = {
        "rule": "crunch",
        "round_types": ["eco"],
        "rounds": [_round(sources=["Ramp", "Squeaky"])],
    }
    values.update(overrides)
    return _anomaly(**values)


def test_an_anomaly_carries_everything_the_report_line_needs() -> None:
    """What, where, when, how often -- and on what grounds."""
    anomaly = _anomaly()
    assert anomaly.area == "TSideLower"
    assert anomaly.rounds[0].seconds == [30.0]
    assert anomaly.rounds[0].round_no == 18
    assert anomaly.orientation[0].t_share == 0.88
    assert (anomaly.n, anomaly.m) == (1, 3)
    assert anomaly.rounds[0].sources == []
    assert anomaly.small_sample is False


def test_an_anomaly_cannot_appear_in_more_rounds_than_exist() -> None:
    with pytest.raises(AggregateError, match="appears on 4 rounds"):
        _anomaly(
            n=4,
            m=3,
            rounds=[_round(round_no=n) for n in (1, 2, 3, 4)],
        )


def test_the_sample_must_be_the_length_of_the_round_list() -> None:
    """The figure and its evidence cannot be of different sizes."""
    with pytest.raises(AggregateError, match="number of round rows is 1"):
        _anomaly(n=2, m=3)


def test_the_same_round_cannot_appear_twice() -> None:
    with pytest.raises(ValidationError, match="same round twice"):
        _anomaly(n=2, m=3, rounds=[_round(), _round()])


def test_a_round_needs_at_least_one_sample_point() -> None:
    """Without a sample point an observation has no "when"."""
    with pytest.raises(ValidationError):
        _round(seconds=[])


def test_a_repeated_sample_point_is_refused() -> None:
    with pytest.raises(ValidationError, match="sample points repeat"):
        _round(seconds=[30.0, 30.0])


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_an_impossible_sample_point_is_refused(value: float) -> None:
    """NaN would pass every comparison and reach the report as 'nan'."""
    with pytest.raises(ValidationError):
        _round(seconds=[value])


def test_unsorted_sample_points_are_refused() -> None:
    """The row is read in time order, so the order is part of the contract."""
    with pytest.raises(ValidationError, match="ascending order"):
        _round(seconds=[30.0, 15.0])


def test_a_repeated_source_area_is_refused() -> None:
    with pytest.raises(ValidationError, match="source areas repeat"):
        _round(sources=["Ramp", "Ramp"])


def test_a_nameless_source_area_is_refused() -> None:
    """A nameless area is not a direction."""
    with pytest.raises(ValidationError, match="nameless area"):
        _round(sources=["Ramp", "  "])


def test_more_sources_than_players_is_refused() -> None:
    """Every direction needs a player of its own -- an impossible observation."""
    with pytest.raises(ValidationError, match="Every direction needs"):
        _round(players=2, sources=["A", "B", "C"])


def test_two_sample_points_keep_their_own_player_counts() -> None:
    """**The maximum must not come back.**

    A round with five players at 15 s and one at 30 s is two observations and
    not one. The structure used to carry one maximum and a list of sample
    points, and the report's row set the maximum against both -- measured,
    this very round is MatureMayhem Inferno round 2.
    """
    entry = _round(points=[_point(15.0, 5), _point(30.0, 1)])
    assert [(p.sample_t_s, p.players) for p in entry.points] == [
        (15.0, 5),
        (30.0, 1),
    ]
    assert entry.players_max == 5
    assert entry.seconds == [15.0, 30.0]


def test_a_point_cannot_have_more_players_than_are_alive() -> None:
    """Those in the group are a subset of those alive."""
    with pytest.raises(ValidationError, match="subset of those alive"):
        _point(15.0, players=5, alive=4)


def test_the_alive_count_is_on_every_point_or_none() -> None:
    """Half a row would set two different units on the same row."""
    with pytest.raises(ValidationError, match="on all of them or on none"):
        AnomalyRound(
            map_demo_id="demo",
            round_no=4,
            round_type="eco",
            points=[_point(15.0, 4, 5), _point(30.0, 4)],
        )


def _stack(**overrides) -> Anomaly:
    """A stack row: site group and alive required, orientation forbidden."""
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
    """Three fields that separate stack from the two other rules."""
    stack = _stack()
    assert (stack.site, stack.area) == ("B", "BombsiteB")
    assert stack.rounds[0].points[0].alive == 5
    assert stack.orientation == []


def test_a_stack_whose_site_and_area_disagree_is_refused() -> None:
    """The field is in the structure only so the reader need not infer it."""
    with pytest.raises(ValidationError, match="disagree: the group's own area"):
        _stack(site="A")


def test_a_stack_without_a_site_group_is_refused() -> None:
    """The group is the row's anchor, and it cannot be left unnamed."""
    with pytest.raises(ValidationError, match="the allowed ones are"):
        _stack(site=None)


def test_a_stack_without_the_alive_count_is_refused() -> None:
    """Four out of five and four out of four are a different observation."""
    with pytest.raises(ValidationError, match="how many players were alive"):
        _stack(rounds=[_round(round_no=13, seconds=[15.0], players=4)])


def test_a_stack_that_carries_orientation_is_refused() -> None:
    """The rule does not read the T share, so carrying it would be invented."""
    with pytest.raises(ValidationError, match="carries the area's orientation"):
        _stack(
            orientation=[
                AreaOrientation(
                    map_demo_id="demo", t_share=0.88, observations=24
                )
            ]
        )


def test_an_orientation_rule_cannot_carry_a_site_group() -> None:
    """Only stack reads site groups."""
    with pytest.raises(ValidationError, match="only stack reads"):
        _anomaly(site="A")


def test_an_orientation_rule_cannot_carry_the_alive_count() -> None:
    """Neither orientation rule counts the living."""
    with pytest.raises(ValidationError, match="the number of the living"):
        _anomaly(rounds=[_round(alive=5)])


def test_a_crunch_without_source_areas_is_refused() -> None:
    """Crunch is by definition arrival from several directions."""
    with pytest.raises(ValidationError, match="rounds without source areas"):
        _anomaly(rule="crunch")


def test_an_advance_with_source_areas_is_refused() -> None:
    """The advance rule does not count directions, so it cannot carry them."""
    with pytest.raises(ValidationError, match="carries source areas"):
        _anomaly(rounds=[_round(sources=["Ramp", "Squeaky"])])


def test_an_anomaly_without_an_area_is_refused() -> None:
    """An area without a name cannot be T territory."""
    with pytest.raises(ValidationError):
        _anomaly(area="")


def test_an_anomaly_needs_an_orientation() -> None:
    """An anomaly always rests on the area's measured T share."""
    with pytest.raises(ValidationError):
        _anomaly(orientation=[])


def test_the_same_demo_cannot_give_an_area_two_shares() -> None:
    with pytest.raises(ValidationError, match="same demo twice"):
        _anomaly(
            orientation=[
                AreaOrientation(map_demo_id="demo", t_share=0.88, observations=24),
                AreaOrientation(map_demo_id="demo", t_share=0.84, observations=37),
            ]
        )


def test_the_orientation_must_cover_exactly_the_observed_demos() -> None:
    """The evidence must cover no more and no less than the observations."""
    with pytest.raises(ValidationError, match="covers the demos"):
        _anomaly(
            orientation=[
                AreaOrientation(map_demo_id="toinen", t_share=0.88, observations=24)
            ]
        )


def test_two_demos_may_give_an_area_two_shares() -> None:
    """The guard's other branch: different demos are different observations."""
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
    """A summary cannot name a type that no round is."""
    with pytest.raises(ValidationError, match="round_types of anomaly"):
        _crunch(round_types=["eco", "full"])


def test_the_round_types_cannot_leave_out_a_type_that_is_there() -> None:
    """The guard's other direction."""
    with pytest.raises(ValidationError, match="round_types of anomaly"):
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
    """Spec change 2: crunch is not keyed by round type."""
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
    """Advance is grouped by round type, so there is one type."""
    with pytest.raises(ValidationError, match="round types"):
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
    """The summary cannot disagree with the rows it was assembled from."""
    with pytest.raises(ValidationError, match="is 5, but the largest"):
        _anomaly(players_max=5)


@pytest.mark.parametrize("share", [-0.01, 1.01])
def test_an_impossible_t_share_is_refused(share: float) -> None:
    with pytest.raises(ValidationError):
        AreaOrientation(map_demo_id="demo", t_share=share, observations=24)


def test_an_orientation_without_observations_is_refused() -> None:
    """Zero observations is not an area but the absence of one."""
    with pytest.raises(ValidationError):
        AreaOrientation(map_demo_id="demo", t_share=0.88, observations=0)


def test_an_unknown_rule_name_is_refused() -> None:
    """The list of rules is contract, not a free string.

    The name was ``stack`` up to Story 2.14. After that it is valid, and the
    test passed for the wrong reason -- the error came from stack's own guards
    and not from the check on the rule name. The name therefore has to be one
    ``ANOMALY_RULES`` does not know and is not going to know.
    """
    with pytest.raises(ValidationError):
        _anomaly(rule="rotate")


def test_an_unknown_map_name_source_is_refused() -> None:
    """The list of sources is shared between two nodes."""
    with pytest.raises(ValidationError):
        _anomaly(map_name_source="not-a-source")


def test_the_map_name_sources_are_the_same_list_for_both_nodes() -> None:
    """Written out twice, a new source would pass in one and fail in the other."""
    assert set(get_args(MapNameSource)) == set(MAP_NAME_SOURCES)


# --- Coverage (AnomalyScan) -----------------------------------------------------


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
    """Three figures, and not one of them may be missing.

    A missing key would be read as zero by a pydantic default, that is, a
    blind spot would be read as a measured negative -- exactly what this node
    exists to prevent. ``stack_rounds`` was defaulted for a moment, and this
    test is what keeps it required.
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
    """Stack examines CT rounds, so it cannot see more than there are."""
    with pytest.raises(AggregateError, match="larger than the"):
        _scan(crunch_rounds=9, stack_rounds=10)


def test_a_gap_in_the_stack_coverage_must_have_a_named_cause() -> None:
    """The difference comes only from a silenced demo, so it has to be named.

    An unnamed difference would read as a measured negative: the reader would
    see a smaller denominator and nothing would say what fell out of it.
    """
    with pytest.raises(AggregateError, match="recorded as having no site groups"):
        _scan(crunch_rounds=9, stack_rounds=4)
    # The same difference with a named cause is accepted.
    assert (
        _scan(
            crunch_rounds=9,
            stack_rounds=4,
            demos_without_site_groups=["Nuke_vs_a"],
        ).stack_rounds
        == 4
    )


def test_the_scan_refuses_the_same_silenced_demo_twice() -> None:
    """The same demo twice would promise two blind spots out of one."""
    with pytest.raises(ValidationError, match="list of those without site groups"):
        _scan(demos_without_site_groups=["Nuke_vs_a", "Nuke_vs_a"])


def test_the_coverage_numbers_must_be_nested() -> None:
    """CT saving rounds ⊆ CT rounds ⊆ all rounds.

    The wrong order would mean that the coverage promises a rule more rounds
    than the rule can examine.
    """
    with pytest.raises(AggregateError, match="are not nested"):
        _scan(rounds_scanned=4, crunch_rounds=5, advance_rounds=1)
    with pytest.raises(AggregateError, match="are not nested"):
        _scan(rounds_scanned=9, crunch_rounds=4, advance_rounds=5)


def test_the_scan_refuses_an_unknown_rule() -> None:
    """A name ``ANOMALY_RULES`` does not know is not a valid rule to have run.

    Before Story 2.14 the name was ``stack``, which is now implemented. That
    is exactly why the test must not name a rule that **can** one day exist:
    the invalid value here is precisely one that is not in the list and is not
    going to be.
    """
    with pytest.raises(ValidationError):
        _scan(rules=["rotate"])


def test_the_scan_refuses_the_same_rule_twice() -> None:
    with pytest.raises(ValidationError, match="The same rule twice"):
        _scan(rules=["crunch", "crunch"])


def test_the_scan_refuses_the_same_blind_demo_twice() -> None:
    with pytest.raises(
        ValidationError, match="list of those without an orientation"
    ):
        _scan(demos_without_orientation=["a", "a"])


def test_the_scan_needs_at_least_one_rule() -> None:
    """Zero rules is not coverage but the absence of a run."""
    with pytest.raises(ValidationError):
        _scan(rules=[])


# --- Anomalies at the report's level --------------------------------------------


def test_an_anomaly_cannot_name_a_map_that_is_not_in_the_report() -> None:
    """The reader would look for a map section that was never written."""
    with pytest.raises(AggregateError, match="names a map that is not in the report"):
        _report_with_anomalies([_anomaly(map_name="de_train")])


def test_two_anomalies_cannot_share_the_grouping_key() -> None:
    """The same observation twice with different samples is what grouping stops."""
    with pytest.raises(AggregateError, match="same anomaly is in the section twice"):
        _report_with_anomalies([_anomaly(), _anomaly(m=4)])


def test_the_same_area_on_two_rules_is_not_a_duplicate() -> None:
    """The rule is part of the key: advance and crunch are different rows."""
    report = _report_with_anomalies([_anomaly(), _crunch()])
    assert len(report.anomalies) == 2


def test_the_same_area_on_two_round_types_is_not_a_duplicate() -> None:
    """The round type is part of advance's key."""
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
    """``model_validate`` is a stale file's route, not the constructor.

    Testing the validator only through the constructor would leave open
    exactly the route by which a broken ``report.json`` reaches ``render``.
    """
    report = _report_with_anomalies([_anomaly()])
    data = report.model_dump(mode="json")
    data["anomalies"][0]["n"] = 9
    with pytest.raises(AggregateError, match="number of round rows is 1"):
        Report.model_validate(data)


def test_a_report_without_the_scan_is_refused() -> None:
    """Coverage is required: an empty section must differ from an unrun one."""
    report = _report_with_anomalies([])
    data = report.model_dump(mode="json")
    data.pop("anomaly_scan")
    with pytest.raises(ValidationError):
        Report.model_validate(data)
