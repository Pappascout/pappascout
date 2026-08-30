"""``stages.parse`` -- vaiheen testit ilman demoja.

Vaihe näkee demon vain portin takaa (AD-8), joten sen koko logiikka -- taulujen
validointi, kierrosnumeron liittäminen näytepisteisiin ja tapahtumiin, atominen
kirjoitus, manifesti ja ohitus -- testataan feikillä, joka rakentaa kaikki
kolme taulua käsin. Yksikään näistä testeistä ei tarvitse demotiedostoa.
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
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.errors import DemoUnavailable, PappascoutError, ParseError, SchemaError
from pappascout.stages import StageResult
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

#: Pistepilven tyypit portin takana: ``CALLOUT_CLOUD`` ilman ``map_demo_id``:tä.
CALLOUTS_ADAPTER_SCHEMA: dict[str, object] = {
    name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS
}

#: Ruudun särmä, jolla feikin pistepilvi on rakennettu. Sama kuin
#: ``settings.toml``in ``callout_grid_units``.
CALLOUT_GRID = 32

#: Näytepisteet, joilla feikki rakentaa tick-rivinsä.
SAMPLE_SECONDS = (6.0, 15.0)


def build_callouts(cells: int = 4) -> pl.DataFrame:
    """Pistepilvi, kuten adapteri sen antaisi: rivi per ruutu, ei kierrosnumeroa.

    Ruudut ovat peräkkäisiä ja alueita on kaksi, jotta taulu näyttää siltä
    mitä oikea pilvi on: monta ruutua alueen sisällä. Havaintomäärät ovat eri
    suuria, koska ne ovat ruudun oma havainto eivätkä vakio.
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
                    "money_buy_end": None if no_anchor else 3000 + index,
                    "equip_buy_end": None if no_anchor else 20000 + index,
                    "equip_round_start": None if no_anchor else 1000 + index,
                    "players_buy_end": None if no_anchor else 5,
                    # Adapteri antaa laskurin valmiina; vaihe vain kuljettaa
                    # sen. Puolikohtainen ero tekee kuljetuksesta todettavan.
                    ARMED_COLUMN: None if no_anchor else 5 - index,
                    # Panssarilaskuri on tarkoituksella **eri jakauma** kuin
                    # aseistettujen: jos vaihe kuljettaisi saman sarakkeen
                    # kahdesti, jakaumat olisivat identtiset eikä yksikään
                    # testi näkisi sitä.
                    ARMORED_COLUMN: None if no_anchor else 5,
                    "survivors": index,
                    "survivors_equip_prev": 500,
                    "freeze_end_tick": None if no_anchor else 1000 * round_raw,
                    # Mittauspiste on tarkoituksella eri kuin ankkuri: jos se
                    # jätettäisiin täyttämättä, Polars täyttäisi sen tyhjällä
                    # eikä yksikään vaihetesti näkisi saraketta koskaan
                    # täytettynä.
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
                            else ("observed" if kind == "grenade_thrown" else "point_cloud")
                        ),
                        "snap_distance": (
                            None if kind == "grenade_thrown" else 120.0
                        ),
                    }
                )
    return pl.DataFrame(rows, schema=dict(EVENTS_ADAPTER_SCHEMA), orient="row")


#: Kokoonpanotaulun tyypit portin takana: ``LINEUPS`` ilman ``map_demo_id``:ta.
LINEUPS_ADAPTER_SCHEMA: dict[str, object] = {
    name: LINEUPS[name] for name in LINEUPS_ADAPTER_COLUMNS
}

#: Klaaninimet, jotka feikki antaa kokoonpanoille. Oikeista demoista mitattuja.
CLANS: dict[str, str] = {"aaa": "MatureMayhem", "bbb": "KALJUKOSTAJA"}


def build_lineups(
    rounds: pl.DataFrame,
    *,
    without_clan: tuple[str, ...] = (),
    without_name: tuple[str, ...] = (),
) -> pl.DataFrame:
    """Kokoonpanotaulu ``build_rounds``-taulua vastaavana, kuten adapteri sen antaisi.

    Rivi per (kokoonpano, pelaaja) ja **ei kierrosnumeroa**: nimi on kartan
    ominaisuus eikä kierroksen. Pelaajatunnisteet ovat samat kuin
    ``build_ticks``issa, jotta taulut eivät ole eri mieltä kokoonpanosta.

    Args:
        rounds: Kierrostaulu, josta kokoonpanotunnisteet luetaan.
        without_clan: Ne kokoonpanot, joilta klaaninimi puuttuu.
        without_name: Ne kokoonpanot, joilta pelaajien nimet puuttuvat.
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


#: Kuolemataulun tyypit portin takana: ``DEATHS`` ilman ``map_demo_id``:ta.
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
    """Kuolemataulu ``build_rounds``-taulua vastaavana, kuten adapteri sen antaisi.

    Adapteri tuottaa rivejä **kaikilta ankkuroiduilta** kierrosrajoilta, myös
    lämmittelystä ja puukkokierrokselta: puukkokierroksella kuollaan
    oikeasti, eikä adapteri tunne numerointisääntöä.

    Kuolema kirjataan T-puolen rivin näkökulmasta: uhri on T-kokoonpanosta ja
    ampuja CT-kokoonpanosta. ``rounds`` on pitkä taulu (kaksi riviä per
    kierros), joten vain toinen puoli luetaan -- muuten jokainen kuolema
    syntyisi kahdesti.

    Args:
        rounds: Kierrostaulu, josta ``round_raw`` ja kokoonpanot luetaan.
        per_round: Montako kuolemaa kierroksella.
        without_attacker: Ne kuolemien järjestysnumerot (1-pohjainen), joilta
            ampuja puuttuu kokonaan -- putoaminen tai pommi.
        without_victim_area: Numerot, joilta uhrin alue puuttuu.
        without_attacker_area: Numerot, joilta **vain** ampujan alue puuttuu.
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
    """Portin toteutus, joka ei koske demoparser2:een."""

    def __init__(
        self,
        frame: pl.DataFrame | None = None,
        error: Exception | None = None,
        ticks: pl.DataFrame | None = None,
        events: pl.DataFrame | None = None,
        lineups: pl.DataFrame | None = None,
        deaths: pl.DataFrame | None = None,
        callouts: pl.DataFrame | None = None,
    ):
        self.frame = frame if frame is not None else build_rounds()
        self.ticks = ticks if ticks is not None else build_ticks(self.frame)
        self.events = events if events is not None else build_events(self.frame)
        self.lineups = (
            lineups if lineups is not None else build_lineups(self.frame)
        )
        self.deaths = deaths if deaths is not None else build_deaths(self.frame)
        self.callouts = callouts if callouts is not None else build_callouts()
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
        )


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
        f"parsed/{MAP_DEMO_ID}/lineups.parquet",
        f"parsed/{MAP_DEMO_ID}/deaths.parquet",
        f"parsed/{MAP_DEMO_ID}/callouts.parquet",
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


