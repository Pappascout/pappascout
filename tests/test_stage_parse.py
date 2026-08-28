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
)
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.constants import SAMPLE_KINDS, SIDES
from pappascout.domain.models import load_settings
from pappascout.domain.schemas import EVENTS, ROUNDS, TICKS
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
    pelatut: int = 3,
    *,
    warmup: int = 1,
    ilman_ankkuria: tuple[int, ...] = (),
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
    rivit: list[dict[str, object]] = []
    round_raw = 0
    pisteet = 0

    def lisaa(alku: int, loppu: int) -> None:
        nonlocal round_raw
        round_raw += 1
        ankkuriton = round_raw in ilman_ankkuria
        for index, (side, lineup) in enumerate((("T", "aaa"), ("CT", "bbb"))):
            rivit.append(
                {
                    "round_raw": round_raw,
                    "round_no": None,
                    "lineup_key": lineup,
                    "side": side,
                    "won": side == "T",
                    "win_reason": "ct_killed",
                    "money_freeze_end": None if ankkuriton else 3000 + index,
                    "equip_freeze_end": None if ankkuriton else 20000 + index,
                    "equip_round_start": None if ankkuriton else 1000 + index,
                    "players_freeze_end": None if ankkuriton else 5,
                    "survivors": index,
                    "survivors_equip_prev": 500,
                    "freeze_end_tick": None if ankkuriton else 1000 * round_raw,
                    "tick_rate": 64.0,
                    "status": "no_freeze_end" if ankkuriton else "ok",
                    "score_start": alku,
                    "score_end": loppu,
                }
            )

    for _ in range(warmup):
        lisaa(pisteet, pisteet)
    for _ in range(pelatut):
        lisaa(pisteet, pisteet + 1)
        pisteet += 1

    return pl.DataFrame(rivit, schema=dict(ADAPTER_SCHEMA), orient="row")


def build_ticks(
    rounds: pl.DataFrame,
    *,
    sample_seconds: tuple[float, ...] = SAMPLE_SECONDS,
    first_contact_rounds: tuple[int, ...] = (),
    lyhyet: dict[int, float] | None = None,
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
    kesto = lyhyet or {}
    rivit: list[dict[str, object]] = []
    for kierros in rounds.iter_rows(named=True):
        raw = kierros["round_raw"]
        if kierros["freeze_end_tick"] is None:
            continue  # ankkuriton kierros ei tuota näytepisteitä
        hetket: list[tuple[str, float]] = [
            ("time", s) for s in sample_seconds if s <= kesto.get(raw, 1e9)
        ]
        if raw in first_contact_rounds:
            hetket.append(("first_contact", contact_t_s))
        for kind, t_s in hetket:
            for index in range(5):
                rivit.append(
                    {
                        "round_raw": raw,
                        "round_no": None,
                        "player_id": f"{kierros['lineup_key']}-{index}",
                        "lineup_key": kierros["lineup_key"],
                        "side": kierros["side"],
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
    return pl.DataFrame(rivit, schema=dict(TICKS_ADAPTER_SCHEMA), orient="row")


def build_events(
    rounds: pl.DataFrame,
    *,
    per_round: int = 2,
    unexploded: tuple[int, ...] = (),
    without_area: tuple[int, ...] = (),
) -> pl.DataFrame:
    """Tapahtumataulu ``build_rounds``-taulua vastaavana, kuten adapteri sen antaisi.

    Adapteri tuottaa rivejä **kaikilta ankkuroiduilta** kierrosrajoilta, myös
    lämmittelystä ja puukkokierrokselta -- se ei tunne numerointisääntöä.

    Args:
        rounds: Kierrostaulu, josta ``round_raw``, ``side`` ja ``lineup_key``
            luetaan; avaimet eivät saa erota tauluissa.
        per_round: Montako kranaattia kumpikin joukkue heittää kierroksella.
        unexploded: Ne ``grenade_entity_id``:t, joilta räjähdysrivi puuttuu.
        without_area: Ne ``grenade_entity_id``:t, joiden alue jäi tyhjäksi.
    """
    rivit: list[dict[str, object]] = []
    entity = 0
    for kierros in rounds.iter_rows(named=True):
        if kierros["freeze_end_tick"] is None:
            continue  # ankkuriton kierros ei tuota tapahtumia
        for index in range(per_round):
            entity += 1
            hetket: list[tuple[str, float]] = [("grenade_thrown", 5.0 + index)]
            if entity not in unexploded:
                hetket.append(("grenade_detonate", 7.0 + index))
            for kind, t_s in hetket:
                rivit.append(
                    {
                        "round_raw": kierros["round_raw"],
                        "round_no": None,
                        "event_kind": kind,
                        "grenade_entity_id": entity,
                        "grenade_type": "smoke" if index == 0 else "flashbang",
                        "thrower_id": f"{kierros['lineup_key']}-{index}",
                        "lineup_key": kierros["lineup_key"],
                        "side": kierros["side"],
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
    return pl.DataFrame(rivit, schema=dict(EVENTS_ADAPTER_SCHEMA), orient="row")


class FakeParser:
    """Portin toteutus, joka ei koske demoparser2:een."""

    def __init__(
        self,
        frame: pl.DataFrame | None = None,
        virhe: Exception | None = None,
        ticks: pl.DataFrame | None = None,
        events: pl.DataFrame | None = None,
    ):
        self.frame = frame if frame is not None else build_rounds()
        self.ticks = ticks if ticks is not None else build_ticks(self.frame)
        self.events = events if events is not None else build_events(self.frame)
        self.virhe = virhe
        self.kutsut = 0
        self.nahdyt_sekunnit: list[tuple[float, ...]] = []

    def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
        self.kutsut += 1
        self.nahdyt_sekunnit.append(tuple(sample_seconds))
        if self.virhe is not None:
            raise self.virhe
        return DemoTables(rounds=self.frame, ticks=self.ticks, events=self.events)


# --- Kiinnikkeet ---------------------------------------------------------------


@pytest.fixture
def arkisto(tmp_path: Path) -> ArchivePaths:
    root = tmp_path / "arkisto"
    root.mkdir()
    return ArchivePaths(root=root)


@pytest.fixture
def demo(arkisto: ArchivePaths) -> Path:
    """Demon paikkamerkki: feikki ei lue sisältöä, mutta polun on oltava aito."""
    path = arkisto.import_dir() / f"{MAP_DEMO_ID}.dem"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"PBDEMS2\x00" + b"x" * 1024)
    return path


@pytest.fixture
def parse_settings(settings_file: Path):
    return load_settings(settings_file, env_files=()).parse


def aja(settings, arkisto, parser, demo, **kwargs):
    return parse_stage.run(
        settings, arkisto, MAP_DEMO_ID, parser, demo_path=demo, **kwargs
    )


# --- Onnistunut ajo ------------------------------------------------------------


def test_writes_a_valid_rounds_table(parse_settings, arkisto, demo) -> None:
    tulos = aja(parse_settings, arkisto, FakeParser(build_rounds(pelatut=21)), demo)

    table = arkisto.parsed_table(MAP_DEMO_ID, "rounds")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert df.height == 42
    assert list(df.columns) == list(ROUNDS)
    assert df.schema == dict(ROUNDS)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert sorted(df["round_no"].unique().to_list()) == list(range(1, 22))
    assert tulos.stats["rounds"] == 21
    assert tulos.stats["rows"] == 42
    assert not tulos.skipped
    assert tulos.status == "ok"


def test_numbering_columns_never_reach_the_archive(
    parse_settings, arkisto, demo
) -> None:
    """``score_start`` ja ``score_end`` ovat portin sisäisiä työkaluja."""
    aja(parse_settings, arkisto, FakeParser(), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    assert "score_start" not in df.columns
    assert "score_end" not in df.columns


def test_unplayed_rounds_stay_out_of_the_table_but_are_counted(
    parse_settings, arkisto, demo
) -> None:
    tulos = aja(parse_settings, arkisto, FakeParser(build_rounds(2, warmup=3)), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df["round_no"].null_count() == 0
    assert df.height == 4
    assert tulos.stats["skipped_rounds"] == 3
    # round_raw säilyy demon omana numerona, joten ohitus näkyy aukkona.
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]


def test_round_without_a_freeze_anchor_stays_in_the_table(
    parse_settings, arkisto, demo
) -> None:
    """Puuttuva ankkuri ei kaada ajoa: kierros on mukana omalla tilallaan."""
    frame = build_rounds(3, warmup=0, ilman_ankkuria=(2,))
    tulos = aja(parse_settings, arkisto, FakeParser(frame), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df.height == 6
    puuttuva = df.filter(pl.col("status") == "no_freeze_end")
    assert puuttuva.height == 2
    assert puuttuva["freeze_end_tick"].null_count() == 2
    assert puuttuva["round_no"].to_list() == [2, 2]
    assert tulos.stats["no_freeze_end"] == 1


def test_writes_a_manifest_with_only_the_parse_section(
    parse_settings, arkisto, demo
) -> None:
    aja(parse_settings, arkisto, FakeParser(), demo)
    manifest = Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID))

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
    parse_settings, arkisto, demo
) -> None:
    """233 MB:n sha256 jokaisella ajolla olisi hitaampi kuin itse parsinta."""
    meta = arkisto.demo_meta(MAP_DEMO_ID)
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"sha256": "kokeiltu-tiiviste"}), encoding="utf-8")

    aja(parse_settings, arkisto, FakeParser(), demo)
    manifest = Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID))
    assert manifest.inputs[0].sha256 == "kokeiltu-tiiviste"


