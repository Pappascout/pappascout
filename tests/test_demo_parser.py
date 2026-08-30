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
    LEAGUE_DEMO_FILES,
    LEAGUE_DEMOS,
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
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoParser,
)
from pappascout.domain.rounds import CT_WIN_REASONS, T_WIN_REASONS
from pappascout.domain.rounds import REQUIRED_COLUMNS as NUMBERING_COLUMNS
from pappascout.domain.rounds import check_win_reasons, mark_played_rounds
from pappascout.domain.models import load_settings
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    DEATHS,
    EVENTS,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
)

from test_calibration import ARMED_TRUTH
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
        buy_window_seconds=parse_settings.buy_window_seconds,
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
    assert df["money_buy_end"].null_count() == 0
    assert df["equip_buy_end"].null_count() == 0
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
    assert df["equip_buy_end"].max() < 10_000
    assert df["equip_buy_end"].min() > 0


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
def test_ancient_armed_player_count_matches_the_human_reading() -> None:
    """Kalustolaskuri kierroksilla 19-21 vasten ihmisen antamaa totuutta.

    **Sääntö itse on kalibroitu demovapaassa** ``test_calibration.py``:ssä
    (``ARMED_TRUTH``), jotta ``pytest -m "not demo"`` valvoo sitä myös
    koneella, jolla demoja ei ole. Tämä testi tarkistaa toisen puolen samasta
    väitteestä: että samat tavaraluettelot ja panssariarvot todella tulevat
    demosta ulos, eivätkä ole taulukkoon kirjattu muistikuva.

    Havainnot **ostoajan lopussa** (Story 1.9; ennen sitä ankkurista):

    * K19 CT -> **5**, ennen 4. Viides pelaaja osti kevlarin ja Deaglen vasta
      freezetimen jälkeen, joten ankkurista luettuna hän näytti jääneen
      ilmaiseen oletuspistooliin. Veetin tuomio "ostivat tyhjäksi" **vahvistuu**
      -- taskuun jäi 150 $ eikä 3 750 $ -- mutta hänen huomionsa "yksi jäi
      ilman panssaria" oli lukema väärältä hetkeltä.
    * K20 T -> **5**. "2x AK, 2x tec9, 1x mac10, kaikilla kevlar+kypärä" --
      kaikki viisi, sama kummastakin hetkestä.
    * K21 T -> **2**. Eco: kahdella kevlar + ostettu pistooli, ja kolmas
      p250-pelaaja putoaa **panssarin puutteeseen**. Story 1.5:n kynnys
      pudotti hänet siksi, että 300 $ < 950 $; sama luku, mutta nyt oikeasta
      syystä.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    def armed(round_no: int, side: str) -> int:
        row = df.filter((pl.col("round_no") == round_no) & (pl.col("side") == side))
        assert row.height == 1, (round_no, side)
        return row[ARMED_COLUMN][0]

    assert armed(19, "CT") == 5
    assert armed(20, "T") == 5
    assert armed(21, "T") == 2


@pytest.mark.demo
def test_ancient_inventories_match_the_calibration_table() -> None:
    """``ARMED_TRUTH``in tavaraluettelot ja panssarit ovat demosta, ei muistista.

    Edellinen testi toteaa vain kolme lukua, ja ne osuisivat myös silloin, jos
    taulun rivit olisivat ajautuneet erilleen demosta ja sääntö kompensoisi
    eron. Tämä lukee samat kolme **mittauspistettä** uudelleen ja vertaa
    jokaisen pelaajan tavaraluettelon ja panssarin taulun riviin.

    Tick on ``buy_end_tick`` eikä ``freeze_end_tick``: laskuri lasketaan siltä,
    joten ankkurin lukeminen tässä vertaisi taulua hetkeen, jota tuote ei
    käytä.

    Vertailu on joukkona: taulun pelaajajärjestys on dokumentin, ei demon.
    """
    from demoparser2 import DemoParser as _Demoparser2

    from pappascout.adapters.decompress import readable_demo

    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    wanted = {(k.round_no, k.side): k for k in ARMED_TRUTH}
    anchors = {
        (row["round_no"], row["side"]): row["buy_end_tick"]
        for row in df.iter_rows(named=True)
        if (row["round_no"], row["side"]) in wanted
    }
    assert len(anchors) == len(wanted)

    adapter = real_parser()
    with readable_demo(require_demo(ANCIENT_DEM)) as demo_path:
        by_tick = adapter._read_ticks(
            _Demoparser2(str(demo_path)),
            sorted(set(anchors.values())),
            require_demo(ANCIENT_DEM),
        )

    for key, truth in wanted.items():
        _round_no, side = key
        rows = [r for r in by_tick[anchors[key]] if r["side"] == side]
        observed = sorted(
            (tuple(sorted(r["inventory"])), r["armor_value"]) for r in rows
        )
        expected = sorted(
            (tuple(sorted(inventory)), armor) for inventory, armor in truth.players
        )
        assert observed == expected, (
            f"Kierros {truth.round_no} {truth.side}: demo ja ARMED_TRUTH "
            f"eroavat.\nDemo: {observed}\nTaulu: {expected}"
        )


#: Kaikki koneella olevat demot: kaksi vanhaa testidemoa ja neljä liigademoa.
#: Aineistoa koskevat väitteet ajetaan koko aineistolla -- liigademot ovat se
#: aineisto, jota vasten tuote lopulta arvioidaan.
ALL_DEMOS: tuple[str, ...] = (
    ANCIENT_DEM,
    NUKE_ZST,
    *(name for name, _ in LEAGUE_DEMOS),
)


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_real_demo_has_no_unknown_inventory_items(demo_name: str) -> None:
    """Aseluokittelu tuntee jokaisen nimen, jonka testidemot sisältävät.

    Tuntematon nimi ei aseista ketään, joten tuntematon **ase** laskisi
    laskurin hiljaa alas. Testi ei vaadi, että luettelo kattaa koko pelin --
    se vaatii, että se kattaa sen aineiston, jota vasten laskuri on
    kalibroitu. Uusi demo saa tuoda uusia nimiä; silloin tämä kertoo mitkä.

    Samalla todetaan, ettei yhdelläkään rivillä jäänyt panssari tai
    tavaraluettelo lukematta: se tyhjentäisi laskurin, ja tyhjä rivi
    näyttäisi ankkurittomalta kierrokselta.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ()
    assert adapter.diagnostics.armed_unreadable_rows == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_armed_count_stays_within_its_divisor(demo_name: str) -> None:
    """``0 <= players_armed_buy_end <= players_buy_end`` joka rivillä.

    Laskuri ja jakaja tulevat samasta pelaajajoukosta, joten rajan ylitys
    tarkoittaisi kahta eri jakajaa samalla rivillä -- vika, joka näkyisi vasta
    raportissa. Ja koska joukko on sama, havainto on aina molemmissa tai ei
    kummassakaan.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert (
        df[ARMED_COLUMN].null_count() == df["players_buy_end"].null_count()
    )
    # Ankkuriton kierros on laillinen havainto, ei virhe: suodatetaan pois sen
    # sijaan että vaadittaisiin, ettei niitä ole. Muuten tuleva demo kaataisi
    # tämän testin väärästä syystä.
    observed = df.filter(pl.col(ARMED_COLUMN).is_not_null())
    assert not observed.is_empty()
    assert observed.select(
        (pl.col(ARMED_COLUMN) >= 0)
        & (pl.col(ARMED_COLUMN) <= pl.col("players_buy_end"))
    ).to_series().all()
    # Sääntö erottaa oikeasti: pelkkä yksi arvo koko taulussa tarkoittaisi,
    # ettei se pure aineistoon lainkaan -- esimerkiksi että jokainen nimi on
    # tuntematon ja laskuri siis aina nolla.
    assert observed[ARMED_COLUMN].n_unique() > 1


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name,expected_rounds",
    [(ANCIENT_DEM, ANCIENT_ROUNDS), (NUKE_ZST, NUKE_ROUNDS), *LEAGUE_DEMOS],
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


# --- Liigademot ja ottelun uudelleenaloitus -------------------------------------


@pytest.mark.demo
@pytest.mark.parametrize("demo_name,expected_rounds", LEAGUE_DEMOS)
def test_league_demo_parses_despite_the_match_restart(
    demo_name: str, expected_rounds: int
) -> None:
    """Liigademo parsiutuu, ja uudelleenaloitus jää kierrosten ulkopuolelle.

    Nämä neljä kaatuivat aiemmin monotonisuustarkistukseen: puukkokierroksen
    jälkeinen uudelleenaloitus sai naapurista numeron, joka osui heti perään
    demon omaan numeroon. Nyt se jää numeroimattomaksi, ja kierrosmäärä on
    demon ``round_end``-tapahtumien määrä miinus puukkokierros.
    """
    parser = real_parser()
    df = mark_played_rounds(
        parser.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    )
    played = df.filter(pl.col("round_no").is_not_null())

    assert played["round_no"].n_unique() == expected_rounds
    assert played.height == 2 * expected_rounds
    assert sorted(played["round_no"].unique().to_list()) == list(
        range(1, expected_rounds + 1)
    )
    # Puukkokierros on demon kierros 1, joten pelatut alkavat raaka-arvosta 2.
    # Uudelleenaloitusta ei ole taulussa lainkaan, joten jono on aukoton.
    assert sorted(played["round_raw"].unique().to_list()) == list(
        range(2, expected_rounds + 2)
    )
    assert df.filter(pl.col("round_no").is_null())["round_raw"].to_list() == [1, 1]

    assert parser.diagnostics is not None
    # Tasan yksi: useampi tarkoittaisi eri ilmiötä, ja parsinta pysähtyisi.
    assert parser.diagnostics.match_restarts == 1
    assert parser.diagnostics.rounds_seen == expected_rounds + 2


@pytest.mark.demo
@pytest.mark.parametrize("demo_name,expected_rounds", LEAGUE_DEMOS)
def test_league_demo_counters_are_observations(
    demo_name: str, expected_rounds: int
) -> None:
    """Liigademon havainnot ovat demosta eivätkä tyhjiä tai oletettuja.

    Uudelleenaloitus katkaisee kaluston ja rahan ketjun heti demon alussa, ja
    juuri siinä kohdassa hiljainen tyhjä rivi olisi helppo jäädä huomaamatta.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    for name in (
        "money_buy_end",
        "money_spent",
        "equip_buy_end",
        "players_buy_end",
        "survivors",
    ):
        assert df[name].null_count() == 0, name
    assert df["players_buy_end"].unique().to_list() == [5]
    assert df["equip_buy_end"].min() > 0
    # Jokainen kierros ratkeaa jommalle kummalle: tasan yksi voittaja per
    # kierros ja tasan kaksi riviä.
    assert df.filter(pl.col("won"))["round_no"].n_unique() == expected_rounds
    assert df.group_by("round_no").len()["len"].unique().to_list() == [2]
    # Ensimmäinen kierros on pistoolikierros myös uudelleenaloituksen jälkeen:
    # jos numerointi olisi siirtynyt yhdellä, tämä osuisi täyteen ostoon.
    # 5 pelaajaa x (pistooli 200..800 + kevlar 650..1000) -> alle 10 000 $,
    # kun täysi osto on noin 21 000 $.
    pistol = df.filter(pl.col("round_no") == 1)
    assert pistol.height == 2
    assert pistol["equip_buy_end"].max() < 10_000


