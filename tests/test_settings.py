"""Tests for the settings and the credentials -- rows 5 and 6 of the I/O
matrix.

The tests also make sure that ``settings.toml``'s numbers are the ones
recorded in the PRD's addendum and in the domain research, and that
contradictions between the sections are caught at load time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from conftest import REAL_SETTINGS, settings_text
from pappascout.archive.paths import (
    ARCHIVE_ROOT_ENV_VAR,
    ArchivePaths,
    _UNEXPANDED_VAR,
)
from pappascout.constants import seconds_label
from pappascout.domain.models import (
    AggregateSettings,
    MAX_BUY_WINDOW_SECONDS,
    MAX_SNAPSHOT_SECONDS,
    REMOVED_SETTINGS,
    SETTINGS_ENV_VAR,
    SETTINGS_SECTIONS,
    EconomySettings,
    LeagueSettings,
    ParseSettings,
    ProjectSettings,
    ReportSettings,
    Settings,
    ThresholdSettings,
    load_settings,
    secrets_env_path,
    settings_search_paths,
)
from pappascout.errors import PappascoutError, SettingsError

FAKE_KEY = "kokeiluavain-1234567890"
FAKE_TOKEN = "kokeilutoken-abcdefghij"


def _load(settings_file: Path, env: Path | None = None) -> Settings:
    return load_settings(settings_file, env_files=(env,) if env else ())


def _write_variant(tmp_path: Path, **replacements: str) -> Path:
    """Write a modified settings.toml and return the path."""
    target = tmp_path / "muunnos.toml"
    target.write_text(
        settings_text(tmp_path / "arkisto", **replacements), encoding="utf-8"
    )
    return target


# --- Row 5: the settings are missing ---------------------------------------


def test_missing_settings_file_names_the_path(tmp_path: Path) -> None:
    """settings.toml is missing -> an error that names the path."""
    missing = tmp_path / "settings.toml"
    with pytest.raises(SettingsError) as exc:
        load_settings(missing)
    message = str(exc.value)
    assert str(missing) in message
    assert "was not found" in message


def test_search_lists_every_path_it_tried(
    isolated_cwd: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the file is nowhere, the error lists every path that was tried."""
    # Block the repository's own settings.toml, the last fallback in the
    # search order.
    monkeypatch.setattr(
        "pappascout.domain.models._repo_root", lambda: tmp_path / "ei-repoa"
    )
    with pytest.raises(SettingsError) as exc:
        load_settings()
    message = str(exc.value)
    assert "settings.toml" in message
    assert SETTINGS_ENV_VAR in message
    assert str(isolated_cwd / "settings.toml") in message


def test_repo_root_is_the_last_fallback(isolated_cwd: Path) -> None:
    """The command may be run from any directory -- the repository root is
    the fallback.
    """
    assert REAL_SETTINGS in settings_search_paths()
    assert load_settings().settings_file == REAL_SETTINGS


