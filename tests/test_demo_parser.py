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
from pappascout.adapters.demo_parser import Demoparser2Rounds
from pappascout.adapters.protocols import ROUNDS_ADAPTER_COLUMNS, DemoRoundsParser
from pappascout.domain.rounds import CT_WIN_REASONS, T_WIN_REASONS
from pappascout.domain.rounds import REQUIRED_COLUMNS as NUMBERING_COLUMNS
from pappascout.domain.rounds import check_win_reasons, mark_played_rounds
from pappascout.domain.schemas import ROUNDS
from pappascout.errors import ParseError

FAKE_DEMO = DEMO_MAGIC + b"\x00" + b"tekaistua sisaltoa" * 64


# --- Portti -------------------------------------------------------------------


def test_adapter_implements_the_port() -> None:
    assert isinstance(Demoparser2Rounds(), DemoRoundsParser)


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
    lahde = tmp_path / "a.dem.zst"
    lahde.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    kohde = decompress_to(lahde, tmp_path / "ulos.dem")
    assert kohde.read_bytes() == FAKE_DEMO


def test_gzip_round_trip_is_byte_identical(tmp_path: Path) -> None:
    lahde = tmp_path / "a.dem.gz"
    lahde.write_bytes(gzip.compress(FAKE_DEMO))
    kohde = decompress_to(lahde, tmp_path / "ulos.dem")
    assert kohde.read_bytes() == FAKE_DEMO


def test_readable_demo_cleans_up_the_temp_file(tmp_path: Path) -> None:
    lahde = tmp_path / "a.dem.zst"
    lahde.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    with readable_demo(lahde) as purettu:
        assert purettu.read_bytes() == FAKE_DEMO
        tilapainen = purettu
    assert not tilapainen.exists()
    assert not tilapainen.parent.exists()


def test_readable_demo_does_not_copy_an_uncompressed_demo(tmp_path: Path) -> None:
    lahde = tmp_path / "a.dem"
    lahde.write_bytes(FAKE_DEMO)
    with readable_demo(lahde) as polku:
        assert polku == lahde


def test_decompression_never_writes_into_the_archive(tmp_path: Path) -> None:
    """Purku menee koneen temp-hakemistoon, ei OneDrive-arkistoon."""
    arkisto = tmp_path / "arkisto"
    arkisto.mkdir()
    lahde = arkisto / "import" / "a.dem.zst"
    lahde.parent.mkdir()
    lahde.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    with readable_demo(lahde) as purettu:
        assert arkisto not in purettu.parents
    assert list(arkisto.rglob("*.dem")) == []


# --- Virheet -------------------------------------------------------------------


def test_text_file_with_dem_suffix_is_a_finnish_error(tmp_path: Path) -> None:
    path = tmp_path / "eidemo.dem"
    path.write_text("Tämä on tekstitiedosto, ei demo.\n", encoding="utf-8")
    with pytest.raises(ParseError) as exc:
        check_demo_magic(path)
    viesti = str(exc.value)
    assert "PBDEMS2" in viesti
    assert "ei ole CS2-demo" in viesti


def test_text_file_fails_before_demoparser_is_called(tmp_path: Path) -> None:
    path = tmp_path / "eidemo.dem"
    path.write_text("ei demo", encoding="utf-8")
    with pytest.raises(ParseError, match="PBDEMS2"):
        Demoparser2Rounds().parse_rounds(path)


