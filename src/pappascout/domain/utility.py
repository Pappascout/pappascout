"""Reducing utility trajectories, and the detonation's area (AD-5).

Utility is measured **from throws, not from purchases**: utility gets dropped,
so the buyer and the thrower can be different players. The throw cannot be had
from an event, though -- there is no ``grenade_thrown`` event -- but from the
trajectories, which demoparser2 returns as **a row per grenade per tick**. On
Ancient there are 1 553 329 of them, and that is why this module's most
important task is to reduce a trajectory to two points at once: the
trajectory's first point is the throw, the last one the detonation.

Why ``grenade_entity_id`` alone is not enough
---------------------------------------------
The game **recycles the entity ids**. On Ancient 374 trajectories fit into 187
ids: the same ``grenade_entity_id`` is first a molotov on round 2 and then an
HE on round 14. A plain ``group_by(grenade_entity_id)`` would join them into
one grenade whose throw came from the first and whose "detonation" came from
the last -- a different round, a different player, a different side of the
map. The trajectory therefore has to be cut into **contiguous runs**: a change
of id, of thrower or of type starts a new grenade, and so does a gap left in
the ticks.

The round does not save the id. For a long time it looked as though the
recycling happened only between rounds and that ``(round_no,
grenade_entity_id)`` would do as a key. The league demos showed otherwise: on
``inferno_vs_ryhmarama`` round 11 the id 564 carries **three different
trajectories** within the same round -- a molotov at 9.2 s, a flashbang at
18.0 s and an incendiary at 64.2 s. The segmentation tells them apart
correctly, but the pair does not identify them -- which is why every
trajectory gets a ``grenade_no`` of its own, unambiguous across the whole
demo.

Why a row without coordinates is not a trajectory
-------------------------------------------------
A grenade has rows while it is in a player's bag as well: the type is
``CSmokeGrenade`` (not ``...Projectile``) and ``x, y, z`` are empty. Of
Ancient's 1.55 million rows, 1.34 million are of this kind. They are not a
trajectory and not throws, so they are filtered out before the segmentation.

The detonation's area is derived, the throw's area observed
-----------------------------------------------------------
These two are not the same information, and they must not be computed the same
way.

**The thrower** has their own ``m_szLastPlaceName`` from the same tick, so the
throw's area is read straight from them. It is an observation and is not
derived from anything -- this module is not on the throw's path at all.

**A grenade** has no ``last_place_name`` field, so the detonation's area has
to be inferred from the coordinates. The method is a **point cloud**
(:func:`build_point_cloud`, :func:`nearest_cells`): from the demo's own ticks
a grid is assembled of where the players have really stood on the map and
which area is at each spot, and the detonation is named after the nearest
cell's area.

Why not the nearest living player
----------------------------------
Story 2.2 derived the detonation area from the nearest living player. That was
not imprecise but **structurally wrong**: smoke is thrown where nobody is --
precisely because it blocks sight and forces rotations. The proxy therefore
measured the opposite of what it was meant to, and **42% of the detonations
were left without any area at all** (measured from four league demos, 1 716
detonations). With the point cloud the share is 6.4%.

In the point cloud the source is the game's own area definition
(``env_cs_place``) rather than a neighbouring player. The method is **not kept
alongside as a fallback source**: two methods would make the row
uninterpretable, because the reader would not see which one named the area.

The threshold does not go away
------------------------------
"The nearest cell is always found" is not coverage. The maximum distance
measured across six demos is 1 074 units; without a threshold the report would
claim an area for a detonation that happened far from everywhere any player
has ever stood. ``[parse].area_snap_units`` is therefore kept and
**mandatory**, and it has been recalibrated for the point cloud: with the
threshold 256 the area is obtained for 2 428/2 544 detonations, that is 95.4%.

The distance **survives even then**, when it exceeds the threshold: ``area``
is left empty but ``snap_distance`` says how far away the area would have been
taken from. Without it "far from everywhere" and "the point cloud was empty"
would look the same.

The difference reaches all the way into the table: ``EVENTS.area_source``
tells an observation from an estimate and ``snap_distance`` gives the
estimate's distance. Without them the report would present a callout picked up
from 600 units away as being as certain as the thrower's own area.

The module is pure: no files, no demoparser2, no settings. The game's own
class names (``CSmokeGrenadeProjectile``) do not appear here -- the adapter
translates them before the call, so that this logic stays testable with
hand-built trajectories. The same goes for the point cloud: the adapter reads
the ticks, this module reduces them into a grid.
"""

