"""``discover`` -- the pipeline's first stage at the head: a division into two indexes.

The stage fetches the competition's matches from behind the port **with one
call per competition** and writes two files out of them:

``index/matches.json``
    The matches as they are: id, status, schedule, sides and map picks.
``index/teams.json``
    The teams with their standing rosters. A standing roster is the **union**
    of the starters and the substitutes over all of the team's matches.

Both have **one writer, and it is this stage**. The other stages read --
:func:`read_indexes` is that reader, so that not every later stage unpacks the
JSON by hand and reads ``schema_version`` in a way of its own.

Why there is no separate roster lookup
--------------------------------------
Measured 2026-09-04 (``mittaus-faceit-aineisto.md`` chapter 1): every match row
carries both sides' ``roster`` **and** ``substitutes``, 132 team rows out of
132. The standing roster can therefore be gathered from the match list, and the
port needs no ``get_roster`` -- that would be a second way of fetching the same
thing and a second cache key for the same answer.

Why the stage has no manifest
-----------------------------
The other stages skip the work when the manifest matches. This one does not:
the match list **changes constantly** (measured 2026-09-04: 60 matches out of
66 were still unplayed), and the whole point of the stage is to see the new
matches. A skip would save one call and would cost exactly what the command is
run for. For the same reason the adapter does not cache the match list and the
command has no ``--pakota`` flag: there is nothing to force when nothing is
ever skipped.

Why ``status`` is always ``ok``
-------------------------------
An empty division is not this stage's failure: the lookup succeeded, and the
result was empty. ``UnitStatus``'s values (AD-9) describe the fate of a **demo
unit** (``no_demo``, ``parse_failed``, ...), and not one of them means "the
competition had no matches" -- and adding a new value would extend
``CLASSIFIED``'s polars enum, that is, change the schema contract of the
parquet files already in the archive. An empty result is therefore told in
:attr:`StageResult.reason`, and the command lifts it to the head of its output.
Silent it does not stay.

What this stage does **not** do
-------------------------------
It does not download demos, does not select matches by the roster threshold
(Story 3.3), and does not write the ``index/selections/`` or
``index/next_opponent/`` files (Epic 4). **Nor does it rename the archive's
directories**: ``aggregates/<team_key>`` and ``classified/<team_key>`` stay as
they are, and the connection to them runs through the ``lineup_keys`` field of
``index/teams.json``. The archive's naming decision is Story 3.4, and it is
made on an observation and not in advance.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl

from pappascout.adapters.protocols import Match, MatchSource, MatchTeam, RosterPlayer
from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.paths import (
    ArchivePaths,
    matches_index,
    parsed_root,
    parsed_table,
    teams_index,
)
from pappascout.domain.models import LeagueSettings, Settings, ThresholdSettings
from pappascout.domain.teams import (
    RosterMember,
    Team,
    TeamLookup,
    TeamObservation,
    assign_lineup_keys,
    build_teams,
    find_teams,
    is_steam_id64,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "SCHEMA_VERSION",
    "PLAYED_STATUSES",
    "run",
    "default_source",
    "read_matches_index",
    "read_teams_index",
    "read_indexes",
    "teams_from_index",
    "matches_from_index",
    "IndexedMatch",
    "IndexedMatchTeam",
    "resolve_team",
]

STAGE = "discover"

#: The version of the index files' format. The reader checks it
#: (:func:`read_indexes`) instead of inferring the format from which fields
#: exist.
#:
#: **Story 3.3 added the match row's ``best_of`` field and did not raise the
#: version**, and that is a rule and not an oversight. The version says
#: whether an old file can be read by this code: not one earlier field
#: disappeared or changed meaning, and a missing ``best_of`` is a **valid
#: observation** ("the source did not say") and not a broken file. Raising it
#: would force a network call into ``discover`` before ``select`` -- which
#: does not need the field -- would agree to run at all. The version rises
#: when an old file stops being readable or the meaning of one of its fields
#: changes.
SCHEMA_VERSION = 1

#: The statuses in which a match has been played. The same set as the
#: adapter's ``CACHEABLE_MATCH_STATUSES``, but a **different decision**: there
#: the question is "may the answer be stored for ever", here "has this match
#: been played". A shared constant would tie two different questions together.
PLAYED_STATUSES = frozenset({"FINISHED"})


@dataclass(frozen=True)
class IndexedMatchTeam:
    """A match's side **as it stands in the index**.

    Not the same as :class:`~pappascout.adapters.protocols.MatchTeam`: there
    the players are objects with their nicknames, here they are bare SteamID64
    ids. The difference is deliberate and it is ``_match_team_row``'s decision
    -- the names are in the team index, and the same listing must not be in two
    files as two different truths.

    Attributes:
        faction_id: The source's team id, or ``None``.
        name: The team's name as an observation, or ``None``.
        roster: The starters' SteamID64s.
        substitutes: The substitutes' SteamID64s.
    """

    faction_id: str | None = None
    name: str | None = None
    roster: tuple[str, ...] = ()
    substitutes: tuple[str, ...] = ()


@dataclass(frozen=True)
class IndexedMatch:
    """One match **as it stands in the index**.

    The times are ISO strings and not ``datetime`` objects: they were written
    into the file as strings, and parsing them back would be a conversion no
    reader has asked for yet. ``played`` is ``discover``'s **decision** about
    the match's status, not the status itself -- and that decision is exactly
    the one ``select`` wants.
    """

    match_id: str
    competition_id: str | None = None
    status: str | None = None
    played: bool = False
    scheduled_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    map_picks: tuple[str, ...] = ()
    best_of: int | None = None
    teams: tuple[IndexedMatchTeam, ...] = ()


def run(
    league: LeagueSettings,
    archive: ArchivePaths,
    team: str | None,
    *,
    source: MatchSource,
    thresholds: ThresholdSettings,
) -> StageResult:
    """Fetch the division's matches and write the match index and the team index.

    Args:
        league: The ``[league]`` section; ``championship_ids`` is read from it.
        archive: The archive's paths.
        team: The team's name, a part of it, or its id. ``None`` is valid:
            then the indexes are written and the summary lists the whole
            division.
        source: The match port, **as a keyword parameter**. A fake in the
            tests, :func:`default_source` in a real run.
        thresholds: The ``[thresholds]`` section, as a keyword parameter.
            ``team_identity_min_common`` is read from it, and it both joins
            source ids into the same team and attaches the archive's lineup
            hashes to teams. Two pydantic sections in a row would pass through
            swapped positionally without anything remarking on it -- the same
            reason as with ``aggregate``'s ``aggregate_settings``.

    Returns:
        A :class:`~pappascout.stages.StageResult`. ``stats`` gives the number
        of matches and teams, the whole division's listing, the dropped
        players and -- if ``team`` was given -- that team's details.

    Raises:
        ~pappascout.errors.PappascoutError: If the name matches no team or
            matches more than one. **The indexes have been written even in
            that case**: the lookup is a view onto the result, not a condition
            for it. The message lists the alternatives with their ids and asks
            for a choice; no silent choice is made.
        ~pappascout.errors.ApiError: If the matches could not be fetched.
    """
    started = time.perf_counter()
    generated_at = datetime.now(UTC)

    matches = _fetch(source, league.championship_ids)
    observations, dropped = _observe(matches)
    built = build_teams(
        observations, min_common=thresholds.team_identity_min_common
    )
    teams, contested = assign_lineup_keys(
        built, _archive_lineups(archive), thresholds.team_identity_min_common
    )

    matches_rel = matches_index()
    teams_rel = teams_index()
    _write_pair(
        archive,
        matches_document=_matches_document(
            matches, league.championship_ids, generated_at
        ),
        teams_document=_teams_document(
            teams, contested, league.championship_ids, generated_at
        ),
    )
    outputs = (matches_rel, teams_rel)

    stats = _stats(matches, teams, contested, dropped, league, generated_at)

    unit = _unit(league)
    if team is not None:
        found = resolve_team(teams, team)
        stats["team"] = _team_stats(found)
        unit = found.team_key

    return StageResult(
        stage=STAGE,
        unit=unit,
        status="ok",
        # Never a skip: the match list changes every day, and the whole point
        # of the stage is to see the change. See the module docstring.
        skipped=False,
        outputs=outputs,
        manifest_path=None,
        reason=_reason(matches, teams, dropped),
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def default_source(settings: Settings, archive: ArchivePaths) -> MatchSource:
    """The production FACEIT implementation of the match port.

    The import is inside the function so that importing this module does not
    load ``requests`` or the whole adapter -- the stage itself knows only the
    port, and the tests give it a fake. The same pattern as in
    ``stages.parse.default_parser``.

    **The rule concerns the adapter, not the weight of the dependencies.**
    ``polars`` is imported at the top of this module, because it is the stage
    layer's own tool (the same as in ``stages.aggregate``); ``requests`` and
    ``FaceitClient`` are on the other side of the port, and it is that
    boundary which is kept unloaded.

    This is also the place through which ``cli`` gets the port **without
    touching the adapters**: the dependency arrow is
    ``cli -> stages -> adapters``.
    """
    from pappascout.adapters.faceit import FaceitClient

    return FaceitClient.from_settings(settings, archive.raw_faceit())


# -- The lookup and the observations -----------------------------------------


def _fetch(source: MatchSource, competition_ids: Sequence[str]) -> tuple[Match, ...]:
    """Fetch the matches of every competition into one list.

    The same match can in principle belong to two competitions; ``match_id``
    deduplicates, so that it does not land in the index twice. The order is by
    schedule, so that the file is readable and two runs can be diffed.
    """
    seen: dict[str, Match] = {}
    for competition_id in competition_ids:
        for match in source.get_matches(competition_id):
            seen.setdefault(match.match_id, match)
    return tuple(sorted(seen.values(), key=_match_order))


def _match_order(match: Match) -> tuple[int, float, str]:
    moment = _moment_of(match)
    if moment is None:
        return (1, 0.0, match.match_id)
    return (0, moment.timestamp(), match.match_id)


def _moment_of(match: Match) -> datetime | None:
    """The match's moment: the schedule first, the real start only if it is missing.

    Measured 2026-09-04: the match list carries ``scheduled_at`` and not
    ``started_at``, so the schedule is the only moment an unplayed match has.
    """
    return match.scheduled_at or match.started_at


class _Dropped:
    """What was left out of the observations -- as counts and as identifiable rows.

    **Distinct players, not appearances.** The same player without an id is on
    every match row in the division, so counting appearances would say "11
    players were left out" of one player. The nickname and the match are kept,
    so that the user can check whose id was missing -- a bare number would be a
    claim without any way of checking it.
    """

    def __init__(self) -> None:
        self.players: dict[str, dict[str, str | None]] = {}
        self.team_rows = 0

    def player(
        self, player: RosterPlayer, team_name: str | None, match_id: str
    ) -> None:
        key = player.player_id or f"{team_name}/{player.nickname}"
        self.players.setdefault(
            key,
            {
                "player_id": player.player_id,
                "nickname": player.nickname,
                "team": team_name,
                "match_id": match_id,
            },
        )

    @property
    def player_count(self) -> int:
        return len(self.players)

    def rows(self) -> list[dict[str, str | None]]:
        return sorted(
            self.players.values(),
            key=lambda row: (str(row.get("nickname") or ""), str(row.get("player_id"))),
        )


def _observe(matches: Iterable[Match]) -> tuple[list[TeamObservation], _Dropped]:
    """Turn the matches into the domain's observations.

    This is where the source's vocabulary ends: ``domain.teams`` does not see
    :class:`Match` at all, so its rules can be tested with observations built
    by hand.

    Two drops, and **both are counted**:

    * **A side without an id** is not a team anything could be attached to.
    * **A player without a SteamID64** cannot be attached to the demos, and the
      standing roster is precisely the set that is attached to them.

    Both are told in the run's summary. Earlier a dropped team row was silent
    and a dropped player was not -- an asymmetry the review found.
    """
    observations: list[TeamObservation] = []
    dropped = _Dropped()
    for match in matches:
        played = _is_played(match)
        moment = _moment_of(match)
        for side in match.teams:
            if side.team_id is None:
                dropped.team_rows += 1
                continue
            roster = _members(side.roster, side.name, match.match_id, dropped)
            substitutes = _members(
                side.substitutes, side.name, match.match_id, dropped
            )
            observations.append(
                TeamObservation(
                    faction_id=side.team_id,
                    match_id=match.match_id,
                    observed_at=moment,
                    name=side.name,
                    played=played,
                    roster=roster,
                    substitutes=substitutes,
                )
            )
    return observations, dropped


def _members(
    players: Iterable[RosterPlayer],
    team_name: str | None,
    match_id: str,
    dropped: _Dropped,
) -> tuple[RosterMember, ...]:
    """The port's players into the domain's roster members; the drops are recorded."""
    members: list[RosterMember] = []
    for player in players:
        steam_id = player.game_player_id
        if steam_id is None or not is_steam_id64(steam_id):
            dropped.player(player, team_name, match_id)
            continue
        members.append(
            RosterMember(
                game_player_id=steam_id,
                nickname=player.nickname,
                player_id=player.player_id,
            )
        )
    return tuple(members)


def _is_played(match: Match) -> bool:
    """Has the match been played? An unknown status is not played."""
    return match.status is not None and match.status.upper() in PLAYED_STATUSES


# -- The bridge to the archive -----------------------------------------------


def _archive_lineups(archive: ArchivePaths) -> dict[str, set[str]]:
    """The archive's lineups: ``lineup_key`` -> the set of players' SteamID64s.

    The source is ``lineups.parquet``, the same as for ``aggregate``: its set
    of players is **exactly the one** ``lineup_key`` was computed from. An
    unreadable or missing table is skipped -- the bridge is extra information,
    and a missing bridge is no reason to leave the index unwritten.
    """
    root = archive.parsed_root()
    if not root.is_dir():
        return {}
    lineups: dict[str, set[str]] = {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        _read_lineups(archive, directory.name, lineups)
    return lineups


def _read_lineups(
    archive: ArchivePaths, map_demo_id: str, into: dict[str, set[str]]
) -> None:
    try:
        path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    except PappascoutError:
        # A directory whose name is not valid as an id is not a parsed demo.
        return
    if not path.is_file():
        return
    try:
        frame = pl.read_parquet(path, columns=["lineup_key", "player_id"])
    except (OSError, pl.exceptions.PolarsError):
        return
    for row in frame.unique().iter_rows(named=True):
        into.setdefault(str(row["lineup_key"]), set()).add(str(row["player_id"]))


# -- The name lookup ---------------------------------------------------------


def resolve_team(teams: Sequence[Team], query: str) -> Team:
    """Read ``--team`` as a team, or say why that does not work.

    **Public, because ``select`` asks the same question.** Two copies of this
    would be two different error messages for the same situation, and the user
    would see a different listing depending on which command he ran. The stage
    does not call another stage from here -- this is a reader, like
    :func:`read_indexes`.

    Raises:
        PappascoutError: When there are zero hits or many. The message lists
            the alternatives **with their ids** -- the id is the only way to
            tell two teams with the same name apart -- and asks for a choice.
            Taking the first hit would be a silent choice, and that is exactly
            what is forbidden.
    """
    lookup = find_teams(teams, query)
    if lookup.is_unique:
        return lookup.team
    raise PappascoutError(_lookup_problem(lookup, teams))


def _lookup_problem(lookup: TeamLookup, teams: Sequence[Team]) -> str:
    """The explanation for why the lookup did not produce one single team."""
    if lookup.is_ambiguous:
        return (
            f"The search {lookup.query!r} hits {len(lookup.teams)} teams, "
            "so a choice has to be made:\n"
            + _listing(lookup.teams)
            + "\nNarrow the search so that it hits one -- for example:\n"
            + f"    --team {_unambiguous_query(lookup.teams)}"
        )
    if not teams:
        return (
            f"The search {lookup.query!r} hits no team at all, because no "
            "matches were found in the division.\n"
            "Check [league].championship_ids in the settings."
        )
    return (
        f"The search {lookup.query!r} hits no team in the division.\n"
        "The division's teams are:\n" + _listing(teams)
    )


def _listing(teams: Sequence[Team]) -> str:
    """The teams with their names, roster sizes and **ids**.

    The id is there because without it a listing of two teams with the same
    name would be two identical rows and the choice could not be made at all.
    """
    return "\n".join(
        f"    {team.display_name} ({len(team.roster)} players, "
        f"id {team.team_key})"
        for team in teams
    )


def _unambiguous_query(teams: Sequence[Team]) -> str:
    """A search suggestion that hits exactly one of these teams.

    A name will do only if it is unambiguous among the hits; otherwise the id
    is suggested. Without this the suggestion would, in the case of teams with
    the same name, be exactly the search that has just failed.
    """
    first = teams[0]
    names = [team.display_name for team in teams]
    if first.name and names.count(first.display_name) == 1:
        return f'"{first.display_name}"'
    return first.team_key


# -- The run summary's numbers -----------------------------------------------


def _unit(league: LeagueSettings) -> str:
    """The unit when no team was looked up: **one id, not a listing**.

    ``StageResult.unit`` is everywhere else in the pipeline a single id
    (``map_demo_id``, ``team_key``), and a comma-joined list would read in the
    output as an id without being one. The whole listing is in
    ``stats["competition_ids"]``.
    """
    return league.championship_ids[0]


def _reason(
    matches: Sequence[Match], teams: Sequence[Team], dropped: _Dropped
) -> str | None:
    """An explanation for an empty or incomplete result, or ``None``.

    The lookup succeeded, so ``status`` is ``ok`` -- but "0 teams, 0 matches"
    without a word about what causes it would leave the user guessing. See the
    module docstring on why there is no status of its own for this.
    """
    if not matches:
        return (
            "No match at all was found in the competition. Check "
            "[league].championship_ids in the settings -- the indexes were "
            "written empty."
        )
    if not teams:
        return (
            "Matches were found, but not one of them had a recognisable "
            "team. The team index was left empty."
        )
    empty = [team.display_name for team in teams if not team.roster]
    if empty:
        return (
            "Not one SteamID64-identified player was obtained from these "
            "teams, so their roster is empty: " + ", ".join(empty)
        )
    if dropped.player_count:
        return (
            f"{dropped.player_count} players were left out of the rosters, "
            "because they had no SteamID64."
        )
    return None


def _stats(
    matches: Sequence[Match],
    teams: Sequence[Team],
    contested: Sequence[str],
    dropped: _Dropped,
    league: LeagueSettings,
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "competition_ids": list(league.championship_ids),
        "matches": len(matches),
        "matches_played": sum(1 for m in matches if _is_played(m)),
        "teams": len(teams),
        "roster_min": min((len(t.roster) for t in teams), default=0),
        "roster_max": max((len(t.roster) for t in teams), default=0),
        "teams_without_roster": sum(1 for t in teams if not t.roster),
        "players_without_steam_id": dropped.player_count,
        "dropped_players": dropped.rows(),
        "team_rows_without_id": dropped.team_rows,
        "contested_lineup_keys": list(contested),
        "transfers": _transfers(teams),
        # The whole division as a listing, so that the names can be seen
        # **without an error**: after an ambiguous search this is exactly what
        # the user needs next.
        "division": [
            {
                "team_key": team.team_key,
                "name": team.name,
                "roster_size": len(team.roster),
                "matches_played": team.matches_played,
            }
            for team in teams
        ],
        "generated_at": generated_at.isoformat(),
    }


def _transfers(teams: Sequence[Team]) -> list[dict[str, Any]]:
    """The players observed in more than one team.

    Both the transferred (``released``) and the contested (``shared_players``)
    -- both are cases in which the roster is not a plain union, and both belong
    in the run's summary and not only in the file.
    """
    rows: list[dict[str, Any]] = []
    for team in teams:
        for member in team.released:
            rows.append(
                {
                    "game_player_id": member.game_player_id,
                    "nickname": member.nickname,
                    "from_team": team.display_name,
                    "kind": "released",
                }
            )
        for player in team.shared_players:
            rows.append(
                {
                    "game_player_id": player,
                    "nickname": next(
                        (
                            m.nickname
                            for m in team.roster
                            if m.game_player_id == player
                        ),
                        None,
                    ),
                    "from_team": team.display_name,
                    "kind": "shared",
                }
            )
    return rows


def _team_stats(team: Team) -> dict[str, Any]:
    return {
        "team_key": team.team_key,
        "faction_ids": list(team.faction_ids),
        "name": team.name,
        "alternative_names": list(team.alternative_names),
        "roster": [member.display_name for member in team.roster],
        "roster_size": len(team.roster),
        "released": [member.display_name for member in team.released],
        "shared_players": list(team.shared_players),
        "matches": len(team.match_ids),
        "matches_played": team.matches_played,
        "lineup_keys": list(team.lineup_keys),
    }


# -- The index files ---------------------------------------------------------


def _write_pair(
    archive: ArchivePaths,
    *,
    matches_document: dict[str, Any],
    teams_document: dict[str, Any],
) -> None:
    """Write both indexes so that neither is left without the other.

    **The pair is not atomic, but it is as close as a file system gets.** Both
    are serialised and written into temporary files first, and only once both
    are on the disk intact are they swapped into place one after the other. A
    serialisation error therefore cannot leave a new match list and an old team
    index in the archive.

    What remains is the gap between the two ``os.replace`` calls. For that both
    carry the same ``generated_at``, and :func:`read_indexes` compares them --
    that is, the reader notices an odd pair instead of joining them silently.

    Raises:
        ~pappascout.errors.PappascoutError: If the write does not succeed.
            **Disk errors too.** The same rule and the same ground as in
            ``stages.fetch`` and ``stages.import_demo``: a full disk, a file
            lock held by the sync client and a dropped network drive are the
            user's situations and not programming errors. The read path
            (:func:`_read`) caught ``OSError`` already, but the write path did
            not -- and unhandled it showed on the screen as the text
            "Unexpected error: [Errno 28]" and the advice "This is a
            programming error", that is, the wrong diagnosis and the wrong
            action.
    """
    matches_abs = archive.resolve(matches_index())
    teams_abs = archive.resolve(teams_index())
    try:
        with atomic_path(matches_abs) as matches_tmp:
            _dump(matches_tmp, matches_document)
            with atomic_path(teams_abs) as teams_tmp:
                _dump(teams_tmp, teams_document)
    except OSError as exc:
        raise PappascoutError(
            f"Writing the archive's indexes ({matches_abs.name}, "
            f"{teams_abs.name}) failed with a disk error "
            f"({type(exc).__name__}: {exc}).\n"
            f"The target was {matches_abs.parent}.\n"
            "The most common causes: the disk filled up during the write, the "
            "sync client held the file locked, or the network drive dropped.\n"
            "No incomplete file was left on the disk. If the write was cut "
            "off between the two swaps, the indexes can still be left of "
            "different ages -- the reader notices that from the generated_at "
            "comparison and does not join them silently.",
            advice=(
                "Free some disk space or wait until the sync client releases "
                "the file lock, then run the command again."
            ),
        ) from exc


def _dump(path: Path, document: dict[str, Any]) -> None:
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _matches_document(
    matches: Sequence[Match], competition_ids: Sequence[str], generated_at: datetime
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "competition_ids": list(competition_ids),
        "matches": [_match_row(match) for match in matches],
    }


def _match_row(match: Match) -> dict[str, Any]:
    return {
        "match_id": match.match_id,
        "competition_id": match.competition_id,
        "status": match.status,
        "played": _is_played(match),
        "scheduled_at": _moment(match.scheduled_at),
        "started_at": _moment(match.started_at),
        "finished_at": _moment(match.finished_at),
        "map_picks": list(match.map_picks),
        # The match's length in maps, ``null`` if the source did not say.
        # **Not the same number as len(map_picks)**: in a BO3 that ended 2-0
        # the veto holds three maps but there are two demos, so Story 3.4
        # computes the expected demos from this and not from the length of the
        # map list.
        "best_of": match.best_of,
        "teams": [_match_team_row(side) for side in match.teams],
    }


def _match_team_row(side: MatchTeam) -> dict[str, Any]:
    return {
        "faction_id": side.team_id,
        "name": side.name,
        # The roster is **not** in the match index with its names: that is the
        # team index's business, and the same listing in two files would be two
        # different truths the moment one of them is rewritten. The match row
        # says who were in this match, as ids.
        "roster": [p.game_player_id for p in side.roster if p.game_player_id],
        "substitutes": [p.game_player_id for p in side.substitutes if p.game_player_id],
    }


def _teams_document(
    teams: Sequence[Team],
    contested: Sequence[str],
    competition_ids: Sequence[str],
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "competition_ids": list(competition_ids),
        # The lineup hashes that more than one team owns. Without this list a
        # later stage would count them twice without knowing it was doing so.
        "contested_lineup_keys": list(contested),
        "teams": [_team_row(team) for team in teams],
    }


def _team_row(team: Team) -> dict[str, Any]:
    return {
        "team_key": team.team_key,
        # All of the source's ids that are this team. The identity is the
        # roster; these are the keys by which the source knew it.
        "faction_ids": list(team.faction_ids),
        "name": team.name,
        "alternative_names": list(team.alternative_names),
        # The connection to the archive, not an identity. The archive's
        # directories are named from the lineup hash, and this field makes that
        # readable. **Renaming the directories has not been promised to any
        # story.** This row promised it to Story 3.4, which was the download of
        # demos over the Downloads API and did not touch the archive's naming
        # at all. If the renaming is done, it is a story of its own.
        "lineup_keys": list(team.lineup_keys),
        # Lists of ids, not counts -- the name says so, lest the reader confuse
        # them with the identically named numbers in the run's summary.
        "match_ids": list(team.match_ids),
        "played_match_ids": list(team.played_match_ids),
        "roster_size": len(team.roster),
        "roster": [_player_row(member) for member in team.roster],
        # Players who were observed in this team but later in another. Not in
        # the roster, but not gone either.
        "released": [_player_row(member) for member in team.released],
        # Players whom another team observed just as late. Still in the roster;
        # a dispute is not settled by drawing lots.
        "shared_players": list(team.shared_players),
    }


def _player_row(member: RosterMember) -> dict[str, Any]:
    return {
        "game_player_id": member.game_player_id,
        "nickname": member.nickname,
        "player_id": member.player_id,
        "alternative_nicknames": list(member.alternative_nicknames),
    }


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# -- The reader --------------------------------------------------------------


def read_matches_index(archive: ArchivePaths) -> dict[str, Any]:
    """Read ``index/matches.json``. See :func:`read_indexes`."""
    return _read(archive.matches_index(), "match index")


def read_teams_index(archive: ArchivePaths) -> dict[str, Any]:
    """Read ``index/teams.json``. See :func:`read_indexes`."""
    return _read(archive.teams_index(), "team index")


def read_indexes(archive: ArchivePaths) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read both indexes and check that they are **from the same run**.

    The reader is here and not in every later stage, because otherwise
    ``schema_version`` would be written but not read -- and every stage would
    read the format in a way of its own.

    Returns:
        ``(matches, teams)`` as dictionaries.

    Raises:
        PappascoutError: If a file is not there, is not valid JSON, its
            ``schema_version`` is unknown, or the files' ``generated_at``
            differs. The last means that the write was interrupted between the
            files; then they must not be joined, and ``discover`` has to be run
            again.
    """
    matches = read_matches_index(archive)
    teams = read_teams_index(archive)
    if matches.get("generated_at") != teams.get("generated_at"):
        raise PappascoutError(
            "The match index and the team index are from different runs "
            f"({matches.get('generated_at')} and {teams.get('generated_at')}), "
            "so they cannot be joined.\n"
            "Run again: uv run pappascout discover"
        )
    return matches, teams


