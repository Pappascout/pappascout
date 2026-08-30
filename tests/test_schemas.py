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
    message = str(exc.value)
    assert "round_no" in message
    assert "Int32" in message
    assert "puuttuu" in message


def test_extra_column_names_the_extra_column() -> None:
    """Ylimääräinen sarake -> SchemaError nimeää ylimääräisen sarakkeen."""
    df = empty_frame(ROUNDS).with_columns(pl.lit(1).alias("kaikki_rahat"))
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    message = str(exc.value)
    assert "kaikki_rahat" in message
    assert "ylimääräinen" in message


def test_wrong_dtype_names_column_expected_and_actual() -> None:
    """Väärä tyyppi -> SchemaError nimeää sarakkeen, odotetun ja saadun tyypin."""
    df = empty_frame(ROUNDS).with_columns(pl.col("round_no").cast(pl.Utf8))
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    message = str(exc.value)
    assert "round_no" in message
    assert "Int32" in message
    assert "String" in message or "Utf8" in message


def test_missing_is_reported_before_wrong_type() -> None:
    """Puuttuva sarake raportoidaan, vaikka toisessa olisi myös väärä tyyppi."""
    df = empty_frame(ROUNDS).drop("round_no").with_columns(
        pl.col("won").cast(pl.Int32)
    )
    with pytest.raises(SchemaError) as exc:
        validate(df, ROUNDS, "rounds")
    assert "round_no" in str(exc.value)


def test_advice_replaces_the_developer_instruction() -> None:
    """Kutsuja voi vaihtaa toimintaohjeen, muttei diagnoosia.

    Oletusohje puhuu kehittäjälle, koska sopimusta rikkoo useimmiten koodi.
    Arkistosta luettu taulu on eri tilanne: sen on rikkonut ohjelman oma
    aiempi versio, eikä käyttäjä korjaa sitä muokkaamalla schemas.py:tä.
    """
    df = empty_frame(ROUNDS).drop("round_no")

    with pytest.raises(SchemaError) as default:
        validate(df, ROUNDS, "rounds")
    assert "domain/schemas.py" in str(default.value)

    with pytest.raises(SchemaError) as replaced:
        validate(df, ROUNDS, "rounds", advice="Aja parsinta uudelleen.")
    message = str(replaced.value)
    assert "round_no" in message  # diagnoosi säilyy
    assert message.endswith("Aja parsinta uudelleen.")
    assert "domain/schemas.py" not in message


def test_column_order_does_not_matter() -> None:
    """Sarakkeiden järjestys ei ole osa sopimusta."""
    df = empty_frame(ROUNDS)
    reversed_frame = df.select(reversed(df.columns))
    assert validate(reversed_frame, ROUNDS, "rounds") is reversed_frame


def test_schema_error_is_a_pappascout_error() -> None:
    """CLI voi napata kaikki työkalun virheet yhdellä except-lauseella."""
    assert issubclass(SchemaError, PappascoutError)


def test_rounds_has_one_row_per_team_columns() -> None:
    """Kierrostaulu on pitkä: rivillä on aina joukkueen puoli ja kokoonpano."""
    assert ROUNDS["side"] == pl.Enum(list(SIDES))
    assert "lineup_key" in ROUNDS
    assert ROUNDS["status"] == pl.Enum(list(UNIT_STATUSES))


def test_rounds_carries_the_armed_player_count() -> None:
    """Kalustolaskuri kuuluu kierrostaulun sopimukseen kokonaislukuna.

    Sarakkeiden **järjestystä ei tarkisteta**: tämän moduulin oma
    ``test_column_order_does_not_matter`` ja ``validate``in
    docstring sanovat, ettei järjestys ole osa sopimusta -- järjestysvaatimus
    tässä olisi ristiriita niiden kanssa.
    """
    assert ARMED_COLUMN in ROUNDS
    assert ROUNDS[ARMED_COLUMN] == pl.Int32


def test_rounds_carries_the_armored_player_count() -> None:
    """Panssarilaskuri on oma sarakkeensa kalustolaskurin rinnalla."""
    assert ARMORED_COLUMN in ROUNDS
    assert ROUNDS[ARMORED_COLUMN] == pl.Int32


