"""Tests for the shared enumerations.

``constants.py`` defines every enum twice: as a runtime tuple (which the
Polars schemas and the settings use) and as a type hint (which pydantic and
the type check use). If they diverge, a value the model does not accept can
end up in Parquet -- or the other way round. These tests lock the pairs
together.
"""

from __future__ import annotations

from typing import get_args

import pytest

from pappascout import constants
from pappascout.domain.selection import class_labels
from pappascout.constants import (
    ANOMALY_RULE_FI,
    ANOMALY_RULES,
    SAMPLE_BUCKETS,
    SAMPLE_BUCKET_FI,
    SAVING_ROUND_TYPES,
    AREA_SOURCES,
    ARMING_WEAPONS,
    AnomalyRule,
    AreaSource,
    EVENT_KINDS,
    GRENADES,
    KNOWN_INVENTORY_ITEMS,
    ROSTER_BUCKETS,
    ROSTER_BUCKET_FI,
    ROSTER_CLASS_BUCKET,
    ROSTER_CLASSES,
    RosterBucketName,
    ROUND_TYPE_FI,
    ROUND_TYPES,
    SAMPLE_KINDS,
    SIDES,
    EventKind,
    RosterClass,
    SITE_GROUPS,
    SiteGroup,
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
    ("ANOMALY_RULES", ANOMALY_RULES, AnomalyRule),
    ("UNIT_STATUSES", UNIT_STATUSES, UnitStatus),
    ("SAMPLE_KINDS", SAMPLE_KINDS, SampleKind),
    ("EVENT_KINDS", EVENT_KINDS, EventKind),
    ("AREA_SOURCES", AREA_SOURCES, AreaSource),
    ("ROSTER_CLASSES", ROSTER_CLASSES, RosterClass),
    ("SITE_GROUPS", SITE_GROUPS, SiteGroup),
]


@pytest.mark.parametrize("name,values,literal_type", PAIRS, ids=[p[0] for p in PAIRS])
def test_literal_matches_runtime_tuple(name: str, values: tuple, literal_type) -> None:
    """The type hint and the runtime list hold the same values."""
    assert set(get_args(literal_type)) == set(values), name


@pytest.mark.parametrize("name,values,literal_type", PAIRS, ids=[p[0] for p in PAIRS])
def test_values_are_unique(name: str, values: tuple, literal_type) -> None:
    assert len(set(values)) == len(values), name


def test_finnish_labels_cover_every_round_type() -> None:
    """The report model translates every round type -- no missing headings."""
    assert set(ROUND_TYPE_FI) == set(ROUND_TYPES)


def test_full_is_shown_as_default_in_reports() -> None:
    """The spine's convention: full is shown in the report as 'default'."""
    assert ROUND_TYPE_FI["full"] == "default"


def test_finnish_labels_cover_every_anomaly_rule() -> None:
    """A new rule cannot appear in the report without a name.

    The claim is **only here**: ``render.view`` indexes the map directly
    (``ANOMALY_RULE_FI[rule]``), so a missing name stops the rendering instead
    of degrading quietly into the rule name. This test is the one that keeps
    the map full.
    """
    assert set(ANOMALY_RULE_FI) == set(ANOMALY_RULES)


def test_the_site_groups_are_derived_from_the_site_areas() -> None:
    """The group ids are derived and not a second copy.

    Two hand-written lists could differ from each other, and the difference
    would show only when the rule rejected its own result as an unknown
    group.
    """
    from pappascout.constants import SITE_AREAS

    assert SITE_GROUPS == tuple(SITE_AREAS)
    assert set(SITE_AREAS.values()) == {"BombsiteA", "BombsiteB"}


def test_the_deferred_rules_are_not_among_the_implemented_ones() -> None:
    """The denominator of the coverage: one rule cannot be in both.

    Story 2.14 moved ``stack`` from one list to the other, and the deferred
    list is now empty. **The claim is still needed in both directions**: if a
    rule were in both for a moment, the report would claim to have run a rule
    it names as unrun. The empty list stays for the next deferred rule -- it
    is the denominator of the coverage and not a leftover of the move.
    """
    assert not set(ANOMALY_RULES) & set(constants.ANOMALY_RULES_DEFERRED)
    assert constants.ANOMALY_RULES_DEFERRED == ()
    assert "stack" in ANOMALY_RULES


def test_saving_round_types_are_a_proper_subset_of_the_round_types() -> None:
    """A saving round is a subset of the round types, not a list of its own.

    Without this guard a typo (``"ecos"``) would silence the CT-advance rule
    completely: the round type would never match, and nothing would say so.

    The pistol round is **outside**: both sides then have the same small
    amount of money, so an advance says nothing about a plan. The rendering's
    own "saving round" means something else (the pistol round is included),
    and each place names its own meaning where it is defined.
    """
    assert set(SAVING_ROUND_TYPES) < set(ROUND_TYPES)
    assert set(SAVING_ROUND_TYPES) == {"eco", "half", "force"}
    assert "pistol" not in SAVING_ROUND_TYPES


