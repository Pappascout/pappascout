"""``stages.aggregate`` -- the stage's tests.

The stage does not read the demo, so its whole logic -- collecting the team,
reading the tables, the atomic write, the manifest and the skip -- is tested
with hand-built tables in a temporary archive. The only tests that need a demo
are the regressions at the end, and they skip themselves cleanly.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from time import sleep

import polars as pl
import pytest

from conftest import (
    LEAGUE_DEMOS,
    SITE_CLOUD,
    has_temp_leftovers,
    require_demo,
)
from pappascout.archive.manifest import Manifest, ManifestInput
from pappascout.archive.paths import ArchivePaths
from pappascout.cli import _render_aggregate
from pappascout.domain.models import load_settings
from pappascout.domain.report import Report
from pappascout.domain.schemas import ARMORED_COLUMN

SRC = Path(__file__).resolve().parents[1] / "src" / "pappascout"

#: The fields the stage reads from its settings sections. Read from the
#: source, so that the hashed list cannot go stale silently.
THRESHOLD_READ = r"\bthresholds\.([a-z_]+)"
LEAGUE_READ = r"\bleague\.([a-z_]+)"
from pappascout.errors import AggregateError, PappascoutError, SchemaError
from pappascout.stages import aggregate as aggregate_stage
from test_aggregate import (
    OPPONENT,
    OPPONENT_CLAN,
    TEAM,
    TEAM_CLAN,
    aggregate_settings,
    callouts_frame,
    classified_frame,
    classified_row,
    death_row,
    deaths_frame,
    event_rows,
    events_frame,
    lineup_row,
    lineups_frame,
    match_frame,
    report_for,
    round_row,
    rounds_frame,
    tick_row,
    ticks_frame,
    thresholds,
)


# --- Building the archive -------------------------------------------------------


def build_archive(
    tmp_path: Path,
    demos: dict[str, str],
    *,
    rounds: int = 2,
    players: int = 5,
    write_parsed: bool = True,
    write_manifest: bool = True,
    opponent_players: int = 5,
    clan: str | None = TEAM_CLAN,
    clan_by_demo: dict[str, str | None] | None = None,
    player_names: bool = True,
    bench_player: str | None = None,
    map_names: dict[str, str | None] | None = None,
    callouts: dict[str, Sequence[tuple[str, int, int, int]]] | None = None,
    is_league: dict[str, bool | None] | None = None,
    roster_class: dict[str, str | None] | None = None,
) -> ArchivePaths:
    """Build an archive that holds the given demos under the given lineups.

    Args:
        demos: ``map_demo_id -> lineup_key``.
        rounds: Rounds per demo.
        players: The number of the subject team's players in the sample point
            table.
        write_parsed: Whether the ``parsed/`` tables are written. ``False``
            produces a missing demo.
        write_manifest: Whether the classification's manifest is written.
        opponent_players: The number of the opponent's players; their rows must
            not end up in the report.
        clan: The subject team's clan name in the lineups table. ``None`` = the
            name was not observed, and the report then has to speak of the id.
        clan_by_demo: A per-demo exception to ``clan`` -- for building a name
            conflict.
        player_names: Whether names are written for the players. ``False``
            leaves them empty, and the roster then holds the SteamID alone.
        bench_player: A player who is **only in the lineups table** and on not
            one sample point. Exactly such a player would disappear if the
            lineups were read from the ``ticks`` table -- and that is the whole
            reason they are read from the ``lineups`` table.
        map_names: ``map_demo_id -> the map's name in the header``. The default
            is that there is no name in the header (``None``), and the name is
            then inferred from the id as it was before Story 2.11 -- so the old
            tests still measure the inference and the new ones the observation.
        callouts: ``map_demo_id -> the point cloud's cells``. The default is an
            **empty cloud**, so no site groups are obtained and the stack rule
            stays silent -- so the old tests still measure what they measured
            before Story 2.14. A cell is ``(area, cell_x, cell_y, cell_z)``.
        is_league: ``map_demo_id -> is_league``. The default is ``None``, that
            is, **the state of a hand-imported demo**, so the sample is in the
            ``unknown`` bucket as it was before Story 3.8 -- so the old tests
            still measure what they measured. The value is per demo, because it
            describes the match and not the round.
        roster_class: ``map_demo_id -> roster_class``. Same contract as
            ``is_league`` and for the same reason: the default ``None`` is the
            state of the archive, and the value describes the map rather than
            the round, so it is written onto every round of the demo.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    for demo, lineup in demos.items():
        classified = classified_frame(
            [
                classified_row(
                    demo,
                    n,
                    round_type="pistol" if n == 1 else "full",
                    is_league=(is_league or {}).get(demo),
                    roster_class=(roster_class or {}).get(demo),
                )
                for n in range(1, rounds + 1)
            ]
        )
        path = archive.classified(lineup, demo)
        path.parent.mkdir(parents=True, exist_ok=True)
        classified.write_parquet(path)

        if write_manifest:
            Manifest.new(
                result_id=f"classified/{lineup}/{demo}",
                stage="classify",
                params_hash="hash",
                inputs=[ManifestInput(result_id=f"parsed/{demo}", sha256="x")],
                outputs=(f"classified/{lineup}/{demo}.parquet",),
            ).write(archive.classified_manifest(lineup, demo))

        if not write_parsed:
            continue
        ticks = ticks_frame(
            [
                tick_row(demo, n, f"{lineup}-p{i}", "BombsiteA", lineup=lineup)
                for n in range(1, rounds + 1)
                for i in range(players)
            ]
            + [
                tick_row(
                    demo,
                    n,
                    f"{OPPONENT}-p{i}",
                    "BombsiteB",
                    lineup=OPPONENT,
                    side="CT",
                )
                for n in range(1, rounds + 1)
                for i in range(opponent_players)
            ]
        )
        events = events_frame(
            event_rows(demo, 1, 0, "smoke", lineup=lineup)
            + event_rows(demo, 1, 1, "he", lineup=OPPONENT, side="CT")
        )
        # Two deaths on round 1: one for our own player (an own death) and one
        # for the opponent (an own kill). Both are needed, because the stage
        # filters over two different columns -- with one row the other filter
        # would be left unverified.
        deaths = deaths_frame(
            [
                death_row(
                    demo,
                    1,
                    victim=f"{lineup}-p0",
                    victim_lineup=lineup,
                    attacker=f"{OPPONENT}-p0",
                    attacker_lineup=OPPONENT,
                ),
                death_row(
                    demo,
                    1,
                    victim=f"{OPPONENT}-p1",
                    victim_lineup=OPPONENT,
                    victim_side="CT",
                    victim_area="BombsiteB",
                    attacker=f"{lineup}-p1",
                    attacker_lineup=lineup,
                    attacker_side="T",
                    attacker_area="Middle",
                    t_s=30.0,
                ),
            ]
        )
        # The rounds table: two rows per round, one for each team -- as parse
        # writes it. Only the armour counter is read from here, but both rows
        # are present so that the lineup filter is really under test.
        rounds_table = rounds_frame(
            [
                round_row(demo, n, lineup=lineup, side="T", armored=5)
                for n in range(1, rounds + 1)
            ]
            + [
                round_row(demo, n, lineup=OPPONENT, side="CT", armored=0)
                for n in range(1, rounds + 1)
            ]
        )
        demo_clan = (clan_by_demo or {}).get(demo, clan)
        lineups = lineups_frame(
            [
                lineup_row(
                    demo,
                    f"{lineup}-p{i}",
                    lineup=lineup,
                    player_name=f"nimi{i}" if player_names else None,
                    clan_name=demo_clan,
                )
                for i in range(players)
            ]
            + (
                [
                    lineup_row(
                        demo,
                        bench_player,
                        lineup=lineup,
                        player_name="penkki" if player_names else None,
                        clan_name=demo_clan,
                    )
                ]
                if bench_player
                else []
            )
            + [
                lineup_row(
                    demo,
                    f"{OPPONENT}-p{i}",
                    lineup=OPPONENT,
                    player_name=f"vastus{i}" if player_names else None,
                    clan_name=OPPONENT_CLAN,
                )
                for i in range(opponent_players)
            ]
        )
        ticks_path = archive.parsed_table(demo, "ticks")
        ticks_path.parent.mkdir(parents=True, exist_ok=True)
        ticks.write_parquet(ticks_path)
        events.write_parquet(archive.parsed_table(demo, "events"))
        lineups.write_parquet(archive.parsed_table(demo, "lineups"))
        deaths.write_parquet(archive.parsed_table(demo, "deaths"))
        rounds_table.write_parquet(archive.parsed_table(demo, "rounds"))
        match_frame(demo, (map_names or {}).get(demo)).write_parquet(
            archive.parsed_table(demo, "match")
        )
        callouts_frame(demo, (callouts or {}).get(demo, ())).write_parquet(
            archive.parsed_table(demo, "callouts")
        )
    return archive


def run(archive: ArchivePaths, team: str | None = TEAM, **kwargs):
    kwargs.setdefault("aggregate_settings", aggregate_settings())
    return aggregate_stage.run(thresholds(), _league(), archive, team, **kwargs)