@pytest.mark.demo
@pytest.mark.parametrize("demo_name,expected_rounds", LEAGUE_DEMOS)
def test_league_round_count_matches_the_demos_own_event_stream(
    demo_name: str, expected_rounds: int
) -> None:
    """Kierrosmäärän oraakkeli luetaan demosta **ohi oman numerointimme**.

    ``LEAGUE_DEMOS``in luvut mitattiin adapterilla, eli sillä koodilla jota ne
    testaavat. Yksin ne siis todistaisivat vain, ettei tulos ole muuttunut --
    eivät sitä, että se on oikea. Tässä sama luku johdetaan demoparser2:n
    raa'asta tapahtumavirrasta: ``round_end``-tapahtumien määrä miinus
    puukkokierros. Mikään tämän tarinan koodista ei ole välissä.

    Ensimmäinen ``round_end`` on tyhjä alkuarvo tickissä 1 (ks.
    :mod:`pappascout.adapters.demo_parser`), joten se rajataan pois samalla
    ehdolla kuin adapterissa -- se on kirjaston ominaisuus eikä meidän
    sääntömme.
    """
    from demoparser2 import DemoParser as _Demoparser2

    frame = _Demoparser2(str(require_demo(demo_name))).parse_event("round_end")
    ends = sum(1 for tick in frame["tick"] if tick > 1)
    assert ends - 1 == expected_rounds


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", [name for name, _ in LEAGUE_DEMOS])
def test_league_demo_is_the_file_the_numbers_were_measured_from(
    demo_name: str,
) -> None:
    """Koko ja tiiviste erottavat **väärän** kopion puuttuvasta.

    Liigademot ovat korvaamattomia: FACEIT ei enää tarjoa niitä. Puuttuva demo
    saa ohittaa testin siististi, mutta väärä tai keskeneräinen kopio ei saa
    mennä läpi hiljaa -- silloin koko uudelleenaloituksen regressiosarja ajaisi
    eri aineistolla kuin se väittää.
    """
    path = require_demo(demo_name)
    size, digest = LEAGUE_DEMO_FILES[demo_name]
    assert path.stat().st_size == size, f"{demo_name}: koko ei täsmää"

    reader = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            reader.update(chunk)
    assert reader.hexdigest() == digest, f"{demo_name}: tiiviste ei täsmää"


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
def test_ancient_data_claim_entity_ids_recycle_only_between_rounds(
    ancient_events: pl.DataFrame,
) -> None:
    """**AINEISTOVÄITE, EI SOPIMUS.** Miksi vika ei näkynyt ensimmäisillä demoilla.

    Tämä testi ei lupaa mitään ``EVENTS``-taulusta. Se kuvaa yhden demon
    sisältöä: Ancientilla pelin oma tunniste toistuu demon aikana muttei
    kierroksen sisällä, joten vanha avain ``(round_raw, grenade_entity_id)``
    näytti riittävän. Liigademot osoittivat toisin -- ks.
    :func:`test_inferno_id_564_is_three_trajectories_on_one_round`.

    Testi on tallessa siksi, että se dokumentoi juuri sen aineiston
    rajallisuuden, joka johti väärään sopimukseen. Jos se joskus kaatuu, se
    tarkoittaa että Ancient-demo on vaihtunut -- ei että sopimus olisi rikki.
    Sopimuksen tae on
    :func:`test_the_trajectory_id_is_unique_in_every_demo`.
    """
    counts = ancient_events.group_by(
        "round_raw", "grenade_entity_id", "event_kind"
    ).len()
    assert counts["len"].max() == 1

    # Koko demossa tunniste toistuu -- se ei siis yksilöi kranaattia.
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
    # Ancientilla tunnisteet kierrätetään demon aikana mutta eivät kierroksen
    # sisällä. Liigademoissa kierrätetään myös kierroksen sisällä, ja siksi
    # taulun avain on grenade_no eikä pelin oma tunniste.
    assert diagnostics.grenades_sharing_an_entity_id == 0
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


#: Liigademo, jossa kierrätys näkyy. Nimi luetaan :data:`LEAGUE_DEMOS`ista
#: eikä kirjoiteta uudelleen: oma kopio vanhenisi hiljaa, ja ``require_demo``
#: ohittaisi testin muka puuttuvana demona.
INFERNO_DEMO = next(name for name, _ in LEAGUE_DEMOS if name.startswith("inferno"))

