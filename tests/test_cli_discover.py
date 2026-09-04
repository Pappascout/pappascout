"""``pappascout discover`` -- komennon ja sen yhteenvedon testit (Story 3.2).

Kolme asiaa lukitaan täällä:

* **AD-3 ja kerrossääntö.** Komento antaa vaiheelle vain ``settings.league``n ja
  ``settings.thresholds``in, ja otteluportin se pyytää
  ``stages.discover.default_source``ilta -- ei adaptereilta. Jos komento
  rakentaisi FACEIT-asiakkaan itse, ``cli -> stages -> adapters`` -nuoli
  kääntyisi ja verkko olisi kiinni komentorivissä.
* **Yhteenveto kertoo laajuuden.** Käyttäjä tarkistaa tulosteesta, näkyykö koko
  divisioona ja ovatko rosterit oikean kokoisia -- liian pieni rosteri on ainoa
  tapa huomata puuttuva ``substitutes``-lista avaamatta tiedostoa.
* **Mikään pudotus ei ole hiljainen.** Tunnisteeton pelaaja, tunnisteeton
  joukkuerivi, siirtynyt pelaaja ja kiistanalainen kokoonpano ovat kaikki
  rivejä tulosteessa eivätkä pelkkiä kenttiä tiedostossa.

Vaihe itse on korvattu, joten mikään testi tässä tiedostossa ei käy verkossa
eikä lue arkistoa.
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
    """Tulos, jonka ``stats``ia on muutettu -- pohja pysyy yhtenä paikkana."""
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
    raise AssertionError(f"riviä {label!r} ei ole tulosteessa:\n{output_text}")


@pytest.fixture
def fake_stage(settings_file, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Korvaa vaiheen ja portin; palauta se, mitä vaiheelle annettiin."""
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


# --- Komento antaa vaiheelle oikeat osiot ---------------------------------------


def test_the_command_passes_only_league_and_thresholds(fake_stage: dict) -> None:
    """AD-3: vaihe ei näe ``[parse]``-osiota eikä siis voi invalidoida parsintaa."""
    result = runner.invoke(app, ["discover", "--team", "Rcave"])

    assert result.exit_code == 0, result.output
    assert isinstance(fake_stage["league"], LeagueSettings)
    assert isinstance(fake_stage["thresholds"], ThresholdSettings)
    assert fake_stage["team"] == "Rcave"
    assert fake_stage["kwargs"] == {}


def test_the_port_comes_from_the_stage_not_from_the_adapters(fake_stage: dict) -> None:
    """Riippuvuusnuoli on ``cli -> stages -> adapters``, ei ``cli -> adapters``."""
    result = runner.invoke(app, ["discover"])

    assert result.exit_code == 0, result.output
    assert fake_stage["source"] is not None
    assert fake_stage["source_archive"] is fake_stage["archive"]


def test_the_team_option_is_optional(fake_stage: dict) -> None:
    """Ilman ``--team`` indeksit kirjoitetaan ja divisioona luetellaan."""
    result = runner.invoke(app, ["discover"])

    assert result.exit_code == 0, result.output
    assert fake_stage["team"] is None


def test_the_help_is_in_finnish() -> None:
    """Käyttäjä ei koodaa itse, joten ohje on suomeksi kuten kaikki muukin."""
    result = runner.invoke(app, ["discover", "--help"])

    assert result.exit_code == 0
    assert "divisioonan" in result.output
    assert "Joukkueen nimi" in result.output


def test_there_is_no_force_flag() -> None:
    """Ei ole mitään pakotettavaa, kun mitään ei koskaan ohiteta.

    Tarkistus on rekisteröidyistä valinnoista eikä ohjetekstistä: ohje
    **kertoo** miksi lippua ei ole, joten merkkijonohaku löytäisi sen sieltä.
    Väite koskee vain ``--pakota``n puuttumista -- valintalistan lukitseminen
    kokonaan rikkoutuisi jokaisesta myöhemmästä laillisesta lisäyksestä.
    """
    command = next(c for c in app.registered_commands if c.name == "discover")
    names = [
        parameter
        for value in command.callback.__defaults__ or ()
        for parameter in getattr(value, "param_decls", ())
    ]

    assert "--pakota" not in names
    assert "--team" in names


# --- Yhteenveto -----------------------------------------------------------------


def test_the_summary_reports_the_scope_of_the_division() -> None:
    output_text = _render_discover(discover_result())

    assert output_text.startswith("Divisioona haettu: 12 joukkuetta, 66 ottelua")
    assert field_value(output_text, "Pelatut ottelut") == "6 / 66"
    assert field_value(output_text, "Rosterit") == "6-9 pelaajaa"


def test_the_summary_names_every_player_in_the_standing_roster() -> None:
    """Vakirosteri on koko tarinan tulos, joten se luetaan tulosteesta."""
    output_text = _render_discover(discover_result())

    roster = field_value(output_text, "Vakirosteri")
    assert roster.startswith("7 pelaajaa:")
    for nickname in RCAVE:
        assert nickname in roster


def test_the_summary_shows_the_bridge_to_the_archive() -> None:
    """Kokoonpanotiiviste kertoo, mikä arkiston hakemisto on tämä joukkue."""
    output_text = _render_discover(discover_result())

    assert field_value(output_text, "Arkiston kokoonpanot") == "ff03fb54599d3311"


def test_the_summary_omits_the_bridge_when_there_is_none() -> None:
    assert "Arkiston kokoonpanot" not in _render_discover(with_team(lineup_keys=[]))


