"""``stages.classify`` -- the stage's tests.

The stage does not read the demo at all, so its whole logic -- reading the
rounds table, classifying both teams, the round list, the manifest and the skip
-- is tested with a hand-built rounds table. The only tests that need a demo
are the regressions at the end, and they skip themselves cleanly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from conftest import (
    ANCIENT_DEM,
    ANCIENT_ROUNDS,
    NUKE_ROUNDS,
    NUKE_ZST,
    REAL_SETTINGS,
    even_split,
    has_temp_leftovers,
    require_demo,
    settings_text,
)
from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
)
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.economy import per_player
from pappascout.domain.models import load_settings
from pappascout.domain.rounds import mark_played_rounds
from pappascout.domain.selection import MapSelection
from pappascout.domain.teams import Team, assign_lineup_keys
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    CALLOUT_CLOUD,
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.errors import PappascoutError, SchemaError
from test_calibration import TRUTH_TABLE
from pappascout.stages import classify as classify_stage
from pappascout.stages import discover as discover_stage
from pappascout.stages import parse as parse_stage
from pappascout.stages import select as select_stage

MAP_DEMO_ID = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"

#: A caller without a file saying **explicitly** that it has nothing.
#: ``classify_rounds`` requires the facts as a keyword argument and does not
#: default them to empty: a default would make forgetting silent and would
#: produce exactly the empty column this code exists to fix.
NO_FACTS = classify_stage.MatchFacts()

A = "aaaaaaaaaaaaaaaa"
B = "bbbbbbbbbbbbbbbb"


# --- Building the rounds table ---------------------------------------------------


def round_rows(
    round_no: int,
    *,
    a_side: str = "T",
    a_won: bool = True,
    a_money: int = 5000,
    a_spent: int = 20000,
    a_equip: int = 25000,
    a_start: int = 1000,
    b_money: int = 5000,
    b_spent: int = 20000,
    b_equip: int = 25000,
    b_start: int = 1000,
    a_players: int | None = 5,
    b_players: int | None = 5,
    status: str = "ok",
) -> list[dict[str, object]]:
    """One round's two rows, one for each lineup.

    The reason for the win is chosen by side, so that CS2's rule invariant
    holds in this hand-built table as well.
    """
    b_side = "CT" if a_side == "T" else "T"
    win_reason = "ct_killed" if (a_side == "T") == a_won else "t_killed"
    rows = []
    for lineup, side, won, money, spent, equip, start, players in (
        (A, a_side, a_won, a_money, a_spent, a_equip, a_start, a_players),
        (B, b_side, not a_won, b_money, b_spent, b_equip, b_start, b_players),
    ):
        rows.append(
            {
                "map_demo_id": MAP_DEMO_ID,
                "round_raw": round_no + 1,
                "round_no": round_no,
                "lineup_key": lineup,
                "side": side,
                "won": won,
                "win_reason": win_reason,
                "money_buy_end": None if status != "ok" else money,
                "money_spent": None if status != "ok" else spent,
                "equip_buy_end": None if status != "ok" else equip,
                "equip_round_start": None if status != "ok" else start,
                "players_buy_end": None if status != "ok" else players,
                # The half-buy's two observations are derived from the other
                # values, so that the row is internally consistent: the
                # distribution's sum is money_buy_end and the counter's ceiling
                # is players_buy_end. A test that examines those in particular
                # builds a row of its own.
                ARMED_COLUMN: None if status != "ok" else players,
                MONEY_DISTRIBUTION_COLUMN: (
                    None
                    if status != "ok" or not players
                    else even_split(money, players)
                ),
                "survivors": 2 if won else 0,
                "survivors_equip_prev": 0,
                "freeze_end_tick": None if status != "ok" else 1000 * round_no,
                # Different from the anchor, as on a real run: a column left
                # empty would not reveal it if the stage dropped it on the way.
                "buy_end_tick": None if status != "ok" else 1000 * round_no + 1280,
                "tick_rate": 64.0,
                "status": status,
            }
        )
    return rows


def rounds_frame(rounds: list[list[dict[str, object]]]) -> pl.DataFrame:
    rows = [r for pair in rounds for r in pair]
    df = pl.DataFrame(rows, schema=dict(ROUNDS), orient="row")
    return validate(df, ROUNDS, "rounds")


def match(played: int = 6) -> list[list[dict[str, object]]]:
    """A simple match: A wins the pistol round, after that they alternate."""
    rounds = [round_rows(1, a_won=True, a_equip=4000, b_equip=4000)]
    for no in range(2, played + 1):
        rounds.append(round_rows(no, a_won=no % 2 == 0))
    return rounds


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    root = tmp_path / "arkisto"
    root.mkdir()
    return ArchivePaths(root=root)


@pytest.fixture
def settings(settings_file: Path):
    return load_settings(settings_file, env_files=())


def _minimal_deaths(frame: pl.DataFrame) -> pl.DataFrame:
    """A deaths table from the rounds table, as the port's contract requires.

    The classification does not read the deaths at all, but ``parse`` refuses
    to write an empty deaths table: in a match that was played, people die. One
    death per round is enough, and the victim is the same player as in
    :func:`_minimal_ticks`, so that the tables do not disagree.
    """
    rows: list[dict[str, object]] = []
    for row in frame.iter_rows(named=True):
        if row["round_no"] is None or row["side"] != "T":
            continue
        rows.append(
            {
                "round_raw": row["round_raw"],
                "round_no": None,
                "t_s": 20.0,
                "victim_id": f"{row['lineup_key']}-1",
                "victim_lineup_key": row["lineup_key"],
                "victim_side": row["side"],
                "victim_x": 1.0,
                "victim_y": 2.0,
                "victim_z": 3.0,
                "victim_area": "Middle",
                "attacker_id": None,
                "attacker_lineup_key": None,
                "attacker_side": None,
                "attacker_x": None,
                "attacker_y": None,
                "attacker_z": None,
                "attacker_area": None,
                "weapon": "planted_c4",
            }
        )
    return pl.DataFrame(
        rows,
        schema={name: DEATHS[name] for name in DEATHS_ADAPTER_COLUMNS},
        orient="row",
    )


def _minimal_lineups(frame: pl.DataFrame) -> pl.DataFrame:
    """A lineups table from the rounds table's lineups, as the port's contract requires.

    The classification does not read the names at all, but ``parse`` refuses to
    write an empty lineups table: the lineups are recognised from every demo.
    The player ids are the same as in :func:`_minimal_ticks`, so that the
    tables do not disagree about the lineup.
    """
    rows = [
        {
            "lineup_key": lineup,
            "player_id": f"{lineup}-1",
            "player_name": f"{lineup}-pelaaja",
            "clan_name": f"Klaani-{lineup}",
        }
        for lineup in sorted(
            {
                row["lineup_key"]
                for row in frame.iter_rows(named=True)
                if row["round_no"] is not None
            }
        )
    ]
    return pl.DataFrame(
        rows,
        schema={name: LINEUPS[name] for name in LINEUPS_ADAPTER_COLUMNS},
        orient="row",
    )


def _minimal_ticks(frame: pl.DataFrame) -> pl.DataFrame:
    """One sample point per round row, as the port's contract requires.

    The classification does not read the sample points at all, but ``parse``
    refuses to write an empty setup table for a non-empty rounds table. This
    keeps the fixture honest: it produces what a real adapter would produce,
    not an empty shell.
    """
    rows = [
        {
            "round_raw": row["round_no"],
            "round_no": None,
            "player_id": f"{row['lineup_key']}-1",
            "lineup_key": row["lineup_key"],
            "side": row["side"],
            "sample_kind": "time",
            "sample_t_s": 6.0,
            "t_s": 6.0,
            "x": 1.0,
            "y": 2.0,
            "z": 3.0,
            "area": "Middle",
            "is_alive": True,
        }
        for row in frame.iter_rows(named=True)
        if row["round_no"] is not None
    ]
    return pl.DataFrame(
        rows,
        schema={name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS},
        orient="row",
    )


def write_parse(
    archive: ArchivePaths,
    frame: pl.DataFrame,
    parse_settings,
    *,
    force: bool = False,
) -> None:
    """Write the rounds table and a genuine ``parse`` manifest into the archive.

    The manifest is written with the real stage and not by hand, so that the
    skip chain ``parse -> classify`` is tested as it is in production. The demo
    file is not written again if it already exists: its size and modification
    time are part of the parsing's input id.
    """
    demo = archive.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    if not demo.exists():
        demo.write_bytes(b"PBDEMS2\x00" + b"x" * 512)

    adapter = frame.drop("map_demo_id").with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        (pl.col("round_no") - 1).alias("score_start"),
        pl.col("round_no").alias("score_end"),
    )

    # The sample point table is Story 2.1's result and has no effect on the
    # classification, but it must not be empty: parse refuses a result with no
    # setup. The fake therefore gives one sample point per round, with the same
    # keys as in the rounds table.
    ticks_frame = _minimal_ticks(frame)

    # Utility has no effect on the classification at all, and an empty events
    # table is a valid result -- unlike an empty setup table. The fixture
    # therefore gives an empty table that still matches the contract.
    events_frame = pl.DataFrame(
        schema={name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS}
    )

    lineups_frame = _minimal_lineups(frame)
    deaths_frame = _minimal_deaths(frame)

    # The point cloud has no effect on the classification at all, and an empty
    # cloud is a valid result -- just like an empty events table. The fixture
    # therefore gives an empty table that still matches the contract.
    callouts_frame = pl.DataFrame(
        schema={name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS}
    )

    # The map's name has no effect on the classification at all, but the match
    # table has to be in place: parse requires exactly one row from it.
    match_frame = pl.DataFrame(
        [{"map_name": "de_ancient"}],
        schema={name: MATCH[name] for name in MATCH_ADAPTER_COLUMNS},
    )

    class Fake:
        def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
            return DemoTables(
                rounds=adapter,
                ticks=ticks_frame,
                events=events_frame,
                lineups=lineups_frame,
                deaths=deaths_frame,
                callouts=callouts_frame,
                match=match_frame,
            )

    parse_stage.run(
        parse_settings, archive, MAP_DEMO_ID, Fake(), demo_path=demo, force=force
    )


@pytest.fixture
def parsed(archive: ArchivePaths, settings) -> ArchivePaths:
    write_parse(archive, rounds_frame(match()), settings.parse)
    return archive


def run_classify(settings, archive, team=A, **kwargs):
    return classify_stage.run(
        settings.thresholds,
        settings.league,
        archive,
        MAP_DEMO_ID,
        team,
        economy=settings.economy,
        **kwargs,
    )


# --- A successful run ------------------------------------------------------------


def test_writes_a_valid_classified_table(settings, parsed) -> None:
    result = run_classify(settings, parsed)

    path = parsed.classified(A, MAP_DEMO_ID)
    assert path.is_file()
    df = pl.read_parquet(path)
    assert df.schema == dict(CLASSIFIED)
    assert df.height == 6, "one row per round, not two"
    assert df["round_no"].to_list() == [1, 2, 3, 4, 5, 6]
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert result.status == "ok"
    assert not result.skipped
    assert result.stats["rounds"] == 6


def test_result_is_written_under_the_subject_team(settings, parsed) -> None:
    run_classify(settings, parsed, team=A)
    assert parsed.classified(A, MAP_DEMO_ID).is_file()
    assert not parsed.classified(B, MAP_DEMO_ID).exists()


def test_every_row_carries_a_reason_and_its_inputs(settings, parsed) -> None:
    """Without the reason and the input values, calibrating in Story 1.4 is impossible."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df["reason"].null_count() == 0
    assert all(len(r) > 20 for r in df["reason"].to_list())
    for inputs in df["inputs"].to_list():
        assert inputs["players"] == 5
        assert inputs["full_equip_min"] == settings.thresholds.full_equip_min


