"""The report's view model: **what** the report says.

The module reads :class:`~pappascout.domain.report.Report` and builds rows
and claims out of it. It **computes nothing**: every number is taken from
the model as it is, and there is not one sum, average or quotient here. The
only "logic" is selection -- which observations earn a row -- and
formatting.

The rules that show in every function
-------------------------------------
**Every claim carries its sample.** :class:`Claim` is precisely the pair
"claim + sample", and a claim cannot be built without an ``n`` and an ``m``.
Without the sample one round would look like a pattern.

**Saving rounds and the default are of different shapes.** Pistol, eco,
force and half-buy are described round by round: every observation is
written. Full buys (``full``) and overtime (``ot``) are described **only as
repeating patterns**, and the repetition limit is read from the report
(``thresholds_used.thresholds.small_sample_rounds``) -- it is not invented
here. The number of observations left out is written out, so the filtering
is not silent.

**No interpretations.** The rows tell player counts, grenades and areas. The
words "fake", "rush" or "good" are nowhere -- the conclusion is the
reader's.

**Pruning concerns the presentation and not the content** (Story 2.13). Five
``[report]`` settings leave repetition unwritten: a saturated equipment row,
the other half of an identical pair of equipment rows, a named sample point,
and utility's targets and the kill areas beyond the most common ones. Three
rules hold for every one of them:

**The rows are built first and pruned only afterwards.** The observations
the threshold dropped are counted in the row builder, so a short circuit
before it would shrink the block's note -- that is, pruning would change a
**claim about the data**. The same order also settles when a rule did *not*
remove anything: a row that pattern filtering never let come about has not
been pruned.

**Nothing vanishes in silence.** If a row is left unwritten, the reading
guide says once what its absence means (:func:`_pruning_legend`); if claims
are left off a row, the row states how many were dropped
(:func:`_dropped_note`) -- the same rule as with pattern filtering. The
explanation is written only about a rule that really pruned something, and
it names its setting.

**Some of the round types are protected** (:data:`PROTECTED_ROUND_TYPES`),
and every pruning paragraph of the reading guide says so out loud: the same
report holds unpruned blocks, so an unqualified sentence would be false.

``Report``, ``report.json`` and ``REPORT_SCHEMA_VERSION`` do not change, and
every pruned value is still in them -- it merely goes untold in *this*
report. The measured rationales and the numbers are in ``settings.toml``;
the measurement documents themselves live in the BMAD output and not in this
repository.

**Deaths fit into two rows.** The report already runs to hundreds of rows
when the product owner's own analysis is 30. Deaths were added because they
**explain the other rows** -- not because they are a chapter of their own.
The limit is :data:`MAX_DEATH_LINES`, and exceeding it is an error rather
than silent growth.

**The body speaks in names, and the ids have a chapter of their own.** The
team's and the lineups' digests, the players' SteamID64s and the maps' demo
ids are not in the body but in the chapter :data:`TRACEABILITY_HEADING`. The
rule is here and not only inside the functions, because it concerns six of
them (:func:`_title`, :func:`_team_text`, :func:`_roster_text`,
:func:`_summary`, :func:`_traceability`, :func:`_anomaly_map_label`) --
written inside one of them it would not say that there are exactly three
exceptions:

1. **The round appendix's path.** A path is usable only as it is, and it is
   a reading aid rather than a traceability entry.
2. **The missing demo's row.** The id is part of a command the reader copies
   (``uv run pappascout parse <demo>``); without it the row would not say
   what to do.
3. **A map whose name was not recognised.** Then ``map_name`` *is* the
   ``map_demo_id`` (see :class:`~pappascout.domain.report.MapReport`), that
   is, the id is the map's only name -- the alternative would be a nameless
   map chapter.

The exceptions are said out loud in the reading guide. Without that the
report would claim more about itself than is true, and that is precisely the
mistake that recurs in this file: the text promises an absoluteness the code
does not keep.

Why first contact is shown as a distribution and not as a presence list
-----------------------------------------------------------------------
``Report`` holds first contact twice: ``round_types[].first_contact`` tells
the **presence** (was there a player in the area) and the ``positions``
list's ``first_contact`` sample point tells the **player counts** from the
same moment. The latter contains the former's information and adds a number
to it, so the report uses it. Writing both would repeat the same observation
twice in two shapes, and the report has to be short.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from pappascout.constants import (
    ANOMALY_RULE_FI,
    ANOMALY_RULES,
    ROSTER_BUCKET_FI,
    ROSTER_BUCKETS,
    ROSTER_CLASS_BUCKET,
    ROUND_TYPE_FI,
    SAMPLE_BUCKET_FI,
    SAMPLE_BUCKETS,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
    seconds_label,
)
from pappascout.domain.models import PLAYERS_ON_SERVER, ReportSettings
from pappascout.domain.report import (
    Anomaly,
    AnomalyRound,
    AnomalyScan,
    ArmedPlayers,
    ArmoredPlayers,
    DeathReport,
    Position,
    Report,
    RoundTypeReport,
    UtilityCounts,
    UtilityUse,
)
from pappascout.errors import PappascoutError

__all__ = [
    "GRENADE_TYPE_FI",
    "GRENADE_ORDER",
    "ROUND_TYPE_ORDER",
    "PATTERN_ROUND_TYPES",
    "PROTECTED_ROUND_TYPES",
    "MERGED_EQUIPMENT_LABEL",
    "MAX_DEATH_LINES",
    "KILL_SAMPLE_UNIT",
    "UNKNOWN_AREA",
    "TRACEABILITY_HEADING",
    "ANOMALY_HEADING",
    "MAX_ANOMALY_LINES",
    "UNKNOWN_MAP_LABEL",
    "UNNAMED_PLAYER",
    "Claim",
    "Line",
    "SummaryItem",
    "AnomalyView",
    "RoundTypeView",
    "SideView",
    "MapView",
    "ReportView",
    "build_view",
    "round_list_demo_ids",
    "pattern_min_rounds",
    "rounds_text",
    "demos_text",
    "players_text",
]

#: The grenade types' Finnish names. These are **presentation**, so they live
#: in the render layer and not in ``constants``: ``report.json``'s keys stay
#: English, because they are contract.
#:
#: ``molotov`` and ``incendiary`` are different items in the game but the same
#: class in flight; ``aggregate`` tells them apart by the inventory name, so
#: the report tells them apart too.
GRENADE_TYPE_FI: dict[str, str] = {
    "smoke": "savu",
    "flashbang": "valo",
    "he": "HE",
    "incendiary": "poltto",
    "molotov": "molotov",
    "decoy": "decoy",
}

#: The grenades' presentation order: first the ones that say the most about
#: the plan.
GRENADE_ORDER: tuple[str, ...] = (
    "smoke",
    "flashbang",
    "he",
    "incendiary",
    "molotov",
    "decoy",
)

#: The round types' presentation order: pistol, the saving rounds, then the
#: default.
#:
#: The list covers **every**
#: :data:`~pappascout.constants.ROUND_TYPES` value, and a test watches that.
#: Without the coverage a new round type would vanish from the report in
#: silence.
ROUND_TYPE_ORDER: tuple[str, ...] = (
    "pistol",
    "eco",
    "force",
    "half",
    "anomaly",
    "full",
    "ot",
)

#: The round types about which **only repeating patterns** are told. The
#: product owner: "there is no need to tell what they did on every round, it
#: tries to spot only the broad lines". A full buy is the least clear and the
#: most common round plan, so telling it round by round would be mostly
#: repetition.
PATTERN_ROUND_TYPES: frozenset[str] = frozenset({"full", "ot"})

#: The round types that **no rule prunes** (Story 2.13).
#:
#: Two types, two different rationales -- and in both the **equipment row is
#: the observation** that pruning would remove:
#:
#: ``pistol``
#:     Story 2.8 measured that the armour count is a buy observation only on
#:     the pistol round: elsewhere it is possession, inherited from the
#:     previous round by whoever survived it. The product owner's analysis
#:     treats pistol rounds **round by round** and the other round types as
#:     patterns, and pruning follows the same split -- the same split
#:     :data:`PATTERN_ROUND_TYPES` makes from the other end.
#: ``anomaly``
#:     ``classify`` reserves the type for two situations: **the observation
#:     is contradictory** (the equipment value fell during the buy time) or
#:     **after a win practically nothing was bought**. In both it is the
#:     equipment that made the round an anomaly, so dropping the saturated
#:     row would remove the block's only reason to exist. Unlike with pistol,
#:     the rationale is not buy observation vs. possession but that the block
#:     is **assembled on the strength of this row**.
#:
#: **``ot`` is not protected, and that is a measurement result and not an
#: assumption.** Overtime's first round looks like a pistol round, but
#: ``[league].ot_start_money`` is $12,500 in this league, so a full buy is
#: made in overtime and ``5/5`` is the expectation as it is on a full buy --
#: measured from an archived report, in which the overtime block's
#: ``aseistettuja 5 (3/3)`` and ``panssaroituja 5 (3/3)`` were pruned
#: correctly. **The dependency has to be written out, because it is not
#: obvious:** if ``ot_start_money`` ever falls to pistol level, ``ot`` has to
#: be added to this list.
#:
#: The list is here and not in the settings, because it is not an adjustable
#: value but the bounding of the rule the settings adjust. Changing it is a
#: contract change, and the reading guide names the types out loud
#: (:func:`_protected_round_types_text`), so a sixth rule inherits the
#: explaining of the exception automatically.
PROTECTED_ROUND_TYPES: frozenset[str] = frozenset({"pistol", "anomaly"})

#: The merged equipment row's label (Story 2.13, rule 2).
#:
#: The label names **both** counters, because the row carries both of their
#: numbers: an identical distribution means that the same bar is both the
#: armed and the armoured observation. Using one label ("equipment") would
#: lose which count the row is about, and the reading guide's definitions are
#: precisely the definitions of these two words.
MERGED_EQUIPMENT_LABEL = "aseistettuja ja panssaroituja ostoajan lopussa"

#: At most this many rows about deaths per round type.
#:
#: The report already runs to hundreds of rows when the product owner's own
#: analysis is 30. Deaths were added because they **explain the other rows**
#: -- not because they are a chapter of their own. Two rows: where the first
#: death came from and where the team made its kills. The number is a
#: constant and not a setting, because it is a bound and not an adjustable
#: value; raising it is a contract change ("Ask First").
MAX_DEATH_LINES = 2

#: The kill distribution's sample unit. A constant, because both the row and
#: the reading guide speak of it: written twice, one of them would go on
#: telling of rounds, and that is precisely the sentence that is false.
KILL_SAMPLE_UNIT = "taposta"

#: The name for an area that was not got. Not empty and not left out: an
#: unknown position is a different thing from an empty area.
UNKNOWN_AREA = "tuntematon alue"

#: The mark for an area that is an **estimate** and not an observation: the
#: detonation's area has been read from the nearest cell of the demo's point
#: cloud. Without the mark the report would present an estimate as an
#: observation.
#:
#: The mark stays although the method changed in Story 2.9 from the nearest
#: player to the point cloud. The area is still derived: it is *the game's
#: own* area name for the spot where somebody has stood nearest to the
#: detonation -- not the detonation site's own name, because there is no such
#: thing. A more accurate estimate is still an estimate.
ESTIMATE_MARK = " (arvio)"

#: The traceability chapter's heading **as the template sets it**.
#:
#: The name is here because the report's own text refers to it from two
#: places: the summary's nameless team says where the id is to be found, and
#: the reading guide says the same about every id. The heading line's ``## ``
#: belongs to the template -- structure is the template's business -- so the
#: name appears in two files, and a test guards that they are the same.
#: Without the guard, renaming the chapter would leave two references in the
#: report to a chapter that does not exist.
#:
#: **The chapter is not called an appendix.** The report already has a
#: ``Kierrosliite``, which is a different thing: it points at the
#: ``classify`` stage's round lists in the archive, whereas this chapter is
#: inside the report. Two different things under the same word make either
#: mention ambiguous, so the chapter is spoken of by its own name both in the
#: code and in the tests.
TRACEABILITY_HEADING = "Tekninen jäljitettävyys"

#: The anomaly chapter's heading **as the template sets it**.
#:
#: The same rationale as with :data:`TRACEABILITY_HEADING`: the heading
#: line's ``## `` belongs to the template, but the name appears in the code
#: and in the tests as well, and a test guards that they are the same.
ANOMALY_HEADING = "Poikkeamat"

#: At most this many anomaly rows in the chapter.
#:
#: The same rationale as with :data:`MAX_DEATH_LINES` but a different
#: mechanism: the number of death rows is structural (two marginal
#: distributions), so exceeding it is an error. The number of anomalies is a
#: property of the **data**, so an error would abort the run over data that
#: cannot be chosen -- and the chapter is the report's first content chapter,
#: so neither may it grow without bound.
#:
#: The solution is the same as with the pattern threshold: the bound is
#: applied and **the number left out is written out**. 20 rows is already a
#: whole chapter; a count beyond that means the rule fires too often, and the
#: answer to that is the spec's Ask First gate (a hit rate over 20 %) and not
#: the report's formatting.
MAX_ANOMALY_LINES = 20

#: The label for an unrecognised map. It has to be formatted, because the
#: ordinal is what tells two unrecognised maps apart.
#:
#: **One spelling for two chapters.** Both the traceability chapter's map row
#: (:func:`_map_label`) and the anomaly row (:func:`_anomaly_map_label`) use
#: this, and the reader connects the rows precisely on the strength of the
#: string. Written twice, changing one of them would break the connection in
#: silence.
UNKNOWN_MAP_LABEL = "kartta {index}, nimeä ei tunnistettu"


# -- The view model's parts ------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One claim and its sample.

    A claim cannot be built without a sample: ``n`` is the rounds on which
    the observation was made, ``m`` all of that level's rounds.
    """

    text: str
    n: int
    m: int
    #: Extra information that is not a sample -- for instance the number of
    #: throws when several grenades of the same kind were thrown on the same
    #: round.
    extra: str | None = None
    #: The sample's **unit**. Nearly every claim counts rounds, but the kill
    #: distribution's denominator is kills: a round type can have more kills
    #: than rounds, so "4/6 kierroksesta" would be an outright false sentence
    #: there. The unit is a field and not part of an already formatted
    #: string, so that ``n`` and ``m`` stay numbers in the view.
    unit: str = "kierroksesta"

    @property
    def sample_text(self) -> str:
        return f"{self.n}/{self.m} {self.unit}"


