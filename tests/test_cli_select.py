"""``pappascout select`` -- the command's tests (Story 3.3).

Four things are locked down here:

* **AD-3 and the layering rule.** The command gives the stage only
  ``settings.league`` and ``settings.thresholds``, and **no port at all** --
  the stage does not go on the network.
* **The command prints.** One test runs the whole ``discover`` -> ``select``
  chain with the real stages behind a fake port and reads the screen. Without
  it ``typer.echo`` could disappear from the command, and the command would
  write the file without saying anything.
* **A rejection's reason is on the screen in full.** The user does not code
  and does not open the JSON, so a truncated or missing reason would mean a
  decision they cannot check.
* **An error says what to do next.**

The formatting of the summary (``_render_select``) is tested **on a real
run's result** in ``test_stage_select.py``. In this file the stage is
replaced only where the test's subject is the command's wiring and not its
output.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from test_stage_discover import CHAMPIONSHIP, FakeSource, division_matches
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, app, main
from pappascout.domain.models import SETTINGS_ENV_VAR, LeagueSettings, ThresholdSettings
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult

runner = CliRunner()

TEAM_KEY = "f56dd02a-6107-48e2-abfb-75e7ec7ebcb2"


def select_result(**overrides) -> StageResult:
    """The stage's result for the wiring tests. **Not for testing the output.**"""
    defaults: dict[str, object] = {
        "stage": "select",
        "unit": TEAM_KEY,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath(f"index/selections/{TEAM_KEY}.json"),),
        "manifest_path": None,
        "reason": None,
        "duration_s": 0.42,
        "stats": {
            "map_demos": 2,
            "accepted": 2,
            "rejected": 0,
            "league": 2,
            "observed": 0,
            "predicted": 2,
            "drifted": 0,
            "uncertain": 0,
            "class_5/5": 2,
            "class_4/5": 0,
            "team_key": TEAM_KEY,
            "team_display": "Rcave Veterans",
            "roster_players": 7,
            "roster_threshold": "4/5",
            "matches_seen": 11,
            "matches_with_maps": 1,
            "matches_not_played": 10,
            "matches_without_veto": 0,
            "rejections": [],
            "rejections_total": 0,
            "notes": [],
            "generated_at": "2026-09-04T12:00:00+00:00",
        },
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the stage; return what the stage was given."""
    seen: dict[str, object] = {}

    def fake_run(league, archive, team, *, thresholds, **kwargs):
        seen["league"] = league
        seen["archive"] = archive
        seen["team"] = team
        seen["thresholds"] = thresholds
        seen["kwargs"] = kwargs
        error = seen.get("virhe")
        if error is not None:
            raise error  # type: ignore[misc]
        return seen.get("tulos") or select_result()

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.select.run", fake_run)
    return seen


@pytest.fixture
def real_pipeline(settings_file, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The real stages, a fake port -- and the archive in a temporary directory.

    This is the wiring no replaced stage can prove: the settings are read, the
    archive's paths are built, both stages are run and the output is assembled
    from a real result.
    """
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    return settings_file.parent / "arkisto"


# --- The command gives the stage the right sections ------------------------


def test_the_command_passes_only_league_and_thresholds(fake_stage: dict) -> None:
    """AD-3: the stage does not see ``[parse]`` and so cannot invalidate parsing."""
    result = runner.invoke(app, ["select", "--team", "Rcave"])

    assert result.exit_code == 0, result.output
    assert isinstance(fake_stage["league"], LeagueSettings)
    assert isinstance(fake_stage["thresholds"], ThresholdSettings)
    assert fake_stage["team"] == "Rcave"
    assert fake_stage["kwargs"] == {}


def test_the_stage_gets_no_port_because_it_never_touches_the_network(
    fake_stage: dict,
) -> None:
    """``select`` reads the indexes; the network belongs to ``discover`` and ``fetch``."""
    result = runner.invoke(app, ["select", "--team", "Rcave"])

    assert result.exit_code == 0, result.output
    assert "source" not in fake_stage


