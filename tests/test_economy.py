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
def thresholds(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


def row(**overrides) -> dict:
    """Kierrosrivi oletusarvoilla; testi muuttaa vain sen mitä tutkii.

    Oletus on täysi osto viidellä pelaajalla: 25 000 $ / 5 = 5 000 $/pelaaja.
    """
    defaults = {
        "round_no": 5,
        "side": "T",
        "won": True,
        "status": "ok",
        "money_buy_end": 5000,
        "money_spent": 20000,
        "equip_buy_end": 25000,
        "equip_round_start": 5000,
        "players_buy_end": 5,
        "survivors_equip_prev": 0,
        "survivors": 0,
    }
    defaults.update(overrides)
    return defaults


def previous(won: bool | None = False, *, round_no: int = 4, **overrides) -> dict:
    """Edellinen kierros: oletuksena tasan yhtä pienempi numero, sama puoli."""
    defaults = row(round_no=round_no, won=won, survivors=0)
    defaults.update(overrides)
    return defaults


def team_frame(rounds: list[tuple[int, str, bool | None]]) -> pl.DataFrame:
    """Yhden joukkueen rivit ``(round_no, side, won)``-kolmikoista."""
    return pl.DataFrame(
        [{"round_no": no, "side": side, "won": won} for no, side, won in rounds],
        schema={"round_no": pl.Int32, "side": pl.Utf8, "won": pl.Boolean},
    )


# --- Loss count ----------------------------------------------------------------


def test_half_starts_at_one_and_climbs_with_losses(thresholds) -> None:
    df = team_frame([(1, "T", False), (2, "T", False), (3, "T", False)])
    assert loss_counts(df, thresholds) == [1, 2, 3]


def test_a_win_steps_the_counter_down_by_one(thresholds) -> None:
    """Voitto laskee laskuria yhdellä portaalla, ei nollaa sitä."""
    df = team_frame([(1, "T", False), (2, "T", False), (3, "T", True), (4, "T", True)])
    assert loss_counts(df, thresholds) == [1, 2, 3, 2]


def test_counter_is_clamped_to_the_configured_range(thresholds) -> None:
    losses = [(no, "T", False) for no in range(1, 9)]
    result = loss_counts(team_frame(losses), thresholds)
    assert max(result) == thresholds.loss_count_max
    wins = [(1, "T", False)] + [(no, "T", True) for no in range(2, 8)]
    assert min(loss_counts(team_frame(wins), thresholds)) == thresholds.loss_count_min


def test_new_half_is_detected_from_the_side_swap_not_the_round_number(
    thresholds,
) -> None:
    """I/O-matriisi: puoli vaihtuu 12 -> 13, joten loss count palaa yhteen."""
    rounds = [(no, "T", False) for no in range(1, 13)]
    rounds += [(no, "CT", False) for no in range(13, 16)]
    result = loss_counts(team_frame(rounds), thresholds)
    assert result[11] == thresholds.loss_count_max  # kierros 12, kattoon asti
    assert result[12] == thresholds.loss_count_half_start  # kierros 13
    assert result[13] == 2


def test_overtime_side_swap_also_starts_a_new_half(thresholds) -> None:
    rounds = [(24, "CT", False), (25, "CT", False), (26, "T", False)]
    assert loss_counts(team_frame(rounds), thresholds) == [1, 2, 1]


def test_an_unresolved_round_does_not_move_the_counter(thresholds) -> None:
    """Arvaus siirtäisi kaikkia seuraavia kierroksia."""
    df = team_frame([(1, "T", False), (2, "T", None), (3, "T", False)])
    assert loss_counts(df, thresholds) == [1, 2, 2]


def test_empty_frame_gives_no_counters(thresholds) -> None:
    assert loss_counts(team_frame([]), thresholds) == []


def test_missing_column_is_a_schema_error(thresholds) -> None:
    df = team_frame([(1, "T", False)]).drop("won")
    with pytest.raises(SchemaError, match="won"):
        loss_counts(df, thresholds)


def test_unordered_rounds_are_refused(thresholds) -> None:
    df = team_frame([(2, "T", False), (1, "T", False)])
    with pytest.raises(SchemaError, match="nousevassa"):
        loss_counts(df, thresholds)


def test_duplicate_round_is_refused(thresholds) -> None:
    """Kaksi riviä samalle kierrokselle tarkoittaisi molempia joukkueita."""
    df = team_frame([(1, "T", False), (1, "CT", True)])
    with pytest.raises(SchemaError, match="nousevassa"):
        loss_counts(df, thresholds)


def test_unnumbered_round_is_refused(thresholds) -> None:
    df = team_frame([(1, "T", False)]).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no")
    )
    with pytest.raises(SchemaError, match="round_no"):
        loss_counts(df, thresholds)