def test_pistol_round_is_classified_from_the_round_number(settings, parsed) -> None:
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df.filter(pl.col("round_no") == 1)["round_type"].to_list() == ["pistol"]


def test_both_teams_are_classified_in_the_same_run(settings, parsed) -> None:
    """``opp_round_type`` is the other team's own ``round_type`` from the same run."""
    run_classify(settings, parsed, team=A)
    run_classify(settings, parsed, team=B)

    a = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    b = pl.read_parquet(parsed.classified(B, MAP_DEMO_ID)).sort("round_no")

    assert a["round_type"].to_list() == b["opp_round_type"].to_list()
    assert b["round_type"].to_list() == a["opp_round_type"].to_list()
    assert a["side"].to_list() != b["side"].to_list()


def test_loss_count_is_written_per_round(settings, parsed) -> None:
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    assert df["loss_count"][0] == settings.thresholds.loss_count_half_start
    assert df["loss_count"].is_between(
        settings.thresholds.loss_count_min, settings.thresholds.loss_count_max
    ).all()


def test_without_a_selection_file_the_league_fields_stay_empty(
    settings, parsed
) -> None:
    """A hand-imported demo: a guess would be worse than empty.

    This is also the test that fails if a missing selection file is turned into
    an exception: the run has to succeed and the values have to stay empty.
    """
    result = run_classify(settings, parsed)

    assert result.status == "ok"
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df["is_league"].null_count() == df.height
    assert df["roster_class"].null_count() == df.height


def test_write_is_atomic(settings, parsed) -> None:
    run_classify(settings, parsed)
    assert not has_temp_leftovers(parsed.root)


def test_nothing_is_written_into_the_parsed_area(settings, parsed) -> None:
    """``classify`` does not write into another stage's result area."""
    before = {
        p: p.stat().st_mtime_ns
        for p in (parsed.root / "parsed").rglob("*")
        if p.is_file()
    }
    run_classify(settings, parsed)
    after = {
        p: p.stat().st_mtime_ns
        for p in (parsed.root / "parsed").rglob("*")
        if p.is_file()
    }
    assert before == after


# --- is_league and roster_class from the selection file -------------------------
#
# The values are computed by ``select``, and this stage is their reader. The
# fixture therefore writes both files by hand: the team index, which is the
# bridge from the lineup key to the canonical ``team_key``, and the selection
# file, which holds the values.

#: The canonical ``team_key`` is FACEIT's ``faction_id``, that is, a UUID --
#: **not** a lineup hash. This difference is exactly why the lookup goes
#: through the team index's ``lineup_keys`` field: a direct lookup of
#: ``index/selections/<lineup_key>.json`` would always hit nothing.
TEAM_KEY = "0047af32-5ff8-449e-b665-8fd390e6a44d"
OTHER_TEAM_KEY = "f257054b-46d5-41bb-8e01-543777cd7092"


#: The fixtures' timestamp, **in the past**: the selection file is then older
#: than the manifest the run produces, and the staleness warning does not fire.
#: The warning has a test of its own with a timestamp of its own.
PAST = "2026-09-01T12:00:00+00:00"

