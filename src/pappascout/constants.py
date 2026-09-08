"""Shared enumerations.

These enum values appear identically in the code, in the Parquet tables, in
the settings and in the report (the spine's convention table). The module is
deliberately independent of everything else, so that both ``domain`` and
``archive`` can import it without breaking the layering rule (``archive`` must
not depend on ``domain``).
"""

from __future__ import annotations

import hashlib
from typing import Final, Literal

__all__ = [
    "SIDES",
    "Side",
    "ROUND_TYPES",
    "RoundType",
    "SAVING_ROUND_TYPES",
    "ANOMALY_RULES",
    "ANOMALY_RULES_DEFERRED",
    "AnomalyRule",
    "ANOMALY_RULE_FI",
    "SITE_AREAS",
    "SITE_GROUPS",
    "SiteGroup",
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
    "SAMPLE_BUCKETS",
    "SampleBucketName",
    "SAMPLE_BUCKET_FI",
    "ROSTER_BUCKETS",
    "RosterBucketName",
    "ROSTER_BUCKET_FI",
    "ROSTER_CLASS_BUCKET",
    "UTILITY_BUCKET_ALL",
    "UTILITY_BUCKET_UNKNOWN",
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
    "seconds_label",
]

#: The side of the row's team.
SIDES: Final[tuple[str, ...]] = ("T", "CT")
Side = Literal["T", "CT"]

#: Round type (AD-4). The same value in the code, in Parquet, in the
#: settings and in the report.
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

#: Saving rounds: the round types on which a team **has no money for a
#: normal buy**. The list is a subset of the :data:`ROUND_TYPES` values.
#:
#: Why this is a list of its own and not a threshold: Story 2.5's CT-advance
#: rule is limited to these, because the observation is **economic** -- a poor
#: CT does not normally advance into the T side's area. The epic's wording is
#: "on an eco or a force round", and the half buy (``half``) is included
#: because one of the six calibration hits (MatureMayhem Anubis round 6) is a
#: half buy: the boundary between round types runs along buying power and not
#: along the name.
#:
#: A pistol round is **not** a saving round even though there is little money:
#: neither side has buying power then, so an advance says nothing about a plan.
#:
#: **NOTE: the word "saving round" means something else in the README.** There
#: it is a rendering rule -- the round types the report describes round by
#: round (``pistol``, ``eco``, ``force``, ``half``) as opposed to
#: ``render.view.PATTERN_ROUND_TYPES`` (``full``, ``ot``). This list is
#: **economic**: the round types on which a team cannot afford a normal buy.
#: The pistol round belongs to the former but not to this one, and that is the
#: whole difference. Two concepts under one word, so each place names its own
#: meaning.
SAVING_ROUND_TYPES: Final[tuple[str, ...]] = ("eco", "half", "force")

#: The anomaly rules (AD-10, Story 2.5 and 2.14). The same value in the code,
#: in ``report.json`` and in the report; the Finnish appears in the
#: presentation only (:data:`ANOMALY_RULE_FI`).
#:
#: **Three rules are three different questions about the same observation, and
#: not one of them contains another.** ``ct_advance`` and ``crunch`` share the
#: orientation condition ("is the subject's CT player in an area that the T
#: side holds in that recording"): crunch adds a direction requirement to it
#: **but drops the round-type restriction**, so the hit sets intersect each
#: other -- on a saving round crunch also produces an advance hit, on a full
#: buy only the crunch (measured: MatureMayhem Anubis round 10 is a full buy).
#:
#: ``stack`` does not read orientation at all. It asks whether the subject's
#: own defence has piled into **one site's group**; there is no T-side area in
#: it, no directions and no round-type restriction. So it must not be
#: described as a stricter or a looser form of the other two.
#:
#: The order is the report's order: ``render.view._anomaly_rank`` sorts by
#: ``ANOMALY_RULES.index``, so a new rule's place in the section is decided
#: **here** and not in the rendering.
ANOMALY_RULES: Final[tuple[str, ...]] = ("ct_advance", "crunch", "stack")
AnomalyRule = Literal["ct_advance", "crunch", "stack"]

