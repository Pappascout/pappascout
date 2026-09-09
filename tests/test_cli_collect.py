"""``pappascout collect`` -- the command's tests (Story 3.5).

Collecting is ``fetch``'s sister: **the same download, a different choice of
units**. So this file does not repeat the download's rules (they are in
``test_stage_fetch.py`` and ``test_cli_fetch.py``) but locks down what is
different in this command:

* **The units come from the match index, not from a selection file.** The
  whole division, not one team's sample -- that difference is the command's
  whole reason to exist.
* **A played match does not disappear quietly.** An empty ``map_picks`` is a
  block of its own with its reason, and it is not counted as unplayed.
* **The index's age and an unknown match length are said out loud.** Both
  bound what the run sees, and neither may be left to be inferred.
* **The numbers on the screen can be computed from the disk.** Story 3.4's
  lesson: a number nothing guards can be anything and nothing notices.

The ``discover`` -> ``collect`` chain is run with the real stages behind fake
ports, and **both demo directory modes** go through every test.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import LOCAL_DEMOS_DIRNAME
from test_stage_discover import CHAMPIONSHIP, FakeSource, division_matches
from test_stage_fetch import DEMO_BYTES, FakeDemo, FakeDemoSource
from typer.testing import CliRunner

from pappascout.adapters.protocols import Match
from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    MAX_LISTED_UNITS,
    NO_VETO_STALE_DAYS,
    _collect_no_veto,
    _render_collect_plan,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings
from pappascout.stages import archive_paths
from pappascout.stages import discover as discover_stage
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import select as select_stage

runner = CliRunner()

#: Our own team in the settings file; used only in the comparison with
#: ``select``.
SUBJECT = "Potku"


#: A played match whose ``status`` **is not** the string ``"FINISHED"``.
#:
#: ``discover._is_played`` normalises the case (``status.upper() in
#: PLAYED_STATUSES``), so a lower-case status is a played match -- but a naive
#: ``status == "FINISHED"`` comparison would say otherwise. Without this line
#: the data would hold no match at all in which ``played`` and that comparison
#: differ, and ``test_the_filter_is_played_not_the_status_string`` would guard
#: a difference the command tests never run.
LOWERCASE_STATUS = "finished"


def with_a_lowercase_played_status(
    matches: tuple[Match, ...],
) -> tuple[Match, ...]:
    """Turn the **last** played match's status into lower case.

    The last and not the first: in many tests the first match is the one that
    is modified separately, and two changes should not land on the same row.
    """
    rows = list(matches)
    for index in reversed(range(len(rows))):
        if rows[index].status == "FINISHED":
            rows[index] = rows[index].__class__(
                **{**rows[index].__dict__, "status": LOWERCASE_STATUS}
            )
            return tuple(rows)
    raise AssertionError("the data holds no played matches")


def division_units(archive) -> tuple[str, ...]:
    """The expected ids **from the match index's ``played`` field**.

    Two things this function must not do:

    **It does not ask ``plan_division``.** The test would then compare the
    function against itself and would pass even when it drops half of the
    matches.

    **And it does not filter on ``status == "FINISHED"``.** That is a
    different rule from the production code's ``played``, and
    ``test_stage_fetch.py::test_the_filter_is_played_not_the_status_string``
    is written precisely to prove they are not the same thing. An expectation
    built with a rule another test forbids is right only as long as the data
    holds no matches in which they differ -- and :data:`LOWERCASE_STATUS` is
    exactly such a match.

    The source is therefore the index's own ``played``, as ``discover`` wrote
    it: it is independent of the function under test.
    """
    document = json.loads(archive.matches_index().read_text(encoding="utf-8"))
    return tuple(
        f"{row['match_id']}-{index}"
        for row in document["matches"]
        if row["played"]
        for index in range(len(row["map_picks"]))
    )


class Division:
    """An archive a real ``discover`` has run over, and a fake demo port."""

    def __init__(self, archive, settings, source: FakeDemoSource) -> None:
        self.archive = archive
        self.settings = settings
        self.source = source
        self.matches: tuple[Match, ...] = ()
        self.units: tuple[str, ...] = ()

    def discover(
        self,
        matches: tuple[Match, ...] | None = None,
        *,
        lowercase: bool = True,
    ) -> tuple[str, ...]:
        """Write the match index from these matches and wire up their demos.

        A match in which ``played`` and ``status == "FINISHED"`` differ is
        always added to the default data
        (:func:`with_a_lowercase_played_status`) -- otherwise two different
        filters would coincide in every command test.
        """
        base = division_matches() if matches is None else matches
        self.matches = with_a_lowercase_played_status(base) if lowercase else base
        discover_stage.run(
            self.settings.league,
            self.archive,
            None,
            source=FakeSource({CHAMPIONSHIP: self.matches}),
            thresholds=self.settings.thresholds,
        )
        self.units = division_units(self.archive)
        self.source.demos = {unit: FakeDemo(DEMO_BYTES) for unit in self.units}
        return self.units

    def played_ids(self) -> list[str]:
        """The matches the index says were played -- ``played``, not ``status``."""
        document = json.loads(
            self.archive.matches_index().read_text(encoding="utf-8")
        )
        return [r["match_id"] for r in document["matches"] if r["played"]]

    def unplayed_ids(self) -> list[str]:
        document = json.loads(
            self.archive.matches_index().read_text(encoding="utf-8")
        )
        return [r["match_id"] for r in document["matches"] if not r["played"]]

    def on_disk(self) -> list[str]:
        """The demos that **really** are on the disk, as ids."""
        return sorted(
            path.name[: -len(".dem.zst")]
            for directory in self.archive.demo_dirs()
            if directory.is_dir()
            for path in directory.glob("*.dem.zst")
        )


@pytest.fixture(params=["arkisto", "paikallinen"])
def division(request, settings_file: Path, tmp_path: Path, monkeypatch) -> Division:
    """The real stages, fake ports, both demo directory modes.

    The same reasoning as ``test_cli_fetch.py``'s ``pipeline`` fixture: a mode
    the command tests do not run goes through the CLI zero times.
    """
    if request.param == "paikallinen":
        text = settings_file.read_text(encoding="utf-8")
        line = next(r for r in text.splitlines() if r.startswith("# demos_root = "))
        settings_file.write_text(
            text.replace(line, f"demos_root = '{tmp_path / 'paikalliset'}'", 1),
            encoding="utf-8",
        )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    settings = load_settings()
    archive = archive_paths(settings.project)
    source = FakeDemoSource({})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: source
    )
    # The disk must not be a variable of the test: the check is its own test.
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    world = Division(archive, settings, source)
    world.discover()
    return world


def played_matches(**changes) -> tuple[Match, ...]:
    """The division's matches with the **played** ones changed by these fields.

    This builds the data and is not an expectation, so it may look at the
    status -- but it looks at **both** spellings, so that a lower-case match
    is not left unchanged and does not accidentally produce a different
    outcome from the others.
    """
    return tuple(
        (
            match.__class__(**{**match.__dict__, **changes})
            if (match.status or "").upper() == "FINISHED"
            else match
        )
        for match in division_matches()
    )


# -- The units come from the match index -------------------------------------


def test_the_whole_division_is_planned_and_downloaded(division) -> None:
    result = runner.invoke(app, ["collect"], input="y\n")

    assert result.exit_code == 0, result.output
    # **The whole figure, not a substring.** A single-digit number would hit
    # an id and the run time, and the claim would pass with a wrong number on
    # the screen too.
    assert f"{len(division.units)} to download" in result.output
    assert "Download these demos?" in result.output
    assert division.source.asked == list(division.units)
    for unit in division.units:
        assert division.archive.find_demo(unit) is not None


def test_collect_reaches_matches_that_no_selection_file_contains(
    division,
) -> None:
    """**The command's whole reason.** ``fetch`` the sample, ``collect`` the
    division.

    If collecting stayed per team it would be ``fetch --team`` under another
    name -- and at the end of the season exactly those matches nobody has
    selected would vanish into FACEIT's 30-day retention.
    """
    select_stage.run(
        division.settings.league,
        division.archive,
        SUBJECT,
        thresholds=division.settings.thresholds,
    )
    document = json.loads(
        next(division.archive.resolve("index/selections").glob("*.json")).read_text(
            encoding="utf-8"
        )
    )
    otanta = {r["map_demo_id"] for r in document["selections"] if r["roster_ok"]}
    assert otanta, "the data produced no selected map at all"

    runner.invoke(app, ["collect", "--kylla"])

    haetut = set(division.source.asked)
    assert otanta < haetut, "the collection has to be wider than the sample"
    assert haetut == set(division.units)


def test_an_unplayed_match_never_reaches_the_port(division) -> None:
    """An unplayed match has no demo: asking would spend quota."""
    runner.invoke(app, ["collect", "--kylla"])

    unplayed = division.unplayed_ids()
    assert unplayed, "the data holds no unplayed matches"
    for match_id in unplayed:
        assert not any(unit.startswith(match_id) for unit in division.source.asked)


def test_own_team_is_not_filtered_out_of_the_division(division) -> None:
    """The division means the division -- our own matches are collected too."""
    own = division.settings.project.own_team_name
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    pelatut = set(division.played_ids())
    omat = [
        m.match_id
        for m in division.matches
        if m.match_id in pelatut and any(s.name == own for s in m.teams)
    ]
    assert omat, "the data holds no played matches of our own team"
    for match_id in omat:
        assert f"{match_id}-0" in division.source.asked


# -- The confirmation question ------------------------------------------------


def test_answering_no_downloads_nothing(division) -> None:
    result = runner.invoke(app, ["collect"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Cancelled. No demos were downloaded." in result.output
    assert division.source.asked == []
    assert division.on_disk() == []


def test_no_input_at_all_is_cancelled_not_aborted(division) -> None:
    """The name used to say the cancellation is in Finnish; AD-11 made it false."""
    result = runner.invoke(app, ["collect"], input="")

    assert result.exit_code == 0, result.output
    assert "Aborted" not in result.output
    assert "[y/N]" not in result.output
    assert "Cancelled. No demos were downloaded." in result.output
    assert division.source.asked == []


def test_kylla_skips_the_question_but_not_the_plan(division) -> None:
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert "Download these" not in result.output
    assert f"{len(division.units)} to download" in result.output
    assert division.source.asked == list(division.units)


def test_the_target_directory_is_shown_before_the_question(division) -> None:
    result = runner.invoke(app, ["collect"], input="n\n")

    kohde = [r for r in result.output.splitlines() if r.strip().startswith("Target")]
    assert kohde, f"the plan has no Target row:\n{result.output}"
    assert str(division.archive.demos_dir()) in kohde[0]
    assert result.output.index("Target") < result.output.index("Download these")


def test_the_shown_target_is_the_directory_that_is_written_to(division) -> None:
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    written = division.archive.find_demo(division.units[0])
    assert written is not None
    kohde = [r for r in result.output.splitlines() if r.strip().startswith("Target")]
    assert kohde
    assert all(str(written.parent) in row for row in kohde)


# -- Safe to run at any time -------------------------------------------------


def test_a_second_run_downloads_nothing_and_says_so(division) -> None:
    first = runner.invoke(app, ["collect", "--kylla"])
    assert first.exit_code == 0, first.output
    division.source.asked.clear()

    second = runner.invoke(app, ["collect", "--kylla"])

    assert second.exit_code == 0, second.output
    assert division.source.asked == []
    assert "nothing to download" in second.output
    # And the question is not asked when there is nothing to download.
    assert "Download these" not in second.output


def test_a_second_run_still_counts_the_demos_that_are_on_disk(division) -> None:
    """"Nothing to download" must not look as if the sample had shrunk."""
    runner.invoke(app, ["collect", "--kylla"])

    second = runner.invoke(app, ["collect", "--kylla"])

    assert f"of which {len(division.units)} already on disk" in second.output


# -- A played match with no veto data ----------------------------------------


def test_a_played_match_without_map_picks_is_shown_with_its_reason(
    division,
) -> None:
    """**Not zero rows.** The fault Story 3.3's review found, turned round."""
    matches = played_matches(map_picks=())
    division.discover(matches)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    pelatut = division.played_ids()
    assert f"Played match with no veto data ({len(pelatut)})" in result.output
    for match_id in pelatut:
        assert match_id in result.output
    # And not one port call: the ids cannot be formed.
    assert division.source.asked == []
    assert "nothing to download" in result.output


