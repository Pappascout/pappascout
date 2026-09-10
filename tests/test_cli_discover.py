"""``pappascout discover`` -- the command's and its summary's tests (Story 3.2).

Three things are locked down here:

* **AD-3 and the layering rule.** The command gives the stage only
  ``settings.league`` and ``settings.thresholds``, and it asks
  ``stages.discover.default_source`` for the match port -- not the adapters.
  If the command built the FACEIT client itself, the
  ``cli -> stages -> adapters`` arrow would turn round and the network would
  be wired into the command line.
* **The summary reports the scope.** The user checks from the output whether
  the whole division shows up and whether the rosters are the right size --
  too small a roster is the only way to notice a missing ``substitutes`` list
  without opening the file.
* **No drop is silent.** A player without an id, a team row without an id, a
  transferred player and a contested lineup are all rows in the output and
  not mere fields in a file.

The stage itself is replaced, so no test in this file goes on the network or
reads the archive.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, _render_discover, app, main
from pappascout.domain.models import SETTINGS_ENV_VAR, LeagueSettings, ThresholdSettings
from pappascout.domain.teams import Team, TeamLookup
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult
from pappascout.stages.discover import _lookup_problem

runner = CliRunner()

RCAVE = (
    "HCNoRage",
    "Kronnennn",
    "Lindberq_",
    "MarkusN",
    "SSStttNNN",
    "bobb_y",
    "pornopertti",
)

DIVISION_ROWS = [
    {
        "team_key": "faction-00",
        "name": "popsiCS",
        "roster_size": 9,
        "matches_played": 6,
    },
    {
        "team_key": "faction-02",
        "name": "PotkukelkkaPeek",
        "roster_size": 8,
        "matches_played": 1,
    },
    {
        "team_key": "f56dd02a",
        "name": "Rcave Veterans",
        "roster_size": 7,
        "matches_played": 1,
    },
]


def discover_result(**overrides) -> StageResult:
    defaults: dict[str, object] = {
        "stage": "discover",
        "unit": "f56dd02a-6107-48e2-abfb-75e7ec7ebcb2",
        "status": "ok",
        "skipped": False,
        "outputs": (
            PurePosixPath("index/matches.json"),
            PurePosixPath("index/teams.json"),
        ),
        "manifest_path": None,
        "reason": None,
        "duration_s": 1.25,
        "stats": {
            "competition_ids": ["94681888-b5da-4ab5-bf50-f44b666b98a3"],
            "matches": 66,
            "matches_played": 6,
            "teams": 12,
            "roster_min": 6,
            "roster_max": 9,
            "teams_without_roster": 0,
            "players_without_steam_id": 0,
            "dropped_players": [],
            "team_rows_without_id": 0,
            "contested_lineup_keys": [],
            "transfers": [],
            "division": DIVISION_ROWS,
            "generated_at": "2026-09-04T12:00:00+00:00",
            "team": {
                "team_key": "f56dd02a-6107-48e2-abfb-75e7ec7ebcb2",
                "faction_ids": ["f56dd02a-6107-48e2-abfb-75e7ec7ebcb2"],
                "name": "Rcave Veterans",
                "alternative_names": [],
                "roster": list(RCAVE),
                "roster_size": 7,
                "released": [],
                "shared_players": [],
                "matches": 11,
                "matches_played": 1,
                "lineup_keys": ["ff03fb54599d3311"],
            },
        },
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


def with_stats(**changes) -> StageResult:
    """A result whose ``stats`` have been changed -- the base stays one place."""
    stats = dict(discover_result().stats)
    stats.update(changes)
    return discover_result(stats=stats)


def with_team(**changes) -> StageResult:
    stats = dict(discover_result().stats)
    stats["team"] = dict(stats["team"], **changes)
    return discover_result(stats=stats)


def field_value(output_text: str, label: str) -> str:
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"there is no {label!r} row in the output:\n{output_text}")


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace the stage and the port; return what the stage was given."""
    seen: dict[str, object] = {}

    def fake_run(league, archive, team, *, source, thresholds, **kwargs):
        seen["league"] = league
        seen["archive"] = archive
        seen["team"] = team
        seen["source"] = source
        seen["thresholds"] = thresholds
        seen["kwargs"] = kwargs
        error = seen.get("virhe")
        if error is not None:
            raise error  # type: ignore[misc]
        return seen.get("tulos") or discover_result()

    def fake_source(settings, archive):
        seen["source_settings"] = settings
        seen["source_archive"] = archive
        return object()

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.discover.run", fake_run)
    monkeypatch.setattr("pappascout.stages.discover.default_source", fake_source)
    return seen


# --- The command gives the stage the right sections ---------------------------


