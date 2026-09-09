"""``select`` -- the pipeline's second stage at the head: which maps are in the sample.

``discover`` knows who belongs to a team. It does not know **in which matches
they played as that team**. Without that knowledge the sample would just as
readily take in the opponent's solo games with random players, and the report
would describe something other than the team you are playing against. This
stage writes that decision out as a file::

    index/selections/<team_key>.json

A row is **per MapDemo, not per match**, and it says four things: whether the
map is eligible for the sample (``roster_ok``), why (``roster_reason``), which
roster class (``roster_class``) and whether it is a league match
(``is_league``). The file has **one writer, and it is this stage**;
``discover``'s two indexes are read-only to it, and they are read with
``discover``'s own readers
(:func:`~pappascout.stages.discover.matches_from_index`,
:func:`~pappascout.stages.discover.teams_from_index`) rather than by unpacking
the JSON by hand.

Why the threshold is judged per map
-----------------------------------
Pappaliiga allows two substitutions **between maps**, so the same match can be
a full standing lineup on map 1 and four regulars plus one outsider on map 2. A
threshold judged per match would either accept or reject both, and either would
be wrong about one of the maps.

Why the class is a prediction before the parse and an observation after it
--------------------------------------------------------------------------
FACEIT's roster is **per match** (measured 2026-09-04,
``mittaus-faceit-aineisto.md`` chapter 7), so before the demo the map's lineup
is the best guess at the match roster. After the demo it is an observation. The
row says which of the two it is (``roster_source``), exactly as
``map_name_source`` does in Story 2.11 -- and when both are known, **the
observation wins and the difference is stated**.

Three different reasons for there being no row -- and not one
-------------------------------------------------------------
This is the place where a summary lies most easily, so the reasons are counted
separately and told separately:

**The match has not been played.**
    Measured 2026-09-04: ``map_picks`` is empty in 60 matches out of 66, that
    is, in exactly those that have not been played. There are no maps, so
    there are no MapDemos, and the id ``{match_id}-0`` would point at a file
    that cannot exist. A scheduled match is not "the selection is waiting" but
    "does not exist yet".

**The match has been played, but the veto data is missing.**
    The port's own contract says that an empty ``map_picks`` means "no veto
    data", **not** "no maps". A played match without veto data is therefore
    missing data and not an unplayed match -- and lumping it in with the
    previous one would tell the user a reason that is not true.

**The team is not in the match.**
    Not mentioned: this is the whole division's match list, and 55 matches out
    of 66 always belong to somebody else.

Why a map can stay outside the sample even though it is in the veto data
------------------------------------------------------------------------
A three-map match often ends after two, but the veto still holds three names.
:func:`~pappascout.domain.selection.guaranteed_maps` says up to which map is
still certain; the ones after it get a row but no place in the sample, until a
demo proves them played. In the present regular season (``best_of`` = 2) not a
single row stays uncertain, but the playoffs are BO3.

Why the stage has no manifest
-----------------------------
For the same reason as ``discover``: the input is a match list that changes
every day, and the result changes also when a demo is parsed and the
prediction turns into an observation. A skip would save reading a file and
would cost exactly what the command is run for.

What this stage does **not** do
-------------------------------
It does not download demos (Story 3.4), does not write into the
``aggregates/`` or ``classified/`` directories, and does not touch the veto,
the ban or the pick (Epic 4). It also **does not wire** the ``is_league`` and
``roster_class`` values into the ``classify`` stage: that would change the
archive's classification and the reports' text, and it is a story of its own.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.paths import ArchivePaths, parsed_table, selection
from pappascout.domain.models import LeagueSettings, ThresholdSettings
from pappascout.domain.selection import (
    MapCandidate,
    MapSelection,
    counts,
    guaranteed_maps,
    map_demo_id,
    select_maps,
    sort_key,
)
from pappascout.domain.teams import Team
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult
from pappascout.stages.discover import (
    IndexedMatch,
    IndexedMatchTeam,
    matches_from_index,
    read_indexes,
    resolve_team,
    teams_from_index,
)

__all__ = [
    "STAGE",
    "SCHEMA_VERSION",
    "MAX_LISTED_REJECTIONS",
    "run",
    "read_selection",
]

STAGE = "select"

#: The version of the selection file's format. Its own version and not
#: ``discover``'s, because the file has a different writer and a different
#: life cycle: the match index's format can change without the selection
#: row's format changing, and the other way round.
SCHEMA_VERSION = 1

#: How many rejected maps travel with their reasons in ``StageResult.stats``.
#:
#: ``stats`` is a **summary and not a payload**: it ends up in the command's
#: output as it stands, and an unbounded listing would produce a screenful of
#: text on a division-sized sample. The whole listing is always in the
#: selection file, and the output says how many were left unshown.
MAX_LISTED_REJECTIONS = 10

#: How many matches without veto data are named by id in the note.
MAX_LISTED_MATCHES = 3


@dataclass
class _Skipped:
    """The team's matches that produced no row -- **one reason at a time**.

    A single shared counter would say how many were lost but not why, and the
    summary would have to guess the reason. That guess was precisely the first
    version's mistake: a played match without veto data was explained as an
    unplayed one.
    """

    #: The match is on the schedule but has not been played: no maps exist.
    not_played: int = 0
    #: Played matches whose veto data is missing. The maps were played, but we
    #: do not know which -- **missing data**, not an unplayed match. The ids
    #: and not the count, so that the note can name them.
    no_veto: list[str] = field(default_factory=list)


def run(
    league: LeagueSettings,
    archive: ArchivePaths,
    team: str,
    *,
    thresholds: ThresholdSettings,
) -> StageResult:
    """Select the team's MapDemos by the roster threshold and write the selection file.

    Args:
        league: The ``[league]`` section; ``championship_ids`` is read from it,
            and ``is_league`` is settled against that. **Nothing is inferred
            from the name**: ``competition_name`` is a string written by a
            human.
        archive: The archive's paths.
        team: The team's name, an unambiguous part of it, or its id.
            Mandatory: the selection file is per team, so without a team there
            is no file to write.
        thresholds: The ``[thresholds]`` section as a keyword parameter;
            ``roster_size`` and ``roster_min_regulars`` are read from it. A
            keyword for the same reason as in ``discover``: two pydantic
            sections in a row would pass through swapped positionally without
            anything remarking on it.

    Returns:
        A :class:`~pappascout.stages.StageResult`. ``stats`` gives the number
        of rows, the eligible and the rejected, the class distribution, the
        distribution of the lineup's source, and how many matches were left
        without rows **for each of the two reasons**.

    Raises:
        ~pappascout.errors.PappascoutError: If the indexes are not there (the
            message tells the user to run ``discover``), if they are from
            different runs, if they are broken, or if the name does not hit
            one single team (the message lists the known ones).
        ~pappascout.errors.SettingsError: If the ``[thresholds]`` thresholds
            do not produce a known roster class.
    """
    started = time.perf_counter()
    generated_at = datetime.now(UTC)

    matches_document, teams_document = read_indexes(archive)
    matches = matches_from_index(matches_document)
    teams = teams_from_index(teams_document)
    found = resolve_team(teams, team)

    names = _nicknames(teams)
    candidates, skipped, veto_notes = _candidates(
        matches, found, league.championship_ids, archive
    )
    rows = tuple(
        sorted(
            select_maps(
                candidates,
                roster=found.player_ids,
                roster_size=thresholds.roster_size,
                roster_min_regulars=thresholds.roster_min_regulars,
                names=names,
            ),
            key=sort_key,
        )
    )

    relative = selection(found.team_key)
    _write(
        archive.resolve(relative),
        _document(
            rows,
            team=found,
            league=league,
            thresholds=thresholds,
            generated_at=generated_at,
            index_generated_at=matches_document.get("generated_at"),
        ),
    )

    notes = _notes(rows, found, skipped) + veto_notes
    stats = _stats(rows, found, thresholds, skipped, generated_at)
    stats["notes"] = notes

    return StageResult(
        stage=STAGE,
        unit=found.team_key,
        status="ok",
        # Never a skip: see the module docstring.
        skipped=False,
        outputs=(relative,),
        manifest_path=None,
        # ``reason`` is one string in the contract, but there can be many
        # notes. They are also in ``stats["notes"]`` separately, so that the
        # command prints each on its own line and none disappears under
        # another.
        reason=" ".join(notes) if notes else None,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def read_selection(archive: ArchivePaths, team_key: str) -> dict[str, Any]:
    """Read the team's selection file and check its format.

    The reader is here rather than in the caller for the same reason as
    :func:`~pappascout.stages.discover.read_indexes`: ``schema_version`` is
    written, so it also has to be read -- otherwise the version would be a
    field nobody checks, and every consumer (Story 3.4, ``aggregate``) would
    infer the format from which fields exist.

    Args:
        archive: The archive's paths.
        team_key: The canonical team id.

    Returns:
        The selection file as a dictionary.

    Raises:
        PappascoutError: If the file is not there, is not valid JSON, or its
            ``schema_version`` is unknown. The message tells the user to run
            ``select``.
    """
    path = archive.selection(team_key)
    if not path.is_file():
        raise PappascoutError(
            f"The archive is missing the selection file for team {team_key} "
            f"({path.name}).\n"
            f'Run first: uv run pappascout select --team "{team_key}"'
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PappascoutError(
            f"The archive's selection file ({path.name}) cannot be read: {exc}\n"
            f'Run again: uv run pappascout select --team "{team_key}"'
        ) from exc
    if not isinstance(document, dict):
        raise PappascoutError(
            f"The archive's selection file ({path.name}) is not an object of "
            "the expected shape."
        )
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PappascoutError(
            f"The archive's selection file is in format {version!r}, but this "
            f"version can read only format {SCHEMA_VERSION}.\n"
            f'Run again: uv run pappascout select --team "{team_key}"'
        )
    return document


# -- Gathering the candidates ------------------------------------------------


def _candidates(
    matches: Sequence[IndexedMatch],
    team: Team,
    championship_ids: Sequence[str],
    archive: ArchivePaths,
) -> tuple[tuple[MapCandidate, ...], _Skipped, list[str]]:
    """Gather the team's MapDemos from the match index.

    A match the team is not in is skipped without being counted: most of the
    whole division's match list always belongs to somebody else. The other two
    skips are the team's **own** matches, and they are counted separately
    (:class:`_Skipped`).

    Returns:
        ``(candidates, skipped, notes)``. The notes concern veto data that
        contradicts the match's length.
    """
    league_ids = frozenset(championship_ids)
    own = frozenset(team.faction_ids) | {team.team_key}

    candidates: list[MapCandidate] = []
    skipped = _Skipped()
    notes: list[str] = []

    for match in matches:
        side = _own_side(match, own)
        if side is None:
            continue
        if not match.played:
            skipped.not_played += 1
            continue
        if not match.map_picks:
            # The port's contract: an empty map_picks is "no veto data", not
            # "no maps". A played match without a veto is missing data, and
            # explaining it as unplayed would be a false claim.
            skipped.no_veto.append(match.match_id)
            continue

        certain = guaranteed_maps(match.best_of)
        if match.best_of is not None and len(match.map_picks) > match.best_of:
            notes.append(
                f"The veto data for match {match.match_id} holds "
                f"{len(match.map_picks)} maps, although the match is a "
                f"BO{match.best_of}."
            )

        match_roster = frozenset(side.roster)
        is_league = match.competition_id in league_ids
        for index, map_name in enumerate(match.map_picks):
            unit = map_demo_id(match.match_id, index)
            players, note = _observed(archive, unit, team.player_ids)
            candidates.append(
                MapCandidate(
                    map_demo_id=unit,
                    match_id=match.match_id,
                    map_index=index,
                    map_name=map_name,
                    is_league=is_league,
                    certainly_played=certain is None or index < certain,
                    match_roster=match_roster,
                    observed_players=players,
                    observation_note=note,
                )
            )
    return tuple(candidates), skipped, notes


def _own_side(match: IndexedMatch, own: frozenset[str]) -> IndexedMatchTeam | None:
    """The side of the match that is this team -- or ``None``.

    The match is made **by id and not by name**: a name is an observation that
    can change mid-season, and two teams with the same name would hit each
    other. There are many ids, because a team can have several source ids (a
    new season brings a new one) and because the canonical ``team_key`` is one
    of them.
    """
    for side in match.teams:
        if side.faction_id in own:
            return side
    return None


# -- The observation from the demo -------------------------------------------


def _observed(
    archive: ArchivePaths, unit: str, roster: frozenset[str]
) -> tuple[frozenset[str] | None, str | None]:
    """The map's lineup from the demo, and if there is none, **why not**.

    The source is ``parsed/<map_demo_id>/lineups.parquet``, the same as in
    ``discover``'s bridge to the archive: its set of players is exactly the
    one ``lineup_key`` was computed from.

    **The table holds both teams**, so the right lineup has to be chosen. The
    rule is one and the same set operation as the threshold: the lineup that
    has the most in common with the standing roster.

    Returns:
        ``(players, note)``. The players are ``None`` when there is no
        observation, and the note then gives the reason -- **except** when the
        demo simply has not been parsed, which is an expected state and not an
        anomaly. The four other reasons are all anomalies, and each of them
        ends up in the row's reason instead of demoting the observation to a
        prediction without a trace:

        * the table exists but cannot be read,
        * the table holds no valid row at all,
        * no lineup has a single player in common with the standing roster
          (the demo is there, but this team is not in it), or
        * two lineups are equally close -- drawing lots would attach the
          opponent's lineup to this team.
    """
    try:
        path = archive.resolve(parsed_table(unit, "lineups"))
    except PappascoutError:
        return None, None
    if not path.is_file():
        return None, None
    try:
        frame = pl.read_parquet(path, columns=["lineup_key", "player_id"]).unique()
    except (OSError, pl.exceptions.PolarsError) as exc:
        return None, (
            "Note: the demo's lineup table exists but cannot be read "
            f"({type(exc).__name__}), so the class stayed a prediction. "
            f"Run again: uv run pappascout parse {unit}"
        )

    groups: dict[str, set[str]] = {}
    for row in frame.iter_rows(named=True):
        key, player = row["lineup_key"], row["player_id"]
        # A null value would turn into the string "None" and merge different
        # lineups into one group -- that is, produce a lineup that was in no
        # demo at all.
        if key is None or player is None:
            continue
        groups.setdefault(str(key), set()).add(str(player))
    if not groups:
        return None, (
            "Note: the demo's lineup table held no valid row at all, so the "
            "class stayed a prediction."
        )

    scored = sorted(
        ((len(players & roster), key) for key, players in groups.items()),
        reverse=True,
    )
    best, key = scored[0]
    if best == 0:
        return None, (
            "Note: the demo has been parsed, but none of its lineups holds a "
            "player of this team, so the class stayed a prediction."
        )
    if len(scored) > 1 and scored[1][0] == best:
        return None, (
            "Note: two of the demo's lineups are equally close to the standing "
            f"roster ({best} players in common each), so neither was chosen "
            "and the class stayed a prediction."
        )
    return frozenset(groups[key]), None


# -- Names for the reasons ---------------------------------------------------


def _nicknames(teams: Sequence[Team]) -> dict[str, str]:
    """SteamID64 -> nickname, from the whole division.

    The map is gathered from **all** the teams and not only from the subject,
    because the outsider is precisely the player whose name the reason needs
    -- and he is usually a player of some other team in the division. Without
    this the reason would say "From outside the standing roster:
    76561198062941501", which is true but unreadable.

    Transferred players (``Team.released``) are included for the same reason.
    """
    names: dict[str, str] = {}
    for team in teams:
        for member in team.roster + team.released:
            if member.nickname:
                names.setdefault(member.game_player_id, member.nickname)
    return names


# -- The file ----------------------------------------------------------------


def _document(
    rows: Sequence[MapSelection],
    *,
    team: Team,
    league: LeagueSettings,
    thresholds: ThresholdSettings,
    generated_at: datetime,
    index_generated_at: Any,
) -> dict[str, Any]:
    """The selection file's content.

    The thresholds are in the file **as values and not as a reference to the
    settings**: the file is a decision, and a decision cannot be checked
    afterwards if the ground for it is somewhere else and has had time to
    change. ``index_generated_at`` is included for the same reason: it says
    which match list these rows came out of.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "index_generated_at": index_generated_at,
        "competition_ids": list(league.championship_ids),
        "team_key": team.team_key,
        # The name is an **observation**, so it is allowed to be null. The
        # name shown to the user is a different matter
        # (``stats["team_display"]``), and it is not written here -- otherwise
        # an id would end up in the file as a name.
        "team_name": team.name,
        "roster_size": thresholds.roster_size,
        "roster_min_regulars": thresholds.roster_min_regulars,
        # The set every row was settled against. The roster lives on from one
        # ``discover`` run to the next, so without this row "why was this
        # rejected" would be checkable only against the roster of the moment.
        "roster": sorted(team.player_ids),
        "counts": counts(rows),
        "selections": [_row(row) for row in rows],
    }


