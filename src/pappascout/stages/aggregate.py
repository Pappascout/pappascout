"""``aggregate`` -- the pipeline's third stage: a report from classified rounds.

The stage reads a team's classified rounds from the archive
(``classified/<team_key>/<map_demo_id>.parquet``) together with all seven of
the demo's parsed tables (rounds, sample points, events, lineups, deaths, the
point cloud and the match row, under ``parsed/<map_demo_id>/``) and writes
**one file**: ``aggregates/<team_key>/report.json``, which is the
:class:`~pappascout.domain.report.Report` model as JSON. The demo is not read.

It computes everything, ``render`` computes nothing
---------------------------------------------------
The stage **does not choose what is reported**. It computes every round type,
full buys and overtime included, and leaves the choice to Story 2.4. If
aggregation filtered, changing the presentation choice would need a
recomputation and ``report.json`` would stop being a full picture of what is
known about the demos.

Nor does the stage interpret. The words "fake" and "rush" are in no field --
only observations and counts. The interpreting is done by a human.

``team_key`` in this story
--------------------------
The team index (``index/teams.json``) is written by the ``discover`` stage,
but this stage does not read it, so ``--team`` is the name of the
``classified/`` directory, that is, the lineup key -- exactly as in the
``classify`` stage. **That still holds after Story 4.10**, which names the
opponent of every map: the match index gives both teams' rosters as
SteamID64s, and the subject is recognised by the players the demos were
observed to hold (:func:`_opponent_name`). The stage reads one index file, not
two.

A lineup key is a hash of the players who played the map, so **one
substitution produces a new key**: MatureMayhem is under two different keys
across the four demos. The stage joins them with a rule that is already in the
settings (``[thresholds].team_identity_min_common``, AD-6): two lineups are the
same team when they have at least three players in common. Without the join
the report would see three demos out of four and would not say it had lost
one. The joined keys are recorded in the report (``team.lineup_keys``), so the
decision can be checked.

The team's name is an observation
---------------------------------
The name is read from the lineups table's ``clan_name`` column, which is a
value observed from the demo. It is not derived from the file name, from the
FACEIT id or from any other source: without an observation ``display_name`` is
the ``team_key`` itself, ``display_name_source`` is ``team_key``, and the
report says the absence out loud.

**A conflict does not disappear.** If the joined demos give the team different
names, the one observed most often is chosen to be shown and the rest end up
in the ``display_name_alternatives`` field. The vote is per demo and not per
row, and a tie is settled alphabetically so that the run is repeatable
(:func:`~pappascout.domain.aggregate.team_identity`).

**The team's key does not change.** ``team_key`` is the directory structure
(``classified/<team_key>/``), and turning it into a name is the work of Epic
3's ``select`` stage. This stage changes only what is shown -- and the file
name's slug, which follows the shown name.

The map's name is an observation, inference is the fallback
-----------------------------------------------------------
The map's name is read from the ``parsed/<map_demo_id>/match.parquet`` table,
into which ``parse`` writes it from the demo's header (Story 2.11). The name is
not validated against the map pool: a map outside the pool is a genuine
observation.

Only if there is no observation -- ``map_name`` is ``null`` -- is the name
inferred from the ``map_demo_id`` against the map pool. ``map_name_source``
says where the name came from (``demo_header`` -> ``map_demo_id`` ->
``unknown``), and an unknown map does not melt into another map's branch but
stays its own under the name of its id.

Two demos of the same map are **one branch**: the name is the same, the rounds
add up and ``played_maps`` lists the demos. This is exactly what does not
happen without the header, because a FACEIT id (``1-79f71e00-...``) does not
carry the map's name.

The manifest and re-running
---------------------------
The inputs are the ``classify`` manifests of every demo taken along, and their
id is computed with
:meth:`~pappascout.archive.manifest.Manifest.fingerprint` -- the same
definition with which ``classify`` recognises its own input. The parameter hash
is computed from the named ``[thresholds]`` and ``[league]`` keys and from the
whole ``[aggregate]`` section (AD-3), so adjusting a threshold re-runs this
stage but not the parsing. ``[aggregate]`` holds the pistol routes' points
(``route_sample_seconds``, Story 4.5) as well: the routes read only the points
the report prints, and that list is this stage's own rather than a read of
``[report]``, which is render's.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl

from pappascout import __version__
from pappascout.archive.atomic_write import atomic_write_text
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    ArchivePaths,
    classified,
    classified_manifest,
    parsed_table,
    report_json,
    report_manifest,
    safe_component,
)
from pappascout.constants import SIDES
from pappascout.domain.aggregate import (
    LEAGUE_BUCKETS,
    SLUG_FALLBACK,
    MatchFact,
    build_report,
    lineups_of_same_team,
    match_of,
    roster_entries,
    slugify,
    team_identity,
)
from pappascout.domain.models import (
    AggregateSettings,
    LeagueSettings,
    ThresholdSettings,
)
from pappascout.domain.report import (
    REPORT_SCHEMA_VERSION,
    Anomaly,
    MissingDemo,
    Report,
    TeamReport,
)
from pappascout.domain.sampling import (
    TIME_SAMPLE,
    AreaObservations,
    CloudCell,
    normalize_area,
)
from pappascout.domain.schemas import (
    CALLOUT_CLOUD,
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    ROUNDS,
    TICKS,
    Schema,
    validate,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult
from pappascout.stages.discover import read_matches_index

__all__ = [
    "STAGE",
    "TOOLS",
    "run",
    "team_keys",
    "resolve_team",
    "collect_team",
    "TeamSources",
    "MatchIndexReading",
    "match_order",
    "read_match_index",
    "match_index_input",
    "match_index_input_id",
]

STAGE = "aggregate"

#: Empty: aggregation is pure computation over the tables that were read, and
#: no external library's version changes its result (the manifest module's
#: rule).
#:
#: The list exists all the same and is read with :func:`_tools`, so that adding
#: a new dependency is a one-line change rather than a hunt for two hard-coded
#: ``{}``\ s -- the same shape as in ``stages.parse``.
TOOLS: tuple[str, ...] = ()


def _tools() -> dict[str, str]:
    """Tool versions for the manifest. Empty until :data:`TOOLS` is not."""
    return tool_versions(*TOOLS)


@dataclass(frozen=True)
class TeamSources:
    """A team's material in the archive: lineups, demos and missing demos.

    Split into a type of its own so that collecting a team is testable without
    building the report -- and so that its result can be read out in the error
    message when no demos were found at all.

    Attributes:
        team_key: The directory name the user chose; also the result's
            directory.
        lineup_keys: The lineup keys joined into the same team.
        demos: ``(lineup_key, map_demo_id)`` for every demo taken along.
        roster: The players observed across all the joined lineups -- the
            **union over the whole sample**, and what the report's roster row
            states.
        lineup_players: ``lineup_key -> the players observed in that lineup``
            (Story 4.10). The same observations ``roster`` is the union of,
            kept unmerged, because the union answers "who plays for this
            team" and some questions are about **one match**. See
            :func:`_match_rosters`, where using the union was a defect.

            A default of ``{}`` only so that a caller built before this field
            existed still constructs; every caller in the tree fills it.
        missing: The demos whose data was not there. They do not vanish
            silently.
    """

    team_key: str
    lineup_keys: list[str]
    demos: list[tuple[str, str]]
    roster: list[str]
    missing: list[MissingDemo]
    lineup_players: dict[str, frozenset[str]] = field(default_factory=dict)


def run(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    archive: ArchivePaths,
    team: str | None,
    *,
    aggregate_settings: AggregateSettings,
    force: bool = False,
) -> StageResult:
    """Aggregate one team's classified rounds into a report.

    Args:
        thresholds: The ``[thresholds]`` section; ``small_sample_rounds`` and
            ``team_identity_min_common`` are read from it.
        league: The ``[league]`` section; the map pool is read from it for
            inferring the map's name.
        aggregate_settings: The ``[aggregate]`` section, **as a keyword
            argument**. A keyword because three pydantic sections in a row
            would go through positionally with two of them swapped without
            anything remarking on it -- the same reason as for ``classify``'s
            ``economy`` argument.
        archive: The archive's paths.
        team: The team's id or an unambiguous prefix of it. ``None`` produces
            an error that lists the archive's teams.
        force: Aggregate even if the manifest matched.

    Returns:
        A :class:`~pappascout.stages.StageResult` whose ``stats`` holds the
        samples, the number of maps and the missing demos.

    Raises:
        ~pappascout.errors.PappascoutError: If the team is not recognised or no
            classified demo is found at all.
        ~pappascout.errors.AggregateError: If the sample does not add up at
            some level.
        ~pappascout.errors.SchemaError: If some table that was read does not
            match the contract.
    """
    started = time.perf_counter()
    team_key = resolve_team(archive, team)
    sources = collect_team(archive, team_key, thresholds)

    json_rel = report_json(team_key)
    manifest_rel = report_manifest(team_key)
    json_abs = archive.resolve(json_rel)
    manifest_abs = archive.resolve(manifest_rel)

    # Read once and used twice: the digest that decides whether to skip and
    # the values the report is built from have to be the same reading, or a
    # ``discover`` landing between them would make the manifest describe a
    # report that was never built.
    #
    # **It was read twice until Story 4.10**, measured on this line: the
    # reading was made here and then made again inside ``_aggregate``, whose
    # ``order`` argument this call did not pass -- so the comment above stated
    # a guarantee the code did not keep. It is passed now.
    reading = read_match_index(archive, _match_rosters(sources))
    inputs = [
        *_inputs(archive, sources.demos),
        match_index_input(reading, sources.demos),
    ]
    params_hash = _params_hash(thresholds, league, aggregate_settings)

    existing = Manifest.read_if_exists(manifest_abs)
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
            params_hash=params_hash,
            tool_versions=_tools(),
            root=archive.root,
        )
    ):
        ready = _read_report(json_abs)
        if ready is not None:
            return StageResult(
                stage=STAGE,
                unit=team_key,
                status="ok",
                skipped=True,
                outputs=tuple(PurePosixPath(o) for o in existing.outputs),
                manifest_path=manifest_rel,
                reason=(
                    "The result is up to date: the manifest matches and the "
                    "rounds do not need to be aggregated again."
                ),
                duration_s=time.perf_counter() - started,
                stats=_stats(ready, sources),
            )

    report = _aggregate(
        archive, sources, thresholds, league, aggregate_settings, reading
    )

    atomic_write_text(json_abs, report.model_dump_json(indent=2) + "\n")
    Manifest.new(
        result_id=str(PurePosixPath("aggregates") / team_key),
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions=_tools(),
        status="ok",
        outputs=(str(json_rel),),
    ).write(manifest_abs)

    return StageResult(
        stage=STAGE,
        unit=team_key,
        status="ok",
        skipped=False,
        outputs=(json_rel,),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=_stats(report, sources),
    )


# -- Collecting the team ---------------------------------------------------------


def team_keys(archive: ArchivePaths) -> list[str]:
    """The archive's classified teams, that is, the ``classified/`` dirs."""
    root = archive.resolve(PurePosixPath("classified"))
    if not root.is_dir():
        return []
    return sorted(
        d.name for d in root.iterdir() if d.is_dir() and _demo_ids(d)
    )


