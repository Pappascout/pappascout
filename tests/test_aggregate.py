"""``domain.aggregate`` -- the tests for the aggregation's computation.

Every table is built by hand, and not one test needs a demo or the archive:
every row of the I/O matrix is in this file as a test of its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import polars as pl
import pytest

from conftest import OVERLAPPING_SITE_CLOUD, SITE_CLOUD
from pappascout.domain.aggregate import (
    CLASSIFY_THRESHOLD_KEYS,
    MISSING_ROSTER_CLASS_LABEL,
    ROSTER_SAMPLE_BUCKETS,
    area_distributions,
    armed_players_for,
    armored_by_round,
    armored_players_for,
    bucket_labels,
    build_report,
    demo_buckets,
    first_contact_areas,
    lineups_of_same_team,
    roster_class_values,
    roster_demo_buckets,
    roster_entries,
    roster_sample_for,
    team_identity,
    map_name_for,
    observed_map_name,
    weakest_map_source,
    players_distribution,
    positions_for,
    sample_for,
    seconds_bucket,
    team_slug,
    check_rounds_are_unique,
    classify_thresholds,
    deaths_for,
    unpaired_detonations,
    utility_counts_for,
    utility_uses,
)
from pappascout.constants import ROSTER_CLASS_BUCKET
from pappascout.domain.models import AggregateSettings, ThresholdSettings
from pappascout.domain.report import (
    SLUG_FALLBACK,
    MissingDemo,
    RosterEntry,
    TeamReport,
)
from pappascout.domain.sampling import AreaObservations, CloudCell
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.errors import AggregateError, SchemaError

TEAM = "aaaaaaaaaaaaaaaa"
OPPONENT = "bbbbbbbbbbbbbbbb"
MAP_POOL = ["de_ancient", "de_anubis", "de_inferno", "de_nuke", "de_mirage"]


def thresholds(**overrides: object) -> ThresholdSettings:
    """The thresholds for a test; the defaults are the production ones."""
    values: dict[str, object] = {"pistol_rounds": [1, 13]}
    values.update(overrides)
    return ThresholdSettings(**values)


def aggregate_settings(**overrides: object) -> AggregateSettings:
    """The ``[aggregate]`` section for a test; the default is production's."""
    return AggregateSettings(**overrides)


# --- Building the tables --------------------------------------------------------


def _inputs(armed: int | None) -> dict[str, object]:
    """Every field of the ``CLASSIFIED.inputs`` struct, so the schema fits."""
    return {
        "money_buy_end": 0,
        "money_spent": 0,
        "money_players": [0, 0, 0, 0, 0],
        "equip_buy_end": 0,
        "equip_round_start": 0,
        "survivors_prev": 0,
        "survivors_equip_prev": 0,
        "prev_round_won": False,
        "players": 5,
        "players_readable": 5,
        "players_armed": armed,
        "loss_bonus_if_lost": 1400,
        "players_can_buy": 0,
        "full_equip_min": 4000,
        "force_buy_min": 1500,
        "armed_players_min": 3,
        "normal_buy_money_min": 4000,
        "normal_buy_players_min": 3,
        "anomaly_equip_max_after_win": 2000,
    }


def classified_row(
    demo: str,
    round_no: int,
    *,
    side: str = "T",
    round_type: str | None = "pistol",
    is_league: bool | None = None,
    roster_class: str | None = None,
    armed: int | None = 5,
) -> dict[str, object]:
    return {
        "map_demo_id": demo,
        "round_no": round_no,
        "side": side,
        "won": True,
        "round_type": round_type,
        "opp_round_type": "pistol",
        "loss_count": 1,
        "reason": "testi",
        "inputs": _inputs(armed),
        "is_league": is_league,
        "roster_class": roster_class,
    }


def classified_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(CLASSIFIED))
    return validate(df, CLASSIFIED, "classified")


def tick_row(
    demo: str,
    round_no: int,
    player: str,
    area: str | None,
    *,
    lineup: str = TEAM,
    side: str = "T",
    sample_kind: str = "time",
    sample_t_s: float = 6.0,
    is_alive: bool = True,
) -> dict[str, object]:
    return {
        "map_demo_id": demo,
        "round_raw": round_no + 1,
        "round_no": round_no,
        "player_id": player,
        "lineup_key": lineup,
        "side": side,
        "sample_kind": sample_kind,
        "sample_t_s": sample_t_s,
        "t_s": sample_t_s,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "area": area,
        "is_alive": is_alive,
    }


def ticks_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(TICKS))
    return validate(df, TICKS, "ticks")


#: The clan names the lineup table gives in the test archive. Measured from
#: real demos: the report's heading is this very string.
TEAM_CLAN = "MatureMayhem"
OPPONENT_CLAN = "KALJUKOSTAJA"


def lineup_row(
    demo: str,
    player: str,
    *,
    lineup: str = TEAM,
    player_name: str | None = None,
    clan_name: str | None = TEAM_CLAN,
) -> dict[str, object]:
    """One row of the lineup table.

    ``player_name`` and ``clan_name`` are observations: ``None`` means "not
    observed" and it is not replaced by the id.
    """
    return {
        "map_demo_id": demo,
        "lineup_key": lineup,
        "player_id": player,
        "player_name": player_name,
        "clan_name": clan_name,
    }


def lineups_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(LINEUPS))
    return validate(df, LINEUPS, "lineups")


def match_frame(demo: str, map_name: str | None) -> pl.DataFrame:
    """The match table: one row, the map name from the header or ``None``.

    No ``orient``: the dictionary row names its column itself.
    """
    df = pl.DataFrame(
        [{"map_demo_id": demo, "map_name": map_name}], schema=dict(MATCH)
    )
    return validate(df, MATCH, "match")


def callouts_frame(
    demo: str, cells: Sequence[tuple[str, int, int, int]] = ()
) -> pl.DataFrame:
    """The point cloud: a row per cell ``(area, cell_x, cell_y, cell_z)``.

    The default is an **empty cloud**, and that is deliberate: no site groups
    are obtained from it, so stack stays silent and every old test measures
    what it measured before Story 2.14. A test that needs groups supplies the
    cells itself.
    """
    df = pl.DataFrame(
        [
            {
                "map_demo_id": demo,
                "cell_x": x,
                "cell_y": y,
                "cell_z": z,
                "area": area,
                "observations": 1,
            }
            for area, x, y, z in cells
        ],
        schema=dict(CALLOUT_CLOUD),
    )
    return validate(df, CALLOUT_CLOUD, "callouts")


def event_rows(
    demo: str,
    round_no: int,
    grenade_no: int,
    grenade_type: str,
    *,
    throw_area: str | None = "TSpawn",
    detonate_area: str | None = "BombsiteB",
    t_s: float = 3.0,
    lineup: str = TEAM,
    side: str = "T",
) -> list[dict[str, object]]:
    """The throw and the detonation as a pair, as ``parse`` writes them."""
    common = {
        "map_demo_id": demo,
        "round_raw": round_no + 1,
        "round_no": round_no,
        "grenade_no": grenade_no,
        "grenade_entity_id": 100 + grenade_no,
        "grenade_type": grenade_type,
        "thrower_id": "p1",
        "lineup_key": lineup,
        "side": side,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
    }
    return [
        {
            **common,
            "event_kind": "grenade_thrown",
            "t_s": t_s,
            "area": throw_area,
            "area_source": None if throw_area is None else "observed",
            "snap_distance": None,
        },
        {
            **common,
            "event_kind": "grenade_detonate",
            "t_s": t_s + 2.0,
            "area": detonate_area,
            "area_source": None if detonate_area is None else "point_cloud",
            "snap_distance": None if detonate_area is None else 120.0,
        },
    ]


def events_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(EVENTS))
    return validate(df, EVENTS, "events")


def death_row(
    demo: str,
    round_no: int,
    *,
    victim: str = "p1",
    victim_lineup: str = TEAM,
    victim_side: str = "T",
    victim_area: str | None = "Cave",
    attacker: str | None = "o1",
    attacker_lineup: str | None = OPPONENT,
    attacker_side: str | None = "CT",
    attacker_area: str | None = "Middle",
    t_s: float = 24.0,
    weapon: str = "ak47",
) -> dict[str, object]:
    """One death row, as ``parse`` writes it.

    The default is a kill made by the opponent on one of our own players: the
    victim is in :data:`TEAM`'s lineup and the shooter in :data:`OPPONENT`'s.
    Own kills are built by swapping ``attacker_lineup``.
    """
    return {
        "map_demo_id": demo,
        "round_raw": round_no + 1,
        "round_no": round_no,
        "t_s": t_s,
        "victim_id": victim,
        "victim_lineup_key": victim_lineup,
        "victim_side": victim_side,
        "victim_x": 1.0,
        "victim_y": 2.0,
        "victim_z": 3.0,
        "victim_area": victim_area,
        "attacker_id": attacker,
        "attacker_lineup_key": None if attacker is None else attacker_lineup,
        "attacker_side": None if attacker is None else attacker_side,
        "attacker_x": None if attacker is None else 4.0,
        "attacker_y": None if attacker is None else 5.0,
        "attacker_z": None if attacker is None else 6.0,
        "attacker_area": None if attacker is None else attacker_area,
        "weapon": weapon,
    }


def deaths_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(DEATHS))
    return validate(df, DEATHS, "deaths")


def round_row(
    demo: str,
    round_no: int | None,
    *,
    side: str = "T",
    lineup: str = TEAM,
    armored: int | None = 5,
    armed: int | None = 0,
) -> dict[str, object]:
    """One row of the rounds table. Only the armour count is read from here.

    The default is a pistol round's set-up -- five kevlars, zero armed --
    because that is exactly what tells the two counts apart.
    """
    return {
        "map_demo_id": demo,
        "round_raw": None if round_no is None else round_no + 1,
        "round_no": round_no,
        "lineup_key": lineup,
        "side": side,
        "won": True,
        "win_reason": "elimination",
        "money_buy_end": 0,
        "money_spent": 0,
        "equip_buy_end": 0,
        "equip_round_start": 0,
        "players_buy_end": 5,
        MONEY_DISTRIBUTION_COLUMN: [0, 0, 0, 0, 0],
        ARMED_COLUMN: armed,
        ARMORED_COLUMN: armored,
        "survivors": 5,
        "survivors_equip_prev": 0,
        "freeze_end_tick": 100,
        "buy_end_tick": 200,
        "tick_rate": 64.0,
        "status": "ok",
    }


def rounds_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(ROUNDS))
    return validate(df, ROUNDS, "rounds")


def rounds_for(classified: list[dict[str, object]]) -> list[dict[str, object]]:
    """A rounds table covering exactly the classified rounds given.

    The armour count is the default one; a test that examines it builds its
    own rounds table.
    """
    return [
        round_row(str(row["map_demo_id"]), row["round_no"], side=str(row["side"]))
        for row in classified
    ]


def team_report(lineups: list[str] | None = None) -> TeamReport:
    keys = lineups or [TEAM]
    return TeamReport(
        key=keys[0],
        slug=team_slug(keys[0]),
        display_name=keys[0],
        lineup_keys=keys,
        roster=[
            RosterEntry(player_id=f"p{n}", display_name=f"nimi{n}")
            for n in range(1, 6)
        ],
        roster_source="lineups",
    )


def report_for(
    classified: list[dict[str, object]],
    ticks: list[dict[str, object]] | None = None,
    events: list[dict[str, object]] | None = None,
    deaths: list[dict[str, object]] | None = None,
    *,
    rounds: list[dict[str, object]] | None = None,
    limits: ThresholdSettings | None = None,
    windows: AggregateSettings | None = None,
    missing: list[MissingDemo] | None = None,
    lineups: list[str] | None = None,
    map_names: dict[str, str | None] | None = None,
    area_orientation: dict[str, dict[str | None, AreaObservations]] | None = None,
    point_clouds: dict[str, list[CloudCell]] | None = None,
):
    """A report from hand-built rows.

    ``map_names`` **is used as it stands when given**, empty included.
    Without it the default is that no demo's header held a map name (``None``
    for every one), so the name is inferred from the id as it was before
    Story 2.11 -- that way the old tests still measure the inference and the
    new ones the observation.

    The default is not filled in on top of a map that was given: a missing key
    is an error as far as ``build_report`` is concerned, and that is exactly
    the guard that has to be testable through this helper.

    ``area_orientation`` works the same way, and its default is an **empty
    orientation for every demo**: no area passes the observation threshold, so
    the anomaly rules stay silent. That is deliberate -- the anomalies are
    tested on rows of their own, and every other test measures what it
    measured before Story 2.5. An empty map is also the right answer: a demo
    whose sample points hold no named areas gives an orientation for no area
    at all.

    ``point_clouds`` works the same way, and its default is an **empty cloud
    for every demo**: no site groups are obtained from it, so stack stays
    silent. The same reasoning as for the orientation -- stack is tested on
    rows of its own, and every other test measures what it measured before
    Story 2.14.
    """
    if map_names is None:
        map_names = {str(row["map_demo_id"]): None for row in classified}
    if area_orientation is None:
        area_orientation = {str(row["map_demo_id"]): {} for row in classified}
    if point_clouds is None:
        point_clouds = {str(row["map_demo_id"]): [] for row in classified}
    return build_report(
        classified=classified_frame(classified),
        ticks=ticks_frame(ticks or []),
        events=events_frame(events or []),
        deaths=deaths_frame(deaths or []),
        rounds=rounds_frame(
            rounds if rounds is not None else rounds_for(classified)
        ),
        team=team_report(lineups),
        thresholds=limits or thresholds(),
        aggregate=windows or aggregate_settings(),
        map_pool=MAP_POOL,
        map_names=map_names,
        area_orientation=area_orientation,
        point_clouds=point_clouds,
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
        missing_demos=missing or [],
    )


