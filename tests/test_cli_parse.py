"""``pappascout parse`` -- komennon ja sen tulosteen testit.

Two things are locked down here:

* **AD-3**: the command gives the stage only ``settings.parse``, not the
  whole ``Settings`` object. If the stage saw the thresholds, the promise "a
  change of threshold does not reparse" would no longer be structural.
* **NFR-1**: the output reports the number of rounds, overtime, the skipped
  rounds and the run time -- and no error reaches the screen as a traceback.

The stage itself is replaced, so none of these tests reads a demo.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from typer.testing import CliRunner

from pappascout.cli import (
    _PARSE_LABEL_WIDTH,
    _PARSE_LABEL_WIDTH,
    EXIT_KNOWN_ERROR,
    _render_parse,
    app,
    main,
)
from pappascout.domain.models import SETTINGS_ENV_VAR, ParseSettings
from pappascout.stages import StageResult

runner = CliRunner()

DEMO_ID = "1-abc-1"

#: The column the values start at: a two-space indent + the label column.
_VALUE_COLUMN = 2 + _PARSE_LABEL_WIDTH

#: The stage's numbers from a successful run -- 21 rounds, four sample points.
DEFAULT_STATS: dict[str, object] = {
    "rounds": 21,
    "rows": 42,
    "max_round_no": 21,
    "skipped_rounds": 1,
    "match_restarts": 0,
    "no_freeze_end": 0,
    "buy_window_seconds": 20.0,
    "buy_end_offsets_s": (10.4, 18.4, 20.0),
    "buy_window_truncated_by_death": 13,
    "buy_window_purchases_after_cut": 0,
    "buy_window_cuts_unchecked": 0,
    "buy_window_rounds_with_lost_purchases": (),
    "buy_window_ticks_without_players": 0,
    "buy_window_players_lost": 0,
    "buy_window_sides_without_rows": 0,
    "buy_window_refunds": 0,
    "buy_window_stale_equipment": 0,
    "armed_distribution": {0: 3, 4: 1, 5: 38},
    "armed_missing": 0,
    # The armour counter (Story 2.8). Deliberately a **different**
    # distribution from the armed one: on pistol rounds there are kevlars
    # although there are no armed players, and that difference is the reason
    # the row exists.
    "armored_distribution": {0: 2, 4: 2, 5: 38},
    "armored_missing": 0,
    "armed_unknown_items": (),
    "tick_rows": 780,
    "sample_points": 78,
    "sample_rounds": 21,
    "first_contact_rounds": 20,
    "partial_samples": 0,
    # Story 2.10. Zero is a clean run's value, and the row is then not
    # printed. The keys are here even so, because they belong to a fresh
    # run's numbers: without them the default numbers would describe a
    # skipped run, in which the keys are missing -- and that is a different
    # state in the output.
    "sample_rows_without_pawn": 0,
    "sample_points_without_pawn": 0,
    "grenade_throwers_without_row": 0,
    "unknown_side_events": 0,
    "event_rows": 300,
    "utility_throws": 152,
    "utility_detonations": 148,
    "utility_rounds": 21,
    "utility_area_observed": 152,
    # Story 2.9: a detonation's area comes from the point cloud. The numbers
    # have the shape of the measured Ancient run -- 139/148 named and 9
    # beyond the threshold -- because a "clean run" does not mean complete
    # coverage: a grenade that went off far from everything is a right result
    # and not a fault.
    "utility_area_point_cloud": 139,
    "utility_area_beyond_threshold": 9,
    "utility_without_area": 9,
    "utility_detonation_area_coverage": (139, 148),
    "utility_snap_distance": (15.0, 210.0, 1873.0),
    "utility_unnumbered_rounds": 0,
    "grenades_without_thrower": 0,
    "grenades_outside_rounds": 0,
    "grenades_unknown_side": 0,
    "grenades_unknown_type": 0,
    "grenades_fire_type_unresolved": 0,
    "grenades_detonating_after_round": 0,
    "grenade_ticks_without_players": 0,
    "grenades_sharing_an_entity_id": 0,
    # The point cloud (Story 2.9). The numbers are from the measured Ancient
    # run.
    "callout_cells": 7703,
    "callout_areas": 18,
    "callout_observations": 1092083,
    "callout_cloud_rows_read": 1529910,
    "callout_cloud_empty_reason": None,
    # The lineup table (Story 2.6). One row per lineup:
    # (lineup_key, clan or None, players, players without a name).
    "lineup_rows": 10,
    "lineups": (
        ("a1b2c3d4e5f60718", "MatureMayhem", 5, 0),
        ("b4ebc1ae68a589b6", "KALJUKOSTAJA", 5, 0),
    ),
    "lineup_name_conflicts": 0,
    "lineup_clan_conflicts": 0,
    # The deaths table (Story 2.7). These are in the defaults so that every
    # death row's label is visible to the width guard -- the guard renders
    # from this dictionary, and a block whose key is missing never prints.
    "death_rows": 141,
    "death_rounds": 20,
    "deaths_without_attacker": 1,
    "deaths_without_victim_area": 0,
    "deaths_without_attacker_area": 0,
    "deaths_unnumbered_rounds": 6,
    "deaths_without_tick": 0,
    "deaths_outside_rounds": 4,
    "deaths_without_victim": 0,
    "deaths_without_victim_side": 0,
    "deaths_attacker_without_side": 0,
}


def parse_result(**overrides) -> StageResult:
    """The stage's result with defaults; a test changes only what it studies."""
    defaults: dict[str, object] = {
        "stage": "parse",
        "unit": DEMO_ID,
        "status": "ok",
        "skipped": False,
        "outputs": (PurePosixPath("parsed/1-abc-1/rounds.parquet"),),
        "manifest_path": PurePosixPath("parsed/1-abc-1/parse.manifest.json"),
        "duration_s": 12.34,
        "stats": dict(DEFAULT_STATS),
    }
    defaults.update(overrides)
    return StageResult(**defaults)  # type: ignore[arg-type]


def stats(**overrides) -> dict[str, object]:
    """The stage's numbers with defaults; a test changes only what it studies."""
    numbers = dict(DEFAULT_STATS)
    numbers.update(overrides)
    return numbers


def field_value(output_text: str, label: str) -> str:
    """Pick one row's value by its label."""
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped.startswith(label):
            return stripped[len(label) :].strip()
    raise AssertionError(f"rivia {label!r} ei ole tulosteessa:" + chr(10) + output_text)