#: The anomaly rules the architecture (AD-10) names but which are not
#: implemented. The list is **the denominator of the coverage**: an empty
#: anomaly section can claim a measured negative only about the rules that
#: were run, and the reader needs to know how many of the spine's rules were
#: left unrun.
#:
#: **Empty since Story 2.14.** ``stack`` was here because the rule required a
#: mapping area -> area group, which did not exist: four defenders in the same
#: ``env_cs_place`` area gave 0 hits out of 93 rounds, because the game splits
#: a site across several areas (Ancient's B is ``Alley`` + ``BombsiteB`` +
#: ``SideEntrance``). The group is now derived from the recording's own point
#: cloud (``domain.sampling.site_groups``), so the name moved to
#: :data:`ANOMALY_RULES` and the coverage text corrected itself.
#:
#: The list **stays even while it is empty**: it is the denominator of the
#: coverage, and the next rule the architecture names but does not implement
#: belongs here.
ANOMALY_RULES_DEFERRED: Final[tuple[str, ...]] = ()

#: The Finnish names of the anomaly rules. Headings only, as in
#: :data:`ROUND_TYPE_FI` -- no Finnish is written into the data.
#:
#: ``crunch`` is **the product owner's own term** and not a word to translate
#: ("lobby crunch on nukessa taktiikka, jossa..."), so it stays as it is --
#: like the callouts.
#: Capitalised all the same, so that it does not look half-finished next to
#: ``CT-eteneminen``; the report's reading guide says what it means.
#: ``stack`` is likewise the players' own word -- they say it inside Finnish
#: sentences ("4-5 pelaajan stack" on one site) -- and Finnish has no
#: equivalent that would mean the same: "kasauma" would be a translation
#: nobody says out loud.
ANOMALY_RULE_FI: Final[dict[str, str]] = {
    "ct_advance": "CT-eteneminen",
    "crunch": "Crunch",
    "stack": "Stack",
}

#: A site's group -> that site's own area under the game's own name.
#:
#: **These two names are constants of the game and not an area division.**
#: Every ``de_`` map has the ``env_cs_place`` areas ``BombsiteA`` and
#: ``BombsiteB``, exactly as every round has the sides ``T`` and ``CT``. The
#: stack rule's locked condition forbids a human-supplied **area division** --
#: which areas belong around each of the sites -- and that is precisely what
#: is derived from the recording (``domain.sampling.site_groups``). The site
#: itself is not a division but the anchor the division is measured from.
#:
#: The vocabulary is here rather than next to the rule, because two domain
#: modules read it: the rule (``domain.sampling``) and the report model
#: (``domain.report``, which makes sure that ``Anomaly.site`` and
#: ``Anomaly.area`` cannot disagree). Of two copies it was exactly that pair
#: that diverged.
SITE_AREAS: Final[dict[str, str]] = {"A": "BombsiteA", "B": "BombsiteB"}

#: The ids of the site groups in their fixed order. **Derived, not a second
#: copy**: two hand-written lists could differ from each other.
SITE_GROUPS: Final[tuple[str, ...]] = tuple(SITE_AREAS)
SiteGroup = Literal["A", "B"]

#: A round that could not be classified at all (the observation is missing).
#: Not a round type but the absence of one: ``round_type`` is ``null`` in the
#: table, and this is its only visible name in the output and in the report's
#: sections.
UNCLASSIFIED: Final[str] = "luokittelematon"

#: The report model's Finnish names. Headings only -- no Finnish is written
#: into the data.
ROUND_TYPE_FI: Final[dict[str, str]] = {
    "pistol": "pistooli",
    "eco": "eco",
    "half": "puoliosto",
    "force": "force",
    "full": "default",
    "ot": "jatkoaika",
    "anomaly": "poikkeama",
}

#: The sample's three buckets (Story 2.3). **Three, not two:** ``is_league``
#: comes into being only in the ``select`` stage (Epic 3), so on a
#: hand-imported recording it is ``null``. A division into two buckets would
#: force such a recording to be marked either a league match or something
#: else, and either would be wrong.
SAMPLE_BUCKETS: Final[tuple[str, ...]] = ("league", "other", "unknown")
SampleBucketName = Literal["league", "other", "unknown"]

