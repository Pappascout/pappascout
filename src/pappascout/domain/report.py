"""``Report`` -- the ``aggregate`` stage's result and ``render``'s only input.

This module is a **shared contract** in exactly the same sense as
:mod:`pappascout.domain.schemas` is for the tables: ``aggregate`` (Story 2.3)
writes the model as JSON into ``aggregates/<team_key>/report.json``, and
``render`` (Story 2.4) reads it. **``render`` computes nothing** -- every
number that appears in the report is ready here, and a new number in the
report means a change to **this model**, not to the Jinja report template.

The sample is in the structure, not in a comment
------------------------------------------------
Every claim carries its sample. Two numbers with exactly one reading:

``n``
    The rounds in which the observation was made.
``m``
    Every round of that side and round type from which the observation **was
    readable at all**.

An area's distribution :class:`AreaDistribution` also holds the value
``players = 0`` (the area was empty), so the sum of the ``n`` values over one
area is always ``m``. That is not decoration but a check: if the sum does not
match, some round was lost in the join. The model enforces it itself
(:meth:`AreaDistribution._check_sample`), so an invalid report cannot even be
built in memory. The same check is made **between the levels**: the sum of the
round types is the side's sample, the sum of the sides is the map's and the sum
of the maps is the whole report's -- a round lost there is what would show
first, and not one leaf would notice a thing.

One field is deliberately outside the rule: :class:`FirstContactArea` counts
presence and not the number of players, so the same round produces an
observation for every area in which the team had a player. The full
distribution from the same moment is in the ``positions`` list's
``first_contact`` sample point.

Two units, and the second one is not everywhere yet
---------------------------------------------------
Story 4.9 added a **third** count beside ``n`` and ``m``: the **matches** the
rounds come from. A ``best_of`` match plays several maps, so a demo is not a
match either -- measured 2026-09-24, the scouted team's report is 8 demos and
4 matches. Three rounds from three matches is a habit and three rounds from
one match is one thing that happened once, and ``n`` alone writes them
identically.

The counts do **not** obey the rule above. ``Σ n = m`` is an equality because
a round produces exactly one observation; a match produces one per round, so
``Σ matches`` exceeds the match denominator whenever a match contributed two
rounds. What holds is ``matches <= matches_m`` and ``matches <= n``, and
between the levels a pair of inequalities rather than a sum
(:func:`_check_matches_are_bounded`).

**Where the count is, and where it is not.** :class:`Sample` carries it at
every level, so every group heading and the summary state it.
:class:`PlayersCount`, :class:`AreaDistribution`, :class:`Position` and
:class:`FirstContactArea` carry it per observation -- the sample-point rows
and the first-contact row, which is where the measurement of 2026-09-23 found
the report saying the opposite of the truth. :class:`FirstDeathArea`,
:class:`KillArea`, :class:`ArmedPlayers`, :class:`ArmoredPlayers`,
:class:`UtilityCounts`, :class:`UtilityUse` and :class:`Anomaly` **do not**:
their rows still state rounds only, under the heading's match count. That is a
**bound of the story and not a property of those rows**, and two of them would
need a decision before they could follow: :class:`KillArea`'s denominator is
kills and not rounds, so "in how many matches" needs a second denominator of
its own, and an :class:`Anomaly`'s ``m`` is a different set of rounds from its
own list's.

In one place ``m`` **is not rounds**: :class:`KillArea` counts kills, so its
``Σ n = the number of kills``. A round type can have more kills than rounds, so
"n/m of the rounds" would be a plainly wrong sentence there -- and
:class:`DeathReport`'s documentation says so out loud, because the report
formats that very row with a different unit.

Three buckets, not two
----------------------
``is_league`` is created in the ``select`` stage and travels through
``classify`` into the table, so it is known for those demos that are in the
team's selection file. ``null`` is the state of **all the others**, and there
is not just one of them: a demo imported by hand (not selected from any match
list), ``select`` not run, a demo with no row in the selection file, a lineup
no team owns, owners that disagree about the kind of match, or a table that was
classified before ``select``. A two-bucket split (``league`` / ``other``) would
force a choice between two lies: marking them all league matches or marking
them all other matches. The sample is therefore ``{league, other, unknown}`` at
every level (:class:`Sample`), and the third bucket says what is known. **Why
the information is missing** is told by ``classify``'s run reason
(``StageResult.reason``) and not by the report: the report says what is known,
not what happened in the run.

Everything is computed, the report chooses
------------------------------------------
The model holds **every** round type, overtime and full buys included.
Treating saving rounds and default differently is a presentation choice, and it
belongs to the ``render`` stage: if aggregation filtered, changing the choice
would need a recomputation and ``report.json`` would stop being a full picture
of what is known about the demos.

Anomalies are at the root of the report and not under the round type
--------------------------------------------------------------------
``anomalies`` is a field of :class:`Report`, not of :class:`RoundTypeReport`.
An anomaly is the epic's most valuable output, and under the round type it
would be scattered over 24 blocks -- the very problem Story 2.5 solves. So
every :class:`Anomaly` carries the map, the side and the round types itself: it
can be read as one section without the reader hunting for its place in the
tree.

**The denominator is per rule, and that is intended.** ``ct_advance`` is a
phenomenon of the saving rounds, so the round type is part of the observation
and ``m`` is that round type's rounds on the map and side. ``crunch`` does not
know the round type at all, so grouping it by round type would break the same
pattern into an eco row and a default row with different denominators -- that
is, it would reproduce inside the section the very scatter the section was made
to remove. Crunch's ``m`` is therefore **all** of the side's rounds on the map,
and ``round_types`` says on which types it was observed.

The structure does not cross-check the denominator against the tree (anomalies
are not leaves of the tree), so the connection is aggregation's
responsibility. :class:`Anomaly` instead enforces its **internal** consistency:
``n`` is the length of the round list, ``players_max`` its largest observation
and ``round_types`` its set of types, so the summary cannot disagree with the
rows it was assembled from.

A row does not claim simultaneity across a round boundary
---------------------------------------------------------
:class:`AnomalyRound` is a node of its own because crunch's **source
directions are simultaneous only within the same round**. The union of two
rounds' directions ("from the directions A, B, C and D") would read as four
simultaneous directions, which is the opposite of the definition. The same
holds for the sample points and the player count. The round number is in the
same node, because the scout's next act is to watch that round on the demo --
and the section is useless if it says that something happened but not where to
see it.

What is **not** here: interpretations. The words "fake" and "rush" appear in no
field -- only observations and counts. Anomalous setups (``anomalies``) are
Story 2.5's addition to this same model.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from math import isfinite
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from pappascout.constants import (
    ANOMALY_RULES,
    ROSTER_BUCKETS,
    ROUND_TYPES,
    SAMPLE_BUCKETS,
    SITE_AREAS,
    AnomalyRule,
    AreaSource,
    RoundType,
    SampleKind,
    Side,
    SiteGroup,
)
from pappascout.errors import AggregateError

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "REPORT_SCHEMA_CHANGE",
    "slugify",
    "team_slug",
    "SLUG_FALLBACK",
    "SampleBucket",
    "Sample",
    "RosterSample",
    "PlayersCount",
    "AreaDistribution",
    "Position",
    "UtilityUse",
    "GrenadeCount",
    "UtilityCounts",
    "ArmedCount",
    "ArmedPlayers",
    "ArmoredCount",
    "ArmoredPlayers",
    "FirstContactArea",
    "FirstDeathArea",
    "KillArea",
    "DeathReport",
    "RoundRecord",
    "RoundTypeReport",
    "SideReport",
    "MapReport",
    "RosterEntry",
    "TeamReport",
    "MissingDemo",
    "AreaOrientation",
    "AnomalyPoint",
    "AnomalyRound",
    "Anomaly",
    "AnomalyScan",
    "MAP_NAME_SOURCES",
    "MapNameSource",
    "Report",
]

#: The report model's schema version. Raised when the structure changes so
#: that an old ``report.json`` no longer validates -- then ``render`` says
#: that aggregation has to be run again, instead of silently formatting half
#: a report.
#:
#: **The condition, in one place, because the history below is ninety lines
#: and Story 4.4 read this constant and still failed to raise it.** Raise the
#: version when **either** holds:
#:
#: 1. an old ``report.json`` would no longer validate against this model --
#:    a new required field, a removed or added enumeration value, a
#:    tightening that rejects a file already written; **or**
#: 2. an old file *would* validate, but a value in it would now be **read as
#:    something it is not** -- a field that kept its name and changed its
#:    meaning, or a default that cannot be told apart from an observation.
#:
#: The second is the one that gets missed, and it is the reason the condition
#: is not "was a new field added". Every entry below is an instance of one or
#: the other, and each says which.
#:
#: **Every rise also rewrites :data:`REPORT_SCHEMA_CHANGE`**, which names the
#: current version in the sentence a stage refuses an old file with. A test
#: holds the two together, so a rise that leaves the wording alone fails
#: rather than misinforming the reader.
#:
#: **5.0.0 (Story 2.9): the structure did not change, but the value set did.**
#: ``snapped`` was dropped from the ``AreaSource`` enumeration and
#: ``point_cloud`` took its place, so an old ``report.json`` no longer
#: validates -- no field disappeared, but ``UtilityUse.area_source`` rejects
#: the old value. The version therefore rises for exactly the same reason as
#: for a missing field: the condition is "does the old file validate", not
#: "was a new field added".
#:
#: **6.0.0 (Story 2.11): the same rule, the same reason.**
#: ``MapReport.map_name_source`` gained a new value ``demo_header``, so a
#: **new** ``report.json`` does not validate against the old model -- and an
#: old file's maps are in any case grouped by a different rule than this
#: version's, because the name is now read from the demo header. Two FACEIT
#: demos from the same map are two branches in an old file and one in a new
#: one; the same structure, different numbers. That must not be silently
#: formatted as this run's result.
#:
#: **7.0.0 (Story 2.5): a new field that has a default -- and the version
#: rises anyway.** ``Report.anomalies`` is ``default_factory=list``, so an old
#: ``report.json`` would validate with an empty list. That is exactly the
#: reason to raise it: an empty anomaly section is an **observation** in this
#: model ("no anomalies"), so a report rendered from an old file would claim
#: as a measured result that the rules did not exist. So the condition "does
#: the old file validate" is not enough on its own: a default value that has
#: to be told apart from an observation raises the version too.
#:
#: **8.0.0 (Story 2.14): a third anomaly rule, and coverage changes in
#: meaning.** ``Anomaly.rule`` gained the value ``stack``, so a new file does
#: not validate against the old model. The more important side is the
#: opposite one: an old file would validate, but its ``anomaly_scan`` would
#: name stack as **not implemented** and stay silent about how many rounds it
#: can hit. Rendered, it would claim as measured coverage a number that
#: covered two rules out of three.
#:
#: **8.0.0 stays in Story 2.15, although the model got stricter, and the
#: grounds are to be measured, not assumed.** The story added a duplicate
#: guard to four distributions (:class:`ArmedPlayers`,
#: :class:`ArmoredPlayers`, :class:`Position`,
#: :class:`RoundTypeReport.first_contact`). The condition for raising the
#: version is "does the old file validate", and the answer is yes:
#:
#: * ``aggregate`` builds each of these distributions from a
#:   ``collections.Counter`` or a similar dictionary whose key is exactly the
#:   value whose repetition the guard forbids -- a duplicate cannot arise, so
#:   no previously written ``report.json`` can hold one;
#:   the ``Σ n = m`` check would in addition have rejected most of them
#:   already.
#: * Checked by running it: both of the archive's ``report.json`` files
#:   validate against this model unchanged (``render`` read them in the run of
#:   2026-09-03).
#:
#: A tightening that would **reject** an old file would be a different thing:
#: it would raise the version, because ``render`` would fall over on a
#: pydantic error instead of saying that aggregation has to be run again.
#:
#: **9.0.0 (Story 3.9): the summary carries the roster breakdown.**
#: :attr:`Report.roster_sample` is a **required** field, so every
#: ``report.json`` written before this story fails to validate -- which is the
#: point. An old file has no roster dimension at all, and validating it as
#: current would leave the reader with a summary that silently omits the
#: ``5/5`` / ``4/5`` split rather than a stage that says "run aggregate
#: again". A default would have been worse than a hard failure: it would have
#: had to invent bucket counts, and the only honest invention -- everything
#: ``unknown`` -- is indistinguishable from a measured all-unknown archive.
#:
#: **10.0.0 (Story 4.4): the stack rule's definition changed, and with it the
#: meaning of a field that kept its name.** Two changes, and the second is the
#: reason the version had to move even though the first would have sufficed:
#:
#: * :attr:`AnomalyPoint.areas` is **required on every stack row**, so every
#:   ``report.json`` written before this story fails to validate -- measured:
#:   the archive's own files die at ``anomalies.3``. That is the mechanical
#:   half, and on its own it is the familiar "run aggregate again" case.
#: * :attr:`Anomaly.area` **changed meaning on stack** from the site's own
#:   area (``BombsiteA`` / ``BombsiteB``) to the area the crowd stood on. An
#:   old file would be read with the new meaning: the reader would take
#:   ``BombsiteB`` as "this is where they were standing", which is precisely
#:   what the old rule did not measure. A field that keeps its name and
#:   changes its meaning is the most dangerous change this constant exists
#:   for, because nothing else in the file shows it.
#:
#: **11.0.0 (Story 4.8): every round type carries its win-loss record.**
#: :attr:`RoundTypeReport.record` is **required**, so every ``report.json``
#: written before this story fails to validate -- which is the point, and the
#: same shape as 9.0.0's roster breakdown. A default would have been worse
#: here than at any earlier version: the only default a record can have is
#: ``0-0``, and ``0-0`` does not read as a missing measurement -- it reads
#: as a round type nobody won and nobody lost. The reader is preparing for a
#: match, and a habit that wins and a habit that loses are opposite pieces of
#: advice.
#:
#: **12.0.0 (Story 4.9): every count is also a count of matches.**
#: :attr:`Sample.matches`, :attr:`Position.matches_m`,
#: :attr:`AreaDistribution.matches_m`, :attr:`PlayersCount.matches` and
#: ``.newest``, and :class:`FirstContactArea`'s three are all **required**, so
#: **an old ``report.json`` no longer validates** -- the first of the two
#: conditions above.
#:
#: The second holds as well, and it is why no default was available: a
#: defaulted match count would be **read as something it is not**. The only
#: value a default could take is the demo count beside it, and measured
#: 2026-09-24 the two really differ -- the scouted team's report is 8 demos
#: and 4 matches -- so the default would state the wrong number while looking
#: exactly like a measured one, which is the whole failure this story was
#: written to end. ``newest`` has a third state (``null``, order not known)
#: for the same reason: the absence of the mark must not be read as its
#: negation.
REPORT_SCHEMA_VERSION = "12.0.0"

#: What the newest version changed, in one sentence, for the message a stage
#: refuses an old ``report.json`` with.
#:
#: A version mismatch on its own tells the reader that two numbers differ; it
#: does not tell them what the run they are about to repeat will add, and
#: ``aggregate --force`` over a real archive is not free. **Only the newest
#: change is named**, and not a changelog: the gate compares against exactly
#: one version, so any older file is refused by the same sentence, and a list
#: of every change since 5.0.0 would bury the one that matters. The history
#: is on :data:`REPORT_SCHEMA_VERSION`, where it is read deliberately.
#:
#: **It names the current version, and a test makes it.** The sentence is a
#: durable contract with nothing in the type system tying it to the constant
#: above: left alone across a bump it would go on describing the change
#: *before* last, and the reader would be sent to re-run an aggregate for
#: something they already have. That is Story 4.4's failure one level up --
#: the version number is guarded as a rule, so the sentence has to be too.
#: :func:`tests.test_report_model
#: .test_the_change_sentence_names_the_current_version` asserts that
#: :data:`REPORT_SCHEMA_VERSION` appears here, which is why the sentence
#: opens with the number.
#:
#: English, like every other line the CLI prints (AD-11). It never reaches
#: the report.
REPORT_SCHEMA_CHANGE = (
    "Version 12.0.0 states every count in matches as well as in rounds, "
    "which an older report does not carry at all."
)


#: Characters that a file name will not take. The slug is an ASCII subset,
#: because the archive is a synchronised folder two machines share.
_NON_WORD = re.compile(r"[^a-z0-9]+")

#: The slug used when nothing is left of the name. It is a **shared
#: constant**, so it identifies nothing -- two teams would get the same file
#: name. Use it only as a last resort, when not even the id yields a slug.
#:
#: **English, and that is not a breach of the Finnish report** (decided
#: 2026-09-10). The report's content stays Finnish permanently, but this
#: value never reaches the content: it reaches the **file name**
#: ``<timestamp>-<slug>.md``, and a file name is not content. Every other
#: slug is derived from an observed team name or from a FACEIT id, neither
#: of which is translated either; a Finnish word here was the one place the
#: file name spoke a language of its own.
SLUG_FALLBACK = "team"


def slugify(text: str) -> str:
    """A form a file name will take, or an **empty string**.

    The empty return value is deliberate and it is what separates this
    function from :func:`team_slug`: a Cyrillic or CJK name leaves no ASCII
    character at all, and then the caller has to be able to choose its **own**
    fallback. A shared constant would give every such team the same file name.
    """
    return _NON_WORD.sub("-", text.lower()).strip("-")


def team_slug(team_key: str) -> str:
    """A form a file name will take, made from the team id.

    ``render`` names the report ``<time>-<team_slug>.md``, so the slug must
    not hold path separators or non-ASCII letters.

    The fallback is :data:`SLUG_FALLBACK`, which **identifies nothing**. When
    the caller has another candidate (an id alongside the name, for example),
    use :func:`slugify` and choose the fallback yourself.
    """
    return slugify(team_key) or SLUG_FALLBACK


def _check_rounds_add_up(
    total: "Sample", parts: "list[Sample]", level: str, child: str
) -> None:
    """Check that the upper level's sample is the sum of the lower ones.

    ``Σ n = m`` is enforced in the leaves, but a round lost **between the
    levels** is what would show first: the join ``(map_demo_id, round_no)``
    can drop a row, which leaves the sum of the round types smaller than the
    side's sample without a single leaf noticing anything. The comparison is
    made both on the total and on **each bucket separately**, because a round
    could otherwise change bucket without the total changing.

    Demos are not summed here: the same demo produces rounds for both sides
    and for several round types, so the sum of the lower levels' demo counts
    is larger than the upper level's. The report/map level is the one place
    where the sum does hold, and :func:`_check_demos_add_up` is that check.

    Raises:
        AggregateError: If the sum does not match. The message names the level
            and the bucket.
    """
    # The buckets are read from the same list as everywhere else: two copies
    # would drift apart, and then a new bucket would go unchecked.
    for bucket in (None, *SAMPLE_BUCKETS):
        got = (
            total.rounds
            if bucket is None
            else getattr(total, bucket).rounds
        )
        parts_sum = sum(
            (p.rounds if bucket is None else getattr(p, bucket).rounds)
            for p in parts
        )
        if got != parts_sum:
            where = "in total" if bucket is None else f"in bucket {bucket}"
            raise AggregateError(
                f"The sample does not match at level {level}: the sum of the "
                f"{child} levels' rounds is {parts_sum} {where}, but {level} "
                f"claims a sample of {got}.\n"
                "The difference means a round was lost between the levels -- "
                "usually in the join (map_demo_id, round_no)."
            )


def _check_demos_add_up(total: "Sample", parts: "list[Sample]") -> None:
    """The report's demos are the sum of the maps', **bucket by bucket**.

    The counterpart of :func:`_check_rounds_add_up` for the one dimension it
    leaves alone. Demos sum only here: every demo is on exactly one map, and
    ``is_league`` describes the match, so it is also in exactly one bucket --
    at the side and round-type levels neither holds and the sum is
    meaningless.

    **The per-bucket comparison is the point, and it was missing.** The total
    was checked from Story 2.3 on; Epic 2's retrospective action (11) asked
    for the buckets to be reconciled the way the rounds already were, and the
    gap is the same one the rounds' docstring names: a demo can move from
    ``league`` to ``other`` between the levels without the total changing at
    all. Measured at ``16d8f69``: a report claiming two league demos over one
    league map and one other map was accepted, and the summary would then have
    said "two league demos" over a section showing one.

    ``rounds`` is not compared here. :func:`_check_rounds_add_up` already does
    it at the same call site and in the same buckets, and a second wording of
    one fault would only make the reader look for two.

    Raises:
        AggregateError: If the sum does not match. The message names the
            bucket.
    """
    # The same bucket list as everywhere else, for the same reason: a copy
    # would drift, and a new bucket would go unchecked.
    for bucket in (None, *SAMPLE_BUCKETS):
        got = total.demos if bucket is None else getattr(total, bucket).demos
        parts_sum = sum(
            (p.demos if bucket is None else getattr(p, bucket).demos)
            for p in parts
        )
        if got != parts_sum:
            where = "in total" if bucket is None else f"in bucket {bucket}"
            raise AggregateError(
                f"The report's sample claims {got} demos {where}, but the "
                f"sum of the maps is {parts_sum}. Every demo is on exactly "
                "one map and in exactly one bucket, so the sum has to match."
            )


def _check_matches_are_bounded(
    total: "Sample", parts: "list[Sample]", level: str, child: str
) -> None:
    """The level's matches, against its children's, as **two inequalities**.

    The third count's counterpart to :func:`_check_rounds_add_up` and
    :func:`_check_demos_add_up`, and it is inequalities rather than a sum
    because of what a match is. Rounds sum at every level; demos sum at the
    report/map level only; **matches sum nowhere**. A ``best_of`` match plays
    several maps and every match plays both sides, so the same match is
    counted in several children -- measured 2026-09-24, the scouted team's
    report holds 4 matches over maps of 4, 3 and 1, and the sum 8 is the
    answer to nothing.

    What holds at every level is the pair of bounds a union obeys:

    * the level cannot hold **fewer** matches than its busiest child, because
      that child's matches are all its own, and
    * it cannot hold **more** than its children added up, because a match
      reaches the level only through a child.

    **What this does not catch, measured rather than reasoned.** It is
    tempting to read the upper bound as the guard against a match count taken
    from the **demos**, and it is not: :func:`_check_demos_add_up` is an
    *equality*, so the children's demos always sum to the level's, and
    substituting demos for matches uniformly lands the upper bound on
    ``8 <= 8`` exactly. Measured 2026-09-24 on the real archive's own numbers
    (report 8 demos / 4 matches over maps of 4, 3 and 1): the truth passes and
    the substitution passes. There is no archive on which it would not --
    the slack between 4 and 8 that makes the bound look strong is the very
    slack the substitution fills.

    **And on the pipeline's path it cannot fire at all**, which is the same
    qualification ``RoundTypeReport``'s own row check makes about itself.
    Every level's matches are derived by
    :func:`~pappascout.domain.aggregate.sample_for` from a subset of one
    frame, so the union relations hold structurally. What is left is worth
    keeping and is all it is: a hand-edited ``report.json``, and a future
    second pass that filters differently.

    **Where the guard against the wrong count actually is**: nowhere
    structural. It is :attr:`Sample.matches` existing as a field at all --
    measured, written, and re-derivable -- and ``tests/data/match_counts.json``,
    which pins what the real archive produces so a wrong number is a wrong
    number in a file.

    A level with no children is not checked: a map without sides is a state
    :class:`MapReport` allows, and an empty ``max`` is not a claim about
    anything.

    Args:
        total: The level's own sample.
        parts: Its children's samples.
        level: The level's name, for the message.
        child: The child level's name, for the message.

    Raises:
        AggregateError: If either bound is broken. The message names both
            levels, because the fault is between them and not in either.
    """
    if not parts:
        return
    largest = max(p.matches for p in parts)
    together = sum(p.matches for p in parts)
    if total.matches < largest:
        raise AggregateError(
            f"The sample at level {level} claims {total.matches} matches, "
            f"but one {child} alone holds {largest}. Every {child}'s matches "
            f"are also the {level}'s, so the {level} cannot hold fewer."
        )
    if total.matches > together:
        raise AggregateError(
            f"The sample at level {level} claims {total.matches} matches, "
            f"but the {child} levels hold {together} between them. A match "
            f"reaches the {level} only through a {child}, so the {level} "
            "cannot hold more. (The sum is an upper bound and not the value: "
            "the same match is in several children whenever it played "
            "several maps or both sides.)"
        )


def _check_seconds(seconds: list[float], where: str) -> None:
    """Sample points: finite, non-negative and distinct from one another.

    Three conditions in one place, so that every list of sample points is
    checked against the same conditions. NaN is worse here than a wrong
    figure: it would pass every comparison as false, and the report would
    format it as ``nan s kohdalla`` -- that is, as a figure the reader cannot
    interpret.

    **Two lists, not one.** The promise above was written for
    :class:`AnomalyRound`'s ``points`` and covered only them until Epic 2's
    retrospective action (10); the round type's ``positions`` is the report's
    other list of sample points and was unchecked, so ``nan``, ``inf``, a
    negative second and the same second twice all passed. The caller passes
    the seconds, not the objects, precisely because the two lists carry them
    in differently named fields.

    Args:
        seconds: The moments, in whatever order. Ordering is **not** checked
            here: ``AnomalyRound`` requires ascending order and checks it
            itself, and the round type's sample points do not promise one
            (``aggregate`` sorts first contact last, after the time points,
            because it is an event and not a clock reading).
        where: Names the offending node in the message.
    """
    for value in seconds:
        if not isfinite(value):
            raise ValueError(
                f"{where}: sample point {value!r} is not a finite number. "
                "A sample point is a number of seconds from the round's "
                "anchor."
            )
        if value < 0:
            raise ValueError(
                f"{where}: sample point {value:g} s is negative. "
                "Sample points are measured forward from the end of "
                "freezetime, so a negative value would point into the buy "
                "time."
            )
    if len(set(seconds)) != len(seconds):
        raise ValueError(
            f"{where}: sample points repeat ({seconds}); the same moment is "
            "one observation."
        )


class _Node(BaseModel):
    """The report model's base class: an unknown field is an error, not a skip.

    ``frozen`` because the model is a contract and not a workspace: ``render``
    must not touch up the numbers as it reads them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class SampleBucket(_Node):
    """One sample bucket's demo and round counts."""

    demos: int = Field(ge=0)
    rounds: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_rounds_have_a_demo(self) -> SampleBucket:
        """Rounds cannot exist without a demo to have played them.

        ``aggregate`` counts both from the same rows, so it cannot produce
        such a bucket; a ``report.json`` that carries one has been edited by
        hand or written by something else. The guard belongs here rather than
        in ``render``, which must not decide what a number means -- and every
        reader of the field, not just the summary row, is then covered.

        The converse is allowed: a demo whose rounds all fell out of a level
        (``round_type`` missing, a pruned branch) is a real state.

        Raises:
            ~pappascout.errors.AggregateError: If ``rounds`` is positive while
                ``demos`` is zero.
        """
        if self.demos == 0 and self.rounds > 0:
            raise AggregateError(
                f"A sample bucket claims {self.rounds} rounds without a "
                "single demo. A round is always some demo's round, so a "
                "bucket that holds rounds but no demo cannot be measured.\n"
                "Aggregation does not produce a figure like this: report.json "
                "has been edited by hand. Run aggregation again."
            )
        return self