def test_missing_file_is_a_finnish_error(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as exc:
        Demoparser2Rounds().parse_rounds(tmp_path / "ei-ole.dem")
    assert "ei löytynyt" in str(exc.value)


def test_broken_zstd_is_a_finnish_error(tmp_path: Path) -> None:
    ehja = zstandard.ZstdCompressor().compress(FAKE_DEMO)
    katkaistu = tmp_path / "katkennut.dem.zst"
    katkaistu.write_bytes(ehja[: len(ehja) // 2])
    with pytest.raises(ParseError) as exc:
        Demoparser2Rounds().parse_rounds(katkaistu)
    assert "purku epäonnistui" in str(exc.value)


def test_truncated_demo_is_a_finnish_error(tmp_path: Path) -> None:
    """Otsikko on oikea mutta sisältö loppuu kesken -- demoparser2 kaatuu."""
    path = tmp_path / "katkennut.dem"
    path.write_bytes(FAKE_DEMO)
    with pytest.raises(ParseError) as exc:
        Demoparser2Rounds().parse_rounds(path)
    assert "Lataa demo uudelleen" in str(exc.value) or "katkennut" in str(exc.value)


def test_zstd_compressed_error_page_is_refused(tmp_path: Path) -> None:
    """FACEIT voi palauttaa latauslinkin takaa virhesivun.

    Se pakkautuu moitteettomasti zstd-tiedostoksi, joten purku onnistuu --
    virheen on tultava vasta puretun sisällön otsikkotarkistuksesta.
    """
    sivu = b"<html><head><title>404</title></head><body>Not Found</body></html>"
    polku = tmp_path / "lataus.dem.zst"
    polku.write_bytes(zstandard.ZstdCompressor().compress(sivu))

    with pytest.raises(ParseError) as exc:
        Demoparser2Rounds().parse_rounds(polku)
    viesti = str(exc.value)
    assert "PBDEMS2" in viesti
    assert "ei ole CS2-demo" in viesti


def test_gzip_compressed_error_page_is_refused(tmp_path: Path) -> None:
    polku = tmp_path / "lataus.dem.gz"
    polku.write_bytes(gzip.compress(b"<html>403 Forbidden</html>"))
    with pytest.raises(ParseError, match="PBDEMS2"):
        Demoparser2Rounds().parse_rounds(polku)


@pytest.mark.parametrize(
    "nimi,odotettu",
    [
        ("1-abc-1-1.dem.zst", "1-abc-1-1.dem"),
        ("1-abc-1-1.dem.gz", "1-abc-1-1.dem"),
        ("1-abc-1-1.dem", "1-abc-1-1.dem"),
        ("ottelu.2026.01.01.dem.zst", "ottelu.2026.01.01.dem"),
        ("ilman-paatetta.zst", "ilman-paatetta.dem"),
    ],
)
def test_decompressed_name_keeps_the_whole_name(nimi: str, odotettu: str) -> None:
    """Nimeä ei katkaista ensimmäisestä pisteestä.

    FACEITin tiedostonimissä on useita pisteitä, ja katkaisu tuottaisi eri
    demoille helposti saman purkunimen.
    """
    assert decompressed_name(Path("/x") / nimi) == odotettu


def test_partial_decompression_leaves_no_tmp_file(tmp_path: Path) -> None:
    """Keskeytynyt purku ei saa jättää tiedostoa, joka näyttäisi demolta."""
    ehja = zstandard.ZstdCompressor().compress(FAKE_DEMO)
    katkaistu = tmp_path / "katkennut.dem.zst"
    katkaistu.write_bytes(ehja[: len(ehja) // 2])
    kohde = tmp_path / "ulos" / "katkennut.dem"

    with pytest.raises(ParseError):
        decompress_to(katkaistu, kohde)
    assert not kohde.exists()
    assert list(kohde.parent.glob("*.tmp")) == []


# --- Oikeat demot --------------------------------------------------------------


@pytest.mark.demo
def test_ancient_has_twenty_one_played_rounds() -> None:
    df = Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM))
    pelatut = mark_played_rounds(df).filter(pl.col("round_no").is_not_null())
    assert pelatut["round_no"].n_unique() == ANCIENT_ROUNDS
    assert pelatut.height == ANCIENT_ROUNDS * 2
    assert sorted(pelatut["round_no"].unique().to_list()) == list(
        range(1, ANCIENT_ROUNDS + 1)
    )


@pytest.mark.demo
def test_ancient_columns_match_the_port_contract() -> None:
    df = Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM))
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
    df = mark_played_rounds(Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM)))
    numeroimattomat = df.filter(pl.col("round_no").is_null())
    assert numeroimattomat.height == 2  # yksi rivi kummallekin joukkueelle
    assert numeroimattomat["round_raw"].unique().to_list() == [1]