def _league():
    """The ``[league]`` section from the real settings file, with no archive."""
    from conftest import REAL_SETTINGS

    return load_settings(REAL_SETTINGS, env_files=()).league


def read_report(archive: ArchivePaths, team: str = TEAM) -> Report:
    return Report.model_validate_json(
        archive.report_json(team).read_text(encoding="utf-8")
    )


# --- A basic run ----------------------------------------------------------------


def test_one_demo_produces_a_report_that_validates(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    result = run(archive)

    assert result.stage == "aggregate"
    assert result.unit == TEAM
    assert result.status == "ok"
    assert not result.skipped
    assert [str(p) for p in result.outputs] == [f"aggregates/{TEAM}/report.json"]

    report = read_report(archive)
    assert report.team.key == TEAM
    assert report.sample.demos == 1
    assert [m.map_name for m in report.maps] == ["de_nuke"]


def test_four_demos_of_the_same_team_become_one_report(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path,
        {
            "Ancient_vs_a": TEAM,
            "Anubis_vs_b": TEAM,
            "inferno_vs_c": TEAM,
            "Nuke_vs_d": TEAM,
        },
    )
    run(archive)
    report = read_report(archive)
    assert report.sample.demos == 4
    assert sorted(m.map_name for m in report.maps) == [
        "de_ancient",
        "de_anubis",
        "de_inferno",
        "de_nuke",
    ]
    assert report.sample.rounds == sum(m.sample.rounds for m in report.maps)


def test_the_opponents_rows_are_filtered_out(tmp_path: Path) -> None:
    """The opponent's sample points and grenades do not belong in this report."""
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM}, players=3, opponent_players=5
    )
    run(archive)
    report = read_report(archive)
    position = report.maps[0].sides[0].round_types[0].positions[0]
    assert {a.area for a in position.areas} == {"BombsiteA"}
    types = {
        c.grenade_type
        for rt in report.maps[0].sides[0].round_types
        for c in rt.utility_counts
    }
    assert types == {"smoke"}