def _check_bucket_totals(
    node: Sample | RosterSample, names: Sequence[str], label: str
) -> None:
    """Assert a breakdown's totals are the sum of its buckets.

    Shared by both breakdowns of the same sample (:class:`Sample` and
    :class:`RosterSample`) so that the two cannot come to disagree on what
    "the total is the sum of the buckets" means, and so that one fault does
    not produce two differently worded errors.

    Args:
        node: The breakdown to check.
        names: Its bucket field names.
        label: Which breakdown this is, for the message. The two share one
            exception type on purpose (one fault, one type), so the name is
            the only thing that tells the reader which of the summary's two
            breakdowns failed.

    Raises:
        ~pappascout.errors.AggregateError: If either total differs from the
            sum of the named buckets.
    """
    buckets = [getattr(node, name) for name in names]
    demos = sum(b.demos for b in buckets)
    rounds = sum(b.rounds for b in buckets)
    if node.demos != demos or node.rounds != rounds:
        raise AggregateError(
            f"The sample totals do not match the buckets ({label}): "
            f"demos={node.demos} (buckets {demos}), "
            f"rounds={node.rounds} (buckets {rounds}). "
            "Every demo belongs to exactly one bucket, so the total has to be "
            "the sum of the buckets."
        )


class Sample(_Node):
    """The sample at one level in three buckets.

    ``unknown`` is the bucket for demos whose ``is_league`` is empty. It is
    not an error state but **the bucket of every demo whose kind is not
    known**, and there are several reasons for that: a demo imported by hand,
    ``select`` not run, a demo with no row in the selection file, a lineup
    with no owner in the team index, owners that disagree, or a table
    classified before ``select``. Naming one reason would make the bucket
    narrower than it is. The kind of the match comes from ``select``'s
    selection file; the demo itself does not hold that information, and
    ``aggregate`` does not guess.

    ``demos`` and ``rounds`` are the bucket sums, precomputed so that
    ``render`` does not add them up.

    **This ``unknown`` counts demos, and :class:`RoundRecord`'s counts
    rounds.** The two words sit side by side in a round type's node of
    ``report.json`` and mean different things: here, a demo whose kind is not
    known; there, a round whose outcome was not read. Neither implies the
    other.

    ``matches`` is the third count and it answers the reader's own question
    (Story 4.9). A ``best_of`` match plays several maps, so **a demo is not a
    match**: measured 2026-09-24 on the real archive, the scouted team's
    report is 8 demos and 4 matches, and until this field existed the summary
    said "8 demoa" to a reader counting matches. Three rounds from three
    matches is a habit and three rounds from one match is one thing that
    happened once, and the round count alone writes them identically.
    """

    demos: int = Field(ge=0)
    rounds: int = Field(ge=0)
    #: The matches these demos come from (:func:`.selection.match_of`).
    #:
    #: **It is not bucketed and it is not summed**, and both are properties of
    #: what a match is rather than omissions.
    #:
    #: Not bucketed: ``league``, ``other`` and ``unknown`` are buckets of
    #: **demos**, and :func:`_check_bucket_totals` holds the totals to their
    #: sum. A match's maps all carry the same ``is_league``, so a per-bucket
    #: match count would be a fourth copy of the same split with no reading of
    #: its own -- and the bucket sum would have to be an inequality, which is
    #: not what that function checks.
    #:
    #: Not summed: :func:`_check_demos_add_up` works because every demo is on
    #: exactly one map. A match's two maps are two branches, so the report's
    #: matches are **fewer** than the maps' matches added up (measured: 4
    #: against 4 + 3 + 1). What does hold between the levels is an inequality,
    #: and :meth:`Report._check_matches_are_not_invented` is where it is
    #: checked.
    matches: int = Field(ge=0)
    league: SampleBucket
    other: SampleBucket
    unknown: SampleBucket

    @model_validator(mode="after")
    def _check_totals(self) -> Sample:
        _check_bucket_totals(self, SAMPLE_BUCKETS, "league breakdown")
        return self

    @model_validator(mode="after")
    def _check_matches_have_demos(self) -> Sample:
        """A match is counted through its demos, so it cannot outnumber them.

        The counterpart of :meth:`SampleBucket._check_rounds_have_a_demo` for
        the third count, and it carries that method's qualification too: on
        the pipeline's path :func:`~pappascout.domain.aggregate.sample_for`
        derives both from the same rows, so this cannot fire there. It catches
        a hand-edited ``report.json`` and a future second pass -- and, unlike
        the round check, it is **not** a bare length comparison: ``demos`` and
        ``matches`` really are different numbers on the real archive (8 and
        4), so a wrong one of them is a wrong number and not merely a wrongly
        totalled one.

        Raises:
            ~pappascout.errors.AggregateError: If the matches outnumber the
                demos, or if demos were counted without a match.
        """
        if self.matches > self.demos:
            raise AggregateError(
                f"The sample claims {self.matches} matches from "
                f"{self.demos} demos. Every match is counted through the "
                "demos it left in the archive, so there cannot be more "
                "matches than demos.\n"
                "Aggregation does not produce a figure like this: report.json "
                "has been edited by hand. Run aggregation again."
            )
        if self.demos and not self.matches:
            raise AggregateError(
                f"The sample claims {self.demos} demos from no match at all. "
                "Every demo is some match's demo, so a sample that holds "
                "demos holds at least one match.\n"
                "Aggregation does not produce a figure like this: report.json "
                "has been edited by hand. Run aggregation again."
            )
        return self


