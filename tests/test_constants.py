"""Jaettujen vakioluetteloiden testit.

``constants.py`` maarittelee jokaisen enumin kahdesti: ajonaikaisena tuplena
(jota Polars-skeemat ja asetukset kayttavat) ja tyyppivihjeena (jota pydantic ja
tyyppitarkistus kayttavat). Jos ne erkanevat, Parquetiin voi paatya arvo jota
malli ei hyvaksy -- tai painvastoin. Nama testit lukitsevat parit yhteen.
"""

from __future__ import annotations

from typing import get_args

import pytest

from pappascout import constants
from pappascout.constants import (
    SAMPLE_BUCKETS,
    SAMPLE_BUCKET_FI,
    ARMING_WEAPONS,
    EVENT_KINDS,
    GRENADES,
    KNOWN_INVENTORY_ITEMS,
    ROSTER_CLASSES,
    ROUND_TYPE_FI,
    ROUND_TYPES,
    SAMPLE_KINDS,
    SIDES,
    EventKind,
    RosterClass,
    RoundType,
    SampleKind,
    Side,
    UnitStatus,
    UNIT_STATUSES,
    weapon_classification_digest,
)

PAIRS = [
    ("SIDES", SIDES, Side),
    ("ROUND_TYPES", ROUND_TYPES, RoundType),
    ("UNIT_STATUSES", UNIT_STATUSES, UnitStatus),
    ("SAMPLE_KINDS", SAMPLE_KINDS, SampleKind),
    ("EVENT_KINDS", EVENT_KINDS, EventKind),
    ("ROSTER_CLASSES", ROSTER_CLASSES, RosterClass),
]


@pytest.mark.parametrize("name,values,literal_type", PAIRS, ids=[p[0] for p in PAIRS])
def test_literal_matches_runtime_tuple(name: str, values: tuple, literal_type) -> None:
    """Tyyppivihje ja ajonaikainen luettelo sisaltavat samat arvot."""
    assert set(get_args(literal_type)) == set(values), name


@pytest.mark.parametrize("name,values,literal_type", PAIRS, ids=[p[0] for p in PAIRS])
def test_values_are_unique(name: str, values: tuple, literal_type) -> None:
    assert len(set(values)) == len(values), name


def test_finnish_labels_cover_every_round_type() -> None:
    """Raporttimalli kaantaa jokaisen kierrostyypin -- ei puuttuvia otsikoita."""
    assert set(ROUND_TYPE_FI) == set(ROUND_TYPES)


def test_full_is_shown_as_default_in_reports() -> None:
    """Spinen konventio: full esitetaan raportissa nimella 'default'."""
    assert ROUND_TYPE_FI["full"] == "default"


def test_unit_statuses_match_the_error_policy() -> None:
    """AD-9:n tilajoukko sellaisenaan."""
    assert set(UNIT_STATUSES) == {
        "ok",
        "no_demo",
        "download_failed",
        "parse_failed",
        "no_freeze_end",
        "pruned",
    }


# --- Aseluokittelu (Story 1.6) ------------------------------------------------


#: Luokittelun koko. Luku esiintyy koodin kommenteissa ja READMEssa, joten se
#: lukitaan tässä: kolmeen paikkaan kirjoitettu luku vanhenee muuten hiljaa.
KNOWN_ITEM_COUNT = 57
ARMING_WEAPON_COUNT = 31


def test_classification_sizes_are_locked() -> None:
    """Luokittelun koko on se, jonka dokumentaatio lupaa.

    ``constants.py``, ``README.md`` ja tämän tarinan muutosloki nimeävät nämä
    luvut. Ilman lukitusta aseen lisääminen tekisi jokaisesta niistä väärän
    ilman että mikään kertoo -- ja luku on juuri se, jota lukija käyttää
    arvioidessaan kattaako luettelo pelin.

    Jos lisäät aseen: päivitä tämä luku **ja** ne kolme paikkaa. Tiiviste
    muuttuu joka tapauksessa, joten arkisto parsitaan uudelleen.
    """
    assert len(KNOWN_INVENTORY_ITEMS) == KNOWN_ITEM_COUNT
    assert len(ARMING_WEAPONS) == ARMING_WEAPON_COUNT


