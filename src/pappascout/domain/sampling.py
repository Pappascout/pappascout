"""Sample points, first contact and the anomaly rules (AD-5, AD-10).

The setup is picked at **several moments**, not as one freeze frame: 6 s (the
CT side's direction shows), 15 s (a T rush is told apart from a default), 30 s
(the setup), 45 s (the development) and **first contact**. The times are a
setting (``[parse].snapshot_seconds``), not code.

A sample point is time-based but bounded by the round
------------------------------------------------------
The seconds are converted into ticks as ``freeze_end_tick + t x tick_rate``,
and a point **is dropped if it would fall after the round ended**. This is the
only place where a round's duration affects the sampling -- and it is why
there can be a different number of points on different rounds. If a round is
settled in 30 seconds, the 45-second point does not exist and must not be
invented; aggregation takes that into account in the sample (Story 2.3).

``t_s`` is always computed from the **chosen tick** and not from the nominal
second: ``t_s = (tick - freeze_end_tick) / tick_rate``. They differ by the
rounding, and the row carries both -- ``sample_t_s`` says which sample point
the row belongs to, ``t_s`` its real moment.

First contact is a sample point of its own
------------------------------------------
It is not a point in time but an event: the first ``player_hurt`` in which the
attacker is **on the other side** and the weapon is not utility. Utility damage
is not contact -- a molotov burns around the corner and does not reveal the
setup the way the first bullet does. If no acceptable ``player_hurt`` event is
found, the fallback source is the first ``player_death`` under the same
conditions.

Three anomaly rules, three different questions
----------------------------------------------
Story 2.5 added two rules: :func:`ct_advance_hits` and :func:`crunch_hits`.
Both answer the same question -- **is the subject's CT player in an area that
the T side holds in that demo** -- and they therefore share the same
orientation computation (:func:`t_side_shares`): with two computations they
could disagree about whose area an area is.

**Neither of these two contains the other.** Crunch adds a direction
requirement to the orientation condition but **drops the round-type
restriction**, so the hit sets intersect each other: on a saving round a crunch
also produces an advance hit, on a full buy only the crunch (measured:
MatureMayhem Anubis round 10). So neither may be described as a "stricter form"
of the other.

The orientation **comes as an argument** and is not computed here. It is the
demo's own observation from an area's alive observations at the time sample
points, and it has to be computed from the **unfiltered** sample point table,
that is, from both teams' rows. This is a measured condition and not a
preference: computed on the subject's own rows every true positive disappears,
because the anomaly eats its own detection (:class:`AreaObservations`).

Story 2.14 adds a third one, :func:`stack_hits`. It **does not read the
orientation at all**: it asks whether the subject's own defence is standing
crowded together -- four or more of them on at most two areas of the same
site's group, at the setup sample point. Its derived input is
:func:`site_groups` -- a mapping ``area -> "A" | "B"`` from the demo's own
point cloud -- and it is in this same module as the rule, so that the rule
and its input cannot disagree. The same justification as with the
orientation: **no map database, no human-supplied area division, no table
accumulating across the archive**. An accumulating source would give the
same demo a different result depending on what other demos are in the
archive.

The three rules are three different questions about the same observation, and
not one of them is a stricter or a looser form of another.

Two of them must not read the sampling grid
-------------------------------------------
Story 4.6. The grid (``[parse].snapshot_seconds``) is a **tool for looking**,
so changing it must change how well a rule sees and not what the rule asks.
Two rules were written in the grid's own units and therefore did change
meaning when it was densified: the orientation's observation gate was a raw
count of rows (:func:`t_side_shares`), and the crunch's arrival was read from
"the previous sample point" (:func:`_source_areas`). Measured on the archive,
four points against fourteen and nothing else changed, the CT advance went
from 10 hits on 6 rounds to 45 on 9 and the crunch from 5 hits on 4 rounds to
2 on 2. Both are now **asked** in units the grid cannot move -- observations
**per sample point** and a look-back **in seconds** -- and the grid's own size
is derived from the rows (:func:`sample_point_count`), never from the setting.

**Asked, not answered: neither rule is density-invariant and neither is
claimed to be.** What the units remove is the part of the dependence that is
arithmetic; what is left is a property of the observation, and both residuals
are measured rather than argued about. The orientation's is in
``tests/data/orientation_by_grid.json`` (10 areas lost and 2 gained between
the two grids, and three of those are ``advance_t_share``'s doing and not the
gate's). The crunch's is in :func:`_source_areas`: the look-back can only be
honoured as nearly as the grid allows, and on the one demo that exists on
both grids 67 of 320 shared source look-ups resolve to a different area.

The stack is the counter-example that shows the rule is about units and not
about caution: it reads one named sample point (``stack_sample_s``) and its
five rounds did not move between the two grids at all.

An empty result is a **valid result** and not a shortfall: a demo with no
anomalies in it is an observation that there were no anomalies.

The module is pure: no files, no demoparser2, no settings. That is why it can
be tested with hand-built records, and every row of the I/O matrix is one
function call away here.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from pappascout.constants import (
    ANOMALY_RULE_SIDE,
    CRUNCH,
    CT_ADVANCE,
    SAVING_ROUND_TYPES,
    SIDES,
    SITE_AREAS,
    SITE_GROUPS,
    STACK,
    is_sample_point,
    source_point_index,
)

__all__ = [
    "RoundBounds",
    "SamplePoint",
    "DamageEvent",
    "TIME_SAMPLE",
    "FIRST_CONTACT_SAMPLE",
    "normalize_weapon",
    "normalize_area",
    "seconds_since_freeze_end",
    "sample_ticks",
    "first_contact_tick",
    "SITE_AREAS",
    "SITE_GROUPS",
    "SPAWN_AREAS",
    "AreaObservations",
    "AreaPresence",
    "AnomalyHit",
    "CloudCell",
    "sample_point_count",
    "t_side_shares",
    "site_groups",
    "ct_advance_hits",
    "crunch_hits",
    "stack_hits",
]

#: A time-based sample point.
TIME_SAMPLE = "time"
#: The moment of the first cross-side hit.
FIRST_CONTACT_SAMPLE = "first_contact"

# Both have to be in the SAMPLE_KINDS list. The correspondence is not checked
# with a module-level assert here -- that would vanish under python -O exactly
# when it was needed. The check is in the test test_sampling.py.


@dataclass(frozen=True)
class RoundBounds:
    """One round's bounds in ticks.

    Attributes:
        round_raw: The demo's own round number. It travels with the sample
            point so that the stage can attach a ``round_no`` to it -- the
            numbering is still owned by :mod:`pappascout.domain.rounds` alone.
        freeze_end_tick: The round's **last** ``round_freeze_end``. The same
            anchor as in the ``rounds`` table. ``None`` = there is no anchor,
            and then ``t_s`` is undefined and the round is not sampled.
        end_tick: The moment the round was settled. ``None`` = the round was
            not settled, and then its duration is unknown and points cannot be
            bounded by the round.
    """

    round_raw: int
    freeze_end_tick: int | None
    end_tick: int | None

    @property
    def is_samplable(self) -> bool:
        """Has the round both an anchor and an end, and in that order."""
        return (
            self.freeze_end_tick is not None
            and self.end_tick is not None
            and self.end_tick >= self.freeze_end_tick
        )


@dataclass(frozen=True)
class SamplePoint:
    """One moment on one round; it produces a row for every player.

    Attributes:
        round_raw: The round the point belongs to.
        tick: The demo tick the players' positions are read from.
        sample_kind: ``"time"`` or ``"first_contact"``.
        sample_t_s: The sample point's nominal time in seconds. On a time
            point the setting's number, on first contact the same as ``t_s``
            -- in either case it says which moment the row refers to.
        t_s: The real time from the anchor:
            ``(tick - freeze_end_tick) / tick_rate``.
    """

    round_raw: int
    tick: int
    sample_kind: str
    sample_t_s: float
    t_s: float


@dataclass(frozen=True)
class DamageEvent:
    """A ``player_hurt`` or ``player_death`` for inferring first contact.

    The sides have been resolved before this function: the domain knows
    neither the game's prop names nor the steamid-to-side mapping.

    Attributes:
        tick: The moment of the event.
        attacker_id: The attacker. ``None`` = the world (a fall, a bomb with
            no planter) -- not contact.
        victim_id: The victim.
        weapon: The weapon's name as the demo gives it.
        attacker_side: The attacker's side on this round.
        victim_side: The victim's side on this round.
    """

    tick: int
    attacker_id: str | None
    victim_id: str | None
    weapon: str | None
    attacker_side: str | None
    victim_side: str | None


def normalize_weapon(weapon: str | None) -> str | None:
    """A tidied weapon name for comparison.

    The demo's names are lower case (``hegrenade``, ``molotov``), but some
    sources carry the prefix ``weapon_``. The comparison is made on the
    normalised name, so that the settings file's list stays readable.
    """
    if weapon is None:
        return None
    text = str(weapon).strip().lower()
    if text.startswith("weapon_"):
        text = text[len("weapon_") :]
    return text or None


def normalize_area(value: object) -> str | None:
    """An area as an observation: an empty or whitespace-only name is ``None``.

    **One normalisation for two halves.** An anomaly rule compares an area's
    name from two different sources: the presence row's ``area`` and the
    orientation map's key. If only one of them is cleaned, ``" Lobby "`` is a
    different area in the orientation than in the presence -- and the rule
    goes quiet on that area without anything saying why. ``""``, for its part,
    would make it into the set of T-side areas and would produce an anomaly
    for a nameless area.

    ``parse`` already writes an empty ``last_place_name`` as ``null``, but the
    contract allows a string and a table written by an older version has not
    been through that rule. The function is here and not in the aggregation,
    because both the domain and the ``aggregate`` stage need it -- and of two
    copies it is exactly this pair that would diverge.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def seconds_since_freeze_end(
    tick: int, freeze_end_tick: int, tick_rate: float
) -> float:
    """``t_s``: the seconds from the round's anchor to this tick.

    The same formula as in the ``rounds`` table, so that every time in the
    pipeline has the same origin: the round's **last** ``round_freeze_end``
    tick.
    """
    _check_tick_rate(tick_rate)
    return (tick - freeze_end_tick) / tick_rate