def test_write_is_atomic(parse_settings, arkisto, demo) -> None:
    aja(parse_settings, arkisto, FakeParser(), demo)
    assert not has_temp_leftovers(arkisto.root)


def test_rows_are_sorted_by_round(parse_settings, arkisto, demo) -> None:
    aja(parse_settings, arkisto, FakeParser(build_rounds(5)), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df["round_no"].to_list() == sorted(df["round_no"].to_list())


# --- Näytepistetaulu -----------------------------------------------------------


def test_writes_a_valid_ticks_table(parse_settings, arkisto, demo) -> None:
    """Hyväksymiskriteeri: ``ticks.parquet`` läpäisee ``validate(TICKS)``."""
    rounds = build_rounds(pelatut=3, warmup=0)
    tulos = aja(
        parse_settings, arkisto, FakeParser(rounds, ticks=build_ticks(rounds)), demo
    )

    table = arkisto.parsed_table(MAP_DEMO_ID, "ticks")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert list(df.columns) == list(TICKS)
    assert df.schema == dict(TICKS)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    # 3 kierrosta x 2 joukkuetta x 2 näytepistettä x 5 pelaajaa.
    assert df.height == 60
    assert tulos.stats["tick_rows"] == 60
    assert tulos.stats["sample_points"] == 6  # kierros x hetki
    assert tulos.stats["sample_rounds"] == 3


def test_all_three_tables_are_listed_among_the_outputs(
    parse_settings, arkisto, demo
) -> None:
    tulos = aja(parse_settings, arkisto, FakeParser(), demo)
    assert [p.name for p in tulos.outputs] == [
        "rounds.parquet",
        "ticks.parquet",
        "events.parquet",
    ]


def test_ticks_get_the_round_number_from_the_rounds_table(
    parse_settings, arkisto, demo
) -> None:
    """Numeroinnin omistaa domain.rounds; vaihe vain liittää sen."""
    rounds = build_rounds(pelatut=3, warmup=0)
    aja(parse_settings, arkisto, FakeParser(rounds, ticks=build_ticks(rounds)), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))

    assert df["round_no"].null_count() == 0
    assert sorted(df["round_no"].unique().to_list()) == [1, 2, 3]
    # round_raw säilyy demon omana numerona rinnalla.
    parit = set(zip(df["round_raw"].to_list(), df["round_no"].to_list()))
    assert parit == {(1, 1), (2, 2), (3, 3)}


