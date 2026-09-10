"""``parse`` -- the pipeline's first stage: seven tables out of a demo.

The stage reads one demo from behind the port and writes into the archive
``parsed/<map_demo_id>/rounds.parquet``, ``.../ticks.parquet``,
``.../events.parquet``, ``.../lineups.parquet``, ``.../deaths.parquet``,
``.../callouts.parquet``, ``.../match.parquet`` and their shared manifest.

``rounds`` is two rows for every **played** round, one for each team, and
every value is *observed* from the demo: money and equipment value at the end
of buy time, the equipment value at the start of the round, the survivors,
the winner and the win reason. The round type, the loss count and the other
derived values are not born until the ``classify`` stage, which recomputes
them on every run.

``ticks`` is a row per (player, round, sample point): area, coordinates and
being alive at a few moments from the start of the round
(``[parse].snapshot_seconds``) and at the moment of first contact. All ten
players are recorded at every sample point with an ``is_alive`` flag;
filtering out the dead and aggregating are the work of later stages (AD-10).

``events`` is a row per utility event: the throw and the detonation are two
rows joined by ``grenade_no``, the trajectory's demo-local id. Two demos'
tables are joined on the pair ``(map_demo_id, grenade_no)``. Utility is
measured **from throws, not from purchases** -- utility gets dropped, so the
buyer and the thrower can be different players. **An empty event table is a
valid result**, unlike an empty rounds or sample-point table: a demo may hold
no utility thrown at all, but it always holds played rounds and setups.

``lineups`` is a row per (lineup, player): the player's name and their clan
name. It is an **identity table**, not a round table -- the name is the same
for the whole map, so it has no ``round_no`` and its rows are not dropped
along with the knife round. The name is an observation: a missing clan is
``null``, and the report says the absence out loud instead of inventing a
substitute.

``deaths`` is a row per death: the victim and the attacker, both with their
areas and coordinates. A death has **two actors**, so it does not fit the
single-actor shape of the ``EVENTS`` table -- and its area is not a derived
value but an observation from the same event. A death without an attacker (a
fall, the bomb) is a genuine case: the attacker's fields are then empty and
the row is not dropped.

``callouts`` is a row per point-cloud cell: a grid, assembled from the demo's
own ticks, of where the players have stood on the map and which area is at
each spot. It is the **source of the ``events`` table's detonation areas**,
and it is written for exactly that reason: a derived area can be checked
against the demo only if what it was derived from is kept. The same principle
as with the ``ROUNDS.buy_end_tick`` column -- a measurement is not presented
without the thing it was read from. An empty point cloud is a valid result:
every detonation area is then ``null``, the run does not fail, and the reason
is given in the run's summary.

``match`` is **one row**: the match's own observations, at present the map
name from the demo's header (``parse_header``). The map is a property of the
match and not of a round, so it is not a column of the rounds table where the
same value would repeat on every row -- the same grounds as with the lineup
table. The name is an **observation**: it is used as it stands and is not
compared against a map pool, and a missing name is ``null`` and not a
substitute. Only then does ``aggregate`` infer the name from the
``map_demo_id``.

**An empty deaths table is an error, an empty event table is not.** The
asymmetry is deliberate, and it follows from what emptiness means in each of
them. Utility can genuinely be absent: a team may leave the grenades unbought,
and "zero throws" is then an observation about the round. A death is not a
choice. A CS2 round is decided by killing or by the bomb, and the bomb kills
too; a whole match without a single death is not a match that was played. An
empty deaths table therefore always means a read error -- in practice a
renamed ``player_death`` event -- and not an observation, and as an ok result
it would stay permanently skipped on the strength of the manifest. The error
message names the adapter's own drop counters, so the reason can be read
rather than guessed.

What ends up in the tables
--------------------------
Warm-up rounds, the knife round and ``mp_restartgame`` resets **are not played
rounds**: they get no round number and do not end up in the table.
``round_raw`` is the demo's own round counter, so the skipped rounds show in
the table as a gap (Ancient: ``round_no`` 1..21 corresponds to ``round_raw``
2..22) -- their number is given in the run's summary as well. The decision is
made by one single function,
:func:`~pappascout.domain.rounds.mark_played_rounds`, which only this stage
calls.

The point cloud is an **exception, and that is intended**: its rows do not
fall away in the numbering. The cloud is a property of the map in this demo
and not an observation about a round, and the ticks of the warm-up and of the
knife round tell as much about the map as those of the played rounds. The
same rule as with the lineup table.

The same decision also bounds the sample points, the utility events and the
deaths: the adapter produces rows from every anchored round boundary, and the
stage drops the rows of unnumbered rounds at the same time as it joins
``round_no`` on the key ``round_raw``. This way the knife round produces
neither tick nor event nor death rows, and the adapter does not need to know
the numbering rule. **People really do die on the knife round**, so this very
join is the only place where those deaths fall away -- there is no separate
knife-round rule and there must not be one.

One case is settled by the adapter already: the **match restart**. It has a
freezetime anchor but no ``round_end``, and the demo's own round numbering
carries on over it by one -- so it is not a round at all, and it does not even
have a ``round_raw`` on which it could travel this far. The adapter produces
no row from it, and reports the count in its diagnostics
(``match_restarts``); the stage passes that on to the run's summary. The
number is a different thing from the count of skipped rounds mentioned above:
a skipped round is in the table without a ``round_no``, a restart is not in
the table at all.

The checks before the write
---------------------------
The stage validates both the table it reads and the table it writes (AD-2),
and two further things that the schema alone does not see:

* :func:`~pappascout.domain.rounds.check_win_reasons` -- the CS2 rule about
  who can win a round in what way. A violation nearly always means the sides
  have gone the wrong way round.
* The row count is exactly two per round. One row or three would pass the
  schema but distort every later per-team sum.

Rerunning
---------
The manifest's ``params_hash`` is computed from the ``[parse]`` section
**only**, and ``tool_versions`` holds demoparser2 alone. Changing a
``[thresholds]`` value therefore cannot invalidate the parse -- that is the
whole point of AD-3's settings partition, and this stage does not even see
the other sections.

**A schema change forces a reparse.** The manifest's parameter hash does not
move when a table gains a new column or a wholly new table appears, so a
matching manifest is not enough on its own as the condition for skipping: the
stage also requires that **every** table file this version produces is in
place (``expected_outputs``) and that they still match the contract in force
(:func:`_schema_is_current`). An archived demo missing ``lineups.parquet`` is
therefore not up to date, and it is reparsed without the ``--force`` flag.

The demo's digest is read from its own ``.meta.json`` file. If there is none
(a demo copied into the archive by hand), the id is the file's **size and
modification time**: a sha256 over 233 MB on every run would be slower than
the parse itself. The modification time is included because size alone does
not notice a changed demo. If the sync client changes the timestamp, the
consequence is one needless reparse -- a less dangerous mistake than a stale
result. The manifest can always be overridden with the ``force`` flag.

The error policy
----------------
The rule is one: **a valid result is never overwritten by a failure.** If the
parse fails and the archive already holds intact tables and their ``ok``
manifest, everything is left untouched. Otherwise a ``parse_failed`` manifest
is written, which both blocks the skip and gives the reason. A partial table
is born in neither case, because the writes are atomic and happen only after
all the checks. The manifest is written last, so an interrupted run shows up
on the next occasion as a missing result and not as an up-to-date one.
"""

from __future__ import annotations

import json
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import polars as pl

from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoParser,
    DemoTables,
)
from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    DEMO_SUFFIXES,
    ArchivePaths,
    parsed_manifest,
    parsed_table,
    safe_component,
)
from pappascout.constants import weapon_classification_digest
from pappascout.domain.models import ParseSettings
from pappascout.domain.rounds import check_win_reasons, mark_played_rounds
from pappascout.domain.sampling import FIRST_CONTACT_SAMPLE
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.domain.utility import DETONATE, THROWN
from pappascout.errors import DemoUnavailable, ParseError, SchemaError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TABLE",
    "TICKS_TABLE",
    "EVENTS_TABLE",
    "LINEUPS_TABLE",
    "DEATHS_TABLE",
    "CALLOUTS_TABLE",
    "TOOLS",
    "run",
    "resolve_demo",
    "map_demo_id_from_path",
    "default_parser",
]

@dataclass(frozen=True)
class _ParsedTables:
    """One demo's finished, checked tables and their drop counts.

    A dataclass and not a tuple: the return value holds five frames and three
    bare integers, and two adjacent ``int`` values can be swapped with each
    other without any type check noticing. The same pattern as with
    :class:`~pappascout.adapters.protocols.DemoTables` and the adapter's
    counter classes -- and the next table will not grow the tuple to nine.

    Attributes:
        skipped_rounds: The unnumbered round boundaries (warm-up, the knife
            round).
        unnumbered_utility: The throws dropped from unnumbered rounds.
        unnumbered_deaths: The deaths dropped the same way. Never empty in a
            league demo: people really do die on the knife round.
    """

    rounds: pl.DataFrame
    ticks: pl.DataFrame
    events: pl.DataFrame
    lineups: pl.DataFrame
    deaths: pl.DataFrame
    callouts: pl.DataFrame
    match: pl.DataFrame
    skipped_rounds: int
    unnumbered_utility: int
    unnumbered_deaths: int


STAGE = "parse"
TABLE = "rounds"
TICKS_TABLE = "ticks"
EVENTS_TABLE = "events"
LINEUPS_TABLE = "lineups"
DEATHS_TABLE = "deaths"
CALLOUTS_TABLE = "callouts"
MATCH_TABLE = "match"