def test_the_two_player_counters_are_separate_columns() -> None:
    """Kaksi laskuria, kaksi nimeä, kaksi saraketta -- ei yhtä yleistystä.

    Ne vastaavat eri kysymyksiin: aseistettu on puolioston kalibroitu ehto A,
    panssaroitu on "monellako oli panssari". Sama nimi tai sama sarake
    peittäisi eron, joka on pistoolikierroksella suurimmillaan.
    """
    assert ARMED_COLUMN != ARMORED_COLUMN
    assert {ARMED_COLUMN, ARMORED_COLUMN} <= set(ROUNDS)


def test_the_armored_count_is_not_a_classify_input() -> None:
    """Panssarilaskuri on havainto, ei luokittelun syöte.

    Rajaus on koko Story 2.8:n ehto: puolioston ehto A pysyy
    ``players_armed_buy_end``issä, ja uusi sarake ei saa vaikuttaa yhteenkään
    kierrostyyppiin. Jos se päätyisi ``CLASSIFY_COLUMNS``iin, mikään ei estäisi
    sääntöä nojaamasta siihen huomaamatta.
    """
    from pappascout.domain.economy import CLASSIFY_COLUMNS

    assert ARMORED_COLUMN not in CLASSIFY_COLUMNS


def test_rounds_carries_the_per_player_money_distribution() -> None:
    """Rahajakauma on lista kokonaislukuja, yksi per luettavissa ollut pelaaja.

    Joukkuesumma ``money_buy_end`` on yhä paikallaan: se on eri kysymys.
    Jakauma vastaa siihen, mihin summa ei pysty -- moniko yksittäinen pelaaja
    pystyy ostamaan seuraavalla kierroksella.
    """
    assert MONEY_DISTRIBUTION_COLUMN in ROUNDS
    assert ROUNDS[MONEY_DISTRIBUTION_COLUMN] == pl.List(pl.Int32)
    assert ROUNDS["money_buy_end"] == pl.Int32


def test_the_half_buy_observations_are_classify_inputs() -> None:
    """Puolioston kaksi ehtoa luetaan kierrostaulusta, eivät joukkuesummasta.

    Story 1.5 ja 1.6 tuottivat kalustolaskurin havaintona ilman sääntöä;
    Story 1.9 korjasi mittaushetken; Story 1.10 otti molemmat käyttöön. Jos
    kumpi tahansa sarake katoaisi ``CLASSIFY_COLUMNS``ista, sääntö putoaisi
    takaisin keskiarvoon -- ja juuri se oli vika.
    """
    from pappascout.domain.economy import CLASSIFY_COLUMNS

    assert ARMED_COLUMN in CLASSIFY_COLUMNS
    assert MONEY_DISTRIBUTION_COLUMN in CLASSIFY_COLUMNS