def test_the_team_option_is_required(fake_stage: dict) -> None:
    """The selection file is per team: without a team there is no file."""
    result = runner.invoke(app, ["select"])

    assert result.exit_code != 0
    assert "team" in result.output


def test_the_help_names_the_threshold_and_the_team_option() -> None:
    """The help says what the command chooses by and what ``--team`` takes.

    The name of this test used to say the help is in Finnish. AD-11 moved the
    console into English on 2026-09-07, so the claim would now be false; what
    the assertions pin is that the help still names both.
    """
    result = runner.invoke(app, ["select", "--help"])

    assert result.exit_code == 0
    # Single words: Typer wraps the help into a box, and a two-word needle
    # would break on the wrap rather than on a real change.
    assert "threshold" in result.output
    assert "unambiguous" in result.output


# --- The command prints, and the output comes from a real run --------------


def test_the_command_prints_its_summary_and_writes_the_file(
    real_pipeline: Path,
) -> None:
    """The whole chain with the real stages: the file **and** the output.

    Without a check on the output ``typer.echo`` could disappear from the
    command: the file would come into being, the run would succeed and the
    user would be looking at a blank screen.
    """
    assert runner.invoke(app, ["discover"]).exit_code == 0

    result = runner.invoke(app, ["select", "--team", "Potku"])

    assert result.exit_code == 0, result.output
    assert result.output.strip(), "the command printed nothing"
    assert "Selection made" in result.output
    assert "PotkukelkkaPeek" in result.output
    assert "2 / 2 maps into the sample" in result.output
    assert "index/selections/" in result.output
    written = list((real_pipeline / "index" / "selections").glob("*.json"))
    assert len(written) == 1


def test_the_printed_summary_names_the_written_file(real_pipeline: Path) -> None:
    runner.invoke(app, ["discover"])

    result = runner.invoke(app, ["select", "--team", "Potku"])

    written = next((real_pipeline / "index" / "selections").glob("*.json"))
    assert written.name in result.output


def test_the_summary_goes_to_stdout_not_stderr(real_pipeline: Path) -> None:
    """The summary is the command's result; it belongs in the pipeable stream."""
    runner.invoke(app, ["discover"])

    result = runner.invoke(app, ["select", "--team", "Potku"])

    assert "Selection made" in result.stdout


# --- The errors say what to do ---------------------------------------------


def test_a_known_error_is_shown_without_a_traceback(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The user does not code: a traceback on the screen points them nowhere.

    The name used to say the error is in Finnish. AD-11 moved the console
    into English on 2026-09-07, so the claim would now be false.

    **The message is the wording ``discover._lookup_problem`` really raises.**
    T3 and T5 translated it, and the Finnish copy that used to stand here had
    already stopped matching: a literal that drifts makes the test measure its
    own string.
    """
    fake_stage["virhe"] = PappascoutError(
        "The search 'T' hits 3 teams, so a choice has to be made:\n"
        "    TUUHEE (8 players, id faction-04)"
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "select", "--team", "T"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "a choice has to be made" in stderr
    assert "TUUHEE" in stderr


def test_a_missing_index_tells_the_user_to_run_discover(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The I/O matrix: the error says what to do next, not what went wrong.

    The message is ``discover._read``'s own, translated by T5; the Finnish
    copy that used to stand here had stopped matching it.
    """
    fake_stage["virhe"] = PappascoutError(
        "The archive is missing the match index (matches.json).\n"
        "Run first: uv run pappascout discover"
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "select", "--team", "Rcave"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    assert "pappascout discover" in capsys.readouterr().err


def test_a_bad_threshold_is_a_settings_error_not_a_program_error(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``roster_size = 6`` is the user's settings error, not a program fault.

    A bare ``ValueError`` would come out as "Unexpected error -- a program
    fault" with exit code 2, although the fix is in their own settings.toml.
    """
    from conftest import settings_text

    settings_file.write_text(
        settings_text(
            settings_file.parent / "arkisto",
            **{"roster_size = 5": "roster_size = 6"},
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    runner.invoke(app, ["discover"])
    monkeypatch.setattr("sys.argv", ["pappascout", "select", "--team", "Potku"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    assert "settings.toml" in capsys.readouterr().err
