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
from pappascout.constants import KNOWN_INVENTORY_ITEMS
from pappascout.domain.economy import classify_round
from pappascout.domain.models import ThresholdSettings, load_settings

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
        left: Taskuun jäänyt raha $/pelaaja.
        bought: Ostettu summa $/pelaaja.
        equip: Varustearvo $/pelaaja.
        truth: Veetin antama kierrostyyppi.
        basis: Veetin sanallinen peruste, dokumentista.

    **Tuomio on Veetin, luvut ovat mittaus.** Luvut päivitetään, kun
    mittaushetki muuttuu (ks. taulun muutosloki); tuomioon ei kosketa. Ilman
    päivitystä tämä testi pinnaisi säännön syötteillä, joita tuote ei enää
    tuota.
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
#:
#: **Tuomiot ovat Veetin**, luvut ovat mittaus. Story 1.9 siirsi mittaushetken
#: freezetimen lopusta ostoajan loppuun, ja luvut on päivitetty siihen
#: hetkeen -- muuten tämä taulu pinnaisi säännön syötteillä, joita tuote ei
#: enää tuota, ja :func:`test_every_threshold_keeps_a_margin_to_the_nearest_observation`
#: mittaisi etäisyyttä havaintoihin joita ei enää tehdä. Se olisi pahempi kuin
#: vanhentunut luku: uusi mittaus vain **nostaa** varustearvoja, joten vartija
#: voisi mennä läpi silloinkin kun sen premissi on datassa rikki.
#:
#: MUUTOSLOKI 2026-08-29 (Story 1.9), $/pelaaja, ``jäljellä / ostettu /
#: varusteet``. Seitsemän riviä muuttui, kahdeksan pysyi ennallaan, eikä
#: yksikään **tuomio** muuttunut:
#:
#: ===========  =========================  =========================
#: Kierros      Ennen (freezetimen loppu)  Jälkeen (ostoajan loppu)
#: ===========  =========================  =========================
#: 1 T           270 /  530 /  730          140 /  660 /  860
#: 2 CT         1010 / 2890 / 3200          520 / 3380 / 3690
#: 17 T         3090 /  950 / 1150         3400 /  800 / 1000
#: 19 T         1580 / 3940 / 5330         1540 / 3920 / 5310
#: 19 CT         750 / 1840 / 2040           30 / 2520 / 2720
#: 20 T          270 / 2710 / 2910          210 / 2830 / 3030
#: 21 T         2260 /  510 /  710         2220 /  550 /  750
#: ===========  =========================  =========================
#:
#: Suurin muutos on 19 CT: viides pelaaja osti kevlarin ja Desert Eaglen vasta
#: freezetimen jälkeen, joten "ostivat tyhjäksi" **vahvistuu** -- taskuun jäi
#: 30 $/pelaaja eikä 750 $. Kierros 17 T on ainoa, jolla raha kasvoi ja
#: varusteet laskivat: siellä palautettiin ostos ikkunan aikana.
#:
#: Luvut on luettu tuotannon asetuksilla ajetusta parsinnasta, ja
#: :func:`test_ancient_calibration_verdicts_hold_on_the_real_demo`
#: (demotesti) ajaa saman ketjun oikealla demolla, jottei tämä taulu voi
#: ajautua erilleen siitä huomaamatta.
TRUTH_TABLE: tuple[Round, ...] = (
    Round(1, "CT", None, 110, 650, 850, "pistol", "pistoolikierros"),
    Round(1, "T", None, 140, 660, 860, "pistol", "pistoolikierros"),
    Round(2, "CT", True, 520, 3380, 3690, "full", "voitti pistoolin (S1)"),
    Round(2, "T", False, 2060, 120, 320, "eco", "yksi p250, yksi valo, yksi savu"),
    Round(11, "T", True, 2350, 3790, 5510, "full", "voitti edellisen"),
    Round(11, "CT", False, 2280, 600, 1580, "eco", "yksi säästetty M4 (S3)"),
    Round(14, "T", True, 530, 2960, 3550, "full", "voitti kierroksen 13 (S1)"),
    Round(17, "CT", True, 630, 3520, 5560, "full", "voitti edellisen"),
    Round(17, "T", False, 3400, 800, 1000, "eco", "raha säästetään AWP:hen"),
    Round(19, "T", True, 1540, 3920, 5310, "full", "voitti edellisen"),
    Round(19, "CT", False, 30, 2520, 2720, "force", "ostivat tyhjäksi (S2)"),
    Round(20, "CT", True, 1350, 2660, 5550, "full", "voitti edellisen"),
    Round(20, "T", False, 210, 2830, 3030, "force", "2x AK, 2x tec9; tyhjäksi"),
    Round(21, "CT", True, 2700, 2720, 5680, "full", "voitti edellisen"),
    Round(21, "T", False, 2220, 550, 750, "eco", "pitävät econ"),
)