def test_all_six_tables_are_listed_among_the_outputs(
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


def test_a_lineups_table_breaking_the_port_contract_is_rejected(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_lineups(rounds).drop("clan_name")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, lineups=broken), demo)
    assert "clan_name" in str(exc.value)
    assert "kokoonpanotaulun" in str(exc.value)


def test_an_extra_lineups_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    rounds = build_rounds()
    broken = build_lineups(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, lineups=broken), demo)
    assert "ylimaarainen" in str(exc.value)
    assert "kokoonpanotaulun" in str(exc.value)


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


def test_observed_and_derived_areas_are_counted_separately(
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
    assert detonations["area_source"].unique().to_list() == ["point_cloud"]
    # Napsautusetäisyys on vain arviolla -- havainto ei ole minkään päässä.
    assert throws["snap_distance"].null_count() == throws.height
    assert detonations["snap_distance"].null_count() == 0
    assert result.stats["utility_area_observed"] == throws.height
    assert result.stats["utility_area_point_cloud"] == detonations.height


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


def test_armored_count_survives_the_write(parse_settings, archive, demo) -> None:
    """Panssarilaskuri kulkee adapterilta levylle asti muuttumattomana."""
    run_parse(parse_settings, archive, FakeParser(build_rounds(played=3)), demo)

    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "rounds"))
    assert df[ARMORED_COLUMN].dtype == pl.Int32
    assert df[ARMORED_COLUMN].to_list() == [5] * 6
    # Eri sarake eikä sama kahdesti: aseistettujen jakauma on toinen.
    assert df[ARMED_COLUMN].to_list() != df[ARMORED_COLUMN].to_list()


def test_run_reports_the_armored_distribution(parse_settings, archive, demo) -> None:
    """``run()`` palauttaa ne avaimet, joita panssaririvi lukee.

    Sama kytkentätesti kuin aseistettujen jakaumalla ja samasta syystä: ilman
    sitä tuottaja ja kuluttaja testataan vain erikseen, ja väliltä puuttuva
    ``stats.update(_armored_stats(df))`` vain pudottaisi rivin tulosteesta
    ilman että yksikään testi kaatuu.
    """
    result = run_parse(
        parse_settings, archive, FakeParser(build_rounds(played=3)), demo
    )

    assert result.stats["armored_distribution"] == {5: 6}
    assert result.stats["armored_missing"] == 0
    # Kaksi eri jakaumaa samasta ajosta -- juuri se on sarakkeen tarkoitus.
    assert result.stats["armed_distribution"] == {4: 3, 5: 3}


def test_skipped_run_reports_the_armored_distribution_too(
    parse_settings, archive, demo
) -> None:
    """Ohitettu ajo lukee panssarijakauman valmiista taulusta, ei muistista."""
    parser = FakeParser(build_rounds(played=3))
    run_parse(parse_settings, archive, parser, demo)

    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert result.stats["armored_distribution"] == {5: 6}


def test_armored_distribution_counts_rounds_without_an_anchor_as_missing(
    parse_settings, archive, demo
) -> None:
    """Ankkuriton kierros ei ole nolla panssaria vaan puuttuva havainto."""
    parser = FakeParser(build_rounds(played=3, without_anchor=(2,)))
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.stats["armored_missing"] == 2  # yksi rivi per joukkue
    assert result.stats["armored_distribution"] == {5: 4}


def test_an_old_table_without_the_armored_column_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """Arkiston vanha ``rounds.parquet`` ilman panssarisaraketta ei kaada ajoa.

    I/O-matriisin rivi "vanha arkisto". **Manifestia ei kosketa**: se on
    täsmäävä, ja juuri se on testin ydin. Skeematarkistus yksin riittää
    pakottamaan uudelleenajon, joten käyttäjän ei tarvitse tietää
    ``--pakota``-lipusta eikä vanha taulu jää hiljaa voimaan.
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
    """``0 <= panssaroidut <= players_buy_end`` valvotaan lukuhetkellä.

    Skeeman docstring lupaa rajan, mutta ``validate`` tarkistaa vain tyypit.
    Ilman arvotarkistusta mahdoton luku kirjoittuisi arkistoon ja näkyisi
    raportissa muodossa "6 (1/1 kierroksesta)" viiden pelaajan joukkueelle.
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
    """Sama raja koskee kalustolaskuria -- se oli valvomatta jo ennen tätä."""
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
    """Aseistettujen on oltava panssaroitujen osajoukko.

    Aseistetun ehto sisältää panssarin, joten ylitys tarkoittaisi että
    laskurit lukevat eri tickiä tai eri pelaajajoukkoa -- vika, joka näkyisi
    raportissa vain kahtena uskottavan näköisenä lukuna.
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
    assert "osajoukko" in str(exc.value)


def test_a_null_counter_is_not_an_invariant_break(
    parse_settings, archive, demo
) -> None:
    """Ankkuriton kierros läpäisee tarkistuksen: null on rehellinen puute.

    Ilman tätä paria edelliset kolme testiä menisivät läpi myös
    toteutuksella, joka hylkää jokaisen tyhjän laskurin.
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


