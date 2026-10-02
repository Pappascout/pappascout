"""Typed settings models (AD-3) and how they are loaded.

The settings are split into eight sections, each of which is a model of its
own. The split is **structural**: a stage takes only its own section as a
parameter
(``parse(ParseSettings, ...)``, ``classify(ThresholdSettings, LeagueSettings, ...)``),
so it cannot read the other sections. This is the mechanism that makes the
promise "changing a threshold does not re-parse" structural: the ``parse``
manifest's parameter hash is computed from the ``[parse]`` section alone, and
the stage cannot depend on ``[thresholds]`` values by accident, because it
does not see them.

The credentials are not in ``settings.toml``. They are read from
``%USERPROFILE%\\.pappascout\\.env`` as ``SecretStr`` values; the project's own
``.env`` is read only as a fallback. The reason is the sync client: it would
make conflict copies of the project's ``.env`` on two machines and keep a
rotated credential in its own version history.

Every dollar threshold is **per player** unless the name says otherwise. The
sources of the starting values are recorded line by line in ``settings.toml``.

**The product owner's callout table is loaded here too** (Story 4.13,
AD-13): ``src/pappascout/callouts.toml``, read by :func:`load_callouts` into
:class:`CalloutEntry` values. It is not a settings section -- it ships with
the code and is the same on every machine -- but it is parsed at the same
edge and with the same stance: an unknown section or key is an error.
"""

from __future__ import annotations

import os
import re
import tomllib
from itertools import combinations
from math import floor, isfinite
from pathlib import Path
from collections.abc import Iterator, Mapping
from typing import Annotated, Literal, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as _ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from pappascout.constants import (
    is_sample_point,
    seconds_label,
    source_point_index,
)
from pappascout.errors import SettingsError

__all__ = [
    "ProjectSettings",
    "LeagueSettings",
    "ParseSettings",
    "ThresholdSettings",
    "AggregateSettings",
    "ReportSettings",
    "EconomySettings",
    "FaceitSettings",
    "Settings",
    "load_settings",
    "secrets_env_path",
    "project_env_path",
    "settings_search_paths",
    "find_settings_file",
    "SETTINGS_FILENAME",
    "SETTINGS_ENV_VAR",
    "SETTINGS_SECTIONS",
    "MAX_SNAPSHOT_SECONDS",
    "MAX_BUY_WINDOW_SECONDS",
    "MAX_ADVANCE_SAMPLE_SECONDS",
    "MAX_STACK_GROUP_MARGIN",
    "MAX_STACK_SITE_SEPARATION",
    "MAX_SITE_FLOOR_BAND_TRIM",
    "MAX_SITE_FLOOR_GAP_RATIO",
    "MAX_SITE_FLOOR_Z_WEIGHT",
    "MAX_FACEIT_PAGE_SIZE",
    "MAX_FACEIT_RETRY_ATTEMPTS",
    "PLAYERS_ON_SERVER",
    "REMOVED_SETTINGS",
    "CALLOUT_TABLE_PATH",
    "CERTAIN_CONFIDENCE",
    "CalloutConfidence",
    "CalloutEntry",
    "CellPart",
    "CellRegion",
    "CellSplit",
    "GuideFit",
    "MapCallouts",
    "NamedConfidence",
    "SPLIT_FLOOR_FIELDS",
    "SPLIT_GRID_FIELDS",
    "load_callouts",
    "named_places",
]

SETTINGS_FILENAME = "settings.toml"
SETTINGS_ENV_VAR = "PAPPASCOUT_SETTINGS"

#: The only top-level keys allowed in ``settings.toml`` (AD-3).
SETTINGS_SECTIONS: frozenset[str] = frozenset(
    {
        "project",
        "league",
        "parse",
        "thresholds",
        "aggregate",
        "report",
        "economy",
        "faceit",
    }
)

#: The settings that have been **removed**, and where they went.
#:
#: ``extra="forbid"`` rejects an old ``settings.toml`` in any case, but with a
#: generic "unknown key" message: the user would see only that their file is
#: not valid, and not what replaced it. This table gives the migration advice
#: -- in an archive shared by two machines an old file is the ordinary
#: situation, not the exception.
REMOVED_SETTINGS: Final[dict[tuple[str, str], str]] = {
    ("parse", "armed_player_equip_min"): (
        "Removed in Story 1.6. The equipment counter players_armed_buy_end no "
        "longer compares an equipment value against a threshold; it reads an "
        "observation instead: a player is armed if they hold armour and at "
        "least one weapon. The weapon list is in the code "
        "(src/pappascout/constants.py), because it is the set of the game's "
        "weapons and not an adjustable value. Remove the line from the file "
        "-- nothing needs to be put in its place."
    ),
    ("thresholds", "force_money_left_max"): (
        "Removed in Story 1.10. It was a fixed bound on the money left in "
        "pocket per player, that is the team total divided by five -- and the "
        "average was exactly what hid what a half-buy is about. Three "
        "settings replaced it: normal_buy_money_min (default 4000), "
        "normal_buy_players_min (default 3) and armed_players_min "
        "(default 3): a half-buy is told from a force by how many players can "
        "make a normal buy on the next round, and from an eco by how many "
        "were armed. Add the three new lines and remove this one."
    ),
    ("thresholds", "advance_area_min_observations"): (
        "Renamed in Story 4.6 to advance_area_min_observations_per_point, and "
        "the unit changed with the name: the bound is now the area's alive "
        "observations PER TIME SAMPLE POINT and no longer a raw count of "
        "rows. A raw count asked a different question on every grid, because "
        "an observation is one row per living player per round per sample "
        "point -- measured, the CT advance went from 10 hits on 6 rounds to "
        "45 on 9 when nothing but [parse].snapshot_seconds changed. Divide "
        "the old value by the number of sample points it was calibrated at: "
        "the shipped 20 was measured at four points, and 20 / 4 = 5 selects "
        "exactly the same areas. Rename the line and write the quotient."
    ),
    ("aggregate", "route_sample_seconds"): (
        "Removed in Story 4.13. The pistol route no longer reads only the "
        "printed sample points: it reads every point the demo holds and "
        "compresses each player's path to the product owner's junctions, "
        "which live in src/pappascout/callouts.toml. Remove the line from "
        "the file -- nothing needs to be put in its place."
    ),
}

#: The upper bound on a sample point in seconds. A CS2 round lasts 1:55 =
#: 115 s, so a value above that cannot land on any round at all.
MAX_SNAPSHOT_SECONDS = 115.0

#: Players on the server per team. A rule of the game, not a setting.
#:
#: A different thing from ``[thresholds].roster_size``: a standing roster may
#: hold substitutes (measured: seven players on one team), but no round has
#: six players on the server. Thresholds that count players are compared
#: against this, because the roster size would let through a condition that
#: cannot be met.
PLAYERS_ON_SERVER = 5

#: The upper bound on the buy window in seconds. **Its own bound, not
#: borrowed** from :data:`MAX_SNAPSHOT_SECONDS`: they are two independent
#: settings, and a shared constant would couple them so that adjusting one
#: moved the other's bound unnoticed.
#:
#: The value is **twice the game's own buy time** (20 s). It lets through a
#: tournament rule that differs from the default, but stops a typing error
#: (``200.0``) and a value at which the measuring point would no longer be
#: the buy time but an arbitrary moment in the middle of the round. Without a
#: bound of its own, 100.0 s would go through silently.
MAX_BUY_WINDOW_SECONDS = 40.0

PositiveInt = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]

#: A non-negative float for which **infinity and NaN are not valid**.
#:
#: ``allow_inf_nan=False`` is not decoration. The point-cloud weighting
#: multiplies by the value and compares the result against a threshold; NaN
#: would make every comparison false, so not one explosion would get an area
#: and nothing would say why. Infinity would do the same the other way round.
#: Both would get past a bare ``ge=0`` bound (``nan >= 0`` is false, but the
#: error message would talk about the wrong thing, and ``inf >= 0`` is true).
NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

#: **A majority share**: more than half, at most all of it. Infinity and NaN
#: are not valid.
#:
#: The lower bound is ``> 0.5`` and not ``>= 0``, and that is a definition
#: rather than a matter of taste. The threshold says which side an area
#: **belongs to**; at 0.5 or below an area could be both sides', and at 0.0
#: every area on the map would be T territory -- that is, the rule would fire
#: on every round and the anomaly count would no longer measure an anomaly.
#: The upper bound 1.0 is the definition of a share: a T share of 1.2 is not
#: a stricter threshold but an impossible condition, which would silence the
#: rule altogether.
#:
#: ``allow_inf_nan=False`` for the same reason as on
#: :data:`NonNegativeFloat`: NaN would make every comparison false, so no
#: area would be either side's territory and nothing would say why.
MajorityShare = Annotated[float, Field(gt=0.5, le=1.0, allow_inf_nan=False)]

#: The upper bound on the anomaly time bound in seconds.
#:
#: The same reasoning as on :data:`MAX_SNAPSHOT_SECONDS` but a stricter
#: number: the bound **selects among the sample points**, so a value above
#: them bounds nothing. 45 s is the largest ``[parse].snapshot_seconds``
#: point, and measurement showed it to be exactly the point that brings
#: uncertainty (two CTs on the T spawn's side after a won round). An upper
#: bound of 115.0 would let through a value that looks like a bound but
#: bounds nothing; 60.0 lets through every sensible choice of sample point
#: and stops the typing error ``300``.
#:
#: **Two consumers since Story 4.4, and their failure modes are opposite.**
#: ``advance_max_sample_s`` is a ceiling, and a value above the sample points
#: bounds nothing; ``stack_sample_s`` **is** a sample point, and a value that
#: is not one of them selects nothing -- the rule then reads no row at all,
#: at 61 s as at 16 s. This constant cannot see that difference, which is why
#: the stack's value is also checked against ``[parse].snapshot_seconds`` in
#: :meth:`Settings._check_sections_agree`. The shared ceiling is kept for the
#: typing error alone.
MAX_ADVANCE_SAMPLE_SECONDS = 60.0

#: The upper bound on the stack rule's group margin.
#:
#: The margin is a **ratio**: an area belongs to the nearer site only once
#: the other site is at least this many times further away.
#:
#: **The sites themselves do not drop out at any margin**, and that is worth
#: saying out loud, because the opposite is the natural guess. A site's
#: distance to its own centre is always 0, so for it the condition "the other
#: one is at least margin times further away" is always true -- ``BombsiteA``
#: is always in A and ``BombsiteB`` always in B. What the upper bound stops,
#: then, is **every other** area dropping out ungrouped: the rule wants at
#: least four players in the same site's group, and with two-area groups that
#: can be met only if the whole defence stands on the site itself. The rule
#: would then look as though it had run, but it would no longer measure the
#: setup.
#:
#: The other side is the commoner one: a typing error. The measured maps are
#: told apart at a margin of 1.25, so 10.0 lets through every sensible choice
#: and stops a value that is plainly of the wrong order of magnitude.
MAX_STACK_GROUP_MARGIN = 10.0

#: The upper bound on the stack rule's separation guard.
#:
#: The same reasoning as on the margin and on the anomaly time bound: the
#: threshold must have a ceiling, because going above it does not tighten the
#: rule but **silences it altogether**. The measured ratio is 0.47-5.04
#: (Nuke - Anubis), so a value above 20 would silence every known map, and
#: the report would claim "no stacks" as an observation although not one demo
#: was examined. 20.0 leaves fourfold room over the measured maximum and
#: stops a typing error.
MAX_STACK_SITE_SEPARATION = 20.0

#: A map counts as stacked when the empty height between the two sites' bands
#: reaches this share of the sites' own combined height. A **ratio** and not a
#: cell count: a cell index is not a coordinate, and the grid size multiplies
#: every distance by the same number, so an absolute limit would change
#: meaning with ``[parse].callout_grid_units``. Measured 2026-09-12 over the
#: archive, positive meaning the bands are apart: Nuke 0.75 on all three of
#: its demos, Anubis 0.14 and 0.00, Ancient -0.20 and 0.00, Inferno -0.38.
#:
#: The ceiling of 2.0 is **a guard and not a measured crossover**, and is
#: written down as such rather than dressed up: the void would have to be
#: twice the height of both sites together. The archive's largest is 0.75 and
#: the value the tests use to switch the branch off is 1.0, so both sit
#: strictly inside it -- the ceiling stops a typing error without being the
#: off switch itself.
MAX_SITE_FLOOR_GAP_RATIO = 2.0

#: How far into each site's own cells the band may be trimmed. Measured
#: 2026-09-12 at twelve points across the archive: 0.00 leaves no map stacked
#: at all, 0.01 catches only two of Nuke's three demos, **0.02 to 0.10 is a
#: plateau** giving exactly those three, and from 0.12 Anubis joins them. The
#: ceiling is the plateau's own upper edge -- past it the rule answers for a
#: map the measurement says is not stacked, which is not a calibration but a
#: wrong answer.
MAX_SITE_FLOOR_BAND_TRIM = 0.10

#: How much height may count in the distance on a stacked map. Measured
#: against the product owner's own Nuke grouping: weight 1 reproduces 10 of 15
#: area assignments, 2 gives 13, **3 gives 14**, and 5 and 8 give 14 as well
#: -- the result stops improving at 3. This is the one of the three that is
#: fitted against a human division rather than read from geometry alone;
#: AD-13 permits it, and saying so is part of the record. The 14 was measured
#: holding out two bridges (Ramp, Vents); the shipped rule holds out three,
#: and Secret -- the single remaining disagreement -- is the one it adds.
#:
#: The ceiling of 20 is a guard rather than a measured crossover: nothing
#: here establishes where the plan view stops contributing, only that the
#: answer has not moved since 3.
MAX_SITE_FLOOR_Z_WEIGHT = 20.0

#: The edge of a point-cloud cell. The bounds are performance and sense, not
#: a matter of taste. **Lower bound 8**: the nearest-cell search is a cross
#: product between the points and the cells, and an edge of 1 would produce
#: of the order of a million cells from one demo -- instead of a hundred
#: thousand cells. **Upper bound 1024**: 32 player widths, that is, a grid
#: coarser than that no longer tells the map's areas apart.
CalloutGridUnits = Annotated[int, Field(ge=8, le=1024)]

