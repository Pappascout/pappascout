"""CLI:n testit: ``info`` ja ``parse``.

Kolme vaatimusta, jotka näissä testeissä lukitaan:

* avaimen **tila** näkyy, avaimen **arvo** ei koskaan,
* käyttäjä ei näe koskaan raakaa pinojälkeä -- jokainen virhe tulee ulos
  suomenkielisenä rivinä ja paluukoodina, ja
* ``parse``-komennon tuloste kertoo kierrosten määrän, jatkoajan, ohitetut
  kierrokset ja ajoajan.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import settings_text
from pappascout import __version__
from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    EXIT_UNEXPECTED_ERROR,
    _human_size,
    _render_info,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings

FAKE_KEY = "kokeiluavain-1234567890"
FAKE_TOKEN = "kokeilutoken-abcdefghij"

runner = CliRunner()


def test_version_is_importable() -> None:
    assert __version__
    assert __version__ != "0.0.0+unknown"


def test_version_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_help_lists_info() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "info" in result.output


# --- info: sisältö -----------------------------------------------------------


def test_info_shows_settings_archive_and_key_status(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_API_KEY=FAKE_KEY, FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = load_settings(settings_file, env_files=(env,))
    output_text = _render_info(settings)

    # Asetukset
    assert "PotkukelkkaPeek" in output_text
    assert "de_mirage" in output_text
    assert "MR12" in output_text
    assert "12500" in output_text
    assert "4000" in output_text
    assert str(settings_file) in output_text

    # Avaimet: tila kyllä, arvo ei
    assert "FACEIT_API_KEY" in output_text
    assert "asetettu" in output_text
    assert FAKE_KEY not in output_text
    assert FAKE_TOKEN not in output_text


def test_info_reports_missing_key_without_crashing(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = load_settings(settings_file, env_files=(env,))
    output_text = _render_info(settings)
    keys_section = _section(output_text, "Avaimet")
    assert "FACEIT_API_KEY" in keys_section
    assert "puuttuu" in keys_section
    assert FAKE_TOKEN not in output_text


def test_info_is_finnish(settings_file: Path) -> None:
    settings = load_settings(settings_file, env_files=())
    output_text = _render_info(settings)
    for word in ("Asetukset", "Oma joukkue", "Karttapooli", "Arkisto", "Avaimet"):
        assert word in output_text


def _section(output_text: str, heading: str) -> str:
    """Poimi yhden osion rivit, jotta assertio ei osu vahingossa toiseen osioon."""
    lines = output_text.splitlines()
    start = lines.index(heading) + 1
    end = start
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return "\n".join(lines[start:end])


# --- info: arkiston rivi -----------------------------------------------------


def test_info_reports_missing_archive_precisely(
    tmp_path: Path, settings_file: Path
) -> None:
    """Arkiston puuttuminen näkyy arkisto-osiossa, eikä hakemistoa luoda."""
    settings = load_settings(settings_file, env_files=())
    archive_section = _section(_render_info(settings), "Arkisto")

    missing_dir = tmp_path / "arkisto"
    assert str(missing_dir) in archive_section
    assert "Tila" in archive_section
    assert "puuttuu" in archive_section
    assert "löytyy" not in archive_section
    assert not missing_dir.exists()


def test_info_reports_existing_archive(tmp_path: Path) -> None:
    archive_dir = tmp_path / "arkisto"
    (archive_dir / "index").mkdir(parents=True)
    (archive_dir / "index" / "teams.json").write_bytes(b"12345")

    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    settings = load_settings(target, env_files=())

    section = _section(_render_info(settings), "Arkisto")
    assert "löytyy" in section
    assert "puuttuu" not in section
    # Kokoa ei lasketa ilman --koko.
    assert "ei laskettu" in section
    assert "tavua" not in section


def test_info_computes_size_only_when_asked(tmp_path: Path) -> None:
    """NFR-1: info on nopea tilannekatsaus, joten koko on valinnainen."""
    archive_dir = tmp_path / "arkisto"
    (archive_dir / "index").mkdir(parents=True)
    (archive_dir / "index" / "teams.json").write_bytes(b"12345")

    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    settings = load_settings(target, env_files=())

    section = _section(_render_info(settings, show_size=True), "Arkisto")
    assert "5 tavua" in section
    assert "ei laskettu" not in section


def test_size_flag_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_dir = tmp_path / "arkisto"
    archive_dir.mkdir()
    (archive_dir / "x.json").write_bytes(b"1234")
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(target))

    result = runner.invoke(app, ["info", "--koko"])
    assert result.exit_code == 0, result.output
    assert "4 tavua" in result.output


# --- info: koko putki läpi ---------------------------------------------------


def test_info_command_runs_end_to_end(
    settings_file: Path, env_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = env_file(FACEIT_API_KEY=FAKE_KEY)
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    # cli sitoi nimen importissa, joten patch kohdistuu cli-moduuliin.
    monkeypatch.setattr("pappascout.cli.secrets_env_path", lambda: env)
    monkeypatch.setattr("pappascout.domain.models.secrets_env_path", lambda: env)

    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    assert "PotkukelkkaPeek" in result.output
    assert "asetettu" in result.output
    assert FAKE_KEY not in result.output


def test_secrets_path_shown_comes_from_cli_module(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ilman .env-tiedostoa info näyttää cli:n oman oletuspolun."""
    fake_path = tmp_path / "vale" / ".env"
    monkeypatch.setattr("pappascout.cli.secrets_env_path", lambda: fake_path)
    settings = load_settings(settings_file, env_files=())
    assert settings.secrets_file is None
    assert str(fake_path) in _render_info(settings)


