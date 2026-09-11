"""``domain.sampling`` -- sampling and first contact without demos.

The module is pure, so the whole I/O matrix is one function call away here: a
normal round, a short round, a very short round, first contact, utility only,
friendly fire, a round with no anchor. Not one of these tests reads a demo,
and that is why they are run in the ``-m "not demo"`` run too.

The tick rate is 64 everywhere, the same as in both test demos, so that the
seconds and the ticks are readable: 6 s = 384 ticks.
"""

from __future__ import annotations

import pytest

from conftest import (
    JUST_OVER_SEPARATION_SITE_CLOUD,
    JUST_UNDER_SEPARATION_SITE_CLOUD,
    OVERLAPPING_SITE_CLOUD,
    SITE_CLOUD,
)
from pappascout.constants import (
    CRUNCH,
    CT_ADVANCE,
    SAMPLE_KINDS,
    SAVING_ROUND_TYPES,
    STACK,
)
from pappascout.domain.models import ThresholdSettings
from pappascout.domain.sampling import (
    FIRST_CONTACT_SAMPLE,
    TIME_SAMPLE,
    AnomalyHit,
    AreaObservations,
    AreaPresence,
    CloudCell,
    DamageEvent,
    RoundBounds,
    crunch_hits,
    ct_advance_hits,
    first_contact_tick,
    normalize_weapon,
    sample_ticks,
    seconds_since_freeze_end,
    site_groups,
    stack_hits,
    t_side_shares,
)

RATE = 64.0
FREEZE = 10_000

#: The same list as in ``settings.toml``'s ``[parse]`` section.
UTILITY = (
    "hegrenade",
    "flashbang",
    "smokegrenade",
    "decoy",
    "molotov",
    "incgrenade",
    "inferno",
)

#: The default sample points, ``[parse].snapshot_seconds``.
SECONDS = [6.0, 15.0, 30.0, 45.0]


def bounds(duration_s: float, *, round_raw: int = 1) -> RoundBounds:
    """A round that is settled ``duration_s`` seconds after the anchor."""
    return RoundBounds(
        round_raw=round_raw,
        freeze_end_tick=FREEZE,
        end_tick=FREEZE + round(duration_s * RATE),
    )


def hurt(
    t_s: float,
    *,
    attacker: str = "t1",
    victim: str = "ct1",
    weapon: str = "ak47",
    attacker_side: str = "T",
    victim_side: str = "CT",
) -> DamageEvent:
    """A damage event in seconds from the anchor."""
    return DamageEvent(
        tick=FREEZE + round(t_s * RATE),
        attacker_id=attacker,
        victim_id=victim,
        weapon=weapon,
        attacker_side=attacker_side,
        victim_side=victim_side,
    )


def test_sample_kinds_are_exactly_the_schema_enum() -> None:
    """The sample kind is one value in the code, in Parquet and in the report.

    If the module's constants and ``SAMPLE_KINDS`` diverged, the table would
    be refused only at the writing stage -- or worse, a new kind would be left
    without an enum.
    """
    assert set(SAMPLE_KINDS) == {TIME_SAMPLE, FIRST_CONTACT_SAMPLE}


# --- Time points ---------------------------------------------------------------


def test_a_long_round_gets_every_configured_sample_point() -> None:
    """I/O matrix: a round lasting over 45 s gets all four time points."""
    points = sample_ticks([bounds(90)], RATE, SECONDS)
    assert [p.sample_t_s for p in points] == SECONDS
    assert {p.sample_kind for p in points} == {TIME_SAMPLE}
    assert [p.tick for p in points] == [
        FREEZE + 6 * 64,
        FREEZE + 15 * 64,
        FREEZE + 30 * 64,
        FREEZE + 45 * 64,
    ]


def test_seconds_become_ticks_from_the_freeze_end_anchor() -> None:
    point = sample_ticks([bounds(90)], RATE, [15.0])[0]
    assert point.tick == FREEZE + 15 * 64
    assert point.t_s == pytest.approx(15.0)
    assert point.round_raw == 1


def test_a_short_round_loses_the_points_it_never_reached() -> None:
    """I/O matrix: a round settled in 28 seconds gets only 6 and 15."""
    points = sample_ticks([bounds(28)], RATE, SECONDS)
    assert [p.sample_t_s for p in points] == [6.0, 15.0]


def test_a_very_short_round_gets_no_time_samples_at_all() -> None:
    """I/O matrix: a round settled in 5 seconds produces no time points.

    Zero points is the right answer and not an error -- inventing a point
    would be a claim about the players' positions at a moment that was never
    played.
    """
    assert sample_ticks([bounds(5)], RATE, SECONDS) == []


def test_a_point_exactly_at_the_end_tick_is_kept() -> None:
    """The moment a round is settled is not yet "after it"."""
    points = sample_ticks([bounds(30)], RATE, [30.0])
    assert len(points) == 1
    assert points[0].tick == bounds(30).end_tick


def test_a_point_one_tick_past_the_end_is_dropped() -> None:
    round_bounds = RoundBounds(
        round_raw=1, freeze_end_tick=FREEZE, end_tick=FREEZE + 30 * 64 - 1
    )
    assert sample_ticks([round_bounds], RATE, [30.0]) == []


def test_t_s_is_computed_from_the_chosen_tick_not_the_nominal_second() -> None:
    """The rounding shows in ``t_s``; the nominal time stays in ``sample_t_s``.

    Without that difference the table would claim a moment that was not read.
    """
    point = sample_ticks([bounds(90)], 63.7, [6.0])[0]
    expected_tick = FREEZE + round(6.0 * 63.7)  # 382
    assert point.tick == expected_tick
    assert point.sample_t_s == 6.0
    assert point.t_s == pytest.approx((expected_tick - FREEZE) / 63.7)
    assert point.t_s != 6.0


def test_a_round_without_a_freeze_anchor_is_not_sampled() -> None:
    """I/O matrix: ``status = "no_freeze_end"`` -> no tick rows.

    Without an anchor ``t_s`` is undefined, and the round cannot be compared
    against the others.
    """
    round_bounds = RoundBounds(round_raw=3, freeze_end_tick=None, end_tick=FREEZE + 10_000)
    assert sample_ticks([round_bounds], RATE, SECONDS) == []


def test_a_round_that_never_ended_is_not_sampled() -> None:
    """Without an end tick, points cannot be bounded by the round."""
    round_bounds = RoundBounds(round_raw=3, freeze_end_tick=FREEZE, end_tick=None)
    assert sample_ticks([round_bounds], RATE, SECONDS) == []


