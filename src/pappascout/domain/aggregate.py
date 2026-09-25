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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
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
    seconds_label,
)
from pappascout.domain import sampling
from pappascout.domain.models import AggregateSettings, ThresholdSettings
from pappascout.domain.selection import match_of
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
    PlayedMap,
    PlayersCount,
    Position,
    Report,
    RosterEntry,
    RosterSample,
    ROUTE_ROUND_TYPE,
    RoundRecord,
    RoundRoute,
    RoundTypeReport,
    RouteStep,
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
    "MatchOrder",
    "MatchFact",
    "match_of",
    "matches_of",
    "newest_match",
    "played_maps_for",
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
    "record_for",
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

#: The matches' order, newest first: match key -> place, ``0`` = newest.
#:
#: **An order and not a date**, and the difference is the layering. A match's
#: own time is not in any table ``aggregate`` reads -- ``CLASSIFIED`` carries
#: ``map_demo_id`` and nothing else about the match -- so it comes from the
#: match index, which ``discover`` writes and only the stage can read (AD-2:
#: ``domain`` reads no file). Handed a list of times, this module would have
#: to decide what a missing one means; handed a place, it has one thing to
#: look up and one thing to say when the lookup fails.
#:
#: A match key that is **not** in it has no known place. That is a real state
#: and not a fault: a demo imported by hand is in no match index, and
#: :func:`newest_match` answers ``None`` rather than guessing.
#:
#: **A mapping here and a sequence at the edge.** :func:`build_report` takes
#: the matches as a list, newest first, because that is what a caller has and
#: the only shape in which "newest first" is self-evident; it is turned into
#: this mapping once, at the top of that function. Every reader below asks
#: one question -- where does this key sit -- and asking it of a list would be
#: a scan per round type, with the ordering re-derived at each use.
MatchOrder = Mapping[str, int]


def matches_of(round_keys: Iterable[RoundKey]) -> set[str]:
    """The matches a set of rounds comes from (:func:`.selection.match_of`).

    **One function for every match count in the report**, and that is the
    point of it rather than brevity: the counts are built at five levels and
    in three shapes, and a second spelling of "which match is this round's"
    would let two of them come to disagree -- the failure
    ``SITE_GROUPS = tuple(SITE_AREAS)`` exists to prevent one level up.
    """
    return {match_of(demo) for demo, _ in round_keys}


def newest_match(matches: Iterable[str], order: MatchOrder) -> str | None:
    """The newest of these matches, or ``None`` if it cannot be told.

    The report's recency mark rests on this: an observation is marked as
    including the newest match, or as not including it, and the reader's
    question before a match ("is this still true?") is answered by which.

    **One match is its own newest, order or no order.** A set of a single
    match needs no index to be sure which of them is the latest, and saying
    so here rather than in each caller is what keeps the answer true for a
    hand-imported demo. It is the common case in this archive: of the three
    teams measured 2026-09-24, two are entirely hand-imported demos, so most
    of their **maps** hold one match.

    **The caller asks this at the map level and not at the block's**, and the
    difference is what makes the answer worth printing.
    :func:`positions_for` has the measurement: a block that holds no round
    from the map's newest match is exactly the block a reader needs to be
    told is stale, and asking the block would have told them the opposite.
    On a map of one match the answer is therefore the same for every block,
    and it is still information -- it says the map has one match and this is
    it.

    **The key this looks up is** :func:`.selection.match_of`'s, which resolves
    a composed id to its match and leaves anything else as its own match. An
    id that does not resolve is in no match index either, so a set holding one
    loses the mark rather than getting a wrong one -- and that is the safe
    direction: the mark is absent where it cannot be trusted.

    **Otherwise every match has to have a place.** With one match unplaced,
    the newest of the rest may or may not be the newest of all, and a mark
    that is right most of the time is worse here than no mark: the reader
    would act on it. ``None`` is then the answer, and the report writes
    nothing rather than a guess.

    Args:
        matches: The match keys to choose between.
        order: Match key -> place, newest first. See :data:`MatchOrder`.

    Returns:
        The newest match's key, or ``None`` when the order does not settle it
        -- including when ``matches`` is empty, because then there is no
        newest match rather than an unknown one. The two are the same answer
        to the caller: there is nothing to mark.
    """
    keys = set(matches)
    if len(keys) == 1:
        return next(iter(keys))
    if not keys or any(key not in order for key in keys):
        return None
    return min(keys, key=lambda key: order[key])


@dataclass(frozen=True)
class MatchFact:
    """What the archive's match index says about one match (Story 4.10).

    **Values and not a file**, the same line :data:`MatchOrder` holds: the
    index is ``index/matches.json``, which ``discover`` writes and only a
    stage may read (AD-2). What reaches this module is a date that has
    already been parsed and a name that has already been picked, so nothing
    here needs a clock, a time zone or a rule for which of a match's two
    teams is the opponent.

    A match key **absent** from the mapping is a match the index does not
    hold, which is an ordinary state and not a fault: a hand-imported demo is
    in no index. A key that is present with both fields ``None`` is a
    different statement -- the index holds the match and says neither -- and
    :class:`~pappascout.domain.report.PlayedMap` keeps the two apart with its
    ``indexed`` flag.

    Attributes:
        played_on: The day the match finished, or ``None`` when the index
            gives no finish time. Measured 2026-09-24: 35 of the developer
            archive's 66 indexed matches have none.
        opponent: The other team's name, or ``None`` when the entry does not
            yield one.
    """

    played_on: date | None
    opponent: str | None


