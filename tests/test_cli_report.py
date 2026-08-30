"""``pappascout report`` -- komennon ja sen yhteenvedon testit.

Sama kuvio kuin ``test_cli_parse``, ``test_cli_classify`` ja
``test_cli_aggregate``: komennon tuloste ja sen sopimus vaiheen kanssa
testataan täällä, vaiheen tiedosto-operaatiot ``test_stage_render``issä.

Kaksi asiaa lukitaan:

* **Ensimmäinen rivi on tiedoston polku.** Käyttäjä avaa raportin
  seuraavaksi; joukkuetunniste on se, jonka hän juuri itse kirjoitti
  komentoriville.
* **Komennossa ei ole ``--pakota``a.** Aikaleimattu nimi tekee siitä
  tarpeettoman, eikä valintaa saa lisätä vahingossa takaisin.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from typer.testing import CliRunner

from conftest import settings_text
from pappascout.cli import _render_report, app
from pappascout.domain.models import SETTINGS_ENV_VAR
from pappascout.stages import StageResult
from test_render import TEAM_KEY, pistol_map, report
from test_stage_render import build_archive

runner = CliRunner()

REPORT_FILE = f"reports/{TEAM_KEY}/2026-08-30T0307-{TEAM_KEY}.md"


def render_result(**overrides) -> StageResult:
    defaults: dict[str, object] = {
        "stage": "render",
        "unit": TEAM_KEY,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath(REPORT_FILE),),
        "manifest_path": PurePosixPath(
            f"reports/{TEAM_KEY}/2026-08-30T0307-{TEAM_KEY}.manifest.json"
        ),
        "duration_s": 0.12,
        "stats": {
            "team_key": TEAM_KEY,
            "team_name_known": True,
            "demos": 4,
            "rounds": 85,
            "maps": ["de_nuke", "de_ancient"],
            "missing_demos": 0,
            "unclassified": 0,
            "lines": 367,
            "characters": 32981,
        },
    }
    stats = dict(defaults["stats"])  # type: ignore[arg-type]
    stats.update(overrides.pop("stats", {}))
    defaults.update(overrides)
    defaults["stats"] = stats
    return StageResult(**defaults)  # type: ignore[arg-type]


# --- Tuloste --------------------------------------------------------------------


def test_the_first_line_is_the_path_of_the_written_file() -> None:
    """Tärkein rivi on tuloksen polku, ei 16 merkin tiiviste."""
    text = _render_report(render_result())
    assert text.splitlines()[0] == f"Raportti kirjoitettu: {REPORT_FILE}"


def test_the_output_names_the_maps_and_the_sample() -> None:
    text = _render_report(render_result())
    assert "4 demoa, 85 kierrosta" in text
    assert "de_nuke, de_ancient" in text
    assert "367 riviä" in text


def test_the_output_says_when_the_team_name_is_unknown() -> None:
    text = _render_report(render_result(stats={"team_name_known": False}))
    assert "joukkueen nimi ei tiedossa" in text


def test_a_known_team_name_gets_no_caveat() -> None:
    assert "ei tiedossa" not in _render_report(render_result())


def test_the_output_flags_missing_demos_and_unclassified_rounds() -> None:
    text = _render_report(render_result(stats={"missing_demos": 2, "unclassified": 3}))
    assert "Puuttuvat demot" in text
    assert "Luokittelemattomat" in text


def test_a_clean_run_mentions_neither() -> None:
    text = _render_report(render_result())
    assert "Puuttuvat demot" not in text
    assert "Luokittelemattomat" not in text


def test_the_output_survives_a_report_without_maps() -> None:
    text = _render_report(render_result(stats={"maps": []}))
    assert "ei yhtään karttaa" in text


def test_the_manifest_is_named_but_not_first() -> None:
    text = _render_report(render_result())
    assert "Manifesti" in text
    assert not text.startswith("  Manifesti")


# --- Komento kokonaisuutena -----------------------------------------------------


def prepare(tmp_path: Path, settings_file: Path, monkeypatch) -> Path:
    archive_root = tmp_path / "arkisto"
    settings_file.write_text(settings_text(archive_root), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    build_archive(tmp_path)
    return archive_root


def test_report_command_runs_end_to_end(
    tmp_path: Path, settings_file: Path, monkeypatch
) -> None:
    archive_root = prepare(tmp_path, settings_file, monkeypatch)

    result = runner.invoke(app, ["report", "--team", TEAM_KEY[:6]])
    assert result.exit_code == 0, result.output
    assert "Raportti kirjoitettu" in result.output

    written = list((archive_root / "reports" / TEAM_KEY).glob("*.md"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8").startswith("# MatureMayhem")
    assert written[0].name in result.output


def test_report_command_without_a_team_lists_the_teams(
    tmp_path: Path, settings_file: Path, monkeypatch
) -> None:
    prepare(tmp_path, settings_file, monkeypatch)
    result = runner.invoke(app, ["report"])
    assert result.exit_code != 0
    assert TEAM_KEY in str(result.exception)


def test_report_command_with_an_empty_team_is_refused(
    tmp_path: Path, settings_file: Path, monkeypatch
) -> None:
    archive_root = prepare(tmp_path, settings_file, monkeypatch)
    result = runner.invoke(app, ["report", "--team", "  "])
    assert result.exit_code != 0
    assert not list((archive_root / "reports" / TEAM_KEY).glob("*.md"))


def test_help_lists_report() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "report" in result.output


def test_report_command_has_no_force_option() -> None:
    """Aikaleimattu nimi tekee pakottamisesta tarpeetonta."""
    result = runner.invoke(app, ["report", "--help"])
    assert result.exit_code == 0
    # Ohjeteksti kertoo miksi valintaa ei ole, joten sana esiintyy siinä;
    # tarkistus koskee valintaluetteloa.
    options = result.output.split("Options")[1]
    assert "--pakota" not in options
    assert "--team" in options


def test_the_command_reaches_the_stage_with_the_archive_only(
    tmp_path: Path, settings_file: Path, monkeypatch
) -> None:
    """AD-3: ``render`` ei saa yhtäkään asetusosiota -- kaikki tulee report.jsonista."""
    seen: dict[str, object] = {}

    def fake_run(archive, team, **kwargs):
        seen["archive"] = archive
        seen["team"] = team
        seen["kwargs"] = kwargs
        return render_result()

    prepare(tmp_path, settings_file, monkeypatch)
    monkeypatch.setattr("pappascout.cli.render_stage.run", fake_run)

    result = runner.invoke(app, ["report", "--team", TEAM_KEY])
    assert result.exit_code == 0, result.output
    assert seen["team"] == TEAM_KEY
    assert seen["kwargs"] == {}
    assert hasattr(seen["archive"], "reports_dir")


def test_the_report_uses_the_fixture_report(tmp_path: Path) -> None:
    """Varmistus siitä, että CLI-testit lukevat samaa fikstuuria kuin muut."""
    archive = build_archive(tmp_path)
    assert archive.report_json(TEAM_KEY).is_file()
    assert report([pistol_map()]).team.key == TEAM_KEY
