"""``pappascout select`` -- komennon testit (Story 3.3).

Neljä asiaa lukitaan täällä:

* **AD-3 ja kerrossääntö.** Komento antaa vaiheelle vain ``settings.league``n ja
  ``settings.thresholds``in, ja **ei porttia lainkaan** -- vaihe ei käy verkossa.
* **Komento tulostaa.** Yksi testi ajaa koko ketjun ``discover`` -> ``select``
  oikeilla vaiheilla feikkiportin takaa ja lukee ruudun. Ilman sitä ``typer.echo``
  saisi kadota komennosta, ja komento kirjoittaisi tiedoston sanomatta mitään.
* **Hylkäyksen syy näkyy ruudulla kokonaisena.** Käyttäjä ei koodaa itse eikä
  avaa JSONia, joten katkaistu tai puuttuva syy tarkoittaisi päätöstä, jota hän
  ei voi tarkistaa.
* **Virhe on suomeksi ja kertoo mitä tehdä seuraavaksi.**

Yhteenvedon muotoilua (``_render_select``) testataan **oikean ajon tuloksella**
``test_stage_select.py``:ssä. Tässä tiedostossa vaihe on korvattu vain silloin,
kun testin kohde on komennon johdotus eikä sen tuloste.
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
    """Vaiheen tulos johdotustesteille. **Ei tulosteen testaamiseen.**"""
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
    """Korvaa vaihe; palauta se, mitä vaiheelle annettiin."""
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
    """Oikeat vaiheet, feikattu portti -- ja arkisto väliaikaishakemistossa.

    Tämä on se johdotus, jota mikään korvattu vaihe ei voi todistaa: asetukset
    luetaan, arkiston polut rakennetaan, molemmat vaiheet ajetaan ja tuloste
    muodostetaan oikeasta tuloksesta.
    """
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr(
        "pappascout.stages.discover.default_source",
        lambda settings, archive: FakeSource({CHAMPIONSHIP: division_matches()}),
    )
    return settings_file.parent / "arkisto"


# --- Komento antaa vaiheelle oikeat osiot ----------------------------------


def test_the_command_passes_only_league_and_thresholds(fake_stage: dict) -> None:
    """AD-3: vaihe ei näe ``[parse]``-osiota eikä siis voi invalidoida parsintaa."""
    result = runner.invoke(app, ["select", "--team", "Rcave"])

    assert result.exit_code == 0, result.output
    assert isinstance(fake_stage["league"], LeagueSettings)
    assert isinstance(fake_stage["thresholds"], ThresholdSettings)
    assert fake_stage["team"] == "Rcave"
    assert fake_stage["kwargs"] == {}


def test_the_stage_gets_no_port_because_it_never_touches_the_network(
    fake_stage: dict,
) -> None:
    """``select`` lukee indeksit; verkkoyhteys on ``discover``in ja ``fetch``in."""
    result = runner.invoke(app, ["select", "--team", "Rcave"])

    assert result.exit_code == 0, result.output
    assert "source" not in fake_stage


def test_the_team_option_is_required(fake_stage: dict) -> None:
    """Valintatiedosto on joukkuekohtainen: ilman joukkuetta ei ole tiedostoa."""
    result = runner.invoke(app, ["select"])

    assert result.exit_code != 0
    assert "team" in result.output


def test_the_help_is_in_finnish() -> None:
    result = runner.invoke(app, ["select", "--help"])

    assert result.exit_code == 0
    assert "rosterikynnyksellä" in result.output
    assert "Joukkueen nimi" in result.output


# --- Komento tulostaa, ja tuloste tulee oikeasta ajosta --------------------


def test_the_command_prints_its_summary_and_writes_the_file(
    real_pipeline: Path,
) -> None:
    """Koko ketju oikeilla vaiheilla: tiedosto **ja** tuloste.

    Ilman tulosteen tarkistusta ``typer.echo`` saisi kadota komennosta:
    tiedosto syntyisi, ajo onnistuisi ja käyttäjä katsoisi tyhjää ruutua.
    """
    assert runner.invoke(app, ["discover"]).exit_code == 0

    result = runner.invoke(app, ["select", "--team", "Potku"])

    assert result.exit_code == 0, result.output
    assert result.output.strip(), "komento ei tulostanut mitään"
    assert "Valinta tehty" in result.output
    assert "PotkukelkkaPeek" in result.output
    assert "2 / 2 karttaa otantaan" in result.output
    assert "index/selections/" in result.output
    written = list((real_pipeline / "index" / "selections").glob("*.json"))
    assert len(written) == 1


def test_the_printed_summary_names_the_written_file(real_pipeline: Path) -> None:
    runner.invoke(app, ["discover"])

    result = runner.invoke(app, ["select", "--team", "Potku"])

    written = next((real_pipeline / "index" / "selections").glob("*.json"))
    assert written.name in result.output


def test_the_summary_goes_to_stdout_not_stderr(real_pipeline: Path) -> None:
    """Yhteenveto on komennon tulos; se kuuluu putkitettavaan virtaan."""
    runner.invoke(app, ["discover"])

    result = runner.invoke(app, ["select", "--team", "Potku"])

    assert "Valinta tehty" in result.stdout


# --- Virheet ovat suomeksi ja kertovat mitä tehdä --------------------------


def test_a_known_error_is_shown_in_finnish_without_a_traceback(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Käyttäjä ei koodaa itse: pinojälki ruudulla ei ohjaa mihinkään."""
    fake_stage["virhe"] = PappascoutError(
        "Haku 'T' osuu 3 joukkueeseen, joten valinta on tehtävä:\n"
        "    TUUHEE (8 pelaajaa, tunniste faction-04)"
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "select", "--team", "T"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    stderr = capsys.readouterr().err
    assert "Traceback" not in stderr
    assert "valinta on tehtävä" in stderr
    assert "TUUHEE" in stderr


def test_a_missing_index_tells_the_user_to_run_discover(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """I/O-matriisi: virhe kertoo mitä tehdä seuraavaksi, ei mitä meni pieleen."""
    fake_stage["virhe"] = PappascoutError(
        "Arkistosta puuttuu otteluindeksi (matches.json).\n"
        "Aja ensin: uv run pappascout discover"
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "select", "--team", "Rcave"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    assert "pappascout discover" in capsys.readouterr().err


def test_a_bad_threshold_is_a_settings_error_not_a_program_error(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """``roster_size = 6`` on käyttäjän asetusvirhe, ei ohjelmavirhe.

    Paljas ``ValueError`` päätyisi muotoon "Odottamaton virhe -- ohjelmavirhe"
    ja paluukoodiin 2, vaikka korjaus on hänen omassa settings.tomlissaan.
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
    assert "settings.tomlissa" in capsys.readouterr().err
