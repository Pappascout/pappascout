"""Jaetut vakioluettelot.

Nämä enum-arvot esiintyvät samanlaisina koodissa, Parquet-tauluissa,
asetuksissa ja raportissa (spinen konventiotaulukko). Moduuli on tarkoituksella
riippumaton kaikesta muusta, jotta sekä ``domain`` että ``archive`` voivat tuoda
sen rikkomatta kerrossääntöä (``archive`` ei saa riippua ``domain``ista).
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

__all__ = [
    "SIDES",
    "Side",
    "ROUND_TYPES",
    "RoundType",
    "UNIT_STATUSES",
    "UnitStatus",
    "SAMPLE_KINDS",
    "SampleKind",
    "EVENT_KINDS",
    "EventKind",
    "AREA_SOURCES",
    "AreaSource",
    "ROSTER_CLASSES",
    "RosterClass",
    "ROUND_TYPE_FI",
    "UNCLASSIFIED",
    "KNIVES",
    "DEFAULT_PISTOLS",
    "PURCHASED_PISTOLS",
    "SMGS",
    "RIFLES",
    "SHOTGUNS",
    "ARMING_WEAPONS",
    "GRENADES",
    "OTHER_ITEMS",
    "KNOWN_INVENTORY_ITEMS",
    "weapon_classification_digest",
]

#: Rivin joukkueen puoli.
SIDES: Final[tuple[str, ...]] = ("T", "CT")
Side = Literal["T", "CT"]

#: Kierrostyyppi (AD-4). Sama arvo koodissa, Parquetissa, asetuksissa ja raportissa.
ROUND_TYPES: Final[tuple[str, ...]] = (
    "pistol",
    "eco",
    "half",
    "force",
    "full",
    "ot",
    "anomaly",
)
RoundType = Literal["pistol", "eco", "half", "force", "full", "ot", "anomaly"]

#: Kierros, jota ei voitu luokitella lainkaan (havainto puuttuu). Ei ole
#: kierrostyyppi vaan sen puuttuminen: taulussa ``round_type`` on ``null``,
#: ja tämä on sen ainoa näkyvä nimi tulosteissa ja luvuissa.
UNCLASSIFIED: Final[str] = "luokittelematon"

#: Raporttimallin suomennokset. Vain otsikoissa – dataan ei kirjoiteta suomea.
ROUND_TYPE_FI: Final[dict[str, str]] = {
    "pistol": "pistooli",
    "eco": "eco",
    "half": "puoliosto",
    "force": "force",
    "full": "default",
    "ot": "jatkoaika",
    "anomaly": "poikkeama",
}

#: Yksikön (Match / MapDemo) käsittelytila (AD-9).
UNIT_STATUSES: Final[tuple[str, ...]] = (
    "ok",
    "no_demo",
    "download_failed",
    "parse_failed",
    "no_freeze_end",
    "pruned",
)
UnitStatus = Literal[
    "ok", "no_demo", "download_failed", "parse_failed", "no_freeze_end", "pruned"
]

#: Näytepisteen laji (AD-5).
SAMPLE_KINDS: Final[tuple[str, ...]] = ("time", "first_contact")
SampleKind = Literal["time", "first_contact"]

#: Utility-tapahtuman laji (AD-5). Tarkat demoparser2-nimet lukitaan Story 1.2:ssa.
EVENT_KINDS: Final[tuple[str, ...]] = ("grenade_thrown", "grenade_detonate")
EventKind = Literal["grenade_thrown", "grenade_detonate"]

#: Mistä utility-tapahtuman alue on peräisin (AD-5).
#:
#: ``observed``
#:     Heittäjän oma ``m_szLastPlaceName`` samalta tickiltä. Heittorivillä alue
#:     on siis havainto, ei arvio.
#: ``snapped``
#:     Lähimmän elossa olevan pelaajan alue etäisyysrajan sisältä. Räjähdyksellä
#:     ei ole omaa aluenimeä, joten se on aina approksimaatio.
#:
#: ``null`` tarkoittaa, ettei aluetta saatu lainkaan. Ilman tätä saraketta
#: raportti ei voisi erottaa varmaa tietoa arviosta.
AREA_SOURCES: Final[tuple[str, ...]] = ("observed", "snapped")
AreaSource = Literal["observed", "snapped"]

#: Rosterikynnyksen luokka per MapDemo (AD-6).
ROSTER_CLASSES: Final[tuple[str, ...]] = ("5/5", "4/5")
RosterClass = Literal["5/5", "4/5"]


# -- Aseluokittelu (Story 1.6) -------------------------------------------------
#
# Nämä nimet ovat demoparser2:n ``inventory``-listan arvoja, luettu
# ostoajan lopun tickiltä. Nimet on **mitattu kuudesta demosta** (Ancient,
# Nuke ja neljä Pappaliiga-demoa; Ancientin .dem ja .dem.zst ovat sama ottelu),
# ei arvattu: 48 eri nimeä. Yhdeksän muuta pelin asetta on luettelossa, vaikka
# niitä ei näissä demoissa esiintynyt -- ne ovat pelissä olemassa, ja
# puuttuvasta aseesta seuraisi hiljaa liian pieni laskuri.
#
# LUOKITTELU ON SALLITTUJEN ASEIDEN LUETTELO, EI KIELLETTYJEN. Veitset ovat
# avoin joukko -- kuudessa demossa on jo 15 eri skininimeä ja Valve lisää niitä
# jokaisessa kauppapäivityksessä. Aseita on 31 ja uusi ase on harvinainen
# tapaus. Jos tuntematon nimi laskettaisiin aseeksi, jokainen uusi veitsiskini
# aseistaisi pelaajan väärin ja tekisi sen hiljaa. Sallittujen luettelo
# vanhenee näkyvästi ja väärään suuntaan: uusi ase jää laskematta, mikä on
# turvallisempi virhe kuin veitsi joka aseistaa.
#
# HALLUSSAPITO, EI OSTOS. Luettelo kertoo, mikä *ase* aseistaa pelaajan, ei
# sitä ostiko hän sen. Tavaraluettelo luetaan ostoajan lopusta, joten
# edelliseltä kierrokselta säästetty tai vainajalta poimittu kivääri laskeutuu
# samoin kuin juuri ostettu. Se on tarkoitus eikä puute: kierroksen kannalta
# ratkaisee mitä kädessä on, ei mistä se tuli, ja säästetty AK on tismalleen
# yhtä vaarallinen kuin ostettu. Ainoa poikkeus ovat oletuspistoolit, jotka
# rajataan ulos siksi, että ne saa joka kierros ilmaiseksi -- niiden
# hallussapito ei kerro yhtään mitään.

#: Veitset -- **avoin joukko**, tässä on vain se, mitä aineistossa on nähty.
#: Luettelo ei ole eikä yritä olla täydellinen: sen ainoa tehtävä on hiljentää
#: tuntemattomien nimien raportti niistä, jotka on jo tunnistettu. Puuttuva
#: veitsi ei aseista ketään, koska :data:`ARMING_WEAPONS` ratkaisee sen.
KNIVES: Final[frozenset[str]] = frozenset(
    {
        "Bayonet",
        "Bowie Knife",
        "Butterfly Knife",
        "Falchion Knife",
        "Gut Knife",
        "Huntsman Knife",
        "Kukri Knife",
        "M9 Bayonet",
        "Paracord Knife",
        "Shadow Daggers",
        "Skeleton Knife",
        "Stiletto Knife",
        "Talon Knife",
        "knife",
        "knife_t",
    }
)

#: Oletuspistoolit: pelaaja saa ne joka kierros ilmaiseksi, joten niiden
#: hallussapito ei kerro yhtään mitään. Varustearvo laskee ne silti mukaan
#: 200 $:n arvoisina -- juuri siksi varustearvo ei kelvannut mittariksi.
DEFAULT_PISTOLS: Final[frozenset[str]] = frozenset({"Glock-18", "P2000", "USP-S"})

#: Pistoolit, jotka on hankittava erikseen -- oletuspistooli tulee ilmaiseksi.
PURCHASED_PISTOLS: Final[frozenset[str]] = frozenset(
    {
        "CZ75-Auto",
        "Desert Eagle",
        "Dual Berettas",
        "Five-SeveN",
        "P250",
        "R8 Revolver",
        "Tec-9",
    }
)

#: Konepistoolit.
SMGS: Final[frozenset[str]] = frozenset(
    {
        "MAC-10",
        "MP5-SD",
        "MP7",
        "MP9",
        "P90",
        "PP-Bizon",
        "UMP-45",
    }
)

#: Kiväärit, tarkkuuskiväärit ja konekiväärit.
RIFLES: Final[frozenset[str]] = frozenset(
    {
        "AK-47",
        "AUG",
        "AWP",
        "FAMAS",
        "G3SG1",
        "Galil AR",
        "M4A1-S",
        "M4A4",
        "M249",
        "Negev",
        "SCAR-20",
        "SG 553",
        "SSG 08",
    }
)

#: Haulikot.
SHOTGUNS: Final[frozenset[str]] = frozenset(
    {"MAG-7", "Nova", "Sawed-Off", "XM1014"}
)

#: Kranaatit. Eivät aseista: käyttäjän määritelmä on "kevlar ja jokin
#: parannettu ase", eikä valo ole ase.
GRENADES: Final[frozenset[str]] = frozenset(
    {
        "Decoy Grenade",
        "Flashbang",
        "High Explosive Grenade",
        "Incendiary Grenade",
        "Molotov",
        "Smoke Grenade",
    }
)

#: Tavarat, jotka eivät ole aseita eivätkä kranaatteja.
#:
#: C4 on tehtäväesine, jonka T saa ilmaiseksi. Zeus on lähietäisyyden
#: kertalaukaus, jolla ei pelata kierrosta -- se latautuu CS2:ssa kyllä
#: uudelleen, mutta se ei tee siitä asetta käyttäjän määritelmässä "parempi
#: pistooli, SMG tai halpa kivääri". Kumpikaan ei siis aseista.
OTHER_ITEMS: Final[frozenset[str]] = frozenset({"C4 Explosive", "Zeus x27"})

#: Luokat, joista koko luokittelu johdetaan: ``(nimi, aseistaako, joukko)``.
#:
#: **Tämä on ainoa paikka, johon aseluokka lisätään.** Sekä
#: :data:`ARMING_WEAPONS`, :data:`KNOWN_INVENTORY_ITEMS` että
#: :func:`weapon_classification_digest` johdetaan tästä, joten uusi luokka ei
#: voi päätyä toiseen mutta jäädä pois toisesta. Aiemmin unioni kirjoitettiin
#: erikseen, ja silloin uuden luokan pystyi lisäämään aseistamaan pelaajia
#: ilman että tiiviste muuttui -- eli arkisto jäi hiljaa vanhentuneeksi.
#:
#: Luokan **nimi ja aseistavuus** ovat osa tiivistettä, eivät vain sen sisältö:
#: jos ase siirtyy luokasta toiseen eikä yksikään nimi katoa, unioni ei
#: muuttuisi mutta luokittelu muuttuisi.
_CLASSIFICATION: Final[tuple[tuple[str, bool, frozenset[str]], ...]] = (
    ("knives", False, KNIVES),
    ("default_pistols", False, DEFAULT_PISTOLS),
    ("purchased_pistols", True, PURCHASED_PISTOLS),
    ("smgs", True, SMGS),
    ("rifles", True, RIFLES),
    ("shotguns", True, SHOTGUNS),
    ("grenades", False, GRENADES),
    ("other", False, OTHER_ITEMS),
)

#: **Aseistavat aseet** (31 kpl): pelaajan aseistaa mikä tahansa näistä, kun
#: hänellä on myös panssari. Johdettu :data:`_CLASSIFICATION`ista, ei
#: kirjoitettu erikseen. Oletuspistoolit eivät ole mukana (ne saa ilmaiseksi),
#: veitsi ei ole ase, kranaatti ei ole ase.
ARMING_WEAPONS: Final[frozenset[str]] = frozenset(
    name for _, arms, names in _CLASSIFICATION if arms for name in names
)

#: Kaikki tunnetut tavaraluettelon nimet (57 kpl). Tämän joukon **ulkopuolinen**
#: nimi on tuntematon: se ei aseista ketään, ja se raportoidaan ajon
#: yhteydessä. Hiljainen pudotus olisi yhtä paha kuin hiljainen hyväksyntä.
KNOWN_INVENTORY_ITEMS: Final[frozenset[str]] = frozenset(
    name for _, _, names in _CLASSIFICATION for name in names
)


def weapon_classification_digest() -> str:
    """Tiiviste aseluokittelun **sisällöstä**.

    Menee ``parse``-vaiheen parametrihashiin, jolloin taulun muutos mitätöi
    arkiston ja pakottaa uudelleenparsinnan. Vaihtoehto olisi ``[parse]``-asetus,
    jota nostetaan käsin taulun muuttuessa -- se toimii vain jos kukaan ei
    unohda, eikä ``settings.toml`` täyty 57 esinenimestä, joita käyttäjä ei
    koskaan säädä.

    Tiiviste kattaa jokaisen :data:`_CLASSIFICATION`-luokan nimen, sen
    aseistaako se, ja sen sisällön. Uusi luokka muuttaa siis tiivistettä
    väistämättä -- ei siksi että joku muistaa lisätä sen tänne, vaan siksi
    että sama luettelo on ainoa lähde myös :data:`ARMING_WEAPONS`ille.

    Returns:
        64 merkin heksadesimaalinen sha256-tiiviste. Sama luokittelu antaa aina
        saman tiivisteen: sekä nimet että luokat järjestetään, joten joukkojen
        sisäinen järjestys ja luokkien kirjoitusjärjestys eivät vaikuta
        tulokseen.
    """
    payload = "\n".join(
        f"{label}:{int(arms)}:{','.join(sorted(names))}"
        for label, arms, names in sorted(_CLASSIFICATION)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
