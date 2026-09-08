"""``domain.utility`` -- reducing trajectories and the detonation area from the
point cloud.

Every function is pure, so every row of the I/O matrix is one call away here
without a demo file. The trajectories are built by hand, and they imitate the
structure of a real demo: a grenade has rows while it is in a player's bag as
well (the coordinates empty), and ``grenade_entity_id`` gets recycled.

The point cloud is built by hand in the same way: a handful of observations is
enough to prove the choice of the mode, the tie-break, the vertical weight and
the threshold, and not one of those needs a million rows.
"""

from __future__ import annotations

import polars as pl
import pytest

from pappascout.constants import EVENT_KINDS
from pappascout.domain.utility import (
    CLOUD_CELL_COLUMNS,
    CLOUD_OBSERVATION_COLUMNS,
    DETONATE,
    ENDPOINT_COLUMNS,
    MAX_TRAJECTORY_GAP_SECONDS,
    NEAREST_CHUNK_POINTS,
    NEAREST_POINT_COLUMNS,
    NEAREST_RESULT_COLUMNS,
    THROWN,
    TRAJECTORY_COLUMNS,
    build_point_cloud,
    empty_point_cloud,
    grenade_endpoints,
    nearest_cells,
    trajectory_gap_ticks,
)

#: The point cloud's cell edge in these tests. The same as ``settings.toml``'s
#: ``callout_grid_units``, so that the tests' numbers measure the production
#: grid.
GRID = 32

#: The observation table's types. Explicitly, because even one column holding
#: nothing but ``None`` values would otherwise get a ``Null`` type -- and the
#: filter under test would fail for a different reason than the test claims.
OBSERVATION_SCHEMA: dict[str, object] = {
    "x": pl.Float64,
    "y": pl.Float64,
    "z": pl.Float64,
    "area": pl.Utf8,
    "is_alive": pl.Boolean,
}

#: The fake demos' tick rate; the trajectory's allowed gap is computed from it.
TICK_RATE = 64.0
GAP = trajectory_gap_ticks(TICK_RATE)

TRAJECTORY_SCHEMA: dict[str, object] = {
    "grenade_entity_id": pl.Int32,
    "grenade_type": pl.Utf8,
    "thrower_id": pl.Utf8,
    "tick": pl.Int32,
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
}


def trajectory(
    entity: int,
    thrower: str | None,
    grenade_type: str,
    ticks: list[int],
    *,
    start: tuple[float, float, float] = (0.0, 0.0, 0.0),
    step: tuple[float, float, float] = (10.0, 0.0, 0.0),
    in_bag: list[int] | None = None,
) -> list[dict[str, object]]:
    """One grenade's rows: an optional bag phase and then the trajectory.

    Args:
        entity: ``grenade_entity_id``.
        thrower: The thrower; ``None`` imitates a trajectory without one.
        grenade_type: The type as the adapter gives it.
        ticks: The trajectory's ticks.
        start: The trajectory's first point.
        step: The displacement per tick.
        in_bag: The ticks on which the grenade is in a bag (the coordinates
            empty).
    """
    rows: list[dict[str, object]] = []
    for tick in in_bag or []:
        rows.append(
            {
                "grenade_entity_id": entity,
                "grenade_type": grenade_type,
                "thrower_id": thrower,
                "tick": tick,
                "x": None,
                "y": None,
                "z": None,
            }
        )
    for index, tick in enumerate(ticks):
        rows.append(
            {
                "grenade_entity_id": entity,
                "grenade_type": grenade_type,
                "thrower_id": thrower,
                "tick": tick,
                "x": start[0] + step[0] * index,
                "y": start[1] + step[1] * index,
                "z": start[2] + step[2] * index,
            }
        )
    return rows


def frame(*grenades: list[dict[str, object]]) -> pl.DataFrame:
    rows = [row for grenade in grenades for row in grenade]
    return pl.DataFrame(rows, schema=dict(TRAJECTORY_SCHEMA), orient="row")


def endpoints(frame_in: pl.DataFrame, *, gap: int = GAP):
    """``grenade_endpoints`` with an ordinary 64-tick demo's gap."""
    return grenade_endpoints(frame_in, max_gap_ticks=gap)


# --- Constants -----------------------------------------------------------------


def test_event_kinds_match_the_shared_enum() -> None:
    """The event kinds are the same list as in the ``EVENTS`` schema.

    The check is in a test rather than in a module-level assert: an assert
    would vanish under ``python -O`` exactly when it was needed.
    """
    assert (THROWN, DETONATE) == EVENT_KINDS


