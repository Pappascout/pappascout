"""The shared Polars table contracts (AD-2, AD-4, AD-5).

Eight tables, all of which every stage of the pipeline reads and writes:

``ROUNDS``
    ``parsed/<map_demo_id>/rounds.parquet`` -- a long table, **two rows per
    round**, one for each team. Holds only the values the ``parse`` stage
    *observed*, no derived values.
``TICKS``
    ``parsed/<map_demo_id>/ticks.parquet`` -- a row per (player, round, sample
    point).
``EVENTS``
    ``parsed/<map_demo_id>/events.parquet`` -- a row per utility event. The
    throw and the detonation are two rows, joined by ``grenade_no``.
``LINEUPS``
    ``parsed/<map_demo_id>/lineups.parquet`` -- a row per (lineup, player).
    The player's name and their clan name, that is the team's **identity** --
    not a per-round observation.
``DEATHS``
    ``parsed/<map_demo_id>/deaths.parquet`` -- a row per death. The victim and
    the attacker, both with their area and coordinates; a death has **two
    actors**, so it does not fit the one-actor shape of the ``EVENTS`` table.
``CALLOUT_CLOUD``
    ``parsed/<map_demo_id>/callouts.parquet`` -- a row per point-cloud cell. A
    grid assembled from the demo's own ticks, recording where the players have
    stood on the map and which area is at each spot. The detonation's area is
    read from this, and the table is kept so that the derived area can be
    checked against the demo.
``MATCH``
    ``parsed/<map_demo_id>/match.parquet`` -- **one row per demo**. The
    match's own observations, at present the map's name from the demo header.
    The map is a property of the match and not of a round, so it does not
    belong in the ``ROUNDS``, ``TICKS``, ``EVENTS`` or ``DEATHS`` table.
``CLASSIFIED``
    ``classified/<team_key>/<map_demo_id>.parquet`` -- **one row per round**
    from the subject team's point of view. Holds the ``classify`` stage's
    derived values.

Every table carries ``map_demo_id``, even though it is also in the file path.
The reason: ``aggregate`` reads dozens of demos into one frame, and the join
``(map_demo_id, round_no)`` is then the only correct key -- ``round_no`` alone
would mix the rounds of different maps together.

Naming convention: English ``snake_case``, the unit in the name. ``*money*``
and ``*equip*`` are integer dollars, ``*_s`` seconds as a float, the
coordinates ``x, y, z`` float32.

Every stage validates the table it reads and the table it writes with the
function :func:`validate`. The validation is strict in both directions: a
missing column and an extra column are both errors. The reason is the Polars
join that goes silently empty -- exactly the defect this contract prevents.
"""

from __future__ import annotations

import polars as pl

from pappascout.constants import (
    AREA_SOURCES,
    EVENT_KINDS,
    ROSTER_CLASSES,
    ROUND_TYPES,
    SAMPLE_KINDS,
    SIDES,
    UNIT_STATUSES,
)
from pappascout.errors import SchemaError

__all__ = [
    "ROUNDS",
    "ARMED_COLUMN",
    "ARMORED_COLUMN",
    "MONEY_DISTRIBUTION_COLUMN",
    "TICKS",
    "EVENTS",
    "LINEUPS",
    "DEATHS",
    "CALLOUT_CLOUD",
    "MATCH",
    "CLASSIFIED",
    "CLASSIFIED_INPUTS",
    "SCHEMAS",
    "Schema",
    "validate",
]

Schema = dict[str, "pl.DataType | pl.DataTypeClass"]

_SIDE = pl.Enum(list(SIDES))
_ROUND_TYPE = pl.Enum(list(ROUND_TYPES))
_UNIT_STATUS = pl.Enum(list(UNIT_STATUSES))
_SAMPLE_KIND = pl.Enum(list(SAMPLE_KINDS))
_EVENT_KIND = pl.Enum(list(EVENT_KINDS))
_AREA_SOURCE = pl.Enum(list(AREA_SOURCES))
_ROSTER_CLASS = pl.Enum(list(ROSTER_CLASSES))


