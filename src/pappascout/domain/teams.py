"""Team identity: name lookup and the standing roster (Story 3.2).

The module is **pure**: it knows nothing of HTTP, of files, or of FACEIT's
vocabulary. In comes :class:`TeamObservation` -- one observation of a team in
one match -- and out comes :class:`Team`. ``stages.discover`` does the
translation from the source's vocabulary, so this module's rules can be tested
with hand-built observations, without the network.

Four rules this module keeps
----------------------------

**Identity is the roster; an id is only an id.** The source's own team id
(``faction_id`` at FACEIT) is a *key*, not an identity: a new season or a
re-registration gives the same group of people a new id, and a recycled id
would give two different groups the same one. :func:`build_teams` therefore
joins two ids into the same team when their rosters share at least
``min_common`` players -- the same threshold
(``[thresholds].team_identity_min_common``) and the same way of comparing as
``domain.aggregate.lineups_of_same_team``. **The canonical ``team_key`` is the
id of the earliest observation**, so it does not change when a new season
brings a new id.

*This rule cannot be verified against the current live data*: the settings
hold one championship, and the measured result was exactly one ``faction_id``
per team. The rule is therefore verified by unit tests and is waiting for a
second season -- which is said here out loud, so that the reader does not take
it for measured.

**The roster is a union, not the latest match.** The roster is gathered from
*all* of the team's matches, and it holds both the starters and the
substitutes. The latest match would tell only about that evening, and the
``roster`` alone would underestimate the team systematically: measured
2026-09-04, ``Lindberq_`` is in a demo in the archive but not once in Rcave
Veterans' ``roster``.

**The union is not eternal, though.** A player transferring mid-season would,
under a plain union, stay in both teams for ever, and that would inflate the
rosters and distort the roster threshold (Story 3.3). The rule is therefore:
**a player belongs to the team that observed them last**; the earlier teams
keep them in :attr:`Team.released`, so that the observation is not lost. If
two teams observed them **equally late** -- or if the time of the observations
is not known -- they are moved out of neither, and are in both teams'
:attr:`Team.shared_players`. The dispute is not settled by drawing lots.

**Ambiguity is a result, not an exception.** :func:`find_teams` always returns
a :class:`TeamLookup`, in which there may be zero, one or many hits. A silent
"take the first" would be exactly the mistake the rule is written against: the
division's initial letter ``T`` hits three teams (``TUUHEE``, ``Takakeno``,
``Tankkiluola vilttiketju``), and none of them is "probably the right one".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from datetime import datetime

__all__ = [
    "STEAM_ID64_LENGTH",
    "STEAM_ID64_BASE",
    "STEAM_ID64_MAX",
    "is_steam_id64",
    "RosterMember",
    "TeamObservation",
    "Team",
    "TeamLookup",
    "build_teams",
    "find_teams",
    "assign_lineup_keys",
]

#: The length of a SteamID64 in characters. Every CS2 account is this long.
STEAM_ID64_LENGTH = 17

#: The smallest possible SteamID64 (``STEAM_0:0:0``, universe 1, type 1).
#:
#: The check is a range rather than mere digitness, because
#: ``game_player_id`` is a **string given by the source** and not a value this
#: program wrote. A plain "17 digits" would accept any 17-digit number, and a
#: wrong id would later look like an empty intersection with the demos -- not
#: like an error.
STEAM_ID64_BASE = 76561197960265728

#: The largest possible SteamID64 of an individual account:
#: :data:`STEAM_ID64_BASE` plus the largest value of the 32-bit account number
#: (``0xFFFFFFFF``).
#:
#: **The upper bound is as necessary as the lower one.** Without it, for
#: example ``"99999999999999999"`` would pass as an id: it is 17 digits and
#: larger than the lower bound, but it is not the id of any account that
#: exists.
STEAM_ID64_MAX = STEAM_ID64_BASE + 0xFFFFFFFF


def is_steam_id64(value: object) -> bool:
    """Is the value an individual account id in SteamID64 form?

    >>> is_steam_id64("76561197977479426")
    True
    >>> is_steam_id64("f56dd02a-6107-48e2-abfb-75e7ec7ebcb2")
    False
    >>> is_steam_id64("12345678901234567")
    False
    >>> is_steam_id64("99999999999999999")
    False
    """
    if not isinstance(value, str) or len(value) != STEAM_ID64_LENGTH:
        return False
    if not value.isdigit():
        return False
    return STEAM_ID64_BASE <= int(value) <= STEAM_ID64_MAX


@dataclass(frozen=True)
class RosterMember:
    """One player in a team's roster.

    Attributes:
        game_player_id: **The SteamID64, and the only key.** It is what joins
            the player to the demo's lineup table.
        nickname: The most often observed nickname, or ``None``. The name
            shown to a human; never a key, because it can change.
        player_id: The source's own player id (a UUID at FACEIT), or ``None``.
            Kept for traceability -- it is what fetches the player's details
            from the API, but it does not connect to the demos.
        alternative_nicknames: The other observed nicknames. A nickname
            changing is an observation in the same way a team name changing
            is, and neither is therefore hidden.
    """

    game_player_id: str
    nickname: str | None = None
    player_id: str | None = None
    alternative_nicknames: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_steam_id64(self.game_player_id):
            raise ValueError(
                f"The roster id {self.game_player_id!r} is not in SteamID64 "
                "form, so it cannot be joined to the demos."
            )

    @property
    def display_name(self) -> str:
        """The nickname, or the id if no nickname was observed."""
        return self.nickname if self.nickname else self.game_player_id


@dataclass(frozen=True)
class TeamObservation:
    """One observation of a team in one match.

    This is the module's **input** and at the same time the boundary behind
    which the source's vocabulary stays. ``stages.discover`` turns matches
    into these; a test builds them by hand.

    Attributes:
        faction_id: The team's id in the source. **A key, not an identity**:
            :func:`build_teams` decides which ids are the same team.
        match_id: The match the observation comes from. The same team appears
            in many.
        observed_at: The moment of the observation (the match's schedule or
            start), or ``None`` if the time is not known. **This is what makes
            the words "first" and "latest" true.** Without it the order would
            be the string order of ``match_id``, and FACEIT's ids are
            UUID-based -- that is, the order would be arbitrary and "the team
            observed last" would mean "the last one alphabetically".
        name: The team's name as an observation, or ``None``.
        played: Whether the match has been played. Affects only the number
            "matches played" -- **not the roster**: the number of matches
            played must not affect whether the team is known. Measured
            2026-09-04, ``PotkukelkkaPeek`` has one played match out of eleven
            and still a full eight-player standing roster.
        roster: The starters in the source's order.
        substitutes: The substitutes in the source's order.
    """

    faction_id: str
    match_id: str
    observed_at: datetime | None = None
    name: str | None = None
    played: bool = False
    roster: tuple[RosterMember, ...] = ()
    substitutes: tuple[RosterMember, ...] = ()

    @property
    def everyone(self) -> tuple[RosterMember, ...]:
        """The starters and the substitutes as one list."""
        return self.roster + self.substitutes


@dataclass(frozen=True)
class Team:
    """One team with its standing roster.

    Attributes:
        team_key: **The canonical id**: the ``faction_id`` of the earliest
            observation. It does not change when the team gets a new id in a
            new season.
        faction_ids: Every id from the source that was recognised as this
            team, earliest first. Usually one.
        name: The most commonly observed name, or ``None`` if no name was
            observed.
        roster: The standing roster: the starters and the substitutes as a
            union over all the matches, **except** those since observed in
            another team. The order is by nickname, the nameless last.
        released: The players observed in this team but later in another.
            They are not in the roster, but neither have they vanished.
        shared_players: The SteamID64s that another team observed **equally
            late**. These are still in the roster, because the dispute is not
            settled by drawing lots -- but the fact that a dispute exists can
            be read.
        match_ids: Every match the team appeared in, in chronological order.
        played_match_ids: The played ones among them.
        lineup_keys: The lineup hashes recognised from the archive, if any
            have been attached (:func:`assign_lineup_keys`). **Not an identity
            but a bridge**: the hash changes on a single substitution,
            ``team_key`` does not.
        alternative_names: The other observed names besides ``name``. A name
            change is an observation, and it is not hidden.
    """

    team_key: str
    faction_ids: tuple[str, ...] = ()
    name: str | None = None
    roster: tuple[RosterMember, ...] = ()
    released: tuple[RosterMember, ...] = ()
    shared_players: tuple[str, ...] = ()
    match_ids: tuple[str, ...] = ()
    played_match_ids: tuple[str, ...] = ()
    lineup_keys: tuple[str, ...] = ()
    alternative_names: tuple[str, ...] = ()

    @property
    def display_name(self) -> str:
        """The name, or the id if no name was observed."""
        return self.name if self.name else self.team_key

    @property
    def player_ids(self) -> frozenset[str]:
        """The standing roster as a set of SteamID64s -- what joins to the
        demos."""
        return frozenset(member.game_player_id for member in self.roster)

    @property
    def matches_played(self) -> int:
        return len(self.played_match_ids)


@dataclass(frozen=True)
class TeamLookup:
    """The result of a name lookup. **Ambiguity lives here, not in an
    exception.**

    Attributes:
        query: The query exactly as the user wrote it.
        teams: The hits. Zero, one or many -- all three are valid results, and
            the caller decides what follows from them.
        matched_by: How the hits were found (``"name"``, ``"team_key"``,
            ``"prefix"`` or ``"contains"``), or ``None`` if there are no hits.
            Included so that "why exactly these" can be read rather than
            guessed.
    """

    query: str
    teams: tuple[Team, ...] = ()
    matched_by: str | None = None

    @property
    def is_unique(self) -> bool:
        return len(self.teams) == 1

    @property
    def is_ambiguous(self) -> bool:
        return len(self.teams) > 1

    @property
    def is_empty(self) -> bool:
        return not self.teams

    @property
    def team(self) -> Team:
        """The only hit.

        Raises:
            ValueError: If there are no hits or there are many. The caller has
                to ask for the choice before this; a silent choice is
                forbidden.
        """
        if not self.is_unique:
            raise ValueError(
                f"The query {self.query!r} did not produce one single team "
                f"but {len(self.teams)}. The choice has to be asked for."
            )
        return self.teams[0]


# -- Assembling the teams ----------------------------------------------------


@dataclass
class _Faction:
    """The accumulation for one source id, before identity is resolved."""

    faction_id: str
    members: dict[str, RosterMember]
    nicknames: dict[str, dict[str, int]]
    names: dict[str, int]
    match_ids: list[str]
    played_match_ids: list[str]
    #: The player's latest observation moment under this id. ``None`` means
    #: "the time is not known", and that is a different thing from an early
    #: moment.
    last_seen: dict[str, datetime | None]


def build_teams(
    observations: Iterable[TeamObservation], *, min_common: int
) -> tuple[Team, ...]:
    """Assemble the teams with their standing rosters from the observations.

    Three phases, and each carries out one of the module's rules:

    1. **Accumulation per id.** The roster is the union of the starters and
       the substitutes, keyed by ``game_player_id``.
    2. **Identity from the roster.** Two ids are the same team when their
       rosters share at least ``min_common`` players. The comparison is made
       against the **anchor**, not as a chain: chaining would join two
       different teams through one lineup that sits between them. The same
       reasoning as in ``domain.aggregate.lineups_of_same_team``.
    3. **Transferred players out of the roster.** A player belongs to the team
       that observed them last; in the others they are in
       :attr:`Team.released`. An equally late observation in two teams moves
       nobody -- it is recorded in :attr:`Team.shared_players`.

    The name becomes the **most often observed** one; the same rule applies to
    nicknames. A tie is settled alphabetically, so that the result does not
    depend on the order the matches happened to arrive in.

    Args:
        observations: The observations in any order. The function sorts them
            itself by ``observed_at``, so the result does not depend on the
            order of the input.
        min_common: The minimum number of shared players at which two source
            ids are the same team
            (``[thresholds].team_identity_min_common``).
            **A keyword parameter**: a bare integer after the list of
            observations would be interchangeable with any other number
            without anything remarking on it.

    Returns:
        The teams sorted by name (the nameless last).

    Raises:
        ValueError: If ``min_common`` is not positive. Zero would join every
            id to every other, that is, the whole division would be one team.
    """
    if min_common < 1:
        raise ValueError(
            f"The threshold for joining teams has to be at least 1, it was "
            f"{min_common}. Zero would make the whole division one team."
        )

    ordered = sorted(observations, key=_observation_order)
    factions = _collect(ordered)
    clusters = _cluster(factions, min_common)
    return tuple(sorted(_teams(clusters), key=_team_order))


def _observation_order(observation: TeamObservation) -> tuple[int, float, str, str]:
    """Chronological order; timeless observations last, then ``match_id``.

    A timeless observation is not "the oldest" but "not known", so it must not
    decide which id is the canonical one.
    """
    moment = observation.observed_at
    if moment is None:
        return (1, 0.0, observation.match_id, observation.faction_id)
    return (0, moment.timestamp(), observation.match_id, observation.faction_id)


def _collect(ordered: Sequence[TeamObservation]) -> list[_Faction]:
    """Gather the observations per source id, preserving chronological order."""
    factions: dict[str, _Faction] = {}
    for observation in ordered:
        faction = factions.get(observation.faction_id)
        if faction is None:
            faction = _Faction(
                faction_id=observation.faction_id,
                members={},
                nicknames={},
                names={},
                match_ids=[],
                played_match_ids=[],
                last_seen={},
            )
            factions[observation.faction_id] = faction

        for member in observation.everyone:
            faction.members.setdefault(member.game_player_id, member)
            if member.nickname:
                counts = faction.nicknames.setdefault(member.game_player_id, {})
                counts[member.nickname] = counts.get(member.nickname, 0) + 1
            faction.last_seen[member.game_player_id] = _later(
                faction.last_seen.get(member.game_player_id),
                observation.observed_at,
                seen=member.game_player_id in faction.last_seen,
            )

        if observation.name:
            faction.names[observation.name] = faction.names.get(observation.name, 0) + 1
        if observation.match_id not in faction.match_ids:
            faction.match_ids.append(observation.match_id)
        if observation.played and observation.match_id not in faction.played_match_ids:
            faction.played_match_ids.append(observation.match_id)
    return list(factions.values())


def _later(
    known: datetime | None, candidate: datetime | None, *, seen: bool
) -> datetime | None:
    """The later of two moments; **the unknown beats the known**.

    An unknown moment is not an early moment: if a player has been observed
    once without a time, we cannot claim to know when they were last seen.
    Keeping it ``None`` leaves them disputed and does not move them into the
    wrong team.
    """
    if not seen:
        return candidate
    if known is None or candidate is None:
        return None
    return max(known, candidate)


def _cluster(factions: Sequence[_Faction], min_common: int) -> list[list[_Faction]]:
    """Group the source ids into teams on the basis of the roster.

    An id is joined to the group whose **anchor** roster it shares the most
    players with, provided the shared count is at least ``min_common``. The
    anchor is the group's earliest id, and the comparison is always made
    against it -- not against the group's grown union. This way joining does
    not chain: A--B and B--C do not make A and C the same team unless A also
    shares the threshold's worth of players with C.
    """
    clusters: list[list[_Faction]] = []
    anchors: list[frozenset[str]] = []
    for faction in factions:
        players = frozenset(faction.members)
        best_index = -1
        best_common = min_common - 1
        for index, anchor in enumerate(anchors):
            common = len(players & anchor)
            if common > best_common:
                best_common = common
                best_index = index
        if best_index < 0:
            clusters.append([faction])
            anchors.append(players)
        else:
            clusters[best_index].append(faction)
    return clusters


def _teams(clusters: Sequence[Sequence[_Faction]]) -> list[Team]:
    """Turn the groups into teams and resolve the transferred players."""
    #: The player's latest observation moment in **every** team. Needed before
    #: any roster can be narrowed: a transfer is a matter between two teams,
    #: and it is not visible in either one alone.
    latest: dict[str, dict[int, datetime | None]] = {}
    for index, cluster in enumerate(clusters):
        for faction in cluster:
            for player, moment in faction.last_seen.items():
                per_team = latest.setdefault(player, {})
                per_team[index] = _later(
                    per_team.get(index), moment, seen=index in per_team
                )

    teams: list[Team] = []
    for index, cluster in enumerate(clusters):
        merged = _merge_factions(cluster)
        roster: list[RosterMember] = []
        released: list[RosterMember] = []
        shared: list[str] = []
        for player, member in merged.members.items():
            with_names = _merge_member(member, merged.nicknames.get(player, {}))
            verdict = _belongs(latest.get(player, {}), index)
            if verdict == "released":
                released.append(with_names)
                continue
            if verdict == "shared":
                shared.append(player)
            roster.append(with_names)

        teams.append(
            Team(
                team_key=cluster[0].faction_id,
                faction_ids=tuple(faction.faction_id for faction in cluster),
                name=_best_key(merged.names),
                roster=tuple(sorted(roster, key=_member_order)),
                released=tuple(sorted(released, key=_member_order)),
                shared_players=tuple(sorted(shared)),
                match_ids=tuple(merged.match_ids),
                played_match_ids=tuple(merged.played_match_ids),
                alternative_names=_other_keys(merged.names),
            )
        )
    return teams


def _merge_factions(cluster: Sequence[_Faction]) -> _Faction:
    """Merge the group's ids into one accumulation, preserving the order."""
    merged = _Faction(
        faction_id=cluster[0].faction_id,
        members={},
        nicknames={},
        names={},
        match_ids=[],
        played_match_ids=[],
        last_seen={},
    )
    for faction in cluster:
        for player, member in faction.members.items():
            merged.members.setdefault(player, member)
        for player, counts in faction.nicknames.items():
            target = merged.nicknames.setdefault(player, {})
            for nickname, count in counts.items():
                target[nickname] = target.get(nickname, 0) + count
        for name, count in faction.names.items():
            merged.names[name] = merged.names.get(name, 0) + count
        for match_id in faction.match_ids:
            if match_id not in merged.match_ids:
                merged.match_ids.append(match_id)
        for match_id in faction.played_match_ids:
            if match_id not in merged.played_match_ids:
                merged.played_match_ids.append(match_id)
    return merged