def test_a_match_without_map_picks_is_not_called_unplayed(division) -> None:
    """A wrong reason is worse than none: it is a claim that is not true."""
    division.discover(played_matches(map_picks=()))

    result = runner.invoke(app, ["collect", "--kylla"])

    lohko = result.output.split("Played match with no veto data", 1)[1]
    assert "not played" not in lohko.lower()
    assert "map list" in lohko
    # The data's matches are weeks old, so the advice is an import and not a
    # rerun of discover -- see test_the_advice_branches_on_the_age.
    assert "uv run pappascout import" in lohko


def test_the_no_veto_block_is_shown_even_when_there_is_something_to_download(
    division,
) -> None:
    """The block must not vanish behind there being something else to download."""
    matches = list(division_matches())
    rikki = matches[0]
    matches[0] = rikki.__class__(**{**rikki.__dict__, "map_picks": ()})
    division.discover(tuple(matches))

    result = runner.invoke(app, ["collect", "--kylla"])

    assert "Played match with no veto data (1)" in result.output
    assert rikki.match_id in result.output
    assert division.source.asked, "the other matches were left unfetched"


# -- The index's age and an unknown match length -----------------------------


def test_the_plan_says_how_old_the_match_index_is(division) -> None:
    """The index bounds the whole set of units, so its age belongs to the question."""
    document = json.loads(
        division.archive.matches_index().read_text(encoding="utf-8")
    )

    result = runner.invoke(app, ["collect"], input="n\n")

    assert "Match index" in result.output
    assert document["generated_at"] in result.output