class RosterSample(_Node):
    """The same sample bucketed by ``roster_class`` instead of ``is_league``.

    A sibling of :class:`Sample`, not a replacement: the two describe the
    *same* demos and rounds along two different dimensions, and
    :meth:`Report._check_breakdowns_agree` holds them to equal totals.

    ``full`` is the class where every player on the map was a regular
    (``5/5``) and ``partial`` the one where the roster threshold was met with
    one outsider (``4/5``); the identifiers are in
    :data:`~pappascout.constants.ROSTER_CLASS_BUCKET`, because a class name is
    not a valid field name.

    ``unknown`` is the bucket for every demo whose ``roster_class`` is empty,
    and it is not an error state. It holds **two facts it cannot separate**,
    for the reason given in
    :data:`~pappascout.constants.ROSTER_BUCKETS`: a class that was never
    measured, and a class that was measured and did not meet the threshold
    (AD-6 stores one only when it does). So the row reports the absence of a
    *confirmed* class, which is why the printed sentence says "ei ole
    vahvistettu".

    It carries the whole sample for as long as ``select`` has not been run
    over the archive -- which is the state this story was written in. Two
    buckets would have forced such a demo into ``5/5`` or ``4/5``, and either
    would be a claim nobody made.

    ``demos`` and ``rounds`` are the bucket sums, precomputed so that
    ``render`` does not add them up (AD-10; the rule is AD-10's, and an
    earlier version of this line cited AD-8, which is the ports and the
    import command).
    """

    demos: int = Field(ge=0)
    rounds: int = Field(ge=0)
    full: SampleBucket
    partial: SampleBucket
    unknown: SampleBucket

    @model_validator(mode="after")
    def _check_totals(self) -> RosterSample:
        _check_bucket_totals(self, ROSTER_BUCKETS, "roster breakdown")
        return self


class RoundRecord(_Node):
    """One group's win-loss record: how its rounds ended.

    A sibling of :class:`Sample` and not a part of it: the sample says **how
    many rounds were observed**, the record says **how they ended**. Both are
    counted from the same rows, and
    :meth:`RoundTypeReport._check_record_covers_the_rounds` holds them to the
    same total -- to the same *total* and no further, which that method's own
    docstring sets out. What holds the three counts to the archive is
    ``tests/data/round_records.json``.

    ``unknown`` is a bucket of its own and **never a loss**. ``won`` in the
    ``CLASSIFIED`` schema is ``pl.Boolean`` and therefore nullable, so a round
    whose outcome was not read is a real state of the table. Folding it into
    ``losses`` would turn a gap in the recording into a claim about the team,
    and the record is exactly the figure a reader acts on. Measured
    2026-09-23: 0 nulls in the real archive's 511 classified rounds, so the
    bucket is unobserved today -- which is why it is written down rather than
    assumed away.

    **It is not the same ``unknown`` as :class:`Sample`'s**, although the two
    sit side by side in the same node of ``report.json``. ``Sample.unknown``
    is a bucket of **demos** whose ``is_league`` is empty; this is a count of
    **rounds** whose outcome was not read. Nothing links them: a group can
    have every demo's league status known and still hold an unread round, and
    the other way round.

    **There is no cross-field invariant left inside this class**, which is
    why it carries no validator of its own. ``Field(ge=0)`` states everything
    the three counts owe each other; the one real constraint is that they add
    up to the group's sample, and only
    :meth:`RoundTypeReport._check_record_covers_the_rounds` can see both
    sides of it.

    **No rate and no verdict.** The model holds three counts and nothing
    derived from them. Two wins in three rounds is not a finding; the report
    states the count and the reader is the analyst.
    """

    #: Rounds this team won.
    wins: int = Field(ge=0)
    #: Rounds this team lost. A round whose outcome is unknown is **not**
    #: here -- see the class docstring.
    losses: int = Field(ge=0)
    #: Rounds whose ``won`` was empty in the classified table.
    unknown: int = Field(ge=0)

    @property
    def rounds(self) -> int:
        """The three buckets together: every round the record accounts for.

        **A property is not serialised into ``report.json``**, and that is
        the fact that decides which totals are fields and which are not. Any
        number the report *prints* has to be a field, or ``render`` would
        have to add it up and AD-10 forbids ``render`` computing anything.
        :class:`Sample`'s ``rounds`` is printed, so it is a field; this total
        is never printed -- it exists only for the cross-check against the
        sample -- so it is derived, and a stored copy would be a second
        number free to disagree with the buckets beside it.
        """
        return self.wins + self.losses + self.unknown


class PlayersCount(_Node):
    """One bar in an area's player-count distribution.

    ``players`` is the number of **living** players in the area at the sample
    point -- a dead player produces no row for the area. ``n`` is the number
    of rounds in which the area held exactly that many.

    ``matches`` and ``newest`` are the same observation counted in the
    reader's own unit (Story 4.9). ``n = 3`` is the same string whether those
    three rounds are three matches or one, and the two are opposite pieces of
    advice before a match.
    """

    players: int = Field(ge=0)
    n: int = Field(gt=0)
    #: The **matches** those ``n`` rounds come from.
    #:
    #: ``Σ matches`` over a distribution is **not** ``matches_m``, and that is
    #: the one thing to know about this field. ``Σ n = m`` holds because a
    #: round produces exactly one bar per area; a match produces as many bars
    #: as it has rounds, so the same match is counted in several bars of the
    #: same area (four rounds of one match, four different player counts).
    #: The only relation upward is ``matches <= matches_m``, and
    #: :meth:`AreaDistribution._check_matches` is where that is checked --
    #: here there is nothing to compare it with.
    matches: int = Field(gt=0)
    #: Whether the **map's newest match** is among the matches this
    #: observation was made in, or ``null`` when the matches' order is not
    #: known.
    #:
    #: The reader's question before a match is "is this still true?", and a
    #: count cannot answer it: *Outside on 3 of 4 rounds* reads as "mostly
    #: Outside" whether those three rounds are the three oldest matches or the
    #: three newest. Measured 2026-09-23 on the real archive, ``de_nuke`` T
    #: pistol at 15 s, they are the three oldest and the newest match does
    #: something else entirely.
    #:
    #: **The map's, and neither the sample point's nor the round type's**, and
    #: both narrower scopes were tried and measured out (2026-09-24). The
    #: sample point's: 15 of the real archive's 377 points cover fewer matches
    #: than their round type (a round decided at 30 seconds has no 45-second
    #: sample), 11 of them print a mark, and it would have meant *the newest
    #: match that has a 45-second sample*. The round type's: **11 of the
    #: scouted team's 26 round-type groups hold no round from that team's
    #: newest match at all**, and each marked an older match as the newest --
    #: the sharpest a one-round eco block whose only match is three weeks and
    #: three matches behind. The map is the level the reader asks at, because
    #: the report is read map by map -- and the team's newest match has no
    #: ``de_dust2`` demo at all, so the narrower scopes had that whole chapter
    #: marked against a match never played on it.
    #:
    #: The consequence is deliberate and is the value: a block with nothing
    #: from the map's newest match reads as not including it on **every** row,
    #: which says the block is stale. ``matches_m`` stays the sample point's,
    #: so a row can read ``2/2 ottelussa, ei uusimmassa`` -- in both matches
    #: this moment exists in, and the map's newest is not one of them.
    #:
    #: ``null`` is a third state and not a false: the order of the matches
    #: comes from the match index, and a demo imported by hand has no entry
    #: there (:func:`~pappascout.domain.aggregate.newest_match`). Then nothing
    #: is known about recency, which is a different thing from knowing that
    #: the newest match is absent -- and ``render`` writes no mark at all.
    newest: bool | None


class AreaDistribution(_Node):
    """One area's player-count distribution at one sample point.

    This is the structure the target analysis's line *"3A and 2B"* is read
    from: area ``BombsiteA``, ``players = 3``, ``n`` rounds out of ``m``.

    The distribution also holds the value ``players = 0``, so the sum of the
    ``n`` values is always ``m``. Bars whose ``n`` is zero are not written --
    they would claim as an observation that there is no observation.

    ``matches_m`` is ``m`` in the reader's own unit: the matches those ``m``
    rounds come from. It is the denominator every bar's ``matches`` is read
    against, and it is **not** the sum of them -- see
    :attr:`PlayersCount.matches`.
    """

    #: The game's own ``env_cs_place`` area. ``null`` = the player's area was
    #: not obtained; the row does not vanish, because an unknown position is a
    #: different thing from an empty area.
    area: str | None
    m: int = Field(ge=0)
    #: The matches the ``m`` rounds come from. Every area of the same sample
    #: point shares it, exactly as ``m`` is shared
    #: (:meth:`Position._check_areas_share_the_sample`).
    matches_m: int = Field(ge=0)
    players_dist: list[PlayersCount]

    @model_validator(mode="after")
    def _check_sample(self) -> AreaDistribution:
        """``Σ n = m``. Without this the figure means nothing.

        Raises:
            AggregateError: If the sum does not match. The exception is
                deliberately :class:`~pappascout.errors.AggregateError` and
                not a plain ``ValueError``: this is not a formatting mistake
                but a round lost in the join.
        """
        total = sum(p.n for p in self.players_dist)
        if total != self.m:
            raise AggregateError(
                f"The sample does not match in area {self.area!r}: the sum of "
                f"the player counts' n values is {total}, but there are "
                f"{self.m} rounds.\n"
                "Every round has to produce exactly one observation for the "
                "area -- including when the area was empty (players = 0). "
                "The difference means a round was lost in the join "
                "(map_demo_id, round_no) or that the distribution has no "
                "zero bucket."
            )
        seen = [p.players for p in self.players_dist]
        if len(seen) != len(set(seen)):
            raise ValueError(
                f"Area {self.area!r} has the same player count twice in its "
                "distribution; the distribution must be one bar per player "
                "count."
            )
        return self

    @model_validator(mode="after")
    def _check_matches(self) -> AreaDistribution:
        """No bar spans more matches than the sample point has, or than it
        has rounds.

        **Two conditions and not one sum**, and the difference from
        :meth:`_check_sample` above is the whole reason this is a method of
        its own. ``Σ n = m`` is an equality because a round produces exactly
        one bar per area; a match produces one bar per round, so ``Σ matches``
        exceeds ``matches_m`` whenever any match contributed two rounds -- on
        the real archive, most blocks. An equality here would refuse the
        ordinary case.

        What is left is the pair of bounds the report's sentence rests on: an
        observation cannot have been made in more matches than the figure is
        measured over, and it cannot have been made in more matches than
        rounds, because a match contributes at least one round to each of its
        matches in the bar.

        Raises:
            ~pappascout.errors.AggregateError: If a bar claims more matches
                than the sample point covers, or more matches than its own
                rounds.
        """
        if self.matches_m > self.m:
            raise AggregateError(
                f"Area {self.area!r} claims {self.matches_m} matches over "
                f"{self.m} rounds. Every match contributes at least one round "
                "to the sample point, so there cannot be more matches than "
                "rounds."
            )
        for bar in self.players_dist:
            if bar.matches > self.matches_m:
                raise AggregateError(
                    f"In area {self.area!r} the observation "
                    f"'{bar.players} players' claims {bar.matches} matches, "
                    f"but the sample point covers {self.matches_m}. An "
                    "observation cannot be made in more matches than the "
                    "figure is measured over."
                )
            if bar.matches > bar.n:
                raise AggregateError(
                    f"In area {self.area!r} the observation "
                    f"'{bar.players} players' was made on {bar.n} rounds but "
                    f"claims {bar.matches} matches. Every match in the "
                    "observation contributes at least one of those rounds."
                )
        return self


