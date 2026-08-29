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

from pappascout.domain.economy import classify_round
from pappascout.domain.models import ThresholdSettings, load_settings

#: Kokoonpanon koko, jolla dokumentin per pelaaja -luvut on laskettu.
PELAAJIA = 5

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
MIN_MARGINAALI = 200


class Kierros(NamedTuple):
    """Yksi rivi kalibrointidokumentin totuustaulusta.

    Attributes:
        round_no: Kierrosnumero.
        side: Puoli, jonka näkökulmasta rivi on.
        prev_won: Voittiko joukkue edellisen kierroksen; ``None``, jos
            edellistä kierrosta ei ole (kierros 1).
        jaljella: ``money_freeze_end`` $/pelaaja.
        ostettu: ``equip_freeze_end - equip_round_start`` $/pelaaja.
        varusteet: ``equip_freeze_end`` $/pelaaja.
        totuus: Veetin antama kierrostyyppi.
        peruste: Veetin sanallinen peruste, dokumentista.
    """

    round_no: int
    side: str
    prev_won: bool | None
    jaljella: int
    ostettu: int
    varusteet: int
    totuus: str
    peruste: str


#: Totuustaulu sellaisenaan, dokumentin rivijärjestyksessä.
TOTUUSTAULU: tuple[Kierros, ...] = (
    Kierros(1, "CT", None, 110, 650, 850, "pistol", "pistoolikierros"),
    Kierros(1, "T", None, 270, 530, 730, "pistol", "pistoolikierros"),
    Kierros(2, "CT", True, 1010, 2890, 3200, "full", "voitti pistoolin (S1)"),
    Kierros(2, "T", False, 2060, 120, 320, "eco", "yksi p250, yksi valo, yksi savu"),
    Kierros(11, "T", True, 2350, 3790, 5510, "full", "voitti edellisen"),
    Kierros(11, "CT", False, 2280, 600, 1580, "eco", "yksi säästetty M4 (S3)"),
    Kierros(14, "T", True, 530, 2960, 3550, "full", "voitti kierroksen 13 (S1)"),
    Kierros(17, "CT", True, 630, 3520, 5560, "full", "voitti edellisen"),
    Kierros(17, "T", False, 3090, 950, 1150, "eco", "raha säästetään AWP:hen"),
    Kierros(19, "T", True, 1580, 3940, 5330, "full", "voitti edellisen"),
    Kierros(19, "CT", False, 750, 1840, 2040, "force", "ostivat tyhjäksi (S2)"),
    Kierros(20, "CT", True, 1350, 2660, 5550, "full", "voitti edellisen"),
    Kierros(20, "T", False, 270, 2710, 2910, "force", "2x AK, 2x tec9; tyhjäksi"),
    Kierros(21, "CT", True, 2700, 2720, 5680, "full", "voitti edellisen"),
    Kierros(21, "T", False, 2260, 510, 710, "eco", "pitävät econ"),
)