#: The tools whose version changes this stage's result (the manifest module's
#: rule). Pappascout's own version is not recorded: a patch release must not
#: force a reparse of the whole archive.
TOOLS = ("demoparser2",)

#: The errors for which a per-unit status is recorded in the manifest (AD-9).
_RECORDED_ERRORS = (ParseError, SchemaError, OSError, pl.exceptions.PolarsError)


def default_parser(settings: ParseSettings) -> DemoParser:
    """The production demoparser2 implementation.

    The import is inside the function so that importing this module does not
    load demoparser2 -- the stage itself knows only the port, and the tests
    hand it a fake.

    Args:
        settings: The ``[parse]`` section. The parameters of the first-contact
            rule are settings and not code, so the adapter is given them in
            the call rather than reading them itself.
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    return Demoparser2Adapter(
        exclude_weapons=settings.first_contact_exclude_weapons,
        fallback_death=settings.first_contact_fallback_death,
        area_snap_units=settings.area_snap_units,
        buy_window_seconds=settings.buy_window_seconds,
        callout_grid_units=settings.callout_grid_units,
        callout_z_weight=settings.callout_z_weight,
        callout_z_tolerance_units=settings.callout_z_tolerance_units,
    )


def map_demo_id_from_path(path: Path) -> str:
    """Infer the ``map_demo_id`` from the demo file's name.

    FACEIT's file name is the id as it stands, so the suffix is stripped and
    the rest is checked to be acceptable as part of a path.
    """
    name = Path(path).name
    for suffix in sorted(DEMO_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    else:
        name = Path(name).stem
    return safe_component(name, "map_demo_id")


def resolve_demo(archive: ArchivePaths, target: str) -> tuple[str, Path]:
    """Interpret the target the user gave as a file and an id.

    The target may be either a path to a demo file or a bare ``map_demo_id``,
    in which case the demo is looked for in **three places, in this order**:

    1. ``[project].demos_root`` -- the local directory of downloaded demos, if
       the setting is in use,
    2. the archive's ``demos/`` -- that is where downloads went before the
       setting existed,
    3. the archive's ``import/`` -- demos imported by hand **under the
       canonical name** (FACEIT's own ``...-1-1.dem`` does not match here; see
       :meth:`~pappascout.archive.paths.ArchivePaths.demo_dirs`).

    **The order is not arbitrary.** Item 2 is there because taking the local
    directory into use must not make already downloaded demos invisible; item
    3 because an imported demo behaves in the pipeline exactly like a
    downloaded one -- no stage tells them apart. The first two come from
    :meth:`~pappascout.archive.paths.ArchivePaths.find_demo`, which is the
    same reader as ``fetch``'s idempotence check: two different search orders
    for the same file would mean that one of them downloads what the other
    already finds.

    Raises:
        DemoUnavailable: If the demo is not found. The message says where the
            search looked.
    """
    candidate = Path(target).expanduser()
    if candidate.is_file():
        return map_demo_id_from_path(candidate), candidate

    map_demo_id = safe_component(target, "map_demo_id")
    found = archive.find_demo(map_demo_id)
    if found is not None:
        return map_demo_id, found

    searched = [
        str(directory / f"{map_demo_id}{suffix}")
        for directory in archive.demo_dirs()
        for suffix in DEMO_SUFFIXES
    ]
    listing = "\n".join(f"    {p}" for p in searched)
    raise DemoUnavailable(
        f"Demo {map_demo_id} was not found.\n"
        "I looked in these paths:\n"
        f"{listing}\n"
        "Copy the demo into the archive's import directory, or give the "
        "file's path straight to the command."
    )


def _demo_fingerprint(archive: ArchivePaths, map_demo_id: str, demo_path: Path) -> str:
    """The demo's id for the manifest's input list.

    Primarily the ``sha256`` of the ``demos/<id>.meta.json`` file; otherwise
    the file's size and modification time. The digest is not recomputed from
    the demo (see the module docstring).

    Raises:
        DemoUnavailable: If the file's details cannot be read. A shared
            fallback constant would be dangerous: two different unreadable
            demos would get the same id, and then one's result would look
            like an up-to-date result for the other.
    """
    # **A search and not a write path.** The demo may be in the local
    # directory or in the archive, and the metadata file is always next to
    # its demo; ``demo_meta`` on its own would look only where a new download
    # would go.
    meta_path = archive.find_demo_meta(map_demo_id)
    if meta_path is not None:
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        sha = meta.get("sha256")
        if isinstance(sha, str) and sha:
            return sha
    try:
        stat = demo_path.stat()
    except OSError as exc:
        raise DemoUnavailable(
            f"The details of demo {demo_path} could not be read: {exc}\n"
            "Check that the file is not a cloud placeholder in a synchronised "
            "folder (open it once in File Explorer) and that the path is "
            "right."
        ) from exc
    return f"size-{stat.st_size}-mtime-{stat.st_mtime_ns}"


def _params_hash(settings: ParseSettings) -> str:
    """Parameter hash: ``[parse]`` **and** the weapon classification digest.

    The kit counter reads the weapon list from the code and not from the
    settings, so the section's hash alone would leave a change to the list
    invisible: the archive's old counter would stay in force and nothing
    would say so. A digest of the classification's contents forces a reparse
    without anyone having to remember to raise a version number.

    The sections are in a **two-level structure** and not siblings in the same
    dictionary: as a sibling key the digest could be masked by a ``[parse]``
    setting of the same name, and that would have to be fended off with a
    guard that nothing can trip. Nesting makes the collision impossible at
    the level of the structure.
    """
    return compute_params_hash(
        {
            "parse": settings.model_dump(mode="json"),
            "weapon_classification": weapon_classification_digest(),
        }
    )


def _check_port_columns(
    df: pl.DataFrame,
    expected_columns: tuple[str, ...],
    table_name: str,
    contract: str,
) -> None:
    """Check the table the adapter produced before going any further.

    The requirement is the **exact set of columns**, not a subset: an extra
    column means that the port's implementation and the contract have drifted
    apart, and it would either be carried along silently or fail only in the
    domain layer.
    """
    received = set(df.columns)
    expected = set(expected_columns)
    missing = sorted(expected - received)
    extra = sorted(received - expected)
    if not missing and not extra:
        return
    parts = []
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    if extra:
        parts.append(f"extra: {', '.join(extra)}")
    raise SchemaError(
        f"The demo port returned a {table_name} that breaks the contract -- "
        f"{'; '.join(parts)}.\n"
        f"The port's contract is {contract} in adapters/protocols.py."
    )


def _check_two_rows_per_round(df: pl.DataFrame) -> None:
    """Every round must have exactly two rows.

    The table is long: one row for each team. A third row or a missing pair
    would pass the schema but would double or halve the per-team sums in all
    later analysis.
    """
    round_count = df["round_no"].n_unique()
    if df.height == 2 * round_count:
        return
    deviating = (
        df.group_by("round_no")
        .len()
        .filter(pl.col("len") != 2)
        .sort("round_no")
        .head(5)
    )
    raise SchemaError(
        f"The rounds table has {df.height} rows for {round_count} rounds; "
        "there should be exactly two rows per round (one for each "
        "team).\n"
        f"The deviating rounds: {deviating.to_dicts()}"
    )


def _check_player_counters(df: pl.DataFrame) -> None:
    """Make sure the player counters stay within their divisor and nested.

    ``validate`` checks the columns and the types but not the values, and the
    values are the whole promise of these two columns. Two invariants, which
    follow from both being counted **from the same set of players at the same
    tick**:

    * ``0 <= counter <= players_buy_end`` -- exceeding it would mean two
      different divisors on the same row, and the report would show "6/5".
    * ``players_armed_buy_end <= players_armored_buy_end`` -- the armed
      condition includes armour, so the armed are a **subset** of the
      armoured. Exceeding it would mean the counters are reading a different
      tick or a different set.

    The check is done **at read time** and not merely promised in the schema's
    docstring: without it an impossible number would be written into the
    archive and would come to light only in the report, if even then. Nulls
    are passed over -- they are an honest "no observation" and not a broken
    invariant.

    Raises:
        SchemaError: Names the broken invariant and at most five rows.
    """
    checks = (
        (
            (pl.col(ARMED_COLUMN) < 0)
            | (pl.col(ARMED_COLUMN) > pl.col("players_buy_end")),
            f"{ARMED_COLUMN} is outside the bounds 0..players_buy_end",
        ),
        (
            (pl.col(ARMORED_COLUMN) < 0)
            | (pl.col(ARMORED_COLUMN) > pl.col("players_buy_end")),
            f"{ARMORED_COLUMN} is outside the bounds 0..players_buy_end",
        ),
        (
            pl.col(ARMED_COLUMN) > pl.col(ARMORED_COLUMN),
            f"{ARMED_COLUMN} > {ARMORED_COLUMN}, although the armed "
            "condition includes armour -- the armed must be a subset of the "
            "armoured",
        ),
    )
    for condition, complaint in checks:
        broken = df.filter(condition.fill_null(False))
        if broken.is_empty():
            continue
        rows = broken.select(
            "round_no", "side", "players_buy_end", ARMED_COLUMN, ARMORED_COLUMN
        ).head(5)
        raise SchemaError(
            f"In the rounds table {complaint}: {broken.height} rows.\n"
            f"The first ones: {rows.to_dicts()}"
        )


def _check_grenade_key(events: pl.DataFrame) -> None:
    """Make sure ``(map_demo_id, grenade_no, event_kind)`` identifies a row.

    ``validate`` checks the columns and the types but not the key, and the key
    is the whole promise of this table: aggregation joins utility to rounds on
    the assumption that the join does not multiply rows. A broken key would
    pass the schema and would show only in the report's numbers, as smoke
    counted twice -- exactly the silent defect that ``grenade_no`` exists for
    in the first place.

    ``map_demo_id`` is in the key because ``grenade_no`` runs **within a
    demo**: ``aggregate``, which joins two demos' tables, needs the pair. In
    one demo's table it is a constant and does not change the result, but the
    claim has to be written in the form in which it is used.

    Raises:
        SchemaError: If the number is missing, or if the same key occurs
            twice.
    """
    if events.is_empty():
        return

    missing = int(events["grenade_no"].null_count())
    if missing:
        raise SchemaError(
            f"The event table has {missing} rows without a grenade_no.\n"
            "The number is the only tie between the throw and the detonation; "
            "without it the row cannot be joined to anything."
        )

    key = events.select("map_demo_id", "grenade_no", "event_kind")
    if key.height == key.unique().height:
        return
    duplicates = (
        events.group_by("map_demo_id", "grenade_no", "event_kind")
        .len()
        .filter(pl.col("len") > 1)
        .sort("grenade_no")
        .head(5)
    )
    raise SchemaError(
        "The event table's key (map_demo_id, grenade_no, event_kind) is not "
        f"unique: {key.height - key.unique().height} rows are "
        "duplicates.\n"
        f"The first repeated keys: {duplicates.to_dicts()}"
    )


def _round_stats(df: pl.DataFrame, skipped_rounds: int = 0) -> dict[str, object]:
    """The rounds table's numbers."""
    if df.is_empty():
        stats: dict[str, object] = {
            "rounds": 0,
            "rows": 0,
            "max_round_no": 0,
            "skipped_rounds": skipped_rounds,
            "no_freeze_end": 0,
        }
    else:
        stats = {
            "rounds": int(df["round_no"].n_unique()),
            "rows": int(df.height),
            "max_round_no": int(df["round_no"].max() or 0),
            "skipped_rounds": skipped_rounds,
            "no_freeze_end": int(
                df.filter(pl.col("status") == "no_freeze_end")["round_no"].n_unique()
            ),
        }
    # From an empty table as well: the other numbers are returned as zeroes,
    # so a missing counter would make an empty run look the same as a version
    # that has no counter at all.
    stats.update(_armed_stats(df))
    stats.update(_armored_stats(df))
    return stats


