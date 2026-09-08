"""The demoparser2 implementation of all six parse tables (AD-8).

**This is the only module where the game's prop names appear.** The stage sees
only the :class:`~pappascout.adapters.protocols.DemoParser` port, so replacing
or upgrading demoparser2 does not touch the pipeline.

Every field used below has been **observed in a real demo** (see
``_bmad-output/implementation-artifacts/demoparser2-kentat.md``), not guessed.

Round boundaries
----------------
A round is bounded by two events:

``round_freeze_end``
    The end of freezetime, the round's **anchor**. Every time (``t_s``) is
    measured from it, and the buy time starts there. The side is read at this
    moment.
``round_end``
    The round being decided. At this moment the winner, the win reason, the
    survivors and their equipment are read. The players have not respawned
    yet -- ``round_officially_ended`` would be too late, by then all ten are
    alive again.

Buy time
--------
**Economy values are not read at the anchor.** CS2's buy time continues after
freezetime ends, and **on about half of the rounds people are still buying
then**: measured over five league demos, 52 rounds out of 106. The share
varies wildly from match to match -- on Anubis it was 20 rounds out of 22,
that is 91 %, on Nuke 7/23, that is 30 % -- so "about half" is the average
over the data and not a figure any single demo can be assumed to settle on.
Equipment value read at the anchor underestimates the kit, overestimates the
money left in pocket and gives too small a number for the armed count.

Economy values are therefore read at the **end of the buy time**::

    buy_end_tick = min(freeze_end_tick + buy_window_seconds,
                       the tick before the first death,
                       the end of the round)

Three things here were observed in a demo, not assumed:

* **``m_unFreezetimeEndEquipmentValue`` does not update after freezetime.** It
  is the game's own snapshot of the anchor moment, so read from a later tick
  it gives exactly the same number. The kit at the end of the buy time must
  therefore be read from the prop ``m_unCurrentEquipmentValue``. At the anchor
  these two are the same number, so a 0 s window gives the same result as
  before this change. (Measured: ``inferno_vs_ryhmarama`` round 6, anchor
  $11,550 on both props; at +2 s the freezetime prop is still 11,550 and
  current is 15,350 -- and 15,350 is the number the user read off the demo.)
* **Death empties the inventory and the armour.** A dead player's
  ``inventory`` is ``[]`` and ``m_ArmorValue`` is 0 from the death tick
  onwards, so he must not be read: the armed count would drop. That is why
  the window is cut at the tick **before** the first death and not at the
  death tick. (Equipment value, by contrast, is not zeroed by death, but that
  does not change the rule.)
* **One tick for the whole round.** When nobody has died yet at the moment of
  measurement, nobody has had the chance to drop a weapon on dying either --
  double counting (a team-mate picking up the dead player's rifle) is
  therefore structurally impossible, and no per-player "last alive" point is
  needed. At the moment of measurement the team is untouched.

The cut **fires on about half of the rounds** and is not an edge case: across
six demos (134 rounds played) it hits 69 rounds, that is 51 %. Even so, in
this data no death precedes the last purchase. Because an overlap is possible
-- buying is finished within 8 s on 92 % of those rounds where anything was
bought after the anchor, and the earliest death is 9.8 s -- the price of the
cut is measured on every run: ``buy_window_purchases_after_cut`` says how many
players still bought after the cut point, and ``buy_window_cuts_unchecked``
how many cuts could not be checked at all. Both are supposed to be zero.

``round_end`` **does exist** in demoparser2 0.42.0 even though it is not on
the ``list_game_events()`` list. It returns the columns ``round``, ``tick``,
``winner`` and ``reason``, and the first row is an empty initial value
(tick 1). ``round`` is the demo's own round counter (Ancient 1..22, knife
round included), and it goes into the ``round_raw`` column as it is.

The round number is **not decided here**: the adapter returns the ``round_no``
column empty, and ``stages.parse`` calls
``domain.rounds.mark_played_rounds``.

One round boundary is nevertheless settled here already: the **match
restart**. In league demos the knife round is followed by a
``round_freeze_end`` of its own without any ``round_end``, and play continues
normally after it. It is not a round, and because it has no number of the
demo's own it cannot get a ``round_raw`` either -- and without one
``stages.parse`` could not recognise its rows. It is therefore left unnumbered
here and produces no row in any table; the count travels through the
diagnostics into the run's summary.

Recognition rests on **observations and not on position**: a restart has no
``round_end`` *and* the demo's own numbering continues over it by one. Either
condition broken stops the parse instead of dropping the round silently. See
:meth:`Demoparser2Adapter._assign_round_raw` and
:meth:`Demoparser2Adapter._match_restarts`.

Utility thrown during a restart belongs to no round: there is no round window
for it, so the throw ends up in the count ``grenades_outside_rounds`` -- the
same one that holds warm-up throws. The number is therefore normally a little
larger in a league demo than in an old demo, and that is not a fault.

Where the score is measured
---------------------------
``score_start`` is read at the round's own freezetime anchor and ``score_end``
at the **next round's** anchor. The reason is the knife round: the point it
produces is still visible at its own ``round_end`` tick, but
``mp_restartgame`` zeroes it immediately afterwards -- a reading from its own
end tick would claim the knife round had been played. The reading at the next
anchor is the one after the reset and therefore the right one.

The last round has no next anchor, so its ``score_end`` is read at its own
``round_end`` tick. That is safe: the score has already gone up by that moment
(verified in both test demos) and no reset follows. The same fallback is used
when the next round has no anchor.

Whose values are summed
-----------------------
The end-of-buy-time sums (money, money spent, equipment value, equipment
value at the start of the round) are computed only from those players for
whom **all** of these props are readable, and ``players_buy_end`` is the size
of that same set. The divisor is therefore always the same set as the
numerator: three players' sum divided by five would look like an eco even
when the team had bought a full round.

``players_armed_buy_end`` is computed from **the same set**: how many players
were armed at the end of the buy time. The sum does not say that -- two AKs
and three empty hands give the same sum as five half-buys.

``money_players_buy_end`` comes from the same set too: the **cash balances one
player at a time**, sorted descending. The values are the ones
``money_buy_end`` sums; here they are merely kept. The reason is the same as
for the armed count: a team where one player has 5,000 and four have nothing
gets the same sum as a team where everyone has 1,000, but in the latter all
five can buy next round and in the former only one. That is exactly what
separates a half-buy from a force (``domain.economy``, condition B).

The order is sorted, not player order: the row carries no player ids, so an
element's position says nothing about who it is. Sorting makes the reading
reproducible regardless of the order the tick's rows happen to arrive in.

Armed = **armour and at least one weapon in hand**. The weapon is read from
the player's inventory (``inventory``) and the armour from the prop
``m_ArmorValue``, not from the equipment value: equipment value is weapon +
armour + grenades as a single number, so a Glock + kevlar + two flashes
($1,250, measured on Ancient) would look armed without a single weapon.

**Possession, not purchase.** The inventory is read at the end of the buy
time, so a rifle saved from the previous round or picked up off a dead player
counts the same as one just bought. What matters for the round is what is in
hand, not where it came from. The default pistols are still excluded: they are
free every round, so possessing one says nothing.

**An unreadable observation empties the whole row.** If even one readable
player's armour or inventory is missing, the count is ``null`` -- not the
number the rest would give. The player stays in the ``players_buy_end``
divisor, so a silent drop would look like a save round rather than a read
failure.

``players_armored_buy_end`` is **the same reading under a different
condition**: how many of the same set carried armour (``m_ArmorValue > 0``) on
the same tick. It is not a generalisation of the armed count but an
observation of its own, because the two answer different questions: armed is
the half-buy's calibrated condition A, armoured answers the question "how many
had armour".

**Possession, not purchase** -- the same rule as for the armed count. Armour
carries over the round for anyone who survived, damaged armour included
(37/100 is still armour), so the count says what the players had, not what
they bought. **The pistol round is the exception** (1 and 13): the half starts
from a clean slate and nothing is inherited, so there -- and only there -- the
number is an observation of buying. That is exactly why the product owner's
*"5 kevlars"* is the right reading of Nuke's T pistol round.

The two counts also differ most on the pistol round: with $800 of starting
money, kevlar (650) and an upgraded weapon do not fit into the same purchase,
so the armed count is in practice 0 even when all five have kevlar. It is not
a rule: a picked-up weapon is enough to arm a player, and the measured
counter-example is ``Anubis_vs_ryhmarama`` round 13, where the CT side's
counts are 3 and 1.

The armour count's readability condition is **narrower**: only
``m_ArmorValue`` (:data:`_ARMORED_PROPS`). The inventory is not part of it,
because the count does not read it; an unreadable inventory therefore empties
the armed count but not the armour count. Each emptying has its own
diagnostics figure, so that the difference shows during the run and not first
in the report. The helmet is not told apart: the analysis talks about kevlar,
and the helmet is a different observation.

The classification is a **list of permitted weapons**
(:mod:`pappascout.constants`), not a list of forbidden ones: an unknown name
is not a weapon. Knives are an open set that Valve keeps growing; weapons are
a closed one. Unknown names travel into the diagnostics and from there into
the run's summary -- a silent drop would be as bad as silent acceptance.

Sample points
-------------
The same read also produces the ``ticks`` table: a row per (player, round,
sample point). The sample points are chosen by
:mod:`pappascout.domain.sampling`, which is a pure function -- the adapter's
part is to read the props at the chosen ticks and tell the domain which side
each player is on.

The round boundaries, the lineups and the tick rate are computed **once** and
used for both tables. That is why the port returns them together
(:class:`~pappascout.adapters.protocols.DemoTables`): two separate calls would
identify the lineups twice, and if the results ever differed, ``lineup_key``
would be different in the two tables and the join would no longer land.

The team's and the players' names
---------------------------------
The same read also produces the ``lineups`` table: a row per (lineup, player)
carrying the player's name and his clan name. Both are read at **the same
anchor ticks** at which the lineups are already identified -- the demo is not
read again, and the props come in the same ``parse_ticks`` call.

**The clan is read per player, not through the side.** Measured 2026-08-30 on
five demos: ``team_clan_name`` gives every SteamID exactly one clan at every
anchor, across the half-time switch too. Read through the side
(``m_iTeamNum``) the same value changes team at half time -- ``team_num=2`` is
``KALJUKOSTAJA`` in the first half and ``MatureMayhem`` in the second. That is
the trap the per-player read avoids.

``lineup_key`` **does not change**: it is still computed from the SteamIDs
alone (:meth:`_Lineup.key`), so adding names does not move a single archive
directory.

Kills and deaths
----------------
The same read also produces the ``deaths`` table: a row per death, victim and
attacker both with their areas and coordinates. ``player_death`` is read
**once**, with a call that asks for the per-player fields::

    parse_event("player_death", player=["last_place_name", "X", "Y", "Z",
                                        "team_num"])

The library returns them under the prefixes ``user_*`` (victim),
``attacker_*`` and ``assister_*``. The same result also serves to bound the
buy window and as the first-contact fallback, so the event is not read twice.

**Measured 2026-08-30, ``Ancient_vs_kaljukostaja``.** There are 151 rows.
Coverage: ``user_last_place_name`` 151/151, ``attacker_last_place_name``
149/151, ``assister_last_place_name`` 58/151. The assister is left out: it is
half empty and no row of the target analysis rests on it. The two rows that
have no attacker area are **the same two** that have no attacker at all
(``planted_c4``) -- the area did not go missing, there was no attacker.

The area is **an observation for both**: it comes from the same event as the
death itself, so it is not derived from anything and the table has no
``area_source``.

The side and the lineup are read from the **round's own side map**
(:meth:`Demoparser2Adapter._assign_sides`) with the same :func:`_side_lookup`
as in utility, not from the event's ``team_num`` field. The reason is
consistency: read from the event, one deviant reading would put the death on
a different team from the one ``ticks`` and ``events`` name for the same
player on the same round. The event's own ``team_num`` is the **fallback** for
those players who are in neither lineup and not on the round's anchor tick --
a player who joined mid-map or reconnected.

The round is settled by the same segmentation as in utility
(:func:`_round_windows`, anchor = the last ``round_freeze_end``), and
``round_no`` is left empty: the numbering belongs to ``stages.parse``. People
really do die on the knife round, and that is exactly why its rows are dropped
by the same join as the sample points and the grenades -- there is no separate
knife-round rule and there must not be one.

Utility
-------
The ``grenade_thrown`` event **does not exist**, so utility is read from the
``parse_grenades()`` trajectories: a trajectory's first point is the throw and
its last one the detonation. The table is the demo's largest single batch --
1,553,329 rows on Ancient -- and it is reduced to two rows per grenade
immediately by :func:`~pappascout.domain.utility.grenade_endpoints`, after
which about 750 rows travel on.

Two things about the raw data are surprising, and both were observed on the
Ancient demo:

* **Most of the rows are not trajectory.** A grenade gets a row while it is in
  a player's bag too, and then ``x, y, z`` are empty; 1.34 million rows out of
  1.55 are like that. In flight the type is ``...Projectile``, in the bag it
  is not.
* **``grenade_entity_id`` is recycled.** 374 trajectories fit into 187 ids.
  The recycling is not limited to the gap between rounds: in the league demo
  ``inferno_vs_ryhmarama``, on round 11, id 564 carries three different
  trajectories (molotov 9.2 s, flashbang 18.0 s and incendiary 64.2 s). The
  segmentation is therefore ``grenade_endpoints``'s responsibility rather than
  a grouping by id, and the ``grenade_no`` it hands out is what goes into the
  table.

In flight, molotov and incendiary are both ``CMolotovProjectile``. They are
told apart by the type in the thrower's bag on the tick before the throw
(``CMolotovGrenade`` / ``CIncendiaryGrenade``); if that does not settle it
unambiguously, the type stays ``molotov``.

The detonation position has been cross-checked against the demo's own events
(``smokegrenade_detonate``, ``hegrenade_detonate``, ``flashbang_detonate``):
the trajectory's last point lands on them to within 0.024 game units in all
281 cases. The events are still not read during a run -- the trajectory is
enough, and three extra event reads would cost without adding anything.

The point cloud and the detonation area
---------------------------------------
The same read also produces the ``callouts`` table: a grid of where the
players have actually stood on the map and what area each position is in. It
is the **source of the detonation areas**, and it is kept precisely so that a
derived area can be checked against the demo.

**What was removed and why.** Story 2.2 derived the detonation area from the
nearest living player. That was not merely imprecise but structurally wrong:
smoke is thrown where nobody is -- precisely because it blocks vision and
forces rotations. The proxy measured the opposite of what it was meant to,
and **42 % of the detonations were left without an area entirely** (measured
over four league demos, 1,716 detonations); with the point cloud the share is
6.4 %. The method was not kept alongside as a fallback: two methods would make
the row uninterpretable.

**What it costs, measured.** The cloud needs the only whole-demo tick read in
this module::

    parse_ticks([m_szLastPlaceName, X, Y, Z, m_lifeState])

``Ancient_vs_kaljukostaja`` 2026-08-30: **2.1 s, 1,529,910 rows**, of which
1,092,083 are alive with a known area. The data is reduced to a grid
immediately -- at 32-unit cells that comes to 7,703 cells and 18 areas.

**The memory peak is 1.0 GB, and it is the library's rather than this
module's.** Measured on ``Nuke_vs_imuaijat`` (1,914,720 rows) with the
process's ``PeakWorkingSetSize``: a baseline of 48 MB, 705 MB after
``parse_ticks`` and a peak of **1,043 MB** already inside the call; our own
Polars conversion adds 44 MB to that (705 -> 749) and building the grid
another 134 MB. The peak therefore comes from demoparser2's own frame, which
has 1.9 million rows and eight columns -- the five props asked for plus
``tick``, ``steamid`` and ``name``, which the library always adds.

``del`` drops only the name and does not return memory to the operating
system: the measured working set does not shrink at all after ``del frame``.
The promise is therefore exactly what it is -- **the data does not live longer
than building it requires**, which lets the allocator reuse the space -- and
not "memory is freed".

**The read is unconditional, and that is a trade.** The cloud is built even
when the demo has no grenades at all: the table is an output of its own that
``parse`` promises to write, and its existence must not depend on whether
somebody happened to throw a smoke. The price is about 2 s and about a 1 GB
peak per demo. There is no switch: a conditional cloud would make
``callouts.parquet`` sometimes present and sometimes missing, and ``parse``'s
skip rule (every expected output in place) would turn from predictable into
unpredictable.

**The threshold stays.** "The nearest cell is always found" is not coverage:
the measured maximum distance is 1,074 units, and without a threshold the
report would claim an area for a detonation that happened far from everything
any player has ever stood on. ``[parse].area_snap_units`` is therefore still
here, recalibrated for the point cloud.

Players are **no longer read at the detonation ticks**: the area comes from
the cloud, not from the moment. The throw ticks are still read, because the
thrower's own area is an observation.

The controller and the pawn are different entities
--------------------------------------------------
In CS2 a player has two entities. The **controller**
(``CCSPlayerController``) represents the player -- name, team, money, score --
and survives the whole match. The **pawn** (``CCSPlayerPawn``) is his physical
character on the map -- being alive, area, coordinates, equipment value,
armour -- and it disappears when the player is not in the game. The prop's
prefix says which of the two it is, and it has to be read out of every check:
finding a controller field **does not** prove that the player is on the map.

**Measured 2026-08-31, ``anubis_vs_RCAVE_VETERANS``** (round 19, player
``egerrrrr`` / 76561199635619622): the controller's ``m_iTeamNum`` is 3, but
every pawn field is empty on the same ticks -- ``m_lifeState``,
``m_szLastPlaceName`` and ``X``/``Y``/``Z``. There are **15** pawnless rows
(five from sample-point ticks, ten from throw ticks); in the archive's seven
other demos there are none. Skipping these rows is
:meth:`Demoparser2Adapter._read_sample_ticks`'s job, and the skip requires
**all** of the pawn fields to be missing -- one missing field is a library
change and not a player's state.

Memory use
----------
The demo is not loaded into memory whole. ``parse_ticks`` is called **only for
the ticks of the round boundaries, the ends of the buy times, the sample
points and the grenade throws** (Ancient: 44 + 21 + about 100 + about 375
ticks), not for the whole tick series. There are four such targeted calls
rather than one -- the whole-demo read for the point cloud is a fifth and a
case of its own, see below -- because both the end of the buy time and the
sample-point ticks depend on the tick rate, which is not measured until the
round boundaries have been read, and the grenade ticks are not known until the
trajectories are. A compressed demo is decompressed as a stream into a temp
file.

**One exception, and it is deliberate.** The point cloud is read from the
whole demo's tick series (:data:`CLOUD_TICK_PROPS`), because the question is
"where on the map has anyone stood and what area is that" and not "where was
the team at this moment". It is one call, five light props and 2.1 seconds,
and the result is reduced to a few thousand cells before anything else is
done. The cloud's scope is the whole demo on purpose as well: the rows from
the warm-up and the knife round say as much about the map as the ones from
the rounds played.

The buy window costs one extra ``parse_ticks`` call (Ancient: 21 measurement
points) and one ``parse_event("player_death")`` call. The latter used to be
made only when the first-contact fallback was on; now it is always made,
because cutting the window must not depend on the first-contact setting. An
event read is orders of magnitude cheaper than a tick read, and it is done
once and shared between both users.
"""

from __future__ import annotations

import hashlib
import statistics
import warnings
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.adapters.decompress import readable_demo
from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
    ParseDiagnostics,
)
from pappascout.constants import ARMING_WEAPONS, KNOWN_INVENTORY_ITEMS
from pappascout.domain.sampling import (
    FIRST_CONTACT_SAMPLE,
    DamageEvent,
    RoundBounds,
    SamplePoint,
    first_contact_tick,
    sample_ticks,
    seconds_since_freeze_end,
)
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
)
from pappascout.domain.utility import (
    DETONATE,
    THROWN,
    build_point_cloud,
    empty_point_cloud,
    flight_point,
    grenade_endpoints,
    nearest_cells,
    trajectory_gap_ticks,
)
from pappascout.errors import ParseError

__all__ = [
    "Demoparser2Adapter",
    "TEAM_SIDES",
    "TICK_PROPS",
    "SAMPLE_TICK_PROPS",
    "SAMPLE_PAWN_PROPS",
    "CLOUD_TICK_PROPS",
    "DAMAGE_COLUMNS",
    "DEATH_PLAYER_PROPS",
    "DEATH_COLUMNS",
    "GRENADE_COLUMNS",
    "GRENADE_TYPES",
    "FIRE_ITEM_TYPES",
    "MOLOTOV_PROJECTILE",
    "DEFAULT_TICK_RATE",
    "TICK_RATE_MIN",
    "TICK_RATE_MAX",
    "MAX_MATCH_RESTARTS",
]