#: The format versions of the indexes and of the selection file **as
#: literals**. Borrowing the constants (``discover.SCHEMA_VERSION``,
#: ``select.SCHEMA_VERSION``) would make the fixture always up to date even
#: when the format was stale; a literal number forces someone to look at the
#: fixture when the format rises -- and the reader sees what this test was
#: written against.
TEAMS_INDEX_VERSION = 1
SELECTION_VERSION = 1


def write_teams_index(archive: ArchivePaths, owners: dict[str, list[str]]) -> None:
    """A team index in which each ``team_key`` owns the given lineups."""
    keys = [key for lineups in owners.values() for key in lineups]
    document = {
        "schema_version": TEAMS_INDEX_VERSION,
        "generated_at": PAST,
        "competition_ids": ["kilpailu"],
        "contested_lineup_keys": sorted({k for k in keys if keys.count(k) > 1}),
        "teams": [
            {"team_key": team_key, "lineup_keys": list(lineups), "roster": []}
            for team_key, lineups in owners.items()
        ],
    }
    write_json(archive.teams_index(), document)


def write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def selection_row(
    *,
    map_demo_id: str = MAP_DEMO_ID,
    is_league: object = True,
    roster_class: str | None = "5/5",
    roster_ok: bool = True,
) -> dict[str, object]:
    """A selection row with every field, as ``select`` writes it."""
    return {
        "map_demo_id": map_demo_id,
        "match_id": "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd",
        "map_index": 1,
        "map_name": "de_ancient",
        "is_league": is_league,
        "certainly_played": True,
        "roster_ok": roster_ok,
        "roster_reason": "threshold met" if roster_ok else "threshold not met",
        "roster_class": roster_class,
        "roster_source": "match_players",
        "players_seen": 5,
        "regulars": [],
        "outsiders": [],
        "joined": [],
        "left": [],
    }


def write_selection(
    archive: ArchivePaths,
    rows: list[dict[str, object]],
    *,
    team_key: str = TEAM_KEY,
    generated_at: str = PAST,
) -> None:
    """A selection file for the team, in the shape ``read_selection`` accepts."""
    document = {
        "schema_version": SELECTION_VERSION,
        "generated_at": generated_at,
        "index_generated_at": generated_at,
        "competition_ids": ["kilpailu"],
        "team_key": team_key,
        "team_name": "Testijoukkue",
        "roster_size": 5,
        "roster_min_regulars": 4,
        "roster": [],
        "counts": {},
        "selections": rows,
    }
    write_json(archive.selection(team_key), document)


def facts_of(archive: ArchivePaths, team: str = A) -> tuple[list, list]:
    """The table's two columns as their unique values.

    Unique because the claim has two parts: the value is right **and** the same
    on every row. Checking one row would not notice if the value were set onto
    the first one only -- and that is exactly what ``domain.aggregate`` would
    stop the run over.
    """
    df = pl.read_parquet(archive.classified(team, MAP_DEMO_ID))
    assert df.height > 1, "setting it onto every row is part of the claim"
    return (
        df["is_league"].unique().to_list(),
        df["roster_class"].unique().to_list(),
    )


def test_the_match_facts_are_read_from_the_selection_file(settings, parsed) -> None:
    """A league match whose roster passed: both columns are filled."""
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])


def test_another_faceit_match_is_written_as_not_league(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league=False)])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([False], ["5/5"])


def test_a_substitute_map_carries_the_four_of_five_class(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class="4/5")])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["4/5"])


def test_the_roster_class_is_read_and_not_recomputed(settings, parsed) -> None:
    """The file says ``4/5``, although the roster would look like ``5/5``.

    The rounds table holds five players on every round, so a recomputed class
    would be ``5/5``. ``select`` is the only computer: it sees the match's
    player list and the standing roster, which this stage does not see. The
    test fails if the class is computed again here.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class="4/5")])

    run_classify(settings, parsed)

    rounds = pl.read_parquet(parsed.parsed_table(MAP_DEMO_ID, "rounds"))
    own = rounds.filter(pl.col("lineup_key") == A)
    assert own["players_buy_end"].unique().to_list() == [5], (
        "the fixture's roster is full -- otherwise the test would not tell "
        "reading from computing"
    )
    assert facts_of(parsed) == ([True], ["4/5"])


def test_a_rejected_map_still_carries_the_match_facts(settings, parsed) -> None:
    """Rejection is the sample's business and not a fact about the match: the values are read all the same."""
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_ok=False, roster_class="4/5")])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["4/5"])


def test_a_missing_selection_file_leaves_both_empty(settings, parsed) -> None:
    """The team is in the index, but ``select`` has not been run for it."""
    write_teams_index(parsed, {TEAM_KEY: [A]})

    result = run_classify(settings, parsed)

    assert result.status == "ok"
    assert facts_of(parsed) == ([None], [None])
    assert "pappascout select" in (result.reason or "")


def test_every_empty_reason_is_named_and_they_differ(settings, parsed) -> None:
    """Five different reasons, five different sentences -- not one silent blank.

    Without this claim a broken bridge would look in the report exactly like a
    hand-imported demo, and "why is the sample unknown" would have to be
    guessed. AD-9: an incomplete result belongs in ``reason`` and not in
    silence.
    """
    reasons: dict[str, str] = {}

    # 1. There is no index at all.
    reasons["no_index"] = run_classify(settings, parsed, force=True).reason or ""

    # 2. There is an index, but the lineup has no owner.
    write_teams_index(parsed, {TEAM_KEY: [B]})
    reasons["no_owner"] = run_classify(settings, parsed, force=True).reason or ""

    # 3. There is an owner, but no selection file.
    write_teams_index(parsed, {TEAM_KEY: [A]})
    reasons["no_file"] = run_classify(settings, parsed, force=True).reason or ""

    # 4. There is a file, but no row for the demo.
    write_selection(parsed, [selection_row(map_demo_id="1-toinen-demo-1-1")])
    reasons["no_row"] = run_classify(settings, parsed, force=True).reason or ""

    # 5. Two owners, disagreeing about the kind of match.
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league=True)], team_key=TEAM_KEY)
    write_selection(
        parsed, [selection_row(is_league=False)], team_key=OTHER_TEAM_KEY
    )
    reasons["conflict"] = run_classify(settings, parsed, force=True).reason or ""

    assert all(reasons.values()), f"every state says its reason: {reasons}"
    assert len(set(reasons.values())) == len(reasons), (
        f"five different reasons, five different sentences: {reasons}"
    )
    assert "team index" in reasons["no_index"].lower()
    assert A in reasons["no_owner"]
    assert "pappascout select" in reasons["no_file"]
    assert MAP_DEMO_ID in reasons["no_row"]
    assert "is_league" in reasons["conflict"]


def test_a_demo_that_has_no_row_in_the_file_leaves_both_empty(
    settings, parsed
) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(map_demo_id="1-toinen-demo-1-1")])

    result = run_classify(settings, parsed)

    assert result.status == "ok"
    assert facts_of(parsed) == ([None], [None])


