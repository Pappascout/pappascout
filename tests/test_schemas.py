"""Tests for the schema validation -- the first four rows of the I/O matrix.

``validate`` is deliberately strict in both directions: the Polars join that
goes silently empty is exactly the defect this contract prevents.
"""

from __future__ import annotations

import polars as pl
import pytest

from conftest import empty_frame
from pappascout.constants import ROUND_TYPES, SIDES, UNIT_STATUSES
from pappascout.domain.schemas import (
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    ARMED_COLUMN,
    ARMORED_COLUMN,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    SCHEMAS,
    TICKS,
    validate,
)
from pappascout.errors import PappascoutError, SchemaError


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_valid_frame_passes_through_unchanged(name: str) -> None:
    """The schema is in order -> validate returns the DataFrame unchanged."""
    schema = SCHEMAS[name]
    df = empty_frame(schema)
    result = validate(df, schema, name)
    assert result is df
    assert dict(result.schema) == dict(pl.DataFrame(schema=dict(schema)).schema)


def test_missing_column_names_column_and_expected_type() -> None:
    """A column is missing -> SchemaError names the missing column and its
    type."""
    df = empty_frame(ROUNDS).drop("round_no")
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    message = str(exc.value)
    assert "round_no" in message
    assert "Int32" in message
    assert "missing" in message


def test_extra_column_names_the_extra_column() -> None:
    """An extra column -> SchemaError names the extra column."""
    df = empty_frame(ROUNDS).with_columns(pl.lit(1).alias("not_a_column"))
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    message = str(exc.value)
    assert "not_a_column" in message
    assert "extra column" in message


def test_wrong_dtype_names_column_expected_and_actual() -> None:
    """A wrong type -> SchemaError names the column, the expected and the
    actual type."""
    df = empty_frame(ROUNDS).with_columns(pl.col("round_no").cast(pl.Utf8))
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    message = str(exc.value)
    assert "round_no" in message
    assert "Int32" in message
    assert "String" in message or "Utf8" in message


def test_missing_is_reported_before_wrong_type() -> None:
    """The missing column is reported even when another has a wrong type."""
    df = empty_frame(ROUNDS).drop("round_no").with_columns(
        pl.col("won").cast(pl.Int32)
    )
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    assert "round_no" in str(exc.value)


def test_advice_replaces_the_developer_instruction() -> None:
    """The caller can replace the instruction, but not the diagnosis.

    The default instruction speaks to a developer, because most often it is
    code that breaks the contract. A table read from the archive is a
    different situation: it was broken by an earlier version of the program
    itself, and the user does not fix it by editing schemas.py.
    """
    df = empty_frame(ROUNDS).drop("round_no")

    with pytest.raises(SchemaError) as default:
        validate(df, ROUNDS, "rounds")
    assert "domain/schemas.py" in str(default.value)

    with pytest.raises(SchemaError) as replaced:
        validate(df, ROUNDS, "rounds", advice="Run the parse again.")
    message = str(replaced.value)
    assert "round_no" in message  # the diagnosis survives
    assert message.endswith("Run the parse again.")
    assert "domain/schemas.py" not in message


def test_column_order_does_not_matter() -> None:
    """The order of the columns is not part of the contract."""
    df = empty_frame(ROUNDS)
    reversed_frame = df.select(reversed(df.columns))
    assert validate(reversed_frame, ROUNDS, "rounds") is reversed_frame


def test_schema_error_is_a_pappascout_error() -> None:
    """The CLI can catch every error of the tool with one except clause."""
    assert issubclass(SchemaError, PappascoutError)


def test_rounds_has_one_row_per_team_columns() -> None:
    """The rounds table is long: a row always has the team's side and
    lineup."""
    assert ROUNDS["side"] == pl.Enum(list(SIDES))
    assert "lineup_key" in ROUNDS
    assert ROUNDS["status"] == pl.Enum(list(UNIT_STATUSES))


def test_rounds_carries_the_armed_player_count() -> None:
    """The equipment counter belongs to the rounds table's contract as an
    integer.

    The **order** of the columns is not checked: this module's own
    ``test_column_order_does_not_matter`` and ``validate``'s docstring say
    that the order is not part of the contract -- requiring an order here
    would contradict them.
    """
    assert ARMED_COLUMN in ROUNDS
    assert ROUNDS[ARMED_COLUMN] == pl.Int32