def sample_ticks(
    segments: Iterable[RoundBounds],
    tick_rate: float,
    sample_seconds: Sequence[float],
) -> list[SamplePoint]:
    """Convert the sample point seconds into ticks inside the round bounds.

    Args:
        segments: The round bounds. A round that has no freezetime anchor or
            no end tick is skipped entirely: without an anchor ``t_s`` is
            undefined, and without an end a point cannot be bounded by the
            round.
        tick_rate: The demo's tick rate.
        sample_seconds: The sample points in seconds from the anchor. The
            order does not matter; the result is always in ascending order of
            time.

    Returns:
        The sample points in the order ``(round_raw, sample_t_s)``. **There
        are no points after the round ended**: if a round is settled in 28
        seconds, the 30- and 45-second points do not come into being, and a
        very short round produces no time point at all.

    Raises:
        ValueError: If the tick rate is not positive or some sample point is
            negative. A negative number of seconds would point inside
            freezetime, where the players have not moved yet.
    """
    _check_tick_rate(tick_rate)
    seconds_list = _unique_sorted_seconds(sample_seconds)

    points: list[SamplePoint] = []
    for bounds in segments:
        if not bounds.is_samplable:
            continue
        freeze_end = bounds.freeze_end_tick
        end = bounds.end_tick
        assert freeze_end is not None and end is not None  # is_samplable
        for seconds in seconds_list:
            tick = freeze_end + round(seconds * tick_rate)
            if tick > end:
                # The round was settled before this moment: the point does
                # not exist.
                continue
            points.append(
                SamplePoint(
                    round_raw=bounds.round_raw,
                    tick=tick,
                    sample_kind=TIME_SAMPLE,
                    sample_t_s=seconds,
                    t_s=seconds_since_freeze_end(tick, freeze_end, tick_rate),
                )
            )
    return points


def first_contact_tick(
    hurt_events: Iterable[DamageEvent],
    round_bounds: RoundBounds,
    *,
    exclude_weapons: Collection[str] = (),
    death_events: Iterable[DamageEvent] = (),
    fallback_death: bool = True,
) -> int | None:
    """The tick of the round's first cross-side hit.

    An acceptable hit meets every condition:

    * it happens inside the round's bounds (from the anchor to the end),
    * the attacker and the victim are **on different sides** -- friendly fire
      and self-damage are not contact,
    * the weapon is not utility.

    Args:
        hurt_events: The round's ``player_hurt`` events, in any order.
        round_bounds: The round's bounds. Without an anchor or an end tick
            ``None`` is returned: the moment could not be related to the
            round.
        exclude_weapons: The weapons that do not count as contact
            (``[parse].first_contact_exclude_weapons``). The comparison is
            made on the normalised name.
        death_events: The fallback source's ``player_death`` events.
        fallback_death: Whether the fallback source may be used
            (``[parse].first_contact_fallback_death``).

    Returns:
        A tick, or ``None`` if there was no contact on the round. ``None`` is
        the right answer and not an error: a round can be settled by time
        running out or by utility damage alone, and then there are no first
        contact rows.
    """
    if not round_bounds.is_samplable:
        return None
    excluded_weapons = {
        w
        for w in (normalize_weapon(name) for name in exclude_weapons)
        if w is not None
    }

    hit = _first_matching(hurt_events, round_bounds, excluded_weapons)
    if hit is not None:
        return hit
    if not fallback_death:
        return None
    return _first_matching(death_events, round_bounds, excluded_weapons)


# -- The anomaly rules (AD-10, Story 2.5) -------------------------------------
#
# The rule names (``CT_ADVANCE``, ``CRUNCH``, ``STACK``) and the side they
# examine (``ANOMALY_RULE_SIDE``) are **not** defined here. They are shared
# vocabulary -- ``constants.ANOMALY_RULES`` validates against them and
# ``render`` orders the section by them -- so they live where the other shared
# vocabularies live, and ``ANOMALY_RULES`` is derived from them there. Epic
# 2's retrospective action (9). Only what is the rule's own and nobody else's
# stays below.


#: The areas that **do not count** towards the stack computation.
#:
#: Standing in spawn is not defending a site. The restriction is definition
#: and not tidying: ``CTSpawn`` falls into the A group on Ancient and into the
#: B group on Inferno, so without it the **starting setup** alone would fire
#: the rule on both maps -- that is, the rule would measure the start of the
#: round and not the defence's choice.
#:
#: ``TSpawn`` is here for the same reason: it is in the B group on Anubis, and
#: a CT player there is already a different observation (``ct_advance``), not
#: a stack.
SPAWN_AREAS: frozenset[str] = frozenset({"CTSpawn", "TSpawn"})


@dataclass(frozen=True)
class AreaObservations:
    """One area's alive observations from the demo's **unfiltered** table.

    The orientation is the demo's own observation: no map database, no
    human-supplied area division, no table accumulating across the archive. An
    accumulating source would give the same demo a different result depending
    on what other demos are in the archive.

    **Both teams' rows, not only the subject's.** This is a measured condition
    and not a preference: when the subject advances into an area as CT, their
    own CT observations lower that area's T share -- the anomaly eats its own
    detection. Computed on the subject's rows, three areas fall below the
    threshold (0.88 -> 0.79, 0.85 -> 0.75, 0.84 -> 0.75), and they are exactly
    the three that produced every true hit.

    Attributes:
        t: The observations in which the row's side was ``T``.
        total: All of the area's alive observations at the time sample points.

    Raises:
        ValueError: If the numbers are impossible. ``total = 0`` is not an
            area but the absence of one, and no share can be computed from it
            at all.
    """

    t: int
    total: int

    def __post_init__(self) -> None:
        if self.total <= 0:
            raise ValueError(
                f"The area has {self.total} observations, so it has no "
                "orientation. Zero observations is not an area but the "
                "absence of one, and no T share can be computed."
            )
        if not 0 <= self.t <= self.total:
            raise ValueError(
                f"There are {self.t} T observations when there are "
                f"{self.total} observations in total; a subset cannot be "
                "larger than the set."
            )

    @property
    def t_share(self) -> float:
        """The T observations' share of all of the area's observations."""
        return self.t / self.total


@dataclass(frozen=True)
class AreaPresence:
    """One player's presence at one sample point on one round.

    The row is a ``TICKS`` table row without the coordinates: the rules read
    only the area, and coordinates would tempt one into a geometry that does
    not exist.

    Attributes:
        player_id: The player. A hit's player count is computed from
            **distinct players**, not from rows.
        side: The row's team's side on this round. The rules examine only
            ``CT`` rows, so the same player as T cannot produce a hit.
        sample_kind: ``"time"`` or ``"first_contact"``. Only time sample
            points count: first contact's ``sample_t_s`` is a **measured
            moment**, so it would pass the time bound arbitrarily and would
            not be comparable between rounds.
        sample_t_s: The sample point's nominal time in seconds.
        area: The game's own ``env_cs_place`` area, or ``None``.
        is_alive: A dead player is not on the area.
    """

    player_id: str
    side: str
    sample_kind: str
    sample_t_s: float
    area: str | None
    is_alive: bool = True