class Position(_Node):
    """One sample point: every area's distribution from the same moment.

    There are two kinds of sample point, and ``sample_kind`` tells them apart:

    ``time``
        The ``[parse].snapshot_seconds`` figure as it stands (6, 15, 30, 45 s),
        the same on every round and therefore comparable. ``seconds`` is that
        figure.
    ``first_contact``
        The round's first cross-side hit. The moment is different on every
        round, so ``seconds`` is ``null`` and ``seconds_median`` gives the
        measured timing.

    ``m`` is the number of rounds on which **this sample point exists**, not
    the number of all the round type's rounds. They differ: the 45-second
    sample is missing from a round that was decided in 30 seconds.
    ``rounds_missing`` gives the difference, so that a round does not vanish
    quietly.

    ``matches_m`` is the same sample in matches, and it inherits that
    difference: a sample point missing from some match's rounds covers fewer
    matches than the round type does. The recency mark on the bars is **not**
    measured against this set, nor against the round type's, but against the
    **map's**; :attr:`PlayersCount.newest` says why.
    """

    sample_kind: SampleKind
    seconds: float | None
    seconds_median: float | None = None
    m: int = Field(ge=0)
    #: The matches the ``m`` rounds come from. Shared by every area, like
    #: ``m`` itself.
    matches_m: int = Field(ge=0)
    rounds_missing: int = Field(ge=0)
    areas: list[AreaDistribution]

    @model_validator(mode="after")
    def _check_areas_share_the_sample(self) -> Position:
        """The same sample on every area, and each area once.

        The latter is the same guard as :class:`AreaDistribution`'s (one bar
        per player count) but a step higher: there it stops the same **player
        count** twice on one area, here the same **area** twice on one sample
        point. Without it the row would set
        ``Middle 3 (2/2 kierroksesta), Middle 1 (2/2 kierroksesta)``, that is,
        the same area as two observations whose sum exceeds the sample.

        **Both denominators are checked, for one reason twice.** The rounds
        and the matches are printed side by side on the same claim, so an
        area whose match denominator differed from its neighbour's would set
        two figures from the same moment that are not comparable -- which is
        this method's whole sentence, in the unit Story 4.9 added.
        """
        for area in self.areas:
            if area.m != self.m:
                raise AggregateError(
                    f"At sample point {self.seconds!r} area {area.area!r} "
                    f"claims a sample of {area.m}, but the sample point has "
                    f"{self.m} rounds. Every area of the same sample point "
                    "has to share the same sample -- otherwise two figures "
                    "from the same moment are not comparable."
                )
            if area.matches_m != self.matches_m:
                raise AggregateError(
                    f"At sample point {self.seconds!r} area {area.area!r} "
                    f"claims a sample of {area.matches_m} matches, but the "
                    f"sample point has {self.matches_m}. Every area of the "
                    "same sample point has to share the same sample -- "
                    "otherwise two figures from the same moment are not "
                    "comparable."
                )
        seen = [area.area for area in self.areas]
        if len(seen) != len(set(seen)):
            raise ValueError(
                f"Sample point {self.seconds!r} has the same area twice in "
                "its distribution; a sample point has one distribution per "
                "area."
            )
        return self

    @model_validator(mode="after")
    def _check_matches_fit_the_sample(self) -> Position:
        """A sample point holds no more matches than it holds rounds.

        **Here and not only in :class:`AreaDistribution`**, which makes the
        same comparison, because a sample point with **no areas** is
        reachable: ``area_distributions`` builds the area list from the union
        of the rounds' living players, so a moment at which every player of
        every round had already fallen yields ``m > 0`` and ``areas == []``.
        The round is in the sample by design -- dropping it would break
        ``Sigma n = m`` -- and with no area to carry the check, this
        denominator would be the one number in the node that nothing looked
        at. The group-level bound still applied to it
        (:meth:`RoundTypeReport._check_no_row_claims_more_matches_than_the_group`),
        so what was missing is exactly this floor.

        Raises:
            ~pappascout.errors.AggregateError: If the sample point claims more
                matches than rounds.
        """
        if self.matches_m > self.m:
            raise AggregateError(
                f"Sample point {self.seconds!r} claims {self.matches_m} "
                f"matches over {self.m} rounds. Every match contributes at "
                "least one round to the sample point, so there cannot be "
                "more matches than rounds."
            )
        return self

    @model_validator(mode="after")
    def _check_seconds_matches_kind(self) -> Position:
        if self.sample_kind == "time" and self.seconds is None:
            raise ValueError(
                "A time sample point must have seconds; without it two "
                "sample points cannot be told apart."
            )
        if self.sample_kind == "first_contact" and self.seconds is not None:
            raise ValueError(
                "A first-contact sample point has no nominal second: the "
                "moment is different on every round. Use seconds_median."
            )
        return self


class UtilityUse(_Node):
    """One utility pattern: type, throw area, detonation area and time window.

    The target analysis's line *"a CT smoke onto the B site from T spawn"* is
    read from here: ``grenade_type = "smoke"``, ``throw_area = "TSpawn"``,
    ``detonate_area = "BombsiteB"``.

    ``n`` is the number of rounds, ``throws`` the number of throws. They differ
    when two grenades of the same kind are thrown to the same place on the same
    round -- and that is exactly why the ``n`` values must not be added up into
    a count of grenades. The number of grenades on a round is
    :class:`UtilityCounts`.
    """

    grenade_type: str
    #: The thrower's own area at the moment of the throw. An **observation**,
    #: not an estimate.
    throw_area: str | None
    #: The area of the detonation. An **estimate**: a grenade has no area
    #: name, so it is read from the nearest cell of the demo's point cloud --
    #: from the spot on the map where players have really stood closest to the
    #: detonation.
    detonate_area: str | None
    #: Where ``detonate_area`` came from. ``null`` always and only when
    #: ``detonate_area`` is ``null``. Without this the report would present an
    #: estimate as an observation.
    area_source: AreaSource | None
    #: The name of the time window, for example ``"0-5"`` or ``"20+"``. The
    #: bounds are ``[thresholds].utility_seconds_buckets``.
    seconds_bucket: str
    n: int = Field(gt=0)
    throws: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> UtilityUse:
        if self.n > self.m:
            raise AggregateError(
                f"The utility pattern {self.grenade_type} {self.throw_area!r} "
                f"-> {self.detonate_area!r} appears on {self.n} rounds, "
                f"although there are {self.m} rounds."
            )
        if self.throws < self.n:
            raise AggregateError(
                f"The utility pattern {self.grenade_type} has {self.throws} "
                f"throws but {self.n} rounds; there cannot be fewer throws "
                "than rounds."
            )
        if (self.area_source is None) != (self.detonate_area is None):
            raise ValueError(
                f"detonate_area={self.detonate_area!r} and "
                f"area_source={self.area_source!r} contradict each other: "
                "either both are given or both are empty. An area without a "
                "source would present an estimate as an observation, and a "
                "source without an area would claim a derivation for an area "
                "that does not exist."
            )
        return self


class GrenadeCount(_Node):
    """One bar in the "how many were thrown on a round" distribution."""

    thrown: int = Field(ge=0)
    n: int = Field(gt=0)


class UtilityCounts(_Node):
    """One grenade type's count distribution, round by round.

    The target analysis's lines *"2 smokes 2 flashes"* and *"3 molotovs, 2 HE,
    1 smoke"* are read from here: the question is not where the grenade went
    off but how many of them were thrown. :class:`UtilityUse` does not answer
    that, because its ``n`` counts rounds and not grenades.

    The distribution holds the value ``thrown = 0`` for the same reason
    :class:`AreaDistribution` holds the value ``players = 0``: without it
    ``Σ n = m`` would not hold, and "they threw no smokes at all" would not be
    an observation but a missing row.
    """

    grenade_type: str
    m: int = Field(ge=0)
    counts: list[GrenadeCount]

    @model_validator(mode="after")
    def _check_sample(self) -> UtilityCounts:
        total = sum(c.n for c in self.counts)
        if total != self.m:
            raise AggregateError(
                f"The sample does not match for grenade type "
                f"{self.grenade_type!r}: the sum of the n values is {total}, "
                f"but there are {self.m} rounds. Every round has to produce "
                "an observation -- including when the throws were zero."
            )
        seen = [c.thrown for c in self.counts]
        if len(seen) != len(set(seen)):
            raise ValueError(
                f"Grenade type {self.grenade_type!r} has the same count twice "
                "in its distribution."
            )
        return self


class ArmedCount(_Node):
    """One bar in the "how many players were armed" distribution."""

    armed: int = Field(ge=0)
    n: int = Field(gt=0)


class ArmedPlayers(_Node):
    """The number of armed players at the end of the buy time, round by round.

    The observation is Story 1.6's counter ``players_armed_buy_end``: the
    player had armour **and** at least one weapon in hand. It is possession
    and not a purchase, so a saved rifle counts the same as a bought one.

    The target analysis's lines *"5 kevlars"* and *"no kevs"* are **NOT** read
    from here: they are in :class:`ArmoredPlayers`. On a pistol round this
    distribution is in practice ``0``, because the $800 of starting money does
    not buy both kevlar (650) and an upgraded weapon -- an earlier version of
    this docstring claimed the opposite, and that misreading cost one wrong
    row in Story 2.3's acceptance run.

    ``m`` is the number of rounds from which the observation **was obtained**;
    ``rounds_unknown`` is the rest. They have to be kept apart: zero armed is
    a different thing from an unreadable inventory, and the latter would look
    like a saving round.
    """

    m: int = Field(ge=0)
    rounds_unknown: int = Field(ge=0)
    counts: list[ArmedCount]

    @model_validator(mode="after")
    def _check_sample(self) -> ArmedPlayers:
        """``Σ n = m``, and every bar appears once.

        The latter is the same guard as
        :meth:`AreaDistribution._check_sample`'s, :class:`UtilityCounts`'s and
        :class:`DeathReport`'s. It was missing from here **from Story 2.8 to
        Story 2.15**, although the row is read the same way: the report would
        set the same bar twice with different figures, and the reader would
        see one observation as two.
        """
        total = sum(c.n for c in self.counts)
        if total != self.m:
            raise AggregateError(
                "The sample does not match in the armed players' "
                f"distribution: the sum of the n values is {total}, but there "
                f"are {self.m} observations."
            )
        seen = [c.armed for c in self.counts]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "The armed players' distribution has the same player count "
                "twice; the distribution must be one bar per player count."
            )
        return self


class ArmoredCount(_Node):
    """One bar in the "how many players carried armour" distribution.

    The field is ``armored`` and not ``armed`` on purpose: ``report.json`` is
    also read by hand, and two distributions with nearly the same name would
    be confused with each other if they used the same field name.
    """

    armored: int = Field(ge=0)
    n: int = Field(gt=0)


class ArmoredPlayers(_Node):
    """The number of players carrying armour at the end of the buy time,
    round by round.

    The target analysis's lines *"5 kevlars"* (Nuke, T pistol) and *"no kevs"*
    (Ancient, CT) are read from **here**. The observation is
    ``players_armored_buy_end``: the player had armour (``m_ArmorValue > 0``)
    at the end of the buy time. A helmet is not told apart, nor damaged armour
    from intact armour.

    **A different figure from** :class:`ArmedPlayers`, not a generalisation of
    it. They answer different questions and both are needed:

    * armed = armour **and** an upgraded weapon -- the half-buy's calibrated
      condition A, which ``classify`` reads
    * armoured = armour, full stop -- "how many had armour"

    They are **nested and not parallel**: the armed condition includes the
    armour, so the armed are a subset of the armoured. Both are read from the
    same tick and from the same set of players, so the denominators are the
    same too.

    **Possession, not a purchase.** Armour carries over the round for anyone
    who survived, so on other round types the figure says what the players had
    and not what they bought. **The pistol round (1 and 13) is the
    exception**: the half starts from a clean slate and nothing is inherited,
    so there the figure is an observation of buying -- and that is exactly why
    *"5 kevlars"* is the right reading.

    On a pistol round the counters also differ the most: measured from four
    MatureMayhem demos on 2026-08-30, on all eight pistol rounds the armed
    were 0 and the armoured 1--5. That is not a rule but a consequence of
    money, and a picked-up weapon is enough to arm: in the same material, on
    the opponent's Anubis round 13 the counters are 3 and 1.

    ``m`` is the number of rounds from which the observation **was obtained**;
    ``rounds_unknown`` is the rest. The same distinction as in
    :class:`ArmedPlayers`: zero armoured is an observation, unreadable armour
    is no observation at all.
    """

    m: int = Field(ge=0)
    rounds_unknown: int = Field(ge=0)
    counts: list[ArmoredCount]

    @model_validator(mode="after")
    def _check_sample(self) -> ArmoredPlayers:
        """``Σ n = m``, and every bar appears once.

        The same guard as :class:`ArmedPlayers`'s -- and it was missing from
        here for the same reason: this class was copied from it in Story 2.8,
        so the gap was duplicated instead of being noticed.
        """
        total = sum(c.n for c in self.counts)
        if total != self.m:
            raise AggregateError(
                "The sample does not match in the armoured players' "
                f"distribution: the sum of the n values is {total}, but there "
                f"are {self.m} observations."
            )
        seen = [c.armored for c in self.counts]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "The armoured players' distribution has the same player count "
                "twice; the distribution must be one bar per player count."
            )
        return self


class FirstContactArea(_Node):
    """An area in which the team had a player at the moment of first contact.

    The line *"took contact in the Apartments corridor"* is read from here.
    The observation is **presence**, not a player count: ``n`` is the rounds
    on which the area held at least one living player at the moment the
    round's first cross-side hit happened.

    ``Σ n = m`` **does not hold here** and is not meant to: the same round
    produces an observation for every area in which the team had a player. The
    full distribution from the same moment is in the ``positions`` list's
    ``first_contact`` sample point.

    The match counts follow the rounds into that shape (Story 4.9): ``Σ
    matches`` is not ``matches_m`` either, and here it is not even the
    :attr:`PlayersCount.matches` reason -- it is this class's own. A round
    with players in three areas is counted in three areas' match counts,
    whether or not its match had another round.
    """

    area: str | None
    n: int = Field(gt=0)
    m: int = Field(ge=0)
    #: The matches those ``n`` rounds come from.
    matches: int = Field(gt=0)
    #: The matches the ``m`` rounds come from: the presence list's
    #: denominator, in the same unit.
    matches_m: int = Field(ge=0)
    #: Whether the map's newest match is among them, or ``null`` when the
    #: matches' order is not known. The same field as
    #: :attr:`PlayersCount.newest` and measured the same way, for the reason
    #: given there.
    newest: bool | None

    @model_validator(mode="after")
    def _check_counts(self) -> FirstContactArea:
        if self.n > self.m:
            raise AggregateError(
                f"The first-contact area {self.area!r} appears on {self.n} "
                f"rounds, although there are {self.m} rounds."
            )
        if self.matches_m > self.m:
            raise AggregateError(
                f"The first-contact area {self.area!r} claims a sample of "
                f"{self.matches_m} matches over {self.m} rounds. Every match "
                "contributes at least one round to the sample."
            )
        if self.matches > self.matches_m:
            raise AggregateError(
                f"The first-contact area {self.area!r} appears in "
                f"{self.matches} matches, although the presence list covers "
                f"{self.matches_m}."
            )
        if self.matches > self.n:
            raise AggregateError(
                f"The first-contact area {self.area!r} appears on {self.n} "
                f"rounds but in {self.matches} matches. Every match in the "
                "observation contributes at least one of those rounds."
            )
        return self


class FirstDeathArea(_Node):
    """The area where the team lost its **first** player on a round.

    The target analysis's line *"Cave dies so they play from the site / from
    the newbie spot and from long"* is read from here: the round's first own
    death is the one that explains what the team did afterwards.

    ``Σ n = m`` **does hold here**, unlike in :class:`FirstContactArea`: every
    round has exactly one first death, so it produces an observation for
    exactly one area. ``m`` is the number of rounds on which the team **lost a
    player**; rounds on which nobody died are in
    :attr:`DeathReport.rounds_missing` and not a zero row -- a zero row would
    claim as an observation that there is no observation.
    """

    #: The victim's own ``last_place_name`` at the moment of death. An
    #: **observation**, not an estimate. ``null`` = the game's area name was
    #: not obtained; the row does not vanish.
    area: str | None
    n: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> FirstDeathArea:
        if self.n > self.m:
            raise AggregateError(
                f"The first-death area {self.area!r} appears on {self.n} "
                f"rounds, although there are {self.m} rounds."
            )
        return self