def test_the_two_unreadable_counters_reach_the_stats_separately(
    parse_settings, archive, demo
) -> None:
    """Kaksi lukua eikä yksi: erotus kertoo mikä propeista petti.

    Laskureiden luettavuusehdot eroavat -- panssarilaskuri ei lue
    tavaraluetteloa -- joten yhteinen luku ei erottaisi riviä, jolla panssari
    jäi lukematta, rivistä, jolla petti pelkkä tavaraluettelo. Juuri se on
    väite, jonka koko kahden sarakkeen ratkaisu tekee, eikä sitä voi lukea
    valmiista taulusta.
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
    # Erotus on "rivit, joilla vain tavaraluettelo petti".
    assert (
        result.stats["armed_unreadable_rows"]
        - result.stats["armored_unreadable_rows"]
        == 3
    )


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


#: ``[parse]``-asetus -> adapterin attribuutti, jolle se kytketään.
#:
#: ``None`` tarkoittaa asetusta, joka **ei** kulje konstruktorin kautta.
#: Luettelo on täydellinen, ja :func:`test_every_parse_setting_is_in_the_wiring_map`
#: pitää sen sellaisena: uusi asetus kaataa testin, kunnes sille on päätetty
#: paikka.
PORT_WIRING: dict[str, str | None] = {
    # Annetaan parse_demo-kutsussa, ei konstruktorissa. Oma testinsä:
    # test_the_stage_passes_the_configured_sample_seconds_to_the_port.
    "snapshot_seconds": None,
    "buy_window_seconds": "buy_window_seconds",
    "first_contact_exclude_weapons": "exclude_weapons",
    "first_contact_fallback_death": "fallback_death",
    "area_snap_units": "area_snap_units",
    "callout_grid_units": "callout_grid_units",
    "callout_z_weight": "callout_z_weight",
    "callout_z_tolerance_units": "callout_z_tolerance_units",
}

#: Arvot, jotka eroavat **sekä** tuotannon asetuksista **että** adapterin
#: omista oletuksista. Jälkimmäinen on koko testin idea: adapterin oletukset
#: ovat tarkoituksella samat mitatut luvut kuin asetusten oletukset, joten
#: pelkkä ``port.x == settings.x`` menisi läpi myös silloin, kun kwarg on
#: pudonnut kytkennästä kokonaan.
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
    """Kartan on katettava jokainen ``[parse]``-kenttä.

    Ilman tätä uusi asetus voisi jäädä kytkemättä porttiin, ja
    :func:`test_default_parser_hands_every_parse_setting_to_the_adapter`
    väittäisi kattavuutta jota sillä ei ole -- se iteroi juuri tämän kartan
    yli.
    """
    assert set(PORT_WIRING) == set(ParseSettings.model_fields)


def test_the_distinct_values_really_differ_from_the_adapter_defaults() -> None:
    """Esiehto: testiarvo, joka on adapterin oletus, ei todista kytkennästä.

    Juuri tämä ansa oli auki Story 2.9:ssä: adapterin oletukset
    (``callout_grid_units=32``, ``callout_z_weight=1.0``,
    ``callout_z_tolerance_units=72.0``) ovat samat luvut kuin asetusten
    oletukset, joten kytkentärivin poisto olisi jättänyt jokaisen testin
    vihreäksi.
    """
    defaults = inspect.signature(Demoparser2Adapter.__init__).parameters
    for field, attribute in PORT_WIRING.items():
        if attribute is None:
            continue
        default = defaults[attribute].default
        assert DISTINCT_SETTINGS[field] != default, (
            f"{field}: testiarvo on sama kuin adapterin oletus {default!r}, "
            "joten se ei paljastaisi pudonnutta kytkentää"
        )


def test_default_parser_hands_every_parse_setting_to_the_adapter() -> None:
    """Kytkentä on koodin ainoa kohta, jota mikään muu testi ei kata.

    Jokainen muu testi rakentaa adapterin itse ja antaa parametrit käsin, joten
    jos yksikin kwarg katoaisi tästä, koko testijoukko menisi läpi ja
    tuotannossa arvo olisi hiljaa oletuksensa: ``exclude_weapons=()``
    päästäisi utilityosuman ensikontaktiksi, ja pistepilven mitat
    rakentuisivat adapterin kovakoodatuista luvuista, vaikka käyttäjä olisi
    säätänyt niitä -- ja koska hänen säätönsä muuttaa ``params_hash``ia, hän
    saisi täyden uudelleenparsinnan ja "valmis"-yhteenvedon säätämättömällä
    ruudukolla.

    Arvot ovat siksi **kaikki eri kuin adapterin oletukset**; esiehdon
    tarkistaa :func:`test_the_distinct_values_really_differ_from_the_adapter_defaults`.
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
        assert actual == expected, f"{field} ei päätynyt portin {attribute}:iin"


def test_default_parser_carries_the_real_settings_too(parse_settings) -> None:
    """Sama kytkentä tuotannon arvoilla: kalibroitu kynnys ei ole None."""
    port = parse_stage.default_parser(parse_settings)
    assert port.area_snap_units == parse_settings.area_snap_units
    assert port.area_snap_units is not None, "asetus on kalibroitu, ei None"
    assert port.callout_grid_units == parse_settings.callout_grid_units
    assert port.callout_z_weight == parse_settings.callout_z_weight
    assert (
        port.callout_z_tolerance_units == parse_settings.callout_z_tolerance_units
    )


def test_default_parser_notices_a_changed_snap_distance(settings_file: Path) -> None:
    """Asetuksen muutos on näyttävä portilla asti, ei vain asetusoliossa."""
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
            archive.root, **{"area_snap_units = 256": "area_snap_units = 300"}
        ),
        encoding="utf-8",
    )

    parser = FakeParser()
    run_parse(load_settings(base_toml, env_files=()).parse, archive, parser, demo)
    result = run_parse(load_settings(changed_toml, env_files=()).parse, archive, parser, demo)

    assert not result.skipped, "vanhalla rajalla laskettu alue olisi jäänyt voimaan"
    assert parser.calls == 2


# --- Ostoikkuna (Story 1.9) ---------------------------------------------------


def test_default_parser_hands_the_buy_window_to_the_adapter(parse_settings) -> None:
    """Ostoikkuna on kytkettävä portille asti.

    Adapterin oletus on **0**, eli mittaus ankkurista. Jos kwarg unohtuisi
    tästä, koko putki mittaisi hiljaa freezetimen lopusta -- täsmälleen se
    vika, jonka Story 1.9 korjaa -- eikä yksikään muu testi huomaisi sitä,
    koska ne rakentavat adapterin itse.
    """
    port = parse_stage.default_parser(parse_settings)

    assert port.buy_window_seconds == parse_settings.buy_window_seconds
    assert port.buy_window_seconds > 0, "asetus on pelin sääntö, ei nolla"