@dataclass(frozen=True)
class AnomalyHit:
    """One hit: the rule, the area, the moment and the observation's numbers.

    A hit is the observation of **one sample point** on one round. The same
    area can hit at several sample points and on several rounds; grouping
    them into a sample (``n/m``) is done in the aggregation, not here.

    **A field belongs to the rule that measured it.** Five fields are
    rule-specific, and :meth:`__post_init__` requires them from exactly the
    right rule. Without the guard a hit could carry a number its rule did not
    compute -- and the report's row would claim as measured something that was
    not measured. The same justification ``sources`` already had: an empty
    source-area list on an advance row means "not asked", not "no directions".

    Attributes:
        rule: :data:`~pappascout.constants.CT_ADVANCE`,
            :data:`~pappascout.constants.CRUNCH` or
            :data:`~pappascout.constants.STACK`.
        area: The area the hit was observed on. Never ``None``: an area with
            no name cannot be the T side's area. On a stack it is **the first
            of** ``areas`` -- the largest of the areas the crowd is on, and on
            a tie the first by name. It is the row's **label** and not a claim
            that most of the crowd was there: measured, ``BackofB`` 2 +
            ``BombsiteB`` 2 is labelled ``BackofB`` by the alphabet alone, and
            the row's own ``areas`` is where the observation is. Until Story
            4.4 the label was the site's own area (:data:`SITE_AREAS`) even
            when nobody stood there, and the report then said ``BombsiteB``
            while all five players were in ``Alley``. The group is still on
            the row, as ``site``.
        sample_t_s: The sample point the hit was observed at.
        players: The number of distinct players. On the advance every CT
            player on the area, on the crunch only those who **arrived** (see
            :func:`crunch_hits`), on the stack those standing on the areas the
            hit names -- **not** everyone in the group. Group ``X`` 3, ``Y`` 1,
            ``Z`` 1 with ``stack_max_areas = 2`` gives ``players = 4`` while
            five are in the group, and that is the rule: the crowd is what was
            measured.
        t_share: The area's T share in this demo. **Only on the orientation
            rules**: the stack does not read the orientation, so the number
            would be invented there.
        observations: The number of the area's observations, that is, the
            orientation's own sample. The same restriction as on ``t_share``.
        sources: The crunch's source areas in alphabetical order; empty on the
            others.
        alive: The subject's living CT players **at this sample point**. Only
            on the stack. It is the hit's denominator: four out of five and
            four out of four are a different observation, and ``players``
            alone does not tell them apart.
        site: The site's group (:data:`SITE_GROUPS`) the players were in. Only
            on the stack.
        areas: The areas the crowd is standing on, **the largest first** and
            ties broken by name; at most ``stack_max_areas`` of them, and
            every one of them holds at least one of ``players``. Only on the
            stack, and there never empty. It is the hit's own concentration:
            "four players on at most two areas" is the whole rule, and without
            the list the row could not say which two. ``area`` is ``areas[0]``,
            so the summary row and its evidence cannot name different places.
    """

    rule: str
    area: str
    sample_t_s: float
    players: int
    t_share: float | None = None
    observations: int | None = None
    sources: tuple[str, ...] = ()
    alive: int | None = None
    site: str | None = None
    areas: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """The rule-specific fields belong to their own rule.

        Raises:
            ValueError: If the hit carries a field its rule does not measure,
                or if it lacks a field its rule does measure. Either would
                make the report's row a claim without an observation.
        """
        orientation_rule = self.rule in (CT_ADVANCE, CRUNCH)
        has_orientation = self.t_share is not None or self.observations is not None
        if orientation_rule and not (
            self.t_share is not None and self.observations is not None
        ):
            raise ValueError(
                f"The hit {self.rule!r} on area {self.area!r} does not carry "
                "the area's orientation. The rule leans on the area being "
                "held by the T side, so a hit without a T share and an "
                "observation count is a claim without evidence."
            )
        if self.rule == STACK:
            if has_orientation:
                raise ValueError(
                    f"A stack hit on area {self.area!r} carries the area's "
                    "orientation, although the rule does not read it at all. "
                    "The number would look measured but would not concern "
                    "this hit."
                )
            if self.site not in SITE_GROUPS:
                raise ValueError(
                    f"A stack hit's site group is {self.site!r}; the allowed "
                    f"ones are {list(SITE_GROUPS)}. The group is the hit's "
                    "anchor, and it cannot be left unnamed."
                )
            if self.alive is None:
                raise ValueError(
                    f"A stack hit on area {self.area!r} does not say how many "
                    "players were alive. Four out of five and four out of "
                    "four are a different observation, and the player count "
                    "alone does not tell them apart."
                )
            if not 0 < self.players <= self.alive:
                raise ValueError(
                    f"A stack hit claims {self.players} players on its areas "
                    f"when {self.alive} are alive. The crowd is a subset of "
                    "those alive."
                )
            if not self.areas:
                raise ValueError(
                    f"A stack hit on area {self.area!r} does not say which "
                    "areas the players were standing on. The concentration "
                    "is the rule itself -- four players on at most two areas "
                    "-- so a hit without the areas would report a crowd "
                    "without saying where it stood."
                )
            if len(set(self.areas)) != len(self.areas):
                raise ValueError(
                    f"A stack hit's areas {list(self.areas)} hold the same "
                    "area twice. One area is one place, and a repeat would "
                    "make the concentration bound count it twice."
                )
            if self.areas[0] != self.area:
                raise ValueError(
                    f"A stack hit's area {self.area!r} is not the first of "
                    f"its areas {list(self.areas)}. The row's area is the one "
                    "holding most of the crowd, so the summary and its "
                    "evidence cannot name different places."
                )
            if len(self.areas) > self.players:
                raise ValueError(
                    f"A stack hit names {len(self.areas)} areas but "
                    f"{self.players} players. Every area on the list holds at "
                    "least one of them, so the observation is broken."
                )
        else:
            if self.alive is not None or self.site is not None or self.areas:
                raise ValueError(
                    f"The hit {self.rule!r} on area {self.area!r} carries the "
                    "stack's fields (alive, site group, the crowd's areas), "
                    "although the rule does not measure them."
                )
        if self.sources and self.rule != CRUNCH:
            raise ValueError(
                f"The hit {self.rule!r} on area {self.area!r} carries source "
                "areas, although only the crunch computes directions."
            )


@dataclass(frozen=True)
class CloudCell:
    """One cell of the point cloud: where people stood and which area it is.

    A ``CALLOUT_CLOUD`` table row without ``map_demo_id`` and
    ``observations``. Both are deliberately left out:

    * the demo's id is the caller's bookkeeping, not the rule's input;
    * **the observation count carries no weight** in an area's centre. Every
      cell weighs one, and that is a measured condition and not a
      simplification -- see :func:`site_groups`.

    Attributes:
        area: The cell's area under the game's own name (``env_cs_place``).
        cell_x: The cell's index, ``floor(x / [parse].callout_grid_units)``.
        cell_y: The same on the y axis.
        cell_z: The same on the z axis. **Included and not passed over**: Nuke
            has floors, and the only thing that tells its sites apart there is
            the vertical difference.
    """

    area: str
    cell_x: int
    cell_y: int
    cell_z: int


def sample_point_count(presences: Iterable[AreaPresence]) -> int:
    """How many **time sample points some round of the demo reached**.

    The orientation's observation gate is a count *per sample point*
    (:func:`t_side_shares`), so the rule has to know how many points the demo
    was sampled at. The number is derived here, **from the presence rows
    themselves**, and it is deliberately not read from
    ``[parse].snapshot_seconds``.

    **It is therefore the grid's size only when some round reached the whole
    grid, and that is a real difference and not a quibble.** A point is
    dropped when it would fall after the round ended (see the module
    docstring), so the count is of the distinct seconds the demo's rows
    actually carry. A match whose every round was settled before 45 s has no
    45 s point anywhere, its divisor is 3 and not 4, and its gate is a quarter
    looser than the calibrated one -- **with nobody choosing that**. The
    alternative is worse in the same direction: the setting would divide a
    demo's rows by a grid those rows were never sampled at (below), and it
    would be wrong on every demo rather than on the short ones. Short rounds
    are also what the gate exists to be careful about, so the bias is
    recorded here and is not smoothed over by rounding the count up to the
    setting's length.

    **Why that is not the same as reading the setting.** The setting is an
    intention and the rows are the observation, and the two are allowed to
    differ: an archive parsed at one grid and a settings file edited to
    another is the ordinary state between a settings change and the next
    parse. A gate that trusted the setting would then divide a table's
    observations by a number of points those rows were never sampled at, and
    it would do so silently -- the very failure this story exists to remove,
    in the other direction. Reading the setting would also reach outside
    ``domain`` (AD-2), which is why the derivation is in this module beside
    the rule that uses it: the same justification as :func:`site_groups`, and
    so the rule and its input cannot disagree.

    **Every time row counts, alive or dead and either side.** The grid is a
    property of the parse and not of who survived the round: counting only
    living CT rows would shrink the divisor exactly on the rounds where the
    defence died early, and the gate would then admit the thinnest evidence
    where the evidence is thinnest. First contact is left out for the reason
    given in :func:`_is_ct_time_row` -- its moment is measured per round, so
    it is not a point of a grid at all.

    Args:
        presences: **The whole demo's** sample point rows and not one round's.
            A round that was settled early has fewer points than the grid has
            (see the module docstring), and the orientation these are compared
            against was counted over the whole demo. Derived per round, the
            gate would move with the round's length.

    Returns:
        The number of distinct ``sample_t_s`` values among the time rows. Zero
        when there are none, and that is an answer and not a gap: nobody was
        seen anywhere, so no area is oriented (:func:`t_side_shares`) and the
        demo is recorded as one without an orientation.
    """
    return len(
        {row.sample_t_s for row in presences if row.sample_kind == TIME_SAMPLE}
    )