def _column_distribution(df: pl.DataFrame, column: str) -> tuple[dict[int, int], int]:
    """One counter column's value distribution and its count of missing rows.

    Shared between the two counters on purpose: the armed and the armoured are
    **different observations from the same tick**, and two separate
    computations could drift apart from each other -- one would count a round
    without an anchor as zero and the other as missing, and the numbers could
    no longer be compared.
    """
    counts = (
        df.filter(pl.col(column).is_not_null()).group_by(column).len().sort(column)
    )
    return (
        {int(value): int(rows) for value, rows in zip(counts[column], counts["len"])},
        int(df[column].null_count()),
    )


def _armed_stats(df: pl.DataFrame) -> dict[str, object]:
    """The kit counter's **value distribution** from the rounds table.

    The distribution is given with the run because the counter is the one
    observation whose correctness can be checked only by looking at it: a
    wrong rule or a stale weapon list would produce a table that passes every
    schema check.

    The distribution specifically, not the extremes: 41 rows of zero and one
    five would give ``0-5``, which looks healthy. ``{0: 41, 5: 1}`` does not.

    Returns:
        ``armed_distribution`` (value -> number of rows, ordered by value) and
        ``armed_missing`` (the rows where the observation is missing).
    """
    distribution, missing = _column_distribution(df, ARMED_COLUMN)
    return {"armed_distribution": distribution, "armed_missing": missing}


def _armored_stats(df: pl.DataFrame) -> dict[str, object]:
    """The armour counter's **value distribution** from the rounds table.

    A line of its own and not an extension of the armed distribution: they are
    different observations and they differ most on the pistol round, where the
    armed are in practice 0. One line built from two numbers would hide the
    very difference for which there are two counters.

    The distribution doubles as a self-check: if the armour counter drifted
    into reading the armed condition, the distributions would be identical --
    and that shows in the run's output at once and not only in the report.

    Returns:
        ``armored_distribution`` (value -> number of rows) and
        ``armored_missing`` (the rows where the observation is missing).
    """
    distribution, missing = _column_distribution(df, ARMORED_COLUMN)
    return {"armored_distribution": distribution, "armored_missing": missing}


def _buy_window_stats(
    df: pl.DataFrame, diagnostics: object
) -> dict[str, object]:
    """Buy-window numbers **from played rounds**, not from every boundary.

    The adapter gives the truncations as ``round_raw`` numbers rather than as
    finished counts, and the reason is the knife round: it gets a
    ``round_raw`` of its own but is not a round and does not end up in the
    table. A count computed by the adapter would include it, and it could no
    longer be subtracted out -- the user would see "13 truncations" for a
    table that holds 12. The same goes for the distribution of measurement
    moments: the knife round is decided before the window ends, so including
    it would pull the distribution's lower bound down to a fraction of a
    second and would claim a measurement that is not in the round list.

    The distribution of measurement moments is computed entirely here, straight
    from the table being written: it is the only visible form of the
    ``buy_end_tick`` column, and if the column and the output could drift
    apart, checkability would be only apparent.

    Args:
        df: The finished rounds table, played rounds only.
        diagnostics: The port's diagnostics, or ``None``.

    Returns:
        Only the keys that can be computed. A missing key means a skipped run,
        and ``cli`` then leaves the line out -- "none at all" would be a claim
        that nothing supports.
    """
    if diagnostics is None:
        return {}

    played = set(df["round_raw"].to_list()) if not df.is_empty() else set()
    cuts = [
        (round_raw, missed)
        for round_raw, missed in getattr(diagnostics, "buy_window_cuts", ()) or ()
        if round_raw in played
    ]
    unchecked = [
        round_raw
        for round_raw in getattr(diagnostics, "buy_window_unchecked_cuts", ()) or ()
        if round_raw in played
    ]

    stats: dict[str, object] = {
        "buy_window_seconds": getattr(diagnostics, "buy_window_seconds", None),
        "buy_end_offsets_s": _buy_end_offsets(df),
        "buy_window_truncated_by_death": len(cuts),
        "buy_window_purchases_after_cut": sum(missed for _, missed in cuts),
        "buy_window_cuts_unchecked": len(unchecked),
        "buy_window_rounds_with_lost_purchases": tuple(
            round_raw for round_raw, missed in cuts if missed
        ),
    }
    for name in (
        "buy_window_ticks_without_players",
        "buy_window_players_lost",
        "buy_window_sides_without_rows",
        "buy_window_refunds",
        "buy_window_stale_equipment",
    ):
        stats[name] = getattr(diagnostics, name, 0)
    return stats


def _buy_end_offsets(df: pl.DataFrame) -> tuple[float, float, float] | None:
    """Measurement moments in seconds from the anchor: ``(min, median, max)``.

    The setting promises the window's length; these three numbers say where
    the measurement actually landed. If the window is 20 s but the median is
    12 s, a death truncates the window more often than not -- and that cannot
    be seen from the setting.

    Returns:
        A triple in seconds, or ``None`` if no row has both ticks and a valid
        tick rate.
    """
    if df.is_empty():
        return None
    frame = df.filter(
        pl.col("buy_end_tick").is_not_null()
        & pl.col("freeze_end_tick").is_not_null()
        & (pl.col("tick_rate") > 0)
    )
    if frame.is_empty():
        return None
    offsets = (frame["buy_end_tick"] - frame["freeze_end_tick"]) / frame["tick_rate"]
    return float(offsets.min()), float(offsets.median()), float(offsets.max())


def _stats(
    df: pl.DataFrame,
    ticks: pl.DataFrame,
    events: pl.DataFrame,
    lineups: pl.DataFrame,
    deaths: pl.DataFrame,
    callouts: pl.DataFrame,
    match: pl.DataFrame,
    skipped_rounds: int = 0,
) -> dict[str, object]:
    """The numbers shown to the user, from the finished tables."""
    stats = _round_stats(df, skipped_rounds)
    stats.update(_tick_stats(ticks))
    stats.update(_event_stats(events))
    stats.update(_lineup_stats(lineups))
    stats.update(_death_stats(deaths))
    stats.update(_callout_stats(callouts))
    stats.update(_match_stats(match))
    return stats


def _match_stats(match: pl.DataFrame) -> dict[str, object]:
    """The match table's numbers: the map name, or its absence.

    Readable **from the finished table**, so it is computed here and not in
    the adapter -- the same rule as with sample points and the point cloud,
    and the consequence is that a skipped run gives the map as well. That is
    exactly the line's value: a skipped run is the state in which the user
    otherwise sees nothing at all about the map.

    The key is set **always**, including when the name is missing. A missing
    key and ``None`` would mean the same thing in the output, and the output
    is the only place where a lost header field shows.
    """
    if match.is_empty():
        return {"map_name": None}
    name = match["map_name"][0]
    return {"map_name": None if name is None else str(name)}


def _tick_stats(ticks: pl.DataFrame) -> dict[str, object]:
    """The sample points' numbers.

    ``sample_points`` is round x moment, not a row count: ten players' rows
    are the same sample point. ``first_contact_rounds`` says on how many
    rounds contact was found at all -- a row count on its own would not reveal
    it if the rule failed to bite.
    """
    if ticks.is_empty():
        return {
            "tick_rows": 0,
            "sample_points": 0,
            "sample_rounds": 0,
            "first_contact_rounds": 0,
        }
    contact = ticks.filter(pl.col("sample_kind") == FIRST_CONTACT_SAMPLE)
    return {
        "tick_rows": int(ticks.height),
        "sample_points": int(
            ticks.select("round_no", "sample_kind", "sample_t_s").n_unique()
        ),
        "sample_rounds": int(ticks["round_no"].n_unique()),
        "first_contact_rounds": int(contact["round_no"].n_unique()),
    }


