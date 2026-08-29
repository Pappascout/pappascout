"""``pappascout classify`` -- komennon ja kierroslistan testit.

Kaksi asiaa lukitaan täällä:

* **AD-3**: komento antaa vaiheelle vain ``settings.thresholds`` ja
  ``settings.league``. Jos vaihe näkisi ``[parse]``-osion, lupaus "kynnysmuutos
  ei uudelleenparsi" ei olisi enää rakenteellinen.
* **SM-2**: ``--show`` tulostaa sen listan, jolla käyttäjä tarkistaa
  luokittelun demoa vasten -- jokaisella kierroksella tyyppi, lähtöarvot ja
  perustelu, eikä perustelua katkaista.

Vaihe itse on korvattu, joten mikään näistä testeistä ei lue demoa.
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
        "reason": "Kierros 1 on pistoolikierros (kierrokset 1, 13).",
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
    raise AssertionError(f"rivia {label!r} ei ole tulosteessa:" + chr(10) + output_text)


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Korvaa ``stages.classify.run``; palauta se mitä vaiheelle annettiin."""
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


# --- Yhteenveto -------------------------------------------------------------------


def test_summary_reports_team_rounds_and_type_distribution() -> None:
    output_text = _render_classify(classify_result())
    assert output_text.startswith("Luokiteltu:")
    assert field_value(output_text, "Joukkue") == TEAM
    assert field_value(output_text, "Kierrokset") == "3"
    assert field_value(output_text, "Tyypit") == "pistol 1, eco 1, full 1"
    assert field_value(output_text, "Ajoaika") == "0,4 s"


def test_summary_lists_both_outputs() -> None:
    """Parquet ja kierroslista ovat molemmat ajon tuloksia."""
    output_text = _render_classify(classify_result())
    assert ".parquet" in output_text
    assert ".md" in output_text


def test_summary_says_when_the_stage_was_skipped() -> None:
    result = classify_result(skipped=True, reason="Tulos on ajan tasalla.")
    output_text = _render_classify(result)
    assert output_text.startswith("Ohitettu:")
    assert field_value(output_text, "Syy") == "Tulos on ajan tasalla."


def test_summary_shows_unclassified_rounds_exactly_once() -> None:
    """Luokittelematon on tila, ei kierrostyyppi -- ei siis tyyppijakaumaan."""
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
    assert field_value(output_text, UNCLASSIFIED.capitalize()).startswith("1 (havainto puuttuu")
    assert output_text.lower().count(UNCLASSIFIED) == 1
    assert field_value(output_text, "Tyypit") == "pistol 2"


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
    assert field_value(_render_classify(result), "Numeroimattomat").startswith("2 (")


def test_summary_hides_the_counters_that_are_zero() -> None:
    output_text = _render_classify(classify_result())
    assert UNCLASSIFIED.capitalize() not in output_text
    assert "Numeroimattomat" not in output_text


def test_summary_never_claims_zero_rounds_when_unreadable() -> None:
    result = classify_result(
        skipped=True, stats={"team_key": TEAM, "unreadable": "OSError: rikki"}
    )
    assert "lukuja ei saatu" in _render_classify(result)


# --- Kierroslista -----------------------------------------------------------------


def test_round_list_shows_every_input_the_decision_used() -> None:
    output_text = _render_round_list([row()])
    for label in ("Kierros", "Puoli", "Tyyppi", "Käytössä", "Jäljellä", "Ostettu",
                    "Varusteet", "Loss"):
        assert label in output_text
    assert "270" in output_text
    assert "1070" in output_text
    assert "530" in output_text
    assert "730" in output_text
    assert "pistol" in output_text


def test_round_list_never_truncates_the_reason() -> None:
    """Perustelu on juuri se, jota vasten luokittelu tarkistetaan demosta."""
    long_reason = "Eco hävityn kierroksen jälkeen: " + "x" * 200
    output_text = _render_round_list([row(reason=long_reason)])
    assert long_reason in output_text


def test_round_list_shows_the_opponent_type_too() -> None:
    output_text = _render_round_list([row(round_type="eco", opp_round_type="full")])
    assert "eco" in output_text
    assert "full" in output_text