def branch(report, map_name: str, side: str, round_type: str):
    """Look up one map/side/round type branch in the report."""
    m = next(m for m in report.maps if m.map_name == map_name)
    s = next(s for s in m.sides if s.side == side)
    return next(rt for rt in s.round_types if rt.round_type == round_type)


# --- Small pure functions -------------------------------------------------------


def test_bucket_labels_name_every_window_including_the_open_one() -> None:
    assert bucket_labels([5.0, 10.0, 20.0]) == ["0-5", "5-10", "10-20", "20+"]


def test_empty_bucket_edges_mean_one_window() -> None:
    """Removing the time window is a setting, not a code change."""
    assert bucket_labels([]) == ["kaikki"]
    assert seconds_bucket(37.0, []) == "kaikki"


@pytest.mark.parametrize(
    "t_s,expected",
    [(0.0, "0-5"), (4.9, "0-5"), (5.0, "5-10"), (19.9, "10-20"), (20.0, "20+")],
)
def test_bucket_edge_belongs_to_the_upper_window(t_s: float, expected: str) -> None:
    """One rule for the edge; two readings are not allowed."""
    assert seconds_bucket(t_s, [5.0, 10.0, 20.0]) == expected


def test_missing_throw_time_gets_its_own_bucket() -> None:
    """A missing time is a different thing from zero seconds."""
    assert seconds_bucket(None, [5.0, 10.0]) == "tuntematon"


@pytest.mark.parametrize(
    "demo,expected",
    [
        ("Ancient_vs_kaljukostaja", ("de_ancient", "map_demo_id")),
        ("Nuke_vs_imuaijat", ("de_nuke", "map_demo_id")),
        ("de_inferno-2026", ("de_inferno", "map_demo_id")),
    ],
)
def test_map_name_is_read_from_the_demo_id(demo: str, expected: tuple) -> None:
    assert map_name_for(demo, MAP_POOL) == expected


def test_unknown_map_keeps_its_identifier_and_says_so() -> None:
    """A FACEIT id holds no map name, and no guess is made."""
    assert map_name_for("1-a52ebff2-1-1", MAP_POOL) == ("1-a52ebff2-1-1", "unknown")


def test_map_name_is_not_matched_as_a_substring() -> None:
    """A team called *Infernal* is not Inferno."""
    name, source = map_name_for("Infernal_vs_x", MAP_POOL)
    assert source == "unknown"
    assert name == "Infernal_vs_x"


# --- The map name from the header (Story 2.11) ----------------------------------


def test_the_observed_map_name_wins_over_the_identifier() -> None:
    """A FACEIT id holds no map, but the header does.

    This very row of the I/O matrix is the reason for the whole story: without
    the header ``1-79f71e00-...`` stays a branch of its own under the name of
    its id.
    """
    assert map_name_for("1-79f71e00-1-1", MAP_POOL, "de_nuke") == (
        "de_nuke",
        "demo_header",
    )


def test_a_hand_imported_demo_keeps_its_name_but_changes_source() -> None:
    """The same name as before, the source changes to the observation."""
    assert map_name_for("Ancient_vs_kaljukostaja", MAP_POOL, "de_ancient") == (
        "de_ancient",
        "demo_header",
    )


def test_an_observed_name_outside_the_pool_is_used_as_is() -> None:
    """A map outside the pool is a genuine observation, not an unknown map.

    ``de_train`` is not in the season's pool, but the demo is from that map.
    Quietly correcting the name to a pool name would be a lie, and dropping
    the source to ``unknown`` would claim that no name was observed.
    """
    assert map_name_for("1-79f71e00-1-1", MAP_POOL, "de_train") == (
        "de_train",
        "demo_header",
    )


def test_the_observation_beats_a_conflicting_identifier() -> None:
    """The id is a filename, the header is the demo's own information.

    In a conflict the observation wins: anybody can write a filename, the
    header was written by the game.
    """
    assert map_name_for("Ancient_vs_x", MAP_POOL, "de_nuke") == (
        "de_nuke",
        "demo_header",
    )


@pytest.mark.parametrize("observed", [None, "", "   "])
def test_without_an_observation_the_inference_still_applies(
    observed: str | None,
) -> None:
    """An empty string is not a name, so the inference stays in force."""
    assert map_name_for("Ancient_vs_kaljukostaja", MAP_POOL, observed) == (
        "de_ancient",
        "map_demo_id",
    )
    assert map_name_for("1-a52ebff2-1-1", MAP_POOL, observed) == (
        "1-a52ebff2-1-1",
        "unknown",
    )


def test_two_faceit_demos_of_the_same_map_form_one_branch() -> None:
    """The epic's own measure: a multi-demo sample for the difference between
    a pattern and a one-off.

    Two different ``map_demo_id``s with the same observed name are **one**
    branch: the rounds add up and ``map_demo_ids`` lists both. Without the
    header these two would be two branches, every row of which would carry the
    marking "(1/1 kierroksesta)".
    """
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 1),
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 2),
        ],
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "ANCIENT_vs_RCAVE_VETERANS": "de_ancient",
        },
    )

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert entry.map_name_source == "demo_header"
    assert sorted(entry.map_demo_ids) == [
        "1-a52ebff2-1-1",
        "ANCIENT_vs_RCAVE_VETERANS",
    ]
    assert (entry.sample.demos, entry.sample.rounds) == (2, 3)


def test_a_demo_without_a_name_does_not_merge_into_another_branch() -> None:
    """An unknown map stays its id; no guess is made.

    An observation from one demo will not do as another one's name, even when
    they are in the same run.
    """
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("1-79f71e00-1-1", 1),
        ],
        map_names={"1-a52ebff2-1-1": "de_ancient", "1-79f71e00-1-1": None},
    )

    branches = {m.map_name: m.map_name_source for m in report.maps}
    assert branches == {
        "de_ancient": "demo_header",
        "1-79f71e00-1-1": "unknown",
    }


# --- The branch key is the name, the source is the weakest (Story 2.11, review 1)


def test_the_observed_and_the_inferred_name_form_one_branch() -> None:
    """The same map from two different sources is **one** branch.

    This is the fault the pair ``(name, source)`` as a key would cause: both
    demos are ``de_ancient``, but one's name comes from the header and the
    other's from the filename. As two keys the report would hold two
    ``de_ancient`` sections, both marked "(1/1 kierroksesta)" -- that is,
    exactly the scatter this story removes, in a new form.

    Before Story 2.11 the fault could not arise: an ``unknown`` branch's name
    is the id itself, so it does not collide with a real name.
    """
    report = report_for(
        [
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 1),
            classified_row("Ancient_vs_kaljukostaja", 1),
            classified_row("Ancient_vs_kaljukostaja", 2),
        ],
        map_names={
            "ANCIENT_vs_RCAVE_VETERANS": "de_ancient",
            "Ancient_vs_kaljukostaja": None,
        },
    )

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert (entry.sample.demos, entry.sample.rounds) == (2, 3)
    assert sorted(entry.map_demo_ids) == [
        "ANCIENT_vs_RCAVE_VETERANS",
        "Ancient_vs_kaljukostaja",
    ]
    # A branch's source is the WEAKEST of its demos: one inferred member is
    # enough.
    assert entry.map_name_source == "map_demo_id"


def test_a_branch_is_demo_header_only_when_every_demo_was_observed() -> None:
    """The other branch: everything observed = ``demo_header``.

    Without this claim the previous test would also pass with an
    implementation that always writes ``map_demo_id``.
    """
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 1),
        ],
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "ANCIENT_vs_RCAVE_VETERANS": "de_ancient",
        },
    )

    assert [m.map_name_source for m in report.maps] == ["demo_header"]


@pytest.mark.parametrize(
    "sources,expected",
    [
        (["demo_header"], "demo_header"),
        (["map_demo_id"], "map_demo_id"),
        (["unknown"], "unknown"),
        (["demo_header", "map_demo_id"], "map_demo_id"),
        (["map_demo_id", "demo_header"], "map_demo_id"),
        (["demo_header", "unknown"], "unknown"),
        (["demo_header", "demo_header"], "demo_header"),
    ],
)
def test_the_branch_source_is_the_weakest_of_its_demos(
    sources: list[str], expected: str
) -> None:
    """The source answers the question "can I trust this name".

    One inferred member is enough to answer "not entirely", and choosing the
    strongest would be overstating it: the branch would look wholly observed
    even though some of its rounds were attached to it on the strength of a
    filename. The order does not affect the result.
    """
    assert weakest_map_source(sources) == expected


def test_an_empty_source_list_is_an_error_not_a_default() -> None:
    """An empty list is a broken grouping, not a default source."""
    with pytest.raises(AggregateError, match="source list is empty"):
        weakest_map_source([])


def test_an_unknown_source_is_refused() -> None:
    """A new source has to be added to the strength order, not just to the
    model.

    A default returned in silence would lie to the reader about how reliable
    the name is.
    """
    with pytest.raises(AggregateError, match="Unknown map name source"):
        weakest_map_source(["demo_header", "tiedostonimi"])


def test_a_missing_map_name_key_is_an_error_but_a_null_value_is_not() -> None:
    """``None`` is a legal observation; a **missing key** is a programming
    error.

    That is exactly what ``build_report``'s ``map_names`` was made mandatory
    to prevent. ``Mapping.get`` would mix the two together and quietly hand
    the map over to inference: a FACEIT demo would get its branch from its id,
    and nothing would say that the observation existed but did not arrive.
    """
    assert observed_map_name({"Nuke_vs_a": None}, "Nuke_vs_a") is None
    assert observed_map_name({"Nuke_vs_a": "de_nuke"}, "Nuke_vs_a") == "de_nuke"

    with pytest.raises(AggregateError, match="is not among the map names"):
        observed_map_name({"Nuke_vs_a": None}, "Anubis_vs_b")


def test_build_report_refuses_a_demo_that_is_not_in_the_name_map() -> None:
    """The same guard run through the whole report."""
    with pytest.raises(AggregateError, match="is not among the map names"):
        report_for([classified_row("Nuke_vs_a", 1)], map_names={})


@pytest.mark.parametrize("padded", ["  de_ancient", "de_ancient  ", " de_ancient "])
def test_a_padded_observed_name_is_trimmed(padded: str) -> None:
    """Edge whitespace must not split a map into two branches.

    The adapter trims the name as it reads it, but ``map_name_for`` is a
    public domain function with a contract of its own and does not lean on the
    caller's tidiness.
    """
    assert map_name_for("1-a52ebff2-1-1", MAP_POOL, padded) == (
        "de_ancient",
        "demo_header",
    )


def test_a_padded_and_a_clean_name_are_the_same_branch() -> None:
    """The consequence of trimming in the whole report: one branch, not two."""
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("1-79f71e00-1-1", 1),
        ],
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "1-79f71e00-1-1": " de_ancient ",
        },
    )

    assert [m.map_name for m in report.maps] == ["de_ancient"]
    assert report.maps[0].sample.demos == 2


def test_lineups_with_enough_shared_players_are_one_team() -> None:
    """One substitution produces a new id; the same team all the same."""
    members = {
        "a": {"1", "2", "3", "4", "5"},
        "b": {"1", "2", "3", "4", "9"},
        "c": {"6", "7", "8", "9", "10"},
    }
    assert lineups_of_same_team("a", members, 3) == ["a", "b"]


def test_lineups_are_not_chained_through_a_third_team() -> None:
    """The comparison is against the target; chaining would melt two teams
    into one."""
    members = {
        "a": {"1", "2", "3", "4", "5"},
        "b": {"3", "4", "5", "6", "7"},
        "c": {"5", "6", "7", "8", "9"},
    }
    assert lineups_of_same_team("a", members, 3) == ["a", "b"]


def test_unknown_lineup_is_an_error_not_an_empty_team() -> None:
    with pytest.raises(AggregateError, match="is not among the lineups"):
        lineups_of_same_team("x", {"a": {"1"}}, 3)


def test_team_slug_survives_an_identifier_that_is_not_a_filename() -> None:
    assert team_slug("Mature Mayhem / 2026") == "mature-mayhem-2026"


def test_the_shared_slug_fallback_is_the_english_word() -> None:
    """The value itself, pinned -- because it reaches a file name.

    Every other claim about the fallback compares against the constant
    (``slug != SLUG_FALLBACK``), so the constant could be changed to anything
    at all and the suite would stay green. It is a **user-visible file name**,
    ``<timestamp>-team.md``, so it is pinned as a literal here and nowhere
    else: changing it has to be a decision that shows in a diff.

    It is English while the report's content is Finnish, and that is the
    boundary rather than an oversight (decided 2026-09-10): a file name is
    not content.
    """
    assert SLUG_FALLBACK == "team"
    assert team_slug("Кибер") == SLUG_FALLBACK