def _event_stats(events: pl.DataFrame) -> dict[str, object]:
    """The utility events' numbers.

    Throws and detonations separately, because their **difference** is itself
    an observation: there are always a few grenades that do not go off, but a
    large difference would mean the end of the trajectory is going
    unrecognised.

    Three numbers are given about the area, because they are three different
    kinds of information and must not be bundled into one:

    ``utility_area_observed``
        The thrower's own area. An observation.
    ``utility_area_point_cloud``
        The detonation's area from the nearest cell of the demo's point
        cloud. An estimate, whose reliability ``snap_distance`` gives per row.
    ``utility_area_beyond_threshold``
        The nearest cell was found, but it is farther away than
        ``[parse].area_snap_units``. **This is the threshold's price and the
        only place where it shows.** Without a number of its own it would be
        confused with the case where the point cloud was empty -- in both the
        area is null, but only here is there a distance.

    ``utility_without_area`` is the remainder left outside these: the area is
    missing altogether.

    The last three numbers are the **detonations' coverage**, and that is
    Story 2.9's most important measure: ``utility_detonation_area_coverage``
    is the share of detonations that got an area. It is the one number that
    answers the question "how many utility rows are left without an area", and
    it is computed on every run and not once during calibration.

    The distance spread (``utility_snap_distance``) is a triple
    ``(median, p90, max)`` in the game's units, ``None`` if no detonation got
    a distance. It is the only material for choosing the threshold: the
    setting gives the bound, this says where the measurement actually landed.
    """
    if events.is_empty():
        return {
            "event_rows": 0,
            "utility_throws": 0,
            "utility_detonations": 0,
            "utility_rounds": 0,
            "utility_area_observed": 0,
            "utility_area_point_cloud": 0,
            "utility_area_beyond_threshold": 0,
            "utility_without_area": 0,
            "utility_detonation_area_coverage": None,
            "utility_snap_distance": None,
        }
    kinds = events["event_kind"]
    sources = events["area_source"]
    detonations = events.filter(pl.col("event_kind") == DETONATE)
    named = int(detonations["area"].is_not_null().sum())
    return {
        "event_rows": int(events.height),
        "utility_throws": int((kinds == THROWN).sum()),
        "utility_detonations": int(detonations.height),
        "utility_rounds": int(events["round_no"].n_unique()),
        "utility_area_observed": int((sources == "observed").sum()),
        "utility_area_point_cloud": int((sources == "point_cloud").sum()),
        "utility_area_beyond_threshold": int(
            events.filter(
                pl.col("area").is_null() & pl.col("snap_distance").is_not_null()
            ).height
        ),
        "utility_without_area": int(events["area"].null_count()),
        # A share and not just a numerator: 356 areas out of 400 is different
        # news from 356 out of 4,000, and the reader does not work it out from
        # the run's output.
        "utility_detonation_area_coverage": (
            None if detonations.is_empty() else (named, int(detonations.height))
        ),
        "utility_snap_distance": _snap_distance_spread(detonations),
    }


def _snap_distance_spread(
    detonations: pl.DataFrame,
) -> tuple[float, float, float] | None:
    """The detonations' distance spread: ``(median, p90, max)``.

    Three numbers and not one: the median says what the ordinary case is, the
    p90 where the threshold starts to bite, and the maximum how far the
    farthest detonation is from everywhere any player has stood. The maximum
    is exactly the number that shows why the threshold exists: without it "a
    nearest cell is always found" would look like coverage.

    **Every** detonation that has a distance is included -- those beyond the
    threshold as well. They are the distribution's tail, and cutting it off
    would hide the very thing being measured.

    Returns:
        A triple, or ``None`` if no detonation got a distance (an empty point
        cloud, or no detonations).
    """
    measured = detonations.filter(pl.col("snap_distance").is_not_null())
    if measured.is_empty():
        return None
    distances = measured["snap_distance"]
    return (
        float(distances.median()),
        float(distances.quantile(0.9, interpolation="nearest")),
        float(distances.max()),
    )


def _callout_stats(callouts: pl.DataFrame) -> dict[str, object]:
    """The point cloud's numbers.

    ``callout_cells`` and ``callout_areas`` are readable from the finished
    table, so they are computed here and not in the adapter -- the same rule
    as with sample points and utility.

    **The number of areas matters more than the number of cells.** The number
    of cells says only what the cell size is; the number of areas says whether
    the cloud recognised the map. Measured 2026-08-30: Ancient 18, Nuke 29,
    Anubis 28, Inferno 24. A single-digit number would mean that
    ``last_place_name`` comes back mostly empty.

    ``callout_observations`` is the sum of every cell's observations, that is,
    the number of rows that were accepted into the cloud.
    """
    if callouts.is_empty():
        return {
            "callout_cells": 0,
            "callout_areas": 0,
            "callout_observations": 0,
        }
    return {
        "callout_cells": int(callouts.height),
        "callout_areas": int(callouts["area"].n_unique()),
        "callout_observations": int(callouts["observations"].sum()),
    }


def _death_stats(deaths: pl.DataFrame) -> dict[str, object]:
    """The deaths table's numbers.

    Five numbers, five different questions. ``death_rows`` and
    ``death_rounds`` say whether any material was produced at all. The other
    three are coverage numbers, and they are separate because they mean
    different things:

    ``deaths_without_attacker``
        A death without an attacker -- a fall or the bomb. **An observation
        and not a defect**. Measured on ``Ancient_vs_kaljukostaja``: two
        ``planted_c4`` rows out of 151, one of which fell between rounds and
        therefore does not end up in this table at all -- in the finished
        table the number is 1.
    ``deaths_without_victim_area``
        The victim's area is missing. **Zero** in the measured material, and
        that is exactly why the number exists: a value other than zero means
        the area observation has broken.
    ``deaths_without_attacker_area``
        The attacker's area is missing **although the attacker is known**.
        Rows without an attacker are not in this number: they have already
        been counted above, and a shared number would look like an area
        defect.
    """
    if deaths.is_empty():
        return {
            "death_rows": 0,
            "death_rounds": 0,
            "deaths_without_attacker": 0,
            "deaths_without_victim_area": 0,
            "deaths_without_attacker_area": 0,
        }
    return {
        "death_rows": int(deaths.height),
        "death_rounds": int(deaths["round_no"].n_unique()),
        "deaths_without_attacker": int(deaths["attacker_id"].null_count()),
        "deaths_without_victim_area": int(deaths["victim_area"].null_count()),
        "deaths_without_attacker_area": int(
            deaths.filter(
                pl.col("attacker_id").is_not_null()
                & pl.col("attacker_area").is_null()
            ).height
        ),
    }


def _lineup_stats(lineups: pl.DataFrame) -> dict[str, object]:
    """The lineup table's numbers, **one lineup at a time**.

    The breakdown is not decoration. The demo holds both teams' rows, and a
    combined number would answer a different question from the one the user
    asks: "does *this* team have a name" is not settled by a list that is
    non-empty as soon as the opponent has a clan. The same goes for players
    without a name -- those rows could all belong to the opponent.

    ``lineup_key`` is on every row because the user's next command is
    ``classify --team <lineup_key>`` and the stage has both values in hand.
    Without it the output would give the name but not what to write in its
    place on the command line.

    Returns:
        ``lineup_rows`` and ``lineups``, the latter as a tuple of
        ``(lineup_key, clan or None, players, without a name)`` in
        ``lineup_key`` order.
    """
    if lineups.is_empty():
        return {"lineup_rows": 0, "lineups": ()}
    grouped = (
        lineups.group_by("lineup_key")
        .agg(
            # The clan is a property of the lineup: one value per lineup.
            # ``max`` is merely a deterministic choice from a one-element set
            # -- more than one value would be a defect, and the adapter's
            # ``lineup_clan_conflicts`` reveals it.
            pl.col("clan_name").drop_nulls().unique().sort().alias("clans"),
            pl.len().alias("players"),
            pl.col("player_name").null_count().alias("without_name"),
        )
        .sort("lineup_key")
    )
    return {
        "lineup_rows": int(lineups.height),
        "lineups": tuple(
            (
                str(row["lineup_key"]),
                str(row["clans"][0]) if row["clans"] else None,
                int(row["players"]),
                int(row["without_name"]),
            )
            for row in grouped.iter_rows(named=True)
        ),
    }