def test_endpoint_columns_are_unique() -> None:
    assert len(ENDPOINT_COLUMNS) == len(set(ENDPOINT_COLUMNS))
    assert len(TRAJECTORY_COLUMNS) == len(set(TRAJECTORY_COLUMNS))


# --- grenade_endpoints ---------------------------------------------------------


def test_a_normal_grenade_becomes_two_rows() -> None:
    """The I/O matrix: a smoke thrown and detonated -> a throw and a
    detonation."""
    result, dropped = endpoints(
        frame(trajectory(7, "aaa", "smoke", [100, 101, 102, 103]))
    )

    assert dropped == 0
    assert tuple(result.columns) == ENDPOINT_COLUMNS
    assert result.height == 2
    assert result["event_kind"].to_list() == [THROWN, DETONATE]
    assert result["tick"].to_list() == [100, 103]
    assert result["x"].to_list() == [0.0, 30.0]
    assert result["thrower_id"].unique().to_list() == ["aaa"]
    assert result["grenade_entity_id"].unique().to_list() == [7]


def test_the_whole_trajectory_collapses_to_the_two_endpoints() -> None:
    """1.55 million rows must not travel onwards -- two are enough."""
    long_flight = trajectory(3, "aaa", "smoke", list(range(1000, 3000)))
    result, _ = endpoints(frame(long_flight))
    assert result.height == 2
    assert result["tick"].to_list() == [1000, 2999]


def test_a_single_point_trajectory_gets_no_invented_detonation() -> None:
    """The I/O matrix: the trajectory breaks off -> only ``grenade_thrown``.

    An invented detonation at the same point would claim smoke where there was
    none.
    """
    result, dropped = endpoints(frame(trajectory(9, "aaa", "he", [500])))
    assert dropped == 0
    assert result.height == 1
    assert result["event_kind"].to_list() == [THROWN]


def test_rows_without_coordinates_are_not_a_trajectory() -> None:
    """A grenade in a bag is not a throw.

    In a real demo 1.34 million rows out of 1.55 are of this kind. Without
    this filtering the throwing place would be an empty coordinate minutes
    before the actual throw.
    """
    result, dropped = endpoints(
        frame(
            trajectory(
                4, "aaa", "smoke", [200, 201, 202], in_bag=[100, 120, 150, 199]
            )
        )
    )
    assert dropped == 0
    assert result["tick"].to_list() == [200, 202]


def test_a_grenade_never_thrown_produces_nothing() -> None:
    """Nothing but bag rows -> no grenade, no drop."""
    result, dropped = endpoints(
        frame(trajectory(4, "aaa", "smoke", [], in_bag=[100, 101, 102]))
    )
    assert result.is_empty()
    assert dropped == 0


def test_a_reused_entity_id_is_two_grenades() -> None:
    """The game recycles the ids -- grouping by the id would join them.

    In a real demo Ancient's 374 trajectories fit into 187 ids. If these
    joined, the throw would come from the first round and the "detonation"
    from the second, from a different player and a different side of the map.
    """
    result, _ = endpoints(
        frame(
            trajectory(5, "aaa", "smoke", [100, 101, 102]),
            trajectory(5, "bbb", "he", [5000, 5001, 5002], start=(900.0, 0.0, 0.0)),
        )
    )
    assert result.height == 4
    assert result["grenade_no"].to_list() == [0, 0, 1, 1]
    assert result["thrower_id"].to_list() == ["aaa", "aaa", "bbb", "bbb"]
    assert result["tick"].to_list() == [100, 102, 5000, 5002]


def test_the_same_entity_and_thrower_split_on_a_tick_gap() -> None:
    """The same id, the same thrower, the same type -- only the gap separates
    them."""
    result, _ = endpoints(
        frame(
            trajectory(5, "aaa", "smoke", [100, 101, 102]),
            trajectory(5, "aaa", "smoke", [4000, 4001], start=(900.0, 0.0, 0.0)),
        )
    )
    assert result.height == 4
    assert result["tick"].to_list() == [100, 102, 4000, 4001]


def test_a_small_hole_in_the_trajectory_does_not_invent_a_grenade() -> None:
    """One lost tick must not break a trajectory into two grenades.

    An invented row is a worse error than a lost one: it would add a throw
    that did not happen.
    """
    ticks = [100, 101, 102 + GAP - 1]
    result, _ = endpoints(frame(trajectory(5, "aaa", "smoke", ticks)))
    assert result.height == 2
    assert result["tick"].to_list() == [100, ticks[-1]]