#: The name of the equipment counter's column. A constant, because it is read
#: by the adapter (which computes the number), by ``stages.parse`` (which
#: reports its distribution), by ``classify`` (which reads condition A from
#: it) and by the tests -- as hard-coded strings they would drift apart
#: unnoticed.
ARMED_COLUMN = "players_armed_buy_end"

#: The name of the armour counter's column. The same reason for a constant as
#: with :data:`ARMED_COLUMN`: the adapter computes, ``stages.parse`` reports
#: the distribution, ``aggregate`` reads it into the report and the tests
#: compare.
#:
#: **NOT THE SAME NUMBER AS** :data:`ARMED_COLUMN`. That very confusion cost
#: one wrong row in Story 2.3's acceptance run, so the difference is stated
#: here out loud:
#:
#: * ``players_armed_buy_end`` = armour **AND** an upgraded weapon. It is the
#:   half-buy's condition A: calibrated, and ``classify`` relies on it.
#: * ``players_armored_buy_end`` = armour, full stop. It answers the question
#:   "how many had armour", which is the single most important observation of
#:   a pistol round.
#:
#: Both are **possession and not purchases**: armour survives a round for
#: whoever came through it alive, damaged armour included. The pistol round (1
#: and 13) is the exception -- a half starts from a clean slate and there is
#: nothing to inherit, so there the number is an observation of a purchase.
#:
#: On a pistol round they also differ the most: with 800 dollars of starting
#: money kevlar (650) and an upgraded weapon do not fit into the same
#: purchase, so the armed count is in practice 0 even if all five had kevlar.
#: Measured from four MatureMayhem demos on 2026-08-30: on all eight pistol
#: rounds the armed count was 0 and the armoured count 1--5. It is not a rule:
#: a picked-up weapon is enough to count as armed, and in the same data the
#: opponent's Anubis round 13 gives 3 and 1.
ARMORED_COLUMN = "players_armored_buy_end"

#: The name of the per-player money distribution's column. The same reason for
#: a constant as above: the adapter writes, ``classify`` reads, the tests
#: compare.
MONEY_DISTRIBUTION_COLUMN = "money_players_buy_end"