def test_the_bridge_reads_the_lineup_keys_that_discover_writes(
    settings, parsed
) -> None:
    """The bridge is built with the **producer's own writer**, not by hand.

    A hand-written index pins only the test's own strings: if ``discover``
    wrote a different hash input, a different length or a prefix into
    ``lineup_keys``, ``owners`` would be empty **on every single demo** and not
    one hand-written fixture would fail -- and the result would look the same
    as a genuinely unknown demo. So the lineups are read with ``discover``'s
    own reader, joined with its own rule and written with its own document
    builder; the claim is that the same namespace comes out of ``classify``'s
    end.

    The threshold is zero, because this test's subject is **the ids'
    namespace** and not the roster rule: the fixture's player ids are not
    SteamID64s, so a roster intersection would make no sense here. The
    threshold has tests of its own in ``test_teams.py``.
    """
    lineups: dict[str, set[str]] = {}
    discover_stage._read_lineups(parsed, MAP_DEMO_ID, lineups)
    assert set(lineups) == set(classify_stage.team_keys(parsed, MAP_DEMO_ID)), (
        "discover and classify read the lineups from the same table under "
        "the same name"
    )

    teams, contested = assign_lineup_keys((Team(team_key=TEAM_KEY),), lineups, 0)
    document = discover_stage._teams_document(
        teams, contested, ["kilpailu"], datetime(2026, 9, 1, 12, tzinfo=UTC)
    )
    write_json(parsed.teams_index(), document)

    written = {key for row in document["teams"] for key in row["lineup_keys"]}
    assert set(classify_stage.team_keys(parsed, MAP_DEMO_ID)) & written, (
        "both ends of the bridge are in the same namespace"
    )

    # And the bridge carries the value all the way, not only the name.
    write_selection(parsed, [selection_row()])
    facts = classify_stage.read_match_facts(parsed, A, MAP_DEMO_ID)
    assert (facts.is_league, facts.roster_class) == (True, "5/5")
    assert facts.note is None


def test_a_lineup_that_no_team_owns_leaves_both_empty(settings, parsed) -> None:
    """The bridge is missing: the team in the index does not own this lineup."""
    write_teams_index(parsed, {TEAM_KEY: [B]})
    write_selection(parsed, [selection_row()])

    result = run_classify(settings, parsed, team=A)

    assert facts_of(parsed, A) == ([None], [None])
    # Distinguishable from a genuinely unknown demo: the reason names the
    # lineup for which no owner was found.
    assert A in (result.reason or "")


def test_each_team_gets_the_facts_from_its_own_selection_file(
    settings, parsed
) -> None:
    """``--kaikki-joukkueet``: each run reads its own team's file."""
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [B]})
    write_selection(parsed, [selection_row(roster_class="5/5")], team_key=TEAM_KEY)
    write_selection(
        parsed,
        [selection_row(roster_class="4/5", is_league=False)],
        team_key=OTHER_TEAM_KEY,
    )

    for team in classify_stage.team_keys(parsed, MAP_DEMO_ID):
        run_classify(settings, parsed, team=team)

    assert facts_of(parsed, A) == ([True], ["5/5"])
    assert facts_of(parsed, B) == ([False], ["4/5"])


def test_a_contested_lineup_that_agrees_still_fills_the_columns(
    settings, parsed
) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()], team_key=TEAM_KEY)
    write_selection(parsed, [selection_row()], team_key=OTHER_TEAM_KEY)

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])


def test_only_the_disagreeing_field_is_emptied(settings, parsed) -> None:
    """Two owners, different ``roster_class``, the same ``is_league``.

    The class is judged against **that team's** standing roster (AD-6), so two
    owners are allowed to have different values for it -- that is not a
    contradiction but the normal course of things. ``is_league`` describes the
    match (AD-10), and the owners agree about it. A record-level comparison
    would throw away an ``is_league`` everyone agrees on merely because the
    classes differed; this test fails if the consensus goes back to the record
    level.
    """
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class="5/5")], team_key=TEAM_KEY)
    write_selection(
        parsed, [selection_row(roster_class="4/5")], team_key=OTHER_TEAM_KEY
    )

    result = run_classify(settings, parsed)

    assert result.status == "ok"
    assert facts_of(parsed) == ([True], [None])
    assert "roster_class" in (result.reason or "")
    assert "is_league" not in (result.reason or "")


def test_a_disagreeing_league_flag_empties_only_that_field(
    settings, parsed
) -> None:
    """The same rule the other way round: the kind disagrees, the class is the same."""
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league=True)], team_key=TEAM_KEY)
    write_selection(
        parsed, [selection_row(is_league=False)], team_key=OTHER_TEAM_KEY
    )

    result = run_classify(settings, parsed)

    assert facts_of(parsed) == ([None], ["5/5"])
    assert "is_league" in (result.reason or "")


def test_two_rows_for_the_same_demo_are_not_resolved_by_the_first_one(
    settings, parsed
) -> None:
    """A duplicated row goes into the same consensus as two owners.

    "The first one wins" would be exactly the draw that was forbidden for
    contested lineups. The rows are in the same file and disagree about the
    class, so the class stays empty -- not the first one's value.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(
        parsed,
        [selection_row(roster_class="5/5"), selection_row(roster_class="4/5")],
    )

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], [None])


def test_one_owner_with_a_file_is_enough(settings, parsed) -> None:
    """Two owners, only one with a selection file: one vote is enough.

    Unanimity on one vote is deliberate: a missing file does not disagree, it
    is silent, and silence cannot overturn a value that was read.
    """
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()], team_key=TEAM_KEY)

    result = run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])
    assert result.reason is None


def test_an_owner_whose_file_lacks_the_demo_does_not_veto(
    settings, parsed
) -> None:
    """The same sibling: the other owner's file holds only another demo's row."""
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()], team_key=TEAM_KEY)
    write_selection(
        parsed,
        [selection_row(map_demo_id="1-toinen-demo-1-1", roster_class="4/5")],
        team_key=OTHER_TEAM_KEY,
    )

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])


def test_the_field_names_come_from_selects_own_document_builder(
    settings, parsed
) -> None:
    """The row is built with ``select``'s own writer, not by hand.

    Every other test digs raw keys out of a hand-written row, so renaming a
    field in ``select`` (``is_league`` -> ``league``) would keep them green and
    would empty production silently. This test goes through the producer:
    :class:`MapSelection` -> ``select._document`` -> :func:`read_match_facts`,
    so the field name is pinned to the code that writes it.
    """
    row = MapSelection(
        map_demo_id=MAP_DEMO_ID,
        match_id="1-a52ebff2-a23d-45eb-beb7-37271d96ddfd",
        map_index=1,
        map_name="de_ancient",
        is_league=True,
        roster_ok=True,
        roster_reason="threshold met",
        roster_class="4/5",
        roster_source="observed",
    )
    document = select_stage._document(
        [row],
        team=Team(team_key=TEAM_KEY),
        league=settings.league,
        thresholds=settings.thresholds,
        generated_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        index_generated_at=PAST,
    )
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_json(parsed.selection(TEAM_KEY), document)

    facts = classify_stage.read_match_facts(parsed, A, MAP_DEMO_ID)

    assert (facts.is_league, facts.roster_class) == (True, "4/5")
    assert facts.note is None


def test_a_foreign_roster_class_stops_the_run(settings, parsed) -> None:
    """The invalid value is derived from the schema and not hard-coded.

    The check and the error message both come from the ``CLASSIFIED`` schema's
    enum, so the test has to ask the same source: a hard-coded ``"3/5"`` would
    one day be valid for the schema and the test would pass measuring nothing.
    """
    allowed = classify_stage.roster_classes()
    foreign = f"vieras-{allowed[0]}"
    assert foreign not in allowed

    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class=foreign)])

    with pytest.raises(SchemaError) as err:
        run_classify(settings, parsed)

    message = str(err.value)
    assert foreign in message
    assert ", ".join(allowed) in message, "the message lists the same set"
    assert MAP_DEMO_ID in message
    assert not parsed.classified(A, MAP_DEMO_ID).exists()


def test_a_non_boolean_league_flag_stops_the_run(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league="kylla")])

    with pytest.raises(SchemaError) as err:
        run_classify(settings, parsed)

    assert "is_league" in str(err.value)


def test_a_broken_selection_file_advises_running_select(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    path = parsed.selection(TEAM_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(PappascoutError) as err:
        run_classify(settings, parsed)

    assert "pappascout select" in str(err.value)
    assert not parsed.classified(A, MAP_DEMO_ID).exists()


def test_the_selection_file_is_not_a_manifest_input(settings, parsed) -> None:
    """A skipped run carries the old value, and that is deliberate.

    The test pins where the connection stops: the selection file appearing does
    **not** invalidate a finished result, and ``--pakota`` is how the value is
    updated. Without this claim somebody would add the file to the manifest's
    ``inputs`` without noticing that it would force the whole archive to be
    classified again.
    """
    run_classify(settings, parsed)
    assert facts_of(parsed) == ([None], [None])

    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])

    skipped = run_classify(settings, parsed)
    assert skipped.skipped, "the manifest matches: the selection file is not an input"
    assert facts_of(parsed) == ([None], [None])

    forced = run_classify(settings, parsed, force=True)
    assert not forced.skipped
    assert facts_of(parsed) == ([True], ["5/5"])


def test_a_broken_selection_file_does_not_break_a_skipped_run(
    settings, parsed
) -> None:
    """The facts are read **after the skip branch**, and this pins the order.

    If the read is moved to the top of the function "into one place", one
    corrupted selection file would turn the whole archive's finished
    classifications into errors -- and not one other test would fail, because
    they all make a fresh run.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])
    first = run_classify(settings, parsed)
    assert not first.skipped

    parsed.selection(TEAM_KEY).write_text("not json", encoding="utf-8")

    result = run_classify(settings, parsed)

    assert result.skipped, "a finished result is neither re-read nor broken"
    assert result.status == "ok"
    assert facts_of(parsed) == ([True], ["5/5"])