#: The Finnish names of the buckets. In the output only -- no Finnish is
#: written into the data, exactly as in :data:`ROUND_TYPE_FI`. The keys stay
#: English, because they are part of the ``report.json`` contract.
SAMPLE_BUCKET_FI: Final[dict[str, str]] = {
    "league": "liiga",
    "other": "muut",
    "unknown": "tuntematon",
}

#: The two **special names** of the utility time window, which are not time
#: ranges.
#:
#: ``UTILITY_BUCKET_ALL``
#:     The time windows have been switched off (``utility_seconds_buckets`` is
#:     empty), so there is one bucket.
#: ``UTILITY_BUCKET_UNKNOWN``
#:     The moment of the throw was not obtained: the anchor was missing, the
#:     time was negative, or it was not a finite number. A missing time does
#:     not drop out -- it gets a bucket of its own, so that it does not merge
#:     into the ``0-5`` bucket and look like an "insta".
#:
#: The names are here because they are written in ``aggregate`` and read in
#: ``render``. Two copies would diverge: changing the name would quietly put
#: the row ``" kaikki s"`` into the report instead of anything failing.
UTILITY_BUCKET_ALL: Final[str] = "kaikki"
UTILITY_BUCKET_UNKNOWN: Final[str] = "tuntematon"

#: The processing status of a unit (Match / MapDemo) (AD-9).
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

#: The kind of a sample point (AD-5).
SAMPLE_KINDS: Final[tuple[str, ...]] = ("time", "first_contact")
SampleKind = Literal["time", "first_contact"]

#: The kind of a utility event (AD-5). The exact demoparser2 names are
#: locked in Story 1.2.
EVENT_KINDS: Final[tuple[str, ...]] = ("grenade_thrown", "grenade_detonate")
EventKind = Literal["grenade_thrown", "grenade_detonate"]

#: Where a utility event's area comes from (AD-5).
#:
#: ``observed``
#:     The thrower's own ``m_szLastPlaceName`` from the same tick. On a throw
#:     row the area is therefore an observation, not an estimate.
#: ``point_cloud``
#:     The area of the nearest cell of the recording's own point cloud, from
#:     within the distance limit. A detonation has no area name of its own, so
#:     it is always an approximation -- but an approximation *of the game's
#:     own area definition*, not of a neighbouring one.
#:
#: ``null`` means that no area was obtained at all. Without this column the
#: report could not tell a certain fact from an estimate.
#:
#: **The value ``snapped`` went away in Story 2.9, because the method went
#: away.** It meant "the area of the nearest living player", and it measured
#: structurally the opposite of what it should have: smoke is thrown where
#: nobody is -- precisely because it blocks the view. Repairing the proxy
#: would have been impossible, because the fault was not in its accuracy but
#: in what it measured. The value was not left in the list as a fallback
#: source: two parallel methods would make the row uninterpretable, because
#: the reader would not see which of them named it. An old ``events.parquet``
#: therefore does not load into this enum, and that is intended -- ``parse``
#: runs the recording again without the ``--pakota`` flag.
AREA_SOURCES: Final[tuple[str, ...]] = ("observed", "point_cloud")
AreaSource = Literal["observed", "point_cloud"]

#: The roster threshold's class per MapDemo (AD-6).
ROSTER_CLASSES: Final[tuple[str, ...]] = ("5/5", "4/5")
RosterClass = Literal["5/5", "4/5"]

#: The bucket identifiers as a type. Pinned to :data:`ROSTER_BUCKETS` by
#: ``test_the_roster_bucket_names_are_the_bucket_list``, because a Literal and
#: a tuple are two hand-written spellings of one set.
RosterBucketName = Literal["full", "partial", "unknown"]

#: The bucket identifiers of the roster breakdown, **in class order**.
#:
#: A class name (``"5/5"``) is not a valid field name, so each class gets an
#: identifier. The order is the one
#: :func:`~pappascout.domain.selection.class_labels` returns -- it yields
#: ``(full, partial)`` -- and that coupling is what
#: ``test_the_roster_buckets_follow_the_order_class_labels_returns`` pins:
#: comparing against an index would pass just as happily with the pair
#: reversed, and a reversed pair would turn the whole report upside down in
#: silence.
_ROSTER_CLASS_BUCKETS: Final[tuple[RosterBucketName, ...]] = ("full", "partial")