from __future__ import annotations

import math

import polars as pl

__all__ = [
    "TRAJECTORY_COLUMNS",
    "ENDPOINT_COLUMNS",
    "CLOUD_OBSERVATION_COLUMNS",
    "CLOUD_CELL_COLUMNS",
    "NEAREST_POINT_COLUMNS",
    "NEAREST_RESULT_COLUMNS",
    "THROWN",
    "DETONATE",
    "MAX_TRAJECTORY_GAP_SECONDS",
    "NEAREST_CHUNK_POINTS",
    "build_point_cloud",
    "empty_point_cloud",
    "flight_point",
    "grenade_endpoints",
    "nearest_cells",
    "trajectory_gap_ticks",
]

#: The throw's event kind (``EVENT_KINDS[0]``).
THROWN = "grenade_thrown"
#: The detonation's event kind (``EVENT_KINDS[1]``).
DETONATE = "grenade_detonate"

# The correspondence with the EVENT_KINDS list is not checked with a
# module-level assert -- it would vanish under python -O exactly when it was
# needed. The check is in the test test_utility.py.

#: The columns the trajectory table has to hold. The names are pappascout's
#: own, not demoparser2's: ``steamid`` has already been translated into
#: ``thrower_id``. ``grenade_type`` is not interpreted here at all -- it is a
#: key of the segmentation and passes through unchanged, and translating the
#: game's class name into the canonical one (``smoke``, ``flashbang``, ...) is
#: the adapter's job.
TRAJECTORY_COLUMNS: tuple[str, ...] = (
    "grenade_entity_id",
    "grenade_type",
    "thrower_id",
    "tick",
    "x",
    "y",
    "z",
)

#: The columns :func:`grenade_endpoints` returns.
#:
#: ``grenade_no`` is the trajectory's running number in the demo and **the
#: only reliable key for the pair**: ``grenade_entity_id`` is recycled -- also
#: within the same round -- so it does not identify a grenade. The number is
#: unambiguous **across the whole demo**, not only within a round: a per-round
#: running number would look unambiguous but would fail as soon as the
#: aggregation joined the utility of two rounds into one frame.
#:
#: The number **ends up in the ``EVENTS`` table as it stands** (Story 1.8): it
#: is the only column by which the throw and the detonation join, and the
#: adapter also uses it to attach the round, the side and the area to both
#: rows with the same decision.
#:
#: Shape: the numbering **starts from zero** and grows by the throw's tick. It
#: is unambiguous but not a contiguous range ``0..n-1``: a trajectory without
#: a thrower is dropped already here, and ``stages.parse`` additionally drops
#: the rows of unnumbered rounds, so the finished table has gaps. The number
#: is therefore not an index and its largest value is not the number of
#: grenades.
ENDPOINT_COLUMNS: tuple[str, ...] = (
    "grenade_no",
    "grenade_entity_id",
    "grenade_type",
    "thrower_id",
    "event_kind",
    "tick",
    "x",
    "y",
    "z",
)

#: The largest gap **in seconds** that can fall inside a trajectory with it
#: still being the same grenade.
#:
#: Not one of Ancient's 374 trajectories is broken, so zero would be enough
#: for the observation. A little slack is safer all the same: one lost tick
#: would **break** a trajectory into two grenades and invent a whole extra
#: throw-detonation pair, whereas joining two different grenades would require
#: the same id to be released and taken back into use within this time by the
#: same player with the same grenade type. An invented row is a worse error
#: than a lost one, and this limit rules it out.
#:
#: The limit is **time and not ticks**: in a 128-tick demo eight ticks would
#: be half as long a moment as in a 64-tick one, and the same flight could
#: split into two grenades merely because the server ran more densely.
MAX_TRAJECTORY_GAP_SECONDS = 0.125