def test_changing_the_buy_window_forces_a_reparse(
    tmp_path: Path, archive, demo
) -> None:
    """I/O-matriisi: ``buy_window_seconds`` muuttuu -> ``parse`` ajetaan uudelleen.

    Ikkuna siirtää jokaisen talousrivin mittaushetkeä, joten vanha tulos ei ole
    ajan tasalla. Se on ``[parse]``-osiossa juuri siksi: kynnysten säätö ei
    parsi uudelleen, mutta mittauspisteen siirto parsii.
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
    """Ostoikkunan luvut lasketaan **pelatuista** kierroksista.

    Puukkokierros saa oman ``round_raw``:nsa, mutta se ei ole kierros eikä
    päädy tauluun. Adapteri ei tiedä sitä, joten se antaa katkaisut
    ``round_raw``-numeroina ja vaihe suodattaa ne. Ilman suodatusta käyttäjä
    näkisi rivin "3 kierrosta mitattiin aiemmin" taulussa, jossa niitä on
    kaksi -- ja mittaushetkien jakauma alkaisi puukkokierroksen sekunnin
    murto-osista.

    ``build_rounds`` tuottaa yhden numeroimattoman kierroksen (``round_raw``
    1) ja kolme pelattua (2-4), joten katkaisu numerolla 1 on juuri se, jonka
    on kadottava.
    """

    class _Cutting(FakeParser):
        diagnostics = ParseDiagnostics(
            tick_rate=64.0,
            tick_rate_measured=True,
            rounds_seen=4,
            buy_window_seconds=20.0,
            # Puukkokierros (1) ja kaksi pelattua (2, 3); kierroksella 3 jäi
            # yksi ostos katkaisun taakse.
            buy_window_cuts=((1, 4), (2, 0), (3, 1)),
            buy_window_unchecked_cuts=(1, 2),
        )

    result = run_parse(parse_settings, archive, _Cutting(), demo)
    stats = result.stats

    assert stats["buy_window_truncated_by_death"] == 2
    # Puukkokierroksen neljä menetettyä ostosta eivät ole taulussa, joten ne
    # eivät saa olla luvussakaan.
    assert stats["buy_window_purchases_after_cut"] == 1
    assert stats["buy_window_rounds_with_lost_purchases"] == (3,)
    assert stats["buy_window_cuts_unchecked"] == 1


def test_the_measurement_offsets_come_from_the_written_table(
    parse_settings, archive, demo
) -> None:
    """Mittaushetkien jakauma lasketaan siitä taulusta, joka kirjoitetaan.

    Se on ``buy_end_tick``-sarakkeen ainoa näkyvä muoto. Jos jakauma tulisi
    muualta, sarake ja tuloste voisivat erkaantua, ja tarkistettavuus olisi
    näennäistä.

    ``build_rounds`` asettaa mittauspisteen 1280 tickin päähän ankkurista
    jokaisella kierroksella, eli 20,0 sekuntiin tickratella 64.
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
    """Portti, joka ei kerro ostoikkunasta, ei saa tuottaa nollia.

    ``getattr``-oletus 0 tekisi tuntemattomasta puhtaan ajon näköisen: "ei
    yhtään katkaisua" olisi väite, jota mikään ei tue.
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
    """Ohitetussa ajossa lukuja ei ole, eikä niitä keksitä.

    Katkaisuja ja menetettyjä ostoja ei voi lukea valmiista taulusta, joten
    avaimet puuttuvat kokonaan ja ``cli`` jättää rivit pois -- sama sääntö
    kuin uudelleenaloituksilla ja tuntemattomilla esineillä.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    result = run_parse(parse_settings, archive, parser, demo)

    assert result.skipped
    assert "buy_window_truncated_by_death" not in result.stats
    assert "buy_end_offsets_s" not in result.stats


# --- Kokoonpanotaulu (Story 2.6) -----------------------------------------------


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
    """Kokoonpano on kartan ominaisuus: numerointi ei koske sita.

    Näytepiste- ja tapahtumataulusta puukkokierroksen rivit pudotetaan, mutta
    pelaaja pelasi kartan -- eikä häntä saa pudottaa rosterista.
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
    """Tyhjä taulu jäisi manifestin perusteella pysyvästi ohitetuksi."""
    empty = pl.DataFrame(schema=dict(LINEUPS_ADAPTER_SCHEMA))
    parser = FakeParser(lineups=empty)

    with pytest.raises(SchemaError, match="kokoonpanoriviä"):
        run_parse(parse_settings, archive, parser, demo)

    assert not archive.parsed_table(MAP_DEMO_ID, "lineups").exists()


def test_a_duplicate_roster_row_is_refused(parse_settings, archive, demo) -> None:
    """``aggregate`` liittaa rosterin talla avaimella; kaksoisrivi kahdentaisi pelaajan."""
    frame = build_rounds()
    lineups = build_lineups(frame)
    doubled = pl.concat([lineups, lineups.head(1)])
    parser = FakeParser(frame, lineups=doubled)

    with pytest.raises(SchemaError, match="lineup_key, player_id"):
        run_parse(parse_settings, archive, parser, demo)


def test_an_archive_without_the_lineups_table_is_not_up_to_date(
    parse_settings, archive, demo
) -> None:
    """Skeemamuutos pakottaa uudelleenparsinnan ilman --pakota-lippua.

    Manifestin parametrihash ei liiku, kun tauluja tulee lisaa, joten pelkka
    manifestin tasmays hyvaksyisi vanhan tuloksen ajan tasalla olevana.
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
    """Luvut eritellään kokoonpanoittain, koska demossa on kaksi joukkuetta.

    Yhteinen luettelo vastaisi eri kysymykseen kuin se, jonka käyttäjä esittää:
    "onko *tällä* joukkueella nimi" ei ratkea listasta, joka on epätyhjä heti
    kun vastustajalla on klaani.
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
    """Nimetön kokoonpano näkyy omana rivinään, ei vastustajan nimen alla."""
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
    """Kokoonpanotaulun perusoletus on ajonaikaisesti tarkistettava.

    Taulu kirjaa useimmin havaitun arvon, joten rikkoutunut oletus näyttää
    siellä täsmälleen samalta kuin ehjä. Vain diagnostiikka erottaa ne, ja
    nollasta poikkeava luku on juuri se oire, josta puolen kautta lukemisen
    ansan varoitus puhuu.
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
    assert "Klaani vaihtui kesken" in text
    assert "Nimi vaihtui kesken" in text
    # Nolla on odotusarvo, eikä sitä tulosteta.
    assert "vaihtui kesken" not in _render_parse(result, 24)


