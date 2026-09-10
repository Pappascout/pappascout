"""``stages.parse`` -- the stage's tests without demos.

The stage sees the demo only from behind the port (AD-8), so its whole logic
-- validating the tables, joining the round number to the sample points and
the events, the atomic write, the manifest and the skip -- is tested with a
fake that builds every table by hand. Not one of these tests needs a demo
file.
"""

from __future__ import annotations

import json
import inspect
from pathlib import Path

import polars as pl
import pytest

from conftest import has_temp_leftovers, settings_text
from pappascout.adapters.demo_parser import Demoparser2Adapter
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
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.constants import SAMPLE_KINDS, SIDES
from pappascout.domain.models import ParseSettings, load_settings
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.errors import DemoUnavailable, PappascoutError, ParseError, SchemaError
from pappascout.stages import StageResult
from pappascout.stages import parse as parse_stage

MAP_DEMO_ID = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"

#: The port contract's types: ``ROUNDS`` with the numbering columns added.
ADAPTER_SCHEMA: dict[str, object] = {
    name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
}

#: The sample-point table's types behind the port: ``TICKS`` without
#: ``map_demo_id``.
TICKS_ADAPTER_SCHEMA: dict[str, object] = {
    name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS
}

#: The event table's types behind the port: ``EVENTS`` without
#: ``map_demo_id``.
EVENTS_ADAPTER_SCHEMA: dict[str, object] = {
    name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS
}

#: The point cloud's types behind the port: ``CALLOUT_CLOUD`` without
#: ``map_demo_id``.
CALLOUTS_ADAPTER_SCHEMA: dict[str, object] = {
    name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS
}

#: The match table's types behind the port: ``MATCH`` without
#: ``map_demo_id``.
MATCH_ADAPTER_SCHEMA: dict[str, object] = {
    name: MATCH[name] for name in MATCH_ADAPTER_COLUMNS
}

#: The cell edge the fake's point cloud is built with. The same as
#: ``settings.toml``'s ``callout_grid_units``.
CALLOUT_GRID = 32

#: The sample points the fake builds its tick rows with.
SAMPLE_SECONDS = (6.0, 15.0)


def build_callouts(cells: int = 4) -> pl.DataFrame:
    """A point cloud as the adapter would give it: a row per cell, no round.

    The cells are consecutive and there are two areas, so that the table looks
    like what a real cloud is: many cells inside an area. The observation
    counts differ in size, because they are the cell's own observation and not
    a constant.
    """
    rows = [
        {
            "cell_x": index,
            "cell_y": 0,
            "cell_z": 0,
            "area": "BombsiteA" if index % 2 == 0 else "Middle",
            "observations": 10 + index,
        }
        for index in range(cells)
    ]
    return pl.DataFrame(rows, schema=dict(CALLOUTS_ADAPTER_SCHEMA), orient="row")


def build_match(
    map_name: str | None = "de_ancient", *, rows: int | None = None
) -> pl.DataFrame:
    """A match table as the adapter would give it: one row, no round number.

    ``rows`` is only for testing the row-count guard: with it the fake can
    return a table of zero or two rows whose schema is still right.
    """
    row = {"map_name": map_name}
    payload = [row] if rows is None else [row] * rows
    if not payload:
        return pl.DataFrame(schema=dict(MATCH_ADAPTER_SCHEMA))
    return pl.DataFrame(payload, schema=dict(MATCH_ADAPTER_SCHEMA))


# --- The fake behind the port --------------------------------------------------


def build_rounds(
    played: int = 3,
    *,
    warmup: int = 1,
    without_anchor: tuple[int, ...] = (),
) -> pl.DataFrame:
    """Build the rounds table by hand, as the real adapter would return it.

    The winner is always T and the reason ``ct_killed``, which is a way for T
    to win that the CS2 rules allow -- otherwise ``check_win_reasons`` would
    reject the table.

    Args:
        played: The number of played rounds.
        warmup: The number of unnumbered rounds at the start (the knife round
            and the warm-up): their combined score does not grow.
        without_anchor: The ``round_raw`` values that have no freezetime
            anchor.
    """
    rows: list[dict[str, object]] = []
    round_raw = 0
    score = 0

    def add_pair(start: int, end: int) -> None:
        nonlocal round_raw
        round_raw += 1
        no_anchor = round_raw in without_anchor
        for index, (side, lineup) in enumerate((("T", "aaa"), ("CT", "bbb"))):
            rows.append(
                {
                    "round_raw": round_raw,
                    "round_no": None,
                    "lineup_key": lineup,
                    "side": side,
                    "won": side == "T",
                    "win_reason": "ct_killed",
                    "money_buy_end": None if no_anchor else 3000 + index,
                    "equip_buy_end": None if no_anchor else 20000 + index,
                    "equip_round_start": None if no_anchor else 1000 + index,
                    "players_buy_end": None if no_anchor else 5,
                    # The adapter gives the counter ready-made; the stage only
                    # carries it. A per-side difference makes the carrying
                    # observable.
                    ARMED_COLUMN: None if no_anchor else 5 - index,
                    # The armour counter is deliberately a **different
                    # distribution** from the armed one: if the stage carried
                    # the same column twice, the distributions would be
                    # identical and not one test would see it.
                    ARMORED_COLUMN: None if no_anchor else 5,
                    "survivors": index,
                    "survivors_equip_prev": 500,
                    "freeze_end_tick": None if no_anchor else 1000 * round_raw,
                    # The measurement point is deliberately different from the
                    # anchor: if it were left unfilled, Polars would fill it
                    # with a null and not one stage test would ever see the
                    # column filled.
                    "buy_end_tick": None if no_anchor else 1000 * round_raw + 1280,
                    "tick_rate": 64.0,
                    "status": "no_freeze_end" if no_anchor else "ok",
                    "score_start": start,
                    "score_end": end,
                }
            )

    for _ in range(warmup):
        add_pair(score, score)
    for _ in range(played):
        add_pair(score, score + 1)
        score += 1

    return pl.DataFrame(rows, schema=dict(ADAPTER_SCHEMA), orient="row")


def build_ticks(
    rounds: pl.DataFrame,
    *,
    sample_seconds: tuple[float, ...] = SAMPLE_SECONDS,
    first_contact_rounds: tuple[int, ...] = (),
    short_rounds: dict[int, float] | None = None,
    contact_t_s: float = 9.5,
) -> pl.DataFrame:
    """A sample-point table matching ``build_rounds``, as the adapter gives it.

    The adapter samples **every** anchored round boundary, the warm-up and the
    knife round included: it does not know the numbering rule. Dropping them
    is the stage's job, so the fake has to produce them.

    Args:
        rounds: The rounds table from which ``round_raw``, ``side`` and
            ``lineup_key`` are read -- the keys must not differ between the
            tables.
        sample_seconds: The moments in time.
        first_contact_rounds: The ``round_raw`` values on which first contact
            was found.
        short_rounds: ``round_raw -> the round's duration in seconds``. A
            sample point that exceeds the duration is left out -- as in a real
            demo.
    """
    duration = short_rounds or {}
    rows: list[dict[str, object]] = []
    for round_row in rounds.iter_rows(named=True):
        raw = round_row["round_raw"]
        if round_row["freeze_end_tick"] is None:
            continue  # a round without an anchor produces no sample points
        moments: list[tuple[str, float]] = [
            ("time", s) for s in sample_seconds if s <= duration.get(raw, 1e9)
        ]
        if raw in first_contact_rounds:
            moments.append(("first_contact", contact_t_s))
        for kind, t_s in moments:
            for index in range(5):
                rows.append(
                    {
                        "round_raw": raw,
                        "round_no": None,
                        "player_id": f"{round_row['lineup_key']}-{index}",
                        "lineup_key": round_row["lineup_key"],
                        "side": round_row["side"],
                        "sample_kind": kind,
                        "sample_t_s": t_s,
                        "t_s": t_s,
                        "x": 10.0 * index,
                        "y": -10.0 * index,
                        "z": 1.0,
                        "area": None if index == 4 else "Ramp",
                        "is_alive": index < 4,
                    }
                )
    return pl.DataFrame(rows, schema=dict(TICKS_ADAPTER_SCHEMA), orient="row")


#: The id that every grenade of the same round shares when
#: ``build_events(recycle_entity_ids=True)``. This is exactly what the game
#: does.
RECYCLED_ENTITY_ID = 564


def build_events(
    rounds: pl.DataFrame,
    *,
    per_round: int = 2,
    unexploded: tuple[int, ...] = (),
    without_area: tuple[int, ...] = (),
    recycle_entity_ids: bool = False,
) -> pl.DataFrame:
    """An event table matching ``build_rounds``, as the adapter would give it.

    The adapter produces rows from **every anchored** round boundary, the
    warm-up and the knife round included -- it does not know the numbering
    rule.

    Args:
        rounds: The rounds table from which ``round_raw``, ``side`` and
            ``lineup_key`` are read; the keys must not differ between the
            tables.
        per_round: How many grenades each team throws in a round.
        unexploded: The ordinal numbers of the grenades that have no
            detonation row (1-based, the same number as in
            ``without_area``).
        without_area: The ordinal numbers of the grenades whose area came out
            empty.
        recycle_entity_ids: Give every grenade of the same round the **same**
            ``grenade_entity_id``, as the game really does
            (``inferno_vs_ryhmarama`` round 11). The old key
            ``(round_no, grenade_entity_id)`` then collides, and only
            ``grenade_no`` tells the trajectories apart.

    ``grenade_no`` and ``grenade_entity_id`` get **different values**: the
    numbers start from 500. With identical values the columns crossing over
    could not be observed, because both are ``Int32``.
    """
    rows: list[dict[str, object]] = []
    entity = 0
    number = 500
    for round_row in rounds.iter_rows(named=True):
        if round_row["freeze_end_tick"] is None:
            continue  # a round without an anchor produces no events
        for index in range(per_round):
            entity += 1
            number += 1
            moments: list[tuple[str, float]] = [("grenade_thrown", 5.0 + index)]
            if entity not in unexploded:
                moments.append(("grenade_detonate", 7.0 + index))
            for kind, t_s in moments:
                rows.append(
                    {
                        "round_raw": round_row["round_raw"],
                        "round_no": None,
                        "event_kind": kind,
                        "grenade_no": number,
                        "grenade_entity_id": (
                            RECYCLED_ENTITY_ID
                            if recycle_entity_ids
                            else entity
                        ),
                        "grenade_type": "smoke" if index == 0 else "flashbang",
                        "thrower_id": f"{round_row['lineup_key']}-{index}",
                        "lineup_key": round_row["lineup_key"],
                        "side": round_row["side"],
                        "t_s": t_s,
                        "x": 100.0 * index,
                        "y": -100.0 * index,
                        "z": 2.0,
                        "area": None if entity in without_area else "Ramp",
                        # The throw's area is an observation, the
                        # detonation's an estimate -- as the real adapter
                        # produces them.
                        "area_source": (
                            None
                            if entity in without_area
                            else ("observed" if kind == "grenade_thrown" else "point_cloud")
                        ),
                        "snap_distance": (
                            None if kind == "grenade_thrown" else 120.0
                        ),
                    }
                )
    return pl.DataFrame(rows, schema=dict(EVENTS_ADAPTER_SCHEMA), orient="row")


#: The lineup table's types behind the port: ``LINEUPS`` without
#: ``map_demo_id``.
LINEUPS_ADAPTER_SCHEMA: dict[str, object] = {
    name: LINEUPS[name] for name in LINEUPS_ADAPTER_COLUMNS
}

#: The clan names the fake gives the lineups. Measured from real demos.
CLANS: dict[str, str] = {"aaa": "MatureMayhem", "bbb": "KALJUKOSTAJA"}


def build_lineups(
    rounds: pl.DataFrame,
    *,
    without_clan: tuple[str, ...] = (),
    without_name: tuple[str, ...] = (),
) -> pl.DataFrame:
    """A lineup table matching ``build_rounds``, as the adapter would give it.

    A row per (lineup, player) and **no round number**: the name is a property
    of the map and not of a round. The player ids are the same as in
    ``build_ticks``, so that the tables do not disagree about the lineup.

    Args:
        rounds: The rounds table from which the lineup ids are read.
        without_clan: The lineups that have no clan name.
        without_name: The lineups whose players have no names.
    """
    rows: list[dict[str, object]] = []
    for lineup in sorted({r["lineup_key"] for r in rounds.iter_rows(named=True)}):
        for index in range(5):
            rows.append(
                {
                    "lineup_key": lineup,
                    "player_id": f"{lineup}-{index}",
                    "player_name": (
                        None if lineup in without_name else f"{lineup}{index}"
                    ),
                    "clan_name": (
                        None if lineup in without_clan else CLANS.get(lineup, lineup)
                    ),
                }
            )
    return pl.DataFrame(rows, schema=dict(LINEUPS_ADAPTER_SCHEMA), orient="row")


#: The deaths table's types behind the port: ``DEATHS`` without
#: ``map_demo_id``.
DEATHS_ADAPTER_SCHEMA: dict[str, object] = {
    name: DEATHS[name] for name in DEATHS_ADAPTER_COLUMNS
}


def build_deaths(
    rounds: pl.DataFrame,
    *,
    per_round: int = 1,
    without_attacker: tuple[int, ...] = (),
    without_victim_area: tuple[int, ...] = (),
    without_attacker_area: tuple[int, ...] = (),
) -> pl.DataFrame:
    """A deaths table matching ``build_rounds``, as the adapter would give it.

    The adapter produces rows from **every anchored** round boundary, the
    warm-up and the knife round included: people really do die on the knife
    round, and the adapter does not know the numbering rule.

    The death is recorded from the point of view of the T-side row: the victim
    is from the T lineup and the attacker from the CT lineup. ``rounds`` is a
    long table (two rows per round), so only one side is read -- otherwise
    every death would be born twice.

    Args:
        rounds: The rounds table from which ``round_raw`` and the lineups are
            read.
        per_round: How many deaths in a round.
        without_attacker: The ordinal numbers of the deaths (1-based) that
            have no attacker at all -- a fall or the bomb.
        without_victim_area: The numbers that have no victim area.
        without_attacker_area: The numbers that are missing **only** the
            attacker's area.
    """
    sides = {
        row["side"]: row["lineup_key"] for row in rounds.iter_rows(named=True)
    }
    victim_lineup = sides["T"]
    attacker_lineup = sides["CT"]

    rows: list[dict[str, object]] = []
    number = 0
    for round_row in rounds.iter_rows(named=True):
        if round_row["side"] != "T" or round_row["freeze_end_tick"] is None:
            continue
        for index in range(per_round):
            number += 1
            has_attacker = number not in without_attacker
            rows.append(
                {
                    "round_raw": round_row["round_raw"],
                    "round_no": None,
                    "t_s": 20.0 + index,
                    "victim_id": f"{victim_lineup}-{index}",
                    "victim_lineup_key": victim_lineup,
                    "victim_side": "T",
                    "victim_x": 10.0 * index,
                    "victim_y": -10.0 * index,
                    "victim_z": 1.0,
                    "victim_area": (
                        None if number in without_victim_area else "Cave"
                    ),
                    "attacker_id": (
                        f"{attacker_lineup}-{index}" if has_attacker else None
                    ),
                    "attacker_lineup_key": (
                        attacker_lineup if has_attacker else None
                    ),
                    "attacker_side": "CT" if has_attacker else None,
                    "attacker_x": 20.0 * index if has_attacker else None,
                    "attacker_y": -20.0 * index if has_attacker else None,
                    "attacker_z": 2.0 if has_attacker else None,
                    "attacker_area": (
                        "Middle"
                        if has_attacker and number not in without_attacker_area
                        else None
                    ),
                    "weapon": "planted_c4" if not has_attacker else "ak47",
                }
            )
    return pl.DataFrame(rows, schema=dict(DEATHS_ADAPTER_SCHEMA), orient="row")