def test_a_trajectory_without_a_thrower_is_dropped_and_counted() -> None:
    """The I/O matrix: a trajectory without a throw -> skipped, the number
    reported."""
    result, dropped = endpoints(
        frame(
            trajectory(1, None, "smoke", [100, 101]),
            trajectory(2, "aaa", "smoke", [300, 301]),
        )
    )
    assert dropped == 1
    assert result.height == 2
    assert result["thrower_id"].unique().to_list() == ["aaa"]


def test_grenades_are_numbered_in_throw_order() -> None:
    """``grenade_no`` is the pair's only reliable key, and it follows time."""
    result, _ = endpoints(
        frame(
            trajectory(50, "aaa", "smoke", [900, 901]),
            trajectory(9, "bbb", "he", [100, 101]),
        )
    )
    assert result["tick"].to_list() == [100, 101, 900, 901]
    assert result["grenade_no"].to_list() == [0, 0, 1, 1]


def test_three_trajectories_on_one_id_get_three_numbers() -> None:
    """The I/O matrix: the id repeats within a round -> three trajectories.

    Measured from ``inferno_vs_ryhmarama``: on round 11 the id 564 carries
    three trajectories -- a molotov, a flashbang and an incendiary. The
    segmentation tells them apart correctly, but the pair ``(round_no,
    grenade_entity_id)`` does not -- which is why each has its own
    ``grenade_no``, and the game's own id is the same on all of them. The
    types are the same as in the real demo; the times are condensed into the
    test's ticks.
    """
    result, _ = endpoints(
        frame(
            trajectory(564, "aaa", "molotov", [500, 504]),
            trajectory(564, "aaa", "flashbang", [800, 806]),
            trajectory(564, "bbb", "incendiary", [1200, 1206]),
        )
    )

    throws = result.filter(pl.col("event_kind") == THROWN)
    assert throws.height == 3
    assert throws["grenade_no"].n_unique() == 3
    assert throws["grenade_entity_id"].unique().to_list() == [564]
    # The times and the types survive as they stand -- the id does not change
    # the observation.
    assert throws["tick"].to_list() == [500, 800, 1200]
    assert throws["grenade_type"].to_list() == [
        "molotov",
        "flashbang",
        "incendiary",
    ]


def test_the_number_is_unique_over_the_whole_result() -> None:
    """The unambiguity is demo-wide, not per round.

    A per-round running number would look just as good here, but it would fail
    as soon as the aggregation joined the utility of two rounds into the same
    frame. That is why the claim is about the whole table.
    """
    result, _ = endpoints(
        frame(
            trajectory(1, "aaa", "smoke", [100, 104]),
            trajectory(1, "bbb", "smoke", [900, 904]),
            trajectory(2, "aaa", "he", [140, 144]),
            trajectory(2, "ccc", "flashbang", [950]),
        )
    )

    pairs = result.select("grenade_no", "event_kind")
    assert pairs.height == pairs.unique().height
    assert result["grenade_no"].n_unique() == 4


def test_the_throw_and_its_detonation_share_the_number() -> None:
    """The I/O matrix: a throw and a detonation -- the number is their only
    link."""
    result, _ = endpoints(frame(trajectory(7, "aaa", "smoke", [100, 104, 108])))

    assert result["event_kind"].to_list() == [THROWN, DETONATE]
    assert result["grenade_no"].n_unique() == 1


def test_an_unexploded_grenade_gets_a_number_of_its_own() -> None:
    """The I/O matrix: a single-point trajectory -> only a throw, but a number
    of its own."""
    result, _ = endpoints(
        frame(
            trajectory(1, "aaa", "smoke", [100]),
            trajectory(2, "bbb", "he", [200, 204]),
        )
    )

    lone = result.filter(pl.col("grenade_entity_id") == 1)
    assert lone["event_kind"].to_list() == [THROWN]
    other = result.filter(pl.col("grenade_entity_id") == 2)
    assert lone["grenade_no"][0] not in other["grenade_no"].to_list()


def test_the_same_input_gives_the_same_numbers() -> None:
    """The I/O matrix: the same demo again -> the same ids.

    Stability is not a convenience but a condition: if the numbers changed
    between runs, re-parsing the archive would look like a change. The input
    is shuffled, because repeating the same function on the same input would
    prove nothing about stability.
    """
    rows = frame(
        trajectory(3, "aaa", "smoke", [900, 906]),
        trajectory(3, "aaa", "he", [100, 106]),
        trajectory(8, "bbb", "flashbang", [400, 402]),
    )
    first, _ = endpoints(rows)
    for seed in range(5):
        shuffled, _ = endpoints(rows.sample(fraction=1.0, shuffle=True, seed=seed))
        assert first.equals(shuffled), seed