#: ``roster_class`` -> the name of its summary bucket (Story 3.9).
#:
#: Built from :data:`ROSTER_CLASSES` rather than written out as a second list.
#: ``strict=True`` is deliberate: should there ever be a third class, the
#: import fails at once instead of the third class quietly falling out of the
#: report.
ROSTER_CLASS_BUCKET: Final[dict[str, RosterBucketName]] = dict(
    zip(ROSTER_CLASSES, _ROSTER_CLASS_BUCKETS, strict=True)
)

#: The three buckets of the roster breakdown.
#:
#: ``unknown`` holds **two different facts, and it cannot separate them.**
#: AD-6 stores a ``roster_class`` only when the roster threshold was met, so
#: the bucket contains both *not measured* (no ``select`` row for the demo, a
#: hand-imported demo, a lineup with no owner, a table classified before
#: ``select`` existed) and *measured and below the threshold* (``select``
#: judged the map and rejected it). The printed sentence therefore says the
#: class "ei ole vahvistettu" rather than claiming it is not known: what the
#: bucket honestly reports is the absence of a confirmed class, not the
#: absence of a measurement.
#:
#: Two buckets instead of three would have forced every such demo into
#: ``5/5`` or ``4/5``, and either is a claim nobody made.
ROSTER_BUCKETS: Final[tuple[RosterBucketName, ...]] = (
    *_ROSTER_CLASS_BUCKETS,
    "unknown",
)

#: Bucket names as printed. The first two **are the class name itself**
#: (``5/5``, ``4/5``), because that is the notation the reader knows from the
#: epic; the third is the same word the league breakdown uses. Output only,
#: like :data:`SAMPLE_BUCKET_FI` -- the keys in ``report.json`` stay English.
ROSTER_BUCKET_FI: Final[dict[str, str]] = {
    **{klass_bucket: klass for klass, klass_bucket in ROSTER_CLASS_BUCKET.items()},
    "unknown": SAMPLE_BUCKET_FI["unknown"],
}


# -- Weapon classification (Story 1.6) ----------------------------------------
#
# These names are values of demoparser2's ``inventory`` list, read from the
# tick at the end of the buy time. The names are **measured from six
# recordings** (Ancient, Nuke and four Pappaliiga recordings; Ancient's .dem
# and .dem.zst are the same match), not guessed: 48 different names. Nine
# other weapons of the game are in the list even though they did not occur in
# these recordings -- they exist in the game, and a missing weapon would
# quietly leave a counter too small.
#
# THE CLASSIFICATION IS A LIST OF THE ALLOWED WEAPONS, NOT OF THE FORBIDDEN
# ONES. Knives are an open set -- the six recordings already hold 15 different
# skin names, and Valve adds more in every shop update. There are 31 weapons
# and a new weapon is a rare event. If an unknown name were counted as a
# weapon, every new knife skin would arm a player wrongly and would do it
# quietly. A list of the allowed ones ages visibly and in the wrong direction:
# a new weapon goes uncounted, which is a safer error than a knife that arms.
#
# POSSESSION, NOT PURCHASE. The list says which *weapon* arms a player, not
# whether he bought it. The inventory is read at the end of the buy time, so a
# rifle saved from the previous round or picked up from a dead player counts
# the same as one just bought. That is intended and not a shortcoming: what
# decides the round is what is in hand, not where it came from, and a saved AK
# is exactly as dangerous as a bought one. The only exception is the default
# pistols, which are excluded because they are free every round -- possessing
# them says nothing at all.

#: Knives -- an **open set**; only what has been seen in the material is
#: here. The list is not and does not try to be complete: its only job is to
#: quieten the report of unknown names about those already recognised. A
#: missing knife arms nobody, because :data:`ARMING_WEAPONS` decides that.
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

#: The default pistols: a player gets them free every round, so possessing
#: them says nothing at all. The equipment value counts them in all the same,
#: at $200 -- which is exactly why the equipment value did not do as a
#: measure.
DEFAULT_PISTOLS: Final[frozenset[str]] = frozenset({"Glock-18", "P2000", "USP-S"})

