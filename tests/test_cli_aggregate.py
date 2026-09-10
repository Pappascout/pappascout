"""``pappascout aggregate`` -- the command's and its summary's tests.

Two things are locked down here:

* **AD-3**: the command gives the stage only ``settings.thresholds`` and
  ``settings.league``. If the stage saw the ``[parse]`` section, the promise
  "a change of threshold does not reparse" would no longer be structural.
* **The sample is in the output.** The user checks from the summary whether
  the data they expected came in -- without it a missing demo is noticed only
  in the finished report.

The stage itself is replaced, so none of these tests reads a demo or the
archive.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, _render_aggregate, app, main
from pappascout.domain.models import (
    SETTINGS_ENV_VAR,
    AggregateSettings,
    LeagueSettings,
    ThresholdSettings,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult

runner = CliRunner()

TEAM = "aaaaaaaaaaaaaaaa"
TEAM_B = "bbbbbbbbbbbbbbbb"


def aggregate_result(**overrides) -> StageResult:
    defaults: dict[str, object] = {
        "stage": "aggregate",
        "unit": TEAM,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath(f"aggregates/{TEAM}/report.json"),),
        "manifest_path": PurePosixPath(f"aggregates/{TEAM}/report.manifest.json"),
        "duration_s": 0.42,
        "stats": {
            "team_key": TEAM,
            "lineup_keys": [TEAM, TEAM_B],
            "display_name": "MatureMayhem",
            "display_name_source": "clan_name",
            "display_name_alternatives": [],
            "roster": [
                {"player_id": str(n), "display_name": f"pelaaja{n}"}
                for n in range(1, 7)
            ],
            "demos": 4,
            "rounds": 85,
            "sample": {
                "league": {"demos": 0, "rounds": 0},
                "other": {"demos": 0, "rounds": 0},
                "unknown": {"demos": 4, "rounds": 85},
            },
            "unclassified": 0,
            "unpaired_detonations": 0,
            "classify_thresholds": {"full_equip_min": 4000},
            "maps": [
                {
                    "map_name": "de_nuke",
                    "map_name_source": "map_demo_id",
                    "demos": 1,
                    "rounds": 23,
                    "sides": [
                        {
                            "side": "T",
                            "round_types": {"pistol": 1, "eco": 2, "full": 8},
                            "small_samples": ["pistol", "eco"],
                        }
                    ],
                }
            ],
            "missing_demos": [],
        },
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


def field_value(output_text: str, label: str) -> str:
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"there is no {label!r} row in the output:\n{output_text}")


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace ``stages.aggregate.run``; return what the stage was given."""
    seen: dict[str, object] = {}

    def fake_run(
        thresholds, league, archive, team, *, aggregate_settings, **kwargs
    ):
        seen["thresholds"] = thresholds
        seen["league"] = league
        seen["aggregate"] = aggregate_settings
        seen["archive"] = archive
        seen["team"] = team
        seen["kwargs"] = kwargs
        error = seen.get("virhe")
        if error is not None:
            raise error  # type: ignore[misc]
        return seen.get("tulos") or aggregate_result()

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.aggregate.run", fake_run)
    return seen


# --- The summary ----------------------------------------------------------------


def test_summary_reports_the_sample_first() -> None:
    output_text = _render_aggregate(aggregate_result())
    assert output_text.startswith("Aggregated:")
    assert field_value(output_text, "Sample") == "4 demos, 85 rounds"


def test_summary_names_all_three_buckets_in_finnish() -> None:
    """Three buckets, not two -- also when two of them are empty.

    **The bucket names stay Finnish, and the name of this test stays true.**
    They are ``SAMPLE_BUCKET_FI``, report vocabulary that passes through the
    console (AD-11), so the reader sees the same word here as in the report.
    The JSON keys stay English because they are part of the contract, and the
    rest of the line is console text, which T15 translated. The same division
    of labour as with ``ROUND_TYPE_FI``.
    """
    output_text = _render_aggregate(aggregate_result())
    buckets = field_value(output_text, "Buckets")
    assert "liiga 0 demos" in buckets
    assert "muut 0 demos" in buckets
    assert "tuntematon 4 demos / 85 rounds" in buckets
    assert "league" not in buckets and "unknown" not in buckets