def test_categories_do_not_overlap() -> None:
    """Nimi kuuluu tasan yhteen luokkaan.

    Päällekkäisyys tarkoittaisi, että sama nimi on sekä veitsi että ase -- ja
    koska aseistus katsoo vain :data:`ARMING_WEAPONS`ia, veitsi aseistaisi
    pelaajan. Se ei näkyisi missään muualla kuin lopputuloksessa.
    """
    for index, (name_a, _, a) in enumerate(constants._CLASSIFICATION):
        for name_b, _, b in constants._CLASSIFICATION[index + 1 :]:
            assert not (a & b), f"{name_a} ja {name_b}: {sorted(a & b)}"


def test_arming_weapons_and_known_items_come_from_the_classification() -> None:
    """Molemmat julkiset joukot johdetaan **samasta** luettelosta.

    Tämä on rakenteen toteamista, ei laskutoimituksen: aiemmin unioni
    kirjoitettiin erikseen, ja silloin uuden aseluokan pystyi lisäämään
    ``ARMING_WEAPONS``iin koskematta luetteloon -- nimi aseisti pelaajat,
    laskettiin tunnetuksi ja **tiiviste pysyi samana**, eli arkisto jäi
    hiljaa vanhentuneeksi koko testisarjan pysyessä vihreänä. Kun molemmat
    johdetaan luettelosta, sitä reittiä ei ole.
    """
    arming = {
        name for _, arms, names in constants._CLASSIFICATION if arms for name in names
    }
    known = {name for _, _, names in constants._CLASSIFICATION for name in names}
    assert ARMING_WEAPONS == arming
    assert KNOWN_INVENTORY_ITEMS == known
    # Aseistava nimi on aina myös tunnettu -- muuten se raportoitaisiin
    # tuntemattomana ja aseistaisi silti.
    assert ARMING_WEAPONS <= KNOWN_INVENTORY_ITEMS


def test_every_public_set_is_named_in_the_classification() -> None:
    """Jokainen moduulin julkinen esinejoukko on luettelossa.

    Ilman tätä uuden joukon voisi määritellä ja unohtaa lisätä luetteloon.
    Silloin sen nimet olisivat tuntemattomia, mutta mikään ei kertoisi
    kummassa päässä vika on.
    """
    listed = {id(names) for _, _, names in constants._CLASSIFICATION}
    for name in (
        "KNIVES",
        "DEFAULT_PISTOLS",
        "PURCHASED_PISTOLS",
        "SMGS",
        "RIFLES",
        "SHOTGUNS",
        "GRENADES",
        "OTHER_ITEMS",
    ):
        assert id(getattr(constants, name)) in listed, name


def test_arming_weapons_excludes_what_the_user_excluded() -> None:
    """Käyttäjän määritelmä rajaa ulos oletuspistoolit, veitset ja utilityn.

    Nämä ovat ne neljä rajausta, jotka Veeti nimesi: ilmaisen oletuspistoolin
    hallussapito ei kerro mitään, veitsi ei ole ase, kranaatti ei ole ase,
    eikä Zeus korvaa asetta. C4 on tehtäväesine.
    """
    for name in ("Glock-18", "USP-S", "P2000"):
        assert name not in ARMING_WEAPONS
    for name in ("knife", "knife_t", "Bayonet", "Kukri Knife"):
        assert name not in ARMING_WEAPONS
    for name in GRENADES:
        assert name not in ARMING_WEAPONS
    for name in ("Zeus x27", "C4 Explosive"):
        assert name not in ARMING_WEAPONS


def test_arming_weapons_covers_every_class_the_user_named() -> None:
    """"Parempi pistooli, SMG tai halpa kivääri" -- kaikki kolme luokkaa.

    Yksikin puuttuva luokka tarkoittaisi kokonaisen ostotyypin putoamista
    laskurista, ja se näkyisi vain jakauman hienoisena vinoutumana.
    """
    for name in ("P250", "Tec-9", "Desert Eagle"):  # paremmat pistoolit
        assert name in ARMING_WEAPONS
    for name in ("MAC-10", "MP9", "MP7"):  # SMG:t
        assert name in ARMING_WEAPONS
    for name in ("Galil AR", "FAMAS", "AK-47", "AWP"):  # kiväärit
        assert name in ARMING_WEAPONS
    for name in ("Nova", "MAG-7", "XM1014"):  # haulikot
        assert name in ARMING_WEAPONS