def test_rounds_carries_the_armored_player_count() -> None:
    """The armour counter is a column of its own beside the equipment
    counter."""
    assert ARMORED_COLUMN in ROUNDS
    assert ROUNDS[ARMORED_COLUMN] == pl.Int32


def test_the_two_player_counters_are_separate_columns() -> None:
    """Two counters, two names, two columns -- not one generalisation.

    They answer different questions: armed is the half-buy's calibrated
    condition A, armoured is "how many had armour". The same name or the same
    column would hide the difference, which is at its largest on a pistol
    round.
    """
    assert ARMED_COLUMN != ARMORED_COLUMN
    assert {ARMED_COLUMN, ARMORED_COLUMN} <= set(ROUNDS)


def test_the_armored_count_is_not_a_classify_input() -> None:
    """The armour counter is an observation, not an input of the
    classification.

    The boundary is the condition of the whole of Story 2.8: the half-buy's
    condition A stays in ``players_armed_buy_end``, and the new column must
    not affect a single round type. If it ended up in ``CLASSIFY_COLUMNS``,
    nothing would stop a rule from relying on it unnoticed.
    """
    from pappascout.domain.economy import CLASSIFY_COLUMNS

    assert ARMORED_COLUMN not in CLASSIFY_COLUMNS


def test_rounds_carries_the_per_player_money_distribution() -> None:
    """The money distribution is a list of integers, one per readable player.

    The team sum ``money_buy_end`` is still in place: it is a different
    question. The distribution answers the one the sum cannot -- how many
    individual players can buy on the next round.
    """
    assert MONEY_DISTRIBUTION_COLUMN in ROUNDS
    assert ROUNDS[MONEY_DISTRIBUTION_COLUMN] == pl.List(pl.Int32)
    assert ROUNDS["money_buy_end"] == pl.Int32


def test_the_half_buy_observations_are_classify_inputs() -> None:
    """The half-buy's two conditions are read from the rounds table, not from
    the team sum.

    Story 1.5 and 1.6 produced the equipment counter as an observation without
    a rule; Story 1.9 fixed the moment of measurement; Story 1.10 put both to
    use. If either column vanished from ``CLASSIFY_COLUMNS``, the rule would
    fall back to the mean -- and that was exactly the defect.
    """
    from pappascout.domain.economy import CLASSIFY_COLUMNS

    assert ARMED_COLUMN in CLASSIFY_COLUMNS
    assert MONEY_DISTRIBUTION_COLUMN in CLASSIFY_COLUMNS


def test_money_and_equip_columns_are_integer_dollars() -> None:
    """The convention: *money* and *equip* are integer dollars.

    The money distribution is a list of the same type: one integer per player.
    The same convention, a different shape -- and the shape is the whole
    reason the column exists.
    """
    for schema in SCHEMAS.values():
        for name, dtype in schema.items():
            if "money" not in name and "equip" not in name:
                continue
            expected = (
                pl.List(pl.Int32)
                if name == MONEY_DISTRIBUTION_COLUMN
                else pl.Int32
            )
            assert dtype == expected, name


def test_second_columns_are_float() -> None:
    """The convention: *_s is seconds as a float."""
    for schema in SCHEMAS.values():
        for name, dtype in schema.items():
            if name.endswith("_s"):
                assert dtype == pl.Float64, name


def test_coordinates_are_float32() -> None:
    """The convention: the coordinates x, y, z are float32.

    In the deaths table there are two sets of coordinates -- the victim's and
    the attacker's -- and **both** have to be checked. Checking one set would
    leave the other free to drift to Float64, and the files would grow without
    a single test noticing.
    """
    for schema in (TICKS, EVENTS):
        for axis in ("x", "y", "z"):
            assert schema[axis] == pl.Float32
    for prefix in ("victim", "attacker"):
        for axis in ("x", "y", "z"):
            assert DEATHS[f"{prefix}_{axis}"] == pl.Float32


def test_round_type_enum_matches_shared_constant() -> None:
    """The round type is the same in the code, in Parquet, in the settings and
    in the report."""
    expected = pl.Enum(list(ROUND_TYPES))
    assert CLASSIFIED["round_type"] == expected
    assert CLASSIFIED["opp_round_type"] == expected