def t_side_shares(
    orientation: Mapping[str | None, AreaObservations],
    *,
    t_share_min: float,
    min_observations_per_point: int,
    sample_points: int,
) -> dict[str, AreaObservations]:
    """The areas that are held by the T side **in this demo**.

    Both anomaly rules read an area's side orientation from this same
    function. That is not code thrift but definition: the rules ask the same
    question, and with two computations they could disagree about whose area
    an area is.

    **The observation gate is a count per sample point and not a raw count**
    (Story 4.6). An area's observations are one row per living player per
    round per sample point, so a raw bound means something different on every
    grid: the same demo sampled at fourteen points instead of four multiplies
    the counts and the bound admits areas it was calibrated to exclude.
    Measured on the archive, the CT advance went from 10 hits on 6 rounds to
    45 on 9 when nothing but the grid changed. Dividing by the grid's own size
    puts the bound in a unit the grid cannot move.

    **The equivalence at four points is exact and arithmetic, not a
    recalibration**: ``20 / 4 = 5``, so the shipped 5 per point selects the
    same areas as the raw 20 it replaces. The comparison is done by
    multiplication (``total >= per_point * points``) and not by division, so
    no rounding decides an area's orientation.

    **This does not make the orientation density-invariant, and it is not
    claimed to be.** An area's observations grow with the grid only in
    proportion to how long it is occupied, not uniformly: measured on the
    eight calibration demos parsed on both grids, the growth from four points
    to fourteen runs from 1.08x (``Water``, occupied at the start of a round
    and nowhere else) to 4.90x (``TSideUpper``), so the "about 3.5x" of the
    average is the property of no single area. ``advance_t_share`` moves too,
    because a denser grid weights a round's later seconds differently. What
    this unit removes is the part of the dependence that is arithmetic; the
    rest is a property of the observation, and it is recorded as data in
    ``tests/data/orientation_by_grid.json`` rather than argued about.

    Args:
        orientation: Area -> its observations. The key ``None`` (the area's
            name could not be obtained) is **skipped**: a nameless area cannot
            be either side's area, and a hit "on an unknown area" would not
            say where.
        t_share_min: ``[thresholds].advance_t_share``.
        min_observations_per_point:
            ``[thresholds].advance_area_min_observations_per_point``. The
            comparison is ``>=``: **an area exactly at the bound counts**, and
            an area with exactly 5 observations per point is therefore
            included. The precision carries weight here, because the whole
            calibration of the thresholds leans on exact bounds (Nuke's
            outside is exactly 0.70). An area that falls **below** the bound
            is neither the T side's nor the CT side's area -- an orientation
            is not guessed from a thin observation.
        sample_points: How many time sample points the demo was sampled at,
            from :func:`sample_point_count`. An argument and not a derivation
            here, because this function is given the orientation and not the
            rows it was counted from. **Zero is a valid input**: a demo with
            no time sample point has no area anybody was seen on, so no area
            is oriented -- and that is not silence, it is the demo landing in
            the coverage's ``demos_without_orientation``. It is a valid input
            and **not a shortcut past the checks**: a contradictory map is
            refused at zero points exactly as it is at four.

    Returns:
        Area -> observations, only for the areas that passed the thresholds.

    Raises:
        ValueError: If ``t_share_min`` is not in the range 0..1, if
            ``min_observations_per_point`` is not positive, or if
            ``sample_points`` is negative. The first two would silently make
            the rule impossible or fire it on every area; the third is not a
            thinner grid but an impossible one, so it means the caller counted
            something else.
    """
    if not 0.0 <= t_share_min <= 1.0:
        raise ValueError(
            f"The T share threshold {t_share_min!r} is not in the range 0..1. "
            "The share is the number of T observations divided by all of the "
            "area's observations, so a threshold outside it would either "
            "silence the rule altogether or make every area the T side's."
        )
    if min_observations_per_point < 1:
        raise ValueError(
            f"The area's minimum observation count per sample point "
            f"{min_observations_per_point!r} is not positive. Without an "
            "observation an area has no orientation, and it must not be "
            "guessed."
        )
    if sample_points < 0:
        raise ValueError(
            f"The demo has {sample_points!r} time sample points, which is not "
            "a grid at all. A count of points cannot be negative, so this is "
            "not a thinner observation but a caller that counted something "
            "else."
        )
    minimum = min_observations_per_point * sample_points
    seen_raw: set[str] = set()
    passed: dict[str, AreaObservations] = {}
    for raw_area, obs in orientation.items():
        area = normalize_area(raw_area)
        if area is None:
            continue
        if area in seen_raw:
            raise ValueError(
                f"The orientation map holds the area {area!r} twice in "
                f"different spellings. An area's name is an observation, so "
                "one of two spellings cannot be chosen -- write the map with "
                "one normalisation (domain.sampling.normalize_area)."
            )
        seen_raw.add(area)
        # ``sample_points == 0`` means no time sample point, so nobody was
        # seen anywhere and no area has an orientation. Not silence: the demo
        # lands in the coverage's ``demos_without_orientation``, which is the
        # same answer an empty orientation map gives.
        #
        # **It is a condition here and not an early return above the loop**,
        # which is where it stood until review round 1 measured what that
        # cost: a map holding the same area in two spellings was refused at
        # four points and accepted in silence at zero, so a consistency guard
        # stopped guarding on exactly one input. The zero case must not be a
        # different function with a looser contract. ``minimum`` cannot carry
        # it -- it is ``0`` there, which every ``total`` clears.
        if sample_points and obs.total >= minimum and obs.t_share >= t_share_min:
            passed[area] = obs
    return passed


def _z_band(
    points: Sequence[tuple[float, float, float]], trim: float
) -> tuple[float, float]:
    """The height an area occupies, as its 5th and 95th percentile of z.

    Percentiles and not the extremes: ``m_szLastPlaceName`` is the *last
    named* area, so a handful of cells carry a name from somewhere else
    entirely, and one of those on another floor would stretch the band across
    the very gap the caller is looking for.
    """
    zs = sorted(z for _, _, z in points)
    if not zs:
        return (0.0, 0.0)
    lo = zs[int(len(zs) * trim)]
    hi = zs[min(len(zs) - 1, int(len(zs) * (1.0 - trim)))]
    return (lo, hi)


def _void_share(
    points: Sequence[tuple[float, float, float]], void: tuple[float, float]
) -> float:
    """The share of an area's cells that sit in the empty band between sites.

    Zero for an area wholly on one floor, and the measure of a bridge.
    """
    if not points:
        return 0.0
    lo, hi = void
    return sum(1 for _, _, z in points if lo < z < hi) / len(points)


