"""Demoadapterin testit: purku, otsikkotarkistus ja oikea demo.

Kaksi kerrosta:

* **Ilman demoja** -- purku, tunnistus ja virheilmoitukset testataan pienillä
  itse tehdyillä tiedostoilla. Nämä ajetaan aina.
* **Oikealla demolla** (``@pytest.mark.demo``) -- kierrosmäärä, jatkoaika ja
  purun tavuvastaavuus. Nämä ohittavat itsensä, jos 100-230 MB:n demoja ei ole
  koneella.
"""

from __future__ import annotations

import gzip
import hashlib
import math
from functools import lru_cache
from pathlib import Path

import polars as pl
import pytest
import zstandard

from conftest import (
    ANCIENT_DEM,
    ANCIENT_ROUNDS,
    ANCIENT_ZST,
    NUKE_ROUNDS,
    NUKE_ZST,
    REAL_SETTINGS,
    require_demo,
)
from pappascout.adapters.decompress import (
    DEMO_MAGIC,
    decompressed_name,
    check_demo_magic,
    decompress_to,
    is_compressed,
    readable_demo,
)
from pappascout.adapters.demo_parser import Demoparser2Adapter
from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoParser,
)
from pappascout.domain.rounds import CT_WIN_REASONS, T_WIN_REASONS
from pappascout.domain.rounds import REQUIRED_COLUMNS as NUMBERING_COLUMNS
from pappascout.domain.rounds import check_win_reasons, mark_played_rounds
from pappascout.domain.models import load_settings
from pappascout.domain.schemas import EVENTS, ROUNDS, TICKS
from pappascout.errors import ParseError

FAKE_DEMO = DEMO_MAGIC + b"\x00" + b"tekaistua sisaltoa" * 64

#: Näytepisteet, joita demotestit käyttävät. Sama lista kuin ``settings.toml``in
#: ``[parse]``-osiossa; :func:`test_snapshot_seconds_match_the_real_settings`
#: pitää huolen siitä, etteivät ne pääse erkanemaan. Vakiona eikä
#: asetuslatauksena, jottei moduulin tuonti lue tiedostoja -- se tapahtuisi
#: myös ``-m "not demo"`` -ajossa, jossa mitään demoa ei kosketa.
SNAPSHOT_SECONDS: tuple[float, ...] = (6.0, 15.0, 30.0, 45.0)


@lru_cache(maxsize=1)
def _parse_settings():
    """Oikeat ``[parse]``-asetukset, luettuna vasta kun niitä tarvitaan."""
    return load_settings(REAL_SETTINGS, env_files=()).parse


def real_parser() -> Demoparser2Adapter:
    """Adapteri tuotannon ensikontakti- ja aluesäännöillä.

    Demotestit ajetaan tuotannon arvoilla -- keksityillä poissulkulistoilla tai
    keksityllä ``area_snap_units``illa ne eivät todistaisi mitään oikeasta
    ajosta.
    """
    parse_settings = _parse_settings()
    return Demoparser2Adapter(
        exclude_weapons=parse_settings.first_contact_exclude_weapons,
        fallback_death=parse_settings.first_contact_fallback_death,
        area_snap_units=parse_settings.area_snap_units,
    )


def test_snapshot_seconds_match_the_real_settings() -> None:
    """Testien näytepisteet ovat samat kuin tuotannon.

    Jos ne erkanisivat, demotestien luvut (94 näytepistettä) mittaisivat eri
    konfiguraatiota kuin se, jolla arkisto oikeasti syntyy.
    """
    assert tuple(_parse_settings().snapshot_seconds) == SNAPSHOT_SECONDS


# --- Portti -------------------------------------------------------------------


def test_adapter_implements_the_port() -> None:
    assert isinstance(Demoparser2Adapter(), DemoParser)


def test_port_contract_is_an_exact_column_set() -> None:
    """Sopimus on täsmällinen joukko, ei osajoukko.

    ``map_demo_id`` puuttuu, koska adapteri ei voi tietää arkiston tunnistetta.
    ``score_start`` ja ``score_end`` ovat mukana, koska
    ``mark_played_rounds`` vaatii ne -- ilman niitä sopimuksessa toinen
    adapteri läpäisisi vaiheen saraketarkistuksen ja kaatuisi vasta
    domain-kerroksessa.
    """
    assert set(ROUNDS_ADAPTER_COLUMNS) == (set(ROUNDS) - {"map_demo_id"}) | {
        "score_start",
        "score_end",
    }
    assert set(NUMBERING_COLUMNS) <= set(ROUNDS_ADAPTER_COLUMNS)
    assert len(ROUNDS_ADAPTER_COLUMNS) == len(set(ROUNDS_ADAPTER_COLUMNS))


def test_events_port_contract_is_events_without_the_archive_id() -> None:
    """Tapahtumataulun sopimus on ``EVENTS`` ilman ``map_demo_id``:tä.

    Kranaatin oma juokseva numero ei ole mukana: se on adapterin sisäinen
    parin avain, joka kuolee ennen kuin taulu ylittää portin.
    """
    assert set(EVENTS_ADAPTER_COLUMNS) == set(EVENTS) - {"map_demo_id"}
    assert len(EVENTS_ADAPTER_COLUMNS) == len(set(EVENTS_ADAPTER_COLUMNS))


