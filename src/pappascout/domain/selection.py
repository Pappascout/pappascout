"""Match selection by the roster threshold (Story 3.3).

The module is **pure**: it knows nothing of files, of HTTP, or of FACEIT's
vocabulary. In comes :class:`MapCandidate` -- one map in one match, two sets
of players and whether the match is a league match -- and out comes
:class:`MapSelection`, which is a row of the selection file. ``stages.select``
does the translation from the vocabulary of the indexes, so this module's
rules can be tested with hand-built sets, without the network and without the
archive.

Five rules this module keeps
----------------------------

**The threshold is a set operation, not a new concept.** The standing roster
is a set of SteamID64s (``domain.teams.Team.player_ids``), the map's lineup is
another set of SteamID64s, and the decision is the size of their intersection.
Nicknames exist only for the reason text; not one decision depends on them.

**The roster class is a prediction before the parse and an observation after
it.** FACEIT's ``roster`` is **per match**, not per map, but the league allows
two substitutions between maps -- the real lineup of a map is visible only in
the demo. The row says which of the two it is (:data:`ROSTER_SOURCES`),
exactly like ``map_name_source`` in Story 2.11. When both are known, **the
observation wins** and the difference from the match roster is stated -- a
substitution between maps is precisely the thing the threshold is assessed per
map for, and it must not be silenced.

**Four regulars and one outsider is enough.** The product owner on
2026-09-04: *"the match is against the same team even if in another match they
had one substitute player."* The outsider's positions are counted in; the
class distinguishes ``5/5`` from ``4/5`` so that the report can tell those
rounds apart.

**A map in the veto data is not proof that the map was played.** Some
three-map matches end after two, and the veto still holds three names. A map
that **may have gone unplayed** gets a row but does not reach the sample --
and the row gives exactly that as the reason (:func:`guaranteed_maps`). A
parsed demo is proof: one does not exist for a map that was not played, so an
observation restores certainty.

**A rejection always has a readable reason.**
:attr:`MapSelection.roster_reason` is never empty -- not on an accepted row
and not on a rejected one. The user does not write code, so "this map did not
qualify" without the numbers would be a decision they cannot check. The reason
says how many were found, what the threshold is, who the outsiders were and
**where the information came from**.

What this module does **not** do
--------------------------------
It does not infer whether a match has been played: no :class:`MapCandidate` is
built at all from an unplayed match (measured 2026-09-04, ``map_picks`` is
empty in 60 of 66 matches -- a scheduled match is not "a selection pending"
but "does not exist yet"). Nor does it infer ``is_league``: that is a
comparison between ``competition_id`` and the ``[league].championship_ids``
list, and the comparison is made by the stage, which can see the settings.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import Final, Literal

from pappascout.constants import ROSTER_CLASSES, RosterClass
from pappascout.errors import SettingsError

__all__ = [
    "ROSTER_SOURCES",
    "RosterSource",
    "ROSTER_SOURCE_FI",
    "map_demo_id",
    "guaranteed_maps",
    "class_labels",
    "MapCandidate",
    "MapSelection",
    "evaluate",
    "select_maps",
    "sort_key",
    "counts",
]

#: How the map's lineup is known.
#:
#: ``observed``
#:     From the demo's lineup table (``parsed/<map_demo_id>/lineups.parquet``).
#:     This is who **were** on the map.
#: ``predicted``
#:     From the match roster. This is who were **expected** to be on the map --
#:     FACEIT's roster is per match, and substitutions are allowed between
#:     maps.
#:
#: The order is the order of precedence: observation before prediction.
ROSTER_SOURCES: Final[tuple[str, ...]] = ("observed", "predicted")
RosterSource = Literal["observed", "predicted"]

#: The name of the source for the user's console output and for the reason.
#:
#: The ``_FI`` suffix is a leftover from the time when the console spoke
#: Finnish. This is **console vocabulary, not report content** (AD-11): the
#: value reaches the screen through ``roster_reason`` in the ``select``
#: command's output, and never through ``render/``. It is therefore in
#: English, unlike the report vocabulary that keeps the same suffix.
ROSTER_SOURCE_FI: Final[dict[str, str]] = {
    "observed": "observation",
    "predicted": "prediction",
}


def map_demo_id(match: str, index: int) -> str:
    """The unit's id: ``{match_id}-{map_index}`` (AD-7).

    ``map_index`` is the **0-based index** into the match's list of maps, and
    this is where it is first written into an id -- ``parse`` and ``classify``
    receive the id ready-made. The ``map_no`` shown to the user is
    ``map_index + 1``, and it is never written into the id.

    >>> map_demo_id("1-f6a06dc8", 0)
    '1-f6a06dc8-0'

    Raises:
        ValueError: If ``index`` is negative or ``match`` is empty. Neither
            can produce an id that would find anything, and a silent ``"-1"``
            suffix would point at a file that does not exist.
    """
    if not match:
        raise ValueError("map_demo_id needs a match id; it was empty.")
    if index < 0:
        raise ValueError(
            f"map_index cannot be negative, it was {index}. The index is the "
            "0-based place in the match's list of maps."
        )
    return f"{match}-{index}"


def guaranteed_maps(best_of: int | None) -> int | None:
    """How many maps a ``best_of`` match **certainly** plays.

    The match ends once one side has won ``best_of // 2 + 1`` maps, so at
    least that many maps are always played -- and the rest only if the match
    is not yet decided.

    >>> [guaranteed_maps(n) for n in (1, 2, 3, 4, 5)]
    [1, 2, 2, 3, 3]

    **BO2 is entirely certain, and that is a result of the formula rather
    than an exception:** you cannot win two of two maps before both have been
    played. In the league's regular season (measured 2026-09-04: ``best_of``
    is ``2`` in all 66 matches) not one row is therefore left uncertain. In
    BO3 playoffs the third map is -- and that is exactly why this rule exists.

    Args:
        best_of: The length of the match in maps, or ``None``.

    Returns:
        The number of maps certainly played, or ``None`` if the length of the
        match is not known. ``None`` is a different thing from zero: it means
        nothing can be said about certainty either way.
    """
    if best_of is None or best_of < 1:
        return None
    return best_of // 2 + 1


def class_labels(roster_size: int, roster_min_regulars: int) -> tuple[str, str]:
    """The roster class names from the settings values: ``("5/5", "4/5")``.

    The names are **derived from the thresholds** rather than written by hand,
    so that the class cannot lie about the setting. This is at the same time
    the place that binds the ``[thresholds]`` values to the
    :data:`~pappascout.constants.ROSTER_CLASSES` list: if the thresholds are
    changed, the class name stops being valid for the ``CLASSIFIED`` schema's
    enum, and it is better to hear that here than three stages later as a
    Polars type error.

    >>> class_labels(5, 4)
    ('5/5', '4/5')

    Args:
        roster_size: How many players are on a map.
            ``[thresholds].roster_size``.
        roster_min_regulars: How many of them have to come from the standing
            roster. ``[thresholds].roster_min_regulars``.

    Returns:
        ``(full, partial)`` -- the class when everybody is from the standing
        roster, and the class when the threshold is met but one is an
        outsider.

    Raises:
        ~pappascout.errors.SettingsError: If the thresholds are invalid, or if
            the name derived from them is not in the ``ROSTER_CLASSES`` list.
            **A settings error, not a program error**: both values come from
            ``settings.toml``, and each is a valid ``PositiveInt`` in itself.
            A bare ``ValueError`` would reach the command line as "Unexpected
            error -- a program error", although the fix is in the user's
            settings file.
    """
    if roster_size < 1:
        raise SettingsError(
            f"The setting [thresholds].roster_size is {roster_size}, but a map "
            "always has at least one player. Correct the value in "
            "settings.toml."
        )
    if not 1 <= roster_min_regulars <= roster_size:
        raise SettingsError(
            f"The setting [thresholds].roster_min_regulars is "
            f"{roster_min_regulars}, but it has to be in the range "
            f"1..{roster_size} ([thresholds].roster_size). Correct the value "
            "in settings.toml."
        )
    full = f"{roster_size}/{roster_size}"
    partial = f"{roster_min_regulars}/{roster_size}"
    unknown = [name for name in (full, partial) if name not in ROSTER_CLASSES]
    if unknown:
        raise SettingsError(
            f"The roster class derived from the thresholds "
            f"roster_size={roster_size} and "
            f"roster_min_regulars={roster_min_regulars} is "
            f"{', '.join(unknown)}, which is not among the known classes "
            f"({', '.join(ROSTER_CLASSES)}).\n"
            "The roster class is also an enum value of the classified table, "
            "so it cannot be invented at run time. Return the thresholds in "
            "settings.toml to values that produce a known class."
        )
    return full, partial


@dataclass(frozen=True)
class MapCandidate:
    """One MapDemo before the inference. The module's input.

    Attributes:
        map_demo_id: The unit's id, ``{match_id}-{map_index}``.
        match_id: The match this map belongs to.
        map_index: The 0-based place in the match's list of maps.
        map_name: The map's name from the veto data, or ``None``. **Never a
            basis for the decision** -- it is there to make the row readable.
            The final name of the map is read from the demo header (Story
            2.11), and this is the veto data's observation.
        is_league: Whether the match is on the ``[league].championship_ids``
            list. The comparison is made by the stage; this is its result.
        certainly_played: Whether the map was certainly played. ``False``
            means the map is in the veto data but the match may have been
            decided before it (see :func:`guaranteed_maps`). The stage
            computes this from ``best_of``; ``True`` is the default, because
            without the length of the match there is no reason to doubt any
            map.
        match_roster: The match roster -- the **prediction** of who were on
            the map. FACEIT's ``roster``, that is the starters, not the
            substitutes: the bench is not on the map, and counting it in would
            predict ten players for five places.
        observed_players: The map's lineup from the demo, or ``None`` if the
            demo has not been parsed. ``None`` and an empty set are
            **different things**: the former is "not known", the latter "the
            demo was read and this team was not in it".
        observation_note: An explanation of **why there is no observation**,
            or ``None``. A broken lineup table and a tie between two lineups
            are both "no observation", but neither is the same thing as "the
            demo has not been parsed" -- and without this field the difference
            would vanish without a trace. It travels into the row's reason.
    """

    map_demo_id: str
    match_id: str
    map_index: int
    map_name: str | None = None
    is_league: bool = False
    certainly_played: bool = True
    match_roster: frozenset[str] = frozenset()
    observed_players: frozenset[str] | None = None
    observation_note: str | None = None


@dataclass(frozen=True)
class MapSelection:
    """The selection decision for one MapDemo. A row of the selection file.

    Attributes:
        map_demo_id: The unit's id.
        match_id: The match.
        map_index: The map's place in the match.
        map_name: The map's name from the veto data, or ``None``.
        is_league: Whether the match is from the league.
        roster_ok: Whether the map qualifies for the sample.
        roster_reason: **Always a readable reason**, on an accepted row too.
            It states the numbers and the threshold, so that the decision can
            be checked without opening the demo.
        roster_class: ``"5/5"`` or ``"4/5"``, or ``None`` if the map did not
            qualify. On a rejected row the class would be a claim about rounds
            that are not counted; on an accepted row its absence would make
            the row a sample that neither class counter finds.
        roster_source: ``"observed"`` or ``"predicted"``; see
            :data:`ROSTER_SOURCES`.
        certainly_played: Whether it is known that the map was played. **A
            conclusion, not an input**: true when the length of the match
            guarantees the map (:func:`guaranteed_maps`) **or** when the demo
            has been parsed -- one does not exist for a map that was not
            played. A false row is in the veto data but awaits proof.
        regulars: The map's players who are in the standing roster. Sorted.
        outsiders: The map's players who are not in the standing roster.
            Sorted. **These are counted in** to the sample when the threshold
            is met -- the difference is in the class, not in who is included.
        players_seen: How many players were known to be on the map.
        joined: Players who were in the demo but not in the match roster.
            Empty when there is no observation or no prediction to compare.
        left: Players who were in the match roster but not in the demo.
    """

    map_demo_id: str
    match_id: str
    map_index: int
    map_name: str | None
    is_league: bool
    roster_ok: bool
    roster_reason: str
    roster_class: RosterClass | None
    roster_source: RosterSource
    certainly_played: bool = True
    regulars: tuple[str, ...] = ()
    outsiders: tuple[str, ...] = ()
    players_seen: int = 0
    joined: tuple[str, ...] = ()
    left: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.roster_reason.strip():
            raise ValueError(
                f"The selection row {self.map_demo_id!r} has no reason. A "
                "decision without a reason cannot be checked, and such a row "
                "must not be written."
            )
        if self.roster_source not in ROSTER_SOURCES:
            raise ValueError(
                f"The source of the selection row {self.map_demo_id!r} is "
                f"{self.roster_source!r}, which is not among the known "
                f"sources ({', '.join(ROSTER_SOURCES)})."
            )
        if not self.roster_ok and self.roster_class is not None:
            raise ValueError(
                f"The rejected selection row {self.map_demo_id!r} carries the "
                f"roster class {self.roster_class!r}. The class is a claim "
                "about rounds that are not counted."
            )
        if self.roster_ok and self.roster_class is None:
            raise ValueError(
                f"The accepted selection row {self.map_demo_id!r} has no "
                "roster class. Such a row would be in the sample but in "
                "neither class counter, and nothing would tell the difference."
            )

    @property
    def source_fi(self) -> str:
        """The source as a word: ``observation`` or ``prediction``.

        The ``_fi`` in the name is a leftover from the Finnish console; see
        :data:`ROSTER_SOURCE_FI`.
        """
        return ROSTER_SOURCE_FI[self.roster_source]

    @property
    def drifted(self) -> bool:
        """Did the map's lineup differ from the match roster?"""
        return bool(self.joined or self.left)


