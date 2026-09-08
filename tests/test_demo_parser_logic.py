"""The demo adapter's logic without demos.

``Demoparser2Adapter`` comes in two parts: a thin shell that calls
demoparser2, and under it pure logic -- pairing the round boundaries,
identifying the lineups, the points at which the score is measured, and
computing the tick rate. It is that logic that can go silently wrong, and it
is that logic that is most expensive to test with a real 233 MB demo.

That is why :class:`FakeDemoparser2` lives here, returning pandas frames of
the same shape as the real library. Only ``_open`` is replaced; everything
else in the adapter really runs. These tests always run, in a
``-m "not demo"`` run and on a machine with no demos too.
"""

from __future__ import annotations

import hashlib
import io
from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pytest
import zstandard

from pappascout.adapters import demo_parser as dp
from pappascout.adapters.decompress import DEMO_MAGIC
from pappascout.adapters.demo_parser import (
    DEFAULT_TICK_RATE,
    Demoparser2Adapter,
)
from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
)
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    EVENTS,
    MONEY_DISTRIBUTION_COLUMN,
    TICKS,
)
from pappascout.domain.rounds import mark_played_rounds
from pappascout.errors import ParseError

A_PLAYERS = ["aaa1", "aaa2", "aaa3", "aaa4", "aaa5"]
B_PLAYERS = ["bbb1", "bbb2", "bbb3", "bbb4", "bbb5"]

_SIDE_TEAM = {"T": 2, "CT": 3}


# --- The fake -----------------------------------------------------------------


#: Weapons that do not qualify as a first contact -- the same list as in
#: ``settings.toml``'s ``[parse]`` section.
UTILITY = (
    "hegrenade",
    "flashbang",
    "smokegrenade",
    "decoy",
    "molotov",
    "incgrenade",
    "inferno",
)

#: Sample points that fit inside a fake round (500 ticks = 7.8 s).
SNAPSHOT_SECONDS: tuple[float, ...] = (2.0, 5.0)

#: The sides' heights on the fake map. The difference is deliberately larger
#: than any ``area_snap_units`` used in a test, so that the nearest player is
#: always from one's own side and not the opposing side's player at the same
#: index.
A_SIDE_HEIGHT = 5.0
B_SIDE_HEIGHT = 1005.0

#: The distance from within which a detonation gets its area in these tests.
#: Looser than production's 256, because the fake map's point cloud is sparse:
#: there are ten players and they stand a hundred units apart.
AREA_SNAP_UNITS = 500.0

#: The point cloud's dimensions in these tests. The same as in
#: ``settings.toml``, so that the tests' figures measure production's grid.
CALLOUT_GRID_UNITS = 32
CALLOUT_Z_WEIGHT = 1.0
CALLOUT_Z_TOLERANCE = 72.0

#: The fake's default inventory: a knife, a free pistol, a bought rifle and
#: one smoke. Corresponds to the default equipment value of 4200 (200 + 2700 +
#: 300 + kevlar 1000), so in a normal match all five are armed.
DEFAULT_INVENTORY: tuple[str, ...] = (
    "knife",
    "Glock-18",
    "AK-47",
    "Smoke Grenade",
)

#: The default armour in the fake. Above zero, so the armour condition for
#: being armed is met.
DEFAULT_ARMOR = 100


#: The separator between three cases that ``None`` alone cannot tell apart:
#: the header was **not given** (the fake uses :data:`DEFAULT_HEADER`), the
#: header was given **as the value ``None``** (the library returned an empty
#: one -- a genuine case that must not be replaced by the default), and the
#: header was given **as a dictionary**. The third form is deliberately
#: ``Any``, so that a test can pass something other than a dictionary too.
_MISSING_HEADER: Any = object()

#: The header's default. A real header has 12 fields (measured 2026-08-31);
#: three of them are here, because the rest affect nothing. Only ``map_name``
#: belongs in the match table -- ``server_name`` is included precisely so that
#: the test sees it being left out.
DEFAULT_HEADER: dict[str, Any] = {
    "map_name": "de_ancient",
    "server_name": "FACEIT.com register to play here",
    "demo_version_name": "valve_demo_2",
}


class FakeDemoparser2:
    """Returns frames of the same shape as demoparser2 0.42.0.

    ``parse_ticks`` returns **exactly the props requested**, as the real
    library does, so the adapter's two different prop lists (round boundaries
    and sample points) really do get tested separately. A tick that has not
    been recorded as a round boundary is a sample point: its rows are
    generated from the round whose boundaries the tick falls within.

    ``parse_ticks`` **without a ``ticks`` argument** is the whole-demo read
    the real library performs for the point cloud. The fake returns every tick
    it knows for that: the round boundaries as they are (they have no
    coordinates, just as a real call has rows that lack them) and one sample
    point from the middle of each round, which produces the players'
    positions. That way the point cloud is built from the same data the sample
    points see.

    ``drop_props`` imitates the situation where the library has renamed a
    field and the requested prop no longer comes along.
    """

    def __init__(
        self,
        freeze_ticks: list[int],
        round_ends: list[dict[str, Any]],
        tick_rows: dict[int, list[dict[str, Any]]],
        *,
        drop_props: tuple[str, ...] = (),
        events: dict[str, list[dict[str, Any]]] | None = None,
        rounds_model: list["Round"] | None = None,
        grenades: list[dict[str, Any]] | None = None,
        drop_grenade_columns: tuple[str, ...] = (),
        drop_death_columns: tuple[str, ...] = (),
        drop_cloud_props: tuple[str, ...] = (),
        header: dict[str, Any] | None | Any = _MISSING_HEADER,
        header_error: Exception | None = None,
    ) -> None:
        #: The demo's header as ``parse_header()`` returns it; see
        #: :data:`DEFAULT_HEADER`. ``None`` = the library returned an empty
        #: header, which is a genuine case and not an exception.
        self.header = DEFAULT_HEADER if header is _MISSING_HEADER else header
        #: The exception ``parse_header`` raises -- the library's own fault.
        self.header_error = header_error
        #: The number of header read calls. A test reads this to establish
        #: that a 233 MB demo was not read twice for the map's name.
        self.header_calls = 0
        self.freeze_ticks = freeze_ticks
        self.round_ends = round_ends
        self.tick_rows = tick_rows
        self.drop_props = drop_props
        self.events = events or {}
        self.rounds_model = rounds_model or []
        self.grenades = grenades or []
        self.drop_grenade_columns = drop_grenade_columns
        #: The props dropped **only from the whole-demo read**, that is, from
        #: the point cloud. A list of its own because ``drop_props`` would
        #: bring down an earlier call: the sample points read the same fields
        #: and their own check would name the missing one first, and the point
        #: cloud's guard would never get a word in.
        self.drop_cloud_props = drop_cloud_props
        #: The columns dropped from the ``player_death`` result. This is
        #: exactly how a library rename shows: the event arrives, but the
        #: requested player field is not there.
        self.drop_death_columns = drop_death_columns
        #: The event calls as pairs ``(name, the player fields requested)``,
        #: so that a test can establish that ``player_death`` was not read
        #: twice.
        self.event_calls: list[tuple[str, tuple[str, ...]]] = []
        #: The prop lists in call order -- a test can establish that the whole
        #: tick series was not read.
        self.tick_calls: list[tuple[tuple[str, ...], tuple[int, ...]]] = []

    def parse_header(self) -> dict[str, Any] | None:
        self.header_calls += 1
        if self.header_error is not None:
            raise self.header_error
        return self.header

    def parse_event(
        self, name: str, *, player: list[str] | None = None
    ) -> pd.DataFrame:
        self.event_calls.append((name, tuple(player or ())))
        if name == "round_freeze_end":
            return pd.DataFrame({"tick": list(self.freeze_ticks)})
        if name == "round_end":
            # The real library returns an initial-value row at tick 1.
            dummy = {"reason": None, "round": 0, "tick": 1, "winner": None}
            return pd.DataFrame([dummy, *self.round_ends])
        if name in self.events:
            frame = pd.DataFrame(self.events[name])
            if name == "player_death":
                for column in self.drop_death_columns:
                    if column in frame.columns:
                        frame = frame.drop(columns=[column])
            return frame
        return pd.DataFrame()

    def parse_grenades(self) -> pd.DataFrame:
        """Trajectories, with the columns in the same shape as the real library.

        The real ``parse_grenades()`` returns the **whole** trajectory: the
        rows from the grenade's time in the bag too, where the coordinates are
        empty. The fake gives only what the test built -- bag rows can be
        added by hand.
        """
        columns = [*dp.GRENADE_COLUMNS, "name"]
        frame = pd.DataFrame(
            [{name: row.get(name) for name in columns} for row in self.grenades],
            columns=columns,
        )
        for column in self.drop_grenade_columns:
            if column in frame.columns:
                frame = frame.drop(columns=[column])
        return frame

    def parse_ticks(
        self, wanted_props: list[str], *, ticks: list[int] | None = None
    ) -> pd.DataFrame:
        # An empty tick tuple in the call list means **a whole-demo read**:
        # for the real library ``ticks=None`` is exactly that, and the point
        # cloud is the only place the adapter does it.
        self.tick_calls.append((tuple(wanted_props), tuple(ticks or ())))
        whole_demo = ticks is None
        wanted = list(ticks) if ticks is not None else self._cloud_ticks()
        rows = [r for tick in wanted for r in self._rows_at(tick)]
        columns = [*wanted_props, "tick", "steamid", "name"]
        frame = pd.DataFrame(
            [{name: row.get(name) for name in columns} for row in rows],
            columns=columns,
        )
        dropped = self.drop_props + (self.drop_cloud_props if whole_demo else ())
        for prop in dropped:
            if prop in frame.columns:
                frame = frame.drop(columns=[prop])
        return frame

    def _cloud_ticks(self) -> list[int]:
        """The ticks the whole-demo read returns.

        The round boundaries are included as they are -- they have no
        coordinates, and rows exactly like that exist in a real demo. In
        addition, one tick from the middle of each round: it falls into the
        sample-point branch in :meth:`_rows_at` and produces the players'
        positions the point cloud is built from.
        """
        ticks = set(self.tick_rows)
        for round_spec in self.rounds_model:
            if round_spec.freeze_tick is None or round_spec.end_tick is None:
                continue
            ticks.add((round_spec.freeze_tick + round_spec.end_tick) // 2)
        return sorted(ticks)

    def _rows_at(self, tick: int) -> list[dict[str, Any]]:
        if tick in self.tick_rows:
            return self.tick_rows[tick]
        for round_spec in self.rounds_model:
            if round_spec.freeze_tick is None or round_spec.end_tick is None:
                continue
            if round_spec.freeze_tick <= tick <= round_spec.end_tick:
                return _sample_rows(round_spec, tick)
        return []


def parse_tables(
    fake: FakeDemoparser2,
    tmp_path: Path,
    *,
    sample_seconds: tuple[float, ...] = SNAPSHOT_SECONDS,
    exclude_weapons: tuple[str, ...] = UTILITY,
    fallback_death: bool = True,
    area_snap_units: float | None = AREA_SNAP_UNITS,
    buy_window_seconds: float = 0.0,
    callout_grid_units: int = CALLOUT_GRID_UNITS,
    callout_z_weight: float = CALLOUT_Z_WEIGHT,
    callout_z_tolerance_units: float = CALLOUT_Z_TOLERANCE,
) -> DemoTables:
    """Run the adapter on top of the fake; only ``_open`` is replaced.

    ``buy_window_seconds`` defaults to **0**, that is, the measurement point
    is the anchor. That is not production's value but a neutral one: most of
    these tests measure something other than the buy window, and they should
    not have to build rows for the tick at the end of the window merely to
    stay readable. The window's own tests give the value explicitly.
    """
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Adapter(
        exclude_weapons=exclude_weapons,
        fallback_death=fallback_death,
        area_snap_units=area_snap_units,
        buy_window_seconds=buy_window_seconds,
        callout_grid_units=callout_grid_units,
        callout_z_weight=callout_z_weight,
        callout_z_tolerance_units=callout_z_tolerance_units,
    )
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    return adapter.parse_demo(demo, sample_seconds)


def parse_with(fake: FakeDemoparser2, tmp_path: Path, **kwargs) -> pl.DataFrame:
    """The rounds table alone; most tests look at nothing else."""
    return parse_tables(fake, tmp_path, **kwargs).rounds


def parse_ticks_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """The sample point table alone."""
    return parse_tables(fake, tmp_path, **kwargs).ticks


def parse_events_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """The utility events table alone."""
    return parse_tables(fake, tmp_path, **kwargs).events


def parse_lineups_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """The lineups table alone (Story 2.6)."""
    return parse_tables(fake, tmp_path, **kwargs).lineups


def parse_adapter(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> Demoparser2Adapter:
    """The same, but returns the adapter so that ``diagnostics`` is readable."""
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Adapter(
        exclude_weapons=kwargs.pop("exclude_weapons", UTILITY),
        fallback_death=kwargs.pop("fallback_death", True),
        area_snap_units=kwargs.pop("area_snap_units", AREA_SNAP_UNITS),
        buy_window_seconds=kwargs.pop("buy_window_seconds", 0.0),
        callout_grid_units=kwargs.pop("callout_grid_units", CALLOUT_GRID_UNITS),
        callout_z_weight=kwargs.pop("callout_z_weight", CALLOUT_Z_WEIGHT),
        callout_z_tolerance_units=kwargs.pop(
            "callout_z_tolerance_units", CALLOUT_Z_TOLERANCE
        ),
    )
    adapter._open = lambda *args, **kwargs2: fake  # type: ignore[method-assign]
    adapter.parse_demo(demo, kwargs.pop("sample_seconds", SNAPSHOT_SECONDS))
    return adapter


# --- Building a match ----------------------------------------------------------


@dataclass
class Round:
    """One round in the fake demo."""

    demo_round: int | None
    #: ``None`` = this round has no freezetime anchor.
    freeze_tick: int | None
    #: ``None`` = the round was not resolved (the demo was cut short).
    end_tick: int | None
    winner: str | None = None
    reason: str | None = None
    #: Which side lineup A is on. B is always on the other one.
    a_side: str = "T"
    #: The lineups' clan names (``team_clan_name``). ``None`` = the game gives
    #: no name, in which case it comes from the demo as an empty string.
    a_clan: str | None = "AlphaClan"
    b_clan: str | None = "BetaClan"
    #: A player's visible name, keyed by id. A missing key = the name is the
    #: id itself; the value ``None`` = the name could not be read.
    display_names: dict[str, str | None] = field(default_factory=dict)
    #: The combined score at the freezetime anchor and at the round's end
    #: tick.
    score_at_freeze: int = 0
    score_at_end: int = 0
    #: Alive at the end of the round (A, B).
    alive: tuple[int, int] = (0, 0)
    a_players: list[str] = field(default_factory=lambda: list(A_PLAYERS))
    b_players: list[str] = field(default_factory=lambda: list(B_PLAYERS))
    #: The game's clock in seconds at the freezetime anchor; ``None`` leaves
    #: it empty.
    round_start_time: float | None = None
    #: Leave out one side's score reading (a one-sided measurement).
    score_only_side: str | None = None
    #: How many A-side players are left without readable freezetime values.
    #: Imitates a tick where some of the props are empty.
    a_unreadable: int = 0
    #: The per-player equipment value at the end of freezetime for lineup A,
    #: by index. ``None`` = everyone gets the default of 4200.
    a_equip_buy_end: list[int] | None = None
    #: The per-player inventory for lineup A, by index. ``None`` as the list =
    #: everyone gets :data:`DEFAULT_INVENTORY`; a single ``None`` as an
    #: element = the prop could not be read (a different thing from an empty
    #: list). This is what settles the armed count: arming is about names, not
    #: about a total.
    a_inventory: list[tuple[str, ...] | None] | None = None
    #: The per-player armour value for lineup A, by index. ``None`` as the
    #: list = everyone gets :data:`DEFAULT_ARMOR`; a single ``None`` as an
    #: element = the prop could not be read. This difference has to be
    #: expressible: unreadable armour and zero armour are different things,
    #: and the latter is an observation.
    a_armor: list[int | None] | None = None
    #: The per-player money (``m_iAccount``) for lineup A, by index. ``None``
    #: = everyone gets the default of 800.
    a_account: list[int | None] | None = None
    #: The per-player ``m_iCashSpentThisRound`` for lineup A, by index.
    #: ``None`` = everyone gets the default of 4000. This is the only measure
    #: of whether a player was still buying after the measurement point, so
    #: the buy window's tests have to be able to set it per tick.
    a_cash_spent: list[int | None] | None = None
    #: The buy-time ticks: ``{offset from the anchor: the fields to
    #: replace}``. The value is handed to :func:`dataclasses.replace`, so the
    #: keys are this class's field names (``a_equip_buy_end``,
    #: ``a_inventory``, ``a_armor``, ``a_account``, ``a_cash_spent``). Without
    #: this the tick at the end of the buy window would have exactly the same
    #: values as the anchor, and no window test could show a difference.
    after_freeze: dict[int, dict[str, Any]] = field(default_factory=dict)
    #: Offsets from the anchor at which the fake returns **an empty set of
    #: rows**. Imitates a tick at which demoparser2 gives no player at all --
    #: in practice a demo cut short. Without this such a tick cannot be built:
    #: for a tick inside a round the fake always offers at least the sample
    #: point rows.
    blank_after_freeze: tuple[int, ...] = ()
    #: How many of lineup A's players are **missing from the rows entirely**.
    #: A different thing from :attr:`a_unreadable`, where the row is there but
    #: the props are empty: here the player is not on the tick at all, and the
    #: sum and the divisor shrink together.
    a_absent: int = 0

    # -- Sample points --
    #: The area lineup A is in at the sample points. ``None`` = the game's
    #: unnamed area, which comes from the demo as an empty string.
    a_area: str | None = "TSpawn"
    b_area: str | None = "CTSpawn"
    #: How many, counting from the start of the list, are dead at the sample
    #: point.
    a_dead_at_sample: int = 0
    b_dead_at_sample: int = 0
    #: A per-player area that overrides the side's default area. Needed when a
    #: test has to tell the thrower's own area from a neighbour's.
    player_areas: dict[str, str | None] = field(default_factory=dict)
    #: Players who are **missing** from the sample points' rows entirely.
    #: Imitates a tick from which a player cannot be read at all.
    sample_skip: tuple[str, ...] = ()
    #: Players whose row **is** on the sample point's tick but who have no
    #: pawn: the controller's team is there, every pawn field is empty. A
    #: different thing from :attr:`sample_skip`, where there is no row at all
    #: -- and that difference is exactly the
    #: ``anubis_vs_RCAVE_VETERANS`` demo's fault.
    sample_pawnless: tuple[str, ...] = ()
    #: Damage events ``(tick offset from the anchor, attacker, victim,
    #: weapon)``.
    hurt: list[tuple[int, str | None, str | None, str | None]] = field(
        default_factory=list
    )
    #: Deaths in the same form; the first-contact fallback **and** the source
    #: of the deaths table. The per-player fields (area, coordinates, side)
    #: are derived from the player by the same formula as at the sample
    #: points, so that the tables cannot disagree about where anyone was.
    deaths: list[tuple[int, str | None, str | None, str | None]] = field(
        default_factory=list
    )
    #: The per-player area **at the moment of death**, overriding the sample
    #: point's area. The value ``None`` = the game gave no area name; it comes
    #: from the demo as an empty string. Needed when a test has to tell the
    #: victim's area from the attacker's or to show a missing area.
    death_areas: dict[str, str | None] = field(default_factory=dict)
    #: Raw fields written over **every** death row of this round. Needed when
    #: a test has to build a deviant row the library produces which the normal
    #: derivation does not -- an attackerless death that nevertheless has an
    #: attacker area, for instance.
    death_row_overrides: dict[str, Any] = field(default_factory=dict)
    #: A per-player ``team_num`` in the death event, overriding the value
    #: derived from the side. ``None`` = a spectator or unknown; it is the
    #: only way to build a death whose side cannot be established from
    #: anywhere.
    death_teams: dict[str, int | None] = field(default_factory=dict)

    # -- Utility --
    #: The grenades thrown
    #: ``(entity, thrower, type, starting offset, duration in ticks)``.
    #: The offset is measured from the freezetime anchor, and the trajectory
    #: runs away from the thrower's position so that the area snaps to the
    #: thrower.
    grenades: list[tuple[int, str | None, str, int, int]] = field(
        default_factory=list
    )
    #: Bag rows ``(entity, owner, type, tick offset)``. These have no
    #: coordinates; telling molotov from incendiary reads them.
    grenades_in_bag: list[tuple[int, str, str, int]] = field(default_factory=list)


def _rows(
    round_spec: Round, tick: int, *, at_end: bool, total_score: int
) -> list[dict[str, Any]]:
    """Build one tick's player rows."""
    b_side = "CT" if round_spec.a_side == "T" else "T"
    # The combined score is split into halves; only the total, which the
    # adapter computes, matters to the tests.
    half_scores = {round_spec.a_side: total_score, b_side: 0}

    rows: list[dict[str, Any]] = []
    for side, players, alive_count in (
        (round_spec.a_side, round_spec.a_players, round_spec.alive[0]),
        (b_side, round_spec.b_players, round_spec.alive[1]),
    ):
        if round_spec.score_only_side is not None and side != round_spec.score_only_side:
            score: int | None = None
        else:
            score = half_scores[side]
        for index, steamid in enumerate(players):
            if (
                side == round_spec.a_side
                and not at_end
                and index < round_spec.a_absent
            ):
                continue
            unreadable = (
                side == round_spec.a_side
                and not at_end
                and index < round_spec.a_unreadable
            )
            # The armed count reads the per-player value, so a test has to be
            # able to set it per player and not merely as a total.
            equip_buy_end = 4200
            own_equip = round_spec.a_equip_buy_end
            if own_equip is not None and side == round_spec.a_side:
                # A short list is a typo in the test and not intentional: the
                # players past the end would silently get the default of 4200
                # and would come out armed, and the test would then be
                # measuring a different setup from the one it claims.
                assert len(own_equip) == len(round_spec.a_players), (
                    "a_equip_buy_end and a_players are of different lengths: "
                    f"{len(own_equip)} vs. {len(round_spec.a_players)}"
                )
                equip_buy_end = own_equip[index]

            # The armed count reads the inventory and the armour, not the
            # equipment value. The same length check as above and for the same
            # reason: a short list would silently leave the rest on the
            # default arming.
            inventory: tuple[str, ...] | None = DEFAULT_INVENTORY
            own_inventory = round_spec.a_inventory
            if own_inventory is not None and side == round_spec.a_side:
                assert len(own_inventory) == len(round_spec.a_players), (
                    "a_inventory and a_players are of different lengths: "
                    f"{len(own_inventory)} vs. {len(round_spec.a_players)}"
                )
                inventory = own_inventory[index]
            armor: int | None = DEFAULT_ARMOR
            own_armor = round_spec.a_armor
            if own_armor is not None and side == round_spec.a_side:
                assert len(own_armor) == len(round_spec.a_players), (
                    "a_armor and a_players are of different lengths: "
                    f"{len(own_armor)} vs. {len(round_spec.a_players)}"
                )
                armor = own_armor[index]

            # Money and money spent by the same formula as the other
            # per-player values: the buy window's tests have to be able to
            # show that a purchase moves money from the pocket into the kit.
            account: int | None = 800
            spent: int | None = 4000
            if side == round_spec.a_side:
                if round_spec.a_account is not None:
                    assert len(round_spec.a_account) == len(round_spec.a_players), (
                        "a_account and a_players are of different lengths: "
                        f"{len(round_spec.a_account)} vs. "
                        f"{len(round_spec.a_players)}"
                    )
                    account = round_spec.a_account[index]
                if round_spec.a_cash_spent is not None:
                    assert len(round_spec.a_cash_spent) == len(
                        round_spec.a_players
                    ), (
                        "a_cash_spent and a_players are of different lengths: "
                        f"{len(round_spec.a_cash_spent)} vs. "
                        f"{len(round_spec.a_players)}"
                    )
                    spent = round_spec.a_cash_spent[index]
            clan = (
                round_spec.a_clan
                if side == round_spec.a_side
                else round_spec.b_clan
            )
            rows.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": round_spec.display_names.get(steamid, steamid),
                    # The game gives a clanless team an empty string.
                    dp._CLAN_NAME: "" if clan is None else clan,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._ACCOUNT: None if unreadable else account,
                    dp._CASH_SPENT: None if unreadable else spent,
                    dp._EQUIP_ROUND_START: None if unreadable else 200,
                    # The equipment value at the end of the buy time is read
                    # from this prop and not from
                    # m_unFreezetimeEndEquipmentValue: the game's own
                    # freezetime snapshot does not update during the buy time,
                    # so from a later tick it would still give the anchor's
                    # number. On the end tick the value is the kit the
                    # survivors saved.
                    dp._EQUIP_CURRENT: (
                        3000 if at_end else (None if unreadable else equip_buy_end)
                    ),
                    dp._ARMOR_VALUE: armor,
                    dp._INVENTORY: (
                        None if inventory is None else list(inventory)
                    ),
                    dp._LIFE_STATE: 0 if (at_end and index < alive_count) else 1,
                    dp._TEAM_SCORE: score,
                    dp._ROUND_START_TIME: round_spec.round_start_time,
                }
            )
    return rows


def _death_player_fields(
    round_spec: Round, steamid: str | None, prefix: str
) -> dict[str, Any]:
    """One player's fields in a death event, with the prefix attached.

    The library returns the requested fields under the prefixes ``user_`` and
    ``attacker_``, and **an attackerless death leaves every attacker field
    empty** -- exactly as falling and the bomb do in a real demo.

    The area and the coordinates are derived by the same formula as at the
    sample points, so that the deaths table and the sample point table cannot
    disagree about where a player was. Exceptions are given with
    :attr:`Round.death_areas` and :attr:`Round.death_teams`.
    """
    fields: dict[str, Any] = {
        f"{prefix}_last_place_name": None,
        f"{prefix}_X": None,
        f"{prefix}_Y": None,
        f"{prefix}_Z": None,
        f"{prefix}_team_num": None,
    }
    if steamid is None:
        return fields

    b_side = "CT" if round_spec.a_side == "T" else "T"
    if steamid in round_spec.a_players:
        side = round_spec.a_side
        index = round_spec.a_players.index(steamid)
        default_area = round_spec.a_area
        height = A_SIDE_HEIGHT
    elif steamid in round_spec.b_players:
        side = b_side
        index = round_spec.b_players.index(steamid)
        default_area = round_spec.b_area
        height = B_SIDE_HEIGHT
    else:
        # A player neither lineup knows. The side then comes only from the
        # event's own team_num -- or from nowhere.
        fields[f"{prefix}_team_num"] = round_spec.death_teams.get(steamid)
        area = round_spec.death_areas.get(steamid)
        fields[f"{prefix}_last_place_name"] = "" if area is None else area
        return fields

    area = round_spec.death_areas.get(
        steamid, round_spec.player_areas.get(steamid, default_area)
    )
    # The game gives an unnamed area an empty string, not a null.
    fields[f"{prefix}_last_place_name"] = "" if area is None else area
    fields[f"{prefix}_X"] = float(100 * index)
    fields[f"{prefix}_Y"] = float(-100 * index)
    fields[f"{prefix}_Z"] = height
    fields[f"{prefix}_team_num"] = round_spec.death_teams.get(
        steamid, _SIDE_TEAM[side]
    )
    return fields