#: The upper bound on a FACEIT Data API page. **The interface's own bound,
#: not a matter of taste**: a ``limit`` above 100 does not bring more rows
#: but a 400 Bad Request, and read from the settings file it would break
#: every fetch in a way that would look like a network fault.
#:
#: **The bound is here, but the adapter watches the same constant.** An
#: earlier rationale claimed that the adapter cannot check the value "because
#: it does not read the settings" -- that was not true:
#: :mod:`pappascout.adapters.faceit` imports this constant and rejects a
#: value outside the bound itself. The adapter does not *read* the settings
#: file, but neither does it silently accept what the settings model rejects
#: -- and clamping silently would produce exactly the 400 that would look
#: like a network fault. The number is still only here, so that it is not in
#: two places.
MAX_FACEIT_PAGE_SIZE = 100

#: The upper bound on retries. The threshold must have a ceiling for the same
#: reason as :data:`MAX_STACK_SITE_SEPARATION`, but the other way round: too
#: **large** a value tightens nothing and instead turns an error the user
#: would see into an hour of silence. The growing delay means the tenth
#: attempt is already minutes away; the typing error ``100`` would stall the
#: run without anything saying why.
MAX_FACEIT_RETRY_ATTEMPTS = 10


class _Section(BaseModel):
    """A settings section's base class: an unknown key is an error, not a
    silent skip.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class ProjectSettings(_Section):
    """``[project]`` -- own team, the archive and the run's basic settings.

    Attributes:
        own_team_name: Own team.
        archive_root: The archive root. In a synchronised folder and shared
            by both machines.
        language: The user interface language.
        lock_ttl_seconds: The archive lock's expiry time.
    """

    own_team_name: str
    archive_root: Path
    language: Literal["fi"] = "fi"
    lock_ttl_seconds: PositiveInt = 600


class LeagueSettings(_Section):
    """``[league]`` -- the Pappaliiga season, map pool and rule-book values.

    Read in the ``select``, ``classify`` and ``aggregate`` stages. **Not in
    ``render``**: the report gets its league information from
    ``report.json``, and that stage's only section of its own is ``[report]``
    (Story 2.13). The claim was here before that, and it was already wrong
    then -- the stage took no settings section as a parameter at all.
    """

    season: PositiveInt
    organizer_id: str
    championship_ids: list[str] = Field(min_length=1)
    map_pool: list[str] = Field(min_length=1)
    own_default_bans: list[str] = Field(default_factory=list)
    mr: PositiveInt = 12
    ot_start_money: PositiveInt = 12500

    @model_validator(mode="after")
    def _check_bans_are_in_pool(self) -> "LeagueSettings":
        outside = [m for m in self.own_default_bans if m not in self.map_pool]
        if outside:
            raise ValueError(
                "own_default_bans holds a map that is not in the map pool: "
                f"{', '.join(outside)}. Did the map leave Active Duty?"
            )
        return self


class ParseSettings(_Section):
    """``[parse]`` -- the only section the ``parse`` stage sees.

    The ``parse`` manifest's parameter hash is computed from this section and
    demoparser2's version alone, so adjusting a threshold does not cause a
    re-parse.
    """

    snapshot_seconds: list[float] = Field(min_length=1)
    #: The length of the buy time in seconds from the end of freezetime. The
    #: economy's measuring point is::
    #:
    #:     max(anchor,
    #:         min(anchor + this,
    #:             the tick BEFORE the first death,
    #:             the end of the round))
    #:
    #: and not the end of freezetime: CS2's buy time continues after the
    #: round has started, and on about half of the rounds buying still goes
    #: on after it. The tick **before** the death and not the death's tick,
    #: because a dead player's inventory is already empty; the outermost
    #: ``max`` is a lower bound that stops the measuring point sliding into
    #: freezetime.
    #:
    #: The default 20.0 is **a decision that measurement supports** and not a
    #: calibrated value -- the rationale, the measurements and their limits
    #: are in ``settings.toml``'s line comment. The allowed range is
    #: 0..:data:`MAX_BUY_WINDOW_SECONDS`; 0 means "measure from the anchor".
    buy_window_seconds: float = 20.0
    #: Weapons that do not count as a first contact (AD-5: "a weapon is not
    #: utility").
    first_contact_exclude_weapons: list[str] = Field(default_factory=list)
    #: If no first contact is found among the player_hurt events, whether the
    #: first player_death event is used.
    first_contact_fallback_death: bool = True
    #: The greatest distance in game units from which an explosion's area may
    #: come **from the nearest point-cloud cell** (Story 2.9).
    #:
    #: **Required, not optional.** It defaulted to ``None`` for as long as
    #: the area was snapped from the nearest player and the snapping could be
    #: switched off. The nearest cell in the point cloud is **always** found,
    #: so a run without a threshold would give every explosion an area
    #: regardless of distance and coverage would always be 100% -- that would
    #: not be coverage but the absence of a measure. The spec's Always rule
    #: is "the distance threshold stays", and a required field makes that
    #: structural.
    #:
    #: The measure is weighted in the same way as the naming (see the next
    #: three), so it is not a Euclidean distance but the number by which the
    #: nearest cell was chosen. The distance is stored in
    #: ``EVENTS.snap_distance`` even when it exceeds the threshold.
    area_snap_units: PositiveInt
    #: The edge of a point-cloud cell in game units. Measured 2026-08-30 on
    #: two demos: 32 halves the median distance compared with 64 (15 vs. 29).
    #: A cell of 64 covers slightly more (Ancient 93.2% vs. 91.8%), but a
    #: small distance is what makes the named area likely to be the right
    #: one. At 32 there are about 7,700-10,500 cells per demo. For the bounds
    #: see :data:`CalloutGridUnits`.
    callout_grid_units: CalloutGridUnits = 32
    #: The weight on the vertical difference when the nearest cell is sought
    #: for an explosion. Layered maps (Nuke) need it: a cell one floor down
    #: is right next door seen from above but in a different area. **Only
    #: together with the tolerance** -- see the next one.
    #:
    #: The default is **1.0 and not 2.0**: measured, a weight of 1 separates
    #: the floors perfectly (zero wrong floors on both Nuke demos), and every
    #: weight above it costs coverage while buying nothing -- 99.0% at
    #: weight 1, 98.8% at weight 2, 97.4% at weight 3.
    callout_z_weight: NonNegativeFloat = 1.0
    #: The vertical difference that is free in the weighting. A player's
    #: height is 72 units: the point cloud stores the player's position, but
    #: a grenade explodes anywhere between the floor and the head (smoke in
    #: the air, a molotov on the floor), so without a tolerance the vertical
    #: penalty would hit the ordinary case. Measured: the weight on its own
    #: without a tolerance is **worse** than no weight at all (median 30 vs.
    #: 20); with the tolerance the median falls to 15.
    #:
    #: **Symmetrical, and that is measured.** Freedom upwards only (smoke
    #: drifts) is worse when measured: the median rises 15 -> 17 (Ancient)
    #: and 14 -> 19 (Nuke) and coverage does not improve at all.
    callout_z_tolerance_units: NonNegativeFloat = 72.0

    # The equipment counter (``players_armed_buy_end``) **has no setting**.
    # Story 1.5's ``armed_player_equip_min`` measured an equipment value,
    # which is weapon + armour + grenades as one number; Story 1.6 changed
    # the measure to the observation "armour and at least one weapon in
    # hand", and the weapon list is in the code
    # (:mod:`pappascout.constants`) and not here. A change to the list
    # invalidates the archive all the same, because ``stages.parse`` takes a
    # digest of it into the parameter hash -- the user does not adjust 57
    # item names.
    #
    # The counter measures **possession, not a purchase**: a saved or
    # picked-up rifle counts the same as a bought one, because what settles
    # the round is what is in hand. See :mod:`pappascout.constants`.

    @model_validator(mode="after")
    def _check_buy_window(self) -> "ParseSettings":
        """The buy window is a setting, so it has to be checked at load time.

        Three ways to break silently:

        * **NaN or infinity** would break the ``round()`` call in the middle
          of parsing, after hundreds of megabytes have been read.
        * **A negative value** would move the measuring point into
          freezetime, that is, before the team has even bought. Zero is
          allowed and means "measure from the end of freezetime" -- it is the
          behaviour that preceded this story, and it is a valid choice.
        * **A window above the upper bound** would measure nothing new: the
          measuring point is bounded by the end of the round and the first
          death in any case, so too large a value is almost certainly a
          typing error. The bound is :data:`MAX_BUY_WINDOW_SECONDS` and not
          the sample points' bound.
        """
        value = self.buy_window_seconds
        # isfinite FIRST and not after the comparisons: nan is less than,
        # greater than and equal to anything -- all False. A check made of
        # comparisons would let it through, and round(nan * tick_rate) would
        # break only inside the parsing, after the 400 MB demo has already
        # been decompressed and read.
        if not isfinite(value):
            raise ValueError(
                f"buy_window_seconds is {value!r}, which is not a finite "
                "number. The buy window is multiplied by the tick rate during "
                "parsing, so nan and infinity would break only after the demo "
                "has been read."
            )
        if value < 0:
            raise ValueError(
                f"buy_window_seconds is {value:g}, which is negative. The buy "
                "time is measured forward from the end of freezetime; a "
                "negative value would move the measuring point into "
                "freezetime."
            )
        if value > MAX_BUY_WINDOW_SECONDS:
            raise ValueError(
                f"buy_window_seconds is {value:g} s, which exceeds the buy "
                f"window's upper bound ({MAX_BUY_WINDOW_SECONDS:g} s, that is "
                "twice the game's own buy time). The measuring point is "
                "bounded by the end of the round and the first death in any "
                "case, so a larger value would measure nothing new."
            )
        return self

    @model_validator(mode="after")
    def _check_snapshot_seconds(self) -> "ParseSettings":
        """The sample point times are a setting, so they have to be checked
        at load time.

        Three ways to break silently:

        * **An empty list** never reaches this validator: ``min_length=1``
          refuses ``[]`` as ``too_short`` and a missing key as ``Field
          required`` (measured on pydantic 2.13.4). The check below is a
          second lock that says the consequence out loud: the table would
          otherwise hold nothing but first contacts, and a faulty
          configuration would look like a successful run.
        * **NaN or infinity** would break the ``round()`` call in the middle
          of parsing, after hundreds of megabytes have been read.
        * **A typing error** such as ``450.0`` (``45.0`` intended) would
          break nothing: the point would simply drop off every round, and the
          table would be quietly short.
        """
        if not self.snapshot_seconds:
            raise ValueError(
                "snapshot_seconds is empty. Without sample points the setup "
                "table would rest on first contacts alone."
            )
        for value in self.snapshot_seconds:
            if not isfinite(value):
                raise ValueError(
                    f"snapshot_seconds holds the value {value!r}, which is "
                    "not a finite number."
                )
            if value <= 0:
                raise ValueError(
                    f"snapshot_seconds holds the value {value:g}, which is "
                    "not positive. Sample points are measured forward from "
                    "the end of freezetime; zero would be the anchor itself, "
                    "where nobody has moved yet."
                )
            if value > MAX_SNAPSHOT_SECONDS:
                raise ValueError(
                    f"snapshot_seconds holds the value {value:g} s, which "
                    f"exceeds the length of a round "
                    f"({MAX_SNAPSHOT_SECONDS:g} s). The point would drop off "
                    "every round, so it is almost certainly a typing error."
                )
        duplicates = sorted(
            {a for a in self.snapshot_seconds if self.snapshot_seconds.count(a) > 1}
        )
        if duplicates:
            raise ValueError(
                "snapshot_seconds holds the same time twice: "
                f"{', '.join(f'{a:g}' for a in duplicates)}."
            )
        return self


class ThresholdSettings(_Section):
    """``[thresholds]`` -- every bound for classification and sampling.

    The classification thresholds were calibrated 2026-08-29 against a truth
    table given by a human (``kalibrointi-kierrostyypit.md``); the rationales
    and the observed range are in ``settings.toml``'s comments.

    **Not all of them are observations.** The distinction is a per-line mark
    in ``settings.toml``, and it is meant to be kept. The marks themselves
    stay in the Finnish they are written in, because they are the literal
    text of that file and a reader greps for them there:

    * ``[kalibroitu]`` -- "calibrated": the value sits in an empty gap in the
      data and the margin to the nearest observation has been measured
      (``full_equip_min``, ``force_buy_min``,
      ``anomaly_equip_max_after_win``, ``normal_buy_money_min``).
    * ``[lausuttu]`` -- "stated": the value comes from a rule the user
      stated, and not one measured round tests it (``armed_players_min``).
    * The third mark, which ends ``odottaa havaintoa]`` in the file --
      "inferred, awaiting an observation": the value is reasoning, and the
      data would give the same result over a wide range
      (``normal_buy_players_min``).

    The sampling bounds (``small_sample_rounds``, ``roster_*``) are still
    waiting for data of their own.

    Story 2.5's anomaly thresholds (``advance_*``, ``crunch_*``) are
    ``[kalibroitu]``: each was measured on eight demos and two different
    teams (``kalibrointi-ct-eteneminen.md``), and how often they fire is of
    the order of an anomaly rather than of ordinary play.
    ``stack_min_players`` sat unused until Story 2.14: the stack rule became
    possible once the missing piece -- a mapping area -> area group -- could
    be derived from the demo's own point cloud, and the rule reads the
    threshold now. Story 4.4 added ``stack_max_areas`` and ``stack_sample_s``
    beside it: measured against 43 of the product owner's own judgements,
    both bounded from both sides by them, and together they are what tells a
    crowd from a defence spread across half the map.

    The sums of money are dollars **per player**, except
    ``normal_buy_money_min``, which is **one player's** own balance and not
    the team average.
    """

    # Rules based on the round number (AD-4 steps 1 and 2)
    pistol_rounds: list[PositiveInt] = Field(min_length=1)
    regulation_rounds: PositiveInt = 24

    # Equipment value bounds (AD-4 steps 3 and 5)
    full_equip_min: PositiveInt = 4000
    anomaly_equip_max_after_win: PositiveInt = 2000

    # Money bounds (AD-4 step 4). A purchase is the shared precondition of
    # every class that follows a loss: force_buy_min is the condition for a
    # force and for a half-buy, not merely their band.
    force_buy_min: PositiveInt = 1500

    # The half-buy's two conditions (Story 1.10). BOTH have to be met.
    #
    # A: how many players were armed -- tells a half-buy from an ECO.
    # B: how many players can make a normal buy on the next round, once the
    #    loss bonus is added to the money left in pocket -- tells a half-buy
    #    from a FORCE.
    #
    # normal_buy_money_min is ONE PLAYER'S own balance, not the team average.
    # That difference is the whole reason for the rule: an average hides the
    # distribution.
    armed_players_min: PositiveInt = 3
    normal_buy_money_min: PositiveInt = 4000
    normal_buy_players_min: PositiveInt = 3

    # Loss count rules
    loss_count_half_start: NonNegativeInt = 1
    loss_count_min: NonNegativeInt = 0
    loss_count_max: PositiveInt = 4

    # Team identity and the roster threshold (AD-6)
    team_identity_min_common: PositiveInt = 3
    roster_size: PositiveInt = 5
    roster_min_regulars: PositiveInt = 4

    # Sampling and anomalies (AD-10)
    small_sample_rounds: PositiveInt = 3

    # The stack rule (Story 2.14). stack_min_players sat here UNUSED from
    # Story 2.5 onwards; it was put to work only once the missing piece -- a
    # mapping area -> area group -- could be derived from the demo's own
    # point cloud (domain.sampling.site_groups).
    #
    # All three are MEASURED rather than chosen: the rationale value by value
    # is in settings.toml's comments and in full in kalibrointi-stack.md.
    #
    # Both of the new ones are RATIOS and not game units: each is compared
    # against the quotient of two distances, so the point cloud's grid size
    # ([parse].callout_grid_units) cancels out of them. That is precisely why
    # aggregation does not read the [parse] section on account of this rule.
    # If an absolute distance bound is ever needed, it CANNOT be a number of
    # this kind: a cell index is not a coordinate, it has to be multiplied by
    # the cell size first.
    stack_min_players: PositiveInt = 4
    # The concentration bound and the setup point (Story 4.4). Both are
    # MEASURED against the product owner's own 43 judgements, and both are
    # bounded FROM BOTH SIDES by them; the measurement is
    # stack-saanto-mitattu-2026-09-18.md and the figures are in
    # settings.toml's comments.
    #
    # stack_max_areas is what makes the rule a stack rule: without it the
    # question is "how many are in the group", and a group is half the map.
    #
    # TWO POPULATIONS, AND EVERY FIGURE NAMES ITS OWN. Over all 43 judged
    # rounds: at 1 area the condition finds 3 of his 8 stack-like rounds, at 2
    # all 8 and none of his 33 "not a stack" rounds, at 3 it fires on 9 of
    # them. Eleven of the 43 are the opponent's CT rounds, which this rule
    # cannot scan; inside its own scope (the subject's 93 CT rounds) the
    # judgements are 4 stacks, 2 named pushes and 26 "not a stack", and the
    # shipped value finds 4 of the 4 and fires on 0 of the 26. At 3 areas it
    # adds 8 of those 26.
    stack_max_areas: PositiveInt = 2
    # The setup point is ONE moment and not a bound: a setup IS a moment,
    # while the other two rules ask about movement and therefore have only a
    # ceiling (advance_max_sample_s). At 6 s this would measure the walk out
    # of spawn (the rule fires on 34 rounds of 93 instead of 5), at 30 s the
    # reaction to the round.
    #
    # THE RANGE BELOW IS NOT THE REAL CONDITION. The value has to BE one of
    # [parse].snapshot_seconds: 16.0 is inside every bound here and still
    # selects no row on any round, and the coverage would go on reporting
    # 93/93 scanned. That is checked in Settings._check_sections_agree,
    # because neither section can see it alone -- this one does not know the
    # sample points and [parse] does not know the rule.
    stack_sample_s: Annotated[
        float, Field(gt=0.0, le=MAX_ADVANCE_SAMPLE_SECONDS, allow_inf_nan=False)
    ] = 15.0
    # The lower bound 1.0 is a definition and not a dial: the margin says how
    # much further away the other site has to be before an area is counted
    # into the nearer one's group, and below 1 the "nearer" site could be the
    # further one. Exactly 1.0 is valid: then every area belongs to the
    # nearer site and none is left shared.
    stack_group_margin: Annotated[
        float, Field(ge=1.0, le=MAX_STACK_GROUP_MARGIN, allow_inf_nan=False)
    ] = 1.25
    # The lower bound is above 0: at 0 the guard would silence no map at all,
    # so a division into areas that does not exist would be derived from
    # Nuke's overlapping sites -- and the rule would report Nuke's rounds as
    # examined.
    stack_site_separation_min: Annotated[
        float,
        Field(gt=0.0, le=MAX_STACK_SITE_SEPARATION, allow_inf_nan=False),
    ] = 2.0

    # A map whose sites sit on different floors (Story 4.3). The separation
    # guard above measures distance in plan view, which is the wrong axis
    # there -- so these three decide when height is the axis instead. All
    # three are MEASURED; the value-by-value rationale is in settings.toml
    # and in full in kalibrointi-nimetty-kuvio.md.
    #
    # The upper bounds are the same shape as MAX_STACK_SITE_SEPARATION: a
    # value beyond them would not be a calibration but a different rule.
    site_floor_gap_ratio: Annotated[
        float, Field(gt=0.0, le=MAX_SITE_FLOOR_GAP_RATIO, allow_inf_nan=False)
    ] = 0.40
    # The trim is a threshold in its own right and is here rather than buried
    # in the function, because the answer turns on it: below 0.02 one of
    # Nuke's three demos stops counting as stacked and the map would divide
    # differently across its own demos; at 0.12 Anubis starts counting.
    # Its bounds are what the measurement supports and nothing wider.
    site_floor_band_trim: Annotated[
        float, Field(gt=0.0, le=MAX_SITE_FLOOR_BAND_TRIM, allow_inf_nan=False)
    ] = 0.05
    site_floor_z_weight: Annotated[
        float, Field(ge=1.0, le=MAX_SITE_FLOOR_Z_WEIGHT, allow_inf_nan=False)
    ] = 3.0
    site_bridge_void_share: Annotated[
        float, Field(gt=0.0, le=1.0, allow_inf_nan=False)
    ] = 0.10

    # The anomaly rules (Story 2.5). All six are MEASURED rather than chosen:
    # the rationale value by value is in settings.toml's comments and in full
    # in kalibrointi-ct-eteneminen.md.
    #
    # advance_t_share is SHARED by both rules: both ask the same "the area is
    # held by the T side" condition, and with two computations they could
    # disagree about whose territory an area is. Crunch adds a requirement
    # about directions to the condition but drops the round-type restriction,
    # so it is not a stricter form of an advance but a different cut through
    # the same observation.
    advance_t_share: MajorityShare = 0.80
    # PER SAMPLE POINT and not a raw count of rows (Story 4.6). An area's
    # observations are one row per living player per round per sample point,
    # so a raw bound asks a different question on every grid: measured, the
    # advance went from 10 hits on 6 rounds to 45 on 9 when nothing but
    # [parse].snapshot_seconds changed, four points to fourteen. 20 / 4 = 5,
    # so the value below selects exactly the areas the raw 20 selected -- an
    # arithmetic identity at four points and not a recalibration.
    #
    # THE DIVISOR IS THE DEMO'S OWN NUMBER OF SAMPLE POINTS, derived from its
    # rows (domain.sampling.sample_point_count) and not from
    # [parse].snapshot_seconds. A parsed table and an edited settings file are
    # allowed to disagree -- that is the ordinary state between a settings
    # change and the next parse -- and the rows are the observation.
    advance_area_min_observations_per_point: PositiveInt = 5
    advance_max_sample_s: Annotated[
        float, Field(gt=0.0, le=MAX_ADVANCE_SAMPLE_SECONDS, allow_inf_nan=False)
    ] = 30.0
    advance_min_players: PositiveInt = 1
    # The lower bound 2 is a rule and not a dial: a crunch is by definition
    # arrival from **several** directions (the product owner's own words,
    # kept in Finnish: "kahdesta tai useammasta suunnasta samaan aikaan"),
    # and at 1 both the rule and the report's reading guide would claim
    # something the code no longer requires.
    crunch_min_players: PositiveInt = 2
    crunch_min_sources: Annotated[int, Field(ge=2)] = 2
    # How far back the crunch reads a player's arrival, in SECONDS (Story
    # 4.6). It used to be "the previous sample point", which is not a duration
    # at all but the grid's spacing: 9 s at 15 s and 15 s at 30 s on the
    # four-point grid, 3 s at both on the fourteen-point one. Measured, the
    # crunch fell from 5 hits on 4 rounds to 2 on 2 on the same demos when
    # only the grid changed.
    #
    # 9.0 IS MEASURED AGAINST THE FOUR-POINT GRID and reproduces it exactly:
    # 15 - 9 = 6, which is a sample point, and 30 - 9 = 21, which is not, so
    # the source falls back to 15 -- the same two answers the previous-point
    # rule gave. Above 9 the 15 s target loses its source and the rule stops
    # finding two of its four rounds.
    #
    # WHAT "MEASURED" DOES NOT MEAN HERE, said plainly because the word is
    # load-bearing everywhere else in this file: every value in (0, 9] gives
    # the same four rounds and the same source areas, so the measurement
    # bounds 9.0 from above only. 15 - x lands on the 6 s point and 30 - x on
    # the 15 s point for every x in that interval, which makes the sweep flat
    # below 9 by arithmetic rather than by observation. 9.0 is the LARGEST of
    # those equals, and is chosen so that the rule asks no narrower a question
    # than the previous-point rule it replaces -- not because its lower
    # neighbours were shown to cost anything. A grid with three-second spacing
    # does separate them (tests/test_calibration.py's
    # DENSE_DEMO_LOOKBACK_SWEEP), so this threshold has to be measured again
    # if the grid is ever densified; the flat stretch is that grid's
    # blindness and must not be read as robustness.
    #
    # The upper bound is the family's own
    # (MAX_ADVANCE_SAMPLE_SECONDS); the value that binds in practice is
    # checked against the grid in Settings._check_sections_agree, because
    # neither section can see it alone.
    crunch_lookback_s: Annotated[
        float, Field(gt=0.0, le=MAX_ADVANCE_SAMPLE_SECONDS, allow_inf_nan=False)
    ] = 9.0

    @model_validator(mode="after")
    def _check_ranges_are_consistent(self) -> "ThresholdSettings":
        if self.anomaly_equip_max_after_win >= self.full_equip_min:
            raise ValueError(
                f"anomaly_equip_max_after_win ({self.anomaly_equip_max_after_win}) "
                f"must be smaller than full_equip_min ({self.full_equip_min}); "
                "otherwise the after-a-win anomaly bound would swallow the "
                "full buy, and every won round would be either an anomaly or "
                "a full buy depending on which bound happens to be the higher."
            )
        # Player-count thresholds are compared against the number **on the
        # server** and not against the roster size. They differ: a standing
        # roster may hold substitutes (measured: seven players on one team),
        # but there are always five on the server, so six armed or six
        # players in an area is an impossible condition even where
        # roster_size would let it through.
        on_server = min(self.roster_size, PLAYERS_ON_SERVER)
        for name in (
            "armed_players_min",
            "normal_buy_players_min",
            "advance_min_players",
            "crunch_min_players",
            # Stack is the only threshold measured RIGHT UP TO the upper
            # bound: four is calibrated and five is a genuine extreme
            # (2 rounds of the archive's 93 -- the figure was 2 of 66 while
            # Nuke was silenced, before Story 4.3), so five is a valid value
            # and the guard must not reject it. Six defenders, on the other
            # hand, is an impossible observation.
            "stack_min_players",
        ):
            value = getattr(self, name)
            if value > on_server:
                raise ValueError(
                    f"{name} ({value}) is greater than the number of players "
                    f"on the server ({on_server}), so the condition cannot be "
                    "met on any round and the rule could never fire."
                )
        # stack_max_areas is checked against the same number but in the
        # OPPOSITE direction, so it is not on the list above: the bound is a
        # ceiling, and a ceiling above the number of players never binds. Five
        # players can stand on five areas at most, so from six upwards the
        # rule would be the one Story 4.4 replaced -- "four somewhere in half
        # the map" -- under the name of the new one.
        if self.stack_max_areas > on_server:
            raise ValueError(
                f"stack_max_areas ({self.stack_max_areas}) is greater than the "
                f"number of players on the server ({on_server}), so the "
                "concentration bound could never exclude a single round. The "
                "rule would report every group of four as a stack, which is "
                "precisely the definition Story 4.4 measured as wrong."
            )
        if self.force_buy_min >= self.full_equip_min:
            raise ValueError(
                f"force_buy_min ({self.force_buy_min}) must be smaller than "
                f"full_equip_min ({self.full_equip_min}); otherwise every "
                "purchase that meets the force condition would already raise "
                "the equipment value to the full buy bound, and neither a "
                "force nor a half-buy could ever be reached."
            )
        if self.loss_count_min >= self.loss_count_max:
            raise ValueError(
                f"loss_count_min ({self.loss_count_min}) must be smaller than "
                f"loss_count_max ({self.loss_count_max})."
            )
        if not (
            self.loss_count_min
            <= self.loss_count_half_start
            <= self.loss_count_max
        ):
            raise ValueError(
                f"loss_count_half_start ({self.loss_count_half_start}) is "
                f"outside the bounds {self.loss_count_min}-{self.loss_count_max}."
            )
        if self.roster_min_regulars > self.roster_size:
            raise ValueError(
                f"roster_min_regulars ({self.roster_min_regulars}) cannot be "
                f"greater than roster_size ({self.roster_size})."
            )
        # NOTE: crunch_min_players and advance_min_players are NOT ordered
        # with respect to each other, and no such guard may be added. Crunch
        # is not a stricter form of an advance: it is stricter about
        # directions and looser about the round type, so neither set of hits
        # contains the other (measured: MatureMayhem Anubis round 10 is a
        # full buy on which no advance exists at all). A comparison between
        # these two would therefore be a rule without a reason.
        if self.crunch_min_sources > self.crunch_min_players:
            raise ValueError(
                f"crunch_min_sources ({self.crunch_min_sources}) is greater "
                f"than crunch_min_players ({self.crunch_min_players}). Every "
                "source direction needs a player of its own, so the condition "
                "could not be met at any sample point."
            )
        return self


class AggregateSettings(_Section):
    """``[aggregate]`` -- the ``aggregate`` stage's own values only (AD-3).

    **Why a section of its own and not ``[thresholds]``.** ``classify``
    computes its parameter hash from the whole ``[thresholds]`` section,
    because a partial hash would need a list of which fields the rules happen
    to read -- and that list would go stale silently. The price is that any
    addition to that section invalidates every classified demo. Utility's
    time buckets are purely a presentation choice made in aggregation, and
    ``classify`` does not read them at all, so adjusting them must not force
    a re-classification. The split makes that structural: ``classify`` does
    not see this section.
    """

    #: The time buckets' bounds in seconds from the start of the round. The
    #: bounds ``[5, 10, 20]`` produce the buckets ``0-5``, ``5-10``,
    #: ``10-20`` and ``20+``; a bound always belongs to the upper bucket. The
    #: default is **the same as in settings.toml**: an empty default would
    #: quietly produce a one-bucket report if the key were forgotten from the
    #: file.
    utility_seconds_buckets: list[float] = Field(
        default_factory=lambda: [5.0, 10.0, 20.0]
    )

    @model_validator(mode="after")
    def _check_utility_buckets(self) -> "AggregateSettings":
        """The time buckets are a setting, so they are checked at load time.

        Four ways to break silently:

        * **NaN or infinity** would slip through the comparisons and end up
          in a bucket name that means nothing.
        * **A negative or zero** bound would produce a bucket that no throw
          can land in: ``t_s`` is measured forward from the end of
          freezetime.
        * **An unordered or repeating** list would produce a bucket whose
          lower bound is greater than its upper bound -- it would not break,
          it would stay empty and hand its throws to the neighbouring bucket.
        * **Two bounds that look the same in the name** (for example
          ``5.0000001`` and ``5.0000002``) would produce two buckets with the
          same name, which would make the report's row ambiguous. The name is
          formatted to the shortest representation, so the check is made on
          the names and not on the numbers.

        An empty list is allowed and means one bucket (``kaikki``, the
        Finnish name the report prints): removing a time bucket is a valid
        choice and does not have to be a code change.
        """
        previous: float | None = None
        for value in self.utility_seconds_buckets:
            if not isfinite(value):
                raise ValueError(
                    f"utility_seconds_buckets holds the value {value!r}, "
                    "which is not a finite number."
                )
            if value <= 0:
                raise ValueError(
                    f"utility_seconds_buckets holds the value {value:g}, "
                    "which is not positive. The time buckets are measured "
                    "forward from the end of freezetime, so zero would be the "
                    "first bucket's lower bound and not its upper bound."
                )
            if previous is not None and value <= previous:
                raise ValueError(
                    "utility_seconds_buckets must be strictly increasing; "
                    f"{value:g} comes after {previous:g}. An unordered list "
                    "would hold a bucket whose lower bound is greater than "
                    "its upper bound -- it would quietly stay empty."
                )
            previous = value
        names = [f"{v:g}" for v in self.utility_seconds_buckets]
        if len(names) != len(set(names)):
            raise ValueError(
                "utility_seconds_buckets holds two bounds that look the same "
                f"in the bucket name ({', '.join(names)}). The name is "
                "formatted to the shortest representation, so two values "
                "close together would produce two buckets with the same name."
            )
        return self


class ReportSettings(_Section):
    """``[report]`` -- the pruning rules: what the report leaves unwritten.

    Story 2.13. The report ran to **about 96 content lines per map**, the
    product owner's own analysis to about 30, and part of the length was pure
    repetition. Five rules leave that unwritten. Story 4.5 changed what rule
    3 is and added a sixth field (:attr:`anomaly_min_matches`); both are
    written out below where they depart from the other four.

    **The measured rationales and the numbers are in ``settings.toml``**, not
    here. They are measurement results that the person adjusting a value
    reads while adjusting it, and written into three places two of the copies
    go stale silently. Here there is only what is the code's contract.

    **Why a section of its own and not ``[aggregate]``.** The split follows
    the stage that **reads** the values (AD-3), and ``render`` reads these.
    ``[aggregate]`` goes whole into the ``aggregate`` stage's parameter hash,
    so a presentation choice written there would invalidate every
    aggregation -- that is, switching a presentation setting would force
    ``report.json`` to be computed again although its content does not
    change. A section of its own makes the promise "pruning concerns the
    presentation and not the content" structural.

    **Every rule is a setting of its own**, so that any one of them can be
    switched off without a code change -- and so that with all of them
    switched off the report is character for character the same as before
    this story. The defaults are ``settings.toml``'s defaults: a code default
    that differed from the file would prune silently in a different way
    whenever the key has been forgotten from the file. **Two fields are the
    exception since Story 4.5**, :attr:`skip_sample_seconds` and
    :attr:`anomaly_min_matches`: the file carries a product decision for each
    (the dense grid kept off the page, the match rule), and their code
    defaults are "off", so a class built without the file decides nothing.

    **Pruning does not change a single number** -- neither in
    ``report.json`` nor in the report. In particular it does not change the
    bookkeeping of a block's pattern filtering: the row is always built first
    and pruned only afterwards (:mod:`pappascout.render.view`).

    **Rule 3 is the exception to that last sentence since Story 4.5**, and
    deliberately: its points are the analysis's dense grid, not report
    content, so they are **not built at all** -- they take no part in a
    block's "harvinaisempaa" count and cannot come back through the
    empty-block return. ``report.json`` still holds them. The pistol routes,
    which ``aggregate`` builds, read every sample point since Story 4.13 and
    compress each path to junctions, so no setting of this section reaches
    them either.

    **Some round types are protected from every rule but rule 3**, and the
    list is owned by :data:`pappascout.render.view.PROTECTED_ROUND_TYPES`,
    because it is a presentation choice and not an adjustable value. Rule 3
    applies to them too because it no longer drops a row the report could
    have written: it keeps the grid off the page. :attr:`anomaly_min_matches`
    reads anomaly rows, not round-type blocks, so the protection does not
    concern it.

    **A requirement for a new field: a false value means "rule off."**
    ``False``, an empty list and ``0`` all mean "does not prune", and
    rendering works out from that mechanically whether pruning is involved at
    all -- that decides whether the summary's pruning line exists, that is,
    whether the report is character for character the same as before this
    story. A field whose zero was meant to mean something else would break
    that without anything failing.
    """

    #: Rule 1: leave a saturated equipment row unwritten.
    #:
    #: Saturated = the distribution has **one bar**, its value is
    #: :data:`PLAYERS_ON_SERVER` and the observation was got from every
    #: round.
    drop_saturated_equipment_lines: bool = True

    #: Rule 2: write the armed row and the armoured row as one when the
    #: distributions are identical (bars, divisor and note).
    merge_equal_equipment_lines: bool = True

    #: Rule 3: the time sample points that are **not written** into the
    #: report.
    #:
    #: **What it is for since Story 4.5**: ``[parse].snapshot_seconds`` is a
    #: dense internal series, and this list holds the points that were added
    #: to it, so the report keeps printing exactly the four sample-point rows
    #: it printed before. The rule is therefore no longer "off by default" --
    #: it is what keeps the report's length independent of the grid's
    #: density. The numbers and the measurement are in ``settings.toml``.
    #:
    #: The one sample point that is **not** on the list although it was a
    #: candidate is 45 s, and that is its own measurement: it is thin and
    #: skewed (about half a team on four rounds in five) but **not
    #: repetition** like rules 1, 2, 4 and 5, so leaving it out would cost
    #: content.
    #:
    #: **Not the same thing as** ``[parse].snapshot_seconds``: the sample
    #: point stays in the table and in ``report.json``, the question is only
    #: whether it is printed. The seconds are matched in
    #: :func:`~pappascout.constants.seconds_label`'s format, that is, as they
    #: appear in the row's label, so ``45`` and ``45.0`` mean the same row.
    #:
    #: **The list is sorted at load time**, because it goes into the
    #: ``render`` stage's parameter hash: changing the order produces a
    #: report that is character for character the same, so an unsorted list
    #: would give two identical reports different parameter hashes.
    skip_sample_seconds: list[float] = Field(default_factory=list)

    #: The CT advance and the crunch are **printed** only when their row's
    #: rounds come from at least this many matches (Story 4.5). The product
    #: owner, 2026-09-25: a single brief visit is usually a reaction to a
    #: sound or a kill, *"mutta jos jokin tällainen toistuu ottelusta
    #: toiseen se tulee nostaa esiin"* -- so the unit is matches, not rounds
    #: and not players (:attr:`~pappascout.domain.report.Anomaly.matches`).
    #:
    #: Like every rule in this section it decides what is printed and not
    #: what is in ``report.json``. The crunch rows left out are counted in
    #: the anomaly chapter; the advances, which are printed as habits in each
    #: map chapter's Huomioitavaa block, are counted there, per map. The stack
    #: is not read by it: its rows stand on the product owner's blind
    #: judgements of single rounds (Story 4.4).
    #:
    #: **``0`` is off, and it is the only off.** ``1`` is refused at load:
    #: every row has at least one match, so ``1`` would print everything
    #: while the summary named a rule as set -- two meanings for one effect.
    #: The shipped value and its measurement are in ``settings.toml``.
    anomaly_min_matches: NonNegativeInt = 0

    @field_validator("anomaly_min_matches")
    @classmethod
    def _check_anomaly_min_matches(cls, value: int) -> int:
        """``0`` (off) or at least two: ``1`` is the rule off by another
        name, and the summary would state it as on."""
        if value == 1:
            raise ValueError(
                "anomaly_min_matches is 1, which every row meets, so the rule "
                "would print everything while the report's summary named it "
                "as set. Use 0 to switch it off, or 2 or more to require "
                "recurrence across matches."
            )
        return value

    #: Rule 4: at most this many **targets** on utility's target row.
    #:
    #: A target is an explosion area and not a claim: the same target can
    #: appear on the row more than once (a different throwing area or a
    #: different time bucket), and the limit applies to targets -- otherwise
    #: two retained claims could be the same target in two buckets, and the
    #: row would lose every other target while the note calls them targets.
    #:
    #: ``0`` means "no limit", that is, the rule off: emptying the row is not
    #: pruning but suppression, and that is scoped out of this story.
    max_utility_targets: NonNegativeInt = 2

    #: Rule 5: at most this many areas on the kill row. ``0`` = no limit.
    #:
    #: Three and not two, because the kill row is a whole round type's kills
    #: on one row and its distribution is wider.
    max_kill_areas: NonNegativeInt = 3

    @field_validator("skip_sample_seconds")
    @classmethod
    def _check_skip_sample_seconds(cls, value: list[float]) -> list[float]:
        """Check the sample point list and **sort it** at load time.

        Three ways to break silently, and all three would end in the same
        place: the setting would look as though it removed a row while
        removing nothing.

        * **NaN or infinity** matches no sample point at all.
        * **A negative or zero value** is not a sample point: ``t_s`` is
          measured forward from the end of freezetime, and
          ``[parse].snapshot_seconds`` requires a positive value.
        * **Two values that look the same in the row's label**
          (``45.0000001``) would mean the same row twice. The check is made
          with :func:`~pappascout.constants.seconds_label`, that is, with the
          same function as the matching; with two formattings they would
          agree today only.

        The sorting is **because of the parameter hash** and not tidiness:
        ``render`` hashes its section whole, and ``[45, 15]`` produces a
        report that is character for character the same as ``[15, 45]``.
        Unsorted, the manifest would claim that two identical reports came
        from different parameters.

        A sample point that is not in ``[parse].snapshot_seconds`` is **not
        rejected**: the sections do not see each other (AD-3), and the report
        is also typeset from old ``report.json`` files, in which the sample
        points are the ones that were in use at the time of parsing.
        """
        labels: list[str] = []
        for seconds in value:
            if not isfinite(seconds):
                raise ValueError(
                    f"skip_sample_seconds holds the value {seconds!r}, which "
                    "is not a finite number, and it cannot match any sample "
                    "point."
                )
            if seconds <= 0:
                raise ValueError(
                    f"skip_sample_seconds holds the value "
                    f"{seconds_label(seconds)}, which is not positive. "
                    "Sample points are measured forward from the end of "
                    "freezetime, so zero or a negative value is not a sample "
                    "point."
                )
            labels.append(seconds_label(seconds))
        if len(labels) != len(set(labels)):
            raise ValueError(
                "skip_sample_seconds holds the same sample point twice "
                f"({', '.join(labels)}). The seconds are matched in the form "
                "they take in the row's label, so two values close together "
                "would mean the same row."
            )
        return sorted(value)


class EconomySettings(_Section):
    """``[economy]`` -- CS2's economy model.

    One of these takes part in classifying a round: ``loss_bonus_steps``. A
    half-buy is told from a force by whether a player can make a normal buy
    on the next round, and that depends on the loss bonus -- which is
    directly a function of the loss count and varies between $1,400 and
    $3,400. That is why ``classify`` is given this section and its content is
    part of that stage's parameter hash (Story 1.10). The other values
    explain in the report why the team had the money it had.
    """

    start_money: PositiveInt = 800
    max_money: PositiveInt = 16000
    loss_bonus_steps: list[PositiveInt] = Field(min_length=1)
    win_reward_elimination: PositiveInt = 3250
    win_reward_bomb: PositiveInt = 3500
    plant_bonus_loss: PositiveInt = 600
    plant_reward: PositiveInt = 300
    defuse_reward: PositiveInt = 300
    ct_kill_bonus: NonNegativeInt = 50
    short_handed_bonus: NonNegativeInt = 1000
    #: The kill reward by weapon class; exceptions one weapon at a time.
    kill_rewards: dict[str, int] = Field(default_factory=dict)
    #: The buy menu's prices. Used to tell half-buys apart in the report.
    prices: dict[str, int] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_loss_bonus_is_ascending(self) -> "EconomySettings":
        steps = self.loss_bonus_steps
        if any(a >= b for a, b in zip(steps, steps[1:])):
            raise ValueError(
                f"loss_bonus_steps is not ascending: {steps}. The steps have "
                "to grow, because the counter grows with losses."
            )
        if self.start_money > self.max_money:
            raise ValueError(
                f"start_money ({self.start_money}) is greater than max_money "
                f"({self.max_money})."
            )
        return self


class FaceitSettings(_Section):
    """``[faceit]`` -- the transport's settings: retry, timeout, page.

    Story 3.1. The section exists because **thresholds are settings and not
    code** (AD-3). Every value here is a number whose right size depends on
    the network and on the interface's load -- not on pappascout's logic --
    and adjusting it must not require a code change.

    **The section holds neither an address nor a credential.** The
    interface's address is not an adjustable value but what the client is
    written against; it is a constant in :mod:`pappascout.adapters.faceit`
    that a test may replace with a parameter. The credentials are not in
    ``settings.toml`` at all -- they are read from the machine's own ``.env``
    file, and that is the whole reason
    :meth:`Settings.require_faceit_api_key` exists.

    **The section is in no stage's parameter hash**, and that is a decision.
    Adjusting a retry or a timeout does not change a single answer the
    interface gives, so it must not invalidate anything in the archive. The
    same kinship as ``[report]`` has: the split follows what the value does.

    Attributes:
        retry_attempts: The **total number** of attempts, not the number of
            retries: ``1`` means "try once and not again". Applies only to
            429 and 5xx responses and to connection errors; any other 4xx
            does not heal by waiting and is not retried even once.
        retry_initial_delay_seconds: The first wait before the second
            attempt. The delay doubles on every round.
        retry_max_delay_seconds: The ceiling on one wait. Without a ceiling
            the eighth attempt would be more than two minutes away, and
            adjusting the initial delay would move it exponentially.
        timeout_seconds: The timeout on one HTTP call. A timeout is a
            connection error, that is, it **is retried** -- it is the
            commonest transient fault.
        page_size: How many rows are asked for in one page. Adjustable by the
            maintainer, because the right value depends on the size of the
            response, but at most :data:`MAX_FACEIT_PAGE_SIZE`.
        retry_jitter_share: The jitter's share of the wait (0.0-1.0). The
            wait is ``delay * (1 + this * a random number)``. Without jitter
            two parallel runs would hit the rate limit in the same second
            again and again -- the exponential delay is the same function of
            the same moment for both. ``0.0`` = no jitter.
        call_budget_seconds: **The time budget in seconds for one port
            call**: the whole pagination and all the retries together.
            ``retry_attempts`` is not a ceiling on time, because pagination
            multiplies the attempts by the number of pages -- an attempt
            ceiling alone would allow hundreds of requests and an hour of
            silence from one ``get_matches``, which is precisely what
            :data:`MAX_FACEIT_RETRY_ATTEMPTS` says it prevents. So the
            ceiling has to be in seconds, because seconds are what the user
            waits.
    """

    retry_attempts: Annotated[int, Field(ge=1, le=MAX_FACEIT_RETRY_ATTEMPTS)] = 4
    retry_initial_delay_seconds: Annotated[
        float, Field(gt=0.0, allow_inf_nan=False)
    ] = 1.0
    retry_max_delay_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 30.0
    timeout_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 30.0
    page_size: Annotated[int, Field(ge=1, le=MAX_FACEIT_PAGE_SIZE)] = 100
    retry_jitter_share: Annotated[
        float, Field(ge=0.0, le=1.0, allow_inf_nan=False)
    ] = 0.25
    call_budget_seconds: Annotated[float, Field(gt=0.0, allow_inf_nan=False)] = 300.0

    @model_validator(mode="after")
    def _check_delays_agree(self) -> "FaceitSettings":
        if self.retry_max_delay_seconds < self.retry_initial_delay_seconds:
            raise ValueError(
                f"retry_max_delay_seconds ({self.retry_max_delay_seconds:g} s) "
                f"is smaller than retry_initial_delay_seconds "
                f"({self.retry_initial_delay_seconds:g} s), so the ceiling "
                "would cut the very first wait and the initial delay would "
                "mean nothing."
            )
        # The budget is the ceiling on the whole call, so a budget smaller
        # than one wait or one timeout would stop the fetch before anything
        # can happen -- a retry would look configured but would never take
        # place.
        if self.call_budget_seconds < self.retry_max_delay_seconds:
            raise ValueError(
                f"call_budget_seconds ({self.call_budget_seconds:g} s) is "
                f"smaller than retry_max_delay_seconds "
                f"({self.retry_max_delay_seconds:g} s), so the budget would "
                "run out in the middle of a single wait and not one retry "
                "could take place."
            )
        if self.call_budget_seconds < self.timeout_seconds:
            raise ValueError(
                f"call_budget_seconds ({self.call_budget_seconds:g} s) is "
                f"smaller than timeout_seconds ({self.timeout_seconds:g} s), "
                "so the budget would run out before any call has time to time "
                "out."
            )
        return self


class Settings(BaseSettings):
    """The whole set of settings: eight sections and the machine's own
    credentials.

    A stage is not given this object but one section at a time (AD-3).
    """

    # extra="ignore": the machine's .env may hold more than these credentials
    # without the load breaking. Unknown sections in the settings file are
    # checked separately in load_settings, so that a typing error does not go
    # through silently.
    model_config = SettingsConfigDict(
        extra="ignore",
        frozen=True,
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    project: ProjectSettings
    league: LeagueSettings
    parse: ParseSettings
    thresholds: ThresholdSettings
    aggregate: AggregateSettings
    report: ReportSettings
    economy: EconomySettings
    faceit: FaceitSettings

    faceit_api_key: SecretStr | None = None
    faceit_downloads_token: SecretStr | None = None

    #: Where the credentials were read from -- for error messages and the
    #: ``info`` command only.
    secrets_file: Path | None = None
    #: Where the settings file was read from.
    settings_file: Path | None = None

    @model_validator(mode="after")
    def _check_sections_agree(self) -> "Settings":
        """Catch contradictions between the sections at load time.

        These live in different sections but describe the same thing: the
        match format. If they differ, classification would quietly produce
        the wrong round types right across the archive.
        """
        expected_regulation = 2 * self.league.mr
        if self.thresholds.regulation_rounds != expected_regulation:
            raise ValueError(
                f"thresholds.regulation_rounds ({self.thresholds.regulation_rounds}) "
                f"does not match the league format MR{self.league.mr}, which "
                f"means {expected_regulation} regulation rounds."
            )
        expected_pistols = [1, self.league.mr + 1]
        if self.thresholds.pistol_rounds != expected_pistols:
            raise ValueError(
                f"thresholds.pistol_rounds ({self.thresholds.pistol_rounds}) "
                f"does not match the league format MR{self.league.mr}: the "
                f"pistol rounds are {expected_pistols} (round 1 and the first "
                "round of the second half)."
            )
        expected_steps = self.thresholds.loss_count_max + 1
        if len(self.economy.loss_bonus_steps) != expected_steps:
            raise ValueError(
                f"economy.loss_bonus_steps holds "
                f"{len(self.economy.loss_bonus_steps)} steps, but the loss "
                f"count varies between {self.thresholds.loss_count_min} and "
                f"{self.thresholds.loss_count_max}, so {expected_steps} steps "
                "are needed. The counter indexes this list directly."
            )
        # The half-buy's condition B compares the sum "own balance + loss
        # bonus" against normal_buy_money_min, and the sum is clipped to the
        # money ceiling. Both of those bounds are in the [economy] section,
        # so reachability cannot be checked inside either section alone --
        # and without the check either class would vanish silently.
        # The anomaly time bound selects among the SAMPLE POINTS, which
        # [parse] decides. A bound smaller than the earliest sample point
        # silences both orientation rules permanently -- and the report would
        # then claim "no anomalies" as an observation although not one sample
        # point was ever examined. Neither section can check this alone, so
        # the check is here.
        #
        # The stack is NOT among them since Story 4.4: it reads one sample
        # point of its own (stack_sample_s) and this bound does not touch it.
        earliest_sample = min(self.parse.snapshot_seconds)
        if self.thresholds.advance_max_sample_s < earliest_sample:
            raise ValueError(
                f"thresholds.advance_max_sample_s "
                f"({self.thresholds.advance_max_sample_s:g} s) is smaller "
                f"than the earliest parse.snapshot_seconds "
                f"({earliest_sample:g} s), so not one sample point fits "
                "inside the orientation rules' time bound.\n"
                "The CT advance and the crunch would fall silent permanently, "
                "and the report's anomaly count would claim 'no anomalies' as "
                "an observation -- although nothing was examined."
            )
        # The stack reads ONE sample point, so for it the hazard is not a
        # bound that is too small but a value that names no sample point at
        # all: 16.0 against snapshot_seconds [6, 15, 30, 45] selects no row,
        # the rule finds nothing, and the coverage still reports every CT
        # round as scanned -- "no stacks" read out as a measured negative over
        # a blind spot. It is the same harm as above and it needs the same
        # guard; the tolerance is the shared one, so the settings and the rule
        # cannot disagree about what "is this sample point" means.
        #
        # The check is here and not in either section for the same reason as
        # the one above: [thresholds] does not know the sample points and
        # [parse] does not know the rule.
        if not any(
            is_sample_point(value, self.thresholds.stack_sample_s)
            for value in self.parse.snapshot_seconds
        ):
            points = ", ".join(
                f"{value:g}" for value in self.parse.snapshot_seconds
            )
            raise ValueError(
                f"thresholds.stack_sample_s "
                f"({self.thresholds.stack_sample_s:g} s) is not one of the "
                f"parse.snapshot_seconds ({points}), so the stack rule would "
                "read no row at all.\n"
                "The rule reads exactly this one sample point, so it would "
                "find nothing on every round -- and the coverage would still "
                "report every CT round as scanned, which turns a blind spot "
                "into 'no stacks' as an observation."
            )
        # EVERY TIME THRESHOLD MUST PROVE THE RULE IT GOVERNS CAN FIRE AT ALL
        # (Story 4.6, review round 1). The two guards above each prove it for
        # one rule -- the advance needs a sample point inside its bound, the
        # stack needs its point to exist -- and the crunch needs more than
        # either: a target inside the bound **and** an earlier point the
        # look-back actually reaches. Three settings decide that between them
        # and no section can see it alone, so the proof is here.
        #
        # It is a proof and not an approximation: the same selector the rule
        # uses (constants.source_point_index) is run over the configured grid.
        # A bound of its own shape would be a second copy of the rule's reach
        # and would agree with it only today. Measured before this was
        # written, against the real settings.toml: advance_max_sample_s = 10
        # was ACCEPTED although only one point fits inside it, and
        # crunch_lookback_s = 1e-9 was ACCEPTED although the cutoff then
        # reaches the target's own point, so every arrival is discarded as
        # "already there". Both print "crunchin kattavuus 93/93 CT-kierroksesta"
        # with no rows -- a measured negative over a blind spot, which is the
        # harm this whole guard family exists to prevent.
        #
        # The source may be ANY earlier point and not only one inside the
        # bound (the rule reads rows past it), so the targets are filtered and
        # the sources are not.
        points = sorted(self.parse.snapshot_seconds)
        targets = [
            value
            for value in points
            if value <= self.thresholds.advance_max_sample_s
        ]
        reachable = [
            value
            for value in targets
            if (
                index := source_point_index(
                    points, value, self.thresholds.crunch_lookback_s
                )
            )
            is not None
            and points[index] < value
        ]
        if not reachable:
            named = ", ".join(f"{value:g}" for value in points)
            raise ValueError(
                f"The crunch can fire on no sample point at all.\n"
                f"parse.snapshot_seconds is ({named}) s, "
                f"thresholds.advance_max_sample_s is "
                f"{self.thresholds.advance_max_sample_s:g} s and "
                f"thresholds.crunch_lookback_s is "
                f"{self.thresholds.crunch_lookback_s:g} s. The rule reads a "
                "player's source area from the latest sample point at or "
                "before t - crunch_lookback_s, so it needs a point inside the "
                "bound that has an earlier point that far back -- and with "
                "these three values not one point does.\n"
                "The rule would fall silent permanently and the report would "
                "still count every CT round as scanned, which turns a blind "
                "spot into 'no crunches' as an observation. Widen "
                "advance_max_sample_s, shorten crunch_lookback_s, or add a "
                "sample point."
            )
        smallest_bonus = min(self.economy.loss_bonus_steps)
        if self.thresholds.normal_buy_money_min <= smallest_bonus:
            raise ValueError(
                f"thresholds.normal_buy_money_min "
                f"({self.thresholds.normal_buy_money_min}) is at most the "
                f"smallest loss bonus ({smallest_bonus}), so every player "
                "would pass condition B without a cent of their own money "
                "and no purchase after a loss could be a force any more."
            )
        if self.thresholds.normal_buy_money_min > self.economy.max_money:
            raise ValueError(
                f"thresholds.normal_buy_money_min "
                f"({self.thresholds.normal_buy_money_min}) exceeds the money "
                f"ceiling economy.max_money ({self.economy.max_money}), so no "
                "player can ever meet condition B and a half-buy cannot be "
                "reached."
            )
        return self

    def require_faceit_api_key(self) -> str:
        """Return the FACEIT Data API credential, or say how it is set."""
        return self._require_secret(self.faceit_api_key, "FACEIT_API_KEY")

    def require_faceit_downloads_token(self) -> str:
        """Return the FACEIT Downloads token, or say how it is set."""
        return self._require_secret(
            self.faceit_downloads_token, "FACEIT_DOWNLOADS_TOKEN"
        )

    def _require_secret(self, value: SecretStr | None, name: str) -> str:
        if value is not None and value.get_secret_value().strip():
            return value.get_secret_value()
        path = self.secrets_file or secrets_env_path()
        raise SettingsError(
            f"The credential {name} was not found.\n"
            f"Add this line to the file {path}:\n"
            f"    {name}=<your own credential>\n"
            "The file is the machine's own and it is not in a synchronised "
            "folder or in version control."
        )

    def secret_status(self, name: str) -> str:
        """Return the credential's status as a word -- ``set`` or ``missing``.

        Never returns the credential itself.
        """
        value = getattr(self, name.lower(), None)
        if isinstance(value, SecretStr) and value.get_secret_value().strip():
            return "set"
        return "missing"


def secrets_env_path() -> Path:
    """The machine's own credential file ``%USERPROFILE%\\.pappascout\\.env``.

    Deliberately outside the synchronised folder: a sync client would make
    conflict copies of the file on two machines and would keep a rotated
    credential in its own version history.
    """
    return Path.home() / ".pappascout" / ".env"


def project_env_path(start: Path | None = None) -> Path:
    """The project's own ``.env``, which is used only as a fallback."""
    return (start or Path.cwd()) / ".env"