def test_classified_keeps_decision_inputs() -> None:
    """Every classified row carries the justification and the decision's
    inputs."""
    assert CLASSIFIED["reason"] == pl.Utf8
    fields = {field.name for field in CLASSIFIED["inputs"].fields}
    assert "equip_buy_end" in fields
    assert "money_buy_end" in fields
    assert "full_equip_min" in fields


# --- EVENTS: the trajectory's id ---------------------------------------------


def test_events_carries_a_trajectory_id_of_its_own() -> None:
    """The contract holds a column that identifies a trajectory.

    Without it the table has not one column that would tell two trajectories
    carrying the same entity id apart.
    """
    assert EVENTS["grenade_no"] == pl.Int32


def test_events_keeps_the_games_own_entity_id_too() -> None:
    """The game's id is the only link back into the demo, so it survives.

    It does not identify a grenade -- the game recycles it within a round too
    -- but without it the grenade can no longer be looked up in a viewer. Two
    separate columns rather than one replaced: the observation and the derived
    value are kept apart.
    """
    assert EVENTS["grenade_entity_id"] == pl.Int32
    assert "grenade_no" in EVENTS
    assert "grenade_entity_id" in EVENTS


def events_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    """An events table matching the contract; unnamed columns are ``null``.

    The table is built from ``EVENTS``'s own columns and types, so the test
    fails at once if a column vanishes from the contract. A hand-written frame
    would pass even then -- it brings nothing from the production code.
    """
    return pl.DataFrame(
        {name: [row.get(name) for row in rows] for name in EVENTS},
        schema=dict(EVENTS),
    )


def test_the_trajectory_id_makes_the_utility_join_safe() -> None:
    """The acceptance criterion: a join on the new id does not multiply rows.

    The data is three trajectories with the same entity id on the same round,
    as in ``inferno_vs_ryhmarama`` on round 11. The join is made from the
    table to itself on the key, because that is precisely the claim: a row
    fetched by the key is one row.
    """
    events = events_frame(
        [
            {
                "map_demo_id": "m1-0",
                "round_no": 11,
                "grenade_no": number,
                "grenade_entity_id": 564,
                "event_kind": "grenade_thrown",
            }
            for number in (40, 41, 42)
        ]
    )

    new_key = ["map_demo_id", "grenade_no", "event_kind"]
    assert events.select(new_key).is_unique().all()
    assert events.join(events.select(new_key), on=new_key, how="inner").height == 3

    # The old key does not tell the rows apart at all: the same join multiplies
    # three rows into nine.
    old_key = ["map_demo_id", "round_no", "grenade_entity_id", "event_kind"]
    assert not events.select(old_key).is_unique().any()
    assert events.join(events.select(old_key), on=old_key, how="inner").height == 9


def test_the_trajectory_id_is_unique_across_demos_only_with_map_demo_id() -> None:
    """The number runs inside a demo, so the key across demos is the pair.

    ``aggregate`` reads dozens of demos into one frame. ``grenade_no`` alone
    would then collide between the grenades of two demos -- the same defect as
    ``round_no`` without ``map_demo_id``.
    """
    events = events_frame(
        [
            {
                "map_demo_id": demo,
                "round_no": 1,
                "grenade_no": 40,
                "grenade_entity_id": 564,
                "event_kind": "grenade_thrown",
            }
            for demo in ("m1-0", "m2-0")
        ]
    )

    assert events.select("map_demo_id", "grenade_no", "event_kind").is_unique().all()
    assert not events.select("grenade_no", "event_kind").is_unique().any()


# --- map_demo_id: the aggregation's join key ---------------------------------


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_table_carries_map_demo_id(name: str) -> None:
    """The join (map_demo_id, round_no) needs the key on both sides.

    round_no alone is not enough: aggregate reads dozens of demos into one
    frame, and round 5 is a different round on a different map.
    """
    assert SCHEMAS[name]["map_demo_id"] == pl.Utf8


