"""``pappascout scout`` -- the command and its summary (Story 4.1).

**The summary is the whole interface**, and that is what these tests are
about. A person running the nine single-stage commands sees each result and
adapts; the caller of this one sees only this text, so it has to say what
ran, what was skipped and what failed without the reader inferring any of
the three from a number, an order or an absence.

Four things are locked down:

* the outcome is a **literal word** in the first column, one of the four in
  :data:`~pappascout.stages.pipeline.OUTCOMES`, and every step gets a line
  even when it did nothing;
* a failed step prints **its own next command** underneath it;
* when the run cannot go on, the **summary is printed before the error** --
  the run may have parsed a dozen demos before the step that stopped it;
* the download question is asked **once, up front**, and ``--yes`` asks
  nothing at all.

The pipeline itself is replaced, so none of these tests reads a demo, the
archive or the network. What the pipeline does with the stages is
``tests/test_stage_pipeline.py``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, _render_scout, app, main
from pappascout.domain.models import SETTINGS_ENV_VAR
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import pipeline as pipeline_stage

runner = CliRunner()

TEAM = "Testijoukkue"
TEAM_KEY = "11111111-2222-3333-4444-555555555555"
LINEUP = "aaaaaaaaaaaaaaaa"
DEMO = "1-match-0"
REPORT = f"reports/{LINEUP}/2026-09-10T1200-testijoukkue.md"


def field_value(output_text: str, label: str) -> str:
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"there is no {label!r} row in the output:\n{output_text}")


def step(stage: str, unit: str, outcome: str, **overrides) -> pipeline_stage.Step:
    return pipeline_stage.Step(
        stage=stage, unit=unit, outcome=outcome, **overrides
    )


def a_run(**overrides) -> pipeline_stage.PipelineRun:
    """A finished run: five stages through, one demo lost, one report."""
    render_result = StageResult(
        stage="render",
        unit=LINEUP,
        status="ok",
        skipped=False,
        outputs=(PurePosixPath(REPORT),),
    )
    defaults: dict[str, object] = {
        "team": TEAM,
        "team_key": TEAM_KEY,
        "lineup_key": LINEUP,
        # A finished run has **run** both end-of-run checks and found
        # nothing, which is a different answer from "the check did not
        # run" -- the default on the dataclass. Overriding it here keeps
        # the two apart in every test that starts from a finished run.
        "conflicts": (),
        "lineups_left_out": (),
        "duration_s": 12.3,
        "steps": (
            step("discover", TEAM, "ran", detail="12 teams, 30 matches"),
            step("select", TEAM_KEY, "ran", detail="2 of 5 maps in the sample"),
            step(
                "fetch",
                DEMO,
                "failed",
                reason="download_failed: the interface refused (403).",
                next_step="Ask for the Downloads scope.",
            ),
            step("parse", "1-match-1", "skipped", reason="the manifest matches"),
            step("classify", "1-match-1", "ran", detail="24 rounds"),
            step("aggregate", LINEUP, "ran", detail="2 demos, 48 rounds"),
            step("render", LINEUP, "ran", detail="300 lines", result=render_result),
        ),
    }
    defaults.update(overrides)
    return pipeline_stage.PipelineRun(**defaults)  # type: ignore[arg-type]


# --- The summary ------------------------------------------------------------


def outcome_rows(text: str) -> list[str]:
    """The table's rows: the lines that open with an outcome word.

    A legend line beginning ``ran = ...`` would land here too, which is
    exactly why the legend is written with a label in front of it -- and
    this function is where that would be noticed if it were not.
    """
    return [
        line
        for line in text.splitlines()
        if line[:1].isalpha() and line.split()[0] in pipeline_stage.OUTCOMES
    ]


def test_every_step_is_one_row_that_opens_with_its_outcome() -> None:
    """The three states are told apart by a word, not by inference.

    A symbol, a colour or an absence would each need the reader to know a
    convention. The word is the convention.
    """
    rows = outcome_rows(_render_scout(a_run()))
    assert [row.split()[:2] for row in rows] == [
        ["ran", "discover"],
        ["ran", "select"],
        ["failed", "fetch"],
        ["skipped", "parse"],
        ["ran", "classify"],
        ["ran", "aggregate"],
        ["ran", "render"],
    ]


def test_the_totals_are_counted_over_the_pipelines_own_list_of_words() -> None:
    """One list of words, not two.

    The legend, the totals and the rows all read from
    ``pipeline.OUTCOMES``. A second list in this layer would let the summary
    print a word the pipeline never sets, or leave one out of the totals.

    **This used to assert that each word appeared somewhere in the text**,
    which is true of the legend alone and therefore true of a run with no
    steps at all. The two tests further down replaced that: one checks the
    words are used as rows, the other that the legend explains exactly
    these and no others. What is left here is the totals line, which is the
    third reader of the list.
    """
    from pappascout.cli import OUTCOME_ORDER

    assert OUTCOME_ORDER is pipeline_stage.OUTCOMES
    counted = [
        pair.split()[0]
        for pair in field_value(_render_scout(a_run()), "Totals").split(", ")
    ]
    assert counted == list(pipeline_stage.OUTCOMES)


def test_a_failed_step_prints_its_reason_and_its_next_command() -> None:
    """A failure with no advice makes the reader invent some.

    The same rule as the single-stage summaries: the advice comes from the
    fault, and it is printed where the fault is, not gathered into a heading
    that would give the same sentence to two different faults.
    """
    text = _render_scout(a_run())
    assert "the interface refused (403)." in text
    assert "-> Ask for the Downloads scope." in text


def test_the_totals_count_every_outcome_including_the_empty_ones() -> None:
    """A zero is a measurement; a missing line is a question.

    "failed 0" says the run had no failures. No ``failed`` line at all
    leaves the reader wondering whether failures are counted.
    """
    assert field_value(_render_scout(a_run()), "Totals") == (
        "ran 5, skipped 1, failed 1, no-units 0"
    )


def test_the_report_path_is_in_the_summary() -> None:
    """The caller opens the file next, so the path is not left to be derived."""
    assert field_value(_render_scout(a_run()), "Report") == REPORT


def test_a_run_that_wrote_no_report_says_so_rather_than_leaving_it_out() -> None:
    """An absent line and an absent report look the same; a word does not."""
    text = _render_scout(a_run(steps=(), lineup_key=None))
    assert field_value(text, "Report") == "(none written)"


def test_the_legend_is_not_mistaken_for_a_row_of_the_table() -> None:
    """The legend explains the words; it must not look like a use of them.

    The first draft opened its lines with ``ran = ...``, and both a reader
    scanning the left column and the test above read it as an eighth step.
    """
    assert len(outcome_rows(_render_scout(a_run()))) == len(a_run().steps)


def test_the_summary_names_the_three_stages_that_have_no_skip() -> None:
    """Otherwise ``ran`` on a run that changed nothing reads as a change.

    ``discover`` fetches the match list every time, ``select`` rewrites the
    selection file, and ``render`` writes a report -- a skipped report would
    leave the caller without the file they asked for. All three say so in
    their own module documentation, and the summary repeats it because the
    summary is all the caller sees.
    """
    text = _render_scout(a_run())
    assert "discover, select and render have no skip" in text


def test_the_conflict_check_reports_its_result_three_ways() -> None:
    """AD-7's check has three answers and the summary prints all three.

    A check whose clean result is silence is a check the reader cannot tell
    from one that did not run -- and "none" on a run that never reached the
    scan is the same failure one step further on: a clean result nobody
    measured. A run cancelled at the download question is exactly that run.
    """
    assert field_value(_render_scout(a_run()), "Conflict copies") == "none"
    forked = a_run(conflicts=(PurePosixPath("index/teams (1).json"),))
    assert "index/teams (1).json" in field_value(
        _render_scout(forked), "Conflict copies"
    )
    unchecked = field_value(_render_scout(a_run(conflicts=None)), "Conflict copies")
    assert unchecked != "none"
    assert "not checked" in unchecked


def test_a_long_reason_does_not_destroy_the_columns() -> None:
    """A stage's reason can be a paragraph, and paragraphs go under the row."""
    text = _render_scout(
        a_run(
            steps=(
                step(
                    "fetch",
                    DEMO,
                    "failed",
                    reason="First line.\nSecond line.",
                    next_step="Try again.",
                ),
            )
        )
    )
    rows = text.splitlines()
    assert any(row.strip() == "First line." for row in rows)
    assert any(row.strip() == "Second line." for row in rows)


