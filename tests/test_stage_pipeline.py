"""``stages.pipeline`` -- the module that decides the order (AD-1, Story 4.1).

Three properties are locked down here, and they are the three the story was
written for.

* **Failure is a status, not a stop (AD-9).** One demo that cannot be
  fetched, cannot be parsed or cannot be classified must not cost the others
  their place in the report. Several tests below break exactly one unit and
  then assert that the report was still written from the rest.
* **Freshness is decided by the stages, never here.** The pipeline calls
  every stage for every unit and reports what came back. The guard is
  :func:`test_the_pipeline_calls_the_stage_even_when_the_result_is_current`,
  and it exists because the obvious test -- "everything reports skipped" --
  does **not** guard it: a pipeline that compared manifests itself and
  skipped the call would keep that test green while quietly becoming the
  second implementation of freshness AD-1 exists to prevent.
* **A stage still calls nobody.** AD-1's rule is not relaxed by adding this
  module, it is finally implemented, so
  :func:`test_no_stage_but_the_pipeline_runs_another_stage` reads the source
  of every stage and fails if one of them calls another stage's ``run``.

The stages themselves are replaced in the chain tests, so none of these
tests reads a demo or the network. The joins that read real files --
:func:`~pappascout.stages.pipeline.subject_lineups`,
:func:`~pappascout.stages.pipeline.aggregate_key` and
:func:`~pappascout.stages.pipeline.conflict_copies` -- are tested against
real files in a temporary archive, because a fake would prove nothing about
them.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path, PurePosixPath

import polars as pl
import pytest

from pappascout.archive.atomic_write import host_tag
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.models import Settings, load_settings
from pappascout.domain.schemas import LINEUPS
from pappascout.domain.teams import RosterMember, Team
from pappascout.errors import (
    DownloadsAccessDenied,
    PappascoutError,
    ParseError,
    SettingsError,
)
from pappascout.stages import StageResult
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages import classify as classify_stage
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import parse as parse_stage
from pappascout.stages import pipeline
from pappascout.stages import render as render_stage
from pappascout.stages import select as select_stage
from conftest import settings_text

TEAM = "Testijoukkue"
TEAM_KEY = "11111111-2222-3333-4444-555555555555"
LINEUP = "aaaaaaaaaaaaaaaa"
OTHER_LINEUP = "bbbbbbbbbbbbbbbb"
DEMO_A = "1-match-0"
DEMO_B = "1-match-1"
#: The demo a hand import already put in the archive (AD-8). It is what a
#: run with no download authorisation has to work from, so it is what makes
#: "the chain carries on with what is already there" testable at all.
DEMO_C = "1-match-2"

#: Five SteamID64s for the subject team and five for the opponent. Real
#: SteamID64 form, because ``RosterMember`` refuses anything else -- the id
#: is what joins a roster to a demo's lineup table, so a made-up shape would
#: make the join untestable.
OURS = tuple(str(76561197977479426 + n) for n in range(5))
THEIRS = tuple(str(76561198000000000 + n) for n in range(5))


def subject(**overrides) -> Team:
    """The subject team, with a five-player standing roster."""
    team = Team(
        team_key=TEAM_KEY,
        name=TEAM,
        roster=tuple(RosterMember(game_player_id=pid) for pid in OURS),
    )
    return replace(team, **overrides) if overrides else team


def settings_for(tmp_path: Path) -> tuple[Settings, ArchivePaths]:
    """The real settings with the archive pointed at a temporary directory."""
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(tmp_path / "arkisto"), encoding="utf-8")
    settings = load_settings(target, env_files=())
    return settings, ArchivePaths.from_settings(settings.project.archive_root)


def result(stage: str, unit: str, **overrides) -> StageResult:
    """A plain ``ok`` result for a stage, with that stage's own stats."""
    stats: dict[str, object] = {
        discover_stage.STAGE: {"teams": 12, "matches": 30},
        select_stage.STAGE: {"accepted": 2, "map_demos": 5},
        fetch_stage.STAGE: {"downloaded_bytes": 1024},
        parse_stage.STAGE: {"rounds": 24, "sample_points": 96},
        classify_stage.STAGE: {"rounds": 24, "team_key": LINEUP},
        # ``lineup_keys`` is not decoration: it is how the run measures
        # which of the lineups it classified the aggregation actually
        # joined. ``aggregate._stats`` always sets it, on both its return
        # paths, and ``test_the_aggregate_stage_reports_the_lineups_it_joined``
        # holds the two together.
        aggregate_stage.STAGE: {
            "demos": 2,
            "rounds": 48,
            "lineup_keys": [LINEUP],
        },
        render_stage.STAGE: {"lines": 300, "demos": 2},
    }[stage]
    defaults: dict[str, object] = {
        "stage": stage,
        "unit": unit,
        "status": "ok",
        "skipped": False,
        "stats": dict(stats),
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


class Recorder:
    """Records every call so the order of the chain can be asserted on.

    ``fetched`` is what the fake download actually put in the archive. The
    fixture's ``in_archive`` reads it, because the pipeline now measures
    what arrived before a refusal from the archive itself -- a fixture that
    answered a flat ``True`` would say every demo of a refused download had
    arrived, and the test would pass over the very defect it is for.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fetched: set[str] = set()

    def note(self, stage: str, unit: str) -> None:
        self.calls.append((stage, unit))

    @property
    def stages(self) -> list[str]:
        return [stage for stage, _ in self.calls]


@pytest.fixture
def chain(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Every stage replaced by a recorder that returns a plain ``ok``.

    The archive is not read and no demo is parsed: these tests are about the
    order, the joins and what happens to a unit that fails, and a real stage
    would drown all three in file handling that its own tests already cover.
    """
    recorder = Recorder()

    def discover(_league, _archive, team, *, source, thresholds):
        recorder.note(discover_stage.STAGE, team)
        return result(discover_stage.STAGE, TEAM_KEY)

    def select(_league, _archive, team, *, thresholds):
        recorder.note(select_stage.STAGE, team)
        return result(select_stage.STAGE, TEAM_KEY)

    def run_many(_archive, units, *, source, **_kwargs):
        rows = []
        for unit in units:
            recorder.note(fetch_stage.STAGE, unit)
            recorder.fetched.add(unit)
            rows.append(result(fetch_stage.STAGE, unit))
        return tuple(rows)

    def parse(_settings, _archive, unit, _parser, *, demo_path=None, force=False):
        recorder.note(parse_stage.STAGE, unit)
        return result(parse_stage.STAGE, unit)

    def classify(
        _thresholds, _league, _archive, unit, team, *, economy, force=False
    ):
        recorder.note(classify_stage.STAGE, f"{unit}/{team}")
        return result(classify_stage.STAGE, unit)

    def aggregate(_thresholds, _league, _archive, team, *, aggregate_settings, **_k):
        recorder.note(aggregate_stage.STAGE, team)
        return result(aggregate_stage.STAGE, team)

    def render(_settings, _archive, team, *, now=None):
        recorder.note(render_stage.STAGE, team)
        return result(
            render_stage.STAGE,
            team,
            outputs=(PurePosixPath(f"reports/{team}/2026-09-10T1200-x.md"),),
        )

    monkeypatch.setattr(discover_stage, "run", discover)
    monkeypatch.setattr(select_stage, "run", select)
    monkeypatch.setattr(fetch_stage, "run_many", run_many)
    monkeypatch.setattr(parse_stage, "run", parse)
    monkeypatch.setattr(classify_stage, "run", classify)
    monkeypatch.setattr(aggregate_stage, "run", aggregate)
    monkeypatch.setattr(render_stage, "run", render)

    # The lookups the pipeline makes between the stages. ``resolve_team`` is
    # deliberately left real: an ambiguous name has to fail here exactly as
    # it fails in the single-stage commands.
    monkeypatch.setattr(discover_stage, "read_teams_index", lambda _a: {})
    monkeypatch.setattr(discover_stage, "teams_from_index", lambda _d: (subject(),))
    monkeypatch.setattr(parse_stage, "resolve_demo", lambda _a, u: (u, Path(u)))
    monkeypatch.setattr(pipeline, "subject_lineups", lambda *_a, **_k: (LINEUP,))
    monkeypatch.setattr(aggregate_stage, "team_keys", lambda _a: [LINEUP])
    return recorder


def run_chain(
    tmp_path: Path, *, pending=(DEMO_A,), present=(), recorder=None, **kwargs
):
    """Run the chain with a hand-built download plan.

    ``in_archive`` models the archive rather than answering a constant.
    A demo the plan calls ``present`` is there; a ``pending`` one is there
    only once the fake ``fetch`` has downloaded it. That is what the real
    reader answers, and the pipeline now asks it about a download that was
    refused half way -- so a constant ``True`` here would report demos as
    downloaded that never were, and a constant ``False`` would drop the ones
    that did arrive.
    """
    settings, archive = settings_for(tmp_path)
    plan = fetch_stage.FetchPlan(
        team_key=TEAM_KEY, pending=tuple(pending), present=tuple(present)
    )
    kwargs.setdefault("match_source", object())
    kwargs.setdefault("demo_source", lambda: object())
    kwargs.setdefault("parser", lambda: object())
    monkeypatch = kwargs.pop("monkeypatch")
    override = kwargs.pop("in_archive", None)

    def in_archive(_archive, unit: str) -> bool:
        if override is not None:
            return override
        return unit in set(present) or (
            recorder is not None and unit in recorder.fetched
        )

    monkeypatch.setattr(fetch_stage, "plan", lambda _a, _k: plan)
    monkeypatch.setattr(fetch_stage, "in_archive", in_archive)
    return pipeline.run(settings, archive, TEAM, **kwargs)


# --- The order --------------------------------------------------------------


def test_the_chain_runs_the_seven_stages_in_order(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One command, seven stages, no hand-off left to the caller.

    The order is the whole point of the module: it is the one place allowed
    to know it (AD-1), and a chain that ran classify before parse would
    produce a report from yesterday's tables without saying so.
    """
    run = run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert chain.stages == [
        "discover",
        "select",
        "fetch",
        "parse",
        "classify",
        "aggregate",
        "render",
    ]
    assert run.team_key == TEAM_KEY
    assert run.lineup_key == LINEUP
    assert str(run.report).endswith(".md")


def test_the_run_carries_every_stage_result_it_collected(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stages' own results travel out untouched, beside the outcomes.

    A :class:`~pappascout.stages.pipeline.Step` says what happened; the
    ``StageResult`` inside it says what the stage measured. Both are needed
    and neither replaces the other -- a caller that wanted the manifest path
    or the stats of one stage would otherwise have to re-read the archive.
    """
    run = run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert [r.stage for r in run.results] == [
        "discover",
        "select",
        "fetch",
        "parse",
        "classify",
        "aggregate",
        "render",
    ]
    assert all(isinstance(r, StageResult) for r in run.results)


def test_the_plan_is_the_join_between_select_and_the_demo_stages(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch.plan`` decides which demos the chain works on, not a new rule.

    The plan already knows which of the sample's maps are in the archive and
    which are missing. A second answer to that question in this module would
    be able to disagree with the one ``fetch`` downloads by.
    """
    asked: list[str] = []
    monkeypatch.setattr(
        fetch_stage,
        "plan",
        lambda _a, key: asked.append(key)
        or fetch_stage.FetchPlan(team_key=key, pending=(DEMO_A,), present=(DEMO_B,)),
    )
    monkeypatch.setattr(fetch_stage, "in_archive", lambda _a, _u: True)
    settings, archive = settings_for(tmp_path)
    pipeline.run(
        settings,
        archive,
        TEAM,
        match_source=object(),
        demo_source=lambda: object(),
        parser=lambda: object(),
    )
    assert asked == [TEAM_KEY]
    # Downloaded and already present, both parsed; the download was tried
    # only for the one that was missing.
    assert [u for s, u in chain.calls if s == "fetch"] == [DEMO_A]
    assert sorted(u for s, u in chain.calls if s == "parse") == [DEMO_A, DEMO_B]


# --- AD-9: a failure is a status ---------------------------------------------


def test_one_unparseable_demo_does_not_cost_the_others_the_report(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The others are classified, aggregated and rendered; the one is named.

    **This is the test the first mutation proof breaks.** Making the chain
    stop at the first failed unit turns it red, because ``DEMO_B`` never
    reaches ``classify`` and no report is written.
    """
    def parse(_settings, _archive, unit, _parser, *, demo_path=None, force=False):
        if unit == DEMO_A:
            raise ParseError(
                "The recording is truncated.", advice="Download it again."
            )
        return result(parse_stage.STAGE, unit)

    monkeypatch.setattr(parse_stage, "run", parse)
    run = run_chain(
        tmp_path, pending=(DEMO_A, DEMO_B), monkeypatch=monkeypatch, recorder=chain
    )

    failed = [s for s in run.steps if s.outcome == "failed"]
    assert [s.unit for s in failed] == [DEMO_A]
    assert "truncated" in str(failed[0].reason)
    assert failed[0].next_step == "Download it again."
    assert [u for s, u in chain.calls if s == "classify"] == [f"{DEMO_B}/{LINEUP}"]
    assert run.report is not None


def test_a_failed_unit_always_carries_a_next_step(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault with no advice of its own still names the command to run.

    The advice comes from the error where the error carries one; where it
    does not, the fallback is a **command**, not a sentence -- a unit that
    failed inside the chain is exactly the case where running the single
    stage by hand shows the whole error.
    """
    def parse(_settings, _archive, unit, _parser, *, demo_path=None, force=False):
        if unit == DEMO_A:
            raise ParseError("Something went wrong.")
        return result(parse_stage.STAGE, unit)

    monkeypatch.setattr(parse_stage, "run", parse)
    run = run_chain(
        tmp_path, pending=(DEMO_A, DEMO_B), monkeypatch=monkeypatch, recorder=chain
    )
    failed = next(s for s in run.steps if s.outcome == "failed")
    assert failed.next_step is not None
    assert f"pappascout parse {DEMO_A}" in failed.next_step


def test_a_step_that_failed_cannot_be_built_without_advice() -> None:
    """The guard itself, not merely the paths that happen to use it.

    Without it a new failure path could produce a failure with no next step,
    and the summary would have to invent one -- which is the wrong-advice
    defect the advice field was moved into the error to prevent.
    """
    with pytest.raises(AssertionError, match="no next step"):
        pipeline._failed("parse", DEMO_A, reason="broke", next_step="  ")


def test_a_refused_download_is_a_status_and_the_chain_goes_on(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No download authorisation is today's real state, and it is not the end.

    The download source may be refused outright, in which case ``fetch``
    raises rather than returning statuses -- correctly, since not one unit
    can succeed. For the chain that is still a status: everything already in
    the archive is parsed, classified and reported. Every demo the plan
    promised gets a line of its own, so the count on the screen matches the
    count in the plan.

    ``DEMO_C`` is the demo a hand import already put in the archive, and it
    is what makes the second half of the sentence testable: without it the
    refusal leaves nothing to parse and the run would rightly stop.
    """
    def refuse():
        raise DownloadsAccessDenied(
            "The download interface refused authorisation (403).",
            advice="Ask the interface's owner for the Downloads scope.",
        )

    run = run_chain(
        tmp_path,
        pending=(DEMO_A, DEMO_B),
        present=(DEMO_C,),
        demo_source=refuse,
        monkeypatch=monkeypatch, recorder=chain,
    )
    failed = [s for s in run.steps if s.outcome == "failed"]
    assert [s.unit for s in failed] == [DEMO_A, DEMO_B]
    # The fault is spelled out once and referred to after that: it is a
    # property of the credential, and a second copy of a five-line message
    # would bury the summary without adding anything.
    assert "403" in str(failed[0].reason)
    assert f"blocked by the fault reported on {DEMO_A}" in str(failed[1].reason)
    # Every row still carries its own next step, as ``fetch`` does.
    assert all(
        s.next_step == "Ask the interface's owner for the Downloads scope."
        for s in failed
    )
    # The chain carried on with what the archive already has.
    assert chain.stages[-3:] == ["classify", "aggregate", "render"]
    assert run.report is not None


def test_a_missing_download_token_is_the_same_kind_of_status(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Building the download port is where a missing token is noticed.

    It is a ``SettingsError`` like the refusal, and it means the same thing
    for the chain: not one unit can be downloaded, and everything already in
    the archive is still worth reporting.
    """
    def refuse():
        raise SettingsError("FACEIT_DOWNLOADS_TOKEN is missing.")

    run = run_chain(
        tmp_path,
        present=(DEMO_C,),
        demo_source=refuse,
        monkeypatch=monkeypatch,
        recorder=chain,
    )
    assert [s.unit for s in run.steps if s.outcome == "failed"] == [DEMO_A]
    assert run.report is not None


def test_every_stage_gets_a_line_even_with_nothing_to_do(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absence is the one thing the caller cannot read.

    A run whose sample is empty has nothing to fetch, nothing to parse and
    nothing to classify. If those three vanished from the summary, the
    reader would have to work out from what is *not* there whether the chain
    reached them at all -- and that is inference, which this summary exists
    to remove.
    """
    # The team index knows the lineup from an earlier run's parse, which is
    # what lets the aggregation happen at all when this run parsed nothing.
    monkeypatch.setattr(
        discover_stage,
        "teams_from_index",
        lambda _d: (subject(lineup_keys=(LINEUP,)),),
    )
    run = run_chain(
        tmp_path,
        pending=(),
        present=(),
        monkeypatch=monkeypatch, recorder=chain,
    )
    empty = {s.stage: s for s in run.steps if s.outcome == "no-units"}
    assert set(empty) == {"fetch", "parse", "classify"}
    assert all(s.reason for s in empty.values())


def test_a_demo_the_plan_found_on_disk_still_gets_a_fetch_row(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A row for each, no silent shortening** -- the module's own rule.

    Ten demos already in the archive and two to fetch used to give two
    ``fetch`` rows and twelve ``parse`` rows, with nothing in between
    saying where the ten came from. The reader was left to infer it from
    the difference between two counts, which is exactly the inference this
    summary exists to remove.

    The row says ``skipped``, and the stage decided that: ``fetch.plan``
    put the demo in ``present`` because ``fetch.in_archive`` found it and
    its metadata. Nothing about freshness is decided here.
    """
    monkeypatch.setattr(
        discover_stage,
        "teams_from_index",
        lambda _d: (subject(lineup_keys=(LINEUP,)),),
    )
    run = run_chain(
        tmp_path,
        pending=(DEMO_A,),
        present=(DEMO_B,),
        monkeypatch=monkeypatch,
        recorder=chain,
    )
    rows = [s for s in run.steps if s.stage == "fetch"]
    assert [(s.unit, s.outcome) for s in rows] == [
        (DEMO_B, "skipped"),
        (DEMO_A, "ran"),
    ]
    assert "Already in the archive" in str(rows[0].reason)
    # Both are parsed, so the two counts now match line for line.
    assert len([s for s in run.steps if s.stage == "parse"]) == 2


def test_a_demo_whose_lineup_is_not_the_teams_is_named_not_guessed(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero matching lineups is a status for that demo, not a silent choice.

    Choosing one of the two lineups anyway would classify the opponent's
    rounds as ours, and every number after it would be wrong while looking
    perfectly plausible.
    """
    monkeypatch.setattr(pipeline, "subject_lineups", lambda *_a, **_k: ())
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    failed = [s for s in stopped.value.run.steps if s.outcome == "failed"]
    assert any("cannot be classified" in str(s.reason) for s in failed)


def test_two_matching_lineups_are_ambiguity_and_not_a_draw(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both lineups over the threshold is genuine ambiguity, and it is said so."""
    monkeypatch.setattr(
        pipeline, "subject_lineups", lambda *_a, **_k: (LINEUP, OTHER_LINEUP)
    )
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    failed = [s for s in stopped.value.run.steps if s.outcome == "failed"]
    assert any("drawing lots" in str(s.reason) for s in failed)


# --- Freshness is the stages' decision ---------------------------------------


def test_a_run_with_nothing_to_do_reports_what_the_stages_reported(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Skipped is what the stage said, and the pipeline only passes it on.

    Note what this test does **not** prove: it stays green over a pipeline
    that compared the manifests itself and never called the stages at all.
    That is why the test below exists.
    """
    def parse(_settings, _archive, unit, _parser, *, demo_path=None, force=False):
        return result(parse_stage.STAGE, unit, skipped=True)

    def classify(
        _thresholds, _league, _archive, unit, team, *, economy, force=False
    ):
        return result(classify_stage.STAGE, unit, skipped=True)

    def aggregate(_thresholds, _league, _archive, team, *, aggregate_settings, **_k):
        return result(aggregate_stage.STAGE, team, skipped=True)

    monkeypatch.setattr(parse_stage, "run", parse)
    monkeypatch.setattr(classify_stage, "run", classify)
    monkeypatch.setattr(aggregate_stage, "run", aggregate)
    run = run_chain(
        tmp_path, pending=(), present=(DEMO_A,), monkeypatch=monkeypatch, recorder=chain
    )

    outcomes = {s.stage: s.outcome for s in run.steps}
    assert outcomes["parse"] == "skipped"
    assert outcomes["classify"] == "skipped"
    assert outcomes["aggregate"] == "skipped"
    # The three that have no skip at all say ``ran``, because that is what
    # they did. Claiming otherwise would be a nicer table and a false one.
    assert outcomes["discover"] == "ran"
    assert outcomes["select"] == "ran"
    assert outcomes["render"] == "ran"
    # The demo is already in the archive, which is the plan's own finding,
    # so the download is skipped rather than reported as having no units.
    assert outcomes["fetch"] == "skipped"


def test_the_pipeline_calls_the_stage_even_when_the_result_is_current(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The guard against a second implementation of freshness.**

    Every stage already compares its own manifest and returns
    ``skipped=True``. A comparison in this module would be a second answer to
    the same question, and the two would drift: a manifest field one of them
    reads and the other does not is all it takes.

    The test that looks as though it guards this -- "everything reports
    skipped" -- does not. A pipeline that compared the manifests itself and
    reported ``skipped`` without calling anything keeps that one green. This
    one goes red, because it counts the calls.
    """
    def parse(_settings, _archive, unit, _parser, *, demo_path=None, force=False):
        chain.note("parse-called", unit)
        return result(parse_stage.STAGE, unit, skipped=True)

    def classify(
        _thresholds, _league, _archive, unit, team, *, economy, force=False
    ):
        chain.note("classify-called", unit)
        return result(classify_stage.STAGE, unit, skipped=True)

    def aggregate(_thresholds, _league, _archive, team, *, aggregate_settings, **_k):
        chain.note("aggregate-called", team)
        return result(aggregate_stage.STAGE, team, skipped=True)

    def render(_settings, _archive, team, *, now=None):
        chain.note("render-called", team)
        return result(
            render_stage.STAGE,
            team,
            outputs=(PurePosixPath(f"reports/{team}/2026-09-10T1200-x.md"),),
        )

    monkeypatch.setattr(parse_stage, "run", parse)
    monkeypatch.setattr(classify_stage, "run", classify)
    monkeypatch.setattr(aggregate_stage, "run", aggregate)
    monkeypatch.setattr(render_stage, "run", render)
    settings, archive = settings_for(tmp_path)
    # **The archive is put into the state a second implementation would act
    # on**: every result and every manifest a shortcut would look at is
    # already there. Without this the test would pass over a pipeline that
    # compared manifests, because there would be no manifest to compare and
    # it would call the stage anyway -- that is, the guard would guard
    # nothing. The counter covered ``parse`` alone until 2026-09-10, and
    # the same shortcut in ``_classify_steps`` was measured firing with all
    # 55 tests still green.
    for unit in (DEMO_A, DEMO_B):
        for path in (
            archive.parsed_manifest(unit),
            archive.classified_manifest(LINEUP, unit),
            # The **result** as well as the manifest. A shortcut does not
            # have to read a manifest to be a shortcut: "the output is
            # already there, do not call the stage" is the same defect and
            # the cheaper one to write, and the structural guard would not
            # see it.
            archive.classified(LINEUP, unit),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
    for path in (
        archive.report_manifest(LINEUP),
        archive.report_json(LINEUP),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    reports = archive.reports_dir(LINEUP)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "2026-09-10T1200-x.md").write_text("x", encoding="utf-8")
    # And the index already names the lineup, which is what a shortcut in
    # ``_classify_steps`` would address the finished result by.
    monkeypatch.setattr(
        discover_stage,
        "teams_from_index",
        lambda _d: (subject(lineup_keys=(LINEUP,)),),
    )

    run_chain(
        tmp_path,
        pending=(),
        present=(DEMO_A, DEMO_B),
        monkeypatch=monkeypatch,
        recorder=chain,
    )
    assert [u for s, u in chain.calls if s == "parse-called"] == [DEMO_A, DEMO_B]
    assert [u for s, u in chain.calls if s == "classify-called"] == [DEMO_A, DEMO_B]
    assert [u for s, u in chain.calls if s == "aggregate-called"] == [LINEUP]
    assert [u for s, u in chain.calls if s == "render-called"] == [LINEUP]


#: The names no identifier, import or string in ``pipeline.py`` may carry.
_FRESHNESS_NAMES = frozenset(
    {"is_current", "read_if_exists", "compute_params_hash"}
)


def freshness_names_in(source: str) -> list[str]:
    """Every mention of a manifest or a freshness check in this source.

    **Five node kinds, and the first draft looked at two.** It walked
    ``Name`` and ``Attribute`` only, and Winston put four plausible
    mutations through it: it caught **none of the four**, including the
    string-literal path form its own docstring claimed to stop. An alias
    (``import parsed_manifest as _current_marker``) leaves no ``Name`` node
    carrying the banned word at the point of use, and a path written as a
    string leaves no identifier at all.

    So: identifiers, attributes, **import aliases on both sides**, keyword
    argument names, and string constants.

    **A string counts only if it has no whitespace in it.** That is the
    line between a path and a sentence, and it is not a guess: every
    component of an archive path goes through ``paths.safe_component``,
    whose pattern allows no space, while this module's own documentation
    and its error messages have to be able to say the word "manifest" --
    the module explains the rule it obeys, and a check that forbade the
    word in prose would be a check nobody could write the module under.
    Comments are not in the tree at all, which settles them by a happy
    accident rather than by a rule.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        words: list[str | None] = []
        if isinstance(node, ast.Name):
            words = [node.id]
        elif isinstance(node, ast.Attribute):
            words = [node.attr]
        elif isinstance(node, ast.alias):
            words = [node.name, node.asname]
        elif isinstance(node, ast.keyword):
            words = [node.arg]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            words = [node.value] if node.value.split() == [node.value] else []
        for word in words:
            if word is None:
                continue
            if "manifest" in word.lower() or word in _FRESHNESS_NAMES:
                found.add(word)
    return sorted(found)


def test_the_pipeline_module_never_reads_a_manifest() -> None:
    """The structural half of the same rule, read from the source.

    A behavioural guard can only cover the paths a test happens to run. This
    one covers the module: nothing in it may name a manifest or a freshness
    check, because there is no way to compare one without doing exactly what
    the rule forbids -- and **an existence check on a manifest file is the
    same defect in its cheapest form**, which is why the whole word is
    banned rather than one class name.
    """
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    used = freshness_names_in(source)
    assert used == [], (
        f"stages/pipeline.py uses {used}: freshness is the stages' decision "
        "and a second comparison here would drift from theirs"
    )


@pytest.mark.parametrize(
    "snippet",
    [
        pytest.param(
            "from pappascout.archive.paths import parsed_manifest as _marker\n"
            "def f(archive, unit):\n"
            "    return _marker(unit).exists()\n",
            id="import-alias",
        ),
        pytest.param(
            "from pathlib import PurePosixPath\n"
            "def f(archive, unit):\n"
            '    return archive.resolve(\n'
            '        PurePosixPath("parsed") / unit / "parse.manifest.json"\n'
            "    ).is_file()\n",
            id="string-literal-path",
        ),
        pytest.param(
            "def f(archive, unit):\n"
            '    return getattr(archive, "parsed_manifest")(unit).is_file()\n',
            id="getattr-by-name",
        ),
        pytest.param(
            "from pappascout.archive.manifest import Manifest\n"
            "def f(path):\n"
            "    return Manifest.read_if_exists(path) is not None\n",
            id="the-manifest-reader-itself",
        ),
    ],
)
def test_the_manifest_guard_catches_the_cheap_ways_round_it(snippet: str) -> None:
    """**The guard's own proof, because its docstring asserts one.**

    Every one of these four is a working freshness check that the first
    version of the guard let through -- measured, four for four. A test that
    claims a rule is mechanically held and does not hold it is worse than no
    test, because the next reader stops looking.
    """
    assert freshness_names_in(snippet) != [], (
        "this shortcut reads a manifest and the guard did not see it"
    )


def test_the_manifest_guard_does_not_fire_on_prose_or_comments() -> None:
    """The module has to be able to explain the rule it obeys.

    Both halves matter: a guard that fired on the documentation or on an
    error message would force the rule to go unexplained, and a guard that
    ignored **all** strings would miss the string-literal path above. The
    line is whitespace, because an archive path cannot contain any.
    """
    prose = '"""Freshness and the manifest are the stage\'s own."""\n'
    assert freshness_names_in(prose) == []
    assert freshness_names_in("# the manifest is the stage's business\nx = 1\n") == []
    message = 'x = "the manifests claim there is one"\n'
    assert freshness_names_in(message) == []
    assert freshness_names_in('x = "parse.manifest.json"\n') == ["parse.manifest.json"]


# --- AD-1: a stage still calls nobody ----------------------------------------


def test_no_stage_but_the_pipeline_runs_another_stage() -> None:
    """AD-1's rule, finally machine-checked.

    The rule is that a stage does not **call** another stage; reading another
    stage's public reader is expressly allowed and several stages do it
    (``fetch.resolve_team_key``, ``classify``'s selection reader). So the
    check is not about imports -- ``tests/test_layering.py`` cannot see them
    anyway, since the stages are all in one package -- but about calls to
    another stage's ``run`` or ``run_many``.

    Adding ``pipeline`` is what makes this checkable at all: before it there
    was no module that was *supposed* to make those calls, so the rule had
    nothing to be measured against.
    """
    directory = Path(pipeline.__file__).parent
    runners = {"run", "run_many"}
    offenders: list[str] = []
    for path in sorted(directory.glob("*.py")):
        if path.name in {"__init__.py", "pipeline.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in runners:
                continue
            owner = func.value
            if isinstance(owner, ast.Name) and owner.id.endswith("_stage"):
                offenders.append(f"{path.name}:{node.lineno}: {owner.id}.{func.attr}")
    assert offenders == [], (
        "A stage calls another stage's run (AD-1): " + ", ".join(offenders)
    )


def test_the_unit_errors_are_the_same_families_parse_records() -> None:
    """The pipeline catches per-unit faults where ``parse`` records them.

    Two lists of error classes in two modules drift. ``parse`` decides which
    faults are a property of the unit rather than of the code, and this
    module has to catch at least those -- otherwise a fault ``parse``
    considered a unit's own would end the whole run.
    """
    uncaught = [
        kind.__name__
        for kind in parse_stage._RECORDED_ERRORS
        if not issubclass(kind, pipeline.UNIT_ERRORS)
    ]
    assert uncaught == [], (
        "parse records these as the unit's own status, but the pipeline "
        f"would let them end the run: {uncaught}"
    )
    # And a programming error is deliberately not among them.
    assert not issubclass(TypeError, pipeline.UNIT_ERRORS)
    assert not issubclass(ValueError, pipeline.UNIT_ERRORS)


# --- Nothing can proceed ------------------------------------------------------


def test_no_processable_demo_raises_and_names_the_next_command(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**This is the test the third mutation proof breaks.**

    Swallowing the error to keep the chain alive turns it green over a run
    that produced nothing: no aggregate can be addressed, so no report can
    exist, and a caller told "0 failed" would believe there was one.

    The message names what is missing and the advice names the command to
    run next, which is the AD-9 contract for an exception. Today that
    command is the hand import, because the download source is the one thing
    that can be refused outright.
    """
    monkeypatch.setattr(aggregate_stage, "team_keys", lambda _a: [])
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)

    assert "no classified rounds" in str(stopped.value)
    assert "pappascout import" in str(stopped.value.advice)
    assert DEMO_A in str(stopped.value)


def test_the_steps_taken_so_far_travel_with_the_error(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The summary is not lost at the moment it is worth most.

    A run that parsed eleven demos and then could not aggregate is a run the
    caller has to see. Without the steps on the exception they would get the
    last line and nothing about the eleven.
    """
    monkeypatch.setattr(aggregate_stage, "team_keys", lambda _a: [])
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    stages = [s.stage for s in stopped.value.run.steps]
    assert stages[:2] == ["discover", "select"]
    assert stopped.value.run.report is None


def test_an_aggregate_that_fails_stops_the_run_and_keeps_the_summary(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an aggregate there is no report: that is not one unit failing.

    The error is not swallowed and the run is not carried on with -- but the
    steps before it are still on the exception, so the caller sees what did
    happen.
    """
    def aggregate(*_a, **_k):
        raise PappascoutError("The sample does not add up.", advice="Parse again.")

    monkeypatch.setattr(aggregate_stage, "run", aggregate)
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert "does not add up" in str(stopped.value)
    assert stopped.value.advice == "Parse again."
    assert [s.stage for s in stopped.value.run.steps][-1] == "classify"


# --- The question -------------------------------------------------------------


def test_the_plan_is_shown_once_for_the_whole_download_not_once_per_demo(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One question, up front, about the plan as a whole.

    The quota and the disk space are spent by the plan, not by one demo, so
    a question per demo would ask the wrong thing twelve times.

    The run so far travels with the plan, because the shell has to be able
    to say what the stages before the question already did when the answer
    is no.
    """
    seen: list[tuple[fetch_stage.FetchPlan, pipeline.PipelineRun]] = []
    run_chain(
        tmp_path,
        pending=(DEMO_A, DEMO_B),
        before_download=lambda plan, so_far: seen.append((plan, so_far)),
        monkeypatch=monkeypatch, recorder=chain,
    )
    assert len(seen) == 1
    assert seen[0][0].pending == (DEMO_A, DEMO_B)
    assert [s.stage for s in seen[0][1].steps] == ["discover", "select"]
    # The conflict scan has not run at this point, and the snapshot says so
    # rather than reporting a clean archive nobody looked at.
    assert seen[0][1].conflicts is None


def test_nothing_is_asked_when_there_is_nothing_to_download(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A question about an empty plan is a question with no content."""
    seen: list[fetch_stage.FetchPlan] = []
    run = run_chain(
        tmp_path,
        pending=(),
        present=(DEMO_A,),
        before_download=lambda plan, _so_far: seen.append(plan),
        monkeypatch=monkeypatch, recorder=chain,
    )
    assert seen == []
    fetch_step = next(s for s in run.steps if s.stage == "fetch")
    assert fetch_step.outcome == "skipped"
    assert "Already in the archive" in str(fetch_step.reason)


def test_an_unattended_run_is_never_asked_anything(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a callback nothing can block, which is what ``--yes`` gives."""
    run = run_chain(
        tmp_path,
        pending=(DEMO_A,),
        before_download=None,
        monkeypatch=monkeypatch,
        recorder=chain,
    )
    assert run.report is not None


# --- The joins that read real files -------------------------------------------


def write_lineups(archive: ArchivePaths, map_demo_id: str, members) -> None:
    """Write a demo's ``lineups.parquet`` with the given players."""
    rows = [
        {
            "map_demo_id": map_demo_id,
            "lineup_key": key,
            "player_id": player,
            "player_name": None,
            "clan_name": None,
        }
        for key, players in members.items()
        for player in players
    ]
    path = archive.parsed_table(map_demo_id, "lineups")
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame(rows, schema=dict(LINEUPS)).write_parquet(path)


def test_the_subject_lineup_is_the_one_that_shares_the_roster(
    tmp_path: Path,
) -> None:
    """The rule is ``assign_lineup_keys``, the same one ``discover`` uses.

    A second rule for "whose demo is this" would let the classification and
    the team index disagree, and the report would be written from a lineup
    the index says belongs to somebody else.
    """
    _settings, archive = settings_for(tmp_path)
    write_lineups(archive, DEMO_A, {LINEUP: OURS, OTHER_LINEUP: THEIRS})
    assert pipeline.subject_lineups(archive, DEMO_A, subject(), 3) == (LINEUP,)


def test_a_demo_of_two_other_teams_matches_no_lineup(tmp_path: Path) -> None:
    """Nothing is chosen when nothing is ours, and the caller says so."""
    _settings, archive = settings_for(tmp_path)
    other = tuple(str(76561198100000000 + n) for n in range(5))
    write_lineups(archive, DEMO_A, {LINEUP: other, OTHER_LINEUP: THEIRS})
    assert pipeline.subject_lineups(archive, DEMO_A, subject(), 3) == ()


def test_a_missing_lineups_table_is_no_lineup_and_not_an_exception(
    tmp_path: Path,
) -> None:
    """An unreadable table must not take the other demos down with it.

    The caller turns "no lineup" into this unit's own failed step with its
    own next step, which is what AD-9 asks for; an exception here would end
    the run over one demo.
    """
    _settings, archive = settings_for(tmp_path)
    assert pipeline.subject_lineups(archive, DEMO_A, subject(), 3) == ()


def test_the_threshold_is_read_from_the_settings_and_not_fixed(
    tmp_path: Path,
) -> None:
    """A lineup with two of ours is ours at 2 and not ours at 3.

    The number is ``[thresholds].team_identity_min_common``, the same one
    ``discover`` fills the index with. Fixing it here would make one of the
    two answers wrong the moment the setting is adjusted.
    """
    _settings, archive = settings_for(tmp_path)
    half = (*OURS[:2], *THEIRS[:3])
    write_lineups(archive, DEMO_A, {LINEUP: half})
    assert pipeline.subject_lineups(archive, DEMO_A, subject(), 2) == (LINEUP,)
    assert pipeline.subject_lineups(archive, DEMO_A, subject(), 3) == ()


def classify_dir(archive: ArchivePaths, lineup: str, demo: str = DEMO_A) -> None:
    """Give a lineup a classified demo, which is what makes it a candidate."""
    path = archive.classified(lineup, demo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")


def test_the_aggregate_key_prefers_the_one_that_already_has_a_report(
    tmp_path: Path,
) -> None:
    """One tree per team, not a second one beside the first.

    The archive keeps one ``aggregates/<key>/`` and one ``reports/<key>/``
    per team. A run that chose a different lineup key would start a second
    tree, and the reader who knows where the earlier reports are would not
    find the new one.
    """
    _settings, archive = settings_for(tmp_path)
    classify_dir(archive, LINEUP)
    classify_dir(archive, OTHER_LINEUP)
    established = archive.report_json(OTHER_LINEUP)
    established.parent.mkdir(parents=True, exist_ok=True)
    established.write_text("{}", encoding="utf-8")
    assert pipeline.aggregate_key(archive, [LINEUP, OTHER_LINEUP]) == OTHER_LINEUP


def test_the_aggregate_key_is_the_same_one_on_every_run(tmp_path: Path) -> None:
    """With nothing established the smallest key wins, and it keeps winning.

    Any rule would do as long as it is the same one every time: a choice
    that moved between two runs would put the second report where the first
    one's reader does not look, and ``aggregate`` would never report itself
    skipped.
    """
    _settings, archive = settings_for(tmp_path)
    classify_dir(archive, LINEUP)
    classify_dir(archive, OTHER_LINEUP)
    assert pipeline.aggregate_key(archive, [OTHER_LINEUP, LINEUP]) == LINEUP
    assert pipeline.aggregate_key(archive, [LINEUP, OTHER_LINEUP]) == LINEUP


def test_a_lineup_with_no_classified_rounds_is_not_a_candidate(
    tmp_path: Path,
) -> None:
    """The index may name a lineup the archive has never classified."""
    _settings, archive = settings_for(tmp_path)
    classify_dir(archive, LINEUP)
    assert pipeline.aggregate_key(archive, [OTHER_LINEUP]) is None
    assert pipeline.aggregate_key(archive, [LINEUP, OTHER_LINEUP]) == LINEUP


# --- The conflict copies (AD-7) -----------------------------------------------


def test_a_parenthesised_copy_is_found(tmp_path: Path) -> None:
    """The shape a sync client gives a copy it could not merge.

    Nothing the tool writes can look like this: every name goes through
    ``paths.safe_component``, whose pattern allows neither a space nor a
    bracket.
    """
    _settings, archive = settings_for(tmp_path)
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().write_text("{}", encoding="utf-8")
    forked = archive.teams_index().with_name("teams (1).json")
    forked.write_text("{}", encoding="utf-8")
    assert pipeline.conflict_copies(archive) == (PurePosixPath("index/teams (1).json"),)


def test_a_copy_named_after_this_machine_is_found(tmp_path: Path) -> None:
    """The other shape: the losing machine's name appended to the stem."""
    _settings, archive = settings_for(tmp_path)
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().write_text("{}", encoding="utf-8")
    forked = archive.teams_index().with_name(f"teams-{host_tag()}.json")
    forked.write_text("{}", encoding="utf-8")
    assert PurePosixPath(f"index/teams-{host_tag()}.json") in pipeline.conflict_copies(
        archive
    )


def test_a_copy_named_after_another_machine_is_caught_only_by_its_brackets(
    tmp_path: Path,
) -> None:
    """**The documented limit of the host shape, recorded honestly.**

    A conflict copy is named after the machine whose version lost, and that
    is very often not this one -- the other machine is the whole reason
    AD-7 exists. This check cannot name it. Nothing in the tool ever creates
    ``logs/<host>/`` (measured across ``src/`` on 2026-09-10, and ``logs/``
    in the real archive is empty), so an earlier attempt to learn the other
    machine's name from that directory could never fire; its test built the
    directory itself and was green over a state the product cannot produce.

    So the honest statement is this one: the other machine's copy is found
    by its brackets, which is the shape the sync client is observed to use,
    and not by its name. If that ever changes, this test is where the change
    has to be argued.
    """
    _settings, archive = settings_for(tmp_path)
    archive.logs_dir("otherbox").mkdir(parents=True, exist_ok=True)
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    named = archive.teams_index().with_name("teams-otherbox.json")
    named.write_text("{}", encoding="utf-8")
    assert pipeline.conflict_copies(archive) == ()

    bracketed = archive.teams_index().with_name("teams (otherbox).json")
    bracketed.write_text("{}", encoding="utf-8")
    assert pipeline.conflict_copies(archive) == (
        PurePosixPath("index/teams (otherbox).json"),
    )


def test_the_host_shape_is_anchored_to_the_end_of_the_stem(
    tmp_path: Path,
) -> None:
    """The sync client appends the host; it does not sprinkle it about.

    Unanchored, a machine tag such as ``box7`` matched inside the report of
    any team whose slug began with it -- ``2026-09-10T1200-box7-gaming.md``
    -- and every run of that team ended in ``RunStopped``. The same shape as
    the ``"set"``-inside-``"settings"`` guard this repository found in T11.

    Both directions are pinned, because a guard that is only tested from one
    side can be tightened to nothing or loosened to everything without a
    word.
    """
    host = host_tag()
    assert pipeline._looks_forked(f"teams-{host}.json", host)
    assert pipeline._looks_forked(f"TEAMS-{host.upper()}.JSON", host)
    assert not pipeline._looks_forked(f"2026-09-10T1200-{host}-gaming.md", host)
    assert not pipeline._looks_forked(f"{host}-teams.json", host)
    assert not pipeline._looks_forked("teams.json", host)


def test_a_stopped_run_has_still_had_its_archive_checked_for_forks(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AD-7's check runs on the way out of a stopped run too.

    It could be dropped from ``stop()`` with every test green, and a
    stopped run is a likely place for a forked archive: two machines
    writing at once is one of the things that stops a run in the first
    place. The check is reported and not raised on there, because the run
    is already ending with a fault of its own -- and ``None`` in that field
    would say the scan never happened.
    """
    archive = settings_for(tmp_path)[1]
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().with_name("teams (1).json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(aggregate_stage, "team_keys", lambda _a: [])
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert stopped.value.run.conflicts == (PurePosixPath("index/teams (1).json"),)


def test_the_sync_clients_own_wording_is_a_conflict_copy(tmp_path: Path) -> None:
    """``report (this machine's conflicting copy).md`` -- the real thing.

    The only bracket fixture used to be ``teams (1).json``, so the pattern
    could be narrowed to ``\\s\\(\\d+\\)`` -- digits only -- and stay green,
    while the wording this module's own documentation quotes went uncaught.
    """
    _settings, archive = settings_for(tmp_path)
    reports = archive.reports_dir(LINEUP)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "2026-09-10T1200-slug.md").write_text("x", encoding="utf-8")
    (reports / "2026-09-10T1200-slug (this machine's conflicting copy).md").write_text(
        "x", encoding="utf-8"
    )
    assert pipeline.conflict_copies(archive) == (
        PurePosixPath(
            f"reports/{LINEUP}/2026-09-10T1200-slug "
            "(this machine's conflicting copy).md"
        ),
    )


def test_the_scan_leaves_alone_the_trees_a_run_does_not_write(
    tmp_path: Path,
) -> None:
    """``import/``, ``raw/`` and ``logs/`` are not the tool's results.

    The check's claim is "the tool wrote this file and here is a second
    one", and that is false of all three. ``import/`` above all: it is the
    human's inbox (AD-8) and, with no download authorisation, the only route
    a demo takes into the archive -- so a browser's second copy of a
    hand-fetched demo ended every run in ``RunStopped``, after the report
    had been written, advising the user to "keep the one you want" about a
    file that is not a pair of anything. ``raw/`` is a cache AD-7 says may
    be deleted at any time and ``logs/`` is per-machine.

    All three names are measured hits from the real archive on 2026-09-10.
    """
    _settings, archive = settings_for(tmp_path)
    archive.import_dir().mkdir(parents=True, exist_ok=True)
    (archive.import_dir() / "1-match-0 (1).dem").write_text("x", encoding="utf-8")
    archive.logs_dir(host_tag()).mkdir(parents=True, exist_ok=True)
    (archive.logs_dir(host_tag()) / "debug (1).log").write_text("x", encoding="utf-8")
    archive.raw_faceit().mkdir(parents=True, exist_ok=True)
    (archive.raw_faceit() / "matches-1-abc (2).json").write_text(
        "x", encoding="utf-8"
    )
    assert pipeline.conflict_copies(archive) == ()

    # And a fork in a result area is still found, so the scoping did not
    # simply switch the check off.
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().with_name("teams (1).json").write_text(
        "{}", encoding="utf-8"
    )
    assert pipeline.conflict_copies(archive) == (
        PurePosixPath("index/teams (1).json"),
    )


def test_the_scanned_areas_are_the_archives_own_result_list() -> None:
    """The list is ``paths.RESULT_AREAS`` and not a copy of it here.

    Two lists of directory names would drift, and the one that drifted
    would be this check: a result tree added to the archive and not to the
    scan is a tree the fork check silently stops covering.
    """
    from pappascout.archive import paths

    assert paths.RESULT_AREAS == (
        PurePosixPath("index"),
        PurePosixPath("demos"),
        PurePosixPath("parsed"),
        PurePosixPath("classified"),
        PurePosixPath("aggregates"),
        PurePosixPath("reports"),
    )
    for area in (paths.import_dir(), paths.raw_faceit_dir()):
        assert area not in paths.RESULT_AREAS
    assert PurePosixPath("logs") not in paths.RESULT_AREAS


def test_an_unreadable_entry_does_not_take_the_finished_run_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same guard, and the same reason, as ``paths.total_size_bytes``.

    A cloud placeholder or a file still being transferred raises ``OSError``
    from the walk. This runs **after** the report is written, so an
    exception here would lose a run that had already finished its work.
    """
    _settings, archive = settings_for(tmp_path)
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().write_text("{}", encoding="utf-8")
    archive.reports_dir(LINEUP).mkdir(parents=True, exist_ok=True)
    (archive.reports_dir(LINEUP) / "r (1).md").write_text("x", encoding="utf-8")

    real_rglob = Path.rglob

    def rglob(self, pattern):
        if self.name == "index":
            raise OSError("the sync client is still transferring this")
        return real_rglob(self, pattern)

    monkeypatch.setattr(Path, "rglob", rglob)
    # The area that raises is skipped; the others are still scanned.
    assert pipeline.conflict_copies(archive) == (
        PurePosixPath(f"reports/{LINEUP}/r (1).md"),
    )


def test_our_own_temporary_file_is_not_a_conflict_copy(tmp_path: Path) -> None:
    """The atomic write's temporary name carries the host name too.

    Without the exclusion every interrupted write would be reported as a
    fork. A leftover temporary file is worth noticing, but it is a different
    thing, and one check reporting both would say neither clearly.
    """
    from pappascout.archive.atomic_write import temp_suffix

    _settings, archive = settings_for(tmp_path)
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().with_name(f"teams.json{temp_suffix()}").write_text(
        "{}", encoding="utf-8"
    )
    assert pipeline.conflict_copies(archive) == ()


def test_an_untouched_archive_reports_no_conflict(tmp_path: Path) -> None:
    """The normal answer is nothing, including for a report with an ordinal.

    ``2026-09-10T1200-slug-02.md`` is the second report of the same minute
    and it sits beside the first. A check that read a trailing suffix as a
    machine name would call every one of those a fork.
    """
    _settings, archive = settings_for(tmp_path)
    reports = archive.reports_dir(LINEUP)
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "2026-09-10T1200-slug.md").write_text("x", encoding="utf-8")
    (reports / "2026-09-10T1200-slug-02.md").write_text("x", encoding="utf-8")
    assert pipeline.conflict_copies(archive) == ()


def test_a_forked_archive_is_an_error_at_the_end_of_the_run(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AD-7's promise: the check runs at the end, and the report is written.

    The order matters. Two versions of the same result with one manifest
    between them is a fault worth stopping on, but stopping *before* the
    report would punish the run for a fault that has nothing to do with it.
    """
    settings, archive = settings_for(tmp_path)
    archive.teams_index().parent.mkdir(parents=True, exist_ok=True)
    archive.teams_index().with_name("teams (1).json").write_text(
        "{}", encoding="utf-8"
    )
    monkeypatch.setattr(
        fetch_stage,
        "plan",
        lambda _a, k: fetch_stage.FetchPlan(team_key=k, pending=(DEMO_A,)),
    )
    monkeypatch.setattr(fetch_stage, "in_archive", lambda _a, _u: True)
    with pytest.raises(pipeline.RunStopped) as stopped:
        pipeline.run(
            settings,
            archive,
            TEAM,
            match_source=object(),
            demo_source=lambda: object(),
            parser=lambda: object(),
        )
    assert "could not merge" in str(stopped.value)
    assert stopped.value.run.report is not None
    assert stopped.value.run.conflicts == (PurePosixPath("index/teams (1).json"),)


# --- A real stage result through the chain -----------------------------------

PARSED_TABLE_NAMES = (
    "rounds",
    "ticks",
    "events",
    "lineups",
    "deaths",
    "callouts",
    "match",
)


def test_a_real_parse_result_with_an_unreadable_table_does_not_kill_the_run(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Every chain test above hands the pipeline a hand-built stats dict.**

    That is why this was invisible. ``parse._existing_stats`` deliberately
    returns ``{"unreadable": <the error>}`` -- with no ``rounds`` and no
    ``sample_points`` -- when a finished table will not open on a skipped
    run, and ``_parse_detail`` indexed both keys directly. ``KeyError`` is
    in neither ``UNIT_ERRORS`` nor anything else on the way out, so it
    escaped ``run`` past ``RunStopped``: measured end to end as exit 2, a
    traceback and **not one line of summary**, after the archive had been
    written.

    The state is the one this environment produces on its own -- a sync
    client holding one Parquet file open -- which is the very phenomenon
    the retry in ``atomic_path`` exists for.
    """
    _settings, archive = settings_for(tmp_path)
    for table in PARSED_TABLE_NAMES:
        path = archive.parsed_table(DEMO_A, table)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not a parquet file at all")
    real_stats = parse_stage._existing_stats(
        *(archive.parsed_table(DEMO_A, table) for table in PARSED_TABLE_NAMES)
    )
    # The state really is the documented one, or the test proves nothing.
    assert "unreadable" in real_stats
    assert "rounds" not in real_stats

    def parse(_settings, _archive, unit, _parser, *, demo_path=None, force=False):
        return StageResult(
            stage=parse_stage.STAGE,
            unit=unit,
            status="ok",
            skipped=True,
            stats=real_stats,
            reason="The result is up to date.",
        )

    monkeypatch.setattr(parse_stage, "run", parse)
    run = run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    step = next(s for s in run.steps if s.stage == "parse")
    assert step.outcome == "skipped"
    assert "no rounds obtained" in str(step.detail)
    assert "no sample points obtained" in str(step.detail)
    assert run.report is not None


def test_the_detail_of_a_half_readable_parse_names_only_what_was_lost() -> None:
    """One table unreadable is not both, and the summary says which.

    ``_existing_stats`` reads the tables separately for exactly this
    reason: "a shared try block would lose the round counts whenever only
    the sample-point table is unreadable". A detail that reported both as
    lost would throw that away again at the last step.
    """
    assert (
        pipeline._parse_detail({"rounds": 24, "ticks_unreadable": "OSError: locked"})
        == "24 rounds, no sample points obtained (OSError: locked)"
    )
    assert (
        pipeline._parse_detail({"unreadable": "OSError: locked", "sample_points": 96})
        == "no rounds obtained (OSError: locked), 96 sample points"
    )
    assert (
        pipeline._parse_detail({"rounds": 24, "sample_points": 96})
        == "24 rounds, 96 sample points"
    )


# --- A2: the two identity rules, and what falls between them ------------------


def test_the_two_identity_rules_can_disagree_and_the_difference_is_reported(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**A lineup can be ours and still not be in our report.**

    ``subject_lineups`` asks whether a lineup shares ``min_common`` players
    with the team's **standing roster**; ``aggregate.collect_team`` asks
    whether it shares that many with the **chosen target lineup**, never
    chained. A standing roster is bigger than any one lineup, so the two
    disagree -- measured with an eight-player roster and two lineups
    sharing two players at ``min_common = 3``: the first says both, the
    second says one.

    The demos of the lineup that loses are classified and then dropped, and
    they are **not** in the report's ``missing_demos`` either, because
    ``collect_team`` never looks at that lineup at all. The summary said
    "aggregate ran / N demos" over a sample quietly short of some, which is
    a plausible wrong number with nothing reporting it.

    Reconciling the rules is not available: narrowing the first
    misclassifies demos, and widening the second changes what the
    ``aggregate`` command does today. So the difference is measured and
    named.
    """
    monkeypatch.setattr(
        aggregate_stage, "team_keys", lambda _a: [LINEUP, OTHER_LINEUP]
    )
    # The index already knows both lineups as this team's -- that is the
    # roster rule's answer, and it is the wider of the two.
    monkeypatch.setattr(
        discover_stage,
        "teams_from_index",
        lambda _d: (subject(lineup_keys=(LINEUP, OTHER_LINEUP)),),
    )
    run = run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    # The aggregation reported joining only ``LINEUP`` (its own stats), so
    # the other one's demos are in no report and in no missing list.
    assert run.lineup_key == LINEUP
    assert run.lineups_left_out == (OTHER_LINEUP,)


def test_nothing_is_left_out_when_the_aggregation_joined_them_all(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary run measures the same thing and finds nothing.

    An empty tuple, not ``None``: the check ran.
    """
    run = run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert run.lineups_left_out == ()


def test_a_stage_that_does_not_report_its_lineups_is_not_a_clean_result() -> None:
    """A comparison that could not be made must not look like a clean one.

    ``None`` and ``()`` are two different answers here for the same reason
    they are for the conflict scan, and the summary prints them
    differently.
    """
    without = pipeline.Step(
        stage="aggregate",
        unit=LINEUP,
        outcome="ran",
        result=StageResult(
            stage="aggregate", unit=LINEUP, status="ok", skipped=False, stats={}
        ),
    )
    assert pipeline.lineups_left_out([LINEUP], without) is None
    no_result = pipeline.Step(stage="aggregate", unit=LINEUP, outcome="ran")
    assert pipeline.lineups_left_out([LINEUP], no_result) is None


def test_the_aggregate_stage_reports_the_lineups_it_joined() -> None:
    """The contract :func:`pipeline.lineups_left_out` reads, pinned where it lives.

    The covered set is read from ``aggregate``'s own ``stats["lineup_keys"]``
    and never recomputed -- a second computation of "which lineups did the
    aggregation join" is the duplicate rule this whole measurement exists
    because of. So the key has to be there, on both of the stage's return
    paths.
    """
    source = Path(aggregate_stage.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    stats = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_stats"
    )
    literal = next(node for node in ast.walk(stats) if isinstance(node, ast.Dict))
    keys = {
        node.value
        for node in literal.keys
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "lineup_keys" in keys, (
        "aggregate._stats no longer reports which lineups it joined, so the "
        "pipeline cannot tell which of the ones it classified were left out"
    )
    # And it is the only ``stats=`` the stage returns, on both paths.
    assert source.count("stats=_stats(") == 2


# --- A3: a refusal in the middle of the downloads -----------------------------


def test_a_demo_that_arrived_before_the_refusal_is_not_marked_failed(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_many`` raises **from inside its loop**, after some have landed.

    It attaches its own progress note precisely because some may have
    succeeded -- and then the results it had collected go with the frame.
    Turning the whole plan into ``failed`` rows wrote "failed 1-match-0"
    about a demo that is in the archive and in the report, and blamed the
    credential fault on the unit that worked.
    """

    def run_many(_archive, units, *, source, **_kwargs):
        first = list(units)[0]
        chain.note(fetch_stage.STAGE, first)
        chain.fetched.add(first)
        raise DownloadsAccessDenied(
            "The download interface refused authorisation (403).\n\n"
            "The run stopped at the first authorisation failure: demos "
            "fetched: 1, demos left unfetched: 1.",
            advice="Ask the interface's owner for the Downloads scope.",
        )

    monkeypatch.setattr(fetch_stage, "run_many", run_many)
    run = run_chain(
        tmp_path, pending=(DEMO_A, DEMO_B), monkeypatch=monkeypatch, recorder=chain
    )
    rows = {s.unit: s for s in run.steps if s.stage == "fetch"}
    assert rows[DEMO_A].outcome == "ran"
    assert "before the download stage was refused" in str(rows[DEMO_A].reason)
    assert rows[DEMO_B].outcome == "failed"
    assert "403" in str(rows[DEMO_B].reason)
    # The one that arrived is parsed and reaches the report, which is the
    # contradiction the old rows produced: failed here, present there.
    assert DEMO_A in [u for s, u in chain.calls if s == "parse"]
    assert run.report is not None


# --- A4: the calls that were outside every try --------------------------------


def locked(*_a, **_k):
    """What this environment's commonest fault looks like from a stage."""
    raise PappascoutError("The file is locked.", advice="Try again.")


def test_a_fault_in_select_keeps_the_summary(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``select`` is not "the very first step", and it goes through the disk.

    It writes the selection file, and on this archive the disk is a
    synchronised folder that holds files open. The call sat outside every
    try, so a locked file threw the plain error away together with the step
    ``discover`` had already produced -- while ``run``'s own ``Raises``
    said that could happen only before any stage had produced a result.
    """
    monkeypatch.setattr(select_stage, "run", locked)
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert "The file is locked." in str(stopped.value)
    assert stopped.value.advice == "Try again."
    assert [s.stage for s in stopped.value.run.steps] == ["discover"]


def test_a_disk_fault_listing_the_classified_teams_keeps_the_summary(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fourth call over the same disk, found by looking for more of them.

    The review named three; ``aggregate.team_keys`` lists ``classified/``
    with ``Path.iterdir``, which **raises** where the ``is_file`` calls
    around it swallow. By the time it runs the chain has parsed and
    classified everything, so this is the summary most worth keeping of
    all -- and it was the one left travelling bare.
    """

    def boom(_archive):
        raise OSError("the folder is being synchronised")

    monkeypatch.setattr(aggregate_stage, "team_keys", boom)
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert "could not be listed" in str(stopped.value)
    assert stopped.value.advice
    assert [s.stage for s in stopped.value.run.steps][-1] == "classify"


def test_a_fault_in_the_download_plan_keeps_the_summary(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch.plan`` reads back the file ``select`` has just written.

    Same disk, same fault, and it was outside every try as well -- with two
    steps already collected. The chain is built here rather than through
    ``run_chain``, because that helper installs a working ``plan`` of its
    own and would put it back over the broken one.
    """
    settings, archive = settings_for(tmp_path)
    monkeypatch.setattr(fetch_stage, "plan", locked)
    with pytest.raises(pipeline.RunStopped) as stopped:
        pipeline.run(
            settings,
            archive,
            TEAM,
            match_source=object(),
            demo_source=lambda: object(),
            parser=lambda: object(),
        )
    assert "The file is locked." in str(stopped.value)
    assert stopped.value.advice == "Try again."
    assert [s.stage for s in stopped.value.run.steps] == ["discover", "select"]


def test_a_fault_in_the_download_question_keeps_the_summary(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk gate lives in the shell's callback and raises like anything else.

    A plan that does not fit on the disk is a plan with no right answer,
    and the caller still has to see the two stages that already ran.
    """

    def refuse(_plan, _so_far):
        raise PappascoutError("There is not enough space.", advice="Free some.")

    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(
            tmp_path,
            before_download=refuse,
            monkeypatch=monkeypatch,
            recorder=chain,
        )
    assert "not enough space" in str(stopped.value)
    assert [s.stage for s in stopped.value.run.steps] == ["discover", "select"]


def test_a_fault_in_discover_is_the_one_that_travels_bare(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented exception to the rule above, and it is still true.

    Before ``discover`` has resolved the team there is no team key and no
    step, so there is no summary to lose -- and ``RunStopped`` would carry
    an empty one.
    """

    def boom(*_a, **_k):
        raise PappascoutError("No such competition.", advice="Check the id.")

    monkeypatch.setattr(discover_stage, "run", boom)
    with pytest.raises(PappascoutError) as raised:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert not isinstance(raised.value, pipeline.RunStopped)


# --- A9: the smaller ones ------------------------------------------------------


def test_the_parser_is_built_once_and_a_factory_that_raises_is_not_retried(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run-wide fault gets one error block, not one per demo.

    The factory used to be called inside the per-unit try, so a factory
    that raised was called again for every demo and printed its whole
    message once per demo -- the very thing ``_blocked`` exists to avoid
    for ``fetch``, and the docstring said "called at most once" while it
    happened.
    """
    calls: list[int] = []

    def factory():
        calls.append(1)
        raise ParseError("demoparser2 will not load.")

    with pytest.raises(pipeline.RunStopped):
        run_chain(
            tmp_path,
            pending=(DEMO_A, DEMO_B),
            parser=factory,
            monkeypatch=monkeypatch,
            recorder=chain,
        )
    assert len(calls) == 1


def test_the_parser_is_not_built_at_all_when_there_is_nothing_to_parse(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It loads the parsing library, which an empty run should not pay for."""
    calls: list[int] = []
    monkeypatch.setattr(
        discover_stage,
        "teams_from_index",
        lambda _d: (subject(lineup_keys=(LINEUP,)),),
    )
    run_chain(
        tmp_path,
        pending=(),
        present=(),
        parser=lambda: calls.append(1),
        monkeypatch=monkeypatch,
        recorder=chain,
    )
    assert calls == []


def test_a_missing_parsing_library_is_a_status_and_not_a_traceback(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``parse.default_parser`` imports demoparser2 **inside itself**.

    ``ImportError`` is not in ``UNIT_ERRORS`` and must not be -- for one
    unit it would be a programming error -- so it escaped the chain
    entirely: a traceback, exit 2 and no summary. For the run as a whole it
    is a fault of the run, and every demo gets a row saying so.
    """

    def factory():
        raise ImportError("No module named 'demoparser2'")

    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(
            tmp_path,
            pending=(DEMO_A, DEMO_B),
            parser=factory,
            monkeypatch=monkeypatch,
            recorder=chain,
        )
    rows = [s for s in stopped.value.run.steps if s.stage == "parse"]
    assert [s.unit for s in rows] == [DEMO_A, DEMO_B]
    assert all(s.outcome == "failed" for s in rows)
    assert "demoparser2" in str(rows[0].reason)
    # The message once, referred to after that.
    assert "blocked by the fault reported on" in str(rows[1].reason)
    assert all(s.next_step for s in rows)


def test_an_unreadable_lineups_table_is_raised_and_not_swallowed(
    tmp_path: Path,
) -> None:
    """A read error and "not our demo" are different answers.

    Swallowing the ``PolarsError`` gave the demo the reason "the demo is
    another team's, or the roster in the index is out of date" -- neither
    true -- and advised ``classify <id>``, which lists nothing, because the
    same table will not open for it either.

    A **missing** table is still the empty answer, and that distinction is
    the whole fix: absence is a fact about the parse, a read error is a
    fact about the disk.
    """
    _settings, archive = settings_for(tmp_path)
    assert pipeline.subject_lineups(archive, DEMO_A, subject(), 3) == ()

    path = archive.parsed_table(DEMO_A, "lineups")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is not a parquet file")
    with pytest.raises((OSError, pl.exceptions.PolarsError)):
        pipeline.subject_lineups(archive, DEMO_A, subject(), 3)


def test_a_demo_whose_lineups_will_not_open_is_that_demos_own_failure(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it does not take the other demos down with it (AD-9)."""

    def lineups(_archive, unit, _subject, _min_common):
        if unit == DEMO_A:
            raise pl.exceptions.ComputeError("parquet: magic bytes not found")
        return (LINEUP,)

    monkeypatch.setattr(pipeline, "subject_lineups", lineups)
    run = run_chain(
        tmp_path, pending=(DEMO_A, DEMO_B), monkeypatch=monkeypatch, recorder=chain
    )
    failed = [s for s in run.steps if s.outcome == "failed"]
    assert [s.unit for s in failed] == [DEMO_A]
    assert "magic bytes" in str(failed[0].reason)
    assert "another team's" not in str(failed[0].reason)
    assert run.report is not None


def test_one_mapping_turns_a_stage_result_into_an_outcome() -> None:
    """``_from_fetch`` read ``status`` and ``_ran`` read only ``skipped``.

    Nothing showed, because ``fetch`` is the only stage that returns a
    non-``ok`` status today -- but AD-9 gives every stage the same six
    values, so the first other stage to use one would have had its failure
    printed as ``ran``.
    """
    for stage in ("parse", "classify", "aggregate", "render", "fetch"):
        broken = StageResult(
            stage=stage,
            unit=DEMO_A,
            status="parse_failed",
            skipped=False,
            reason="the recording is truncated",
            stats={"next_step": "Download it again."},
        )
        step = pipeline._step_of(broken)
        assert step.outcome == "failed", stage
        assert step.next_step == "Download it again."


def test_pruned_is_a_result_that_stands_and_not_a_failure() -> None:
    """AD-1: "a missing input file with a current result is ``pruned``".

    The result is not overwritten and it is still the answer. Calling it
    ``failed`` would send the reader off to fix a demo the archive
    deliberately no longer keeps.
    """
    from pappascout.constants import UNIT_STATUSES

    pruned = StageResult(
        stage="parse",
        unit=DEMO_A,
        status="pruned",
        skipped=True,
        reason="the demo file has been removed",
        stats={},
    )
    assert pipeline._step_of(pruned).outcome == "skipped"
    # **The partition is total and it is written down.** A seventh status
    # added to ``UNIT_STATUSES`` turns this red, which is the point: it has
    # to be decided, not defaulted.
    assert pipeline._NOT_A_FAILURE == frozenset({"ok", "pruned"})
    assert set(UNIT_STATUSES) - pipeline._NOT_A_FAILURE == {
        "no_demo",
        "download_failed",
        "parse_failed",
        "no_freeze_end",
    }


def test_the_classify_note_does_not_promise_an_aggregation_yet_to_come(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It was the last line before the totals of a run that stopped there.

    "Whatever the archive already holds is still aggregated below" is a
    promise made before it is known whether anything will be, and a reader
    who believed it went looking for a report.
    """
    monkeypatch.setattr(aggregate_stage, "team_keys", lambda _a: [])
    monkeypatch.setattr(
        discover_stage,
        "teams_from_index",
        lambda _d: (subject(lineup_keys=(LINEUP,)),),
    )
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(
            tmp_path, pending=(), present=(), monkeypatch=monkeypatch, recorder=chain
        )
    note = next(
        s
        for s in stopped.value.run.steps
        if s.stage == "classify" and s.outcome == "no-units"
    )
    assert "is still aggregated below" not in str(note.reason)


def test_the_run_reports_how_long_it_took(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SM-1 is measured against this command, and every other one prints it."""
    run = run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert run.duration_s > 0.0


def test_a_stopped_run_reports_how_long_it_took_as_well(
    tmp_path: Path, chain: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run that stopped is the one whose cost is worth knowing."""
    monkeypatch.setattr(aggregate_stage, "team_keys", lambda _a: [])
    with pytest.raises(pipeline.RunStopped) as stopped:
        run_chain(tmp_path, monkeypatch=monkeypatch, recorder=chain)
    assert stopped.value.run.duration_s > 0.0


# --- The stages the legend calls skipless ------------------------------------


def test_the_stages_named_as_skipless_never_return_a_skip() -> None:
    """**The sentence and the behaviour, pinned together.**

    The summary says "discover, select and render have no skip", and until
    2026-09-10 that sentence was held by one literal-string test in the CLI
    while the behaviour was held in three other files. Neither failed if
    only the other changed, and the spec marked this sentence KEEP.

    Now the sentence is built from ``pipeline.NO_SKIP_STAGES`` and this
    reads the three modules' source against the same list, so a stage that
    grew a skip turns both red at once.
    """
    modules = {
        discover_stage.STAGE: discover_stage,
        select_stage.STAGE: select_stage,
        render_stage.STAGE: render_stage,
    }
    assert set(pipeline.NO_SKIP_STAGES) == set(modules)
    for name in pipeline.NO_SKIP_STAGES:
        source = Path(modules[name].__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        skips = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.keyword)
            and node.arg == "skipped"
            and not (
                isinstance(node.value, ast.Constant) and node.value.value is False
            )
        ]
        assert skips == [], (
            f"the summary says {name} has no skip, but {name}.py sets "
            f"skipped to something other than False at lines {skips}"
        )
