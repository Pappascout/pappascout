"""The aggregation's computation as pure functions (Story 2.3).

The module takes finished tables (``CLASSIFIED``, ``TICKS``, ``EVENTS``,
``DEATHS``) and returns a :class:`~pappascout.domain.report.Report` model.
**No files, no archive, no settings loading** -- every test of this module
builds its tables by hand, and not one of them needs a demo.

What is computed here
---------------------
Seven observations, each with its own sample, at the level map -> side ->
round type:

``positions``
    The player count per area at every sample point. The line *"3A and 2B"*
    is read from here.
``utility``
    The grenade's type, throw area, detonation area and time window. The
    lines *"a CT smoke onto the B site from T spawn"* and *"insta mid house
    smoke"* are read from here.
``utility_counts``
    How many of each grenade type were thrown on a round. *"2 smokes 2
    flashes"* is read from here. It is **not** derivable from the ``utility``
    rows: their ``n`` counts rounds and not grenades.
``players_armed``
    How many players were armed at the end of the buy time (Story 1.6's
    count: armour **and** an upgraded weapon). It is the half-buy's
    calibrated condition A.
``players_armored``
    How many players carried armour at the end of the buy time (Story 2.8).
    *"5 kevlars"* and *"no kevs"* are read from here -- **and not from the
    previous one**: on a pistol round the previous one is in practice 0,
    because $800 does not buy both kevlar and an upgraded weapon. The figure
    is possession and not a purchase, except on a pistol round (1 and 13),
    where nothing is inherited.
``first_contact``
    Which areas the team had a player in at the moment of first contact.
    *"took contact in the Apartments corridor"* is read from here.
``deaths``
    Where and when the team lost its first player, and from which areas it
    made kills. *"Cave dies so they play from the site"* and *"the enemy came
    through the secret yard"* are read from here.

Three rules that do not bend
----------------------------
**The player count is taken from the living only.** A dead player produces no
row for an area; he is in the round, but not on the map.

**One distribution counts kills and not rounds.** ``deaths``'s kill side is
the only place where ``m`` is not rounds: a round type can have more kills
than rounds. The difference is written into :class:`DeathReport`'s contract,
and the report formats that very row with a different unit.

**The sample is always the sample point's own.** ``m`` is the number of rounds
on which that sample point exists -- not the number of all the round type's
rounds. They differ: the 45-second sample is missing from a round that was
decided in 30 seconds. If the round type's total were taken as ``m``, the
decided round would show in every area as "0 players" -- that is, as a claim
that the area was empty. Such a figure looks like an observation but is not
one.

Why Polars is used for reading only
-----------------------------------
The tables are small (four demos = a few thousand rows), and every
computation in this module is a grouping whose Polars phrasing would hide
what the sample is made of. The rows are therefore unpacked into
dictionaries once and counted in plain Python, so that ``Σ n = m`` is
readable from the code and not only from the test.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from statistics import median
from typing import Any

import polars as pl

from pappascout.constants import (
    ANOMALY_RULE_SIDE,
    ANOMALY_RULES,
    ANOMALY_RULES_DEFERRED,
    CRUNCH,
    CT_ADVANCE,
    ROSTER_BUCKETS,
    ROSTER_CLASS_BUCKET,
    ROUND_TYPES,
    SAMPLE_BUCKETS,
    SAVING_ROUND_TYPES,
    SIDES,
    STACK,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
    RosterBucketName,
)
from pappascout.domain import sampling
from pappascout.domain.models import AggregateSettings, ThresholdSettings
from pappascout.domain.report import (
    MAP_NAME_SOURCES,
    SLUG_FALLBACK,
    Anomaly,
    AnomalyPoint,
    AnomalyRound,
    AnomalyScan,
    AreaDistribution,
    AreaOrientation,
    ArmedCount,
    ArmedPlayers,
    ArmoredCount,
    ArmoredPlayers,
    DeathReport,
    FirstContactArea,
    FirstDeathArea,
    GrenadeCount,
    KillArea,
    MapReport,
    MissingDemo,
    PlayersCount,
    Position,
    Report,
    RosterEntry,
    RosterSample,
    RoundTypeReport,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
    slugify,
    team_slug,
)
from pappascout.domain.schemas import ARMORED_COLUMN, CLASSIFIED
from pappascout.errors import AggregateError, SchemaError

__all__ = [
    "LEAGUE_BUCKETS",
    "ROSTER_SAMPLE_BUCKETS",
    "RoundKey",
    "bucket_labels",
    "seconds_bucket",
    "map_name_for",
    "observed_map_name",
    "weakest_map_source",
    "MAP_NAME_SOURCE_RANK",
    "team_slug",
    "slugify",
    "SLUG_FALLBACK",
    "lineups_of_same_team",
    "TeamIdentity",
    "team_identity",
    "roster_entries",
    "demo_buckets",
    "sample_for",
    "roster_class_values",
    "MISSING_ROSTER_CLASS_LABEL",
    "roster_demo_buckets",
    "roster_sample_for",
    "players_distribution",
    "area_distributions",
    "positions_for",
    "utility_uses",
    "utility_counts_for",
    "unpaired_detonations",
    "armed_players_for",
    "SideRoundKey",
    "armored_by_round",
    "armored_players_for",
    "first_contact_areas",
    "deaths_for",
    "anomalies_for",
    "check_rounds_are_unique",
    "classify_thresholds",
    "CLASSIFY_THRESHOLD_KEYS",
    "build_report",
]

#: The sample's three buckets. ``unknown`` is not an error state:
#: ``is_league`` comes from the ``select`` stage's selection file (Story 3.8
#: wired it into ``classify``), and it stays empty for every demo ``select``
#: gave no row -- a demo imported by hand, a lineup with no owner, or a table
#: classified before ``select``.
#:
#: The same list as :data:`pappascout.constants.SAMPLE_BUCKETS`, because the
#: buckets' Finnish names (``SAMPLE_BUCKET_FI``) live there and two diverging
#: lists would drop the third bucket from the output in silence.
LEAGUE_BUCKETS: tuple[str, ...] = SAMPLE_BUCKETS

#: The three buckets of the roster breakdown (Story 3.9). Same list as
#: :data:`pappascout.constants.ROSTER_BUCKETS`, aliased here for the same
#: reason ``LEAGUE_BUCKETS`` is: the printed names live beside it, and two
#: diverging lists would drop a bucket from the report in silence.
ROSTER_SAMPLE_BUCKETS: tuple[str, ...] = ROSTER_BUCKETS

#: A round's key across the whole archive. ``round_no`` on its own would mix
#: up the rounds of different maps.
RoundKey = tuple[str, int]

#: A round row's key **including the side**. The ``ROUNDS`` table has two rows
#: per round, one for each team, so :data:`RoundKey` on its own would hit both
#: -- and the opponent's armour would look like ours. A classified row carries
#: its own side, so the join is exact.
SideRoundKey = tuple[str, int, str]

#: The line break in error messages. A constant of its own, because these
#: files are often edited with scripts in which a backslash does not survive.
NEWLINE = "\n"

_NON_WORD = re.compile(r"[^a-z0-9]+")

#: Those ``CLASSIFIED.inputs`` fields that are thresholds and not
#: observations. Only these reach the report's ``classify_thresholds`` field;
#: the rest are per-round measurements (money, equipment) that have no single
#: value.
CLASSIFY_THRESHOLD_KEYS: tuple[str, ...] = (
    "full_equip_min",
    "force_buy_min",
    "armed_players_min",
    "normal_buy_money_min",
    "normal_buy_players_min",
    "anomaly_equip_max_after_win",
)


# -- Small pure helpers ----------------------------------------------------------


def bucket_labels(edges: Sequence[float]) -> list[str]:
    """The time windows' names, from their edges.

    >>> bucket_labels([5.0, 10.0, 20.0])
    ['0-5', '5-10', '10-20', '20+']
    >>> bucket_labels([])
    ['kaikki']

    An empty edge list means one bucket: removing the time window is a valid
    choice and it does not have to be a code change.
    """
    if not edges:
        return [UTILITY_BUCKET_ALL]
    # The check is made from the EDGES' names and not from the finished
    # buckets: two edges close together produce the bucket "5-5", which is a
    # different string from its neighbour but does not mean anything. Only a
    # third edge would produce two buckets with exactly the same name, and
    # until then the fault would be visible only as a meaningless name.
    names = [_seconds(edge) for edge in edges]
    if len(names) != len(set(names)):
        raise AggregateError(
            "Two time window edges look the same in the bucket name "
            f"({', '.join(names)}). The name is formatted to its shortest "
            "representation, so two edges close together would be "
            "indistinguishable in the report.\n"
            "Fix the setting [aggregate].utility_seconds_buckets."
        )
    labels = [f"0-{_seconds(edges[0])}"]
    labels += [
        f"{_seconds(low)}-{_seconds(high)}"
        for low, high in zip(edges, edges[1:], strict=False)
    ]
    labels.append(f"{_seconds(edges[-1])}+")
    return labels


def _seconds(value: float) -> str:
    """A second count as a name: ``5.0 -> '5'``, ``7.5 -> '7.5'``."""
    return f"{value:g}"


def seconds_bucket(t_s: float | None, edges: Sequence[float]) -> str:
    """Put the moment of the throw into a time window.

    The edge belongs to the **upper** bucket: with an edge at 5 s, a throw at
    5.0 s is in the bucket ``5-10``. The rule is arbitrary but it is one rule,
    and neither bucket may be read in both directions.

    An invalid moment gets a bucket of its own instead of dropping out -- a
    missing time is a different thing from zero. There are three invalid
    kinds:

    * ``None`` -- the round had no anchor, so there is no time.
    * **negative** -- the grenade left before the end of freezetime, so the
      measurement is contradictory. Without the check it would land in the
      ``0-5`` bucket and look like an "insta".
    * **NaN or infinite** -- every comparison with NaN is false, so it would
      slide into the last bucket (``20+``) and look like a late throw.

    The check is **before** the empty-edge-list short circuit: otherwise an
    unknown moment would merge into the known ones the moment the time
    windows are turned off.
    """
    if t_s is None or not isfinite(t_s) or t_s < 0:
        return UTILITY_BUCKET_UNKNOWN
    if not edges:
        return UTILITY_BUCKET_ALL
    labels = bucket_labels(edges)
    for index, edge in enumerate(edges):
        if t_s < edge:
            return labels[index]
    return labels[-1]


#: The sources of a map's name in **weakening** order. A small number = a
#: stronger source. A comparable number, because a branch's source is the
#: weakest of its demos (see :func:`weakest_map_source`).
#:
#: **Derived, not written out.** :data:`~pappascout.domain.report.MAP_NAME_SOURCES`
#: is already in order of precedence, and as two lists they would diverge: a
#: new source would be accepted by the model and rejected here -- or the other
#: way round, it would quietly get a rank that does not match its strength.
MAP_NAME_SOURCE_RANK: dict[str, int] = {
    source: rank for rank, source in enumerate(MAP_NAME_SOURCES)
}


def weakest_map_source(sources: Iterable[str]) -> str:
    """A branch's source is the **weakest** of its demos, not the strongest.

    Two demos from the same map are one branch (``map_demo_ids`` lists them),
    and the source of their name can differ: one had the map in its header,
    the other did not.

    The source answers the reader's question "can I trust this name", and one
    inferred member is enough to answer "not entirely". Choosing the strongest
    would be overstating it: the branch would look wholly observed even though
    some of its rounds were attached to it on the strength of a filename. A
    wrong name on one demo brings wrong rounds into the whole branch, so the
    weakest link is the one that has to be reported.

    ``unknown`` cannot end up here beside another name: its name is the
    ``map_demo_id`` itself, so it does not collide with any real map's name.

    Args:
        sources: The sources of the branch's demos. Non-empty.

    Returns:
        The weakest source in :data:`MAP_NAME_SOURCE_RANK` order.

    Raises:
        AggregateError: If the list is empty or holds an unknown source.
            Neither can arise from this module's own grouping, so it would be
            a caller's mistake -- and a default returned in silence would lie
            to the reader about how reliable the name is.
    """
    known = list(sources)
    if not known:
        raise AggregateError(
            "The map branch's source list is empty.\n"
            "A branch is created only from demos, so every one of them has at "
            "least one source. An empty list means the grouping is broken."
        )
    unknown = sorted(set(known) - set(MAP_NAME_SOURCE_RANK))
    if unknown:
        raise AggregateError(
            f"Unknown map name source: {', '.join(unknown)}.\n"
            f"The allowed ones are {', '.join(MAP_NAME_SOURCE_RANK)}. A new "
            "source has to be added both to this list and to ``MapReport``'s "
            "contract, so that its strength is defined."
        )
    return max(known, key=lambda source: MAP_NAME_SOURCE_RANK[source])


def observed_map_name(
    map_names: Mapping[str, str | None], demo: str
) -> str | None:
    """The name observed from the demo's header; a missing **key** is an error.

    Two things must not be conflated: the value ``None`` is a legal
    observation ("there was no map in the header", so the inference stays in
    force), but a **missing key** means the demo was not in the name map
    ``aggregate`` read at all. That is a programming error -- and it is
    exactly what :func:`build_report`'s ``map_names`` was made mandatory
    without a default to prevent.

    ``Mapping.get`` would mix the two together and quietly hand the map over
    to inference: a FACEIT demo would get its branch from its id, and nothing
    would say that the observation existed but did not arrive.

    Raises:
        AggregateError: If ``demo`` is not in the ``map_names`` map.
    """
    if demo not in map_names:
        raise AggregateError(
            f"Demo {demo} is not among the map names.\n"
            "``aggregate`` reads the name from every included demo's "
            "``match.parquet`` table, so a missing key means the table was "
            "not read for this demo. A missing **name** is a different "
            "thing: it is ``None`` and entirely legal, and then the name is "
            "inferred from the id.\n"
            f"Run: uv run pappascout parse {demo}"
        )
    return map_names[demo]


def map_name_for(
    map_demo_id: str, map_pool: Iterable[str], observed: str | None = None
) -> tuple[str, str]:
    """The map's name: the observation first, inference only in its absence.

    ``observed`` is the name read from the demo's header (``MATCH.map_name``,
    Story 2.11). It **always wins** and is used as it stands: it is not
    compared against the map pool, because a map outside the pool -- a
    workshop version or ``de_train`` -- is a genuine observation and not an
    unknown map. Quietly correcting it to a pool name would turn the
    observation into an inference.

    Without an observation the name is inferred from the id. On a
    hand-imported demo it is in the filename (``Ancient_vs_kaljukostaja``), so
    it is read from there against the map pool. The id is split into words
    rather than searched as a substring: a substring match would take a team
    called *Inferno* for Inferno.

    Args:
        map_demo_id: The demo's id.
        map_pool: ``[league].map_pool``, the pool the inference is made
            against.
        observed: The name observed from the header, or ``None``. An empty
            string and a string of nothing but spaces are the same thing as
            ``None``: neither is a name, so the inference stays in force.
            Edge whitespace is trimmed, so that the same map does not split
            into two branches.

    Returns:
        ``(name, source)``. In order of precedence the source is
        ``"demo_header"`` (observed from the header), ``"map_demo_id"`` (the
        pool identified exactly one map from the id) or ``"unknown"``, in
        which case the name stays ``map_demo_id`` as it is. No guess is made:
        a FACEIT id (``1-a52ebff2-...``) holds no map name, and a demo without
        a map must not merge into another map's branch.
    """
    if observed is not None and observed.strip():
        # Trimmed, not raw. The docstring already promises that nothing but
        # spaces is the same thing as ``None``; if edge whitespace stayed in
        # the name, ``" de_ancient"`` would be a different branch from
        # ``"de_ancient"`` -- that is, the observation would break the map
        # apart instead of assembling it.
        #
        # Trimming **is not validation**: it does not compare the name against
        # the map pool and does not change its spelling. The adapter trims the
        # name as it reads it, so this is a second line of defence -- but the
        # function is public and has a contract of its own, so it does not
        # lean on the caller's tidiness.
        return observed.strip(), "demo_header"
    tokens = {t for t in _NON_WORD.split(map_demo_id.lower()) if t}
    hits = {
        name
        for name in map_pool
        if name.lower() in tokens or name.lower().removeprefix("de_") in tokens
    }
    if len(hits) == 1:
        return next(iter(hits)), "map_demo_id"
    return map_demo_id, "unknown"


def lineups_of_same_team(
    target: str,
    members: Mapping[str, Iterable[str]],
    min_common: int,
) -> list[str]:
    """The lineups that are the same team as ``target``.

    A lineup id is a digest of the players who played on the map, so **one
    substitution produces a new id**. MatureMayhem appears in four demos under
    two different ids, and without joining them the report would see three
    demos out of four and would not say it had lost one.

    The rule is ``[thresholds].team_identity_min_common`` (AD-6): lineups are
    the same team when they have at least ``min_common`` players in common.
    The comparison is **always against the target**, never chained: chaining
    would join two teams to each other through one shared lineup.

    Args:
        target: The lineup id being matched against.
        members: Id -> players.
        min_common: The minimum number of players in common.

    Returns:
        The ids sorted, ``target`` always among them.

    Raises:
        AggregateError: If ``target`` is not in the ``members`` map.
    """
    if target not in members:
        raise AggregateError(
            f"Lineup {target!r} is not among the lineups given, so the team "
            "identity cannot be resolved."
        )
    own = set(members[target])
    return sorted(
        key
        for key, players in members.items()
        if key == target or len(own & set(players)) >= min_common
    )


@dataclass(frozen=True)
class TeamIdentity:
    """The team's name and roster as they were observed from the demos.

    Attributes:
        display_name: The most often observed clan name, or ``None`` if none
            was observed. ``None`` is an honest result: the absence of a name
            is an observation, not a reason to invent a substitute from the id
            or the filename.
        alternatives: The other observed clan names in alphabetical order.
            **A contradiction does not vanish**: if the joined demos give the
            team different names, the most often observed one is chosen for
            display and the rest are listed, so the reader sees that the team
            appeared under two names.
        names: ``player_id -> name`` for those players whose name was
            observed. A missing key means the name was not obtained -- the
            roster row is written anyway, because the SteamID always exists.
    """

    display_name: str | None = None
    alternatives: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)


def team_identity(rows: Sequence[Mapping[str, Any]]) -> TeamIdentity:
    """Infer the team's name and the players' names from the lineup rows.

    Args:
        rows: The rows of the ``LINEUPS`` table, **filtered to this team's
            lineups**. Filtering is the caller's responsibility: the function
            does not know which lineups are the same team.

    The clan name vote is a **two-stage majority**, not "one vote per name
    observed in a demo". First the most common clan inside the demo is
    settled, then it gets its demo's **one** vote, and finally the vote is
    taken across the demos.

    The difference is decisive on a small sample. If a demo has five players,
    four of them carrying the clan ``A`` and one carrying ``B``, "a vote per
    observed name" would give both of them one -- on a one-demo sample that is
    a tie, and alphabetical order could raise into the heading a name that one
    single player carried. A majority inside the demo settles that correctly,
    and counted across the demos a five-player demo still does not weigh five
    times a one-player demo.

    A tie is resolved **alphabetically at both levels**, so that the same
    archive gives the same report from one run to the next. Without it the
    result would depend on the order in which the files happened to be read.

    Returns:
        :class:`TeamIdentity`.

    Raises:
        AggregateError: If some row's ``map_demo_id`` is empty. It is a cheap
            guard against an expensive fault: an empty id would merge all the
            demos into one vote, so a majority of four demos would shrink to
            one and nothing would say so.
    """
    clans_per_demo: dict[str, Counter[str]] = {}
    name_votes: dict[str, Counter[str]] = {}

    for row in rows:
        demo = _clean_name(row.get("map_demo_id"))
        if demo is None:
            raise AggregateError(
                "A row of the lineup table has no map_demo_id, so it cannot "
                "be matched to a demo.\n"
                "The team's name is voted on one demo at a time, and a row "
                "without an id would merge all the demos into one vote. Run "
                "the parse again."
            )
        clan = _clean_name(row.get("clan_name"))
        if clan is not None:
            clans_per_demo.setdefault(demo, Counter())[clan] += 1
        player_id = row.get("player_id")
        name = _clean_name(row.get("player_name"))
        if player_id is not None and name is not None:
            name_votes.setdefault(str(player_id), Counter())[name] += 1

    # Stage 1: the majority inside the demo. Stage 2: one vote per demo.
    clan_votes: Counter[str] = Counter(
        _by_votes(votes)[0] for votes in clans_per_demo.values() if votes
    )
    ordered = _by_votes(clan_votes)
    return TeamIdentity(
        display_name=ordered[0] if ordered else None,
        alternatives=sorted(ordered[1:]),
        names={
            player_id: _by_votes(votes)[0]
            for player_id, votes in name_votes.items()
            if votes
        },
    )


def roster_entries(
    player_ids: Iterable[str], names: Mapping[str, str]
) -> list[RosterEntry]:
    """The roster: every player with his id and his name.

    The set of players comes **from the ids and not from the names**: the row
    is written even when the name was not obtained, because the SteamID is the
    only traceable value and a player dropped in silence would shrink the
    roster without saying so.
    """
    return [
        RosterEntry(player_id=player_id, display_name=names.get(player_id))
        for player_id in sorted(player_ids)
    ]


def _clean_name(value: Any) -> str | None:
    """A name without surrounding whitespace, or ``None``.

    An empty string is not a name. The same rule as in the parse, repeated
    here because a table written with an older version may hold an empty
    string, and that must not be presented as a name.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _by_votes(votes: Counter[str]) -> list[str]:
    """The values by vote count, ties in alphabetical order."""
    return sorted(votes, key=lambda name: (-votes[name], name))