def test_a_round_whose_bounds_are_reversed_is_not_sampled() -> None:
    round_bounds = RoundBounds(round_raw=3, freeze_end_tick=FREEZE, end_tick=FREEZE - 100)
    assert sample_ticks([round_bounds], RATE, SECONDS) == []


def test_points_are_ordered_by_round_and_time_whatever_the_input_order() -> None:
    points = sample_ticks(
        [bounds(90, round_raw=2), bounds(90, round_raw=1)], RATE, [30.0, 6.0]
    )
    assert [(p.round_raw, p.sample_t_s) for p in points] == [
        (2, 6.0),
        (2, 30.0),
        (1, 6.0),
        (1, 30.0),
    ]


def test_a_duplicated_sample_second_produces_one_point() -> None:
    """A typo in the settings file must not double the sample."""
    points = sample_ticks([bounds(90)], RATE, [6.0, 6.0, 15.0])
    assert [p.sample_t_s for p in points] == [6.0, 15.0]


def test_zero_seconds_is_the_anchor_itself() -> None:
    point = sample_ticks([bounds(90)], RATE, [0.0])[0]
    assert point.tick == FREEZE
    assert point.t_s == 0.0


@pytest.mark.parametrize("rate", [0.0, -64.0])
def test_an_impossible_tick_rate_is_refused(rate: float) -> None:
    with pytest.raises(ValueError, match="[Tt]ick rate"):
        sample_ticks([bounds(90)], rate, SECONDS)


def test_a_negative_sample_second_is_refused() -> None:
    """A negative point would point into freezetime, where nobody has moved."""
    with pytest.raises(ValueError, match="is negative"):
        sample_ticks([bounds(90)], RATE, [-1.0, 6.0])


def test_seconds_since_freeze_end_matches_the_rounds_table_formula() -> None:
    assert seconds_since_freeze_end(FREEZE + 128, FREEZE, 64.0) == 2.0
    assert seconds_since_freeze_end(FREEZE, FREEZE, 64.0) == 0.0


# --- First contact -------------------------------------------------------------


def test_the_first_cross_side_hit_is_the_contact() -> None:
    """I/O matrix: the first cross-side hit with a non-utility weapon."""
    events = [hurt(30.0), hurt(12.0), hurt(20.0)]
    tick = first_contact_tick(events, bounds(90), exclude_weapons=UTILITY)
    assert tick == FREEZE + round(12.0 * RATE)


def test_a_round_without_any_damage_has_no_contact() -> None:
    """I/O matrix: time ran out, nobody fired."""
    assert first_contact_tick([], bounds(115), exclude_weapons=UTILITY) is None


def test_utility_only_damage_is_not_a_contact() -> None:
    """I/O matrix: the only damage is from a molotov -> no first contact.

    The round is still included, it is just left without ``first_contact``
    rows.
    """
    events = [hurt(20.0, weapon="molotov"), hurt(25.0, weapon="inferno")]
    assert first_contact_tick(events, bounds(90), exclude_weapons=UTILITY) is None


def test_utility_damage_does_not_hide_a_later_real_contact() -> None:
    events = [hurt(10.0, weapon="hegrenade"), hurt(22.0, weapon="awp")]
    tick = first_contact_tick(events, bounds(90), exclude_weapons=UTILITY)
    assert tick == FREEZE + round(22.0 * RATE)


def test_friendly_fire_is_not_a_contact() -> None:
    """I/O matrix: the attacker is on the same side."""
    same_side = hurt(10.0, attacker="t1", victim="t2", victim_side="T")
    opponent = hurt(40.0)
    tick = first_contact_tick([same_side, opponent], bounds(90), exclude_weapons=UTILITY)
    assert tick == FREEZE + round(40.0 * RATE)


def test_self_damage_is_not_a_contact() -> None:
    """I/O matrix: attacker = victim."""
    self_damage = hurt(10.0, attacker="t1", victim="t1", victim_side="T")
    assert first_contact_tick([self_damage], bounds(90), exclude_weapons=UTILITY) is None


def test_world_damage_without_an_attacker_is_not_a_contact() -> None:
    """Fall damage has no attacker, so it says nothing about an encounter."""
    fall_damage = DamageEvent(
        tick=FREEZE + 640,
        attacker_id=None,
        victim_id="ct1",
        weapon=None,
        attacker_side=None,
        victim_side="CT",
    )
    assert first_contact_tick([fall_damage], bounds(90), exclude_weapons=UTILITY) is None


def test_a_player_whose_side_is_unknown_is_not_a_contact() -> None:
    """Guessing the side would attribute the contact to the wrong team."""
    unknown_side = hurt(10.0, attacker_side=None)
    assert first_contact_tick([unknown_side], bounds(90), exclude_weapons=UTILITY) is None


def test_damage_outside_the_round_belongs_to_another_round() -> None:
    """A hit after the round belongs to the next round, not to this one."""
    round_bounds = bounds(30)
    after_end = hurt(45.0)
    before_start = DamageEvent(
        tick=FREEZE - 500,
        attacker_id="t1",
        victim_id="ct1",
        weapon="ak47",
        attacker_side="T",
        victim_side="CT",
    )
    assert (
        first_contact_tick([after_end, before_start], round_bounds, exclude_weapons=UTILITY) is None
    )


def test_death_is_the_fallback_when_no_hurt_event_qualifies() -> None:
    death = hurt(18.0)
    tick = first_contact_tick(
        [],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[death],
        fallback_death=True,
    )
    assert tick == FREEZE + round(18.0 * RATE)


def test_the_hurt_event_wins_over_the_death_fallback() -> None:
    """The fallback is used only if there is no primary -- not alongside."""
    tick = first_contact_tick(
        [hurt(30.0)],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[hurt(10.0)],
        fallback_death=True,
    )
    assert tick == FREEZE + round(30.0 * RATE)


def test_the_death_fallback_can_be_switched_off() -> None:
    tick = first_contact_tick(
        [],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[hurt(18.0)],
        fallback_death=False,
    )
    assert tick is None


def test_a_death_by_utility_is_not_a_contact_either() -> None:
    tick = first_contact_tick(
        [],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[hurt(18.0, weapon="inferno")],
    )
    assert tick is None


def test_a_round_without_an_anchor_has_no_contact() -> None:
    """Without an anchor the contact's moment could not be given as ``t_s``."""
    round_bounds = RoundBounds(round_raw=1, freeze_end_tick=None, end_tick=FREEZE + 5000)
    assert first_contact_tick([hurt(10.0)], round_bounds, exclude_weapons=UTILITY) is None