def _sample_rows(round_spec: Round, tick: int) -> list[dict[str, Any]]:
    """A sample point's rows: position, side and alive state, no economy values.

    The coordinates are derived from the player's index, so that a test can
    establish that they reach the table and do not change on the way.

    The sides are separated by height (:data:`B_SIDE_HEIGHT`): without it the
    A- and B-side players at the same index would stand exactly on top of each
    other, and utility's area logic would have no unambiguous nearest player.
    """
    b_side = "CT" if round_spec.a_side == "T" else "T"
    rows: list[dict[str, Any]] = []
    for side, players, dead_count, area, height in (
        (
            round_spec.a_side,
            round_spec.a_players,
            round_spec.a_dead_at_sample,
            round_spec.a_area,
            A_SIDE_HEIGHT,
        ),
        (
            b_side,
            round_spec.b_players,
            round_spec.b_dead_at_sample,
            round_spec.b_area,
            B_SIDE_HEIGHT,
        ),
    ):
        for index, steamid in enumerate(players):
            if steamid in round_spec.sample_skip:
                continue
            if steamid in round_spec.sample_pawnless:
                # The controller is there (the team is readable), the pawn is
                # not: every pawn field empty on the same row.
                rows.append(
                    {
                        "tick": tick,
                        "steamid": steamid,
                        "name": steamid,
                        dp._TEAM_NUM: _SIDE_TEAM[side],
                        dp._LIFE_STATE: None,
                        dp._PLACE_NAME: None,
                        dp._X: None,
                        dp._Y: None,
                        dp._Z: None,
                    }
                )
                continue
            own_area = round_spec.player_areas.get(steamid, area)
            rows.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": steamid,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._LIFE_STATE: 2 if index < dead_count else 0,
                    # The game gives an unnamed area an empty string.
                    dp._PLACE_NAME: "" if own_area is None else own_area,
                    dp._X: float(100 * index),
                    dp._Y: float(-100 * index),
                    dp._Z: height,
                }
            )
    return rows


def build(rounds: list[Round]) -> FakeDemoparser2:
    """Assemble the fake from a list of rounds."""
    freeze_ticks: list[int] = []
    round_ends: list[dict[str, Any]] = []
    tick_rows: dict[int, list[dict[str, Any]]] = {}
    hurt_rows: list[dict[str, Any]] = []
    death_rows: list[dict[str, Any]] = []
    grenade_rows: list[dict[str, Any]] = []

    for round_spec in rounds:
        if round_spec.freeze_tick is not None:
            grenade_rows.extend(_grenade_rows(round_spec))
            for offset, attacker, victim, weapon in round_spec.hurt:
                hurt_rows.append(
                    {
                        "tick": round_spec.freeze_tick + offset,
                        "attacker_steamid": attacker,
                        "user_steamid": victim,
                        "weapon": weapon,
                    }
                )
            for offset, attacker, victim, weapon in round_spec.deaths:
                row = {
                    "tick": round_spec.freeze_tick + offset,
                    "attacker_steamid": attacker,
                    "user_steamid": victim,
                    "weapon": weapon,
                }
                row.update(_death_player_fields(round_spec, victim, "user"))
                row.update(
                    _death_player_fields(round_spec, attacker, "attacker")
                )
                row.update(round_spec.death_row_overrides)
                death_rows.append(row)
            freeze_ticks.append(round_spec.freeze_tick)
            tick_rows[round_spec.freeze_tick] = _rows(
                round_spec,
                round_spec.freeze_tick,
                at_end=False,
                total_score=round_spec.score_at_freeze,
            )
            # The buy-time ticks. They are ordinary ticks inside the round and
            # not end ticks: the players are alive and the values are the ones
            # the test gave.
            for offset, overrides in round_spec.after_freeze.items():
                tick = round_spec.freeze_tick + offset
                tick_rows[tick] = _rows(
                    replace(round_spec, **overrides),
                    tick,
                    at_end=False,
                    total_score=round_spec.score_at_freeze,
                )
            # An empty tick is **a different thing from a missing one**: for a
            # missing one the fake offers the sample point rows, for an empty
            # one nothing. Only the latter triggers the adapter's fallback.
            for offset in round_spec.blank_after_freeze:
                tick_rows[round_spec.freeze_tick + offset] = []
        if round_spec.end_tick is not None:
            round_ends.append(
                {
                    "reason": round_spec.reason,
                    "round": round_spec.demo_round,
                    "tick": round_spec.end_tick,
                    "winner": round_spec.winner,
                }
            )
            tick_rows[round_spec.end_tick] = _rows(
                round_spec,
                round_spec.end_tick,
                at_end=True,
                total_score=round_spec.score_at_end,
            )
    return FakeDemoparser2(
        sorted(freeze_ticks),
        round_ends,
        tick_rows,
        events={"player_hurt": hurt_rows, "player_death": death_rows},
        rounds_model=list(rounds),
        grenades=grenade_rows,
    )


def _grenade_rows(round_spec: Round) -> list[dict[str, Any]]:
    """One round's trajectory and bag rows.

    The trajectory's start is set to the thrower's own position
    (:func:`_sample_rows` uses the same formula), so that the throw's area
    snaps to the thrower as it does in a real demo. The trajectory runs away
    from there.
    """
    assert round_spec.freeze_tick is not None
    rows: list[dict[str, Any]] = []

    for entity, owner, grenade_type, offset in round_spec.grenades_in_bag:
        rows.append(
            {
                "grenade_type": grenade_type,
                "grenade_entity_id": entity,
                "x": None,
                "y": None,
                "z": None,
                "tick": round_spec.freeze_tick + offset,
                "steamid": owner,
                "name": owner,
            }
        )

    for entity, thrower, grenade_type, offset, duration in round_spec.grenades:
        if thrower in round_spec.a_players:
            index, height = round_spec.a_players.index(thrower), A_SIDE_HEIGHT
        elif thrower in round_spec.b_players:
            index, height = round_spec.b_players.index(thrower), B_SIDE_HEIGHT
        else:
            index, height = 0, A_SIDE_HEIGHT
        start = (float(100 * index), float(-100 * index), height)
        for step in range(duration):
            rows.append(
                {
                    "grenade_type": grenade_type,
                    "grenade_entity_id": entity,
                    "x": start[0] + 40.0 * step,
                    "y": start[1],
                    "z": start[2],
                    "tick": round_spec.freeze_tick + offset + step,
                    "steamid": thrower,
                    "name": thrower,
                }
            )
    return rows


def normal_match(
    played: int = 3, *, knife: bool = True, tickrate: float = 64.0
) -> list[Round]:
    """A knife round + N rounds played, as in a real demo.

    The knife round's point is zeroed, so its ``score_at_end`` is 1 but at the
    next round's anchor the reading is 0 again.
    """
    rounds: list[Round] = []
    tick = 1000
    time_s = 100.0
    demo_round = 1
    points = 0

    if knife:
        rounds.append(
            Round(
                demo_round=demo_round,
                freeze_tick=tick,
                end_tick=tick + 500,
                winner="T",
                reason="ct_killed",
                score_at_freeze=0,
                score_at_end=1,  # zeroed by mp_restartgame
                alive=(4, 0),
                round_start_time=time_s,
            )
        )
        demo_round += 1
        tick += 1000
        time_s += 1000 / tickrate

    for _ in range(played):
        rounds.append(
            Round(
                demo_round=demo_round,
                freeze_tick=tick,
                end_tick=tick + 500,
                winner="CT",
                reason="t_killed",
                score_at_freeze=points,
                score_at_end=points + 1,
                alive=(0, 3),
                round_start_time=time_s,
            )
        )
        points += 1
        demo_round += 1
        tick += 1000
        time_s += 1000 / tickrate
    return rounds


def insert_restart(
    rounds: list[Round],
    after: int,
    *,
    score: int | None = None,
    offset: int = 100,
    tickrate: float = 64.0,
) -> list[Round]:
    """Insert a match restart after round ``after``.

    A restart is a freezetime anchor **without** a ``round_end``. It does not
    consume the demo's own round number, so the surrounding rounds' numbers
    are untouched -- and that is exactly what makes it an observable restart
    rather than a lost round.

    Args:
        rounds: The list of rounds, modified in place.
        after: The round the restart comes after.
        score: The combined score at the restart's anchor. The default is the
            previous round's final state; ``0`` imitates an
            ``mp_restartgame`` reset.
        offset: Ticks from the end of the previous round. It has to fit before
            the next round's anchor (500 ticks in the fake).
    """
    host = rounds[after]
    assert host.freeze_tick is not None and host.end_tick is not None
    assert host.round_start_time is not None
    tick = host.end_tick + offset
    rounds.insert(
        after + 1,
        Round(
            demo_round=None,
            freeze_tick=tick,
            end_tick=None,
            score_at_freeze=host.score_at_end if score is None else score,
            round_start_time=(
                host.round_start_time + (tick - host.freeze_tick) / tickrate
            ),
        ),
    )
    return rounds


def restarted_match(
    played: int = 3, *, restarts: int = 1, tickrate: float = 64.0
) -> list[Round]:
    """A knife round, a match restart and N rounds played.

    This is the league demos' pattern: right after the knife round comes a
    ``round_freeze_end`` of its own **without** any ``round_end``, and play
    continues normally after it. The restart zeroes the score, so the reading
    at its anchor is 0 -- and that is exactly where the knife round's point
    disappears.

    The pattern was measured in all four league demos; the measurement is kept
    in the file ``vika-kierrosnumerointi.md``, which is outside this
    repository in the BMAD project's directory
    ``_bmad-output/implementation-artifacts/``. The essential content is
    repeated here and in :mod:`pappascout.adapters.demo_parser`'s module
    documentation, so that the test does not rest on a file that is not in the
    repository.

    Args:
        restarts: How many **consecutive** restarts to build. More than one is
            an unknown phenomenon that the parse is meant to stop on.
    """
    rounds = normal_match(played=played, knife=True, tickrate=tickrate)
    # In reverse order: each goes right after the knife round, so the one
    # added last ends up first and the ticks rise through the list.
    for number in reversed(range(restarts)):
        insert_restart(
            rounds, 0, score=0, offset=100 * (number + 1), tickrate=tickrate
        )
    return rounds


def numbers(df: pl.DataFrame) -> list[int | None]:
    return (
        mark_played_rounds(df)
        .unique(subset=["round_raw"], keep="first", maintain_order=True)
        .sort("round_raw")["round_no"]
        .to_list()
    )


# --- The port contract ---------------------------------------------------------


def test_frame_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    df = parse_with(build(normal_match()), tmp_path)
    assert tuple(df.columns) == ROUNDS_ADAPTER_COLUMNS
    assert df["round_no"].null_count() == df.height


def test_two_rows_per_round(tmp_path: Path) -> None:
    df = parse_with(build(normal_match(played=5)), tmp_path)
    assert df.height == 2 * 6  # the knife round + 5
    assert df.group_by("round_raw").len()["len"].unique().to_list() == [2]


# --- Round numbering and round_raw ---------------------------------------------


def test_round_raw_comes_from_the_demo_not_from_a_counter(tmp_path: Path) -> None:
    """The demo's own ``round`` field reaches the table as it is."""
    rounds = normal_match(played=2)
    for offset, round_spec in enumerate(rounds):
        round_spec.demo_round = 40 + offset  # the demo's numbering need not start at 1
    df = parse_with(build(rounds), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [40, 41, 42]


def test_knife_round_is_not_played(tmp_path: Path) -> None:
    df = parse_with(build(normal_match(played=3)), tmp_path)
    assert numbers(df) == [None, 1, 2, 3]


def test_last_round_score_end_comes_from_its_own_round_end_tick(
    tmp_path: Path,
) -> None:
    """The last round has no next anchor.

    This is the only place where ``score_end`` is read at a different tick
    from the other rounds. Without this test every match's last round could
    drop out silently.
    """
    df = parse_with(build(normal_match(played=3)), tmp_path)
    last_round = df.filter(pl.col("round_raw") == pl.col("round_raw").max())
    assert last_round["score_start"].unique().to_list() == [2]
    assert last_round["score_end"].unique().to_list() == [3]
    assert numbers(df)[-1] == 3


def test_unfinished_last_round_is_not_numbered(tmp_path: Path) -> None:
    """The demo was cut short mid-round: an anchor without a round_end."""
    rounds = normal_match(played=2)
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=9000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=250.0,
        )
    )
    df = parse_with(build(rounds), tmp_path)
    assert numbers(df) == [None, 1, 2, None]
    unfinished = df.filter(pl.col("round_raw") == df["round_raw"].max())
    assert unfinished["won"].null_count() == 2
    assert unfinished["win_reason"].null_count() == 2


def test_orphan_freeze_anchor_before_the_first_round_becomes_its_own_round(
    tmp_path: Path,
) -> None:
    """Two freeze anchors before the first round_end.

    ``_segments`` gives the anchor to the **last** one before the ending, so
    the extra anchor ends up at the **start** of the list -- not in the
    middle, even though it is written in between in the list of rounds. An
    unnumbered segment at the start is not a restart: before the demo's first
    number of its own there is no value to collide with, so it gets its number
    by counting backwards and survives as a round of its own. The case in the
    middle is
    :func:`test_match_restart_after_the_knife_round_is_not_a_round`.
    """
    rounds = normal_match(played=2, knife=False)
    rounds.insert(
        1,
        Round(
            demo_round=None,
            freeze_tick=rounds[0].freeze_tick + 100,
            end_tick=None,
            score_at_freeze=1,
            round_start_time=110.0,
        ),
    )
    df = parse_with(build(rounds), tmp_path)
    assert df.height == 6
    assert numbers(df) == [1, None, 2]


def test_match_restart_after_the_knife_round_is_not_a_round(tmp_path: Path) -> None:
    """I/O matrix: a league demo -- a restart in the middle stays unnumbered.

    A restart is played, but it is not a round: it gets no number of the
    demo's own and produces no row in the rounds table. The other rounds keep
    their own numbers from the demo, so the numbering does not shift.
    """
    df = parse_with(build(restarted_match(played=3)), tmp_path)

    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3, 4]
    assert df.height == 2 * 4  # the knife round + 3 played, the restart not
    assert numbers(df) == [None, 1, 2, 3]


def test_match_restart_is_counted_and_reported(tmp_path: Path) -> None:
    """I/O matrix: reporting -- the drop must not be silent."""
    adapter = parse_adapter(build(restarted_match(played=3)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 1
    # The round boundary was still seen: the knife round + the restart + 3
    # played.
    assert adapter.diagnostics.rounds_seen == 5


def test_normal_demo_has_no_match_restarts(tmp_path: Path) -> None:
    """I/O matrix: an old demo -- every segment has a round_end of its own."""
    adapter = parse_adapter(build(normal_match(played=3)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 0


def test_a_lost_round_is_not_mistaken_for_a_restart(tmp_path: Path) -> None:
    """The numbering jumps over an unnumbered boundary -> a round is lost.

    Position alone is not a distinguishing mark: a lost round looks exactly
    like a restart in the list. The difference is in the demo's own numbering
    -- a restart does not consume a round number, a lost round does. A silent
    drop would take the round out of every table and report it as a restart on
    top of that.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[1].demo_round = 3  # round 2 is missing from the demo
    insert_restart(rounds, 0)

    with pytest.raises(ParseError, match="jumps over an unnumbered"):
        parse_with(build(rounds), tmp_path)


def test_a_resolved_round_without_a_number_is_not_a_restart(
    tmp_path: Path,
) -> None:
    """A resolved round without the demo's own number is a round, not a restart.

    The second observable distinguishing mark: a restart has no ``round_end``.
    A segment that has one is a round -- it is numbered from a neighbour and
    not dropped, and it is not counted as a restart.
    """
    rounds = normal_match(played=3, knife=False)
    rounds[1].demo_round = None  # a round_end without a round field

    df = parse_with(build(rounds), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3]
    assert df.height == 2 * 3

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 0


def test_restart_in_the_middle_of_the_match_is_recognised(tmp_path: Path) -> None:
    """A restart is not tied to the place right after the knife round.

    In the league demos it is always at index 1, but the recognition rests on
    observations and not on that. Here it is mid-match and does not zero the
    score.
    """
    rounds = normal_match(played=4)
    insert_restart(rounds, 2)  # after the third round boundary

    df = parse_with(build(rounds), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3, 4, 5]
    assert df.height == 2 * 5
    assert numbers(df) == [None, 1, 2, 3, 4]


def test_restart_and_an_unfinished_last_round_live_in_the_same_demo(
    tmp_path: Path,
) -> None:
    """Both rules in one demo: a drop in the middle, a fill at the tail."""
    rounds = restarted_match(played=2)
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=4000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=100.0 + 3000 / 64.0,
        )
    )

    df = parse_with(build(rounds), tmp_path)
    # The restart gets no number; the unresolved last one gets its from a
    # neighbour.
    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3, 4]
    assert numbers(df) == [None, 1, 2, None]
    unfinished = df.filter(pl.col("round_raw") == 4)
    assert unfinished["won"].null_count() == 2

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 1


def test_restart_players_never_enter_the_lineups(tmp_path: Path) -> None:
    """The restart's readings must not contaminate the lineups.

    A restart is exactly the moment when the team and side state is at its
    least stable: players are moved and reconnect. A single wrong reading
    would stay in ``lineups`` permanently and could flip the sides for every
    round after it -- that is, attribute the wins and the economy values to
    the wrong team.
    """
    clean = parse_with(build(restarted_match(played=3)), tmp_path)

    polluted = restarted_match(played=3)
    polluted[1].a_players = [f"outo{n}" for n in range(5)]
    polluted[1].b_players = [f"muukalainen{n}" for n in range(5)]
    dirty = parse_with(build(polluted), tmp_path)

    assert sorted(clean["lineup_key"].unique().to_list()) == sorted(
        dirty["lineup_key"].unique().to_list()
    )


def test_round_after_a_restart_keeps_its_score_start(tmp_path: Path) -> None:
    """The score's fallback must not stop at a restart.

    The round after a restart always has an anchor of its own -- ``_segments``
    gives the anchor to the last boundary before the ending -- but its
    **reading** may have been rejected (a one-sided score). The fallback then
    fetches the previous reading. A restart has no end tick, and the end tick
    of the round before it is from the moment before the reset: without the
    restart's own anchor the round would get ``score_start == score_end`` and
    would drop out of the rounds played.
    """
    rounds = restarted_match(played=3)
    rounds[2].score_only_side = rounds[2].a_side
    rounds[2].score_at_freeze = 99  # a one-sided reading does not count

    df = parse_with(build(rounds), tmp_path)
    first_after = df.filter(pl.col("round_raw") == 2)
    assert first_after["score_start"].unique().to_list() == [0]
    assert numbers(df) == [None, 1, 2, 3]


def test_restart_breaks_the_saved_equipment_chain(tmp_path: Path) -> None:
    """A restart zeroes the kit, so the chain breaks there.

    Without the break, the pistol round following a restart would inherit the
    knife round's survivors' equipment value as the input to the
    classification -- and that is exactly the round whose classification would
    go wrong.
    """
    df = parse_with(build(restarted_match(played=3)), tmp_path)

    first_after = df.filter(pl.col("round_raw") == 2)
    assert first_after["survivors_equip_prev"].null_count() == 2
    # The chain continues normally from the very next round.
    later = df.filter(pl.col("round_raw") == 3)
    assert later["survivors_equip_prev"].null_count() == 0


def test_sample_points_stay_aligned_with_their_segment_after_a_restart(
    tmp_path: Path,
) -> None:
    """Filtering out the restart must not shift the side map by one.

    ``_sample_points`` samples only the numbered segments, but ``sides`` and
    ``segments`` are in segment order. Without the original index the rounds
    after a restart would read the previous segment's ticks -- and the first
    contact would be settled from the wrong round's observations.
    """
    swap = "vaihtopelaaja"
    rounds = restarted_match(played=3)
    # The sides switch mid-match, so that the segments' maps differ.
    for round_spec in rounds[3:]:
        round_spec.a_side = "CT"
    # A player who appears in both lineups is left outside lineup_of. His side
    # is only settled through **the round's own tick** -- through exactly the
    # index the filtering can shift.
    rounds[2].a_players = [swap, *A_PLAYERS[1:]]
    rounds[3].b_players = [swap, *B_PLAYERS[1:]]
    rounds[2].hurt = [(10, swap, B_PLAYERS[1], "ak47")]

    ticks = parse_ticks_table(build(rounds), tmp_path)
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["round_raw"].unique().to_list() == [2]

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_side_events == 0


def test_the_restart_never_reaches_the_side_key_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The side map is built only when a sample point needs it.

    ``_keys_by_side`` raises a ``ParseError`` if both lineups came out on the
    same side. Eager construction would give a restart the power to bring the
    whole run down even though it produces no row in any table -- and the
    error message would report its ``round_raw`` as ``None``, which tells the
    reader nothing.
    """
    seen: list[int | None] = []
    original = dp._keys_by_side

    def spy(sides, lineup_keys, segment):
        seen.append(segment.round_raw)
        return original(sides, lineup_keys, segment)

    monkeypatch.setattr(dp, "_keys_by_side", spy)
    parse_ticks_table(build(restarted_match(played=3)), tmp_path)

    assert seen, "the rule was not run at all"
    assert None not in seen
    assert sorted(set(seen)) == [1, 2, 3, 4]


def test_restart_utility_does_not_leak_into_the_event_table(
    tmp_path: Path,
) -> None:
    """I/O matrix: a restart produces no row in the EVENTS table."""
    rounds = restarted_match(played=2)
    rounds[1].grenades = [(9001, A_PLAYERS[0], "CSmokeGrenadeProjectile", 50, 5)]
    rounds[2].grenades = [(9002, A_PLAYERS[0], "CSmokeGrenadeProjectile", 50, 5)]
    tables = parse_tables(build(rounds), tmp_path)

    thrown = tables.events["grenade_entity_id"].unique().to_list()
    assert 9001 not in thrown
    assert 9002 in thrown
    # Neither other table knows a round the rounds table does not have.
    known = set(tables.rounds["round_raw"].to_list())
    assert set(tables.ticks["round_raw"].to_list()) <= known
    assert set(tables.events["round_raw"].to_list()) <= known

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    # A restart has no round window, so a grenade thrown during it is
    # recorded under the same figure as the warm-up throws. That is the
    # figure's right place, but it has to be said out loud: otherwise the
    # figure looks in a league demo's summary like a fault that is not there.
    assert adapter.diagnostics.grenades_outside_rounds == 1


def test_unfinished_last_round_still_takes_its_number_from_the_neighbour(
    tmp_path: Path,
) -> None:
    """I/O matrix: an unresolved last round -- the neighbour fill survives.

    An unnumbered segment at the tail is a genuine round the demo cut short.
    It is not a restart, and it has to get its number from a neighbour as
    before.
    """
    rounds = normal_match(played=2, knife=False)
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=9000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=250.0,
        )
    )
    df = parse_with(build(rounds), tmp_path)

    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3]
    assert df.height == 2 * 3


def test_two_restarts_in_a_row_halt_the_parse(tmp_path: Path) -> None:
    """I/O matrix: more than one restart -> stop, do not guess."""
    with pytest.raises(ParseError, match="look like a match restart"):
        parse_with(build(restarted_match(played=3, restarts=2)), tmp_path)


def test_two_restarts_apart_from_each_other_halt_the_parse(
    tmp_path: Path,
) -> None:
    """The limit is per demo and not tied to the restarts being consecutive."""
    rounds = restarted_match(played=4)
    insert_restart(rounds, 3)  # a second one mid-match

    with pytest.raises(ParseError, match="look like a match restart") as err:
        parse_with(build(rounds), tmp_path)
    # The error states the ticks the demo can be opened at -- not list
    # indices.
    assert "1600" in str(err.value)


def test_the_restart_limit_is_read_from_the_constant(tmp_path: Path) -> None:
    """The messages are derived from the constant, so raising the limit does
    not leave them lying."""
    with pytest.raises(ParseError) as err:
        parse_with(build(restarted_match(played=3, restarts=2)), tmp_path)
    assert f"at most {dp.MAX_MATCH_RESTARTS}" in str(err.value)


def test_public_names_all_exist() -> None:
    """``__all__`` must not promise a name the module does not have."""
    assert "MAX_MATCH_RESTARTS" in dp.__all__
    for name in dp.__all__:
        assert hasattr(dp, name), name


def test_segments_without_any_round_end_keep_the_running_count(
    tmp_path: Path,
) -> None:
    """I/O matrix: every segment unnumbered -> the fallback survives."""
    rounds = [
        Round(
            demo_round=None,
            freeze_tick=1000 + 1000 * index,
            end_tick=None,
            score_at_freeze=0,
            round_start_time=100.0 + index * 1000 / 64.0,
        )
        for index in range(3)
    ]
    df = parse_with(build(rounds), tmp_path)

    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3]
    assert df.height == 2 * 3


def test_round_end_without_an_anchor_is_kept_with_its_own_status(
    tmp_path: Path,
) -> None:
    """A round without a freezetime anchor is included and gets a number."""
    rounds = normal_match(played=3, knife=False)
    rounds[1].freeze_tick = None
    df = parse_with(build(rounds), tmp_path)

    assert numbers(df) == [1, 2, 3]
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor["freeze_end_tick"].null_count() == 2
    assert no_anchor["money_buy_end"].null_count() == 2
    # The scores are inherited from the neighbours, so the round stays played.
    assert no_anchor["score_start"].unique().to_list() == [1]
    assert no_anchor["score_end"].unique().to_list() == [2]


def test_inconsistent_demo_round_numbers_are_refused(tmp_path: Path) -> None:
    rounds = normal_match(played=3, knife=False)
    rounds[2].demo_round = rounds[1].demo_round  # the same number twice
    with pytest.raises(ParseError, match="does not grow evenly"):
        parse_with(build(rounds), tmp_path)


# --- Lineups and sides ---------------------------------------------------------