def test_a_newer_selection_file_warns_and_names_the_flag(
    settings, parsed
) -> None:
    """Staleness is observable and not merely documented.

    The selection file is not a manifest input, so changing it does not
    invalidate the result -- and so it does not say anything about itself.
    Without the warning the table would carry an old ``is_league`` and the
    report would look up to date, and ``--pakota`` would rest on a human's
    memory.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])
    run_classify(settings, parsed)

    # Newer than the classification's manifest: select has been run again.
    write_selection(
        parsed,
        [selection_row(is_league=False)],
        generated_at="2099-01-01T00:00:00+00:00",
    )

    result = run_classify(settings, parsed)

    assert result.skipped
    assert "--pakota" in (result.reason or "")
    assert classify_stage.SKIP_REASON in (result.reason or "")
    # And when the file is older, there is no warning.
    write_selection(parsed, [selection_row()])
    assert run_classify(settings, parsed).reason == classify_stage.SKIP_REASON


# --- The round list as Markdown --------------------------------------------------


def test_writes_a_readable_round_list_beside_the_table(settings, parsed) -> None:
    result = run_classify(settings, parsed)
    path = parsed.classified_round_list(A, MAP_DEMO_ID)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    assert MAP_DEMO_ID in text
    assert text.count("\n|") >= 6, "a row for every round"
    # The thresholds are there, otherwise the list does not say what the
    # decision was made against. The check is aimed at the heading's SENTENCE
    # and not at the number alone: the fixture's money sums hold the same
    # digits, so a bare "1000" would be found on the table's rows even if the
    # heading were broken.
    t = settings.thresholds
    header_line = next(r for r in text.splitlines() if r.startswith("- Thresholds"))
    assert f"a full buy is at least {t.full_equip_min}" in header_line
    assert f"after a win at most {t.anomaly_equip_max_after_win}" in header_line
    assert f"a buy needs at least {t.force_buy_min}" in header_line
    # The half-buy's two conditions are on a line of their own: they are not
    # dollar thresholds per player but player counters, and crammed into one
    # sentence neither would be readable.
    half_line = next(r for r in text.splitlines() if r.startswith("- The half-buy"))
    assert f"at least {t.armed_players_min} players armed" in half_line
    assert f"at least {t.normal_buy_players_min} players" in half_line
    assert f"at least {t.normal_buy_money_min} $" in half_line
    # The loss bonus's steps are there: without them condition B's figure
    # cannot be checked, because the bonus shows nowhere else in the list.
    bonus_line = next(r for r in text.splitlines() if r.startswith("- Condition B"))
    for step in settings.economy.loss_bonus_steps:
        assert str(step) in bonus_line
    assert str(settings.league.ot_start_money) in text
    # Retired thresholds are not mentioned: the heading says only what the
    # classification really compared.
    for retired in (
        "eco_money_max",
        "eco_loss_count_min",
        "force_money_min",
        "force_money_max",
        "half_equip_min",
    ):
        assert retired not in text
    assert str(path.relative_to(parsed.root)).replace("\\", "/") in [
        str(o) for o in result.outputs
    ]


def test_round_list_is_listed_as_an_output_in_the_manifest(settings, parsed) -> None:
    run_classify(settings, parsed)
    manifest = Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID))
    assert any(o.endswith(".md") for o in manifest.outputs)
    assert any(o.endswith(".parquet") for o in manifest.outputs)


# --- The manifest and the skip ---------------------------------------------------


def test_manifest_has_no_tool_versions(settings, parsed) -> None:
    """Classification is pure domain computation: no library version changes it."""
    run_classify(settings, parsed)
    manifest = Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID))
    assert manifest.stage == "classify"
    assert manifest.tool_versions == {}
    assert manifest.inputs[0].result_id == f"parsed/{MAP_DEMO_ID}"


def test_second_run_is_skipped(settings, parsed) -> None:
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    before = path.stat().st_mtime_ns

    result = run_classify(settings, parsed)
    assert result.skipped
    assert path.stat().st_mtime_ns == before
    assert result.stats["rounds"] == 6
    assert result.stats["rows"], "the round list is read from the finished result"


def test_force_overrides_a_matching_manifest(settings, parsed) -> None:
    run_classify(settings, parsed)
    assert not run_classify(settings, parsed, force=True).skipped


def test_a_stale_inputs_struct_is_recomputed_not_read(settings, parsed) -> None:
    """An old result whose ``inputs`` structure is a different shape is run again.

    The fields of the ``inputs`` struct changed during the calibration on
    2026-08-29 without the manifest's schema version changing, so a matching
    manifest can point at a table in the old shape. That has to lead to a
    recomputation -- not to a crash and not to the old result being returned
    silently.
    """
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    assert run_classify(settings, parsed).skipped, "precondition: the manifest matches"

    # Write the table again with the old-shaped inputs structure: the retired
    # thresholds back, the new ones out. Both have to be really retired or
    # really new -- removing a live key would test something other than what
    # the name promises.
    df = pl.read_parquet(path)
    old_inputs = []
    for i in df["inputs"].to_list():
        removed_in_this_story = (
            "money_players",
            "players_armed",
            "players_can_buy",
            "loss_bonus_if_lost",
            "armed_players_min",
            "normal_buy_money_min",
            "normal_buy_players_min",
        )
        row = {
            k: v for k, v in i.items() if k not in removed_in_this_story
        }
        row["eco_money_max"] = 2000
        row["force_money_min"] = 1500
        row["force_money_left_max"] = 1000
        old_inputs.append(row)
    df.with_columns(pl.Series("inputs", old_inputs)).write_parquet(path)

    result = run_classify(settings, parsed)
    assert not result.skipped, "a result in the old shape must not be returned as it is"
    assert result.status == "ok"
    fields = set(pl.read_parquet(path)["inputs"].to_list()[0])
    assert "force_buy_min" in fields
    assert "eco_money_max" not in fields


def test_threshold_change_reruns_classify_but_not_parse(
    tmp_path: Path, archive
) -> None:
    """The acceptance criterion: a threshold change re-runs the classification, not the parsing."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"full_equip_min = 4000": "full_equip_min = 4100"}
        ),
        encoding="utf-8",
    )
    a = load_settings(base_toml, env_files=())
    b = load_settings(changed_toml, env_files=())

    write_parse(archive, rounds_frame(match()), a.parse)
    parse_mtime_before = archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    run_classify(a, archive)
    result = run_classify(b, archive)

    assert not result.skipped, "after a threshold change the classification is run again"
    assert archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_mtime_before
    ), "the parsing must not be run again"


