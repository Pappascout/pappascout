"""``pappascout aggregate`` -- komennon ja sen yhteenvedon testit.

Kaksi asiaa lukitaan täällä:

* **AD-3**: komento antaa vaiheelle vain ``settings.thresholds`` ja
  ``settings.league``. Jos vaihe näkisi ``[parse]``-osion, lupaus
  "kynnysmuutos ei uudelleenparsi" ei olisi enää rakenteellinen.
* **Otanta näkyy tulosteessa.** Käyttäjä tarkistaa yhteenvedosta, tuliko
  mukaan se aineisto, jonka hän odotti -- ilman sitä puuttuva demo huomataan
  vasta valmiista raportista.

Vaihe itse on korvattu, joten mikään näistä testeistä ei lue demoa eikä
arkistoa.
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, _render_aggregate, app, main
from pappascout.domain.models import (
    SETTINGS_ENV_VAR,
    AggregateSettings,
    LeagueSettings,
    ThresholdSettings,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult

runner = CliRunner()

TEAM = "aaaaaaaaaaaaaaaa"
TEAM_B = "bbbbbbbbbbbbbbbb"


def aggregate_result(**overrides) -> StageResult:
    defaults: dict[str, object] = {
        "stage": "aggregate",
        "unit": TEAM,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath(f"aggregates/{TEAM}/report.json"),),
        "manifest_path": PurePosixPath(f"aggregates/{TEAM}/report.manifest.json"),
        "duration_s": 0.42,
        "stats": {
            "team_key": TEAM,
            "lineup_keys": [TEAM, TEAM_B],
            "display_name": "MatureMayhem",
            "display_name_source": "clan_name",
            "display_name_alternatives": [],
            "roster": [
                {"player_id": str(n), "display_name": f"pelaaja{n}"}
                for n in range(1, 7)
            ],
            "demos": 4,
            "rounds": 85,
            "sample": {
                "league": {"demos": 0, "rounds": 0},
                "other": {"demos": 0, "rounds": 0},
                "unknown": {"demos": 4, "rounds": 85},
            },
            "unclassified": 0,
            "unpaired_detonations": 0,
            "classify_thresholds": {"full_equip_min": 4000},
            "maps": [
                {
                    "map_name": "de_nuke",
                    "map_name_source": "map_demo_id",
                    "demos": 1,
                    "rounds": 23,
                    "sides": [
                        {
                            "side": "T",
                            "round_types": {"pistol": 1, "eco": 2, "full": 8},
                            "small_samples": ["pistol", "eco"],
                        }
                    ],
                }
            ],
            "missing_demos": [],
        },
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


def field_value(output_text: str, label: str) -> str:
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"rivia {label!r} ei ole tulosteessa:\n{output_text}")


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Korvaa ``stages.aggregate.run``; palauta se mitä vaiheelle annettiin."""
    seen: dict[str, object] = {}

    def fake_run(
        thresholds, league, archive, team, *, aggregate_settings, **kwargs
    ):
        seen["thresholds"] = thresholds
        seen["league"] = league
        seen["aggregate"] = aggregate_settings
        seen["archive"] = archive
        seen["team"] = team
        seen["kwargs"] = kwargs
        error = seen.get("virhe")
        if error is not None:
            raise error  # type: ignore[misc]
        return seen.get("tulos") or aggregate_result()

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.aggregate.run", fake_run)
    return seen


# --- Yhteenveto -----------------------------------------------------------------


def test_summary_reports_the_sample_first() -> None:
    output_text = _render_aggregate(aggregate_result())
    assert output_text.startswith("Aggregoitu:")
    assert field_value(output_text, "Otanta") == "4 demoa, 85 kierrosta"


def test_summary_names_all_three_buckets_in_finnish() -> None:
    """Kolme lokeroa, ei kahta -- myös silloin kun kaksi on tyhjää.

    Tuloste on suomeksi kuten kaikki muukin käyttäjälle näkyvä teksti; JSONin
    avaimet pysyvät englanniksi, koska ne ovat osa sopimusta. Sama työnjako
    kuin ``ROUND_TYPE_FI``:llä.
    """
    output_text = _render_aggregate(aggregate_result())
    buckets = field_value(output_text, "Lokerot")
    assert "liiga 0 demoa" in buckets
    assert "muut 0 demoa" in buckets
    assert "tuntematon 4 demoa / 85 kierrosta" in buckets
    assert "league" not in buckets and "unknown" not in buckets