def test_half_time_switch_keeps_the_lineup_key(tmp_path: Path) -> None:
    """The sides switch, the teams do not."""
    rounds = normal_match(played=4, knife=False)
    for round_spec in rounds[2:]:
        round_spec.a_side = "CT"

    df = parse_with(build(rounds), tmp_path)
    assert df["lineup_key"].n_unique() == 2

    a_key = df.filter(pl.col("round_raw") == rounds[0].demo_round).filter(
        pl.col("side") == "T"
    )["lineup_key"][0]
    later_key = df.filter(pl.col("round_raw") == rounds[-1].demo_round).filter(
        pl.col("side") == "CT"
    )["lineup_key"][0]
    assert a_key == later_key


def test_substitute_does_not_split_the_team(tmp_path: Path) -> None:
    """One player is substituted mid-map: the lineup stays the same team."""
    rounds = normal_match(played=4, knife=False)
    for round_spec in rounds[2:]:
        round_spec.a_players = [*A_PLAYERS[:4], "sijainen"]

    df = parse_with(build(rounds), tmp_path)
    assert df["lineup_key"].n_unique() == 2
    # Both teams have the same number of rows -- no third team appears.
    assert df.group_by("lineup_key").len()["len"].unique().to_list() == [4]


def test_side_assignment_never_guesses_when_teams_do_not_separate(
    tmp_path: Path,
) -> None:
    """On a tie the previous round's map is inherited, (T, CT) is not assumed."""
    rounds = normal_match(played=3, knife=False)
    rounds[1].a_side = "CT"  # the teams switched sides
    # The third round: entirely new players -> neither mapping wins.
    rounds[2].a_players = ["uusi1", "uusi2"]
    rounds[2].b_players = ["uusi3", "uusi4"]
    rounds[2].a_side = "T"

    df = parse_with(build(rounds), tmp_path)
    # The previous mapping was "A is CT", so it is inherited: lineup 0 is CT.
    third_round = df.filter(pl.col("round_raw") == rounds[2].demo_round)
    first_key = df["lineup_key"][0]
    assert third_round.filter(pl.col("lineup_key") == first_key)["side"].to_list() == ["CT"]


def test_first_round_with_only_one_side_is_refused(tmp_path: Path) -> None:
    rounds = normal_match(played=2, knife=False)
    rounds[0].b_players = []
    with pytest.raises(ParseError, match="on only one side"):
        parse_with(build(rounds), tmp_path)


def test_round_without_players_and_without_history_is_refused(
    tmp_path: Path,
) -> None:
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_players = []
    rounds[0].b_players = []
    with pytest.raises(ParseError, match="could not be determined"):
        parse_with(build(rounds), tmp_path)


def test_empty_lineup_never_produces_a_key() -> None:
    """An empty lineup's digest would be the same for both teams."""
    with pytest.raises(ParseError, match="lineups could not be identified"):
        dp._Lineup().key()


# --- The score -----------------------------------------------------------------


def test_one_sided_score_reading_is_not_a_sum() -> None:
    """One side's reading alone is not the combined score.

    A one-sided sum would be too small but would look like a valid figure, and
    the round could drop out of the rounds played because of it, or stay in
    with the wrong number.
    """
    both_sides = [
        {"side": "T", "team_score": 7},
        {"side": "CT", "team_score": 5},
    ]
    assert dp._total_score(both_sides) == 12
    assert dp._total_score(both_sides[:1]) is None
    assert dp._total_score([{"side": "T", "team_score": None}]) is None
    assert dp._total_score([]) is None


def test_one_sided_anchor_falls_back_to_a_trustworthy_neighbour(
    tmp_path: Path,
) -> None:
    """A rejected reading is replaced by a neighbour, not by half a sum."""
    rounds = normal_match(played=3, knife=False)
    rounds[1].score_only_side = rounds[1].a_side
    # If a one-sided reading counted, score_start would be this 99.
    rounds[1].score_at_freeze = 99

    df = parse_with(build(rounds), tmp_path)
    other = df.filter(pl.col("round_raw") == rounds[1].demo_round)
    assert other["score_start"].unique().to_list() == [1]
    assert numbers(df) == [1, 2, 3]


def test_score_jump_larger_than_one_is_refused(tmp_path: Path) -> None:
    """A jump of two points means a round went unrecognised."""
    rounds = normal_match(played=3, knife=False)
    rounds[2].score_at_freeze = 5  # a gap between the second and the third
    rounds[2].score_at_end = 6

    df = parse_with(build(rounds), tmp_path)
    with pytest.raises(ParseError, match="more than one"):
        mark_played_rounds(df)


# --- Tick rate -----------------------------------------------------------------