def test_two_rows_on_the_same_tick_do_not_make_the_result_undefined() -> None:
    """The segmentation must not depend on the order the rows came in.

    The run boundary is read from the neighbouring rows, and Polars' sort is
    not stable. If the key were just ``(id, tick)``, two rows with the same id
    and the same tick could swap places between runs -- and then **the
    segmentation itself**, not just the numbering, would be undetermined.

    Here the same id carries two different types on the same ticks, which is
    the worst case: the type is a key of the segmentation, so the order of the
    rows decides where a run breaks.
    """
    rows = frame(
        trajectory(9, "aaa", "smoke", [100, 102], start=(0.0, 0.0, 0.0)),
        trajectory(9, "aaa", "he", [100, 102], start=(50.0, 0.0, 0.0)),
    )
    first, _ = endpoints(rows)
    for seed in range(8):
        shuffled, _ = endpoints(rows.sample(fraction=1.0, shuffle=True, seed=seed))
        assert first.equals(shuffled), seed


def test_two_trajectories_at_the_same_moment_stay_apart() -> None:
    """A tie in time does not mix the trajectories with each other.

    Two grenades can leave on the same tick (two players throwing at once).
    The number has to tell them apart, and both rows of a trajectory have to
    stay under the same number -- otherwise the throw and the detonation would
    cross over.
    """
    result, _ = endpoints(
        frame(
            trajectory(11, "aaa", "smoke", [300, 306]),
            trajectory(12, "bbb", "smoke", [300, 306]),
        )
    )

    assert result["grenade_no"].n_unique() == 2
    for _, pair in result.group_by("grenade_no", maintain_order=True):
        assert pair["event_kind"].to_list() == [THROWN, DETONATE]
        assert pair["grenade_entity_id"].n_unique() == 1


def test_an_empty_table_gives_an_empty_result_with_the_right_types() -> None:
    """The I/O matrix: a demo without utility -> an empty result, no crash."""
    result, dropped = endpoints(pl.DataFrame(schema=dict(TRAJECTORY_SCHEMA)))
    assert result.is_empty()
    assert dropped == 0
    assert tuple(result.columns) == ENDPOINT_COLUMNS


def test_a_missing_column_is_an_error_not_an_empty_result() -> None:
    """An empty result would look like a demo in which no grenade was
    thrown."""
    broken = frame(trajectory(1, "aaa", "smoke", [10, 11])).drop("thrower_id")
    with pytest.raises(ValueError) as exc:
        endpoints(broken)
    assert "thrower_id" in str(exc.value)


def test_non_finite_coordinates_are_not_a_trajectory_point() -> None:
    """A NaN coordinate is not an observation, even though it is not null."""
    rows = trajectory(1, "aaa", "smoke", [10, 11, 12])
    rows[0]["x"] = float("nan")
    result, _ = endpoints(frame(rows))
    assert result["tick"].to_list() == [11, 12]


# --- Scaling the gap with the tick rate ----------------------------------------


def test_the_gap_is_the_same_moment_at_any_tick_rate() -> None:
    """The gap is time and not ticks.

    A fixed number of ticks would be half as long a moment in a 128-tick demo,
    and the same flight could split into two grenades -- that is, **invent an
    extra throw** -- merely because the server ran more densely.
    """
    assert trajectory_gap_ticks(64.0) == 8
    assert trajectory_gap_ticks(128.0) == 16
    for tick_rate in (64.0, 128.0):
        assert trajectory_gap_ticks(tick_rate) / tick_rate == pytest.approx(
            MAX_TRAJECTORY_GAP_SECONDS
        )


def test_the_gap_is_never_zero() -> None:
    """Zero would mean that no gap is allowed -- one lost tick would be
    enough."""
    assert trajectory_gap_ticks(1.0) >= 1


@pytest.mark.parametrize("tick_rate", [0.0, -64.0, float("nan"), float("inf")])
def test_an_impossible_tick_rate_is_refused(tick_rate: float) -> None:
    with pytest.raises(ValueError, match="Tickrate"):
        trajectory_gap_ticks(tick_rate)


