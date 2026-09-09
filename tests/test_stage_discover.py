"""``stages.discover`` -- the stage's tests (Story 3.2).

**No network.** In the port's place is :class:`FakeSource`, which returns
hand-built matches and counts its calls. If somebody removed the ``source``
parameter, the test would not go quietly to the network but would fail.

One exception is deliberate: :func:`test_default_source_really_builds_a_port`
really runs ``default_source``. It is the stage's only line that connects it to
the network, and every other test replaces it -- without this test its breaking
would fail nothing. It still does not go to the network: the client is built,
not used.

The data is the division's real shape: 12 teams, round robin, 66 matches, 11
per team -- the same numbers as in ``mittaus-faceit-aineisto.md``. The names and
the roster sizes come from ``test_teams``, so that the measured numbers are in
one place.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from conftest import has_temp_leftovers
from test_teams import DIVISION, MEASURED_RCAVE_IDS, POTKU, RCAVE, members, steam_id

from pappascout.adapters.protocols import Match, MatchSource, MatchTeam, RosterPlayer
from pappascout.archive.paths import ArchivePaths, parsed_table
from pappascout.domain.models import (
    SETTINGS_ENV_VAR,
    LeagueSettings,
    ThresholdSettings,
    load_settings,
)
from pappascout.domain.schemas import LINEUPS
from pappascout.domain.teams import is_steam_id64
from pappascout.errors import PappascoutError, SettingsError
from pappascout.stages import discover as discover_stage

CHAMPIONSHIP = "94681888-b5da-4ab5-bf50-f44b666b98a3"

#: Measured: 66 matches, of which 6 played.
TOTAL_MATCHES = 66
FINISHED_MATCHES = 6

KICKOFF = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


class FakeSource:
    """The match port's fake. Counts the calls, so "fetched again" is measurable."""

    def __init__(self, matches: dict[str, tuple[Match, ...]]) -> None:
        self._matches = matches
        self.calls: list[str] = []

    def replace(self, competition_id: str, matches: tuple[Match, ...]) -> None:
        self._matches[competition_id] = matches

    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        self.calls.append(competition_id)
        return self._matches.get(competition_id, ())

    def get_match(self, match_id: str) -> Match:  # pragma: no cover - not in use
        raise AssertionError("discover does not fetch single matches")


def players(
    team_name: str, size: int, offset: int, shift: int
) -> tuple[tuple[RosterPlayer, ...], tuple[RosterPlayer, ...]]:
    """One match's starters and substitutes in the port's vocabulary.

    The set of starters rotates from match to match, so that no single match
    row holds the whole standing roster -- the stage has to gather it as a
    union.
    """
    roster = members(team_name, size, offset=offset)
    rotated = roster[shift % size :] + roster[: shift % size]
    as_port = tuple(
        RosterPlayer(
            player_id=member.player_id or "",
            nickname=member.nickname,
            game_player_id=member.game_player_id,
        )
        for member in rotated
    )
    return as_port[:5], as_port[5:]


def division_matches() -> tuple[Match, ...]:
    """Round robin: 12 teams, 66 matches, 11 per team.

    The first six matches have been played and the rest are scheduled -- the
    same ratio as measured (6 ``FINISHED``, 60 ``SCHEDULED``).
    ``PotkukelkkaPeek`` is the division's third name, so it hits the played
    ones **exactly once**: precisely the case the acceptance criterion
    requires.
    """
    names = list(DIVISION)
    appearances = {name: 0 for name in names}
    matches: list[Match] = []
    for first in range(len(names)):
        for second in range(first + 1, len(names)):
            index = len(matches)
            sides = []
            for team_index in (first, second):
                name = names[team_index]
                roster, substitutes = players(
                    name,
                    DIVISION[name],
                    offset=team_index * 100,
                    shift=appearances[name],
                )
                appearances[name] += 1
                sides.append(
                    MatchTeam(
                        team_id=f"faction-{team_index:02d}",
                        name=name,
                        roster=roster,
                        substitutes=substitutes,
                    )
                )
            played = index < FINISHED_MATCHES
            matches.append(
                Match(
                    match_id=f"1-match-{index:02d}",
                    competition_id=CHAMPIONSHIP,
                    status="FINISHED" if played else "SCHEDULED",
                    scheduled_at=KICKOFF + timedelta(days=index),
                    finished_at=(
                        KICKOFF + timedelta(days=index, hours=2) if played else None
                    ),
                    teams=tuple(sides),
                    map_picks=("de_ancient", "de_nuke") if played else (),
                    # Measured 2026-09-04: best_of is 2 in all 66 matches, in
                    # the unplayed ones too -- it is the rule book's promise
                    # and not an observation of a played match.
                    best_of=2,
                )
            )
    return tuple(matches)


@pytest.fixture
def league() -> LeagueSettings:
    return LeagueSettings(
        season=13,
        organizer_id="1bfc69fa-5a21-4ed9-9ef3-37edbd7210d8",
        championship_ids=[CHAMPIONSHIP],
        map_pool=["de_ancient", "de_nuke"],
    )


