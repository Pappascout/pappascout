"""``pappascout fetch`` -- the command's tests (Story 3.4).

Four things are locked down here:

* **The question before the download.** The command says how many demos are
  fetched, where to and how much space they take, and waits for an answer.
  ``--yes`` skips the question -- but not the printing of the plan.
* **A negative answer downloads nothing.** Cancelling the confirmation is a
  cancellation, not a delay.
* **The layering rule.** The command imports neither the adapters nor the
  archive: the port comes from ``stages.fetch.default_source`` and the paths
  from ``stages.archive_paths``.
* **Anything other than ``ok`` shows with its reason.** A demo that was
  removed and a connection that broke are different next steps, and they must
  not look the same on the screen.

The whole ``discover`` -> ``select`` -> ``fetch`` chain is run with the real
stages behind fake ports: that is the only way to prove that the selection
file's shape and its reader stay together.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_stage_discover import CHAMPIONSHIP, FakeSource, division_matches
from conftest import LOCAL_DEMOS_DIRNAME
from pappascout.archive.paths import DEMOS_ROOT_ENV_VAR
from test_stage_fetch import DEMO_BYTES, FakeDemo, FakeDemoSource
from typer.testing import CliRunner

from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    MAX_LISTED_UNITS,
    _fetch_notes,
    _render_fetch,
    _render_fetch_plan,
    _render_info,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR
from pappascout.stages import StageResult, archive_paths
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import select as select_stage

runner = CliRunner()

SUBJECT = "Potku"


@pytest.fixture(params=["arkisto", "paikallinen"])
def pipeline(request, settings_file: Path, tmp_path: Path, monkeypatch):
    """The real stages, fake ports, the archive in a temporary directory.

    **Both demo directory modes, in every test.** Without a variable the
    demos go into the archive, but ``PAPPASCOUT_DEMOS_ROOT`` is a supported
    mode -- and a mode the command tests do not run goes through the CLI zero
    times. Turning the gap the other way round is not a fix.

    Returns ``(archive, source, units)``.
    """
    if request.param == "paikallinen":
        monkeypatch.setenv(DEMOS_ROOT_ENV_VAR, str(tmp_path / "paikalliset"))
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )

    from pappascout.domain.models import load_settings

    settings = load_settings()
    archive = archive_paths(settings.project)
    discover_stage.run(
        settings.league,
        archive,
        None,
        source=FakeSource({CHAMPIONSHIP: division_matches()}),
        thresholds=settings.thresholds,
    )
    result = select_stage.run(
        settings.league, archive, SUBJECT, thresholds=settings.thresholds
    )
    team_key = result.unit

    document = select_stage.read_selection(archive, team_key)
    units = [
        row["map_demo_id"] for row in document["selections"] if row["roster_ok"]
    ]
    assert units, "the data produced no selected map at all"

    source = FakeDemoSource({unit: FakeDemo(DEMO_BYTES) for unit in units})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source",
        lambda settings, archive: source,
    )
    # The disk must not be a variable of the test: the check is its own test.
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)
    return archive, source, units


def test_the_plan_is_shown_and_confirmed_before_anything_is_downloaded(
    pipeline,
) -> None:
    archive, source, units = pipeline

    # ``k`` and not ``y``: the Finnish answers the tool asked for until
    # 2026-09-09 stay accepted (``_CONSENT_ANSWERS``), and this is the one place
    # that still exercises them.
    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="k\n")

    assert result.exit_code == 0, result.output
    # **The whole figure, not a substring.** A single-digit ``str(len(units))``
    # would hit a map id and the run time, and the claim would pass even when
    # the screen shows some entirely different number.
    assert f"{len(units)} to download" in result.output
    assert "Download these demos?" in result.output
    assert source.asked == units
    for unit in units:
        assert archive.demo(unit).read_bytes() == DEMO_BYTES


def test_answering_no_downloads_nothing(pipeline) -> None:
    archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="n\n")

    # A negative answer is not an error: the user was asked and answered.
    assert result.exit_code == 0, result.output
    assert "Cancelled" in result.output
    assert source.asked == []
    assert archive.find_demo(units[0]) is None


def test_the_question_offers_its_own_options_and_not_typers(pipeline) -> None:
    """The prompt is this tool's own, and its options say so.

    ``typer.confirm`` prints ``[y/N]`` and aborts with ``Aborted.`` and a
    non-zero exit code, which would tell a user who answered the question
    that something went wrong. The name used to say the options are in
    Finnish; AD-11 moved the console into English, and what the assertions
    pin is that this is not ``typer.confirm``.
    """
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="n\n")

    assert "[y/n]" in result.output
    assert "[y/N]" not in result.output
    assert "Aborted" not in result.output
    assert source.asked == []


def test_y_is_the_answer_that_downloads(pipeline) -> None:
    _archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="y\n")

    assert result.exit_code == 0, result.output
    assert source.asked == units


def test_an_empty_answer_does_not_download(pipeline) -> None:
    """Enter is not yes: the default is the one that spends no quota and no disk."""
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="\n")

    assert result.exit_code == 0, result.output
    assert source.asked == []


def test_an_unrecognised_answer_does_not_download(pipeline) -> None:
    """A misread answer must not lead to a 2.3 GB download."""
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="maybe\n")

    assert result.exit_code == 0, result.output
    assert source.asked == []


def test_yes_skips_the_question_but_not_the_plan(pipeline) -> None:
    archive, source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    assert "Download these" not in result.output
    assert f"{len(units)} to download" in result.output
    assert source.asked == units


def test_the_plan_names_the_target_directory(pipeline) -> None:
    archive, _source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="n\n")

    assert str(archive.demos_dir()) in result.output


def test_a_second_run_downloads_nothing_and_says_so(pipeline) -> None:
    _archive, source, units = pipeline

    first = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])
    assert first.exit_code == 0, first.output
    source.asked.clear()

    second = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert second.exit_code == 0, second.output
    assert source.asked == []
    assert "nothing to download" in second.output


def test_a_missing_demo_is_listed_with_its_reason(
    pipeline, monkeypatch
) -> None:
    """A removed demo is not an error, but it has to be reported."""
    from pappascout.errors import DemoUnavailable

    _archive, source, units = pipeline
    source.demos[units[0]] = DemoUnavailable(
        "FACEIT has removed the recording (retention is about 30 days)."
    )

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    assert "Not available" in result.output
    assert "30 days" in result.output
    assert units[0] in result.output


def test_a_failed_download_is_listed_separately_from_a_missing_one(
    pipeline,
) -> None:
    from pappascout.errors import ApiError

    _archive, source, units = pipeline
    source.demos[units[0]] = ApiError("The interface did not answer.", status_code=503)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    assert "Failed" in result.output
    # The heading **states only what happened**: the advice comes with the
    # fault (D1).
    assert "Failed (1) -- run the command again" not in result.output


def test_a_full_disk_stops_the_command_before_the_question(
    pipeline, monkeypatch, capsys
) -> None:
    _archive, source, _units = pipeline
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 1024)
    monkeypatch.setattr("sys.argv", ["pappascout", "fetch", "--team", SUBJECT])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    assert "no room for a single demo" in captured.err + captured.out
    assert "Download these" not in captured.out
    assert source.asked == []


def test_without_a_selection_file_the_error_says_to_run_select(
    settings_file, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    runner.invoke(app, ["discover"])
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--yes"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    assert "select" in captured.err + captured.out


def test_the_local_demos_root_variable_moves_the_files_out_of_the_archive(
    settings_file, local_demos_root, monkeypatch
) -> None:
    """``PAPPASCOUT_DEMOS_ROOT`` points the demos outside the synchronised folder.

    Unset, the demos go into the archive -- the default, because the archive
    follows from one machine to the other and the sync client frees a parsed
    demo's space without deleting the file. The other mode is supported even
    so, and this test runs the whole command with a real settings file: the
    variable, ``ArchivePaths`` and the stage are proved together, because not
    one of them alone shows that the file ends up in another directory.
    """
    from pappascout.domain.models import load_settings

    local = local_demos_root
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root == local

    units = _prepare(archive, monkeypatch)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (local / f"{unit}.dem.zst").read_bytes() == DEMO_BYTES
        assert (local / f"{unit}.meta.json").is_file()
    assert not archive.archive_demos_dir().exists()
    assert str(local) in result.output


def _prepare(archive, monkeypatch) -> list[str]:
    """Run discover and select, and wire the demo source to the chosen maps."""
    runner.invoke(app, ["discover"])
    runner.invoke(app, ["select", "--team", SUBJECT])
    document = json.loads(
        next(archive.resolve("index/selections").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    units = [r["map_demo_id"] for r in document["selections"] if r["roster_ok"]]
    source = FakeDemoSource({unit: FakeDemo(DEMO_BYTES) for unit in units})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda s, a: source
    )
    return units


# -- The numbers on the screen (B3, 2026-09-05) ------------------------------
#
# **The confirmation question's whole value is in the numbers**: "Demos 12 to
# download, an estimated 2,6 Gt" is a question that can be answered, and
# "Demos 1001 to download, an estimated 168,1 Gt" is a different question. If
# the numbers are not guarded they can be anything and nothing notices -- and
# the user would answer the wrong question.


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (0, "0 tavua"),
        (1023, "1023 tavua"),
        (1024, "1,0 kt"),
        (1536, "1,5 kt"),
        (1024**2, "1,0 Mt"),
        (234_163_493, "223,3 Mt"),
        (fetch_stage.DEMO_SIZE_ESTIMATE_BYTES, "256,0 Mt"),
        (12 * 223 * 1024**2, "2,6 Gt"),
        (1024**4, "1,0 Tt"),
    ],
)
def test_sizes_are_formatted_with_a_finnish_decimal_comma(
    num_bytes: int, expected: str
) -> None:
    assert fetch_stage.size_fi(num_bytes) == expected


def test_the_plan_line_says_the_real_count_and_the_real_size() -> None:
    """The plan's numbers come from the plan and from nowhere else."""
    todo = fetch_stage.FetchPlan(
        team_key="joukkue",
        pending=("a-0", "a-1", "b-0"),
        present=("c-0",),
        estimated_bytes=3 * 223 * 1024**2,
    )

    text = _render_fetch_plan(todo, 9_900_000_000, r"D:\demot")

    assert "Sample: 4 maps, of which 1 already on disk" in text
    assert "3 to download, an estimated 669,0 Mt" in text
    assert r"D:\demot" in text
    assert "9,2 Gt" in text
    for unit in todo.pending:
        assert unit in text