def test_ticks_port_contract_is_ticks_without_the_archive_id() -> None:
    """Näytepistetaulun sopimus on ``TICKS`` ilman ``map_demo_id``:tä.

    Kaikki muu on mukana, myös ``round_no`` -- se on adapterin taulussa aina
    tyhjä, mutta sen paikka on varattu, jotta vaihe voi täyttää sen ilman että
    sarakkeiden järjestys muuttuu.
    """
    assert set(TICKS_ADAPTER_COLUMNS) == set(TICKS) - {"map_demo_id"}
    assert len(TICKS_ADAPTER_COLUMNS) == len(set(TICKS_ADAPTER_COLUMNS))


# --- Tunnistus ja purku --------------------------------------------------------


def test_plain_demo_is_not_compressed(tmp_path: Path) -> None:
    path = tmp_path / "a.dem"
    path.write_bytes(FAKE_DEMO)
    assert not is_compressed(path)


def test_zstd_and_gzip_are_recognised_from_content_not_suffix(tmp_path: Path) -> None:
    """Väärin nimetty tiedosto tunnistetaan silti oikein."""
    zst = tmp_path / "vaarin-nimetty.dem"
    zst.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    gz = tmp_path / "toinen.dem"
    gz.write_bytes(gzip.compress(FAKE_DEMO))
    assert is_compressed(zst)
    assert is_compressed(gz)


def test_zstd_round_trip_is_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "a.dem.zst"
    source.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    target = decompress_to(source, tmp_path / "ulos.dem")
    assert target.read_bytes() == FAKE_DEMO


def test_gzip_round_trip_is_byte_identical(tmp_path: Path) -> None:
    source = tmp_path / "a.dem.gz"
    source.write_bytes(gzip.compress(FAKE_DEMO))
    target = decompress_to(source, tmp_path / "ulos.dem")
    assert target.read_bytes() == FAKE_DEMO


def test_readable_demo_cleans_up_the_temp_file(tmp_path: Path) -> None:
    source = tmp_path / "a.dem.zst"
    source.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    with readable_demo(source) as decompressed:
        assert decompressed.read_bytes() == FAKE_DEMO
        temp_path = decompressed
    assert not temp_path.exists()
    assert not temp_path.parent.exists()


def test_readable_demo_does_not_copy_an_uncompressed_demo(tmp_path: Path) -> None:
    source = tmp_path / "a.dem"
    source.write_bytes(FAKE_DEMO)
    with readable_demo(source) as path:
        assert path == source


def test_decompression_never_writes_into_the_archive(tmp_path: Path) -> None:
    """Purku menee koneen temp-hakemistoon, ei OneDrive-arkistoon."""
    archive_dir = tmp_path / "arkisto"
    archive_dir.mkdir()
    source = archive_dir / "import" / "a.dem.zst"
    source.parent.mkdir()
    source.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    with readable_demo(source) as decompressed:
        assert archive_dir not in decompressed.parents
    assert list(archive_dir.rglob("*.dem")) == []


# --- Virheet -------------------------------------------------------------------


def test_text_file_with_dem_suffix_is_a_finnish_error(tmp_path: Path) -> None:
    path = tmp_path / "eidemo.dem"
    path.write_text("Tämä on tekstitiedosto, ei demo.\n", encoding="utf-8")
    with pytest.raises(ParseError) as exc:
        check_demo_magic(path)
    message = str(exc.value)
    assert "PBDEMS2" in message
    assert "ei ole CS2-demo" in message


def test_text_file_fails_before_demoparser_is_called(tmp_path: Path) -> None:
    path = tmp_path / "eidemo.dem"
    path.write_text("ei demo", encoding="utf-8")
    with pytest.raises(ParseError, match="PBDEMS2"):
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds


def test_missing_file_is_a_finnish_error(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(
            tmp_path / "ei-ole.dem", SNAPSHOT_SECONDS
        )
    assert "ei löytynyt" in str(exc.value)


def test_broken_zstd_is_a_finnish_error(tmp_path: Path) -> None:
    intact = zstandard.ZstdCompressor().compress(FAKE_DEMO)
    truncated = tmp_path / "katkennut.dem.zst"
    truncated.write_bytes(intact[: len(intact) // 2])
    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(truncated, SNAPSHOT_SECONDS).rounds
    assert "purku epäonnistui" in str(exc.value)


def test_truncated_demo_is_a_finnish_error(tmp_path: Path) -> None:
    """Otsikko on oikea mutta sisältö loppuu kesken -- demoparser2 kaatuu."""
    path = tmp_path / "katkennut.dem"
    path.write_bytes(FAKE_DEMO)
    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds
    assert "Lataa demo uudelleen" in str(exc.value) or "katkennut" in str(exc.value)


def test_zstd_compressed_error_page_is_refused(tmp_path: Path) -> None:
    """FACEIT voi palauttaa latauslinkin takaa virhesivun.

    Se pakkautuu moitteettomasti zstd-tiedostoksi, joten purku onnistuu --
    virheen on tultava vasta puretun sisällön otsikkotarkistuksesta.
    """
    error_page = b"<html><head><title>404</title></head><body>Not Found</body></html>"
    path = tmp_path / "lataus.dem.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(error_page))

    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds
    message = str(exc.value)
    assert "PBDEMS2" in message
    assert "ei ole CS2-demo" in message


def test_gzip_compressed_error_page_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "lataus.dem.gz"
    path.write_bytes(gzip.compress(b"<html>403 Forbidden</html>"))
    with pytest.raises(ParseError, match="PBDEMS2"):
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds


@pytest.mark.parametrize(
    "name,expected",
    [
        ("1-abc-1-1.dem.zst", "1-abc-1-1.dem"),
        ("1-abc-1-1.dem.gz", "1-abc-1-1.dem"),
        ("1-abc-1-1.dem", "1-abc-1-1.dem"),
        ("ottelu.2026.01.01.dem.zst", "ottelu.2026.01.01.dem"),
        ("ilman-paatetta.zst", "ilman-paatetta.dem"),
    ],
)
def test_decompressed_name_keeps_the_whole_name(name: str, expected: str) -> None:
    """Nimeä ei katkaista ensimmäisestä pisteestä.

    FACEITin tiedostonimissä on useita pisteitä, ja katkaisu tuottaisi eri
    demoille helposti saman purkunimen.
    """
    assert decompressed_name(Path("/x") / name) == expected


def test_partial_decompression_leaves_no_tmp_file(tmp_path: Path) -> None:
    """Keskeytynyt purku ei saa jättää tiedostoa, joka näyttäisi demolta."""
    intact = zstandard.ZstdCompressor().compress(FAKE_DEMO)
    truncated = tmp_path / "katkennut.dem.zst"
    truncated.write_bytes(intact[: len(intact) // 2])
    target = tmp_path / "ulos" / "katkennut.dem"

    with pytest.raises(ParseError):
        decompress_to(truncated, target)
    assert not target.exists()
    assert list(target.parent.glob("*.tmp")) == []


# --- Oikeat demot --------------------------------------------------------------


@pytest.mark.demo
def test_ancient_has_twenty_one_played_rounds() -> None:
    df = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    played = mark_played_rounds(df).filter(pl.col("round_no").is_not_null())
    assert played["round_no"].n_unique() == ANCIENT_ROUNDS
    assert played.height == ANCIENT_ROUNDS * 2
    assert sorted(played["round_no"].unique().to_list()) == list(
        range(1, ANCIENT_ROUNDS + 1)
    )


@pytest.mark.demo
def test_ancient_columns_match_the_port_contract() -> None:
    df = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    assert tuple(df.columns) == ROUNDS_ADAPTER_COLUMNS
    for name, dtype in ROUNDS.items():
        if name == "map_demo_id":
            continue
        assert df.schema[name] == dtype, name
    # round_no jätetään tyhjäksi: numeroinnin päättää domain.rounds.
    assert df["round_no"].null_count() == df.height


@pytest.mark.demo
def test_ancient_knife_round_is_present_but_unnumbered() -> None:
    """Puukkokierros on demossa, mutta se ei ole pelattu kierros."""
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    )
    unnumbered = df.filter(pl.col("round_no").is_null())
    assert unnumbered.height == 2  # yksi rivi kummallekin joukkueelle
    assert unnumbered["round_raw"].unique().to_list() == [1]


@pytest.mark.demo
def test_ancient_observations_are_plausible() -> None:
    """Havaitut arvot ovat oikeasta demosta, eivät johdettuja tai tyhjiä."""
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert df["tick_rate"].unique().to_list() == [64.0]
    assert df["lineup_key"].n_unique() == 2
    assert set(df["side"].unique().to_list()) == {"T", "CT"}
    assert df["won"].null_count() == 0
    assert df["win_reason"].null_count() == 0
    assert df["money_freeze_end"].null_count() == 0
    assert df["equip_freeze_end"].null_count() == 0
    assert df["survivors"].is_between(0, 5).all()
    assert df["status"].unique().to_list() == ["ok"]

    # Kummallakin kierroksella on täsmälleen yksi voittaja.
    per_round = df.group_by("round_no").agg(pl.col("won").sum().alias("voittajia"))
    assert per_round["voittajia"].unique().to_list() == [1]

    # Ancient päättyi 13-8 (FACEIT). Voitot jakautuvat siten kokoonpanoittain.
    wins = sorted(
        df.group_by("lineup_key").agg(pl.col("won").sum())["won"].to_list()
    )
    assert wins == [8, 13]


@pytest.mark.demo
def test_ancient_pistol_round_shows_a_pistol_economy() -> None:
    """Kierros 1 on pistoolikierros: varustearvo on murto-osa täydestä."""
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no") == 1)
    assert df.height == 2
    # 5 pelaajaa x (pistooli 200 + kevlar 650..1000) -> selvästi alle 10 000 $.
    assert df["equip_freeze_end"].max() < 10_000
    assert df["equip_freeze_end"].min() > 0


@pytest.mark.demo
def test_zst_and_dem_give_byte_identical_tables(tmp_path: Path) -> None:
    """Sama demo pakattuna ja purettuna tuottaa samat taulut tavu tavulta."""
    dem = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)
    zst = real_parser().parse_demo(require_demo(ANCIENT_ZST), SNAPSHOT_SECONDS)
    decompressed, compressed = dem.rounds, zst.rounds
    assert decompressed.equals(compressed)
    assert dem.ticks.equals(zst.ticks)

    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    decompressed.write_parquet(a)
    compressed.write_parquet(b)
    assert hashlib.sha256(a.read_bytes()).hexdigest() == (
        hashlib.sha256(b.read_bytes()).hexdigest()
    )


@pytest.mark.demo
def test_nuke_reaches_round_twenty_eight_in_overtime() -> None:
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(NUKE_ZST), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())
    assert df["round_no"].max() == NUKE_ROUNDS
    assert df.height == NUKE_ROUNDS * 2
    # Jatkoajan kierrokset 25-28 ovat mukana normaalisti.
    overtime = df.filter(pl.col("round_no") > 24)
    assert sorted(overtime["round_no"].unique().to_list()) == [25, 26, 27, 28]
    assert overtime["won"].null_count() == 0


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name,expected_rounds",
    [(ANCIENT_DEM, ANCIENT_ROUNDS), (NUKE_ZST, NUKE_ROUNDS)],
)
def test_real_demos_obey_the_cs2_win_rule(
    demo_name: str, expected_rounds: int
) -> None:
    """CS2:n sääntö pitää molemmissa oikeissa demoissa.

    T voittaa vain eliminoimalla CT:t tai räjäyttämällä pommin; CT
    eliminoimalla, purkamalla tai ajan loppuessa. Jos tämä pettäisi, puolet
    olisivat menneet väärin päin ja jokainen havainto olisi väärällä
    joukkueella.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert df["round_no"].n_unique() == expected_rounds
    check_win_reasons(df)  # nostaa ParseErrorin, jos sääntö pettää

    wins = df.filter(pl.col("won"))
    for side, allowed in (("T", T_WIN_REASONS), ("CT", CT_WIN_REASONS)):
        reasons = set(wins.filter(pl.col("side") == side)["win_reason"].unique())
        assert reasons <= set(allowed), (side, reasons)


@pytest.mark.demo
def test_round_raw_is_the_demo_own_counter() -> None:
    """``round_raw`` tulee ``round_end``-tapahtuman ``round``-kentästä.

    Ancientissa puukkokierros on demon kierros 1, joten pelatut kierrokset
    1..21 vastaavat raaka-arvoja 2..22. Aukko on nimenomaan se todiste, että
    puukkokierros ohitettiin.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    )
    played = df.filter(pl.col("round_no").is_not_null())
    assert sorted(played["round_raw"].unique().to_list()) == list(range(2, 23))
    assert df.filter(pl.col("round_no").is_null())["round_raw"].unique().to_list() == [1]