def test_a_missing_best_of_makes_the_output_say_the_length_is_unknown(
    division,
) -> None:
    """Measured 2026-09-06: the field was missing from the archive's whole index.

    The maps are then read from the veto data -- and every one of them is
    attempted.
    """
    matches = played_matches(best_of=None)
    division.discover(matches)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert "unknown in" in result.output
    assert "best_of" in result.output
    # And the maps were read from the veto data even so: every one attempted.
    assert division.source.asked == list(division.units)


def test_a_known_best_of_does_not_claim_the_length_is_unknown(division) -> None:
    """The row is a claim, not a decoration: not when the length is known."""
    result = runner.invoke(app, ["collect"], input="n\n")

    assert "Match length" not in result.output


def test_a_bo3_that_ended_two_nil_still_tries_the_third_map(division) -> None:
    """The third map is an **expected** ``no_demo``, not a reason not to try.

    Skipping it would cost a demo that in a month is nowhere to be had; an
    attempt costs one call. And a repeated ``no_demo`` does not end the series
    (``IDENTICAL_FAILURE_LIMIT`` looks only at ``download_failed``), so the
    rest of the division downloads.
    """
    matches = played_matches(
        best_of=3, map_picks=("de_ancient", "de_nuke", "de_mirage")
    )
    units = division.discover(matches)
    kolmannet = [u for u in units if u.endswith("-2")]
    assert len(kolmannet) >= 3, "the data proves nothing with one match"
    for unit in kolmannet:
        del division.source.demos[unit]

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert division.source.asked == list(units)
    assert f"{len(kolmannet)} not available" in result.output
    assert f"{len(units) - len(kolmannet)} fetched" in result.output