def _repo_root() -> Path:
    """The repository root worked out from the package's location (src
    layout).
    """
    return Path(__file__).resolve().parents[3]


def settings_search_paths(start: Path | None = None) -> list[Path]:
    """The paths ``settings.toml`` is looked for in, in order of priority."""
    paths: list[Path] = []
    from_env = os.environ.get(SETTINGS_ENV_VAR)
    if from_env:
        paths.append(Path(from_env))

    cwd = (start or Path.cwd()).resolve()
    for directory in [cwd, *cwd.parents]:
        paths.append(directory / SETTINGS_FILENAME)

    paths.append(_repo_root() / SETTINGS_FILENAME)

    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def find_settings_file(start: Path | None = None) -> Path:
    """Find ``settings.toml``, or say where it was looked for.

    If the environment variable is set, it is an order and not a suggestion:
    a missing file is an error, not a reason to fall back to the working
    directory. A silent fallback would read settings other than the ones the
    user asked for.
    """
    from_env = os.environ.get(SETTINGS_ENV_VAR)
    if from_env:
        requested = Path(from_env)
        if not requested.is_file():
            raise SettingsError(
                f"The environment variable {SETTINGS_ENV_VAR} points at the "
                f"file {requested}, which does not exist.\n"
                "Fix the path or remove the variable, and settings.toml will "
                "be looked for in the working directory."
            )
        return requested

    candidates = settings_search_paths(start)
    for path in candidates:
        if path.is_file():
            return path
    listing = "\n".join(f"    {path}" for path in candidates)
    raise SettingsError(
        "The settings file settings.toml was not found.\n"
        "These are the paths that were searched:\n"
        f"{listing}\n"
        "Move to the project root or set the environment variable "
        f"{SETTINGS_ENV_VAR} to point at the file."
    )


