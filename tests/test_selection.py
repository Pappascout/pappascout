"""``domain.selection`` -- tests for the roster threshold (Story 3.3).

The module is pure, so not one test in this file touches the network or the
disk: the sets of players are built by hand. The ids are of the right shape
for a SteamID64 (``test_teams.steam_id``), because the threshold is a set
operation between them -- an invented ``"a"`` would pass the test but would
say nothing about what happens in real data.

Six things are locked down here:

* **The threshold is a set operation.** 5/5 qualifies, 4/5 qualifies, 3/5 does
  not -- and an outsider is counted into the sample, not out of it.
* **The row's invariants are in the structure.** An empty reason, a rejection
  with a class, an acceptance without a class and an unknown source are all
  impossible to build.
* **The source is said out loud.** A parsed demo is an observation, an
  unparsed one a prediction.
* **The observation wins, and the difference is told** -- but no difference is
  claimed when there is nothing to compare against.
* **The class and the ratio must not claim different things.** Four regulars
  without an outsider and six regulars are both cases in which the class's
  denominator differs from the size of the lineup -- and the row says so.
* **A map in the veto data is not proof that the map was played.**
"""

from __future__ import annotations

import pytest
from test_teams import steam_id

from pappascout.constants import ROSTER_CLASSES
from pappascout.domain.selection import (
    ROSTER_SOURCE_FI,
    ROSTER_SOURCES,
    MapCandidate,
    MapSelection,
    class_labels,
    counts,
    evaluate,
    guaranteed_maps,
    map_demo_id,
    select_maps,
    sort_key,
)
from pappascout.errors import SettingsError

#: The measured defaults of the ``[thresholds]`` section
#: (settings.toml:464-473).
ROSTER_SIZE = 5
MIN_REGULARS = 4

#: A seven-player standing roster, the same size as Rcave Veterans' (measured).
REGULARS = tuple(steam_id(index) for index in range(1, 8))
OUTSIDER = steam_id(90)
SECOND_OUTSIDER = steam_id(91)

MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"

NAMES = {
    OUTSIDER: "guest",
    SECOND_OUTSIDER: "second_guest",
    REGULARS[0]: "SSStttNNN",
}


def candidate(
    on_map: tuple[str, ...],
    *,
    index: int = 0,
    observed: tuple[str, ...] | None = None,
    is_league: bool = True,
    map_name: str = "de_ancient",
    certainly_played: bool = True,
    observation_note: str | None = None,
) -> MapCandidate:
    """One map: the match roster as the prediction, the demo's lineup as the
    observation."""
    return MapCandidate(
        map_demo_id=map_demo_id(MATCH, index),
        match_id=MATCH,
        map_index=index,
        map_name=map_name,
        is_league=is_league,
        certainly_played=certainly_played,
        match_roster=frozenset(on_map),
        observed_players=None if observed is None else frozenset(observed),
        observation_note=observation_note,
    )


def decide(
    candidate_: MapCandidate, roster: tuple[str, ...] = REGULARS
) -> MapSelection:
    return evaluate(
        candidate_,
        roster=frozenset(roster),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
        names=NAMES,
    )


def row(**overrides) -> dict:
    """The fields of a valid row, so that an invariant test changes only one."""
    base = {
        "map_demo_id": "1-x-0",
        "match_id": "1-x",
        "map_index": 0,
        "map_name": None,
        "is_league": False,
        "roster_ok": True,
        "roster_reason": "Eligible.",
        "roster_class": "5/5",
        "roster_source": "predicted",
    }
    base.update(overrides)
    return base


# -- The threshold as a set operation ----------------------------------------


def test_five_regulars_is_accepted_as_the_full_class() -> None:
    """The I/O matrix: 5 regulars -> eligible, class 5/5."""
    decided = decide(candidate(REGULARS[:5]))

    assert decided.roster_ok is True
    assert decided.roster_class == "5/5"
    assert decided.outsiders == ()
    assert len(decided.regulars) == 5


def test_four_regulars_and_one_outsider_is_accepted() -> None:
    """The product owner on 2026-09-04: the match is against the same team
    even if one of them is a sub.

    The outsider **is counted in** -- the difference is in the class, not in
    who is in the sample.
    """
    decided = decide(candidate(REGULARS[:4] + (OUTSIDER,)))

    assert decided.roster_ok is True
    assert decided.roster_class == "4/5"
    assert decided.outsiders == (OUTSIDER,)
    assert decided.players_seen == 5