def test_unnumbered_rounds_produce_no_tick_rows(parse_settings, arkisto, demo) -> None:
    """I/O-matriisi: warmup ja puukkokierros -> ei tick-rivejä.

    Adapteri näytteistää ne, koska se ei tunne numerointisääntöä; tämä testi
    lukitsee sen, että vaihe pudottaa ne samalla päätöksellä kuin
    kierrostaulusta.
    """
    rounds = build_rounds(pelatut=2, warmup=3)
    tulos = aja(
        parse_settings, arkisto, FakeParser(rounds, ticks=build_ticks(rounds)), demo
    )
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    # Numeroimattomat round_raw-arvot 1..3 eivät ole taulussa.
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]
    assert tulos.stats["skipped_rounds"] == 3


def test_a_round_without_an_anchor_has_no_tick_rows(
    parse_settings, arkisto, demo
) -> None:
    """I/O-matriisi: ankkuriton kierros on rounds-taulussa mutta ei ticksissä."""
    rounds = build_rounds(3, warmup=0, ilman_ankkuria=(2,))
    aja(parse_settings, arkisto, FakeParser(rounds, ticks=build_ticks(rounds)), demo)

    kierrokset = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    ticks = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))
    assert 2 in kierrokset["round_no"].to_list()
    assert sorted(ticks["round_no"].unique().to_list()) == [1, 3]


def test_a_short_round_keeps_only_the_points_it_reached(
    parse_settings, arkisto, demo
) -> None:
    """Hyväksymiskriteeri: ei näytepistettä kierroksen päättymisen jälkeen."""
    rounds = build_rounds(pelatut=2, warmup=0)
    ticks = build_ticks(rounds, lyhyet={2: 10.0})  # round_raw 2 ratkesi 10 s
    aja(parse_settings, arkisto, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))
    lyhyt = df.filter(pl.col("round_no") == 2)
    assert sorted(lyhyt["sample_t_s"].unique().to_list()) == [6.0]
    pitka = df.filter(pl.col("round_no") == 1)
    assert sorted(pitka["sample_t_s"].unique().to_list()) == [6.0, 15.0]


def test_first_contact_rows_are_counted_separately(
    parse_settings, arkisto, demo
) -> None:
    rounds = build_rounds(pelatut=3, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(1, 3))
    tulos = aja(parse_settings, arkisto, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))
    kontakti = df.filter(pl.col("sample_kind") == "first_contact")
    assert sorted(kontakti["round_no"].unique().to_list()) == [1, 3]
    assert tulos.stats["first_contact_rounds"] == 2


def test_lineup_keys_join_across_the_two_tables(
    parse_settings, arkisto, demo
) -> None:
    """Liitos ``(map_demo_id, round_no)`` ei saa mennä ristiin joukkueissa."""
    aja(parse_settings, arkisto, FakeParser(), demo)
    kierrokset = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    ticks = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))

    liitos = ticks.join(
        kierrokset.select("map_demo_id", "round_no", "lineup_key", "side"),
        on=["map_demo_id", "round_no", "lineup_key", "side"],
        how="inner",
    )
    assert liitos.height == ticks.height


def test_ticks_rows_are_sorted_by_round_and_time(
    parse_settings, arkisto, demo
) -> None:
    aja(parse_settings, arkisto, FakeParser(build_rounds(4, warmup=0)), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))
    avaimet = list(zip(df["round_no"].to_list(), df["sample_t_s"].to_list()))
    assert avaimet == sorted(avaimet)


def test_the_stage_passes_the_configured_sample_seconds_to_the_port(
    parse_settings, arkisto, demo
) -> None:
    """Näytepisteajat ovat asetus eivätkä koodia (AD-3)."""
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)
    assert parser.nahdyt_sekunnit == [tuple(parse_settings.snapshot_seconds)]


def test_a_ticks_table_breaking_the_port_contract_is_rejected(
    parse_settings, arkisto, demo
) -> None:
    rounds = build_rounds()
    rikki = build_ticks(rounds).drop("area")
    with pytest.raises(SchemaError) as exc:
        aja(parse_settings, arkisto, FakeParser(rounds, ticks=rikki), demo)
    assert "area" in str(exc.value)
    assert "näytepistetaulun" in str(exc.value)


def test_an_extra_ticks_column_is_a_contract_break_too(
    parse_settings, arkisto, demo
) -> None:
    rounds = build_rounds()
    rikki = build_ticks(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        aja(parse_settings, arkisto, FakeParser(rounds, ticks=rikki), demo)
    assert "ylimaarainen" in str(exc.value)


def test_an_empty_ticks_table_with_rounds_is_refused(
    parse_settings, arkisto, demo
) -> None:
    """Kierroksia mutta ei yhtään näytepistettä on virhe, ei ok-tulos.

    Tyhjä asetelmataulu jäisi manifestin perusteella pysyvästi ohitetuksi, ja
    aggregointi raportoisi kartan ilman yhtään asetelmaa -- tasan se hiljainen
    tyhjyys, jonka koko sopimuksen on tarkoitus estää.
    """
    rounds = build_rounds(pelatut=2, warmup=0)
    tyhja = pl.DataFrame(schema=dict(TICKS_ADAPTER_SCHEMA))
    with pytest.raises(ParseError) as exc:
        aja(parse_settings, arkisto, FakeParser(rounds, ticks=tyhja), demo)
    assert "näytepistettä" in str(exc.value)

    assert not arkisto.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_a_failure_leaves_no_partial_ticks_table(
    parse_settings, arkisto, demo
) -> None:
    with pytest.raises(ParseError):
        aja(parse_settings, arkisto, FakeParser(virhe=ParseError("rikki")), demo)
    assert not arkisto.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert not has_temp_leftovers(arkisto.root)


def test_a_missing_ticks_table_forces_a_reparse(
    parse_settings, arkisto, demo
) -> None:
    """Puolikas tulos ei ole ajantasainen tulos."""
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)
    arkisto.parsed_table(MAP_DEMO_ID, "ticks").unlink()

    tulos = aja(parse_settings, arkisto, parser, demo)
    assert not tulos.skipped
    assert parser.kutsut == 2