def _read_table(path: Path) -> pl.DataFrame | str:
    """Read a table, or return a description of the error as a string."""
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _schema_is_current(
    table_abs: Path,
    ticks_abs: Path,
    events_abs: Path,
    lineups_abs: Path,
    deaths_abs: Path,
    callouts_abs: Path,
    match_abs: Path,
) -> bool:
    """Do the archive's finished tables still match the contract in force.

    A matching manifest is not enough on its own -- the same reason as in
    :func:`pappascout.stages.classify._usable_result`. An output table's
    schema can change without the manifest's contents changing: the parameter
    hash is computed from the ``[parse]`` section and demoparser2's version
    (AD-3), and neither moves when ``EVENTS`` gains a new column. Without this
    check the old table would stay silently in force and would look up to
    date, until some later stage failed on it.

    Story 2.9 is a precise example of this. The value ``snapped`` was removed
    from the ``AREA_SOURCES`` list, so an old ``events.parquet`` no longer
    loads into this version's Enum -- and ``callouts.parquet`` is missing from
    it entirely. Either is enough: the demo is reparsed without the
    ``--force`` flag.

    An unreadable table is **a different defect and not this function's
    business**: it is no more likely to be cured by rereading the demo than
    without it, and it already has its own reporting in
    :func:`_existing_stats`. What is settled here is only whether an intact
    table still matches the contract.

    Returns:
        ``False`` at the first contract break -- the stage is then run again
        and the tables are written with the current columns.
    """
    for path, schema, name in (
        (table_abs, ROUNDS, TABLE),
        (ticks_abs, TICKS, TICKS_TABLE),
        (events_abs, EVENTS, EVENTS_TABLE),
        (lineups_abs, LINEUPS, LINEUPS_TABLE),
        (deaths_abs, DEATHS, DEATHS_TABLE),
        (callouts_abs, CALLOUT_CLOUD, CALLOUTS_TABLE),
        (match_abs, MATCH, MATCH_TABLE),
    ):
        df = _read_table(path)
        if isinstance(df, str):
            continue
        try:
            validate(df, schema, name)
        except SchemaError:
            return False
    return True


def _existing_stats(
    table_abs: Path,
    ticks_abs: Path,
    events_abs: Path,
    lineups_abs: Path,
    deaths_abs: Path,
    callouts_abs: Path,
    match_abs: Path,
) -> dict[str, object]:
    """Numbers for a skipped run: read the finished tables, do not parse.

    The tables are read **separately**. A shared try block would lose the
    round counts whenever only the sample-point table is unreadable, and the
    user would see "the numbers could not be obtained" about what was
    perfectly intact as well.

    If the result cannot be read, that fact is returned rather than zeroes --
    a row of zeroes would look as though the demo held no rounds at all.
    """
    rounds = _read_table(table_abs)
    ticks = _read_table(ticks_abs)
    events = _read_table(events_abs)
    lineups = _read_table(lineups_abs)
    deaths = _read_table(deaths_abs)
    callouts = _read_table(callouts_abs)
    match = _read_table(match_abs)

    # The rounds and sample-point tables are the stage's core result. If
    # **neither** opens, the whole result is unreadable and no partial summary
    # is assembled from it: showing the utility numbers alone would give the
    # impression of an up-to-date result.
    if isinstance(rounds, str) and isinstance(ticks, str):
        return {"unreadable": rounds}

    stats: dict[str, object] = {}
    if isinstance(rounds, str):
        stats["unreadable"] = rounds
    else:
        stats.update(_round_stats(rounds))
    if isinstance(ticks, str):
        stats["ticks_unreadable"] = ticks
    else:
        stats.update(_tick_stats(ticks))
    if isinstance(events, str):
        stats["events_unreadable"] = events
    else:
        stats.update(_event_stats(events))
    if isinstance(lineups, str):
        stats["lineups_unreadable"] = lineups
    else:
        stats.update(_lineup_stats(lineups))
    if isinstance(deaths, str):
        stats["deaths_unreadable"] = deaths
    else:
        stats.update(_death_stats(deaths))
    if isinstance(callouts, str):
        stats["callouts_unreadable"] = callouts
    else:
        stats.update(_callout_stats(callouts))
    if isinstance(match, str):
        stats["match_unreadable"] = match
    else:
        stats.update(_match_stats(match))
    return stats


def run(
    settings: ParseSettings,
    archive: ArchivePaths,
    map_demo_id: str,
    parser: DemoParser,
    *,
    demo_path: Path | None = None,
    force: bool = False,
) -> StageResult:
    """Parse one demo into six tables.

    Args:
        settings: The ``[parse]`` section -- the only section this stage sees.
        archive: The archive's paths.
        map_demo_id: The unit's id, ``{match_id}-{map_index}``.
        parser: The demo port (AD-8). In production :func:`default_parser`.
        demo_path: The demo file. By default it is looked for in the archive.
        force: Ignore the manifest match and parse in any case.

    Returns:
        A :class:`~pappascout.stages.StageResult` whose ``stats`` gives the
        number of rounds, the row count, the largest round number, the counts
        of skipped rounds and of rounds without an anchor, the number of
        sample points and of first contacts, and the number of utility
        throws, of detonations and of events without an area.

    Raises:
        DemoUnavailable: If the demo is not found or cannot be read.
        ~pappascout.errors.ParseError: If the demo is not readable, or its
            contents break the CS2 rules.
        ~pappascout.errors.SchemaError: If the result does not match the
            contract.
    """
    started = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    if demo_path is None:
        _, demo_path = resolve_demo(archive, map_demo_id)
    demo_path = Path(demo_path)

    table_rel = parsed_table(map_demo_id, TABLE)
    ticks_rel = parsed_table(map_demo_id, TICKS_TABLE)
    events_rel = parsed_table(map_demo_id, EVENTS_TABLE)
    lineups_rel = parsed_table(map_demo_id, LINEUPS_TABLE)
    deaths_rel = parsed_table(map_demo_id, DEATHS_TABLE)
    callouts_rel = parsed_table(map_demo_id, CALLOUTS_TABLE)
    match_rel = parsed_table(map_demo_id, MATCH_TABLE)
    manifest_rel = parsed_manifest(map_demo_id)
    table_abs = archive.resolve(table_rel)
    ticks_abs = archive.resolve(ticks_rel)
    events_abs = archive.resolve(events_rel)
    lineups_abs = archive.resolve(lineups_rel)
    deaths_abs = archive.resolve(deaths_rel)
    callouts_abs = archive.resolve(callouts_rel)
    match_abs = archive.resolve(match_rel)
    manifest_abs = archive.resolve(manifest_rel)

    result_id = str(PurePosixPath("parsed") / map_demo_id)
    inputs = [
        ManifestInput(
            result_id=f"demo/{map_demo_id}",
            sha256=_demo_fingerprint(archive, map_demo_id, demo_path),
        )
    ]
    params_hash = _params_hash(settings)
    versions = tool_versions(*TOOLS)

    # The results a run of **this version** produces. The manifest's own
    # outputs list is not enough as the condition for skipping: an old
    # manifest names only those tables the code of the time knew how to
    # write, so a new table would never be born in the archive for demos that
    # have already been parsed.
    expected_outputs = (
        (table_rel, table_abs),
        (ticks_rel, ticks_abs),
        (events_rel, events_abs),
        (lineups_rel, lineups_abs),
        (deaths_rel, deaths_abs),
        (callouts_rel, callouts_abs),
        (match_rel, match_abs),
    )

    existing = Manifest.read_if_exists(manifest_abs)
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions=versions,
            root=archive.root,
        )
        and all(path.is_file() for _, path in expected_outputs)
        # The contract last: it reads the tables, and the cheaper conditions
        # prune most runs away before it.
        and _schema_is_current(
            table_abs,
            ticks_abs,
            events_abs,
            lineups_abs,
            deaths_abs,
            callouts_abs,
            match_abs,
        )
    ):
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in existing.outputs),
            manifest_path=manifest_rel,
            reason=(
                "The result is up to date: the manifest matches and the demo "
                "does not need to be parsed again."
            ),
            duration_s=time.perf_counter() - started,
            stats=_existing_stats(
                table_abs,
                ticks_abs,
                events_abs,
                lineups_abs,
                deaths_abs,
                callouts_abs,
                match_abs,
            ),
        )

    try:
        parsed = _parse_tables(parser, settings, demo_path, map_demo_id)
        df = parsed.rounds
        # The write is inside the same error handling as the parse: the disk
        # can fill up or the sync client can hold the file locked, and even
        # then the manifest has to be left with a record of the failure --
        # otherwise the next run would skip the stage over half a result.
        _write_tables(
            (
                (table_abs, df),
                (ticks_abs, parsed.ticks),
                (events_abs, parsed.events),
                (lineups_abs, parsed.lineups),
                (deaths_abs, parsed.deaths),
                (callouts_abs, parsed.callouts),
                (match_abs, parsed.match),
            )
        )
    except _RECORDED_ERRORS as exc:
        _record_failure(
            archive=archive,
            manifest_abs=manifest_abs,
            tables_abs=(
                table_abs,
                ticks_abs,
                events_abs,
                lineups_abs,
                deaths_abs,
                callouts_abs,
                match_abs,
            ),
            existing=existing,
            result_id=result_id,
            params_hash=params_hash,
            inputs=inputs,
            versions=versions,
            reason=str(exc),
        )
        raise

    diagnostics = getattr(parser, "diagnostics", None)

    # The manifest last: an interrupted run shows up on the next occasion as
    # a missing result and not as an up-to-date one.
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versions,
        status="ok",
        outputs=(
            str(table_rel),
            str(ticks_rel),
            str(events_rel),
            str(lineups_rel),
            str(deaths_rel),
            str(callouts_rel),
            str(match_rel),
        ),
    ).write(manifest_abs)

    stats = _stats(
        df,
        parsed.ticks,
        parsed.events,
        parsed.lineups,
        parsed.deaths,
        parsed.callouts,
        parsed.match,
        parsed.skipped_rounds,
    )
    # The buy-window numbers are computed **from the finished table**, so that
    # the knife round is not among them; see _buy_window_stats.
    stats.update(_buy_window_stats(df, diagnostics))
    # Unknown inventory names: they are not in the table, because they arm
    # nobody -- without this line a new weapon would look exactly like a new
    # knife skin. The key is set **on every fresh run**, including when the
    # port does not report them: a missing key means a skipped run, and
    # ``None`` a port that cannot say. Without the difference these three
    # states would look the same in the output.
    stats["armed_unknown_items"] = (
        None
        if diagnostics is None
        else tuple(getattr(diagnostics, "unknown_inventory_items", ()) or ())
    )
    # A match restart produces no row in any table, so its count **cannot be
    # computed from the finished result**. Three states have to be kept apart
    # exactly as above: the key is missing (a skipped run), ``None`` (a fresh
    # run, the port does not say) and a number (a fresh run, the port says).
    # Without the difference a demo served from the cache would silently
    # claim "no restart".
    stats["match_restarts"] = (
        None if diagnostics is None else getattr(diagnostics, "match_restarts", None)
    )
    # A pawnless player: the controller is there, the character is not on the
    # map. The row is not in the table and the point is not among the table's
    # sample points, so neither number **can be computed from the finished
    # result**.
    #
    # Three states kept apart as with the restarts, and here it is especially
    # needed: a missing key would mean the same thing in the output as zero,
    # and a skipped run would silently claim a clean setup. That is exactly
    # the lie the whole counter exists to remove -- a missing player would
    # look like a round on which the team simply played a man down.
    for _name in (
        "sample_rows_without_pawn",
        "sample_points_without_pawn",
        "grenade_throwers_without_row",
    ):
        stats[_name] = (
            None if diagnostics is None else getattr(diagnostics, _name, None)
        )
    # From a fresh run only: the rows of unnumbered rounds are not in the
    # table, so the number cannot be read back from a skipped run.
    stats["utility_unnumbered_rounds"] = parsed.unnumbered_utility
    # The same rule as with utility, and here it is especially needed: people
    # really do die on the knife round, so the drop is never empty in a league
    # demo. Without the number it would look as though there had been no
    # deaths.
    stats["deaths_unnumbered_rounds"] = parsed.unnumbered_deaths
    if diagnostics is not None:
        stats["tick_rate"] = diagnostics.tick_rate
        stats["tick_rate_measured"] = diagnostics.tick_rate_measured
        # These cannot be computed from the finished table: a partial sample
        # point, a skipped damage event and a dropped grenade show only in the
        # moment when the demo is being read. In a skipped run they are
        # therefore absent, and that is right.
        stats["partial_samples"] = getattr(diagnostics, "partial_samples", 0)
        # The lineup table's assumption "one name and one clan per player" is
        # not readable from the finished table: it writes the mode, so a
        # broken assumption looks intact there. Only a fresh run knows.
        for name in ("lineup_name_conflicts", "lineup_clan_conflicts"):
            stats[name] = getattr(diagnostics, name, 0)
        stats["armed_unreadable_rows"] = getattr(
            diagnostics, "armed_unreadable_rows", 0
        )
        # A number of its own, because the counters' readability conditions
        # differ: the difference says how many rows fell on the inventory
        # alone, and that cannot be read from the finished table.
        stats["armored_unreadable_rows"] = getattr(
            diagnostics, "armored_unreadable_rows", 0
        )
        # The buy window: from what moment the numbers have been read, how
        # often a death truncated the window and **whether the truncation cost
        # anything**. The last is this story's most important number: it is
        # the only sign that the compromise bit, and it cannot be computed
        # from the finished table.
        stats["unknown_side_events"] = getattr(
            diagnostics, "unknown_side_events", 0
        )
        stats["grenades_without_thrower"] = getattr(
            diagnostics, "grenades_without_thrower", 0
        )
        stats["grenades_outside_rounds"] = getattr(
            diagnostics, "grenades_outside_rounds", 0
        )
        for name in (
            "deaths_without_tick",
            "deaths_outside_rounds",
            "deaths_without_victim",
            "deaths_without_victim_side",
            "deaths_attacker_without_side",
            "grenades_unknown_side",
            "grenades_unknown_type",
            "grenades_fire_type_unresolved",
            "grenades_detonating_after_round",
            "grenade_ticks_without_players",
            "grenades_sharing_an_entity_id",
            "callout_cloud_rows_read",
        ):
            stats[name] = getattr(diagnostics, name, 0)
        # The reason for an empty point cloud: **from a fresh run only**. The
        # finished table says that the cloud is empty but not why -- and that
        # difference is exactly what separates a broken prop from a demo in
        # which nobody moved.
        stats["callout_cloud_empty_reason"] = getattr(
            diagnostics, "callout_cloud_empty_reason", None
        )
        # Why the map name was not obtained: **from a fresh run only**, like
        # the reason for the cloud's emptiness. The finished table says that
        # the name is missing but not whether the field was absent from the
        # header altogether -- and that difference is exactly what separates a
        # field the library renamed from a demo whose header never recorded
        # the map.
        stats["header_map_name_missing_reason"] = getattr(
            diagnostics, "header_map_name_missing_reason", None
        )

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(
            table_rel,
            ticks_rel,
            events_rel,
            lineups_rel,
            deaths_rel,
            callouts_rel,
            match_rel,
        ),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def _write_tables(tables: tuple[tuple[Path, pl.DataFrame], ...]) -> None:
    """Write all the tables as one transaction.

    Each table goes first into a temporary file of its own, and only once
    **all** of them are written are they moved to their targets. Successive
    ``atomic_path`` blocks are not enough: if the second write fails, the
    first would already be in place and the archive would be left with a
    rounds table saying 21 rounds and a sample-point table that knows two of
    them -- a combination that would pass every schema check.

    The move itself is not one atomic operation (one ``os.replace`` per
    table), but the window left between them is microseconds and holds no
    I/O.
    """
    with ExitStack() as stack:
        pending_writes = [
            (stack.enter_context(atomic_path(target)), frame)
            for target, frame in tables
        ]
        for tmp, frame in pending_writes:
            frame.write_parquet(tmp)
    # ExitStack unwinds the blocks only here, and each atomic_path does its
    # own rename. An exception in any of the writes cleans up all the
    # temporary files and does not touch the targets.