def load_settings(
    settings_file: Path | None = None,
    env_files: tuple[Path, ...] | None = None,
) -> Settings:
    """Load the settings from a TOML file and the credentials from ``.env``
    files.

    Args:
        settings_file: The settings file's path. By default it is looked for
            in :func:`settings_search_paths` order.
        env_files: The credential files from weakest to strongest. By default
            the project's ``.env`` first and the machine's own ``.env`` last,
            so that the machine's own wins.

    Returns:
        A validated :class:`Settings`.

    Raises:
        SettingsError: If the file is not found, is not valid TOML or some
            value is not valid. The message always says what has to be fixed.
    """
    path = Path(settings_file) if settings_file is not None else find_settings_file()
    if not path.is_file():
        raise SettingsError(
            f"The settings file was not found at the path {path}.\n"
            "Create the file or give the right path."
        )

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            f"The settings file {path} is not valid TOML: {exc}\n"
            "Fix the syntax and run the command again."
        ) from exc
    except OSError as exc:
        raise SettingsError(
            f"The settings file {path} could not be read: {exc}"
        ) from exc

    for (section, key), advice in REMOVED_SETTINGS.items():
        values = data.get(section)
        if isinstance(values, dict) and key in values:
            raise SettingsError(
                f"The settings file {path} holds the key [{section}].{key}, "
                "which no longer exists.\n"
                f"{advice}"
            )

    # A missing section, caught before pydantic. ``report: Field required``
    # is true but guides the reader nowhere: it does not say that the section
    # is gone altogether, nor where to get it. A section being required is a
    # decision and not a shortcoming -- every setting is written out, because
    # a default that lives only in the code is not visible in the file being
    # adjusted -- so what has to be fixed is the message.
    #
    # The same kinship as :data:`REMOVED_SETTINGS` has, which covers the
    # opposite case (a setting that no longer exists). An archive shared by
    # two machines makes both of them ordinary situations: the repository's
    # ``settings.toml`` updates from git, and a pull that was missed looks
    # exactly like this.
    missing = sorted(name for name in SETTINGS_SECTIONS if name not in data)
    if missing:
        raise SettingsError(
            f"The settings file {path} is missing a section: "
            + ", ".join(f"[{name}]" for name in missing)
            + ".\n"
            "Every section is required, because every setting is written "
            "out: a default in the code is not visible in the file being "
            "adjusted.\n"
            "Copy the missing section from the repository's own "
            "settings.toml -- it can be shown with the command: "
            "git show HEAD:settings.toml"
        )

    unknown = sorted(set(data) - SETTINGS_SECTIONS)
    if unknown:
        raise SettingsError(
            f"The settings file {path} holds an unknown section or key: "
            f"{', '.join(unknown)}.\n"
            f"The allowed sections are {', '.join(sorted(SETTINGS_SECTIONS))}.\n"
            "Credentials are not written into this file but into "
            f"{secrets_env_path()}."
        )

    if env_files is None:
        env_files = (project_env_path(path.parent), secrets_env_path())
    # pydantic-settings: the last file in the list wins.
    existing = [str(p) for p in env_files if Path(p).is_file()]
    secrets_file = Path(existing[-1]) if existing else None

    try:
        return Settings(
            _env_file=existing or None,
            settings_file=path,
            secrets_file=secrets_file,
            **data,
        )
    except _ValidationError as exc:
        raise SettingsError(
            f"The settings file {path} is not valid:\n"
            f"{_format_validation_error(exc)}\n"
            "Fix the values and run the command again."
        ) from exc


