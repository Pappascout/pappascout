"""Jaettujen vakioluetteloiden testit.

``constants.py`` maarittelee jokaisen enumin kahdesti: ajonaikaisena tuplena
(jota Polars-skeemat ja asetukset kayttavat) ja tyyppivihjeena (jota pydantic ja
tyyppitarkistus kayttavat). Jos ne erkanevat, Parquetiin voi paatya arvo jota
malli ei hyvaksy -- tai painvastoin. Nama testit lukitsevat parit yhteen.
"""

from __future__ import annotations

from typing import get_args

import pytest

from pappascout.constants import (
    EVENT_KINDS,
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