@pytest.fixture
def thresholds() -> ThresholdSettings:
    """The ``[thresholds]`` section at its defaults; ``team_identity_min_common``
    is 3.
    """
    return ThresholdSettings(pistol_rounds=[1, 13])


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


@pytest.fixture
def source() -> FakeSource:
    return FakeSource({CHAMPIONSHIP: division_matches()})


def discover(
    league: LeagueSettings,
    archive: ArchivePaths,
    thresholds: ThresholdSettings,
    source: MatchSource,
    team: str | None = None,
):
    return discover_stage.run(
        league, archive, team, source=source, thresholds=thresholds
    )


def read_index(archive: ArchivePaths, name: str) -> dict[str, Any]:
    return json.loads((archive.root / "index" / name).read_text(encoding="utf-8"))


# -- One call, two indexes ---------------------------------------------------


def test_one_call_per_competition_produces_both_indexes(
    league, archive, thresholds, source
) -> None:
    """Measured: the roster is on the match row, so no separate roster lookup is
    needed."""
    result = discover(league, archive, thresholds, source)

    assert source.calls == [CHAMPIONSHIP]
    assert [str(path) for path in result.outputs] == [
        "index/matches.json",
        "index/teams.json",
    ]
    assert (archive.root / "index" / "matches.json").is_file()
    assert (archive.root / "index" / "teams.json").is_file()


def test_the_run_summary_counts_matches_and_teams(
    league, archive, thresholds, source
) -> None:
    result = discover(league, archive, thresholds, source)

    assert result.stats["matches"] == TOTAL_MATCHES
    assert result.stats["matches_played"] == FINISHED_MATCHES
    assert result.stats["teams"] == len(DIVISION)
    assert result.stats["roster_min"] == 6
    assert result.stats["roster_max"] == 9
    assert result.status == "ok"
    assert result.skipped is False
    assert result.reason is None


def test_the_stage_has_no_manifest_because_it_never_skips(
    league, archive, thresholds, source
) -> None:
    """A skip would save one call and would cost seeing the new matches."""
    result = discover(league, archive, thresholds, source)

    assert result.manifest_path is None


def test_the_unit_is_one_identifier_not_a_list(
    archive, thresholds
) -> None:
    """``StageResult.unit`` is everywhere else in the pipeline a single id.

    A comma-joined list would read in the output as an id without being one;
    the whole listing is in ``stats["competition_ids"]``.
    """
    league = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP, "toinen"],
        map_pool=["de_ancient"],
    )
    source = FakeSource({CHAMPIONSHIP: division_matches()})

    result = discover(league, archive, thresholds, source)

    assert result.unit == CHAMPIONSHIP
    assert "," not in result.unit
    assert result.stats["competition_ids"] == [CHAMPIONSHIP, "toinen"]


# -- The team index's content ------------------------------------------------