def _demo_ids(directory: Path) -> list[str]:
    """The directory's classified demos as ids."""
    return sorted(p.stem for p in directory.glob("*.parquet"))


def resolve_team(archive: ArchivePaths, team: str | None) -> str:
    """Read ``--team`` as one of the archive's team directories.

    Accepts both the full id and an unambiguous prefix of it -- a 16-character
    hash is uncomfortable to type by hand.

    Raises:
        PappascoutError: If the id is missing, matches nothing or matches more
            than one. The message always lists the archive's teams, so the next
            command can be copied straight from it.
    """
    available = team_keys(archive)
    if not available:
        raise PappascoutError(
            "The archive has no classified team at all, so there is nothing "
            "to aggregate.\n"
            "Run first: uv run pappascout classify <map_demo_id> --team "
            "<id>"
        )
    if team is None:
        raise PappascoutError(
            "Say with the --team option whose rounds are to be "
            f"aggregated.\n{_team_listing(available)}"
        )

    query = team.strip().lower()
    matches = [k for k in available if k.lower() == query]
    if not matches:
        matches = [k for k in available if k.lower().startswith(query)]
    if len(matches) == 1:
        return safe_component(matches[0], "team_key")

    problem = (
        f"The team id {team!r} matches more than one team."
        if matches
        else f"The team id {team!r} matches no team at all."
    )
    raise PappascoutError(f"{problem}\n{_team_listing(available)}")


def _team_listing(available: Sequence[str]) -> str:
    rows = "\n".join(f"    {key}" for key in available)
    return (
        "The archive's classified teams are:\n"
        + rows
        + "\nGive the id in full or its beginning, for example:\n"
        + f"    --team {available[0][:8]}"
    )


def collect_team(
    archive: ArchivePaths, team_key: str, thresholds: ThresholdSettings
) -> TeamSources:
    """Collect the team's lineups, demos and roster from the archive.

    A lineup's members are read from **one demo per lineup**: the key is a hash
    of the players who played the map, so the same key always means the same
    set and there is no need to read more than one demo.

    **How wide the search is, and where it stops.** Team identity is a
    comparison against the *other* lineups, so the members of every lineup in
    the archive have to be known -- one team's own demos are not enough. The
    cost is therefore one read per ``classified/`` directory, not per demo, and
    the read is two columns (``lineup_key``, ``player_id``) from the sample
    point table. With four demos that is milliseconds; with a hundred teams it
    is a hundred small reads. If the archive ever grows large enough for this
    to cost, the right answer is Epic 3's team index
    (``index/teams.json``), which removes the whole inference -- not optimising
    this loop.

    Raises:
        PappascoutError: If the team has no readable demo at all.
    """
    root = archive.resolve(PurePosixPath("classified"))
    demos_by_lineup = (
        {
            d.name: _demo_ids(d)
            for d in sorted(root.iterdir())
            if d.is_dir() and _demo_ids(d)
        }
        if root.is_dir()
        else {}
    )

    members: dict[str, set[str]] = {}
    missing: list[MissingDemo] = []
    for lineup, lineup_demos in demos_by_lineup.items():
        for demo in lineup_demos:
            found = _lineup_members(archive, demo)
            if found is None:
                continue
            members.update({k: v for k, v in found.items() if k not in members})
            if lineup in members:
                break

    known = {k: v for k, v in members.items() if k in demos_by_lineup}

    # A lineup whose lineups table could not be read from any of its demos is
    # not comparable to the others -- and so cannot be joined to a team. Its
    # demos must not vanish without trace all the same: they are recorded as
    # missing with the reason, so that the reader sees something is absent from
    # the sample even though aggregation cannot know whose it was.
    #
    # **One row per demo, not per lineup.** One demo holds both teams, so the
    # same file is under two directories and would produce two rows for the
    # same absence. Measured on the first run: four rows from two demos. That
    # reads as though four matches were missing from the sample.
    for lineup, lineup_demos in demos_by_lineup.items():
        if lineup in known:
            continue
        for demo in lineup_demos:
            missing.append(
                MissingDemo(
                    match=demo,
                    # The reason says what is actually known: an unreadable
                    # lineups table does not say whose demo this is, so it
                    # cannot be claimed to be a match *this* team lost.
                    reason=(
                        "The lineups table (lineups.parquet) could not be "
                        "read, so it is not known whether the demo belongs to "
                        "this team. "
                        f"Parse it again: uv run pappascout parse {demo}"
                    ),
                )
            )

    if team_key not in known:
        raise PappascoutError(
            f"The lineup of team {team_key} could not be read: the lineups "
            "table (lineups.parquet) of not one of its demos was found in the "
            "archive.\n"
            "Parse them again: uv run pappascout parse <map_demo_id>"
        )

    lineup_keys = lineups_of_same_team(
        team_key, known, thresholds.team_identity_min_common
    )

    demos: list[tuple[str, str]] = []
    roster: set[str] = set()
    for lineup in lineup_keys:
        roster.update(known[lineup])
        for demo in demos_by_lineup[lineup]:
            reason = _demo_unusable(archive, lineup, demo)
            if reason is None:
                demos.append((lineup, demo))
            else:
                missing.append(MissingDemo(match=demo, reason=reason))

    # A demo that got in is not missing even if it is also under some
    # unreadable lineup: the same file is always in two teams' directories, and
    # the opponent's unreadable lineup does not take from us a match we have
    # just read.
    included = {demo for _, demo in demos}
    missing = _unique_by_match(m for m in missing if m.match not in included)

    if not demos:
        raise PappascoutError(
            f"Team {team_key} has not one demo whose classification and "
            "parsing are both in the archive.\n"
            + (
                "Missing:\n"
                + "\n".join(f"    {m.match}: {m.reason}" for m in missing)
                if missing
                else ""
            )
        )
    return TeamSources(
        team_key,
        lineup_keys,
        demos,
        sorted(roster),
        missing,
        # The same observations the union above was built from, unmerged.
        # ``known`` holds every lineup the archive has; only the joined ones
        # are this team's.
        {lineup: frozenset(known[lineup]) for lineup in lineup_keys},
    )


def _unique_by_match(entries: Iterable[MissingDemo]) -> list[MissingDemo]:
    """One row per demo, the first reason wins.

    Missing demos are listed as **matches**, because that is how the reader
    counts them. The same file is under two lineups (both teams), and the same
    absence found under two lineups would look in the list like two different
    missing matches.
    """
    seen: dict[str, MissingDemo] = {}
    for entry in entries:
        seen.setdefault(entry.match, entry)
    return list(seen.values())