class KillArea(_Node):
    """The area from which a player of the team made a kill.

    The target analysis's line *"the enemy came through the secret yard"* is
    read from here: the area is the **shooter's own** ``last_place_name`` at
    the moment of the kill, not the victim's.

    ``m`` **is not rounds but kills**, and ``Σ n = m`` over it. The difference
    from :class:`AreaDistribution` matters: there every round produces one
    observation for every area, here every **kill** produces one observation
    for one area. A round type can have more kills than rounds, so the figure
    must not be read as "n rounds out of m".
    """

    area: str | None
    n: int = Field(gt=0)
    m: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_counts(self) -> KillArea:
        if self.n > self.m:
            raise AggregateError(
                f"The kill area {self.area!r} appears in {self.n} kills, "
                f"although there are {self.m} kills."
            )
        return self


class DeathReport(_Node):
    """The team's own deaths and kills on one round type.

    Two marginal distributions, and they have **different denominators**. The
    confusion would be easy and expensive, so it is written down here:

    ``first_death_areas``
        Where the team lost its first player. One observation per round, so
        ``Σ n = m`` and ``m`` is **rounds**.
    ``kills``
        Where the team's players made kills from. One observation per
        **kill**, so ``Σ n = kills_total`` and the figure can exceed the
        number of rounds.

    ``rounds_missing`` is the rounds on which the team **lost no player at
    all**. It is a figure of its own and not a zero row: the area "did not
    die" is not an area, and without a separate figure ``Σ n = m`` would fail.

    **Own kills include the team kill.** If a player of the team kills a
    team-mate, the row is both an own death and an own kill. Filtering either
    of them out would be interpretation: the observation is that a player died
    and that the shooter was in a particular area. A team kill is rare (1 out
    of 591 deaths, measured 2026-08-30), but if it ever shows in a figure in
    the report, it shows because it happened.

    **Suicide is not a kill.** If the shooter and the victim are the same
    player, the row is an own death but not an own kill. That is not an
    interpretation but the same observation read correctly: "kills by area"
    says **where the team shoots from**, and the area of a suicide is a place
    nobody shot from. Measured 0/591 in the material, so the fault would have
    been latent -- and that is why it is written down as a rule rather than
    left to not happen.
    """

    #: Rounds on which the team lost at least one player.
    m: int = Field(ge=0)
    #: Rounds on which the team lost no player at all.
    rounds_missing: int = Field(ge=0)
    #: The median timing of the first own death in seconds, or ``null`` if no
    #: timing was obtained from any round.
    first_death_seconds_median: float | None = None
    first_death_areas: list[FirstDeathArea] = Field(default_factory=list)
    #: The team's own kills in total. This is the ``kills`` list's denominator.
    kills_total: int = Field(default=0, ge=0)
    kills: list[KillArea] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_first_death_sample(self) -> DeathReport:
        """``Σ n = m`` and every area shares the same sample.

        Without the latter two areas could claim different denominators, and
        two figures from the same distribution in the report would not be
        comparable.
        """
        for entry in self.first_death_areas:
            if entry.m != self.m:
                raise AggregateError(
                    f"The first-death area {entry.area!r} claims a sample of "
                    f"{entry.m}, but the rounds on which the team lost a "
                    f"player are {self.m}."
                )
        total = sum(entry.n for entry in self.first_death_areas)
        if total != self.m:
            raise AggregateError(
                "The sample does not match in the first-death areas: the sum "
                f"of the n values is {total}, but the rounds on which the "
                f"team lost a player are {self.m}.\n"
                "Every such round has exactly one first death, so the sum has "
                "to be the same figure."
            )
        seen = [entry.area for entry in self.first_death_areas]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "The first-death distribution has the same area twice."
            )
        return self

    @model_validator(mode="after")
    def _check_kill_sample(self) -> DeathReport:
        """``Σ n = kills_total``, and the denominator is kills, not rounds."""
        for entry in self.kills:
            if entry.m != self.kills_total:
                raise AggregateError(
                    f"The kill area {entry.area!r} claims a sample of "
                    f"{entry.m}, but there are {self.kills_total} kills."
                )
        total = sum(entry.n for entry in self.kills)
        if total != self.kills_total:
            raise AggregateError(
                "The sample does not match in the kill areas: the sum of the "
                f"n values is {total}, but there are {self.kills_total} "
                "kills.\n"
                "Every kill belongs to exactly one area -- including when the "
                "area is unknown."
            )
        seen = [entry.area for entry in self.kills]
        if len(seen) != len(set(seen)):
            raise ValueError("The kill distribution has the same area twice.")
        return self

    @model_validator(mode="after")
    def _check_median_has_a_sample(self) -> DeathReport:
        """A median without a single death would be a figure out of nothing."""
        if self.first_death_seconds_median is not None and self.m == 0:
            raise AggregateError(
                "The median of the first death is "
                f"{self.first_death_seconds_median}, but nobody died on a "
                "single round. A median without observations would be a "
                "figure out of nothing."
            )
        return self


class RoundTypeReport(_Node):
    """Every observation of one round type on one map and side.

    ``small_sample`` is a mark and not a filter: a sample of fewer than
    ``[thresholds].small_sample_rounds`` rounds is still shown, but marked.
    One repetition is not a pattern, and the report has to say so.

    **No field here takes a default, and that is a property of the node and
    not of any one field.** All of them are bare annotations, so a
    ``report.json`` missing any of them is refused instead of read. The
    reason is the same every time: an empty default is indistinguishable
    from a measured absence, so a branch computed with an older version
    would render as a round type on which nobody had armour, nobody died or
    nobody won -- which is exactly the difference
    :data:`REPORT_SCHEMA_VERSION` exists to tell apart. A field below carries
    a comment only where something is peculiar to it.
    """

    round_type: RoundType
    sample: Sample
    small_sample: bool
    positions: list[Position]
    utility: list[UtilityUse]
    utility_counts: list[UtilityCounts]
    players_armed: ArmedPlayers
    #: The armoured as an observation of their own alongside the armed, and
    #: not a second reading of the same count -- see :class:`ArmoredPlayers`.
    players_armored: ArmoredPlayers
    first_contact: list[FirstContactArea]
    #: Own deaths and kills.
    deaths: DeathReport
    #: How the round type's rounds ended. **The node's no-default rule bites
    #: hardest here**: an empty distribution at least looks empty, whereas
    #: the only default a record could carry is ``0-0``, which does not read
    #: as absent data at all -- it reads as a round type nobody won and
    #: nobody lost. ``render`` therefore never prints that string for a
    #: measured group either (:func:`~pappascout.render.view.record_text`).
    record: RoundRecord

    @model_validator(mode="after")
    def _check_sample_points(self) -> RoundTypeReport:
        """The sample points: finite, non-negative and each moment once.

        The same three conditions :class:`AnomalyRound` holds its own sample
        points to, from the same function -- the round type's ``positions``
        is the report's other list of sample points, and until Epic 2's
        retrospective action (10) it was the one nobody checked. Measured at
        ``16d8f69``: ``seconds=nan``, ``seconds=inf``, ``seconds=-5.0`` and
        two ``time`` points at 6.0 s were all accepted, and the report would
        have written ``nan s kohdalla`` and the same moment as two
        observations.

        Only the ``time`` points are compared. A ``first_contact`` point has
        no nominal second by contract (``_check_seconds_matches_kind``), so
        there is nothing to compare -- its moment is ``seconds_median``, one
        per round. That leaves one case outside this guard: **two**
        first-contact points in the same round type, which ``aggregate``
        cannot produce (it groups them into one) and which nothing here
        forbids.

        Raises:
            ValueError: If a sample point is not a finite non-negative
                number, or if the same moment appears twice.
        """
        _check_seconds(
            [p.seconds for p in self.positions if p.seconds is not None],
            f"round type {self.round_type}",
        )
        return self

    @model_validator(mode="after")
    def _check_deaths_cover_the_rounds(self) -> RoundTypeReport:
        """The deaths' rounds are exactly the round type's rounds.

        ``Σ n = m`` is enforced inside :class:`DeathReport`, but it holds even
        when ``m`` has been computed from the **wrong set of rounds**: the
        distribution would be internally consistent and quietly wrong. Every
        other level checks its round sum upwards
        (:func:`_check_rounds_add_up`), and this is the deaths' counterpart to
        that.

        Raises:
            AggregateError: If the rounds with deaths and the rounds without
                them are not together the round type's sample.
        """
        covered = self.deaths.m + self.deaths.rounds_missing
        if covered != self.sample.rounds:
            raise AggregateError(
                f"The deaths of round type {self.round_type} cover "
                f"{covered} rounds ({self.deaths.m} on which the team lost a "
                f"player, {self.deaths.rounds_missing} on which it did not), "
                f"but the round type's sample is {self.sample.rounds} "
                "rounds.\n"
                "The difference means the deaths were computed from a "
                "different set of rounds than the other observations -- the "
                "distribution would still look internally correct."
            )
        return self

    @model_validator(mode="after")
    def _check_record_covers_the_rounds(self) -> RoundTypeReport:
        """The record's three buckets are exactly the round type's rounds.

        The counterpart of :meth:`_check_deaths_cover_the_rounds`, and it is
        here rather than inside :class:`RoundRecord` for the reason that
        method gives: the record is internally consistent whatever rows it
        was counted from, so only the level that holds **both** the record
        and the sample can see that they were counted from the same ones.

        The report states this count without a rate beside it, so a reader
        has nothing to notice a drift against: a record covering 29 rounds
        printed on a 22-round block looks exactly as authoritative as the
        right one. That is why the disagreement is refused here rather than
        rendered.

        **What this guard weighs, and what it cannot** -- the same
        qualification :meth:`SampleBucket._check_rounds_have_a_demo` makes
        about itself, and it matters more here. ``sample_for`` and
        ``record_for`` are handed the **same sequence** and each counts one
        round per row, so both totals are ``len(rows)``: on the pipeline's
        own path this is an **identity and can never fire**. It is a **length
        check and not a content check** -- it catches a hand-edited
        ``report.json`` and a future second pass over a differently filtered
        frame, but a record built from the wrong rows of the right length
        passes it, and so does a record whose wins and losses are the wrong
        way round. Read beside
        :meth:`_check_deaths_cover_the_rounds`, which guards a live join and
        really can fire on the pipeline's path, the two look alike and are
        not. What catches the content is the archive table in
        ``tests/data/round_records.json``.

        Raises:
            ~pappascout.errors.AggregateError: If the wins, losses and
                unknown outcomes are not together the round type's sample.
        """
        if self.record.rounds != self.sample.rounds:
            raise AggregateError(
                f"The record of round type {self.round_type} covers "
                f"{self.record.rounds} rounds ({self.record.wins} won, "
                f"{self.record.losses} lost, {self.record.unknown} with an "
                f"unknown outcome), but the round type's sample is "
                f"{self.sample.rounds} rounds.\n"
                "The difference means the record was counted from a "
                "different set of rounds than the other observations -- the "
                "record would still add up on its own."
            )
        return self

    @model_validator(mode="after")
    def _check_no_row_claims_more_matches_than_the_group(self) -> RoundTypeReport:
        """No row spans more matches than the round type itself holds.

        The match counts are built at three levels and the group's own is the
        outermost: a sample point's matches are a subset of the round type's,
        because its rounds are. A row claiming more would put a denominator on
        the line that the heading above it contradicts -- ``3/5 ottelusta``
        under ``4 ottelusta`` -- and the reader has no way to tell which of
        the two is the measurement.

        **What it weighs, and what it cannot.** Like
        :meth:`_check_record_covers_the_rounds`, it cannot fire on the
        pipeline's own path: ``positions_for`` and ``first_contact_areas``
        derive their matches from a subset of the very round keys
        ``sample_for`` counted, so the inclusion is structural. Unlike that
        one it is **not a length check** -- the two sides are the sizes of two
        independently built sets, so a row assembled from the wrong rounds
        fails it as soon as those rounds bring in a match the group does not
        have. What it will not catch is a count taken from the **demos**
        instead of the matches, because demos are never fewer: on this
        archive the two are equal at and below the map level, and the check
        that sees that mistake is :func:`_check_matches_are_bounded` at the
        report level, where a match's two maps make them differ.

        **It checks the denominators only, and that is enough because of a
        chain that is worth writing down.** A bar's own count is bounded by
        its area's (:meth:`AreaDistribution._check_matches`), an area's by its
        sample point's (:meth:`Position._check_areas_share_the_sample`, which
        holds them equal), and the sample point's by the group's, here. So
        ``bar.matches <= area.matches_m == position.matches_m <=
        sample.matches`` without any link being stated twice.

        **A row type added later has to supply its own first link.**
        :class:`FirstContactArea` does it inside itself, because its list has
        no per-moment node to share a denominator with. Whoever adds the next
        one chooses between those two shapes; what they must not do is rely on
        this method, which never sees a numerator.

        Raises:
            ~pappascout.errors.AggregateError: If a sample point or a
                first-contact row claims more matches than the round type's
                sample.
        """
        for position in self.positions:
            if position.matches_m > self.sample.matches:
                raise AggregateError(
                    f"At sample point {position.seconds!r} of round type "
                    f"{self.round_type} the sample is {position.matches_m} "
                    f"matches, but the round type holds "
                    f"{self.sample.matches}. A sample point's matches are "
                    "the round type's own, so it cannot cover more."
                )
        for entry in self.first_contact:
            if entry.matches_m > self.sample.matches:
                raise AggregateError(
                    f"The first-contact presence list of round type "
                    f"{self.round_type} covers {entry.matches_m} matches, "
                    f"but the round type holds {self.sample.matches}."
                )
        return self

    @model_validator(mode="after")
    def _check_first_contact_areas_are_unique(self) -> RoundTypeReport:
        """The same area once in the first-contact presence list.

        ``Σ n = m`` **does not hold here** and is not meant to (the same round
        produces an observation for every area in which the team had a
        player), so a duplicate does not show up in the sum the way it does in
        the other distributions -- it is just two rows about the same area with
        different figures. The guard is therefore here and not in
        :class:`FirstContactArea`: an area is unambiguous only within its list.

        Raises:
            ValueError: If the same area appears twice. The report would set
                ``Middle (2/2 kierroksesta), Middle (1/2 kierroksesta)``, and
                the reader would see one observation as two.
        """
        seen = [entry.area for entry in self.first_contact]
        if len(seen) != len(set(seen)):
            raise ValueError(
                f"The first-contact presence list of round type "
                f"{self.round_type} holds the same area twice."
            )
        return self