#: The types of the empty result. Polars would infer a ``Null`` type from an
#: empty list, and the adapter's later handling would then fail only later on.
_ENDPOINT_SCHEMA: dict[str, pl.DataType | pl.DataTypeClass] = {
    "grenade_no": pl.Int32,
    "grenade_entity_id": pl.Int32,
    "grenade_type": pl.Utf8,
    "thrower_id": pl.Utf8,
    "event_kind": pl.Utf8,
    "tick": pl.Int32,
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
}

#: The columns the point cloud's observation table has to hold.
#:
#: The names are pappascout's own and not demoparser2's: the adapter has
#: already translated ``CCSPlayerPawn.m_szLastPlaceName`` into ``area`` and
#: ``m_lifeState`` into ``is_alive``. The coordinates are the same ``x, y, z``
#: as on the trajectories, so that :func:`flight_point` serves both and the
#: rule for a row without coordinates is not in two places.
CLOUD_OBSERVATION_COLUMNS: tuple[str, ...] = ("x", "y", "z", "area", "is_alive")

#: The columns :func:`build_point_cloud` returns -- and which, besides
#: ``map_demo_id``, are in the ``CALLOUT_CLOUD`` table.
#:
#: ``cell_x``, ``cell_y`` and ``cell_z`` are the cell's **indexes** and not
#: coordinates: the coordinate is obtained by multiplying by the cell's edge.
#: An index rather than the centre, because it is an exact integer -- the
#: centre would store the same information as a float whose rounding could
#: shift the cell.
CLOUD_CELL_COLUMNS: tuple[str, ...] = (
    "cell_x",
    "cell_y",
    "cell_z",
    "area",
    "observations",
)

#: The columns :func:`nearest_cells`'s input table has to hold. ``point_id``
#: is the caller's own key (``EVENTS.grenade_no``), which comes back in the
#: result as it stands -- the function knows nothing of grenades.
NEAREST_POINT_COLUMNS: tuple[str, ...] = ("point_id", "x", "y", "z")

#: The columns :func:`nearest_cells` returns.
NEAREST_RESULT_COLUMNS: tuple[str, ...] = ("point_id", "area", "distance")

#: The point cloud's types. The same reason as with :data:`_ENDPOINT_SCHEMA`:
#: Polars would infer a ``Null`` type from an empty list.
_CLOUD_SCHEMA: dict[str, pl.DataType | pl.DataTypeClass] = {
    "cell_x": pl.Int32,
    "cell_y": pl.Int32,
    "cell_z": pl.Int32,
    "area": pl.Utf8,
    "observations": pl.Int32,
}

#: How many points are compared against the point cloud at a time
#: (:func:`nearest_cells`).
#:
#: The comparison is a cross product: every point against every cell, and a
#: sort after it. It is exact and does not rely on a search tree, but the row
#: count is a product.
#:
#: **The chunking is not an optimisation but an upper bound.** In the measured
#: data it cuts the peak from 4.8 million rows to 2.7 million (455 detonations
#: x 10 522 cells vs. 256 x 10 522), that is by 44% -- not an order of
#: magnitude. Its value is that the peak does not **grow** with the number of
#: grenades: a demo in which 2 000 grenades are thrown fits within the same
#: bound.
#:
#: **The cost is measured, not negligible.** The whole search (cross product +
#: sort) takes 285-580 ms per demo, when the cloud holds 7 700-10 500 cells
#: and there are 373-465 detonations. That is a few per cent of the demo's
#: 6-12 second parse, but it is orders of magnitude more than nothing, and a
#: search tree would be faster -- only not as simple and not as easily proved
#: correct.
#:
#: The chunk size **does not affect** the result: the nearest cell is the same
#: regardless of which batch the point was handled in, and
#: :func:`nearest_cells` refuses duplicate keys, which could multiply at a
#: chunk boundary.
NEAREST_CHUNK_POINTS = 256