@pytest.mark.demo
def test_ancient_observations_are_plausible() -> None:
    """Havaitut arvot ovat oikeasta demosta, eivät johdettuja tai tyhjiä."""
    df = mark_played_rounds(
        Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM))
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
    voitot = sorted(
        df.group_by("lineup_key").agg(pl.col("won").sum())["won"].to_list()
    )
    assert voitot == [8, 13]


@pytest.mark.demo
def test_ancient_pistol_round_shows_a_pistol_economy() -> None:
    """Kierros 1 on pistoolikierros: varustearvo on murto-osa täydestä."""
    df = mark_played_rounds(
        Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM))
    ).filter(pl.col("round_no") == 1)
    assert df.height == 2
    # 5 pelaajaa x (pistooli 200 + kevlar 650..1000) -> selvästi alle 10 000 $.
    assert df["equip_freeze_end"].max() < 10_000
    assert df["equip_freeze_end"].min() > 0


@pytest.mark.demo
def test_zst_and_dem_give_byte_identical_tables(tmp_path: Path) -> None:
    """Sama demo pakattuna ja purettuna tuottaa saman taulun tavu tavulta."""
    purettu = Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM))
    pakattu = Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_ZST))
    assert purettu.equals(pakattu)

    a = tmp_path / "a.parquet"
    b = tmp_path / "b.parquet"
    purettu.write_parquet(a)
    pakattu.write_parquet(b)
    assert hashlib.sha256(a.read_bytes()).hexdigest() == (
        hashlib.sha256(b.read_bytes()).hexdigest()
    )


@pytest.mark.demo
def test_nuke_reaches_round_twenty_eight_in_overtime() -> None:
    df = mark_played_rounds(
        Demoparser2Rounds().parse_rounds(require_demo(NUKE_ZST))
    ).filter(pl.col("round_no").is_not_null())
    assert df["round_no"].max() == NUKE_ROUNDS
    assert df.height == NUKE_ROUNDS * 2
    # Jatkoajan kierrokset 25-28 ovat mukana normaalisti.
    jatkoaika = df.filter(pl.col("round_no") > 24)
    assert sorted(jatkoaika["round_no"].unique().to_list()) == [25, 26, 27, 28]
    assert jatkoaika["won"].null_count() == 0


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_nimi,odotetut_kierrokset",
    [(ANCIENT_DEM, ANCIENT_ROUNDS), (NUKE_ZST, NUKE_ROUNDS)],
)
def test_real_demos_obey_the_cs2_win_rule(
    demo_nimi: str, odotetut_kierrokset: int
) -> None:
    """CS2:n sääntö pitää molemmissa oikeissa demoissa.

    T voittaa vain eliminoimalla CT:t tai räjäyttämällä pommin; CT
    eliminoimalla, purkamalla tai ajan loppuessa. Jos tämä pettäisi, puolet
    olisivat menneet väärin päin ja jokainen havainto olisi väärällä
    joukkueella.
    """
    df = mark_played_rounds(
        Demoparser2Rounds().parse_rounds(require_demo(demo_nimi))
    ).filter(pl.col("round_no").is_not_null())

    assert df["round_no"].n_unique() == odotetut_kierrokset
    check_win_reasons(df)  # nostaa ParseErrorin, jos sääntö pettää

    voitot = df.filter(pl.col("won"))
    for side, sallitut in (("T", T_WIN_REASONS), ("CT", CT_WIN_REASONS)):
        syyt = set(voitot.filter(pl.col("side") == side)["win_reason"].unique())
        assert syyt <= set(sallitut), (side, syyt)


@pytest.mark.demo
def test_round_raw_is_the_demo_own_counter() -> None:
    """``round_raw`` tulee ``round_end``-tapahtuman ``round``-kentästä.

    Ancientissa puukkokierros on demon kierros 1, joten pelatut kierrokset
    1..21 vastaavat raaka-arvoja 2..22. Aukko on nimenomaan se todiste, että
    puukkokierros ohitettiin.
    """
    df = mark_played_rounds(Demoparser2Rounds().parse_rounds(require_demo(ANCIENT_DEM)))
    pelatut = df.filter(pl.col("round_no").is_not_null())
    assert sorted(pelatut["round_raw"].unique().to_list()) == list(range(2, 23))
    assert df.filter(pl.col("round_no").is_null())["round_raw"].unique().to_list() == [1]