def _format_validation_error(exc: _ValidationError) -> str:
    """Format pydantic's errors as a short list."""
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"    {location}: {error['msg']}")
    return "\n".join(lines)


# -- The product owner's callout table (Story 4.13, AD-13) ---------------------

#: The callout table: a static file **shipped with the code**, not in the
#: archive and not in the machine's settings (AD-13). It sits beside the
#: package's modules rather than beside ``settings.toml`` because it is not a
#: setting anybody adjusts per machine -- it is the product owner's knowledge
#: of the maps, and every entry in it names the document it came from.
CALLOUT_TABLE_PATH = Path(__file__).resolve().parent.parent / "callouts.toml"

#: How sure an entry is, **as its source says** -- ``callouts.toml``'s header
#: defines each value. ``stated``, ``guide`` and ``measured`` are certain and
#: print unmarked; ``inferred`` and ``guess`` print marked; ``unnamed`` is the
#: one value an entry with no callout takes, because there is no mapping to
#: be sure of.
#:
#: **A name match is certain only while the coordinates do not contradict
#: it** (Story 4.16). ``guide`` rests on the game area's name equalling a
#: guide callout, and once, on real data, that was not the place: de_ancient's
#: ``Ruins`` lies on the guide's B DOORS, not its RUINS
#: (``koordinaatit-mitattu-2026-09-27.md``). A new ``guide`` entry is certain
#: on the name alone until the coordinates check it; the entries checked on
#: 2026-09-27 are those in ``tests/data/guide_fit.json`` or in section 1 of
#: that document.
#:
#: **``measured`` confirms and never names.** The coordinates confirm a
#: callout another source already proposed -- his words, his guess or a
#: guide name: the area's projected ticks meet that callout's label box in
#: ``tests/data/guide_fit.json``. The boxes are read by hand from the guide
#: image; the fit and the check run on the archive (``-m archive``). A
#: callout only the coordinates propose stays ``inferred``, which is why
#: ``Ruins`` = *b doors* is. A measured area may spill onto guide labels that
#: are not his callouts (``TSideUpper`` touches CAT, ``ExtendedA`` reaches
#: NINJA); it is coarse only when it covers several of **his** callouts.
CalloutConfidence = Literal[
    "stated", "guide", "measured", "inferred", "guess", "unnamed"
]

