"""``pappascout classify`` -- the command's and the round list's tests.

Two things are locked down here:

* **AD-3**: the command gives the stage only ``settings.thresholds`` and
  ``settings.league``. If the stage saw the ``[parse]`` section, the promise
  "a change of threshold does not reparse" would no longer be structural.
* **SM-2**: ``--show`` prints the list the user checks the classification
  against the demo with -- for every round the type, the input values and the
  reasoning, and the reasoning is not truncated.

The stage itself is replaced, so none of these tests reads a demo.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    _render_classify,
    _render_round_list,
    app,
    main,
)
from pappascout.constants import UNCLASSIFIED
from pappascout.domain.models import (
    SETTINGS_ENV_VAR,
    EconomySettings,
    LeagueSettings,
    ThresholdSettings,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult
from pappascout.stages.classify import ROUND_LIST_COLUMNS, round_list_cells

runner = CliRunner()

DEMO_ID = "1-abc-1"
TEAM = "aaaaaaaaaaaaaaaa"
TEAM_B = "bbbbbbbbbbbbbbbb"


def row(**overrides) -> dict:
    defaults = {
        "round_no": 1,
        "side": "T",
        "won": False,
        "round_type": "pistol",
        "opp_round_type": "pistol",
        "loss_count": 1,
        "money_per_player": 270,
        "money_available_per_player": 1070,
        "spent_per_player": 530,
        "equip_per_player": 730,
        "players": 5,
        # The stage's own wording (``economy``, translated by T10), so the
        # needles below stay pointed at the text the user really sees.
        "reason": "Round 1 is a pistol round (1, 13), so the economy "
        "reasoning is not applied.",
    }
    defaults.update(overrides)
    return defaults


def classify_result(**overrides) -> StageResult:
    defaults: dict[str, object] = {
        "stage": "classify",
        "unit": DEMO_ID,
        "status": "ok",
        "skipped": False,
        "outputs": (
            PurePosixPath(f"classified/{TEAM}/{DEMO_ID}.parquet"),
            PurePosixPath(f"classified/{TEAM}/{DEMO_ID}.md"),
        ),
        "manifest_path": PurePosixPath(f"classified/{TEAM}/{DEMO_ID}.manifest.json"),
        "duration_s": 0.42,
        "stats": {
            "team_key": TEAM,
            "rounds": 3,
            "by_type": {"pistol": 1, "eco": 1, "full": 1},
            "unclassified": 0,
            "unnumbered": 0,
            "round_list": f"classified/{TEAM}/{DEMO_ID}.md",
            "rows": [
                row(),
                row(round_no=2, round_type="eco", opp_round_type="full", loss_count=2),
                row(round_no=3, round_type="full", won=True),
            ],
        },
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


def field_value(output_text: str, label: str) -> str:
    for r in output_text.splitlines():
        stripped = r.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(
        f"there is no {label!r} row in the output:" + chr(10) + output_text
    )


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace ``stages.classify.run``; return what the stage was given."""
    seen: dict[str, object] = {"kutsut": []}

    def fake_run(
        thresholds, league, archive, map_demo_id, team, *, economy, **kwargs
    ):
        seen["thresholds"] = thresholds
        seen["league"] = league
        seen["economy"] = economy
        seen["archive"] = archive
        seen["unit"] = map_demo_id
        seen["team"] = team
        seen["kwargs"] = kwargs
        seen["kutsut"].append(team)  # type: ignore[union-attr]
        error = seen.get("virhe")
        if error is not None:
            raise error  # type: ignore[misc]
        return seen.get("tulos") or classify_result(unit=map_demo_id)

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.classify.run", fake_run)
    monkeypatch.setattr(
        "pappascout.stages.classify.team_keys", lambda archive, target: [TEAM, TEAM_B]
    )
    return seen


# --- The summary --------------------------------------------------------------------


def test_summary_reports_team_rounds_and_type_distribution() -> None:
    output_text = _render_classify(classify_result())
    assert output_text.startswith("Classified:")
    assert field_value(output_text, "Team") == TEAM
    assert field_value(output_text, "Rounds") == "3"
    assert field_value(output_text, "Types") == "pistol 1, eco 1, full 1"
    assert field_value(output_text, "Run time") == "0,4 s"