def test_the_plan_line_scales_with_the_plan() -> None:
    """The same function, a different plan, different numbers."""
    small = _render_fetch_plan(
        fetch_stage.FetchPlan("t", pending=("a-0",), estimated_bytes=1024**2),
        None,
        "kohde",
    )
    large = _render_fetch_plan(
        fetch_stage.FetchPlan(
            "t", pending=tuple(f"a-{i}" for i in range(1001)), estimated_bytes=1024**4
        ),
        None,
        "kohde",
    )

    assert "1 to download, an estimated 1,0 Mt" in small
    assert "1001 to download, an estimated 1,0 Tt" in large
    # The free space is not known: the row is not invented.
    assert "Free disk space" not in small


def fetch_result(unit: str, status: str, **stats) -> StageResult:
    """The stage's result, for testing the output.

    ``next_step`` exists by default on the failures, because the stage does
    not produce such a row without it (``stages.fetch._result`` guards that).
    """
    if status != "ok":
        stats.setdefault("next_step", "Do something.")
    return StageResult(
        stage="fetch",
        unit=unit,
        status=status,
        skipped=stats.pop("skipped", False),
        reason=stats.pop("reason", None),
        duration_s=stats.pop("duration_s", 1.0),
        stats={"downloaded_bytes": 0, **stats},
    )


