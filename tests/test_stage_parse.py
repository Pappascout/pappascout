"""``stages.parse`` -- vaiheen testit ilman demoja.

Vaihe näkee demon vain portin takaa (AD-8), joten sen koko logiikka -- taulujen
validointi, kierrosnumeron liittäminen näytepisteisiin ja tapahtumiin, atominen
kirjoitus, manifesti ja ohitus -- testataan feikillä, joka rakentaa kaikki
kolme taulua käsin. Yksikään näistä testeistä ei tarvitse demotiedostoa.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from conftest import has_temp_leftovers, settings_text
from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
    ParseDiagnostics,
)
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.constants import SAMPLE_KINDS, SIDES
from pappascout.domain.models import load_settings
from pappascout.domain.schemas import ARMED_COLUMN, EVENTS, ROUNDS, TICKS
from pappascout.errors import DemoUnavailable, PappascoutError, ParseError, SchemaError
from pappascout.stages import parse as parse_stage

MAP_DEMO_ID = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"

#: Portin sopimuksen tyypit: ``ROUNDS`` täydennettynä numerointisarakkeilla.
ADAPTER_SCHEMA: dict[str, object] = {
    name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
}

#: Näytepistetaulun tyypit portin takana: ``TICKS`` ilman ``map_demo_id``:tä.
TICKS_ADAPTER_SCHEMA: dict[str, object] = {
    name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS
}

#: Tapahtumataulun tyypit portin takana: ``EVENTS`` ilman ``map_demo_id``:tä.
EVENTS_ADAPTER_SCHEMA: dict[str, object] = {
    name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS
}

#: Näytepisteet, joilla feikki rakentaa tick-rivinsä.
SAMPLE_SECONDS = (6.0, 15.0)


# --- Feikki portin taakse ------------------------------------------------------


def build_rounds(
    played: int = 3,
    *,
    warmup: int = 1,
    without_anchor: tuple[int, ...] = (),
) -> pl.DataFrame:
    """Rakenna kierrostaulu käsin, kuten oikea adapteri sen palauttaisi.

    Voittaja on aina T ja syy ``ct_killed``, joka on CS2:n sääntöjen mukainen
    tapa T:n voitolle -- muuten ``check_win_reasons`` hylkäisi taulun.

    Args:
        pelatut: Pelattujen kierrosten määrä.
        warmup: Numeroimattomien kierrosten määrä alussa (puukkokierros ja
            warmup): niiden yhteispistemäärä ei kasva.
        ilman_ankkuria: Ne ``round_raw``-arvot, joilta freezetime-ankkuri
            puuttuu.
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
                    "money_freeze_end": None if no_anchor else 3000 + index,
                    "equip_freeze_end": None if no_anchor else 20000 + index,
                    "equip_round_start": None if no_anchor else 1000 + index,
                    "players_freeze_end": None if no_anchor else 5,
                    # Adapteri antaa laskurin valmiina; vaihe vain kuljettaa
                    # sen. Puolikohtainen ero tekee kuljetuksesta todettavan.
                    ARMED_COLUMN: None if no_anchor else 5 - index,
                    "survivors": index,
                    "survivors_equip_prev": 500,
                    "freeze_end_tick": None if no_anchor else 1000 * round_raw,
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
    """Näytepistetaulu ``build_rounds``-taulua vastaavana, kuten adapteri sen antaisi.

    Adapteri näytteistää **kaikki** ankkuroidut kierrosrajat, myös warmupin ja
    puukkokierroksen: se ei tunne numerointisääntöä. Vaiheen tehtävä on pudottaa
    ne, joten feikin on tuotettava ne mukaan.

    Args:
        rounds: Kierrostaulu, josta ``round_raw``, ``side`` ja ``lineup_key``
            luetaan -- avaimet eivät saa erota tauluissa.
        sample_seconds: Aikapisteet.
        first_contact_rounds: Ne ``round_raw``-arvot, joilta löytyi ensikontakti.
        lyhyet: ``round_raw -> kierroksen kesto sekunteina``. Näytepiste, joka
            ylittää keston, jätetään pois -- kuten oikeassa demossa.
    """
    duration = short_rounds or {}
    rows: list[dict[str, object]] = []
    for round_row in rounds.iter_rows(named=True):
        raw = round_row["round_raw"]
        if round_row["freeze_end_tick"] is None:
            continue  # ankkuriton kierros ei tuota näytepisteitä
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


#: Tunniste, jonka kaikki saman kierroksen kranaatit jakavat, kun
#: ``build_events(recycle_entity_ids=True)``. Peli tekee juuri näin.
RECYCLED_ENTITY_ID = 564