def flight_point() -> pl.Expr:
    """An expression that is true only on a real point of a trajectory.

    A grenade gets a row while it is in a player's bag as well, and then the
    coordinates are missing. The same expression has to be used in both
    directions: :func:`grenade_endpoints` keeps these rows and the adapter
    picks the bag rows from its complement. If the filters drifted apart --
    one checking only for ``null`` and the other for NaN too -- some rows
    would be in both or in neither, and the lookup of the fire grenade's type
    would search the bag among the trajectory rows.
    """
    return pl.all_horizontal(
        (pl.col(name).is_not_null() & pl.col(name).is_finite()).fill_null(False)
        for name in ("x", "y", "z")
    )


def trajectory_gap_ticks(tick_rate: float) -> int:
    """:data:`MAX_TRAJECTORY_GAP_SECONDS` in ticks at this tick rate.

    Args:
        tick_rate: The demo's tick rate.

    Returns:
        At least 1. Zero would mean that no gap is allowed at all, and then
        one lost tick would invent an extra grenade.

    Raises:
        ValueError: If the tick rate is not a positive finite number.
    """
    if not (tick_rate > 0 and math.isfinite(tick_rate)):
        raise ValueError(
            f"Tickrate {tick_rate!r} cannot be used to segment trajectories: "
            "it has to be positive and finite."
        )
    return max(1, round(MAX_TRAJECTORY_GAP_SECONDS * tick_rate))


def grenade_endpoints(
    trajectories: pl.DataFrame, *, max_gap_ticks: int
) -> tuple[pl.DataFrame, int]:
    """Reduce the trajectories to two points per grenade.

    Args:
        trajectories: The trajectory table, columns at least
            :data:`TRAJECTORY_COLUMNS`. A row per grenade per tick; rows
            without coordinates (the grenade in a player's bag) may be
            included, they are filtered out here.
        max_gap_ticks: The largest tick gap across which the trajectory is
            still the same grenade. Compute it with
            :func:`trajectory_gap_ticks` from the demo's own tick rate -- a
            fixed number of ticks would be a moment of a different length at a
            different tick rate.

    Returns:
        ``(endpoints, dropped)``.

        ``endpoints`` is a long table, columns :data:`ENDPOINT_COLUMNS`: one
        ``grenade_thrown`` row for every grenade and a ``grenade_detonate``
        row for those whose trajectory is longer than a single point. Every
        trajectory gets a ``grenade_no`` of its own, which is unambiguous
        across the whole table and **the same on both rows of the
        trajectory** -- it is the throw's and the detonation's only link. The
        numbering is stable: the same input gives the same numbers, because
        the segmentation and its sort key are deterministic.
        **A single-point trajectory produces no detonation**: it is the only
        sign readable from the trajectory itself that the grenade never flew
        -- an invented detonation at the same point would claim smoke where
        there was none.

        ``dropped`` is the number of trajectories missing a thrower. They are
        dropped entirely (the detonation too), because the row cannot be
        attributed to a team -- but their number is reported, so that utility
        does not vanish silently.

    Raises:
        ValueError: If a column is missing from the table. Without the check
            the result would be an empty table that looked like a demo without
            utility.
    """
    missing = [name for name in TRAJECTORY_COLUMNS if name not in trajectories.columns]
    if missing:
        raise ValueError(
            f"The trajectory table is missing a column: {', '.join(missing)}. "
            f"The expected columns are {', '.join(TRAJECTORY_COLUMNS)}."
        )

    flight = trajectories.select(TRAJECTORY_COLUMNS).filter(
        pl.col("grenade_entity_id").is_not_null()
        & pl.col("tick").is_not_null()
        # An empty type will not do: it is mandatory in the EVENTS contract,
        # so it would bring down a whole demo in the validation because of one
        # broken row.
        & pl.col("grenade_type").is_not_null()
        & flight_point()
    )
    if flight.is_empty():
        return pl.DataFrame(schema=_ENDPOINT_SCHEMA), 0

    runs = _aggregate_runs(flight, max_gap_ticks)

    without_thrower = runs.filter(pl.col("thrower_id").is_null())
    runs = runs.filter(pl.col("thrower_id").is_not_null())
    if runs.is_empty():
        return pl.DataFrame(schema=_ENDPOINT_SCHEMA), without_thrower.height

    # The numbering is the whole definition of the id, and two things have to
    # be true at once. **Unambiguity**: the row index runs across the whole
    # demo, so the same number cannot land on two trajectories even within the
    # same round -- which is exactly what broke the old
    # (round_no, grenade_entity_id) key. **Stability**: the sort key
    # (throw_tick, grenade_entity_id) is unambiguous, because the runs of the
    # same id are in chronological order and cannot start on the same tick.
    # The order therefore does not depend on the stability of the sort, and
    # the same demo with the same settings gives the same numbers on every run
    # -- otherwise re-parsing the archive would look like a change.
    runs = runs.sort("throw_tick", "grenade_entity_id").with_row_index(
        "grenade_no"
    )

    result = pl.concat(
        [
            _endpoint_rows(runs, THROWN, "throw"),
            _endpoint_rows(runs.filter(pl.col("points") > 1), DETONATE, "detonate"),
        ]
    )
    # The order is part of the contract: the throw always comes before its
    # detonation. The tick alone would do in a real demo, but it is not an
    # invariant -- the kind is therefore an explicit key and does not rely on
    # the alphabetical order of the strings, in which "grenade_detonate" would
    # come before "grenade_thrown".
    result = result.sort(
        "grenade_no",
        pl.col("event_kind").replace_strict({THROWN: 0, DETONATE: 1}, return_dtype=pl.Int8),
        "tick",
    )

    return result.select(ENDPOINT_COLUMNS).cast(_ENDPOINT_SCHEMA), without_thrower.height