def test_the_same_fault_three_times_stops_the_series_and_every_unit_gets_a_row(
    division,
) -> None:
    """A repeated fault stops the attempts -- but does not quietly shorten the
    listing.

    The plan promised a certain number of units, and a shorter summary would
    leave the user guessing where the rest went.
    """
    from pappascout.errors import ApiError

    units = division.units
    division.source.demos = {
        unit: ApiError(
            "FACEIT did not accept the download link request.",
            status_code=400,
            advice="Check FACEIT_DOWNLOADS_TOKEN.",
        )
        for unit in units
    }

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    raja = fetch_stage.IDENTICAL_FAILURE_LIMIT
    assert division.source.asked == list(units[:raja])
    assert f"{len(units)} failed" in result.output
    for unit in units:
        assert unit in result.output
    assert "Not attempted" in result.output


def test_the_summary_separates_all_four_outcomes(division) -> None:
    """**The acceptance criterion.** Four numbers, four different next steps.

    Fetched, already on disk, not available and failed are four different
    things, and none may leak into another.
    """
    from pappascout.errors import ApiError, DemoUnavailable

    units = division.units
    # One is already on disk before the run: the first run fetches it.
    runner.invoke(app, ["collect", "--kylla"])
    for unit in units[1:]:
        demo = division.archive.find_demo(unit)
        assert demo is not None
        demo.unlink()
        (demo.parent / f"{unit}.meta.json").unlink()
    division.source.asked.clear()
    division.source.demos[units[1]] = DemoUnavailable(
        "FACEIT removed the recording."
    )
    division.source.demos[units[2]] = ApiError(
        "The connection broke.", status_code=503, advice="Run the command again."
    )

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert f"Download done: {len(units) - 3} fetched" in result.output
    assert "1 already on disk" in result.output
    assert "1 not available" in result.output
    assert "1 failed" in result.output
    assert "FACEIT removed the recording." in result.output
    assert "-> Run the command again." in result.output


# -- The no-veto row's timestamp and advice (review 2026-09-06) --------------


def no_veto_row(match_id: str = "1-aaa", **changes):
    """One row without veto data, for testing the output."""
    from pappascout.stages import fetch as stage

    fields = {
        "match_id": match_id,
        "reason": "The match has been played, but there is no map list.",
        "finished_at": "2026-09-05T20:14:00+00:00",
    }
    fields.update(changes)
    return stage.NoVetoMatch(**fields)


def test_the_row_carries_the_time_the_match_ended() -> None:
    """The timestamp on the screen is a claim, and a claim is to be guarded.

    Without this, ``_collect_no_veto``'s timestamp part can be removed
    entirely without a single test going red -- proved in the review of
    2026-09-06.
    """
    text = "\n".join(_collect_no_veto((no_veto_row(),)))

    assert "1-aaa" in text
    assert "ended 2026-09-05T20:14:00+00:00" in text


def test_the_row_scales_with_the_timestamp_it_is_given() -> None:
    """A fixed text would pass every check of mere existence."""
    eka = "\n".join(_collect_no_veto((no_veto_row(),)))
    toka = "\n".join(
        _collect_no_veto((no_veto_row(finished_at="2026-08-01T10:00:00+00:00"),))
    )

    assert "2026-09-05T20:14:00+00:00" in eka
    assert "2026-08-01T10:00:00+00:00" in toka
    assert eka != toka


def test_a_row_without_a_timestamp_does_not_print_the_word_none() -> None:
    """A missing timestamp is left out, not printed as ``None``.

    "(ended None)" would be a programming error on the screen, and the user
    would have to guess whether it means missing information or something
    else.
    """
    text = "\n".join(_collect_no_veto((no_veto_row(finished_at=None),)))

    assert "1-aaa" in text
    assert "None" not in text
    assert "ended" not in text