def build_events(
    rounds: pl.DataFrame,
    *,
    per_round: int = 2,
    unexploded: tuple[int, ...] = (),
    without_area: tuple[int, ...] = (),
    recycle_entity_ids: bool = False,
) -> pl.DataFrame:
    """Tapahtumataulu ``build_rounds``-taulua vastaavana, kuten adapteri sen antaisi.

    Adapteri tuottaa rivejä **kaikilta ankkuroiduilta** kierrosrajoilta, myös
    lämmittelystä ja puukkokierrokselta -- se ei tunne numerointisääntöä.

    Args:
        rounds: Kierrostaulu, josta ``round_raw``, ``side`` ja ``lineup_key``
            luetaan; avaimet eivät saa erota tauluissa.
        per_round: Montako kranaattia kumpikin joukkue heittää kierroksella.
        unexploded: Ne kranaattien järjestysnumerot, joilta räjähdysrivi
            puuttuu (1-pohjainen, sama luku kuin ``without_area``ssa).
        without_area: Ne kranaattien järjestysnumerot, joiden alue jäi
            tyhjäksi.
        recycle_entity_ids: Anna kaikille saman kierroksen kranaateille **sama**
            ``grenade_entity_id``, kuten peli oikeasti tekee
            (``inferno_vs_ryhmarama`` kierros 11). Vanha avain
            ``(round_no, grenade_entity_id)`` menee silloin päällekkäin, ja
            vain ``grenade_no`` erottaa radat.

    ``grenade_no`` ja ``grenade_entity_id`` saavat **eri arvot**: numerot
    alkavat 500:sta. Identtisillä arvoilla sarakkeiden menemistä ristiin ei
    voisi havaita, koska molemmat ovat ``Int32``.
    """
    rows: list[dict[str, object]] = []
    entity = 0
    number = 500
    for round_row in rounds.iter_rows(named=True):
        if round_row["freeze_end_tick"] is None:
            continue  # ankkuriton kierros ei tuota tapahtumia
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
                        # Heiton alue on havainto, räjähdyksen arvio -- kuten
                        # oikea adapteri ne tuottaa.
                        "area_source": (
                            None
                            if entity in without_area
                            else ("observed" if kind == "grenade_thrown" else "snapped")
                        ),
                        "snap_distance": (
                            None if kind == "grenade_thrown" else 120.0
                        ),
                    }
                )
    return pl.DataFrame(rows, schema=dict(EVENTS_ADAPTER_SCHEMA), orient="row")


class FakeParser:
    """Portin toteutus, joka ei koske demoparser2:een."""

    def __init__(
        self,
        frame: pl.DataFrame | None = None,
        error: Exception | None = None,
        ticks: pl.DataFrame | None = None,
        events: pl.DataFrame | None = None,
    ):
        self.frame = frame if frame is not None else build_rounds()
        self.ticks = ticks if ticks is not None else build_ticks(self.frame)
        self.events = events if events is not None else build_events(self.frame)
        self.error = error
        self.calls = 0
        self.seen_seconds: list[tuple[float, ...]] = []

    def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
        self.calls += 1
        self.seen_seconds.append(tuple(sample_seconds))
        if self.error is not None:
            raise self.error
        return DemoTables(rounds=self.frame, ticks=self.ticks, events=self.events)


# --- Kiinnikkeet ---------------------------------------------------------------


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    root = tmp_path / "arkisto"
    root.mkdir()
    return ArchivePaths(root=root)


@pytest.fixture
def demo(archive: ArchivePaths) -> Path:
    """Demon paikkamerkki: feikki ei lue sisältöä, mutta polun on oltava aito."""
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


# --- Onnistunut ajo ------------------------------------------------------------


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
    """``score_start`` ja ``score_end`` ovat portin sisäisiä työkaluja."""
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
    # round_raw säilyy demon omana numerona, joten ohitus näkyy aukkona.
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]


def test_round_without_a_freeze_anchor_stays_in_the_table(
    parse_settings, archive, demo
) -> None:
    """Puuttuva ankkuri ei kaada ajoa: kierros on mukana omalla tilallaan."""
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
    ]
    assert manifest.inputs[0].result_id == f"demo/{MAP_DEMO_ID}"


def test_demo_hash_is_read_from_meta_not_recomputed(
    parse_settings, archive, demo
) -> None:
    """233 MB:n sha256 jokaisella ajolla olisi hitaampi kuin itse parsinta."""
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


# --- Näytepistetaulu -----------------------------------------------------------


def test_writes_a_valid_ticks_table(parse_settings, archive, demo) -> None:
    """Hyväksymiskriteeri: ``ticks.parquet`` läpäisee ``validate(TICKS)``."""
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
    # 3 kierrosta x 2 joukkuetta x 2 näytepistettä x 5 pelaajaa.
    assert df.height == 60
    assert result.stats["tick_rows"] == 60
    assert result.stats["sample_points"] == 6  # kierros x hetki
    assert result.stats["sample_rounds"] == 3


def test_all_three_tables_are_listed_among_the_outputs(
    parse_settings, archive, demo
) -> None:
    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert [p.name for p in result.outputs] == [
        "rounds.parquet",
        "ticks.parquet",
        "events.parquet",
    ]


def test_ticks_get_the_round_number_from_the_rounds_table(
    parse_settings, archive, demo
) -> None:
    """Numeroinnin omistaa domain.rounds; vaihe vain liittää sen."""
    rounds = build_rounds(played=3, warmup=0)
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))

    assert df["round_no"].null_count() == 0
    assert sorted(df["round_no"].unique().to_list()) == [1, 2, 3]
    # round_raw säilyy demon omana numerona rinnalla.
    pairs = set(zip(df["round_raw"].to_list(), df["round_no"].to_list()))
    assert pairs == {(1, 1), (2, 2), (3, 3)}


