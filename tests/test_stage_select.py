"""``stages.select`` -- the stage's tests (Story 3.3).

**No network.** The stage does not take the port at all: it reads the indexes
``discover`` wrote and the archive's lineup tables. The indexes are written
here by the real ``discover`` from behind a fake port, so that the tests read
exactly the format the program really writes -- a hand-built ``matches.json``
would pass the test and fall apart in a real run.

The summary is rendered **from a real run's result** (``_render_select``) and
not from a hand-written dictionary: otherwise a key could disappear between
the stage and the output without a single test noticing, and the rejections'
reasons would be printed as an empty block.

The I/O matrix's ten cases are in this file, and each is named so that its row
can be recognised from the specification.

The data is the division's real shape (``test_stage_discover.division_matches``):
12 teams, 66 matches, six played. The subject is ``PotkukelkkaPeek``, because
it is the division's third name and hits the played ones **exactly once** --
that is, it has one played match out of eleven, the same ratio as Rcave
Veterans in the real data (measured 2026-09-04), and therefore the same
expectation: **two MapDemos**. Rcave Veterans is in this data the team that has
no played match at all -- and that too is a case that has to behave correctly.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from conftest import has_temp_leftovers
from test_stage_discover import (
    CHAMPIONSHIP,
    FakeSource,
    division_matches,
    write_lineups,
)
from test_teams import DIVISION, members, steam_id

from pappascout.archive.paths import ArchivePaths, parsed_table
from pappascout.cli import _render_select
from pappascout.domain.models import LeagueSettings, ThresholdSettings
from pappascout.domain.schemas import LINEUPS
from pappascout.errors import PappascoutError
from pappascout.stages import discover as discover_stage
from pappascout.stages import select as select_stage

#: The division's order settles who hits the six played matches.
TEAM_INDEX = {name: index for index, name in enumerate(DIVISION)}

#: The subject: the division's third name, and therefore **exactly one played
#: match**.
SUBJECT = "PotkukelkkaPeek"

#: The subject's only played match: the pair (popsiCS, PotkukelkkaPeek) is the
#: data's second match, and the first six have been played.
PLAYED_MATCH = "1-match-01"

#: The lender of outsider players. It has to be a team that still plays after
#: :data:`PLAYED_MATCH` -- otherwise it would not observe its own players last
#: and the transfer rule would not remove them from the subject's standing
#: roster. Takakeno is the division's sixth name, so its matches continue well
#: past the loan.
LENDER = "Takakeno"

SUBJECT_FACTION = f"faction-{TEAM_INDEX[SUBJECT]:02d}"


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
    """``[thresholds]`` at its defaults: ``roster_size`` 5, ``roster_min_regulars``
    4.
    """
    return ThresholdSettings(pistol_rounds=[1, 13])


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


def index(
    archive: ArchivePaths,
    league: LeagueSettings,
    thresholds: ThresholdSettings,
    matches=None,
) -> None:
    """Write the indexes with the real ``discover``, from behind a fake port."""
    source = FakeSource(
        {CHAMPIONSHIP: division_matches() if matches is None else matches}
    )
    discover_stage.run(league, archive, None, source=source, thresholds=thresholds)


def select(
    league: LeagueSettings,
    archive: ArchivePaths,
    thresholds: ThresholdSettings,
    team: str = "Potku",
):
    return select_stage.run(league, archive, team, thresholds=thresholds)


def read_selection(archive: ArchivePaths, team_key: str) -> dict[str, Any]:
    return select_stage.read_selection(archive, team_key)


def subject_key(archive: ArchivePaths) -> str:
    document = json.loads(
        (archive.root / "index" / "teams.json").read_text(encoding="utf-8")
    )
    return next(
        team["team_key"] for team in document["teams"] if team["name"] == SUBJECT
    )


def played_match_of(archive: ArchivePaths, team_name: str) -> str:
    document = json.loads(
        (archive.root / "index" / "teams.json").read_text(encoding="utf-8")
    )
    row = next(team for team in document["teams"] if team["name"] == team_name)
    return row["played_match_ids"][0]


def team_roster_in_match(
    archive: ArchivePaths, match_id: str, faction_ids: set[str]
) -> list[str]:
    """The match's own side's roster from the match index."""
    document = json.loads(
        (archive.root / "index" / "matches.json").read_text(encoding="utf-8")
    )
    row = next(m for m in document["matches"] if m["match_id"] == match_id)
    side = next(s for s in row["teams"] if s["faction_id"] in faction_ids)
    return list(side["roster"])


def rows_by_index(archive: ArchivePaths) -> dict[int, dict[str, Any]]:
    document = read_selection(archive, subject_key(archive))
    return {row["map_index"]: row for row in document["selections"]}


# -- A row is born only out of played matches -------------------------------


def test_only_played_matches_produce_rows(league, archive, thresholds) -> None:
    """Acceptance criterion: 1 played match -> 2 MapDemos.

    The subject has 11 matches of which one has been played -- the same ratio
    as Rcave Veterans in the real data.
    """
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.stats["map_demos"] == 2
    assert result.stats["matches_with_maps"] == 1
    assert result.stats["matches_seen"] == 11
    document = read_selection(archive, subject_key(archive))
    assert len(document["selections"]) == 2
    assert {row["map_index"] for row in document["selections"]} == {0, 1}


def test_a_scheduled_match_produces_no_row_at_all(league, archive, thresholds) -> None:
    """I/O matrix: an unplayed match, ``map_picks`` empty -> no row at all.

    Measured 2026-09-04: ``map_picks`` is empty in 60 matches out of 66. A
    scheduled match is not "the selection is waiting" but "does not exist yet".
    """
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert len({row["match_id"] for row in document["selections"]}) == 1
    assert result.stats["matches_not_played"] == 10
    assert result.stats["matches_without_veto"] == 0


def test_the_match_counters_add_up_to_every_match_the_team_has(
    league, archive, thresholds
) -> None:
    """A match must not disappear between the counters.

    ``matches_with_maps + matches_not_played + matches_without_veto ==
    matches_seen``. Without that equation a dropped match would be left
    unexplained -- and that is exactly why the skips are counted one reason at
    a time.
    """
    index(archive, league, thresholds)

    stats = select(league, archive, thresholds).stats

    assert (
        stats["matches_with_maps"]
        + stats["matches_not_played"]
        + stats["matches_without_veto"]
        == stats["matches_seen"]
    )


def test_a_played_match_without_veto_is_not_called_unplayed(
    league, archive, thresholds
) -> None:
    """The port's contract: an empty ``map_picks`` is "no veto data", not "no maps".

    An earlier version lumped this in with an unplayed match and printed the
    reason *"because they have not been played"* -- a claim that is not true
    of a played match. The maps were played; we do not know which.
    """
    matches = tuple(
        replace(match, map_picks=()) if match.match_id == PLAYED_MATCH else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    assert result.stats["map_demos"] == 0
    assert result.stats["matches_without_veto"] == 1
    assert result.stats["matches_not_played"] == 10
    notes = " ".join(result.stats["notes"])
    assert "veto data is missing" in notes
    assert PLAYED_MATCH in notes
    # Unplayed matches are a note of their own and do not vanish under
    # another.
    assert "still unplayed" in notes


def test_the_unit_identifier_is_match_and_zero_based_map_index(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)
    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    match_id = document["selections"][0]["match_id"]
    assert [row["map_demo_id"] for row in document["selections"]] == [
        f"{match_id}-0",
        f"{match_id}-1",
    ]


def test_a_team_with_no_played_matches_gets_an_empty_file_with_a_reason(
    league, archive, thresholds
) -> None:
    """An empty result is not an error -- but it is not left unexplained."""
    index(archive, league, thresholds)

    result = select(league, archive, thresholds, team="Rcave")

    assert result.status == "ok"
    assert result.stats["map_demos"] == 0
    assert result.reason is not None
    assert "not one of them has been played" in result.reason


# -- Every row has the four fields and always a reason ----------------------


def test_every_row_has_the_four_fields_and_never_a_silent_rejection(
    league, archive, thresholds
) -> None:
    """Acceptance criterion: every row has ``roster_ok``, ``roster_reason``,
    ``roster_class`` and ``is_league`` -- and not one rejection is without a
    reason."""
    index(archive, league, thresholds)
    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    for row in document["selections"]:
        assert set(row) >= {"roster_ok", "roster_reason", "roster_class", "is_league"}
        assert row["roster_reason"].strip()
        if not row["roster_ok"]:
            assert row["roster_class"] is None


def test_a_rejected_row_states_the_numbers_and_the_threshold(
    league, archive, thresholds
) -> None:
    """I/O matrix: 3 regulars -> not eligible, the reason says how many and which
    threshold.

    Two of the played match's starters are on loan from another team; see
    :func:`borrow` on why the outsider is a player of another team rather than
    an invented id.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=3)
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert result.stats["accepted"] == 0
    assert result.stats["rejected"] == 2
    for row in document["selections"]:
        assert row["roster_ok"] is False
        assert "3/5" in row["roster_reason"]
        assert "4/5" in row["roster_reason"]


