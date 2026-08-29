"""``domain.economy`` -- loss count ja kierrostyypin luokittelu ilman demoja.

Nämä testit ovat I/O-matriisin rivit yksi kerrallaan käsin rakennetuilla
tauluilla. Yksikään ei tarvitse demotiedostoa, joten ``pytest -m "not demo"``
kattaa koko luokittelulogiikan.

Kynnykset luetaan **oikeasta** ``settings.toml``ista: testi, joka keksisi omat
rajansa, ei todistaisi mitään siitä asetustiedostosta, jolla työkalu ajetaan.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from pappascout.domain.economy import (
    INPUT_FIELDS,
    Decision,
    available_money,
    classify_round,
    loss_counts,
    per_player,
)
from pappascout.domain.models import ThresholdSettings, load_settings
from pappascout.domain.schemas import CLASSIFIED_INPUTS
from pappascout.errors import SchemaError


@pytest.fixture
def kynnykset(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


def rivi(**muutokset) -> dict:
    """Kierrosrivi oletusarvoilla; testi muuttaa vain sen mitä tutkii.

    Oletus on täysi osto viidellä pelaajalla: 25 000 $ / 5 = 5 000 $/pelaaja.
    """
    oletus = {
        "round_no": 5,
        "side": "T",
        "won": True,
        "status": "ok",
        "money_freeze_end": 5000,
        "money_spent": 20000,
        "equip_freeze_end": 25000,
        "equip_round_start": 5000,
        "players_freeze_end": 5,
        "survivors_equip_prev": 0,
        "survivors": 0,
    }
    oletus.update(muutokset)
    return oletus


def edellinen(won: bool | None = False, *, round_no: int = 4, **muutokset) -> dict:
    """Edellinen kierros: oletuksena tasan yhtä pienempi numero, sama puoli."""
    oletus = rivi(round_no=round_no, won=won, survivors=0)
    oletus.update(muutokset)
    return oletus


def team_frame(kierrokset: list[tuple[int, str, bool | None]]) -> pl.DataFrame:
    """Yhden joukkueen rivit ``(round_no, side, won)``-kolmikoista."""
    return pl.DataFrame(
        [{"round_no": no, "side": side, "won": won} for no, side, won in kierrokset],
        schema={"round_no": pl.Int32, "side": pl.Utf8, "won": pl.Boolean},
    )


# --- Loss count ----------------------------------------------------------------


def test_half_starts_at_one_and_climbs_with_losses(kynnykset) -> None:
    df = team_frame([(1, "T", False), (2, "T", False), (3, "T", False)])
    assert loss_counts(df, kynnykset) == [1, 2, 3]


def test_a_win_steps_the_counter_down_by_one(kynnykset) -> None:
    """Voitto laskee laskuria yhdellä portaalla, ei nollaa sitä."""
    df = team_frame([(1, "T", False), (2, "T", False), (3, "T", True), (4, "T", True)])
    assert loss_counts(df, kynnykset) == [1, 2, 3, 2]


def test_counter_is_clamped_to_the_configured_range(kynnykset) -> None:
    haviot = [(no, "T", False) for no in range(1, 9)]
    tulos = loss_counts(team_frame(haviot), kynnykset)
    assert max(tulos) == kynnykset.loss_count_max
    voitot = [(1, "T", False)] + [(no, "T", True) for no in range(2, 8)]
    assert min(loss_counts(team_frame(voitot), kynnykset)) == kynnykset.loss_count_min


def test_new_half_is_detected_from_the_side_swap_not_the_round_number(
    kynnykset,
) -> None:
    """I/O-matriisi: puoli vaihtuu 12 -> 13, joten loss count palaa yhteen."""
    kierrokset = [(no, "T", False) for no in range(1, 13)]
    kierrokset += [(no, "CT", False) for no in range(13, 16)]
    tulos = loss_counts(team_frame(kierrokset), kynnykset)
    assert tulos[11] == kynnykset.loss_count_max  # kierros 12, kattoon asti
    assert tulos[12] == kynnykset.loss_count_half_start  # kierros 13
    assert tulos[13] == 2


def test_overtime_side_swap_also_starts_a_new_half(kynnykset) -> None:
    kierrokset = [(24, "CT", False), (25, "CT", False), (26, "T", False)]
    assert loss_counts(team_frame(kierrokset), kynnykset) == [1, 2, 1]


def test_an_unresolved_round_does_not_move_the_counter(kynnykset) -> None:
    """Arvaus siirtäisi kaikkia seuraavia kierroksia."""
    df = team_frame([(1, "T", False), (2, "T", None), (3, "T", False)])
    assert loss_counts(df, kynnykset) == [1, 2, 2]


def test_empty_frame_gives_no_counters(kynnykset) -> None:
    assert loss_counts(team_frame([]), kynnykset) == []


def test_missing_column_is_a_schema_error(kynnykset) -> None:
    df = team_frame([(1, "T", False)]).drop("won")
    with pytest.raises(SchemaError, match="won"):
        loss_counts(df, kynnykset)


def test_unordered_rounds_are_refused(kynnykset) -> None:
    df = team_frame([(2, "T", False), (1, "T", False)])
    with pytest.raises(SchemaError, match="nousevassa"):
        loss_counts(df, kynnykset)


def test_duplicate_round_is_refused(kynnykset) -> None:
    """Kaksi riviä samalle kierrokselle tarkoittaisi molempia joukkueita."""
    df = team_frame([(1, "T", False), (1, "CT", True)])
    with pytest.raises(SchemaError, match="nousevassa"):
        loss_counts(df, kynnykset)


def test_unnumbered_round_is_refused(kynnykset) -> None:
    df = team_frame([(1, "T", False)]).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no")
    )
    with pytest.raises(SchemaError, match="round_no"):
        loss_counts(df, kynnykset)


def test_empty_side_is_refused_instead_of_silently_resetting(kynnykset) -> None:
    """Tyhjä puoli näyttäisi puolen vaihdolta ja nollaisi laskurin äänettömästi."""
    df = team_frame([(1, "T", False), (2, None, False), (3, "T", False)])
    with pytest.raises(SchemaError, match="side"):
        loss_counts(df, kynnykset)


# --- Kierrostyyppi: kierrosnumeron säännöt --------------------------------------


@pytest.mark.parametrize("round_no", [1, 13])
def test_pistol_round_is_decided_by_the_round_number(kynnykset, round_no) -> None:
    """Pistooli ratkeaa numerosta ennen kuin rahaa katsotaan lainkaan."""
    paatos = classify_round(
        rivi(round_no=round_no, equip_freeze_end=25000),
        edellinen(won=True, round_no=round_no - 1),
        kynnykset,
        loss_count=1,
    )
    assert paatos.round_type == "pistol"
    assert "pistoolikierros" in paatos.reason


@pytest.mark.parametrize("round_no", [25, 26, 27, 28])
def test_overtime_round_gets_no_economy_reasoning(kynnykset, round_no) -> None:
    paatos = classify_round(
        rivi(round_no=round_no, equip_freeze_end=1000, money_freeze_end=60000),
        edellinen(won=False, round_no=round_no - 1),
        kynnykset,
        loss_count=4,
    )
    assert paatos.round_type == "ot"
    assert "jatkoaikaa" in paatos.reason


def test_regulation_round_is_never_overtime(kynnykset) -> None:
    paatos = classify_round(
        rivi(round_no=24), edellinen(True, round_no=23), kynnykset, loss_count=1
    )
    assert paatos.round_type != "ot"


# --- Kierrostyyppi: talous ------------------------------------------------------


def test_full_buy_is_decided_from_the_equipment_value(kynnykset) -> None:
    paatos = classify_round(
        rivi(equip_freeze_end=5 * kynnykset.full_equip_min),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "full"


def test_full_buy_wins_over_the_after_loss_rules(kynnykset) -> None:
    """Täysi osto ratkeaa varustearvosta, vaikka edellinen olisi hävitty."""
    paatos = classify_round(
        rivi(equip_freeze_end=25000, money_freeze_end=100),
        edellinen(won=False),
        kynnykset,
        loss_count=4,
    )
    assert paatos.round_type == "full"


def test_eco_after_a_loss_when_the_team_did_not_buy(kynnykset) -> None:
    """Todennettu tilanne: pistoolihäviön jälkeen säästö, raha jää kassaan."""
    paatos = classify_round(
        rivi(
            round_no=2,
            equip_freeze_end=2000,
            equip_round_start=1000,
            money_freeze_end=10100,
            money_spent=600,
        ),
        edellinen(won=False, round_no=1),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "eco"
    # I/O-matriisi: perustelu kertoo rahan ja loss countin.
    assert "loss count 2" in paatos.reason
    assert "$/pelaaja" in paatos.reason


def test_force_after_a_loss_when_the_team_bought_itself_empty(kynnykset) -> None:
    """I/O-matriisi: ostettiin tyhjäksi -- 2 380 $/pelaaja, saldoa jäljellä 30 $."""
    paatos = classify_round(
        rivi(
            round_no=23,
            equip_freeze_end=12900,
            equip_round_start=1000,
            money_freeze_end=150,
            money_spent=11900,
        ),
        edellinen(won=False, round_no=22),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "force"
    assert "ostettu tyhjäksi" in paatos.reason
    # S2: perustelu nimeää molemmat vertailuun käytetyt arvot.
    assert str(kynnykset.force_buy_min) in paatos.reason
    assert str(kynnykset.force_money_left_max) in paatos.reason


def test_half_after_a_loss_when_the_team_left_money_in_the_pocket(kynnykset) -> None:
    """I/O-matriisi: ostettiin ja jätettiin varaa -- sama ostos, eri saldo.

    S2: force ja puoliosto eroavat **taskuun jätetystä rahasta**. Tämä on
    tasan edellisen testin pari: ostos on sama, mutta rahaa jäi seuraavalle
    kierrokselle.
    """
    yhteiset = dict(
        round_no=23, equip_freeze_end=12900, equip_round_start=1000, money_spent=11900
    )
    tyhjaksi = classify_round(
        rivi(money_freeze_end=5 * kynnykset.force_money_left_max, **yhteiset),
        edellinen(won=False, round_no=22),
        kynnykset,
        loss_count=2,
    )
    varaa = classify_round(
        rivi(money_freeze_end=5 * (kynnykset.force_money_left_max + 100), **yhteiset),
        edellinen(won=False, round_no=22),
        kynnykset,
        loss_count=2,
    )
    assert tyhjaksi.round_type == "force"
    assert varaa.round_type == "half"
    assert "jätettiin varaa" in varaa.reason


def test_the_purchase_threshold_is_inclusive_at_exactly_the_limit(
    kynnykset,
) -> None:
    """``>=``, ei ``>``: tasan rajalla oleva ostos on jo ostos.

    Rajan molemmat naapurit on pinnattu muualla; tämä pinnaa itse rajan.
    Ilman tätä ``>=`` voi vaihtua merkiksi ``>`` ilman että mikään huomauttaa.
    """
    def paatos(ostettu_pp: int) -> str | None:
        return classify_round(
            rivi(
                equip_freeze_end=5 * (ostettu_pp + 300),
                equip_round_start=5 * 300,
                money_freeze_end=5 * 100,
            ),
            edellinen(won=False),
            kynnykset,
            loss_count=2,
        ).round_type

    assert paatos(kynnykset.force_buy_min) == "force"
    assert paatos(kynnykset.force_buy_min - 1) == "eco"


def test_the_money_left_threshold_is_inclusive_at_exactly_the_limit(
    kynnykset,
) -> None:
    """``<=``, ei ``<``: tasan rajalle jäänyt raha on yhä "ostettu tyhjäksi"."""
    def paatos(jaljella_pp: int) -> str | None:
        return classify_round(
            rivi(
                equip_freeze_end=5 * 2000,
                equip_round_start=5 * 300,
                money_freeze_end=5 * jaljella_pp,
            ),
            edellinen(won=False),
            kynnykset,
            loss_count=2,
        ).round_type

    assert paatos(kynnykset.force_money_left_max) == "force"
    assert paatos(kynnykset.force_money_left_max + 1) == "half"


def test_the_reason_never_contradicts_its_own_rounded_number(kynnykset) -> None:
    """P13: vertailu ja perustelun luku ovat sama pyöristetty luku.

    Pyöristämätön vertailu tuottaisi tekstin "taskuun jäi 1000 $/pelaaja eli
    yli 1000 $" -- juuri siinä rajatapauksessa, jonka lukija haluaa tarkistaa.
    """
    raja = kynnykset.force_money_left_max
    paatos = classify_round(
        rivi(
            equip_freeze_end=5 * 2000,
            equip_round_start=5 * 300,
            money_freeze_end=5 * raja + 2,  # 1000,4 $/pelaaja -> pyöristyy 1000:een
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "force"
    assert f"taskuun jäi vain {raja} $/pelaaja eli enintään {raja} $" in paatos.reason


def test_a_poor_team_that_did_not_buy_is_an_eco_not_a_force(kynnykset) -> None:
    """I/O-matriisi: köyhä joukkue -- kassa tyhjä, mutta ostos jäi rajan alle.

    Pelkkä "raha loppui" ei ole force: panssarin ja pistoolin viimeisillä
    rahoillaan ostava joukkue tyhjensi kassan mutta ei forcannut. Siksi
    ``force_buy_min`` on forcen **edellytys**, ei vain sen kaista.
    """
    paatos = classify_round(
        rivi(
            equip_freeze_end=5 * 1200,
            equip_round_start=5 * 300,
            money_freeze_end=50,
            money_spent=4500,
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=3,
    )
    assert paatos.round_type == "eco"
    assert str(kynnykset.force_buy_min) in paatos.reason


def test_force_and_eco_differ_only_by_what_was_bought(kynnykset) -> None:
    """Sama varustearvo, eri ostos: erottava havainto on ostettu summa."""
    ostettu = classify_round(
        rivi(equip_freeze_end=9000, equip_round_start=1000, money_freeze_end=500),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    saastetty = classify_round(
        rivi(equip_freeze_end=9000, equip_round_start=8000, money_freeze_end=500),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert ostettu.round_type == "force"
    assert saastetty.round_type == "eco"


def test_a_large_purchase_below_full_is_still_a_force(kynnykset) -> None:
    """Forcella ei ole ylärajaa: ylhäältä rajaa ``full_equip_min``.

    Kalibrointidemon kierros 20 (2 710 $/pelaaja ostettu, 2 910 varusteita)
    putosi vanhan kaistan yläpuolelle ja luokittui poikkeamaksi. Kaista
    poistui, joten sama tilanne on nyt force.
    """
    ostos_pp = 2710
    paatos = classify_round(
        rivi(
            equip_freeze_end=5 * 2910,
            equip_round_start=5 * (2910 - ostos_pp),
            money_freeze_end=5 * 270,
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "force"


def test_equipment_value_alone_does_not_make_a_half_buy(kynnykset) -> None:
    """S2: puoliosto ei ratkea varustearvosta vaan taskuun jääneestä rahasta.

    Sama varustearvo, sama ostos, eri saldo -- ja tulos on eri. Jos joku
    kytkee varustearvorajan takaisin puolioston päätökseen, tämä testi kaatuu.
    """
    yhteiset = dict(equip_freeze_end=5 * 3500, equip_round_start=1000)
    tyhjaksi = classify_round(
        rivi(money_freeze_end=5 * 200, **yhteiset),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    varaa = classify_round(
        rivi(money_freeze_end=5 * 2500, **yhteiset),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert tyhjaksi.round_type == "force"
    assert varaa.round_type == "half"


def test_a_half_buy_is_never_played_after_a_win(kynnykset) -> None:
    """S1: säästö on aina reaktio häviöön, joten voiton jälkeen on normaali osto.

    Kalibroinnin kierros 2: pistoolin voittanut CT ostaa 3 200 $/pelaaja.
    Vanha luokittelija sanoi ``half``; Veeti sanoo ``full``.
    """
    paatos = classify_round(
        rivi(round_no=2, equip_freeze_end=16000, equip_round_start=1100),
        edellinen(won=True, round_no=1),
        kynnykset,
        loss_count=1,
    )
    assert paatos.round_type == "full"
    assert "voitetun kierroksen jälkeen" in paatos.reason


def test_low_value_after_a_win_is_an_anomaly_not_an_eco(kynnykset) -> None:
    paatos = classify_round(
        rivi(
            equip_freeze_end=5 * kynnykset.anomaly_equip_max_after_win,
            equip_round_start=1000,
            money_freeze_end=50000,
        ),
        edellinen(won=True),
        kynnykset,
        loss_count=1,
    )
    assert paatos.round_type == "anomaly"
    assert "voiton jälkeen" in paatos.reason


def test_there_is_no_gap_left_after_a_win(kynnykset) -> None:
    """S1: voiton jälkeen on vain normaali osto tai poikkeama, ei väliä.

    Vanha luokittelija jätti poikkeamarajan ja puoliostorajan väliin aukon,
    joka putosi poikkeamaksi. Testi ajetaan koko sillä välillä, joka jää
    poikkeamarajan ja täyden oston väliin.
    """
    ala = kynnykset.anomaly_equip_max_after_win
    yla = kynnykset.full_equip_min
    for varusteet_pp in (ala + 1, (ala + yla) // 2, yla - 1):
        paatos = classify_round(
            rivi(
                equip_freeze_end=5 * varusteet_pp,
                equip_round_start=5 * (varusteet_pp - 100),
            ),
            edellinen(won=True),
            kynnykset,
            loss_count=1,
        )
        assert paatos.round_type == "full", varusteet_pp


def test_a_saved_rifle_does_not_turn_an_eco_into_a_buy(kynnykset) -> None:
    """S3: säästetty ase nostaa varustearvoa, mutta ei ole ostos.

    Kalibroinnin kierros 11 CT: yksi säästetty M4, ostettu 600 $/pelaaja.
    Veeti sanoo ``eco`` -- korkea varustearvo ei saa kääntää sitä ostokseksi.
    """
    paatos = classify_round(
        rivi(
            equip_freeze_end=5 * 1580,
            equip_round_start=5 * (1580 - 600),
            money_freeze_end=5 * 2280,
            money_spent=5 * 600,
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=3,
    )
    assert paatos.round_type == "eco"
    assert "säästetty kalusto ei ole" in paatos.reason


def test_negative_purchase_is_an_anomaly_not_silenced_to_zero(kynnykset) -> None:
    """Varustearvon lasku ei ole ostotapahtuma; nolla piilottaisi ristiriidan."""
    paatos = classify_round(
        rivi(equip_freeze_end=10000, equip_round_start=14000),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "anomaly"
    assert "laski" in paatos.reason


def test_a_negative_purchase_beats_the_full_buy_rule(kynnykset) -> None:
    """I/O-matriisin rivi on ehdoton: negatiivinen ostos -> anomaly.

    Korkea varustearvo ei saa peittää ristiriitaista havaintoa. Jos
    täyden oston tarkistus siirtyisi tämän eteen, kierros luokittuisi
    fulliksi ja rikkinäinen havainto katoaisi näkyvistä.
    """
    paatos = classify_round(
        rivi(
            equip_freeze_end=5 * (kynnykset.full_equip_min + 1000),
            equip_round_start=5 * (kynnykset.full_equip_min + 2000),
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "anomaly"
    assert "laski" in paatos.reason


def test_a_small_negative_purchase_is_not_rounded_away(kynnykset) -> None:
    """Merkki luetaan joukkuesummasta, ei pyöristetystä per pelaaja -luvusta.

    Kahden dollarin lasku viidellä pelaajalla pyöristyy nollaan per pelaaja.
    Se on silti ristiriitainen havainto, eikä sitä saa vaimentaa.
    """
    paatos = classify_round(
        rivi(equip_freeze_end=9998, equip_round_start=10000),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "anomaly"
    assert "-2 $ joukkueena" in paatos.reason


# --- Edellisen kierroksen jatkuvuus ---------------------------------------------


def test_missing_previous_round_is_an_anomaly(kynnykset) -> None:
    paatos = classify_round(rivi(equip_freeze_end=5000), None, kynnykset, loss_count=1)
    assert paatos.round_type == "anomaly"
    assert "edelliseen kierrokseen" in paatos.reason


def test_a_full_buy_is_recognised_even_without_a_previous_round(kynnykset) -> None:
    """Tietoinen poikkeus kalibrointidokumentin johdetusta järjestyksestä.

    5 000 $/pelaaja on täysi osto riippumatta siitä, tunnetaanko edellinen
    kierros. Puoliajan ensimmäisellä kierroksella ja kierrosnumeroiden aukossa
    edellistä ei ole, ja ``anomaly`` väittäisi siellä ilmiselvästä täydestä
    ostosta, ettei sitä voi luokitella. Edellistä tarvitaan vain econ, forcen
    ja puolioston erottamiseen toisistaan.
    """
    paatos = classify_round(
        rivi(equip_freeze_end=5 * kynnykset.full_equip_min),
        None,
        kynnykset,
        loss_count=1,
    )
    assert paatos.round_type == "full"
    assert paatos.inputs["prev_round_won"] is None


def test_a_gap_in_round_numbers_breaks_the_previous_round(kynnykset) -> None:
    """``rivit[index - 1]`` ei ole edellinen kierros, jos numeroissa on aukko."""
    paatos = classify_round(
        rivi(round_no=8, equip_freeze_end=5000),
        edellinen(won=False, round_no=5),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "anomaly"
    assert paatos.inputs["prev_round_won"] is None


def test_a_side_change_breaks_the_previous_round(kynnykset) -> None:
    """Puolen vaihtuminen tarkoittaa, että edellinen kierros on toiselta puoliajalta."""
    paatos = classify_round(
        rivi(round_no=14, side="CT", equip_freeze_end=5000),
        edellinen(won=False, round_no=13, side="T"),
        kynnykset,
        loss_count=1,
    )
    assert paatos.round_type == "anomaly"
    assert paatos.inputs["survivors_prev"] is None


def test_a_contiguous_previous_round_is_used(kynnykset) -> None:
    paatos = classify_round(
        rivi(
            round_no=14,
            side="CT",
            equip_freeze_end=5 * 3500,
            money_freeze_end=5 * 2500,
        ),
        edellinen(won=False, round_no=13, side="CT", survivors=2),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "half"
    assert paatos.inputs["prev_round_won"] is False
    assert paatos.inputs["survivors_prev"] == 2


# --- Vajaa joukkue ja puuttuvat havainnot ---------------------------------------


def test_per_player_values_use_the_observed_player_count(kynnykset) -> None:
    """Neljällä pelaajalla sama joukkuesumma ylittää täyden oston rajan."""
    summa = 4 * kynnykset.full_equip_min
    nelja = classify_round(
        rivi(equip_freeze_end=summa, players_freeze_end=4),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    viisi = classify_round(
        rivi(equip_freeze_end=summa, players_freeze_end=5),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert nelja.round_type == "full"
    assert viisi.round_type != "full"
    assert nelja.inputs["players"] == 4
    assert nelja.inputs["players_readable"] == 4
    assert "vain 4 pelaajan arvot" in nelja.reason


def test_unknown_player_count_falls_back_and_says_so(kynnykset) -> None:
    """I/O-matriisi: jos määrä ei ole tiedossa, se kirjataan perusteluun."""
    paatos = classify_round(
        rivi(players_freeze_end=None), edellinen(won=False), kynnykset, loss_count=2
    )
    assert paatos.inputs["players"] == kynnykset.roster_size
    assert paatos.inputs["players_readable"] is None
    assert "roster_size" in paatos.reason


@pytest.mark.parametrize("havaittu", [0, -1, 6, 11])
def test_player_count_outside_the_roster_is_refused_as_a_divisor(
    kynnykset, havaittu
) -> None:
    """Ylimääräinen tai vanhentunut rivi tickissä aliarvioisi per pelaaja -arvot."""
    paatos = classify_round(
        rivi(players_freeze_end=havaittu),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.inputs["players"] == kynnykset.roster_size
    assert paatos.inputs["players_readable"] == havaittu
    assert "sallitun välin" in paatos.reason


def test_round_without_a_freeze_anchor_is_not_classified(kynnykset) -> None:
    paatos = classify_round(
        rivi(
            status="no_freeze_end",
            money_freeze_end=None,
            money_spent=None,
            equip_freeze_end=None,
            equip_round_start=None,
            players_freeze_end=None,
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type is None
    assert "no_freeze_end" in paatos.reason


def test_missing_observation_without_a_status_is_not_classified(kynnykset) -> None:
    paatos = classify_round(
        rivi(equip_freeze_end=None), edellinen(won=False), kynnykset, loss_count=2
    )
    assert paatos.round_type is None
    assert "varustearvo" in paatos.reason


def test_missing_round_start_equipment_is_not_classified(kynnykset) -> None:
    """Nollana luettuna koko varustearvo näyttäisi tällä kierroksella ostetulta.

    Silloin aito säästö saisi tuomion "force -- ostettu lähes tyhjäksi", eli
    tasan päinvastoin kuin demossa tapahtui.
    """
    paatos = classify_round(
        rivi(equip_freeze_end=9000, equip_round_start=None),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type is None
    assert "kierroksen alun varustearvo" in paatos.reason


def test_zero_roster_size_is_refused_instead_of_dividing_by_zero(kynnykset) -> None:
    nolla = kynnykset.model_copy(update={"roster_size": 0})
    with pytest.raises(SchemaError, match="roster_size"):
        classify_round(rivi(), edellinen(won=False), nolla, loss_count=2)


# --- Perustelu ja lähtöarvot ----------------------------------------------------


def test_every_decision_carries_money_and_loss_count_in_its_reason(
    kynnykset,
) -> None:
    """Ilman rahaa ja loss countia kalibrointi Story 1.4:ssä olisi mahdotonta."""
    tapaukset = [
        (rivi(round_no=1), edellinen(True, round_no=0), 1),
        (rivi(round_no=25), edellinen(False, round_no=24), 3),
        (rivi(), edellinen(False), 2),
        (
            rivi(equip_freeze_end=2000, equip_round_start=1000),
            edellinen(False),
            2,
        ),
        (rivi(equip_freeze_end=2000), edellinen(True), 1),
    ]
    for r, e, lc in tapaukset:
        paatos = classify_round(r, e, kynnykset, loss_count=lc)
        assert "Käytettävissä" in paatos.reason, paatos
        assert "jäljellä" in paatos.reason, paatos
        assert f"loss count {lc}" in paatos.reason, paatos


def test_eco_reason_names_the_purchase_it_compared_against(kynnykset) -> None:
    """Eco ratkeaa ostetusta summasta, ja perustelu näyttää molemmat luvut.

    Perustelu kertoo silti myös rahan molemmat suunnat, jotta lukija ei sekoita
    jäljelle jäänyttä saldoa käytettävissä olleeseen rahaan.
    """
    paatos = classify_round(
        rivi(
            round_no=2,
            equip_freeze_end=1600,
            equip_round_start=1000,
            money_freeze_end=9000,
            money_spent=600,
        ),
        edellinen(won=False, round_no=1),
        kynnykset,
        loss_count=2,
    )
    assert paatos.round_type == "eco"
    matala = paatos.reason.lower()
    assert f"ostettu vain {per_player(600, 5)} $/pelaaja" in matala
    assert f"alle forcen edellytyksen {kynnykset.force_buy_min} $" in matala
    assert f"käytettävissä {per_player(9000 + 600, 5)} $/pelaaja" in matala
    assert "jäljellä 1800 $/pelaaja" in matala


def test_available_money_is_the_sum_of_left_and_spent() -> None:
    assert available_money({"money_freeze_end": 100, "money_spent": 900}) == 1000
    assert available_money({"money_freeze_end": None, "money_spent": None}) is None
    assert available_money({"money_freeze_end": 100, "money_spent": None}) == 100


def test_inputs_match_the_classified_schema_exactly(kynnykset) -> None:
    """``inputs`` on skeemasopimus, ei vapaa sanakirja."""
    paatos = classify_round(rivi(), edellinen(False), kynnykset, loss_count=2)
    assert tuple(paatos.inputs) == INPUT_FIELDS
    assert set(paatos.inputs) == {f.name for f in CLASSIFIED_INPUTS.fields}


def test_inputs_carry_every_threshold_the_rules_compare_against(kynnykset) -> None:
    paatos = classify_round(rivi(), edellinen(False), kynnykset, loss_count=2)
    assert paatos.inputs["full_equip_min"] == kynnykset.full_equip_min
    assert paatos.inputs["force_buy_min"] == kynnykset.force_buy_min
    assert paatos.inputs["force_money_left_max"] == kynnykset.force_money_left_max
    assert (
        paatos.inputs["anomaly_equip_max_after_win"]
        == kynnykset.anomaly_equip_max_after_win
    )


def test_inputs_no_longer_carry_the_retired_thresholds(kynnykset) -> None:
    """Poistuneet kynnykset poistuivat kaikkialta, myös lähtöarvoista.

    Puolittainen siivous jättäisi taulun sarakkeen, jolla ei ole lukijaa --
    ja seuraava lukija luulisi sen kertovan jotain päätöksestä.
    """
    paatos = classify_round(rivi(), edellinen(False), kynnykset, loss_count=2)
    for poistunut in (
        "eco_money_max",
        "eco_money_max_low_loss",
        "eco_loss_count_min",
        "eco_money_max_applied",
        "force_money_min",
        "force_money_max",
        "half_equip_min",
    ):
        assert poistunut not in paatos.inputs
        assert not hasattr(kynnykset, poistunut)


def test_inputs_carry_the_previous_round_state(kynnykset) -> None:
    paatos = classify_round(
        rivi(survivors_equip_prev=4200),
        edellinen(won=True, survivors=3),
        kynnykset,
        loss_count=1,
    )
    assert paatos.inputs["prev_round_won"] is True
    assert paatos.inputs["survivors_prev"] == 3
    assert paatos.inputs["survivors_equip_prev"] == 4200


def test_bought_and_available_are_recoverable_without_new_columns(kynnykset) -> None:
    """Molemmat johdokset saa ``inputs``-rakenteesta ilman skeemamuutosta."""
    paatos = classify_round(
        rivi(
            equip_freeze_end=12000,
            equip_round_start=1000,
            money_freeze_end=400,
            money_spent=11000,
        ),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    assert (
        paatos.inputs["equip_freeze_end"] - paatos.inputs["equip_round_start"] == 11000
    )
    assert paatos.inputs["money_freeze_end"] + paatos.inputs["money_spent"] == 11400


def test_per_player_rounds_the_same_way_everywhere(kynnykset) -> None:
    """Sama luku ei saa poiketa dollarilla taulukossa ja perustelussa."""
    paatos = classify_round(
        rivi(equip_freeze_end=12345, equip_round_start=1000),
        edellinen(won=False),
        kynnykset,
        loss_count=2,
    )
    odotettu = per_player(12345, 5)
    assert f"{odotettu} $/pelaaja" in paatos.reason


def test_decision_unpacks_as_a_triple(kynnykset) -> None:
    round_type, reason, inputs = classify_round(
        rivi(), edellinen(False), kynnykset, loss_count=2
    )
    assert isinstance(round_type, str)
    assert isinstance(reason, str)
    assert isinstance(inputs, dict)
    assert isinstance(
        classify_round(rivi(), edellinen(False), kynnykset, loss_count=2), Decision
    )


def test_a_threshold_change_changes_the_verdict(kynnykset) -> None:
    """Kynnykset ovat asetuksia, eivät koodia: sama rivi, eri raja, eri tulos."""
    tiukempi = kynnykset.model_copy(update={"full_equip_min": 6000})
    r = rivi(equip_freeze_end=25000, equip_round_start=1000)
    assert classify_round(r, edellinen(False), kynnykset, loss_count=2).round_type == (
        "full"
    )
    assert classify_round(r, edellinen(False), tiukempi, loss_count=2).round_type != (
        "full"
    )