def _belongs(per_team: Mapping[int, datetime | None], index: int) -> str:
    """Does the player belong to this team: ``own``, ``shared`` or
    ``released``?

    * A player observed in one team is always ``own`` -- a player who has been
      involved in only three matches out of eleven has not transferred
      anywhere.
    * One observed in several belongs to the one that saw them **latest**.
    * An equally late -- or an unknown -- observation is ``shared``: a dispute
      that is not settled by drawing lots.
    """
    if len(per_team) <= 1:
        return "own"
    mine = per_team.get(index)
    others = [moment for team, moment in per_team.items() if team != index]
    if mine is None or any(moment is None for moment in others):
        return "shared"
    newest = max(moment for moment in others if moment is not None)
    if mine > newest:
        return "own"
    if mine == newest:
        return "shared"
    return "released"


def _merge_member(member: RosterMember, counts: Mapping[str, int]) -> RosterMember:
    """The player with their most often observed nickname, the others kept."""
    if not counts:
        return member
    return replace(
        member,
        nickname=_best_key(counts),
        alternative_nicknames=_other_keys(counts),
    )


def _best_key(counts: Mapping[str, int]) -> str | None:
    """The most often observed value; on a tie the first alphabetically.

    The alphabetical order is ``casefold``ed in the same way as the order of
    the teams (:func:`_team_order`), so that there are not two different
    alphabetical orders in one module.
    """
    if not counts:
        return None
    return min(counts, key=lambda value: (-counts[value], value.casefold(), value))