def test_the_summary_lists_both_written_indexes() -> None:
    output_text = _render_discover(discover_result())

    assert "index/matches.json" in output_text
    assert "index/teams.json" in output_text


def test_the_summary_lists_the_division_when_no_team_was_asked_for() -> None:
    """Katselmus: nimet sai näkyviin vain aiheuttamalla virheen tahallaan."""
    stats = dict(discover_result().stats)
    del stats["team"]
    output_text = _render_discover(discover_result(stats=stats))

    assert "Vakirosteri" not in output_text
    assert "Divisioonan joukkueet:" in output_text
    for row in DIVISION_ROWS:
        assert str(row["name"]) in output_text
        assert str(row["team_key"]) in output_text


def test_the_summary_does_not_list_the_division_when_a_team_was_found() -> None:
    """Haetun joukkueen rivit ovat vastaus; koko luettelo olisi kohinaa."""
    assert "Divisioonan joukkueet:" not in _render_discover(discover_result())


def test_the_summary_names_other_observed_names() -> None:
    """Nimenvaihto on havainto eikä sitä piiloteta."""
    output_text = _render_discover(with_team(alternative_names=["Rcave"]))

    assert field_value(output_text, "Muut havaitut nimet") == "Rcave"


def test_the_summary_says_when_one_team_has_two_source_identifiers() -> None:
    """Identiteetti on rosteri: kaksi tunnistetta voi olla sama joukkue."""
    output_text = _render_discover(with_team(faction_ids=["kausi-12", "kausi-13"]))

    assert "kausi-12" in field_value(output_text, "Lähteen tunnisteet")
    assert "eri kaudet" in field_value(output_text, "Lähteen tunnisteet")


# --- Mikään pudotus ei ole hiljainen --------------------------------------------


def test_the_summary_names_the_players_that_were_left_out() -> None:
    """Hiljainen pudotus näyttäisi vain lyhyemmältä rosterilta.

    Nimi on mukana luvun lisäksi, jotta käyttäjä voi tarkistaa keneltä tunniste
    puuttui -- pelkkä luku olisi väite ilman tarkistusmahdollisuutta.
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

    value = field_value(output_text, "Ilman SteamID64:aa")
    assert value.startswith("2 pelaajaa")
    assert "eka" in value and "toka" in value


def test_the_summary_is_silent_when_nobody_was_left_out() -> None:
    assert "Ilman SteamID64" not in _render_discover(discover_result())


def test_the_summary_counts_team_rows_that_had_no_identifier() -> None:
    """Katselmus: pudotetut pelaajat kerrottiin, pudotetut joukkuerivit eivät."""
    output_text = _render_discover(with_stats(team_rows_without_id=3))

    assert field_value(output_text, "Tunnisteettomat joukkuerivit").startswith("3 kpl")


def test_the_summary_reports_a_player_who_changed_teams() -> None:
    """Siirtymä muuttaa rosteria, joten se kuuluu yhteenvetoon."""
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

    assert "siirtyja" in field_value(output_text, "Siirtyneet pelaajat")
    assert "Aakkoset" in field_value(output_text, "Siirtyneet pelaajat")


def test_the_summary_reports_a_player_two_teams_both_claim() -> None:
    """Kiistaa ei ratkaista arpomalla, joten se on luettavissa."""
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

    assert "kiistelty" in field_value(output_text, "Kahdessa joukkueessa")


def test_the_summary_reports_a_contested_lineup_key() -> None:
    """Jatkovaihe laskisi tiivisteen kahdesti tietämättä tekevänsä niin."""
    output_text = _render_discover(with_stats(contested_lineup_keys=["ff03fb54"]))

    assert "ff03fb54" in field_value(output_text, "Kiistanalaiset kokoonpanot")


def test_the_summary_leads_with_the_reason_when_there_is_one() -> None:
    """Tyhjä tulos ``ok``-tilassa ilman selitystä jättäisi käyttäjän arvaamaan."""
    output_text = _render_discover(
        discover_result(
            reason="Kilpailusta ei löytynyt yhtään ottelua. Tarkista "
            "[league].championship_ids asetuksista.",
            stats=dict(discover_result().stats, matches=0, teams=0),
        )
    )

    assert field_value(output_text, "Huomio").startswith("Kilpailusta ei löytynyt")


def test_the_summary_has_no_reason_line_when_everything_was_found() -> None:
    assert "Huomio" not in _render_discover(discover_result())


# --- Virheet --------------------------------------------------------------------


def ambiguous_error() -> PappascoutError:
    """Sama viesti kuin vaihe oikeasti nostaa -- ei käsin kirjoitettu kopio.

    Literaali menisi läpi vaikka vaiheen sanamuoto muuttuisi, eli testi
    mittaisi omaa merkkijonoaan eikä ohjelmaa.
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


def test_an_ambiguous_name_ends_in_a_finnish_error_and_exit_code_one(
    fake_stage: dict, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """Monitulkintaisuutta ei ratkaista hiljaa: ajo päättyy, valinta pyydetään."""
    fake_stage["virhe"] = ambiguous_error()
    monkeypatch.setattr("sys.argv", ["pappascout", "discover", "--team", "T"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    stderr = capsys.readouterr().err
    for name in ("TUUHEE", "Takakeno", "Tankkiluola vilttiketju"):
        assert name in stderr
    # Tunniste on mukana, koska ilman sitä kahta samannimistä ei voi erottaa.
    assert "faction-04" in stderr