def test_lineups_of_the_same_team_are_joined(tmp_path: Path) -> None:
    """One substitution produces a new lineup key; the demo must not vanish."""
    other = "cccccccccccccccc"
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    # A second lineup that shares three players with the first.
    extra = build_archive(tmp_path / "toinen", {"Anubis_vs_b": other})
    for src in (extra.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(extra.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    for src in (extra.root / "parsed").rglob("*.parquet"):
        dst = archive.root / src.relative_to(extra.root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())
    # Write the second demo's sample points so that three players are the same.
    shared = ticks_frame(
        [
            tick_row("Anubis_vs_b", n, f"{TEAM}-p{i}", "BombsiteA", lineup=other)
            for n in range(1, 3)
            for i in range(3)
        ]
        + [
            tick_row("Anubis_vs_b", n, f"{other}-p{i}", "BombsiteA", lineup=other)
            for n in range(1, 3)
            for i in range(3, 5)
        ]
    )
    shared.write_parquet(archive.parsed_table("Anubis_vs_b", "ticks"))
    # Team identity is read from the lineups table (Story 2.6), so the shared
    # players have to be written there -- not into the sample points alone.
    lineups_frame(
        [
            lineup_row(
                "Anubis_vs_b",
                f"{TEAM}-p{i}",
                lineup=other,
                player_name=f"nimi{i}",
            )
            for i in range(3)
        ]
        + [
            lineup_row(
                "Anubis_vs_b",
                f"{other}-p{i}",
                lineup=other,
                player_name=f"nimi{i}",
            )
            for i in range(3, 5)
        ]
    ).write_parquet(archive.parsed_table("Anubis_vs_b", "lineups"))

    run(archive)
    report = read_report(archive)
    assert sorted(report.team.lineup_keys) == sorted([TEAM, other])
    assert report.sample.demos == 2
    assert len(report.maps) == 2


def test_a_demo_without_parsed_tables_is_reported_missing(tmp_path: Path) -> None:
    """A missing demo does not stop the run and does not vanish silently."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    broken = build_archive(
        tmp_path / "rikki", {"Anubis_vs_b": TEAM}, write_parsed=False
    )
    for src in (broken.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(broken.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    result = run(archive)
    assert result.status == "ok"
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    # ``_demo_unusable`` returns the **first** missing table, and the order of
    # the list is part of the contract the user sees. The claim names the table
    # and does not settle for some absence being mentioned: a general claim
    # would pass even if a table disappeared from the list.
    assert "rounds.parquet" in report.missing_demos[0].reason
    assert report.sample.demos == 1


@pytest.mark.parametrize(
    "table", ["rounds", "ticks", "events", "lineups", "deaths", "match"]
)
def test_each_required_parsed_table_is_guarded_on_its_own(
    tmp_path: Path, table: str
) -> None:
    """Each of the six tables is named when it is absent.

    One shared test that removes a single table leaves the rest unguarded:
    ``_demo_unusable`` returns the first absence, so a table near the head of
    the list hides every one after it. Verified by removing ``"ticks"`` from
    the list -- the whole suite passed.
    """
    archive = build_archive(
        tmp_path, {"Ancient_vs_a": TEAM, "Nuke_vs_b": TEAM}
    )
    archive.parsed_table("Nuke_vs_b", table).unlink()

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Nuke_vs_b"]
    assert f"{table}.parquet" in report.missing_demos[0].reason
    assert [m.map_name for m in report.maps] == ["de_ancient"]


def test_no_usable_demo_is_an_error_that_names_the_reason(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, write_parsed=False)
    # The sample point table is still needed to read the lineup, so collecting
    # already fails on it.
    with pytest.raises(PappascoutError, match="could not be read: the lineups"):
        run(archive)


def test_missing_classify_manifest_moves_the_demo_to_missing(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    extra = build_archive(
        tmp_path / "ilman", {"Anubis_vs_b": TEAM}, write_manifest=False
    )
    for sub in ("classified", "parsed"):
        for src in (extra.root / sub).rglob("*"):
            if src.is_file():
                dst = archive.root / src.relative_to(extra.root)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(src.read_bytes())

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    assert "manifest" in report.missing_demos[0].reason.lower()


# --- Choosing the team ----------------------------------------------------------


def test_a_prefix_is_enough_to_name_the_team(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    assert aggregate_stage.resolve_team(archive, TEAM[:6]) == TEAM


def test_missing_team_lists_the_alternatives(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    with pytest.raises(PappascoutError, match="archive's classified teams"):
        run(archive, team=None)


def test_an_unknown_team_is_named_in_the_error(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    with pytest.raises(PappascoutError, match="matches no team at all"):
        run(archive, team="zzzz")


def test_an_empty_archive_says_what_to_run_first(tmp_path: Path) -> None:
    archive = ArchivePaths(root=tmp_path / "tyhja")
    with pytest.raises(PappascoutError, match="classify"):
        run(archive)


def test_team_keys_lists_only_directories_with_results(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    (archive.root / "classified" / "tyhja").mkdir(parents=True)
    assert aggregate_stage.team_keys(archive) == [TEAM]


# --- The manifest and the skip --------------------------------------------------


def test_a_second_run_is_skipped_and_reports_the_same_numbers(
    tmp_path: Path,
) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    first = run(archive)
    second = run(archive)
    assert not first.skipped
    assert second.skipped
    assert second.stats["rounds"] == first.stats["rounds"]
    assert second.stats["maps"] == first.stats["maps"]


def test_force_runs_again_even_when_the_manifest_matches(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    assert not run(archive, force=True).skipped


def test_a_changed_classify_result_invalidates_the_report(tmp_path: Path) -> None:
    """The input's id is from the manifest's content, not from a file hash."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    Manifest.new(
        result_id=f"classified/{TEAM}/Nuke_vs_a",
        stage="classify",
        params_hash="toinen-hash",
        inputs=[ManifestInput(result_id="parsed/Nuke_vs_a", sha256="x")],
        outputs=(f"classified/{TEAM}/Nuke_vs_a.parquet",),
    ).write(archive.classified_manifest(TEAM, "Nuke_vs_a"))
    assert not run(archive).skipped


def test_a_deleted_report_is_written_again(tmp_path: Path) -> None:
    """A synchronised folder: the small manifest syncs before the result."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    archive.report_json(TEAM).unlink()
    assert not run(archive).skipped
    assert archive.report_json(TEAM).is_file()


def test_a_report_from_an_older_schema_is_written_again(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    archive.report_json(TEAM).write_text('{"vanha": true}', encoding="utf-8")
    assert not run(archive).skipped
    assert read_report(archive).sample.demos == 1


def test_the_manifest_records_every_demo_as_an_input(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM}
    )
    run(archive)
    manifest = Manifest.read(archive.report_manifest(TEAM))
    assert manifest.stage == "aggregate"
    assert sorted(i.result_id for i in manifest.inputs) == [
        f"classified/{TEAM}/Anubis_vs_b",
        f"classified/{TEAM}/Nuke_vs_a",
    ]
    assert manifest.tool_versions == {}


def test_thresholds_change_the_params_hash(tmp_path: Path) -> None:
    """Adjusting the thresholds re-runs the aggregation but not the parsing."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(small_sample_rounds=9),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash != before


# --- The write ------------------------------------------------------------------


def test_the_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    assert not has_temp_leftovers(archive.root)


def test_the_report_is_valid_utf8_json(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    data = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    # A literal and not the constant: comparing against the constant would be
    # a tautology -- the code wrote the value from that very constant. When the
    # version rises, this line MUST fail, so that the rise is deliberate.
    assert data["schema_version"] == "9.0.0"
    assert data["team"]["roster_source"] == "lineups"


def test_a_corrupt_classified_table_is_named(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    pl.DataFrame({"vaara": [1]}).write_parquet(
        archive.classified(TEAM, "Nuke_vs_a")
    )
    with pytest.raises(SchemaError, match="classify"):
        run(archive)


# --- The review's findings ------------------------------------------------------


def test_a_report_that_no_longer_passes_the_sample_check_is_written_again(
    tmp_path: Path,
) -> None:
    """A sum error is an AggregateError and not a ValueError.

    An old ``report.json`` that no longer passes the sum validators must not
    break the skip branch for ever -- the stage writes a new one in its place.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    broken = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    broken["sample"]["rounds"] = 999
    broken["sample"]["unknown"]["rounds"] = 999
    archive.report_json(TEAM).write_text(
        json.dumps(broken, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(AggregateError):
        Report.model_validate(broken)

    result = run(archive)
    assert not result.skipped
    assert read_report(archive).sample.rounds == 2


def test_a_report_from_a_foreign_schema_version_is_written_again(
    tmp_path: Path,
) -> None:
    """The schema version has to be compared, as ``Manifest`` does.

    An old file can validate field by field and still mean something else;
    without the comparison the skip would return its figures as this run's
    result.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    stale = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    stale["schema_version"] = "0.1.0"
    stale["unclassified_rounds"] = 999
    archive.report_json(TEAM).write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )

    result = run(archive)
    assert not result.skipped
    assert result.stats["unclassified"] == 0
    assert read_report(archive).schema_version == "9.0.0"


def test_the_real_stats_render_without_a_key_error(tmp_path: Path) -> None:
    """No other test watches over the contract between the stage and the output.

    Every CLI test builds the stats by hand and every stage test compares the
    producer against itself, so renaming a key would pass with a green suite
    and fail only on a real run.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM})
    text = _render_aggregate(run(archive))
    assert "Sample" in text
    assert "de_nuke" in text and "de_anubis" in text
    assert _render_aggregate(run(archive)).startswith("Skipped:")


def test_the_summary_reports_the_roster_size(tmp_path: Path) -> None:
    """The roster line was in the output without a test."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, players=5)
    assert "5 players observed" in _render_aggregate(run(archive))


def test_a_lineup_that_cannot_be_read_at_all_is_still_reported(
    tmp_path: Path,
) -> None:
    """A lineup that cannot be joined must not vanish without trace."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    orphan = build_archive(
        tmp_path / "orpo", {"Anubis_vs_b": "cccccccccccccccc"}, write_parsed=False
    )
    for src in (orphan.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(orphan.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    assert "not known whether the demo belongs to this team" in (
        report.missing_demos[0].reason
    )


def test_many_teams_in_the_archive_do_not_confuse_the_identity(
    tmp_path: Path,
) -> None:
    """Identity is settled against every lineup.

    The archive holds four teams, of which no other shares players with the
    subject -- so the join must not drag them along.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    for index, name in enumerate(("Anubis_vs_b", "inferno_vs_c", "Ancient_vs_d")):
        other = chr(ord("c") + index) * 16
        extra = build_archive(tmp_path / ("muu" + str(index)), {name: other})
        for sub in ("classified", "parsed"):
            for src in (extra.root / sub).rglob("*"):
                if src.is_file():
                    dst = archive.root / src.relative_to(extra.root)
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.write_bytes(src.read_bytes())

    assert len(aggregate_stage.team_keys(archive)) == 4
    run(archive)
    report = read_report(archive)
    assert report.team.lineup_keys == [TEAM]
    assert report.sample.demos == 1
    assert report.missing_demos == []


def test_the_report_names_the_thresholds_the_rounds_were_classified_with(
    tmp_path: Path,
) -> None:
    """The classification's thresholds are read from the table, not the settings."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    report = read_report(archive)
    assert report.classify_thresholds["full_equip_min"] == 4000
    assert report.thresholds_used["aggregate"]["utility_seconds_buckets"] == [
        5.0,
        10.0,
        20.0,
    ]


def test_an_unrelated_threshold_does_not_invalidate_the_report(
    tmp_path: Path,
) -> None:
    """A parameter hash only of what changes this stage's result."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(full_equip_min=4500),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash == before


def test_the_time_windows_do_change_the_params_hash(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(utility_seconds_buckets=[7.0]),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash != before


def test_every_setting_the_stage_reads_is_in_the_params_hash() -> None:
    """A named list must not go stale silently.

    Which ``thresholds.`` and ``league.`` fields the stage and its domain
    functions read is taken from the source and compared against the hashed
    list. If somebody adds a new field that is read but not to the hash, the
    report would stay stale without anything saying so.
    """
    source = "".join(
        (SRC / name).read_text(encoding="utf-8")
        for name in ("stages/aggregate.py", "domain/aggregate.py")
    )
    ignore = {"model_dump"}
    read = set(re.findall(THRESHOLD_READ, source)) - ignore
    assert read <= set(aggregate_stage.HASHED_THRESHOLD_KEYS), read
    league_read = set(re.findall(LEAGUE_READ, source)) - ignore
    assert league_read <= set(aggregate_stage.HASHED_LEAGUE_KEYS), league_read


def test_the_manifest_fingerprint_ignores_only_the_timestamp() -> None:
    """The contract "the moment of creation is left out" was only tested indirectly."""

    def make(params_hash: str = "h") -> Manifest:
        return Manifest.new(
            result_id="classified/x/y",
            stage="classify",
            params_hash=params_hash,
            inputs=[ManifestInput(result_id="parsed/y", sha256="s")],
            outputs=("classified/x/y.parquet",),
        )

    first = make()
    sleep(0.002)
    second = make()
    assert first.created_at != second.created_at
    assert first.fingerprint() == second.fingerprint()
    assert make("toinen").fingerprint() != first.fingerprint()


def test_a_table_written_with_a_different_column_order_still_reads(
    tmp_path: Path,
) -> None:
    """Column order is not part of the contract, but ``pl.concat`` cares.

    ``validate`` accepts any order -- the contract is about names and types --
    so an archive written with two different versions can hold the same table
    in a different order. Without the ordering ``pl.concat`` would fail with a
    raw ``ShapeError`` that tells the user nothing.
    """
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM}
    )
    for demo in ("Anubis_vs_b",):
        for table in ("ticks", "events"):
            path = archive.parsed_table(demo, table)
            df = pl.read_parquet(path)
            df.select(reversed(df.columns)).write_parquet(path)
        path = archive.classified(TEAM, demo)
        df = pl.read_parquet(path)
        df.select(reversed(df.columns)).write_parquet(path)

    run(archive)
    assert read_report(archive).sample.demos == 2


# --- The sample's three buckets (Story 3.8) -------------------------------------
#
# ``classify`` fills the ``is_league`` column from the selection file, so the
# bucketing finally gets a value. These tests measure that the value goes **the
# whole way**: from the table into a bucket, and from the bucket into the
# report's total.


def test_a_league_demo_lands_in_the_league_bucket(tmp_path: Path) -> None:
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM}, is_league={"Nuke_vs_a": True}
    )
    run(archive)

    sample = read_report(archive).sample
    assert (sample.league.demos, sample.league.rounds) == (1, sample.rounds)
    assert sample.other.demos == 0
    assert sample.unknown.demos == 0