def teams_from_index(document: Mapping[str, Any]) -> tuple[Team, ...]:
    """Build :class:`Team` objects out of the team index's dictionary.

    This is :func:`_team_row`'s counterpart, and it is here for the same reason
    as :func:`read_indexes`: without it every later stage would read the
    index's fields in a way of its own, and the name lookup would work
    differently depending on who wrote it. ``select`` gets in this way exactly
    the teams ``discover`` wrote -- and the same rules
    (:func:`resolve_team`).

    Args:
        document: The dictionary :func:`read_teams_index` returns.

    Returns:
        The teams in the file's order.

    Raises:
        PappascoutError: If a team row of the file is broken -- the id is
            missing or a player's ``game_player_id`` is not a SteamID64. Both
            mean that the file has been edited by hand or was written by
            another version, and a silent skip would produce an incomplete
            roster, that is, the wrong roster threshold.
    """
    rows = document.get("teams")
    if not isinstance(rows, list):
        raise PappascoutError(
            "The archive's team index has no teams list.\n"
            "Run again: uv run pappascout discover"
        )
    teams: list[Team] = []
    for row in rows:
        if not isinstance(row, dict):
            raise PappascoutError(
                "The archive's team index has a row that is not an object.\n"
                "Run again: uv run pappascout discover"
            )
        team_key = row.get("team_key")
        if not isinstance(team_key, str) or not team_key:
            raise PappascoutError(
                "The archive's team index has a team with no team_key field.\n"
                "Run again: uv run pappascout discover"
            )
        teams.append(
            Team(
                team_key=team_key,
                faction_ids=_strings(
                    row.get("faction_ids"), f"{team_key}.faction_ids"
                ),
                name=row.get("name") if isinstance(row.get("name"), str) else None,
                roster=_members_from_index(row.get("roster"), team_key),
                released=_members_from_index(row.get("released"), team_key),
                shared_players=_strings(
                    row.get("shared_players"), f"{team_key}.shared_players"
                ),
                match_ids=_strings(row.get("match_ids"), f"{team_key}.match_ids"),
                played_match_ids=_strings(
                    row.get("played_match_ids"), f"{team_key}.played_match_ids"
                ),
                lineup_keys=_strings(row.get("lineup_keys"), f"{team_key}.lineup_keys"),
                alternative_names=_strings(
                    row.get("alternative_names"), f"{team_key}.alternative_names"
                ),
            )
        )
    return tuple(teams)