class FakeParser:
    """An implementation of the port that does not touch demoparser2."""

    def __init__(
        self,
        frame: pl.DataFrame | None = None,
        error: Exception | None = None,
        ticks: pl.DataFrame | None = None,
        events: pl.DataFrame | None = None,
        lineups: pl.DataFrame | None = None,
        deaths: pl.DataFrame | None = None,
        callouts: pl.DataFrame | None = None,
        match: pl.DataFrame | None = None,
    ):
        self.frame = frame if frame is not None else build_rounds()
        self.ticks = ticks if ticks is not None else build_ticks(self.frame)
        self.events = events if events is not None else build_events(self.frame)
        self.lineups = (
            lineups if lineups is not None else build_lineups(self.frame)
        )
        self.deaths = deaths if deaths is not None else build_deaths(self.frame)
        self.callouts = callouts if callouts is not None else build_callouts()
        self.match = match if match is not None else build_match()
        self.error = error
        self.calls = 0
        self.seen_seconds: list[tuple[float, ...]] = []

    def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
        self.calls += 1
        self.seen_seconds.append(tuple(sample_seconds))
        if self.error is not None:
            raise self.error
        return DemoTables(
            rounds=self.frame,
            ticks=self.ticks,
            events=self.events,
            lineups=self.lineups,
            deaths=self.deaths,
            callouts=self.callouts,
            match=self.match,
        )


# --- Fixtures --------------------------------------------------------------


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    root = tmp_path / "arkisto"
    root.mkdir()
    return ArchivePaths(root=root)


@pytest.fixture
def demo(archive: ArchivePaths) -> Path:
    """A placeholder demo: the fake reads no content, the path must be real."""
    path = archive.import_dir() / f"{MAP_DEMO_ID}.dem"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PBDEMS2\x00" + b"x" * 1024)
    return path


@pytest.fixture
def parse_settings(settings_file: Path):
    return load_settings(settings_file, env_files=()).parse


def run_parse(settings, archive, parser, demo, **kwargs):
    return parse_stage.run(
        settings, archive, MAP_DEMO_ID, parser, demo_path=demo, **kwargs
    )


# --- A successful run --------------------------------------------------------


def test_writes_a_valid_rounds_table(parse_settings, archive, demo) -> None:
    result = run_parse(parse_settings, archive, FakeParser(build_rounds(played=21)), demo)

    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert df.height == 42
    assert list(df.columns) == list(ROUNDS)
    assert df.schema == dict(ROUNDS)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert sorted(df["round_no"].unique().to_list()) == list(range(1, 22))
    assert result.stats["rounds"] == 21
    assert result.stats["rows"] == 42
    assert not result.skipped
    assert result.status == "ok"


def test_numbering_columns_never_reach_the_archive(
    parse_settings, archive, demo
) -> None:
    """``score_start`` and ``score_end`` are the port's internal tools."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert "score_start" not in df.columns
    assert "score_end" not in df.columns


def test_unplayed_rounds_stay_out_of_the_table_but_are_counted(
    parse_settings, archive, demo
) -> None:
    result = run_parse(parse_settings, archive, FakeParser(build_rounds(2, warmup=3)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df["round_no"].null_count() == 0
    assert df.height == 4
    assert result.stats["skipped_rounds"] == 3
    # round_raw stays the demo's own number, so the skip shows as a gap.
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]


def test_round_without_a_freeze_anchor_stays_in_the_table(
    parse_settings, archive, demo
) -> None:
    """A missing anchor does not fail the run: the round is in with its own
    status."""
    frame = build_rounds(3, warmup=0, without_anchor=(2,))
    result = run_parse(parse_settings, archive, FakeParser(frame), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df.height == 6
    missing = df.filter(pl.col("status") == "no_freeze_end")
    assert missing.height == 2
    assert missing["freeze_end_tick"].null_count() == 2
    assert missing["round_no"].to_list() == [2, 2]
    assert result.stats["no_freeze_end"] == 1


def test_writes_a_manifest_with_only_the_parse_section(
    parse_settings, archive, demo
) -> None:
    run_parse(parse_settings, archive, FakeParser(), demo)
    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))

    assert manifest.stage == "parse"
    assert manifest.status == "ok"
    assert list(manifest.tool_versions) == ["demoparser2"]
    assert manifest.outputs == [
        f"parsed/{MAP_DEMO_ID}/rounds.parquet",
        f"parsed/{MAP_DEMO_ID}/ticks.parquet",
        f"parsed/{MAP_DEMO_ID}/events.parquet",
        f"parsed/{MAP_DEMO_ID}/lineups.parquet",
        f"parsed/{MAP_DEMO_ID}/deaths.parquet",
        f"parsed/{MAP_DEMO_ID}/callouts.parquet",
        f"parsed/{MAP_DEMO_ID}/match.parquet",
    ]
    assert manifest.inputs[0].result_id == f"demo/{MAP_DEMO_ID}"


def test_demo_hash_is_read_from_meta_not_recomputed(
    parse_settings, archive, demo
) -> None:
    """A sha256 over 233 MB on every run would be slower than the parse."""
    meta_path = archive.demo_meta(MAP_DEMO_ID)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"sha256": "kokeiltu-tiiviste"}), encoding="utf-8")

    run_parse(parse_settings, archive, FakeParser(), demo)
    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))
    assert manifest.inputs[0].sha256 == "kokeiltu-tiiviste"


def test_write_is_atomic(parse_settings, archive, demo) -> None:
    run_parse(parse_settings, archive, FakeParser(), demo)
    assert not has_temp_leftovers(archive.root)


def test_rows_are_sorted_by_round(parse_settings, archive, demo) -> None:
    run_parse(parse_settings, archive, FakeParser(build_rounds(5)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df["round_no"].to_list() == sorted(df["round_no"].to_list())


# --- The sample-point table --------------------------------------------------


def test_writes_a_valid_ticks_table(parse_settings, archive, demo) -> None:
    """Acceptance criterion: ``ticks.parquet`` passes ``validate(TICKS)``."""
    rounds = build_rounds(played=3, warmup=0)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo
    )

    table = archive.parsed_table(MAP_DEMO_ID, "ticks")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert list(df.columns) == list(TICKS)
    assert df.schema == dict(TICKS)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    # 3 rounds x 2 teams x 2 sample points x 5 players.
    assert df.height == 60
    assert result.stats["tick_rows"] == 60
    assert result.stats["sample_points"] == 6  # round x moment
    assert result.stats["sample_rounds"] == 3


def test_all_seven_tables_are_listed_among_the_outputs(
    parse_settings, archive, demo
) -> None:
    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert [p.name for p in result.outputs] == [
        "rounds.parquet",
        "ticks.parquet",
        "events.parquet",
        "lineups.parquet",
        "deaths.parquet",
        "callouts.parquet",
        "match.parquet",
    ]


def test_ticks_get_the_round_number_from_the_rounds_table(
    parse_settings, archive, demo
) -> None:
    """The numbering belongs to domain.rounds; the stage only joins it."""
    rounds = build_rounds(played=3, warmup=0)
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))

    assert df["round_no"].null_count() == 0
    assert sorted(df["round_no"].unique().to_list()) == [1, 2, 3]
    # round_raw stays alongside as the demo's own number.
    pairs = set(zip(df["round_raw"].to_list(), df["round_no"].to_list()))
    assert pairs == {(1, 1), (2, 2), (3, 3)}


def test_unnumbered_rounds_produce_no_tick_rows(parse_settings, archive, demo) -> None:
    """I/O matrix: the warm-up and the knife round -> no tick rows.

    The adapter samples them because it does not know the numbering rule;
    this test locks in that the stage drops them by the same decision as from
    the rounds table.
    """
    rounds = build_rounds(played=2, warmup=3)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo
    )
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    # The unnumbered round_raw values 1..3 are not in the table.
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]
    assert result.stats["skipped_rounds"] == 3


def test_a_round_without_an_anchor_has_no_tick_rows(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: a round without an anchor is in rounds but not in ticks."""
    rounds = build_rounds(3, warmup=0, without_anchor=(2,))
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo)

    rounds_list = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    ticks = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    assert 2 in rounds_list["round_no"].to_list()
    assert sorted(ticks["round_no"].unique().to_list()) == [1, 3]


def test_a_short_round_keeps_only_the_points_it_reached(
    parse_settings, archive, demo
) -> None:
    """Acceptance criterion: no sample point after the round has ended."""
    rounds = build_rounds(played=2, warmup=0)
    ticks = build_ticks(rounds, short_rounds={2: 10.0})  # round_raw 2 ended at 10 s
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    short_round = df.filter(pl.col("round_no") == 2)
    assert sorted(short_round["sample_t_s"].unique().to_list()) == [6.0]
    long_round = df.filter(pl.col("round_no") == 1)
    assert sorted(long_round["sample_t_s"].unique().to_list()) == [6.0, 15.0]


def test_first_contact_rows_are_counted_separately(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds(played=3, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(1, 3))
    result = run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    contact = df.filter(pl.col("sample_kind") == "first_contact")
    assert sorted(contact["round_no"].unique().to_list()) == [1, 3]
    assert result.stats["first_contact_rounds"] == 2


def test_lineup_keys_join_across_the_two_tables(
    parse_settings, archive, demo
) -> None:
    """The join ``(map_demo_id, round_no)`` must not cross the teams over."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    rounds_list = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    ticks = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))

    joined = ticks.join(
        rounds_list.select("map_demo_id", "round_no", "lineup_key", "side"),
        on=["map_demo_id", "round_no", "lineup_key", "side"],
        how="inner",
    )
    assert joined.height == ticks.height


def test_ticks_rows_are_sorted_by_round_and_time(
    parse_settings, archive, demo
) -> None:
    run_parse(parse_settings, archive, FakeParser(build_rounds(4, warmup=0)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    keys = list(zip(df["round_no"].to_list(), df["sample_t_s"].to_list()))
    assert keys == sorted(keys)


def test_the_stage_passes_the_configured_sample_seconds_to_the_port(
    parse_settings, archive, demo
) -> None:
    """The sample-point times are a setting and not code (AD-3)."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    assert parser.seen_seconds == [tuple(parse_settings.snapshot_seconds)]


def test_a_ticks_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_ticks(rounds).drop("area")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, ticks=broken), demo)
    assert "area" in str(exc.value)
    assert "sample-point table" in str(exc.value)


def test_an_extra_ticks_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_ticks(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, ticks=broken), demo)
    assert "ylimaarainen" in str(exc.value)


def test_a_lineups_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_lineups(rounds).drop("clan_name")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, lineups=broken), demo)
    assert "clan_name" in str(exc.value)
    assert "lineup table" in str(exc.value)


def test_an_extra_lineups_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_lineups(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, lineups=broken), demo)
    assert "ylimaarainen" in str(exc.value)
    assert "lineup table" in str(exc.value)


def test_an_empty_ticks_table_with_rounds_is_refused(
    parse_settings, archive, demo
) -> None:
    """Rounds but not a single sample point is an error, not an ok result.

    An empty setup table would stay permanently skipped on the strength of
    the manifest, and aggregation would report a map without a single setup
    -- exactly the silent emptiness the whole contract is meant to prevent.
    """
    rounds = build_rounds(played=2, warmup=0)
    empty = pl.DataFrame(schema=dict(TICKS_ADAPTER_SCHEMA))
    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, ticks=empty), demo)
    assert "not a single sample point" in str(exc.value)

    assert not archive.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_a_failure_leaves_no_partial_ticks_table(
    parse_settings, archive, demo
) -> None:
    with pytest.raises(ParseError):
        run_parse(parse_settings, archive, FakeParser(error=ParseError("rikki")), demo)
    assert not archive.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert not has_temp_leftovers(archive.root)


