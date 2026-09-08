"""``domain.teams`` -- tests for the name lookup and the standing roster
(Story 3.2).

**Real names, not invented ones.** The division's 12 teams and their measured
roster sizes come from sections 3 and 4 of ``mittaus-faceit-aineisto.md``, and
they are written here as they stand. With an invented list of teams the
ambiguity of the name lookup would be a theoretical case that one could
accidentally make easy; with the real names it is what the user actually
types.

Rcave Veterans' four SteamID64s are measured (section 2, the intersection with
the archive's ``lineups.parquet``). The rest are **constructed** from a running
number -- they are of the right shape but are not real accounts, and that is
said here out loud, so that nobody later takes them for a measurement.

Not one test goes on the network or to the disk: the module is pure, and the
observations are built by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pappascout.domain.teams import (
    STEAM_ID64_BASE,
    STEAM_ID64_MAX,
    RosterMember,
    Team,
    TeamObservation,
    assign_lineup_keys,
    build_teams,
    find_teams,
    is_steam_id64,
)

#: The division's teams and their **measured** standing roster sizes
#: (section 3).
DIVISION: dict[str, int] = {
    "popsiCS": 9,
    "JYSAEYTTAEJAET": 8,
    "PotkukelkkaPeek": 8,
    "Suhisevat Sukat": 8,
    "TUUHEE": 8,
    "Takakeno": 8,
    "YllatysMomentti": 8,
    "cM Esports": 8,
    "KASIKAASU": 7,
    "Rcave Veterans": 7,
    "uncs67": 7,
    "Tankkiluola vilttiketju": 6,
}

#: The measured nicknames of two teams (section 3).
RCAVE = (
    "HCNoRage",
    "Kronnennn",
    "Lindberq_",
    "MarkusN",
    "SSStttNNN",
    "bobb_y",
    "pornopertti",
)
POTKU = (
    "-Kurittaja-",
    "Jekkuekku",
    "Kisuisukki",
    "MyrkkyPena",
    "Patteri",
    "miicco",
    "progepanda",
    "wormi27z",
)

#: The measured SteamID64s: these four were found both in FACEIT and in the
#: archive's demo ``ANCIENT_vs_RCAVE_VETERANS`` (section 2). In the
#: coordinator's live run the intersection was in the end **5**, once the
#: substitutes were counted in.
MEASURED_RCAVE_IDS = {
    "SSStttNNN": "76561197977479426",
    "pornopertti": "76561197985923425",
    "HCNoRage": "76561197993527314",
    "bobb_y": "76561198062941501",
}

#: Round robin, 12 teams: 11 matches per team.
MATCHES_PER_TEAM = 11

#: The threshold at which two source ids are the same team
#: (``[thresholds].team_identity_min_common``, default 3).
MIN_COMMON = 3

KICKOFF = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


def steam_id(number: int) -> str:
    """A constructed but correctly shaped SteamID64."""
    return str(STEAM_ID64_BASE + number)


def nicknames(team_name: str, size: int) -> tuple[str, ...]:
    if team_name == "Rcave Veterans":
        return RCAVE
    if team_name == "PotkukelkkaPeek":
        return POTKU
    slug = team_name.split()[0].lower()
    return tuple(f"{slug}{index}" for index in range(1, size + 1))


def members(team_name: str, size: int, offset: int) -> tuple[RosterMember, ...]:
    return tuple(
        RosterMember(
            game_player_id=MEASURED_RCAVE_IDS.get(nick, steam_id(offset + index)),
            nickname=nick,
            player_id=f"uuid-{offset + index}",
        )
        for index, nick in enumerate(nicknames(team_name, size))
    )


def division_observations(
    played: dict[str, int] | None = None,
) -> list[TeamObservation]:
    """The division's observations: 12 teams, 11 matches for each.

    The standing roster is **spread across the matches**: every match has five
    starters and the rest as substitutes, and the set of starters rotates.
    This way no single match's row holds the whole roster on its own -- that
    is, the test measures the union rather than the latest match.
    """
    played = played or {}
    observations: list[TeamObservation] = []
    for team_index, (team_name, size) in enumerate(DIVISION.items()):
        roster = members(team_name, size, offset=team_index * 100)
        played_count = played.get(team_name, MATCHES_PER_TEAM)
        for match_no in range(MATCHES_PER_TEAM):
            shift = match_no % size
            rotated = roster[shift:] + roster[:shift]
            observations.append(
                TeamObservation(
                    faction_id=f"faction-{team_index:02d}",
                    match_id=f"1-t{team_index:02d}-m{match_no:02d}",
                    observed_at=KICKOFF + timedelta(days=match_no),
                    name=team_name,
                    played=match_no < played_count,
                    roster=rotated[:5],
                    substitutes=rotated[5:],
                )
            )
    return observations


@pytest.fixture
def division() -> tuple[Team, ...]:
    """The division, in which PotkukelkkaPeek has played 1 match out of 11."""
    return build_teams(
        division_observations(played={"PotkukelkkaPeek": 1}), min_common=MIN_COMMON
    )


# -- The SteamID64 is the roster's only id -----------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("76561197977479426", True),
        ("76561198062941501", True),
        (str(STEAM_ID64_BASE), True),
        (str(STEAM_ID64_MAX), True),
        # FACEIT's own player_id is a UUID and does not occur in the demos at
        # all.
        ("f56dd02a-6107-48e2-abfb-75e7ec7ebcb2", False),
        # The right length, but smaller than the smallest possible SteamID64.
        ("12345678901234567", False),
        # The right length and larger than the lower bound, but larger than
        # the id of any account that exists. Without the upper bound this
        # would pass.
        ("99999999999999999", False),
        (str(STEAM_ID64_MAX + 1), False),
        ("7656119797747942", False),
        ("765611979774794267", False),
        ("", False),
        (76561197977479426, False),
        (None, False),
    ],
)
def test_only_steam_id64_shaped_values_are_identifiers(
    value: object, expected: bool
) -> None:
    assert is_steam_id64(value) is expected


def test_the_upper_bound_is_the_largest_possible_account_id() -> None:
    """The upper bound is not a guess: it is the account number's 32-bit
    ceiling."""
    assert STEAM_ID64_MAX == STEAM_ID64_BASE + 0xFFFFFFFF
    assert len(str(STEAM_ID64_MAX)) == 17


def test_a_roster_member_without_a_steam_id_cannot_exist() -> None:
    """The id is the key: a wrongly shaped one would not join to the demos but
    would vanish.

    The error comes at once when the object is built and not only in the join,
    where it would look like an empty intersection -- that is, like an
    observation rather than an error.
    """
    with pytest.raises(ValueError, match="SteamID64"):
        RosterMember(game_player_id="f56dd02a-6107-48e2-abfb-75e7ec7ebcb2")


def test_every_identifier_in_every_roster_is_steam_id64_shaped(
    division: tuple[Team, ...],
) -> None:
    """The acceptance criterion: every id is in SteamID64 form."""
    for team in division:
        assert team.player_ids, team.team_key
        assert all(is_steam_id64(pid) for pid in team.player_ids), team.team_key


# -- The standing roster is a union ------------------------------------------


def test_the_standing_roster_is_the_union_not_the_latest_match() -> None:
    """The I/O matrix: the roster varies between matches.

    Three matches, each with five starters but a different five. The latest
    match would give five players; the union gives seven.
    """
    roster = members("Rcave Veterans", 7, offset=0)
    observations = [
        TeamObservation(
            faction_id="f56dd02a",
            match_id=f"1-m{index}",
            observed_at=KICKOFF + timedelta(days=index),
            name="Rcave Veterans",
            roster=roster[index : index + 5],
        )
        for index in range(3)
    ]

    (team,) = build_teams(observations, min_common=MIN_COMMON)

    assert len(team.roster) == 7
    assert team.player_ids == {member.game_player_id for member in roster}


def test_substitutes_are_part_of_the_standing_roster() -> None:
    """Measured: without ``substitutes`` the roster underestimates
    systematically.

    ``Lindberq_`` is in a demo in the archive but not once in ``roster``, so
    the list of starters alone would leave him out -- and the roster threshold
    (Story 3.3) would therefore be one short every time. In the coordinator's
    live run the intersection with the substitutes was 5/5 and not 4/5.
    """
    roster = members("Rcave Veterans", 7, offset=0)
    starters = tuple(m for m in roster if m.nickname != "Lindberq_")[:5]
    lindberq = next(m for m in roster if m.nickname == "Lindberq_")

    (team,) = build_teams(
        [
            TeamObservation(
                faction_id="f56dd02a",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="Rcave Veterans",
                roster=starters,
                substitutes=(lindberq,),
            )
        ],
        min_common=MIN_COMMON,
    )

    assert "Lindberq_" in [member.nickname for member in team.roster]


def test_a_missing_substitutes_list_is_not_an_error() -> None:
    """The I/O matrix: ``substitutes`` is missing or empty."""
    roster = members("KASIKAASU", 5, offset=0)

    (team,) = build_teams(
        [
            TeamObservation(
                faction_id="k",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="KASIKAASU",
                roster=roster,
            )
        ],
        min_common=MIN_COMMON,
    )

    assert len(team.roster) == 5


def test_the_number_of_played_matches_does_not_change_the_roster(
    division: tuple[Team, ...],
) -> None:
    """The acceptance criterion: PotkukelkkaPeek, 1 played match out of 11.

    The standing roster is full, because it is read from **all** the matches
    -- the unplayed ones too. If the roster depended on the played matches,
    not one team would be identifiable at the start of a season.
    """
    lookup = find_teams(division, "PotkukelkkaPeek")

    assert lookup.is_unique
    assert lookup.team.matches_played == 1
    assert len(lookup.team.match_ids) == MATCHES_PER_TEAM
    assert [m.nickname for m in lookup.team.roster] == list(POTKU)


def test_every_roster_in_the_division_is_between_six_and_nine_players(
    division: tuple[Team, ...],
) -> None:
    """The acceptance criterion: rosters of 6--9 players. The epic's promise
    was 5--10."""
    sizes = {team.display_name: len(team.roster) for team in division}

    assert sizes == DIVISION
    assert min(sizes.values()) == 6
    assert max(sizes.values()) == 9


def test_building_teams_does_not_depend_on_observation_order() -> None:
    """The same input in a different order is the same result -- otherwise the
    index would wobble."""
    observations = division_observations()

    assert build_teams(observations, min_common=MIN_COMMON) == build_teams(
        list(reversed(observations)), min_common=MIN_COMMON
    )


# -- Identity is the roster, not the id --------------------------------------


def two_seasons(
    *, shared: int, first_key: str = "season-12", second_key: str = "season-13"
) -> list[TeamObservation]:
    """The same team in two seasons, two different source ids.

    ``shared`` says how many of the five-player roster are the same in both
    seasons. The rest are new players.
    """
    old = members("season", 5, offset=0)
    new = old[:shared] + members("newcomers", 5, offset=500)[shared:]
    return [
        TeamObservation(
            faction_id=first_key,
            match_id="1-old",
            observed_at=KICKOFF,
            name="Rcave Veterans",
            roster=old,
        ),
        TeamObservation(
            faction_id=second_key,
            match_id="1-new",
            observed_at=KICKOFF + timedelta(days=365),
            name="Rcave Veterans",
            roster=new,
        ),
    ]


def test_a_new_season_identifier_is_the_same_team_when_the_roster_stays() -> None:
    """The epic's AC3: a change of name or of division does not break the
    identity.

    A new season gives the same group of people a new ``faction_id``. If the
    id were the identity, the result would be two teams that nothing connects
    -- and the whole archive's history would break at the turn of the season.

    **This cannot be verified against the live data**: the settings hold one
    championship, and the measured result was exactly one id per team.
    """
    teams = build_teams(two_seasons(shared=4), min_common=MIN_COMMON)

    assert len(teams) == 1
    assert teams[0].faction_ids == ("season-12", "season-13")


def test_the_canonical_key_is_the_earliest_identifier_and_does_not_change() -> None:
    """``team_key`` does not change when a new season brings a new id.

    The canonical id is the id of the **earliest observation**, and the order
    comes from ``observed_at`` -- not from the ``match_id`` string, which with
    FACEIT's UUID-based ids would be arbitrary.
    """
    teams = build_teams(two_seasons(shared=4), min_common=MIN_COMMON)

    assert teams[0].team_key == "season-12"


def test_two_identifiers_below_the_threshold_stay_two_teams() -> None:
    """Two shared players are not the same team but a coincidence.

    This is the counterpart of joining: without a threshold any shared player
    would fuse two different teams into one.
    """
    teams = build_teams(two_seasons(shared=2), min_common=MIN_COMMON)

    assert len(teams) == 2


def test_joining_is_never_chained_through_a_middle_roster() -> None:
    """A--B and B--C do not make A and C the same team.

    Chaining would join two different teams through one lineup that sits
    between them -- the same reasoning as in
    ``domain.aggregate.lineups_of_same_team``.
    """
    pool = members("pool", 9, offset=0)
    observations = [
        TeamObservation(
            faction_id="A",
            match_id="1-a",
            observed_at=KICKOFF,
            name="A",
            roster=pool[0:5],
        ),
        TeamObservation(
            faction_id="B",
            match_id="1-b",
            observed_at=KICKOFF + timedelta(days=1),
            name="B",
            roster=pool[2:7],
        ),
        TeamObservation(
            faction_id="C",
            match_id="1-c",
            observed_at=KICKOFF + timedelta(days=2),
            name="C",
            roster=pool[4:9],
        ),
    ]

    teams = build_teams(observations, min_common=MIN_COMMON)

    # B joins A (3 shared). C shares only one with A, so it stays on its own
    # even though it would share three with B.
    keys = {team.team_key: team.faction_ids for team in teams}
    assert keys == {"A": ("A", "B"), "C": ("C",)}


def test_a_zero_threshold_would_make_the_division_one_team_and_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        build_teams(division_observations(), min_common=0)


def test_an_old_season_identifier_still_finds_the_team() -> None:
    """An old id is still a key, even though it is no longer the canonical
    one."""
    teams = build_teams(two_seasons(shared=4), min_common=MIN_COMMON)

    lookup = find_teams(teams, "season-13")

    assert lookup.is_unique
    assert lookup.team.team_key == "season-12"


# -- The transferring player -------------------------------------------------


def transfer_observations(
    *, second_moment: datetime | None, first_moment: datetime | None = KICKOFF
) -> list[TeamObservation]:
    """The player ``mover`` is observed first in team A and then in B."""
    a_players = members("aaa", 5, offset=0)
    b_players = members("bbb", 5, offset=100)
    mover = a_players[0]
    return [
        TeamObservation(
            faction_id="A",
            match_id="1-a",
            observed_at=first_moment,
            name="Alpha",
            roster=a_players,
        ),
        TeamObservation(
            faction_id="B",
            match_id="1-b",
            observed_at=second_moment,
            name="Beta",
            roster=b_players[:4] + (mover,),
        ),
    ]


def test_a_player_who_moved_leaves_the_old_roster() -> None:
    """Review: a union over all the matches left the mover in both.

    That would inflate the rosters and distort both the roster threshold
    (Story 3.3) and the joining of teams -- two different teams would start to
    look the same, because both would hold the same player.
    """
    teams = build_teams(
        transfer_observations(second_moment=KICKOFF + timedelta(days=30)),
        min_common=MIN_COMMON,
    )

    by_name = {team.display_name: team for team in teams}
    mover = members("aaa", 5, offset=0)[0].game_player_id

    assert mover not in by_name["Alpha"].player_ids
    assert mover in by_name["Beta"].player_ids


def test_the_old_team_still_remembers_the_player_who_left() -> None:
    """Dropping is not deleting: the observation survives in ``released``."""
    teams = build_teams(
        transfer_observations(second_moment=KICKOFF + timedelta(days=30)),
        min_common=MIN_COMMON,
    )

    by_name = {team.display_name: team for team in teams}
    released = [m.nickname for m in by_name["Alpha"].released]

    assert released == ["aaa1"]
    assert by_name["Beta"].released == ()


def test_an_equally_recent_observation_moves_nobody() -> None:
    """An equally late observation in two teams is a dispute, not a transfer.

    The dispute is not settled by drawing lots: the player stays in both
    rosters, and the fact that a dispute exists can be read.
    """
    teams = build_teams(
        transfer_observations(second_moment=KICKOFF), min_common=MIN_COMMON
    )

    mover = members("aaa", 5, offset=0)[0].game_player_id
    for team in teams:
        assert mover in team.player_ids
        assert team.shared_players == (mover,)
        assert team.released == ()


def test_an_unknown_observation_time_moves_nobody() -> None:
    """Without a time one cannot claim to know which observation was later."""
    teams = build_teams(
        transfer_observations(second_moment=None), min_common=MIN_COMMON
    )

    mover = members("aaa", 5, offset=0)[0].game_player_id
    assert all(mover in team.player_ids for team in teams)
    assert all(team.shared_players == (mover,) for team in teams)


def test_a_player_seen_in_only_one_team_is_never_released(
    division: tuple[Team, ...],
) -> None:
    """Three matches out of eleven is not a transfer but a little playing
    time."""
    assert all(team.released == () for team in division)
    assert all(team.shared_players == () for team in division)


# -- The name lookup ---------------------------------------------------------


def test_an_exact_name_finds_exactly_one_team(division: tuple[Team, ...]) -> None:
    """The I/O matrix: the exact name ``Rcave Veterans``."""
    lookup = find_teams(division, "Rcave Veterans")

    assert lookup.is_unique
    assert lookup.team.name == "Rcave Veterans"
    assert lookup.matched_by == "name"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("rcave veterans", "Rcave Veterans"),
        ("RCAVE VETERANS", "Rcave Veterans"),
        ("POTKUKELKKAPEEK", "PotkukelkkaPeek"),
        ("potkukelkkapeek", "PotkukelkkaPeek"),
        ("UNCS67", "uncs67"),
        ("cm esports", "cM Esports"),
        ("POPSICS", "popsiCS"),
        ("  Rcave Veterans  ", "Rcave Veterans"),
    ],
)
def test_case_does_not_matter(
    division: tuple[Team, ...], query: str, expected: str
) -> None:
    """The I/O matrix: the letter case differs.

    The case genuinely varies in the division's names (``uncs67``,
    ``cM Esports``, ``popsiCS``, ``JYSAEYTTAEJAET``), so this is not a
    convenience but a requirement.
    """
    lookup = find_teams(division, query)

    assert lookup.is_unique
    assert lookup.team.name == expected


def test_an_unambiguous_partial_name_is_enough(division: tuple[Team, ...]) -> None:
    """The I/O matrix: ``Rcave`` hits exactly one."""
    lookup = find_teams(division, "Rcave")

    assert lookup.is_unique
    assert lookup.team.name == "Rcave Veterans"
    assert lookup.matched_by == "prefix"


def test_the_rcave_roster_is_the_seven_measured_players(
    division: tuple[Team, ...],
) -> None:
    """The acceptance criterion: the query ``Rcave`` -> one team, a roster of
    7 players."""
    lookup = find_teams(division, "Rcave")

    assert [member.nickname for member in lookup.team.roster] == list(RCAVE)
    # The four measured ids are included as they stand: they are exactly what
    # joins the roster to the archive's demo.
    assert set(MEASURED_RCAVE_IDS.values()) <= lookup.team.player_ids


def test_an_ambiguous_prefix_lists_all_three_and_chooses_none(
    division: tuple[Team, ...],
) -> None:
    """The acceptance criterion: ``T`` hits three, and not one is chosen.

    This is a **measured case and not a theoretical one**: three of the
    division's 12 names begin with T.
    """
    lookup = find_teams(division, "T")

    assert lookup.is_ambiguous
    assert [team.name for team in lookup.teams] == [
        "Takakeno",
        "Tankkiluola vilttiketju",
        "TUUHEE",
    ]
    with pytest.raises(ValueError, match="has to be asked for"):
        _ = lookup.team


def test_an_unknown_name_finds_nothing(division: tuple[Team, ...]) -> None:
    """The I/O matrix: ``Astralis`` is not in the division."""
    lookup = find_teams(division, "Astralis")

    assert lookup.is_empty
    assert lookup.matched_by is None


def test_two_teams_with_the_same_name_are_both_listed() -> None:
    """The I/O matrix: the same name on two teams.

    Theoretical but not impossible. An exact name makes the choice no more
    silently than a partial one does.
    """
    teams = build_teams(
        [
            TeamObservation(
                faction_id="faction-a",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="Takakeno",
                roster=members("a", 5, offset=0),
            ),
            TeamObservation(
                faction_id="faction-b",
                match_id="1-m1",
                observed_at=KICKOFF,
                name="Takakeno",
                roster=members("b", 5, offset=50),
            ),
        ],
        min_common=MIN_COMMON,
    )

    lookup = find_teams(teams, "Takakeno")

    assert lookup.is_ambiguous
    assert {team.team_key for team in lookup.teams} == {"faction-a", "faction-b"}


def test_an_exact_name_wins_over_being_a_prefix_of_another() -> None:
    """An exact name is not left ambiguous as the start of another name.

    Without the ladder ``uncs67`` would also hit the team ``uncs67 Academy``,
    and searching by the team's own name would be impossible.
    """
    teams = build_teams(
        [
            TeamObservation(
                faction_id="a",
                match_id="1-m0",
                observed_at=KICKOFF,
                name="uncs67",
                roster=members("a", 5, 0),
            ),
            TeamObservation(
                faction_id="b",
                match_id="1-m1",
                observed_at=KICKOFF,
                name="uncs67 Academy",
                roster=members("b", 5, 50),
            ),
        ],
        min_common=MIN_COMMON,
    )

    lookup = find_teams(teams, "uncs67")

    assert lookup.is_unique
    assert lookup.team.team_key == "a"


def test_a_team_can_be_found_by_its_key(division: tuple[Team, ...]) -> None:
    """The id is still valid for a lookup -- the index adds the name, it does
    not remove the id."""
    lookup = find_teams(division, "faction-09")

    assert lookup.is_unique
    assert lookup.team.team_key == "faction-09"


def test_a_name_can_be_found_from_the_middle(division: tuple[Team, ...]) -> None:
    """``vilttiketju`` is the end of the name, and even that is enough when it
    is unambiguous."""
    lookup = find_teams(division, "vilttiketju")

    assert lookup.is_unique
    assert lookup.team.name == "Tankkiluola vilttiketju"
    assert lookup.matched_by == "contains"


def test_an_empty_query_finds_nothing_rather_than_everything(
    division: tuple[Team, ...],
) -> None:
    """An empty query would hit them all, and "all" is not a lookup result."""
    assert find_teams(division, "   ").is_empty


# -- Names and nicknames are observations ------------------------------------


def test_the_most_often_observed_name_wins_and_the_others_are_kept() -> None:
    """A name change is not hidden: the alternatives can be read."""
    roster = members("x", 5, offset=0)
    observations = [
        TeamObservation(
            faction_id="a",
            match_id="1-m0",
            observed_at=KICKOFF,
            name="Old name",
            roster=roster,
        ),
        TeamObservation(
            faction_id="a",
            match_id="1-m1",
            observed_at=KICKOFF + timedelta(days=1),
            name="New name",
            roster=roster,
        ),
        TeamObservation(
            faction_id="a",
            match_id="1-m2",
            observed_at=KICKOFF + timedelta(days=2),
            name="New name",
            roster=roster,
        ),
    ]

    (team,) = build_teams(observations, min_common=MIN_COMMON)

    assert team.name == "New name"
    assert team.alternative_names == ("Old name",)


def test_a_changed_nickname_is_kept_the_same_way_a_changed_team_name_is() -> None:
    """Review: the team had ``alternative_names``, the player had nothing.

    A nickname changing is exactly the same kind of observation as a team's
    name changing, and neither is hidden.
    """
    old = RosterMember(game_player_id=steam_id(1), nickname="oldnick", player_id="u1")
    new = RosterMember(game_player_id=steam_id(1), nickname="newnick", player_id="u1")
    rest = members("x", 5, offset=100)[1:]
    observations = [
        TeamObservation(
            faction_id="a",
            match_id=f"1-m{index}",
            observed_at=KICKOFF + timedelta(days=index),
            name="Team",
            roster=(member,) + rest,
        )
        for index, member in enumerate((old, new, new))
    ]

    (team,) = build_teams(observations, min_common=MIN_COMMON)
    player = next(m for m in team.roster if m.game_player_id == steam_id(1))

    assert player.nickname == "newnick"
    assert player.alternative_nicknames == ("oldnick",)


def test_a_team_without_any_observed_name_falls_back_to_its_key() -> None:
    """A missing name is ``None``, not a substitute -- but there is always
    something to display."""
    (team,) = build_teams(
        [
            TeamObservation(
                faction_id="faction-x",
                match_id="1-m0",
                observed_at=KICKOFF,
                roster=members("x", 5, 0),
            )
        ],
        min_common=MIN_COMMON,
    )

    assert team.name is None
    assert team.display_name == "faction-x"


# -- The bridge to the archive -----------------------------------------------


def test_lineup_keys_are_attached_to_every_team_above_the_threshold(
    division: tuple[Team, ...],
) -> None:
    """The archive's lineup hash is attached to a team on the basis of the
    roster.

    The hash is not an identity -- one substitution changes it -- but it is
    the only bridge to the ``aggregates/<team_key>`` directories, which this
    story does not rename.
    """
    rcave = find_teams(division, "Rcave").team
    lineup = set(sorted(rcave.player_ids)[:5])

    updated, contested = assign_lineup_keys(division, {"ff03fb54599d3311": lineup}, 3)

    by_key = {team.team_key: team for team in updated}
    assert by_key[rcave.team_key].lineup_keys == ("ff03fb54599d3311",)
    assert contested == ()
    assert all(
        not team.lineup_keys for key, team in by_key.items() if key != rcave.team_key
    )


def test_a_lineup_below_the_threshold_is_attached_to_nobody(
    division: tuple[Team, ...],
) -> None:
    """Two shared players are not a team but a coincidence."""
    rcave = find_teams(division, "Rcave").team
    lineup = set(sorted(rcave.player_ids)[:2])

    updated, contested = assign_lineup_keys(division, {"some-lineup": lineup}, 3)

    assert all(not team.lineup_keys for team in updated)
    assert contested == ()


def test_a_lineup_claimed_by_two_teams_is_given_to_both_and_flagged() -> None:
    """Review: "the most shared wins" meant a different thing from what it
    means in ``aggregate``.

    The same settings value (``team_identity_min_common``) stood for two
    different things in two places: ``aggregate`` attaches **all** that cross
    the threshold, this attached only the best. Now the rule is the same, and
    the dispute is flagged -- so that a later stage does not count the hash
    twice without knowing that it does.
    """
    shared = members("shared", 4, offset=0)
    a = build_teams(
        [
            TeamObservation(
                faction_id="A",
                match_id="1-a",
                observed_at=KICKOFF,
                name="A",
                roster=shared + members("aaa", 2, offset=100)[:1],
            ),
            TeamObservation(
                faction_id="B",
                match_id="1-b",
                observed_at=KICKOFF,
                name="B",
                roster=shared + members("bbb", 2, offset=200)[:1],
            ),
        ],
        # A threshold of 5, so that the joining does not make these the same
        # team -- what is measured here is the lineup attachment and not the
        # identity.
        min_common=5,
    )
    lineup = {member.game_player_id for member in shared}

    updated, contested = assign_lineup_keys(a, {"shared-lineup": lineup}, 3)

    assert len(a) == 2
    assert all(team.lineup_keys == ("shared-lineup",) for team in updated)
    assert contested == ("shared-lineup",)


def test_assigning_lineups_keeps_the_teams_otherwise_untouched(
    division: tuple[Team, ...],
) -> None:
    updated, contested = assign_lineup_keys(division, {}, 3)

    assert [t.team_key for t in updated] == [t.team_key for t in division]
    assert all(not team.lineup_keys for team in updated)
    assert contested == ()