# --- Sampling -------------------------------------------------------------------


def test_empty_is_league_lands_in_unknown_not_other() -> None:
    """A hand-imported demo is neither a league match nor 'other' -- it is
    unknown."""
    rows = [classified_row("Anubis_vs_x", n) for n in (1, 2)]
    assert demo_buckets(rows) == {"Anubis_vs_x": "unknown"}
    s = sample_for(rows, demo_buckets(rows))
    assert (s.unknown.demos, s.unknown.rounds) == (1, 2)
    assert s.other.rounds == 0 and s.league.rounds == 0


def test_a_demo_cannot_belong_to_two_buckets() -> None:
    rows = [
        classified_row("x", 1, is_league=True),
        classified_row("x", 2, is_league=False),
    ]
    with pytest.raises(AggregateError, match="two sample buckets"):
        demo_buckets(rows)


def test_league_and_other_stay_apart_through_the_sample() -> None:
    rows = [
        classified_row("a", 1, is_league=True),
        classified_row("b", 1, is_league=False),
        classified_row("c", 1),
    ]
    s = sample_for(rows, demo_buckets(rows))
    assert (s.league.rounds, s.other.rounds, s.unknown.rounds) == (1, 1, 1)
    assert s.demos == 3


# --- Roster breakdown (Story 3.9) -----------------------------------------------


def test_the_allowed_roster_classes_come_from_the_classified_schema() -> None:
    """The set is read from the schema, not from a parallel constant.

    ``ROSTER_CLASS_BUCKET`` is what turns a class into a bucket name, so a
    class the schema allows but the mapping does not know would raise
    ``KeyError`` instead of counting. Locking the two together here is the
    check that keeps the third bucket from appearing silently.
    """
    assert set(roster_class_values()) == set(ROSTER_CLASS_BUCKET)
    assert len(ROSTER_SAMPLE_BUCKETS) == len(ROSTER_CLASS_BUCKET) + 1


def test_an_empty_roster_class_lands_in_unknown_not_in_a_class() -> None:
    """Every demo in the archive is here: ``select`` gave it no class."""
    rows = [classified_row("Anubis_vs_x", n) for n in (1, 2)]
    assert roster_demo_buckets(rows) == {"Anubis_vs_x": "unknown"}
    s = roster_sample_for(rows, roster_demo_buckets(rows))
    assert (s.unknown.demos, s.unknown.rounds) == (1, 2)
    assert s.full.rounds == 0 and s.partial.rounds == 0


def test_the_two_roster_classes_map_to_their_own_buckets() -> None:
    rows = [
        classified_row("a", 1, roster_class="5/5"),
        classified_row("b", 1, roster_class="4/5"),
        classified_row("c", 1),
    ]
    assert roster_demo_buckets(rows) == {
        "a": "full",
        "b": "partial",
        "c": "unknown",
    }


def test_a_demo_cannot_belong_to_two_roster_buckets() -> None:
    """``roster_class`` describes the map; two values would split one demo."""
    rows = [
        classified_row("x", 1, roster_class="5/5"),
        classified_row("x", 2, roster_class="4/5"),
    ]
    with pytest.raises(AggregateError, match="two roster buckets") as err:
        roster_demo_buckets(rows)
    assert "x" in str(err.value)


def test_a_partly_classified_demo_is_a_different_fault_from_a_contradiction() -> None:
    """Some rounds classified, some not: an interrupted run, not a conflict.

    The fix differs -- reclassify the whole demo rather than resolve two
    claims -- so the message has to differ too. And it must not print
    Python's ``None`` into the sentence: the column is empty, and
    :data:`MISSING_ROSTER_CLASS_LABEL` is the word for that.
    """
    rows = [
        classified_row("x", 1, roster_class="5/5"),
        classified_row("x", 2),
    ]
    with pytest.raises(AggregateError, match="interrupted one") as err:
        roster_demo_buckets(rows)
    message = str(err.value)
    assert MISSING_ROSTER_CLASS_LABEL in message
    assert "None" not in message
    assert "two different roster_class values" not in message


def test_a_table_without_the_roster_column_names_the_stage_to_rerun() -> None:
    """The archive's current state, not a hypothesis (Story 3.9).

    Every classified table written before Story 3.8 has no ``roster_class``
    column at all. A bare ``row[...]`` would surface that as ``KeyError`` --
    an internal error on the command line -- instead of telling the user to
    reclassify.
    """
    rows = [{"map_demo_id": "x", "round_no": 1}]
    with pytest.raises(SchemaError, match="missing the column") as err:
        roster_demo_buckets(rows)
    assert "classify" in str(err.value)


def test_a_demo_outside_the_bucket_map_is_named_not_dropped() -> None:
    """The buckets and the sample must come from the same rows.

    Silently dropping the demo would reach the reader as a roster total
    quietly smaller than the league one; the model would then reject the
    report with a sum that does not add up, several layers from the cause.
    """
    rows = [classified_row("x", 1)]
    with pytest.raises(AggregateError, match="has no roster bucket") as err:
        roster_sample_for(rows, {"y": "unknown"})
    assert "x" in str(err.value)


def test_a_foreign_roster_class_stops_the_run_naming_the_allowed_set() -> None:
    """A corrupt table is not a missing measurement, so it is not ``unknown``."""
    rows = [classified_row("x", 1, roster_class="3/5")]
    with pytest.raises(SchemaError, match="the classified table allows") as err:
        roster_demo_buckets(rows)
    for allowed in roster_class_values():
        assert allowed in str(err.value)


def test_the_roster_classes_stay_apart_through_the_sample() -> None:
    rows = [
        classified_row("a", 1, roster_class="5/5"),
        classified_row("a", 2, roster_class="5/5"),
        classified_row("b", 1, roster_class="4/5"),
        classified_row("c", 1),
        classified_row("d", 1),
    ]
    s = roster_sample_for(rows, roster_demo_buckets(rows))
    assert (s.full.demos, s.full.rounds) == (1, 2)
    assert (s.partial.demos, s.partial.rounds) == (1, 1)
    assert (s.unknown.demos, s.unknown.rounds) == (2, 2)
    assert (s.demos, s.rounds) == (4, 5)


def test_the_two_breakdowns_of_one_sample_agree_on_the_totals() -> None:
    """The invariant ``Report`` enforces, measured at the counting end.

    The two dimensions are independent -- a league map can be played with a
    stand-in -- so the buckets differ while the totals cannot.
    """
    rows = [
        classified_row("a", 1, is_league=True, roster_class="4/5"),
        classified_row("b", 1, is_league=False, roster_class="5/5"),
        classified_row("c", 1),
    ]
    league = sample_for(rows, demo_buckets(rows))
    roster = roster_sample_for(rows, roster_demo_buckets(rows))
    assert (league.demos, league.rounds) == (roster.demos, roster.rounds)
    assert (league.league.demos, roster.full.demos) == (1, 1)


# --- Distributions --------------------------------------------------------------


def test_players_distribution_keeps_the_zero_bucket() -> None:
    dist = players_distribution([0, 0, 3])
    assert [(p.players, p.n) for p in dist] == [(0, 2), (3, 1)]


def test_area_without_players_still_gets_a_row() -> None:
    """I/O matrix: an empty area at a sample point is an observation, not a
    missing row."""
    rows = {
        ("d", 1): [{"area": "BombsiteA"}, {"area": "BombsiteA"}],
        ("d", 2): [{"area": "BombsiteB"}],
    }
    dists = {d.area: d for d in area_distributions(rows)}
    assert [(p.players, p.n) for p in dists["BombsiteA"].players_dist] == [
        (0, 1),
        (2, 1),
    ]
    assert sum(p.n for p in dists["BombsiteB"].players_dist) == 2


def test_dead_players_do_not_count_towards_an_area() -> None:
    ticks = [
        tick_row("d", 1, "p1", "BombsiteA"),
        tick_row("d", 1, "p2", "BombsiteA", is_alive=False),
    ]
    position = positions_for(ticks, [("d", 1)])[0]
    assert [(p.players, p.n) for p in position.areas[0].players_dist] == [(1, 1)]


def test_a_round_where_everyone_died_still_belongs_to_the_sample() -> None:
    """Otherwise the round would vanish from the sample and Σ n = m would
    fail."""
    ticks = [
        tick_row("d", 1, "p1", "BombsiteA"),
        tick_row("d", 2, "p1", "BombsiteA", is_alive=False),
    ]
    position = positions_for(ticks, [("d", 1), ("d", 2)])[0]
    assert position.m == 2
    assert [(p.players, p.n) for p in position.areas[0].players_dist] == [
        (0, 1),
        (1, 1),
    ]


def test_a_sample_point_that_the_round_never_reached_is_reported_missing() -> None:
    """The 45 s sample is missing from a round that was decided in 30
    seconds."""
    ticks = [
        tick_row("d", 1, "p1", "A", sample_t_s=6.0),
        tick_row("d", 2, "p1", "A", sample_t_s=6.0),
        tick_row("d", 1, "p1", "A", sample_t_s=45.0),
    ]
    positions = {p.seconds: p for p in positions_for(ticks, [("d", 1), ("d", 2)])}
    assert (positions[6.0].m, positions[6.0].rounds_missing) == (2, 0)
    assert (positions[45.0].m, positions[45.0].rounds_missing) == (1, 1)


def test_first_contact_is_one_position_not_one_per_round() -> None:
    """The moment differs on every round, so it cannot be grouped by."""
    ticks = [
        tick_row("d", 1, "p1", "A", sample_kind="first_contact", sample_t_s=11.0),
        tick_row("d", 2, "p1", "A", sample_kind="first_contact", sample_t_s=23.0),
    ]
    positions = positions_for(ticks, [("d", 1), ("d", 2)])
    assert len(positions) == 1
    assert positions[0].sample_kind == "first_contact"
    assert positions[0].seconds is None
    assert positions[0].seconds_median == 17.0


def test_first_contact_areas_count_presence_not_players() -> None:
    ticks = [
        tick_row("d", 1, "p1", "Banana", sample_kind="first_contact", sample_t_s=9.0),
        tick_row("d", 1, "p2", "Banana", sample_kind="first_contact", sample_t_s=9.0),
        tick_row("d", 2, "p1", "Banana", sample_kind="first_contact", sample_t_s=9.0),
        tick_row("d", 2, "p2", "Apartments", sample_kind="first_contact", sample_t_s=9.0),
    ]
    areas = {a.area: (a.n, a.m) for a in first_contact_areas(ticks, [("d", 1), ("d", 2)])}
    assert areas == {"Banana": (2, 2), "Apartments": (1, 2)}


# --- Utility --------------------------------------------------------------------


def test_utility_pairs_throw_and_detonation_by_grenade_no() -> None:
    events = event_rows("d", 1, 0, "smoke", throw_area="TSpawn", detonate_area="BombsiteB")
    use = utility_uses(events, [("d", 1)], [5.0, 10.0, 20.0])[0]
    assert (use.grenade_type, use.throw_area, use.detonate_area) == (
        "smoke",
        "TSpawn",
        "BombsiteB",
    )
    assert use.area_source == "point_cloud"
    assert (use.n, use.throws, use.m) == (1, 1, 1)


def test_utility_without_a_detonation_area_is_counted_not_dropped() -> None:
    """I/O matrix: a smoke is thrown where nobody is."""
    events = event_rows("d", 1, 0, "smoke", detonate_area=None)
    use = utility_uses(events, [("d", 1)], [5.0])[0]
    assert use.detonate_area is None
    assert use.area_source is None
    assert use.n == 1


def test_rounds_and_throws_are_counted_separately() -> None:
    """Two identical grenades on one round are one round."""
    events = event_rows("d", 1, 0, "flashbang") + event_rows("d", 1, 1, "flashbang")
    use = utility_uses(events, [("d", 1), ("d", 2)], [5.0])[0]
    assert (use.n, use.throws, use.m) == (1, 2, 2)


def test_utility_counts_answer_how_many_were_thrown_per_round() -> None:
    """The target analysis's line *"2 smokes"* -- not derivable from the
    utility rows."""
    events = (
        event_rows("d", 1, 0, "smoke")
        + event_rows("d", 1, 1, "smoke")
        + event_rows("d", 2, 2, "smoke")
    )
    counts = utility_counts_for(events, [("d", 1), ("d", 2), ("d", 3)])
    assert len(counts) == 1
    assert counts[0].grenade_type == "smoke"
    assert [(c.thrown, c.n) for c in counts[0].counts] == [(0, 1), (1, 1), (2, 1)]
    assert counts[0].m == 3


def test_events_from_other_rounds_do_not_leak_into_the_branch() -> None:
    events = event_rows("d", 9, 0, "he")
    assert utility_uses(events, [("d", 1)], [5.0]) == []
    assert utility_counts_for(events, [("d", 1)]) == []


# --- Armed players --------------------------------------------------------------