def test_a_128_tick_demo_keeps_a_trajectory_whole() -> None:
    """The same gap in seconds, a different one in ticks: the trajectory must
    not break."""
    ticks = [1000, 1001, 1001 + trajectory_gap_ticks(128.0)]
    result, _ = endpoints(
        frame(trajectory(5, "aaa", "smoke", ticks)),
        gap=trajectory_gap_ticks(128.0),
    )
    assert result.height == 2
    # With the same gap in a 64-tick demo it would be a different grenade: two
    # points and then a lone third, that is a throw + a detonation + an
    # invented third throw.
    split_result, _ = endpoints(
        frame(trajectory(5, "aaa", "smoke", ticks)),
        gap=trajectory_gap_ticks(64.0),
    )
    assert split_result["grenade_no"].n_unique() == 2
    assert split_result.filter(pl.col("event_kind") == THROWN).height == 2


# --- Missing values ------------------------------------------------------------


def test_a_row_without_a_grenade_type_is_not_a_trajectory_point() -> None:
    """An empty type would bring down a whole demo in the ``EVENTS``
    validation.

    ``grenade_type`` is mandatory in the contract, so one broken row would
    prevent the parse of a 233 MB demo entirely.
    """
    rows = trajectory(1, "aaa", "smoke", [10, 11, 12])
    rows[0]["grenade_type"] = None
    result, _ = endpoints(frame(rows))
    assert result["tick"].to_list() == [11, 12]
    assert result["grenade_type"].null_count() == 0


def test_a_trajectory_of_only_null_types_disappears() -> None:
    rows = trajectory(1, "aaa", "smoke", [10, 11])
    for row in rows:
        row["grenade_type"] = None
    result, dropped = endpoints(frame(rows))
    assert result.is_empty()
    assert dropped == 0


# --- Building the point cloud --------------------------------------------------


def observations(rows: list[dict[str, object]]) -> pl.DataFrame:
    """An observation table with the contract's types."""
    return pl.DataFrame(rows, schema=OBSERVATION_SCHEMA, orient="row")


def seen(
    x: float,
    y: float,
    z: float = 0.0,
    area: str | None = "BombsiteA",
    alive: bool = True,
) -> dict[str, object]:
    return {"x": x, "y": y, "z": z, "area": area, "is_alive": alive}


def test_the_cloud_is_a_grid_of_where_players_stood() -> None:
    """Two observations in the same cell are one cell, a distant one is
    another."""
    cloud = build_point_cloud(
        observations([seen(10.0, 10.0), seen(20.0, 12.0), seen(300.0, 300.0, area="Mid")]),
        grid_units=GRID,
    )
    assert cloud.select("cell_x", "cell_y", "cell_z").rows() == [(0, 0, 0), (9, 9, 0)]
    assert cloud["area"].to_list() == ["BombsiteA", "Mid"]
    assert cloud["observations"].to_list() == [2, 1]


def test_the_cell_area_is_the_mode_not_the_first_row() -> None:
    """At the edge of a cell there are always rows from the neighbouring area.

    The first row would depend on the order demoparser2 gave the ticks in --
    that is, the same demo could give a different area on a different run.
    """
    rows = [seen(1.0, 1.0, area="Edge")] + [seen(2.0, 2.0, area="Centre")] * 3
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Centre"]
    # The observations are ALL of the cell's rows, not only the winning area's.
    assert cloud["observations"].to_list() == [4]


def test_a_tie_is_broken_by_the_area_name() -> None:
    """A tie must not be left to the chance of the sort."""
    rows = [seen(1.0, 1.0, area="Zulu"), seen(2.0, 2.0, area="Alpha")]
    assert build_point_cloud(observations(rows), grid_units=GRID)["area"].to_list() == [
        "Alpha"
    ]
    # The same content in another order gives the same answer.
    assert build_point_cloud(
        observations(list(reversed(rows))), grid_units=GRID
    )["area"].to_list() == ["Alpha"]


def test_a_dead_player_is_not_in_the_cloud() -> None:
    """A body stays where the player fell; a dead player does not move on the
    map."""
    rows = [seen(10.0, 10.0, area="Alive"), seen(300.0, 300.0, area="Body", alive=False)]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Alive"]


def test_an_unnamed_area_is_not_in_the_cloud() -> None:
    """A cell named "no name" would name a detonation empty.

    The row would still look like a hit -- the area null from within the
    threshold -- and the reader could not tell it apart from a case where no
    area was obtained at all.
    """
    rows = [seen(10.0, 10.0, area=None), seen(300.0, 300.0, area="Mid")]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Mid"]
    assert cloud["area"].null_count() == 0


