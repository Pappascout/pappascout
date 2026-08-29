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
_VALUE_COLUMN = 2 + _PARSE_LABEL_WIDTH

#: Vaiheen luvut onnistuneesta ajosta -- 21 kierrosta, neljä näytepistettä.
DEFAULT_STATS: dict[str, object] = {
    "rounds": 21,
    "rows": 42,
    "max_round_no": 21,
    "skipped_rounds": 1,
    "match_restarts": 0,
    "no_freeze_end": 0,
    "buy_window_seconds": 20.0,
    "buy_end_offsets_s": (10.4, 18.4, 20.0),
    "buy_window_truncated_by_death": 13,
    "buy_window_purchases_after_cut": 0,
    "buy_window_cuts_unchecked": 0,
    "buy_window_rounds_with_lost_purchases": (),
    "buy_window_ticks_without_players": 0,
    "buy_window_players_lost": 0,
    "buy_window_sides_without_rows": 0,
    "buy_window_refunds": 0,
    "buy_window_stale_equipment": 0,
    "armed_distribution": {0: 3, 4: 1, 5: 38},
    "armed_missing": 0,
    "armed_unknown_items": (),
    "tick_rows": 780,
    "sample_points": 78,
    "sample_rounds": 21,
    "first_contact_rounds": 20,
    "partial_samples": 0,
    "unknown_side_events": 0,
    "event_rows": 300,
    "utility_throws": 152,
    "utility_detonations": 148,
    "utility_rounds": 21,
    "utility_area_observed": 152,
    "utility_area_snapped": 52,
    "utility_area_unnamed": 0,
    "utility_without_area": 96,
    "utility_unnumbered_rounds": 0,
    "grenades_without_thrower": 0,
    "grenades_outside_rounds": 0,
    "grenades_unknown_side": 0,
    "grenades_unknown_type": 0,
    "grenades_fire_type_unresolved": 0,
    "grenades_detonating_after_round": 0,
    "grenade_ticks_without_players": 0,
    "grenades_sharing_an_entity_id": 0,
}


def parse_result(**overrides) -> StageResult:
    """Vaiheen tulos oletusarvoilla; testi muuttaa vain sen mitä tutkii."""
    defaults: dict[str, object] = {
        "stage": "parse",
        "unit": DEMO_ID,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath("parsed/1-abc-1/rounds.parquet"),),
        "manifest_path": PurePosixPath("parsed/1-abc-1/parse.manifest.json"),
        "duration_s": 12.34,
        "stats": dict(DEFAULT_STATS),
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


def stats(**overrides) -> dict[str, object]:
    """Vaiheen luvut oletuksilla; testi muuttaa vain sen mitä tutkii."""
    numbers = dict(DEFAULT_STATS)
    numbers.update(overrides)
    return numbers


def field_value(output_text: str, label: str) -> str:
    """Poimi yhden rivin arvo otsikon perusteella."""
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"rivia {label!r} ei ole tulosteessa:" + chr(10) + output_text)


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """Demon paikkamerkki -- vaihe on korvattu, joten sisältöä ei lueta."""
    path = tmp_path / f"{DEMO_ID}.dem"
    path.write_bytes(b"PBDEMS2" + bytes(1))
    return path