def test_unnumbered_rounds_produce_no_tick_rows(parse_settings, archive, demo) -> None:
    """I/O-matriisi: warmup ja puukkokierros -> ei tick-rivejä.

    Adapteri näytteistää ne, koska se ei tunne numerointisääntöä; tämä testi
    lukitsee sen, että vaihe pudottaa ne samalla päätöksellä kuin
    kierrostaulusta.
    """
    rounds = build_rounds(played=2, warmup=3)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo
    )
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    # Numeroimattomat round_raw-arvot 1..3 eivät ole taulussa.
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]
    assert result.stats["skipped_rounds"] == 3


def test_a_round_without_an_anchor_has_no_tick_rows(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: ankkuriton kierros on rounds-taulussa mutta ei ticksissä."""
    rounds = build_rounds(3, warmup=0, without_anchor=(2,))
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=build_ticks(rounds)), demo)

    rounds_list = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    ticks = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    assert 2 in rounds_list["round_no"].to_list()
    assert sorted(ticks["round_no"].unique().to_list()) == [1, 3]


def test_a_short_round_keeps_only_the_points_it_reached(
    parse_settings, archive, demo
) -> None:
    """Hyväksymiskriteeri: ei näytepistettä kierroksen päättymisen jälkeen."""
    rounds = build_rounds(played=2, warmup=0)
    ticks = build_ticks(rounds, short_rounds={2: 10.0})  # round_raw 2 ratkesi 10 s
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
    """Liitos ``(map_demo_id, round_no)`` ei saa mennä ristiin joukkueissa."""
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
    """Näytepisteajat ovat asetus eivätkä koodia (AD-3)."""
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
    assert "näytepistetaulun" in str(exc.value)


def test_an_extra_ticks_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_ticks(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, ticks=broken), demo)
    assert "ylimaarainen" in str(exc.value)


def test_an_empty_ticks_table_with_rounds_is_refused(
    parse_settings, archive, demo
) -> None:
    """Kierroksia mutta ei yhtään näytepistettä on virhe, ei ok-tulos.

    Tyhjä asetelmataulu jäisi manifestin perusteella pysyvästi ohitetuksi, ja
    aggregointi raportoisi kartan ilman yhtään asetelmaa -- tasan se hiljainen
    tyhjyys, jonka koko sopimuksen on tarkoitus estää.
    """
    rounds = build_rounds(played=2, warmup=0)
    empty = pl.DataFrame(schema=dict(TICKS_ADAPTER_SCHEMA))
    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, ticks=empty), demo)
    assert "näytepistettä" in str(exc.value)

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
    """Puolikas tulos ei ole ajantasainen tulos."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "ticks").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_an_archive_parsed_by_an_older_version_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """Story 1.3:n arkisto ei saa jäädä ilman ``ticks.parquet``-taulua.

    ``ParseSettings`` ei muuttunut Story 2.1:ssä, joten ``params_hash`` on
    identtinen. Manifestin ``outputs_present()`` tarkistaa vain ne polut, jotka
    **levyllä oleva** manifesti nimeää -- ja vanha manifesti nimeää vain
    kierrostaulun. Ilman erillistä tarkistusta ajo ohitettaisiin, asetelmataulu
    ei syntyisi koskaan, ja käyttäjälle kerrottaisiin "Tulos on ajan tasalla".
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)

    # Kelaa arkisto Story 1.3:n muotoon: manifesti nimeää vain rounds-taulun
    # ja ticks-taulua ei ole.
    manifest_path = archive.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["outputs"] = [f"parsed/{MAP_DEMO_ID}/rounds.parquet"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    archive.parsed_table(MAP_DEMO_ID, "ticks").unlink()

    result = run_parse(parse_settings, archive, parser, demo)

    assert not result.skipped, "vanha arkisto olisi jäänyt ilman asetelmataulua"
    assert parser.calls == 2
    assert archive.parsed_table(MAP_DEMO_ID, "ticks").is_file()


def test_a_failed_second_write_never_looks_up_to_date(
    parse_settings, archive, demo, monkeypatch
) -> None:
    """Kolmen taulun kirjoitus on yksi tapahtuma.

    Jos ticks-kirjoitus kaatuu peräkkäisissä lohkoissa, arkistoon jäisi
    kierrostaulu ilman pariaan -- ja koska manifesti kirjoitettaisiin silti,
    seuraava ajo ohittaisi vaiheen ja kertoisi iloisesti kierrosmäärän.
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

    # Yksikään taulu ei jäänyt paikalleen, ja manifesti kertoo virheestä.
    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert not archive.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert not archive.parsed_table(MAP_DEMO_ID, "events").exists()
    assert not has_temp_leftovers(archive.root)
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"

    # Ja seuraava ajo ei ohita.
    result = run_parse(parse_settings, archive, FakeParser(build_rounds(5, warmup=0)), demo)
    assert not result.skipped
    assert result.stats["rounds"] == 5
    assert result.stats["sample_rounds"] == 5


def test_ticks_are_sorted_deterministically_by_kind_too(
    parse_settings, archive, demo
) -> None:
    """Ensikontakti voi osua tasan konfiguroidulle sekunnille.

    Ilman ``sample_kind``ia lajitteluavaimessa kahden rivin järjestys riippuisi
    syötejärjestyksestä, ja sama demo tuottaisi eri tavut eri ajoilla.
    """
    rounds = build_rounds(played=2, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(1, 2), contact_t_s=6.0)
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "ticks"))
    same_second = df.filter(pl.col("sample_t_s") == 6.0)
    assert set(same_second["sample_kind"].unique()) == {"time", "first_contact"}
    # sample_kind on Enum, joten Polars lajittelee sen luettelon
    # järjestyksessä (time, first_contact) eikä aakkosittain. Kumpi tahansa
    # kelpaa; olennaista on että järjestys on määrätty eikä satunnainen.
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
    """Yksi rikki mennyt taulu ei saa viedä toisen lukuja."""
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
    """Ohitettu ajo lukee luvut valmiista tauluista, ei parsi demoa."""
    rounds = build_rounds(played=3, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(2,))
    run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)

    result = run_parse(parse_settings, archive, FakeParser(rounds, ticks=ticks), demo)
    assert result.skipped
    assert result.stats["tick_rows"] == 70  # 60 aikapistettä + 10 ensikontaktia
    assert result.stats["first_contact_rounds"] == 1


# --- Tapahtumataulu ------------------------------------------------------------


def test_writes_a_valid_events_table(parse_settings, archive, demo) -> None:
    """Hyväksymiskriteeri: ``events.parquet`` läpäisee ``validate(EVENTS)``."""
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
    # 3 kierrosta x 2 joukkuetta x 2 kranaattia x 2 riviä.
    assert df.height == 24
    assert result.stats["event_rows"] == 24
    assert result.stats["utility_throws"] == 12
    assert result.stats["utility_detonations"] == 12
    assert result.stats["utility_rounds"] == 3


def test_every_grenade_has_at_most_one_throw_and_one_detonation(
    parse_settings, archive, demo
) -> None:
    """Hyväksymiskriteeri: pari on pari, ei kolmea riviä."""
    rounds = build_rounds(played=4, warmup=0)
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    counts = df.group_by("round_no", "grenade_entity_id", "event_kind").len()
    assert counts["len"].max() == 1


def test_the_trajectory_id_is_unique_in_the_written_table(
    parse_settings, archive, demo
) -> None:
    """Hyväksymiskriteeri: ``(grenade_no, event_kind)`` on yksikäsitteinen.

    Aineistossa **on** kierrätetty tunniste, joten vanha avain menee
    päällekkäin samassa taulussa. Ilman sitä testi menisi läpi myös silloin,
    kun ``grenade_no`` ei tee mitään.

    Väite koskee koko taulua, ei kierrosta: kierroskohtainen tunniste
    näyttäisi tässä yhtä hyvältä, mutta pettäisi heti kun aggregointi liittää
    monta kierrosta yhteen kehykseen.
    """
    rounds = build_rounds(played=4, warmup=0)
    events = build_events(rounds, recycle_entity_ids=True)
    run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    assert df["grenade_no"].null_count() == 0
    assert df.select("map_demo_id", "grenade_no", "event_kind").is_unique().all()

    # Vanha avain **ei** ole yksikäsitteinen tässä samassa taulussa.
    old_key = df.select("map_demo_id", "round_no", "grenade_entity_id", "event_kind")
    assert not old_key.is_unique().all()


def test_joining_utility_on_the_new_key_does_not_duplicate_rows(
    parse_settings, archive, demo
) -> None:
    """Hyväksymiskriteeri: liitos uudella tunnisteella ei monista rivejä.

    Liitos tehdään taulusta itseensä avaimella, koska juuri se on väite:
    avaimella haettu rivi on yksi rivi. Sama liitos vanhalla avaimella
    monistaa rivit, ja molemmat luvut tarkistetaan -- muuten testi menisi
    läpi myös taululla, jossa avainta ei ole lainkaan.
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
    """Vaihe ei numeroi rivejä uudelleen -- numero tulee adapterilta.

    Ilman tätä vaihe voisi antaa omat juoksevat numeronsa, ja jokainen muu
    uusi testi menisi silti läpi: tulos olisi yhä yksikäsitteinen, mutta se ei
    olisi enää sama tunniste kuin lentoradalla.
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
    # Numeroimattomien kierrosten rivit putoavat; jäljelle jääneet kantavat
    # adapterin numeron sellaisenaan, ja numero osoittaa samaan tapahtumaan.
    assert written
    assert set(written) < set(given)
    for key, t_s in written.items():
        assert given[key] == t_s

    # Ja numerot alkavat 500:sta kuten adapteri ne antoi -- vaihe ei
    # uudelleennumeroi nollasta.
    assert min(no for no, _ in written) >= 500


def test_a_duplicate_trajectory_id_is_refused(
    parse_settings, archive, demo
) -> None:
    """Sopimusrikko nostetaan virheenä eikä kirjoiteta arkistoon.

    ``validate`` tarkistaa sarakkeet ja tyypit muttei avainta, joten
    kaksoiskappale läpäisisi sen ja näkyisi vasta raportin luvuissa
    kaksinkertaisena savuna.
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
    """Tyhjä numero jättäisi rivin ilman sidettä pariinsa."""
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
    """Skeemamuutos mitätöi arkiston, vaikka manifestiin ei kosketa.

    Parametrihash lasketaan ``[parse]``-osiosta ja demoparser2:n versiosta
    (AD-3), eikä kumpikaan liiku, kun ``EVENTS`` saa uuden sarakkeen. Ilman
    skeematarkistusta vanha taulu jäisi hiljaa voimaan ja näyttäisi
    ajantasaiselta. Kolme muuta "vanha arkisto" -testiä eivät kata tätä: kaksi
    poistaa tiedoston ja kolmas ylikirjoittaa ``params_hash``in.
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
    # Manifesti oli koko ajan täsmäävä -- uudelleenajon laukaisi skeema.
    assert manifest_before != ""


def test_an_unexploded_grenade_has_no_invented_detonation(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: rata katkeaa -> vain ``grenade_thrown``."""
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
    """I/O-matriisi: heitto numeroimattomalla kierroksella -> ei rivejä."""
    rounds = build_rounds(played=2, warmup=3)
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]