def evaluate(
    candidate: MapCandidate,
    *,
    roster: Set[str],
    roster_size: int,
    roster_min_regulars: int,
    names: Mapping[str, str] | None = None,
) -> MapSelection:
    """Decide the fate of one MapDemo as a set operation.

    The checks are in this order, and the order matters: each of them makes
    the next one meaningless, so the first one that applies is always the one
    that says the most.

    1. **The standing roster is unknown** -- without it there is nothing to
       compare against, and falling short of the threshold is not a true
       claim.
    2. **The map may have gone unplayed** -- assessing the lineup of a map
       that may not exist would be a claim about nothing. A parsed demo
       overturns this: it would not exist for an unplayed map.
    3. **The lineup is unknown** -- a different reason depending on whether
       the demo was parsed or not.
    4. **The threshold.**

    Args:
        candidate: The map and its two sets of players.
        roster: The team's standing roster as a set of SteamID64s
            (``domain.teams.Team.player_ids``).
        roster_size: ``[thresholds].roster_size`` -- how many players are on a
            map.
        roster_min_regulars: ``[thresholds].roster_min_regulars`` -- how many
            of them have to come from the standing roster.
        names: SteamID64 -> nickname, for the reason text only. A missing name
            is the id as it stands: the reason is for a human, but an invented
            name would point at the wrong player.

    Returns:
        A :class:`MapSelection`, which always has a reason.

    Raises:
        ~pappascout.errors.SettingsError: If the thresholds are invalid; see
            :func:`class_labels`.
    """
    full_label, partial_label = class_labels(roster_size, roster_min_regulars)
    show = _namer(names)

    observed = candidate.observed_players
    if observed is not None:
        source: RosterSource = "observed"
        on_map = frozenset(observed)
    else:
        source = "predicted"
        on_map = frozenset(candidate.match_roster)

    joined, left = _drift(candidate)
    note = candidate.observation_note

    # The row's ``certainly_played`` is a **conclusion**, not an input: the
    # length of the match guarantees the map, OR a parsed demo proves it.
    # Merely copying the input would leave the row uncertain for ever even
    # though the demo is in the archive -- and the summary would report
    # uncertain maps that have already been proved.
    known_played = candidate.certainly_played or observed is not None

    def rejected(reason: str) -> MapSelection:
        return _row(
            candidate,
            certainly_played=known_played,
            roster_ok=False,
            roster_class=None,
            roster_source=source,
            roster_reason=_with_note(reason, note),
            regulars=(),
            outsiders=(),
            players_seen=len(on_map),
            joined=joined,
            left=left,
        )

    if not roster:
        return rejected(
            "Not eligible: the team's standing roster is not known, so the "
            "threshold cannot be assessed at all."
        )

    # An observation is proof that the map was played: a parsed demo does not
    # exist for a map that was not played.
    if not candidate.certainly_played and observed is None:
        return rejected(
            f"Not eligible: the map is number {candidate.map_index + 1} in the "
            "veto data and the match length does not guarantee that it was "
            "played. The map reaches the sample once its demo has been "
            "downloaded and parsed -- the demo is the proof."
        )

    if not on_map:
        return rejected(_no_players_reason(source))

    regulars = tuple(sorted(on_map & frozenset(roster)))
    outsiders = tuple(sorted(on_map - frozenset(roster)))
    found = len(regulars)

    # The class says how many of the **map's five places** belong to a
    # regular. Without a cap a six-player lineup would produce the class 5/5
    # and the reason "6/6", that is the class and the reason would claim
    # different things.
    counted = min(found, roster_size)
    if counted >= roster_size:
        label: str | None = full_label
        ok = True
    elif counted >= roster_min_regulars:
        label = partial_label
        ok = True
    else:
        label = None
        ok = False

    reason = _reason(
        ok=ok,
        label=label,
        found=found,
        on_map=len(on_map),
        outsiders=outsiders,
        roster_min_regulars=roster_min_regulars,
        roster_size=roster_size,
        source=source,
        joined=joined,
        left=left,
        show=show,
        note=note,
    )
    return _row(
        candidate,
        certainly_played=known_played,
        roster_ok=ok,
        roster_class=label,  # type: ignore[arg-type]
        roster_source=source,
        roster_reason=reason,
        regulars=regulars,
        outsiders=outsiders,
        players_seen=len(on_map),
        joined=joined,
        left=left,
    )