def test_a_forced_reparse_with_the_same_result_does_not_rerun_classify(
    settings, parsed
) -> None:
    """The classification's input is the parsing's **result**, not the moment it ran.

    Without this, every ``parse --pakota`` would force a new classification as
    well, even when the rounds table was byte for byte the same.
    """
    run_classify(settings, parsed)
    write_parse(parsed, rounds_frame(match()), settings.parse, force=True)
    assert run_classify(settings, parsed).skipped


def test_a_changed_demo_forces_a_new_classification(settings, parsed) -> None:
    """A new parse of a new demo must not stay behind the old classification."""
    run_classify(settings, parsed)
    demo = parsed.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 4096)
    write_parse(parsed, rounds_frame(match()), settings.parse)

    assert not run_classify(settings, parsed).skipped


def test_missing_output_forces_a_rerun(settings, parsed) -> None:
    run_classify(settings, parsed)
    parsed.classified(A, MAP_DEMO_ID).unlink()
    assert not run_classify(settings, parsed).skipped


def test_unreadable_result_is_recomputed_not_reported(settings, parsed) -> None:
    """Classification is cheap: a broken result is computed again."""
    run_classify(settings, parsed)
    parsed.classified(A, MAP_DEMO_ID).write_bytes(b"not parquet")

    result = run_classify(settings, parsed)
    assert not result.skipped
    assert result.stats["rounds"] == 6
    assert pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).height == 6


def test_result_that_no_longer_matches_the_contract_is_recomputed(
    settings, parsed
) -> None:
    """A matching manifest is not enough if the result table's contract has changed.

    Widening the schema does not change the manifest's content, so an old
    result would look up to date but the new values would be missing from it.
    """
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    pl.read_parquet(path).drop("loss_count").write_parquet(path)

    result = run_classify(settings, parsed)
    assert not result.skipped
    assert "loss_count" in pl.read_parquet(path).columns


# --- Choosing the team -----------------------------------------------------------


def test_team_can_be_given_as_a_unique_prefix(settings, parsed) -> None:
    run_classify(settings, parsed, team=A[:6])
    assert parsed.classified(A, MAP_DEMO_ID).is_file()


def test_unknown_team_lists_both_lineups_of_the_demo(settings, parsed) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed, team="eitallaista")
    message = str(exc.value)
    assert A in message
    assert B in message
    assert "matches neither lineup" in message


def test_missing_team_lists_both_lineups_too(settings, parsed) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed, team=None)
    message = str(exc.value)
    assert A in message and B in message
    assert "--team" in message


def test_ambiguous_prefix_is_refused(settings, archive) -> None:
    """A shared prefix must not choose a lineup by drawing lots."""
    frame = rounds_frame(match()).with_columns(
        pl.when(pl.col("lineup_key") == A)
        .then(pl.lit("yhteinen1"))
        .otherwise(pl.lit("yhteinen2"))
        .alias("lineup_key")
    )
    write_parse(archive, frame, settings.parse)
    with pytest.raises(PappascoutError, match="matches more than one lineup"):
        run_classify(settings, archive, team="yhteinen")


# --- Errors ----------------------------------------------------------------------


def test_unparsed_demo_tells_which_command_to_run(settings, archive) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, archive)
    message = str(exc.value)
    assert "has not been parsed yet" in message
    assert "pappascout parse" in message


def test_failed_parse_is_not_classified_over(settings, parsed) -> None:
    manifest = Manifest.read(parsed.parsed_manifest(MAP_DEMO_ID))
    broken = manifest.model_copy(
        update={"status": "parse_failed", "reason": "the demo is truncated"}
    )
    broken.write(parsed.parsed_manifest(MAP_DEMO_ID))

    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed)
    assert "parse_failed" in str(exc.value)
    assert "the demo is truncated" in str(exc.value)


def test_missing_parse_manifest_names_the_path_and_the_next_command(
    settings, parsed
) -> None:
    parsed.parsed_manifest(MAP_DEMO_ID).unlink()
    with pytest.raises(PappascoutError, match="parse manifest was not found"):
        run_classify(settings, parsed)


def test_rounds_table_that_breaks_the_contract_is_refused(
    settings, parsed
) -> None:
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).drop("survivors").write_parquet(path)
    with pytest.raises(SchemaError, match="survivors"):
        run_classify(settings, parsed)


def test_outdated_rounds_table_tells_the_user_to_reparse(
    settings, parsed
) -> None:
    """An old table is the user's situation, not the developer's.

    ``validate`` speaks to the developer: "Add the column or fix the stage that
    produced the table -- the contract is in the file domain/schemas.py." That
    is the wrong advice for someone who does not code: the table in the archive
    was parsed with an older version, and the fix is to parse it again.

    The column is the armed counter, because it is the most recent ``ROUNDS``
    extension and therefore the one this situation really arises over.
    """
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).drop(ARMED_COLUMN).write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run_classify(settings, parsed)

    message = str(exc.value)
    # The diagnosis stays: the message names the column that is missing.
    assert ARMED_COLUMN in message
    # The advice is an action the user takes, not a code change.
    assert "parsed with an older version of the program" in message
    assert f"pappascout parse {MAP_DEMO_ID} --pakota" in message
    # The developer's advice must not leak in: it would tell the reader to edit
    # code the user does not write.
    #
    # The second guard names the English text ``validate`` writes today. It
    # named the Finnish wording until T9 translated ``domain/schemas.py``, and
    # from that commit on it matched nothing and guarded nothing -- a negative
    # guard whose subject was translated in another tranche. Found in T6.
    assert "domain/schemas.py" not in message
    assert "Add the column" not in message


def test_a_round_without_an_anchor_does_not_break_the_run(
    settings, archive
) -> None:
    """The I/O matrix: a round with no anchor is left unclassified, the run goes on."""
    rounds = match()
    rounds[2] = round_rows(3, status="no_freeze_end")
    write_parse(archive, rounds_frame(rounds), settings.parse)

    result = run_classify(settings, archive)
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID)).sort("round_no")

    assert df.height == 6
    assert df["round_type"][2] is None
    assert "no_freeze_end" in df["reason"][2]
    assert df["round_type"].null_count() == 1
    assert result.stats["unclassified"] == 1


def test_short_handed_team_is_divided_by_the_observed_count(
    settings, archive
) -> None:
    """A short-handed team: the per-player value is computed with the right count."""
    total = 4 * settings.thresholds.full_equip_min
    rounds = match()
    rounds[3] = round_rows(4, a_won=False, a_equip=total, a_players=4)
    write_parse(archive, rounds_frame(rounds), settings.parse)

    run_classify(settings, archive)
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID)).sort("round_no")
    row = df.row(3, named=True)
    assert row["inputs"]["players"] == 4
    assert row["round_type"] == "full"