def test_armed_players_distribution_keeps_unknown_out_of_the_sample() -> None:
    rows = [
        classified_row("d", 1, armed=5),
        classified_row("d", 2, armed=0),
        classified_row("d", 3, armed=None),
    ]
    armed = armed_players_for(rows)
    assert armed.m == 2
    assert armed.rounds_unknown == 1
    assert [(c.armed, c.n) for c in armed.counts] == [(0, 1), (5, 1)]


# --- Armoured players (Story 2.8) -----------------------------------------------


def test_armored_lookup_reads_the_rounds_table_by_demo_round_and_side() -> None:
    """The key has three parts: the rounds table has two rows per round.

    Without the side the opponent's armour count could end up on our own row
    -- (demo, round) alone hits both.
    """
    lookup = armored_by_round(
        [
            round_row("d", 1, side="T", lineup=TEAM, armored=5),
            round_row("d", 1, side="CT", lineup=OPPONENT, armored=1),
        ]
    )
    assert lookup[("d", 1, "T")] == 5
    assert lookup[("d", 1, "CT")] == 1


def test_armored_lookup_drops_unnumbered_rounds_and_missing_observations() -> None:
    """An unnumbered round and an unreadable armour value are not zeros.

    Both stay out of the map, and a missing key means ``rounds_unknown`` --
    zero would claim that nobody had armour.
    """
    lookup = armored_by_round(
        [
            round_row("d", None, armored=5),
            round_row("d", 2, armored=None),
            round_row("d", 3, armored=0),
        ]
    )
    assert set(lookup) == {("d", 3, "T")}
    assert lookup[("d", 3, "T")] == 0


def test_two_different_armor_readings_for_one_round_are_refused() -> None:
    """A duplicate key is an error and not a silent overwrite.

    ``check_rounds_are_unique`` is run for the classified rows; without the
    equivalent check in the lookup map, whichever row happens to be last would
    stay in force, and nothing would say which figure reached the report.
    """
    with pytest.raises(AggregateError, match="two different armour counts"):
        armored_by_round(
            [
                round_row("d", 1, side="T", armored=5),
                round_row("d", 1, side="T", armored=1),
            ]
        )


def test_an_identical_duplicate_row_is_not_an_error() -> None:
    """The same figure twice is not a contradiction, so it does not stop the
    run.

    Without this pair the previous test would also pass with an
    implementation that rejects every repeated key -- and an identical row
    read twice leaves no doubt about which figure reaches the report.
    """
    lookup = armored_by_round(
        [
            round_row("d", 1, side="T", armored=5),
            round_row("d", 1, side="T", armored=5),
        ]
    )
    assert lookup == {("d", 1, "T"): 5}


def test_a_rounds_row_with_a_missing_key_part_is_dropped() -> None:
    """An incomplete key is dropped **before** the ``str()`` conversion.

    ``str(None)`` would build the key ``"None"``, which never matches anything
    but looks entirely ordinary in the map -- the row would vanish in silence
    and show only as a missing observation in the report.
    """
    lookup = armored_by_round(
        [
            round_row(None, 1, side="T", armored=5),
            round_row("d", 1, side=None, armored=4),
            round_row("d", 2, side="T", armored=3),
        ]
    )
    assert lookup == {("d", 2, "T"): 3}
    assert not any("None" in str(key) for key in lookup)


def test_a_classified_row_with_a_missing_key_part_is_unknown_not_a_crash() -> None:
    """An incomplete key is a missing observation, not a crashing run.

    ``int(None)`` or ``row["side"]`` would raise an exception that took the
    whole aggregation with it -- one round's absence must not cost the whole
    report.
    """
    rows = [
        classified_row("d", 1),
        {**classified_row("d", 2), "side": None},
    ]
    armored = armored_players_for(rows, {("d", 1, "T"): 5})

    assert armored.m == 1
    assert armored.rounds_unknown == 1


def test_armored_players_distribution_keeps_unknown_out_of_the_sample() -> None:
    """The same distinction as for the armed: a missing observation is not
    zero."""
    rows = [classified_row("d", n) for n in (1, 2, 3)]
    lookup = armored_by_round(
        [
            round_row("d", 1, armored=5),
            round_row("d", 2, armored=0),
            round_row("d", 3, armored=None),
        ]
    )
    armored = armored_players_for(rows, lookup)
    assert armored.m == 2
    assert armored.rounds_unknown == 1
    assert [(c.armored, c.n) for c in armored.counts] == [(0, 1), (5, 1)]


def test_a_round_missing_from_the_rounds_table_is_unknown_not_zero() -> None:
    """An old or incomplete rounds table must not look like a round without
    kevlar."""
    armored = armored_players_for([classified_row("d", 1)], {})
    assert armored.m == 0
    assert armored.rounds_unknown == 1
    assert armored.counts == []


def test_the_two_counters_answer_different_questions_on_a_pistol_round() -> None:
    """A pistol round: the armour distribution 5/5, the armed distribution 0.

    This is the target analysis's line *"5 kevlars"* (Nuke, T) as the
    aggregation produces it. If either count were read wrongly from the other
    source, the figures would be the same -- and that is exactly the fault
    this test prevents.
    """
    rows = [classified_row("Nuke_vs_x", 13, armed=0)]
    lookup = armored_by_round([round_row("Nuke_vs_x", 13, armored=5)])

    assert [(c.armed, c.n) for c in armed_players_for(rows).counts] == [(0, 1)]
    assert [
        (c.armored, c.n) for c in armored_players_for(rows, lookup).counts
    ] == [(5, 1)]


def test_the_report_carries_both_counters_for_the_same_round_type() -> None:
    """The whole pipeline: both distributions in the same branch, different
    figures.

    Ancient's CT pistol, the product owner's *"no kevs"*: one kevlar out of
    five and zero armed.
    """
    classified = [classified_row("Ancient_vs_x", 1, side="CT", armed=0)]
    report = report_for(
        classified,
        rounds=[round_row("Ancient_vs_x", 1, side="CT", armored=1, armed=0)],
    )
    entry = branch(report, "de_ancient", "CT", "pistol")

    assert [(c.armed, c.n) for c in entry.players_armed.counts] == [(0, 1)]
    assert [(c.armored, c.n) for c in entry.players_armored.counts] == [(1, 1)]


def test_the_key_picks_our_side_from_the_two_rows_of_one_round() -> None:
    """The rounds table has two rows per round -- the key picks our own.

    The test verifies **the key's third part**, not the lineup filtering: the
    rows are given here unfiltered, as they are in the rounds table, and
    (demo, round) alone would hit both.
    """
    classified = [classified_row("Nuke_vs_x", 13, side="T", armed=0)]
    report = report_for(
        classified,
        rounds=[
            round_row("Nuke_vs_x", 13, side="T", lineup=TEAM, armored=5),
            round_row("Nuke_vs_x", 13, side="CT", lineup=OPPONENT, armored=0),
        ],
    )
    entry = branch(report, "de_nuke", "T", "pistol")
    assert [(c.armored, c.n) for c in entry.players_armored.counts] == [(5, 1)]


# --- The whole report: the I/O matrix -------------------------------------------


def test_one_demo_gives_one_map_and_an_unknown_sample() -> None:
    report = report_for(
        [classified_row("Anubis_vs_x", 1)],
        [tick_row("Anubis_vs_x", 1, "p1", "BombsiteA")],
    )
    assert [m.map_name for m in report.maps] == ["de_anubis"]
    assert report.sample.unknown.demos == 1
    assert report.sample.demos == 1


def test_four_demos_become_four_map_branches_and_the_sample_adds_up() -> None:
    rows = [
        classified_row(demo, 1)
        for demo in (
            "Ancient_vs_a",
            "Anubis_vs_b",
            "inferno_vs_c",
            "Nuke_vs_d",
        )
    ]
    report = report_for(rows)
    assert len(report.maps) == 4
    assert report.sample.rounds == 4
    assert sum(m.sample.rounds for m in report.maps) == report.sample.rounds


def test_the_same_map_twice_merges_into_one_branch() -> None:
    """I/O matrix: the rounds add up, ``demos`` is two."""
    report = report_for(
        [
            classified_row("Nuke_vs_a", 1),
            classified_row("Nuke_vs_b", 1),
            classified_row("Nuke_vs_b", 2),
        ]
    )
    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_nuke"
    assert sorted(entry.map_demo_ids) == ["Nuke_vs_a", "Nuke_vs_b"]
    assert (entry.sample.demos, entry.sample.rounds) == (2, 3)


def test_a_round_type_that_was_never_played_is_absent_not_a_zero_row() -> None:
    report = report_for([classified_row("Nuke_vs_a", 1, round_type="pistol")])
    side = report.maps[0].sides[0]
    assert [rt.round_type for rt in side.round_types] == ["pistol"]


def test_overtime_is_its_own_round_type() -> None:
    report = report_for(
        [
            classified_row("Nuke_vs_a", 1, round_type="pistol"),
            classified_row("Nuke_vs_a", 25, round_type="ot"),
        ]
    )
    side = report.maps[0].sides[0]
    assert [rt.round_type for rt in side.round_types] == ["pistol", "ot"]


def test_full_buys_are_not_filtered_out() -> None:
    """The aggregation does not decide what is reported; it counts
    everything."""
    report = report_for(
        [classified_row("Nuke_vs_a", n, round_type="full") for n in (1, 2, 3, 4)]
    )
    assert branch(report, "de_nuke", "T", "full").sample.rounds == 4


def test_an_unclassified_round_is_counted_but_not_placed() -> None:
    """I/O matrix: not into the structure, but the count is reported."""
    report = report_for(
        [
            classified_row("Nuke_vs_a", 1, round_type="pistol"),
            classified_row("Nuke_vs_a", 2, round_type=None),
        ]
    )
    assert report.unclassified_rounds == 1
    assert report.sample.rounds == 1


def test_a_small_sample_is_marked_but_still_reported() -> None:
    limits = thresholds(small_sample_rounds=3)
    report = report_for(
        [classified_row("Nuke_vs_a", n, round_type="eco") for n in (1, 2)],
        limits=limits,
    )
    assert branch(report, "de_nuke", "T", "eco").small_sample is True


def test_a_sample_at_or_above_the_threshold_is_not_marked_small() -> None:
    limits = thresholds(small_sample_rounds=3)
    report = report_for(
        [classified_row("Nuke_vs_a", n, round_type="eco") for n in (1, 2, 3)],
        limits=limits,
    )
    assert branch(report, "de_nuke", "T", "eco").small_sample is False


def test_the_sample_check_holds_across_every_area_of_a_real_shaped_branch() -> None:
    """Σ n = m over an area, when the rounds are in different areas."""
    classified = [classified_row("Nuke_vs_a", n, round_type="eco") for n in (1, 2, 3)]
    ticks = [
        tick_row("Nuke_vs_a", 1, "p1", "Ramp"),
        tick_row("Nuke_vs_a", 1, "p2", "Ramp"),
        tick_row("Nuke_vs_a", 2, "p1", "Hell"),
        tick_row("Nuke_vs_a", 3, "p1", "Ramp"),
        tick_row("Nuke_vs_a", 3, "p2", "Hell"),
        tick_row("Nuke_vs_a", 3, "p3", "Hell"),
    ]
    position = branch(report_for(classified, ticks), "de_nuke", "T", "eco").positions[0]
    assert position.m == 3
    for area in position.areas:
        assert sum(p.n for p in area.players_dist) == 3
    found = {a.area: [(p.players, p.n) for p in a.players_dist] for a in position.areas}
    assert found["Ramp"] == [(0, 1), (1, 1), (2, 1)]
    assert found["Hell"] == [(0, 1), (1, 1), (2, 1)]


def test_a_lost_round_breaks_the_sample_check_loudly() -> None:
    """Built broken on purpose: an area's sample must not differ from the
    others."""
    from pappascout.domain.report import AreaDistribution, PlayersCount, Position

    with pytest.raises(AggregateError):
        Position(
            sample_kind="time",
            seconds=6.0,
            m=3,
            rounds_missing=0,
            areas=[
                AreaDistribution(
                    area="Ramp", m=3, players_dist=[PlayersCount(players=0, n=3)]
                ),
                AreaDistribution(
                    area="Hell", m=2, players_dist=[PlayersCount(players=0, n=2)]
                ),
            ],
        )


def test_the_opponents_rows_never_reach_the_report() -> None:
    """Lineup filtering is the stage's responsibility, but the join must not
    leak."""
    classified = [classified_row("Nuke_vs_a", 1, round_type="pistol")]
    ticks = [
        tick_row("Nuke_vs_a", 1, "p1", "Ramp"),
        tick_row("Nuke_vs_a", 1, "x1", "Ramp", lineup=OPPONENT, side="CT"),
    ]
    # build_report is given an already filtered table, so this tests the state
    # after the filtering: only our own lineup's rows are counted.
    own = [r for r in ticks if r["lineup_key"] == TEAM]
    position = branch(report_for(classified, own), "de_nuke", "T", "pistol").positions[0]
    assert [(p.players, p.n) for p in position.areas[0].players_dist] == [(1, 1)]


def test_missing_demos_travel_into_the_report_with_their_reason() -> None:
    report = report_for(
        [classified_row("Nuke_vs_a", 1)],
        missing=[MissingDemo(match="Anubis_vs_b", reason="ei parsittu")],
    )
    assert [(m.match, m.reason) for m in report.missing_demos] == [
        ("Anubis_vs_b", "ei parsittu")
    ]