@dataclass(frozen=True)
class Line:
    """One bullet: an optional label and its claims."""

    label: str | None
    claims: tuple[Claim, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SummaryItem:
    """A summary row: a label and a value."""

    label: str
    value: str


@dataclass(frozen=True)
class AnomalyView:
    """One anomaly: the summary row and its round rows.

    Two levels on purpose. The summary row carries the sample and the
    orientation, the round rows what was observed on each round -- and it is
    precisely the round row that stops the row being read wrong: a crunch's
    source directions are simultaneous only within the same round, so the
    union of two rounds would claim more simultaneous directions than were
    observed.

    ``rounds`` is already formatted strings and not :class:`Line` objects:
    they have no sample of their own, so :class:`Claim`'s contract ("a claim
    cannot be built without a sample") does not hold for them. The rows'
    sample is on the summary row they belong under.
    """

    rule: str
    line: Line
    rounds: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoundTypeView:
    """One round type's part on one map and side."""

    round_type: str
    heading: str
    rounds_text: str
    small_sample: bool
    pattern_only: bool
    lines: tuple[Line, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SideView:
    """One side's round types.

    ``note`` is filled in when the side has no round type at all. A bare
    heading without content would look like an interrupted report; a named
    reason says that it was the data that ran out and not the formatting.
    """

    side: str
    heading: str
    rounds_text: str
    round_types: tuple[RoundTypeView, ...]
    note: str | None = None


@dataclass(frozen=True)
class MapView:
    """One map with both of its sides.

    ``heading`` already contains the mention of an unrecognised map;
    ``name_unknown`` is the same information as a flag, for those who read
    the view rather than the text.

    ``note`` as in :class:`SideView`: a map without sides says so out loud.
    """

    map_name: str
    heading: str
    name_unknown: bool
    sides: tuple[SideView, ...]
    note: str | None = None


@dataclass(frozen=True)
class ReportView:
    """The whole report already selected; the template merely sets this."""

    title: str
    summary: tuple[SummaryItem, ...]
    missing_demos: tuple[SummaryItem, ...]
    maps: tuple[MapView, ...]
    legend: tuple[str, ...] = ()
    #: The round appendix's explanation and the paths where the appendix
    #: really is.
    appendix_note: str = ""
    appendix_paths: tuple[str, ...] = ()
    #: The traceability chapter's rows: the ids that are not in the body.
    #:
    #: The default is empty only because the field was added to an existing
    #: class; :func:`build_view` fills it in **always**, because a team has an
    #: id in an empty report too. The template sets the chapter
    #: unconditionally, as it does ``Kierrosliite`` and ``Lukuohje``, so an
    #: empty sequence would produce a bare heading -- a state that does not
    #: exist and is therefore not guarded.
    traceability: tuple[SummaryItem, ...] = ()
    #: The traceability chapter's explanation: why the ids are there and not
    #: in the body.
    traceability_note: str = ""
    empty_note: str | None = None
    #: The anomaly chapter's rows -- every map and side in the same sequence,
    #: as in ``Report.anomalies``. An empty sequence is a valid state.
    anomalies: tuple[AnomalyView, ...] = ()
    #: The text that is read **instead of** the anomaly rows when there are
    #: none. :func:`build_view` always fills in exactly one of these two: the
    #: anomaly chapter exists even when there are no anomalies, and it then
    #: says out loud what was examined and what was left in a blind spot
    #: (:func:`_no_anomalies_text`).
    anomalies_note: str | None = None
    #: The note about the anomalies the row cap dropped. Separate from
    #: :attr:`anomalies_note`, because the two are different states: one is
    #: "none were found", the other "more were found than the chapter shows".
    anomalies_dropped_note: str | None = None


@dataclass
class _Flags:
    """Things observed during the report that are explained once at the end.

    The explanations -- an unknown area, an estimated detonation area, what
    the armed counter means -- are written once at the end of the report and
    not into every block. They therefore have to be collected across the
    whole report, and the collector cannot be replaced by a return value
    without every function starting to return a pair.

    ``dropped`` is a running counter: one round type reads the difference
    between its own opening and closing readings, so the same field serves
    both the whole report's and a single block's bookkeeping.

    **Two different kinds of flag, and the difference matters because of
    pruning** (Story 2.13). ``dropped`` is **pattern filtering's
    bookkeeping**: it says how many observations the threshold dropped, and
    pruning must not change it -- the block's note is a claim about the data.
    All the others are **presentation**: they explain rows that are in the
    report. That is why the row builders are given a :class:`_Flags` of their
    own, merged with :meth:`absorb`, which transfers the bookkeeping always
    and the presentation only if the row stayed in the report.
    """

    unknown_area: bool = False
    estimated_area: bool = False
    armed_shown: bool = False
    armored_shown: bool = False
    kills_shown: bool = False
    dropped: int = 0

    # -- Pruning (Story 2.13). One flag per rule, because the reading guide
    # explains **only the rules that really pruned something**: a rule that
    # never hit would explain a missing row that does not exist, and that
    # would be a claim about the report that does not hold. The flags are
    # therefore raised only once the row has been built and it is known that
    # it would have been written.
    #: Rule 1 dropped at least one saturated equipment row.
    saturated_dropped: bool = False
    #: Rule 2 wrote at least one equipment row merged.
    equipment_merged: bool = False
    #: Rule 3 left these sample points unwritten -- the labels as they would
    #: have read on the row (``"45"``), so that the reading guide can name the
    #: missing row by the same number as the other rows show theirs.
    skipped_samples: list[str] = field(default_factory=list)
    #: Rule 4 shortened at least one of utility's target rows.
    #:
    #: A boolean and not a counter: the number dropped is **at the end of the
    #: row**, where it concerns that row, and a whole-report total would tell
    #: the reader nothing more. The flag answers only the question "is the
    #: rule explained".
    utility_targets_capped: bool = False
    #: Rule 5 shortened at least one kill row. The same rationale.
    kill_areas_capped: bool = False

    def absorb(self, other: "_Flags", *, keep: bool) -> None:
        """Merge one row's flags into the whole report's bookkeeping.

        ``dropped`` transfers **always**: it is pattern filtering's
        bookkeeping, and it has to be the same number regardless of whether
        the row was pruned. Without this, the block's note ("N harvinaisempaa
        havaintoa jäi pois") would shrink along with the pruning, that is,
        pruning would change a **claim about the data** -- precisely what
        "pruning concerns the presentation and not the content" forbids.

        The flags that concern the presentation transfer only when ``keep``
        is true, that is when the row stayed in the report. Otherwise the
        reading guide would explain an unknown area or an estimate off a row
        the reader does not see.
        """
        self.dropped += other.dropped
        if not keep:
            return
        self.unknown_area |= other.unknown_area
        self.estimated_area |= other.estimated_area
        self.armed_shown |= other.armed_shown
        self.armored_shown |= other.armored_shown
        self.kills_shown |= other.kills_shown
        self.saturated_dropped |= other.saturated_dropped
        self.equipment_merged |= other.equipment_merged
        self.utility_targets_capped |= other.utility_targets_capped
        self.kill_areas_capped |= other.kill_areas_capped
        for label in other.skipped_samples:
            if label not in self.skipped_samples:
                self.skipped_samples.append(label)


@dataclass(frozen=True)
class _UseEntry:
    """One claim on utility's target row, and what has to be explained of it.

    Named fields and not a tuple, because ``estimated`` and ``unknown`` are
    **different things**: the former is a derived detonation area
    ("(arvio)") and the latter an area whose name was not got. Bundled into
    one flag, one explanation would appear in the reading guide because of
    the other -- and the reading guide explains only what shows on the row.

    ``target`` is the detonation area **raw** (``None`` = no name), because
    rule 4 bounds targets: the same area from a different throwing area or a
    different time bucket is the same target, and the label's formatting (the
    estimate mark, the bucket) is no part of the comparison.
    """

    #: The sort key: most common first, the text on a tie.
    rank: tuple[int, str]
    claim: Claim
    target: str | None
    estimated: bool
    unknown: bool


@dataclass(frozen=True)
class _Row:
    """One built row and what pruning decided about it.

    The rows are built **once and pruned only afterwards**, and this object
    is what makes that order possible. Two reasons:

    1. **The threshold's bookkeeping.** The row builder is the only place
       that counts the observations the threshold dropped. If pruning
       bypassed the builder, the block's note would shrink along with the
       pruning and would claim something different about the data from an
       unpruned report.
    2. **A block that would empty.** Returning to the unpruned form does not
       require a second building pass, because the unpruned row is kept
       (:attr:`plain`) -- and therefore neither does it require running the
       row builders twice over the same numbers.
    """

    #: The row without pruning.
    plain: Line
    #: The row after pruning. ``None`` = pruning dropped it entirely.
    kept: Line | None
    #: The row builder's flags. ``None`` = the flags have already been merged
    #: straight into the bookkeeping, because pruning cannot drop this row.
    flags: _Flags | None = None
    #: Whether the presentation flags are merged even if the row itself does
    #: not stay. A merged equipment row (rule 2) carries **both** counters'
    #: numbers, so both definitions are needed in the reading guide even
    #: though one of the rows is not in the report on its own.
    keep_flags: bool = False


@dataclass(frozen=True)
class _Pruning:
    """One round type's pruning rules already resolved.

    An object and not the settings section directly, because **on a protected
    round type every rule is off** (:data:`PROTECTED_ROUND_TYPES`): without
    one place where the exception is resolved, the same ``if`` would repeat
    in five functions and a sixth addition would forget it. The constructor
    is therefore the only place that knows the round type, and the rest of
    the code reads resolved values.

    :meth:`off` is the protected round type's rule. It is **not needed** for
    a block that would empty: the unpruned rows are kept in :class:`_Row`, so
    returning to them does not require a second building pass -- and
    therefore neither does it require undoing a row's truncation (rules 4 and
    5). Truncation cannot empty a block, so undoing it would only bring back
    the 5-9 item list the whole story is written against.
    """

    #: Rule 1.
    drop_saturated: bool
    #: Rule 2.
    merge_equal: bool
    #: Rule 3: the sample points' labels (``{"45"}``) and not floats. The
    #: match is made in the form the number has on the row, so ``45`` and
    #: ``45.0`` mean the same row and a float comparison cannot miss.
    skipped_seconds: frozenset[str]
    #: Rule 4; ``0`` = no limit.
    max_utility_targets: int
    #: Rule 5; ``0`` = no limit.
    max_kill_areas: int

    @classmethod
    def for_round_type(
        cls, settings: ReportSettings, round_type: str
    ) -> "_Pruning":
        if round_type in PROTECTED_ROUND_TYPES:
            return cls.off()
        return cls(
            drop_saturated=settings.drop_saturated_equipment_lines,
            merge_equal=settings.merge_equal_equipment_lines,
            skipped_seconds=frozenset(
                seconds_label(value) for value in settings.skip_sample_seconds
            ),
            max_utility_targets=settings.max_utility_targets,
            max_kill_areas=settings.max_kill_areas,
        )

    @classmethod
    def off(cls) -> "_Pruning":
        """Pruning off: the report is the one that was there before Story
        2.13.
        """
        return cls(
            drop_saturated=False,
            merge_equal=False,
            skipped_seconds=frozenset(),
            max_utility_targets=0,
            max_kill_areas=0,
        )

    def skips(self, position: Position) -> bool:
        """Whether this sample point is left unwritten (rule 3)."""
        return _sample_key(position) in self.skipped_seconds


# -- Formatting ------------------------------------------------------------------


#: The characters Markdown reads as structure **inside a line**. The list is
#: of the forbidden and not of the allowed, because Markdown's syntax is a
#: known and closed set -- unlike weapon names, where the rule is the other
#: way round.
#:
#: ``<`` and ``>`` are on it because Markdown lets raw HTML through: a player
#: named ``<b>`` would embolden the rest of the report, and pasted into
#: Discord the same text travels on. ``~`` is on it because of Discord's
#: strikethrough. The backslash is first, because it is the escape character
#: itself -- handled last, it would escape its own escapes.
#:
#: **What is NOT on the list, and why.** Parentheses, braces, the full stop,
#: plus, exclamation mark and hyphen are significant in Markdown only in a
#: particular place (at the start of a line, or as part of a
#: ``[text](address)`` pair whose brackets are escaped already). Escaping
#: them would prevent nothing but would make the raw text unreadable -- and
#: this report is read raw as well: the data holds a player named
#: ``--allu-``, who would turn into ``\-\-allu\-``.
_MARKDOWN_SPECIALS = "\\`*_[]#|<>~"

#: Runs of spaces, tabs and newlines. A name with a newline in it would break
#: a bullet row in two and make a paragraph of the second half -- that is, it
#: would break the report's structure rather than its content.
_WHITESPACE_RUN = re.compile(r"\s+")


def markdown_text(value: str) -> str:
    """A string the demo gave, as safe Markdown.

    **The escaping is done here and not in the data.** ``report.json`` keeps
    the observation as it is -- it is the archive's truth, and escapes there
    would make the name a different string from the one the demo gave.
    Formatting is the presentation layer's business, and this is the
    presentation layer.

    Every one of Markdown's structural characters occurs in CS2 names: clans
    write themselves as ``*|LOL|*`` and players add underscores and square
    brackets to their names. Without escaping, a roster row would go bold,
    go italic or vanish entirely inside link syntax.

    The whitespace is normalised at the same time: a newline would break a
    bullet row and runs of spaces would disappear in the rendering anyway, so
    they are cleaned up visibly rather than in silence.
    """
    collapsed = _WHITESPACE_RUN.sub(" ", value).strip()
    return "".join(
        "\\" + char if char in _MARKDOWN_SPECIALS else char
        for char in collapsed
    )


def rounds_text(count: int) -> str:
    """``1 kierros`` / ``5 kierrosta``: one round / five rounds."""
    return "1 kierros" if count == 1 else f"{count} kierrosta"


def demos_text(count: int) -> str:
    """``1 demo`` / ``4 demoa``: one demo / four demos."""
    return "1 demo" if count == 1 else f"{count} demoa"


def players_text(count: int) -> str:
    """``1 pelaaja`` / ``5 pelaajaa``: one player / five players.

    The singular is an observation here and not grammatical decoration: four
    of the calibration's six marked rounds hit on **one** player's
    observation, so that is the form that recurs most often in the report.
    """
    return "1 pelaaja" if count == 1 else f"{count} pelaajaa"


def _seconds(value: float) -> str:
    """A number of seconds with a Finnish decimal comma: ``9.0 -> '9'``.

    The formatting itself is in
    :func:`~pappascout.constants.seconds_label`, because **the setting and
    the row have to agree about it** (Story 2.13):
    ``[report].skip_sample_seconds`` names a sample point by the number the
    reader sees on the row, and the load-time check "two values would look
    the same on the row" uses the same function. As two copies they would
    agree only today.

    The name stays here, because this module uses it in twenty places and not
    one of them is a settings match.
    """
    return seconds_label(value)


def _median_seconds(value: float) -> str:
    """A median time to one decimal place.

    First contact's median is a float out of the ticks (``12.531``), and its
    third decimal is a precision that does not exist: the sample is the
    round's first hit, not a measurement result. One decimal says the same
    without looking more accurate than it is.
    """
    return f"{value:.1f}".replace(".", ",")


def _area(name: str | None) -> str:
    """An area name as safe Markdown, or the mark for a missing one.

    **The area is text the demo gave** (``m_szLastPlaceName``) just as the
    team's name and the map's name are: the game gives the value, and it is
    not validated against any list. On every real CS2 area the escaping is
    invisible (``BombsiteA``, ``TSideUpper``), but a workshop map's area
    ``*|Aim|* Botz [beta]`` would break the row -- and that row carries the
    claim's sample.

    **Escaping and not a code span**, and the split is the project's own: a
    name is escaped (team, roster), an id is wrapped in a code span (a demo
    id, the map name in the traceability chapter and in the map chapter's
    heading, the round appendix's path), because an id has to be copyable out
    of the report as it is. An area name is not copied anywhere: it is read
    as part of a sentence in the middle of a row, and a code span would break
    every observation row into three pieces.

    ``None`` is **the absence of an observation and not a name**, so it gets
    a mark of its own and does not go through the escaping.
    """
    return markdown_text(name) if name else UNKNOWN_AREA


def _grenade(name: str) -> str:
    """A grenade type's Finnish name.

    An unknown type is returned **as it is** and not dropped: a new grenade
    type shows in the report in English, it does not vanish.
    """
    return GRENADE_TYPE_FI.get(name, name)


def _grenade_rank(name: str) -> int:
    return GRENADE_ORDER.index(name) if name in GRENADE_ORDER else len(GRENADE_ORDER)


def _capitalise(value: str) -> str:
    """A capital first letter **without touching the other characters**.

    ``str.capitalize`` would lower-case the rest: the abbreviation "OT" would
    turn into "Ot" and "HE" into "He". The round types' Finnish names are
    ordinary words today, but the list is in ``constants`` and not here, so
    the rule must not lean on whatever happens to be there now.
    """
    return value[:1].upper() + value[1:]


# -- The threshold, read from the report and not invented here -------------------


def pattern_min_rounds(report: Report) -> int | None:
    """How many rounds a repetition needs to be a pattern.

    The value is read from ``report.json``'s
    ``thresholds_used.thresholds.small_sample_rounds`` field -- the same
    number ``aggregate`` marks a small sample with. The report does not
    invent a filtering threshold of its own: if the number is not there, no
    filtering is done at all and the report says so.

    Returns:
        A positive number of rounds, or ``None`` if there was no value.
    """
    return _threshold_int(report, "small_sample_rounds")


def _threshold_value(report: Report, name: str) -> int | float | None:
    """One threshold value out of ``report.json``, untyped.

    **One lookup for two readers.** The value is read **from the report and
    not from the settings**, and if it is not there the row is written
    without a threshold instead of the rendering inventing a number of its
    own. The rule used to be written twice (:func:`_threshold_int` and
    :func:`_threshold_float`) with exactly the argument that it must not be
    written twice -- and the copies had already drifted: one demanded a
    positive value, the other accepted zero and negatives.

    This function carries the **shared** conditions: the section exists, the
    key exists, the value is a number. **Two** conditions are left to the
    callers, and both are dictated by the type: the permitted type (``int``
    vs. any number) and the floor in the form the type requires (``>= 1`` for
    a count, ``> 0`` for a share). The floors are not the same condition
    twice but the same rule -- a threshold is positive -- in two units;
    without the difference ``advance_t_share = 0.80`` would drop out.

    ``bool`` is rejected separately, because in Python it is an ``int``:
    ``True`` would set the number 1 as the threshold.
    """
    section = report.thresholds_used.get("thresholds")
    if not isinstance(section, Mapping):
        return None
    value = section.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def _threshold_int(report: Report, name: str) -> int | None:
    """An integer threshold: **a whole number and at least one**.

    The floor is the definition of a count and not an extra condition: these
    thresholds are rounds, players, observations and areas, and "at least
    zero rounds" is not a threshold but its absence.
    """
    value = _threshold_value(report, name)
    if not isinstance(value, int) or value < 1:
        return None
    return value


def _threshold_float(report: Report, name: str) -> float | None:
    """A float threshold: **finite and positive**.

    The floor is in a different form from :func:`_threshold_int`'s and for
    the same reason: the float thresholds are shares and seconds, and the
    smallest one in use is ``advance_t_share = 0.80``. An integer floor would
    reject it. Both still reject zero and negatives, because
    ``ThresholdSettings`` demands a positive value from every one of these --
    letting zero through would set a threshold into the report that the
    settings do not allow.

    Infinity and NaN are rejected, because they would be set on the row as
    ``inf`` and ``nan``: a number the reader cannot interpret is worse than a
    missing number.
    """
    value = _threshold_value(report, name)
    if value is None:
        return None
    number = float(value)
    if not isfinite(number) or number <= 0.0:
        return None
    return number


# -- Building the rows -----------------------------------------------------------


def _position_line(position: Position, min_n: int, flags: _Flags) -> Line | None:
    """One sample point's row: the areas and their player counts."""
    claims: list[tuple[int, int, str, Claim]] = []
    for area in position.areas:
        for bar in area.players_dist:
            if bar.players == 0:
                # An empty area is in the structure so that Σ n = m holds. It
                # is not an observation to be told: "there was nobody in the
                # area" is true of every area on the map that is not
                # mentioned.
                continue
            if bar.n < min_n:
                if min_n > 1:
                    flags.dropped += 1
                continue
            name = _area(area.area)
            if area.area is None:
                flags.unknown_area = True
            claims.append(
                (
                    -bar.players,
                    -bar.n,
                    name,
                    Claim(text=f"{name} {bar.players}", n=bar.n, m=area.m),
                )
            )

    note = None
    if position.rounds_missing:
        note = f"näyte puuttuu {position.rounds_missing} kierrokselta"

    if not claims and note is None:
        return None

    claims.sort(key=lambda item: item[:3])
    return Line(
        # The same rule as on the first-death row
        # (:func:`_first_death_label`): the median is a claim, and pruning
        # can take away every area claim and leave it alone. The sample is
        # written only when the row has no other sample -- otherwise the
        # same number would be on the row twice and every working row would
        # change.
        label=_position_label(position, with_sample=not claims),
        claims=tuple(item[3] for item in claims),
        note=note,
    )


def _sample_key(position: Position) -> str | None:
    """A sample point's key for pruning: the seconds **in the row label's
    form**.

    The match is made as a string and not as a float comparison, so that the
    setting ``45`` and the sample point ``45.0`` mean the same row: the
    setting's value is a TOML number a human wrote and the sample point is
    the report's float, and they do not have to be byte for byte the same
    value to match what the reader sees on the row.

    ``None`` for first contact: it is not a chosen sample point but the
    round's own moment, and it has no nominal number of seconds by which it
    could be named in a setting.
    """
    if position.sample_kind != "time" or position.seconds is None:
        return None
    return _seconds(position.seconds)


def _keep_most_common(
    entries: Sequence[Any], limit: int, n_of: Callable[[Any], int]
) -> tuple[list[Any], int]:
    """The most common ``limit`` items, **ties included**.

    ``limit <= 0`` means "no limit": emptying a row is not pruning but
    suppression, which is scoped out of Story 2.13, so zero is not read as a
    limit.

    **A tie continues the limit.** If after the cut-off there is an
    observation as common as the last one retained, it is retained too:
    otherwise the row would drop one of two observations with identical
    samples and the note would call it *rarer*, which is a false claim. At a
    limit of three, four equally common areas therefore produce four areas --
    the limit is "the most common", not "at most three".

    ``entries`` **has to be assumed already sorted** from the most common to
    the rarest; the caller does that already, because the row's order is its
    own decision.

    Returns:
        ``(retained, the number dropped)``.
    """
    if limit <= 0 or len(entries) <= limit:
        return list(entries), 0
    cutoff = n_of(entries[limit - 1])
    kept = [
        entry
        for index, entry in enumerate(entries)
        if index < limit or n_of(entry) == cutoff
    ]
    return kept, len(entries) - len(kept)


def _position_label(position: Position, *, with_sample: bool = False) -> str:
    """A sample point's label: ``15 s`` or ``ensikontakti (mediaani 9 s)``.

    ``with_sample`` adds **the median's own sample**, and that is the
    caller's decision and not this function's: the sample is written only
    when the row has no area claim at all. First contact's median is a timing
    claim just as the first death's median is, and pruning can take every
    area row out from under it -- the row would then be left as
    ``ensikontakti (mediaani 14,2 s): näyte puuttuu 2 kierrokselta``, that
    is, a claim without a sample.

    **A time sample point gets no sample**, although it too can be left as a
    bare note. Its label (``15 s``) is not a claim but the name of a moment:
    without area claims the row holds nothing for a sample to be about.

    The sample is ``m / (m + rounds_missing)``: the rounds on which **this
    sample point exists**, out of all the round type's rounds. Those are
    exactly the ones the median has been computed from.
    """
    if position.sample_kind == "time":
        # The model guarantees that a time sample point cannot have an empty
        # number of seconds.
        return f"{_seconds(position.seconds or 0.0)} s"
    if position.seconds_median is None:
        return "ensikontakti"
    median = _median_seconds(position.seconds_median)
    if not with_sample:
        return f"ensikontakti (mediaani {median} s)"
    rounds = position.m + position.rounds_missing
    return f"ensikontakti (mediaani {median} s, {position.m}/{rounds} kierroksesta)"


def _utility_count_line(
    counts: Sequence[UtilityCounts], min_n: int, flags: _Flags
) -> Line | None:
    """The "how many grenades were thrown" row -- the target analysis's
    *"2 savua 2 valoo"*.
    """
    claims: list[tuple[int, int, Claim]] = []
    for entry in counts:
        for bar in entry.counts:
            if bar.thrown == 0:
                # "Zero smokes" is true of every grenade that is not
                # mentioned.
                continue
            if bar.n < min_n:
                if min_n > 1:
                    flags.dropped += 1
                continue
            claims.append(
                (
                    _grenade_rank(entry.grenade_type),
                    -bar.thrown,
                    Claim(
                        text=f"{_grenade(entry.grenade_type)} {bar.thrown} kpl",
                        n=bar.n,
                        m=entry.m,
                    ),
                )
            )
    if not claims:
        return None
    claims.sort(key=lambda item: (item[0], item[1], item[2].text))
    return Line(label="utility", claims=tuple(item[2] for item in claims))


def _utility_use_lines(
    uses: Sequence[UtilityUse], min_n: int, flags: _Flags, pruning: _Pruning
) -> list[Line]:
    """The "from where to where" rows -- the target analysis's
    *"T-spawnista CT-savu B sitelle"*.

    One row **per grenade type**, not per throw. A pattern's every throw is
    on the same row, because the report is read in the rush before a match:
    five smoke rows in a row take five rows to say one thing ("the smokes go
    to B").

    **Rule 4 (Story 2.13) bounds targets and not claims.** Measured, ten rows
    had between five and nine targets, and such a row is a list and not a
    pattern. A target is a **detonation area**, and the same target can be on
    the row more than once: from a different throwing area or in a different
    time bucket (``[aggregate].utility_seconds_buckets``). By bounding claims
    the two retained places could be the same target in two buckets, whereby
    the row would lose every other target while the note calls them targets
    -- and that is precisely the mistake the measured rationale does not
    mean.

    **Every** claim of a retained target stays on the row: the row says where
    the utility goes, and the same target in two buckets is two different
    observations of the same target.

    The order is the claims' own (most common first), not grouped by target:
    without a limit the row is **character for character** the one that was
    there before Story 2.13.
    """
    rows: dict[str, list[_UseEntry]] = {}
    for use in uses:
        if use.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        target = _area(use.detonate_area)
        estimated = use.area_source == "point_cloud"
        text = (
            f"{_area(use.throw_area)} -> {target}"
            f"{ESTIMATE_MARK if estimated else ''}"
            f"{_bucket_text(use.seconds_bucket)}"
        )
        extra = f"{use.throws} heittoa" if use.throws != use.n else None
        unknown = use.throw_area is None or use.detonate_area is None
        rows.setdefault(use.grenade_type, []).append(
            _UseEntry(
                rank=(-use.n, text),
                claim=Claim(text=text, n=use.n, m=use.m, extra=extra),
                target=use.detonate_area,
                estimated=estimated,
                unknown=unknown,
            )
        )

    lines: list[Line] = []
    for grenade_type in sorted(rows, key=_grenade_rank):
        entries = sorted(rows[grenade_type], key=lambda entry: entry.rank)
        kept, dropped = _kept_targets(entries, pruning.max_utility_targets)
        if dropped:
            flags.utility_targets_capped = True
        # The flags only from the retained claims and **separately**: a
        # dropped claim's estimate or unknown area would explain in the
        # reading guide a row that is not there -- and an estimate (a derived
        # detonation area) is a different thing from an unknown area (the
        # name was not got), so bundled into one flag one explanation would
        # appear because of the other.
        for entry in kept:
            flags.unknown_area |= entry.unknown
            flags.estimated_area |= entry.estimated
        lines.append(
            Line(
                label=_grenade(grenade_type),
                claims=tuple(entry.claim for entry in kept),
                note=_dropped_note(dropped, "kohdetta"),
            )
        )
    return lines


def _kept_targets(
    entries: Sequence[_UseEntry], limit: int
) -> tuple[list[_UseEntry], int]:
    """The claims to retain when the limit concerns **targets** (rule 4).

    The targets are ordered by the most common of their claims: the claims
    are in order already, so a target's first occurrence tells its place. The
    bound is applied to the set of targets (ties included), and the claims
    are filtered by it **in their original order**.

    Returns:
        ``(the retained claims, the number of targets dropped)``.
    """
    order: list[str | None] = []
    best: dict[str | None, int] = {}
    for entry in entries:
        if entry.target not in best:
            best[entry.target] = entry.claim.n
            order.append(entry.target)
    kept_targets, dropped = _keep_most_common(order, limit, lambda a: best[a])
    if not dropped:
        return list(entries), 0
    allowed = set(kept_targets)
    return [entry for entry in entries if entry.target in allowed], dropped


def _dropped_note(dropped: int, unit: str) -> str | None:
    """The row's own note about how many observations were left off it.

    The wording is **pattern filtering's wording** ("119 harvinaisempaa
    havaintoa jäi pois", :func:`_round_type_view`): the reader sees the same
    sentence about observations left out for two different reasons, and that
    is intended -- it is the same thing, the row saying what is missing from
    it. The unit changes, because a target and an area are different things
    and "havaintoa" would not say which is meant.

    ``None`` when nothing was dropped: an empty note would set a dash on the
    row with nothing after it.
    """
    if not dropped:
        return None
    return f"{dropped} harvinaisempaa {unit} jäi pois"


def _bucket_text(bucket: str) -> str:
    """The time bucket for the row.

    Two special names are not time intervals, and neither may be glued to the
    word "s": :data:`~pappascout.constants.UTILITY_BUCKET_ALL` means that no
    buckets are in use, and
    :data:`~pappascout.constants.UTILITY_BUCKET_UNKNOWN` that the throw's
    moment was not got. The names are read from ``constants``, because
    ``aggregate`` writes them -- written into two places, changing a name
    would silently produce the row ``" kaikki s"`` in the report.
    """
    if bucket == UTILITY_BUCKET_ALL:
        return ""
    if bucket == UTILITY_BUCKET_UNKNOWN:
        return " (heittoaika tuntematon)"
    return f" {bucket} s"


def _first_contact_gap_line(
    report_type: RoundTypeReport, min_n: int, flags: _Flags
) -> Line | None:
    """First contact's areas that are **not** in the corresponding sample
    point.

    The report shows first contact from the ``positions`` list's
    ``first_contact`` sample point, because that tells the player counts and
    not merely the presence (see the module docstring). The assumption is
    that the sample point is a superset: every area in which the team had a
    player is there.

    **The assumption must not be left an assumption.** If ``aggregate`` ever
    produces a presence without a corresponding distribution row -- for
    instance if the sample point is dropped but the presence list stays --
    the observation would vanish from the report without a trace. This
    function compares the lists and writes the difference on a row of its
    own. When the assumption holds, the row is left out and costs nothing.
    """
    covered = {
        distribution.area
        for position in report_type.positions
        if position.sample_kind == "first_contact"
        for distribution in position.areas
        if any(bar.players > 0 for bar in distribution.players_dist)
    }
    claims: list[tuple[int, str, Claim]] = []
    for entry in report_type.first_contact:
        if entry.area in covered:
            continue
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        if entry.area is None:
            flags.unknown_area = True
        claims.append((-entry.n, name, Claim(text=name, n=entry.n, m=entry.m)))
    if not claims:
        return None
    claims.sort(key=lambda item: item[:2])
    return Line(
        label="ensikontakti, vain läsnäolo",
        claims=tuple(claim for _, _, claim in claims),
        note="pelaajamäärää ei ole näytepisteessä",
    )


def _death_lines(
    deaths: DeathReport, min_n: int, flags: _Flags, pruning: _Pruning
) -> list[Line]:
    """At most :data:`MAX_DEATH_LINES` rows: the first death and the kills.

    Two rows, because deaths explain the other rows and are not a chapter of
    their own. The first answers the question *"where does the team lose its
    first player and when"* -- the target analysis's line "Luola kuolee nii
    pelaa siteltä/nyypästä ja longilta". The second answers the question
    *"where do they shoot from"* -- "Vihu meni secret pihalta".

    **The kill row's sample is kills and not rounds**, and that is said in
    the claim itself (:data:`KILL_SAMPLE_UNIT`) and not only in the reading
    guide: the row is read on its own, far from the reading guide.

    **When a row is written.** A row comes about if it has a claim **or** an
    observation -- not a bare label with neither. The first-death row can
    therefore be a bare note without a single area: "ei omia kuolemia 4
    kierroksella" is a **genuine observation**, it says that the team lost
    nobody. The kill row has no counterpart to that, because "zero kills" has
    not been counted into any number. A round type that has neither deaths
    nor rounds produces neither row.

    **One consequence is worth saying out loud: the median can disappear
    entirely.** When every round has a death of its own
    (``rounds_missing == 0``) but pruning takes every area row, the condition
    is not met -- no claim, no note -- and the row is not written, even
    though the timing was measured. That is not a false claim but a missing
    row, so the behaviour stays as it is: the report says nothing it cannot
    justify. The false claim would be a median without a sample, and that was
    fixed (:func:`_first_death_label`). If the row is ever wanted back, it is
    an **added row** and not a changed one -- and then it is a matter for the
    epic's page-length target, not for this function.

    **Rule 5 (Story 2.13) concerns only the kill row.** The kill row is a
    whole round type's kills on one row, and measured, 29 claims are pruned
    off it when it is bounded to the three most common areas. The first-death
    row is **not bounded**, and the rationale is measured and not inferred:
    its distribution is rounds and ``Σ n = m``, so there can be at most as
    many areas as there are rounds -- and in both of the archive's reports
    (8 demos, 7 map branches, 52 round-type blocks) the row's width is **at
    most 3 areas**, with a median of 1. The kill row's denominator is kills,
    so the same area can recur across dozens of kills and the row grows
    regardless of the number of rounds; that difference is exactly what makes
    one of them a list and the other a distribution.
    """
    lines: list[Line] = []

    claims: list[tuple[int, str, Claim]] = []
    for entry in deaths.first_death_areas:
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        if entry.area is None:
            flags.unknown_area = True
        claims.append((-entry.n, name, Claim(text=name, n=entry.n, m=entry.m)))
    note = None
    if deaths.rounds_missing:
        note = f"ei omia kuolemia {deaths.rounds_missing} kierroksella"
    if claims or note is not None:
        claims.sort(key=lambda item: item[:2])
        lines.append(
            Line(
                # The sample goes into the label **only when the row has no
                # claim at all** (A2). Pruning can take every area row and
                # leave the median behind, and the row would then claim the
                # timing without a sample -- that is, it would break the
                # epic's second criterion. When there are area rows, they
                # carry the sample themselves and nothing is added to the
                # label: the row is then character for character the same as
                # before this story.
                label=_first_death_label(deaths, with_sample=not claims),
                claims=tuple(claim for _, _, claim in claims),
                note=note,
            )
        )

    kill_claims: list[tuple[int, str, Claim, bool]] = []
    for entry in deaths.kills:
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        kill_claims.append(
            (
                -entry.n,
                name,
                Claim(text=name, n=entry.n, m=entry.m, unit=KILL_SAMPLE_UNIT),
                entry.area is None,
            )
        )
    if kill_claims:
        flags.kills_shown = True
        kill_claims.sort(key=lambda item: item[:2])
        kept, dropped = _keep_most_common(
            kill_claims, pruning.max_kill_areas, lambda item: item[2].n
        )
        if dropped:
            flags.kill_areas_capped = True
        # The flag only from the retained areas: a dropped unknown area would
        # explain in the reading guide a row that is not there.
        if any(unknown for _, _, _, unknown in kept):
            flags.unknown_area = True
        lines.append(
            Line(
                label="tapot alueittain",
                claims=tuple(claim for _, _, claim, _ in kept),
                note=_dropped_note(dropped, "aluetta"),
            )
        )

    # A guard and not decoration: the row limit is this story's whole bound,
    # and a third row would come about in silence from somebody adding a
    # block to this function. Raising the limit is a contract change, not a
    # code change.
    #
    # An exception and not an ``assert``: an assert disappears under
    # ``python -O``, and the guard would then exist only on a development
    # machine -- that is, precisely where it is not needed.
    if len(lines) > MAX_DEATH_LINES:
        raise PappascoutError(
            f"There were {len(lines)} death rows, although at most "
            f"{MAX_DEATH_LINES} are allowed per round type.\n"
            "This is a programming error in the report's view: the row limit "
            "is Story 2.7's bound, and raising it is not a code change but a "
            "contract change."
        )
    return lines


def _first_death_label(deaths: DeathReport, *, with_sample: bool = False) -> str:
    """The label: ``ensimmäinen kuolema (mediaani 24 s)``.

    The median is in the label and not a claim of its own for the same reason
    as at the first-contact sample point: it is the whole row's timing and
    not one area's observation.

    **But it is a claim, and a claim carries its sample.** The earlier
    wording said that the median has no ``n/m`` sample of its own; the retro
    measured a counter-example (RCAVE ``de_anubis`` default): when each of
    the seven deaths was in a different area, pruning dropped every area row
    and the row was left as
    ``ensimmäinen kuolema (mediaani 14,2 s): ei omia kuolemia 2 kierroksella``
    -- a timing without a single number saying what it was computed from.

    ``with_sample`` is therefore **the caller's decision and not this
    function's**: the sample is written only when the row has no other
    sample. Otherwise the same number would be on the row twice, and every
    working row would change.

    The sample is ``m / (m + rounds_missing)``: **the rounds on which the
    team lost a player, out of all the round type's rounds**. Those are
    exactly the rounds the median covers.

    **The denominator is not the same as the area rows', and that is
    deliberate.** An area row reads ``4/7``: its denominator is ``deaths.m``,
    that is the rounds on which a death happened -- the distribution's
    ``Σ n = m`` holds only in that population. The median reads ``7/9``: its
    denominator is all the round type's rounds, because the median is the
    whole block's timing claim and not one area's share. The reading guide's
    general rule (``m`` = all the round type's rounds) therefore describes
    the median; the area rows are a named exception to it, and the reading
    guide says so out loud. **The numbers are never on the same row** -- the
    median gets a sample only when there are no area rows -- but they are
    consecutive in the same chapter, so the difference has to be written out.

    The median is computed from those rounds' timings; a death without a
    timing would narrow it, but there are none in the archive (measured
    2026-09-03: 1,219 deaths, 0 without a ``t_s``), and the model has no
    field for telling that apart -- adding one would be a schema change.
    """
    if deaths.first_death_seconds_median is None:
        # Without a median the row has no claim, only a coverage note ("ei
        # omia kuolemia N kierroksella"). A sample would then give a sample
        # to a claim that is not there.
        return "ensimmäinen kuolema"
    median = _median_seconds(deaths.first_death_seconds_median)
    if not with_sample:
        return f"ensimmäinen kuolema (mediaani {median} s)"
    rounds = deaths.m + deaths.rounds_missing
    return (
        f"ensimmäinen kuolema (mediaani {median} s, "
        f"{deaths.m}/{rounds} kierroksesta)"
    )


def _player_count_line(
    label: str,
    bars: Sequence[tuple[int, int]],
    m: int,
    rounds_unknown: int,
    min_n: int,
    flags: _Flags,
) -> Line | None:
    """One player counter's distribution as one row.

    Shared between armed and armoured for the same reason as
    ``stages.parse``'s ``_column_distribution``: they are different
    observations from the same tick, and two copies would before long set
    them differently -- one would filter on the threshold and the other would
    not, or one would tell of missing observations and the other would stay
    silent. The **difference** between the rows is this report's observation,
    so their shape has to stay the same.

    Args:
        label: The row's label.
        bars: ``(player count, rounds)`` pairs, unsorted.
        m: The rounds the observation was got from -- the claims'
            denominator.
        rounds_unknown: The rounds the observation was not got from.
        min_n: The repetition threshold; bars below it are dropped and
            counted into ``flags.dropped`` when the threshold is above one.
        flags: The report-wide collector.

    Returns:
        The row, or ``None`` if there is nothing to tell. **A bare note is
        enough** for a row: "havainto puuttuu 3 kierrokselta" is a different
        thing from "nobody carried armour", and without the row the reader
        could not tell them apart -- the latter would show as a zero and the
        former not at all.
    """
    claims: list[Claim] = []
    for value, n in sorted(bars, key=lambda bar: (-bar[1], -bar[0])):
        if n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        claims.append(Claim(text=str(value), n=n, m=m))
    note = None
    if rounds_unknown:
        note = f"havainto puuttuu {rounds_unknown} kierrokselta"
    if not claims and note is None:
        return None
    return Line(label=label, claims=tuple(claims), note=note)


def _armed_line(armed: ArmedPlayers, min_n: int, flags: _Flags) -> Line | None:
    """The armed players' distribution at the end of the buy time.

    The flag is raised whenever the row is written -- also when the row is a
    bare note. Otherwise the reader would see the label "aseistettuja"
    without its definition, and the definition is exactly what tells the row
    apart from the armour row.
    """
    line = _player_count_line(
        "aseistettuja ostoajan lopussa",
        [(bar.armed, bar.n) for bar in armed.counts],
        armed.m,
        armed.rounds_unknown,
        min_n,
        flags,
    )
    if line is not None:
        flags.armed_shown = True
    return line


def _armored_line(
    armored: ArmoredPlayers, min_n: int, flags: _Flags
) -> Line | None:
    """The armoured players' distribution at the end of the buy time.

    A row of its own beside the armed row, not in its place. This is where
    the target analysis's *"5 kevlaria"* and *"ei kevuja"* are read, and they
    cannot be read off the armed row: on a pistol round the armed count is
    practically 0, because $800 does not buy both armour and an upgraded
    weapon. Two rows in succession, then, and their difference is precisely
    the observation.
    """
    line = _player_count_line(
        "panssaroituja ostoajan lopussa",
        [(bar.armored, bar.n) for bar in armored.counts],
        armored.m,
        armored.rounds_unknown,
        min_n,
        flags,
    )
    if line is not None:
        flags.armored_shown = True
    return line


def _equipment_rows(
    report_type: RoundTypeReport,
    min_n: int,
    flags: _Flags,
    pruning: _Pruning,
) -> tuple[list[_Row], bool, bool]:
    """The equipment rows: armed and armoured, pruning rules 1 and 2.

    The rows are in one function because both rules concern the **pair** and
    not a single row: rule 1 compares the distribution with a full team and
    rule 2 compares the distributions with each other. Written separately
    neither would see the other, and merging requires knowing that the other
    row was not dropped already.

    **The rows are built first and pruned only afterwards.** The order is not
    a matter of taste but the whole rule's precondition:

    * The row builder is the only place that counts the bars pattern
      filtering dropped. A short circuit before it would shrink the block's
      note, that is, pruning would change a **claim about the data**.
    * A saturated row can also be left unwritten **because of the
      threshold** (on a full buy two rounds are not enough for a pattern).
      Rule 1 then removed nothing, and it must not say in the reading guide
      that it did.

    The order between the rules is the rule number: the saturated row is
    dropped **first**, because if both are saturated, merging would write a
    row that is precisely the expectation rule 1 leaves unsaid.

    Args:
        report_type: The round type's observations from the report.
        min_n: The repetition threshold, the same as on the other rows.
        flags: The report-wide collector. It is needed here because a row
            that **never came about at all** does not fit into a
            :class:`_Row` -- and it is exactly that threshold bookkeeping
            whose loss would make the block's note false.
        pruning: This round type's pruning rules.

    Returns:
        ``(the rows, whether rule 1 dropped, whether rule 2 merged)``. The
        rows are :class:`_Row` objects, so the caller sees both the pruned
        and the unpruned form and does not have to build anything a second
        time.
    """
    armed = report_type.players_armed
    armored = report_type.players_armored
    armed_flags, armored_flags = _Flags(), _Flags()
    armed_line = _armed_line(armed, min_n, armed_flags)
    armored_line = _armored_line(armored, min_n, armored_flags)
    # A row that pattern filtering never let come about does not fit into a
    # ``_Row``, but its bookkeeping belongs to the block: without this the
    # block's note would shrink and would not say how many observations fell
    # below the threshold.
    if armed_line is None:
        flags.absorb(armed_flags, keep=False)
    if armored_line is None:
        flags.absorb(armored_flags, keep=False)

    armed_bars = [(bar.armed, bar.n) for bar in armed.counts]
    armored_bars = [(bar.armored, bar.n) for bar in armored.counts]
    drop_armed = (
        pruning.drop_saturated
        and armed_line is not None
        and _is_saturated(armed_bars, armed.rounds_unknown)
    )
    drop_armored = (
        pruning.drop_saturated
        and armored_line is not None
        and _is_saturated(armored_bars, armored.rounds_unknown)
    )
    saturated_dropped = drop_armed or drop_armored

    merge = (
        pruning.merge_equal
        and armed_line is not None
        and armored_line is not None
        and not drop_armed
        and not drop_armored
        and _same_distribution(armed_bars, armored_bars, armed, armored)
    )
    if merge:
        # A new label for the same claims: the row carries both counters'
        # number, so this is a renaming and not a new computation.
        merged = Line(
            label=MERGED_EQUIPMENT_LABEL,
            claims=armed_line.claims,
            note=armed_line.note,
        )
        return (
            [
                _Row(plain=armed_line, kept=merged, flags=armed_flags),
                # The armour row does not stay on its own, but its flags do:
                # the reading guide has to define both words, or the merged
                # row would give the number without a definition.
                _Row(
                    plain=armored_line,
                    kept=None,
                    flags=armored_flags,
                    keep_flags=True,
                ),
            ],
            saturated_dropped,
            True,
        )

    rows: list[_Row] = []
    if armed_line is not None:
        rows.append(
            _Row(
                plain=armed_line,
                kept=None if drop_armed else armed_line,
                flags=armed_flags,
            )
        )
    # The armour row immediately after the armed one: their difference is
    # the observation itself, and it cannot be seen if anything else is
    # between the rows.
    if armored_line is not None:
        rows.append(
            _Row(
                plain=armored_line,
                kept=None if drop_armored else armored_line,
                flags=armored_flags,
            )
        )
    return rows, saturated_dropped, False


def _is_saturated(bars: Sequence[tuple[int, int]], rounds_unknown: int) -> bool:
    """Whether an equipment row is **saturated**, that is an expectation and
    not an observation (rule 1).

    Three conditions, and every one of them is needed:

    * **One bar.** Two bars means that the reading varied between rounds, and
      variation is an observation.
    * **The value is**
      :data:`~pappascout.domain.models.PLAYERS_ON_SERVER`. One bar with the
      value 0 is just as monotonous a row, but it is an observation: "nobody
      had armour" is the target analysis's *"ei kevuja"* and not an
      expectation.
    * **The observation was got from every round.** A ``rounds_unknown``
      above zero means that the row has a note about unreadable rounds -- and
      that note is an observation that would vanish along with the row.

    ``Σ n = m`` is guaranteed by the model, so in the one-bar case ``n = m``
    follows from it and does not have to be checked separately.

    **Saturation is not the only reason** a row can be left unwritten:
    pattern filtering drops it on a full buy if the sample is not enough for
    a pattern. That is why this is asked only of a row that was really built
    (:func:`_equipment_rows`).
    """
    return (
        len(bars) == 1
        and bars[0][0] == PLAYERS_ON_SERVER
        and rounds_unknown == 0
    )


def _same_distribution(
    armed_bars: Sequence[tuple[int, int]],
    armored_bars: Sequence[tuple[int, int]],
    armed: ArmedPlayers,
    armored: ArmoredPlayers,
) -> bool:
    """Whether the equipment rows tell **exactly the same** distribution
    (rule 2).

    Three things have to be compared, not one. The bars give the readings,
    ``m`` is the claims' denominator and ``rounds_unknown`` is the row's
    note: if any of these differs, the rows differ, and the difference is
    precisely the observation the two rows exist for (Story 2.8).

    The bars are compared sorted, because the list order in ``report.json``
    is not a contract.
    """
    return (
        armed.m == armored.m
        and armed.rounds_unknown == armored.rounds_unknown
        and sorted(armed_bars) == sorted(armored_bars)
    )


#: The note about a block that pruning would have emptied.
#:
#: The spec's "Ask First" gate: a rule that would remove every row from a
#: block would stop the block saying anything, and that is **a suppression
#: decision and not pruning**. Suppression is scoped out of Story 2.13, so
#: the code does what the matrix says: the rows stay. The note is there
#: because a silent return to the unpruned form would read as if the rule had
#: not been in use -- and the next reader would wonder why this block in
#: particular has a row the rule removes elsewhere.
#:
#: **The return concerns only rows dropped entirely.** Truncating a row
#: (rules 4 and 5) cannot empty a block, so it is not undone: undoing it
#: would only bring back the 5-9 item list the whole story is written
#: against.
_PRUNING_KEPT_THE_BLOCK = (
    "Karsinta olisi poistanut tästä lohkosta jokaisen rivin, joten sitä ei "
    "karsittu: tyhjä lohko olisi vaimennus eikä karsinta."
)


def _round_type_lines(
    report_type: RoundTypeReport,
    min_n: int,
    flags: _Flags,
    pruning: _Pruning,
) -> tuple[list[Line], bool]:
    """One round type's rows in order, pruning included.

    **The rows are built once.** Every row comes about exactly as it would
    without pruning, and the pruning is done afterwards on the finished rows:
    a row dropped entirely stays in :class:`_Row`'s ``plain``, so checking
    whether the block empties does not require a second building pass. This
    is also the only way the block's pattern-filtering note can be the same
    with and without pruning -- the counter grows in the builder.

    The rows pruning cannot drop (grenade counts, target rows, first
    contact's presence, deaths) write their flags straight into ``flags``:
    their fate does not depend on any decision, so conditional bookkeeping
    would be needless machinery.

    Returns:
        ``(the rows, whether the unpruned block was returned)``.
    """
    rows: list[_Row] = []
    skipped_samples: list[str] = []

    for position in report_type.positions:
        scratch = _Flags()
        line = _position_line(position, min_n, scratch)
        if line is None:
            # The row never came about, so pruning has nothing to say about
            # it. The threshold's bookkeeping transfers all the same.
            flags.absorb(scratch, keep=False)
            continue
        if pruning.skips(position):
            label = _sample_key(position)
            if label is not None and label not in skipped_samples:
                skipped_samples.append(label)
            rows.append(_Row(plain=line, kept=None, flags=scratch))
        else:
            rows.append(_Row(plain=line, kept=line, flags=scratch))

    def keep_all(lines: Sequence[Line]) -> None:
        rows.extend(_Row(plain=line, kept=line) for line in lines)

    utility = _utility_count_line(report_type.utility_counts, min_n, flags)
    if utility is not None:
        keep_all([utility])
    keep_all(_utility_use_lines(report_type.utility, min_n, flags, pruning))

    equipment, saturated_dropped, merged = _equipment_rows(
        report_type, min_n, flags, pruning
    )
    rows.extend(equipment)

    gap = _first_contact_gap_line(report_type, min_n, flags)
    if gap is not None:
        keep_all([gap])

    keep_all(_death_lines(report_type.deaths, min_n, flags, pruning))

    kept = [row.kept for row in rows if row.kept is not None]
    if rows and not kept and (saturated_dropped or skipped_samples):
        # Pruning would have left the block empty: the rows stay unpruned,
        # and not one rule is marked as used -- otherwise the reading guide
        # would explain a removal that was not made.
        for row in rows:
            if row.flags is not None:
                flags.absorb(row.flags, keep=True)
        return [row.plain for row in rows], True

    for row in rows:
        if row.flags is not None:
            flags.absorb(row.flags, keep=row.keep_flags or row.kept is not None)
    if saturated_dropped:
        flags.saturated_dropped = True
    if merged:
        flags.equipment_merged = True
    for label in skipped_samples:
        if label not in flags.skipped_samples:
            flags.skipped_samples.append(label)
    return kept, False


def _round_type_view(
    report_type: RoundTypeReport,
    threshold: int | None,
    flags: _Flags,
    settings: ReportSettings,
) -> RoundTypeView:
    """Assemble one round type's rows.

    Args:
        report_type: The round type's observations from the report.
        threshold: The repetition threshold, or ``None`` if there was none.
        flags: The report-wide collector. The explanations (unknown area,
            estimate, armed) are written **once** at the end of the report,
            so they have to be collected across all the round types and not
            within one.
        settings: The pruning rules (Story 2.13). The section is handed in
            whole, because a protected round type is resolved here -- see
            :meth:`_Pruning.for_round_type`.
    """
    pattern_only = report_type.round_type in PATTERN_ROUND_TYPES
    dropped_before = flags.dropped
    # On saving rounds every observation is written (min_n = 1); on full buys
    # only the repeating ones. The threshold comes from the report, not from
    # here.
    min_n = threshold if (pattern_only and threshold is not None) else 1

    pruning = _Pruning.for_round_type(settings, report_type.round_type)
    lines, kept_the_block = _round_type_lines(
        report_type, min_n, flags, pruning
    )

    # The two things about filtering -- the rule and its price -- are on the
    # same row: the report is short, and two italic footnotes after every
    # default block would cost eight rows per map to say one thing.
    #
    # **The number is the same with and without pruning.** It is a claim
    # about the data ("this many observations did not repeat enough"), not a
    # presentation choice, and pruning does not touch it: the rows are built
    # before the pruning and the bookkeeping transfers from dropped rows too
    # (:meth:`_Flags.absorb`).
    dropped = flags.dropped - dropped_before
    notes: list[str] = []
    if pattern_only and threshold is None:
        notes.append(
            "Toistumisen kynnystä ei ollut raportissa "
            "(thresholds_used.thresholds.small_sample_rounds), joten "
            "yksittäisiäkään havaintoja ei suodatettu pois."
        )
    elif pattern_only:
        note = f"Vain kuviot, jotka toistuvat vähintään {threshold} kierroksella"
        note += (
            f"; {dropped} harvinaisempaa havaintoa jäi pois."
            if dropped
            else "; jokainen havainto ylitti kynnyksen."
        )
        notes.append(note)
    # On saving rounds the threshold is 1, and no bar of a distribution can
    # fall below it: the model demands ``n > 0`` from every one. Telling of
    # filtering would therefore be a claim about a threshold that never
    # applied -- which is why there is no third branch here. The counter is
    # protected at the source: ``flags.dropped`` grows only when
    # ``min_n > 1``.
    if not lines:
        # Two different things: the threshold ate everything, or there were
        # no observations in the first place. The same sentence for both
        # would hide the difference.
        notes.append(
            "Ei kuvioita, jotka ylittäisivät kynnyksen."
            if pattern_only and threshold is not None
            else "Ei havaintoja tältä kierrostyypiltä."
        )
    # Last, because this is an **exception** and not the block's rule: the
    # threshold's note says how the block was assembled, and this says what
    # was then left undone.
    if kept_the_block:
        notes.append(_PRUNING_KEPT_THE_BLOCK)

    heading = _capitalise(
        ROUND_TYPE_FI.get(report_type.round_type, report_type.round_type)
    )

    return RoundTypeView(
        round_type=report_type.round_type,
        heading=heading,
        rounds_text=rounds_text(report_type.sample.rounds),
        small_sample=report_type.small_sample,
        pattern_only=pattern_only,
        lines=tuple(lines),
        notes=tuple(notes),
    )


def _anomaly_views(
    report: Report,
) -> tuple[tuple[AnomalyView, ...], str | None]:
    """The anomaly rows and the row cap's note.

    Two return values, because the cap cannot be applied in silence: the
    number left out is written out under the same rule as the observations
    the pattern threshold dropped.

    **The order is by repetition, not by map.** The chapter is the report's
    first content chapter and its job is to raise what repeats; on a tie the
    key is (map, side, rule, area), so the equally often observed stay in map
    order and the result is the same from one run to the next.
    """
    ordered = sorted(report.anomalies, key=_anomaly_rank)
    kept = ordered[:MAX_ANOMALY_LINES]
    dropped = len(ordered) - len(kept)
    note = None
    if dropped:
        note = (
            f"{dropped} poikkeamaa jäi pois: luvussa näytetään enintään "
            f"{MAX_ANOMALY_LINES} useimmin toistuvaa. Kaikki ovat "
            "report.jsonissa kentässä anomalies."
        )
    # The maps' ordinals from the same source as in the traceability
    # chapter, so that an unrecognised map's label is the same string in both
    # and the reader can connect the rows.
    index_of = {
        entry.map_name: index
        for index, entry in enumerate(report.maps, start=1)
    }
    return tuple(_anomaly_view(entry, index_of) for entry in kept), note


def _anomaly_rank(anomaly: Anomaly) -> tuple[int, str, str, int, str]:
    """Most often repeated first, then map, side, rule and area."""
    return (
        -anomaly.n,
        anomaly.map_name,
        anomaly.side,
        ANOMALY_RULES.index(anomaly.rule),
        anomaly.area,
    )


def _anomaly_view(
    anomaly: Anomaly, index_of: Mapping[str, int]
) -> AnomalyView:
    """One anomaly: the summary row and the round rows.

    **Simultaneity does not cross the round boundary.** The summary row gives
    the area, the sample and the orientation; the sample points, the
    directions and the player count are on the round rows, because only there
    are they simultaneous. The union of two rounds' directions would read as
    more simultaneous directions than were observed -- the opposite of the
    definition.

    The label's map goes through :func:`_anomaly_map_label`, so an
    unrecognised map does not bring a demo id into the body (Story 2.12).
    """
    label = (
        f"{ANOMALY_RULE_FI[anomaly.rule]} "
        f"({_anomaly_map_label(anomaly, index_of)}, {anomaly.side}-puoli"
        f"{_round_type_suffix(anomaly)})"
    )
    return AnomalyView(
        rule=anomaly.rule,
        line=Line(
            label=label,
            claims=(
                Claim(
                    text=_area(anomaly.area),
                    n=anomaly.n,
                    m=anomaly.m,
                    extra=_anomaly_extra(anomaly),
                ),
            ),
            note="pieni otanta" if anomaly.small_sample else None,
        ),
        rounds=tuple(_anomaly_round_text(anomaly, entry) for entry in anomaly.rounds),
    )


def _anomaly_extra(anomaly: Anomaly) -> str:
    """The row's extra information: the piece of evidence **this rule**
    measured.

    On the orientation rules it is the area's T share and its own sample. A
    stack does not have one -- the rule does not read the orientation at all
    -- and in its place comes what is derived in a stack: **the group**.
    Without it the row would look as though it claimed that four players were
    in one ``env_cs_place`` area; that is precisely what the rule does not
    claim and could not claim, and that is why the group exists.
    """
    if anomaly.rule != "stack":
        return _orientation_text(anomaly)
    # The group's name as a compound ("B-siten ryhmässä") and not as a word
    # pair ("siten B ryhmässä": the Finnish "siten" is then read as an
    # adverb). How it is derived is left to the reading guide -- the row
    # gives the observation, not the method.
    return f"{anomaly.site}-siten ryhmässä"


def _anomaly_map_label(anomaly: Anomaly, index_of: Mapping[str, int]) -> str:
    """The map's name on an anomaly row, an unrecognised one said out loud.

    **The body speaks in names** (Story 2.12), and its three exceptions do
    not cover this chapter. When the map name's source is ``unknown``,
    ``map_name`` **is** the demo id (see
    :class:`~pappascout.domain.report.MapReport`), so bare it would bring the
    id into the body. The map chapter's heading is an exception because there
    the id is the map's only name; here it is not, because the row gives the
    side, the area and the sample as well as the map.

    The label is **the same string** as on the traceability chapter's map row
    (:data:`UNKNOWN_MAP_LABEL`, :func:`_map_label`), so the reader can
    connect the row to the right map chapter. The ordinal is necessary: two
    unrecognised maps must not get the same label.

    **The name is protected as a code span** (:func:`_identifier`), because
    since Story 2.11 it is free text the demo gave and not a value validated
    against a map pool: a workshop map named ``*|Aim|* Botz [beta]`` is a
    legal observation, and bare it would break the row in the middle --
    precisely the row that carries the anomaly's sample. The protection was
    missing from here and from the map chapter's heading.

    **The same row's other half is the area's name**, and that too is text
    the demo gave; it is protected in :func:`_area` by escaping. Either half
    alone would leave the row breakable, and it was exactly that pair that
    went unnoticed while the rule was written under only one of the two
    function names.

    The mechanism is **the same as in the two other places where the same
    name is set** (:func:`_map_label`, the map chapter's heading), and it was
    not chosen again: a code span preserves exactly the same characters,
    whereas escaping would give this row a different spelling from the map
    chapter's -- and this row's whole job is to steer the reader to the right
    map chapter.

    An unrecognised map's label is **our own text**, so it is not protected:
    it holds not one character the demo gave.
    """
    if anomaly.map_name_source == "unknown":
        return UNKNOWN_MAP_LABEL.format(index=index_of.get(anomaly.map_name, 0))
    return _identifier(anomaly.map_name)


def _round_type_suffix(anomaly: Anomaly) -> str:
    """The round types into the label -- or a mention that they are no
    restriction.

    An advance is grouped by round type, so it has exactly one and it belongs
    in the label as an observation. Crunch knows no round type: its
    denominator is all the side's rounds, so the label says **on which types
    it was observed** and not what it is restricted to. Without the
    difference the reader would read a crunch's ``eco`` mark as a restriction
    and wonder why there is no ``default`` row.
    """
    names = ", ".join(
        ROUND_TYPE_FI.get(name, name) for name in anomaly.round_types
    )
    if anomaly.rule == "ct_advance":
        return f", {names}"
    return f", havaittu: {names}"


def _anomaly_round_text(anomaly: Anomaly, entry: AnomalyRound) -> str:
    """One round's observation: when, how many and from where.

    The round number first, because the scout's next act is to open that
    round in the demo. The round type is included only for crunch and stack:
    for an advance it is in the label already, and the same word is not
    written twice on the same row.

    **A stack's player count is a fraction and not a number.** "4 players"
    says nothing about the anomaly without the number alive: four out of five
    is the defence's choice, four out of four is what was left. The rule
    counts both, so the row says both as well.
    """
    text = f"kierros {entry.round_no}"
    if anomaly.rule != "ct_advance":
        text += f" ({ROUND_TYPE_FI.get(entry.round_type, entry.round_type)})"
    text += f": {_anomaly_points_text(entry)}"
    if entry.sources:
        # The directions only in a crunch, and **simultaneous** because they
        # are the same round's observation. On the others an empty list means
        # "not asked" and not "no directions", so it is not said out loud.
        text += f", yhtä aikaa suunnista {_areas_text(entry.sources)}"
    # Two demos on the same map: the round number does not identify without
    # the demo id. The id is a code span, because that is the only usable
    # form here -- the same rationale as with the round appendix.
    #
    # The count comes from the ROUNDS and not from the orientation. On the
    # orientation rules they are the same number (the model watches that the
    # orientation covers exactly the demos on which the anomaly was
    # observed), but a stack has no orientation -- and read from that the id
    # would be left out precisely when the map comes from two demos and the
    # reader needs it most.
    if len({round_entry.map_demo_id for round_entry in anomaly.rounds}) > 1:
        text += f" -- `{entry.map_demo_id}`"
    return text


def _anomaly_points_text(entry: AnomalyRound) -> str:
    """The round's sample points with their counts.

    **A number belongs to its moment.** When the sample points have different
    counts, each gets its own
    (``5/5 pelaajaa 15 s ja 4/5 pelaajaa 30 s kohdalla``); when the counts
    are the same, they are collapsed into one
    (``4/5 pelaajaa 15 ja 30 s kohdalla``). Collapsing is safe only because
    the condition compares **all** the counts: the row used to set the
    round's maximum for every sample point, and measured, it claimed five
    players on Inferno round 2 at 30 s as well, where there was one.
    """
    counts = {(point.players, point.alive) for point in entry.points}
    if len(counts) == 1:
        players, alive = counts.pop()
        seconds = _seconds_list([point.sample_t_s for point in entry.points])
        return f"{_players_of(players, alive)} {seconds} s kohdalla"
    parts = [
        f"{_players_of(point.players, point.alive)} "
        f"{_seconds(point.sample_t_s)} s"
        for point in entry.points
    ]
    return f"{_join_fi(parts)} kohdalla"


def _players_of(players: int, alive: int | None) -> str:
    """``4 pelaajaa``, or on a stack ``4/5 pelaajaa``.

    The number alive is **only on a stack**, because only that rule counts
    it. On the two other rules ``alive`` is ``None``, and an invented
    denominator would look on the row exactly like a measured one.
    """
    if alive is None:
        return players_text(players)
    return f"{players}/{alive} pelaajaa"


def _orientation_text(anomaly: Anomaly) -> str:
    """The area's T share and its own sample, one demo at a time.

    A share **without the number of observations** would be the most
    dangerous number on the whole row: 1.00 would look the same from one
    observation as from a hundred. Two demos from the same map can give the
    area different shares, and then both are written -- an average would be a
    number that has not been observed.

    The number of observations is **without parentheses**, because the whole
    extra is already inside the claim's parentheses: nested parentheses would
    force the row to be read twice.
    """
    parts = [
        f"T-osuus {_share(entry.t_share)} alueen "
        f"{entry.observations} havainnosta"
        for entry in anomaly.orientation
    ]
    return "; ".join(parts)


def _share(value: float) -> str:
    """A share to two decimal places with a Finnish comma."""
    return f"{value:.2f}".replace(".", ",")


def _seconds_list(values: Sequence[float]) -> str:
    """``[15.0, 30.0] -> '15 ja 30'``."""
    return _join_fi([_seconds(value) for value in values])


def _areas_text(areas: Sequence[str]) -> str:
    """The area names as a list; the callouts stay in English."""
    return _join_fi([_area(name) for name in areas])


def _join_fi(parts: Sequence[str]) -> str:
    """``a, b ja c`` -- a Finnish-language list, not a run of commas.

    The last separator is **ja** ("and") and not a comma, because the row is
    read as a sentence: "suunnista Arch, TopofMid" would look like a
    truncated list.
    """
    if len(parts) <= 1:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} ja {parts[-1]}"


def _no_anomalies_text(report: Report) -> str:
    """The text for an empty anomaly chapter -- the coverage included.

    **"No anomalies" is an observation only about what was examined.** The
    bare sentence without the coverage would claim a measured negative about
    a blind spot as well: unclassified rounds are scoped out, the orientation
    can be empty, and the site groups can go unobtained. Every one of them is
    said out loud here, because that difference ("an observation and not an
    absence") is the whole chapter's worth.

    Since Story 2.14 **all three of the architecture's rules (AD-10) are
    run**, so the sentence about deferred rules is only set if
    :data:`~pappascout.constants.ANOMALY_RULES_DEFERRED` fills up again.

    The method and the thresholds are there for the same reason: the reader
    of a clean report has to be told what was measured and within what
    bounds, and he sees not one row from which they could be inferred.
    """
    scan = report.anomaly_scan
    rules = _join_fi(
        [ANOMALY_RULE_FI.get(name, name) for name in scan.rules]
    )
    parts = [
        f"Ei poikkeamia. Säännöt ({rules}) ajettiin "
        f"{scan.rounds_scanned} kierrokselle, mutta kaikki tutkivat vain "
        f"CT-puolen rivejä: crunch voi osua {scan.crunch_rounds} "
        f"kierroksella, CT-eteneminen {scan.advance_rounds} "
        "kierroksella, koska se on rajattu säästökierroksiin, ja stack "
        f"{scan.stack_rounds} kierroksella -- se lukee vain demot, joista "
        "siteryhmät saatiin johdettua."
    ]
    if scan.rules_deferred:
        parts.append(
            f"Arkkitehtuuri nimeää {len(scan.rules) + len(scan.rules_deferred)} "
            f"poikkeamasääntöä; näistä {len(scan.rules_deferred)} on "
            f"toteuttamatta ({', '.join(scan.rules_deferred)}), joten tämä "
            "luku ei kattavuudeltaan vastaa niitä."
        )
    if report.unclassified_rounds:
        parts.append(
            f"{report.unclassified_rounds} kierrosta jäi kokonaan tutkimatta, "
            "koska niiden kierrostyyppi puuttuu."
        )
    if scan.demos_without_orientation:
        parts.append(
            f"{demos_text(len(scan.demos_without_orientation))} ei antanut "
            "yhdellekään alueelle puoliorientaatiota, joten CT-etenemisen ja "
            "crunchin vaikeneminen niissä on sokea piste eikä havainto."
        )
    else:
        parts.append(
            "Jokainen demo antoi vähintään yhdelle alueelle "
            "puoliorientaation, joten sokeita pisteitä ei ole."
        )
    # The stack's blind spot is **not here** but in the reading guide
    # (:func:`_stack_legend`). From two places it would be set into an empty
    # chapter twice, and the reading guide is the place that is written also
    # when there are anomalies -- that is, precisely when the silenced map is
    # at its most invisible.
    return " ".join(parts)


def _round_type_rank(round_type: str) -> int:
    return (
        ROUND_TYPE_ORDER.index(round_type)
        if round_type in ROUND_TYPE_ORDER
        else len(ROUND_TYPE_ORDER)
    )


# -- The summary -----------------------------------------------------------------


def _sample_text(sample: Any) -> str:
    """The sample in three buckets. All three always, the empty ones too."""
    parts = [
        f"{SAMPLE_BUCKET_FI[name]} {getattr(sample, name).demos} / "
        f"{getattr(sample, name).rounds}"
        for name in SAMPLE_BUCKETS
    ]
    return (
        f"{demos_text(sample.demos)}, {rounds_text(sample.rounds)} "
        f"(demoa/kierrosta: {', '.join(parts)})"
    )


def _roster_sample_text(sample: Any) -> str:
    """The roster breakdown as one line. All three buckets, empty ones too.

    Reads and formats; it counts nothing (AD-8). Every number here is a field
    of ``report.json``, so the line cannot disagree with the sample it
    describes. Whether the line is written at all is decided by the caller --
    an all-unknown split is noise dressed as information.

    The totals are deliberately absent: they are the same demos and rounds
    the ``Otanta`` row already states, and repeating them would invite the
    reader to check two numbers that cannot differ (``Report`` rejects the
    report if they do).

    **Why this row does not use the shape its sibling uses.** ``Otanta``
    writes ``liiga 0 / 0, muut 0 / 0`` -- units named once in the heading,
    then bare numbers. That shorthand cannot carry these bucket names,
    because the name *is* ``5/5``: the row would read ``5/5 1 / 3``, three
    slashed numbers in a row, and the reader could not tell the label from the
    counts. So the units are repeated per number here, and the two rows differ
    on purpose. Should the class names ever stop containing a slash, this row
    should go back to the sibling's shape.
    """
    return ", ".join(
        f"{ROSTER_BUCKET_FI[name]}: {demos_text(getattr(sample, name).demos)} / "
        f"{rounds_text(getattr(sample, name).rounds)}"
        for name in ROSTER_BUCKETS
    )


def _roster_row(report: Report) -> str:
    """The ``Rosteriluokka`` row: the split, or one sentence saying there is none.

    Two branches, not one. For as long as ``select`` has not been run over the
    archive every demo is ``unknown``, and a three-bucket row would then read
    ``5/5: 0 demoa / 0 kierrosta, ...`` -- noise dressed as information. Same
    shape as the ``Liigatieto`` row: what is not confirmed is said once, in a
    sentence, instead of being spelled out as zeros.

    The gloss on ``4/5`` is attached **only when that bucket carries demos**.
    Explaining a notation that does not appear in the row's own numbers would
    make the reader look for it.

    Called only when the sample has demos; see :func:`_summary`.
    """
    roster = report.roster_sample
    known = [
        name
        for name in ROSTER_CLASS_BUCKET.values()
        if getattr(roster, name).demos
    ]
    if not known:
        return (
            "yhdenkään demon rosteriluokkaa ei ole vahvistettu: kaikki ovat "
            f"lokerossa {ROSTER_BUCKET_FI['unknown']}, eikä otanta erottele "
            f"{ROSTER_BUCKET_FI['full']}- ja "
            f"{ROSTER_BUCKET_FI['partial']}-karttoja"
        )
    text = _roster_sample_text(roster)
    if roster.partial.demos:
        text += (
            f" -- {ROSTER_BUCKET_FI['partial']} on kartta, jolla yksi pelaaja "
            "oli vakirosterin ulkopuolelta, joten se on heikompi havainto "
            "joukkueen vakiasetelmasta"
        )
    return text


def _flatten(values: Mapping[str, Any]) -> dict[str, str]:
    """Flatten the threshold dictionary into one level of ``key -> value``
    pairs.

    ``thresholds_used`` is sectioned (``{"thresholds": {...}, "aggregate":
    {...}}``), and printed with its braces it is the report's longest row --
    in a document that is read in the rush before a match. The section's name
    tells the reader nothing the key's name does not already tell, so it is
    dropped. If two sections used the same key with different values, the
    name gets a section prefix -- otherwise one of them would vanish.
    """
    flat: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, Mapping):
            for inner_key, inner in value.items():
                text = _value(inner)
                if flat.get(inner_key, text) != text:
                    flat[f"{key}.{inner_key}"] = text
                else:
                    flat[inner_key] = text
        else:
            flat[key] = _value(value)
    return flat