def _lineup_members(
    archive: ArchivePaths, map_demo_id: str
) -> dict[str, set[str]] | None:
    """The demo's lineups with their players, or ``None`` if there is no table.

    The source is ``lineups.parquet`` and not ``ticks.parquet`` (Story 2.6).
    Two reasons: the lineups table is tens of rows where the sample point table
    is tens of thousands, and its set of players is **exactly the one**
    ``lineup_key`` was computed from -- the sample point table would be missing
    a player who did not make it to a single sample point.
    """
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    if not path.is_file():
        return None
    try:
        df = pl.read_parquet(path, columns=["lineup_key", "player_id"])
    except (OSError, pl.exceptions.PolarsError):
        return None
    members: dict[str, set[str]] = {}
    for row in df.unique().iter_rows(named=True):
        members.setdefault(str(row["lineup_key"]), set()).add(str(row["player_id"]))
    return members


def _demo_unusable(
    archive: ArchivePaths, lineup: str, map_demo_id: str
) -> str | None:
    """The reason the demo cannot be taken along -- or ``None`` if it can.

    A missing parse does not stop the run: the demo goes into the
    ``missing_demos`` list with its reason, and the report says so. A single
    missing demo must not take the whole sample with it -- it would take along
    three others that are in order.
    """
    # The rounds table has been on the list since Story 2.8: the armour counter
    # is there and not in the classified table, because it is an observation
    # and not an input to the classification's decision. A missing table is
    # therefore the same absence as any other -- without it the report would
    # look like a round type on which nobody bought armour.
    # The match table has been on the list since Story 2.11: the map's name is
    # there. Without it the demo would get its branch by inference, that is, a
    # FACEIT demo would stay a branch of its own under the name of its id --
    # and a multi-demo sample would fall apart silently on exactly the demo
    # whose table is absent.
    # The point cloud has been on the list since Story 2.14: the stack rule's
    # site groups are derived from it. A MISSING TABLE IS NOT THE SAME THING AS
    # A SILENCED DEMO. The latter is an observation about the map (the sites do
    # not separate into levels) and is recorded in the coverage; the former
    # means the demo was parsed with an older version of the program -- and,
    # silent, it would read in the coverage as a property of the map.
    for table in (
        "rounds",
        "ticks",
        "events",
        "lineups",
        "deaths",
        "match",
        "callouts",
    ):
        if not archive.resolve(parsed_table(map_demo_id, table)).is_file():
            return (
                f"The parsed table {table}.parquet is not in the archive. "
                f"Run: uv run pappascout parse {map_demo_id}"
            )
    manifest = Manifest.read_if_exists(
        archive.resolve(classified_manifest(lineup, map_demo_id))
    )
    if manifest is None:
        return (
            "There is no classification manifest, so the input cannot be "
            "recognised. "
            f"Run: uv run pappascout classify {map_demo_id} --team {lineup}"
        )
    if manifest.status != "ok":
        return (
            f"The classification is marked with the status {manifest.status!r}: "
            f"{manifest.reason or 'no reason was recorded'}"
        )
    return None


# -- Aggregation -----------------------------------------------------------------


def _aggregate(
    archive: ArchivePaths,
    sources: TeamSources,
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    aggregate_settings: AggregateSettings,
    reading: MatchIndexReading | None = None,
) -> Report:
    """Read the tables and build the report.

    ``reading`` is what the archive's match index says (:func:`read_match_index`
    ): the matches newest first and, per match, its date and the opponent. It
    is an argument rather than a read, because :func:`run` has to declare the
    same reading in the manifest that the report is built from; ``None`` means
    "read it here", which is what a caller outside the stage -- the
    calibration tests -- wants.

    **One object and not two arguments**, since Story 4.10 takes two things
    out of the same file: split, a caller could hand an order from one reading
    and facts from another, and the report would mark recency against matches
    it did not name.
    """
    # Resolved before the tables and not beside ``build_report``: a malformed
    # index is the user's to fix, and saying so takes a moment here where it
    # would take the whole parquet read below first.
    index = (
        read_match_index(archive, _match_rosters(sources))
        if reading is None
        else reading
    )
    classified_frames: list[pl.DataFrame] = []
    tick_frames: list[pl.DataFrame] = []
    event_frames: list[pl.DataFrame] = []
    lineup_frames: list[pl.DataFrame] = []
    death_frames: list[pl.DataFrame] = []
    round_frames: list[pl.DataFrame] = []
    # The map's name per demo: an observation or ``None``.
    #
    # THE KEY IS THE **DEMO THAT WAS READ**, NOT THE TABLE'S OWN COLUMN.
    # ``_read_parsed`` validates the schema but not that the value of the
    # ``map_demo_id`` column matches the demo from whose directory the table
    # was read. A stale ``match.parquet``, or one that ended up in the wrong
    # directory, would record its name against the wrong demo and the right
    # demo would fall back silently to inference; two identical ids would drop
    # one of them entirely. The loop's ``demo`` is the one from whose path the
    # table was read, so as the key it removes the whole class of failure --
    # the empty and ``null`` id cases included -- without any new checks.
    #
    # The table is **not filtered by lineup** as the others are: it has no
    # ``lineup_key`` column, because the map is a property of the match and not
    # of either team.
    #
    # One row per demo is a contract the ``parse`` stage enforces, so the map
    # cannot get two values for the same demo.
    map_names: dict[str, str | None] = {}
    # The side orientation of the areas, per demo (Story 2.5): area -> how many
    # of its alive observations are from the T side and how many there are all
    # told.
    #
    # COMPUTED IN THIS LOOP, BEFORE THE LINEUP FILTER. The filter happens only
    # after the loop (``ticks.filter(...)``), and that is the whole reason the
    # orientation is computed here and not in the domain: the rule needs
    # **both teams'** rows. Computed on the subject's own rows every true
    # positive disappears -- when the subject advances into an area as CT, his
    # own CT observations lower that area's T share, so the anomaly eats its
    # own detection. Measured, three areas fall below the threshold (0.88 ->
    # 0.79, 0.85 -> 0.75, 0.84 -> 0.75), and they are exactly the three that
    # produced every true hit.
    #
    # The key is the **demo that was read** for the same reason as in
    # ``map_names``, and the orientation is per demo and not per map: an
    # accumulating source would give the same demo a different result
    # depending on which other demos happen to be in the archive (Story 2.9's
    # grounds).
    area_orientation: dict[str, dict[str | None, AreaObservations]] = {}
    # The demo's point cloud (Story 2.14): the stack rule's site groups are
    # derived from it.
    #
    # **The cloud is NOT filtered by lineup**, unlike the sample points. The
    # map is where it is regardless of which team is the subject, and the cloud
    # was collected from the ticks of all the demo's players already during
    # parsing. Filtering would make the division per team, so the same demo
    # would give two teams a different division of areas -- exactly the
    # contradiction that being per demo (Story 2.9) exists to prevent.
    #
    # The key is the **demo that was read** for the same reason as in
    # ``map_names``: the table's own ``map_demo_id`` column may be wrong, the
    # loop's id may not.
    #
    # The groups are derived in the domain and not here: the thresholds are the
    # rule's own and applying them belongs where the rule is.
    point_clouds: dict[str, list[CloudCell]] = {}

    for lineup, demo in sources.demos:
        classified_frames.append(_read_classified(archive, lineup, demo))
        demo_ticks = _read_parsed(archive, demo, "ticks", TICKS)
        tick_frames.append(demo_ticks)
        area_orientation[demo] = _area_orientation(demo_ticks)
        event_frames.append(_read_parsed(archive, demo, "events", EVENTS))
        lineup_frames.append(_read_parsed(archive, demo, "lineups", LINEUPS))
        death_frames.append(_read_parsed(archive, demo, "deaths", DEATHS))
        round_frames.append(_read_parsed(archive, demo, "rounds", ROUNDS))
        map_names[demo] = _read_map_name(archive, demo)
        point_clouds[demo] = _read_point_cloud(archive, demo)

    lineups = set(sources.lineup_keys)
    # The rounds table has two rows per round, one for each team. The
    # three-part key (demo, round, side) already picks our own row, so the
    # filter is **a defence and not the only obstacle**: it keeps the
    # opponent's rows out of the lookup map, so that the key collision check
    # (``armored_by_round``) watches over our own rows only and the change of
    # sides at half time cannot bring two candidates for the same key.
    rounds = pl.concat(round_frames).filter(pl.col("lineup_key").is_in(lineups))
    if rounds.is_empty():
        raise PappascoutError(
            f"Not one round row was found in the demos of team "
            f"{sources.team_key} under its own lineup keys.\n"
            "``parse`` refuses to write an empty rounds table, so an empty "
            "result means the lineup filter did not hit: the rounds tables "
            "were written under different lineup keys than the ones joined to "
            "this team. Parse them again: "
            "uv run pappascout parse <map_demo_id> --force\n"
            "Without this check every round type would report its armour "
            "distribution as nothing but 'the observation is missing' -- that "
            "is, as an observation that there is no observation."
        )
    ticks = pl.concat(tick_frames).filter(pl.col("lineup_key").is_in(lineups))
    events = pl.concat(event_frames).filter(pl.col("lineup_key").is_in(lineups))
    # In the deaths table the filter is over **two columns**: a row belongs to
    # the team if either the victim or the attacker is in its lineup.
    # ``victim_lineup_key`` alone would drop our own kills and
    # ``attacker_lineup_key`` alone our own deaths. A death between two
    # opponents drops out, because neither condition holds. Splitting the rows
    # into deaths and kills is done in ``domain.aggregate.deaths_for``, which
    # sees both columns.
    #
    # ``fill_null(False)`` is **necessary and not decoration**: a death with no
    # attacker (a fall, the bomb) leaves ``attacker_lineup_key`` empty, and in
    # Polars ``is_in`` gives null for null. Without the fill the condition
    # would rest on ``true | null`` being true -- right today, but a silent
    # dependency on a detail of three-valued logic. That row is precisely an
    # own death, whose disappearance would show in the report only as an
    # absence.
    deaths = pl.concat(death_frames).filter(
        pl.col("victim_lineup_key").is_in(lineups).fill_null(False)
        | pl.col("attacker_lineup_key").is_in(lineups).fill_null(False)
    )
    if deaths.is_empty():
        raise PappascoutError(
            f"Not one death was found in the demos of team "
            f"{sources.team_key} in which it was the victim or the attacker.\n"
            "``parse`` refuses to write an empty deaths table, so an empty "
            "result means the lineup filter did not hit: the deaths tables "
            "were written under different lineup keys than the ones joined to "
            "this team. Parse them again: "
            "uv run pappascout parse <map_demo_id> --force\n"
            "Without this check every round type would report 'no own deaths' "
            "-- that is, as an observation that there is no observation."
        )
    # This team's lineups only: the same demo holds both teams' rows, and
    # unfiltered the opponent's clan name would get a vote on the title.
    lineup_rows = (
        pl.concat(lineup_frames)
        .filter(pl.col("lineup_key").is_in(lineups))
        .to_dicts()
    )
    identity = team_identity(lineup_rows)

    team = TeamReport(
        key=sources.team_key,
        # The file name's slug follows the name whenever the name is an
        # observation.
        #
        # The fallback is **the id and not a shared constant**, and the chain
        # is three-part on purpose: a Cyrillic or CJK clan name exists and was
        # observed, but not one ASCII character is left of it. ``team_slug``'s
        # own fallback would then give every such team the same file name
        # ``<timestamp>-team.md``, so the name would disappear and the files
        # would collide with each other. A slug derived from the id is
        # unambiguous. The same rule is written into ``TeamReport``'s contract,
        # so that a report read from disk cannot disagree.
        slug=(
            slugify(identity.display_name or "")
            or slugify(sources.team_key)
            or SLUG_FALLBACK
        ),
        # The name is an observation from the demo (``LINEUPS.clan_name``) and
        # not a derivation. Without an observation the name is the id and the
        # source says so out loud; the report does not invent a substitute from
        # the file name or from the FACEIT id.
        display_name=identity.display_name or sources.team_key,
        display_name_source=(
            "clan_name" if identity.display_name else "team_key"
        ),
        display_name_alternatives=identity.alternatives,
        lineup_keys=sources.lineup_keys,
        # The set of players comes from ``sources.roster`` and not from the
        # name map: a roster row is written even when no name was obtained,
        # because the SteamID is the only traceable value.
        roster=roster_entries(sources.roster, identity.names),
        roster_source="lineups",
    )
    return build_report(
        classified=pl.concat(classified_frames),
        ticks=ticks,
        events=events,
        deaths=deaths,
        rounds=rounds,
        team=team,
        thresholds=thresholds,
        aggregate=aggregate_settings,
        map_pool=league.map_pool,
        map_names=map_names,
        area_orientation=area_orientation,
        point_clouds=point_clouds,
        match_order=index.order,
        match_facts=index.facts,
        generated_at=datetime.now(UTC),
        tool_versions={"pappascout": __version__},
        missing_demos=sources.missing,
    )