# The rounds table: two rows per round, one for each team (AD-5).
# round_no is null in warm-up, on the knife round and at mp_restartgame resets
# -- only parse sets it.
ROUNDS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    "round_raw": pl.Int32,  # demoparser2's own round counter
    "round_no": pl.Int32,  # 1-based played round, null if not played
    "lineup_key": pl.Utf8,  # the lineup id of the row's team (AD-6)
    "side": _SIDE,  # the side of the row's team on this round
    "won": pl.Boolean,
    "win_reason": pl.Utf8,
    "money_buy_end": pl.Int32,  # $ balance left at the end of the buy time
    "money_spent": pl.Int32,  # $ money spent on this round (cash_spent)
    "equip_buy_end": pl.Int32,  # $ sum of current_equip_value at the buy end
    "equip_round_start": pl.Int32,  # $ sum of round_start_equip_value
    "players_buy_end": pl.Int32,  # the players whose values could be read
    # The money balances of the previous, one player at a time, in descending
    # order of size. THE SAME SET as in the sum of money_buy_end and as the
    # divisor of players_buy_end, so the length is always players_buy_end.
    #
    # WHY THE SUM IS NOT ENOUGH: a half-buy is told apart from a force by how
    # many players can make a normal buy on the next round, and that is a
    # per-player question. A team where one has 5 000 and four have nothing
    # gets the same sum as a team where everyone has 1 000, but in the former
    # four out of five can buy nothing. The mean gives impossible numbers too:
    # the measured round 19 CT showed "$30 per player" when the real balances
    # were 0, 0, 50, 50, 50 -- every price is a multiple of fifty, so 30
    # cannot be anybody's balance.
    #
    # THE ORDER IS SORTED and not the player order: the row does not carry the
    # players' ids, so the position of an element says nothing about who it
    # is, and sorting makes the reading reproducible regardless of the tick's
    # row order.
    #
    # null always and only when players_buy_end is null (a round without an
    # anchor). An empty list is not written: it would claim as an observation
    # that nobody was there.
    MONEY_DISTRIBUTION_COLUMN: pl.List(pl.Int32),
    # Of the previous, those who had ARMOUR AND AT LEAST ONE WEAPON IN HAND at
    # the end of the buy time. Read from the inventory and from m_ArmorValue,
    # not from the equipment value: the equipment value is weapon + armour +
    # grenades as one number and does not tell a weapon apart from a free
    # pistol and two flashes. POSSESSION AND NOT A PURCHASE: a saved or a
    # picked-up rifle counts the same as a bought one. The team sum does not
    # tell this: two AKs and three empty hands give the same sum as five
    # half-equipped players. Always 0..players_buy_end; null when there is no
    # observation at all -- zero means "nobody was armed".
    # Null also when even one player's armour or inventory is unreadable: a
    # partial reading would look like saving rather than like a read error.
    ARMED_COLUMN: pl.Int32,
    # Of the previous, those who had ARMOUR -- regardless of the weapon. The
    # same tick, the same set of players and the same m_ArmorValue reading as
    # above; only the condition differs. "Armoured" is m_ArmorValue > 0; the
    # helmet is not distinguished, and neither is damaged armour from intact,
    # because the analysis speaks of kevlar and the helmet is a different
    # observation.
    #
    # POSSESSION AND NOT A PURCHASE, as with the one above: armour survives a
    # round for whoever came through it alive, so the number says what the
    # players had rather than what they bought. On a pistol round (1 and 13)
    # there is nothing to inherit, so there -- and only there -- it is an
    # observation of a purchase.
    #
    # THE ARMED ARE A SUBSET OF THIS: the armed condition includes armour, so
    # players_armed_buy_end <= players_armored_buy_end always.
    #
    # WHY A COLUMN OF ITS OWN RATHER THAN A GENERALISATION OF THE ONE ABOVE:
    # they answer different questions and both are needed. The one above is
    # calibrated as the half-buy's condition A and is deliberately strict;
    # this one answers the question "how many had armour". On a pistol round
    # the one above is in practice 0 ($800 is not
    # enough for both kevlar and an upgraded weapon), so reading it makes
    # "5 kevlars" and "no kevlars" look exactly the same.
    #
    # Always 0..players_buy_end. Null always and only when players_buy_end is
    # null or even one readable player's armour is unreadable -- a partial
    # reading would look like saving rather than like a read error. NOTE:
    # readability is NARROWER here than above: the inventory is not part of
    # the condition, because this counter does not read it. An unreadable
    # inventory therefore empties only the column above, not this one.
    ARMORED_COLUMN: pl.Int32,
    "survivors": pl.Int32,  # alive at the end of the round
    "survivors_equip_prev": pl.Int32,  # $ equipment value saved from the previous round
    "freeze_end_tick": pl.Int32,  # the last round_freeze_end tick, null if absent
    # The tick the economic values above were read from:
    #   max(freeze_end_tick,
    #       min(freeze_end_tick + [parse].buy_window_seconds,
    #           the tick BEFORE the round's first death,
    #           the end of the round))
    # The same on both rows of the same round -- a per-player or per-team
    # point would count a weapon dropped by a dead player and picked up twice.
    # Null always and only when freeze_end_tick is null.
    #
    # The column exists so that the reading can be checked against the demo:
    # without it freeze_end_tick would claim to be the moment of measurement,
    # and that very lie hid this defect in the first place (Story 1.9).
    #
    # WHAT IT DOES NOT TELL: equality with freeze_end_tick is AMBIGUOUS.
    # It means either the setting buy_window_seconds = 0 (measure at the
    # anchor), a death immediately after the anchor, or the fallback rule
    # parse returned to when no player could be read at the buy-time tick. The
    # row's status is "ok" in all three, and classify cannot tell them apart
    # -- the distinction is in the run's output (stages.parse ->
    # ParseDiagnostics), because UNIT_STATUSES is a shared contract and is not
    # extended for the needs of one stage.
    #
    # classify DOES NOT read this column (see economy.CLASSIFY_COLUMNS): the
    # measurement is the parse's responsibility, and the classification rule
    # relies on the numbers rather than on the moment they were read at. The
    # column is there for traceability, and the run's output tells the
    # distribution of the measurement moments.
    "buy_end_tick": pl.Int32,
    "tick_rate": pl.Float32,
    "status": _UNIT_STATUS,
}