def test_the_parse_summary_renders_every_key_the_stage_produces(
    parse_settings, archive, demo
) -> None:
    """Tuottajan ja kuluttajan avainsopimus, valvottuna kuten aggregate-puolella.

    ``_render_parse`` lukee kymmeniä avaimia ``stats``ista. Ilman tätä testiä
    vaiheen tuottama avain ja komentorivin lukema avain voisivat erota, ja
    lohko jäisi hiljaa tulostumatta -- ei kaatuisi, vaan katoaisi.
    """
    from pappascout.cli import _render_parse

    result = run_parse(parse_settings, archive, FakeParser(), demo)
    text = _render_parse(result, 24)

    assert "Kokoonpanot" in text
    assert "MatureMayhem (aaa)" in text
    assert "KALJUKOSTAJA (bbb)" in text


# --- Kuolemataulu (Story 2.7) --------------------------------------------------


def read_deaths(archive: ArchivePaths) -> pl.DataFrame:
    return pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "deaths"))


def test_the_deaths_table_is_written_and_carries_the_map_demo_id(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: normaali demo -> deaths.parquet sopimuksen mukaisena."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    df = read_deaths(archive)

    validate(df, DEATHS, "deaths")
    assert set(df["map_demo_id"].to_list()) == {MAP_DEMO_ID}
    assert df.height == 3  # kolme pelattua kierrosta, yksi kuolema kullakin


def test_deaths_get_the_round_number_from_the_rounds_table(
    parse_settings, archive, demo
) -> None:
    """Numeroinnin omistaa domain.rounds; vaihe vain liittää sen."""
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
    """I/O-matriisi: puukkokierroksen kuolemat eivät päädy tauluun.

    Adapteri tuottaa ne, koska se ei tunne numerointisääntöä --
    puukkokierroksella kuollaan oikeasti. Ne putoavat samassa liitoksessa
    kuin näytepisteet ja kranaatit, ei erillisellä säännöllä.
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
    """Hiljainen pudotus näyttäisi demolta, jossa kuolemia oli vähemmän."""
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
    """I/O-matriisi: ampujaton kuolema -> attacker_* null, rivi säilyy."""
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
    """Ampujaton rivi ei ole aluevika, joten luvut ovat erikseen.

    Yhteinen luku näyttäisi kahdelta aluevialta silloin, kun toinen on
    rehellinen putoaminen.
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
    """Uhrin alueen puuttuminen on mitatussa aineistossa nolla -- luku kertoo
    jos se muuttuu."""
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
    """Vakaa järjestys: sama syöte, samat tavut."""
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
    """Puuttuva sarake -> suomenkielinen SchemaError, joka nimeää taulun."""
    rounds = build_rounds()
    broken = build_deaths(rounds).drop("victim_area")
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, deaths=broken), demo)
    assert "victim_area" in str(exc.value)
    assert "kuolemataulun" in str(exc.value)


def test_an_extra_deaths_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    """Ylimääräinen sarake tarkoittaa, että portti ja sopimus erkanivat."""
    rounds = build_rounds()
    broken = build_deaths(rounds).with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(rounds, deaths=broken), demo)
    assert "ylimaarainen" in str(exc.value)
    assert "kuolemataulun" in str(exc.value)


@pytest.mark.parametrize(
    "column", ["attacker_x", "attacker_y", "attacker_z", "attacker_area"]
)
def test_an_attackerless_death_may_not_carry_attacker_observations(
    parse_settings, archive, demo, column: str
) -> None:
    """Puolikas ampuja on vika, vaikka skeema hyväksyisi jokaisen kentän.

    Paikka ilman toimijaa laskeutuisi raportissa tapoksi, jota kukaan ei
    tehnyt. Jokainen havaintokenttä testataan erikseen: yksi yhteinen testi
    menisi läpi, vaikka kolme neljästä ehdosta poistettaisiin.
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
    assert "ampujaa" in str(exc.value)
    assert column in str(exc.value)


def test_an_attackerless_death_with_only_nulls_is_accepted(
    parse_settings, archive, demo
) -> None:
    """Vartijan toinen haara: ehjä ampujaton rivi menee läpi.

    Ilman tätä edellinen testi todistaisi vain, että jokin kaataa ajon.
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
    """Pelatussa ottelussa kuollaan, joten tyhjä taulu on rikkinäinen portti.

    Tyhjä tulos jäisi manifestin perusteella pysyvästi ohitetuksi, ja
    raportti kertoisi kartasta, jolla kukaan ei kuollut.
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
    assert "kuolemaa" in str(exc.value)
    assert not archive.parsed_table(MAP_DEMO_ID, "deaths").exists()
    assert Manifest.read(archive.parsed_manifest(MAP_DEMO_ID)).status == "parse_failed"


def test_an_archive_without_the_deaths_table_is_not_up_to_date(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: vanha arkisto ilman deaths.parquet -> ajetaan uudelleen.

    ``ParseSettings`` ei muuttunut, joten parametrihash on identtinen ja
    vanha manifesti nimeää vain neljä taulua. Ilman erillistä tarkistusta ajo
    ohitettaisiin ja kuolemataulu ei syntyisi koskaan.
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

    assert not result.skipped, "vanha arkisto olisi jäänyt ilman kuolemataulua"
    assert parser.calls == 2
    assert archive.parsed_table(MAP_DEMO_ID, "deaths").is_file()


def test_a_stale_deaths_table_missing_a_column_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """Sopimusmuutos pakottaa uudelleenparsinnan ilman ``--pakota``."""
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
    """Yksi lukukelvoton taulu ei saa viedä muiden lukuja.

    Taulut luetaan erikseen: rikkinäinen kuolemataulu kerrotaan omalla
    avaimellaan, ja kierros-, näytepiste- ja utility-luvut säilyvät.
    Lukukelvoton taulu on eri vika kuin vanhentunut sopimus, eikä se parane
    demon uudelleenluvusta -- siksi ajo pysyy ohitettuna.
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
    """Ohitettu ajo lukee luvut valmiista taulusta eikä väitä nollaa."""
    run_parse(parse_settings, archive, FakeParser(), demo)
    result = run_parse(parse_settings, archive, FakeParser(), demo)

    assert result.skipped
    assert result.stats["death_rows"] == 3
    assert result.stats["death_rounds"] == 3
    # Numeroimattomilta pudonneita ei voi lukea valmiista taulusta.
    assert "deaths_unnumbered_rounds" not in result.stats