def test_three_regulars_is_rejected_and_the_reason_names_the_threshold() -> None:
    """The I/O matrix: 3 regulars -> not eligible, the reason gives the
    numbers and the threshold."""
    decided = decide(candidate(REGULARS[:3] + (OUTSIDER, SECOND_OUTSIDER)))

    assert decided.roster_ok is False
    assert decided.roster_class is None
    assert "3/5" in decided.roster_reason
    assert "4/5" in decided.roster_reason


def test_no_rejection_is_ever_without_a_reason() -> None:
    """The frozen rule: ``roster_ok = false`` without a reason is
    forbidden."""
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:3] + (OUTSIDER, SECOND_OUTSIDER), index=1),
            candidate((), index=2),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    assert len(rows) == 3
    for decided in rows:
        assert decided.roster_reason.strip()


# -- The row's invariants are in the structure -------------------------------


def test_a_row_without_a_reason_cannot_be_built_at_all() -> None:
    with pytest.raises(ValueError, match="has no reason"):
        MapSelection(**row(roster_reason="   "))


def test_a_rejected_row_cannot_carry_a_class() -> None:
    """A class on a rejected row would be a claim about rounds that are not
    counted."""
    with pytest.raises(ValueError, match="carries the roster class"):
        MapSelection(**row(roster_ok=False, roster_class="4/5"))


def test_an_accepted_row_cannot_be_missing_its_class() -> None:
    """Such a row would be in the sample but in neither class counter.

    Without this claim ``accepted != class_5/5 + class_4/5`` would be possible
    without anything shouting -- and that is exactly the invariant
    :func:`counts` promises.
    """
    with pytest.raises(ValueError, match="has no roster class"):
        MapSelection(**row(roster_ok=True, roster_class=None))


def test_an_unknown_source_cannot_be_built() -> None:
    """``Literal`` is a check for the type checker, not at run time.

    Without the guard an invalid value would be built and would blow up only
    as a KeyError from ``source_fi`` somewhere else entirely.
    """
    with pytest.raises(ValueError, match="The source of the selection row"):
        MapSelection(**row(roster_source="guessed"))


def test_the_counts_invariants_hold_for_every_row() -> None:
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:4] + (OUTSIDER,), index=1),
            candidate(REGULARS[:2], index=2),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )
    numbers = counts(rows)

    assert numbers["accepted"] + numbers["rejected"] == numbers["map_demos"]
    assert (
        sum(numbers[f"class_{label}"] for label in ROSTER_CLASSES)
        == numbers["accepted"]
    )


# -- The reason is readable and names the people -----------------------------


def test_the_reason_names_the_outsider_by_nickname() -> None:
    """The I/O matrix: the reason says **who** the outsider was, not just how
    many."""
    decided = decide(candidate(REGULARS[:4] + (OUTSIDER,)))

    assert "guest" in decided.roster_reason
    assert OUTSIDER not in decided.roster_reason


def test_an_unnamed_outsider_falls_back_to_the_identifier() -> None:
    """The nickname may be missing; an invented name would point at the wrong
    player."""
    unknown = steam_id(555)
    decided = decide(candidate(REGULARS[:4] + (unknown,)))

    assert unknown in decided.roster_reason


def test_an_observation_note_is_carried_into_the_reason() -> None:
    """A broken lineup table must not vanish without a trace.

    Without this chain an observation would silently demote itself into a
    prediction: the row would look like an ordinary prediction although the
    demo exists and is broken.
    """
    note = "Note: the demo's lineup table exists but is not readable."
    decided = decide(candidate(REGULARS[:5], observation_note=note))

    assert note in decided.roster_reason
    assert decided.roster_source == "predicted"


def test_an_unknown_roster_is_its_own_reason_not_a_failed_threshold() -> None:
    """Without a standing roster, falling short of the threshold is not a true
    claim."""
    decided = decide(candidate(REGULARS[:5]), roster=())

    assert decided.roster_ok is False
    assert "standing roster is not known" in decided.roster_reason
    assert "the threshold is" not in decided.roster_reason


def test_a_map_without_any_known_players_is_rejected() -> None:
    """An empty match roster is not a failed threshold but an unknown
    lineup."""
    decided = decide(candidate(()))

    assert decided.roster_ok is False
    assert decided.roster_class is None
    assert "is not known" in decided.roster_reason


def test_a_parsed_demo_without_this_team_is_rejected_with_its_own_reason() -> None:
    """An observed empty is a different thing from "not known", and the reason
    says so."""
    decided = decide(candidate(REGULARS[:5], observed=()))

    assert decided.roster_ok is False
    assert decided.roster_source == "observed"
    assert "The demo has been parsed" in decided.roster_reason


# -- The class and the ratio do not claim different things -------------------