@pytest.fixture
def fake_stage(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Korvaa ``stages.parse.run`` ja portti; palauta se mitä vaiheelle annettiin."""
    seen: dict[str, object] = {}

    def fake_run(settings, archive, map_demo_id, parser, **kwargs):
        seen["settings"] = settings
        seen["archive"] = archive
        seen["unit"] = map_demo_id
        seen["parser"] = parser
        seen["kwargs"] = kwargs
        return seen.get("tulos") or parse_result(unit=map_demo_id)

    def fake_port(settings):
        # Ensikontaktin sääntö on asetus, joten portti saa [parse]-osion.
        seen["portin_asetukset"] = settings
        return "portti"

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.parse.run", fake_run)
    monkeypatch.setattr("pappascout.stages.parse.default_parser", fake_port)
    return seen


# --- Tuloste -------------------------------------------------------------------


def test_reports_rounds_skips_and_duration() -> None:
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert field_value(output_text, "Kierrokset") == "21 (rivejä 42)"
    assert field_value(output_text, "Ohitetut kierrokset").startswith("1 (warmup")
    assert field_value(output_text, "Ajoaika") == "12,3 s"
    assert "rounds.parquet" in output_text


def test_columns_line_up() -> None:
    """Arvot alkavat samasta sarakkeesta, myös pisimmän otsikon rivillä.

    Pisin otsikko on ``Ohitetut kierrokset``; ennen sen lisäämistä tuloste
    hyppäsi juuri sillä rivillä sarakkeen yli.
    """
    result = parse_result(
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
    lines = _render_parse(result, regulation_rounds=24).splitlines()[1:]
    assert len(lines) >= 6

    for line in lines:
        assert line.startswith("  "), line
        # Arvo alkaa aina samasta sarakkeesta, ja otsikko mahtuu sen eteen.
        assert line[_VALUE_COLUMN] != " ", f"arvo ei ala sarakkeesta: {line!r}"
        assert line[_VALUE_COLUMN - 1] == " ", f"otsikko ja arvo kiinni: {line!r}"
        assert line[2:_VALUE_COLUMN].strip(), f"otsikko puuttuu: {line!r}"


def test_mentions_overtime_only_when_earned() -> None:
    assert field_value(_render_parse(parse_result(), regulation_rounds=24), "Jatkoaika") == (
        "ei (21/24)"
    )

    overtime = parse_result(
        stats=stats(
            rounds=28,
            rows=56,
            max_round_no=28,
            skipped_rounds=1,
            no_freeze_end=0,
        )
    )
    line = field_value(_render_parse(overtime, regulation_rounds=24), "Jatkoaika")
    assert line.startswith("kyllä")
    assert "28" in line


def test_hides_the_skip_line_when_nothing_was_skipped() -> None:
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
        )
    )
    assert "Ohitetut kierrokset" not in _render_parse(result, regulation_rounds=24)


def test_reports_match_restarts_separately_from_skipped_rounds() -> None:
    """Ottelun uudelleenaloitus ei ole edes kierros, joten sillä on oma rivi.

    Se ei sisälly ohitettujen kierrosten lukuun: uudelleenaloitus ei tule
    kierrostauluun lainkaan, kun taas ohitettu kierros on siellä ilman
    kierrosnumeroa. Siksi ohitettujen kierrosten rivi ei myöskään enää
    mainitse uudelleenkäynnistyksiä -- kaksi riviä laskisi saman asian.
    """
    result = parse_result(
        stats=stats(
            rounds=20,
            rows=40,
            max_round_no=20,
            skipped_rounds=1,
            match_restarts=1,
            no_freeze_end=0,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Uudelleenaloitukset") == (
        "1 kierrosraja ilman demon omaa numeroa -- ei kierros, ei riviä tauluun"
    )
    assert field_value(output_text, "Ohitetut kierrokset") == (
        "1 (warmup ja puukkokierros)"
    )


def test_more_than_one_restart_takes_the_plural() -> None:
    """``1 kierrosraja`` mutta ``2 kierrosrajaa`` -- luku taivuttaa yksikön."""
    result = parse_result(stats=stats(match_restarts=2))
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Uudelleenaloitukset").startswith(
        "2 kierrosrajaa "
    )


def test_says_out_loud_when_there_were_no_restarts() -> None:
    """Nolla on havainto: tuore ajo sanoo sen ääneen eikä vaikene."""
    result = parse_result(stats=stats(match_restarts=0))
    assert field_value(
        _render_parse(result, regulation_rounds=24), "Uudelleenaloitukset"
    ) == "ei yhtään"


def test_a_port_that_cannot_report_restarts_is_not_a_zero() -> None:
    """``None`` on eri asia kuin nolla: väitettä ei tehdä ilman havaintoa."""
    result = parse_result(stats=stats(match_restarts=None))
    assert field_value(
        _render_parse(result, regulation_rounds=24), "Uudelleenaloitukset"
    ).startswith("ei tiedossa")


def test_a_skipped_run_does_not_claim_there_were_no_restarts() -> None:
    """Ohitetussa ajossa avainta ei ole: rivi jätetään pois kokonaan.

    Uudelleenaloitus ei ole missään taulussa, joten sen määrää ei voi lukea
    valmiista tuloksesta. Nolla olisi väite, jota mikään ei tue.
    """
    without = {k: v for k, v in DEFAULT_STATS.items() if k != "match_restarts"}
    result = parse_result(skipped=True, stats=without)
    assert "Uudelleenaloitukset" not in _render_parse(result, regulation_rounds=24)


def test_reports_rounds_without_a_freeze_anchor() -> None:
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=1,
            no_freeze_end=2,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Ilman ankkuria").startswith("2 (freezetime")


def test_says_when_the_stage_was_skipped() -> None:
    result = parse_result(skipped=True, reason="Tulos on ajan tasalla.")
    output_text = _render_parse(result, regulation_rounds=24)
    assert output_text.startswith("Ohitettu:")
    assert field_value(output_text, "Syy") == "Tulos on ajan tasalla."


def test_shows_a_non_ok_status_and_its_reason() -> None:
    """AD-9: epäonnistunut yksikkö ei saa näyttää onnistuneelta."""
    result = parse_result(status="no_freeze_end", reason="Ankkuri puuttui.")
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Tila") == "no_freeze_end"
    assert field_value(output_text, "Syy") == "Ankkuri puuttui."


def test_ok_status_is_not_repeated_on_its_own_line() -> None:
    assert "Tila" not in _render_parse(parse_result(), regulation_rounds=24)


def test_says_when_the_tick_rate_is_a_default() -> None:
    result = parse_result(
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
    assert "oletus" in field_value(_render_parse(result, regulation_rounds=24), "Tickrate")


def test_measured_tick_rate_is_not_mentioned() -> None:
    result = parse_result(
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
    assert "Tickrate" not in _render_parse(result, regulation_rounds=24)


def test_never_claims_zero_rounds_when_the_result_is_unreadable() -> None:
    result = parse_result(skipped=True, stats={"unreadable": "OSError: rikki"})
    output_text = _render_parse(result, regulation_rounds=24)
    assert "lukuja ei saatu" in output_text
    assert field_value(output_text, "Kierrokset").startswith("lukuja ei saatu")


# --- Näytepisteet ja ensikontaktit ---------------------------------------------


def test_reports_sample_points_and_first_contacts() -> None:
    """Käyttäjän on nähtävä, että asetelmadata syntyi."""
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    line = field_value(output_text, "Näytepisteet")
    assert line.startswith("78 (21/21 kierroksella")
    assert "780" in line
    assert field_value(output_text, "Ensikontaktit") == "20/21 kierroksella"


def test_rounds_without_any_sample_point_are_named() -> None:
    """Nollan syytä ei arvata: erotus kerrotaan, mahdolliset syyt luetellaan.

    Ankkurin puute ja hyvin lyhyt kierros tuottavat saman nollan, joten
    yhden syyn nimeäminen olisi arvaus.
    """
    result = parse_result(stats=stats(sample_rounds=18))
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Näytepisteet").startswith("78 (18/21 kierroksella")
    line = field_value(output_text, "Ilman näytepistettä")
    assert line.startswith("3 kierrosta")
    assert "ankkuri" in line


def test_every_round_sampled_hides_the_difference_line() -> None:
    assert "Ilman näytepistettä" not in _render_parse(
        parse_result(), regulation_rounds=24
    )


def test_armed_player_distribution_is_reported() -> None:
    """Jakauma ja sääntö kerrotaan ajon yhteydessä.

    Väärä sääntö ei näy taulussa mitenkään: se läpäisisi jokaisen
    skeematarkistuksen. Jakauma on halvin tapa huomata se ajossa eikä vasta
    raportissa, ja säännön nimeäminen tarvitaan sen tulkitsemiseen.
    """
    line = field_value(
        _render_parse(parse_result(), regulation_rounds=24), "Aseistettuja"
    )
    assert line.startswith("panssari ja ase hallussa ostoajan lopussa; ")
    assert "0 -> 3 riviä" in line
    assert "4 -> 1 riviä" in line
    assert "5 -> 38 riviä" in line


def test_unknown_inventory_items_are_named_with_their_counts() -> None:
    """Tuntemattomat nimet kerrotaan esiintymämäärineen, ei vain lasketa.

    Luokittelu on sallittujen aseiden luettelo, joten tuntematon nimi ei
    aseista ketään. Uusi veitsiskini on odotettu tulos ja uusi **ase** on
    merkki siitä, että luettelo on jäänyt jälkeen -- ilman nimiä ne
    näyttäisivät täsmälleen samalta. Määrä erottaa ne vielä tarkemmin: yksi
    eksoottinen veitsi näkyy kerran, nimeämismuutos joka rivillä.
    """
    result = parse_result(
        stats=stats(armed_unknown_items=(("Uusi Ase", 12), ("Outo Veitsi", 1)))
    )
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Tuntemattomat esineet"
    )
    assert line.startswith("2 eri esinenimeä: Uusi Ase x12, Outo Veitsi x1")
    assert "ei laskettu aseeksi" in line


def test_one_unknown_item_is_named_in_the_singular() -> None:
    """Yksi nimi ei ole "1 eri esinenimeä"."""
    result = parse_result(stats=stats(armed_unknown_items=(("Outo Veitsi", 1),)))
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Tuntemattomat esineet"
    )
    assert line.startswith("1 esinenimi: Outo Veitsi x1")


def test_unknown_item_list_is_truncated() -> None:
    """Satojen nimien rivi ei ole luettava -- eikä se ole edes tarpeen.

    Jos demoparser2 muuttaa nimeämistapaansa, **jokainen** nimi on
    tuntematon. Silloin käyttäjän on nähtävä yhdellä silmäyksellä että jokin
    on pahasti pielessä, ei selattava kolmea riviä nimiä.
    """
    many = tuple((f"Nimi {index:02d}", 1) for index in range(30))
    result = parse_result(stats=stats(armed_unknown_items=many))
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Tuntemattomat esineet"
    )
    assert line.startswith("30 eri esinenimeä: ")
    assert "Nimi 19 x1" in line
    assert "Nimi 20" not in line
    assert "(+10 muuta)" in line


def test_no_unknown_inventory_items_says_so() -> None:
    """Tyhjä luettelo sanotaan ääneen: se on ajon terve tulos."""
    line = field_value(
        _render_parse(parse_result(), regulation_rounds=24),
        "Tuntemattomat esineet",
    )
    assert line == "ei yhtään"


def test_unknown_item_line_is_absent_when_the_run_was_skipped() -> None:
    """Ohitettu ajo ei tiedä nimiä: rivi puuttuu, se ei väitä tyhjää.

    Nimet eivät ole taulussa -- ne eivät aseista ketään -- joten ohitetusta
    ajosta niitä ei voi lukea takaisin. "Ei yhtään" olisi silloin väite,
    jota mikään ei tue.
    """
    numbers = stats()
    numbers.pop("armed_unknown_items")
    assert "Tuntemattomat esineet" not in _render_parse(
        parse_result(stats=numbers), regulation_rounds=24
    )


def test_unknown_item_line_says_when_the_port_does_not_report() -> None:
    """Kolmas tila: tuore ajo portilla, joka ei kerro tuntemattomia.

    ``None`` on eri asia kuin tyhjä. Jos ne yhdistettäisiin, portin
    hiljeneminen näyttäisi siltä, että jokainen nimi tunnistettiin.
    """
    result = parse_result(stats=stats(armed_unknown_items=None))
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Tuntemattomat esineet"
    )
    assert "ei tiedossa" in line


def test_armed_player_line_separates_a_skewed_distribution_from_a_healthy_one() -> None:
    """Ääripäät eivät riitä: 41 riviä nollaa ja yksi viitonen on eri asia.

    Molemmat tuottaisivat ääripäinä "0-5", joka näyttää terveeltä. Tämä on
    koko syy siihen, että rivillä on jakauma eikä min ja max.
    """
    healthy = parse_result(stats=stats(armed_distribution={0: 3, 4: 1, 5: 38}))
    skewed = parse_result(stats=stats(armed_distribution={0: 41, 5: 1}))

    healthy_line = field_value(_render_parse(healthy, regulation_rounds=24), "Aseistettuja")
    skewed_line = field_value(_render_parse(skewed, regulation_rounds=24), "Aseistettuja")

    assert healthy_line != skewed_line
    assert "0 -> 41 riviä" in skewed_line
    assert "5 -> 1 riviä" in skewed_line


def test_armed_player_line_names_the_rows_without_an_observation() -> None:
    result = parse_result(stats=stats(armed_missing=2))
    line = field_value(_render_parse(result, regulation_rounds=24), "Aseistettuja")
    assert line.endswith("havainto puuttuu 2 riviltä")


def test_armed_player_line_says_when_there_is_no_observation_at_all() -> None:
    """Tyhjä jakauma ei ole nolla: se on "ei tiedetä" jokaisella rivillä."""
    result = parse_result(stats=stats(armed_distribution={}, armed_missing=42))
    line = field_value(_render_parse(result, regulation_rounds=24), "Aseistettuja")
    assert "ei yhtään havaintoa (42 riviä)" in line


def test_armed_player_line_is_absent_without_the_numbers() -> None:
    """Lukukelvoton tulos ei saa väittää jakaumaa, jota ei ole."""
    numbers = stats()
    numbers.pop("armed_distribution")
    numbers.pop("armed_unknown_items")
    assert "Aseistettuja" not in _render_parse(
        parse_result(stats=numbers), regulation_rounds=24
    )


def test_partial_sample_points_are_reported() -> None:
    """Vajaa näytepiste on adapterin havainto -- taulusta sitä ei näe."""
    result = parse_result(stats=stats(partial_samples=4))
    line = field_value(_render_parse(result, regulation_rounds=24), "Vajaat näytepisteet")
    assert line.startswith("4 (")


def test_events_with_an_unknown_side_are_reported() -> None:
    result = parse_result(stats=stats(unknown_side_events=2))
    line = field_value(_render_parse(result, regulation_rounds=24), "Puoli tuntematon")
    assert line.startswith("2 vahinkotapahtumaa")


def test_clean_run_hides_both_diagnostic_lines() -> None:
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert "Vajaat näytepisteet" not in output_text
    assert "Puoli tuntematon" not in output_text


def test_unreadable_ticks_do_not_hide_the_round_counts() -> None:
    """Yksi rikki mennyt taulu ei saa viedä toisen lukuja."""
    numbers = stats()
    for key in (
        "tick_rows",
        "sample_points",
        "sample_rounds",
        "first_contact_rounds",
    ):
        numbers.pop(key)
    numbers["ticks_unreadable"] = "OSError: rikki"
    output_text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert field_value(output_text, "Kierrokset") == "21 (rivejä 42)"
    assert field_value(output_text, "Näytepisteet").startswith("lukuja ei saatu")


def test_zero_sample_points_is_said_out_loud() -> None:
    """Nolla ei saa hukkua: kierrosluku näyttäisi samalta tyhjällä taululla."""
    result = parse_result(
        stats=stats(tick_rows=0, sample_points=0, sample_rounds=0,
                    first_contact_rounds=0)
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Näytepisteet").startswith("0 --")
    assert field_value(output_text, "Ensikontaktit").startswith("0 --")


def test_zero_first_contacts_is_said_out_loud_even_with_samples() -> None:
    """Purematon ensikontaktisääntö ei saa näyttää normaalilta ajolta."""
    result = parse_result(stats=stats(first_contact_rounds=0))
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Näytepisteet").startswith("78 ")
    assert field_value(output_text, "Ensikontaktit").startswith("0 --")


def test_sample_lines_are_absent_when_the_result_was_unreadable() -> None:
    """Ilman lukuja ei keksitä nollaa -- se väittäisi tyhjää tulosta."""
    result = parse_result(skipped=True, stats={"unreadable": "OSError: rikki"})
    output_text = _render_parse(result, regulation_rounds=24)
    assert "Näytepisteet" not in output_text
    assert "Ensikontaktit" not in output_text


def test_both_output_tables_are_listed() -> None:
    result = parse_result(
        outputs=(
            PurePosixPath("parsed/1-abc-1/rounds.parquet"),
            PurePosixPath("parsed/1-abc-1/ticks.parquet"),
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert "rounds.parquet" in output_text
    assert "ticks.parquet" in output_text


# --- Komento -------------------------------------------------------------------


def test_stage_gets_only_the_parse_section(fake_stage, demo: Path) -> None:
    """AD-3: vaihe ei saa nähdä kynnyksiä eikä liiga-asetuksia."""
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output

    settings = fake_stage["settings"]
    assert isinstance(settings, ParseSettings)
    for forbidden in ("thresholds", "league", "economy", "project"):
        assert not hasattr(settings, forbidden)
    assert fake_stage["unit"] == DEMO_ID
    assert fake_stage["parser"] == "portti"
    # Portti saa saman [parse]-osion: ensikontaktin sääntö on asetus.
    assert fake_stage["portin_asetukset"] is settings
    assert fake_stage["kwargs"]["force"] is False
    assert fake_stage["kwargs"]["demo_path"] == demo


def test_force_flag_reaches_the_stage(fake_stage, demo: Path) -> None:
    result = runner.invoke(app, ["parse", str(demo), "--pakota"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kwargs"]["force"] is True


def test_overtime_line_uses_the_league_format(fake_stage, demo: Path) -> None:
    """Säännönmukaisten kierrosten määrä on 2 x liigan MR-arvo."""
    fake_stage["tulos"] = parse_result(
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


def test_run_is_announced_before_it_starts(fake_stage, demo: Path) -> None:
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
    error = capsys.readouterr().err
    assert "Virhe:" in error
    assert "ei löytynyt" in error
    assert "Traceback" not in error


# --- Utility -------------------------------------------------------------------


def test_reports_utility_throws_detonations_and_areas() -> None:
    """Neljä lukua, neljä eri kysymystä: syntyikö, päättyikö, osuiko, katosiko."""
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    line = field_value(output_text, "Utility ")
    assert line.startswith("152 heittoa, 148 räjähdystä")
    assert "21/21 kierroksella" in line
    assert field_value(output_text, "Ilman räjähdystä").startswith("4 kranaattia")
    assert field_value(output_text, "Utilityn alue") == (
        "152 havaittua, 52 napsautettua, 96 ilman aluetta"
    )


def test_observed_and_snapped_areas_are_never_lumped_together() -> None:
    """Heiton alue on havainto, räjähdyksen arvio -- yhteen niputettuna
    raportin lukija luulisi molempia yhtä varmoiksi."""
    line = field_value(_render_parse(parse_result(), regulation_rounds=24), "Utilityn alue")
    assert "havaittua" in line
    assert "napsautettua" in line


def test_an_unnamed_nearest_area_gets_its_own_line() -> None:
    """"Kukaan ei ollut lähellä" ja "lähin oli nimettömällä alueella" eroavat."""
    result = parse_result(stats=stats(utility_area_unnamed=7))
    line = field_value(_render_parse(result, regulation_rounds=24), "Nimetön alue")
    assert line.startswith("7 tapahtumaa")


def test_a_clean_run_hides_the_unnamed_area_line() -> None:
    assert "Nimetön alue" not in _render_parse(parse_result(), regulation_rounds=24)


def test_more_detonations_than_throws_is_never_a_negative_count() -> None:
    """Miinusmerkkinen "ilman räjähdystä" olisi luettavissa väärin päin.

    Räjähdys syntyy vain heiton parina, joten ylimäärä on vika eikä havainto.
    """
    result = parse_result(stats=stats(utility_throws=10, utility_detonations=13))
    output_text = _render_parse(result, regulation_rounds=24)
    assert "Ilman räjähdystä" not in output_text
    assert field_value(output_text, "Räjähdyksiä liikaa").startswith("3 enemmän")


def test_utility_dropped_by_the_stage_is_reported() -> None:
    """Numeroimattomilta kierroksilta pudonnut utility ei saa kadota hiljaa."""
    result = parse_result(stats=stats(utility_unnumbered_rounds=9))
    line = field_value(_render_parse(result, regulation_rounds=24), "Ei kierrosnumeroa")
    assert line.startswith("9 heittoa")


def test_the_remaining_utility_diagnostics_are_reported() -> None:
    """Jokainen hiljainen pudotus- tai epävarmuussyy näkyy omalla rivillään.

    Rivit tarkistetaan myös leveydeltään: yhteenvedon sarakkeet menevät
    sekaisin, jos yksi kuvaus on kaksi kertaa muiden mittainen.
    """
    result = parse_result(
        stats=stats(
            grenades_unknown_type=2,
            grenades_fire_type_unresolved=5,
            grenades_detonating_after_round=3,
            grenade_ticks_without_players=1,
            grenades_sharing_an_entity_id=4,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Tuntematon tyyppi").startswith("2 kranaattia")
    assert field_value(output_text, "Tulityyppi auki").startswith("5 kranaattia")
    assert field_value(output_text, "Räjähdys myöhässä").startswith("3 kierroksen")
    assert field_value(output_text, "Tickillä ei rivejä").startswith("1 päätepistettä")

    # Jaettu tunniste on **havainto eikä vika**, ja rivin on sanottava se.
    # Koko arvo tarkistetaan, ei vain alkuosaa: pelkkä startswith päästi läpi
    # sekä väärän yksikön ("kranaattiparia") että parquet-sarakkeen nimen.
    shared = field_value(output_text, "Jaettu tunniste")
    assert shared == "4 kranaattia jakaa pelin tunnisteen kierroksella (havainto)"
    assert "grenade_no" not in output_text


def test_zero_utility_is_said_out_loud() -> None:
    """Nolla ei saa hukkua: kierrosluku näyttäisi samalta tyhjällä taululla."""
    result = parse_result(
        stats=stats(
            event_rows=0,
            utility_throws=0,
            utility_detonations=0,
            utility_rounds=0,
            utility_area_observed=0,
            utility_area_snapped=0,
            utility_area_unnamed=0,
            utility_without_area=0,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Utility").startswith("0 heittoa --")
    assert "Utilityn alue" not in output_text


def test_every_grenade_detonated_hides_the_difference_line() -> None:
    result = parse_result(stats=stats(utility_detonations=152))
    assert "Ilman räjähdystä" not in _render_parse(result, regulation_rounds=24)


def test_dropped_grenades_are_reported_not_hidden() -> None:
    """Pudotettua kranaattia ei näe valmiista taulusta -- luku on ainoa jälki."""
    result = parse_result(
        stats=stats(
            grenades_without_thrower=2,
            grenades_outside_rounds=7,
            grenades_unknown_side=1,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Ilman heittäjää").startswith("2 lentorataa")
    assert field_value(output_text, "Ilman kierrosta").startswith("7 kranaattia")
    assert field_value(output_text, "Ilman puolta").startswith("1 kranaattia")


def test_a_clean_run_hides_the_dropped_grenade_lines() -> None:
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert "Ilman heittäjää" not in output_text
    assert "Ilman kierrosta" not in output_text
    assert "Ilman puolta" not in output_text


def test_unreadable_events_do_not_hide_the_other_counts() -> None:
    """Yksi rikki mennyt taulu ei saa viedä toisen lukuja."""
    numbers = stats()
    for key in (
        "event_rows",
        "utility_throws",
        "utility_detonations",
        "utility_rounds",
        "utility_area_observed",
        "utility_area_snapped",
        "utility_area_unnamed",
        "utility_without_area",
    ):
        numbers.pop(key)
    numbers["events_unreadable"] = "OSError: rikki"
    output_text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert field_value(output_text, "Kierrokset") == "21 (rivejä 42)"
    assert field_value(output_text, "Näytepisteet").startswith("78 ")
    assert field_value(output_text, "Utility").startswith("lukuja ei saatu")


def test_utility_lines_are_absent_when_the_result_was_unreadable() -> None:
    """Ilman lukuja ei keksitä nollaa -- se väittäisi tyhjää tulosta."""
    result = parse_result(skipped=True, stats={"unreadable": "OSError: rikki"})
    assert "Utility" not in _render_parse(result, regulation_rounds=24)


def test_all_three_output_tables_are_listed() -> None:
    result = parse_result(
        outputs=(
            PurePosixPath("parsed/1-abc-1/rounds.parquet"),
            PurePosixPath("parsed/1-abc-1/ticks.parquet"),
            PurePosixPath("parsed/1-abc-1/events.parquet"),
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert "rounds.parquet" in output_text
    assert "ticks.parquet" in output_text
    assert "events.parquet" in output_text


# --- Ostoikkuna (Story 1.9) ---------------------------------------------------


def test_the_measurement_point_is_always_named() -> None:
    """Ajo kertoo, mistä hetkestä talousluvut on luettu.

    Mittaushetki on asetus, joten kaksi eri arvolla ajettua tulosta ovat eri
    lukuja samannäköisessä taulussa. Ilman tätä riviä lukija ei voi tietää
    kumpaa hän katsoo.
    """
    text = _render_parse(
        parse_result(stats=stats(buy_window_seconds=20.0)), regulation_rounds=24
    )
    line = field_value(text, "Mittauspiste")
    assert "ostoajan lopusta" in line
    assert "20,0 s" in line


def test_a_zero_window_says_it_measured_the_anchor() -> None:
    """Ikkuna 0 mittaa freezetimen lopusta, ja se sanotaan sillä nimellä.

    "Ostoajan loppu, ikkuna 0,0 s" olisi totta mutta harhaanjohtavaa: juuri
    sen niminen mittaus oli se vika, jonka tämä tarina korjaa.
    """
    text = _render_parse(
        parse_result(stats=stats(buy_window_seconds=0.0)), regulation_rounds=24
    )
    assert "freezetimen lopusta" in field_value(text, "Mittauspiste")


def test_a_clean_death_cut_is_reported_as_zero_not_silence() -> None:
    """Nolla menetettyä ostosta sanotaan ääneen.

    Kuolema katkaisee ikkunan noin puolella kierroksista, joten katkaisujen
    määrä ei ole hälytys. Hälytys on se, jäikö ostoja katkaisun taakse -- ja
    vaiettu nolla ei erottuisi vaietusta viidestä.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=13,
                buy_window_purchases_after_cut=0,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Kuoleman katkaisu")
    assert "13 kierrosta" in line
    assert "yksikään osto ei jäänyt" in line


def test_a_purchase_lost_behind_the_cut_is_reported() -> None:
    """Menetetty ostos näkyy tulosteessa lukuna, ei pelkkänä katkaisumääränä."""
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=4,
                buy_window_purchases_after_cut=2,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Kuoleman katkaisu")
    assert "2 pelaajaa osti vielä katkaisun jälkeen" in line


def test_a_skipped_run_does_not_claim_a_clean_buy_window() -> None:
    """Ohitetusta ajosta lukuja ei ole, joten rivejä ei myöskään ole.

    Katkaisujen ja menetettyjen ostosten määrää ei voi lukea valmiista
    taulusta. "Ei yhtään" olisi väite, jota mikään ei tue -- sama sääntö kuin
    uudelleenaloituksilla ja tuntemattomilla esineillä.
    """
    numbers = {
        key: value
        for key, value in DEFAULT_STATS.items()
        if not key.startswith("buy_window_")
    }
    text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert "Mittauspiste" not in text
    assert "Kuoleman katkaisu" not in text


def test_an_unknown_buy_window_is_not_claimed_to_be_the_anchor() -> None:
    """Portti, joka ei kerro ikkunaa, ei saa näyttää ankkurimittaukselta.

    ``None`` ja ``0.0`` ovat eri asioita: jälkimmäinen on valinta, edellinen
    tietämättömyys. "Talous luettu freezetimen lopusta" olisi varma väite
    hetkestä, jota mikään ei tue.
    """
    text = _render_parse(
        parse_result(stats=stats(buy_window_seconds=None)), regulation_rounds=24
    )
    line = field_value(text, "Mittauspiste")
    assert "ei tiedossa" in line
    assert "freezetimen lopusta" not in line


def test_a_defaulted_tick_rate_makes_the_window_an_estimate() -> None:
    """Ikkuna lasketaan tickratesta, joten mittaamaton tickrate on kerrottava.

    Rivi tulostaa sekunnit yhtä varmasti kummassakin tapauksessa, joten ilman
    tätä lisäystä oletukseen nojaava 20,0 s näyttäisi mittaukselta.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0, tick_rate=64.0, tick_rate_measured=False
            )
        ),
        regulation_rounds=24,
    )
    assert "tickrate oletus" in field_value(text, "Mittauspiste")


def test_the_real_measurement_offsets_are_shown() -> None:
    """Asetus lupaa ikkunan pituuden; jakauma kertoo mihin mittaus osui.

    Se on ``buy_end_tick``-sarakkeen ainoa näkyvä muoto: jos ikkuna on 20 s
    mutta mediaani 12 s, kuolema katkaisee ikkunan useammin kuin ei.
    """
    text = _render_parse(
        parse_result(
            stats=stats(buy_window_seconds=20.0, buy_end_offsets_s=(3.5, 12.0, 20.0))
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Mittauspiste")
    assert "3,5 s-20,0 s" in line
    assert "mediaani 12,0 s" in line


def test_an_unchecked_cut_is_told_apart_from_a_clean_one() -> None:
    """Tarkistamatta jäänyt katkaisu sanotaan erikseen.

    Ilman sitä ``buy_window_purchases_after_cut``in nolla tarkoittaisi kahta
    eri asiaa: "mitään ei menetetty" ja "ei tiedetä".
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=6,
                buy_window_purchases_after_cut=0,
                buy_window_cuts_unchecked=2,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Kuoleman katkaisu")
    assert "yksikään osto ei jäänyt" in line
    assert "2 kierrosta ei voitu tarkistaa" in line


def test_a_lost_purchase_names_the_rounds() -> None:
    """Menetetty ostos on jäljitettävissä: rivi nimeää kierrokset.

    Yksi luku koko demolle ei anna käyttäjälle mitään mistä jatkaa.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=4,
                buy_window_purchases_after_cut=2,
                buy_window_rounds_with_lost_purchases=(7, 12),
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Kuoleman katkaisu")
    assert "2 pelaajaa osti vielä katkaisun jälkeen" in line
    assert "round_raw) 7, 12" in line


def test_the_singular_forms_are_finnish() -> None:
    """Luvulla 1 partitiivi taipuu: "1 kierros", "1 pelaaja".

    Yksi menetetty ostos on juuri se tapaus, jota rivi on kertomassa, joten
    "1 pelaajaa" olisi väärin juuri silloin kun rivi eniten merkitsee.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=1,
                buy_window_purchases_after_cut=1,
                buy_window_rounds_with_lost_purchases=(9,),
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Kuoleman katkaisu")
    assert "1 kierros mitattiin aiemmin" in line
    assert "1 pelaaja osti vielä" in line


def test_an_empty_buy_tick_is_reported_as_a_fault() -> None:
    """Tyhjä ostotick on vika, ja se saa oman rivinsä.

    Se on ainoa polku, jonka ``ParseDiagnostics`` merkitsee sanoilla "vika eikä
    havainto", ja ilman riviä mittaus olisi hiljaa palannut ankkuriin.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0, buy_window_ticks_without_players=2
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Ostoajan tick tyhjä")
    assert "2 kierrosta" in line
    assert "palautui freezetimen ankkuriin" in line


def test_players_lost_from_the_buy_tick_are_reported() -> None:
    """Kadonneet pelaajat ja kokonaan tyhjä joukkuerivi näkyvät samalla rivillä."""
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_players_lost=5,
                buy_window_sides_without_rows=1,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Kadonneet pelaajat")
    assert "5 pelaajaa" in line
    assert "1 joukkueriviä jäi kokonaan tyhjäksi" in line


def test_stale_equipment_gets_its_own_line() -> None:
    """Palautuksen jättämä vanhentunut varustearvo kerrotaan ja rajataan."""
    text = _render_parse(
        parse_result(
            stats=stats(buy_window_seconds=20.0, buy_window_stale_equipment=1)
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Vanhentunut arvo")
    assert "1 pelaaja" in line
    assert "1000 $/pelaaja" in line


def test_a_clean_run_does_not_print_the_fault_lines() -> None:
    """Nollat eivät toistu joka ajossa.

    Nämä neljä ovat vikoja eivätkä normaalia. Nollan toistaminen opettaisi
    lukijan ohittamaan rivin juuri ennen kuin se kerran merkitsee.
    """
    text = _render_parse(parse_result(), regulation_rounds=24)
    for label in (
        "Ostoajan tick tyhjä",
        "Kadonneet pelaajat",
        "Vanhentunut arvo",
    ):
        assert label not in text


def test_every_parse_label_fits_the_column() -> None:
    """Jokainen otsikko mahtuu sarakkeeseen, myös harvoin näkyvät.

    ``_line`` täyttää otsikon kiinteään leveyteen; liian pitkä otsikko syö
    välilyönnin ja arvo liimautuu siihen kiinni ("Vanhentunut varustearvo1
    pelaaja"). Vikarivit näkyvät vain kun jokin on rikki, joten ilman tätä
    testiä muotoiluvirhe paljastuisi vasta silloin kun rivin pitäisi olla
    selkein mahdollinen.
    """
    every = stats(
        buy_window_seconds=20.0,
        buy_window_truncated_by_death=3,
        buy_window_purchases_after_cut=1,
        buy_window_rounds_with_lost_purchases=(4,),
        buy_window_cuts_unchecked=1,
        buy_window_ticks_without_players=1,
        buy_window_players_lost=2,
        buy_window_sides_without_rows=1,
        buy_window_stale_equipment=1,
        armed_unknown_items=(("Tuntematon Ase", 3),),
    )
    text = _render_parse(parse_result(stats=every), regulation_rounds=24)

    for line in text.splitlines():
        if not line.startswith("  ") or ":" in line[:4]:
            continue
        body = line[2:]
        label = body.rstrip()
        if "  " not in body:
            continue
        head = body.split("  ", 1)[0]
        assert len(head) <= _PARSE_LABEL_WIDTH - 1, (
            f"otsikko {head!r} on {len(head)} merkkiä; sarakkeeseen mahtuu "
            f"{_PARSE_LABEL_WIDTH - 1}"
        )
        assert label