def test_the_deaths_diagnostics_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """Adapterin omat luvut kulkevat ajon yhteenvetoon sellaisinaan."""

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
    """Tuottajan ja kuluttajan avainsopimus jokaiselle uudelle tulosteriville.

    Jokainen rivi tarkistetaan **arvoineen**: pelkkä otsikon etsiminen menisi
    läpi, vaikka luku olisi väärästä avaimesta.
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
        # Numerot juoksevat KAIKKIEN kierrosrajojen yli, myös warmupin:
        # numerot 1-2 putoavat numeroimattomina, joten poikkeukset on
        # asetettava pelatuille kierroksille 3-5.
        deaths=build_deaths(
            rounds,
            without_attacker=(3,),
            without_victim_area=(4,),
            without_attacker_area=(5,),
        ),
    )
    text = _render_parse(run_parse(parse_settings, archive, parser, demo), 24)

    assert "Kuolemat" in text and "3 (3/3 kierroksella)" in text
    assert "Numeroimattomilta" in text and "2 kuolemaa" in text
    assert "Ampujaton kuolema" in text and "1 (putoaminen" in text
    assert "Uhri ilman aluetta" in text and "1 riviä" in text
    assert "Ampuja ilman aluetta" in text
    assert "Kierrosten välissä" in text and "2 (" in text
    assert "Uhri ilman puolta" in text and "3 (" in text
    assert "Ampuja ilman puolta" in text and "4 (" in text


def test_the_parse_summary_stays_silent_when_every_death_is_whole(
    parse_settings, archive, demo
) -> None:
    """Nolla on odotusarvo, eikä sitä tulosteta -- vain rivimäärä jää."""
    from pappascout.cli import _render_parse

    text = _render_parse(
        run_parse(parse_settings, archive, FakeParser(), demo), 24
    )
    assert "Kuolemat" in text and "3 (3/3 kierroksella)" in text
    assert "Ampujaton kuolema" not in text
    assert "Uhri ilman aluetta" not in text
    assert "Ampuja ilman aluetta" not in text
    assert "Uhri ilman puolta" not in text
    assert "Ampuja ilman puolta" not in text
    assert "Kierrosten välissä" not in text


def test_an_unreadable_deaths_table_is_reported_in_a_skipped_run(
    parse_settings, archive, demo
) -> None:
    """Lukukelvoton taulu kerrotaan, ei nollata."""
    from pappascout.cli import _render_parse

    stats = parse_stage._existing_stats(
        archive.parsed_table(MAP_DEMO_ID, "rounds"),
        archive.parsed_table(MAP_DEMO_ID, "ticks"),
        archive.parsed_table(MAP_DEMO_ID, "events"),
        archive.parsed_table(MAP_DEMO_ID, "lineups"),
        archive.parsed_table(MAP_DEMO_ID, "deaths"),
        archive.parsed_table(MAP_DEMO_ID, "callouts"),
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
    )
    assert "deaths_unreadable" in stats
    # Yksi rikki mennyt taulu ei vie toisen lukuja: pistepilvi on ehjä.
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
    assert "Kuolemat" in text and "lukuja ei saatu" in text


# --- Katselmuskierros: uhrin eheys, järjestys ja pudotussyyt -------------------


@pytest.mark.parametrize(
    "column", ["victim_id", "victim_lineup_key", "victim_side"]
)
def test_a_death_without_its_victim_is_refused(
    parse_settings, archive, demo, column: str
) -> None:
    """Uhri on rivin identiteetti; ilman sitä rivi katoaisi hiljaa.

    Jokainen näistä on nullable-sarake, joten ``validate`` päästäisi rivin
    läpi -- ja aggregoinnissa se ei olisi kuolema eikä tappo, koska molemmat
    suodattimet vertaavat kokoonpanoon. Sarakkeet testataan erikseen: yksi
    yhteinen testi menisi läpi, vaikka kaksi kolmesta ehdosta poistettaisiin.
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
    assert "uhrin" in str(exc.value)
    assert column in str(exc.value)


def test_a_whole_victim_passes_the_guard(parse_settings, archive, demo) -> None:
    """Vartijan toinen haara: ehjä uhri menee läpi.

    Ilman tätä edellinen testi todistaisi vain, että jokin kaataa ajon.
    """
    run_parse(parse_settings, archive, FakeParser(), demo)
    assert read_deaths(archive).height == 3


def test_the_integrity_guards_see_the_knife_round_too(
    parse_settings, archive, demo
) -> None:
    """Vartijat ajetaan **ennen** numerointia, eli koko adapterin tuotokselle.

    Puolinainen rivi tulee todennäköisimmin juuri lämmittelystä ja
    puukkokierrokselta -- ne ovat kierroksia, joilla pelin tila on epävakain.
    Numeroinnin jälkeen vartija katsoisi vain sitä osaa aineistoa, jossa
    vikaa ei odoteta.
    """
    rounds = build_rounds(played=2, warmup=2)
    # Rikotaan **vain** numeroimaton kierros: numeroinnin jälkeen ajettu
    # vartija ei näkisi tätä riviä lainkaan.
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
    assert "uhrin" in str(exc.value)


def test_the_attacker_guard_sees_the_knife_round_too(
    parse_settings, archive, demo
) -> None:
    """Sama ampujan vartijalle: pudotettu kierros ei saa piilottaa vikaa."""
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
    assert "ampujaa" in str(exc.value)


def test_a_death_without_a_time_sorts_last_in_the_written_table(
    parse_settings, archive, demo
) -> None:
    """Puuttuva aika ei ole nolla -- eikä parquetissa toisin kuin domainissa.

    ``deaths_for`` järjestää tyhjän ``t_s``:n viimeiseksi. Ilman
    ``nulls_last=True``:tä sama rivi johtaisi kierrostaan taulussa, ja sama
    asia järjestyisi kahdella eri tavalla riippuen kummasta katsoo.
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
    """Virheilmoitus ei arvaa syytä, kun luvut ovat kädessä.

    Adapteri erittelee jokaisen pudotussyyn, ja ne on laskettu ennen kuin
    tyhjyys havaitaan. Ilman niitä ilmoitus nimeäisi kaksi arvausta.
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
    assert "kierrosten ulkopuolella 7" in message
    assert "uhri puuttui 2" in message