def test_an_archive_parsed_by_an_older_version_is_reparsed(
    parse_settings, arkisto, demo
) -> None:
    """Story 1.3:n arkisto ei saa jäädä ilman ``ticks.parquet``-taulua.

    ``ParseSettings`` ei muuttunut Story 2.1:ssä, joten ``params_hash`` on
    identtinen. Manifestin ``outputs_present()`` tarkistaa vain ne polut, jotka
    **levyllä oleva** manifesti nimeää -- ja vanha manifesti nimeää vain
    kierrostaulun. Ilman erillistä tarkistusta ajo ohitettaisiin, asetelmataulu
    ei syntyisi koskaan, ja käyttäjälle kerrottaisiin "Tulos on ajan tasalla".
    """
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)

    # Kelaa arkisto Story 1.3:n muotoon: manifesti nimeää vain rounds-taulun
    # ja ticks-taulua ei ole.
    manifest_polku = arkisto.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_polku.read_text(encoding="utf-8"))
    manifest["outputs"] = [f"parsed/{MAP_DEMO_ID}/rounds.parquet"]
    manifest_polku.write_text(json.dumps(manifest), encoding="utf-8")
    arkisto.parsed_table(MAP_DEMO_ID, "ticks").unlink()

    tulos = aja(parse_settings, arkisto, parser, demo)

    assert not tulos.skipped, "vanha arkisto olisi jäänyt ilman asetelmataulua"
    assert parser.kutsut == 2
    assert arkisto.parsed_table(MAP_DEMO_ID, "ticks").is_file()


def test_a_failed_second_write_never_looks_up_to_date(
    parse_settings, arkisto, demo, monkeypatch
) -> None:
    """Kolmen taulun kirjoitus on yksi tapahtuma.

    Jos ticks-kirjoitus kaatuu peräkkäisissä lohkoissa, arkistoon jäisi
    kierrostaulu ilman pariaan -- ja koska manifesti kirjoitettaisiin silti,
    seuraava ajo ohittaisi vaiheen ja kertoisi iloisesti kierrosmäärän.
    """
    alkuperainen = pl.DataFrame.write_parquet
    kutsut = {"n": 0}

    def kaatuva(self, *args, **kwargs):
        kutsut["n"] += 1
        if kutsut["n"] == 2:
            raise OSError("levy täyttyi kesken kirjoituksen")
        return alkuperainen(self, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "write_parquet", kaatuva)

    parser = FakeParser(build_rounds(pelatut=5, warmup=0))
    with pytest.raises(OSError):
        aja(parse_settings, arkisto, parser, demo)

    monkeypatch.undo()

    # Yksikään taulu ei jäänyt paikalleen, ja manifesti kertoo virheestä.
    assert not arkisto.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert not arkisto.parsed_table(MAP_DEMO_ID, "ticks").exists()
    assert not arkisto.parsed_table(MAP_DEMO_ID, "events").exists()
    assert not has_temp_leftovers(arkisto.root)
    assert Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"

    # Ja seuraava ajo ei ohita.
    tulos = aja(parse_settings, arkisto, FakeParser(build_rounds(5, warmup=0)), demo)
    assert not tulos.skipped
    assert tulos.stats["rounds"] == 5
    assert tulos.stats["sample_rounds"] == 5


def test_ticks_are_sorted_deterministically_by_kind_too(
    parse_settings, arkisto, demo
) -> None:
    """Ensikontakti voi osua tasan konfiguroidulle sekunnille.

    Ilman ``sample_kind``ia lajitteluavaimessa kahden rivin järjestys riippuisi
    syötejärjestyksestä, ja sama demo tuottaisi eri tavut eri ajoilla.
    """
    rounds = build_rounds(pelatut=2, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(1, 2), contact_t_s=6.0)
    aja(parse_settings, arkisto, FakeParser(rounds, ticks=ticks), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "ticks"))
    paallekkain = df.filter(pl.col("sample_t_s") == 6.0)
    assert set(paallekkain["sample_kind"].unique()) == {"time", "first_contact"}
    # sample_kind on Enum, joten Polars lajittelee sen luettelon
    # järjestyksessä (time, first_contact) eikä aakkosittain. Kumpi tahansa
    # kelpaa; olennaista on että järjestys on määrätty eikä satunnainen.
    lajit = {nimi: index for index, nimi in enumerate(SAMPLE_KINDS)}
    puolet = {nimi: index for index, nimi in enumerate(SIDES)}
    avaimet = [
        (
            rivi["round_no"],
            lajit[rivi["sample_kind"]],
            puolet[rivi["side"]],
            rivi["player_id"],
        )
        for rivi in paallekkain.iter_rows(named=True)
    ]
    assert avaimet == sorted(avaimet)


def test_unreadable_ticks_do_not_hide_the_round_counts(
    parse_settings, arkisto, demo
) -> None:
    """Yksi rikki mennyt taulu ei saa viedä toisen lukuja."""
    aja(parse_settings, arkisto, FakeParser(build_rounds(pelatut=4, warmup=0)), demo)
    arkisto.parsed_table(MAP_DEMO_ID, "ticks").write_bytes(b"ei parquetia")

    tulos = aja(parse_settings, arkisto, FakeParser(), demo)
    assert tulos.skipped
    assert tulos.stats["rounds"] == 4
    assert "ticks_unreadable" in tulos.stats
    assert "unreadable" not in tulos.stats


def test_skipped_run_reports_the_tick_counts_too(
    parse_settings, arkisto, demo
) -> None:
    """Ohitettu ajo lukee luvut valmiista tauluista, ei parsi demoa."""
    rounds = build_rounds(pelatut=3, warmup=0)
    ticks = build_ticks(rounds, first_contact_rounds=(2,))
    aja(parse_settings, arkisto, FakeParser(rounds, ticks=ticks), demo)

    tulos = aja(parse_settings, arkisto, FakeParser(rounds, ticks=ticks), demo)
    assert tulos.skipped
    assert tulos.stats["tick_rows"] == 70  # 60 aikapistettä + 10 ensikontaktia
    assert tulos.stats["first_contact_rounds"] == 1


# --- Tapahtumataulu ------------------------------------------------------------