def test_four_regulars_and_nobody_else_says_there_was_no_outsider() -> None:
    """The class ``4/5`` on its own would claim an outsider who is not there.

    In a four-player lineup four regulars meet the threshold, but the fifth
    place is empty rather than a guest's. The row says both.
    """
    decided = decide(candidate(REGULARS[:4]))

    assert decided.roster_ok is True
    assert decided.roster_class == "4/5"
    assert decided.outsiders == ()
    assert "There were no outsiders" in decided.roster_reason
    assert "4 players instead of the expected 5" in decided.roster_reason


def test_six_regulars_is_the_full_class_and_the_reason_admits_the_size() -> None:
    """Without a cap the class would be 5/5 and the reason would say "6/6" --
    different denominators."""
    decided = decide(candidate(REGULARS[:6]))

    assert decided.roster_class == "5/5"
    assert decided.players_seen == 6
    assert "6/6" in decided.roster_reason
    assert "6 players instead of the expected 5" in decided.roster_reason


def test_a_full_five_player_lineup_says_nothing_about_the_size() -> None:
    """The note is there for the anomaly; the normal case does not need it."""
    decided = decide(candidate(REGULARS[:5]))

    assert "instead of the expected" not in decided.roster_reason


# -- Prediction vs. observation ----------------------------------------------


def test_an_unparsed_map_is_a_prediction_and_says_so() -> None:
    """The I/O matrix: the demo has not been parsed -> the class is a
    prediction from the match roster."""
    decided = decide(candidate(REGULARS[:5]))

    assert decided.roster_source == "predicted"
    assert decided.source_fi == "prediction"
    assert "prediction" in decided.roster_reason


def test_a_parsed_map_is_an_observation_and_says_so() -> None:
    """The I/O matrix: ``lineups.parquet`` exists -> the class is an
    observation."""
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:5]))

    assert decided.roster_source == "observed"
    assert decided.source_fi == "observation"
    assert "observation" in decided.roster_reason


def test_the_observation_wins_over_the_match_roster() -> None:
    """The I/O matrix: the parsed lineup differs from the match roster -> the
    observation wins.

    The match roster holds five regulars, the demo four and one outsider: a
    substitution between maps. The class is **4/5**, not 5/5.
    """
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:4] + (OUTSIDER,)))

    assert decided.roster_class == "4/5"
    assert decided.roster_source == "observed"
    assert decided.outsiders == (OUTSIDER,)


def test_the_difference_to_the_match_roster_is_told_not_silenced() -> None:
    """The I/O matrix: the difference is told -- a substitution is exactly
    what the threshold is there for."""
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:4] + (OUTSIDER,)))

    assert decided.drifted is True
    assert decided.joined == (OUTSIDER,)
    assert decided.left == (REGULARS[4],)
    assert "differs from the match roster" in decided.roster_reason
    assert "guest" in decided.roster_reason


def test_no_difference_is_claimed_when_there_is_no_match_roster() -> None:
    """Without a match roster the whole observation would be a "difference",
    and that would be a false claim."""
    decided = evaluate(
        MapCandidate(
            map_demo_id=map_demo_id(MATCH, 0),
            match_id=MATCH,
            map_index=0,
            observed_players=frozenset(REGULARS[:5]),
        ),
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    assert decided.drifted is False
    assert decided.joined == ()
    assert decided.left == ()


def test_no_difference_is_claimed_when_the_observation_is_empty() -> None:
    """The row must not report a substitution on a map whose lineup is
    unknown.

    An empty observation would produce ``left`` = the whole match roster in
    the comparison, and the output would report "a substitution between maps"
    for a map whose own reason says the lineup is not known. Two claims, one
    of which is false.
    """
    decided = decide(candidate(REGULARS[:5], observed=()))

    assert decided.drifted is False
    assert decided.left == ()
    assert "differs from the match roster" not in decided.roster_reason


def test_an_identical_observation_reports_no_difference() -> None:
    decided = decide(candidate(REGULARS[:5], observed=REGULARS[:5]))

    assert decided.drifted is False
    assert "differs" not in decided.roster_reason


# -- A map in the veto data is not proof that the map was played -------------


@pytest.mark.parametrize(
    "best_of,expected",
    [(1, 1), (2, 2), (3, 2), (4, 3), (5, 3), (None, None), (0, None)],
)
def test_the_guaranteed_map_count_comes_from_the_match_length(
    best_of: int | None, expected: int | None
) -> None:
    """BO2 is entirely certain, BO3 only as to two of the maps."""
    assert guaranteed_maps(best_of) == expected


def test_a_map_that_may_not_have_been_played_is_kept_out_of_the_sample() -> None:
    """In a BO3 that ended 2-0 the veto holds three maps but there are two
    demos.

    Without this rule the third map would be in the sample as a full 5/5 map,
    although it may not have been played at all.
    """
    decided = decide(candidate(REGULARS[:5], index=2, certainly_played=False))

    assert decided.roster_ok is False
    assert decided.roster_class is None
    assert decided.certainly_played is False
    assert "match length" in decided.roster_reason


def test_a_parsed_demo_proves_the_map_was_played() -> None:
    """A demo does not exist for a map that was not played -- the observation
    is the proof."""
    decided = decide(
        candidate(REGULARS[:5], index=2, certainly_played=False, observed=REGULARS[:5])
    )

    assert decided.roster_ok is True
    assert decided.roster_class == "5/5"
    assert decided.roster_source == "observed"


def test_uncertain_maps_are_counted_separately() -> None:
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:5], index=1),
            candidate(REGULARS[:5], index=2, certainly_played=False),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    numbers = counts(rows)
    assert numbers["uncertain"] == 1
    assert numbers["accepted"] == 2