# --- Edge cases the review raised ------------------------------------------------


def test_unnumbered_rounds_are_dropped_and_counted(settings, archive) -> None:
    """An unnumbered row would break the loss count; it is dropped and told.

    The rounds table is written directly here, because ``parse`` itself does
    not let an unnumbered row through -- but the archive may hold a table
    written with an older version, and the classification must not fail on it.
    """
    write_parse(archive, rounds_frame(match()), settings.parse)
    path = archive.parsed_table(MAP_DEMO_ID, "rounds")
    table = pl.read_parquet(path)
    unnumbered = table.head(2).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        pl.lit(99, dtype=pl.Int32).alias("round_raw"),
    )
    pl.concat([unnumbered, table]).write_parquet(path)

    result = run_classify(settings, archive)
    assert result.stats["unnumbered"] == 1
    assert result.stats["rounds"] == 6
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID))
    assert df["round_no"].null_count() == 0


def test_skipped_run_gives_exactly_the_same_round_list(settings, parsed) -> None:
    """One path to the round list: a skip must not show different figures.

    If a fresh run and a skipped one built the rows differently, ``--show``
    would show on the second run, for example, the opponent's economy on the
    subject's rounds -- and nothing would say so.
    """
    fresh = run_classify(settings, parsed)
    skipped_run = run_classify(settings, parsed)

    assert skipped_run.skipped
    assert not fresh.skipped
    assert skipped_run.stats["rows"] == fresh.stats["rows"]
    assert skipped_run.stats["by_type"] == fresh.stats["by_type"]


def test_league_change_reruns_classify_but_not_parse(tmp_path: Path, archive) -> None:
    """``[league]`` is part of the classification's parameter hash just as the thresholds are."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"ot_start_money = 12500": "ot_start_money = 10000"}
        ),
        encoding="utf-8",
    )
    a = load_settings(base_toml, env_files=())
    b = load_settings(changed_toml, env_files=())

    write_parse(archive, rounds_frame(match()), a.parse)
    parse_mtime_before = archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    run_classify(a, archive)
    result = run_classify(b, archive)

    assert not result.skipped
    assert archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_mtime_before
    )


def test_markdown_row_matches_the_parquet_row(settings, parsed) -> None:
    """The table's content, not only its shape: the type and the reason are the same."""
    run_classify(settings, parsed)
    text = parsed.classified_round_list(A, MAP_DEMO_ID).read_text(encoding="utf-8")
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    expected = df.row(0, named=True)

    row = next(r for r in text.splitlines() if r.startswith("| 1 |"))
    cells = [s.strip() for s in row.strip("|").split("|")]
    headers = [o for o, _ in classify_stage.ROUND_LIST_COLUMNS]
    fields = dict(zip(headers, cells))

    assert fields["Type"] == str(expected["round_type"])
    assert fields["Opp."] == str(expected["opp_round_type"])
    assert fields["Loss"] == str(expected["loss_count"])
    assert fields["Side"] == str(expected["side"])
    # The reason is the same text, only with the Markdown escapes removed.
    assert fields["Reason"].replace("\\", "") == str(expected["reason"]).replace(
        "\\", ""
    )
    # And the per-player values match the inputs structure.
    inputs = expected["inputs"]
    assert fields["Equipment"] == str(
        per_player(inputs["equip_buy_end"], inputs["players"])
    )


def test_markdown_escapes_everything_that_would_break_the_table(settings) -> None:
    """A newline would break the table and a backtick would eat the rest of the line."""
    rows = [
        {
            "round_no": 1,
            "side": "T",
            "won": True,
            "round_type": "eco",
            "opp_round_type": "full",
            "loss_count": 1,
            "money_per_player": 100,
            "money_available_per_player": 200,
            "spent_per_player": 100,
            "equip_per_player": 300,
            "players": 5,
            "reason": "Row\nbreak | pipe `backtick`.",
        }
    ]
    text = classify_stage.render_round_list_markdown(
        rows,
        map_demo_id=MAP_DEMO_ID,
        team_key=A,
        thresholds=settings.thresholds,
        league=settings.league,
        economy=settings.economy,
    )
    table_lines = [r for r in text.splitlines() if r.startswith("| 1 |")]
    assert len(table_lines) == 1, "a newline must not break the cell"
    row = table_lines[0]
    assert row.count("|") == len(classify_stage.ROUND_LIST_COLUMNS) + 1 + 1
    assert "\\`" in row
    assert "\\|" in row


def test_markdown_is_byte_identical_on_a_rerun(settings, parsed) -> None:
    """The moment of the run belongs in the manifest, not in the output -- otherwise differences do not show."""
    run_classify(settings, parsed)
    before = parsed.classified_round_list(A, MAP_DEMO_ID).read_bytes()
    run_classify(settings, parsed, force=True)
    assert parsed.classified_round_list(A, MAP_DEMO_ID).read_bytes() == before
    # The timestamp is safe all the same.
    assert Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID)).created_at


def test_rounds_table_of_another_demo_is_refused(settings, parsed) -> None:
    """The wrong parquet in the right path would be classified under the wrong id."""
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).with_columns(
        pl.lit("1-toinen-demo-1").alias("map_demo_id")
    ).write_parquet(path)

    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed)
    assert "holds rows of another demo" in str(exc.value)
    assert "1-toinen-demo-1" in str(exc.value)


def test_three_lineups_are_refused_with_the_right_count(settings, archive) -> None:
    rounds = match(4)
    rounds[3][1]["lineup_key"] = "cccccccccccccccc"
    write_parse(archive, rounds_frame(rounds), settings.parse)

    with pytest.raises(SchemaError) as exc:
        run_classify(settings, archive)
    message = str(exc.value)
    assert "3 lineups" in message
    assert "cccccccccccccccc" in message


def test_round_number_mismatch_between_teams_is_refused(settings, parsed) -> None:
    """Without the check the opponent's type would be joined to the wrong row."""
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).with_columns(
        pl.when((pl.col("lineup_key") == B) & (pl.col("round_no") == 6))
        .then(pl.lit(7, dtype=pl.Int32))
        .otherwise(pl.col("round_no"))
        .alias("round_no")
    ).write_parquet(path)

    with pytest.raises(SchemaError, match="round numbers do not match"):
        run_classify(settings, parsed)


def test_team_keys_lists_both_lineups(settings, parsed) -> None:
    assert classify_stage.team_keys(parsed, MAP_DEMO_ID) == sorted([A, B])