def select_maps(
    candidates: Iterable[MapCandidate],
    *,
    roster: Set[str],
    roster_size: int,
    roster_min_regulars: int,
    names: Mapping[str, str] | None = None,
) -> tuple[MapSelection, ...]:
    """Decide the fate of many MapDemos. See :func:`evaluate`.

    The order is the same as in the input: the stage decides the order, so
    that the difference between two runs can be read as a diff.
    """
    return tuple(
        evaluate(
            candidate,
            roster=roster,
            roster_size=roster_size,
            roster_min_regulars=roster_min_regulars,
            names=names,
        )
        for candidate in candidates
    )


# -- Internal helpers --------------------------------------------------------


def _row(
    candidate: MapCandidate,
    *,
    certainly_played: bool,
    roster_ok: bool,
    roster_class: RosterClass | None,
    roster_source: RosterSource,
    roster_reason: str,
    regulars: tuple[str, ...],
    outsiders: tuple[str, ...],
    players_seen: int,
    joined: tuple[str, ...],
    left: tuple[str, ...],
) -> MapSelection:
    return MapSelection(
        map_demo_id=candidate.map_demo_id,
        match_id=candidate.match_id,
        map_index=candidate.map_index,
        map_name=candidate.map_name,
        is_league=candidate.is_league,
        certainly_played=certainly_played,
        roster_ok=roster_ok,
        roster_reason=roster_reason,
        roster_class=roster_class,
        roster_source=roster_source,
        regulars=regulars,
        outsiders=outsiders,
        players_seen=players_seen,
        joined=joined,
        left=left,
    )