def site_groups(
    cells: Iterable[CloudCell],
    *,
    margin: float,
    separation_min: float,
    floor_gap_ratio: float,
    floor_band_trim: float,
    floor_z_weight: float,
    bridge_void_share: float,
) -> dict[str, str] | None:
    """A mapping ``area -> "A" | "B"`` **from the demo's own point cloud**.

    This is the piece the stack rule was missing. The game splits a site
    across several areas (Ancient's B is ``Alley`` + ``BombsiteB`` +
    ``SideEntrance``), so four defenders are never on the same
    ``env_cs_place`` area: the rule needs a group, and the group is
    **derived**, not given. The same locked condition as on
    :class:`AreaObservations` -- no map database, no human-supplied area
    division, no table accumulating across the archive.

    The method is in three parts:

    1. **An area's centre is the cell median**: every cell weighs one, the
       observation count carries no weight. This is a measured condition. The
       game's ``m_szLastPlaceName`` is the *last named* area, so a player
       standing in an unnamed spot carries the previous round's area; in
       ``Ancient_vs_kaljukostaja``'s CT spawn cells ``BombsiteB`` has 75,524
       observations and ``CTSpawn`` only 135. An observation-weighted mean
       therefore drags the centre into the spawn, the cell median does not --
       75,000 observations in 16 cells weigh as much as 16 cells. Measured:
       Ancient's contradictory areas 5/18 -> **0/18**, and the area division
       is word for word the same from all three Ancient demos.
    2. **An area's size is the cells' median distance** from their own centre.
       The median and not the mean or the largest: one stale name on the other
       side of the map would stretch both of those, and it is precisely that
       fault that is warded off here.
    3. **The group is the nearer site by a margin**: an area belongs to the
       nearer site only if the other site is at least ``margin`` times
       further away. Otherwise the area is left without a group (the map's
       shared middle), and no direction is guessed.

    **A map whose sites do not separate on either axis goes quiet.** The
    guard is a ratio and not a list of maps: the distance between the sites'
    centres divided by the sites' own size is 0.47-0.54 on Nuke and 3.70-5.04
    on the three other maps, so a threshold of 2.0 separates them cleanly
    **without naming a map in the code**. Going quiet is the right answer
    where it stands, and it has to be recorded in the coverage
    (``AnomalyScan.demos_without_site_groups``) rather than left silent.

    **Story 4.3 narrowed what that guard covers, and the earlier record of it
    was wrong.** Until then this docstring said that on Nuke, where the sites
    sit on different floors, *any* A/B distance measure is meaningless. That
    was a statement about one axis mistaken for a statement about the map.
    Measured over the archive on 2026-09-12: the plan-view ratio does fail on
    Nuke, and it fails for a reason -- but the sites' height bands there do
    not overlap at all (a gap of 0.75 of their own combined height, agreed by
    all three of its demos), while every other map's bands touch or overlap.
    Nuke's division was available the whole time on the axis the guard was
    not measuring. The separation guard is therefore applied **only when the
    map is not stacked**; on a stacked map it would answer for the wrong
    axis, and going quiet there would be a shortfall rather than a verdict.

    **The grid size cancels out, and that is why it is not given.** A cell
    index is not a coordinate: the real coordinate is
    ``cell * [parse].callout_grid_units``. Every threshold here is, however,
    **a quotient of two distances or a share of a count**, and the grid size
    multiplies every distance by the same number, so it cancels out of every
    comparison. That is exactly why this function -- and the aggregation as
    its caller -- does not read the ``[parse]`` section at all. **If an
    absolute distance limit is ever added here, the conversion is
    mandatory**, and its source does not yet exist in the aggregation.

    Story 4.3 came close to breaking that rule and is the reason it is
    restated here. The floor gap was first written as a count of cells,
    which would have made the branch change meaning with the grid: at twice
    the grid Nuke's gap of six cells is three, and the archive -- parsed at
    one grid size -- could not have noticed. It is now divided by the sites'
    own combined height, so it is a ratio like the rest.

    Args:
        cells: The demo's point cloud cells. In any order; an empty cloud is
            a valid input and produces ``None``.
        margin: ``[thresholds].stack_group_margin``. At least 1.0.
        separation_min: ``[thresholds].stack_site_separation_min``.
        floor_gap_ratio: ``[thresholds].site_floor_gap_ratio``. How much
            empty height must lie between the two sites' bands, **as a share
            of the sites' own combined height**, before the map counts as
            **stacked** -- its sites on different floors rather than side by
            side. A ratio and not a cell count, so the grid size cancels out.
            On a stacked map the plan-view separation guard is not applied,
            because it measures the wrong axis there.
        floor_band_trim: ``[thresholds].site_floor_band_trim``. Which
            percentile bounds a site's band. A threshold in its own right,
            and not an implementation detail: ``m_szLastPlaceName`` is the
            **last** name entered, so a handful of a site's cells carry its
            name from elsewhere, and one of those on another floor would
            stretch the band across the gap. Measured, the answer is stable
            from 0.02 to 0.10 and wrong on both sides of that.
        floor_z_weight: ``[thresholds].site_floor_z_weight``. How much height
            counts in the distance on a stacked map. Exactly 1.0 on every
            other map, so nothing changes where the sites are side by side.
        bridge_void_share: ``[thresholds].site_bridge_void_share``. The share
            of an area's cells that must sit in the empty band between the
            floors before the area is treated as a **bridge** and left out of
            both groups. Only applies on a stacked map; a flat map has no
            void to span.

    Returns:
        ``area -> "A" | "B"`` for the areas that have a group, or ``None`` if
        the map has no A/B division that separates evenly. Areas without a
        group are **absent** from the mapping; a ``None`` value is not
        written, so that ``groups.get(area)`` is unambiguous.

        **Two different things are absent from the mapping and the caller
        cannot tell them apart.** An area may be missing because it is the
        map's shared middle -- neither site is nearer by the margin -- or,
        on a stacked map, because it is a bridge between the floors. Both are
        "no group", and that is enough for the rules that consume this; an
        API that distinguished them would be a different function.

        **The result cannot be an empty mapping, and Story 4.3 had to work
        to keep it that way.** A site's distance to its own centre is 0, so
        each site always belongs to its own group under any margin; if the
        function gets this far, the mapping holds at least those two. An
        empty dictionary is therefore possible only as a caller's own value
        (in a test, for instance), not as this function's result, and the
        code must not lean on telling it apart from ``None``.

        The bridge rule is tested **before** the distance test and could
        otherwise drop a site: a site's own cells reach into the void by the
        width of the percentile trim, so a ``bridge_void_share`` below about
        0.05 would exclude both sites and return ``{}``. That is why the
        bridge test excludes the two sites by name rather than relying on a
        threshold to keep them. The guarantee matters two layers up:
        ``aggregate`` reads an empty mapping as "the map has a division and
        nobody is on either side of it" -- an observation -- while ``None``
        is a blind spot it records in the coverage. A ``{}`` leaking out of
        here would be counted as a scanned round with no hits, which is the
        one thing ``demos_without_site_groups`` exists to prevent.

    Raises:
        ValueError: If ``floor_band_trim`` is not strictly between 0 and 0.5,
            if ``floor_gap_ratio`` or ``bridge_void_share`` is not positive
            and finite, if ``floor_z_weight`` is below 1.0, if ``margin`` is
            below 1.0, or if ``separation_min`` is not positive. Each message
            says what the value would do rather than merely that it is out of
            range: a trim of 0 leaves no map in the archive stacked at all, a
            gap ratio of 0 makes every map whose bands merely touch stacked,
            a weight below 1 would make height count for less than plan
            distance on the one map where height is the answer. For the last
            two: the former would make the "nearer" one the further one,
            the latter would remove the guard altogether -- that is, a
            division would be derived from Nuke's overlapping sites that does
            not exist.
    """
    if not (margin >= 1.0 and math.isfinite(margin)):
        raise ValueError(
            f"The group margin {margin!r} is below 1.0 or is not finite. The "
            "margin says how much further away the other site has to be, and "
            "with a value below one the 'nearer' site could be the further "
            "one."
        )
    if not (separation_min > 0.0 and math.isfinite(separation_min)):
        raise ValueError(
            f"The separation threshold {separation_min!r} is not positive and "
            "finite. With the value 0 the guard would silence no map at all, "
            "that is, an area division that does not exist would be derived "
            "from overlapping sites."
        )
    if not (0.0 < floor_band_trim < 0.5 and math.isfinite(floor_band_trim)):
        raise ValueError(
            f"The floor band trim {floor_band_trim!r} is not a share strictly "
            "between 0 and 0.5. At zero the band is the raw extremes and a "
            "single stray cell carrying a name from another floor stretches "
            "it across the very gap being looked for -- measured, no map in "
            "the archive is stacked at all at zero. At 0.5 the band collapses "
            "to the median and has no height to compare."
        )
    if not (floor_gap_ratio > 0.0 and math.isfinite(floor_gap_ratio)):
        raise ValueError(
            f"The floor gap ratio {floor_gap_ratio!r} is not positive and "
            "finite. At zero every map whose site bands merely touch would be "
            "treated as stacked -- measured, Anubis touches at 0.00 and 0.14 "
            "and would qualify, which is the opposite of what the gap is for."
        )
    if not (floor_z_weight >= 1.0 and math.isfinite(floor_z_weight)):
        raise ValueError(
            f"The floor z weight {floor_z_weight!r} is below 1.0 or is not "
            "finite. Below one, height would count for *less* on the one kind "
            "of map where height is the only thing that tells the sites "
            "apart."
        )
    if not 0.0 < bridge_void_share <= 1.0:
        raise ValueError(
            f"The bridge share {bridge_void_share!r} is outside (0, 1]. At or "
            "below zero every area with a single stray cell between the "
            "floors would be called a bridge and dropped from both groups; "
            "above one no area could ever be one."
        )

    points: dict[str, list[tuple[float, float, float]]] = {}
    for cell in cells:
        area = normalize_area(cell.area)
        if area is None:
            # A nameless cell does not name an area. The same rule as in
            # building the cloud (domain.utility.point_cloud).
            continue
        points.setdefault(area, []).append(
            (float(cell.cell_x), float(cell.cell_y), float(cell.cell_z))
        )

    centres = {area: _cell_median(pts) for area, pts in points.items()}
    site_a, site_b = SITE_AREAS["A"], SITE_AREAS["B"]
    if site_a not in centres or site_b not in centres:
        # A cloud in which one of the sites is missing cannot say how the
        # division between the sites runs. The absence of an observation is
        # not an observation that the division is absent.
        return None

    span = _spread(points[site_a], centres[site_a]) + _spread(
        points[site_b], centres[site_b]
    )
    separation = math.dist(centres[site_a], centres[site_b])
    # Three conditions, and the first two are holes in the guard and not
    # just-in-case checks.
    #
    # ``span <= 0`` is a **site of zero size**: both sites have one cell, so
    # the ratio divides by zero and any difference at all would pass the
    # threshold. An observation of two cells says nothing about the map's site
    # structure, and a demo whose cloud is that thin is broken, not a map
    # whose sites separate evenly.
    #
    # ``separation <= 0`` is **overlapping centres**: the sites cannot be told
    # apart from each other at all, and neither would be genuinely nearer to
    # any area.
    if span <= 0.0 or separation <= 0.0:
        return None

    # **The sites may separate vertically instead, and then the ratio above
    # measures the wrong axis.** On Nuke the two sites sit on different floors
    # and overlap in plan view, so their distance-over-size ratio is 0.47-0.54
    # against a threshold of 2.0 and the map went silent -- while their cells
    # share no height at all. Measured 2026-09-12 over the archive:
    #
    #   de_nuke      A -13..-11   B -25..-19   gap  6 cells, disjoint
    #   de_anubis    A  -6..-2    B  -1..2     gap -1..0, touching
    #   de_ancient   A   1..4     B   3..5     overlap
    #   de_inferno   A   3..8     B   4..7     overlap
    #
    # Nuke is the only map whose sites are disjoint on z, and the margin
    # between its 6 and Anubis's -1 is what ``floor_gap_ratio`` sits in. The
    # question the separation ratio really asks is *are the sites
    # distinguishable at all*; on a stacked map the answer is yes, on another
    # axis, so the silence is lifted rather than the threshold loosened.
    a_band = _z_band(points[site_a], floor_band_trim)
    b_band = _z_band(points[site_b], floor_band_trim)
    void = (min(a_band[1], b_band[1]), max(a_band[0], b_band[0]))
    # **A ratio and not a cell count**, for the same reason the two thresholds
    # above are quotients: a cell index is not a coordinate, and the grid size
    # multiplies every distance by the same number. An absolute gap would
    # silently change meaning if ``[parse].callout_grid_units`` ever moved --
    # at twice the grid Nuke's gap of six cells becomes three. Dividing by the
    # sites' own vertical size cancels the grid out. Measured over the archive
    # (positive = the bands are apart):
    #
    #   de_nuke     0.75  0.75  0.75
    #   de_anubis   0.14  0.00
    #   de_ancient -0.20 -0.20  0.00
    #   de_inferno -0.38
    heights = (a_band[1] - a_band[0]) + (b_band[1] - b_band[0])
    gap = void[1] - void[0]
    # Sites with no height of their own are a degenerate cloud rather than a
    # flat map: a real site spans several cells. Treat them as stacked when
    # anything at all separates them, because a gap measured against zero own
    # size is as separated as a pair can be -- silently answering "not
    # stacked" would be a worse lie than either verdict.
    stacked = gap / heights >= floor_gap_ratio if heights > 0.0 else gap > 0.0

    if not stacked and separation < separation_min * span:
        return None

    # On a stacked map height is what tells the sites apart, so it has to
    # carry more than one third of the distance. Measured against the product
    # owner's own Nuke grouping: weight 1 reproduces 10 of 15 area
    # assignments, weight 2 gives 13, weight 3 gives 14, and weights above 3
    # add nothing. On a flat map the weight is 1 and nothing changes.
    weight = floor_z_weight if stacked else 1.0
    if weight != 1.0:
        centres = {
            area: _cell_median([(x, y, z * weight) for x, y, z in pts])
            for area, pts in points.items()
        }

    found: dict[str, str] = {}
    for area, centre in centres.items():
        # **An area that spans the empty band between the sites is a bridge,
        # and it belongs to neither.** It is a way through rather than a place
        # on one side, so a nearest-centre measure must put it on one side and
        # both answers are wrong. Measured 2026-09-12, share of each area's
        # cells inside Nuke's void: ``Ramp`` 40 %, ``Secret`` 31 %, ``Vents``
        # 21 %, and every one of the other 26 areas at 3 % or below. Those
        # three are exactly the ones the product owner names as connecting the
        # levels, so the derivation finds what he would have had to supply.
        #
        # **A site is never a bridge**, and the exclusion is structural rather
        # than a matter of threshold. Without it the contract below -- that
        # the result can never be an empty mapping -- is reachable: a site's
        # own cells extend into the void by the width of the percentile trim,
        # so a share below about 0.05 drops both sites and returns ``{}``.
        # That would be worse than silence, because ``aggregate`` reads an
        # empty mapping as "the map has a division and nobody is on either
        # side of it" -- an observation -- while ``None`` is a blind spot it
        # records in the coverage.
        if (
            stacked
            and area not in (site_a, site_b)
            and _void_share(points[area], void) >= bridge_void_share
        ):
            continue
        to_a = math.dist(centre, centres[site_a])
        to_b = math.dist(centre, centres[site_b])
        # Genuinely nearer BEFORE the margin: with margin == 1.0 the margin
        # condition alone would hold in both directions on a tie, and the
        # group would be settled by which branch was written first.
        if to_a < to_b and to_b >= margin * to_a:
            found[area] = "A"
        elif to_b < to_a and to_a >= margin * to_b:
            found[area] = "B"
    return found


