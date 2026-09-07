"""The ports through which the stages see the outside world (AD-8).

A stage must not know demoparser2. It takes a protocol from this module as a
parameter, and the real implementation is handed to it in the call. In the
tests a fake is given instead, one that builds the table by hand -- and then a
stage's logic can be tested without a 233 MB demo.

A port is a ``typing.Protocol``, not a base class: an implementation does not
have to inherit from anything, and the import stays light.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import polars as pl

from pappascout.domain.rounds import REQUIRED_COLUMNS as _NUMBERING_COLUMNS
from pappascout.domain.schemas import (
    CALLOUT_CLOUD,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    ROUNDS,
    TICKS,
)

__all__ = [
    "DemoParser",
    "DemoTables",
    "ParseDiagnostics",
    "MatchSource",
    "Match",
    "MatchTeam",
    "RosterPlayer",
    "DemoSource",
    "DemoStream",
    "ROUNDS_ADAPTER_COLUMNS",
    "TICKS_ADAPTER_COLUMNS",
    "EVENTS_ADAPTER_COLUMNS",
    "LINEUPS_ADAPTER_COLUMNS",
    "DEATHS_ADAPTER_COLUMNS",
    "CALLOUTS_ADAPTER_COLUMNS",
    "MATCH_ADAPTER_COLUMNS",
]

#: The columns the rounds table has **exactly** -- no more and no fewer.
#:
#: Two differences from the ``ROUNDS`` contract:
#:
#: ``map_demo_id`` is missing
#:     The adapter is given nothing but a file path and cannot know the
#:     archive's id. ``stages.parse`` attaches it.
#: ``score_start`` and ``score_end`` are included
#:     They are the **combined score** at the start and at the end of the
#:     round, and :func:`~pappascout.domain.rounds.mark_played_rounds` decides
#:     from them whether the round was played. ``stages.parse`` drops them
#:     before writing, so they never reach the disk. Without them in the
#:     contract another adapter would pass the stage's column check and fall
#:     over only in the domain layer.
ROUNDS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    [name for name in ROUNDS if name != "map_demo_id"]
    + [name for name in _NUMBERING_COLUMNS if name not in ROUNDS]
)

#: The columns the sample-point table has **exactly** -- no more and no
#: fewer.
#:
#: One difference from the ``TICKS`` contract: ``map_demo_id`` is missing for
#: the same reason as in the rounds table. ``round_no`` is included but
#: **always empty**: the adapter knows only the demo's own ``round_raw``
#: counter, and the numbering is owned by
#: :func:`~pappascout.domain.rounds.mark_played_rounds`, which only
#: ``stages.parse`` calls. The stage joins the number on the ``round_raw``
#: key and drops the rows of unnumbered rounds as it does so.
TICKS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in TICKS if name != "map_demo_id"
)

#: The columns the utility event table has **exactly** -- no more and no
#: fewer.
#:
#: The same two exceptions as in the sample-point table: ``map_demo_id`` is
#: missing and ``round_no`` is included but always empty. The trajectory's own
#: running number (``grenade_no``) **is** included: it is the only tie between
#: the throw and the detonation, and it is unique across the whole demo,
#: unlike ``grenade_entity_id``. Between demos the key is the pair
#: ``(map_demo_id, grenade_no)``.
EVENTS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in EVENTS if name != "map_demo_id"
)

#: The columns the lineup table has **exactly** -- no more and no fewer.
#:
#: One difference from the ``LINEUPS`` contract: ``map_demo_id`` is missing
#: for the same reason as in the other tables. ``round_no`` is not there at
#: all -- a lineup and a name are properties of the map and not of the round,
#: so the numbering does not concern this table and its rows are not dropped
#: along with the knife round.
LINEUPS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in LINEUPS if name != "map_demo_id"
)

#: The columns the deaths table has **exactly** -- no more and no fewer.
#:
#: The same two exceptions as in the sample-point and the event table:
#: ``map_demo_id`` is missing and ``round_no`` is included but **always
#: empty**. The adapter knows only the demo's own ``round_raw`` counter, and
#: the numbering is owned by
#: :func:`~pappascout.domain.rounds.mark_played_rounds`. That is exactly why
#: the knife round's deaths -- of which the data really does have some -- fall
#: out by the same mechanism as its sample points and its grenades, and not
#: separately.
DEATHS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in DEATHS if name != "map_demo_id"
)

#: The columns the point-cloud table has **exactly** -- no more and no fewer.
#:
#: One difference from the ``CALLOUT_CLOUD`` contract: ``map_demo_id`` is
#: missing for the same reason as in the other tables. ``round_no`` is not
#: there at all, and it must not be added: the point cloud is a property of
#: the **map** in this demo and not an observation about a round, so its rows
#: do not fall out along with the knife round -- the same rule as for the
#: lineup table. The cloud is deliberately gathered from **all** of the demo's
#: ticks, the warm-up and the knife round included: the question is "where on
#: the map is it possible to stand, and which area is that", and the data of
#: the played rounds alone does not answer it.
CALLOUTS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in CALLOUT_CLOUD if name != "map_demo_id"
)


#: The columns the match table has **exactly** -- no more and no fewer.
#:
#: One difference from the ``MATCH`` contract: ``map_demo_id`` is missing for
#: the same reason as in the other tables. ``round_no`` is not there at all
#: and must not be added: a match is not a round, and that very difference is
#: the whole justification for the table.
MATCH_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in MATCH if name != "map_demo_id"
)


@dataclass(frozen=True)
class DemoTables:
    """All of one demo's parsed tables from the same read.

    The port returns them together rather than in three calls, and the reason
    is consistency and not speed: ``lineup_key``, ``side`` and ``round_raw``
    are computed **once** and reach every table with the same values.
    Separate calls would do the lineup identification again, and if they ever
    diverged, the join ``(map_demo_id, round_no)`` would silently go
    crosswise.

    As a side effect a 233 MB demo is decompressed and read only once.

    Attributes:
        rounds: The rounds table, columns :data:`ROUNDS_ADAPTER_COLUMNS`.
        ticks: The sample-point table, columns
            :data:`TICKS_ADAPTER_COLUMNS`.
        events: The utility event table, columns
            :data:`EVENTS_ADAPTER_COLUMNS`. The field is mandatory and has no
            default: an empty default would let an old port implementation
            look like a demo in which not a single grenade was thrown.
        lineups: The lineup table, columns :data:`LINEUPS_ADAPTER_COLUMNS`.
            One row per (lineup, player): the player's name and their clan
            name. The field is mandatory for the same reason as ``events``:
            an empty default would let a nameless port look like a demo that
            has no names in it.
        deaths: The deaths table, columns :data:`DEATHS_ADAPTER_COLUMNS`. One
            row per death, the victim and the attacker both with their areas.
            The field is mandatory for the same reason as the two before it:
            an empty default would let an old port implementation look like a
            demo in which nobody died.
        callouts: The point cloud, columns
            :data:`CALLOUTS_ADAPTER_COLUMNS`. One row per cell. It is the
            **source** of the detonation areas in the ``events`` table, and
            that is exactly why it is in the same return value rather than in
            a call of its own: two calls could build the cloud twice and with
            a different result, and then the table's row would no longer
            explain the area that is in the table. The field is mandatory for
            the same reason as the ones before it.
        match: The match table; its content is described in
            :meth:`DemoParser.parse_demo`. The field is mandatory for the
            same reason as the ones before it: an empty default would let an
            old port implementation look like a demo whose header has no map
            in it -- and that difference decides whether the name is read as
            an observation or inferred from the id.
    """

    rounds: pl.DataFrame
    ticks: pl.DataFrame
    events: pl.DataFrame
    lineups: pl.DataFrame
    deaths: pl.DataFrame
    callouts: pl.DataFrame
    match: pl.DataFrame


@dataclass(frozen=True)
class ParseDiagnostics:
    """Observations that do not fit into the ``ROUNDS`` contract.

    Columns must not be added to the table without a schema change (AD-2),
    but the user still has to be told when some value is a default rather
    than a measurement. The adapter may offer this as an optional
    ``diagnostics`` attribute; the stage reads it cautiously (``getattr``), so
    a port implementation is not obliged to offer it.

    Attributes:
        tick_rate: The tickrate that was used.
        tick_rate_measured: ``True`` if the tickrate was measured from the
            demo; ``False`` if a default had to be fallen back on.
        rounds_seen: The number of **round boundaries** found in the demo.
            Played and unplayed rounds are included, and so are the
            boundaries that are not rounds at all (``match_restarts``). The
            number is therefore always at least as large as the number of
            rounds.
        match_restarts: Restarts of the match. A restart has a freezetime
            anchor but no ``round_end``, and the demo's own round numbering
            **continues over it by one** -- it does not, in other words,
            consume a round number. In league matches there is exactly one,
            right after the knife round. It is played, but it is not a round
            and it produces no row in any table -- just like the knife round.
            The number is reported because the drop must not be silent; zero
            is the normal result for older demos.

            **A different thing from** ``stages.parse``'s
            ``utility_unnumbered_rounds``, which counts throws from rounds
            that have no ``round_no``. Here it is ``round_raw`` that is
            missing, and the round does not exist at all.
        partial_samples: Sample points from which fewer players were obtained
            than from the demo's best point. Zero is the normal result; a
            systematic prop fault would show in this number already at the
            parsing stage and not only as skewed aggregates.
        sample_rows_without_pawn: Player rows that were skipped because the
            player's **controller was there but the pawn was not**: every
            pawn field (alive state, area, x/y/z) was empty on the same row.
            The player is then not on the map at all, so their row does not
            exist -- the alive state is not guessed in either direction.

            **The name's prefix limits the number to what it measures.** It
            covers the ticks read with the sample-point props -- the setup's
            sample points and utility's throw ticks -- and not the whole
            demo: the point cloud's full tick series and the reading of the
            round boundaries do not add to it. The same tick may be read by
            both calls, and it is then counted once: the number is rows and
            not row readings.

            **An observation and not a fault**, but it must not be silent: a
            missing player makes that round's setup smaller, and the reader
            has to see it. The round stays in the sample. Zero is the normal
            result; measured in the ``anubis_vs_RCAVE_VETERANS`` demo as 15
            rows -- **one player on one round**, five from the sample point's
            tick and ten from the throw ticks. Zero in the archive's seven
            other demos.

            **A different thing from a spectator**, who has no controller
            team -- spectator rows are not in this number. **A different
            thing, too, from a missing alive state on its own**: if the
            position or the area is there but ``m_lifeState`` is missing, the
            run still falls over, because then it is a change in the
            library's field name and not a state of the player.
        sample_points_without_pawn: Sample points from which **no row at all**
            came, because every row was pawnless. The point is missed
            entirely: the round loses that sample point, but the run
            continues.

            **A number of its own and not part of ``partial_samples``.** A
            wholly missing point is more serious than an incomplete one, and
            lumped in with the incomplete ones it would look milder than it
            is. Zero is the normal result, also when
            ``sample_rows_without_pawn`` is something other than zero: one
            pawnless player out of ten leaves the point incomplete but not
            empty.
        grenade_throwers_without_row: Throws whose **thrower was not among the
            rows of the throw's tick**. A throw's area is the thrower's own
            ``m_szLastPlaceName``, so without their row the area stays empty
            and cannot be substituted for: the point cloud names detonations,
            not throws.

            **A fault and not an observation.** The number has existed since
            Story 2.10: before the pawnless row was skipped, such a throw
            brought the run down at the alive-state guard, and without a
            counter of its own it would now leak silently into the
            ``utility_without_area`` number with no reason given. Zero is the
            expected value -- a thrower has a pawn at the moment they throw --
            and it is zero also in the measured ``anubis_vs_RCAVE_VETERANS``
            demo, in which the pawnless player threw nothing.
        unknown_side_events: Damage events in which the side of the actor or
            of the victim could not be established. They do not qualify as a
            first contact, so a round can lose its contact -- the number says
            when that happened.
        grenades_without_thrower: Trajectories that have no thrower. The row
            would be left without a team, so the grenade is skipped
            altogether.
        grenades_outside_rounds: Grenades whose throw does not fall inside the
            boundaries of any round -- in practice a warm-up before the first
            anchor, or a throw after the round has been decided. They have no
            ``t_s``, so they cannot be assigned to any round.
        grenades_unknown_side: Grenades whose thrower's side could not be
            established either from the lineup or from the round's own tick.
            A wrong team would put the utility on the opponent's account, so
            the row is skipped.
        grenades_unknown_type: Grenades whose class name is not known. The
            name survives in the table as it stands; the number exposes a
            renaming in demoparser2 before it shows up in the report.
        grenades_fire_type_unresolved: Fire grenades whose molotov/incendiary
            distinction was not resolved. The type stays ``molotov``, so
            without the number a complete breakdown of the distinction would
            look like a demo in which nothing but molotovs were thrown.
        grenades_detonating_after_round: Detonations that fall after the round
            has ended. **An observation and not a drop**: the row gets its
            area like all the others, because the point cloud is a property of
            the map and does not depend on where the players are at that
            moment. In Story 2.2 these were deliberately left without an area
            -- back then the area came from the nearest living player, and
            after the round that would have told the next round's spawn. The
            reason went away with the method; the number stays, because a late
            detonation is still a phenomenon of its own.
        grenade_ticks_without_players: **Throw** ticks from which no player
            row at all was obtained. **A fault and not an observation**: the
            thrower's own area could not even be attempted. Detonation ticks
            are no longer read at all -- their area comes from the point cloud
            and not from the tick's players.

            **A wholly pawnless tick is not in this number.** It is an
            observation by the same rule as with the sample points -- the demo
            returned rows, and nobody simply happened to be on the map -- and
            it has already been counted into ``sample_rows_without_pawn``. The
            same phenomenon must not be a fault on one path and an
            observation on another.
        callout_cloud_rows_read: The rows that building the point cloud read
            from the demo (the whole demo's tick series, one row per player
            per tick). The number is kept, because it is the only place where
            the cost of this step shows: it is orders of magnitude larger than
            any other tick figure, and that is exactly why the data is dropped
            into a grid straight away.

            **The number of valid rows is not here**: it is the sum of the
            ``observations`` column of the ``callouts`` table, because every
            valid row ends up in exactly one cell. The stage computes it from
            there, and the **ratio** of these two is what tells whether the
            cloud is healthy -- 71-78 % in the measured data.
        callout_cloud_empty_reason: Why the point cloud came out empty, or
            ``None`` if it did not. An empty cloud **does not bring the run
            down**: every detonation area stays null and the run continues.
            Without a reason it would, however, look like a demo in which no
            utility was thrown -- precisely the silent fault that every other
            drop counter exists against.
        grenades_sharing_an_entity_id: Trajectories that share the game's own
            ``grenade_entity_id`` with another trajectory on the same
            ``round_raw``. **An observation and not a fault**: the table's key
            is ``grenade_no``, which is unique across the whole demo, so the
            recycling mixes nothing up. The number is trajectories and not
            pairs -- three trajectories on one id is 3 -- and it is kept
            because it was exactly what exposed that the pair
            ``(round_no, grenade_entity_id)`` would not do as a key.
        unknown_inventory_items: Inventory names the weapon classification
            does not know, as pairs ``(name, occurrences)`` in alphabetical
            order. An unknown name **does not arm** a player (the
            classification is a list of what is allowed), so without this list
            a new knife skin and a new weapon would look exactly alike: no
            trace of either. The occurrence count is included, because it is
            what tells them apart: one exotic knife shows up once or twice, a
            demoparser2 renaming on every row. Empty is the normal result.
        buy_window_seconds: The length of the buy window this run was made
            with (``[parse].buy_window_seconds``). Included so that the run's
            output says at what moment the numbers were read -- without it the
            reader cannot see whether the run measured from the anchor or from
            the end of the buy time. ``None`` means a port that does not say;
            that is a different thing from 0.0.
        buy_window_cuts: Rounds cut short by a death, as pairs
            ``(round_raw, how many buys were left behind the cut)``.
            **Pairs rather than a finished number**, because the adapter does
            not know which rounds end up in the table: the knife round gets a
            ``round_raw`` of its own but ``stages.parse`` drops it. The stage
            filters these against the played rounds and only then computes the
            numbers shown to the user -- the same rule as with the sample
            points and utility (see this class's closing note).

            A cut is **an observation and not a fault**: it is a rule, because
            a dead player's inventory empties and their armor resets, and it
            hits about half of the rounds (69/134 in the measured data). The lost buys **ought to be zero**:
            in the measured data (134 rounds) not a single death precedes the
            last buy.
        buy_window_unchecked_cuts: Cut rounds (``round_raw``) on which the
            lost buys **could not be checked**: from the tick at the end of
            the window not a single player gave a readable ``cash_spent``
            value. Without this, a zero for the lost buys would mean two
            different things -- "nothing was lost" and "it is not known".
        buy_window_ticks_without_players: Rounds on which the tick at the end
            of the buy time gave no player row at all and the measurement fell
            back to the anchor. **A fault and not an observation**: in
            practice the demo is truncated mid-round. Without the fallback
            rule the whole round's economy would be empty. Such a round is not
            recorded as a cut, because the measurement then did not hit the
            cut point at all.
        buy_window_players_lost: Players who were readable at the anchor but
            no longer at the measuring point, counted per team row. The sums
            and the divisor shrink together, so the per-player values stay
            correct -- but the team looks as if it were playing short-handed,
            and that is a different claim from "the connection dropped
            mid-round".
        buy_window_sides_without_rows: Team rows from which not a single
            readable player was obtained at the measuring point, even though
            one was at the anchor. The row goes into the table empty but with
            the state ``ok``, and ``classify`` leaves it unclassified because
            of the missing observation -- the right outcome, but without this
            number nobody would get to know why.
        buy_window_refunds: Player rows on which ``cash_spent`` **fell**
            between the anchor and the measuring point, that is, a purchase
            was refunded. The prop only grows on buys, so a fall is an
            unambiguous sign of a refund and does not get mixed up with a
            death. Measured: 8 player rows on 7 rounds out of six demos.
        buy_window_stale_equipment: Player rows on which the equipment value
            rose without the player buying, receiving armor or changing their
            inventory. It is the stale reading a refund leaves behind: CS2
            returns the money and the armor correctly, but
            ``m_unCurrentEquipmentValue`` does not always come down with them.
            Measured: 1 player row out of 134 rounds, the effect at most
            1 000 $ per player, that is 200 $/player at team level. **It does
            not concern the armed counter**, which reads the inventory and the
            armor.
        lineup_name_conflicts: Players for whom **more than one name** was
            observed on the same map's anchor ticks.
        lineup_clan_conflicts: Players for whom **more than one clan** was
            observed on the same map's anchor ticks.

            Both are zero in the measured data (five demos, 2026-08-30), and
            that measurement is the whole foundation of the lineup table: it
            says that a name is a property of the map and not of the round,
            and that a clan follows the player and not the side. The
            assumption is **unchecked at run time** without these numbers: the
            table writes the mode, so a broken assumption would look exactly
            the same in the table as an intact one. A number other than zero
            is therefore the symptom the warning about the trap of reading
            through the side is talking about.
        deaths_without_tick: Deaths whose tick was not readable. Without a
            tick a death cannot be assigned to a round and ``t_s`` cannot be
            computed. Zero is the expected value; the number exists because
            every other reason for a drop is reported and this one must not be
            an exception.
        deaths_outside_rounds: Deaths that do not fall inside the boundaries
            of any round -- a warm-up before the first anchor, or a death
            between the round being decided and the next buy time. They have
            no ``t_s``, so they cannot be assigned to any round. **A different
            thing from the knife round's deaths**: those are inside a round,
            get their ``round_raw`` and fall out only in ``stages.parse``'s
            numbering along with the other tables.
        deaths_without_victim: Deaths **with no victim**: the event has no
            ``user_steamid`` at all. A different thing from a missing side,
            and hence a number of its own -- combined, it would look like a
            fault in the side inference that is not there.
        deaths_without_victim_side: Deaths whose victim is known but whose
            side could not be established either from the lineup, from the
            round's own tick or from the event's ``user_team_num`` field. The
            row is dropped: ``victim_lineup_key`` is the whole table's join
            key, and without it the death belongs to nobody.
        deaths_attacker_without_side: Deaths whose **attacker's** side could
            not be established, even though the attacker is known. The row
            survives and the attacker's observations (id, coordinates, area)
            with it; only ``attacker_side`` and ``attacker_lineup_key`` stay
            empty. Dropping it would take the victim's death with it, and
            emptying the attacker would throw away an observation that is
            readable.
        armed_unreadable_rows: Team rows on which the armed counter was left
            empty because some player's armor **or** inventory was not
            readable. **A fault and not an observation**: rounds without an
            anchor are not in this number, so a value other than zero means a
            prop fault. Without a number of its own it would get mixed up with
            honest "no observation" rows.
        armored_unreadable_rows: The same for the armored counter, whose
            readability condition is narrower: **armor only**. Two numbers and
            not one, because a shared number would not tell apart a row on
            which the armor went unread from a row on which the inventory
            alone failed -- and it is exactly the latter that empties only the
            upper counter. The difference
            ``armed_unreadable_rows - armored_unreadable_rows`` is therefore
            "rows on which only the inventory failed", and this cannot be
            larger than the one before it.

    The **counts** of sample points, first contacts and utility events **are
    not here**: they are read from the finished table in the stage. The
    adapter would count the unnumbered rounds in as well, and the same name
    with a different denominator is read wrongly.

    **The same goes for the buy window**, and that is why ``buy_window_cuts``
    and ``buy_window_unchecked_cuts`` are ``round_raw`` numbers and not
    finished figures: the knife round gets a ``round_raw`` of its own, but it
    is not a round and does not end up in the table. The adapter's "13 cuts"
    would be 12 in the table the user sees -- and the distribution of the
    measuring moments would begin at fractions of a second, because the knife
    round is decided before the window ends. The distribution of the measuring
    moments is accordingly computed entirely in the stage from the columns
    ``freeze_end_tick`` and ``buy_end_tick``.

    The per-player fault counters (``buy_window_players_lost``,
    ``buy_window_sides_without_rows``, ``buy_window_ticks_without_players``,
    ``buy_window_refunds``, ``buy_window_stale_equipment``) are integers
    instead, and they **include the knife round**. They are fault counters
    whose expected value is zero, so a fault observed on the knife round is
    just as worth telling as one on any other -- and it must not be filtered
    away.
    """

    tick_rate: float
    tick_rate_measured: bool
    rounds_seen: int
    match_restarts: int = 0
    partial_samples: int = 0
    sample_rows_without_pawn: int = 0
    sample_points_without_pawn: int = 0
    grenade_throwers_without_row: int = 0
    unknown_side_events: int = 0
    grenades_without_thrower: int = 0
    grenades_outside_rounds: int = 0
    grenades_unknown_side: int = 0
    grenades_unknown_type: int = 0
    grenades_fire_type_unresolved: int = 0
    grenades_detonating_after_round: int = 0
    grenade_ticks_without_players: int = 0
    grenades_sharing_an_entity_id: int = 0
    callout_cloud_rows_read: int = 0
    callout_cloud_empty_reason: str | None = None
    #: Why the map name was not obtained from the demo's header, or ``None``
    #: if it was.
    #:
    #: A missing name is a legal observation, but it has **three different
    #: reasons**: the header has no ``map_name`` field at all, the field is
    #: empty, or the whole header is not readable as a dictionary. The first
    #: means in practice a field demoparser2 has renamed, and without this
    #: breakdown the whole archive would go back to per-demo map branches
    #: without a single sign of it -- the same fault class as Story 2.10's
    #: pawnless player. The same rule as with ``callout_cloud_empty_reason``:
    #: the reason for an empty result is visible only at the moment of
    #: reading, so it travels in the diagnostics.
    header_map_name_missing_reason: str | None = None
    unknown_inventory_items: tuple[tuple[str, int], ...] = ()
    lineup_name_conflicts: int = 0
    lineup_clan_conflicts: int = 0
    deaths_without_tick: int = 0
    deaths_outside_rounds: int = 0
    deaths_without_victim: int = 0
    deaths_without_victim_side: int = 0
    deaths_attacker_without_side: int = 0
    armed_unreadable_rows: int = 0
    armored_unreadable_rows: int = 0
    buy_window_seconds: float | None = None
    buy_window_cuts: tuple[tuple[int, int], ...] = ()
    buy_window_unchecked_cuts: tuple[int, ...] = ()
    buy_window_ticks_without_players: int = 0
    buy_window_players_lost: int = 0
    buy_window_sides_without_rows: int = 0
    buy_window_refunds: int = 0
    buy_window_stale_equipment: int = 0


@runtime_checkable
class DemoParser(Protocol):
    """The port that reads all seven tables out of a demo.

    An implementation must return the **observed** values as they stand: no
    round-type classification, no loss count, no aggregation, nothing else
    derived. The only inference that belongs here is identifying the round
    boundaries and choosing the sample points inside them.

    **Two operations and not one (Story 3.6).** Reading the header used to be
    the adapter's private step inside a full parse, because nobody needed it
    on its own. The import does: it asks for the map's name before the file is
    moved into place, and decompressing 230 MB into six tables is not an
    answer to that question. There were three alternatives, and two were
    rejected: calling the private ``_header_map_name`` (it would break the
    layer boundary that ``tests/test_layering.py`` polices, and it would tie
    the import to the innards of one adapter) and picking the name out of
    :meth:`parse_demo`'s ``match`` table (it would do exactly the work that is
    meant to be saved). What is left is extending the port: the same
    observation, an operation of its own.
    """

    def read_map_name(self, path: Path) -> str | None:
        """Read the map's name from the demo's header **without parsing the demo**.

        Args:
            path: The demo file, either ``.dem`` or compressed ``.dem.zst`` /
                ``.dem.gz``. A compressed file is decompressed, because the
                header is inside the compression -- but only the header is
                read, not the tables.

        Returns:
            The map's name **as an observation**, or ``None`` if the header
            has no readable name.

            **The name is not compared against the map pool**, and that is the
            same rule as in :meth:`parse_demo`'s ``match`` table: a workshop
            version or ``de_train`` is a genuine observation and not an
            unknown map, and a silent correction to the pool's name would make
            it a lie. In this operation the rule carries even further: the
            import compares the name against FACEIT's veto data, and if the
            port had already corrected the name to the "right" one, the
            comparison could never notice a discrepancy -- that is, the very
            check this method exists for would be dead.

            A name that is empty or nothing but spaces is ``None`` and not a
            substitute: "no observation" and "the observation is empty" are
            different things, and only the former is true.

        Raises:
            ~pappascout.errors.ParseError: If the file is not a CS2 demo, it
                cannot be decompressed or the header cannot be read. The
                message says what to do.
        """
        ...

    def parse_demo(
        self, path: Path, sample_seconds: Sequence[float]
    ) -> DemoTables:
        """Read the demo and return all of its tables.

        Args:
            path: The demo file, either ``.dem`` or compressed ``.dem.zst`` /
                ``.dem.gz``.
            sample_seconds: The sample points in seconds from the round's
                freezetime anchor (``[parse].snapshot_seconds``).

        Returns:
            :class:`DemoTables`.

            ``rounds`` is a long table, two rows per round (one for each
            team), columns exactly :data:`ROUNDS_ADAPTER_COLUMNS`.

            ``ticks`` is one row per (player, round, sample point), columns
            exactly :data:`TICKS_ADAPTER_COLUMNS`. There are rows only from
            rounds that have a freezetime anchor and an end tick, and not a
            single sample point after the round has ended.

            ``events`` is one row per utility event, columns exactly
            :data:`EVENTS_ADAPTER_COLUMNS`. The throw and the detonation are
            two rows joined by ``grenade_no`` -- the game's own
            ``grenade_entity_id`` will not do as a key, because it is recycled
            even within the same round. ``grenade_no`` starts at zero and
            grows with the throw's tick; it is unique but **not a contiguous
            range**, because the stage drops the rows of unnumbered rounds.
            A grenade that does not detonate produces only the throw.
            ``area_source`` tells an observed throw area apart from a derived
            detonation area, and ``snap_distance`` gives the distance of the
            latter. An empty table is a valid result -- there was no utility
            in the demo.

            ``lineups`` is one row per (lineup, player), columns exactly
            :data:`LINEUPS_ADAPTER_COLUMNS`. The set of players is the same
            one ``lineup_key`` was computed from, so the table and the id
            cannot disagree. ``player_name`` and ``clan_name`` are
            **observations**: a missing value is ``null`` and not a
            substitute, and an empty string is not a name. The clan is read
            per player and not through the side -- a side changes team at half
            time.

            ``deaths`` is one row per death, columns exactly
            :data:`DEATHS_ADAPTER_COLUMNS`. The victim's and the attacker's
            areas are **observations** from the same event and not derived
            values, so the table has no ``area_source``. A death without an
            attacker (a fall, the bomb) is a genuine case: every
            ``attacker_*`` is then ``null`` and the row is not dropped. An
            empty table is not a valid result -- in a played match people die,
            so an empty table means a broken port.

            ``callouts`` is one row per point-cloud cell, columns exactly
            :data:`CALLOUTS_ADAPTER_COLUMNS`. It is the source of the
            detonation areas in the ``events`` table: every
            ``area_source = "point_cloud"`` row is traceable to a cell in this
            table. An empty table **is** a valid result -- not a single alive
            row in a named area was obtained from the demo -- and then every
            detonation area is ``null``; the reason travels in the
            diagnostics.

            ``match`` is **exactly one row**, columns exactly
            :data:`MATCH_ADAPTER_COLUMNS`. ``map_name`` is the map of the
            demo's header (``parse_header``) **as an observation**: it is
            returned as it stands and is not compared against the map pool,
            because a map outside the pool is a genuine observation. A missing
            or empty name is ``null`` and not a substitute. An empty table is
            not a valid result: a demo always has a match in it, even if its
            map is unknown.

            ``round_no`` is ``null`` on every row in the ``rounds``,
            ``ticks``, ``events`` and ``deaths`` tables -- the numbering is
            decided by ``domain.rounds.mark_played_rounds``, which only
            ``stages.parse`` calls. In the ``lineups``, ``callouts`` and
            ``match`` tables the column is not there at all.

        Raises:
            ~pappascout.errors.ParseError: If the file is not a CS2 demo or
                cannot be read. The message says what to do.
        """
        ...


# -- The port for matches (Story 3.1) ---------------------------------------
#
# The same division as with :class:`DemoParser`: the port is here, the only
# implementation is in a module of its own
# (:mod:`pappascout.adapters.faceit`). The stage does not import ``requests``
# and does not know HTTP -- it sees these three data classes and two methods.


@dataclass(frozen=True)
class RosterPlayer:
    """A player in a match's roster.

    **Two ids, and only one of them appears in demos.** Story 3.1's
    measurement (``mittaus-faceit-aineisto.md`` chapter 2) compared FACEIT's
    roster against the archive's ``lineups.parquet``: the ids they had in
    common were ``game_player_id`` values (SteamID64) and they matched **as
    strings, without conversion**. ``player_id`` is FACEIT's own UUID, which
    does not appear in demos at all. Both are kept, but the one that joins a
    FACEIT roster to the demos is ``game_player_id``.

    Attributes:
        player_id: The FACEIT player id (UUID). The source's own key, the one
            the player's details are fetched with from the interface.
        nickname: The nickname **as an observation**. A missing or empty one
            is ``None`` and not a substitute; a nickname can change, an id
            cannot.
        game_player_id: The game's own player id, in CS2 the **SteamID64**.
            The same value as in the ``player_id`` column of
            ``lineups.parquet``, so this is the only id that joins the roster
            to the demos. ``None`` if the source did not give it -- a missing
            one is not substituted for, because an invented id would join to
            the wrong player.
    """

    player_id: str
    nickname: str | None = None
    game_player_id: str | None = None


@dataclass(frozen=True)
class MatchTeam:
    """One side of a match and its roster.

    Attributes:
        team_id: The source's own team id. **Not the same thing as the
            archive's ``team_key``** (AD-6): the canonical id is decided by
            Story 3.2 from the lineups, and this is only an observation of
            which id the source gave.
        name: The team's name as an observation, or ``None``.
        roster: The players in the order the source gave them. An empty tuple
            is a valid result -- an upcoming match may not have a roster yet.
        substitutes: The substitute players in the order the source gave them.
            **A field of its own and not merged into ``roster``**, because the
            source tells them apart and the difference is an observation: who
            started and who was in reserve. The standing roster (Story 3.2) is
            computed by ``domain.teams`` as a union, and it has to be allowed
            to do that itself -- merging here would take the rule into the
            port, where it cannot be tested without a network. Measured
            2026-09-04: without this list the standing roster systematically
            underestimates (``Lindberq_`` is in the demo but not in
            ``roster``).
    """

    team_id: str | None = None
    name: str | None = None
    roster: tuple[RosterPlayer, ...] = ()
    substitutes: tuple[RosterPlayer, ...] = ()


@dataclass(frozen=True)
class Match:
    """One match in the core's vocabulary.

    **The port does not speak FACEIT's vocabulary** (AD-8): ``faction1``,
    ``voting`` and epoch seconds stay inside the adapter, and what comes here
    is what the stage needs.

    Attributes:
        match_id: The match's id. The same one that is the first part of
            ``map_demo_id`` (``{match_id}-{map_index}``).
        competition_id: The competition's id, or ``None``. **This is what
            settles ``is_league``**: a match is a league match if this is in
            the ``[league].championship_ids`` list -- not from the name,
            because a name is a string written by a human.
        status: The match's state in the source's own word (for example
            ``FINISHED``), or ``None``. It is not interpreted here: the list
            of states is the source's own and not pappascout's, and a guess
            would go stale silently.
        scheduled_at: The match's agreed start time, UTC-aware, or ``None``.
            **The match list carries this rather than ``started_at``**
            (measured 2026-09-04): an unplayed match has no start time, but a
            schedule it does have. Two different fields and not one, because a
            schedule is a plan and a start time is an observation -- and
            mixing them up would claim as played a match that has not been
            played.
        started_at: The real start time, UTC-aware, or ``None`` if the match
            has not started. The match list does not have this; it comes only
            from fetching a single match.
        finished_at: The finish time, UTC-aware, or ``None``.
        teams: The sides. Usually two, but the count is not claimed here --
            the stage checks it if it depends on it.
        map_picks: The maps played, in the order in which they were chosen.
            **The order is the definition of ``map_index``**: ``map_index`` is
            a 0-based index into this tuple, and ``map_demo_id`` is built from
            it. An empty tuple means "no veto data", not "no maps". Modelling
            the whole veto (the bans, the order of turns) is Epic 4's; here
            there is only the list that ``map_index`` needs.
        best_of: How many maps are played in the match at most, or ``None`` if
            the source did not say. Measured 2026-09-04
            (``mittaus-faceit-aineisto.md`` chapter 8): in the raw response
            the value is ``2`` in all 66 matches, but it **did not travel
            through the port** at all -- the length of ``map_picks`` was the
            only number that got here.

            **They are not the same number.** ``map_picks`` is veto data, that
            is, which maps were chosen; ``best_of`` is the rulebook's promise
            of how many of them are played. In a BO3 that ended two to nil the
            veto has three maps but there are two demos, so Story 3.4 cannot
            infer the number of demos to expect from ``map_picks``. The field
            is therefore in the port already, even though Story 3.3 does not
            use it itself: the port should not have to be prised open later
            for something that was measurable now.
    """

    match_id: str
    competition_id: str | None = None
    status: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    teams: tuple[MatchTeam, ...] = ()
    map_picks: tuple[str, ...] = ()
    best_of: int | None = None


@runtime_checkable
class MatchSource(Protocol):
    """The port through which a stage sees matches.

    An implementation may use the network, a cache and retries; the port
    promises nothing about them. The only promise is that an error is an
    :class:`~pappascout.errors.ApiError`.

    **Two methods and not four.** ARCHITECTURE-SPINE (AD-8) also listed
    ``get_roster`` and ``get_veto`` for the port, but the data of both is in
    :class:`Match`'s fields ``teams`` and ``map_picks`` -- a separate method
    would be a second way of fetching the same thing and a second cache key
    for the same response. ``get_schedule`` (Epic 4: the next opponent) is
    :meth:`get_matches` with a time filter, which the stage applies itself on
    ``scheduled_at`` -- **not** on ``started_at``: it was measured 2026-09-04
    that the match list has no start time at all, and the next opponent is by
    definition a match that has not started yet. The port does not, therefore,
    have to be prised open in Epic 4.
    """

    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        """Return **all** of a competition's matches.

        Args:
            competition_id: The competition's id (an element of
                ``[league].championship_ids``).

        Returns:
            The matches as one tuple. The implementation fetches every page,
            so the caller does not paginate: pagination is a detail of the
            transport. An empty tuple is a valid result -- a competition that
            has no matches yet.

        Raises:
            ~pappascout.errors.ApiError: If the matches could not be fetched.
        """
        ...

    def get_match(self, match_id: str) -> Match:
        """Return the details of a single match.

        Args:
            match_id: The match's id.

        Returns:
            :class:`Match`. The rosters and the map picks are more complete
            here than in the match list, if the source tells them apart.

        Raises:
            ~pappascout.errors.ApiError: If the match is not found or could
                not be fetched.
        """
        ...


# -- The port for demos (Story 3.4) -----------------------------------------
#
# A third port on the same pattern: the data class and the protocol here, the
# only implementation in :mod:`pappascout.adapters.faceit`. The stage knows
# neither HTTP nor where the demos are kept -- it sees an id going in and a
# byte stream coming out.


@dataclass(frozen=True)
class DemoStream:
    """One demo's byte stream and what the source said about it.

    **The address is not here, and that is not an oversight.** A signed
    download link is an authorisation and not an address: whoever has it gets
    the file. If it travelled through the port, it would be a variable of the
    caller's -- and a variable becomes, before long, a log line, an error
    message or a field in a metadata file. The port therefore takes a
    ``map_demo_id`` and returns the bytes; the link is born and dies inside
    the adapter.

    **The stream is read once.** ``chunks`` is an iterator and not a list,
    because a compressed demo is 142-223 MB (measured 2026-09-05) and is not
    read into memory whole. The same reason forbids a second read: the caller
    computes the digest while it writes, not afterwards.

    Attributes:
        chunks: The byte chunks in the order the source gives them. The size
            of a chunk is the source's decision; the caller must assume
            nothing about it.
        content_length: The expected size in bytes, **if the source said so**.
            ``None`` means "the source did not say" and not "zero bytes": an
            invented number would turn an intact download into a short one.
            When there is a number, the caller compares it against the amount
            written -- that is exactly what tells a truncated download from a
            finished one.
        on_close: The function that releases the source's resources (an open
            connection), or ``None``. Called from :meth:`close`, which the
            ``with`` statement takes care of.
    """

    chunks: Iterable[bytes]
    content_length: int | None = None
    on_close: Callable[[], None] | None = None

    def close(self) -> None:
        """Release the source's resources. Safe to call many times."""
        if self.on_close is not None:
            self.on_close()

    def __enter__(self) -> "DemoStream":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


@runtime_checkable
class DemoSource(Protocol):
    """The port through which a stage sees demos.

    One method, because the stage needs one thing: the bytes. Everything else
    -- fetching the match, the ``instances`` structure, exchanging the
    download link, retrying -- is inside the implementation, and that is
    exactly why the ``fetch`` stage's tests run without a network.
    """

    def get_demo(self, map_demo_id: str) -> DemoStream:
        """Open a demo's byte stream.

        Args:
            map_demo_id: The unit's id, of the form
                ``{match_id}-{map_index}``. **The port does not take an
                address, nor a match and a map number separately**: the id is
                what the archive knows, and it is the implementation's job to
                resolve it in the source's own vocabulary.

        Returns:
            A :class:`DemoStream`, which has to be closed (a ``with``
            statement).

        Raises:
            ~pappascout.errors.DemoUnavailable: When there is no demo -- the
                match has not been played, the map has no recording, or the
                source has already removed it. **A type of its own and not an
                ``ApiError``**, because the caller's decision is different:
                this is the unit's state ``no_demo``, and it is a final fact
                that is not retried.
            ~pappascout.errors.ApiError: When the source did not answer or the
                download failed. To the caller this is ``download_failed``,
                that is, a situation that a new run may fix.
        """
        ...