def test_an_empty_exclude_list_lets_utility_count() -> None:
    """The rule is a setting, not code: an empty list lets a molotov count."""
    tick = first_contact_tick([hurt(10.0, weapon="molotov")], bounds(90))
    assert tick == FREEZE + round(10.0 * RATE)


# --- Weapon name normalisation -------------------------------------------------


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("hegrenade", "hegrenade"),
        ("weapon_hegrenade", "hegrenade"),
        ("HEGrenade", "hegrenade"),
        ("  molotov  ", "molotov"),
        ("", None),
        (None, None),
    ],
)
def test_weapon_names_are_compared_normalised(raw_name, expected) -> None:
    assert normalize_weapon(raw_name) == expected


def test_the_exclude_list_is_normalised_too() -> None:
    """The settings file may say ``weapon_molotov`` -- that is utility too."""
    tick = first_contact_tick(
        [hurt(10.0, weapon="molotov")],
        bounds(90),
        exclude_weapons=("weapon_Molotov",),
    )
    assert tick is None


# --- The anomaly rules (Story 2.5) ----------------------------------------------
#
# Every row of spec-2-5's I/O matrix is here, and all the tables are built by
# hand: not one of these tests reads a demo. The numbers come from the
# calibration (``kalibrointi-ct-eteneminen.md``), so that a row says what it
# measures: 0.88 is Ancient's ``TSideLower``, 0.70 Nuke's outside.

#: The thresholds as in ``settings.toml``. The tests read them from here and
#: not from the settings file -- the rule is a function, and its parameters
#: are arguments.
T_SHARE = 0.80
MIN_OBSERVATIONS = 20
MAX_SAMPLE_S = 30.0

#: An area held by the T side in the demo: Ancient's B long, T share 0.88
#: (n 24).
T_AREA = "TSideLower"

#: An area that stays below the threshold: Nuke's outside, T share exactly
#: 0.70 (n 302).
SHARED_AREA = "Outside"


def observed(t_share: float, observations: int) -> AreaObservations:
    """An area's observations at the given T share."""
    return AreaObservations(t=round(t_share * observations), total=observations)


def orientation(
    **areas: AreaObservations,
) -> dict[str | None, AreaObservations]:
    """Area -> observations; the default map holds only the T side's area."""
    return dict(areas) or {T_AREA: observed(0.88, 24)}


def at(
    seconds: float,
    area: str | None,
    *players: str,
    side: str = "CT",
    kind: str = TIME_SAMPLE,
    alive: bool = True,
) -> list[AreaPresence]:
    """Sample point rows: the given players, area and moment."""
    return [
        AreaPresence(
            player_id=player,
            side=side,
            sample_kind=kind,
            sample_t_s=seconds,
            area=area,
            is_alive=alive,
        )
        for player in players
    ]


def advance(
    rows: list[AreaPresence],
    *,
    round_type: str | None = "eco",
    areas: dict[str | None, AreaObservations] | None = None,
    min_players: int = 1,
):
    return ct_advance_hits(
        rows,
        round_type=round_type,
        orientation=areas if areas is not None else orientation(),
        t_share_min=T_SHARE,
        area_min_observations=MIN_OBSERVATIONS,
        max_sample_s=MAX_SAMPLE_S,
        min_players=min_players,
    )


def crunch(
    rows: list[AreaPresence],
    *,
    areas: dict[str | None, AreaObservations] | None = None,
    min_players: int = 2,
    min_sources: int = 2,
):
    return crunch_hits(
        rows,
        orientation=areas if areas is not None else orientation(),
        t_share_min=T_SHARE,
        area_min_observations=MIN_OBSERVATIONS,
        max_sample_s=MAX_SAMPLE_S,
        min_players=min_players,
        min_sources=min_sources,
    )



def test_an_area_is_t_side_when_it_passes_both_thresholds() -> None:
    """The share AND the observation count -- neither is enough on its own."""
    areas = {
        T_AREA: observed(0.88, 24),
        SHARED_AREA: observed(0.70, 302),
        "Thin": observed(1.00, 8),
    }
    passed = t_side_shares(
        areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
    )
    assert set(passed) == {T_AREA}


def test_an_unnamed_area_is_neither_sides_area() -> None:
    """A ``None`` area has no orientation, even if the share would do."""
    areas = {None: observed(1.00, 100)}
    assert (
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )
        == {}
    )


@pytest.mark.parametrize("share", [-0.1, 1.1])
def test_an_impossible_t_share_threshold_is_refused(share: float) -> None:
    with pytest.raises(ValueError, match="is not in the range 0..1"):
        t_side_shares(orientation(), t_share_min=share, min_observations=20)


def test_a_non_positive_observation_threshold_is_refused() -> None:
    with pytest.raises(ValueError, match="is not positive"):
        t_side_shares(orientation(), t_share_min=T_SHARE, min_observations=0)


@pytest.mark.parametrize(
    "t,total",
    [(0, 0), (5, 4), (-1, 10)],
)
def test_impossible_observation_counts_are_refused(t: int, total: int) -> None:
    """Zero observations is not an area, and a subset cannot exceed the set."""
    with pytest.raises(ValueError):
        AreaObservations(t=t, total=total)


# --- I/O matrix: the CT advance -------------------------------------------------


def test_a_ct_player_on_a_t_side_area_on_an_eco_round_is_an_advance() -> None:
    """Matrix row 1: T share 0.88, eco, 30 s."""
    hits = advance(at(30.0, T_AREA, "ct1"))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == CT_ADVANCE
    assert hit.area == T_AREA
    assert hit.sample_t_s == 30.0
    assert hit.players == 1
    assert hit.t_share == pytest.approx(0.88, abs=0.01)
    assert hit.observations == 24
    assert hit.sources == ()


def test_an_area_below_the_share_threshold_is_no_anomaly() -> None:
    """Matrix row 4: Nuke's outside (0.70) is genuinely both sides' area."""
    areas = {SHARED_AREA: observed(0.70, 302)}
    assert advance(at(30.0, SHARED_AREA, "ct1"), areas=areas) == []


def test_an_area_with_too_few_observations_is_no_anomaly() -> None:
    """Matrix row 5: 8 observations -- the area is neither side's."""
    areas = {"Thin": observed(1.00, 8)}
    assert advance(at(30.0, "Thin", "ct1"), areas=areas) == []


def test_a_late_sample_point_is_outside_the_rule() -> None:
    """Matrix row 6: a hit at 45 s only is not an anomaly."""
    assert advance(at(45.0, T_AREA, "ct1")) == []


def test_an_early_sample_point_is_inside_the_rule() -> None:
    """The bound is ``<= 30 s``, and 15 s holds most of the calibration hits."""
    assert len(advance(at(15.0, T_AREA, "ct1"))) == 1