def test_digest_ignores_the_order_of_names_and_classes(monkeypatch) -> None:
    """Sama luokittelu eri järjestyksessä antaa saman tiivisteen.

    Joukko on järjestämätön ja luokkalista on käsin kirjoitettu, joten ilman
    lajittelua tiiviste voisi vaihtua ilman että luokittelu muuttuu -- ja koko
    arkisto parsittaisiin uudelleen ilman syytä. Aiempi versio tästä testistä
    vertasi funktiota itseensä eikä siis mitannut järjestystä lainkaan.
    """
    before = weapon_classification_digest()

    shuffled = tuple(
        (label, arms, frozenset(sorted(names, reverse=True)))
        for label, arms, names in reversed(constants._CLASSIFICATION)
    )
    monkeypatch.setattr(constants, "_CLASSIFICATION", shuffled)

    assert weapon_classification_digest() == before
    assert len(before) == 64


def test_digest_changes_when_a_name_moves_between_classes(monkeypatch) -> None:
    """Tiiviste huomaa myös siirron luokasta toiseen, ei vain lisäyksen.

    Jos ase siirretään veitsiin, tunnettujen nimien unioni ei muutu lainkaan
    -- mutta luokittelu muuttuu ja laskuri sen mukana. Siksi tiiviste
    lasketaan nimetyistä luokista eikä uniosta.
    """
    before = weapon_classification_digest()
    moved = tuple(
        (label, arms, names - {"Nova"})
        if label == "shotguns"
        else (label, arms, names | {"Nova"})
        if label == "knives"
        else (label, arms, names)
        for label, arms, names in constants._CLASSIFICATION
    )
    monkeypatch.setattr(constants, "_CLASSIFICATION", moved)
    assert weapon_classification_digest() != before


def test_digest_changes_when_a_class_is_added(monkeypatch) -> None:
    """Uusi aseluokka mitätöi arkiston, vaikka mikään vanha nimi ei muuttuisi.

    Tämä on se tapaus, jonka katselmoija mursi: uusi joukko lisättiin
    aseistaviin, uusi nimi aseisti pelaajat -- ja tiiviste pysyi täsmälleen
    samana. Nyt luettelo on ainoa lähde, joten sama muutos näkyy tiivisteessä.
    """
    before = weapon_classification_digest()
    monkeypatch.setattr(
        constants,
        "_CLASSIFICATION",
        (*constants._CLASSIFICATION, ("machineguns", True, frozenset({"M60"}))),
    )
    assert weapon_classification_digest() != before


def test_digest_changes_when_a_class_stops_arming(monkeypatch) -> None:
    """Aseistavuuden vaihtaminen muuttaa tiivisteen, vaikka nimet säilyvät.

    Haulikoiden pudottaminen pois aseistavista on luokittelun muutos siinä
    missä nimen poistokin: ilman aseistavuutta tiivisteessä arkisto jäisi
    voimaan vanhalla säännöllä.
    """
    before = weapon_classification_digest()
    disarmed = tuple(
        (label, False if label == "shotguns" else arms, names)
        for label, arms, names in constants._CLASSIFICATION
    )
    monkeypatch.setattr(constants, "_CLASSIFICATION", disarmed)
    assert weapon_classification_digest() != before


def test_every_sample_bucket_has_a_finnish_name() -> None:
    """Kolmas lokero ei saa jäädä pois tulosteesta suomennoksen puuttuessa.

    ``cli`` iteroi :data:`SAMPLE_BUCKETS`in yli ja hakee nimen
    :data:`SAMPLE_BUCKET_FI`:stä, joten puuttuva avain kaataisi ajon --
    ja kaksi samaa suomennosta sulauttaisi kaksi lokeroa yhdeksi riviksi.
    """
    assert set(SAMPLE_BUCKET_FI) == set(SAMPLE_BUCKETS)
    assert len(set(SAMPLE_BUCKET_FI.values())) == len(SAMPLE_BUCKETS)
    assert SAMPLE_BUCKETS == ("league", "other", "unknown")