def test_unit_statuses_match_the_error_policy() -> None:
    """AD-9's set of statuses as it stands."""
    assert set(UNIT_STATUSES) == {
        "ok",
        "no_demo",
        "download_failed",
        "parse_failed",
        "no_freeze_end",
        "pruned",
    }


# --- Weapon classification (Story 1.6) ----------------------------------------


#: The size of the classification. The number appears in the code's
#: comments, so it is locked here: a number written in two places otherwise
#: goes quietly stale.
KNOWN_ITEM_COUNT = 57
ARMING_WEAPON_COUNT = 31


def test_classification_sizes_are_locked() -> None:
    """The size of the classification is the one the documentation promises.

    ``constants.py`` and this story's change log name these numbers. Without
    the lock, adding a weapon would make every one of them wrong without
    anything saying so -- and the number is exactly what a reader uses to
    judge whether the list covers the game.

    If you add a weapon: update this number **and** those three places. The
    digest changes in any case, so the archive is parsed again.
    """
    assert len(KNOWN_INVENTORY_ITEMS) == KNOWN_ITEM_COUNT
    assert len(ARMING_WEAPONS) == ARMING_WEAPON_COUNT


def test_categories_do_not_overlap() -> None:
    """A name belongs to exactly one class.

    An overlap would mean that the same name is both a knife and a weapon --
    and because the arming looks only at :data:`ARMING_WEAPONS`, the knife
    would arm the player. It would show nowhere except in the end result.
    """
    for index, (name_a, _, a) in enumerate(constants._CLASSIFICATION):
        for name_b, _, b in constants._CLASSIFICATION[index + 1 :]:
            assert not (a & b), f"{name_a} and {name_b}: {sorted(a & b)}"


def test_arming_weapons_and_known_items_come_from_the_classification() -> None:
    """Both public sets are derived from the **same** list.

    This states the structure, not a calculation: the union used to be written
    out separately, and then a new weapon class could be added to
    ``ARMING_WEAPONS`` without touching the list -- the name armed players,
    was counted as known and **the digest stayed the same**, that is, the
    archive went quietly stale while the whole suite stayed green. When both
    are derived from the list, that route does not exist.
    """
    arming = {
        name for _, arms, names in constants._CLASSIFICATION if arms for name in names
    }
    known = {name for _, _, names in constants._CLASSIFICATION for name in names}
    assert ARMING_WEAPONS == arming
    assert KNOWN_INVENTORY_ITEMS == known
    # An arming name is always known as well -- otherwise it would be
    # reported as unknown and would arm all the same.
    assert ARMING_WEAPONS <= KNOWN_INVENTORY_ITEMS


def test_every_public_set_is_named_in_the_classification() -> None:
    """Every public item set of the module is in the list.

    Without this, a new set could be defined and forgotten from the list. Its
    names would then be unknown, but nothing would say which end the fault is
    at.
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
    """The user's definition excludes default pistols, knives and utility.

    These are the four exclusions the product owner named: possessing a free
    default pistol says nothing, a knife is not a weapon, a grenade is not a
    weapon, and a Zeus does not replace a weapon. The C4 is a mission item.
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
    """"A better pistol, an SMG or a cheap rifle" -- all three classes.

    A single missing class would mean a whole buy type dropping out of the
    counter, and it would show only as a slight skew in the distribution.
    """
    for name in ("P250", "Tec-9", "Desert Eagle"):  # the better pistols
        assert name in ARMING_WEAPONS
    for name in ("MAC-10", "MP9", "MP7"):  # the SMGs
        assert name in ARMING_WEAPONS
    for name in ("Galil AR", "FAMAS", "AK-47", "AWP"):  # the rifles
        assert name in ARMING_WEAPONS
    for name in ("Nova", "MAG-7", "XM1014"):  # the shotguns
        assert name in ARMING_WEAPONS


def test_digest_ignores_the_order_of_names_and_classes(monkeypatch) -> None:
    """The same classification in another order gives the same digest.

    A set is unordered and the class list is hand-written, so without the
    sorting the digest could change without the classification changing -- and
    the whole archive would be parsed again for no reason. An earlier version
    of this test compared the function with itself and so did not measure the
    order at all.
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
    """The digest notices a move between classes too, not only an addition.

    If a weapon is moved into the knives, the union of the known names does
    not change at all -- but the classification changes and the counter with
    it. That is why the digest is computed from the named classes and not from
    the union.
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
    """A new weapon class invalidates the archive, even if no old name changes.

    This is the case the reviewer broke: a new set was added to the arming
    ones, a new name armed players -- and the digest stayed exactly the same.
    Now the list is the only source, so the same change shows in the digest.
    """
    before = weapon_classification_digest()
    monkeypatch.setattr(
        constants,
        "_CLASSIFICATION",
        (*constants._CLASSIFICATION, ("machineguns", True, frozenset({"M60"}))),
    )
    assert weapon_classification_digest() != before