def _drift(candidate: MapCandidate) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The difference between observation and prediction -- a substitution
    between maps.

    The difference is computed **only when both exist and are non-empty**.
    Without the match roster the difference would be the whole observed
    lineup; without the observed lineup it would be the whole match roster,
    and the row would report a "substitution" on a map whose own reason says
    the lineup is unknown.
    """
    observed = candidate.observed_players
    if not observed or not candidate.match_roster:
        return (), ()
    predicted = frozenset(candidate.match_roster)
    return tuple(sorted(observed - predicted)), tuple(sorted(predicted - observed))


def _namer(names: Mapping[str, str] | None):
    known = names or {}

    def show(player: str) -> str:
        return known.get(player) or player

    return show


def _with_note(reason: str, note: str | None) -> str:
    return reason if note is None else f"{reason} {note}"


def _no_players_reason(source: RosterSource) -> str:
    if source == "observed":
        return (
            "Not eligible: the map's lineup is not known. The demo has been "
            "parsed, but it held not one player of this team."
        )
    return (
        "Not eligible: the map's lineup is not known. The match roster is "
        "empty and the demo has not been parsed, so the threshold cannot be "
        "assessed."
    )


def _reason(
    *,
    ok: bool,
    label: str | None,
    found: int,
    on_map: int,
    outsiders: tuple[str, ...],
    roster_min_regulars: int,
    roster_size: int,
    source: RosterSource,
    joined: tuple[str, ...],
    left: tuple[str, ...],
    show,
    note: str | None,
) -> str:
    """One row's reason: the numbers, the threshold, the outsiders, the
    difference and the source.

    The same skeleton for the accepted and the rejected row, because the user
    checks both in the same place. The difference is in the first sentence.
    """
    origin = ROSTER_SOURCE_FI[source]
    origin_text = (
        "lineup from the demo"
        if source == "observed"
        else "lineup from the match roster"
    )
    parts: list[str] = []

    if ok:
        parts.append(
            f"Eligible: {found}/{on_map} of the map's players are in the "
            f"standing roster, class {label}."
        )
    else:
        parts.append(
            f"Not eligible: only {found}/{on_map} of the map's players are in "
            f"the standing roster, and the threshold is "
            f"{roster_min_regulars}/{roster_size}."
        )

    # The class always speaks of five places. If the map held a different
    # number of players, the denominator of the class and the denominator of
    # the sentence differ -- and that is said, so that the reader does not
    # infer from the class an outsider who does not exist.
    if on_map != roster_size:
        parts.append(
            f"Note: the lineup held {on_map} players instead of the expected "
            f"{roster_size}, so the class and the ratio do not share a "
            "denominator."
        )

    if outsiders:
        named = ", ".join(show(player) for player in outsiders)
        parts.append(f"From outside the standing roster: {named}.")
    elif ok and found < roster_size:
        parts.append(
            "There were no outsiders -- the partial class comes from the size "
            "of the lineup, not from a foreign player."
        )

    if joined or left:
        changed: list[str] = []
        if joined:
            changed.append("joined: " + ", ".join(show(p) for p in joined))
        if left:
            changed.append("left: " + ", ".join(show(p) for p in left))
        parts.append(
            "The lineup differs from the match roster: " + "; ".join(changed) + "."
        )

    parts.append(f"Source: {origin} ({origin_text}).")
    return _with_note(" ".join(parts), note)


def sort_key(selection: MapSelection) -> tuple[str, int]:
    """The order of the rows in the file: match, then the map's index."""
    return (selection.match_id, selection.map_index)


def counts(selections: Sequence[MapSelection]) -> dict[str, int]:
    """Summary numbers from the selection rows, for the user's output.

    The numbers are computed **from the rows and not along the way**, so that
    the file and the summary cannot say different things. Two invariants hold:
    ``accepted + rejected == map_demos`` and ``class_5/5 + class_4/5 ==
    accepted`` -- the latter is guaranteed by
    :meth:`MapSelection.__post_init__`.
    """
    accepted = [row for row in selections if row.roster_ok]
    return {
        "map_demos": len(selections),
        "accepted": len(accepted),
        "rejected": len(selections) - len(accepted),
        "league": sum(1 for row in selections if row.is_league),
        "observed": sum(1 for row in selections if row.roster_source == "observed"),
        "predicted": sum(1 for row in selections if row.roster_source == "predicted"),
        "drifted": sum(1 for row in selections if row.drifted),
        "uncertain": sum(1 for row in selections if not row.certainly_played),
        **{
            f"class_{label}": sum(1 for row in accepted if row.roster_class == label)
            for label in ROSTER_CLASSES
        },
    }