# The sample-point table: a row per (player, round, sample point) (AD-5).
TICKS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    "round_raw": pl.Int32,
    "round_no": pl.Int32,
    "player_id": pl.Utf8,
    "lineup_key": pl.Utf8,
    "side": _SIDE,
    "sample_kind": _SAMPLE_KIND,  # "time" or "first_contact"
    # s -- the nominal time. NOTE: the value has two different semantics,
    # which only sample_kind tells apart:
    #   sample_kind = "time"          -> a [parse].snapshot_seconds number
    #                                    as it stands (6.0, 15.0, ...), the
    #                                    same on every round, comparable
    #   sample_kind = "first_contact" -> the measured moment, the same as t_s,
    #                                    different on every round
    # Grouping therefore always has to be done by the pair (sample_kind,
    # sample_t_s). Grouping by sample_t_s alone would mix two different
    # things: 15.0 would mean both "the 15-second sample" and "a round on
    # which first contact happened to be at 15.0 s".
    "sample_t_s": pl.Float64,
    "t_s": pl.Float64,  # s -- the time since the last round_freeze_end
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
    # the game's last_place_name; an unknown area is null, the coordinates are
    # still there
    "area": pl.Utf8,
    "is_alive": pl.Boolean,
}

# The utility event table (AD-5). The throw and the detonation are two rows,
# joined by grenade_no -- the trajectory's own id, which is unambiguous across
# the whole demo. area and x, y, z mean, depending on event_kind, either the
# throwing place or the detonation place. Utility is measured from throws, not
# from purchases.
#
# The area is two kinds of information, and area_source tells which: on the
# throw row it is the thrower's own m_szLastPlaceName (an observation), on the
# detonation it is the area of the nearest cell of the demo's own point cloud
# within the distance limit parse.area_snap_units (an approximation). Without
# the distinction the report would present an estimate as an observation.
EVENTS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    "round_raw": pl.Int32,
    "round_no": pl.Int32,
    "event_kind": _EVENT_KIND,
    # The trajectory's id: a running number inside the demo, ordered by the
    # throw's tick. UNAMBIGUOUS ACROSS THE WHOLE DEMO, not only within a
    # round, and the throw and the detonation share it -- it is their only
    # link.
    # One demo: (grenade_no, event_kind) identifies the row.
    # Many demos: (map_demo_id, grenade_no, event_kind) -- the number runs
    # inside a demo, so aggregate needs map_demo_id alongside.
    # SHAPE: it starts from zero and grows, but IS NOT A CONTIGUOUS RANGE
    # 0..n-1. The numbering is done before parse drops the warm-up and knife
    # round rows, so the finished table has gaps. The number must therefore
    # not be used as an index, nor its largest value as the number of
    # grenades.
    # STABLE over the same input: the segmentation is deterministic, so
    # re-parsing gives the same numbers.
    "grenade_no": pl.Int32,
    # The game's own entity id. DOES NOT IDENTIFY A GRENADE: the game recycles
    # the ids during the demo and also WITHIN THE SAME ROUND. Measured:
    # inferno_vs_ryhmarama, round 11, id 564 = three different trajectories
    # (molotov 9.2 s, flashbang 18.0 s, incendiary 64.2 s).
    # The column is kept because it is the only link back into the demo --
    # the grenade can be found with it in a viewer. It must not be used as a
    # key.
    "grenade_entity_id": pl.Int32,
    "grenade_type": pl.Utf8,
    "thrower_id": pl.Utf8,
    "lineup_key": pl.Utf8,
    "side": _SIDE,
    "t_s": pl.Float64,
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
    "area": pl.Utf8,
    # observed = the thrower's own area, point_cloud = derived from the demo's
    # point cloud. null always and only when area is null.
    "area_source": _AREA_SOURCE,
    # The weighted distance to the nearest POINT-CLOUD CELL (CALLOUT_CLOUD):
    #
    #     d = sqrt(dx^2 + dy^2 + (callout_z_weight *
    #                             max(0, |dz| - callout_z_tolerance_units))^2)
    #
    # It is therefore not the Euclidean distance but the number by which the
    # nearest cell was chosen. The consumer tells a 15-unit hit apart from a
    # 240-unit estimate with this. The NAME of the column is a leftover of the
    # method -- "snap" means snapping to the nearest cell and not snatching
    # from a player; the reason for keeping the name is in settings.toml above
    # area_snap_units.
    #
    # THE VALUE SURVIVES THE THRESHOLD TOO. A detonation whose nearest cell is
    # further away than parse.area_snap_units gets area = null but keeps its
    # distance: that is what tells "far from everywhere anyone has stood"
    # apart from "the point cloud was empty", where this is null as well. On a
    # throw row the value is always null, because the area is an observation
    # and not a derived value.
    "snap_distance": pl.Float32,
}