def test_the_summary_counts_every_status_separately() -> None:
    """Four numbers, four different next steps -- and none may leak into another."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",), present=("z-0", "z-1"))
    results = (
        fetch_result("a-0", "ok", downloaded_bytes=1024**2, demos_dir=r"D:\demot"),
        fetch_result("b-0", "ok", downloaded_bytes=2 * 1024**2, demos_dir=r"D:\demot"),
        fetch_result("c-0", "ok", skipped=True),
        fetch_result("d-0", "no_demo", reason="FACEIT removed the recording."),
        fetch_result("e-0", "download_failed", reason="The connection broke."),
    )

    text = _render_fetch(results, todo)

    assert "2 fetched" in text
    # The skipped ones + the plan's already-on-disk ones: 1 + 2.
    assert "3 already on disk" in text
    assert "1 not available" in text
    assert "1 failed" in text
    assert "Written" in text and "3,0 Mt" in text
    assert r"D:\demot" in text


def test_the_summary_lists_every_reason_not_just_the_count() -> None:
    """A removed demo and a broken connection are different next steps."""
    todo = fetch_stage.FetchPlan("t", pending=())
    results = (
        fetch_result("d-0", "no_demo", reason="FACEIT removed the recording."),
        fetch_result("e-0", "download_failed", reason="The connection broke."),
    )

    text = _render_fetch(results, todo)

    assert "Not available (1)" in text
    assert "FACEIT removed the recording." in text
    assert "Failed (1)" in text
    assert "The connection broke." in text
    assert "d-0" in text and "e-0" in text


def test_the_summary_sums_the_real_byte_counts() -> None:
    todo = fetch_stage.FetchPlan("t", pending=())
    results = tuple(
        fetch_result(f"a-{i}", "ok", downloaded_bytes=100 * 1024**2)
        for i in range(12)
    )

    text = _render_fetch(results, todo)

    assert "12 fetched" in text
    assert "Written" in text and "1,2 Gt" in text


# -- Story 3.7: the sister fixes ----------------------------------------------


def test_a_single_map_sample_says_one_map_not_one_maps() -> None:
    """The I/O matrix: a one-map sample -> "1 map", not "1 maps".

    The word was hard-coded although :func:`_maps` existed and ``collect``
    used it correctly. A one-map sample is not a rarity: the season's first
    run lands on exactly that.

    **The claim covers the whole sentence and not only the numeral.** The
    review of 2026-09-06 found that the first fix moved the mistake one word
    later ("1 kartta, **joista** 0"), and a test that looked only as far as
    the beginning locked the wrong form in place.

    In Finnish the relative pronoun took the same number as the noun; English
    has one form, so :func:`_of_which` returns the same word either way
    and the third assertion below only proves the sister word is still in the
    sentence.
    """
    one = _render_fetch_plan(
        fetch_stage.FetchPlan("t", pending=("a-0",), estimated_bytes=1024**2),
        None,
        "kohde",
    )
    many = _render_fetch_plan(
        fetch_stage.FetchPlan(
            "t", pending=("a-0", "a-1"), estimated_bytes=2 * 1024**2
        ),
        None,
        "kohde",
    )

    # The ``replace`` folded a Finnish letter to ASCII back when this
    # sentence was Finnish. It is a no-op now and is kept so that the diff
    # reads as a translation.
    assert "Sample: 1 map, of which 0 already on disk" in one.replace(
        "ä", "a"
    )
    assert "1 maps" not in one
    # In Finnish this word inflected with the count and the assertion was
    # ``"joista" not in yksi``. English has one form, so all that is left to
    # claim is that the sister helper is still in the sentence.
    assert "of which" in one
    assert "Sample: 2 maps, of which 0 already on disk" in many.replace(
        "ä", "a"
    )


def test_the_fetch_plan_warns_when_it_does_not_fit_on_disk() -> None:
    """The I/O matrix: the plan does not fit -> a warning row, not a gate.

    The row was added in Story 3.5 to ``collect`` only, but it is not about
    the division: a sample of 12 demos fits on a 1 GB disk no better than one
    of 132, and the user confirms the same question here.
    """
    todo = fetch_stage.FetchPlan(
        "t",
        pending=tuple(f"a-{i}" for i in range(12)),
        estimated_bytes=12 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES,
    )

    tight = _render_fetch_plan(todo, 2560 * 1024**2, "kohde")
    roomy = _render_fetch_plan(todo, 100 * 1024**3, "kohde")

    assert "NOTE" in tight and "does not fit on the disk" in tight
    assert "NOTE" not in roomy


def test_the_fetch_plan_truncates_a_long_listing() -> None:
    """The I/O matrix: over 20 ids -> the listing is cut and says how many.

    The same ceiling and the same reason as ``collect``'s: over a hundred
    rows of ids would scroll off the screen exactly the rows the plan is
    printed for -- the target, the free space and the question itself.
    """
    todo = fetch_stage.FetchPlan(
        "t",
        pending=tuple(f"a-{i}" for i in range(25)),
        estimated_bytes=25 * 1024**2,
    )

    text = _render_fetch_plan(todo, None, "kohde")

    assert text.count("\n  a-") == MAX_LISTED_UNITS
    assert "(+5 more" in text


def test_a_successful_download_shows_its_note_on_screen() -> None:
    """The I/O matrix: a successful download whose length could not be checked.

    ``reason`` was printed only in the failure blocks, so a note on a
    ``status="ok"`` result was never seen. ``fetch._unverified_note`` is one
    of them, and its own documentation says the stage **"has to say it"** --
    and an unspoken uncertainty looks like certainty.
    """
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    note = fetch_stage._unverified_note("a-0")
    results = (
        fetch_result("a-0", "ok", downloaded_bytes=1024**2, reason=note),
        fetch_result("b-0", "ok", downloaded_bytes=1024**2),
    )

    text = _render_fetch(results, todo)

    assert "Notes (1)" in text
    assert "did not state the size of demo a-0" in text
    assert "b-0" not in text


def test_a_skipped_download_does_not_repeat_its_note_for_every_unit() -> None:
    """A skipped result's ``reason`` is "already on disk" -- already a count.

    A listing of the same sentence as long as the sample would drown exactly
    the rows the block exists for. This test looks through the output;
    :func:`test_the_note_block_filters_skipped_results_itself` looks at the
    same rule in the function's own body.
    """
    todo = fetch_stage.FetchPlan("t", pending=())
    results = tuple(
        fetch_result(
            f"a-{i}", "ok", skipped=True, reason="The demo was already there."
        )
        for i in range(12)
    )

    text = _render_fetch(results, todo)

    assert "Notes" not in text


def test_the_note_block_filters_skipped_results_itself() -> None:
    """**The rule is in the function, not at the call site.**

    The review of 2026-09-06: the docstring promised "only the downloaded
    ones", but the removal of the skipped ones was in :func:`_render_fetch`
    (it passed a pre-filtered list). A rule that is not where its
    documentation is disappears with the next call site -- and that call site
    would get a listing as long as the sample of the sentence "the demo was
    already on disk". So this test calls :func:`_fetch_notes` **directly** and
    gives it an unfiltered list.
    """
    results = (
        fetch_result(
            "a-0", "ok", skipped=True, reason="The demo was already there."
        ),
        fetch_result("b-0", "no_demo", reason="FACEIT removed the recording."),
        fetch_result("c-0", "download_failed", reason="The connection broke."),
        fetch_result(
            "d-0", "ok", reason="Downloaded, but the length was not confirmed."
        ),
    )

    lines = _fetch_notes(results)

    text = "\n".join(lines)
    assert "Notes (1)" in text
    assert "d-0" in text and "length was not confirmed" in text
    # The skipped, the not-available and the failed do not belong in this
    # block: the last two have a block of their own with their advice
    # (``_fetch_failures``).
    assert "a-0" not in text
    assert "b-0" not in text
    assert "c-0" not in text


def test_the_note_block_is_empty_when_there_is_nothing_to_say() -> None:
    """An empty block would be a heading with no content."""
    assert _fetch_notes(()) == []
    assert _fetch_notes((fetch_result("a-0", "ok", reason=None),)) == []
    assert _fetch_notes((fetch_result("a-0", "ok", reason="   "),)) == []


def test_a_failed_orphan_removal_reaches_the_screen() -> None:
    """The I/O matrix: an orphaned metadata file that cannot be removed.

    The stage's note and the command's output are different things: a note
    that comes into being in the result but not on the screen is the same
    thing as silence.
    """
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result(
            "a-0",
            "ok",
            downloaded_bytes=1024**2,
            # The stage's own wording (``fetch._orphan_note``), so the needles
            # below stay pointed at the note this test exists for.
            reason=(
                "WARNING: the old metadata file D:\\demot\\a-0.meta.json "
                "could not be removed"
            ),
        ),
    )

    text = _render_fetch(results, todo)

    assert "WARNING" in text
    assert "was removed" not in text


def test_the_same_byte_count_prints_the_same_string_in_every_command() -> None:
    """The acceptance criterion: the same byte count, the same string.

    ``cli`` formatted bytes with a table of its own (``Pt`` included) and
    ``fetch`` with one of its own (``Tt`` last), so the same number could
    print differently depending on which command printed it.
    """
    bytes_text = 234_163_493  # the archive's largest compressed demo, 223,3 Mt

    plan = _render_fetch_plan(
        fetch_stage.FetchPlan("t", pending=("a-0",), estimated_bytes=bytes_text),
        bytes_text,
        "kohde",
    )
    summary = _render_fetch(
        (fetch_result("a-0", "ok", downloaded_bytes=bytes_text),),
        fetch_stage.FetchPlan("t", pending=("a-0",)),
    )

    expected = fetch_stage.size_fi(bytes_text)
    assert expected == "223,3 Mt"
    assert plan.count(expected) == 2  # the estimate and the free space
    assert expected in summary


# -- The default mode goes through the command (B6) --------------------------


def test_without_the_demos_root_variable_the_demos_go_into_the_archive(
    settings_file, monkeypatch
) -> None:
    """The default that ships: the demos into the archive's ``demos/``.

    Decided 2026-09-05. The archive is in a synchronised folder and follows
    from one machine to the other, so the project's whole state moves as one
    -- and the sync client's on-demand mode frees a parsed demo's local space
    **without deleting the file**, which in a local folder would be a
    permanent deletion.
    """
    from pappascout.domain.models import load_settings

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root is None

    units = _prepare(archive, monkeypatch)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (archive.root / "demos" / f"{unit}.dem.zst").is_file()
        assert (archive.root / "demos" / f"{unit}.meta.json").is_file()
    assert "(local)" not in result.output


# -- The disk space gate really reaches the stage (B9) -----------------------


def test_a_disk_that_fills_up_between_demos_stops_only_that_demo(
    pipeline, monkeypatch
) -> None:
    """The stage's own gate, not the command's: space can run out mid-series.

    The test also proves that the stage's ``disk_free`` really asks
    :func:`free_space` at run time -- a function bound as a default value
    would not react to this monkeypatch at all, and the test would pass only
    because the machine has space.
    """
    _archive, source, units = pipeline
    calls = {"n": 0}

    def shrinking(_archive):
        calls["n"] += 1
        return 100 * 1024**3 if calls["n"] <= 2 else 1024

    monkeypatch.setattr(fetch_stage, "free_space", shrinking)

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    assert "Not enough disk space" in result.output
    assert len(source.asked) < len(units)


# -- The info command's Demos row (B7) ---------------------------------------


def test_info_names_the_demo_directory(
    settings_file, local_demos_root, monkeypatch
) -> None:
    from pappascout.domain.models import load_settings

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    rendered = _render_info(load_settings())

    assert f"Demos              {local_demos_root} (local)" in rendered


def test_info_says_the_archive_directory_when_there_is_no_local_one(
    settings_file, monkeypatch
) -> None:
    """``(local)`` is a claim, not a decoration: not in the wrong mode."""
    from pappascout.domain.models import load_settings

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    settings = load_settings()
    rendered = _render_info(settings)
    archive = archive_paths(settings.project)

    assert f"Demos              {archive.root / 'demos'}" in rendered
    assert "(local)" not in rendered


# -- The target directory is part of the question (product owner 2026-09-05) -
#
# The product owner suggested that the tool should ask where to save. No
# separate question is asked -- the environment variable and the info command
# are already that answer, and a question repeated on every run would be noise.
# Instead **the confirmation question names the target** just as it gives the
# count and the size: the user sees where the writing is about to go at the
# moment it is decided, and can stop if it is wrong. The path is therefore to
# be guarded as closely as the numbers.


def test_the_target_directory_is_shown_before_the_question(pipeline) -> None:
    archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="n\n")

    target = [r for r in result.output.splitlines() if r.strip().startswith("Target")]
    assert target, f"the plan has no Target row:\n{result.output}"
    assert str(archive.demos_dir()) in target[0]
    # The question comes only after the target: otherwise it would be seen
    # only after answering.
    assert result.output.index("Target") < result.output.index("Download these")
    assert source.asked == []


def test_the_shown_target_is_the_directory_that_is_written_to(pipeline) -> None:
    """The path must not be a decoration: it has to be where the files go.

    A fixed or wrong path would pass every claim that checks mere existence
    -- and the user would approve a download to the wrong place believing
    they had checked it.
    """
    archive, _source, units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT, "--yes"])

    assert result.exit_code == 0, result.output
    written = archive.find_demo(units[0])
    assert written is not None
    target = [r for r in result.output.splitlines() if r.strip().startswith("Target")]
    assert target, f"the output has no Target row:\n{result.output}"
    assert all(str(written.parent) in row for row in target)


def test_the_target_line_distinguishes_the_two_modes(tmp_path) -> None:
    """The same row, a different mode, a different path.

    The target comes from ``demos_dir()``, so a fixed path would pass every
    claim that checks mere existence.
    """
    from pappascout.archive.paths import ArchivePaths

    archive_dir = ArchivePaths(root=tmp_path / "arkisto")
    local = ArchivePaths(
        root=tmp_path / "arkisto", demos_root=tmp_path / LOCAL_DEMOS_DIRNAME
    )

    todo = fetch_stage.FetchPlan("t", pending=("a-0",), estimated_bytes=1024**2)
    archive_row = _render_fetch_plan(todo, None, str(archive_dir.demos_dir()))
    local_row = _render_fetch_plan(todo, None, str(local.demos_dir()))

    assert str(tmp_path / "arkisto" / "demos") in archive_row
    assert str(tmp_path / LOCAL_DEMOS_DIRNAME) in local_row
    assert archive_row != local_row


# -- The first real run against the network (2026-09-05) ---------------------


@pytest.fixture
def denied_pipeline(pipeline, monkeypatch):
    """The same chain, but the real adapter answers the signing call with 403.

    **The real adapter and not a fake port**, because that is exactly where
    the seam differed: a fake cannot produce the Downloads API's 403, and
    that is why two reviews and the whole test suite missed a fault the first
    real run found in seven seconds.
    """
    from test_faceit_demos import DOWNLOADS, FakeResponse, FakeSession, build

    archive, _source, units = pipeline
    session = FakeSession(sign=FakeResponse(403), download=FakeResponse(200))
    real_source = build(archive.root.parent, session)
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda s, a: real_source
    )
    assert real_source.downloads_base_url == DOWNLOADS
    return archive, session, units


def test_a_denied_downloads_token_does_not_tell_the_user_to_retry(
    denied_pipeline, monkeypatch, capsys
) -> None:
    """**C1.** "Run the command again" does not help until the application is
    approved.

    Measured 2026-09-05: the product owner's application was in the queue
    ("waiting for review"), and the tool sorted the 403 under the heading
    "Failed (2) -- run the command again". The advice was wrong, and it would
    have repeated on every run.
    """
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--yes"]
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    out = capsys.readouterr()
    text = out.out + out.err
    assert "aja komento uudelleen" not in text.lower()
    # ``fetch.run_many``'s progress note is English and rides in this same
    # text, so the Finnish needle alone no longer covers the whole output.
    assert "run the command again" not in text.lower()
    assert "Failed" not in text
    assert "fc-downloads.loza.gg" in text
    assert "downloads-api-application" in text
    assert "FACEIT_DOWNLOADS_TOKEN" in text


def test_only_one_signing_call_is_made_before_the_run_stops(
    denied_pipeline, monkeypatch, capsys
) -> None:
    """**C2.** With twelve demos this would have been 12 doomed calls."""
    _archive, session, units = denied_pipeline
    assert len(units) > 1, "the data proves nothing with one unit"
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--yes"]
    )

    with pytest.raises(SystemExit):
        main()

    capsys.readouterr()
    assert len(session.posts) == 1


def test_the_denied_message_reaches_the_screen_and_says_waiting_helps(
    denied_pipeline, monkeypatch, capsys
) -> None:
    _archive, _session, _units = denied_pipeline
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "fetch", "--team", SUBJECT, "--yes"]
    )

    with pytest.raises(SystemExit):
        main()

    text = "".join(capsys.readouterr())
    assert "The Downloads API is a separate authorisation" in text
    assert "Waiting" in text


# -- Not answering is an answer (C3) -----------------------------------------


def test_no_input_at_all_is_cancelled_not_aborted(pipeline) -> None:
    """**C3.** ``typer`` aborts on EOF with a message of its own, ``Aborted.``

    That happens **before** the confirmation's own code sees anything, so
    A7's fix did not cover this route: the same word claiming a failure came
    by another way. A pipe, a scheduler and Ctrl-C all land here. The name
    used to say the cancellation is in Finnish; AD-11 made that false.
    """
    _archive, source, _units = pipeline

    result = runner.invoke(app, ["fetch", "--team", SUBJECT], input="")

    assert result.exit_code == 0, result.output
    assert "Aborted" not in result.output
    assert "Cancelled. No demos were downloaded." in result.output
    assert source.asked == []


def test_the_cancel_message_is_the_same_however_the_user_declines(
    pipeline,
) -> None:
    """Answering no and not answering at all are the same outcome.

    Two different wordings would suggest that they differ.
    """
    _archive, _source, _units = pipeline

    refusal = runner.invoke(app, ["fetch", "--team", SUBJECT], input="n\n")
    empty = runner.invoke(app, ["fetch", "--team", SUBJECT], input="")

    assert "Cancelled. No demos were downloaded." in refusal.output
    assert "Cancelled. No demos were downloaded." in empty.output
    assert refusal.exit_code == empty.exit_code == 0


# -- D1: the advice belongs to the fault, not to the heading (live 2026-09-05)
#
# Two consecutive real runs found the same pattern: the classification was
# right but the advice was wrong, because the advice came from the heading
# "Failed (N) -- run the command again". That is a bucket that collects both a
# passing glitch and a permanent fault, and every new class of fault inherited
# the wrong advice by default.


def test_two_failures_from_different_causes_get_different_advice() -> None:
    """**Koko korjauksen pointti.**

    The same output, two failed units, two different causes -- and two
    different pieces of advice. A shared heading could be right for at most
    one of them, and that is exactly what made a 403 and a 400 into "run the
    command again" cases.
    """
    todo = fetch_stage.FetchPlan("t", pending=("a-0", "b-0"))
    results = (
        fetch_result(
            "a-0",
            "download_failed",
            reason="The connection broke during the download.",
            next_step="Run the command again.",
        ),
        fetch_result(
            "b-0",
            "download_failed",
            reason="FACEIT did not accept the download link request (HTTP 400).",
            next_step="Check FACEIT_DOWNLOADS_TOKEN in your machine's .env file.",
        ),
    )

    text = _render_fetch(results, todo)

    assert "-> Run the command again." in text
    assert "-> Check FACEIT_DOWNLOADS_TOKEN" in text
    # And they are on different rows under different units, not in a shared
    # heading.
    rows = text.splitlines()
    a_index = next(i for i, r in enumerate(rows) if r.strip() == "a-0")
    b_index = next(i for i, r in enumerate(rows) if r.strip() == "b-0")
    assert "Run the command again." in rows[a_index + 2]
    assert "FACEIT_DOWNLOADS_TOKEN" in rows[b_index + 2]


def test_the_failure_heading_states_what_happened_and_nothing_more() -> None:
    """The heading must not advise, because it does not know which fault it is."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result("a-0", "download_failed", reason="x", next_step="y"),
    )

    text = _render_fetch(results, todo)

    heading = next(r for r in text.splitlines() if r.startswith("Failed"))
    assert heading == "Failed (1):"


def test_a_missing_demo_also_carries_its_own_advice() -> None:
    """The same rule in both buckets, not in one of them only."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result(
            "a-0",
            "no_demo",
            reason="FACEIT has removed the recording.",
            next_step="Import the demo by hand, if you still have it.",
        ),
    )

    text = _render_fetch(results, todo)

    assert "Not available (1):" in text
    assert "-> Import the demo by hand, if you still have it." in text


def test_a_multi_line_reason_stays_readable_under_its_unit() -> None:
    """The messages are multi-line; the indent has to carry every row."""
    todo = fetch_stage.FetchPlan("t", pending=("a-0",))
    results = (
        fetch_result(
            "a-0",
            "download_failed",
            reason="First row.\nSecond row.\nThird row.",
            next_step="Do something.",
        ),
    )

    text = _render_fetch(results, todo)

    for row in ("First row.", "Second row.", "Third row."):
        assert f"    {row}" in text