class SideReport(_Node):
    """One side's round types on one map."""

    side: Side
    sample: Sample
    round_types: list[RoundTypeReport]

    @model_validator(mode="after")
    def _check_rounds(self) -> SideReport:
        _check_rounds_add_up(
            self.sample, [rt.sample for rt in self.round_types], "side", "round type"
        )
        _check_matches_are_bounded(
            self.sample,
            [rt.sample for rt in self.round_types],
            "side",
            "round type",
        )
        return self


class MapReport(_Node):
    """Both sides of one map.

    ``map_name`` is first of all an **observation**: ``parse`` reads the map's
    name from the demo header into the ``MATCH`` table (Story 2.11), and it is
    not validated against the map pool -- a map outside the pool is a genuine
    observation. When the observation is missing, the name is derived from the
    ``map_demo_id`` against the map pool, and even an unknown map does not
    vanish: then the name is the ``map_demo_id`` itself and the source is
    ``unknown``.

    ``map_name_source`` says where the name came from, and its values in order
    of precedence are: ``demo_header`` -> ``map_demo_id`` -> ``unknown``.
    """

    map_name: str
    map_name_source: MapNameSource
    #: The map's demos. Two demos from the same map add up into one branch,
    #: and this list says which ones.
    map_demo_ids: list[str]
    sample: Sample
    sides: list[SideReport]

    @model_validator(mode="after")
    def _check_rounds(self) -> MapReport:
        _check_rounds_add_up(
            self.sample, [s.sample for s in self.sides], "map", "side"
        )
        _check_matches_are_bounded(
            self.sample, [s.sample for s in self.sides], "map", "side"
        )
        if self.sample.demos != len(self.map_demo_ids):
            raise AggregateError(
                f"Map {self.map_name} claims a sample of "
                f"{self.sample.demos} demos, but lists "
                f"{len(self.map_demo_ids)}: "
                f"{', '.join(self.map_demo_ids)}.\n"
                "The map's demos have to be listed exactly, because they are "
                "what the rounds added up from."
            )
        return self


class RosterEntry(_Node):
    """One roster row: a player with their id and their name.

    **Both, always.** The SteamID64 stays alongside the name, because the name
    is there for readability but the id is the only traceable value: a name
    can change from one match to the next, an id cannot.

    ``display_name`` is ``None`` if no name could be read from the demo. That
    is not the same thing as an empty string, and it is not replaced with the
    id here -- the replacement is a presentation choice and belongs to the
    ``render`` stage.
    """

    player_id: str
    display_name: str | None = None

    @field_validator("display_name")
    @classmethod
    def _empty_is_not_a_name(cls, value: str | None) -> str | None:
        """An empty string is not a name -- it is ``None``.

        Without this a roster row would show an empty name beside the SteamID,
        which reads as though the name were empty and not as though there were
        none. ``TeamReport`` guards the same thing for the team's name; a
        player's name cannot be looser.
        """
        if value is None:
            return None
        return value.strip() or None


class TeamReport(_Node):
    """The team from whose point of view the report was made.

    ``key`` is the name of the directory under ``classified/``. Before Epic 3
    it is a lineup id (``lineup_key``); ``lineup_keys`` says which lineups
    were joined into the same team and on what grounds
    (``[thresholds].team_identity_min_common`` players in common).

    **The name is an observation, not a derivation.** ``display_name`` is the
    team's clan name from the demo (``LINEUPS.clan_name``) when and only when
    ``display_name_source`` is ``clan_name``. Without an observation the name
    is the id and the source is ``team_key``, and the report says so out loud
    instead of presenting a hash as a name.
    """

    key: str
    slug: str
    display_name: str
    #: Where ``display_name`` comes from. ``clan_name`` = observed from the
    #: demo; ``team_key`` = there is no observation, so the name is the id
    #: itself.
    display_name_source: Literal["clan_name", "team_key"] = "team_key"
    #: The other clan names observed in the joined demos. The conflict does
    #: not vanish: the most often observed one is chosen to be shown, and the
    #: rest are listed here, so that the reader sees the team appeared under
    #: two names.
    display_name_alternatives: list[str] = Field(default_factory=list)
    lineup_keys: list[str]
    roster: list[RosterEntry]
    #: Where ``roster`` comes from. ``lineups`` = observed from the demos;
    #: ``index`` = from the team index (``index/teams.json``, which the
    #: ``discover`` stage writes). No stage produces the ``index`` value yet.
    roster_source: Literal["lineups", "index"]

    @model_validator(mode="after")
    def _check_name_source(self) -> TeamReport:
        """The source, the name, the alternatives and the slug must agree.

        Without this ``display_name_source = "clan_name"`` together with the
        id would claim a hash as an observed name -- and the report's heading
        is built on exactly that difference.

        **The slug is part of the same claim.** It ends up in the file name,
        which the reader sees before opening the report; a slug that disagrees
        with the name would name the file after a team the report is not
        about. It cannot be guarded separately, because the claim is precisely
        the pair (name, slug).

        **Alternative names are observations.** An empty string is not a name,
        the same name twice is not two observations, and the shown name is not
        an alternative to itself. ``aggregate`` prevents these already as it
        computes, but ``render`` and a ``report.json`` read from disk lean on
        this contract and not on that computation.
        """
        self._check_alternatives()
        self._check_slug()
        if self.display_name_source == "team_key":
            if self.display_name != self.key:
                raise AggregateError(
                    f"The team's name is recorded as {self.display_name!r}, "
                    f"but the source as {self.display_name_source!r}, which "
                    f"means there is no name -- then the name has to be the "
                    f"id {self.key!r} itself."
                )
            if self.display_name_alternatives:
                raise AggregateError(
                    "The team has alternative names recorded "
                    f"({', '.join(self.display_name_alternatives)}), but the "
                    "source is 'team_key', which means no name at all was "
                    "observed. The alternatives are observations, so there "
                    "cannot be any without an observed name."
                )
        elif not self.display_name.strip():
            raise AggregateError(
                "The team's name is recorded as an empty string, although the "
                "source is recorded as 'clan_name'. An empty string is not a "
                "name -- then the source is 'team_key'."
            )
        return self

    def _check_alternatives(self) -> None:
        """Alternative names are observations, not decoration."""
        blank = [name for name in self.display_name_alternatives if not name.strip()]
        if blank:
            raise AggregateError(
                f"Team {self.key} has {len(blank)} empty strings among its "
                "alternative names. An empty string is not a name, and an "
                "observation cannot be presented as one."
            )
        seen = sorted(
            {
                name
                for name in self.display_name_alternatives
                if self.display_name_alternatives.count(name) > 1
            }
        )
        if seen:
            raise AggregateError(
                f"Team {self.key} has repeated alternative names: "
                f"{', '.join(seen)}. The same name twice is not two "
                "observations, and the list would claim more conflicts than "
                "were observed."
            )
        if self.display_name in self.display_name_alternatives:
            raise AggregateError(
                f"The team's shown name {self.display_name!r} is also among "
                "its own alternatives. The alternatives are the names that "
                "were NOT chosen -- otherwise the report would list the "
                "chosen name as a conflict with itself."
            )

    def _check_slug(self) -> None:
        """The slug is derived from the shown name, with the id as fallback.

        The same rule as in ``aggregate``, written into this contract: a
        ``report.json`` read from disk has not been through that computation.
        """
        expected = slugify(self.display_name) or slugify(self.key) or SLUG_FALLBACK
        if self.slug != expected:
            raise AggregateError(
                f"The team's slug is {self.slug!r}, but derived from the name "
                f"{self.display_name!r} it would be {expected!r}.\n"
                "The slug ends up in the report's file name, so a slug that "
                "disagrees would name the file after a team the report is not "
                "about."
            )


class MissingDemo(_Node):
    """A match that would belong to the sample but whose data is not there.

    A missing demo does not vanish quietly: it is in this list with its
    reason, and the report says so. A single missing demo does not bring the
    run down.
    """

    match: str
    reason: str


#: Where the map's name came from, in order of precedence.
#:
#: The list is here because two nodes read it now (:class:`MapReport` and
#: :class:`Anomaly`) and not one. Written out twice, a new source would be
#: accepted in one and rejected in the other.
MAP_NAME_SOURCES: tuple[str, ...] = ("demo_header", "map_demo_id", "unknown")
MapNameSource = Literal["demo_header", "map_demo_id", "unknown"]


class AreaOrientation(_Node):
    """An area's side orientation **in one demo**.

    The anomaly's piece of evidence: the area is T territory in that demo if
    at least ``advance_t_share`` of its alive observations at the time sample
    points come from the T side. The figure is the demo's own observation --
    not a map database, not a human-given division of areas, not a table that
    accumulates across the archive. An accumulating source would give the same
    demo a different result depending on what other demos happen to be in the
    archive.

    **A row per demo and not one figure.** Two demos from the same map are one
    branch (Story 2.11), and their T shares can differ. One figure would force
    a choice between the mean (which was not observed) and an extreme (which
    would say something about one demo only), so an anomaly carries the
    orientation of each of its demos separately.

    Attributes:
        map_demo_id: The demo this is an observation of.
        t_share: The T observations' share of all the area's observations.
        observations: All of the area's alive observations, that is, the
            orientation's own sample. Without it a share of 1.00 would look
            the same from one observation and from a hundred.
    """

    map_demo_id: str
    t_share: float = Field(ge=0.0, le=1.0)
    observations: int = Field(gt=0)


class AnomalyPoint(_Node):
    """One sample point's observation on one round.

    **A figure belongs to the moment it was measured at.** From Story 2.5 to
    2.14 a round carried one maximum and a list of sample points, and the
    report's row set the maximum against every one of them. That is a claim
    about data that does not exist: measured, MatureMayhem Inferno round 2 has
    five players at 15 s and **one** at 30 s, but the row read "5 pelaajaa 15
    ja 30 s kohdalla"; Anubis round 4 is 5/5 at 15 s and **4/5** at 30 s. The
    summary cannot be per sample point, so the structure is.

    Attributes:
        sample_t_s: The sample point's nominal time in seconds.
        players: The player count at **this** sample point -- not the round's
            largest. On stack it is the crowd standing on this point's
            ``areas`` and not everybody in the site's group: the two differ
            whenever the group holds players outside the areas the rule
            named.
        alive: The subject's living CT players at this sample point. **Only on
            stack**, and there at every point: four out of five and four out
            of four are a different observation, and the player count alone
            does not tell them apart. On the two other rules the figure would
            be invented -- neither of them counts the living.
        areas: The areas the crowd was standing on at this sample point, the
            largest first. **Only on stack**, and there never empty: the rule
            is a concentration ("four players on at most two areas"), so
            without the areas the row would report a crowd without saying
            where it stood -- and until Story 4.4 the row named the site's own
            area even when nobody was on it. The list is per sample point for
            the same reason as ``players``: where the defence stands at 15 s
            is an observation of that moment and of no other.
    """

    sample_t_s: float
    players: int = Field(gt=0)
    alive: int | None = Field(default=None, gt=0)
    areas: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_point(self) -> AnomalyPoint:
        """The crowd is a subset of those alive.

        Raises:
            ValueError: If there are more players than living ones, if an area
                is named twice or is nameless, or if there are more areas than
                players. Each is a broken observation and not a stricter one:
                every area on the list holds at least one of the crowd.
        """
        if self.alive is not None and self.alive < self.players:
            raise ValueError(
                f"At sample point {self.sample_t_s:g} s there are "
                f"{self.players} players but {self.alive} alive. The crowd "
                "is a subset of those alive."
            )
        if len(set(self.areas)) != len(self.areas):
            raise ValueError(
                f"At sample point {self.sample_t_s:g} s the same area is "
                f"named twice ({self.areas}). One area is one place."
            )
        if any(not name.strip() for name in self.areas):
            raise ValueError(
                f"At sample point {self.sample_t_s:g} s there is a nameless "
                f"area among the crowd's areas ({self.areas}). A nameless "
                "area is not a place the defence can stand on."
            )
        if len(self.areas) > self.players:
            raise ValueError(
                f"At sample point {self.sample_t_s:g} s there are "
                f"{len(self.areas)} areas but {self.players} players. Every "
                "area on the list holds at least one of them."
            )
        return self


class AnomalyRound(_Node):
    """One round on which an anomaly was observed.

    **This node is what makes the row read correctly.** Crunch's source
    directions are simultaneous only within the same round: the union of two
    rounds' directions ("from the directions A, B, C and D") would read as
    four simultaneous directions, which is the opposite of the definition.

    The player count goes one step further: it is not simultaneous even within
    the same round, because every sample point is an observation of its own.
    That is why the figures are :class:`AnomalyPoint` rows and not one maximum
    with a list of seconds.

    Attributes:
        map_demo_id: The demo the round is from. One map can have two demos
            (Story 2.11), so the round number alone does not identify it.
        round_no: The round number the scout looks up on the demo. Without it
            the section would say that something happened but not where to see
            it.
        round_type: The round type. On crunch and stack it varies within the
            row, because neither of them knows it.
        points: The sample points with their observations, in **ascending
            order**.
        sources: The source areas on this round -- **simultaneous**. Only on
            crunch.
    """

    map_demo_id: str = Field(min_length=1)
    round_no: int = Field(gt=0)
    round_type: RoundType
    points: list[AnomalyPoint] = Field(min_length=1)
    sources: list[str] = Field(default_factory=list)

    @property
    def seconds(self) -> list[float]:
        """The sample points' moments. Derived, not a field, so it cannot differ."""
        return [point.sample_t_s for point in self.points]

    @property
    def players_max(self) -> int:
        """The largest player count on this round.

        **A summary and not the row's figure.** It is used only where the
        whole round is compared against something (the number of source
        directions, the row's ``players_max``); the report's row sets the
        per-sample-point figures.
        """
        return max(point.players for point in self.points)

    @property
    def points_without_alive(self) -> list[AnomalyPoint]:
        """The sample points that do not give the number of the living."""
        return [point for point in self.points if point.alive is None]

    @property
    def points_without_areas(self) -> list[AnomalyPoint]:
        """The sample points that do not say where the crowd stood."""
        return [point for point in self.points if not point.areas]

    @property
    def areas(self) -> list[str]:
        """The areas the crowd stood on during the round, largest first.

        **The union across the round's sample points and not one of them**:
        the same defence can move between two points, and a list read from one
        of them would name a place the other did not see. The order is each
        point's own (the largest area first), first occurrence first -- a
        sorted list would put the crowd's own area anywhere on the row.
        """
        found: list[str] = []
        for point in self.points:
            for name in point.areas:
                if name not in found:
                    found.append(name)
        return found

    @model_validator(mode="after")
    def _check_round(self) -> AnomalyRound:
        """The round's internal consistency.

        Raises:
            ValueError: If the sample points are impossible, the source areas
                repeat, or there are more directions than players. The last is
                an impossible observation: every direction needs a player of
                its own, so three directions with two players is not a
                stricter observation but a broken one.
        """
        where = f"round {self.round_no} ({self.map_demo_id})"
        seconds = self.seconds
        _check_seconds(seconds, where)
        if sorted(seconds) != seconds:
            raise ValueError(
                f"{where}: the sample points are not in ascending order "
                f"({seconds})."
            )
        if len(set(self.sources)) != len(self.sources):
            raise ValueError(
                f"{where}: the source areas repeat ({self.sources}); the same "
                "direction is one direction."
            )
        # Namelessness before order: a nameless area is not a direction at
        # all, and there is no point saying anything about its place in the
        # list.
        if any(not name.strip() for name in self.sources):
            raise ValueError(
                f"{where}: there is a nameless area among the source areas "
                f"({self.sources}). A nameless area is not a direction."
            )
        if sorted(self.sources) != self.sources:
            raise ValueError(
                f"{where}: the source areas are not in alphabetical order "
                f"({self.sources}). The directions are simultaneous, so they "
                "have no order of their own -- a fixed order makes the "
                "report's row the same from one run to the next."
            )
        if len(self.sources) > self.players_max:
            raise ValueError(
                f"{where}: there are {len(self.sources)} source areas but "
                f"{self.players_max} players. Every direction needs a player "
                "of its own, so the observation is broken."
            )
        # The alive figure is on every point or on none of them. Half a row
        # would set "4/5 ja 4 pelaajaa", that is, two different units on the
        # same row. Which of them is right is decided by the rule
        # (Anomaly._check_stack_fields); here it is only enforced that the row
        # does not disagree with itself.
        missing = len(self.points_without_alive)
        if missing not in (0, len(self.points)):
            raise ValueError(
                f"{where}: {missing} sample points out of {len(self.points)} "
                "do not give the number of the living. The figure is on all "
                "of them or on none -- otherwise the same row would set two "
                "different units."
            )
        # The crowd's areas are all-or-none for the same reason (Story 4.4).
        # Half a row would name the places at one sample point and leave the
        # other bare, and the round's own area list -- which the report's row
        # is written from -- would then be an observation of part of the
        # round presented as one of the whole.
        placeless = len(self.points_without_areas)
        if placeless not in (0, len(self.points)):
            raise ValueError(
                f"{where}: {placeless} sample points out of "
                f"{len(self.points)} do not say which areas the crowd stood "
                "on. The list is on all of them or on none."
            )
        return self


