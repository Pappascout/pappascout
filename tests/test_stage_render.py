"""``stages.render`` -- the stage's tests.

The stage reads one file and writes one file, so its whole logic -- choosing
the team, checking the input, the timestamped name, the atomic write and the
manifest -- is tested in a temporary archive without demos.

The report's **content** is tested in ``test_render`` and the command's output
in ``test_cli_report``; here only what the stage does to the files is tested.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from conftest import has_temp_leftovers
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
)
from pappascout.archive.paths import MAX_REPORTS_PER_MINUTE, ArchivePaths, report_name
from pappascout.domain.models import ReportSettings
from pappascout.domain.report import REPORT_SCHEMA_VERSION, Report
from pappascout.errors import PappascoutError
from pappascout.stages import render as render_stage
from test_render import (
    DEFAULT_PRUNING,
    DEMO_ID,
    TEAM_KEY,
    TEAM_SLUG,
    UNKNOWN_ROSTER_ROW,
    pistol_map,
    report,
    three_demo_report,
    with_roster,
)

OTHER_TEAM = "bbbbbbbbbbbbbbbb"
STAMP = datetime(2026, 8, 30, 3, 7)


# --- Building the archive -------------------------------------------------------


def build_archive(
    tmp_path: Path,
    *,
    teams: dict[str, Report] | None = None,
    write_manifest: bool = True,
) -> ArchivePaths:
    """An archive holding the given teams' ``report.json`` files."""
    archive = ArchivePaths(root=tmp_path / "arkisto")
    entries = teams if teams is not None else {TEAM_KEY: report([pistol_map()])}
    for team_key, entry in entries.items():
        path = archive.report_json(team_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(entry.model_dump_json(indent=2), encoding="utf-8")
        if write_manifest:
            Manifest.new(
                result_id=f"aggregates/{team_key}",
                stage="aggregate",
                params_hash="hash",
                inputs=[ManifestInput(result_id=f"classified/{team_key}", sha256="x")],
                outputs=(f"aggregates/{team_key}/report.json",),
            ).write(archive.report_manifest(team_key))
    return archive


def run(
    archive: ArchivePaths,
    team: str | None = TEAM_KEY,
    settings: ReportSettings = DEFAULT_PRUNING,
    **kwargs,
):
    """The stage run with the production pruning settings (Story 2.13).

    The default is ``settings.toml``'s default for the same reason as in
    ``test_render``: a fixture that turned the pruning off would test the
    stage in a state nobody runs.
    """
    return render_stage.run(settings, archive, team, **kwargs)


def reports(archive: ArchivePaths, team_key: str = TEAM_KEY) -> list[Path]:
    return sorted(archive.reports_dir(team_key).glob("*.md"))


# --- The basic run --------------------------------------------------------------


def test_the_roster_row_reaches_the_written_markdown(tmp_path: Path) -> None:
    """The stage writes the split into the file on disk, not just the model.

    The sibling league row is pinned from rendered text at stage level, and
    this is the same claim for the roster row: ``report.json`` -> template ->
    ``.md``. Without it, "the template needed no change" would rest on the
    view model alone -- and a template that dropped the row would still pass
    every other test.
    """
    split = with_roster(three_demo_report(), {"full": (1, 2), "partial": (2, 4)})
    archive = build_archive(tmp_path, teams={TEAM_KEY: split})
    result = run(archive)

    text = archive.resolve(result.outputs[0]).read_text(encoding="utf-8")
    assert (
        "- **Rosteriluokka:** 5/5: 1 demo / 2 kierrosta, "
        "4/5: 2 demoa / 4 kierrosta, tuntematon: 0 demoa / 0 kierrosta "
        "-- 4/5 on kartta, jolla yksi pelaaja oli vakirosterin "
        "ulkopuolelta, joten se on heikompi havainto joukkueen "
        "vakiasetelmasta"
    ) in text


def test_the_unknown_roster_row_reaches_the_written_markdown(
    tmp_path: Path,
) -> None:
    """The other branch, through the same path: the sentence, whole."""
    archive = build_archive(tmp_path, teams={TEAM_KEY: three_demo_report()})
    result = run(archive)

    text = archive.resolve(result.outputs[0]).read_text(encoding="utf-8")
    assert UNKNOWN_ROSTER_ROW in text


def test_run_writes_one_timestamped_markdown_file(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    result = run(archive)

    assert result.stage == "render"
    assert result.status == "ok"
    assert not result.skipped
    assert len(result.outputs) == 1

    written = archive.resolve(result.outputs[0])
    assert written.is_file()
    assert written.suffix == ".md"
    assert written.parent == archive.reports_dir(TEAM_KEY)
    assert written.read_text(encoding="utf-8").startswith("# MatureMayhem")


def test_the_file_on_disk_is_exactly_what_render_produced(tmp_path: Path) -> None:
    """The stage formats nothing at write time -- the Finnish letters included.

    The file is read back **as UTF-8 and as bytes**: the wrong encoding would
    produce a file that opens on Windows but looks wrong in Discord, and a
    substring claim would not notice that.
    """
    from pappascout.render import render_report

    archive = build_archive(tmp_path)
    result = run(archive)
    entry = render_stage.read_report(archive.report_json(TEAM_KEY), TEAM_KEY)
    expected = render_report(
        entry,
        settings=DEFAULT_PRUNING,
        round_list_paths=render_stage.round_list_paths(archive, entry),
    )

    written = archive.resolve(result.outputs[0])
    assert written.read_text(encoding="utf-8") == expected
    assert written.read_bytes() == expected.encode("utf-8")
    assert "ä" in expected


def test_the_file_name_carries_the_timestamp_and_the_team_slug(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path)
    result = run(archive, now=STAMP)
    assert result.outputs[0].name == f"2026-08-30T0307-{TEAM_SLUG}.md"


def test_running_twice_never_overwrites_the_earlier_report(tmp_path: Path) -> None:
    """The same minute, two runs: a new file, the old one left untouched."""
    archive = build_archive(tmp_path)

    first = run(archive, now=STAMP)
    first_path = archive.resolve(first.outputs[0])
    original = first_path.read_text(encoding="utf-8")

    second = run(archive, now=STAMP)
    second_path = archive.resolve(second.outputs[0])

    assert first_path != second_path
    assert second_path.name == f"2026-08-30T0307-{TEAM_SLUG}-02.md"
    assert first_path.is_file()
    assert first_path.read_text(encoding="utf-8") == original

    third = run(archive, now=STAMP)
    assert archive.resolve(third.outputs[0]).name.endswith("-03.md")
    assert len(reports(archive)) == 3


def test_rendering_twice_leaves_the_report_json_untouched(tmp_path: Path) -> None:
    """``render`` is a reader: the input is the same byte for byte after a run.

    **This test guards the stage's read-only contract, not any story.**
    ``render`` has never written ``report.json``, so the test would pass
    before Story 2.12 as well -- it therefore does not prove that moving the
    ids was a mere presentation choice. What proves that is that there is no
    change in ``domain/report.py`` at all.

    The contract is worth guarding all the same: ``render`` is the pipeline's
    only stage that reads from another stage's result area, and a write there
    would be exactly the layering breach the stage's contract
    (``run(...) -> StageResult``) forbids. The claim is made about **bytes**: a
    file read off the disk and written back could be a valid ``Report`` and
    still be a different file.

    Two runs and not one, because the absence of a skip is this stage's
    contract: the stage is always run, and still the input stays untouched.
    """
    archive = build_archive(tmp_path)
    source = archive.report_json(TEAM_KEY)
    before = source.read_bytes()

    run(archive, now=STAMP)
    run(archive, now=STAMP)

    assert source.read_bytes() == before
    # The constant and not a fourth copy of the literal: pinning the version
    # belongs to ``test_report_model``, and the claim here is "the same
    # version as before the run" -- not "the version is this number".
    assert json.loads(before)["schema_version"] == REPORT_SCHEMA_VERSION
    assert len(reports(archive)) == 2


def test_the_ordinal_is_zero_padded_so_the_listing_sorts(tmp_path: Path) -> None:
    """Without the padding a directory listing would order ``-10, -100, -11, -2``."""
    archive = build_archive(tmp_path)
    for _ in range(11):
        run(archive, now=STAMP)
    numbered = [
        path.name
        for path in reports(archive)
        if path.name != f"2026-08-30T0307-{TEAM_SLUG}.md"
    ]
    assert len(numbered) == 10
    # Alphabetical order is numerical order only when zero-padded: without the
    # padding "-10" would come before "-2".
    assert numbered == sorted(numbered)
    assert numbered[0].endswith("-02.md")
    assert numbered[-1].endswith("-11.md")


def test_a_full_minute_of_reports_fails_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running out of names is an error, not a reason to overwrite an old report."""
    monkeypatch.setattr(render_stage, "MAX_REPORTS_PER_MINUTE", 2)
    archive = build_archive(tmp_path)
    run(archive, now=STAMP)
    run(archive, now=STAMP)
    with pytest.raises(PappascoutError, match="never overwritten"):
        run(archive, now=STAMP)
    assert len(reports(archive)) == 2


def test_no_temporary_files_are_left_behind(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    run(archive)
    assert not has_temp_leftovers(archive.root)


def test_report_name_numbering_starts_without_a_suffix() -> None:
    assert report_name("2026-08-30T0307", "abc") == "2026-08-30T0307-abc.md"
    assert report_name("2026-08-30T0307", "abc", 2) == "2026-08-30T0307-abc-02.md"
    assert report_name("2026-08-30T0307", "abc", 12) == "2026-08-30T0307-abc-12.md"
    for bad in (0, -1, MAX_REPORTS_PER_MINUTE + 1):
        with pytest.raises(ValueError, match="report ordinal"):
            report_name("2026-08-30T0307", "abc", bad)


# --- The reservation and a failed write -----------------------------------------


def test_a_failed_write_does_not_leave_an_empty_report_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reservation is an empty file; if the write fails, it has to be undone.

    Without undoing it a zero-byte ``.md`` would stay in the directory for
    good, looking like a report, taking up an ordinal and not being an atomic
    write's temporary file -- that is, no check that looks for leftovers finds
    it.
    """

    def boom(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(render_stage, "atomic_write_text", boom)
    archive = build_archive(tmp_path)

    with pytest.raises(PappascoutError, match="reservation was cancelled"):
        run(archive, now=STAMP)

    assert reports(archive) == []
    assert not has_temp_leftovers(archive.root)


def test_a_write_failure_frees_the_name_for_the_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled reservation must not take the ordinal from the next run."""
    calls = {"n": 0}
    real = render_stage.atomic_write_text

    def once(path, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("the disk is full")
        return real(path, text)

    monkeypatch.setattr(render_stage, "atomic_write_text", once)
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError):
        run(archive, now=STAMP)
    result = run(archive, now=STAMP)
    assert result.outputs[0].name == f"2026-08-30T0307-{TEAM_SLUG}.md"


def test_an_unwritable_directory_is_a_clear_error_not_a_stack_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only ``FileExistsError`` means "the name is taken"; anything else is an error."""
    archive = build_archive(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("no permission")

    monkeypatch.setattr(os, "open", denied)
    with pytest.raises(PappascoutError, match="could not be reserved"):
        run(archive, now=STAMP)


def test_an_uncreatable_directory_is_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = build_archive(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError("no permission")

    monkeypatch.setattr(Path, "mkdir", denied)
    with pytest.raises(PappascoutError, match="could not be created"):
        run(archive, now=STAMP)


# --- Checking the input ---------------------------------------------------------


def test_a_missing_report_json_tells_the_user_to_aggregate(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    archive.report_json(TEAM_KEY).unlink()
    with pytest.raises(PappascoutError, match="aggregate"):
        render_stage.read_report(archive.report_json(TEAM_KEY), TEAM_KEY)


def test_a_different_schema_version_refuses_and_says_to_aggregate(
    tmp_path: Path,
) -> None:
    """An old schema version: the run refuses and does not format half a report."""
    archive = build_archive(tmp_path)
    path = archive.report_json(TEAM_KEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = "0.9.0"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PappascoutError) as excinfo:
        run(archive)
    message = str(excinfo.value)
    assert "schema version" in message
    assert "0.9.0" in message
    assert "aggregate" in message
    assert reports(archive) == []


def test_a_broken_report_json_is_a_clear_error_not_a_stack_trace(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path)
    archive.report_json(TEAM_KEY).write_text("{ not json", encoding="utf-8")
    with pytest.raises(PappascoutError, match="as JSON"):
        run(archive)


def test_a_report_that_does_not_match_the_model_is_rejected(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    path = archive.report_json(TEAM_KEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["sample"]["rounds"] = 999
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PappascoutError, match="does not match the report model"):
        run(archive)


def test_a_report_belonging_to_another_team_is_refused(tmp_path: Path) -> None:
    """``team.key`` and the directory's name are the same thing -- or the file is wrong.

    A difference means the report would be named after the directory while the
    round-list appendix's paths and the statistics told of another team.
    """
    archive = build_archive(tmp_path)
    path = archive.report_json(TEAM_KEY)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["team"]["key"] = OTHER_TEAM
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(PappascoutError, match="is about team"):
        run(archive)
    assert reports(archive) == []


# --- Choosing the team ----------------------------------------------------------


def test_team_keys_lists_only_aggregated_teams(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    (archive.root / "aggregates" / "tyhja").mkdir(parents=True)
    assert render_stage.team_keys(archive) == [TEAM_KEY]


def test_a_unique_prefix_is_enough(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    assert render_stage.resolve_team(archive, TEAM_KEY[:6]) == TEAM_KEY


def test_a_prefix_that_matches_nothing_lists_the_candidates(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError, match="matches no team at all"):
        render_stage.resolve_team(archive, "zz")


def test_a_missing_team_option_lists_the_candidates(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError, match="with the --team option"):
        render_stage.resolve_team(archive, None)


@pytest.mark.parametrize("empty", ["", "   ", "\t"])
def test_an_empty_team_is_not_a_prefix_that_matches_everything(
    tmp_path: Path, empty: str
) -> None:
    """Every id begins with the empty string.

    Without the check ``--team ""`` would silently pick the only team -- that
    is, do exactly what requiring ``--team`` prevents.
    """
    archive = build_archive(tmp_path)
    with pytest.raises(PappascoutError, match="The team id is empty"):
        render_stage.resolve_team(archive, empty)


def test_a_prefix_matching_two_teams_is_refused(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path,
        teams={"aaaa1111": report([pistol_map()]), "aaaa2222": report([pistol_map()])},
    )
    with pytest.raises(PappascoutError, match="matches more than one team"):
        render_stage.resolve_team(archive, "aaaa")


def test_an_empty_archive_says_to_aggregate_first(tmp_path: Path) -> None:
    archive = ArchivePaths(root=tmp_path / "tyhja")
    with pytest.raises(PappascoutError, match="aggregate --team"):
        render_stage.resolve_team(archive, TEAM_KEY)


# --- The round-list appendix's paths --------------------------------------------


def test_round_list_paths_are_absolute_and_come_from_archive_paths(
    tmp_path: Path,
) -> None:
    """From a report attached in Discord the reader cannot see where the archive is."""
    archive = build_archive(tmp_path)
    entry = render_stage.read_report(archive.report_json(TEAM_KEY), TEAM_KEY)
    paths = render_stage.round_list_paths(archive, entry)
    assert paths == [str(archive.classified_round_list(TEAM_KEY, DEMO_ID))]
    assert Path(paths[0]).is_absolute()


def test_the_written_report_contains_those_paths(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    result = run(archive)
    text = archive.resolve(result.outputs[0]).read_text(encoding="utf-8")
    assert str(archive.classified_round_list(TEAM_KEY, DEMO_ID)) in text


# --- The manifest ---------------------------------------------------------------


def manifest_of(archive: ArchivePaths, result) -> Manifest:
    return Manifest.read(archive.resolve(result.manifest_path))


def test_the_manifest_records_the_input_the_parameters_and_the_output(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path)
    result = run(archive)

    manifest = manifest_of(archive, result)
    assert manifest.stage == "render"
    assert manifest.status == "ok"
    assert manifest.outputs == [str(result.outputs[0])]
    assert manifest.inputs[0].result_id == f"aggregates/{TEAM_KEY}"
    assert manifest.inputs[0].sha256
    assert "jinja2" in manifest.tool_versions


def test_each_report_gets_its_own_manifest(tmp_path: Path) -> None:
    """A shared manifest would stand up badly to exactly the concurrency the
    name is reserved against: the last writer would remain in force and would
    describe a different report from the one the user has just been given."""
    archive = build_archive(tmp_path)
    first = run(archive, now=STAMP)
    second = run(archive, now=STAMP)

    assert first.manifest_path != second.manifest_path
    assert manifest_of(archive, first).outputs == [str(first.outputs[0])]
    assert manifest_of(archive, second).outputs == [str(second.outputs[0])]
    assert first.manifest_path.name == f"2026-08-30T0307-{TEAM_SLUG}.manifest.json"


def test_a_missing_aggregate_manifest_does_not_stop_the_report(
    tmp_path: Path,
) -> None:
    """The report matters more than the traceability; the input is marked unknown."""
    archive = build_archive(tmp_path, write_manifest=False)
    result = run(archive)
    assert archive.resolve(result.outputs[0]).is_file()
    assert manifest_of(archive, result).inputs[0].sha256 == ""


def test_the_template_is_part_of_the_parameter_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing the template changes the report, so it shows up in the manifest."""
    archive = build_archive(tmp_path)
    before = manifest_of(archive, run(archive)).params_hash

    monkeypatch.setattr(render_stage, "template_digest", lambda: "f" * 64)
    after = manifest_of(archive, run(archive)).params_hash
    assert before != after


def test_a_pruning_setting_is_part_of_the_parameter_hash(tmp_path: Path) -> None:
    """Story 2.13: a setting that is read but not in the hash is the Story 1.8 defect.

    The stage got its first settings of its own in that story. Without them in
    the hash the manifest would claim that two different reports are the same
    result -- and this defect has been found three times in this project.
    """
    archive = build_archive(tmp_path)
    default = manifest_of(archive, run(archive)).params_hash

    for changed in (
        ReportSettings(drop_saturated_equipment_lines=False),
        ReportSettings(merge_equal_equipment_lines=False),
        ReportSettings(skip_sample_seconds=[45.0]),
        ReportSettings(max_utility_targets=0),
        ReportSettings(max_kill_areas=5),
    ):
        other = manifest_of(archive, run(archive, settings=changed)).params_hash
        assert other != default, changed

    # And the same setting produces the same hash: the hash must not change
    # with the run's time or the file's name, or the manifest would say
    # nothing about anything.
    assert manifest_of(archive, run(archive)).params_hash == default


def test_the_hash_covers_the_template_and_the_whole_section(
    tmp_path: Path,
) -> None:
    """The hash's input is locked: the template and ``[report]`` **whole**.

    The section goes into the hash as a ``model_dump`` and not as named
    fields, so a sixth pruning rule is in there the moment it is in the
    section. A listing would go stale quietly at exactly the moment the rule
    is added -- and that is the same failure mode as a missing setting itself.
    """
    archive = build_archive(tmp_path)
    settings = ReportSettings(max_kill_areas=4)
    manifest = manifest_of(archive, run(archive, settings=settings))
    assert manifest.params_hash == compute_params_hash(
        {
            "render": {"template_sha256": render_stage.template_digest()},
            "report": settings.model_dump(mode="json"),
        }
    )


def test_the_same_rules_in_a_different_order_hash_the_same(
    tmp_path: Path,
) -> None:
    """Point G1: an identical report, an identical parameter hash.

    The sample-point list is sorted at load time, so ``[45, 15]`` and
    ``[15, 45]`` are the same setting. Without the sorting the manifest would
    claim that two character-for-character identical reports came from
    different parameters -- and the manifest's whole value is that a
    difference in it means a difference in the result.
    """
    archive = build_archive(tmp_path)
    one = ReportSettings(skip_sample_seconds=[45.0, 15.0])
    other = ReportSettings(skip_sample_seconds=[15.0, 45.0])
    assert one == other
    first = manifest_of(archive, run(archive, settings=one))
    second = manifest_of(archive, run(archive, settings=other))
    assert first.params_hash == second.params_hash
    # And a different set is a different hash: sorting must not lump different
    # values together.
    third = manifest_of(
        archive, run(archive, settings=ReportSettings(skip_sample_seconds=[30.0]))
    )
    assert third.params_hash != first.params_hash


def test_the_stage_is_never_skipped(tmp_path: Path) -> None:
    """The user asked for a report; a skip would leave him without the file."""
    archive = build_archive(tmp_path)
    first = run(archive)
    second = run(archive)
    assert not first.skipped
    assert not second.skipped
    assert first.outputs != second.outputs


# --- The result's numbers -------------------------------------------------------


def test_stats_carry_the_numbers_the_user_checks(tmp_path: Path) -> None:
    archive = build_archive(tmp_path)
    stats = run(archive).stats
    assert stats["team_key"] == TEAM_KEY
    assert stats["team_name_known"] is True
    assert stats["demos"] == 1
    assert stats["rounds"] == 2
    assert stats["maps"] == ["de_ancient"]
    assert stats["lines"] > 0


def test_stats_flag_a_team_without_a_name(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path, teams={TEAM_KEY: report([pistol_map()], display_name=TEAM_KEY)}
    )
    assert run(archive).stats["team_name_known"] is False