def test_empty_side_is_refused_instead_of_silently_resetting(thresholds) -> None:
    """Tyhjä puoli näyttäisi puolen vaihdolta ja nollaisi laskurin äänettömästi."""
    df = team_frame([(1, "T", False), (2, None, False), (3, "T", False)])
    with pytest.raises(SchemaError, match="side"):
        loss_counts(df, thresholds)


# --- Kierrostyyppi: kierrosnumeron säännöt --------------------------------------


@pytest.mark.parametrize("round_no", [1, 13])
def test_pistol_round_is_decided_by_the_round_number(thresholds, round_no) -> None:
    """Pistooli ratkeaa numerosta ennen kuin rahaa katsotaan lainkaan."""
    decision = classify_round(
        row(round_no=round_no, equip_buy_end=25000),
        previous(won=True, round_no=round_no - 1),
        thresholds,
        loss_count=1,
    )
    assert decision.round_type == "pistol"
    assert "pistoolikierros" in decision.reason


@pytest.mark.parametrize("round_no", [25, 26, 27, 28])
def test_overtime_round_gets_no_economy_reasoning(thresholds, round_no) -> None:
    decision = classify_round(
        row(round_no=round_no, equip_buy_end=1000, money_buy_end=60000),
        previous(won=False, round_no=round_no - 1),
        thresholds,
        loss_count=4,
    )
    assert decision.round_type == "ot"
    assert "jatkoaikaa" in decision.reason


def test_regulation_round_is_never_overtime(thresholds) -> None:
    decision = classify_round(
        row(round_no=24), previous(True, round_no=23), thresholds, loss_count=1
    )
    assert decision.round_type != "ot"


# --- Kierrostyyppi: talous ------------------------------------------------------


