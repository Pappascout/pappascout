"""``stages.parse`` -- vaiheen testit ilman demoja.

Vaihe näkee demon vain portin takaa (AD-8), joten sen koko logiikka -- taulun
validointi, atominen kirjoitus, manifesti ja ohitus -- testataan feikillä, joka
rakentaa kierrostaulun käsin. Yksikään näistä testeistä ei tarvitse
demotiedostoa.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl
import pytest

from conftest import has_temp_leftovers, settings_text
from pappascout.adapters.protocols import ROUNDS_ADAPTER_COLUMNS
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.models import load_settings
from pappascout.domain.schemas import ROUNDS
from pappascout.errors import DemoUnavailable, PappascoutError, ParseError, SchemaError
from pappascout.stages import parse as parse_stage

MAP_DEMO_ID = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"

#: Portin sopimuksen tyypit: ``ROUNDS`` täydennettynä numerointisarakkeilla.
ADAPTER_SCHEMA: dict[str, object] = {
    name: ROUNDS.get(name, pl.Int32) for name in ROUNDS_ADAPTER_COLUMNS
}


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


class FakeParser:
    """Portin toteutus, joka ei koske demoparser2:een."""

    def __init__(
        self, frame: pl.DataFrame | None = None, virhe: Exception | None = None
    ):
        self.frame = frame if frame is not None else build_rounds()
        self.virhe = virhe
        self.kutsut = 0

    def parse_rounds(self, path: Path) -> pl.DataFrame:
        self.kutsut += 1
        if self.virhe is not None:
            raise self.virhe
        return self.frame


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
    assert manifest.outputs == ["parsed/" + MAP_DEMO_ID + "/rounds.parquet"]
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
