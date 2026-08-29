"""Kalibrointidokumentin totuustaulu regressiotestinä (Story 1.4).

Lähde on ``_bmad-output/implementation-artifacts/kalibrointi-kierrostyypit.md``:
Veeti katsoi Ancient-demon 2D-replaynä ja kertoi jokaisesta kierroksesta, mitä
siinä tapahtui. **Se dokumentti on totuus.** Jos jokin rivi tästä taulusta ei
mene läpi, luokittelija on väärässä -- taulua ei muuteta koodin mukaiseksi eikä
kynnysarvoa viilata niin, että yksittäinen rivi menisi läpi.

Luvut ovat dollaria **per pelaaja**, luettu ``classified``-taulun
``inputs``-rakenteesta, ja ne muunnetaan joukkuesummiksi kertomalla viidellä.
Kynnykset luetaan oikeasta ``settings.toml``ista: testi, joka keksisi omat
rajansa, ei todistaisi mitään siitä asetustiedostosta, jolla työkalu ajetaan.

Testi ei tarvitse demotiedostoa, joten ``pytest -m "not demo"`` kattaa sen.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from pappascout.adapters.demo_parser import _armed_count
from pappascout.domain.economy import classify_round
from pappascout.domain.models import (
    ParseSettings,
    ThresholdSettings,
    load_settings,
)

#: Kokoonpanon koko, jolla dokumentin per pelaaja -luvut on laskettu.
PLAYERS = 5

#: Pienin hyväksyttävä etäisyys kynnyksestä lähimpään havaintoon, $/pelaaja.
#:
#: 200 $ on oletuspistoolin (Glock / USP-S / P2000) arvo varustearvossa eli
#: pienin yksikkö, joka näissä luvuissa ylipäätään liikkuu: sitä kapeampi
#: marginaali tarkoittaisi, että yksi halvin mahdollinen ostos voi kääntää
#: kierroksen luokan. Vartija ei siis vaadi kynnystä välin keskeltä -- se
#: vaatii, ettei kumpikaan havaittu joukko ole kosketusetäisyydellä.
#:
#: Nykyisillä arvoilla tiukin marginaali on 250 $ (force_money_left_max = 1000
#: vs. havaittu force, jolla jäi 750). Jos jokin kynnys viilataan aineiston
#: reunaan, tämä kaatuu.
MIN_MARGIN = 200


class Round(NamedTuple):
    """Yksi rivi kalibrointidokumentin totuustaulusta.

    Attributes:
        round_no: Kierrosnumero.
        side: Puoli, jonka näkökulmasta rivi on.
        prev_won: Voittiko joukkue edellisen kierroksen; ``None``, jos
            edellistä kierrosta ei ole (kierros 1).
        left: ``money_freeze_end`` $/pelaaja.
        bought: ``equip_freeze_end - equip_round_start`` $/pelaaja.
        equip: ``equip_freeze_end`` $/pelaaja.
        truth: Veetin antama kierrostyyppi.
        basis: Veetin sanallinen peruste, dokumentista.
    """

    round_no: int
    side: str
    prev_won: bool | None
    left: int
    bought: int
    equip: int
    truth: str
    basis: str


#: Totuustaulu sellaisenaan, dokumentin rivijärjestyksessä.
TRUTH_TABLE: tuple[Round, ...] = (
    Round(1, "CT", None, 110, 650, 850, "pistol", "pistoolikierros"),
    Round(1, "T", None, 270, 530, 730, "pistol", "pistoolikierros"),
    Round(2, "CT", True, 1010, 2890, 3200, "full", "voitti pistoolin (S1)"),
    Round(2, "T", False, 2060, 120, 320, "eco", "yksi p250, yksi valo, yksi savu"),
    Round(11, "T", True, 2350, 3790, 5510, "full", "voitti edellisen"),
    Round(11, "CT", False, 2280, 600, 1580, "eco", "yksi säästetty M4 (S3)"),
    Round(14, "T", True, 530, 2960, 3550, "full", "voitti kierroksen 13 (S1)"),
    Round(17, "CT", True, 630, 3520, 5560, "full", "voitti edellisen"),
    Round(17, "T", False, 3090, 950, 1150, "eco", "raha säästetään AWP:hen"),
    Round(19, "T", True, 1580, 3940, 5330, "full", "voitti edellisen"),
    Round(19, "CT", False, 750, 1840, 2040, "force", "ostivat tyhjäksi (S2)"),
    Round(20, "CT", True, 1350, 2660, 5550, "full", "voitti edellisen"),
    Round(20, "T", False, 270, 2710, 2910, "force", "2x AK, 2x tec9; tyhjäksi"),
    Round(21, "CT", True, 2700, 2720, 5680, "full", "voitti edellisen"),
    Round(21, "T", False, 2260, 510, 710, "eco", "pitävät econ"),
)


@pytest.fixture
def thresholds(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


@pytest.fixture
def parse_settings(settings_file: Path) -> ParseSettings:
    """``[parse]``-osio: kalustokynnys luetaan oikeasta asetustiedostosta.

    Testi, joka keksisi oman kynnyksensä, ei todistaisi mitään siitä arvosta,
    jolla arkisto oikeasti syntyy.
    """
    return load_settings(settings_file, env_files=()).parse


def _rows(k: Round) -> tuple[dict, dict | None]:
    """Kierrosrivi ja sen edellinen kierros ``ROUNDS``-muodossa.

    Per pelaaja -luvut kerrotaan viidellä, jotta ``classify_round`` päätyy
    jakaessaan tasan samoihin lukuihin kuin dokumentissa. Käytetty raha ei
    kuulu totuustauluun eikä vaikuta yhteenkään sääntöön; se asetetaan
    ostetun summan mukaiseksi, jotta perustelun rahaluvut ovat uskottavia.
    """
    row = {
        "round_no": k.round_no,
        "side": k.side,
        "status": "ok",
        "money_freeze_end": k.left * PLAYERS,
        "money_spent": k.bought * PLAYERS,
        "equip_freeze_end": k.equip * PLAYERS,
        "equip_round_start": (k.equip - k.bought) * PLAYERS,
        "players_freeze_end": PLAYERS,
        "survivors_equip_prev": 0,
    }
    if k.prev_won is None:
        return row, None
    previous = {
        "round_no": k.round_no - 1,
        "side": k.side,
        "won": k.prev_won,
        "survivors": 0,
    }
    return row, previous


def _test_id(k: Round) -> str:
    return f"k{k.round_no}-{k.side}-{k.truth}"


@pytest.mark.parametrize("k", TRUTH_TABLE, ids=[_test_id(k) for k in TRUTH_TABLE])
def test_truth_table_row_matches_the_classifier(k: Round, thresholds) -> None:
    """Jokainen dokumentin rivi luokittuu siksi, mitä Veeti näki replaystä."""
    row, previous = _rows(k)
    decision = classify_round(row, previous, thresholds, loss_count=2)
    assert decision.round_type == k.truth, (
        f"Kierros {k.round_no} {k.side}: dokumentti sanoo {k.truth!r} "
        f"({k.basis}), luokittelija sanoi {decision.round_type!r}. "
        f"Perustelu: {decision.reason}"
    )


def test_the_truth_table_has_every_row_from_the_document() -> None:
    """15 riviä, ei yhtään ohitettua: taulu on tarinan mittatikku."""
    assert len(TRUTH_TABLE) == 15
    assert len({(k.round_no, k.side) for k in TRUTH_TABLE}) == 15
    assert {k.side for k in TRUTH_TABLE} == {"CT", "T"}


def test_the_two_sides_of_a_round_cannot_both_have_won_the_previous_one() -> None:
    """Ristiintarkistus taulun sisäisestä johdonmukaisuudesta.

    Rivien laskeminen ei voi havaita väärin kirjattua riviä, koska taulu
    vertaisi itseään itseensä. Tämä vertaa sitä pelin sääntöön: saman
    kierroksen kaksi puolta ovat vastustajia, joten edellisen kierroksen
    voitti tasan toinen -- tai kumpikaan ei tiedä sitä (kierros 1).
    """
    by_round: dict[int, list[Round]] = {}
    for k in TRUTH_TABLE:
        by_round.setdefault(k.round_no, []).append(k)

    pairs = {no: rows for no, rows in by_round.items() if len(rows) == 2}
    assert pairs, "taulussa ei ole yhtään kierrosta molemmilta puolilta"

    for no, rows in pairs.items():
        prev_wins = [k.prev_won for k in rows]
        if all(v is None for v in prev_wins):
            continue  # kierros 1: edellistä kierrosta ei ole kummallakaan
        assert sorted(prev_wins, key=str) == [False, True], (
            f"Kierros {no}: edellisen kierroksen voitti tasan toinen puoli, "
            f"mutta taulussa lukee {prev_wins}."
        )


def test_the_truth_table_covers_the_four_observed_round_types() -> None:
    """Taulussa on neljä tyyppiä viidestä -- ``half`` ja ``anomaly`` puuttuvat.

    Tämä ei ole aukko testissä vaan **aineistossa**: Veeti ei nähnyt tässä
    demossa yhtäkään kiistatonta puoliostoa eikä poikkeamaa. Siksi
    ``force_money_left_max`` on ainoa kynnys, jonka toista puolta ei ole
    havaittu, ja siksi ``half`` ja ``anomaly`` on pinnattu erikseen
    ``test_economy.py``:n käsin rakennetuilla tapauksilla. Jos taulu joskus
    saa puoliostorivin, tämä testi kaatuu -- ja se on hyvä, koska silloin
    puolioston raja pitää säätää havaintoa vasten.
    """
    assert {k.truth for k in TRUTH_TABLE} == {"pistol", "full", "force", "eco"}
    assert not [k for k in TRUTH_TABLE if k.truth in ("half", "anomaly")]


@pytest.mark.parametrize("k", TRUTH_TABLE, ids=[_test_id(k) for k in TRUTH_TABLE])
def test_the_loss_counter_never_changes_the_verdict(k: Round, thresholds) -> None:
    """Loss count ei ole enää sääntö vaan taustatieto perustelussa.

    Jos jokin tuleva muutos kytkee laskurin takaisin päätökseen, tämä testi
    kaatuu ennen kuin totuustaulu ehtii mennä hiljaa rikki.
    """
    row, previous = _rows(k)
    types = {
        classify_round(row, previous, thresholds, loss_count=lc).round_type
        for lc in range(thresholds.loss_count_min, thresholds.loss_count_max + 1)
    }
    assert types == {k.truth}


def test_every_threshold_keeps_a_margin_to_the_nearest_observation(
    thresholds,
) -> None:
    """Kynnys ei saa olla kosketusetäisyydellä havaitusta kierroksesta.

    Pelkkä välin merkin tarkistus ("forcet yläpuolella, ecot alapuolella")
    menisi läpi myös arvolla, jonka marginaali on 10 $. Tämä vaatii
    :data:`MIN_MARGIN`-etäisyyden siihen suuntaan, johon aineistossa on
    havaintoja.
    """
    losses = [k for k in TRUTH_TABLE if k.prev_won is False]
    forces = [k for k in losses if k.truth == "force"]
    ecos = [k for k in losses if k.truth == "eco"]
    wins = [k for k in TRUTH_TABLE if k.prev_won is True]
    assert forces and ecos and wins

    def margin(observation: int, threshold: int) -> int:
        return abs(observation - threshold)

    # force_buy_min: molemmilla puolilla on havaintoja, joten marginaali
    # vaaditaan molempiin suuntiin.
    nearest_force = min(k.bought for k in forces)
    nearest_eco = max(k.bought for k in ecos)
    assert nearest_eco < thresholds.force_buy_min <= nearest_force
    assert margin(nearest_force, thresholds.force_buy_min) >= MIN_MARGIN
    assert margin(nearest_eco, thresholds.force_buy_min) >= MIN_MARGIN

    # force_money_left_max: puoliostoja ei ole havaittu, joten vain
    # force-puoli on mitattavissa. Ecot eivät ole vertailujoukko -- ne
    # erottuvat jo siitä, ettei niissä ostettu.
    highest_force = max(k.left for k in forces)
    assert highest_force <= thresholds.force_money_left_max
    assert (
        margin(highest_force, thresholds.force_money_left_max) >= MIN_MARGIN
    )

    # anomaly_equip_max_after_win: yksikään havaittu voiton jälkeinen osto ei
    # saa pudota poikkeamaksi (P9).
    lowest_after_win = min(k.equip for k in wins)
    assert lowest_after_win > thresholds.anomaly_equip_max_after_win
    assert (
        margin(lowest_after_win, thresholds.anomaly_equip_max_after_win)
        >= MIN_MARGIN
    )

    # full_equip_min: aineiston korkein häviön jälkeinen kierros ei saa yltää
    # täyden oston rajalle, muuten force luokittuisi fulliksi.
    highest_after_loss = max(k.equip for k in losses)
    assert highest_after_loss < thresholds.full_equip_min
    assert (
        margin(highest_after_loss, thresholds.full_equip_min)
        >= MIN_MARGIN
    )


# --- Kalustolaskuri (Story 1.5) ------------------------------------------------
#
# Nämä ovat samaa ihmisen antamaa totuutta kuin yllä oleva taulu, mutta
# pelaajakohtaisina varustearvoina. Ne asuvat täällä eivätkä demotestissä,
# koska ``pytest -m "not demo"`` on se ajo, jolla totuustaulu on tarkoitus
# säilyä: demotesti ohittaa itsensä koneella, jolla demoja ei ole, ja
# kalibrointi jäisi silloin valvomatta.
#
# Arvot on luettu Ancientista freezetimen lopun tickiltä 2026-08-29, ja
# tavaraluettelo on tarkistettu samalta tickiltä. Kolme kalibrointihavaintoa:
#
#   200  = veitsi + USP-S, ei panssaria     (pelkkä ilmainen oletuspistooli)
#   300  = veitsi + P250, ei panssaria      (ostettu pistooli KORVAA ilmaisen)
#   1250 = veitsi + Glock + C4 + 2 valoa, kevlar
#
# Keskimmäinen ratkaisee kynnyksen laskutavan: kevlar 650 + p250 300 = 950,
# ei 1150. Viimeinen on laskurin tunnustettu rajaus -- se laskeutuu
# aseistetuksi ilman yhtään parannettua asetta, koska varustearvo ei erota
# asetta panssarista ja kranaateista.


class ArmedRound(NamedTuple):
    """Yhden kierroksen pelaajakohtaiset varustearvot ja Veetin kuvaus.

    Attributes:
        round_no: Kierrosnumero.
        side: Puoli, jonka rivi tämä on.
        equip: Varustearvo $/pelaaja, viisi lukua nousevassa järjestyksessä.
        armed: Montako niistä ylittää kynnyksen -- odotusarvo.
        basis: Veetin sanallinen kuvaus, kalibrointidokumentista.
    """

    round_no: int
    side: str
    equip: tuple[int, ...]
    armed: int
    basis: str


ARMED_TRUTH: tuple[ArmedRound, ...] = (
    ArmedRound(
        19,
        "CT",
        (200, 2200, 2450, 2550, 2800),
        4,
        'force, "ostivat tyhjäksi"; yksi jäi ilmaiseen oletuspistooliin',
    ),
    ArmedRound(
        20,
        "T",
        (1500, 1700, 2550, 4400, 4400),
        5,
        "2x AK, 2x tec9, 1x mac10, kaikilla kevlar+kypärä -- kaikki viisi",
    ),
    ArmedRound(
        21,
        "T",
        (200, 300, 500, 1250, 1300),
        2,
        "eco: kahdella kevlar+pistooli; 300 $:n p250 jää kynnyksen alle",
    ),
)


@pytest.mark.parametrize(
    "k", ARMED_TRUTH, ids=[f"k{k.round_no}-{k.side}" for k in ARMED_TRUTH]
)
def test_armed_player_count_matches_the_human_reading(
    k: ArmedRound, parse_settings
) -> None:
    """Laskuri antaa sen luvun, jonka Veeti näki replaystä.

    Sääntö luetaan adapterilta eikä kirjoiteta tässä uudelleen: testi, joka
    laskisi omalla ``>=``-lausekkeellaan, todistaisi vain oman lausekkeensa.
    """
    rows = [{"equip_freeze_end": value} for value in k.equip]
    counted = _armed_count(rows, parse_settings.armed_player_equip_min)
    assert counted == k.armed, (
        f"Kierros {k.round_no} {k.side}: dokumentti sanoo {k.armed} "
        f"({k.basis}), laskuri sanoi {counted}. "
        "Dokumentti on totuus -- korjaa kynnys tai laskenta, ei tätä taulua."
    )


def test_armed_threshold_keeps_a_margin_to_the_nearest_observation(
    parse_settings,
) -> None:
    """Kynnys ei saa olla kosketusetäisyydellä havaitusta varustearvosta.

    Aineiston lähimmät havainnot ovat 950:n molemmin puolin: 500 alapuolella
    (Glock + savu) ja 1250 yläpuolella (Glock + kevlar + kaksi valoa).
    Marginaalit ovat 450 ja 300, eli molemmat ylittävät
    :data:`MIN_MARGIN`-etäisyyden. Ilman tätä kynnyksen viilaaminen 501:een
    menisi läpi, vaikka yksi halvin mahdollinen ostos kääntäisi laskurin.
    """
    threshold = parse_settings.armed_player_equip_min
    observations = sorted(value for k in ARMED_TRUTH for value in k.equip)
    below = [v for v in observations if v < threshold]
    at_or_above = [v for v in observations if v >= threshold]
    assert below and at_or_above, "kynnys on aineiston ulkopuolella"

    assert threshold - max(below) >= MIN_MARGIN
    assert min(at_or_above) - threshold >= MIN_MARGIN


def test_armed_count_needs_the_whole_distribution_not_the_team_sum(
    parse_settings,
) -> None:
    """Sama joukkuesumma, eri laskuri -- tämä on koko sarakkeen olemassaolon syy.

    Kolme pelaajaa kynnyksellä ja kaksi ilmaisella pistoolilla antaa saman
    summan kuin viisi pelaajaa 650 $:n kevlareilla. Ensimmäinen on puoliosto,
    jälkimmäinen ei ole, eikä ``equip_freeze_end`` erota niitä.
    """
    threshold = parse_settings.armed_player_equip_min  # 950
    half_buy = [threshold, threshold, threshold, 200, 200]  # 3250
    kevlars_only = [650, 650, 650, 650, 650]  # 3250

    assert sum(half_buy) == sum(kevlars_only)
    assert _armed_count(
        [{"equip_freeze_end": v} for v in half_buy], threshold
    ) == 3
    assert _armed_count(
        [{"equip_freeze_end": v} for v in kevlars_only], threshold
    ) == 0