def test_a_missing_ticks_table_forces_a_reparse(
    parse_settings, archive, demo
) -> None:
    """Half a result is not an up-to-date result."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "ticks").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_an_archive_parsed_by_an_older_version_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """A Story 1.3 archive must not be left without a ``ticks.parquet``.

    ``ParseSettings`` did not change in Story 2.1, so ``params_hash`` is
    identical. The manifest's ``outputs_present()`` checks only the paths the
    manifest **on disk** names -- and the old manifest names only the rounds
    table. Without a separate check the run would be skipped, the setup table
    would never be born, and the user would be told "the result is up to
    date".
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)

    # Wind the archive back to Story 1.3's shape: the manifest names only the
    # rounds table and there is no ticks table.
    manifest_path = archive.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = [f"parsed/{MAP_DEMO_ID}/rounds.parquet"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive.parsed_table(MAP_DEMO_ID, "ticks").unlink()

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped, "the old archive would have been left without a setup table"
    assert parser.calls == 2
    assert archive.parsed_table(MAP_DEMO_ID, "ticks").is_file()


def test_a_failed_second_write_never_looks_up_to_date(
    parse_settings, archive, demo, monkeypatch
) -> None:
    """Writing the three tables is one transaction.

    If the ticks write fails in successive blocks, the archive would be left
    with a rounds table without its pair -- and because the manifest would be
    written anyway, the next run would skip the stage and cheerfully give the
    round count.
    """
    original_write = pl.DataFrame.write_parquet
    calls = {"n": 0}

    def failing(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("levy täyttyi kesken kirjoituksen")
        return original_write(self, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", failing)

    parser = FakeParser(build_rounds(played=5, warmup=0))
    with pytest.raises(OSError):
        run_parse(parse_settings, archive, parser, demo)

    monkeypatch.undo()

    # Not one table was left in place, and the manifest tells of the error.
    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert not archive.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert not archive.parsed_table(MAP_DEMO_ID, "events").exists()
    assert not has_temp_leftovers(archive.root)
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"

    # And the next run does not skip.
    result = run_parse(parse_settings, archive, FakeParser(build_rounds(5, warmup=0)), demo)
    assert not result.skipped
    assert result.stats["rounds"] == 5
    assert result.stats["sample_rounds"] == 5


def test_ticks_are_sorted_deterministically_by_kind_too(
    parse_settings, archive, demo
) -> None:
    """First contact can land on exactly the configured second.

    Without ``sample_kind`` in the sort key, the order of two rows would
    depend on the input order, and the same demo would produce different bytes
    on different runs.
    """
    rounds = build_rounds(played=2, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(1, 2), contact_t_s=6.0)
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    same_second = df.filter(pl.col("sample_t_s") == 6.0)
    assert set(same_second["sample_kind"].unique()) == {"time", "first_contact"}
    # sample_kind is an Enum, so Polars sorts it in the order of the list
    # (time, first_contact) and not alphabetically. Either will do; what
    # matters is that the order is determined and not random.
    kind_order = {name: index for index, name in enumerate(SAMPLE_KINDS)}
    side_order = {name: index for index, name in enumerate(SIDES)}
    keys = [
        (
            row["round_no"],
            kind_order[row["sample_kind"]],
            side_order[row["side"]],
            row["player_id"],
        )
        for row in same_second.iter_rows(named=True)
    ]
    assert keys == sorted(keys)


def test_unreadable_ticks_do_not_hide_the_round_counts(
    parse_settings, archive, demo
) -> None:
    """One broken table must not take another's numbers away."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=4, warmup=0)), demo)
    archive.parsed_table(MAP_DEMO_ID, "ticks").write_bytes(b"ei parquetia")

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert result.skipped
    assert result.stats["rounds"] == 4
    assert "ticks_unreadable" in result.stats
    assert "unreadable" not in result.stats


def test_skipped_run_reports_the_tick_counts_too(
    parse_settings, archive, demo
) -> None:
    """A skipped run reads the numbers from the finished tables, not the
    demo."""
    rounds = build_rounds(played=3, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(2,))
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)

    result = run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)
    assert result.skipped
    assert result.stats["tick_rows"] == 70  # 60 time points + 10 first contacts
    assert result.stats["first_contact_rounds"] == 1


# --- The event table -----------------------------------------------------------


def test_writes_a_valid_events_table(parse_settings, archive, demo) -> None:
    """Acceptance criterion: ``events.parquet`` passes ``validate(EVENTS)``."""
    rounds = build_rounds(played=3, warmup=0)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo
    )

    table = archive.parsed_table(MAP_DEMO_ID, "events")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert list(df.columns) == list(EVENTS)
    assert df.schema == dict(EVENTS)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    # 3 rounds x 2 teams x 2 grenades x 2 rows.
    assert df.height == 24
    assert result.stats["event_rows"] == 24
    assert result.stats["utility_throws"] == 12
    assert result.stats["utility_detonations"] == 12
    assert result.stats["utility_rounds"] == 3


def test_every_grenade_has_at_most_one_throw_and_one_detonation(
    parse_settings, archive, demo
) -> None:
    """Acceptance criterion: a pair is a pair, not three rows."""
    rounds = build_rounds(played=4, warmup=0)
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    counts = df.group_by("round_no", "grenade_entity_id", "event_kind").len()
    assert counts["len"].max() == 1


def test_the_trajectory_id_is_unique_in_the_written_table(
    parse_settings, archive, demo
) -> None:
    """Acceptance criterion: ``(grenade_no, event_kind)`` is unique.

    The material **does** hold a recycled id, so the old key collides within
    the same table. Without that the test would also pass when ``grenade_no``
    does nothing.

    The claim concerns the whole table, not a round: a per-round id would look
    just as good here, but would fail the moment aggregation joins many rounds
    into one frame.
    """
    rounds = build_rounds(played=4, warmup=0)
    events = build_events(rounds, recycle_entity_ids=True)
    run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    assert df["grenade_no"].null_count() == 0
    assert df.select("map_demo_id", "grenade_no", "event_kind").is_unique().all()

    # The old key is **not** unique in this same table.
    old_key = df.select("map_demo_id", "round_no", "grenade_entity_id", "event_kind")
    assert not old_key.is_unique().all()


def test_joining_utility_on_the_new_key_does_not_duplicate_rows(
    parse_settings, archive, demo
) -> None:
    """Acceptance criterion: a join on the new id does not multiply rows.

    The join is made from the table to itself on the key, because that is the
    claim: a row fetched by the key is one row. The same join on the old key
    multiplies the rows, and both numbers are checked -- otherwise the test
    would also pass on a table that has no key at all.
    """
    rounds = build_rounds(played=3, warmup=0)
    events = build_events(rounds, recycle_entity_ids=True)
    run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    new_key = ["map_demo_id", "grenade_no", "event_kind"]
    on_new = df.join(df.select(new_key), on=new_key, how="inner")
    assert on_new.height == df.height

    old_key = ["map_demo_id", "round_no", "grenade_entity_id", "event_kind"]
    on_old = df.join(df.select(old_key), on=old_key, how="inner")
    assert on_old.height > df.height


def test_the_stage_passes_the_adapters_numbers_through_unchanged(
    parse_settings, archive, demo
) -> None:
    """The stage does not renumber the rows -- the number comes from the
    adapter.

    Without this the stage could give running numbers of its own, and every
    other new test would still pass: the result would still be unique, but it
    would no longer be the same id as the trajectory's.
    """
    rounds = build_rounds(played=3, warmup=2)
    events = build_events(rounds)
    run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    written = dict(
        zip(
            zip(df["grenade_no"].to_list(), df["event_kind"].to_list()),
            df["t_s"].to_list(),
        )
    )
    given = dict(
        zip(
            zip(events["grenade_no"].to_list(), events["event_kind"].to_list()),
            events["t_s"].to_list(),
        )
    )
    # The rows of unnumbered rounds fall away; those that remain carry the
    # adapter's number as it stands, and the number points at the same event.
    assert written
    assert set(written) < set(given)
    for key, t_s in written.items():
        assert given[key] == t_s

    # And the numbers start from 500 as the adapter gave them -- the stage
    # does not renumber from zero.
    assert min(no for no, _ in written) >= 500


def test_a_duplicate_trajectory_id_is_refused(
    parse_settings, archive, demo
) -> None:
    """A contract break is raised as an error and not written to the archive.

    ``validate`` checks the columns and the types but not the key, so a
    duplicate would pass it and would show only in the report's numbers, as
    smoke counted twice.
    """
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds)
    broken = events.with_columns(
        pl.lit(500, dtype=pl.Int32).alias("grenade_no")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, events=broken), demo)

    assert "grenade_no" in str(exc.value)
    assert not archive.parsed_table(MAP_DEMO_ID, "events").is_file()


def test_a_missing_trajectory_id_is_refused(
    parse_settings, archive, demo
) -> None:
    """An empty number would leave the row with no tie to its pair."""
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds)
    broken = events.with_columns(
        pl.lit(None, dtype=pl.Int32).alias("grenade_no")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, events=broken), demo)

    assert "grenade_no" in str(exc.value)


def test_a_stale_table_missing_a_column_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """A schema change invalidates the archive without touching the manifest.

    The parameter hash is computed from the ``[parse]`` section and
    demoparser2's version (AD-3), and neither moves when ``EVENTS`` gains a
    new column. Without the schema check the old table would stay silently in
    force and would look up to date. The three other "old archive" tests do
    not cover this: two delete a file and the third overwrites
    ``params_hash``.
    """
    parser = FakeParser(build_rounds(played=3, warmup=0))
    run_parse(parse_settings, archive, parser, demo)

    table = archive.parsed_table(MAP_DEMO_ID, "events")
    manifest_before = archive.parsed_manifest(MAP_DEMO_ID).read_text(encoding="utf-8")
    pl.read_parquet(table).drop("grenade_no").write_parquet(table)

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped
    assert parser.calls == 2
    fresh = pl.read_parquet(table)
    assert "grenade_no" in fresh.columns
    assert fresh.schema == dict(EVENTS)
    # The manifest matched the whole time -- the rerun was set off by the
    # schema.
    assert manifest_before != ""


def test_an_unexploded_grenade_has_no_invented_detonation(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: the trajectory breaks off -> only ``grenade_thrown``."""
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds, unexploded=(1,))
    result = run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    lone_grenade = df.filter(pl.col("grenade_entity_id") == 1)
    assert lone_grenade["event_kind"].to_list() == ["grenade_thrown"]
    assert result.stats["utility_throws"] - result.stats["utility_detonations"] == 1


def test_events_get_the_round_number_from_the_rounds_table(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds(played=3, warmup=0)
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))

    assert df["round_no"].null_count() == 0
    assert sorted(df["round_no"].unique().to_list()) == [1, 2, 3]


def test_unnumbered_rounds_produce_no_event_rows(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: a throw on an unnumbered round -> no rows."""
    rounds = build_rounds(played=2, warmup=3)
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]


def test_a_round_without_an_anchor_has_no_event_rows(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: a round without an anchor -> no rows (``t_s`` undefined)."""
    rounds = build_rounds(3, warmup=0, without_anchor=(2,))
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    assert sorted(df["round_no"].unique().to_list()) == [1, 3]


def test_an_empty_events_table_is_a_valid_result(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: a demo without utility -> an empty ``events.parquet``.

    Unlike an empty rounds or sample-point table, this is not an error: a demo
    always holds played rounds, but utility can genuinely be absent. An error
    would block the parse of the whole demo over a piece of information that
    is itself an observation.
    """
    rounds = build_rounds(played=2, warmup=0)
    empty = pl.DataFrame(schema=dict(EVENTS_ADAPTER_SCHEMA))
    result = run_parse(parse_settings, archive, FakeParser(rounds, events=empty), demo)

    assert result.status == "ok"
    table = archive.parsed_table(MAP_DEMO_ID, "events")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert df.is_empty()
    assert df.schema == dict(EVENTS)
    assert result.stats["event_rows"] == 0
    assert result.stats["utility_throws"] == 0


def test_events_without_an_area_are_counted(parse_settings, archive, demo) -> None:
    """I/O matrix: a detonation far from everything -> ``area = null``, no
    drop."""
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds, without_area=(2, 4))
    result = run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    without_area_rows = df.filter(pl.col("area").is_null())
    assert without_area_rows.height == 4  # two grenades x two rows
    # The coordinates survive although the area was not settled.
    assert without_area_rows["x"].null_count() == 0
    assert without_area_rows["area_source"].null_count() == 4
    assert result.stats["utility_without_area"] == 4


def test_observed_and_derived_areas_are_counted_separately(
    parse_settings, archive, demo
) -> None:
    """An observation and an estimate are different kinds of information and
    must not be bundled.

    Without the distinction the report would present the detonation's estimate
    as being as certain as the thrower's own area.
    """
    rounds = build_rounds(played=2, warmup=0)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo
    )
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))

    throws = df.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = df.filter(pl.col("event_kind") == "grenade_detonate")
    assert throws["area_source"].unique().to_list() == ["observed"]
    assert detonations["area_source"].unique().to_list() == ["point_cloud"]
    # Only the estimate has a snap distance -- an observation is no distance
    # away from anything.
    assert throws["snap_distance"].null_count() == throws.height
    assert detonations["snap_distance"].null_count() == 0
    assert result.stats["utility_area_observed"] == throws.height
    assert result.stats["utility_area_point_cloud"] == detonations.height


def test_utility_on_unnumbered_rounds_is_counted_not_just_dropped(
    parse_settings, archive, demo
) -> None:
    """The three other drop reasons are reported -- this must not be an
    exception."""
    rounds = build_rounds(played=2, warmup=3)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo
    )
    # 3 unnumbered rounds x 2 teams x 2 grenades = 12 throws.
    assert result.stats["utility_unnumbered_rounds"] == 12


def test_lineup_keys_join_from_events_to_rounds(
    parse_settings, archive, demo
) -> None:
    """The thrower's team is the same as in the rounds table; no crossover."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    rounds_list = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    events = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))

    joined = events.join(
        rounds_list.select("map_demo_id", "round_no", "lineup_key", "side"),
        on=["map_demo_id", "round_no", "lineup_key", "side"],
        how="inner",
    )
    assert joined.height == events.height


def test_event_rows_are_sorted_deterministically(
    parse_settings, archive, demo
) -> None:
    """A grenade's throw always comes before its own detonation."""
    rounds = build_rounds(played=3, warmup=0)
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    keys = list(zip(df["round_no"].to_list(), df["grenade_entity_id"].to_list()))
    assert keys == sorted(keys)
    for _, group in df.group_by("grenade_entity_id", maintain_order=True):
        assert group["event_kind"].to_list()[0] == "grenade_thrown"


def test_an_events_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_events(rounds).drop("area")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, events=broken), demo)
    assert "area" in str(exc.value)
    assert "event table" in str(exc.value)


def test_a_missing_events_table_forces_a_reparse(
    parse_settings, archive, demo
) -> None:
    """Half a result is not an up-to-date result."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "events").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_an_archive_parsed_before_utility_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """A Story 2.1 archive must not be left without an ``events.parquet``.

    The same trap as in Story 2.1: ``ParseSettings`` changed only in the
    default of the ``area_snap_units`` field, and if that is the same,
    ``params_hash`` is identical. The manifest's ``outputs_present()`` checks
    only the paths the manifest **on disk** names.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)

    manifest_path = archive.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = [
        f"parsed/{MAP_DEMO_ID}/rounds.parquet",
        f"parsed/{MAP_DEMO_ID}/ticks.parquet",
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive.parsed_table(MAP_DEMO_ID, "events").unlink()

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped, "the old archive would have been left without a utility table"
    assert archive.parsed_table(MAP_DEMO_ID, "events").is_file()


def test_unreadable_events_do_not_hide_the_other_counts(
    parse_settings, archive, demo
) -> None:
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=4, warmup=0)), demo)
    archive.parsed_table(MAP_DEMO_ID, "events").write_bytes(b"ei parquetia")

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert result.skipped
    assert result.stats["rounds"] == 4
    assert "events_unreadable" in result.stats
    assert "unreadable" not in result.stats


def test_skipped_run_reports_the_event_counts_too(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds(played=3, warmup=0)
    events = build_events(rounds)
    parser = FakeParser(rounds, events=events)
    run_parse(parse_settings, archive, parser, demo)

    result = run_parse(parse_settings, archive, parser, demo)
    assert result.skipped
    assert result.stats["utility_throws"] == 12
    assert result.stats["utility_detonations"] == 12


# --- Skipping ------------------------------------------------------------------


def test_second_run_is_skipped(parse_settings, archive, demo) -> None:
    parser = FakeParser(build_rounds(played=21))
    run_parse(parse_settings, archive, parser, demo)
    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    before = table.stat().st_mtime_ns

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert parser.calls == 1, "the demo must not be parsed again"
    assert table.stat().st_mtime_ns == before, "the file must not be written again"
    assert result.stats["rounds"] == 21
    assert "up to date" in (result.reason or "")


def test_force_overrides_a_matching_manifest(parse_settings, archive, demo) -> None:
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    result = run_parse(parse_settings, archive, parser, demo, force=True)

    assert not result.skipped
    assert parser.calls == 2


def test_changed_demo_bytes_trigger_a_reparse(parse_settings, archive, demo) -> None:
    """The manifest alone is not enough: a stale result must not be permanent."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    assert run_parse(parse_settings, archive, parser, demo).skipped

    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 2048)  # different content, different size

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_threshold_change_does_not_trigger_a_reparse(
    tmp_path: Path, archive, demo
) -> None:
    """AD-3: adjusting the thresholds must not invalidate the parse."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"full_equip_min = 4000": "full_equip_min = 4100"}
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    run_parse(load_settings(base_toml, env_files=()).parse, archive, parser, demo)
    result = run_parse(load_settings(changed_toml, env_files=()).parse, archive, parser, demo)

    assert result.skipped
    assert parser.calls == 1


def test_parse_setting_change_triggers_a_reparse(tmp_path: Path, archive, demo) -> None:
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root,
            **{
                "snapshot_seconds = [6.0, 15.0, 30.0, 45.0]": (
                    "snapshot_seconds = [6.0, 15.0, 30.0, 50.0]"
                )
            },
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    run_parse(load_settings(base_toml, env_files=()).parse, archive, parser, demo)
    result = run_parse(load_settings(changed_toml, env_files=()).parse, archive, parser, demo)

    assert not result.skipped
    assert parser.calls == 2


def test_params_hash_covers_the_weapon_classification(
    parse_settings, monkeypatch
) -> None:
    """A change to the weapon classification invalidates the archive even
    though the settings do not change.

    The classification is code and not a setting, so the hash of the
    ``[parse]`` section alone would leave its change invisible: the table
    would have been computed with the old weapon list, the manifest would
    match, and a stale counter would stay silently in the archive. The
    alternative would be a version number raised by hand -- and that works
    only if nobody forgets.
    """
    before = parse_stage._params_hash(parse_settings)
    monkeypatch.setattr(
        parse_stage, "weapon_classification_digest", lambda: "toinen-tiiviste"
    )
    assert parse_stage._params_hash(parse_settings) != before


def test_params_hash_still_covers_the_parse_section(
    tmp_path: Path, archive
) -> None:
    """The hash covers the whole ``ParseSettings`` section too -- established.

    ``_params_hash`` dumps the section as it stands, so a new field *should*
    reach the hash automatically. That is exactly why it is checked: a silent
    exception (an ``exclude`` list, say) would otherwise go unnoticed, and
    adding the classification's digest to the dict is precisely the kind of
    place where the section could have been left out.
    """
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root,
            **{"area_snap_units = 256": "area_snap_units = 257"},
        ),
        encoding="utf-8",
    )

    base = load_settings(base_toml, env_files=()).parse
    changed = load_settings(changed_toml, env_files=()).parse
    assert "area_snap_units" in base.model_dump(mode="json")
    assert parse_stage._params_hash(base) != parse_stage._params_hash(changed)