def test_four_regulars_and_one_outsider_is_accepted_and_the_reason_names_them(
    league, archive, thresholds
) -> None:
    """I/O matrix: 4 regulars + 1 outsider -> eligible, class 4/5.

    The product owner 2026-09-04: the match is against the same team even if in
    another match they had one substituted player. The outsider **is counted
    in**; the difference is in the class.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=4)
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert result.stats["accepted"] == 2
    assert result.stats["class_4/5"] == 2
    for row in document["selections"]:
        assert row["roster_class"] == "4/5"
        assert len(row["outsiders"]) == 1
        assert "From outside the standing roster" in row["roster_reason"]


def test_the_reason_names_the_outsider_by_nickname_end_to_end(
    league, archive, thresholds
) -> None:
    """The name map travels from the stage all the way to the domain.

    Without this claim, removing the ``names`` parameter would break nothing:
    every reason would name the outsider by a 17-digit id, and the whole
    "readable reason" promise would disappear unnoticed.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=4)
    index(archive, league, thresholds, matches=matches)

    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    for row in document["selections"]:
        outsider = row["outsiders"][0]
        assert outsider not in row["roster_reason"], "the id instead of the name"
        # The lender's nicknames are of the form "takakeno1".
        assert "takakeno" in row["roster_reason"]