def test_summary_lists_both_outputs() -> None:
    """The Parquet and the round list are both results of the run."""
    output_text = _render_classify(classify_result())
    assert ".parquet" in output_text
    assert ".md" in output_text


def test_summary_says_when_the_stage_was_skipped() -> None:
    result = classify_result(skipped=True, reason="The result is up to date.")
    output_text = _render_classify(result)
    assert output_text.startswith("Skipped:")
    assert field_value(output_text, "Reason") == "The result is up to date."


def test_summary_shows_unclassified_rounds_exactly_once() -> None:
    """Unclassified is a state, not a round type -- so not in the breakdown.

    ``UNCLASSIFIED`` is ``constants``' Finnish word and stays Finnish: it is
    the only visible name of such a round, in this output and in the report's
    sections alike, so the two have to call it the same thing.
    """
    result = classify_result(
        stats={
            "team_key": TEAM,
            "rounds": 3,
            "by_type": {"pistol": 2},
            "unclassified": 1,
            "unnumbered": 0,
            "rows": [],
        }
    )
    output_text = _render_classify(result)
    assert field_value(output_text, UNCLASSIFIED.capitalize()).startswith(
        "1 (the observation is missing"
    )
    assert output_text.lower().count(UNCLASSIFIED) == 1
    assert field_value(output_text, "Types") == "pistol 2"


def test_summary_reports_rounds_dropped_for_having_no_number() -> None:
    result = classify_result(
        stats={
            "team_key": TEAM,
            "rounds": 3,
            "by_type": {"pistol": 3},
            "unclassified": 0,
            "unnumbered": 2,
            "rows": [],
        }
    )
    assert field_value(_render_classify(result), "Unnumbered").startswith("2 (")


def test_summary_hides_the_counters_that_are_zero() -> None:
    output_text = _render_classify(classify_result())
    assert UNCLASSIFIED.capitalize() not in output_text
    assert "Unnumbered" not in output_text


def test_summary_never_claims_zero_rounds_when_unreadable() -> None:
    result = classify_result(
        skipped=True, stats={"team_key": TEAM, "unreadable": "OSError: broken"}
    )
    assert "no counts obtained" in _render_classify(result)


# --- The round list ---------------------------------------------------------------


def test_round_list_shows_every_input_the_decision_used() -> None:
    output_text = _render_round_list([row()])
    for label in ("Round", "Side", "Type", "Available", "Left", "Bought",
                    "Equipment", "Loss"):
        assert label in output_text
    assert "270" in output_text
    assert "1070" in output_text
    assert "530" in output_text
    assert "730" in output_text
    assert "pistol" in output_text


def test_round_list_never_truncates_the_reason() -> None:
    """The reasoning is exactly what the classification is checked against."""
    long_reason = "Eco after a lost round: " + "x" * 200
    output_text = _render_round_list([row(reason=long_reason)])
    assert long_reason in output_text


def test_round_list_shows_the_opponent_type_too() -> None:
    output_text = _render_round_list([row(round_type="eco", opp_round_type="full")])
    assert "eco" in output_text
    assert "full" in output_text


def test_round_list_marks_an_unclassified_round() -> None:
    """A missing classification shows by name and a missing value as a dash."""
    output_text = _render_round_list(
        [
            row(
                round_type=None,
                money_per_player=None,
                money_available_per_player=None,
                spent_per_player=None,
                equip_per_player=None,
                reason="Round 1 is not classified: the status is 'no_freeze_end'.",
            )
        ]
    )
    data_line = output_text.splitlines()[2]
    assert data_line.split() == [
        "1",
        "T",
        "loss",
        UNCLASSIFIED,
        "pistol",
        "-",
        "-",
        "-",
        "-",
        "1",
        # Bonus, Armed and Can-buy: a missing counter is a dash and not "0/5".
        # A zero would claim as an observation that nobody was able to buy.
        "-",
        "-",
        "-",
    ]
    assert "0" not in data_line
    assert "no_freeze_end" in output_text


def test_round_list_columns_line_up() -> None:
    rows = [row(), row(round_no=12, round_type="anomaly", money_per_player=12345)]
    out_lines = [r for r in _render_round_list(rows).splitlines() if r]
    label, rule_line = out_lines[0], out_lines[1]
    assert len(rule_line) >= len(label) - 2
    assert set(rule_line.replace(" ", "")) == {"-"}