def test_league_and_other_demos_fill_their_own_buckets(tmp_path: Path) -> None:
    """Three demos, three buckets: the whole sample is no longer ``unknown``.

    Before Story 3.8 this very line read *"liiga 0 / 0, muut 0 / 0, tuntematon
    4 / 85"*. The claim is per bucket **and** a total: every demo belongs to
    exactly one bucket, so the total has to be the sum of the buckets.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM, "Anubis_vs_c": TEAM},
        rounds=2,
        is_league={"Nuke_vs_a": True, "Ancient_vs_b": False},
    )
    run(archive)

    sample = read_report(archive).sample
    assert sample.demos == 3
    assert sample.league.demos == 1
    assert sample.other.demos == 1
    assert sample.unknown.demos == 1, "a hand-imported demo stays unknown"
    assert sample.rounds == (
        sample.league.rounds + sample.other.rounds + sample.unknown.rounds
    )
    assert sample.league.rounds == 2
    assert sample.other.rounds == 2


def test_the_bucket_row_names_the_counts_in_the_summary(tmp_path: Path) -> None:
    """The command's summary says the buckets, not only the sample's size."""
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM},
        is_league={"Nuke_vs_a": True, "Ancient_vs_b": False},
    )
    result = run(archive)

    text = _render_aggregate(result)
    # The bucket names stay Finnish (``SAMPLE_BUCKET_FI``, AD-11); the
    # surrounding console text was translated in T15.
    assert "liiga 1 demos / 2 rounds" in text
    assert "muut 1 demos / 2 rounds" in text
    assert "tuntematon 0 demos / 0 rounds" in text


# --- Roster class split (Story 3.9) ---------------------------------------------
#
# Story 3.8 carried ``roster_class`` all the way into the ``CLASSIFIED`` table,
# but nothing read it. These tests measure that the value now goes **the whole
# way**: from the table into a bucket, and from the bucket into the report.


def test_both_roster_classes_land_in_their_own_buckets(tmp_path: Path) -> None:
    """AC: one ``5/5`` and one ``4/5`` -- the report reports both."""
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM},
        rounds=2,
        roster_class={"Nuke_vs_a": "5/5", "Ancient_vs_b": "4/5"},
    )
    run(archive)

    roster = read_report(archive).roster_sample
    assert (roster.full.demos, roster.full.rounds) == (1, 2)
    assert (roster.partial.demos, roster.partial.rounds) == (1, 2)
    assert roster.unknown.demos == 0


def test_the_two_breakdowns_of_the_summary_agree_on_the_totals(
    tmp_path: Path,
) -> None:
    """Equal totals, different buckets.

    The two breakdowns describe the same demos along two dimensions: a league
    match can be played with a stand-in. The model would reject a report whose
    totals differed, so this test proves ``aggregate`` builds one that passes.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM, "Anubis_vs_c": TEAM},
        rounds=2,
        is_league={"Nuke_vs_a": True, "Ancient_vs_b": False},
        roster_class={"Nuke_vs_a": "4/5", "Ancient_vs_b": "5/5"},
    )
    run(archive)

    entry = read_report(archive)
    assert (entry.sample.demos, entry.sample.rounds) == (
        entry.roster_sample.demos,
        entry.roster_sample.rounds,
    )
    assert entry.sample.league.demos == 1
    assert entry.roster_sample.partial.demos == 1
    assert entry.roster_sample.unknown.demos == 1


def test_an_unclassified_roster_leaves_the_whole_sample_unknown(
    tmp_path: Path,
) -> None:
    """The archive's state for as long as ``select`` has not been run over it.

    A condition rather than a date: a date in a comment is a fact about a
    moment that nobody comes back to update -- which is exactly what this
    story had to fix at ``aggregate.py:166``.
    """
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM}, rounds=2
    )
    run(archive)

    roster = read_report(archive).roster_sample
    assert (roster.unknown.demos, roster.unknown.rounds) == (2, 4)
    assert roster.full.demos == 0 and roster.partial.demos == 0


def test_a_demo_whose_rounds_disagree_on_the_roster_class_stops_the_run(
    tmp_path: Path,
) -> None:
    """AC: a hand-edited table stops the run and names the demo.

    Averaging the rounds would be worse than stopping: the demo would belong
    to two buckets, and the sample total would stop being the sum of the
    buckets -- the very check the whole structure rests on.
    """
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM}, rounds=2, roster_class={"Nuke_vs_a": "5/5"}
    )
    path = archive.classified(TEAM, "Nuke_vs_a")
    df = pl.read_parquet(path)
    df.with_columns(
        roster_class=pl.when(pl.col("round_no") == 1)
        .then(pl.lit("5/5"))
        .otherwise(pl.lit("4/5"))
        .cast(df.schema["roster_class"])
    ).write_parquet(path)

    with pytest.raises(PappascoutError, match="two roster buckets") as err:
        run(archive)
    assert "Nuke_vs_a" in str(err.value)


def test_the_raw_json_keys_of_the_roster_breakdown_are_locked(
    tmp_path: Path,
) -> None:
    """The names in the file, read **without** the model (Story 3.9).

    Every other test reads ``report.json`` through ``Report``, which resolves
    aliases -- so a renamed field or an added alias would break nothing here
    while breaking every reader outside this repo. ``report.json`` is the
    contract ``aggregate`` and ``render`` share, and the literals are the
    only place that contract is written down as text.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM},
        rounds=2,
        roster_class={"Nuke_vs_a": "5/5", "Ancient_vs_b": "4/5"},
    )
    run(archive)

    data = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    roster = data["roster_sample"]
    assert set(roster) == {"demos", "rounds", "full", "partial", "unknown"}
    assert roster["full"] == {"demos": 1, "rounds": 2}
    assert roster["partial"] == {"demos": 1, "rounds": 2}
    assert roster["unknown"] == {"demos": 0, "rounds": 0}
    assert (roster["demos"], roster["rounds"]) == (2, 4)
    # Siblings, not nested: the roster breakdown is a second breakdown of the
    # summary sample, not a field inside the league one.
    assert "roster_sample" not in data["sample"]


def test_a_report_without_the_roster_breakdown_is_written_again(
    tmp_path: Path,
) -> None:
    """An old ``report.json`` does not read as current -- it is rebuilt.

    The field is required, so a file written during Story 3.8 fails to
    validate. The skip branch must treat that like a wrong version number:
    compute the numbers again rather than return the old file's numbers as
    this run's result.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    stale = json.loads(archive.report_json(TEAM).read_text(encoding="utf-8"))
    del stale["roster_sample"]
    stale["unclassified_rounds"] = 999
    archive.report_json(TEAM).write_text(
        json.dumps(stale, ensure_ascii=False), encoding="utf-8"
    )

    result = run(archive)
    assert not result.skipped
    assert result.stats["unclassified"] == 0
    assert read_report(archive).roster_sample.demos == 1


# --- Regressions on real demos --------------------------------------------------


@pytest.mark.demo
def test_the_league_demos_aggregate_into_one_team(tmp_path: Path) -> None:
    """Four MatureMayhem demos, one report, the sample ``unknown``.

    The test runs the whole pipeline (parse -> classify -> aggregate) into a
    temporary archive, so it does not rest on whatever happens to be in the
    developer's own archive. It is also the only place where joining lineups is
    verified on real material: across these four demos MatureMayhem is under
    two different lineup keys.
    """
    from pappascout.stages import classify as classify_stage
    from pappascout.stages import parse as parse_stage

    demos = [require_demo(name) for name, _ in LEAGUE_DEMOS]
    settings = load_settings(_real_settings_at(tmp_path), env_files=())
    archive = ArchivePaths.from_settings(settings.project.archive_root)

    members: dict[str, dict[str, set[str]]] = {}
    for demo in demos:
        map_demo_id = demo.stem
        parse_stage.run(
            settings.parse,
            archive,
            map_demo_id,
            parse_stage.default_parser(settings.parse),
            demo_path=demo,
        )
        members[map_demo_id] = _lineup_members(archive, map_demo_id)

    subject = _common_lineups(members, settings.thresholds.team_identity_min_common)
    assert len(subject) == len(demos), (
        "Every demo should hold the same team; found " f"{subject}"
    )

    for map_demo_id, lineup in subject.items():
        classify_stage.run(
            settings.thresholds,
            settings.league,
            archive,
            map_demo_id,
            lineup,
            economy=settings.economy,
        )

    result = aggregate_stage.run(
        settings.thresholds,
        settings.league,
        archive,
        next(iter(subject.values())),
        aggregate_settings=settings.aggregate,
    )
    report = Report.model_validate_json(
        archive.report_json(result.unit).read_text(encoding="utf-8")
    )
    assert report.sample.demos == 4
    assert report.sample.unknown.demos == 4
    assert report.sample.league.demos == 0
    assert report.sample.other.demos == 0
    assert {m.map_name for m in report.maps} == {
        "de_ancient",
        "de_anubis",
        "de_inferno",
        "de_nuke",
    }
    for entry in report.maps:
        for side in entry.sides:
            for round_type in side.round_types:
                for position in round_type.positions:
                    for area in position.areas:
                        assert sum(p.n for p in area.players_dist) == area.m


def _real_settings_at(tmp_path: Path) -> Path:
    from conftest import settings_text

    target = tmp_path / "settings.toml"
    target.write_text(settings_text(tmp_path / "arkisto"), encoding="utf-8")
    return target


def _lineup_members(
    archive: ArchivePaths, map_demo_id: str
) -> dict[str, set[str]]:
    df = pl.read_parquet(
        archive.parsed_table(map_demo_id, "ticks"),
        columns=["lineup_key", "player_id"],
    ).unique()
    found: dict[str, set[str]] = {}
    for row in df.iter_rows(named=True):
        found.setdefault(row["lineup_key"], set()).add(row["player_id"])
    return found


def _common_lineups(
    members: dict[str, dict[str, set[str]]], min_common: int
) -> dict[str, str]:
    """Demo -> the lineup that appears in every demo.

    A lineup key changes if the team substitutes a player, so the comparison
    has to be made with sets of players and not with keys.
    """
    first = next(iter(members))
    for candidate, players in members[first].items():
        picked = {first: candidate}
        for demo, lineups in members.items():
            if demo == first:
                continue
            match = [
                key
                for key, others in lineups.items()
                if len(players & others) >= min_common
            ]
            if len(match) == 1:
                picked[demo] = match[0]
        if len(picked) == len(members):
            return picked
    return {}


# --- The team's and the players' names (Story 2.6) ------------------------------


