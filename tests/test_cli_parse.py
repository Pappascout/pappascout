"""``pappascout parse`` -- komennon ja sen tulosteen testit.

Kaksi asiaa lukitaan täällä:

* **AD-3**: komento antaa vaiheelle vain ``settings.parse``, ei koko
  ``Settings``-oliota. Jos vaihe näkisi kynnykset, lupaus "kynnysmuutos ei
  uudelleenparsi" ei olisi enää rakenteellinen.
* **NFR-1**: tuloste on suomeksi, kertoo kierrosten määrän, jatkoajan,
  ohitetut kierrokset ja ajoajan -- eikä yksikään virhe päädy ruudulle
  pinojälkenä.

Vaihe itse on korvattu, joten mikään näistä testeistä ei lue demoa.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import (
    _PARSE_LABEL_WIDTH,
    EXIT_KNOWN_ERROR,
    _render_parse,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR, ParseSettings
from pappascout.stages import StageResult

runner = CliRunner()

DEMO_ID = "1-abc-1"

#: Sarake, josta arvot alkavat: kahden välilyönnin sisennys + otsikkosarake.
_ARVOSARAKE = 2 + _PARSE_LABEL_WIDTH

#: Vaiheen luvut onnistuneesta ajosta -- 21 kierrosta, neljä näytepistettä.
DEFAULT_STATS: dict[str, object] = {
    "rounds": 21,
    "rows": 42,
    "max_round_no": 21,
    "skipped_rounds": 1,
    "no_freeze_end": 0,
    "tick_rows": 780,
    "sample_points": 78,
    "sample_rounds": 21,
    "first_contact_rounds": 20,
    "partial_samples": 0,
    "unknown_side_events": 0,
}


def parse_result(**muutokset) -> StageResult:
    """Vaiheen tulos oletusarvoilla; testi muuttaa vain sen mitä tutkii."""
    oletus: dict[str, object] = {
        "stage": "parse",
        "unit": DEMO_ID,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath("parsed/1-abc-1/rounds.parquet"),),
        "manifest_path": PurePosixPath("parsed/1-abc-1/parse.manifest.json"),
        "duration_s": 12.34,
        "stats": dict(DEFAULT_STATS),
    }
    oletus.update(muutokset)
    return StageResult(**oletus)  # type: ignore[arg-type]


def stats(**muutokset) -> dict[str, object]:
    """Vaiheen luvut oletuksilla; testi muuttaa vain sen mitä tutkii."""
    luvut = dict(DEFAULT_STATS)
    luvut.update(muutokset)
    return luvut


def arvo(tuloste: str, otsikko: str) -> str:
    """Poimi yhden rivin arvo otsikon perusteella."""
    for rivi in tuloste.splitlines():
        kuori = rivi.strip()
        if kuori.startswith(otsikko):
            return kuori[len(otsikko) :].strip()
    raise AssertionError(f"rivia {otsikko!r} ei ole tulosteessa:" + chr(10) + tuloste)


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """Demon paikkamerkki -- vaihe on korvattu, joten sisältöä ei lueta."""
    path = tmp_path / f"{DEMO_ID}.dem"
    path.write_bytes(b"PBDEMS2" + bytes(1))
    return path


@pytest.fixture
def vale_vaihe(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Korvaa ``stages.parse.run`` ja portti; palauta se mitä vaiheelle annettiin."""
    nahty: dict[str, object] = {}

    def vale_run(settings, archive, map_demo_id, parser, **kwargs):
        nahty["settings"] = settings
        nahty["archive"] = archive
        nahty["unit"] = map_demo_id
        nahty["parser"] = parser
        nahty["kwargs"] = kwargs
        return nahty.get("tulos") or parse_result(unit=map_demo_id)

    def vale_portti(settings):
        # Ensikontaktin sääntö on asetus, joten portti saa [parse]-osion.
        nahty["portin_asetukset"] = settings
        return "portti"

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.parse.run", vale_run)
    monkeypatch.setattr("pappascout.stages.parse.default_parser", vale_portti)
    return nahty


# --- Tuloste -------------------------------------------------------------------


def test_reports_rounds_skips_and_duration() -> None:
    tuloste = _render_parse(parse_result(), regulation_rounds=24)
    assert arvo(tuloste, "Kierrokset") == "21 (rivejä 42)"
    assert arvo(tuloste, "Ohitetut kierrokset").startswith("1 (warmup")
    assert arvo(tuloste, "Ajoaika") == "12,3 s"
    assert "rounds.parquet" in tuloste