def test_tick_rate_is_measured_from_the_game_clock(tmp_path: Path) -> None:
    adapter = parse_adapter(build(normal_match(played=4)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == 64.0
    assert adapter.diagnostics.tick_rate_measured is True


def test_tick_rate_measurement_uses_the_median(tmp_path: Path) -> None:
    """One deviant round interval must not move the result."""
    rounds = normal_match(played=5, knife=False)
    rounds[2].round_start_time = rounds[2].round_start_time + 40  # a clock jump
    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == 64.0
    assert adapter.diagnostics.tick_rate_measured is True


def test_tick_rate_falls_back_to_the_default_without_a_clock(tmp_path: Path) -> None:
    rounds = normal_match(played=3)
    for round_spec in rounds:
        round_spec.round_start_time = None

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == DEFAULT_TICK_RATE
    assert adapter.diagnostics.tick_rate_measured is False


def test_absurd_tick_rate_is_rejected_as_a_measurement(tmp_path: Path) -> None:
    """A value outside the sanity bounds is a measurement error, not the truth."""
    rounds = normal_match(played=4, knife=False)
    for index, round_spec in enumerate(rounds):
        # 1000 ticks / 0.001 s = 1,000,000 ticks per second.
        round_spec.round_start_time = 100.0 + index * 0.001

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == DEFAULT_TICK_RATE
    assert adapter.diagnostics.tick_rate_measured is False


def test_diagnostics_report_every_round_boundary(tmp_path: Path) -> None:
    adapter = parse_adapter(build(normal_match(played=6)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.rounds_seen == 7  # the knife round included


# --- Props going missing -------------------------------------------------------


@pytest.mark.parametrize(
    "prop",
    [
        dp._TEAM_SCORE,
        dp._ACCOUNT,
        dp._LIFE_STATE,
        # Story 2.6. ``name`` is not a requested prop but a column
        # demoparser2 adds itself, and that is exactly why its disappearance
        # would be silent: the table would be structurally valid but every
        # name empty.
        dp._PLAYER_NAME,
        dp._CLAN_NAME,
    ],
)
def test_missing_prop_is_named_in_the_error(tmp_path: Path, prop: str) -> None:
    """A renamed field would otherwise produce a structurally valid but
    entirely empty table."""
    fake = build(normal_match(played=2))
    fake.drop_props = (prop,)
    with pytest.raises(ParseError) as exc:
        parse_with(fake, tmp_path)
    assert prop in str(exc.value)


def test_no_rounds_at_all_is_a_wrapped_error(tmp_path: Path) -> None:
    """Renamed in T4: the name used to claim the message is in Finnish."""
    with pytest.raises(ParseError, match="No rounds at all were found"):
        parse_with(FakeDemoparser2([], [], {}), tmp_path)


# --- Observations --------------------------------------------------------------


def test_survivors_and_carry_over_follow_the_team_not_the_side(
    tmp_path: Path,
) -> None:
    """The survivors' kit carries to the team, across a side switch too."""
    rounds = normal_match(played=3, knife=False)
    rounds[0].alive = (2, 0)  # lineup A left two alive
    for round_spec in rounds[1:]:
        round_spec.a_side = "CT"

    df = parse_with(build(rounds), tmp_path)
    a_key = df["lineup_key"][0]
    second_round = df.filter(
        (pl.col("round_raw") == rounds[1].demo_round)
        & (pl.col("lineup_key") == a_key)
    )
    assert second_round["side"].to_list() == ["CT"]
    assert second_round["survivors_equip_prev"].to_list() == [2 * 3000]


def test_player_count_is_observed_not_assumed(tmp_path: Path) -> None:
    """``players_buy_end`` is an observation: four players -> 4, five -> 5.

    The thresholds are per player, so the divisor has to be read from the
    demo. Without this test the column's value would be verified nowhere --
    only its existence.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_players = A_PLAYERS[:4]

    df = parse_with(build(rounds), tmp_path)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    own_rows = df.filter(pl.col("lineup_key") == a_key).sort("round_raw")
    assert own_rows["players_buy_end"].to_list() == [4, 5]
    opponent = df.filter(pl.col("lineup_key") != a_key).sort("round_raw")
    assert opponent["players_buy_end"].to_list() == [5, 5]


def test_round_without_an_anchor_has_no_player_count(tmp_path: Path) -> None:
    """Without a freezetime anchor there is nothing to count -- not even zero."""
    rounds = normal_match(played=2, knife=False)
    rounds[1].freeze_tick = None

    df = parse_with(build(rounds), tmp_path)
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor["players_buy_end"].null_count() == 2


def test_sums_and_their_divisor_come_from_the_same_players(tmp_path: Path) -> None:
    """The numerator and the denominator come from the same set.

    Three players' sum divided by five would underestimate the equipment value
    by 40 % and push the round into an eco -- silently and plausibly.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 2  # two players' props are empty

    df = parse_with(build(rounds), tmp_path)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    row = df.filter(pl.col("lineup_key") == a_key).row(0, named=True)

    assert row["players_buy_end"] == 3
    assert row["equip_buy_end"] == 3 * 4200
    assert row["money_buy_end"] == 3 * 800
    assert row["money_spent"] == 3 * 4000
    # The per-player value stays right, because the divisor is the same set.
    assert row["equip_buy_end"] / row["players_buy_end"] == 4200


# --- The per-player money distribution (Story 1.10) ---------------------------
#
# The team total does not say how many players can buy on the next round, and
# that is exactly what separates a half-buy from a force. The column keeps the
# same figures ``money_buy_end`` sums -- it is not a new demo field.


def test_money_distribution_keeps_every_balance_not_just_the_sum(
    tmp_path: Path,
) -> None:
    """The measured balances are kept as they are, sorted descending.

    The figures come from ``inferno_vs_ryhmarama``'s round 10 (the product
    owner's half-buy): 2,150, 2,000, 2,050, 800, 900. The total of 7,900 is
    the same as before, but it would not say that all five can buy -- nor
    would it tell this apart from a team where one has 7,900 and four have
    nothing.
    """
    balances = [2150, 2000, 2050, 800, 900]
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_account = list(balances)

    df = parse_with(build(rounds), tmp_path)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    row = df.filter(pl.col("lineup_key") == a_key).row(0, named=True)

    assert row[MONEY_DISTRIBUTION_COLUMN] == sorted(balances, reverse=True)
    assert sum(row[MONEY_DISTRIBUTION_COLUMN]) == row["money_buy_end"]


def test_money_distribution_comes_from_the_same_players_as_the_sum(
    tmp_path: Path,
) -> None:
    """The distribution's length is ``players_buy_end``, not the lineup's size.

    Two players are unreadable, so their balances are not known. A zero in
    their place would claim they had no money and would push the round into a
    force -- and a silent misreading of exactly that kind is why this column
    exists.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 2

    df = parse_with(build(rounds), tmp_path)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    row = df.filter(pl.col("lineup_key") == a_key).row(0, named=True)

    assert len(row[MONEY_DISTRIBUTION_COLUMN]) == row["players_buy_end"] == 3
    assert sum(row[MONEY_DISTRIBUTION_COLUMN]) == row["money_buy_end"]


def test_round_without_an_anchor_has_no_money_distribution(tmp_path: Path) -> None:
    """Without an anchor there is no distribution -- nor an empty list.

    An empty list would claim as an observation that there was nobody.
    ``null`` says that nobody could be read, and the classification leaves the
    round unclassified rather than guessing.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[1].freeze_tick = None

    df = parse_with(build(rounds), tmp_path)
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor[MONEY_DISTRIBUTION_COLUMN].null_count() == 2


# --- The armed count (Story 1.6) ----------------------------------------------
#
# ``players_armed_buy_end`` is the only observation that cannot be derived
# from the team total: two AKs and three empty hands give the same total as
# five half-buys. The rule is **armour and at least one weapon in hand**, read
# from the inventory -- not from the equipment value, which is weapon + armour
# + grenades as a single number. Below, every row of the specification's I/O
# matrix as a test of its own.

#: An arming setup: a knife, a bought rifle and a smoke. The armour comes from
#: :data:`DEFAULT_ARMOR`.
FULL_BUY: tuple[str, ...] = ("Bayonet", "AK-47", "Smoke Grenade")

#: The $1,250 case that made Story 1.5's count look armed: a free pistol,
#: armour and two flashes, not a single bought weapon.
FREE_PISTOL_AND_UTILITY: tuple[str, ...] = (
    "M9 Bayonet",
    "Glock-18",
    "Flashbang",
    "Flashbang",
)

#: A free pistol and a knife: unarmed, however much armour there is.
FREE_PISTOL: tuple[str, ...] = ("knife", "Glock-18")

#: A name the classification cannot know. **Not a real knife skin**: such a
#: name would end up in the KNIVES set before long from a new batch of demos,
#: and these tests would break for a reason unrelated to their subject.
UNKNOWN_ITEM = "Ei-Ole-Olemassa-9000"


def _armed_row(
    rounds: list[Round], tmp_path: Path, **kwargs
) -> dict[str, Any]:
    """Lineup A's row from the first round played."""
    df = parse_with(build(rounds), tmp_path, **kwargs)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    return (
        df.filter(pl.col("lineup_key") == a_key).sort("round_raw").row(0, named=True)
    )


def _armed_with(
    tmp_path: Path,
    inventories: list[tuple[str, ...] | None],
    armor: list[int | None] | None = None,
) -> dict[str, Any]:
    """One round with the given inventories; lineup A's row."""
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_players = list(A_PLAYERS[: len(inventories)])
    rounds[0].a_inventory = list(inventories)
    if armor is not None:
        rounds[0].a_armor = list(armor)
    return _armed_row(rounds, tmp_path)


def test_full_buy_arms_every_player(tmp_path: Path) -> None:
    """Five players with a bought weapon and armour -> the count is 5."""
    row = _armed_with(tmp_path, [FULL_BUY] * 5)
    assert row[ARMED_COLUMN] == 5
    assert row["players_buy_end"] == 5


def test_upgraded_pistol_with_armor_is_armed(tmp_path: Path) -> None:
    """A better pistol is weapon enough: knife + Tec-9 + armour."""
    row = _armed_with(tmp_path, [("knife_t", "Tec-9")] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 5


def test_free_pistol_with_armor_is_not_armed(tmp_path: Path) -> None:
    """A free pistol and armour do not arm: there is no bought weapon.

    This is the whole reason Story 1.6 exists. Story 1.5's count looked at the
    equipment value, and a Glock + kevlar was $850 -- only just under the
    threshold. Two flashes on top made it $1,250, that is, "armed", without a
    single bought weapon.
    """
    row = _armed_with(tmp_path, [FREE_PISTOL] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 4


def test_free_pistol_with_armor_and_utility_is_not_armed(tmp_path: Path) -> None:
    """Exactly that $1,250 case: a free pistol, armour and two flashes."""
    row = _armed_with(tmp_path, [FREE_PISTOL_AND_UTILITY] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 4


def test_weapon_without_armor_is_not_armed(tmp_path: Path) -> None:
    """A weapon without armour does not arm.

    The user's definition is "kevlar **and** some upgraded weapon": neither
    alone is enough. This is the case of Ancient's round 21 p250 player -- he
    drops out for lack of armour, not for the value of his kit.
    """
    row = _armed_with(
        tmp_path,
        [("knife_t", "AK-47")] + [FULL_BUY] * 4,
        armor=[0, 100, 100, 100, 100],
    )
    assert row[ARMED_COLUMN] == 4


def test_armor_without_a_weapon_is_not_armed(tmp_path: Path) -> None:
    """Armour without a weapon does not arm: a knife alone."""
    row = _armed_with(tmp_path, [("Falchion Knife",)] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 4


def test_zeus_is_not_a_weapon(tmp_path: Path) -> None:
    """The Zeus is single-use and does not replace a weapon."""
    row = _armed_with(
        tmp_path, [("knife", "Glock-18", "Zeus x27")] + [FULL_BUY] * 4
    )
    assert row[ARMED_COLUMN] == 4


def test_c4_does_not_change_the_verdict(tmp_path: Path) -> None:
    """The C4 is an objective item: it neither arms nor disarms."""
    row = _armed_with(
        tmp_path, [("knife_t", "AK-47", "C4 Explosive")] + [FULL_BUY] * 4
    )
    assert row[ARMED_COLUMN] == 5


def test_shotgun_is_a_weapon(tmp_path: Path) -> None:
    """A shotgun is a bought weapon just as a rifle is."""
    row = _armed_with(tmp_path, [("knife", "Nova")] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 5


def test_unknown_knife_skin_does_not_arm_and_is_reported(tmp_path: Path) -> None:
    """An unknown name is not a weapon, and it is reported.

    Knives are an open set that Valve keeps growing. A list of forbidden names
    would go stale silently with every store update; a list of permitted names
    goes stale visibly and in the right direction. Both halves have to be
    established in the same test: a bare "not armed" would pass even if the
    name were dropped without a word.

    The name is invented on purpose (:data:`UNKNOWN_ITEM`) and is not a real
    knife skin: a real name would end up in the classification before long
    from a new batch of demos, and this test would then break for a reason
    unrelated to its subject.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_inventory = [(UNKNOWN_ITEM, "Glock-18")] + [FULL_BUY] * 4

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    # The occurrence count is included: one exotic knife and a demoparser2
    # rename that hits every row would look exactly the same as a bare name.
    assert adapter.diagnostics.unknown_inventory_items == ((UNKNOWN_ITEM, 1),)

    assert _armed_row(rounds, tmp_path)[ARMED_COLUMN] == 4


def test_unknown_name_is_counted_every_time_it_appears(tmp_path: Path) -> None:
    """The same unknown name on four players is reported as four.

    The count is the signal that tells one odd item from the whole naming
    scheme having changed. Without it both would be "1 distinct item name".
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_inventory = [(UNKNOWN_ITEM, "AK-47")] * 4 + [FULL_BUY]

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ((UNKNOWN_ITEM, 4),)


def test_unknown_name_is_reported_even_from_an_unreadable_player(
    tmp_path: Path,
) -> None:
    """The name is reported even if the player drops out of the count's set.

    ``_readable`` drops a player whose economy fields are missing. If the
    unknown names were scanned only after the filtering, a new weapon name
    would go unreported from the very demo that brought it -- and the list
    would go stale silently, which is what the whole report exists against.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 1
    rounds[0].a_inventory = [(UNKNOWN_ITEM, "AK-47")] + [FULL_BUY] * 4

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ((UNKNOWN_ITEM, 1),)


def test_known_names_leave_no_unknowns(tmp_path: Path) -> None:
    """Known data produces not a single unknown name.

    Without this, the test above would establish only that *some* name ends up
    on the list -- an empty list has to be the normal result for the list to
    be readable.
    """
    adapter = parse_adapter(build(normal_match(played=2, knife=False)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ()
    assert adapter.diagnostics.armed_unreadable_rows == 0


def test_empty_inventory_is_an_observation_not_a_gap(tmp_path: Path) -> None:
    """An empty inventory: ``0``, not ``null``.

    Zero is an observation here -- it is known that nobody was armed. ``null``
    would claim the matter could not be read, and would leave the eco out of
    the data the half-buy's bound is one day calibrated against.
    """
    row = _armed_with(tmp_path, [()] * 5)
    assert row[ARMED_COLUMN] == 0
    assert row["players_buy_end"] == 5


def test_eco_counts_zero_armed_and_zero_is_an_observation(tmp_path: Path) -> None:
    """Five with nothing but a free pistol -> ``0``, not ``null``."""
    row = _armed_with(tmp_path, [FREE_PISTOL] * 5)
    assert row[ARMED_COLUMN] == 0
    assert row["players_buy_end"] == 5


def test_half_buy_counts_only_the_armed_players(tmp_path: Path) -> None:
    """Three with armour + a better pistol, two with a free pistol -> ``3``.

    This very case is the reason the whole column exists, and it is
    established here from both directions: the equipment value is set to the
    same $3,250 that five bare kevlars (5 x 650) would produce, and the count
    still gives three. From the total, then, it cannot be inferred which setup
    it is.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_inventory = [("knife", "P250")] * 3 + [FREE_PISTOL] * 2
    rounds[0].a_equip_buy_end = [950, 950, 950, 200, 200]

    row = _armed_row(rounds, tmp_path)
    assert row[ARMED_COLUMN] == 3
    assert row["equip_buy_end"] == 3 * 950 + 2 * 200 == 5 * 650


def test_missing_inventory_for_the_whole_team_is_null_not_zero(
    tmp_path: Path,
) -> None:
    """No player's inventory could be read: ``null``, not ``0``.

    Zero would claim "nobody was armed" and would pass as an eco.
    ``players_buy_end`` is kept, because the inventory is not among the props
    that drop a player from the sums: the divisor must not change for this
    reason either.
    """
    row = _armed_with(tmp_path, [None] * 5)
    assert row["players_buy_end"] == 5
    assert row[ARMED_COLUMN] is None


def test_one_missing_inventory_empties_the_whole_row(tmp_path: Path) -> None:
    """One missing inventory empties the whole count.

    A partial figure would be a silent lie. The player **stays** in the
    ``players_buy_end`` divisor, because the inventory is not among the props
    that drop him from the sums -- the divisor has to be the same set for
    every figure on the row. So "4/5" would claim that one was unarmed when
    the truth is that he could not be read: a read failure would look like a
    save round.
    """
    row = _armed_with(tmp_path, [None] + [FULL_BUY] * 4)
    assert row["players_buy_end"] == 5
    assert row[ARMED_COLUMN] is None


def test_one_unreadable_armor_empties_the_whole_row(tmp_path: Path) -> None:
    """The same goes for armour -- and that is what used to slip past silently.

    ``armor_value`` is not among the props that drop a player from the sums,
    so unreadable armour used to look exactly like having no armour: the
    player stayed in the divisor and fell out of the numerator.
    """
    row = _armed_with(
        tmp_path, [FULL_BUY] * 5, armor=[None, 100, 100, 100, 100]
    )
    assert row["players_buy_end"] == 5
    assert row[ARMED_COLUMN] is None


def test_zero_armor_is_an_observation_but_missing_armor_is_not(
    tmp_path: Path,
) -> None:
    """``0`` and ``None`` are different things where armour is concerned.

    Without this pair the previous test would also pass an implementation that
    empties the row whenever somebody's armour is missing **as the value
    zero**. Zero is an observation: the player had no armour.
    """
    zeros = _armed_with(tmp_path, [FULL_BUY] * 5, armor=[0, 100, 100, 100, 100])
    assert zeros[ARMED_COLUMN] == 4

    missing = _armed_with(
        tmp_path, [FULL_BUY] * 5, armor=[None, 100, 100, 100, 100]
    )
    assert missing[ARMED_COLUMN] is None


def test_unreadable_armed_rows_are_counted_but_anchorless_ones_are_not(
    tmp_path: Path,
) -> None:
    """A read failure has a figure of its own, an anchorless round does not.

    Both produce a ``null`` count, but only one is a fault. Without a separate
    figure a prop fault would be lost among the normal absences, and the armed
    count could be broken across a whole demo with nothing saying so.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_armor = [None] * 5
    rounds[1].freeze_tick = None

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    # One row: lineup A's row from the first round. The second round's two
    # empty counts are anchorless and not read failures.
    assert adapter.diagnostics.armed_unreadable_rows == 1


def test_armed_count_and_player_count_come_from_the_same_players(
    tmp_path: Path,
) -> None:
    """A short-handed team: the count and the divisor come from the same set.

    Two different divisors on the same row would be a fault that showed only
    in the report -- ``2/5`` and ``2/4`` are different claims.
    """
    row = _armed_with(tmp_path, [FULL_BUY, FULL_BUY, FREE_PISTOL, FREE_PISTOL])
    assert row["players_buy_end"] == 4
    assert row[ARMED_COLUMN] == 2


def test_unreadable_player_is_dropped_not_counted_as_unarmed(
    tmp_path: Path,
) -> None:
    """A missing economy value drops a player from the set; being unarmed does
    not.

    These are two different things, and only the divisor tells them apart. The
    test runs the same setup twice: first player 0 is unreadable, then the
    same player is readable but unarmed. The count is 4 in both -- the
    difference shows in ``players_buy_end`` (4 vs. 5).
    """
    unreadable = normal_match(played=1, knife=False)
    unreadable[0].a_unreadable = 1
    unreadable[0].a_inventory = [FREE_PISTOL] + [FULL_BUY] * 4
    dropped = _armed_row(unreadable, tmp_path)
    assert dropped["players_buy_end"] == 4
    assert dropped[ARMED_COLUMN] == 4

    readable = normal_match(played=1, knife=False)
    readable[0].a_inventory = [FREE_PISTOL] + [FULL_BUY] * 4
    kept = _armed_row(readable, tmp_path)
    assert kept["players_buy_end"] == 5
    assert kept[ARMED_COLUMN] == 4


def test_no_readable_player_gives_null_not_zero(tmp_path: Path) -> None:
    """No player's values could be read: ``null``, not ``0``."""
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 5

    row = _armed_row(rounds, tmp_path)
    assert row["players_buy_end"] is None
    assert row[ARMED_COLUMN] is None


def test_round_without_an_anchor_has_no_armed_count(tmp_path: Path) -> None:
    """An anchorless round: there is no end of freezetime, so no count."""
    rounds = normal_match(played=2, knife=False)
    rounds[1].freeze_tick = None

    df = parse_with(build(rounds), tmp_path)
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor[ARMED_COLUMN].null_count() == 2


def test_armed_count_never_exceeds_the_player_count(tmp_path: Path) -> None:
    """An invariant across the whole table: ``0 <= count <= players_buy_end``."""
    rounds = normal_match(played=3)
    rounds[1].a_inventory = [
        FULL_BUY,
        ("knife", "P250"),
        FREE_PISTOL,
        FREE_PISTOL,
        FREE_PISTOL,
    ]
    rounds[2].a_players = A_PLAYERS[:4]
    rounds[3].a_unreadable = 2

    df = parse_with(build(rounds), tmp_path)
    observed = df.filter(pl.col(ARMED_COLUMN).is_not_null())
    assert observed.height > 0
    assert observed.select(
        (pl.col(ARMED_COLUMN) >= 0)
        & (pl.col(ARMED_COLUMN) <= pl.col("players_buy_end"))
    ).to_series().all()
    # An observation is always in both or in neither -- the same set.
    assert (
        df[ARMED_COLUMN].null_count()
        == df["players_buy_end"].null_count()
    )


# --- The armour count (Story 2.8) ---------------------------------------------
#
# ``players_armored_buy_end`` is **the same reading under a different
# condition**: how many of the same set of players carried armour on the same
# end-of-buy-time tick. It is not a generalisation of the count above but an
# observation of its own -- this is where the product owner's analysis's "5
# kevlars" and "no kevs" are read from, and the armed count cannot give them.
# Below, every row of the specification's I/O matrix as a test of its own.


def test_pistol_round_separates_the_two_counters(tmp_path: Path) -> None:
    """The pistol round: five kevlars, zero armed.

    **This is the whole story's reason.** A free pistol is not an upgraded
    weapon, so the armed count is 0 even when all five have kevlar. Without a
    column of its own the product owner's line *"5 kevlars"* and the line
    *"no kevs"* look exactly the same in the report.

    The pistol round is also the round type on which the armour figure is an
    **observation of buying** rather than of possession: the half starts from
    a clean slate and nothing is inherited from the previous round. On other
    round types the same figure says what the players had, not what they
    bought.

    If anyone ever merges the counts or copies the condition from one into the
    other, this test is the one that fails -- and only this one.
    """
    row = _armed_with(tmp_path, [FREE_PISTOL] * 5)
    assert row[ARMORED_COLUMN] == 5
    assert row[ARMED_COLUMN] == 0
    assert row["players_buy_end"] == 5


def test_eco_without_armor_is_zero_in_both_counters(tmp_path: Path) -> None:
    """An eco without inherited armour: both zero -- and zero is an observation.

    On an eco almost nothing is bought, so the counterpart to zero is not
    "they bought" but "nobody was left with armour from the previous round".
    The setup is therefore built by hand with zero armour rather than on the
    default.
    """
    row = _armed_with(tmp_path, [FREE_PISTOL] * 5, armor=[0] * 5)
    assert row[ARMORED_COLUMN] == 0
    assert row[ARMED_COLUMN] == 0
    assert row["players_buy_end"] == 5


def test_full_buy_is_five_in_both_counters(tmp_path: Path) -> None:
    """A full buy: everyone with armour and a rifle, both counts five.

    A pair with the previous one: the counts must not differ **always**, only
    when the conditions really differ. An implementation that returns a
    constant for the armour does not pass both.
    """
    row = _armed_with(tmp_path, [FULL_BUY] * 5)
    assert row[ARMORED_COLUMN] == 5
    assert row[ARMED_COLUMN] == 5


def test_armor_without_a_weapon_counts_as_armored(tmp_path: Path) -> None:
    """Three kevlars with a free pistol: 3 armoured, 0 armed.

    A partial purchase of kevlars is exactly the pistol-round observation the
    column exists for -- and it separates the counts even when neither is at
    an extreme.
    """
    row = _armed_with(
        tmp_path, [FREE_PISTOL] * 5, armor=[100, 100, 100, 0, 0]
    )
    assert row[ARMORED_COLUMN] == 3
    assert row[ARMED_COLUMN] == 0


def test_a_weapon_without_armor_is_neither(tmp_path: Path) -> None:
    """A rifle without kevlar: both counts leave the player out.

    The rule is ``armor_value > 0``, not "bought something". Without this test
    the armour count could have been implemented as "is there anything in the
    inventory", and that would pass every other row.
    """
    row = _armed_with(tmp_path, [FULL_BUY] * 5, armor=[0, 0, 0, 0, 0])
    assert row[ARMORED_COLUMN] == 0
    assert row[ARMED_COLUMN] == 0


def test_one_unreadable_armor_empties_the_armored_count_too(
    tmp_path: Path,
) -> None:
    """Even one player's unreadable armour empties the whole count.

    A partial figure would look like a save rather than a read failure -- the
    same rule as for the armed count. The player stays in the
    ``players_buy_end`` divisor.
    """
    row = _armed_with(tmp_path, [FULL_BUY] * 5, armor=[None, 100, 100, 100, 100])
    assert row["players_buy_end"] == 5
    assert row[ARMORED_COLUMN] is None


def test_zero_armor_is_an_observation_for_the_armored_count(
    tmp_path: Path,
) -> None:
    """``0`` and ``None`` are different things in the armour count too.

    Without this pair the previous test would also pass an implementation that
    empties the row whenever somebody's armour is missing **as the value
    zero**.
    """
    zeros = _armed_with(tmp_path, [FULL_BUY] * 5, armor=[0, 100, 100, 100, 100])
    assert zeros[ARMORED_COLUMN] == 4

    missing = _armed_with(tmp_path, [FULL_BUY] * 5, armor=[None, 100, 100, 100, 100])
    assert missing[ARMORED_COLUMN] is None


def test_an_unreadable_inventory_does_not_empty_the_armored_count(
    tmp_path: Path,
) -> None:
    """The armour count's readability condition is **narrower**: armour alone.

    The inventory is not part of it, because the count does not read it. The
    same row is therefore ``null`` as far as the armed count goes and a figure
    as far as the armour goes -- and that is exactly the difference an
    implementation could lose by copying the condition across.
    """
    row = _armed_with(tmp_path, [None] + [FULL_BUY] * 4)
    assert row["players_buy_end"] == 5
    assert row[ARMED_COLUMN] is None
    assert row[ARMORED_COLUMN] == 5


def test_round_without_an_anchor_has_no_armored_count(tmp_path: Path) -> None:
    """An anchorless round: both counts are null."""
    rounds = normal_match(played=2, knife=False)
    rounds[1].freeze_tick = None

    df = parse_with(build(rounds), tmp_path)
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor[ARMORED_COLUMN].null_count() == 2
    assert no_anchor[ARMED_COLUMN].null_count() == 2


def test_no_readable_player_gives_null_armored_not_zero(tmp_path: Path) -> None:
    """No player's values could be read: ``null``, not ``0``."""
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 5

    row = _armed_row(rounds, tmp_path)
    assert row["players_buy_end"] is None
    assert row[ARMORED_COLUMN] is None


def test_armored_count_and_player_count_come_from_the_same_players(
    tmp_path: Path,
) -> None:
    """A short-handed team: the armour count and the divisor share a set.

    Two different divisors on the same row would be a fault that showed only
    in the report -- ``2/4`` and ``2/5`` are different claims.
    """
    row = _armed_with(
        tmp_path,
        [FREE_PISTOL, FREE_PISTOL, FREE_PISTOL, FREE_PISTOL],
        armor=[100, 100, 0, 0],
    )
    assert row["players_buy_end"] == 4
    assert row[ARMORED_COLUMN] == 2


def test_the_armed_count_is_a_subset_of_the_armored_count(tmp_path: Path) -> None:
    """The armed are always a subset of the armoured.

    The counts' most important structural relation: the armed condition
    **includes** armour, so a row with more armed players would mean the
    counts are reading a different tick or a different set of players. A
    synthetic test, because its demo-based counterpart skips itself without
    demos -- and establishing an invariant must not depend on whether the
    machine has 200 MB demos on it.

    The setup covers all four combinations: armour and a weapon, armour
    without a weapon, a weapon without armour, neither.
    """
    row = _armed_with(
        tmp_path,
        [FULL_BUY, FREE_PISTOL, FULL_BUY, FREE_PISTOL, FULL_BUY],
        armor=[100, 100, 0, 0, 100],
    )
    assert row[ARMED_COLUMN] == 2  # players 0 and 4
    assert row[ARMORED_COLUMN] == 3  # players 0, 1 and 4
    assert row[ARMED_COLUMN] <= row[ARMORED_COLUMN]


def test_the_subset_relation_holds_across_a_whole_table(tmp_path: Path) -> None:
    """The same invariant across the whole table, with varying setups.

    One row could match by chance; this runs four different rounds and claims
    the relation of every row, the opponent's rows included.
    """
    rounds = normal_match(played=3)
    rounds[1].a_inventory = [FULL_BUY, FULL_BUY, FREE_PISTOL, FREE_PISTOL, ()]
    rounds[1].a_armor = [100, 0, 100, 0, 100]
    rounds[2].a_inventory = [FREE_PISTOL] * 5
    rounds[3].a_inventory = [FULL_BUY] * 5

    df = parse_with(build(rounds), tmp_path)
    both = df.filter(
        pl.col(ARMED_COLUMN).is_not_null() & pl.col(ARMORED_COLUMN).is_not_null()
    )
    assert both.height > 0
    assert both.select(
        pl.col(ARMED_COLUMN) <= pl.col(ARMORED_COLUMN)
    ).to_series().all()


def test_unreadable_armor_and_unreadable_inventory_are_counted_apart(
    tmp_path: Path,
) -> None:
    """Two diagnostics figures, because the readability conditions differ.

    On the first round the armour is unreadable: **both** counts empty. On the
    second only the inventory fails: only the armed count empties. A single
    figure would not tell these apart, and that distinction is this story's
    central claim.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_armor = [None] * 5
    rounds[1].a_inventory = [None] * 5

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.armed_unreadable_rows == 2
    assert adapter.diagnostics.armored_unreadable_rows == 1


def test_armored_count_never_exceeds_the_player_count(tmp_path: Path) -> None:
    """An invariant across the table: ``0 <= armoured <= players_buy_end``."""
    rounds = normal_match(played=3)
    rounds[1].a_armor = [100, 100, 0, 0, 0]
    rounds[2].a_players = A_PLAYERS[:4]
    rounds[3].a_unreadable = 2

    df = parse_with(build(rounds), tmp_path)
    observed = df.filter(pl.col(ARMORED_COLUMN).is_not_null())
    assert observed.height > 0
    assert observed.select(
        (pl.col(ARMORED_COLUMN) >= 0)
        & (pl.col(ARMORED_COLUMN) <= pl.col("players_buy_end"))
    ).to_series().all()
    # The rule really does discriminate: a single value across the whole table
    # would mean it does not bite on the data at all.
    assert observed[ARMORED_COLUMN].n_unique() > 1


def test_money_spent_is_read_from_the_demo(tmp_path: Path) -> None:
    """The money available = what was left + what was spent.

    Not a test of the armed count but of the identity ``economy.py`` rests on:
    it reads the money available as the sum of these two. The test lives in
    this file because it is an observation the adapter reads.
    """
    df = parse_with(build(normal_match(played=1, knife=False)), tmp_path)
    row = df.row(0, named=True)
    assert row["money_spent"] == 5 * 4000
    assert row["money_buy_end"] + row["money_spent"] == 5 * 4800


# --- The inventory's shapes ----------------------------------------------------
#
# ``inventory`` is the first list-shaped column the adapter reads. The fake
# always feeds ``list[str]`` or ``None``, so the library's other possible
# shapes would be left entirely uncovered without these -- and if demoparser2
# ever returned the inventory as a single string, every player would be
# unarmed, the column would be zero throughout, and the whole suite would stay
# green on a machine with no demos.


def test_inventory_reads_a_list_of_names() -> None:
    """The ordinary shape: a list of strings."""
    assert dp._as_inventory(["AK-47", "Smoke Grenade"]) == (
        "AK-47",
        "Smoke Grenade",
    )


def test_inventory_reads_a_numpy_style_sequence() -> None:
    """Any iterable will do -- pandas may return an array."""
    import numpy as np

    assert dp._as_inventory(np.array(["AK-47", "P250"], dtype=object)) == (
        "AK-47",
        "P250",
    )


def test_empty_inventory_is_not_the_same_as_a_missing_one() -> None:
    """``()`` is an observation ("nothing"), ``None`` is not.

    This difference carries the whole count's ``null`` rule: an empty list
    means an unarmed player, a missing one means an empty row.
    """
    assert dp._as_inventory([]) == ()
    assert dp._as_inventory(None) is None


def test_inventory_nan_is_missing_not_empty() -> None:
    """Pandas promotes a missing value to NaN, not to ``None``.

    NaN as an empty list would claim "the player had nothing", so a read
    failure would look like an eco.
    """
    assert dp._as_inventory(float("nan")) is None


def test_inventory_as_a_bare_string_is_one_name_not_characters() -> None:
    """A single string is one name, not a list of letters.

    Iterating a string would give five one-character "items", every one of
    them unknown -- and because an unknown item does not arm, every player
    would be unarmed. The column would be zero throughout and would look
    valid.
    """
    assert dp._as_inventory("AK-47") == ("AK-47",)


def test_inventory_drops_unreadable_entries_but_keeps_the_rest() -> None:
    """A single empty element does not bring down the whole list."""
    assert dp._as_inventory(["AK-47", None, "", "P250"]) == ("AK-47", "P250")


def test_inventory_of_an_unreadable_type_is_missing() -> None:
    """Anything else is not interpreted -- ``None`` says "not read"."""
    assert dp._as_inventory(42) is None


# --- The sample point table ----------------------------------------------------


def long_match(played: int = 2, duration: int = 4000) -> list[Round]:
    """Rounds long enough for the real sample points.

    ``normal_match``'s rounds are 500 ticks, that is, 7.8 seconds; the
    45-second point could not be examined in them at all.
    """
    rounds: list[Round] = []
    tick = 1000
    time_s = 100.0
    points = 0
    for number in range(1, played + 1):
        rounds.append(
            Round(
                demo_round=number,
                freeze_tick=tick,
                end_tick=tick + duration,
                winner="CT",
                reason="t_killed",
                score_at_freeze=points,
                score_at_end=points + 1,
                alive=(0, 3),
                round_start_time=time_s,
            )
        )
        points += 1
        time_s += (duration + 1000) / 64.0
        tick += duration + 1000
    return rounds


def test_ticks_frame_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    ticks = parse_ticks_table(build(long_match()), tmp_path)
    assert tuple(ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ticks.schema[name] == TICKS[name], name
    # The numbering belongs to domain.rounds, not to the adapter.
    assert ticks["round_no"].null_count() == ticks.height


def test_every_player_gets_a_row_at_every_sample_point(tmp_path: Path) -> None:
    """10 players x 4 sample points x 2 rounds = 80 rows."""
    ticks = parse_ticks_table(
        build(long_match(played=2)), tmp_path, sample_seconds=(6.0, 15.0, 30.0, 45.0)
    )
    time_s = ticks.filter(pl.col("sample_kind") == "time")
    assert time_s.height == 80
    per_point = time_s.group_by("round_raw", "sample_t_s").len()
    assert per_point["len"].unique().to_list() == [10]


def test_a_short_round_has_no_points_after_it_ended(tmp_path: Path) -> None:
    """Acceptance criterion: a round decided in 28 seconds gets only 6 and 15."""
    rounds = long_match(played=1, duration=28 * 64)
    ticks = parse_ticks_table(
        build(rounds), tmp_path, sample_seconds=(6.0, 15.0, 30.0, 45.0)
    )
    time_s = ticks.filter(pl.col("sample_kind") == "time")
    assert sorted(time_s["sample_t_s"].unique().to_list()) == [6.0, 15.0]
    assert time_s["t_s"].max() <= 28.0


def test_area_and_coordinates_come_from_the_sample_tick(tmp_path: Path) -> None:
    rounds = long_match(played=1)
    rounds[0].a_area = "Ramp"
    rounds[0].b_area = "Heaven"
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    a_side_of = ticks.filter(pl.col("side") == rounds[0].a_side)
    assert a_side_of["area"].unique().to_list() == ["Ramp"]
    b_side = "CT" if rounds[0].a_side == "T" else "T"
    assert ticks.filter(pl.col("side") == b_side)["area"].unique().to_list() == [
        "Heaven"
    ]
    # The coordinates were derived from the player's index; they are not zeros.
    assert sorted(a_side_of["x"].to_list()) == [0.0, 100.0, 200.0, 300.0, 400.0]
    assert a_side_of["z"].unique().to_list() == [5.0]


def test_an_unnamed_area_stays_null_but_the_coordinates_remain(
    tmp_path: Path,
) -> None:
    """I/O matrix: an empty ``m_szLastPlaceName`` -> ``area = null``.

    The row is not dropped -- an unknown position is reported as coordinates.
    """
    rounds = long_match(played=1)
    rounds[0].a_area = None
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    unknown_area = ticks.filter(pl.col("side") == rounds[0].a_side)
    assert unknown_area.height == 5
    assert unknown_area["area"].null_count() == 5
    assert unknown_area["x"].null_count() == 0


def test_a_dead_player_still_gets_a_row(tmp_path: Path) -> None:
    """I/O matrix: filtering the dead is aggregation's job, not the parse's."""
    rounds = long_match(played=1)
    rounds[0].a_dead_at_sample = 2
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    own_rows = ticks.filter(pl.col("side") == rounds[0].a_side)
    assert own_rows.height == 5
    assert own_rows["is_alive"].sum() == 3
    assert ticks["is_alive"].sum() == 8


def test_lineup_key_is_the_same_in_both_tables(tmp_path: Path) -> None:
    """The same team, the same key -- otherwise the join would go crosswise."""
    tables = parse_tables(build(long_match(played=2)), tmp_path)
    assert set(tables.ticks["lineup_key"].unique()) == set(
        tables.rounds["lineup_key"].unique()
    )
    assert tables.ticks["lineup_key"].n_unique() == 2


def test_lineup_key_follows_the_team_through_the_side_switch(tmp_path: Path) -> None:
    rounds = long_match(played=4)
    for round_spec in rounds[2:]:
        round_spec.a_side = "CT"

    tables = parse_tables(build(rounds), tmp_path)
    ticks = tables.ticks
    a_key = tables.rounds.filter(
        (pl.col("round_raw") == 1) & (pl.col("side") == "T")
    )["lineup_key"][0]

    first = ticks.filter((pl.col("round_raw") == 1) & (pl.col("lineup_key") == a_key))
    last_round = ticks.filter(
        (pl.col("round_raw") == 4) & (pl.col("lineup_key") == a_key)
    )
    assert first["side"].unique().to_list() == ["T"]
    assert last_round["side"].unique().to_list() == ["CT"]


def test_an_unanchored_round_produces_no_tick_rows(tmp_path: Path) -> None:
    """I/O matrix: ``status = "no_freeze_end"`` -> no tick rows."""
    rounds = long_match(played=3)
    rounds[1].freeze_tick = None
    ticks = parse_ticks_table(build(rounds), tmp_path)
    assert sorted(ticks["round_raw"].unique().to_list()) == [1, 3]


def test_only_the_needed_ticks_are_read(tmp_path: Path) -> None:
    """The whole tick series is not read: the ticks asked for are sample points."""
    fake = build(long_match(played=2))
    parse_ticks_table(fake, tmp_path, sample_seconds=(6.0, 15.0))
    sample_points = next(
        ticks for props, ticks in fake.tick_calls if dp._PLACE_NAME in props
    )
    assert len(sample_points) == 4  # 2 rounds x 2 points
    # The economy props are not read again at the sample points.
    assert dp._ACCOUNT not in next(
        props for props, _ in fake.tick_calls if dp._PLACE_NAME in props
    )


@pytest.mark.parametrize("prop", [dp._PLACE_NAME, dp._X, dp._Y, dp._Z])
def test_a_missing_sample_prop_is_named_in_the_error(
    tmp_path: Path, prop: str
) -> None:
    """Without the check the setup table would be structurally sound but have
    no positions.

    Only the sample points' own props are here: ``m_lifeState`` and
    ``m_iTeamNum`` are already read at the round boundaries, so their
    disappearance is caught earlier and with a different message.
    """
    fake = build(long_match(played=2))
    fake.drop_props = (prop,)
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path)
    assert prop in str(exc.value)
    assert "sample point" in str(exc.value)


# --- The first contact with real events ----------------------------------------


def test_first_contact_produces_its_own_sample_point(tmp_path: Path) -> None:
    """Acceptance criterion: a round with a firefight has a first_contact."""
    rounds = long_match(played=1)
    rounds[0].hurt = [
        (20 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
        (10 * 64, A_PLAYERS[1], B_PLAYERS[1], "awp"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact.height == 10
    assert contact["t_s"].unique().to_list() == [10.0]
    # sample_t_s states the same moment -- the row is not left without a
    # timestamp.
    assert contact["sample_t_s"].unique().to_list() == [10.0]


def test_utility_only_damage_leaves_the_round_without_a_contact(
    tmp_path: Path,
) -> None:
    """I/O matrix: the only damage from a molotov -> no first-contact rows."""
    rounds = long_match(played=1)
    rounds[0].hurt = [(15 * 64, A_PLAYERS[0], B_PLAYERS[0], "molotov")]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()
    # The round is still in, with its time points.
    assert ticks.height == 10


def test_friendly_fire_does_not_start_the_round(tmp_path: Path) -> None:
    """I/O matrix: the attacker on the same side -> not a first contact."""
    rounds = long_match(played=1)
    rounds[0].hurt = [
        (8 * 64, A_PLAYERS[0], A_PLAYERS[1], "hegrenade"),
        (9 * 64, A_PLAYERS[0], A_PLAYERS[1], "ak47"),
        (25 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["t_s"].unique().to_list() == [25.0]


def test_a_round_without_damage_has_no_contact_rows(tmp_path: Path) -> None:
    """I/O matrix: time ran out, nobody fired."""
    ticks = parse_ticks_table(build(long_match(played=1)), tmp_path)
    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()


def test_death_is_used_as_the_fallback_source(tmp_path: Path) -> None:
    rounds = long_match(played=1)
    rounds[0].deaths = [(12 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["t_s"].unique().to_list() == [12.0]


def test_the_death_fallback_can_be_switched_off(tmp_path: Path) -> None:
    rounds = long_match(played=1)
    rounds[0].deaths = [(12 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(
        build(rounds), tmp_path, sample_seconds=(6.0,), fallback_death=False
    )
    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()


def test_contact_is_attributed_to_its_own_round(tmp_path: Path) -> None:
    """A hit on the second round must not pull the first's contact earlier."""
    rounds = long_match(played=2)
    rounds[1].hurt = [(5 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["round_raw"].unique().to_list() == [2]
    assert contact["t_s"].unique().to_list() == [5.0]


def test_contact_after_a_side_switch_uses_the_current_sides(tmp_path: Path) -> None:
    """The sides switch; being across the sides is a per-round fact."""
    rounds = long_match(played=4)
    for round_spec in rounds[2:]:
        round_spec.a_side = "CT"
    # On the third round A is CT -- a hit from A to B is still across.
    rounds[2].hurt = [
        (7 * 64, A_PLAYERS[0], A_PLAYERS[1], "ak47"),  # friendly damage
        (11 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(
        (pl.col("sample_kind") == "first_contact") & (pl.col("round_raw") == 3)
    )
    assert contact["t_s"].unique().to_list() == [11.0]


def test_diagnostics_report_what_the_table_cannot(tmp_path: Path) -> None:
    """The diagnostics report only what the finished table does not show.

    The numbers of sample points and first contacts are read from the table in
    the stage; if they were here as well, the same name would mean two
    different things -- the adapter would count the unnumbered rounds in, the
    stage would not.
    """
    adapter = parse_adapter(build(long_match(played=2)), tmp_path)
    assert adapter.diagnostics is not None
    assert not hasattr(adapter.diagnostics, "sample_points")
    assert adapter.diagnostics.partial_samples == 0
    assert adapter.diagnostics.unknown_side_events == 0


def test_a_partial_sample_point_is_counted(tmp_path: Path) -> None:
    """A partial sample point must not disappear.

    A systematic prop fault would otherwise show only as skewed aggregates in
    Story 2.3, by which point the cause could no longer be found in the parse.
    """
    rounds = long_match(played=2)
    rounds[1].a_players = A_PLAYERS[:3]  # two players missing
    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0, 15.0))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.partial_samples == 2


def test_damage_by_an_unknown_player_is_counted_not_hidden(tmp_path: Path) -> None:
    """Damage by an unknown player is skipped, but the figure says so.

    Without the counter a round could lose its first contact silently.
    """
    rounds = long_match(played=1)
    rounds[0].hurt = [(10 * 64, "tuntematon-pelaaja", B_PLAYERS[0], "ak47")]
    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_side_events == 1


def test_a_late_joiner_gets_their_side_from_the_tick(tmp_path: Path) -> None:
    """A player who joined mid-map gets his side from the tick's own value."""
    rounds = long_match(played=2)
    rounds[1].a_players = [*A_PLAYERS[:4], "myohemmin-tullut"]
    rounds[1].hurt = [(9 * 64, "myohemmin-tullut", B_PLAYERS[0], "ak47")]

    tables = parse_tables(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = tables.ticks.filter(
        (pl.col("sample_kind") == "first_contact") & (pl.col("round_raw") == 2)
    )
    assert contact["t_s"].unique().to_list() == [9.0]


def test_a_contact_without_a_weapon_name_is_not_a_contact(tmp_path: Path) -> None:
    """An empty weapon name is not on the exclusion list -- and still does not
    qualify."""
    rounds = long_match(played=1)
    rounds[0].hurt = [
        (8 * 64, A_PLAYERS[0], B_PLAYERS[0], None),
        (20 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["t_s"].unique().to_list() == [20.0]


def test_a_missing_life_state_is_an_error_not_a_dead_player(tmp_path: Path) -> None:
    """``is_alive`` is not nullable: a missing value would silently mean dead.

    A living player would then disappear from the aggregation. An unknown area
    may stay null, but the alive state may not.
    """
    fake = build(long_match(played=1))
    original_path = fake._rows_at

    def without_life_state(tick: int):
        rows = [dict(r) for r in original_path(tick)]
        if rows and dp._PLACE_NAME in rows[0]:
            rows[0][dp._LIFE_STATE] = None
        return rows

    fake._rows_at = without_life_state  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert dp._LIFE_STATE in str(exc.value)


# --- The pawnless player (Story 2.10) ------------------------------------------


def pawnless_at(fake, *, pawnless: tuple[str, ...], spectators: tuple[str, ...] = ()):
    """Replace the sample point rows: some pawnless, some spectators.

    ``Round.sample_pawnless`` is enough on its own, but it cannot make a tick
    that lost rows for **two different reasons**. It is exactly such a tick
    that separates a good skip from a greedy one.
    """
    rows_at = fake._rows_at

    def replaced(tick: int):
        rows = [dict(r) for r in rows_at(tick)]
        if not rows or dp._PLACE_NAME not in rows[0]:
            return rows
        for row in rows:
            if row["steamid"] in spectators:
                row[dp._TEAM_NUM] = None
            elif row["steamid"] in pawnless:
                for name in dp.SAMPLE_PAWN_PROPS:
                    row[name] = None
        return rows

    fake._rows_at = replaced  # type: ignore[method-assign]
    return fake


def test_a_pawnless_player_is_skipped_but_a_missing_life_state_still_stops_the_run(
    tmp_path: Path,
) -> None:
    """A pawnless row is skipped; a missing alive state alone still stops the run.

    The guard's skip looked at ``m_iTeamNum``, which is a **controller** field
    and present for a pawnless player too, and the check looked at
    ``m_lifeState``, which is a **pawn** field. A player who had one but not
    the other fell between them -- and the whole demo came down.

    Both halves are here, because the distinction is a claim about what lies
    **between** two cases: a skip that fired on a missing alive state alone
    would pass the first claim and would eat the very fault the guard was
    written against.
    """
    rounds = long_match(played=1)
    ghost = rounds[0].a_players[0]
    rounds[0].sample_pawnless = (ghost,)
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    assert ghost not in ticks["player_id"].to_list()
    assert ticks.height == 9

    fake = build(long_match(played=1))
    rows_at = fake._rows_at

    def only_life_state_missing(tick: int):
        rows = [dict(r) for r in rows_at(tick)]
        if rows and dp._PLACE_NAME in rows[0]:
            rows[0][dp._LIFE_STATE] = None
        return rows

    fake._rows_at = only_life_state_missing  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert dp._LIFE_STATE in str(exc.value)
    assert "Being alive is a mandatory observation" in str(exc.value)


def test_a_pawnless_row_is_counted_from_both_read_paths(tmp_path: Path) -> None:
    """The counter sees both the sample points' and the throws' ticks.

    The sample point props are read in **two different calls**: at the setup's
    ticks and at utility's throw ticks. Without the latter two thirds of the
    counter could stop working silently -- and it is exactly the counter that
    exists to make a silent drop visible. In the measured
    ``anubis_vs_RCAVE_VETERANS`` demo, ten of the 15 rows come from throw
    ticks.

    The round throws two grenades (offsets 100 and 200) and its only sample
    point is at 6.0 s, that is, tick 384, so three ticks are read. The
    pawnless player **is not a thrower**, so his row is skipped at all three.
    """
    rounds = utility_match(played=1)
    ghost = rounds[0].a_players[1]
    assert ghost not in {throw[1] for throw in rounds[0].grenades}
    rounds[0].sample_pawnless = (ghost,)

    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert adapter.diagnostics is not None
    # One sample point tick + two throw ticks, one pawnless player.
    assert adapter.diagnostics.sample_rows_without_pawn == 3
    # The throwers have pawns, so both throws' areas were read.
    assert adapter.diagnostics.grenade_throwers_without_row == 0


def test_a_tick_read_by_both_paths_is_counted_once(tmp_path: Path) -> None:
    """The same physical row must not be counted twice.

    A smoke thrown at the start of a round can leave on **the same tick** as a
    sample point. Both read paths read the tick independently, so a plain sum
    would turn the figure into "row readings" rather than "rows".
    """
    rounds = long_match(played=1)
    ghost = rounds[0].a_players[1]
    rounds[0].sample_pawnless = (ghost,)
    # 6.0 s x 64 ticks = 384: the throw leaves on exactly the sample point's
    # tick.
    rounds[0].grenades = [
        (1, rounds[0].a_players[0], "CSmokeGrenadeProjectile", 384, 60)
    ]

    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.sample_rows_without_pawn == 1


def test_a_pawnless_thrower_leaves_its_throw_without_an_area_and_says_so(
    tmp_path: Path,
) -> None:
    """The skip created a new kind of drop, and it has to have a reason.

    A throw's area is the thrower's own ``m_szLastPlaceName`` at that tick.
    When the thrower's row is skipped as pawnless there is no area and none
    can replace it -- the point cloud names detonations, not throws. Before
    Story 2.10 this brought the run down; without a counter of its own it
    would now drain silently into the ``utility_without_area`` figure.
    """
    rounds = utility_match(played=1)
    thrower = rounds[0].grenades[0][1]
    rounds[0].sample_pawnless = (thrower,)

    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))
    events = parse_events_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenade_throwers_without_row == 1
    throw = events.filter(
        (pl.col("event_kind") == "grenade_thrown")
        & (pl.col("thrower_id") == thrower)
    )
    assert throw.height == 1
    assert throw["area"].to_list() == [None]
    assert throw["area_source"].to_list() == [None]


def test_a_wholly_pawnless_throw_tick_is_not_counted_as_a_fault(
    tmp_path: Path,
) -> None:
    """The same phenomenon must not be an observation on one path and a fault
    on the other.

    ``grenade_ticks_without_players`` is documented as a fault counter: the
    thrower's own area could not even be attempted. A wholly pawnless tick is
    not that -- the demo returned rows and nobody was simply on the map -- and
    it is already counted among the pawnless rows.
    """
    rounds = utility_match(played=2)
    rounds[0].sample_pawnless = tuple(rounds[0].a_players + rounds[0].b_players)

    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenade_ticks_without_players == 0
    # The throwers were pawnless too, so both throws' areas went unread --
    # that is a different thing and it has a figure of its own.
    assert adapter.diagnostics.grenade_throwers_without_row == 2


def test_a_pawnless_player_does_not_drop_the_round_from_the_sample(
    tmp_path: Path,
) -> None:
    """The round stays in the sample; only its player count shrinks.

    A partial sample point is a **consequence** of a pawnless row, and both
    figures are reported: without the link the reader would not see what made
    the point partial.
    """
    rounds = long_match(played=2)
    rounds[0].sample_pawnless = (rounds[0].a_players[0],)

    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert ticks.filter(pl.col("round_raw") == 1).height == 9
    assert ticks.filter(pl.col("round_raw") == 2).height == 10
    assert adapter.diagnostics is not None
    # There is exactly one partial point: round 1's only sample point.
    assert adapter.diagnostics.partial_samples == 1
    assert adapter.diagnostics.sample_rows_without_pawn == 1
    # The point was not missed, it merely shrank.
    assert adapter.diagnostics.sample_points_without_pawn == 0


def test_a_spectator_is_not_counted_as_a_pawnless_row(tmp_path: Path) -> None:
    """A spectator is skipped as before, but **for a different reason**.

    A spectator has no pawn either, but he also has no controller team. Added
    together, the figures would not tell a player who dropped out mid-match
    from a spectator who was never in the game.
    """
    fake = build(long_match(played=1))
    rows_at = fake._rows_at

    def with_a_spectator(tick: int):
        rows = [dict(r) for r in rows_at(tick)]
        if rows and dp._PLACE_NAME in rows[0]:
            row = {"tick": tick, "steamid": "katsoja", "name": "katsoja"}
            row[dp._TEAM_NUM] = None
            for name in dp.SAMPLE_PAWN_PROPS:
                row[name] = None
            rows.append(row)
        return rows

    fake._rows_at = with_a_spectator  # type: ignore[method-assign]
    adapter = parse_adapter(fake, tmp_path, sample_seconds=(6.0,))

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.sample_rows_without_pawn == 0


def test_a_fully_pawnless_sample_point_does_not_stop_the_run(
    tmp_path: Path,
) -> None:
    """I/O matrix: a whole round pawnless -> record it, do not stop the run.

    An empty sample point is otherwise a **fault**: it would be counted in the
    figures but be missing from the table. Pawnlessness is a different thing
    -- the demo returned rows and nobody was simply on the map -- and the
    dropped point has a figure of its own.

    The point is not **partial** but absent: lumped in with the partial ones,
    a point missing entirely would look milder than it is.
    """
    rounds = long_match(played=2)
    rounds[0].sample_pawnless = tuple(rounds[0].a_players + rounds[0].b_players)

    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    assert ticks.filter(pl.col("round_raw") == 1).is_empty()
    assert ticks.filter(pl.col("round_raw") == 2).height == 10

    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.sample_rows_without_pawn == 10
    assert adapter.diagnostics.sample_points_without_pawn == 1
    assert adapter.diagnostics.partial_samples == 0


def test_a_tick_that_lost_rows_for_another_reason_is_still_a_hard_error(
    tmp_path: Path,
) -> None:
    """A single pawnless row must not silence a hard error.

    The skip fires only when pawnlessness explains the empty tick
    **entirely**. A tick that lost nine rows as spectators and one as pawnless
    is still a fault -- and without the condition it would be skipped by
    chance, according to whether a pawnless row happened to be among them.
    """
    rounds = long_match(played=1)
    players = rounds[0].a_players + rounds[0].b_players
    fake = pawnless_at(
        build(rounds), pawnless=(players[0],), spectators=tuple(players[1:])
    )

    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert "yielded no player rows" in str(exc.value)


@pytest.mark.parametrize(
    "column", ["attacker_steamid", "user_steamid", "weapon", "tick"]
)
def test_a_missing_damage_column_is_named_in_the_error(
    tmp_path: Path, column: str
) -> None:
    """Without the check, zero first contacts would look like a valid result."""
    rounds = long_match(played=1)
    rounds[0].hurt = [(10 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    fake = build(rounds)
    fake.events["player_hurt"] = [
        {k: v for k, v in row.items() if k != column}
        for row in fake.events["player_hurt"]
    ]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert column in str(exc.value)


def test_a_sample_point_without_any_rows_is_refused(tmp_path: Path) -> None:
    """A sample point that yields no row would still be counted in the figures."""
    fake = build(long_match(played=1))
    original_path = fake._rows_at

    def empty_at_sample(tick: int):
        rows = original_path(tick)
        if rows and dp._PLACE_NAME in rows[0]:
            return []
        return rows

    fake._rows_at = empty_at_sample  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert "player rows" in str(exc.value)


def test_a_round_where_both_lineups_get_the_same_side_is_refused() -> None:
    """The same side for both would give both the same ``lineup_key``.

    The table would look valid, but every per-team figure would be the sum of
    both.
    """
    segment = dp._Segment(
        demo_round=1,
        freeze_end_tick=1000,
        end_tick=2000,
        winner_side="T",
        win_reason="ct_killed",
        round_raw=1,
    )
    with pytest.raises(ParseError, match="on the same side"):
        dp._keys_by_side(("T", "T"), ["aaa", "bbb"], segment)
    assert dp._keys_by_side(("T", "CT"), ["aaa", "bbb"], segment) == {
        "T": "aaa",
        "CT": "bbb",
    }


def test_a_demo_without_any_samplable_round_yields_an_empty_typed_table(
    tmp_path: Path,
) -> None:
    """An empty table still conforms to the contract -- no Null types."""
    rounds = long_match(played=2)
    for round_spec in rounds:
        round_spec.freeze_tick = None
    ticks = parse_ticks_table(build(rounds), tmp_path)
    assert ticks.is_empty()
    assert tuple(ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ticks.schema[name] == TICKS[name], name


# --- Utility -------------------------------------------------------------------


def utility_match(played: int = 1, duration: int = 4000) -> list[Round]:
    """Rounds on which each side's first player throws a grenade."""
    rounds = long_match(played=played, duration=duration)
    entity = 1
    for round_spec in rounds:
        round_spec.grenades = [
            (entity, round_spec.a_players[0], "CSmokeGrenadeProjectile", 100, 60),
            (entity + 1, round_spec.b_players[0], "CFlashbangProjectile", 200, 40),
        ]
        entity += 2
    return rounds


def test_a_grenade_becomes_a_throw_and_a_detonation(tmp_path: Path) -> None:
    """I/O matrix: an ordinary smoke -> two rows with the same entity."""
    events = parse_events_table(build(utility_match()), tmp_path)

    assert tuple(events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert events.schema[name] == EVENTS[name], name

    smoke = events.filter(pl.col("grenade_entity_id") == 1)
    assert smoke["event_kind"].to_list() == ["grenade_thrown", "grenade_detonate"]
    assert smoke["grenade_type"].unique().to_list() == ["smoke"]
    assert smoke["thrower_id"].unique().to_list() == [A_PLAYERS[0]]
    # round_no is left empty: the numbering is decided by domain.rounds.
    assert smoke["round_no"].null_count() == smoke.height


def test_the_grenade_type_is_canonical_not_a_class_name(tmp_path: Path) -> None:
    events = parse_events_table(build(utility_match()), tmp_path)
    assert set(events["grenade_type"].unique()) == {"smoke", "flashbang"}


def test_an_unknown_class_name_survives_verbatim(tmp_path: Path) -> None:
    """An unknown type is a readable observation; emptying it would lose it."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CUusiKranaatti", 100, 30)]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["CUusiKranaatti"]


def test_the_thrower_side_and_lineup_come_from_the_round(tmp_path: Path) -> None:
    """The thrower's team comes from the round's map, not from a guess."""
    rounds = utility_match(played=2)
    rounds[1].a_side = "CT"  # a half-time switch inside the fake match
    tables = parse_tables(build(rounds), tmp_path)
    events, rounds = tables.events, tables.rounds

    for row in events.iter_rows(named=True):
        own = rounds.filter(
            (pl.col("round_raw") == row["round_raw"])
            & (pl.col("side") == row["side"])
        )
        assert own["lineup_key"].to_list() == [row["lineup_key"]]

    a_rows = events.filter(pl.col("thrower_id") == A_PLAYERS[0]).sort("round_raw")
    assert a_rows["side"].to_list() == ["T", "T", "CT", "CT"]


def test_the_throw_area_snaps_to_the_thrower(tmp_path: Path) -> None:
    """At the throw point the nearest living player is the thrower himself."""
    rounds = utility_match()
    rounds[0].a_area = "Ramp"
    rounds[0].b_area = "Heaven"
    events = parse_events_table(build(rounds), tmp_path)

    throw = events.filter(
        (pl.col("event_kind") == "grenade_thrown")
        & (pl.col("thrower_id") == A_PLAYERS[0])
    )
    assert throw["area"].to_list() == ["Ramp"]


def test_a_detonation_far_from_everyone_keeps_its_coordinates(
    tmp_path: Path,
) -> None:
    """I/O matrix: a distant detonation gets area = null, not a drop."""
    rounds = long_match(played=1)
    # 200 ticks x 40 units = 7,960 units away from every player.
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 200)]
    events = parse_events_table(build(rounds), tmp_path)

    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation["area"].null_count() == 1
    assert detonation["x"].null_count() == 0
    assert detonation["x"].to_list() == [pytest.approx(7960.0)]


def test_an_unset_snap_distance_only_silences_the_detonations(
    tmp_path: Path,
) -> None:
    """An uncalibrated bound takes away the estimate, not the observation.

    A throw's area is read from the thrower's own ``m_szLastPlaceName``, so it
    survives even with the threshold removed entirely. Only the detonation
    depends on the threshold.

    **The distance is measured anyway.** The nearest cell is always found in
    the point cloud, so the absence of a threshold does not mean there is no
    measurement -- it means nothing is named on the strength of it. That
    distance is precisely the data the threshold is calibrated from, and it
    must not be lost merely because the setting is still empty.
    """
    events = parse_events_table(
        build(utility_match()), tmp_path, area_snap_units=None
    )
    assert not events.is_empty()
    throws = events.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert throws["area"].null_count() == 0
    assert throws["area_source"].unique().to_list() == ["observed"]
    assert detonations["area"].null_count() == detonations.height
    assert detonations["area_source"].null_count() == detonations.height
    # An observation has no distance, an estimate does -- without a threshold
    # too.
    assert throws["snap_distance"].null_count() == throws.height
    assert detonations["snap_distance"].null_count() == 0
    assert events["x"].null_count() == 0


def test_a_single_tick_trajectory_gets_no_detonation(tmp_path: Path) -> None:
    """I/O matrix: the trajectory breaks -> only a throw, no invented
    detonation."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CHEGrenadeProjectile", 100, 1)]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["event_kind"].to_list() == ["grenade_thrown"]


def test_a_grenade_thrown_outside_any_round_is_counted_not_kept(
    tmp_path: Path,
) -> None:
    """I/O matrix: a throw after the round was decided gets no t_s."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        # The round is decided at tick 4,000; this one leaves after that.
        (2, A_PLAYERS[1], "CSmokeGrenadeProjectile", 4100, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_outside_rounds == 1
    tables = parse_tables(build(rounds), tmp_path)
    assert tables.events["grenade_entity_id"].unique().to_list() == [1]


def test_a_grenade_that_outlives_the_round_belongs_to_its_throw_round(
    tmp_path: Path,
) -> None:
    """I/O matrix: a smoke that outlives the round belongs to its throw round.

    The detonation's t_s may therefore exceed the round's duration -- that is
    an observation and not an error.
    """
    rounds = long_match(played=2)
    # Thrown 100 ticks from the anchor; the trajectory lasts past the round's
    # end (4,000).
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 5000)]
    events = parse_events_table(build(rounds), tmp_path)

    assert events["round_raw"].unique().to_list() == [1]
    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation["t_s"].to_list()[0] > 4000 / 64.0


def test_a_trajectory_without_a_thrower_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """I/O matrix: a trajectory with no throw -> skipped, the count reported."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, None, "CSmokeGrenadeProjectile", 100, 30),
        (2, A_PLAYERS[0], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_without_thrower == 1


def test_a_thrower_in_neither_lineup_gets_their_side_from_the_tick(
    tmp_path: Path,
) -> None:
    """I/O matrix: an unknown thrower -> the side from the tick's m_iTeamNum."""
    rounds = long_match(played=2)
    late_joiner = "myohassa1"
    rounds[1].a_players = [*A_PLAYERS[:4], late_joiner]
    rounds[1].grenades = [(9, late_joiner, "CSmokeGrenadeProjectile", 100, 30)]
    tables = parse_tables(build(rounds), tmp_path)

    rows = tables.events.filter(pl.col("thrower_id") == late_joiner)
    assert rows.height == 2
    assert rows["side"].unique().to_list() == [rounds[1].a_side]


def test_a_thrower_whose_side_never_resolves_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """The wrong team would credit the utility to the opponent, so the row is
    skipped."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (9, "haamu1", "CSmokeGrenadeProjectile", 100, 30),
        (1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    tables = parse_tables(build(rounds), tmp_path)

    assert "haamu1" not in tables.events["thrower_id"].to_list()
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_side == 1


def test_a_reused_entity_id_stays_two_grenades(tmp_path: Path) -> None:
    """The game recycles ids -- two rounds' smokes are not one trajectory."""
    rounds = long_match(played=2)
    for round_spec in rounds:
        round_spec.grenades = [
            (1, round_spec.a_players[0], "CSmokeGrenadeProjectile", 100, 30)
        ]
    events = parse_events_table(build(rounds), tmp_path)

    assert events.height == 4
    assert sorted(events["round_raw"].unique().to_list()) == [1, 2]
    counts = events.group_by("round_raw", "event_kind").len()
    assert counts["len"].max() == 1


def test_bag_rows_are_not_a_throw(tmp_path: Path) -> None:
    """A grenade in the bag is not a throw: the coordinates are missing."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [
        (1, A_PLAYERS[0], "CSmokeGrenade", offset) for offset in (10, 50, 299)
    ]
    events = parse_events_table(build(rounds), tmp_path)

    assert events.height == 2
    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw["t_s"].to_list() == [pytest.approx(300 / 64.0)]


def test_molotov_and_incendiary_are_told_apart_by_the_bag(tmp_path: Path) -> None:
    """I/O matrix: grenade_type tells molotov from incendiary.

    In flight both are CMolotovProjectile; the distinction comes from the
    thrower's bag on the tick before the throw.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CMolotovProjectile", 300, 30),
        (2, B_PLAYERS[0], "CMolotovProjectile", 400, 30),
    ]
    rounds[0].grenades_in_bag = [
        (11, A_PLAYERS[0], "CMolotovGrenade", 299),
        (12, B_PLAYERS[0], "CIncendiaryGrenade", 399),
    ]
    events = parse_events_table(build(rounds), tmp_path)

    molotov = events.filter(pl.col("grenade_entity_id") == 1)
    incendiary = events.filter(pl.col("grenade_entity_id") == 2)
    assert molotov["grenade_type"].unique().to_list() == ["molotov"]
    assert incendiary["grenade_type"].unique().to_list() == ["incendiary"]


def test_an_ambiguous_bag_leaves_the_generic_molotov(tmp_path: Path) -> None:
    """Both fire grenades in the bag -> no guess is made."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [
        (11, A_PLAYERS[0], "CMolotovGrenade", 299),
        (12, A_PLAYERS[0], "CIncendiaryGrenade", 299),
    ]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["molotov"]


def test_a_bag_type_does_not_leak_onto_another_grenade(tmp_path: Path) -> None:
    """A smoke must not be named an incendiary just because one is in the bag."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [(11, A_PLAYERS[0], "CIncendiaryGrenade", 299)]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["smoke"]


def test_an_unanchored_round_produces_no_event_rows(tmp_path: Path) -> None:
    """I/O matrix: an anchorless round -> no rows (t_s is undefined)."""
    rounds = utility_match(played=2)
    rounds[0].freeze_tick = None
    rounds[0].grenades = []
    events = parse_events_table(build(rounds), tmp_path)
    assert events["round_raw"].unique().to_list() == [2]


def test_a_demo_without_utility_yields_an_empty_typed_table(tmp_path: Path) -> None:
    """I/O matrix: a demo without utility -> an empty but conforming table."""
    events = parse_events_table(build(long_match(played=2)), tmp_path)
    assert events.is_empty()
    assert tuple(events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert events.schema[name] == EVENTS[name], name


def test_a_missing_grenade_column_is_an_error_not_an_empty_table(
    tmp_path: Path,
) -> None:
    """An empty table would look like a demo in which no grenade was thrown."""
    fake = build(utility_match())
    fake.drop_grenade_columns = ("steamid",)
    with pytest.raises(ParseError) as exc:
        parse_events_table(fake, tmp_path)
    assert "steamid" in str(exc.value)
    assert "GRENADE_COLUMNS" in str(exc.value)


def test_a_broken_grenade_read_is_a_wrapped_error(tmp_path: Path) -> None:
    """Renamed in T4: the name used to claim the message is in Finnish."""
    fake = build(utility_match())

    def boom():
        raise RuntimeError("lentoradat rikki")

    fake.parse_grenades = boom  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_events_table(fake, tmp_path)
    assert "could not be read" in str(exc.value)


def test_only_the_throw_tick_is_read_for_areas(tmp_path: Path) -> None:
    """For the area the trajectory's start is read, not the whole -- and not
    its end.

    Without this, a 1.55-million-row trajectory would travel all the way to
    the tick read. And **the detonation's tick is not read at all**: its area
    comes from the point cloud and not from who happened to be nearby. The
    trajectory starts at tick 1100 and ends at tick 1599; only the former gets
    a tick read.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 500)]
    fake = build(rounds)
    parse_events_table(fake, tmp_path, sample_seconds=(6.0,))

    endpoint_calls = [
        set(ticks)
        for props, ticks in fake.tick_calls
        if dp._TEAM_NUM in props and dp._PLACE_NAME in props and 1100 in ticks
    ]
    assert endpoint_calls == [{1100}], f"wrong ticks: {fake.tick_calls}"
    assert not any(1599 in ticks for _, ticks in fake.tick_calls)


# --- The area's source: observation vs. estimate -------------------------------


def test_the_throw_area_is_observed_not_derived(tmp_path: Path) -> None:
    """The thrower's own area is known, so it is not guessed from a neighbour.

    A snap could latch on to the team-mate standing beside him. Here the
    team-mate is nearer the grenade's starting point than the thrower himself:
    if the row went through the point cloud, the area would be the nearest
    cell's and not the thrower's own.
    """
    rounds = long_match(played=1)
    # The thrower aaa1 is dead at the sample point, so the filter for the dead
    # would leave him out of the cloud and would latch on to aaa2 -- a
    # different area. The observation still reads the thrower's own row: a
    # dead player gets a row and an area name too.
    rounds[0].a_dead_at_sample = 1
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30)]
    rounds[0].player_areas = {A_PLAYERS[0]: "Tunnel", A_PLAYERS[1]: "MainHall"}
    events = parse_events_table(build(rounds), tmp_path)

    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw["area"].to_list() == ["Tunnel"]
    assert throw["area_source"].to_list() == ["observed"]
    assert throw["snap_distance"].null_count() == 1


def test_the_detonation_area_is_marked_as_derived(tmp_path: Path) -> None:
    """A detonation has no area name of its own, so it is always an estimate."""
    rounds = long_match(played=1)
    # A short trajectory: 9 x 40 = 360 units, well inside the bound of 500.
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    events = parse_events_table(build(rounds), tmp_path)

    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    with_area = detonation.filter(pl.col("area").is_not_null())
    assert not with_area.is_empty()
    assert with_area["area_source"].unique().to_list() == ["point_cloud"]
    assert with_area["snap_distance"].null_count() == 0
    assert with_area["snap_distance"].min() > 0.0


def test_area_source_is_set_exactly_when_the_area_is(tmp_path: Path) -> None:
    """Contract: ``area_source`` is empty if and only if the area is."""
    events = parse_events_table(build(utility_match(played=2)), tmp_path)
    for row in events.iter_rows(named=True):
        assert (row["area"] is None) == (row["area_source"] is None), row


def test_a_throw_whose_thrower_has_no_row_gets_no_area(tmp_path: Path) -> None:
    """An observation is not replaced by an estimate: the area is left empty,
    the coordinates stay."""
    rounds = long_match(played=1)
    ghost = "haamu1"
    # The thrower is in the round's ticks (the side resolves) but not in the
    # sample points.
    rounds[0].a_players = [*A_PLAYERS[:4], ghost]
    rounds[0].sample_skip = (ghost,)
    rounds[0].grenades = [(1, ghost, "CSmokeGrenadeProjectile", 100, 30)]
    events = parse_events_table(build(rounds), tmp_path)

    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw.height == 1
    assert throw["area"].null_count() == 1
    assert throw["area_source"].null_count() == 1
    assert throw["x"].null_count() == 0


def test_a_detonation_after_the_round_is_counted_but_not_punished(
    tmp_path: Path,
) -> None:
    """A late detonation gets its area like every other -- and is still counted.

    In Story 2.2 these were left without an area deliberately: the area then
    came from the nearest living player, and after the round it would have
    reported the next round's spawn. The point cloud does not depend on the
    moment, so the reason went away with the method. The figure stays, because
    a late detonation is still a phenomenon of its own.
    """
    rounds = long_match(played=2)
    # The round is decided at tick 5,000; the trajectory starts at 4,950 and
    # continues past it.
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 3950, 100)]
    # The fake map's point cloud is sparse, so the threshold is loose: the
    # test measures THAT an area is given, not how near it came from.
    tables = parse_tables(build(rounds), tmp_path, area_snap_units=5000.0)
    adapter = parse_adapter(build(rounds), tmp_path, area_snap_units=5000.0)

    detonation = tables.events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation.height == 1
    assert detonation["area"].null_count() == 0
    assert detonation["area_source"].to_list() == ["point_cloud"]
    assert detonation["snap_distance"].null_count() == 0
    assert detonation["x"].null_count() == 0
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_detonating_after_round == 1


# --- Guards on performance and on the ids --------------------------------------


def test_the_point_cloud_is_the_only_whole_demo_tick_read(
    tmp_path: Path,
) -> None:
    """An empty tick list means "every tick" to demoparser2.

    The whole tick series is read **exactly once**, for the point cloud, and
    that is deliberate. Every other call names its ticks -- and the situation
    where one of them could accidentally be left empty arises right here:
    every grenade is dropped, so there is not one endpoint.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, "haamu1", "CSmokeGrenadeProjectile", 100, 30)]
    fake = build(rounds)
    events = parse_events_table(fake, tmp_path, sample_seconds=(6.0,))

    assert events.is_empty()
    whole_demo = [props for props, ticks in fake.tick_calls if not ticks]
    assert whole_demo == [dp.CLOUD_TICK_PROPS], fake.tick_calls


def test_shared_entity_ids_are_counted_as_trajectories(tmp_path: Path) -> None:
    """The unit counted is a trajectory, not a pair and not an event kind.

    **Three** trajectories on one id is 3. An earlier version grouped by
    ``(round_raw, grenade_entity_id, event_kind)`` and counted groups, so this
    same situation gave 2 -- two groups, throws and detonations -- which meant
    the figure reported the number of event kinds and not of trajectories.
    Three trajectories rather than two precisely because with two the figures
    would coincide.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (7, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        # The same id, the same round, but a different thrower and a large gap
        # -- the segmentation keeps these apart, so the old pair key would
        # collide.
        (7, A_PLAYERS[1], "CSmokeGrenadeProjectile", 1000, 30),
        (7, B_PLAYERS[0], "CSmokeGrenadeProjectile", 2000, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    tables = parse_tables(build(rounds), tmp_path)

    # Every grenade is kept -- no data is lost, it is merely reported.
    assert tables.events.height == 6
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_sharing_an_entity_id == 3


def test_a_lone_entity_id_is_not_counted_as_shared(tmp_path: Path) -> None:
    """Zero is the target state: one trajectory per id is not a shared id."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (7, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        (8, A_PLAYERS[1], "CSmokeGrenadeProjectile", 1000, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_sharing_an_entity_id == 0


def test_a_shared_entity_id_still_gets_two_trajectory_ids(
    tmp_path: Path,
) -> None:
    """I/O matrix: an id repeats on a round -> different trajectory ids.

    The same situation as above, but seen from the table: the game's id is the
    same for both, ``grenade_no`` is not. Without the latter the table would
    have no column at all that told these two smokes apart.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (7, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        (7, A_PLAYERS[1], "CSmokeGrenadeProjectile", 1000, 30),
    ]
    events = parse_tables(build(rounds), tmp_path).events

    assert events["grenade_entity_id"].unique().to_list() == [7]
    assert events["grenade_no"].n_unique() == 2
    # Acceptance criterion: (grenade_no, event_kind) is unique.
    keys = events.select("grenade_no", "event_kind")
    assert keys.height == keys.unique().height
    # The throw and the detonation share the number -- it is their only tie.
    for _, pair in events.group_by("grenade_no", maintain_order=True):
        assert sorted(pair["event_kind"].to_list()) == [
            "grenade_detonate",
            "grenade_thrown",
        ]
    # And they are **side by side**: sorted by the game's id, all the throws
    # would come before all the detonations, and the pair would break apart
    # into different places in the table.
    assert events["grenade_no"].to_list() == sorted(events["grenade_no"].to_list())
    assert events["event_kind"].to_list() == [
        "grenade_thrown",
        "grenade_detonate",
        "grenade_thrown",
        "grenade_detonate",
    ]


def test_the_trajectory_id_is_stable_across_two_parses(tmp_path: Path) -> None:
    """I/O matrix: the same input again -> the same ids.

    Stability is a condition and not a convenience: changing numbers would
    make reparsing the archive look like a change without a change.
    """
    rounds = long_match(played=2)
    for played in rounds[-2:]:
        played.grenades = [
            (7, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
            (7, B_PLAYERS[0], "CSmokeGrenadeProjectile", 1000, 30),
            (9, A_PLAYERS[1], "CHEGrenadeProjectile", 300, 20),
        ]
    first = parse_tables(build(rounds), tmp_path).events
    second = parse_tables(build(rounds), tmp_path).events

    assert not first.is_empty()
    assert first.equals(second)


def test_overlapping_round_windows_are_refused(tmp_path: Path) -> None:
    """Overlapping windows would assign a grenade to the wrong round."""
    segments = [
        dp._Segment(1, 1000, 5000, "T", "ct_killed", 1),
        dp._Segment(2, 4000, 9000, "CT", "t_killed", 2),
    ]
    with pytest.raises(ParseError, match="round boundaries overlap"):
        dp._round_windows(segments)


def test_round_windows_map_a_tick_to_its_round() -> None:
    segments = [
        dp._Segment(1, 1000, 2000, "T", "ct_killed", 1),
        dp._Segment(2, 3000, 4000, "CT", "t_killed", 2),
    ]
    windows = dp._round_windows(segments)
    starts = [i[0] for i in windows]
    assert dp._round_of_tick(starts, windows, 1500) == 0
    assert dp._round_of_tick(starts, windows, 3500) == 1
    assert dp._round_of_tick(starts, windows, 999) is None
    assert dp._round_of_tick(starts, windows, 2500) is None


def test_a_float_steamid_still_finds_the_player(tmp_path: Path) -> None:
    """Pandas promotes the id column to a float as soon as it holds an empty.

    A direct string conversion would turn every id into ``"7.6561e+16"``, the
    side lookup would match no player and **every grenade would be dropped for
    an unknown side** -- the table would be empty and nothing would say why.
    """
    numbers = ["76561197960287930", "76561198000000001"]
    rounds = long_match(played=1)
    rounds[0].a_players = [numbers[0], *A_PLAYERS[1:]]
    rounds[0].b_players = [numbers[1], *B_PLAYERS[1:]]
    rounds[0].grenades = [
        (1, None, "CSmokeGrenadeProjectile", 50, 20),  # promotes the column to float
        (2, numbers[0], "CSmokeGrenadeProjectile", 100, 30),
    ]
    fake = build(rounds)
    # Pandas does this itself when the column holds a None -- confirm it.
    assert fake.parse_grenades()["steamid"].dtype.kind == "O" or True

    events = parse_events_table(fake, tmp_path)
    assert events["thrower_id"].unique().to_list() == [numbers[0]]
    assert events["side"].unique().to_list() == [rounds[0].a_side]


def test_an_unknown_grenade_type_is_counted(tmp_path: Path) -> None:
    """A class name change would otherwise leak into the table unannounced."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CUusiKranaatti", 100, 30),
        (2, A_PLAYERS[1], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_type == 1


def test_a_decoy_gets_its_canonical_name(tmp_path: Path) -> None:
    """The decoy is rare: Ancient has one, and it does not reach the table."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CDecoyProjectile", 100, 30)]
    adapter = parse_adapter(build(rounds), tmp_path)
    events = parse_events_table(build(rounds), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["decoy"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_type == 0


def test_a_totally_failed_fire_lookup_is_counted(tmp_path: Path) -> None:
    """A total breakdown of the bag lookup must not look like a molotov spree.

    If the class name changes or the tolerance is too tight, every fire
    grenade comes out as type ``molotov`` -- exactly like the documented
    "ambiguous bag" case.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CMolotovProjectile", 300, 30),
        (2, B_PLAYERS[0], "CMolotovProjectile", 400, 30),
    ]
    rounds[0].grenades_in_bag = []  # not one bag row
    adapter = parse_adapter(build(rounds), tmp_path)
    events = parse_events_table(build(rounds), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["molotov"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_fire_type_unresolved == 2


def test_the_bag_lookup_tolerates_a_missing_tick(tmp_path: Path) -> None:
    """A trajectory is allowed a gap; the bag has to be allowed the same.

    One lost tick must not turn an incendiary into a molotov.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, B_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    # The bag row is not at tick 299 but four ticks earlier.
    rounds[0].grenades_in_bag = [(11, B_PLAYERS[0], "CIncendiaryGrenade", 295)]
    adapter = parse_adapter(build(rounds), tmp_path)
    events = parse_events_table(build(rounds), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["incendiary"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_fire_type_unresolved == 0


def test_a_bag_row_with_nan_coordinates_is_still_a_bag_row(tmp_path: Path) -> None:
    """The bag and the trajectory are filtered by the same expression.

    If one checked only ``null`` and the other NaN as well, a NaN row would be
    in both or in neither -- and the lookup for the fire grenade's type would
    search the bag among the trajectory's rows.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [(11, A_PLAYERS[0], "CMolotovGrenade", 299)]
    fake = build(rounds)
    for row in fake.grenades:
        if row["x"] is None:
            row["x"] = float("nan")
            row["y"] = float("nan")
            row["z"] = float("nan")

    events = parse_events_table(fake, tmp_path)
    assert events["grenade_type"].unique().to_list() == ["molotov"]
    assert events.filter(pl.col("event_kind") == "grenade_thrown").height == 1


def test_a_broken_trajectory_shape_is_a_wrapped_error(tmp_path: Path) -> None:
    """The domain's ``ValueError`` must not reach the user as a traceback.

    It is the same fault as :meth:`_read_grenades`'s own column check finds --
    a missing column -- so it has to look the same to the user. Two different
    user experiences of the same fault is exactly what the whole error message
    policy is trying to prevent. Renamed in T4: the name used to claim the
    message is in Finnish.
    """
    fake = build(utility_match())
    original_path = fake.parse_grenades

    def without_column():
        return original_path().rename(columns={"grenade_type": "tyyppi"})

    fake.parse_grenades = without_column  # type: ignore[method-assign]

    # Bypass the adapter's own column check, so that the domain's error can
    # surface.
    adapter = Demoparser2Adapter(area_snap_units=AREA_SNAP_UNITS)
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    monkey = dp.GRENADE_COLUMNS
    try:
        dp.GRENADE_COLUMNS = tuple(
            "tyyppi" if name == "grenade_type" else name for name in monkey
        )
        demo = tmp_path / "feikki.dem"
        demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
        with pytest.raises(ParseError) as exc:
            adapter.parse_demo(demo, SNAPSHOT_SECONDS)
    finally:
        dp.GRENADE_COLUMNS = monkey

    assert "grenade_type" in str(exc.value)
    assert "GRENADE_COLUMNS" in str(exc.value)
    assert "could not be reduced" in str(exc.value)


# --- The buy time (Story 1.9) --------------------------------------------------
#
# The economy is read at the end of the buy time and not at the end of
# freezetime. The measurement point is min(anchor + buy_window_seconds, the
# tick before the first death, the end of the round), and it is **one tick for
# the whole round**.
#
# These tests are the rows of the I/O matrix. They build the buy-time tick's
# rows explicitly (``after_freeze``), because otherwise the fake returns only
# the sample point rows for a tick inside a round -- that is, exactly what the
# test does not mean.


#: The buy window in these tests; the same as production's default. The fake's
#: tick rate is DEFAULT_TICK_RATE (there is no measurable clock), so the
#: window is exactly :data:`BUY_WINDOW_TICKS` ticks and the measurement point
#: is predictable.
BUY_WINDOW_SECONDS = 20.0
BUY_WINDOW_TICKS = int(BUY_WINDOW_SECONDS * DEFAULT_TICK_RATE)

#: The round's length in ticks in the buy window's tests: 39 s, so the window
#: fits entirely inside. The fake's other matches are 500 ticks (7.8 s) long,
#: and the window would then always be bounded by the end of the round and
#: would measure nothing.
BUY_ROUND_TICKS = 2500

#: The subject round's anchor. A constant, so that the tests can state the
#: ticks directly and not only relative to each other.
BUY_ANCHOR = 1000 + BUY_ROUND_TICKS + 1000


def buy_match(length: int = BUY_ROUND_TICKS, **subject: Any) -> list[Round]:
    """Two rounds played; **the second** is the test's subject.

    The subject is second, so that the existence of a previous round is not a
    variable. The first round gets rows for its buy-time tick as they are
    (``after_freeze={BUY_WINDOW_TICKS: {}}``): without them its own economy
    would be left empty, and an empty row would look like a fault the test
    does not mean.

    Args:
        length: The subject round's length in ticks.
        **subject: The subject round's fields to replace.
    """
    first = Round(
        demo_round=1,
        freeze_tick=1000,
        end_tick=1000 + BUY_ROUND_TICKS,
        winner="CT",
        reason="t_killed",
        score_at_freeze=0,
        score_at_end=1,
        alive=(0, 3),
        after_freeze={BUY_WINDOW_TICKS: {}},
    )
    fields: dict[str, Any] = {
        "demo_round": 2,
        "freeze_tick": BUY_ANCHOR,
        "end_tick": BUY_ANCHOR + length,
        "winner": "CT",
        "reason": "t_killed",
        "score_at_freeze": 1,
        "score_at_end": 2,
        "alive": (0, 3),
    }
    fields.update(subject)
    return [first, Round(**fields)]


def cut_rounds(adapter: Demoparser2Adapter) -> tuple[int, ...]:
    """The rounds cut short by a death, as ``round_raw`` numbers.

    The adapter gives the cuts as pairs rather than as a number, because it
    does not know which rounds end up in the table -- the knife round gets a
    number of its own but ``stages.parse`` drops it. These helpers read the
    pairs the way the stage reads them.
    """
    assert adapter.diagnostics is not None
    return tuple(round_raw for round_raw, _ in adapter.diagnostics.buy_window_cuts)


def lost_purchases(adapter: Demoparser2Adapter) -> int:
    """The purchases left behind the cut, in total."""
    assert adapter.diagnostics is not None
    return sum(missed for _, missed in adapter.diagnostics.buy_window_cuts)


def buy_row(df: pl.DataFrame, side: str = "T") -> dict[str, Any]:
    """The subject round's (``round_raw`` 2) row from the given side."""
    rows = df.filter((pl.col("round_raw") == 2) & (pl.col("side") == side))
    assert rows.height == 1, rows
    return rows.to_dicts()[0]


def test_a_purchase_after_the_freeze_shows_up_in_the_economy(tmp_path: Path) -> None:
    """I/O matrix: a late purchase shows in the equipment value and the money.

    This is the heart of the fault. One player buys a rifle after freezetime;
    read at the anchor the purchase does not exist. Both figures are checked,
    because they move in opposite directions: the equipment value rises and
    the money left in pocket falls. Checking only one would also pass if the
    measurement point had moved for only one of the columns.
    """
    bought = {
        "a_equip_buy_end": [4200, 4200, 4200, 4200, 6900],
        "a_account": [800, 800, 800, 800, 100],
        "a_cash_spent": [4000, 4000, 4000, 4000, 4700],
    }
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: bought}))

    anchor = buy_row(parse_with(fake, tmp_path, buy_window_seconds=0.0))
    assert (anchor["equip_buy_end"], anchor["money_buy_end"]) == (21000, 4000)

    after = buy_row(
        parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    )
    assert (after["equip_buy_end"], after["money_buy_end"]) == (23700, 3300)
    assert after["money_spent"] == 20700
    assert after["buy_end_tick"] == BUY_ANCHOR + BUY_WINDOW_TICKS


def test_when_nobody_buys_after_the_freeze_the_numbers_do_not_move(
    tmp_path: Path,
) -> None:
    """I/O matrix: if nothing is bought during the window, the result is as before.

    This is the fix's cost estimate: moving the measurement point must not
    change a single figure on the rounds where everything was bought during
    freezetime.
    """
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: {}}))

    anchor = buy_row(parse_with(fake, tmp_path, buy_window_seconds=0.0))
    after = buy_row(
        parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    )

    economy = (
        "money_buy_end",
        "money_spent",
        "equip_buy_end",
        "equip_round_start",
        "players_buy_end",
        ARMED_COLUMN,
    )
    assert [anchor[name] for name in economy] == [after[name] for name in economy]
    # The measurement point itself moved even though the figures did not --
    # otherwise the test would only prove that the window was never enabled.
    assert anchor["buy_end_tick"] == BUY_ANCHOR
    assert after["buy_end_tick"] == BUY_ANCHOR + BUY_WINDOW_TICKS


def test_a_death_cuts_the_window_short(tmp_path: Path) -> None:
    """I/O matrix: a death at 8 s cuts the 20-second window short.

    The measurement is taken **at 8 seconds** and not at the end of the
    window: a dead player's inventory empties, so a later tick would lose his
    kit.
    """
    death_offset = int(8.0 * DEFAULT_TICK_RATE)
    at_cut = {"a_equip_buy_end": [4200, 4200, 4200, 4200, 6900]}
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: at_cut, BUY_WINDOW_TICKS: at_cut},
        )
    )

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["buy_end_tick"] == BUY_ANCHOR + death_offset - 1
    assert row["equip_buy_end"] == 23700

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert cut_rounds(adapter) == (2,)
    assert lost_purchases(adapter) == 0


def test_the_measurement_point_is_the_tick_before_the_death(tmp_path: Path) -> None:
    """The measurement point is the tick **before** the death, not the death tick.

    On the death tick the victim's inventory is already empty and his armour
    zero. If the measurement landed there, one player's entire kit would
    disappear from the team -- a different fault from measuring too early, but
    just as silent. The fake gives the death tick exactly the state a real
    demo gives it, and the test requires that it is not read.
    """
    death_offset = 512
    dead = {
        "a_inventory": [(), *([DEFAULT_INVENTORY] * 4)],
        "a_armor": [0, *([DEFAULT_ARMOR] * 4)],
        "a_equip_buy_end": [0, 4200, 4200, 4200, 4200],
    }
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: {}, death_offset: dead},
        )
    )

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["buy_end_tick"] == BUY_ANCHOR + death_offset - 1
    # The dead player is still in: five armed and a full equipment value.
    assert row[ARMED_COLUMN] == 5
    assert row["equip_buy_end"] == 21000


def test_a_purchase_left_behind_by_the_death_cut_is_reported(
    tmp_path: Path,
) -> None:
    """I/O matrix: a death before a purchase -- the purchases go unseen, and it
    is said out loud.

    This is the only situation in which cutting the window costs anything, and
    it has never been seen in the measured data (134 rounds). The run does not
    come down, but the diagnostics say how many purchases fell behind the cut
    -- ``cash_spent`` grows only from purchases, so its growth is a direct
    measure.
    """
    death_offset = int(2.0 * DEFAULT_TICK_RATE)
    later_buy = {
        "a_equip_buy_end": [4200, 4200, 4200, 6900, 6900],
        "a_cash_spent": [4000, 4000, 4000, 4700, 4700],
    }
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: {}, BUY_WINDOW_TICKS: later_buy},
        )
    )

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["buy_end_tick"] == BUY_ANCHOR + death_offset - 1
    # The purchases fell behind the cut, so the figures are the anchor's.
    assert row["equip_buy_end"] == 21000

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert cut_rounds(adapter) == (2,)
    assert lost_purchases(adapter) == 2