def test_the_command_passes_only_league_and_thresholds(fake_stage: dict) -> None:
    """AD-3: the stage does not see ``[parse]`` and so cannot invalidate parsing."""
    result = runner.invoke(app, ["discover", "--team", "Rcave"])

    assert result.exit_code == 0, result.output
    assert isinstance(fake_stage["league"], LeagueSettings)
    assert isinstance(fake_stage["thresholds"], ThresholdSettings)
    assert fake_stage["team"] == "Rcave"
    assert fake_stage["kwargs"] == {}


def test_the_port_comes_from_the_stage_not_from_the_adapters(fake_stage: dict) -> None:
    """The dependency arrow is ``cli -> stages -> adapters``, not ``cli -> adapters``."""
    result = runner.invoke(app, ["discover"])

    assert result.exit_code == 0, result.output
    assert fake_stage["source"] is not None
    assert fake_stage["source_archive"] is fake_stage["archive"]


def test_the_team_option_is_optional(fake_stage: dict) -> None:
    """Without ``--team`` the indexes are written and the division is listed."""
    result = runner.invoke(app, ["discover"])

    assert result.exit_code == 0, result.output
    assert fake_stage["team"] is None


def test_the_help_names_the_division_and_the_team_option() -> None:
    """The help says what the command fetches and what ``--team`` takes.

    The name of this test used to say the help is in Finnish. AD-11 moved the
    console into English on 2026-09-07, so the claim would now be false; what
    the assertions pin is that the help still names both.
    """
    result = runner.invoke(app, ["discover", "--help"])

    assert result.exit_code == 0
    # Single words: Typer wraps the help into a box, and a two-word needle
    # would break on the wrap rather than on a real change.
    assert "division's" in result.output
    assert "unambiguous" in result.output


def test_there_is_no_force_flag() -> None:
    """There is nothing to force when nothing is ever skipped.

    The check is on the registered options and not on the help text: the help
    **says** why the flag does not exist, so a string search would find it
    there. The claim covers only the absence of ``--force`` -- locking the
    whole option list down would break on every later legitimate addition.
    """
    command = next(c for c in app.registered_commands if c.name == "discover")
    names = [
        parameter
        for value in command.callback.__defaults__ or ()
        for parameter in getattr(value, "param_decls", ())
    ]

    assert "--force" not in names
    assert "--team" in names


# --- The summary --------------------------------------------------------------


def test_the_summary_reports_the_scope_of_the_division() -> None:
    output_text = _render_discover(discover_result())

    assert output_text.startswith("Division fetched: 12 teams, 66 matches")
    assert field_value(output_text, "Matches played") == "6 / 66"
    assert field_value(output_text, "Rosters") == "6-9 players"


def test_the_summary_names_every_player_in_the_standing_roster() -> None:
    """The standing roster is the whole story's result, so it is read here."""
    output_text = _render_discover(discover_result())

    roster = field_value(output_text, "Standing roster")
    assert roster.startswith("7 players:")
    for nickname in RCAVE:
        assert nickname in roster


def test_the_summary_shows_the_bridge_to_the_archive() -> None:
    """The lineup digest says which archive directory this team is."""
    output_text = _render_discover(discover_result())

    assert field_value(output_text, "Archive lineups") == "ff03fb54599d3311"


def test_the_summary_omits_the_bridge_when_there_is_none() -> None:
    assert "Archive lineups" not in _render_discover(with_team(lineup_keys=[]))


def test_the_summary_lists_both_written_indexes() -> None:
    output_text = _render_discover(discover_result())

    assert "index/matches.json" in output_text
    assert "index/teams.json" in output_text


def test_the_summary_lists_the_division_when_no_team_was_asked_for() -> None:
    """From the review: the names could be seen only by causing an error."""
    stats = dict(discover_result().stats)
    del stats["team"]
    output_text = _render_discover(discover_result(stats=stats))

    assert "Standing roster" not in output_text
    assert "The division's teams:" in output_text
    for row in DIVISION_ROWS:
        assert str(row["name"]) in output_text
        assert str(row["team_key"]) in output_text


def test_the_summary_does_not_list_the_division_when_a_team_was_found() -> None:
    """The rows of the team looked up are the answer; the listing would be noise."""
    assert "The division's teams:" not in _render_discover(discover_result())


def test_the_summary_names_other_observed_names() -> None:
    """A change of name is an observation and it is not hidden."""
    output_text = _render_discover(with_team(alternative_names=["Rcave"]))

    assert field_value(output_text, "Other observed names") == "Rcave"


def test_the_summary_says_when_one_team_has_two_source_identifiers() -> None:
    """Identity is the roster: two ids can be the same team."""
    output_text = _render_discover(with_team(faction_ids=["kausi-12", "kausi-13"]))

    assert "kausi-12" in field_value(output_text, "Source ids")
    assert "different seasons" in field_value(output_text, "Source ids")