def _parse_tables(
    parser: DemoParser,
    settings: ParseSettings,
    demo_path: Path,
    map_demo_id: str,
) -> _ParsedTables:
    """Read the demo and build the finished, checked tables."""
    tables: DemoTables = parser.parse_demo(demo_path, settings.snapshot_seconds)
    _check_port_columns(
        tables.rounds, ROUNDS_ADAPTER_COLUMNS, "rounds table", "ROUNDS_ADAPTER_COLUMNS"
    )
    _check_port_columns(
        tables.ticks, TICKS_ADAPTER_COLUMNS, "sample-point table", "TICKS_ADAPTER_COLUMNS"
    )
    _check_port_columns(
        tables.events,
        EVENTS_ADAPTER_COLUMNS,
        "event table",
        "EVENTS_ADAPTER_COLUMNS",
    )
    _check_port_columns(
        tables.lineups,
        LINEUPS_ADAPTER_COLUMNS,
        "lineup table",
        "LINEUPS_ADAPTER_COLUMNS",
    )
    _check_port_columns(
        tables.deaths,
        DEATHS_ADAPTER_COLUMNS,
        "deaths table",
        "DEATHS_ADAPTER_COLUMNS",
    )
    _check_port_columns(
        tables.callouts,
        CALLOUTS_ADAPTER_COLUMNS,
        "point cloud",
        "CALLOUTS_ADAPTER_COLUMNS",
    )
    _check_port_columns(
        tables.match,
        MATCH_ADAPTER_COLUMNS,
        "match table",
        "MATCH_ADAPTER_COLUMNS",
    )

    numbered = mark_played_rounds(tables.rounds)
    skipped_rounds = int(
        numbered.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    played = numbered.filter(pl.col("round_no").is_not_null())

    df = played.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    ).sort("round_no", "side")

    if df.is_empty():
        raise ParseError(
            f"No played round was found in demo {demo_path.name} "
            f"({skipped_rounds} round boundaries were warm-up or the knife "
            "round).\n"
            "An empty result is not written -- otherwise it would stay "
            "permanently skipped on the strength of the manifest. Check that "
            "the demo is a recording of the whole match."
        )

    validate(df, ROUNDS, TABLE)
    check_win_reasons(df)
    _check_two_rows_per_round(df)
    _check_player_counters(df)

    ticks = _number_ticks(tables.ticks, numbered, map_demo_id)
    validate(ticks, TICKS, TICKS_TABLE)

    if ticks.is_empty():
        raise ParseError(
            f"Demo {demo_path.name} produced {df['round_no'].n_unique()} "
            "played rounds but not a single sample point.\n"
            "An empty setup table is not written as an ok result: it would "
            "stay permanently skipped on the strength of the manifest, and "
            "aggregation would report a map without a single setup.\n"
            + _tick_drop_reason(parser)
        )

    # An empty event table is **not** treated as an error, unlike the other
    # two: a demo always holds played rounds and setups, but utility can
    # genuinely be absent (a practice match, pistol rounds only). An error
    # would block the parse of the whole demo over a piece of information
    # that is itself an observation.
    events, unnumbered = _number_events(tables.events, numbered, map_demo_id)
    validate(events, EVENTS, EVENTS_TABLE)
    _check_grenade_key(events)

    lineups = _build_lineups(tables.lineups, map_demo_id)
    validate(lineups, LINEUPS, LINEUPS_TABLE)
    _check_lineup_key(lineups, demo_path)

    # The integrity guards are run **before the numbering**, that is, over
    # the adapter's whole output. The numbering drops the warm-up and
    # knife-round rows, and those are exactly the rounds a half-formed row
    # from the library most likely comes from -- after the numbering the
    # guard would look only at the part of the material where no defect is
    # expected.
    _check_victim_is_whole(tables.deaths)
    _check_attacker_is_whole(tables.deaths)

    deaths, unnumbered_deaths = _number_deaths(
        tables.deaths, numbered, map_demo_id
    )
    validate(deaths, DEATHS, DEATHS_TABLE)

    # The point cloud is **not numbered**: it is a property of the map in this
    # demo and not an observation about a round, so its rows do not fall away
    # along with the knife round -- the same rule as with the lineup table. An
    # empty cloud is a valid result, unlike an empty lineup table: a demo in
    # which last_place_name comes back empty is genuinely cloudless, and the
    # consequence (every detonation area null) is the right outcome and not a
    # silent defect -- the reason is given in the run's summary.
    callouts = _build_callouts(tables.callouts, map_demo_id)
    validate(callouts, CALLOUT_CLOUD, CALLOUTS_TABLE)
    _check_callout_cells(callouts)

    # The row count **before** the build: see _check_single_match_row.
    #
    # The match table is **not numbered** for the same reason as the lineup
    # table and the point cloud: the map is a property of the match and not an
    # observation about a round.
    _check_single_match_row(tables.match, demo_path)
    match = _build_match(tables.match, map_demo_id)
    validate(match, MATCH, MATCH_TABLE)

    if deaths.is_empty():
        raise ParseError(
            f"Demo {demo_path.name} produced {df['round_no'].n_unique()} "
            "played rounds but not a single death.\n"
            "An empty deaths table is not written as an ok result: it would "
            "stay permanently skipped on the strength of the manifest, and "
            "the report would give a map on which nobody died.\n"
            + _death_drop_reasons(parser, unnumbered_deaths)
        )

    return _ParsedTables(
        rounds=df,
        ticks=ticks,
        events=events,
        lineups=lineups,
        deaths=deaths,
        callouts=callouts,
        match=match,
        skipped_rounds=skipped_rounds,
        unnumbered_utility=unnumbered,
        unnumbered_deaths=unnumbered_deaths,
    )