def played_maps_for(
    demos: Iterable[str], order: MatchOrder, facts: Mapping[str, MatchFact]
) -> list[PlayedMap]:
    """One map's demos as the report lists them: **newest first**.

    The list the product owner asked for twice on 2026-09-24 -- how many maps,
    played when, and against whom. Every value on a row is looked up by the
    demo's **match** (:func:`match_of`), because two demos of one ``best_of``
    match were played on the same day against the same team and must not be
    read as two meetings.

    **The order is the report's own recency order and not a second sort.**
    ``order`` is the same mapping :func:`newest_match` reads, so the row the
    list puts first is the match every block's recency mark is decided
    against. A demo the order does not place has no place at all: it is not
    "oldest" and not "newest", so it goes after the placed ones, where its own
    row says it carries no date. Sorting it among them by anything else --
    the id, the map name, the order the files were read -- would put a number
    in a position that claims a date it does not have.

    Ties are broken on the demo id so the list is the same from one run to
    the next. A tie is real and not hypothetical: a ``best_of`` match's two
    demos share a match key, and so do two recordings of the same map.

    Args:
        demos: The map's ``map_demo_id`` values, in any order.
        order: Match key -> place, newest first. See :data:`MatchOrder`.
        facts: Match key -> what the index says. A key that is missing means
            the index does not hold that match.

    Returns:
        One :class:`~pappascout.domain.report.PlayedMap` per demo. The list
        is as long as ``demos`` -- nothing is grouped away here, because the
        map's sample counts demos and
        :class:`~pappascout.domain.report.MapReport` holds the two to being
        equal.
    """
    # Past every rank in use, so an unplaced demo sorts after every placed
    # one. **Not ``len(order)``**, which was the first version and is wrong
    # when the index holds a match twice: ``order`` is a mapping built from a
    # list that :func:`~pappascout.stages.aggregate._order_of` does not
    # deduplicate, so two rows with one id give it fewer keys than ranks --
    # measured 2026-09-25, the list ``[A, A, B]`` yields ``{A: 1, B: 2}``,
    # where ``len`` is 2 and 2 is a rank in use. The unplaced demo would then
    # tie with the last placed one and the id would break the tie, putting a
    # dateless row above a dated one.
    unplaced = max(order.values(), default=-1) + 1
    rows: list[tuple[int, str, PlayedMap]] = []
    for demo in demos:
        match = match_of(demo)
        fact = facts.get(match)
        rows.append(
            (
                order.get(match, unplaced),
                demo,
                PlayedMap(
                    map_demo_id=demo,
                    indexed=fact is not None,
                    played_on=fact.played_on if fact else None,
                    opponent=fact.opponent if fact else None,
                ),
            )
        )
    return [entry for _, _, entry in sorted(rows, key=lambda row: row[:2])]


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

    Two demos from the same map are one branch (``played_maps`` lists them),
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
    """One level's sample: demos, matches and rounds in three buckets.

    **The matches are counted from the same rows, in the same pass**, which is
    the whole reason this is one function and not two -- the same design
    :func:`record_for` states for the win-loss record. A second pass over a
    differently filtered frame is what lets two numbers on the same line come
    to disagree, and this one cannot drift because there is no second pass.

    The match count is **not bucketed**; :attr:`.report.Sample.matches` says
    why.
    """
    demos: defaultdict[str, set[str]] = defaultdict(set)
    rounds: Counter[str] = Counter()
    matches: set[str] = set()
    for row in rows:
        demo = str(row["map_demo_id"])
        bucket = buckets[demo]
        demos[bucket].add(demo)
        rounds[bucket] += 1
        matches.add(match_of(demo))
    made = {
        name: SampleBucket(demos=len(demos[name]), rounds=rounds[name])
        for name in LEAGUE_BUCKETS
    }
    return Sample(
        demos=sum(b.demos for b in made.values()),
        rounds=sum(b.rounds for b in made.values()),
        matches=len(matches),
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


def players_distribution(
    counts: Iterable[tuple[RoundKey, int]],
    newest: str | None,
) -> list[PlayersCount]:
    """The player counts' distribution as bars.

    The input is **one element per round**, zeros included: they are exactly
    what produces the ``players = 0`` bar, without which ``Σ n = m`` would not
    hold.

    **The round's key travels with its count**, and that is Story 4.9's one
    change to the shape of this function. The bar's rounds and its matches are
    then counted in the same pass over the same elements, so they cannot be
    made from different sets -- the same reason
    :func:`record_for` takes rows and not a filter. A second function that
    re-derived the matches from the sample point would be free to disagree
    with the bar beside it, and nothing in the report would show it.

    Args:
        counts: ``(round key, players in the area)``, one per round.
        newest: The newest match of the denominator these bars are read
            against, or ``None`` when the matches' order is not known
            (:func:`newest_match`). Every bar's ``newest`` is then ``null``,
            which the report writes as no mark at all rather than as a
            denial. **It takes no default**, for the reason the report model
            gives no field one: a default would be a silent ``null``, and a
            recency mark that is quietly absent reads exactly like a report
            whose matches have no order.
    """
    per_count: defaultdict[int, set[RoundKey]] = defaultdict(set)
    for key, value in counts:
        per_count[int(value)].add(key)
    return [
        PlayersCount(
            players=players,
            n=len(per_count[players]),
            matches=len(matches_of(per_count[players])),
            newest=(
                None if newest is None else newest in matches_of(per_count[players])
            ),
        )
        for players in sorted(per_count)
    ]


def area_distributions(
    rows_by_round: Mapping[RoundKey, Sequence[Mapping[str, Any]]],
    newest: str | None,
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
        newest: The sample point's newest match, or ``None``. Passed on to
            every bar; see :func:`players_distribution`.
    """
    m = len(rows_by_round)
    matches_m = len(matches_of(rows_by_round.keys()))
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
            matches_m=matches_m,
            players_dist=players_distribution(
                ((key, per_round[key][area]) for key in rows_by_round),
                newest,
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
    newest: str | None,
) -> list[Position]:
    """The sample points of one map/side/round type branch.

    Time sample points are grouped by ``sample_t_s``, first contact into
    **one** sample point: its moment differs on every round, so grouping it by
    ``sample_t_s`` would produce one sample point per round.

    **The newest match is the map's, and it is handed in rather than derived
    here.** Which match the mark names is a question about the whole map
    chapter and not about this branch, so the caller settles it once
    (:func:`build_report`) and every branch of the map gets the same answer.

    That scope was measured into place, twice, and both numbers below were
    re-measured on 2026-09-24 after a review found the first pair wrong.

    Taken from the **sample point's** own matches, the mark would have meant
    *the newest match that has a 45-second sample*: of the real archive's 377
    sample points, **15** cover fewer matches than their branch, **14** of
    those have any bar at all and **11** print a mark. (An earlier version of
    this paragraph said 14 printed one; 14 is the count of non-empty points.)

    Taken from the **branch's**, it was still wrong in the way a reader would
    act on: **11 of the scouted team's 26 round-type groups** hold no round
    from that team's newest match, and each marked an older one as the newest.
    The sharpest is a one-round eco block whose only match is the **oldest**,
    three weeks and three matches behind.

    The map is the level the reader asks at, because the report is read per
    map: inside a Nuke chapter, "the newest" is the most recent Nuke demo. It
    also removes a contradiction the narrower scopes produced inside one
    chapter: the scouted team's newest match has **no ``de_dust2`` demo at
    all**, so every Dust2 block was being marked against a match never played
    on that map, and rows a few lines apart disagreed about it.

    The consequence is deliberate and is the point: a block holding nothing
    from the map's newest match reads "not in the newest" on **every** line,
    which says the whole block is stale -- exactly what a reader preparing
    for a match needs to know. Measured after the change: **4** of those 26
    groups read that way, against 11 under the branch scope.

    ``matches_m`` stays the **sample point's**, because it is the denominator
    beside ``m`` and has to be measured over the same rounds. So a row can
    read ``2/2 ottelussa, ei uusimmassa``: in both matches this moment exists
    in, and the map's newest is not one of them.

    Args:
        ticks: The branch's sample point rows.
        round_keys: The branch's rounds.
        newest: The map's newest match, or ``None`` when the matches' order
            does not settle it (:func:`newest_match`).
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
        matches = matches_of(rows_by_round.keys())
        positions.append(
            Position(
                sample_kind=kind,
                seconds=seconds,
                seconds_median=(
                    round(median(contact_times), 3) if contact_times else None
                ),
                m=len(rows_by_round),
                matches_m=len(matches),
                rounds_missing=total_rounds - len(rows_by_round),
                areas=area_distributions(rows_by_round, newest),
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


def record_for(rows: Sequence[Mapping[str, Any]]) -> RoundRecord:
    """The win-loss record of one group, counted from its own rows.

    **The argument is the same sequence the group's sample is counted from**,
    and that is the whole design of this function: it takes rows rather than
    a filter, so there is no second pass over a differently filtered frame
    for the record to drift against, and
    ``RoundTypeReport._check_record_covers_the_rounds`` refuses the drift
    if one is ever introduced.

    The observation is ``won`` as ``classify`` stored it -- the subject
    lineup's own row from the parsed rounds table, copied across unchanged
    (``stages.classify``), so it means "this team won this round" and nothing
    narrower.

    **An empty ``won`` goes to ``unknown`` and never to ``losses``.** The
    column is ``pl.Boolean`` and therefore nullable, so an unread outcome is
    a real state of the table; counting it as a loss would turn a gap in the
    recording into a claim about the team. A **missing** ``won`` key is
    treated the same way, and that is a deliberate floor rather than a
    silence: ``stages.aggregate._read_classified`` validates every frame
    against the ``CLASSIFIED`` schema before it gets here, so the key cannot
    be absent on the pipeline's path -- and if it ever were, the report would
    say that no outcome was known instead of inventing defeats.
    """
    wins = 0
    losses = 0
    unknown = 0
    for row in rows:
        value = row.get("won")
        if value is None:
            unknown += 1
        elif bool(value):
            wins += 1
        else:
            losses += 1
    return RoundRecord(wins=wins, losses=losses, unknown=unknown)


#: What the archive's sample points really look like, measured 2026-09-25 and
#: written down **once** (Story 4.11).
#:
#: Every claim the route rests on about the sampling is here, and no other
#: docstring restates a number from it -- they name this constant instead. A
#: measurement copied into two docstrings is the shape that left a schema
#: count wrong four stories running, and a docstring is exactly where a stale
#: copy is least visible.
#:
#: The archive is seventeen demos, all parsed on the fourteen-point grid
#: since Story 4.5 re-parsed it (re-measured 2026-09-25). Over all of them:
#:
#: * **4 735 time sample points**, of which **4 721 carry ten rows** -- one
#:   per player of both teams, alive or dead -- and **fourteen carry nine**.
#: * Those fourteen are **all fourteen points of one round**
#:   (``anubis_vs_RCAVE_VETERANS`` round 19), and the player missing from it
#:   has rows in **every other round of that demo, before it and after it**.
#:   A missing row is therefore not a player who left the server, and not
#:   evidence about the round either. That round is a ``full`` buy, so no
#:   route is ever built from it.
#: * **293 rounds reach the last point of the grid, and 285 of them -- 97.3
#:   per cent -- have a death recorded after it.** The grid ends; the round
#:   does not. This is the measurement that forbids reading the end of a
#:   route row as the end of the round. (The figure recorded before the
#:   re-parse was 280; re-run on the pre-re-parse backup, the same count
#:   gives 285 there too, so the difference is the earlier count's and not
#:   the grid's -- the last point, 45 s, is on both grids.)
#: * **2 513 death rows, none missing ``t_s``** (:func:`_route_deaths`).
#:
#: It is an observation and not a rule: importing a demo changes every
#: number here legitimately, and the answer is then to measure again and say
#: so in the commit.
ROUTE_SAMPLING_MEASURED = "2026-09-25"


def _route_observations(
    ticks: Sequence[Mapping[str, Any]],
    keys: set[RoundKey],
    route_labels: frozenset[str] | None = None,
) -> tuple[
    dict[RoundKey, list[float]],
    dict[RoundKey, set[str]],
    dict[RoundKey, dict[str, dict[float, str | None]]],
]:
    """One pass over the sample points, for the three things a route needs.

    **Which moments a round was sampled at comes from the rows themselves**,
    and that is the whole reason this walks the table instead of taking the
    sampling grid as an argument. ``parse`` creates a time sample point only
    while the round is still running (:func:`~pappascout.domain.sampling
    .sample_ticks`: *"there are no points after the round ended"*), and it
    writes a row there for **every** player, alive or dead.

    :data:`ROUTE_SAMPLING_MEASURED` is the measurement behind that, and it is
    stated in one place because a number restated is a number that goes
    stale. Its one exception is worth reading before trusting the rule: the
    archive's sample points that carry nine rows are all the points of a
    single round, from which one player is missing **who has rows
    in every other round of that demo, before and after**. So a missing row
    is not a player who left the server, and this function must not read a
    player's absence as the round's.

    **A moment with no row at all is a moment this round was not sampled
    at.** It is emphatically *not* "a moment the round did not reach": the
    grid stops at its last point while the round runs on, and
    :data:`ROUTE_SAMPLING_MEASURED` shows how often. What the route may say
    about such a moment is nothing, which is what an empty ``steps`` says.

    The rows are the team's own (the ``aggregate`` stage filters on
    ``lineup_key``), so strictly this reads "no player **of the scouted
    team** was sampled". The two differ only if all five of the team's
    players are missing from a point the opponent still has, which the
    measurement does not show and which would in any case end the row rather
    than claim anything.

    **A moment outside ``route_labels`` is treated exactly as a moment the
    round was not sampled at** (Story 4.5), which is what makes the route
    read only the points the report prints. The grid is a dense internal
    series; a route read from all of it would be a fourteen-step row, and the
    product owner ruled on 2026-09-25 that the report cannot carry that many
    sample points. Skipping the rows **before** anything is built is what
    makes this exact: the route is then the one a parse at those points alone
    would have produced, because no point's rows depend on any other point's.

    The points are matched **as labels** (:func:`~pappascout.constants
    .seconds_label`), Story 2.13's rule for naming one second across layers:
    the renderer decides by label which sample-point section it prints, so a
    float comparison here could print a route through a point whose section
    is hidden (``9.0000001`` is the label ``9``).

    Args:
        ticks: The branch's sample point rows, both sample kinds.
        keys: The rounds to read. Others are skipped.
        route_labels: The labels of the points the route reads
            (``[aggregate].route_sample_seconds``). ``None`` reads every
            moment the rows carry.

    Returns:
        Three mappings from round key: the moments the round reached in
        ascending order, the players who have any row on it, and, per player,
        the area they were observed alive in at each moment. A player is in
        the second and not in the third exactly when they were dead or
        unsampled at every moment.
    """
    points: defaultdict[RoundKey, set[float]] = defaultdict(set)
    players: defaultdict[RoundKey, set[str]] = defaultdict(set)
    at: defaultdict[RoundKey, dict[str, dict[float, str | None]]] = defaultdict(
        dict
    )
    for row in ticks:
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        if str(row["sample_kind"]) != sampling.TIME_SAMPLE:
            continue
        seconds = _sample_seconds(row)
        if (
            route_labels is not None
            and seconds_label(seconds) not in route_labels
        ):
            continue
        player = str(row["player_id"])
        points[key].add(seconds)
        players[key].add(player)
        if bool(row["is_alive"]):
            at[key].setdefault(player, {})[seconds] = _observed_area(row["area"])
    return (
        {key: sorted(value) for key, value in points.items()},
        dict(players),
        dict(at),
    )


def _route_deaths(
    deaths: Sequence[Mapping[str, Any]],
    keys: set[RoundKey],
    lineup_keys: Iterable[str],
) -> dict[RoundKey, dict[str, tuple[str | None, float]]]:
    """Per round, where and when each of the team's players was killed.

    **The route states a death only from this table** (Story 4.11's frozen
    Intent): *"nobody went to X"*, *"nobody was alive to go anywhere"* and
    *"the round was already over"* are three claims, and a missing sample
    point tells them apart from none of them. The first mock inferred the
    second from an absence and printed ``4 kuoli`` for a round that had been
    won.

    **A row whose ``t_s`` is missing or negative does not produce a death
    step**, and the player then reaches the route unaccounted for. The
    report's form states the moment (*"kuoli Mini (27 s)"*), and neither of
    those is a moment in the round: there is nothing to print for the first,
    and the second would put a death before the freezetime ended. Both
    understate -- the archive does know that player died -- and they
    understate in the direction the story asks for, which is to claim less
    than the data supports rather than more. Neither is a case the archive
    meets: see :data:`ROUTE_SAMPLING_MEASURED` for the death rows, and a
    negative ``t_s`` has never been observed either.

    **Skipped here rather than refused**, which is a deliberate difference
    from :meth:`~pappascout.domain.report.RouteStep._check_the_moment`. That
    validator rejects a negative second because a **built** step must carry a
    real moment; this reader meets the raw table, where one bad row would
    otherwise take the whole report down with a ``pydantic`` traceback
    instead of a report missing one claim.

    Args:
        deaths: The ``DEATHS`` rows as the stage filtered them -- on the
            victim **or** the attacker, so most rows here are not own deaths.
        keys: The rounds to read.
        lineup_keys: The team's lineup ids, against which a row is our own
            death.

    Returns:
        Round key -> player id -> ``(area, seconds)``. The **earliest**
        usable row per player: a player dies once in a round, so a second row
        would be a broken table rather than a second death, and taking the
        earliest is what :func:`deaths_for` does for the round's first death.
    """
    own = set(lineup_keys)
    died: defaultdict[RoundKey, dict[str, tuple[str | None, float]]] = (
        defaultdict(dict)
    )
    for row in deaths:
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        if row["victim_lineup_key"] not in own or row["t_s"] is None:
            continue
        player = str(row["victim_id"])
        moment = float(row["t_s"])
        if moment < 0:
            continue
        current = died[key].get(player)
        if current is None or moment < current[1]:
            died[key][player] = (_observed_area(row["victim_area"]), moment)
    return dict(died)


def _route_steps(
    group: Sequence[str],
    points: Sequence[float],
    at: Mapping[str, Mapping[float, str | None]],
    died: Mapping[str, tuple[str | None, float]],
) -> list[RouteStep]:
    """What a group of players became at ``points[0]``, and onwards.

    The traversal the product owner's form is read from: a group that stays
    together is **one** part and grows the chain, and only where it divides do
    the parts become rows of their own. That reading is the renderer's
    (:func:`~pappascout.render.view._route_rows`); what this builds is the
    tree it reads -- which part held how many players, and where each part
    went next.

    **Every player of the group lands in exactly one part**, which is what
    :meth:`~pappascout.domain.report.RouteStep
    ._check_the_group_divides_into_itself` then holds the tree to. There are
    three parts and no more:

    * observed alive at this moment -> grouped by area, ``fate="seen"``, and
      followed into the remaining moments;
    * not observed, and a death record places them **at or before this
      moment** -> ``fate="died"``, one part per ``(area, moment)``;
    * not observed and nothing accounts for them -> one ``fate="gone"``
      part, followed into the remaining moments **only if the sample has a
      position for one of them there**.

    **A death is used only when it happened by this moment**, and that
    condition is not decoration. Most of the archive's deaths are after the
    grid ends -- see :data:`ROUTE_SAMPLING_MEASURED` -- and what keeps them
    off the row today is that their players are still observed at every
    point. Without the condition, a player merely **missing** a row would be
    reported as having died at a second the round had not reached, which is
    a fate stated about a moment in the future.

    **A ``gone`` part that is observed again is followed**, and this is the
    asymmetry with ``died``: a killed player cannot come back, an unsampled
    one can. Without it a player whose rows begin after the first moment was
    reported lost and **every position the archive held for them was
    discarded** -- the report claiming a loss it could itself disprove. It
    does not repeat itself on the ordinary path: a player the sample really
    lost has nothing later, so the branch ends there, which is what the
    first mock's "a player who is gone is gone" rule got right.

    **An empty ``points`` returns no steps**, and that is the claim none of
    the three parts makes: there is no later moment in the sample, so the
    branch ends and says nothing at all about the time after it. It does not
    mean the round ended -- see :data:`ROUTE_SAMPLING_MEASURED`.

    The order inside a moment is **the biggest part first, then the area's
    own name**, with the unnamed area last (:func:`_area_sort_key`); the
    ``died`` parts break a tie on the **moment** before the area, so two
    deaths in one area come out in the order they happened. It is
    deliberately not the order the rows happened to arrive in: that is the
    order of the parquet file's players, which is arbitrary and would still
    be arbitrary after somebody re-parsed the demo.

    **Two deaths in one area whose seconds differ but round to the same
    number stay two parts** and print the same sentence twice. The grouping
    is on the measured moment, because this layer does not know how the
    moment will be spelled; an exact tie does merge. The boundary is
    recorded rather than removed -- moving it would put a rendering decision
    into ``domain``.
    """
    if not points:
        return []
    moment, rest = points[0], points[1:]
    seen: defaultdict[str | None, list[str]] = defaultdict(list)
    gone: list[str] = []
    killed: defaultdict[tuple[str | None, float], list[str]] = defaultdict(list)
    for player in group:
        where = at.get(player, {})
        if moment in where:
            seen[where[moment]].append(player)
        elif player in died and died[player][1] <= moment:
            killed[died[player]].append(player)
        else:
            gone.append(player)

    steps = [
        RouteStep(
            fate="seen",
            seconds=moment,
            area=area,
            players=len(members),
            steps=_route_steps(members, rest, at, died),
        )
        for area, members in sorted(
            seen.items(), key=lambda kv: (-len(kv[1]), _area_sort_key(kv[0]))
        )
    ]
    steps.extend(
        RouteStep(
            fate="died",
            seconds=seconds,
            area=area,
            players=len(members),
        )
        for (area, seconds), members in sorted(
            killed.items(),
            key=lambda kv: (-len(kv[1]), kv[0][1], _area_sort_key(kv[0][0])),
        )
    )
    if gone:
        # Followed on only where the sample really has them again. Recursing
        # unconditionally would write "poistui otannasta" once per remaining
        # moment for every player the sample lost for good, which is the same
        # absence said four times.
        returns = any(
            later in at.get(player, {}) for player in gone for later in rest
        )
        steps.append(
            RouteStep(
                fate="gone",
                seconds=moment,
                area=None,
                players=len(gone),
                steps=_route_steps(gone, rest, at, died) if returns else [],
            )
        )
    return steps


def routes_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    deaths: Sequence[Mapping[str, Any]],
    lineup_keys: Iterable[str],
    demo_order: Mapping[str, int],
    route_seconds: Collection[float] | None = None,
) -> list[RoundRoute]:
    """One round's route per round of the group, **newest match first**.

    The story's whole output (Story 4.11). The report has always said where a
    team's players *were* and never which way they *came*; a route is a
    sequence, and this is the same rows the sample points are counted from,
    **grouped by player instead of by area**.

    **Only** :data:`~pappascout.domain.report.ROUTE_ROUND_TYPE` **reaches
    this function**, and the caller decides that rather than this function,
    because the model already refuses routes on any other type: two places
    asking the same question would be the second copy this codebase keeps
    removing.

    **A row for every round, including one with no route.** A round settled
    inside the first sample point produces a :class:`RoundRoute` with no
    steps, which the report states; dropping it would leave the block's
    heading counting a round the reader is never shown.

    Args:
        rows: The group's own ``CLASSIFIED`` rows -- **the same sequence**
            :func:`sample_for` and :func:`record_for` are handed, which is
            what makes one row per round an identity rather than a join
            (:meth:`~pappascout.domain.report.RoundTypeReport
            ._check_routes_are_the_round_types_own_rounds`).
        ticks: The map's sample point rows, filtered to the team's lineups by
            the stage. Rows of other rounds are skipped here.
        deaths: The map's ``DEATHS`` rows, filtered on victim **or**
            attacker. Splitting out our own deaths is :func:`_route_deaths`'s
            job, as it is :func:`deaths_for`'s.
        lineup_keys: The team's lineup ids.
        demo_order: ``map_demo_id`` -> place, newest first. **The report's own
            recency order and not a second sort**: the caller builds it from
            :func:`played_maps_for`'s result, so the round the block lists
            first belongs to the demo the map's own list puts first. A demo
            the order does not place sorts last, where its row carries no
            date to contradict -- :func:`played_maps_for`'s rule, and its
            reason.
        route_seconds: The time sample points the route reads
            (``[aggregate].route_sample_seconds``, which the settings hold
            equal to the points the report prints); see
            :func:`_route_observations`. ``None``, the default, reads every
            point the rows carry, which is what a direct caller with a
            four-point table wants.

    Returns:
        One :class:`~pappascout.domain.report.RoundRoute` per row in ``rows``,
        newest match first and, inside a demo, in round order.
    """
    # A dict and not a set, although only membership is asked of it below.
    # The routes are built in **this** order and then sorted, and a set of
    # tuples iterates in hash order: measured 2026-09-25, dropping
    # ``round_no`` from the sort below still passed 11 runs in 12, because
    # the pre-sort order rode on Python's string hash randomisation. An
    # insertion-ordered mapping makes the product deterministic and the sort
    # the only thing deciding the order, which is what the test can then pin.
    outcome: dict[RoundKey, bool | None] = {}
    for row in rows:
        key = _round_key(row)
        if key is None:
            # The same broken table ``_round_types_for`` refuses, and it
            # refuses it before this is ever called; the guard is here so the
            # function is safe to call on its own.
            raise AggregateError(
                f"The classified table {row['map_demo_id']!r} holds a row "
                "without a round number, so its route cannot be joined to "
                "the sample points.\n"
                f"Run the classification again: uv run pappascout classify "
                f"{row['map_demo_id']} --force"
            )
        value = row.get("won")
        outcome[key] = None if value is None else bool(value)

    keys = set(outcome)
    points, players, at = _route_observations(
        ticks,
        keys,
        None
        if route_seconds is None
        else frozenset(seconds_label(value) for value in route_seconds),
    )
    died = _route_deaths(deaths, keys, lineup_keys)
    # Past every place in use, so a demo the order does not place sorts after
    # every placed one.
    #
    # **Not ``len(demo_order)``**, and the reason is this function's argument
    # rather than ``played_maps_for``'s duplicate-match hazard, which cannot
    # arise here: ``build_report`` builds the mapping with ``enumerate``, so
    # its places are 0..n-1 and ``max + 1`` and ``len`` agree. What does not
    # agree is a mapping a **direct** caller hands in -- ``demo_order`` is a
    # plain mapping and nothing constrains its values -- and there ``len``
    # would be a place already in use, tying an unplaced demo with a placed
    # one.
    unplaced = max(demo_order.values(), default=-1) + 1
    routes = [
        RoundRoute(
            map_demo_id=demo,
            round_no=round_no,
            won=outcome[(demo, round_no)],
            steps=_route_steps(
                sorted(players.get((demo, round_no), ())),
                points.get((demo, round_no), []),
                at.get((demo, round_no), {}),
                died.get((demo, round_no), {}),
            ),
        )
        for demo, round_no in outcome
    ]
    routes.sort(
        key=lambda route: (
            demo_order.get(route.map_demo_id, unplaced),
            route.map_demo_id,
            route.round_no,
        )
    )
    return routes


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
    newest: str | None,
) -> list[FirstContactArea]:
    """The areas the team had a player in at the moment of first contact.

    The observation is **presence**: the same round produces an observation
    for every area in which the team had a living player. ``Σ n = m``
    therefore does not hold and is not meant to -- the full distribution from
    the same moment is in the ``positions`` list's first-contact sample point.

    The match counts are taken from the **same** ``set`` of round keys the
    rounds are counted from, in the same pass; there is no second walk over
    the ticks for them to differ from. The denominator ``matches_m`` is the
    matches of the rounds that have a first-contact sample, and the recency
    mark is the **map's** newest match, for the reason :func:`positions_for`
    sets out.

    Args:
        ticks: The branch's sample point rows.
        round_keys: The branch's rounds.
        newest: The map's newest match, or ``None``.
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
    sampled_matches = matches_of(rounds_with_sample)
    areas = [
        FirstContactArea(
            area=area,
            n=len(rounds),
            m=m,
            matches=len(matches_of(rounds)),
            matches_m=len(sampled_matches),
            newest=(None if newest is None else newest in matches_of(rounds)),
        )
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

    **The grouping key is ``(map, side, area)`` for every rule**, plus the
    site group on a stack. ``ct_advance`` was grouped by round type as well
    until Story 4.5; the product owner ruled that an eco and a force push
    into one area are *"sama tapa"*, so it now gathers every save type and
    lists them. Splitting by type would give one habit two rows and two
    denominators, and the reader would not see the total -- that is, the
    figure would reproduce inside itself the very scatter it was made to
    remove.

    ``n`` is the number of **rounds** on which the hit was observed: the same
    area on two eco rounds is one row with a sample of ``2/m``, not two rows,
    and the same round with two sample points does not raise ``n`` to two.
    ``m`` is all of the grouping level's rounds -- for the advance the
    side's save rounds, for crunch and stack all the side's rounds.

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
        thresholds: The ``[thresholds]`` section. Ten anomaly thresholds and
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

    # The demo's sampling grid **once** per demo: how many time sample points
    # it was parsed at (Story 4.6). The orientation's observation gate is a
    # count per sample point, so the rules need the divisor -- and it is
    # derived from the rows, never read from ``[parse].snapshot_seconds``
    # (``domain.sampling.sample_point_count`` says why).
    #
    # **Per demo and over all its rounds, not per round.** A round settled in
    # 30 seconds has fewer points than the grid has, and the orientation the
    # gate is applied to was counted over the whole demo; a per-round divisor
    # would lower the bound exactly on the short rounds.
    #
    # The lineup filter on ``ticks`` does not touch this: the grid is a
    # property of the parse, and both teams' rows lie on the same points.
    #
    # The rows are turned into presences twice -- here and again per round in
    # ``_rule_hits`` -- and that is a deliberate trade: the alternative is to
    # count the distinct seconds here by hand, which would be a second copy of
    # the definition that ``sample_point_count`` holds. Counted 2026-09-23,
    # not estimated: the eight calibration demos are 11 105 sample point rows
    # and the whole archive of seventeen is 19 805, so the second pass costs a
    # fraction of a second. (The figure here said "some thirty thousand" until
    # a reviewer counted; the conclusion held, the number did not.)
    presences_by_demo: defaultdict[str, list[sampling.AreaPresence]] = defaultdict(
        list
    )
    for key, tick_rows in ticks_by_round.items():
        presences_by_demo[key[0]].extend(_presence(tick) for tick in tick_rows)
    points_by_demo: dict[str, int] = {
        demo: sampling.sample_point_count(presences_by_demo.get(demo, ()))
        for demo in {str(row["map_demo_id"]) for row in rows}
    }

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
            floor_band_trim=thresholds.site_floor_band_trim,
            floor_gap_ratio=thresholds.site_floor_gap_ratio,
            floor_z_weight=thresholds.site_floor_z_weight,
            bridge_void_share=thresholds.site_bridge_void_share,
        )

    # (map, side) -> round type -> round rows. One split, from which both the
    # advance's denominator (the save types together) and crunch's and
    # stack's (every type) can be read.
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
        rows,
        ticks_by_round,
        area_orientation,
        groups_by_demo,
        points_by_demo,
        thresholds,
    )

    anomalies: list[Anomaly] = []
    for map_name, side in sorted(branches):
        by_type = branches[(map_name, side)]
        side_rows = [row for type_rows in by_type.values() for row in type_rows]
        source = map_sources.get(map_name, "unknown")
        # The advance first, over **all the side's save rounds at once**
        # (Story 4.5). It was grouped per round type until the product owner
        # ruled, 2026-09-25, that an eco push and a force push into the same
        # area in two matches are *"sama tapa"* -- one habit. Split by type,
        # his own example (Nuke Lobby, eco in one match and force in another)
        # was two one-match rows and neither reached the report. The round
        # types are still on the row (``Anomaly.round_types``); they no longer
        # divide it, and the denominator is the save rounds the rule can see.
        save_rows = [
            row
            for round_type in SAVING_ROUND_TYPES
            for row in by_type.get(round_type, ())
        ]
        if save_rows:
            anomalies.extend(
                _grouped_anomalies(
                    save_rows,
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
    # neither scopes by round type: a silenced demo's (the sites separate on
    # neither axis) CT rounds are in crunch's denominator but not in stack's.
    # Without a figure of its own, those rounds would look examined with a
    # nil result. Nuke used to be the worked example here -- 27 rounds -- and
    # since Story 4.3 it is divided by height instead, so the archive has no
    # silenced demo left. The two denominators still differ in kind.
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
            min_observations_per_point=(
                thresholds.advance_area_min_observations_per_point
            ),
            sample_points=points_by_demo[demo],
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
    points_by_demo: Mapping[str, int],
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

    **The sampling grid's size is per demo and comes in the same shape as the
    orientation** (Story 4.6): ``points_by_demo`` is derived once by the
    caller from the demo's own rows. It cannot be derived here, because here
    the rows are one round's -- and a round settled early holds fewer points
    than the grid has, so the orientation's gate would sink with the round's
    length.
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
            area_min_observations_per_point=(
                thresholds.advance_area_min_observations_per_point
            ),
            sample_points=points_by_demo[demo],
            max_sample_s=thresholds.advance_max_sample_s,
            min_players=thresholds.advance_min_players,
        ) + sampling.crunch_hits(
            presences,
            orientation=orientation,
            t_share_min=thresholds.advance_t_share,
            area_min_observations_per_point=(
                thresholds.advance_area_min_observations_per_point
            ),
            sample_points=points_by_demo[demo],
            max_sample_s=thresholds.advance_max_sample_s,
            lookback_s=thresholds.crunch_lookback_s,
            min_players=thresholds.crunch_min_players,
            min_sources=thresholds.crunch_min_sources,
        ) + sampling.stack_hits(
            presences,
            # ``None`` (the sites do not separate) silences the rule, and
            # that is a different thing from a missing key: a missing key
            # raises an error already in ``anomalies_for``, because it would
            # mean the caller left the demo out.
            groups=groups_by_demo[demo],
            # The stack reads ONE sample point and no longer shares
            # advance_max_sample_s (Story 4.4). The shared bound exists so
            # that the rules agree about when the start of the round ends,
            # and the other two still share it: they ask about MOVEMENT,
            # which has only a ceiling. A setup is a MOMENT and has both a
            # floor and a ceiling, so the sharing is replaced deliberately
            # and not dropped by accident.
            sample_s=thresholds.stack_sample_s,
            max_areas=thresholds.stack_max_areas,
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
    advance the side's save rounds, for crunch all of the side's rounds. The
    same function serves both, because the difference is **only** in which
    rows are given.
    """
    m = len(branch_rows)
    # The key is the area AND the site group, not the area alone. On the two
    # orientation rules the group is always ``None`` and the key is therefore
    # the area, exactly as before. On the stack the area is no longer the
    # group's own site (Story 4.4) but the area the crowd stands on, so the
    # SAME area CAN be read into a different group by two demos of the same
    # map: the division is derived per demo by design (AD-13) and nothing
    # makes it agree across demos. Keyed by the area alone, such a row would
    # name one group for observations made in two, and ``_AnomalyTally.add``
    # would let the last hit decide which.
    #
    # **Possible and not measured.** Checked over all three multi-demo maps in
    # the archive, no area is read into different A/B groups by two of a map's
    # demos; what differs is grouped vs. ungrouped (Anubis ``Middle``, Nuke
    # ``Control``, ``Garage``), and an ungrouped area produces no hit. This is
    # defence against a state the derivation allows, not a repair of one it
    # produced.
    #
    # **The area and not the SET of areas, and that is a decision.** Keyed by
    # the set, two rounds of the same two-area habit whose majority alternates
    # would stay one row; keyed by the area, they become two rows of n=1.
    # Keyed by the set, the opposite pair fragments instead: the same crowd on
    # ``Outside`` alone on one round and ``Outside`` + ``Hut`` on the next
    # would be two rows where the area keeps them one. Neither case occurs in
    # this archive (all five rows are n=1 and no two share a map, a side and a
    # group), so nothing measured decides it. The area keeps the report's
    # identity where the rest of the chapter already has it -- (rule, map,
    # side, area), the same key Report._check_anomalies enforces -- and the
    # round rows name their own areas either way, so no observation is lost.
    # The consequence is pinned in test_aggregate.py rather than left to be
    # discovered.
    tally: defaultdict[tuple[str, str | None], _AnomalyTally] = defaultdict(
        _AnomalyTally
    )
    for row in branch_rows:
        key = _round_key(row)
        if key is None:
            continue
        for hit in hits_by_round.get(key, ()):
            if hit.rule != rule:
                continue
            tally[(hit.area, hit.site)].add(key, str(row["round_type"]), hit)

    return [
        tally[(area, site)].to_anomaly(
            rule=rule,
            area=area,
            map_name=map_name,
            map_name_source=map_name_source,
            side=side,
            m=m,
            small_sample=m < thresholds.small_sample_rounds,
        )
        for area, site in sorted(tally)
    ]


@dataclass
class _RoundTally:
    """One round's tally: the sample points with their observations.

    **Nothing here is simultaneous across two sample points**, the source
    directions included. They were once collected per round on the belief
    that a round was the unit of simultaneity; at fourteen sample points one
    round entered the same area twice from different directions and the union
    -- four directions, three players -- failed the model (Story 4.5). So the
    directions are kept per sample point, beside the player count, for the
    reason the next paragraph gives for the count.

    **The player count is not simultaneous even inside a round.** One maximum
    and a list of sample points set the maximum against every point in the
    report -- measured, MatureMayhem Inferno round 2 has five players at 15 s
    and one at 30 s, but the row read "5 players at 15 and 30 s". So the
    figure is on the sample point's own row and not in the round's summary.
    """

    round_type: str
    #: Sample point -> (players, alive, the crowd's areas, the sources). A
    #: dictionary and not a list: the same rule produces at most one hit per
    #: sample point for one area, so the key is unique and the order comes
    #: from the sort. The areas are the stack's own and the sources the
    #: crunch's, each empty on the other rules -- and both are kept per sample
    #: point for the same reason as the player count: they are an observation
    #: of that moment and of no other.
    points: dict[
        float, tuple[int, int | None, tuple[str, ...], tuple[str, ...]]
    ] = field(default_factory=dict)

    def add(self, hit: sampling.AnomalyHit) -> None:
        self.points[hit.sample_t_s] = (
            hit.players,
            hit.alive,
            hit.areas,
            tuple(hit.sources),
        )

    @property
    def players_max(self) -> int:
        """The largest player count on the round; the source of the row's
        ``players_max``."""
        return max(players for players, _, _, _ in self.points.values())


@dataclass
class _AnomalyTally:
    """One area's tally at one grouping level.

    Per-round bookkeeping and not one set: the report's row says how often,
    from where and how many, and the last two are true only inside a round
    -- inside one sample point of it, since Story 4.5. It no longer says when
    (the seconds are a tool, not the report's content).
    """

    rounds: dict[RoundKey, _RoundTally] = field(default_factory=dict)
    #: Demo -> the area's orientation in that demo. A map can come from two
    #: demos (Story 2.11), and their T shares can differ -- one figure would
    #: force choosing a mean that was never observed. **Empty for stack**: the
    #: rule does not read the orientation, so it has none to record.
    orientation: dict[str, tuple[float, int]] = field(default_factory=dict)
    #: The site's group. Only for stack; every hit tallied here is from the
    #: same group, because the group is **part of the key** -- since Story 4.4
    #: the area is the crowd's own and no longer the group's site, so the area
    #: alone no longer determines it.
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
                        sample_t_s=seconds,
                        players=players,
                        alive=alive,
                        areas=list(areas),
                        sources=sorted(sources),
                    )
                    for seconds, (players, alive, areas, sources) in sorted(
                        entry.points.items()
                    )
                ],
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
    match_order: Sequence[str],
    match_facts: Mapping[str, MatchFact],
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
        match_order: The matches whose place in time is known, **newest
            first** (Story 4.9). Only the order is passed and not the times:
            ``domain`` reads no file (AD-2), and a match's time is in the
            match index, which ``discover`` writes and the stage reads.

            The argument is **mandatory and has no default**, for the reason
            ``map_names`` and ``area_orientation`` are: an empty default would
            quietly turn the whole recency mark off, and a report with no
            marks looks exactly like an archive whose matches have no order.
            An **empty sequence** is a legitimate value and says that -- it is
            what an archive with no match index gives.

            A match missing from the list has no known place, which is a
            state and not a fault: a demo imported by hand is in no match
            index. Then a group of more than one match carries ``null``
            instead of a mark (:func:`newest_match`).
        match_facts: Match key -> the date and the opponent the archive's
            match index holds for it (Story 4.10). A **key that is missing**
            says the index does not hold that match, which is what every
            hand-imported demo is.

            **Mandatory and without a default**, for the reason ``map_names``
            and ``match_order`` are and with a sharper edge: an empty default
            would print "not in the match index" on every row of every
            report, which is a **claim about the archive** and not a missing
            formatting. An empty mapping is still a legitimate value and says
            exactly that -- it is what an archive with no match index gives.

            Separate from ``match_order`` although both come out of the same
            file and the same read, because they answer different questions
            and fail apart: a match with no finish time is in the facts and
            not in the order.
        generated_at: The moment of the run.
        tool_versions: The tool versions, for the report's own field.
        missing_demos: The matches whose data was not there.

    Returns:
        A validated :class:`Report`. If the sample does not match at some
        level, the model itself raises
        :class:`~pappascout.errors.AggregateError`.
    """
    # Place, not time: every reader of it asks "which of these is the newest",
    # and a place answers that without this module owning a clock.
    order = {match: rank for rank, match in enumerate(match_order)}
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
    # ``played_maps`` says where they came from -- and, since Story 4.10,
    # when each was played and against whom.
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
        played_maps = played_maps_for(demos, order, match_facts)
        maps.append(
            MapReport(
                map_name=map_name,
                map_name_source=source,
                played_maps=played_maps,
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
                    # The map's newest match, decided once for the
                    # whole chapter: see positions_for.
                    newest_match(
                        {match_of(demo) for demo in demos}, order
                    ),
                    # The pistol routes' order, **read off the list the map
                    # chapter prints** rather than sorted again from
                    # ``order``: the block's first round then belongs to the
                    # demo the map's own list puts first, and the two cannot
                    # come to disagree about which match is the newest
                    # (Story 4.11).
                    {
                        entry.map_demo_id: place
                        for place, entry in enumerate(played_maps)
                    },
                    # The pistol routes read only these points (Story 4.5):
                    # this stage's own section, which the settings hold equal
                    # to the points the report prints. Only the routes read
                    # it -- the sample-point sections keep every point in
                    # report.json and the renderer decides what is printed,
                    # while a route is a chain built from the points it reads
                    # and cannot be shortened afterwards without merging
                    # groups that divided only at a dropped moment.
                    aggregate.route_sample_seconds,
                ),
            )
        )
    # The maps with the most ROUNDS first; on a tie the name, so that the
    # order is the same from one run to the next.
    #
    # "The most played maps first" is what this said, and Story 4.10 made the
    # imprecision matter: the summary's map-pool row prints each map's
    # **demo** count in this order, and rounds and demos do not agree.
    # Measured 2026-09-25 on the real archive -- a map of 1 demo and 28 rounds
    # precedes one of 1 demo and 22 rounds, and four maps of one demo each
    # come out in round order and not alphabetically. The sort stays on
    # rounds, because the chapter's size is what a table of contents should
    # follow; both texts now name the key rather than describing it.
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
    newest: str | None,
    demo_order: Mapping[str, int],
    route_seconds: Collection[float],
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
                    newest,
                    demo_order,
                    route_seconds,
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
    newest: str | None,
    demo_order: Mapping[str, int],
    route_seconds: Collection[float],
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
                # From ``type_rows``, the same sequence ``sample_for`` was
                # just handed. Not ``rows`` and not a fresh filter: the model
                # checks that the record covers the sample, and the cheapest
                # way to keep that true is to give both the same rows.
                record=record_for(type_rows),
                small_sample=sample.rounds < thresholds.small_sample_rounds,
                positions=positions_for(ticks, keys, newest),
                utility=utility_uses(
                    events, keys, aggregate.utility_seconds_buckets
                ),
                utility_counts=utility_counts_for(events, keys),
                players_armed=armed_players_for(type_rows),
                players_armored=armored_players_for(type_rows, armored),
                first_contact=first_contact_areas(ticks, keys, newest),
                deaths=deaths_for(deaths, keys, lineup_keys),
                # Only the pistol type has routes (Story 4.11's scope, and
                # the model refuses them elsewhere). The condition is here
                # and not inside ``routes_for``, so the function does one
                # thing and the scope is stated where the scope is decided.
                routes=(
                    routes_for(
                        type_rows,
                        ticks,
                        deaths,
                        lineup_keys,
                        demo_order,
                        route_seconds,
                    )
                    if round_type == ROUTE_ROUND_TYPE
                    else []
                ),
            )
        )
    return reports