#: ``inferno_vs_ryhmarama`` kierroksella 11 pelin tunniste 564 kantaa **kolme**
#: eri lentorataa. Mitattu arkiston ``events.parquet``ista 2026-08-29, ja se on
#: koko Story 1.8:n olemassaolon syy: pari ``(round_no, grenade_entity_id)`` ei
#: yksilöi kranaattia.
#:
#: Adapterin taulussa ``round_no`` on aina tyhjä -- numeroinnin omistaa
#: ``stages.parse`` -- joten kierros nimetään tässä demon omalla laskurilla.
#: ``round_raw`` 12 on ``round_no`` 11: puukkokierros ja ottelun
#: uudelleenaloitus eivät ole pelattuja kierroksia.
INFERNO_REUSED_ROUND_RAW = 12
INFERNO_REUSED_ENTITY = 564

#: Tunnisteen 564 kolme rataa **ennen tätä muutosta**, luettuna arkiston
#: ``events.parquet``ista. Uusi sarake ei saa muuttaa yhtäkään näistä: jaksotus
#: pysyy ennallaan, ja vain tunniste on uusi. Ajat verrataan toleranssilla --
#: väite on "sama havainto", ei "sama liukulukubitti".
INFERNO_564_THROWS: tuple[tuple[str, float], ...] = (
    ("molotov", 9.1875),
    ("flashbang", 18.015625),
    ("incendiary", 64.21875),
)
INFERNO_564_DETONATIONS: tuple[tuple[str, float], ...] = (
    ("molotov", 10.15625),
    ("flashbang", 19.625),
    ("incendiary", 65.78125),
)

#: Sallitut tapahtumalajit yhtä ``grenade_no``:ta kohden. Räjähtämätön
#: kranaatti tuottaa vain heiton, joten kaksi riviä on sallittua muttei
#: pakollista -- ja kaksi riviä on aina juuri tämä pari, ei kaksi heittoa.
GRENADE_ROW_SHAPES: tuple[tuple[str, ...], ...] = (
    ("grenade_thrown",),
    ("grenade_thrown", "grenade_detonate"),
)


@lru_cache(maxsize=None)
def parsed_demo(demo_name: str):
    """Demon taulut ja diagnostiikka, parsittuna **kerran per ajo**.

    Sama kuvio kuin ``ancient_tables``-fikstuurissa, mutta nimellä
    parametroituna: kuusi 100-230 MB:n demoa ei mahdu parsittavaksi uudelleen
    joka testissä. ``require_demo`` on kutsun sisällä, jotta puuttuva demo
    ohittaa testin siististi eikä välimuistiin jää ohitusta.
    """
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    return tables, adapter.diagnostics


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_trajectory_id_is_unique_in_every_demo(demo_name: str) -> None:
    """Hyväksymiskriteeri: ``(grenade_no, event_kind)`` on yksikäsitteinen.

    Väite koskee **koko taulua** eikä kierrosta, ja se ajetaan kaikilla
    kuudella demolla. Kahdella vanhalla testidemolla myös vanha avain olisi
    mennyt läpi -- juuri siksi väite on ajettava sillä aineistolla, jossa vika
    näkyi.

    Jokainen väite kestää **tyhjän taulun**: demo ilman utilityä on
    kelvollinen tulos, eikä sopimustesti saa vaatia aineistolta sisältöä.
    Se, että näissä kuudessa demossa utilityä on, on erillinen aineistoväite
    tämän testin lopussa.
    """
    events = parsed_demo(demo_name)[0].events

    assert events["grenade_no"].null_count() == 0
    keys = events.select("grenade_no", "event_kind")
    assert keys.height == keys.unique().height

    # Pelin oma tunniste säilyy -- se on ainoa side takaisin demoon.
    assert events["grenade_entity_id"].null_count() == 0

    # Heitto ja räjähdys jakavat numeron, eikä kaksi riviä voi olla kaksi
    # heittoa. Väite on muodosta eikä lukumäärästä, joten se pitää myös
    # räjähtämättömälle kranaatille.
    shapes = (
        events.group_by("grenade_no")
        .agg(pl.col("event_kind").sort().cast(pl.Utf8).alias("kinds"))["kinds"]
        .to_list()
    )
    assert all(tuple(kinds) in GRENADE_ROW_SHAPES for kinds in shapes)

    # Aineistoväite, ei sopimus: näissä kuudessa demossa utilityä heitettiin.
    assert not events.is_empty()


@pytest.mark.demo
def test_inferno_id_564_is_three_trajectories_on_one_round() -> None:
    """Hyväksymiskriteeri: tunniste 564 hajoaa kolmeen -- ajat ja tyypit ennallaan.

    Tämä on se mitattu tapaus, jota vanha sopimus ei kestänyt. Uuden
    tunnisteen on erotettava radat toisistaan **muuttamatta havaintoa**:
    jaksotukseen ei kosketa, joten ajat ja kranaattityypit ovat samat kuin
    ennen muutosta.
    """
    events = parsed_demo(INFERNO_DEMO)[0].events
    subset = events.filter(
        (pl.col("round_raw") == INFERNO_REUSED_ROUND_RAW)
        & (pl.col("grenade_entity_id") == INFERNO_REUSED_ENTITY)
    ).sort("t_s")

    throws = subset.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = subset.filter(pl.col("event_kind") == "grenade_detonate")

    def observed(frame: pl.DataFrame) -> list[tuple[str, float]]:
        return list(zip(frame["grenade_type"].to_list(), frame["t_s"].to_list()))

    def expected(pairs: tuple[tuple[str, float], ...]) -> list[tuple[str, object]]:
        # Toleranssi tickin murto-osan verran: väite on sama havainto, ei sama
        # liukulukubitti. Tickrate- tai pyöristysmuutos ei saa näyttää siltä,
        # että jaksotus muuttui.
        return [(name, pytest.approx(t_s, abs=0.02)) for name, t_s in pairs]

    # Havainto on ennallaan: samat kolme tyyppiä samoilla hetkillä.
    assert observed(throws) == expected(INFERNO_564_THROWS)
    assert observed(detonations) == expected(INFERNO_564_DETONATIONS)

    # Kolme rataa, kolme tunnistetta -- ja pelin oma tunniste on yhä sama.
    assert throws["grenade_no"].n_unique() == 3
    assert subset["grenade_no"].n_unique() == 3
    assert subset["grenade_entity_id"].unique().to_list() == [INFERNO_REUSED_ENTITY]

    # Heitto ja räjähdys jakavat numeron: se on niiden ainoa side.
    for _, pair in subset.group_by("grenade_no", maintain_order=True):
        assert sorted(pair["event_kind"].to_list()) == [
            "grenade_detonate",
            "grenade_thrown",
        ]


@pytest.mark.demo
def test_parsing_the_same_demo_twice_gives_identical_tables() -> None:
    """Hyväksymiskriteeri: sama demo kahdesti -> identtiset taulut.

    Tunnisteen vakaus on ehto: jos numerot vaihtuisivat ajojen välillä,
    arkiston uudelleenparsinta näyttäisi muutokselta ilman muutosta.

    Väite on tässä heikko -- deterministinen funktio samalla syötteellä --
    ja sen vahva muoto on ``test_utility.py``:n puolella, jossa lentoratojen
    **rivijärjestys sekoitetaan** ennen jaksotusta. Demolla sitä ei voi tehdä,
    joten tämä varmistaa vain, ettei koko putkeen ole jäänyt satunnaisuutta
    (hajautusjärjestys, rinnakkaisuus). Ensimmäinen parsinta on jaettu muiden
    testien kanssa, joten hinta on yksi ylimääräinen luku eikä kaksi.
    """
    first = parsed_demo(ANCIENT_DEM)[0]
    second = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)

    assert not first.events.is_empty()
    assert first.events.equals(second.events)


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


# --- Ostoaika oikeissa demoissa (Story 1.9) ------------------------------------