#: The confidence of a place that has a callout: every value but
#: ``unnamed``, **derived** from :data:`CalloutConfidence` rather than
#: written twice, so a value added there reaches a split's parts too
#: (Story 4.17).
NamedConfidence = Literal[  # type: ignore[valid-type]
    tuple(value for value in get_args(CalloutConfidence) if value != "unnamed")
]

#: The confidence values the route prints without a mark.
CERTAIN_CONFIDENCE: frozenset[str] = frozenset({"stated", "guide", "measured"})


def _normal_callout(value: str) -> str:
    """A callout as it is compared: case and surrounding or doubled spaces
    ignored (:class:`CalloutEntry`)."""
    normal = " ".join(value.split()).lower()
    if not normal:
        raise ValueError("a callout cannot be blank")
    return normal


class GuideFit(_Section):
    """The projection of a game position onto a guide image (Story 4.16):
    ``px = sx * x + bx`` and ``py = -sy * y + by``, in image pixels."""

    sx: float
    sy: float
    bx: float
    by: float


#: A half-cell of a guide grid as he names it: the column's letter, the
#: row's number, and the quarter -- a top-left, b top-right, c bottom-left,
#: d bottom-right (``nuke-piha-vastaus-2026-09-27.md``).
_HALF_CELL = re.compile(r"([A-Z])([1-9][0-9]*)([abcd])")


#: A height band: a position at ``z`` is inside when ``lo <= z < hi``,
#: half-open like a half-cell. ``-inf`` / ``inf`` leave a side open.
ZBand = tuple[float, float]


def _z_inside(band: ZBand | None, z: float) -> bool:
    return band is None or band[0] <= z < band[1]


#: A pixel rectangle ``(x0, y0, x1, y1)``, half-open like a half-cell.
_Rect = tuple[float, float, float, float]


def _rects_meet(one: _Rect, two: _Rect) -> bool:
    """Whether two rectangles share area (touching edges share none)."""
    return (
        one[0] < two[2] and two[0] < one[2] and one[1] < two[3] and two[1] < one[3]
    )


def _bands_meet(one: ZBand | None, two: ZBand | None) -> bool:
    open_band = (-float("inf"), float("inf"))
    low_one, high_one = one if one is not None else open_band
    low_two, high_two = two if two is not None else open_band
    return low_one < high_two and low_two < high_one


class CellRegion(_Section):
    """A place finer than a half-cell (Story 4.20), in his own terms: a
    **fraction** of one named half-cell, or a **crossing** -- the point where
    named half-cells meet -- and either with an optional **height band**.

    * ``cell`` with ``x`` and ``y``: ranges in [0, 1] of that half-cell, x
      from its left and y from its **top** (pixel y grows downward), e.g.
      the top fifth is ``y = [0, 0.2]``. A range left out is the whole.
    * ``crossing``: the half-cells whose corners meet at one point, as a
      box centred on it ``size`` half-cells wide and high. The size is
      **required**, with no default: the precedents -- Nuke's postimerkki
      and ct box, Ancient's dig -- are boxes of one half-cell (21.5 x 21.1
      px on a 21.5 x 21.1 px half-cell), and his short boost is one half-cell
      in his own words (*"noin yhden ruudun kokoinen"*), so each crossing
      says its size where it is written.
    * ``z``: the region holds a position only inside this band. A band is
      measured from the positions of the place, never guessed (spec 4.20
      decision 2), and its part's source cites the measurement.

    ``words`` is his phrase the region is read from, quoted from the part's
    source, so a test can hold the geometry to the words. It is provenance:
    it moves no position.
    """

    cell: str | None = None
    crossing: list[str] | None = None
    x: tuple[float, float] = (0.0, 1.0)
    y: tuple[float, float] = (0.0, 1.0)
    size: float | None = None
    z: ZBand | None = None
    words: str = Field(min_length=1)

    @model_validator(mode="after")
    def _check_the_region(self) -> "CellRegion":
        if (self.cell is None) == (self.crossing is None):
            raise ValueError(
                "a region is either a fraction of one half-cell (cell) or a "
                "crossing of half-cells (crossing), and exactly one of them."
            )
        if self.crossing is not None:
            if len(self.crossing) < 2 or len(set(self.crossing)) != len(
                self.crossing
            ):
                raise ValueError(
                    f"the crossing {self.crossing} must name at least two "
                    "distinct half-cells that meet."
                )
            if (self.x, self.y) != ((0.0, 1.0), (0.0, 1.0)):
                raise ValueError(
                    "x and y are fractions of one half-cell; a crossing has "
                    "a size instead."
                )
            if self.size is None:
                raise ValueError(
                    f"the crossing {self.crossing} has no size; a crossing box "
                    "says how many half-cells wide it is (one for postimerkki, "
                    "ct box and dig), and there is no default."
                )
            if not 0 < self.size <= 2:
                raise ValueError(
                    f"a crossing box of {self.size} half-cells is not in (0, 2]."
                )
        elif self.size is not None:
            raise ValueError("size belongs to a crossing, not to a fraction.")
        for name, (low, high) in (("x", self.x), ("y", self.y)):
            if not 0 <= low < high <= 1:
                raise ValueError(
                    f"{name} = [{low}, {high}] is not a range inside [0, 1] "
                    "with the low end first."
                )
        if self.z is not None and not self.z[0] < self.z[1]:
            raise ValueError(
                f"the height band {list(self.z)} is not [lo, hi] with lo < hi."
            )
        return self


class CellPart(_Section):
    """One of his callouts inside a split game area (Story 4.17): the
    half-cells of the guide grid it holds, and the small pixel boxes of a
    place smaller than a half-cell, which override the half-cells beneath
    them. Since Story 4.20 also :attr:`regions` finer than a half-cell, and
    :attr:`broad`.

    It carries what a :class:`CalloutEntry` carries for a whole area --
    his name, whether it is a junction and on whose words, how sure the name
    is, and its source -- because it is a place of the report exactly as an
    area is. It is never coarse: it is the split's answer.
    """

    callout: str = Field(min_length=1)
    cells: list[str] = Field(default_factory=list)
    #: ``[x0, y0, x1, y1]`` in image pixels, half-open like a half-cell.
    boxes: list[tuple[float, float, float, float]] = Field(default_factory=list)
    #: Fractions and crossings of half-cells, each with an optional height
    #: band (:class:`CellRegion`, Story 4.20).
    regions: list[CellRegion] = Field(default_factory=list)
    #: **This part yields to every earlier part it overlaps** (Story 4.20):
    #: it may claim a spot an earlier part claims, and the earlier part wins
    #: there. That covers his broader name over the finer ones inside it
    #: (his *banaani* over the names he gives within), and the place he
    #: gives "minus" another (his *miinus*, *paitsi*). Every other double
    #: claim is refused, and so is a broad part that overlaps no earlier part.
    broad: bool = False
    junction: bool
    junction_source: str | None = Field(default=None, min_length=1)
    confidence: NamedConfidence
    source: str = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)

    @field_validator("callout")
    @classmethod
    def _normalise_the_callout(cls, value: str) -> str:
        return _normal_callout(value)

    @model_validator(mode="after")
    def _check_the_part(self) -> "CellPart":
        if self.junction != (self.junction_source is not None):
            raise ValueError(
                f"{self.callout!r}: junction and junction_source come "
                "together, as on an area's entry."
            )
        if not self.cells and not self.boxes and not self.regions:
            raise ValueError(
                f"{self.callout!r} holds neither a half-cell nor a box nor a "
                "region, so no position could ever be counted under it."
            )
        for box in self.boxes:
            if not (box[0] < box[2] and box[1] < box[3]):
                raise ValueError(
                    f"{self.callout!r}: the box {list(box)} is not "
                    "[x0, y0, x1, y1] with x0 < x1 and y0 < y1."
                )
        return self

    #: A part is never coarse; :func:`~pappascout.domain.aggregate
    #: ._route_place` reads it like an entry.
    coarse: Literal[False] = False

    @property
    def certain(self) -> bool:
        """As :attr:`CalloutEntry.certain`."""
        return self.confidence in CERTAIN_CONFIDENCE

    @property
    def kept(self) -> bool:
        """As :attr:`CalloutEntry.kept`: a part always has a callout, so it
        is kept exactly when it is a junction."""
        return self.junction


