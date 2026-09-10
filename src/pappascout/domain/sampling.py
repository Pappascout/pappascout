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
orientation at all**: it asks whether the subject's own defence has piled into
one site's group. Its derived input is :func:`site_groups` -- a mapping
``area -> "A" | "B"`` from the demo's own point cloud -- and it is in this same
module as the rule, so that the rule and its input cannot disagree. The same
justification as with the orientation: **no map database, no human-supplied
area division, no table accumulating across the archive**. An accumulating
source would give the same demo a different result depending on what other
demos are in the archive.

The three rules are three different questions about the same observation, and
not one of them is a stricter or a looser form of another.

An empty result is a **valid result** and not a shortfall: a demo with no
anomalies in it is an observation that there were no anomalies.

The module is pure: no files, no demoparser2, no settings. That is why it can
be tested with hand-built records, and every row of the I/O matrix is one
function call away here.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import median

from pappascout.constants import (
    SAVING_ROUND_TYPES,
    SIDES,
    SITE_AREAS,
    SITE_GROUPS,
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
    "CT_ADVANCE",
    "CRUNCH",
    "STACK",
    "SITE_AREAS",
    "SITE_GROUPS",
    "SPAWN_AREAS",
    "AreaObservations",
    "AreaPresence",
    "AnomalyHit",
    "CloudCell",
    "t_side_shares",
    "site_groups",
    "ct_advance_hits",
    "crunch_hits",
    "stack_hits",
    "RULE_SIDE",
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


#: CT advance: the subject's CT player in an area that is held by the T side
#: **in that demo**, on a saving round.
CT_ADVANCE = "ct_advance"

#: The side whose rows the anomaly rules examine.
#:
#: All three rules ask what **the subject does as CT**, so T-side rows cannot
#: produce a hit under any of them. A constant because the same value is
#: needed in two places: in the filtering of the rows
#: (:func:`_is_ct_time_row`) and in the aggregation's coverage figure, which
#: says on how many rounds the rule **can** hit. Written out twice, the
#: coverage could promise more than the rule examines.
RULE_SIDE = "CT"

#: Crunch: the same area, but at least two players having **arrived** from at
#: least two different directions at the same time -- **on any round type**.
#: The same orientation condition as the advance, one requirement more and one
#: restriction fewer, so the rules' hit sets intersect each other and neither
#: contains the other.
CRUNCH = "crunch"

#: Stack: at least ``min_players`` of the subject's living CT players in the
#: same site's group, and at least one of them on the site's **own** area.
#:
#: The rule reads neither the orientation nor the round type. It is a third
#: question about the same observation, not a variant of the other two.
STACK = "stack"

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

    **A field belongs to the rule that measured it.** Four fields are
    rule-specific, and :meth:`__post_init__` requires them from exactly the
    right rule. Without the guard a hit could carry a number its rule did not
    compute -- and the report's row would claim as measured something that was
    not measured. The same justification ``sources`` already had: an empty
    source-area list on an advance row means "not asked", not "no directions".

    Attributes:
        rule: :data:`CT_ADVANCE`, :data:`CRUNCH` or :data:`STACK`.
        area: The area the hit was observed on. Never ``None``: an area with
            no name cannot be the T side's area. On a stack it is the
            **site's own area** (:data:`SITE_AREAS`), because that is the
            group's anchor and the rule's extra condition -- not the area
            that happened to hold the most players.
        sample_t_s: The sample point the hit was observed at.
        players: The number of distinct players. On the advance every CT
            player on the area, on the crunch only those who **arrived** (see
            :func:`crunch_hits`), on the stack those in the group.
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
                    f"A stack hit claims {self.players} players in the group "
                    f"when {self.alive} are alive. Those in the group are a "
                    "subset of those alive."
                )
        else:
            if self.alive is not None or self.site is not None:
                raise ValueError(
                    f"The hit {self.rule!r} on area {self.area!r} carries the "
                    "stack's fields (alive, site group), although the rule "
                    "does not measure them."
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


def t_side_shares(
    orientation: Mapping[str | None, AreaObservations],
    *,
    t_share_min: float,
    min_observations: int,
) -> dict[str, AreaObservations]:
    """The areas that are held by the T side **in this demo**.

    Both anomaly rules read an area's side orientation from this same
    function. That is not code thrift but definition: the rules ask the same
    question, and with two computations they could disagree about whose area
    an area is.

    Args:
        orientation: Area -> its observations. The key ``None`` (the area's
            name could not be obtained) is **skipped**: a nameless area cannot
            be either side's area, and a hit "on an unknown area" would not
            say where.
        t_share_min: ``[thresholds].advance_t_share``.
        min_observations: ``[thresholds].advance_area_min_observations``. The
            comparison is ``>=``: **an area exactly at the bound counts**, and
            an area with exactly 20 observations is therefore included. The
            precision carries weight here, because the whole calibration of
            the thresholds leans on exact bounds (Nuke's outside is exactly
            0.70). An area that falls **below** the bound is neither the T
            side's nor the CT side's area -- an orientation is not guessed
            from a thin observation.

    Returns:
        Area -> observations, only for the areas that passed the thresholds.

    Raises:
        ValueError: If ``t_share_min`` is not in the range 0..1 or
            ``min_observations`` is not positive. Either would silently make
            the rule impossible or fire it on every area.
    """
    if not 0.0 <= t_share_min <= 1.0:
        raise ValueError(
            f"The T share threshold {t_share_min!r} is not in the range 0..1. "
            "The share is the number of T observations divided by all of the "
            "area's observations, so a threshold outside it would either "
            "silence the rule altogether or make every area the T side's."
        )
    if min_observations < 1:
        raise ValueError(
            f"The area's minimum observation count {min_observations!r} is "
            "not positive. Without an observation an area has no orientation, "
            "and it must not be guessed."
        )
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
        if obs.total >= min_observations and obs.t_share >= t_share_min:
            passed[area] = obs
    return passed


def site_groups(
    cells: Iterable[CloudCell],
    *,
    margin: float,
    separation_min: float,
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

    **A map whose sites do not separate goes quiet.** On Nuke ``BombsiteA``
    and ``BombsiteB`` are on top of each other on different floors, so *any*
    A/B distance measure is meaningless there. The guard is a ratio and not a
    list of maps: the distance between the sites' centres divided by the
    sites' own size is 0.47-0.54 on Nuke and 3.70-5.04 on the three other
    maps, so a threshold of 2.0 separates them cleanly **without naming a map
    in the code**. Going quiet is the right answer and not a shortfall -- but
    it has to be recorded in the coverage
    (``AnomalyScan.demos_without_site_groups``), not left silent.

    **The grid size cancels out, and that is why it is not given.** A cell
    index is not a coordinate: the real coordinate is
    ``cell * [parse].callout_grid_units``. Both thresholds are, however,
    **quotients of two distances**, and the grid size multiplies every
    distance by the same number, so it cancels out of both comparisons. That
    is exactly why this function -- and the aggregation as its caller -- does
    not read the ``[parse]`` section at all. **If an absolute distance limit
    is ever added here, the conversion is mandatory**, and its source does not
    yet exist in the aggregation.

    Args:
        cells: The demo's point cloud cells. In any order; an empty cloud is
            a valid input and produces ``None``.
        margin: ``[thresholds].stack_group_margin``. At least 1.0.
        separation_min: ``[thresholds].stack_site_separation_min``.

    Returns:
        ``area -> "A" | "B"`` for the areas that have a group, or ``None`` if
        the map has no A/B division that separates evenly. Areas without a
        group are **absent** from the mapping; a ``None`` value is not
        written, so that ``groups.get(area)`` is unambiguous.

        **The result cannot be an empty mapping.** A site's distance to its
        own centre is 0, so each site always belongs to its own group under
        any margin; if the function gets this far, the mapping holds at least
        those two. An empty dictionary is therefore possible only as a
        caller's own value (in a test, for instance), not as this function's
        result, and the code must not lean on telling it apart from ``None``.

    Raises:
        ValueError: If ``margin`` is below 1.0 or ``separation_min`` is not
            positive. The former would make the "nearer" one the further one,
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
    if separation < separation_min * span:
        return None

    found: dict[str, str] = {}
    for area, centre in centres.items():
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
    area_min_observations: int,
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
        area_min_observations: ``[thresholds].advance_area_min_observations``.
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
        min_observations=area_min_observations,
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
    area_min_observations: int,
    max_sample_s: float,
    min_players: int,
    min_sources: int,
) -> list[AnomalyHit]:
    """One round's crunches.

    The rule reads the same orientation as :func:`ct_advance_hits`, but it
    also requires the players to have **arrived** on the area from at least
    ``min_sources`` different directions at the same time. A source area is
    the player's own area at the **previous** time sample point, that is, an
    observation -- not map geometry and not a table of neighbouring areas.

    **A crunch is not limited to saving rounds even though the advance is.**
    The epic sets the economic condition on the advance only, and the
    measurement supports it: one of the five crunches (MatureMayhem Anubis
    round 10) is a full buy. So the rule is not a "stricter form" of the
    advance: it is stricter about directions and looser about the round type,
    so the hit sets intersect each other.

    ``players`` is the number of those who **arrived** and not of those on the
    area: a player who was already on the area at the previous sample point
    did not arrive there from anywhere. The same sample point can therefore
    produce an advance hit with three players and a crunch hit with two, and
    those are two different observations of the same moment.

    A player whose previous area is not known (a nameless area or the round's
    first sample point) **has not arrived from anywhere**: no direction is
    guessed.

    Args:
        presences: The round's sample point rows, in any order. Because of the
            source areas, **all** of the round's time sample points have to be
            included, also the ones after ``max_sample_s`` -- otherwise the
            previous sample point can be missing and the arrival would go
            unseen.
        orientation: As in :func:`ct_advance_hits`.
        t_share_min: ``[thresholds].advance_t_share``, **shared** with the
            advance.
        area_min_observations: ``[thresholds].advance_area_min_observations``.
        max_sample_s: ``[thresholds].advance_max_sample_s``.
        min_players: ``[thresholds].crunch_min_players``.
        min_sources: ``[thresholds].crunch_min_sources``.

    Returns:
        The hits in the order ``(sample_t_s, area)``.
    """
    t_areas = t_side_shares(
        orientation,
        t_share_min=t_share_min,
        min_observations=area_min_observations,
    )
    if not t_areas:
        return []
    rows = [row for row in presences if _is_ct_time_row(row)]
    previous = _previous_areas(rows)

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
    max_sample_s: float,
    min_players: int,
) -> list[AnomalyHit]:
    """One round's stacks.

    The rule: **at least** ``min_players`` of the subject's living CT players
    in the same site's group at one time sample point, **and at least one of
    them on the site's own area** (:data:`SITE_AREAS`).

    The two extra conditions are not fine-tuning but definition:

    * **The site's own area.** The players' own phrase "Stack sitellä" means
      being on the site, not being on that half of the map. Without the
      condition Ancient's ``Alley`` alone produces hits at 6 s -- it is the
      CT spawn's exit corridor, not a site. Measured: the condition drops
      17 rounds -> 9 (26 hits -> 10).
    * **The spawns out** (:data:`SPAWN_AREAS`). Standing in spawn is not
      defending a site, and ``CTSpawn`` falls into the A group on Ancient and
      into the B group on Inferno -- without the restriction the starting
      setup alone would fire the rule on both maps.

    The rule **is not limited by round type** and does not read the area's
    orientation. So it is not a stricter or a looser form of either of the
    other two rules but a third question about the same observation.

    Args:
        presences: The round's sample point rows, in any order. Anything other
            than living CT rows from the time sample points is skipped here.
        groups: The result of :func:`site_groups` for this demo. ``None`` (the
            map has no A/B division that separates evenly) **silences the
            rule**, and that is the right answer and not a shortfall -- but
            the caller has to record it in the coverage, not leave it silent.
        max_sample_s: ``[thresholds].advance_max_sample_s``. **Shared** with
            the two other rules and not a threshold of its own: three rules
            ask about the same observation, and with two time bounds they
            could disagree about when the start of the round ends.
        min_players: ``[thresholds].stack_min_players``.

    Returns:
        The hits in the order ``(sample_t_s, area)``. An empty list is a valid
        result; ``groups=None`` also produces an empty list, and **the two
        cannot be told apart from here** -- the difference is in the coverage.

    Raises:
        ValueError: If ``min_players`` is not positive, or if ``groups`` names
            a group that does not exist. The former would fire the rule at
            every sample point, the latter would mean that the groups come
            from somewhere other than :func:`site_groups`.
    """
    if min_players < 1:
        raise ValueError(
            f"The stack's minimum player count {min_players!r} is not "
            "positive. With zero the rule would hit at every sample point at "
            "which there is nobody in the site's group."
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
    alive: dict[float, set[str]] = {}
    # (sample point, group) -> player -> their areas. The players are the keys
    # and not the rows: a duplicated row for the same player must not raise
    # the player count, because that count is precisely the report's number.
    members: dict[tuple[float, str], dict[str, set[str]]] = {}
    for row in presences:
        if not _is_ct_time_row(row) or row.sample_t_s > max_sample_s:
            continue
        alive.setdefault(row.sample_t_s, set()).add(row.player_id)
        area = normalize_area(row.area)
        if area is None or area in SPAWN_AREAS:
            continue
        group = groups.get(area)
        if group is None:
            continue
        members.setdefault((row.sample_t_s, group), {}).setdefault(
            row.player_id, set()
        ).add(area)

    hits: list[AnomalyHit] = []
    for (seconds, group), by_player in members.items():
        if len(by_player) < min_players:
            continue
        site = SITE_AREAS[group]
        if not any(site in areas for areas in by_player.values()):
            continue
        hits.append(
            AnomalyHit(
                rule=STACK,
                area=site,
                sample_t_s=seconds,
                players=len(by_player),
                alive=len(alive[seconds]),
                site=group,
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
        and row.side == RULE_SIDE
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


def _previous_areas(
    rows: Sequence[AreaPresence],
) -> dict[tuple[str, float], str]:
    """``(player, sample point) -> the area at the previous sample point``.

    Named areas only: ``None`` is not a direction. A missing key therefore
    means two things at once -- the round's first sample point or an unknown
    previous area -- and both are the same answer: the player did not arrive
    from anywhere.

    **The sample point is collapsed first and only then paired up.** Without
    that, a duplicated row for the same player at the same sample point would
    pair up with itself, so the source area would become the target area --
    and ``source == area`` would silence the arrival altogether. A duplicated
    row is not theoretical: the same guard is already in
    :func:`_players_by_point`, where the players are a set. If the same player
    is on two different areas at the same sample point, the table is
    contradictory; the alphabetically first one is then chosen, so that the
    result is the same from one run to the next.
    """
    # (player, sample point) -> the areas as a set. The set collapses a
    # duplicated row into one observation before the pairing up.
    by_point: dict[tuple[str, float], set[str]] = {}
    for row in rows:
        area = normalize_area(row.area)
        seen = by_point.setdefault((row.player_id, row.sample_t_s), set())
        if area is not None:
            seen.add(area)

    seconds_by_player: dict[str, list[float]] = {}
    for player, seconds in by_point:
        seconds_by_player.setdefault(player, []).append(seconds)

    previous: dict[tuple[str, float], str] = {}
    for player, seconds_list in seconds_by_player.items():
        ordered = sorted(seconds_list)
        for earlier, later in zip(ordered, ordered[1:]):
            areas = by_point[(player, earlier)]
            if areas:
                previous[(player, later)] = min(areas)
    return previous


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