def test_params_hash_keeps_the_section_and_the_digest_apart(
    parse_settings, monkeypatch
) -> None:
    """The settings section and the digest are on different levels, not
    siblings.

    As a sibling key, a ``[parse]`` setting of the same name could mask the
    digest, and that would have to be fended off with a guard that nothing can
    trip. The two-level structure makes the collision impossible, and this
    establishes that both halves really are in the hash: a change to either is
    enough.
    """
    before = parse_stage._params_hash(parse_settings)

    monkeypatch.setattr(
        parse_stage, "weapon_classification_digest", lambda: "toinen-tiiviste"
    )
    only_digest_changed = parse_stage._params_hash(parse_settings)
    assert only_digest_changed != before

    other_section = parse_settings.model_copy(update={"area_snap_units": 501})
    assert parse_stage._params_hash(other_section) != only_digest_changed


def test_old_table_without_the_column_is_reparsed_not_rejected(
    parse_settings, archive, demo
) -> None:
    """An archive's old ``rounds.parquet`` without the new column does not
    fail the run.

    A manifest written by the old code was hashed without the kit threshold,
    so the skip condition is not met and the demo is parsed again. A schema
    error therefore leads to **a run**, not to an exception -- and the old
    table does not stay silently in force.
    """
    parser = FakeParser(build_rounds(played=3))
    run_parse(parse_settings, archive, parser, demo)

    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    old = pl.read_parquet(table).drop(ARMED_COLUMN)
    old.write_parquet(table)
    manifest_path = archive.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["params_hash"] = "vanha-hash-ilman-kalustolaskuria"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped
    assert parser.calls == 2
    fresh = pl.read_parquet(table)
    assert ARMED_COLUMN in fresh.columns
    assert fresh.schema == dict(ROUNDS)


def test_armed_count_survives_the_write(parse_settings, archive, demo) -> None:
    """The counter travels unchanged from the adapter all the way to disk."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=3)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df[ARMED_COLUMN].dtype == pl.Int32
    assert df.filter(pl.col("side") == "T")[ARMED_COLUMN].to_list() == [5, 5, 5]
    assert df.filter(pl.col("side") == "CT")[ARMED_COLUMN].to_list() == [4, 4, 4]


def test_run_reports_the_armed_distribution(parse_settings, archive, demo) -> None:
    """``run()`` returns the keys the output reads.

    Without this the producer and the consumer are tested only separately: the
    statistics function against a hand-built table and the output against a
    hand-written dict. When ``stats.update(_armed_stats(df))`` was removed
    from between them, 124 tests passed and the "Aseistettuja" line simply
    vanished from the output.

    ``build_rounds`` gives T 5 and CT 4 on every round, so the distribution is
    known exactly.
    """
    result = run_parse(
        parse_settings, archive, FakeParser(build_rounds(played=3)), demo
    )

    assert result.stats["armed_distribution"] == {4: 3, 5: 3}
    assert result.stats["armed_missing"] == 0


def test_skipped_run_reports_the_armed_distribution_too(
    parse_settings, archive, demo
) -> None:
    """A skipped run reads the distribution from the finished table, not from
    memory.

    A skipped run does **not** report unknown names: they are not in the
    table, because they arm nobody, and they cannot be read back without the
    demo. A missing key is therefore the right result -- an invented empty
    list would claim there had been no unknowns.
    """
    parser = FakeParser(build_rounds(played=3))
    run_parse(parse_settings, archive, parser, demo)

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert result.stats["armed_distribution"] == {4: 3, 5: 3}
    assert "armed_unknown_items" not in result.stats


def test_armed_distribution_counts_rounds_without_an_anchor_as_missing(
    parse_settings, archive, demo
) -> None:
    """A round without an anchor is not zero but a missing observation."""
    parser = FakeParser(build_rounds(played=3, without_anchor=(2,)))
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armed_missing"] == 2  # one row per team
    assert result.stats["armed_distribution"] == {4: 2, 5: 2}


def test_armored_count_survives_the_write(parse_settings, archive, demo) -> None:
    """The armour counter travels unchanged from the adapter to disk."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=3)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df[ARMORED_COLUMN].dtype == pl.Int32
    assert df[ARMORED_COLUMN].to_list() == [5] * 6
    # A different column and not the same one twice: the armed distribution
    # is another one.
    assert df[ARMED_COLUMN].to_list() != df[ARMORED_COLUMN].to_list()


def test_run_reports_the_armored_distribution(parse_settings, archive, demo) -> None:
    """``run()`` returns the keys the armour line reads.

    The same wiring test as with the armed distribution and for the same
    reason: without it the producer and the consumer are tested only
    separately, and a missing ``stats.update(_armored_stats(df))`` in between
    would simply drop the line from the output without a single test failing.
    """
    result = run_parse(
        parse_settings, archive, FakeParser(build_rounds(played=3)), demo
    )

    assert result.stats["armored_distribution"] == {5: 6}
    assert result.stats["armored_missing"] == 0
    # Two different distributions from the same run -- that is exactly what
    # the column is for.
    assert result.stats["armed_distribution"] == {4: 3, 5: 3}


def test_skipped_run_reports_the_armored_distribution_too(
    parse_settings, archive, demo
) -> None:
    """A skipped run reads the armour distribution from the finished table,
    not from memory."""
    parser = FakeParser(build_rounds(played=3))
    run_parse(parse_settings, archive, parser, demo)

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert result.stats["armored_distribution"] == {5: 6}


def test_armored_distribution_counts_rounds_without_an_anchor_as_missing(
    parse_settings, archive, demo
) -> None:
    """A round without an anchor is not zero armour but a missing
    observation."""
    parser = FakeParser(build_rounds(played=3, without_anchor=(2,)))
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armored_missing"] == 2  # one row per team
    assert result.stats["armored_distribution"] == {5: 4}


def test_an_old_table_without_the_armored_column_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """An archive's old ``rounds.parquet`` without the armour column does not
    fail the run.

    The I/O matrix's "old archive" row. **The manifest is not touched**: it
    matches, and that is the heart of the test. The schema check alone is
    enough to force a rerun, so the user does not need to know about the
    ``--force`` flag and the old table does not stay silently in force.
    """
    parser = FakeParser(build_rounds(played=3))
    run_parse(parse_settings, archive, parser, demo)

    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(table).drop(ARMORED_COLUMN).write_parquet(table)

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped
    assert parser.calls == 2
    fresh = pl.read_parquet(table)
    assert ARMORED_COLUMN in fresh.columns
    assert fresh.schema == dict(ROUNDS)


def test_an_armored_count_above_its_divisor_is_refused(
    parse_settings, archive, demo
) -> None:
    """``0 <= armoured <= players_buy_end`` is enforced at read time.

    The schema's docstring promises the bound, but ``validate`` checks only
    the types. Without a value check an impossible number would be written
    into the archive and would show in the report as "6 (1/1 kierroksesta)"
    for a team of five players.
    """
    rounds = build_rounds(played=3).with_columns(
        pl.when(pl.col("round_raw") == 2)
        .then(6)
        .otherwise(pl.col(ARMORED_COLUMN))
        .cast(pl.Int32)
        .alias(ARMORED_COLUMN)
    )

    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds), demo)
    assert ARMORED_COLUMN in str(exc.value)
    assert "players_buy_end" in str(exc.value)


def test_an_armed_count_above_its_divisor_is_refused(
    parse_settings, archive, demo
) -> None:
    """The same bound applies to the kit counter -- it was unenforced before
    this."""
    rounds = build_rounds(played=3).with_columns(
        pl.when(pl.col("round_raw") == 2)
        .then(6)
        .otherwise(pl.col(ARMED_COLUMN))
        .cast(pl.Int32)
        .alias(ARMED_COLUMN)
    )

    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds), demo)
    assert ARMED_COLUMN in str(exc.value)


def test_more_armed_than_armored_players_is_refused(
    parse_settings, archive, demo
) -> None:
    """The armed must be a subset of the armoured.

    The armed condition includes armour, so exceeding it would mean the
    counters are reading a different tick or a different set of players -- a
    defect that would show in the report only as two plausible-looking
    numbers.
    """
    rounds = build_rounds(played=3).with_columns(
        pl.when(pl.col("round_raw") == 2)
        .then(1)
        .otherwise(pl.col(ARMORED_COLUMN))
        .cast(pl.Int32)
        .alias(ARMORED_COLUMN)
    )

    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds), demo)
    assert "subset of the armoured" in str(exc.value)


def test_a_null_counter_is_not_an_invariant_break(
    parse_settings, archive, demo
) -> None:
    """A round without an anchor passes the check: null is an honest gap.

    Without this counterpart the previous three tests would also pass on an
    implementation that rejects every empty counter.
    """
    result = run_parse(
        parse_settings,
        archive,
        FakeParser(build_rounds(played=3, without_anchor=(2,))),
        demo,
    )
    assert result.status == "ok"


def test_run_reports_the_unknown_inventory_items(
    parse_settings, archive, demo
) -> None:
    """Unknown inventory names travel from the diagnostics into the numbers.

    Without this the producer and the consumer are tested only separately: the
    adapter collects the names and the output knows how to format them, but
    the one line that moves them across would be missing from between. An
    unknown name arms nobody, so without the output a new weapon would look
    exactly like a new knife skin: the distribution would simply drift
    silently downwards.
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        unknown_inventory_items=(("Ei-Ole-Olemassa-9000", 3), ("Uusi Ase", 1)),
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armed_unknown_items"] == (
        ("Ei-Ole-Olemassa-9000", 3),
        ("Uusi Ase", 1),
    )


def test_run_reports_an_empty_unknown_list_as_empty(
    parse_settings, archive, demo
) -> None:
    """Empty is a different thing from missing.

    An empty list is a fresh run in which every name was recognised; a missing
    key is a skipped run from which the names cannot be read back. Only of the
    former may one say "none at all".
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0, tick_rate_measured=True, rounds_seen=3
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armed_unknown_items"] == ()


def test_fresh_run_without_diagnostics_is_not_the_same_as_a_skipped_one(
    parse_settings, archive, demo
) -> None:
    """A port that does not report unknowns gets a state of its own.

    Three states have to be kept apart: the key is missing (a skipped run, the
    names cannot be read back), ``None`` (a fresh run, the port does not say)
    and empty (a fresh run, every name was recognised). Without the difference
    the output would claim the same about a run without diagnostics as about a
    skipped one.
    """
    parser = FakeParser(build_rounds(played=3))
    assert not hasattr(parser, "diagnostics")

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped
    assert "armed_unknown_items" in result.stats
    assert result.stats["armed_unknown_items"] is None


def test_unreadable_armed_rows_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """The kit counter's read errors travel from the diagnostics into the
    numbers.

    The number is a defect and not an observation: without it the counter
    could be empty for the whole demo because of a prop fault, and the result
    would just look like saving rounds.
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        armed_unreadable_rows=2,
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armed_unreadable_rows"] == 2


def test_the_two_unreadable_counters_reach_the_stats_separately(
    parse_settings, archive, demo
) -> None:
    """Two numbers and not one: the difference says which prop failed.

    The counters' readability conditions differ -- the armour counter does not
    read the inventory -- so a shared number would not tell a row on which the
    armour went unread from a row on which only the inventory failed. That is
    exactly the claim the whole two-column solution makes, and it cannot be
    read from the finished table.
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        armed_unreadable_rows=5,
        armored_unreadable_rows=2,
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armed_unreadable_rows"] == 5
    assert result.stats["armored_unreadable_rows"] == 2
    # The difference is "the rows on which only the inventory failed".
    assert (
        result.stats["armed_unreadable_rows"]
        - result.stats["armored_unreadable_rows"]
        == 3
    )


def test_match_restarts_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """The number of restarts travels from the diagnostics into the numbers.

    A match restart produces no row in any table, so its count **cannot be
    computed from the finished result**. Without this one line the drop would
    be silent: the adapter would know it, but nobody would say so.
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=4,
        match_restarts=1,
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["match_restarts"] == 1


def test_zero_match_restarts_is_not_the_same_as_no_answer(
    parse_settings, archive, demo
) -> None:
    """Three states are kept apart, as with the unknown items.

    A port that does not report restarts gets ``None``; a port that reports
    zero gets zero. In a skipped run there is no key at all. Without the
    difference a demo served from the cache would silently claim "no restart".
    """
    reporting = FakeParser(build_rounds(played=3))
    reporting.diagnostics = ParseDiagnostics(
        tick_rate=64.0, tick_rate_measured=True, rounds_seen=3, match_restarts=0
    )
    assert run_parse(parse_settings, archive, reporting, demo).stats[
        "match_restarts"
    ] == 0

    silent = FakeParser(build_rounds(played=3))
    assert not hasattr(silent, "diagnostics")
    result = run_parse(parse_settings, archive, silent, demo, force=True)
    assert "match_restarts" in result.stats
    assert result.stats["match_restarts"] is None


def test_missing_output_forces_a_reparse(parse_settings, archive, demo) -> None:
    """The sync client may still be moving the result -- the manifest alone is
    not enough."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "rounds").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_unreadable_result_is_reported_not_zeroed(
    parse_settings, archive, demo
) -> None:
    """A row of zeroes would look as though the demo held no rounds at all."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    archive.parsed_table(MAP_DEMO_ID, "rounds").write_bytes(b"ei parquetia")

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert result.skipped
    assert "unreadable" in result.stats
    assert "rounds" not in result.stats


# --- Errors --------------------------------------------------------------------


def test_parse_error_is_recorded_in_the_manifest(parse_settings, archive, demo) -> None:
    parser = FakeParser(error=ParseError("Demo on katkennut kesken latauksen."))
    with pytest.raises(ParseError, match="katkennut"):
        run_parse(parse_settings, archive, parser, demo)

    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "parse_failed"
    assert "katkennut" in (manifest.reason or "")
    assert manifest.outputs == []


def test_schema_error_is_recorded_too(parse_settings, archive, demo) -> None:
    """A contract break too is the unit's status, not a trackless crash."""
    frame = build_rounds().drop("survivors")
    with pytest.raises(SchemaError):
        run_parse(parse_settings, archive, FakeParser(frame), demo)

    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "parse_failed"
    assert "survivors" in (manifest.reason or "")


def test_parse_error_leaves_no_partial_table(parse_settings, archive, demo) -> None:
    parser = FakeParser(error=ParseError("rikki"))
    with pytest.raises(ParseError):
        run_parse(parse_settings, archive, parser, demo)

    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert not has_temp_leftovers(archive.root)