def ct_advance_hits(
    presences: Iterable[AreaPresence],
    *,
    round_type: str | None,
    orientation: Mapping[str | None, AreaObservations],
    t_share_min: float,
    area_min_observations_per_point: int,
    sample_points: int,
    max_sample_s: float,
    min_players: int,
) -> list[AnomalyHit]:
    """One round's CT advances.

    The rule: the subject's CT player in an area that is held by the T side
    **in that demo**, on a saving round and at ``max_sample_s`` seconds at the
    latest.

    The restriction to saving rounds is an **economic observation** and not a
    narrowing of the sample: a poor CT does not normally advance into the T
    side's area, so it is exactly then that an advance says something about a
    plan. The round types are
    :data:`~pappascout.constants.SAVING_ROUND_TYPES`.

    Args:
        presences: The round's sample point rows, in any order. Anything other
            than living CT rows from the time sample points is skipped here,
            so that the caller does not have to remember to filter them out.
        round_type: The round's type. ``None`` (an unclassified round) cannot
            hit: without a type it is not known whether the round was a saving
            round.
        orientation: Area -> observations from the demo's **unfiltered**
            table.
        t_share_min: ``[thresholds].advance_t_share``.
        area_min_observations_per_point:
            ``[thresholds].advance_area_min_observations_per_point``.
        sample_points: The **demo's** number of time sample points, from
            :func:`sample_point_count`. It comes as an argument for the same
            reason as ``orientation``: both are per-demo observations, and
            this function sees one round. Derived from this round's
            ``presences`` it would be the round's own point count, so a round
            settled in 30 seconds would lower the orientation's bound -- and
            the four-point behaviour this story preserves would move.
        max_sample_s: ``[thresholds].advance_max_sample_s``.
        min_players: ``[thresholds].advance_min_players``.

    Returns:
        The hits in the order ``(sample_t_s, area)``. An empty list is a valid
        result and not a shortfall.
    """
    if round_type not in SAVING_ROUND_TYPES:
        return []
    t_areas = t_side_shares(
        orientation,
        t_share_min=t_share_min,
        min_observations_per_point=area_min_observations_per_point,
        sample_points=sample_points,
    )
    if not t_areas:
        return []
    hits: list[AnomalyHit] = []
    for (seconds, area), players in _players_by_point(
        presences, t_areas, max_sample_s
    ).items():
        if len(players) < min_players:
            continue
        obs = t_areas[area]
        hits.append(
            AnomalyHit(
                rule=CT_ADVANCE,
                area=area,
                sample_t_s=seconds,
                players=len(players),
                t_share=obs.t_share,
                observations=obs.total,
            )
        )
    return sorted(hits, key=lambda hit: (hit.sample_t_s, hit.area))


def crunch_hits(
    presences: Iterable[AreaPresence],
    *,
    orientation: Mapping[str | None, AreaObservations],
    t_share_min: float,
    area_min_observations_per_point: int,
    sample_points: int,
    max_sample_s: float,
    lookback_s: float,
    min_players: int,
    min_sources: int,
) -> list[AnomalyHit]:
    """One round's crunches.

    The rule reads the same orientation as :func:`ct_advance_hits`, but it
    also requires the players to have **arrived** on the area from at least
    ``min_sources`` different directions at the same time. A source area is
    the player's own area ``lookback_s`` seconds earlier, that is, an
    observation -- not map geometry and not a table of neighbouring areas.

    **The look-back is a duration and no longer "the previous sample point"**
    (Story 4.6). Read as the previous point, the rule's reach was whatever the
    grid's spacing happened to be -- 9 s from one point and 15 s from another
    at four points, 3 s at fourteen -- so densifying the grid silently changed
    what the rule asks. Measured, the crunch fell from 5 hits on 4 rounds to
    2 on 2 when nothing but the grid changed. A duration is the same question
    on every grid. **The answer still is not**, and the residual is measured
    rather than claimed away: the grid decides how nearly the look-back can be
    honoured, and on the one demo that exists on both grids 67 of 320 shared
    source look-ups land on a different area (:func:`_source_areas`).

    **A crunch is not limited to saving rounds even though the advance is.**
    The epic sets the economic condition on the advance only, and the
    measurement supports it: one of the five crunches (MatureMayhem Anubis
    round 10) is a full buy. So the rule is not a "stricter form" of the
    advance: it is stricter about directions and looser about the round type,
    so the hit sets intersect each other.

    ``players`` is the number of those who **arrived** and not of those on the
    area: a player who was already on the area ``lookback_s`` earlier did not
    arrive there from anywhere. The same sample point can therefore produce an
    advance hit with three players and a crunch hit with two, and those are
    two different observations of the same moment.

    A player whose source area is not known (a nameless area, or no sample
    point that far back on this round) **has not arrived from anywhere**: no
    direction is guessed. That is also the answer when the look-back reaches
    past the round's own start -- the rule does not reach into freezetime or
    into the round before.

    Args:
        presences: The round's sample point rows, in any order. Rows after
            ``max_sample_s`` may be included and are simply not targets.
            **They are not needed for the sources either**, and the docstring
            said otherwise from Story 2.5 until this was measured in review:
            a source is at or before ``t - lookback_s`` and the target ``t``
            is itself bounded by ``max_sample_s``, so a source can never lie
            after the bound -- with the look-back and with the previous-point
            rule that preceded it alike. The requirement was contentless, and
            the test that guarded it could not fail; both are gone rather than
            carried forward as a reason that never was one.
        orientation: As in :func:`ct_advance_hits`.
        t_share_min: ``[thresholds].advance_t_share``, **shared** with the
            advance.
        area_min_observations_per_point:
            ``[thresholds].advance_area_min_observations_per_point``.
        sample_points: As in :func:`ct_advance_hits`.
        max_sample_s: ``[thresholds].advance_max_sample_s``.
        lookback_s: ``[thresholds].crunch_lookback_s``, how far back the
            arrival is read from.
        min_players: ``[thresholds].crunch_min_players``.
        min_sources: ``[thresholds].crunch_min_sources``.

    Returns:
        The hits in the order ``(sample_t_s, area)``.
    """
    t_areas = t_side_shares(
        orientation,
        t_share_min=t_share_min,
        min_observations_per_point=area_min_observations_per_point,
        sample_points=sample_points,
    )
    if not t_areas:
        return []
    rows = [row for row in presences if _is_ct_time_row(row)]
    previous = _source_areas(rows, lookback_s)

    arrivals: dict[tuple[float, str], dict[str, str]] = {}
    for row in rows:
        area = normalize_area(row.area)
        if area is None or area not in t_areas or row.sample_t_s > max_sample_s:
            continue
        source = previous.get((row.player_id, row.sample_t_s))
        if source is None or source == area:
            # Did not arrive: the direction is unknown or the player was
            # already on the area.
            continue
        arrivals.setdefault((row.sample_t_s, area), {})[row.player_id] = source

    hits: list[AnomalyHit] = []
    for (seconds, area), by_player in arrivals.items():
        sources = sorted(set(by_player.values()))
        if len(by_player) < min_players or len(sources) < min_sources:
            continue
        obs = t_areas[area]
        hits.append(
            AnomalyHit(
                rule=CRUNCH,
                area=area,
                sample_t_s=seconds,
                players=len(by_player),
                t_share=obs.t_share,
                observations=obs.total,
                sources=tuple(sources),
            )
        )
    return sorted(hits, key=lambda hit: (hit.sample_t_s, hit.area))