def test_classified_joins_to_ticks_on_map_demo_id_and_round_no() -> None:
    """The join works in practice and does not mix the rounds of two demos."""
    classified = pl.DataFrame(
        {
            "map_demo_id": ["m1-0", "m1-0", "m2-0"],
            "round_no": [1, 2, 1],
            "round_type": ["pistol", "eco", "pistol"],
        }
    )
    ticks = pl.DataFrame(
        {
            "map_demo_id": ["m1-0", "m1-0", "m2-0", "m2-0"],
            "round_no": [1, 2, 1, 1],
            "area": ["Ramp", "Heaven", "Lobby", "Main"],
        }
    )

    joined = ticks.join(classified, on=["map_demo_id", "round_no"], how="inner")
    assert joined.height == 4
    # m2-0's round 1 is a pistol round, but not the same row as m1-0's round 1.
    m2 = joined.filter(pl.col("map_demo_id") == "m2-0")
    assert sorted(m2["area"].to_list()) == ["Lobby", "Main"]
    assert set(m2["round_type"].to_list()) == {"pistol"}

    # Without map_demo_id the same join would produce rows that cross over.
    wrong_join = ticks.drop("map_demo_id").join(
        classified.drop("map_demo_id"), on="round_no", how="inner"
    )
    assert wrong_join.height > joined.height


def test_lineups_is_identity_not_a_round_observation() -> None:
    """The name is a property of the map, not of a round (Story 2.6).

    A round number in the table would mean that the name can change from
    round to round,
    and ``parse`` would drop the knife round's rows -- that is, a player who
    played the map. The key is (lineup, player) and not (round, player).
    """
    assert "round_no" not in LINEUPS
    assert "round_raw" not in LINEUPS
    assert "side" not in LINEUPS
    assert set(LINEUPS) == {
        "map_demo_id",
        "lineup_key",
        "player_id",
        "player_name",
        "clan_name",
    }


def test_the_name_never_lands_in_the_ticks_table() -> None:
    """The name is not a per-round observation and must not repeat over tens
    of thousands of rows."""
    for column in ("player_name", "clan_name", "name", "team_clan_name"):
        assert column not in TICKS
        assert column not in ROUNDS
        assert column not in EVENTS


def test_the_roster_keeps_the_steamid_beside_the_name() -> None:
    """The name is for readability; the id is the only traceable value."""
    assert LINEUPS["player_id"] == pl.Utf8
    assert LINEUPS["player_name"] == pl.Utf8
    assert LINEUPS["clan_name"] == pl.Utf8


def test_map_demo_id_is_first_column_in_parse_tables() -> None:
    """The join key first makes the table easier to read by hand."""
    for schema in (ROUNDS, TICKS, EVENTS, LINEUPS, DEATHS, CLASSIFIED):
        assert next(iter(schema)) == "map_demo_id"


# --- DEATHS: two actors, both as observations --------------------------------


def test_a_death_carries_both_actors_with_their_own_place() -> None:
    """A death has two actors, and the place of both matters.

    This is exactly the reason for a table of its own: ``EVENTS``'s convention
    is one actor and one place per row. If either half vanished from the
    contract, the table would fall back to the one-actor shape and "the enemy
    came through secret from the yard" would no longer be readable anywhere.
    """
    for prefix in ("victim", "attacker"):
        assert DEATHS[f"{prefix}_id"] == pl.Utf8
        assert DEATHS[f"{prefix}_lineup_key"] == pl.Utf8
        assert DEATHS[f"{prefix}_side"] == pl.Enum(list(SIDES))
        assert DEATHS[f"{prefix}_area"] == pl.Utf8


def test_the_death_areas_are_observations_not_derived() -> None:
    """The area comes from the same event, so there are no fields of a derived
    value.

    ``area_source`` and ``snap_distance`` exist for the grenade's
    approximation. In the deaths table they would claim that the area is an
    estimate -- and the report would mark an observation as an estimate, or
    the other way round.
    """
    assert "area_source" not in DEATHS
    assert "snap_distance" not in DEATHS


def test_deaths_carry_no_derived_concepts() -> None:
    """A trade, an entry and a duel win are interpretation, not observations.

    The division of labour is: observation from the machine, interpretation
    from the human. A column named ``trade`` would make interpretation the
    archive's truth.
    """
    for name in ("trade", "is_trade", "entry", "duel", "duel_won", "assister_id"):
        assert name not in DEATHS


def test_the_death_table_joins_to_the_others_on_the_same_key() -> None:
    """``(map_demo_id, round_no)`` is the same join key as in the other
    tables."""
    assert DEATHS["map_demo_id"] == pl.Utf8
    assert DEATHS["round_no"] == pl.Int32
    assert DEATHS["round_raw"] == pl.Int32