def test_summary_reports_unpaired_detonations() -> None:
    """Hiljainen pudotus näyttäisi siltä, ettei kranaattia heitetty."""
    stats = dict(aggregate_result().stats)
    stats["unpaired_detonations"] = 3
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert field_value(output_text, "Parittomat räjähdykset").startswith("3 ")


def test_summary_omits_unpaired_detonations_when_there_are_none() -> None:
    assert "Parittomat" not in _render_aggregate(aggregate_result())


def test_summary_says_when_two_lineups_were_joined() -> None:
    """Liittäminen on päätös, joten se on näkyvissä eikä pääteltävissä."""
    output_text = _render_aggregate(aggregate_result())
    assert TEAM_B in field_value(output_text, "Kokoonpanot")


def test_summary_does_not_mention_lineups_when_there_is_only_one() -> None:
    stats = dict(aggregate_result().stats)
    stats["lineup_keys"] = [TEAM]
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert "Kokoonpanot" not in output_text


def test_summary_marks_small_samples_per_side() -> None:
    output_text = _render_aggregate(aggregate_result())
    assert "pieni otanta: pistol, eco" in output_text
    assert "T: pistol 1, eco 2, full 8" in output_text


def test_summary_shows_unclassified_rounds() -> None:
    stats = dict(aggregate_result().stats)
    stats["unclassified"] = 3
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert field_value(output_text, "Luokittelemattomat").startswith("3 kierrosta")


def test_summary_lists_missing_demos_with_their_reason() -> None:
    """Puuttuva demo ei katoa hiljaa."""
    stats = dict(aggregate_result().stats)
    stats["missing_demos"] = [{"match": "Anubis_vs_b", "reason": "ei parsittu"}]
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert field_value(output_text, "Puuttuva demo") == "Anubis_vs_b: ei parsittu"


def test_summary_says_when_the_stage_was_skipped() -> None:
    result = aggregate_result(skipped=True, reason="Tulos on ajan tasalla.")
    output_text = _render_aggregate(result)
    assert output_text.startswith("Ohitettu:")
    assert field_value(output_text, "Syy") == "Tulos on ajan tasalla."


@pytest.mark.parametrize(
    "source,note",
    [
        ("demo_header", ""),
        ("map_demo_id", ""),
        ("unknown", " (nimi tuntematon)"),
    ],
)
def test_summary_marks_only_the_map_whose_name_is_unknown(
    source: str, note: str
) -> None:
    """Merkintä kuuluu **vain** lähteelle ``unknown`` (Story 2.11).

    Kolme paria eikä yksi, ja se on mitattu tarve. Ehto oli alun perin
    kirjoitettu tunnettujen lähteiden luettelona
    (``"" if source == "map_demo_id" else " (nimi tuntematon)"``), ja kolmannen
    lähteen tullessa se olisi merkinnyt jokaisen otsikosta luetun kartan
    tuntemattomaksi. Yhden lähteen testi ei huomaa sitä: mutaatio, joka
    palauttaa vanhan ehdon, läpäisee ``unknown``-tapauksen sellaisenaan ja koko
    muun sarjan sen mukana. Vain ``demo_header``-pari punastuu.
    """
    stats = dict(aggregate_result().stats)
    stats["maps"] = [
        {
            "map_name": "de_ancient",
            "map_name_source": source,
            "demos": 2,
            "rounds": 42,
            "sides": [],
        }
    ]
    output_text = _render_aggregate(aggregate_result(stats=stats))
    assert f"de_ancient{note}: 2 demoa, 42 kierrosta" in output_text
    if not note:
        assert "nimi tuntematon" not in output_text


def test_summary_names_the_output_and_the_manifest() -> None:
    output_text = _render_aggregate(aggregate_result())
    assert field_value(output_text, "Tulos") == f"aggregates/{TEAM}/report.json"
    assert (
        field_value(output_text, "Manifesti")
        == f"aggregates/{TEAM}/report.manifest.json"
    )
    assert field_value(output_text, "Ajoaika") == "0,4 s"


# --- Komennon kytkentä ----------------------------------------------------------


def test_help_lists_aggregate() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "aggregate" in result.output


