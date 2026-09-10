"""``pipeline`` -- the one module allowed to know the order of the stages (AD-1).

AD-1 has said since 2026-08-27 that a stage never calls another stage and
that ``stages.pipeline`` decides the order. Until Story 4.1 the second half
had no implementation: the nine commands each ran exactly one stage and a
person chained them by hand. This module is that missing half, and it changes
nothing about the stages -- they are called here exactly as the nine commands
call them, with the same settings section and the same port.

Why it exists at all
--------------------
The tool is run **through a model**, and every hand-off between two commands
is a place where the model has to decide something. The chain
``discover -> select -> fetch -> parse -> classify -> aggregate -> render``
is seven such decisions, and each is a chance to decide wrongly. One command
removes all seven.

Both shapes stay. The single-stage commands are how a person stops and looks
at an intermediate result, and they are how every stage is tested; ``scout``
is for the unattended run.

Failure is a status, not a stop (AD-9)
--------------------------------------
A unit that fails becomes a :class:`Step` with the outcome ``failed``, and
the chain carries on with the units that did not fail. One corrupt demo must
not cost the others their place in the report -- a person running the nine
commands sees what happened and carries on, and a model running one command
cannot. An exception is raised only when **nothing can proceed at all**, and
then it is raised as :class:`RunStopped`, which carries the steps collected
so far so that the summary is printed before the error.

Nothing is swallowed. A stage-wide fault becomes a ``failed`` step per unit
with the fault's own message and its own next step, exactly as
``fetch.run_many`` already does for a repeated failure.

It has to work with no download authorisation
---------------------------------------------
The download port may be refused authorisation altogether. That is a status
here and not the end of the run: the chain goes on to parse and classify
whatever is already in the archive, which is what a hand import puts there
(AD-8). A ``scout`` that only works once the download source answers would be
a ``scout`` that has never been run.

Freshness is not decided here
-----------------------------
Every stage compares its own manifest and returns ``skipped=True`` when its
result is current. This module **must not** compare manifests: it calls the
stage for every unit and reports what came back. A second implementation of
freshness is the defect this architecture is shaped to prevent, so the rule
is guarded rather than merely written down. Both guards are in
``tests/test_stage_pipeline.py``:
``test_the_pipeline_calls_the_stage_even_when_the_result_is_current``
counts the calls, and ``test_the_pipeline_module_never_reads_a_manifest``
reads this module's own source, because an existence check on a manifest
file is the same defect in its cheapest form.

Three stages have no skip at all, and the summary says so rather than
pretending otherwise: ``discover`` fetches the match list every time (seeing
the new matches is the point of it), ``select`` rewrites the selection file
every time, and ``render`` writes a report every time (a skip would leave the
caller without the file they asked for). All three say so in their own module
documentation.

Which lineup is the subject
---------------------------
The chain is **team -> demos -> team**, and the two ends do not speak the
same id. ``discover`` and ``select`` take a team; ``classify``, ``aggregate``
and ``render`` take a *lineup key*, which is a hash of the five players who
played one map and therefore changes on a single substitution. Choosing the
subject lineup is the decision a person makes today by reading
``classify``'s lineup listing and recognising the names -- that is, it is
one of the seven hand-offs this module removes.

It is made by the rule that already exists,
:func:`~pappascout.domain.teams.assign_lineup_keys`, with the archive's own
threshold ``[thresholds].team_identity_min_common`` -- the same rule
``discover`` uses to fill ``index/teams.json``'s ``lineup_keys``. It is
applied here to **one demo at a time**, because the index is written before
this run parses anything and therefore cannot know a lineup that this run
has just observed for the first time.

Layering
--------
The arrow is ``cli -> stages -> {domain, adapters, archive}``. This module is
in ``stages`` and calls the other stages; the other stages still call nobody
(``tests/test_stage_pipeline.py::test_no_stage_but_the_pipeline_runs_another_stage``).
It is handed the whole ``Settings`` and gives each stage **only its own
section** (AD-3), which is what the nine commands do one at a time.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Final, Literal

import polars as pl

from pappascout.adapters.protocols import DemoParser, DemoSource, MatchSource
from pappascout.archive.atomic_write import host_tag
from pappascout.archive.paths import RESULT_AREAS, ArchivePaths, parsed_table
from pappascout.domain.models import Settings
from pappascout.domain.teams import Team, assign_lineup_keys
from pappascout.errors import PappascoutError, SettingsError
from pappascout.stages import StageResult
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages import classify as classify_stage
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import parse as parse_stage
from pappascout.stages import render as render_stage
from pappascout.stages import select as select_stage

__all__ = [
    "STAGE",
    "OUTCOMES",
    "NO_SKIP_STAGES",
    "Outcome",
    "Step",
    "PipelineRun",
    "RunStopped",
    "run",
    "conflict_copies",
    "subject_lineups",
    "subject_candidates",
    "aggregate_key",
    "lineups_left_out",
]

STAGE = "pipeline"

#: What happened to one unit of one stage. Four words, printed literally, so
#: that the reader tells them apart without inferring anything.
#:
#: ``ran``
#:     The stage did the work and returned a result.
#: ``skipped``
#:     The work was not needed and was not done. **The stage decided this,
#:     not this module** -- either by comparing its own manifest and
#:     returning ``skipped=True``, or, for the demos ``fetch.plan`` reports
#:     as already in the archive, by the plan that stage builds.
#: ``failed``
#:     The unit did not come through. The reason and the next step are on the
#:     step, and the chain carried on with the other units (AD-9).
#: ``no-units``
#:     The stage had nothing to do, and the reason says why. Different from
#:     ``skipped``: nothing was compared, because there was nothing to
#:     compare. A stage that vanished from the summary in this case would
#:     leave the reader to work out whether it ran at all.
OUTCOMES: Final[tuple[str, ...]] = ("ran", "skipped", "failed", "no-units")
Outcome = Literal["ran", "skipped", "failed", "no-units"]

#: The stages that have no skip at all and never will.
#:
#: One list, because the sentence the summary prints about them and the
#: behaviour of the three modules have to be able to fail together. Until
#: 2026-09-10 the sentence was pinned only as text in one test and the
#: behaviour only in the three stages' own tests, so either could change
#: without the other going red. Now ``tests/test_stage_pipeline.py``'s
#: ``test_the_stages_named_as_skipless_never_return_a_skip`` reads the three
#: modules' source against this list, and the command builds the sentence
#: from it.
#:
#: ``discover`` fetches the match list every run (seeing the new matches is
#: the point of it), ``select`` rewrites the selection file every run, and
#: ``render`` writes a report every run -- a skipped report would leave the
#: caller without the file they asked for. All three say so in their own
#: module documentation.
NO_SKIP_STAGES: Final[tuple[str, ...]] = (
    discover_stage.STAGE,
    select_stage.STAGE,
    render_stage.STAGE,
)

#: The ``UnitStatus`` values that are **not** a failure of the unit.
#:
#: Explicit, and its complement is pinned by a test, so that a value added to
#: ``constants.UNIT_STATUSES`` cannot fall through a default into whichever
#: word happens to read plausibly.
#:
#: ``pruned`` is here and it is the whole reason the set exists. AD-1 defines
#: it as "the input file is gone, the result and its manifest are still
#: current, and the result is **not** overwritten" -- a result that stands,
#: not a unit that did not come through. Calling it ``failed`` would tell the
#: reader to go and fix a demo that the archive deliberately no longer keeps.
_NOT_A_FAILURE: Final[frozenset[str]] = frozenset({"ok", "pruned"})

#: The unit is the whole team, not one demo. Used where a stage's unit is the
#: team itself and there is no id to print.
TEAM_UNIT = "-"

#: The error families that are a property of the **unit** and not of the code.
#:
#: The same three as ``stages.parse``'s ``_RECORDED_ERRORS`` and the two
#: ``stages.fetch.run_many`` catches: the tool's own errors, the disk's, and
#: the table reader's. A full disk, a file the sync client holds open and a
#: recording that will not parse are all situations in which **the next unit
#: may very well succeed**.
#:
#: A programming error (``TypeError`` and the rest) is deliberately not here.
#: It is not a property of the unit, and hiding it among eleven successful
#: demos is exactly what AD-9 does not ask for. The list is compared against
#: ``parse._RECORDED_ERRORS`` by a test, so the two cannot drift apart.
UNIT_ERRORS: Final[tuple[type[BaseException], ...]] = (
    PappascoutError,
    OSError,
    pl.exceptions.PolarsError,
)

#: The advice on a unit whose fault carries none of its own.
DEFAULT_NEXT_STEP = (
    "Read the reason above and run the single-stage command for this unit "
    "again to see the whole error."
)


# -- What one run is made of -----------------------------------------------


@dataclass(frozen=True)
class Step:
    """One stage over one unit, and how it went.

    A step is **not** a :class:`~pappascout.stages.StageResult`, and the
    difference is the point of the type. ``StageResult.status`` is
    ``UnitStatus``, a closed enumeration shared with the Parquet schema
    (``domain.schemas``): it has a value for a download that failed and one
    for a recording that would not parse, and none at all for "the stage
    raised before it could return anything". Extending it would change the
    contract of the tables already in the archive for the sake of a console
    summary, so the outcome lives here and the stage's own result travels
    along untouched.

    Attributes:
        stage: The stage's name, ``"discover"``, ``"parse"`` and so on.
        unit: The unit, or :data:`TEAM_UNIT` when the stage has no id to
            print.
        outcome: One of :data:`OUTCOMES`.
        reason: Why it failed, why it was skipped, or why there was nothing
            to do. ``None`` only on a plain ``ran``.
        next_step: What the reader has to do about a failure. Never empty on
            a ``failed`` step -- :func:`_failed` refuses one without it, for
            the same reason ``fetch._result`` does: a failure with no advice
            makes the output invent some.
        detail: A short line of the stage's own numbers, for the summary.
        result: The stage's own result when it returned one, otherwise
            ``None``. This is where ``status``, ``outputs`` and
            ``manifest_path`` are read from.
    """

    stage: str
    unit: str
    outcome: Outcome
    reason: str | None = None
    next_step: str | None = None
    detail: str | None = None
    result: StageResult | None = None
    duration_s: float = 0.0


@dataclass(frozen=True)
class PipelineRun:
    """Everything one ``scout`` run did, in the order it did it.

    Attributes:
        team: The team exactly as the caller wrote it.
        team_key: The canonical team id the name resolved to.
        steps: Every step, in order. The whole record of the run.
        lineup_key: The subject lineup the report was written for, or
            ``None`` if the run never got that far.
        conflicts: Files in the archive whose names have the shape a sync
            client's conflict copy takes (AD-7). Reported as an error at the
            end of the run. ``None`` means **the check did not run**, which
            is not the same as finding nothing: a run that was cancelled at
            the download question never reached it, and an empty tuple there
            would be a clean result the check never gave.
        lineups_left_out: The subject's lineups that had classified rounds
            but did not reach the report -- see :func:`lineups_left_out`.
            ``None`` again means the comparison could not be made, for the
            same reason and with the same rule: a check that did not run
            must not look like a check that came back clean.
        duration_s: Wall-clock seconds for the whole run. SM-1 is measured
            against this command, and every other command prints its time.
    """

    team: str
    team_key: str
    steps: tuple[Step, ...] = ()
    lineup_key: str | None = None
    conflicts: tuple[PurePosixPath, ...] | None = None
    lineups_left_out: tuple[str, ...] | None = None
    duration_s: float = 0.0

    @property
    def results(self) -> tuple[StageResult, ...]:
        """Every ``StageResult`` the run collected, in order."""
        return tuple(s.result for s in self.steps if s.result is not None)

    def count(self, outcome: str) -> int:
        """How many steps ended with this outcome."""
        return sum(1 for s in self.steps if s.outcome == outcome)

    @property
    def report(self) -> PurePosixPath | None:
        """The Markdown report this run wrote, if it wrote one."""
        for step in reversed(self.steps):
            if step.stage == render_stage.STAGE and step.result is not None:
                outputs = step.result.outputs
                return outputs[0] if outputs else None
        return None


class RunStopped(PappascoutError):
    """The run could not go on, and the steps taken so far travel with it.

    The exception is the ordinary :class:`~pappascout.errors.PappascoutError`
    contract -- same message, same advice -- so the command line reports it
    exactly as it reports any other. The addition is :attr:`run`: without it
    the summary of everything that *did* happen would be lost at the moment
    it is most worth reading, because the caller would see the last error and
    nothing about the eleven units that came through before it.

    Attributes:
        cause: The error that stopped the run.
        run: The run as far as it got.
    """

    def __init__(self, cause: PappascoutError, run: PipelineRun) -> None:
        super().__init__(str(cause), advice=getattr(cause, "advice", None))
        self.cause = cause
        self.run = run


# -- The chain ---------------------------------------------------------------


def run(
    settings: Settings,
    archive: ArchivePaths,
    team: str,
    *,
    match_source: MatchSource,
    demo_source: Callable[[], DemoSource],
    parser: Callable[[], DemoParser],
    before_download: Callable[[fetch_stage.FetchPlan, PipelineRun], None]
    | None = None,
    now: datetime | None = None,
) -> PipelineRun:
    """Run the whole chain for one team and return what happened.

    Args:
        settings: The whole settings file. Each stage is handed **only its
            own section** (AD-3) here, exactly as the single-stage commands
            hand it over one at a time.
        archive: The archive's paths.
        team: The team's name, an unambiguous part of it, or the team id.
        match_source: The ``MatchSource`` port ``discover`` reads the
            division with. Built by the caller and **not lazily**: a missing
            interface key must stop the run before anything is written, and
            building the port is where that is noticed.
        demo_source: A factory for the ``DemoSource`` port. A factory and not
            a port, because building it needs the download authorisation, and
            a missing authorisation is a status of the ``fetch`` units rather
            than the end of the run -- the chain goes on to parse and
            classify what is already in the archive.
        parser: A factory for the ``DemoParser`` port. A factory because it
            loads the parsing library, which a run with nothing to parse
            should not pay for. **Called once**, before the first demo, and
            not at all when there is no demo to parse. A factory that raises
            is a fault of the run and not of a demo: every unit gets a
            ``failed`` row and the fault is written out once.
        before_download: Called once before the first download, and only
            when there is something to download, with the plan and with the
            run as far as it has got. **Once for the whole plan, never once
            per demo**: the quota and the disk space are spent by the plan as
            a whole, so that is what the caller shows, checks and -- unless
            the run is unattended -- asks about. The shell owns the question,
            because the shell is the only layer that has a user in front of
            it; raising from here cancels the run.

            The run travels with the plan because the shell needs it to say
            what the two stages before the question already did. Without it a
            cancelled run prints the question's answer and nothing else, and
            a caller cannot tell that from a run in which nothing happened.
        now: The clock for the report's file name. For the tests.

    Returns:
        :class:`PipelineRun`.

    Raises:
        RunStopped: Whenever the run cannot go on **after ``discover`` has
            resolved the team** -- ``select`` could not write its selection,
            the selection file cannot be read into a plan, the caller
            cancelled at the download question, no demo of the sample is in
            the archive and nothing of the team's has ever been classified,
            the aggregation or the rendering failed, or the archive holds
            conflict copies (AD-7). The steps taken so far are on the
            exception, which is the whole point of the type: this
            environment's own commonest fault is the sync client holding a
            file open, and it strikes ``select`` as readily as anything else.
        ~pappascout.errors.PappascoutError: Plainly, without a run attached,
            only from the very first step: a missing interface key (raised
            where the caller builds the match port), a fault in ``discover``
            itself, or a team name that matches no team or more than one.
            There is no summary to lose at that point -- ``discover`` has
            written its two indexes, which is deliberate and its own
            documented behaviour, but no stage has produced a result to
            report.
    """
    started = time.perf_counter()
    steps: list[Step] = []
    subject = _subject_team(archive, team, steps, match_source, settings)
    team_key = subject.team_key

    def snapshot(
        *,
        lineup_key: str | None = None,
        conflicts: tuple[PurePosixPath, ...] | None = None,
        left_out: tuple[str, ...] | None = None,
    ) -> PipelineRun:
        return PipelineRun(
            team=team,
            team_key=team_key,
            steps=tuple(steps),
            lineup_key=lineup_key,
            conflicts=conflicts,
            lineups_left_out=left_out,
            duration_s=time.perf_counter() - started,
        )

    def stop(cause: PappascoutError) -> RunStopped:
        return RunStopped(cause, snapshot(conflicts=conflict_copies(archive)))

    try:
        steps.append(
            _step_of(
                select_stage.run(
                    settings.league,
                    archive,
                    team,
                    thresholds=settings.thresholds,
                ),
                detail=_select_detail,
            )
        )
        plan = fetch_stage.plan(archive, team_key)
        _fetch_steps(archive, plan, demo_source, before_download, snapshot, steps)
    except PappascoutError as exc:
        # ``select`` writes the selection file and ``fetch.plan`` reads it
        # back, so both go through the disk -- and on this archive the disk
        # is a synchronised folder that can hold a file open. The disk gate
        # inside ``before_download`` raises here too. None of the three is
        # "the very first step", and a summary of what did happen is worth
        # more than the exception on its own.
        raise stop(exc) from exc

    # The join between ``select`` and the per-demo stages is the plan, and it
    # already exists: the sample's units that are in the archive **now**,
    # whether they arrived by download or by hand (AD-8). ``in_archive`` is
    # the same reader ``fetch`` decides idempotence with, so the pipeline and
    # the stage cannot disagree about what "is there" means.
    units = [
        unit
        for unit in (*plan.present, *plan.pending)
        if fetch_stage.in_archive(archive, unit)
    ]
    parsed = _parse_steps(settings, archive, units, parser, steps)
    lineups = _classify_steps(settings, archive, parsed, subject, steps)

    try:
        candidates = subject_candidates(archive, (*lineups, *subject.lineup_keys))
        key = aggregate_key(archive, candidates)
    except OSError as exc:
        # A fourth call over the same disk: ``aggregate.team_keys`` lists
        # ``classified/``. Not in the review's list of three, but measured
        # to be the same hole -- ``Path.iterdir`` raises where
        # ``Path.is_file`` swallows -- and by then the run has parsed and
        # classified everything, which is the summary most worth keeping.
        raise stop(
            PappascoutError(
                f"The archive's classified rounds could not be listed: {exc}",
                advice=(
                    "Something is holding the folder open. Wait for the sync "
                    "client to settle and run scout again."
                ),
            )
        ) from exc
    if key is None:
        raise stop(_nothing_to_report(team, subject, plan, steps))

    try:
        aggregated = _step_of(
            aggregate_stage.run(
                settings.thresholds,
                settings.league,
                archive,
                key,
                aggregate_settings=settings.aggregate,
            ),
            detail=_aggregate_detail,
        )
        steps.append(aggregated)
        steps.append(
            _step_of(
                render_stage.run(settings.report, archive, key, now=now),
                detail=_render_detail,
            )
        )
    except PappascoutError as exc:
        # **Not swallowed.** These two are the end of the chain: without an
        # aggregate there is no report, and that is "nothing can proceed at
        # all" rather than one unit failing. The summary is printed all the
        # same, because the run may have parsed and classified a dozen demos
        # before this.
        raise stop(exc) from exc

    finished = snapshot(
        lineup_key=key,
        conflicts=conflict_copies(archive),
        left_out=lineups_left_out(candidates, aggregated),
    )
    if finished.conflicts:
        raise RunStopped(_conflict_error(finished.conflicts), finished)
    return finished


def _subject_team(
    archive: ArchivePaths,
    team: str,
    steps: list[Step],
    match_source: MatchSource,
    settings: Settings,
) -> Team:
    """Run ``discover`` and read the team it resolved to out of the index.

    The team is read from the index the stage has just written, through
    ``discover``'s own public reader, so the pipeline and the stage answer
    the question "which team is this" with the same code. The whole
    :class:`~pappascout.domain.teams.Team` is needed and not only its id: the
    standing roster is what joins a demo's lineup to this team.
    """
    steps.append(
        _step_of(
            discover_stage.run(
                settings.league,
                archive,
                team,
                source=match_source,
                thresholds=settings.thresholds,
            ),
            detail=_discover_detail,
        )
    )
    teams = discover_stage.teams_from_index(discover_stage.read_teams_index(archive))
    return discover_stage.resolve_team(teams, team)


# -- fetch -------------------------------------------------------------------


#: The fallback advice when the whole download stage is blocked.
_FETCH_BLOCKED_NEXT_STEP = (
    "No demo can be downloaded until this is fixed. The supported way past "
    "it is a hand import: uv run pappascout import --match <match_id> "
    "--map <map_no>"
)


def _fetch_steps(
    archive: ArchivePaths,
    plan: fetch_stage.FetchPlan,
    demo_source: Callable[[], DemoSource],
    before_download: Callable[[fetch_stage.FetchPlan, PipelineRun], None] | None,
    snapshot: Callable[..., PipelineRun],
    steps: list[Step],
) -> None:
    """Download the sample's missing demos, and give every one of them a row.

    **A row for each, no silent shortening.** The plan names two groups and
    both get lines: the demos it found already in the archive are
    ``skipped`` -- that is the stage's own decision, made by
    ``fetch.plan`` -- and the ones it has to fetch get whatever the stage
    returns for them. Until 2026-09-10 the first group had no rows at all,
    so ten demos already on disk and two to fetch produced two ``fetch``
    lines and twelve ``parse`` lines with nothing saying where the ten came
    from.

    A stage-wide refusal -- no download authorisation, or no token at all --
    becomes one ``failed`` step **per demo that did not arrive**, carrying
    the refusal's own message and its own next step. It is not an exception
    here, because it does not stop the chain: everything already in the
    archive is still parsed, classified and reported.

    **A demo that arrived before the refusal is not marked failed.**
    ``fetch.run_many`` raises from inside its loop, after some downloads may
    already have finished, and it says so in the message it attaches. Those
    files are in the archive and they reach the report, so a ``failed`` row
    for them would blame the fault on the units that worked and would
    contradict the report they are in.
    """
    for unit in plan.present:
        steps.append(
            Step(
                stage=fetch_stage.STAGE,
                unit=unit,
                outcome="skipped",
                reason=(
                    "Already in the archive: the download plan found the "
                    "demo and its metadata, so it was not downloaded."
                ),
            )
        )

    if not plan.pending:
        if not plan.present:
            steps.append(
                Step(
                    stage=fetch_stage.STAGE,
                    unit=TEAM_UNIT,
                    outcome="no-units",
                    reason="Nothing to download: the sample is empty.",
                )
            )
        return

    if before_download is not None:
        before_download(plan, snapshot())

    started = time.perf_counter()
    try:
        source = demo_source()
        results = fetch_stage.run_many(archive, plan.pending, source=source)
    except SettingsError as exc:
        # The two ways the **whole** stage is blocked: no authorisation to
        # download at all, and no token to try with. Both mean that not one
        # unit can succeed, which is why ``fetch`` raises rather than
        # returning statuses -- and why the pipeline turns it back into one
        # status per unit instead of ending the run. Everything else
        # ``run_many`` already reports as a unit's status.
        steps.extend(
            _refused(archive, plan.pending, exc, time.perf_counter() - started)
        )
        return
    steps.extend(_step_of(result, detail=_fetch_detail) for result in results)


def _refused(
    archive: ArchivePaths,
    units: Sequence[str],
    exc: PappascoutError,
    duration_s: float,
) -> list[Step]:
    """The rows for a download stage that was refused part of the way through.

    ``fetch.run_many`` raises from **inside** the loop and keeps nothing: the
    results it had collected go with the frame. So what arrived is measured
    from the archive instead, with ``fetch.in_archive`` -- the same reader
    the plan and this module already use, and the plan had just reported
    every one of these demos as missing. A demo that is there now got there
    in this run.

    Its row says ``ran`` and carries no size, because the size was in the
    result that was lost. An invented zero would read as a measurement.
    """
    arrived = [unit for unit in units if fetch_stage.in_archive(archive, unit)]
    blocked = [unit for unit in units if unit not in set(arrived)]
    rows = [
        Step(
            stage=fetch_stage.STAGE,
            unit=unit,
            outcome="ran",
            reason=(
                "Downloaded before the download stage was refused. The "
                "stage's own numbers were lost with the refusal, so there "
                "is no size to report for this demo."
            ),
        )
        for unit in arrived
    ]
    if not blocked:
        # Every promised demo arrived and the refusal still happened. The
        # fault must not vanish just because no demo is left to hang it on.
        return [
            *rows,
            _failed(
                fetch_stage.STAGE,
                TEAM_UNIT,
                reason=str(exc),
                next_step=_advice(exc, _FETCH_BLOCKED_NEXT_STEP),
                duration_s=duration_s,
            ),
        ]
    return [*rows, *_blocked(fetch_stage.STAGE, blocked, exc, duration_s=duration_s)]


def _blocked(
    stage: str,
    units: Sequence[str],
    exc: BaseException,
    *,
    fallback: str = _FETCH_BLOCKED_NEXT_STEP,
    duration_s: float = 0.0,
) -> list[Step]:
    """Every promised unit gets a row, and the fault is spelled out once.

    **A row for each, no silent shortening**, which is
    ``fetch._not_attempted``'s rule and its reason: the plan promised a
    number of demos, and a shorter listing would leave the reader working
    out where the rest went.

    The fault itself is a property of the credential, or of the library, and
    not of any one demo, so it is written out on the first row and referred
    to on the others. Repeating a five-line message twelve times would bury
    the summary the caller is supposed to read, and the second copy adds
    nothing the first did not say.
    """
    advice = _advice(exc, fallback)
    later = (
        f"Not attempted: the whole {stage} stage is blocked by the fault "
        f"reported on {units[0]}. It is not a property of this demo, so the "
        "same fault would repeat and no attempt was made."
    )
    return [
        _failed(
            stage,
            unit,
            reason=str(exc) if index == 0 else later,
            next_step=advice,
            duration_s=duration_s if index == 0 else 0.0,
        )
        for index, unit in enumerate(units)
    ]


# -- parse -------------------------------------------------------------------


def _parse_steps(
    settings: Settings,
    archive: ArchivePaths,
    units: Sequence[str],
    parser: Callable[[], DemoParser],
    steps: list[Step],
) -> list[str]:
    """Parse every demo of the sample that is in the archive.

    Returns the ids that have a parsed result afterwards -- the ones the
    stage parsed **and** the ones it skipped because its manifest already
    matched. A skipped result is a result; leaving it out would classify a
    demo only on the run that parsed it.

    The stage is called for every unit, every time. Whether the work is
    needed is the stage's decision and it is made from its manifest; a
    comparison here would be the second implementation of freshness AD-1
    exists to prevent.
    """
    if not units:
        steps.append(
            Step(
                stage=parse_stage.STAGE,
                unit=TEAM_UNIT,
                outcome="no-units",
                reason=(
                    "No demo of the sample is in the archive, so there is "
                    "nothing to parse."
                ),
            )
        )
        return []

    # **The factory is called once, before the loop, and its failure is not
    # the first demo's.** It was inside the try until 2026-09-10, so a
    # factory that raised was called again for every unit and printed its
    # whole error block once per demo -- the very thing ``_blocked`` exists
    # to avoid for ``fetch``. ``ImportError`` is caught beside the unit
    # errors because ``parse.default_parser`` imports the parsing library
    # inside itself: a library that is not installed is a run-wide fault, and
    # it is not in ``UNIT_ERRORS`` (nor should it be -- for a *unit* it would
    # be a programming error).
    started = time.perf_counter()
    try:
        built = parser()
    except (*UNIT_ERRORS, ImportError) as exc:
        steps.extend(
            _blocked(
                parse_stage.STAGE,
                units,
                exc,
                fallback=(
                    "No demo can be parsed until this is fixed. The parser "
                    "is built once for the whole run, so the same fault "
                    "would repeat on every demo. Check that the parsing "
                    "library is installed: uv sync"
                ),
                duration_s=time.perf_counter() - started,
            )
        )
        return []

    ready: list[str] = []
    for unit in units:
        started = time.perf_counter()
        try:
            _, demo_path = parse_stage.resolve_demo(archive, unit)
            result = parse_stage.run(
                settings.parse,
                archive,
                unit,
                built,
                demo_path=demo_path,
            )
        except UNIT_ERRORS as exc:
            steps.append(
                _failed(
                    parse_stage.STAGE,
                    unit,
                    reason=str(exc),
                    next_step=_advice(
                        exc,
                        _retry_unit(f"uv run pappascout parse {unit} --force"),
                    ),
                    duration_s=time.perf_counter() - started,
                )
            )
            continue
        steps.append(_step_of(result, detail=_parse_detail))
        ready.append(unit)
    return ready


# -- classify ----------------------------------------------------------------


def _classify_steps(
    settings: Settings,
    archive: ArchivePaths,
    units: Sequence[str],
    subject: Team,
    steps: list[Step],
) -> list[str]:
    """Classify every parsed demo from the subject team's point of view.

    Returns the lineup keys the classification was written under. They are
    what the aggregation is addressed by, and there can be several: the key
    is a hash of the five players who played the map, so a substitution
    between two matches produces a second one for the same team.

    A stage with nothing to do still gets a line. A stage that vanished from
    the summary would leave the reader to work out from an absence whether
    it ran, and an absence is exactly what the caller cannot read.
    """
    if not units:
        steps.append(
            Step(
                stage=classify_stage.STAGE,
                unit=TEAM_UNIT,
                outcome="no-units",
                reason=(
                    "No demo of the sample was parsed in this run, so there "
                    "is nothing new to classify. What the archive already "
                    "holds is what the aggregation below works from -- and "
                    "whether that is anything is the next line's answer, "
                    "not this one's."
                ),
            )
        )
        return []

    written: list[str] = []
    for unit in units:
        started = time.perf_counter()
        try:
            found = subject_lineups(
                archive, unit, subject, settings.thresholds.team_identity_min_common
            )
        except UNIT_ERRORS as exc:
            steps.append(
                _failed(
                    classify_stage.STAGE,
                    unit,
                    reason=str(exc),
                    next_step=_advice(
                        exc,
                        _retry_unit(f"uv run pappascout parse {unit} --force"),
                    ),
                    duration_s=time.perf_counter() - started,
                )
            )
            continue

        if len(found) != 1:
            steps.append(
                _failed(
                    classify_stage.STAGE,
                    unit,
                    reason=_lineup_problem(found, subject, unit),
                    next_step=(
                        "Look at the demo's lineups with: uv run pappascout "
                        f"classify {unit}"
                    ),
                    duration_s=time.perf_counter() - started,
                )
            )
            continue

        lineup = found[0]
        try:
            result = classify_stage.run(
                settings.thresholds,
                settings.league,
                archive,
                unit,
                lineup,
                economy=settings.economy,
            )
        except UNIT_ERRORS as exc:
            steps.append(
                _failed(
                    classify_stage.STAGE,
                    unit,
                    reason=str(exc),
                    next_step=_advice(
                        exc,
                        _retry_unit(
                            f"uv run pappascout classify {unit} "
                            f"--team {lineup}"
                        ),
                    ),
                    duration_s=time.perf_counter() - started,
                )
            )
            continue
        steps.append(_step_of(result, detail=_classify_detail))
        written.append(lineup)
    return written


def subject_lineups(
    archive: ArchivePaths,
    map_demo_id: str,
    subject: Team,
    min_common: int,
) -> tuple[str, ...]:
    """Which of the demo's lineups belong to the subject team.

    **The rule is not invented here.** It is
    :func:`~pappascout.domain.teams.assign_lineup_keys` with the archive's
    own ``[thresholds].team_identity_min_common``, the same rule and the same
    number ``discover`` fills ``index/teams.json``'s ``lineup_keys`` with. A
    second rule for the same question would let two parts of the run disagree
    about whose demo this is.

    It is applied to one demo rather than read out of the index because the
    index is written at the head of this run, before anything has been
    parsed. A demo parsed for the first time in this very run has a lineup
    the index cannot know yet, and asking the index would fail exactly on the
    demos the run has just made available.

    Returns:
        The keys, in the order the rule assigned them. Empty means the team
        did not play this demo; more than one means genuine ambiguity, and
        neither is settled here by drawing lots.

    Raises:
        OSError: If the demo's lineup table cannot be read from the disk.
        polars.exceptions.PolarsError: If it is there but will not open. The
            caller turns both into this demo's own ``failed`` step -- see
            :func:`_lineup_members` for why they are not an empty answer.
    """
    return assign_lineup_keys(
        (subject,), _lineup_members(archive, map_demo_id), min_common
    )[0][0].lineup_keys


def _lineup_members(
    archive: ArchivePaths, map_demo_id: str
) -> dict[str, set[str]]:
    """The demo's lineups: ``lineup_key`` -> the players' game ids.

    The source is ``lineups.parquet``, the same table ``discover`` and
    ``aggregate`` read the members from, and for the same reason: its set of
    players is **exactly the one** the lineup key was computed from.

    **A table that is not there and a table that will not open are two
    different answers, and they were one until 2026-09-10.** A missing table
    gives an empty mapping: it is a demo whose parse produced no lineups,
    and the caller turns that into the unit's own ``failed`` step with its
    own next step, which is what AD-9 asks for. A table that exists and
    cannot be read is a **read error**, and it is raised: swallowing it
    produced the sentence "the demo is another team's, or the roster is out
    of date" -- neither of which was true -- and advised ``classify <id>``,
    which lists nothing, because the same table would not open for it
    either. The caller catches it (``UNIT_ERRORS``) into this demo's own
    ``failed`` step with the reader's real fault in it, so the other demos
    are not taken down with it.

    Raises:
        OSError: If the table cannot be read from the disk.
        polars.exceptions.PolarsError: If it is there but not a readable
            table.
    """
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    if not path.is_file():
        return {}
    frame = pl.read_parquet(path, columns=["lineup_key", "player_id"])
    members: dict[str, set[str]] = {}
    for row in frame.unique().iter_rows(named=True):
        members.setdefault(str(row["lineup_key"]), set()).add(str(row["player_id"]))
    return members


def _lineup_problem(found: Sequence[str], subject: Team, unit: str) -> str:
    """Why the subject lineup could not be chosen for this demo."""
    if not found:
        return (
            f"Not one lineup of demo {unit} shares enough players with the "
            f"standing roster of team {subject.display_name}, so the demo "
            "cannot be classified from this team's point of view. Either the "
            "demo is another team's, or the roster in the index is out of "
            "date."
        )
    listed = ", ".join(found)
    return (
        f"{len(found)} lineups of demo {unit} share enough players with the "
        f"standing roster of team {subject.display_name} ({listed}). That is "
        "genuine ambiguity and it is not settled by drawing lots."
    )


# -- aggregate ---------------------------------------------------------------


def subject_candidates(
    archive: ArchivePaths, lineups: Iterable[str]
) -> tuple[str, ...]:
    """The subject's lineups that the archive actually has classified rounds for.

    The run's own answer to "which lineups are this team's" is
    :func:`subject_lineups`, applied per demo, plus whatever the team index
    already knew. Only the ones with a ``classified/<key>/`` directory can be
    aggregated, which is what ``aggregate.team_keys`` answers.

    Returns:
        The keys, sorted, without duplicates.
    """
    return tuple(sorted(set(lineups) & set(aggregate_stage.team_keys(archive))))


def aggregate_key(
    archive: ArchivePaths, lineups: Iterable[str]
) -> str | None:
    """The lineup key the aggregation and the report are addressed by.

    The aggregation takes **one** key and gathers the team's other lineups
    itself (``aggregate.collect_team`` ->
    ``domain.aggregate.lineups_of_same_team``).

    **It does not necessarily gather all of them, and this function cannot
    make it.** The two ends of the run answer "is this lineup the subject's"
    with two different rules: :func:`subject_lineups` compares a lineup with
    the team's **standing roster**, and ``lineups_of_same_team`` compares it
    with the **chosen target lineup**, never chained. A standing roster is
    larger than any one lineup, so the two can disagree -- measured with an
    eight-player roster and two five-player lineups sharing two players at
    ``team_identity_min_common = 3``: the first rule returns both, the second
    only the target. Which key is chosen here therefore does decide part of
    what is in the report, and what falls outside it is reported by
    :func:`lineups_left_out` rather than passed over.

    Two rules, in this order:

    1. **A key that already has an aggregate wins.** The archive keeps one
       ``aggregates/<key>/`` and one ``reports/<key>/`` per team, and a run
       that chose a different key would start a second tree beside the one
       the earlier reports are in.
    2. Otherwise the smallest key. Any rule would do here as long as it is
       the same one every time -- a choice that moved between two runs would
       put the second run's report somewhere the first one's reader does not
       look, and the ``aggregate`` stage would never report itself skipped.

    Only keys that actually have classified rounds are candidates, which is
    :func:`subject_candidates`.

    Returns:
        The key, or ``None`` when the team has no classified rounds at all.
    """
    candidates = subject_candidates(archive, lineups)
    if not candidates:
        return None
    established = [
        key for key in candidates if archive.report_json(key).is_file()
    ]
    return (established or candidates)[0]


def lineups_left_out(
    candidates: Sequence[str], aggregated: Step
) -> tuple[str, ...] | None:
    """The subject's classified lineups that did not reach the report.

    **The gap this reports is real and it is not closed here.** The run
    classifies a demo under every lineup that shares
    ``team_identity_min_common`` players with the team's **standing roster**
    (:func:`subject_lineups`); the aggregation joins to the report every
    lineup that shares that many with the **chosen target lineup**
    (``domain.aggregate.lineups_of_same_team``). The second set can be
    smaller, and a lineup outside it is not in the report's
    ``missing_demos`` either -- ``collect_team`` never looks at it, so its
    demos leave no trace at all. The summary would say "aggregate ran / N
    demos" over a sample quietly short of some.

    Reconciling the two rules is not this module's to do: narrowing
    :func:`subject_lineups` would misclassify demos, and widening
    ``lineups_of_same_team`` would change what the ``aggregate`` command
    does today, which this story's own Never forbids. So the run measures
    the difference and prints it, and a reader who sees a name here knows to
    run ``aggregate`` and ``report`` for that key as well.

    The covered set is read from the stage's **own reported result**
    (``stats["lineup_keys"]``), never recomputed here: a second computation
    of "which lineups did the aggregation join" is exactly the kind of
    duplicate rule this whole function exists because of.

    Returns:
        The keys left out, in the candidates' order, or ``None`` if the
        stage did not report which lineups it covered -- a comparison that
        could not be made must not come back looking like a clean one.
    """
    result = aggregated.result
    if result is None:
        return None
    covered = result.stats.get("lineup_keys")
    if not isinstance(covered, (list, tuple, set, frozenset)):
        return None
    inside = {str(key) for key in covered}
    return tuple(key for key in candidates if key not in inside)


def _nothing_to_report(
    team: str,
    subject: Team,
    plan: fetch_stage.FetchPlan,
    steps: Sequence[Step],
) -> PappascoutError:
    """Nothing can proceed: no demo here, and nothing of this team classified.

    The message names **what is missing** and **the command to run next**,
    which is the AD-9 contract for an exception. Today the next command is
    almost always the hand import: the download source is the one thing that
    may be refused outright, and an import is the supported way past it
    (AD-8).
    """
    failures = [s for s in steps if s.outcome == "failed"]
    missing = ", ".join(plan.pending) if plan.pending else "none listed"
    why = (
        f"\nNot one of them could be downloaded: {failures[0].reason}"
        if failures
        else ""
    )
    advice = (
        "uv run pappascout import --match <match_id> --map <map_no>"
        if plan.pending
        else f'uv run pappascout select --team "{team}"'
    )
    return PappascoutError(
        f"Team {subject.display_name} has no classified rounds in the "
        f"archive, so there is nothing to report.\n"
        f"Demos of the sample missing from the archive: {missing}.{why}",
        advice=(
            "Bring a demo into the archive by hand and run scout again: "
            f"{advice}"
        ),
    )


# -- The conflict copies (AD-7) ----------------------------------------------

#: A name in which a parenthesised group follows a space.
#:
#: The shape a sync client gives a copy it could not merge, and a shape
#: nothing in this archive is written with: every name the tool writes goes
#: through ``paths.safe_component``, whose pattern allows neither a space nor
#: a bracket.
_PARENTHESISED = re.compile(r"\s\([^()]*\)")

#: The marker of this tool's own in-flight temporary file.
#:
#: It carries the host name too (``atomic_write.temp_suffix``), so without
#: this exclusion every interrupted write would be reported as a conflict
#: copy. A leftover temporary file is a real thing to notice, but it is not
#: this, and one check that reported both would say neither clearly.
_TEMP_MARKER = ".tmp-"


def conflict_copies(archive: ArchivePaths) -> tuple[PurePosixPath, ...]:
    """Files in the tool's result areas whose names have a conflict copy's shape.

    AD-7 has promised this check since 2026-08-27 and it has had nowhere to
    live, because the module that was to run it at the end of a run did not
    exist. This is that module.

    The archive is a folder two machines share through a sync client, and the
    lock that would keep two runs out of each other's way is advisory at
    best: a sync delay means both machines can believe they hold it. So the
    writes are atomic and the run **checks afterwards** whether the folder
    forked anyway. A forked file is not a crash; it is two versions of a
    result where the manifests claim there is one, and the reader would take
    whichever the tool happened to open.

    **Only the areas a run writes results into** (``paths.RESULT_AREAS``).
    The claim the check makes is "the tool wrote this file, and here is a
    second one" -- which is false of everything else in the archive.
    ``import/`` is the human's inbox and holds hand-named files; with no
    download authorisation it is the only route a demo takes, and a browser's
    second copy of a hand-fetched demo would have ended every run with a
    nonzero exit and the advice to "keep the one you want" about a file that
    is not a pair of anything. ``raw/faceit`` is a cache AD-7 says may be
    deleted at any time, and ``logs/`` is per-machine by design. Measured
    2026-09-10 on the real archive, those three produced every hit the
    unscoped walk found.

    Two shapes, and they are the sync product's observable convention rather
    than a property of anyone's machine, which is the boundary AD-12 draws:

    * a parenthesised group after a space -- ``teams (1).json``,
      ``report (this machine's conflicting copy).md``;
    * this machine's name appended to the stem -- ``teams-<host>.json``.

    **What the second shape can and cannot see, honestly.** A conflict copy
    is named after the machine whose version lost, and the only machine name
    this run can produce is its own. Nothing in the tool ever creates
    ``logs/<host>/`` -- measured across the whole source tree on 2026-09-10,
    and ``logs/`` in the real archive is empty -- so an earlier attempt to
    learn the *other* machine's name from that directory could never fire,
    and read as a guard while guarding nothing. **A copy made by the other
    machine is therefore caught only by the bracket shape**, which is the
    shape this sync client is observed to use anyway. Guessing at machine
    names is the alternative and it is worse: it would flag ordinary files,
    and a check that cries wolf is a check nobody reads. This limit is
    pinned in ``tests/test_stage_pipeline.py`` by
    ``test_a_copy_named_after_another_machine_is_caught_only_by_its_brackets``.

    An unreadable entry is skipped rather than raised on: the same reason
    ``paths.total_size_bytes`` guards the same walk -- a cloud placeholder,
    or a file still being transferred. This runs after the report is
    written, and an exception here would take a finished run down with it.

    Returns:
        The paths relative to the archive root, sorted. Empty is the normal
        answer.
    """
    root = archive.root
    if not root.is_dir():
        return ()
    host = host_tag()
    found: list[PurePosixPath] = []
    for area in RESULT_AREAS:
        directory = archive.resolve(area)
        try:
            if not directory.is_dir():
                continue
            for path in directory.rglob("*"):
                name = path.name
                if _TEMP_MARKER in name:
                    continue
                if not _looks_forked(name, host):
                    continue
                if not path.is_file():
                    continue
                found.append(PurePosixPath(path.relative_to(root).as_posix()))
        except OSError:
            # A cloud placeholder from the sync client, or a file still
            # being transferred. One unreadable entry is not a reason to
            # lose the run's report.
            continue
    return tuple(sorted(found))


def _looks_forked(name: str, host: str) -> bool:
    """Does this file name have one of the two conflict-copy shapes?

    **The host shape is anchored to the end of the stem**, because that is
    where the sync client puts it -- ``teams.json`` becomes
    ``teams-<host>.json``. An unanchored search was a false positive
    waiting for a team slug: if this machine's tag were ``box7``, a team
    called ``box7-gaming`` would produce ``...-box7-gaming.md`` and end
    every run in ``RunStopped``. It is the same shape as the guard this
    repository found in T11, where ``"set"`` matched inside ``"settings"``.
    """
    if _PARENTHESISED.search(name):
        return True
    if not host:
        return False
    stem = PurePosixPath(name.lower()).stem
    return stem.endswith(f"-{host}")


def _conflict_error(conflicts: Sequence[PurePosixPath]) -> PappascoutError:
    """The error the run ends with when the archive has forked."""
    listed = "\n".join(f"    {path}" for path in conflicts)
    return PappascoutError(
        f"The archive holds {len(conflicts)} file(s) whose name has the "
        "shape a sync client gives a copy it could not merge:\n"
        f"{listed}\n"
        "Two versions of the same result are in the archive and the "
        "manifests claim there is one, so the next run could read either.",
        advice=(
            "Compare each pair, keep the one you want, delete the copy, then "
            "run scout again."
        ),
    )


# -- Steps --------------------------------------------------------------------


def _step_of(
    result: StageResult,
    *,
    detail: Callable[[Mapping[str, Any]], str] | None = None,
) -> Step:
    """A step out of a stage's own result. **One mapping, used everywhere.**

    There were two until 2026-09-10: the ``fetch`` results were read through
    ``status`` and everything else only through ``skipped``. Nothing showed,
    because ``fetch`` is the only stage that returns a non-``ok`` status
    today -- but AD-9 gives every stage the same six-value ``UnitStatus``,
    so the first stage that used one would have had its failure printed as
    ``ran``. The mapping belongs in one place for the same reason the
    outcome words do.

    The stage decides all of it: ``skipped`` is the stage's own flag, and
    the status is the stage's own value. See :data:`_NOT_A_FAILURE` for why
    ``pruned`` is not a failure.
    """
    if result.status not in _NOT_A_FAILURE:
        return _failed(
            result.stage,
            result.unit,
            reason=f"{result.status}: {result.reason or 'no reason given'}",
            next_step=str(result.stats.get("next_step") or DEFAULT_NEXT_STEP),
            result=result,
            duration_s=result.duration_s,
        )
    return Step(
        stage=result.stage,
        unit=result.unit,
        outcome="skipped" if result.skipped else "ran",
        reason=result.reason,
        detail=None if detail is None else detail(result.stats),
        result=result,
        duration_s=result.duration_s,
    )


def _failed(
    stage: str,
    unit: str,
    *,
    reason: str,
    next_step: str,
    result: StageResult | None = None,
    duration_s: float = 0.0,
) -> Step:
    """A failed step, and **it may not be without advice**.

    The same guard and the same reason as ``fetch._result``'s: without it a
    new failure path could produce a failure with no next step, and the
    summary would have to invent one -- which is exactly the wrong-advice
    defect the advice field was moved into the error to prevent.
    """
    if not next_step.strip():
        raise AssertionError(
            f"The step {stage}/{unit} failed but has no next step. A failure "
            "without advice leaves the reader guessing -- add one."
        )
    return Step(
        stage=stage,
        unit=unit,
        outcome="failed",
        reason=reason,
        next_step=next_step,
        result=result,
        duration_s=duration_s,
    )


def _advice(exc: BaseException, fallback: str) -> str:
    """The fault's own advice, or the caller's fallback sentence.

    The advice comes from the error and not from where the error happened to
    be sorted (see :class:`~pappascout.errors.PappascoutError`). Inferring it
    from a status code or from the words of a message is the wrong-advice
    defect the field was added to prevent, and it would be made here, far
    from where the cause is known.

    The fallback is the caller's, because what to do about a unit that broke
    inside the chain depends on which stage it broke in.
    """
    advice = getattr(exc, "advice", None)
    if isinstance(advice, str) and advice.strip():
        return advice.strip()
    return fallback


def _retry_unit(command: str) -> str:
    """The fallback for a unit that failed inside the chain.

    A command and not a sentence: this is exactly the case where running the
    single stage by hand shows the whole error instead of the one line the
    summary has room for.
    """
    return f"Run this unit on its own to see the whole error: {command}"


# -- The detail column --------------------------------------------------------
#
# One short line of each stage's own numbers.
#
# **The keys are read directly, and where a stage documents that it may not
# have them, the shape it documents is checked first.** ``stats`` is the
# stage's contract with this function, and ``stats.get(name, 0)`` on a key
# the stage always sets would turn a drift between the two into a silent
# zero that looks like a measurement. But a key a stage deliberately omits
# is not drift, and indexing it is a ``KeyError`` -- which is in neither
# ``UNIT_ERRORS`` nor ``fetch``'s catches, so it escapes ``run`` past
# ``RunStopped`` and takes the whole summary with it. That was measured on
# ``parse`` on 2026-09-10 (exit 2, a traceback, not one line of summary,
# after the archive had been written) and it is what the single-stage
# summaries have always done: check ``*_unreadable`` first, and only then
# read the numbers.
#
# Six of the seven stages set every key of theirs on every path they can
# return from -- their ``_stats`` helpers build one literal dict -- so those
# six index directly. ``parse`` is the exception and says so in
# ``_existing_stats``: on a skipped run it reads the finished tables, and a
# table that will not open yields ``{"unreadable": <the error>}`` instead of
# a count, "rather than zeroes -- a row of zeroes would look as though the
# demo held no rounds at all". That state is reachable exactly as this
# archive fails: a sync client holding one Parquet file open.


def _discover_detail(stats: Mapping[str, Any]) -> str:
    return f"{stats['teams']} teams, {stats['matches']} matches"


def _select_detail(stats: Mapping[str, Any]) -> str:
    return f"{stats['accepted']} of {stats['map_demos']} maps in the sample"


def _fetch_detail(stats: Mapping[str, Any]) -> str:
    return fetch_stage.size_fi(int(stats["downloaded_bytes"]))


def _parse_detail(stats: Mapping[str, Any]) -> str:
    """``parse``'s numbers, or what could not be read instead of them.

    The two ``*_unreadable`` keys are ``parse._existing_stats``'s documented
    answer for a table that will not open on a skipped run, and each is
    reported where its own number would have been -- the same rule and the
    same wording as the ``parse`` command's own summary, which checks this
    shape in eight places.
    """
    # **Presence of the number decides, not presence of the excuse.** When
    # neither core table opens, ``_existing_stats`` returns the rounds
    # table's error under ``unreadable`` alone and no ``ticks_unreadable``
    # beside it -- so keying off the excuse would print "0 sample points",
    # which is the row of zeroes that whole design exists to avoid.
    unread = "the table was not read"
    rounds = (
        f"{stats['rounds']} rounds"
        if "rounds" in stats
        else f"no rounds obtained ({stats.get('unreadable') or unread})"
    )
    points = (
        f"{stats['sample_points']} sample points"
        if "sample_points" in stats
        else (
            "no sample points obtained "
            f"({stats.get('ticks_unreadable') or stats.get('unreadable') or unread})"
        )
    )
    return f"{rounds}, {points}"


def _classify_detail(stats: Mapping[str, Any]) -> str:
    return f"{stats['rounds']} rounds, team {stats['team_key']}"


def _aggregate_detail(stats: Mapping[str, Any]) -> str:
    return f"{stats['demos']} demos, {stats['rounds']} rounds"


def _render_detail(stats: Mapping[str, Any]) -> str:
    return f"{stats['lines']} lines, {stats['demos']} demos"