# --- Näytepisteet oikeasta demosta ---------------------------------------------

#: Ancientin ``env_cs_place``-alueet, luettu demosta 2026-08-29. Pelin omat
#: alueet ovat noin kaksi kertaa karkeampia kuin Total CS -calloutit: esimerkiksi
#: A-sitelle johtava Ramp on omansa, mutta Donut ja Cave sulautuvat naapureihin.
#: Lista on tarkoituksella kiinteä -- demosta johdettu joukko hyväksyisi minkä
#: tahansa nimen ja lakkaisi olemasta tarkistus.
ANCIENT_PLACES: frozenset[str] = frozenset(
    {
        "Alley",
        "BombsiteA",
        "BombsiteB",
        "CTSpawn",
        "House",
        "MainHall",
        "Middle",
        "Outside",
        "Ramp",
        "Ruins",
        "SideEntrance",
        "SideHall",
        "TSideLower",
        "TSideUpper",
        "TSpawn",
        "TopofMid",
        "Tunnel",
        "Water",
    }
)

#: Ancientin T-puolen alueet. CT-pelaaja ei voi olla näissä kuuden sekunnin
#: kohdalla; jos on, puolet ovat menneet väärin päin ja jokainen asetelma
#: olisi kohdistettu väärälle joukkueelle.
T_SIDE_PLACES: frozenset[str] = frozenset(
    {"TSpawn", "TSideUpper", "TSideLower", "Outside", "Tunnel"}
)


@pytest.fixture(scope="module")
def ancient_tables():
    """Ancient-demon taulut ja diagnostiikka. Parsitaan kerran, ei testiä kohden."""
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)
    return tables, adapter.diagnostics


@pytest.fixture(scope="module")
def ancient_ticks(ancient_tables) -> pl.DataFrame:
    """Ancient-demon näytepistetaulu."""
    return ancient_tables[0].ticks