def _strings(value: Any, what: str) -> tuple[str, ...]:
    """A list of strings as a tuple -- **no silent thinning is done**.

    A missing key is an empty tuple: an old file may not have the field, and
    "not mentioned" is a valid observation. But a value of the wrong type, or a
    non-string inside the list, is **broken**, and dropping it would do exactly
    what :func:`teams_from_index`'s docstring promises to prevent: a shorter
    listing without anything saying why. An incomplete ``faction_ids`` would
    leave a match unrecognised, an incomplete ``match_ids`` would distort the
    match count.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PappascoutError(
            f"In the archive's index the field {what} is not a list but a "
            f"{type(value).__name__}.\n"
            "Run again: uv run pappascout discover"
        )
    for item in value:
        if not isinstance(item, str):
            raise PappascoutError(
                f"In the archive's index the field {what} holds the value "
                f"{item!r}, which is not a string. Dropping it would shorten "
                "the listing silently.\n"
                "Run again: uv run pappascout discover"
            )
    return tuple(value)


def _members_from_index(value: Any, team_key: str) -> tuple[RosterMember, ...]:
    """The roster's players from the index's rows. A broken row **fails the run**.

    An incomplete roster is the wrong roster threshold (Story 3.3): a dropped
    player would later look as though he had not been in the team, and a map
    would be rejected for a reason that is true only because a row went
    missing.
    """
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PappascoutError(
            f"In the archive's team index the roster of team {team_key} is "
            f"not a list but a {type(value).__name__}.\n"
            "Run again: uv run pappascout discover"
        )
    members: list[RosterMember] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise PappascoutError(
                f"In the archive's team index team {team_key} has the roster "
                f"row {entry!r}, which is not an object.\n"
                "Run again: uv run pappascout discover"
            )
        try:
            members.append(
                RosterMember(
                    game_player_id=str(entry.get("game_player_id")),
                    nickname=(
                        entry.get("nickname")
                        if isinstance(entry.get("nickname"), str)
                        else None
                    ),
                    player_id=(
                        entry.get("player_id")
                        if isinstance(entry.get("player_id"), str)
                        else None
                    ),
                    alternative_nicknames=_strings(
                        entry.get("alternative_nicknames"),
                        f"{team_key}.alternative_nicknames",
                    ),
                )
            )
        except ValueError as exc:
            raise PappascoutError(
                f"In the archive's team index team {team_key} has a player "
                f"who cannot be attached to the demos: {exc}\n"
                "Run again: uv run pappascout discover"
            ) from exc
    return tuple(members)


def matches_from_index(document: Mapping[str, Any]) -> tuple[IndexedMatch, ...]:
    """Build :class:`IndexedMatch` objects out of the match index's dictionary.

    :func:`teams_from_index`'s counterpart on the match side, and it exists for
    the same reason: without it every later stage would unpack ``matches.json``
    by hand and decide on its own which row is intact enough to use.

    **A broken row fails the run and does not disappear into a counter.** A
    skipped match would shorten the sample silently, and that is exactly the
    error the selection's counters are written against.

    Args:
        document: The dictionary :func:`read_matches_index` returns.

    Returns:
        The matches in the file's order.

    Raises:
        PappascoutError: If there is no ``matches`` list, if a row is not an
            object, if ``match_id`` is missing, if the same ``match_id``
            appears twice (two rows for the same match would put every one of
            its maps into the sample twice) or if some field is of the wrong
            type.
    """
    rows = document.get("matches")
    if not isinstance(rows, list):
        raise PappascoutError(
            "The archive's match index has no matches list.\n"
            "Run again: uv run pappascout discover"
        )
    matches: list[IndexedMatch] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PappascoutError(
                f"The archive's match index has the row {row!r}, which is not "
                "an object.\n"
                "Run again: uv run pappascout discover"
            )
        match_id = row.get("match_id")
        if not isinstance(match_id, str) or not match_id:
            raise PappascoutError(
                "The archive's match index has a match with no match_id "
                "field.\n"
                "Run again: uv run pappascout discover"
            )
        if match_id in seen:
            raise PappascoutError(
                f"The archive's match index has match {match_id} twice. "
                "Two rows for the same match would put every one of its maps "
                "into the sample twice.\n"
                "Run again: uv run pappascout discover"
            )
        seen.add(match_id)
        matches.append(
            IndexedMatch(
                match_id=match_id,
                competition_id=_optional_str(row.get("competition_id")),
                status=_optional_str(row.get("status")),
                played=bool(row.get("played")),
                scheduled_at=_optional_str(row.get("scheduled_at")),
                started_at=_optional_str(row.get("started_at")),
                finished_at=_optional_str(row.get("finished_at")),
                map_picks=_strings(row.get("map_picks"), f"{match_id}.map_picks"),
                best_of=_optional_int(row.get("best_of"), f"{match_id}.best_of"),
                teams=_match_teams_from_index(row.get("teams"), match_id),
            )
        )
    return tuple(matches)


def _match_teams_from_index(value: Any, match_id: str) -> tuple[IndexedMatchTeam, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PappascoutError(
            f"In the archive's match index the teams of match {match_id} is "
            f"not a list but a {type(value).__name__}.\n"
            "Run again: uv run pappascout discover"
        )
    sides: list[IndexedMatchTeam] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise PappascoutError(
                f"In the archive's match index match {match_id} has the side "
                f"{entry!r}, which is not an object.\n"
                "Run again: uv run pappascout discover"
            )
        sides.append(
            IndexedMatchTeam(
                faction_id=_optional_str(entry.get("faction_id")),
                name=_optional_str(entry.get("name")),
                roster=_strings(entry.get("roster"), f"{match_id}.roster"),
                substitutes=_strings(
                    entry.get("substitutes"), f"{match_id}.substitutes"
                ),
            )
        )
    return tuple(sides)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _optional_int(value: Any, what: str) -> int | None:
    """An integer or ``None``; another type is broken and not a default.

    ``None`` is a **valid observation**: an old index does not have the field,
    and "the source did not say" is a different matter from "the value is
    broken".
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PappascoutError(
            f"In the archive's match index the field {what} is {value!r}, "
            "which is neither an integer nor a missing value.\n"
            "Run again: uv run pappascout discover"
        )
    return value


def _read(path: Path, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise PappascoutError(
            f"The archive is missing the {what} ({path.name}).\n"
            "Run first: uv run pappascout discover"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PappascoutError(
            f"The archive's {what} ({path.name}) cannot be read: {exc}\n"
            "Run again: uv run pappascout discover"
        ) from exc
    if not isinstance(document, dict):
        raise PappascoutError(
            f"The archive's {what} ({path.name}) is not an object of the "
            "expected shape."
        )
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PappascoutError(
            f"The archive's {what} is in format {version!r}, but this version "
            f"can read only format {SCHEMA_VERSION}.\n"
            "Run again: uv run pappascout discover"
        )
    return document