def match_order(archive: ArchivePaths) -> list[str]:
    """The archive's matches, **newest first**, for the report's recency mark.

    The one thing ``aggregate`` cannot read from its own tables (Story 4.9).
    ``CLASSIFIED`` carries ``map_demo_id`` and nothing else about the match,
    so the report can count matches without this but cannot say which of them
    is the latest -- and "is this still true?" is the reader's actual
    question. The times come from ``index/matches.json``, which ``discover``
    writes.

    **A missing index is an empty order and not an error.** ``aggregate`` has
    never needed either index, and it must go on working on an archive built
    entirely from hand-imported demos -- two of the three teams in the
    developer's archive are exactly that. What it costs is stated rather than
    hidden: a map of more than one match then carries no recency mark, and
    the model's ``null`` is what says so (:attr:`~pappascout.domain.report
    .PlayersCount.newest`). A **broken** index is not silenced the same way;
    :func:`~pappascout.stages.discover.read_matches_index` raises, and a file
    that is there and unreadable is a fault the user can fix.

    **The shape of the match list is checked in the reader this shares with
    the opponent lookup** (:func:`_match_index_rows`, Story 4.10), and that is
    a line the stage has to hold rather than inherit. ``read_matches_index``
    validates that the file is JSON, that it is an object and that its
    ``schema_version`` is known, and **nothing about ``matches``** -- so
    before this check a list that was ``null`` raised ``TypeError: 'NoneType'
    object is not iterable`` and a list of strings raised ``AttributeError``,
    both of which the command line reports as *"This is a program fault"*.
    That is a regression measured 2026-09-24: at the baseline ``aggregate``
    never read this file, so the same archive aggregated normally. A malformed
    index is the user's to fix and the message has to say so.

    **Malformed raises; valid-but-unusable is skipped**, and the line between
    them is the one that matters here. A match with no ``finished_at`` is
    ordinary data -- FACEIT gives no time for a match that has not finished --
    so it is left out silently. A ``matches`` that is not a list of objects is
    not data at all, and skipping it would turn a broken file into an archive
    that quietly has no recency anywhere.

    A match with no ``finished_at`` is **left out** rather than sorted last.
    The value is the only thing here that orders anything, and a match without
    it has no place -- put last, it would claim to be the oldest, and put
    first it would silence the mark on every group it touches by claiming to
    be the newest. Left out, it makes the order incomplete, which
    :func:`~pappascout.domain.aggregate.newest_match` already answers
    ``null`` to. Measured 2026-09-24: 35 of the developer archive's 66 indexed
    matches have none, and not one of them has a demo in the archive.

    The order is total: ties are broken on the match id, so the same index
    gives the same order from one run to the next.

    **Strings are sorted, and that is sorting time only because of a
    normalisation two layers away.** ``finished_at`` reaches the index through
    :func:`pappascout.adapters.faceit._moment`, which builds every moment from
    epoch seconds as **UTC-aware** (``datetime.fromtimestamp(value, UTC)``),
    and :func:`pappascout.stages.discover._moment`, which writes it with
    ``isoformat()``. Every value is therefore
    ``YYYY-MM-DDTHH:MM:SS+00:00`` -- one offset, fixed width -- and
    lexicographic order is chronological order. Measured 2026-09-24 on the
    developer's index: 31 values, every one 25 characters, every one ending
    ``+00:00``.

    **Nothing enforces that at the point of use.**
    :func:`~pappascout.stages.discover.read_matches_index` returns raw JSON
    and validates no field, so a value written with a local offset would sort
    into the wrong place and the recency mark would be wrong on some blocks
    with nothing to show it. The contract is therefore: **if another source
    ever writes this field, parse before sorting.** It is stated rather than
    guarded because parsing here would need a policy for an unparseable value,
    and today there is exactly one writer.

    Story 4.10 parses the same field in :func:`_finish_date` and **that does
    not settle the contract above** -- asked directly in review, 2026-09-25.
    The parsed value is printed; this sort is still lexicographic on the raw
    string. So with mixed offsets a map's list could carry a date on every
    row, be headed "newest first", and descend out of order: the printed
    dates would be UTC and the order would not. Unreachable while ``discover``
    is the only writer (all 31 dated rows end ``+00:00``), and the fix when it
    is needed is one line -- sort on :func:`_finish_date`'s value instead --
    with one consequence to accept: a row whose ``finished_at`` is present but
    unparseable or naive would then be **unplaced** rather than placed at an
    arbitrary point, which is the better answer and a different one.

    **This list is not deduplicated**, and that mattered one layer away:
    :func:`~pappascout.domain.aggregate.played_maps_for` turned it into a
    mapping and used ``len`` of the mapping as a rank past every real one.
    Measured 2026-09-25, ``[A, A, B]`` becomes ``{A: 1, B: 2}`` -- two keys,
    highest rank two -- so the sentinel collided with a rank in use and a
    dateless demo could sort above a dated one. That function now takes
    ``max(...) + 1`` and no longer depends on this list being distinct.
    """
    return _order_of(_match_index_rows(archive))


def _match_index_rows(archive: ArchivePaths) -> list[Mapping[str, Any]]:
    """The match index's rows, or an empty list when there is no index.

    **The one reader of the file in this stage** (Story 4.10). The order and
    the per-match facts are two readings of the same rows, and taking them
    from two reads would let a ``discover`` land between them: the report
    would then mark recency against one version of the index and name
    opponents from another. So the file is opened here and nowhere else, and
    :func:`read_match_index` is what both readings come out of.

    The shape check is :func:`match_order`'s, moved down a level with it: the
    guarantee the callers need is "every row is a mapping", and it holds for
    both readings or for neither.
    """
    if not archive.matches_index().exists():
        return []
    document = read_matches_index(archive)
    rows = document.get("matches", [])
    if not isinstance(rows, list) or not all(
        isinstance(row, Mapping) for row in rows
    ):
        raise PappascoutError(
            "The archive's match index (matches.json) has no readable match "
            "list, so the matches cannot be put in order.\n"
            "Run again: uv run pappascout discover"
        )
    return list(rows)