def test_the_empty_table_error_says_so_when_every_counter_is_zero(
    parse_settings, archive, demo
) -> None:
    """Toinen haara: ilman pudotuksia syy on portti itse."""
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
    assert "ei tuottanut yhtään kuolemaa" in message
    assert "player_death" in message


def test_the_new_drop_counters_reach_the_stats_and_the_summary(
    parse_settings, archive, demo
) -> None:
    """Tickitön ja uhriton kuolema ovat eri syitä, ja molemmat näkyvät."""
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
    assert "Kuolema ilman tickiä" in text and "2 (" in text
    assert "Kuolema ilman uhria" in text and "3 (" in text


# --- Pistepilvi (Story 2.9) ------------------------------------------------------


def test_the_usable_row_count_has_exactly_one_source(
    parse_settings, archive, demo
) -> None:
    """Kelvolliset rivit ovat ruutujen havaintojen summa, eivät oma laskuri.

    Kaksi lähdettä samalle luvulle voi erkaantua: adapterin suodatin ja
    taulun summa antaisivat eri vastauksen heti, jos toista muutettaisiin.
    Portti ei siis raportoi lukua lainkaan -- vaihe laskee sen taulusta ja
    ``cli`` käyttää sitä suhteen osoittajana.
    """
    assert not hasattr(ParseDiagnostics(tick_rate=64.0,
                                        tick_rate_measured=True,
                                        rounds_seen=1),
                       "callout_cloud_rows_usable")
    result = run_parse(parse_settings, archive, FakeParser(), demo)
    table = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "callouts"))
    assert result.stats["callout_observations"] == int(table["observations"].sum())


def test_writes_a_valid_callout_cloud(parse_settings, archive, demo) -> None:
    """Hyväksymiskriteeri: ``callouts.parquet`` läpäisee ``validate``in.

    Taulu on räjähdysalueiden **lähde**, ja se kirjoitetaan juuri siksi:
    johdettu alue on tarkistettavissa demoa vasten vain, jos se mistä se
    johdettiin on tallessa.
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
    # Havainnot ovat ruudun omia lukuja, ei vakio: 10 + 11 + 12 + 13.
    assert result.stats["callout_observations"] == 46


def test_the_cloud_keeps_every_round_including_the_knife_round(
    parse_settings, archive, demo
) -> None:
    """Pistepilveä ei numeroida, joten sen rivit eivät putoa numeroinnissa.

    Pilvi on kartan ominaisuus tässä demossa eikä kierroksen havainto, ja
    lämmittelyn ja puukkokierroksen tickit kertovat kartasta yhtä paljon kuin
    pelattujen kierrosten. Sama sääntö kuin kokoonpanotaululla: taulussa ei
    ole ``round_no``-saraketta lainkaan.
    """
    rounds = build_rounds(played=3, warmup=2)
    run_parse(parse_settings, archive, FakeParser(rounds), demo)
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "callouts"))
    assert "round_no" not in df.columns
    assert "round_raw" not in df.columns
    assert df.height == 4


def test_the_cloud_rows_are_sorted_by_cell(parse_settings, archive, demo) -> None:
    """Sama demo tuottaa tavu tavulta saman tiedoston."""
    shuffled = build_callouts().sort("cell_x", descending=True)
    run_parse(
        parse_settings, archive, FakeParser(callouts=shuffled), demo
    )
    df = pl.read_parquet(archive.parsed_table(MAP_DEMO_ID, "callouts"))
    assert df["cell_x"].to_list() == sorted(df["cell_x"].to_list())


def test_an_empty_cloud_is_written_and_does_not_stop_the_run(
    parse_settings, archive, demo
) -> None:
    """I/O-matriisi: tyhjä pistepilvi -> ajo ei kaadu, luvut ovat nollia.

    Tyhjä kuolemataulu on virhe ja tyhjä pistepilvi ei -- ero on siinä mitä
    tyhjyys tarkoittaa. Kuolema ei ole valinta, mutta demo, jonka
    ``last_place_name`` jää tyhjäksi, on aidosti pilvetön. Sen seuraus
    (kaikki räjähdysalueet null) on oikea lopputulos, ja syy kerrotaan
    diagnostiikassa.
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
    assert "pistepilven" in str(exc.value)


def test_an_extra_callouts_column_is_a_contract_break_too(
    parse_settings, archive, demo
) -> None:
    broken = build_callouts().with_columns(pl.lit(1).alias("ylimaarainen"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "ylimaarainen" in str(exc.value)
    assert "pistepilven" in str(exc.value)


def test_a_duplicate_cell_is_refused(parse_settings, archive, demo) -> None:
    """Kaksi riviä samalle ruudulle tarkoittaisi, ettei moodivalinta toiminut.

    Räjähdys saisi alueensa sen mukaan, kumpi rivi sattui olemaan lähempänä
    lajittelussa, ja sama demo voisi antaa eri alueen eri ajolla. Skeema ei
    näe tätä: molemmat rivit ovat erikseen kelvollisia.
    """
    doubled = pl.concat([build_callouts(), build_callouts().head(1)])
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=doubled), demo)
    assert "cell_x, cell_y, cell_z" in str(exc.value)
    assert "kaksoiskappaleita" in str(exc.value)


def test_a_cell_without_an_area_is_refused(parse_settings, archive, demo) -> None:
    """Nimetön ruutu nimeäisi räjähdyksen tyhjäksi *kynnyksen sisällä*.

    Rivi näyttäisi siis samalta kuin "aluetta ei saatu", vaikka osuma oli
    hyvä. ``area`` on nullable-sarake, joten skeema päästäisi sen läpi.
    """
    nameless = build_callouts().with_columns(
        pl.lit(None, dtype=pl.Utf8).alias("area")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=nameless), demo)
    assert "ilman aluenimeä" in str(exc.value)


@pytest.mark.parametrize("observations", [0, -1, None])
def test_a_cell_without_observations_is_refused(
    parse_settings, archive, demo, observations
) -> None:
    """Ruutu syntyy vain havainnosta, joten nolla tarkoittaa keksittyä ruutua.

    ``Int32`` päästää nollan ja negatiivisen läpi, ja avaintarkistus katsoo
    vain koordinaatteja. Seuraus olisi hiljainen: ``callout_observations`` ja
    ajon kelpoisuussuhde näyttäisivät todellista pienemmiltä, eikä mikään
    kertoisi miksi.
    """
    broken = build_callouts().with_columns(
        pl.lit(observations, dtype=pl.Int32).alias("observations")
    )
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "yhtään havaintoa" in str(exc.value)