def test_writes_a_valid_events_table(parse_settings, arkisto, demo) -> None:
    """Hyväksymiskriteeri: ``events.parquet`` läpäisee ``validate(EVENTS)``."""
    rounds = build_rounds(pelatut=3, warmup=0)
    tulos = aja(
        parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo
    )

    table = arkisto.parsed_table(MAP_DEMO_ID, "events")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert list(df.columns) == list(EVENTS)
    assert df.schema == dict(EVENTS)
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    # 3 kierrosta x 2 joukkuetta x 2 kranaattia x 2 riviä.
    assert df.height == 24
    assert tulos.stats["event_rows"] == 24
    assert tulos.stats["utility_throws"] == 12
    assert tulos.stats["utility_detonations"] == 12
    assert tulos.stats["utility_rounds"] == 3


def test_every_grenade_has_at_most_one_throw_and_one_detonation(
    parse_settings, arkisto, demo
) -> None:
    """Hyväksymiskriteeri: pari on pari, ei kolmea riviä."""
    rounds = build_rounds(pelatut=4, warmup=0)
    aja(parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))
    maarat = df.group_by("round_no", "grenade_entity_id", "event_kind").len()
    assert maarat["len"].max() == 1


def test_an_unexploded_grenade_has_no_invented_detonation(
    parse_settings, arkisto, demo
) -> None:
    """I/O-matriisi: rata katkeaa -> vain ``grenade_thrown``."""
    rounds = build_rounds(pelatut=2, warmup=0)
    events = build_events(rounds, unexploded=(1,))
    tulos = aja(parse_settings, arkisto, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))
    yksinainen = df.filter(pl.col("grenade_entity_id") == 1)
    assert yksinainen["event_kind"].to_list() == ["grenade_thrown"]
    assert tulos.stats["utility_throws"] - tulos.stats["utility_detonations"] == 1


def test_events_get_the_round_number_from_the_rounds_table(
    parse_settings, arkisto, demo
) -> None:
    rounds = build_rounds(pelatut=3, warmup=0)
    aja(parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))

    assert df["round_no"].null_count() == 0
    assert sorted(df["round_no"].unique().to_list()) == [1, 2, 3]


def test_unnumbered_rounds_produce_no_event_rows(
    parse_settings, arkisto, demo
) -> None:
    """I/O-matriisi: heitto numeroimattomalla kierroksella -> ei rivejä."""
    rounds = build_rounds(pelatut=2, warmup=3)
    aja(parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo)
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))

    assert sorted(df["round_no"].unique().to_list()) == [1, 2]
    assert sorted(df["round_raw"].unique().to_list()) == [4, 5]


def test_a_round_without_an_anchor_has_no_event_rows(
    parse_settings, arkisto, demo
) -> None:
    """I/O-matriisi: ankkuriton kierros -> ei rivejä (``t_s`` ei määritelty)."""
    rounds = build_rounds(3, warmup=0, ilman_ankkuria=(2,))
    aja(parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))
    assert sorted(df["round_no"].unique().to_list()) == [1, 3]


def test_an_empty_events_table_is_a_valid_result(
    parse_settings, arkisto, demo
) -> None:
    """I/O-matriisi: demo ilman utilityä -> tyhjä ``events.parquet``.

    Toisin kuin tyhjä kierros- tai näytepistetaulu, tämä ei ole virhe: demossa
    on aina pelattuja kierroksia, mutta utility voi aidosti puuttua. Virhe
    estäisi koko demon parsinnan tiedosta, joka on itsessään havainto.
    """
    rounds = build_rounds(pelatut=2, warmup=0)
    tyhja = pl.DataFrame(schema=dict(EVENTS_ADAPTER_SCHEMA))
    tulos = aja(parse_settings, arkisto, FakeParser(rounds, events=tyhja), demo)

    assert tulos.status == "ok"
    table = arkisto.parsed_table(MAP_DEMO_ID, "events")
    assert table.is_file()
    df = pl.read_parquet(table)
    assert df.is_empty()
    assert df.schema == dict(EVENTS)
    assert tulos.stats["event_rows"] == 0
    assert tulos.stats["utility_throws"] == 0


def test_events_without_an_area_are_counted(parse_settings, arkisto, demo) -> None:
    """I/O-matriisi: räjähdys kaukana kaikista -> ``area = null``, ei pudotusta."""
    rounds = build_rounds(pelatut=2, warmup=0)
    events = build_events(rounds, without_area=(2, 4))
    tulos = aja(parse_settings, arkisto, FakeParser(rounds, events=events), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))
    aluettomat = df.filter(pl.col("area").is_null())
    assert aluettomat.height == 4  # kaksi kranaattia x kaksi riviä
    # Koordinaatit säilyvät, vaikka alue ei ratkennut.
    assert aluettomat["x"].null_count() == 0
    assert aluettomat["area_source"].null_count() == 4
    assert tulos.stats["utility_without_area"] == 4


def test_observed_and_snapped_areas_are_counted_separately(
    parse_settings, arkisto, demo
) -> None:
    """Havainto ja arvio ovat eri laatua olevaa tietoa eivätkä saa niputtua.

    Ilman erottelua raportti esittäisi räjähdyksen arvion yhtä varmana kuin
    heittäjän oman alueen.
    """
    rounds = build_rounds(pelatut=2, warmup=0)
    tulos = aja(
        parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo
    )
    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))

    heitot = df.filter(pl.col("event_kind") == "grenade_thrown")
    rajahdykset = df.filter(pl.col("event_kind") == "grenade_detonate")
    assert heitot["area_source"].unique().to_list() == ["observed"]
    assert rajahdykset["area_source"].unique().to_list() == ["snapped"]
    # Napsautusetäisyys on vain arviolla -- havainto ei ole minkään päässä.
    assert heitot["snap_distance"].null_count() == heitot.height
    assert rajahdykset["snap_distance"].null_count() == 0
    assert tulos.stats["utility_area_observed"] == heitot.height
    assert tulos.stats["utility_area_snapped"] == rajahdykset.height


def test_utility_on_unnumbered_rounds_is_counted_not_just_dropped(
    parse_settings, arkisto, demo
) -> None:
    """Kolme muuta pudotussyytä raportoidaan -- tämä ei saa olla poikkeus."""
    rounds = build_rounds(pelatut=2, warmup=3)
    tulos = aja(
        parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo
    )
    # 3 numeroimatonta kierrosta x 2 joukkuetta x 2 kranaattia = 12 heittoa.
    assert tulos.stats["utility_unnumbered_rounds"] == 12