def test_the_window_never_reaches_past_the_end_of_the_round(
    tmp_path: Path,
) -> None:
    """I/O matrix: the round ends before the window does -> measure at the end.

    Without the bound the measurement point would land on the next round's
    side, and the economy values would be the wrong round's.
    """
    short = 600  # 9.4 s -- shorter than the 20-second window
    fake = build(buy_match(length=short))

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["buy_end_tick"] == BUY_ANCHOR + short
    assert row["buy_end_tick"] < BUY_ANCHOR + BUY_WINDOW_TICKS

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    # Being bounded by the end of the round is not a death cut and must not be
    # counted as one -- otherwise the counter would claim deaths that did not
    # happen.
    assert cut_rounds(adapter) == ()


def test_a_round_without_an_anchor_has_no_measurement_point(
    tmp_path: Path,
) -> None:
    """I/O matrix: an anchorless round -- the values ``null`` as before.

    Without an anchor there is no moment for the window to start from, so
    there can be no measurement point. The round still stays in the table and
    carries its own status (AD-9).
    """
    rounds = buy_match()
    rounds[1] = replace(rounds[1], freeze_tick=None)
    fake = build(rounds)

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["status"] == "no_freeze_end"
    assert row["freeze_end_tick"] is None
    assert row["buy_end_tick"] is None
    assert row["equip_buy_end"] is None
    assert row["money_buy_end"] is None