def test_thresholds_are_recorded_for_traceability() -> None:
    report = report_for([classified_row("Nuke_vs_a", 1)])
    assert report.thresholds_used["thresholds"]["small_sample_rounds"] == 3
    assert report.thresholds_used["aggregate"]["utility_seconds_buckets"] == [
        5.0,
        10.0,
        20.0,
    ]


def test_maps_are_ordered_by_how_much_was_played() -> None:
    rows = [classified_row("Nuke_vs_a", n) for n in (1, 2, 3)]
    rows += [classified_row("Anubis_vs_b", 1)]
    report = report_for(rows)
    assert [m.map_name for m in report.maps] == ["de_nuke", "de_anubis"]


def test_both_sides_stay_apart() -> None:
    rows = [
        classified_row("Nuke_vs_a", 1, side="T"),
        classified_row("Nuke_vs_a", 2, side="CT"),
    ]
    report = report_for(rows)
    assert [s.side for s in report.maps[0].sides] == ["T", "CT"]
    assert all(s.sample.rounds == 1 for s in report.maps[0].sides)


def test_the_report_carries_no_interpretation() -> None:
    """No interpretations -- only observations and counts."""
    report = report_for(
        [classified_row("Nuke_vs_a", 1)],
        events=event_rows("Nuke_vs_a", 1, 0, "smoke"),
    )
    # Two fields are **traceability and not observations**, and the word stack
    # appears in both: thresholds_used is a copy of the thresholds (the
    # threshold names stack_min_players, stack_group_margin,
    # stack_site_separation_min) and anomaly_scan names the rules that were
    # run -- it is the coverage's denominator. The check concerns
    # observations, so both are lifted out; below it is verified separately
    # that the word does not vanish from the place where it belongs.
    data = report.model_dump(mode="json")
    thresholds_used = data.pop("thresholds_used")
    scan = data.pop("anomaly_scan")
    assert "stack" in scan["rules"]
    assert "stack_min_players" in thresholds_used["thresholds"]
    text = str(data).lower()
    for word in ("fake", "rush", "stack", "eksekuutio"):
        assert word not in text


# --- The review's findings ------------------------------------------------------


def test_a_single_edge_still_names_both_windows() -> None:
    assert bucket_labels([5.0]) == ["0-5", "5+"]


def test_two_edges_that_look_alike_are_refused() -> None:
    """Two buckets with the same name would make a report row ambiguous."""
    with pytest.raises(AggregateError, match="the same in the bucket name"):
        bucket_labels([5.000000001, 5.000000002])


def test_unknown_time_stays_unknown_even_without_windows() -> None:
    """An empty edge list must not merge an unknown moment into the known
    ones."""
    assert seconds_bucket(None, []) == "tuntematon"
    assert seconds_bucket(float("nan"), []) == "tuntematon"


@pytest.mark.parametrize("t_s", [-1.0, float("nan"), float("inf")])
def test_an_impossible_throw_time_does_not_look_like_a_pattern(t_s: float) -> None:
    """A negative would land as an "insta" and NaN would slide into the last
    bucket."""
    assert seconds_bucket(t_s, [5.0, 10.0, 20.0]) == "tuntematon"


def test_first_contact_median_is_taken_over_rounds_not_player_rows() -> None:
    """Four players alive at 10 s must not pull the median down."""
    ticks = [
        tick_row("d", 1, f"p{i}", "A", sample_kind="first_contact", sample_t_s=10.0)
        for i in range(4)
    ] + [
        tick_row("d", 2, "p1", "A", sample_kind="first_contact", sample_t_s=20.0)
    ]
    position = positions_for(ticks, [("d", 1), ("d", 2)])[0]
    assert position.seconds_median == 15.0


def test_a_time_sample_without_its_second_is_an_error_not_a_crash() -> None:
    row = tick_row("d", 1, "p1", "A")
    row["sample_t_s"] = None
    with pytest.raises(AggregateError, match="sample_t_s"):
        positions_for([row], [("d", 1)])


def test_a_grenade_that_detonates_in_the_next_round_keeps_its_area() -> None:
    """A molotov burns for seven seconds; the pair must not break at the round
    boundary."""
    rows = event_rows("d", 1, 0, "molotov", detonate_area="Banana", t_s=110.0)
    # The detonation lands on the next round's side, as in a real demo.
    rows[1]["round_no"] = 2
    rows[1]["round_raw"] = 3
    use = utility_uses(rows, [("d", 1)], [5.0, 10.0, 20.0])[0]
    assert use.detonate_area == "Banana"
    assert use.area_source == "point_cloud"


def test_a_detonation_without_a_throw_is_counted_not_hidden() -> None:
    rows = event_rows("d", 1, 0, "smoke")
    orphan = [r for r in rows if r["event_kind"] == "grenade_detonate"]
    assert unpaired_detonations(rows) == 0
    assert unpaired_detonations(orphan) == 1
    # And it does not reach the utility: there is neither a throw area nor a
    # moment.
    assert utility_uses(orphan, [("d", 1)], [5.0]) == []


def test_a_duplicated_round_is_refused() -> None:
    """A duplicate would distort both m and rounds_missing."""
    rows = [classified_row("d", 1), classified_row("d", 1)]
    with pytest.raises(AggregateError, match="tables more than once"):
        check_rounds_are_unique(rows)


def test_a_classified_row_without_a_round_number_is_named() -> None:
    """An unguarded int(None) would fail with a TypeError and no
    instructions."""
    row = classified_row("Nuke_vs_a", 1)
    row["round_no"] = None
    with pytest.raises(AggregateError, match="without a round number"):
        report_for([classified_row("Nuke_vs_a", 2), row])


def test_classify_thresholds_are_read_from_the_classified_table() -> None:
    """The observation of which thresholds the rounds were actually
    classified with."""
    found = classify_thresholds([classified_row("d", 1)])
    assert set(found) == set(CLASSIFY_THRESHOLD_KEYS)
    assert found["full_equip_min"] == 4000


def test_rounds_classified_with_different_thresholds_are_refused() -> None:
    """A mixture would produce a figure that does not mean one thing."""
    a = classified_row("d", 1)
    b = classified_row("d", 2)
    b["inputs"] = dict(b["inputs"]) | {"full_equip_min": 3800}
    with pytest.raises(AggregateError, match="with different thresholds"):
        classify_thresholds([a, b])


def test_rounds_classified_with_stale_thresholds_are_refused() -> None:
    """Changing a threshold without reclassifying would name the wrong
    thresholds.

    The user can change ``settings.toml`` and run the aggregation alone. Then
    ``thresholds_used`` would speak of thresholds no round was classified with
    -- and every round type would have been counted by the old rules.
    """
    rows = [classified_row("d", 1)]
    with pytest.raises(AggregateError, match="from what is in the settings"):
        classify_thresholds(rows, thresholds(full_equip_min=4500))
    # With the same values there is no complaint.
    assert classify_thresholds(rows, thresholds())["full_equip_min"] == 4000


def test_the_report_records_the_thresholds_the_rounds_were_classified_with() -> None:
    report = report_for([classified_row("Nuke_vs_a", 1)])
    assert report.classify_thresholds["force_buy_min"] == 1500
    # thresholds_used is THIS run's settings, which is not the same thing.
    assert "thresholds" in report.thresholds_used


def test_utility_rows_are_ordered_by_the_clock_not_the_alphabet() -> None:
    """Alphabetically "10-20" would come before "5-10"."""
    events = (
        event_rows("d", 1, 0, "smoke", t_s=1.0)
        + event_rows("d", 1, 1, "smoke", t_s=7.0)
        + event_rows("d", 1, 2, "smoke", t_s=15.0)
        + event_rows("d", 1, 3, "smoke", t_s=30.0)
    )
    uses = utility_uses(events, [("d", 1)], [5.0, 10.0, 20.0])
    assert [u.seconds_bucket for u in uses] == ["0-5", "5-10", "10-20", "20+"]


# --- The team's and the players' names (Story 2.6) ------------------------------


def test_a_team_without_a_clan_name_has_no_name_at_all() -> None:
    """A missing name is ``None``, not the id and not an empty string."""
    identity = team_identity(
        [
            lineup_row("d", "p1", clan_name=None),
            lineup_row("d", "p2", clan_name=None),
        ]
    )
    assert identity.display_name is None
    assert identity.alternatives == []


def test_an_empty_string_is_not_a_name() -> None:
    """A table written with an older version may hold an empty string."""
    identity = team_identity([lineup_row("d", "p1", clan_name="   ")])
    assert identity.display_name is None


def test_the_same_clan_in_every_demo_is_the_teams_name() -> None:
    identity = team_identity(
        [
            lineup_row(demo, f"p{i}", clan_name="MatureMayhem")
            for demo in ("a", "b", "c", "d")
            for i in range(5)
        ]
    )
    assert identity.display_name == "MatureMayhem"
    assert identity.alternatives == []


def test_conflicting_names_keep_the_most_observed_and_list_the_rest() -> None:
    """A contradiction does not vanish: the most often observed one is shown,
    the rest are listed."""
    rows = [
        lineup_row(demo, f"p{i}", clan_name="MatureMayhem")
        for demo in ("a", "b", "c")
        for i in range(5)
    ] + [lineup_row("d", f"p{i}", clan_name="MM Academy") for i in range(5)]

    identity = team_identity(rows)
    assert identity.display_name == "MatureMayhem"
    assert identity.alternatives == ["MM Academy"]


def test_the_vote_is_per_demo_not_per_row() -> None:
    """A five-player demo must not vote five times.

    The contradiction arises because two *demos* give a different name. A
    row-based count would give fivefold weight to the demo that happened to
    hold five players.
    """
    rows = [lineup_row("a", f"p{i}", clan_name="Aakkoset") for i in range(5)] + [
        lineup_row("b", "p9", clan_name="Bee"),
        lineup_row("c", "p9", clan_name="Bee"),
    ]
    identity = team_identity(rows)
    assert identity.display_name == "Bee"
    assert identity.alternatives == ["Aakkoset"]


def test_one_player_cannot_outvote_his_own_team_inside_a_demo() -> None:
    """Inside a demo the **majority** decides, not "one vote per observed
    name".

    Four players carry the clan ``Zulu``, one the clan ``Alfa``. A vote per
    observed name would give both of them one; on a one-demo sample that is a
    tie, and alphabetical order would raise into the heading a name that one
    single player carried -- and no trace of it would be left in the report.

    Nor is the minority a contradiction *between demos*, so it does not belong
    among the alternative names: it is an observation inside the demo, and the
    parse's ``lineup_clan_conflicts`` reports it.
    """
    rows = [
        lineup_row("d", f"p{i}", clan_name="Zulu" if i < 4 else "Alfa")
        for i in range(5)
    ]
    identity = team_identity(rows)
    assert identity.display_name == "Zulu"
    assert identity.alternatives == []


def test_a_demos_majority_is_one_vote_no_matter_how_many_players_carry_it() -> None:
    """Two demos, two votes -- even though one of them holds five players."""
    rows = [
        lineup_row("iso", f"p{i}", clan_name="Zulu") for i in range(5)
    ] + [lineup_row("pieni", "p9", clan_name="Alfa")]

    identity = team_identity(rows)
    # A tie across the demos -> the alphabet.
    assert identity.display_name == "Alfa"
    assert identity.alternatives == ["Zulu"]


def test_a_row_without_a_map_demo_id_is_refused() -> None:
    """A row without an id would merge all the demos into one vote."""
    with pytest.raises(AggregateError, match="map_demo_id"):
        team_identity([lineup_row("", "p1", clan_name="Zulu")])


def test_a_tie_is_resolved_alphabetically_so_the_run_repeats() -> None:
    """Without alphabetical order the result would depend on the order the
    files were read in."""
    rows = [
        lineup_row("b", "p1", clan_name="Zulu"),
        lineup_row("a", "p1", clan_name="Alfa"),
    ]
    assert team_identity(rows).display_name == "Alfa"
    assert team_identity(list(reversed(rows))).display_name == "Alfa"


def test_the_player_name_is_the_most_observed_one() -> None:
    rows = [
        lineup_row("a", "p1", player_name="Laetikko"),
        lineup_row("b", "p1", player_name="Laetikko"),
        lineup_row("c", "p1", player_name="tertseli"),
    ]
    assert team_identity(rows).names["p1"] == "Laetikko"


def test_the_roster_keeps_a_player_whose_name_was_never_read() -> None:
    """A player dropped in silence would shrink the roster without saying
    so."""
    entries = roster_entries(["p2", "p1"], {"p1": "Sassiz"})
    assert [e.player_id for e in entries] == ["p1", "p2"]
    assert entries[0].display_name == "Sassiz"
    assert entries[1].display_name is None


# --- Deaths and kills (Story 2.7) -----------------------------------------------


KEYS = [("Ancient_vs_x", 1), ("Ancient_vs_x", 2)]