# --- The command ------------------------------------------------------------


@pytest.fixture
def fake_pipeline(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace ``stages.pipeline.run``; return what the command handed it."""
    seen: dict[str, object] = {}

    def fake_run(settings, archive, team, **kwargs):
        seen["settings"] = settings
        seen["archive"] = archive
        seen["team"] = team
        seen.update(kwargs)
        error = seen.get("error")
        if error is not None:
            raise error  # type: ignore[misc]
        before = kwargs.get("before_download")
        plan = seen.get("plan")
        if before is not None and plan is not None:
            # The real pipeline hands the callback the run as far as it has
            # got, so that the shell can print it if the answer is no.
            before(plan, seen.get("so_far") or a_run(steps=a_run().steps[:2]))
        return seen.get("result") or a_run()

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.pipeline.run", fake_run)
    # The ports are built by the command before the chain starts; none of
    # them may reach the network in a test.
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source", lambda *_a: object()
    )
    return seen


def test_the_command_prints_the_summary(fake_pipeline: dict) -> None:
    result = runner.invoke(app, ["scout", "--team", TEAM, "--yes"])
    assert result.exit_code == 0, result.output
    assert REPORT in result.output
    assert "failed" in result.output


def test_the_command_hands_the_pipeline_the_whole_settings(
    fake_pipeline: dict,
) -> None:
    """The pipeline distributes the sections; the shell does not pre-chop them.

    AD-3 is about what each **stage** sees, and the pipeline is what hands a
    stage its own section. A shell that passed seven sections separately
    would be deciding the wiring, which is the pipeline's job and the reason
    the module exists.
    """
    runner.invoke(app, ["scout", "--team", TEAM, "--yes"])
    settings = fake_pipeline["settings"]
    assert settings.thresholds is not None
    assert settings.parse is not None
    assert fake_pipeline["team"] == TEAM


def test_the_download_port_is_a_factory_and_is_not_built_up_front(
    fake_pipeline: dict,
) -> None:
    """A missing download authorisation must not stop the run before it starts.

    Building the download port needs the token, and the chain's whole point
    today is that it works without one. So the shell hands over a factory
    the pipeline calls only if there is something to download -- and the
    match port, whose absence *is* a reason to stop before anything is
    written, is built eagerly.
    """
    runner.invoke(app, ["scout", "--team", TEAM, "--yes"])
    assert callable(fake_pipeline["demo_source"])
    assert callable(fake_pipeline["parser"])
    assert not callable(fake_pipeline["match_source"])


def test_yes_asks_nothing_even_when_there_is_a_download(
    fake_pipeline: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unattended run must not block on a question.

    The plan is still shown -- ``--yes`` means "do not ask", not "do not
    say" -- and the disk check still runs, because a plan that does not fit
    on the disk is a plan with no right answer whether or not anybody is
    asked about it.
    """
    fake_pipeline["plan"] = fetch_stage.FetchPlan(
        team_key=TEAM_KEY, pending=(DEMO,), estimated_bytes=300 * 1024**2
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _a: 100 * 1024**3)
    result = runner.invoke(app, ["scout", "--team", TEAM, "--yes"], input="")
    assert result.exit_code == 0, result.output
    assert "[y/n]" not in result.output


def test_without_yes_the_question_is_asked_once_for_the_whole_plan(
    fake_pipeline: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One question about the plan, not one per demo.

    The quota and the disk space are spent by the plan as a whole. Twelve
    questions would also be twelve chances for an unattended run to hang.
    """
    fake_pipeline["plan"] = fetch_stage.FetchPlan(
        team_key=TEAM_KEY,
        pending=(DEMO, "1-match-1"),
        estimated_bytes=600 * 1024**2,
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _a: 100 * 1024**3)
    result = runner.invoke(app, ["scout", "--team", TEAM], input="y\n")
    assert result.exit_code == 0, result.output
    assert result.output.count("[y/n]") == 1


@pytest.mark.parametrize(
    "answer", [pytest.param("n\n", id="no"), pytest.param("", id="eof")]
)
def test_a_no_answer_says_the_whole_chain_was_cancelled(
    fake_pipeline: dict, monkeypatch: pytest.MonkeyPatch, answer: str
) -> None:
    """**Not** ``fetch``'s sentence, because this is not ``fetch``'s question.

    A user who was asked and answered has not caused an error, so the run
    still ends at zero. But "No demos were downloaded" is *also* what an
    ordinary ``scout`` run with no download authorisation says -- and that
    run writes a report. A caller told only that, at exit 0, has no way to
    tell the two apart, and an unattended one goes looking for a file that
    was never written. Reading EOF is the same answer by another route and
    gets the same sentence, which is why both are here.

    The summary of what the run already did goes out **before** the
    sentence: without it the caller sees the answer to a question and
    nothing else.
    """
    fake_pipeline["plan"] = fetch_stage.FetchPlan(
        team_key=TEAM_KEY, pending=(DEMO,), estimated_bytes=300 * 1024**2
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _a: 100 * 1024**3)
    result = runner.invoke(app, ["scout", "--team", TEAM], input=answer)
    assert result.exit_code == 0, result.output
    assert "no report was written" in result.output
    assert "the rest of the chain" in result.output
    # The summary first, then the sentence -- a reader stops at the last
    # line they are given.
    assert "ran       discover" in result.output
    assert result.output.index("ran       discover") < result.output.index(
        "no report was written"
    )


def test_the_fetch_commands_own_cancellation_wording_is_untouched(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``scout``'s sentence is its own; it did not replace anybody else's.

    The spec's Never is "do not change what any of the nine commands does
    today", and the cancellation sentence is part of what ``fetch`` does.
    """
    from pappascout.cli import _CANCELLED, _CANCELLED_SCOUT

    assert _CANCELLED == "Cancelled. No demos were downloaded."
    assert _CANCELLED != _CANCELLED_SCOUT


def test_a_stopped_run_prints_the_summary_before_the_error(
    fake_pipeline: dict,
) -> None:
    """The eleven demos that came through are not lost with the twelfth.

    The error alone tells the caller that the run ended; it does not tell
    them what the run did first, and that is what they act on.
    """
    fake_pipeline["error"] = pipeline_stage.RunStopped(
        PappascoutError("Nothing to report.", advice="Import a demo."),
        a_run(steps=a_run().steps[:2], lineup_key=None),
    )
    result = runner.invoke(app, ["scout", "--team", TEAM, "--yes"])
    assert result.exit_code != 0
    assert "ran       discover" in result.output
    assert "(none written)" in result.output


def test_a_stopped_run_ends_with_the_same_exit_code_as_the_single_stage(
    fake_pipeline: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``RunStopped`` carries the steps; it must not change the exit code.

    The message and the advice are asserted by
    :func:`test_a_stopped_run_ends_with_the_original_message_and_advice`,
    which this test's own name used to promise and did not deliver.
    """
    fake_pipeline["error"] = pipeline_stage.RunStopped(
        PappascoutError("Nothing to report.", advice="Import a demo."),
        a_run(steps=(), lineup_key=None),
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "scout", "--team", TEAM, "--yes"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == EXIT_KNOWN_ERROR


def test_the_command_says_it_has_started(fake_pipeline: dict) -> None:
    """A chain over several demos takes minutes; a blank screen says nothing."""
    result = runner.invoke(app, ["scout", "--team", TEAM, "--yes"])
    assert f"Scouting {TEAM}" in result.output


def test_the_team_option_is_required() -> None:
    """Without a team there is no chain to run, and guessing one would be worse."""
    result = runner.invoke(app, ["scout"])
    assert result.exit_code != 0


# --- What the summary must not stop saying -----------------------------------


def a_run_of_every_outcome() -> pipeline_stage.PipelineRun:
    """One run that exercises all four outcome words at once."""
    return a_run(
        steps=(
            step("discover", TEAM, "ran", detail="12 teams, 30 matches"),
            step(
                "fetch",
                DEMO,
                "skipped",
                reason="Already in the archive: the plan found the demo.",
            ),
            step("fetch", "1-match-9", "no-units", reason="the sample is empty"),
            step(
                "parse",
                "1-match-1",
                "failed",
                reason="the recording is truncated",
                next_step="Download it again.",
            ),
        )
    )


def test_every_outcome_word_is_used_as_a_row_and_not_only_explained() -> None:
    """**The old version passed on a run with no steps at all.**

    It asserted that each word appeared *somewhere* in the text -- and all
    four appear in the legend, which is printed whether or not anything
    ran. A summary that had stopped printing rows entirely kept it green.
    """
    rows = outcome_rows(_render_scout(a_run_of_every_outcome()))
    assert {row.split()[0] for row in rows} == set(pipeline_stage.OUTCOMES)


def test_the_legend_explains_exactly_the_words_the_pipeline_defines() -> None:
    """The legend and ``OUTCOMES`` cannot drift apart, in either direction.

    Adding a fifth outcome used to turn only a literal-string test red, so
    the pipeline could grow a word the legend never explained -- or keep an
    explanation for a word it no longer sets. The legend is now **built by
    walking** ``OUTCOMES``, which makes the first direction a ``KeyError``
    from every render; this pins the second.
    """
    from pappascout.cli import _OUTCOME_LEGEND

    assert set(_OUTCOME_LEGEND) == set(pipeline_stage.OUTCOMES)
    text = _render_scout(a_run())
    for word, meaning in _OUTCOME_LEGEND.items():
        assert f"{word} = " in " ".join(text.split()), word
        assert meaning.split()[0] in " ".join(text.split()), word


def test_a_skipped_row_still_says_why_it_was_skipped() -> None:
    """The reason is how a caller learns that nothing needed doing.

    Removing the reason from ``skipped`` and ``no-units`` rows left all 86
    tests green: only the ``failed`` reason was pinned. Those two lines are
    the whole answer to "why did nothing happen", which is the question a
    model asks first.
    """
    text = _render_scout(a_run_of_every_outcome())
    assert "Already in the archive: the plan found the demo." in text


def test_a_no_units_row_still_says_why_there_were_no_units() -> None:
    """Same rule, the other word. ``skipped`` and ``no-units`` differ, and
    the difference is entirely in the reason: one compared something and
    one had nothing to compare.
    """
    text = _render_scout(a_run_of_every_outcome())
    assert "the sample is empty" in text


def test_the_detail_column_is_printed() -> None:
    """The stage's own numbers are the only measurement on the row.

    Dropping the column left 20 tests green. "ran parse 1-match-1" with no
    numbers says the stage was called and nothing about what it found.
    """
    text = _render_scout(a_run())
    assert "12 teams, 30 matches" in text
    assert "2 of 5 maps in the sample" in text
    assert "2 demos, 48 rounds" in text


def test_the_team_and_the_lineup_are_named() -> None:
    """The lineup key is the directory the report lands in.

    Both lines could vanish with 51 tests still green. Without the team the
    summary does not say whose run it is; without the lineup the caller
    cannot find the report tree, and the key is what every follow-up
    command is addressed by.
    """
    text = _render_scout(a_run())
    assert field_value(text, "Team") == f"{TEAM} ({TEAM_KEY})"
    assert field_value(text, "Lineup") == LINEUP


def test_the_run_time_is_printed() -> None:
    """Every other command prints one, and SM-1 is measured against this one."""
    assert field_value(_render_scout(a_run()), "Run time") == "12,3 s"


def test_a_steps_own_time_is_printed_when_there_is_one() -> None:
    """Which of twelve demos cost the minutes is a question only this answers.

    ``Step.duration_s`` was set in eleven places and read in none.
    """
    text = _render_scout(
        a_run(steps=(step("parse", DEMO, "ran", detail="24 rounds", duration_s=4.2),))
    )
    assert "[4,2 s]" in text
    # A time too small to measure is left off rather than printed as a
    # zero, which would be a measurement of nothing on almost every row.
    quick = _render_scout(
        a_run(steps=(step("parse", DEMO, "ran", detail="24 rounds"),))
    )
    assert "0,0 s" not in quick


def test_the_lineups_left_out_are_named_with_what_to_do_about_them() -> None:
    """**The demos behind these keys leave no other trace.**

    They are classified as this team, they are not in the report, and they
    are not in the report's missing-demos list either, because the
    aggregation never looked at their lineup. This row is the only place
    they appear, so it is printed on every run -- including the ordinary
    one, where it says ``none`` and thereby says the check ran.
    """
    assert field_value(_render_scout(a_run()), "Lineups left out") == "none"
    text = _render_scout(a_run(lineups_left_out=("bbbbbbbbbbbbbbbb",)))
    assert "bbbbbbbbbbbbbbbb" in field_value(text, "Lineups left out")
    assert "pappascout aggregate --team" in " ".join(text.split())
    unchecked = field_value(
        _render_scout(a_run(lineups_left_out=None)), "Lineups left out"
    )
    assert "not checked" in unchecked


def test_a_stopped_run_still_reports_the_conflict_check() -> None:
    """AD-7's check runs on the way out of a stopped run too.

    It could disappear from that path with 55 tests green, and a stopped
    run is a likely place for a forked archive: two machines writing at
    once is what stops a run in the first place.
    """
    stopped = a_run(
        steps=a_run().steps[:2],
        lineup_key=None,
        conflicts=(PurePosixPath("index/teams (1).json"),),
    )
    assert "index/teams (1).json" in field_value(
        _render_scout(stopped), "Conflict copies"
    )


def test_a_stopped_run_ends_with_the_original_message_and_advice(
    fake_pipeline: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The name promised three things and only the exit code was asserted.

    ``RunStopped`` carries the steps; it must not swallow the fault. The
    caller gets the same message, the same advice and the same exit code as
    they would from the single-stage command that raised it -- and losing
    the message or the advice left 20 tests green.
    """
    fake_pipeline["error"] = pipeline_stage.RunStopped(
        PappascoutError("Nothing to report.", advice="Import a demo."),
        a_run(steps=a_run().steps[:2], lineup_key=None),
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "scout", "--team", TEAM, "--yes"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == EXIT_KNOWN_ERROR
    printed = capsys.readouterr()
    both = printed.out + printed.err
    assert "Nothing to report." in both
    assert "Import a demo." in both


def test_the_command_hands_over_the_settings_it_loaded_and_not_a_section(
    fake_pipeline: dict, settings_file: Path
) -> None:
    """The pipeline distributes the sections; the shell does not pre-chop them.

    AD-3 is about what each **stage** sees, and the pipeline is what hands
    a stage its own section. A shell that passed seven sections separately
    would be deciding the wiring, which is the pipeline's job and the
    reason the module exists.

    The old assertion was ``settings.thresholds is not None``, which is
    true of every ``Settings`` ever constructed and would have stayed true
    of one built from thin air here. This one pins the object: it is the
    whole settings, loaded from the file the command was pointed at, and
    no section was passed beside it.
    """
    from pappascout.domain.models import Settings, load_settings

    runner.invoke(app, ["scout", "--team", TEAM, "--yes"])
    settings = fake_pipeline["settings"]
    assert isinstance(settings, Settings)
    assert settings == load_settings(settings_file, env_files=())
    assert fake_pipeline["team"] == TEAM
    # Nothing was chopped off and handed over separately.
    assert set(fake_pipeline) & {
        "thresholds",
        "league",
        "parse",
        "economy",
        "aggregate",
        "report",
        "project",
    } == set()