def test_the_team_name_comes_from_the_lineups_table(tmp_path: Path) -> None:
    """The name is an observation from the demo, not derived from the id."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)

    report = read_report(archive)
    assert report.team.display_name == TEAM_CLAN
    assert report.team.display_name_source == "clan_name"
    assert report.team.display_name_alternatives == []
    # The file name's slug follows the name, so that the report's name says
    # the team and not a hash.
    assert report.team.slug == "maturemayhem"
    # The key does not change: it is the directory structure.
    assert report.team.key == TEAM


def test_the_opponents_clan_name_does_not_become_the_title(tmp_path: Path) -> None:
    """The same demo holds both teams' rows."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    report = read_report(archive)
    assert OPPONENT_CLAN not in [
        report.team.display_name,
        *report.team.display_name_alternatives,
    ]


def test_a_team_without_a_clan_name_keeps_the_key_and_says_so(tmp_path: Path) -> None:
    """Without an observation the name is the id, and the source says so."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, clan=None)
    run(archive)

    report = read_report(archive)
    assert report.team.display_name == TEAM
    assert report.team.display_name_source == "team_key"
    assert report.team.slug == TEAM


def test_a_clan_name_without_ascii_still_names_its_own_file(tmp_path: Path) -> None:
    """A Cyrillic clan: the name into the title, the slug from the id.

    Not one character is left of the slug, but the name was observed all the
    same. If the fallback were a shared constant, every such team would get the
    file name ``<timestamp>-joukkue.md`` -- so the reports would collide with
    each other and the name would disappear from the file name entirely.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, clan="Кибер")
    run(archive)

    team = read_report(archive).team
    assert team.display_name == "Кибер"
    assert team.display_name_source == "clan_name"
    assert team.slug == TEAM
    assert team.slug != "joukkue"


def test_conflicting_clan_names_are_resolved_and_the_rest_listed(
    tmp_path: Path,
) -> None:
    """Three demos with one name, one with another: the majority wins."""
    archive = build_archive(
        tmp_path,
        {
            "Ancient_vs_a": TEAM,
            "Anubis_vs_b": TEAM,
            "inferno_vs_c": TEAM,
            "Nuke_vs_d": TEAM,
        },
        clan_by_demo={"Nuke_vs_d": "MM Academy"},
    )
    run(archive)

    report = read_report(archive)
    assert report.team.display_name == TEAM_CLAN
    assert report.team.display_name_alternatives == ["MM Academy"]


def test_the_roster_carries_both_the_name_and_the_steamid(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)

    roster = read_report(archive).team.roster
    assert len(roster) == 5
    assert all(entry.player_id.startswith(f"{TEAM}-p") for entry in roster)
    assert sorted(e.display_name for e in roster) == [f"nimi{i}" for i in range(5)]


def test_a_player_without_a_name_is_still_in_the_roster(tmp_path: Path) -> None:
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, player_names=False)
    run(archive)

    roster = read_report(archive).team.roster
    assert len(roster) == 5
    assert all(entry.display_name is None for entry in roster)


def test_a_demo_without_the_lineups_table_is_reported_missing(
    tmp_path: Path,
) -> None:
    """A missing lineups table does not stop the run but does not vanish either."""
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM, "Ancient_vs_b": TEAM}
    )
    archive.parsed_table("Ancient_vs_b", "lineups").unlink()
    run(archive)

    report = read_report(archive)
    reasons = {m.match: m.reason for m in report.missing_demos}
    assert "Ancient_vs_b" in reasons
    assert "lineups.parquet" in reasons["Ancient_vs_b"]
    assert report.sample.demos == 1


def test_a_player_seen_only_in_the_lineups_table_is_in_the_roster(
    tmp_path: Path,
) -> None:
    """Lineups are read from the ``lineups`` table and not from ``ticks``.

    The sample point table is missing a player who did not make it to a single
    sample point -- and ``lineup_key`` was computed with him in all the same.
    If the lineups were read from the sample points, the key and its set of
    players would disagree, and the roster would be missing a player who played
    the map.
    """
    bench = f"{TEAM}-penkki"
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM}, bench_player=bench)
    run(archive)

    roster = read_report(archive).team.roster
    ids = [entry.player_id for entry in roster]
    assert bench in ids
    assert len(roster) == 6

    # And he really is absent from the sample point table -- otherwise the
    # test would prove nothing about the change of source.
    ticks = pl.read_parquet(archive.parsed_table("Nuke_vs_a", "ticks"))
    assert bench not in set(ticks["player_id"])


def test_the_lineup_join_can_rest_on_a_player_who_has_no_sample_points(
    tmp_path: Path,
) -> None:
    """The join between teams is made with the lineups table's set of players.

    Three players in common are enough for the join. Here two of them are
    players who are not in the sample point table at all, so the old source
    would not find enough players in common and would not join the lineups.
    """
    other = "cccccccccccccccc"
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    extra = build_archive(tmp_path / "toinen", {"Anubis_vs_b": other})
    for src in (extra.root / "classified").rglob("*"):
        if src.is_file():
            dst = archive.root / src.relative_to(extra.root)
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
    for src in (extra.root / "parsed").rglob("*.parquet"):
        dst = archive.root / src.relative_to(extra.root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())

    # In the sample points the second lineup has ONLY its own players: not one
    # of the shared ones shows. The three shared players are in the lineups
    # table alone.
    ticks_frame(
        [
            tick_row("Anubis_vs_b", n, f"{other}-p{i}", "BombsiteA", lineup=other)
            for n in range(1, 3)
            for i in range(3, 5)
        ]
    ).write_parquet(archive.parsed_table("Anubis_vs_b", "ticks"))
    lineups_frame(
        [
            lineup_row("Anubis_vs_b", f"{TEAM}-p{i}", lineup=other)
            for i in range(3)
        ]
        + [
            lineup_row("Anubis_vs_b", f"{other}-p{i}", lineup=other)
            for i in range(3, 5)
        ]
    ).write_parquet(archive.parsed_table("Anubis_vs_b", "lineups"))

    run(archive)
    report = read_report(archive)
    assert sorted(report.team.lineup_keys) == sorted([TEAM, other])


# --- The deaths table (Story 2.7) ----------------------------------------------


def test_own_deaths_and_own_kills_both_reach_the_report(tmp_path: Path) -> None:
    """The filter is over two columns, and both halves have to survive.

    ``victim_lineup_key`` alone would drop our own kills and
    ``attacker_lineup_key`` alone our own deaths -- and either mistake would
    look in the report as though the team had not done what it did.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    run(archive)
    entry = read_report(archive).maps[0].sides[0].round_types[0]

    assert entry.deaths.m == 1
    assert [(a.area, a.n) for a in entry.deaths.first_death_areas] == [("Cave", 1)]
    assert entry.deaths.kills_total == 1
    assert [(k.area, k.n) for k in entry.deaths.kills] == [("Middle", 1)]


def test_the_opponents_own_deaths_do_not_become_ours(tmp_path: Path) -> None:
    """A death between two opponents does not belong in this report."""
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    extra = deaths_frame(
        [
            death_row(
                "Ancient_vs_a",
                1,
                victim=f"{OPPONENT}-p3",
                victim_lineup=OPPONENT,
                victim_side="CT",
                attacker=f"{OPPONENT}-p4",
                attacker_lineup=OPPONENT,
                attacker_side="CT",
                attacker_area="Ramp",
            )
        ]
    )
    path = archive.parsed_table("Ancient_vs_a", "deaths")
    pl.concat([pl.read_parquet(path), extra]).write_parquet(path)

    run(archive, force=True)
    entry = read_report(archive).maps[0].sides[0].round_types[0]
    assert entry.deaths.m == 1
    assert entry.deaths.kills_total == 1
    assert "Ramp" not in [k.area for k in entry.deaths.kills]


def test_a_demo_without_the_deaths_table_is_reported_missing(
    tmp_path: Path,
) -> None:
    """A missing table does not stop the run but does not vanish silently."""
    archive = build_archive(
        tmp_path, {"Ancient_vs_a": TEAM, "Nuke_vs_b": TEAM}
    )
    archive.parsed_table("Nuke_vs_b", "deaths").unlink()

    run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Nuke_vs_b"]
    assert "deaths.parquet" in report.missing_demos[0].reason
    assert [m.map_name for m in report.maps] == ["de_ancient"]


def test_an_outdated_deaths_table_tells_the_user_to_reparse(
    tmp_path: Path,
) -> None:
    """A table written with an old version does not pass silently."""
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    path = archive.parsed_table("Ancient_vs_a", "deaths")
    pl.read_parquet(path).drop("attacker_area").write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run(archive)
    assert "attacker_area" in str(exc.value)
    assert "uv run pappascout parse Ancient_vs_a --force" in str(exc.value)


def test_an_attackerless_own_death_survives_the_lineup_filter(
    tmp_path: Path,
) -> None:
    """Our own player killed by the bomb must not drop out in the filter.

    ``attacker_lineup_key`` is then ``null``, and in Polars ``is_in`` gives
    null for null. Without ``fill_null(False)`` the condition would rest on
    ``true | null`` being true -- right today, but a silent dependency on a
    detail of three-valued logic.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    only_bomb = deaths_frame(
        [
            death_row(
                "Ancient_vs_a",
                1,
                victim=f"{TEAM}-p0",
                victim_lineup=TEAM,
                victim_area="BombsiteB",
                attacker=None,
                t_s=95.0,
            )
        ]
    )
    only_bomb.write_parquet(archive.parsed_table("Ancient_vs_a", "deaths"))

    run(archive, force=True)
    entry = read_report(archive).maps[0].sides[0].round_types[0]
    assert entry.deaths.m == 1
    assert [(a.area, a.n) for a in entry.deaths.first_death_areas] == [
        ("BombsiteB", 1)
    ]
    assert entry.deaths.kills_total == 0