def test_full_buy_is_decided_from_the_equipment_value(thresholds) -> None:
    decision = classify_round(
        row(equip_buy_end=5 * thresholds.full_equip_min),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "full"


def test_full_buy_wins_over_the_after_loss_rules(thresholds) -> None:
    """Täysi osto ratkeaa varustearvosta, vaikka edellinen olisi hävitty."""
    decision = classify_round(
        row(equip_buy_end=25000, money_buy_end=100),
        previous(won=False),
        thresholds,
        loss_count=4,
    )
    assert decision.round_type == "full"


def test_eco_after_a_loss_when_the_team_did_not_buy(thresholds) -> None:
    """Todennettu tilanne: pistoolihäviön jälkeen säästö, raha jää kassaan."""
    decision = classify_round(
        row(
            round_no=2,
            equip_buy_end=2000,
            equip_round_start=1000,
            money_buy_end=10100,
            money_spent=600,
        ),
        previous(won=False, round_no=1),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "eco"
    # I/O-matriisi: perustelu kertoo rahan ja loss countin.
    assert "loss count 2" in decision.reason
    assert "$/pelaaja" in decision.reason


def test_force_after_a_loss_when_the_team_bought_itself_empty(thresholds) -> None:
    """I/O-matriisi: ostettiin tyhjäksi -- 2 380 $/pelaaja, saldoa jäljellä 30 $."""
    decision = classify_round(
        row(
            round_no=23,
            equip_buy_end=12900,
            equip_round_start=1000,
            money_buy_end=150,
            money_spent=11900,
        ),
        previous(won=False, round_no=22),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "force"
    assert "ostettu tyhjäksi" in decision.reason
    # S2: perustelu nimeää molemmat vertailuun käytetyt arvot.
    assert str(thresholds.force_buy_min) in decision.reason
    assert str(thresholds.force_money_left_max) in decision.reason


def test_half_after_a_loss_when_the_team_left_money_in_the_pocket(thresholds) -> None:
    """I/O-matriisi: ostettiin ja jätettiin varaa -- sama ostos, eri saldo.

    S2: force ja puoliosto eroavat **taskuun jätetystä rahasta**. Tämä on
    tasan edellisen testin pari: ostos on sama, mutta rahaa jäi seuraavalle
    kierrokselle.
    """
    shared = dict(
        round_no=23, equip_buy_end=12900, equip_round_start=1000, money_spent=11900
    )
    bought_empty = classify_round(
        row(money_buy_end=5 * thresholds.force_money_left_max, **shared),
        previous(won=False, round_no=22),
        thresholds,
        loss_count=2,
    )
    left_room = classify_round(
        row(money_buy_end=5 * (thresholds.force_money_left_max + 100), **shared),
        previous(won=False, round_no=22),
        thresholds,
        loss_count=2,
    )
    assert bought_empty.round_type == "force"
    assert left_room.round_type == "half"
    assert "jätettiin varaa" in left_room.reason


def test_the_purchase_threshold_is_inclusive_at_exactly_the_limit(
    thresholds,
) -> None:
    """``>=``, ei ``>``: tasan rajalla oleva ostos on jo ostos.

    Rajan molemmat naapurit on pinnattu muualla; tämä pinnaa itse rajan.
    Ilman tätä ``>=`` voi vaihtua merkiksi ``>`` ilman että mikään huomauttaa.
    """
    def decision(bought_pp: int) -> str | None:
        return classify_round(
            row(
                equip_buy_end=5 * (bought_pp + 300),
                equip_round_start=5 * 300,
                money_buy_end=5 * 100,
            ),
            previous(won=False),
            thresholds,
            loss_count=2,
        ).round_type

    assert decision(thresholds.force_buy_min) == "force"
    assert decision(thresholds.force_buy_min - 1) == "eco"


def test_the_money_left_threshold_is_inclusive_at_exactly_the_limit(
    thresholds,
) -> None:
    """``<=``, ei ``<``: tasan rajalle jäänyt raha on yhä "ostettu tyhjäksi"."""
    def decision(left_pp: int) -> str | None:
        return classify_round(
            row(
                equip_buy_end=5 * 2000,
                equip_round_start=5 * 300,
                money_buy_end=5 * left_pp,
            ),
            previous(won=False),
            thresholds,
            loss_count=2,
        ).round_type

    assert decision(thresholds.force_money_left_max) == "force"
    assert decision(thresholds.force_money_left_max + 1) == "half"


def test_the_reason_never_contradicts_its_own_rounded_number(thresholds) -> None:
    """P13: vertailu ja perustelun luku ovat sama pyöristetty luku.

    Pyöristämätön vertailu tuottaisi tekstin "taskuun jäi 1000 $/pelaaja eli
    yli 1000 $" -- juuri siinä rajatapauksessa, jonka lukija haluaa tarkistaa.
    """
    limit = thresholds.force_money_left_max
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * limit + 2,  # 1000,4 $/pelaaja -> pyöristyy 1000:een
        ),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "force"
    assert f"taskuun jäi vain {limit} $/pelaaja eli enintään {limit} $" in decision.reason


def test_a_poor_team_that_did_not_buy_is_an_eco_not_a_force(thresholds) -> None:
    """I/O-matriisi: köyhä joukkue -- kassa tyhjä, mutta ostos jäi rajan alle.

    Pelkkä "raha loppui" ei ole force: panssarin ja pistoolin viimeisillä
    rahoillaan ostava joukkue tyhjensi kassan mutta ei forcannut. Siksi
    ``force_buy_min`` on forcen **edellytys**, ei vain sen kaista.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 1200,
            equip_round_start=5 * 300,
            money_buy_end=50,
            money_spent=4500,
        ),
        previous(won=False),
        thresholds,
        loss_count=3,
    )
    assert decision.round_type == "eco"
    assert str(thresholds.force_buy_min) in decision.reason


def test_force_and_eco_differ_only_by_what_was_bought(thresholds) -> None:
    """Sama varustearvo, eri ostos: erottava havainto on ostettu summa."""
    bought = classify_round(
        row(equip_buy_end=9000, equip_round_start=1000, money_buy_end=500),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    saved = classify_round(
        row(equip_buy_end=9000, equip_round_start=8000, money_buy_end=500),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert bought.round_type == "force"
    assert saved.round_type == "eco"


def test_a_large_purchase_below_full_is_still_a_force(thresholds) -> None:
    """Forcella ei ole ylärajaa: ylhäältä rajaa ``full_equip_min``.

    Kalibrointidemon kierros 20 (2 710 $/pelaaja ostettu, 2 910 varusteita)
    putosi vanhan kaistan yläpuolelle ja luokittui poikkeamaksi. Kaista
    poistui, joten sama tilanne on nyt force.
    """
    purchase_pp = 2710
    decision = classify_round(
        row(
            equip_buy_end=5 * 2910,
            equip_round_start=5 * (2910 - purchase_pp),
            money_buy_end=5 * 270,
        ),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "force"


def test_equipment_value_alone_does_not_make_a_half_buy(thresholds) -> None:
    """S2: puoliosto ei ratkea varustearvosta vaan taskuun jääneestä rahasta.

    Sama varustearvo, sama ostos, eri saldo -- ja tulos on eri. Jos joku
    kytkee varustearvorajan takaisin puolioston päätökseen, tämä testi kaatuu.
    """
    shared = dict(equip_buy_end=5 * 3500, equip_round_start=1000)
    bought_empty = classify_round(
        row(money_buy_end=5 * 200, **shared),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    left_room = classify_round(
        row(money_buy_end=5 * 2500, **shared),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert bought_empty.round_type == "force"
    assert left_room.round_type == "half"


def test_a_half_buy_is_never_played_after_a_win(thresholds) -> None:
    """S1: säästö on aina reaktio häviöön, joten voiton jälkeen on normaali osto.

    Kalibroinnin kierros 2: pistoolin voittanut CT ostaa 3 200 $/pelaaja.
    Vanha luokittelija sanoi ``half``; Veeti sanoo ``full``.
    """
    decision = classify_round(
        row(round_no=2, equip_buy_end=16000, equip_round_start=1100),
        previous(won=True, round_no=1),
        thresholds,
        loss_count=1,
    )
    assert decision.round_type == "full"
    assert "voitetun kierroksen jälkeen" in decision.reason


def test_low_value_after_a_win_is_an_anomaly_not_an_eco(thresholds) -> None:
    decision = classify_round(
        row(
            equip_buy_end=5 * thresholds.anomaly_equip_max_after_win,
            equip_round_start=1000,
            money_buy_end=50000,
        ),
        previous(won=True),
        thresholds,
        loss_count=1,
    )
    assert decision.round_type == "anomaly"
    assert "voiton jälkeen" in decision.reason


def test_there_is_no_gap_left_after_a_win(thresholds) -> None:
    """S1: voiton jälkeen on vain normaali osto tai poikkeama, ei väliä.

    Vanha luokittelija jätti poikkeamarajan ja puoliostorajan väliin aukon,
    joka putosi poikkeamaksi. Testi ajetaan koko sillä välillä, joka jää
    poikkeamarajan ja täyden oston väliin.
    """
    low = thresholds.anomaly_equip_max_after_win
    high = thresholds.full_equip_min
    for equip_pp in (low + 1, (low + high) // 2, high - 1):
        decision = classify_round(
            row(
                equip_buy_end=5 * equip_pp,
                equip_round_start=5 * (equip_pp - 100),
            ),
            previous(won=True),
            thresholds,
            loss_count=1,
        )
        assert decision.round_type == "full", equip_pp


def test_a_saved_rifle_does_not_turn_an_eco_into_a_buy(thresholds) -> None:
    """S3: säästetty ase nostaa varustearvoa, mutta ei ole ostos.

    Kalibroinnin kierros 11 CT: yksi säästetty M4, ostettu 600 $/pelaaja.
    Veeti sanoo ``eco`` -- korkea varustearvo ei saa kääntää sitä ostokseksi.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 1580,
            equip_round_start=5 * (1580 - 600),
            money_buy_end=5 * 2280,
            money_spent=5 * 600,
        ),
        previous(won=False),
        thresholds,
        loss_count=3,
    )
    assert decision.round_type == "eco"
    assert "säästetty kalusto ei ole" in decision.reason