def test_the_advice_branches_on_the_age_of_the_match() -> None:
    """**``finished_at``'s documented purpose is in use here.**

    On a fresh match the veto is missing because the index is older than the
    match -- ``discover`` fixes that. On a match weeks old, ``discover`` has
    already been run since the match and there is still no veto, so running
    again produces nothing. A shared piece of advice would be right for at
    most one of them.
    """
    nyt = datetime.now(UTC)
    tuore = "\n".join(
        _collect_no_veto((no_veto_row(finished_at=nyt.isoformat()),))
    )
    vanha = "\n".join(
        _collect_no_veto(
            (
                no_veto_row(
                    finished_at=(
                        nyt - timedelta(days=NO_VETO_STALE_DAYS + 1)
                    ).isoformat()
                ),
            )
        )
    )

    assert "uv run pappascout discover" in tuore
    assert "import" not in tuore
    assert "uv run pappascout import" in vanha
    assert "discover probably will not bring it" in vanha


def test_an_unknown_age_gets_the_cheap_advice_not_the_expensive_one() -> None:
    """An unknown age is not "old": the advice is not chosen on absent data."""
    text = "\n".join(_collect_no_veto((no_veto_row(finished_at=None),)))

    assert "uv run pappascout discover" in text
    assert "import" not in text


def test_an_unparseable_timestamp_does_not_crash_the_plan() -> None:
    """A broken timestamp is an unknown age, not an exception."""
    text = "\n".join(_collect_no_veto((no_veto_row(finished_at="yesterday"),)))

    assert "uv run pappascout discover" in text


# -- An empty division does not lie (review 2026-09-06) ----------------------


def test_an_empty_division_does_not_claim_everything_is_on_disk(
    division,
) -> None:
    """**A wrong claim about the data.** Nothing is on disk, nothing is known.

    The situation comes from a wrong or foreign ``championship_ids``, from
    another division's index, and from a season that has not been played yet.
    """
    pelaamattomat = tuple(
        match.__class__(
            **{**match.__dict__, "status": "SCHEDULED", "map_picks": ()}
        )
        for match in division_matches()
    )
    division.discover(pelaamattomat, lowercase=False)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert "nothing to download" in result.output
    assert "already on disk -- nothing to download" not in result.output
    assert "no played match from this division" in result.output
    # **Item 3:** the id that was filtered with is what the user has to
    # compare against the index -- and they cannot do that without seeing it.
    assert CHAMPIONSHIP in result.output
    assert "championship_ids" in result.output
    assert "uv run pappascout discover" in result.output
    assert division.source.asked == []


def test_the_empty_message_names_the_ids_it_filtered_with() -> None:
    """``league_ids`` is a field that is read, not filled. Two different
    settings, two different messages -- otherwise the field is decoration."""
    yksi = _render_collect_plan(
        fetch_stage.CollectPlan(league_ids=("aaa-111",)), None, "kohde"
    )
    kaksi = _render_collect_plan(
        fetch_stage.CollectPlan(league_ids=("aaa-111", "bbb-222")), None, "kohde"
    )

    assert "aaa-111" in yksi
    assert "bbb-222" not in yksi
    assert "aaa-111, bbb-222" in kaksi


def test_all_on_disk_and_nothing_known_are_different_messages() -> None:
    """Two empty plans, two different situations, two different messages."""
    levylla = _render_collect_plan(
        fetch_stage.CollectPlan(
            league_ids=("aaa-111",), present=("a-0", "a-1"), matches_played=1
        ),
        None,
        "kohde",
    )
    tyhja = _render_collect_plan(
        fetch_stage.CollectPlan(league_ids=("aaa-111",)), None, "kohde"
    )

    assert "Every demo in the division is already on disk" in levylla
    assert "no played match from this division" not in levylla
    assert "Every demo in the division is already on disk" not in tyhja
    assert "no played match from this division" in tyhja


# -- The disk warning: one fits, not all (review 2026-09-06) -----------------