def test_a_deaths_table_that_names_no_known_lineup_is_refused(
    tmp_path: Path,
) -> None:
    """A deaths frame filtered empty is the state parse forbids.

    Without the guard every round type would report "no own deaths" -- that
    is, as an observation that there is no observation.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    strangers = deaths_frame(
        [
            death_row(
                "Ancient_vs_a",
                1,
                victim="x1",
                victim_lineup="tuntematonkokoonp",
                attacker="x2",
                attacker_lineup="tuntematonkokoonp",
            )
        ]
    )
    strangers.write_parquet(archive.parsed_table("Ancient_vs_a", "deaths"))

    with pytest.raises(PappascoutError) as exc:
        run(archive, force=True)
    assert "Not one death was found" in str(exc.value)
    assert "--force" in str(exc.value)


# --- The rounds table and the armour counter (Story 2.8) ------------------------


def test_an_outdated_rounds_table_tells_the_user_to_reparse(
    tmp_path: Path,
) -> None:
    """An old rounds table without the armour column does not pass silently.

    The I/O matrix's row "an old archive" from the aggregation's side: the
    schema error names both the missing column and the command.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    path = archive.parsed_table("Ancient_vs_a", "rounds")
    pl.read_parquet(path).drop(ARMORED_COLUMN).write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run(archive)
    assert ARMORED_COLUMN in str(exc.value)
    assert "uv run pappascout parse Ancient_vs_a --force" in str(exc.value)


def test_an_extra_column_in_the_rounds_table_is_refused_too(
    tmp_path: Path,
) -> None:
    """The contract is strict in both directions, on the rounds table too.

    An extra column means a table some other version wrote; a check for missing
    columns alone would let it through.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    path = archive.parsed_table("Ancient_vs_a", "rounds")
    df = pl.read_parquet(path)
    df.with_columns(pl.lit(1).alias("ylimaarainen")).write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run(archive)
    assert "ylimaarainen" in str(exc.value)


def test_the_armored_count_reaches_the_report_from_the_rounds_table(
    tmp_path: Path,
) -> None:
    """The connection from disk to the report: the counter is not in the classified table.

    ``build_archive`` writes five kevlars for our own team and zero for the
    opponent. Without this test the read of the ``rounds`` table could be
    missing from the stage entirely and the distribution would be silently
    empty.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})

    run(archive)
    entry = read_report(archive).maps[0].sides[0].round_types[0]
    assert [(c.armored, c.n) for c in entry.players_armored.counts] == [(5, 1)]
    assert entry.players_armored.rounds_unknown == 0


def test_only_our_own_rounds_row_reaches_the_distribution(tmp_path: Path) -> None:
    """The rounds table has two rows per round -- only our own reaches the distribution.

    The opponent has zero kevlars on the same round. Two obstacles together:
    the lineup filter drops his row before the lookup map, and the key's third
    part (the side) would pick our own row even if the filter were absent. The
    test verifies the outcome both of them guarantee.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})

    run(archive)
    for map_report in read_report(archive).maps:
        for side_report in map_report.sides:
            for entry in side_report.round_types:
                assert [c.armored for c in entry.players_armored.counts] == [5]


def test_rounds_that_name_no_known_lineup_are_refused(tmp_path: Path) -> None:
    """A rounds table filtered empty is an error, not a silent absence.

    The same guard as on the deaths table and for the same reason: without it
    every round type would report its armour distribution as nothing but "the
    observation is missing" -- that is, as an observation that there is no
    observation. Avoiding precisely that outcome is why this column exists.
    """
    archive = build_archive(tmp_path, {"Ancient_vs_a": TEAM})
    strangers = rounds_frame(
        [
            round_row("Ancient_vs_a", n, lineup="tuntematonkokoonp", side="T")
            for n in (1, 2)
        ]
        + [
            round_row("Ancient_vs_a", n, lineup=OPPONENT, side="CT")
            for n in (1, 2)
        ]
    )
    strangers.write_parquet(archive.parsed_table("Ancient_vs_a", "rounds"))

    with pytest.raises(PappascoutError) as exc:
        run(archive, force=True)
    assert "Not one round row was found" in str(exc.value)
    assert "--force" in str(exc.value)


# --- The map's name from the header (Story 2.11) ------------------------------


def test_the_map_name_comes_from_the_match_table(tmp_path: Path) -> None:
    """The observation beats the inference through the stage as well.

    The id says ``Nuke``, the header says ``de_ancient``. The report holds the
    header's name and the source ``demo_header``: without the connection from
    the table to the report this test would look exactly as it did before the
    change.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM},
        map_names={"Nuke_vs_a": "de_ancient"},
    )

    run(archive)
    report = read_report(archive)

    assert [(m.map_name, m.map_name_source) for m in report.maps] == [
        ("de_ancient", "demo_header")
    ]


def test_two_faceit_demos_of_the_same_map_are_one_branch(tmp_path: Path) -> None:
    """The RCAVE case: two ids, one map, one branch.

    Neither id holds the map's name, so without the header these would be two
    branches -- and every row would carry the note "(1/1 kierroksesta)".
    """
    archive = build_archive(
        tmp_path,
        {"1-a52ebff2-1-1": TEAM, "1-79f71e00-1-1": TEAM},
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "1-79f71e00-1-1": "de_ancient",
        },
    )

    run(archive)
    report = read_report(archive)

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert sorted(entry.map_demo_ids) == ["1-79f71e00-1-1", "1-a52ebff2-1-1"]
    assert entry.sample.demos == 2


def test_a_demo_without_a_header_name_falls_back_to_the_identifier(
    tmp_path: Path,
) -> None:
    """A nameless header does not stop the run: inference from the pool stands."""
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "1-a52ebff2-1-1": TEAM},
        map_names={"Nuke_vs_a": None, "1-a52ebff2-1-1": None},
    )

    result = run(archive)
    assert result.status == "ok"

    report = read_report(archive)
    branches = {m.map_name: m.map_name_source for m in report.maps}
    assert branches == {"de_nuke": "map_demo_id", "1-a52ebff2-1-1": "unknown"}


def test_a_match_table_written_with_a_different_column_order_still_reads(
    tmp_path: Path,
) -> None:
    """Column order is not part of the contract, but ``pl.concat`` cares."""
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM},
        map_names={"Nuke_vs_a": "de_nuke", "Anubis_vs_b": "de_anubis"},
    )
    path = archive.parsed_table("Anubis_vs_b", "match")
    df = pl.read_parquet(path)
    df.select(reversed(df.columns)).write_parquet(path)

    run(archive)
    report = read_report(archive)
    assert sorted(m.map_name for m in report.maps) == ["de_anubis", "de_nuke"]


def test_an_observed_and_an_inferred_demo_of_one_map_are_one_branch(
    tmp_path: Path,
) -> None:
    """The same map from two different sources is one branch, through the stage too.

    ``Ancient_vs_a``'s header holds the map, ``Ancient_vs_b``'s does not. Both
    are named ``de_ancient``, so they are one branch -- and its source is the
    weaker one, ``map_demo_id``, because one member was inferred.
    """
    archive = build_archive(
        tmp_path,
        {"Ancient_vs_a": TEAM, "Ancient_vs_b": TEAM},
        map_names={"Ancient_vs_a": "de_ancient", "Ancient_vs_b": None},
    )

    run(archive)
    report = read_report(archive)

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert entry.map_name_source == "map_demo_id"
    assert entry.sample.demos == 2
    assert sorted(entry.map_demo_ids) == ["Ancient_vs_a", "Ancient_vs_b"]


def test_the_map_name_follows_the_demo_the_table_was_read_for(
    tmp_path: Path,
) -> None:
    """The name is joined to the **demo that was read**, not to the table's own column.

    A stale ``match.parquet``, or one that ended up in the wrong directory,
    carries a wrong ``map_demo_id`` value; schema validation does not see it,
    because the column's type is right. If the map were built from the column,
    the name would be recorded against the wrong demo and the right demo would
    fall back silently to inference -- and two identical ids would drop one of
    them entirely.

    Here the table of both demos claims to belong to the same third demo. With
    the loop's key the names still end up on the right demos.
    """
    archive = build_archive(
        tmp_path,
        {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM},
        map_names={"Nuke_vs_a": "de_nuke", "Anubis_vs_b": "de_anubis"},
    )
    for demo in ("Nuke_vs_a", "Anubis_vs_b"):
        path = archive.parsed_table(demo, "match")
        pl.read_parquet(path).with_columns(
            pl.lit("Vieras_vs_x").alias("map_demo_id")
        ).write_parquet(path)

    run(archive)
    report = read_report(archive)

    assert sorted(m.map_name for m in report.maps) == ["de_anubis", "de_nuke"]
    assert all(m.map_name_source == "demo_header" for m in report.maps)