# -- The game's fields --------------------------------------------------------

_TEAM_NUM = "CCSPlayerController.m_iTeamNum"
_ACCOUNT = "CCSPlayerController.CCSPlayerController_InGameMoneyServices.m_iAccount"
_CASH_SPENT = (
    "CCSPlayerController.CCSPlayerController_InGameMoneyServices"
    ".m_iCashSpentThisRound"
)
_EQUIP_ROUND_START = "CCSPlayerPawn.m_unRoundStartEquipmentValue"

#: The value of the player's kit **right now**. This is the source of the
#: equipment value at the end of the buy time, not
#: ``m_unFreezetimeEndEquipmentValue``: the latter is the game's snapshot of
#: the anchor moment and does not update after freezetime, so read from a
#: later tick it would still give the anchor's number and the whole fix would
#: stay invisible. At the anchor these two are the same number.
_EQUIP_CURRENT = "CCSPlayerPawn.m_unCurrentEquipmentValue"
_ARMOR_VALUE = "CCSPlayerPawn.m_ArmorValue"

#: The player's inventory: a list of item display names (``AK-47``,
#: ``Smoke Grenade``, ``knife_t``, the knife skins' own names). Not a prop
#: name but demoparser2's own derived column, and the only source that shows
#: **which** weapon a player has -- the equipment value says only what the kit
#: cost.
_INVENTORY = "inventory"

_LIFE_STATE = "CCSPlayerPawn.m_lifeState"
_TEAM_SCORE = "CCSTeam.m_iScore"
_ROUND_START_TIME = "CCSGameRulesProxy.CCSGameRules.m_fRoundStartTime"

#: The game's own area name (``env_cs_place``). About twice as coarse as a
#: Total CS callout; an empty string means an area the game gives no name to,
#: and it is kept in the table as ``null``.
_PLACE_NAME = "CCSPlayerPawn.m_szLastPlaceName"

#: The player's clan name, that is, the team's name in the demo.
#: demoparser2's own derived column (not a prop name), and the only source for
#: the team's name -- it must not be guessed from the file name or from the
#: FACEIT id.
#:
#: **Read per player.** Read through the side, the value changes team at half
#: time; read through the SteamID it is constant for the whole map (measured
#: 2026-08-30, five demos, no exceptions).
_CLAN_NAME = "team_clan_name"

#: The player's name. demoparser2 adds it to every ``parse_ticks`` result
#: automatically alongside ``steamid`` and ``tick``, so it is not asked for as
#: a prop -- but it **is checked** against the returned columns, so that a
#: library change cannot leave the names silently empty.
_PLAYER_NAME = "name"

#: The player's coordinates. demoparser2 returns these as float32 already.
_X = "X"
_Y = "Y"
_Z = "Z"

#: The props read at the round boundary ticks.
TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _CLAN_NAME,
    _ACCOUNT,
    _CASH_SPENT,
    _EQUIP_ROUND_START,
    _EQUIP_CURRENT,
    _ARMOR_VALUE,
    _INVENTORY,
    _LIFE_STATE,
    _TEAM_SCORE,
    _ROUND_START_TIME,
)

#: The props read at the sample-point ticks. A shorter list than at the round
#: boundaries: the setup needs only position, side and whether the player is
#: alive -- the economy values are a property of the round, not of the moment.
#:
#: **One of these is a controller field and four are the pawn's.**
#: ``m_iTeamNum`` comes from ``CCSPlayerController`` and is there even for a
#: player who has no character on the map; the other four are
#: ``CCSPlayerPawn`` fields and disappear with him. The difference is measured
#: in this module's documentation, and it is the reason why
#: :meth:`Demoparser2Adapter._read_sample_ticks` looks at four fields and not
#: one.
SAMPLE_TICK_PROPS: tuple[str, ...] = (
    _TEAM_NUM,
    _LIFE_STATE,
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
)

#: :data:`SAMPLE_TICK_PROPS`'s **pawn fields**, that is, the ones that
#: disappear when the player has no character on the map. The list is a
#: constant of its own because skipping a pawnless row requires every one of
#: these to be empty -- a new pawn prop has to be added here, or the skip
#: would silently loosen by one field.
SAMPLE_PAWN_PROPS: tuple[str, ...] = (
    _LIFE_STATE,
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
)

#: The props read from the **whole demo's** tick series for the point cloud.
#:
#: A shorter list than for the sample points: ``m_iTeamNum`` is not among them
#: because the cloud is a property of the map and not of a team -- the
#: question is "who has stood in this spot and what area is it", and which
#: side stood there does not answer it. Spectator rows do not spoil the cloud:
#: a spectator has no ``last_place_name`` and is not alive, so the filter
#: drops him under the same condition as a dead player.
#:
#: **This is the module's only whole-tick-series read.** The reasoning and the
#: measured cost are in the module's documentation.
CLOUD_TICK_PROPS: tuple[str, ...] = (
    _PLACE_NAME,
    _X,
    _Y,
    _Z,
    _LIFE_STATE,
)

#: The columns the ``parse_grenades()`` table must have. ``name`` is present
#: in the library but is left unread: a player's name can change mid-match,
#: and the id is ``steamid``.
GRENADE_COLUMNS: tuple[str, ...] = (
    "grenade_type",
    "grenade_entity_id",
    "x",
    "y",
    "z",
    "tick",
    "steamid",
)

#: The game's class name in flight -> the canonical grenade type.
#:
#: These are ``parse_grenades()``'s ``grenade_type`` values on the rows that
#: have coordinates. An unknown name is kept as it is: it is a rare but
#: readable result, whereas turning it into an empty value would lose the
#: observation.
GRENADE_TYPES: dict[str, str] = {
    "CSmokeGrenadeProjectile": "smoke",
    "CFlashbangProjectile": "flashbang",
    "CHEGrenadeProjectile": "he",
    "CMolotovProjectile": "molotov",
    "CDecoyProjectile": "decoy",
}

#: In flight, molotov and incendiary are the **same** class.
MOLOTOV_PROJECTILE = "CMolotovProjectile"

#: In the bag they are distinct. This is where the grenade's real type comes
#: back from.
FIRE_ITEM_TYPES: dict[str, str] = {
    "CMolotovGrenade": "molotov",
    "CIncendiaryGrenade": "incendiary",
}

#: ``m_iTeamNum`` -> side. 0 and 1 are spectator and unassigned, not teams.
TEAM_SIDES: dict[int, str] = {2: "T", 3: "CT"}

#: The columns the ``player_hurt`` and ``player_death`` events must have. Both
#: offer all four in demoparser2 0.42.0.
DAMAGE_COLUMNS: tuple[str, ...] = (
    "tick",
    "attacker_steamid",
    "user_steamid",
    "weapon",
)

#: The per-player fields asked for from the ``player_death`` event.
#:
#: The library returns each of these under **three prefixes**: ``user_*``
#: (victim), ``attacker_*`` and ``assister_*``. The assister is not read: it
#: is half empty (58/151 measured 2026-08-30) and no row of the target
#: analysis rests on it.
DEATH_PLAYER_PROPS: tuple[str, ...] = (
    "last_place_name",
    "X",
    "Y",
    "Z",
    "team_num",
)

#: ``player_death``'s **per-player** columns, prefixes included.
#:
#: These come **in addition to** the :data:`DAMAGE_COLUMNS` fields, not
#: instead of them: the lists are separate because they are fixed in
#: different places, and :meth:`Demoparser2Adapter._damage_rows` names a
#: missing column together with its own list.
#:
#: A missing column is an error and not an empty value: without the check the
#: deaths table would be structurally valid but have no areas, and the
#: report's "first death, most often Cave" line would disappear without
#: saying why. ``*_team_num`` is there as the fallback for a side the lineups
#: do not know -- it is mandatory too, because its disappearance would show
#: only as dropped rows.
DEATH_COLUMNS: tuple[str, ...] = tuple(
    f"{prefix}_{prop}"
    for prefix in ("user", "attacker")
    for prop in DEATH_PLAYER_PROPS
)

#: A living player's ``m_lifeState``. The other values are dead or dying.
_ALIVE = 0

#: CS2's default tick rate. Used only if no measured value can be got from the
#: demo.
DEFAULT_TICK_RATE = 64.0

#: Sanity bounds for a measured tick rate. CS2's servers run at 64 or 128
#: ticks; a value outside these is a measurement error (a clock reset
#: mid-match, for instance), not the truth.
TICK_RATE_MIN = 16.0
TICK_RATE_MAX = 256.0

#: How many match restarts are accepted in one demo.
#:
#: A restart is a round boundary that has a freezetime anchor but no
#: ``round_end``, and **over which the demo's own round numbering continues by
#: one**. In league matches there is exactly one, right after the knife round.
#: More than that would mean a phenomenon nobody has seen yet; the parse then
#: stops rather than guessing.
#:
#: Every message derived from this reads the number from here, so that raising
#: the limit does not leave the texts lying (see
#: :meth:`Demoparser2Adapter._match_restarts`).
MAX_MATCH_RESTARTS = 1


@dataclass
class _Lineup:
    """One team's lineup on one map.

    ``members`` grows during the map if the team substitutes a player. The id
    is computed from everyone who played on the map, so that the same lineup
    produces the same key from one run to the next.

    ``names`` and ``clans`` are **per-player** observation counters, not
    per-team ones: the clan name is read through the SteamID, because read
    through the side it would change team at half time (see the module's
    documentation). A counter rather than a single value, because a conflict
    is settled by the number of observations and not by the order of reading
    -- and a tie alphabetically, so that the run is reproducible.

    **The id is still computed from the SteamIDs alone.** Adding names must
    not change ``lineup_key``: it is the archive's directory structure.
    """

    members: set[str] = field(default_factory=set)
    names: dict[str, Counter[str]] = field(default_factory=dict)
    clans: dict[str, Counter[str]] = field(default_factory=dict)

    def observe(self, rows: Sequence[dict[str, Any]], side: str) -> None:
        """Record one tick's rows as belonging to this lineup.

        Takes in only the given side's rows, that is, exactly the same set
        that used to be merged into ``members`` with a set operation.

        An empty string is not a name: ``_read_ticks`` has already turned it
        into ``None``, and ``None`` is not recorded. A missing name is an
        observation, and it shows in the table as ``null`` rather than as an
        invented value.
        """
        for row in rows:
            if row["side"] != side:
                continue
            steamid = row["steamid"]
            self.members.add(steamid)
            name = row.get("player_name")
            if name is not None:
                self.names.setdefault(steamid, Counter())[name] += 1
            clan = row.get("clan_name")
            if clan is not None:
                self.clans.setdefault(steamid, Counter())[clan] += 1

    def key(self) -> str:
        """The lineup's digest.

        Raises:
            ParseError: If the lineup is empty. The digest of an empty string
                would be the same for both teams, so ``lineup_key`` would not
                tell the teams apart at all and every later grouping would go
                silently wrong.
        """
        if not self.members:
            raise ParseError(
                "Both teams' lineups could not be identified from the demo: "
                "one of them came out empty.\n"
                "No players were found on both sides at the round boundary "
                "ticks. The demo is most likely corrupt or truncated."
            )
        raw = ",".join(sorted(self.members))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


@dataclass
class _Segment:
    """One round boundary in the demo: a round played, unresolved, or a restart.

    Attributes:
        demo_round: The demo's own round number, from the ``round`` field of
            the ``round_end`` event. ``None`` = the segment was not resolved,
            or demoparser2 gave no number.
        freeze_end_tick: The round's anchor, the last ``round_freeze_end``
            before it ended. ``None`` = there is no anchor, in which case the
            observations at the end of freezetime cannot be read (``status``
            says so).
        end_tick: The moment the round was decided, from the ``round_end``
            event. ``None`` = the round was not resolved: the demo was cut
            short, or the segment is not a round at all.
        winner_side: The winning side (``"T"``/``"CT"``), or ``None`` if the
            round was not resolved.
        win_reason: The win reason under the demo's own name, or ``None`` for
            the same reason as ``winner_side``.
        round_raw: The demo's own round number given to the segment. ``None``
            means a **match restart**: it was played, but it is not a round
            and it produces no row in any table -- the same as the knife
            round. See :meth:`Demoparser2Adapter._assign_round_raw`.
    """

    demo_round: int | None
    freeze_end_tick: int | None
    end_tick: int | None
    winner_side: str | None
    win_reason: str | None
    round_raw: int | None = None


@dataclass(frozen=True)
class _SampleTickCounts:
    """The row counts of one sample-point tick.

    Two numbers rather than one, because the skip must not rest on the count
    of pawnless rows alone. A tick that yielded no usable row at all is an
    **observation** only when pawnlessness explains it entirely -- if rows
    were lost for some other reason too (a spectator, an unknown side), it is
    a fault, and it has to be raised. Without ``seen``, a single pawnless row
    would silence a hard error by chance.

    Attributes:
        seen: The rows demoparser2 returned for this tick. Every row, the
            skipped ones included.
        without_pawn: Of those, the ones where the controller was there but
            every pawn field was empty.
    """

    seen: int = 0
    without_pawn: int = 0


@dataclass(frozen=True)
class _UtilityCounts:
    """Grenades that did not reach the table as they were -- and the reason.

    These are not columns that fit into the table: a dropped grenade cannot be
    a row, and an unresolved type is not distinguishable from a resolved one
    except as a number. All of them therefore travel into the diagnostics and
    from there into the run's summary.

    Zero is the target state, but not for every one of them:
    ``outside_rounds`` is normally 1-2, because grenades are still thrown
    after the round has been decided and there is no ``t_s`` for them.

    Attributes:
        without_thrower: A trajectory with no thrower.
        outside_rounds: A throw that falls within no round's boundaries.
        unknown_side: A thrower whose side could not be established.
        unknown_type: A grenade whose class name is not known. The name is
            kept in the table as it is, but the figure exposes a demoparser2
            rename before it shows up in the report.
        fire_type_unresolved: A fire grenade whose molotov/incendiary
            distinction was not settled. The type stays ``molotov``, so
            without the figure a total breakdown of the bag lookup would look
            exactly like a demo in which only molotovs were thrown.
        detonating_after_round: A detonation that falls after the round
            ended. **An observation and not a drop**: the row gets its area
            from the point cloud like every other. In Story 2.2 these were
            left without an area because the method of the day would have
            read the area off the next round's spawn; the reason went away
            with the method.
        ticks_without_players: A **throw** tick that yielded no player row at
            all. Unlike the other figures in this class, this is a **fault**
            and not an observation -- it means the thrower's own area could
            not even be attempted. The detonation ticks are not read at all.
        sharing_an_entity_id: Trajectories that share the game's own
            ``grenade_entity_id`` with another trajectory **on the same
            ``round_raw``** -- the demo's own round counter, which includes
            the warm-up and the knife round too. The figure counts
            trajectories and not pairs: three trajectories on one id is 3. An
            observation and not a fault, because the table's key is
            ``grenade_no``.
        throwers_without_row: Throws whose **thrower was not among the rows
            of the throw's tick**. The area is then left empty, because an
            observation is not replaced by an estimate -- and the point cloud
            does not help: a throw's area is the thrower's own
            ``m_szLastPlaceName``.

            **A fault and not an observation**, and new since Story 2.10:
            before the pawnless row was skipped, this case brought the run
            down on the alive guard. Now the row is skipped silently, so a
            throw can drain into the ``utility_without_area`` figure without
            a reason. This figure is that reason. The expected value is zero:
            by definition a thrower has a pawn at the moment he throws.
    """

    without_thrower: int = 0
    outside_rounds: int = 0
    unknown_side: int = 0
    unknown_type: int = 0
    fire_type_unresolved: int = 0
    detonating_after_round: int = 0
    ticks_without_players: int = 0
    sharing_an_entity_id: int = 0
    throwers_without_row: int = 0


@dataclass(frozen=True)
class _CloudCounts:
    """Point cloud observations that do not fit the ``CALLOUT_CLOUD`` contract.

    The number of cells and of areas is **not here**, and neither is the
    number of rows that made it into the cloud: all three are readable from
    the finished table. The sum of the ``observations`` column **is** the
    number of rows that made it, because every valid row ends up in exactly
    one cell -- computing it here as well would be the same number from two
    sources. Here there is only what is visible **only** at the moment of
    reading.

    Attributes:
        rows_read: The rows the whole-demo tick read returned (a row per
            player per tick). The module's largest single batch, and this is
            the only place its size is visible -- the finished table holds
            only the part that qualified.
        empty_reason: Why the cloud came out empty, or ``None``. An empty
            cloud does not bring the run down, but without a reason it would
            look like a demo in which no utility was thrown.
    """

    rows_read: int = 0
    empty_reason: str | None = None


@dataclass(frozen=True)
class _DeathCounts:
    """Deaths that did not reach the table as they were -- and the reason.

    Zero is the target state for all three, but ``outside_rounds`` can
    genuinely be a small number: people still die after the round has been
    decided, and such a death has no ``t_s``.

    **Knife-round deaths are not in these figures.** They are inside the
    round's boundaries and get their ``round_raw``; they are dropped only in
    ``stages.parse``'s numbering, by the same mechanism as the sample points
    and the grenades, and the stage reports how many.

    Attributes:
        without_tick: A death whose tick could not be read. Without a tick a
            death cannot be assigned to a round and ``t_s`` cannot be
            computed.
        outside_rounds: A death that falls within no round's boundaries.
        without_victim: A death **with no victim**. A different thing from a
            missing side: here the event has no ``user_steamid`` at all, and
            it is not a failure of side inference. The reasons are kept apart
            for the same reason as ``without_attacker`` and
            ``without_attacker_area`` in ``stages.parse``'s figures: a
            combined figure would look like an inference fault that is not
            there.
        without_victim_side: A death whose victim **is known** but whose side
            could not be established from the lineup, from the round's anchor
            tick, or from the event's own ``user_team_num`` field. The row is
            dropped: ``victim_lineup_key`` is the whole table's join key.
        attacker_without_side: A death where the **attacker's** side stayed
            unknown even though the attacker is known. The row survives and
            the attacker's observations with it; only ``attacker_side`` and
            ``attacker_lineup_key`` are left empty.
    """

    without_tick: int = 0
    outside_rounds: int = 0
    without_victim: int = 0
    without_victim_side: int = 0
    attacker_without_side: int = 0


@dataclass
class _BuyWindowCounters:
    """Buy window observations that do not fit the ``ROUNDS`` contract.

    Attributes:
        cuts: The rounds cut short by a death, as pairs ``(round_raw, how many
            purchases fell behind the cut)``. **Pairs rather than a number**,
            because the adapter does not know which rounds end up in the
            table: the knife round gets a ``round_raw`` of its own but
            ``stages.parse`` drops it, and a plain total would include it
            without any way to subtract it out again. The stage filters these
            against the rounds played.

            The cut itself is **an observation and not a fault**: it is the
            rule, because a dead player's inventory empties, and it hits about
            half the rounds. Lost purchases **are supposed to be zero**.
        unchecked_cuts: Cut rounds (``round_raw``) where the lost purchases
            **could not be checked**: not one player yielded a readable
            ``cash_spent`` value at the tick at the end of the window.
            Without this, a zero for lost purchases would mean two different
            things -- "nothing was lost" and "not known".
        ticks_without_players: Rounds where the tick at the end of the buy
            time yielded no player row and the measurement fell back to the
            anchor. **A fault and not an observation**: in practice the demo
            has been cut short mid-round. Without the fallback the whole
            round's economy would be empty. On such a round a cut is **not**
            recorded: nothing was measured at the end of the window, so lost
            purchases are not the death cut's doing.
        players_lost: Team rows times the players who were readable at the
            anchor but no longer at the measurement point. The sums and the
            divisor shrink together, so per-player values stay right -- but
            the team looks like it is playing short-handed, and that is a
            different claim from "the connection dropped mid-round".
        sides_without_rows: Team rows that yielded no readable player at the
            measurement point even though they did at the anchor. The row goes
            into the table empty but with status ``ok``, and ``classify``
            leaves it unclassified because of the missing observation -- the
            right outcome, but without this figure nobody would get to know
            why.
        refunds: Player rows where ``cash_spent`` decreased between the anchor
            and the measurement point, that is, a purchase was refunded. The
            prop only grows from purchases, so a decrease is an unambiguous
            sign of a refund.
        stale_equipment: Player rows where the equipment value rose without
            the player buying, gaining armour or changing his inventory. That
            is the stale reading a refund leaves behind (see
            :func:`_refunds_and_stale_equipment`). Measured: 1 row out of 134
            rounds, at most $1,000 per player.
    """

    cuts: list[tuple[int, int]] = field(default_factory=list)
    unchecked_cuts: list[int] = field(default_factory=list)
    ticks_without_players: int = 0
    players_lost: int = 0
    sides_without_rows: int = 0
    refunds: int = 0
    stale_equipment: int = 0