def _tick_drop_reason(parser: DemoParser) -> str:
    """Why the setup table came out empty -- a number already computed.

    Without this the error message would name two guesses ("check
    ``snapshot_seconds`` and the freezetime anchors") even when the adapter
    has already worked out the real reason. A pawnless sample point is such a
    reason: if every point was missed because nobody had a character on the
    map, there is nothing wrong with the settings or the anchors and the user
    must not be told to check them.
    """
    diagnostics = getattr(parser, "diagnostics", None)
    dropped = getattr(diagnostics, "sample_points_without_pawn", 0) or 0
    rows = getattr(diagnostics, "sample_rows_without_pawn", 0) or 0
    if dropped:
        return (
            f"The reason has been read from the demo: {dropped} sample "
            "points were missed entirely, because every player row was "
            f"pawnless ({rows} rows). So it is not about "
            "[parse].snapshot_seconds or the freezetime anchors but about "
            "there being no player characters in the demo at the sample "
            "points' ticks. Check the demo."
        )
    return (
        "Check [parse].snapshot_seconds, and whether the rounds have a "
        "freezetime anchor."
    )


def _death_drop_reasons(parser: DemoParser, unnumbered: int) -> str:
    """Why the deaths table came out empty -- numbers already computed.

    The adapter breaks every drop reason out into a counter of its own, and
    they are in hand right here. Without them the error message would name two
    guesses ("a broken port or a renamed event") in a situation where the real
    reason can be read.
    """
    diagnostics = getattr(parser, "diagnostics", None)
    counts = [
        ("no tick", "deaths_without_tick"),
        ("outside the rounds", "deaths_outside_rounds"),
        ("no victim", "deaths_without_victim"),
        ("the victim's side not obtained", "deaths_without_victim_side"),
    ]
    named = [
        f"{label} {value}"
        for label, name in counts
        if diagnostics is not None and (value := getattr(diagnostics, name, 0))
    ]
    if unnumbered:
        named.append(f"on unnumbered rounds {unnumbered}")
    if named:
        return (
            "Dropped deaths: " + ", ".join(named) + ".\n"
            "If every number is zero, the demo port produced no death at "
            "all; otherwise the reason is named above."
        )
    return (
        "Not one drop counter is other than zero, so the demo port produced "
        "no death at all. Check that the player_death event has not been "
        "renamed in demoparser2."
    )


def _check_victim_is_whole(deaths: pl.DataFrame) -> None:
    """The victim is the row's identity, and it must not be missing.

    A death without a victim is not a death. An empty ``victim_id``,
    ``victim_lineup_key`` or ``victim_side`` would pass ``validate`` -- each
    of them is a nullable column -- and aggregation would count the row as
    **neither a death nor a kill**: it would disappear silently, because both
    filters compare against the lineup.

    The attacker has a guard of its own
    (:func:`_check_attacker_is_whole`), and it is looser on purpose: a death
    without an attacker is a genuine observation, one without a victim is
    nothing.

    Raises:
        SchemaError: If even one row is missing the victim's id, lineup or
            side.
    """
    if deaths.is_empty():
        return
    required = ("victim_id", "victim_lineup_key", "victim_side")
    broken = deaths.filter(
        pl.any_horizontal([pl.col(name).is_null() for name in required])
    )
    if broken.is_empty():
        return
    empty = ", ".join(
        f"{name}: {broken[name].null_count()}"
        for name in required
        if broken[name].null_count()
    )
    raise SchemaError(
        f"The deaths table has {broken.height} rows that are missing the "
        f"victim's details ({empty}).\n"
        "The victim is the row's identity: a death without a victim belongs "
        "to neither team, so it would disappear silently in aggregation "
        "-- neither as a death nor as a kill.\n"
        "The first rows: "
        f"{broken.select('round_raw', 't_s').head(3).to_dicts()}"
    )


def _check_attacker_is_whole(deaths: pl.DataFrame) -> None:
    """A death without an attacker is **wholly** without an attacker.

    A fall and the bomb produce a row on which there is no attacker, and that
    is a genuine observation. Half an attacker is not: a row on which
    ``attacker_id`` is empty but the coordinates or the area are not would
    claim a place for an actor who does not exist -- and aggregation would
    count that area as "kills" nobody made. The schema does not see this,
    because each field is valid on its own.

    ``attacker_side`` and ``attacker_lineup_key`` are **not** part of the
    check: they are derived from the round's side description rather than
    being the event's own, and they can be missing for an attacker who is
    known. ``attacker_area`` is included in this direction only -- it may be
    missing on its own, but it may not exist without an attacker.

    Raises:
        SchemaError: If even one row without an attacker carries attacker
            observations.
    """
    if deaths.is_empty():
        return
    observations = ("attacker_x", "attacker_y", "attacker_z", "attacker_area")
    broken = deaths.filter(
        pl.col("attacker_id").is_null()
        & pl.any_horizontal(
            [pl.col(name).is_not_null() for name in observations]
        )
    )
    if broken.is_empty():
        return
    raise SchemaError(
        f"The deaths table has {broken.height} rows that have no attacker "
        "but do have attacker observations "
        f"({', '.join(observations)}).\n"
        "A death without an attacker (a fall, the bomb) is a genuine case, "
        "but then every attacker field is empty: a place without an actor "
        "would land in the report as a kill nobody made.\n"
        "The first rows: "
        f"{broken.select('round_raw', 't_s', 'victim_id').head(3).to_dicts()}"
    )


def _number_deaths(
    deaths: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Join ``round_no`` to the deaths and drop the unnumbered rounds.

    The same decision and the same join as with sample points and utility. In
    this table the drop is **never empty in a league demo**: people really do
    die on the knife round, and in the measured material it produces about ten
    ``player_death`` rows. That is exactly why they are not filtered out
    separately -- one knife-round rule in two places would drift away from the
    rest of the numbering.

    The sort key is ``(round_no, t_s, victim_id)``: two team-mates can die on
    the same tick, and without the victim's id their order would depend on the
    join's stability.

    ``nulls_last=True`` is the same rule as in
    :func:`~pappascout.domain.aggregate._death_order`: **a missing time is not
    zero**. Without it a death with no anchor would lead its round in the
    parquet but would be last in aggregation, and the same thing would be
    ordered in two different ways depending on which one you look at.

    Returns:
        ``(the table, the deaths dropped)``. The latter is reported, because a
        silent drop would look like a demo that held fewer deaths.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        deaths.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in DEATHS if name != "map_demo_id"],
        )
        .sort("round_no", "t_s", "victim_id", nulls_last=True)
    )
    return joined, int(deaths.height - joined.height)


def _build_lineups(lineups: pl.DataFrame, map_demo_id: str) -> pl.DataFrame:
    """Join ``map_demo_id`` to the lineup table and order the rows.

    Round numbering is **not done**: the lineup and the name are properties of
    the map and not of a round, so dropping the knife round would take out of
    this table a player who played the map.
    """
    return lineups.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in LINEUPS if name != "map_demo_id"],
    ).sort("lineup_key", "player_id")


def _build_callouts(callouts: pl.DataFrame, map_demo_id: str) -> pl.DataFrame:
    """Join ``map_demo_id`` to the point cloud and order the rows.

    Round numbering is **not done**: the point cloud is a property of the map
    in this demo and not an observation about a round, and dropping the knife
    round would take out of it cells where people really did stand. The same
    grounds as with :func:`_build_lineups`.

    The sort key is the cell's coordinate, so that the same demo produces the
    same file byte for byte -- the adapter's own order would do, but the write
    order is part of the result's reproducibility and it is not left to
    another layer.
    """
    return callouts.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in CALLOUT_CLOUD if name != "map_demo_id"],
    ).sort("cell_x", "cell_y", "cell_z")