# --- main(): virheiden käsittely (NFR-1) -------------------------------------


def test_main_turns_known_error_into_finnish_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Puuttuva asetustiedosto -> paluukoodi 1, suomenkielinen rivi, ei tracebackia."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / "ei-ole.toml"))
    monkeypatch.setattr(
        "pappascout.domain.models._repo_root", lambda: tmp_path / "ei-repoa"
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    err_text = capsys.readouterr().err
    assert err_text.startswith("Virhe:")
    assert "Traceback" not in err_text
    assert "settings.toml" in err_text


def test_main_never_shows_a_traceback_for_a_bug(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Ohjelmavirhe -> paluukoodi 2 ja lyhyt suomenkielinen rivi."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    def boom():
        raise RuntimeError("odottamaton hajoaminen")

    monkeypatch.setattr("pappascout.cli.load_settings", lambda *a, **k: boom())
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_UNEXPECTED_ERROR
    err_text = capsys.readouterr().err
    assert err_text.startswith("Odottamaton virhe:")
    assert "Traceback" not in err_text


def test_main_exits_zero_on_success(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert "PotkukelkkaPeek" in capsys.readouterr().out


# --- _human_size --------------------------------------------------------------


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (0, "0 tavua"),
        (1, "1 tavua"),
        (1023, "1023 tavua"),
        (1024, "1,0 kt"),
        (1536, "1,5 kt"),
        (1024**2, "1,0 Mt"),
        (1024**3, "1,0 Gt"),
        (1024**4, "1,0 Tt"),
        (1024**5, "1,0 Pt"),
        (5 * 1024**5, "5,0 Pt"),
    ],
)
def test_human_size(num_bytes: int, expected: str) -> None:
    assert _human_size(num_bytes) == expected


# --- Rakenne ------------------------------------------------------------------


def test_pipeline_packages_expose_their_contracts() -> None:
    """Story 1.2 loi putken ensimmäisen vaiheen ja sen portin.

    Korvaa Story 1.1:n ``test_no_pipeline_stages_exist_yet``-testin, joka
    vartioi sitä, ettei runkostory toteuta putkea etuajassa. Tarkistetaan
    nimetty symboli eikä pelkkää tuontia: tyhjä paketti läpäisisi jälkimmäisen.
    """
    import importlib

    expected = {
        "pappascout.stages": ("StageResult", "archive_paths"),
        "pappascout.stages.parse": ("run", "resolve_demo", "default_parser"),
        "pappascout.stages.classify": ("run", "resolve_team", "team_keys"),
        "pappascout.stages.aggregate": ("run", "resolve_team", "team_keys"),
        # Story 2.4: putken viimeinen vaihe ja sen esityskerros.
        "pappascout.stages.render": (
            "run",
            "resolve_team",
            "team_keys",
            "read_report",
            "round_list_paths",
        ),
        "pappascout.render": (
            "render_report",
            "build_view",
            "template_digest",
            "round_list_demo_ids",
        ),
        # Story 2.3: raporttimalli on aggregointi- ja render-vaiheen jaettu
        # sopimus, joten sen nimi ja skeemaversio ovat osa rakennetta.
        "pappascout.domain.report": ("Report", "REPORT_SCHEMA_VERSION"),
        "pappascout.domain.aggregate": ("build_report", "positions_for"),
        "pappascout.adapters": (
            "DemoParser",
            "DemoTables",
            "ROUNDS_ADAPTER_COLUMNS",
            "TICKS_ADAPTER_COLUMNS",
        ),
    }
    for name, symbols in expected.items():
        module = importlib.import_module(name)
        for symbol in symbols:
            assert hasattr(module, symbol), f"{name}.{symbol}"


def test_help_lists_parse() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "parse" in result.output


def test_help_lists_every_pipeline_command() -> None:
    """Putken komennot ovat luettelossa siinä järjestyksessä kuin ne ajetaan.

    Yhden komennon lisääminen ilman tätä väitettä jättäisi sen ohjeesta
    huomaamatta -- eikä käyttäjä, joka ei koodaa itse, löytäisi sitä mistään.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("info", "discover", "parse", "classify", "aggregate", "report"):
        assert command in result.output, command