# The lineup table: a row per (lineup, player) (Story 2.6).
#
# WHY A TABLE OF ITS OWN RATHER THAN A COLUMN IN THE TICKS TABLE. ``TICKS`` is
# (player x round x sample point) -- tens of thousands of rows across four
# demos, on which the name would repeat. Nor is the name a per-round
# observation: it is the same for the whole map (measured 2026-08-30 with five
# demos: zero players with more than one name or clan). Identity belongs in a
# table of its own, and ``aggregate``'s lineup reading got cheaper at the same
# time: it used to read the whole ``ticks`` table to get the set of players.
#
# THE CLAN IS READ PER PLAYER, NOT THROUGH THE SIDE. The same measurement:
# ``parse_ticks(["team_clan_name"])`` gives every SteamID exactly one clan at
# every anchor, across the half-time switch too. Read through the side
# (``team_num``) the same value changes team at half time --
# ``team_num=2`` is ``KALJUKOSTAJA`` in the 1st half and ``MatureMayhem`` in
# the 2nd. That trap must not be stepped into.
#
# THE NAME IS AN OBSERVATION AND NOT A DERIVED VALUE. A missing clan is
# ``null``, not the id and not an empty string: the report says the absence
# out loud and does not invent a substitute. The SteamID64 (``player_id``)
# stays on every row beside the name -- the name is for readability, the id is
# the only traceable value.
LINEUPS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    "lineup_key": pl.Utf8,  # the lineup id, the same as in the other tables
    "player_id": pl.Utf8,  # SteamID64
    # The player's name in the demo; null if it could not be read.
    "player_name": pl.Utf8,
    # The player's clan name (``team_clan_name``); null if there is none.
    "clan_name": pl.Utf8,
}