def test_failure_never_overwrites_a_valid_result(parse_settings, archive, demo) -> None:
    """A valid table with a manifest claiming failure is the worst pair."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=4)), demo)
    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    before = table.read_bytes()

    # The same demo and the same settings -> a skip, so the run is forced.
    with pytest.raises(ParseError):
        run_parse(
            parse_settings,
            archive,
            FakeParser(error=ParseError("rikki")),
            demo,
            force=True,
        )

    assert table.read_bytes() == before
    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "ok", "an intact result must not be marked failed"


def test_failed_manifest_is_not_treated_as_current(
    parse_settings, archive, demo
) -> None:
    broken = FakeParser(error=ParseError("rikki"))
    with pytest.raises(ParseError):
        run_parse(parse_settings, archive, broken, demo)

    intact_parser = FakeParser()
    result = run_parse(parse_settings, archive, intact_parser, demo)
    assert not result.skipped
    assert result.status == "ok"


def test_zero_played_rounds_is_an_error_not_an_empty_result(
    parse_settings, archive, demo
) -> None:
    """An empty table would stay permanently skipped on the manifest."""
    frame = build_rounds(played=0, warmup=3)
    with pytest.raises(ParseError, match="No played round was found"):
        run_parse(parse_settings, archive, FakeParser(frame), demo)

    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_a_missing_demo_names_the_paths_that_were_searched(
    parse_settings, archive
) -> None:
    with pytest.raises(DemoUnavailable) as exc:
        parse_stage.run(parse_settings, archive, MAP_DEMO_ID, FakeParser())
    assert "was not found" in str(exc.value)


def test_unreadable_demo_does_not_get_a_shared_fingerprint(
    parse_settings, archive, tmp_path
) -> None:
    """A shared fallback constant would make two demos the same input."""
    missing = tmp_path / "kadonnut.dem"
    with pytest.raises(DemoUnavailable):
        run_parse(parse_settings, archive, FakeParser(), missing)


# --- Contract checks -------------------------------------------------------------


def test_port_contract_is_checked_exactly(parse_settings, archive, demo) -> None:
    """An extra column breaks the contract just as much as a missing one."""
    frame = build_rounds().with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(frame), demo)
    assert "ylimaarainen" in str(exc.value)


def test_table_that_breaks_the_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    frame = build_rounds().drop("survivors")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(frame), demo)
    assert "survivors" in str(exc.value)


def test_impossible_win_reason_is_refused(parse_settings, archive, demo) -> None:
    """In CS2 T cannot win by ``t_killed`` -- the sides are the wrong way
    round."""
    frame = build_rounds(played=3, warmup=0).with_columns(
        pl.lit("t_killed").alias("win_reason")
    )
    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, FakeParser(frame), demo)
    message = str(exc.value)
    assert "against the rules" in message
    assert "wrong way round" in message
    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()


def test_uneven_row_count_per_round_is_refused(parse_settings, archive, demo) -> None:
    """A third row for a round would distort every later sum."""
    frame = build_rounds(played=3, warmup=0)
    frame = pl.concat([frame, frame.head(1)])
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(frame), demo)
    assert "two rows per round" in str(exc.value)


# --- Interpreting the target -----------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        f"{MAP_DEMO_ID}.dem",
        f"{MAP_DEMO_ID}.dem.zst",
        f"{MAP_DEMO_ID}.dem.gz",
    ],
)
def test_map_demo_id_is_read_from_the_file_name(name: str) -> None:
    assert parse_stage.map_demo_id_from_path(Path("/x") / name) == MAP_DEMO_ID


def test_resolve_demo_accepts_a_file_path(archive, demo) -> None:
    assert parse_stage.resolve_demo(archive, str(demo)) == (MAP_DEMO_ID, demo)


def test_resolve_demo_finds_the_demo_from_the_import_dir(archive, demo) -> None:
    assert parse_stage.resolve_demo(archive, MAP_DEMO_ID) == (MAP_DEMO_ID, demo)


def test_resolve_demo_finds_the_demo_from_the_demos_dir(archive) -> None:
    path = archive.demo(MAP_DEMO_ID)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    assert parse_stage.resolve_demo(archive, MAP_DEMO_ID) == (MAP_DEMO_ID, path)


def test_resolve_demo_lists_the_searched_paths(archive) -> None:
    with pytest.raises(DemoUnavailable) as exc:
        parse_stage.resolve_demo(archive, MAP_DEMO_ID)
    message = str(exc.value)
    assert "demos" in message
    assert "import" in message


def test_unsafe_identifier_is_refused(archive) -> None:
    with pytest.raises(PappascoutError):
        parse_stage.resolve_demo(archive, "../pako")


# --- Wiring the port to the settings ---------------------------------------------


#: A ``[parse]`` setting -> the adapter attribute it is wired to.
#:
#: ``None`` means a setting that does **not** go through the constructor. The
#: list is complete, and :func:`test_every_parse_setting_is_in_the_wiring_map`
#: keeps it so: a new setting fails the test until a place has been decided
#: for it.
PORT_WIRING: dict[str, str | None] = {
    # Given in the parse_demo call, not in the constructor. It has a test of
    # its own: test_the_stage_passes_the_configured_sample_seconds_to_the_port.
    "snapshot_seconds": None,
    "buy_window_seconds": "buy_window_seconds",
    "first_contact_exclude_weapons": "exclude_weapons",
    "first_contact_fallback_death": "fallback_death",
    "area_snap_units": "area_snap_units",
    "callout_grid_units": "callout_grid_units",
    "callout_z_weight": "callout_z_weight",
    "callout_z_tolerance_units": "callout_z_tolerance_units",
}

#: Values that differ **both** from the production settings **and** from the
#: adapter's own defaults. The latter is the whole idea of the test: the
#: adapter's defaults are deliberately the same measured numbers as the
#: settings' defaults, so ``port.x == settings.x`` on its own would also pass
#: when the kwarg has dropped out of the wiring altogether.
DISTINCT_SETTINGS: dict[str, object] = {
    "snapshot_seconds": [7.0, 21.0],
    "buy_window_seconds": 11.0,
    "first_contact_exclude_weapons": ["kuvitteellinen_ase"],
    "first_contact_fallback_death": False,
    "area_snap_units": 199,
    "callout_grid_units": 96,
    "callout_z_weight": 4.5,
    "callout_z_tolerance_units": 33.0,
}


def test_every_parse_setting_is_in_the_wiring_map() -> None:
    """The map has to cover every ``[parse]`` field.

    Without this a new setting could be left unwired to the port, and
    :func:`test_default_parser_hands_every_parse_setting_to_the_adapter` would
    claim a coverage it does not have -- it iterates over this very map.
    """
    assert set(PORT_WIRING) == set(ParseSettings.model_fields)


def test_the_distinct_values_really_differ_from_the_adapter_defaults() -> None:
    """Precondition: a test value that is the adapter's default proves nothing
    about the wiring.

    This very trap was open in Story 2.9: the adapter's defaults
    (``callout_grid_units=32``, ``callout_z_weight=1.0``,
    ``callout_z_tolerance_units=72.0``) are the same numbers as the settings'
    defaults, so removing the wiring line would have left every test green.
    """
    defaults = inspect.signature(Demoparser2Adapter.__init__).parameters
    for field, attribute in PORT_WIRING.items():
        if attribute is None:
            continue
        default = defaults[attribute].default
        assert DISTINCT_SETTINGS[field] != default, (
            f"{field}: the test value is the same as the adapter's default "
            f"{default!r}, so it would not reveal a dropped wiring"
        )


def test_default_parser_hands_every_parse_setting_to_the_adapter() -> None:
    """The wiring is the one place in the code no other test covers.

    Every other test builds the adapter itself and hands it the parameters by
    hand, so if even one kwarg disappeared from here, the whole test set would
    pass and in production the value would silently be its default:
    ``exclude_weapons=()`` would let a utility hit through as first contact,
    and the point cloud's dimensions would be built from the adapter's
    hard-coded numbers although the user had adjusted them -- and because
    their adjustment changes ``params_hash``, they would get a full reparse
    and a "done" summary on an unadjusted grid.

    That is why the values are **all different from the adapter's defaults**;
    the precondition is checked by
    :func:`test_the_distinct_values_really_differ_from_the_adapter_defaults`.
    """
    settings = ParseSettings(**DISTINCT_SETTINGS)
    port = parse_stage.default_parser(settings)

    for field, attribute in PORT_WIRING.items():
        if attribute is None:
            continue
        expected = getattr(settings, field)
        actual = getattr(port, attribute)
        if isinstance(expected, list):
            actual = list(actual)
        assert actual == expected, f"{field} did not reach the port's {attribute} attribute"


def test_default_parser_carries_the_real_settings_too(parse_settings) -> None:
    """The same wiring with production values: a calibrated threshold is not
    None."""
    port = parse_stage.default_parser(parse_settings)
    assert port.area_snap_units == parse_settings.area_snap_units
    assert port.area_snap_units is not None, "the setting is calibrated, not None"
    assert port.callout_grid_units == parse_settings.callout_grid_units
    assert port.callout_z_weight == parse_settings.callout_z_weight
    assert (
        port.callout_z_tolerance_units == parse_settings.callout_z_tolerance_units
    )


def test_default_parser_notices_a_changed_snap_distance(settings_file: Path) -> None:
    """A change to the setting must show at the port, not just in the settings
    object."""
    changed_toml = settings_file.parent / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            settings_file.parent / "arkisto",
            **{"area_snap_units = 256": "area_snap_units = 300"},
        ),
        encoding="utf-8",
    )
    changed_parse_settings = load_settings(changed_toml, env_files=()).parse
    assert parse_stage.default_parser(changed_parse_settings).area_snap_units == 300


def test_changing_the_snap_distance_forces_a_reparse(
    tmp_path: Path, archive, demo
) -> None:
    """``area_snap_units`` changes every row's ``area`` value.

    It therefore has to be in ``params_hash``: otherwise a utility table
    computed with the old bound would stay in the archive, and the user would
    be told "the result is up to date". For comparison, a ``[thresholds]``
    change, which must not reparse -- the same file, a different section.
    """
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"area_snap_units = 256": "area_snap_units = 300"}
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    run_parse(load_settings(base_toml, env_files=()).parse, archive, parser, demo)
    result = run_parse(load_settings(changed_toml, env_files=()).parse, archive, parser, demo)

    assert not result.skipped, "an area computed with the old bound would have stayed in force"
    assert parser.calls == 2


# --- The buy window (Story 1.9) -------------------------------------------------


def test_default_parser_hands_the_buy_window_to_the_adapter(parse_settings) -> None:
    """The buy window has to be wired all the way to the port.

    The adapter's default is **0**, that is, measuring from the anchor. If the
    kwarg were forgotten here, the whole pipeline would silently measure from
    the end of freezetime -- exactly the defect Story 1.9 fixes -- and not one
    other test would notice, because they build the adapter themselves.
    """
    port = parse_stage.default_parser(parse_settings)

    assert port.buy_window_seconds == parse_settings.buy_window_seconds
    assert port.buy_window_seconds > 0, "the setting is a rule of the game, not zero"


def test_changing_the_buy_window_forces_a_reparse(
    tmp_path: Path, archive, demo
) -> None:
    """I/O matrix: ``buy_window_seconds`` changes -> ``parse`` is run again.

    The window moves the measurement moment of every economy row, so the old
    result is not up to date. That is exactly why it is in the ``[parse]``
    section: adjusting the thresholds does not reparse, but moving the
    measurement point does.
    """
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root,
            **{"buy_window_seconds = 20.0": "buy_window_seconds = 5.0"},
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    run_parse(load_settings(base_toml, env_files=()).parse, archive, parser, demo)
    result = run_parse(
        load_settings(changed_toml, env_files=()).parse, archive, parser, demo
    )

    assert not result.skipped
    assert parser.calls == 2


def test_the_knife_round_is_not_counted_in_the_buy_window_numbers(
    parse_settings, archive, demo
) -> None:
    """The buy-window numbers are computed from the **played** rounds.

    The knife round gets a ``round_raw`` of its own, but it is not a round and
    does not end up in the table. The adapter does not know that, so it gives
    the truncations as ``round_raw`` numbers and the stage filters them.
    Without the filtering the user would see the line "3 rounds were measured
    earlier" for a table that holds two -- and the distribution of measurement
    moments would start from the knife round's fraction of a second.

    ``build_rounds`` produces one unnumbered round (``round_raw`` 1) and three
    played ones (2-4), so the truncation numbered 1 is exactly the one that
    has to disappear.
    """

    class _Cutting(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=4,
            buy_window_seconds=20.0,
            # The knife round (1) and two played ones (2, 3); on round 3 one
            # purchase was left behind the truncation.
            buy_window_cuts=((1, 4), (2, 0), (3, 1)),
            buy_window_unchecked_cuts=(1, 2),
        )

    result = run_parse(parse_settings, archive, _Cutting(), demo)
    stats = result.stats

    assert stats["buy_window_truncated_by_death"] == 2
    # The knife round's four lost purchases are not in the table, so they
    # must not be in the number either.
    assert stats["buy_window_purchases_after_cut"] == 1
    assert stats["buy_window_rounds_with_lost_purchases"] == (3,)
    assert stats["buy_window_cuts_unchecked"] == 1


def test_run_reports_the_pawnless_rows(parse_settings, archive, demo) -> None:
    """Story 2.10: the pawnless numbers travel from the diagnostics into the
    numbers.

    The row is not in the table and the dropped point is not among its sample
    points, so neither **can be computed from the finished result**. Without
    this one transfer the producer and the consumer would be tested only
    separately: the adapter counts the skips and the output knows how to
    format them, but the line that carries the number across would be missing
    from between -- and a missing player would look like a round on which the
    team simply played a man down.
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        sample_rows_without_pawn=2,
        sample_points_without_pawn=1,
        grenade_throwers_without_row=3,
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["sample_rows_without_pawn"] == 2
    assert result.stats["sample_points_without_pawn"] == 1
    assert result.stats["grenade_throwers_without_row"] == 3


def test_a_port_that_never_saw_a_pawnless_row_reports_zero(
    parse_settings, archive, demo
) -> None:
    """Zero is a fresh run's honest result, and it is said out loud.

    The output leaves the line out on a zero, so the key's existence is the
    only difference between "no skips at all" and "the skips were not
    counted".
    """
    parser = FakeParser(build_rounds(played=3))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0, tick_rate_measured=True, rounds_seen=3
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["sample_rows_without_pawn"] == 0
    assert result.stats["sample_points_without_pawn"] == 0
    assert result.stats["grenade_throwers_without_row"] == 0


def test_a_port_without_pawnless_diagnostics_claims_nothing(
    parse_settings, archive, demo
) -> None:
    """A port that says nothing about pawnless rows must not produce a zero.

    A zero would be a claim of a clean setup -- exactly the lie the whole
    counter exists to remove.
    """

    class _Silent(FakeParser):
        diagnostics = None

    result = run_parse(parse_settings, archive, _Silent(), demo)

    assert result.stats["sample_rows_without_pawn"] is None
    assert result.stats["sample_points_without_pawn"] is None
    assert result.stats["grenade_throwers_without_row"] is None


def test_an_empty_tick_table_names_the_pawnless_reason(
    parse_settings, archive, demo
) -> None:
    """An empty setup table must not blame the settings when the reason has
    been read from the demo.

    If every sample point was missed because of pawnlessness, there is nothing
    wrong with ``[parse].snapshot_seconds`` or the freezetime anchors -- and
    the user must not be told to check them.
    """
    empty = pl.DataFrame(schema=dict(TICKS_ADAPTER_SCHEMA))
    parser = FakeParser(build_rounds(played=3, warmup=0), ticks=empty)
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        sample_rows_without_pawn=30,
        sample_points_without_pawn=12,
    )

    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, parser, demo)

    message = str(exc.value)
    assert "12 sample points were missed entirely" in message
    assert "snapshot_seconds" not in message.split(
        "The reason has been read from the demo:"
    )[0]


def test_the_measurement_offsets_come_from_the_written_table(
    parse_settings, archive, demo
) -> None:
    """The distribution of measurement moments is computed from the table that
    is written.

    It is the only visible form of the ``buy_end_tick`` column. If the
    distribution came from elsewhere, the column and the output could drift
    apart, and checkability would be only apparent.

    ``build_rounds`` places the measurement point 1,280 ticks from the anchor
    on every round, that is, at 20.0 seconds at a tick rate of 64.
    """

    class _Measured(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=4,
            buy_window_seconds=20.0,
        )

    result = run_parse(parse_settings, archive, _Measured(), demo)
    assert result.stats["buy_end_offsets_s"] == (20.0, 20.0, 20.0)