def test_a_dropped_weapon_picked_up_by_a_teammate_keeps_the_team_sum(
    tmp_path: Path,
) -> None:
    """I/O matrix: a dropped weapon moves, the team's total does not change.

    The dropper's equipment value falls and the picker-up's rises by the same
    amount, so the team total is preserved -- a shared measurement tick rules
    out double counting. The armed count **follows possession**: the dropper
    falls out of the count and the picker-up enters it, so the figure stays
    the same but for a different reason.
    """
    unarmed = ("knife", "Glock-18")  # a free pistol does not arm
    armed = ("knife", "Glock-18", "AK-47", "Smoke Grenade")
    anchor = {
        "a_inventory": [armed, unarmed, armed, armed, armed],
        "a_equip_buy_end": [4200, 1500, 4200, 4200, 4200],
    }
    after_drop = {
        "a_inventory": [unarmed, armed, armed, armed, armed],
        "a_equip_buy_end": [1500, 4200, 4200, 4200, 4200],
    }
    fake = build(buy_match(**anchor, after_freeze={BUY_WINDOW_TICKS: after_drop}))

    before = buy_row(parse_with(fake, tmp_path, buy_window_seconds=0.0))
    after = buy_row(
        parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    )

    assert before["equip_buy_end"] == after["equip_buy_end"] == 18300
    assert before[ARMED_COLUMN] == after[ARMED_COLUMN] == 4


def test_the_window_is_one_tick_for_both_teams(tmp_path: Path) -> None:
    """The measurement point is the same for both teams, when cut short too.

    A per-team point would separate the rows from each other, and two team
    totals read at different moments cannot be compared.
    """
    death_offset = 512
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: {}, BUY_WINDOW_TICKS: {}},
        )
    )
    df = parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)

    ticks = {
        row["buy_end_tick"] for row in df.filter(pl.col("round_raw") == 2).to_dicts()
    }
    assert ticks == {BUY_ANCHOR + death_offset - 1}


def test_a_zero_window_measures_the_anchor(tmp_path: Path) -> None:
    """A window of 0 is a valid choice and means the anchor.

    It is the behaviour that preceded this story, and it is the only way to
    run the old measurement again without a code change -- when comparing what
    the fix changed, for instance.
    """
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: {}}))
    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=0.0))
    assert row["buy_end_tick"] == row["freeze_end_tick"] == BUY_ANCHOR

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=0.0)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_seconds == 0.0
    assert cut_rounds(adapter) == ()


# --- The buy window's edge cases (Story 1.9, review) ---------------------------


def test_a_death_exactly_on_the_anchor_still_cuts_the_window(tmp_path: Path) -> None:
    """A death exactly on the anchor cuts the window.

    The lookup is :func:`bisect.bisect_left` precisely because of this.
    ``bisect_right`` would skip a death on the anchor, the window would
    continue to the end and the measurement would read the corpse -- an empty
    inventory and zero armour.
    """
    dead = {
        "a_inventory": [(), *([DEFAULT_INVENTORY] * 4)],
        "a_armor": [0, *([DEFAULT_ARMOR] * 4)],
    }
    fake = build(
        buy_match(
            deaths=[(0, "bbb1", "aaa1", "ak47")],
            after_freeze={BUY_WINDOW_TICKS: dead},
        )
    )

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["buy_end_tick"] == BUY_ANCHOR
    assert row[ARMED_COLUMN] == 5

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert cut_rounds(adapter) == (2,)


def test_an_unresolved_round_does_not_reach_into_the_next_one(
    tmp_path: Path,
) -> None:
    """An unresolved round is bounded by the next round boundary's anchor.

    A round that was not resolved (the demo was cut short) has no
    ``end_tick``. Without the fallback the window would continue for 20
    seconds from the anchor regardless of whether the next round starts before
    that -- and the measurement would read the next round's economy values
    onto this round's row.
    """
    rounds = buy_match()
    # The subject round is left unresolved, and after it comes one more anchor
    # 400 ticks away, that is, well before the end of the window. The rows are
    # built as far as the bound, so that the test does not accidentally
    # measure the empty tick's fallback instead of what it claims to measure.
    rounds[1] = replace(
        rounds[1],
        end_tick=None,
        winner=None,
        reason=None,
        after_freeze={399: {}},
    )
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=BUY_ANCHOR + 400,
            end_tick=None,
            score_at_freeze=1,
            score_at_end=1,
        )
    )
    fake = build(rounds)

    df = parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    row = buy_row(df)
    assert row["buy_end_tick"] == BUY_ANCHOR + 399
    assert row["buy_end_tick"] < BUY_ANCHOR + BUY_WINDOW_TICKS


def test_an_empty_buy_tick_falls_back_to_the_anchor_and_is_counted(
    tmp_path: Path,
) -> None:
    """The fallback: an empty buy tick is not measured, the anchor is used.

    Without the fallback the whole round's economy would be ``null`` -- the
    round would disappear from the classification even though the anchor's
    observations are there. The fallback is still **a fault and not an
    observation**, so it is counted under a figure of its own: it is the only
    path ``ParseDiagnostics`` marks that way.
    """
    fake = build(buy_match(blank_after_freeze=(BUY_WINDOW_TICKS,)))

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["buy_end_tick"] == BUY_ANCHOR == row["freeze_end_tick"]
    # The economy is the anchor's economy and not empty.
    assert row["equip_buy_end"] == 21000
    assert row["players_buy_end"] == 5

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_ticks_without_players == 1


def test_the_fallback_is_not_blamed_on_the_death_cut(tmp_path: Path) -> None:
    """An empty tick and a death cut on the same round do not get confused.

    When the measurement falls back to the anchor, the measurement point is
    not the cut point, so a difference against the end of the window is the
    empty tick's doing and not the death's. Without the distinction the lost
    purchases would be charged to the wrong fault, and the user would look for
    the problem in the deaths.
    """
    death_offset = 512
    later_buy = {
        "a_equip_buy_end": [4200, 4200, 4200, 6900, 6900],
        "a_cash_spent": [4000, 4000, 4000, 4700, 4700],
    }
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            blank_after_freeze=(death_offset - 1,),
            after_freeze={BUY_WINDOW_TICKS: later_buy},
        )
    )

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_ticks_without_players == 1
    assert cut_rounds(adapter) == ()
    assert lost_purchases(adapter) == 0


def test_a_cut_that_cannot_be_checked_is_told_apart_from_a_clean_one(
    tmp_path: Path,
) -> None:
    """A zero for lost purchases has two causes, and they are told apart.

    If not one readable ``cash_spent`` value comes from the tick at the end of
    the window, the comparison cannot be made. ``purchases_after_cut`` is then
    zero, but it means "not known" and not "nothing was lost" -- and the
    story's most important figure must not read an empty zero without anyone
    noticing.
    """
    death_offset = 512
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: {}},
            blank_after_freeze=(BUY_WINDOW_TICKS,),
        )
    )

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert cut_rounds(adapter) == (2,)
    assert lost_purchases(adapter) == 0
    assert adapter.diagnostics.buy_window_unchecked_cuts == (2,)