def test_round_list_marks_an_unclassified_round() -> None:
    """Puuttuva luokittelu näkyy nimeltä ja puuttuva arvo viivana, ei nollana."""
    output_text = _render_round_list(
        [
            row(
                round_type=None,
                money_per_player=None,
                money_available_per_player=None,
                spent_per_player=None,
                equip_per_player=None,
                reason="Kierrosta 1 ei luokitella: tila on 'no_freeze_end'.",
            )
        ]
    )
    data_line = output_text.splitlines()[2]
    assert data_line.split() == [
        "1",
        "T",
        "häviö",
        UNCLASSIFIED,
        "pistol",
        "-",
        "-",
        "-",
        "-",
        "1",
        # Bonus, Aseist. ja Ostokyky: puuttuva laskuri on viiva eikä "0/5".
        # Nolla väittäisi havainnoksi sen, ettei kukaan pystynyt ostamaan.
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
    """Kaksi sarakemäärittelyä erkanisi, ja tulosteet väittäisivät eri asioita."""
    headers = [o for o, _ in ROUND_LIST_COLUMNS]
    output_text = _render_round_list([row()])
    header_line = output_text.splitlines()[0]
    # Perustelu on omalla rivillään, muut sarakkeet otsikkorivillä.
    for label in headers[:-1]:
        assert label in header_line
    assert headers[-1] == "Perustelu"
    # Solut tulevat vaiheen omasta funktiosta, eivät komentorivin kopiosta.
    cells = round_list_cells(row())
    assert len(cells) == len(ROUND_LIST_COLUMNS)
    for cell in cells[:-1]:
        assert cell in output_text


def test_empty_round_list_says_so() -> None:
    assert _render_round_list([]) == "Kierroksia ei ole."


# --- Komento ------------------------------------------------------------------------


def test_stage_gets_only_the_three_sections_it_reads(fake_stage) -> None:
    """AD-3: vaihe ei saa nähdä ``[parse]``- eikä ``[project]``-osiota.

    ``[economy]`` tuli mukaan Story 1.10:ssä, koska puolioston ehto B lukee
    siitä häviöbonuksen portaat. Partitio ei silti löystynyt: vaihe saa yhä
    valmiit osiot eikä koko ``Settings``-oliota, joten se ei voi vahingossa
    alkaa lukea arkiston polkua tai parsinnan ikkunaa.
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
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--pakota"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kwargs"]["force"] is True


def test_round_list_is_printed_only_with_show(fake_stage) -> None:
    without_show = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM])
    assert "Kierros " not in without_show.output

    with_show = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--show"])
    assert with_show.exit_code == 0, with_show.output
    assert "Kierros" in with_show.output
    assert "pistoolikierros" in with_show.output


def test_all_teams_flag_classifies_both_lineups(fake_stage) -> None:
    """Molemmat joukkueet luokitellaan joka tapauksessa -- tämä myös tallentaa ne."""
    result = runner.invoke(app, ["classify", DEMO_ID, "--kaikki-joukkueet"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kutsut"] == [TEAM, TEAM_B]
    assert result.output.count("Luokiteltu:") == 2


def test_missing_team_is_passed_through_as_none(fake_stage) -> None:
    """Vaihe päättää virheilmoituksen, koska vain se tuntee demon kokoonpanot."""
    runner.invoke(app, ["classify", DEMO_ID])
    assert fake_stage["team"] is None


def test_missing_round_list_tells_what_to_do(fake_stage) -> None:
    fake_stage["tulos"] = classify_result(
        skipped=True, stats={"team_key": TEAM, "unreadable": "OSError: rikki"}
    )
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--show"])
    assert result.exit_code == 0, result.output
    assert "Kierroslistaa ei saatu luettua" in result.output
    assert "--pakota" in result.output


def test_unknown_team_is_finnish_without_a_traceback(
    fake_stage, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    fake_stage["virhe"] = PappascoutError(
        "Kokoonpanotunniste 'xxx' ei täsmää kumpaankaan demon kokoonpanoon.\n"
        "Demon kokoonpanot ovat:\n    aaa\n    bbb"
    )
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "classify", DEMO_ID, "--team", "xxx"]
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    error = capsys.readouterr().err
    assert "ei täsmää" in error
    assert "kokoonpanot ovat" in error
    assert "Traceback" not in error