def _other_keys(counts: Mapping[str, int]) -> tuple[str, ...]:
    best = _best_key(counts)
    return tuple(sorted((value for value in counts if value != best), key=str.casefold))


def _member_order(member: RosterMember) -> tuple[int, str, str]:
    """By nickname, the nameless last.

    The comparison is the nickname **as it stands** and not lowercased: that
    is exactly what produces the rosters listed in the measurement document
    (section 3) in the order they are written there. This is a different thing
    from :func:`_best_key`'s tie-break, which is not a display order but a
    choice between two values observed equally often.
    """
    if member.nickname is None:
        return (1, "", member.game_player_id)
    return (0, member.nickname, member.game_player_id)


def _team_order(team: Team) -> tuple[int, str, str]:
    if team.name is None:
        return (1, "", team.team_key)
    return (0, team.name.casefold(), team.team_key)


# -- Name lookup -------------------------------------------------------------


def find_teams(teams: Sequence[Team], query: str) -> TeamLookup:
    """Find a team by name. **Case-insensitively, and without choosing
    silently.**

    The lookup goes from the most precise to the loosest, and **the first
    level that matches decides**:

    1. the name exactly (case-insensitively),
    2. the id exactly -- both the canonical ``team_key`` and any id in
       :attr:`Team.faction_ids`, so that an old season id still finds the same
       team,
    3. the start of the name,
    4. the middle of the name.

    The ladder is there so that an exact name is not left ambiguous merely
    because it happens to be the start of another name. If a level produces
    many hits, the result is ambiguous -- including when two teams have the
    same name. The choice is **not made here**.

    Args:
        teams: The teams, usually the result of :func:`build_teams`.
        query: The name, part of it, or the id, as written by the user.

    Returns:
        A :class:`TeamLookup` whose hits are in the same order as ``teams``.
    """
    needle = query.strip().casefold()
    if not needle:
        return TeamLookup(query=query)

    tiers: tuple[tuple[str, list[Team]], ...] = (
        ("name", [t for t in teams if t.name and t.name.casefold() == needle]),
        (
            "team_key",
            [
                t
                for t in teams
                if t.team_key.casefold() == needle
                or any(key.casefold() == needle for key in t.faction_ids)
            ],
        ),
        (
            "prefix",
            [t for t in teams if t.name and t.name.casefold().startswith(needle)],
        ),
        ("contains", [t for t in teams if t.name and needle in t.name.casefold()]),
    )
    for matched_by, found in tiers:
        if found:
            return TeamLookup(query=query, teams=tuple(found), matched_by=matched_by)
    return TeamLookup(query=query)


