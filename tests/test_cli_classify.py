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


def rivi(**muutokset) -> dict:
    oletus = {
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
    oletus.update(muutokset)
    return oletus


def classify_result(**muutokset) -> StageResult:
    oletus: dict[str, object] = {
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
                rivi(),
                rivi(round_no=2, round_type="eco", opp_round_type="full", loss_count=2),
                rivi(round_no=3, round_type="full", won=True),
            ],
        },
    }
    oletus.update(muutokset)
    return StageResult(**oletus)  # type: ignore[arg-type]


def arvo(tuloste: str, otsikko: str) -> str:
    for r in tuloste.splitlines():
        kuori = r.strip()
        if kuori.startswith(otsikko):
            return kuori[len(otsikko) :].strip()
    raise AssertionError(f"rivia {otsikko!r} ei ole tulosteessa:" + chr(10) + tuloste)


@pytest.fixture
def vale_vaihe(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Korvaa ``stages.classify.run``; palauta se mitä vaiheelle annettiin."""
    nahty: dict[str, object] = {"kutsut": []}

    def vale_run(thresholds, league, archive, map_demo_id, team, **kwargs):
        nahty["thresholds"] = thresholds
        nahty["league"] = league
        nahty["archive"] = archive
        nahty["unit"] = map_demo_id
        nahty["team"] = team
        nahty["kwargs"] = kwargs
        nahty["kutsut"].append(team)  # type: ignore[union-attr]
        virhe = nahty.get("virhe")
        if virhe is not None:
            raise virhe  # type: ignore[misc]
        return nahty.get("tulos") or classify_result(unit=map_demo_id)

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.classify.run", vale_run)
    monkeypatch.setattr(
        "pappascout.stages.classify.team_keys", lambda archive, kohde: [TEAM, TEAM_B]
    )
    return nahty


# --- Yhteenveto -------------------------------------------------------------------


def test_summary_reports_team_rounds_and_type_distribution() -> None:
    tuloste = _render_classify(classify_result())
    assert tuloste.startswith("Luokiteltu:")
    assert arvo(tuloste, "Joukkue") == TEAM
    assert arvo(tuloste, "Kierrokset") == "3"
    assert arvo(tuloste, "Tyypit") == "pistol 1, eco 1, full 1"
    assert arvo(tuloste, "Ajoaika") == "0,4 s"


def test_summary_lists_both_outputs() -> None:
    """Parquet ja kierroslista ovat molemmat ajon tuloksia."""
    tuloste = _render_classify(classify_result())
    assert ".parquet" in tuloste
    assert ".md" in tuloste


def test_summary_says_when_the_stage_was_skipped() -> None:
    tulos = classify_result(skipped=True, reason="Tulos on ajan tasalla.")
    tuloste = _render_classify(tulos)
    assert tuloste.startswith("Ohitettu:")
    assert arvo(tuloste, "Syy") == "Tulos on ajan tasalla."


def test_summary_shows_unclassified_rounds_exactly_once() -> None:
    """Luokittelematon on tila, ei kierrostyyppi -- ei siis tyyppijakaumaan."""
    tulos = classify_result(
        stats={
            "team_key": TEAM,
            "rounds": 3,
            "by_type": {"pistol": 2},
            "unclassified": 1,
            "unnumbered": 0,
            "rows": [],
        }
    )
    tuloste = _render_classify(tulos)
    assert arvo(tuloste, UNCLASSIFIED.capitalize()).startswith("1 (havainto puuttuu")
    assert tuloste.lower().count(UNCLASSIFIED) == 1
    assert arvo(tuloste, "Tyypit") == "pistol 2"


def test_summary_reports_rounds_dropped_for_having_no_number() -> None:
    tulos = classify_result(
        stats={
            "team_key": TEAM,
            "rounds": 3,
            "by_type": {"pistol": 3},
            "unclassified": 0,
            "unnumbered": 2,
            "rows": [],
        }
    )
    assert arvo(_render_classify(tulos), "Numeroimattomat").startswith("2 (")


def test_summary_hides_the_counters_that_are_zero() -> None:
    tuloste = _render_classify(classify_result())
    assert UNCLASSIFIED.capitalize() not in tuloste
    assert "Numeroimattomat" not in tuloste


def test_summary_never_claims_zero_rounds_when_unreadable() -> None:
    tulos = classify_result(
        skipped=True, stats={"team_key": TEAM, "unreadable": "OSError: rikki"}
    )
    assert "lukuja ei saatu" in _render_classify(tulos)


# --- Kierroslista -----------------------------------------------------------------


def test_round_list_shows_every_input_the_decision_used() -> None:
    tuloste = _render_round_list([rivi()])
    for otsikko in ("Kierros", "Puoli", "Tyyppi", "Käytössä", "Jäljellä", "Ostettu",
                    "Varusteet", "Loss"):
        assert otsikko in tuloste
    assert "270" in tuloste
    assert "1070" in tuloste
    assert "530" in tuloste
    assert "730" in tuloste
    assert "pistol" in tuloste


def test_round_list_never_truncates_the_reason() -> None:
    """Perustelu on juuri se, jota vasten luokittelu tarkistetaan demosta."""
    pitka = "Eco hävityn kierroksen jälkeen: " + "x" * 200
    tuloste = _render_round_list([rivi(reason=pitka)])
    assert pitka in tuloste


def test_round_list_shows_the_opponent_type_too() -> None:
    tuloste = _render_round_list([rivi(round_type="eco", opp_round_type="full")])
    assert "eco" in tuloste
    assert "full" in tuloste


def test_round_list_marks_an_unclassified_round() -> None:
    """Puuttuva luokittelu näkyy nimeltä ja puuttuva arvo viivana, ei nollana."""
    tuloste = _render_round_list(
        [
            rivi(
                round_type=None,
                money_per_player=None,
                money_available_per_player=None,
                spent_per_player=None,
                equip_per_player=None,
                reason="Kierrosta 1 ei luokitella: tila on 'no_freeze_end'.",
            )
        ]
    )
    datarivi = tuloste.splitlines()[2]
    assert datarivi.split() == [
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
    ]
    assert "0" not in datarivi
    assert "no_freeze_end" in tuloste


def test_round_list_columns_line_up() -> None:
    rivit = [rivi(), rivi(round_no=12, round_type="anomaly", money_per_player=12345)]
    rivit_ulos = [r for r in _render_round_list(rivit).splitlines() if r]
    otsikko, viiva = rivit_ulos[0], rivit_ulos[1]
    assert len(viiva) >= len(otsikko) - 2
    assert set(viiva.replace(" ", "")) == {"-"}


def test_console_and_markdown_share_one_column_definition() -> None:
    """Kaksi sarakemäärittelyä erkanisi, ja tulosteet väittäisivät eri asioita."""
    otsikot = [o for o, _ in ROUND_LIST_COLUMNS]
    tuloste = _render_round_list([rivi()])
    otsikkorivi = tuloste.splitlines()[0]
    # Perustelu on omalla rivillään, muut sarakkeet otsikkorivillä.
    for otsikko in otsikot[:-1]:
        assert otsikko in otsikkorivi
    assert otsikot[-1] == "Perustelu"
    # Solut tulevat vaiheen omasta funktiosta, eivät komentorivin kopiosta.
    solut = round_list_cells(rivi())
    assert len(solut) == len(ROUND_LIST_COLUMNS)
    for solu in solut[:-1]:
        assert solu in tuloste


def test_empty_round_list_says_so() -> None:
    assert _render_round_list([]) == "Kierroksia ei ole."


# --- Komento ------------------------------------------------------------------------


def test_stage_gets_only_the_threshold_and_league_sections(vale_vaihe) -> None:
    """AD-3: vaihe ei saa nähdä ``[parse]``-, ``[economy]``- eikä ``[project]``-osiota."""
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM])
    assert result.exit_code == 0, result.output

    thresholds = vale_vaihe["thresholds"]
    league = vale_vaihe["league"]
    assert isinstance(thresholds, ThresholdSettings)
    assert isinstance(league, LeagueSettings)
    for kielletty in ("parse", "economy", "project", "league"):
        assert not hasattr(thresholds, kielletty)
    for kielletty in ("parse", "economy", "project", "thresholds"):
        assert not hasattr(league, kielletty)
    assert vale_vaihe["unit"] == DEMO_ID
    assert vale_vaihe["team"] == TEAM
    assert vale_vaihe["kwargs"]["force"] is False


def test_force_flag_reaches_the_stage(vale_vaihe) -> None:
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--pakota"])
    assert result.exit_code == 0, result.output
    assert vale_vaihe["kwargs"]["force"] is True


def test_round_list_is_printed_only_with_show(vale_vaihe) -> None:
    ilman = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM])
    assert "Kierros " not in ilman.output

    kanssa = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--show"])
    assert kanssa.exit_code == 0, kanssa.output
    assert "Kierros" in kanssa.output
    assert "pistoolikierros" in kanssa.output


def test_all_teams_flag_classifies_both_lineups(vale_vaihe) -> None:
    """Molemmat joukkueet luokitellaan joka tapauksessa -- tämä myös tallentaa ne."""
    result = runner.invoke(app, ["classify", DEMO_ID, "--kaikki-joukkueet"])
    assert result.exit_code == 0, result.output
    assert vale_vaihe["kutsut"] == [TEAM, TEAM_B]
    assert result.output.count("Luokiteltu:") == 2


def test_missing_team_is_passed_through_as_none(vale_vaihe) -> None:
    """Vaihe päättää virheilmoituksen, koska vain se tuntee demon kokoonpanot."""
    runner.invoke(app, ["classify", DEMO_ID])
    assert vale_vaihe["team"] is None


def test_missing_round_list_tells_what_to_do(vale_vaihe) -> None:
    vale_vaihe["tulos"] = classify_result(
        skipped=True, stats={"team_key": TEAM, "unreadable": "OSError: rikki"}
    )
    result = runner.invoke(app, ["classify", DEMO_ID, "--team", TEAM, "--show"])
    assert result.exit_code == 0, result.output
    assert "Kierroslistaa ei saatu luettua" in result.output
    assert "--pakota" in result.output


def test_unknown_team_is_finnish_without_a_traceback(
    vale_vaihe, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    vale_vaihe["virhe"] = PappascoutError(
        "Kokoonpanotunniste 'xxx' ei täsmää kumpaankaan demon kokoonpanoon.\n"
        "Demon kokoonpanot ovat:\n    aaa\n    bbb"
    )
    monkeypatch.setattr(
        "sys.argv", ["pappascout", "classify", DEMO_ID, "--team", "xxx"]
    )
    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    virhe = capsys.readouterr().err
    assert "ei täsmää" in virhe
    assert "kokoonpanot ovat" in virhe
    assert "Traceback" not in virhe