# The deaths table: a row per death (Story 2.7).
#
# WHY A TABLE OF ITS OWN RATHER THAN THE ``EVENTS`` TABLE. ``EVENTS``'s
# convention is that the row's ``lineup_key``, ``side`` and ``x, y, z``
# describe one actor of **the row's own team** in one place. A death has two
# actors, and the place of both matters: "Cave dies" is the victim's area,
# "the enemy came through secret from the yard" is the attacker's. As two rows
# it would require renaming ``thrower_id`` and ``grenade_no``; as one row it
# would require five new nullable columns that would be null on every grenade
# row and the other way round -- that is, exactly the weakening of the strict
# schema validation the whole contract relies on.
#
# BOTH AREAS ARE OBSERVATIONS. ``user_last_place_name`` and
# ``attacker_last_place_name`` come from the same ``player_death`` event and
# are not derived values, so the table has no ``area_source`` and no
# ``snap_distance`` column -- those exist precisely for the grenade's
# approximation. Measured 2026-08-30 on ``Ancient_vs_kaljukostaja``: the
# victim's area was missing from 0/151 events, the attacker's from 2/151.
#
# A DEATH WITHOUT AN ATTACKER IS A GENUINE CASE. A fall and the bomb produce a
# row on which every ``attacker_*`` is null. The row is not dropped: the
# victim died, and that is an observation. Those same two rows are the ones
# missing the attacker's area -- they are ``planted_c4`` deaths, in which
# there is no attacker at all. The area therefore did not disappear: there was
# no attacker.
#
# NO DERIVED CONCEPTS. The table holds no trade, no entry and no duel win.
# Those are interpretation, and the division of labour is: observation from
# the machine, interpretation from the human.
DEATHS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    "round_raw": pl.Int32,  # demoparser2's own round counter
    "round_no": pl.Int32,  # 1-based played round
    "t_s": pl.Float64,  # s -- the time since the last round_freeze_end
    # The victim. These fields are the row's identity: a death without a
    # victim is not a death, so a row missing them does not reach the table at
    # all.
    "victim_id": pl.Utf8,  # SteamID64
    "victim_lineup_key": pl.Utf8,  # the victim's lineup on this round
    "victim_side": _SIDE,
    "victim_x": pl.Float32,
    "victim_y": pl.Float32,
    "victim_z": pl.Float32,
    # The game's last_place_name at the moment of death. An observation, not a
    # derived value.
    "victim_area": pl.Utf8,
    # The attacker. **All null or none of them**: a death without an attacker
    # is a genuine case (a fall, the bomb), and half an attacker would be a
    # defect. The exception is ``attacker_area``, which may be missing on its
    # own -- the area is an observation like the others, and its absence is a
    # different thing from the attacker's absence. The exception is also
    # ``attacker_side`` and, with it, ``attacker_lineup_key``: they are
    # derived from the round's side description, not the event's own.
    "attacker_id": pl.Utf8,
    "attacker_lineup_key": pl.Utf8,
    "attacker_side": _SIDE,
    "attacker_x": pl.Float32,
    "attacker_y": pl.Float32,
    "attacker_z": pl.Float32,
    "attacker_area": pl.Utf8,
    # The weapon by the game's own name (``ak47``, ``planted_c4``,
    # ``knife_butterfly``). As it stands and not classified: classifying would
    # be interpretation.
    "weapon": pl.Utf8,
}


# The point cloud: a row per cell (Story 2.9).
#
# WHY THE TABLE EXISTS. The detonation area is a derived value, and a derived
# value without its source cannot be checked. The same principle as with
# ROUNDS.buy_end_tick: a measurement is not presented without what it was read
# from. With this table every ``area_source = "point_cloud"`` row can be
# traced to the cell whose area it got -- without it the reader would have to
# take the number on trust.
#
# WHY THE DEMO'S OWN AND PER DEMO, NOT A PER-MAP ARCHIVE TABLE. An
# accumulating ``callouts/<map>.parquet`` would break a basic property of the
# pipeline: re-parsing the same demo would give a different result depending
# on what other demos happen to be in the archive, and ``params_hash`` could
# not cover that -- the manifest would hold the result up to date although the
# point cloud had changed underneath. Besides, a per-map table would require
# the map's name as an observation, which does not exist yet. The demo's own
# cloud needs neither.
#
# THE CELL IS AN INDEX, NOT A COORDINATE. ``cell_x`` is ``floor(x /
# grid_units)``, and the cell's centre is ``(cell_x + 0.5) * grid_units``. The
# index is an exact integer; the centre would store the same information as a
# float whose rounding could shift the cell. The edge
# (``[parse].callout_grid_units``) is not in the table but in the manifest's
# parameter hash -- changing it re-parses the demo, so the table and the
# setting cannot disagree.
CALLOUT_CLOUD: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    "cell_x": pl.Int32,  # floor(x / [parse].callout_grid_units)
    "cell_y": pl.Int32,
    "cell_z": pl.Int32,
    # The cell's area by the game's own name (env_cs_place). THE MODE and not
    # the first observation: at the edge of a cell there are always rows from
    # the neighbouring area, and the first row would depend on the order
    # demoparser2 gave the ticks in. A tie is settled by the alphabetical
    # order of the name, so that the same demo always gives the same cloud.
    # Never null: an unnamed area does not reach the cloud at all, because a
    # cell named "no name" would name a detonation empty and still look like a
    # hit.
    "area": pl.Utf8,
    # ALL of the cell's observations, not only the winning area's. Always >=
    # 1: a cell comes into being only from an observation, so zero would mean
    # an invented cell.
    #
    # The number says how strongly the cell has been seen: a one-tick cell and
    # a thousand-tick cell count the same in the naming, and that difference
    # can be read only from here.
    #
    # THIN CELLS ARE NOT FILTERED OUT, and that is a measured decision and not
    # an omission. The suspicion was that a single stray step creates a cell
    # that beats its busy neighbour. Measured 2026-08-30 across all six demos:
    # one-observation cells number 1 634/55 429, that is 2.9%, the median
    # number of observations in a cell is 32-42 and p10 is 4. Of the
    # detonations 96/2 544 (3.8%) end up in a one-observation cell -- and even
    # those are cells where somebody REALLY stood. A filter would remove the
    # map's edges and corners, which is exactly where utility is thrown.
    "observations": pl.Int32,
}


