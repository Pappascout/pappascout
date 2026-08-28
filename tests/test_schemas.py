"""Skeemavalidoinnin testit -- I/O-matriisin neljä ensimmäistä riviä.

``validate`` on tarkoituksella tiukka molempiin suuntiin: hiljaa tyhjäksi menevä
Polars-join on juuri se vika, jonka tämä sopimus estää.
"""

from __future__ import annotations

import polars as pl
import pytest

from conftest import empty_frame
from pappascout.constants import ROUND_TYPES, SIDES, UNIT_STATUSES
from pappascout.domain.schemas import (
    CLASSIFIED,
    EVENTS,
    ROUNDS,
    SCHEMAS,
    TICKS,
    validate,
)
from pappascout.errors import PappascoutError, SchemaError


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_valid_frame_passes_through_unchanged(name: str) -> None:
    """Skeema kunnossa -> validate palauttaa DataFramen muuttumattomana."""
    schema = SCHEMAS[name]
    df = empty_frame(schema)
    result = validate(df, schema, name)
    assert result is df
    assert dict(result.schema) == dict(pl.DataFrame(schema=dict(schema)).schema)


def test_missing_column_names_column_and_expected_type() -> None:
    """Sarake puuttuu -> SchemaError nimeää puuttuvan sarakkeen ja sen tyypin."""
    df = empty_frame(ROUNDS).drop("round_no")
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    viesti = str(exc.value)
    assert "round_no" in viesti
    assert "Int32" in viesti
    assert "puuttuu" in viesti


def test_extra_column_names_the_extra_column() -> None:
    """Ylimääräinen sarake -> SchemaError nimeää ylimääräisen sarakkeen."""
    df = empty_frame(ROUNDS).with_columns(pl.lit(1).alias("kaikki_rahat"))
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    viesti = str(exc.value)
    assert "kaikki_rahat" in viesti
    assert "ylimääräinen" in viesti


def test_wrong_dtype_names_column_expected_and_actual() -> None:
    """Väärä tyyppi -> SchemaError nimeää sarakkeen, odotetun ja saadun tyypin."""
    df = empty_frame(ROUNDS).with_columns(pl.col("round_no").cast(pl.Utf8))
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    viesti = str(exc.value)
    assert "round_no" in viesti
    assert "Int32" in viesti
    assert "String" in viesti or "Utf8" in viesti


def test_missing_is_reported_before_wrong_type() -> None:
    """Puuttuva sarake raportoidaan, vaikka toisessa olisi myös väärä tyyppi."""
    df = empty_frame(ROUNDS).drop("round_no").with_columns(
        pl.col("won").cast(pl.Int32)
    )
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    assert "round_no" in str(exc.value)


def test_column_order_does_not_matter() -> None:
    """Sarakkeiden järjestys ei ole osa sopimusta."""
    df = empty_frame(ROUNDS)
    kaannetty = df.select(reversed(df.columns))
    assert validate(kaannetty, ROUNDS, "rounds") is kaannetty


def test_schema_error_is_a_pappascout_error() -> None:
    """CLI voi napata kaikki työkalun virheet yhdellä except-lauseella."""
    assert issubclass(SchemaError, PappascoutError)


def test_rounds_has_one_row_per_team_columns() -> None:
    """Kierrostaulu on pitkä: rivillä on aina joukkueen puoli ja kokoonpano."""
    assert ROUNDS["side"] == pl.Enum(list(SIDES))
    assert "lineup_key" in ROUNDS
    assert ROUNDS["status"] == pl.Enum(list(UNIT_STATUSES))


def test_money_and_equip_columns_are_integer_dollars() -> None:
    """Konventio: *money* ja *equip* ovat kokonaislukuja dollareita."""
    for schema in SCHEMAS.values():
        for name, dtype in schema.items():
            if "money" in name or "equip" in name:
                assert dtype == pl.Int32, name


def test_second_columns_are_float() -> None:
    """Konventio: *_s on sekunteja liukulukuna."""
    for schema in SCHEMAS.values():
        for name, dtype in schema.items():
            if name.endswith("_s"):
                assert dtype == pl.Float64, name


def test_coordinates_are_float32() -> None:
    """Konventio: koordinaatit x, y, z ovat float32."""
    for schema in (TICKS, EVENTS):
        for axis in ("x", "y", "z"):
            assert schema[axis] == pl.Float32


def test_round_type_enum_matches_shared_constant() -> None:
    """Kierrostyyppi on sama koodissa, Parquetissa, asetuksissa ja raportissa."""
    expected = pl.Enum(list(ROUND_TYPES))
    assert CLASSIFIED["round_type"] == expected
    assert CLASSIFIED["opp_round_type"] == expected


def test_classified_keeps_decision_inputs() -> None:
    """Jokainen luokiteltu rivi kantaa perustelun ja päätöksen syötteet."""
    assert CLASSIFIED["reason"] == pl.Utf8
    kentat = {field.name for field in CLASSIFIED["inputs"].fields}
    assert "equip_freeze_end" in kentat
    assert "money_freeze_end" in kentat
    assert "full_equip_min" in kentat


# --- map_demo_id: aggregoinnin liitosavain -----------------------------------


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_every_table_carries_map_demo_id(name: str) -> None:
    """Liitos (map_demo_id, round_no) vaatii avaimen molemmilta puolilta.

    Pelkka round_no ei riita: aggregate lukee kymmenia demoja yhteen kehykseen,
    ja kierros 5 on eri kierros eri kartalla.
    """
    assert SCHEMAS[name]["map_demo_id"] == pl.Utf8


def test_classified_joins_to_ticks_on_map_demo_id_and_round_no() -> None:
    """Liitos toimii kaytannossa eika sekoita kahden demon kierroksia."""
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

    liitos = ticks.join(classified, on=["map_demo_id", "round_no"], how="inner")
    assert liitos.height == 4
    # m2-0:n kierros 1 on pistooli, muttei sama rivi kuin m1-0:n kierros 1.
    m2 = liitos.filter(pl.col("map_demo_id") == "m2-0")
    assert sorted(m2["area"].to_list()) == ["Lobby", "Main"]
    assert set(m2["round_type"].to_list()) == {"pistol"}

    # Ilman map_demo_idia sama liitos tuottaisi ristiin meneviä rivejä.
    vaarin = ticks.drop("map_demo_id").join(
        classified.drop("map_demo_id"), on="round_no", how="inner"
    )
    assert vaarin.height > liitos.height


def test_map_demo_id_is_first_column_in_parse_tables() -> None:
    """Liitosavain ensimmaisena helpottaa taulun lukemista kasin."""
    for schema in (ROUNDS, TICKS, EVENTS, CLASSIFIED):
        assert next(iter(schema)) == "map_demo_id"