def test_the_teams_index_holds_twelve_teams_with_six_to_nine_players(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: 12 teams, rosters of 6--9 players."""
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    sizes = {team["name"]: len(team["roster"]) for team in document["teams"]}

    assert sizes == DIVISION
    assert min(sizes.values()) == 6
    assert max(sizes.values()) == 9


def test_every_roster_identifier_in_the_index_is_steam_id64(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: every id is in SteamID64 form.

    ``player_id`` (FACEIT's UUID) is on the row for the sake of traceability,
    but it **is not** the roster's id -- it does not appear in the demos.
    """
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    for team in document["teams"]:
        for player in team["roster"]:
            assert is_steam_id64(player["game_player_id"]), player


def test_the_team_row_carries_identifier_lists_not_counts(
    league, archive, thresholds, source
) -> None:
    """The index holds the ids, so that the match and the team can be joined.

    A bare count would not say **which** matches these are, and nothing could
    be joined to ``index/matches.json``.
    """
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    potku = next(t for t in document["teams"] if t["name"] == "PotkukelkkaPeek")

    assert len(potku["match_ids"]) == 11
    assert potku["played_match_ids"] == ["1-match-01"]
    assert potku["roster_size"] == len(potku["roster"]) == 8
    known = {row["match_id"] for row in read_index(archive, "matches.json")["matches"]}
    assert set(potku["match_ids"]) <= known


def test_both_indexes_order_matches_the_same_way(
    league, archive, thresholds, source
) -> None:
    """Review: the matches were in time order, the team's matches were not.

    Two different orders for the same ids would make reading the files side by
    side laborious for no reason.
    """
    discover(league, archive, thresholds, source)

    order = [
        row["match_id"] for row in read_index(archive, "matches.json")["matches"]
    ]
    position = {match_id: index for index, match_id in enumerate(order)}
    for team in read_index(archive, "teams.json")["teams"]:
        indices = [position[match_id] for match_id in team["match_ids"]]
        assert indices == sorted(indices), team["name"]


def test_the_index_says_which_form_it_is_in(
    league, archive, thresholds, source
) -> None:
    discover(league, archive, thresholds, source)

    for name in ("teams.json", "matches.json"):
        document = read_index(archive, name)
        assert document["schema_version"] == discover_stage.SCHEMA_VERSION
        assert document["competition_ids"] == [CHAMPIONSHIP]
        assert document["generated_at"]


def test_the_matches_index_carries_status_schedule_and_maps(
    league, archive, thresholds, source
) -> None:
    discover(league, archive, thresholds, source)

    document = read_index(archive, "matches.json")
    assert len(document["matches"]) == TOTAL_MATCHES

    first = document["matches"][0]
    assert first["status"] == "FINISHED"
    assert first["played"] is True
    assert first["scheduled_at"].startswith("2026-08-03")
    assert first["map_picks"] == ["de_ancient", "de_nuke"]
    assert [side["name"] for side in first["teams"]] == ["popsiCS", "JYSAEYTTAEJAET"]
    # The match row carries the source's id, not the canonical team_key: the
    # row says what the source said, and the identity is decided by the team
    # index.
    assert first["teams"][0]["faction_id"] == "faction-00"

    last = document["matches"][-1]
    assert last["played"] is False
    assert last["map_picks"] == []


def test_the_matches_index_carries_best_of(
    league, archive, thresholds, source
) -> None:
    """``best_of`` is in the index because Story 3.4 needs it -- and it cannot be
    computed from the length of ``map_picks``.

    Measured 2026-09-04: the value is 2 in all 66 matches, but before this it
    did not travel through the port at all. An unplayed match carries it too:
    it is the rule book's promise, not an observation of a played match.
    """
    discover(league, archive, thresholds, source)

    document = read_index(archive, "matches.json")
    assert all(row["best_of"] == 2 for row in document["matches"])
    assert document["matches"][-1]["played"] is False
    assert document["matches"][-1]["best_of"] == 2


def test_an_older_index_without_the_best_of_key_is_still_readable(
    league, archive, thresholds, source
) -> None:
    """This is exactly why SCHEMA_VERSION did not rise with the field.

    In an old file the key is **not there at all** -- not as null but as
    missing. A fresh file into which ``best_of: null`` is written would not
    test this case: it is already written by this version.
    """
    discover(league, archive, thresholds, source)
    path = archive.root / "index" / "matches.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for row in document["matches"]:
        del row["best_of"]
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    # The reader accepts the old file: a missing value is a valid observation.
    matches = discover_stage.matches_from_index(
        discover_stage.read_matches_index(archive)
    )

    assert len(matches) == TOTAL_MATCHES
    assert all(match.best_of is None for match in matches)


def test_a_broken_best_of_is_refused_instead_of_defaulted(
    league, archive, thresholds, source
) -> None:
    """Missing is an observation, broken is broken -- and they are different things."""
    discover(league, archive, thresholds, source)
    path = archive.root / "index" / "matches.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["matches"][0]["best_of"] = "kaksi"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PappascoutError, match="best_of"):
        discover_stage.matches_from_index(
            discover_stage.read_matches_index(archive)
        )


# -- The reader ---------------------------------------------------------------


def test_the_indexes_can_be_read_back_as_a_pair(
    league, archive, thresholds, source
) -> None:
    """``schema_version`` is written **and** read; a later stage does not unpack
    the JSON."""
    discover(league, archive, thresholds, source)

    matches, teams = discover_stage.read_indexes(archive)

    assert len(matches["matches"]) == TOTAL_MATCHES
    assert len(teams["teams"]) == len(DIVISION)
    assert matches["generated_at"] == teams["generated_at"]


def test_reading_a_missing_index_says_what_to_run(archive) -> None:
    with pytest.raises(PappascoutError, match="discover"):
        discover_stage.read_teams_index(archive)


def test_an_unknown_schema_version_is_refused(
    league, archive, thresholds, source
) -> None:
    """An unknown format is an error, not a guess."""
    discover(league, archive, thresholds, source)
    path = archive.teams_index()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="99"):
        discover_stage.read_teams_index(archive)