class Anomaly(_Node):
    """One anomalous setup together with its sample.

    A row is **one (rule, map, side, area)** combination, plus the round type
    on advance, and not one round: the same area on two eco rounds is one row
    with the sample ``2/m``, not two rows. Without the grouping a repeated
    anomaly would look like two different observations, and it is exactly the
    repetition that tells a plan from a coincidence.

    **The denominator is per rule.** ``ct_advance`` is a phenomenon of the
    saving rounds, so ``m`` is that round type's rounds on the map and side
    and ``round_types`` has a single element. ``crunch`` does not know the
    round type, so ``m`` is **all** of the side's rounds on the map and
    ``round_types`` says on which types it was observed. One round is a valid
    sample; it is marked as small by the same rule as the others
    (``small_sample``).

    **Stack's denominator is the same as crunch's**: it does not know the
    round type, so ``m`` is all of the side's rounds on the map and
    ``round_types`` says on which types it was observed.

    Attributes:
        rule: ``ct_advance``, ``crunch`` or ``stack``. The first two **share**
            the orientation condition, but neither hit set contains the other:
            crunch adds the direction requirement and drops the round-type
            restriction, so on a saving round the same round produces both
            rows and on a full buy only crunch's. ``stack`` does not read the
            orientation at all and is not a variant of either.
        map_name: The map on which the anomaly was observed.
        map_name_source: Where the map's name came from. It is carried because
            the report's body speaks in names (Story 2.12): when the source is
            ``unknown``, ``map_name`` **is** the demo id, and it must not be
            set into the body bare.
        side: The subject's side. In practice always ``CT``, because all three
            rules examine CT rows; the field is in the structure because the
            row states the side and the reader must not have to infer it from
            the rule's name.
        area: The game's own ``env_cs_place`` area. **Never empty**: an area
            without a name cannot be T territory. On stack it is the **label**
            of the row: the first of the crowd's own areas -- the largest of
            them, and on a tie the first by name. It is not a claim that most
            of the crowd stood there (measured: ``BackofB`` 2 + ``BombsiteB``
            2 is labelled ``BackofB`` by the alphabet), and the observation is
            in the rounds' own ``areas``. Until Story 4.4 the label was the
            site's own area (``BombsiteA`` / ``BombsiteB``) even when nobody
            stood there, and the row then said ``BombsiteB`` while all five
            players were in ``Alley``.
        site: The site group, ``"A"`` or ``"B"``. **Only on stack.** The field
            no longer follows from ``area`` -- the crowd's own area can be any
            area of the group -- so it is the row's own observation: which
            site's group the defence piled into. It is also part of the row's
            identity: two rows can share an area and differ here.
            ``_check_stack_fields`` enforces what this model can see of the
            agreement between the two, which is **less than the whole**: see
            that method.
        round_types: The round types on which the anomaly was observed, in
            ``ROUND_TYPES`` order. Exactly one on advance.
        rounds: The rounds with their observations. "When", "from where" and
            "how many" are read from here in a way that does not let the row
            claim simultaneity across a round boundary.
        orientation: The area's orientation from those demos in which the
            anomaly was observed -- the anomaly's piece of evidence. **Empty
            on stack and only there**: the rule does not read the orientation,
            so the figure would be invented for it. The same rule as with
            ``AnomalyRound.sources`` -- empty means "not asked", not "not
            observed".
        players_max: The largest observed player count on the whole row. A
            summary of ``rounds``, and the model enforces that it matches
            them.
        n: The rounds on which the anomaly was observed (``len(rounds)``).
        m: The denominator, see above.
        small_sample: Whether ``m`` is below ``small_sample_rounds``. The same
            mark by the same rule as elsewhere; ``render`` does not compute it.
    """

    rule: AnomalyRule
    map_name: str = Field(min_length=1)
    map_name_source: MapNameSource
    side: Side
    area: str = Field(min_length=1)
    site: SiteGroup | None = None
    round_types: list[RoundType] = Field(min_length=1)
    rounds: list[AnomalyRound] = Field(min_length=1)
    orientation: list[AreaOrientation] = Field(default_factory=list)
    players_max: int = Field(gt=0)
    n: int = Field(gt=0)
    m: int = Field(gt=0)
    small_sample: bool = False

    @model_validator(mode="after")
    def _check_observation(self) -> Anomaly:
        """The summary cannot disagree with the rows it was assembled from.

        Raises:
            AggregateError: If the sample is impossible (``n > m``) or ``n`` is
                not the length of the round list. Either means that the row
                and its evidence are of different sizes.
            ValueError: If a round appears twice, if the orientation does not
                cover exactly those demos on which the anomaly was observed,
                if the rule and the presence of source areas contradict each
                other, or if ``round_types`` does not match the rounds.
        """
        if self.n != len(self.rounds):
            raise AggregateError(
                f"The anomaly {self.rule} in area {self.area!r} claims a "
                f"sample of {self.n} rounds, but the number of round rows is "
                f"{len(self.rounds)}. The figure and its evidence are of "
                "different sizes."
            )
        if self.n > self.m:
            raise AggregateError(
                f"The anomaly {self.rule} in area {self.area!r} appears on "
                f"{self.n} rounds, although there are {self.m} rounds."
            )
        keys = [(entry.map_demo_id, entry.round_no) for entry in self.rounds]
        if len(set(keys)) != len(keys):
            raise ValueError(
                f"The round list of anomaly {self.area!r} holds the same "
                f"round twice ({sorted(keys)}); a round is one observation."
            )
        expected_types = [
            name for name in ROUND_TYPES
            if name in {entry.round_type for entry in self.rounds}
        ]
        if list(self.round_types) != expected_types:
            raise ValueError(
                f"The round_types of anomaly {self.area!r} is "
                f"{self.round_types}, but the rounds are of the types "
                f"{expected_types}. A summary cannot name a type that no "
                "round is -- nor leave out a type that is there."
            )
        if self.rule == "ct_advance" and len(self.round_types) != 1:
            raise ValueError(
                f"The CT advance in area {self.area!r} carries "
                f"{len(self.round_types)} round types ({self.round_types}). "
                "Advance is grouped by round type, because it is a phenomenon "
                "of the saving rounds and the round type is part of the "
                "observation, so one row has exactly one type."
            )
        biggest = max(entry.players_max for entry in self.rounds)
        if self.players_max != biggest:
            raise ValueError(
                f"The players_max of anomaly {self.area!r} is "
                f"{self.players_max}, but the largest of the rounds is "
                f"{biggest}."
            )
        with_sources = [entry for entry in self.rounds if entry.sources]
        if self.rule == "crunch" and len(with_sources) != len(self.rounds):
            raise ValueError(
                f"The crunch in area {self.area!r} carries rounds without "
                "source areas. Crunch is arrival into an area from several "
                "directions at the same time, so a round without directions "
                "would be a different rule under the same name."
            )
        if self.rule != "crunch" and with_sources:
            raise ValueError(
                f"The rule {self.rule} in area {self.area!r} carries source "
                "areas, although only crunch counts directions. The "
                "directions would claim as measured something that was not "
                "measured."
            )
        self._check_stack_fields()
        demos = [entry.map_demo_id for entry in self.orientation]
        if len(set(demos)) != len(demos):
            raise ValueError(
                f"The orientation of anomaly {self.area!r} holds the same "
                f"demo twice ({sorted(demos)}); an area has one T share per "
                "demo."
            )
        if self.rule == "stack":
            # There is no orientation, so the coverage comparison cannot be
            # made -- and its absence is not a gap. _check_stack_fields has
            # already required the list to be empty.
            return self
        seen = {entry.map_demo_id for entry in self.rounds}
        if set(demos) != seen:
            raise ValueError(
                f"The orientation of anomaly {self.area!r} covers the demos "
                f"{sorted(demos)}, but the observations are from the demos "
                f"{sorted(seen)}. The orientation is the anomaly's piece of "
                "evidence, so it has to cover exactly those demos on which "
                "the anomaly was observed -- no more and no fewer."
            )
        return self

    def _check_stack_fields(self) -> None:
        """Stack's fields belong to stack, and the others do not carry them.

        Four fields separate stack from the two other rules, and each of them
        is here in both directions: ``site``, ``AnomalyPoint.alive`` and
        ``AnomalyPoint.areas`` are stack's observations, ``orientation`` is
        not. Without the guard a row could carry a figure its rule did not
        measure -- and a reader of the report does not see a field's source,
        only its value.

        **The group and the area no longer determine each other** (Story 4.4):
        the row's area is the one the crowd stood on, which is any area of the
        group, so the old equality ``SITE_AREAS[site] == area`` is gone.

        **What is checked here is narrower than "the areas belong to the
        group", and the limit is stated rather than implied.** The area ->
        group mapping is derived per demo (``domain.sampling.site_groups``)
        and is not in the report, so this model cannot look an area up. Two
        things it can see, and they are what it enforces:

        * the row's area is one of its own evidence's areas -- a summary
          cannot name a place its rounds did not see;
        * no area named on the row is the **other** group's own site, which is
          decidable because a site's own area is always in its own group.

        Everything else passes: ``site='B'`` with areas ``['MainHall',
        'House']`` -- both Ancient's A group -- is **accepted here**. That the
        areas are really of one group is the rule's invariant
        (:func:`~pappascout.domain.sampling.stack_hits` reads one group's
        areas at a time), and the archive-wide check that they are is in
        ``tests/test_calibration.py``.

        Raises:
            ValueError: If a field is on the wrong rule, is missing from its
                own, or if ``site`` and the row's areas contradict each other
                in one of the two ways above.
        """
        without_alive = [
            entry for entry in self.rounds if entry.points_without_alive
        ]
        without_areas = [
            entry for entry in self.rounds if entry.points_without_areas
        ]
        if self.rule != "stack":
            if self.site is not None:
                raise ValueError(
                    f"The rule {self.rule} in area {self.area!r} names the "
                    f"site group {self.site!r}, although only stack reads "
                    "site groups."
                )
            if len(without_alive) != len(self.rounds):
                raise ValueError(
                    f"The rule {self.rule} in area {self.area!r} gives the "
                    "number of the living, although it does not count it. "
                    "The figure would look measured but would not concern "
                    "this row."
                )
            if len(without_areas) != len(self.rounds):
                raise ValueError(
                    f"The rule {self.rule} in area {self.area!r} names the "
                    "areas the crowd stood on, although only stack measures "
                    "a concentration. The list would look measured but would "
                    "not concern this row."
                )
            return
        if self.site not in SITE_AREAS:
            raise ValueError(
                f"The stack in area {self.area!r} names the site group "
                f"{self.site!r}; the allowed ones are {sorted(SITE_AREAS)}. "
                "The group is the row's anchor, and it cannot be left "
                "unnamed."
            )
        if without_areas:
            raise ValueError(
                f"The stack in area {self.area!r} carries rounds that do not "
                "say which areas the players were standing on "
                f"({sorted(entry.round_no for entry in without_areas)}). The "
                "rule is a concentration -- four players on at most two areas "
                "-- so a row without the areas reports a crowd without "
                "saying where it stood."
            )
        elsewhere = [
            entry
            for entry in self.rounds
            if self.area not in entry.areas
        ]
        if elsewhere:
            raise ValueError(
                f"The stack's area {self.area!r} is not among the areas of "
                f"the rounds "
                f"{sorted(entry.round_no for entry in elsewhere)}. The row's "
                "area is the one the crowd stood on, so it has to be in the "
                "evidence the row was assembled from."
            )
        other_site = {
            name: group
            for group, name in SITE_AREAS.items()
            if group != self.site
        }
        strays = sorted(
            {
                name
                for entry in self.rounds
                for name in entry.areas
                if name in other_site
            }
        )
        if strays:
            raise ValueError(
                f"The stack of site group {self.site!r} in area "
                f"{self.area!r} stands on {strays}, which is the other "
                "group's own site. A site's own area is always in its own "
                "group, so the row's group and its areas contradict each "
                "other."
            )
        if without_alive:
            raise ValueError(
                f"The stack in area {self.area!r} carries rounds that do not "
                "give how many players were alive "
                f"({sorted(entry.round_no for entry in without_alive)}). "
                "Four out of five and four out of four are a different "
                "observation."
            )
        if self.orientation:
            raise ValueError(
                f"The stack in area {self.area!r} carries the area's "
                "orientation, although the rule does not read it at all. Its "
                "T share would concern a different question from this row."
            )