# The match table: one row per demo (Story 2.11).
#
# WHY A TABLE OF ITS OWN RATHER THAN A COLUMN. ``ROUNDS``, ``TICKS``,
# ``EVENTS`` and ``DEATHS`` describe rounds; the map is a property of the
# **match**. As a column it would repeat the same on every row -- tens of
# thousands of times -- and would also have to travel through ``classify`` to
# reach ``aggregate``. ``LINEUPS`` is the precedent for this: the team's
# identity was separated into a table of its own for exactly the same reason.
#
# ONE ROW PER DEMO. The table is not a list of observations but one match, so
# the row count is part of the contract and ``stages.parse`` enforces it. Zero
# rows would mean a demo without a match and two rows two matches in the same
# file; neither is true.
#
# THE NAME IS AN OBSERVATION AND NOT A DERIVED VALUE. ``map_name`` is read
# from the demo header (``parse_header()``) and used **as it stands**: it is
# not validated against the map pool, because a map outside the pool (a
# workshop version, ``de_train``) is a genuine observation and not an unknown
# map. A missing name is ``null`` and not a substitute, and an empty string is
# not a name -- and only then does ``aggregate`` fall back to inferring the
# name from ``map_demo_id``.
#
# THE TABLE IS EXTENSIBLE. Match-level observations (Epic 3) fit here as new
# columns without another round of tables.
MATCH: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, the join key
    # The map's name from the demo header, for example ``de_ancient``; null if
    # the header held no name.
    "map_name": pl.Utf8,
}


# The decision inputs the classify stage stores (AD-4): every value used in
# the comparison, so that the report's round appendix can be checked against
# the demo. The threshold values are dollars per player, the same as in the
# [thresholds] section.
CLASSIFIED_INPUTS = pl.Struct(
    {
        "money_buy_end": pl.Int32,
        # The money that was available = money_buy_end + money_spent. It is
        # included for the justification and for checking; the calibration of
        # 2026-08-29 showed that the rules rely on the balance left over.
        "money_spent": pl.Int32,
        # The distribution as it stands, so that a row of the round list can
        # be checked against the demo without another parse. Without this the
        # reader would see only the counter "5/5 can buy" and could not check
        # it.
        "money_players": pl.List(pl.Int32),
        "equip_buy_end": pl.Int32,
        "equip_round_start": pl.Int32,
        "survivors_prev": pl.Int32,
        "survivors_equip_prev": pl.Int32,
        "prev_round_won": pl.Boolean,
        # players = the divisor actually used in the per-player values;
        # players_readable = how many players were readable. They differ only
        # if the observation was outside the bounds 1..roster_size.
        "players": pl.Int32,
        "players_readable": pl.Int32,
        # Condition A's observation (ROUNDS.players_armed_buy_end) and
        # condition B's two derived values: the loss bonus that would be in
        # force if this round is lost, and the number of players for whom it
        # plus the money left in their pocket is enough for a normal buy.
        # players_can_buy is null if the distribution could not be obtained.
        "players_armed": pl.Int32,
        "loss_bonus_if_lost": pl.Int32,
        "players_can_buy": pl.Int32,
        "full_equip_min": pl.Int32,
        # Force, half-buy and eco: the precondition for all of them is the sum
        # bought (force_buy_min). After that two conditions, BOTH of which
        # have to hold for the round to be a half-buy:
        #   A  armed_players_min                 tells a half-buy from an ECO
        #   B  normal_buy_money_min +
        #      normal_buy_players_min            tells a half-buy from a FORCE
        # Neither is enough on its own: measured on inferno_vs_ryhmarama,
        # rounds 6 (force) and 10 (half-buy), on which condition A is
        # identical (5/5).
        "force_buy_min": pl.Int32,
        "armed_players_min": pl.Int32,
        "normal_buy_money_min": pl.Int32,
        "normal_buy_players_min": pl.Int32,
        "anomaly_equip_max_after_win": pl.Int32,
    }
)