def test_indexes_from_two_different_runs_are_refused_as_a_pair(
    league, archive, thresholds, source
) -> None:
    """The write can be interrupted between the files; then they must not be joined.

    Both carry the same ``generated_at`` for exactly this -- without the
    comparison the reader would join a new match list to an old team index
    silently.
    """
    discover(league, archive, thresholds, source)
    path = archive.teams_index()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["generated_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="from different runs"):
        discover_stage.read_indexes(archive)


def test_a_failure_between_the_two_writes_leaves_both_untouched(
    league, archive, thresholds, source, monkeypatch
) -> None:
    """The pair is written into temporary files before either is swapped.

    Without this a failure would leave a new match list and an old team index
    in the archive, and the reader would join them together.
    """
    discover(league, archive, thresholds, source)
    before_matches = archive.matches_index().read_bytes()
    before_teams = archive.teams_index().read_bytes()

    def explode(*args, **kwargs):
        raise RuntimeError("the disk is full")

    monkeypatch.setattr("pappascout.stages.discover._dump", explode)
    with pytest.raises(RuntimeError):
        discover(league, archive, thresholds, source)

    assert archive.matches_index().read_bytes() == before_matches
    assert archive.teams_index().read_bytes() == before_teams
    assert not has_temp_leftovers(archive.root)


def test_a_write_that_fails_on_disk_is_a_clear_error_with_advice(
    league, archive, thresholds, source
) -> None:
    """I/O matrix: ``discover`` cannot write the indexes.

    **The real stage, a real write.** A directory is put in the target's place,
    so that ``os.replace`` really fails with an ``OSError`` at the atomic
    write's last step. The same fix and the same word as in ``select``: its
    read path caught ``OSError`` already, its write path did not.
    """
    kohde = archive.teams_index()
    kohde.parent.mkdir(parents=True, exist_ok=True)
    kohde.mkdir()
    (kohde / "esteena.txt").write_text("x", encoding="utf-8")

    with pytest.raises(PappascoutError) as err:
        discover(league, archive, thresholds, source)

    viesti = str(err.value)
    # **The siblings' guards claim the same things.** Two fixes made
    # explicitly as siblings must not be left guarded to different degrees --
    # that difference is exactly what Story 3.7 is putting right.
    assert "programming error" not in viesti
    assert "failed with a disk error" in viesti
    assert kohde.name in viesti
    assert err.value.advice
    assert not kohde.is_file()
    assert not has_temp_leftovers(archive.root)
    # The pair is still unwritten: neither was swapped into place.
    assert not archive.matches_index().is_file()


def test_a_failure_on_the_outer_index_says_the_pair_may_be_odd(
    league, archive, thresholds, source
) -> None:
    """The other direction: the **outer** swap fails, the inner one got through.

    ``_write_pair`` writes both into temporary files and swaps them one after
    the other: teams first, matches then. If the latter swap fails, no
    incomplete file is left on the disk -- but what is left on the disk is an
    **odd pair**, a new teams beside the old (or missing) matches. The
    function's own docstring speaks of exactly that gap, so the message must
    not be silent about it.

    The sibling test covers the inner direction; without this test half of
    ``_write_pair``'s failure surface would go unrun.
    """
    kohde = archive.matches_index()
    kohde.parent.mkdir(parents=True, exist_ok=True)
    kohde.mkdir()
    (kohde / "esteena.txt").write_text("x", encoding="utf-8")

    with pytest.raises(PappascoutError) as err:
        discover(league, archive, thresholds, source)

    viesti = str(err.value)
    assert "programming error" not in viesti
    assert "failed with a disk error" in viesti
    assert kohde.name in viesti
    assert err.value.advice
    # The message is not silent about the odd pair and does not promise the
    # impossible.
    assert "generated_at" in viesti
    # And the pair IS odd: teams got into place, matches did not.
    assert archive.teams_index().is_file()
    assert not archive.matches_index().is_file()
    assert not has_temp_leftovers(archive.root)


# -- The name lookup through the stage ---------------------------------------


def test_a_partial_name_finds_rcave_and_its_seven_players(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: ``Rcave`` -> one team, a standing roster of 7 players."""
    result = discover(league, archive, thresholds, source, team="Rcave")

    team = result.stats["team"]
    assert team["name"] == "Rcave Veterans"
    assert team["roster"] == list(RCAVE)
    assert team["roster_size"] == 7
    assert result.unit == team["team_key"]


def test_the_measured_rcave_identifiers_survive_into_the_index(
    league, archive, thresholds, source
) -> None:
    """The four measured SteamID64s are exactly the ones that join to the demo."""
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    rcave = next(t for t in document["teams"] if t["name"] == "Rcave Veterans")
    identifiers = {player["game_player_id"] for player in rcave["roster"]}

    assert set(MEASURED_RCAVE_IDS.values()) <= identifiers


def test_a_team_with_one_played_match_out_of_eleven_is_found_in_full(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: the number of played matches does not affect the roster."""
    result = discover(league, archive, thresholds, source, team="PotkukelkkaPeek")

    team = result.stats["team"]
    assert team["matches"] == 11
    assert team["matches_played"] == 1
    assert team["roster"] == list(POTKU)


def test_case_does_not_matter_through_the_stage(
    league, archive, thresholds, source
) -> None:
    result = discover(league, archive, thresholds, source, team="POTKUKELKKAPEEK")

    assert result.stats["team"]["name"] == "PotkukelkkaPeek"


def test_an_ambiguous_name_lists_all_three_and_chooses_none(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: ``T`` lists three teams and picks none."""
    with pytest.raises(PappascoutError) as error:
        discover(league, archive, thresholds, source, team="T")

    message = str(error.value)
    for name in ("TUUHEE", "Takakeno", "Tankkiluola vilttiketju"):
        assert name in message
    assert "a choice has to be made" in message.lower()


def test_the_ambiguity_listing_shows_identifiers_and_suggests_a_working_query(
    league, archive, thresholds
) -> None:
    """Review: with teams of the same name the suggestion was the very search that
    failed.

    Two identical rows without the id are not a choice but a dead end.
    """
    shared_name = "Takakeno"
    matches = (
        Match(
            match_id="1-a",
            competition_id=CHAMPIONSHIP,
            status="SCHEDULED",
            scheduled_at=KICKOFF,
            teams=(
                MatchTeam(
                    team_id="faction-a",
                    name=shared_name,
                    roster=players("a", 5, 0, 0)[0],
                ),
                MatchTeam(
                    team_id="faction-b",
                    name=shared_name,
                    roster=players("b", 5, 500, 0)[0],
                ),
            ),
        ),
    )
    source = FakeSource({CHAMPIONSHIP: matches})

    with pytest.raises(PappascoutError) as error:
        discover(league, archive, thresholds, source, team=shared_name)

    message = str(error.value)
    assert "faction-a" in message and "faction-b" in message
    # The suggestion is the id and not the name, because the name hits both.
    assert "--team faction-a" in message


def test_the_indexes_are_written_even_when_the_name_is_ambiguous(
    league, archive, thresholds, source
) -> None:
    """The lookup is a view onto the result, not a condition for it.

    Without this an ambiguous name would leave the indexes unwritten, and the
    user would have to run the command twice to see the teams he is choosing
    between.
    """
    with pytest.raises(PappascoutError):
        discover(league, archive, thresholds, source, team="T")

    assert (archive.root / "index" / "teams.json").is_file()
    assert (archive.root / "index" / "matches.json").is_file()


def test_an_unknown_name_lists_the_whole_division(
    league, archive, thresholds, source
) -> None:
    """I/O matrix: ``Astralis`` is not in the division."""
    with pytest.raises(PappascoutError) as error:
        discover(league, archive, thresholds, source, team="Astralis")

    message = str(error.value)
    assert "Astralis" in message
    for name in DIVISION:
        assert name in message


def test_the_division_can_be_listed_without_causing_an_error(
    league, archive, thresholds, source
) -> None:
    """Review: the names could be seen only by entering a wrong name on purpose.

    Without the ``--team`` option the summary lists the division's teams with
    their ids -- exactly what the user needs after an ambiguous search.
    """
    result = discover(league, archive, thresholds, source)

    listing = result.stats["division"]
    assert [row["name"] for row in listing] == [
        team["name"] for team in read_index(archive, "teams.json")["teams"]
    ]
    assert all(row["team_key"] for row in listing)
    assert "team" not in result.stats


# -- The second run -----------------------------------------------------------


def test_running_twice_fetches_the_match_list_again(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: new matches do not stay invisible.

    The match list is not cached and the stage is not skipped, so the second
    run sees what changed. 60 matches out of 66 were unplayed at the time of
    the measurement.
    """
    discover(league, archive, thresholds, source)
    discover(league, archive, thresholds, source)

    assert source.calls == [CHAMPIONSHIP, CHAMPIONSHIP]


def test_a_new_match_shows_up_on_the_second_run(league, archive, thresholds) -> None:
    first = division_matches()
    source = FakeSource({CHAMPIONSHIP: first})
    discover(league, archive, thresholds, source)

    source.replace(
        CHAMPIONSHIP,
        first
        + (
            Match(
                match_id="1-match-99",
                competition_id=CHAMPIONSHIP,
                status="SCHEDULED",
                scheduled_at=KICKOFF + timedelta(days=99),
                teams=first[0].teams,
            ),
        ),
    )
    result = discover(league, archive, thresholds, source)

    assert result.stats["matches"] == TOTAL_MATCHES + 1
    ids = [row["match_id"] for row in read_index(archive, "matches.json")["matches"]]
    assert "1-match-99" in ids


# -- The archive stays untouched ----------------------------------------------


def test_aggregates_and_classified_are_neither_renamed_nor_changed(
    league, archive, thresholds, source
) -> None:
    """Acceptance criterion: the archive's team directories stay as they are.

    The archive's naming decision is Story 3.4, and it is made on an
    observation and not in advance -- so this stage must not touch them at all.
    """
    for kind in ("aggregates", "classified"):
        directory = archive.root / kind / "ff03fb54599d3311"
        directory.mkdir(parents=True)
        (directory / "report.json").write_text("vanha", encoding="utf-8")

    before = {
        path.relative_to(archive.root).as_posix(): path.read_bytes()
        for path in archive.root.rglob("*")
        if path.is_file()
    }

    discover(league, archive, thresholds, source, team="Rcave")

    after = {
        path.relative_to(archive.root).as_posix(): path.read_bytes()
        for path in archive.root.rglob("*")
        if path.is_file()
        if not path.relative_to(archive.root).as_posix().startswith("index/")
    }
    assert after == before


def test_no_selection_or_next_opponent_file_is_written(
    league, archive, thresholds, source
) -> None:
    """Boundaries: ``index/selections/`` is Story 3.3, ``next_opponent`` Epic 4."""
    discover(league, archive, thresholds, source, team="Rcave")

    written = sorted(
        path.relative_to(archive.root).as_posix()
        for path in archive.root.rglob("*")
        if path.is_file()
    )
    assert written == ["index/matches.json", "index/teams.json"]


def test_atomic_writes_leave_no_temporary_files(
    league, archive, thresholds, source
) -> None:
    """The archive is a synchronised folder: half an index would be the seed of a
    conflict copy."""
    discover(league, archive, thresholds, source)

    assert not has_temp_leftovers(archive.root)


# -- The bridge to the archive ------------------------------------------------


def write_lineups(archive: ArchivePaths, map_demo_id: str, lineups: dict) -> None:
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "map_demo_id": map_demo_id,
            "lineup_key": lineup_key,
            "player_id": player_id,
            "player_name": None,
            "clan_name": None,
        }
        for lineup_key, player_ids in lineups.items()
        for player_id in player_ids
    ]
    pl.DataFrame(rows, schema=dict(LINEUPS)).write_parquet(path)


def test_known_lineup_keys_bridge_the_index_to_the_archive(
    league, archive, thresholds, source
) -> None:
    """``index/teams.json`` carries the known lineup hashes.

    It is the only readable connection to the ``aggregates/<team_key>``
    directories, which this story does not rename.
    """
    write_lineups(
        archive,
        "1-match-00-0",
        {"ff03fb54599d3311": list(MEASURED_RCAVE_IDS.values())},
    )

    result = discover(league, archive, thresholds, source, team="Rcave")

    assert result.stats["team"]["lineup_keys"] == ["ff03fb54599d3311"]
    assert result.stats["contested_lineup_keys"] == []
    document = read_index(archive, "teams.json")
    rcave = next(t for t in document["teams"] if t["name"] == "Rcave Veterans")
    assert rcave["lineup_keys"] == ["ff03fb54599d3311"]


def test_a_lineup_claimed_by_two_teams_is_flagged_in_the_index(
    league, archive, thresholds, source
) -> None:
    """A contested hash is flagged, so that a later stage does not count it twice."""
    rcave = list(MEASURED_RCAVE_IDS.values())
    # Three of Rcave's players and three of PotkukelkkaPeek's: both cross the
    # threshold of 3, and neither is "more right".
    potku_ids = [steam_id(200 + index) for index in range(3)]
    write_lineups(archive, "1-match-00-0", {"kiistanalainen": rcave[:3] + potku_ids})

    result = discover(league, archive, thresholds, source)

    assert result.stats["contested_lineup_keys"] == ["kiistanalainen"]
    document = read_index(archive, "teams.json")
    owners = [t["name"] for t in document["teams"] if t["lineup_keys"]]
    assert len(owners) == 2
    assert document["contested_lineup_keys"] == ["kiistanalainen"]


def test_a_missing_or_unreadable_lineup_table_is_not_a_reason_to_fail(
    league, archive, thresholds, source
) -> None:
    """The bridge is extra information: without it the index is still correct."""
    broken = archive.resolve(parsed_table("1-rikki-0", "lineups"))
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not a parquet file")
    (archive.root / "parsed" / "ei-hakemisto").write_text("x", encoding="utf-8")

    result = discover(league, archive, thresholds, source, team="Rcave")

    assert result.stats["team"]["lineup_keys"] == []


# -- The observations' edge cases ---------------------------------------------


def test_the_same_player_missing_a_steam_id_is_counted_once_not_once_per_match(
    league, archive, thresholds
) -> None:
    """Review: the counter counted appearances, so one player was "11 players".

    The dropped are told **by name** as well, so that the user can check whose
    id was missing -- a bare number would be a claim without any way of
    checking it.
    """
    matches = tuple(
        Match(
            match_id=f"1-vajaa-{index}",
            competition_id=CHAMPIONSHIP,
            status="SCHEDULED",
            scheduled_at=KICKOFF + timedelta(days=index),
            teams=(
                MatchTeam(
                    team_id="faction-x",
                    name="Vajaa",
                    roster=(
                        RosterPlayer(
                            player_id="uuid-1",
                            nickname="a",
                            game_player_id="76561197977479426",
                        ),
                        RosterPlayer(
                            player_id="uuid-2", nickname="ei-tunnistetta"
                        ),
                    ),
                ),
            ),
        )
        for index in range(11)
    )
    source = FakeSource({CHAMPIONSHIP: matches})

    result = discover(league, archive, thresholds, source, team="Vajaa")

    assert result.stats["players_without_steam_id"] == 1
    assert result.stats["dropped_players"] == [
        {
            "player_id": "uuid-2",
            "nickname": "ei-tunnistetta",
            "team": "Vajaa",
            "match_id": "1-vajaa-0",
        }
    ]
    assert result.stats["team"]["roster"] == ["a"]


def test_a_side_without_an_identifier_is_counted_not_only_dropped(
    league, archive, thresholds
) -> None:
    """Review: dropped players were told of, dropped team rows were not.

    The asymmetry was a silent drop just like any other.
    """
    match = Match(
        match_id="1-tuntematon",
        competition_id=CHAMPIONSHIP,
        status="SCHEDULED",
        scheduled_at=KICKOFF,
        teams=(MatchTeam(team_id=None, name="Vielä ratkeamatta"),),
    )
    source = FakeSource({CHAMPIONSHIP: (match,)})

    result = discover(league, archive, thresholds, source)

    assert result.stats["teams"] == 0
    assert result.stats["team_rows_without_id"] == 1
    assert result.reason is not None
    assert "recognisable team" in result.reason


def test_an_empty_competition_says_where_to_look(league, archive, thresholds) -> None:
    """A competition without matches is an observation, not an error -- but not
    silent either.

    ``status`` stays ``ok``, because the lookup succeeded; the reason is told
    in ``reason``, and the command lifts it to the head of its output.
    """
    source = FakeSource({CHAMPIONSHIP: ()})

    result = discover(league, archive, thresholds, source)

    assert result.stats["matches"] == 0
    assert result.status == "ok"
    assert result.reason is not None
    assert "championship_ids" in result.reason
    assert read_index(archive, "teams.json")["teams"] == []


def test_a_team_whose_players_all_lack_steam_ids_is_named_in_the_reason(
    league, archive, thresholds
) -> None:
    """An empty roster was written into the index without a mark of any kind."""
    match = Match(
        match_id="1-tyhja",
        competition_id=CHAMPIONSHIP,
        status="SCHEDULED",
        scheduled_at=KICKOFF,
        teams=(
            MatchTeam(
                team_id="faction-x",
                name="Tunnisteeton",
                roster=(RosterPlayer(player_id="uuid-1", nickname="a"),),
            ),
        ),
    )
    source = FakeSource({CHAMPIONSHIP: (match,)})

    result = discover(league, archive, thresholds, source)

    assert result.stats["teams_without_roster"] == 1
    assert result.reason is not None
    assert "Tunnisteeton" in result.reason


def test_a_name_search_in_an_empty_division_says_why(
    league, archive, thresholds
) -> None:
    source = FakeSource({CHAMPIONSHIP: ()})

    with pytest.raises(PappascoutError, match="championship_ids"):
        discover(league, archive, thresholds, source, team="Rcave")


def test_the_same_match_in_two_competitions_is_counted_once(
    archive, thresholds
) -> None:
    """The same match in two competitions is one match, not two."""
    other = "toinen-championship"
    league = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP, other],
        map_pool=["de_ancient"],
    )
    matches = division_matches()
    source = FakeSource({CHAMPIONSHIP: matches, other: matches[:5]})

    result = discover(league, archive, thresholds, source)

    assert source.calls == [CHAMPIONSHIP, other]
    assert result.stats["matches"] == TOTAL_MATCHES