def test_the_sample_point_exactly_at_the_bound_is_kept() -> None:
    """30 s is at the bound, and the calibration's strongest hit is there."""
    assert len(advance(at(30.0, T_AREA, "ct1"))) == 1


@pytest.mark.parametrize("round_type", ["pistol", "full", "ot", "anomaly", None])
def test_advance_cannot_hit_outside_a_saving_round(round_type) -> None:
    """Matrix row 8: the restriction is an economic observation, not a sample."""
    assert advance(at(30.0, T_AREA, "ct1"), round_type=round_type) == []


@pytest.mark.parametrize("round_type", SAVING_ROUND_TYPES)
def test_advance_hits_on_every_saving_round_type(round_type: str) -> None:
    """Eco, force and half-buy are one observation: too little buying power."""
    assert len(advance(at(30.0, T_AREA, "ct1"), round_type=round_type)) == 1


def test_only_ct_rows_are_examined() -> None:
    """Matrix row 9: the same player as T and as CT -- CT rows only."""
    rows = at(30.0, T_AREA, "p1", side="T") + at(30.0, T_AREA, "p2", side="CT")
    hits = advance(rows)
    assert len(hits) == 1
    assert hits[0].players == 1


def test_a_dead_player_is_not_on_the_area() -> None:
    """The same rule as in the player counts: a dead player makes no row."""
    assert advance(at(30.0, T_AREA, "ct1", alive=False)) == []


def test_a_first_contact_row_never_triggers_the_rule() -> None:
    """First contact's ``sample_t_s`` is a measured moment, not a point."""
    rows = at(12.0, T_AREA, "ct1", kind="first_contact")
    assert advance(rows) == []


def test_the_same_player_twice_is_one_player() -> None:
    """The player count counts distinct players, not rows."""
    hits = advance(at(30.0, T_AREA, "ct1") + at(30.0, T_AREA, "ct1"))
    assert hits[0].players == 1


def test_the_player_minimum_is_a_threshold_not_a_constant() -> None:
    """The product owner chose 1, but 2 can be set without a code change."""
    rows = at(30.0, T_AREA, "ct1")
    assert advance(rows, min_players=1)
    assert advance(rows, min_players=2) == []


def test_two_sample_points_on_the_same_area_are_two_hits() -> None:
    """A hit is one point's observation; aggregation bundles them per round."""
    hits = advance(at(15.0, T_AREA, "ct1") + at(30.0, T_AREA, "ct1"))
    assert [hit.sample_t_s for hit in hits] == [15.0, 30.0]


def test_an_area_that_is_not_t_side_is_silent_even_with_five_players() -> None:
    """The player count does not replace the orientation."""
    areas = {T_AREA: observed(0.88, 24)}
    rows = at(30.0, "CTSpawn", "ct1", "ct2", "ct3", "ct4", "ct5")
    assert advance(rows, areas=areas) == []


def test_no_anomalies_is_a_valid_result() -> None:
    """Matrix row 7: an empty list is an observation and not an error."""
    assert advance([]) == []
    assert crunch([]) == []


# --- I/O matrix: the crunch -----------------------------------------------------


def test_two_players_arriving_from_two_areas_is_a_crunch() -> None:
    """Matrix row 2: 2 CT players, 2 different source areas."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    hits = crunch(rows)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == CRUNCH
    assert hit.area == T_AREA
    assert hit.sample_t_s == 30.0
    assert hit.players == 2
    assert hit.sources == ("SideEntrance", "TSideUpper")


def test_two_players_from_the_same_area_is_not_a_crunch() -> None:
    """One direction is not two directions, even with two players."""
    rows = at(15.0, "SideEntrance", "ct1", "ct2") + at(30.0, T_AREA, "ct1", "ct2")
    assert crunch(rows) == []


def test_a_player_already_on_the_area_did_not_arrive() -> None:
    """The source differs from the target -- else the player just stood still."""
    rows = (
        at(15.0, T_AREA, "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows) == []


def test_the_first_sample_point_of_a_round_has_no_source() -> None:
    """Without a previous sample point no direction is guessed."""
    rows = at(6.0, T_AREA, "ct1", "ct2")
    assert crunch(rows) == []


def test_an_unknown_previous_area_is_not_a_direction() -> None:
    """``None`` is not a direction, so it does not do as a source area."""
    rows = (
        at(15.0, None, "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows) == []


def test_crunch_counts_arrivals_not_everyone_on_the_area() -> None:
    """Three on the area, two arrived: the crunch's number is two."""
    rows = (
        at(15.0, T_AREA, "ct3")
        + at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2", "ct3")
    )
    hits = crunch(rows)
    assert len(hits) == 1
    assert hits[0].players == 2
    assert len(advance(rows)) == 2  # 15 s and 30 s: the advance counts all
    assert max(hit.players for hit in advance(rows)) == 3


def test_crunch_is_not_limited_to_saving_rounds() -> None:
    """Measured: one crunch of five is a full buy, so there is no restriction.

    The rule does not take the round type as an argument at all -- a
    restriction that does not exist cannot come back by accident. This is also
    why the crunch must not be described as a "stricter form" of the advance:
    on a full buy there is no advance row at all, so the hit sets intersect
    each other and neither contains the other.
    """
    rows = (
        at(15.0, "Arch", "ct1")
        + at(15.0, "TopofMid", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert len(crunch(rows)) == 1
    assert "round_type" not in crunch_hits.__annotations__


def test_a_crunch_round_also_hits_the_advance_rule() -> None:
    """Matrix row 3: both are stated; a crunch does not replace an advance."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert len(crunch(rows)) == 1
    advances = [hit for hit in advance(rows) if hit.sample_t_s == 30.0]
    assert len(advances) == 1
    assert advances[0].players == 2


def test_crunch_needs_the_area_to_be_t_side_too() -> None:
    """The advance's orientation condition holds; direction does not replace it."""
    areas = {T_AREA: observed(0.88, 24)}
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, "CTSpawn", "ct1", "ct2")
    )
    assert crunch(rows, areas=areas) == []