def test_summary_reports_unpaired_detonations() -> None:
    """A silent drop would look as if no grenade had been thrown."""
    stats = dict(aggregate_result().stats)
    stats["unpaired_detonations"] = 3
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert field_value(output_text, "Unpaired detonations").startswith("3 ")


def test_summary_omits_unpaired_detonations_when_there_are_none() -> None:
    assert "Unpaired" not in _render_aggregate(aggregate_result())


def test_summary_says_when_two_lineups_were_joined() -> None:
    """Joining them is a decision, so it is visible and not to be inferred."""
    output_text = _render_aggregate(aggregate_result())
    assert TEAM_B in field_value(output_text, "Lineups")


def test_summary_does_not_mention_lineups_when_there_is_only_one() -> None:
    stats = dict(aggregate_result().stats)
    stats["lineup_keys"] = [TEAM]
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert "Lineups" not in output_text


def test_summary_marks_small_samples_per_side() -> None:
    output_text = _render_aggregate(aggregate_result())
    assert "small sample: pistol, eco" in output_text
    assert "T: pistol 1, eco 2, full 8" in output_text


def test_summary_shows_unclassified_rounds() -> None:
    stats = dict(aggregate_result().stats)
    stats["unclassified"] = 3
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert field_value(output_text, "Unclassified").startswith("3 rounds")


def test_summary_lists_missing_demos_with_their_reason() -> None:
    """A missing demo does not disappear quietly."""
    stats = dict(aggregate_result().stats)
    stats["missing_demos"] = [{"match": "Anubis_vs_b", "reason": "not parsed"}]
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert field_value(output_text, "Missing demo") == "Anubis_vs_b: not parsed"


def test_summary_says_when_the_stage_was_skipped() -> None:
    result = aggregate_result(skipped=True, reason="The result is up to date.")
    output_text = _render_aggregate(result)
    assert output_text.startswith("Skipped:")
    assert field_value(output_text, "Reason") == "The result is up to date."


@pytest.mark.parametrize(
    "source,note",
    [
        ("demo_header", ""),
        ("map_demo_id", ""),
        ("unknown", " (name unknown)"),
    ],
)
def test_summary_marks_only_the_map_whose_name_is_unknown(
    source: str, note: str
) -> None:
    """The mark belongs to the ``unknown`` source **only** (Story 2.11).

    Three pairs and not one, and that is a measured need. The condition was
    originally written as a listing of the known sources
    (``"" if source == "map_demo_id" else " (name unknown)"``), and when the
    third source arrived it would have marked every map read from a header as
    unknown. A one-source test does not notice that: a mutation that restores
    the old condition passes the ``unknown`` case as it is and the whole rest
    of the series with it. Only the ``demo_header`` pair goes red.
    """
    stats = dict(aggregate_result().stats)
    stats["maps"] = [
        {
            "map_name": "de_ancient",
            "map_name_source": source,
            "demos": 2,
            "rounds": 42,
            "sides": [],
        }
    ]
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert f"de_ancient{note}: 2 demos, 42 rounds" in output_text
    if not note:
        assert "name unknown" not in output_text


def test_summary_names_the_output_and_the_manifest() -> None:
    output_text = _render_aggregate(aggregate_result())
    assert field_value(output_text, "Output") == f"aggregates/{TEAM}/report.json"
    assert (
        field_value(output_text, "Manifest")
        == f"aggregates/{TEAM}/report.manifest.json"
    )
    assert field_value(output_text, "Run time") == "0,4 s"


# --- The command's wiring -------------------------------------------------------


def test_help_lists_aggregate() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "aggregate" in result.output