def test_settings_env_var_has_highest_priority(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, isolated_cwd: Path
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    assert settings_search_paths()[0] == settings_file
    assert load_settings().settings_file == settings_file


def test_broken_toml_says_what_to_fix(tmp_path: Path) -> None:
    broken = tmp_path / "settings.toml"
    broken.write_text("[project\nown_team_name = 1", encoding="utf-8")
    with pytest.raises(SettingsError) as exc:
        load_settings(broken)
    assert "TOML" in str(exc.value)


def test_unknown_section_is_not_silently_ignored(tmp_path: Path) -> None:
    """A typing error in a section name is an error, not a silent default."""
    target = tmp_path / "settings.toml"
    target.write_text(
        settings_text(tmp_path / "arkisto") + "\n[treshholds]\nfoo = 1\n",
        encoding="utf-8",
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "treshholds" in str(exc.value)


def test_unknown_key_inside_a_section_is_rejected(tmp_path: Path) -> None:
    """A typing error in a key name inside a section is caught.

    This is the safety net of Story 1.4's calibration workflow: a threshold
    is adjusted by hand, and a misspelled key would otherwise quietly affect
    nothing -- the user would believe they had adjusted a bound while the
    code still uses the old value.
    """
    target = _write_variant(tmp_path, **{"full_equip_min = 4000": "full_equp_min = 4500"})
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "full_equp_min" in message
    assert "thresholds" in message


def test_secrets_cannot_be_put_in_settings_toml(tmp_path: Path) -> None:
    """A credential does not belong in a versioned file, so it is rejected."""
    target = tmp_path / "settings.toml"
    target.write_text(
        settings_text(tmp_path / "arkisto") + '\nfaceit_api_key = "salainen"\n',
        encoding="utf-8",
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "faceit_api_key" in str(exc.value)


def test_invalid_value_is_reported_with_field_name(tmp_path: Path) -> None:
    target = _write_variant(tmp_path, **{"full_equip_min = 4000": "full_equip_min = -5"})
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "full_equip_min" in str(exc.value)


# --- Contradictions inside and between the sections -------------------------


def test_the_after_win_anomaly_bar_must_stay_below_a_full_buy(
    tmp_path: Path,
) -> None:
    """Otherwise the anomaly bound would swallow the full buy on the branch
    that follows a win.
    """
    target = _write_variant(
        tmp_path,
        **{"anomaly_equip_max_after_win = 2000": "anomaly_equip_max_after_win = 4000"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "anomaly_equip_max_after_win" in str(exc.value)


def test_a_player_counter_above_the_roster_is_refused(tmp_path: Path) -> None:
    """P2: without this ``half`` is unreachable and nothing says so.

    Both of the half-buy's conditions are player counters. If either of them
    wants more players than the lineup has, the condition cannot be met on
    any round -- and every purchase after a loss would be a force or an eco.
    """
    for name in ("armed_players_min", "normal_buy_players_min"):
        target = _write_variant(tmp_path, **{f"{name} = 3": f"{name} = 6"})
        with pytest.raises(SettingsError) as exc:
            load_settings(target)
        assert name in str(exc.value)
        # The comparison is against the players on the server and not against
        # the roster size (Story 2.5's review): a standing roster may hold
        # substitutes, the server may not.
        assert "players on the server" in str(exc.value)


def test_a_threshold_below_the_smallest_loss_bonus_makes_force_unreachable(
    tmp_path: Path,
) -> None:
    """Condition B would pass without a cent of one's own money.

    The retired ``force_money_left_max < force_buy_min`` made sure that a
    half-buy is reachable. What replaces it has to be a guard in both
    directions, and this is one of them: if the threshold is at most the
    smallest loss bonus, every player always meets condition B and no
    purchase after a loss can be a force any more.

    The bound sits between two sections (``[thresholds]`` and
    ``[economy]``), so neither of them can check it alone.
    """
    target = _write_variant(
        tmp_path,
        **{"normal_buy_money_min = 4000": "normal_buy_money_min = 1400"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "normal_buy_money_min" in message
    assert "force" in message


def test_a_threshold_above_the_money_ceiling_makes_a_half_buy_unreachable(
    tmp_path: Path,
) -> None:
    """The same guard the other way round: nobody can ever meet condition B.

    Buying power is clipped to the money ceiling, so a threshold above the
    ceiling is unreachable -- and the half-buy would vanish silently
    altogether.
    """
    target = _write_variant(
        tmp_path,
        **{"normal_buy_money_min = 4000": "normal_buy_money_min = 20000"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "normal_buy_money_min" in message
    assert "max_money" in message


def test_force_buy_min_must_stay_below_a_full_buy(tmp_path: Path) -> None:
    """Otherwise neither a force nor a half-buy could ever be reached."""
    target = _write_variant(
        tmp_path, **{"force_buy_min = 1500": "force_buy_min = 4000"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "force_buy_min" in str(exc.value)


def test_a_retired_threshold_left_in_settings_is_refused(tmp_path: Path) -> None:
    """I/O matrix: ``force_money_max`` is gone, so leaving it in is an error.

    ``extra="forbid"`` turns a half-finished clean-up into a run-time error
    rather than a silent leftover: otherwise the user would believe they were
    adjusting a bound that no longer has a reader.
    """
    target = _write_variant(
        tmp_path,
        **{"force_buy_min = 1500": "force_buy_min = 1500\nforce_money_max = 2500"},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    message = str(exc.value)
    assert "force_money_max" in message
    assert "thresholds" in message


def test_loss_count_min_must_be_below_max(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path, **{"loss_count_min = 0": "loss_count_min = 4"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "loss_count_min" in str(exc.value)


def test_roster_min_regulars_cannot_exceed_roster_size(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path, **{"roster_min_regulars = 4": "roster_min_regulars = 6"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "roster_min_regulars" in str(exc.value)


def test_loss_bonus_steps_must_match_loss_count_max(tmp_path: Path) -> None:
    """The counter indexes the step table directly, so the length has to
    match.
    """
    target = _write_variant(
        tmp_path,
        **{
            "loss_bonus_steps = [1400, 1900, 2400, 2900, 3400]": (
                "loss_bonus_steps = [1400, 1900, 2400]"
            )
        },
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "loss_bonus_steps" in str(exc.value)


def test_loss_bonus_steps_must_ascend(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path,
        **{
            "loss_bonus_steps = [1400, 1900, 2400, 2900, 3400]": (
                "loss_bonus_steps = [1400, 1900, 1900, 2900, 3400]"
            )
        },
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "loss_bonus_steps" in str(exc.value)


def test_regulation_rounds_must_match_mr(tmp_path: Path) -> None:
    """MR12 means 24 regulation rounds -- a contradiction is an error."""
    target = _write_variant(
        tmp_path, **{"regulation_rounds = 24": "regulation_rounds = 30"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "regulation_rounds" in str(exc.value)


def test_pistol_rounds_must_match_mr(tmp_path: Path) -> None:
    """In MR12 the pistol rounds are 1 and 13, not just anything."""
    target = _write_variant(
        tmp_path, **{"pistol_rounds = [1, 13]": "pistol_rounds = [1, 16]"}
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "pistol_rounds" in str(exc.value)


def test_changing_mr_consistently_is_accepted(tmp_path: Path) -> None:
    """The contradiction check does not block a change of league format when
    everything is updated.
    """
    target = _write_variant(
        tmp_path,
        **{
            "mr = 12": "mr = 15",
            "regulation_rounds = 24": "regulation_rounds = 30",
            "pistol_rounds = [1, 13]": "pistol_rounds = [1, 16]",
        },
    )
    s = load_settings(target, env_files=())
    assert s.league.mr == 15
    assert s.thresholds.pistol_rounds == [1, 16]


def test_default_ban_outside_map_pool_is_rejected(tmp_path: Path) -> None:
    target = _write_variant(
        tmp_path,
        **{'own_default_bans = ["de_mirage", "de_dust2"]': 'own_default_bans = ["de_overpass"]'},
    )
    with pytest.raises(SettingsError) as exc:
        load_settings(target)
    assert "own_default_bans" in str(exc.value)


# --- Row 6: the credential is missing ----------------------------------------


def test_missing_api_key_tells_path_and_required_line(
    settings_file: Path, env_file
) -> None:
    """A .env without FACEIT_API_KEY -> the error names the path and the line
    that is needed.
    """
    env = env_file(FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = _load(settings_file, env)

    assert settings.secret_status("FACEIT_API_KEY") == "missing"
    with pytest.raises(SettingsError) as exc:
        settings.require_faceit_api_key()

    message = str(exc.value)
    assert "FACEIT_API_KEY" in message
    assert str(env) in message
    assert "FACEIT_API_KEY=" in message
    # Does not reveal the other values.
    assert FAKE_TOKEN not in message


def test_missing_env_file_falls_back_to_machine_path_in_message(
    settings_file: Path,
) -> None:
    """Without a .env file the error points at the machine's own credential
    file.
    """
    settings = load_settings(settings_file, env_files=())
    with pytest.raises(SettingsError) as exc:
        settings.require_faceit_downloads_token()
    assert str(secrets_env_path()) in str(exc.value)


def test_present_key_is_returned_and_never_repr_ed(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_API_KEY=FAKE_KEY, FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = _load(settings_file, env)

    assert settings.require_faceit_api_key() == FAKE_KEY
    assert settings.require_faceit_downloads_token() == FAKE_TOKEN
    assert settings.secret_status("FACEIT_API_KEY") == "set"

    # The credential must not leak into the log, the output or the
    # serialisation.
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
    assert FAKE_KEY not in str(settings.model_dump())
    assert FAKE_KEY not in settings.model_dump_json()


def test_blank_key_counts_as_missing(settings_file: Path, env_file) -> None:
    env = env_file(FACEIT_API_KEY="   ")
    settings = _load(settings_file, env)
    assert settings.secret_status("FACEIT_API_KEY") == "missing"
    with pytest.raises(SettingsError):
        settings.require_faceit_api_key()


def test_machine_env_wins_over_project_env(settings_file: Path, tmp_path: Path) -> None:
    """The project's .env is only a fallback; the machine's own file wins."""
    project_env = tmp_path / "project.env"
    project_env.write_text("FACEIT_API_KEY=vanha\n", encoding="utf-8")
    machine_env = tmp_path / "machine.env"
    machine_env.write_text(f"FACEIT_API_KEY={FAKE_KEY}\n", encoding="utf-8")

    settings = load_settings(settings_file, env_files=(project_env, machine_env))
    assert settings.require_faceit_api_key() == FAKE_KEY
    assert settings.secrets_file == machine_env


def test_secrets_path_follows_the_machine_home() -> None:
    """The credential file is looked for in the machine's home directory, not
    under the repository.
    """
    path = secrets_env_path()
    assert path.name == ".env"
    assert path.parent.name == ".pappascout"
    assert path.parent.parent == Path.home()


def test_settings_error_is_a_pappascout_error() -> None:
    assert issubclass(SettingsError, PappascoutError)


# --- settings.toml's content: every number traceable ------------------------


def test_sections_are_separate_typed_models(settings_file: Path) -> None:
    """AD-3: a stage gets only its own part, so the sections are models of
    their own.
    """
    s = _load(settings_file)
    assert isinstance(s.project, ProjectSettings)
    assert isinstance(s.league, LeagueSettings)
    assert isinstance(s.parse, ParseSettings)
    assert isinstance(s.thresholds, ThresholdSettings)
    assert isinstance(s.economy, EconomySettings)
    # A section's settings do not see the other sections.
    assert not hasattr(s.parse, "thresholds")
    assert not hasattr(s.thresholds, "parse")


def test_sections_are_frozen(settings_file: Path) -> None:
    """The settings are not changed during a run -- the parameter hash stays
    true.
    """
    s = _load(settings_file)
    with pytest.raises(ValidationError):
        s.thresholds.full_equip_min = 1  # type: ignore[misc]


def test_project_values(settings_file: Path) -> None:
    s = _load(settings_file)
    assert s.project.own_team_name == "PotkukelkkaPeek"
    assert s.project.language == "fi"
    assert s.project.lock_ttl_seconds == 600


#: Path separators and the home shortcut -- everything that is not a name.
_PATH_NOISE = re.compile(r"[\\/~\s]")


def _literal_residue(line: str) -> str:
    """What is left of the ``archive_root`` line once variables and separators go.

    **The guard must not write the forbidden names down.** A list of employers
    and sync products would be exactly the leak this test prevents: Story
    3.10's own check is a ``git grep`` over those names, and a denylist here
    would put every hit straight back into a versioned file. So this looks at
    *shape* rather than names -- and shape is the stronger claim: a path with
    no literal directory name in it cannot name anyone. The repository-wide
    counterpart, which does use names, reads them from outside the repository
    (:mod:`test_public_repo`).

    The environment-reference pattern is **production's own**
    (:data:`pappascout.archive.paths._UNEXPANDED_VAR`), not a local copy. A
    looser local pattern such as ``%[^%]+%`` would disagree with the loader
    about what a variable reference is, and the disagreement is exploitable:
    ``%Some Employer Oy%`` would strip to nothing and pass here while the
    loader treats it as a literal directory name.
    """
    value = line.split("=", 1)[1].strip().strip("'\"")
    return _PATH_NOISE.sub("", _UNEXPANDED_VAR.sub("", value))


def test_real_archive_root_names_no_one() -> None:
    """The versioned path names no employer, no product and no user.

    The repository is public, and this line was the whole of the leak. It
    carries no literal directory name at all: the real path lives in the
    machine's ``PAPPASCOUT_ARCHIVE_ROOT``.

    **Portability is no longer asserted here, because it is no longer this
    line's job.** The old guard demanded ``%USERPROFILE%`` or ``~`` so that
    one committed line would work on two machines with different user names.
    That premise died with this change: the line does not resolve on either
    machine any more -- it is meant to fail -- and portability now comes from
    the variable. What is left of the concern, "no user name written out", is
    covered strictly harder by the residue check below.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    line = next(r for r in text.splitlines() if r.startswith("archive_root"))
    assert _literal_residue(line) == "", (
        "the versioned path holds a literal directory name, which could name "
        "an organisation, a sync client or a user"
    )


def test_the_shipped_archive_root_names_the_environment_variable() -> None:
    r"""The versioned value is a placeholder that fails, not a path that works.

    Without this assertion the default could be neutral but usable -- say
    ``%USERPROFILE%\pappascout-archive`` -- and a fresh clone with no
    variable set would quietly create an **empty** archive in the wrong place
    and look like a success.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    line = next(r for r in text.splitlines() if r.startswith("archive_root"))
    assert f"%{ARCHIVE_ROOT_ENV_VAR}%" in line


def test_the_shipped_settings_fail_loudly_without_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matrix row 2: a fresh clone with no variable stops, in Finnish.

    The message names both the variable and the file the path would otherwise
    live in; without the file the reader would know something is missing but
    not where to fix it.
    """
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)
    settings = load_settings(REAL_SETTINGS, env_files=())

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings(
            settings.project.archive_root, settings.project.demos_root
        )

    message = str(exc.value)
    assert ARCHIVE_ROOT_ENV_VAR in message
    assert "settings.toml" in message


def test_the_environment_variable_reaches_the_shipped_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matrix row 1: with the variable set, the archive resolves from it.

    The pair to the previous test: an implementation that never finds the
    archive at all would also pass a test that only checks the failure.
    """
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "arkisto"))
    settings = load_settings(REAL_SETTINGS, env_files=())
    archive = ArchivePaths.from_settings(
        settings.project.archive_root, settings.project.demos_root
    )
    assert archive.root == tmp_path / "arkisto"


def test_league_values_match_season_13(settings_file: Path) -> None:
    s = _load(settings_file)
    assert s.league.season == 13
    assert s.league.championship_ids == ["94681888-b5da-4ab5-bf50-f44b666b98a3"]
    assert s.league.organizer_id == "1bfc69fa-5a21-4ed9-9ef3-37edbd7210d8"
    assert s.league.map_pool == [
        "de_mirage",
        "de_inferno",
        "de_dust2",
        "de_nuke",
        "de_ancient",
        "de_anubis",
        "de_cache",
    ]
    assert "de_overpass" not in s.league.map_pool  # left Active Duty 2026-07
    assert s.league.own_default_bans == ["de_mirage", "de_dust2"]
    assert s.league.mr == 12
    assert s.league.ot_start_money == 12500


def test_parse_values(settings_file: Path) -> None:
    s = _load(settings_file)
    assert s.parse.snapshot_seconds == [6.0, 15.0, 30.0, 45.0]
    assert s.parse.first_contact_fallback_death is True
    assert "hegrenade" in s.parse.first_contact_exclude_weapons
    # Calibrated in Story 2.9 on all six demos (2,544 explosions): 95.4% fall
    # inside the bound of 256. The bound applies to the explosion only -- the
    # throw's area is read from the thrower themselves. The value is a
    # setting and not code.
    assert s.parse.area_snap_units == 256
    # The point cloud's three measures. They are settings and not code, and
    # their values have been measured (see settings.toml's tables). The
    # weight is 1 and not 2: measured, 1 separates the floors perfectly and
    # covers more.
    assert s.parse.callout_grid_units == 32
    assert s.parse.callout_z_weight == 1.0
    assert s.parse.callout_z_tolerance_units == 72.0


def test_armed_counter_has_no_setting(settings_file: Path) -> None:
    """The equipment counter has no threshold setting in any section.

    Story 1.5's ``armed_player_equip_min`` measured an equipment value, which
    is weapon + armour + grenades as one number. Story 1.6 changed the
    measure to the observation "armour and at least one bought weapon", and
    an observation has no threshold. A setting left behind would be worse
    than a removed one: ``extra="forbid"`` would break the load, but only if
    the key is in the file -- this test also fails when the key comes back
    into the code.
    """
    s = _load(settings_file)
    for section in (ParseSettings, ThresholdSettings, EconomySettings):
        assert "armed_player_equip_min" not in section.model_fields
    for loaded in (s.parse, s.thresholds, s.economy):
        assert not hasattr(loaded, "armed_player_equip_min")


def test_settings_file_has_no_armed_threshold(settings_file: Path) -> None:
    """The key is no longer assigned in ``settings.toml``.

    ``extra="forbid"`` would break the load, so this is in practice a repeat
    of ``_load`` -- but it names the reason: a line left behind would be in
    the production settings file, not in a test's variant. The name may
    appear in the comments: there it says what was removed and why.
    """
    lines = settings_file.read_text(encoding="utf-8").splitlines()
    assigned = [
        line
        for line in lines
        if not line.lstrip().startswith("#")
        and "armed_player_equip_min" in line
    ]
    assert assigned == []


def test_old_settings_file_gets_a_migration_message(tmp_path: Path) -> None:
    """An old ``settings.toml`` says what replaced the key, not merely "not
    valid".

    ``extra="forbid"`` rejects the file in any case, but with a generic
    "unknown key" message: the user would see only that their file is not
    valid. With an archive shared by two machines the other machine usually
    holds an old file, so this is an expected situation and not an exception
    -- and the user does not write code.
    """
    target = _write_variant(
        tmp_path,
        **{"area_snap_units = 256": "area_snap_units = 256\narmed_player_equip_min = 950"},
    )
    with pytest.raises(SettingsError) as exc:
        _load(target)

    message = str(exc.value)
    assert "armed_player_equip_min" in message
    # Names the section, what replaced the key and what to do.
    assert "[parse]" in message
    assert "armour" in message
    assert "Remove the line" in message


def test_the_retired_money_left_threshold_names_its_three_replacements(
    tmp_path: Path,
) -> None:
    """Story 1.10 removed ``force_money_left_max``: the advice says what
    replaced it.

    Three new thresholds in place of one is precisely the kind of change a
    user cannot guess. Without the advice they would see only "unknown key".
    """
    target = _write_variant(
        tmp_path,
        **{"force_buy_min = 1500": "force_buy_min = 1500\nforce_money_left_max = 1000"},
    )
    with pytest.raises(SettingsError) as exc:
        _load(target)

    message = str(exc.value)
    assert "force_money_left_max" in message
    assert "[thresholds]" in message
    for replacement in (
        "normal_buy_money_min",
        "normal_buy_players_min",
        "armed_players_min",
    ):
        assert replacement in message


def test_every_removed_setting_has_an_instruction() -> None:
    """Every removed setting says what to do, not merely that it is gone.

    Empty or evasive advice would be the same as no advice at all.
    """
    assert REMOVED_SETTINGS
    for (section, key), advice in REMOVED_SETTINGS.items():
        assert section in SETTINGS_SECTIONS, section
        assert key and advice.strip(), key
        assert len(advice) > 60, key


def test_adapter_needs_no_armed_setting() -> None:
    """The adapter can be built without the equipment counter's parameters.

    The rule and the weapon list are code (``pappascout.constants``), so the
    adapter has no default that could drift away from a setting.
    """
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    adapter = Demoparser2Adapter()
    assert not hasattr(adapter, "armed_player_equip_min")


def test_threshold_values(settings_file: Path) -> None:
    s = _load(settings_file)
    t = s.thresholds
    assert t.pistol_rounds == [1, 13]
    assert t.regulation_rounds == 24
    # Calibrated 2026-08-29 against kalibrointi-kierrostyypit.md's truth
    # table. These four are locked: the rationale for each value is in
    # settings.toml's comment, and changing a value without changing this
    # test would mean the rationale went unread.
    assert t.full_equip_min == 4000
    assert t.anomaly_equip_max_after_win == 2000
    assert t.force_buy_min == 1500
    # The half-buy's two conditions (Story 1.10). Condition A is a bound the
    # product owner stated, condition B is calibrated against
    # inferno_vs_ryhmaraman rounds 6 and 10.
    assert t.armed_players_min == 3
    assert t.normal_buy_money_min == 4000
    assert t.normal_buy_players_min == 3
    assert t.loss_count_half_start == 1
    assert t.loss_count_min == 0
    assert t.loss_count_max == 4
    assert t.team_identity_min_common == 3
    assert t.small_sample_rounds == 3
    # The anomaly thresholds (Story 2.5). These six are locked on the same
    # grounds as the round-type thresholds: each was measured on eight demos
    # and two teams (``kalibrointi-ct-eteneminen.md``), and changing a value
    # without changing this test would mean the rationale went unread.
    assert t.advance_t_share == 0.80
    assert t.advance_area_min_observations == 20
    assert t.advance_max_sample_s == 30.0
    assert t.advance_min_players == 1
    assert t.crunch_min_players == 2
    assert t.crunch_min_sources == 2
    # The stack rule's three thresholds (Story 2.14), on the same grounds:
    # each was measured on eight demos (``kalibrointi-stack.md``). Four is
    # calibrated and three is not; 1.25 produces the same division into areas
    # on Ancient from all three demos; 2.0 separates Nuke (0.47-0.54) from
    # the other maps (3.70-5.04).
    assert t.stack_min_players == 4
    assert t.stack_group_margin == 1.25
    assert t.stack_site_separation_min == 2.0


@pytest.mark.parametrize(
    "value",
    [
        # The lower bound is a **majority**: 0.5 and below would make the
        # area both sides'.
        -0.01,
        0.0,
        0.5,
        # The upper bound is the definition of a share.
        1.01,
        # NaN would make every comparison false, infinity the opposite.
        float("nan"),
        float("inf"),
    ],
)
def test_an_impossible_t_share_is_refused(value: float) -> None:
    """The threshold says which side an area **belongs to**, not which side
    visits it.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], advance_t_share=value)


def test_the_t_share_bound_itself_is_allowed() -> None:
    """The guard's other branch: just over half is valid."""
    assert (
        ThresholdSettings(pistol_rounds=[1, 13], advance_t_share=0.51).advance_t_share
        == 0.51
    )


def test_a_crunch_looser_than_an_advance_is_allowed() -> None:
    """**Crunch is not a stricter form of an advance**, so there is no
    ordering.

    Review round 1 removed the guard that required
    ``crunch_min_players >= advance_min_players``. The rationale was untrue:
    crunch is stricter about directions and looser about the round type, so
    neither set of hits contains the other (measured: MatureMayhem Anubis
    round 10 is a full buy on which no advance exists at all). The test is
    here so that the guard does not come back by accident.
    """
    limits = ThresholdSettings(
        pistol_rounds=[1, 13],
        advance_min_players=3,
        crunch_min_players=2,
    )
    assert (limits.advance_min_players, limits.crunch_min_players) == (3, 2)


def test_more_sources_than_players_is_refused() -> None:
    """Every source direction needs a player of its own."""
    with pytest.raises(ValidationError, match="Every source direction"):
        ThresholdSettings(
            pistol_rounds=[1, 13],
            crunch_min_players=2,
            crunch_min_sources=3,
        )


def test_a_single_crunch_source_is_refused() -> None:
    """A crunch is by definition arrival from **several** directions.

    At 1 both the rule and the report's reading guide would claim something
    the code no longer requires.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], crunch_min_sources=1)


@pytest.mark.parametrize(
    "key",
    ["advance_min_players", "crunch_min_players", "stack_min_players"],
)
def test_an_anomaly_player_minimum_above_the_server_is_refused(key: str) -> None:
    """Six players in an area is not a stricter threshold but an impossible
    condition.

    The comparison is against **the players on the server** and not against
    the roster size: a standing roster may hold substitutes (measured: seven
    on one team), but there are always five on the server.
    """
    with pytest.raises(ValidationError, match="players on the server"):
        ThresholdSettings(pistol_rounds=[1, 13], **{key: 6})


def test_five_defenders_is_a_valid_stack_threshold() -> None:
    """Five is the rule's genuine extreme and not an impossible condition.

    Measured, ``stack_min_players = 5`` gives 2 rounds out of 66, so the
    guard must not reject it -- six defenders is a different matter.
    """
    limits = ThresholdSettings(pistol_rounds=[1, 13], stack_min_players=5)
    assert limits.stack_min_players == 5


@pytest.mark.parametrize(
    "value",
    [
        # The lower bound is 1.0: below it the "nearer" site could be the
        # further one.
        0.99,
        0.0,
        -1.0,
        # The upper bound stops a value at which every other area would drop
        # out ungrouped.
        10.01,
        float("inf"),
        float("nan"),
    ],
)
def test_an_impossible_group_margin_is_refused(value: float) -> None:
    """The margin's bounds are **in the model** and not only in the rule.

    The same condition is written twice (pydantic and ``site_groups``'s
    ValueError), because the rule is a public function and does not see the
    settings. Without this test only the domain copy would be proved -- and a
    value coming from the settings file would go through.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], stack_group_margin=value)


@pytest.mark.parametrize(
    "value",
    [
        # The lower bound is above 0: at zero the guard would silence no map.
        0.0,
        -1.0,
        # The upper bound: the measured maximum is 5.04, so above 20 would
        # silence them all.
        20.01,
        float("inf"),
        float("nan"),
    ],
)
def test_an_impossible_site_separation_is_refused(value: float) -> None:
    """The separation threshold has a ceiling for the same reason the margin
    has one.

    Raising the threshold does not tighten the rule but silences it: at too
    large a value every map falls silent, and the report would claim "no
    stacks" as an observation although not one demo was examined.
    """
    with pytest.raises(ValidationError):
        ThresholdSettings(
            pistol_rounds=[1, 13], stack_site_separation_min=value
        )


def test_the_measured_stack_thresholds_are_inside_their_bounds() -> None:
    """The guard's other direction: the measured values are valid."""
    limits = ThresholdSettings(
        pistol_rounds=[1, 13],
        stack_group_margin=1.0,
        stack_site_separation_min=20.0,
    )
    assert (limits.stack_group_margin, limits.stack_site_separation_min) == (
        1.0,
        20.0,
    )


def test_a_player_minimum_is_measured_against_the_server_not_the_roster() -> None:
    """A seven-player roster does not make six armed players possible."""
    with pytest.raises(ValidationError, match="players on the server"):
        ThresholdSettings(
            pistol_rounds=[1, 13], roster_size=7, armed_players_min=6
        )


@pytest.mark.parametrize(
    "value",
    [
        # Zero would silence both rules permanently.
        0.0,
        -1.0,
        # The upper bound: the time bound selects among the sample points, so
        # a value above them bounds nothing -- and 300 is a typing error.
        61.0,
        300.0,
        float("nan"),
        float("inf"),
    ],
)
def test_an_impossible_anomaly_time_bound_is_refused(value: float) -> None:
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], advance_max_sample_s=value)


def test_a_time_bound_below_the_first_sample_point_is_refused(
    tmp_path: Path,
) -> None:
    """A cross-check between the sections: the bound would silence both rules.

    The report would then claim "no anomalies" as an observation although not
    one sample point was ever examined. Neither section can check this alone,
    so the check is in ``Settings._check_sections_agree``.
    """
    target = _write_variant(
        tmp_path,
        **{"advance_max_sample_s = 30.0": "advance_max_sample_s = 3.0"},
    )
    with pytest.raises(SettingsError, match="earliest parse.snapshot_seconds"):
        load_settings(target)


def test_a_time_bound_at_the_first_sample_point_is_allowed(
    tmp_path: Path,
) -> None:
    """The guard's other branch: exactly the earliest sample point is
    valid.
    """
    target = _write_variant(
        tmp_path,
        **{"advance_max_sample_s = 30.0": "advance_max_sample_s = 6.0"},
    )
    assert load_settings(target).thresholds.advance_max_sample_s == 6.0


@pytest.mark.parametrize(
    "value,message",
    [
        ([10.0, 5.0], "strictly increasing"),
        ([5.0, 5.0], "strictly increasing"),
        ([0.0], "is not positive"),
        ([-1.0], "is not positive"),
        ([float("nan")], "finite"),
        ([float("inf")], "finite"),
        # Two bounds that look the same in the bucket name.
        ([5.000000001, 5.000000002], "look the same in the bucket name"),
    ],
)
def test_utility_seconds_buckets_are_checked_at_load(
    value: list[float], message: str
) -> None:
    """An unordered or impossible bound would otherwise quietly become an
    empty bucket.
    """
    with pytest.raises(ValidationError, match=message):
        AggregateSettings(utility_seconds_buckets=value)


def test_utility_seconds_buckets_may_be_empty() -> None:
    """Removing a time bucket is a valid choice, not a code change."""
    a = AggregateSettings(utility_seconds_buckets=[])
    assert a.utility_seconds_buckets == []


def test_aggregate_section_is_read_from_the_settings_file(
    settings_file: Path,
) -> None:
    """``[aggregate]`` is a section of its own so that classify does not hash
    it.
    """
    s = _load(settings_file)
    assert s.aggregate.utility_seconds_buckets == [5.0, 10.0, 20.0]
    assert not hasattr(s.thresholds, "utility_seconds_buckets")


def test_aggregate_default_matches_the_settings_file() -> None:
    """The code default must not differ from the settings file.

    An empty default would quietly produce a one-bucket report if the key
    were forgotten from the file -- and nothing would say that the time
    buckets had gone.
    """
    assert AggregateSettings().utility_seconds_buckets == [5.0, 10.0, 20.0]


# --- The pruning rules (Story 2.13) -------------------------------------------


def test_report_section_is_read_from_the_settings_file(
    settings_file: Path,
) -> None:
    """``[report]`` is a section of its own so that aggregate does not hash it.

    The split follows the stage that reads the values (AD-3), and ``render``
    reads these. Written into ``[aggregate]``, adjusting a pruning rule would
    invalidate every aggregation -- that is, a presentation choice would
    force report.json to be computed again although its content does not
    change.
    """
    s = _load(settings_file)
    assert s.report.drop_saturated_equipment_lines is True
    assert s.report.merge_equal_equipment_lines is True
    assert s.report.skip_sample_seconds == []
    assert s.report.max_utility_targets == 2
    assert s.report.max_kill_areas == 3
    assert not hasattr(s.aggregate, "max_kill_areas")
    assert not hasattr(s.thresholds, "max_kill_areas")
    assert "report" in SETTINGS_SECTIONS


def test_report_defaults_match_the_settings_file(settings_file: Path) -> None:
    """The code default must not differ from the settings file.

    Pruning is on by default for four rules and off for one. If the code
    default differed from the file, a forgotten key would prune differently
    from what the file says -- and nothing would say which of the two the
    report came from.
    """
    assert ReportSettings() == _load(settings_file).report


def test_the_late_sample_point_is_off_by_default() -> None:
    """Rule 3 is a measurement result: 45 s is not repetition but a thin
    observation.

    Measured on all eight demos: the 45 s point describes 53% of the team and
    exists on 285/354 round halves. It is skewed, but the product owner's
    analyses hold late-round observations, so removing it can cost content --
    the setting exists, the default is to keep it.
    """
    assert ReportSettings().skip_sample_seconds == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([float("nan")], "finite"),
        ([float("inf")], "finite"),
        ([0.0], "positive"),
        ([-45.0], "positive"),
        ([45.0, 45.0], "twice"),
        ([45.000000001, 45.000000002], "twice"),
    ],
)
def test_skip_sample_seconds_are_checked_at_load(
    value: list[float], message: str
) -> None:
    """A value that cannot match a sample point is a typing error.

    Without the check the setting would look as though it removed a row while
    removing nothing, and noticing that from the report is hard: the row is
    where it always was.
    """
    with pytest.raises(ValidationError, match=message):
        ReportSettings(skip_sample_seconds=value)


def test_a_sample_point_outside_the_parse_setting_is_allowed() -> None:
    """The sections do not see each other (AD-3), and this is no
    contradiction.

    The report is also typeset from old report.json files, whose sample
    points are the ones that were in use at the time of parsing. Rejecting it
    would require ``[report]`` to know ``[parse]`` -- and would turn
    rendering an old aggregation into an error.
    """
    assert ReportSettings(skip_sample_seconds=[7.5]).skip_sample_seconds == [7.5]


@pytest.mark.parametrize("key", ["max_utility_targets", "max_kill_areas"])
def test_a_negative_pruning_limit_is_refused(key: str) -> None:
    """``0`` is "no limit"; a negative value means nothing."""
    with pytest.raises(ValidationError):
        ReportSettings(**{key: -1})


@pytest.mark.parametrize("key", ["max_utility_targets", "max_kill_areas"])
def test_a_zero_pruning_limit_means_no_limit(key: str) -> None:
    """Switching a rule off is a valid choice, not a code change."""
    assert getattr(ReportSettings(**{key: 0}), key) == 0


def test_the_sample_point_list_is_ordered_at_load(tmp_path: Path) -> None:
    """Point G1: the order must not change the parameter hash.

    ``render`` hashes its section whole, and ``[45, 15]`` produces a report
    that is character for character the same as ``[15, 45]``. Unsorted, the
    manifest would claim that two identical reports came from different
    parameters -- that is, it would report a difference that does not exist.
    """
    assert ReportSettings(skip_sample_seconds=[45.0, 15.0]).skip_sample_seconds == [
        15.0,
        45.0,
    ]
    target = _write_variant(
        tmp_path, **{"skip_sample_seconds = []": "skip_sample_seconds = [45.0, 15.0]"}
    )
    assert _load(target).report.skip_sample_seconds == [15.0, 45.0]


def test_the_validator_and_the_report_share_one_seconds_format() -> None:
    """Point H9: two layers, one formatting.

    The load-time check "two values would look the same on the row" and the
    row's label are the same function (``constants.seconds_label``). As two
    copies they would agree today only: adding one decimal to the row would
    make two setting values the same row without validation noticing.

    The error text prints the same form as the report, that is, a decimal
    comma.
    """
    assert seconds_label(45.5) == "45,5"
    with pytest.raises(ValidationError, match="45,5"):
        ReportSettings(skip_sample_seconds=[-45.5])
    with pytest.raises(ValidationError, match="twice"):
        ReportSettings(skip_sample_seconds=[45.0, 45.0000001])


def test_a_missing_section_is_a_finnish_error_that_says_what_to_do(
    tmp_path: Path,
) -> None:
    """Point G2: ``report: Field required`` guides the reader nowhere.

    A section being required is the project's decision and not a shortcoming
    -- every setting is written out -- so what has to be fixed is the
    **message**. An archive shared by two machines makes this an ordinary
    situation: the repository's ``settings.toml`` updates from git, and a
    pull that was missed looks exactly like this.
    """
    text = settings_text(tmp_path / "arkisto")
    lines = text.splitlines(keepends=True)
    kept, skip = [], False
    for line in lines:
        if line.startswith("[report]"):
            skip = True
        elif line.startswith("[economy]"):
            skip = False
        if not skip:
            kept.append(line)
    target = tmp_path / "ilman-reporttia.toml"
    target.write_text("".join(kept), encoding="utf-8")

    with pytest.raises(SettingsError) as exc:
        _load(target)
    message = str(exc.value)
    assert "[report]" in message
    assert "is missing a section" in message
    assert "git show HEAD:settings.toml" in message
    assert "Field required" not in message


def test_a_misspelled_pruning_rule_is_not_silently_ignored(
    tmp_path: Path,
) -> None:
    """A typing error in a setting's name must not leave the rule quietly
    on.
    """
    target = _write_variant(tmp_path, **{"max_kill_areas = 3": "max_kill_area = 3"})
    with pytest.raises(SettingsError, match="max_kill_area"):
        _load(target)


def test_economy_values(settings_file: Path) -> None:
    s = _load(settings_file)
    e = s.economy
    assert e.loss_bonus_steps == [1400, 1900, 2400, 2900, 3400]
    assert e.win_reward_elimination == 3250
    assert e.win_reward_bomb == 3500
    # The CS2 value, not CS:GO's 800.
    assert e.plant_bonus_loss == 600
    assert e.ct_kill_bonus == 50
    # Contradiction settled: M4A4 2900, not 3100.
    assert e.prices["m4a4"] == 2900
    assert e.prices["ak47"] == 2700
    assert e.prices["mp9"] == 1250
    assert e.prices["mac10"] == 1050
    assert e.kill_rewards["awp"] == 100


def test_loss_bonus_step_matches_half_start(settings_file: Path) -> None:
    """The second half starts from loss count 1 -> a lost pistol round yields
    $1,900.
    """
    s = _load(settings_file)
    steps = s.economy.loss_bonus_steps
    assert steps[s.thresholds.loss_count_half_start] == 1900
    assert len(steps) == s.thresholds.loss_count_max + 1


def test_settings_env_var_pointing_nowhere_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The environment variable is an order, not a suggestion.

    A silent return to the working directory would read settings other than
    the ones the user asked for -- and in calibration that would mean the
    adjusted value affects nothing.
    """
    missing = tmp_path / "ei-ole.toml"
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(missing))
    with pytest.raises(SettingsError) as exc:
        load_settings()
    message = str(exc.value)
    assert SETTINGS_ENV_VAR in message
    assert str(missing) in message


# --- The buy window (Story 1.9) -----------------------------------------------


def test_buy_window_is_the_games_own_rule(settings_file: Path) -> None:
    """The buy window is 20 s, that is, CS2's own buy time from the start of
    the round.

    The value is **a decision that measurement supports**, not a calibrated
    threshold, so it must not drift with the data. Measured on five league
    demos (106 rounds), buying stopped by 19.4 s at the latest, which is
    consistent with a 20-second buy time -- but 19.4 is not the number that
    belongs here, and the measurement cannot say what would have happened at
    20.5 s.
    """
    s = _load(settings_file)
    assert s.parse.buy_window_seconds == 20.0


def test_a_negative_buy_window_is_refused(tmp_path: Path) -> None:
    """A negative window would move the measuring point into freezetime.

    The economy would then be read from a moment at which the team has not
    even bought yet, and nothing would break -- the numbers would simply be
    quietly too small.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = -1.0"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "buy_window_seconds" in str(exc.value)


def test_a_buy_window_longer_than_a_round_is_refused(tmp_path: Path) -> None:
    """A window longer than a round is a typing error, not a choice.

    The measuring point is bounded by the end of the round in any case, so
    the value would measure nothing new -- it would merely make the settings
    file look as though the buy time lasted the whole round.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = 200.0"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "buy_window_seconds" in str(exc.value)


def test_a_zero_buy_window_is_allowed(tmp_path: Path) -> None:
    """Zero is valid: it means "measure from the end of freezetime".

    It is the behaviour that preceded Story 1.9 and the only way to reproduce
    the old measurement without a code change.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = 0.0"}
    )
    assert _load(path).parse.buy_window_seconds == 0.0


@pytest.mark.parametrize("literal", ["nan", "inf", "-inf"])
def test_a_non_finite_buy_window_is_refused(tmp_path: Path, literal: str) -> None:
    """``nan`` and infinity must not slip between the comparisons.

    TOML understands these literals, and ``nan`` passes every comparison:
    ``nan < 0`` is false and ``nan > bound`` is false. A check made only of
    comparisons would let the value through, and ``round(nan * tick_rate)``
    would break only inside the parsing -- after the 400 MB demo has already
    been decompressed and read. Infinity would break at the same place.
    """
    path = _write_variant(
        tmp_path,
        **{"buy_window_seconds = 20.0": f"buy_window_seconds = {literal}"},
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    message = str(exc.value)
    assert "buy_window_seconds" in message
    assert "finite" in message


def test_the_buy_window_bound_is_its_own_not_the_snapshot_bound() -> None:
    """The buy window's upper bound is a constant of its own, not the sample
    points' bound.

    A shared constant would couple two independent settings: adjusting either
    would move the other's bound unnoticed. The sample points' bound is the
    length of a round (115 s), under which, for example, 100 s would go
    through -- a value at which the measuring point would no longer be the
    buy time but an arbitrary moment in the middle of the round.
    """
    assert MAX_BUY_WINDOW_SECONDS < MAX_SNAPSHOT_SECONDS


def test_a_window_below_the_round_length_but_above_the_bound_is_refused(
    tmp_path: Path,
) -> None:
    """100 s is shorter than a round but still too much -- and it does not go
    through silently.
    """
    path = _write_variant(
        tmp_path, **{"buy_window_seconds = 20.0": "buy_window_seconds = 100.0"}
    )
    assert 100.0 < MAX_SNAPSHOT_SECONDS
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "buy_window_seconds" in str(exc.value)


# --- The point cloud's measures (Story 2.9) --------------------------------


@pytest.mark.parametrize("literal", ["0", "-32"])
def test_a_non_positive_grid_size_is_refused(tmp_path: Path, literal: str) -> None:
    """A cell of size zero is not a grid but a division by zero.

    A cell's index is ``floor(x / edge)``, so zero would break in the middle
    of parsing a 400 MB demo -- and a negative value would turn the grid
    inside out without anything breaking.
    """
    path = _write_variant(
        tmp_path, **{"callout_grid_units = 32": f"callout_grid_units = {literal}"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "callout_grid_units" in str(exc.value)


@pytest.mark.parametrize(
    "key", ["callout_z_weight", "callout_z_tolerance_units"]
)
def test_a_negative_weighting_parameter_is_refused(tmp_path: Path, key: str) -> None:
    """A negative weight or tolerance would turn the distance inside out.

    The weighting is ``max(0, |dz| - tolerance) * weight``: a negative weight
    would make the vertical difference a reward, and a negative tolerance
    would penalise a vertical difference that does not exist. Neither would
    break -- the area would simply be quietly wrong.
    """
    current = {"callout_z_weight": "1.0", "callout_z_tolerance_units": "72"}[key]
    path = _write_variant(
        tmp_path, **{f"{key} = {current}": f"{key} = -1.0"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert key in str(exc.value)


@pytest.mark.parametrize(
    "key", ["callout_z_weight", "callout_z_tolerance_units"]
)
@pytest.mark.parametrize("literal", ["nan", "inf"])
def test_a_non_finite_weighting_parameter_is_refused(
    tmp_path: Path, key: str, literal: str
) -> None:
    """``nan`` and infinity must not slip between the comparisons.

    The distance is multiplied by the weight and compared against a
    threshold. ``nan`` would make every comparison false, so **not one**
    explosion would get an area -- and the result would look exactly like a
    demo whose point cloud stayed empty. Infinity would do the same the other
    way round.
    """
    current = {"callout_z_weight": "1.0", "callout_z_tolerance_units": "72"}[key]
    path = _write_variant(tmp_path, **{f"{key} = {current}": f"{key} = {literal}"})
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert key in str(exc.value)


def test_the_threshold_is_a_required_setting(tmp_path: Path) -> None:
    """The threshold is not optional: without it coverage would always be
    100%.

    The nearest cell in the point cloud is ALWAYS found, so a run without a
    threshold would give every explosion an area regardless of distance. The
    spec's Always rule is "the distance threshold stays", and a required
    field makes that structural -- not something a deleted line could quietly
    undo.
    """
    text = settings_text(tmp_path / "arkisto")
    without = "\n".join(
        line for line in text.splitlines()
        if not line.startswith("area_snap_units")
    )
    target = tmp_path / "ilman.toml"
    target.write_text(without, encoding="utf-8")
    with pytest.raises(SettingsError) as exc:
        _load(target)
    assert "area_snap_units" in str(exc.value)


@pytest.mark.parametrize("literal", ["1", "4", "2048"])
def test_a_grid_size_outside_the_bounds_is_refused(
    tmp_path: Path, literal: str
) -> None:
    """The bounds are performance, not a matter of taste.

    The nearest-cell search is a cross product between the points and the
    cells: an edge of 1 would produce of the order of a million cells from
    one demo. The upper bound in turn is the point at which the grid no
    longer tells the map's areas apart.
    """
    path = _write_variant(
        tmp_path, **{"callout_grid_units = 32": f"callout_grid_units = {literal}"}
    )
    with pytest.raises(SettingsError) as exc:
        _load(path)
    assert "callout_grid_units" in str(exc.value)


def test_the_grid_bounds_themselves_are_allowed(tmp_path: Path) -> None:
    """The bounds are inclusive: 8 and 1024 are valid, 4 and 2048 are not."""
    for literal in ("8", "1024"):
        path = _write_variant(
            tmp_path,
            **{"callout_grid_units = 32": f"callout_grid_units = {literal}"},
        )
        assert _load(path).parse.callout_grid_units == int(literal)


def test_a_zero_z_weight_is_allowed(tmp_path: Path) -> None:
    """Zero is a valid choice: it means "do not look at height".

    On a flat map it is the right answer, and it is the only way to reproduce
    the unweighted measurement without a code change.
    """
    path = _write_variant(
        tmp_path, **{"callout_z_weight = 1.0": "callout_z_weight = 0.0"}
    )
    assert _load(path).parse.callout_z_weight == 0.0


def test_the_shipped_settings_keep_demos_in_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demos go into the archive, and that is a decision and not a
    missing value.

    Decided 2026-09-05 after new information. Two grounds:

    1. **The archive follows from one machine to the other**, because it is
       in a synchronised folder. Demos in a local folder do not follow, and
       on the other machine they would be fetched from FACEIT again -- which
       works for about 30 days only.
    2. **A synchronised folder frees a parsed demo's space without deleting
       the file.** In a local folder freeing that space is a final deletion.

    The claim is about the **versioned settings file**, and that is the same
    place the decision would live if it were reversed: taking the line out of
    the comments switches the mode, and this test then says so.
    """
    # The versioned ``archive_root`` is a placeholder that fails without the
    # environment variable (Story 3.10). This test's claim is about
    # ``demos_root``, so point the archive at tmp_path -- never at the
    # machine's own.
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "arkisto"))

    settings = load_settings(REAL_SETTINGS)
    assert settings.project.demos_root is None

    archive = ArchivePaths.from_settings(
        settings.project.archive_root, settings.project.demos_root
    )
    assert archive.demos_dir() == archive.root / "demos"
    assert archive.demos_dir() == archive.archive_demos_dir()


def test_the_demos_root_setting_stays_documented_in_the_shipped_file() -> None:
    """The commented-out line is guidance, not a leftover.

    The setting is a supported mode for when disk space runs out on a machine
    where the cloud is not an option. If the line vanished from the file, the
    only way to find it would be to read the source -- and the user does not
    write code.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    assert "# demos_root = " in text
    # The rationale is beside the line and not in someone's memory -- and it
    # is described as a property rather than as a product name, because the
    # repository is public. Keeping the product name out of the file is the
    # repository-wide guard's business (tests/test_public_repo.py): the name
    # does not belong in this file either.
    assert "POISTAMATTA tiedostoa" in text