def test_a_row_without_coordinates_is_not_in_the_cloud() -> None:
    rows = [
        {"x": None, "y": 1.0, "z": 0.0, "area": "Ghost", "is_alive": True},
        {"x": float("nan"), "y": 1.0, "z": 0.0, "area": "Ghost", "is_alive": True},
        seen(300.0, 300.0, area="Mid"),
    ]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Mid"]


def test_negative_coordinates_round_downwards() -> None:
    """CS maps lie on both sides of the origin, so truncation would be a
    defect.

    Truncating towards zero would put -1 and +1 in the same cell, and then the
    grid would be twice the size at the origin and two different areas would
    fuse.
    """
    cloud = build_point_cloud(
        observations([seen(-1.0, -1.0), seen(1.0, 1.0, area="Other")]),
        grid_units=GRID,
    )
    assert cloud.select("cell_x", "cell_y").rows() == [(-1, -1), (0, 0)]


def test_an_empty_cloud_still_has_the_contract_columns() -> None:
    """The I/O matrix: an empty point cloud is a valid result, not an error."""
    cloud = build_point_cloud(observations([]), grid_units=GRID)
    assert cloud.is_empty()
    assert cloud.columns == list(CLOUD_CELL_COLUMNS)
    assert cloud.schema == empty_point_cloud().schema


def test_a_cloud_of_only_dead_players_is_empty_not_broken() -> None:
    rows = [seen(10.0, 10.0, alive=False)]
    assert build_point_cloud(observations(rows), grid_units=GRID).is_empty()


def test_the_cloud_does_not_depend_on_the_row_order() -> None:
    """The acceptance criterion: the same demo twice -> identical tables.

    With a demo the claim is weak (a deterministic function on the same
    input). This is its strong form: **the same content in a different
    order**. If the choice of the mode relied on the stability of the grouping
    or of the sort, the cell's area could change between runs -- and the
    detonation area with it.
    """
    rows = [seen(1.0, 1.0, area="Alpha")] * 3 + [
        seen(2.0, 2.0, area="Beta")
    ] * 3 + [seen(3.0, 3.0, area="Gamma"), seen(300.0, 300.0, area="Delta")]
    forwards = build_point_cloud(observations(rows), grid_units=GRID)
    # Two different shuffles, so that neither is "the same order backwards".
    backwards = build_point_cloud(observations(rows[::-1]), grid_units=GRID)
    interleaved = build_point_cloud(
        observations(rows[1::2] + rows[0::2]), grid_units=GRID
    )
    assert forwards.equals(backwards)
    assert forwards.equals(interleaved)


@pytest.mark.parametrize("column", CLOUD_OBSERVATION_COLUMNS)
def test_a_missing_observation_column_is_named(column: str) -> None:
    """Without the check the result would be an empty cloud -- that is, a demo
    in which nobody moved."""
    rows = observations([seen(1.0, 1.0)]).drop(column)
    with pytest.raises(ValueError, match=column):
        build_point_cloud(rows, grid_units=GRID)


@pytest.mark.parametrize("grid", [0, -32, float("nan"), float("inf")])
def test_an_impossible_grid_size_is_refused(grid: float) -> None:
    with pytest.raises(ValueError, match="cell size"):
        build_point_cloud(observations([seen(1.0, 1.0)]), grid_units=grid)


# --- Looking up the nearest cell -----------------------------------------------


def points(rows: list[tuple[int, float | None, float | None, float | None]]):
    return pl.DataFrame(
        rows,
        schema={
            "point_id": pl.Int64,
            "x": pl.Float64,
            "y": pl.Float64,
            "z": pl.Float64,
        },
        orient="row",
    )


def two_area_cloud() -> pl.DataFrame:
    """Two cells far from each other, in different areas."""
    return build_point_cloud(
        observations([seen(16.0, 16.0, area="Lower"), seen(1000.0, 16.0, area="Upper")]),
        grid_units=GRID,
    )


def nearest(pts, cloud, *, max_units=256.0, z_weight=2.0, z_tolerance=72.0):
    """The nearest-cell lookup with the tests' default measures.

    The weight here is **2 and not production's 1**, and that is deliberate:
    these tests measure the *mechanics* of the weighting and not the
    production configuration, and a factor of two units makes the
    hand-computed expected values readable. The production value is guarded by
    ``tests/test_settings.py``.
    """
    return nearest_cells(
        pts,
        cloud,
        grid_units=GRID,
        z_weight=z_weight,
        z_tolerance_units=z_tolerance,
        max_units=max_units,
    )


def test_the_nearest_cell_gives_the_area() -> None:
    result = nearest(points([(7, 20.0, 20.0, 0.0)]), two_area_cloud())
    assert result["area"].to_list() == ["Lower"]
    assert result["distance"][0] == pytest.approx(5.657, abs=0.01)