def test_the_first_death_of_a_round_is_the_earliest_one() -> None:
    """The first death is the row with the smallest ``t_s``, not the table's
    first row.

    The rows are given in the wrong order on purpose: if the function took the
    first match, it would pick Long.
    """
    report = deaths_for(
        [
            death_row("Ancient_vs_x", 1, victim="p2", victim_area="Long", t_s=40.0),
            death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=12.0),
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("Cave", 1)]
    assert report.first_death_seconds_median == 12.0


def test_the_first_death_distribution_sums_to_its_own_sample() -> None:
    """``Σ n = m``, and ``m`` is the rounds on which the team lost a
    player."""
    report = deaths_for(
        [
            death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave"),
            death_row("Ancient_vs_x", 2, victim="p2", victim_area="Long"),
        ],
        KEYS,
        [TEAM],
    )
    assert report.m == 2
    assert sum(a.n for a in report.first_death_areas) == report.m
    assert {a.m for a in report.first_death_areas} == {2}


def test_rounds_without_an_own_death_are_counted_apart() -> None:
    """A round without an own death is not a zero row but ``rounds_missing``.

    A zero row would claim as an observation that there is no observation --
    and it would break ``Σ n = m``.
    """
    report = deaths_for(
        [death_row("Ancient_vs_x", 1, victim="p1")], KEYS, [TEAM]
    )
    assert report.m == 1
    assert report.rounds_missing == 1


def test_the_median_is_measured_from_the_rounds_not_the_rows() -> None:
    """The median is computed from the rounds' first deaths.

    The other deaths of the same round must not weigh: five players fallen on
    one round would shift the median to the far end.
    """
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1", t_s=10.0),
        death_row("Ancient_vs_x", 1, victim="p2", t_s=60.0),
        death_row("Ancient_vs_x", 1, victim="p3", t_s=61.0),
        death_row("Ancient_vs_x", 2, victim="p1", t_s=20.0),
    ]
    report = deaths_for(rows, KEYS, [TEAM])
    assert report.first_death_seconds_median == 15.0


def test_a_tie_on_the_same_tick_is_broken_by_the_victim_id() -> None:
    """Two teammates at the same moment: the choice must not depend on the row
    order."""
    rows = [
        death_row("Ancient_vs_x", 1, victim="p9", victim_area="Long", t_s=12.0),
        death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=12.0),
    ]
    forward = deaths_for(rows, KEYS[:1], [TEAM])
    backward = deaths_for(list(reversed(rows)), KEYS[:1], [TEAM])
    assert [a.area for a in forward.first_death_areas] == ["Cave"]
    assert forward == backward


def test_a_death_without_a_time_is_last_not_first() -> None:
    """A missing time is not zero.

    Without the distinction an empty ``t_s`` would order before every measured
    one and would claim to be the round's first death.
    """
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=30.0),
        death_row("Ancient_vs_x", 1, victim="p2", victim_area="Long", t_s=None),
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert [a.area for a in report.first_death_areas] == ["Cave"]
    assert report.first_death_seconds_median == 30.0


def test_a_round_whose_only_death_has_no_time_still_counts() -> None:
    """The time is missing, the observation is not: the area is still the
    round's first."""
    report = deaths_for(
        [death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=None)],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [a.area for a in report.first_death_areas] == ["Cave"]
    assert report.first_death_seconds_median is None


def test_kills_are_counted_from_the_attackers_lineup_and_area() -> None:
    """A kill is the **shooter's** observation: the area is where he shot
    from."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="o1",
            victim_lineup=OPPONENT,
            victim_side="CT",
            victim_area="BombsiteA",
            attacker="p1",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle",
        ),
        death_row(
            "Ancient_vs_x",
            2,
            victim="o2",
            victim_lineup=OPPONENT,
            victim_side="CT",
            victim_area="BombsiteA",
            attacker="p2",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle",
        ),
    ]
    report = deaths_for(rows, KEYS, [TEAM])
    assert report.kills_total == 2
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 2)]
    # There were no own deaths: these are the opponent's deaths.
    assert report.m == 0
    assert report.rounds_missing == 2


def test_the_kill_sample_counts_kills_not_rounds() -> None:
    """There can be more kills than rounds -- ``Σ n = kills_total``."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim=f"o{i}",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker=f"p{i}",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle" if i < 3 else "BombsiteB",
        )
        for i in range(4)
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.kills_total == 4
    assert sum(k.n for k in report.kills) == 4
    assert {k.m for k in report.kills} == {4}
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 3), ("BombsiteB", 1)]


def test_a_kill_without_an_area_gets_its_own_bucket() -> None:
    """An unknown area does not drop out: it is a different thing from having
    no kills."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="o1",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker="p1",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area=None,
        )
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert [(k.area, k.n) for k in report.kills] == [(None, 1)]


def test_a_teamkill_is_both_an_own_death_and_an_own_kill() -> None:
    """Filtering out either one would be interpretation.

    The observation is that a player died and that the shooter was in a
    particular area.
    """
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="p1",
            victim_lineup=TEAM,
            victim_area="Cave",
            attacker="p2",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle",
        )
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("Cave", 1)]
    assert report.kills_total == 1
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 1)]


def test_a_death_between_two_opponents_is_neither() -> None:
    """A death between two opponents does not belong in this report."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="o1",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker="o2",
            attacker_lineup=OPPONENT,
            attacker_side="CT",
        )
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.m == 0
    assert report.kills_total == 0


def test_a_death_outside_the_branch_is_ignored() -> None:
    """A round of another round type must not leak into this sample."""
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1"),
        death_row("Ancient_vs_x", 9, victim="p1"),
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.m == 1


def test_an_unnumbered_death_row_is_ignored() -> None:
    """An empty ``round_no`` means a round that was not played."""
    row = death_row("Ancient_vs_x", 1, victim="p1")
    row["round_no"] = None
    report = deaths_for([row], KEYS, [TEAM])
    assert report.m == 0


def test_several_lineups_of_the_same_team_all_count_as_ours() -> None:
    """One substitution produces a new lineup id; both of them are us."""
    other = "cccccccccccccccc"
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1", victim_lineup=TEAM),
        death_row("Ancient_vs_x", 2, victim="p1", victim_lineup=other),
    ]
    report = deaths_for(rows, KEYS, [TEAM, other])
    assert report.m == 2


def test_the_death_report_reaches_the_round_type_branch() -> None:
    """The marginal distribution is in the structure where the report reads
    it."""
    report = report_for(
        [classified_row("Ancient_vs_x", 1)],
        [tick_row("Ancient_vs_x", 1, "p1", "BombsiteA")],
        deaths=[
            death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=24.0),
            death_row(
                "Ancient_vs_x",
                1,
                victim="o1",
                victim_lineup=OPPONENT,
                victim_side="CT",
                attacker="p2",
                attacker_lineup=TEAM,
                attacker_side="T",
                attacker_area="Middle",
                t_s=30.0,
            ),
        ],
    )
    entry = branch(report, "de_ancient", "T", "pistol")
    assert entry.deaths.m == 1
    assert entry.deaths.first_death_seconds_median == 24.0
    assert [(a.area, a.n) for a in entry.deaths.first_death_areas] == [("Cave", 1)]
    assert [(k.area, k.n) for k in entry.deaths.kills] == [("Middle", 1)]