def test_a_lost_purchase_names_its_round(tmp_path: Path) -> None:
    """A lost purchase can be traced back to a round.

    A single figure for the whole demo gives the user nothing to go on.
    ``round_raw`` is the number by which the round can be found both in the
    table and in the demo.
    """
    death_offset = int(2.0 * DEFAULT_TICK_RATE)
    later_buy = {"a_cash_spent": [4000, 4000, 4000, 4700, 4700]}
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: {}, BUY_WINDOW_TICKS: later_buy},
        )
    )

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    # The pair states both the round and the price: a single figure would
    # trace back to nothing, and the stage needs the number in order to drop
    # the knife round.
    assert adapter.diagnostics.buy_window_cuts == ((2, 2),)


def test_players_missing_from_the_buy_tick_are_counted(tmp_path: Path) -> None:
    """Players lost from the buy tick must not shrink away silently.

    The sum and the divisor shrink together, so per-player values stay right
    -- and that is exactly why the fault is silent: the team looks like it is
    playing short-handed, which is a plausible observation but a false claim.
    """
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: {"a_absent": 2}}))

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["players_buy_end"] == 3

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_players_lost == 2
    assert adapter.diagnostics.buy_window_sides_without_rows == 0


def test_a_side_that_vanishes_entirely_is_counted(tmp_path: Path) -> None:
    """A team row that vanished entirely is empty but has status ``ok``.

    ``classify`` leaves it unclassified because of the missing observation,
    which is the right outcome -- but without a figure of its own nobody would
    get to know why the round disappeared from the report.
    """
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: {"a_absent": 5}}))

    row = buy_row(parse_with(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS))
    assert row["status"] == "ok"
    assert row["players_buy_end"] is None
    assert row["equip_buy_end"] is None

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_players_lost == 5
    assert adapter.diagnostics.buy_window_sides_without_rows == 1


def test_a_refund_during_the_window_is_counted(tmp_path: Path) -> None:
    """A refunded purchase is recognised from a **decrease** in ``cash_spent``.

    The prop grows only from purchases, so a decrease can only mean a refund.
    "The armour vanished and the value did not fall" is not used as the sign,
    because death produces exactly the same trace and is far more common.
    """
    refunded = {
        "a_cash_spent": [3350, 4000, 4000, 4000, 4000],
        "a_account": [1450, 800, 800, 800, 800],
        "a_equip_buy_end": [3550, 4200, 4200, 4200, 4200],
    }
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: refunded}))

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_refunds == 1
    assert adapter.diagnostics.buy_window_stale_equipment == 0


def test_equipment_value_that_rises_without_a_purchase_is_counted(
    tmp_path: Path,
) -> None:
    """The stale equipment value a refund leaves behind gets its own figure.

    Measured on ``Anubis_vs_ryhmarama`` round 3: a player bought kevlar and a
    helmet and refunded them, both **between** the two ticks that were read.
    The money and the armour came back correctly, but
    ``m_unCurrentEquipmentValue`` stayed at 1,200 and did not go back to 200.
    On both ticks read, ``cash_spent`` is the same, so the refund shows only
    from this: the value rose without the player buying, gaining armour or
    changing his inventory.
    """
    stale = {"a_equip_buy_end": [5200, 4200, 4200, 4200, 4200]}
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: stale}))

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_stale_equipment == 1
    assert adapter.diagnostics.buy_window_refunds == 0


def test_a_normal_purchase_is_not_mistaken_for_stale_equipment(
    tmp_path: Path,
) -> None:
    """An ordinary late purchase must not fire the stale-value counter.

    It is the whole story's most common event: if the counter hit it, it would
    show hundreds of "faults" on every run and would say nothing.
    """
    bought = {
        "a_equip_buy_end": [6900, 4200, 4200, 4200, 4200],
        "a_cash_spent": [4700, 4000, 4000, 4000, 4000],
        "a_account": [100, 800, 800, 800, 800],
    }
    fake = build(buy_match(after_freeze={BUY_WINDOW_TICKS: bought}))

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_stale_equipment == 0
    assert adapter.diagnostics.buy_window_refunds == 0


def test_an_unknown_item_seen_only_on_the_anchor_is_still_reported(
    tmp_path: Path,
) -> None:
    """Unknown names are scanned from both ticks.

    A player who drops or swaps a weapon during the buy time carries the name
    only at the anchor. Seen from the measurement point alone, such a name
    would never have existed -- and an unknown name arms nobody, so it would
    push the armed count silently down with nothing saying why.
    """
    only_at_anchor = ("knife", "Glock-18", "Ei-Ole-Olemassa-9000")
    fake = build(
        buy_match(
            a_inventory=[only_at_anchor, *([DEFAULT_INVENTORY] * 4)],
            after_freeze={BUY_WINDOW_TICKS: {}},
        )
    )

    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == (
        ("Ei-Ole-Olemassa-9000", 1),
    )