#: ``inferno_vs_ryhmarama``, kierros 6, Ryhmä Rämä T-puolella. Veeti katsoi
#: tämän kierroksen demosta ja luki siitä luvut, jotka eivät täsmänneet
#: työkalun tuottamiin -- se oli koko vian löytöhavainto.
#:
#: Freezetimen lopussa varusteita 11 550 ja rahaa 6 600; kaksi sekuntia
#: myöhemmin 15 350 ja 2 400. Kolme viidestä pelaajasta osti vasta silloin.
INFERNO_ROUND_6 = {
    "equip_buy_end": 15_350,
    "money_buy_end": 2_400,
    "armed": 5,
    # Veetin lukemat saldot pelaajittain (kalibrointidokumentti): 150, 0, 500,
    # 1 750, 0. Summa on sama 2 400 -- ja juuri se on ongelma: summasta ei näe,
    # että vain yksi pelaaja pääsee 4 000 dollariin häviöbonuksen kanssa.
    "money_players": [1_750, 500, 150, 0, 0],
}

#: Sama kierrokselta 10, jonka Veeti kutsui puoliostoksi. Molemmilla on viisi
#: aseistettua pelaajaa, joten kalusto ei erota niitä -- vain jakauma erottaa.
INFERNO_ROUND_10 = {
    "equip_buy_end": 11_900,
    "money_buy_end": 7_900,
    "armed": 5,
    "money_players": [2_150, 2_050, 2_000, 900, 800],
}

#: Samat pelaajat, samat aseet, Veetin lukemina. Kolme näistä on ostettu vasta
#: freezetimen jälkeen; ankkurista luettuna kaikilla kolmella on Glock.
INFERNO_ROUND_6_WEAPONS = {
    "petemonni": "P250",
    "Toumee": "Tec-9",
    "Manetsu": "AK-47",
}


@pytest.mark.demo
def test_inferno_round_six_matches_the_human_reading() -> None:
    """Vian löytökierros tuottaa nyt ne luvut, jotka Veeti luki demosta.

    Kolme lukua yhdessä, koska ne rikkoutuivat yhdessä: varustearvo
    aliarvioitiin, taskuun jäänyt raha yliarvioitiin ja aseistettujen laskuri
    antoi 2 vaikka totuus oli 5. Yksikään niistä ei olisi paljastanut vikaa
    yksinään -- laskurin 2 näytti uskottavalta ecolta.
    """
    df = mark_played_rounds(
        real_parser()
        .parse_demo(require_demo("inferno_vs_ryhmarama.dem"), SNAPSHOT_SECONDS)
        .rounds
    ).filter(pl.col("round_no").is_not_null())

    row = df.filter((pl.col("round_no") == 6) & (pl.col("side") == "T"))
    assert row.height == 1
    observed = row.to_dicts()[0]

    assert observed["equip_buy_end"] == INFERNO_ROUND_6["equip_buy_end"]
    assert observed["money_buy_end"] == INFERNO_ROUND_6["money_buy_end"]
    assert observed[ARMED_COLUMN] == INFERNO_ROUND_6["armed"]
    assert (
        list(observed[MONEY_DISTRIBUTION_COLUMN])
        == INFERNO_ROUND_6["money_players"]
    )
    # Mittauspiste on ankkurin jälkeen mutta ennen ikkunan loppua: kierroksen
    # ensimmäinen kuolema (18,1 s) katkaisi ikkunan.
    assert observed["buy_end_tick"] > observed["freeze_end_tick"]


@pytest.mark.demo
def test_inferno_rounds_six_and_ten_differ_only_in_the_distribution() -> None:
    """Kaksi kierrosta, sama kalusto, eri tuomio -- ero on jakaumassa.

    Molemmissa on viisi aseistettua pelaajaa, joten puolioston ehto A ei
    erota niitä lainkaan. Veeti kutsui kierrosta 6 forceksi ja kierrosta 10
    puoliostoksi, ja perusteli sen sillä kuka pystyy ostamaan seuraavalla
    kierroksella. Tämä testi pinnaa **havainnon**, josta se luetaan; säännön
    oma testi on ``test_calibration.py``:ssä eikä tarvitse demoa.
    """
    df = mark_played_rounds(
        real_parser()
        .parse_demo(require_demo("inferno_vs_ryhmarama.dem"), SNAPSHOT_SECONDS)
        .rounds
    ).filter(pl.col("round_no").is_not_null())

    for round_no, expected in ((6, INFERNO_ROUND_6), (10, INFERNO_ROUND_10)):
        row = df.filter((pl.col("round_no") == round_no) & (pl.col("side") == "T"))
        assert row.height == 1, round_no
        observed = row.to_dicts()[0]
        assert observed[ARMED_COLUMN] == expected["armed"], round_no
        assert observed["money_buy_end"] == expected["money_buy_end"], round_no
        assert observed["equip_buy_end"] == expected["equip_buy_end"], round_no
        assert (
            list(observed[MONEY_DISTRIBUTION_COLUMN]) == expected["money_players"]
        ), round_no