def test_an_attackerless_own_death_is_counted_as_a_death_and_nothing_else() -> None:
    """An own player killed by the bomb or by a fall is an own death.

    Parse treats a death without a shooter as a first-class case, but as far
    as the aggregation it went through no test at all -- and there
    ``attacker_lineup_key`` is ``null``, which is a different thing from "not
    ours". Without this case own players killed by the bomb could drop out of
    the report without anything failing.
    """
    report = deaths_for(
        [
            death_row(
                "Ancient_vs_x",
                1,
                victim="p1",
                victim_area="BombsiteB",
                attacker=None,
                t_s=95.0,
            )
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("BombsiteB", 1)]
    # There is no shooter, so there is no kill -- and a null lineup must not
    # match the own-kills filter.
    assert report.kills_total == 0
    assert report.kills == []


def test_a_suicide_is_a_death_but_not_a_kill() -> None:
    """A suicide's area is a place nobody shot from.

    The row would pass both branches, because the shooter is in our own
    lineup. Counted towards the kills it would raise ``kills_total`` and would
    add to the "where they shoot from" row a location that does not exist. The
    data holds 0 suicides out of 591 deaths, so the fault would be latent.
    """
    report = deaths_for(
        [
            death_row(
                "Ancient_vs_x",
                1,
                victim="p1",
                victim_area="Cave",
                attacker="p1",
                attacker_lineup=TEAM,
                attacker_side="T",
                attacker_area="Cave",
            )
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("Cave", 1)]
    assert report.kills_total == 0


def test_a_teamkill_is_still_a_kill_beside_the_suicide_rule() -> None:
    """The guard's other branch: a teammate **really did shoot** from that
    area.

    Without this the suicide rule could have been written by lineup rather
    than by player, and the teamkill would vanish from the kills with it.
    """
    report = deaths_for(
        [
            death_row(
                "Ancient_vs_x",
                1,
                victim="p1",
                attacker="p2",
                attacker_lineup=TEAM,
                attacker_side="T",
                attacker_area="Middle",
            )
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.kills_total == 1
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 1)]


def test_an_empty_area_string_is_the_same_observation_as_a_missing_one() -> None:
    """An empty string is not an area.

    Without normalisation the same observation would reach the distribution
    twice: the model's duplicate check compares raw values (``""`` and
    ``None`` are different), but the report shows both under the name
    "tuntematon alue" -- that is, one row would say the same thing twice with
    different figures.
    """
    report = deaths_for(
        [
            death_row("Ancient_vs_x", 1, victim="p1", victim_area=""),
            death_row("Ancient_vs_x", 2, victim="p2", victim_area=None),
        ],
        KEYS,
        [TEAM],
    )
    assert [(a.area, a.n) for a in report.first_death_areas] == [(None, 2)]


def test_an_empty_kill_area_string_collapses_too() -> None:
    """The same rule on the kill side; a different line in the code, a
    different test here."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim=f"o{i}",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker=f"p{i}",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area=area,
        )
        for i, area in enumerate(("", None))
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert [(k.area, k.n) for k in report.kills] == [(None, 2)]


# --- Anomalies (Story 2.5) ------------------------------------------------------
#
# Grouping into a sample is the aggregation's job and not the rule's: a rule
# sees one round at a time. The tests therefore build rounds and check the
# ``n/m``, the map, the side and the round types -- not when a rule hits (that
# is ``test_sampling.py``).
#
# **The thresholds always travel through the ``limits=`` parameter.** That is
# not a matter of style: without it the tests would prove only that a rule
# hits on the default values, and not one claim would fail if the call site
# wired the thresholds wrongly or left them unread. That is exactly Story
# 1.8's fault -- an adjusted settings.toml changes the parameter hash but not
# the report.

#: An area the T side holds in the demo: Ancient's B long, T share 0.88
#: (n 24).
ANOMALY_AREA = "TSideLower"


def t_side(demo: str, *, area: str = ANOMALY_AREA, t: int = 21, total: int = 24):
    """The orientation map for one demo: one area held by the T side."""
    return {demo: {area: AreaObservations(t=t, total=total)}}


def advance_round(
    demo: str,
    round_no: int,
    *,
    area: str = ANOMALY_AREA,
    players: int = 1,
    seconds: float = 30.0,
) -> list[dict[str, object]]:
    """The sample point rows that trigger a CT advance on one round."""
    return [
        tick_row(
            demo,
            round_no,
            f"{TEAM}-p{i}",
            area,
            side="CT",
            sample_t_s=seconds,
        )
        for i in range(players)
    ]


def crunch_round(
    demo: str,
    round_no: int,
    *,
    area: str = ANOMALY_AREA,
    sources: tuple[str, ...] = ("SideEntrance", "TSideUpper"),
    seconds: float = 30.0,
) -> list[dict[str, object]]:
    """The players arrive in the area from the given source directions -- one
    per direction."""
    rows: list[dict[str, object]] = []
    for i, source in enumerate(sources):
        rows.append(
            tick_row(
                demo,
                round_no,
                f"{TEAM}-p{i}",
                source,
                side="CT",
                sample_t_s=seconds - 15.0,
            )
        )
        rows.append(
            tick_row(
                demo, round_no, f"{TEAM}-p{i}", area, side="CT", sample_t_s=seconds
            )
        )
    return rows


def eco_ct(demo: str, *rounds: int, round_type: str = "eco"):
    """Classified CT rounds of the given type."""
    return [
        classified_row(demo, n, side="CT", round_type=round_type) for n in rounds
    ]


def stack_cloud(demo: str) -> dict[str, list[CloudCell]]:
    """A per-demo point cloud that yields site groups."""
    return {demo: [CloudCell(a, x, y, z) for a, x, y, z in SITE_CLOUD]}


def stack_round(
    demo: str,
    round_no: int,
    *,
    site: str = "BombsiteB",
    others: tuple[str, ...] = ("SideEntrance", "Ramp"),
    elsewhere: tuple[str, ...] = ("BombsiteA",),
    seconds: float = 15.0,
) -> list[dict[str, object]]:
    """Four CT players in the same site's group, one on the site itself.

    ``elsewhere`` is outside the group: it raises the number of living players
    to five without raising the group's size -- that is, exactly the
    difference ``4/5`` measures.
    """
    areas = (site, site, *others, *elsewhere)
    return [
        tick_row(
            demo,
            round_no,
            f"{TEAM}-p{i}",
            area,
            side="CT",
            sample_t_s=seconds,
        )
        for i, area in enumerate(areas)
    ]


def anomaly_report(
    classified: list[dict[str, object]],
    ticks: list[dict[str, object]],
    *,
    demo: str,
    limits: ThresholdSettings,
    orientation: dict | None = None,
    map_names: dict[str, str | None] | None = None,
    point_clouds: dict[str, list[CloudCell]] | None = None,
):
    """A report for an anomaly test -- the thresholds **always** named.

    ``point_clouds`` defaults to an empty cloud: no site groups are obtained,
    so stack stays silent and the tests about the advance and crunch do not
    measure the third rule by accident.
    """
    return report_for(
        classified,
        ticks,
        limits=limits,
        area_orientation=orientation if orientation is not None else t_side(demo),
        map_names=map_names,
        point_clouds=point_clouds,
    )


def areas_of(report, rule: str) -> list[str]:
    """The anomaly areas of the given rule, from the report."""
    return [a.area for a in report.anomalies if a.rule == rule]


def test_an_anomaly_carries_its_map_side_round_type_and_sample() -> None:
    """Every anomaly carries its sample and the map, the side and the type."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert anomaly.rule == "ct_advance"
    assert anomaly.map_name == "de_ancient"
    assert anomaly.map_name_source == "map_demo_id"
    assert anomaly.side == "CT"
    assert anomaly.round_types == ["eco"]
    assert anomaly.area == ANOMALY_AREA
    assert (anomaly.n, anomaly.m) == (1, 3)
    assert [entry.round_no for entry in anomaly.rounds] == [1]
    assert anomaly.rounds[0].seconds == [30.0]
    assert anomaly.rounds[0].map_demo_id == demo
    assert anomaly.orientation[0].map_demo_id == demo
    assert anomaly.orientation[0].t_share == pytest.approx(0.875)
    assert anomaly.orientation[0].observations == 24


def test_the_round_numbers_are_carried_not_thrown_away() -> None:
    """The scout's next act is to open that round on the demo."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 2) + advance_round(demo, 3),
        demo=demo,
        limits=thresholds(),
    )
    anomaly = report.anomalies[0]
    assert [entry.round_no for entry in anomaly.rounds] == [2, 3]


def test_the_same_area_on_two_rounds_is_one_row_with_a_sample_of_two() -> None:
    """The I/O matrix's last row: one row with a sample of 2/m, not two
    rows."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 1) + advance_round(demo, 2, players=2),
        demo=demo,
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert (anomaly.n, anomaly.m) == (2, 3)
    assert anomaly.players_max == 2
    assert [entry.players_max for entry in anomaly.rounds] == [1, 2]


def test_two_sample_points_on_one_round_do_not_double_the_sample() -> None:
    """``n`` is rounds: the same area at 15 s and at 30 s is one round."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2),
        advance_round(demo, 1, seconds=15.0) + advance_round(demo, 1, seconds=30.0),
        demo=demo,
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    assert report.anomalies[0].n == 1
    assert report.anomalies[0].rounds[0].seconds == [15.0, 30.0]


def test_a_crunch_round_produces_both_rows() -> None:
    """The rules share the orientation; on a saving round both of them hit."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        crunch_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    rules = [a.rule for a in report.anomalies]
    assert rules == ["ct_advance", "crunch"]
    crunch = report.anomalies[1]
    assert crunch.rounds[0].sources == ["SideEntrance", "TSideUpper"]
    assert crunch.players_max == 2


def test_an_empty_anomaly_list_is_a_valid_report() -> None:
    """The matrix's row 7: not one rule hits."""
    demo = "Nuke_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1, area="Outside"),
        demo=demo,
        # Nuke's outside yard: a T share of 0.70, that is below the
        # threshold.
        orientation={demo: {"Outside": AreaObservations(t=211, total=302)}},
        limits=thresholds(),
    )
    assert report.anomalies == []


def test_a_full_buy_round_gets_no_advance_but_can_get_a_crunch() -> None:
    """The matrix's row 8: the advance cannot hit, crunch can."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3, round_type="full"),
        crunch_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    assert [a.rule for a in report.anomalies] == ["crunch"]


# --- Spec change 2: crunch is not keyed by round type ---------------------------


def test_a_crunch_on_two_round_types_is_one_row_over_the_whole_side() -> None:
    """The same pattern on two round types is one row, not two.

    Without this the same crunch would break into an eco row and a default row
    with different denominators, and the total could not be seen -- exactly
    the scatter the whole chapter was made to remove.
    """
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2) + eco_ct(demo, 3, 4, round_type="full")
    report = anomaly_report(
        classified,
        crunch_round(demo, 1) + crunch_round(demo, 3),
        demo=demo,
        limits=thresholds(),
    )
    crunches = [a for a in report.anomalies if a.rule == "crunch"]
    assert len(crunches) == 1
    crunch = crunches[0]
    # The denominator is ALL of the side's rounds, not one round type's.
    assert (crunch.n, crunch.m) == (2, 4)
    assert crunch.round_types == ["eco", "full"]
    assert [entry.round_type for entry in crunch.rounds] == ["eco", "full"]


def test_the_advance_is_still_keyed_by_round_type() -> None:
    """The advance is a phenomenon of the saving rounds, so the type is part
    of the observation."""
    demo = "Ancient_vs_x"
    classified = (
        eco_ct(demo, 1, 2)
        + eco_ct(demo, 3, 4, round_type="force")
    )
    report = anomaly_report(
        classified,
        advance_round(demo, 1) + advance_round(demo, 3),
        demo=demo,
        limits=thresholds(),
    )
    advances = [a for a in report.anomalies if a.rule == "ct_advance"]
    assert [(a.round_types, a.n, a.m) for a in advances] == [
        (["eco"], 1, 2),
        (["force"], 1, 2),
    ]


def test_an_advance_row_never_carries_two_round_types() -> None:
    """The model enforces it, but the grouping has to produce it correctly."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1) + eco_ct(demo, 2, round_type="force")
    report = anomaly_report(
        classified,
        advance_round(demo, 1) + advance_round(demo, 2),
        demo=demo,
        limits=thresholds(),
    )
    for anomaly in report.anomalies:
        if anomaly.rule == "ct_advance":
            assert len(anomaly.round_types) == 1


def test_the_crunch_denominator_is_the_side_not_the_round_type() -> None:
    """The total is visible: 1/24 and not 1/2 plus 0/22."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2) + eco_ct(
        demo, *range(3, 25), round_type="full"
    )
    report = anomaly_report(
        classified,
        crunch_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    crunch = next(a for a in report.anomalies if a.rule == "crunch")
    assert (crunch.n, crunch.m) == (1, 24)


# --- The thresholds affect the report's content ---------------------------------
#
# Each of the six thresholds is proved **in both directions**: a value at
# which the row is there, and a value at which it disappears. The mutation
# these stop: wiring a threshold wrongly, or leaving it unread.


def test_the_t_share_threshold_decides_whether_the_row_exists() -> None:
    """Nuke's outside yard (0.70) is the borderline case the whole threshold
    is calibrated from."""
    demo = "Nuke_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, area="Outside")
    # Nuke's outside yard is **exactly** on the threshold: the whole
    # justification for 0.80 rests on 0.70 letting it through. The figures are
    # therefore exactly 0.70 and not the measured 211/302 (= 0.6987), which
    # rounds to 0.70 but does not reach it -- that very difference is the
    # reason to write the comparison out.
    orientation = {demo: {"Outside": AreaObservations(t=210, total=300)}}
    strict = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_t_share=0.80),
    )
    loose = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_t_share=0.70),
    )
    assert areas_of(strict, "ct_advance") == []
    assert areas_of(loose, "ct_advance") == ["Outside"]


def test_the_observation_minimum_decides_whether_the_row_exists() -> None:
    """A thin area is neither side's area -- the threshold decides."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, area="Ramp")
    orientation = {demo: {"Ramp": AreaObservations(t=5, total=6)}}
    strict = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_area_min_observations=20),
    )
    loose = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_area_min_observations=6),
    )
    assert areas_of(strict, "ct_advance") == []
    assert areas_of(loose, "ct_advance") == ["Ramp"]


def test_the_time_bound_decides_whether_the_row_exists() -> None:
    """A hit at 45 s is outside the bound, one at 30 s inside it."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, seconds=45.0)
    strict = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_max_sample_s=30.0)
    )
    loose = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_max_sample_s=45.0)
    )
    assert areas_of(strict, "ct_advance") == []
    assert areas_of(loose, "ct_advance") == [ANOMALY_AREA]


def test_the_advance_player_minimum_decides_whether_the_row_exists() -> None:
    """The product owner chose 1; requiring two would leave four hits out of
    six behind.
    """
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, players=1)
    one = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_min_players=1)
    )
    two = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_min_players=2)
    )
    assert areas_of(one, "ct_advance") == [ANOMALY_AREA]
    assert areas_of(two, "ct_advance") == []


def test_the_crunch_player_minimum_decides_whether_the_row_exists() -> None:
    """Requiring three players drops a two-player crunch."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = crunch_round(demo, 1)
    two = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(crunch_min_players=2)
    )
    three = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(crunch_min_players=3)
    )
    assert areas_of(two, "crunch") == [ANOMALY_AREA]
    assert areas_of(three, "crunch") == []


def test_the_crunch_source_minimum_decides_whether_the_row_exists() -> None:
    """Requiring three source directions drops a two-direction crunch."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = crunch_round(demo, 1)
    two = anomaly_report(
        classified,
        ticks,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=2),
    )
    three = anomaly_report(
        classified,
        ticks,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=3),
    )
    # Two players from two directions: crunch_min_players=3 drops it under
    # either direction requirement, so the direction requirement is proved
    # with three-player data below.
    assert areas_of(two, "crunch") == []
    assert areas_of(three, "crunch") == []

    ticks3 = crunch_round(
        demo, 1, sources=("Alley", "BombsiteB", "LowerTunnel")
    )
    ok = anomaly_report(
        classified,
        ticks3,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=3),
    )
    tight = anomaly_report(
        classified,
        ticks3,
        demo=demo,
        limits=thresholds(crunch_min_players=4, crunch_min_sources=4),
    )
    assert areas_of(ok, "crunch") == [ANOMALY_AREA]
    assert areas_of(tight, "crunch") == []


def test_the_two_crunch_thresholds_are_not_interchangeable() -> None:
    """**Mutation guard: swapping the thresholds over fails this test.**

    The data is three players from two source directions, and the thresholds
    are ``players=3, sources=2``. Wired correctly it hits. If the call site
    swaps the thresholds over, the condition becomes ``players>=2,
    sources>=3`` -- and two directions do not reach three, so the row
    disappears.

    On the default values (2 and 2) the swap does not show at all, and that is
    exactly why this test uses different values.
    """
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    # Three players, two source directions: p0 and p1 from the same direction.
    ticks = [
        tick_row(demo, 1, f"{TEAM}-p0", "SideEntrance", side="CT", sample_t_s=15.0),
        tick_row(demo, 1, f"{TEAM}-p1", "SideEntrance", side="CT", sample_t_s=15.0),
        tick_row(demo, 1, f"{TEAM}-p2", "TSideUpper", side="CT", sample_t_s=15.0),
    ] + [
        tick_row(demo, 1, f"{TEAM}-p{i}", ANOMALY_AREA, side="CT", sample_t_s=30.0)
        for i in range(3)
    ]
    report = anomaly_report(
        classified,
        ticks,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=2),
    )
    crunch = next(a for a in report.anomalies if a.rule == "crunch")
    assert crunch.players_max == 3
    assert crunch.rounds[0].sources == ["SideEntrance", "TSideUpper"]


def test_the_small_sample_threshold_decides_the_mark_both_ways() -> None:
    """The sibling flag's rule: both directions, not just ``True``.

    The mutation ``m <`` -> ``m <=`` would mark every three-round branch's
    anomaly as a small sample, and a one-directional claim would not notice
    it.
    """
    demo = "Ancient_vs_x"
    small = anomaly_report(
        eco_ct(demo, 1, 2),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(small_sample_rounds=3),
    )
    big = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(small_sample_rounds=3),
    )
    assert small.anomalies[0].small_sample is True
    assert small.anomalies[0].m == 2
    assert big.anomalies[0].small_sample is False
    assert big.anomalies[0].m == 3


# --- Grouping and coverage ------------------------------------------------------


def test_two_demos_of_the_same_map_keep_both_orientations() -> None:
    """A map can come from two demos, and their T shares can differ."""
    first, second = "Ancient_vs_x", "Ancient_vs_y"
    report = anomaly_report(
        eco_ct(first, 1) + eco_ct(second, 1),
        advance_round(first, 1) + advance_round(second, 1),
        demo=first,
        orientation={
            first: {ANOMALY_AREA: AreaObservations(t=21, total=24)},
            second: {ANOMALY_AREA: AreaObservations(t=40, total=46)},
        },
        map_names={first: "de_ancient", second: "de_ancient"},
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert (anomaly.n, anomaly.m) == (2, 2)
    assert [entry.map_demo_id for entry in anomaly.orientation] == [first, second]
    assert [entry.observations for entry in anomaly.orientation] == [24, 46]
    assert [entry.map_demo_id for entry in anomaly.rounds] == [first, second]


def test_anomalies_from_two_maps_stay_apart() -> None:
    """An anomaly is a map's observation; two maps are two rows."""
    ancient, anubis = "Ancient_vs_x", "Anubis_vs_x"
    report = anomaly_report(
        eco_ct(ancient, 1) + eco_ct(anubis, 1),
        advance_round(ancient, 1) + advance_round(anubis, 1, area="Bridge"),
        demo=ancient,
        orientation={
            ancient: {ANOMALY_AREA: AreaObservations(t=21, total=24)},
            anubis: {"Bridge": AreaObservations(t=39, total=46)},
        },
        limits=thresholds(),
    )
    assert [(a.map_name, a.area) for a in report.anomalies] == [
        ("de_ancient", ANOMALY_AREA),
        ("de_anubis", "Bridge"),
    ]