def test_the_point_id_comes_back_unchanged() -> None:
    """The key is the caller's own (``grenade_no``); the function knows nothing
    of grenades."""
    result = nearest(points([(41, 20.0, 20.0, 0.0), (7, 20.0, 20.0, 0.0)]), two_area_cloud())
    assert sorted(result["point_id"].to_list()) == [7, 41]


def test_a_point_beyond_the_threshold_keeps_its_distance() -> None:
    """The I/O matrix: a detonation far away -> ``area`` null,
    ``snap_distance`` kept.

    The distance is what tells this apart from an empty point cloud: in both
    the area is null, but only here is it known how far away it would have
    been taken from.
    """
    result = nearest(points([(1, 5000.0, 16.0, 0.0)]), two_area_cloud())
    assert result["area"].to_list() == [None]
    # The cell's centre is 1008 (cell 31), so the distance is 3992.
    assert result["distance"][0] == pytest.approx(3992.0, abs=0.5)


def test_the_threshold_itself_still_counts() -> None:
    """The limit is ``<=`` and not ``<``: a cell 256 units away qualifies."""
    cloud = build_point_cloud(observations([seen(16.0, 16.0, area="Mid")]), grid_units=GRID)
    exactly = nearest(points([(1, 16.0 + 256.0, 16.0, 0.0)]), cloud, max_units=256.0)
    assert exactly["area"].to_list() == ["Mid"]
    just_over = nearest(points([(1, 16.0 + 256.1, 16.0, 0.0)]), cloud, max_units=256.0)
    assert just_over["area"].to_list() == [None]


def test_an_empty_cloud_gives_neither_area_nor_distance() -> None:
    """The I/O matrix: an empty point cloud -> every detonation area null."""
    result = nearest(points([(1, 20.0, 20.0, 0.0)]), empty_point_cloud())
    assert result["area"].to_list() == [None]
    assert result["distance"].to_list() == [None]


def test_a_point_without_coordinates_gives_nothing() -> None:
    result = nearest(points([(1, None, 20.0, 0.0)]), two_area_cloud())
    assert result["area"].to_list() == [None]
    assert result["distance"].to_list() == [None]


def test_the_players_own_height_is_free() -> None:
    """A grenade detonates anywhere between the floor and a head.

    A vertical penalty without a tolerance would hit exactly the normal case:
    smoke in the air, a molotov on the floor. A player's height of vertical
    difference must therefore cost nothing.
    """
    cloud = build_point_cloud(observations([seen(16.0, 16.0, 16.0, "Mid")]), grid_units=GRID)
    # The cell's centre is z = 16; 72 units higher the difference is free.
    result = nearest(points([(1, 16.0, 16.0, 16.0 + 72.0)]), cloud)
    assert result["distance"][0] == pytest.approx(0.0, abs=0.01)
    assert result["area"].to_list() == ["Mid"]


def test_height_beyond_the_tolerance_is_weighted() -> None:
    """A layered map: the cell below is right next door seen from above.

    The smoke here is exactly above the lower cell, 192 units higher, and the
    upper cell is 224 units away in the same plane. Without a weight the lower
    one would be nearer (192 < 224) and the smoke would get the wrong floor;
    weighted, its distance is 2 * (192 - 72) = 240, so the upper one wins.
    That is the whole task of the weight.
    """
    cloud = build_point_cloud(
        observations(
            [
                seen(16.0, 16.0, -180.0, "Downstairs"),
                seen(240.0, 16.0, 16.0, "Upstairs"),
            ]
        ),
        grid_units=GRID,
    )
    smoke = points([(1, 16.0, 16.0, 16.0)])
    result = nearest(smoke, cloud, max_units=1000.0)
    assert result["area"].to_list() == ["Upstairs"]
    assert result["distance"][0] == pytest.approx(224.0, abs=0.5)
    # Without the weight and the tolerance (weight 1, tolerance 0) the lower
    # one would be nearer -- that is the error the weight exists against.
    unweighted = nearest(
        smoke, cloud, max_units=1000.0, z_weight=1.0, z_tolerance=0.0
    )
    assert unweighted["area"].to_list() == ["Downstairs"]
    assert unweighted["distance"][0] == pytest.approx(192.0, abs=0.5)