def stack_hits(
    presences: Iterable[AreaPresence],
    *,
    groups: Mapping[str, str] | None,
    sample_s: float,
    max_areas: int,
    min_players: int,
) -> list[AnomalyHit]:
    """One round's stacks.

    The rule: **at least** ``min_players`` of the subject's living CT players
    standing on **at most** ``max_areas`` areas of the same site's group, at
    the setup sample point ``sample_s``.

    Story 4.4 rewrote the definition, and it was the definition and not the
    thresholds that was wrong: on two blind lists (43 judged rounds) the old
    rule read 22 of the 23 rounds it reported differently from the product
    owner. Two conditions changed:

    * **The concentration is the rule.** The old rule asked only how many
      players were in the group, and a group is half the map: five players on
      five different areas of the A side is a normal defence, not a stack.
      Measured at 15 s over the archive's 93 CT rounds, bounding the areas to
      two takes the rule from 24 rounds to 5. **Two populations, and they
      must not be confused** (they were in the first version of this text):

      - *Inside the rule's scope*, that is, the subject's 93 CT rounds: 32 of
        the 43 judged rounds. The rule finds all **4** of his stack-like
        rounds there and fires on **none** of the 26 he read as not a stack.
      - *Over all 43 judgements*, opponent-side rounds included, the same
        condition separates them without one exception: 8 of 8 stacks reach
        four players on at most two areas, and all 33 "not a stack" rounds
        stay at three or fewer. Four of those 8 are the opponent's CT rounds,
        which this rule cannot scan at all; they are evidence about the
        *shape* and not about this archive's hits.
    * **The site's own area is not required.** The old rule demanded that at
      least one player stand on :data:`SITE_AREAS`, on the reasoning that
      "stack sitellä" means being on the site. Measured, that condition
      silences six of the measurement's seven candidate hits and four of the
      five this rule reports: five CT players in ``Alley`` is his own "B
      stack" and not one of them is on ``BombsiteB``. The hit therefore names
      the areas the crowd is really on (``areas``, ``area``) and keeps the
      group as ``site``.

    What did not change: **the spawns stay out** (:data:`SPAWN_AREAS` -- a
    player standing in spawn is not defending a site, and ``CTSpawn`` falls
    into the A group on Ancient and into the B group on Inferno), and an area
    the demo's own geometry left **without a group** produces no hit.

    **That last one is a property of the derivation and not a definition, and
    the difference matters.** Whether an area has a group is measured per demo
    from that demo's point cloud, so a map's middle can be in a group on one
    map and in none on another -- Inferno's ``Middle`` is group A and is one
    of this archive's five hits, while Ancient's is in neither group.
    Measured, ``Middle`` is even group A in ``anubis_vs_RCAVE_VETERANS`` and
    ungrouped in ``Anubis_vs_ryhmarama``, two demos of the same map. So the
    rule does not exclude "the middle": it reports a crowd on the areas of
    **one derived group**, whatever those areas are. Whether a mid
    concentration belongs in the report under a name of its own is an open
    product question (measurement §5) and is not decided here.

    **One sample point and not a time bound.** The other two rules ask about
    *movement*, which has only a ceiling; a setup is a *moment* and has both a
    floor and a ceiling, so the stack reads exactly ``sample_s`` and no longer
    shares ``advance_max_sample_s``. Measured: at 6 s the rule fires on 34 of
    the archive's 93 CT rounds instead of 5 -- on Nuke nearly every round,
    where ``Hell`` and ``Outside`` are the way out of spawn -- and 30 s is
    already the reaction to the round. The product owner's own words, in full
    because a fragment of them would be a paraphrase: *"30 s kohdalla on
    voitu jo hyvinkin reagoida kierroksen tapahtumiin joten strategiaa on
    voinut lähteä elämään."*

    **The pattern is not named here.** Whether a concentration is a stack or a
    push was measured as *not separable* from this data (neither the share of
    the crowd that held its place nor the areas' T share separates his own
    judgements), so the rule reports what it saw and not what to call it.

    The rule **is not limited by round type** and does not read the area's
    orientation. So it is not a stricter or a looser form of either of the
    other two rules but a third question about the same observation.

    Args:
        presences: The round's sample point rows, in any order. Anything other
            than living CT rows from the time sample point ``sample_s`` is
            skipped here.
        groups: The result of :func:`site_groups` for this demo. ``None`` (the
            map has no A/B division that separates evenly) **silences the
            rule**, and that is the right answer and not a shortfall -- but
            the caller has to record it in the coverage, not leave it silent.
        sample_s: ``[thresholds].stack_sample_s``, the setup sample point. A
            row is read when its ``sample_t_s`` is this point; both numbers
            are the same nominal second written twice (in the settings and in
            the table), so they are compared with a tolerance and not with
            ``==``.
        max_areas: ``[thresholds].stack_max_areas``, how many areas the crowd
            may be spread over.
        min_players: ``[thresholds].stack_min_players``.

    Returns:
        The hits in the order ``(sample_t_s, area)``. An empty list is a valid
        result; ``groups=None`` also produces an empty list, and **the two
        cannot be told apart from here** -- the difference is in the coverage.

    Raises:
        ValueError: If ``min_players`` or ``max_areas`` is not positive, or if
            ``groups`` names a group that does not exist. The first would fire
            the rule at every sample point, the second would leave the
            concentration with no areas to be on, and the third would mean
            that the groups come from somewhere other than :func:`site_groups`.
    """
    if min_players < 1:
        raise ValueError(
            f"The stack's minimum player count {min_players!r} is not "
            "positive. With zero the rule would hit at every sample point at "
            "which there is nobody in the site's group."
        )
    if max_areas < 1:
        raise ValueError(
            f"The stack's area bound stack_max_areas {max_areas!r} is not "
            "positive. Below one the crowd would have nowhere to stand and "
            "the rule could not fire on any round at all."
        )
    if groups is None:
        return []
    unknown = sorted({name for name in groups.values() if name not in SITE_GROUPS})
    if unknown:
        raise ValueError(
            f"The site groups hold an unknown group: {unknown}. The allowed "
            f"ones are {list(SITE_GROUPS)}, and they come from site_groups()."
        )

    # Being alive is counted from ALL acceptable rows, including those in
    # spawn and on an area without a group: it is the hit's denominator
    # ("four out of five"), and a player does not stop being alive because
    # they are standing in the wrong place.
    alive: set[str] = set()
    # group -> area -> the distinct players on it. The players are a set and
    # not a row count: a duplicated row for the same player must not raise the
    # player count, because that count is precisely the report's number.
    members: dict[str, dict[str, set[str]]] = {}
    for row in presences:
        if not _is_ct_time_row(row) or not is_sample_point(
            row.sample_t_s, sample_s
        ):
            continue
        alive.add(row.player_id)
        area = normalize_area(row.area)
        if area is None or area in SPAWN_AREAS:
            continue
        group = groups.get(area)
        if group is None:
            continue
        members.setdefault(group, {}).setdefault(area, set()).add(row.player_id)

    hits: list[AnomalyHit] = []
    for group, by_area in members.items():
        chosen, crowd = _biggest_crowd(by_area, max_areas)
        if len(crowd) < min_players:
            continue
        hits.append(
            AnomalyHit(
                rule=STACK,
                area=chosen[0],
                sample_t_s=sample_s,
                players=len(crowd),
                alive=len(alive),
                site=group,
                areas=tuple(chosen),
            )
        )
    return sorted(hits, key=lambda hit: (hit.sample_t_s, hit.area))


# -- Internal -----------------------------------------------------------------


def _cell_median(
    points: Sequence[tuple[float, float, float]],
) -> tuple[float, float, float]:
    """The per-axis median of a set of cells.

    **Every cell weighs one.** The median is computed axis by axis and not as
    a multidimensional (geometric) median: the latter would be an iterative
    approximation whose result would depend on the starting value and the
    number of iterations -- that is, the same demo could give a different
    centre on a different run.
    """
    return (
        median(p[0] for p in points),
        median(p[1] for p in points),
        median(p[2] for p in points),
    )