def test_a_cell_with_a_blank_area_is_refused(
    parse_settings, archive, demo
) -> None:
    """Pelkkä välilyönti ei ole aluenimi, vaikka se ei olekaan null.

    Tyhjä nimi nimeäisi räjähdyksen tyhjäksi *kynnyksen sisällä*, eli rivi
    näyttäisi samalta kuin "aluetta ei saatu" vaikka osuma oli hyvä.
    """
    broken = build_callouts().with_columns(pl.lit("   ").alias("area"))
    with pytest.raises(SchemaError) as exc:
        run_parse(parse_settings, archive, FakeParser(callouts=broken), demo)
    assert "ilman aluenimeä" in str(exc.value)


def test_a_missing_callouts_table_forces_a_reparse(
    parse_settings, archive, demo
) -> None:
    """Puolikas tulos ei ole ajantasainen tulos.

    Story 2.8:n arkisto on juuri tässä tilassa: manifesti täsmää, mutta
    ``callouts.parquet`` puuttuu. Ilman tarkistusta ajo ohitettaisiin ja
    käyttäjälle kerrottaisiin "Tulos on ajan tasalla".
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
    """Story 2.9:n oikea migraatiopolku: vanha ``snapped`` ei lataudu enumiin.

    Tämä on se tilanne, johon käyttäjä oikeasti törmää -- arkistossa on
    Story 2.8:n ``events.parquet``, jonka ``area_source`` on
    ``Enum(["observed", "snapped"])``. README, ``constants.py`` ja
    ``_schema_is_current`` lupaavat kaikki, ettei se kelpaa ja että demo
    parsitaan uudelleen **ilman** ``--pakota``-lippua. Muut testit kattavat
    puuttuvan taulun ja pudotetun sarakkeen; tämä kattaa väärän arvojoukon,
    joka on eri vika: sarakkeet ovat kohdallaan ja rivit luettavissa.
    """
    parser = FakeParser()
    run_parse(parse_settings, archive, parser, demo)
    assert parser.calls == 1

    table = archive.parsed_table(MAP_DEMO_ID, "events")
    old_enum = pl.Enum(["observed", "snapped"])
    stale = pl.read_parquet(table).with_columns(
        # Arvot palautetaan vanhaan sanastoon: juuri sellainen tiedosto
        # arkistossa on, kun se on kirjoitettu Story 2.8:n koodilla.
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
    # Ja uusi tulos on nykyisellä luettelolla.
    assert pl.read_parquet(table).schema["area_source"] == EVENTS["area_source"]


def test_a_callouts_table_that_no_longer_matches_the_contract_is_reparsed(
    parse_settings, archive, demo
) -> None:
    """Skeemamuutos ei liikuta parametrihashia, joten se on tarkistettava."""
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
    """Ruudut ja alueet ovat luettavissa valmiista taulusta, joten ne kerrotaan
    myös ohitetussa ajossa -- toisin kuin luetut tickirivit."""
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
    """Luetut ja kelvolliset rivit näkee vain lukuhetkellä.

    Valmis taulu kertoo, montako ruutua syntyi, muttei sitä mistä ne
    pelkistettiin -- eikä sitä, miksi pilvi jäi tyhjäksi.
    """
    from pappascout.cli import _render_parse

    parser = FakeParser(callouts=pl.DataFrame(schema=dict(CALLOUTS_ADAPTER_SCHEMA)))
    parser.diagnostics = ParseDiagnostics(
        tick_rate=64.0,
        tick_rate_measured=True,
        rounds_seen=3,
        callout_cloud_rows_read=1529910,
        callout_cloud_empty_reason="1529910 tickiriviä luettiin, mutta "
        "yhdelläkään ei ollut elossa olevaa pelaajaa nimetyllä alueella",
    )
    result = run_parse(parse_settings, archive, parser, demo)
    assert result.stats["callout_cloud_rows_read"] == 1529910
    assert result.stats["callout_cloud_empty_reason"].startswith("1529910 tickiriviä")
    # Kelvolliset rivit tulevat taulusta, eivät toisesta laskurista.
    assert result.stats["callout_observations"] == 0

    text = _render_parse(result, regulation_rounds=24)
    assert "tyhjä -- yhtäkään räjähdysaluetta ei nimetä" in text
    assert "yhdelläkään ei ollut elossa olevaa pelaajaa" in text


def test_the_detonation_area_coverage_and_distance_reach_the_stats(
    parse_settings, archive, demo
) -> None:
    """Kattavuus ja etäisyysjakauma lasketaan valmiista taulusta joka ajolla.

    Ne ovat Story 2.9:n mittarit, eikä niitä saa jättää kalibroinnin varaan:
    kynnys voi vanhentua uuden kartan myötä, ja silloin sen kuuluu näkyä
    ajossa eikä vasta raportissa.
    """
    rounds = build_rounds(played=2, warmup=0)
    events = build_events(rounds)
    # Kaksi räjähdystä neljästä jää kynnyksen taakse: alue null, etäisyys
    # tallessa. Se on I/O-matriisin rivi "räjähdys kaukana".
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
    """Mediaani, p90 ja suurin ovat **kolme eri lukua**, eivät sama kolmesti.

    Ilman tätä kiinnikettä ``p90`` on laskettu mutta havaitsematon: sen voi
    vaihtaa ``quantile(0.3)``:een eikä yksikään väite kaadu, koska muut
    testit joko purkavat sen muuttujaan käyttämättä sitä tai vertaavat
    tulosteessa kovakoodattuun literaaliin. Ja juuri p90 on se luku, jota
    asetusten docstringit myyvät ainoana ajokohtaisena todisteena kynnyksen
    kalibroinnista.

    Etäisyydet ovat 10, 20, ..., 200 (20 räjähdystä): mediaani 105, p90
    (lähin havainto) 180 ja suurin 200. Kolme eri lukua, ja mikä tahansa muu
    kvantiili antaisi eri tuloksen -- ``quantile(0.3)`` antaisi 60.
    """
    rounds = build_rounds(played=5, warmup=0)
    events = build_events(rounds)
    detonations = pl.col("event_kind") == "grenade_detonate"
    # 20 räjähdystä, etäisyydet 10..200 nousevassa järjestyksessä.
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
    # Kolme eri lukua: jos kaksi olisi sama, väite ei erottaisi kvantiileja.
    assert len({median, p90, largest}) == 3
