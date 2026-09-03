"""Kalibrointidokumenttien totuustaulut regressiotesteinä.

Kaksi lähdettä, kaksi lohkoa ja **kaksi eri vaatimusta ympäristölle**.

``kalibrointi-kierrostyypit.md`` (Story 1.4)
    Veeti katsoi Ancient-demon 2D-replaynä ja kertoi jokaisesta kierroksesta,
    mitä siinä tapahtui. Luvut ovat dollaria **per pelaaja**, luettu
    ``classified``-taulun ``inputs``-rakenteesta, ja ne muunnetaan
    joukkuesummiksi kertomalla viidellä. Nämä testit eivät tarvitse mitään
    koneelta: ne rakentavat rivit käsin.

``kalibrointi-stack.md`` (Story 2.14)
    Stack-säännön aluejako, kattavuus ja osumataulukko kahdeksasta demosta.
    Nämä testit **lukevat oikeaa arkistoa** (``parsed/`` ja ``classified/``,
    eivät demotiedostoja) eivätkä kirjoita sinne mitään. Ne on merkitty
    ``@pytest.mark.archive``, jotta ne voi valita ja sulkea pois
    (``pytest -m "not archive"``), ja ne ohittavat itsensä siististi
    koneella, jolla arkistoa ei ole.

**Dokumentti on molemmissa totuus.** Jos jokin rivi taulusta ei mene läpi,
koodi on väärässä -- taulua ei muuteta koodin mukaiseksi eikä kynnysarvoa
viilata niin, että yksittäinen rivi menisi läpi. Kynnykset luetaan oikeasta
``settings.toml``ista: testi, joka keksisi omat rajansa, ei todistaisi mitään
siitä asetustiedostosta, jolla työkalu ajetaan.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import polars as pl
import pytest

from conftest import REAL_SETTINGS, require_parsed
from pappascout.adapters.demo_parser import _armed_count
from pappascout.archive.paths import ArchivePaths
from pappascout.constants import KNOWN_INVENTORY_ITEMS, SITE_AREAS
from pappascout.domain.economy import (
    classify_round,
    loss_bonus_if_lost,
    players_who_can_buy,
)
from pappascout.domain.models import (
    EconomySettings,
    ThresholdSettings,
    load_settings,
)
from pappascout.domain.sampling import CloudCell, site_groups
from pappascout.domain.schemas import ARMED_COLUMN, MONEY_DISTRIBUTION_COLUMN
from pappascout.errors import SchemaError
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages.aggregate import collect_team

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
#: Vartija koskee vain **joukkuesummasta johdettuja** kynnyksiä (ostettu ja
#: varustearvo per pelaaja). Puolioston ehdot A ja B eivät ole dollarirajoja
#: per pelaaja vaan pelaajalaskureita, ja niillä on oma vartijansa alempana.
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
        armed: Montako pelaajaa oli aseistettu (ehto A). ``None`` tarkoittaa
            "ei kirjattu": rivi ei päädy haaraan, joka lukisi laskurin, ja jos
            joskus päätyy, kierros jää luokittelematta ja tämä testi kaatuu.
            Arvaus olisi pahempi kuin puute.
        left_players: Taskuun jäänyt raha pelaajittain (ehto B), jos se on
            dokumentissa. ``None`` -> tasajako, ks. :func:`_rows`.

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
    armed: int | None = None
    left_players: tuple[int, ...] | None = None


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
    # Aseistetut ARMED_TRUTH-taulusta (5/5) ja saldot dokumentin lauseesta
    # "todelliset saldot olivat 0, 0, 50, 50, 50" -- summa 150 = 30 * 5.
    Round(
        19, "CT", False, 30, 2520, 2720, "force", "ostivat tyhjäksi (S2)",
        armed=5, left_players=(50, 50, 50, 0, 0),
    ),
    Round(20, "CT", True, 1350, 2660, 5550, "full", "voitti edellisen"),
    # Aseistetut ARMED_TRUTH-taulusta (5/5). Saldojen jakaumaa ei ole
    # kirjattu, joten se on tasajako -- ja
    # test_no_split_of_the_observed_money_can_satisfy_the_next_round_condition
    # todistaa, ettei mikään jakauma voisi muuttaa tuomiota.
    Round(
        20, "T", False, 210, 2830, 3030, "force", "2x AK, 2x tec9; tyhjäksi",
        armed=5,
    ),
    Round(21, "CT", True, 2700, 2720, 5680, "full", "voitti edellisen"),
    Round(21, "T", False, 2220, 550, 750, "eco", "pitävät econ"),
)


@pytest.fixture
def thresholds(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


@pytest.fixture
def economy(settings_file: Path) -> EconomySettings:
    """``[economy]``-osio: puolioston ehto B lukee siitä häviöbonuksen portaat."""
    return load_settings(settings_file, env_files=()).economy


def _rows(k: Round) -> tuple[dict, dict | None]:
    """Kierrosrivi ja sen edellinen kierros ``ROUNDS``-muodossa.

    Per pelaaja -luvut kerrotaan viidellä, jotta ``classify_round`` päätyy
    jakaessaan tasan samoihin lukuihin kuin dokumentissa. Käytetty raha ei
    kuulu totuustauluun eikä vaikuta yhteenkään sääntöön; se asetetaan
    ostetun summan mukaiseksi, jotta perustelun rahaluvut ovat uskottavia.

    **Rahajakauma on tasajako, ellei taulu anna sitä.** Totuustaulu kirjaa
    joukkuesummat, koska se on kirjoitettu ennen kuin jakauma oli olemassa.
    Tasajako on siis oletus eikä havainto -- ja
    :func:`test_no_split_of_the_observed_money_can_satisfy_the_next_round_condition`
    todistaa, ettei oletus voi muuttaa yhtäkään tuomiota: aineiston forceilla
    koko joukkueen saldo on pienempi kuin se, mitä kolme pelaajaa tarvitsisi
    edes suurimmalla häviöbonuksella.
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
        ARMED_COLUMN: k.armed,
        MONEY_DISTRIBUTION_COLUMN: (
            list(k.left_players)
            if k.left_players is not None
            else [k.left] * PLAYERS
        ),
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
def test_truth_table_row_matches_the_classifier(k: Round, thresholds, economy) -> None:
    """Jokainen dokumentin rivi luokittuu siksi, mitä Veeti näki replaystä."""
    row, previous = _rows(k)
    decision = classify_round(row, previous, thresholds, economy=economy, loss_count=2)
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
    demossa yhtäkään puoliostoa eikä poikkeamaa. ``half`` ja ``anomaly`` on
    siksi pinnattu erikseen ``test_economy.py``:n käsin rakennetuilla
    tapauksilla, ja puolioston oma havainto tuli vasta toisesta demosta
    (``inferno_vs_ryhmarama`` kierros 10, ks. ``test_inferno_*`` alempana).
    Jos tämä taulu joskus saa puoliostorivin, testi kaatuu -- ja se on hyvä:
    silloin Ancientin tuomiot on luettava uudelleen.
    """
    assert {k.truth for k in TRUTH_TABLE} == {"pistol", "full", "force", "eco"}
    assert not [k for k in TRUTH_TABLE if k.truth in ("half", "anomaly")]


@pytest.mark.parametrize("k", TRUTH_TABLE, ids=[_test_id(k) for k in TRUTH_TABLE])
def test_the_loss_counter_never_changes_the_verdict(
    k: Round,
    thresholds,
    economy,
) -> None:
    """Laskuri vaikuttaa nyt bonukseen -- muttei yhteenkään tuomioon tässä.

    Story 1.10 palautti ``loss_count``in päätöksentekoon: häviöbonus on
    suoraan sen funktio, ja bonus on puolioston ehdon B toinen puolisko.
    Totuustaulun riviä se ei silti liikuta yhdelläkään laskurin arvolla, ja
    juuri se on tämän testin väite. Se ei ole itsestäänselvä: laskuri 0
    antaisi bonukseksi 1 900 $ ja laskuri 4 arvon 3 400 $, eli 1 500 $
    liikkumavaraa ehdon B kummallekin puolelle.

    Loss count ei ole taulussa sarakkeena, koska sitä ei mitattu -- ja koska
    tämä testi tekee siitä tarpeettoman: tuomio kestää laskurin joka arvon.
    """
    row, previous = _rows(k)
    types = {
        classify_round(
            row, previous, thresholds, economy=economy, loss_count=lc
        ).round_type
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


# --- Ensimmäinen havaittu puoliosto (Story 1.10) --------------------------------
#
# Ancientin aineistossa ei ole yhtäkään puoliostoa. ``inferno_vs_ryhmarama``
# sulki aukon: Veeti katsoi kierrokset 6 ja 10 demosta ja antoi niille
# tuomiot, ja kierros 11 vahvisti kierroksen 10 ennusteen.
#
# MITÄ NÄMÄ KIERROKSET TODISTAVAT -- ja mitä eivät. Molemmissa on viisi
# aseistettua pelaajaa, joten ehto A ei erota niitä lainkaan; erottelun tekee
# ehto B. Ne EIVÄT kuitenkaan erota uutta sääntöä poistuneesta
# keskiarvosäännöstä: myös vanha ``force_money_left_max = 1000`` antaisi
# kierrokselle 6 forcen (480 $/pelaaja) ja kierrokselle 10 puolioston
# (1 580 $/pelaaja). Sama pätee kaikkiin kuuden demon 23 hävityn jälkeiseen
# ostokierrokseen: nolla eroa.
#
# Kierros 6 luokittui väärin vain ENNEN Story 1.9:ää, kun raha luettiin
# freezetimen lopusta -- korjauksen teki mittaus, ei sääntö. Se, mitä nämä
# rivit todistavat, on että uusi sääntö toistaa Veetin tuomiot; se, että
# sääntö kestää epätasaisen jakauman, on pinnattu ``test_economy.py``:n
# käsin rakennetuilla riveillä.
#
# Luvut on luettu ostoajan lopusta tuotannon asetuksilla (parsed-taulu
# 2026-08-29). Kierrosten 6 ja 10 saldot ovat myös kalibrointidokumentin
# taulussa; kierroksen 11 saldot on luettu samasta taulusta.


class InfernoRound(NamedTuple):
    """Yksi mitattu kierros ``inferno_vs_ryhmarama``-demosta ja Veetin tuomio.

    Attributes:
        round_no: Kierrosnumero.
        loss_count: Kierrokseen mentäessä voimassa oleva laskuri. **Vaikuttaa
            tuomioon**: häviöbonus on sen porras.
        money: ``money_buy_end`` joukkuesummana.
        money_players: Saldot pelaajittain.
        spent: ``money_spent`` joukkuesummana.
        equip: ``equip_buy_end`` joukkuesummana.
        equip_start: ``equip_round_start`` joukkuesummana.
        armed: Aseistettujen laskuri.
        can_buy: Montako pelaajaa pystyy normaaliin ostoon ensi kierroksella
            -- odotusarvo. ``None``, jos kierros ei saavuta ehtoa B lainkaan
            (esim. täysi osto ratkeaa jo varustearvosta).
        truth: Veetin tuomio.
        basis: Veetin sanat.
    """

    round_no: int
    loss_count: int
    money: int
    money_players: tuple[int, ...]
    spent: int
    equip: int
    equip_start: int
    armed: int
    can_buy: int | None
    truth: str
    basis: str


INFERNO_TRUTH: tuple[InfernoRound, ...] = (
    InfernoRound(
        6, 1, 2400, (1750, 500, 150, 0, 0), 14750, 15350, 1000, 5, 0, "force",
        "neljällä pelaajalla viidestä ei ole seuraavalla kierroksella varaa "
        "ostaa jos he häviävät",
    ),
    InfernoRound(
        10, 4, 7900, (2150, 2050, 2000, 900, 800), 10900, 11900, 1000, 5, 5,
        "half",
        "kutsuisin sitä puoliostoksi koska he todennäköisesti ostavat ensi "
        "kierroksella -- ja niin kävikin, kierroksella 11 normaali osto",
    ),
    # Tukeva havainto: kierros 11 vahvisti kierroksen 10 ennusteen. Se ei ole
    # puoliosto vaan **normaali osto** (viisi AK:ta, 4 940 $/pelaaja), ja se
    # on täällä siksi, että väite "he ostivat ensi kierroksella" olisi
    # kiinnitetty lukuihin eikä vain toistettu kolmessa kommentissa.
    # Ehtoja A ja B ei lasketa: täysi osto ratkeaa jo varustearvosta.
    InfernoRound(
        11, 4, 1500, (650, 550, 300, 0, 0), 23700, 24700, 1000, 5, None,
        "full",
        "kierros 11 vahvisti ennusteen: viisi AK:ta",
    ),
)


def _inferno_rows(k: InfernoRound) -> tuple[dict, dict]:
    """Kierrosrivi ja sen edellinen kierros ``ROUNDS``-muodossa."""
    row = {
        "round_no": k.round_no,
        "side": "T",
        "status": "ok",
        "money_buy_end": k.money,
        "money_spent": k.spent,
        "equip_buy_end": k.equip,
        "equip_round_start": k.equip_start,
        "players_buy_end": PLAYERS,
        ARMED_COLUMN: k.armed,
        MONEY_DISTRIBUTION_COLUMN: list(k.money_players),
        "survivors_equip_prev": 0,
    }
    previous = {
        "round_no": k.round_no - 1,
        "side": "T",
        "won": False,
        "survivors": 0,
    }
    return row, previous


#: Ne kierrokset, jotka oikeasti saavuttavat ehdot A ja B.
INFERNO_BUY_ROUNDS: tuple[InfernoRound, ...] = tuple(
    k for k in INFERNO_TRUTH if k.can_buy is not None
)


@pytest.mark.parametrize(
    "k", INFERNO_TRUTH, ids=[f"k{k.round_no}-{k.truth}" for k in INFERNO_TRUTH]
)
def test_inferno_round_matches_veetis_verdict(
    k: InfernoRound, thresholds, economy
) -> None:
    """Kierrokset 6, 10 ja 11 luokittuvat siksi, mitä Veeti näki demosta."""
    row, previous = _inferno_rows(k)
    decision = classify_round(
        row, previous, thresholds, economy=economy, loss_count=k.loss_count
    )
    assert decision.round_type == k.truth, (
        f"Kierros {k.round_no}: Veeti sanoo {k.truth!r} ({k.basis}), "
        f"luokittelija sanoi {decision.round_type!r}. "
        f"Perustelu: {decision.reason}"
    )


@pytest.mark.parametrize(
    "k",
    INFERNO_BUY_ROUNDS,
    ids=[f"k{k.round_no}-{k.truth}" for k in INFERNO_BUY_ROUNDS],
)
def test_inferno_reason_names_both_counters(
    k: InfernoRound, thresholds, economy
) -> None:
    """Perustelu kertoo molempien ehtojen laskurit, ei vain ratkaisevaa.

    Lukija ei muuten näe, kumpi ehto hylkäsi kierroksen. Perustelun on myös
    näytettävä **saldot sellaisinaan**: pelkkä "0/5 pystyy ostamaan" ei ole
    tarkistettavissa demoa vasten, koska siitä ei näe, kuinka läheltä kukin
    jäi.
    """
    row, previous = _inferno_rows(k)
    decision = classify_round(
        row, previous, thresholds, economy=economy, loss_count=k.loss_count
    )
    bonus = economy.loss_bonus_steps[k.loss_count]
    assert f"{k.armed}/{PLAYERS} aseistettua" in decision.reason
    assert (
        f"{k.can_buy}/{PLAYERS} pystyy ostamaan ensi kierroksella"
        in decision.reason
    )
    # Jakauma sellaisenaan, yksikkö jokaisessa luvussa.
    assert ", ".join(f"{m} $" for m in k.money_players) in decision.reason
    # Häviöbonus näkyviin: ilman sitä lukija ei voi laskea laskuria itse.
    assert f"häviöbonus {bonus} $" in decision.reason
    assert decision.inputs["players_can_buy"] == k.can_buy
    assert decision.inputs["players_armed"] == k.armed
    assert decision.inputs["loss_bonus_if_lost"] == bonus


def test_inferno_rows_are_internally_consistent(thresholds) -> None:
    """Taulun rivit eivät saa väittää kahta eri asiaa samasta kierroksesta.

    Kolme invarianttia, joista mikään ei ole itsestäänselvä käsin kirjatussa
    taulussa:

    * jakauman summa on ``money_buy_end`` -- muuten rivi mittaisi ehtoa B eri
      rahalla kuin se, jonka se kirjaa joukkuesummaksi
    * jakaumassa on tasan viisi pelaajaa, sama joukko kuin laskureissa
    * ostettu summa ylittää ``force_buy_min``in, eli kierros oikeasti päätyy
      ostohaaraan. Jos ``equip_start`` olisi väärin, rivi menisi läpi
      ecohaaran kautta eikä koettelisi ehtoja lainkaan -- eikä yksikään
      demotesti kata sitä.
    """
    for k in INFERNO_TRUTH:
        assert sum(k.money_players) == k.money, k.round_no
        assert len(k.money_players) == PLAYERS, k.round_no
        bought_pp = (k.equip - k.equip_start) / PLAYERS
        assert bought_pp >= thresholds.force_buy_min, k.round_no
        assert 0 <= k.armed <= PLAYERS, k.round_no


def test_the_armed_counter_cannot_separate_the_two_inferno_rounds() -> None:
    """Ehto A on identtinen kierroksilla 6 ja 10; ehto B ei ole.

    Tämä ei todista, että ehto A olisi tarpeeton -- se mittaa eri asiaa. Se
    todistaa, ettei ehto A yksin riitä erottamaan forcea puoliostosta, ja
    että ehto B tekee sen erottelun näillä kahdella kierroksella.
    """
    assert len({k.armed for k in INFERNO_BUY_ROUNDS}) == 1
    assert len({k.can_buy for k in INFERNO_BUY_ROUNDS}) == 2


def test_the_next_round_threshold_keeps_a_margin_to_the_nearest_observation(
    thresholds, economy
) -> None:
    """Ehdon B raha-kynnys ei saa olla kosketusetäisyydellä havainnosta.

    Sama vartija kuin muilla per pelaaja -kynnyksillä, mutta havainto on
    tässä **yhden pelaajan ostovoima** (oma saldo + häviöbonus). Aineistossa
    on molemmat puolet: kierroksen 6 rikkaimman ostovoima jää kynnyksen alle
    (1 750 + 1 900 = 3 650) ja kierroksen 10 köyhimmän ylittää sen
    (800 + 3 400 = 4 200).

    Ilman tätä kynnys voisi liukua aineiston reunaan huomaamatta -- ja
    yhden ostoksen verran siirtynyt saldo kääntäisi luokan.
    """
    below: list[int] = []
    above: list[int] = []
    for k in INFERNO_BUY_ROUNDS:
        bonus = economy.loss_bonus_steps[k.loss_count]
        for money in k.money_players:
            power = min(money + bonus, economy.max_money)
            if power >= thresholds.normal_buy_money_min:
                above.append(power)
            else:
                below.append(power)

    assert below and above, "yksipuolinen aineisto ei mittaa marginaalia"
    assert max(below) < thresholds.normal_buy_money_min <= min(above)
    assert thresholds.normal_buy_money_min - max(below) >= MIN_MARGIN
    assert min(above) - thresholds.normal_buy_money_min >= MIN_MARGIN


def test_the_loss_bonus_is_the_step_the_counter_points_at(
    thresholds, economy
) -> None:
    """Bonus on ``steps[loss_count]``, ei ``steps[loss_count + 1]``.

    ``settings.toml`` sanoo sen suoraan: indeksi on loss count, joten
    puoliajan alku (laskuri 1) antaa pistoolihäviöstä 1 900 $. Laskuri kuvaa
    tilaa kierrokseen mentäessä, ja juuri se porras maksetaan, jos kierros
    hävitään. Yhden liian suuri indeksi antaisi jokaiselle pelaajalle 500 $
    liikaa ostovoimaa -- ja kierroksen 6 laskuri olisi 1/5 eikä 0/5.
    """
    assert loss_bonus_if_lost(1, thresholds, economy) == 1900
    assert loss_bonus_if_lost(4, thresholds, economy) == 3400
    for k in INFERNO_BUY_ROUNDS:
        assert loss_bonus_if_lost(k.loss_count, thresholds, economy) == (
            economy.loss_bonus_steps[k.loss_count]
        )


def test_the_loss_bonus_clamps_instead_of_raising(thresholds, economy) -> None:
    """Reunat eivät kaadu: nolla, negatiivinen ja liian lyhyt porraslista.

    Katkaisu on turva eikä sääntö -- asetusten lataus vaatii tasan
    ``loss_count_max + 1`` porrasta. Käsin rakennettu osio tai vioittunut
    laskuri ei silti saa kaataa koko ajoa ``IndexError``iin: silloin yksi
    rivi veisi mukanaan koko demon.
    """
    steps = economy.loss_bonus_steps
    assert loss_bonus_if_lost(0, thresholds, economy) == steps[0]
    # Negatiivinen laskuri ei ole mahdollinen loss_countsin jäljiltä, mutta
    # Pythonissa steps[-1] antaisi hiljaa **suurimman** bonuksen.
    assert loss_bonus_if_lost(-3, thresholds, economy) == steps[0]
    assert loss_bonus_if_lost(99, thresholds, economy) == steps[-1]

    short = economy.model_copy(update={"loss_bonus_steps": [1400, 1900]})
    assert loss_bonus_if_lost(4, thresholds, short) == 1900


def test_players_who_can_buy_refuses_a_hole_in_the_distribution(
    thresholds, economy
) -> None:
    """Julkisen funktion sopimus ei saa elää vain kutsujissa.

    Tyhjä saldo nollana väittäisi pelaajaa rahattomaksi ja kääntäisi
    puolioston forceksi. Kutsujat tarkistavat sen jo, mutta funktio on
    julkinen -- seuraava kutsuja ei välttämättä tarkista.
    """
    with pytest.raises(SchemaError):
        players_who_can_buy([2000, None, 0], 1900, thresholds, economy)


def test_the_buying_power_is_capped_at_the_money_ceiling(
    thresholds, economy
) -> None:
    """``saldo + bonus`` ei voi ylittää ``[economy].max_money``a.

    Peli leikkaisi ylimenevän pois, joten katkaisematta laskuri lupaisi
    ostovoimaa rahalla, jota pelaajalla ei koskaan ole.

    Tuotantoarvoilla katto ei pure (16 000 $ vs. 4 000 $), joten se pinnataan
    matalalla katolla: 2 000 + 2 400 = 4 400 riittäisi rajalle 4 000, mutta
    3 000 dollarin katto leikkaa summan alle sen. Ilman katkaisua tämä testi
    palauttaisi 1.
    """
    low_ceiling = economy.model_copy(update={"max_money": 3000})
    assert (
        players_who_can_buy([2000], 2400, thresholds, low_ceiling) == 0
    ), "katto ei leikannut summaa"
    # Sama pelaaja ja sama bonus tuotannon katolla: raja ylittyy.
    assert players_who_can_buy([2000], 2400, thresholds, economy) == 1


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


def test_no_split_of_the_observed_money_can_satisfy_the_next_round_condition(
    thresholds, economy
) -> None:
    """Tasajako-oletus ei voi muuttaa yhtäkään havaittua force-tuomiota.

    Totuustaulu kirjaa joukkuesummat, joten :func:`_rows` joutuu olettamaan
    jakauman. Oletus olisi vaarallinen, jos jokin muu jakauma antaisi eri
    tuloksen -- silloin taulu pinnaisi säännön keksityllä syötteellä.

    Se ei ole mahdollista, ja tämä todistaa sen ilman jakaumaa: ehto B vaatii
    ``normal_buy_players_min`` pelaajaa, joilla kullakin on vähintään
    ``normal_buy_money_min - bonus`` omaa rahaa. Suurimmallakin bonuksella
    (3 400 $) tarve on 3 x 600 = 1 800 $, ja aineiston forceilla koko
    joukkueen saldo on 150 $ ja 1 050 $. Rahaa ei yksinkertaisesti ole
    tarpeeksi mihinkään jakaumaan.
    """
    forces = [k for k in TRUTH_TABLE if k.truth == "force"]
    assert forces

    per_player_need = thresholds.normal_buy_money_min - max(economy.loss_bonus_steps)
    team_need = thresholds.normal_buy_players_min * per_player_need

    for k in forces:
        team_total = k.left * PLAYERS
        assert team_total < team_need, (
            f"Kierros {k.round_no} {k.side}: joukkueella oli {team_total} $, "
            f"ja ehto B vaatisi {team_need} $ pelkkään kolmen pelaajan "
            "ostokykyyn. Tasajako ei siis ole enää vaaraton oletus -- "
            "kirjaa tälle riville todellinen jakauma left_players-kenttään."
        )


def test_no_calibration_round_exercises_the_armed_condition(thresholds) -> None:
    """Ehto A:n kynnys on **lausuttu sääntö, ei havainto** -- ja tämä sanoo sen.

    Aineiston ainoa vähän aseistettu kierros on Ancientin 21 T (2/5), ja
    juuri sitä on aiemmin käytetty kynnyksen ``armed_players_min = 3``
    perusteluna. Se ei kelpaa: kierros ratkeaa jo ostorajalla
    ``force_buy_min`` (ostettu 550 $/pelaaja) eikä koskaan saavuta ehtoa A.

    Aineiston kaksi ostokierrosta ovat molemmat 5/5 aseistettuja, joten
    kynnys voisi olla mikä tahansa väliltä 1-5 ilman että yksikään tuomio
    muuttuisi. Kynnys nojaa siis käyttäjän lausumaan rajaan ("vähintään
    kolmella kevlar ja jokin parannettu ase"), ja tämä testi pitää sen
    näkyvissä: jos aineistoon joskus tulee kierros, joka oikeasti koettelee
    ehtoa A, testi kaatuu -- ja silloin kynnyksen voi kalibroida.
    """
    by_round = {(k.round_no, k.side): k for k in ARMED_TRUTH}
    truth = {(k.round_no, k.side): k for k in TRUTH_TABLE}

    # Vähän aseistettu kierros ei saavuta ehtoa A: se on eco jo ostorajalla.
    saved = by_round[(21, "T")]
    assert saved.armed < thresholds.armed_players_min
    assert truth[(21, "T")].bought < thresholds.force_buy_min

    # Ne kierrokset, jotka saavuttavat ehdon A, ovat kaikki täysin
    # aseistettuja -- eli kynnyksen yläpuolella eivätkä sen tuntumassa.
    # Vertailu on ``>=``, sama kuin säännössä: aito force tasan kolmella
    # aseistetulla ei saa kaatua vartijaan.
    reaching = [
        by_round[key]
        for key, k in truth.items()
        if k.prev_won is False and k.bought >= thresholds.force_buy_min
        and key in by_round
    ]
    assert reaching, "aineistossa ei ole yhtään ehdon A saavuttavaa kierrosta"
    for k in reaching:
        assert k.armed >= thresholds.armed_players_min, (k.round_no, k.side)
    # Havaintoja on vain kynnyksen yläpuolelta, joten "tyhjää väliä" ei ole
    # kummallakaan puolella -- se on tämän testin koko väite.
    assert {k.armed for k in reaching} == {PLAYERS}


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


# --- Stack-säännön kalibrointi (Story 2.14) -------------------------------------
#
# Lähde on ``_bmad-output/implementation-artifacts/kalibrointi-stack.md``.
# Nämä testit lukevat arkiston ``parsed/``- ja ``classified/``-taulut
# mutteivät demotiedostoja, eivätkä kirjoita arkistoon mitään. Toisella
# koneella ne ohittavat itsensä (:func:`conftest.require_parsed`).

#: Ancientin kolme demoa. Aluejaon on oltava **sanatarkasti sama** jokaisesta:
#: se on koko solumediaanin peruste, ja liikeratapohjainen johtaminen hylättiin
#: juuri siksi, ettei se ollut.
ANCIENT_DEMOS = (
    "Ancient_vs_kaljukostaja",
    "ANCIENT_vs_RCAVE_VETERANS",
    "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1",
)

#: Ancientin johdettu aluejako, dokumentin taulukosta rivi riviltä.
ANCIENT_GROUPS = {
    "BombsiteA": "A",
    "CTSpawn": "A",
    "House": "A",
    "MainHall": "A",
    "Outside": "A",
    "SideHall": "A",
    "Alley": "B",
    "BombsiteB": "B",
    "Ramp": "B",
    "Ruins": "B",
    "SideEntrance": "B",
    "TSideLower": "B",
    "TSideUpper": "B",
    "Tunnel": "B",
    "Water": "B",
}

#: Ancientin alueet, jotka jäävät **kummankin ryhmän ulkopuolelle**: kartan
#: jaettu keski. Ne eivät ole puuttuva havainto vaan havainto siitä, ettei
#: kumpikaan site ole aidosti lähempänä.
ANCIENT_SHARED = ("Middle", "TSpawn", "TopofMid")

#: Nuken kaksi demoa. Siteet ovat päällekkäin eri kerroksissa, joten sääntö
#: vaikenee -- ja juuri se on kirjattava kattavuuteen eikä nollaosumaksi.
NUKE_DEMOS = (
    "Nuke_vs_imuaijat",
    "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1",
)

#: Arkiston kaksi subjektijoukkuetta. **Tunniste eikä hakemistolistaus**:
#: ``classified/`` sisältää rivin jokaiselle kokoonpanolle, ja sama joukkue on
#: siellä useamman tunnisteen alla (puolenvaihto ja vaihtopelaajat). Kaikkien
#: yli iterointi laskisi samat kierrokset kahdesti.
CALIBRATION_TEAMS = ("9ac92660986558d3", "ff03fb54599d3311")

#: Kaikki kahdeksan arkiston demoa.
CALIBRATION_DEMOS = (
    *ANCIENT_DEMOS,
    *NUKE_DEMOS,
    "Anubis_vs_ryhmarama",
    "anubis_vs_RCAVE_VETERANS",
    "inferno_vs_ryhmarama",
)

#: Kalibroinnin luvut, dokumentin osiosta "Osumat: 9 kierrosta 66:sta".
STACK_HITS = 10
STACK_ROUNDS = 9
STACK_SCANNED = 66
STACK_CT_ROUNDS = 93
STACK_SILENCED_ROUNDS = 27

#: Osumataulukko **näytepisteittäin**: (kartta, demo, kierros, tyyppi, hetki,
#: ryhmä, pelaajat, elossa). Rivi per osuma, kuten dokumentissa -- Anubiksen
#: k4 on siksi kahdesti, 15 s kohdalla 5/5 ja 30 s kohdalla 4/5. Juuri se pari
#: on syy siihen, ettei kierros voi kantaa yhtä maksimia: yhtenä rivinä se
#: väittäisi viittä pelaajaa myös 30 s kohdalla.
#:
#: Lajiteltuna, jotta vertailu on riippumaton karttojen ja joukkueiden
#: käsittelyjärjestyksestä.
STACK_TABLE = sorted(
    [
        ("de_ancient", "ANCIENT_vs_RCAVE_VETERANS", 13, "pistol", 15.0, "B", 4, 5),
        ("de_ancient", "ANCIENT_vs_RCAVE_VETERANS", 15, "full", 15.0, "B", 4, 5),
        ("de_ancient", "ANCIENT_vs_RCAVE_VETERANS", 18, "eco", 15.0, "B", 4, 5),
        (
            "de_ancient",
            "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1",
            16,
            "full",
            30.0,
            "A",
            4,
            5,
        ),
        ("de_ancient", "Ancient_vs_kaljukostaja", 2, "eco", 30.0, "A", 5, 5),
        ("de_ancient", "Ancient_vs_kaljukostaja", 7, "eco", 6.0, "A", 4, 5),
        ("de_ancient", "Ancient_vs_kaljukostaja", 12, "full", 30.0, "A", 4, 5),
        ("de_anubis", "Anubis_vs_ryhmarama", 4, "eco", 15.0, "B", 5, 5),
        ("de_anubis", "Anubis_vs_ryhmarama", 4, "eco", 30.0, "B", 4, 5),
        ("de_anubis", "Anubis_vs_ryhmarama", 11, "full", 15.0, "B", 4, 5),
    ]
)

#: Montako osumaa syntyy, jos siten OMALLA alueella vaaditaan olevan useampi
#: kuin yksi pelaaja: (vaatimus, kierroksia, osumia). Mitattu 3.9.
#:
#: Taulukko on tässä siksi, että valinta "yksi riittää" olisi **tietoinen
#: eikä oletusarvo**: sen vaihtoehdot on mitattu, ja niiden hinta on
#: nähtävissä. Osumista viidellä on tasan yksi pelaaja sitellä, kolmella kaksi
#: ja kahdella kolme, joten tiukennus ei poistaisi kohinaa vaan puolet
#: havainnoista.
SITE_PRESENCE_TABLE = ((1, 9, 10), (2, 5, 5), (3, 2, 2))


def _real_settings():
    """Oikea ``settings.toml``: kynnykset luetaan siitä, mitä työkalu käyttää.

    Ei ``settings_file``-fixtuuria, toisin kuin muualla tässä tiedostossa: se
    ohjaa arkiston juuren ``tmp_path``iin, ja nämä testit lukevat **oikeaa**
    arkistoa. Juuri tulee :func:`conftest.require_parsed`ilta, joten kopio
    ei olisi vain turha vaan harhaanjohtava.
    """
    return load_settings(REAL_SETTINGS, env_files=())


def _site_groups(root: Path, map_demo_id: str, limits: ThresholdSettings):
    """Yhden demon siteryhmät sen omasta pistepilvestä."""
    df = pl.read_parquet(root / "parsed" / map_demo_id / "callouts.parquet")
    cells = [
        CloudCell(area, x, y, z)
        for area, x, y, z in zip(
            df["area"], df["cell_x"], df["cell_y"], df["cell_z"], strict=True
        )
    ]
    return site_groups(
        cells,
        margin=limits.stack_group_margin,
        separation_min=limits.stack_site_separation_min,
    )


def _stack_reports(root: Path, limits: ThresholdSettings | None = None):
    """Molempien joukkueiden raportit **muistiin**, arkistoa muuttamatta.

    Vaiheen oma ``run`` kirjoittaisi ``report.json``in kehittäjän arkistoon;
    testi ei saa muuttaa sitä aineistoa, jota vasten se mittaa. ``_aggregate``
    on saman moduulin funktio ja tekee täsmälleen sen, mitä ``run`` tekee
    ennen kirjoitusta -- alaviiva on merkki siitä, ettei sitä pidä kutsua
    tuotantokoodista, ei siitä ettei sitä saa lukea.
    """
    settings = _real_settings()
    thresholds = limits or settings.thresholds
    archive = ArchivePaths(root=root)
    return [
        aggregate_stage._aggregate(
            archive,
            collect_team(archive, team, thresholds),
            thresholds,
            settings.league,
            settings.aggregate,
        )
        for team in CALIBRATION_TEAMS
    ]


def _stack_points(reports) -> list[tuple]:
    """Kaikkien raporttien stack-osumat näytepisteittäin, lajiteltuna."""
    found = []
    for report in reports:
        for anomaly in report.anomalies:
            if anomaly.rule != "stack":
                continue
            for entry in anomaly.rounds:
                for point in entry.points:
                    found.append(
                        (
                            anomaly.map_name,
                            entry.map_demo_id,
                            entry.round_no,
                            entry.round_type,
                            point.sample_t_s,
                            anomaly.site,
                            point.players,
                            point.alive,
                        )
                    )
    return sorted(found)


@pytest.mark.archive
def test_the_ancient_site_groups_are_identical_in_all_three_demos() -> None:
    """Sama kartta, kolme demoa, **sanatarkasti sama** aluejako.

    Tämä on solumediaanin koko peruste. Liikeratapohjainen johtaminen antoi
    saman kartan kahdesta demosta 32 % ja 94 % kattavuuden ja neljä
    ristiriitaista aluetta; havaintopainotettu keskiarvo viisi. Solumediaani
    antaa nolla, ja se on tämän testin väite.
    """
    root = require_parsed(*ANCIENT_DEMOS)
    limits = _real_settings().thresholds
    found = [_site_groups(root, demo, limits) for demo in ANCIENT_DEMOS]
    assert all(groups is not None for groups in found)
    for groups, demo in zip(found, ANCIENT_DEMOS, strict=True):
        assert groups == ANCIENT_GROUPS, demo
        for area in ANCIENT_SHARED:
            assert area not in groups, f"{demo}: {area}"


@pytest.mark.archive
def test_nuke_stays_silent_because_its_sites_do_not_separate() -> None:
    """Vartija vaientaa Nuken **ilman että karttaa nimetään koodissa**.

    Suhde ``erotus / (säde_A + säde_B)`` on Nukella 0,47-0,54 ja kolmella
    muulla kartalla 3,70-5,04; kynnys 2,0 erottaa ne puhtaasti.
    """
    root = require_parsed(*NUKE_DEMOS)
    limits = _real_settings().thresholds
    for demo in NUKE_DEMOS:
        assert _site_groups(root, demo, limits) is None, demo


@pytest.mark.archive
def test_every_other_map_does_give_site_groups() -> None:
    """Vartijan toinen suunta: se vaientaa vain sen, mitä sen pitääkin.

    Ilman tätä väitettä kynnyksen nostaminen vaientaisi koko säännön eikä
    yksikään testi kertoisi siitä -- nolla osumaa näyttäisi mitatulta
    negatiiviselta.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds
    speaking = {
        demo
        for demo in CALIBRATION_DEMOS
        if _site_groups(root, demo, limits) is not None
    }
    assert speaking == set(CALIBRATION_DEMOS) - set(NUKE_DEMOS)


@pytest.mark.archive
def test_the_stack_rule_finds_exactly_the_calibrated_sample_points() -> None:
    """Kalibroinnin osumataulukko rivi riviltä, molemmilta joukkueilta.

    Taulukko on **näytepisteittäin** eikä kierroksittain, ja Anubiksen k4 on
    siksi kahdesti: 15 s kohdalla 5/5 ja 30 s kohdalla 4/5. Juuri se pari
    kaataisi rakenteen, joka kantaa kierrosta kohden yhden maksimin.

    **Subjektin rivit tunnistetaan kokoonpanotunnisteista**, eivät
    ``classified/``-hakemiston nimestä: sama demo on arkistossa kahdesti,
    kerran kummallakin joukkueella, ja väärä lähde antoi kalibroinnin
    ensimmäisessä versiossa 7 osumaa 59 kierroksesta -- vastustajan
    kierroksilta.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    found = _stack_points(_stack_reports(root))
    assert found == STACK_TABLE
    assert len(found) == STACK_HITS
    assert len({(row[1], row[2]) for row in found}) == STACK_ROUNDS


@pytest.mark.archive
def test_the_stack_coverage_says_what_it_could_not_see() -> None:
    """66 tutkittua 93:sta; 27 vaiennettua Nukella.

    Kattavuus on kolme lukua eikä yksi: CT-kierroksia on 93, niistä stack
    näki 66, ja erotus 27 on Nuken kaksi demoa. Ilman erottelua Nuken nolla
    osumaa lukisi mitattuna negatiivisena.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    reports = _stack_reports(root)
    ct_rounds = sum(r.anomaly_scan.crunch_rounds for r in reports)
    scanned = sum(r.anomaly_scan.stack_rounds for r in reports)
    silenced = {
        demo
        for r in reports
        for demo in r.anomaly_scan.demos_without_site_groups
    }
    assert ct_rounds == STACK_CT_ROUNDS
    assert scanned == STACK_SCANNED
    assert ct_rounds - scanned == STACK_SILENCED_ROUNDS
    assert silenced == set(NUKE_DEMOS)


@pytest.mark.archive
def test_five_defenders_are_the_rules_real_extreme_not_an_empty_set() -> None:
    """``stack_min_players = 5`` antaa 2 kierrosta, ei 0.

    Kynnys on siis **aidosti luettu asetuksista** eikä kovakoodattu, ja
    viisi on säännön aito ääripää: Ancientin kaljukostaja k2 ja Anubiksen
    ryhmarama k4.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds.model_copy(
        update={"stack_min_players": 5}
    )
    rounds = {(row[1], row[2]) for row in _stack_points(_stack_reports(root, limits))}
    assert sorted(rounds) == [
        ("Ancient_vs_kaljukostaja", 2),
        ("Anubis_vs_ryhmarama", 4),
    ]


@pytest.mark.archive
@pytest.mark.parametrize(
    "required,rounds,hits", SITE_PRESENCE_TABLE, ids=lambda v: str(v)
)
def test_requiring_more_players_on_the_site_itself_halves_the_hits(
    required: int, rounds: int, hits: int
) -> None:
    """Miksi **yksi** pelaaja siten omalla alueella riittää.

    Sääntö vaatii vähintään yhden. Vaihtoehdot on mitattu, ja ne ovat tässä
    taulukkona, jotta valinta on tietoinen eikä oletusarvo: kahdella
    pelaajalla osumia on 5 ja kolmella 2. Tiukennus ei siis poistaisi kohinaa
    vaan puolet havainnoista -- ja pelaajamäärä on rivillä joka tapauksessa,
    joten lukija arvioi itse.

    Luku lasketaan **samoista osumista samasta arkistosta**, joten testi
    kaatuu jos vaihtoehdon hinta muuttuu ilman että taulukko muuttuu.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    on_site = _players_on_the_site(root, _stack_reports(root))
    kept = [entry for entry in on_site if entry[2] >= required]
    assert len(kept) == hits
    assert len({(demo, round_no) for demo, round_no, _ in kept}) == rounds


def _players_on_the_site(root: Path, reports) -> list[tuple[str, int, int]]:
    """``(demo, kierros, montako sitellä)`` jokaiselle stack-osumalle.

    Sääntö vaatii vähintään yhden pelaajan siten **omalla** alueella; tämä
    laskee, montako heitä oikeasti oli. Subjektin rivit tunnistetaan
    kokoonpanotunnisteista, kuten aggregointi ne tunnistaa -- sama demo on
    arkistossa kahdesti, ja hakemistonimestä luettuna puolet riveistä olisi
    vastustajan.
    """
    cache: dict[str, pl.DataFrame] = {}
    found: list[tuple[str, int, int]] = []
    for report in reports:
        lineups = list(report.team.lineup_keys)
        for anomaly in report.anomalies:
            if anomaly.rule != "stack":
                continue
            site = SITE_AREAS[anomaly.site]
            for entry in anomaly.rounds:
                demo = entry.map_demo_id
                if demo not in cache:
                    cache[demo] = pl.read_parquet(
                        root / "parsed" / demo / "ticks.parquet"
                    )
                rows = cache[demo].filter(
                    pl.col("lineup_key").is_in(lineups)
                    & (pl.col("round_no") == entry.round_no)
                    & (pl.col("side") == "CT")
                    & (pl.col("sample_kind") == "time")
                    & pl.col("is_alive").fill_null(False)
                    & (pl.col("area") == site)
                )
                for point in entry.points:
                    at_point = rows.filter(
                        pl.col("sample_t_s") == point.sample_t_s
                    )
                    found.append(
                        (demo, entry.round_no, at_point["player_id"].n_unique())
                    )
    return found