def test_a_full_regular_lineup_is_the_full_class(league, archive, thresholds) -> None:
    """I/O matrix: a played league match, 5 regulars -> 2 rows, 5/5."""
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.stats["class_5/5"] == 2
    assert result.stats["accepted"] == 2


# -- is_league is inferred from the id, not from the name --------------------


def test_is_league_is_true_for_matches_in_the_configured_championship(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)
    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert all(row["is_league"] for row in document["selections"])


def test_a_match_outside_the_league_still_gets_a_row_but_is_not_league(
    archive, thresholds
) -> None:
    """I/O matrix: ``competition_id`` not on the list -> a row is born,
    ``is_league`` false.

    The sample comes from outside the league -- that is the whole epic's core
    -- so the row **has** to be born. The difference is in ``is_league``, not
    in who is included.
    """
    outside = "muu-kilpailu-00000000"
    matches = tuple(
        replace(match, competition_id=outside) for match in division_matches()
    )
    fetching = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[outside],
        map_pool=["de_ancient"],
    )
    configured = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP],
        map_pool=["de_ancient"],
    )
    source = FakeSource({outside: matches})
    discover_stage.run(fetching, archive, None, source=source, thresholds=thresholds)

    result = select_stage.run(configured, archive, "Potku", thresholds=thresholds)

    document = read_selection(archive, subject_key(archive))
    assert len(document["selections"]) == 2
    assert not any(row["is_league"] for row in document["selections"])
    assert result.stats["league"] == 0