def test_a_port_without_buy_window_diagnostics_claims_nothing(
    parse_settings, archive, demo
) -> None:
    """A port that says nothing about the buy window must not produce zeroes.

    A ``getattr`` default of 0 would make the unknown look like a clean run:
    "no truncations at all" would be a claim that nothing supports.
    """

    class _Silent(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0, tick_rate_measured=True, rounds_seen=4
        )

    result = run_parse(parse_settings, archive, _Silent(), demo)
    assert result.stats["buy_window_seconds"] is None
    assert result.stats["buy_window_truncated_by_death"] == 0
    assert result.stats["buy_window_purchases_after_cut"] == 0


def test_a_skipped_run_has_no_buy_window_numbers(
    parse_settings, archive, demo
) -> None:
    """In a skipped run there are no numbers, and none are invented.

    Truncations and lost purchases cannot be read from the finished table, so
    the keys are missing altogether and ``cli`` leaves the lines out -- the
    same rule as with the restarts and the unknown items.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert "buy_window_truncated_by_death" not in result.stats
    assert "buy_end_offsets_s" not in result.stats


# --- The lineup table (Story 2.6) ------------------------------------------------


def test_the_lineups_table_is_written_and_carries_the_map_demo_id(
    parse_settings, archive, demo
) -> None:
    run_parse(parse_settings, archive, FakeParser(), demo)

    table = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "lineups"))
    validate(table, LINEUPS, "lineups")
    assert set(table["map_demo_id"]) == {MAP_DEMO_ID}
    assert set(table["clan_name"]) == {"MatureMayhem", "KALJUKOSTAJA"}
    assert table.height == 10


def test_the_knife_round_does_not_drop_a_player_from_the_lineups_table(
    parse_settings, archive, demo
) -> None:
    """The lineup is a property of the map: the numbering does not touch it.

    The knife round's rows are dropped from the sample-point and event tables,
    but the player played the map -- and must not be dropped from the standing
    roster.
    """
    run_parse(parse_settings, archive, FakeParser(), demo)

    table = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "lineups"))
    assert "round_no" not in table.columns
    assert sorted(table["player_id"]) == sorted(
        f"{lineup}-{i}" for lineup in ("aaa", "bbb") for i in range(5)
    )


def test_a_missing_clan_name_is_written_as_null_not_as_the_key(
    parse_settings, archive, demo
) -> None:
    frame = build_rounds()
    parser = FakeParser(frame, lineups=build_lineups(frame, without_clan=("aaa",)))
    run_parse(parse_settings, archive, parser, demo)

    table = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "lineups"))
    without = table.filter(pl.col("lineup_key") == "aaa")
    assert without.height == 5
    assert without["clan_name"].null_count() == 5


def test_an_empty_lineups_table_is_refused_instead_of_written(
    parse_settings, archive, demo
) -> None:
    """An empty table would stay permanently skipped on the manifest."""
    empty = pl.DataFrame(schema=dict(LINEUPS_ADAPTER_SCHEMA))
    parser = FakeParser(lineups=empty)

    with pytest.raises(SchemaError, match="no lineup row"):
        run_parse(parse_settings, archive, parser, demo)

    assert not archive.parsed_table(MAP_DEMO_ID, "lineups").exists()


def test_a_duplicate_roster_row_is_refused(parse_settings, archive, demo) -> None:
    """``aggregate`` joins the standing roster on this key; a duplicate row
    would double the player."""
    frame = build_rounds()
    lineups = build_lineups(frame)
    doubled = pl.concat([lineups, lineups.head(1)])
    parser = FakeParser(frame, lineups=doubled)

    with pytest.raises(SchemaError, match="lineup_key, player_id"):
        run_parse(parse_settings, archive, parser, demo)


def test_an_archive_without_the_lineups_table_is_not_up_to_date(
    parse_settings, archive, demo
) -> None:
    """A schema change forces a reparse without the --force flag.

    The manifest's parameter hash does not move when more tables appear, so a
    manifest match on its own would accept the old result as up to date.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "lineups").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2
    assert archive.parsed_table(MAP_DEMO_ID, "lineups").is_file()


def test_the_run_reports_the_clans_per_lineup_not_as_one_list(
    parse_settings, archive, demo
) -> None:
    """The numbers are broken out per lineup, because a demo holds two teams.

    A combined list would answer a different question from the one the user
    asks: "does *this* team have a name" is not settled by a list that is
    non-empty as soon as the opponent has a clan.
    """
    result = run_parse(parse_settings, archive, FakeParser(), demo)

    assert result.stats["lineup_rows"] == 10
    assert result.stats["lineups"] == (
        ("aaa", "MatureMayhem", 5, 0),
        ("bbb", "KALJUKOSTAJA", 5, 0),
    )


def test_one_lineup_without_a_clan_does_not_hide_behind_the_other(
    parse_settings, archive, demo
) -> None:
    """A lineup without a name shows on a line of its own, not under the
    opponent's name."""
    frame = build_rounds()
    parser = FakeParser(
        frame,
        lineups=build_lineups(frame, without_clan=("aaa",), without_name=("aaa",)),
    )
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["lineups"] == (
        ("aaa", None, 5, 5),
        ("bbb", "KALJUKOSTAJA", 5, 0),
    )


def test_the_run_reports_players_whose_name_or_clan_changed_mid_map(
    parse_settings, archive, demo
) -> None:
    """The lineup table's basic assumption has to be checked at run time.

    The table records the most frequently observed value, so a broken
    assumption looks there exactly like an intact one. Only the diagnostics
    tell them apart, and a number other than zero is precisely the symptom the
    warning about the read-through-the-side trap speaks of.
    """
    clean = FakeParser()
    clean.diagnostics = ParseDiagnostics(
        tick_rate=64.0, tick_rate_measured=True, rounds_seen=4
    )
    result = run_parse(parse_settings, archive, clean, demo)
    assert result.stats["lineup_clan_conflicts"] == 0
    assert result.stats["lineup_name_conflicts"] == 0

    conflicted = FakeParser()
    conflicted.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=4,
        lineup_clan_conflicts=2,
        lineup_name_conflicts=1,
    )
    again = run_parse(parse_settings, archive, conflicted, demo, force=True)
    assert again.stats["lineup_clan_conflicts"] == 2
    assert again.stats["lineup_name_conflicts"] == 1

    from pappascout.cli import _render_parse

    text = _render_parse(again, 24)
    assert "Clan changed mid-map" in text
    assert "Name changed mid-map" in text
    # Zero is the expected value, and it is not printed.
    assert "changed mid-map" not in _render_parse(result, 24)


def test_the_parse_summary_renders_every_key_the_stage_produces(
    parse_settings, archive, demo
) -> None:
    """The producer/consumer key contract, enforced as on the aggregate side.

    ``_render_parse`` reads dozens of keys out of ``stats``. Without this test
    the key the stage produces and the key the command line reads could
    differ, and the block would silently go unprinted -- it would not fail, it
    would disappear.
    """
    from pappascout.cli import _render_parse

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    text = _render_parse(result, 24)

    assert "Lineups" in text
    assert "MatureMayhem (aaa)" in text
    assert "KALJUKOSTAJA (bbb)" in text


# --- The deaths table (Story 2.7) ------------------------------------------------


def read_deaths(archive: ArchivePaths) -> pl.DataFrame:
    return pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "deaths"))


def test_the_deaths_table_is_written_and_carries_the_map_demo_id(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: an ordinary demo -> deaths.parquet matching the contract."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    df = read_deaths(archive)

    validate(df, DEATHS, "deaths")
    assert set(df["map_demo_id"].to_list()) == {MAP_DEMO_ID}
    assert df.height == 3  # three played rounds, one death on each


def test_deaths_get_the_round_number_from_the_rounds_table(
    parse_settings, archive, demo
) -> None:
    """The numbering belongs to domain.rounds; the stage only joins it."""
    rounds = build_rounds(played=3, warmup=0)
    run_parse(
        parse_settings,
        archive,
        FakeParser(rounds, ticks=build_ticks(rounds), deaths=build_deaths(rounds)),
        demo,
    )
    df = read_deaths(archive)

    assert df["round_no"].null_count() == 0
    assert sorted(df["round_no"].unique().to_list()) == [1, 2, 3]


def test_knife_round_deaths_are_dropped_by_the_same_join(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: the knife round's deaths do not end up in the table.

    The adapter produces them because it does not know the numbering rule --
    people really do die on the knife round. They fall away in the same join
    as the sample points and the grenades, not by a separate rule.
    """
    rounds = build_rounds(played=2, warmup=3)
    result = run_parse(
        parse_settings,
        archive,
        FakeParser(rounds, ticks=build_ticks(rounds), deaths=build_deaths(rounds)),
        demo,
    )
    df = read_deaths(archive)

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]
    assert result.stats["deaths_unnumbered_rounds"] == 3


def test_dropped_deaths_are_counted_not_just_dropped(
    parse_settings, archive, demo
) -> None:
    """A silent drop would look like a demo that held fewer deaths."""
    rounds = build_rounds(played=2, warmup=2)
    result = run_parse(
        parse_settings,
        archive,
        FakeParser(
            rounds,
            ticks=build_ticks(rounds),
            deaths=build_deaths(rounds, per_round=2),
        ),
        demo,
    )
    assert result.stats["deaths_unnumbered_rounds"] == 4
    assert result.stats["death_rows"] == 4


def test_a_death_without_an_attacker_survives_the_write(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: a death without an attacker -> attacker_* null, the row
    survives."""
    rounds = build_rounds(played=2, warmup=0)
    result = run_parse(
        parse_settings,
        archive,
        FakeParser(
            rounds,
            ticks=build_ticks(rounds),
            deaths=build_deaths(rounds, without_attacker=(1,)),
        ),
        demo,
    )
    df = read_deaths(archive)

    assert df.height == 2
    without = df.filter(pl.col("attacker_id").is_null())
    assert without.height == 1
    assert without["victim_id"].null_count() == 0
    assert without["weapon"].to_list() == ["planted_c4"]
    assert result.stats["deaths_without_attacker"] == 1


def test_an_attacker_without_an_area_is_counted_apart_from_a_missing_attacker(
    parse_settings, archive, demo
) -> None:
    """A row without an attacker is not an area defect, so the numbers are
    separate.

    A shared number would look like two area defects when one of them is an
    honest fall.
    """
    rounds = build_rounds(played=3, warmup=0)
    result = run_parse(
        parse_settings,
        archive,
        FakeParser(
            rounds,
            ticks=build_ticks(rounds),
            deaths=build_deaths(
                rounds, without_attacker=(1,), without_attacker_area=(2,)
            ),
        ),
        demo,
    )
    assert result.stats["deaths_without_attacker"] == 1
    assert result.stats["deaths_without_attacker_area"] == 1
    assert result.stats["deaths_without_victim_area"] == 0


def test_a_victim_without_an_area_is_counted(
    parse_settings, archive, demo
) -> None:
    """A missing victim area is zero in the measured material -- the number
    says so if that changes."""
    rounds = build_rounds(played=2, warmup=0)
    result = run_parse(
        parse_settings,
        archive,
        FakeParser(
            rounds,
            ticks=build_ticks(rounds),
            deaths=build_deaths(rounds, without_victim_area=(1,)),
        ),
        demo,
    )
    assert result.stats["deaths_without_victim_area"] == 1
    assert read_deaths(archive)["victim_x"].null_count() == 0


def test_death_rows_are_sorted_by_round_and_time(
    parse_settings, archive, demo
) -> None:
    """A stable order: the same input, the same bytes."""
    rounds = build_rounds(played=3, warmup=0)
    run_parse(
        parse_settings,
        archive,
        FakeParser(
            rounds,
            ticks=build_ticks(rounds),
            deaths=build_deaths(rounds, per_round=2),
        ),
        demo,
    )
    df = read_deaths(archive)
    assert df["round_no"].to_list() == [1, 1, 2, 2, 3, 3]
    assert df.sort("round_no", "t_s", "victim_id").equals(df)


def test_a_deaths_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    """A missing column -> a SchemaError of our own that names the table."""
    rounds = build_rounds()
    broken = build_deaths(rounds).drop("victim_area")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, deaths=broken), demo)
    assert "victim_area" in str(exc.value)
    assert "deaths table" in str(exc.value)


def test_an_extra_deaths_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    """An extra column means the port and the contract have drifted apart."""
    rounds = build_rounds()
    broken = build_deaths(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, deaths=broken), demo)
    assert "ylimaarainen" in str(exc.value)
    assert "deaths table" in str(exc.value)