class CellSplit(_Section):
    """A game area divided by position into his callouts, on a grid
    over a guide image (Story 4.17): on Nuke the guide's own grid, on Ancient
    a generated grid of 425 game units from pixel (0, 0) (Story 4.18,
    ``grid2.py``). In both cases he named the half-cells against pictures of
    that grid, so the grid here must be the pictures' exactly.

    **The geometry is the pictures'.** A position is projected with
    :attr:`fit` (the fit of Story 4.16, unrounded as the pictures were
    drawn; one value per floor of a map, carried by each split on that
    floor and refused at load if it differs -- ``tests/data/guide_fit.json``
    reads it from here through ``fit_from``), then placed on the grid of
    :attr:`columns` x :attr:`rows` cells whose top-left corner is
    :attr:`origin` and whose size is :attr:`cell`, each cell divided into
    the four half-cells he names (``K13b``).

    **A split is on one floor** (Story 4.24): the height range
    ``[zmin, zmax)`` its image draws. A position outside it -- below
    :attr:`zmin` or at or above :attr:`zmax` -- is not on that floor and is
    not split: it keeps the area's own callout, and so does a position with
    no coordinates. Nuke's guide draws the upper floor (``zmin``, the yard
    and lobby) and, apart, the lower floor (``zmax``, B site and what lies
    around it), each with its own fit. A split whose fit was made with no
    floor cut (Ancient's, Story 4.18) has neither, and every position with
    coordinates is split. An area has one split, so an area that spans two
    floors (Nuke's ``Ramp``) is split on one of them, and its positions on
    the other keep its own callout.

    **A position takes, in order:** the part whose box holds it; else the
    **first part in table order** that names its half-cell or holds it in a
    region (:class:`CellRegion`: a fraction, a crossing, either with a
    height band the position's ``z`` must be inside -- Story 4.20); else
    **the nearest named half-cell's part**, by the distance between
    half-cell centres in image pixels, over whole named half-cells only, a
    tie going to the part and half-cell that come first in the table (his
    words: join the rest to the nearest). **Table order decides, not the
    kind of claim**: a whole half-cell written before a region holds the
    region's spot too. The table is therefore written finer first, and a
    part marked broad -- it yields to every earlier part it overlaps --
    comes after the parts it yields to. The
    third step is a derivation in code and never a hand-filled row:
    :attr:`inherited` lists it for every half-cell no part names whole, and
    a position off the grid is placed by the same rule.

    **On an area that is not coarse** (Story 4.23: Nuke's ``Lobby``) the
    third step does not apply. The area is one of his places whole, and the
    parts are finer places inside it, so a position no part holds keeps the
    area's own callout: :meth:`part_at` returns ``None`` and
    :attr:`inherited` is empty. :class:`CalloutEntry` sets this from its
    ``coarse``; it is not a key of the table.

    **Refused at load:** a spot two parts claim at the same height, unless
    the later part is marked :attr:`~CellPart.broad` (and a broad part that
    overlaps no earlier part), a half-cell outside the grid, a crossing
    with no size or whose half-cells do not meet at one point, two parts with one
    callout, and two boxes that overlap -- each would leave a position with
    two answers, or a flag with no meaning; and a floor whose ``zmin`` is
    not below its ``zmax`` or is not finite. Across the areas of one map,
    :func:`load_callouts` refuses splits on one floor that are on different
    grids (:data:`SPLIT_GRID_FIELDS`), and splits whose floors overlap
    without being one floor (:data:`SPLIT_FLOOR_FIELDS`), so a half-cell's
    name means one place of one image on every split of that floor.
    """

    image: str = Field(min_length=1)
    fit: GuideFit
    zmin: float | None = None
    zmax: float | None = None
    origin: tuple[float, float]
    cell: tuple[float, float]
    columns: str = Field(min_length=1)
    rows: int = Field(ge=1)
    source: str = Field(min_length=1)
    parts: list[CellPart] = Field(min_length=1)

    _named: dict[tuple[int, int], CellPart] = PrivateAttr(default_factory=dict)
    _nearest: dict[tuple[int, int], CellPart] = PrivateAttr(default_factory=dict)
    #: Whether a position no part holds keeps the area's own callout instead
    #: of taking the nearest named half-cell's (Story 4.23): true exactly on
    #: a split of an area that is **not coarse**, and set by
    #: :class:`CalloutEntry` from its ``coarse`` -- derived, so the table
    #: cannot say otherwise.
    _rest_is_area: bool = PrivateAttr(default=False)
    #: Every named half-cell and region as (part, rectangle, band), in table
    #: order: the lookup's second step reads it front to back.
    _claims: list[tuple[CellPart, _Rect, ZBand | None]] = PrivateAttr(
        default_factory=list
    )

    @model_validator(mode="after")
    def _check_the_split(self) -> "CellSplit":
        if self.cell[0] <= 0 or self.cell[1] <= 0:
            raise ValueError("a grid cell has a positive width and height.")
        if not all(isfinite(v) for v in (self.zmin, self.zmax) if v is not None):
            raise ValueError("a floor's zmin and zmax are finite heights.")
        if not self.floor_band[0] < self.floor_band[1]:
            raise ValueError(
                f"the floor [zmin {self.zmin}, zmax {self.zmax}) is empty; "
                "zmin must be below zmax."
            )
        if not re.fullmatch(r"[A-Z]+", self.columns) or len(
            set(self.columns)
        ) != len(self.columns):
            raise ValueError(
                f"columns {self.columns!r} must be distinct capital letters, "
                "one per grid column, so a half-cell name reads one way."
            )
        right = self.origin[0] + self.cell[0] * len(self.columns)
        bottom = self.origin[1] + self.cell[1] * self.rows
        for part in self.parts:
            for box in part.boxes:
                if not (
                    self.origin[0] <= box[0]
                    and box[2] <= right
                    and self.origin[1] <= box[1]
                    and box[3] <= bottom
                ):
                    raise ValueError(
                        f"The box {list(box)} of {part.callout!r} reaches "
                        "outside the grid "
                        f"[{self.origin[0]}, {self.origin[1]}, {right}, "
                        f"{bottom}]; a box is a place on the guide image."
                    )
        callouts = [part.callout for part in self.parts]
        twice = sorted({c for c in callouts if callouts.count(c) > 1})
        if twice:
            raise ValueError(
                f"{', '.join(twice)} is given more than one row; one callout "
                "is one row of the split."
            )
        named: dict[tuple[int, int], CellPart] = {}
        claims: list[tuple[int, _Rect, ZBand | None]] = []
        for number, part in enumerate(self.parts):
            own: set[tuple[int, int]] = set()
            for name in part.cells:
                index = self.index_of(name)
                earlier = named.get(index)
                if index in own or (earlier is not None and not part.broad):
                    raise ValueError(
                        f"The half-cell {name} is claimed by both "
                        f"{(earlier or part).callout!r} and "
                        f"{part.callout!r}. One half-cell has one callout, "
                        "unless the later part is marked broad."
                    )
                own.add(index)
                named.setdefault(index, part)
                claims.append((number, self._rect_of(index), None))
            for region in part.regions:
                rect = self._region_rect(part, region)
                claims.append((number, rect, region.z))
        broad_used: set[int] = set()
        for later, rect, band in claims:
            for earlier, other, other_band in claims:
                if earlier >= later:
                    break
                if _rects_meet(rect, other) and _bands_meet(band, other_band):
                    if not self.parts[later].broad:
                        raise ValueError(
                            f"{self.parts[later].callout!r} claims a spot "
                            f"{self.parts[earlier].callout!r} already "
                            "claims, at the same height, and is not marked "
                            "broad; a position there would have two callouts."
                        )
                    broad_used.add(later)
        idle = [
            part.callout
            for number, part in enumerate(self.parts)
            if part.broad and number not in broad_used
        ]
        if idle:
            raise ValueError(
                f"{', '.join(idle)} is marked broad but overlaps no earlier "
                "part; broad says the part yields to every earlier part it "
                "overlaps, and a flag that covers nothing says something "
                "untrue."
            )
        boxes = [(part, box) for part in self.parts for box in part.boxes]
        for i, (one, a) in enumerate(boxes):
            for two, b in boxes[i + 1 :]:
                if a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]:
                    raise ValueError(
                        f"The boxes of {one.callout!r} and {two.callout!r} "
                        "overlap, so a position there would have two callouts."
                    )
        # Kept only once every check above has passed: pydantic sets up the
        # private storage before an "after" validator runs, so a half-cell
        # name is read here and never before the grid it names is checked.
        self._named.update(named)
        self._claims.extend(
            (self.parts[number], rect, band) for number, rect, band in claims
        )
        return self

    def _rect_of(self, index: tuple[int, int]) -> _Rect:
        """A half-cell's pixel rectangle ``(x0, y0, x1, y1)``."""
        half_width, half_height = self.cell[0] / 2, self.cell[1] / 2
        return (
            self.origin[0] + index[0] * half_width,
            self.origin[1] + index[1] * half_height,
            self.origin[0] + (index[0] + 1) * half_width,
            self.origin[1] + (index[1] + 1) * half_height,
        )

    def _region_rect(self, part: CellPart, region: CellRegion) -> _Rect:
        """A region's pixel rectangle: the fraction of its half-cell, or the
        box on the crossing point. Refused if the crossing's half-cells do
        not meet at one point."""
        half_width, half_height = self.cell[0] / 2, self.cell[1] / 2
        if region.cell is not None:
            x0, y0, x1, y1 = self._rect_of(self.index_of(region.cell))
            # A fraction reaching the half-cell's far side ends on its edge
            # exactly: x0 + 1.0 * width can miss the neighbour's x0 by float
            # noise, and two regions in touching half-cells then "overlap"
            # (found in Story 4.23: I11a against I10c and I11c).
            return (
                x0 + region.x[0] * half_width,
                y0 + region.y[0] * half_height,
                x1 if region.x[1] == 1.0 else x0 + region.x[1] * half_width,
                y1 if region.y[1] == 1.0 else y0 + region.y[1] * half_height,
            )
        indices = [self.index_of(name) for name in region.crossing or []]
        # In index units a half-cell is [c, c + 1] x [r, r + 1], so the
        # common point is exact integer arithmetic, never float equality.
        left = max(column for column, _ in indices)
        right = min(column + 1 for column, _ in indices)
        top = max(row for _, row in indices)
        bottom = min(row + 1 for _, row in indices)
        if left != right or top != bottom:
            raise ValueError(
                f"{part.callout!r}: the half-cells {region.crossing} do not "
                "meet at one point, so they name no crossing."
            )
        # Half-cells that meet at one point without sharing an edge meet
        # inside the grid, at least one half-cell from its border, so a box
        # of at most two half-cells (CellRegion refuses more) stays on it.
        size = region.size or 0.0  # never None on a crossing (CellRegion)
        px = self.origin[0] + left * half_width
        py = self.origin[1] + top * half_height
        return (
            px - size * half_width / 2,
            py - size * half_height / 2,
            px + size * half_width / 2,
            py + size * half_height / 2,
        )

    def index_of(self, name: str) -> tuple[int, int]:
        """A half-cell's name -> its (column, row) index on the half-cell
        grid, counted from the origin.

        Raises:
            ValueError: The name is not a half-cell of this grid.
        """
        match = _HALF_CELL.fullmatch(name)
        if (
            match is None
            or match[1] not in self.columns
            or not 1 <= int(match[2]) <= self.rows
        ):
            raise ValueError(
                f"{name!r} is not a half-cell of the grid: a column "
                f"{self.columns[0]}-{self.columns[-1]}, a row 1-{self.rows} "
                "and a quarter a-d, e.g. K13b."
            )
        quarter = "abcd".index(match[3])
        return (
            2 * self.columns.index(match[1]) + quarter % 2,
            2 * (int(match[2]) - 1) + quarter // 2,
        )

    def name_of(self, index: tuple[int, int]) -> str:
        """A half-cell index on the grid -> its name (``K13b``)."""
        column, row = index
        return (
            f"{self.columns[column // 2]}{row // 2 + 1}"
            f"{'abcd'[2 * (row % 2) + column % 2]}"
        )

    @property
    def floor_band(self) -> ZBand:
        """The height range ``[zmin, zmax)`` of the floor the image draws
        (Story 4.24), as a :data:`ZBand` -- a side left open (an infinity)
        where the split has no cut there -- so the floor is tested with the
        bands' own :func:`_z_inside` and :func:`_bands_meet`. Named apart
        from :func:`math.floor`, which :meth:`part_at` also calls."""
        return (
            -float("inf") if self.zmin is None else self.zmin,
            float("inf") if self.zmax is None else self.zmax,
        )

    def pixel(self, x: float, y: float) -> tuple[float, float]:
        """A game position projected onto the guide image (:attr:`fit`)."""
        return (
            self.fit.sx * x + self.fit.bx,
            -self.fit.sy * y + self.fit.by,
        )

    def _part_of(self, index: tuple[int, int]) -> CellPart:
        """The half-cell's part: named, or the nearest named one's.

        **The distance is taken from the index steps**, not from two
        absolute centres subtracted: two half-cells equally far in steps are
        then exactly equally far in floats, so a tie is a tie and goes to
        the table's order as documented. Measured in the Story 4.17 review:
        subtracting centres made G12b's two neighbours at 21.1 px differ in
        the last bits, and float noise -- not the file -- chose *toutside*
        over *tladder*.
        """
        if index in self._named:
            return self._named[index]
        if index not in self._nearest:
            half_width, half_height = self.cell[0] / 2, self.cell[1] / 2
            best: tuple[float, CellPart] | None = None
            for named, part in self._named.items():
                distance = ((named[0] - index[0]) * half_width) ** 2 + (
                    (named[1] - index[1]) * half_height
                ) ** 2
                if best is None or distance < best[0]:
                    best = (distance, part)
            if best is None:
                raise ValueError("the split names no half-cell to inherit from.")
            self._nearest[index] = best[1]
        return self._nearest[index]

    def part_at(
        self,
        x: float | None,
        y: float | None,
        z: float | None,
        *,
        stands: bool = True,
    ) -> CellPart | None:
        """The part a position is counted under, or ``None`` where the split
        cannot place it: a coordinate missing or not finite (NaN or an
        infinity is no position, as elsewhere in the codebase), off the
        split's floor (:attr:`floor_band`, Story 4.24), or -- on a split of an area
        that is not coarse (Story 4.23) -- held by no part's box, half-cell
        or region and band: the rest of the room keeps the area's own
        callout, and the nearest rule does not apply.

        **A height band applies only where a player stands** (Story 4.23,
        the lead's decision): a band is measured on the live players' ticks,
        so ``stands`` is ``False`` for a position no player stood at -- a
        grenade's detonation, often in mid-air -- and then no banded claim
        holds it. It falls to the next claim with no band, then to the
        area's rest or the nearest half-cell, as the split's form says."""
        if any(v is None or not isfinite(v) for v in (x, y, z)):
            return None
        if not _z_inside(self.floor_band, z):  # type: ignore[arg-type]
            return None
        px, py = self.pixel(x, y)
        for part in self.parts:
            for box in part.boxes:
                if box[0] <= px < box[2] and box[1] <= py < box[3]:
                    return part
        for part, rect, band in self._claims:
            if (
                rect[0] <= px < rect[2]
                and rect[1] <= py < rect[3]
                and (stands or band is None)
                and _z_inside(band, z)  # type: ignore[arg-type]
            ):
                return part
        if self._rest_is_area:
            return None
        return self._part_of(
            (
                floor((px - self.origin[0]) / (self.cell[0] / 2)),
                floor((py - self.origin[1]) / (self.cell[1] / 2)),
            )
        )

    @property
    def inherited(self) -> dict[str, str]:
        """Every half-cell of the grid the table does not name, and the
        callout it takes from its nearest named half-cell -- the table's
        *"the rest to the nearest"*, written out. Empty on a split of an
        area that is not coarse, whose rest keeps the area's own callout."""
        if self._rest_is_area:
            return {}
        return {
            self.name_of((column, row)): self._part_of((column, row)).callout
            for row in range(2 * self.rows)
            for column in range(2 * len(self.columns))
            if (column, row) not in self._named
        }