def test_columns_line_up() -> None:
    """Arvot alkavat samasta sarakkeesta, myös pisimmän otsikon rivillä.

    Pisin otsikko on ``Ohitetut kierrokset``; ennen sen lisäämistä tuloste
    hyppäsi juuri sillä rivillä sarakkeen yli.
    """
    tulos = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=1,
            no_freeze_end=2,
            tick_rate=64.0,
            tick_rate_measured=False,
        )
    )
    rivit = _render_parse(tulos, regulation_rounds=24).splitlines()[1:]
    assert len(rivit) >= 6

    for rivi in rivit:
        assert rivi.startswith("  "), rivi
        # Arvo alkaa aina samasta sarakkeesta, ja otsikko mahtuu sen eteen.
        assert rivi[_ARVOSARAKE] != " ", f"arvo ei ala sarakkeesta: {rivi!r}"
        assert rivi[_ARVOSARAKE - 1] == " ", f"otsikko ja arvo kiinni: {rivi!r}"
        assert rivi[2:_ARVOSARAKE].strip(), f"otsikko puuttuu: {rivi!r}"


def test_mentions_overtime_only_when_earned() -> None:
    assert arvo(_render_parse(parse_result(), regulation_rounds=24), "Jatkoaika") == (
        "ei (21/24)"
    )

    jatkoaika = parse_result(
        stats=stats(
            rounds=28,
            rows=56,
            max_round_no=28,
            skipped_rounds=1,
            no_freeze_end=0,
        )
    )
    rivi = arvo(_render_parse(jatkoaika, regulation_rounds=24), "Jatkoaika")
    assert rivi.startswith("kyllä")
    assert "28" in rivi


def test_hides_the_skip_line_when_nothing_was_skipped() -> None:
    tulos = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
        )
    )
    assert "Ohitetut kierrokset" not in _render_parse(tulos, regulation_rounds=24)


def test_reports_rounds_without_a_freeze_anchor() -> None:
    tulos = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=1,
            no_freeze_end=2,
        )
    )
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert arvo(tuloste, "Ilman ankkuria").startswith("2 (freezetime")


def test_says_when_the_stage_was_skipped() -> None:
    tulos = parse_result(skipped=True, reason="Tulos on ajan tasalla.")
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert tuloste.startswith("Ohitettu:")
    assert arvo(tuloste, "Syy") == "Tulos on ajan tasalla."


def test_shows_a_non_ok_status_and_its_reason() -> None:
    """AD-9: epäonnistunut yksikkö ei saa näyttää onnistuneelta."""
    tulos = parse_result(status="no_freeze_end", reason="Ankkuri puuttui.")
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert arvo(tuloste, "Tila") == "no_freeze_end"
    assert arvo(tuloste, "Syy") == "Ankkuri puuttui."


def test_ok_status_is_not_repeated_on_its_own_line() -> None:
    assert "Tila" not in _render_parse(parse_result(), regulation_rounds=24)


def test_says_when_the_tick_rate_is_a_default() -> None:
    tulos = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
            tick_rate=64.0,
            tick_rate_measured=False,
        )
    )
    assert "oletus" in arvo(_render_parse(tulos, regulation_rounds=24), "Tickrate")


def test_measured_tick_rate_is_not_mentioned() -> None:
    tulos = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
            tick_rate=64.0,
            tick_rate_measured=True,
        )
    )
    assert "Tickrate" not in _render_parse(tulos, regulation_rounds=24)


def test_never_claims_zero_rounds_when_the_result_is_unreadable() -> None:
    tulos = parse_result(skipped=True, stats={"unreadable": "OSError: rikki"})
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert "lukuja ei saatu" in tuloste
    assert arvo(tuloste, "Kierrokset").startswith("lukuja ei saatu")


# --- Näytepisteet ja ensikontaktit ---------------------------------------------


def test_reports_sample_points_and_first_contacts() -> None:
    """Käyttäjän on nähtävä, että asetelmadata syntyi."""
    tuloste = _render_parse(parse_result(), regulation_rounds=24)
    rivi = arvo(tuloste, "Näytepisteet")
    assert rivi.startswith("78 (21/21 kierroksella")
    assert "780" in rivi
    assert arvo(tuloste, "Ensikontaktit") == "20/21 kierroksella"


def test_rounds_without_any_sample_point_are_named() -> None:
    """Nollan syytä ei arvata: erotus kerrotaan, mahdolliset syyt luetellaan.

    Ankkurin puute ja hyvin lyhyt kierros tuottavat saman nollan, joten
    yhden syyn nimeäminen olisi arvaus.
    """
    tulos = parse_result(stats=stats(sample_rounds=18))
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert arvo(tuloste, "Näytepisteet").startswith("78 (18/21 kierroksella")
    rivi = arvo(tuloste, "Ilman näytepistettä")
    assert rivi.startswith("3 kierrosta")
    assert "ankkuri" in rivi


def test_every_round_sampled_hides_the_difference_line() -> None:
    assert "Ilman näytepistettä" not in _render_parse(
        parse_result(), regulation_rounds=24
    )


def test_partial_sample_points_are_reported() -> None:
    """Vajaa näytepiste on adapterin havainto -- taulusta sitä ei näe."""
    tulos = parse_result(stats=stats(partial_samples=4))
    rivi = arvo(_render_parse(tulos, regulation_rounds=24), "Vajaat näytepisteet")
    assert rivi.startswith("4 (")


def test_events_with_an_unknown_side_are_reported() -> None:
    tulos = parse_result(stats=stats(unknown_side_events=2))
    rivi = arvo(_render_parse(tulos, regulation_rounds=24), "Puoli tuntematon")
    assert rivi.startswith("2 vahinkotapahtumaa")