def test_is_league_is_not_read_from_the_competition_name(archive, thresholds) -> None:
    """The name is a string written by a human; the decision is from the id."""
    misleading = "6-divisioona-vaara-tunniste"
    matches = tuple(
        replace(match, competition_id=misleading) for match in division_matches()
    )
    fetching = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[misleading],
        map_pool=["de_ancient"],
    )
    configured = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP],
        map_pool=["de_ancient"],
    )
    discover_stage.run(
        fetching,
        archive,
        None,
        source=FakeSource({misleading: matches}),
        thresholds=thresholds,
    )

    select_stage.run(configured, archive, "Potku", thresholds=thresholds)

    document = read_selection(archive, subject_key(archive))
    assert not any(row["is_league"] for row in document["selections"])


# -- A map in the veto data is not proof that the map was played -------------


def test_a_third_map_in_a_best_of_three_is_not_counted_into_the_sample(
    league, archive, thresholds
) -> None:
    """The product owner confirmed on 4 September: the playoffs are BO3, so this
    is not theoretical.

    In a BO3 that ended 2-0 the veto holds three maps but there are two demos.
    The third row is born -- it does not disappear silently -- but it does not
    reach the sample until a demo proves the map played.
    """
    matches = tuple(
        replace(match, best_of=3, map_picks=("de_ancient", "de_nuke", "de_dust2"))
        if match.match_id == PLAYED_MATCH
        else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert len(rows) == 3
    assert result.stats["accepted"] == 2
    assert result.stats["uncertain"] == 1
    assert rows[2]["roster_ok"] is False
    assert rows[2]["certainly_played"] is False
    assert "match length" in rows[2]["roster_reason"]
    assert rows[0]["roster_ok"] is True
    assert rows[1]["roster_ok"] is True


def test_a_parsed_demo_proves_the_third_map_was_played(
    league, archive, thresholds
) -> None:
    """No demo exists of a map that was not played."""
    matches = tuple(
        replace(match, best_of=3, map_picks=("de_ancient", "de_nuke", "de_dust2"))
        if match.match_id == PLAYED_MATCH
        else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(archive, f"{PLAYED_MATCH}-2", {"kokoonpano": roster})

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[2]["roster_ok"] is True
    assert rows[2]["roster_source"] == "observed"
    assert result.stats["uncertain"] == 0


def test_every_map_of_a_best_of_two_is_certain(league, archive, thresholds) -> None:
    """In a BO2 you cannot win two before both have been played.

    The present regular season is measured as BO2, so the uncertainty rule must
    not drop a single row out of that sample.
    """
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.stats["uncertain"] == 0
    assert result.stats["accepted"] == 2


# -- Prediction and observation ----------------------------------------------


def test_an_unparsed_map_is_a_prediction_from_the_match_roster(
    league, archive, thresholds
) -> None:
    """I/O matrix: the demo has not been parsed -> the class is a prediction, and
    the source says so."""
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert result.stats["predicted"] == 2
    assert result.stats["observed"] == 0
    for row in document["selections"]:
        assert row["roster_source"] == "predicted"
        assert "prediction" in row["roster_reason"]


def test_a_parsed_map_is_an_observation_from_the_demo(
    league, archive, thresholds
) -> None:
    """I/O matrix: ``lineups.parquet`` exists -> the class is an observation."""
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(
        archive,
        f"{PLAYED_MATCH}-0",
        {
            "ff03fb54599d3311": roster,
            "9ac92660986558d3": [steam_id(9000 + n) for n in range(5)],
        },
    )

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "observed"
    assert "observation" in rows[0]["roster_reason"]
    assert rows[1]["roster_source"] == "predicted"
    assert result.stats["observed"] == 1
    assert result.stats["predicted"] == 1


def test_the_observation_wins_and_the_difference_is_told(
    league, archive, thresholds
) -> None:
    """I/O matrix: the parsed lineup differs from the match roster -> the
    observation wins.

    The demo holds four of the match roster's players and one outsider: a
    substitution between maps. The prediction would have been 5/5, the
    observation is 4/5.
    """
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    outsider = steam_id(9999)
    write_lineups(
        archive, f"{PLAYED_MATCH}-1", {"ff03fb54599d3311": roster[:4] + [outsider]}
    )

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_class"] == "5/5"
    assert rows[1]["roster_class"] == "4/5"
    assert rows[1]["joined"] == [outsider]
    assert rows[1]["left"] == [roster[4]]
    assert "differs from the match roster" in rows[1]["roster_reason"]
    assert result.stats["drifted"] == 1


def test_a_short_lineup_is_accepted_but_the_reason_admits_the_size(
    league, archive, thresholds
) -> None:
    """A four-player lineup is not 4/5 because of an outsider.

    The class is ``4/5``, but there is no outsider -- and the row says both, so
    that the reader does not infer a foreign player from the class.
    """
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(archive, f"{PLAYED_MATCH}-0", {"vajaa": roster[:4]})

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_ok"] is True
    assert rows[0]["roster_class"] == "4/5"
    assert rows[0]["outsiders"] == []
    assert rows[0]["players_seen"] == 4
    assert "There were no outsiders" in rows[0]["roster_reason"]


def test_a_long_lineup_is_the_full_class_and_the_reason_admits_the_size(
    league, archive, thresholds
) -> None:
    """A six-player lineup (a reunion) does not claim a "6/6" class."""
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    extra = json.loads(
        (archive.root / "index" / "teams.json").read_text(encoding="utf-8")
    )
    subject = next(t for t in extra["teams"] if t["name"] == SUBJECT)
    sixth = next(
        p["game_player_id"]
        for p in subject["roster"]
        if p["game_player_id"] not in roster
    )
    write_lineups(archive, f"{PLAYED_MATCH}-0", {"pitka": roster + [sixth]})

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_class"] == "5/5"
    assert rows[0]["players_seen"] == 6
    assert "instead of the expected 5" in rows[0]["roster_reason"]


def test_a_demo_without_this_team_does_not_become_a_false_observation(
    league, archive, thresholds
) -> None:
    """Zero players in common is not an observation of this team.

    The row stays a prediction **and says why** -- a silent demotion would look
    like an ordinary prediction, even though the demo exists.
    """
    index(archive, league, thresholds)
    write_lineups(
        archive, f"{PLAYED_MATCH}-0", {"toinen": [steam_id(9000 + n) for n in range(5)]}
    )

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "predicted"
    assert "none of its lineups" in rows[0]["roster_reason"]


def test_a_tie_between_two_lineups_is_not_resolved_by_guessing(
    league, archive, thresholds
) -> None:
    """Drawing lots would attach the opponent's lineup to this team."""
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(
        archive,
        f"{PLAYED_MATCH}-0",
        {
            "a": roster[:2] + [steam_id(9100), steam_id(9101), steam_id(9102)],
            "b": roster[2:4] + [steam_id(9200), steam_id(9201), steam_id(9202)],
        },
    )

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "predicted"
    assert "equally close" in rows[0]["roster_reason"]


def test_an_unreadable_lineup_table_is_told_not_swallowed(
    league, archive, thresholds
) -> None:
    """A broken table must not demote an observation to a prediction without a trace.

    Without the note the row would look like an ordinary prediction, and
    nothing would say that the demo is in the archive and broken. The opposite
    of how ``teams_from_index`` refuses loudly -- but here the run is **not**
    failed: one demo's fault does not stop the others being selected (AD-9).
    """
    index(archive, league, thresholds)
    path = archive.resolve(parsed_table(f"{PLAYED_MATCH}-0", "lineups"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a parquet file")

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "predicted"
    assert "cannot be read" in rows[0]["roster_reason"]
    assert "pappascout parse" in rows[0]["roster_reason"]
    # The other map is intact, and the run went on.
    assert result.stats["map_demos"] == 2


def test_null_identifiers_in_the_lineup_table_do_not_merge_lineups(
    league, archive, thresholds
) -> None:
    """``str(None)`` would merge different lineups into a group "None".

    The outcome would be a lineup that was in no demo at all -- and it could
    cross the threshold.
    """
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_raw_lineups(
        archive,
        f"{PLAYED_MATCH}-0",
        [(None, player) for player in roster[:3]]
        + [(None, steam_id(9300)), ("oikea", roster[0])],
    )

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    # The only valid group is "oikea", which holds one player.
    assert rows[0]["roster_source"] == "observed"
    assert rows[0]["players_seen"] == 1
    assert rows[0]["roster_ok"] is False


# -- The side is matched by any of the team's ids ---------------------------


def test_the_side_is_matched_by_any_of_the_teams_identifiers() -> None:
    """A team can have many source ids (a new season brings a new one).

    The canonical ``team_key`` is one of them, and the match row does not
    necessarily carry that one.
    """
    own = frozenset({"team-key-vanha", "faction-uusi"})
    match = discover_stage.IndexedMatch(
        match_id="1-x",
        teams=(
            discover_stage.IndexedMatchTeam(faction_id="joku-muu"),
            discover_stage.IndexedMatchTeam(faction_id="faction-uusi", name="Me"),
        ),
    )

    side = select_stage._own_side(match, own)

    assert side is not None
    assert side.name == "Me"
    assert select_stage._own_side(match, frozenset({"ei-mikaan"})) is None


# -- The errors say what to do ----------------------------------------------


def test_an_unknown_team_lists_the_known_ones(league, archive, thresholds) -> None:
    """I/O matrix: an unknown ``team_key`` -> an error that lists the teams."""
    index(archive, league, thresholds)

    with pytest.raises(PappascoutError) as excinfo:
        select(league, archive, thresholds, team="Ei olemassa")

    message = str(excinfo.value)
    assert "Rcave Veterans" in message
    assert SUBJECT in message


def test_an_ambiguous_name_asks_instead_of_choosing(league, archive, thresholds) -> None:
    """The prefix ``T`` hits three; the choice is not made silently."""
    index(archive, league, thresholds)

    with pytest.raises(PappascoutError) as excinfo:
        select(league, archive, thresholds, team="T")

    message = str(excinfo.value)
    assert "TUUHEE" in message
    assert "Takakeno" in message
    assert "Tankkiluola vilttiketju" in message


def test_a_missing_index_tells_the_user_to_run_discover(
    league, archive, thresholds
) -> None:
    """I/O matrix: ``index/`` has not been run -> the error says to run discover."""
    with pytest.raises(PappascoutError) as excinfo:
        select(league, archive, thresholds)

    assert "pappascout discover" in str(excinfo.value)


def test_indexes_from_different_runs_are_refused(league, archive, thresholds) -> None:
    """The reader is ``discover``'s, and it refuses to join two different runs."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "teams.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["generated_at"] = "1999-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="from different runs"):
        select(league, archive, thresholds)


def test_a_duplicated_match_is_refused_instead_of_doubling_the_sample(
    league, archive, thresholds
) -> None:
    """Two rows for the same match would put every map into the sample twice."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "matches.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["matches"].append(dict(document["matches"][1]))
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="twice"):
        select(league, archive, thresholds)


def test_a_malformed_match_row_is_refused_not_dropped(
    league, archive, thresholds
) -> None:
    """A skipped match would shorten the sample without anything explaining it."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "matches.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["matches"][0]["map_picks"] = "de_ancient"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="is not a list"):
        select(league, archive, thresholds)


def test_a_malformed_roster_row_is_refused_not_thinned(
    league, archive, thresholds
) -> None:
    """An incomplete roster is the wrong roster threshold, and that is this
    reader's promise.

    The value put into the roster is one that cannot be mistaken for the
    message: the row's repr is in the error too, so a guard that matched the
    value would pass even if the message stopped saying anything.
    """
    index(archive, league, thresholds)
    path = archive.root / "index" / "teams.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["teams"][0]["roster"].append("not-a-roster-row")
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="which is not an object"):
        select(league, archive, thresholds)


# -- The stage does not touch anything else ----------------------------------


def test_the_stage_writes_only_into_the_selections_directory(
    league, archive, thresholds
) -> None:
    """Acceptance criterion: ``aggregates/`` and ``classified/`` are the same byte
    for byte."""
    index(archive, league, thresholds)
    for name in ("aggregates", "classified"):
        directory = archive.root / name / "ff03fb54599d3311"
        directory.mkdir(parents=True)
        (directory / "report.json").write_bytes(b'{"kosketaan": false}')
    before = _snapshot(archive.root)

    select(league, archive, thresholds)

    after = _snapshot(archive.root)
    for name in ("aggregates", "classified"):
        assert {k: v for k, v in after.items() if k.startswith(name)} == {
            k: v for k, v in before.items() if k.startswith(name)
        }
    new = set(after) - set(before)
    assert all(path.startswith("index/selections/") for path in new), new


def test_the_stage_has_no_manifest_and_never_skips(league, archive, thresholds) -> None:
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.manifest_path is None
    assert result.skipped is False


def test_the_write_is_atomic_and_leaves_no_temp_files(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    select(league, archive, thresholds)

    assert not has_temp_leftovers(archive.root)


def test_running_twice_produces_the_same_bytes_apart_from_the_timestamp(
    league, archive, thresholds
) -> None:
    """Diffability: the difference between two runs is readable only in a stable
    order."""
    index(archive, league, thresholds)

    select(league, archive, thresholds)
    first = read_selection(archive, subject_key(archive))
    select(league, archive, thresholds)
    second = read_selection(archive, subject_key(archive))

    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


# -- The file's content can be checked without the settings ------------------


def test_the_file_carries_the_thresholds_and_the_roster_it_decided_against(
    league, archive, thresholds
) -> None:
    """A decision cannot be checked if its ground is elsewhere and has had time to
    change."""
    index(archive, league, thresholds)

    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert document["roster_size"] == 5
    assert document["roster_min_regulars"] == 4
    assert len(document["roster"]) == DIVISION[SUBJECT]
    assert document["team_name"] == SUBJECT
    assert document["competition_ids"] == [CHAMPIONSHIP]
    assert document["index_generated_at"]


def test_the_file_has_a_reader_that_checks_its_version(
    league, archive, thresholds
) -> None:
    """``schema_version`` is written, so it also has to be read.

    Otherwise the version would be a field nobody checks, and every consumer
    would infer the format from which fields exist.
    """
    index(archive, league, thresholds)
    select(league, archive, thresholds)
    key = subject_key(archive)

    path = archive.selection(key)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="format 99"):
        select_stage.read_selection(archive, key)


def test_reading_a_missing_selection_says_which_command_writes_it(
    archive,
) -> None:
    with pytest.raises(PappascoutError, match="pappascout select"):
        select_stage.read_selection(archive, "ei-ole")


def test_the_summary_and_the_file_cannot_disagree(league, archive, thresholds) -> None:
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    for key, value in document["counts"].items():
        assert result.stats[key] == value


def test_the_output_path_is_relative_and_named_by_team_key(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    key = subject_key(archive)
    assert [str(path) for path in result.outputs] == [f"index/selections/{key}.json"]
    assert result.unit == key


# -- The summary renders from a real run's result ---------------------------
#
# These are the test that was missing: previously ``_render_select`` was given
# only a hand-written dictionary, which the stage never produces. A key could
# then disappear between the stage and the output without anyone noticing.


def test_the_summary_renders_from_a_real_stage_result(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    text = _render_select(select(league, archive, thresholds))

    assert SUBJECT in text
    assert "2 / 2 karttaa otantaan" in text
    assert "Vakirosteri" in text
    assert "8 pelaajaa, kynnys 4/5" in text
    assert "Rosteriluokat" in text
    assert "5/5: 2" in text
    assert "index/selections/" in text


def test_the_rejection_block_renders_from_a_real_stage_result(
    league, archive, thresholds
) -> None:
    """The rejection block is printed from the stage's own rows, not from a
    hand-built list.

    Previously renaming a ``stats`` key would have emptied the block without a
    single test failing.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=3)
    index(archive, league, thresholds, matches=matches)
    result = select(league, archive, thresholds)

    text = _render_select(result)

    assert "Hylätyt kartat (2)" in text
    for row in read_selection(archive, subject_key(archive))["selections"]:
        # The reason whole, not truncated.
        assert row["roster_reason"] in text


def test_every_note_reaches_the_summary_on_its_own_line(
    league, archive, thresholds
) -> None:
    """Two notes, and **both** show.

    Previously only the first survived, so that "not one map ended up in the
    sample" swallowed the news of the missing veto data.
    """
    matches = tuple(
        replace(match, map_picks=()) if match.match_id == PLAYED_MATCH else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)

    text = _render_select(select(league, archive, thresholds))

    notes = [line for line in text.splitlines() if "Huomio" in line]
    assert len(notes) == 2
    assert any("still unplayed" in line for line in notes)
    assert any("veto data is missing" in line for line in notes)


def test_the_summary_counts_league_matches_and_sources(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    text = _render_select(select(league, archive, thresholds))

    assert "2 / 2 kartasta" in text
    assert "0 havaintoa demosta, 2 ennustetta ottelurosterista" in text


# -- Helpers -----------------------------------------------------------------


def borrow(matches, faction_id: str, match_id: str, *, keep: int):
    """Lend players from another team in the division **into one match**.

    This is how an outsider player really comes about; with an invented
    ``vieras0`` id it could not be tested at all: the standing roster is by
    ``domain.teams``'s definition **the union over all the team's matches**, so
    anyone who appears on the team's match row is by definition one of its
    regulars. An outsider is therefore a player whom **another team observed
    later** -- then the transfer rule takes him out of this team's roster and
    leaves him in ``released``.

    The loan is therefore made into one match only, and the lender
    (:data:`LENDER`) is a team that still plays after the loan -- it observes
    its own players last.
    """
    from pappascout.adapters.protocols import RosterPlayer

    lender = tuple(
        RosterPlayer(
            player_id=member.player_id or "",
            nickname=member.nickname,
            game_player_id=member.game_player_id,
        )
        for member in members(LENDER, DIVISION[LENDER], offset=TEAM_INDEX[LENDER] * 100)
    )

    changed = []
    for match in matches:
        if match.match_id != match_id:
            changed.append(match)
            continue
        sides = []
        for side in match.teams:
            if side.team_id != faction_id:
                sides.append(side)
                continue
            missing = len(side.roster) - keep
            sides.append(replace(side, roster=side.roster[:keep] + lender[:missing]))
        changed.append(replace(match, teams=tuple(sides)))
    return tuple(changed)


def write_raw_lineups(
    archive: ArchivePaths, map_demo_id: str, pairs: list[tuple[str | None, str | None]]
) -> None:
    """A lineup table in which the key is allowed to be ``null``.

    ``write_lineups`` will not do for this: it builds the rows from a
    dictionary whose key cannot be ``None``.
    """
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "map_demo_id": map_demo_id,
            "lineup_key": key,
            "player_id": player,
            "player_name": None,
            "clan_name": None,
        }
        for key, player in pairs
    ]
    pl.DataFrame(rows, schema=dict(LINEUPS)).write_parquet(path)


def _snapshot(root: Path) -> dict[str, bytes]:
    """The archive's files with their content -- for a byte-for-byte comparison."""
    return {
        str(path.relative_to(root).as_posix()): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# -- Story 3.7: a write error is the user's situation, not a programming error --


def test_a_write_that_fails_on_disk_is_a_clear_error_with_advice(
    league, archive, thresholds
) -> None:
    """I/O matrix: ``select`` cannot write the selection file.

    **The real stage, a real write.** A directory is put in the target's place,
    so that ``os.replace`` really fails with an ``OSError`` at the atomic
    write's last step -- a faked write would prove only that the fake raises
    what it was asked to raise.

    The read path caught ``OSError`` already, the write path did not:
    unhandled, the error showed on the screen as the text "Unexpected error:
    [Errno 28]" and the advice "This is a programming error" -- that is, the
    wrong diagnosis and the wrong action.
    """
    index(archive, league, thresholds)
    kohde = archive.selection(subject_key(archive))
    kohde.parent.mkdir(parents=True, exist_ok=True)
    kohde.mkdir()
    (kohde / "esteena.txt").write_text("x", encoding="utf-8")

    with pytest.raises(PappascoutError) as err:
        select(league, archive, thresholds)

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