def _build_match(match: pl.DataFrame, map_demo_id: str) -> pl.DataFrame:
    """Join ``map_demo_id`` to the match table.

    Round numbering is **not done** and no ordering is needed: the table has
    one row. The same rule as with :func:`_build_lineups` and
    :func:`_build_callouts` -- a match is not a round, so dropping the knife
    round does not concern this table.
    """
    return match.select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in MATCH if name != "map_demo_id"],
    )


def _check_single_match_row(match: pl.DataFrame, demo_path: Path) -> None:
    """Make sure the match table holds exactly one row.

    The check is run **before** :func:`_build_match` and not after it.
    Afterwards it would work only because ``pl.lit`` happens to broadcast to
    height 0 as well; if it stopped doing so, the user would get a Polars
    exception in place of our own guidance. The same rule as with the
    ``ShapeError`` in ``stages.aggregate._in_schema_order``: the failure mode
    is removed, not translated.

    The row count is part of the contract, but schema validation does not see
    it: it looks at columns and types. Two defects would fit through.

    * **Zero rows.** ``aggregate`` reads into the name map only those demos
      for which a row is found, so an empty table would look exactly like a
      demo whose header held no map -- that is, as the observation that there
      is no observation. The difference matters: the latter is a genuine
      observation, the former a broken port.
    * **Two rows.** Two matches in the same file is not true, and the name map
      would take its value according to whichever row happened to be first --
      that is, the same demo could give a different map on a different run.

    ``map_name`` may be ``null``: that is the absence of an observation and
    not a missing row.
    """
    if match.height == 1:
        return
    raise ParseError(
        f"The match table of demo {demo_path.name} has {match.height} rows, "
        "although there must be exactly one.\n"
        "The table describes one match, so zero rows means a demo without a "
        "match and two rows means two matches in the same file. Neither is "
        "true, and either would give the map name a value that was not "
        "observed. This is a defect in the port: fix the demoparser2 adapter."
    )


def _check_callout_cells(callouts: pl.DataFrame) -> None:
    """Make sure a cell is unique and that it has an area.

    Two defects that would pass the schema but would break the detonation
    areas:

    * **A duplicate cell.** The same ``(cell_x, cell_y, cell_z)`` twice would
      mean the mode selection had not done its work: a detonation would get
      its area according to whichever row happened to be nearer in the
      ordering, and the same demo could give a different area on a different
      run.
    * **A cell without a name.** ``area`` is a nullable column, so an empty
      name would pass the schema -- and would name the detonation empty
      *inside the threshold*. The row would then look the same as "no area was
      obtained", although the hit was good. An area without a name does not
      belong in the cloud at all, and neither does a name made of nothing but
      spaces: ``strip`` makes them the same thing.
    * **A cell without observations.** ``observations`` is ``Int32``, so zero
      and negative would pass the schema. A cell is born only from an
      observation, so zero would mean an invented cell -- and that would skew
      both the ``callout_observations`` number and the run's usable-row ratio
      silently downwards.

    An empty table is neither of these: it is a valid result, and its reason
    is given in the run's summary.

    Raises:
        SchemaError: Names the broken invariant and at most five rows.
    """
    if callouts.is_empty():
        return

    unnamed = int(
        callouts.filter(
            pl.col("area").is_null()
            | (pl.col("area").str.strip_chars().str.len_chars() == 0)
        ).height
    )
    if unnamed:
        raise SchemaError(
            f"The point cloud has {unnamed} cells without an area name.\n"
            "A cell without a name would name the detonation empty inside "
            "the threshold, that is, the row would look the same as 'no area "
            "was obtained' although the hit was good. An observation without "
            "an area does not belong in the cloud at all -- check the filter "
            "in the function domain.utility.build_point_cloud."
        )

    empty_cells = callouts.filter(
        pl.col("observations").is_null() | (pl.col("observations") < 1)
    )
    if not empty_cells.is_empty():
        raise SchemaError(
            f"The point cloud has {empty_cells.height} cells that have not a "
            "single observation.\n"
            "A cell is born only from an observation, so zero or negative "
            "means an invented cell -- and that skews both the cloud's "
            "observation count and the run's usable ratio silently "
            "downwards.\n"
            f"The first rows: "
            f"{empty_cells.select('cell_x', 'cell_y', 'cell_z', 'observations').head(3).to_dicts()}"
        )

    key = callouts.select("cell_x", "cell_y", "cell_z")
    if key.height == key.unique().height:
        return
    duplicates = (
        callouts.group_by("cell_x", "cell_y", "cell_z")
        .len()
        .filter(pl.col("len") > 1)
        .sort("cell_x", "cell_y", "cell_z")
        .head(5)
    )
    raise SchemaError(
        "The point cloud's key (cell_x, cell_y, cell_z) is not unique: "
        f"{key.height - key.unique().height} rows are duplicates.\n"
        "A cell has exactly one area -- the most common of the observations "
        "-- and two rows would mean that a detonation got its area from the "
        "accident of the ordering.\n"
        f"The first repeated cells: {duplicates.to_dicts()}"
    )


def _check_lineup_key(lineups: pl.DataFrame, demo_path: Path) -> None:
    """Make sure ``(lineup_key, player_id)`` identifies a row -- and exists.

    Two defects that would pass the schema but would break the report:

    * **An empty table.** Lineups are identified from every demo, so an empty
      table means a broken port. It would stay permanently skipped on the
      strength of the manifest, and every report would talk about digests
      without anything saying why.
    * **A duplicate row.** ``aggregate`` joins the standing roster on this
      key; a player occurring twice would show twice in the roster.
    """
    if lineups.is_empty():
        raise SchemaError(
            f"Demo {demo_path.name} produced no lineup row at all.\n"
            "Lineups are identified from every demo, so an empty table means "
            "a broken demo port. An empty result is not written: it would "
            "stay permanently skipped on the strength of the manifest and the "
            "report would talk about digests without saying why."
        )
    key = lineups.select("lineup_key", "player_id")
    if key.height == key.unique().height:
        return
    duplicates = (
        lineups.group_by("lineup_key", "player_id")
        .len()
        .filter(pl.col("len") > 1)
        .sort("lineup_key", "player_id")
        .head(5)
    )
    raise SchemaError(
        "The lineup table's key (lineup_key, player_id) is not "
        f"unique: {key.height - key.unique().height} rows are "
        "duplicates.\n"
        f"The first repeated keys: {duplicates.to_dicts()}"
    )


def _number_ticks(
    ticks: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> pl.DataFrame:
    """Join ``round_no`` to the sample points and drop unnumbered rounds.

    The adapter samples every anchored round boundary, because it does not
    know the numbering rule -- that belongs to
    :func:`~pappascout.domain.rounds.mark_played_rounds`. The warm-up and
    knife-round rows therefore go only here, by the same decision as from the
    rounds table, so the tables cannot disagree about which round was played.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        ticks.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in TICKS if name != "map_demo_id"],
        )
        .sort("round_no", "sample_t_s", "sample_kind", "side", "player_id")
    )
    return joined


def _number_events(
    events: pl.DataFrame, numbered: pl.DataFrame, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Join ``round_no`` to the events and drop the unnumbered rounds.

    The same decision and the same join as with the sample points
    (:func:`_number_ticks`): utility thrown during the warm-up and on the
    knife round goes here, so the tables cannot disagree about which round was
    played.

    The sort key is ``(round_no, grenade_no, event_kind, t_s)``.
    ``grenade_no`` comes **before** ``event_kind`` so that a trajectory's two
    rows stay next to each other: sorted by the game's id, all the throws of a
    recycled id would come before all of its detonations, and the pair would
    scatter to different places in the table. ``event_kind`` comes before
    ``t_s`` so that the throw always comes before its detonation even when
    both have the same ``t_s``; it is an Enum, so the order is the order of
    the list and not alphabetical. ``grenade_no`` is unique, so the key
    determines the order completely -- the game's own id is not in the sort at
    all, because it does not tell two trajectories apart.

    Returns:
        ``(the table, the throws dropped)``. The latter is counted because the
        three other reasons for dropping utility are reported for exactly the
        reason that utility must not disappear silently -- and this must not
        be an exception.
    """
    numbers = (
        numbered.select("round_raw", "round_no")
        .unique(subset=["round_raw"], keep="first")
        .filter(pl.col("round_no").is_not_null())
    )
    joined = (
        events.drop("round_no")
        .join(numbers, on="round_raw", how="inner")
        .select(
            pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
            *[pl.col(name) for name in EVENTS if name != "map_demo_id"],
        )
        .sort("round_no", "grenade_no", "event_kind", "t_s")
    )
    before = int(events.filter(pl.col("event_kind") == THROWN).height)
    after = int(joined.filter(pl.col("event_kind") == THROWN).height)
    return joined, before - after


def _record_failure(
    *,
    archive: ArchivePaths,
    manifest_abs: Path,
    tables_abs: tuple[Path, ...],
    existing: Manifest | None,
    result_id: str,
    params_hash: str,
    inputs: list[ManifestInput],
    versions: dict[str, str],
    reason: str,
) -> None:
    """Record the failure in the manifest -- but do not destroy a good result.

    If the archive already holds valid tables and their ``ok`` manifest,
    everything is left in place. Otherwise ``parse_failed`` is recorded in the
    manifest, which both gives the reason and blocks the skip on the next run.

    **Every** table has to be in place: an old ``rounds`` together with a new
    ``ticks`` table would be half a result that looked intact.
    """
    intact_result = (
        existing is not None
        and existing.status == "ok"
        and existing.outputs_present(archive.root)
        and all(path.is_file() for path in tables_abs)
    )
    if intact_result:
        return
    Manifest.new(
        result_id=result_id,
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=versions,
        status="parse_failed",
        reason=reason,
        outputs=(),
    ).write(manifest_abs)

