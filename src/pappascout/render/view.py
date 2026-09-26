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

**Protected round types and filtered ones are of different shapes.**
Pistol and anomaly are described round by round: every observation is
written. Every other round type (:data:`PATTERN_ROUND_TYPES`) is described
**only as repeating patterns**, and the repetition limit is read from the
report (``thresholds_used.thresholds.small_sample_rounds``) -- it is not
invented here. The number of observations left out is written out, so the
filtering is not silent.

**The limit is capped at the block's own round count**
(:func:`block_min_rounds`). A block of two rounds cannot hold a repetition
of three, so an uncapped limit would either empty the block or -- as it did
for ``eco``, ``force`` and ``half`` before 2026-09-11 -- never be applied at
all and let every single observation through as though it were a pattern.
The cap leaves a large block exactly as it was (62 rounds still demand 3)
and bites only where nothing *can* repeat three times. Because the limit
then differs from block to block, **the block's own note states the number
that block used** -- a block that said "3" while filtering at 2 would be a
false claim about the data.

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

**Which strings in this module are Finnish, and how to tell** (AD-11).
Everything here is English -- identifiers, docstrings, comments -- except
**the text this module lays into the report**, which stays Finnish
permanently because Finnish team-mates read the report before a match. The
boundary is not the ``*_FI`` naming convention and it is not the package.
This module is full of constants that carry no ``*_FI`` in their names and
reach the report anyway -- :data:`UNKNOWN_AREA`, :data:`ESTIMATE_MARK`,
:data:`TRACEABILITY_HEADING`, :data:`UNNAMED_PLAYER`, :data:`RECORD_VERB`,
:data:`MATCH_SAMPLE_UNIT`, :data:`RECENCY_NEWEST_INCLUDED`,
:data:`MATCH_NOT_INDEXED`, :data:`MAP_POOL_LABEL`, and so on down the file.
**Trace a value to its consumer**; the name does not tell you. A constant
added below inherits this paragraph, which is why it is here and not attached
to whichever constant happened to be the last one to need saying so.

**This paragraph used to say how many there were, and the number was wrong in
four stories running**: fifteen, then seventeen (Story 4.8), then twenty
(Story 4.9) -- and Story 4.10 added seven while the word "twenty" stayed, in
a sentence carrying that story's own date. The count is gone rather than
corrected a fifth time, and the reason is the pattern and not the
arithmetic: it is a property of the file below, it moves whenever anybody
adds a line, and **nothing in the repository re-reads it**. What does re-read
the tree is :mod:`tests.test_translated_prose`, on every run, and it is the
only thing that does -- so the **rule** lives here, where a constant's author
meets it, and the counting lives there, where it cannot go stale in silence.

**Some of the round types are protected** (:data:`PROTECTED_ROUND_TYPES`),
and every pruning paragraph of the reading guide says so out loud: the same
report holds unpruned blocks, so an unqualified sentence would be false.

``Report``, ``report.json`` and ``REPORT_SCHEMA_VERSION`` do not change, and
every pruned value is still in them -- it merely goes untold in *this*
report. **Rule 3 differs in one way since Story 4.5, and it is written here
because this is where the rule is stated**: its points are the analysis's
dense grid and not report content, so they are **not built into a block at
all** -- they do not enter the block's "harvinaisempaa" count and the
empty-block return cannot bring them back. They are still in ``report.json``.
The pistol routes read only the printed points, through
``[aggregate].route_sample_seconds``, which the settings hold equal to them;
the aggregate stage hashes that list as part of its own section and no
``[report]`` value (AD-3).
The measured rationales and the numbers are in ``settings.toml``;
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
rule is here and not only inside the functions, because it concerns seven of
them (:func:`_title`, :func:`_team_text`, :func:`_roster_text`,
:func:`_summary`, :func:`_traceability`, :func:`_anomaly_map_label`,
:func:`_played_map_line`) -- written inside one of them it would not say that
there are exactly four exceptions:

1. **The round appendix's path.** A path is usable only as it is, and it is
   a reading aid rather than a traceability entry.
2. **The missing demo's row.** The id is part of a command the reader copies
   (``uv run pappascout parse <demo>``); without it the row would not say
   what to do.
3. **A map whose name was not recognised.** Then ``map_name`` *is* the
   ``map_demo_id`` (see :class:`~pappascout.domain.report.MapReport`), that
   is, the id is the map's only name -- the alternative would be a nameless
   map chapter.
4. **A row of a map chapter's demo list** (Story 4.10). The id says which
   demo the row is about, and on a hand-imported demo -- which has neither a
   date nor an opponent -- it is the row's only distinguishing mark.

This list grew from three to four in Story 4.10, and three other texts had to
grow with it: the reading guide's sentence, :data:`_TRACEABILITY_NOTE`, and
the test that holds the guide to the code. A number written in four places is
exactly the shape the paragraph above this one was deleted for, and it
survives here only because a **test** reads the guide's wording back.

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
from collections import Counter
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
    MapReport,
    PlayedMap,
    Position,
    Report,
    RoundRecord,
    RoundRoute,
    RoundTypeReport,
    RouteStep,
    UtilityCounts,
    UtilityUse,
)
from pappascout.errors import PappascoutError