@pytest.fixture
def kynnykset(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


def _rivit(k: Kierros) -> tuple[dict, dict | None]:
    """Kierrosrivi ja sen edellinen kierros ``ROUNDS``-muodossa.

    Per pelaaja -luvut kerrotaan viidellä, jotta ``classify_round`` päätyy
    jakaessaan tasan samoihin lukuihin kuin dokumentissa. Käytetty raha ei
    kuulu totuustauluun eikä vaikuta yhteenkään sääntöön; se asetetaan
    ostetun summan mukaiseksi, jotta perustelun rahaluvut ovat uskottavia.
    """
    rivi = {
        "round_no": k.round_no,
        "side": k.side,
        "status": "ok",
        "money_freeze_end": k.jaljella * PELAAJIA,
        "money_spent": k.ostettu * PELAAJIA,
        "equip_freeze_end": k.varusteet * PELAAJIA,
        "equip_round_start": (k.varusteet - k.ostettu) * PELAAJIA,
        "players_freeze_end": PELAAJIA,
        "survivors_equip_prev": 0,
    }
    if k.prev_won is None:
        return rivi, None
    edellinen = {
        "round_no": k.round_no - 1,
        "side": k.side,
        "won": k.prev_won,
        "survivors": 0,
    }
    return rivi, edellinen


def _tunnus(k: Kierros) -> str:
    return f"k{k.round_no}-{k.side}-{k.totuus}"


@pytest.mark.parametrize("k", TOTUUSTAULU, ids=[_tunnus(k) for k in TOTUUSTAULU])
def test_truth_table_row_matches_the_classifier(k: Kierros, kynnykset) -> None:
    """Jokainen dokumentin rivi luokittuu siksi, mitä Veeti näki replaystä."""
    rivi, edellinen = _rivit(k)
    paatos = classify_round(rivi, edellinen, kynnykset, loss_count=2)
    assert paatos.round_type == k.totuus, (
        f"Kierros {k.round_no} {k.side}: dokumentti sanoo {k.totuus!r} "
        f"({k.peruste}), luokittelija sanoi {paatos.round_type!r}. "
        f"Perustelu: {paatos.reason}"
    )


def test_the_truth_table_has_every_row_from_the_document() -> None:
    """15 riviä, ei yhtään ohitettua: taulu on tarinan mittatikku."""
    assert len(TOTUUSTAULU) == 15
    assert len({(k.round_no, k.side) for k in TOTUUSTAULU}) == 15
    assert {k.side for k in TOTUUSTAULU} == {"CT", "T"}


def test_the_two_sides_of_a_round_cannot_both_have_won_the_previous_one() -> None:
    """Ristiintarkistus taulun sisäisestä johdonmukaisuudesta.

    Rivien laskeminen ei voi havaita väärin kirjattua riviä, koska taulu
    vertaisi itseään itseensä. Tämä vertaa sitä pelin sääntöön: saman
    kierroksen kaksi puolta ovat vastustajia, joten edellisen kierroksen
    voitti tasan toinen -- tai kumpikaan ei tiedä sitä (kierros 1).
    """
    kierroksittain: dict[int, list[Kierros]] = {}
    for k in TOTUUSTAULU:
        kierroksittain.setdefault(k.round_no, []).append(k)

    parit = {no: rivit for no, rivit in kierroksittain.items() if len(rivit) == 2}
    assert parit, "taulussa ei ole yhtään kierrosta molemmilta puolilta"

    for no, rivit in parit.items():
        voitot = [k.prev_won for k in rivit]
        if all(v is None for v in voitot):
            continue  # kierros 1: edellistä kierrosta ei ole kummallakaan
        assert sorted(voitot, key=str) == [False, True], (
            f"Kierros {no}: edellisen kierroksen voitti tasan toinen puoli, "
            f"mutta taulussa lukee {voitot}."
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
    assert {k.totuus for k in TOTUUSTAULU} == {"pistol", "full", "force", "eco"}
    assert not [k for k in TOTUUSTAULU if k.totuus in ("half", "anomaly")]


@pytest.mark.parametrize("k", TOTUUSTAULU, ids=[_tunnus(k) for k in TOTUUSTAULU])
def test_the_loss_counter_never_changes_the_verdict(k: Kierros, kynnykset) -> None:
    """Loss count ei ole enää sääntö vaan taustatieto perustelussa.

    Jos jokin tuleva muutos kytkee laskurin takaisin päätökseen, tämä testi
    kaatuu ennen kuin totuustaulu ehtii mennä hiljaa rikki.
    """
    rivi, edellinen = _rivit(k)
    tyypit = {
        classify_round(rivi, edellinen, kynnykset, loss_count=lc).round_type
        for lc in range(kynnykset.loss_count_min, kynnykset.loss_count_max + 1)
    }
    assert tyypit == {k.totuus}


def test_every_threshold_keeps_a_margin_to_the_nearest_observation(
    kynnykset,
) -> None:
    """Kynnys ei saa olla kosketusetäisyydellä havaitusta kierroksesta.

    Pelkkä välin merkin tarkistus ("forcet yläpuolella, ecot alapuolella")
    menisi läpi myös arvolla, jonka marginaali on 10 $. Tämä vaatii
    :data:`MIN_MARGINAALI`-etäisyyden siihen suuntaan, johon aineistossa on
    havaintoja.
    """
    haviot = [k for k in TOTUUSTAULU if k.prev_won is False]
    forcet = [k for k in haviot if k.totuus == "force"]
    ecot = [k for k in haviot if k.totuus == "eco"]
    voitot = [k for k in TOTUUSTAULU if k.prev_won is True]
    assert forcet and ecot and voitot

    def marginaali(havainto: int, kynnys: int) -> int:
        return abs(havainto - kynnys)

    # force_buy_min: molemmilla puolilla on havaintoja, joten marginaali
    # vaaditaan molempiin suuntiin.
    lahin_force = min(k.ostettu for k in forcet)
    lahin_eco = max(k.ostettu for k in ecot)
    assert lahin_eco < kynnykset.force_buy_min <= lahin_force
    assert marginaali(lahin_force, kynnykset.force_buy_min) >= MIN_MARGINAALI
    assert marginaali(lahin_eco, kynnykset.force_buy_min) >= MIN_MARGINAALI

    # force_money_left_max: puoliostoja ei ole havaittu, joten vain
    # force-puoli on mitattavissa. Ecot eivät ole vertailujoukko -- ne
    # erottuvat jo siitä, ettei niissä ostettu.
    korkein_force = max(k.jaljella for k in forcet)
    assert korkein_force <= kynnykset.force_money_left_max
    assert (
        marginaali(korkein_force, kynnykset.force_money_left_max) >= MIN_MARGINAALI
    )

    # anomaly_equip_max_after_win: yksikään havaittu voiton jälkeinen osto ei
    # saa pudota poikkeamaksi (P9).
    matalin_voiton_jalkeen = min(k.varusteet for k in voitot)
    assert matalin_voiton_jalkeen > kynnykset.anomaly_equip_max_after_win
    assert (
        marginaali(matalin_voiton_jalkeen, kynnykset.anomaly_equip_max_after_win)
        >= MIN_MARGINAALI
    )

    # full_equip_min: aineiston korkein häviön jälkeinen kierros ei saa yltää
    # täyden oston rajalle, muuten force luokittuisi fulliksi.
    korkein_havion_jalkeen = max(k.varusteet for k in haviot)
    assert korkein_havion_jalkeen < kynnykset.full_equip_min
    assert (
        marginaali(korkein_havion_jalkeen, kynnykset.full_equip_min)
        >= MIN_MARGINAALI
    )