def test_lineup_keys_join_from_events_to_rounds(
    parse_settings, arkisto, demo
) -> None:
    """Heittäjän joukkue on sama kuin kierrostaulussa; ei ristiinkytkentää."""
    aja(parse_settings, arkisto, FakeParser(), demo)
    kierrokset = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "rounds"))
    events = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))

    liitos = events.join(
        kierrokset.select("map_demo_id", "round_no", "lineup_key", "side"),
        on=["map_demo_id", "round_no", "lineup_key", "side"],
        how="inner",
    )
    assert liitos.height == events.height


def test_event_rows_are_sorted_deterministically(
    parse_settings, arkisto, demo
) -> None:
    """Saman kranaatin heitto tulee aina ennen sen räjähdystä."""
    rounds = build_rounds(pelatut=3, warmup=0)
    aja(parse_settings, arkisto, FakeParser(rounds, events=build_events(rounds)), demo)

    df = pl.read_parquet(arkisto.parsed_table(MAP_DEMO_ID, "events"))
    avaimet = list(zip(df["round_no"].to_list(), df["grenade_entity_id"].to_list()))
    assert avaimet == sorted(avaimet)
    for _, ryhma in df.group_by("grenade_entity_id", maintain_order=True):
        assert ryhma["event_kind"].to_list()[0] == "grenade_thrown"


def test_an_events_table_breaking_the_port_contract_is_rejected(
    parse_settings, arkisto, demo
) -> None:
    rounds = build_rounds()
    rikki = build_events(rounds).drop("area")
    with pytest.raises(SchemaError) as exc:
        aja(parse_settings, arkisto, FakeParser(rounds, events=rikki), demo)
    assert "area" in str(exc.value)
    assert "tapahtumataulun" in str(exc.value)


def test_a_missing_events_table_forces_a_reparse(
    parse_settings, arkisto, demo
) -> None:
    """Puolikas tulos ei ole ajantasainen tulos."""
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)
    arkisto.parsed_table(MAP_DEMO_ID, "events").unlink()

    tulos = aja(parse_settings, arkisto, parser, demo)
    assert not tulos.skipped
    assert parser.kutsut == 2


def test_an_archive_parsed_before_utility_is_reparsed(
    parse_settings, arkisto, demo
) -> None:
    """Story 2.1:n arkisto ei saa jäädä ilman ``events.parquet``-taulua.

    Sama ansa kuin Story 2.1:ssä: ``ParseSettings`` muuttui vain
    ``area_snap_units``-kentän oletuksella, ja jos se on sama, ``params_hash``
    on identtinen. Manifestin ``outputs_present()`` tarkistaa vain ne polut,
    jotka **levyllä oleva** manifesti nimeää.
    """
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)

    manifest_polku = arkisto.parsed_manifest(MAP_DEMO_ID)
    manifest = json.loads(manifest_polku.read_text(encoding="utf-8"))
    manifest["outputs"] = [
        f"parsed/{MAP_DEMO_ID}/rounds.parquet",
        f"parsed/{MAP_DEMO_ID}/ticks.parquet",
    ]
    manifest_polku.write_text(json.dumps(manifest), encoding="utf-8")
    arkisto.parsed_table(MAP_DEMO_ID, "events").unlink()

    tulos = aja(parse_settings, arkisto, parser, demo)

    assert not tulos.skipped, "vanha arkisto olisi jäänyt ilman utility-taulua"
    assert arkisto.parsed_table(MAP_DEMO_ID, "events").is_file()


def test_unreadable_events_do_not_hide_the_other_counts(
    parse_settings, arkisto, demo
) -> None:
    aja(parse_settings, arkisto, FakeParser(build_rounds(pelatut=4, warmup=0)), demo)
    arkisto.parsed_table(MAP_DEMO_ID, "events").write_bytes(b"ei parquetia")

    tulos = aja(parse_settings, arkisto, FakeParser(), demo)
    assert tulos.skipped
    assert tulos.stats["rounds"] == 4
    assert "events_unreadable" in tulos.stats
    assert "unreadable" not in tulos.stats


def test_skipped_run_reports_the_event_counts_too(
    parse_settings, arkisto, demo
) -> None:
    rounds = build_rounds(pelatut=3, warmup=0)
    events = build_events(rounds)
    parser = FakeParser(rounds, events=events)
    aja(parse_settings, arkisto, parser, demo)

    tulos = aja(parse_settings, arkisto, parser, demo)
    assert tulos.skipped
    assert tulos.stats["utility_throws"] == 12
    assert tulos.stats["utility_detonations"] == 12


# --- Ohitus --------------------------------------------------------------------


def test_second_run_is_skipped(parse_settings, arkisto, demo) -> None:
    parser = FakeParser(build_rounds(pelatut=21))
    aja(parse_settings, arkisto, parser, demo)
    table = arkisto.parsed_table(MAP_DEMO_ID, "rounds")
    ennen = table.stat().st_mtime_ns

    tulos = aja(parse_settings, arkisto, parser, demo)

    assert tulos.skipped
    assert parser.kutsut == 1, "demoa ei saa parsia uudelleen"
    assert table.stat().st_mtime_ns == ennen, "tiedostoa ei saa kirjoittaa uudelleen"
    assert tulos.stats["rounds"] == 21
    assert "ajan tasalla" in (tulos.reason or "")


def test_force_overrides_a_matching_manifest(parse_settings, arkisto, demo) -> None:
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)
    tulos = aja(parse_settings, arkisto, parser, demo, force=True)

    assert not tulos.skipped
    assert parser.kutsut == 2


def test_changed_demo_bytes_trigger_a_reparse(parse_settings, arkisto, demo) -> None:
    """Pelkkä manifesti ei riitä: vanhentunut tulos ei saa jäädä pysyvästi."""
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)
    assert aja(parse_settings, arkisto, parser, demo).skipped

    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 2048)  # eri sisältö ja eri koko

    tulos = aja(parse_settings, arkisto, parser, demo)
    assert not tulos.skipped
    assert parser.kutsut == 2