__all__ = [
    "GRENADE_TYPE_FI",
    "GRENADE_ORDER",
    "ROUND_TYPE_ORDER",
    "SIDE_ORDER",
    "PATTERN_ROUND_TYPES",
    "PROTECTED_ROUND_TYPES",
    "MERGED_EQUIPMENT_LABEL",
    "MAX_DEATH_LINES",
    "KILL_SAMPLE_UNIT",
    "UNKNOWN_AREA",
    "RECORD_VERB",
    "RECORD_UNKNOWN_OUTCOME",
    "TRACEABILITY_HEADING",
    "ANOMALY_HEADING",
    "MAX_ANOMALY_LINES",
    "UNKNOWN_MAP_LABEL",
    "PLAYED_MAPS_ORDERED",
    "PLAYED_MAPS_UNORDERED",
    "MATCH_NOT_INDEXED",
    "MATCH_DATE_MISSING",
    "OPPONENT_MISSING",
    "OPPONENT_PREFIX",
    "MAP_POOL_LABEL",
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
    "block_min_rounds",
    "pattern_min_rounds",
    "rounds_text",
    "record_text",
    "demos_text",
    "times_text",
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
#: **Force before eco is the product owner's order** (2026-09-26, Story
#: 4.12): *"ct-pistol, t pistol --> ct force, t force --> ct eco, t eco -->
#: ct half, t half"*. Default, which his sentence does not name, comes after
#: the half-buy; of the two types that are not buy classes, ``anomaly`` sits
#: before it and ``ot`` after. Since Story 4.12 this is also the order of the
#: map chapter's sections --
#: the buy class is above the side (:class:`BuyClassView`).
#:
#: The list covers **every**
#: :data:`~pappascout.constants.ROUND_TYPES` value, and a test watches that.
#: Without the coverage a new round type would vanish from the report in
#: silence.
ROUND_TYPE_ORDER: tuple[str, ...] = (
    "pistol",
    "force",
    "eco",
    "half",
    "anomaly",
    "full",
    "ot",
)

#: The sides' presentation order inside a buy class: **CT before T**, as the
#: product owner wrote every pair (2026-09-26, Story 4.12). A presentation
#: order and not the enumeration :data:`~pappascout.constants.SIDES`, which
#: happens to list T first; a test holds the two to the same members.
SIDE_ORDER: tuple[str, ...] = ("CT", "T")

#: The round types about which **only repeating patterns** are told. The
#: product owner: "there is no need to tell what they did on every round, it
#: tries to spot only the broad lines". A full buy is the least clear and the
#: most common round plan, so telling it round by round would be mostly
#: repetition.
#:
#: **The set is the complement of :data:`PROTECTED_ROUND_TYPES`, and that is
#: the point of it** (2026-09-11). Until then it held ``full`` and ``ot``
#: only, and ``eco``, ``force`` and ``half`` fell between the two lists:
#: neither filtered nor protected, and for no stated reason. Measured from
#: the real three-map report, ``eco`` then took 27 % of the body for 7 % of
#: the rounds, because in a two-round block every single observation was
#: written and almost every one of them read ``(1/2 kierroksesta)`` -- one
#: observation out of two, which is not a pattern. A round type that is not
#: protected is filtered; a type that must not be filtered goes on the
#: protected list with its measured reason. There is no third state, and
#: ``test_every_round_type_is_either_filtered_or_protected`` keeps it that
#: way.
#:
#: The filtering is only usable on a small block because the threshold is
#: capped at the block's round count (:func:`block_min_rounds`): without the
#: cap, adding these three types would have emptied every two-round block
#: instead of shortening it.
PATTERN_ROUND_TYPES: frozenset[str] = frozenset(
    {"eco", "force", "full", "half", "ot"}
)

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

#: The match sample's **unit**: ``4 kierrosta 4 ottelussa``.
#:
#: **The inessive, and it is the product owner's own word** (2026-09-24): if
#: the sentence to be made is "four rounds in four matches", then the
#: inessive is the right case. (His words are translated here rather than
#: quoted, as :data:`PATTERN_ROUND_TYPES` translates his; AD-11 keeps Finnish
#: to the report's own content and a docstring is not that.) The
#: implementation had proposed the elative ``ottelusta``, to match
#: :attr:`Claim.unit`'s ``kierroksesta`` beside it; that argument is recorded
#: here because it lost, not because it is still open. The sentence
#: the heading makes is "four rounds **in** four matches", and the rounds are
#: in the matches rather than out of them.
#:
#: A constant and not a literal, for :data:`KILL_SAMPLE_UNIT`'s reason: the
#: reading guide explains this word and the rows print it, and written twice
#: one of them would go on saying something the report no longer says.
#:
#: What is still **awaiting the product owner's word** (Story 4.9, "Ask
#: First"), as :data:`RECORD_VERB` is: :data:`RECENCY_NEWEST_INCLUDED`,
#: :data:`RECENCY_NEWEST_ABSENT`, :func:`played_maps_text` and the reading
#: guide's three entries. The unit itself is settled.
MATCH_SAMPLE_UNIT = "ottelussa"

#: The recency mark when the newest of the matches is among them.
#:
#: The reader's question before a match is *"is this still true?"*, and a
#: count cannot answer it: measured 2026-09-23, ``de_nuke`` T pistol reads
#: *Outside 3 (3/4 kierroksesta)* where the truth is "in the three oldest
#: matches and not in the newest" -- the opposite piece of advice. The mark
#: is the answer in the fewest words that are still plain; an arrow or a star
#: would be shorter and would have to be looked up.
#:
#: **A mark and not a weight.** The product owner chose this over weighting
#: recent matches more heavily (option A, 2026-09-24): a weighted count
#: cannot be checked against the demos, and every number in this report has to
#: be one the reader could re-derive by watching.
#:
#: **It is written only where it can be false**, and that is the cost this
#: constant carries rather than the rule that carries it. Two of the three
#: teams in the developer's archive are entirely hand-imported demos, one map
#: per match, so most of their blocks hold a single match -- and there every
#: observation is in the newest match by definition. Printed, this word would
#: be on every line of those reports, always true, and a reader learns to skip
#: a mark that never varies. :func:`_block_states_matches` is where the block
#: is left unmarked, and the reading guide names that reason beside the other
#: one (an order that is not known), because a reader cannot tell the two
#: apart from the line.
#:
#: **Awaiting the product owner's word**, as :data:`MATCH_SAMPLE_UNIT` says.
RECENCY_NEWEST_INCLUDED = "uusin mukana"

#: The recency mark when the newest of the matches is **not** among them.
#:
#: Written out rather than left off, and that is the whole of its value: an
#: absent mark would be the ordinary case for every row the story does not
#: touch, so silence cannot mean "not in the newest". The words are short
#: because the line already says ``ottelusta`` immediately before them.
#:
#: **Awaiting the product owner's word**, as :data:`MATCH_SAMPLE_UNIT` says.
RECENCY_NEWEST_ABSENT = "ei uusimmassa"

#: The win-loss record's verb on the round type's sample line (Story 4.8):
#: ``**Default** (22 kierrosta, voitettu 15-7)``.
#:
#: **A verb and not a ratio.** The report writes the count and nothing
#: derived from it -- no percentage, no rate and no verdict. Two wins in
#: three rounds is not a finding, and a number the report does not compute is
#: one the reader cannot mistake for one.
#:
#: The separator is an ASCII hyphen, not an en dash. Measured 2026-09-23: the
#: source tree holds no en or em dash anywhere, and the template writes its
#: own separators as ``--``. One typographic exception inside a heading line
#: would be the only non-ASCII punctuation in the report.
#:
#: **Awaiting the product owner's word** (Story 4.8, "Ask First"), as are
#: :data:`RECORD_UNKNOWN_OUTCOME` and the record's sentence in the reading
#: guide. The line is read before a match and the words are his; these three
#: are the implementation's suggestion and not his choice. The open question
#: is in the architecture memlog, because a contract question recorded only
#: in a source docstring is not in the decision record.
RECORD_VERB = "voitettu"

#: How the record names rounds whose outcome is not known.
#:
#: The unknown is **named and not hidden**, and it is never folded into the
#: losses: ``won`` is nullable in the classified table, so an unread outcome
#: is a gap in the recording and not a defeat. Written as the whole
#: sentence-fragment rather than a bare count, because ``2 tuntematonta``
#: leaves the reader to guess what is unknown -- the round, the side or the
#: figure before it.
#:
#: Measured 2026-09-23: 0 of the real archive's 511 classified rounds have an
#: empty ``won``, so nothing in the archive prints this today. It is written
#: because the column allows it, not because it was seen.
#:
#: **Awaiting the product owner's word**, as :data:`RECORD_VERB` says.
RECORD_UNKNOWN_OUTCOME = "kierroksen tulos ei tiedossa"

#: The outcome on a pistol round's own row (Story 4.11). The product owner
#: asked for it on 2026-09-25, in the words recorded in
#: ``raportin-muoto-2026-09-25.md``: he could not tell from the row how the
#: round had ended, and asked whether it could say which of the two it was.
#:
#: **One word and not a verb phrase**, unlike :data:`RECORD_VERB` beside it,
#: and the difference is the unit: the record counts a block's rounds and
#: needs a verb to say what the count is of, whereas this labels **one**
#: round, where the outcome is the whole statement.
#:
#: A round whose outcome was not read takes :data:`RECORD_UNKNOWN_OUTCOME` --
#: the same string the block's record uses, because it is the same state of
#: the same nullable column, and two wordings for it would read as two
#: different gaps.
#:
#: **Awaiting the product owner's word**, as :data:`RECORD_VERB` says.
ROUND_WON = "voitto"

#: The other outcome. See :data:`ROUND_WON`.
ROUND_LOST = "häviö"

#: How a round is named on its own row: ``(kierros 13)``.
#:
#: The number is on the row because the reader's next act is to open the demo
#: at that round -- the same reason :class:`~pappascout.domain.report
#: .AnomalyRound` carries one. It is the **played** round number, so 1 and 13
#: are regulation time's two pistol rounds.
#:
#: **Awaiting the product owner's word**, as :data:`RECORD_VERB` says.
ROUND_LABEL = "kierros"

#: How a route states a death: ``1 kuoli Mini (27 s)``.
#:
#: The product owner's own word and his correction, 2026-09-25: he asked for
#: this verb in place of the mock's, and for the place to be stated as well.
#: The place is ``victim_area`` and the moment ``t_s``, both read from
#: ``parsed/<map_demo_id>/deaths.parquet`` -- **measured and never inferred
#: from a missing sample point**, which is the defect the first mock shipped.
ROUTE_DIED = "kuoli"

#: What a route says about a player it cannot account for: the sample point
#: exists, the player has no row on it, and no death record places them.
#:
#: **It is not a death and must not read as one.** The three claims this
#: story keeps apart are *"nobody went there"*, *"nobody was alive to go
#: anywhere"* and *"the round was already over"*; this string is the honest
#: form of the second where the archive holds no proof of it.
#:
#: **Awaiting the product owner's word**, as :data:`RECORD_VERB` says.
ROUTE_GONE = "poistui otannasta"

#: What a pistol round's row says when the round reached no sample point at
#: all -- settled inside the first of them, so there is no route to state.
#:
#: The row is still written. Dropping it would leave the block's heading
#: counting a round the reader never sees, and the shorter list would be
#: taken for the sample.
#:
#: **Awaiting the product owner's word**, as :data:`RECORD_VERB` says.
ROUTE_NO_SAMPLE = "kierros ratkesi ennen ensimmäistä näytepistettä"

#: The arrow between two sample points **within one row**.
#:
#: **It is not adjacency and the reading guide says so** (Story 4.11,
#: "Never"): these are positions several seconds apart, so ``A -> B`` means
#: the players were in A and then in B, not that A touches B. The product
#: owner corrected exactly that reading once, on ``Ruins -> Banana 27``.
#:
#: **It never begins a row.** A row is a Markdown list item and starts with
#: its marker; the arrow only ever joins moments a group passed through
#: while it stayed together. Where a group divides, the division is the
#: **nesting** and not an arrow -- which is what lets a reader tell "these
#: four happened one after another" from "these four happened instead of
#: each other".
ROUTE_ARROW = "->"

#: One level of **list nesting** under the point a group divided at.
#:
#: The shape is the product owner's, settled 2026-09-25 after he rejected
#: two renderings: the first put every leaf on a full row and he asked why
#: the shared stretch repeated; the second indented but repeated it still.
#: So the shared stretch is written **once** and only the parts that
#: separated are nested under it.
#:
#: **Two spaces and not four, because the rows are list items.** Every route
#: row opens with ``- ``, and a nested list has to start at or past the
#: content column of the item above it -- which two spaces per level gives
#: exactly (``  - `` -> ``    - `` -> ``      - ``). The four-space version
#: belonged to rows that opened with ``-> ``, and those were not list items
#: at all: CommonMark read them as lazy paragraph continuations and threw
#: the indentation away. See :func:`_route_rows`.
ROUTE_INDENT = "  "

#: The record the reading guide shows as its example, as a record and not as
#: text: the guide's sentence runs it through :func:`record_text`, the same
#: function the heading uses, so the example cannot come to explain a string
#: the report no longer prints.
#:
#: **The precedent is :data:`KILL_SAMPLE_UNIT` four constants above** and not
#: ``SITE_GROUPS = tuple(SITE_AREAS)``: nothing here is derived from anything
#: -- the numbers are an independent literal -- and what the guide and the
#: heading share is the **function**. That is exactly the kill row's problem
#: ("written twice, one of them would go on telling of rounds"), and it has
#: the same answer.
#:
#: The numbers are the real archive's Nuke CT default block, and they are
#: **pinned** rather than merely stated: ``tests/data/round_records.json``
#: carries that group's record and an ``-m archive`` test compares it against
#: the archive, so this example stops being true loudly rather than quietly.
_LEGEND_RECORD = RoundRecord(wins=15, losses=7, unknown=0)

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

#: The map chapter's demo list label when **every** demo's date is known.
#:
#: The label carries a claim, and that is why there are two of these. The list
#: is ordered by the matches' own order, which the archive's match index
#: gives; a demo the index does not place is appended, and then the list is
#: not newest first at all. A label that said so anyway would be the one
#: sentence in the chapter a reader could check against the rows and find
#: false. The rule is :func:`~pappascout.domain.aggregate.newest_match`'s --
#: all or nothing, because one unplaced demo makes the rest's order
#: unknowable, not merely incomplete.
#:
#: **Awaiting the product owner's word** (Story 4.10), as
#: :data:`MATCH_SAMPLE_UNIT` is: he asked for the maps, the dates and the
#: opponents, and the wording around them is the implementation's.
PLAYED_MAPS_ORDERED = "Kartat uusin ensin"

#: The section the CT advances are printed in, per map (Story 4.5).
#:
#: **Awaiting the product owner's final wording**: he wrote *"huomioitavaa
#: tms"*, 2026-09-25. The distinction behind it is his and is the point: an
#: advance is a habit -- one player's, or the team's way of working a site --
#: and not a strategy, so it does not stand among the anomalies (crunch,
#: stack) that the report can state as a pattern.
HABITS_LABEL = "Huomioitavaa"

#: The unit of a habit row's round sample: the side's **save** rounds on the
#: map, which is the advance's denominator (Story 4.5). **Awaiting the
#: product owner's wording.**
HABIT_SAMPLE_UNIT = "säästökierroksesta"

#: The anomaly chapter's pointer to where the CT advances are (Story 4.5).
#: **Awaiting the product owner's wording.** It names the reason as his:
#: a habit, not a strategy.
HABITS_POINTER = (
    f"CT-etenemiset ovat karttalukujen kohdassa {HABITS_LABEL}: ne kertovat "
    "pelaajan tai joukkueen tavasta eivätkä joukkueen strategiasta."
)

#: The same label when at least one demo has no date. See
#: :data:`PLAYED_MAPS_ORDERED`.
PLAYED_MAPS_UNORDERED = "Kartat"

#: A demo whose match is not in the archive's match index at all.
#:
#: **One phrase for two absences**, and that is the honest shape rather than
#: brevity: such a demo has neither a date nor an opponent, and it has neither
#: for the same single reason. Two separate "not known" marks on one row would
#: read as two independent gaps.
#:
#: The reading guide says what it means and, more to the point, what the
#: report **will not** do about it: a hand-imported demo's file name often
#: contains something that looks like an opponent, and reading a name out of
#: it would put a claim in the report that nothing can check.
#:
#: **Awaiting the product owner's word**, as :data:`PLAYED_MAPS_ORDERED` is.
MATCH_NOT_INDEXED = "ei otteluindeksissä"

#: A demo whose match is in the index but carries no finish time. Measured
#: 2026-09-24: 35 of the archive's 66 indexed matches are like this.
#:
#: **Awaiting the product owner's word**, as :data:`PLAYED_MAPS_ORDERED` is.
MATCH_DATE_MISSING = "päivämäärä ei tiedossa"

#: A demo whose match is in the index but whose opponent cannot be named --
#: a malformed entry, or one whose two sides cannot be told apart from the
#: team's own observed roster (:func:`~pappascout.stages.aggregate
#: ._opponent_name`).
#:
#: **Written out and never left blank.** A row that simply stopped after the
#: date would read as a map played against nobody, which is the one thing the
#: absence must not look like.
#:
#: **Awaiting the product owner's word**, as :data:`PLAYED_MAPS_ORDERED` is.
OPPONENT_MISSING = "vastustaja ei tiedossa"

#: How the opponent is introduced on the row: ``vastustaja Teekkarit``.
#:
#: A word and not a punctuation mark, because the name is free text from the
#: index and stands beside a date: ``2026-09-20, Teekkarit`` reads as a second
#: field of the same kind, and a team called after a date would be unreadable.
#:
#: **The example team is invented** (Story 4.10, AD-12): this illustrates a
#: format, and any string would do it, so a real league team's name has no
#: business here. The rule -- a real name is written where it identifies the
#: evidence a claim rests on, and not where a string is merely needed -- is in
#: the architecture memlog of 2026-09-25.
#:
#: **The report states the name and judges nothing** (Story 4.10, "Never"):
#: no strength, no seeding, no "against a weak team". The reader is the
#: analyst.
#:
#: **Awaiting the product owner's word**, as :data:`PLAYED_MAPS_ORDERED` is.
OPPONENT_PREFIX = "vastustaja"

#: The summary row that states the map pool: which maps the team plays and how
#: many times each.
#:
#: The product owner asked for it in the same conversation as the opponent
#: (2026-09-24): *"it also tells which maps the team plays, so that is
#: genuinely useful information too"* -- translated here, as
#: :data:`MATCH_SAMPLE_UNIT` translates his words rather than quoting them.
#:
#: **Awaiting the product owner's word**, as :data:`PLAYED_MAPS_ORDERED` is.
MAP_POOL_LABEL = "Karttavalikoima"


# -- The view model's parts ------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """One claim and its sample.

    A claim cannot be built without a sample: ``n`` is the rounds on which
    the observation was made, ``m`` all of that level's rounds.

    **The match sample is optional and the round sample is not**, and that
    asymmetry is Story 4.9's boundary rather than an oversight. Every claim in
    the report counts rounds; the ones the model gives a match count to are
    the sample-point rows and the first-contact row, which is where the
    measurement of 2026-09-23 found the report saying the opposite of the
    truth. A claim built without ``matches_n`` prints exactly what it printed
    before, character for character. The rows still without one are named in
    :mod:`pappascout.domain.report`'s own docstring, where the reason belongs.
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
    #: The same observation in **matches**: how many of the ``matches_m``
    #: matches the denominator covers it was made in. Both or neither --
    #: :meth:`__post_init__` refuses one without the other, because half a
    #: fraction is not a sample.
    matches_n: int | None = None
    matches_m: int | None = None
    #: Whether the **map's** newest match is among the matches the observation
    #: was made in, or ``None`` when the matches' order is not known. The
    #: model's own three states
    #: (:attr:`~pappascout.domain.report.PlayersCount.newest`), carried
    #: through unchanged: ``render`` does not decide what a missing order
    #: means, and it does not decide which match is the newest either.
    #:
    #: **It is independent of the fraction above**, and that is deliberate.
    #: A block of one match prints no fraction (every one would be ``1/1``)
    #: and still prints the mark, because since the mark became the *map's*
    #: the answer is no longer a foregone one: a one-match block is either
    #: the map's newest or it is stale, and which of the two is the most
    #: useful thing on the line.
    newest: bool | None = None

    def __post_init__(self) -> None:
        """A match sample is a fraction, so it is both numbers or neither.

        The pair is two fields and not one node because ``Claim`` is a
        dataclass the whole module builds by keyword; what a node would buy is
        exactly this check, and here it is. Without it a claim could carry
        ``matches_n`` alone and :attr:`sample_text` would have to choose
        between printing a numerator with no denominator and dropping a
        measured number in silence.

        ``newest`` is **not** part of the pair; see its own comment.

        Raises:
            ValueError: If exactly one of the two is given.
        """
        if (self.matches_n is None) != (self.matches_m is None):
            raise ValueError(
                "A claim's match sample needs both its numerator and its "
                f"denominator; it has matches_n={self.matches_n!r} and "
                f"matches_m={self.matches_m!r}. A fraction with one half "
                "missing is not a sample."
            )

    @property
    def sample_text(self) -> str:
        """The sample as the reader sees it, in one unit or in two.

        ``3/4 kierroksesta`` when the model has no match count for this row,
        ``3/4 kierroksesta, 2/4 ottelussa, ei uusimmassa`` when it has. The
        order is rounds, matches, recency: the round count is the honest
        sample size and stays where the reader's eye already is, and the mark
        comes last because it qualifies the pair rather than being a third
        count.

        **The match fraction is dropped when it is the same fraction**, and
        that is the product owner's decision of 2026-09-24 taken on the
        rendered pistol block: every entry there printed ``3/4 kierroksesta,
        3/4 ottelussa`` because a pistol round is one per map, so the second
        half was the first half again in another word. The heading states the
        block's match count, so nothing is lost -- and the line was 330
        characters where it had been 150, in a document read in the rush
        before a match.

        **The mark is not dropped with it, ever.** It is the one thing on the
        line that the round fraction cannot say: *"Outside on three rounds of
        four"* reads the same whether those three are the three oldest
        matches or the three newest, and that is the whole finding this story
        was written from. It is printed whenever the model knows it, including
        on blocks that print no fraction at all.
        """
        text = f"{self.n}/{self.m} {self.unit}"
        # Identical fractions, not identical numbers: (3, 4) and (3, 4) say
        # the same thing twice, while (2, 4) beside (3, 4) is the observation
        # the story exists for.
        if self.matches_n is not None and (
            (self.matches_n, self.matches_m) != (self.n, self.m)
        ):
            text += f", {self.matches_n}/{self.matches_m} {MATCH_SAMPLE_UNIT}"
        if self.newest is not None:
            mark = (
                RECENCY_NEWEST_INCLUDED
                if self.newest
                else RECENCY_NEWEST_ABSENT
            )
            text += f", {mark}"
        return text


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
    source directions are simultaneous only at one sample point, so a union
    across rounds -- or across two moments of one round -- would claim more
    simultaneous directions than were observed.

    ``rounds`` is already formatted strings and not :class:`Line` objects:
    they have no sample of their own, so :class:`Claim`'s contract ("a claim
    cannot be built without a sample") does not hold for them. The rows'
    sample is on the summary row they belong under.
    """

    rule: str
    line: Line
    rounds: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteView:
    """One pistol round's block: whose round it was, and the way they came.

    ``heading`` is the round's identity -- when, against whom, and how it
    ended -- and ``round_text`` the round number beside it. Two fields and
    not one, because the template sets the first in bold and the second
    outside it, which is the product owner's own shape.

    ``rows`` is **already-formatted Markdown list items, marker and
    indentation included**, for :attr:`AnomalyView.rounds`' reason and one
    of its own. The reason it shares: a row carries no sample of its own, so
    :class:`Claim`'s contract ("a claim cannot be built without a sample")
    does not hold for it. The reason of its own: the row's *nesting is its
    meaning* -- a division sits one list level under the point it divided at
    -- and a tuple of ``(depth, text)`` pairs would move the decision of how
    a level is spelled into the template, which is where it was in the first
    mock and where it was hardest to read.

    **A row is a real list item and not an indented line.** The rows once
    opened with ``-> ``, and a Markdown preview -- which is how the report
    is read -- discarded every space of their indentation and ran a whole
    block together into one line. Whoever changes this shape has to check it
    through a CommonMark renderer and not by eye: the indentation looks
    right in the raw file either way, and that is exactly why the defect
    survived to be shipped.

    ``note`` is filled in **instead of** ``rows`` when the round reached no
    sample point at all (:data:`ROUTE_NO_SAMPLE`). Exactly one of the two is
    ever set: a block with neither would be a bold heading with nothing under
    it, which is the shape :class:`SideView`'s note exists to prevent one
    level up.
    """

    heading: str
    round_text: str
    rows: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class RoundTypeView:
    """One round type's part on one map and side."""

    round_type: str
    heading: str
    rounds_text: str
    #: The win-loss record as the reader sees it, beside the round count on
    #: the same line. Already formatted, like ``rounds_text``: the record's
    #: three counts are the model's, and the sentence built from them is this
    #: layer's.
    record_text: str
    small_sample: bool
    pattern_only: bool
    lines: tuple[Line, ...]
    notes: tuple[str, ...] = ()
    #: One block per round of this type, newest match first -- empty on every
    #: type but the pistol (Story 4.11).
    #:
    #: **First in the block and before** :attr:`lines`, which is a content
    #: decision and the product owner's: his sketch puts ``date - opponent -
    #: reitti`` directly under the block's heading (the side, since Story
    #: 4.12, under its buy-class section), so the route is what the
    #: pistol block is *about* and the marginal distributions are what is
    #: known besides. Set after the rows, they would read as the block's
    #: findings and the route as a footnote to them.
    #:
    #: The default is empty only because the field was added to an existing
    #: class; :func:`build_view` fills it in always, from a list the model
    #: holds to being exactly as long as the block's sample.
    routes: tuple[RouteView, ...] = ()


@dataclass(frozen=True)
class SideView:
    """One side's round types.

    **Since Story 4.12 the side is not a section of the report.** Its
    round types are printed under the buy class they belong to
    (:class:`BuyClassView`), and the side itself is one row near the top of
    the map chapter: its heading and its sample. The round types stay here
    too, because a side is still what the model is divided by and what
    :class:`BuyClassView` regroups -- it points at these same objects and
    copies none of them.

    ``note`` is filled in when the side has no round type at all. A side row
    without content would look like an interrupted report; a named reason
    says that it was the data that ran out and not the formatting.
    """

    side: str
    heading: str
    rounds_text: str
    round_types: tuple[RoundTypeView, ...]
    note: str | None = None


@dataclass(frozen=True)
class BuyClassView:
    """One buy class on one map: the CT block, then the T block.

    **The report's order is map, then buy class, then side** -- the product
    owner, 2026-09-26: *"kartta --> side --> ct-pistol, t pistol --> ct
    force, t force --> ct eco, t eco --> ct half, t half... They do different
    things on different sides ofcourse."* Before Story 4.12 it was map, side,
    buy class.

    ``blocks`` pairs each block with the side it came from, **CT first**
    (:data:`SIDE_ORDER`), and holds the very :class:`RoundTypeView` objects of
    :attr:`SideView.round_types` -- a regrouping, not a second build, so a
    block cannot say one thing under its side and another under its buy
    class. A side that never played the class has no block here, and a class
    neither side played has no :class:`BuyClassView` at all: an empty block
    is not printed, exactly as before.
    """

    round_type: str
    heading: str
    blocks: tuple[tuple[SideView, RoundTypeView], ...]


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
    #: The label over the demo list, which states the order **only when the
    #: order is known** (:data:`PLAYED_MAPS_ORDERED`).
    played_maps_label: str = PLAYED_MAPS_ORDERED
    #: The map's demos as the reader sees them, one already-formatted string
    #: each: when it was played, against whom, and the id (Story 4.10).
    #:
    #: Formatted strings and not :class:`Line` objects, for
    #: :attr:`AnomalyView.rounds`' reason: a row here carries no sample of its
    #: own, so :class:`Claim`'s contract ("a claim cannot be built without a
    #: sample") does not hold for it. The sample is the map's heading.
    #:
    #: The default is empty only because the field was added to an existing
    #: class; :func:`build_view` fills it in always, because every map in the
    #: report has at least one demo -- the model refuses a map whose demo
    #: count and demo list disagree.
    played_maps: tuple[str, ...] = ()
    note: str | None = None
    #: The map's CT advances that recur across matches, one formatted row
    #: each (Story 4.5, :func:`_habit_views`). Empty when there are none.
    habits: tuple[str, ...] = ()
    #: How many one-match advances on this map were not raised, or ``None``.
    habits_note: str | None = None
    #: The map's round types grouped by buy class, in
    #: :data:`ROUND_TYPE_ORDER` (Story 4.12, :func:`_buy_class_views`). The
    #: default is empty only because the field was added to an existing
    #: class; :func:`build_view` fills it in always.
    buy_classes: tuple[BuyClassView, ...] = ()


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
    #: The text that is read **instead of** the anomaly rows when none is
    #: printed. :func:`build_view` fills it exactly when :attr:`anomalies` is
    #: empty. It is then the match rule's count of crunch rows left out if
    #: there were any, otherwise the coverage (:func:`_no_anomalies_text`),
    #: and in both cases followed by the pointer to the map chapters' habits
    #: when the report has CT advances (Story 4.5).
    anomalies_note: str | None = None
    #: The note under printed anomaly rows: the match rule's count of crunch
    #: rows left out, the row cap's count, and the habits pointer -- each only
    #: when it applies. ``None`` when :attr:`anomalies` is empty (the same
    #: text is then :attr:`anomalies_note`) or when none applies.
    anomalies_dropped_note: str | None = None
    #: The label over each map's habits block (:data:`HABITS_LABEL`). On the
    #: view rather than in the template so the template holds no report
    #: wording of its own.
    habits_label: str = HABITS_LABEL


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
    #: At least one map's demo list holds a demo the match index does not
    #: hold (Story 4.10). The reading guide then says what that means and,
    #: more to the point, that the report will not read an opponent out of a
    #: file name. Flagged and not unconditional, for the pruning flags'
    #: reason: on a report built entirely from FACEIT demos the sentence would
    #: explain a mark that is on no row.
    #:
    #: Raised in :func:`build_view` and not through :meth:`absorb`, because it
    #: is a property of the map chapter and not of a row that pruning can
    #: remove.
    unindexed_demo: bool = False
    #: At least one demo **is** in the match index and is missing the date or
    #: the opponent (:data:`MATCH_DATE_MISSING`, :data:`OPPONENT_MISSING`).
    #:
    #: **A second flag and not the one above, because the inverse is the one
    #: that costs the reader** (found in Story 4.10's edge-case review, live
    #: on the scouted team's own report). Both marks were explained in the
    #: note gated on :attr:`unindexed_demo`, which is raised only when some
    #: demo is **outside** the index -- so a report whose every demo is
    #: indexed could print :data:`MATCH_DATE_MISSING` with nothing in the
    #: guide defining it. That is not a corner: 35 of the archive's 66
    #: indexed matches carry no ``finished_at``, and
    #: :func:`~pappascout.stages.aggregate._opponent_name` returns ``None`` on
    #: several states it handles deliberately.
    #:
    #: The flag above says "explain a mark only where it appears"; this one is
    #: the same rule applied to the marks it did not cover.
    unknown_in_index: bool = False
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
    #: Rule 3 left these sample points unwritten, as the labels they would
    #: have carried (``"45"``). **Only whether it is empty is read** since
    #: Story 4.5: the reading guide says in words that the analysis samples
    #: more densely than the report prints, and a list of the hidden seconds
    #: would put the grid back on the page.
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
    #: At least one pistol round printed a **route row** (Story 4.11). The
    #: reading guide then explains how one is read -- the indentation, the
    #: arrow that is not adjacency, the difference between a death and a
    #: player the sample lost, and what the end of a row does *not* mean.
    #:
    #: Flagged and not unconditional, for :attr:`unindexed_demo`'s reason: on
    #: a report with no pistol block the paragraphs would explain rows that
    #: are nowhere in it.
    #:
    #: **Rows and not routes, and the difference is a live state rather than
    #: a nicety.** A round that reached no sample point is a real
    #: :class:`~pappascout.domain.report.RoundRoute` that renders as the
    #: :data:`ROUTE_NO_SAMPLE` note and no rows at all, so a block made only
    #: of those would have raised a flag counting routes and printed two
    #: paragraphs about an indentation the reader never meets. That is the
    #: exact failure this flag exists to prevent, one level in.
    #:
    #: Raised in :func:`build_view` and not through :meth:`absorb`, because
    #: the routes are not a row pruning can remove.
    routes_shown: bool = False

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

    **Rule 3 is the one exception, and Story 4.5 made it one.** Every other
    rule prunes: it drops a row the report could have written, and a
    protected round type is protected precisely from that. Rule 3 no longer
    does that job. ``[parse].snapshot_seconds`` is now a dense internal
    series that the rules read and the reader does not, and
    ``skip_sample_seconds`` is what holds the two apart -- so switching it
    off on a protected round type does not preserve a row, it prints the
    grid. Measured on this archive with the rule switched off here: 80
    sample-point rows in one report's pistol blocks and 52 in the other's,
    while every unprotected block in the same reports kept its four.

    The exception is therefore not a weakening of the protection but a
    recognition that one member of the list stopped being a pruning rule.
    The reading guide says so in its own words: rule 3's paragraph is the
    only one that does not carry :func:`_protected_round_types_text`.
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
        skipped = frozenset(
            seconds_label(value) for value in settings.skip_sample_seconds
        )
        if round_type in PROTECTED_ROUND_TYPES:
            # Rule 3 survives the protection; see the class docstring. It is
            # passed in rather than read inside :meth:`off`, so that "off"
            # keeps meaning off and the exception is visible at the one place
            # that knows about the protection.
            return cls.off(skipped_seconds=skipped)
        return cls(
            drop_saturated=settings.drop_saturated_equipment_lines,
            merge_equal=settings.merge_equal_equipment_lines,
            skipped_seconds=skipped,
            max_utility_targets=settings.max_utility_targets,
            max_kill_areas=settings.max_kill_areas,
        )

    @classmethod
    def off(cls, *, skipped_seconds: frozenset[str] = frozenset()) -> "_Pruning":
        """Pruning off: the report is the one that was there before Story
        2.13.

        Args:
            skipped_seconds: Rule 3, which is not pruning and therefore is
                not switched off with the rest. The default is the empty set,
                so the name still tells the truth on its own.
        """
        return cls(
            drop_saturated=False,
            merge_equal=False,
            skipped_seconds=skipped_seconds,
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


def record_text(record: RoundRecord) -> str:
    """The record as one fragment: ``voitettu 15-7``.

    When some outcome was not read, the unknown is named after the record and
    not inside it -- ``voitettu 15-7, 2 kierroksen tulos ei tiedossa``. Two
    separate claims: this many were won and lost, and this many were not
    known either way. Adding the unknown into the pair would make the two
    numbers stop being the wins and the losses.

    **The clause is left out when there is nothing to report.** A standing ``0
    kierroksen tulos ei tiedossa`` on every block would be a sentence about
    an absence, and there are hundreds of blocks.

    **And the pair is left out when nothing was won or lost**, so that a
    block whose every outcome is unknown reads ``3 kierroksen tulos ei
    tiedossa`` and not ``voitettu 0-0, 3 kierroksen tulos ei tiedossa``.
    ``0-0`` is the exact string :data:`~pappascout.domain.report
    .REPORT_SCHEMA_VERSION` refuses as a default, and for the reason that
    applies here word for word: it does not read as a missing measurement, it
    reads as a round type nobody won and nobody lost -- and it would sit in
    the position the eye lands on, repaired by a trailing clause. The shape
    is the report's own: ``ei omia kuolemia 3 kierroksella`` likewise
    replaces the figure instead of printing a zero beside it
    (:func:`_death_lines`).

    An **empty** record -- nothing won, nothing lost, nothing unknown --
    still reads ``voitettu 0-0``, and there the string is true: no round was
    won or lost because the group has no rounds. It cannot reach the report
    in any case, because ``aggregate`` writes no group without rounds.

    There is no rate here and there is not meant to be one (Story 4.8): the
    report states the count and the reader is the analyst.
    """
    if record.unknown and not (record.wins or record.losses):
        return f"{record.unknown} {RECORD_UNKNOWN_OUTCOME}"
    text = f"{RECORD_VERB} {record.wins}-{record.losses}"
    if record.unknown:
        text += f", {record.unknown} {RECORD_UNKNOWN_OUTCOME}"
    return text


def demos_text(count: int) -> str:
    """``1 demo`` / ``4 demoa``: one demo / four demos."""
    return "1 demo" if count == 1 else f"{count} demoa"


def matches_text(count: int) -> str:
    """``1 ottelussa`` / ``4 ottelussa``: in one match / in four matches.

    The **inessive**, unlike :func:`demos_text` and :func:`rounds_text` beside
    it, because of where the three are used. Those two name a quantity
    (``8 demoa``, ``157 kierrosta``); this one always follows one of them and
    says what that quantity is inside -- ``4 kierrosta 4 ottelussa``. In the
    nominative the two would read as a list of two separate counts, which is
    exactly the reading Story 4.9 exists to prevent: the rounds are **in** the
    matches. The case is the product owner's own
    (:data:`MATCH_SAMPLE_UNIT`).

    The same word as :data:`MATCH_SAMPLE_UNIT`, and deliberately so: a heading
    that said ``ottelua`` over rows that said ``ottelussa`` would look like
    two different measurements. The unit is one constant and this function
    only puts a number in front of it.
    """
    return f"{count} {MATCH_SAMPLE_UNIT}"


def played_maps_text(count: int) -> str:
    """``1 pelattu kartta`` / ``8 pelattua karttaa``: the summary's own unit.

    **The root of the report counts played maps and not matches**, and that is
    the product owner's decision of 2026-09-24: the count of played maps --
    eight of them -- is what he wants there, and it is enough. The summary had
    read ``8 demoa 4 ottelussa``, which is true, because every match of that
    sample was a ``best_of 2`` that played two maps; the distinction is simply
    not one he wants at the root. Below the root the headings keep both
    counts, where they are equal anyway on this archive.

    **It is the same number as** :func:`demos_text`'s, in the reader's word
    instead of the pipeline's: one demo is one played map. The two words are
    not merged, because ``demoa`` is still right where the report is talking
    about the **files** -- the map chapter's heading and the traceability
    chapter's ids -- and this one is right where it is talking about what the
    opponent played.

    **Awaiting the product owner's word**, as :data:`RECENCY_NEWEST_INCLUDED`
    is: the number is his, the wording around it is the implementation's.
    """
    return "1 pelattu kartta" if count == 1 else f"{count} pelattua karttaa"


def times_text(count: int) -> str:
    """``1 kerran`` / ``4 kertaa``: once / four times.

    The map pool's unit (Story 4.10). It counts the **same thing**
    :func:`played_maps_text` counts -- one demo is one played map -- and says
    it in the case the pool row needs: that row names a map first and the
    number answers "how often", where the summary's own row names the number
    first and answers "how many". ``de_nuke 4 pelattua karttaa`` would read as
    four maps called ``de_nuke``.

    **Awaiting the product owner's word**, as :data:`PLAYED_MAPS_ORDERED` is.
    """
    return "1 kerran" if count == 1 else f"{count} kertaa"


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

    The name stays here, because this module calls it in **nine** places and
    not one of them is a settings match. (The count said "twenty" until
    2026-09-25, when it was counted rather than recalled; Story 4.11 added
    the ninth.)
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


def block_min_rounds(threshold: int | None, rounds: int) -> int | None:
    """The repetition threshold **this block** uses: the report's, capped.

    The report-wide threshold (:func:`pattern_min_rounds`) says how many
    rounds a repetition needs. A block of two rounds cannot hold three of
    anything, so applied as it is the threshold would not select patterns
    there -- it would delete the block. The cap is therefore the block's own
    round count: a two-round block keeps what happened on **both** rounds, a
    four-round block still demands three, and a 62-round block is untouched.

    **A cap and not a proportion.** "At least half the rounds" was the first
    proposal and it is the wrong shape: at 62 rounds it would demand 31 and
    gut the one block that actually holds the data. The requirement was to
    shorten the small blocks, and the cap is the rule that does only that --
    it is inert wherever ``rounds >= threshold``, which is every block the
    threshold was written for.

    **The number is computed and not configured.** Both inputs already
    exist: the threshold is read from the report (``view.py:19``) and the
    round count is the block's own ``sample.rounds``. A setting of its own
    would be a third source for a number the report already states twice.

    Args:
        threshold: The report's threshold, or ``None`` if it had none.
        rounds: The block's round count.

    Returns:
        ``None`` when there was no threshold -- then nothing is filtered and
        the block says so. Otherwise at least ``1``: a floor is needed
        because a block can have ``0`` rounds, and a threshold of ``0`` would
        be a claim that the block filtered at a limit no observation can
        even be measured against. At ``1`` the filter demands nothing, which
        is the truth about a one-round block, and the caller writes no note
        about a rule that cannot drop anything.
    """
    if threshold is None:
        return None
    return max(1, min(threshold, rounds))


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


#: Whether a block's claims carry a match **fraction**.
#:
#: **A block of one match says so in its heading, and every fraction in it
#: would be ``1/1``**: the sample point's matches are a subset of that one
#: match. Two of the three teams in the developer's archive are entirely
#: hand-imported demos, one map per match, so this is not a corner -- without
#: the rule their whole report gains a tautology on every claim.
#:
#: **The recency mark has its own rule one level up**
#: (:func:`_map_states_recency`), because since the mark became the **map's**
#: a one-match *block* still says something: whether its single match is the
#: map's most recent. Only a one-match **map** makes it a tautology.
#:
#: The rule is **selection and not computation** (AD-10): the number comes
#: from the model, and what is chosen here is which observations earn a place
#: on the line -- the same kind of choice as the pattern threshold.
def _block_states_matches(report_type: RoundTypeReport) -> bool:
    return report_type.sample.matches > 1


#: Whether a map's claims carry a **recency mark**.
#:
#: **A map of one match has nothing to mark.** Every observation on it is in
#: that match, and that match is by definition the map's most recent, so the
#: mark would read *the newest is among them* on every line of the chapter --
#: and the chapter heading already says ``1 ottelussa``.
#:
#: **Measured, and it is why this rule is at the map level and not the
#: block's** (2026-09-24, all three teams of the developer's archive
#: rendered). With the rule at the block level the two hand-imported teams,
#: whose maps hold one demo each, printed the mark 304 and 197 times against
#: 2 and 2 of the other form: a mark that is effectively always the same word
#: is one the reader learns to skip, and that cost falls on the maps where it
#: does discriminate. With the rule here, those chapters carry no mark at
#: all, and the scouted team -- whose maps hold three and four matches --
#: keeps its 326 against 155.
#:
#: The **fraction**'s rule stays at the block level
#: (:func:`_block_states_matches`), because a fraction's denominator is the
#: sample point's matches and that is a property of the block.
#:
#: The reading guide names both reasons a mark can be missing -- one match on
#: the map, or an order that is not known -- because a reader cannot tell them
#: apart from the line. The first is visible in the map's heading; the second
#: is not.
def _map_states_recency(map_report: MapReport) -> bool:
    return map_report.sample.matches > 1


def _position_line(
    position: Position,
    min_n: int,
    flags: _Flags,
    *,
    matches: bool,
    recency: bool,
) -> Line | None:
    """One sample point's row: the areas and their player counts.

    ``matches`` is :func:`_block_states_matches` and ``recency`` is
    :func:`_map_states_recency`; when either is false that half of the match
    sample is left off the claims. They are two flags and not one because
    they are decided at two levels, and those functions say why.
    """
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
                    Claim(
                        text=f"{name} {bar.players}",
                        n=bar.n,
                        m=area.m,
                        matches_n=bar.matches if matches else None,
                        matches_m=area.matches_m if matches else None,
                        newest=bar.newest if recency else None,
                    ),
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
    report_type: RoundTypeReport,
    min_n: int,
    flags: _Flags,
    *,
    matches: bool,
    recency: bool,
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
        claims.append(
            (
                -entry.n,
                name,
                Claim(
                    text=name,
                    n=entry.n,
                    m=entry.m,
                    matches_n=entry.matches if matches else None,
                    matches_m=entry.matches_m if matches else None,
                    newest=entry.newest if recency else None,
                ),
            )
        )
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
    * An equipment row can also be left unwritten **because of the
      threshold** (two bars of two rounds each are not a pattern in a
      four-round block). Rule 1 then removed nothing, and it must not say in
      the reading guide that it did.

      **A saturated row is no longer one of those**, and that follows from
      the cap (2026-09-11): saturation means one bar carrying every round of
      the observation (``n = m``), and the threshold is capped at the
      block's round count, so a saturated row always clears it. Before the
      cap, a two-round block filtered at 3 and dropped the row first. The
      order between the two mechanisms is therefore no longer observable on
      *this* rule -- it still is on rule 3, and that is where it is watched.

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
    pattern filtering drops a row whose bars do not repeat often enough.
    That is why this is asked only of a row that was really built
    (:func:`_equipment_rows`) -- even though a *saturated* row now always
    survives the threshold, because ``n = m`` and the threshold is capped at
    the block's round count. The guard is kept because it is about the order
    of the two mechanisms and not about this one distribution's arithmetic.
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
    recency: bool,
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
    # Decided once for the whole block, because it is a property of the block
    # and not of any row: see :func:`_block_states_matches`.
    states_matches = _block_states_matches(report_type)

    for position in report_type.positions:
        if pruning.skips(position):
            # **Not built at all** (Story 4.5). A hidden sample point is the
            # analysis's grid and not a row the report chose to drop, so it
            # takes part in nothing the reader sees: its observations are not
            # counted as "harvinaisempaa" in the block's note, and it cannot
            # come back through the empty-block return below -- which it did
            # once, printing ``42 s`` in a report that shows four seconds.
            #
            # The row is built into a **throwaway** bookkeeping only to ask
            # whether it would have existed: the reading guide explains rule
            # 3 only when it hid a row, and a point whose every bar the
            # threshold drops hid nothing.
            label = _sample_key(position)
            would_print = (
                _position_line(
                    position,
                    min_n,
                    _Flags(),
                    matches=states_matches,
                    recency=recency,
                )
                is not None
            )
            if would_print and label is not None and label not in skipped_samples:
                skipped_samples.append(label)
            continue
        scratch = _Flags()
        line = _position_line(
            position, min_n, scratch, matches=states_matches, recency=recency
        )
        if line is None:
            # The row never came about, so pruning has nothing to say about
            # it. The threshold's bookkeeping transfers all the same.
            flags.absorb(scratch, keep=False)
            continue
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

    gap = _first_contact_gap_line(
        report_type, min_n, flags, matches=states_matches, recency=recency
    )
    if gap is not None:
        keep_all([gap])

    keep_all(_death_lines(report_type.deaths, min_n, flags, pruning))

    kept = [row.kept for row in rows if row.kept is not None]
    if rows and not kept and saturated_dropped:
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


def _repetition_requirement(threshold: int, rounds: int) -> str:
    """How often a pattern had to repeat, in the block's own words.

    ``kaikilla N kierroksella`` when the cap bit -- the requirement is then
    every round of the block -- and ``vähintään N kierroksella`` otherwise.
    Two wordings and not one, because the number on its own does not say
    which of the two the block did: in a three-round block "vähintään 3" and
    "kaikilla 3" are the same requirement, and only the second tells the
    reader that the block could not have demanded more. The two are the same
    length, so the distinction costs the report nothing.
    """
    if threshold >= rounds:
        return f"kaikilla {threshold} kierroksella"
    return f"vähintään {threshold} kierroksella"


def _route_step_text(step: RouteStep, flags: _Flags) -> str:
    """One group at one moment, as the row reads it.

    Three forms for :data:`~pappascout.domain.report.RouteFate`'s three
    values, and they are three because the story exists to keep three claims
    apart:

    ``seen``
        ``4 Control`` -- this many players were observed there. An area the
        game gave no name to reads :data:`UNKNOWN_AREA`, exactly as every
        other area row in the report does: it is an **observed position the
        map does not name**, not a player who went missing.
    ``died``
        ``1 kuoli Mini (27 s)`` -- the place and the moment, both from
        ``deaths.parquet``. The moment is the death's own and not the sample
        point's, which is why the seconds are worth printing at all.

        **Whole seconds**, as the approved form writes them, and **rounded
        and not truncated**: the archive's own values are ``t_s`` to four
        decimals (21.9531), and truncating that to 21 states a second that
        did not happen when 22 is the same number of characters. The
        rounding goes through :func:`_seconds` rather than a format string
        of its own, so the decimal mark stays the one the rest of the report
        uses. One decimal, as the first-death median carries, would claim a
        precision the reader has no use for on a single round.

        **``round`` is half-to-even**, so 26.5 s prints ``26`` and 27.5 s
        prints ``28``. That is stated rather than fixed: the argument above
        is about a whole second of error and this is half of one, the
        archive's ``t_s`` is tick arithmetic that never lands on an exact
        half, and a hand-rolled half-up rounder would be a second spelling
        of a number the report already knows how to print.
    ``gone``
        ``1 poistui otannasta`` -- the sample point exists, the player has no
        row on it, and nothing accounts for them. No area, because there is
        none to state.

    An unnamed area raises :attr:`_Flags.unknown_area` on **both** of the
    first two forms and on neither's account in particular: the game names no
    place for a death either (``victim_area`` is nullable), and the reading
    guide's one paragraph covers the mark wherever it is printed.
    """
    return f"{step.players} {_route_step_label(step, flags)}"


def _route_step_label(step: RouteStep, flags: _Flags) -> str:
    """The same text **without the player count** in front of it.

    Split out for :func:`_route_parts`, which merges sibling leaves whose
    text is identical: the merge adds the counts up, so it has to compare
    what is left after the count. Nothing else should call it -- a label on
    its own is a group with no size, which is not a claim this report makes.
    """
    if step.fate == "gone":
        return ROUTE_GONE
    if step.area is None:
        flags.unknown_area = True
    if step.fate == "died":
        return (
            f"{ROUTE_DIED} {_area(step.area)} "
            f"({_seconds(round(step.seconds))} s)"
        )
    return _area(step.area)


def _route_parts(
    steps: Sequence[RouteStep], flags: _Flags
) -> list[tuple[str, RouteStep | None]]:
    """One part per row of a division, **leaves that print alike merged**.

    Two deaths in one area at 26.4 s and 26.49 s are two observations in the
    model and print the same sentence, so as two parts the reader is shown
    ``1 kuoli Mini (26 s)`` twice and has to guess whether that is one death
    written twice or two. Merged, it is ``2 kuoli Mini (26 s)``, which is
    what the same two deaths at an identical moment have always produced --
    ``aggregate`` groups those into one step (:func:`~pappascout.domain
    .aggregate._route_steps`), and until this merge existed the rendering
    disagreed with itself either side of a rounding boundary.

    **The merge is here and not in ``domain``**, and that boundary is the
    point: whether two moments print alike is a question about the spelling
    of a second, and the model must not know how anything is spelled. It
    keeps the measured moments; this layer merges what it has just decided
    to show as the same.

    **Only leaves merge.** A part that still has steps carries a branch of
    its own, so two of them are never the same row however their heads read.

    Returns:
        ``(text, step)`` per part in order, the step being ``None`` for a
        merged leaf -- there is no single step such a part came from, and
        the caller has nothing left to follow.
    """
    parts: list[tuple[int, str, RouteStep | None]] = []
    for step in steps:
        label = _route_step_label(step, flags)
        if step.steps:
            parts.append((step.players, label, step))
            continue
        for index, (count, seen, owner) in enumerate(parts):
            if owner is None and seen == label:
                parts[index] = (count + step.players, seen, None)
                break
        else:
            parts.append((step.players, label, None))
    return [(f"{count} {label}", owner) for count, label, owner in parts]


def _route_chain(
    steps: Sequence[RouteStep], depth: int, flags: _Flags
) -> list[tuple[int, str]]:
    """Follow a group until it divides, then give the parts rows of their own.

    **The shared stretch is written once.** That is the product owner's
    decision of 2026-09-25 and the one he arrived at by rejecting two
    renderings: the first gave every leaf a full row and repeated
    ``4 OutsideTunnel -> 4 UpperTunnel`` four times, and he asked why it
    repeated; the second indented and repeated it still.

    So the three shapes, and there are only three:

    * **A group that stays together grows the row.** One part -> its text
      joins the chain with an arrow and the walk goes on into that part.
      This holds whatever that part's fate is: a single death ends the row
      it is on (``1 Mini -> 1 kuoli Mini (36 s)``), and a single ``gone``
      that the sample finds again carries on through it.
    * **A division that goes no further is read inline**, on the row it
      divided on: ``4 Control -> 2 Ramp, 1 Heaven, 1 Hell``. "No further"
      means not one part carries a step after this moment.
    * **A division whose parts move on ends the row at the division point**,
      and every part gets a row of its own one level in. **No part is
      spliced onto the parent's row.**

    **The splice is what this function used to do, and it put the seconds
    backwards on the page.** The first part's chain was joined to the shared
    row and the remaining parts were shifted underneath it, so on the
    archive's 2026-09-06 ``de_nuke`` T pistol round ``1 kuoli Mini (26 s)``
    sat indented beneath a row ending ``1 kuoli Mini (36 s)`` -- reading as
    something that happened *after* a death ten seconds later, when the two
    are siblings of one division at the 30 s point. Every part now sits at
    the same level as its siblings, because that is what they are. The
    ``shift`` arithmetic that computed the old offsets is gone with it.

    Returns:
        ``(depth, text)`` pairs, the depth in **list nesting levels**.
        :func:`_route_rows` turns them into list items; this function owns
        the shape and not the markers.
    """
    segments: list[str] = []
    while steps:
        if len(steps) == 1:
            segments.append(_route_step_text(steps[0], flags))
            steps = steps[0].steps
            continue
        parts = _route_parts(steps, flags)
        if all(owner is None for _, owner in parts):
            segments.append(", ".join(text for text, _ in parts))
            break
        rows = [(depth, f" {ROUTE_ARROW} ".join(segments))] if segments else []
        # The parts sit one level under the row that carried the shared
        # stretch -- and at the same level as it when there was none, which
        # is a group that divided at the very first moment it was followed to.
        below = depth + 1 if segments else depth
        for text, owner in parts:
            if owner is None:
                rows.append((below, text))
            else:
                rows.extend(_route_chain([owner], below, flags))
        return rows
    return [(depth, f" {ROUTE_ARROW} ".join(segments))] if segments else []


def _route_rows(route: RoundRoute, flags: _Flags) -> tuple[str, ...]:
    """One round's route as the lines the template sets.

    The first moment is a bullet of its own for every starting group -- the
    product owner's shape, and the reason is that a side does not always
    leave from one place. Measured 2026-09-25 on the rendered archive: all
    three of ``de_dust2`` CT pistol's rounds start from **four** places,
    against two on ``de_nuke`` T pistol, because a defence spreads to hold
    positions and does not travel as a body. The block is therefore several
    short rows, and **that is the finding** rather than a rendering fault
    (Story 4.11's frozen Always, where it is a reading note since both sides
    were asked for). Both of those blocks are pinned whole in
    ``tests/data/pistol_routes.json``, so the claim fails there if it stops
    being true.

    Everything after the first moment is a chain under its start
    (:func:`_route_chain`), and every row is a **real Markdown list item**
    nested under the row above it.

    **That is a correction and the reason is measured.** The rows used to
    open with ``-> ``, which is not a list marker, so in a Markdown preview
    -- which is how the product owner reads the report -- CommonMark took
    them as lazy continuations of the paragraph in the list item above and
    **discarded every space of the indentation**. Rendered in a browser the
    newlines became spaces and a whole block collapsed into one line: a
    reader saw ``4 Outside -> 2 Outside -> 1 Mini -> ... -> 1 Vending ...``
    as a single eleven-step chain, which is exactly the "one group travelled
    all of this" misreading this story exists to prevent. Verified both
    ways through ``markdown-it-py``: with list markers the nesting survives
    as nested ``<ul>``.

    So the indentation is **two spaces per level**, which is what puts each
    marker at or past the content column of the item above it, and the row
    depth is a list level rather than a decoration.
    """
    rows: list[str] = []
    for start in route.steps:
        rows.append(f"{ROUTE_INDENT}- {_route_step_text(start, flags)}")
        for depth, text in _route_chain(start.steps, 0, flags):
            rows.append(f"{ROUTE_INDENT * (depth + 2)}- {text}")
    return tuple(rows)


def _route_heading(route: RoundRoute, played: PlayedMap | None) -> str:
    """When the round was played, against whom, and how it ended.

    **The date and the opponent are looked up and not stored on the route.**
    They belong to the demo, one row per demo in :attr:`MapReport
    .played_maps`, and a copy on every pistol round would be the same date in
    ``report.json`` once per round with nothing holding the copies together.

    The absences are written out rather than dropped, exactly as
    :func:`_played_map_line` writes them: a row that simply began with the
    opponent would read as a match played on no day.

    **The opponent is introduced by name of its field** (``vastustaja X``,
    :data:`OPPONENT_PREFIX`), as the map's own row does. The approved form's
    example wrote it bare, and that left the report labelling only the
    absence: a **known** opponent appeared unlabelled beside a date while an
    unknown one read ``vastustaja ei tiedossa``, which is the labelling
    backwards. The prefix's own reason applies here unchanged -- the name is
    free text standing next to a date, and a team named after a date would
    be unreadable without it.

    **``played`` is ``None`` when the map's own demo list does not hold this
    route's demo, and the row then carries the id and says nothing else.**
    ``aggregate`` cannot produce that state -- a map's routes are built from
    the map's own rows -- so it is a hand-edited ``report.json`` and the id
    is the only thing about it that is certainly true.
    :data:`MATCH_NOT_INDEXED` would be **worse than useless** there: it is a
    claim about the archive's match index, and the demo may well be in it,
    listed under another map.
    """
    if played is None:
        parts = [_identifier(route.map_demo_id)]
    elif not played.indexed:
        parts = [MATCH_NOT_INDEXED]
    else:
        parts = [
            played.played_on.isoformat()
            if played.played_on is not None
            else MATCH_DATE_MISSING,
            f"{OPPONENT_PREFIX} {markdown_text(played.opponent)}"
            if played.opponent is not None
            else OPPONENT_MISSING,
        ]
    if route.won is None:
        outcome = RECORD_UNKNOWN_OUTCOME
    else:
        outcome = ROUND_WON if route.won else ROUND_LOST
    return f"{', '.join(parts)} -- {outcome}"


def _route_round_text(route: RoundRoute, played: PlayedMap | None) -> str:
    """The round number beside the heading, and the demo id where it is the
    row's only identifier.

    ``(kierros 13)`` ordinarily. For a demo the match index does not hold,
    ``(kierros 13, `Nuke_vs_hand`)``: such a row has no date and no
    opponent, so without the id **two unindexed demos of one map whose
    pistol rounds share a number and an outcome render as the same row** --
    the confusion :meth:`~pappascout.domain.report.RoundTypeReport
    ._check_routes_are_the_round_types_own_rounds` refuses one layer up, let
    through by the rendering.

    It mirrors :func:`_played_map_line`, which carries the id on **every**
    row for the same reason and calls it that row's only identifier for a
    hand-imported demo. Here it is carried only where it is needed: an
    indexed row is already told apart by its date and its opponent, and an
    id on it would be the traceability chapter's work done twice in the
    body.

    A code span, so the id survives copying byte for byte, and **outside the
    bold**: it is an identifier and not part of the claim.
    """
    text = f"{ROUND_LABEL} {route.round_no}"
    if played is not None and not played.indexed:
        return f"{text}, {_identifier(route.map_demo_id)}"
    return text


def _route_views(
    report_type: RoundTypeReport,
    played_maps: Mapping[str, PlayedMap],
    flags: _Flags,
) -> tuple[RouteView, ...]:
    """Every round's block, in the order the model holds them: newest first.

    **The order is not re-sorted here.** ``aggregate`` built it from the same
    list the map chapter prints (:func:`~pappascout.domain.aggregate
    .routes_for`), so a second sort in this layer could only ever come to
    disagree with the list a few lines above it in the report.

    A round that reached no sample point gets the note instead of rows -- see
    :class:`RouteView`.
    """
    views: list[RouteView] = []
    for route in report_type.routes:
        rows = _route_rows(route, flags)
        played = played_maps.get(route.map_demo_id)
        views.append(
            RouteView(
                heading=_route_heading(route, played),
                round_text=_route_round_text(route, played),
                rows=rows,
                note=None if rows else ROUTE_NO_SAMPLE,
            )
        )
    return tuple(views)


def _round_type_view(
    report_type: RoundTypeReport,
    threshold: int | None,
    flags: _Flags,
    settings: ReportSettings,
    recency: bool,
    played_maps: Mapping[str, PlayedMap],
) -> RoundTypeView:
    """Assemble one round type's rows.

    **The threshold this block filters at is not the one handed in.** The
    argument is the whole report's threshold; the block caps it at its own
    round count (:func:`block_min_rounds`), and every sentence the block
    writes about filtering states the capped number. The two are the same
    in every block large enough for the report's threshold to mean
    something, and they differ exactly where they must.

    Args:
        report_type: The round type's observations from the report.
        threshold: The report's repetition threshold, or ``None`` if there
            was none. Uncapped -- see above.
        flags: The report-wide collector. The explanations (unknown area,
            estimate, armed) are written **once** at the end of the report,
            so they have to be collected across all the round types and not
            within one.
        settings: The pruning rules (Story 2.13). The section is handed in
            whole, because a protected round type is resolved here -- see
            :meth:`_Pruning.for_round_type`.
    """
    filtered = report_type.round_type in PATTERN_ROUND_TYPES
    rounds = report_type.sample.rounds
    dropped_before = flags.dropped
    # On a protected round type every observation is written (min_n = 1); on
    # every other type only the repeating ones. The threshold comes from the
    # report, not from here, and the cap comes from this block's own round
    # count (:func:`block_min_rounds`).
    block_threshold = block_min_rounds(threshold, rounds) if filtered else None
    min_n = block_threshold if block_threshold is not None else 1
    # **The label and the note are about a rule that can really drop
    # something.** At a capped threshold of 1 -- a one-round block -- the
    # filter demands nothing: every observation in it was made on every
    # round of it. Marking the block "vain toistuvat kuviot" would then
    # claim a selection that was not made, and the block's round count,
    # which is in its heading, already says why.
    pattern_only = filtered and (block_threshold is None or block_threshold > 1)

    pruning = _Pruning.for_round_type(settings, report_type.round_type)
    lines, kept_the_block = _round_type_lines(
        report_type, min_n, flags, pruning, recency
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
        # **The number is the one this block filtered at**, not the report's
        # uncapped threshold: with the cap they differ per block, and a
        # block that said "3" while filtering at 2 would be a false claim
        # about the data. When the cap bit, the requirement is every round
        # of the block, and the wording says so in the same breath and in
        # the same number of words (:func:`_repetition_requirement`).
        note = (
            "Vain kuviot, jotka toistuvat "
            + _repetition_requirement(block_threshold, rounds)
        )
        note += (
            f"; {dropped} harvinaisempaa havaintoa jäi pois."
            if dropped
            else "; jokainen havainto ylitti kynnyksen."
        )
        notes.append(note)
    # On a protected round type -- and on a block the cap took down to 1 --
    # the threshold is 1, and no bar of a distribution can fall below it:
    # the model demands ``n > 0`` from every one. Telling of filtering would
    # therefore be a claim about a threshold that never applied -- which is
    # why there is no third branch here. The counter is protected at the
    # source: ``flags.dropped`` grows only when ``min_n > 1``.
    if not lines:
        # Three different things, and one sentence for all of them would
        # hide the difference: the cap took everything (the sample is too
        # small for anything to repeat), the report-wide threshold took
        # everything, or there were no observations in the first place.
        #
        # **The third is told apart by ``dropped`` and not by the round
        # type.** Until 2026-09-11 the branch asked whether the type was
        # filtered at all, which answered a different question: a ``full``
        # block with no observations was made to say that nothing passed the
        # threshold, when nothing had been offered to it. ``dropped`` counts
        # what the threshold really removed, so it is the one that knows.
        if not dropped:
            notes.append("Ei havaintoja tältä kierrostyypiltä.")
        elif block_threshold is not None and block_threshold >= rounds:
            notes.append(
                "Yksikään havainto ei toistunut "
                f"{_repetition_requirement(block_threshold, rounds)}: otanta "
                "on liian pieni, jotta mikään ehtisi toistua. Havainnot ovat "
                "report.jsonissa."
            )
        else:
            notes.append("Ei kuvioita, jotka ylittäisivät kynnyksen.")
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
        rounds_text=(
            f"{rounds_text(report_type.sample.rounds)} "
            f"{matches_text(report_type.sample.matches)}"
        ),
        record_text=record_text(report_type.record),
        small_sample=report_type.small_sample,
        pattern_only=pattern_only,
        lines=tuple(lines),
        notes=tuple(notes),
        routes=_route_views(report_type, played_maps, flags),
    )


def _one_match_text(minimum: int) -> str:
    """``vain yhdessä ottelussa`` at the shipped two; ``alle N ottelussa``
    otherwise."""
    return "vain yhdessä ottelussa" if minimum == 2 else f"alle {minimum} ottelussa"


def _crunch_rule_note(left_out: int, minimum: int) -> str:
    """How many crunch rows the match rule did not print.

    The count is said out loud for the reason every pruning rule says what
    it left out: an absent row must not read as an absent observation.
    **Awaiting the product owner's wording** (Story 4.5).
    """
    one = left_out == 1
    rows = "crunch-rivi jäi" if one else "crunch-riviä jäi"
    subject = "se" if one else "ne"
    return (
        f"{left_out} {rows} pois, koska {subject} havaittiin "
        f"{_one_match_text(minimum)}: crunch nostetaan esiin vasta, kun sama "
        f"toistuu vähintään {minimum} ottelussa. Kaikki ovat report.jsonissa "
        "kentässä anomalies. Asetus: [report].anomaly_min_matches."
    )


def _habits_note(left_out: int, minimum: int) -> str:
    """How many of a map's advance rows were not raised, and why.

    **The unit is the row, and a row is one T area** (per map, across the
    save round types), so the count is of areas -- not of observations,
    which a row holds several of. **Awaiting the product owner's wording**
    (Story 4.5). The reason is his: a single brief visit is usually a
    reaction to a sound or a kill.
    """
    one = left_out == 1
    what = "T:n alue" if one else "T:n aluetta"
    relative = "jolla" if one else "joilla"
    return (
        f"{left_out} {what}, {relative} CT-pelaaja nähtiin "
        f"{_one_match_text(minimum)}, jäi nostamatta -- yksittäinen käynti on "
        "usein reaktio ääneen tai tappoon. Kaikki ovat report.jsonissa "
        "kentässä anomalies. Asetus: [report].anomaly_min_matches."
    )


def _habit_players_text(anomaly: Anomaly) -> str:
    """Who, with the verb: ``CT-pelaaja puskee``, ``2 CT-pelaajaa puskee``,
    or ``jopa 3 CT-pelaajaa puskee``.

    **The peak :func:`_anomaly_points_text` states, read over every sample
    point of every round**: when all of them saw the same count, that count
    is the observation; when any two differ, the largest is stated as a
    peak. Read over the rounds' maxima instead, a round seen at 3, 4, 5, 5,
    4, 2, 2 and 1 players would print a flat "5" -- a peak stated as a fact.

    **A peak is ``jopa``, not ``enintään``** (the product owner, 2026-09-26,
    quoted verbatim in the Story 4.12 spec: the old row was a bad translation
    of *up to 4 CT players*). ``enintään`` reads as a cap; the number is the
    most players seen, and ``jopa`` says that. One player seen
    every time is not a peak and takes no ``jopa``. **Awaiting his word.**

    **Why ``puskee`` is allowed here** (Story 4.12). This row reports a CT
    player **inside ground the T side holds, on a save round** -- the two
    conditions :func:`~pappascout.domain.sampling.ct_advance_hits` fires on.
    Being there *is* having pushed: the observation contains the movement.
    At ``[report].anomaly_min_matches`` 2 (the shipped setting) the row is
    raised only when it recurs across matches, which is what makes it a
    habit; with the rule off, a one-match visit prints with the same verb.

    **Why it stays off the two other rows**, and the reasons differ:

    * **stack** -- a concentration on the CTs' **own** site. The project
      refuted, twice, a rule that tried to tell such a concentration apart
      as waiting or pushing; it cannot be told, so the row names neither.
    * **crunch** -- also on T-held ground, so "being there is having pushed"
      would fit it too, and that is **not** the reason. The crunch already
      has its own name, the product owner's word for that strategy, and a
      habit is not a strategy (his decision, 2026-09-25): the habit's verb
      does not belong on a strategy's row.

    A test pins the verb here and its absence from both.
    """
    counts = {point.players for entry in anomaly.rounds for point in entry.points}
    if len(counts) > 1:
        return f"jopa {max(counts)} CT-pelaajaa puskee"
    count = counts.pop()
    return "CT-pelaaja puskee" if count == 1 else f"{count} CT-pelaajaa puskee"


def _habit_buy_types_text(anomaly: Anomaly) -> str:
    """How many times, and on which buy types: ``2 kertaa: force 1, eco 1``,
    or ``kerran: eco 1``.

    **Read from the row's own rounds and from nothing else** (Story 4.12):
    the product owner's example sentence said *"ecoilla ja puoliostoilla"*
    where the data held an eco and a force, and the row says what the data
    says. The count is of those same rounds, so the per-type numbers always
    add up to it. The types are in :data:`ROUND_TYPE_ORDER` and named by
    :data:`~pappascout.constants.ROUND_TYPE_FI`, the same names the round
    list at the end of the row uses. **Awaiting his word.**
    """
    per_type = Counter(entry.round_type for entry in anomaly.rounds)
    types = ", ".join(
        f"{ROUND_TYPE_FI.get(name, name)} {per_type[name]}"
        for name in sorted(per_type, key=lambda name: (_round_type_rank(name), name))
    )
    # "kerran" for one, not the map pool's "1 kerran" (:func:`times_text`):
    # there the number leads the row, here it would read wrongly.
    count = len(anomaly.rounds)
    times = "kerran" if count == 1 else f"{count} kertaa"
    return f"{times}: {types}"


def _habit_text(
    anomaly: Anomaly, played: Mapping[str, PlayedMap], matches_m: int | None
) -> str:
    """One habit, said in words: what they do, how often, on which buys --
    and its sample.

    **Worded as a habit and never as a strategy** (the product owner,
    2026-09-25): no pattern is named and nothing is said about a plan. Since
    Story 4.12 the row states the one interpretation the observation carries
    -- the CTs push into that area (:func:`_habit_players_text` says why
    that word is allowed here) -- then how many times and on which buy types
    (:func:`_habit_buy_types_text`). It reads ``jopa 4 CT-pelaajaa puskee
    alueelle Lobby säästökierroksilla, 2 kertaa: force 1, eco 1 (2/8
    säästökierroksesta, 2/4 ottelussa; eco k14 2026-09-20, force k19
    2026-09-13)``. **Awaiting his wording.**

    **The area name stays the game's own** (AD-10) and the sentence is built
    so it needs no Finnish case ending: ``alueelle Lobby``, never
    ``Lobbyyn``.

    **It carries its sample like every claim in the report** (the reading
    guide's first promise), built through :class:`Claim`: the rounds over the
    side's save rounds on the map, and the matches over the map's matches.

    Each round carries its match's date so the scout knows which demo to
    open: round numbers are per demo, and at the shipped threshold a habit
    spans at least two matches (with the rule off it may span one).
    Without a date the demo id stands in, as elsewhere. The rounds are in
    **the map's own list order** -- newest match first, the order the
    played-maps list directly above prints -- so the two cannot disagree.
    """
    place = {demo: index for index, demo in enumerate(played)}
    ordered = sorted(
        anomaly.rounds,
        key=lambda entry: (
            place.get(entry.map_demo_id, len(place)),
            entry.map_demo_id,
            entry.round_no,
        ),
    )
    rounds = []
    for entry in ordered:
        match = played.get(entry.map_demo_id)
        when = (
            match.played_on.isoformat()
            if match is not None and match.played_on is not None
            else _identifier(entry.map_demo_id)
        )
        name = ROUND_TYPE_FI.get(entry.round_type, entry.round_type)
        rounds.append(f"{name} k{entry.round_no} {when}")
    claim = Claim(
        text=_area(anomaly.area),
        n=anomaly.n,
        m=anomaly.m,
        unit=HABIT_SAMPLE_UNIT,
        matches_n=anomaly.matches if matches_m is not None else None,
        matches_m=matches_m,
    )
    return (
        f"{_habit_players_text(anomaly)} alueelle {claim.text} "
        f"säästökierroksilla, {_habit_buy_types_text(anomaly)} "
        f"({claim.sample_text}; {', '.join(rounds)})"
    )


def _habit_views(
    report: Report, map_report: MapReport, settings: ReportSettings
) -> tuple[tuple[str, ...], str | None]:
    """The map's CT advances, as habits, and the count of those not raised.

    **Per map and not in the anomaly chapter** (Story 4.5). The product owner
    separated them: an advance is a habit and a crunch or a stack is a
    strategy. The block sits in the map chapter, under the played-maps list
    and above the buy-class sections (Story 4.12): a habit row spans the
    save round types, so it belongs to the map and to no single buy class.

    The match rule reads the row grouped per map and area across the save
    round types (:attr:`~pappascout.domain.report.Anomaly.matches`).
    """
    played = {entry.map_demo_id: entry for entry in map_report.played_maps}
    advances = [
        entry
        for entry in report.anomalies
        if entry.rule == "ct_advance" and entry.map_name == map_report.map_name
    ]
    minimum = settings.anomaly_min_matches
    raised = sorted(
        (entry for entry in advances if entry.matches >= minimum),
        key=lambda entry: (-entry.matches, -entry.n, entry.area),
    )
    left_out = len(advances) - len(raised)
    return (
        tuple(
            _habit_text(entry, played, map_report.sample.matches)
            for entry in raised
        ),
        _habits_note(left_out, minimum) if left_out else None,
    )


def _anomaly_views(
    report: Report, settings: ReportSettings
) -> tuple[tuple[AnomalyView, ...], str | None]:
    """The anomaly rows and the notes on what was not printed.

    Two return values, because neither the match rule nor the cap can be
    applied in silence: the number left out is written out under the same
    rule as the observations the pattern threshold dropped.

    **The match rule first, then the cap** (Story 4.5), so the cap counts
    only rows the match rule would print: a row that recurs in one match is
    not a candidate for the chapter at all.

    **The order is by repetition, not by map.** The chapter is the report's
    first content chapter and its job is to raise what repeats; on a tie the
    key is (map, side, rule, area), so the equally often observed stay in map
    order and the result is the same from one run to the next.
    """
    minimum = settings.anomaly_min_matches
    # The CT advances are habits and live in the map chapters
    # (:func:`_habit_views`); the stack stands on single-round judgements and
    # is not match-ruled; the crunch is a strategy and is.
    strategies = [
        entry for entry in report.anomalies if entry.rule != "ct_advance"
    ]
    eligible = [
        entry
        for entry in strategies
        if entry.rule != "crunch" or entry.matches >= minimum
    ]
    left_out = len(strategies) - len(eligible)
    ordered = sorted(eligible, key=_anomaly_rank)
    kept = ordered[:MAX_ANOMALY_LINES]
    dropped = len(ordered) - len(kept)
    notes: list[str] = []
    if left_out:
        notes.append(_crunch_rule_note(left_out, minimum))
    if dropped:
        notes.append(
            f"{dropped} poikkeamaa jäi pois: luvussa näytetään enintään "
            f"{MAX_ANOMALY_LINES} useimmin toistuvaa. Kaikki ovat "
            "report.jsonissa kentässä anomalies."
        )
    note = " ".join(notes) or None
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

    **Simultaneity does not cross a sample point.** The summary row gives
    the area, the sample and the orientation; the directions and the player
    count are on the round rows, which read them per sample point
    (:func:`_anomaly_round_text`). The union of two rounds' directions would
    read as more simultaneous directions than were observed -- the opposite
    of the definition -- and so, as Story 4.5 measured, would the union of
    two moments of one round.

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

    The mechanism is **the same as in the three other places where the same
    name is set** (:func:`_map_label`, which the summary's map-pool row also
    calls since Story 4.10, and the map chapter's heading), and it was
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
    """The round types into the label, as a mention that they are no
    restriction.

    The chapter's rules (crunch, stack) know no round type: their
    denominator is all the side's rounds, so the label says **on which types
    it was observed** and not what it is restricted to. Without the wording
    the reader would read a crunch's ``eco`` mark as a restriction and wonder
    why there is no ``default`` row. The CT advance, which was grouped by
    round type and carried a bare type here, left the chapter in Story 4.5
    (:func:`_habit_views`).
    """
    # In :data:`ROUND_TYPE_ORDER`, the order the sections and the habit row
    # use (Story 4.12), so one report never lists the types two ways round.
    names = ", ".join(
        ROUND_TYPE_FI.get(name, name)
        for name in sorted(
            anomaly.round_types, key=lambda name: (_round_type_rank(name), name)
        )
    )
    return f", havaittu: {names}"


def _anomaly_round_text(anomaly: Anomaly, entry: AnomalyRound) -> str:
    """One round's observation: its type, how many, and where from.

    The round number first, because the scout's next act is to open that
    round in the demo, then the round's type, since the chapter's rules are
    not scoped by it.

    **The row no longer says when** (Story 4.5). It used to name the sample
    points, and that was the last place where ``[parse].snapshot_seconds``
    reached the reader: the grid is now a dense internal series, so the same
    round would read ``12/15/18/21/24 s kohdalla`` -- a sentence about the
    settings file rather than about the opponent. See
    :func:`_anomaly_points_text` for what replaced it.

    **A stack's player count is a fraction and not a number.** "4 players"
    says nothing about the anomaly without the number alive: four out of five
    is the defence's choice, four out of four is what was left. The rule
    counts both, so the row says both as well.

    **A stack's places are the crowd's own** (Story 4.4). The row names them
    when there is more than one, because "at most two areas" is the rule
    itself: the summary row carries the area most of the crowd stood on, and
    it alone would read as a crowd on one area where the rule saw it on two.
    """
    text = (
        f"kierros {entry.round_no} "
        f"({ROUND_TYPE_FI.get(entry.round_type, entry.round_type)})"
    )
    text += f": {_anomaly_points_text(entry)}"
    if len(entry.areas) > 1:
        # The crowd's OWN areas, and only when there is more than one of them.
        # The summary row already names the area that held most of them, so
        # repeating a single area would say the same word twice; two or more
        # is the observation the summary cannot carry -- "at most two areas"
        # is the rule itself, and the row would otherwise claim a crowd on one
        # area where the rule saw it on two.
        text += f", alueilla {_areas_text(entry.areas)}"
    if entry.sources:
        # The directions only in a crunch. On the others an empty list means
        # "not asked" and not "no directions", so it is not said out loud.
        text += f", {_sources_text(entry)}"
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


def _sources_text(entry: AnomalyRound) -> str:
    """A crunch round's directions, and whether they were simultaneous.

    **``yhtä aikaa`` only when it is true** (Story 4.5). The directions are
    simultaneous at one sample point (:class:`~pappascout.domain.report
    .AnomalyPoint`), so when **one point holds every direction the round
    names** the row can say so: ``yhtä aikaa suunnista Arch ja TopofMid``.
    That is the condition and not "every point names the same ones": a denser
    grid adds points that see a subset of the arrival (``Anubis_vs_ryhmarama``
    round 10 reads three directions at 15 s and two at 18 s), and a claim
    that flipped to ``eri hetkinä`` on that would depend on the grid. When no
    single moment holds them all, the round's list is a union across moments
    and the row says ``eri hetkinä`` instead
    -- measured on the first dense grid, ``anubis_vs_RCAVE_VETERANS`` round 3
    reached ``Bridge`` from four directions with three players, which
    ``yhtä aikaa`` would have stated as a single impossible moment.

    **The moments are not named**, for the reason the seconds left the row
    (:func:`_anomaly_points_text`): the sample points are a tool.
    """
    names = _areas_text(entry.sources)
    if any(point.sources == entry.sources for point in entry.points):
        return f"yhtä aikaa suunnista {names}"
    return f"suunnista {names} eri hetkinä"


def _anomaly_points_text(entry: AnomalyRound) -> str:
    """How many players the round was seen with -- **without the seconds**.

    **Sample points are a tool for forming the analysis, not the analysis.**
    The product owner, 2026-09-13 and again 2026-09-21: *"Mitta pisteitä ei
    tule laittaa varmaan lopulliseen raporttiin vaan ne ovat työkalu
    muodostaa analyysi raporttiin."* Since Story 4.5
    ``[parse].snapshot_seconds`` is a dense internal series, so a list of
    grid seconds is a statement about the settings file: the same round read
    ``15 ja 30 s kohdalla`` on four points and would read
    ``12/15/18/21/24 s kohdalla`` on fourteen, although the opponent did the
    same thing in both.

    **What replaced the enumeration is the round's peak, stated as a peak and
    not dressed up as a fact.** The rule is overturned, not edited: the old
    docstring argued that *"a number belongs to its moment"*, and it was
    right about the hazard it named -- the row before it took the round's maximum and
    attached it to **every** sample point, claiming five players on Inferno
    round 2 at 30 s where there was one. Naming the seconds was one way to
    stop that. Dropping them is another, and it is the one that survives a
    dense grid: with no moment on the row, no count is attached to a moment
    it was not observed at. So

    * when every sample point of the round saw the same player count, that
      count is the observation (``4/5 pelaajaa``) -- also when the number
      alive differed between them, which is the denominator and not the
      count; the fraction is then the moment with the most alive;
    * when the player counts differ, the row says ``jopa`` and the largest
      (``jopa 4 pelaajaa``) -- the most seen at any of the round's observed
      moments, marked as a peak and true whatever the grid's density.

    **``jopa`` and not ``enintään``** since Story 4.12, the product owner's
    correction (2026-09-26): ``enintään`` reads as a cap, as if more were
    impossible, while the number is the largest count observed. What the
    number *is* did not change -- only the word that says so. ``enintään``
    stays where the report states a real bound (the reading guide's
    ``enintään 30 sekunnin kohdalla``).

    **The number of moments is not written either**, and for the same reason
    the seconds are not: it counts sample points. Measured 2026-09-13, the
    stack's hits went 69 -> 396 on the same archive when the grid went from 4
    points to 22, while the rounds they sit on went 54 -> 82. A count of
    moments would be the same figure in a different coat.

    **A stack reads one point** (``stack_sample_s``), so on the pipeline's
    path its row has one point and takes the first branch, and the fraction
    is a real observation from a real moment. The model does not enforce
    one point, and a row built with two takes the second branch like any
    other.
    """
    # The largest crowd of the round, ties broken by the **most** alive: of
    # two moments with four players, the one with five alive is the defence
    # choosing to be there, and the one with four alive is what was left of
    # it. The stronger claim is the choice, and picking it deliberately keeps
    # the tie-break from depending on the order the points arrive in.
    peak = max(entry.points, key=lambda point: (point.players, point.alive or 0))
    text = _players_of(peak.players, peak.alive)
    # A peak only when the **player count** varied. The number alive is a
    # denominator, not the count the row reports: 4/5 and 4/4 are the same
    # four players, and ``jopa`` on them would call a constant a peak.
    if len({point.players for point in entry.points}) == 1:
        return text
    return f"jopa {text}"


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


def _no_anomalies_text(report: Report, *, habits_elsewhere: bool = False) -> str:
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
    # **Not "no anomalies" when the advances found something** (Story 4.5):
    # they are printed as habits in the map chapters, so an empty chapter
    # means only that no crunch or stack row is printed here. Saying "Ei
    # poikkeamia" beside the rule list would state that the advance rule
    # found nothing, next to its rows in Huomioitavaa. **Awaiting the product
    # owner's wording.**
    opening = (
        "Tässä luvussa ei ole crunch- eikä stack-rivejä."
        if habits_elsewhere
        else "Ei poikkeamia."
    )
    parts = [
        f"{opening} Säännöt ({rules}) ajettiin "
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


def _side_rank(side: str) -> int:
    return SIDE_ORDER.index(side) if side in SIDE_ORDER else len(SIDE_ORDER)


def _buy_class_views(sides: Sequence[SideView]) -> tuple[BuyClassView, ...]:
    """The map's blocks regrouped: buy class first, then side (Story 4.12).

    Every block of every side lands in exactly one :class:`BuyClassView` --
    the one named by its own ``round_type`` -- so nothing is dropped or
    printed twice: the classes are the set of round types the sides hold,
    and each class takes each side's blocks of that type. The model does not
    forbid a side two blocks of one type (``aggregate`` never writes one);
    were there two, both would be printed in the side's own order, because
    the regrouping neither merges nor drops. The side order is the one
    ``sides`` arrives in, which :func:`build_view` has sorted CT first.
    """
    names = sorted(
        {entry.round_type for side in sides for entry in side.round_types},
        key=lambda name: (_round_type_rank(name), name),
    )
    classes = []
    for name in names:
        blocks = tuple(
            (side, entry)
            for side in sides
            for entry in side.round_types
            if entry.round_type == name
        )
        classes.append(
            BuyClassView(round_type=name, heading=blocks[0][1].heading, blocks=blocks)
        )
    return tuple(classes)


# -- The summary -----------------------------------------------------------------


def _sample_text(sample: Any) -> str:
    """The sample in three buckets. All three always, the empty ones too.

    **The row leads with played maps and states no match count**, which is the
    product owner's decision of 2026-09-24 (:func:`played_maps_text`). His
    sample is four ``best_of 2`` matches over eight maps, and what he wants at
    the root is the eight.

    That leaves the report's match count in ``report.json`` and off this row,
    which is deliberate and not an omission: :attr:`.report.Sample.matches` is
    still measured, still validated and still printed **under** the root, on
    every map heading, side row and block heading (Story 4.12 moved the side
    from a heading to a row and put the round type's count on the block). A
    reader adding those up gets more than the root holds, because a match
    that played two maps is in two of them -- the reading guide says so,
    because it is the one thing a Finnish reader needs in order to read the
    numbers and it cannot live in a docstring (:func:`_legend`).

    The bucket breakdown stays in demos and rounds: ``is_league`` buckets
    demos, and a match count per bucket would be a fourth copy of the same
    split (:attr:`~pappascout.domain.report.Sample.matches`).
    """
    parts = [
        f"{SAMPLE_BUCKET_FI[name]} {getattr(sample, name).demos} / "
        f"{getattr(sample, name).rounds}"
        for name in SAMPLE_BUCKETS
    ]
    return (
        f"{played_maps_text(sample.demos)}, {rounds_text(sample.rounds)} "
        f"(demoa/kierrosta: {', '.join(parts)})"
    )


def _map_pool_text(report: Report) -> str:
    """The map pool: which maps were played and how many times each.

    The product owner asked for this beside the opponent, 2026-09-24, and it
    is scouting information in its own right -- which maps a team picks and
    plays is a fact about the team, not bookkeeping about the sample.

    **It computes nothing and brings in no new value** (AD-10), and that is
    why the story adds no field for it: every number here is
    ``maps[].sample.demos``, which the map's own heading already states, and a
    field would be the same eight demos counted a second time in
    ``report.json`` with nothing holding the two to each other. The precedent
    is :func:`_traceability`, which collects the report's ids into a chapter
    without the model growing a list of them.

    **The order is the map chapters' own, and it is an order in rounds while
    the number printed is demos.** ``build_report`` sorts the maps on
    ``-sample.rounds`` with the name breaking a tie, so this row reads as a
    table of contents for the chapters below it -- which is the property
    worth having -- and it is **not** sorted by the count it shows. Measured
    2026-09-25 on the real archive: one team's four maps all tie at one demo
    and are printed nuke, anubis, ancient, inferno, which is the round order
    and not the alphabet; another prints ``de_nuke`` before ``de_anubis``
    on 28 rounds against 22, though both are one demo. A map of 2 demos over
    10 rounds precedes one of 3 demos over 3, so the counts can descend,
    ascend or neither.

    The earlier wording here said "most played first and the name on a tie",
    which is ``build_report``'s comment word for word and was harmless until
    this story started **printing the demo count in that order**. Two
    sentences now have to agree, and they do: ``aggregate``'s comment says
    which key it sorts on, and this one says the row inherits that order
    rather than imposing one.

    The names are code spans for :func:`_map_label`'s reason: since Story 2.11
    a map name is free text from the demo's header, and a workshop map called
    ``*|Aim|* Botz [beta]`` would otherwise set the rest of the row in bold.

    **An unrecognised map is named by its ordinal and not by its id**, which
    is :data:`UNKNOWN_MAP_LABEL`'s whole purpose and was found here by the
    test that guards it: when the name could not be read, ``map_name`` **is**
    the ``map_demo_id``, so a row built from the name alone would put a demo
    id in the summary -- the one chapter the reader sees first and the one
    Story 2.12 emptied of ids.

    **The label comes from :func:`_map_label` and is not spelled again here.**
    The first draft of this row had a copy of that rule, which would have made
    three copies of it with only two of them arguing for themselves -- and
    :func:`_anomaly_map_label` says out loud that there are *two other
    places*, a sentence a third copy falsifies without touching it.

    **The separator is a semicolon and not a comma**, which is this row's own
    problem and nothing else's: :data:`UNKNOWN_MAP_LABEL` **contains a
    comma of its own**, and comma-joined it turned two maps into three
    fragments in the report's first chapter. (The string is not quoted
    here; the constant is two hundred lines up and the test that pins
    this row quotes it, under a named exemption.) The label's two other
    uses put it in a bold key on a row of its own, where its comma never
    meets another; this is the first place it sits in a list. Changing the
    label instead would have moved the punctuation into a string three
    chapters share.
    """
    return "; ".join(
        f"{_map_label(index, entry)} {times_text(entry.sample.demos)}"
        for index, entry in enumerate(report.maps, start=1)
    )


def _roster_sample_text(sample: Any) -> str:
    """The roster breakdown as one line. All three buckets, empty ones too.

    Reads and formats; it counts nothing (AD-10). Every number here is a field
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
    value had been lost on the way. A non-empty list is its **count**
    (``10 näytepistettä``) and not its seconds: since Story 4.5 the list is
    the analysis's grid minus what the report prints, and the seconds on the
    summary row would be the grid on the page.
    """
    if isinstance(value, list):
        # A count and not the seconds (Story 4.5): the one list in the
        # section is skip_sample_seconds, and its members are the analysis's
        # grid, which the report does not show.
        if not value:
            return "ei yhtään"
        return f"{len(value)} näytepiste" + ("" if len(value) == 1 else "ttä")
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

    # The pool immediately under the sample, because it is the same eight
    # played maps broken down -- and above every note about what is missing
    # from them. Omitted on an empty report rather than written as an empty
    # list: a bare label would say the team plays no maps, where the truth is
    # that this archive holds none of them. The ``Otanta`` row and the
    # empty-data note already say that, and the two rows have to agree.
    if report.maps:
        items.append(SummaryItem(MAP_POOL_LABEL, _map_pool_text(report)))

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
    """How a chapter names one map: its name, or its ordinal when unknown.

    **Three callers since Story 4.10** -- the traceability chapter's map row,
    the summary's map-pool row (:func:`_map_pool_text`) and, through
    :data:`UNKNOWN_MAP_LABEL`, the anomaly row -- and one rule between them,
    so that a reader can carry a map from one chapter to another by the
    string. The paragraphs below argue the traceability row because that is
    where the rule was first needed; every word of them holds for the other
    callers.

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
    "polussa, puuttuvan demon komennossa, kartassa, jonka nimeä ei "
    "tunnistettu, ja karttaluvun karttalistan riveillä, joilla se kertoo "
    "minkä demon rivistä on kyse."
)


# -- The public build function ---------------------------------------------------


def _played_map_line(entry: PlayedMap) -> str:
    """One row of a map's demo list: when, against whom, and which file.

    **A row whose match is in the index has three parts in one order**, so
    the column the eye lands on is the same one on each: **when**, **who**,
    and the id in brackets. Neither of the first two is dropped when its
    value is missing -- the absence is written out instead
    (:data:`MATCH_DATE_MISSING`, :data:`OPPONENT_MISSING`) -- because a
    dropped part would move the others and a reader scanning four rows for a
    date would find a name in its place. The two are named separately
    because they are independent: 35 of the archive's 66 indexed matches have
    no finish time and every one of them can still be named.

    **A row whose match is not in the index has two**, and that is the one
    deliberate exception to the shape above. It has no date and no opponent
    and it has neither for a single reason, so it says that reason once
    (:data:`MATCH_NOT_INDEXED`) rather than printing two "not known" marks
    side by side, which would read as two independent gaps. The cost is real
    and is the smaller one: that row's id does sit further left than an
    indexed row's. The first draft of this docstring asserted "three parts on
    every row" and then described this exception two paragraphs later;
    measured 2026-09-25, a map is in practice all one kind or the other --
    each of the archive's three teams has either every demo in the index or
    none of them.

    **The id is on the row and not only in the traceability chapter**, which
    is a deliberate exception to "the body speaks in names" (Story 2.12) and
    the same one the missing-demo row and the unrecognised map take. Here it
    is what the reader copies into ``uv run pappascout parse`` to go and watch
    the map, and for a hand-imported demo it is the row's **only** identifier:
    there is no date and no opponent to tell it from the map's other rows.

    A code span, so the id survives copying byte for byte
    (:func:`_identifier`), and the opponent's name is escaped as text: it is
    free text from the match index, and a team whose name contains an
    asterisk would otherwise set the rest of the chapter in italics.
    """
    if not entry.indexed:
        parts = [MATCH_NOT_INDEXED]
    else:
        parts = [
            entry.played_on.isoformat()
            if entry.played_on is not None
            else MATCH_DATE_MISSING,
            f"{OPPONENT_PREFIX} {markdown_text(entry.opponent)}"
            if entry.opponent is not None
            else OPPONENT_MISSING,
        ]
    return f"{', '.join(parts)} ({_identifier(entry.map_demo_id)})"


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
        # One decision per map chapter: see ``_map_states_recency``.
        states_recency = _map_states_recency(map_report)
        # The map's demos by id, so a pistol round's row can say when it was
        # played and against whom without the route carrying a second copy
        # of either (Story 4.11). The model refuses a map that lists a demo
        # twice, so the mapping cannot lose a row.
        played_by_demo = {
            entry.map_demo_id: entry for entry in map_report.played_maps
        }
        # CT first (:data:`SIDE_ORDER`): the side rows at the top of the map
        # chapter and the blocks inside each buy class read in the same order.
        for side in sorted(
            map_report.sides, key=lambda entry: _side_rank(entry.side)
        ):
            views: list[RoundTypeView] = []
            for entry in sorted(
                side.round_types, key=lambda rt: _round_type_rank(rt.round_type)
            ):
                views.append(
                    _round_type_view(
                        entry,
                        threshold,
                        flags,
                        settings,
                        states_recency,
                        played_by_demo,
                    )
                )
                # The rows and not the routes: a round that reached no
                # sample point is a route that prints no route row, and the
                # guide's paragraphs would then explain an indentation
                # nothing in the report shows. See ``_Flags.routes_shown``.
                if any(view.rows for view in views[-1].routes):
                    flags.routes_shown = True
            sides.append(
                SideView(
                    side=side.side,
                    heading=f"{side.side}-puoli",
                    # The match count goes beside the round count and never
                    # instead of it (Story 4.9): the rounds are the honest
                    # sample size and ``small_sample`` is read from them.
                    rounds_text=(
                        f"{rounds_text(side.sample.rounds)} "
                        f"{matches_text(side.sample.matches)}"
                    ),
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
        # The map's heading names all three units, and the demos stay: a demo
        # is the file the reader opens to check a claim, and the map chapter
        # is where the traceability chapter's ids are counted from. The
        # matches are what the reader is asking about.
        heading = (
            f"{_identifier(map_report.map_name)} -- "
            f"{rounds_text(map_report.sample.rounds)}, "
            f"{demos_text(map_report.sample.demos)} "
            f"{matches_text(map_report.sample.matches)}"
        )
        if name_unknown:
            heading += " (kartan nimeä ei tunnistettu tunnisteesta)"
        # The demo list belongs to the map chapter and not to the summary:
        # it is the map's own sample spelled out, and a reader who has
        # stopped at ``de_nuke`` is exactly the reader asking how old these
        # four observations are.
        #
        # Two flags and not one: a demo outside the index and a demo inside it
        # with a value missing print different marks, and each mark is
        # explained only where it appears. See ``_Flags.unknown_in_index``.
        for entry in map_report.played_maps:
            if not entry.indexed:
                flags.unindexed_demo = True
            elif entry.played_on is None or entry.opponent is None:
                flags.unknown_in_index = True
        habits, habits_note = _habit_views(report, map_report, settings)
        maps.append(
            MapView(
                map_name=map_report.map_name,
                heading=heading,
                name_unknown=name_unknown,
                sides=tuple(sides),
                played_maps_label=(
                    PLAYED_MAPS_ORDERED
                    if map_report.every_demo_is_placed
                    else PLAYED_MAPS_UNORDERED
                ),
                played_maps=tuple(
                    _played_map_line(entry) for entry in map_report.played_maps
                ),
                note=None if sides else _NO_SIDES,
                habits=habits,
                habits_note=habits_note,
                buy_classes=_buy_class_views(sides),
            )
        )

    anomaly_views, dropped_note = _anomaly_views(report, settings)
    # Where the advances went, said in the chapter the reader looks for them
    # in -- once, and only when there are any (Story 4.5).
    pointer = (
        HABITS_POINTER
        if any(entry.rule == "ct_advance" for entry in report.anomalies)
        else None
    )
    chapter_note = " ".join(
        part
        for part in (
            dropped_note
            or _no_anomalies_text(report, habits_elsewhere=pointer is not None),
            pointer,
        )
        if part
    )
    trailing_note = " ".join(part for part in (dropped_note, pointer) if part)
    return ReportView(
        title=_title(report),
        summary=tuple(_summary(report, threshold, settings)),
        anomalies=anomaly_views,
        # When the match rule left nothing to print, its note is the
        # chapter: "Ei poikkeamia" would claim a measured negative about rows
        # that exist in report.json.
        anomalies_note=None if anomaly_views else chapter_note,
        anomalies_dropped_note=(trailing_note or None) if anomaly_views else None,
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
        "kierrokset, joissa havainto tehtiin, m lohkon kaikki kierrokset "
        "eli saman puolen kyseisen kierrostyypin kierrokset. Mediaanin "
        "otanta rivin otsikossa (esimerkiksi "
        "\"mediaani 14,2 s, 7/9 kierroksesta\") noudattaa tätä sääntöä: se "
        "kertoo, monellako kierroksella ajoitus mitattiin. Saman rivin "
        "aluevaateet laskevat sen sijaan vain niitä kierroksia, joilla "
        "havainto oli olemassa, joten niiden nimittäjä on pienempi."
    )
    # Unconditional, like the two notes above it and unlike the flagged ones
    # below: the record is on every block's heading, so there is no state
    # in which it is absent and the note would explain a line that is not
    # there. The last sentence is the one the guide exists for -- the reader
    # is told that the missing rate is a decision and not an omission.
    # "Lohkon" and not "Kierrostyypin" since Story 4.12: the record now sits on
    # the block's heading, which names the side under a buy-class section.
    # The changed word awaits the product owner's.
    notes.append(
        "Lohkon otsikon tulos (esimerkiksi "
        f'"{record_text(_LEGEND_RECORD)}") laskee '
        "lohkon omat kierrokset: ensin voitetut, sitten hävityt. Ne ovat "
        "samat kierrokset, jotka otsikon kierrosmäärä laskee. "
        "Jos jonkin kierroksen tulosta ei saatu, se sanotaan otsikossa "
        "erikseen eikä lasketa tappioksi. Raportti kertoo luvun eikä "
        "johda siitä osuutta tai arviota: tulkinta on lukijan."
    )
    # THREE PARAGRAPHS, ALL UNCONDITIONAL, ALL AWAITING THE PRODUCT OWNER'S
    # WORD (Story 4.9). Unconditional for the record's reason: every heading
    # carries the match count, so there is no report in which they explain a
    # line that is not there.
    #
    # The third one is not an explanation of a formatting choice but **an
    # invariant the reader cannot read the numbers without**. The map
    # headings say 4, 3 and 1 matches under a root that counts eight played
    # maps; adding the three gives eight as well, and a reader who adds them
    # concludes eight matches. Nothing else on the Finnish side says
    # otherwise -- the reasoning lives in ``domain``'s English docstrings,
    # where no team-mate looks before a match, and that is an AD-11 question
    # and not a wording one.
    #
    # The second one names **two** causes for a row that is marked absent all
    # the way across, because there are two and only one of them used to be
    # written. Measured 2026-09-24 on the real archive: ``de_nuke`` T force at
    # 6 s prints only bars that exclude the newest match, while the bar that
    # includes it (``Outside 5``) was dropped by the repetition threshold and
    # ``rounds_missing`` is zero. Every claim on that row is true; the
    # paragraph's account of them was not.
    notes.append(
        f"Kierrosten rinnalla luetaan ottelut (n/m {MATCH_SAMPLE_UNIT}): "
        "kolme kierrosta kolmesta ottelusta on tapa, kolme kierrosta yhdestä "
        "ottelusta tapahtui kerran, ja pelkkä kierrosmäärä kirjoittaa ne "
        "samalla tavalla. Kartan otsikko, puolten rivit ja lohkojen otsikot "
        "kertovat ottelumäärän aina. Väitekohtainen "
        "ottelumäärä on näytepisteiden riveillä ja ensikontaktin "
        "läsnäolorivillä; muilla riveillä lukee vain kierrokset, ja niiden "
        "nimittäjä on otsikon ottelumäärä. Ottelumäärä jätetään riviltä pois "
        "silloin, kun se on sama murtoluku kuin kierrosmäärä -- esimerkiksi "
        "pistoolilohkossa, jossa jokainen ottelu antaa yhden kierroksen -- ja "
        "kokonaan silloin, kun lohkossa on vain yksi ottelu. Tuoreusmerkintä "
        "kirjoitetaan siltikin, jos kartalla on useampi ottelu."
    )
    notes.append(
        f'Merkintä "{RECENCY_NEWEST_INCLUDED}" tai '
        f'"{RECENCY_NEWEST_ABSENT}" kertoo, onko **kartan uusin ottelu** '
        "niiden joukossa, joissa havainto tehtiin. Se vastaa kysymykseen "
        "\"päteekö tämä yhä\" -- osuus ei vastaa: sama 3/4 syntyy kolmesta "
        "vanhimmasta ottelusta ja kolmesta uusimmasta. **Uusin on kartan "
        "uusin eikä lohkon oma uusin**, ja se on merkinnän koko arvo: jos "
        f'lohkossa ei ole yhtään kierrosta kartan uusimmasta ottelusta, koko '
        f'lohko lukee "{RECENCY_NEWEST_ABSENT}" -- eli tämä lohko on vanhaa '
        "tietoa. Kokonaan merkitty rivi voi syntyä myös siitä, että "
        "uusimman ottelun havainnot jäivät rivin kynnyksen alle tai "
        "näytepisteestä puuttuu kierroksia; rivin oma huomautus kertoo "
        "puuttuvat kierrokset ja lohkon huomautus kertoo karsitut havainnot. "
        "Ottelut eivät ole painotettuja millään luvulla: raportti kertoo "
        "havainnot siinä järjestyksessä kuin ne tapahtuivat, jotta jokainen "
        "luku on tarkistettavissa demoilta. Merkintä puuttuu kahdessa "
        "tapauksessa: kun kartalla on vain yksi ottelu, jolloin jokainen "
        "havainto on siinä ja kartan otsikko sanoo sen jo, ja kun otteluiden "
        "järjestystä ei tiedetä -- esimerkiksi käsin tuodulle demolle, jota "
        "ei ole otteluindeksissä."
    )
    notes.append(
        "**Ottelumäärät eivät laske yhteen tasojen välillä, kierrosmäärät "
        "laskevat.** Sama ottelu voi pelata kaksi karttaa ja pelaa aina "
        "molemmat puolet, joten se on mukana useamman otsikon "
        "ottelumäärässä: karttojen ottelumäärät yhteen laskettuna saa "
        "suuremman luvun kuin otteluita on. Kierrokset sen sijaan jakautuvat "
        "kartoille, puolille ja kierrostyypeille kukin täsmälleen kerran, "
        "joten ne laskevat yhteen. Yhteenvedon rivi kertoo pelattujen "
        "karttojen määrän juuri tästä syystä: se on luku, jonka voi laskea "
        "yhteen."
    )
    # Conditional, and it was written "unconditional" in the first draft --
    # wrongly, twice over. An empty report has no map chapter, so the list it
    # describes is not there; and its last sentence names the
    # ``Karttavalikoima`` row, which ``_summary`` deliberately omits on an
    # empty report with a comment saying the two rows have to agree. The
    # condition is therefore the same one the summary row uses.
    if report.maps:
        notes.append(
            "Jokaisen kartan alussa on karttojen lista: yksi rivi per "
            "pelattu kartta, ja rivillä pelipäivä, vastustaja ja demon "
            f'tunniste. Listan otsikko on "{PLAYED_MAPS_ORDERED}" silloin, '
            "kun jokaisen rivin pelipäivä tiedetään, ja pelkkä "
            f'"{PLAYED_MAPS_UNORDERED}" silloin, kun yhdenkin rivin päivä '
            "puuttuu -- järjestystä ei silloin voi luvata. Päivä ja "
            "vastustaja tulevat arkiston otteluindeksistä, jonka "
            "discover-vaihe kirjoittaa, eikä raportti arvaa kumpaakaan "
            "mistään muualta. Raportti kertoo vastustajan nimen eikä arvioi "
            "sitä: vahvuus, sijoitus ja vastaavat ovat lukijan tulkintaa. "
            "Lukuja ei myöskään ryhmitellä vastustajan mukaan -- se veisi "
            "kierroksia niiltä lohkoilta, joilla niitä on, niille joilla ei "
            f'ole. Yhteenvedon "{MAP_POOL_LABEL}" laskee samat kartat: '
            "kuinka monta kertaa kukin kartta on pelattu, yhteensä yhtä "
            "monta kuin otannan pelatut kartat."
        )
        # THE STRUCTURE, SAID ONCE (Story 4.12). Awaiting the product owner's
        # word. The types are the ones **this report prints**, in the order
        # the view sorts them by, so the sentence neither names a section
        # the report lacks nor drifts from the order it prints.
        # "Kierrostyyppi" and not "ostoluokka": two of the types (poikkeama,
        # jatkoaika) are not buy classes.
        printed = {
            entry.round_type
            for map_report in report.maps
            for side_report in map_report.sides
            for entry in side_report.round_types
        }
        if printed:
            types = _join_fi(
                [
                    ROUND_TYPE_FI.get(name, name)
                    for name in sorted(
                        printed, key=lambda name: (_round_type_rank(name), name)
                    )
                ]
            )
            sides = " ja sitten ".join(f"{side}-puolen" for side in SIDE_ORDER)
            notes.append(
                "Kartan luku etenee kierrostyypeittäin; tämän raportin "
                f"kierrostyypit järjestyksessä: {types}. Kunkin kierrostyypin "
                f"alla on ensin {sides} lohko. Puoli, joka ei pelannut "
                "kierrostyyppiä, jää siitä pois, eikä kierrostyyppiä, jota "
                "kumpikaan puoli ei pelannut, kirjoiteta. Puolten omat "
                "kierros- ja ottelumäärät ovat kartan luvussa omilla riveillään ennen ensimmäistä "
                "kierrostyyppiä."
            )
    if flags.unindexed_demo:
        notes.append(
            f'"{MATCH_NOT_INDEXED}" kartan rivillä tarkoittaa, ettei demon '
            "ottelua löydy arkiston otteluindeksistä -- tavallisimmin siksi, "
            "että demo on tuotu käsin eikä sen takana ole liigaottelua. "
            "Silloin rivillä ei ole päivää eikä vastustajaa, eikä raportti "
            "lue niitä tiedostonimestä: tiedostonimi ei ole havainto, ja "
            "siitä luettu nimi olisi väite, jota ei voi tarkistaa. Rivin "
            "tunniste kertoo, mistä demosta on kyse."
        )
    # The two marks a row inside the index can carry, explained under a flag
    # of their own. Folded into the note above they were invisible on exactly
    # the report that prints them most: one whose every demo IS indexed.
    if flags.unknown_in_index:
        notes.append(
            f'"{MATCH_DATE_MISSING}" ja "{OPPONENT_MISSING}" kartan rivillä '
            "tarkoittavat, että ottelu **on** otteluindeksissä, mutta tietoa "
            "ei saatu siitä. Päivä puuttuu, jos ottelulle ei ole kirjattu "
            "päättymisaikaa. Vastustaja jää nimeämättä kahdesta eri syystä, "
            "joita rivi ei erottele: joko indeksin rivi ei nimeä joukkueita, "
            "tai nimet ovat siellä mutta rivin kahta puolta ei voi erottaa "
            "toisistaan -- raportti tunnistaa scoutattavan joukkueen siitä, "
            "kumman puolen kokoonpanosta sen havaitut pelaajat löytyvät, ja "
            "jos tämä ei ratkea, raportti jättää nimen kertomatta sen sijaan "
            "että arvaisi kumman tahansa. Kumpikaan ei tarkoita samaa kuin "
            f'"{MATCH_NOT_INDEXED}", joka tarkoittaa ettei ottelua ole '
            "indeksissä lainkaan."
        )
    notes.append(
        "Ensikontaktin rivi kertoo elossa olevat pelaajat alueittain sillä "
        "hetkellä, kun kierroksen ensimmäinen ristiinpuolinen osuma tapahtui."
    )
    # THE ROUTE'S OWN PARAGRAPH (Story 4.11). Flagged, like the two index
    # notes above and unlike the record's: a report with no route ROW has
    # nothing for it to define. The flag counts rows and not routes, because
    # a round that reached no sample point is a route with no rows at all --
    # gated on the routes, a block of such rounds would print a paragraph
    # about an indentation nothing in the report shows.
    #
    # It says four things, and the last three are the ones the block cannot
    # say for itself. The arrow is **not** adjacency -- these are positions
    # more than ten seconds apart, and the product owner corrected exactly
    # that reading once. A death is stated only from the death table. A
    # player the sample lost is NOT a death.
    #
    # AND A ROW THAT SIMPLY ENDS DOES NOT MEAN THE ROUND ENDED. It means the
    # sampling has no later moment for those players, and measured over the
    # archive the usual reason is that the grid ran out while the round went
    # on: 285 of the 293 rounds reaching the last point record a death after
    # it (domain.aggregate.ROUTE_SAMPLING_MEASURED). The first wording of
    # this paragraph said "kierros oli jo ohi" and was wrong for 97.3 per
    # cent of the rows it described -- and it was the one sentence in the
    # report that told the reader how to read the end of a row.
    #
    # THE WORDING IS THE IMPLEMENTATION'S AND AWAITS THE PRODUCT OWNER'S
    # WORD, as every other Finnish string this story added does (see
    # ROUND_WON and the constants beside it). What is not his to soften is
    # the fact underneath it.
    if flags.routes_shown:
        notes.append(
            "Pistoolilohkon rivit kertovat reitin: yksi lihavoitu rivi per "
            "kierros, ja sen alla lista, jossa on yksi kohta jokaisesta "
            f"paikasta, josta lähdettiin. Nuoli ({ROUTE_ARROW}) vie "
            "näytepisteestä seuraavaan saman rivin sisällä, ja luku sen "
            "edessä on pelaajien määrä. **Nuoli ei tarkoita, että alueet "
            "olisivat vierekkäin**: näytepisteiden väli on useita "
            f"sekunteja, joten \"A {ROUTE_ARROW} B\" tarkoittaa että "
            "pelaajat olivat ensin A:ssa ja sitten B:ssä -- väliin jäänyttä "
            "reittiä otanta ei näe."
        )
        notes.append(
            "**Jakautuminen on listan sisennys, ei nuoli.** Yhdessä pysyvä "
            "ryhmä jatkaa samaa riviä; siellä missä ryhmä jakautuu, rivi "
            "päättyy siihen kohtaan ja jokainen osa saa oman rivinsä yhtä "
            "tasoa sisempänä. Yhteistä alkuosaa ei toisteta. Saman tason "
            "rivit ovat siis **toistensa vaihtoehtoja** -- eri pelaajia, "
            "samaan aikaan -- eivätkä peräkkäisiä tapahtumia, ja siksi "
            "niiden sekunnit eivät ole kasvavassa järjestyksessä. "
            "Jakautuminen, joka ei enää jatku, luetaan samalta riviltä "
            "pilkuilla eroteltuna. Kierrokset ovat uusin ottelu ensin, "
            "samassa järjestyksessä kuin kartan karttalista."
        )
        notes.append(
            f'"{ROUTE_DIED}" reitillä on **mitattu kuolema**: paikka ja '
            "hetki tulevat demon kuolemataulusta, eivät siitä että pelaaja "
            f'puuttuu näytepisteeltä. "{ROUTE_GONE}" tarkoittaa juuri sitä '
            "eroa: näytepiste on olemassa, pelaajalla ei ole siinä riviä, "
            "eikä yksikään kuolemarivi kerro mihin hän jäi -- raportti ei "
            "silloin väitä kuolemaa, jota se ei ole mitannut. Raportti "
            "nimeää alueet; kuvion nimeäminen on lukijan."
        )
        notes.append(
            "**Rivin loppuminen ei tarkoita, että kierros olisi ohi.** Se "
            "tarkoittaa vain, ettei otannassa ole näille pelaajille "
            "myöhempää näytepistettä -- ja tavallisin syy on, että otanta "
            "loppui kesken kierroksen: näytepisteitä otetaan vain "
            "kierroksen ensimmäisiltä sekunneilta, ja arkistosta mitattuna "
            "285 niistä 293 kierroksesta, jotka yltävät viimeiseen "
            "näytepisteeseen, kirjaavat kuoleman vielä sen jälkeen. Rivin "
            "viimeinen kohta on siis viimeinen havainto eikä kierroksen "
            "loppu, eikä rivi väitä mitään sen jälkeisestä ajasta."
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
        f"viimeisessä luvussa {TRACEABILITY_HEADING}. Neljä poikkeusta, "
        "joissa tunniste on rungossa siksi että se on siellä ainoa "
        "käyttökelpoinen muoto: kierrosliitteen polut, puuttuvan demon rivi "
        "(tunniste on osa komentoa, jonka voi kopioida), kartta, jonka "
        "nimeä ei tunnistettu (tunniste on kartan ainoa nimi), ja karttojen "
        "listan rivit (tunniste kertoo, minkä demon rivistä on kyse, ja "
        "käsin tuodulla demolla se on rivin ainoa tuntomerkki)."
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
            "takia kalustorivi voi puuttua**: myös toistumisen kynnys voi "
            "pudottaa sen, ja silloin lohkon oma huomautus kertoo siitä. "
            f"{exception} Asetus: "
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
        # **In words and not as a list** (Story 4.5): the reader learns once
        # that the analysis samples more densely than the report prints, and
        # does not see the grid. A list of the hidden seconds is the settings
        # file on the page.
        # **Of what was read, not of the archive in general** (review round
        # 1): the paragraph appears only when a block really held a hidden
        # point, and an archive can mix demos parsed densely with demos parsed
        # at the printed points alone -- so the sentence is conditional on the
        # demo rather than a claim about every map.
        notes.append(
            "**Säännöt lukevat tiheämmin jäsennetyistä demoista näytepisteitä, "
            "joita raportti ei tulosta.** Niissä säännöt näkevät pelaajien "
            "liikkeen tiheästä näytepistesarjasta; raportti tulostaa niistä "
            "vain osan, koska se kertoo mitä kierroksella tapahtui eikä missä "
            "kukin oli kunkin sekunnin kohdalla. Tulostamaton näytepiste ei ole "
            "puuttuva "
            "havainto: se on report.jsonissa ja parsituissa tauluissa "
            "sellaisenaan. Lohkojen huomautusten harvinaisempien havaintojen "
            "määrä koskee vain tulostettuja näytepisteitä. Tämä on ainoa "
            "näistä säännöistä, joka koskee myös suojattuja kierrostyyppejä: "
            "se ei karsi riviä pois vaan pitää näytepisteruudukon raportin "
            "ulkopuolella. Asetus: [report].skip_sample_seconds."
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
    observations = _threshold_int(
        report, "advance_area_min_observations_per_point"
    )
    bound = _threshold_float(report, "advance_max_sample_s")
    advance_players = _threshold_int(report, "advance_min_players")
    crunch_players = _threshold_int(report, "crunch_min_players")
    crunch_sources = _threshold_int(report, "crunch_min_sources")
    crunch_lookback = _threshold_float(report, "crunch_lookback_s")

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
            f"alueella on vähintään {observations} havaintoa näytepistettä "
            "kohden; sitä vähemmällä alue ei ole kummankaan puolen aluetta "
            "eikä tuota poikkeamaa."
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
    # Where it is printed, since Story 4.5. **Awaiting the product owner's
    # wording.** Stated in the definition because the reader meets the rule
    # here and its rows elsewhere.
    advance += (
        f" Kirjataan tavaksi karttaluvun kohtaan {HABITS_LABEL} eikä "
        "Poikkeamat-lukuun, ja saman alueen säästökierrokset lasketaan "
        "yhdessä kierrostyypistä riippumatta."
    )
    # Why the habit row may say "puskee" and the other two rows may not
    # (Story 4.12, see ``_habit_players_text``). Awaiting the product owner's
    # wording.
    advance += (
        " Rivi sanoo, että CT-pelaajat puskevat alueelle: T:n hallussa "
        "olevalla alueella oleminen säästökierroksella on jo etenemistä. "
        "Stack- ja crunch-rivit eivät sano puskusta mitään, eri syistä: "
        "stack on kasauma CT:n omalla sitellä, eikä siitä erotu, odottaako "
        "se vai puskeeko se; crunchilla on oma nimensä strategiana, ja tapa "
        "ei ole strategia."
    )
    notes.append(advance)

    lookback_text = (
        f"pelaajan oma alue {_seconds(crunch_lookback)} sekuntia aiemmin"
        if crunch_lookback is not None
        else "pelaajan oma alue hetkeä aiemmin"
    )
    crunch = (
        f"**{ANOMALY_RULE_FI['crunch']}**: sama T:n alue, mutta pelaajien on "
        f"**saavuttava** sinne yhtä aikaa eri suunnista -- lähtösuunta on "
        f"{lookback_text}."
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

    **The coverage is here and not only in the empty chapter's text.** A
    silenced map stays silent also when there are hits on other maps -- and
    precisely then the reader sees a chapter without that map in it and
    nothing that says why. The empty chapter's text (:func:`_no_anomalies_text`) is
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
    areas = _threshold_int(report, "stack_max_areas")
    moment = _threshold_float(report, "stack_sample_s")

    rule = (
        f"**{ANOMALY_RULE_FI['stack']}**: subjektin puolustus kasautuneena "
        "saman alueryhmän alueille. Alueryhmä on **johdettu tästä demosta**: "
        "jokaisen alueen keskipiste lasketaan demon omasta pistepilvestä, ja "
        "alue kuuluu lähemmän siten ryhmään"
    )
    if margin is not None:
        rule += f", jos toinen site on vähintään {_ratio(margin)} kertaa kauempana"
    rule += (
        ". Ei karttatietokantaa eikä käsin annettua aluejakoa. Osuma vaatii "
    )
    if players is not None:
        rule += f"vähintään {players_text(players)} "
    if areas is not None:
        rule += f"enintään {areas} saman siten ryhmän alueella"
    else:
        rule += "saman siten ryhmässä"
    if moment is not None:
        rule += f" {_seconds(moment)} sekunnin kohdalla"
    rule += (
        ". Spawnissa seisova ei laske, eikä alue jonka geometria jättää "
        "**ilman ryhmää** tuota osumaa -- ja se on demokohtainen havainto "
        "eikä sääntö: Infernon Middle kuuluu A-ryhmään ja näkyy siksi "
        "rivinä, Ancientin ei kuulu kumpaankaan. **Rivin alue on vain rivin "
        "nimilappu**: ensimmäinen kierroksen nimeämistä alueista, suurin "
        "ensin ja tasatilanteessa aakkosissa ensimmäinen -- ei väite siitä, "
        "että juuri siellä olisi ollut eniten pelaajia. Havainto on "
        "kierrosrivin alueissa: viisi pelaajaa Alleyssa on B-siten stack, "
        "vaikka kukaan ei seiso BombsiteB:llä. Rivin luku on muotoa 4/5 -- "
        "kasassa olleet kaikista elossa olleista, myös spawnissa tai "
        "ryhmättömällä alueella seisovista. **Stackia ei ole rajattu "
        "kierrostyyppiin** eikä se lue alueen T-osuutta, joten se ei ole "
        "kummankaan toisen säännön tiukempi eikä löysempi muoto. Sääntö ei "
        "myöskään nimeä kuviota: **kasauma on havainto, ei nimi** -- "
        "odottaako se paikallaan vai puskeeko se, ei erotu tästä havainnosta."
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
            "ilman siteryhmiä: demosta ei saatu A:n ja B:n välille jakoa "
            "kummallakaan akselilla -- ei vaakatasossa eikä korkeudella -- "
            "eikä jakoa saa keksiä. **Sääntö vaikenee siellä**, ja "
            "vaikeneminen on oikea vastaus -- muttei havainto siitä, ettei "
            "stackeja ollut."
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