@dataclass
class _ArmedCounters:
    """Armed-count observations that do not fit the ``ROUNDS`` contract.

    Attributes:
        unknown_items: An inventory name -> how many times it was seen. **A
            count and not just a set**: one exotic knife and a demoparser2
            rename that hits every row would look exactly the same as a bare
            name.
        unreadable_rows: Team rows where the **armed count** was left empty
            because some readable player's armour or inventory was missing.
            Anchorless rounds are **not** here: they have no observation at
            all, which is a different thing from a read that failed.
        armored_unreadable_rows: The same for the armour count. A figure of
            its own, because the conditions differ: this one only grows when
            the armour goes unread, whereas the previous one grows when the
            inventory alone fails too. The difference is therefore "rows where
            only the inventory failed" -- exactly the difference that makes
            the two counts' readability conditions different. This figure is
            always less than or equal to the previous one.
    """

    unknown_items: Counter[str] = field(default_factory=Counter)
    unreadable_rows: int = 0
    armored_unreadable_rows: int = 0


class Demoparser2Adapter:
    """Reads the rounds and sample-point tables with demoparser2.

    Implements the :class:`~pappascout.adapters.protocols.DemoParser` port.

    Args:
        exclude_weapons: Weapons that do not qualify as a first contact
            (``[parse].first_contact_exclude_weapons``). The default is
            deliberately empty: the adapter does not read settings, the stage
            hands the list over.
        fallback_death: Whether a first contact may come from the
            ``player_death`` event when there is no valid ``player_hurt``
            (``[parse].first_contact_fallback_death``).
        area_snap_units: The greatest distance from which a detonation's area
            may be taken from the nearest point cloud cell
            (``[parse].area_snap_units``). ``None`` = no threshold in use, in
            which case ``area`` is left empty but the coordinates and the
            distance are stored. That is the honest value of an uncalibrated
            setting and not a fault: the nearest cell is always found, so
            naming without a threshold would be a claim and not a
            measurement.
        callout_grid_units: The edge of a point cloud cell in game units
            (``[parse].callout_grid_units``).
        callout_z_weight: The weight of a vertical difference when the nearest
            cell to a detonation is looked for (``[parse].callout_z_weight``).
        callout_z_tolerance_units: The vertical difference that is free in the
            weighting (``[parse].callout_z_tolerance_units``). A player's
            height: a grenade detonates anywhere between the floor and head
            height, so without a tolerance the vertical penalty would hit the
            normal case.

            The defaults of the last three are **measured values** and not
            neutral zeros -- there is no such thing as a neutral cell size,
            and zero would be invalid. In production they still always come
            from the stage: the adapter does not read settings.
        buy_window_seconds: The length of the buy time in seconds from the end
            of freezetime (``[parse].buy_window_seconds``). The default is
            deliberately **0.0** and not the game's 20 s: the adapter does not
            read settings, and a neutral default means "measure at the
            anchor", which is exactly what this class did before the buy
            window existed. The stage hands over the right value.
    The armed count has no settings: the rule is "armour and at least one
    weapon in hand", and the weapon list is :mod:`pappascout.constants`. A
    change to the list invalidates the archive through ``stages.parse``'s
    parameter hash, not through this class.

    Attributes:
        diagnostics: The observations of the most recent parse that do not fit
            the table contracts. ``None`` before the first call.
    """

    def __init__(
        self,
        *,
        exclude_weapons: Sequence[str] = (),
        fallback_death: bool = True,
        area_snap_units: float | None = None,
        buy_window_seconds: float = 0.0,
        callout_grid_units: int = 32,
        callout_z_weight: float = 1.0,
        callout_z_tolerance_units: float = 72.0,
    ) -> None:
        self.exclude_weapons = tuple(exclude_weapons)
        self.fallback_death = fallback_death
        self.area_snap_units = area_snap_units
        self.buy_window_seconds = float(buy_window_seconds)
        self.callout_grid_units = int(callout_grid_units)
        self.callout_z_weight = float(callout_z_weight)
        self.callout_z_tolerance_units = float(callout_z_tolerance_units)
        self.diagnostics: ParseDiagnostics | None = None
        #: Why the map name could not be got from the header; set at the
        #: moment of reading and passed on into the diagnostics. Cleared at
        #: the start of every read, so that the previous demo's reason does
        #: not carry into the next one.
        self._header_missing_reason: str | None = None

    def read_map_name(self, path: Path) -> str | None:
        """See the port's documentation.

        **The same reader as the full parse** (:meth:`_header_map_name`) and
        the same decompression (``readable_demo``), so the import and the
        parse cannot see a different name for the demo's map. Two parallel
        readers would diverge before long, and the divergence would show only
        in the import accepting a demo that the parse names differently.

        The **reason** for a missing map name is left on this object
        (``_header_missing_reason``) the same way as in the parse, but it is
        not returned: the port's contract is an observation or its absence,
        and the caller's decision is the same in both cases -- the
        cross-check cannot be made, so the user is asked.
        """
        path = Path(path)
        with readable_demo(path) as demo_path:
            parser = self._open(demo_path, path)
            self._header_missing_reason = None
            return self._header_map_name(parser, path)

    def parse_demo(
        self, path: Path, sample_seconds: Sequence[float]
    ) -> DemoTables:
        """See the port's documentation."""
        path = Path(path)
        with readable_demo(path) as demo_path:
            return self._parse(demo_path, path, tuple(sample_seconds))

    # -- Internal ------------------------------------------------------------

    def _parse(
        self,
        demo_path: Path,
        original_path: Path,
        sample_seconds: tuple[float, ...],
    ) -> DemoTables:
        parser = self._open(demo_path, original_path)
        # The header is read from **the same parser object** as everything
        # else: the demo is not opened a second time for the map name.
        self._header_missing_reason = None
        map_name = self._header_map_name(parser, original_path)
        freeze_ticks = self._freeze_end_ticks(parser, original_path)
        round_ends = self._round_ends(parser, original_path)
        segments = self._segments(freeze_ticks, round_ends)

        if not segments:
            raise ParseError(
                f"No rounds at all were found in demo {original_path.name}.\n"
                "The file was most likely truncated during the download. "
                "Download the demo again."
            )

        wanted = sorted(
            {s.freeze_end_tick for s in segments if s.freeze_end_tick is not None}
            | {s.end_tick for s in segments if s.end_tick is not None}
        )
        by_tick = self._read_ticks(parser, wanted, original_path)
        tick_rate, measured = self._tick_rate(by_tick, freeze_ticks)

        # Deaths are read **always**, even when first_contact_fallback_death
        # is false: they bound the buy window, because a dead player's
        # inventory empties, and that must not depend on the first-contact
        # setting. The setting only decides whether a first contact may come
        # from a death. The same read is handed to the sample points, so that
        # the event is not parsed twice.
        death_rows, deaths_without_tick = self._death_events(
            parser, original_path
        )
        deaths = [
            (r["tick"], r["attacker_id"], r["victim_id"], r["weapon"])
            for r in death_rows
        ]
        death_ticks = sorted(tick for tick, *_ in deaths)
        buy_ticks, window_ticks = _buy_end_ticks(
            segments, death_ticks, tick_rate, self.buy_window_seconds
        )
        extra = sorted(
            {
                tick
                for tick in (*buy_ticks, *window_ticks)
                if tick is not None and tick not in by_tick
            }
        )
        if extra:
            by_tick.update(self._read_ticks(parser, extra, original_path))

        lineups = [_Lineup(), _Lineup()]
        sides = self._assign_sides(segments, by_tick, lineups)
        lineup_keys = self._lineup_keys(lineups)
        # The armed count's and the buy window's own observations come back
        # with the table rather than accumulating in an object the caller
        # hands in: a mutable out-parameter would silently stop working if
        # somebody forgot to pass it on.
        rounds, armed, buy = self._build_frame(
            segments, by_tick, tick_rate, sides, lineup_keys, buy_ticks, window_ticks
        )

        points, unknown_sides = self._sample_points(
            parser,
            original_path,
            segments,
            sides,
            lineups,
            by_tick,
            tick_rate,
            sample_seconds,
            deaths,
        )
        ticks, partial, sample_tick_counts, points_without_pawn = (
            self._build_ticks_frame(
                points, parser, original_path, segments, sides, lineup_keys
            )
        )
        # The point cloud before the events table: it is the source of the
        # detonation areas, so it has to be in hand before a single area is
        # named.
        callouts, cloud_counts = self._build_callout_cloud(parser, original_path)
        events, utility, throw_tick_counts = self._build_events_frame(
            parser,
            original_path,
            segments,
            sides,
            lineup_keys,
            lineups,
            by_tick,
            tick_rate,
            callouts,
        )
        death_frame, death_counts = self._build_deaths_frame(
            death_rows,
            segments,
            sides,
            lineup_keys,
            lineups,
            by_tick,
            tick_rate,
            without_tick=deaths_without_tick,
        )
        # The lineups table is built only here, so that it carries every
        # member and name observed during the map -- the substitute who came
        # in only on a later round included.
        lineups_frame = self._build_lineups_frame(lineups, lineup_keys)

        self.diagnostics = ParseDiagnostics(
            tick_rate=tick_rate,
            tick_rate_measured=measured,
            rounds_seen=len(segments),
            match_restarts=sum(1 for s in segments if s.round_raw is None),
            partial_samples=partial,
            sample_rows_without_pawn=_pawnless_rows(
                sample_tick_counts, throw_tick_counts
            ),
            sample_points_without_pawn=points_without_pawn,
            grenade_throwers_without_row=utility.throwers_without_row,
            unknown_side_events=unknown_sides,
            grenades_without_thrower=utility.without_thrower,
            grenades_outside_rounds=utility.outside_rounds,
            grenades_unknown_side=utility.unknown_side,
            grenades_unknown_type=utility.unknown_type,
            grenades_fire_type_unresolved=utility.fire_type_unresolved,
            grenades_detonating_after_round=utility.detonating_after_round,
            grenade_ticks_without_players=utility.ticks_without_players,
            grenades_sharing_an_entity_id=utility.sharing_an_entity_id,
            callout_cloud_rows_read=cloud_counts.rows_read,
            callout_cloud_empty_reason=cloud_counts.empty_reason,
            header_map_name_missing_reason=self._header_missing_reason,
            unknown_inventory_items=tuple(sorted(armed.unknown_items.items())),
            lineup_name_conflicts=sum(
                1
                for lineup in lineups
                for votes in lineup.names.values()
                if len(votes) > 1
            ),
            lineup_clan_conflicts=sum(
                1
                for lineup in lineups
                for votes in lineup.clans.values()
                if len(votes) > 1
            ),
            deaths_without_tick=death_counts.without_tick,
            deaths_outside_rounds=death_counts.outside_rounds,
            deaths_without_victim=death_counts.without_victim,
            deaths_without_victim_side=death_counts.without_victim_side,
            deaths_attacker_without_side=death_counts.attacker_without_side,
            armed_unreadable_rows=armed.unreadable_rows,
            armored_unreadable_rows=armed.armored_unreadable_rows,
            buy_window_seconds=self.buy_window_seconds,
            buy_window_cuts=tuple(sorted(buy.cuts)),
            buy_window_unchecked_cuts=tuple(sorted(buy.unchecked_cuts)),
            buy_window_ticks_without_players=buy.ticks_without_players,
            buy_window_players_lost=buy.players_lost,
            buy_window_sides_without_rows=buy.sides_without_rows,
            buy_window_refunds=buy.refunds,
            buy_window_stale_equipment=buy.stale_equipment,
        )
        return DemoTables(
            rounds=rounds,
            ticks=ticks,
            events=events,
            lineups=lineups_frame,
            deaths=death_frame,
            callouts=callouts,
            match=self._build_match_frame(map_name),
        )

    def _header_map_name(self, parser: Any, original_path: Path) -> str | None:
        """The map's name from the demo's header, or ``None`` if it has none.

        The header is **an observation**: the name is returned as it is and is
        not compared against the map pool. A map outside the pool -- a
        workshop version or ``de_train`` -- is a genuine observation and not
        an unknown map, and silently correcting it to a pool name would make
        it a lie.

        A name that is empty or only spaces is ``None`` and not a substitute:
        only then does ``aggregate`` fall back to inferring the name from the
        ``map_demo_id``. The header's other fields (``server_name``, for
        instance) do not belong in this table.

        The exception is wrapped into a
        :class:`~pappascout.errors.ParseError` by the same rule as in
        :meth:`_open` and :meth:`_event`: the library's own error type is not
        this layer's contract.

        The message names **two** possible causes and not just a corrupt file.
        A method the library has renamed raises an ``AttributeError`` on a
        perfectly intact demo, and a bare "download it again" would send the
        user to fetch a 230 MB file that is already fine.
        """
        try:
            header = parser.parse_header()
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"The header of demo {original_path.name} could not be read: "
                f"{type(exc).__name__}: {exc}\n"
                "The cause is one of these two: the file is corrupt, or "
                "demoparser2's interface has changed and the header is no "
                "longer read this way. Check first whether the same demo "
                "opens with another demoparser2 version; if it does, the fix "
                "belongs in the adapter. Otherwise download the demo again."
            ) from exc
        get = getattr(header, "get", None)
        if not callable(get):
            self._header_missing_reason = (
                "parse_header() did not return a dictionary "
                f"(type {type(header).__name__})"
            )
            return None
        value = get("map_name")
        if value is None:
            self._header_missing_reason = (
                "the header has no map_name field at all -- demoparser2 has "
                "most likely renamed it"
            )
            return None
        # ``isinstance``, not ``str()``: a bytes object would turn into the
        # name ``b'de_ancient'``, which would look like an observation in the
        # table and shatter the map into a branch of its own. The observation
        # is the name or its absence, not a substitute -- and a library type
        # change must not pass silently.
        if not isinstance(value, str):
            self._header_missing_reason = (
                f"map_name is not a string but a {type(value).__name__}"
            )
            return None
        text = value.strip()
        if not text:
            self._header_missing_reason = "map_name is empty in the header"
            return None
        return text

    @staticmethod
    def _build_match_frame(map_name: str | None) -> pl.DataFrame:
        """Build the match table: **one row**, a known or an unknown name.

        The row is written even when there was no name. An empty table would
        mean a demo without a match, and that would be a different claim from
        "there is a match, but the map name could not be got" -- only the
        latter is true.
        """
        schema: dict[str, Any] = {
            name: MATCH[name] for name in MATCH_ADAPTER_COLUMNS
        }
        # No ``orient``: a dictionary row names its columns, so the
        # row-versus-column orientation does not apply to this call.
        return pl.DataFrame([{"map_name": map_name}], schema=schema)

    def _open(self, demo_path: Path, original_path: Path) -> Any:
        from demoparser2 import DemoParser as _Demoparser2

        try:
            return _Demoparser2(str(demo_path))
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"Demo {original_path.name} could not be opened: {exc}\n"
                "The file is most likely corrupt. Download the demo again."
            ) from exc

    def _freeze_end_ticks(self, parser: Any, original_path: Path) -> list[int]:
        frame = self._event(parser, "round_freeze_end", original_path)
        if frame is None or "tick" not in frame.columns:
            return []
        return sorted({int(t) for t in frame["tick"].tolist()})

    def _round_ends(self, parser: Any, original_path: Path) -> list[dict[str, Any]]:
        """The round endings in chronological order.

        The first row (tick 1, ``round`` 0, an empty winner) is demoparser2's
        initial value and not a round, so it is dropped.
        """
        frame = self._event(parser, "round_end", original_path)
        if frame is None or "tick" not in frame.columns:
            return []
        ends: list[dict[str, Any]] = []
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None or tick <= 1:
                continue
            ends.append(
                {
                    "tick": tick,
                    "round": _as_int(row.get("round")),
                    "winner": _as_side(row.get("winner")),
                    "reason": _as_str(row.get("reason")),
                }
            )
        ends.sort(key=lambda r: r["tick"])
        return ends

    def _event(
        self,
        parser: Any,
        name: str,
        original_path: Path,
        *,
        player: Sequence[str] | None = None,
    ) -> Any:
        """Read one event; ``player`` asks for the per-player fields.

        The library adds the requested fields under three prefixes
        (``user_*``, ``attacker_*``, ``assister_*``). The parameter is
        **keyword-only and empty by default**, because most events are read
        without them and nobody wants to pay for extra columns.
        """
        try:
            frame = (
                parser.parse_event(name)
                if player is None
                else parser.parse_event(name, player=list(player))
            )
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"In demo {original_path.name}, event {name!r} could not be "
                f"read: {exc}\n"
                "The file is most likely truncated. Download the demo again."
            ) from exc
        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return None
        return frame

    @staticmethod
    def _segments(
        freeze_ticks: list[int], round_ends: list[dict[str, Any]]
    ) -> list[_Segment]:
        """Pair the freezetime anchors with the round endings.

        A round's anchor is the last ``round_freeze_end`` before it ended. If
        there is no anchor the round is still included -- ``freeze_end_tick``
        is left empty and ``status`` says why (AD-9). If no ending follows an
        anchor, the demo was cut short mid-round; the round stays in, but
        without a result.
        """
        segments: list[_Segment] = []
        pending: list[int] = []
        i = 0
        for end in round_ends:
            while i < len(freeze_ticks) and freeze_ticks[i] < end["tick"]:
                pending.append(freeze_ticks[i])
                i += 1
            # All but the last anchor were left without an ending.
            for orphan in pending[:-1]:
                segments.append(_Segment(None, orphan, None, None, None))
            segments.append(
                _Segment(
                    demo_round=end["round"],
                    freeze_end_tick=pending[-1] if pending else None,
                    end_tick=end["tick"],
                    winner_side=end["winner"],
                    win_reason=end["reason"],
                )
            )
            pending = []
        for orphan in freeze_ticks[i:]:
            segments.append(_Segment(None, orphan, None, None, None))

        Demoparser2Adapter._assign_round_raw(segments)
        return segments

    @staticmethod
    def _assign_round_raw(segments: list[_Segment]) -> None:
        """Give every round the demo's own running number.

        The value comes from the ``round`` field of the ``round_end`` event. A
        segment left without a value of its own is handled according to
        **where in the list it is found and what has been observed about it**:

        * **At the tail of the list** it is an unresolved round: the demo was
          cut short and no ``round_end`` is coming. A value that preserves the
          order is derived for it from its neighbours.
        * **At the start of the list**, before the demo's first number of its
          own, it gets its value by counting backwards. The numbering can
          start anywhere, and there is no value before it to collide with.
        * **In the middle** it is a match restart -- but only if the
          observations say so. That is settled by :meth:`_match_restarts`,
          which stops the parse if the conditions are not met. A restart is
          left **unnumbered** (``round_raw = None``) by the same mechanism as
          the knife round: filling it in from a neighbour would give it a
          number the demo uses again immediately afterwards.

        Raises:
            ParseError: If an unnumbered round boundary in the middle does not
                meet the conditions of a restart, if there are more of them
                than :data:`MAX_MATCH_RESTARTS`, or if the numbering does not
                grow evenly.
        """
        if not segments:
            return
        raws: list[int | None] = [s.demo_round for s in segments]
        own = [index for index, value in enumerate(raws) if value is not None]

        if not own:
            # Not one segment has a number of the demo's own. The order is
            # still known, so the fallback is a running numbering.
            raws = list(range(1, len(raws) + 1))
        else:
            first_own, last_own = own[0], own[-1]

            # The tail: no number of the demo's own comes after these, so they
            # are unresolved rounds.
            value = raws[last_own]
            assert value is not None
            for index in range(last_own + 1, len(raws)):
                value += 1
                raws[index] = value

            # The start: before the first number of its own there is no value
            # to collide with.
            value = raws[first_own]
            assert value is not None
            for index in range(first_own - 1, -1, -1):
                value -= 1
                raws[index] = value

            # The ones left in the middle are checked against the
            # observations; the accepted ones stay ``None``, that is,
            # unnumbered.
            Demoparser2Adapter._match_restarts(segments, raws, own)

        known = [value for value in raws if value is not None]
        for first, second in zip(known, known[1:]):
            if second <= first:
                raise ParseError(
                    "The demo's own round numbering does not grow evenly "
                    f"({first} -> {second}).\n"
                    "The round boundaries do not match demoparser2's "
                    "round_end numbers, so the rounds cannot be identified "
                    "reliably."
                )

        for segment, number in zip(segments, raws):
            segment.round_raw = number

    @staticmethod
    def _match_restarts(
        segments: list[_Segment], raws: list[int | None], own: list[int]
    ) -> list[int]:
        """The round boundaries in the middle that are match restarts.

        Recognition rests on **observations and not on position**. "In the
        middle and without a number" alone is not enough: a round the boundary
        detection lost would look the same, and dropping it would take the
        round out of every table and name it a restart on top of that.

        A restart meets both conditions:

        * **No ``round_end``.** A segment that was resolved but has no number
          of the demo's own is a round without a number -- not a restart. It
          is numbered from a neighbour as before: the round exists, so it is
          not dropped. If the derived number collides with the demo's own, the
          monotonicity check deals with it.
        * **The demo's own numbering continues over it by one.** A restart
          does not consume a round number, so the numbers on either side of it
          are consecutive. A jump means a round has been lost in between; that
          stops the parse, because dropping it would shift everything that
          follows.

        Args:
            segments: The round boundaries in chronological order.
            raws: The number decided for each segment; ``None`` for the ones
                that have not been numbered. **Modified in place**: the gaps
                that are not restarts are filled in here.
            own: The indices of the segments that have a number of the demo's
                own.

        Returns:
            The indices of the restarts in the ``segments`` list. They stay
            ``None`` in ``raws``.

        Raises:
            ParseError: If the demo's numbering jumps over an unnumbered round
                boundary, or if there are more restarts than
                :data:`MAX_MATCH_RESTARTS`.
        """
        restarts: list[int] = []
        for previous, following in zip(own, own[1:]):
            gap = list(range(previous + 1, following))
            if not gap:
                continue

            if any(segments[i].end_tick is not None for i in gap):
                # A round was left in between that was resolved but has no
                # number of the demo's own. It is a round and not a restart,
                # so it is numbered from a neighbour -- dropping it would take
                # it out of every table and name it wrongly on top of that.
                value = raws[previous]
                assert value is not None
                for index in gap:
                    value += 1
                    raws[index] = value
                continue

            before, after = raws[previous], raws[following]
            assert before is not None and after is not None
            if after != before + 1:
                ticks = ", ".join(str(segments[i].freeze_end_tick) for i in gap)
                raise ParseError(
                    "The demo's own round numbering jumps over an unnumbered "
                    f"round boundary ({before} -> {after}, freezetime ticks "
                    f"{ticks}).\n"
                    "Over a restart the numbering would continue by one, so a "
                    "round the boundary detection did not find has been left "
                    "in between. It is not dropped on a guess: look at the "
                    "demo at the ticks listed and tell the developer what you "
                    "see."
                )

            restarts.extend(gap)

        if len(restarts) > MAX_MATCH_RESTARTS:
            ticks = ", ".join(str(segments[i].freeze_end_tick) for i in restarts)
            raise ParseError(
                f"The demo has {len(restarts)} round boundaries that look "
                f"like a match restart (freezetime ticks {ticks}), but at "
                f"most {MAX_MATCH_RESTARTS} are accepted.\n"
                "More than that means a phenomenon nobody has seen yet, and "
                "it is not guessed at. Open the demo at the ticks listed and "
                "tell the developer what you see before the result is used."
            )
        return restarts

    def _read_ticks(
        self, parser: Any, ticks: list[int], original_path: Path
    ) -> dict[int, list[dict[str, Any]]]:
        """Read the props at the given ticks and group them by tick."""
        if not ticks:
            return {}
        try:
            frame = parser.parse_ticks(list(TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"The tick values of demo {original_path.name} could not be "
                f"read: {exc}\n"
                "The file is most likely corrupt, or this demoparser2 version "
                "does not know these fields. Run: uv sync"
            ) from exc

        received = set(getattr(frame, "columns", ()))
        # ``name`` is among the requirements even though it is not asked for
        # as a prop: demoparser2 adds it itself, and without the check a
        # library change would leave the roster's names silently empty.
        missing = [
            name
            for name in (*TICK_PROPS, "tick", "steamid", _PLAYER_NAME)
            if name not in received
        ]
        if missing:
            raise ParseError(
                "demoparser2 did not return every requested field from demo "
                f"{original_path.name}. Missing: {', '.join(missing)}.\n"
                "The field has most likely been renamed in a demoparser2 "
                "update. Without the check the table would look valid but be "
                "empty. Update the prop names in adapters/demo_parser.py."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if steamid is None or side is None or tick is None:
                # Spectators and the unassigned are not parties to the round.
                continue
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    # Identity: the name and the clan from the same tick as
                    # the lineup. An empty string turns into ``None`` in
                    # ``_as_str`` -- empty is not a name.
                    "player_name": _as_str(row.get(_PLAYER_NAME)),
                    "clan_name": _as_str(row.get(_CLAN_NAME)),
                    "account": _as_int(row.get(_ACCOUNT)),
                    "cash_spent": _as_int(row.get(_CASH_SPENT)),
                    "equip_round_start": _as_int(row.get(_EQUIP_ROUND_START)),
                    "equip_current": _as_int(row.get(_EQUIP_CURRENT)),
                    "armor_value": _as_int(row.get(_ARMOR_VALUE)),
                    "inventory": _as_inventory(row.get(_INVENTORY)),
                    # A missing alive state turns into False here, and that
                    # is **deliberate** -- unlike at the sample points, where
                    # the same conversion is forbidden. Two reasons, and both
                    # are measured:
                    #
                    # 1. A pawnless player IS NOT alive. He is not on the
                    #    map, so False is the right answer for both figures
                    #    that use this (``survivors`` and
                    #    ``survivors_equip_prev``). At the sample points the
                    #    same value would be wrong, because there the row's
                    #    existence is itself a claim that the player is part
                    #    of the setup.
                    # 2. The row still does not drain into the economy
                    #    figures: _BUY_END_PROPS holds two PAWN fields
                    #    (``m_unCurrentEquipmentValue``,
                    #    ``m_unRoundStartEquipmentValue``), so _readable drops
                    #    a pawnless row from both the sums and their divisor
                    #    before being alive matters at all. Measured
                    #    2026-08-31, anubis_vs_RCAVE_VETERANS round 19:
                    #    players_buy_end is 4 where the neighbouring rounds
                    #    have 5.
                    #
                    # A library rename cannot pass through here silently:
                    # _read_sample_ticks reads the same prop and brings the
                    # whole run down before a single table is written.
                    "alive": _as_int(row.get(_LIFE_STATE)) == _ALIVE,
                    "team_score": _as_int(row.get(_TEAM_SCORE)),
                    "round_start_time": _as_float(row.get(_ROUND_START_TIME)),
                }
            )
        return dict(by_tick)

    @staticmethod
    def _tick_rate(
        by_tick: dict[int, list[dict[str, Any]]], freeze_ticks: list[int]
    ) -> tuple[float, bool]:
        """Compute the tick rate from the ratio of round start times to ticks.

        ``m_fRoundStartTime`` is the game's clock in seconds and the tick is
        the demo's own counter. The ratio between two rounds gives the tick
        rate directly; the median protects against a single restart. The
        demo's header does not carry the tick rate.

        Returns:
            ``(tick rate, was it measured)``. If no measurement could be made
            or the result is outside the sanity bounds,
            :data:`DEFAULT_TICK_RATE` and ``False`` are returned -- the stage
            tells the user, so that a default does not pass as a measurement.
        """
        observations: list[float] = []
        previous: tuple[int, float] | None = None
        for tick in freeze_ticks:
            rows = by_tick.get(tick) or []
            time_s = next(
                (
                    r["round_start_time"]
                    for r in rows
                    if r["round_start_time"] is not None
                ),
                None,
            )
            if time_s is None:
                continue
            if previous is not None:
                d_tick = tick - previous[0]
                d_time = time_s - previous[1]
                if d_tick > 0 and d_time > 0:
                    observations.append(d_tick / d_time)
            previous = (tick, time_s)
        if not observations:
            return DEFAULT_TICK_RATE, False
        rate = statistics.median(observations)
        if not TICK_RATE_MIN <= rate <= TICK_RATE_MAX:
            return DEFAULT_TICK_RATE, False
        rounded = round(rate)
        clean = float(rounded) if abs(rate - rounded) < 0.05 else float(rate)
        return clean, True

    @staticmethod
    def _lineup_keys(lineups: list[_Lineup]) -> list[str]:
        """The lineups' ids; they must not be the same.

        The same id would mean the teams cannot be told apart -- and then
        every per-team figure would be the sum of both.
        """
        lineup_keys = [lineup.key() for lineup in lineups]
        if lineup_keys[0] == lineup_keys[1]:
            raise ParseError(
                "Both teams came out with the same lineup id, so they cannot "
                "be told apart.\n"
                "The round boundary ticks show the same set of players on "
                "both sides. The demo is most likely corrupt."
            )
        return lineup_keys

    @staticmethod
    def _build_lineups_frame(
        lineups: list[_Lineup], lineup_keys: list[str]
    ) -> pl.DataFrame:
        """Build the lineups table: a row per (lineup, player).

        The source is the same set of anchor ticks from which the lineups were
        already identified (:meth:`_assign_sides`), so the demo is not read
        again. Rows are produced from exactly those players from whom
        ``lineup_key`` was computed -- so the table's set of players and the
        id cannot disagree.

        The name and the clan are the **most often observed** ones; a tie is
        settled alphabetically, so that the same demo gives the same result
        from one run to the next. A missing observation is ``null`` and not a
        substitute.

        Choosing the mode **loses the conflict**, so it is counted separately
        into the diagnostics (``lineup_name_conflicts``,
        ``lineup_clan_conflicts``). Without that, the assumption "one name and
        one clan per player" would be unchecked at run time: broken, it would
        look exactly the same in the table as intact.
        """
        rows: list[dict[str, Any]] = []
        # strict: a length difference would drop a lineup silently, and that
        # very invariant -- the table's set of players is the one lineup_key
        # was computed from -- is this table's whole promise.
        for lineup, key in zip(lineups, lineup_keys, strict=True):
            for player_id in sorted(lineup.members):
                rows.append(
                    {
                        "lineup_key": key,
                        "player_id": player_id,
                        "player_name": _most_observed(lineup.names.get(player_id)),
                        "clan_name": _most_observed(lineup.clans.get(player_id)),
                    }
                )
        schema: dict[str, Any] = {
            name: LINEUPS[name] for name in LINEUPS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    def _build_frame(
        self,
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        buy_ticks: list[int | None],
        window_ticks: list[int | None],
    ) -> tuple[pl.DataFrame, _ArmedCounters, _BuyWindowCounters]:
        """Build the rounds table.

        The economy values are read at the ``buy_ticks[index]`` tick (the end
        of the buy time), the winner and the survivors at
        ``segment.end_tick``. The anchor ``freeze_end_tick`` is still on the
        row, but no figures are read from it any more -- it is the round's
        time zero.

        ``window_ticks[index]`` is non-``None`` only when a death cut the
        window short: it is the tick that would have been measured without the
        cut, and it is used solely to work out whether any purchases fell
        behind the cut (``cash_spent`` grows only from purchases, not from
        deaths).
        """
        armed = _ArmedCounters()
        buy = _BuyWindowCounters()
        anchor_score = [
            _total_score(by_tick.get(s.freeze_end_tick or -1) or []) for s in segments
        ]
        end_score = [_total_score(by_tick.get(s.end_tick or -1) or []) for s in segments]

        # The equipment value of the previous round's survivors, per team.
        previous_saved: list[int | None] = [None, None]
        rows: list[dict[str, Any]] = []

        for index, segment in enumerate(segments):
            freeze_rows = by_tick.get(segment.freeze_end_tick or -1) or []
            end_rows = by_tick.get(segment.end_tick or -1) or []

            # The tick at the end of the buy time. The fallback is
            # deliberately the anchor and not an empty set: if the tick falls
            # past the end of the demo, the whole round's economy would
            # otherwise be null. The fallback is counted, because it is a
            # fault.
            buy_tick = buy_ticks[index]
            buy_rows = by_tick.get(buy_tick if buy_tick is not None else -1) or []
            fell_back = False
            if buy_tick is not None and not buy_rows and freeze_rows:
                buy.ticks_without_players += 1
                fell_back = True
                buy_tick = segment.freeze_end_tick
                buy_rows = freeze_rows

            # Unknown names are scanned from **both ticks** and from every
            # row, not only from the ones that qualify for the count. Two
            # reasons: a new weapon name can appear for the first time on a
            # player whose economy values are not readable (_readable drops
            # him), and a weapon may be held on only one of the ticks -- a
            # player who drops or swaps a weapon during the buy time would,
            # seen from only one moment, look as if the name had never been
            # there. The same name on the same player is still counted once
            # per round, so that reading two ticks does not double the
            # occurrence counts.
            seen_unknown: set[tuple[str, str]] = set()
            for row in (*freeze_rows, *buy_rows):
                for name in row.get("inventory") or ():
                    key = (row["steamid"], name)
                    if name not in KNOWN_INVENTORY_ITEMS and key not in seen_unknown:
                        seen_unknown.add(key)
                        armed.unknown_items[name] += 1

            # An unnumbered segment (a match restart) is not a round: it
            # produces no row. The anchor's inventories are still read above,
            # because a new weapon name may appear for the first time right
            # there. The segment stays in the list so that the previous
            # round's ``score_end`` is still read at **its** anchor -- that
            # reading is exactly where the knife round's reset shows.
            if segment.round_raw is None:
                # A restart zeroes the kit, so the next round does not inherit
                # the survivors' equipment from the round before it. The same
                # result as before: the ghost has no end tick, so its own sum
                # would be empty anyway.
                previous_saved = [None, None]
                continue

            score_start = anchor_score[index]
            if score_start is None:
                score_start = _score_before(index, segments, anchor_score, end_score)
            if score_start is None:
                score_start = end_score[index]

            score_end = anchor_score[index + 1] if index + 1 < len(segments) else None
            if score_end is None:
                score_end = end_score[index]

            # A window cut short by a death: it is always reported, and on top
            # of that **whether it cost anything** is checked. cash_spent
            # grows only from purchases and does not react to deaths, so its
            # growth between the cut and the end of the window is a direct
            # measure of how many purchases fell behind the measurement.
            #
            # A round that fell back is not recorded as a cut. The measurement
            # point is then the anchor rather than the cut point, so a
            # difference against the end of the window would be the empty
            # tick's doing and not the death's -- and that is already counted
            # under its own figure.
            window_tick = window_ticks[index]
            if window_tick is not None and not fell_back:
                missed, compared = _purchases_between(
                    buy_rows, by_tick.get(window_tick) or []
                )
                buy.cuts.append((segment.round_raw, missed))
                if not compared:
                    buy.unchecked_cuts.append(segment.round_raw)

            # A refunded purchase and the stale equipment value it leaves
            # behind. Only when the ticks differ: compared against the same
            # tick every value is trivially identical.
            if buy_tick is not None and buy_tick != segment.freeze_end_tick:
                refunds, stale = _refunds_and_stale_equipment(freeze_rows, buy_rows)
                buy.refunds += refunds
                buy.stale_equipment += stale

            saved_now: list[int | None] = [None, None]
            for team_index, side in enumerate(sides[index]):
                own_buy = _readable([r for r in buy_rows if r["side"] == side])
                own_end = [r for r in end_rows if r["side"] == side]
                alive = [r for r in own_end if r["alive"]]
                armed_count = _armed_count(own_buy)
                armored_count = _armored_count(own_buy)
                # An empty set is an anchorless round, not a read failure --
                # only the latter is counted, so that the figure reports a
                # prop fault and not a normal absence.
                if armed_count is None and own_buy:
                    armed.unreadable_rows += 1
                if armored_count is None and own_buy:
                    armed.armored_unreadable_rows += 1

                # Players who were readable at the anchor but are no longer
                # there at the measurement point. The sum and the divisor
                # shrink together, so per-player values stay right -- but the
                # team looks like it is playing short-handed, and that is a
                # different claim from "the connection dropped".
                if not fell_back:
                    at_anchor = _readable([r for r in freeze_rows if r["side"] == side])
                    if len(own_buy) < len(at_anchor):
                        buy.players_lost += len(at_anchor) - len(own_buy)
                        if not own_buy:
                            buy.sides_without_rows += 1
                saved_now[team_index] = (
                    _sum_or_zero([r["equip_current"] for r in alive])
                    if own_end
                    else None
                )
                rows.append(
                    {
                        "round_raw": segment.round_raw,
                        "round_no": None,
                        "lineup_key": lineup_keys[team_index],
                        "side": side,
                        "won": (
                            None
                            if segment.winner_side is None
                            else segment.winner_side == side
                        ),
                        "win_reason": segment.win_reason,
                        "money_buy_end": _sum_or_none(
                            [r["account"] for r in own_buy]
                        ),
                        "money_spent": _sum_or_none(
                            [r["cash_spent"] for r in own_buy]
                        ),
                        "equip_buy_end": _sum_or_none(
                            [r["equip_current"] for r in own_buy]
                        ),
                        "equip_round_start": _sum_or_none(
                            [r["equip_round_start"] for r in own_buy]
                        ),
                        # The thresholds are per player, so the divisor has to
                        # be observed rather than assumed: a short-handed team
                        # divided by five would look like an eco. The divisor
                        # is the same set as the sums (see _readable).
                        "players_buy_end": len(own_buy) or None,
                        # The same set and the same order on every run: the
                        # cash balances one player at a time, sorted
                        # descending. The values are already at hand -- until
                        # now they were merely summed, and the sum hides
                        # exactly what the half-buy rule is about.
                        MONEY_DISTRIBUTION_COLUMN: (
                            sorted(
                                (int(r["account"]) for r in own_buy),
                                reverse=True,
                            )
                            or None
                        ),
                        # The same set as the sums and the divisor. Two
                        # different divisors on the same row would be a fault
                        # that showed only in the report.
                        ARMED_COLUMN: armed_count,
                        # The same set, the same tick and the same armour
                        # reading as above -- a different condition. Two
                        # counts and not one, because they answer different
                        # questions: the one above is the half-buy's
                        # calibrated condition A, this one is "how many had
                        # armour".
                        ARMORED_COLUMN: armored_count,
                        "survivors": len(alive) if own_end else None,
                        "survivors_equip_prev": previous_saved[team_index],
                        "freeze_end_tick": segment.freeze_end_tick,
                        "buy_end_tick": buy_tick,
                        "tick_rate": tick_rate,
                        "status": (
                            "ok"
                            if segment.freeze_end_tick is not None
                            else "no_freeze_end"
                        ),
                        "score_start": score_start,
                        "score_end": score_end,
                    }
                )
            previous_saved = saved_now

        return self._typed_frame(rows), armed, buy

    @staticmethod
    def _assign_sides(
        segments: list[_Segment],
        by_tick: dict[int, list[dict[str, Any]]],
        lineups: list[_Lineup],
    ) -> list[tuple[str, str]]:
        """Decide which side each lineup is on for each round.

        Teams switch sides at half time and in overtime, so the side is no use
        as a team's id. The lineups are identified from the overlap of the
        sets of players: that survives both a side switch and a single
        substitution.

        A tie is **not settled by guessing**. If neither mapping wins, the
        previous round's mapping is used; if there is no previous one either,
        the parse stops. A silent assumption would attribute the wins to the
        wrong team.

        **A match restart is skipped entirely.** It is exactly the moment when
        the team and side state is at its least stable: players are moved,
        reconnect and have their sides set again. A single wrong reading there
        would stay in ``lineups`` permanently and could flip the sides for
        every round after it. The segment still gets an element of its own so
        that the list stays as long as the segments; it is not used for
        anything, because a restart produces no row in any table.

        Returns:
            Per round, the pair ``(lineup 0's side, lineup 1's side)``.
        """
        result: list[tuple[str, str]] = []
        previous: tuple[str, str] | None = None

        for segment in segments:
            if segment.round_raw is None:
                result.append(
                    _require_previous(previous, segment, "a match restart")
                )
                continue
            rows = (
                by_tick.get(segment.freeze_end_tick or -1)
                or by_tick.get(segment.end_tick or -1)
                or []
            )
            sets_by_side = {
                side: {r["steamid"] for r in rows if r["side"] == side}
                for side in ("T", "CT")
            }
            if not sets_by_side["T"] and not sets_by_side["CT"]:
                result.append(_require_previous(previous, segment, "no players"))
                continue

            if not lineups[0].members and not lineups[1].members:
                if not sets_by_side["T"] or not sets_by_side["CT"]:
                    raise ParseError(
                        "The first round identified had players on only one "
                        "side, so the lineups cannot be told apart.\n"
                        "The demo is most likely truncated at the start."
                    )
                lineups[0].observe(rows, "T")
                lineups[1].observe(rows, "CT")
                previous = ("T", "CT")
                result.append(previous)
                continue

            direct = sum(
                len(sets_by_side[side] & lineups[i].members)
                for i, side in enumerate(("T", "CT"))
            )
            swapped = sum(
                len(sets_by_side[side] & lineups[i].members)
                for i, side in enumerate(("CT", "T"))
            )
            if direct == swapped:
                sides = _require_previous(
                    previous, segment, "the lineups cannot be told apart"
                )
            else:
                sides = ("T", "CT") if direct > swapped else ("CT", "T")
            for i, side in enumerate(sides):
                lineups[i].observe(rows, side)
            previous = sides
            result.append(sides)
        return result

    @staticmethod
    def _typed_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Build the table with the contract's types.

        The types are given explicitly, because from null values alone Polars
        would infer the ``Null`` type and ``schemas.validate`` would reject
        the table. ``score_start`` and ``score_end`` are part of the port's
        contract (``ROUNDS_ADAPTER_COLUMNS``); ``stages.parse`` drops them
        before writing.
        """
        schema: dict[str, Any] = {
            name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    # -- Sample points -------------------------------------------------------

    def _sample_points(
        self,
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        sample_seconds: tuple[float, ...],
        all_deaths: list[tuple[int, str | None, str | None, str | None]],
    ) -> tuple[list[SamplePoint], int]:
        """Choose the moments at which the players' positions are read.

        The time points come straight from
        :func:`~pappascout.domain.sampling.sample_ticks`. The first contact is
        settled one round at a time, because its rule needs to know which side
        each player was on **this** round -- the sides switch at half time.

        Args:
            all_deaths: The ``player_death`` events ``_parse`` has already
                read for the buy window. They are handed in here rather than
                parsed again; ``fallback_death`` only decides whether a first
                contact may come from them.

        Returns:
            ``(the sample points, the events skipped for an unknown side)``.
        """
        # A match restart is not sampled: it has no round number for the rows
        # to attach to. The original index travels along, because ``sides``
        # and ``segments`` are in segment order -- without it every round
        # after the restart would read the previous segment's sides.
        sampled: list[tuple[int, _Segment]] = []
        bounds: list[RoundBounds] = []
        for index, segment in enumerate(segments):
            raw = segment.round_raw
            if raw is None:
                continue
            sampled.append((index, segment))
            bounds.append(
                RoundBounds(
                    round_raw=raw,
                    freeze_end_tick=segment.freeze_end_tick,
                    end_tick=segment.end_tick,
                )
            )
        points = sample_ticks(bounds, tick_rate, sample_seconds)

        hurt = self._damage_events(parser, "player_hurt", original_path)
        deaths = all_deaths if self.fallback_death else []
        if not hurt and not deaths:
            return _sorted_points(points), 0

        lineup_of = _lineup_index_by_player(lineups)
        unknown_sides = 0
        for position, round_bounds in enumerate(bounds):
            if not round_bounds.is_samplable:
                continue
            index = sampled[position][0]
            player_sides = _side_lookup(lineup_of, sides[index], segments[index], by_tick)
            own_hurt, a = _with_sides(hurt, round_bounds, player_sides)
            own_deaths, b = _with_sides(deaths, round_bounds, player_sides)
            unknown_sides += a + b
            tick = first_contact_tick(
                own_hurt,
                round_bounds,
                exclude_weapons=self.exclude_weapons,
                death_events=own_deaths,
                fallback_death=self.fallback_death,
            )
            if tick is None:
                continue
            assert round_bounds.freeze_end_tick is not None  # is_samplable
            t_s = seconds_since_freeze_end(tick, round_bounds.freeze_end_tick, tick_rate)
            points.append(
                SamplePoint(
                    round_raw=round_bounds.round_raw,
                    tick=tick,
                    sample_kind=FIRST_CONTACT_SAMPLE,
                    sample_t_s=t_s,
                    t_s=t_s,
                )
            )
        return _sorted_points(points), unknown_sides

    def _damage_events(
        self, parser: Any, name: str, original_path: Path
    ) -> list[tuple[int, str | None, str | None, str | None]]:
        """``player_hurt`` as four fields: ``(tick, attacker, victim, weapon)``.

        The first-contact rule needs only these, so the per-player fields are
        not asked for -- they would be 30 columns nothing reads.

        The sides are not attached here: the same player is on a different
        side before and after half time, so the mapping is per round.
        """
        rows, _ = self._damage_rows(parser, name, original_path)
        return [
            (r["tick"], r["attacker_id"], r["victim_id"], r["weapon"])
            for r in rows
        ]

    def _death_events(
        self, parser: Any, original_path: Path
    ) -> tuple[list[dict[str, Any]], int]:
        """``player_death`` including the victim's and the attacker's fields.

        One call, three users: the deaths table, the bounding of the buy
        window and the first-contact fallback. The per-player fields cost the
        same event read as going without them, so there is no separate light
        call -- and two calls could in addition give a different set of rows
        if the library ever changes.

        Returns:
            ``(rows, without a tick)``. The latter is the number of events
            whose tick was not readable; without a tick a death cannot be
            assigned to a round and ``t_s`` cannot be computed.
        """
        return self._damage_rows(
            parser, "player_death", original_path, player=DEATH_PLAYER_PROPS
        )

    def _damage_rows(
        self,
        parser: Any,
        name: str,
        original_path: Path,
        *,
        player: Sequence[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read a damage event and check that its columns are all there.

        One reader for both events. Two nearly identical copies would give
        **contradictory fix instructions for the same rename**: a lost
        ``user_steamid`` would tell the reader to update ``DAMAGE_COLUMNS`` on
        one path and ``DEATH_COLUMNS`` on the other. Here every missing column
        is named **together with its own list**, so the instruction is always
        the one that fixes the fault.

        A missing **column** is an error. Without the check the first contact
        would disappear silently, and the deaths table would come out without
        areas but structurally valid. A missing **event** is not an error -- a
        round can be decided without any damage, and the emptiness of the
        deaths table is checked by ``stages.parse``, which also sees the
        number of rounds.

        Args:
            player: The per-player fields the library returns under the
                prefixes ``user_*`` and ``attacker_*``. ``None`` reads only
                the :data:`DAMAGE_COLUMNS` fields.

        Returns:
            ``(rows, without a tick)``. A row is a dictionary in which
            ``tick``, ``attacker_id``, ``victim_id`` and ``weapon`` are always
            present; the other fields only if ``player`` was given. An event
            without a tick is **dropped and counted** -- every other reason
            for dropping is reported, and this must not be the exception.
        """
        frame = self._event(parser, name, original_path, player=player)
        if frame is None:
            # The event is not in the demo at all. That is possible (a round
            # can be decided without damage), so it is not an error.
            return [], 0

        # Column -> the list the developer has to fix. A pair rather than a
        # bare name: an instruction without the right list would send them
        # looking for the wrong constant.
        required: dict[str, str] = {c: "DAMAGE_COLUMNS" for c in DAMAGE_COLUMNS}
        if player is not None:
            required.update({c: "DEATH_COLUMNS" for c in DEATH_COLUMNS})
        missing = [
            f"{column} ({owner})"
            for column, owner in required.items()
            if column not in frame.columns
        ]
        if missing:
            raise ParseError(
                f"In demo {original_path.name}, event {name!r} is missing a "
                f"column: {', '.join(missing)}.\n"
                "Without it every event would be rejected silently and the "
                "result would claim that no round had a first contact -- or "
                "the deaths table would come out without areas but looking "
                "valid. The field has most likely been renamed in a "
                "demoparser2 update; update the list named in brackets in the "
                "file adapters/demo_parser.py."
            )

        rows: list[dict[str, Any]] = []
        without_tick = 0
        for row in frame.to_dict("records"):
            tick = _as_int(row.get("tick"))
            if tick is None:
                without_tick += 1
                continue
            entry: dict[str, Any] = {
                "tick": tick,
                "attacker_id": _as_str(row.get("attacker_steamid")),
                "victim_id": _as_str(row.get("user_steamid")),
                "weapon": _as_str(row.get("weapon")),
            }
            if player is not None:
                entry.update(
                    {
                        "victim_area": _as_str(row.get("user_last_place_name")),
                        "victim_x": _as_float(row.get("user_X")),
                        "victim_y": _as_float(row.get("user_Y")),
                        "victim_z": _as_float(row.get("user_Z")),
                        "victim_team": _as_int(row.get("user_team_num")),
                        "attacker_area": _as_str(
                            row.get("attacker_last_place_name")
                        ),
                        "attacker_x": _as_float(row.get("attacker_X")),
                        "attacker_y": _as_float(row.get("attacker_Y")),
                        "attacker_z": _as_float(row.get("attacker_Z")),
                        "attacker_team": _as_int(row.get("attacker_team_num")),
                    }
                )
            rows.append(entry)
        return rows, without_tick

    def _build_deaths_frame(
        self,
        death_rows: list[dict[str, Any]],
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        *,
        without_tick: int = 0,
    ) -> tuple[pl.DataFrame, _DeathCounts]:
        """Build a ``DEATHS``-shaped table from the deaths that were read.

        The round is settled by the **death tick**: the same segmentation as
        in utility (:func:`_round_windows`), so a death belongs to the round
        within whose boundaries it falls. A death outside every round gets no
        ``t_s`` and therefore no row; a knife-round death gets both and is
        dropped only in ``stages.parse``'s numbering.

        The side and the lineup come from the round's own side map, and the
        event's ``team_num`` is the fallback for a player the round does not
        know. A missing victim side drops the row -- a death that belongs to
        neither team is no use as the target of a join. A missing attacker
        side drops nothing: the attacker's own observations are readable, and
        emptying them would lose them.

        Returns:
            ``(the table, the figures)``. The table is empty but conforms to
            the contract if no death falls inside a round.
        """
        if not death_rows:
            return (
                self._typed_deaths_frame([]),
                _DeathCounts(without_tick=without_tick),
            )

        windows = _round_windows(segments)
        starts = [window[0] for window in windows]
        lineup_of = _lineup_index_by_player(lineups)
        sides_by_round: dict[int, dict[str, str]] = {}
        keys_by_round: dict[int, dict[str, str]] = {}

        rows: list[dict[str, Any]] = []
        outside = 0
        without_victim = 0
        without_victim_side = 0
        attacker_without_side = 0

        for death in death_rows:
            index = _round_of_tick(starts, windows, death["tick"])
            if index is None:
                outside += 1
                continue
            if index not in sides_by_round:
                sides_by_round[index] = _side_lookup(
                    lineup_of, sides[index], segments[index], by_tick
                )
                keys_by_round[index] = _keys_by_side(
                    sides[index], lineup_keys, segments[index]
                )
            player_sides = sides_by_round[index]
            keys = keys_by_round[index]

            # Two different reasons, two different counters. An event without
            # a victim is not a failure of side inference, and combined it
            # would look like a fault that is not there.
            if death["victim_id"] is None:
                without_victim += 1
                continue
            victim_side = _resolve_side(
                death["victim_id"], death["victim_team"], player_sides
            )
            if victim_side is None:
                without_victim_side += 1
                continue

            attacker_side: str | None = None
            if death["attacker_id"] is not None:
                attacker_side = _resolve_side(
                    death["attacker_id"], death["attacker_team"], player_sides
                )
                if attacker_side is None:
                    attacker_without_side += 1

            segment = segments[index]
            freeze_end = segment.freeze_end_tick
            if freeze_end is None:  # pragma: no cover - _round_windows ensures
                raise ParseError(
                    "A death was assigned to a round "
                    f"(round_raw={segment.round_raw}) that has no anchor.\n"
                    "Without one t_s cannot be computed. The demo is most "
                    "likely corrupt."
                )
            has_attacker = death["attacker_id"] is not None
            rows.append(
                {
                    "round_raw": segment.round_raw,
                    "round_no": None,
                    "t_s": seconds_since_freeze_end(
                        death["tick"], freeze_end, tick_rate
                    ),
                    "victim_id": death["victim_id"],
                    "victim_lineup_key": keys[victim_side],
                    "victim_side": victim_side,
                    "victim_x": death["victim_x"],
                    "victim_y": death["victim_y"],
                    "victim_z": death["victim_z"],
                    "victim_area": death["victim_area"],
                    "attacker_id": death["attacker_id"],
                    "attacker_lineup_key": (
                        None if attacker_side is None else keys[attacker_side]
                    ),
                    "attacker_side": attacker_side,
                    # Every attacker field of an attackerless death is empty.
                    # The coordinates and the area are read only if there is
                    # an attacker: a death caused by the world has no
                    # position, and a stray value left by the library would
                    # look like an attacker.
                    "attacker_x": death["attacker_x"] if has_attacker else None,
                    "attacker_y": death["attacker_y"] if has_attacker else None,
                    "attacker_z": death["attacker_z"] if has_attacker else None,
                    "attacker_area": (
                        death["attacker_area"] if has_attacker else None
                    ),
                    "weapon": death["weapon"],
                }
            )

        counts = _DeathCounts(
            without_tick=without_tick,
            outside_rounds=outside,
            without_victim=without_victim,
            without_victim_side=without_victim_side,
            attacker_without_side=attacker_without_side,
        )
        return self._typed_deaths_frame(rows), counts

    @staticmethod
    def _typed_deaths_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Build the deaths table with the contract's types and a stable order.

        The columns are picked **by name** and not in the row dictionary's
        order, for the same reason as in the events table: the table has three
        consecutive Float32 columns for the victim and three for the attacker,
        and ``orient="row"`` would swap them silently if ``DEATHS``'s key
        order ever changed.

        The sort key is ``(round_raw, t_s, victim_id)``. ``victim_id`` is in
        it because two team-mates can die on the same tick -- without it the
        row order would depend on the order the library happened to return the
        events in, and the same demo would produce different bytes on
        different runs.
        """
        schema: dict[str, Any] = {
            name: DEATHS[name] for name in DEATHS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        columns = {name: [row[name] for row in rows] for name in schema}
        return pl.DataFrame(columns, schema=schema).sort(
            "round_raw", "t_s", "victim_id"
        )

    def _build_ticks_frame(
        self,
        points: list[SamplePoint],
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
    ) -> tuple[pl.DataFrame, int, dict[int, _SampleTickCounts], int]:
        """Read the players' positions at the sample ticks and build the table.

        A row is produced for **every** player, the dead included: filtering
        the dead is aggregation's job (AD-10), not the parse's. An unknown
        area is left ``null``, but the coordinates are stored anyway -- the row
        is not dropped silently.

        Returns:
            ``(the table, the number of partial sample points, the per-tick
            row counts, the number of sample points missed entirely)``.

            **A partial sample point** is one that yielded fewer players than
            the demo's best point. The figure is reported because a systematic
            prop fault would otherwise show only as skewed aggregates. A
            pawnless row is **one reason** a sample point comes out partial,
            and that is exactly why both figures are reported.

            **A sample point missed entirely** is a different thing and is not
            included in the partial ones: it yielded no row at all, because
            every row was pawnless. It is more serious than a partial point,
            so it must not disappear among them -- nor go uncounted.
        """
        if not points:
            return self._typed_ticks_frame([]), 0, {}, 0

        wanted = sorted({p.tick for p in points})
        by_tick, tick_counts = self._read_sample_ticks(
            parser, wanted, original_path
        )
        points_without_pawn = 0
        # sides is in segment order, but a sample point knows only the
        # round_raw value, so a mapping back to the segment index is needed.
        index_by_raw = {
            s.round_raw: index
            for index, s in enumerate(segments)
            if s.round_raw is not None
        }
        # Lazily and not eagerly: _keys_by_side raises a ParseError if both
        # lineups came out on the same side. Eager construction would give a
        # restart the power to bring the whole run down even though it
        # produces no row in any table -- and the error message would report
        # its round_raw as ``None``, which helps the reader with nothing.
        keys_per_round: dict[int, dict[str, str]] = {}

        rows: list[dict[str, Any]] = []
        players_per_point: list[int] = []
        for point in points:
            segment_index = index_by_raw.get(point.round_raw)
            if segment_index is None:  # pragma: no cover - sample_ticks takaa
                continue
            side_keys = keys_per_round.get(segment_index)
            if side_keys is None:
                side_keys = _keys_by_side(
                    sides[segment_index], lineup_keys, segments[segment_index]
                )
                keys_per_round[segment_index] = side_keys
            tick_rows = by_tick.get(point.tick, ())
            counts = tick_counts.get(point.tick, _SampleTickCounts())
            if (
                not tick_rows
                and counts.without_pawn
                and counts.without_pawn == counts.seen
            ):
                # The whole sample point is pawnless: every row existed but
                # nobody had a character on the map. That is not a fault but
                # an observation -- the demo did not return nothing, there
                # were no players. The run is not brought down; the point is
                # missed and both the rows and the point itself show under
                # their own counters.
                #
                # The condition requires pawnlessness to explain the tick
                # **entirely**. A bare "even one pawnless row" would silence a
                # hard error by chance: a tick that lost nine rows as
                # spectators and one as pawnless is still a fault.
                points_without_pawn += 1
                continue
            if not tick_rows:
                raise ParseError(
                    f"The sample point of demo {original_path.name} "
                    f"(round_raw={point.round_raw}, {point.sample_kind}, "
                    f"t={point.sample_t_s:g} s, tick={point.tick}) yielded no "
                    "player rows at all.\n"
                    "The tick is inside the round's boundaries, so an empty "
                    "result means the demo is corrupt or demoparser2 returns "
                    "nothing at this tick. The sample point would be counted "
                    "in the figures but be missing from the table."
                )
            players_per_point.append(len(tick_rows))
            for row in tick_rows:
                side = row["side"]
                rows.append(
                    {
                        "round_raw": point.round_raw,
                        "round_no": None,
                        "player_id": row["steamid"],
                        "lineup_key": side_keys[side],
                        "side": side,
                        "sample_kind": point.sample_kind,
                        "sample_t_s": point.sample_t_s,
                        "t_s": point.t_s,
                        "x": row["x"],
                        "y": row["y"],
                        "z": row["z"],
                        "area": row["area"],
                        "is_alive": row["alive"],
                    }
                )

        # The expected player count is read from the demo itself:
        # [thresholds] is not visible to this stage (AD-3), so roster_size
        # cannot be used.
        full_count = max(players_per_point, default=0)
        partial = sum(1 for count in players_per_point if count < full_count)
        return (
            self._typed_ticks_frame(rows),
            partial,
            tick_counts,
            points_without_pawn,
        )

    def _read_sample_ticks(
        self, parser: Any, ticks: list[int], original_path: Path
    ) -> tuple[dict[int, list[dict[str, Any]]], dict[int, _SampleTickCounts]]:
        """Read the position props at the given ticks and group them by tick.

        **A pawnless player is not a party to the round.** The difference
        between the controller and the pawn is measured in the module's
        documentation. A row where the controller is there but **every**
        :data:`SAMPLE_PAWN_PROPS` field is empty is about a player who is not
        on the map -- his row is skipped like a spectator's, recorded under a
        counter of its own.

        The skip requires **all** of the pawn fields to be missing and not
        merely the alive state: if it fired on an empty ``m_lifeState``
        alone, it would eat the very fault the guard below exists against. A
        demoparser2 update that renamed the field would produce an empty value
        for every row, every row would be skipped, and the setup would empty
        out silently. Requiring all the fields separates "the player is not
        there" from "the field was renamed".

        **The guard itself stays in place for a row that has a pawn**, and in
        the original words: ``is_alive`` is not nullable, so a missing value
        would silently turn into ``False`` and a living player would disappear
        from the aggregation. An unknown area may stay null, but this may not.

        **The skip rests on the assumption that a missing pawn field arrives
        empty.** demoparser2 gives a missing value as ``None`` or as ``NaN``,
        and both end up as ``None`` in the ``_as_*`` converters; an empty
        ``m_szLastPlaceName`` is already ``None``. If the library ever returns
        zero coordinates or a zero alive state for a pawnless player, the skip
        **does not fire** and the guard below brings the run down as it did
        before Story 2.10. That is the deliberate direction: the number zero
        is an observation like any other, and it must not be read as absence.

        Returns:
            ``(the rows grouped by tick, :class:`_SampleTickCounts` per
            tick)``.

            The latter is **per tick and not one total**, because the caller
            needs it for three different things: the total goes into the
            diagnostics, the per-tick figure separates an empty tick's two
            different causes ("the demo returned nothing" versus "every row
            was pawnless"), and the same tick can be read twice from different
            call paths -- summed, the same row would be counted twice.
        """
        if not ticks:
            return {}, {}
        try:
            frame = parser.parse_ticks(list(SAMPLE_TICK_PROPS), ticks=ticks)
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"The sample points of demo {original_path.name} could not be "
                f"read: {exc}\n"
                "The file is most likely corrupt, or this demoparser2 version "
                "does not know these fields. Run: uv sync"
            ) from exc

        received = set(getattr(frame, "columns", ()))
        missing = [
            name
            for name in (*SAMPLE_TICK_PROPS, "tick", "steamid")
            if name not in received
        ]
        if missing:
            raise ParseError(
                "demoparser2 did not return every sample point field from "
                f"demo {original_path.name}. Missing: {', '.join(missing)}.\n"
                "The field has most likely been renamed in a demoparser2 "
                "update. Without the check the sample point table would look "
                "valid but be empty or have no positions. Update the prop "
                "names in adapters/demo_parser.py."
            )

        by_tick: dict[int, list[dict[str, Any]]] = defaultdict(list)
        seen: Counter[int] = Counter()
        without_pawn: Counter[int] = Counter()
        for row in frame.to_dict("records"):
            steamid = _as_str(row.get("steamid"))
            side = TEAM_SIDES.get(_as_int(row.get(_TEAM_NUM)) or -1)
            tick = _as_int(row.get("tick"))
            if tick is not None:
                # The rows seen are counted **before** any skip: only against
                # them can it be said whether pawnlessness explained an empty
                # tick entirely.
                seen[tick] += 1
            if steamid is None or side is None or tick is None:
                # Spectators and the unassigned are not parties to the round.
                continue
            life_state = _as_int(row.get(_LIFE_STATE))
            # An empty area name is the game's way of saying "no named area".
            # It is kept as null; the coordinates still say where.
            area = _as_str(row.get(_PLACE_NAME))
            x = _as_float(row.get(_X))
            y = _as_float(row.get(_Y))
            z = _as_float(row.get(_Z))
            # "All pawn fields empty" read through :data:`SAMPLE_PAWN_PROPS`
            # and not as a chain written by hand. A new pawn prop without a
            # value in this dictionary is a ``KeyError`` and not a silently
            # looser skip -- and ``KeyError`` is the right reaction, because
            # the skip's coverage is the whole condition of the fix.
            pawn_fields = {
                _LIFE_STATE: life_state,
                _PLACE_NAME: area,
                _X: x,
                _Y: y,
                _Z: z,
            }
            if all(pawn_fields[name] is None for name in SAMPLE_PAWN_PROPS):
                # A pawnless player: the controller is there but the
                # character is not on the map. He is not a party to this tick,
                # so the row is skipped like a spectator's -- neither the
                # alive state nor the position is guessed. The counter keeps
                # the drop visible: a missing player shrinks the setup, and
                # the reader has to see it.
                without_pawn[tick] += 1
                continue
            if life_state is None:
                # is_alive is not nullable, so a missing value would silently
                # turn into False and a living player would disappear from the
                # aggregation. An unknown area may stay null, but this may
                # not.
                raise ParseError(
                    f"In demo {original_path.name}, tick {tick} is missing "
                    f"player {steamid}'s {_LIFE_STATE}.\n"
                    "Being alive is a mandatory observation: a missing value "
                    "would become 'dead', and the player would disappear from "
                    "the setup silently. Check the demoparser2 version."
                )
            by_tick[tick].append(
                {
                    "steamid": steamid,
                    "side": side,
                    "area": area,
                    "x": x,
                    "y": y,
                    "z": z,
                    "alive": life_state == _ALIVE,
                }
            )
        return dict(by_tick), {
            tick: _SampleTickCounts(seen=count, without_pawn=without_pawn[tick])
            for tick, count in seen.items()
        }

    @staticmethod
    def _typed_ticks_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Build the sample point table with the contract's types.

        The types are given explicitly for the same reason as in the rounds
        table: from null values alone Polars would infer the ``Null`` type.
        """
        schema: dict[str, Any] = {name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS}
        if not rows:
            return pl.DataFrame(schema=schema)
        return pl.DataFrame(rows, schema=schema, orient="row")

    # -- Utility -------------------------------------------------------------

    def _build_events_frame(
        self,
        parser: Any,
        original_path: Path,
        segments: list[_Segment],
        sides: list[tuple[str, str]],
        lineup_keys: list[str],
        lineups: list[_Lineup],
        by_tick: dict[int, list[dict[str, Any]]],
        tick_rate: float,
        cloud: pl.DataFrame,
    ) -> tuple[pl.DataFrame, _UtilityCounts, dict[int, _SampleTickCounts]]:
        """Read the trajectories and build an ``EVENTS``-shaped table of them.

        The order is deliberate: a trajectory is reduced to its endpoints
        **before** anything else is done, which shrinks 1.55 million rows to
        about 750 rather than carrying them through the stages whole.

        The round is settled by the **throw**: a smoke thrown at the end of a
        round belongs to the round it left from, even if it burns out only on
        the next one's side. Both rows therefore get the same ``round_raw``,
        and the detonation's ``t_s`` may exceed the round's duration -- that
        is a correct observation and not an error.

        The area is two kinds of information. The thrower has his own
        ``m_szLastPlaceName`` at the same tick, so a throw's area is an
        **observation** (``area_source = "observed"``). A grenade has no area
        name, so a detonation's area is read from the **point cloud**: the
        nearest cell's area (``"point_cloud"``), and the distance is stored so
        that a consumer can tell a sure hit from a distant estimate.

        The detonation areas are resolved **as one batch** and not a row at a
        time: the comparison is every detonation against every cell, and a
        batch lets Polars do it once instead of in hundreds of small calls.

        Ticks are read only for the **throws**. There is nothing left to read
        at a detonation's tick: its area comes from the cloud and not from who
        happened to be nearby.

        Returns:
            ``(the table, the figures)``. The table is empty but conforms to
            the contract if the demo had no grenades thrown at all.
        """
        raw = self._read_grenades(parser, original_path)
        if raw.is_empty():
            return self._typed_events_frame([]), _UtilityCounts(), {}

        endpoints, without_thrower = self._endpoints(raw, tick_rate, original_path)
        if endpoints.is_empty():
            return (
                self._typed_events_frame([]),
                _UtilityCounts(without_thrower=without_thrower),
            )
        unknown_type = _unknown_type_count(endpoints)
        endpoints, fire_unresolved = _name_fire_grenades(
            endpoints, raw, trajectory_gap_ticks(tick_rate)
        )

        windows = _round_windows(segments)
        starts = [window[0] for window in windows]

        round_of_throw: dict[int, int] = {}
        outside = 0
        throws = endpoints.filter(pl.col("event_kind") == THROWN)
        for row in throws.iter_rows(named=True):
            index = _round_of_tick(starts, windows, row["tick"])
            if index is None:
                outside += 1
                continue
            round_of_throw[row["grenade_no"]] = index

        lineup_of = _lineup_index_by_player(lineups)
        sides_by_round: dict[int, dict[str, str]] = {}
        keys_by_round: dict[int, dict[str, str]] = {}

        selected: list[dict[str, Any]] = []
        unknown_side_count = 0
        for row in endpoints.iter_rows(named=True):
            index = round_of_throw.get(row["grenade_no"])
            if index is None:
                continue
            if index not in sides_by_round:
                sides_by_round[index] = _side_lookup(
                    lineup_of, sides[index], segments[index], by_tick
                )
                keys_by_round[index] = _keys_by_side(
                    sides[index], lineup_keys, segments[index]
                )
            side = sides_by_round[index].get(row["thrower_id"])
            if side is None:
                # The grenade is dropped entirely but counted once -- at the
                # throw, so that the figure counts grenades and not rows.
                if row["event_kind"] == THROWN:
                    unknown_side_count += 1
                continue
            selected.append(
                {
                    **row,
                    "_segment": index,
                    "_side": side,
                    "_lineup": keys_by_round[index][side],
                }
            )

        wanted = sorted({r["tick"] for r in selected if r["event_kind"] == THROWN})
        # An empty list **must not** go to parse_ticks: to demoparser2 it
        # means "every tick". The point cloud reads the whole tick series
        # deliberately and once; this call must not do it a second time by
        # accident. The situation arises if every grenade is dropped as
        # outside the rounds or for an unknown side.
        positions, throw_tick_counts = (
            self._read_sample_ticks(parser, wanted, original_path)
            if wanted
            else ({}, {})
        )
        # An empty tick is a **fault** only when pawnlessness does not explain
        # it: then the thrower's own area could not even be attempted. A
        # wholly pawnless tick is an observation by the same rule as at the
        # sample points, and it is already counted among the pawnless rows --
        # the same phenomenon must not be a fault on one path and an
        # observation on the other.
        empty_ticks = sum(
            1
            for tick in wanted
            if not positions.get(tick)
            and not _is_wholly_pawnless(throw_tick_counts.get(tick))
        )
        # The detonation areas in one go: the cloud does not change between
        # rows, so a lookup per row would do the same work again and again.
        detonation_areas = self._detonation_areas(selected, cloud)

        rows: list[dict[str, Any]] = []
        late_detonations = 0
        throwers_without_row = 0
        for r in selected:
            segment = segments[r["_segment"]]
            freeze_end = segment.freeze_end_tick
            end_tick = segment.end_tick
            if freeze_end is None or end_tick is None:
                # _round_windows is built only from rounds that have an
                # anchor, so this cannot happen. The check is still right and
                # not an assert: an assert disappears under python -O, and the
                # consequence would be a TypeError in the middle of parsing a
                # 233 MB demo.
                raise ParseError(
                    f"A grenade in demo {original_path.name} was assigned to "
                    f"a round (round_raw={segment.round_raw}) that has no "
                    "anchor or no end tick.\n"
                    "Without them t_s cannot be computed. The demo is most "
                    "likely corrupt."
                )
            if r["event_kind"] == DETONATE and r["tick"] > end_tick:
                late_detonations += 1
            if r["event_kind"] == THROWN:
                area, source, distance, thrower_found = self._throw_area(
                    r, positions.get(r["tick"], ())
                )
                if not thrower_found:
                    # The thrower was not among the rows: the area is left
                    # empty and nobody else can supply it. Since Story 2.10
                    # one reason for this is a pawnless thrower whose row is
                    # skipped -- before that the case brought the run down.
                    throwers_without_row += 1
            else:
                area, distance = detonation_areas.get(r["grenade_no"], (None, None))
                source = "point_cloud" if area is not None else None
            rows.append(
                {
                    "round_raw": segment.round_raw,
                    "round_no": None,
                    "event_kind": r["event_kind"],
                    "grenade_no": r["grenade_no"],
                    "grenade_entity_id": r["grenade_entity_id"],
                    "grenade_type": r["grenade_type"],
                    "thrower_id": r["thrower_id"],
                    "lineup_key": r["_lineup"],
                    "side": r["_side"],
                    "t_s": seconds_since_freeze_end(r["tick"], freeze_end, tick_rate),
                    "x": r["x"],
                    "y": r["y"],
                    "z": r["z"],
                    "area": area,
                    "area_source": source,
                    "snap_distance": distance,
                }
            )

        frame = self._typed_events_frame(rows)
        counts = _UtilityCounts(
            without_thrower=without_thrower,
            outside_rounds=outside,
            unknown_side=unknown_side_count,
            unknown_type=unknown_type,
            fire_type_unresolved=fire_unresolved,
            detonating_after_round=late_detonations,
            ticks_without_players=empty_ticks,
            sharing_an_entity_id=_shared_entity_id_count(frame),
            throwers_without_row=throwers_without_row,
        )
        return frame, counts, throw_tick_counts

    @staticmethod
    def _throw_area(
        row: dict[str, Any],
        tick_players: Sequence[dict[str, Any]],
    ) -> tuple[str | None, str | None, float | None, bool]:
        """A throw's area: the thrower's own ``m_szLastPlaceName`` at that tick.

        It is an **observation** and not a derivation, which is why this path
        does not touch the point cloud at all: the cloud would give the
        nearest cell's area even though the right answer can be read off the
        thrower himself. A dead player counts too -- he threw the grenade
        while alive, and the row gives his own area.

        ``snap_distance`` is always ``None``: an observation has no distance.

        Returns:
            ``(area, source, distance, was the thrower found)``. The first
            three are empty if the thrower is not among the tick's rows -- an
            observation is not replaced by an estimate.

            **The fourth tells two kinds of emptiness apart.** "The thrower
            was found, but the game has no name for his area" is an
            observation; "the thrower was not among the rows" is a fault, and
            it has a counter of its own
            (:attr:`_UtilityCounts.throwers_without_row`). Without this flag
            they would look exactly the same to the caller.
        """
        for player in tick_players:
            if player["steamid"] == row["thrower_id"]:
                area = player["area"]
                return area, ("observed" if area is not None else None), None, True
        return None, None, None, False

    def _detonation_areas(
        self, selected: Sequence[dict[str, Any]], cloud: pl.DataFrame
    ) -> dict[int, tuple[str | None, float | None]]:
        """Name every detonation from the point cloud in one go.

        The key is ``grenade_no``, which is unique across the whole demo and
        on which a trajectory has **at most one** detonation row -- the game's
        own ``grenade_entity_id`` would be no use, because it is recycled.

        **A late detonation is not an exception.** In Story 2.2 a grenade that
        detonated after the round ended was left without an area, because the
        method of the day would have read the area off the players standing in
        the next round's spawn. The point cloud does not depend on the moment,
        so the reason went away with the method and the row gets its area like
        every other.

        Returns:
            ``grenade_no -> (area, distance)``. The distance is kept even when
            the area fell behind the threshold; both are ``None`` only if the
            cloud is empty or there are no coordinates.
        """
        rows = [r for r in selected if r["event_kind"] == DETONATE]
        if not rows:
            return {}
        points = pl.DataFrame(
            {
                "point_id": [int(r["grenade_no"]) for r in rows],
                "x": [r["x"] for r in rows],
                "y": [r["y"] for r in rows],
                "z": [r["z"] for r in rows],
            },
            schema={
                "point_id": pl.Int64,
                "x": pl.Float64,
                "y": pl.Float64,
                "z": pl.Float64,
            },
        )
        named = nearest_cells(
            points,
            cloud,
            grid_units=self.callout_grid_units,
            z_weight=self.callout_z_weight,
            z_tolerance_units=self.callout_z_tolerance_units,
            max_units=self.area_snap_units,
        )
        return {
            int(row["point_id"]): (row["area"], row["distance"])
            for row in named.iter_rows(named=True)
        }

    # -- The point cloud -----------------------------------------------------

    def _build_callout_cloud(
        self, parser: Any, original_path: Path
    ) -> tuple[pl.DataFrame, _CloudCounts]:
        """Read the whole demo's tick series and reduce it to a grid.

        This is the module's **only** whole-demo tick read, and it is
        deliberate: the question is "where on the map has anyone stood and
        what area is each position in", and a sample of a few anchors does not
        answer it. The data is reduced to a grid immediately, so a million
        rows do not travel on.

        An empty cloud **is not an error**: it is a demo from which no alive
        row in a named area could be got. Every detonation area is then left
        empty, the run continues, and the reason travels through the
        diagnostics into the run's summary.

        Returns:
            ``(the point cloud, the figures)``.
        """
        frame = self._read_cloud_ticks(parser, original_path)
        if frame is None:
            return self._typed_callouts_frame(empty_point_cloud()), _CloudCounts(
                empty_reason=(
                    "demoparser2 returned no tick rows at all from the whole "
                    "demo"
                )
            )
        # The conversion and the reduction are inside the same error wrapper:
        # both raise the library's or the domain's own error type, and the
        # port's contract promises a ParseError. Without the wrapper a
        # demoparser2 type change would show as a bare PolarsError in the
        # middle of parsing 400 MB.
        try:
            observations = _cloud_observations(frame)
            # The pandas frame is no longer needed. It does not return memory
            # to the operating system -- the measured working set does not
            # shrink -- but it lets the allocator reuse the space.
            del frame
            rows_read = observations.height
            cloud = build_point_cloud(
                observations, grid_units=self.callout_grid_units
            )
            del observations
        except (ValueError, pl.exceptions.PolarsError) as exc:
            raise ParseError(
                f"The point cloud of demo {original_path.name} could not be "
                f"built: {exc}\n"
                "Either demoparser2 returned a field in an unexpected type or "
                "[parse].callout_grid_units is invalid. Check the settings "
                "and run: uv sync"
            ) from exc

        reason = None
        if cloud.is_empty():
            reason = (
                f"{rows_read} tick rows were read, but not one of them had a "
                "living player in a named area"
                if rows_read
                else "demoparser2 returned no tick rows at all from the whole "
                "demo"
            )
        return self._typed_callouts_frame(cloud), _CloudCounts(
            rows_read=rows_read, empty_reason=reason
        )

    @staticmethod
    def _typed_callouts_frame(cloud: pl.DataFrame) -> pl.DataFrame:
        """Give the point cloud the port contract's columns and types.

        The domain builds the cloud with its own types; this binds it to the
        ``CALLOUT_CLOUD`` contract. Without the binding, a change to the
        schema's types would show only in the stage's ``validate``, and the
        error message would blame the stage for work the adapter left undone.
        """
        schema: dict[str, Any] = {
            name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS
        }
        return cloud.select(CALLOUTS_ADAPTER_COLUMNS).cast(schema)

    def _read_cloud_ticks(self, parser: Any, original_path: Path) -> Any:
        """Read :data:`CLOUD_TICK_PROPS` from the whole demo and check columns.

        An empty result is not an error -- the point cloud is then left empty
        and the reason is reported. A missing **column** is an error: without
        the check the cloud would be empty and indistinguishable from a demo
        in which nobody moved, and every detonation would be left without an
        area with nothing saying why.
        """
        try:
            frame = parser.parse_ticks(list(CLOUD_TICK_PROPS))
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"The point cloud of demo {original_path.name} could not be "
                f"read: {exc}\n"
                "The file is most likely corrupt, or this demoparser2 version "
                "does not know these fields. Run: uv sync"
            ) from exc

        if frame is None or not hasattr(frame, "columns"):
            return None

        # COLUMNS BEFORE EMPTINESS. A renamed field can produce a frame that
        # has the columns but zero rows, and checking emptiness first would
        # turn a contract breach into the observation "the demo had no ticks".
        # That is exactly the silent interpretation this guard prevents.
        missing = [name for name in CLOUD_TICK_PROPS if name not in frame.columns]
        if missing:
            raise ParseError(
                "demoparser2 did not return every point cloud field from demo "
                f"{original_path.name}. Missing: {', '.join(missing)}.\n"
                "The field has most likely been renamed in a demoparser2 "
                "update. Without the check the point cloud would be empty and "
                "every detonation area null -- and nothing would say why. "
                "Update CLOUD_TICK_PROPS in adapters/demo_parser.py."
            )
        if len(frame) == 0:
            return None
        return frame

    def _endpoints(
        self, raw: pl.DataFrame, tick_rate: float, original_path: Path
    ) -> tuple[pl.DataFrame, int]:
        """Call the domain's reduction and translate its errors for the user.

        A missing column can surface in two places: Polars raises a
        ``ColumnNotFoundError`` in the conversion already, and
        ``grenade_endpoints`` raises a ``ValueError`` in its own check. Either
        is the same fault as :meth:`_read_grenades`'s own check finds, so all
        three have to look the same to the user -- and not like a bare
        traceback.
        """
        try:
            return grenade_endpoints(
                _trajectory_frame(raw),
                max_gap_ticks=trajectory_gap_ticks(tick_rate),
            )
        except (ValueError, pl.exceptions.PolarsError) as exc:
            raise ParseError(
                f"The trajectories of demo {original_path.name} could not be "
                f"reduced: {exc}\n"
                "The field has most likely been renamed in a demoparser2 "
                "update. Update GRENADE_COLUMNS in "
                "adapters/demo_parser.py."
            ) from exc

    def _read_grenades(self, parser: Any, original_path: Path) -> pl.DataFrame:
        """Read ``parse_grenades()`` and check that the columns are all there.

        An empty result is not an error: the demo may simply have had no
        grenades thrown in it. A missing **column** is an error, because the
        result would then be empty and indistinguishable from a demo without
        utility.
        """
        try:
            frame = parser.parse_grenades()
        except Exception as exc:  # noqa: BLE001 - the library's own error type
            raise ParseError(
                f"The trajectories of demo {original_path.name} could not be "
                f"read: {exc}\n"
                "The file is most likely corrupt, or this demoparser2 version "
                "does not know the parse_grenades method. Run: uv sync"
            ) from exc

        if frame is None or not hasattr(frame, "columns") or len(frame) == 0:
            return pl.DataFrame()

        missing = [name for name in GRENADE_COLUMNS if name not in frame.columns]
        if missing:
            raise ParseError(
                "demoparser2 did not return every trajectory field from demo "
                f"{original_path.name}. Missing: {', '.join(missing)}.\n"
                "The field has most likely been renamed in a demoparser2 "
                "update. Without the check the utility table would be empty "
                "and would look like a demo in which no grenade was thrown. "
                "Update GRENADE_COLUMNS in adapters/demo_parser.py."
            )
        return _as_polars(frame, GRENADE_COLUMNS)

    @staticmethod
    def _typed_events_frame(rows: list[dict[str, Any]]) -> pl.DataFrame:
        """Build the events table with the contract's types and a stable order.

        The sort is explicit: the joins made along the way do not preserve row
        order, and the same demo would otherwise produce different bytes on
        different runs. ``event_kind`` is an Enum, so its order is the order
        of the enumeration -- the throw before the detonation.

        The second key is ``grenade_no`` and not the game's own id, and there
        are two reasons for that. It is **unique**, so the key determines the
        order completely rather than depending on the sort's stability. And it
        keeps a trajectory's two rows **side by side**: sorted by the game's
        id, all the throws of a recycled id would come before all its
        detonations, and the pair would break apart into different places in
        the table.
        """
        schema: dict[str, Any] = {
            name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS
        }
        if not rows:
            return pl.DataFrame(schema=schema)
        # The columns are picked **by name**, not in the row dictionary's
        # order. ``orient="row"`` would read the values in order, and two
        # adjacent Int32 columns (grenade_no, grenade_entity_id) would then
        # silently swap places if EVENTS's key order ever changed -- without a
        # type error to reveal it.
        columns = {name: [row[name] for row in rows] for name in schema}
        return pl.DataFrame(columns, schema=schema).sort(
            "round_raw", "grenade_no", "event_kind", "t_s"
        )


def _as_polars(frame: Any, columns: Sequence[str]) -> pl.DataFrame:
    """Convert demoparser2's table to Polars, only the requested columns.

    The column selection is made **before** the conversion: ``name`` is a
    1.55-million-row string column that is needed for nothing -- the id is
    ``steamid``.
    """
    if isinstance(frame, pl.DataFrame):
        return frame.select(columns)
    return pl.from_pandas(frame[list(columns)])


def _thrower_id() -> pl.Expr:
    """``steamid`` as a string, without the id turning into a float.

    Pandas promotes an integer column to ``float64`` as soon as it has one
    empty value. A direct ``cast(Utf8)`` would then turn every id into
    something of the form ``"7.6561e+16"``, the side lookup would match no
    player and **every grenade would be dropped for an unknown side** -- the
    table would be empty and nothing would say why. Going via the integer
    gives the same decimal form as the ticks' ``steamid``.
    """
    return pl.coalesce(
        pl.col("steamid").cast(pl.Int64, strict=False).cast(pl.Utf8),
        pl.col("steamid").cast(pl.Utf8, strict=False),
    )


def _cloud_observations(frame: Any) -> pl.DataFrame:
    """The point cloud's observations under the domain's column names and types.

    Translates demoparser2's prop names (:data:`CLOUD_TICK_PROPS`) into the
    domain's names (``x``, ``y``, ``z``, ``area``, ``is_alive``), so that
    :func:`~pappascout.domain.utility.build_point_cloud` stays pure and knows
    nothing of the game's fields. The same translation as
    :func:`_trajectory_frame` does for the trajectories.

    **An empty area name is not an area.** The game gives an unnamed area an
    empty string, and it is turned into ``null`` here -- the same as at the
    sample points. Without the conversion the cloud would gain cells whose
    area is ``""``: a detonation would get an empty name from them and would
    still look like a hit.

    ``is_alive`` is ``null`` if ``m_lifeState`` is missing. It is **not**
    turned into false here: the cloud's filter rejects null in any case, but
    this would be the wrong place to decide that.
    """
    return pl.from_pandas(frame[list(CLOUD_TICK_PROPS)]).select(
        pl.col(_X).cast(pl.Float64).alias("x"),
        pl.col(_Y).cast(pl.Float64).alias("y"),
        pl.col(_Z).cast(pl.Float64).alias("z"),
        pl.when(pl.col(_PLACE_NAME).cast(pl.Utf8).str.len_chars() > 0)
        .then(pl.col(_PLACE_NAME).cast(pl.Utf8))
        .otherwise(None)
        .alias("area"),
        (pl.col(_LIFE_STATE).cast(pl.Int64) == _ALIVE).alias("is_alive"),
    )


def _trajectory_frame(raw: pl.DataFrame) -> pl.DataFrame:
    """A trajectory under the domain's column names and types."""
    return raw.select(
        pl.col("grenade_entity_id").cast(pl.Int32),
        pl.col("grenade_type").cast(pl.Utf8),
        _thrower_id().alias("thrower_id"),
        pl.col("tick").cast(pl.Int32),
        pl.col("x").cast(pl.Float32),
        pl.col("y").cast(pl.Float32),
        pl.col("z").cast(pl.Float32),
    )


def _unknown_type_count(endpoints: pl.DataFrame) -> int:
    """Grenades whose class name is not known.

    An unknown name is kept in the table as it is -- it is a readable
    observation -- but a demoparser2 rename would otherwise leak into the
    table without warning, and the report would show utility whose type is the
    name of the game's C++ class.
    """
    return int(
        endpoints.filter(
            (pl.col("event_kind") == THROWN)
            & ~pl.col("grenade_type").is_in(list(GRENADE_TYPES))
        ).height
    )


def _name_fire_grenades(
    endpoints: pl.DataFrame, raw: pl.DataFrame, tolerance: int
) -> tuple[pl.DataFrame, int]:
    """Turn the class names canonical and tell molotov from incendiary.

    In flight both are ``CMolotovProjectile``, so the distinction has to be
    fetched from the thrower's bag at the moment before the throw: there the
    grenade is still a ``CMolotovGrenade`` or a ``CIncendiaryGrenade``. The
    lookup is a ``join_asof`` and not an exact tick: a trajectory is allowed a
    small gap, and the bag has to be allowed the same -- one lost tick must
    not turn an incendiary into a molotov.

    Both fire grenades in the bag (one picked up after an opponent dropped it)
    leaves the type unresolved; a guess would give the wrong answer half the
    time and would still look like an observation.

    Returns:
        ``(the table, the unresolved ones)``. The latter covers both the
        misses and the ambiguous cases. Without the figure a **total** failure
        of the bag lookup -- a class name change, too tight a tolerance --
        would look exactly like a demo in which only molotovs were thrown.
    """
    canonical = endpoints.with_columns(
        pl.col("grenade_type").replace(GRENADE_TYPES)
    )
    fire_throws = (
        endpoints.filter(
            (pl.col("event_kind") == THROWN)
            & (pl.col("grenade_type") == MOLOTOV_PROJECTILE)
        )
        .select("grenade_no", "thrower_id", pl.col("tick").alias("throw_tick"))
        .sort("throw_tick")
    )
    if fire_throws.is_empty():
        return canonical, 0

    in_inventory = raw.filter(~flight_point()).select(
        _thrower_id().alias("thrower_id"),
        pl.col("tick").cast(pl.Int32),
        pl.col("grenade_type").cast(pl.Utf8),
    )

    # One asof join per type: it says which fire grenades the thrower had in
    # his bag just before the throw. Two hits is the ambiguous case, one
    # settles the type, zero leaves it open.
    names = list(FIRE_ITEM_TYPES.values())
    matches = fire_throws.select("grenade_no")
    for class_name, name in FIRE_ITEM_TYPES.items():
        own = (
            in_inventory.filter(pl.col("grenade_type") == class_name)
            .select("thrower_id", "tick")
            .unique()
            .sort("tick")
        )
        if own.is_empty():
            matches = matches.with_columns(pl.lit(False).alias(name))
            continue
        with warnings.catch_warnings():
            # Polars cannot check the sort order when a grouping is given, and
            # warns about it on every call. Both frames are sorted by tick in
            # this function, so the warning would be pure noise on the user's
            # screen in the middle of a parse.
            warnings.simplefilter("ignore", UserWarning)
            joined = fire_throws.join_asof(
                own,
                left_on="throw_tick",
                right_on="tick",
                by="thrower_id",
                strategy="backward",
                tolerance=tolerance,
            ).select("grenade_no", pl.col("tick").is_not_null().alias(name))
        matches = matches.join(joined, on="grenade_no", how="left")

    resolved = matches.with_columns(
        pl.sum_horizontal(
            [pl.col(name).fill_null(False).cast(pl.Int8) for name in names]
        ).alias("_hits")
    )
    unresolved = int(resolved.filter(pl.col("_hits") != 1).height)

    unambiguous = resolved.filter(pl.col("_hits") == 1).select(
        "grenade_no",
        pl.coalesce(
            [
                pl.when(pl.col(name).fill_null(False)).then(
                    pl.lit(name, dtype=pl.Utf8)
                )
                for name in names
            ]
        ).alias("fire_type"),
    )
    if unambiguous.is_empty():
        return canonical, unresolved

    renamed = (
        canonical.join(unambiguous, on="grenade_no", how="left")
        .with_columns(
            pl.when(pl.col("fire_type").is_not_null())
            .then(pl.col("fire_type"))
            .otherwise(pl.col("grenade_type"))
            .alias("grenade_type")
        )
        .drop("fire_type")
        .sort("grenade_no", "tick")
    )
    return renamed, unresolved


def _shared_entity_id_count(frame: pl.DataFrame) -> int:
    """Trajectories that share the game's id with another trajectory.

    This figure was once an alarm: ``(round_no, grenade_entity_id)`` had been
    promised as the pair's key, and a non-zero value meant the key did not
    identify a pair. The league demos pushed the figure above zero
    (``inferno_vs_ryhmarama``: id 564 on round 11 carries three
    trajectories), and the answer was to change the key: the table now has
    ``grenade_no``, which is unique across the whole demo. The figure stays in
    place **as an observation**, and it is the only measure that would warn if
    somebody went back to using the entity id as a key.

    The unit counted is a **trajectory and not a pair**: three trajectories on
    one id is 3, not 2. An earlier version grouped by
    ``(round_raw, grenade_entity_id, event_kind)`` and counted groups, so the
    same situation gave 2 -- two groups, throws and detonations -- which meant
    the figure reported neither trajectories nor pairs but the number of event
    kinds. Now the distinct ``grenade_no`` values per id are counted.

    The round is the demo's own ``round_raw``, not ``round_no``: in the
    adapter's table ``round_no`` is always empty, because the numbering
    belongs to ``stages.parse``. The figure therefore includes the warm-up and
    the knife round as well.
    """
    if frame.is_empty():
        return 0
    per_id = (
        frame.group_by("round_raw", "grenade_entity_id")
        .agg(pl.col("grenade_no").n_unique().alias("trajectories"))
        .filter(pl.col("trajectories") > 1)
    )
    return int(per_id["trajectories"].sum())


def _round_windows(segments: list[_Segment]) -> list[tuple[int, int, int]]:
    """The rounds' ``[anchor, end]`` windows in chronological order.

    Raises:
        ParseError: If the windows overlap. The binary search in
            :func:`_round_of_tick` could then assign a grenade to the wrong
            round -- and every utility observation of that round would be the
            wrong team's plan.
    """
    windows = sorted(
        (s.freeze_end_tick, s.end_tick, index)
        for index, s in enumerate(segments)
        if s.freeze_end_tick is not None and s.end_tick is not None
    )
    for first, second in zip(windows, windows[1:]):
        if second[0] <= first[1]:
            raise ParseError(
                "The demo's round boundaries overlap: a round starts at tick "
                f"{second[0]} even though the previous one does not end until "
                f"tick {first[1]}.\n"
                "A grenade cannot then be assigned to a round unambiguously. "
                "The demo is most likely corrupt."
            )
    return windows


def _round_of_tick(
    starts: list[int], windows: list[tuple[int, int, int]], tick: int
) -> int | None:
    """The round within whose boundaries the tick falls, or ``None``.

    The windows do not overlap (:func:`_round_windows` makes sure of that), so
    the last anchor before the tick is the only candidate. ``None`` means the
    warm-up before the first anchor, or a throw between a round being decided
    and the next buy time; ``t_s`` is undefined for either.
    """
    position = bisect_right(starts, tick) - 1
    if position < 0:
        return None
    _, end, index = windows[position]
    return index if tick <= end else None


def _keys_by_side(
    sides: tuple[str, str], lineup_keys: list[str], segment: _Segment
) -> dict[str, str]:
    """Side -> lineup id on one round.

    A dictionary and not ``sides.index(side)``: if the side map were for some
    reason ``("T", "T")``, ``.index`` would return zero for both and **both
    teams would get the same lineup_key**. The table would look valid, but
    every per-team figure would be the sum of both -- exactly the cross-wiring
    that :meth:`Demoparser2Adapter._lineup_keys` prevents in the rounds table.
    """
    if sides[0] == sides[1]:
        raise ParseError(
            f"On the round (round_raw={segment.round_raw}, "
            f"freeze_end_tick={segment.freeze_end_tick}) both lineups came "
            f"out on the same side {sides[0]!r}.\n"
            "The sides do not separate, so the sample points' rows would be "
            "assigned to the same team. The demo is most likely corrupt."
        )
    return {sides[0]: lineup_keys[0], sides[1]: lineup_keys[1]}


def _lineup_index_by_player(lineups: list[_Lineup]) -> dict[str, int]:
    """Player -> the lineup's index.

    A player who has appeared in both lineups is left out: his side cannot be
    inferred, and a guess would attribute the contact the wrong way round.
    That does not happen in a normal demo.
    """
    result: dict[str, int] = {}
    in_both = lineups[0].members & lineups[1].members
    for index, lineup in enumerate(lineups):
        for steamid in lineup.members - in_both:
            result[steamid] = index
    return result


def _side_lookup(
    lineup_of: dict[str, int],
    sides: tuple[str, str],
    segment: _Segment,
    by_tick: dict[int, list[dict[str, Any]]],
) -> dict[str, str]:
    """Player -> side **on this round**.

    The primary source is the lineup: the side comes from the round's own map
    and not from the player, because teams switch sides at half time and in
    overtime.

    The fallback is ``m_iTeamNum`` on the round's own tick. It is needed for a
    player who is in neither lineup -- one who joined mid-map or reconnected.
    Without the fallback his damage would be rejected silently and the round
    could lose its first contact.
    """
    player_sides = {steamid: sides[index] for steamid, index in lineup_of.items()}
    for tick in (segment.freeze_end_tick, segment.end_tick):
        for row in by_tick.get(tick or -1) or ():
            player_sides.setdefault(row["steamid"], row["side"])
    return player_sides


def _resolve_side(
    player_id: str | None,
    team_num: int | None,
    player_sides: dict[str, str],
) -> str | None:
    """A player's side: first the round's map, then the event's own reading.

    The primary source is :func:`_side_lookup`'s map, which is **the same
    round's** side map -- the one by which the sample point and events tables'
    rows have been recorded. Consistency matters more here than freshness: a
    deviant side read from the event would put the death on a different team
    from the one the other tables name for the same player on the same round.

    The fallback is the event's own ``team_num``. It covers a player who is in
    neither lineup and not on the round's anchor tick -- one who joined
    mid-map or reconnected. Without it his death would drop out of the table.

    **Why the deaths table has a third level and the others two.** The chain
    is lineup -> the round's anchor tick -> the event's own ``team_num``. The
    first two are in :func:`_side_lookup` and shared with utility; the third
    is only here, and the reason is field availability rather than a different
    rule: ``player_death`` **carries the side with it**, a grenade's
    trajectory does not. That is why a sideless grenade ends up in the
    ``grenades_unknown_side`` figure and a sideless death does not -- in both
    cases everything the source has is read.

    **It is not a claim about the roster.** ``victim_lineup_key`` says *which
    team's side* lost a player on that round; the lineup's member list is in
    the ``lineups`` table, which is computed from the anchor ticks and not
    from this. A player who joined mid-map still plays on that team's side and
    his death belongs to it -- even though the roster digest does not know
    him. In the sample point table the same player would distort the *number
    of players* in an area, which is a different claim; that is why there is
    no corresponding path there.

    Returns:
        ``"T"``, ``"CT"`` or ``None``. ``None`` means that neither source
        knew: the player is a spectator, unassigned, or unknown.
    """
    if player_id is None:
        return None
    side = player_sides.get(player_id)
    if side is not None:
        return side
    return TEAM_SIDES.get(team_num if team_num is not None else -1)


def _with_sides(
    events: list[tuple[int, str | None, str | None, str | None]],
    bounds: RoundBounds,
    player_sides: dict[str, str],
) -> tuple[list[DamageEvent], int]:
    """Bound the events to the round and attach the players' sides to them.

    Returns:
        ``(the events, how many were left without a side)``. The latter figure
        goes into the diagnostics: damage rejected silently could take a
        round's first contact away with nothing saying so.
    """
    if bounds.freeze_end_tick is None or bounds.end_tick is None:
        return [], 0
    start, end = bounds.freeze_end_tick, bounds.end_tick

    result: list[DamageEvent] = []
    unknown_sides = 0
    for tick, attacker, victim, weapon in events:
        if not start <= tick <= end:
            continue
        attacker_side = player_sides.get(attacker) if attacker else None
        victim_side = player_sides.get(victim) if victim else None
        # Damage caused by the world (attacker None) is a known case and not
        # a missing observation, so it is not counted as unknown.
        if (attacker and attacker_side is None) or (victim and victim_side is None):
            unknown_sides += 1
        result.append(
            DamageEvent(
                tick=tick,
                attacker_id=attacker,
                victim_id=victim,
                weapon=weapon,
                attacker_side=attacker_side,
                victim_side=victim_side,
            )
        )
    return result, unknown_sides


def _sorted_points(points: list[SamplePoint]) -> list[SamplePoint]:
    """The sample points in a stable order.

    ``sample_kind`` is in the key because a first contact can land exactly on
    a configured second. Without it the order of two rows would depend on the
    input order, and the same demo would produce different bytes on different
    runs.
    """
    return sorted(points, key=lambda p: (p.round_raw, p.sample_t_s, p.sample_kind))


def _require_previous(
    previous: tuple[str, str] | None, segment: _Segment, reason: str
) -> tuple[str, str]:
    """Return the previous round's side map, or stop.

    Defaulting to ``("T", "CT")`` would be a guess that looked like it worked
    but attributed the round's observations to the wrong team.
    """
    if previous is not None:
        return previous
    raise ParseError(
        f"The sides of the round (freeze_end_tick={segment.freeze_end_tick}, "
        f"round_end_tick={segment.end_tick}) could not be determined: "
        f"{reason}, and there is no previous round to inherit the map from.\n"
        "Guessing the side would attribute the round's observations to the "
        "wrong team, so the parse is stopped. The demo is most likely "
        "corrupt."
    )


# -- Small converters ---------------------------------------------------------


def _is_wholly_pawnless(counts: "_SampleTickCounts | None") -> bool:
    """Whether pawnlessness explains a tick yielding no rows at all.

    ``True`` only when there were rows and **every** one of them was pawnless.
    An empty result does not count: then the demo returned nothing, and that
    is a fault.
    """
    return bool(counts and counts.without_pawn and counts.without_pawn == counts.seen)


def _pawnless_rows(*by_call: dict[int, _SampleTickCounts]) -> int:
    """The pawnless rows in total, with the same tick counted once.

    The sample point ticks and the throw ticks are read by calls of their own,
    and they can land on **the same tick**: a smoke thrown at the start of a
    round leaves on the same tick as the 6-second sample point. A plain sum
    would then count one physical row twice, and the figure would no longer be
    "rows" but "row readings". The per-tick counters are therefore merged as a
    union; the same tick gives the same number on both calls, so the maximum
    is the right choice and not one taken for safety.
    """
    merged: dict[int, int] = {}
    for counts in by_call:
        for tick, count in counts.items():
            merged[tick] = max(merged.get(tick, 0), count.without_pawn)
    return sum(merged.values())


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover - a type that cannot be compared
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else number


def _most_observed(counts: "Counter[str] | None") -> str | None:
    """The most often observed value, ties settled alphabetically.

    Alphabetical order is not a matter of taste but of reproducibility: on a
    tie ``Counter.most_common`` returns insertion order, which depends on the
    order demoparser2 happened to return the rows in.

    Returns:
        The value, or ``None`` if there are no observations. ``None`` is an
        honest result: a missing name is an observation and not a reason to
        invent a substitute.
    """
    if not counts:
        return None
    return min(counts.items(), key=lambda item: (-item[1], item[0]))[0]


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:  # pragma: no cover
        return None
    text = str(value).strip()
    return text or None


def _as_inventory(value: Any) -> tuple[str, ...] | None:
    """The inventory at one tick.

    Returns:
        The names in order, or ``None`` if the prop could not be read. An
        empty tuple and ``None`` are **different things**: the former says "it
        was read and there was nothing", the latter "it was not read". Only
        the latter may leave the armed count empty.
    """
    if value is None:
        return None
    if isinstance(value, float):  # pandas promotes a missing value to NaN
        return None
    if isinstance(value, str):  # a single name without a list
        text = _as_str(value)
        return () if text is None else (text,)
    try:
        items = list(value)
    except TypeError:
        return None
    names = [_as_str(item) for item in items]
    return tuple(name for name in names if name is not None)


def _as_side(value: Any) -> str | None:
    text = _as_str(value)
    if text is None:
        return None
    text = text.upper()
    return text if text in ("T", "CT") else None


# -- Buy time -----------------------------------------------------------------


def _buy_end_ticks(
    segments: list[_Segment],
    death_ticks: list[int],
    tick_rate: float,
    window_seconds: float,
) -> tuple[list[int | None], list[int | None]]:
    """Choose, for each round, the tick the economy values are read at.

    The measurement point is::

        max(freeze_end_tick,
            min(freeze_end_tick + window_seconds * tick_rate,
                the tick BEFORE the round's first death,
                the round's upper bound))

    The outer ``max`` is not decoration: without it a death exactly one tick
    after the anchor would push the measurement point earlier than the anchor,
    that is, into freezetime.

    The round's upper bound is ``end_tick``. If the round was not resolved
    (the demo was cut short), the bound is **the tick before the next round
    boundary's anchor**: without it the window would spill over into the next
    round and read its economy values onto this round's row.

    **The tick before the death, not the death's tick.** On the death tick the
    victim's ``inventory`` is already empty and ``m_ArmorValue`` is 0
    (measured: ``inferno_vs_ryhmarama`` round 6, tick 42236). Read exactly at
    the death tick, one player's entire kit would disappear from the team -- a
    different fault from measuring too early, but just as silent. The lookup
    is therefore :func:`~bisect.bisect_left`, which also takes in a death
    **exactly on the anchor**; ``bisect_right`` would skip it and read the
    corpse.

    **One tick for the whole round.** When nobody has died yet at the moment
    of measurement, nobody has had the chance to drop a weapon on dying
    either, so the source of double counting (a team-mate picking up the dead
    player's rifle) is structurally impossible. No per-player "last alive"
    point is therefore needed: at the moment of measurement the team is
    untouched.

    **The cut is the normal path, not an edge case.** Measured over six demos
    (134 rounds played), a death cuts the window on 69 rounds, that is
    **51 %**. The round's first death lands at 9.80 s at the earliest and at
    19.7 s at the median; nobody dies within 8 seconds on any round. The
    effective moment of measurement is therefore often 10-20 s rather than
    20 s.

    The overlap with buying is narrow but real: of the rounds where anything
    was still bought after freezetime, buying was finished within 8 s on 92 %,
    and the earliest death is 9.8 s. In the same data no death precedes the
    last purchase, but four rounds are still buying at 11.0 / 11.3 / 13.5 /
    19.4 s. That is why the cut's price is **measured on every run**
    (:func:`_purchases_between`) rather than assumed to be zero.

    Args:
        segments: The round boundaries.
        death_ticks: The ticks of every ``player_death`` event in ascending
            order.
        tick_rate: The tick rate in use. It may be an unmeasured default, in
            which case the window's length in ticks is a default too --
            ``stages.parse`` tells the user, this function cannot know the
            difference.
        window_seconds: ``[parse].buy_window_seconds``.

    Returns:
        ``(the measurement points, the uncut window ends)``, both in segment
        order.

        The measurement point is ``None`` if the round has no anchor or if it
        is not a round at all (a match restart).

        The latter list is ``None`` everywhere except on the rounds where a
        death cut the window: there it is the tick that would have been
        measured without the cut. It is not used for the measurement, only to
        work out whether any purchases fell behind the cut.
    """
    # The window's length in ticks. The name is not ``window_ticks``, because
    # to the caller that means a list of ticks; the same name for two
    # different things is exactly the confusion this module otherwise avoids.
    window_length = max(0, round(window_seconds * tick_rate))
    measured: list[int | None] = []
    uncut: list[int | None] = []

    for index, segment in enumerate(segments):
        anchor = segment.freeze_end_tick
        if anchor is None or segment.round_raw is None:
            measured.append(None)
            uncut.append(None)
            continue

        limit = anchor + window_length
        bound = _round_upper_bound(segments, index, anchor)
        if bound is not None:
            limit = min(limit, bound)
        limit = max(limit, anchor)

        # The first death at or after the anchor. bisect, because the ticks
        # are in order and there are hundreds of them in a demo.
        position = bisect_left(death_ticks, anchor)
        first_death = death_ticks[position] if position < len(death_ticks) else None

        cut = None if first_death is None else max(anchor, first_death - 1)
        if cut is not None and cut < limit:
            measured.append(cut)
            uncut.append(limit)
        else:
            # A death after the window, or the window is already zero long:
            # the window did not shorten, so no cut is reported either.
            # Recording a zero as a cut would turn the counter into noise.
            measured.append(limit)
            uncut.append(None)
    return measured, uncut


def _round_upper_bound(
    segments: list[_Segment], index: int, anchor: int
) -> int | None:
    """The last tick that still belongs to round ``index``.

    A resolved round ends at its own ``end_tick``. An unresolved one (the demo
    was cut short) has none, and the bound is then taken from **the next round
    boundary**: the buy window must not reach the next round's anchor, because
    economy values read there would already be the next round's.

    A match restart serves as a bound just as a round does: it is not a round,
    but it is the moment after which this round's values no longer hold.

    Returns:
        The upper bound, or ``None`` if the round is the demo's last and has
        no ending -- there is then no bound and none is invented.
    """
    if segments[index].end_tick is not None:
        return segments[index].end_tick
    for later in segments[index + 1 :]:
        if later.freeze_end_tick is not None and later.freeze_end_tick > anchor:
            return later.freeze_end_tick - 1
    return None


def _purchases_between(
    at_measurement: list[dict[str, Any]],
    at_window_end: list[dict[str, Any]],
) -> tuple[int, int]:
    """Purchases lost behind the window cut -- and how many could be checked.

    ``m_iCashSpentThisRound`` grows **only from purchases** and does not react
    to deaths or dropped weapons, and it is on the player's controller rather
    than the pawn, so it survives death too. It is therefore the only safe
    measure of whether cutting the window cost anything.

    The lost purchases **are supposed to be zero**. A non-zero value means
    somebody bought after the window was cut, that is, the measurement lost a
    purchase -- and that has to be said out loud in the run's output rather
    than passed over.

    The number compared comes back with it, because **a zero has two different
    causes**: nothing was lost, or the comparison could not be made at all (no
    rows came from the tick at the end of the window). Without the
    distinction, the story's most important figure could read an empty zero
    with nothing saying so.

    Args:
        at_measurement: The player rows from the measurement point's tick.
        at_window_end: The player rows from the tick the window would have
            reached without the cut.

    Returns:
        ``(purchases lost, players compared)``. The first is the number of
        players whose ``cash_spent`` is larger on the latter tick than on the
        former. A player who is missing from either tick, or whose figure is
        not readable, does not count as an observation and does not increment
        either number.
    """
    before = {
        r["steamid"]: r["cash_spent"]
        for r in at_measurement
        if r["cash_spent"] is not None
    }
    missed = 0
    compared = 0
    for row in at_window_end:
        spent = row["cash_spent"]
        earlier = before.get(row["steamid"])
        if spent is None or earlier is None:
            continue
        compared += 1
        if spent > earlier:
            missed += 1
    return missed, compared


def _refunds_and_stale_equipment(
    at_anchor: list[dict[str, Any]],
    at_measurement: list[dict[str, Any]],
) -> tuple[int, int]:
    """Purchases refunded during the window -- and the stale value they leave.

    In CS2 an item just bought can be refunded for a few seconds. The money
    and the armour come back correctly, but **the equipment value does not
    always follow**: measured on ``Anubis_vs_ryhmarama`` round 3 CT, where a
    player bought kevlar and a helmet at 0.4 s and refunded them at 1.9 s --
    ``m_iAccount`` and ``m_ArmorValue`` returned to their starting values
    (450 -> 1,450 and 100 -> 0), but ``m_unCurrentEquipmentValue`` stayed at
    1,200 and did not go back to 200.

    Two figures, because they are different observations:

    ``refunds``
        ``cash_spent`` **decreased** between the anchor and the measurement
        point. The prop grows only from purchases, so a decrease can only mean
        a refund -- an unambiguous sign that cannot be confused with a death.
        Measured: 8 player rows on 7 rounds across six demos, and in these the
        equipment value followed the refund correctly.
    ``stale value``
        The equipment value **rose** even though the player did not buy
        (``cash_spent`` unchanged), gained no armour (``m_ArmorValue``
        unchanged) and did not change his inventory. Nothing arrived, so the
        value must be stale. This is the trace left by a refund that happened
        **entirely between the two ticks that were read**: ``cash_spent`` is
        the same on both ticks, and the refund shows in no other way.
        Measured: 1 player row out of 134 rounds, an effect of $1,000, that
        is, $200 per player at team level.

    **It is not recognised from the trace "the armour vanished and the value
    did not fall".** Death produces exactly the same trace and is ten times as
    common, so such a counter would measure deaths and not refunds. Both
    conditions above require, on the contrary, that the armour did not change.

    **It does not affect the armed count.** That reads the inventory and
    ``m_ArmorValue``, both of which come back correctly; going stale affects
    only the equipment value.

    Returns:
        ``(refunds, stale values)`` as player rows.
    """
    anchor_by_id = {r["steamid"]: r for r in at_anchor}
    refunds = 0
    stale = 0
    for row in at_measurement:
        earlier = anchor_by_id.get(row["steamid"])
        if earlier is None:
            continue
        spent_before, spent_now = earlier["cash_spent"], row["cash_spent"]
        if spent_before is None or spent_now is None:
            continue
        if spent_now < spent_before:
            refunds += 1
            continue
        equip_before, equip_now = earlier["equip_current"], row["equip_current"]
        armor_before, armor_now = earlier["armor_value"], row["armor_value"]
        if None in (equip_before, equip_now, armor_before, armor_now):
            continue
        if (
            equip_now > equip_before
            and spent_now == spent_before
            and armor_now == armor_before
            and (earlier.get("inventory") or ()) == (row.get("inventory") or ())
        ):
            stale += 1
    return refunds, stale


#: The props that have to be readable for a player to be counted into the
#: sums at the end of the buy time and into their divisor.
_BUY_END_PROPS: tuple[str, ...] = (
    "account",
    "cash_spent",
    "equip_current",
    "equip_round_start",
)


def _readable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The players whose end-of-buy-time values are all readable.

    Both the sum and its divisor are computed from **this same set**. If only
    the readable ones were summed but the division were by every row, three
    players' equipment value divided by five would underestimate the result by
    40 per cent and push the round into an eco -- silently and plausibly.
    """
    return [
        r for r in rows if all(r.get(name) is not None for name in _BUY_END_PROPS)
    ]


#: The name of the armour reading's prop. A constant, because three places
#: read it -- both counts' readability conditions and the shared
#: :func:`_has_armor` -- and hard-coded the name would diverge from them
#: unnoticed.
_ARMOR_PROP = "armor_value"

#: The props that have to be readable for the armed count to be computed.
#: These are **not** in :data:`_BUY_END_PROPS`: a player stays in the sums and
#: in their divisor even if these are missing, because the divisor has to be
#: the same set for every figure on the row.
_ARMED_PROPS: tuple[str, ...] = (_ARMOR_PROP, "inventory")

#: The props that have to be readable for the armour count to be computed.
#: A **proper subset** of :data:`_ARMED_PROPS`, and that is the whole
#: difference written into the code rather than into a comment: the armour
#: count does not read the inventory, so an unreadable inventory empties only
#: the armed count.
_ARMORED_PROPS: tuple[str, ...] = (_ARMOR_PROP,)


def _has_armor(row: dict[str, Any]) -> bool:
    """Whether the player has armour at the end of the buy time.

    **The condition both counts share**, and therefore in one place. Both
    :func:`_is_armed` and :func:`_armored_count` read the same
    ``m_ArmorValue`` reading from the same tick; if the condition were written
    twice, a change to the threshold, to telling the helmet apart, or to how
    damaged armour is bounded would change only one of the counts -- and
    preventing exactly that silent divergence is the whole justification for
    the two columns.

    The caller is responsible for the value being readable; here ``None``
    would be read as "no armour", so a read failure would look like a save.

    The helmet is not told apart: ``m_bHasHelmet`` is an observation of its
    own and the analysis does not talk about it. Damaged armour is not told
    apart from intact armour either: 37/100 is still armour, and the player is
    carrying it.
    """
    return (row.get(_ARMOR_PROP) or 0) > 0


def _armed_readable(row: dict[str, Any]) -> bool:
    """Whether the player's armour and inventory are readable.

    An empty inventory (``()``) is **an observation**: the player had nothing.
    A missing one (``None``) is not. The same goes for the armour: ``0`` is an
    observation, ``None`` is not.
    """
    return all(row.get(name) is not None for name in _ARMED_PROPS)


def _armored_readable(row: dict[str, Any]) -> bool:
    """Whether the player's armour is readable.

    **A narrower condition** than :func:`_armed_readable`: the inventory is
    not part of it, because the armour count does not read it. ``0`` is an
    observation (the player had no armour), ``None`` is not.
    """
    return all(row.get(name) is not None for name in _ARMORED_PROPS)


def _is_armed(row: dict[str, Any]) -> bool:
    """Whether the player has armour and at least one weapon in hand.

    The user's definition is "kevlar **and** some upgraded weapon". Kevlar
    without a weapon is not enough, nor a weapon without kevlar.
    ``armor_value > 0`` is enough; a helmet is not required, because a CT
    often buys kevlar alone because of the AK's one-shot kill.

    What decides is **possession, not purchase**: a saved or picked-up rifle
    counts the same as a bought one. The default pistols are still out,
    because they are free every round.

    The row comes from the tick at the end of the buy time, which is chosen
    before the round's first death (see :func:`_buy_end_ticks`). A dead
    player's ``inventory`` is empty and his armour 0, so read at the death
    tick this would return ``False`` regardless of what the player bought.

    The caller has already made sure with :func:`_armed_readable` that the
    values are readable -- here ``None`` would be read as "no armour" and "no
    items", so a read failure would look like a save.

    An unknown name is not a weapon (see
    :data:`~pappascout.constants.ARMING_WEAPONS`).
    """
    if not _has_armor(row):
        return False
    return any(name in ARMING_WEAPONS for name in row.get("inventory") or ())


def _armed_count(own_buy: list[dict[str, Any]]) -> int | None:
    """How many players were armed at the end of the buy time.

    Armed = **armour and at least one weapon in hand**. The team sum does not
    say this: two AKs and three empty hands give the same sum as five
    half-buys, and the equipment value does not distinguish a weapon from
    armour and grenades at all. The count is computed from **the same set** as
    the sums and ``players_buy_end`` (see :func:`_readable`), so the row has
    only one divisor.

    Args:
        own_buy: The team's set of players, filtered by :func:`_readable`.

    Returns:
        The number of armed players, or ``None`` if the figure cannot be
        given.

        **Zero is not a missing observation**: it is the information that
        nobody was armed -- a full eco produces a zero, and that is data.

        ``None`` is two different things, and both are "not known":

        * the set is empty (a round without a freezetime anchor), or
        * **even one** player's armour or inventory is unreadable.

        The latter empties the whole row rather than dropping just one player,
        because the player stays in the ``players_buy_end`` divisor anyway:
        "3/5" would claim that two were unarmed when the truth is that they
        could not be read. A read failure passed over in silence would look
        like a save round.
    """
    if not own_buy:
        return None
    if not all(_armed_readable(row) for row in own_buy):
        return None
    return sum(1 for row in own_buy if _is_armed(row))


def _armored_count(own_buy: list[dict[str, Any]]) -> int | None:
    """How many players carried armour at the end of the buy time.

    **A different figure from** :func:`_armed_count`, not a generalisation of
    it. The condition here is :func:`_has_armor` alone; the weapon does not
    matter. This is where the target analysis's lines *"5 kevlars"* and *"no
    kevs"* are read from, and the armed count cannot give them: on a pistol
    round it is in practice 0, because $800 does not buy both kevlar and an
    upgraded weapon.

    **Possession, not purchase.** Armour carries over the round for anyone who
    survived, so on other round types the figure says what the players had and
    not what they bought. On a pistol round (1 and 13) nothing is inherited --
    the half starts from a clean slate -- so there it is an observation of
    buying.

    The same set, the same tick and the same reading as :func:`_armed_count`,
    so the armed are always a subset of the armoured and there cannot be more
    armoured than there were readable players.

    Args:
        own_buy: The team's set of players, filtered by :func:`_readable`.

    Returns:
        The number of armoured players, or ``None`` if the figure cannot be
        given.

        **Zero is not a missing observation**: a round on which nobody carried
        armour produces a zero, and that is exactly Ancient's CT pistol round
        *"no kevs"*.

        ``None`` is two different things, and both are "not known": the set is
        empty (an anchorless round), or **even one** player's armour is
        unreadable. The latter empties the whole row for the same reason as in
        the armed count: the player stays in the ``players_buy_end`` divisor,
        so a partial figure would look like a save rather than a read failure.

        The readability condition is **narrower** than the armed count's: the
        inventory is not part of it, because this count does not read it. An
        unreadable inventory therefore empties only the count above.
    """
    if not own_buy:
        return None
    if not all(_armored_readable(row) for row in own_buy):
        return None
    return sum(1 for row in own_buy if _has_armor(row))


def _sum_or_none(values: list[int | None]) -> int | None:
    """Sum the values; ``None`` if there is not a single observation."""
    valid = [v for v in values if v is not None]
    return sum(valid) if valid else None


def _sum_or_zero(values: list[int | None]) -> int:
    """Sum the values; an empty set is zero (nobody survived)."""
    return sum(v for v in values if v is not None)


def _score_before(
    index: int,
    segments: list[_Segment],
    anchor_score: list[int | None],
    end_score: list[int | None],
) -> int | None:
    """The combined score just before round ``index``, when the anchor is gone.

    The fallback is asked for only on a round that has no freezetime anchor of
    its own. The nearest earlier reading will do, but **at a match restart its
    anchor is read and not its end tick**: it has no end tick at all, and the
    reading of the round before it is from *the moment before the reset*.
    After the knife round that would be 1 even though the score has just been
    zeroed -- and then the round following the restart would get
    ``score_start == score_end`` and drop out of the rounds played.

    Returns:
        The reading, or ``None`` if none was found.
    """
    for back in range(index - 1, -1, -1):
        value = (
            anchor_score[back]
            if segments[back].round_raw is None
            else end_score[back]
        )
        if value is not None:
            return value
    return None


def _total_score(rows: list[dict[str, Any]]) -> int | None:
    """The teams' combined score at one tick.

    The sum survives the half-time switch: the per-team scores swap places,
    but the sum stays and grows only from a round played.

    It requires the reading from **both** sides. A one-sided sum would look
    like a valid figure but be too small, and the round could then drop out of
    the rounds played -- or stay in with the wrong number.
    """
    per_side: dict[str, int] = {}
    for row in rows:
        if row["team_score"] is not None:
            per_side.setdefault(row["side"], row["team_score"])
    if len(per_side) != len(TEAM_SIDES):
        return None
    return sum(per_side.values())