@pytest.fixture
def thresholds(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


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
        "money_buy_end": k.left * PLAYERS,
        "money_spent": k.bought * PLAYERS,
        "equip_buy_end": k.equip * PLAYERS,
        "equip_round_start": (k.equip - k.bought) * PLAYERS,
        "players_buy_end": PLAYERS,
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


# --- Kalustolaskuri (Story 1.6) ------------------------------------------------
#
# Nämä ovat samaa ihmisen antamaa totuutta kuin yllä oleva taulu, mutta
# pelaajakohtaisina havaintoina. Ne asuvat täällä eivätkä demotestissä,
# koska ``pytest -m "not demo"`` on se ajo, jolla totuustaulu on tarkoitus
# säilyä: demotesti ohittaa itsensä koneella, jolla demoja ei ole, ja
# kalibrointi jäisi silloin valvomatta.
#
# TAVARALUETTELO JA PANSSARI on luettu Ancientista **ostoajan lopun** tickiltä
# 2026-08-29 (kierrokset 19, 20 ja 21). Story 1.5:ssä tässä olivat samojen
# pelaajien varustearvot ja kynnys 950 $; Story 1.6 vaihtoi mittarin
# havaintoon, koska varustearvo on ase + panssari + kranaatit yhtenä lukuna
# eikä erota ostettua asetta ilmaisesta pistoolista ja kahdesta valosta.
#
# STORY 1.9 SIIRSI MITTAUSHETKEÄ, ja yksi luku muuttui: **kierros 19 CT on
# 5/5, ei 4/5**. Kolmas pelaaja osti kevlarin ja Desert Eaglen vasta
# freezetimen jälkeen (ankkurilla ('knife', 'USP-S'), panssari 0; ostoajan
# lopussa ('knife', 'Desert Eagle', 'Smoke Grenade'), panssari 100). Veetin
# TUOMIO ei muutu -- kierros on force, ja "ostivat tyhjäksi" jopa vahvistuu,
# koska taskuun jäi 150 $ eikä 3 750 $ -- mutta hänen sanansa "yksi jäi
# ilmaiseen oletuspistooliin" oli lukema väärältä hetkeltä. Kierrokset 20 ja
# 21 pysyivät ennallaan (5/5 ja 2/5).
#
# TAVARALUETTELOT OVAT MYÖHEMMÄSTÄ HETKESTÄ kuin ennen, ja se näkyy niissä
# kahdella tavalla, jotka **eivät** vaikuta laskuriin:
#
#   * heitetyt kranaatit ovat poissa ja istutettu C4 vaihtanut paikkaa
#     (kierros 20 T mitataan 19,0 s, kierros 21 T 17,2 s ankkurin jälkeen)
#   * panssari on ottanut osumia: kierroksella 21 arvot ovat 90 ja 89, eivät
#     100
#
# Kumpikaan ei liikuta laskuria: sääntö on "panssari **ja vähintään yksi ase**
# hallussa", eikä kranaatti ole ase eikä 90 ole nolla. Jos jompikumpi joskus
# alkaa liikuttaa lukua, se näkyy täällä ennen kuin se näkyy raportissa.
#
# Kierroksen 21 luku perustuu edelleen oikeaan syyhyn. Veeti kuvasi
# kierroksen: "kahdella kevlar+pistooli ja yhdellä 300 $:n p250 ilman
# kevlaria". Vanha kynnys pudotti p250-pelaajan siksi, että 300 < 950; uusi
# sääntö pudottaa hänet siksi, ettei hänellä ole panssaria. Sama luku, eri
# väite -- ja jälkimmäinen on se, jonka Veeti sanoi.


class ArmedRound(NamedTuple):
    """Yhden kierroksen pelaajakohtaiset havainnot ja Veetin kuvaus.

    Attributes:
        round_no: Kierrosnumero.
        side: Puoli, jonka rivi tämä on.
        players: Viisi paria ``(tavaraluettelo, panssariarvo)`` demosta
            luettuna.
        armed: Montako niistä on aseistettu -- odotusarvo.
        basis: Veetin sanallinen kuvaus, kalibrointidokumentista.
    """

    round_no: int
    side: str
    players: tuple[tuple[tuple[str, ...], int], ...]
    armed: int
    basis: str


ARMED_TRUTH: tuple[ArmedRound, ...] = (
    ArmedRound(
        19,
        "CT",
        (
            (("Skeleton Knife", "Desert Eagle"), 100),
            (("Huntsman Knife", "Five-SeveN"), 100),
            (("knife", "Desert Eagle", "Smoke Grenade"), 100),
            (("Shadow Daggers", "P2000", "SSG 08"), 100),
            (("knife", "USP-S", "MP9", "High Explosive Grenade"), 100),
        ),
        5,
        'force, "ostivat tyhjäksi": kaikki viisi aseistautuivat, ja taskuun '
        "jäi 150 $/pelaaja",
    ),
    ArmedRound(
        20,
        "T",
        (
            (("knife_t", "Tec-9"), 100),
            (("knife_t", "Glock-18", "AK-47", "Flashbang"), 100),
            (("M9 Bayonet", "Glock-18", "AK-47"), 100),
            (("Talon Knife", "Tec-9"), 100),
            (("Bowie Knife", "Glock-18", "MAC-10", "Smoke Grenade",
              "Flashbang"), 100),
        ),
        5,
        "2x AK, 2x tec9, 1x mac10, kaikilla kevlar+kypärä -- kaikki viisi",
    ),
    ArmedRound(
        21,
        "T",
        (
            (("knife_t", "C4 Explosive", "P250"), 90),
            (("knife_t", "Glock-18"), 0),
            (("M9 Bayonet", "P250"), 0),
            (("Talon Knife", "Glock-18"), 0),
            (("Bowie Knife", "P250"), 89),
        ),
        2,
        'eco, "pitävät econ": kahdella kevlar+pistooli, yhdellä 300 $:n p250 '
        "ilman kevlaria",
    ),
)


@pytest.mark.parametrize(
    "k", ARMED_TRUTH, ids=[f"k{k.round_no}-{k.side}" for k in ARMED_TRUTH]
)
def test_armed_player_count_matches_the_human_reading(k: ArmedRound) -> None:
    """Laskuri antaa sen luvun, jonka Veeti näki replaystä.

    Sääntö luetaan adapterilta eikä kirjoiteta tässä uudelleen: testi, joka
    tarkistaisi omalla lausekkeellaan onko pelaajalla ase, todistaisi vain
    oman lausekkeensa.
    """
    rows = [
        {"inventory": inventory, "armor_value": armor}
        for inventory, armor in k.players
    ]
    counted = _armed_count(rows)
    assert counted == k.armed, (
        f"Kierros {k.round_no} {k.side}: dokumentti sanoo {k.armed} "
        f"({k.basis}), laskuri sanoi {counted}. "
        "Dokumentti on totuus -- korjaa sääntö tai aseluettelo, ei tätä taulua."
    )


def test_calibration_inventories_contain_no_unknown_names() -> None:
    """Kalibroinnin jokainen nimi on luokittelussa.

    Tuntematon nimi ei ole ase, joten tuntematon **ase** laskisi luvun
    hiljaa alas ja yllä oleva testi kaatuisi vasta lopputulokseen. Tämä
    nimeää syyn suoraan. Samalla se on aineistokohtainen vartija: jos
    aseluettelosta poistetaan nimi, tämä kertoo mikä.
    """
    unknown: set[str] = set()
    for k in ARMED_TRUTH:
        for inventory, _armor in k.players:
            unknown |= set(inventory) - KNOWN_INVENTORY_ITEMS
    assert unknown == set()


def test_unknown_name_does_not_arm_the_player() -> None:
    """Tuntematon nimi ei aseista, vaikka pelaajalla olisi panssari.

    Luokittelu on sallittujen aseiden luettelo: uusi veitsiskini ei saa
    aseistaa ketään. Raportoinnin puoli on adapterin testeissä, koska nimet
    kerätään kaikilta ankkurin riveiltä eikä vasta laskurissa.

    Nimi on tarkoituksella keksitty eikä oikea veitsiskini: oikea nimi
    päätyisi ennen pitkää luokitteluun uudesta demoerästä.
    """
    rows = [{"inventory": ("Ei-Ole-Olemassa-9000", "Glock-18"), "armor_value": 100}]
    assert _armed_count(rows) == 0


def test_unreadable_armor_or_inventory_empties_the_count() -> None:
    """Lukukelvoton havainto on ``null``, ei osittainen luku.

    Pelaaja pysyy ``players_buy_end``in jakajassa, joten osittainen luku
    väittäisi häntä aseettomaksi -- lukuvirhe näyttäisi säästökierrokselta.
    Nolla ja tyhjä luettelo ovat sen sijaan havaintoja.
    """
    armed = {"inventory": ("Bowie Knife", "P250"), "armor_value": 100}

    assert _armed_count([armed, {"inventory": None, "armor_value": 100}]) is None
    assert _armed_count([armed, {"inventory": (), "armor_value": None}]) is None
    # Havaintoja, eivät puutteita.
    assert _armed_count([armed, {"inventory": (), "armor_value": 0}]) == 1


def test_armed_count_needs_the_whole_distribution_not_the_team_sum() -> None:
    """Sama joukkuesumma, eri laskuri -- tämä on koko sarakkeen olemassaolon syy.

    Kolme pelaajaa kevlarilla ja ostetulla pistoolilla (950 $ kukin) ja kaksi
    ilmaispistoolilla (200 $) antaa saman joukkuesumman 3250 kuin viisi
    pelaajaa pelkillä kevlareilla (650 $ kukin). Ensimmäinen on puoliosto,
    jälkimmäinen ei ole, eikä ``equip_buy_end`` erota niitä.
    """
    half_buy = [
        {"inventory": ("knife", "P250"), "armor_value": 100} for _ in range(3)
    ] + [
        {"inventory": ("knife", "Glock-18"), "armor_value": 0} for _ in range(2)
    ]
    kevlars_only = [
        {"inventory": ("knife", "Glock-18"), "armor_value": 100} for _ in range(5)
    ]

    assert _armed_count(half_buy) == 3
    assert _armed_count(kevlars_only) == 0


def test_armor_and_weapon_are_both_required() -> None:
    """Kevlar ilman asetta ei riitä, eikä ase ilman kevlaria.

    Veetin määritelmä on "kevlar **ja** jokin parannettu ase". Kierroksen 21
    p250-pelaaja on jälkimmäinen tapaus, ja kierroksen 19 pelkkä-USP-pelaaja
    edellinen: molemmat esiintyvät aineistossa, joten kumpaakaan ehtoa ei voi
    pudottaa väittämättä jotain, mitä Veeti ei sanonut.
    """
    weapon_no_armor = [{"inventory": ("M9 Bayonet", "P250"), "armor_value": 0}]
    armor_no_weapon = [{"inventory": ("knife", "USP-S"), "armor_value": 100}]
    both = [{"inventory": ("Bowie Knife", "P250"), "armor_value": 100}]

    assert _armed_count(weapon_no_armor) == 0
    assert _armed_count(armor_no_weapon) == 0
    assert _armed_count(both) == 1