@pytest.fixture
def demo(tmp_path: Path) -> Path:
    """A placeholder for the demo -- the stage is replaced, so it is not read."""
    path = tmp_path / f"{DEMO_ID}.dem"
    path.write_bytes(b"PBDEMS2" + bytes(1))
    return path


@pytest.fixture
def fake_stage(settings_file: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace ``stages.parse.run`` and the port; return what the stage got."""
    seen: dict[str, object] = {}

    def fake_run(settings, archive, map_demo_id, parser, **kwargs):
        seen["settings"] = settings
        seen["archive"] = archive
        seen["unit"] = map_demo_id
        seen["parser"] = parser
        seen["kwargs"] = kwargs
        return seen.get("tulos") or parse_result(unit=map_demo_id)

    def fake_port(settings):
        # The first contact rule is a setting, so the port gets [parse].
        seen["portin_asetukset"] = settings
        return "portti"

    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("pappascout.stages.parse.run", fake_run)
    monkeypatch.setattr("pappascout.stages.parse.default_parser", fake_port)
    return seen


# --- The output ----------------------------------------------------------------


def test_reports_rounds_skips_and_duration() -> None:
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert field_value(output_text, "Rounds") == "21 (42 rows)"
    assert field_value(output_text, "Skipped rounds").startswith("1 (warmup")
    assert field_value(output_text, "Run time") == "12,3 s"
    assert "rounds.parquet" in output_text


def test_columns_line_up() -> None:
    """The values start at the same column, on the longest label's row too.

    The longest label is ``Partial sample points``; before it was added the
    output jumped past the column on exactly that row.
    """
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=1,
            no_freeze_end=2,
            tick_rate=64.0,
            tick_rate_measured=False,
        )
    )
    lines = _render_parse(result, regulation_rounds=24).splitlines()[1:]
    assert len(lines) >= 6

    for line in lines:
        assert line.startswith("  "), line
        # The value always starts at the same column, and the label fits in
        # front of it.
        assert line[_VALUE_COLUMN] != " ", f"the value misses the column: {line!r}"
        assert line[_VALUE_COLUMN - 1] == " ", f"label and value touch: {line!r}"
        assert line[2:_VALUE_COLUMN].strip(), f"the label is missing: {line!r}"


def test_mentions_overtime_only_when_earned() -> None:
    assert field_value(_render_parse(parse_result(), regulation_rounds=24), "Overtime") == (
        "no (21/24)"
    )

    overtime = parse_result(
        stats=stats(
            rounds=28,
            rows=56,
            max_round_no=28,
            skipped_rounds=1,
            no_freeze_end=0,
        )
    )
    line = field_value(_render_parse(overtime, regulation_rounds=24), "Overtime")
    assert line.startswith("yes")
    assert "28" in line


def test_hides_the_skip_line_when_nothing_was_skipped() -> None:
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
        )
    )
    assert "Skipped rounds" not in _render_parse(result, regulation_rounds=24)


def test_reports_match_restarts_separately_from_skipped_rounds() -> None:
    """A match restart is not even a round, so it has a row of its own.

    It is not included in the count of skipped rounds: a restart does not
    reach the rounds table at all, whereas a skipped round is there without a
    round number. That is also why the skipped rounds row no longer mentions
    restarts -- two rows would count the same thing.
    """
    result = parse_result(
        stats=stats(
            rounds=20,
            rows=40,
            max_round_no=20,
            skipped_rounds=1,
            match_restarts=1,
            no_freeze_end=0,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Match restarts") == (
        "1 round boundary with no number of the demo's own -- not a round, "
        "not a row in the table"
    )
    assert field_value(output_text, "Skipped rounds") == (
        "1 (warmup and the knife round)"
    )


def test_more_than_one_restart_takes_the_plural() -> None:
    """``1 round boundary`` but ``2 round boundaries`` -- the count inflects it."""
    result = parse_result(stats=stats(match_restarts=2))
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Match restarts").startswith(
        "2 round boundaries "
    )


def test_says_out_loud_when_there_were_no_restarts() -> None:
    """Zero is an observation: a fresh run says it out loud and does not hush."""
    result = parse_result(stats=stats(match_restarts=0))
    assert field_value(
        _render_parse(result, regulation_rounds=24), "Match restarts"
    ) == "none at all"


def test_a_port_that_cannot_report_restarts_is_not_a_zero() -> None:
    """``None`` is a different thing from zero: no claim without an observation."""
    result = parse_result(stats=stats(match_restarts=None))
    assert field_value(
        _render_parse(result, regulation_rounds=24), "Match restarts"
    ).startswith("not known")


def test_a_skipped_run_does_not_claim_there_were_no_restarts() -> None:
    """A skipped run has no key: the row is left out entirely.

    A restart is in no table, so its number cannot be read out of a finished
    result. Zero would be a claim nothing supports.
    """
    without = {k: v for k, v in DEFAULT_STATS.items() if k != "match_restarts"}
    result = parse_result(skipped=True, stats=without)
    assert "Match restarts" not in _render_parse(result, regulation_rounds=24)


def test_reports_rounds_without_a_freeze_anchor() -> None:
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=1,
            no_freeze_end=2,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Without an anchor").startswith("2 (the freezetime")


def test_says_when_the_stage_was_skipped() -> None:
    result = parse_result(skipped=True, reason="The result is up to date.")
    output_text = _render_parse(result, regulation_rounds=24)
    assert output_text.startswith("Skipped:")
    assert field_value(output_text, "Reason") == "The result is up to date."


def test_shows_a_non_ok_status_and_its_reason() -> None:
    """AD-9: a failed unit must not look like a successful one."""
    result = parse_result(status="no_freeze_end", reason="The anchor was missing.")
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Status") == "no_freeze_end"
    assert field_value(output_text, "Reason") == "The anchor was missing."


def test_ok_status_is_not_repeated_on_its_own_line() -> None:
    assert "Status" not in _render_parse(parse_result(), regulation_rounds=24)


def test_says_when_the_tick_rate_is_a_default() -> None:
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
            tick_rate=64.0,
            tick_rate_measured=False,
        )
    )
    assert "a default" in field_value(_render_parse(result, regulation_rounds=24), "Tickrate")


def test_measured_tick_rate_is_not_mentioned() -> None:
    result = parse_result(
        stats=stats(
            rounds=21,
            rows=42,
            max_round_no=21,
            skipped_rounds=0,
            no_freeze_end=0,
            tick_rate=64.0,
            tick_rate_measured=True,
        )
    )
    assert "Tickrate" not in _render_parse(result, regulation_rounds=24)


def test_never_claims_zero_rounds_when_the_result_is_unreadable() -> None:
    result = parse_result(skipped=True, stats={"unreadable": "OSError: broken"})
    output_text = _render_parse(result, regulation_rounds=24)
    assert "no counts obtained" in output_text
    assert field_value(output_text, "Rounds").startswith("no counts obtained")


# --- Sample points and first contacts ------------------------------------------


def test_reports_sample_points_and_first_contacts() -> None:
    """The user has to see that positional data came into being."""
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    line = field_value(output_text, "Sample points")
    assert line.startswith("78 (in 21/21 rounds")
    assert "780" in line
    assert field_value(output_text, "First contacts") == "20/21 rounds"


def test_rounds_without_any_sample_point_are_named() -> None:
    """A zero's cause is not guessed: the difference and the possible causes.

    A missing anchor and a very short round produce the same zero, so naming
    one cause would be a guess.
    """
    result = parse_result(stats=stats(sample_rounds=18))
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Sample points").startswith("78 (in 18/21 rounds")
    line = field_value(output_text, "No sample point")
    assert line.startswith("3 rounds")
    assert "anchor" in line


def test_every_round_sampled_hides_the_difference_line() -> None:
    assert "No sample point" not in _render_parse(
        parse_result(), regulation_rounds=24
    )


def test_armed_player_distribution_is_reported() -> None:
    """The distribution and the rule are reported during the run.

    A wrong rule does not show in the table at all: it would pass every
    schema check. The distribution is the cheapest way to notice it during
    the run rather than only in the report, and naming the rule is needed to
    interpret it.
    """
    line = field_value(
        _render_parse(parse_result(), regulation_rounds=24), "Armed"
    )
    assert line.startswith("armour and a weapon held at the end of the buy time; ")
    assert "0 -> 3 rows" in line
    assert "4 -> 1 rows" in line
    assert "5 -> 38 rows" in line


def test_unknown_inventory_items_are_named_with_their_counts() -> None:
    """Unknown names are reported with their counts, not merely counted.

    The classification is a list of allowed weapons, so an unknown name arms
    nobody. A new knife skin is an expected result and a new **weapon** is a
    sign that the list has fallen behind -- without the names they would look
    exactly alike. The count tells them apart more sharply still: one exotic
    knife shows once, a naming change on every row.
    """
    result = parse_result(
        stats=stats(armed_unknown_items=(("New Weapon", 12), ("Odd Knife", 1)))
    )
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Unknown items"
    )
    assert line.startswith("2 different item names: New Weapon x12, Odd Knife x1")
    assert "not counted as a weapon" in line


def test_one_unknown_item_is_named_in_the_singular() -> None:
    """One name is not "1 different item names"."""
    result = parse_result(stats=stats(armed_unknown_items=(("Odd Knife", 1),)))
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Unknown items"
    )
    assert line.startswith("1 item name: Odd Knife x1")


def test_unknown_item_list_is_truncated() -> None:
    """A row of hundreds of names is not readable -- nor is it even needed.

    If demoparser2 changes the way it names things, **every** name is
    unknown. The user then has to see at a glance that something is badly
    wrong, not scroll through three rows of names.
    """
    many = tuple((f"Name {index:02d}", 1) for index in range(30))
    result = parse_result(stats=stats(armed_unknown_items=many))
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Unknown items"
    )
    assert line.startswith("30 different item names: ")
    assert "Name 19 x1" in line
    assert "Name 20" not in line
    assert "(+10 more)" in line


def test_no_unknown_inventory_items_says_so() -> None:
    """An empty listing is said out loud: it is the run's healthy result."""
    line = field_value(
        _render_parse(parse_result(), regulation_rounds=24),
        "Unknown items",
    )
    assert line == "none at all"


def test_unknown_item_line_is_absent_when_the_run_was_skipped() -> None:
    """A skipped run does not know the names: the row is absent, not empty.

    The names are not in the table -- they arm nobody -- so they cannot be
    read back from a skipped run. "None at all" would then be a claim nothing
    supports.
    """
    numbers = stats()
    numbers.pop("armed_unknown_items")
    assert "Unknown items" not in _render_parse(
        parse_result(stats=numbers), regulation_rounds=24
    )


def test_unknown_item_line_says_when_the_port_does_not_report() -> None:
    """The third state: a fresh run with a port that does not report unknowns.

    ``None`` is a different thing from empty. If they were merged, the port
    going quiet would look as if every name had been recognised.
    """
    result = parse_result(stats=stats(armed_unknown_items=None))
    line = field_value(
        _render_parse(result, regulation_rounds=24), "Unknown items"
    )
    assert "not known" in line


def test_armed_player_line_separates_a_skewed_distribution_from_a_healthy_one() -> None:
    """The extremes are not enough: 41 rows of zero and one five is different.

    Both would give "0-5" as extremes, which looks healthy. That is the whole
    reason the row carries a distribution and not a min and a max.
    """
    healthy = parse_result(stats=stats(armed_distribution={0: 3, 4: 1, 5: 38}))
    skewed = parse_result(stats=stats(armed_distribution={0: 41, 5: 1}))

    healthy_line = field_value(_render_parse(healthy, regulation_rounds=24), "Armed")
    skewed_line = field_value(_render_parse(skewed, regulation_rounds=24), "Armed")

    assert healthy_line != skewed_line
    assert "0 -> 41 rows" in skewed_line
    assert "5 -> 1 rows" in skewed_line


def test_armed_player_line_names_the_rows_without_an_observation() -> None:
    result = parse_result(stats=stats(armed_missing=2))
    line = field_value(_render_parse(result, regulation_rounds=24), "Armed")
    assert line.endswith("the observation is missing from 2 rows")


def test_armed_player_line_says_when_there_is_no_observation_at_all() -> None:
    """An empty distribution is not zero: it is "not known" on every row."""
    result = parse_result(stats=stats(armed_distribution={}, armed_missing=42))
    line = field_value(_render_parse(result, regulation_rounds=24), "Armed")
    assert "no observations at all (42 rows)" in line


def test_armed_player_line_is_absent_without_the_numbers() -> None:
    """An unreadable result must not claim a distribution that is not there."""
    numbers = stats()
    numbers.pop("armed_distribution")
    numbers.pop("armed_unknown_items")
    assert "Armed" not in _render_parse(
        parse_result(stats=numbers), regulation_rounds=24
    )


def test_armored_player_line_reports_its_own_distribution() -> None:
    """The armour row is a row of its own and not an echo of the armed row.

    Both are in the output, and they **have** to be able to show different
    numbers: if the row read the same column, the distributions would be
    identical and the whole story would be pointless.
    """
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    armed_line = field_value(output_text, "Armed")
    armored_line = field_value(output_text, "Armoured")

    assert "0 -> 2 rows" in armored_line
    assert "5 -> 38 rows" in armored_line
    assert armored_line != armed_line
    # The rule is on the row: without it two rows with nearly the same name
    # in succession are read wrongly.
    assert "regardless of weapon" in armored_line


def test_armored_player_line_separates_a_skewed_distribution_from_a_healthy_one() -> None:
    """The extremes are not enough here either: a distribution, not min and max."""
    healthy = parse_result(stats=stats(armored_distribution={0: 3, 5: 39}))
    skewed = parse_result(stats=stats(armored_distribution={0: 41, 5: 1}))

    healthy_line = field_value(
        _render_parse(healthy, regulation_rounds=24), "Armoured"
    )
    skewed_line = field_value(
        _render_parse(skewed, regulation_rounds=24), "Armoured"
    )

    assert healthy_line != skewed_line
    assert "0 -> 41 rows" in skewed_line


def test_armored_player_line_names_the_rows_without_an_observation() -> None:
    result = parse_result(stats=stats(armored_missing=2))
    line = field_value(_render_parse(result, regulation_rounds=24), "Armoured")
    assert line.endswith("the observation is missing from 2 rows")


def test_armored_player_line_says_when_there_is_no_observation_at_all() -> None:
    """An empty distribution is not zero kevlars: it is "not known" on every row."""
    result = parse_result(stats=stats(armored_distribution={}, armored_missing=42))
    line = field_value(_render_parse(result, regulation_rounds=24), "Armoured")
    assert "no observations at all (42 rows)" in line


def test_armored_player_line_is_absent_without_the_numbers() -> None:
    """An unreadable result must not claim a distribution that is not there."""
    numbers = stats()
    numbers.pop("armored_distribution")
    assert "Armoured" not in _render_parse(
        parse_result(stats=numbers), regulation_rounds=24
    )


def test_partial_sample_points_are_reported() -> None:
    """A partial sample point is the adapter's observation -- not in the table."""
    result = parse_result(stats=stats(partial_samples=4))
    line = field_value(_render_parse(result, regulation_rounds=24), "Partial sample points")
    assert line.startswith("4 (")


def test_a_pawnless_player_gets_its_own_line() -> None:
    """Story 2.10: a skipped row shrinks the setup, and the reader must see it.

    Without a row of its own the round would look as if the team had simply
    played a man down -- and not as if one player had dropped off the map.
    """
    result = parse_result(stats=stats(sample_rows_without_pawn=3))
    line = field_value(_render_parse(result, regulation_rounds=24), "Player without a pawn")
    assert line.startswith("3 rows were skipped")
    assert "the controller is there" in line
    # Zero dropped points must not produce the extra clause.
    assert "went missing entirely" not in line


def test_a_dropped_sample_point_joins_the_pawnless_line() -> None:
    """A point missed entirely is graver than a partial one, and it is said.

    It is not in ``partial_samples`` and must not be left to the row count
    alone: ten skipped rows mean a different thing depending on whether they
    spread over ten points or emptied one.
    """
    result = parse_result(
        stats=stats(sample_rows_without_pawn=10, sample_points_without_pawn=1)
    )
    line = field_value(_render_parse(result, regulation_rounds=24), "Player without a pawn")
    assert "10 rows were skipped" in line
    assert "1 of the sample points went missing entirely" in line


def test_the_partial_line_names_pawnless_rows_as_a_cause() -> None:
    """The same event must not look like two different events.

    One player without a pawn produces both a partial sample point and a
    skipped row. The docstring knows the connection; without this the output
    does not.
    """
    both = _render_parse(
        parse_result(stats=stats(partial_samples=1, sample_rows_without_pawn=1)),
        regulation_rounds=24,
    )
    assert "the pawnless rows below are one cause" in field_value(
        both, "Partial sample points"
    )

    # Without pawnless rows the cause is not claimed.
    alone = _render_parse(
        parse_result(stats=stats(partial_samples=1, sample_rows_without_pawn=0)),
        regulation_rounds=24,
    )
    assert "pawnless" not in field_value(alone, "Partial sample points")


def test_a_dropped_point_is_named_among_the_reasons_for_a_missing_sample() -> None:
    """The fourth cause of a round with no sample point is Story 2.10's own.

    The explanation listed three causes -- a missing anchor, an early
    decision and wrong sample point times -- and none of them is this one.
    The cause is mentioned only when it has been measured, so that the
    explanation does not list a cause there was none of.
    """
    numbers = stats(
        sample_rounds=20,
        sample_rows_without_pawn=10,
        sample_points_without_pawn=4,
    )
    line = field_value(
        _render_parse(parse_result(stats=numbers), regulation_rounds=24),
        "No sample point",
    )
    assert "Player without a pawn" in line

    clean = stats(sample_rounds=20, sample_points_without_pawn=0)
    assert "Player without a pawn" not in field_value(
        _render_parse(parse_result(stats=clean), regulation_rounds=24),
        "No sample point",
    )


def test_a_clean_run_hides_the_pawnless_line() -> None:
    """A zero is not printed, as with the other anomaly counters."""
    assert "Player without a pawn" not in _render_parse(
        parse_result(stats=stats(sample_rows_without_pawn=0)), regulation_rounds=24
    )


def test_a_skipped_run_does_not_claim_a_clean_lineup() -> None:
    """Pawnless rows cannot be read from a skipped run, and are not invented.

    The row is left out entirely -- not as a zero, which would look like a
    measurement.
    """
    numbers = {
        key: value
        for key, value in DEFAULT_STATS.items()
        if not key.startswith(("sample_rows_", "sample_points_", "grenade_throwers_"))
    }
    text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert "Player without a pawn" not in text


def test_a_port_that_cannot_count_pawnless_rows_says_so() -> None:
    """``None`` is a different thing from zero: not knowing is not a clean result."""
    line = field_value(
        _render_parse(
            parse_result(stats=stats(sample_rows_without_pawn=None)),
            regulation_rounds=24,
        ),
        "Player without a pawn",
    )
    assert "not known" in line


def test_a_thrower_without_a_row_gets_its_own_line() -> None:
    """Story 2.10 created a new drop, and it has to have a cause in the output.

    A throw by a thrower without a pawn is left without an area. Without a
    row of its own it would drift into the ``Utility area`` row's "without an
    area" count, where it would look like the threshold's price.
    """
    line = field_value(
        _render_parse(
            parse_result(stats=stats(grenade_throwers_without_row=2)),
            regulation_rounds=24,
        ),
        "Thrower without a row",
    )
    assert line.startswith("2 throws were left without an area")

    assert "Thrower without a row" not in _render_parse(
        parse_result(stats=stats(grenade_throwers_without_row=0)),
        regulation_rounds=24,
    )


def test_events_with_an_unknown_side_are_reported() -> None:
    result = parse_result(stats=stats(unknown_side_events=2))
    line = field_value(_render_parse(result, regulation_rounds=24), "Side unknown")
    assert line.startswith("2 damage events")


def test_clean_run_hides_both_diagnostic_lines() -> None:
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert "Partial sample points" not in output_text
    assert "Side unknown" not in output_text


def test_unreadable_ticks_do_not_hide_the_round_counts() -> None:
    """One broken table must not take another one's numbers with it."""
    numbers = stats()
    for key in (
        "tick_rows",
        "sample_points",
        "sample_rounds",
        "first_contact_rounds",
    ):
        numbers.pop(key)
    numbers["ticks_unreadable"] = "OSError: broken"
    output_text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert field_value(output_text, "Rounds") == "21 (42 rows)"
    assert field_value(output_text, "Sample points").startswith("no counts obtained")


def test_zero_sample_points_is_said_out_loud() -> None:
    """A zero must not be lost: the round count would look the same when empty."""
    result = parse_result(
        stats=stats(tick_rows=0, sample_points=0, sample_rounds=0,
                    first_contact_rounds=0)
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Sample points").startswith("0 --")
    assert field_value(output_text, "First contacts").startswith("0 --")


def test_zero_first_contacts_is_said_out_loud_even_with_samples() -> None:
    """A first contact rule that never bites must not look like a normal run."""
    result = parse_result(stats=stats(first_contact_rounds=0))
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Sample points").startswith("78 ")
    assert field_value(output_text, "First contacts").startswith("0 --")


def test_sample_lines_are_absent_when_the_result_was_unreadable() -> None:
    """Without numbers a zero is not invented -- it would claim an empty result."""
    result = parse_result(skipped=True, stats={"unreadable": "OSError: broken"})
    output_text = _render_parse(result, regulation_rounds=24)
    assert "Sample points" not in output_text
    assert "First contacts" not in output_text


def test_both_output_tables_are_listed() -> None:
    result = parse_result(
        outputs=(
            PurePosixPath("parsed/1-abc-1/rounds.parquet"),
            PurePosixPath("parsed/1-abc-1/ticks.parquet"),
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert "rounds.parquet" in output_text
    assert "ticks.parquet" in output_text


# --- The command ----------------------------------------------------------------


def test_stage_gets_only_the_parse_section(fake_stage, demo: Path) -> None:
    """AD-3: the stage must see neither the thresholds nor the league settings."""
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output

    settings = fake_stage["settings"]
    assert isinstance(settings, ParseSettings)
    for forbidden in ("thresholds", "league", "economy", "project"):
        assert not hasattr(settings, forbidden)
    assert fake_stage["unit"] == DEMO_ID
    assert fake_stage["parser"] == "portti"
    # The port gets the same [parse] section: the first contact rule is a
    # setting.
    assert fake_stage["portin_asetukset"] is settings
    assert fake_stage["kwargs"]["force"] is False
    assert fake_stage["kwargs"]["demo_path"] == demo


def test_force_flag_reaches_the_stage(fake_stage, demo: Path) -> None:
    result = runner.invoke(app, ["parse", str(demo), "--force"])
    assert result.exit_code == 0, result.output
    assert fake_stage["kwargs"]["force"] is True


def test_overtime_line_uses_the_league_format(fake_stage, demo: Path) -> None:
    """The number of regulation rounds is 2 x the league's MR value."""
    fake_stage["tulos"] = parse_result(
        stats=stats(
            rounds=28,
            rows=56,
            max_round_no=28,
            skipped_rounds=1,
            no_freeze_end=0,
        )
    )
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output
    # In settings.toml mr = 12 -> 24 regulation rounds.
    assert "regulation is 24" in result.output


def test_run_is_announced_before_it_starts(fake_stage, demo: Path) -> None:
    """Several seconds of silence would look like a hang."""
    result = runner.invoke(app, ["parse", str(demo)])
    assert result.exit_code == 0, result.output
    assert f"Parsing {DEMO_ID}" in result.output


def test_missing_demo_ends_in_one_line_without_a_traceback(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The user does not code: exit code 1 and one line.

    The name used to say the line is in Finnish. It was true only while the
    CLI's own ``Virhe:`` heading was Finnish; T15 translated it, so the name
    says what the message does instead.
    """
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("sys.argv", ["pappascout", "parse", "1-ei-tallaista-demoa-1"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    error = capsys.readouterr().err
    assert "Error:" in error
    # The body of the message comes from ``stages.parse.resolve_demo``, which
    # T8 translated; the CLI's own wrapper around it is the ``Error:``
    # heading, which T15 translated.
    assert "was not found" in error
    assert "Traceback" not in error


# --- Utility ---------------------------------------------------------------------


def test_reports_utility_throws_detonations_and_areas() -> None:
    """Four numbers, four questions: did it come into being, end, land, vanish."""
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    line = field_value(output_text, "Utility ")
    assert line.startswith("152 throws, 148 detonations")
    assert "in 21/21 rounds" in line
    assert field_value(output_text, "Without a detonation").startswith("4 grenades")
    assert field_value(output_text, "Utility area") == (
        "152 observed, 139 from the point cloud, 9 without an area"
    )


def test_observed_and_derived_areas_are_never_lumped_together() -> None:
    """A throw's area is an observation, a detonation's an estimate -- lumped
    together the report's reader would take both to be equally certain."""
    line = field_value(_render_parse(parse_result(), regulation_rounds=24), "Utility area")
    assert "observed" in line
    assert "from the point cloud" in line


def test_the_detonation_area_coverage_is_reported_as_a_share() -> None:
    """Story 2.9's most important number: how many detonations got an area.

    A share and not the numerator alone -- 139 areas is different news out of
    148 than out of 1,480, and the reader does not work it out themselves.
    """
    line = field_value(_render_parse(parse_result(), regulation_rounds=24), "Detonation area")
    assert line == "139/148 named (94%)"


def test_the_snap_distance_spread_is_reported() -> None:
    """The setting states the threshold; this states where it really landed.

    The largest number is the one that shows why the threshold exists:
    without it "a nearest cell is always found" would look like coverage.
    """
    line = field_value(
        _render_parse(parse_result(), regulation_rounds=24), "Distance to a cell"
    )
    assert line == "median 15, p90 210, largest 1873 units"


def test_detonations_beyond_the_threshold_get_their_own_line() -> None:
    """"The nearest cell is far away" and "the cloud was empty" differ."""
    line = field_value(
        _render_parse(parse_result(), regulation_rounds=24), "Beyond the threshold"
    )
    assert line.startswith("9 detonations")


def test_full_coverage_hides_the_threshold_line() -> None:
    result = parse_result(
        stats=stats(
            utility_area_beyond_threshold=0,
            utility_area_point_cloud=148,
            utility_without_area=0,
            utility_detonation_area_coverage=(148, 148),
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert "Beyond the threshold" not in output_text
    assert field_value(output_text, "Detonation area") == "148/148 named (100%)"


# --- The point cloud (Story 2.9) -------------------------------------------------


def test_the_point_cloud_block_names_cells_and_areas() -> None:
    """The number of areas matters more than the number of cells: it says
    whether the cloud recognised the map. A single-digit number would mean an
    empty ``last_place_name``."""
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert field_value(output_text, "Point cloud") == "7703 cells, 18 areas"
    assert field_value(output_text, "Cloud observations") == (
        "1092083/1529910 tick rows qualified (71% alive and with a known "
        "area)"
    )


def test_an_empty_point_cloud_says_so_and_says_why() -> None:
    """The I/O matrix: an empty point cloud -- the run survives, the cause is
    reported.

    Without the cause an empty cloud would look like a demo in which no
    utility was thrown.
    """
    result = parse_result(
        stats=stats(
            callout_cells=0,
            callout_areas=0,
            callout_observations=0,
            callout_cloud_empty_reason="1529910 tick rows were read, but not "
            "one of them had a living player in a named area",
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Point cloud") == (
        "empty -- not one detonation area is named"
    )
    assert field_value(output_text, "Cloud empty because").startswith("1529910 tick rows")


def test_a_healthy_point_cloud_hides_the_reason_line() -> None:
    assert "Cloud empty because" not in _render_parse(
        parse_result(), regulation_rounds=24
    )


def test_unreadable_callouts_do_not_hide_the_other_counts() -> None:
    """One broken table must not take another one's numbers with it."""
    numbers = stats()
    for key in ("callout_cells", "callout_areas", "callout_observations"):
        numbers.pop(key)
    numbers["callouts_unreadable"] = "OSError: broken"
    output_text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert field_value(output_text, "Point cloud").startswith("no counts obtained")
    assert field_value(output_text, "Rounds") == "21 (42 rows)"


def test_more_detonations_than_throws_is_never_a_negative_count() -> None:
    """A negative "without a detonation" would be readable the wrong way round.

    A detonation comes into being only as a throw's pair, so a surplus is a
    fault and not an observation.
    """
    result = parse_result(stats=stats(utility_throws=10, utility_detonations=13))
    output_text = _render_parse(result, regulation_rounds=24)
    assert "Without a detonation" not in output_text
    assert field_value(output_text, "Too many detonations").startswith("3 more than")


def test_utility_dropped_by_the_stage_is_reported() -> None:
    """Utility dropped from unnumbered rounds must not vanish quietly."""
    result = parse_result(stats=stats(utility_unnumbered_rounds=9))
    line = field_value(_render_parse(result, regulation_rounds=24), "No round number")
    assert line.startswith("9 throws")


def test_the_remaining_utility_diagnostics_are_reported() -> None:
    """Every silent drop or uncertainty gets a row of its own.

    The rows are checked for width too: the summary's columns go astray if
    one description is twice as long as the others.
    """
    result = parse_result(
        stats=stats(
            grenades_unknown_type=2,
            grenades_fire_type_unresolved=5,
            grenades_detonating_after_round=3,
            grenade_ticks_without_players=1,
            grenades_sharing_an_entity_id=4,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Unknown type").startswith("2 grenades")
    assert field_value(output_text, "Fire type unresolved").startswith("5 grenades")
    assert field_value(output_text, "Late detonation").startswith("3 after the round")
    assert field_value(output_text, "No rows on the tick").startswith("1 throws")

    # A shared id is **an observation and not a fault**, and the row has to
    # say so. The whole value is checked and not only its beginning: a bare
    # startswith let through both a wrong unit and a parquet column name.
    shared = field_value(output_text, "Shared id")
    assert shared == "4 grenades share the game's id within a round (an observation)"
    assert "grenade_no" not in output_text


def test_zero_utility_is_said_out_loud() -> None:
    """A zero must not be lost: the round count would look the same when empty."""
    result = parse_result(
        stats=stats(
            event_rows=0,
            utility_throws=0,
            utility_detonations=0,
            utility_rounds=0,
            utility_area_observed=0,
            utility_area_point_cloud=0,
            utility_area_beyond_threshold=0,
            utility_without_area=0,
            utility_detonation_area_coverage=None,
            utility_snap_distance=None,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Utility").startswith("0 throws --")
    assert "Utility area" not in output_text


def test_every_grenade_detonated_hides_the_difference_line() -> None:
    result = parse_result(stats=stats(utility_detonations=152))
    assert "Without a detonation" not in _render_parse(result, regulation_rounds=24)


def test_dropped_grenades_are_reported_not_hidden() -> None:
    """A dropped grenade is not visible in a finished table -- the count is all."""
    result = parse_result(
        stats=stats(
            grenades_without_thrower=2,
            grenades_outside_rounds=7,
            grenades_unknown_side=1,
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert field_value(output_text, "Without a thrower").startswith("2 trajectories")
    assert field_value(output_text, "Without a round").startswith("7 grenades")
    assert field_value(output_text, "Without a side").startswith("1 grenades")


def test_a_clean_run_hides_the_dropped_grenade_lines() -> None:
    output_text = _render_parse(parse_result(), regulation_rounds=24)
    assert "Without a thrower" not in output_text
    assert "Without a round" not in output_text
    assert "Without a side" not in output_text


def test_unreadable_events_do_not_hide_the_other_counts() -> None:
    """One broken table must not take another one's numbers with it."""
    numbers = stats()
    for key in (
        "event_rows",
        "utility_throws",
        "utility_detonations",
        "utility_rounds",
        "utility_area_observed",
        "utility_area_point_cloud",
        "utility_area_beyond_threshold",
        "utility_without_area",
        "utility_detonation_area_coverage",
        "utility_snap_distance",
    ):
        numbers.pop(key)
    numbers["events_unreadable"] = "OSError: broken"
    output_text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert field_value(output_text, "Rounds") == "21 (42 rows)"
    assert field_value(output_text, "Sample points").startswith("78 ")
    assert field_value(output_text, "Utility").startswith("no counts obtained")


def test_utility_lines_are_absent_when_the_result_was_unreadable() -> None:
    """Without numbers a zero is not invented -- it would claim an empty result."""
    result = parse_result(skipped=True, stats={"unreadable": "OSError: broken"})
    assert "Utility" not in _render_parse(result, regulation_rounds=24)


def test_all_three_output_tables_are_listed() -> None:
    result = parse_result(
        outputs=(
            PurePosixPath("parsed/1-abc-1/rounds.parquet"),
            PurePosixPath("parsed/1-abc-1/ticks.parquet"),
            PurePosixPath("parsed/1-abc-1/events.parquet"),
        )
    )
    output_text = _render_parse(result, regulation_rounds=24)
    assert "rounds.parquet" in output_text
    assert "ticks.parquet" in output_text
    assert "events.parquet" in output_text


# --- The buy window (Story 1.9) -------------------------------------------------


def test_the_measurement_point_is_always_named() -> None:
    """The run says which moment the economy numbers were read from.

    The moment of measurement is a setting, so two results run with two
    different values are different numbers in identical-looking tables.
    Without this row the reader cannot know which of the two they are
    looking at.
    """
    text = _render_parse(
        parse_result(stats=stats(buy_window_seconds=20.0)), regulation_rounds=24
    )
    line = field_value(text, "Measurement point")
    assert "end of the buy time" in line
    assert "20,0 s" in line


def test_a_zero_window_says_it_measured_the_anchor() -> None:
    """Window 0 measures at the end of the freezetime, and is said by that name.

    "The end of the buy time, window 0.0 s" would be true but misleading: a
    measurement by exactly that name was the fault this story fixes.
    """
    text = _render_parse(
        parse_result(stats=stats(buy_window_seconds=0.0)), regulation_rounds=24
    )
    assert "end of the freezetime" in field_value(text, "Measurement point")


def test_a_clean_death_cut_is_reported_as_zero_not_silence() -> None:
    """Zero lost purchases is said out loud.

    A death cuts the window short in about half the rounds, so the number of
    cuts is not an alarm. The alarm is whether purchases were left behind the
    cut -- and an unspoken zero would not stand apart from an unspoken five.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=13,
                buy_window_purchases_after_cut=0,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Cut short by a death")
    assert "13 rounds" in line
    assert "not one purchase was left" in line


def test_a_purchase_lost_behind_the_cut_is_reported() -> None:
    """A lost purchase shows in the output as a number, not just a cut count."""
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=4,
                buy_window_purchases_after_cut=2,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Cut short by a death")
    assert "2 players bought after the cut" in line


def test_a_skipped_run_does_not_claim_a_clean_buy_window() -> None:
    """A skipped run has no numbers, so it has no rows either.

    The number of cuts and of lost purchases cannot be read from a finished
    table. "None at all" would be a claim nothing supports -- the same rule
    as with the restarts and the unknown items.
    """
    numbers = {
        key: value
        for key, value in DEFAULT_STATS.items()
        if not key.startswith("buy_window_")
    }
    text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert "Measurement point" not in text
    assert "Cut short by a death" not in text


def test_an_unknown_buy_window_is_not_claimed_to_be_the_anchor() -> None:
    """A port that does not report the window must not look like an anchor read.

    ``None`` and ``0.0`` are different things: the latter is a choice, the
    former is not knowing. "Economy read at the end of the freezetime" would
    be a confident claim about a moment nothing supports.
    """
    text = _render_parse(
        parse_result(stats=stats(buy_window_seconds=None)), regulation_rounds=24
    )
    line = field_value(text, "Measurement point")
    assert "not known" in line
    assert "end of the freezetime" not in line


def test_a_defaulted_tick_rate_makes_the_window_an_estimate() -> None:
    """The window is computed from the tickrate, so a defaulted one is reported.

    The row prints the seconds with equal confidence in both cases, so
    without this addition a 20,0 s resting on a default would look like a
    measurement.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0, tick_rate=64.0, tick_rate_measured=False
            )
        ),
        regulation_rounds=24,
    )
    assert "tickrate a default" in field_value(text, "Measurement point")


def test_the_real_measurement_offsets_are_shown() -> None:
    """The setting promises the window's length; the spread says where it landed.

    It is the ``buy_end_tick`` column's only visible form: if the window is
    20 s but the median is 12 s, a death cuts it short more often than not.
    """
    text = _render_parse(
        parse_result(
            stats=stats(buy_window_seconds=20.0, buy_end_offsets_s=(3.5, 12.0, 20.0))
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Measurement point")
    assert "3,5 s-20,0 s" in line
    assert "median 12,0 s" in line


def test_an_unchecked_cut_is_told_apart_from_a_clean_one() -> None:
    """A cut that went unchecked is said separately.

    Without it ``buy_window_purchases_after_cut``'s zero would mean two
    different things: "nothing was lost" and "it is not known".
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=6,
                buy_window_purchases_after_cut=0,
                buy_window_cuts_unchecked=2,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Cut short by a death")
    assert "not one purchase was left" in line
    assert "2 rounds could not be checked" in line


def test_a_lost_purchase_names_the_rounds() -> None:
    """A lost purchase can be traced: the row names the rounds.

    One number for the whole demo gives the user nothing to go on.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=4,
                buy_window_purchases_after_cut=2,
                buy_window_rounds_with_lost_purchases=(7, 12),
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Cut short by a death")
    assert "2 players bought after the cut" in line
    assert "round_raw) 7, 12" in line


def test_the_singular_forms_inflect() -> None:
    """At 1 the noun inflects: "1 round", "1 player".

    One lost purchase is exactly the case the row is reporting, so "1
    players" would be wrong at the very moment the row matters most. The
    name used to say the forms are Finnish; the rule survived the
    translation, the language did not.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_truncated_by_death=1,
                buy_window_purchases_after_cut=1,
                buy_window_rounds_with_lost_purchases=(9,),
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Cut short by a death")
    assert "1 round measured earlier" in line
    assert "1 player bought after" in line


def test_an_empty_buy_tick_is_reported_as_a_fault() -> None:
    """An empty buy tick is a fault, and it gets a row of its own.

    It is the only path ``ParseDiagnostics`` marks with the words "a fault
    and not an observation", and without the row the measurement would have
    fallen back to the anchor quietly.
    """
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0, buy_window_ticks_without_players=2
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Buy-end tick empty")
    assert "2 rounds" in line
    assert "fell back to the freezetime anchor" in line


def test_players_lost_from_the_buy_tick_are_reported() -> None:
    """Players lost and an entirely empty team row show on the same row."""
    text = _render_parse(
        parse_result(
            stats=stats(
                buy_window_seconds=20.0,
                buy_window_players_lost=5,
                buy_window_sides_without_rows=1,
            )
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Players lost")
    assert "5 players" in line
    assert "1 of the team rows ended up entirely empty" in line


def test_stale_equipment_gets_its_own_line() -> None:
    """The stale equipment value a refund leaves is reported and bounded."""
    text = _render_parse(
        parse_result(
            stats=stats(buy_window_seconds=20.0, buy_window_stale_equipment=1)
        ),
        regulation_rounds=24,
    )
    line = field_value(text, "Stale value")
    assert "1 player" in line
    assert "$1000 per player" in line


def test_a_clean_run_does_not_print_the_fault_lines() -> None:
    """The zeros do not repeat on every run.

    All four of these are faults and not the normal state. Repeating a zero
    would teach the reader to skip the row just before the one time it
    matters.
    """
    text = _render_parse(parse_result(), regulation_rounds=24)
    for label in (
        "Buy-end tick empty",
        "Players lost",
        "Stale value",
    ):
        assert label not in text


def test_every_parse_label_fits_the_column() -> None:
    """Every label fits the column, the rarely seen ones too.

    ``_line`` pads the label to a fixed width; too long a label eats the
    space and the value sticks to it ("Stale equipment value1 player"). The
    fault rows appear only when something is broken, so without this test a
    formatting mistake would come to light exactly when the row should be as
    clear as possible.
    """
    every = stats(
        buy_window_seconds=20.0,
        buy_window_truncated_by_death=3,
        buy_window_purchases_after_cut=1,
        buy_window_rounds_with_lost_purchases=(4,),
        buy_window_cuts_unchecked=1,
        buy_window_ticks_without_players=1,
        buy_window_players_lost=2,
        buy_window_sides_without_rows=1,
        buy_window_stale_equipment=1,
        armed_unknown_items=(("Tuntematon Ase", 3),),
        # The deaths block's fault rows print only when non-zero, and they
        # are the longest labels in the whole output.
        deaths_without_victim_area=1,
        deaths_without_attacker_area=1,
        deaths_without_tick=1,
        deaths_without_victim=1,
        deaths_without_victim_side=1,
        deaths_attacker_without_side=1,
        # Story 2.10: the new labels are visible to the guard only with
        # non-zero numbers.
        sample_rows_without_pawn=1,
        sample_points_without_pawn=1,
        grenade_throwers_without_row=1,
    )
    text = _render_parse(parse_result(stats=every), regulation_rounds=24)

    seen = 0
    for line in text.splitlines():
        if not line.startswith("  ") or ":" in line[:4]:
            continue
        body = line[2:]
        if not body.strip():
            continue
        seen += 1
        # The claim is about **the padding**, not about looking for a
        # separator. Too long a label eats its own padding, the value then
        # starts right after it and the row holds no two consecutive spaces
        # at all -- and that separator is exactly what the old version looked
        # for. So it skipped precisely the rows it was meant to check.
        column = body[:_PARSE_LABEL_WIDTH]
        assert column != column.rstrip(), (
            f"the label {body.split('  ')[0]!r} fills the whole "
            f"{_PARSE_LABEL_WIDTH}-character column, so the value sticks "
            "to it"
        )
    # Without this an empty output would pass the guard: the loop would not
    # run once and no claim would be made.
    assert seen > 20, f"only {seen} label rows to check"


# --- The lineup block (Story 2.6) ------------------------------------------------


def test_the_lineup_block_names_the_clan_and_its_lineup_key() -> None:
    """The name says who is meant, the id says what to type on the command line.

    The user's next command is ``classify --team <lineup_key>``, and the
    stage has both values in hand.
    """
    output_text = _render_parse(parse_result(), regulation_rounds=24)

    assert field_value(output_text, "Lineups") == "10 player rows"
    lines = [
        line.strip()
        for line in output_text.splitlines()
        if line.strip().startswith("Lineup ")
    ]
    assert len(lines) == 2
    assert "MatureMayhem (a1b2c3d4e5f60718) -- 5 players" in lines[0]
    assert "KALJUKOSTAJA (b4ebc1ae68a589b6) -- 5 players" in lines[1]


def test_a_lineup_without_a_clan_says_so_on_its_own_row() -> None:
    """The opponent's name must not hide that this team has no name.

    A shared clan listing would be non-empty as soon as either of them has a
    name, and so would say nothing about either.
    """
    numbers = stats(
        lineups=(
            ("aaaa", None, 5, 5),
            ("bbbb", "KALJUKOSTAJA", 5, 0),
        )
    )
    output_text = _render_parse(
        parse_result(stats=numbers), regulation_rounds=24
    )
    assert "no clan name was observed (aaaa) -- 5 players" in output_text
    assert "5 without a name" in output_text
    assert "KALJUKOSTAJA (bbbb) -- 5 players" in output_text


def test_a_conflicting_name_or_clan_is_reported_and_zero_is_not() -> None:
    """Zero is the expected value; the anomaly is the symptom to report."""
    quiet = _render_parse(parse_result(), regulation_rounds=24)
    assert "changed mid-map" not in quiet

    loud = _render_parse(
        parse_result(
            stats=stats(lineup_clan_conflicts=2, lineup_name_conflicts=1)
        ),
        regulation_rounds=24,
    )
    assert field_value(loud, "Clan changed mid-map").startswith("2 of the players had")
    assert field_value(loud, "Name changed mid-map").startswith("1 of the players had")


def test_unreadable_lineups_do_not_hide_the_other_counts() -> None:
    """One broken table must not take another one's numbers with it."""
    numbers = stats()
    for key in ("lineup_rows", "lineups"):
        numbers.pop(key)
    numbers["lineups_unreadable"] = "OSError: broken"
    output_text = _render_parse(
        parse_result(skipped=True, stats=numbers), regulation_rounds=24
    )
    assert field_value(output_text, "Rounds") == "21 (42 rows)"
    assert field_value(output_text, "Lineups").startswith("no counts obtained")


def test_the_lineup_block_is_absent_when_the_result_was_unreadable() -> None:
    """Without numbers a zero is not invented -- it would claim an empty lineup."""
    result = parse_result(skipped=True, stats={"unreadable": "OSError: broken"})
    assert "Lineup" not in _render_parse(result, regulation_rounds=24)