def test_clean_run_hides_both_diagnostic_lines() -> None:
    tuloste = _render_parse(parse_result(), regulation_rounds=24)
    assert "Vajaat näytepisteet" not in tuloste
    assert "Puoli tuntematon" not in tuloste


def test_unreadable_ticks_do_not_hide_the_round_counts() -> None:
    """Yksi rikki mennyt taulu ei saa viedä toisen lukuja."""
    luvut = stats()
    for avain in (
        "tick_rows",
        "sample_points",
        "sample_rounds",
        "first_contact_rounds",
    ):
        luvut.pop(avain)
    luvut["ticks_unreadable"] = "OSError: rikki"
    tuloste = _render_parse(
        parse_result(skipped=True, stats=luvut), regulation_rounds=24
    )
    assert arvo(tuloste, "Kierrokset") == "21 (rivejä 42)"
    assert arvo(tuloste, "Näytepisteet").startswith("lukuja ei saatu")


def test_zero_sample_points_is_said_out_loud() -> None:
    """Nolla ei saa hukkua: kierrosluku näyttäisi samalta tyhjällä taululla."""
    tulos = parse_result(
        stats=stats(tick_rows=0, sample_points=0, sample_rounds=0,
                    first_contact_rounds=0)
    )
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert arvo(tuloste, "Näytepisteet").startswith("0 --")
    assert arvo(tuloste, "Ensikontaktit").startswith("0 --")


def test_zero_first_contacts_is_said_out_loud_even_with_samples() -> None:
    """Purematon ensikontaktisääntö ei saa näyttää normaalilta ajolta."""
    tulos = parse_result(stats=stats(first_contact_rounds=0))
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert arvo(tuloste, "Näytepisteet").startswith("78 ")
    assert arvo(tuloste, "Ensikontaktit").startswith("0 --")


def test_sample_lines_are_absent_when_the_result_was_unreadable() -> None:
    """Ilman lukuja ei keksitä nollaa -- se väittäisi tyhjää tulosta."""
    tulos = parse_result(skipped=True, stats={"unreadable": "OSError: rikki"})
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert "Näytepisteet" not in tuloste
    assert "Ensikontaktit" not in tuloste


def test_both_output_tables_are_listed() -> None:
    tulos = parse_result(
        outputs=(
            PurePosixPath("parsed/1-abc-1/rounds.parquet"),
            PurePosixPath("parsed/1-abc-1/ticks.parquet"),
        )
    )
    tuloste = _render_parse(tulos, regulation_rounds=24)
    assert "rounds.parquet" in tuloste
    assert "ticks.parquet" in tuloste


# --- Komento -------------------------------------------------------------------


def test_stage_gets_only_the_parse_section(vale_vaihe, demo: Path) -> None:
    """AD-3: vaihe ei saa nähdä kynnyksiä eikä liiga-asetuksia."""
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output

    settings = vale_vaihe["settings"]
    assert isinstance(settings, ParseSettings)
    for kielletty in ("thresholds", "league", "economy", "project"):
        assert not hasattr(settings, kielletty)
    assert vale_vaihe["unit"] == DEMO_ID
    assert vale_vaihe["parser"] == "portti"
    # Portti saa saman [parse]-osion: ensikontaktin sääntö on asetus.
    assert vale_vaihe["portin_asetukset"] is settings
    assert vale_vaihe["kwargs"]["force"] is False
    assert vale_vaihe["kwargs"]["demo_path"] == demo


def test_force_flag_reaches_the_stage(vale_vaihe, demo: Path) -> None:
    result = runner.invoke(app, ["parse", str(demo), "--pakota"])
    assert result.exit_code == 0, result.output
    assert vale_vaihe["kwargs"]["force"] is True


def test_overtime_line_uses_the_league_format(vale_vaihe, demo: Path) -> None:
    """Säännönmukaisten kierrosten määrä on 2 x liigan MR-arvo."""
    vale_vaihe["tulos"] = parse_result(
        stats=stats(
            rounds=28,
            rows=56,
            max_round_no=28,
            skipped_rounds=1,
            no_freeze_end=0,
        )
    )
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output
    # settings.tomlissa mr = 12 -> 24 saannonmukaista kierrosta.
    assert "säännönmukaisia 24" in result.output


def test_run_is_announced_before_it_starts(vale_vaihe, demo: Path) -> None:
    """Useiden sekuntien hiljaisuus näyttäisi jumittumiselta."""
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output
    assert f"Parsitaan {DEMO_ID}" in result.output


def test_missing_demo_is_finnish_without_a_traceback(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Käyttäjä ei koodaa itse: paluukoodi 1 ja suomenkielinen rivi."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("sys.argv", ["pappascout", "parse", "1-ei-tallaista-demoa-1"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    virhe = capsys.readouterr().err
    assert "Virhe:" in virhe
    assert "ei löytynyt" in virhe
    assert "Traceback" not in virhe
