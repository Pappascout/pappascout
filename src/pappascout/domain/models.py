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
"""

from __future__ import annotations

import os
import tomllib
from math import isfinite
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as _ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from pappascout.constants import seconds_label
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
    "MAX_FACEIT_PAGE_SIZE",
    "MAX_FACEIT_RETRY_ATTEMPTS",
    "PLAYERS_ON_SERVER",
    "REMOVED_SETTINGS",
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
        demos_root: **A directory for downloaded demos outside the archive**,
            or ``None`` = the archive's own ``demos/``.

            **The default is ``None``, and that is a decision and not a
            missing value** (2026-09-05). The archive is in a synchronised
            folder and follows from one machine to the other; demos in a
            local folder do not follow, and on the other machine they would
            be fetched from FACEIT again -- which works for about 30 days
            only. On top of that, a sync client frees the local space a
            parsed demo takes **without deleting the file**, leaving a cloud
            placeholder, whereas in a local folder freeing that space is a
            final deletion. The difference in size (a demo 142-223 MB, its
            parsed result about 1 MB) would argue for a local directory, but
            that solves disk space only -- and the cloud solves it without
            the material being left on one machine.

            The setting exists all the same, because disk space can run out
            on a machine where the cloud is not an option. Changing it
            **downloads nothing again**: demos already in the archive are
            still found (see
            :meth:`~pappascout.archive.paths.ArchivePaths.find_demo`).
        language: The user interface language.
        lock_ttl_seconds: The archive lock's expiry time.
    """

    own_team_name: str
    archive_root: Path
    demos_root: Path | None = None
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
    threshold now.

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
    advance_area_min_observations: PositiveInt = 20
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
            # (2 rounds out of 66), so five is a valid value and the guard
            # must not reject it. Six defenders, on the other hand, is an
            # impossible observation.
            "stack_min_players",
        ):
            value = getattr(self, name)
            if value > on_server:
                raise ValueError(
                    f"{name} ({value}) is greater than the number of players "
                    f"on the server ({on_server}), so the condition cannot be "
                    "met on any round and the rule could never fire."
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
    repetition. Five rules leave that unwritten.

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
    whenever the key has been forgotten from the file.

    **Pruning does not change a single number** -- neither in
    ``report.json`` nor in the report. In particular it does not change the
    bookkeeping of a block's pattern filtering: the row is always built first
    and pruned only afterwards (:mod:`pappascout.render.view`).

    **Some round types are protected from every rule**, and the list is owned
    by :data:`pappascout.render.view.PROTECTED_ROUND_TYPES`, because it is a
    presentation choice and not an adjustable value.

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
    #: The default is empty, that is, the rule is off, and that is a
    #: measurement result: a late sample point is thin and skewed but **not
    #: repetition** like rules 1, 2, 4 and 5, so leaving it out can cost
    #: content. The numbers are in ``settings.toml``.
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
        # silences all three anomaly rules permanently -- and the report would
        # then claim "no anomalies" as an observation although not one sample
        # point was ever examined. Neither section can check this alone, so
        # the check is here.
        earliest_sample = min(self.parse.snapshot_seconds)
        if self.thresholds.advance_max_sample_s < earliest_sample:
            raise ValueError(
                f"thresholds.advance_max_sample_s "
                f"({self.thresholds.advance_max_sample_s:g} s) is smaller "
                f"than the earliest parse.snapshot_seconds "
                f"({earliest_sample:g} s), so not one sample point fits "
                "inside the anomaly rules' time bound.\n"
                "All three rules would fall silent permanently, and the "
                "report's anomaly count would claim 'no anomalies' as an "
                "observation -- although nothing was examined."
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