def test_a_late_crunch_is_outside_the_rule() -> None:
    """The time bound is shared: MatureMayhem's 45 s crunch falls the same way."""
    rows = (
        at(30.0, "BombsiteA", "ct1")
        + at(30.0, "MainHall", "ct2")
        + at(45.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows) == []


def test_a_source_after_the_time_bound_still_counts_as_a_source() -> None:
    """The source area comes from the previous point, not the time bound.

    Without this a 30 s crunch would lose its source area if the previous
    sample point were outside the time bound -- and the arrival would go
    unseen.
    """
    rows = (
        at(6.0, "SideEntrance", "ct1")
        + at(6.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
        + at(45.0, T_AREA, "ct1", "ct2")
    )
    hits = crunch(rows)
    assert [hit.sample_t_s for hit in hits] == [30.0]


def test_the_source_minimum_is_a_threshold() -> None:
    """Three directions can be set without a code change."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows, min_sources=2)
    assert crunch(rows, min_sources=3) == []


def test_a_duplicated_row_at_the_target_area_does_not_eat_the_source() -> None:
    """A duplicated row used to pair up with itself and silence the crunch.

    A reproduced finding: one extra ``p1`` row at 30 s made the source area
    the target area, after which ``source == area`` skipped the arrival. A
    duplicated row is not theoretical -- ``_players_by_point`` already guards
    against it with a set, and the same guard is needed for the source areas.
    """
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
        + at(30.0, T_AREA, "ct1")  # a duplicated row
    )
    hits = crunch(rows)
    assert len(hits) == 1
    assert hits[0].players == 2
    assert hits[0].sources == ("SideEntrance", "TSideUpper")


def test_a_duplicated_row_does_not_inflate_the_player_count() -> None:
    """The same guard the other way: the count is of distinct players."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows)[0].players == 2


@pytest.mark.parametrize("written", [f" {T_AREA} ", f"{T_AREA}\t", f"\n{T_AREA}"])
def test_stray_whitespace_in_an_area_name_still_matches(written: str) -> None:
    """The orientation and the presence are normalised by the same function."""
    hits = advance(at(30.0, written, "ct1"))
    assert [hit.area for hit in hits] == [T_AREA]


def test_an_empty_area_name_is_not_an_area() -> None:
    """``""`` is not an area: it would otherwise make it into the T areas."""
    areas = {"": observed(1.00, 100), T_AREA: observed(0.88, 24)}
    assert set(t_side_shares(
        areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
    )) == {T_AREA}
    assert advance(at(30.0, "", "ct1"), areas=areas) == []


def test_the_same_area_in_two_spellings_is_refused() -> None:
    """One of two spellings cannot be chosen -- the caller normalises."""
    areas = {T_AREA: observed(0.88, 24), f" {T_AREA}": observed(0.20, 30)}
    with pytest.raises(ValueError, match="twice in different"):
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )


def test_an_area_exactly_at_the_observation_bound_is_included() -> None:
    """``>=`` and not ``>``: an area with exactly 20 observations is included.

    The precision carries weight, because the calibration leans on exact
    bounds. The wording "does not exceed the bound" would mean the opposite,
    and it was precisely that contradiction that the review corrected.
    """
    areas = {T_AREA: observed(0.90, 20)}
    assert set(
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )
    ) == {T_AREA}
    assert len(advance(at(30.0, T_AREA, "ct1"), areas=areas)) == 1


def test_an_area_one_observation_below_the_bound_is_excluded() -> None:
    """The guard's other direction: 19 observations fall below the bound."""
    areas = {T_AREA: observed(0.90, 19)}
    assert (
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )
        == {}
    )


def test_an_area_exactly_at_the_share_bound_is_included() -> None:
    """The same rule for the share: exactly 0.80 counts."""
    areas = {T_AREA: observed(0.80, 100)}
    assert len(advance(at(30.0, T_AREA, "ct1"), areas=areas)) == 1


def test_the_infernos_five_player_crunch_is_measured_as_five() -> None:
    """The strongest calibration hit: 5 CT in mid from two directions."""
    areas = {"Middle": observed(0.83, 60)}
    rows = (
        at(6.0, "Arch", "ct1", "ct2", "ct3")
        + at(6.0, "TopofMid", "ct4", "ct5")
        + at(15.0, "Middle", "ct1", "ct2", "ct3", "ct4", "ct5")
    )
    hits = crunch(rows, areas=areas)
    assert len(hits) == 1
    assert hits[0].players == 5
    assert hits[0].sources == ("Arch", "TopofMid")


# --- Stack (Story 2.14) ---------------------------------------------------------
#
# The rule and its derived input are in the same module, so every row of the
# I/O matrix is one function call away here -- without a demo, without an
# archive. The calibration's real numbers are in test_calibration.py.

#: The clouds are in ``conftest``, because the same geometry is shared by
#: three files: the rule here, the aggregation in ``test_aggregate`` and the
#: stage in ``test_stage_aggregate``. Of three copies they could drift apart,
#: and not one test would say so.
CLOUD = SITE_CLOUD
CLOUD_OVERLAPPING = OVERLAPPING_SITE_CLOUD

#: **Read from the model's default, not written here.** A hand-written copy
#: made this module blind to the setting: on 2026-09-11 the shipped margin
#: changed and every test in this file went on measuring 1.25.
#:
#: The chain is two links, and it is worth being exact about what each
#: catches. ``test_threshold_defaults_match_the_settings_file`` pins this
#: default to ``settings.toml``; the grouping tests below then react to the
#: default. An edit to ``settings.toml`` **alone** is caught by that equality
#: assertion and not by behaviour here; an edit to both is caught here.
#: Neither link is sufficient on its own.
MARGIN = ThresholdSettings(pistol_rounds=[1, 13]).stack_group_margin
#: Read from the model's default for the same reason as ``MARGIN`` above.
SEPARATION_MIN = ThresholdSettings(
    pistol_rounds=[1, 13]
).stack_site_separation_min
STACK_MIN_PLAYERS = 4


def cloud(cells=CLOUD) -> list[CloudCell]:
    return [CloudCell(area, x, y, z) for area, x, y, z in cells]


def groups(cells=CLOUD) -> dict[str, str] | None:
    return site_groups(
        cloud(cells), margin=MARGIN, separation_min=SEPARATION_MIN
    )


def stack(
    rows: list[AreaPresence],
    *,
    cells=CLOUD,
    min_players: int = STACK_MIN_PLAYERS,
):
    return stack_hits(
        rows,
        groups=groups(cells),
        max_sample_s=MAX_SAMPLE_S,
        min_players=min_players,
    )