def test_a_round_without_an_anchor_has_no_event_rows(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: ankkuriton kierros -> ei rivejä (``t_s`` ei määritelty)."""
    rounds = build_rounds(3, warmup=0, without_anchor=(2,))
    run_parse(parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    assert sorted(df["round_no"].unique().to_list()) == [1, 3]


def test_an_empty_events_table_is_a_valid_result(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: demo ilman utilityä -> tyhjä ``events.parquet``.

    Toisin kuin tyhjä kierros- tai näytepistetaulu, tämä ei ole virhe: demossa
    on aina pelattuja kierroksia, mutta utility voi aidosti puuttua. Virhe
    estäisi koko demon parsinnan tiedosta, joka on itsessään havainto.
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
    """I/O-matriisi: räjähdys kaukana kaikista -> ``area = null``, ei pudotusta."""
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds, without_area=(2, 4))
    result = run_parse(parse_settings, archive, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))
    without_area_rows = df.filter(pl.col("area").is_null())
    assert without_area_rows.height == 4  # kaksi kranaattia x kaksi riviä
    # Koordinaatit säilyvät, vaikka alue ei ratkennut.
    assert without_area_rows["x"].null_count() == 0
    assert without_area_rows["area_source"].null_count() == 4
    assert result.stats["utility_without_area"] == 4


def test_observed_and_snapped_areas_are_counted_separately(
    parse_settings, archive, demo
) -> None:
    """Havainto ja arvio ovat eri laatua olevaa tietoa eivätkä saa niputtua.

    Ilman erottelua raportti esittäisi räjähdyksen arvion yhtä varmana kuin
    heittäjän oman alueen.
    """
    rounds = build_rounds(played=2, warmup=0)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo
    )
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "events"))

    throws = df.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = df.filter(pl.col("event_kind") == "grenade_detonate")
    assert throws["area_source"].unique().to_list() == ["observed"]
    assert detonations["area_source"].unique().to_list() == ["snapped"]
    # Napsautusetäisyys on vain arviolla -- havainto ei ole minkään päässä.
    assert throws["snap_distance"].null_count() == throws.height
    assert detonations["snap_distance"].null_count() == 0
    assert result.stats["utility_area_observed"] == throws.height
    assert result.stats["utility_area_snapped"] == detonations.height


def test_utility_on_unnumbered_rounds_is_counted_not_just_dropped(
    parse_settings, archive, demo
) -> None:
    """Kolme muuta pudotussyytä raportoidaan -- tämä ei saa olla poikkeus."""
    rounds = build_rounds(played=2, warmup=3)
    result = run_parse(
        parse_settings, archive, FakeParser(rounds, events=build_events(rounds)), demo
    )
    # 3 numeroimatonta kierrosta x 2 joukkuetta x 2 kranaattia = 12 heittoa.
    assert result.stats["utility_unnumbered_rounds"] == 12


def test_lineup_keys_join_from_events_to_rounds(
    parse_settings, archive, demo
) -> None:
    """Heittäjän joukkue on sama kuin kierrostaulussa; ei ristiinkytkentää."""
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
    """Saman kranaatin heitto tulee aina ennen sen räjähdystä."""
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
    assert "tapahtumataulun" in str(exc.value)


def test_a_missing_events_table_forces_a_reparse(
    parse_settings, archive, demo
) -> None:
    """Puolikas tulos ei ole ajantasainen tulos."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "events").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_an_archive_parsed_before_utility_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """Story 2.1:n arkisto ei saa jäädä ilman ``events.parquet``-taulua.

    Sama ansa kuin Story 2.1:ssä: ``ParseSettings`` muuttui vain
    ``area_snap_units``-kentän oletuksella, ja jos se on sama, ``params_hash``
    on identtinen. Manifestin ``outputs_present()`` tarkistaa vain ne polut,
    jotka **levyllä oleva** manifesti nimeää.
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

    assert not result.skipped, "vanha arkisto olisi jäänyt ilman utility-taulua"
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


# --- Ohitus --------------------------------------------------------------------


def test_second_run_is_skipped(parse_settings, archive, demo) -> None:
    parser = FakeParser(build_rounds(played=21))
    run_parse(parse_settings, archive, parser, demo)
    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    before = table.stat().st_mtime_ns

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert parser.calls == 1, "demoa ei saa parsia uudelleen"
    assert table.stat().st_mtime_ns == before, "tiedostoa ei saa kirjoittaa uudelleen"
    assert result.stats["rounds"] == 21
    assert "ajan tasalla" in (result.reason or "")


def test_force_overrides_a_matching_manifest(parse_settings, archive, demo) -> None:
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    result = run_parse(parse_settings, archive, parser, demo, force=True)

    assert not result.skipped
    assert parser.calls == 2


def test_changed_demo_bytes_trigger_a_reparse(parse_settings, archive, demo) -> None:
    """Pelkkä manifesti ei riitä: vanhentunut tulos ei saa jäädä pysyvästi."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    assert run_parse(parse_settings, archive, parser, demo).skipped

    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 2048)  # eri sisältö ja eri koko

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_threshold_change_does_not_trigger_a_reparse(
    tmp_path: Path, archive, demo
) -> None:
    """AD-3: kynnysten säätö ei saa invalidoida parsintaa."""
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
    """Aseluokittelun muutos mitätöi arkiston, vaikka asetukset eivät muutu.

    Luokittelu on koodia eikä asetus, joten pelkkä ``[parse]``-osion hash
    jättäisi sen muutoksen näkymättömäksi: taulu olisi laskettu vanhalla
    aseluettelolla, manifesti täsmäisi ja arkistoon jäisi hiljaa vanhentunut
    laskuri. Vaihtoehto olisi käsin nostettava versionumero -- se toimii vain
    jos kukaan ei unohda.
    """
    before = parse_stage._params_hash(parse_settings)
    monkeypatch.setattr(
        parse_stage, "weapon_classification_digest", lambda: "toinen-tiiviste"
    )
    assert parse_stage._params_hash(parse_settings) != before


def test_params_hash_still_covers_the_parse_section(
    tmp_path: Path, archive
) -> None:
    """Hash lasketaan myös koko ``ParseSettings``-osiosta -- todettuna.

    ``_params_hash`` dumppaa osion sellaisenaan, joten uusi kenttä *pitäisi*
    tulla hashiin automaattisesti. Juuri siksi se tarkistetaan: hiljainen
    poikkeus (esim. ``exclude``-lista) jäisi muuten huomaamatta, ja
    luokittelun tiivisteen lisääminen dictiin on juuri sellainen kohta,
    jossa osio olisi voinut jäädä pois.
    """
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root,
            **{"area_snap_units = 500": "area_snap_units = 501"},
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
    """Asetusosio ja tiiviste ovat eri tasoilla, eivät sisaruksina.

    Sisarusavaimena samanniminen ``[parse]``-asetus voisi peittää tiivisteen,
    ja sitä pitäisi torjua vartijalla, jota mikään ei voi laukaista.
    Kaksitasoinen rakenne tekee törmäyksen mahdottomaksi, ja tämä toteaa
    että molemmat puolet ovat oikeasti hashissa: kummankin muutos riittää.
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
    """Arkiston vanha ``rounds.parquet`` ilman uutta saraketta ei kaada ajoa.

    Vanhan koodin kirjoittama manifesti on hashattu ilman kalustokynnystä,
    joten ohitusehto ei täyty ja demo parsitaan uudelleen. Skeemavirhe johtaa
    siis **ajoon**, ei poikkeukseen -- eikä vanha taulu jää hiljaa voimaan.
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
    """Laskuri kulkee adapterilta levylle asti muuttumattomana."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=3)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df[ARMED_COLUMN].dtype == pl.Int32
    assert df.filter(pl.col("side") == "T")[ARMED_COLUMN].to_list() == [5, 5, 5]
    assert df.filter(pl.col("side") == "CT")[ARMED_COLUMN].to_list() == [4, 4, 4]


def test_run_reports_the_armed_distribution(parse_settings, archive, demo) -> None:
    """``run()`` palauttaa ne avaimet, joita tuloste lukee.

    Ilman tätä tuottaja ja kuluttaja testataan vain erikseen: tilastofunktio
    käsin rakennettua taulua vasten ja tuloste käsin kirjoitettua dictiä
    vasten. Kun välistä poistettiin ``stats.update(_armed_stats(df))``, 124
    testiä meni läpi ja "Aseistettuja"-rivi vain katosi tulosteesta.

    ``build_rounds`` antaa T:lle 5 ja CT:lle 4 joka kierroksella, joten
    jakauma on tarkalleen tiedossa.
    """
    result = run_parse(
        parse_settings, archive, FakeParser(build_rounds(played=3)), demo
    )

    assert result.stats["armed_distribution"] == {4: 3, 5: 3}
    assert result.stats["armed_missing"] == 0


def test_skipped_run_reports_the_armed_distribution_too(
    parse_settings, archive, demo
) -> None:
    """Ohitettu ajo lukee jakauman valmiista taulusta, ei muistista.

    Tuntemattomia nimiä ohitettu ajo **ei** raportoi: ne eivät ole taulussa,
    koska ne eivät aseista ketään, eikä niitä voi lukea takaisin ilman demoa.
    Puuttuva avain on siis oikea tulos -- keksitty tyhjä lista väittäisi,
    ettei tuntemattomia ollut.
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
    """Ankkuriton kierros ei ole nolla vaan puuttuva havainto."""
    parser = FakeParser(build_rounds(played=3, without_anchor=(2,)))
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armed_missing"] == 2  # yksi rivi per joukkue
    assert result.stats["armed_distribution"] == {4: 2, 5: 2}


def test_run_reports_the_unknown_inventory_items(
    parse_settings, archive, demo
) -> None:
    """Tuntemattomat tavaraluettelon nimet kulkevat diagnostiikasta lukuihin.

    Ilman tätä tuottaja ja kuluttaja testataan vain erikseen: adapteri
    kerää nimet ja tuloste osaa muotoilla ne, mutta väliltä puuttuisi se
    yksi rivi, joka siirtää ne. Tuntematon nimi ei aseista ketään, joten
    ilman tulostetta uusi ase näyttäisi täsmälleen samalta kuin uusi
    veitsiskini: jakauma vain valuisi hiljaa alaspäin.
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
    """Tyhjä on eri asia kuin puuttuva.

    Tyhjä luettelo on tuore ajo, jossa jokainen nimi tunnistettiin; avaimen
    puuttuminen on ohitettu ajo, josta nimiä ei voi lukea takaisin. Vain
    edellisestä saa sanoa "ei yhtään".
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
    """Portti, joka ei raportoi tuntemattomia, saa oman tilansa.

    Kolme tilaa on pidettävä erillään: avain puuttuu (ohitettu ajo, nimiä ei
    voi lukea takaisin), ``None`` (tuore ajo, portti ei kerro) ja tyhjä
    (tuore ajo, jokainen nimi tunnistettiin). Ilman eroa tuloste väittäisi
    diagnostiikattomasta ajosta samaa kuin ohitetusta.
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
    """Kalustolaskurin lukuvirheet kulkevat diagnostiikasta lukuihin.

    Luku on vika eikä havainto: ilman sitä laskuri voisi olla tyhjä koko
    demossa propivian takia, ja tulos näyttäisi vain säästökierroksilta.
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


def test_match_restarts_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """Uudelleenaloitusten määrä kulkee diagnostiikasta lukuihin.

    Ottelun uudelleenaloitus ei tuota riviä yhteenkään tauluun, joten sen
    määrää **ei voi laskea valmiista tuloksesta**. Ilman tätä yhtä riviä
    pudotus olisi hiljainen: adapteri tietäisi sen, mutta kukaan ei kertoisi.
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
    """Kolme tilaa pidetään erillään, kuten tuntemattomilla esineillä.

    Portti, joka ei raportoi uudelleenaloituksia, saa ``None``:n; portti joka
    raportoi nollan saa nollan. Ohitetussa ajossa avainta ei ole lainkaan.
    Ilman eroa välimuistista ajettu demo väittäisi hiljaa "ei
    uudelleenaloitusta".
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
    """OneDrive voi olla vielä siirtämässä tulosta -- manifesti ei yksin riitä."""
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    archive.parsed_table(MAP_DEMO_ID, "rounds").unlink()

    result = run_parse(parse_settings, archive, parser, demo)
    assert not result.skipped
    assert parser.calls == 2


def test_unreadable_result_is_reported_not_zeroed(
    parse_settings, archive, demo
) -> None:
    """Nollarivi näyttäisi siltä, ettei demossa ollut yhtään kierrosta."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    archive.parsed_table(MAP_DEMO_ID, "rounds").write_bytes(b"ei parquetia")

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    assert result.skipped
    assert "unreadable" in result.stats
    assert "rounds" not in result.stats


# --- Virheet -------------------------------------------------------------------


def test_parse_error_is_recorded_in_the_manifest(parse_settings, archive, demo) -> None:
    parser = FakeParser(error=ParseError("Demo on katkennut kesken latauksen."))
    with pytest.raises(ParseError, match="katkennut"):
        run_parse(parse_settings, archive, parser, demo)

    manifest = Manifest.read(archive.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "parse_failed"
    assert "katkennut" in (manifest.reason or "")
    assert manifest.outputs == []


def test_schema_error_is_recorded_too(parse_settings, archive, demo) -> None:
    """Sopimusrikkokin on yksikön tila, ei jälkeä jättämätön kaatuminen."""
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
    """Kelvollinen taulu ja epäonnistumista väittävä manifesti on pahin pari."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=4)), demo)
    table = archive.parsed_table(MAP_DEMO_ID, "rounds")
    before = table.read_bytes()

    # Sama demo ja sama asetus -> ohitus, joten ajo pakotetaan.
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
    assert manifest.status == "ok", "ehjää tulosta ei saa merkitä epäonnistuneeksi"


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
    """Tyhjä taulu jäisi manifestin perusteella pysyvästi ohitetuksi."""
    frame = build_rounds(played=0, warmup=3)
    with pytest.raises(ParseError, match="yhtään pelattua kierrosta"):
        run_parse(parse_settings, archive, FakeParser(frame), demo)

    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_missing_demo_is_a_finnish_error(parse_settings, archive) -> None:
    with pytest.raises(DemoUnavailable) as exc:
        parse_stage.run(parse_settings, archive, MAP_DEMO_ID, FakeParser())
    assert "ei löytynyt" in str(exc.value)


def test_unreadable_demo_does_not_get_a_shared_fingerprint(
    parse_settings, archive, tmp_path
) -> None:
    """Yhteinen varakonstantti tekisi kahdesta eri demosta saman syötteen."""
    missing = tmp_path / "kadonnut.dem"
    with pytest.raises(DemoUnavailable):
        run_parse(parse_settings, archive, FakeParser(), missing)


# --- Sopimustarkistukset -------------------------------------------------------


def test_port_contract_is_checked_exactly(parse_settings, archive, demo) -> None:
    """Ylimääräinen sarake on yhtä lailla sopimusrikko kuin puuttuva."""
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
    """CS2:ssa T ei voi voittaa syyllä ``t_killed`` -- puolet ovat väärin päin."""
    frame = build_rounds(played=3, warmup=0).with_columns(
        pl.lit("t_killed").alias("win_reason")
    )
    with pytest.raises(ParseError) as exc:
        run_parse(parse_settings, archive, FakeParser(frame), demo)
    message = str(exc.value)
    assert "sääntöjen vastaista" in message
    assert "väärin päin" in message
    assert not archive.parsed_table(MAP_DEMO_ID, "rounds").exists()


def test_uneven_row_count_per_round_is_refused(parse_settings, archive, demo) -> None:
    """Kolmas rivi kierrokselle vääristäisi jokaisen myöhemmän summan."""
    frame = build_rounds(played=3, warmup=0)
    frame = pl.concat([frame, frame.head(1)])
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(frame), demo)
    assert "kaksi riviä" in str(exc.value)


# --- Kohteen tulkinta ----------------------------------------------------------


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


# --- Portin kytkentä asetuksiin ------------------------------------------------


def test_default_parser_hands_every_parse_setting_to_the_adapter(
    parse_settings,
) -> None:
    """Kytkentä on koodin ainoa kohta, jota mikään muu testi ei kata.

    Jokainen muu testi rakentaa adapterin itse ja antaa parametrit käsin, joten
    jos yksikin kwarg katoaisi tästä, koko testijoukko menisi läpi ja
    tuotannossa arvo olisi hiljaa oletuksensa: ``area_snap_units=None`` tekisi
    jokaisesta alueesta tyhjän, ``exclude_weapons=()`` päästäisi utilityosuman
    ensikontaktiksi ja ``fallback_death`` kääntyisi päinvastaiseksi vasta jos
    asetus olisi epätosi.
    """
    port = parse_stage.default_parser(parse_settings)

    assert port.area_snap_units == parse_settings.area_snap_units
    assert port.area_snap_units is not None, "asetus on kalibroitu, ei None"
    assert list(port.exclude_weapons) == list(
        parse_settings.first_contact_exclude_weapons
    )
    assert port.fallback_death == parse_settings.first_contact_fallback_death


def test_default_parser_notices_a_changed_snap_distance(settings_file: Path) -> None:
    """Asetuksen muutos on näyttävä portilla asti, ei vain asetusoliossa."""
    changed_toml = settings_file.parent / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            settings_file.parent / "arkisto",
            **{"area_snap_units = 500": "area_snap_units = 300"},
        ),
        encoding="utf-8",
    )
    changed_parse_settings = load_settings(changed_toml, env_files=()).parse
    assert parse_stage.default_parser(changed_parse_settings).area_snap_units == 300


def test_changing_the_snap_distance_forces_a_reparse(
    tmp_path: Path, archive, demo
) -> None:
    """``area_snap_units`` muuttaa jokaisen rivin ``area``-arvon.

    Se on siis oltava ``params_hash``issa: muuten arkistoon jäisi vanhalla
    rajalla laskettu utility-taulu, ja käyttäjälle kerrottaisiin "tulos on ajan
    tasalla". Vertailukohtana ``[thresholds]``-muutos, joka ei saa parsia
    uudelleen -- sama tiedosto, eri osio.
    """
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"area_snap_units = 500": "area_snap_units = 300"}
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    run_parse(load_settings(base_toml, env_files=()).parse, archive, parser, demo)
    result = run_parse(load_settings(changed_toml, env_files=()).parse, archive, parser, demo)

    assert not result.skipped, "vanhalla rajalla laskettu alue olisi jäänyt voimaan"
    assert parser.calls == 2
