"""CLI:n testit: ``info`` on ensimmäinen ajettava komento.

Kaksi vaatimusta, jotka näissä testeissä lukitaan:

* avaimen **tila** näkyy, avaimen **arvo** ei koskaan, ja
* käyttäjä ei näe koskaan raakaa pinojälkeä -- jokainen virhe tulee ulos
  suomenkielisenä rivinä ja paluukoodina.
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
    tuloste = _render_info(settings)

    # Asetukset
    assert "PotkukelkkaPeek" in tuloste
    assert "de_mirage" in tuloste
    assert "MR12" in tuloste
    assert "12500" in tuloste
    assert "4000" in tuloste
    assert str(settings_file) in tuloste

    # Avaimet: tila kyllä, arvo ei
    assert "FACEIT_API_KEY" in tuloste
    assert "asetettu" in tuloste
    assert FAKE_KEY not in tuloste
    assert FAKE_TOKEN not in tuloste


def test_info_reports_missing_key_without_crashing(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = load_settings(settings_file, env_files=(env,))
    tuloste = _render_info(settings)
    avainrivi = _section(tuloste, "Avaimet")
    assert "FACEIT_API_KEY" in avainrivi
    assert "puuttuu" in avainrivi
    assert FAKE_TOKEN not in tuloste


def test_info_is_finnish(settings_file: Path) -> None:
    settings = load_settings(settings_file, env_files=())
    tuloste = _render_info(settings)
    for sana in ("Asetukset", "Oma joukkue", "Karttapooli", "Arkisto", "Avaimet"):
        assert sana in tuloste


def _section(tuloste: str, otsikko: str) -> str:
    """Poimi yhden osion rivit, jotta assertio ei osu vahingossa toiseen osioon."""
    rivit = tuloste.splitlines()
    alku = rivit.index(otsikko) + 1
    loppu = alku
    while loppu < len(rivit) and rivit[loppu].startswith("  "):
        loppu += 1
    return "\n".join(rivit[alku:loppu])


# --- info: arkiston rivi -----------------------------------------------------


def test_info_reports_missing_archive_precisely(
    tmp_path: Path, settings_file: Path
) -> None:
    """Arkiston puuttuminen näkyy arkisto-osiossa, eikä hakemistoa luoda."""
    settings = load_settings(settings_file, env_files=())
    arkisto_osio = _section(_render_info(settings), "Arkisto")

    puuttuva = tmp_path / "arkisto"
    assert str(puuttuva) in arkisto_osio
    assert "Tila" in arkisto_osio
    assert "puuttuu" in arkisto_osio
    assert "löytyy" not in arkisto_osio
    assert not puuttuva.exists()


def test_info_reports_existing_archive(tmp_path: Path) -> None:
    arkisto = tmp_path / "arkisto"
    (arkisto / "index").mkdir(parents=True)
    (arkisto / "index" / "teams.json").write_bytes(b"12345")

    kohde = tmp_path / "settings.toml"
    kohde.write_text(settings_text(arkisto), encoding="utf-8")
    settings = load_settings(kohde, env_files=())

    osio = _section(_render_info(settings), "Arkisto")
    assert "löytyy" in osio
    assert "puuttuu" not in osio
    # Kokoa ei lasketa ilman --koko.
    assert "ei laskettu" in osio
    assert "tavua" not in osio


def test_info_computes_size_only_when_asked(tmp_path: Path) -> None:
    """NFR-1: info on nopea tilannekatsaus, joten koko on valinnainen."""
    arkisto = tmp_path / "arkisto"
    (arkisto / "index").mkdir(parents=True)
    (arkisto / "index" / "teams.json").write_bytes(b"12345")

    kohde = tmp_path / "settings.toml"
    kohde.write_text(settings_text(arkisto), encoding="utf-8")
    settings = load_settings(kohde, env_files=())

    osio = _section(_render_info(settings, show_size=True), "Arkisto")
    assert "5 tavua" in osio
    assert "ei laskettu" not in osio


def test_koko_flag_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arkisto = tmp_path / "arkisto"
    arkisto.mkdir()
    (arkisto / "x.json").write_bytes(b"1234")
    kohde = tmp_path / "settings.toml"
    kohde.write_text(settings_text(arkisto), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(kohde))

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
    vale = tmp_path / "vale" / ".env"
    monkeypatch.setattr("pappascout.cli.secrets_env_path", lambda: vale)
    settings = load_settings(settings_file, env_files=())
    assert settings.secrets_file is None
    assert str(vale) in _render_info(settings)


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
    virhe = capsys.readouterr().err
    assert virhe.startswith("Virhe:")
    assert "Traceback" not in virhe
    assert "settings.toml" in virhe


def test_main_never_shows_a_traceback_for_a_bug(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Ohjelmavirhe -> paluukoodi 2 ja lyhyt suomenkielinen rivi."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    def raja():
        raise RuntimeError("odottamaton hajoaminen")

    monkeypatch.setattr("pappascout.cli.load_settings", lambda *a, **k: raja())
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_UNEXPECTED_ERROR
    virhe = capsys.readouterr().err
    assert virhe.startswith("Odottamaton virhe:")
    assert "Traceback" not in virhe


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
    "tavut,odotettu",
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
def test_human_size(tavut: int, odotettu: str) -> None:
    assert _human_size(tavut) == odotettu


# --- Rakenne ------------------------------------------------------------------


def test_no_pipeline_stages_exist_yet() -> None:
    """Story 1.1 pystyttää sopimukset -- putken vaiheita ei ole vielä.

    HUOM: tämä testi poistetaan Story 1.2:ssa, kun ``stages.parse`` syntyy.
    Se on tässä vain varmistamassa, ettei tämä story vahingossa toteuttanut
    putkea, jota sen ei pitänyt koskea.
    """
    import importlib

    for moduuli in ("pappascout.stages", "pappascout.adapters"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(moduuli)