# --- No drop is silent ----------------------------------------------------------


def test_the_summary_names_the_players_that_were_left_out() -> None:
    """A silent drop would look like nothing but a shorter roster.

    The name is there beside the number so that the user can check whose id
    was missing -- the number alone would be a claim with no way to check it.
    """
    output_text = _render_discover(
        with_stats(
            players_without_steam_id=2,
            dropped_players=[
                {"player_id": "uuid-1", "nickname": "eka", "team": "A"},
                {"player_id": "uuid-2", "nickname": "toka", "team": "B"},
            ],
        )
    )

    value = field_value(output_text, "Without a SteamID64")
    assert value.startswith("2 players")
    assert "eka" in value and "toka" in value


def test_the_summary_is_silent_when_nobody_was_left_out() -> None:
    assert "Without a SteamID64" not in _render_discover(discover_result())


def test_the_summary_counts_team_rows_that_had_no_identifier() -> None:
    """From the review: dropped players were reported, dropped team rows were not."""
    output_text = _render_discover(with_stats(team_rows_without_id=3))

    assert field_value(output_text, "Team rows without id").startswith(
        "3 were skipped"
    )


def test_the_summary_reports_a_player_who_changed_teams() -> None:
    """A transfer changes the roster, so it belongs in the summary."""
    output_text = _render_discover(
        with_stats(
            transfers=[
                {
                    "game_player_id": "76561197977479426",
                    "nickname": "siirtyja",
                    "from_team": "Aakkoset",
                    "kind": "released",
                }
            ]
        )
    )

    assert "siirtyja" in field_value(output_text, "Transferred players")
    assert "Aakkoset" in field_value(output_text, "Transferred players")


def test_the_summary_reports_a_player_two_teams_both_claim() -> None:
    """A dispute is not settled by drawing lots, so it can be read here."""
    output_text = _render_discover(
        with_stats(
            transfers=[
                {
                    "game_player_id": "76561197977479426",
                    "nickname": "kiistelty",
                    "from_team": "Aakkoset",
                    "kind": "shared",
                }
            ]
        )
    )

    assert "kiistelty" in field_value(output_text, "In two teams")


def test_the_summary_reports_a_contested_lineup_key() -> None:
    """A later stage would count the digest twice without knowing it did."""
    output_text = _render_discover(with_stats(contested_lineup_keys=["ff03fb54"]))

    assert "ff03fb54" in field_value(output_text, "Contested lineups")


def test_the_summary_leads_with_the_reason_when_there_is_one() -> None:
    """An empty result in state ``ok`` with no explanation leaves the user guessing.

    **The reason is the wording the stage really raises.** T5 translated
    ``discover``, and the Finnish copy that used to stand here had stopped
    matching the stage months before anyone would have noticed: a literal that
    drifts makes the test measure its own string.
    """
    output_text = _render_discover(
        discover_result(
            reason="No match at all was found in the competition. Check "
            "[league].championship_ids in the settings -- the indexes were "
            "written empty.",
            stats=dict(discover_result().stats, matches=0, teams=0),
        )
    )

    assert field_value(output_text, "Note").startswith("No match at all was found")


def test_the_summary_has_no_reason_line_when_everything_was_found() -> None:
    assert "Note" not in _render_discover(discover_result())


# --- The errors -----------------------------------------------------------------


def ambiguous_error() -> PappascoutError:
    """The same message the stage really raises -- not a hand-written copy.

    A literal would pass even if the stage's wording changed, that is, the
    test would measure its own string and not the program.
    """
    teams = tuple(
        Team(team_key=key, name=name)
        for key, name in (
            ("faction-04", "TUUHEE"),
            ("faction-05", "Takakeno"),
            ("faction-11", "Tankkiluola vilttiketju"),
        )
    )
    lookup = TeamLookup(query="T", teams=teams, matched_by="prefix")
    return PappascoutError(_lookup_problem(lookup, teams))


def test_an_ambiguous_name_lists_the_candidates_and_exits_with_code_one(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Ambiguity is not settled quietly: the run ends and a choice is asked for.

    The name used to say the error is in Finnish. AD-11 moved the console into
    English on 2026-09-07, so the claim would now be false; what the
    assertions pin is the listing, the ids and the exit code.
    """
    fake_stage["virhe"] = ambiguous_error()
    monkeypatch.setattr("sys.argv", ["pappascout", "discover", "--team", "T"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    stderr = capsys.readouterr().err
    for name in ("TUUHEE", "Takakeno", "Tankkiluola vilttiketju"):
        assert name in stderr
    # The id is there because without it two teams of the same name cannot be
    # told apart.
    assert "faction-04" in stderr