#: The pistols that have to be bought separately -- the default pistol
#: comes free.
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

#: Submachine guns.
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

#: Rifles, sniper rifles and machine guns.
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

#: Shotguns.
SHOTGUNS: Final[frozenset[str]] = frozenset(
    {"MAG-7", "Nova", "Sawed-Off", "XM1014"}
)

#: Grenades. They do not arm: the user's definition is "kevlar and some
#: upgraded weapon", and a flash is not a weapon.
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

#: The items that are neither weapons nor grenades.
#:
#: The C4 is a mission item that a T gets free. The Zeus is a single
#: close-range shot with which a round is not played -- it does recharge in
#: CS2, but that does not make it a weapon under the user's definition, "a
#: better pistol, an SMG or a cheap rifle". So neither of them arms.
OTHER_ITEMS: Final[frozenset[str]] = frozenset({"C4 Explosive", "Zeus x27"})

#: The classes the whole classification is derived from:
#: ``(name, does it arm, set)``.
#:
#: **This is the only place a weapon class is added to.**
#: :data:`ARMING_WEAPONS`, :data:`KNOWN_INVENTORY_ITEMS` and
#: :func:`weapon_classification_digest` are all derived from this, so a new
#: class cannot end up in one of them and be left out of another. The union
#: used to be written out separately, and then a new class could be added to
#: arm players without the digest changing -- that is, the archive went
#: quietly stale.
#:
#: A class's **name and whether it arms** are part of the digest, not only its
#: contents: if a weapon moves from one class to another and not one name
#: disappears, the union would not change but the classification would.
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

#: **The arming weapons** (31 of them): a player is armed by any one of these
#: when he also has armour. Derived from :data:`_CLASSIFICATION`, not written
#: out separately. The default pistols are not among them (they come free), a
#: knife is not a weapon, a grenade is not a weapon.
ARMING_WEAPONS: Final[frozenset[str]] = frozenset(
    name for _, arms, names in _CLASSIFICATION if arms for name in names
)

#: All the known inventory names (57 of them). A name **outside** this set is
#: unknown: it arms nobody, and it is reported as part of the run. A silent
#: drop would be as bad as a silent acceptance.
KNOWN_INVENTORY_ITEMS: Final[frozenset[str]] = frozenset(
    name for _, _, names in _CLASSIFICATION for name in names
)


def weapon_classification_digest() -> str:
    """A digest of the **contents** of the weapon classification.

    It goes into the ``parse`` stage's parameter hash, so that a change to the
    table invalidates the archive and forces a re-parse. The alternative would
    be a ``[parse]`` setting raised by hand when the table changes -- that
    works only if nobody forgets, and ``settings.toml`` does not fill up with
    57 item names the user never adjusts.

    The digest covers the name of every :data:`_CLASSIFICATION` class, whether
    it arms, and its contents. A new class therefore changes the digest
    inevitably -- not because somebody remembers to add it here, but because
    the same list is the only source for :data:`ARMING_WEAPONS` as well.

    Returns:
        A 64-character hexadecimal sha256 digest. The same classification
        always gives the same digest: both the names and the classes are
        sorted, so the internal order of the sets and the order the classes
        are written in do not affect the result.
    """
    payload = "\n".join(
        f"{label}:{int(arms)}:{','.join(sorted(names))}"
        for label, arms, names in sorted(_CLASSIFICATION)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def seconds_label(value: float) -> str:
    """A number of seconds **as the report shows it**: ``45``, ``9,5``.

    The formatting is in ``constants`` and not inside the rendering, because
    **two layers have to agree about it** (Story 2.13). The setting
    ``[report].skip_sample_seconds`` names a sample point by the number the
    reader sees on the row, and the match is made from this form -- a
    floating-point comparison would let the spelling decide whether the row
    goes away (``45`` vs ``45.0``). The same function checks at load time that
    the setting does not hold two values that would look the same on the row.

    As two copies they would agree only **today**: if the report started
    showing one decimal, two setting values could mean the same row and the
    validation would not notice it.

    The decimal separator is a comma, because the report is in Finnish.
    """
    return f"{value:g}".replace(".", ",")