def _spread(
    points: Sequence[tuple[float, float, float]],
    centre: tuple[float, float, float],
) -> float:
    """An area's size: the cells' **median distance** from the centre.

    The median and not the mean or the largest. One stale area name on the
    other side of the map stretches both of the latter, and it is precisely
    that fault that would make the separation guard unreliable: measured, the
    mean gives ``Ancient_vs_kaljukostaja`` the ratio 3.14 against 3.88-3.92
    for the two other Ancient demos, the median 3.70 against 3.82-3.95.
    """
    return median(math.dist(point, centre) for point in points)


def _biggest_crowd(
    by_area: Mapping[str, set[str]], max_areas: int
) -> tuple[tuple[str, ...], set[str]]:
    """The largest crowd that stands on at most ``max_areas`` of these areas.

    **The crowd is a union and not a sum**, and that is why the areas cannot
    simply be taken in order of size. Ranking by per-area count is maximal
    only while every player is on exactly one area; a player who appears on
    two of them at the same sample point is counted twice by the ranking, and
    the two biggest areas can then hold fewer distinct players than a smaller
    pair. Measured, that silences a real hit -- and a blind spot is the worse
    direction, because it cannot be corrected afterwards.

    Two rules decide between combinations, and the second is not tidying:

    * **The most players.** That is the question the rule asks.
    * **Then the fewest areas.** An area that brings no player the others do
      not already have is not where the crowd is standing, and naming it on
      the row would be a place without an observation. It also keeps the hit's
      own invariant true by construction -- every area on the list holds at
      least one of the crowd, so there can never be more areas than players,
      which is otherwise reachable whenever ``max_areas > min_players``
      (``stack_max_areas = 5`` is a legal setting).

    Ties beyond that are broken by the areas' own order -- the biggest first,
    equal ones by name -- so that the row reads the same from one run to the
    next. The order cannot change the player count; it decides only which of
    two equally large areas is named first.

    Args:
        by_area: The group's areas and the distinct players on each.
        max_areas: ``[thresholds].stack_max_areas``.

    Returns:
        The chosen areas in the row's order (the largest first, ties by name)
        and the distinct players standing on them. With no areas at all, an
        empty pair.
    """
    ranked = sorted(by_area, key=lambda name: (-len(by_area[name]), name))
    best: tuple[str, ...] = ()
    best_crowd: set[str] = set()
    # Combinations are generated over the ranked order, so the first
    # combination that reaches a given crowd size is the one this order
    # prefers -- the comparison below keeps it and only a strictly bigger
    # crowd, or the same crowd on fewer areas, replaces it.
    for size in range(1, min(max_areas, len(ranked)) + 1):
        for combination in itertools.combinations(ranked, size):
            crowd: set[str] = set()
            for area in combination:
                crowd |= by_area[area]
            if len(crowd) > len(best_crowd):
                best, best_crowd = combination, crowd
    return best, best_crowd


def _is_ct_time_row(row: AreaPresence) -> bool:
    """Does the row count for an anomaly rule at all.

    Three conditions in one place, because all three rules need all of them: a
    living **CT** player **at a time sample point**. A first contact row does
    not count, because its ``sample_t_s`` is a measured moment -- it would
    pass the time bound according to when on the round somebody happened to
    shoot.
    """
    return (
        row.is_alive
        and row.side == ANOMALY_RULE_SIDE
        and row.sample_kind == TIME_SAMPLE
    )


def _players_by_point(
    presences: Iterable[AreaPresence],
    t_areas: Mapping[str, AreaObservations],
    max_sample_s: float,
) -> dict[tuple[float, str], set[str]]:
    """``(sample point, area) -> distinct players``, T areas and in time only.

    The players are a **set**: a duplicated row for the same player must not
    raise the player count, because the player count is precisely the
    report's number.
    """
    found: dict[tuple[float, str], set[str]] = {}
    for row in presences:
        if not _is_ct_time_row(row):
            continue
        area = normalize_area(row.area)
        if area is None or area not in t_areas or row.sample_t_s > max_sample_s:
            continue
        found.setdefault((row.sample_t_s, area), set()).add(row.player_id)
    return found


def _source_areas(
    rows: Sequence[AreaPresence], lookback_s: float
) -> dict[tuple[str, float], str]:
    """``(player, sample point) -> the area the player was on earlier``.

    "Earlier" is ``lookback_s`` **seconds** and not "one sample point back"
    (Story 4.6). The rule asks where a player came from, and a question asked
    in sample points is a different question on every grid: at four points the
    previous point is 9 s back at 15 s and 15 s back at 30 s, at fourteen
    points it is 3 s back at both. The duration is the same question
    everywhere.

    **Which point answers it** is
    :func:`~pappascout.constants.source_point_index`'s decision: the latest
    point at or before ``t - lookback_s``. It is in ``constants`` and not here
    because the settings' load-time check uses the same selector to prove that
    the crunch can fire at all on the configured grid; as two copies they
    would agree only today. Measured against the four-point grid, the shipped
    ``lookback_s = 9`` reproduces it exactly: at 15 s ``15 - 9 = 6`` is a
    point, and at 30 s ``30 - 9 = 21`` is not, so the answer falls back to
    15 s -- which is what the previous-point rule read there.

    **The question is the same on every grid; the answer is not, and that is
    measured.** The grid decides how nearly the look-back can be honoured, so
    a denser grid answers the same question more precisely rather than
    differently in kind -- but it does answer it differently. Same demo, four
    points against fourteen, shipped look-back: of 320 source look-ups the two
    grids share, **67 resolve to a different area** (at 15 s 0 of 136, at 30 s
    40 of 107, at 45 s 27 of 77), because ``30 - 9`` lands on the 15 s point
    on one grid and on the 21 s point on the other. The residual is pinned by
    :func:`tests.test_calibration.test_the_look_backs_answer_still_depends_on_the_grid`,
    which runs the two grids against each other on the archive's one demo that
    exists on both.

    Named areas only: ``None`` is not a direction. A missing key therefore
    means two things at once -- no sample point that far back on this round,
    or an unknown area there -- and both are the same answer: the player did
    not arrive from anywhere.

    **The sample point is collapsed first and only then looked up.** Without
    that, a duplicated row for the same player at the same sample point could
    make the source area the target area -- and ``source == area`` would
    silence the arrival altogether. A duplicated row is not theoretical: the
    same guard is already in :func:`_players_by_point`, where the players are
    a set. If the same player is on two different areas at the same sample
    point, the table is contradictory; the alphabetically first one is then
    chosen, so that the result is the same from one run to the next.
    """
    # (player, sample point) -> the areas as a set. The set collapses a
    # duplicated row into one observation before the look-up.
    by_point: dict[tuple[str, float], set[str]] = {}
    for row in rows:
        area = normalize_area(row.area)
        seen = by_point.setdefault((row.player_id, row.sample_t_s), set())
        if area is not None:
            seen.add(area)

    seconds_by_player: dict[str, list[float]] = {}
    for player, seconds in by_point:
        seconds_by_player.setdefault(player, []).append(seconds)

    sources: dict[tuple[str, float], str] = {}
    for player, seconds_list in seconds_by_player.items():
        ordered = sorted(seconds_list)
        for later in ordered:
            index = source_point_index(ordered, later, lookback_s)
            if index is None:
                # The look-back reaches past this round's first sample point.
                # No source, and the round's start is not reached behind.
                continue
            areas = by_point[(player, ordered[index])]
            if areas:
                sources[(player, later)] = min(areas)
    return sources


def _first_matching(
    events: Iterable[DamageEvent],
    bounds: RoundBounds,
    excluded: Collection[str],
) -> int | None:
    """The smallest tick at which an event meets the conditions for contact."""
    ticks = [e.tick for e in events if _is_contact(e, bounds, excluded)]
    return min(ticks) if ticks else None


def _is_contact(
    event: DamageEvent, bounds: RoundBounds, excluded: Collection[str]
) -> bool:
    """Is the event a cross-side, armed contact on this round."""
    if bounds.freeze_end_tick is None or bounds.end_tick is None:
        return False
    if not bounds.freeze_end_tick <= event.tick <= bounds.end_tick:
        return False
    if event.attacker_id is None or event.victim_id is None:
        # Damage caused by the world: a fall or a bomb with no planter.
        return False
    if event.attacker_id == event.victim_id:
        return False
    if event.attacker_side not in SIDES or event.victim_side not in SIDES:
        return False
    if event.attacker_side == event.victim_side:
        # Friendly fire. It says nothing about the opponent's setup.
        return False
    weapon_name = normalize_weapon(event.weapon)
    if weapon_name is None:
        # An unknown or empty weapon name does not count as contact. An empty
        # name is not on the exclusion list, so it would otherwise pass just
        # like a hit fired with a rifle -- and first contact could move
        # earlier, to a moment whose source is not known.
        return False
    return weapon_name not in excluded


def _check_tick_rate(tick_rate: float) -> None:
    if not tick_rate > 0:
        raise ValueError(
            f"The tick rate {tick_rate!r} does not do for sampling: it has to "
            "be positive, otherwise seconds cannot be converted into ticks."
        )


def _unique_sorted_seconds(sample_seconds: Sequence[float]) -> list[float]:
    """The sample points in ascending order, with duplicates removed."""
    values = [float(s) for s in sample_seconds]
    negative = [s for s in values if s < 0]
    if negative:
        raise ValueError(
            f"The sample point {negative[0]:g} s is negative. Sample points "
            "are measured forward from the end of freezetime, so a negative "
            "value would point into freezetime, where the players have not "
            "moved yet."
        )
    return sorted(set(values))