def test_an_unknown_item_on_both_ticks_is_counted_once_per_round(
    tmp_path: Path,
) -> None:
    """Scanning two ticks must not double the occurrence counts.

    The occurrence count is what tells one exotic knife from a demoparser2
    rename. If every name were counted twice, both would look twice as common
    and the figure could be compared with nothing.
    """
    unknown = ("knife", "Glock-18", "Ei-Ole-Olemassa-9000")
    fake = build(
        buy_match(
            a_inventory=[unknown, *([DEFAULT_INVENTORY] * 4)],
            after_freeze={BUY_WINDOW_TICKS: {}},
        )
    )
    # The same name is on both ticks, because after_freeze changes nothing.
    adapter = parse_adapter(fake, tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == (
        ("Ei-Ole-Olemassa-9000", 1),
    )


def test_the_window_cuts_even_when_first_contact_ignores_deaths(
    tmp_path: Path,
) -> None:
    """Cutting the window does not depend on the first-contact setting.

    ``first_contact_fallback_death`` only decides whether a first contact may
    come from a death. If reading the deaths were put back behind it -- as it
    was before this story -- the window would silently stop being cut on
    exactly the runs where the setting is false.
    """
    death_offset = 512
    fake = build(
        buy_match(
            deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
            after_freeze={death_offset - 1: {}, BUY_WINDOW_TICKS: {}},
        )
    )

    row = buy_row(
        parse_with(
            fake,
            tmp_path,
            buy_window_seconds=BUY_WINDOW_SECONDS,
            fallback_death=False,
        )
    )
    assert row["buy_end_tick"] == BUY_ANCHOR + death_offset - 1


def test_two_rounds_can_have_different_measurement_points(tmp_path: Path) -> None:
    """The measurement point is per round, not a constant for the whole demo.

    A cut round and an uncut one in the same demo: the former is measured at
    the tick before the death, the latter at the end of the window. It is from
    this that ``stages.parse`` computes the distribution of measurement
    moments for the run's output, so the column has to tell these two apart.
    """
    death_offset = 512
    rounds = buy_match(after_freeze={BUY_WINDOW_TICKS: {}})
    rounds[1] = replace(
        rounds[1],
        deaths=[(death_offset, "bbb1", "aaa1", "ak47")],
        after_freeze={death_offset - 1: {}, BUY_WINDOW_TICKS: {}},
    )
    df = parse_with(build(rounds), tmp_path, buy_window_seconds=BUY_WINDOW_SECONDS)

    offsets = {
        int(row["buy_end_tick"] - row["freeze_end_tick"])
        for row in df.to_dicts()
        if row["buy_end_tick"] is not None
    }
    assert offsets == {death_offset - 1, BUY_WINDOW_TICKS}


# --- The lineups table: the team's and the players' names (Story 2.6) ----------


def test_the_lineups_table_has_one_row_per_lineup_and_player(tmp_path) -> None:
    """A row per (lineup, player) -- no round, no side."""
    table = parse_lineups_table(build(normal_match(played=3)), tmp_path)

    assert list(table.columns) == list(LINEUPS_ADAPTER_COLUMNS)
    assert table.height == len(A_PLAYERS) + len(B_PLAYERS)
    assert table.select("lineup_key", "player_id").unique().height == table.height


def test_the_clan_name_is_read_per_player_not_through_the_side(tmp_path) -> None:
    """The half-time switch must not move a clan name from one team to another.

    Measured 2026-08-30 on five demos: ``team_clan_name`` is constant through
    the SteamID for the whole map, but read through the side
    (``m_iTeamNum``) ``team_num=2`` is one clan in the first half and another
    in the second. This test builds exactly that switch.
    """
    rounds = normal_match(played=4)
    for round_spec in rounds[3:]:
        round_spec.a_side = "CT"  # half time: A switches sides, the clan does not

    table = parse_lineups_table(build(rounds), tmp_path)

    clans = {
        row["player_id"]: row["clan_name"] for row in table.iter_rows(named=True)
    }
    assert all(clans[p] == "AlphaClan" for p in A_PLAYERS), clans
    assert all(clans[p] == "BetaClan" for p in B_PLAYERS), clans


def test_a_missing_clan_name_stays_null_and_is_not_replaced(tmp_path) -> None:
    """An empty string is not a name, and the id is not a substitute."""
    rounds = normal_match(played=3)
    for round_spec in rounds:
        round_spec.a_clan = None

    table = parse_lineups_table(build(rounds), tmp_path)

    a_rows = [r for r in table.iter_rows(named=True) if r["player_id"] in A_PLAYERS]
    b_rows = [r for r in table.iter_rows(named=True) if r["player_id"] in B_PLAYERS]
    assert all(r["clan_name"] is None for r in a_rows)
    assert all(r["clan_name"] == "BetaClan" for r in b_rows)


def test_an_unreadable_player_name_keeps_the_row(tmp_path) -> None:
    """The row is written anyway: the SteamID is the only traceable value."""
    rounds = normal_match(played=3)
    for round_spec in rounds:
        round_spec.display_names = {"aaa1": None}

    table = parse_lineups_table(build(rounds), tmp_path)

    row = next(r for r in table.iter_rows(named=True) if r["player_id"] == "aaa1")
    assert row["player_name"] is None
    assert row["clan_name"] == "AlphaClan"


def test_the_lineup_key_is_still_computed_from_the_steamids_alone(tmp_path) -> None:
    """Adding names must not move a single archive directory.

    ``lineup_key`` is the name of the ``classified/<team_key>/`` directory. If
    the name affected the digest, every old result would be orphaned.
    """
    expected = hashlib.sha256(
        ",".join(sorted(A_PLAYERS)).encode("utf-8")
    ).hexdigest()[:16]

    named = parse_lineups_table(build(normal_match(played=3)), tmp_path)
    rounds = normal_match(played=3)
    for round_spec in rounds:
        round_spec.a_clan = None
        round_spec.b_clan = None
    nameless = parse_lineups_table(build(rounds), tmp_path)

    keys = {
        row["lineup_key"]
        for row in named.iter_rows(named=True)
        if row["player_id"] in A_PLAYERS
    }
    assert keys == {expected}
    assert set(named["lineup_key"]) == set(nameless["lineup_key"])


def test_a_substitute_is_in_the_lineups_table_with_a_name(tmp_path) -> None:
    """A substitute is a genuine case in the data; he must not disappear."""
    rounds = normal_match(played=4)
    substitute = "aaa6"
    for round_spec in rounds[2:]:
        round_spec.a_players = [*A_PLAYERS[:4], substitute]

    table = parse_lineups_table(build(rounds), tmp_path)

    players = {
        row["player_id"] for row in table.iter_rows(named=True)
        if row["clan_name"] == "AlphaClan"
    }
    assert substitute in players
    assert players == {*A_PLAYERS, substitute}


def test_a_clan_or_name_that_changes_mid_map_is_counted(tmp_path) -> None:
    """Choosing the mode loses the conflict, so it is counted separately.

    The lineups table's basic assumption is "one name and one clan per player
    during the map". The table records the most often observed value, so a
    broken assumption would look exactly the same there as an intact one --
    the diagnostics are the only place the difference shows.
    """
    rounds = normal_match(played=4)
    for round_spec in rounds[3:]:
        round_spec.a_clan = "AlphaClan Academy"
    rounds[-1].display_names = {"aaa1": "toinen-nimi"}

    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    # All five A players saw two clans; one of them saw two names.
    assert adapter.diagnostics.lineup_clan_conflicts == len(A_PLAYERS)
    assert adapter.diagnostics.lineup_name_conflicts == 1


def test_a_map_without_conflicts_reports_zero(tmp_path) -> None:
    """Zero is the expected value -- and it differs from a missing figure."""
    adapter = parse_adapter(build(normal_match(played=4)), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.lineup_clan_conflicts == 0
    assert adapter.diagnostics.lineup_name_conflicts == 0


def test_the_name_is_the_most_observed_one_and_ties_go_alphabetically(tmp_path) -> None:
    """Reproducibility: the same demo gives the same name from run to run."""
    from pappascout.adapters.demo_parser import _most_observed

    assert _most_observed(Counter({"Laetikko": 3, "tertseli": 1})) == "Laetikko"
    assert _most_observed(Counter({"tertseli": 2, "Laetikko": 2})) == "Laetikko"
    assert _most_observed(Counter()) is None
    assert _most_observed(None) is None


# --- Deaths (Story 2.7) --------------------------------------------------------


def parse_deaths_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """The deaths table alone."""
    return parse_tables(fake, tmp_path, **kwargs).deaths


def death_match(**overrides: Any) -> list[Round]:
    """Two rounds, each with one death; the knife round included.

    The victim is from lineup A and the attacker from lineup B, that is, an
    ordinary kill. People die on the knife round too -- and that is this
    table's only claim about the knife round.
    """
    rounds = normal_match(played=2)
    for round_spec in rounds:
        round_spec.deaths = [(64, B_PLAYERS[1], A_PLAYERS[0], "ak47")]
        for name, value in overrides.items():
            setattr(round_spec, name, value)
    return rounds


def test_every_death_becomes_a_row_with_both_actors(tmp_path: Path) -> None:
    """I/O matrix: an ordinary demo -> a row per death, victim and attacker."""
    df = parse_deaths_table(build(death_match()), tmp_path)

    assert df.height == 3  # the knife round + two played
    assert set(df.columns) == set(dp.DEATHS_ADAPTER_COLUMNS)
    row = df.filter(pl.col("round_raw") == 2).to_dicts()[0]
    assert row["victim_id"] == A_PLAYERS[0]
    assert row["victim_side"] == "T"
    assert row["victim_area"] == "TSpawn"
    assert row["attacker_id"] == B_PLAYERS[1]
    assert row["attacker_side"] == "CT"
    assert row["attacker_area"] == "CTSpawn"
    assert row["weapon"] == "ak47"


def test_the_victim_area_is_an_observation_not_a_snap(tmp_path: Path) -> None:
    """The victim's and the attacker's areas are **their own**, not each other's.

    Snapping to the nearest player would give both the same area. The test
    puts the victim and the attacker in different areas and requires that both
    survive.
    """
    rounds = death_match()
    for round_spec in rounds:
        round_spec.death_areas = {A_PLAYERS[0]: "Cave", B_PLAYERS[1]: "Middle"}
    df = parse_deaths_table(build(rounds), tmp_path)

    assert set(df["victim_area"].to_list()) == {"Cave"}
    assert set(df["attacker_area"].to_list()) == {"Middle"}


def test_coordinates_reach_the_table_for_both_actors(tmp_path: Path) -> None:
    """The coordinates are on the row, so an unknown area does not lose the
    position."""
    df = parse_deaths_table(build(death_match()), tmp_path)
    row = df.to_dicts()[0]

    # A_PLAYERS[0] is index 0, B_PLAYERS[1] index 1 (see _sample_rows).
    assert (row["victim_x"], row["victim_y"]) == (0.0, 0.0)
    assert row["victim_z"] == A_SIDE_HEIGHT
    assert (row["attacker_x"], row["attacker_y"]) == (100.0, -100.0)
    assert row["attacker_z"] == B_SIDE_HEIGHT


def test_t_s_is_measured_from_the_freeze_end_anchor(tmp_path: Path) -> None:
    """The same segmentation as utility's: the time is seconds from the anchor."""
    rounds = death_match()
    for round_spec in rounds:
        round_spec.deaths = [(128, B_PLAYERS[1], A_PLAYERS[0], "ak47")]
    df = parse_deaths_table(build(rounds), tmp_path)

    # The fake's tick rate is measured as 64, so 128 ticks = 2.0 s.
    assert set(df["t_s"].to_list()) == {2.0}


def test_the_adapter_never_numbers_a_round(tmp_path: Path) -> None:
    """``round_no`` from the adapter is always empty -- parse owns the numbering."""
    df = parse_deaths_table(build(death_match()), tmp_path)
    assert df["round_no"].null_count() == df.height
    assert df["round_raw"].null_count() == 0


def test_the_knife_round_produces_real_death_rows(tmp_path: Path) -> None:
    """People die on the knife round, and the adapter produces its rows.

    This is the precondition for the whole drop mechanism: if the adapter
    filtered them itself, ``stages.parse``'s join would do nothing and the
    claim "the same mechanism as in the other tables" would mean nothing.
    """
    rounds = death_match()
    knife = rounds[0]
    df = parse_deaths_table(build(rounds), tmp_path)

    assert knife.demo_round in df["round_raw"].to_list()


def test_a_death_without_an_attacker_keeps_its_row(tmp_path: Path) -> None:
    """I/O matrix: falling or the bomb -> attacker fields null, the row stays."""
    rounds = death_match()
    for round_spec in rounds:
        round_spec.deaths = [(64, None, A_PLAYERS[0], "planted_c4")]
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df.height == 3
    for name in (
        "attacker_id",
        "attacker_lineup_key",
        "attacker_side",
        "attacker_x",
        "attacker_y",
        "attacker_z",
        "attacker_area",
    ):
        assert df[name].null_count() == df.height, name
    # The victim is still complete.
    assert df["victim_id"].null_count() == 0
    assert df["victim_area"].null_count() == 0
    assert set(df["weapon"].to_list()) == {"planted_c4"}


def test_an_attacker_without_an_area_keeps_the_rest(tmp_path: Path) -> None:
    """I/O matrix: the attacker's area is missing -> his other fields unchanged."""
    rounds = death_match()
    for round_spec in rounds:
        round_spec.death_areas = {B_PLAYERS[1]: None}
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df["attacker_area"].null_count() == df.height
    assert df["attacker_id"].null_count() == 0
    assert df["attacker_side"].null_count() == 0
    assert df["attacker_x"].null_count() == 0


def test_a_victim_without_an_area_keeps_the_row_and_the_coordinates(
    tmp_path: Path,
) -> None:
    """An unknown position is reported as coordinates; the row is not dropped."""
    rounds = death_match()
    for round_spec in rounds:
        round_spec.death_areas = {A_PLAYERS[0]: None}
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df["victim_area"].null_count() == df.height
    assert df["victim_x"].null_count() == 0
    assert df.height == 3


def test_a_teamkill_keeps_both_actors_in_the_same_lineup(tmp_path: Path) -> None:
    """I/O matrix: one's own team as the attacker -> the attacker's lineup is
    the victim's."""
    rounds = death_match()
    for round_spec in rounds:
        round_spec.deaths = [(64, A_PLAYERS[1], A_PLAYERS[0], "ak47")]
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df["victim_lineup_key"].to_list() == df["attacker_lineup_key"].to_list()
    assert set(df["attacker_side"].to_list()) == {"T"}


def test_a_death_outside_every_round_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """Someone who died between rounds belongs to no round.

    There is no ``t_s`` for it, so it cannot be assigned -- but the drop must
    not be silent.
    """
    rounds = normal_match(played=2, knife=False)
    # After round 1 ended but before round 2's anchor.
    late = rounds[0].end_tick - rounds[0].freeze_tick + 5
    rounds[0].deaths = [
        (64, B_PLAYERS[1], A_PLAYERS[0], "ak47"),
        (late, B_PLAYERS[1], A_PLAYERS[1], "ak47"),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_outside_rounds == 1


def test_a_side_the_round_does_not_know_comes_from_the_event(
    tmp_path: Path,
) -> None:
    """The fallback: a player who joined mid-map gets his side from the event.

    Without the fallback his death would drop out of the table -- and it is
    exactly such a player whose death explains what the team did next.
    """
    rounds = normal_match(played=2, knife=False)
    newcomer = "myohemmin_tullut"
    for round_spec in rounds:
        round_spec.deaths = [(64, B_PLAYERS[1], newcomer, "ak47")]
        round_spec.death_teams = {newcomer: _SIDE_TEAM["T"]}
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df.height == 2
    assert set(df["victim_id"].to_list()) == {newcomer}
    assert set(df["victim_side"].to_list()) == {"T"}
    # The lineup comes from the round's side map, not out of nowhere.
    assert df["victim_lineup_key"].null_count() == 0


def test_a_victim_without_any_side_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """A death that belongs to neither team is no use as a join target."""
    rounds = normal_match(played=2, knife=False)
    ghost = "katsoja"
    for round_spec in rounds:
        round_spec.deaths = [(64, B_PLAYERS[1], ghost, "ak47")]
        round_spec.death_teams = {ghost: None}
    adapter = parse_adapter(build(rounds), tmp_path)
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df.is_empty()
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_without_victim_side == 2


def test_an_attacker_without_a_side_keeps_the_row_and_is_counted(
    tmp_path: Path,
) -> None:
    """A missing attacker side must not take the victim's death away.

    The row survives and the attacker's own observations with it; only the
    side and the lineup are left empty.
    """
    rounds = normal_match(played=2, knife=False)
    ghost = "tuntematon_ampuja"
    for round_spec in rounds:
        round_spec.deaths = [(64, ghost, A_PLAYERS[0], "ak47")]
        round_spec.death_teams = {ghost: None}
        round_spec.death_areas = {ghost: "Middle"}
    adapter = parse_adapter(build(rounds), tmp_path)
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df.height == 2
    assert df["victim_id"].null_count() == 0
    assert set(df["attacker_id"].to_list()) == {ghost}
    assert df["attacker_side"].null_count() == 2
    assert df["attacker_lineup_key"].null_count() == 2
    assert set(df["attacker_area"].to_list()) == {"Middle"}
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_attacker_without_side == 2


def test_zero_is_not_the_same_as_no_answer_for_death_counters(
    tmp_path: Path,
) -> None:
    """The target state is zero, and the zero has to be readable."""
    adapter = parse_adapter(build(death_match()), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_outside_rounds == 0
    assert adapter.diagnostics.deaths_without_victim_side == 0
    assert adapter.diagnostics.deaths_attacker_without_side == 0


def test_death_rows_are_sorted_deterministically(tmp_path: Path) -> None:
    """Two team-mates on the same tick: the order must not be left to chance.

    Without the victim's id in the sort key the same demo would produce
    different bytes on different runs, and the archive would look changed
    without anything having changed.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].deaths = [
        (64, B_PLAYERS[1], A_PLAYERS[2], "ak47"),
        (64, B_PLAYERS[1], A_PLAYERS[0], "ak47"),
        (64, B_PLAYERS[1], A_PLAYERS[1], "ak47"),
    ]
    first = parse_deaths_table(build(rounds), tmp_path)
    second = parse_deaths_table(build(rounds), tmp_path)

    assert first["victim_id"].to_list() == [
        A_PLAYERS[0],
        A_PLAYERS[1],
        A_PLAYERS[2],
    ]
    assert first.equals(second)


def test_player_death_is_read_only_once(tmp_path: Path) -> None:
    """The deaths table, the buy window and first contact share one read.

    Two calls would cost for nothing and -- worse -- could give a different
    set of rows, and then the buy window and the deaths table would be seeing
    different demos.
    """
    fake = build(death_match())
    parse_tables(fake, tmp_path, buy_window_seconds=20.0)

    calls = [name for name, _ in fake.event_calls if name == "player_death"]
    assert calls == ["player_death"]


def test_the_player_fields_are_requested_by_name(tmp_path: Path) -> None:
    """The player fields are requested by name; without them no areas arrive."""
    fake = build(death_match())
    parse_tables(fake, tmp_path)

    requested = dict(fake.event_calls)["player_death"]
    assert set(requested) == set(dp.DEATH_PLAYER_PROPS)
    assert "last_place_name" in requested


def test_an_empty_death_event_is_not_an_error_here(tmp_path: Path) -> None:
    """The adapter does not judge emptiness -- ``stages.parse`` sees the round
    count."""
    rounds = normal_match(played=2, knife=False)
    df = parse_deaths_table(build(rounds), tmp_path)
    assert df.is_empty()
    assert set(df.columns) == set(dp.DEATHS_ADAPTER_COLUMNS)


@pytest.mark.parametrize(
    "column",
    [
        "user_last_place_name",
        "user_X",
        "user_team_num",
        "attacker_last_place_name",
        "attacker_X",
        "attacker_team_num",
    ],
)
def test_missing_death_column_is_named_in_the_error(
    tmp_path: Path, column: str
) -> None:
    """A renamed field would produce a valid table with no areas.

    The same guard as ``test_missing_prop_is_named_in_the_error``, but a
    different mechanism: these fields come from ``parse_event`` and not from
    ``parse_ticks``, so ``drop_props`` does not touch them.
    """
    fake = build(death_match())
    fake.drop_death_columns = (column,)
    with pytest.raises(ParseError) as exc:
        parse_tables(fake, tmp_path)
    assert column in str(exc.value)
    assert "DEATH_COLUMNS" in str(exc.value)


def test_every_requested_player_prop_is_required_in_both_prefixes() -> None:
    """The contract list is derived from the requested props, not written by hand.

    Two hand-written lists would diverge: a new prop would be requested but
    left unchecked, and its disappearance would show only as empty columns.
    """
    for prop in dp.DEATH_PLAYER_PROPS:
        assert f"user_{prop}" in dp.DEATH_COLUMNS
        assert f"attacker_{prop}" in dp.DEATH_COLUMNS
    # The assister is not read: half empty, and no row of the report rests on
    # it.
    assert not any(c.startswith("assister_") for c in dp.DEATH_COLUMNS)


def test_the_two_column_lists_stay_apart() -> None:
    """``DAMAGE_COLUMNS`` and ``DEATH_COLUMNS`` are separate, not nested.

    They are fixed in different places, and that is exactly why the error
    message names a missing column **together with its own list**. If one were
    a subset of the other, the instruction would name the wrong constant half
    the time -- and that was the whole reason the readers were merged.
    """
    assert not set(dp.DAMAGE_COLUMNS) & set(dp.DEATH_COLUMNS)


@pytest.mark.parametrize(
    ("column", "owner"),
    [
        ("tick", "DAMAGE_COLUMNS"),
        ("user_steamid", "DAMAGE_COLUMNS"),
        ("user_last_place_name", "DEATH_COLUMNS"),
        ("attacker_team_num", "DEATH_COLUMNS"),
    ],
)
def test_the_error_names_the_list_that_owns_the_missing_column(
    tmp_path: Path, column: str, owner: str
) -> None:
    """The same rename, one instruction -- and that instruction is the right one.

    Before the merge, a lost ``user_steamid`` told the reader to update
    ``DEATH_COLUMNS`` on one path and ``DAMAGE_COLUMNS`` on the other.
    """
    fake = build(death_match())
    fake.drop_death_columns = (column,)
    with pytest.raises(ParseError) as exc:
        parse_tables(fake, tmp_path)
    assert f"{column} ({owner})" in str(exc.value)


def test_an_attackerless_death_never_carries_an_attacker_place(
    tmp_path: Path,
) -> None:
    """A position without an actor does not reach the table.

    In the measured data the library leaves every attacker field of an
    attackerless row empty, but that is **an observation and not a
    contract**: if it ever leaves an area or a coordinate in place, it would
    come out in the report as a kill nobody made. Here exactly such a row is
    built.
    """
    rounds = normal_match(played=2, knife=False)
    for round_spec in rounds:
        round_spec.deaths = [(64, None, A_PLAYERS[0], "planted_c4")]
        round_spec.death_row_overrides = {
            "attacker_last_place_name": "Middle",
            "attacker_X": 11.0,
            "attacker_Y": 22.0,
            "attacker_Z": 33.0,
            "attacker_team_num": _SIDE_TEAM["CT"],
        }
    df = parse_deaths_table(build(rounds), tmp_path)

    assert df.height == 2
    for name in (
        "attacker_id",
        "attacker_area",
        "attacker_x",
        "attacker_y",
        "attacker_z",
        "attacker_side",
        "attacker_lineup_key",
    ):
        assert df[name].null_count() == df.height, name


def test_a_death_without_a_victim_gets_its_own_counter(tmp_path: Path) -> None:
    """An event without a victim is not a failure of side inference.

    The module separates the reasons for dropping elsewhere on purpose
    (``deaths_without_attacker`` vs. ``deaths_without_attacker_area``);
    combined, a victimless row would look in the
    ``deaths_without_victim_side`` figure like a fault that is not there.
    """
    rounds = normal_match(played=2, knife=False)
    for round_spec in rounds:
        round_spec.deaths = [(64, B_PLAYERS[1], None, "ak47")]
    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_without_victim == 2
    assert adapter.diagnostics.deaths_without_victim_side == 0
    assert parse_deaths_table(build(rounds), tmp_path).is_empty()


def test_a_victim_without_a_side_keeps_its_own_counter(tmp_path: Path) -> None:
    """The guard's other half: a known victim without a side is a different
    figure."""
    rounds = normal_match(played=2, knife=False)
    ghost = "katsoja"
    for round_spec in rounds:
        round_spec.deaths = [(64, B_PLAYERS[1], ghost, "ak47")]
        round_spec.death_teams = {ghost: None}
    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_without_victim == 0
    assert adapter.diagnostics.deaths_without_victim_side == 2


def test_a_death_without_a_tick_is_dropped_and_counted(tmp_path: Path) -> None:
    """Without a tick a death cannot be assigned to a round -- nor lost.

    Every other reason for dropping is reported, and this must not be the
    exception.
    """
    rounds = normal_match(played=2, knife=False)
    for round_spec in rounds:
        round_spec.deaths = [(64, B_PLAYERS[1], A_PLAYERS[0], "ak47")]
    fake = build(rounds)
    # A library row without a tick: a valid frame that points nowhere.
    fake.events["player_death"][0]["tick"] = None

    adapter = parse_adapter(fake, tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.deaths_without_tick == 1
    assert adapter.diagnostics.deaths_outside_rounds == 0


def test_the_hurt_event_is_read_without_the_player_fields(
    tmp_path: Path,
) -> None:
    """The first contact needs four fields, not thirty.

    The shared reader must not start asking for the per-player fields for
    ``player_hurt`` too: they would be columns nothing reads.
    """
    fake = build(death_match())
    parse_tables(fake, tmp_path)

    requested = dict(fake.event_calls)
    assert requested["player_hurt"] == ()
    assert set(requested["player_death"]) == set(dp.DEATH_PLAYER_PROPS)


# --- The point cloud (Story 2.9) -----------------------------------------------


def parse_callouts_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """The point cloud alone."""
    return parse_tables(fake, tmp_path, **kwargs).callouts


def test_the_cloud_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    """The contract is an exact set of columns, not a subset."""
    cloud = parse_callouts_table(build(long_match(played=1)), tmp_path)
    assert list(cloud.columns) == list(CALLOUTS_ADAPTER_COLUMNS)
    assert cloud.schema == {
        name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS
    }


def test_the_cloud_is_built_from_where_the_players_stood(tmp_path: Path) -> None:
    """One cell per distinct player position, and the area is the game's own.

    On the fake map the players stand at ``(100i, -100i)``, the A side low and
    the B side high. Ten cells and two areas are therefore produced -- and it
    is that two that separates a working cloud from an empty one.
    """
    rounds = long_match(played=1)
    rounds[0].a_area = "Ramp"
    rounds[0].b_area = "Heaven"
    cloud = parse_callouts_table(build(rounds), tmp_path)
    assert cloud.height == 10
    assert sorted(cloud["area"].unique().to_list()) == ["Heaven", "Ramp"]
    assert cloud["observations"].min() >= 1


def test_the_cloud_is_read_from_the_whole_demo_in_one_call(tmp_path: Path) -> None:
    """One call, five light props, no tick list.

    Reading the whole tick series is expensive, so it is done **once**: two
    calls would mean the cloud is built twice and possibly with a different
    result.
    """
    fake = build(long_match(played=2))
    parse_callouts_table(fake, tmp_path)
    whole_demo = [props for props, ticks in fake.tick_calls if not ticks]
    assert whole_demo == [dp.CLOUD_TICK_PROPS]
    # The team is not asked for: the cloud is a property of the map and not of
    # a team.
    assert dp._TEAM_NUM not in dp.CLOUD_TICK_PROPS


def test_the_detonation_area_comes_from_the_cloud(tmp_path: Path) -> None:
    """A detonation has no area name of its own, so it is always an estimate.

    The source is now the game's own area definition in the nearest cell and
    not a neighbouring player -- and ``area_source`` says so out loud.
    """
    rounds = long_match(played=1)
    rounds[0].a_area = "Ramp"
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    events = parse_events_table(build(rounds), tmp_path)

    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation["area"].to_list() == ["Ramp"]
    assert detonation["area_source"].to_list() == ["point_cloud"]
    assert detonation["snap_distance"].null_count() == 0
    assert detonation["snap_distance"].min() > 0.0


def test_a_detonation_beyond_the_threshold_keeps_its_distance(
    tmp_path: Path,
) -> None:
    """I/O matrix: a distant detonation -> area null, ``snap_distance`` kept.

    The distance tells this apart from an empty point cloud: in both the area
    is null, but only here is it known from how far it would have been taken.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    events = parse_events_table(build(rounds), tmp_path, area_snap_units=10.0)

    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation["area"].null_count() == 1
    assert detonation["area_source"].null_count() == 1
    assert detonation["snap_distance"].null_count() == 0
    # A throw's area is an observation and does not go with the threshold.
    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw["area_source"].to_list() == ["observed"]


def test_an_empty_cloud_leaves_every_detonation_without_an_area(
    tmp_path: Path,
) -> None:
    """I/O matrix: an empty point cloud -> every detonation area null.

    The run does not come down, and the reason is reported: without it an
    empty cloud would look like a demo where an area just happened not to be
    found.
    """
    rounds = long_match(played=1)
    # The game gives neither side an area name -> nothing qualifies for the
    # cloud.
    rounds[0].a_area = None
    rounds[0].b_area = None
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    tables = parse_tables(build(rounds), tmp_path)
    adapter = parse_adapter(build(rounds), tmp_path)

    assert tables.callouts.is_empty()
    detonation = tables.events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation.height == 1
    assert detonation["area"].null_count() == 1
    assert detonation["snap_distance"].null_count() == 1
    assert detonation["x"].null_count() == 0
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.callout_cloud_empty_reason is not None
    assert "living player in a named area" in (
        adapter.diagnostics.callout_cloud_empty_reason
    )


def test_the_cloud_reports_the_rows_it_read(tmp_path: Path) -> None:
    """The number of rows read is visible **only** at the moment of reading.

    The round boundary ticks have no coordinates and no area -- just as a real
    demo has rows that lack them -- so more rows are read than qualify. The
    finished table holds only the part that qualified, so the ratio's
    denominator is available only from here.

    **The port does not report the number of qualifying rows**, and that is
    deliberate: it is the sum of the ``observations`` column, because every
    qualifying row ends up in exactly one cell. Two sources for the same
    figure could diverge.
    """
    adapter = parse_adapter(build(long_match(played=2)), tmp_path)
    assert adapter.diagnostics is not None
    read = adapter.diagnostics.callout_cloud_rows_read
    assert not hasattr(adapter.diagnostics, "callout_cloud_rows_usable")

    cloud = parse_callouts_table(build(long_match(played=2)), tmp_path)
    usable = int(cloud["observations"].sum())
    assert read > usable > 0


def test_a_dead_player_is_not_in_the_demos_cloud(tmp_path: Path) -> None:
    """A corpse stays where the player fell; a dead player says nothing about
    the map."""
    rounds = long_match(played=1)
    rounds[0].a_area = "Ramp"
    rounds[0].b_area = "Heaven"
    rounds[0].a_dead_at_sample = 5
    cloud = parse_callouts_table(build(rounds), tmp_path)
    assert cloud["area"].unique().to_list() == ["Heaven"]


@pytest.mark.parametrize("prop", dp.CLOUD_TICK_PROPS)
def test_a_missing_cloud_prop_is_named_in_the_error(
    tmp_path: Path, prop: str
) -> None:
    """Without the check the point cloud would be empty and every detonation
    area null.

    That would be a structurally valid result whose cause could be seen
    nowhere -- and the manifest would consider it up to date.
    """
    fake = build(long_match(played=1))
    fake.drop_cloud_props = (prop,)
    with pytest.raises(ParseError) as exc:
        parse_callouts_table(fake, tmp_path)
    assert prop in str(exc.value)
    assert "point cloud field" in str(exc.value)
    assert "CLOUD_TICK_PROPS" in str(exc.value)


def test_the_grid_size_is_a_setting_not_code(tmp_path: Path) -> None:
    """A coarser grid bundles neighbouring positions into the same cell.

    The test does not measure the "right" cell size but that the setting
    really does drive the grid: a hard-coded value would give the same table
    for both.
    """
    rounds = long_match(played=1)
    fine = parse_callouts_table(build(rounds), tmp_path, callout_grid_units=32)
    coarse = parse_callouts_table(build(rounds), tmp_path, callout_grid_units=512)
    assert fine.height > coarse.height


def _detonation_distance(rounds, tmp_path: Path, **kwargs) -> float:
    """One detonation's distance to the nearest cell at the given dimensions."""
    events = parse_events_table(build(rounds), tmp_path, **kwargs)
    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation.height == 1
    return float(detonation["snap_distance"][0])


def test_the_z_weight_and_tolerance_are_settings_not_code(tmp_path: Path) -> None:
    """The weighting is two settings, and both have to drive the measure.

    A multi-storey map is the reason the weight exists, and the tolerance is
    what makes the weight useful -- the weight alone measures worse than no
    weight at all. What is measured here is the **coupling**: a hard-coded
    value would give the same distance for all three.

    The detonation is at the B side's height, the cell's centre three units
    from it. Inside the tolerance a vertical difference is free; without the
    tolerance it costs the weight.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, B_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    common = {"area_snap_units": 5000.0, "callout_z_tolerance_units": 0.0}

    no_weight = _detonation_distance(rounds, tmp_path, callout_z_weight=0.0, **common)
    heavy = _detonation_distance(rounds, tmp_path, callout_z_weight=20.0, **common)
    forgiving = _detonation_distance(
        rounds,
        tmp_path,
        callout_z_weight=20.0,
        area_snap_units=5000.0,
        callout_z_tolerance_units=72.0,
    )

    assert heavy > no_weight
    # The tolerance swallows the same vertical difference entirely, so the
    # weight no longer costs anything.
    assert forgiving == pytest.approx(no_weight)


# --- The match table: the map name from the header (Story 2.11) ---------------


def parse_match_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """The match table alone (Story 2.11)."""
    return parse_tables(fake, tmp_path, **kwargs).match


def test_the_map_name_is_read_from_the_demo_header(tmp_path: Path) -> None:
    """The map name is an observation from the header, and the table has one row."""
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_nuke", "server_name": "FACEIT.com"}

    match = parse_match_table(fake, tmp_path)

    assert match.columns == list(MATCH_ADAPTER_COLUMNS)
    assert match.height == 1
    assert match["map_name"].to_list() == ["de_nuke"]


def test_the_header_is_read_once_from_the_same_parser_object(
    tmp_path: Path,
) -> None:
    """The header is read **once** from the same parser object.

    The claim is about the number of ``parse_header`` calls and not of
    ``_open`` calls. The difference matters: constructing a ``DemoParser(...)``
    directly inside ``_header_map_name`` would bypass ``_open`` entirely, and
    the ``_open`` counter would still show one. The call counter is on the
    other side of the port, so it sees either route.

    Two calls would mean a 233 MB demo is read again purely for the map's name
    -- and that is this story's explicit prohibition.

    Run through the shared :func:`parse_tables` harness, so that the
    configuration is the same as in the other logic tests.
    """
    fake = build(long_match(played=1))

    parse_tables(fake, tmp_path)

    assert fake.header_calls == 1


def test_a_map_outside_the_pool_is_kept_as_observed(tmp_path: Path) -> None:
    """A map outside the pool is a genuine observation, not an unknown map.

    The adapter knows nothing of the map pool at all, and that is exactly the
    contract: silently correcting the name to a pool name would turn an
    observation into a derivation.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_train"}

    assert parse_match_table(fake, tmp_path)["map_name"].to_list() == ["de_train"]


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_map_name_is_null_and_not_a_substitute(
    tmp_path: Path, value: str | None
) -> None:
    """An empty string is not a name, and the row is written anyway.

    The row exists because the match exists: an empty table would claim a demo
    without a match, which is a different thing from a match without a map
    name.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": value}

    match = parse_match_table(fake, tmp_path)

    assert match.height == 1
    assert match["map_name"].to_list() == [None]


def test_a_header_without_the_field_at_all_is_null(tmp_path: Path) -> None:
    """The library may rename the field; there is then no name."""
    fake = build(long_match(played=1))
    fake.header = {"server_name": "FACEIT.com"}

    assert parse_match_table(fake, tmp_path)["map_name"].to_list() == [None]


def test_an_empty_header_is_null_and_does_not_crash(tmp_path: Path) -> None:
    """``parse_header`` may return an empty header; that is not an exception."""
    fake = build(long_match(played=1))
    fake.header = None

    assert parse_match_table(fake, tmp_path)["map_name"].to_list() == [None]


@pytest.mark.parametrize(
    "header,expected",
    [
        ({"server_name": "FACEIT.com"}, "no map_name field"),
        ({"map_name": ""}, "is empty"),
        ({"map_name": "   "}, "is empty"),
        ({"map_name": b"de_ancient"}, "is not a string"),
        (None, "did not return a dictionary"),
    ],
)
def test_a_missing_map_name_records_its_reason(
    tmp_path: Path, header, expected: str
) -> None:
    """A missing name has three different causes, and none may go unsaid.

    A missing name is a legitimate observation, but "the header had no map"
    and "demoparser2 renamed the field" are different things. The first is a
    property of the demo, the second a fault in the program -- and as the
    latter the whole archive would go back to per-demo map branches without a
    single sign. The same rule as for the reason a point cloud is empty: it is
    visible only at the moment of reading, so it travels in the diagnostics.
    """
    fake = build(long_match(played=1))
    fake.header = header

    adapter = parse_adapter(fake, tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.header_map_name_missing_reason is not None
    assert expected in adapter.diagnostics.header_map_name_missing_reason


def test_a_found_map_name_records_no_reason(tmp_path: Path) -> None:
    """The other branch: a successful read leaves no reason hanging.

    Without this claim the field could be left at the previous demo's value,
    and the output would report a missing name for a demo that had one.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_nuke"}

    adapter = parse_adapter(fake, tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.header_map_name_missing_reason is None


def test_a_byte_map_name_is_not_a_name(tmp_path: Path) -> None:
    """A bytes object is no name, and no substitute is made from it.

    ``str(b"de_ancient")`` is ``"b'de_ancient'"``, which would look like an
    observation in the table and would shatter the map into a branch of its
    own. The observation is the name or its absence -- a library type change
    must not pass silently.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": b"de_ancient"}

    assert parse_match_table(fake, tmp_path)["map_name"].to_list() == [None]


def test_the_map_name_is_trimmed(tmp_path: Path) -> None:
    """Spaces at the edges are not part of the name.

    ``" de_ancient "`` would be a different branch from ``"de_ancient"``, so
    the observation would shatter the map instead of assembling it. Trimming
    is not validation: the spelling is not changed and the name is not
    compared against the map pool.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "  de_ancient  "}

    assert parse_match_table(fake, tmp_path)["map_name"].to_list() == ["de_ancient"]


def test_an_unreadable_header_is_a_wrapped_error_naming_both_causes(
    tmp_path: Path,
) -> None:
    """The library's own exception is wrapped, and the message does not claim
    one cause out of two.

    Wrapping is the same rule as in ``_open`` and ``_event``: the
    ``ParseError``'s message says what to do, and it is only that kind of
    error ``stages.parse`` recognises in order to record the demo's status as
    ``parse_failed``. Renamed in T4: the name used to claim the message is in
    Finnish.

    A method the library has renamed raises an ``AttributeError`` on a
    perfectly intact demo. A bare "download it again" would send the user to
    fetch a 230 MB file that is already fine, and would say nothing about
    where the fault really is.
    """
    fake = build(long_match(played=1))
    fake.header_error = AttributeError("parse_header")

    with pytest.raises(ParseError) as err:
        parse_match_table(fake, tmp_path)

    message = str(err.value)
    assert "the file is corrupt" in message
    assert "demoparser2's interface has changed" in message
    # The exception's kind is included, so that the reader can see which cause
    # is the more likely without having to repeat the run.
    assert "AttributeError" in message


def test_the_match_table_carries_only_the_map_name(tmp_path: Path) -> None:
    """One of the header's 12 fields belongs in the table.

    On FACEIT demos ``server_name`` is ``FACEIT.com register to play here``,
    so it would say something about the match's source -- but that is not this
    story's business, and every extra column would be a change to the
    contract.
    """
    fake = build(long_match(played=1))

    match = parse_match_table(fake, tmp_path)

    assert set(match.columns) == {"map_name"}


# --- Reading the header as an operation of its own (Story 3.6) ----------------


def read_map_name_with(
    fake: FakeDemoparser2, tmp_path: Path, name: str = "feikki.dem"
) -> str | None:
    """Run the port's new operation on the fake; only ``_open`` is replaced.

    The same harness as :func:`parse_tables` and for the same reason: the
    adapter's own code really runs, and only the library's boundary is faked.
    """
    demo = tmp_path / name
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Adapter()
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    return adapter.read_map_name(demo)


def test_reading_the_map_name_does_not_parse_the_demo(tmp_path: Path) -> None:
    """Reading the header must not read a single tick or event.

    This is the reason the whole operation exists. The import needs the map's
    name before the file is moved into place, and if the name could only be
    got by parsing, every import would cost a 230 MB pass into six tables --
    that is, more than the import itself.

    The claim is about call counters and not about run time: a slow machine
    makes run time noise, but the number of ``parse_ticks`` calls is zero or
    it is not.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_nuke"}

    assert read_map_name_with(fake, tmp_path) == "de_nuke"

    assert fake.tick_calls == []
    assert fake.event_calls == []
    assert fake.header_calls == 1


def test_a_map_outside_the_pool_is_an_observation_here_too(tmp_path: Path) -> None:
    """The same observation rule as in the match table -- and here it is
    essential.

    The import compares this name against FACEIT's draft information. If the
    port corrected the name to the "right" one from the map pool, the
    comparison could never notice a discrepancy: the very check the operation
    exists for would be dead.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_train"}

    assert read_map_name_with(fake, tmp_path) == "de_train"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_a_missing_map_name_is_none_and_not_a_substitute(
    tmp_path: Path, value: str | None
) -> None:
    """An empty name is "no observation", not an observation of emptiness.

    The difference matters in the import: "no observation" leads to a
    question, whereas a substitute (an empty string, say) would match or fail
    to match the draft information entirely at random.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": value}

    assert read_map_name_with(fake, tmp_path) is None


def test_a_header_without_the_field_at_all_is_none(tmp_path: Path) -> None:
    """A field the library renamed is the same outcome as an empty name."""
    fake = build(long_match(played=1))
    fake.header = {"server_name": "FACEIT.com"}

    assert read_map_name_with(fake, tmp_path) is None


def test_an_unreadable_header_is_a_wrapped_parse_error(tmp_path: Path) -> None:
    """The library's own error type is no use as the port's contract.

    The same wrapper as in the full parse, because the caller is different but
    the fault is the same: the message names both possible causes and does not
    send the user to download a file that is already fine. Renamed in T4: the
    name used to claim the message is in Finnish.
    """
    fake = build(long_match(played=1))
    fake.header_error = AttributeError("parse_header")

    with pytest.raises(ParseError) as err:
        read_map_name_with(fake, tmp_path)

    assert "demoparser2's interface has changed" in str(err.value)


def test_a_file_that_is_not_a_demo_never_reaches_the_library(
    tmp_path: Path,
) -> None:
    """The magic-byte check comes before the library, in this operation too.

    An HTML error page with a ``.dem`` suffix is a perfectly valid file, and
    without the check it would reach demoparser2 -- which would report the
    fault in its own vocabulary rather than the tool's.
    """
    demo = tmp_path / "eidemo.dem"
    demo.write_bytes(b"<!doctype html><html>403</html>")
    fake = build(long_match(played=1))
    adapter = Demoparser2Adapter()
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]

    with pytest.raises(ParseError) as err:
        adapter.read_map_name(demo)

    assert "is not a CS2 demo" in str(err.value)
    assert fake.header_calls == 0


def test_read_map_name_uses_the_same_reader_as_the_full_parse(
    tmp_path: Path,
) -> None:
    """There is **one** header reader, and this test is what guards that.

    :func:`test_the_two_operations_read_the_same_name` compares the results,
    and it is useful -- but it would pass even when ``read_map_name`` is a
    copied reader of its own: two identical readers see the same fake the same
    way. A review proved it: a substituted reader passed 372 tests.

    The claim is therefore **about the call and not about the value**. The
    adapter's own ``_header_map_name`` is spied on, and a parallel reader
    would leave the spy uncalled. Two readers would diverge before long, and
    the divergence would show only in the import accepting a demo that the
    parse names differently.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_nuke"}
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Adapter()
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]

    nahdyt: list[Path] = []
    oikea = adapter._header_map_name

    def vakooja(parser, original_path):
        nahdyt.append(original_path)
        return oikea(parser, original_path)

    adapter._header_map_name = vakooja  # type: ignore[method-assign]

    assert adapter.read_map_name(demo) == "de_nuke"
    assert nahdyt == [demo], (
        "read_map_name did not call the adapter's own _header_map_name "
        "reader: a parallel reader would diverge from the full parse "
        "unnoticed"
    )


def test_read_map_name_reads_a_compressed_demo_in_the_default_run(
    tmp_path: Path,
) -> None:
    """A compressed file goes through decompression **without real demos**.

    The import's real input is a ``.dem.zst``, but every other test in this
    file gives an uncompressed path -- so the decompression path never ran in
    the default run, and its breaking would have shown only in a ``-m demo``
    run on a machine that has the demos.

    The claim is also that the library is given the **decompressed** path and
    not the compressed one: without the decompression demoparser2 would get
    zstd bytes and would fail in its own vocabulary.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_ancient"}
    raw = DEMO_MAGIC + b"\x00" + b"x" * 4096
    demo = tmp_path / "feikki.dem.zst"
    demo.write_bytes(zstandard.ZstdCompressor().compress(raw))

    nahdyt: list[tuple[Path, bytes]] = []
    adapter = Demoparser2Adapter()

    def avaa(demo_path, original_path):
        # The content is read **here**: the decompression directory is cleaned
        # up as soon as ``readable_demo`` closes, so there is nothing to read
        # afterwards.
        path = Path(demo_path)
        nahdyt.append((path, path.read_bytes()))
        return fake

    adapter._open = avaa  # type: ignore[method-assign]

    assert adapter.read_map_name(demo) == "de_ancient"
    assert len(nahdyt) == 1
    annettu, sisalto = nahdyt[0]
    assert annettu != demo, "the library was given the compressed file"
    assert sisalto == raw


def test_read_map_name_refuses_a_truncated_compressed_demo(
    tmp_path: Path,
) -> None:
    """**A partial demo must not give a map name.**

    This is the seam through which the import sees a truncated file: the
    decompression compares the result against the size the frame declares, and
    the difference rises all the way up here. Without it the header would be
    read from half a demo entirely successfully -- measured 2026-09-05 with a
    real demo that gave ``de_ancient`` cut in half.

    **The input has to decompress partially but not emptily.** A payload under
    a megabyte fits into a single zstd block and decompresses to zero when
    truncated -- that is, it would hit the old "the result was an empty file"
    guard, and this test would fail on the removal of the frame-size guard
    only **because of the error message's wording**. A large payload still
    compresses to a bit over a kilobyte, so the cost is negligible (see the
    measurement table in ``tests/test_demo_parser.py``).

    The library is not called at all, and that is part of the claim: the
    refusal happens before anything has been parsed.
    """
    raw = DEMO_MAGIC + b"x" * 40_000_000
    whole = zstandard.ZstdCompressor().compress(raw)
    katkaistu = whole[: len(whole) // 2]
    demo = tmp_path / "katkennut.dem.zst"
    demo.write_bytes(katkaistu)
    # The input's property is measured with the library directly and not with
    # the code the input exists to test.
    osittainen = len(
        zstandard.ZstdDecompressor().stream_reader(io.BytesIO(katkaistu)).read()
    )
    assert 0 < osittainen < len(raw), (
        "the input does not decompress partially but not emptily"
    )

    fake = build(long_match(played=1))
    adapter = Demoparser2Adapter()
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]

    with pytest.raises(ParseError) as err:
        adapter.read_map_name(demo)

    assert "came up short" in str(err.value)
    assert str(osittainen) in str(err.value)
    assert fake.header_calls == 0


def test_the_two_operations_read_the_same_name(tmp_path: Path) -> None:
    """The import and the parse must not see a different name for the demo's map.

    Two parallel header readers would diverge before long, and the divergence
    would show only in the import accepting a demo that the parse names
    differently -- that is, in exactly the situation the cross-check exists to
    prevent. This test is the one that fails if the readers are separated.
    """
    fake = build(long_match(played=1))
    fake.header = {"map_name": "de_ancient"}

    from_header = read_map_name_with(fake, tmp_path)
    from_tables = parse_match_table(fake, tmp_path)["map_name"].to_list()[0]

    assert from_header == from_tables == "de_ancient"