def test_a_transferred_player_is_reported_in_the_run_summary(
    league, archive, thresholds
) -> None:
    """A transfer changes the roster, so it must not be left only in the file."""
    mover = RosterPlayer(
        player_id="uuid-mover", nickname="siirtyja", game_player_id=steam_id(1)
    )
    a_players = players("aaa", 5, 100, 0)[0]
    b_players = players("bbb", 5, 200, 0)[0]
    matches = (
        Match(
            match_id="1-a",
            competition_id=CHAMPIONSHIP,
            status="FINISHED",
            scheduled_at=KICKOFF,
            teams=(
                MatchTeam(
                    team_id="A", name="Aakkoset", roster=a_players[:4] + (mover,)
                ),
            ),
        ),
        Match(
            match_id="1-b",
            competition_id=CHAMPIONSHIP,
            status="SCHEDULED",
            scheduled_at=KICKOFF + timedelta(days=30),
            teams=(
                MatchTeam(team_id="B", name="Beeta", roster=b_players[:4] + (mover,)),
            ),
        ),
    )
    source = FakeSource({CHAMPIONSHIP: matches})

    result = discover(league, archive, thresholds, source, team="Aakkoset")

    assert result.stats["transfers"] == [
        {
            "game_player_id": steam_id(1),
            "nickname": "siirtyja",
            "from_team": "Aakkoset",
            "kind": "released",
        }
    ]
    assert result.stats["team"]["released"] == ["siirtyja"]
    assert "siirtyja" not in result.stats["team"]["roster"]