def _threshold_text(values: Mapping[str, str]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(values.items()))


def _value(value: Any) -> str:
    if isinstance(value, Mapping):
        return "{" + _threshold_text(_flatten(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "/".join(_value(v) for v in value)
    if isinstance(value, bool):
        return "kyllä" if value else "ei"
    if isinstance(value, float):
        return _seconds(value)
    return str(value)


def _pruning_value(value: Any) -> str:
    """One pruning setting's value for the summary row.

    An empty list is ``ei yhtään`` ("none at all") and not an empty string:
    the row ``skip_sample_seconds`` without a value would read as if the
    value had been lost on the way. The seconds are formatted with
    :func:`~pappascout.constants.seconds_label`, that is in the same way as
    the sample point rows' labels -- otherwise the summary and the body would
    speak of the same number in two ways.
    """
    if isinstance(value, list):
        return "/".join(seconds_label(item) for item in value) or "ei yhtään"
    return _value(value)


def _pruning_summary_text(settings: ReportSettings) -> str | None:
    """The pruning rules for the summary row, one key at a time.

    **A mechanical list and not a hand-written sentence**: it comes about
    from the section's fields, so a sixth rule is on the row as soon as it is
    in the section. A hand-written sentence would fall behind precisely when
    a rule is added.

    The shape is the same as on the neighbouring rows (``Luokittelun
    kynnykset``, ``Aggregoinnin kynnykset``), and the rationale is theirs:
    the reader judges a claim by how it was computed -- and pruning decides
    which claims he sees. Without the row the reader of a clean report could
    not know whether some rule was on, because the pruning paragraphs are
    written only about the rules that hit.

    **``None`` when every rule is off.** There is then nothing to report on
    the row, and writing it would break the story's most important promise:
    every rule off means that the report is **character for character** the
    one that was there before Story 2.13 -- and not one with one row more.
    The row therefore appears exactly when pruning is involved in deciding
    what the reader sees.

    The detection is mechanical for the same reason as the list: **every
    field's false value means "rule off"** (``False``, an empty list, ``0``),
    so a sixth rule fits here without a change. The condition is recorded in
    :class:`~pappascout.domain.models.ReportSettings`'s docstring, because it
    is a requirement on a future field.
    """
    values = settings.model_dump(mode="json")
    if not any(values.values()):
        return None
    return ", ".join(
        f"{key} {_pruning_value(values[key])}" for key in sorted(values)
    )


def _summary(
    report: Report, threshold: int | None, settings: ReportSettings
) -> list[SummaryItem]:
    """The summary's rows. Every item that could vanish is here.

    **The body speaks in names** (Story 2.12). The summary is the part of the
    report the reader sees first in the rush before a match, and it cannot
    speak in digests: the team's id, the lineup ids and the players'
    SteamID64s are in the chapter :data:`TRACEABILITY_HEADING`. A move and
    not a drop -- every id is still in the report, only in a different place.

    **Thresholds are not ids and do not move.** A threshold says *how* a
    number was computed, so the reader needs it to judge the claim; the same
    goes for the tool versions and the timestamp. An id changes not one
    number in the report -- it serves tracing only. That difference is
    exactly what settles which row belongs in the summary.
    """
    team = report.team
    items = [SummaryItem("Joukkue", _team_text(team))]
    if len(team.lineup_keys) > 1:
        items.append(SummaryItem("Kokoonpanot", _lineups_text(team, report)))
    if team.display_name_alternatives:
        items.append(
            SummaryItem(
                "Muut havaitut nimet",
                ", ".join(
                    markdown_text(name)
                    for name in team.display_name_alternatives
                )
                + " -- demot antavat "
                "joukkueelle useamman nimen; yllä on useimmin havaittu",
            )
        )
    roster_source = (
        "havaittu demoista" if team.roster_source == "lineups" else "joukkueindeksistä"
    )
    if team.roster:
        items.append(
            SummaryItem(
                "Rosteri",
                f"{len(team.roster)} pelaajaa ({roster_source}): "
                + ", ".join(_roster_text(entry) for entry in team.roster),
            )
        )
    else:
        items.append(
            SummaryItem("Rosteri", f"ei pelaajia ({roster_source} -- lähde tyhjä)")
        )

    items.append(SummaryItem("Otanta", _sample_text(report.sample)))

    # Both breakdown notes are silent on an empty sample. "Yhdenkään demon
    # lajia ei ole vahvistettu" is a claim about demos, and with none in the
    # sample there are no demos to make it about -- the Otanta row already
    # says "0 demoa", and the empty-data note says the rest. Same fault class
    # as an empty division in ``collect``, and the two rows have to agree on
    # it or the reader learns to distrust both.
    if report.sample.demos:
        if report.sample.league.demos == 0 and report.sample.other.demos == 0:
            items.append(
                SummaryItem(
                    "Liigatieto",
                    "yhdenkään demon lajia ei ole vahvistettu: kaikki ovat "
                    "lokerossa tuntematon, eikä otannassa ole yhtään "
                    "varmistettua liigaottelua",
                )
            )
        items.append(SummaryItem("Rosteriluokka", _roster_row(report)))

    if report.unclassified_rounds:
        items.append(
            SummaryItem(
                "Luokittelemattomat",
                f"{rounds_text(report.unclassified_rounds)} ilman kierrostyyppiä "
                "-- ei mukana yhdenkään väitteen otannassa",
            )
        )
    if report.unpaired_detonations:
        items.append(
            SummaryItem(
                "Parittomat räjähdykset",
                f"{report.unpaired_detonations} kpl ilman heittoriviä "
                "-- ei mukana utilityn luvuissa",
            )
        )
    if threshold is not None:
        items.append(
            SummaryItem(
                "Pieni otanta",
                f"alle {rounds_text(threshold)} merkitään "
                "(pieni otanta); havaintoa ei silti piiloteta",
            )
        )
    classify_used = _flatten(report.classify_thresholds)
    if classify_used:
        items.append(
            SummaryItem("Luokittelun kynnykset", _threshold_text(classify_used))
        )
    # The same threshold appears in both dictionaries, because the
    # aggregation stores its whole section. Printed twice it takes up space
    # without saying anything new -- but if the values differ, the difference
    # is exactly what the reader has to see, so only an identical pair is
    # dropped.
    aggregate_used = {
        key: value
        for key, value in _flatten(report.thresholds_used).items()
        if classify_used.get(key) != value
    }
    if aggregate_used:
        items.append(
            SummaryItem("Aggregoinnin kynnykset", _threshold_text(aggregate_used))
        )
    pruning_text = _pruning_summary_text(settings)
    if pruning_text is not None:
        items.append(SummaryItem("Karsinnan säännöt", pruning_text))
    tools = ", ".join(f"{k} {v}" for k, v in sorted(report.tool_versions.items()))
    items.append(
        SummaryItem(
            "Aineisto koottu",
            _generated_text(report.generated_at) + (f" ({tools})" if tools else ""),
        )
    )
    return items


def _lineups_text(team: Any, report: Report) -> str:
    """The body's lineup row: how many lineups were joined and on what
    condition.

    Three numbers and not one, because one is not checkable. ``lineup_keys``
    contains **the target's own lineup** (see
    :func:`~pappascout.domain.aggregate.lineups_of_same_team`, "``target``
    always included"), so a bare ``len`` would read as if one more had been
    joined than was. The row therefore gives the number joined *and* the
    total, and their sum is checkable by counting the traceability chapter's
    ids.

    The threshold (``[thresholds].team_identity_min_common``, AD-6) is read
    from the report under the same rule as the small-sample limit, and it is
    written on the row as on the neighbouring row ("alle 3 kierrosta
    merkitään"). Without it the row would claim a decision without a
    rationale. If there is no value, the row gives the rationale in words and
    does not invent a number.
    """
    total = len(team.lineup_keys)
    joined = total - 1
    count = "1 muu kokoonpano" if joined == 1 else f"{joined} muuta kokoonpanoa"
    min_common = _threshold_int(report, "team_identity_min_common")
    if min_common is None:
        rule = "yhteisten pelaajien perusteella"
    else:
        rule = f"vähintään {min_common} yhteisen pelaajan perusteella"
    return (
        f"{count} liitetty samaksi joukkueeksi {rule}; yhteensä {total} "
        f"kokoonpanoa, tunnisteet luvussa {TRACEABILITY_HEADING}"
    )


def _team_text(team: Any) -> str:
    """The team's name -- or an honest statement that there is none.

    The name is an **observation**: it is the demo's ``team_clan_name`` as
    the lineup table recorded it. Without the observation, repeating the
    digest in the name's place would claim that ``9ac92660986558d3`` is the
    team's name -- and that is exactly why the report says the absence out
    loud and does not invent a substitute.

    **The same rationale carries the id into a chapter of its own** (Story
    2.12). If the digest is not fit for the name's place, neither is it fit
    in parentheses after the name on the row the reader reads first: the row
    would then say two things, one of which is nothing to him. The id is in
    the chapter :data:`TRACEABILITY_HEADING`, where it is an id and not a
    name -- and the nameless team's row says so out loud, because for that
    reader the id is all there is of the team.
    """
    if _has_name(team):
        return markdown_text(team.display_name)
    return (
        "nimi ei ole tiedossa. Demoista ei löytynyt joukkueelle klaaninimeä "
        "(team_clan_name), eikä raportti keksi nimeä muusta lähteestä; "
        f"tunniste on luvussa {TRACEABILITY_HEADING}."
    )


def _has_name(team: Any) -> bool:
    """Whether the team's name is an observation or an id in its place.

    The source settles it, not a comparison with the id: a team could be
    named exactly like its own id, and the comparison would then claim the
    observation was missing.
    """
    return team.display_name_source == "clan_name"


#: A roster player whose name could not be read.
#:
#: A placeholder and not an omission: thanks to it the body's name list is
#: exactly as long as ``roster``, so the row's own count and the list cannot
#: disagree.
#:
#: The same text is the label of the traceability chapter's row, but **with
#: an ordinal** (``2. nimi ei luettavissa``). Without the number two nameless
#: players -- or two with the same name, which is ordinary in CS2 -- would
#: produce two identical labels, and the reader could not say which
#: SteamID64 belongs to which. The number is the place in the body's name
#: list, so the pair is found by counting and not by guessing.
UNNAMED_PLAYER = "nimi ei luettavissa"


def _roster_text(entry: Any) -> str:
    """One roster row in the body: **the name alone**.

    Story 2.6 decided "both, always": the name for readability and the
    SteamID64 beside it, because the id is the only traceable value. The
    rationale has not turned false -- the id is still the only value that
    does not change from one match to the next -- but **the place has**
    (Story 2.12). Seven 17-digit numbers beside the names make the body's row
    a list a human does not read in the rush before a match, and he does not
    need it there. The name -> SteamID64 pair is whole in the chapter
    :data:`TRACEABILITY_HEADING`.

    The return value is **the same string** as in the traceability chapter's
    label after the ordinal. A shared source makes the promise about the
    order checkable: if the name were written twice, the two spellings could
    drift and the reader would no longer find its pair.

    The name is a string the demo gave, so it goes through
    :func:`markdown_text`. A missing name is said out loud and the player is
    not dropped: he is in the roster's count, and his SteamID64 is in the
    chapter :data:`TRACEABILITY_HEADING`.
    """
    if entry.display_name:
        return markdown_text(entry.display_name)
    return UNNAMED_PLAYER


def _generated_text(moment: datetime) -> str:
    """The aggregation's timestamp.

    ``generated_at`` is UTC in ``aggregate``, but ``report.json`` is a text
    file: a naive timestamp or one from another zone can end up in it. The
    formatting **converts** a zoned value to UTC instead of gluing the
    letters "UTC" onto the end -- and it says so out loud if there is no
    zone.
    """
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M") + " (aikavyöhyke tuntematon)"
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# -- Technical traceability ------------------------------------------------------


def _identifier(value: str) -> str:
    """An id **literally**: as a code span and not as escaped text.

    The traceability chapter's whole worth is that an id can be copied out of
    the report into a command or a search engine. :func:`markdown_text` would
    protect Markdown's structural characters but would at the same time make
    the value a different string: the demo id ``ANCIENT_vs_RCAVE_VETERANS``
    would get a backslash before every one of its underscores and would no
    longer match any directory in the archive. A code span preserves the
    value byte for byte and prevents Markdown's interpretation at the same
    time -- the same choice as with the round appendix's paths.

    The backtick is the only character a code span cannot contain, and on
    Windows it is legal in a file name, that is possible in a demo id. The
    value is then escaped as text: a broken code span would set the rest of
    the report wrongly, which is worse than losing copyability on one row.
    """
    if "`" in value:
        return markdown_text(value)
    return f"`{value}`"


def _traceability(report: Report) -> list[SummaryItem]:
    """The traceability chapter's rows: every id the body does not speak.

    The chapter is **the other end of the move**. The body speaks in names
    (Story 2.12), and for the removal to be a move and not a drop, every id
    that left the body has to be here: the team's digest, the lineup digests,
    the players' SteamID64s and the maps' demo ids.

    The roster's rows are the pair **ordinal + name -> SteamID64** and not
    one list, because that pair is precisely the question the chapter
    answers: "who ``76561190000000001`` really is" is answerable from the
    report itself without ``report.json`` (the id in the example is invented;
    a real one is not written into the source). The ordinal is the place in
    ``roster``, that is the same as in the body's name list, and it is on the
    row because a name **is not a unique key**: two nameless players or two
    with the same name would without the number produce two identical
    labels.

    The chapter computes nothing and brings in no new value: all four sources
    are in ``Report`` already (``team.key``, ``team.lineup_keys``,
    ``roster[].player_id``, ``maps[].map_demo_ids``). That is exactly why a
    new CHAPTER does not this time mean a change to ``Report``.
    """
    team = report.team
    items = [SummaryItem("Joukkueen tunniste", _team_key_text(team))]
    # The same condition as on the body's lineup row. With one lineup a row
    # of its own would repeat the team's id word for word: ``team.key``
    # **is** the lineup id (``lineups_of_same_team`` always returns the
    # target included), so the repetition would not be traceability but
    # noise. Nor would it vanish -- the team row carries it, and says so out
    # loud.
    if len(team.lineup_keys) > 1:
        items.append(
            SummaryItem(
                "Kokoonpanotunnisteet",
                ", ".join(_identifier(key) for key in team.lineup_keys),
            )
        )
    for index, entry in enumerate(team.roster, start=1):
        items.append(
            SummaryItem(
                f"{index}. {_roster_text(entry)}", _identifier(entry.player_id)
            )
        )
    for index, map_report in enumerate(report.maps, start=1):
        items.append(
            SummaryItem(
                _map_label(index, map_report),
                ", ".join(
                    _identifier(demo_id) for demo_id in map_report.map_demo_ids
                ),
            )
        )
    return items


def _team_key_text(team: Any) -> str:
    """The team's id, and with one lineup what it also is.

    Before Epic 3 ``team.key`` is a lineup id, so for a team with one lineup
    it **is** that one lineup id. Two rows with the same value under
    different labels would read as if there were two values; one row that
    says it is both tells the same without the repetition.
    """
    if len(team.lineup_keys) == 1:
        return (
            f"{_identifier(team.key)} -- sama arvo kuin joukkueen ainoa "
            "kokoonpanotunniste"
        )
    return _identifier(team.key)


def _map_label(index: int, map_report: Any) -> str:
    """The map row's label in the traceability chapter.

    **The name as a code span and not as escaped text.** Since Story 2.11 the
    map's name is an observation from the demo's header, and it is not
    validated against a map pool -- a workshop map named ``*|Aim|* Botz
    [beta]`` is a legal observation. Bare it would break the row: the label's
    bold would be left unclosed, and it is this row that carries the demo
    ids, that is the whole chapter's purpose. Escaping, in turn, would give
    the map a different spelling from the map chapter's heading, and the
    report is read raw as well. A code span does neither: it preserves
    **exactly the same characters** and still prevents Markdown's
    interpretation.

    When the name was not recognised, ``map_name`` is the ``map_demo_id``
    itself (see :class:`~pappascout.domain.report.MapReport`). The label and
    the value would then be the same string, that is, the row would say
    nothing: the label says instead what it is about, and the ordinal says
    which map chapter the row concerns -- two unrecognised maps must not get
    the same label.
    """
    if map_report.map_name_source == "unknown":
        return UNKNOWN_MAP_LABEL.format(index=index)
    return _identifier(map_report.map_name)


#: The traceability chapter's explanation.
#:
#: The product owner, 2026-08-31: *"to a human the hashes and ids mean
#: nothing at all, but to the project and how it works they are valuable"*.
#: Both things are true in the same sentence, so dropping them would serve
#: only the first. The chapter's explanation says this out loud, so that the
#: next reader does not take the chapter for a leftover.
_TRACEABILITY_NOTE = (
    "Tunnisteet, jotka eivät ole rungossa: joukkueen ja kokoonpanojen "
    "tiivisteet, pelaajien SteamID64 ja karttojen demotunnisteet. Mitään ei "
    "ole poistettu -- ne ovat täällä, koska ne palvelevat vain jäljittämistä. "
    "Kynnykset, työkaluversiot ja aikaleima jäivät yhteenvetoon, koska ne "
    "kertovat miten luku laskettiin, eikä väitettä voi arvioida ilman niitä; "
    "tunniste ei muuta yhtäkään raportin lukua. Rungossa tunniste on vain "
    "siellä, missä se on ainoa käyttökelpoinen muoto: kierrosliitteen "
    "polussa, puuttuvan demon komennossa ja kartassa, jonka nimeä ei "
    "tunnistettu."
)


# -- The public build function ---------------------------------------------------


def build_view(
    report: Report,
    *,
    settings: ReportSettings,
    round_list_paths: Sequence[str] = (),
) -> ReportView:
    """Build the view model out of the report.

    Args:
        report: The ``aggregate`` stage's result as it is.
        settings: The ``[report]`` section, that is the pruning rules (Story
            2.13). **Mandatory and not defaulted**: a default that differed
            from ``settings.toml`` would prune silently in a different way
            from what the user's file says -- and a default that is the same
            would be a copy of the settings file in the code. Pruning
            concerns **the presentation and not the content**: every pruned
            value is still in ``report.json``.
        round_list_paths: The round lists' paths, which the ``render`` stage
            has resolved out of ``archive.paths``. The view **does not build
            the paths itself**: it does not see the archive (the layering
            rule), and the archive's directory structure must not be written
            in two places. An empty list means "no paths were given", and the
            round appendix says so instead of ending in a colon without a
            list.

    Returns:
        A :class:`ReportView` every number of which comes from the report.
    """
    threshold = pattern_min_rounds(report)
    flags = _Flags()

    maps: list[MapView] = []
    for map_report in report.maps:
        sides: list[SideView] = []
        for side in map_report.sides:
            views: list[RoundTypeView] = []
            for entry in sorted(
                side.round_types, key=lambda rt: _round_type_rank(rt.round_type)
            ):
                views.append(
                    _round_type_view(entry, threshold, flags, settings)
                )
            sides.append(
                SideView(
                    side=side.side,
                    heading=f"{side.side}-puoli",
                    rounds_text=rounds_text(side.sample.rounds),
                    round_types=tuple(views),
                    note=None if views else _NO_ROUND_TYPES,
                )
            )
        # The heading is assembled whole here, not in the template. In the
        # template it would need an inline condition, and Jinja's
        # ``trim_blocks`` eats the newline after every block tag that ends a
        # line -- no blank line would then be left between the heading and
        # the next heading.
        # The mark belongs **only** to the source ``unknown``. Both
        # ``demo_header`` and ``map_demo_id`` are recognised names: the
        # former is an observation from the demo's header, the latter an
        # inference from the id.
        name_unknown = map_report.map_name_source == "unknown"
        # The name is protected as a code span (``_identifier``), because
        # since Story 2.11 it is free text the demo gave: a workshop map
        # named ``*|Aim|* Botz [beta]`` is a legal observation, and bare it
        # broke the heading in the middle. There are four strings in the body
        # that the demo gave -- the team's name, a player's name, the map's
        # name and **the area's name** (:func:`_area`) -- and the map's name
        # was unprotected in two places out of three.
        #
        # A CODE SPAN AND NOT ESCAPING, and the mechanism is borrowed from
        # ``_map_label`` rather than chosen again: that place argues the same
        # choice word for word for the map's name in particular, and escaping
        # would give the map **a different spelling** from the one on the
        # traceability chapter's row. The report is read raw as well, and the
        # same map in two spellings would read as two maps.
        heading = (
            f"{_identifier(map_report.map_name)} -- "
            f"{rounds_text(map_report.sample.rounds)}, "
            f"{demos_text(map_report.sample.demos)}"
        )
        if name_unknown:
            heading += " (kartan nimeä ei tunnistettu tunnisteesta)"
        maps.append(
            MapView(
                map_name=map_report.map_name,
                heading=heading,
                name_unknown=name_unknown,
                sides=tuple(sides),
                note=None if sides else _NO_SIDES,
            )
        )

    anomaly_views, dropped_note = _anomaly_views(report)
    return ReportView(
        title=_title(report),
        summary=tuple(_summary(report, threshold, settings)),
        anomalies=anomaly_views,
        anomalies_note=None if anomaly_views else _no_anomalies_text(report),
        anomalies_dropped_note=dropped_note,
        # The label is a demo id and it **stays on the body's row**: the
        # reason contains a command the reader copies (``uv run pappascout
        # parse <demo>``), and the command does not work without the id. A
        # code span because the id has to survive copying -- the same
        # rationale as with the traceability chapter's values and the round
        # appendix's paths.
        missing_demos=tuple(
            SummaryItem(_identifier(entry.match), entry.reason)
            for entry in report.missing_demos
        ),
        maps=tuple(maps),
        legend=tuple(_legend(flags, report, settings)),
        appendix_note=(
            _APPENDIX_NOTE if round_list_paths else _APPENDIX_NOTE_WITHOUT_PATHS
        ),
        appendix_paths=tuple(round_list_paths),
        traceability=tuple(_traceability(report)),
        traceability_note=_TRACEABILITY_NOTE,
        empty_note=None if report.maps else _EMPTY_NOTE,
    )


def round_list_demo_ids(report: Report) -> list[str]:
    """The demos a round list is named for -- one per demo that reached the
    report.

    Separated from the view because building a path belongs to the stage:
    ``render`` sees ``archive.paths``, the view does not. This function says
    **which demos** the paths are made from; the stage says **where** they
    point.
    """
    demos: list[str] = []
    for map_report in report.maps:
        demos.extend(map_report.map_demo_ids)
    return sorted(set(demos))


def _title(report: Report) -> str:
    """The report's title.

    The name goes into the title only if it has been **observed**. Without
    the observation, no digest is written into the title in the name's place:
    ``# 9ac92660986558d3 -- scouting-raportti`` reads as if the team were
    named that. The id is in the chapter :data:`TRACEABILITY_HEADING`, where
    it is an id and not a name.
    """
    team = report.team
    if _has_name(team):
        return f"{markdown_text(team.display_name)} -- scouting-raportti"
    return "Scouting-raportti -- joukkueen nimi ei tiedossa"


#: A side that has no round type at all. A heading without content would look
#: like an interrupted report.
_NO_ROUND_TYPES = (
    "Ei yhtään luokiteltua kierrostyyppiä tällä puolella. Kierrokset ovat "
    "otannassa mukana, mutta niistä ei syntynyt yhtäkään kierrostyyppitasoa."
)

#: A map that has neither side.
_NO_SIDES = (
    "Ei havaintoja kummaltakaan puolelta. Kartta on otannassa mukana, mutta "
    "sen kierroksista ei syntynyt puolitason haaraa."
)


#: The text for an empty report. The summary is written all the same -- the
#: sample, the missing demos and the thresholds are exactly the information
#: an empty report needs.
_EMPTY_NOTE = (
    "Aineistoa ei ole: yhtään karttaa ei saatu raporttiin. Yhteenvedon otanta "
    "ja puuttuvat demot kertovat miksi."
)


def _legend(
    flags: _Flags, report: Report, settings: ReportSettings
) -> list[str]:
    """The explanations that are written once at the end of the report.

    ``report`` is an argument because the anomaly rules' explanation names
    **the thresholds they were run with**. They are read from the report and
    not invented here -- the same rule as with the pattern threshold
    (:func:`pattern_min_rounds`): an adjusted ``settings.toml`` shows in the
    report's text only if the text comes from the report.

    ``settings`` is an argument because of the pruning rules (Story 2.13),
    and it is a different source for a different reason: a pruning limit is
    not in ``report.json`` and does not belong there -- it is a presentation
    choice made in the rendering, whereas a threshold is a value that affects
    the aggregation's numbers. The limit's number still has to be written
    into the reading guide, because "3 harvinaisempaa aluetta jäi pois" at
    the end of a row does not say how many stayed.
    """
    notes: list[str] = []
    notes.append(
        "Jokainen väite kantaa otantansa muodossa (n/m kierroksesta): n on "
        "kierrokset, joissa havainto tehtiin, m kyseisen kierrostyypin kaikki "
        "kierrokset. Mediaanin otanta rivin otsikossa (esimerkiksi "
        "\"mediaani 14,2 s, 7/9 kierroksesta\") noudattaa tätä sääntöä: se "
        "kertoo, monellako kierroksella ajoitus mitattiin. Saman rivin "
        "aluevaateet laskevat sen sijaan vain niitä kierroksia, joilla "
        "havainto oli olemassa, joten niiden nimittäjä on pienempi."
    )
    notes.append(
        "Ensikontaktin rivi kertoo elossa olevat pelaajat alueittain sillä "
        "hetkellä, kun kierroksen ensimmäinen ristiinpuolinen osuma tapahtui."
    )
    notes.extend(_anomaly_legend(report))
    if flags.unknown_area:
        notes.append(
            f"{UNKNOWN_AREA}: pelin aluenimeä ei saatu. Koordinaatteja ei ole "
            "report.jsonissa, joten sijaintia ei voi tarkentaa tässä raportissa."
        )
    if flags.estimated_area:
        notes.append(
            "(arvio) räjähdysalueen perässä: kranaatilla ei ole aluenimeä, joten "
            "alue on luettu demon pistepilvestä -- siitä kohdasta kartalla, "
            "jossa pelaajat ovat lähinnä räjähdystä oikeasti seisoneet."
        )
    notes.extend(_player_counter_legend(flags))
    if flags.kills_shown:
        notes.append(
            "Tapot alueittain: alue on **ampujan** oma alue tappohetkellä, ja "
            f"otanta (n/m {KILL_SAMPLE_UNIT}) laskee tappoja eikä kierroksia "
            "-- kierrostyypillä on yleensä enemmän tappoja kuin kierroksia."
        )
    notes.extend(_pruning_legend(flags, settings))
    notes.append(
        "Runko puhuu nimillä: joukkueen ja kokoonpanojen tiivisteet, "
        "pelaajien SteamID64 ja karttojen demotunnisteet ovat raportin "
        f"viimeisessä luvussa {TRACEABILITY_HEADING}. Kolme poikkeusta, "
        "joissa tunniste on rungossa siksi että se on siellä ainoa "
        "käyttökelpoinen muoto: kierrosliitteen polut, puuttuvan demon rivi "
        "(tunniste on osa komentoa, jonka voi kopioida) ja kartta, jonka "
        "nimeä ei tunnistettu (tunniste on kartan ainoa nimi)."
    )
    notes.append(
        "Raportti kuvaa vain havainnot. Tulkinta ja vastastrategia ovat lukijan."
    )
    return notes


def _protected_round_types_text() -> str:
    """The protected round types as a sentence for the reading guide.

    The sentence is **derived** from :data:`PROTECTED_ROUND_TYPES` and not
    hand-written, and it is attached to every pruning paragraph. Two reasons:

    1. A paragraph that says "the two most common targets are written" is an
       unqualified sentence, and **in the same report** a protected round
       type's block prints four of them. Without the exception sentence the
       reading guide claims more about the report than the report does.
    2. When a sixth type joins the list, every paragraph says so by itself --
       hand-written, one of them would fall behind.

    The order comes from :data:`ROUND_TYPE_ORDER`, so that the sentence is
    the same from one run to the next (a ``frozenset`` is not ordered).
    """
    names = _join_fi(
        [
            ROUND_TYPE_FI.get(name, name)
            for name in ROUND_TYPE_ORDER
            if name in PROTECTED_ROUND_TYPES
        ]
    )
    return f"Karsinta ei koske näitä kierrostyyppejä: {names}."


def _pruning_legend(flags: _Flags, settings: ReportSettings) -> list[str]:
    """The pruning rules' explanations: **what a missing row means**.

    A paragraph is written only about a rule that **really pruned
    something** in this report -- not about every rule that is on. The
    difference matters and it is the same rule as elsewhere in this function
    (:func:`_player_counter_legend`, ``flags.kills_shown``): the reading
    guide explains what is in the report or what is missing from it, and not
    what the code is able to do. An explanation of a rule that never hit
    would tell the reader about a missing row that does not exist -- that is,
    it would be a claim about the report that does not hold.

    The same requirement concerns the **bounds**: every paragraph says that
    pruning does not concern the protected round types
    (:func:`_protected_round_types_text`), because their blocks are in the
    same report unpruned.

    Every paragraph also names the **setting** the rule is switched off with.
    Pruning is adjustable by the product owner without a code change, and it
    is not adjustable if the report does not say what the value being
    adjusted is called.
    """
    notes: list[str] = []
    exception = _protected_round_types_text()
    if flags.saturated_dropped:
        notes.append(
            "**Kylläinen kalustorivi on jätetty pois.** Kun jakaumassa on "
            f"vain arvo {PLAYERS_ON_SERVER} ja havainto saatiin joka "
            "kierrokselta, rivi sanoo että kaikilla viidellä oli panssari "
            "(tai ase) joka kierroksella -- se on odotus eikä havainto. Luku "
            "on yhä report.jsonissa. **Kylläisyys ei ole ainoa syy, jonka "
            "takia kalustorivi voi puuttua**: täydellä ostolla myös "
            "toistumisen kynnys voi pudottaa sen, ja silloin lohkon oma "
            f"huomautus kertoo siitä. {exception} Asetus: "
            "[report].drop_saturated_equipment_lines."
        )
    if flags.equipment_merged:
        notes.append(
            "**Aseistettujen ja panssaroitujen rivi on kirjoitettu yhtenä** "
            "silloin, kun jakaumat ovat identtiset: luvut on luettu samalta "
            "tickiltä samasta pelaajajoukosta ja samalla jakajalla, joten "
            "toinen rivi ei kertoisi mitään uutta. Kaksi erillistä riviä "
            f"tarkoittaa siis, että luvut eroavat -- ja se ero on havainto. "
            f"{exception} Asetus: [report].merge_equal_equipment_lines."
        )
    if flags.skipped_samples:
        samples = _join_fi([f"{value} s" for value in flags.skipped_samples])
        notes.append(
            f"**Näytepistettä {samples} ei kirjoiteta tähän raporttiin.** "
            "Puuttuva näytepiste ei tarkoita puuttuvaa havaintoa: se on "
            "report.jsonissa ja parsituissa tauluissa sellaisenaan, eikä "
            "[parse].snapshot_seconds ole muuttunut -- kyse on vain siitä, "
            "tulostetaanko rivi. Myöhäinen näytepiste kertoo eloonjääneistä "
            f"eikä asetelmasta. {exception} Asetus: "
            "[report].skip_sample_seconds."
        )
    if flags.utility_targets_capped:
        notes.append(
            "Utilityn kohderiviltä kirjoitetaan "
            f"{settings.max_utility_targets} yleisintä **kohdetta** (sama "
            "kohde voi olla rivillä useammin kuin kerran: eri heittoalueelta "
            "tai eri aikaikkunassa), ja rivin perässä on niiden kohteiden "
            "määrä, jotka jäivät pois; rivi, jolla on kohteita viidestä "
            "yhdeksään, on luettelo eikä kuvio. Jokainen kohde on yhä "
            f"report.jsonissa kentässä utility. {exception} Asetus: "
            "[report].max_utility_targets."
        )
    if flags.kill_areas_capped:
        notes.append(
            f"Tapporiviltä kirjoitetaan {settings.max_kill_areas} yleisintä "
            "aluetta samalla säännöllä, ja pois jääneiden määrä on rivin "
            "perässä. Yhtä yleiset alueet säilyvät molemmat, joten rivillä "
            "voi olla rajaa enemmän alueita. Jokainen alue on yhä "
            f"report.jsonissa kentässä deaths.kills. {exception} Asetus: "
            "[report].max_kill_areas."
        )
    return notes


def _anomaly_legend(report: Report) -> list[str]:
    """The anomaly chapter's explanations: the method and **every rule's
    conditions**.

    Three paragraphs about the rules that have been implemented, and they are
    written **into an empty chapter too**: the orientation's method and the
    advance's and the crunch's conditions. :func:`_stack_legend` adds two
    (the stack's conditions and its coverage), so with the present set of
    rules there are five paragraphs -- but the number follows
    :data:`ANOMALY_RULES` and is not a constant: if stack ever returns to the
    deferred list, its two paragraphs are left out and the reading guide does
    not explain a row that cannot exist.
    The rationale differs from the reading guide's other paragraphs: these do
    not explain a row that is in the report but **what was measured**. The
    reader of a clean report needs them more than anyone: he sees only the
    claim "ei poikkeamia" and not one row from which the method could be
    inferred.

    The rules' asymmetry is said out loud, because it is invisible otherwise:
    the advance is restricted to saving rounds, crunch and stack are not, so
    the same area can appear on an ``eco`` row but not on a ``default`` row
    -- and without the explanation that looks like a missing observation.
    """
    share = _threshold_float(report, "advance_t_share")
    observations = _threshold_int(report, "advance_area_min_observations")
    bound = _threshold_float(report, "advance_max_sample_s")
    advance_players = _threshold_int(report, "advance_min_players")
    crunch_players = _threshold_int(report, "crunch_min_players")
    crunch_sources = _threshold_int(report, "crunch_min_sources")

    orientation = (
        f"Luvun {ANOMALY_HEADING} T-osuus on **demon oma havainto** siitä, "
        "kumman puolen aluetta alue on: se on alueen elossa-havainnoista "
        "aikanäytepisteillä laskettu T-puolen osuus, **molempien joukkueiden** "
        "riveistä. Ei karttatietokantaa eikä käsin annettua aluejakoa -- ja "
        "eri demo voi antaa samalle alueelle eri osuuden, joten havaintomäärä "
        "on osuuden vieressä."
    )
    if share is not None and observations is not None:
        orientation += (
            f" Alue on T:n aluetta, kun osuus on vähintään {_share(share)} ja "
            f"alueella on vähintään {observations} havaintoa; sitä vähemmällä "
            "alue ei ole kummankaan puolen aluetta eikä tuota poikkeamaa."
        )
    notes = [orientation]

    advance = (
        f"**{ANOMALY_RULE_FI['ct_advance']}**: subjektin CT-pelaaja alueella, "
        "joka on siinä demossa T:n hallussa, **säästökierroksella** (eco, "
        "force tai puoliosto)."
    )
    if advance_players is not None and bound is not None:
        advance += (
            f" Vähintään {players_text(advance_players)} alueella ja havainto "
            f"enintään {_seconds(bound)} sekunnin kohdalla kierroksen alusta."
        )
    notes.append(advance)

    crunch = (
        f"**{ANOMALY_RULE_FI['crunch']}**: sama T:n alue, mutta pelaajien on "
        "**saavuttava** sinne yhtä aikaa eri suunnista -- lähtösuunta on "
        "pelaajan oma alue edellisellä näytepisteellä."
    )
    if crunch_players is not None and crunch_sources is not None:
        crunch += (
            f" Vähintään {players_text(crunch_players)} ja "
            f"{crunch_sources} eri suuntaa."
        )
    crunch += (
        " **Crunchia ei ole rajattu kierrostyyppiin**, toisin kuin etenemistä, "
        "joten sen otanta on puolen kaikki kierrokset ja nimiö kertoo millä "
        "kierrostyypeillä se havaittiin. Sama kierros voi siis tuottaa "
        "molemmat rivit, ja täysi osto vain crunchin."
    )
    notes.append(crunch)
    notes.extend(_stack_legend(report))
    return notes


def _stack_legend(report: Report) -> list[str]:
    """The stack's two paragraphs: what the rule is and what it did not see.

    **The coverage is here and not only in the empty chapter's text.** A map
    like Nuke stays silent also when there are hits on other maps -- and
    precisely then the reader sees a chapter without Nuke in it and nothing
    that says why. The empty chapter's text (:func:`_no_anomalies_text`) is
    then not set at all.

    The group is said to be **derived** and not given, because that is the
    whole rule's rationale: the division into areas is in no database but is
    computed from that demo's own point cloud every time.
    """
    scan = report.anomaly_scan
    if "stack" not in scan.rules:
        # The rule was not run, so explaining it would describe rows that
        # cannot exist. The coverage is then told by rules_deferred.
        return []
    players = _threshold_int(report, "stack_min_players")
    margin = _threshold_float(report, "stack_group_margin")

    rule = (
        f"**{ANOMALY_RULE_FI['stack']}**: subjektin puolustus kasautuneena "
        "yhden siten ympärille. Alueryhmä on **johdettu tästä demosta**: "
        "jokaisen alueen keskipiste lasketaan demon omasta pistepilvestä, ja "
        "alue kuuluu lähemmän siten ryhmään"
    )
    if margin is not None:
        rule += f", jos toinen site on vähintään {_ratio(margin)} kertaa kauempana"
    rule += (
        ". Ei karttatietokantaa eikä käsin annettua aluejakoa. Osuma vaatii "
    )
    if players is not None:
        rule += f"vähintään {players_text(players)} saman siten ryhmässä ja "
    rule += (
        "vähintään yhden heistä sitellä itsellään; spawnissa seisova ei "
        "laske. Rivin luku on muotoa 4/5 -- ryhmässä olleet kaikista elossa "
        "olleista. **Stackia ei ole rajattu kierrostyyppiin** eikä se lue "
        "alueen T-osuutta, joten se ei ole kummankaan toisen säännön tiukempi "
        "eikä löysempi muoto."
    )
    notes = [rule]

    # The sample in the same shape as the report's claims (n/m), because the
    # number is the same thing: how many of the rounds on which it could hit
    # the rule saw. The wording "N kierrosta M:sta" would inflect wrongly at
    # the number 1.
    coverage = (
        f"Stackin kattavuus on {scan.stack_rounds}/{scan.crunch_rounds} "
        "CT-kierroksesta."
    )
    if scan.demos_without_site_groups:
        coverage += (
            f" Erotus on {demos_text(len(scan.demos_without_site_groups))} "
            "ilman siteryhmiä: kartalla, jolla A ja B ovat päällekkäin eri "
            "kerroksissa (Nuke), etäisyys siteeseen ei kerro kummasta "
            "puolesta on kyse, eikä jakoa saa keksiä. **Sääntö vaikenee "
            "siellä**, ja vaikeneminen on oikea vastaus -- muttei havainto "
            "siitä, ettei stackeja ollut."
        )
    else:
        coverage += " Jokaiselta demolta saatiin siteryhmät."
    notes.append(coverage)
    return notes


def _ratio(value: float) -> str:
    """A ratio with a Finnish decimal comma and without needless zeros.

    ``1.25 -> '1,25'``, ``2.0 -> '2'``. Different from :func:`_share`, which
    forces two decimals: the share 1.0 is a different claim from 1, but the
    margin 2.00 is no more accurate than 2.
    """
    return f"{value:g}".replace(".", ",")


def _player_counter_legend(flags: _Flags) -> list[str]:
    """The player counters' explanations, one paragraph per counter shown.

    Three branches and not two independent sentences. When both rows are in
    the report, they are explained as **one paragraph**, because the numbers
    are nested and not parallel: the armed are a subset of the armoured, both
    are read from the same tick and the same set of players, and the divisors
    are the same. As two separate sentences "aseistettuja 0" and
    "panssaroituja 5" would be left as two loose numbers, and their
    difference is exactly what the rows say together.
    """
    nesting = (
        "Aseistettu = panssari JA parannettu ase ostoajan lopussa; "
        "panssaroitu = panssari, aseesta riippumatta. Luvut ovat "
        "**sisäkkäisiä**: aseistetut ovat panssaroitujen osajoukko, molemmat "
        "on luettu samalta tickiltä samasta pelaajajoukosta, ja jakaja on "
        "sama. Rivien ero on siis se havainto -- pistoolikierroksella "
        "aseistettuja on tyypillisesti 0 (800 $ ei riitä sekä kevlariin että "
        "parannettuun aseeseen), joten panssaririvi on se, joka kertoo "
        "kevlarien määrän."
    )
    holding = (
        "Molemmat luvut ovat **hallussapitoa eivätkä ostoja**: panssari ja ase "
        "säilyvät kierroksen yli hengissä selvinneellä, eikä vaurioitunutta "
        "panssaria eroteta ehjästä. Poikkeus on pistoolikierros -- puoliaika "
        "alkaa puhtaalta pöydältä, joten siellä luvut kertovat mitä ostettiin."
    )
    if flags.armed_shown and flags.armored_shown:
        return [nesting, holding]
    if flags.armored_shown:
        return [
            "Panssaroitu = panssari ostoajan lopussa, aseesta riippumatta; "
            "kypärää ei eroteta. Luku on **hallussapitoa eikä ostos**: "
            "panssari säilyy kierroksen yli hengissä selvinneellä. Poikkeus on "
            "pistoolikierros, jolla puoliaika alkaa puhtaalta pöydältä -- "
            "siellä luku kertoo montako kevlaria ostettiin."
        ]
    if flags.armed_shown:
        return [
            "Aseistettu = panssari JA parannettu ase ostoajan lopussa. Se ei "
            "ole sama asia kuin kevlarien määrä: pistoolikierroksella luku on "
            "tyypillisesti 0, vaikka kaikilla olisi panssari, koska 800 $ ei "
            "riitä sekä kevlariin että parannettuun aseeseen. Luku on "
            "**hallussapitoa eikä ostos**: säästetty tai poimittu ase "
            "lasketaan samoin kuin ostettu."
        ]
    return []


#: The round appendix: what can be said of it when it is not in the report.
#:
#: The round, the type and the justification **are not** in ``report.json``:
#: ``Report`` is marginal distributions and holds no per-round rows.
#: ``render`` does not compute the missing appendix itself -- that would be
#: computation, and the gap belongs to Story 2.3. Instead the report says
#: where the appendix really is: ``classify`` writes a round list with
#: justifications for every demo. When ``Report`` one day gets a round
#: appendix, this passage is replaced by a table.
_APPENDIX_NOTE = (
    "Kierros, tyyppi ja perustelu eivät ole report.jsonissa: se sisältää "
    "reunajakaumia, ei kierroskohtaisia rivejä. Liite on classify-vaiheen "
    "kierroslistassa, jossa jokaisella kierroksella on päätös ja sen lähtöarvot:"
)

#: The same explanation without the paths. A sentence ending in a colon in
#: front of an empty list would read as if the list had been lost on the way.
_APPENDIX_NOTE_WITHOUT_PATHS = (
    "Kierros, tyyppi ja perustelu eivät ole report.jsonissa: se sisältää "
    "reunajakaumia, ei kierroskohtaisia rivejä. Liite on classify-vaiheen "
    "kierroslistassa arkiston hakemistossa classified/<joukkue>/, mutta sen "
    "polkuja ei annettu tätä raporttia kirjoitettaessa."
)