class AnomalyScan(_Node):
    """What the anomaly rules were given to read.

    **An empty anomaly section is an observation only about what was
    examined.** Without this node "no anomalies" would read as a measured
    negative also when the rules were not run on any round at all, or when
    some demo's orientation came out empty -- and that difference ("an
    observation and not a gap") is the whole value of the section.

    Attributes:
        rules: The rules that were run.
        rules_deferred: The rules named by the architecture (AD-10) that have
            not been implemented. The coverage's denominator: the reader sees
            how many of the spine's rules were left unrun.
        rounds_scanned: The rounds the rules saw -- that is, the ones that
            have a round type. Unclassified ones do not fit into the structure
            at all, and their number is ``Report.unclassified_rounds``.
        crunch_rounds: Of those, the ones **crunch can hit**: the subject's CT
            side rounds. All three rules examine CT rows only, so a T side
            round cannot produce a hit on any of them -- and
            ``rounds_scanned`` alone would promise coverage that is not there.
        advance_rounds: Of those, the ones **CT advance can hit**: the CT
            side's saving rounds. The narrowest figure, and it is exactly
            advance's real denominator as coverage.
        stack_rounds: Of those, the CT rounds **from those demos in which the
            site groups could be derived**.
            *Since Story 4.4 that is no longer quite the same thing as "the
            rounds stack can hit"*, and the difference is recorded here rather
            than left for a reader to discover: the rule reads one sample
            point (``stack_sample_s``), so a round with no living CT row at
            that point cannot produce a hit and is still in this figure. It is
            a second cause of non-examination and this node names only the
            first. Measured 2026-09-18: all 93 of the subject's CT rounds do
            have a living CT row at 15 s, so the two figures coincide on this
            archive -- the settings guard
            (``Settings._check_sections_agree``) stops the whole-archive
            version of the same hazard, a sample point that exists for no
            round at all. A per-round figure is deferred work, not a
            measured negative.
            Required and not defaulted, like the two other coverage figures:
            a missing key would be read quietly as zero, that is, a blind spot
            would be read as a measured negative -- exactly what this node
            exists to prevent.
            *Not the same figure as* ``crunch_rounds``, although neither of
            them restricts the round type: a map on which the sites do not
            separate silences stack completely, and its rounds are in
            crunch's denominator but not in stack's. Without a figure of its
            own, those rounds would look examined with a zero result. **No
            map in the archive is in that state since Story 4.3** -- Nuke,
            which was, is now divided by height -- but the figure stays,
            because it is the difference between "nothing happened" and
            "nothing was looked at" and the archive is not every map.
        demos_without_orientation: The demos from whose sample points not a
            single area came out above the observation threshold. On those
            **advance and crunch** stay silent, and that is not a measured
            negative but a blind spot.
        demos_without_site_groups: The demos from which no site groups could
            be obtained: a site is missing from the point cloud, or the
            distance between the sites' centres relative to the sites' own
            size falls below the threshold **and** the sites' height bands
            are not separated either -- a map has to fail on both axes to
            land here. On those **stack** stays silent.
            The same grounds as for the previous one: silence is the right
            answer, but it has to be recorded -- unspoken it would read as a
            zero hit.
    """

    rules: list[AnomalyRule] = Field(min_length=1)
    rules_deferred: list[str] = Field(default_factory=list)
    rounds_scanned: int = Field(ge=0)
    crunch_rounds: int = Field(ge=0)
    advance_rounds: int = Field(ge=0)
    stack_rounds: int = Field(ge=0)
    demos_without_orientation: list[str] = Field(default_factory=list)
    demos_without_site_groups: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_scan(self) -> AnomalyScan:
        """The coverage figures are nested, and the order is a definition.

        ``advance_rounds`` (CT + saving) is a subset of ``crunch_rounds``
        (CT), which is a subset of ``rounds_scanned`` (all). The wrong order
        would mean that the coverage promises a rule more rounds than the rule
        can examine.

        ``stack_rounds`` is a subset of ``crunch_rounds`` but neither a
        superset nor a subset of ``advance_rounds``: it is the CT rounds
        without a round-type restriction but **only from the demos that were
        not silenced**, and there can be more or fewer saving rounds than
        those. A comparison between these two would therefore be a rule
        without grounds -- the same state as between ``crunch_min_players``
        and ``advance_min_players``.
        """
        if not self.advance_rounds <= self.crunch_rounds <= self.rounds_scanned:
            raise AggregateError(
                f"The coverage figures are not nested: advance "
                f"{self.advance_rounds}, crunch {self.crunch_rounds}, "
                f"all {self.rounds_scanned}.\n"
                "The CT side's saving rounds are a subset of the CT rounds, "
                "which are a subset of all the rounds."
            )
        if self.stack_rounds > self.crunch_rounds:
            raise AggregateError(
                f"Stack's coverage {self.stack_rounds} is larger than the "
                f"number of CT rounds {self.crunch_rounds}.\n"
                "Stack examines CT rounds from those demos in which the site "
                "groups were obtained, so its denominator cannot exceed the "
                "total number of CT rounds."
            )
        if self.stack_rounds < self.crunch_rounds and not (
            self.demos_without_site_groups
        ):
            raise AggregateError(
                f"Stack saw {self.stack_rounds} rounds out of "
                f"{self.crunch_rounds}, but not a single demo has been "
                "recorded as having no site groups.\n"
                "The difference always has to have a named cause: a round "
                "falls out of stack's denominator only if no site groups were "
                "obtained from its demo -- an unnamed difference would read "
                "as a measured negative."
            )
        # THE OPPOSITE DIRECTION IS NOT AN INVARIANT, and it must not be
        # added. A silenced demo can have zero of the subject's CT rounds, in
        # which case it is in this list although the figures are equal. The
        # condition "list non-empty => the figures differ" would then reject a
        # valid observation.
        if len(set(self.demos_without_site_groups)) != len(
            self.demos_without_site_groups
        ):
            raise ValueError(
                "The same demo twice in the list of those without site "
                f"groups: {self.demos_without_site_groups}."
            )
        unknown = sorted(set(self.rules) - set(ANOMALY_RULES))
        if unknown:
            raise ValueError(
                f"Unknown anomaly rules: {unknown}. The allowed ones are "
                f"{list(ANOMALY_RULES)}."
            )
        if len(set(self.rules)) != len(self.rules):
            raise ValueError(f"The same rule twice: {self.rules}.")
        if len(set(self.demos_without_orientation)) != len(
            self.demos_without_orientation
        ):
            raise ValueError(
                "The same demo twice in the list of those without an "
                f"orientation: {self.demos_without_orientation}."
            )
        return self


class Report(_Node):
    """``aggregates/<team_key>/report.json``.

    Every figure has been computed in advance; ``render`` only picks and
    formats.
    """

    schema_version: str = REPORT_SCHEMA_VERSION
    generated_at: datetime
    #: The tools whose version affected this result. Not a manifest field but
    #: there for traceability: ``report.json`` is always overwritten, so its
    #: contents may say which version made it.
    tool_versions: dict[str, str] = Field(default_factory=dict)
    team: TeamReport
    sample: Sample
    #: The same sample as :attr:`sample`, bucketed by ``roster_class``
    #: (Story 3.9). A second breakdown rather than a field on
    #: :class:`TeamReport`, because it counts demos and rounds and that is
    #: what a sample is.
    #:
    #: **Summary only.** AD-10 puts the ``5/5`` / ``4/5`` split in the summary
    #: and gives the per-level contract as ``{league_rounds, other_rounds,
    #: league_demos, other_demos}`` -- so :class:`MapReport`,
    #: :class:`SideReport` and :class:`RoundTypeReport` keep one sample each.
    #: Widening that is a spine change, not an implementation detail.
    #:
    #: **Required, not defaulted.** See :data:`REPORT_SCHEMA_VERSION`: an old
    #: file must fail rather than read as current.
    roster_sample: RosterSample
    #: The ``[thresholds]`` and ``[aggregate]`` sections as they were **when
    #: this aggregation was run**. They are not the same as the ones the
    #: rounds were classified with -- classification is a different stage and
    #: may have been run with different settings. Classification's own
    #: thresholds are in the field :attr:`classify_thresholds`, and they are
    #: read from the classified table and not from the current settings.
    thresholds_used: dict[str, Any] = Field(default_factory=dict)
    #: The threshold values the rounds **really were classified with**, read
    #: from the ``CLASSIFIED.inputs`` column. ``classify`` stores for every
    #: round the values used in the comparison, so this is an observation and
    #: not a copy of the current settings. ``aggregate`` refuses if the values
    #: differ between rounds: the report would then mix rounds classified by
    #: different rules into the same figure.
    classify_thresholds: dict[str, int] = Field(default_factory=dict)
    #: Detonations that have no throw row. The pair is joined by the key
    #: ``(map_demo_id, grenade_no)``, and ``parse`` always writes them as a
    #: pair -- so an odd row is a sign of a broken table. It is dropped from
    #: the utility computation, but the count is here, because a silent drop
    #: would look as though the grenade had not been thrown.
    unpaired_detonations: int = Field(default=0, ge=0)
    missing_demos: list[MissingDemo] = Field(default_factory=list)
    #: Rounds that have no round type. They are not in the structure -- the
    #: round-type level cannot be built without a type -- but the count is
    #: reported, so that a round does not vanish quietly.
    unclassified_rounds: int = Field(default=0, ge=0)
    #: Anomalous setups, every map and side in the same list. An empty list is
    #: an **observation** and not a gap: "no anomalies" is a result, and the
    #: report says so out loud in its own section -- but only about what
    #: :attr:`anomaly_scan` says was examined.
    #:
    #: The list is at the root of the report and not under the round type,
    #: because an anomaly is the epic's most valuable output: broken into 24
    #: blocks it would be the very problem Story 2.5 solves. Every row
    #: therefore carries the map, the side and the round types itself.
    anomalies: list[Anomaly] = Field(default_factory=list)
    #: The anomaly rules' coverage: what was run, on what, and what was left
    #: in a blind spot. **Required and not defaulted**, because it is exactly
    #: the empty anomaly list that needs it: without the coverage "no
    #: anomalies" does not differ from the rules not having been run.
    anomaly_scan: AnomalyScan
    maps: list[MapReport] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_rounds(self) -> Report:
        """The top-level sample is the sum of the maps.

        ``unclassified_rounds`` is **not** part of the sum and must not be: a
        round without a round type does not fit into the structure at all, so
        it is not in any map's, any side's or any round type's sample. It is a
        figure of its own exactly so that it is not counted into claims it
        does not support.
        """
        _check_rounds_add_up(
            self.sample, [m.sample for m in self.maps], "report", "map"
        )
        _check_demos_add_up(self.sample, [m.sample for m in self.maps])
        self._check_matches_are_not_invented()
        self._check_breakdowns_agree()
        self._check_anomalies()
        return self

    def _check_matches_are_not_invented(self) -> None:
        """The report's matches, against the maps', as an **inequality**.

        Demos sum (:func:`_check_demos_add_up`) because every demo is on
        exactly one map. Matches do not: a ``best_of`` match plays several
        maps, so the same match is in several branches. Measured 2026-09-24 on
        the real archive, the scouted team's report is 4 matches over maps of
        4, 3 and 1 -- the sum is 8, and it is not the answer to anything.

        What does hold is a pair of bounds: the report cannot hold **fewer**
        matches than its busiest map (that map's matches are all in the
        report), and it cannot hold **more** than the maps added up (a match
        reaches the report only through a map).

        **They do not catch a match count taken from the demos**, which is
        the mistake this story exists to stop. Measured 2026-09-24 on this
        archive's own numbers, the substitution passes the upper bound
        exactly, and :func:`_check_matches_are_bounded` sets out why no
        archive would make it fail. What these bounds guard is a hand-edited
        file and a future second pass, and they cannot fire on the pipeline's
        path at all.

        Raises:
            ~pappascout.errors.AggregateError: If either bound is broken.
        """
        _check_matches_are_bounded(
            self.sample, [m.sample for m in self.maps], "report", "map"
        )

    def _check_breakdowns_agree(self) -> None:
        """The two breakdowns of the summary sample must have equal totals.

        :attr:`sample` and :attr:`roster_sample` bucket the *same* demos and
        rounds -- one by ``is_league``, the other by ``roster_class`` -- so
        their totals cannot differ. Each breakdown already checks its own
        totals against its own buckets, and that check alone would pass a
        breakdown that is internally consistent about the wrong demos.

        **What this actually catches is a hand-edited ``report.json``.**
        ``aggregate`` buckets both breakdowns from the same rows and gives
        every demo a bucket in each, so it cannot emit a mismatched pair; a
        demo missing from the roster bucketing stops
        :func:`~pappascout.domain.aggregate.roster_sample_for` instead, and
        stops it naming the demo. This guard is what keeps a file edited or
        written elsewhere from being read as a measurement.

        The exception is :class:`~pappascout.errors.AggregateError`, the same
        type every other sample-total check raises, because it is the same
        fault -- and the caller catches them as one.

        Raises:
            ~pappascout.errors.AggregateError: If the demo or round totals of
                the two breakdowns differ.
        """
        if (
            self.sample.demos == self.roster_sample.demos
            and self.sample.rounds == self.roster_sample.rounds
        ):
            return
        raise AggregateError(
            "The summary's two breakdowns speak of different samples: the "
            f"league breakdown claims {self.sample.demos} demos and "
            f"{self.sample.rounds} rounds, the roster breakdown "
            f"{self.roster_sample.demos} demos and "
            f"{self.roster_sample.rounds} rounds.\n"
            "Both bucket the same demos, so the totals have to be the same. "
            "Aggregation cannot produce a difference -- it buckets both "
            "breakdowns from the same rows -- so report.json has been edited "
            "by hand or written elsewhere. Run aggregation again."
        )

    def _check_anomalies(self) -> None:
        """Anomalies are outside the tree, so the side is pinned here.

        Two conditions no :class:`Anomaly` can check by itself: a row must not
        name a map that is not in the report (the reader would look for a map
        section that was never written), and two rows must not share the same
        grouping key (the same observation would then be in the section twice
        with different figures -- exactly what the grouping exists to
        prevent).

        Raises:
            AggregateError: If the map is missing from the report or a key
                repeats.
        """
        known = {entry.map_name for entry in self.maps}
        missing = sorted(
            {a.map_name for a in self.anomalies if a.map_name not in known}
        )
        if missing:
            raise AggregateError(
                f"An anomaly names a map that is not in the report: "
                f"{missing}. The report's maps are {sorted(known)}.\n"
                "The reader would look for a map section that was never "
                "written."
            )
        # The key carries the round type on an advance (it is grouped by
        # type) and the site group on a stack. The group is part of the
        # stack's identity since Story 4.4: the row's area is the crowd's own,
        # so the same area CAN be read into different groups by two demos of
        # the same map -- the division is derived per demo by design (AD-13),
        # and nothing in the derivation makes it agree across demos.
        #
        # **Possible and not measured**, and the distinction is the point:
        # checked over all three of the archive's multi-demo maps, no area is
        # read into different A/B groups by two demos of the same map. What
        # does differ is grouped vs. ungrouped (Anubis ``Middle``, Nuke
        # ``Control``, Nuke ``Garage``), and an ungrouped area produces no hit
        # at all. So this guard is defence against a state the derivation
        # allows, not a fix for one the archive shows.
        keys = [
            (a.rule, a.map_name, a.side, a.area)
            + (tuple(a.round_types) if a.rule == "ct_advance" else ())
            + ((a.site,) if a.rule == "stack" else ())
            for a in self.anomalies
        ]
        twice = sorted({key for key in keys if keys.count(key) > 1})
        if twice:
            raise AggregateError(
                f"The same anomaly is in the section twice: {twice}.\n"
                "The grouping key is (rule, map, side, area) plus the round "
                "type on advance and the site group on a stack. Two rows with "
                "the same key mean that the grouping did not do its work: the "
                "same observation would show twice with different samples."
            )