def test_the_cloud_can_see_the_margin_move() -> None:
    """The fixture must be able to SEE ``stack_group_margin`` change.

    Without this guard the sensitive areas in ``conftest.SITE_CLOUD`` are
    decoration. Measured on 2026-09-11: before they were added the cloud
    grouped **identically for every margin from 1.00 to 5.66** (swept in 0.01
    steps), and a wrong shipped value passed a green fast suite. A suite
    cannot guard a number it cannot see.

    ``Outside`` (1.3256) and ``TopofMid`` (1.2222) straddle the shipped 1.25,
    so the blind band is ``[1.2222, 1.3256)``. This test asserts against the
    **shipped** margin rather than a constant, and it is red on both sides:
    above 1.3256 there is nothing left to drop, and at or below about 1.10
    ``MARGIN * 1.2`` no longer reaches ``Outside``'s ratio. Red here means
    "the fixture has gone blind at the shipped value", not "the margin is
    wrong" -- the repair is a new sensitive area, not a new number.

    The probe ``MARGIN * 1.2`` only certifies that *some* breakpoint lies in
    ``(MARGIN, 1.2 * MARGIN]``. The two assertions below name which areas must
    do the work, so a future repair cannot satisfy the guard while leaving
    ``Outside`` decorative.
    """
    at_shipped = groups()
    wider = site_groups(cloud(), margin=MARGIN * 1.2, separation_min=SEPARATION_MIN)
    assert at_shipped != wider, (
        f"The cloud groups identically at {MARGIN} and {MARGIN * 1.2}, so no "
        "test in this file can see the margin. Add an area whose ratio falls "
        "between them."
    )
    assert "Outside" in at_shipped and "Outside" not in wider, (
        "``Outside`` is the area meant to move with the margin: grouped at "
        f"{MARGIN}, ungrouped at {MARGIN * 1.2}."
    )
    assert "TopofMid" not in at_shipped, (
        "``TopofMid`` is the downward sensor and must be UNgrouped at the "
        f"shipped {MARGIN}, so that a LOWERED margin is visible too."
    )


def test_the_clouds_can_see_the_separation_threshold_move() -> None:
    """``stack_site_separation_min`` must be visible to a fixture too.

    Measured 2026-09-11, before these two clouds existed: raising the shipped
    2.0 to **10.0** -- a five-fold error, still inside the model's ceiling of
    20 -- failed **one test of 3 225**, the value lock, and nothing at rule
    level. The suite owned two clouds with separation ratios 25 and 0.05, and
    every value the model allows falls between them, so no fixture could see
    the setting at all.

    It is the more dangerous of the two geometry thresholds: it decides
    whether a map's site division is trusted **at all**. Too high and maps go
    quiet while the coverage says they were examined; too low and Nuke's
    vertically overlapping sites are reported as a real division.

    **What these fixtures do and do not buy.** On the archive as it stands, no
    value in ``[0.6, 3.6]`` changes any map's outcome -- the real ratios are
    Nuke 0.47-0.54 and the others 3.70-5.04, so the archive suite catches the
    ends of the range and is itself blind across ``[0.55, 3.69]``. These
    fixtures therefore guard two things the archive cannot: a machine with no
    archive at all (the fast suite must stand alone), and a future map whose
    ratio lands in the middle band -- which is exactly the case
    ``settings.toml`` anticipates when it says including Nuke needs its own
    derivation for vertically separated sites.

    The pair is two-sided on purpose. ``JUST_OVER_SEPARATION`` (ratio 2.25)
    speaks at the shipped value and goes quiet if it is raised past 2.25;
    ``JUST_UNDER_SEPARATION`` (1.75) is silent at the shipped value and speaks
    if it is lowered **to 1.75 or below** -- the condition is
    ``separation < separation_min * span``, so an equal ratio is accepted. The
    With the two probes below the guard is red outside ``(1.875, 2.1875]`` --
    tighter than the fixtures' own ``(1.75, 2.25]``, because probe 1 needs
    ``1.75 >= 0.8 * shipped`` and probe 2 needs ``2.25 < 1.2 * shipped``. Red
    here means the fixtures have gone blind at the shipped value: the repair
    is a new ratio, not a new threshold.

    **The two probes are not decoration.** ``site_groups`` returns ``None``
    for four different reasons -- a missing site, zero span, zero separation,
    and this threshold -- and a bare ``is None`` cannot tell them apart.
    Measured: rename ``BombsiteB`` in the silent fixture and it still returns
    ``None``, for the wrong reason, while both assertions stay green and the
    downward sensor is gone without a word. The probes move the threshold
    instead of the cloud, so only the separation rule can satisfy them.

    They **bound** the ratios rather than pinning them: probe 1 forces the
    silent cloud into ``[0.8 * shipped, shipped)`` and probe 2 the speaking one
    into ``[shipped, 1.2 * shipped)``. A fixture edit inside those windows
    still passes -- measured, adding one cell at x=5 to the speaking cloud
    drops its ratio to exactly 2.0 and 401 tests stay green -- so the windows
    are a fence, not a pin.

    The last assertion guards the **boundary itself**. The code refuses on
    ``separation < separation_min * span``, so a ratio *equal* to the threshold
    is accepted, and an off-by-one edit to ``<=`` would reverse that. Measured:
    that mutation survives all 778 tests in the seven files that touch this
    code. No fixture sits at ratio == threshold, and x=8 -- the offset that
    would -- is avoided deliberately, so the boundary has to be probed
    explicitly instead.
    """
    speaks = groups(JUST_OVER_SEPARATION_SITE_CLOUD)
    assert speaks == {"BombsiteA": "A", "BombsiteB": "B"}, (
        f"At the shipped {SEPARATION_MIN} a cloud whose sites separate by a "
        "ratio of 2.25 must yield both sites' groups. If this is red, the "
        "shipped threshold has risen past 2.25, or the fixture's sites have "
        "been renamed and it is no longer measuring separation at all."
    )
    assert groups(JUST_UNDER_SEPARATION_SITE_CLOUD) is None, (
        f"At the shipped {SEPARATION_MIN} a cloud whose sites separate by "
        "only 1.75 must be refused. If this is red, the shipped threshold has "
        "fallen to 1.75 or below -- the direction that reports a map whose "
        "sites do not separate as though they did."
    )
    # The silent one must be silent BECAUSE of the threshold: lower it and the
    # same cloud speaks. Without this, a renamed site would pass the assertion
    # above for the wrong reason.
    assert (
        site_groups(
            cloud(JUST_UNDER_SEPARATION_SITE_CLOUD),
            margin=MARGIN,
            separation_min=SEPARATION_MIN * 0.8,
        )
        is not None
    ), (
        "The silent fixture must speak once the threshold drops below its "
        "ratio. If this is red, its ``None`` came from something other than "
        "the separation rule -- a missing site, or a zero span."
    )
    # And the speaking one must go quiet when the threshold rises above its
    # ratio, which pins that ratio from the other side.
    assert (
        site_groups(
            cloud(JUST_OVER_SEPARATION_SITE_CLOUD),
            margin=MARGIN,
            separation_min=SEPARATION_MIN * 1.2,
        )
        is None
    ), (
        "The speaking fixture must fall silent once the threshold rises above "
        "its ratio. If this is red, its ratio has drifted upward and the "
        "blind band is wider than this test claims."
    )
    # The boundary is inclusive: a ratio EQUAL to the threshold is accepted.
    # Without this, changing ``<`` to ``<=`` in site_groups passes every test
    # in the suite -- measured, 778 of 778 green.
    assert (
        site_groups(
            cloud(JUST_OVER_SEPARATION_SITE_CLOUD),
            margin=MARGIN,
            separation_min=2.25,
        )
        is not None
    ), (
        "A cloud whose separation ratio is EXACTLY the threshold must be "
        "accepted: the refusal is ``separation < separation_min * span``. If "
        "this is red, that comparison has become ``<=`` and every map sitting "
        "exactly on the threshold now goes quiet."
    )