def test_a_plan_that_does_not_fit_warns_but_does_not_stop(
    division, monkeypatch
) -> None:
    """A partial collection is better than no collection.

    FACEIT removes a demo in about 30 days: the part that is fetched in time
    is kept for good. The stage checks the space separately for every demo,
    so the run stops of its own accord at the right point.
    """
    yksi_mahtuu = (
        fetch_stage.DEMO_SIZE_ESTIMATE_BYTES + fetch_stage.DISK_RESERVE_BYTES
    )
    todo = fetch_stage.plan_division(division.archive, division.settings.league)
    assert todo.estimated_bytes > yksi_mahtuu, "the data proves nothing"
    vapaana = yksi_mahtuu + fetch_stage.DEMO_SIZE_ESTIMATE_BYTES
    assert vapaana < todo.estimated_bytes
    monkeypatch.setattr(fetch_stage, "free_space", lambda _a: vapaana)

    result = runner.invoke(app, ["collect"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "NOTE" in result.output
    assert "does not fit on the disk" in result.output
    # A warning, not a gate: the question is asked even so.
    assert "Download these" in result.output
    # The CLI's own hard error and the stage's are different messages, and
    # neither of them appears here.
    assert "no room for a single demo" not in result.output
    assert "Not enough disk space" not in result.output


def test_a_plan_that_fits_gets_no_warning(division) -> None:
    """The warning is a claim, not a decoration: not when everything fits."""
    result = runner.invoke(app, ["collect"], input="n\n")

    assert "NOTE" not in result.output
    assert "does not fit on the disk" not in result.output


def test_the_warning_says_how_many_of_the_plan_would_fit() -> None:
    """The number is what the user decides on -- so it is not fixed."""
    pending = tuple(f"a-{i}" for i in range(100))
    todo = fetch_stage.CollectPlan(
        pending=pending,
        matches_played=50,
        estimated_bytes=100 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES,
    )
    vapaana = fetch_stage.DISK_RESERVE_BYTES + 3 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES

    text = _render_collect_plan(todo, vapaana, "kohde")

    assert "an estimated 3 of the 100 demos" in text


def test_the_warning_never_promises_more_than_the_plan_holds() -> None:
    """Three demos in the plan and room for four: the number must not be four."""
    todo = fetch_stage.CollectPlan(
        pending=("a-0", "a-1", "a-2"),
        matches_played=2,
        # The estimate is larger than the free space, so the warning fires...
        estimated_bytes=fetch_stage.DISK_RESERVE_BYTES * 100,
    )
    vapaana = fetch_stage.DISK_RESERVE_BYTES + 9 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES

    text = _render_collect_plan(todo, vapaana, "kohde")

    assert "an estimated 3 of the 3 demos" in text


# -- A long listing is truncated (review 2026-09-06) -------------------------


def test_a_long_unit_list_is_truncated_and_says_how_many_are_hidden() -> None:
    """For the whole division the listing is over a hundred rows.

    What would scroll off the screen are exactly the rows the plan is printed
    for: the target, the free space, the matches with no veto data and the
    question itself.
    """
    pending = tuple(f"a-{i}" for i in range(132))
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=pending, matches_played=66, estimated_bytes=1024**3
        ),
        None,
        "kohde",
    )

    rivit = [r for r in text.splitlines() if r.startswith("  a-")]
    assert len(rivit) == MAX_LISTED_UNITS
    assert f"(+{132 - MAX_LISTED_UNITS} more" in text
    # And the Demos row still gives the whole number: the cut is on the
    # listing.
    assert "132 to download" in text


def test_a_short_list_is_not_truncated_and_says_nothing_about_hiding() -> None:
    """The truncation message is a claim, and must not show when nothing was cut."""
    pending = tuple(f"a-{i}" for i in range(MAX_LISTED_UNITS))
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=pending, matches_played=10, estimated_bytes=1024**3
        ),
        None,
        "kohde",
    )

    for unit in pending:
        assert f"  {unit}" in text
    assert "more" not in text


def test_the_whole_division_still_fits_on_a_screen(division) -> None:
    """The current division (12 demos) is still listed in full."""
    result = runner.invoke(app, ["collect"], input="n\n")

    assert len(division.units) <= MAX_LISTED_UNITS
    for unit in division.units:
        assert unit in result.output
    assert "more --" not in result.output


# -- Singular and plural inflect (review 2026-09-06) -------------------------


def test_one_match_and_one_map_are_singular() -> None:
    """"1 matches played, 1 maps" is wrong.

    A one-match division is not a rarity: the season's first run lands on
    exactly that, so the mistake would show just as the tool is introduced.
    The name used to say the forms are Finnish; the rule survived the
    translation, the language did not.
    """
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, estimated_bytes=1024**2
        ),
        None,
        "kohde",
    )

    assert "Division: 1 match played, 1 map, of which 0 already " in text
    assert "matches played" not in text
    assert "maps" not in text
    # In Finnish the relative pronoun took the same number as the noun and
    # the assertion was ``"joista" not in text``. English has one form, so
    # all that is left to claim is that the sister helper is in the sentence.
    assert "of which" in text


def test_two_or_more_take_the_partitive() -> None:
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0", "a-1"), matches_played=2, estimated_bytes=1024**2
        ),
        None,
        "kohde",
    )

    assert "Division: 2 matches played, 2 maps, of which 0 already " in text


def test_zero_takes_the_partitive_too() -> None:
    """Zero takes the plural: "0 matches played"."""
    text = _render_collect_plan(fetch_stage.CollectPlan(), None, "kohde")

    assert "0 matches played, 0 maps, of which 0 already " in text


# -- The unknown length states its scope (review 2026-09-06) -----------------