def _order_of(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """The rows' matches newest first. See :func:`match_order`."""
    dated = [
        (str(row["finished_at"]), str(row["match_id"]))
        for row in rows
        if row.get("finished_at") and row.get("match_id")
    ]
    return [match for _, match in sorted(dated, reverse=True)]


@dataclass(frozen=True)
class MatchIndexReading:
    """Everything ``aggregate`` takes out of ``index/matches.json``, once.

    Two readings of one file (Story 4.10), kept together because they are one
    reading of the archive: ``order`` decides which of a map's matches is the
    newest, ``facts`` says when each was played and against whom, and a report
    built from two different moments of that file would mark recency against
    matches it did not name.

    Attributes:
        order: The matches whose place in time is known, newest first
            (:func:`match_order`). A match with no ``finished_at`` is **not**
            in it.
        facts: Match key -> the date and the opponent. A match with no
            ``finished_at`` **is** in it, with ``played_on`` ``None``: it is
            in the index and its opponent can still be named. That asymmetry
            is the reason these are two fields and not one.
    """

    order: tuple[str, ...]
    facts: Mapping[str, MatchFact]


def _match_rosters(sources: TeamSources) -> dict[str, frozenset[str]]:
    """Match key -> the subject's players **observed in that match**.

    The comparison set :func:`_opponent_name` recognises the subject by, and
    it is deliberately **not** :attr:`TeamSources.roster` (Story 4.10, found
    in architecture review). The union over the whole sample is who plays for
    this team; the question here is who played *this match*, and the two
    differ the moment a roster changes mid-season.

    **Measured on the developer's archive 2026-09-24:** the subject's report
    roster is 7 players, its FACEIT squad (``roster`` + ``substitutes``) is
    the same 8 people in all four matches, and the 7 are a subset of the 8
    every time. So the union works *here* -- and that is exactly why the
    defect it causes is worth stating rather than waiting for. A player who
    joined in week 3 is in the union, is in week 3's and week 4's squad
    entries and is in **neither of week 1's and week 2's**; the subject would
    then fail to be recognised in the two older matches and both would lose
    their opponent, silently, for a reason that has nothing to do with them.
    Per match, the comparison set is the five who actually played, who are by
    definition registered for that match.

    A match whose demos come from several lineups (a substitution inside a
    ``best_of``) gets the union of those lineups and nothing wider.

    Matches the report has no demo of are **absent** from the result. They
    are still in :attr:`MatchIndexReading.facts` -- the index holds them and
    ``indexed`` says so -- but with no name, because nothing was observed
    against which to recognise a side. No reader asks: the report looks up
    only its own demos' matches.
    """
    found: dict[str, set[str]] = {}
    for lineup, demo in sources.demos:
        found.setdefault(match_of(demo), set()).update(
            sources.lineup_players.get(lineup, frozenset())
        )
    return {match: frozenset(players) for match, players in found.items()}


def read_match_index(
    archive: ArchivePaths, rosters: Mapping[str, frozenset[str]]
) -> MatchIndexReading:
    """Read the match index once and take both readings from it.

    Args:
        archive: The archive's paths.
        rosters: Match key -> the subject's players observed in that match, as
            :func:`_match_rosters` builds it. It is what tells the subject's
            own side of a match from the opponent's; see
            :func:`_opponent_name`. A match that is not a key here gets no
            opponent, which is right: nothing was observed in it.

    Returns:
        The reading. A missing index gives an empty order and no facts, which
        is a legitimate state and not an error -- see :func:`match_order`.
    """
    rows = _match_index_rows(archive)
    return MatchIndexReading(
        order=tuple(_order_of(rows)), facts=_facts_of(rows, rosters)
    )


def _facts_of(
    rows: Sequence[Mapping[str, Any]], rosters: Mapping[str, frozenset[str]]
) -> dict[str, MatchFact]:
    """Match key -> what the index says about it (Story 4.10).

    **Presence in this mapping is itself a fact**: it is what
    :attr:`~pappascout.domain.report.PlayedMap.indexed` records, and it is a
    different statement from an entry whose two values are both ``None``.
    "This match is not in the archive's index" and "the index holds this match
    and says neither when nor against whom" are two different things to tell a
    reader who is deciding whether to go and look.

    A row with no usable ``match_id`` is skipped rather than keyed on an empty
    string, which would collect every such row under one match.

    **This row test is stricter than :func:`_order_of`'s**, and the two are
    written out rather than left to be noticed. That one places any row whose
    ``match_id`` is merely truthy -- a number, a list -- and keys the order on
    ``str(...)`` of it; this one requires a non-empty ``str``. A row with,
    say, a numeric id would therefore be **placed in the order and absent
    from the facts**, and the report would print
    :data:`~pappascout.render.view.MATCH_NOT_INDEXED` about a match the index
    holds: the one place in this story that would state *presence* wrongly
    rather than merely omitting a value. It is unreachable
    while ``discover`` is the only writer -- it writes ``match_id`` from
    FACEIT's string id -- and it is recorded because "unreachable today" is
    the sort of premise this file has already watched expire.

    Measured 2026-09-24 on the developer's index: 66 rows, 66 distinct match
    ids, every row two teams, not one team name empty. A duplicate id would
    leave the last row's values, and nothing here needs a rule for it: the
    file has one writer and it keys on the match.
    """
    facts: dict[str, MatchFact] = {}
    for row in rows:
        match = row.get("match_id")
        if not isinstance(match, str) or not match:
            continue
        facts[match] = MatchFact(
            played_on=_finish_date(row.get("finished_at")),
            opponent=_opponent_name(
                row.get("teams"), rosters.get(match, frozenset())
            ),
        )
    return facts


def _finish_date(value: Any) -> date | None:
    """The day a match finished, or ``None`` when the index does not say it.

    **A date and not a moment**, because the reader's question is how old the
    observation is.

    **It is the UTC date, and it is converted rather than assumed** (Story
    4.10, review). An earlier version took ``.date()`` off the parsed value
    and said in two docstrings that the result was UTC. It was not:
    ``2026-09-21T01:30:00+03:00`` is 22:30 UTC on the **20th** and yielded the
    21st. ``discover`` writes every value ``+00:00`` today
    (:func:`~pappascout.adapters.faceit._moment` builds it with
    ``datetime.fromtimestamp(value, UTC)`` and
    :func:`~pappascout.stages.discover._moment` writes ``isoformat()``, and
    measured 2026-09-24 all 31 dated rows end ``+00:00``), so nothing in the
    archive showed it -- but the format permits an offset and the sentence
    promised a conversion the code did not make.

    **A value with no zone yields ``None``.** Its day depends on a zone
    nobody wrote down, and this story's answer to an unknown is to say it is
    unknown rather than to pick the likely reading: the row then states that
    the date is missing, which is true. It costs nothing today (no naive
    value exists in the index) and the order is unaffected --
    :func:`_order_of` sorts the strings and never comes here.

    **Parsed here although :func:`_order_of` sorts the same field as a
    string.** That is not an inconsistency but the answer to the contract
    :func:`match_order` states and cannot enforce: sorting strings is only
    chronological while every value carries the same offset, and a value that
    broke that assumption would sort wrongly in silence. It cannot produce a
    wrong **date** here -- an unparseable value yields ``None`` and the row
    says the date is missing, which is the safe direction and the same one
    every other absence in this story takes.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC).date()


def _opponent_name(teams: Any, roster: frozenset[str]) -> str | None:
    """The other team's name, or ``None`` when it cannot be told.

    **Whose match this is, is not in the index entry.** A row names two teams
    and nothing in it says which of them the report is about, so the subject
    has to be recognised. The only observation this stage holds is which
    players it saw -- SteamID64s from the demos' ``LINEUPS`` rows -- and the
    index carries each team's ``roster`` and ``substitutes`` in the same ids.

    **The rule, in full, because every clause is load-bearing:** the row must
    name exactly two sides; one of them must hold **every** player the
    subject was observed to field in that match, and the other must hold
    **none** of them; the opponent is that other side. Anything else names
    nobody.

    **The short form of this rule was wrong and shipped** (found in Story
    4.10's architecture review, reproduced here before the fix). It read "the
    opponent is the side that shares no player with the subject", which is
    sound only under an unstated premise -- that exactly one side shares a
    player with us -- and three rows break it:

    * a row naming **one** side, disjoint from us. ``teams`` is built by
      :func:`~pappascout.adapters.faceit._to_match` from the payload's
      faction mapping, keeping only mappings, so a bye or an un-drawn
      opponent yields one entry. Measured: the developer's 66 rows all carry
      two, so this is latent and not live. It named the side that was there.
    * a row naming **three**, one of them disjoint. It named that one.
    * and the one that must not ship: the subject's observed five are all
      outside its own registered squad while one of them is registered with
      the opponent. The disjoint side is then **ours**, and the report printed
      *our own team* as the opponent.

    The two arity cases are closed by the first clause, which is checked and
    not implied -- the code already defends against ``teams`` not being a
    list, an entry not being a mapping and a name that is missing, and arity
    was the one shape it did not test. The third is closed by "holds every
    player": in that row neither side holds all five, so neither can be the
    subject.

    **The comparison set is one match's players and not the team's roster**
    (:func:`_match_rosters`); with the season's union here, a mid-season
    signing would silence the opponent on every earlier match.

    The alternative was the team index (``index/teams.json``), which maps a
    lineup key to a faction id. ``aggregate`` does not read it and the stage's
    own module docstring says so; making it do so would add a second index
    file to a stage that has needed neither, for a lookup the demos already
    answer. Ids and not names, for :class:`~pappascout.domain.report
    .RosterEntry`'s reason: a name changes between matches, an id does not.

    **This is the codebase's third team-identity rule** and the first that
    asks its question the other way round -- the other two ask for
    ``thresholds.team_identity_min_common`` players **in common**
    (:func:`~pappascout.domain.teams.assign_lineup_keys` and
    :func:`~pappascout.domain.aggregate.lineups_of_same_team`), and this one
    asks for all of them on one side and none on the other, with no
    threshold. AD-6's standing note in the spine says two exist and may
    disagree; that note is now short by one, and the memlog entry of
    2026-09-25 records it. See :mod:`pappascout.domain.teams`, where the three
    are named together.

    **What it costs, stated rather than discovered.** A match in which the
    subject fielded somebody the index does not list in either squad is a
    match with no opponent in the report, and the row says so. That is the
    safe direction: the failure this replaces printed a name.

    Measured 2026-09-24 on the developer's archive: the scouted team's eight
    demos all resolve, every one with 7 of its 7 observed players on one side
    and 0 on the other, so the separation is not a near thing on this data.

    Args:
        teams: The entry's ``teams`` value, as the index holds it. Anything
            that is not a list of two objects yields ``None`` -- the entry is
            malformed or is not a match between two sides, and the report says
            the name is missing rather than failing the run, because an index
            row this stage never needed is not worth refusing a report over.
        roster: The subject's players observed **in this match**.

    Returns:
        The opponent's name, or ``None``. **The name is used as the index
        wrote it**, with no check that it is a string: a numeric ``name``
        prints as its digits. That is consistent with every other value read
        out of this file -- it is free text from an external source and the
        report quotes it -- and it is stated because the alternative (refusing
        a name that is not a string) would silently drop a real opponent over
        a type.

        An empty or blank name is ``None``;
        :class:`~pappascout.domain.report.PlayedMap` holds that contract too,
        so the report cannot print a blank that reads as "nobody".
    """
    # Arity first, stated rather than implied -- and **redundant, measured**.
    # With the two counts below in place, removing ``len(teams) != 2`` changes
    # no answer this function gives: a one-side row already fails on
    # ``len(ours) != 1`` and a three-side row on one of the two. The mutation
    # that removes it survives the whole unit suite (860 passed, 2026-09-25),
    # and that is recorded here rather than left for the next reader to
    # discover, because a redundant check that reads as load-bearing is the
    # defect this story's review round found nine of. It is kept because the
    # contract is "a match between two sides" and a reader should meet that
    # here instead of inferring it from two counts further down.
    if not isinstance(teams, list) or len(teams) != 2:
        return None
    ours: list[frozenset[str]] = []
    theirs: list[Mapping[str, Any]] = []
    for entry in teams:
        if not isinstance(entry, Mapping):
            return None
        members = frozenset(
            str(player)
            for key in ("roster", "substitutes")
            for player in (entry.get(key) or [])
        )
        if members.isdisjoint(roster):
            theirs.append(entry)
        else:
            ours.append(members)
    if len(theirs) != 1 or len(ours) != 1:
        return None
    # Every observed player on the side that is not the opponent. An empty
    # ``roster`` cannot reach here: it is disjoint from both sides, so both
    # land in ``theirs``.
    if not roster <= ours[0]:
        return None
    return str(theirs[0].get("name") or "").strip() or None


def match_index_input_id() -> str:
    """The id under which the match index reading is declared in the manifest.

    It names **what is read** and not a file, the way ``classify``'s
    ``match-facts`` does (Story 4.7) and ``parse``'s ``demo/<map_demo_id>``
    did before it: the values are properties of ``index/matches.json`` that no
    other stage reads, and the file holds a great deal this stage never looks
    at.

    **It was ``match-order`` until Story 4.10** and the name had to move with
    the reading: the same input now also identifies each match's date and the
    opponent's name, and an id that went on saying "order" would describe a
    narrower reading than the digest covers -- which is the drift the id
    exists to prevent. The rename costs one re-aggregation per team, on a
    story that bumps the report schema and forces one anyway.

    A literal, and outside the ``classified/`` namespace :func:`_inputs` fills,
    so that neither can be mistaken for the other when a manifest is read
    back.
    """
    return "match-order-and-opponents"


def match_index_input(
    reading: MatchIndexReading, demos: Sequence[tuple[str, str]]
) -> ManifestInput:
    """The match index reading as a manifest input, narrowed to what is read.

    **The defect this closes** (Story 4.9, found in review): ``aggregate``
    began reading ``index/matches.json`` and declared nothing, which is Story
    4.7's defect in the stage next door. The ordinary sequence makes it bite:
    the archive holds 35 of 66 indexed matches with no ``finished_at``, so a
    report aggregated today has no recency mark; ``discover`` later fills the
    times in; and aggregating again **skips**, leaving the report unmarked
    while telling the user it is up to date. Only ``--force`` recovered it.

    **Identified by the values, not by the file**, and that is the same rule
    and the same file as 4.7's: ``discover`` rewrites ``matches.json`` on every
    run and stamps a wall-clock ``generated_at`` into it, so a whole-file
    digest would re-aggregate every team after every ``discover`` and cascade
    into ``render``.

    **The declaration was widened in Story 4.10, in the same commit as the
    reading.** Until then this stage took the order out of the file and
    nothing else; now it also takes each of its own matches' **date** and
    **opponent's name**, and both are printed. Left as it was, the digest
    would have missed a class of change it is there to catch: a match whose
    ``finished_at`` moves to another day without changing its place among the
    report's own matches, and an opponent that renames itself on FACEIT.
    Either one leaves the digest unmoved, ``aggregate`` skipping, and a
    printed line stating something the index no longer says.

    What this stage reads out of that file is **the order among its own
    matches, and for each of them the date and the opponent** -- nothing
    else reaches the report, because
    :func:`~pappascout.domain.aggregate.newest_match` and
    :func:`~pappascout.domain.aggregate.played_maps_for` both look up only
    the keys of the demos the report is built from. So the digest is two
    lists:

    ``placed``
        The report's own match keys in the index's order. It moves when one of
        them changes place relative to another, which is exactly when a map's
        newest match can change.
    ``facts``
        One row per own match key, sorted by the key: the key, and then the
        date and the opponent, or ``null`` where the index does not hold the
        match at all. It moves when a date or a name changes, and when a match
        enters or leaves the index.

    **``unplaced`` is gone and is not missing.** Story 4.9's digest listed the
    own keys the index does not place, to catch a ``finished_at`` appearing or
    disappearing. Every one of those keys is in ``facts`` now, with its
    ``played_on`` -- so the old list is derivable from the new one, and
    keeping it would be the same value twice in the same digest.

    **It covers more than "what this stage reads out of that file", and the
    extra is worth naming.** ``facts`` digests the **resolved** opponent and
    not the row it was resolved from, so it also moves when the *subject's*
    side of the row changes: a demo joining or leaving the sample changes
    :func:`_match_rosters`, which can change which side
    :func:`_opponent_name` recognises as ours, and the digest follows. The
    demo set is in :func:`_inputs` as well, so that particular change is
    caught twice -- but a digest of the raw row would not have caught it at
    all, and the value the report prints is the resolved one.

    **Global position is deliberately not in it.** Digesting each key's rank
    in the whole list would move whenever any unrelated match gained a finish
    time -- the commonest thing ``discover`` does -- and re-run every team for
    a value none of their numbers depend on. Nor is any other match's date or
    opponent: the report names its own matches only.

    The demo set is **not** in this digest and does not need to be: a demo
    joining or leaving changes :func:`_inputs`, which carries one entry per
    demo. The two together identify the reading completely.

    Args:
        reading: The match index as :func:`read_match_index` read it.
        demos: The ``(lineup, map_demo_id)`` pairs the report is built from.

    Returns:
        The input. **Never ``None``**, unlike ``classify``'s: a missing index
        is a legitimate reading here (every key unindexed) and a malformed one
        has already raised in :func:`_match_index_rows`, so there is no state
        left in which the input cannot be identified.
    """
    own = {match_of(demo) for _, demo in demos}
    facts = []
    for key in sorted(own):
        fact = reading.facts.get(key)
        facts.append(
            [key, None]
            if fact is None
            else [
                key,
                fact.played_on.isoformat() if fact.played_on else None,
                fact.opponent,
            ]
        )
    return ManifestInput(
        result_id=match_index_input_id(),
        sha256=compute_params_hash(
            {
                "placed": [key for key in reading.order if key in own],
                "facts": facts,
            }
        ),
    )


def _read_classified(
    archive: ArchivePaths, lineup: str, map_demo_id: str
) -> pl.DataFrame:
    path = archive.resolve(classified(lineup, map_demo_id))
    df = _read_parquet(path, map_demo_id)
    validate(
        df,
        CLASSIFIED,
        "classified",
        advice=(
            "The table was classified with an older version of the program. "
            f"Classify it again: uv run pappascout classify {map_demo_id} "
            f"--team {lineup} --force"
        ),
    )
    return _in_schema_order(df, CLASSIFIED)


def _area_orientation(
    ticks: pl.DataFrame,
) -> dict[str | None, AreaObservations]:
    """Area -> the side orientation's observations from one demo.

    The source is the **unfiltered** sample point table, that is, both teams'
    rows; see the comment at the call site for why that is a measured
    condition and not a preference.

    Four restrictions, and every one of them is a definition and not tidying:

    * ``sample_kind == "time"`` -- the moment of first contact is different on
      every round, so its rows would weight the orientation towards the rounds
      on which somebody happened to shoot early.
    * ``is_alive`` -- a dead player is not in an area. The same rule as in the
      player counts (``positions_for``), so the orientation and the setup are
      computed from the same set.
    * ``side`` is ``T`` or ``CT`` -- **this is a restriction on the
      denominator, not on the numerator.** Without it ``pl.len()`` would count
      in a row whose side is not known, but ``side == "T"`` could not count it
      as T: an unknown side would press the T share down and could drop a T
      area below the threshold. That share is precisely what both rules rest
      on.
    * ``area`` is not empty -- an unnamed area cannot be either side's area,
      and an anomaly "in an unknown area" could not be told.

    ``fill_null(False)`` on being alive is **necessary and not decoration**: in
    Polars ``null`` as a truth value is not false but null, and it would carry
    the row through the filter or out of it depending on how the conditions are
    combined. A missing observation is not an observation that the player was
    alive.

    The area name is normalised with the **same function** as the presence row
    (:func:`~pappascout.domain.sampling.normalize_area`). Without it
    ``" Lobby "`` would be a different area in the orientation than in the
    presence, and the rules would fall silent on that area; ``""``, for its
    part, would get through into the T areas and produce an anomaly for an
    unnamed area.

    Returns:
        Area -> :class:`~pappascout.domain.sampling.AreaObservations`. An empty
        map is a valid result: a demo with not one named area in its sample
        points gives no orientation to any area -- and it is not guessed at
        then. The result ends up in the report's coverage figure
        (``anomaly_scan.demos_without_orientation``), so an empty one does not
        disappear silently.
    """
    grouped = (
        ticks.filter(
            (pl.col("sample_kind") == TIME_SAMPLE)
            & pl.col("is_alive").fill_null(False)
            & pl.col("side").is_in(list(SIDES))
            & pl.col("area").is_not_null()
        )
        .group_by("area")
        .agg(
            pl.len().alias("observations"),
            # The side is the row's own observation; "T" is a member of the
            # TICKS table's value set (``constants.SIDES``) and not a
            # derivation.
            (pl.col("side") == "T").sum().alias("t"),
        )
    )
    found: dict[str | None, AreaObservations] = {}
    for row in grouped.iter_rows(named=True):
        area = normalize_area(row["area"])
        if area is None:
            continue
        observed = AreaObservations(
            t=int(row["t"]), total=int(row["observations"])
        )
        previous = found.get(area)
        # Two spellings of the same area (``"Lobby"`` and ``" Lobby "``) are
        # one area, so their observations ARE ADDED TOGETHER. The alternative
        # would be to drop one, which would compute the orientation's share
        # from a subset and could turn the threshold.
        found[area] = (
            observed
            if previous is None
            else AreaObservations(
                t=previous.t + observed.t,
                total=previous.total + observed.total,
            )
        )
    return found


def _read_point_cloud(
    archive: ArchivePaths, map_demo_id: str
) -> list[CloudCell]:
    """The cells of the demo's point cloud as the rule's records.

    Two columns are left out, and each is a decision and not an omission:

    * ``map_demo_id`` is the caller's bookkeeping; the caller uses the demo it
      **read** as its key, not the table's own column (the same grounds as for
      :func:`_read_map_name`).
    * ``observations`` **does not weight** the area's centre point. The cell
      median is a measured condition: the game's ``m_szLastPlaceName`` is the
      *last named* area, so an observation-weighted mean pulls the centre point
      to where the players stood longest -- in
      ``Ancient_vs_kaljukostaja``'s CT spawn, into cells named ``BombsiteB``.
      Carrying the column this far would invite weighting by it.

    An empty cloud is a **valid result** and not an omission: no site groups
    come out of it, and it is recorded in the coverage
    (``AnomalyScan.demos_without_site_groups``). A missing **file** is a
    different matter, and :func:`_demo_unusable` has already stopped it.
    """
    df = _read_parsed(archive, map_demo_id, "callouts", CALLOUT_CLOUD)
    return [
        CloudCell(
            area=str(row["area"]),
            cell_x=int(row["cell_x"]),
            cell_y=int(row["cell_y"]),
            cell_z=int(row["cell_z"]),
        )
        for row in df.iter_rows(named=True)
        # The contract says an unnamed area does not get into the cloud at
        # all, but a table written with an older version has not been through
        # that rule. ``str(None)`` would make it an area named "None".
        if normalize_area(row["area"]) is not None
    ]


def _read_map_name(archive: ArchivePaths, map_demo_id: str) -> str | None:
    """The map's name from the demo's ``match.parquet`` table, or ``None``.

    By the contract the ``parse`` stage enforces, the table has exactly one
    row. The check is repeated here because the file that was read may have
    been written by an older version of the program: the contract is enforced
    by the stage that writes, and a reader must not rest on the writer having
    been this version.

    The value returned is **the name or its absence**, not a table: the caller
    needs the frame for nothing, and lifting the row out here keeps the
    contract's check in one place.
    """
    df = _read_parsed(archive, map_demo_id, "match", MATCH)
    if df.height != 1:
        raise PappascoutError(
            f"The match table of demo {map_demo_id} has {df.height} rows, "
            "although there must be exactly one.\n"
            "The table describes one match. Zero rows would mean the map's "
            "name was not observed -- but that is a different matter from the "
            "observation ``null``, and out of two rows the name would be "
            "picked by row order.\n"
            f"Parse it again: uv run pappascout parse {map_demo_id} "
            "--force"
        )
    name = df["map_name"][0]
    return None if name is None else str(name)


def _read_parsed(
    archive: ArchivePaths, map_demo_id: str, table: str, schema: Schema
) -> pl.DataFrame:
    path = archive.resolve(parsed_table(map_demo_id, table))
    df = _read_parquet(path, map_demo_id)
    validate(
        df,
        schema,
        table,
        advice=(
            "The table was parsed with an older version of the program. Parse "
            f"it again: uv run pappascout parse {map_demo_id} --force"
        ),
    )
    return _in_schema_order(df, schema)


def _in_schema_order(df: pl.DataFrame, schema: Schema) -> pl.DataFrame:
    """Put the columns into the order the contract gives.

    :func:`~pappascout.domain.schemas.validate` accepts any column order -- the
    contract is about names and types. ``pl.concat`` does not: two frames with
    the same columns in a different order fail with a ``ShapeError``, a raw
    Polars exception that tells the user nothing. An archive written with two
    different versions is exactly the situation in which that can happen.
    Ordering removes the whole failure mode instead of rewording it.
    """
    return df.select(list(schema))


def _read_parquet(path: Path, map_demo_id: str) -> pl.DataFrame:
    try:
        return pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise PappascoutError(
            f"The table {path} could not be read: {exc}\n"
            f"Run the stage again for demo {map_demo_id}."
        ) from exc


def _read_report(path: Path) -> Report | None:
    """Read a report written earlier, or ``None`` if it cannot be used.

    The skip must not rest on the manifest alone, and there are two checks.

    **The schema version.** A ``report.json`` written with an old version can
    validate field by field against the current model and still mean something
    else. The comparison is the same as in :meth:`Manifest.read`: a different
    version = an unknown file, and the stage is run again. Without this the
    skip would return the old result's figures and claim them as this run's
    result.

    **The kind of exception.** The sample's sum checks raise
    :class:`~pappascout.errors.AggregateError`, which inherits from
    ``PappascoutError`` **and not from ValueError**. ``ValueError`` alone would
    leave it uncaught, so an old invalid report would break the skip branch on
    every run instead of the stage writing a new one in its place. All three
    kinds -- a read error, a validation error and a sum error -- mean the same
    thing here: the result cannot be used, so it is computed again.
    """
    if not path.is_file():
        return None
    try:
        report = Report.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, PappascoutError):
        return None
    if report.schema_version != REPORT_SCHEMA_VERSION:
        return None
    return report


# -- The manifest ----------------------------------------------------------------


def _inputs(
    archive: ArchivePaths, demos: Sequence[tuple[str, str]]
) -> list[ManifestInput]:
    """The inputs: the ``classify`` manifest of every demo taken along."""
    inputs: list[ManifestInput] = []
    for lineup, demo in demos:
        path = archive.resolve(classified_manifest(lineup, demo))
        manifest = Manifest.read_if_exists(path)
        if manifest is None:
            raise PappascoutError(
                f"The classification manifest was not found at {path}, so the "
                "aggregation's input cannot be recognised.\n"
                f"Run: uv run pappascout classify {demo} --team {lineup}"
            )
        inputs.append(
            ManifestInput(
                result_id=manifest.result_id, sha256=manifest.fingerprint()
            )
        )
    return inputs


#: The ``[thresholds]`` and ``[league]`` keys that **really change** the
#: result of the aggregation. Only these go into the parameter hash.
#:
#: Why a named list and not the whole section (unlike in ``classify``):
#: ``[thresholds]`` is the classification's section and holds some thirty
#: threshold values, of which aggregation reads only a part. Hashing the whole
#: section would invalidate every report whenever any classification threshold
#: changes -- and it is exactly then that the classification is run again,
#: which already shows in the inputs' ids. The same work would therefore be
#: done twice.
#:
#: The list is kept from going stale by the test
#: ``test_every_setting_the_stage_reads_is_in_the_params_hash``: it reads from
#: the source which fields the stage and its domain functions read, and
#: compares them against this list.
#:
#: **One deliberate exception.** :func:`~pappascout.domain.aggregate.classify_thresholds`
#: reads six of the classification's thresholds by name (``getattr``) in order
#: to compare them against the ones the rounds were really classified with.
#: They are not on this list and they do not belong on it: they change not one
#: figure in the report but **stop the run** if the classification is stale.
#: And if the user runs the classification again, its manifest's id changes --
#: so the aggregation is run again because of the input and not because of a
#: parameter.
HASHED_THRESHOLD_KEYS: tuple[str, ...] = (
    "small_sample_rounds",
    "team_identity_min_common",
    # The anomaly thresholds (Story 2.5). Without them adjusting a threshold
    # would not re-run the aggregation, and the report would keep the old
    # anomalies -- the same defect as in Story 1.8. The stage has a guard of
    # its own for this
    # (``test_every_setting_the_stage_reads_is_in_the_params_hash``), which
    # reads the fields that are read from the source.
    "advance_t_share",
    "advance_area_min_observations_per_point",
    "advance_max_sample_s",
    "advance_min_players",
    "crunch_min_players",
    "crunch_min_sources",
    # The crunch's look-back (Story 4.6). It decides which sample point is
    # read as a player's source area, so a changed value changes which rounds
    # the rule fires on -- and without it here the report would keep the
    # anomalies measured with the old reach.
    "crunch_lookback_s",
    # The stack rule's three thresholds (Story 2.14). The latter two are the
    # story's quietest trap: they change not one rule but its INPUT (the site
    # groups from the demo's point cloud), so forgetting them would leave in
    # the report anomalies computed with the old division of areas -- and
    # whoever adjusted the threshold would see the effect only with
    # ``--force``.
    "stack_min_players",
    "stack_group_margin",
    "stack_site_separation_min",
    # The stack's definition itself (Story 4.4): the concentration bound and
    # the setup sample point. Both decide which rounds the rule fires on, so
    # without them a changed definition would leave the old anomalies in the
    # report. ``stack_sample_s`` is the quieter of the two: it is the only
    # threshold here whose change is invisible everywhere else in the
    # settings, because the rule stopped sharing ``advance_max_sample_s``.
    "stack_max_areas",
    "stack_sample_s",
    # The stacked-map branch (Story 4.3). These three decide whether a map's
    # sites are told apart by plan distance or by height, so they change the
    # area division itself -- the same quiet trap as the two above, and for
    # the same reason they must invalidate the aggregation.
    "site_floor_band_trim",
    "site_floor_gap_ratio",
    "site_floor_z_weight",
    "site_bridge_void_share",
)
HASHED_LEAGUE_KEYS: tuple[str, ...] = ("map_pool",)


def _params_hash(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    aggregate_settings: AggregateSettings,
) -> str:
    """AD-3: a parameter hash only of what affects this stage's result.

    ``[aggregate]`` is included **whole**, because it is this stage's own
    section: every value in it is by definition for this stage, and the list
    cannot go stale. From the ``[thresholds]`` and ``[league]`` sections only
    the named keys are taken (:data:`HASHED_THRESHOLD_KEYS`,
    :data:`HASHED_LEAGUE_KEYS`).

    ``[parse]`` is left out on purpose: the sample points are read from the
    table as they are, and changing them cannot affect this stage without the
    parsing being run again -- and that already shows in the inputs' ids.
    """
    return compute_params_hash(
        {
            "thresholds": {
                key: getattr(thresholds, key) for key in HASHED_THRESHOLD_KEYS
            },
            "league": {key: getattr(league, key) for key in HASHED_LEAGUE_KEYS},
            "aggregate": aggregate_settings.model_dump(mode="json"),
        }
    )


# -- The output's figures --------------------------------------------------------


def _alive_at_max(anomaly: Anomaly) -> int | None:
    """The players alive at the sample point the row's ``players_max`` is from.

    ``None`` for the two orientation rules: neither counts the players alive,
    and an invented denominator would not be distinguishable in the output from
    a measured one.

    The point is looked up **at the maximum** and not separately: the row reads
    "5/5", and its two figures have to be from the same moment. On a tie (two
    points with the same player count) the first in time order is chosen, so
    that the output is the same from one run to the next.
    """
    for entry in anomaly.rounds:
        for point in entry.points:
            if point.players == anomaly.players_max and point.alive is not None:
                return point.alive
    return None


def _stats(report: Report, sources: TeamSources) -> dict[str, Any]:
    """The figures ``cli`` shows the user."""
    return {
        "team_key": report.team.key,
        "lineup_keys": list(report.team.lineup_keys),
        "display_name": report.team.display_name,
        "display_name_source": report.team.display_name_source,
        "display_name_alternatives": list(report.team.display_name_alternatives),
        # The roster into the output as pairs: the name and the id side by
        # side, neither on its own. The name may be ``None``, and that is an
        # observation.
        "roster": [
            {"player_id": entry.player_id, "display_name": entry.display_name}
            for entry in report.team.roster
        ],
        "demos": report.sample.demos,
        "rounds": report.sample.rounds,
        # The buckets are read from one list, so that the third bucket cannot
        # be left out of the output while it is in the structure.
        "sample": {
            name: {
                "demos": getattr(report.sample, name).demos,
                "rounds": getattr(report.sample, name).rounds,
            }
            for name in LEAGUE_BUCKETS
        },
        "unclassified": report.unclassified_rounds,
        "unpaired_detonations": report.unpaired_detonations,
        # The anomalies into the output, because they are the epic's most
        # valuable product: without this line the user sees the effect of
        # adjusting a threshold only by opening the report. The coverage is on
        # the line for the same reason as in the report -- zero anomalies is an
        # observation only about what was examined.
        "anomalies": [
            {
                "rule": entry.rule,
                "map_name": entry.map_name,
                "side": entry.side,
                "round_types": list(entry.round_types),
                "area": entry.area,
                "players_max": entry.players_max,
                # The players alive at the sample point ``players_max`` is
                # from -- for stack only, ``None`` for the others. Without it
                # the output's row would set "4 players", and the whole
                # claim of the rule is that the figure means nothing on its
                # own.
                "alive_at_max": _alive_at_max(entry),
                "n": entry.n,
                "m": entry.m,
            }
            for entry in report.anomalies
        ],
        "anomaly_scan": {
            "rules": list(report.anomaly_scan.rules),
            "rules_deferred": list(report.anomaly_scan.rules_deferred),
            "rounds_scanned": report.anomaly_scan.rounds_scanned,
            "crunch_rounds": report.anomaly_scan.crunch_rounds,
            "advance_rounds": report.anomaly_scan.advance_rounds,
            "stack_rounds": report.anomaly_scan.stack_rounds,
            "demos_without_orientation": list(
                report.anomaly_scan.demos_without_orientation
            ),
            "demos_without_site_groups": list(
                report.anomaly_scan.demos_without_site_groups
            ),
        },
        "classify_thresholds": dict(report.classify_thresholds),
        "maps": [
            {
                "map_name": m.map_name,
                "map_name_source": m.map_name_source,
                "demos": m.sample.demos,
                "rounds": m.sample.rounds,
                "sides": [
                    {
                        "side": s.side,
                        "round_types": {
                            rt.round_type: rt.sample.rounds
                            for rt in s.round_types
                        },
                        "small_samples": [
                            rt.round_type for rt in s.round_types if rt.small_sample
                        ],
                    }
                    for s in m.sides
                ],
            }
            for m in report.maps
        ],
        "missing_demos": [
            {"match": m.match, "reason": m.reason} for m in report.missing_demos
        ],
    }