# -- Sampling --------------------------------------------------------------------


def demo_buckets(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Demo -> sample bucket ``league`` / ``other`` / ``unknown``.

    ``is_league`` is per-demo information (it says what kind of match it was),
    so every round of the same demo has to carry the same value.

    Raises:
        AggregateError: If one demo's rounds carry two different values. The
            demo would then belong to two buckets, and the sample total would
            stop being the sum of the buckets -- and that sum is the whole
            structure's check.
    """
    seen: defaultdict[str, set[bool | None]] = defaultdict(set)
    for row in rows:
        seen[str(row["map_demo_id"])].add(row["is_league"])
    buckets: dict[str, str] = {}
    for demo, values in seen.items():
        if len(values) > 1:
            raise AggregateError(
                f"Demo {demo}'s rounds carry two different is_league values "
                f"({sorted(str(v) for v in values)}), so the demo would "
                "belong to two sample buckets.\n"
                "is_league describes the match and not the round. Run the "
                "classification again: "
                "uv run pappascout classify <map_demo_id> --force"
            )
        value = next(iter(values))
        buckets[demo] = (
            "unknown" if value is None else ("league" if value else "other")
        )
    return buckets


def sample_for(
    rows: Sequence[Mapping[str, Any]], buckets: Mapping[str, str]
) -> Sample:
    """One level's sample: demos and rounds in three buckets."""
    demos: defaultdict[str, set[str]] = defaultdict(set)
    rounds: Counter[str] = Counter()
    for row in rows:
        demo = str(row["map_demo_id"])
        bucket = buckets[demo]
        demos[bucket].add(demo)
        rounds[bucket] += 1
    made = {
        name: SampleBucket(demos=len(demos[name]), rounds=rounds[name])
        for name in LEAGUE_BUCKETS
    }
    return Sample(
        demos=sum(b.demos for b in made.values()),
        rounds=sum(b.rounds for b in made.values()),
        **made,
    )


def roster_class_values() -> tuple[str, ...]:
    """The roster classes **the ``CLASSIFIED`` schema enum allows**.

    Read from the contract the value is finally written against, not from the
    parallel constant :data:`~pappascout.constants.ROSTER_CLASSES`. Two
    sources could drift, and then the guard below and its error message would
    speak of a different set than Polars does -- naming an allowed value as
    foreign, or waving through one that breaks the write three stages later.

    ``stages.classify.roster_classes`` reads the same enum for the same
    reason; it cannot be called from here, because ``domain`` does not import
    ``stages`` (see ``tests/test_layering.py``).
    """
    return tuple(str(value) for value in CLASSIFIED["roster_class"].categories)


#: The name of an empty ``roster_class`` in a user-facing message. Python's
#: ``None`` tells the reader nothing; the column is empty, and that is how it
#: is said.
#:
#: **The value is English.** Measured 2026-09-09: this constant has exactly
#: one production consumer, an ``AggregateError`` message, and none at all
#: under ``render/``. It is console vocabulary, not report vocabulary, so
#: AD-11 puts it in English -- the same case as
#: :data:`~pappascout.domain.selection.ROSTER_SOURCE_LABELS`, and the same
#: conclusion T9 reached for that one. It is a ``_LABEL`` and not a member of
#: :data:`~pappascout.constants.ROSTER_CLASSES`: the word stands for the
#: absence of a class, not for a class.
MISSING_ROSTER_CLASS_LABEL = "empty"


def _classified_value(row: Mapping[str, Any], column: str, demo: str) -> Any:
    """One ``CLASSIFIED`` column, or the stage that has to be run again.

    A plain ``row[column]`` would raise ``KeyError`` on a table written before
    the column existed, and a ``KeyError`` on the command line is an internal
    error rather than an instruction. **This is the archive's current state,
    not a hypothesis:** every classified table written before Story 3.8 lacks
    ``roster_class`` entirely.

    Raises:
        SchemaError: If the column is missing from the row.
    """
    try:
        return row[column]
    except KeyError:
        raise SchemaError(
            f"A classified row is missing the column {column!r}"
            + (f" (demo {demo})" if demo else "")
            + ", so the roster breakdown cannot be computed.\n"
            "The table was written before the column existed. Run the "
            "classification again: "
            "uv run pappascout classify <map_demo_id> --force"
        ) from None


def roster_demo_buckets(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, RosterBucketName]:
    """Demo -> roster bucket ``full`` / ``partial`` / ``unknown``.

    ``roster_class`` describes the **map**, not the round: ``select`` judges
    the roster threshold once per MapDemo and ``classify`` copies that one
    value onto every round. So all rounds of one demo must carry it, exactly
    like ``is_league`` in :func:`demo_buckets`.

    The class itself is never recomputed here. ``select`` is the only stage
    that judges the threshold (:mod:`pappascout.domain.selection`); this
    function only reads the value the table carries and maps it to a bucket
    name through :data:`~pappascout.constants.ROSTER_CLASS_BUCKET`.

    Raises:
        AggregateError: If one demo's rounds carry two different classes
            (a contradictory classification), or if some carry a class and
            others are empty (an **interrupted** classification -- a different
            fault with a different fix, so it gets its own message). Either
            way the demo would belong to two buckets, and the sample total
            would stop being the sum of the buckets.
        SchemaError: If a value is not in the ``CLASSIFIED`` enum, or if the
            column is missing altogether. Both are a table that does not meet
            the contract rather than a missing measurement, so they stop the
            run instead of being counted as unknown.
    """
    seen: defaultdict[str, set[str | None]] = defaultdict(set)
    for row in rows:
        demo = str(_classified_value(row, "map_demo_id", ""))
        value = _classified_value(row, "roster_class", demo)
        seen[demo].add(None if value is None else str(value))
    allowed = roster_class_values()
    buckets: dict[str, RosterBucketName] = {}
    for demo, values in seen.items():
        known = sorted(value for value in values if value is not None)
        if len(known) > 1:
            raise AggregateError(
                f"Demo {demo}'s rounds carry two different roster_class "
                f"values ({', '.join(known)}), so the demo would belong to "
                "two roster buckets.\n"
                "roster_class describes the map and not the round. Run the "
                "classification again: "
                "uv run pappascout classify <map_demo_id> --force"
            )
        if known and None in values:
            raise AggregateError(
                f"Some of demo {demo}'s rounds carry the roster_class value "
                f"{known[0]} and some are {MISSING_ROSTER_CLASS_LABEL}, so the "
                "demo would belong to two roster buckets.\n"
                "This is not a contradictory classification but an "
                "interrupted one: some of the rounds were classified before "
                "the selection file gave the map a class. Run the whole "
                "classification again: "
                "uv run pappascout classify <map_demo_id> --force"
            )
        if not known:
            buckets[demo] = "unknown"
            continue
        value = known[0]
        if value not in allowed:
            raise SchemaError(
                f"Demo {demo}'s roster_class value {value!r} is not among "
                f"the ones the classified table allows "
                f"({', '.join(allowed)}).\n"
                "The class is an enum value of the schema, so it cannot be "
                "invented during a run. Run select and the classification "
                "again."
            )
        buckets[demo] = ROSTER_CLASS_BUCKET[value]
    return buckets


def roster_sample_for(
    rows: Sequence[Mapping[str, Any]], buckets: Mapping[str, str]
) -> RosterSample:
    """The roster breakdown of one sample: demos and rounds in three buckets.

    Only the summary uses this (AD-10). Levels below it keep one sample each.

    Raises:
        AggregateError: If a row names a demo that ``buckets`` does not cover.
            That means the two were built from different rows, and it is the
            fault that would otherwise reach the reader as a roster total
            quietly smaller than the league one -- named here, at the demo,
            instead of as a sum that does not add up.
        SchemaError: If ``map_demo_id`` is missing from a row.
    """
    demos: defaultdict[str, set[str]] = defaultdict(set)
    rounds: Counter[str] = Counter()
    for row in rows:
        demo = str(_classified_value(row, "map_demo_id", ""))
        bucket = buckets.get(demo)
        if bucket is None:
            raise AggregateError(
                f"Demo {demo} has no roster bucket, so its rounds would drop "
                "out of the roster breakdown.\n"
                "The buckets were computed from different rows than the "
                "sample. Run the aggregation again."
            )
        demos[bucket].add(demo)
        rounds[bucket] += 1
    made = {
        name: SampleBucket(demos=len(demos[name]), rounds=rounds[name])
        for name in ROSTER_SAMPLE_BUCKETS
    }
    return RosterSample(
        demos=sum(b.demos for b in made.values()),
        rounds=sum(b.rounds for b in made.values()),
        **made,
    )


# -- Distributions ---------------------------------------------------------------


def players_distribution(counts: Iterable[int]) -> list[PlayersCount]:
    """The player counts' distribution as bars.

    The input is **one element per round**, zeros included: they are exactly
    what produces the ``players = 0`` bar, without which ``Σ n = m`` would not
    hold.
    """
    tally = Counter(int(c) for c in counts)
    return [
        PlayersCount(players=players, n=tally[players])
        for players in sorted(tally)
    ]


def area_distributions(
    rows_by_round: Mapping[RoundKey, Sequence[Mapping[str, Any]]],
) -> list[AreaDistribution]:
    """The areas' distributions at one sample point.

    The set of areas is the **union of every round's areas**, and every round
    produces an observation for every area -- the value 0 included. Without
    the union the distribution would speak only of the round on which the area
    happened to hold somebody, and "on three rounds out of four B was empty"
    would be a missing row rather than an observation.

    Args:
        rows_by_round: Round -> the sample point's rows. **The living only**;
            a dead player is not counted.
    """
    m = len(rows_by_round)
    areas: set[str | None] = set()
    per_round: dict[RoundKey, Counter[str | None]] = {}
    for key, rows in rows_by_round.items():
        tally: Counter[str | None] = Counter(
            (row["area"] if row["area"] is not None else None) for row in rows
        )
        per_round[key] = tally
        areas.update(tally)

    return [
        AreaDistribution(
            area=area,
            m=m,
            players_dist=players_distribution(
                per_round[key][area] for key in rows_by_round
            ),
        )
        for area in sorted(areas, key=_area_sort_key)
    ]


def _area_sort_key(area: str | None) -> tuple[int, str]:
    """The areas in alphabetical order, the unknown one last."""
    return (1, "") if area is None else (0, area)


def _sample_seconds(row: Mapping[str, Any]) -> float:
    """A sample point's nominal time, or an error if it has none.

    **One check for two readers.** Both :func:`positions_for` and the anomaly
    rules need this figure, and neither can carry on without it: a time sample
    point without a nominal second cannot be grouped (an empty value would
    merge two different sample points into one), and an anomaly compares it
    against a time bound. Written twice, one of them would get there first and
    the other would be dead code that can be deleted by accident -- and the
    ordering it would lean on is not enforced by anything.

    Raises:
        AggregateError: If ``sample_t_s`` is missing. The instruction is to
            run the parse again, because the value is created there.
    """
    value = row["sample_t_s"]
    if value is None:
        raise AggregateError(
            f"A sample point is missing sample_t_s (demo "
            f"{row['map_demo_id']!r}, round {row['round_no']!r}, "
            f"sample_kind={row['sample_kind']!r}). Run the parse again: "
            f"uv run pappascout parse {row['map_demo_id']} --force"
        )
    return float(value)


def _round_key(row: Mapping[str, Any]) -> RoundKey | None:
    """The row's round key, or ``None`` if the round has no number.

    The rows of the warmup, the knife round and a match restart come from the
    ``parse`` stage without a ``round_no``. They are not rounds, so they
    cannot belong to any round type's sample -- and they must not be turned
    into a number either.
    """
    if row["round_no"] is None:
        return None
    return (str(row["map_demo_id"]), int(row["round_no"]))


def positions_for(
    ticks: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
) -> list[Position]:
    """The sample points of one map/side/round type branch.

    Time sample points are grouped by ``sample_t_s``, first contact into
    **one** sample point: its moment differs on every round, so grouping it by
    ``sample_t_s`` would produce one sample point per round.
    """
    total_rounds = len(round_keys)
    groups: defaultdict[
        tuple[str, float | None], dict[RoundKey, list[Mapping[str, Any]]]
    ] = defaultdict(dict)

    keys = set(round_keys)
    contact_seconds: dict[RoundKey, float] = {}
    for row in ticks:
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        kind = str(row["sample_kind"])
        seconds = _sample_seconds(row)
        group = groups[(kind, seconds if kind == "time" else None)]
        # A round is in the sample point as soon as it has even one row --
        # including when every player is dead. Otherwise a round on which the
        # whole team had fallen would vanish from the sample and Σ n = m
        # would fail.
        group.setdefault(key, [])
        if kind == "first_contact":
            contact_seconds[key] = float(row["sample_t_s"])
        if bool(row["is_alive"]):
            group[key].append(row)

    positions: list[Position] = []
    for (kind, seconds), rows_by_round in groups.items():
        # The median is computed from the ROUNDS and not from the player
        # rows: the same moment repeats for every living player, so a
        # row-based median would weight the round that had more players
        # alive. Four players at 10 s and one at 20 s would give 10.0 even
        # though the rounds' median is 15.0.
        contact_times = (
            sorted(contact_seconds[key] for key in rows_by_round)
            if kind == "first_contact"
            else []
        )
        positions.append(
            Position(
                sample_kind=kind,
                seconds=seconds,
                seconds_median=(
                    round(median(contact_times), 3) if contact_times else None
                ),
                m=len(rows_by_round),
                rounds_missing=total_rounds - len(rows_by_round),
                areas=area_distributions(rows_by_round),
            )
        )
    # Time sample points in ascending order, first contact last: it is not a
    # clock time but an event.
    positions.sort(key=lambda p: (p.sample_kind == "first_contact", p.seconds or 0.0))
    return positions


def armed_players_for(
    rows: Sequence[Mapping[str, Any]],
) -> ArmedPlayers:
    """The armed players' distribution, round by round.

    The observation is ``inputs.players_armed`` as stored by the ``classify``
    stage, that is Story 1.6's count. ``null`` means an unreadable inventory,
    and it is kept apart from zero: zero armed is a saving round, unreadable
    is not an observation at all.
    """
    values = [_armed(row) for row in rows]
    known = [v for v in values if v is not None]
    tally = Counter(known)
    return ArmedPlayers(
        m=len(known),
        rounds_unknown=len(values) - len(known),
        counts=[
            ArmedCount(armed=armed, n=tally[armed]) for armed in sorted(tally)
        ],
    )


def _armed(row: Mapping[str, Any]) -> int | None:
    """``inputs.players_armed`` from one row, ``None`` if it is missing."""
    inputs = row.get("inputs")
    if not isinstance(inputs, Mapping):
        return None
    value = inputs.get("players_armed")
    return None if value is None else int(value)


def armored_by_round(rounds: Sequence[Mapping[str, Any]]) -> dict[SideRoundKey, int]:
    """The armour count from the rounds table, keyed by (demo, round, side).

    **The source is ``parsed/rounds`` and not ``classified``**, unlike the
    armed count. The reason is scope and not convenience: the armed count is
    the half-buy's condition A, so ``classify`` reads it in any case and
    stores the decision in its inputs. The armour count is not an input to the
    decision -- it is an observation -- and it is therefore not added to
    ``economy.CLASSIFY_COLUMNS``. The classification rule stays as it is, and
    the observation is read from where it lives.

    The key **includes the side**: the rounds table has two rows per round,
    and without the side the opponent's armour could end up on our own row.

    Unnumbered rounds (the warmup, the knife round), rows from which the
    observation was not obtained and rows whose key is incomplete stay out of
    the map. A missing key means ``rounds_unknown`` in
    :func:`armored_players_for` -- it must not mean zero, because zero is an
    observation and a failed read is not.

    Raises:
        AggregateError: If two rows claim the same key. Overwriting in silence
            would leave in force whichever happens to be last, and nothing
            would say which figure reached the report. For classified rows the
            same check is :func:`check_rounds_are_unique`.
    """
    lookup: dict[SideRoundKey, int] = {}
    for row in rounds:
        round_no = row.get("round_no")
        value = row.get(ARMORED_COLUMN)
        demo, side = row.get("map_demo_id"), row.get("side")
        # An incomplete key is dropped **before** the str() conversion:
        # ``str(None)`` would build the key "None", which never matches
        # anything but looks entirely ordinary in the map.
        if round_no is None or value is None or demo is None or side is None:
            continue
        key = (str(demo), int(round_no), str(side))
        if key in lookup and lookup[key] != int(value):
            raise AggregateError(
                f"The rounds table holds two different armour counts for the "
                f"same round {key}: {lookup[key]} and {int(value)}.\n"
                "Whichever happens to be last would reach the report. "
                "Run the parse again: uv run pappascout parse "
                f"{demo} --force"
            )
        lookup[key] = int(value)
    return lookup


def armored_players_for(
    rows: Sequence[Mapping[str, Any]],
    armored: Mapping[SideRoundKey, int],
) -> ArmoredPlayers:
    """The armoured players' distribution, round by round.

    **A different observation from** :func:`armed_players_for`, not a
    generalisation of it. The target analysis's *"5 kevlars"* and *"no kevs"*
    are read from here, and the armed distribution cannot give them: on a
    pistol round it is in practice 0, because $800 does not buy both kevlar
    and an upgraded weapon.

    Args:
        rows: The round type's classified rows. They determine the sample, so
            a round from which the armour count was not obtained is
            ``rounds_unknown`` and does not vanish from the distribution.
        armored: :func:`armored_by_round`'s map over the whole sample.
    """
    values: list[int | None] = []
    for row in rows:
        round_no = row.get("round_no")
        demo, side = row.get("map_demo_id"), row.get("side")
        # An incomplete key is a **missing observation and not a crashing
        # run**: every row given produces an element, so that the sample does
        # not shrink in silence, and one incomplete row stays unknown instead
        # of taking the whole aggregation with it. ``classify`` drops the
        # unnumbered ones before this, so the branch is a defence and not an
        # expected state.
        values.append(
            None
            if round_no is None or demo is None or side is None
            else armored.get((str(demo), int(round_no), str(side)))
        )
    known = [v for v in values if v is not None]
    tally = Counter(known)
    return ArmoredPlayers(
        m=len(known),
        rounds_unknown=len(values) - len(known),
        counts=[
            ArmoredCount(armored=armored_n, n=tally[armored_n])
            for armored_n in sorted(tally)
        ],
    )


def first_contact_areas(
    ticks: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
) -> list[FirstContactArea]:
    """The areas the team had a player in at the moment of first contact.

    The observation is **presence**: the same round produces an observation
    for every area in which the team had a living player. ``Σ n = m``
    therefore does not hold and is not meant to -- the full distribution from
    the same moment is in the ``positions`` list's first-contact sample point.
    """
    keys = set(round_keys)
    rounds_with_sample: set[RoundKey] = set()
    present: defaultdict[str | None, set[RoundKey]] = defaultdict(set)
    for row in ticks:
        if str(row["sample_kind"]) != "first_contact":
            continue
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        rounds_with_sample.add(key)
        if bool(row["is_alive"]):
            present[row["area"]].add(key)

    m = len(rounds_with_sample)
    areas = [
        FirstContactArea(area=area, n=len(rounds), m=m)
        for area, rounds in present.items()
    ]
    areas.sort(key=lambda a: _by_count_then_area((a.area, a.n)))
    return areas


# -- Deaths ----------------------------------------------------------------------


def deaths_for(
    deaths: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
    lineup_keys: Iterable[str],
) -> DeathReport:
    """The team's own deaths and kills for one round type.

    Two marginal distributions, and they are read **from different columns of
    the same table**: ``victim_lineup_key`` gives the own deaths,
    ``attacker_lineup_key`` the own kills. The same row can be both -- in a
    teamkill a player of the team killed his own teammate -- and neither is
    filtered out: the observation is that a player died and that the shooter
    was in a particular area, and telling a teamkill apart would be
    interpretation.

    **The first death of a round** is the one with the smallest ``t_s``. A tie
    -- two teammates on the same tick -- is resolved by the victim's id, so
    that the same data gives the same result from one run to the next. A row
    from which ``t_s`` is missing is last in the ordering and not first: a
    missing time is not zero.

    **The kill area is the shooter's own area**, not the victim's. That is
    exactly how the target analysis's line "the enemy came through the secret
    yard" is written: it says where the shooter shot from.

    **A suicide is not a kill.** A row on which the shooter and the victim are
    the same player is an own death but not an own kill: a kill row says where
    the team shoots from, and a suicide's area is a place nobody shot from. A
    teamkill, in contrast, counts towards both -- there a teammate **really
    did shoot** from that area. Measured 2026-08-30: 0 suicides and 1 teamkill
    out of 591 deaths.

    Args:
        deaths: The ``DEATHS`` rows. **Not filtered to the lineup**: the
            function needs both columns, and the caller cannot know which of
            them will match.
        round_keys: This branch's rounds.
        lineup_keys: The team's lineup ids.

    Returns:
        :class:`~pappascout.domain.report.DeathReport`. Empty but valid if
        nobody died in the branch.
    """
    keys = set(round_keys)
    own = set(lineup_keys)

    first: dict[RoundKey, tuple[tuple[int, float, str], Mapping[str, Any]]] = {}
    kills: Counter[str | None] = Counter()
    kills_total = 0

    for row in deaths:
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        suicide = (
            row["attacker_id"] is not None
            and row["attacker_id"] == row["victim_id"]
        )
        if row["attacker_lineup_key"] in own and not suicide:
            kills[_observed_area(row["attacker_area"])] += 1
            kills_total += 1
        if row["victim_lineup_key"] not in own:
            continue
        order = _death_order(row)
        current = first.get(key)
        if current is None or order < current[0]:
            first[key] = (order, row)

    moments = [
        float(row["t_s"])
        for _, row in first.values()
        if row["t_s"] is not None
    ]
    areas: Counter[str | None] = Counter(
        _observed_area(row["victim_area"]) for _, row in first.values()
    )
    m = len(first)

    # The most common area first, ties alphabetically and the unknown one
    # last -- the same order as the first-contact areas, so that the report's
    # rows are read the same way.
    first_areas = [
        FirstDeathArea(area=area, n=n, m=m)
        for area, n in sorted(areas.items(), key=_by_count_then_area)
    ]
    kill_areas = [
        KillArea(area=area, n=n, m=kills_total)
        for area, n in sorted(kills.items(), key=_by_count_then_area)
    ]
    return DeathReport(
        m=m,
        rounds_missing=len(round_keys) - m,
        first_death_seconds_median=(
            round(median(moments), 3) if moments else None
        ),
        first_death_areas=first_areas,
        kills_total=kills_total,
        kills=kill_areas,
    )


def _observed_area(value: Any) -> str | None:
    """The area as an observation: an empty string **is not an area** but
    ``None``.

    ``parse`` already writes an empty ``last_place_name`` as ``null``, but the
    contract allows a string and a table written with an older version has not
    been through that rule. Without normalisation the same observation would
    reach the distribution **twice**: the model's duplicate check compares raw
    values (``""`` and ``None`` are different), but the report shows both
    under the name "tuntematon alue" -- that is, one row would say the same
    thing twice with different figures.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _by_count_then_area(item: tuple[str | None, int]) -> tuple[int, int, str]:
    """The sort key for an area distribution: most common first, the unknown
    one last.

    **One spelling for one rule.** The areas of first contact, of the first
    death and of the kills all order this way, and two copies would diverge:
    the report's rows are read the same way, so they have to order the same
    way too.
    """
    area, count = item
    return (-count, *_area_sort_key(area))


def _death_order(row: Mapping[str, Any]) -> tuple[int, float, str]:
    """The ordering key inside a round: the time first, the victim's id next.

    The first element tells **a missing time apart from zero**: without it a
    row from which ``t_s`` is missing would be the round's first death. The
    victim's id makes a tie repeatable -- two teammates can die on the same
    tick, and the row order must not decide which one appears in the report.
    """
    t_s = row["t_s"]
    return (
        1 if t_s is None else 0,
        0.0 if t_s is None else float(t_s),
        str(row["victim_id"]),
    )


# -- Utility ---------------------------------------------------------------------


def _detonations(
    events: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], Mapping[str, Any]]:
    """The detonation rows keyed by ``(map_demo_id, grenade_no)``.

    **The round is not filtered.** A molotov burns for seven seconds and a
    smoke for eighteen, so a grenade thrown at the end of a round detonates on
    the next round's side of the boundary and gets a different ``round_no``.
    If both rows were filtered against the round separately, the pair would
    break with nothing recorded and the grenade would lose its area in
    silence. A grenade belongs to the round on which it was **thrown**, so
    only the throw is filtered.
    """
    return {
        (str(row["map_demo_id"]), int(row["grenade_no"])): row
        for row in events
        if str(row["event_kind"]) == "grenade_detonate"
    }


def unpaired_detonations(events: Sequence[Mapping[str, Any]]) -> int:
    """The detonations that have no throw row.

    ``parse`` always writes the throw and the detonation as a pair with the
    same ``grenade_no``, so an unpaired detonation means a broken table. It is
    dropped from the utility computation -- there is neither a throw area nor
    a moment of throw -- but the count is returned, because dropping it in
    silence would look as though the grenade had not been thrown at all.
    """
    thrown = {
        (str(row["map_demo_id"]), int(row["grenade_no"]))
        for row in events
        if str(row["event_kind"]) == "grenade_thrown"
    }
    return sum(1 for pair in _detonations(events) if pair not in thrown)


def _grenades(
    events: Sequence[Mapping[str, Any]], round_keys: Sequence[RoundKey]
) -> list[dict[str, Any]]:
    """Pair up the throw and the detonation into one grenade.

    The pair is joined by the key ``(map_demo_id, grenade_no)``:
    ``grenade_no`` is unique inside a demo, ``grenade_entity_id`` is not --
    the game recycles entity ids even inside one round.

    **Only the throw is filtered against the round** (see
    :func:`_detonations`). A missing detonation row does not drop the grenade:
    the throw is an observation on its own and it is exactly what the utility
    is measured from. An unpaired detonation does drop -- it has neither a
    throw area nor a moment of throw -- and its count is reported separately
    by :func:`unpaired_detonations`.
    """
    keys = set(round_keys)
    detonate = _detonations(events)

    grenades: list[dict[str, Any]] = []
    for row in events:
        if str(row["event_kind"]) != "grenade_thrown":
            continue
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        blast = detonate.get((str(row["map_demo_id"]), int(row["grenade_no"])))
        grenades.append(
            {
                "round": key,
                "grenade_type": str(row["grenade_type"]),
                "throw_area": row["area"],
                "t_s": None if row["t_s"] is None else float(row["t_s"]),
                "detonate_area": None if blast is None else blast["area"],
                # The source is read as it stands and not derived from the
                # area: if the table holds an area without a source, the
                # report model fails on it loudly instead of letting an
                # estimate through as an observation.
                "area_source": (
                    None
                    if blast is None or blast["area_source"] is None
                    else str(blast["area_source"])
                ),
            }
        )
    return grenades


def utility_uses(
    events: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
    bucket_edges: Sequence[float],
) -> list[UtilityUse]:
    """Utility patterns: type, throw area, detonation area and time window.

    ``n`` counts **rounds** and ``throws`` counts **grenades**. They differ
    when two identical grenades are thrown to the same place on the same
    round, which is why the sum of the ``n`` values is not the number of
    grenades.

    A grenade without an area (``area`` is ``null``) gets a bucket of its own
    instead of dropping out: a smoke is often thrown where nobody is, and that
    is precisely its purpose.
    """
    m = len(round_keys)
    rounds: defaultdict[tuple[Any, ...], set[RoundKey]] = defaultdict(set)
    throws: Counter[tuple[Any, ...]] = Counter()
    for grenade in _grenades(events, round_keys):
        key = (
            grenade["grenade_type"],
            grenade["throw_area"],
            grenade["detonate_area"],
            grenade["area_source"],
            seconds_bucket(grenade["t_s"], bucket_edges),
        )
        rounds[key].add(grenade["round"])
        throws[key] += 1

    uses = [
        UtilityUse(
            grenade_type=key[0],
            throw_area=key[1],
            detonate_area=key[2],
            area_source=key[3],
            seconds_bucket=key[4],
            n=len(seen),
            throws=throws[key],
            m=m,
        )
        for key, seen in rounds.items()
    ]
    # The time windows in clock order and not alphabetically: alphabetically
    # "10-20" would come before "5-10", and the reader of a pattern expects
    # the order of the clock.
    order = {label: i for i, label in enumerate(bucket_labels(bucket_edges))}
    uses.sort(
        key=lambda u: (
            u.grenade_type,
            _area_sort_key(u.throw_area),
            _area_sort_key(u.detonate_area),
            order.get(u.seconds_bucket, len(order)),
        )
    )
    return uses


def utility_counts_for(
    events: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
) -> list[UtilityCounts]:
    """How many of each grenade type were thrown on a round.

    The set of types is **the types observed in this branch**. The zero bar
    comes from those rounds on which the type was not thrown, so "they threw
    no smokes at all" is an observation and not a missing row. A type that was
    never thrown once is not written at all -- the same rule as for an empty
    round type.
    """
    m = len(round_keys)
    per_type: defaultdict[str, Counter[RoundKey]] = defaultdict(Counter)
    for grenade in _grenades(events, round_keys):
        per_type[grenade["grenade_type"]][grenade["round"]] += 1

    result: list[UtilityCounts] = []
    for grenade_type in sorted(per_type):
        tally = per_type[grenade_type]
        counts = Counter(tally.get(key, 0) for key in round_keys)
        result.append(
            UtilityCounts(
                grenade_type=grenade_type,
                m=m,
                counts=[
                    GrenadeCount(thrown=thrown, n=counts[thrown])
                    for thrown in sorted(counts)
                ],
            )
        )
    return result


# -- Anomalies -------------------------------------------------------------------


def anomalies_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    by_map: Mapping[str, Sequence[str]],
    map_sources: Mapping[str, str],
    area_orientation: Mapping[str, Mapping[str | None, sampling.AreaObservations]],
    point_clouds: Mapping[str, Sequence[sampling.CloudCell]],
    thresholds: ThresholdSettings,
) -> tuple[list[Anomaly], AnomalyScan]:
    """Anomalous set-ups from every map and side as one list.

    The rules themselves are in :mod:`pappascout.domain.sampling` and they
    look at **one round at a time**; this function does three things a rule
    cannot: it calls them with the right demo's orientation, **groups the hits
    into a sample** and records what was examined in the first place.

    **The grouping key is per rule.** ``ct_advance`` is grouped by ``(map,
    side, round type, area)``, because it is a phenomenon of the saving rounds
    and the round type is part of the observation. ``crunch`` and ``stack``
    are grouped by ``(map, side, area)`` **without the round type**: neither
    of them knows it, and splitting into an eco row and a default row would
    give the same pattern two different denominators and the reader would not
    see the total -- that is, the figure would reproduce inside itself the
    very scatter it was made to remove.

    ``n`` is the number of **rounds** on which the hit was observed: the same
    area on two eco rounds is one row with a sample of ``2/m``, not two rows,
    and the same round with two sample points does not raise ``n`` to two.
    ``m`` is all of the grouping level's rounds -- for the advance the round
    type's, for crunch and stack the side's.

    **The site groups are derived once per demo**, not once per round like the
    orientation's threshold filtering. The difference is measured: a point
    cloud holds thousands of cells and deriving them sorts the cells, whereas
    the orientation's filtering is tens of dictionary lookups. A demo's own
    cloud is a per-demo constant, so a result computed once is exactly the
    same -- and it has to be computed in any case for the coverage as well.

    Args:
        rows: The classified rounds that **have** a round type. This is the
            only source for which rounds exist and what side and type they
            are -- the same rule as everywhere else in the report.
        ticks: The sample point rows, **filtered to the team's lineups**. An
            anomaly is the subject's own movement, so the hits are read from
            his rows; the area's orientation, in contrast, **cannot** come
            from them (see ``area_orientation``).
        by_map: Map name -> its demos. The same grouping as in
            :func:`build_report`, so that an anomaly is on the same map as the
            map chapter.
        map_sources: Map name -> the branch's ``map_name_source``. Carried all
            the way to the anomaly, because the report's body speaks in names
            (Story 2.12): with the source ``unknown`` the name **is** the demo
            id, and it must not be set into the body bare.
        area_orientation: ``map_demo_id`` -> (area -> observations) from the
            demo's **unfiltered** sample point table. An argument and not a
            derivation: computed on the subject's rows, every true positive
            vanishes, because the anomaly eats its own detection.
        point_clouds: ``map_demo_id`` -> the demo's ``CALLOUT_CLOUD`` cells.
            The stack's site groups are derived from this
            (:func:`~pappascout.domain.sampling.site_groups`). The same locked
            condition as for the orientation: **the demo's own observation**,
            not a map database and not a table accumulated across the archive.
            The cloud is not filtered to the team's lineups and must not be:
            the map is where it is, no matter which team is the subject.
        thresholds: The ``[thresholds]`` section. Nine anomaly thresholds and
            ``small_sample_rounds`` are read from it.

    Returns:
        The pair ``(anomalies, coverage)``. The anomalies are in the order
        map, side, rule, round types, area. **An empty list is a valid
        result**, and that is exactly why the coverage is returned beside it:
        "no anomalies" is an observation only about what was examined.

    Raises:
        AggregateError: If some included round's demo is missing from
            ``area_orientation`` or from ``point_clouds`` **altogether**. A
            missing key is a different thing from an empty orientation or a
            silenced cloud: the former means the caller left the demo out, and
            a default assumed in silence would silence the rules on that very
            demo with nothing to say so. An empty orientation and a cloud
            without groups, in contrast, are valid observations, and they are
            recorded in the coverage (``demos_without_orientation``,
            ``demos_without_site_groups``).
    """
    # The rows and the sample points are split **once**. An earlier version
    # re-filtered the whole row list for every (map, side, round type)
    # triple, that is 24 times over on one map.
    ticks_by_round: defaultdict[RoundKey, list[Mapping[str, Any]]] = defaultdict(
        list
    )
    for tick in ticks:
        key = _round_key(tick)
        if key is not None:
            ticks_by_round[key].append(tick)

    map_of_demo: dict[str, str] = {}
    for map_name, demos in by_map.items():
        for demo in demos:
            map_of_demo[demo] = map_name

    # The site groups **once** per demo: demo -> (area -> "A"|"B") or None,
    # when the map has no A/B split that separates on the level. ``None`` and
    # an empty description are different answers, so the value is kept as it
    # stands and neither is normalised into the other.
    #
    # The loop walks ``rows``'s demos and not ``point_clouds``'s keys: the
    # coverage is computed from which demos were included, not from which
    # clouds the caller happened to supply.
    groups_by_demo: dict[str, dict[str, str] | None] = {}
    for demo in {str(row["map_demo_id"]) for row in rows}:
        if demo not in point_clouds:
            raise AggregateError(
                f"Demo {demo}'s point cloud was not given, so the stack rule "
                "cannot be run for it.\n"
                "The cloud is the demo's own observation "
                "(parsed/<demo>/callouts.parquet) and the site groups are "
                "derived from it. A missing key means the aggregation left "
                "the demo out -- not that the demo has no cloud. A default "
                "assumed in silence would silence the rule on that very demo."
            )
        groups_by_demo[demo] = sampling.site_groups(
            point_clouds[demo],
            margin=thresholds.stack_group_margin,
            separation_min=thresholds.stack_site_separation_min,
        )

    # (map, side) -> round type -> round rows. One split, from which both the
    # advance's denominator (round type included) and crunch's and stack's
    # (round type left out) can be read.
    branches: defaultdict[
        tuple[str, str], defaultdict[str, list[Mapping[str, Any]]]
    ] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        demo = str(row["map_demo_id"])
        map_name = map_of_demo.get(demo)
        if map_name is None:
            # A round from a demo that is on no map at all. build_report
            # cannot produce this, but the function is public.
            raise AggregateError(
                f"A round from demo {demo!r} belongs to no map, so the "
                "anomaly cannot be named. The map split is "
                "``build_report``'s own, so a difference means that "
                "``by_map`` and ``rows`` are not from the same run."
            )
        branches[(map_name, str(row["side"]))][str(row["round_type"])].append(row)

    hits_by_round = _rule_hits(
        rows, ticks_by_round, area_orientation, groups_by_demo, thresholds
    )

    anomalies: list[Anomaly] = []
    for map_name, side in sorted(branches):
        by_type = branches[(map_name, side)]
        side_rows = [row for type_rows in by_type.values() for row in type_rows]
        source = map_sources.get(map_name, "unknown")
        # The advance first: its denominator is narrower, so the reader sees
        # the per-round-type observation first and then the whole side's
        # crunch and stack. The order is the same from one run to the next.
        for round_type in ROUND_TYPES:
            type_rows = by_type.get(round_type)
            if not type_rows:
                continue
            anomalies.extend(
                _grouped_anomalies(
                    type_rows,
                    hits_by_round,
                    rule=CT_ADVANCE,
                    map_name=map_name,
                    map_name_source=source,
                    side=side,
                    thresholds=thresholds,
                )
            )
        # Crunch and stack share the SHAPE of the denominator (all of the
        # side's rounds) but not its content: crunch reads the area's
        # orientation and the source directions, stack reads neither.
        #
        # **A silenced demo's rounds drop out of the stack's denominator.**
        # A map can have two demos (Story 2.11), one of which yielded no site
        # groups; those rounds are in crunch's denominator but not in
        # stack's, because the rule did not see them. The same scoping as in
        # the coverage figure ``stack_rounds`` -- and without it a row's
        # ``n/m`` would speak of a different coverage from the chapter's own
        # coverage text.
        stack_rows = [
            row
            for row in side_rows
            if groups_by_demo.get(str(row["map_demo_id"])) is not None
        ]
        for rule, branch_rows in (
            (CRUNCH, side_rows),
            (STACK, stack_rows),
        ):
            if not branch_rows:
                continue
            anomalies.extend(
                _grouped_anomalies(
                    branch_rows,
                    hits_by_round,
                    rule=rule,
                    map_name=map_name,
                    map_name_source=source,
                    side=side,
                    thresholds=thresholds,
                )
            )

    # The coverage is computed from what the rule CAN examine, not from how
    # many rounds the loop walked through. All three rules read only
    # ``constants.ANOMALY_RULE_SIDE`` rows, so a T-side round cannot produce a hit in
    # any of them -- and the bare total number of rounds would promise a
    # coverage that does not exist (measured: of RCAVE's 92 rounds 45 are CT
    # and 8 of those are saving rounds, so the advance saw 8 and not 92).
    crunch_rounds = sum(
        1 for row in rows if str(row["side"]) == ANOMALY_RULE_SIDE
    )
    advance_rounds = sum(
        1
        for row in rows
        if str(row["side"]) == ANOMALY_RULE_SIDE
        and str(row["round_type"]) in SAVING_ROUND_TYPES
    )
    # The stack's denominator IS NOT crunch's denominator, even though
    # neither scopes by round type: a silenced demo's (the sites do not
    # separate) CT rounds are in crunch's denominator but not in stack's.
    # Without a figure of its own, Nuke's 27 rounds would look examined with
    # a nil result.
    stack_rounds = sum(
        1
        for row in rows
        if str(row["side"]) == ANOMALY_RULE_SIDE
        and groups_by_demo.get(str(row["map_demo_id"])) is not None
    )
    blind = sorted(
        demo
        for demo in {str(row["map_demo_id"]) for row in rows}
        if not sampling.t_side_shares(
            area_orientation[demo],
            t_share_min=thresholds.advance_t_share,
            min_observations=thresholds.advance_area_min_observations,
        )
    )
    # The silenced demos: ``None`` and not an empty description. An empty
    # description means "the map has an A/B split, but no area is on either
    # side of it" -- that is an observation, not a blind spot, and on that
    # demo the rule WAS run.
    without_groups = sorted(
        demo for demo, groups in groups_by_demo.items() if groups is None
    )
    scan = AnomalyScan(
        rules=list(ANOMALY_RULES),
        rules_deferred=list(ANOMALY_RULES_DEFERRED),
        rounds_scanned=len(rows),
        crunch_rounds=crunch_rounds,
        advance_rounds=advance_rounds,
        stack_rounds=stack_rounds,
        demos_without_orientation=blind,
        demos_without_site_groups=without_groups,
    )
    return anomalies, scan


def _rule_hits(
    rows: Sequence[Mapping[str, Any]],
    ticks_by_round: Mapping[RoundKey, Sequence[Mapping[str, Any]]],
    area_orientation: Mapping[str, Mapping[str | None, sampling.AreaObservations]],
    groups_by_demo: Mapping[str, Mapping[str, str] | None],
    thresholds: ThresholdSettings,
) -> dict[RoundKey, list[sampling.AnomalyHit]]:
    """All three rules' hits round by round, computed once.

    The rules are run **once per round**, not once per grouping level:
    crunch's and stack's denominator is the side and the advance's is the
    round type, so the same round belongs to two levels. Without this
    intermediate step every round would be run twice and the rules could --
    through two different call sites -- get different thresholds.

    **The orientation's threshold filtering still repeats per round and per
    rule**, even though the result is a per-demo constant: by the spec a rule
    is given the orientation as an argument and **applies the thresholds
    itself**, so a pre-filtered map would move the definition out of the rule.
    The measured cost is negligible -- eight demos, 93 rounds and about 18
    areas make on the order of 3,000 dictionary lookups -- so the
    independence is worth the trade here. If the sample grows to tens of demos
    in Epic 3, the right fix is a cache keyed by demo, not a change to the
    rule's contract.
    """
    found: dict[RoundKey, list[sampling.AnomalyHit]] = {}
    for row in rows:
        key = _round_key(row)
        if key is None:
            # classify drops the unnumbered rounds, so this means the
            # classified table is broken. build_report raises its own error
            # about it, with instructions.
            continue
        demo = key[0]
        if demo not in area_orientation:
            raise AggregateError(
                f"Demo {demo}'s area orientation was not given, so the "
                "anomaly rules cannot be run for it.\n"
                "The orientation is the demo's own observation and it is "
                "computed from the UNFILTERED sample point table. A missing "
                "key means the aggregation left the demo out -- not that the "
                "demo has no orientation. A default assumed in silence would "
                "silence the rules on that very demo."
            )
        orientation = area_orientation[demo]
        presences = [_presence(tick) for tick in ticks_by_round.get(key, ())]
        found[key] = sampling.ct_advance_hits(
            presences,
            round_type=str(row["round_type"]),
            orientation=orientation,
            t_share_min=thresholds.advance_t_share,
            area_min_observations=thresholds.advance_area_min_observations,
            max_sample_s=thresholds.advance_max_sample_s,
            min_players=thresholds.advance_min_players,
        ) + sampling.crunch_hits(
            presences,
            orientation=orientation,
            t_share_min=thresholds.advance_t_share,
            area_min_observations=thresholds.advance_area_min_observations,
            max_sample_s=thresholds.advance_max_sample_s,
            min_players=thresholds.crunch_min_players,
            min_sources=thresholds.crunch_min_sources,
        ) + sampling.stack_hits(
            presences,
            # ``None`` (the sites do not separate) silences the rule, and
            # that is a different thing from a missing key: a missing key
            # raises an error already in ``anomalies_for``, because it would
            # mean the caller left the demo out.
            groups=groups_by_demo[demo],
            # The time bound is COMMON to all three rules and not stack's own.
            max_sample_s=thresholds.advance_max_sample_s,
            min_players=thresholds.stack_min_players,
        )
    return found


def _grouped_anomalies(
    branch_rows: Sequence[Mapping[str, Any]],
    hits_by_round: Mapping[RoundKey, Sequence[sampling.AnomalyHit]],
    *,
    rule: str,
    map_name: str,
    map_name_source: str,
    side: str,
    thresholds: ThresholdSettings,
) -> list[Anomaly]:
    """One rule's anomalies at one grouping level.

    ``branch_rows`` is the set that determines the denominator: for the
    advance one round type's rounds, for crunch all of the side's rounds. The
    same function serves both, because the difference is **only** in which
    rows are given.
    """
    m = len(branch_rows)
    tally: defaultdict[str, _AnomalyTally] = defaultdict(_AnomalyTally)
    for row in branch_rows:
        key = _round_key(row)
        if key is None:
            continue
        for hit in hits_by_round.get(key, ()):
            if hit.rule != rule:
                continue
            tally[hit.area].add(key, str(row["round_type"]), hit)

    return [
        tally[area].to_anomaly(
            rule=rule,
            area=area,
            map_name=map_name,
            map_name_source=map_name_source,
            side=side,
            m=m,
            small_sample=m < thresholds.small_sample_rounds,
        )
        for area in sorted(tally)
    ]


@dataclass
class _RoundTally:
    """One round's tally: the sample points with their observations, and the
    source directions.

    The directions are collected **inside the round**, because only there are
    they simultaneous. The union of two rounds would read as more simultaneous
    directions than were observed.

    **The player count is not simultaneous even inside a round.** One maximum
    and a list of sample points set the maximum against every point in the
    report -- measured, MatureMayhem Inferno round 2 has five players at 15 s
    and one at 30 s, but the row read "5 players at 15 and 30 s". So the
    figure is on the sample point's own row and not in the round's summary.
    """

    round_type: str
    #: Sample point -> (players, alive). A dictionary and not a list: the same
    #: rule produces at most one hit per sample point for one area, so the key
    #: is unique and the order comes from the sort.
    points: dict[float, tuple[int, int | None]] = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)

    def add(self, hit: sampling.AnomalyHit) -> None:
        self.points[hit.sample_t_s] = (hit.players, hit.alive)
        self.sources.update(hit.sources)

    @property
    def players_max(self) -> int:
        """The largest player count on the round; the source of the row's
        ``players_max``."""
        return max(players for players, _ in self.points.values())


@dataclass
class _AnomalyTally:
    """One area's tally at one grouping level.

    Per-round bookkeeping and not one set: the report's row says four things
    (how often, when, from where, how many), and three of them are true only
    inside a round.
    """

    rounds: dict[RoundKey, _RoundTally] = field(default_factory=dict)
    #: Demo -> the area's orientation in that demo. A map can come from two
    #: demos (Story 2.11), and their T shares can differ -- one figure would
    #: force choosing a mean that was never observed. **Empty for stack**: the
    #: rule does not read the orientation, so it has none to record.
    orientation: dict[str, tuple[float, int]] = field(default_factory=dict)
    #: The site's group. Only for stack; every hit for the same area is from
    #: the same group, because the area **is** the group's own site.
    site: str | None = None

    def add(
        self, key: RoundKey, round_type: str, hit: sampling.AnomalyHit
    ) -> None:
        entry = self.rounds.get(key)
        if entry is None:
            entry = _RoundTally(round_type=round_type)
            self.rounds[key] = entry
        entry.add(hit)
        if hit.t_share is not None and hit.observations is not None:
            self.orientation[key[0]] = (hit.t_share, hit.observations)
        if hit.site is not None:
            self.site = hit.site

    def to_anomaly(
        self,
        *,
        rule: str,
        area: str,
        map_name: str,
        map_name_source: str,
        side: str,
        m: int,
        small_sample: bool,
    ) -> Anomaly:
        rounds = [
            AnomalyRound(
                map_demo_id=demo,
                round_no=round_no,
                round_type=entry.round_type,
                points=[
                    AnomalyPoint(
                        sample_t_s=seconds, players=players, alive=alive
                    )
                    for seconds, (players, alive) in sorted(
                        entry.points.items()
                    )
                ],
                sources=sorted(entry.sources),
            )
            for (demo, round_no), entry in sorted(self.rounds.items())
        ]
        types = {entry.round_type for entry in rounds}
        return Anomaly(
            rule=rule,
            map_name=map_name,
            map_name_source=map_name_source,
            side=side,
            area=area,
            site=self.site,
            round_types=[name for name in ROUND_TYPES if name in types],
            rounds=rounds,
            orientation=[
                AreaOrientation(
                    map_demo_id=demo,
                    t_share=round(t_share, 4),
                    observations=observations,
                )
                for demo, (t_share, observations) in sorted(
                    self.orientation.items()
                )
            ],
            players_max=max(entry.players_max for entry in rounds),
            n=len(rounds),
            m=m,
            small_sample=small_sample,
        )


def _presence(tick: Mapping[str, Any]) -> sampling.AreaPresence:
    """A sample point row as the rule's record.

    The coordinates are left out on purpose: the rules read only the area, and
    a coordinate would tempt one into geometry that is not there.

    **The side and being alive are required as observations.** ``str(None)``
    and ``bool(None)`` would quietly decide that the row is not CT or that the
    player is dead -- and for an anomaly that difference is the whole result,
    not one bar in a distribution. In the sample points' distributions
    (:func:`positions_for`) a missing ``is_alive`` costs one player; here it
    decides whether the anomaly exists at all, so it must not be read as a
    default.

    Raises:
        AggregateError: If ``sample_t_s``, ``side`` or ``is_alive`` is missing
            or unknown.
    """
    seconds = _sample_seconds(tick)
    side = tick["side"]
    if side not in SIDES:
        raise AggregateError(
            _broken_tick(tick, f"the side is {side!r} and not {' or '.join(SIDES)}")
        )
    if tick["is_alive"] is None:
        raise AggregateError(_broken_tick(tick, "being alive is missing"))
    return sampling.AreaPresence(
        player_id=str(tick["player_id"]),
        side=str(side),
        sample_kind=str(tick["sample_kind"]),
        sample_t_s=seconds,
        area=sampling.normalize_area(tick["area"]),
        is_alive=bool(tick["is_alive"]),
    )


def _broken_tick(tick: Mapping[str, Any], what: str) -> str:
    """The error message for a broken sample point row, instructions included."""
    demo = tick["map_demo_id"]
    return (
        f"On a sample point row {what} (demo {demo!r}, round "
        f"{tick['round_no']!r}), so the anomaly rule cannot be run for it."
        f"{NEWLINE}Run the parse again: uv run pappascout parse {demo} "
        "--force"
    )


# -- The whole report ------------------------------------------------------------


def check_rounds_are_unique(rows: Sequence[Mapping[str, Any]]) -> None:
    """Make sure ``(map_demo_id, round_no)`` occurs at most once.

    The key is the whole structure's join key, and a duplicate would break the
    sample in two ways at the same time: a round type's ``m`` counts from the
    **list** (the duplicate included) but a sample point's ``m`` from the
    **set** (the duplicate excluded), so the same data would produce two
    different figures and the difference would show in the ``rounds_missing``
    field -- the very field whose job is to stop a round from vanishing.

    Raises:
        AggregateError: If the same round occurs twice. In practice the cause
            is two classified tables from the same demo.
    """
    seen: Counter[RoundKey] = Counter()
    for row in rows:
        key = _round_key(row)
        if key is not None:
            seen[key] += 1
    twice = sorted(f"{demo} round {no}" for (demo, no), n in seen.items() if n > 1)
    if twice:
        raise AggregateError(
            "The same round occurs in the classified tables more than "
            f"once: {', '.join(twice[:10])}"
            + (f" (+{len(twice) - 10} more)" if len(twice) > 10 else "")
            + ".\n"
            "The join key (map_demo_id, round_no) is the whole structure's "
            "foundation, and a duplicate would distort every sample."
        )


def classify_thresholds(
    rows: Sequence[Mapping[str, Any]],
    expected: ThresholdSettings | None = None,
) -> dict[str, int]:
    """The thresholds the rounds were **actually classified with**.

    Read from the ``CLASSIFIED.inputs`` column, where ``classify`` stores the
    values used for every round's comparison. This is an observation and not a
    copy of the current settings: the user can change ``settings.toml`` and
    run the aggregation alone, in which case ``[thresholds]`` would speak of
    thresholds no round was classified with.

    Args:
        rows: The classified rounds.
        expected: The current ``[thresholds]`` settings. If given, the
            observed values **are compared against them**: a difference means
            a threshold has been changed and the classification has not been
            run again, in which case the report would name thresholds no round
            was classified with.

    Raises:
        AggregateError: If some threshold differs between the rounds or from
            the current settings. The former would mix rounds classified by
            different rules into the same figure, the latter would give the
            report the wrong explanation -- in both cases the figure would
            look right but would not mean what it claims.
    """
    seen: defaultdict[str, set[int]] = defaultdict(set)
    for row in rows:
        inputs = row.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        for name in CLASSIFY_THRESHOLD_KEYS:
            value = inputs.get(name)
            if value is not None:
                seen[name].add(int(value))
    mixed = sorted(f"{name}: {sorted(v)}" for name, v in seen.items() if len(v) > 1)
    if mixed:
        raise AggregateError(
            "The rounds have been classified with different thresholds, so "
            f"they cannot be counted into the same sample: {'; '.join(mixed)}.\n"
            "Run the classification again for every demo with the same "
            "settings: uv run pappascout classify <map_demo_id> --team "
            "<id> --force"
        )
    found = {name: next(iter(v)) for name, v in sorted(seen.items())}
    if expected is not None:
        stale = sorted(
            f"{name}: classified with {value}, in the settings "
            f"{getattr(expected, name)}"
            for name, value in found.items()
            if getattr(expected, name, value) != value
        )
        if stale:
            raise AggregateError(
                "The rounds have been classified with different thresholds "
                "from what is in the settings now, so the report would name "
                f"thresholds no round was classified with: "
                f"{'; '.join(stale)}.\n"
                "Run the classification again before the aggregation: "
                "uv run pappascout classify <map_demo_id> --team "
                "<id> --force"
            )
    return found


def build_report(
    *,
    classified: pl.DataFrame,
    ticks: pl.DataFrame,
    events: pl.DataFrame,
    deaths: pl.DataFrame,
    rounds: pl.DataFrame,
    team: TeamReport,
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    map_pool: Sequence[str],
    map_names: Mapping[str, str | None],
    area_orientation: Mapping[str, Mapping[str | None, sampling.AreaObservations]],
    point_clouds: Mapping[str, Sequence[sampling.CloudCell]],
    generated_at: datetime,
    tool_versions: Mapping[str, str] | None = None,
    missing_demos: Sequence[MissingDemo] = (),
) -> Report:
    """Build the whole report from finished tables.

    Args:
        classified: Every demo's ``CLASSIFIED`` rows as one frame. This is the
            only source for which rounds exist and what type they are.
        ticks: Every demo's ``TICKS`` rows, **filtered to the team's
            lineups**.
        events: Every demo's ``EVENTS`` rows, filtered the same way.
        deaths: Every demo's ``DEATHS`` rows. The filtering is **different**:
            a row belongs to the team if either the victim or the shooter is
            in its lineup, so filtering on one column would drop either the
            deaths or the kills. Splitting the rows between them is done here
            (:func:`deaths_for`) against the team's ``lineup_keys`` list.
        rounds: Every demo's ``ROUNDS`` rows, **filtered to the team's
            lineups**. Only the armour count is read from here
            (:func:`armored_by_round`): it is an observation and not an input
            to the classification's decision, so ``classify`` does not carry
            it forward. The round type and the sample are still owned by
            ``classified`` -- this table must not add or remove a single
            round.
        team: The team's details; the ``aggregate`` stage assembles them from
            the archive.
        thresholds: The ``[thresholds]`` section. ``small_sample_rounds`` is
            read from it.
        aggregate: The ``[aggregate]`` section. ``utility_seconds_buckets`` is
            read from it. Both sections are recorded in the
            ``thresholds_used`` field for traceability -- they are **this
            run's** settings, not the ones the rounds were classified with
            (see :func:`classify_thresholds`).
        map_pool: ``[league].map_pool``, the pool the map's name is inferred
            against when the header held no name.
        map_names: ``map_demo_id`` -> the map name observed from the header,
            or ``None``. The source is ``MATCH.map_name`` (Story 2.11). The
            argument is **mandatory and has no default**: an empty default
            would quietly hand the whole report back to inference, and the
            FACEIT demos would break into branches of their own again with
            nothing to say so. Every included demo has to be in the map; a
            missing key raises an error (:func:`observed_map_name`), because
            it is a different thing from the value ``None``. The maps are
            grouped **by name**, and a branch's ``map_name_source`` is the
            weakest of its demos (:func:`weakest_map_source`).
        area_orientation: ``map_demo_id`` -> (area -> observations) from the
            demo's **unfiltered** sample point table, that is from both teams'
            rows. The anomaly rules read from it which side's area an area is
            in that demo. Every included demo has to be in the map; a missing
            key raises an error, because it is a different thing from an
            **empty** orientation (which is a valid observation and is
            recorded in ``anomaly_scan``).

            The argument is **mandatory and has no default**, and it comes
            from the stage rather than from this function for two reasons at
            once. The first is measured: ``ticks`` is filtered to the team's
            lineups, and computed on the subject's own rows every true
            positive vanishes -- when the subject advances into an area as CT,
            his own CT observations lower that area's T share, that is, the
            anomaly eats its own detection. The second is structural:
            ``build_report`` sees only the subject's rows, and passing the
            whole table in here would open the door to the opponent's figures
            leaking into the report.
        point_clouds: ``map_demo_id`` -> the demo's ``CALLOUT_CLOUD`` cells,
            from which the stack's site groups are derived. **Mandatory and
            without a default** for the same reason as ``area_orientation``:
            an empty default would silence the rule on every demo and record
            it in the coverage as a blind spot, which is the caller's
            oversight and not a property of the demo. Every included demo has
            to be in the map; a missing key raises an error, because it is a
            different thing from a cloud that yielded no groups.
        generated_at: The moment of the run.
        tool_versions: The tool versions, for the report's own field.
        missing_demos: The matches whose data was not there.

    Returns:
        A validated :class:`Report`. If the sample does not match at some
        level, the model itself raises
        :class:`~pappascout.errors.AggregateError`.
    """
    rows = classified.to_dicts()
    tick_rows = ticks.to_dicts()
    event_rows = events.to_dicts()
    death_rows = deaths.to_dicts()
    armored = armored_by_round(rounds.to_dicts())

    check_rounds_are_unique(rows)
    buckets = demo_buckets(rows)
    # The second breakdown of the same sample. Bucketed from the same rows as
    # the league one, so a demo cannot be in the sample of one and outside the
    # other -- and ``Report`` holds the two totals to being equal.
    roster_buckets = roster_demo_buckets(rows)
    played = [r for r in rows if r["round_type"] is not None]
    unclassified = len(rows) - len(played)

    by_demo: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in played:
        by_demo[str(row["map_demo_id"])].append(row)

    # Two demos from the same map are one branch: the rounds add up, and
    # map_demo_ids says where they came from.
    #
    # THE GROUPING IS **BY NAME**, NOT BY THE PAIR (name, source). The pair
    # would look right but would break the map apart exactly the way a
    # missing header did before this story: ``ANCIENT_vs_RCAVE_VETERANS``
    # (observed from the header) and ``Ancient_vs_kaljukostaja`` (inferred
    # from the filename) are both ``de_ancient``, but with a different source
    # -- and as two keys they would be two ``de_ancient`` sections, both
    # marked "(1/1 kierroksesta)". Before Story 2.11 the fault could not
    # arise: an ``unknown`` branch's name is the id itself, so it does not
    # collide with a real name. With the observation, two different sources
    # can produce the same name, and that is why the key is the name.
    #
    # A branch's source is the **weakest** of its demos
    # (:func:`weakest_map_source`).
    by_map: defaultdict[str, list[str]] = defaultdict(list)
    branch_sources: defaultdict[str, list[str]] = defaultdict(list)
    for demo in sorted(by_demo):
        name, source = map_name_for(
            demo, map_pool, observed_map_name(map_names, demo)
        )
        by_map[name].append(demo)
        branch_sources[name].append(source)
    # The branch's source once: both the map chapter and the anomaly row need
    # it, and computed twice they could disagree about whether the map's name
    # was identified.
    map_sources = {
        name: weakest_map_source(sources)
        for name, sources in branch_sources.items()
    }

    ticks_by_demo = _group_by_demo(tick_rows)
    events_by_demo = _group_by_demo(event_rows)
    deaths_by_demo = _group_by_demo(death_rows)

    maps: list[MapReport] = []
    for map_name, demos in by_map.items():
        source = map_sources[map_name]
        map_rows = [r for demo in demos for r in by_demo[demo]]
        map_ticks = [r for demo in demos for r in ticks_by_demo.get(demo, [])]
        map_events = [r for demo in demos for r in events_by_demo.get(demo, [])]
        map_deaths = [r for demo in demos for r in deaths_by_demo.get(demo, [])]
        maps.append(
            MapReport(
                map_name=map_name,
                map_name_source=source,
                map_demo_ids=demos,
                sample=sample_for(map_rows, buckets),
                sides=_sides_for(
                    map_rows,
                    map_ticks,
                    map_events,
                    map_deaths,
                    armored,
                    buckets,
                    thresholds,
                    aggregate,
                    team.lineup_keys,
                ),
            )
        )
    # The most played maps first; on a tie the name, so that the order is the
    # same from one run to the next.
    maps.sort(key=lambda m: (-m.sample.rounds, m.map_name))

    # The anomalies are computed **after the maps** and not before, and the
    # call is here and not in the Report call's argument list: both read the
    # same sample point rows, and a broken table's error has to come from the
    # reader that ordinarily meets it. A sample point's missing sample_t_s is
    # now in one place (:func:`_sample_seconds`), so the order no longer
    # decides the wording of the error message -- it decides only which row it
    # speaks of.
    anomalies, scan = anomalies_for(
        played,
        tick_rows,
        by_map,
        map_sources,
        area_orientation,
        point_clouds,
        thresholds,
    )

    thresholds_used = {
        "thresholds": thresholds.model_dump(mode="json"),
        "aggregate": aggregate.model_dump(mode="json"),
    }
    return Report(
        generated_at=generated_at,
        tool_versions=dict(tool_versions or {}),
        team=team,
        sample=sample_for(played, buckets),
        roster_sample=roster_sample_for(played, roster_buckets),
        thresholds_used=thresholds_used,
        classify_thresholds=classify_thresholds(rows, thresholds),
        unpaired_detonations=unpaired_detonations(event_rows),
        missing_demos=list(missing_demos),
        unclassified_rounds=unclassified,
        anomalies=anomalies,
        anomaly_scan=scan,
        maps=maps,
    )


def _group_by_demo(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["map_demo_id"])].append(row)
    return grouped


def _sides_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    deaths: Sequence[Mapping[str, Any]],
    armored: Mapping[SideRoundKey, int],
    buckets: Mapping[str, str],
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    lineup_keys: Sequence[str],
) -> list[SideReport]:
    """The sides in a fixed order; a side with no rounds is left out."""
    sides: list[SideReport] = []
    for side in SIDES:
        side_rows = [r for r in rows if str(r["side"]) == side]
        if not side_rows:
            continue
        sides.append(
            SideReport(
                side=side,
                sample=sample_for(side_rows, buckets),
                round_types=_round_types_for(
                    side_rows,
                    ticks,
                    events,
                    deaths,
                    armored,
                    buckets,
                    thresholds,
                    aggregate,
                    lineup_keys,
                ),
            )
        )
    return sides


def _round_types_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    deaths: Sequence[Mapping[str, Any]],
    armored: Mapping[SideRoundKey, int],
    buckets: Mapping[str, str],
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    lineup_keys: Sequence[str],
) -> list[RoundTypeReport]:
    """The round types in a fixed order.

    A type that was not played on the map is **absent from the structure** and
    is not a zero row: a zero row would claim as an observation that there is
    no observation. No filtering is done in the other direction either -- full
    buys and overtime are counted too, because the report chooses what it
    says.
    """
    reports: list[RoundTypeReport] = []
    for round_type in ROUND_TYPES:
        type_rows = [r for r in rows if str(r["round_type"]) == round_type]
        if not type_rows:
            continue
        keys: list[RoundKey] = []
        for row in type_rows:
            key = _round_key(row)
            if key is None:
                # classify drops the unnumbered rounds, so this means the
                # classified table is broken. An unguarded int(None) would
                # fail with a TypeError and no instructions.
                raise AggregateError(
                    f"The classified table {row['map_demo_id']!r} holds a row "
                    "without a round number, so it cannot be joined to the "
                    "sample points.\n"
                    f"Run the classification again: uv run pappascout classify "
                    f"{row['map_demo_id']} --force"
                )
            keys.append(key)
        sample = sample_for(type_rows, buckets)
        reports.append(
            RoundTypeReport(
                round_type=round_type,
                sample=sample,
                small_sample=sample.rounds < thresholds.small_sample_rounds,
                positions=positions_for(ticks, keys),
                utility=utility_uses(
                    events, keys, aggregate.utility_seconds_buckets
                ),
                utility_counts=utility_counts_for(events, keys),
                players_armed=armed_players_for(type_rows),
                players_armored=armored_players_for(type_rows, armored),
                first_contact=first_contact_areas(ticks, keys),
                deaths=deaths_for(deaths, keys, lineup_keys),
            )
        )
    return reports
