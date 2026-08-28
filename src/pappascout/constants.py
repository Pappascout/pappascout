"""Jaetut vakioluettelot.

Nämä enum-arvot esiintyvät samanlaisina koodissa, Parquet-tauluissa,
asetuksissa ja raportissa (spinen konventiotaulukko). Moduuli on tarkoituksella
riippumaton kaikesta muusta, jotta sekä ``domain`` että ``archive`` voivat tuoda
sen rikkomatta kerrossääntöä (``archive`` ei saa riippua ``domain``ista).
"""

from __future__ import annotations

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
    "ROSTER_CLASSES",
    "RosterClass",
    "ROUND_TYPE_FI",
    "UNCLASSIFIED",
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

#: Rosterikynnyksen luokka per MapDemo (AD-6).
ROSTER_CLASSES: Final[tuple[str, ...]] = ("5/5", "4/5")
RosterClass = Literal["5/5", "4/5"]