@pytest.mark.parametrize("rows", [0, 2])
def test_a_match_table_without_exactly_one_row_is_refused(
    tmp_path: Path, rows: int
) -> None:
    """The row count is checked on reading too, not only on writing.

    The contract is enforced by the stage that writes -- but the file that was
    read may have been written by an older version of the program, and a reader
    must not rest on the writer having been this version. Zero rows would look
    the same as the observation ``null``, and out of two rows the name would be
    picked by row order.
    """
    archive = build_archive(
        tmp_path, {"Nuke_vs_a": TEAM}, map_names={"Nuke_vs_a": "de_nuke"}
    )
    path = archive.parsed_table("Nuke_vs_a", "match")
    df = pl.read_parquet(path)
    pl.concat([df] * rows if rows else [df.head(0)]).write_parquet(path)

    with pytest.raises(PappascoutError, match="match table of demo"):
        run(archive)


# --- The anomalies' orientation (Story 2.5) -------------------------------------
#
# The stage's own part of the anomaly rules is the **areas' side orientation**,
# and it is computed before the lineup filter. The tests prove it from two
# directions: what the orientation counts, and what disappears if the filter is
# moved ahead of it.

#: An area the opponent holds throughout the demo -- Nuke's lobby.
LOBBY = "Lobby"


def lobby_ticks(demo: str) -> list[dict[str, object]]:
    """Sample points on which the subject pushes into the opponent's area.

    The opponent (T) is in ``Lobby`` at every sample point on every round: 45
    observations. The subject's CT players are on their own side, and **only on
    round 1** two of them move there from two different source directions -- 2
    observations.

    The figures are chosen so that the same area is on **different sides** of
    the threshold depending on which table the orientation is computed from:
    from the whole table the T share is 45/47 = 0.96, but from the subject's
    rows 0/2. That difference is exactly what the calibration measured on real
    demos.
    """
    rows: list[dict[str, object]] = []
    for round_no in (1, 2, 3):
        for seconds in (6.0, 15.0, 30.0):
            rows += [
                tick_row(
                    demo,
                    round_no,
                    f"{OPPONENT}-p{i}",
                    LOBBY,
                    lineup=OPPONENT,
                    side="T",
                    sample_t_s=seconds,
                )
                for i in range(5)
            ]
        rows += [
            tick_row(
                demo, round_no, f"{TEAM}-p{i}", "Outside", side="CT", sample_t_s=6.0
            )
            for i in range(5)
        ]
        # Two different source areas on round 1, so that crunch is possible.
        rows += [
            tick_row(
                demo, round_no, f"{TEAM}-p0", "Ramp", side="CT", sample_t_s=15.0
            ),
            tick_row(
                demo, round_no, f"{TEAM}-p1", "Squeaky", side="CT", sample_t_s=15.0
            ),
        ]
        rows += [
            tick_row(
                demo, round_no, f"{TEAM}-p{i}", "Outside", side="CT", sample_t_s=15.0
            )
            for i in range(2, 5)
        ]
        rows += [
            tick_row(
                demo,
                round_no,
                f"{TEAM}-p{i}",
                LOBBY if round_no == 1 and i < 2 else "Outside",
                side="CT",
                sample_t_s=30.0,
            )
            for i in range(5)
        ]
    return rows


def eco_ct_rounds(demo: str, count: int = 3) -> list[dict[str, object]]:
    """Classified rounds: the subject as CT on saving rounds."""
    return [
        classified_row(demo, n, side="CT", round_type="eco")
        for n in range(1, count + 1)
    ]


def test_the_orientation_counts_t_rows_against_all_rows() -> None:
    """The share is the demo's own observation: T observations per all."""
    demo = "Nuke_vs_a"
    found = aggregate_stage._area_orientation(ticks_frame(lobby_ticks(demo)))
    assert found[LOBBY].t == 45
    assert found[LOBBY].total == 47
    assert found[LOBBY].t_share == pytest.approx(45 / 47)


def test_the_orientation_ignores_first_contact_dead_and_unnamed_rows() -> None:
    """Four restrictions, each a definition and not tidying."""
    demo = "Nuke_vs_a"
    rows = [
        tick_row(demo, 1, "p1", "Ramp", side="T"),
        tick_row(demo, 1, "p2", "Ramp", side="T", sample_kind="first_contact"),
        tick_row(demo, 1, "p3", "Ramp", side="T", is_alive=False),
        tick_row(demo, 1, "p4", None, side="T"),
        tick_row(demo, 1, "p5", "Ramp", side="T"),
    ]
    rows[-1]["side"] = None
    found = aggregate_stage._area_orientation(ticks_frame(rows))
    assert set(found) == {"Ramp"}
    assert found["Ramp"].total == 1


def test_an_unknown_side_is_left_out_of_the_denominator_too() -> None:
    """**A restriction on the denominator, not on the numerator.**

    Without the ``side`` filter ``pl.len()`` would count in a row of unknown
    side but ``side == "T"`` could not count it as T -- the share would press
    down and could drop a T area below the threshold. That share is precisely
    what both rules rest on, so the guard is here and not in the rule.
    """
    demo = "Nuke_vs_a"
    rows = [tick_row(demo, 1, f"t{i}", "Lobby", side="T") for i in range(9)]
    rows.append(tick_row(demo, 1, "x", "Lobby", side="CT"))
    clean = aggregate_stage._area_orientation(ticks_frame(rows))
    assert clean["Lobby"].t_share == pytest.approx(0.9)

    with_null = [dict(row) for row in rows]
    for _ in range(4):
        extra = dict(rows[0])
        extra["player_id"] = f"tuntematon{_}"
        extra["side"] = None
        with_null.append(extra)
    dirty = aggregate_stage._area_orientation(ticks_frame(with_null))
    # The rows of unknown side are in neither figure, so the share is the same
    # as it is without them.
    assert dirty["Lobby"].t_share == pytest.approx(0.9)
    assert dirty["Lobby"].total == clean["Lobby"].total


def test_the_orientation_normalises_the_area_name() -> None:
    """The same normalisation as on the presence row; otherwise the rules fall silent.

    ``" Lobby "`` and ``"Lobby"`` are one area, and their observations **are
    added together**: dropping one would compute the share from a subset and
    could turn the threshold.
    """
    demo = "Nuke_vs_a"
    rows = [
        tick_row(demo, 1, "t1", "Lobby", side="T"),
        tick_row(demo, 1, "t2", " Lobby ", side="T"),
        tick_row(demo, 1, "c1", "Lobby\t", side="CT"),
        tick_row(demo, 1, "c2", "   ", side="CT"),
    ]
    found = aggregate_stage._area_orientation(ticks_frame(rows))
    assert set(found) == {"Lobby"}
    assert found["Lobby"].t == 2
    assert found["Lobby"].total == 3


def test_a_demo_without_named_areas_gets_an_empty_orientation() -> None:
    """An empty map is the right answer and not an error: the side is not guessed."""
    demo = "Nuke_vs_a"
    rows = [tick_row(demo, 1, "p1", None, side="T")]
    assert aggregate_stage._area_orientation(ticks_frame(rows)) == {}


def test_the_orientation_must_come_from_the_unfiltered_table() -> None:
    """**The filter moved ahead of the orientation eats the hit entirely.**

    This is Story 2.5's measured condition as a machine guard. The same table,
    the same rule, the same threshold -- the only difference is whether the
    orientation is computed from the whole table or from the subject's rows.
    Without this test ``_aggregate``'s loop could be moved after the filter,
    and every true positive would disappear without one test failing.
    """
    demo = "Nuke_vs_a"
    ticks = ticks_frame(lobby_ticks(demo))
    classified = eco_ct_rounds(demo)
    subject = ticks.filter(pl.col("lineup_key") == TEAM)

    from_all = aggregate_stage._area_orientation(ticks)
    from_subject = aggregate_stage._area_orientation(subject)
    assert from_all[LOBBY].t_share > 0.80
    assert from_subject[LOBBY].t_share == 0.0

    # ``build_report`` sees only the subject's rows in either case; only the
    # orientation's source changes.
    subject_rows = subject.to_dicts()
    found = report_for(
        classified, subject_rows, area_orientation={demo: from_all}
    )
    lost = report_for(
        classified, subject_rows, area_orientation={demo: from_subject}
    )
    assert [a.rule for a in found.anomalies] == ["ct_advance", "crunch"]
    assert lost.anomalies == []


def test_the_stage_reads_the_orientation_before_it_filters(
    tmp_path: Path,
) -> None:
    """The whole stage end to end: the anomaly is in ``report.json``."""
    demo = "Nuke_vs_a"
    archive = build_archive(tmp_path, {demo: TEAM}, rounds=3)
    classified_frame(eco_ct_rounds(demo)).write_parquet(
        archive.classified(TEAM, demo)
    )
    ticks_frame(lobby_ticks(demo)).write_parquet(
        archive.parsed_table(demo, "ticks")
    )
    run(archive)
    report = read_report(archive)
    assert [a.rule for a in report.anomalies] == ["ct_advance", "crunch"]
    advance = report.anomalies[0]
    assert advance.map_name == "de_nuke"
    assert advance.map_name_source == "map_demo_id"
    assert advance.side == "CT"
    assert advance.round_types == ["eco"]
    assert advance.area == LOBBY
    assert (advance.n, advance.m) == (1, 3)
    assert advance.players_max == 2
    assert advance.rounds[0].round_no == 1
    assert advance.orientation[0].observations == 47
    crunch = report.anomalies[1]
    assert crunch.rounds[0].sources == ["Ramp", "Squeaky"]
    assert report.anomaly_scan.rounds_scanned == 3
    assert report.anomaly_scan.crunch_rounds == 3
    assert report.anomaly_scan.advance_rounds == 3
    assert report.anomaly_scan.demos_without_orientation == []