# -- The bridge to the archive -----------------------------------------------


def assign_lineup_keys(
    teams: Sequence[Team],
    lineups: Mapping[str, Set[str]],
    min_common: int,
) -> tuple[tuple[Team, ...], tuple[str, ...]]:
    """Attach the archive's lineup hashes to the teams on the basis of the
    roster.

    The bridge has to be built, because the archive's directories are named
    after the lineup hash and this story **does not rename them** (that
    decision is Story 3.4). Without the bridge ``index/teams.json`` and
    ``aggregates/<team_key>`` would be two worlds that know nothing of each
    other.

    The rule is **the same as** in ``domain.aggregate.lineups_of_same_team``:
    the hash is attached to **every** team whose standing roster it shares at
    least ``min_common`` players with. Earlier this was "the most shared
    wins", and that meant the same settings value
    (``team_identity_min_common``) stood for two different things in two
    places.

    Crossing the threshold in two teams is **genuine ambiguity** and it is not
    settled by drawing lots. The hashes that more than one team owns are
    returned separately, so that a later stage does not count them twice
    without knowing that it does.

    Args:
        teams: The teams to attach to.
        lineups: ``lineup_key`` -> the set of the players' SteamID64s, read
            from the archive's lineup tables.
        min_common: The minimum number of shared players
            (``[thresholds].team_identity_min_common``).

    Returns:
        ``(teams, contested)``. The teams are in the same order as they came
        in, with ``lineup_keys`` filled. The contested ones are the hashes
        that more than one team owns.
    """
    assigned: dict[str, list[str]] = {}
    contested: list[str] = []
    for lineup_key, players in sorted(lineups.items()):
        owners = [
            team.team_key
            for team in teams
            if len(team.player_ids & set(players)) >= min_common
        ]
        for team_key in owners:
            assigned.setdefault(team_key, []).append(lineup_key)
        if len(owners) > 1:
            contested.append(lineup_key)

    updated = tuple(
        replace(team, lineup_keys=tuple(sorted(assigned.get(team.team_key, ()))))
        for team in teams
    )
    return updated, tuple(contested)