@pytest.mark.parametrize("limit", [None, float("nan"), float("inf")])
def test_a_threshold_that_is_not_a_number_gives_no_area(limit: float | None) -> None:
    """The honest value of an uncalibrated setting, not the disappearance of
    the limit.

    The nearest cell is always found, so naming without a threshold would be a
    claim rather than a measurement. The distance is measured all the same --
    it is the material for the calibration.
    """
    result = nearest(points([(1, 20.0, 20.0, 0.0)]), two_area_cloud(), max_units=limit)
    assert result["area"].to_list() == [None]
    assert result["distance"][0] == pytest.approx(5.657, abs=0.01)


def test_an_equal_distance_is_broken_by_the_area_name() -> None:
    """Two equally distant cells in different areas: the same demo, the same
    answer."""
    cloud = build_point_cloud(
        observations([seen(-16.0, 16.0, area="Zulu"), seen(48.0, 16.0, area="Alpha")]),
        grid_units=GRID,
    )
    result = nearest(points([(1, 16.0, 16.0, 0.0)]), cloud, max_units=1000.0)
    assert result["area"].to_list() == ["Alpha"]


def test_chunking_does_not_change_the_answer() -> None:
    """The chunk size is a memory bound, not part of the answer.

    There are more points here than fit into one chunk, so both the chunking
    and the merging after it are exercised.
    """
    cloud = two_area_cloud()
    many = points(
        [(i, 20.0 if i % 2 else 1000.0, 20.0 if i % 2 else 16.0, 0.0)
         for i in range(NEAREST_CHUNK_POINTS * 2 + 3)]
    )
    result = nearest(many, cloud).sort("point_id")
    assert result.height == many.height
    odd = result.filter(pl.col("point_id") % 2 == 1)
    even = result.filter(pl.col("point_id") % 2 == 0)
    assert odd["area"].unique().to_list() == ["Lower"]
    assert even["area"].unique().to_list() == ["Upper"]


def test_no_points_at_all_gives_an_empty_result() -> None:
    """An empty input is valid: zero grenades is zero rows.

    The early return exists because a cross product with an empty side would
    produce an empty frame with the wrong types -- and the caller would join
    it silently empty.
    """
    empty = points([])
    result = nearest(empty, two_area_cloud())
    assert result.is_empty()
    assert result.columns == list(NEAREST_RESULT_COLUMNS)


def test_a_duplicate_point_id_is_refused() -> None:
    """A key that occurs twice would **multiply** in the final join.

    The chunks are grouped separately and then merged, so the same key in two
    chunks would produce two rows in ``best`` and through that four rows in
    the result. The caller would get the same grenade more than once without
    anything failing.
    """
    doubled = points([(7, 20.0, 20.0, 0.0), (7, 30.0, 30.0, 0.0)])
    with pytest.raises(ValueError, match="point_id"):
        nearest(doubled, two_area_cloud())


def test_a_blank_area_is_not_an_area() -> None:
    """A single space is not an area name, even though it is not null.

    The rule is here and not only in the adapter: this function is public, and
    its contract is "an arealess observation does not reach the cloud". A cell
    named ``" "`` would name a detonation empty within the threshold and would
    look like a hit.
    """
    rows = [seen(10.0, 10.0, area=""), seen(12.0, 12.0, area="   "),
            seen(300.0, 300.0, area="Mid")]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Mid"]


@pytest.mark.parametrize("column", NEAREST_POINT_COLUMNS)
def test_a_missing_point_column_is_named(column: str) -> None:
    pts = points([(1, 20.0, 20.0, 0.0)]).drop(column)
    with pytest.raises(ValueError, match=column):
        nearest(pts, two_area_cloud())


@pytest.mark.parametrize("column", CLOUD_CELL_COLUMNS)
def test_a_missing_cloud_column_is_named(column: str) -> None:
    cloud = two_area_cloud().drop(column)
    with pytest.raises(ValueError, match=column):
        nearest(points([(1, 20.0, 20.0, 0.0)]), cloud)


@pytest.mark.parametrize("weight", [-1.0, float("nan"), float("inf")])
def test_an_impossible_z_weight_is_refused(weight: float) -> None:
    with pytest.raises(ValueError, match="z_weight"):
        nearest(points([(1, 20.0, 20.0, 0.0)]), two_area_cloud(), z_weight=weight)


@pytest.mark.parametrize("tolerance", [-1.0, float("nan"), float("inf")])
def test_an_impossible_z_tolerance_is_refused(tolerance: float) -> None:
    with pytest.raises(ValueError, match="z_tolerance_units"):
        nearest(
            points([(1, 20.0, 20.0, 0.0)]), two_area_cloud(), z_tolerance=tolerance
        )