#: The fields that make a split's grid: the image and its fit, and the
#: grid laid on it. Every split **on one floor** of one map must agree on
#: them (Story 4.18: Ancient's three splits are one grid, as his pictures
#: are), so the fit written on each is checked to be one value, not three
#: that may drift. Since Story 4.24 the rule is one grid per floor: Nuke's
#: guide draws its lower floor apart, so that floor has a fit of its own.
SPLIT_GRID_FIELDS: tuple[str, ...] = (
    "image", "fit", "origin", "cell", "columns", "rows"
)
#: The fields that make a split's floor (Story 4.24): the height range
#: ``[zmin, zmax)`` its image draws, a side left open where one is absent.
#: Two splits of one map whose ranges overlap (half-open, so [a, b) and
#: [b, c) are two floors) are on one floor, and must then have the same
#: range and the same grid.
SPLIT_FLOOR_FIELDS: tuple[str, ...] = ("zmin", "zmax")


class CalloutEntry(_Section):
    """One game area of one map, as the product owner names and reads it.

    ``callout`` absent is a statement and not a gap in the file: the place
    has no callout, and the report prints the game's name **flagged**, so the
    reader sees what is missing. See ``callouts.toml``'s header for every key.

    The three rules checked here are the ones a single entry can break:

    * ``junction`` and ``junction_source`` come together -- a junction the
      file cannot trace to his words is exactly what AD-13 forbids, and a
      source on a transit area would claim a reason nothing uses;
    * ``coarse`` needs a ``callout``: a coarse name is a name;
    * ``confidence`` is ``unnamed`` exactly when there is no ``callout`` --
      a place with no name has no mapping to be sure of, and a named one
      must say how sure it is.

    **A callout is compared ignoring case and surrounding spaces**, and it
    is normalised to that form here, so ``"radio"`` and ``"Radio "`` in two
    entries are one place and cannot become two steps.
    """

    callout: str | None = Field(default=None, min_length=1)
    coarse: bool = False
    junction: bool
    junction_source: str | None = Field(default=None, min_length=1)
    #: The derived check (AD-13: derivation first). Recorded, never read by
    #: the route: where it disagrees with :attr:`junction`, his list wins.
    neighbours: int = Field(ge=0)
    confidence: CalloutConfidence
    source: str = Field(min_length=1)
    note: str | None = Field(default=None, min_length=1)
    #: The area divided by position into his callouts (Story 4.17), or
    #: ``None`` for an area counted whole. Its own ``callout`` stays what a
    #: rule row on the whole area prints. A coarse area's split places every
    #: position; a split of an area that is not coarse holds only finer
    #: places inside it, and the rest keeps ``callout`` (Story 4.23).
    split: CellSplit | None = None

    @field_validator("callout")
    @classmethod
    def _normalise_the_callout(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normal = " ".join(value.split()).lower()
        if not normal:
            raise ValueError("a callout cannot be blank")
        return normal

    @model_validator(mode="after")
    def _check_the_entry(self) -> "CalloutEntry":
        if self.junction != (self.junction_source is not None):
            raise ValueError(
                "junction and junction_source come together: a junction "
                "names the words that make it one, and a transit area has "
                "none."
            )
        if self.coarse and self.callout is None:
            raise ValueError(
                "coarse is set on an area with no callout; a coarse name is "
                "still a callout."
            )
        if (self.callout is None) != (self.confidence == "unnamed"):
            raise ValueError(
                "confidence is 'unnamed' exactly when there is no callout: a "
                "place with no name has no mapping to be sure of, and a "
                "named place must say how sure its name is."
            )
        if self.split is not None:
            if self.callout in {part.callout for part in self.split.parts}:
                raise ValueError(
                    f"a part of the split is named {self.callout!r}, the "
                    "whole area's own name; the two would print alike."
                )
            # Story 4.23: on an area that is not coarse the parts are finer
            # places inside it, and the rest keeps the area's own callout.
            self.split._rest_is_area = not self.coarse
        return self

    @property
    def certain(self) -> bool:
        """Whether the callout prints unmarked: his words, the guide's
        callout that is the game area's own name where the coordinates do not
        contradict it, or a callout another source proposed that the
        coordinates confirm (:data:`CERTAIN_CONFIDENCE`). An entry with no
        callout is not certain -- it prints its own mark."""
        return self.confidence in CERTAIN_CONFIDENCE

    @property
    def kept(self) -> bool:
        """Whether the route keeps this area in the middle of a path.

        A junction is kept; so is a place with no callout, whatever its
        ``junction`` says, because dropping it would hide the flag that asks
        for the missing callout -- which is the point of the flag.
        """
        return self.junction or self.callout is None


#: One map's table: game area -> entry.
MapCallouts = dict[str, CalloutEntry]


def named_places(
    entries: Mapping[str, CalloutEntry],
) -> Iterator[tuple[str, CalloutEntry | CellPart]]:
    """Every place of a map's table with the key it is reported under: each
    area's entry, and each part of a split area (Story 4.17) under
    ``"<area> split"`` -- a part is a place exactly as an area is. The one
    enumeration: the loader's rules and the aggregate's marks both read it.
    """
    for area, entry in entries.items():
        yield area, entry
        if entry.split is not None:
            for part in entry.split.parts:
                yield f"{area} split", part


def _shared_parts(path: Path, map_name: str, areas: dict) -> dict:
    """The map's raw areas with every ``parts_from = "<Area>"`` of a split
    replaced by that area's split parts (Story 4.20).

    His answers for Inferno name places across several split areas at once
    (his top-of-mid answer points back to the boiler he named under
    Apartments), and one half-cell holds
    positions of two of them (I11a: Apartments and TopofMid), so the splits
    read **one** table -- written once and derived, not one copy per split
    that may drift. Since Story 4.28 five Inferno splits read it, the one
    junction table: Banana's, which writes it, and Apartments', TopofMid's
    and both sites', so his site answers of 2026-09-28 reach the sites'
    positions; Pit and Ruins, transit, write tables of their own. Ancient's
    CTSpawn reads BombsiteA's table the same way (Story 4.27). Each split
    still renames only its own area's positions. The table must be written
    on a split that does not itself borrow.

    Raises:
        SettingsError: The named area has no split, or borrows itself, or
            disagrees with the borrower on junction.
    """
    shared = {}
    for area, entry in areas.items():
        split = entry.get("split") if isinstance(entry, dict) else None
        if not isinstance(split, dict) or "parts_from" not in split:
            shared[area] = entry
            continue
        origin = split["parts_from"]
        source = areas.get(origin)
        source_split = source.get("split") if isinstance(source, dict) else None
        if (
            not isinstance(source_split, dict)
            or "parts_from" in source_split
            or "parts" in split
        ):
            raise SettingsError(
                f"The callout table {path}: [{map_name}.{area}.split] takes "
                f"parts_from = {origin!r}, which must be another area of "
                f"[{map_name}] whose split writes its own parts; and a split "
                "that borrows its parts writes none of its own."
            )
        # A shared part's junction is derived from the areas whose positions
        # it holds (the header's rule), and every split that reads the table
        # holds some: so every reader must agree with the lender, or one row
        # would carry a flag derived for another kind of place.
        if entry.get("junction") != source.get("junction"):
            raise SettingsError(
                f"The callout table {path}: [{map_name}.{area}] borrows the "
                f"parts of [{map_name}.{origin}] but disagrees with it on "
                "junction; a shared part's junction is derived from the "
                "junction of the areas whose positions it holds, so the two "
                "must agree."
            )
        rest = {key: value for key, value in split.items() if key != "parts_from"}
        shared[area] = {
            **entry,
            # The raw rows, not a copy: each split validates them into its
            # own part objects.
            "split": {**rest, "parts": source_split.get("parts", [])},
        }
    return shared


def load_callouts(
    map_pool: list[str], path: Path = CALLOUT_TABLE_PATH
) -> dict[str, MapCallouts]:
    """Read the callout table, refusing what it cannot vouch for.

    **An unknown map is an error and not a skip** (AD-13, the same stance as
    ``settings.toml``'s ``extra="forbid"``): a misspelt map section would
    otherwise leave that map's places in the game's names with nothing to
    say why. The allowed maps are ``[league].map_pool``, handed in, so the
    list of maps is not kept twice.

    **A callout may not be spelled like an area the table leaves
    unnamed**: that area prints the game's name, so the two would be one
    place on the report (Story 4.15).

    **Two areas that share a callout must agree** on ``junction`` and
    ``coarse``. They are one place to him, so the route treats them as one;
    an entry that disagreed would make the same place a junction on one
    side of the room and transit on the other. They may differ in
    ``confidence``: the route marks the merged callout if any of them is not
    certain.

    **A part of a split area is a place like an area** (Story 4.17): its
    callout is held to the same two rules. Nuke's yard part *main* is the
    game's ``Mini`` (his *main*), so the two must agree, and the counts
    merge.

    **An empty map section is refused**: it would say the map is described
    while describing nothing, and every route on it would lose the
    ``no_table`` note that says so.

    **The splits of one floor of a map are on one grid** (Story 4.18, per
    floor since Story 4.24): each carries the image, fit and grid
    (:data:`SPLIT_GRID_FIELDS`) and its floor (:data:`SPLIT_FLOOR_FIELDS`).
    Two splits whose floors overlap are on one floor, so their floors must
    be equal and their grids too, because his half-cell names are read off
    one picture grid of that floor; splits on floors that do not overlap
    (Nuke's upper and lower) may carry different grids, as the guide draws
    the floors apart.

    **The limit, stated:** an area name is not checked here. The loader has
    no list of the game's areas, so a misspelt area loads and simply never
    matches; ``-m archive`` catches it (``tests/test_calibration.py``
    requires the table to cover exactly the areas the archive moves
    through).

    Raises:
        SettingsError: The file is missing, is not TOML, names a map outside
            the pool, or holds an entry that is not valid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SettingsError(
            f"The callout table {path} could not be read: {exc}\n"
            "It ships with the code; restore it from the repository."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError(
            f"The callout table {path} is not valid TOML: {exc}"
        ) from exc
    unknown = sorted(set(data) - set(map_pool))
    if unknown:
        raise SettingsError(
            f"The callout table {path} holds a section for "
            f"{', '.join(unknown)}, which is not in [league].map_pool "
            f"({', '.join(map_pool)}).\n"
            "Fix the map's name, or add the map to the pool."
        )
    table: dict[str, MapCallouts] = {}
    for map_name, areas in data.items():
        if not isinstance(areas, dict) or not areas or not all(
            isinstance(entry, dict) for entry in areas.values()
        ):
            raise SettingsError(
                f"The callout table {path}: [{map_name}] must hold one "
                f"[{map_name}.<game area>] table per area and nothing else, "
                "and at least one."
            )
        areas = _shared_parts(path, map_name, areas)
        try:
            entries = {
                area: CalloutEntry(**entry) for area, entry in areas.items()
            }
        except _ValidationError as exc:
            raise SettingsError(
                f"The callout table {path} holds an entry of [{map_name}] "
                f"that is not valid:\n{_format_validation_error(exc)}"
            ) from exc
        unnamed = {area for area, entry in entries.items() if entry.callout is None}
        named = list(named_places(entries))
        clash = sorted(
            f"{area} ({entry.callout!r})"
            for area, entry in named
            if entry.callout in unnamed
        )
        if clash:
            raise SettingsError(
                f"The callout table {path}: in [{map_name}], "
                f"{', '.join(clash)} is spelled exactly like an area the "
                "table gives no callout. That area prints the game's name, "
                "so the report would read the two as one place."
            )
        splits = {
            area: entry.split
            for area, entry in entries.items()
            if entry.split is not None
        }
        for one, two in combinations(splits, 2):
            if not _bands_meet(splits[one].floor_band, splits[two].floor_band):
                continue  # two floors, which the guide draws apart
            if splits[one].floor_band != splits[two].floor_band:
                raise SettingsError(
                    f"The callout table {path}: the splits of [{map_name}] "
                    f"{one} and {two} are on floors that overlap and differ "
                    f"({list(splits[one].floor_band)} against "
                    f"{list(splits[two].floor_band)}); "
                    f"{', '.join(SPLIT_FLOOR_FIELDS)} "
                    "must be the same on splits of one floor, so a position "
                    "is on one floor's grid."
                )
            if any(
                getattr(splits[one], key) != getattr(splits[two], key)
                for key in SPLIT_GRID_FIELDS
            ):
                raise SettingsError(
                    f"The callout table {path}: the splits of [{map_name}] "
                    f"{one} and {two} are on one floor but not on one grid; "
                    f"{', '.join(SPLIT_GRID_FIELDS)} must be the same on each "
                    "split of a floor, so a half-cell's name means one place "
                    "of the image."
                )
        by_callout: dict[str, tuple[str, CalloutEntry | CellPart]] = {}
        for area, entry in named:
            if entry.callout is None:
                continue
            first = by_callout.setdefault(entry.callout, (area, entry))
            if (first[1].junction, first[1].coarse) != (
                entry.junction,
                entry.coarse,
            ):
                raise SettingsError(
                    f"The callout table {path}: [{map_name}.{first[0]}] and "
                    f"[{map_name}.{area}] are both {entry.callout!r} but "
                    "disagree on junction or coarse. One callout is one "
                    "place, so the two entries must say the same."
                )
        table[map_name] = entries
    return table