def test_the_site_groups_are_read_off_the_demos_own_point_cloud() -> None:
    """Every area gets its group from the nearer site -- or neither.

    ``Middle`` is equally far from both, so it is **absent from the mapping**:
    an area without a group is not a ``None`` value but a missing key, so that
    ``groups.get(area)`` is unambiguous.
    """
    found = groups()
    assert found == {
        "BombsiteA": "A",
        "BombsiteB": "B",
        "House": "A",
        "Outside": "A",
        "CTSpawn": "A",
        "SideEntrance": "B",
        "Ramp": "B",
        "TSpawn": "B",
    }
    assert "Middle" not in found
    # ``TopofMid`` (1.2222) sits just below the shipped margin, so it is
    # ungrouped here -- that is what makes a LOWERED margin visible.
    assert "TopofMid" not in found


def test_four_defenders_in_one_sites_group_are_a_stack() -> None:
    """The I/O matrix's first row: 4 CT in the B group, one on the site.

    The hit says four things: the area (the site's own), the player count,
    those alive and the sample point. The area is ``BombsiteB`` and not
    ``SideEntrance``, even though the latter holds more players: the row names
    **the group's anchor**, not the area that happened to be crowded.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1")
        + at(15.0, "SideEntrance", "ct2", "ct3")
        + at(15.0, "Ramp", "ct4")
        + at(15.0, "House", "ct5")
    )
    hits = stack(rows)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == STACK
    assert hit.area == "BombsiteB"
    assert hit.site == "B"
    assert hit.players == 4
    assert hit.alive == 5
    assert hit.sample_t_s == 15.0
    # The orientation does not concern the stack, and the empty field is what
    # keeps it apart from the two other rules.
    assert hit.t_share is None
    assert hit.observations is None
    assert hit.sources == ()


def test_half_the_map_is_not_a_site() -> None:
    """The I/O matrix's second row: four in the group, none on the site.

    The players' own phrase "Stack sitellä" means being on the site, not being
    on that half of the map. Measured: the condition drops 17 rounds -> 9.
    """
    rows = at(15.0, "SideEntrance", "ct1", "ct2") + at(
        15.0, "Ramp", "ct3", "ct4"
    )
    assert stack(rows) == []


def test_a_player_in_spawn_does_not_defend_a_site() -> None:
    """The I/O matrix's third row: two in spawn, two left.

    ``CTSpawn`` is in the A group in this cloud, as on Ancient. Without the
    spawn restriction the starting setup alone would fire the rule.
    """
    rows = at(15.0, "BombsiteA", "ct1", "ct2") + at(
        15.0, "CTSpawn", "ct3", "ct4"
    )
    assert stack(rows) == []
    # ``TSpawn`` is in the B group, and the same restriction covers it: a CT
    # player in the T spawn is already a different observation (ct_advance),
    # not a stack.
    b_side = at(15.0, "BombsiteB", "ct1", "ct2") + at(
        15.0, "TSpawn", "ct3", "ct4"
    )
    assert stack(b_side) == []
    # The guard's other direction: the same four players on the group's areas
    # do hit.
    ok = at(15.0, "BombsiteA", "ct1", "ct2") + at(15.0, "House", "ct3", "ct4")
    assert len(stack(ok)) == 1


def test_a_map_whose_sites_do_not_separate_stays_silent() -> None:
    """The I/O matrix's fourth row: Nuke's sites are on top of each other.

    The guard is **a ratio and not a list of maps**: no map is named in the
    code, the distance between the sites is divided by the sites' own size
    instead. Going quiet is the right answer -- and the caller has to record
    it in the coverage.
    """
    assert groups(CLOUD_OVERLAPPING) is None
    rows = at(15.0, "BombsiteA", "ct1", "ct2", "ct3", "ct4")
    assert stack(rows, cells=CLOUD_OVERLAPPING) == []


def test_the_dead_are_not_the_denominator() -> None:
    """The I/O matrix's fifth row: 4 alive, all in the group -> 4/4.

    Four out of five is the defence's choice, four out of four is what was
    left. The player count alone does not tell them apart.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1", "ct2")
        + at(15.0, "SideEntrance", "ct3", "ct4")
        + at(15.0, "House", "ct5", alive=False)
    )
    hits = stack(rows)
    assert len(hits) == 1
    assert (hits[0].players, hits[0].alive) == (4, 4)


def test_a_cloud_without_a_site_gives_no_groups() -> None:
    """The I/O matrix's sixth row: an empty cloud or a missing site.

    The absence of an observation is not an observation that the division is
    absent, so both are ``None``.
    """
    assert groups(()) is None
    assert groups((("BombsiteB", 100, 0, 0), ("House", 10, 0, 0))) is None


def test_sites_without_a_size_of_their_own_are_silenced() -> None:
    """A hole in the guard: with one-cell sites the ratio would divide by zero.

    A cloud of two cells says nothing about the map's site structure, so *any*
    difference at all would pass the threshold -- that is, the guard would not
    measure anything any more. A demo whose cloud is that thin is broken, not
    a map whose sites separate evenly.
    """
    assert groups(
        (("BombsiteA", 0, 0, 0), ("BombsiteB", 100, 0, 0))
    ) is None


def test_two_sites_at_the_same_point_are_silenced() -> None:
    """Neither would be genuinely nearer to any area."""
    same = (
        ("BombsiteA", 0, 0, 0),
        ("BombsiteA", 2, 0, 0),
        ("BombsiteA", -2, 0, 0),
        ("BombsiteB", 0, 0, 0),
        ("BombsiteB", 2, 0, 0),
        ("BombsiteB", -2, 0, 0),
    )
    assert groups(same) is None