@pytest.mark.demo
def test_ancient_ticks_match_the_port_contract(ancient_ticks: pl.DataFrame) -> None:
    assert tuple(ancient_ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ancient_ticks.schema[name] == TICKS[name], name
    assert ancient_ticks["round_no"].null_count() == ancient_ticks.height
    assert not ancient_ticks.is_empty()


@pytest.mark.demo
def test_ancient_samples_ten_players_at_every_point(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Kaikki kymmenen tallennetaan joka näytepisteessä, myös kuolleet.

    Puukkokierros (``round_raw`` 1) on poikkeus: siinä yksi pelaaja ei ollut
    vielä liittynyt joukkueeseen, joten rivejä on yhdeksän. Kierros ei ole
    pelattu eikä päädy arkistoon, joten poikkeus ei näy tuloksessa -- mutta
    sitä ei myöskään paikata keksimällä kymmenettä riviä.
    """
    played = ancient_ticks.filter(pl.col("round_raw") > 1)
    per_point = played.group_by("round_raw", "sample_kind", "sample_t_s").len()
    assert per_point["len"].unique().to_list() == [10]
    assert played["round_raw"].n_unique() == ANCIENT_ROUNDS


@pytest.mark.demo
def test_ancient_has_no_sample_after_the_round_ended(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Hyväksymiskriteeri: lyhyt kierros ei saa 45 sekunnin pistettä.

    Todiste on siinä, että aikapisteiden määrä **vaihtelee** kierroksittain:
    jos kaikilla olisi neljä, rajausta ei tapahtuisi lainkaan.
    """
    time_samples = ancient_ticks.filter(pl.col("sample_kind") == "time")
    per_round = time_samples.group_by("round_raw").agg(
        pl.col("sample_t_s").n_unique().alias("pisteita")
    )
    counts = set(per_round["pisteita"].to_list())
    assert counts <= set(range(1, len(SNAPSHOT_SECONDS) + 1))
    assert len(counts) > 1, "yksikään kierros ei jäänyt lyhyeksi -- rajaus ei purrut"
    assert set(time_samples["sample_t_s"].unique().to_list()) <= set(SNAPSHOT_SECONDS)


@pytest.mark.demo
def test_ancient_areas_are_real_callouts(ancient_ticks: pl.DataFrame) -> None:
    """Hyväksymiskriteeri: kierroksen 1 alueet ovat Ancientin callouteja.

    ``round_raw`` 2 on pelattu kierros 1 (puukkokierros on 1). Alueiden nimet
    ovat pelin omia ``env_cs_place``-nimiä, ja niiden on kuuluttava Ancientin
    nimijoukkoon -- pelkkä "ei tyhjä" menisi läpi myös väärältä kartalta
    luetuilla nimillä tai steamideilla.
    """
    first_round = ancient_ticks.filter(pl.col("round_raw") == 2)
    areas = {a for a in first_round["area"].to_list() if a}
    assert areas, "kierroksen 1 alueet olivat tyhjiä"
    assert areas <= ANCIENT_PLACES, sorted(areas - ANCIENT_PLACES)
    # CT-pelaajien on oltava CT-puolen alueilla kierroksen alussa: kuuden
    # sekunnin kohdalla kukaan ei ole vielä ehtinyt T-puolen alueille.
    ct_at_start = first_round.filter(
        (pl.col("side") == "CT") & (pl.col("sample_t_s") == min(SNAPSHOT_SECONDS))
    )
    assert ct_at_start.height == 5
    assert not (set(ct_at_start["area"].to_list()) & T_SIDE_PLACES), (
        "CT-pelaaja oli T-puolen alueella kuuden sekunnin kohdalla -- puolet "
        "ovat todennäköisesti väärin päin"
    )
    # Ja sama toisin päin: T:t eivät ole ehtineet CT-spawniin.
    t_at_start = first_round.filter(
        (pl.col("side") == "T") & (pl.col("sample_t_s") == min(SNAPSHOT_SECONDS))
    )
    assert t_at_start.height == 5
    assert "CTSpawn" not in t_at_start["area"].to_list()


@pytest.mark.demo
def test_ancient_uses_only_ancient_place_names(ancient_ticks: pl.DataFrame) -> None:
    """Koko demon alueiden on oltava Ancientin nimiä, ei vain kierroksen 1."""
    areas = {a for a in ancient_ticks["area"].to_list() if a}
    assert areas <= ANCIENT_PLACES, sorted(areas - ANCIENT_PLACES)
    assert len(areas) > 5, "vain muutama alue -- näytteistys osunee samaan hetkeen"


@pytest.mark.demo
def test_ancient_coordinates_are_present_even_without_an_area(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Tuntematon alue jää nulliksi, mutta riviä ei pudoteta."""
    assert ancient_ticks["x"].null_count() == 0
    assert ancient_ticks["y"].null_count() == 0
    unnamed = ancient_ticks.filter(pl.col("area").is_null())
    if not unnamed.is_empty():
        assert unnamed["x"].null_count() == 0


@pytest.mark.demo
def test_ancient_first_contact_is_found_on_every_round(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Ensikontakti löytyy Ancientissa jokaiselta kierrokselta.

    ``settings.toml`` perustelee ``planted_c4``:n poisjätön sillä, että
    ensikontakti löytyy ilman sitä joka kierrokselta. Tämä testi on se väite:
    jos se pettää, kommentti on väärässä eikä toisin päin.
    """
    contacts = ancient_ticks.filter(pl.col("sample_kind") == "first_contact")
    round_count = ancient_ticks["round_raw"].n_unique()
    assert contacts["round_raw"].n_unique() == round_count
    # sample_t_s ja t_s kertovat saman hetken, eivät jää tyhjiksi.
    assert contacts["sample_t_s"].null_count() == 0
    assert (contacts["sample_t_s"] == contacts["t_s"]).all()
    # Kontakti tapahtuu kierroksen sisällä, ei ennen ankkuria.
    assert contacts["t_s"].min() > 0


@pytest.mark.demo
def test_ancient_sample_point_count_is_exact(ancient_ticks: pl.DataFrame) -> None:
    """Ancientin näytepisteiden tarkka määrä -- README lainaa tätä lukua.

    21 pelattua kierrosta ja neljä näytepistettä antaisi 84 aikapistettä, mutta
    kierroksen päättymisen jälkeisiä pisteitä ei ole: todellinen luku on 73.
    Ensikontakteja on yksi per kierros, ja puukkokierros (``round_raw`` 1) tuo
    omansa päälle -- yhteensä 94 näytepistettä 21 pelatulta kierrokselta.
    """
    played = ancient_ticks.filter(pl.col("round_raw") > 1)
    point_count = played.select("round_raw", "sample_kind", "sample_t_s").n_unique()
    time_point_count = (
        played.filter(pl.col("sample_kind") == "time")
        .select("round_raw", "sample_t_s")
        .n_unique()
    )
    contact_count = (
        played.filter(pl.col("sample_kind") == "first_contact")
        .select("round_raw")
        .n_unique()
    )
    assert time_point_count == 73
    assert contact_count == ANCIENT_ROUNDS == 21
    assert point_count == 94
    assert point_count < ANCIENT_ROUNDS * (len(SNAPSHOT_SECONDS) + 1)
    assert played.height == 940


@pytest.mark.demo
def test_ancient_reports_no_partial_samples(ancient_tables) -> None:
    """Pelatuilla kierroksilla ei ole vajaita näytepisteitä.

    Vajaat ovat puukkokierroksen molemmat näytepisteet -- sen 6 sekunnin piste
    ja sen ensikontakti -- joissa yksi pelaaja ei ollut vielä liittynyt
    joukkueeseen. Kaksi on siis oikea luku; suurempi tarkoittaisi propivikaa.
    Puukkokierros ei ole pelattu eikä päädy arkistoon, joten vajaus ei näy
    tuloksessa; se näkyy vain tässä luvussa, ja siinä se kuuluukin näkyä.
    """
    diagnostics_obj = ancient_tables[1]
    assert diagnostics_obj.partial_samples == 2
    assert diagnostics_obj.unknown_side_events == 0


@pytest.mark.demo
def test_ancient_alive_flag_thins_out_over_the_round(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Elossaolo on havainto: myöhemmällä pisteellä elossa on vähemmän."""
    time_samples = ancient_ticks.filter(pl.col("sample_kind") == "time")
    per_point = (
        time_samples.group_by("sample_t_s")
        .agg(pl.col("is_alive").mean().alias("osuus"))
        .sort("sample_t_s")
    )
    shares = per_point["osuus"].to_list()
    assert shares[0] == 1.0, "ensimmäisellä pisteellä kaikkien pitäisi olla elossa"
    assert shares[-1] < shares[0]


# --- Utility oikeasta demosta --------------------------------------------------

#: Ancientin utility-luvut, mitattu 2026-08-29. Kiinteät luvut eivät ole
#: itsetarkoitus: ne ovat ainoa tapa huomata, jos lentoratojen jaksotus alkaa
#: yhdistää tai katkaista kranaatteja väärin. Yksikin virhe siirtäisi näitä.
ANCIENT_GRENADES = 373
ANCIENT_GRENADE_TYPES: dict[str, int] = {
    "smoke": 96,
    "flashbang": 95,
    "he": 90,
    "incendiary": 62,
    "molotov": 30,
}
#: ``[parse].area_snap_units``, jolla :data:`ANCIENT_DETONATIONS_WITH_AREA` on
#: mitattu. Luku ei tarkoita mitään ilman rajaa, joten testi tarkistaa
#: esiehdon eikä oleta sitä.
CALIBRATED_SNAP_UNITS = 500

#: CS2:n kierrosaika ja pommin ajastin sekunteina. Kierros voi jatkua näiden
#: summan verran ankkurista, joten heiton t_s ei voi ylittää sitä.
ROUND_SECONDS = 115.0
BOMB_SECONDS = 40.0

#: Räjähdykset, joille lähin elossa oleva pelaaja oli enintään
#: :data:`CALIBRATED_SNAP_UNITS`in päässä.
#:
#: Kalibrointimittaus antoi 178/374, mutta taulussa luku on pienempi kahdesta
#: syystä: yksi kranaatti lähtee kierrosten ulkopuolella, ja
#: :data:`ANCIENT_DETONATIONS_AFTER_ROUND` räjähdystä osuu kierroksen
#: päättymisen jälkeen, jolloin aluetta ei napsauteta lainkaan -- pelaajat
#: ovat jo seuraavan kierroksen spawnissa.
ANCIENT_DETONATIONS_WITH_AREA = 170

#: Räjähdykset kierroksen päättymisen jälkeen. Käytännössä savuja, jotka
#: haihtuvat vasta seuraavan ostoajan puolella.
ANCIENT_DETONATIONS_AFTER_ROUND = 22

#: Demon omat räjähdystapahtumat ja niitä vastaava kanoninen tyyppi.
#: ``inferno_startburn`` puuttuu listalta tarkoituksella: palo syntyy **eri**
#: entiteettinä muutama tick radan päättymisen jälkeen, joten sen paikka ei ole
#: sama piste vaan lähellä sitä.
DETONATE_EVENTS: tuple[tuple[str, str], ...] = (
    ("smokegrenade_detonate", "smoke"),
    ("hegrenade_detonate", "he"),
    ("flashbang_detonate", "flashbang"),
)


@pytest.fixture(scope="module")
def ancient_events(ancient_tables) -> pl.DataFrame:
    """Ancient-demon utility-tapahtumataulu."""
    return ancient_tables[0].events


@pytest.mark.demo
def test_ancient_events_match_the_port_contract(ancient_events: pl.DataFrame) -> None:
    assert tuple(ancient_events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert ancient_events.schema[name] == EVENTS[name], name
    assert ancient_events["round_no"].null_count() == ancient_events.height
    assert not ancient_events.is_empty()


@pytest.mark.demo
def test_ancient_grenade_count_is_exact(ancient_events: pl.DataFrame) -> None:
    """Hyväksymiskriteeri: jokaisesta kranaatista heitto ja räjähdys."""
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = ancient_events.filter(pl.col("event_kind") == "grenade_detonate")
    assert throws.height == ANCIENT_GRENADES
    assert detonations.height == ANCIENT_GRENADES
    assert ancient_events.height == 2 * ANCIENT_GRENADES


@pytest.mark.demo
def test_ancient_grenade_types_are_plausible(ancient_events: pl.DataFrame) -> None:
    """Savut, flashit, HE:t ja tulikranaatit uskottavina määrinä."""
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    counts_by_type = {
        row["grenade_type"]: row["len"]
        for row in throws.group_by("grenade_type").len().iter_rows(named=True)
    }
    assert counts_by_type == ANCIENT_GRENADE_TYPES


@pytest.mark.demo
def test_ancient_fire_grenades_follow_the_side_that_can_buy_them(
    ancient_events: pl.DataFrame,
) -> None:
    """Molotov on T:n ja incendiary CT:n ase -- erottelu ei tule puolesta.

    Tyyppi luetaan heittäjän repusta, ei puolesta, joten tämä on riippumaton
    tarkistus: jos erottelu olisi rikki, jakauma menisi ristiin. Poikkeuksia
    saa olla vähän (pudotettu kranaatti poimitaan), mutta ei paljon.
    """
    fire_grenades = ancient_events.filter(
        pl.col("grenade_type").is_in(["molotov", "incendiary"])
    )
    distribution = {
        (row["side"], row["grenade_type"]): row["len"]
        for row in fire_grenades.group_by("side", "grenade_type").len().iter_rows(named=True)
    }
    assert distribution.get(("T", "molotov"), 0) > 0
    assert distribution.get(("CT", "incendiary"), 0) > 0
    # T ei voi ostaa incendiarya lainkaan.
    assert distribution.get(("T", "incendiary"), 0) == 0
    # CT:llä molotov on aina poimittu, joten niitä on selvä vähemmistö.
    assert distribution.get(("CT", "molotov"), 0) < distribution[("CT", "incendiary")] / 4


@pytest.mark.demo
def test_ancient_entity_ids_are_unique_within_a_round(
    ancient_events: pl.DataFrame,
) -> None:
    """Hyväksymiskriteeri: korkeintaan yksi heitto ja yksi räjähdys per kranaatti.

    Tunniste **kierrätetään** demon aikana, joten avain on
    ``(round_raw, grenade_entity_id)`` eikä pelkkä tunniste. Tämä testi lukitsee
    sen, että avain riittää: kierroksen sisällä tunniste ei toistu.
    """
    counts = ancient_events.group_by(
        "round_raw", "grenade_entity_id", "event_kind"
    ).len()
    assert counts["len"].max() == 1

    # Ja koko demossa se toistuu -- juuri siksi kierros on osa avainta.
    whole_demo = ancient_events.group_by("grenade_entity_id", "event_kind").len()
    assert whole_demo["len"].max() > 1


@pytest.mark.demo
def test_ancient_throw_area_is_always_observed(ancient_events: pl.DataFrame) -> None:
    """Heiton alue tulee heittäjältä itseltään, ei lähimmältä pelaajalta.

    Kaikki 373 heittoa saavat alueen, koska heittäjä on aina paikalla omalla
    tickillään. Jos tämä luku ei ole täysi, joko koordinaatit tai puolet ovat
    menneet sekaisin.
    """
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throws["area"].null_count() == 0
    assert throws["area_source"].unique().to_list() == ["observed"]
    # Havainto ei ole minkään päässä: napsautusetäisyys kuuluu vain arviolle.
    assert throws["snap_distance"].null_count() == throws.height


@pytest.mark.demo
def test_ancient_throw_areas_are_real_callouts(ancient_events: pl.DataFrame) -> None:
    """Heittäjän oma alue on Ancientin oma callout, ei mikään muu."""
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    areas = set(throws["area"].drop_nulls().unique().to_list())
    assert areas <= ANCIENT_PLACES, areas - ANCIENT_PLACES


@pytest.mark.demo
def test_ancient_snap_distances_are_within_the_configured_limit(
    ancient_events: pl.DataFrame,
) -> None:
    """Napsautusetäisyys on olemassa täsmälleen napsautetuilla riveillä.

    Ilman etäisyyttä kuluttaja ei voisi erottaa 40 yksikön osumaa 490 yksikön
    arvauksesta -- ja oman kalibroinnin mukaan vain 76 % rajan sisällä
    olevista tapauksista on paikallisesti yksiselitteisiä.
    """
    limit = _parse_settings().area_snap_units
    assert limit == CALIBRATED_SNAP_UNITS

    snapped = ancient_events.filter(pl.col("area_source") == "snapped")
    assert not snapped.is_empty()
    assert snapped["snap_distance"].null_count() == 0
    assert snapped["snap_distance"].max() <= limit
    assert snapped["snap_distance"].min() > 0.0


@pytest.mark.demo
def test_ancient_detonation_areas_are_real_callouts(
    ancient_events: pl.DataFrame,
) -> None:
    """Alue on Ancientin oma callout tai tyhjä -- ei koskaan keksitty nimi."""
    assert _parse_settings().area_snap_units == CALIBRATED_SNAP_UNITS

    detonations = ancient_events.filter(pl.col("event_kind") == "grenade_detonate")
    areas = set(detonations["area"].drop_nulls().unique().to_list())
    assert areas <= ANCIENT_PLACES, areas - ANCIENT_PLACES
    assert detonations.height - detonations["area"].null_count() == (
        ANCIENT_DETONATIONS_WITH_AREA
    )
    received = detonations.filter(pl.col("area").is_not_null())
    assert received["area_source"].unique().to_list() == ["snapped"]


@pytest.mark.demo
def test_ancient_area_source_is_set_exactly_when_the_area_is(
    ancient_events: pl.DataFrame,
) -> None:
    """Sopimus: ``area_source`` on tyhjä silloin ja vain silloin kun alue on."""
    conflicts = ancient_events.filter(
        pl.col("area").is_null() != pl.col("area_source").is_null()
    )
    assert conflicts.is_empty(), conflicts.head(3).to_dicts()


@pytest.mark.demo
def test_ancient_coordinates_are_kept_even_without_an_area(
    ancient_events: pl.DataFrame,
) -> None:
    """I/O-matriisi: kaukana räjähtänyt saa ``area = null``, ei pudotusta."""
    without_area = ancient_events.filter(pl.col("area").is_null())
    assert not without_area.is_empty()
    for column in ("x", "y", "z"):
        assert without_area[column].null_count() == 0


@pytest.mark.demo
def test_ancient_events_stay_inside_their_round(
    ancient_events: pl.DataFrame,
) -> None:
    """Heitto tapahtuu kierroksen sisällä; räjähdys saa jäädä sen ulkopuolelle.

    Kierroksen lopussa heitetty savu palaa vasta seuraavan puolella, ja se
    kuuluu silti heittokierrokselle -- mutta heiton itsensä on oltava
    kierroksen rajoissa, muuten ``t_s`` ei tarkoita mitään.
    """
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throws["t_s"].min() >= 0.0
    # CS2:n kierrosaika on 115 s, mutta istutettu pommi jatkaa kierrosta vielä
    # 40 sekunnilla: post plant -savu 130 sekunnin kohdalla on normaali, ei
    # virhe. Raja on siis 115 + 40 eikä 115.
    assert throws["t_s"].max() <= ROUND_SECONDS + BOMB_SECONDS


@pytest.mark.demo
@pytest.mark.parametrize(("event_name", "grenade_kind"), DETONATE_EVENTS)
def test_ancient_detonation_point_matches_the_games_own_event(
    ancient_events: pl.DataFrame, event_name: str, grenade_kind: str
) -> None:
    """Radan viimeinen piste on räjähdyspaikka -- riippumaton tarkistus.

    Demossa on omat räjähdystapahtumansa, joissa on ``x, y, z``. Niitä ei
    lueta ajossa (kolme ylimääräistä tapahtumalukua ilman lisätietoa), mutta
    ne kelpaavat testin totuudeksi: jos jaksotus katkaisisi radan liian
    aikaisin, räjähdyspaikka olisi jossain lentoradan varrella.
    """
    from demoparser2 import DemoParser as _Demoparser2

    parser = _Demoparser2(str(require_demo(ANCIENT_DEM)))
    observed = pl.from_pandas(parser.parse_event(event_name))
    own_rows = ancient_events.filter(
        (pl.col("event_kind") == "grenade_detonate")
        & (pl.col("grenade_type") == grenade_kind)
    )
    # Vertailu tehdään **taulusta tapahtumiin**, ei toisin päin: pudotettu
    # kranaatti (kierroksen ulkopuolinen heitto, tuntematon puoli) puuttuu
    # taulusta täysin oikeutetusti, eikä testi saa vaatia että pudonneet
    # sattuisivat aina olemaan muuta tyyppiä kuin tämä.
    assert not own_rows.is_empty()
    assert own_rows.height <= observed.height

    # Paritus entiteettitunnisteella; sama tunniste esiintyy useasti, joten
    # riittää että jokin sen radoista päättyy tapahtuman paikkaan. Sallittu ero
    # on yksi pelin yksikkö -- mitattu ero on alle 0,03, ja lentoradan varrella
    # oleva piste olisi satojen yksiköiden päässä.
    positions: dict[int, list[tuple[float, float, float]]] = {}
    for row in own_rows.iter_rows(named=True):
        positions.setdefault(int(row["grenade_entity_id"]), []).append(
            (float(row["x"]), float(row["y"]), float(row["z"]))
        )

    for entity, points in positions.items():
        targets = [
            (float(r["x"]), float(r["y"]), float(r["z"]))
            for r in observed.iter_rows(named=True)
            if int(r["entityid"]) == entity
        ]
        assert targets, f"{event_name}: entiteetille {entity} ei ole tapahtumaa"
        for point in points:
            distances = [math.dist(point, target) for target in targets]
            assert min(distances) < 1.0, (
                f"{event_name} entiteetti {entity}: radan pää on "
                f"{min(distances):.1f} yksikön päässä lähimmästä "
                "räjähdyspaikasta"
            )


@pytest.mark.demo
def test_ancient_utility_diagnostics_are_clean(ancient_tables) -> None:
    """Pudotettu kranaatti on poikkeus, ei normaali tulos."""
    diagnostics = ancient_tables[1]
    assert diagnostics.grenades_without_thrower == 0
    assert diagnostics.grenades_unknown_side == 0
    # Yksi kranaatti lähtee kierroksen ratkeamisen jälkeen -- se on oikea
    # havainto eikä vika, mutta sille ei ole t_s:ää.
    assert diagnostics.grenades_outside_rounds == 1
    # Tunnisteet kierrätetään demon aikana mutta eivät kierroksen sisällä,
    # joten (round_no, grenade_entity_id) riittää parin avaimeksi.
    assert diagnostics.grenades_id_reused_in_round == 0
    # Luokkanimet ja tulikranaatin erottelu ovat ajan tasalla.
    assert diagnostics.grenades_unknown_type == 0
    assert diagnostics.grenades_fire_type_unresolved == 0
    # Tämä on ainoa luku, joka on suoraan vika: päätepistetick ilman pelaajia
    # tarkoittaisi, ettei aluetta voitu edes yrittää.
    assert diagnostics.grenade_ticks_without_players == 0
    # Savu haihtuu usein vasta seuraavan ostoajan puolella; niille ei
    # napsauteta aluetta, koska pelaajat ovat jo spawnissa.
    assert (
        diagnostics.grenades_detonating_after_round
        == ANCIENT_DETONATIONS_AFTER_ROUND
    )


@pytest.mark.demo
def test_nuke_utility_is_read_too() -> None:
    """Toinen kartta, toinen nimistö: aluepäättely ei saa olla Ancient-kohtainen."""
    tables = real_parser().parse_demo(require_demo(NUKE_ZST), SNAPSHOT_SECONDS)
    events = tables.events
    assert not events.is_empty()
    throws = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throws["area"].null_count() == 0
    assert throws["area_source"].unique().to_list() == ["observed"]
    detonations = events.filter(pl.col("event_kind") == "grenade_detonate")
    # Nuken calloutit ovat tiheämmässä kuin Ancientin, joten alue ratkeaa
    # useammin -- mutta ei koskaan kaikille.
    received = detonations.height - detonations["area"].null_count()
    assert 0 < received < detonations.height