def empty_point_cloud() -> pl.DataFrame:
    """An empty point cloud with the contract's types.

    Empty is a **valid result**, not an error: a demo from which not one
    alive row on a named area was obtained is genuinely cloudless. The empty
    table matching the contract still has to be built from the types rather
    than from an empty list -- Polars would infer a ``Null`` type from the
    latter, and the write would fail only at the archive.
    """
    return pl.DataFrame(schema=_CLOUD_SCHEMA)


def build_point_cloud(
    observations: pl.DataFrame, *, grid_units: int
) -> pl.DataFrame:
    """Reduce the demo's ticks into a grid: where players stood and which area
    that is.

    The grid is **the demo's own**, not a per-map archive table. The reason is
    reproducibility: an accumulating table would give the same demo a
    different result depending on what other demos happen to be in the
    archive, and ``params_hash`` could not cover that.

    The cell's **area is the mode** and not the first observation: at the edge
    of a cell there are always a few rows from the neighbouring area, and the
    first row would depend on the order demoparser2 gave the ticks in. A tie
    is settled by the alphabetical order of the area's name, so that the same
    demo always gives the same cloud.

    Args:
        observations: A row per (player, tick), columns at least
            :data:`CLOUD_OBSERVATION_COLUMNS`. Dead, arealess and
            coordinateless rows may be included -- they are filtered out here,
            so that the filtering rule is in one place.
        grid_units: The cell's edge in the game's units
            (``[parse].callout_grid_units``).

    Returns:
        A table with the columns :data:`CLOUD_CELL_COLUMNS`, ordered by the
        cell's coordinates. ``observations`` is **all** of the cell's
        observations, not only the winning area's -- it says how strongly the
        cell has been seen.

    Raises:
        ValueError: If a column is missing or ``grid_units`` is not a positive
            finite number. Without the check the result would be an empty
            cloud that looked like a demo in which nobody moved.
    """
    if not (grid_units > 0 and math.isfinite(grid_units)):
        raise ValueError(
            f"The cell size {grid_units!r} cannot be used for a point cloud: "
            "it has to be positive and finite."
        )
    missing = [
        name for name in CLOUD_OBSERVATION_COLUMNS if name not in observations.columns
    ]
    if missing:
        raise ValueError(
            f"The point cloud's observation table is missing a column: "
            f"{', '.join(missing)}. "
            f"The expected columns are {', '.join(CLOUD_OBSERVATION_COLUMNS)}."
        )

    usable = observations.select(CLOUD_OBSERVATION_COLUMNS).filter(
        pl.col("is_alive").fill_null(False)
        # An unnamed area will not do for the cloud: a cell whose name is "no
        # name" would name a detonation empty and would still look like a hit
        # -- that is, the row would not be distinguishable from one where no
        # area was obtained at all.
        #
        # **An empty name and a name of nothing but spaces are the same thing
        # as null.** The adapter already turns the game's empty string into
        # null, but the rule is here rather than there: this function is
        # public and its contract is "an arealess observation does not reach
        # the cloud". If the condition were only in the adapter, another
        # caller would get a cell named ``" "``.
        & (pl.col("area").str.strip_chars().str.len_chars() > 0).fill_null(False)
        & flight_point()
    )
    if usable.is_empty():
        return empty_point_cloud()

    cells = usable.select(
        (pl.col("x") // grid_units).cast(pl.Int32).alias("cell_x"),
        (pl.col("y") // grid_units).cast(pl.Int32).alias("cell_y"),
        (pl.col("z") // grid_units).cast(pl.Int32).alias("cell_z"),
        pl.col("area"),
    )
    # Two phases: first (cell, area) -> observations, then the area with the
    # most observations per cell. The sort is part of the answer and not a way
    # of presenting it: it is the only thing that makes the mode
    # deterministic on a tie.
    per_area = cells.group_by("cell_x", "cell_y", "cell_z", "area").len()
    return (
        per_area.sort(
            ["cell_x", "cell_y", "cell_z", "len", "area"],
            descending=[False, False, False, True, False],
        )
        .group_by("cell_x", "cell_y", "cell_z", maintain_order=True)
        .agg(
            pl.col("area").first(),
            pl.col("len").sum().cast(pl.Int32).alias("observations"),
        )
        .sort("cell_x", "cell_y", "cell_z")
        .select(CLOUD_CELL_COLUMNS)
        .cast(_CLOUD_SCHEMA)
    )


def nearest_cells(
    points: pl.DataFrame,
    cloud: pl.DataFrame,
    *,
    grid_units: int,
    z_weight: float,
    z_tolerance_units: float,
    max_units: float | None,
) -> pl.DataFrame:
    """Name every point after the area of the nearest point-cloud cell.

    The distance is weighted::

        d = sqrt(dx^2 + dy^2 + (z_weight * max(0, |dz| - z_tolerance))^2)

    **Why a tolerance.** Without a tolerance a vertical difference costs
    something even when it is entirely normal: the point cloud records a
    player's position, but a grenade detonates anywhere between the floor and
    a head -- smoke in the air, a molotov on the floor. Measured, raising the
    weight without a tolerance *worsens* the result (the median 20 -> 30 on
    Ancient, 20 -> 31 on Nuke), and when a player's height is subtracted from
    the z difference before the weighting, the median drops to 15 and to 14.

    The tolerance is **symmetric**: freedom upwards only raises the median
    15 -> 17 and 14 -> 19 without improving the coverage.

    **Why a weight at all.** Nuke is layered: the cell below is right next
    door seen from above but in a different area. Without a weight the smoke
    upstairs **gets** the area downstairs -- measured, 38 detonations on
    ``Nuke_vs_imuaijat`` and 25 on the other Nuke demo.

    **Why the weight is 1 and not more.** A weight of 1 is enough: zero areas
    from the wrong floor on both Nuke demos. Every weight larger than that
    costs coverage without buying anything -- 99.0% at weight 1, 98.8% at
    weight 2, 97.4% at weight 3 -- and the median does not move at all.

    The cell's representative is its **centre**, not the mean of the
    observations: the mean would move depending on where inside the cell the
    players happened to place themselves, and the grid would no longer be
    regular.

    Args:
        points: The points to be named, columns :data:`NEAREST_POINT_COLUMNS`.
            ``point_id`` is the caller's own key, which comes back as it
            stands.
        cloud: The point cloud, columns :data:`CLOUD_CELL_COLUMNS`.
        grid_units: The same cell edge the cloud was built with.
        z_weight: The weighting factor for the vertical difference after the
            tolerance.
        z_tolerance_units: The vertical difference that is free (a player's
            height).
        max_units: The maximum distance the area may come from within.
            ``None`` or non-finite = no threshold in use, in which case **no
            area is given at all**. That is the honest value of an
            uncalibrated setting: the nearest cell is always found, so naming
            without a threshold would claim an area for a detonation that
            happened far from everything.

    Returns:
        A row per input point, columns :data:`NEAREST_RESULT_COLUMNS`.

        ``distance`` is **always** the nearest cell's distance, also when it
        exceeds the threshold -- that is exactly what tells the case "far from
        everything" apart from "the cloud was empty", where it is ``null``.
        ``area`` is given only within the threshold.

    Raises:
        ValueError: If a column is missing or a parameter of the weighting is
            not a finite non-negative number.
    """
    for name, value in (
        ("z_weight", z_weight),
        ("z_tolerance_units", z_tolerance_units),
    ):
        if not (value >= 0 and math.isfinite(value)):
            raise ValueError(
                f"{name} is {value!r}, which cannot be used to weight the "
                "distance: it has to be finite and not negative."
            )
    missing = [name for name in NEAREST_POINT_COLUMNS if name not in points.columns]
    if missing:
        raise ValueError(
            f"The table of points to be named is missing a column: "
            f"{', '.join(missing)}. "
            f"The expected columns are {', '.join(NEAREST_POINT_COLUMNS)}."
        )
    missing = [name for name in CLOUD_CELL_COLUMNS if name not in cloud.columns]
    if missing:
        raise ValueError(
            f"The point cloud is missing a column: {', '.join(missing)}. "
            f"The expected columns are {', '.join(CLOUD_CELL_COLUMNS)}."
        )
    # ``point_id`` is a key, and the final left join would **multiply** the
    # row if the same key occurred twice in different chunks. The result would
    # then be longer than the input, and the caller would get the same grenade
    # twice in the table without anything failing.
    duplicates = points.height - points["point_id"].n_unique()
    if duplicates:
        raise ValueError(
            f"The key point_id of the points to be named is not unambiguous: "
            f"{duplicates} rows are duplicates. The result would multiply in "
            "the join, that is, the same point would come back more than once."
        )

    empty = points.select(
        pl.col("point_id"),
        pl.lit(None, dtype=pl.Utf8).alias("area"),
        pl.lit(None, dtype=pl.Float64).alias("distance"),
    )
    if points.is_empty() or cloud.is_empty():
        return empty

    # A point without coordinates cannot get a distance -- and neither may it
    # drop out: the row is in the table in any case, and a missing result is
    # its honest content.
    locatable = points.filter(flight_point())
    if locatable.is_empty():
        return empty

    centers = cloud.select(
        ((pl.col("cell_x").cast(pl.Float64) + 0.5) * grid_units).alias("_cx"),
        ((pl.col("cell_y").cast(pl.Float64) + 0.5) * grid_units).alias("_cy"),
        ((pl.col("cell_z").cast(pl.Float64) + 0.5) * grid_units).alias("_cz"),
        pl.col("area").alias("_area"),
    )
    vertical = (
        pl.max_horizontal(
            (pl.col("z").cast(pl.Float64) - pl.col("_cz")).abs() - z_tolerance_units,
            pl.lit(0.0),
        )
        * z_weight
    )
    distance = (
        (pl.col("x").cast(pl.Float64) - pl.col("_cx")) ** 2
        + (pl.col("y").cast(pl.Float64) - pl.col("_cy")) ** 2
        + vertical**2
    ).sqrt()

    # The cross product between the points and the cells is exact and simple,
    # but it grows as a product: 456 detonations x 10 500 cells is 4.8 million
    # rows. The points are therefore handled in chunks, so that the memory
    # peak is the chunk size times the cells rather than the whole demo times
    # the cells.
    best_frames: list[pl.DataFrame] = []
    for offset in range(0, locatable.height, NEAREST_CHUNK_POINTS):
        chunk = locatable.slice(offset, NEAREST_CHUNK_POINTS)
        best_frames.append(
            chunk.join(centers, how="cross")
            .with_columns(distance.alias("_d"))
            # The sort is part of the answer: two equally distant cells in
            # different areas are settled by the alphabetical order of the
            # name, so that the same demo gives the same area on every run.
            .sort(["point_id", "_d", "_area"])
            .group_by("point_id", maintain_order=True)
            .agg(pl.col("_area").first(), pl.col("_d").first())
        )
    best = pl.concat(best_frames)

    inside = (
        pl.col("_d") <= max_units
        if max_units is not None and math.isfinite(max_units)
        else pl.lit(False)
    )
    return (
        points.select("point_id")
        .join(best, on="point_id", how="left")
        .select(
            pl.col("point_id"),
            pl.when(inside).then(pl.col("_area")).otherwise(None).alias("area"),
            pl.col("_d").cast(pl.Float64).alias("distance"),
        )
    )


# -- Internal -----------------------------------------------------------------


def _aggregate_runs(flight: pl.DataFrame, max_gap_ticks: int) -> pl.DataFrame:
    """Cut the trajectory into contiguous runs and condense each into
    endpoints.

    A run changes when the id, the thrower or the grenade type changes, or
    when a gap larger than ``max_gap_ticks`` is left in the ticks.
    ``ne_missing`` and not ``!=``: an empty thrower is as good a value in a run
    as any other, and ``!=`` would return ``null`` for it, so the run boundary
    would go unnoticed.

    The sort key is ``(id, tick)`` **followed by every remaining column**. The
    first two determine the order; the rest are mere tie-breakers, and they do
    not move a single row in a situation where the pair is unambiguous.

    They are there for determinism. The run boundary is read from the
    neighbouring rows, so it depends on the result of the sort, and Polars'
    sort is not stable: two rows with the same id and the same tick could swap
    places between runs. Then **the segmentation itself** -- not just the
    numbering -- would be undetermined, and ``grenade_no``'s stability would
    be an empty promise. With every column in the key, the order is a function
    of the rows' **content**: two exactly identical rows are interchangeable,
    so the result is the same regardless of the order demoparser2 gave the
    rows in.
    """
    tie_break = [
        name
        for name in TRAJECTORY_COLUMNS
        if name not in ("grenade_entity_id", "tick")
    ]
    frame = flight.sort("grenade_entity_id", "tick", *tie_break)
    run_start = (
        pl.col("grenade_entity_id").ne_missing(pl.col("grenade_entity_id").shift(1))
        | pl.col("thrower_id").ne_missing(pl.col("thrower_id").shift(1))
        | pl.col("grenade_type").ne_missing(pl.col("grenade_type").shift(1))
        | ((pl.col("tick") - pl.col("tick").shift(1)) > max_gap_ticks)
    )
    frame = frame.with_columns(run_start.fill_null(True).cum_sum().alias("_run"))

    return frame.group_by("_run", maintain_order=True).agg(
        pl.col("grenade_entity_id").first(),
        pl.col("grenade_type").first(),
        pl.col("thrower_id").first(),
        pl.col("tick").first().alias("throw_tick"),
        pl.col("x").first().alias("throw_x"),
        pl.col("y").first().alias("throw_y"),
        pl.col("z").first().alias("throw_z"),
        pl.col("tick").last().alias("detonate_tick"),
        pl.col("x").last().alias("detonate_x"),
        pl.col("y").last().alias("detonate_y"),
        pl.col("z").last().alias("detonate_z"),
        pl.len().alias("points"),
    )


def _endpoint_rows(runs: pl.DataFrame, event_kind: str, prefix: str) -> pl.DataFrame:
    """Pick either the throw rows or the detonation rows out of the runs."""
    return runs.select(
        pl.col("grenade_no"),
        pl.col("grenade_entity_id"),
        pl.col("grenade_type"),
        pl.col("thrower_id"),
        pl.lit(event_kind, dtype=pl.Utf8).alias("event_kind"),
        pl.col(f"{prefix}_tick").alias("tick"),
        pl.col(f"{prefix}_x").alias("x"),
        pl.col(f"{prefix}_y").alias("y"),
        pl.col(f"{prefix}_z").alias("z"),
    )