@pytest.mark.parametrize(
    "column", ["attacker_x", "attacker_y", "attacker_z", "attacker_area"]
)
def test_an_attackerless_death_may_not_carry_attacker_observations(
    parse_settings, archive, demo, column: str
) -> None:
    """Half an attacker is a defect even if the schema accepts every field.

    A place without an actor would land in the report as a kill nobody made.
    Each observation field is tested separately: one shared test would pass
    even if three of the four conditions were removed.
    """
    rounds = build_rounds(played=2, warmup=0)
    value = "Middle" if column == "attacker_area" else 1.0
    broken = build_deaths(rounds, without_attacker=(1,)).with_columns(
        pl.when(pl.col("attacker_id").is_null())
        .then(pl.lit(value))
        .otherwise(pl.col(column))
        .cast(DEATHS[column])
        .alias(column)
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(
            parse_settings,
            archive,
            FakeParser(rounds, ticks=build_ticks(rounds), deaths=broken),
            demo,
        )
    assert "no attacker" in str(exc.value)
    assert column in str(exc.value)


def test_an_attackerless_death_with_only_nulls_is_accepted(
    parse_settings, archive, demo
) -> None:
    """The guard's other branch: an intact attackerless row passes.

    Without this the previous test would prove only that something fails the
    run.
    """
    rounds = build_rounds(played=2, warmup=0)
    run_parse(
        parse_settings,
        archive,
        FakeParser(
            rounds,
            ticks=build_ticks(rounds),
            deaths=build_deaths(rounds, without_attacker=(1, 2)),
        ),
        demo,
    )
    assert read_deaths(archive).height == 2


def test_an_empty_deaths_table_is_refused_instead_of_written(
    parse_settings, archive, demo
) -> None:
    """People die in a played match, so an empty table is a broken port.

    An empty result would stay permanently skipped on the strength of the
    manifest, and the report would tell of a map on which nobody died.
    """
    rounds = build_rounds(played=2, warmup=0)
    empty = pl.DataFrame(schema=dict(DEATHS_ADAPTER_SCHEMA))
    with pytest.raises(ParseError) as exc:
        run_parse(
            parse_settings,
            archive,
            FakeParser(rounds, ticks=build_ticks(rounds), deaths=empty),
            demo,
        )
    assert "not a single death" in str(exc.value)
    assert not archive.parsed_table(MAP_DEMO_ID, "deaths").exists()
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_an_archive_without_the_deaths_table_is_not_up_to_date(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: an old archive without deaths.parquet -> it is run again.

    ``ParseSettings`` did not change, so the parameter hash is identical and
    the old manifest names only four tables. Without a separate check the run
    would be skipped and the deaths table would never be born.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)

    manifest_path = archive.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = [
        o for o in manifest["outputs"] if not o.endswith("deaths.parquet")
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive.parsed_table(MAP_DEMO_ID, "deaths").unlink()

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped, "the old archive would have been left without a deaths table"
    assert parser.calls == 2
    assert archive.parsed_table(MAP_DEMO_ID, "deaths").is_file()


def test_a_stale_deaths_table_missing_a_column_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """A contract change forces a reparse without ``--force``."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)

    path = archive.parsed_table(MAP_DEMO_ID, "deaths")
    pl.read_parquet(path).drop("attacker_area").write_parquet(path)

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2
    validate(pl.read_parquet(path), DEATHS, "deaths")


def test_unreadable_deaths_do_not_hide_the_other_counts(
    parse_settings, archive, demo
) -> None:
    """One unreadable table must not take the others' numbers away.

    The tables are read separately: a broken deaths table is reported under a
    key of its own, and the round, sample-point and utility numbers survive.
    An unreadable table is a different defect from a stale contract, and it is
    not cured by rereading the demo -- which is why the run stays skipped.
    """
    run_parse(parse_settings, archive, FakeParser(), demo)
    archive.parsed_table(MAP_DEMO_ID, "deaths").write_text("ei parquetia")

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert result.skipped
    assert "deaths_unreadable" in result.stats
    assert "death_rows" not in result.stats
    assert result.stats["rounds"] == 3
    assert result.stats["tick_rows"] == 60


def test_skipped_run_reports_the_death_counts_too(
    parse_settings, archive, demo
) -> None:
    """A skipped run reads the numbers from the finished table and does not
    claim zero."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    result = run_parse(parse_settings, archive, FakeParser(), demo)

    assert result.skipped
    assert result.stats["death_rows"] == 3
    assert result.stats["death_rounds"] == 3
    # Those dropped from unnumbered rounds cannot be read from the finished
    # table.
    assert "deaths_unnumbered_rounds" not in result.stats


def test_the_deaths_diagnostics_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """The adapter's own numbers reach the run's summary as they stand."""

    class WithDiagnostics(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=4,
            deaths_outside_rounds=2,
            deaths_without_victim_side=3,
            deaths_attacker_without_side=4,
        )

    result = run_parse(parse_settings, archive, WithDiagnostics(), demo)
    assert result.stats["deaths_outside_rounds"] == 2
    assert result.stats["deaths_without_victim_side"] == 3
    assert result.stats["deaths_attacker_without_side"] == 4


def test_the_parse_summary_names_every_death_line(
    parse_settings, archive, demo
) -> None:
    """The producer/consumer key contract for every new output line.

    Every line is checked **with its value**: searching for the heading alone
    would pass even if the number came from the wrong key.
    """
    from pappascout.cli import _render_parse

    class WithDiagnostics(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=6,
            deaths_outside_rounds=2,
            deaths_without_victim_side=3,
            deaths_attacker_without_side=4,
        )

    rounds = build_rounds(played=3, warmup=2)
    parser = WithDiagnostics(
        rounds,
        ticks=build_ticks(rounds),
        # The numbers run over ALL round boundaries, the warm-up included:
        # numbers 1-2 fall away as unnumbered, so the exceptions have to be
        # placed on the played rounds 3-5.
        deaths=build_deaths(
            rounds,
            without_attacker=(3,),
            without_victim_area=(4,),
            without_attacker_area=(5,),
        ),
    )
    text = _render_parse(run_parse(parse_settings, archive, parser, demo), 24)

    assert "Deaths" in text and "3 (in 3/3 rounds)" in text
    assert "From unnumbered" in text and "2 deaths" in text
    assert "No attacker" in text and "1 (a fall" in text
    assert "Victim without area" in text and "1 rows" in text
    assert "Attacker without area" in text
    assert "Between rounds" in text and "2 (" in text
    assert "Victim without side" in text and "3 (" in text
    assert "Attacker without side" in text and "4 (" in text


def test_the_parse_summary_stays_silent_when_every_death_is_whole(
    parse_settings, archive, demo
) -> None:
    """Zero is the expected value and is not printed -- only the row count
    stays."""
    from pappascout.cli import _render_parse

    text = _render_parse(
        run_parse(parse_settings, archive, FakeParser(), demo), 24
    )
    assert "Deaths" in text and "3 (in 3/3 rounds)" in text
    assert "No attacker" not in text
    assert "Victim without area" not in text
    assert "Attacker without area" not in text
    assert "Victim without side" not in text
    assert "Attacker without side" not in text
    assert "Between rounds" not in text


def test_an_unreadable_deaths_table_is_reported_in_a_skipped_run(
    parse_settings, archive, demo
) -> None:
    """An unreadable table is reported, not zeroed."""
    from pappascout.cli import _render_parse

    stats = parse_stage._existing_stats(
        archive.parsed_table(MAP_DEMO_ID, "rounds"),
        archive.parsed_table(MAP_DEMO_ID, "ticks"),
        archive.parsed_table(MAP_DEMO_ID, "events"),
        archive.parsed_table(MAP_DEMO_ID, "lineups"),
        archive.parsed_table(MAP_DEMO_ID, "deaths"),
        archive.parsed_table(MAP_DEMO_ID, "callouts"),
        archive.parsed_table(MAP_DEMO_ID, "match"),
    )
    assert "unreadable" in stats

    run_parse(parse_settings, archive, FakeParser(), demo)
    archive.parsed_table(MAP_DEMO_ID, "deaths").write_text("ei parquetia")
    stats = parse_stage._existing_stats(
        archive.parsed_table(MAP_DEMO_ID, "rounds"),
        archive.parsed_table(MAP_DEMO_ID, "ticks"),
        archive.parsed_table(MAP_DEMO_ID, "events"),
        archive.parsed_table(MAP_DEMO_ID, "lineups"),
        archive.parsed_table(MAP_DEMO_ID, "deaths"),
        archive.parsed_table(MAP_DEMO_ID, "callouts"),
        archive.parsed_table(MAP_DEMO_ID, "match"),
    )
    assert "deaths_unreadable" in stats
    # One broken table does not take another's numbers: the point cloud is
    # intact.
    assert stats["callout_cells"] == 4
    text = _render_parse(
        StageResult(
            stage="parse",
            unit=MAP_DEMO_ID,
            status="ok",
            skipped=True,
            stats=stats,
        ),
        24,
    )
    assert "Deaths" in text and "no counts obtained" in text


# --- Review round: victim integrity, ordering and drop reasons -----------------


@pytest.mark.parametrize(
    "column", ["victim_id", "victim_lineup_key", "victim_side"]
)
def test_a_death_without_its_victim_is_refused(
    parse_settings, archive, demo, column: str
) -> None:
    """The victim is the row's identity; without it the row would vanish
    silently.

    Each of these is a nullable column, so ``validate`` would let the row
    through -- and in aggregation it would be neither a death nor a kill,
    because both filters compare against the lineup. The columns are tested
    separately: one shared test would pass even if two of the three conditions
    were removed.
    """
    rounds = build_rounds(played=2, warmup=0)
    broken = build_deaths(rounds).with_columns(
        pl.when(pl.col("round_raw") == pl.col("round_raw").min())
        .then(None)
        .otherwise(pl.col(column))
        .cast(DEATHS[column])
        .alias(column)
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(
            parse_settings,
            archive,
            FakeParser(rounds, ticks=build_ticks(rounds), deaths=broken),
            demo,
        )
    assert "the victim's details" in str(exc.value)
    assert column in str(exc.value)


def test_a_whole_victim_passes_the_guard(parse_settings, archive, demo) -> None:
    """The guard's other branch: an intact victim passes.

    Without this the previous test would prove only that something fails the
    run.
    """
    run_parse(parse_settings, archive, FakeParser(), demo)
    assert read_deaths(archive).height == 3


def test_the_integrity_guards_see_the_knife_round_too(
    parse_settings, archive, demo
) -> None:
    """The guards are run **before** the numbering, over the adapter's whole
    output.

    A half-formed row most likely comes from the warm-up and the knife round
    -- those are the rounds on which the game's state is least stable. After
    the numbering the guard would look only at the part of the material where
    no defect is expected.
    """
    rounds = build_rounds(played=2, warmup=2)
    # Break **only** the unnumbered round: a guard run after the numbering
    # would not see this row at all.
    broken = build_deaths(rounds).with_columns(
        pl.when(pl.col("round_raw") <= 2)
        .then(None)
        .otherwise(pl.col("victim_id"))
        .cast(DEATHS["victim_id"])
        .alias("victim_id")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(
            parse_settings,
            archive,
            FakeParser(rounds, ticks=build_ticks(rounds), deaths=broken),
            demo,
        )
    assert "the victim's details" in str(exc.value)


def test_the_attacker_guard_sees_the_knife_round_too(
    parse_settings, archive, demo
) -> None:
    """The same for the attacker's guard: a dropped round must not hide a
    defect."""
    rounds = build_rounds(played=2, warmup=2)
    broken = build_deaths(rounds, without_attacker=(1, 2)).with_columns(
        pl.when(pl.col("attacker_id").is_null())
        .then(pl.lit("Middle"))
        .otherwise(pl.col("attacker_area"))
        .alias("attacker_area")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(
            parse_settings,
            archive,
            FakeParser(rounds, ticks=build_ticks(rounds), deaths=broken),
            demo,
        )
    assert "no attacker" in str(exc.value)


def test_a_death_without_a_time_sorts_last_in_the_written_table(
    parse_settings, archive, demo
) -> None:
    """A missing time is not zero -- and not one way in the parquet and
    another in the domain.

    ``deaths_for`` sorts an empty ``t_s`` last. Without ``nulls_last=True``
    the same row would lead its round in the table, and the same thing would
    be ordered in two different ways depending on which one you look at.
    """
    rounds = build_rounds(played=1, warmup=0)
    deaths = build_deaths(rounds, per_round=3).with_columns(
        pl.when(pl.col("victim_id").str.ends_with("-0"))
        .then(None)
        .otherwise(pl.col("t_s"))
        .cast(pl.Float64)
        .alias("t_s")
    )
    run_parse(
        parse_settings,
        archive,
        FakeParser(rounds, ticks=build_ticks(rounds), deaths=deaths),
        demo,
    )
    written = read_deaths(archive)

    assert written["t_s"].to_list()[-1] is None
    assert written["t_s"].to_list()[0] is not None


def test_the_empty_table_error_names_the_counters_it_already_has(
    parse_settings, archive, demo
) -> None:
    """The error message does not guess the reason when the numbers are in
    hand.

    The adapter breaks out every drop reason, and they have been computed
    before the emptiness is noticed. Without them the message would name two
    guesses.
    """

    class WithDrops(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=3,
            deaths_outside_rounds=7,
            deaths_without_victim=2,
        )

    rounds = build_rounds(played=2, warmup=0)
    empty = pl.DataFrame(schema=dict(DEATHS_ADAPTER_SCHEMA))
    with pytest.raises(ParseError) as exc:
        run_parse(
            parse_settings,
            archive,
            WithDrops(rounds, ticks=build_ticks(rounds), deaths=empty),
            demo,
        )
    message = str(exc.value)
    assert "outside the rounds 7" in message
    assert "no victim 2" in message


def test_the_empty_table_error_says_so_when_every_counter_is_zero(
    parse_settings, archive, demo
) -> None:
    """The other branch: with no drops the reason is the port itself."""
    rounds = build_rounds(played=2, warmup=0)
    empty = pl.DataFrame(schema=dict(DEATHS_ADAPTER_SCHEMA))
    with pytest.raises(ParseError) as exc:
        run_parse(
            parse_settings,
            archive,
            FakeParser(rounds, ticks=build_ticks(rounds), deaths=empty),
            demo,
        )
    message = str(exc.value)
    assert "produced no death at all" in message
    assert "player_death" in message


def test_the_new_drop_counters_reach_the_stats_and_the_summary(
    parse_settings, archive, demo
) -> None:
    """A death without a tick and one without a victim are different reasons,
    and both show."""
    from pappascout.cli import _render_parse

    class WithDrops(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=4,
            deaths_without_tick=2,
            deaths_without_victim=3,
        )

    result = run_parse(parse_settings, archive, WithDrops(), demo)
    assert result.stats["deaths_without_tick"] == 2
    assert result.stats["deaths_without_victim"] == 3

    text = _render_parse(result, 24)
    assert "Death without a tick" in text and "2 (" in text
    assert "Death without victim" in text and "3 (" in text


# --- The point cloud (Story 2.9) -------------------------------------------------


def test_the_usable_row_count_has_exactly_one_source(
    parse_settings, archive, demo
) -> None:
    """The usable rows are the sum of the cells' observations, not a counter
    of their own.

    Two sources for the same number can drift apart: the adapter's filter and
    the table's sum would give different answers the moment one of them was
    changed. So the port does not report the number at all -- the stage
    computes it from the table and ``cli`` uses it as the ratio's numerator.
    """
    assert not hasattr(ParseDiagnostics(tick_rate=64.0,
                                        tick_rate_measured=True,
                                        rounds_seen=1),
                       "callout_cloud_rows_usable")
    result = run_parse(parse_settings, archive, FakeParser(), demo)
    table = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "callouts"))
    assert result.stats["callout_observations"] == int(table["observations"].sum())


def test_writes_a_valid_callout_cloud(parse_settings, archive, demo) -> None:
    """Acceptance criterion: ``callouts.parquet`` passes ``validate``.

    The table is the **source** of the detonation areas, and it is written for
    exactly that reason: a derived area can be checked against the demo only
    if what it was derived from is kept.
    """
    result = run_parse(parse_settings, archive, FakeParser(), demo)

    table = archive.parsed_table(MAP_DEMO_ID, "callouts")
    assert table.is_file()
    df = pl.read_parquet(table)
    validate(df, CALLOUT_CLOUD, "callouts")
    assert list(df.columns) == list(CALLOUT_CLOUD)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert df.height == 4
    assert result.stats["callout_cells"] == 4
    assert result.stats["callout_areas"] == 2
    # The observations are the cells' own numbers, not a constant:
    # 10 + 11 + 12 + 13.
    assert result.stats["callout_observations"] == 46


def test_the_cloud_keeps_every_round_including_the_knife_round(
    parse_settings, archive, demo
) -> None:
    """The point cloud is not numbered, so its rows do not fall away in it.

    The cloud is a property of the map in this demo and not an observation
    about a round, and the ticks of the warm-up and of the knife round tell as
    much about the map as those of the played rounds. The same rule as with
    the lineup table: the table has no ``round_no`` column at all.
    """
    rounds = build_rounds(played=3, warmup=2)
    run_parse(parse_settings, archive, FakeParser(rounds), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "callouts"))
    assert "round_no" not in df.columns
    assert "round_raw" not in df.columns
    assert df.height == 4


def test_the_cloud_rows_are_sorted_by_cell(parse_settings, archive, demo) -> None:
    """The same demo produces the same file byte for byte."""
    shuffled = build_callouts().sort("cell_x", descending=True)
    run_parse(
        parse_settings, archive, FakeParser(callouts=shuffled), demo
    )
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "callouts"))
    assert df["cell_x"].to_list() == sorted(df["cell_x"].to_list())


def test_an_empty_cloud_is_written_and_does_not_stop_the_run(
    parse_settings, archive, demo
) -> None:
    """I/O matrix: an empty point cloud -> the run holds, the numbers are zero.

    An empty deaths table is an error and an empty point cloud is not -- the
    difference is in what emptiness means. A death is not a choice, but a demo
    whose ``last_place_name`` comes back empty is genuinely cloudless. Its
    consequence (every detonation area null) is the right outcome, and the
    reason is given in the diagnostics.
    """
    empty = pl.DataFrame(schema=dict(CALLOUTS_ADAPTER_SCHEMA))
    result = run_parse(
        parse_settings, archive, FakeParser(callouts=empty), demo
    )
    assert result.status == "ok"
    table = archive.parsed_table(MAP_DEMO_ID, "callouts")
    assert table.is_file()
    assert pl.read_parquet(table).is_empty()
    assert result.stats["callout_cells"] == 0
    assert result.stats["callout_areas"] == 0
    assert result.stats["callout_observations"] == 0


def test_a_callouts_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    broken = build_callouts().drop("observations")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "observations" in str(exc.value)
    assert "point cloud" in str(exc.value)