# -- is_league passes through unchanged --------------------------------------


def test_is_league_is_carried_through_unchanged_in_both_directions() -> None:
    """The decision is made by the stage from ``competition_id``; the domain
    guesses neither."""
    league = decide(candidate(REGULARS[:5], is_league=True))
    other = decide(candidate(REGULARS[:5], is_league=False))

    assert league.is_league is True
    assert other.is_league is False


# -- The id ------------------------------------------------------------------


def test_the_unit_identifier_is_match_id_and_zero_based_map_index() -> None:
    assert map_demo_id(MATCH, 0) == f"{MATCH}-0"
    assert map_demo_id(MATCH, 1) == f"{MATCH}-1"


@pytest.mark.parametrize("match,index", [("", 0), (MATCH, -1)])
def test_an_impossible_unit_identifier_is_refused(match: str, index: int) -> None:
    """A silent ``-1`` suffix would point at a file that does not exist."""
    with pytest.raises(ValueError):
        map_demo_id(match, index)


# -- The class names are derived from the thresholds -------------------------


def test_the_class_labels_come_from_the_thresholds() -> None:
    assert class_labels(ROSTER_SIZE, MIN_REGULARS) == ("5/5", "4/5")
    assert set(class_labels(ROSTER_SIZE, MIN_REGULARS)) <= set(ROSTER_CLASSES)


def test_a_threshold_that_would_invent_a_class_is_a_settings_error() -> None:
    """``roster_size = 6`` is a valid PositiveInt but a wrong setting.

    **A settings error, not a program error**: a bare ``ValueError`` would
    reach the command line as "Unexpected error -- a program error", exit 2,
    although the fix is in the user's own settings.toml.
    """
    with pytest.raises(SettingsError, match="known classes"):
        class_labels(6, 5)


@pytest.mark.parametrize("size,minimum", [(0, 0), (5, 0), (5, 6), (-1, 1)])
def test_impossible_thresholds_are_settings_errors(size: int, minimum: int) -> None:
    with pytest.raises(SettingsError, match="settings.toml"):
        class_labels(size, minimum)


# -- The order and the summary numbers ---------------------------------------


def test_rows_sort_by_match_and_then_map_index() -> None:
    """Diffability: the difference between two runs is readable only in a
    stable order."""
    rows = [
        decide(candidate(REGULARS[:5], index=1)),
        decide(candidate(REGULARS[:5], index=0)),
    ]

    assert [r.map_index for r in sorted(rows, key=sort_key)] == [0, 1]


def test_the_counts_are_computed_from_the_rows_themselves() -> None:
    """The file and the summary cannot say different things, because the
    source is the same."""
    rows = select_maps(
        [
            candidate(REGULARS[:5], index=0),
            candidate(REGULARS[:4] + (OUTSIDER,), index=1, is_league=False),
            candidate(REGULARS[:3] + (OUTSIDER, SECOND_OUTSIDER), index=2),
            candidate(REGULARS[:5], index=3, observed=REGULARS[:5]),
        ],
        roster=frozenset(REGULARS),
        roster_size=ROSTER_SIZE,
        roster_min_regulars=MIN_REGULARS,
    )

    numbers = counts(rows)

    assert numbers["map_demos"] == 4
    assert numbers["accepted"] == 3
    assert numbers["rejected"] == 1
    assert numbers["league"] == 3
    assert numbers["observed"] == 1
    assert numbers["predicted"] == 3
    assert numbers["class_5/5"] == 2
    assert numbers["class_4/5"] == 1


def test_the_source_names_cover_every_source() -> None:
    assert set(ROSTER_SOURCE_FI) == set(ROSTER_SOURCES)
    assert ROSTER_SOURCE_FI["observed"] == "observation"
    assert ROSTER_SOURCE_FI["predicted"] == "prediction"