def test_command_passes_only_its_own_settings_sections(fake_stage: dict) -> None:
    """AD-3: the stage sees neither the ``[parse]`` nor the ``[economy]`` section."""
    result = runner.invoke(app, ["aggregate", "--team", TEAM])
    assert result.exit_code == 0, result.output
    assert isinstance(fake_stage["thresholds"], ThresholdSettings)
    assert isinstance(fake_stage["league"], LeagueSettings)
    assert isinstance(fake_stage["aggregate"], AggregateSettings)
    assert fake_stage["team"] == TEAM
    assert fake_stage["kwargs"] == {"force": False}


def test_force_flag_reaches_the_stage(fake_stage: dict) -> None:
    result = runner.invoke(app, ["aggregate", "--team", TEAM, "--force"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kwargs"] == {"force": True}


def test_without_team_the_stage_decides_what_to_say(fake_stage: dict) -> None:
    """The team listing is the stage's knowledge, not the command line's."""
    result = runner.invoke(app, ["aggregate"])
    assert result.exit_code == 0, result.output
    assert fake_stage["team"] is None


def test_the_team_option_help_matches_what_actually_happens() -> None:
    """Without --team the run ends in an error; the help must not promise a listing."""
    result = runner.invoke(app, ["aggregate", "--help"])
    assert result.exit_code == 0
    # Typer wraps the help text into a box, so the border lines (Unicode rules
    # or ASCII pipes depending on the environment) and the line breaks have to
    # be cleaned away before the comparison.
    text = " ".join(
        "".join(
            " " if ord(ch) >= 0x2500 or ch == "|" else ch for ch in result.output
        ).split()
    )
    assert "the run ends in an error that lists the archive's teams" in text
    assert "the command lists the archive" not in text


def test_a_known_error_ends_the_run_with_exit_code_one(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name used to say the line is in Finnish; the exit code is what it pins."""
    fake_stage["virhe"] = PappascoutError("The team was not found.")
    monkeypatch.setattr("sys.argv", ["pappascout", "aggregate", "--team", TEAM])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == EXIT_KNOWN_ERROR


# --- The team's name and roster in the output (Story 2.6) -----------------------


def test_an_observed_name_is_reported_as_observed() -> None:
    """The source is in the output, not just the name.

    Without the source the reader cannot see whether the heading holds an
    observation or an id standing in for one -- and that is exactly what they
    check from the output.
    """
    output_text = _render_aggregate(aggregate_result())
    assert field_value(output_text, "Name") == (
        "MatureMayhem (observed from the demos)"
    )


def test_a_missing_name_says_the_report_speaks_of_the_key() -> None:
    numbers = dict(aggregate_result().stats)
    numbers.update(display_name=TEAM, display_name_source="team_key")
    output_text = _render_aggregate(aggregate_result(stats=numbers))
    assert field_value(output_text, "Name") == (
        f"not observed -- the report speaks of the id {TEAM}"
    )


def test_conflicting_names_are_listed_and_absent_when_there_is_no_conflict() -> None:
    assert "Other observed names" not in _render_aggregate(aggregate_result())

    numbers = dict(aggregate_result().stats)
    numbers["display_name_alternatives"] = ["MM Academy", "MM B"]
    output_text = _render_aggregate(aggregate_result(stats=numbers))
    assert field_value(output_text, "Other observed names").startswith(
        "MM Academy, MM B"
    )


def test_the_roster_line_lists_the_names_not_just_their_count() -> None:
    """Six SteamID64s would fill the output without saying more."""
    output_text = _render_aggregate(aggregate_result())
    assert field_value(output_text, "Roster") == (
        "6 players observed: pelaaja1, pelaaja2, pelaaja3, pelaaja4, "
        "pelaaja5, pelaaja6"
    )


def test_a_player_without_a_name_shows_the_id_and_is_counted() -> None:
    """A player with no name is said out loud and not dropped."""
    numbers = dict(aggregate_result().stats)
    numbers["roster"] = [
        {"player_id": "1", "display_name": "Sassiz"},
        {"player_id": "76561198163808926", "display_name": None},
    ]
    output_text = _render_aggregate(aggregate_result(stats=numbers))
    value = field_value(output_text, "Roster")
    assert value == (
        "2 players observed: Sassiz, 76561198163808926 "
        "(1 without a name, the id stands in for it)"
    )