def test_an_extra_callouts_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    broken = build_callouts().with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "ylimaarainen" in str(exc.value)
    assert "point cloud" in str(exc.value)


def test_a_duplicate_cell_is_refused(parse_settings, archive, demo) -> None:
    """Two rows for the same cell would mean the mode selection did not work.

    A detonation would get its area according to whichever row happened to be
    nearer in the ordering, and the same demo could give a different area on a
    different run. The schema does not see this: both rows are valid on their
    own.
    """
    doubled = pl.concat([build_callouts(), build_callouts().head(1)])
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=doubled), demo)
    assert "cell_x, cell_y, cell_z" in str(exc.value)
    assert "duplicates" in str(exc.value)


def test_a_cell_without_an_area_is_refused(parse_settings, archive, demo) -> None:
    """A cell without a name would name a detonation empty *inside the
    threshold*.

    The row would then look the same as "no area was obtained", although the
    hit was good. ``area`` is a nullable column, so the schema would let it
    through.
    """
    nameless = build_callouts().with_columns(
        pl.lit(None, dtype=pl.Utf8).alias("area")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=nameless), demo)
    assert "without an area name" in str(exc.value)


@pytest.mark.parametrize("observations", [0, -1, None])
def test_a_cell_without_observations_is_refused(
    parse_settings, archive, demo, observations
) -> None:
    """A cell is born only from an observation, so zero means an invented cell.

    ``Int32`` lets zero and negative through, and the key check looks only at
    the coordinates. The consequence would be silent:
    ``callout_observations`` and the run's usable ratio would look smaller
    than they are, and nothing would say why.
    """
    broken = build_callouts().with_columns(
        pl.lit(observations, dtype=pl.Int32).alias("observations")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "not a single observation" in str(exc.value)


def test_a_cell_with_a_blank_area_is_refused(
    parse_settings, archive, demo
) -> None:
    """A bare space is not an area name, even though it is not null.

    An empty name would name a detonation empty *inside the threshold*, that
    is, the row would look the same as "no area was obtained" although the hit
    was good.
    """
    broken = build_callouts().with_columns(pl.lit("   ").alias("area"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "without an area name" in str(exc.value)


def test_a_missing_callouts_table_forces_a_reparse(
    parse_settings, archive, demo
) -> None:
    """Half a result is not an up-to-date result.

    A Story 2.8 archive is in exactly this state: the manifest matches, but
    ``callouts.parquet`` is missing. Without the check the run would be
    skipped and the user would be told "the result is up to date".
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "callouts").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_an_events_table_with_the_retired_enum_value_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """Story 2.9's real migration path: an old ``snapped`` does not load.

    This is the situation the user really runs into -- the archive holds a
    Story 2.8 ``events.parquet`` whose ``area_source`` is
    ``Enum(["observed", "snapped"])``. Both ``constants.py`` and
    ``_schema_is_current`` promise that it will not do and that the demo is
    parsed again **without** the ``--force`` flag. Other tests cover a
    missing table and a dropped column; this covers the wrong value set,
    which is a different defect: the columns are in order and the rows can be
    read.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    assert parser.calls == 1

    table = archive.parsed_table(MAP_DEMO_ID, "events")
    old_enum = pl.Enum(["observed", "snapped"])
    stale = pl.read_parquet(table).with_columns(
        # The values are put back into the old vocabulary: that is exactly
        # the file in the archive when it was written by Story 2.8's code.
        pl.col("area_source")
        .cast(pl.Utf8)
        .replace("point_cloud", "snapped")
        .cast(old_enum)
    )
    assert stale.schema["area_source"] == old_enum
    stale.write_parquet(table)

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2
    # And the new result is on the current list.
    assert pl.read_parquet(table).schema["area_source"] == EVENTS["area_source"]


def test_a_callouts_table_that_no_longer_matches_the_contract_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """A schema change does not move the parameter hash, so it is checked."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    table = archive.parsed_table(MAP_DEMO_ID, "callouts")
    pl.read_parquet(table).drop("observations").write_parquet(table)

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_the_cloud_counts_come_back_from_a_skipped_run(
    parse_settings, archive, demo
) -> None:
    """The cells and areas are readable from the finished table, so they are
    given in a skipped run too -- unlike the tick rows that were read."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    result = run_parse(parse_settings, archive, parser, demo)
    assert result.skipped
    assert result.stats["callout_cells"] == 4
    assert result.stats["callout_areas"] == 2
    assert "callout_cloud_rows_read" not in result.stats


def test_the_cloud_diagnostics_reach_the_stats_and_the_summary(
    parse_settings, archive, demo
) -> None:
    """The rows read and the rows usable are visible only at read time.

    The finished table says how many cells were born but not what they were
    reduced from -- nor why the cloud came out empty.
    """
    from pappascout.cli import _render_parse

    parser = FakeParser(callouts=pl.DataFrame(schema=dict(CALLOUTS_ADAPTER_SCHEMA)))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        callout_cloud_rows_read=1529910,
        callout_cloud_empty_reason="1529910 tick rows were read, but not "
        "one of them had a living player in a named area",
    )
    result = run_parse(parse_settings, archive, parser, demo)
    assert result.stats["callout_cloud_rows_read"] == 1529910
    assert result.stats["callout_cloud_empty_reason"].startswith("1529910 tick rows")
    # The usable rows come from the table, not from another counter.
    assert result.stats["callout_observations"] == 0

    text = _render_parse(result, regulation_rounds=24)
    assert "empty -- not one detonation area is named" in text
    # The reason is the diagnostics' own value, given by this test's fixture
    # above; only the row around it belongs to the command line.
    assert "not one of them had a living player" in text


def test_the_detonation_area_coverage_and_distance_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """Coverage and distance spread are computed from the finished table on
    every run.

    They are Story 2.9's measures, and they must not be left to calibration:
    the threshold can go stale with a new map, and when it does that belongs
    in the run and not only in the report.
    """
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds)
    # Two detonations out of four stay behind the threshold: area null,
    # distance kept. That is the I/O matrix's "a distant detonation" row.
    detonation = pl.col("event_kind") == "grenade_detonate"
    far = pl.col("grenade_no") % 2 == 1
    events = events.with_columns(
        pl.when(detonation & far)
        .then(None)
        .otherwise(pl.col("area"))
        .alias("area"),
        pl.when(detonation & far)
        .then(None)
        .otherwise(pl.col("area_source"))
        .alias("area_source"),
        pl.when(detonation)
        .then(pl.when(far).then(900.0).otherwise(40.0))
        .otherwise(None)
        .cast(pl.Float32)
        .alias("snap_distance"),
    )
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=events), demo
    )

    named, total = result.stats["utility_detonation_area_coverage"]
    assert total == 8
    assert named == 4
    assert result.stats["utility_area_beyond_threshold"] == 4
    median, p90, largest = result.stats["utility_snap_distance"]
    assert largest == pytest.approx(900.0)
    assert median == pytest.approx(470.0)


def test_the_distance_spread_reports_three_different_numbers(
    parse_settings, archive, demo
) -> None:
    """The median, the p90 and the max are **three different numbers**, not
    the same one three times.

    Without this fixture ``p90`` is computed but unobserved: it can be
    swapped for ``quantile(0.3)`` and not one claim fails, because the other
    tests either unpack it into a variable without using it or compare against
    a hard-coded literal in the output. And the p90 is exactly the number the
    settings' docstrings sell as the only per-run evidence of the threshold's
    calibration.

    The distances are 10, 20, ..., 200 (20 detonations): median 105, p90 (the
    nearest observation) 180 and max 200. Three different numbers, and any
    other quantile would give a different result -- ``quantile(0.3)`` would
    give 60.
    """
    rounds = build_rounds(played=5, warmup=0)
    events = build_events(rounds)
    detonations = pl.col("event_kind") == "grenade_detonate"
    # 20 detonations, distances 10..200 in ascending order.
    step = (
        pl.col("grenade_no").rank("ordinal").over("event_kind").cast(pl.Float32)
        * 10.0
    )
    events = events.with_columns(
        pl.when(detonations).then(step).otherwise(None).alias("snap_distance")
    )
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=events), demo
    )

    median, p90, largest = result.stats["utility_snap_distance"]
    assert result.stats["utility_detonations"] == 20
    assert (median, p90, largest) == pytest.approx((105.0, 180.0, 200.0))
    # Three different numbers: if two were the same, the claim would not tell
    # the quantiles apart.
    assert len({median, p90, largest}) == 3


# --- The match table (Story 2.11) ------------------------------------------------


def test_writes_a_match_table_with_one_row_and_the_map_demo_id(
    parse_settings, archive, demo
) -> None:
    """One row per demo, with ``map_demo_id`` as the join key as elsewhere."""
    run_parse(parse_settings, archive, FakeParser(match=build_match("de_nuke")), demo)

    table = archive.parsed_table(MAP_DEMO_ID, "match")
    df = pl.read_parquet(table)

    validate(df, MATCH, "match")
    assert df.height == 1
    assert df["map_demo_id"].to_list() == [MAP_DEMO_ID]
    assert df["map_name"].to_list() == ["de_nuke"]


def test_a_match_table_without_a_name_is_still_written(
    parse_settings, archive, demo
) -> None:
    """A missing name is ``null`` and not a missing row.

    Aggregation reads into its name map only those demos for which a row is
    found. Without the row, "the header held no map" would look exactly like
    "there is no table" -- and the latter drops the demo out of the sample
    altogether.
    """
    run_parse(parse_settings, archive, FakeParser(match=build_match(None)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "match"))
    assert df.height == 1
    assert df["map_name"].to_list() == [None]


@pytest.mark.parametrize("rows", [0, 2])
def test_a_match_table_without_exactly_one_row_is_refused(
    parse_settings, archive, demo, rows: int
) -> None:
    """The row count is a contract that schema validation does not see.

    Zero rows is a demo without a match and two rows is two matches in the
    same file; neither is true, and both would pass ``validate``, because the
    columns and the types are in order.
    """
    broken = build_match("de_nuke", rows=rows)
    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, FakeParser(match=broken), demo)

    assert "match table of demo" in str(exc.value)
    assert not archive.parsed_table(MAP_DEMO_ID, "match").exists()
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_a_match_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    """The port's column list is checked before the table is built."""
    broken = build_match("de_nuke").drop("map_name")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(match=broken), demo)
    assert "MATCH_ADAPTER_COLUMNS" in str(exc.value)


def test_an_unreadable_header_marks_the_demo_parse_failed(
    parse_settings, archive, demo
) -> None:
    """The adapter's ``ParseError`` is recorded in the manifest and blocks the
    skip.

    The manifest is what makes sure the next run does not skip over half a
    result: without the record the demo would look up to date.
    """
    parser = FakeParser(error=ParseError("Demon otsikkoa ei voitu lukea: rikki"))
    with pytest.raises(ParseError):
        run_parse(parse_settings, archive, parser, demo)

    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "parse_failed"
    assert "otsikkoa ei voitu lukea" in (manifest.reason or "")
    assert not archive.parsed_table(MAP_DEMO_ID, "match").exists()

    # And the next run does not skip: a failed result is not an up-to-date
    # result.
    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert not result.skipped


def test_a_missing_match_table_forces_a_reparse(
    parse_settings, archive, demo
) -> None:
    """An old archive: the manifest matches, but the new table is missing.

    Story 1.8: ``params_hash`` is computed only from the ``[parse]`` section
    and demoparser2's version, so a schema change on its own does **not**
    invalidate the archive. ``expected_outputs`` is the only thing standing
    between eight already parsed demos and being left without the map name --
    and without the ``--force`` flag.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "match").unlink()

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped
    assert parser.calls == 2
    assert archive.parsed_table(MAP_DEMO_ID, "match").is_file()


def test_a_match_table_that_no_longer_matches_the_contract_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """A schema change does not move the parameter hash, so it is checked."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    table = archive.parsed_table(MAP_DEMO_ID, "match")
    pl.read_parquet(table).drop("map_name").write_parquet(table)

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_the_match_table_is_listed_in_the_manifest_and_the_result(
    parse_settings, archive, demo
) -> None:
    """The manifest **and the run's result** name every table written.

    Two different lists, and both have to be kept up to date: the manifest
    governs the skip, ``StageResult.outputs`` is what the user sees on the
    run summary's ``Tulos`` lines. A missing line in the output would give the
    impression that the table had not been written.
    """
    result = run_parse(parse_settings, archive, FakeParser(), demo)

    outputs = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).outputs
    assert any(str(o).endswith("match.parquet") for o in outputs)
    assert any(str(o).endswith("match.parquet") for o in result.outputs)


def test_the_map_name_reaches_the_stats_and_the_summary(
    parse_settings, archive, demo
) -> None:
    """The map name shows in the run's summary -- that is the only sign of it.

    The name is ``aggregate``'s only means of joining two demos into the same
    map, and it cannot be inferred from the FACEIT id. Without the line in the
    output, a lost header field would send the whole archive back to per-demo
    map branches without a single sign.
    """
    from pappascout.cli import _render_parse

    result = run_parse(
        parse_settings, archive, FakeParser(match=build_match("de_nuke")), demo
    )

    assert result.stats["map_name"] == "de_nuke"
    assert "de_nuke (observed from the demo's header)" in _render_parse(result, 24)


def test_a_missing_map_name_says_so_out_loud(parse_settings, archive, demo) -> None:
    """A missing name is a line of its own, not a blank spot in the output.

    A missing key and ``None`` would mean the same thing in the output, and
    the output is the only place where a lost header field shows.
    """
    from pappascout.cli import _render_parse

    result = run_parse(
        parse_settings, archive, FakeParser(match=build_match(None)), demo
    )

    assert result.stats["map_name"] is None
    text = _render_parse(result, 24)
    assert "the header held no map name" in text


def test_the_reason_for_a_missing_map_name_reaches_the_summary(
    parse_settings, archive, demo
) -> None:
    """The reason comes from the diagnostics and shows only from a fresh run.

    The finished table says that the name is missing but not whether the field
    was absent from the header altogether -- and that difference is exactly
    what separates a field the library renamed from a demo whose header never
    recorded the map.
    """
    from pappascout.cli import _render_parse

    parser = FakeParser(match=build_match(None))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        header_map_name_missing_reason="the header has no map_name field at "
        "all -- demoparser2 has most likely renamed it",
    )

    result = run_parse(parse_settings, archive, parser, demo)

    assert (
        result.stats["header_map_name_missing_reason"]
        == "the header has no map_name field at all -- demoparser2 has most "
        "likely renamed it"
    )
    assert "Map missing because" in _render_parse(result, 24)


def test_the_map_name_comes_back_from_a_skipped_run(
    parse_settings, archive, demo
) -> None:
    """A skipped run gives the map, because the name is readable from the
    table.

    That is exactly the line's value: a skipped run is the state in which the
    user otherwise sees nothing at all about the map. The reason for an
    absence, on the other hand, comes from the diagnostics, so it is not there
    in a skipped run -- and that is right.
    """
    parser = FakeParser(match=build_match("de_anubis"))
    run_parse(parse_settings, archive, parser, demo)

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert result.stats["map_name"] == "de_anubis"
    assert "header_map_name_missing_reason" not in result.stats


def test_an_empty_match_table_gets_our_own_error_not_a_polars_one(
    parse_settings, archive, demo
) -> None:
    """The row count is checked **before** the frame is built.

    Checked afterwards, our own error would survive only because ``pl.lit``
    happens to broadcast to height 0 as well. The same rule as with the
    ``ShapeError`` in ``stages.aggregate._in_schema_order``: the failure mode
    is removed, not translated.
    """
    broken = build_match("de_nuke", rows=0)
    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, FakeParser(match=broken), demo)

    message = str(exc.value)
    assert "has 0 rows" in message
    assert "ShapeError" not in message