def test_inputs_carry_the_money_that_was_available(settings, parsed) -> None:
    """Story 1.4 needs the money that was available, not only what was left."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    for inputs in df["inputs"].to_list():
        assert inputs["money_spent"] == 20000
        assert inputs["money_buy_end"] + inputs["money_spent"] == 25000
        assert inputs["force_buy_min"] == settings.thresholds.force_buy_min
        assert (
            inputs["normal_buy_money_min"]
            == settings.thresholds.normal_buy_money_min
        )
        # Story 1.10: the distribution and the counters of both conditions
        # travel along, so that the round list's row can be checked without a
        # new run.
        assert sum(inputs["money_players"]) == inputs["money_buy_end"]
        assert inputs["players_can_buy"] is not None
        assert inputs["loss_bonus_if_lost"] in settings.economy.loss_bonus_steps


# --- Real demos ------------------------------------------------------------------


def real_rounds(demo_name: str, map_demo_id: str) -> pl.DataFrame:
    """A real demo's rounds table in ``ROUNDS`` shape, with no archive."""
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    # One sample point is enough: this helper uses the rounds table only, and
    # the port returns both from the same read. The exclusion list **and the
    # buy window** are production's, so that the adapter is run under the same
    # rules as it really is. Without the window the adapter's default is 0.0,
    # that is, the measurement from the anchor, and this file's whole
    # demo-based series would confirm verdicts about figures the product no
    # longer produces.
    parse_settings_real = load_settings(REAL_SETTINGS, env_files=()).parse
    adapter = Demoparser2Adapter(
        exclude_weapons=parse_settings_real.first_contact_exclude_weapons,
        fallback_death=parse_settings_real.first_contact_fallback_death,
        buy_window_seconds=parse_settings_real.buy_window_seconds,
    )
    tables = adapter.parse_demo(require_demo(demo_name), (6.0,))
    raw = mark_played_rounds(tables.rounds)
    df = raw.filter(pl.col("round_no").is_not_null()).select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    )
    return validate(df.sort("round_no", "side"), ROUNDS, "rounds")


def subject_key(df: pl.DataFrame) -> str:
    """The lineup that started on the T side.

    In both test demos it is ``team_SSStttNNN``
    (``_bmad-output/implementation-artifacts/testiaineisto.md``). The name
    cannot be read from the demo -- the rounds table holds only the lineup hash
    -- so the subject is recognised from the starting side.
    """
    return str(
        df.filter((pl.col("round_no") == 1) & (pl.col("side") == "T"))["lineup_key"][0]
    )


@pytest.mark.demo
def test_ancient_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    """A regression: the verified sequence pistol -> saving round -> full buy."""
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(ANCIENT_DEM, "ancient")
    df_, rows = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient", economy=economy, facts=NO_FACTS
    )
    assert df_.height == ANCIENT_ROUNDS
    assert df_.sort("round_no")["round_type"].to_list()[:3] == ["pistol", "eco", "full"]
    # The reason says the money and the loss count on every round.
    assert all("loss count" in str(r["reason"]) for r in rows)


@pytest.mark.demo
def test_ancient_has_no_unclassified_rounds(settings_file: Path) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(ANCIENT_DEM, "ancient")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient", economy=economy, facts=NO_FACTS
    )
    assert result["round_type"].null_count() == 0


@pytest.mark.demo
def test_nuke_overtime_rounds_get_no_economy_reasoning(settings_file: Path) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(NUKE_ZST, "nuke")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "nuke", economy=economy, facts=NO_FACTS
    )

    assert result.height == NUKE_ROUNDS
    overtime = result.filter(pl.col("round_no") > thresholds.regulation_rounds)
    assert sorted(overtime["round_no"].to_list()) == [25, 26, 27, 28]
    assert set(overtime["round_type"].to_list()) == {"ot"}
    assert set(overtime["opp_round_type"].to_list()) == {"ot"}
    assert all("is overtime" in r for r in overtime["reason"].to_list())


@pytest.mark.demo
def test_nuke_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(NUKE_ZST, "nuke")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "nuke", economy=economy, facts=NO_FACTS
    )
    assert result.sort("round_no")["round_type"].to_list()[:3] == [
        "pistol",
        "eco",
        "full",
    ]


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name,identifier", [(ANCIENT_DEM, "ancient"), (NUKE_ZST, "nuke")]
)
def test_opponent_type_matches_the_other_teams_own_type(
    settings_file: Path, demo_name: str, identifier: str
) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(demo_name, identifier)
    a = subject_key(df)
    b = next(k for k in df["lineup_key"].unique().to_list() if k != a)

    own, _ = classify_stage.classify_rounds(
        df, a, thresholds, identifier, economy=economy, facts=NO_FACTS
    )
    other, _ = classify_stage.classify_rounds(
        df, b, thresholds, identifier, economy=economy, facts=NO_FACTS
    )

    assert own.sort("round_no")["round_type"].to_list() == (
        other.sort("round_no")["opp_round_type"].to_list()
    )


# --- Calibration on a real demo (Story 1.9) -----------------------------------


@pytest.mark.demo
def test_ancient_calibration_verdicts_hold_on_the_real_demo(
    settings_file: Path,
) -> None:
    """The calibration's 15 verdicts **from a real demo**, not from a hand-built row.

    ``test_calibration.py`` pins the rule: on the given figures it gives the
    product owner's verdict. It cannot establish that **those figures** are
    read from the demo -- there the table's rows are the input, and if the
    measurement drifts apart, the rule
    still passes on figures of its own.

    This closes the chain from the other end: the demo is parsed with
    production's settings, classified with production's thresholds, and each of
    the 15 rows' verdicts is compared against the one the product owner gave.
    The earlier check, :func:`test_ancient_has_no_unclassified_rounds`, settles
    for the value not being empty -- which ``anomaly`` and any wrong verdict
    satisfy, and it does not cover thirteen of these fifteen rows at all.

    **The figures are checked as well as the verdict**, because a verdict
    survives surprisingly large changes: round 21 T is eco at both 710 and 750
    dollars, so the verdict alone would not notice the measurement point
    sliding.
    """
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(ANCIENT_DEM, "ancient")

    observed: dict[tuple[int, str], dict] = {}
    for team in df["lineup_key"].unique().to_list():
        result, _ = classify_stage.classify_rounds(
            df, team, thresholds, "ancient", economy=economy, facts=NO_FACTS
        )
        for row in result.iter_rows(named=True):
            observed[(int(row["round_no"]), str(row["side"]))] = row

    for k in TRUTH_TABLE:
        row = observed.get((k.round_no, k.side))
        assert row is not None, f"round {k.round_no} {k.side} is missing from the demo"

        assert row["round_type"] == k.truth, (
            f"Round {k.round_no} {k.side}: the product owner says "
            f"{k.truth!r} "
            f"({k.basis}), classified from the demo {row['round_type']!r}. "
            f"Reason: {row['reason']}"
        )

        inputs = row["inputs"]
        players = int(inputs["players"])
        left = per_player(inputs["money_buy_end"], players)
        equip = per_player(inputs["equip_buy_end"], players)
        bought = per_player(
            inputs["equip_buy_end"] - inputs["equip_round_start"], players
        )
        assert (left, bought, equip) == (k.left, k.bought, k.equip), (
            f"Round {k.round_no} {k.side}: the truth table's figures are "
            f"{(k.left, k.bought, k.equip)}, the demo gives "
            f"{(left, bought, equip)} (left / bought / equipment, "
            "$/player). Update the table's figures and the changelog -- the "
            "verdict is not touched."
        )


@pytest.mark.demo
def test_the_calibration_demo_is_measured_from_the_buy_window(
    settings_file: Path,
) -> None:
    """The calibration demo is measured from the end of the buy time, not from the anchor.

    The previous test would pass even if both the measurement and the truth
    table fell back the same distance to the anchor -- two mistakes that cancel
    each other out. This one establishes the measurement point directly: every
    round has a ``buy_end_tick``, and at least one of them is behind the
    anchor.
    """
    df = real_rounds(ANCIENT_DEM, "ancient")

    assert df["buy_end_tick"].null_count() == 0
    later = df.filter(pl.col("buy_end_tick") > pl.col("freeze_end_tick"))
    assert later.height > 0, (
        "on not one round is the measurement point behind the anchor -- "
        "real_rounds is probably running without production's buy window"
    )