def test_the_fake_source_satisfies_the_port(source) -> None:
    """The fake must not be looser than the port, or the tests would measure the
    wrong thing."""
    assert isinstance(source, MatchSource)


# -- The production port -----------------------------------------------------


def test_default_source_really_builds_a_port(
    settings_file: Path, env_file, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``default_source`` is really run -- **without going to the network**.

    It is the stage's only line that connects it to FACEIT, and every other
    test replaces it with a fake. Without this test its breaking would fail
    nothing. Its sibling ``stages.parse.default_parser`` is likewise run for
    real.

    The client is **built, not used**: not one request goes out, and the cache
    directory is checked against what the archive gave.
    """
    env = env_file(".env", FACEIT_API_KEY="salainen-avain-XYZZY-42")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    settings = load_settings(settings_file, env_files=(env,))
    archive = ArchivePaths(root=tmp_path / "arkisto")

    port = discover_stage.default_source(settings, archive)

    assert isinstance(port, MatchSource)
    assert port.cache_dir == archive.raw_faceit()
    # The key does not show in the representation; that is the adapter's
    # promise, and this is the place where the client comes into being.
    assert "XYZZY" not in repr(port)
    port.close()


def test_default_source_without_a_key_says_which_file_to_edit(
    settings_file: Path, tmp_path: Path
) -> None:
    """A missing credential stops the run with an instruction, not a stack trace."""
    settings = load_settings(settings_file, env_files=())
    archive = ArchivePaths(root=tmp_path / "arkisto")

    with pytest.raises(SettingsError, match="FACEIT_API_KEY"):
        discover_stage.default_source(settings, archive)


# -- The reader returns the domain's objects ---------------------------------


def test_the_teams_index_reads_back_as_domain_teams(
    league, archive, thresholds, source
) -> None:
    """``select`` gets exactly the teams ``discover`` wrote.

    Without this reader every later stage would read the index's fields in a
    way of its own, and the name lookup would work differently depending on
    who wrote it.
    """
    discover(league, archive, thresholds, source)

    teams = discover_stage.teams_from_index(
        discover_stage.read_teams_index(archive)
    )

    assert {team.name for team in teams} == set(DIVISION)
    rcave = next(team for team in teams if team.name == "Rcave Veterans")
    assert len(rcave.roster) == DIVISION["Rcave Veterans"]
    assert all(is_steam_id64(pid) for pid in rcave.player_ids)
    assert len(rcave.match_ids) == 11


def test_every_written_team_field_survives_the_round_trip(
    league, archive, thresholds, source
) -> None:
    """The writer-reader pair is locked **field by field**.

    A check of the roster alone would not notice if ``shared_players``,
    ``alternative_names``, ``lineup_keys`` or ``released`` disappeared on the
    round trip -- and those are exactly the fields nobody looks at until they
    are missing. The comparison is made against the file's own row, so a new
    field has to be added to both sides or this fails.
    """
    write_lineups(
        archive,
        "1-match-00-0",
        {"ff03fb54599d3311": list(MEASURED_RCAVE_IDS.values())},
    )
    discover(league, archive, thresholds, source)
    document = read_index(archive, "teams.json")

    teams = discover_stage.teams_from_index(document)

    by_key = {team.team_key: team for team in teams}
    assert len(by_key) == len(document["teams"])
    for written in document["teams"]:
        team = by_key[written["team_key"]]
        assert list(team.faction_ids) == written["faction_ids"]
        assert team.name == written["name"]
        assert list(team.alternative_names) == written["alternative_names"]
        assert list(team.lineup_keys) == written["lineup_keys"]
        assert list(team.match_ids) == written["match_ids"]
        assert list(team.played_match_ids) == written["played_match_ids"]
        assert list(team.shared_players) == written["shared_players"]
        assert [m.game_player_id for m in team.roster] == [
            p["game_player_id"] for p in written["roster"]
        ]
        assert [m.nickname for m in team.roster] == [
            p["nickname"] for p in written["roster"]
        ]
        assert [m.game_player_id for m in team.released] == [
            p["game_player_id"] for p in written["released"]
        ]
    # At least one team has a known lineup hash, so that the lineup_keys
    # comparison is not empty on both sides.
    assert any(team.lineup_keys for team in teams)


def test_a_non_string_in_a_list_field_is_refused_not_dropped(
    league, archive, thresholds, source
) -> None:
    """An incomplete ``faction_ids`` would leave a match unrecognised.

    An earlier reader dropped non-strings silently -- that is, did exactly what
    its own docstring promised to prevent.
    """
    discover(league, archive, thresholds, source)
    path = archive.root / "index" / "teams.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["teams"][0]["faction_ids"].append(42)
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(PappascoutError, match="is not a string"):
        discover_stage.teams_from_index(
            discover_stage.read_teams_index(archive)
        )


def test_the_same_lookup_rules_apply_to_the_teams_read_back(
    league, archive, thresholds, source
) -> None:
    """Two copies of the name lookup would be two different error messages for the
    same thing."""
    discover(league, archive, thresholds, source)
    teams = discover_stage.teams_from_index(
        discover_stage.read_teams_index(archive)
    )

    found = discover_stage.resolve_team(teams, "Rcave")

    assert found.name == "Rcave Veterans"
    with pytest.raises(PappascoutError, match="hits 3 teams"):
        discover_stage.resolve_team(teams, "T")


def test_a_hand_broken_teams_index_is_refused_not_silently_thinned(
    league, archive, thresholds, source
) -> None:
    """An incomplete roster would be the wrong roster threshold, and nothing would
    say why."""
    discover(league, archive, thresholds, source)
    path = archive.root / "index" / "teams.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["teams"][0]["roster"][0]["game_player_id"] = "ei-steamid"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="cannot be attached to the demos"):
        discover_stage.teams_from_index(
            discover_stage.read_teams_index(archive)
        )