def test_negative_purchase_is_an_anomaly_not_silenced_to_zero(thresholds) -> None:
    """Varustearvon lasku ei ole ostotapahtuma; nolla piilottaisi ristiriidan."""
    decision = classify_round(
        row(equip_buy_end=10000, equip_round_start=14000),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert "laski" in decision.reason


def test_a_negative_purchase_beats_the_full_buy_rule(thresholds) -> None:
    """I/O-matriisin rivi on ehdoton: negatiivinen ostos -> anomaly.

    Korkea varustearvo ei saa peittää ristiriitaista havaintoa. Jos
    täyden oston tarkistus siirtyisi tämän eteen, kierros luokittuisi
    fulliksi ja rikkinäinen havainto katoaisi näkyvistä.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * (thresholds.full_equip_min + 1000),
            equip_round_start=5 * (thresholds.full_equip_min + 2000),
        ),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert "laski" in decision.reason


def test_a_small_negative_purchase_is_not_rounded_away(thresholds) -> None:
    """Merkki luetaan joukkuesummasta, ei pyöristetystä per pelaaja -luvusta.

    Kahden dollarin lasku viidellä pelaajalla pyöristyy nollaan per pelaaja.
    Se on silti ristiriitainen havainto, eikä sitä saa vaimentaa.
    """
    decision = classify_round(
        row(equip_buy_end=9998, equip_round_start=10000),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert "-2 $ joukkueena" in decision.reason


# --- Edellisen kierroksen jatkuvuus ---------------------------------------------


def test_missing_previous_round_is_an_anomaly(thresholds) -> None:
    decision = classify_round(row(equip_buy_end=5000), None, thresholds, loss_count=1)
    assert decision.round_type == "anomaly"
    assert "edelliseen kierrokseen" in decision.reason


def test_a_full_buy_is_recognised_even_without_a_previous_round(thresholds) -> None:
    """Tietoinen poikkeus kalibrointidokumentin johdetusta järjestyksestä.

    5 000 $/pelaaja on täysi osto riippumatta siitä, tunnetaanko edellinen
    kierros. Puoliajan ensimmäisellä kierroksella ja kierrosnumeroiden aukossa
    edellistä ei ole, ja ``anomaly`` väittäisi siellä ilmiselvästä täydestä
    ostosta, ettei sitä voi luokitella. Edellistä tarvitaan vain econ, forcen
    ja puolioston erottamiseen toisistaan.
    """
    decision = classify_round(
        row(equip_buy_end=5 * thresholds.full_equip_min),
        None,
        thresholds,
        loss_count=1,
    )
    assert decision.round_type == "full"
    assert decision.inputs["prev_round_won"] is None


def test_a_gap_in_round_numbers_breaks_the_previous_round(thresholds) -> None:
    """``rivit[index - 1]`` ei ole edellinen kierros, jos numeroissa on aukko."""
    decision = classify_round(
        row(round_no=8, equip_buy_end=5000),
        previous(won=False, round_no=5),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert decision.inputs["prev_round_won"] is None


def test_a_side_change_breaks_the_previous_round(thresholds) -> None:
    """Puolen vaihtuminen tarkoittaa, että edellinen kierros on toiselta puoliajalta."""
    decision = classify_round(
        row(round_no=14, side="CT", equip_buy_end=5000),
        previous(won=False, round_no=13, side="T"),
        thresholds,
        loss_count=1,
    )
    assert decision.round_type == "anomaly"
    assert decision.inputs["survivors_prev"] is None


def test_a_contiguous_previous_round_is_used(thresholds) -> None:
    decision = classify_round(
        row(
            round_no=14,
            side="CT",
            equip_buy_end=5 * 3500,
            money_buy_end=5 * 2500,
        ),
        previous(won=False, round_no=13, side="CT", survivors=2),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "half"
    assert decision.inputs["prev_round_won"] is False
    assert decision.inputs["survivors_prev"] == 2


# --- Vajaa joukkue ja puuttuvat havainnot ---------------------------------------


def test_per_player_values_use_the_observed_player_count(thresholds) -> None:
    """Neljällä pelaajalla sama joukkuesumma ylittää täyden oston rajan."""
    total = 4 * thresholds.full_equip_min
    four_players = classify_round(
        row(equip_buy_end=total, players_buy_end=4),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    five_players = classify_round(
        row(equip_buy_end=total, players_buy_end=5),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert four_players.round_type == "full"
    assert five_players.round_type != "full"
    assert four_players.inputs["players"] == 4
    assert four_players.inputs["players_readable"] == 4
    assert "vain 4 pelaajan arvot" in four_players.reason


def test_unknown_player_count_falls_back_and_says_so(thresholds) -> None:
    """I/O-matriisi: jos määrä ei ole tiedossa, se kirjataan perusteluun."""
    decision = classify_round(
        row(players_buy_end=None), previous(won=False), thresholds, loss_count=2
    )
    assert decision.inputs["players"] == thresholds.roster_size
    assert decision.inputs["players_readable"] is None
    assert "roster_size" in decision.reason


@pytest.mark.parametrize("observed", [0, -1, 6, 11])
def test_player_count_outside_the_roster_is_refused_as_a_divisor(
    thresholds, observed
) -> None:
    """Ylimääräinen tai vanhentunut rivi tickissä aliarvioisi per pelaaja -arvot."""
    decision = classify_round(
        row(players_buy_end=observed),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.inputs["players"] == thresholds.roster_size
    assert decision.inputs["players_readable"] == observed
    assert "sallitun välin" in decision.reason


def test_round_without_a_freeze_anchor_is_not_classified(thresholds) -> None:
    decision = classify_round(
        row(
            status="no_freeze_end",
            money_buy_end=None,
            money_spent=None,
            equip_buy_end=None,
            equip_round_start=None,
            players_buy_end=None,
        ),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "no_freeze_end" in decision.reason


def test_missing_observation_without_a_status_is_not_classified(thresholds) -> None:
    decision = classify_round(
        row(equip_buy_end=None), previous(won=False), thresholds, loss_count=2
    )
    assert decision.round_type is None
    assert "varustearvo" in decision.reason


def test_missing_round_start_equipment_is_not_classified(thresholds) -> None:
    """Nollana luettuna koko varustearvo näyttäisi tällä kierroksella ostetulta.

    Silloin aito säästö saisi tuomion "force -- ostettu lähes tyhjäksi", eli
    tasan päinvastoin kuin demossa tapahtui.
    """
    decision = classify_round(
        row(equip_buy_end=9000, equip_round_start=None),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "kierroksen alun varustearvo" in decision.reason


def test_zero_roster_size_is_refused_instead_of_dividing_by_zero(thresholds) -> None:
    zero = thresholds.model_copy(update={"roster_size": 0})
    with pytest.raises(SchemaError, match="roster_size"):
        classify_round(row(), previous(won=False), zero, loss_count=2)


# --- Perustelu ja lähtöarvot ----------------------------------------------------


def test_every_decision_carries_money_and_loss_count_in_its_reason(
    thresholds,
) -> None:
    """Ilman rahaa ja loss countia kalibrointi Story 1.4:ssä olisi mahdotonta."""
    cases = [
        (row(round_no=1), previous(True, round_no=0), 1),
        (row(round_no=25), previous(False, round_no=24), 3),
        (row(), previous(False), 2),
        (
            row(equip_buy_end=2000, equip_round_start=1000),
            previous(False),
            2,
        ),
        (row(equip_buy_end=2000), previous(True), 1),
    ]
    for r, e, lc in cases:
        decision = classify_round(r, e, thresholds, loss_count=lc)
        assert "Käytettävissä" in decision.reason, decision
        assert "jäljellä" in decision.reason, decision
        assert f"loss count {lc}" in decision.reason, decision


def test_eco_reason_names_the_purchase_it_compared_against(thresholds) -> None:
    """Eco ratkeaa ostetusta summasta, ja perustelu näyttää molemmat luvut.

    Perustelu kertoo silti myös rahan molemmat suunnat, jotta lukija ei sekoita
    jäljelle jäänyttä saldoa käytettävissä olleeseen rahaan.
    """
    decision = classify_round(
        row(
            round_no=2,
            equip_buy_end=1600,
            equip_round_start=1000,
            money_buy_end=9000,
            money_spent=600,
        ),
        previous(won=False, round_no=1),
        thresholds,
        loss_count=2,
    )
    assert decision.round_type == "eco"
    lowered = decision.reason.lower()
    assert f"ostettu vain {per_player(600, 5)} $/pelaaja" in lowered
    assert f"alle forcen edellytyksen {thresholds.force_buy_min} $" in lowered
    assert f"käytettävissä {per_player(9000 + 600, 5)} $/pelaaja" in lowered
    assert "jäljellä 1800 $/pelaaja" in lowered


def test_available_money_is_the_sum_of_left_and_spent() -> None:
    assert available_money({"money_buy_end": 100, "money_spent": 900}) == 1000
    assert available_money({"money_buy_end": None, "money_spent": None}) is None
    assert available_money({"money_buy_end": 100, "money_spent": None}) == 100


def test_inputs_match_the_classified_schema_exactly(thresholds) -> None:
    """``inputs`` on skeemasopimus, ei vapaa sanakirja."""
    decision = classify_round(row(), previous(False), thresholds, loss_count=2)
    assert tuple(decision.inputs) == INPUT_FIELDS
    assert set(decision.inputs) == {f.name for f in CLASSIFIED_INPUTS.fields}


def test_inputs_carry_every_threshold_the_rules_compare_against(thresholds) -> None:
    decision = classify_round(row(), previous(False), thresholds, loss_count=2)
    assert decision.inputs["full_equip_min"] == thresholds.full_equip_min
    assert decision.inputs["force_buy_min"] == thresholds.force_buy_min
    assert decision.inputs["force_money_left_max"] == thresholds.force_money_left_max
    assert (
        decision.inputs["anomaly_equip_max_after_win"]
        == thresholds.anomaly_equip_max_after_win
    )


def test_inputs_no_longer_carry_the_retired_thresholds(thresholds) -> None:
    """Poistuneet kynnykset poistuivat kaikkialta, myös lähtöarvoista.

    Puolittainen siivous jättäisi taulun sarakkeen, jolla ei ole lukijaa --
    ja seuraava lukija luulisi sen kertovan jotain päätöksestä.
    """
    decision = classify_round(row(), previous(False), thresholds, loss_count=2)
    for retired in (
        "eco_money_max",
        "eco_money_max_low_loss",
        "eco_loss_count_min",
        "eco_money_max_applied",
        "force_money_min",
        "force_money_max",
        "half_equip_min",
    ):
        assert retired not in decision.inputs
        assert not hasattr(thresholds, retired)


def test_inputs_carry_the_previous_round_state(thresholds) -> None:
    decision = classify_round(
        row(survivors_equip_prev=4200),
        previous(won=True, survivors=3),
        thresholds,
        loss_count=1,
    )
    assert decision.inputs["prev_round_won"] is True
    assert decision.inputs["survivors_prev"] == 3
    assert decision.inputs["survivors_equip_prev"] == 4200


def test_bought_and_available_are_recoverable_without_new_columns(thresholds) -> None:
    """Molemmat johdokset saa ``inputs``-rakenteesta ilman skeemamuutosta."""
    decision = classify_round(
        row(
            equip_buy_end=12000,
            equip_round_start=1000,
            money_buy_end=400,
            money_spent=11000,
        ),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    assert (
        decision.inputs["equip_buy_end"] - decision.inputs["equip_round_start"] == 11000
    )
    assert decision.inputs["money_buy_end"] + decision.inputs["money_spent"] == 11400


def test_per_player_rounds_the_same_way_everywhere(thresholds) -> None:
    """Sama luku ei saa poiketa dollarilla taulukossa ja perustelussa."""
    decision = classify_round(
        row(equip_buy_end=12345, equip_round_start=1000),
        previous(won=False),
        thresholds,
        loss_count=2,
    )
    expected = per_player(12345, 5)
    assert f"{expected} $/pelaaja" in decision.reason


def test_decision_unpacks_as_a_triple(thresholds) -> None:
    round_type, reason, inputs = classify_round(
        row(), previous(False), thresholds, loss_count=2
    )
    assert isinstance(round_type, str)
    assert isinstance(reason, str)
    assert isinstance(inputs, dict)
    assert isinstance(
        classify_round(row(), previous(False), thresholds, loss_count=2), Decision
    )


def test_a_threshold_change_changes_the_verdict(thresholds) -> None:
    """Kynnykset ovat asetuksia, eivät koodia: sama rivi, eri raja, eri tulos."""
    stricter = thresholds.model_copy(update={"full_equip_min": 6000})
    r = row(equip_buy_end=25000, equip_round_start=1000)
    assert classify_round(r, previous(False), thresholds, loss_count=2).round_type == (
        "full"
    )
    assert classify_round(r, previous(False), stricter, loss_count=2).round_type != (
        "full"
    )