def _row(row: MapSelection) -> dict[str, Any]:
    return {
        "map_demo_id": row.map_demo_id,
        "match_id": row.match_id,
        "map_index": row.map_index,
        "map_name": row.map_name,
        "is_league": row.is_league,
        "certainly_played": row.certainly_played,
        "roster_ok": row.roster_ok,
        "roster_reason": row.roster_reason,
        "roster_class": row.roster_class,
        "roster_source": row.roster_source,
        "players_seen": row.players_seen,
        "regulars": list(row.regulars),
        "outsiders": list(row.outsiders),
        # The difference between the observation and the prediction. Empty
        # when there is nothing to compare -- a substitution between maps is
        # an observation, and its absence is a different matter from its not
        # having been possible to look at.
        "joined": list(row.joined),
        "left": list(row.left),
    }


def _write(path: Path, document: dict[str, Any]) -> None:
    """Write the selection file atomically; the directory is created if needed.

    Raises:
        ~pappascout.errors.PappascoutError: If the write does not succeed.
            **Disk errors too.** The same rule and the same ground as in
            ``stages.fetch`` and ``stages.import_demo``: a full disk, a file
            lock held by the sync client and a dropped network drive are the
            user's situations and not programming errors. The read path
            (:func:`read_selection`) caught ``OSError`` already, but the write
            path did not -- and unhandled it showed on the screen as the text
            "Unexpected error: [Errno 28]" and the advice "This is a
            programming error", that is, the wrong diagnosis and the wrong
            action.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
        with atomic_path(path) as tmp:
            tmp.write_text(text + "\n", encoding="utf-8")
    except OSError as exc:
        raise PappascoutError(
            f"Writing the team's selection file ({path.name}) failed with a "
            f"disk error ({type(exc).__name__}: {exc}).\n"
            f"The target was {path}.\n"
            "The most common causes: the disk filled up during the write, the "
            "sync client held the file locked, or the network drive dropped.\n"
            "No incomplete file was left on the disk.",
            advice=(
                "Free some disk space or wait until the sync client releases "
                "the file lock, then run the command again."
            ),
        ) from exc


# -- The run's summary -------------------------------------------------------


def _stats(
    rows: Sequence[MapSelection],
    team: Team,
    thresholds: ThresholdSettings,
    skipped: _Skipped,
    generated_at: datetime,
) -> dict[str, Any]:
    """The summary's numbers.

    The match counters **add up**: ``matches_with_maps + matches_not_played +
    matches_without_veto == matches_seen``. Without that equation a match
    could disappear in between without anything explaining the difference --
    and that is exactly why :class:`_Skipped` tells the two skips apart.
    """
    rejections = [
        {
            "map_demo_id": row.map_demo_id,
            "map_name": row.map_name,
            "roster_reason": row.roster_reason,
        }
        for row in rows
        if not row.roster_ok
    ]
    return {
        **counts(rows),
        "team_key": team.team_key,
        # The name for the user: always a string, the id where necessary. The
        # file's ``team_name`` is a different field and is allowed to be null
        # -- the same name for two different promises would be exactly the
        # confusion being avoided here.
        "team_display": team.display_name,
        # ``roster_players`` is the roster's size, ``roster_threshold`` is the
        # threshold. Both used to travel under the name ``roster_size``, which
        # in the file is the threshold and in the summary was the roster's
        # size -- the same name, a different number.
        "roster_players": len(team.roster),
        "roster_threshold": (
            f"{thresholds.roster_min_regulars}/{thresholds.roster_size}"
        ),
        "matches_seen": len(team.match_ids),
        "matches_with_maps": len({row.match_id for row in rows}),
        "matches_not_played": skipped.not_played,
        "matches_without_veto": len(skipped.no_veto),
        "rejections": rejections[:MAX_LISTED_REJECTIONS],
        "rejections_total": len(rejections),
        "generated_at": generated_at.isoformat(),
    }


def _notes(rows: Sequence[MapSelection], team: Team, skipped: _Skipped) -> list[str]:
    """Notes about an empty or incomplete result -- **all of them, not the first**.

    An empty result is not an error -- a team may be yet to play at the start
    of the season -- and ``status`` is therefore ``ok``. But "0 rows" without a
    word about what causes it would leave the user guessing. An earlier version
    returned only the first note, so that "not one map ended up in the sample"
    swallowed the news of the missing veto data.
    """
    notes: list[str] = []
    if not rows:
        if not team.match_ids:
            notes.append(
                f"Team {team.display_name} has no matches in the index, so "
                "there is nothing to select."
            )
        elif not skipped.no_veto:
            notes.append(
                f"Team {team.display_name} has {len(team.match_ids)} "
                "matches, but not one of them has been played -- an unplayed "
                "match has no maps, so no MapDemos exist yet."
            )
    elif not any(row.roster_ok for row in rows):
        notes.append(
            f"Not one of the {len(rows)} maps ended up in the sample. "
            "The reasons are on the rows."
        )

    if skipped.not_played:
        notes.append(
            f"{skipped.not_played} matches are still unplayed, so they have "
            "no maps."
        )
    if skipped.no_veto:
        listed = ", ".join(skipped.no_veto[:MAX_LISTED_MATCHES])
        rest = len(skipped.no_veto) - MAX_LISTED_MATCHES
        more = f" (+{rest} more)" if rest > 0 else ""
        notes.append(
            f"{len(skipped.no_veto)} played matches were left without rows, "
            "because their veto data is missing from the index -- the maps "
            f"were played, but we do not know which: {listed}{more}. "
            "Run again: uv run pappascout discover"
        )
    return notes