@pytest.mark.demo
def test_inferno_round_six_players_hold_the_weapons_veeti_saw() -> None:
    """Pelaajakohtaiset aseet, ei vain joukkuesumma.

    Summa 15 350 osuisi myös silloin, jos mittauspiste olisi oikea mutta
    tavaraluettelo luettaisiin väärältä tickiltä -- ja juuri tavaraluettelo
    ratkaisee aseistettujen laskurin. Veeti nimesi kolme asetta, jotka
    ostettiin vasta freezetimen jälkeen; ankkurista luettuna kaikilla kolmella
    on yhä ilmainen Glock.
    """
    from demoparser2 import DemoParser as _Demoparser2

    from pappascout.adapters.decompress import readable_demo

    demo = require_demo("inferno_vs_ryhmarama.dem")
    adapter = real_parser()
    df = mark_played_rounds(
        adapter.parse_demo(demo, SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    row = df.filter((pl.col("round_no") == 6) & (pl.col("side") == "T")).to_dicts()[0]

    with readable_demo(demo) as demo_path:
        parser = _Demoparser2(str(demo_path))
        frame = parser.parse_ticks(["inventory"], ticks=[row["buy_end_tick"]])
    inventories = {
        str(record["name"]): tuple(record["inventory"] or ())
        for record in frame.to_dict("records")
    }

    for player, weapon in INFERNO_ROUND_6_WEAPONS.items():
        assert player in inventories, sorted(inventories)
        assert weapon in inventories[player], (
            f"{player}: Veeti näki {weapon!r}, demo antoi "
            f"{inventories[player]}"
        )


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_no_purchase_is_lost_behind_the_death_cut(demo_name: str) -> None:
    """Kuoleman katkaisu ei maksa yhtään ostosta -- todettuna, ei oletettuna.

    Tämä on ostoikkunan koko kompromissi yhtenä lukuna. Ikkuna on 20 s, mutta
    kuolema katkaisee sen noin puolella kierroksista; jos joku ostaisi vielä
    katkaisun jälkeen, mittaus menettäisi ostoksen. Mitattuna kaikista kuudesta
    demosta niin ei käy kertaakaan.

    Katkaisujen määrää **ei** väitetä nollaksi: se on normaali polku eikä
    vika. Väite koskee vain sen hintaa.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    assert adapter.diagnostics is not None
    cuts = adapter.diagnostics.buy_window_cuts
    assert sum(missed for _, missed in cuts) == 0
    assert adapter.diagnostics.buy_window_ticks_without_players == 0
    # Katkaisuja on, eli ikkuna todella rajautuu kuolemaan. Ilman tätä
    # rivi menisi läpi myös silloin, jos kuolemia ei luettaisi lainkaan.
    assert cuts, "yhtäkään ikkunaa ei katkaistu -- kuolemia ei ilmeisesti lueta"


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_measurement_point_stays_inside_its_round(demo_name: str) -> None:
    """Mittauspiste on ankkurin jälkeen, ikkunan sisällä ja sama molemmilla.

    Kolme invarianttia, joista jokainen rikkoutuisi eri tavalla: mittauspiste
    ennen ankkuria lukisi freezetimen sisältä, ikkunan lopun jälkeen se ei
    enää olisi ostoaika, ja joukkuekohtainen piste tekisi kahden rivin
    summista vertailukelvottomat.

    Neljäs raja -- kierroksen loppu -- on omassa testissään
    :func:`test_the_measurement_never_reaches_the_next_round`, koska se vaatii
    vertailun **seuraavaan** kierrokseen eikä ole luettavissa yhdeltä riviltä.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert df["buy_end_tick"].null_count() == 0
    assert (df["buy_end_tick"] >= df["freeze_end_tick"]).all()

    window_ticks = round(_parse_settings().buy_window_seconds * df["tick_rate"][0])
    assert (df["buy_end_tick"] - df["freeze_end_tick"] <= window_ticks).all()

    per_round = df.group_by("round_no").agg(
        pl.col("buy_end_tick").n_unique().alias("ticks")
    )
    assert per_round["ticks"].unique().to_list() == [1]


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_anchor_reading_is_what_it_was_before_the_window(demo_name: str) -> None:
    """Ikkuna 0 toistaa Story 1.9:ää edeltävän mittauksen sellaisenaan.

    Varustearvo luetaan nyt propista ``m_unCurrentEquipmentValue`` eikä
    ``m_unFreezetimeEndEquipmentValue``sta -- jälkimmäinen ei päivity
    freezetimen jälkeen, joten sillä koko korjaus jäisi näkymättömäksi.
    Ankkurilla nämä kaksi ovat sama luku, ja juuri se tekee vaihdosta
    turvallisen: ilman sitä propinvaihto olisi voinut siirtää jokaisen luvun
    hiljaa.

    Vertailuarvot ovat kalibrointidokumentin ja vikaraportin lukuja, jotka on
    mitattu vanhalla propilla.
    """
    parse_settings = _parse_settings()
    adapter = Demoparser2Adapter(
        exclude_weapons=parse_settings.first_contact_exclude_weapons,
        fallback_death=parse_settings.first_contact_fallback_death,
        area_snap_units=parse_settings.area_snap_units,
        buy_window_seconds=0.0,
    )
    df = mark_played_rounds(
        adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert (df["buy_end_tick"] == df["freeze_end_tick"]).all()
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.buy_window_cuts == ()

    if demo_name != ANCIENT_DEM:
        return
    # Ancientin kalibrointiluvut vanhalla mittauksella, dokumentista
    # (kalibrointi-kierrostyypit.md, totuustaulu; $/pelaaja x 5).
    def equip(round_no: int, side: str) -> int:
        row = df.filter((pl.col("round_no") == round_no) & (pl.col("side") == side))
        assert row.height == 1, (round_no, side)
        return int(row["equip_buy_end"][0])

    assert equip(19, "CT") == 2_040 * 5
    assert equip(20, "T") == 2_910 * 5
    assert equip(21, "T") == 710 * 5


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_round_start_equipment_is_the_same_at_both_ticks(demo_name: str) -> None:
    """``m_unRoundStartEquipmentValue`` ei muutu ostoikkunan aikana.

    Ostettu summa on ``equip_buy_end - equip_round_start``, ja se on sääntö
    S3:n koko perusta. Vähennettävä luetaan nyt myöhemmältä tickiltä kuin
    ennen, ja **juuri tässä tarinassa osoittautui**, että toinen samannäköinen
    kenttä (``m_unFreezetimeEndEquipmentValue``) ei käyttäydy odotetusti
    myöhemmältä tickiltä luettuna. Sama oletus ei saa jäädä toisen kentän
    kohdalla pelkän mittauksen varaan.

    Vertailu tehdään pelaajakohtaisesti molemmilta tickeiltä: jos kenttä
    joskus alkaa elää kierroksen aikana, ostettu summa liukuisi hiljaa ja
    S3 kääntäisi säästöjä ostoiksi.
    """
    from demoparser2 import DemoParser as _Demoparser2

    from pappascout.adapters.decompress import readable_demo

    demo = require_demo(demo_name)
    adapter = real_parser()
    df = mark_played_rounds(adapter.parse_demo(demo, SNAPSHOT_SECONDS).rounds).filter(
        pl.col("round_no").is_not_null()
    )

    pairs = {
        (int(row["freeze_end_tick"]), int(row["buy_end_tick"]))
        for row in df.iter_rows(named=True)
        if row["freeze_end_tick"] is not None and row["buy_end_tick"] is not None
    }
    moved = {(a, b) for a, b in pairs if a != b}
    assert moved, "yksikään mittauspiste ei siirtynyt ankkurista"

    wanted = sorted({tick for pair in moved for tick in pair})
    with readable_demo(demo) as demo_path:
        by_tick = adapter._read_ticks(_Demoparser2(str(demo_path)), wanted, demo)

    compared = 0
    for anchor_tick, buy_tick in sorted(moved):
        at_anchor = {r["steamid"]: r["equip_round_start"] for r in by_tick[anchor_tick]}
        for row in by_tick[buy_tick]:
            before = at_anchor.get(row["steamid"])
            if before is None or row["equip_round_start"] is None:
                continue
            compared += 1
            assert row["equip_round_start"] == before, (
                f"{demo_name}: pelaajan {row['steamid']} "
                "round_start_equip_value muuttui ankkurin ja mittauspisteen "
                f"välillä ({before} -> {row['equip_round_start']}). "
                "Ostettu summa ei ole enää luotettava."
            )
    assert compared >= 5 * len(moved), (compared, len(moved))


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_measurement_never_reaches_the_next_round(demo_name: str) -> None:
    """Mittauspiste ei yllä seuraavan kierroksen ankkuriin.

    Kierroksen loppuun rajautuminen on kolmas raja mittauspisteen kaavassa,
    eikä sitä ole tähän asti todettu oikealla demolla lainkaan -- feikissä
    kierros on 39 s, joten 20 sekunnin ikkuna mahtuu aina sisään. Oikeassa
    demossa kierros voi ratketa alle 20 sekunnissa, ja silloin rajaton ikkuna
    lukisi seuraavan kierroksen talousarvot tämän kierroksen riville.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    per_round = (
        df.group_by("round_no")
        .agg(
            pl.col("freeze_end_tick").first().alias("anchor"),
            pl.col("buy_end_tick").first().alias("measured"),
        )
        .sort("round_no")
    )
    anchors = per_round["anchor"].to_list()
    measured = per_round["measured"].to_list()

    for index in range(len(anchors) - 1):
        assert measured[index] < anchors[index + 1], (
            f"{demo_name}: kierroksen {per_round['round_no'][index]} "
            f"mittauspiste {measured[index]} yltää seuraavan kierroksen "
            f"ankkuriin {anchors[index + 1]}."
        )


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_buy_window_reports_no_broken_measurement(demo_name: str) -> None:
    """Mittauspisteen vikalaskurit ovat nollia koko aineistossa.

    Nämä neljä ovat **vikoja eivätkä havaintoja**: tyhjä ostotick, ankkurilta
    kadonneet pelaajat, kokonaan tyhjäksi jäänyt joukkuerivi ja tarkistamatta
    jäänyt katkaisu. Yksikään ei laukea kuudessa demossa, ja juuri siksi ne on
    pinnattava: nollasta poikkeava arvo on merkki siitä, että jokin
    mittauspisteen oletus on rikki.

    Palautukset ja niiden jättämä vanhentunut varustearvo **eivät** ole tässä:
    ne ovat pelin käyttäytymistä eivätkä meidän vikojamme, ja niillä on omat
    testinsä.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    diagnostics = adapter.diagnostics
    assert diagnostics is not None
    assert diagnostics.buy_window_ticks_without_players == 0
    assert diagnostics.buy_window_players_lost == 0
    assert diagnostics.buy_window_sides_without_rows == 0
    assert diagnostics.buy_window_unchecked_cuts == ()
    assert [missed for _, missed in diagnostics.buy_window_cuts] == [
        0 for _ in diagnostics.buy_window_cuts
    ]


@pytest.mark.demo
def test_refunds_are_observed_and_stay_rare() -> None:
    """Palautuksia esiintyy, ja niiden jättämä vanhentunut arvo on harvinaista.

    Molemmat luvut ovat pelin käyttäytymistä eivätkä vikoja, mutta ne on
    pinnattava kahdesta suunnasta. Nolla palautusta tarkoittaisi, ettei
    tunnistus enää toimi -- ``cash_spent``in lasku on niiden ainoa
    yksikäsitteinen merkki. Suuri määrä vanhentunutta arvoa taas tarkoittaisi,
    ettei varustearvoon voi luottaa; mitattuna se on yksi pelaajarivi koko
    aineistossa.
    """
    refunds = 0
    stale = 0
    for demo_name in ALL_DEMOS:
        adapter = real_parser()
        adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
        assert adapter.diagnostics is not None
        refunds += adapter.diagnostics.buy_window_refunds
        stale += adapter.diagnostics.buy_window_stale_equipment

    assert refunds > 0, "palautuksia ei havaittu lainkaan -- tunnistus on rikki"
    # 8 palautusta ja 1 vanhentunut arvo, mitattu 2026-08-29. Rajat ovat
    # väljät, koska luvut ovat aineiston ominaisuus eivätkä sopimus; tiukka
    # yhtäsuuruus kaatuisi heti kun aineistoon lisätään demo.
    assert refunds <= 20, refunds
    assert stale <= 3, stale


# --- Kokoonpanotaulu oikeista demoista (Story 2.6) ------------------------------

#: Liigademojen klaaninimet, mitattu 2026-08-30 suoraan demoista.
#:
#: Nämä eivät ole meidän koodimme tuotos: ne ovat pelin oma
#: ``team_clan_name`` -kenttä jokaisen pelaajan ankkuririvillä. Testi lukee ne
#: uudelleen, koska juuri nämä merkkijonot päätyvät raportin otsikkoon --
#: uudelleennimeäminen demoparser2:ssa näkyisi muuten vasta valmiissa
#: raportissa.
LEAGUE_CLANS: dict[str, tuple[str, str]] = {
    "Ancient_vs_kaljukostaja.dem": ("KALJUKOSTAJA", "MatureMayhem"),
    "Anubis_vs_ryhmarama.dem": ("MatureMayhem", "Ryhma Rama"),
    "inferno_vs_ryhmarama.dem": ("MatureMayhem", "Ryhma Rama"),
    "Nuke_vs_imuaijat.dem": ("MatureMayhem", "NadedNConfused"),
}


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_CLANS))
def test_real_demo_gives_ten_players_two_clans_five_each(demo_name: str) -> None:
    """Kymmenen riviä, kaksi klaania, viisi pelaajaa kumpaankin.

    Vaihtopelaaja tekee poikkeuksen: jos joukkue vaihtoi pelaajaa kesken
    kartan, rivimäärä on suurempi. Testidemoissa vaihto tapahtuu **karttojen
    välissä** eikä niiden sisällä, joten jokainen niistä antaa tasan kymmenen.
    """
    tables = real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    lineups = tables.lineups

    assert list(lineups.columns) == list(LINEUPS_ADAPTER_COLUMNS)
    assert lineups.height == 10
    assert lineups.select("lineup_key", "player_id").unique().height == 10

    clans = sorted(set(lineups["clan_name"].to_list()))
    assert clans == list(LEAGUE_CLANS[demo_name])
    counts = lineups.group_by("clan_name").len()["len"].to_list()
    assert counts == [5, 5]


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_CLANS))
def test_every_player_has_exactly_one_clan_and_one_name(demo_name: str) -> None:
    """Yksi klaani ja yksi nimi per SteamID -- myös puoliajan vaihdon yli.

    Tämä on se mittaus, jonka takia klaani luetaan pelaajakohtaisesti eikä
    puolen kautta. Puolen kautta luettuna ``team_num=2`` on 1. puoliajalla
    toinen joukkue ja 2. puoliajalla toinen.

    **Väite on raakoihin havaintoihin, ei valmiiseen tauluun.** Taulu on
    kollapsoitu: ``_most_observed`` takaa yhden rivin ja yhden arvon per
    pelaaja riippumatta siitä, montako klaania havaittiin, joten taulusta
    luettu "yksi arvo per pelaaja" olisi väite koodin rakenteesta eikä
    demosta. Ainoa paikka, jossa ero näkyy, on adapterin oma laskuri --
    ja siksi sitä luetaan tässä.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.lineup_clan_conflicts == 0
    assert adapter.diagnostics.lineup_name_conflicts == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_CLANS))
def test_the_lineup_key_matches_the_players_in_the_table(demo_name: str) -> None:
    """Tunniste on tiiviste taulun omista SteamID:istä, ei mistään muusta.

    Jos nämä erkanisivat, ``aggregate`` liittäisi rosterin joukkueeseen, jota
    ``lineup_key`` ei tarkoita.
    """
    tables = real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    for key, group in tables.lineups.group_by("lineup_key"):
        players = sorted(group["player_id"].to_list())
        expected = hashlib.sha256(
            ",".join(players).encode("utf-8")
        ).hexdigest()[:16]
        assert key[0] == expected


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_CLANS))
def test_no_name_is_missing_from_a_league_demo(demo_name: str) -> None:
    """Liigademoissa jokaisella pelaajalla on nimi ja klaani.

    Eri väite kuin ristiriidattomuus: tämä sanoo, että havainto ylipäätään
    saatiin. Nullit ovat sallittu tulos sopimuksessa, mutta tässä aineistossa
    niitä ei ole -- ja jos joskus on, se näkyy raportissa SteamID:nä.
    """
    tables = real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    assert tables.lineups["clan_name"].null_count() == 0
    assert tables.lineups["player_name"].null_count() == 0


@pytest.mark.demo
def test_the_ticks_table_agrees_with_the_lineups_table() -> None:
    """Sama kokoonpano molemmissa tauluissa; liitos ei saa mennä ristiin."""
    tables = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)

    from_ticks = set(
        tables.ticks.select("lineup_key", "player_id").unique().iter_rows()
    )
    from_lineups = set(
        tables.lineups.select("lineup_key", "player_id").iter_rows()
    )
    # Kokoonpanotaulu on kartan totuus: näytepistetaulusta voi puuttua
    # pelaaja,
    # joka ei ehtinyt yhdellekään näytepisteelle, mutta yhtään ylimääräistä
    # siinä ei saa olla.
    assert from_ticks <= from_lineups


# --- Kuolemat oikeasta demosta (Story 2.7) -------------------------------------

#: Ampujattomat kuolemat demoittain, mitattu 2026-08-30 **adapterin
#: tuotoksesta** (ennen kuin ``stages.parse`` pudottaa numeroimattomat
#: kierrokset). Putoaminen ja pommi ovat aitoja tapauksia, mutta niiden määrä
#: on pieni ja tunnettu: jos se hyppää, jokin muu on rikki.
#:
#: Luvut ovat demokohtaisia eivätkä yhteissumma, koska yhteissumma säilyisi
#: samana vaikka kaksi demoa vaihtaisi lukujaan keskenään.
LEAGUE_DEATHS_WITHOUT_ATTACKER: dict[str, int] = {
    "Ancient_vs_kaljukostaja.dem": 1,
    "Anubis_vs_ryhmarama.dem": 0,
    "inferno_vs_ryhmarama.dem": 0,
    "Nuke_vs_imuaijat.dem": 5,
}


@lru_cache(maxsize=None)
def _league_deaths(demo_name: str) -> tuple[pl.DataFrame, object]:
    """Yhden liigademon kuolemataulu ja diagnostiikka, parsittu kerran."""
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    return tables.deaths, adapter.diagnostics


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_real_demo_deaths_match_the_port_contract(demo_name: str) -> None:
    """Sarakkeet ja tyypit tulevat oikeasta demosta, eivät vain feikistä."""
    deaths, _ = _league_deaths(demo_name)

    assert tuple(deaths.columns) == DEATHS_ADAPTER_COLUMNS
    for name in DEATHS_ADAPTER_COLUMNS:
        assert deaths.schema[name] == DEATHS[name], name
    assert not deaths.is_empty()
    # Numeroinnin omistaa stages.parse; adapteri jättää sarakkeen tyhjäksi.
    assert deaths["round_no"].null_count() == deaths.height


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_every_victim_has_an_area_in_a_real_demo(demo_name: str) -> None:
    """Uhrin alue on koko storyn väite, ja se on **luettava demosta**.

    ``DEATH_COLUMNS``-vartija tarkistaa sarakkeen olemassaolon eikä sisältöä.
    Jos ``last_place_name`` palaisi tyhjänä merkkijonona, taulu olisi
    skeemakelvollinen ja jokainen rivi alueeton -- eikä yksikään feikkitesti
    huomaisi mitään, koska feikki tuottaa alueet itse.

    Mitattu 2026-08-30: 0 puuttuvaa uhrin aluetta 591 kirjoitetusta
    kuolemasta.
    """
    deaths, _ = _league_deaths(demo_name)
    assert deaths["victim_area"].null_count() == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_the_attacker_area_is_missing_only_when_the_attacker_is(
    demo_name: str,
) -> None:
    """Alue ei katoa ampujalta -- ampuja katoaa.

    Kaksi väitettä yhdessä: ampujattomia rivejä on täsmälleen mitattu määrä,
    ja jokainen puuttuva ampujan alue on **niillä riveillä**. Jälkimmäinen on
    se, joka erottaa rehellisen putoamisen rikkoutuneesta aluehavainnosta.
    """
    deaths, _ = _league_deaths(demo_name)

    without_attacker = deaths.filter(pl.col("attacker_id").is_null())
    assert without_attacker.height == LEAGUE_DEATHS_WITHOUT_ATTACKER[demo_name]

    # Ampuja tiedossa mutta alue tyhjä: nolla mitatussa aineistossa.
    unnamed = deaths.filter(
        pl.col("attacker_id").is_not_null() & pl.col("attacker_area").is_null()
    )
    assert unnamed.is_empty(), unnamed.head(3).to_dicts()

    # Ampujaton rivi on kokonaan ampujaton, myös oikeassa demossa.
    for column in (
        "attacker_lineup_key",
        "attacker_side",
        "attacker_x",
        "attacker_y",
        "attacker_z",
        "attacker_area",
    ):
        assert without_attacker[column].null_count() == without_attacker.height


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_no_death_is_dropped_for_a_missing_side_in_a_real_demo(
    demo_name: str,
) -> None:
    """Puolen päättely ei saa hukata kuolemia oikeasta ottelusta.

    Luku on adapterin oma laskuri eikä valmis taulu: pudotettu rivi ei ole
    taulussa, joten sen puuttumista ei voi lukea sieltä. Nolla on odotusarvo,
    ja nollasta poikkeava arvo tarkoittaisi että ``m_iTeamNum``-koodit tai
    kokoonpanojen tunnistus ovat muuttuneet.
    """
    _, diagnostics = _league_deaths(demo_name)

    assert diagnostics is not None
    assert diagnostics.deaths_without_victim_side == 0
    assert diagnostics.deaths_without_victim == 0
    assert diagnostics.deaths_attacker_without_side == 0
    assert diagnostics.deaths_without_tick == 0


@pytest.mark.demo
def test_ancient_death_areas_are_real_callouts() -> None:
    """Molemmat alueet ovat Ancientin omia calloutteja, eivät keksittyjä.

    Sama vartija kuin utilityn heittoalueilla. Ilman sitä
    ``user_last_place_name`` voisi palata kokonaan eri kentästä -- vaikkapa
    aseen nimenä -- ja taulu olisi silti kelvollinen.
    """
    deaths, _ = _league_deaths(ANCIENT_DEM)

    for column in ("victim_area", "attacker_area"):
        areas = set(deaths[column].drop_nulls().unique().to_list())
        assert areas <= ANCIENT_PLACES, (column, areas - ANCIENT_PLACES)
        assert areas


@pytest.mark.demo
def test_ancient_deaths_carry_coordinates_for_every_actor_present() -> None:
    """Koordinaatit ovat tallessa aina kun toimija on -- alueesta riippumatta."""
    deaths, _ = _league_deaths(ANCIENT_DEM)

    for axis in ("victim_x", "victim_y", "victim_z"):
        assert deaths[axis].null_count() == 0
    with_attacker = deaths.filter(pl.col("attacker_id").is_not_null())
    for axis in ("attacker_x", "attacker_y", "attacker_z"):
        assert with_attacker[axis].null_count() == 0


@pytest.mark.demo
def test_ancient_deaths_stay_inside_their_round() -> None:
    """Kuolema kuuluu sille kierrokselle, jonka rajojen sisään se osuu.

    ``t_s`` on aika ankkurista, joten negatiivinen arvo tarkoittaisi kuolemaa
    ennen freezetimen loppua -- eli väärää kierrosta.
    """
    deaths, _ = _league_deaths(ANCIENT_DEM)
    assert deaths["t_s"].null_count() == 0
    assert deaths["t_s"].min() >= 0.0


@pytest.mark.demo
def test_ancient_victim_side_agrees_with_the_ticks_table() -> None:
    """Kuoleman puoli on sama kuin näytepistetaulun puoli samalla kierroksella.

    Tämä on se ristiintarkistus, jota feikki ei voi tehdä: se rakentaa
    molemmat taulut samasta kuvauksesta, joten ne eivät voi olla eri mieltä.
    Oikeassa demossa ne luetaan eri lähteistä -- ``player_death``-tapahtumasta
    ja ``parse_ticks``istä -- ja juuri siksi ne voisivat erota.
    """
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)

    from_ticks = {
        (row["round_raw"], row["player_id"]): row["lineup_key"]
        for row in tables.ticks.iter_rows(named=True)
    }
    checked = 0
    for row in tables.deaths.iter_rows(named=True):
        expected = from_ticks.get((row["round_raw"], row["victim_id"]))
        if expected is None:
            continue
        checked += 1
        assert row["victim_lineup_key"] == expected, row
    assert checked > 100, f"vain {checked} riviä vertailtavissa"


@pytest.mark.demo
def test_a_knife_round_really_does_produce_death_rows() -> None:
    """Puukkokierroksen pudotus ei ole teoriaa: adapteri tuottaa ne rivit.

    Jos adapteri suodattaisi ne itse, ``stages.parse``in liitos ei tekisi
    mitään eikä väite "sama mekanismi kuin muissa tauluissa" tarkoittaisi
    mitään. Puukkokierros on liigademon ensimmäinen kierrosraja.
    """
    deaths, _ = _league_deaths(ANCIENT_DEM)
    first_round = deaths["round_raw"].min()

    assert first_round == 1
    assert deaths.filter(pl.col("round_raw") == 1).height > 0


# --- Panssarilaskuri oikeista demoista (Story 2.8) ------------------------------

#: Mittatikku 2026-08-30: MatureMayhemin panssari- ja kalustolaskurit niiltä
#: kierroksilta, joista Veetin käsin tehty analyysi puhuu.
#:
#: ``(demo, kierros, puoli) -> (panssaroituja, aseistettuja)``. Luvut mitattiin
#: **ennen toteutusta** arkiston kierrostaulun ``buy_end_tick``-sarakkeelta eli
#: samalta hetkeltä, jolta talousluvut jo luetaan -- ei arvatulta tickiltä.
#:
#: **Kiinnike kattaa väitteen kokonaan.** Dokumentaatio sanoo kolmessa paikassa
#: "neljä demoa, kaikki kahdeksan pistoolikierrosta", joten kaikki kahdeksan
#: ovat tässä -- kaksi per demo (kierrokset 1 ja 13). Ilman niitä väite
#: nojaisi mittaukseen, jota mikään ei aja uudelleen.
#:
#: Kaksi riviä ovat suoria osumia analyysiin: Nuken T-pistoolista Veeti
#: kirjoitti *"5 kevlaria"* (mitattu 5/5) ja Ancientin CT-osuudesta
#: *"Kitit ja duelit takaboksille piiloon (ei kevuja)"* (mitattu 1/5).
#: Kumpaakaan ei voi lukea aseistettujen laskurista, joka on 0 jokaisella
#: kahdeksalla pistoolikierroksella.
#:
#: Kolme viimeistä riviä ovat eco ja force: siellä laskurit ovat lähellä
#: toisiaan, ja ne ovat mukana siksi, ettei testi läpäisisi toteutusta, joka
#: tuottaa eron aina.
ARMOR_TRUTH: dict[tuple[str, int, str], tuple[int, int]] = {
    ("Nuke_vs_imuaijat.dem", 1, "CT"): (4, 0),
    ("Nuke_vs_imuaijat.dem", 13, "T"): (5, 0),
    ("Ancient_vs_kaljukostaja.dem", 1, "CT"): (1, 0),
    ("Ancient_vs_kaljukostaja.dem", 13, "T"): (3, 0),
    ("Anubis_vs_ryhmarama.dem", 1, "CT"): (4, 0),
    ("Anubis_vs_ryhmarama.dem", 13, "T"): (4, 0),
    ("inferno_vs_ryhmarama.dem", 1, "CT"): (2, 0),
    ("inferno_vs_ryhmarama.dem", 13, "T"): (3, 0),
    ("Nuke_vs_imuaijat.dem", 2, "CT"): (0, 0),
    ("Nuke_vs_imuaijat.dem", 16, "T"): (4, 4),
    ("Ancient_vs_kaljukostaja.dem", 14, "T"): (5, 5),
}

#: Pistoolikierrokset MR12:ssa. Luettelona, jotta väite "kaikki kahdeksan" on
#: laskettavissa kiinnikkeestä eikä kirjoitettu käsin.
PISTOL_ROUNDS: tuple[int, ...] = (1, 13)

#: Mitattu **vastaesimerkki** väitteelle "pistoolikierroksella aseistettuja on
#: aina 0". Vastustaja (Ryhmä Rämä) Anubiksen kierroksella 13: panssaroituja 3,
#: aseistettuja 1. 800 dollarilla ei osteta sekä kevlaria että parannettua
#: asetta, mutta **poimittu ase** riittää aseistamaan -- luku on siis rahan
#: seuraus eikä sääntö, ja dokumentaatio sanoo "käytännössä" eikä "aina".
ARMED_ON_A_PISTOL_ROUND = ("Anubis_vs_ryhmarama.dem", 13, "CT", (3, 1))

#: Joukkue, jonka riveistä mittatikku puhuu. Rivit tunnistetaan klaaninimestä
#: eikä kokoonpanotunnisteesta: tunniste on hash pelaajajoukosta ja muuttuisi
#: vaihtopelaajasta, jolloin testi kaatuisi väärästä syystä.
ARMOR_TRUTH_TEAM = "MatureMayhem"


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name", sorted({demo for demo, _, _ in ARMOR_TRUTH})
)
def test_the_armor_counter_matches_the_measured_truth(demo_name: str) -> None:
    """Mitatut luvut demosta, ei muistista -- ja molemmat laskurit rinnakkain.

    Neljä pistoolikierrosta ovat mukana siksi, että niillä laskurit **eroavat**
    (panssaria on, aseita ei), ja kolme muuta siksi, että niillä ne ovat lähes
    samat. Pelkkä ero tai pelkkä yhtäläisyys menisi läpi myös väärällä
    toteutuksella: ensimmäisen läpäisisi vakio, jälkimmäisen kopioitu sarake.
    """
    tables = real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    ours = {
        row["lineup_key"]
        for row in tables.lineups.iter_rows(named=True)
        if row["clan_name"] == ARMOR_TRUTH_TEAM
    }
    assert ours, f"{ARMOR_TRUTH_TEAM} ei ole demon kokoonpanotaulussa"

    df = mark_played_rounds(tables.rounds).filter(pl.col("round_no").is_not_null())
    for (demo, round_no, side), expected in ARMOR_TRUTH.items():
        if demo != demo_name:
            continue
        row = df.filter(
            (pl.col("round_no") == round_no) & (pl.col("side") == side)
        )
        assert row.height == 1, (round_no, side)
        observed = row.to_dicts()[0]
        assert observed["lineup_key"] in ours, (round_no, side)
        assert (
            observed[ARMORED_COLUMN],
            observed[ARMED_COLUMN],
        ) == expected, (round_no, side)


def test_the_armor_fixture_covers_the_claim_the_docs_make() -> None:
    """Kiinnike kattaa väitteen "neljä demoa, kaikki kahdeksan pistoolia".

    Ei tarvitse demoja: tämä lukee kiinnikkeen eikä aineistoa. Ilman sitä
    dokumentaation luku ja regressiotestin kattavuus voisivat erkaantua --
    ja juuri niin oli, kun kiinnike pinnasi kaksi demoa ja neljä kierrosta.
    """
    pistols = [key for key in ARMOR_TRUTH if key[1] in PISTOL_ROUNDS]
    assert len({demo for demo, _, _ in pistols}) == 4
    assert len(pistols) == 8
    # Ja väite "aseistettuja 0 kaikilla kahdeksalla" on kiinnikkeessä.
    assert all(ARMOR_TRUTH[key][1] == 0 for key in pistols)


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_armored_count_stays_within_its_divisor(demo_name: str) -> None:
    """``0 <= players_armored_buy_end <= players_buy_end`` joka rivillä.

    Ja lisäksi: aseistettu on **osajoukko** panssaroiduista, koska aseistetun
    ehto sisältää panssarin. Rivi, jolla aseistettuja on enemmän, tarkoittaisi
    että laskurit lukevat eri pelaajajoukkoa tai eri tickiä.

    **Sarakkeiden eroa ei vaadita.** Demo, jossa panssarin ostanut osti aina
    myös aseen, tuottaa laillisesti identtiset sarakkeet -- eroavuusväite
    kaatuisi siitä oikeasta aineistosta. Se, että laskurit ovat eri
    havaintoja, todennetaan :data:`ARMOR_TRUTH`in pistoolikierroksilla ja
    synteettisillä testeillä, joissa asetelma on valittu eikä satunnainen.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    observed = df.filter(pl.col(ARMORED_COLUMN).is_not_null())
    assert not observed.is_empty()
    assert observed.select(
        (pl.col(ARMORED_COLUMN) >= 0)
        & (pl.col(ARMORED_COLUMN) <= pl.col("players_buy_end"))
    ).to_series().all()

    both = observed.filter(pl.col(ARMED_COLUMN).is_not_null())
    assert both.select(
        pl.col(ARMED_COLUMN) <= pl.col(ARMORED_COLUMN)
    ).to_series().all()

    # Sääntö erottaa oikeasti: yksi ainoa arvo koko taulussa tarkoittaisi,
    # ettei se pure aineistoon lainkaan.
    assert observed[ARMORED_COLUMN].n_unique() > 1


@pytest.mark.demo
def test_a_pistol_round_can_have_an_armed_player_after_all() -> None:
    """Mitattu vastaesimerkki: "aina 0" olisi väärä sääntö.

    800 dollarilla ei osteta sekä kevlaria (650) että parannettua asetta,
    joten aseistettuja on pistoolikierroksella **tyypillisesti** 0 -- mutta
    poimittu ase riittää aseistamaan. Ilman tätä testiä dokumentaation
    varovainen muotoilu näyttäisi turhalta ja joku palauttaisi sanan "aina".
    """
    demo_name, round_no, side, expected = ARMED_ON_A_PISTOL_ROUND
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    row = df.filter((pl.col("round_no") == round_no) & (pl.col("side") == side))
    assert row.height == 1
    observed = row.to_dicts()[0]
    assert (observed[ARMORED_COLUMN], observed[ARMED_COLUMN]) == expected
    assert observed[ARMED_COLUMN] > 0