def test_a_late_sample_point_is_outside_the_shared_time_bound() -> None:
    """The I/O matrix's seventh row: a hit at 45 s only is not a hit.

    The time bound is **shared** with the two other rules
    (``advance_max_sample_s``), not a threshold of the stack's own.
    """
    rows = at(45.0, "BombsiteB", "ct1", "ct2", "ct3", "ct4")
    assert stack(rows) == []


def test_the_player_threshold_comes_from_the_settings() -> None:
    """The threshold is read from the settings and not hard-coded.

    The same claim as in the acceptance criterion ``stack_min_players = 5``,
    but without demos: a four-player setup disappears, a five-player one does
    not.
    """
    four = at(15.0, "BombsiteB", "ct1", "ct2") + at(
        15.0, "SideEntrance", "ct3", "ct4"
    )
    assert len(stack(four)) == 1
    assert stack(four, min_players=5) == []
    five = four + at(15.0, "Ramp", "ct5")
    assert len(stack(five, min_players=5)) == 1


def test_the_same_player_twice_does_not_raise_the_stack_count() -> None:
    """The players are a set: a duplicated row does not make three into four.

    The same guard as on the advance and the crunch, and for the same reason:
    the player count is the report's number.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1", "ct2", "ct3")
        + at(15.0, "SideEntrance", "ct1")
        + at(15.0, "Ramp", "ct2")
    )
    assert stack(rows) == []


def test_stack_reads_only_alive_ct_time_rows() -> None:
    """Three restrictions the stack shares with the two other rules."""
    site = "BombsiteB"
    assert stack(at(15.0, site, "t1", "t2", "t3", "t4", side="T")) == []
    assert stack(at(15.0, site, "c1", "c2", "c3", "c4", alive=False)) == []
    assert (
        stack(at(15.0, site, "c1", "c2", "c3", "c4", kind=FIRST_CONTACT_SAMPLE))
        == []
    )


def test_two_sample_points_are_two_stack_hits_on_one_round() -> None:
    """A hit is **one sample point's** observation, as on the two other rules.

    Grouping them into a sample is done in the aggregation, not here. In the
    calibration Anubis round 4 is exactly this case: 15 s and 30 s, 5/5 and
    4/5.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1", "ct2")
        + at(15.0, "SideEntrance", "ct3", "ct4", "ct5")
        + at(30.0, "BombsiteB", "ct1", "ct2", "ct3", "ct4")
        + at(30.0, "House", "ct5")
    )
    hits = stack(rows)
    assert [(h.sample_t_s, h.players, h.alive) for h in hits] == [
        (15.0, 5, 5),
        (30.0, 4, 5),
    ]


def test_the_group_margin_leaves_the_shared_middle_out() -> None:
    """The margin is what makes the shared middle shared.

    ``Corner`` is 40 cells from A and 60 from B, that is, a ratio of 1.5: with
    a margin of 1.0 it is in A's group, with a margin of 1.6 in neither's.
    ``Middle`` is exactly in the centre and belongs to neither even with a
    margin of 1.0, because neither site is genuinely nearer.
    """
    near_a = [CloudCell("Corner", 40, 0, 0), *cloud()]
    loose = site_groups(near_a, margin=1.0, separation_min=SEPARATION_MIN)
    tight = site_groups(near_a, margin=1.6, separation_min=SEPARATION_MIN)
    assert loose is not None and tight is not None
    assert loose["Corner"] == "A"
    assert "Corner" not in tight
    assert "Middle" not in loose


def test_the_stack_thresholds_are_refused_when_they_would_break_the_rule() -> None:
    """Three values that would silently make the rule something else."""
    with pytest.raises(ValueError, match=r"below 1\.0"):
        site_groups(cloud(), margin=0.9, separation_min=SEPARATION_MIN)
    with pytest.raises(ValueError, match="is not positive"):
        site_groups(cloud(), margin=MARGIN, separation_min=0.0)
    with pytest.raises(ValueError, match="is not positive"):
        stack_hits([], groups={}, max_sample_s=MAX_SAMPLE_S, min_players=0)


def test_a_group_that_is_not_a_site_is_refused() -> None:
    """The groups come from ``site_groups``, not from any map at all."""
    with pytest.raises(ValueError, match="an unknown group"):
        stack_hits(
            at(15.0, "BombsiteB", "ct1"),
            groups={"BombsiteB": "C"},
            max_sample_s=MAX_SAMPLE_S,
            min_players=1,
        )


def test_a_silenced_demo_looks_the_same_as_a_measured_negative_from_here() -> None:
    """A silenced demo and a measured negative are the same empty list.

    The difference is **not** in the rule's return value but in the coverage,
    and that is exactly why the caller has to record the silence separately:
    seen from here, a Nuke round and "no stacks" look the same.

    An empty group mapping is **the caller's own value** here, not the result
    of ``site_groups``: a site's distance to its own centre is 0, so each site
    always belongs to its own group and the function cannot return an empty
    dictionary. The rule still stands up to it, because it is public.
    """
    rows = at(15.0, "BombsiteB", "ct1", "ct2", "ct3", "ct4")
    assert (
        stack_hits(rows, groups=None, max_sample_s=MAX_SAMPLE_S, min_players=4)
        == []
    )
    assert (
        stack_hits(rows, groups={}, max_sample_s=MAX_SAMPLE_S, min_players=4)
        == []
    )
    # And the claim that an empty mapping cannot come out of the derivation.
    assert groups() != {}


def test_a_hit_cannot_carry_a_field_its_rule_did_not_measure() -> None:
    """``AnomalyHit``'s guard in both directions.

    The same justification ``sources`` already had: a field belongs to the
    rule that measured it, because the report's reader sees only the value and
    not its source.
    """
    with pytest.raises(ValueError, match="does not carry the area's orientation"):
        AnomalyHit(rule=CT_ADVANCE, area=T_AREA, sample_t_s=6.0, players=1)
    with pytest.raises(ValueError, match="carries the area's orientation"):
        AnomalyHit(
            rule=STACK,
            area="BombsiteA",
            sample_t_s=6.0,
            players=4,
            alive=5,
            site="A",
            t_share=0.9,
            observations=30,
        )
    with pytest.raises(ValueError, match="carries the stack's fields"):
        AnomalyHit(
            rule=CRUNCH,
            area=T_AREA,
            sample_t_s=6.0,
            players=2,
            t_share=0.9,
            observations=30,
            alive=5,
        )
    with pytest.raises(ValueError, match="subset of those alive"):
        AnomalyHit(
            rule=STACK,
            area="BombsiteA",
            sample_t_s=6.0,
            players=5,
            alive=4,
            site="A",
        )