def test_money_and_equip_columns_are_integer_dollars() -> None:
    """Konventio: *money* ja *equip* ovat kokonaislukuja dollareita.

    Rahajakauma on lista samaa tyyppiä: yksi kokonaisluku per pelaaja. Sama
    konventio, eri muoto -- ja muoto on koko sarakkeen olemassaolon syy.
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
    """Konventio: *_s on sekunteja liukulukuna."""
    for schema in SCHEMAS.values():
        for name, dtype in schema.items():
            if name.endswith("_s"):
                assert dtype == pl.Float64, name


def test_coordinates_are_float32() -> None:
    """Konventio: koordinaatit x, y, z ovat float32.

    Kuolemataulussa koordinaatteja on kaksi joukkoa -- uhrin ja ampujan --
    ja **molemmat** on tarkistettava. Yhden joukon tarkistaminen jättäisi
    toisen ajautumaan Float64:ksi, ja tiedostot kasvaisivat ilman että
    yksikään testi huomaisi.
    """
    for schema in (TICKS, EVENTS):
        for axis in ("x", "y", "z"):
            assert schema[axis] == pl.Float32
    for prefix in ("victim", "attacker"):
        for axis in ("x", "y", "z"):
            assert DEATHS[f"{prefix}_{axis}"] == pl.Float32


def test_round_type_enum_matches_shared_constant() -> None:
    """Kierrostyyppi on sama koodissa, Parquetissa, asetuksissa ja raportissa."""
    expected = pl.Enum(list(ROUND_TYPES))
    assert CLASSIFIED["round_type"] == expected
    assert CLASSIFIED["opp_round_type"] == expected


def test_classified_keeps_decision_inputs() -> None:
    """Jokainen luokiteltu rivi kantaa perustelun ja päätöksen syötteet."""
    assert CLASSIFIED["reason"] == pl.Utf8
    fields = {field.name for field in CLASSIFIED["inputs"].fields}
    assert "equip_buy_end" in fields
    assert "money_buy_end" in fields
    assert "full_equip_min" in fields


# --- EVENTS: lentoradan tunniste ---------------------------------------------


def test_events_carries_a_trajectory_id_of_its_own() -> None:
    """Sopimuksessa on sarake, joka yksilöi lentoradan.

    Ilman sitä taulussa ei ole yhtään saraketta, joka erottaisi kaksi samaa
    entiteettitunnistetta kantavaa rataa toisistaan.
    """
    assert EVENTS["grenade_no"] == pl.Int32


def test_events_keeps_the_games_own_entity_id_too() -> None:
    """Pelin tunniste on ainoa side takaisin demoon, joten se säilyy.

    Se ei yksilöi kranaattia -- peli kierrättää sen myös kierroksen sisällä --
    mutta ilman sitä kranaattia ei voi enää etsiä katselimesta. Kaksi eri
    saraketta eikä yksi korvattu: havainto ja johdos pidetään erillään.
    """
    assert EVENTS["grenade_entity_id"] == pl.Int32
    assert "grenade_no" in EVENTS
    assert "grenade_entity_id" in EVENTS


def events_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    """Sopimuksen mukainen tapahtumataulu; nimeämättömät sarakkeet ``null``.

    Taulu rakennetaan ``EVENTS``in omista sarakkeista ja tyypeistä, joten
    testi kaatuu heti, jos sarake katoaa sopimuksesta. Käsin kirjoitettu
    kehys menisi läpi silloinkin -- se ei tuo tuotantokoodista mitään.
    """
    return pl.DataFrame(
        {name: [row.get(name) for row in rows] for name in EVENTS},
        schema=dict(EVENTS),
    )


def test_the_trajectory_id_makes_the_utility_join_safe() -> None:
    """Hyväksymiskriteeri: liitos uudella tunnisteella ei monista rivejä.

    Aineisto on kolme rataa samalla entiteettitunnisteella samalla
    kierroksella, kuten ``inferno_vs_ryhmarama`` kierroksella 11. Liitos
    tehdään taulusta itseensä avaimella, koska juuri se on väite: avaimella
    haettu rivi on yksi rivi.
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

    # Vanha avain ei erota rivejä toisistaan lainkaan: sama liitos monistaa
    # kolme riviä yhdeksäksi.
    old_key = ["map_demo_id", "round_no", "grenade_entity_id", "event_kind"]
    assert not events.select(old_key).is_unique().any()
    assert events.join(events.select(old_key), on=old_key, how="inner").height == 9