def test_command_passes_only_its_own_settings_sections(fake_stage: dict) -> None:
    """AD-3: vaihe ei näe ``[parse]``- eikä ``[economy]``-osiota."""
    result = runner.invoke(app, ["aggregate", "--team", TEAM])
    assert result.exit_code == 0, result.output
    assert isinstance(fake_stage["thresholds"], ThresholdSettings)
    assert isinstance(fake_stage["league"], LeagueSettings)
    assert isinstance(fake_stage["aggregate"], AggregateSettings)
    assert fake_stage["team"] == TEAM
    assert fake_stage["kwargs"] == {"force": False}


def test_force_flag_reaches_the_stage(fake_stage: dict) -> None:
    result = runner.invoke(app, ["aggregate", "--team", TEAM, "--pakota"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kwargs"] == {"force": True}


def test_without_team_the_stage_decides_what_to_say(fake_stage: dict) -> None:
    """Joukkueluettelo on vaiheen tieto, ei komentorivin."""
    result = runner.invoke(app, ["aggregate"])
    assert result.exit_code == 0, result.output
    assert fake_stage["team"] is None


def test_the_team_option_help_matches_what_actually_happens() -> None:
    """Ilman --teamia ajo päättyy virheeseen; ohje ei saa luvata listausta."""
    result = runner.invoke(app, ["aggregate", "--help"])
    assert result.exit_code == 0
    # Typer katkoo ohjetekstin laatikkoon, joten reunaviivat (Unicode-viivat
    # tai ASCII-putket ymparistosta riippuen) ja rivinvaihdot on siivottava
    # ennen vertailua.
    text = " ".join(
        "".join(
            " " if ord(ch) >= 0x2500 or ch == "|" else ch for ch in result.output
        ).split()
    )
    assert "ajo päättyy virheeseen, joka listaa arkiston joukkueet" in text
    assert "komento listaa arkiston" not in text


def test_a_known_error_becomes_a_finnish_line(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_stage["virhe"] = PappascoutError("Joukkuetta ei löytynyt.")
    monkeypatch.setattr("sys.argv", ["pappascout", "aggregate", "--team", TEAM])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == EXIT_KNOWN_ERROR


# --- Joukkueen nimi ja rosteri tulosteessa (Story 2.6) --------------------------


def test_an_observed_name_is_reported_as_observed() -> None:
    """Lähde on tulosteessa, ei vain nimi.

    Ilman lähdettä lukija ei näe, onko otsikossa havainto vai tunniste sen
    paikalla -- ja juuri sitä hän tulosteesta tarkistaa.
    """
    output_text = _render_aggregate(aggregate_result())
    assert field_value(output_text, "Nimi") == "MatureMayhem (havaittu demoista)"


def test_a_missing_name_says_the_report_speaks_of_the_key() -> None:
    numbers = dict(aggregate_result().stats)
    numbers.update(display_name=TEAM, display_name_source="team_key")
    output_text = _render_aggregate(aggregate_result(stats=numbers))
    assert field_value(output_text, "Nimi") == (
        f"ei havaittu -- raportti puhuu tunnisteesta {TEAM}"
    )


def test_conflicting_names_are_listed_and_absent_when_there_is_no_conflict() -> None:
    assert "Muut havaitut nimet" not in _render_aggregate(aggregate_result())

    numbers = dict(aggregate_result().stats)
    numbers["display_name_alternatives"] = ["MM Academy", "MM B"]
    output_text = _render_aggregate(aggregate_result(stats=numbers))
    assert field_value(output_text, "Muut havaitut nimet").startswith(
        "MM Academy, MM B"
    )


def test_the_roster_line_lists_the_names_not_just_their_count() -> None:
    """Kuusi SteamID64:ää täyttäisi tulosteen kertomatta enempää."""
    output_text = _render_aggregate(aggregate_result())
    assert field_value(output_text, "Rosteri") == (
        "6 pelaajaa havaittu: pelaaja1, pelaaja2, pelaaja3, pelaaja4, "
        "pelaaja5, pelaaja6"
    )


def test_a_player_without_a_name_shows_the_id_and_is_counted() -> None:
    """Nimetön pelaaja sanotaan ääneen eikä pudoteta."""
    numbers = dict(aggregate_result().stats)
    numbers["roster"] = [
        {"player_id": "1", "display_name": "Sassiz"},
        {"player_id": "76561198163808926", "display_name": None},
    ]
    output_text = _render_aggregate(aggregate_result(stats=numbers))
    value = field_value(output_text, "Rosteri")
    assert value == (
        "2 pelaajaa havaittu: Sassiz, 76561198163808926 "
        "(1 ilman nimeä, tunniste sen paikalla)"
    )