def test_the_run_output_names_the_anomalies_and_the_coverage(
    tmp_path: Path,
) -> None:
    """The run's output says the anomalies: otherwise an adjustment shows only in the report."""
    demo = "Nuke_vs_a"
    archive = build_archive(tmp_path, {demo: TEAM}, rounds=3)
    classified_frame(eco_ct_rounds(demo)).write_parquet(
        archive.classified(TEAM, demo)
    )
    ticks_frame(lobby_ticks(demo)).write_parquet(
        archive.parsed_table(demo, "ticks")
    )
    text = _render_aggregate(run(archive))
    assert "Anomalies" in text
    # ``2 pelaajaa`` is ``render.players_text``: report vocabulary passing
    # through the console (AD-11), so it stays Finnish.
    assert "ct_advance de_nuke CT eco: Lobby 2 pelaajaa (1/3)" in text
    assert (
        "the rules ct_advance, crunch, stack were run over 3 rounds -- "
        "crunch can hit 3, the advance 3 and stack 0"
    ) in text
    # There are no deferred rules any more, so the sentence does not belong in
    # the output. The stack rule's zero is not "not run" but a silenced demo,
    # and that is said as a figure of its own -- otherwise the user would go
    # looking for an implementation that is not missing.
    assert "not run:" not in text
    assert "without site groups, 1 of the demos: Nuke_vs_a" in text


def test_the_run_output_says_when_a_demo_has_no_orientation(
    tmp_path: Path,
) -> None:
    """A blind spot shows in the run's output too, not only in the report."""
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    text = _render_aggregate(run(archive))
    assert "without area orientation, 1 of the demos" in text


def test_a_demo_without_anomalies_writes_an_empty_list(tmp_path: Path) -> None:
    """An empty anomaly list is a valid result and not a missing field.

    The coverage is written all the same: the default archive's sample points
    hold only two areas and three observations, so not one crosses the
    observation threshold -- that is, the empty figure is a **blind spot** and
    not a measured negative, and saying exactly that difference is the
    coverage's job.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    report = read_report(archive)
    assert report.anomalies == []
    assert report.anomaly_scan.rules == ["ct_advance", "crunch", "stack"]
    assert report.anomaly_scan.rules_deferred == []
    assert report.anomaly_scan.demos_without_orientation == ["Nuke_vs_a"]
    # Two blind spots, two different reasons: no orientation was obtained (two
    # areas, three observations) and no site groups either (the default cloud
    # is empty). Neither is a measured negative.
    assert report.anomaly_scan.demos_without_site_groups == ["Nuke_vs_a"]
    assert report.anomaly_scan.stack_rounds == 0


def stack_ticks(demo: str, round_no: int) -> list[dict[str, object]]:
    """Four CT players in B's group, the fifth on A -- one sample point."""
    areas = ("BombsiteB", "BombsiteB", "SideEntrance", "Ramp", "BombsiteA")
    return [
        tick_row(demo, round_no, f"{TEAM}-p{i}", area, side="CT", sample_t_s=15.0)
        for i, area in enumerate(areas)
    ]


def test_the_stage_reads_the_point_cloud_and_finds_the_stack(
    tmp_path: Path,
) -> None:
    """The whole chain from the stage to the report: callouts.parquet -> site groups -> row.

    This is the only hermetic test that proves **the table is read**: the
    domain's tests give the cloud directly, and the calibration tests skip
    themselves on a machine that has no archive.
    """
    demo = "Ancient_vs_a"
    archive = build_archive(
        tmp_path, {demo: TEAM}, rounds=2, callouts={demo: SITE_CLOUD}
    )
    classified_frame(eco_ct_rounds(demo)).write_parquet(
        archive.classified(TEAM, demo)
    )
    ticks_frame(stack_ticks(demo, 1)).write_parquet(
        archive.parsed_table(demo, "ticks")
    )
    result = run(archive)
    report = read_report(archive)
    stacks = [a for a in report.anomalies if a.rule == "stack"]
    assert len(stacks) == 1
    assert (stacks[0].area, stacks[0].site) == ("BombsiteB", "B")
    assert stacks[0].rounds[0].players_max == 4
    assert stacks[0].rounds[0].points[0].alive == 5
    assert report.anomaly_scan.demos_without_site_groups == []
    assert report.anomaly_scan.stack_rounds == report.anomaly_scan.crunch_rounds
    # The run's output says the same as a fraction: "4 players" on its own
    # would be exactly the figure whose meaninglessness is the rule's whole
    # claim.
    assert (
        "stack de_ancient CT eco: BombsiteB 4/5 players (1/3)"
        in _render_aggregate(result)
    )


def test_a_demo_without_the_callouts_table_is_reported_missing(
    tmp_path: Path,
) -> None:
    """A missing table is an absence, not a silenced map.

    The difference is the whole value of the coverage: a silenced demo is an
    observation about the map, a missing table is a demo parsed with an old
    version. Silent, the latter would read in the coverage as the former.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM, "Anubis_vs_b": TEAM})
    archive.parsed_table("Anubis_vs_b", "callouts").unlink()
    result = run(archive)
    report = read_report(archive)
    assert [m.match for m in report.missing_demos] == ["Anubis_vs_b"]
    assert "callouts.parquet" in report.missing_demos[0].reason
    assert result.status == "ok"


#: For every anomaly threshold, a **valid** change from the default.
#:
#: A dictionary and not a single key, because ``crunch_min_sources`` cannot
#: move on its own: its lower bound is 2 (crunch is by definition several
#: source directions) and the validator forbids more source directions than
#: players, so its only way up raises ``crunch_min_players`` as well. That is
#: said out loud here, so that a missing key does not look like an oversight.
ANOMALY_THRESHOLD_CHANGES: tuple[tuple[str, dict[str, object]], ...] = (
    ("advance_t_share", {"advance_t_share": 0.9}),
    ("advance_area_min_observations", {"advance_area_min_observations": 40}),
    ("advance_max_sample_s", {"advance_max_sample_s": 15.0}),
    ("advance_min_players", {"advance_min_players": 2}),
    ("crunch_min_players", {"crunch_min_players": 3}),
    (
        "crunch_min_sources",
        {"crunch_min_players": 3, "crunch_min_sources": 3},
    ),
    # The stack rule's three (Story 2.14). The latter two change not the rule
    # but its INPUT -- the site groups from the demo's point cloud -- and that
    # is exactly why they would be the easiest to forget from the hash.
    ("stack_min_players", {"stack_min_players": 5}),
    ("stack_group_margin", {"stack_group_margin": 1.5}),
    ("stack_site_separation_min", {"stack_site_separation_min": 3.0}),
)


@pytest.mark.parametrize(
    "key,overrides",
    ANOMALY_THRESHOLD_CHANGES,
    ids=[name for name, _ in ANOMALY_THRESHOLD_CHANGES],
)
def test_every_anomaly_threshold_changes_the_params_hash(
    tmp_path: Path, key: str, overrides: dict[str, object]
) -> None:
    """Without this, adjusting a threshold would not re-run the aggregation.

    The same defect as in Story 1.8: the report would keep the old anomalies,
    and the user would see the effect of the adjustment only with ``--force``.

    **This is a runtime guard**, unlike
    :func:`test_every_setting_the_stage_reads_is_in_the_params_hash`, which
    reads the fields that are read from the source text with a regex and does
    not run the hash at all. A threshold's effect on **the report's content**
    is proved separately in ``test_aggregate.py``'s anomaly block.
    """
    archive = build_archive(tmp_path, {"Nuke_vs_a": TEAM})
    run(archive)
    before = Manifest.read(archive.report_manifest(TEAM)).params_hash
    aggregate_stage.run(
        thresholds(**overrides),
        _league(),
        archive,
        TEAM,
        aggregate_settings=aggregate_settings(),
    )
    assert Manifest.read(archive.report_manifest(TEAM)).params_hash != before


def test_every_hashed_anomaly_threshold_has_a_runtime_case() -> None:
    """The list must not go stale silently.

    The hashed keys and this file's runtime cases have to cover the same
    anomaly thresholds. Without this a new threshold could end up in the hash
    with not one run proving that it has an effect -- that is, exactly the
    state the review found ``crunch_min_sources`` in.
    """
    hashed = {
        key
        for key in aggregate_stage.HASHED_THRESHOLD_KEYS
        if key.startswith(("advance_", "crunch_", "stack_"))
    }
    covered = {name for name, _ in ANOMALY_THRESHOLD_CHANGES}
    assert hashed == covered