# The classified table: one row per round from the subject team's point of
# view. The sample points are not copied -- aggregate joins the parsed/ticks
# table with the key (map_demo_id, round_no), which is therefore in both
# tables.
CLASSIFIED: Schema = {
    "map_demo_id": pl.Utf8,  # the join key to the parsed/ticks table
    "round_no": pl.Int32,
    "side": _SIDE,
    "won": pl.Boolean,
    "round_type": _ROUND_TYPE,
    "opp_round_type": _ROUND_TYPE,
    "loss_count": pl.Int32,
    "reason": pl.Utf8,
    "inputs": CLASSIFIED_INPUTS,
    "is_league": pl.Boolean,
    "roster_class": _ROSTER_CLASS,
}

# Name -> schema, so that an error message and a manifest can refer to a table
# by name.
SCHEMAS: dict[str, Schema] = {
    "rounds": ROUNDS,
    "ticks": TICKS,
    "events": EVENTS,
    "lineups": LINEUPS,
    "deaths": DEATHS,
    "callouts": CALLOUT_CLOUD,
    "match": MATCH,
    "classified": CLASSIFIED,
}


def _type_name(dtype: object) -> str:
    """Return a readable type name for an error message."""
    return str(dtype)


def validate(
    df: pl.DataFrame,
    schema: Schema,
    name: str,
    advice: str | None = None,
) -> pl.DataFrame:
    """Check that ``df`` matches the contract ``schema`` exactly.

    The check is strict in both directions: a missing column, an extra column
    and a wrong type are all errors. The order of the columns does not matter.

    Args:
        df: The table to check.
        schema: The contract in the form ``{column name: Polars type}``.
        name: The table's name for the error message, for example
            ``"rounds"``.
        advice: A replacement instruction for the end of the error message.
            The default instruction speaks to a developer ("add the column or
            fix the stage that produced the table"), because most often it is
            code that breaks the contract. A table read from the archive is a
            different situation: it was broken by an earlier version of the
            program itself, and the user cannot fix it by editing code. Then
            the caller gives their own instruction, so that the wrong advice
            does not end up in front of the user.

    Returns:
        The same ``df``, unchanged.

    Raises:
        SchemaError: If the table does not match the contract. The message
            names exactly the missing, extra or wrongly typed column.
    """
    actual = dict(df.schema)

    missing = [col for col in schema if col not in actual]
    if missing:
        listing = ", ".join(f"{col} ({_type_name(schema[col])})" for col in missing)
        raise SchemaError(
            f"The table {name!r} is missing a column: {listing}. "
            + (
                advice
                if advice is not None
                else "Add the column or fix the stage that produced the table "
                "-- the contract is in the file domain/schemas.py."
            )
        )

    extra = [col for col in actual if col not in schema]
    if extra:
        listing = ", ".join(f"{col} ({_type_name(actual[col])})" for col in extra)
        raise SchemaError(
            f"The table {name!r} has an extra column: {listing}. "
            + (
                advice
                if advice is not None
                else "Remove the column or add it to the contract in the file "
                "domain/schemas.py."
            )
        )

    wrong = [
        (col, schema[col], actual[col]) for col in schema if actual[col] != schema[col]
    ]
    if wrong:
        listing = "; ".join(
            f"{col}: expected {_type_name(exp)}, got {_type_name(got)}"
            for col, exp, got in wrong
        )
        raise SchemaError(
            f"A column of the table {name!r} has the wrong type -- {listing}. "
            + (
                advice
                if advice is not None
                else "Convert the column to the right type before writing."
            )
        )

    return df