def test_the_unknown_length_line_says_how_many_matches_it_means(
    division,
) -> None:
    """An observation without its scope cannot be checked."""
    kaikki = played_matches(best_of=None)
    division.discover(kaikki)
    pelatut = len(division.played_ids())

    result = runner.invoke(app, ["collect"], input="n\n")

    assert f"unknown in {pelatut} of the matches" in result.output


def test_the_unknown_length_count_scales_with_the_plan() -> None:
    """A fixed number would pass every check of mere existence."""
    yksi = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, best_of_unknown=("a",)
        ),
        None,
        "kohde",
    )
    monta = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=9, best_of_unknown=tuple("abcdefghi")
        ),
        None,
        "kohde",
    )

    assert "unknown in 1 of the matches" in yksi
    assert "unknown in 9 of the matches" in monta


def test_the_unknown_length_line_says_what_to_do_about_it() -> None:
    """Story 3.7 (item 11): an observation without advice leaves one guessing.

    The field is not missing from the source but from **the old index**:
    ``discover`` writes it (``discover._match_row``), so running again fixes
    the row. Without this sentence the row looks like a fault nothing can be
    done about.
    """
    text = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, best_of_unknown=("a",)
        ),
        None,
        "kohde",
    )

    # The ``replace`` chain is left over from when the needle was Finnish
    # folded to ASCII. It is a no-op now and is kept so that the diff reads
    # as a translation.
    assert "missing from the old index" in text.replace("ä", "a").replace("ö", "o")
    assert "uv run pappascout discover" in text


def test_no_advice_line_appears_when_every_length_is_known() -> None:
    """The advice belongs to the observation: without it, it would be noise."""
    text = _render_collect_plan(
        fetch_stage.CollectPlan(pending=("a-0",), matches_played=1),
        None,
        "kohde",
    )

    assert "Match length" not in text
    assert "uv run pappascout discover" not in text


# -- The gates: the index, the disk space, the Downloads permission ----------


def test_without_a_match_index_the_error_says_to_run_discover(
    settings_file, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    text = "".join(capsys.readouterr())
    assert "discover" in text


def test_a_full_disk_stops_the_command_before_the_question(
    division, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 1024)
    monkeypatch.setattr("sys.argv", ["pappascout", "collect"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    assert "no room for a single demo" in captured.err + captured.out
    assert "Download these" not in captured.out
    assert division.source.asked == []


@pytest.fixture
def denied(division, monkeypatch):
    """The real adapter, which gets a 403 on the signing call.

    A fake port cannot produce the Downloads API's 403, and that seam is
    exactly the one Story 3.4's whole test suite missed.
    """
    from test_faceit_demos import DOWNLOADS, FakeResponse, FakeSession, build

    session = FakeSession(sign=FakeResponse(403), download=FakeResponse(200))
    real_source = build(division.archive.root.parent, session)
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: real_source
    )
    assert real_source.downloads_base_url == DOWNLOADS
    return division, session


def test_the_plan_is_shown_in_full_before_the_first_403(
    denied, monkeypatch, capsys
) -> None:
    """The plan is an output of its own: it shows even if no download starts."""
    world, _session = denied
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    text = "".join(capsys.readouterr())
    assert f"{len(world.units)} to download" in text
    for unit in world.units:
        assert unit in text


def test_only_one_signing_call_is_made_before_the_run_stops(
    denied, monkeypatch, capsys
) -> None:
    """With twelve demos this would have been 12 doomed calls."""
    world, session = denied
    assert len(world.units) > 1
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit):
        main()

    capsys.readouterr()
    assert len(session.posts) == 1


def test_the_denied_message_names_the_status_page(
    denied, monkeypatch, capsys
) -> None:
    monkeypatch.setattr("sys.argv", ["pappascout", "collect", "--kylla"])

    with pytest.raises(SystemExit):
        main()

    text = "".join(capsys.readouterr())
    assert "The Downloads API is a separate authorisation" in text
    assert "downloads-api-application" in text
    # The Finnish needle guarded the CLI's own heading until T15 translated
    # it; it is kept so that Finnish advice cannot come back unnoticed.
    assert "aja komento uudelleen" not in text.lower()
    assert "run the command again" not in text.lower()


def test_the_production_port_is_wired_on_the_collect_path(
    division, monkeypatch
) -> None:
    """**Mutation proof 4.** The wiring to ``default_source`` has to be measured.

    Without this, ``collect`` could build the port past the settings, and the
    difference would show only on the first real run.
    """
    seen: list[tuple] = []
    port = division.source

    def spy(settings, archive):
        seen.append((settings, archive))
        return port

    monkeypatch.setattr("pappascout.stages.fetch.default_source", spy)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    assert len(seen) == 1
    settings, archive = seen[0]
    assert settings.league.championship_ids == [CHAMPIONSHIP]
    assert archive.demos_dir() == division.archive.demos_dir()


# -- The numbers on the screen can be computed from the disk -----------------


def test_the_summary_counts_match_the_files_that_are_on_disk(division) -> None:
    """Story 3.4's lesson: a hard-coded number would pass every claim."""
    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    levylla = division.on_disk()
    assert levylla == sorted(division.units)
    assert f"Download done: {len(levylla)} fetched" in result.output
    kirjoitettu = fetch_stage.size_fi(len(levylla) * len(DEMO_BYTES))
    assert kirjoitettu in result.output


def test_the_plan_line_says_the_real_count_and_the_real_size(division) -> None:
    """The plan's numbers come from the plan and from nowhere else."""
    todo = fetch_stage.plan_division(division.archive, division.settings.league)

    text = _render_collect_plan(todo, 9_900_000_000, r"D:\demot")

    assert f"{len(division.units)} to download" in text
    odotettu = fetch_stage.size_fi(
        len(division.units) * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES
    )
    assert odotettu in text
    assert r"D:\demot" in text
    assert "9,2 Gt" in text
    for unit in division.units:
        assert unit in text


def test_the_plan_line_scales_with_the_plan() -> None:
    """The same function, a different plan, different numbers."""
    small = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=("a-0",), matches_played=1, estimated_bytes=1024**2
        ),
        None,
        "kohde",
    )
    large = _render_collect_plan(
        fetch_stage.CollectPlan(
            pending=tuple(f"a-{i}" for i in range(1001)),
            matches_played=501,
            estimated_bytes=1024**4,
        ),
        None,
        "kohde",
    )

    assert "1 to download, an estimated 1,0 Mt" in small
    assert "1001 to download, an estimated 1,0 Tt" in large
    # The free space is not known: the row is not invented.
    assert "Free disk space" not in small