def test_threshold_change_does_not_trigger_a_reparse(
    tmp_path: Path, arkisto, demo
) -> None:
    """AD-3: kynnysten säätö ei saa invalidoida parsintaa."""
    perus = tmp_path / "perus.toml"
    perus.write_text(settings_text(arkisto.root), encoding="utf-8")
    muutettu = tmp_path / "muutettu.toml"
    muutettu.write_text(
        settings_text(
            arkisto.root, **{"full_equip_min = 4000": "full_equip_min = 4100"}
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    aja(load_settings(perus, env_files=()).parse, arkisto, parser, demo)
    tulos = aja(load_settings(muutettu, env_files=()).parse, arkisto, parser, demo)

    assert tulos.skipped
    assert parser.kutsut == 1


def test_parse_setting_change_triggers_a_reparse(tmp_path: Path, arkisto, demo) -> None:
    perus = tmp_path / "perus.toml"
    perus.write_text(settings_text(arkisto.root), encoding="utf-8")
    muutettu = tmp_path / "muutettu.toml"
    muutettu.write_text(
        settings_text(
            arkisto.root,
            **{
                "snapshot_seconds = [6.0, 15.0, 30.0, 45.0]": (
                    "snapshot_seconds = [6.0, 15.0, 30.0, 50.0]"
                )
            },
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    aja(load_settings(perus, env_files=()).parse, arkisto, parser, demo)
    tulos = aja(load_settings(muutettu, env_files=()).parse, arkisto, parser, demo)

    assert not tulos.skipped
    assert parser.kutsut == 2


def test_missing_output_forces_a_reparse(parse_settings, arkisto, demo) -> None:
    """OneDrive voi olla vielä siirtämässä tulosta -- manifesti ei yksin riitä."""
    parser = FakeParser()
    aja(parse_settings, arkisto, parser, demo)
    arkisto.parsed_table(MAP_DEMO_ID, "rounds").unlink()

    tulos = aja(parse_settings, arkisto, parser, demo)
    assert not tulos.skipped
    assert parser.kutsut == 2


def test_unreadable_result_is_reported_not_zeroed(
    parse_settings, arkisto, demo
) -> None:
    """Nollarivi näyttäisi siltä, ettei demossa ollut yhtään kierrosta."""
    aja(parse_settings, arkisto, FakeParser(), demo)
    arkisto.parsed_table(MAP_DEMO_ID, "rounds").write_bytes(b"ei parquetia")

    tulos = aja(parse_settings, arkisto, FakeParser(), demo)
    assert tulos.skipped
    assert "unreadable" in tulos.stats
    assert "rounds" not in tulos.stats


# --- Virheet -------------------------------------------------------------------


def test_parse_error_is_recorded_in_the_manifest(parse_settings, arkisto, demo) -> None:
    parser = FakeParser(virhe=ParseError("Demo on katkennut kesken latauksen."))
    with pytest.raises(ParseError, match="katkennut"):
        aja(parse_settings, arkisto, parser, demo)

    manifest = Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "parse_failed"
    assert "katkennut" in (manifest.reason or "")
    assert manifest.outputs == []


def test_schema_error_is_recorded_too(parse_settings, arkisto, demo) -> None:
    """Sopimusrikkokin on yksikön tila, ei jälkeä jättämätön kaatuminen."""
    frame = build_rounds().drop("survivors")
    with pytest.raises(SchemaError):
        aja(parse_settings, arkisto, FakeParser(frame), demo)

    manifest = Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "parse_failed"
    assert "survivors" in (manifest.reason or "")


def test_parse_error_leaves_no_partial_table(parse_settings, arkisto, demo) -> None:
    parser = FakeParser(virhe=ParseError("rikki"))
    with pytest.raises(ParseError):
        aja(parse_settings, arkisto, parser, demo)

    assert not arkisto.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert not has_temp_leftovers(arkisto.root)


def test_failure_never_overwrites_a_valid_result(parse_settings, arkisto, demo) -> None:
    """Kelvollinen taulu ja epäonnistumista väittävä manifesti on pahin pari."""
    aja(parse_settings, arkisto, FakeParser(build_rounds(pelatut=4)), demo)
    table = arkisto.parsed_table(MAP_DEMO_ID, "rounds")
    ennen = table.read_bytes()

    # Sama demo ja sama asetus -> ohitus, joten ajo pakotetaan.
    with pytest.raises(ParseError):
        aja(
            parse_settings,
            arkisto,
            FakeParser(virhe=ParseError("rikki")),
            demo,
            force=True,
        )

    assert table.read_bytes() == ennen
    manifest = Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID))
    assert manifest.status == "ok", "ehjää tulosta ei saa merkitä epäonnistuneeksi"


def test_failed_manifest_is_not_treated_as_current(
    parse_settings, arkisto, demo
) -> None:
    rikki = FakeParser(virhe=ParseError("rikki"))
    with pytest.raises(ParseError):
        aja(parse_settings, arkisto, rikki, demo)

    ehja = FakeParser()
    tulos = aja(parse_settings, arkisto, ehja, demo)
    assert not tulos.skipped
    assert tulos.status == "ok"


def test_zero_played_rounds_is_an_error_not_an_empty_result(
    parse_settings, arkisto, demo
) -> None:
    """Tyhjä taulu jäisi manifestin perusteella pysyvästi ohitetuksi."""
    frame = build_rounds(pelatut=0, warmup=3)
    with pytest.raises(ParseError, match="yhtään pelattua kierrosta"):
        aja(parse_settings, arkisto, FakeParser(frame), demo)

    assert not arkisto.parsed_table(MAP_DEMO_ID, "rounds").exists()
    assert Manifest.read(arkisto.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_missing_demo_is_a_finnish_error(parse_settings, arkisto) -> None:
    with pytest.raises(DemoUnavailable) as exc:
        parse_stage.run(parse_settings, arkisto, MAP_DEMO_ID, FakeParser())
    assert "ei löytynyt" in str(exc.value)


def test_unreadable_demo_does_not_get_a_shared_fingerprint(
    parse_settings, arkisto, tmp_path
) -> None:
    """Yhteinen varakonstantti tekisi kahdesta eri demosta saman syötteen."""
    puuttuva = tmp_path / "kadonnut.dem"
    with pytest.raises(DemoUnavailable):
        aja(parse_settings, arkisto, FakeParser(), puuttuva)


# --- Sopimustarkistukset -------------------------------------------------------


def test_port_contract_is_checked_exactly(parse_settings, arkisto, demo) -> None:
    """Ylimääräinen sarake on yhtä lailla sopimusrikko kuin puuttuva."""
    frame = build_rounds().with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        aja(parse_settings, arkisto, FakeParser(frame), demo)
    assert "ylimaarainen" in str(exc.value)


def test_table_that_breaks_the_contract_is_rejected(
    parse_settings, arkisto, demo
) -> None:
    frame = build_rounds().drop("survivors")
    with pytest.raises(SchemaError) as exc:
        aja(parse_settings, arkisto, FakeParser(frame), demo)
    assert "survivors" in str(exc.value)


def test_impossible_win_reason_is_refused(parse_settings, arkisto, demo) -> None:
    """CS2:ssa T ei voi voittaa syyllä ``t_killed`` -- puolet ovat väärin päin."""
    frame = build_rounds(pelatut=3, warmup=0).with_columns(
        pl.lit("t_killed").alias("win_reason")
    )
    with pytest.raises(ParseError) as exc:
        aja(parse_settings, arkisto, FakeParser(frame), demo)
    viesti = str(exc.value)
    assert "sääntöjen vastaista" in viesti
    assert "väärin päin" in viesti
    assert not arkisto.parsed_table(MAP_DEMO_ID, "rounds").exists()


def test_uneven_row_count_per_round_is_refused(parse_settings, arkisto, demo) -> None:
    """Kolmas rivi kierrokselle vääristäisi jokaisen myöhemmän summan."""
    frame = build_rounds(pelatut=3, warmup=0)
    frame = pl.concat([frame, frame.head(1)])
    with pytest.raises(SchemaError) as exc:
        aja(parse_settings, arkisto, FakeParser(frame), demo)
    assert "kaksi riviä" in str(exc.value)


# --- Kohteen tulkinta ----------------------------------------------------------


@pytest.mark.parametrize(
    "nimi",
    [
        f"{MAP_DEMO_ID}.dem",
        f"{MAP_DEMO_ID}.dem.zst",
        f"{MAP_DEMO_ID}.dem.gz",
    ],
)
def test_map_demo_id_is_read_from_the_file_name(nimi: str) -> None:
    assert parse_stage.map_demo_id_from_path(Path("/x") / nimi) == MAP_DEMO_ID


def test_resolve_demo_accepts_a_file_path(arkisto, demo) -> None:
    assert parse_stage.resolve_demo(arkisto, str(demo)) == (MAP_DEMO_ID, demo)


def test_resolve_demo_finds_the_demo_from_the_import_dir(arkisto, demo) -> None:
    assert parse_stage.resolve_demo(arkisto, MAP_DEMO_ID) == (MAP_DEMO_ID, demo)


def test_resolve_demo_finds_the_demo_from_the_demos_dir(arkisto) -> None:
    polku = arkisto.demo(MAP_DEMO_ID)
    polku.parent.mkdir(parents=True, exist_ok=True)
    polku.write_bytes(b"x")
    assert parse_stage.resolve_demo(arkisto, MAP_DEMO_ID) == (MAP_DEMO_ID, polku)


def test_resolve_demo_lists_the_searched_paths(arkisto) -> None:
    with pytest.raises(DemoUnavailable) as exc:
        parse_stage.resolve_demo(arkisto, MAP_DEMO_ID)
    viesti = str(exc.value)
    assert "demos" in viesti
    assert "import" in viesti


def test_unsafe_identifier_is_refused(arkisto) -> None:
    with pytest.raises(PappascoutError):
        parse_stage.resolve_demo(arkisto, "../pako")


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
    portti = parse_stage.default_parser(parse_settings)

    assert portti.area_snap_units == parse_settings.area_snap_units
    assert portti.area_snap_units is not None, "asetus on kalibroitu, ei None"
    assert list(portti.exclude_weapons) == list(
        parse_settings.first_contact_exclude_weapons
    )
    assert portti.fallback_death == parse_settings.first_contact_fallback_death


def test_default_parser_notices_a_changed_snap_distance(settings_file: Path) -> None:
    """Asetuksen muutos on näyttävä portilla asti, ei vain asetusoliossa."""
    muutettu = settings_file.parent / "muutettu.toml"
    muutettu.write_text(
        settings_text(
            settings_file.parent / "arkisto",
            **{"area_snap_units = 500": "area_snap_units = 300"},
        ),
        encoding="utf-8",
    )
    asetukset = load_settings(muutettu, env_files=()).parse
    assert parse_stage.default_parser(asetukset).area_snap_units == 300


def test_changing_the_snap_distance_forces_a_reparse(
    tmp_path: Path, arkisto, demo
) -> None:
    """``area_snap_units`` muuttaa jokaisen rivin ``area``-arvon.

    Se on siis oltava ``params_hash``issa: muuten arkistoon jäisi vanhalla
    rajalla laskettu utility-taulu, ja käyttäjälle kerrottaisiin "tulos on ajan
    tasalla". Vertailukohtana ``[thresholds]``-muutos, joka ei saa parsia
    uudelleen -- sama tiedosto, eri osio.
    """
    perus = tmp_path / "perus.toml"
    perus.write_text(settings_text(arkisto.root), encoding="utf-8")
    muutettu = tmp_path / "muutettu.toml"
    muutettu.write_text(
        settings_text(
            arkisto.root, **{"area_snap_units = 500": "area_snap_units = 300"}
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    aja(load_settings(perus, env_files=()).parse, arkisto, parser, demo)
    tulos = aja(load_settings(muutettu, env_files=()).parse, arkisto, parser, demo)

    assert not tulos.skipped, "vanhalla rajalla laskettu alue olisi jäänyt voimaan"
    assert parser.kutsut == 2