def test_an_anomaly_denominator_matches_the_round_type_branch() -> None:
    """The advance's ``m`` is the same figure as the matching round type's
    sample."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3) + eco_ct(demo, 4, round_type="full"),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    eco = branch(report, "de_ancient", "CT", "eco")
    advance = next(a for a in report.anomalies if a.rule == "ct_advance")
    assert advance.m == eco.sample.rounds == 3


def test_a_demo_without_an_orientation_is_refused() -> None:
    """A missing key is a different thing from an empty orientation."""
    demo = "Ancient_vs_x"
    with pytest.raises(AggregateError, match="area orientation was not given"):
        anomaly_report(
            eco_ct(demo, 1),
            advance_round(demo, 1),
            demo=demo,
            orientation={"toinen-demo": {}},
            limits=thresholds(),
        )


def test_an_empty_orientation_silences_the_rules_and_is_recorded() -> None:
    """An empty orientation is a **blind spot**, and the coverage says so out
    loud."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1),
        demo=demo,
        orientation={demo: {}},
        limits=thresholds(),
    )
    assert report.anomalies == []
    assert report.anomaly_scan.demos_without_orientation == [demo]


def test_the_scan_says_what_was_run_and_on_what() -> None:
    """An empty chapter is an observation only about what was examined."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2) + eco_ct(demo, 3, round_type="full"),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    scan = report.anomaly_scan
    assert scan.rules == ["ct_advance", "crunch", "stack"]
    assert scan.rules_deferred == []
    assert scan.rounds_scanned == 3
    # All three rounds are the CT side's, and two of them are eco: crunch can
    # hit on three, the advance on two.
    assert scan.crunch_rounds == 3
    assert scan.advance_rounds == 2
    # Stack saw none of them: the helper's default cloud is empty, so no site
    # groups were obtained. **That very difference is the reason for the
    # coverage**: the same demo is in crunch's denominator with three rounds
    # and in stack's with zero.
    assert scan.stack_rounds == 0
    assert scan.demos_without_site_groups == [demo]
    assert scan.demos_without_orientation == []


def test_an_area_below_the_threshold_leaves_no_blind_spot() -> None:
    """A blind spot is **a missing orientation**, not a missing hit.

    A demo that has a T-side area but no hit is a measured negative -- exactly
    what Nuke's zero crunches are. It does not belong on the list of the
    blind.
    """
    demo = "Nuke_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1, area="CTSpawn"),
        demo=demo,
        orientation={demo: {"Lobby": AreaObservations(t=57, total=64)}},
        limits=thresholds(),
    )
    assert report.anomalies == []
    assert report.anomaly_scan.demos_without_orientation == []


def test_the_map_name_source_is_carried_to_the_anomaly() -> None:
    """An unidentified map has to be recognisable on the anomaly row too."""
    demo = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    assert report.anomalies[0].map_name_source == "unknown"
    assert report.anomalies[0].map_name == demo


def test_a_null_side_on_a_sample_row_is_refused() -> None:
    """``str(None)`` would quietly decide that the row is not CT."""
    demo = "Ancient_vs_x"
    rows = advance_round(demo, 1)
    rows[0]["side"] = None
    with pytest.raises(AggregateError, match="the side is None"):
        anomaly_report(
            eco_ct(demo, 1), rows, demo=demo, limits=thresholds()
        )


def test_a_null_is_alive_on_a_sample_row_is_refused() -> None:
    """``bool(None)`` would quietly decide that the player is dead."""
    demo = "Ancient_vs_x"
    rows = advance_round(demo, 1)
    rows[0]["is_alive"] = None
    with pytest.raises(AggregateError, match="being alive is missing"):
        anomaly_report(
            eco_ct(demo, 1), rows, demo=demo, limits=thresholds()
        )


def test_an_area_written_with_stray_whitespace_still_matches() -> None:
    """The orientation and the presence are normalised by the same function.

    Without it ``" TSideLower "`` would be a different area in the orientation
    from the one in the presence, and the rule would stay silent on that area
    -- with nothing to say why.
    """
    demo = "Ancient_vs_x"
    rows = advance_round(demo, 1)
    rows[0]["area"] = f" {ANOMALY_AREA} "
    report = anomaly_report(
        eco_ct(demo, 1),
        rows,
        demo=demo,
        orientation={demo: {f"{ANOMALY_AREA} ": AreaObservations(t=21, total=24)}},
        limits=thresholds(),
    )
    assert areas_of(report, "ct_advance") == [ANOMALY_AREA]


def test_a_duplicated_sample_row_does_not_silence_the_crunch() -> None:
    """A duplicated row used to pair with itself and eat the source area."""
    demo = "Ancient_vs_x"
    rows = crunch_round(demo, 1)
    # Duplicate one row of the target area.
    rows.append(dict(rows[-1]))
    report = anomaly_report(
        eco_ct(demo, 1, 2), rows, demo=demo, limits=thresholds()
    )
    crunch = next(a for a in report.anomalies if a.rule == "crunch")
    assert crunch.rounds[0].sources == ["SideEntrance", "TSideUpper"]
    assert crunch.players_max == 2


# --- Stack (Story 2.14) ---------------------------------------------------------


def test_a_stack_anomaly_carries_the_site_its_group_and_the_survivors() -> None:
    """The row names the site's own area, the group and the survivors.

    The denominator is **all of the side's rounds**, as with crunch: the rule
    does not know the round type, and splitting into an eco row and a default
    row would give the same pattern two different denominators.
    """
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2) + eco_ct(demo, 3, round_type="full"),
        stack_round(demo, 1),
        demo=demo,
        limits=thresholds(),
        point_clouds=stack_cloud(demo),
    )
    stacks = [a for a in report.anomalies if a.rule == "stack"]
    assert len(stacks) == 1
    stack = stacks[0]
    assert stack.area == "BombsiteB"
    assert stack.site == "B"
    assert stack.side == "CT"
    assert (stack.n, stack.m) == (1, 3)
    assert stack.rounds[0].players_max == 4
    assert [(p.sample_t_s, p.players, p.alive) for p in stack.rounds[0].points] == [
        (15.0, 4, 5)
    ]
    # The orientation is empty: the rule does not read it, so a figure would
    # have been invented.
    assert stack.orientation == []


def test_a_stack_spanning_round_types_is_one_row() -> None:
    """The round type is an observation on the row, not the denominator.

    The same pattern on eco and on a full buy is **one row with a sample of
    2/3**, and ``round_types`` says on which types it was observed.
    """
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1) + eco_ct(demo, 2, 3, round_type="full"),
        stack_round(demo, 1) + stack_round(demo, 2, seconds=30.0),
        demo=demo,
        limits=thresholds(),
        point_clouds=stack_cloud(demo),
    )
    stack = next(a for a in report.anomalies if a.rule == "stack")
    assert stack.round_types == ["eco", "full"]
    assert (stack.n, stack.m) == (2, 3)
    assert [entry.round_no for entry in stack.rounds] == [1, 2]


def test_a_silenced_demo_is_in_the_coverage_and_not_in_the_denominator() -> None:
    """A silenced demo is in crunch's denominator but not in stack's.

    This very difference is the reason for the whole ``stack_rounds`` field:
    without it Nuke's rounds would look examined with a nil result.
    """
    speaks = "Ancient_vs_x"
    silent = "Nuke_vs_y"
    clouds = stack_cloud(speaks)
    # The sites overlap: a separation of 2 cells, the sites' own size 20 + 20.
    clouds[silent] = [
        CloudCell(a, x, y, z) for a, x, y, z in OVERLAPPING_SITE_CLOUD
    ]
    report = anomaly_report(
        eco_ct(speaks, 1, 2) + eco_ct(silent, 1, 2, 3),
        stack_round(speaks, 1),
        demo=speaks,
        limits=thresholds(),
        orientation={speaks: {}, silent: {}},
        map_names={speaks: "de_ancient", silent: "de_nuke"},
        point_clouds=clouds,
    )
    scan = report.anomaly_scan
    assert scan.crunch_rounds == 5
    assert scan.stack_rounds == 2
    assert scan.demos_without_site_groups == [silent]
    assert [a.map_name for a in report.anomalies if a.rule == "stack"] == [
        "de_ancient"
    ]


def test_a_silenced_demo_is_not_in_the_stack_denominator() -> None:
    """A map from two demos, one of which stays silent.

    The silenced demo's rounds are in **crunch's** denominator but not in
    stack's: the rule did not see them. Without the scoping a row's ``n/m``
    would speak of a different coverage from the chapter's own coverage text
    (``stack_rounds``), which does leave them out -- that is, the same figure
    with two values in the same report.
    """
    speaks = "ANCIENT_vs_a"
    silent = "Ancient_vs_b"
    clouds = stack_cloud(speaks)
    clouds[silent] = [
        CloudCell(a, x, y, z) for a, x, y, z in OVERLAPPING_SITE_CLOUD
    ]
    report = anomaly_report(
        eco_ct(speaks, 1, 2) + eco_ct(silent, 1, 2, 3),
        stack_round(speaks, 1),
        demo=speaks,
        limits=thresholds(),
        orientation={speaks: {}, silent: {}},
        map_names={speaks: "de_ancient", silent: "de_ancient"},
        point_clouds=clouds,
    )
    # One map, five CT rounds -- but stack saw two of them.
    assert [m.map_name for m in report.maps] == ["de_ancient"]
    stack = next(a for a in report.anomalies if a.rule == "stack")
    assert (stack.n, stack.m) == (1, 2)
    assert report.anomaly_scan.crunch_rounds == 5
    assert report.anomaly_scan.stack_rounds == 2
    # The row's denominator and the coverage figure say the same: both 2,
    # not 5.
    assert stack.m == report.anomaly_scan.stack_rounds


def test_a_missing_point_cloud_is_refused_rather_than_assumed() -> None:
    """A missing key is not the same thing as a cloud that yielded no groups.

    A default assumed in silence would silence the rule on that very demo, and
    the coverage would record it as a property of the map -- even though it
    would be the caller's oversight.
    """
    demo = "Ancient_vs_x"
    with pytest.raises(AggregateError, match="point cloud was not given"):
        anomaly_report(
            eco_ct(demo, 1),
            stack_round(demo, 1),
            demo=demo,
            limits=thresholds(),
            point_clouds={},
        )


def test_the_stack_threshold_is_read_from_the_settings() -> None:
    """A four-player set-up disappears when the threshold is raised to five."""
    demo = "Ancient_vs_x"

    def stacks(min_players: int) -> list[str]:
        report = anomaly_report(
            eco_ct(demo, 1, 2),
            stack_round(demo, 1),
            demo=demo,
            limits=thresholds(stack_min_players=min_players),
            point_clouds=stack_cloud(demo),
        )
        return areas_of(report, "stack")

    assert stacks(4) == ["BombsiteB"]
    assert stacks(5) == []


def test_the_separation_threshold_is_a_setting_not_code() -> None:
    """The same demo stays silent or speaks depending on which threshold is
    set.

    The cloud is the **overlapping sites** cloud, whose ratio is 0.05 -- the
    Nuke case sharpened. At production's threshold of 2.0 it stays silent; low
    enough, it speaks, and then the same set-up produces a hit. The direction
    is this way round because a separating cloud's ratio is 25 and no
    threshold the model allows (an upper bound of 20) would silence it -- and
    that upper bound is exactly the good news: silencing the measured maps by
    accident cannot be done.
    """
    demo = "Nuke_vs_x"
    clouds = {
        demo: [CloudCell(a, x, y, z) for a, x, y, z in OVERLAPPING_SITE_CLOUD]
    }
    # ``House`` is in B's group in this cloud (10 cells against 8).
    rows = stack_round(
        demo, 1, others=("House", "House"), elsewhere=("BombsiteA",)
    )

    def stacks(separation_min: float) -> list[str]:
        report = anomaly_report(
            eco_ct(demo, 1, 2),
            rows,
            demo=demo,
            limits=thresholds(stack_site_separation_min=separation_min),
            point_clouds=clouds,
        )
        return areas_of(report, "stack")

    assert stacks(2.0) == []
    assert stacks(0.01) == ["BombsiteB"]