def test_the_trajectory_id_is_unique_across_demos_only_with_map_demo_id() -> None:
    """Numero juoksee demon sisällä, joten demojen välinen avain on pari.

    ``aggregate`` lukee kymmeniä demoja yhteen kehykseen. Pelkkä
    ``grenade_no`` osuisi silloin ristiin kahden demon kranaattien välillä --
    sama vika kuin ``round_no``lla ilman ``map_demo_id``:tä.
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

    joined = ticks.join(classified, on=["map_demo_id", "round_no"], how="inner")
    assert joined.height == 4
    # m2-0:n kierros 1 on pistooli, muttei sama rivi kuin m1-0:n kierros 1.
    m2 = joined.filter(pl.col("map_demo_id") == "m2-0")
    assert sorted(m2["area"].to_list()) == ["Lobby", "Main"]
    assert set(m2["round_type"].to_list()) == {"pistol"}

    # Ilman map_demo_idia sama liitos tuottaisi ristiin meneviä rivejä.
    wrong_join = ticks.drop("map_demo_id").join(
        classified.drop("map_demo_id"), on="round_no", how="inner"
    )
    assert wrong_join.height > joined.height


def test_lineups_is_identity_not_a_round_observation() -> None:
    """Nimi on kartan ominaisuus, ei kierroksen (Story 2.6).

    Kierrosnumero taulussa tarkoittaisi, että nimi voi vaihtua
    kierroksittain,
    ja ``parse`` pudottaisi puukkokierroksen rivit -- eli pelaajan, joka pelasi
    kartan. Avain on (kokoonpano, pelaaja) eikä (kierros, pelaaja).
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
    """Nimi ei ole kierroskohtainen havainto eikä saa toistua kymmenissä
    tuhansissa riveissä."""
    for column in ("player_name", "clan_name", "name", "team_clan_name"):
        assert column not in TICKS
        assert column not in ROUNDS
        assert column not in EVENTS


def test_the_roster_keeps_the_steamid_beside_the_name() -> None:
    """Nimi on luettavuutta varten; tunniste on ainoa jaljitettava arvo."""
    assert LINEUPS["player_id"] == pl.Utf8
    assert LINEUPS["player_name"] == pl.Utf8
    assert LINEUPS["clan_name"] == pl.Utf8


def test_map_demo_id_is_first_column_in_parse_tables() -> None:
    """Liitosavain ensimmaisena helpottaa taulun lukemista kasin."""
    for schema in (ROUNDS, TICKS, EVENTS, LINEUPS, DEATHS, CLASSIFIED):
        assert next(iter(schema)) == "map_demo_id"


# --- DEATHS: kaksi toimijaa, molemmat havaintoina ----------------------------


def test_a_death_carries_both_actors_with_their_own_place() -> None:
    """Kuolemalla on kaksi toimijaa, ja molempien paikka on merkityksellinen.

    Juuri tämä on syy omaan tauluun: ``EVENTS``in konventio on yksi toimija ja
    yksi paikka riviä kohden. Jos jompikumpi puolisko katoaisi sopimuksesta,
    taulu palaisi yhden toimijan muotoon eikä "Vihu meni secret pihalta"
    olisi enää luettavissa mistään.
    """
    for prefix in ("victim", "attacker"):
        assert DEATHS[f"{prefix}_id"] == pl.Utf8
        assert DEATHS[f"{prefix}_lineup_key"] == pl.Utf8
        assert DEATHS[f"{prefix}_side"] == pl.Enum(list(SIDES))
        assert DEATHS[f"{prefix}_area"] == pl.Utf8


def test_the_death_areas_are_observations_not_derived() -> None:
    """Alue tulee samalta tapahtumalta, joten johdoksen kenttiä ei ole.

    ``area_source`` ja ``snap_distance`` ovat olemassa kranaatin
    approksimaatiota varten. Kuolemataulussa ne väittäisivät, että alue on
    arvio -- ja raportti merkitsisi havainnon arvioksi tai päinvastoin.
    """
    assert "area_source" not in DEATHS
    assert "snap_distance" not in DEATHS


def test_deaths_carry_no_derived_concepts() -> None:
    """Trade, entry ja duel-voitto ovat tulkintaa, eivät havaintoja.

    Työnjako on: havainto koneelta, tulkinta ihmiseltä. Sarake nimeltä
    ``trade`` tekisi tulkinnasta arkiston totuuden.
    """
    for name in ("trade", "is_trade", "entry", "duel", "duel_won", "assister_id"):
        assert name not in DEATHS


def test_the_death_table_joins_to_the_others_on_the_same_key() -> None:
    """``(map_demo_id, round_no)`` on sama liitosavain kuin muissa tauluissa."""
    assert DEATHS["map_demo_id"] == pl.Utf8
    assert DEATHS["round_no"] == pl.Int32
    assert DEATHS["round_raw"] == pl.Int32