def test_the_plan_says_the_index_age_it_was_given() -> None:
    """The age comes from the plan: a fixed text would pass on every run."""
    vanha = _render_collect_plan(
        fetch_stage.CollectPlan(index_generated_at="2026-09-04T18:20:11+00:00"),
        None,
        "kohde",
    )
    tuore = _render_collect_plan(
        fetch_stage.CollectPlan(index_generated_at="2026-09-06T17:05:00+00:00"),
        None,
        "kohde",
    )

    assert "2026-09-04T18:20:11+00:00" in vanha
    assert "2026-09-06T17:05:00+00:00" in tuore
    assert vanha != tuore


def test_an_index_without_a_timestamp_does_not_invent_one() -> None:
    text = _render_collect_plan(fetch_stage.CollectPlan(), None, "kohde")

    assert "time unknown" in text


# -- The default mode and the local mode go through the command --------------


def test_the_local_demos_root_setting_moves_the_files_out_of_the_archive(
    settings_file_local_demos, tmp_path, monkeypatch
) -> None:
    local = tmp_path / LOCAL_DEMOS_DIRNAME
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file_local_demos))
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root == local

    source = FakeDemoSource({})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: source
    )
    world = Division(archive, settings, source)
    units = world.discover()

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (local / f"{unit}.dem.zst").read_bytes() == DEMO_BYTES
        assert (local / f"{unit}.meta.json").is_file()
    assert not archive.archive_demos_dir().exists()
    assert str(local) in result.output


def test_without_demos_root_the_demos_go_into_the_archive(
    settings_file, monkeypatch
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(fetch_stage, "free_space", lambda _archive: 100 * 1024**3)

    settings = load_settings()
    archive = archive_paths(settings.project)
    assert archive.demos_root is None

    source = FakeDemoSource({})
    monkeypatch.setattr(
        "pappascout.stages.fetch.default_source", lambda _s, _a: source
    )
    world = Division(archive, settings, source)
    units = world.discover()

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output
    for unit in units:
        assert (archive.root / "demos" / f"{unit}.dem.zst").is_file()
        assert (archive.root / "demos" / f"{unit}.meta.json").is_file()


# -- Collecting does not write to the index or run other stages --------------


def test_collect_does_not_touch_the_match_index(division) -> None:
    """``collect`` reads the index. Writing would be a second ``discover``."""
    path = division.archive.matches_index()
    before = path.read_bytes()

    runner.invoke(app, ["collect", "--kylla"])

    assert path.read_bytes() == before


def test_collect_does_not_run_discover(division, monkeypatch) -> None:
    """Refreshing the match index is a decision of its own, not a side effect."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("collect must not call discover")

    monkeypatch.setattr(discover_stage, "run", refuse)
    monkeypatch.setattr(discover_stage, "default_source", refuse)

    result = runner.invoke(app, ["collect", "--kylla"])

    assert result.exit_code == 0, result.output


def test_collect_writes_no_selection_file(division) -> None:
    """The roster threshold is ``select``'s business; collecting makes no selection."""
    runner.invoke(app, ["collect", "--kylla"])

    selections = division.archive.resolve("index/selections")
    assert not selections.exists() or not list(selections.glob("*.json"))