def test_console_and_markdown_share_one_column_definition() -> None:
    """Two column definitions would drift, and the outputs would differ."""
    headers = [o for o, _ in ROUND_LIST_COLUMNS]
    output_text = _render_round_list([row()])
    header_line = output_text.splitlines()[0]
    # The reasoning is on a row of its own, the other columns on the header row.
    for label in headers[:-1]:
        assert label in header_line
    assert headers[-1] == "Reason"
    # The cells come from the stage's own function, not from a copy in the CLI.
    cells = round_list_cells(row())
    assert len(cells) == len(ROUND_LIST_COLUMNS)
    for cell in cells[:-1]:
        assert cell in output_text


def test_empty_round_list_says_so() -> None:
    assert _render_round_list([]) == "There are no rounds."


# --- The command --------------------------------------------------------------------


def test_stage_gets_only_the_three_sections_it_reads(fake_stage) -> None:
    """AD-3: the stage must see neither ``[parse]`` nor ``[project]``.

    ``[economy]`` came in with Story 1.10, because the half-buy's condition B
    reads the loss bonus steps from it. The partition did not loosen even so:
    the stage still gets ready-made sections and not the whole ``Settings``
    object, so it cannot accidentally start reading the archive's path or the
    parsing window.
    """
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM])
    assert result.exit_code == 0, result.output

    thresholds = fake_stage["thresholds"]
    league = fake_stage["league"]
    economy = fake_stage["economy"]
    assert isinstance(thresholds, ThresholdSettings)
    assert isinstance(league, LeagueSettings)
    assert isinstance(economy, EconomySettings)
    assert economy.loss_bonus_steps
    for forbidden in ("parse", "economy", "project", "league"):
        assert not hasattr(thresholds, forbidden)
    for forbidden in ("parse", "economy", "project", "thresholds"):
        assert not hasattr(league, forbidden)
    for forbidden in ("parse", "project", "thresholds", "league"):
        assert not hasattr(economy, forbidden)
    assert fake_stage["unit"] == DEMO_ID
    assert fake_stage["team"] == TEAM
    assert fake_stage["kwargs"]["force"] is False


def test_force_flag_reaches_the_stage(fake_stage) -> None:
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--force"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kwargs"]["force"] is True


def test_round_list_is_printed_only_with_show(fake_stage) -> None:
    without_show = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM])
    assert "Round " not in without_show.output

    with_show = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--show"])
    assert with_show.exit_code == 0, with_show.output
    assert "Round" in with_show.output
    assert "is a pistol round" in with_show.output


def test_all_teams_flag_classifies_both_lineups(fake_stage) -> None:
    """Both teams are classified in any case -- this also saves them."""
    result = runner.invoke(app, ["classify", DEMO_ID, "--all-teams"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kutsut"] == [TEAM, TEAM_B]
    assert result.output.count("Classified:") == 2


def test_missing_team_is_passed_through_as_none(fake_stage) -> None:
    """The stage decides the error, because only it knows the demo's lineups."""
    runner.invoke(app, ["classify", DEMO_ID])
    assert fake_stage["team"] is None


def test_missing_round_list_tells_what_to_do(fake_stage) -> None:
    fake_stage["tulos"] = classify_result(
        skipped=True, stats={"team_key": TEAM, "unreadable": "OSError: broken"}
    )
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--show"])
    assert result.exit_code == 0, result.output
    assert "The round list could not be read" in result.output
    assert "--force" in result.output


def test_unknown_team_gets_a_listing_without_a_traceback(
    fake_stage, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The name used to say the error is in Finnish; AD-11 made that false.

    **The message is ``classify._resolve``'s own wording**, translated by T6;
    the Finnish copy that used to stand here had stopped matching it.
    """
    fake_stage["virhe"] = PappascoutError(
        "The lineup key 'xxx' matches neither lineup of demo 1-abc-1.\n"
        "The demo's lineups are:\n    aaa\n    bbb"
    )
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "classify", DEMO_ID, "--team", "xxx"]
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    error = capsys.readouterr().err
    assert "matches neither lineup" in error
    assert "lineups are" in error
    assert "Traceback" not in error