def test_digest_changes_when_a_class_stops_arming(monkeypatch) -> None:
    """Changing whether a class arms changes the digest, though the names stay.

    Dropping the shotguns out of the arming ones is a change to the
    classification just as much as removing a name is: without
    whether-it-arms in the digest, the archive would stay valid under the old
    rule.
    """
    before = weapon_classification_digest()
    disarmed = tuple(
        (label, False if label == "shotguns" else arms, names)
        for label, arms, names in constants._CLASSIFICATION
    )
    monkeypatch.setattr(constants, "_CLASSIFICATION", disarmed)
    assert weapon_classification_digest() != before


def test_every_sample_bucket_has_a_finnish_name() -> None:
    """The third bucket must not drop out of the output for want of a Finnish
    name.

    ``cli`` iterates over :data:`SAMPLE_BUCKETS` and looks the name up in
    :data:`SAMPLE_BUCKET_FI`, so a missing key would stop the run -- and two
    identical Finnish names would merge two buckets into one row.
    """
    assert set(SAMPLE_BUCKET_FI) == set(SAMPLE_BUCKETS)
    assert len(set(SAMPLE_BUCKET_FI.values())) == len(SAMPLE_BUCKETS)
    assert SAMPLE_BUCKETS == ("league", "other", "unknown")


def test_every_roster_bucket_has_a_printed_name() -> None:
    """Same rule as the league buckets, and for the same reason.

    ``render`` iterates :data:`ROSTER_BUCKETS` and looks the name up in
    :data:`ROSTER_BUCKET_FI`, so a missing key would stop the run -- and two
    identical names would merge two buckets into one row.
    """
    assert set(ROSTER_BUCKET_FI) == set(ROSTER_BUCKETS)
    assert len(set(ROSTER_BUCKET_FI.values())) == len(ROSTER_BUCKETS)
    assert ROSTER_BUCKETS == ("full", "partial", "unknown")


def test_the_roster_buckets_follow_the_order_class_labels_returns() -> None:
    """The pairing is bound to ``class_labels()``, not to an index.

    ``class_labels`` is the only place that decides which class name means a
    full roster and which means one outsider; it returns them as
    ``(full, partial)``. Comparing ``ROSTER_BUCKET_FI["full"]`` against
    ``ROSTER_CLASSES[0]`` would pass just as happily with the pair reversed,
    and a reversed pair turns every roster number in the report upside down
    without failing anything. So the assertion asks the function.
    """
    full_label, partial_label = class_labels(5, 4)
    assert ROSTER_CLASS_BUCKET[full_label] == "full"
    assert ROSTER_CLASS_BUCKET[partial_label] == "partial"
    assert ROSTER_BUCKET_FI["full"] == full_label
    assert ROSTER_BUCKET_FI["partial"] == partial_label


def test_the_roster_buckets_are_derived_from_the_classes() -> None:
    """The bucket names carry the class names, not a hand-written copy.

    A third class would break the ``zip(..., strict=True)`` at import time
    rather than fall silently out of the report, and that is the point of
    building the pairing instead of writing it twice.
    """
    assert set(ROSTER_CLASS_BUCKET) == set(ROSTER_CLASSES)
    assert len(ROSTER_CLASS_BUCKET) == len(ROSTER_CLASSES)
    # The third bucket is the same honest word the league breakdown uses; two
    # spellings of "unknown" would read as two different states.
    assert ROSTER_BUCKET_FI["unknown"] == SAMPLE_BUCKET_FI["unknown"]


def test_the_roster_bucket_names_are_the_bucket_list() -> None:
    """The Literal and the tuple are two spellings of one set.

    A mismatch between them would surface as an ``AttributeError`` during
    validation rather than at import, which is exactly the silent divergence
    the module goes out of its way to avoid elsewhere.
    """
    assert get_args(RosterBucketName) == ROSTER_BUCKETS


def test_the_nearest_player_method_is_gone_from_the_area_sources() -> None:
    """``snapped`` went away in Story 2.9, because the method went away.

    It meant "the area of the nearest living player", and it measured
    structurally the opposite of what it should have: smoke is thrown where
    nobody is. The value was not left in the list as a fallback source -- two
    parallel methods would make the row uninterpretable, because the reader
    would not see which of them named it.

    An old ``events.parquet`` therefore does not load into this enum, and that
    is intended: ``parse`` runs the recording again without the ``--force``
    flag.
    """
    assert AREA_SOURCES == ("observed", "point_cloud")
    assert "snapped" not in AREA_SOURCES
