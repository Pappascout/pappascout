"""Tests for the demo adapter: decompression, header check and a real demo.

Two layers:

* **Without demos** -- decompression, recognition and the error messages are
  tested with small hand-made files. These always run.
* **With a real demo** (``@pytest.mark.demo``) -- the round count, overtime
  and the byte-for-byte match of the decompression. These skip themselves if
  the 100-230 MB demos are not on the machine.
"""

from __future__ import annotations

import gzip
import hashlib
import io
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
    PAWNLESS_DEMO,
    PAWNLESS_DEMO_FILE,
    PAWNLESS_DEMO_POINTS,
    PAWNLESS_DEMO_ROUNDS,
    PAWNLESS_DEMO_ROWS,
    REAL_SETTINGS,
    require_demo,
)
from pappascout.adapters.decompress import (
    declared_size,
    DEMO_MAGIC,
    decompressed_name,
    check_demo_magic,
    decompress_to,
    is_compressed,
    readable_demo,
)
from pappascout.adapters.demo_parser import Demoparser2Adapter
from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
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
    MATCH,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
)

from test_calibration import ARMED_TRUTH
from pappascout.errors import ParseError
from pappascout.stages import parse as parse_stage

FAKE_DEMO = DEMO_MAGIC + b"\x00" + b"tekaistua sisaltoa" * 64

#: The sample points the demo tests use. The same list as in
#: ``settings.toml``'s ``[parse]`` section;
#: :func:`test_snapshot_seconds_match_the_real_settings` makes sure they
#: cannot diverge. A constant rather than a settings load, so that importing
#: the module does not read files -- that would happen in a
#: ``-m "not demo"`` run too, where no demo is touched.
SNAPSHOT_SECONDS: tuple[float, ...] = (6.0, 15.0, 30.0, 45.0)


@lru_cache(maxsize=1)
def _parse_settings():
    """The real ``[parse]`` settings, read only when they are needed."""
    return load_settings(REAL_SETTINGS, env_files=()).parse


def real_parser() -> Demoparser2Adapter:
    """The adapter with production's first-contact and area rules.

    The demo tests are run with production's values -- with invented exclusion
    lists or an invented ``area_snap_units`` they would prove nothing about a
    real run.
    """
    parse_settings = _parse_settings()
    return Demoparser2Adapter(
        exclude_weapons=parse_settings.first_contact_exclude_weapons,
        fallback_death=parse_settings.first_contact_fallback_death,
        area_snap_units=parse_settings.area_snap_units,
        buy_window_seconds=parse_settings.buy_window_seconds,
    )


def test_snapshot_seconds_match_the_real_settings() -> None:
    """The tests' sample points are the same as production's.

    If they diverged, the demo tests' figures (94 sample points) would measure
    a different configuration from the one the archive is really built with.
    """
    assert tuple(_parse_settings().snapshot_seconds) == SNAPSHOT_SECONDS


# --- The port -----------------------------------------------------------------


def test_adapter_implements_the_port() -> None:
    assert isinstance(Demoparser2Adapter(), DemoParser)


def test_port_contract_is_an_exact_column_set() -> None:
    """The contract is an exact set, not a subset.

    ``map_demo_id`` is absent, because the adapter cannot know the archive's
    id. ``score_start`` and ``score_end`` are present, because
    ``mark_played_rounds`` requires them -- without them in the contract
    another adapter would pass the stage's column check and fail only in the
    domain layer.
    """
    assert set(ROUNDS_ADAPTER_COLUMNS) == (set(ROUNDS) - {"map_demo_id"}) | {
        "score_start",
        "score_end",
    }
    assert set(NUMBERING_COLUMNS) <= set(ROUNDS_ADAPTER_COLUMNS)
    assert len(ROUNDS_ADAPTER_COLUMNS) == len(set(ROUNDS_ADAPTER_COLUMNS))


def test_events_port_contract_is_events_without_the_archive_id() -> None:
    """The events table's contract is ``EVENTS`` without ``map_demo_id``.

    The grenade's own running number is not in it: it is the adapter's
    internal key for the pair, and it dies before the table crosses the port.
    """
    assert set(EVENTS_ADAPTER_COLUMNS) == set(EVENTS) - {"map_demo_id"}
    assert len(EVENTS_ADAPTER_COLUMNS) == len(set(EVENTS_ADAPTER_COLUMNS))


def test_ticks_port_contract_is_ticks_without_the_archive_id() -> None:
    """The sample point table's contract is ``TICKS`` without ``map_demo_id``.

    Everything else is in it, ``round_no`` included -- it is always empty in
    the adapter's table, but its place is reserved so that the stage can fill
    it in without the column order changing.
    """
    assert set(TICKS_ADAPTER_COLUMNS) == set(TICKS) - {"map_demo_id"}
    assert len(TICKS_ADAPTER_COLUMNS) == len(set(TICKS_ADAPTER_COLUMNS))


# --- Recognition and decompression ---------------------------------------------


def test_plain_demo_is_not_compressed(tmp_path: Path) -> None:
    path = tmp_path / "a.dem"
    path.write_bytes(FAKE_DEMO)
    assert not is_compressed(path)


def test_zstd_and_gzip_are_recognised_from_content_not_suffix(tmp_path: Path) -> None:
    """A misnamed file is still recognised correctly."""
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
    """Decompression goes to the machine's temp directory, not the archive.

    The archive lives in a synchronised folder that two machines share, and a
    sync client would upload every temporary file written there.
    """
    archive_dir = tmp_path / "arkisto"
    archive_dir.mkdir()
    source = archive_dir / "import" / "a.dem.zst"
    source.parent.mkdir()
    source.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))
    with readable_demo(source) as decompressed:
        assert archive_dir not in decompressed.parents
    assert list(archive_dir.rglob("*.dem")) == []


# --- A truncated compressed file (Story 3.6, A1) -------------------------------
#
# **The size of the test input is the claim itself, not a convenience.**
#
# Measured 2026-09-05: a payload under a megabyte fits into a single zstd
# block, so truncated in half it decompresses to **zero bytes** -- that is, it
# hits the old "the result was an empty file" guard, which existed before this
# story::
#
#     raw  1,160 -> compressed    44 -> truncated   22 -> decompressed          0
#     raw  4,104 -> compressed    26 -> truncated   13 -> decompressed          0
#     raw 40,000,007 -> compressed 1,249 -> truncated 624 -> decompressed 19,529,728
#
# The reason the new frame-size guard exists is **the opposite case**: the
# decompression succeeds partially but not emptily, and then no earlier check
# sees anything wrong. That is exactly how a real demo behaved (148,871,905
# bytes cut in half -> 104,464,384 bytes, with the frame declaring
# 208,561,416).
#
# With a small input the test would fail a mutation only **because of the
# error message's wording**, and whoever fixed it could fix it by changing the
# assertion -- leaving the real fault in place. That is why the truncation
# tests use :data:`BIG_DEMO`, and
# :func:`test_the_truncation_fixture_decompresses_partially_not_to_nothing`
# guards that property separately.
#
# The cost is negligible: 40 MB of nothing but ``x`` compresses to 1,249 bytes
# and takes 0.04 s to decompress.

#: A large, extremely compressible demo for the truncation tests.
BIG_DEMO = DEMO_MAGIC + b"x" * 40_000_000
BIG_ZSTD = zstandard.ZstdCompressor().compress(BIG_DEMO)
BIG_TRUNCATED = BIG_ZSTD[: len(BIG_ZSTD) // 2]


def partial_size(blob: bytes) -> int:
    """How many bytes a truncated frame decompresses to **on its own**.

    The measurement is made with ``zstandard`` directly and not through
    pappascout's decompression: a property of the test input cannot be proved
    with the very code the input exists to test.
    """
    return len(zstandard.ZstdDecompressor().stream_reader(io.BytesIO(blob)).read())


def test_the_truncation_fixture_decompresses_partially_not_to_nothing() -> None:
    """**The input decompresses partially but not emptily -- or it tests the
    wrong thing.**

    This test does not measure production code at all. It measures the test
    data, and that is deliberate: if the input decompressed to zero, every
    truncation test below would pass on the strength of the old emptiness
    check, and removing the frame-size guard would show only in the wording of
    an error message.
    """
    osittainen = partial_size(BIG_TRUNCATED)

    assert osittainen > 0, "the input decompresses to nothing -- it hits the old guard"
    assert osittainen < len(BIG_DEMO), "the input is not partial at all"
#
# Measured 2026-09-05 with a real demo: ``ANCIENT_vs_RCAVE_VETERANS.dem.zst``
# (148,871,905 bytes) cut in half decompressed to 104,464,384 bytes **without
# an error**, even though the frame declares 208,561,416. The decompressed
# beginning is a valid CS2 demo: ``PBDEMS2`` is in place and the header's map
# name read as ``de_ancient``. The shortfall is therefore invisible to
# anything that can be seen by looking at the file -- it only surfaces during
# the parse, by which time the import has already deleted the source file.
#
# These tests are run **without real demos**: the same fault reproduces with
# two kilobytes, because it is about the frame's structure and not its size.


def test_zstd_declares_its_decompressed_size(tmp_path: Path) -> None:
    """The frame declares the decompressed size, the only independent source.

    ``fetch`` gets a ``Content-Length`` from the source; a file copied by hand
    has nobody to state its right length -- except the file itself. The claim
    is about the library and not an assumption: if ``ZstdCompressor`` stopped
    writing the size, the whole guard would be silently toothless.
    """
    source = tmp_path / "a.dem.zst"
    source.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))

    assert declared_size(source) == len(FAKE_DEMO)


def test_an_uncompressed_demo_declares_nothing(tmp_path: Path) -> None:
    """An uncompressed demo has no length -- and none may be invented."""
    source = tmp_path / "a.dem"
    source.write_bytes(FAKE_DEMO)

    assert declared_size(source) is None


def test_a_truncated_zstd_is_refused_instead_of_decompressing_silently(
    tmp_path: Path,
) -> None:
    """**A partial compressed file must not decompress silently.**

    Without this guard a truncated demo is imported into the archive looking
    right, ``length_verified`` is ``true`` and the source file is deleted --
    and ``import/`` holds six of the season's 12 league demos that FACEIT no
    longer offers.

    The input decompresses **partially but not emptily**, so no earlier check
    sees anything wrong with it: the size the frame declares is the only thing
    that exposes it.

    The message names **both numbers**, because the difference is the whole
    observation: a bare "the file is corrupt" does not say whether it is about
    a byte or a hundred megabytes, nor that the copying may still be in
    progress.
    """
    source = tmp_path / "katkennut.dem.zst"
    source.write_bytes(BIG_TRUNCATED)

    with pytest.raises(ParseError) as err:
        decompress_to(source, tmp_path / "ulos.dem")

    message = str(err.value)
    assert str(len(BIG_DEMO)) in message
    assert str(partial_size(BIG_TRUNCATED)) in message
    assert "came up short" in message
    assert err.value.advice
    # The advice is to wait rather than to download again: the most common
    # cause is a copy still in progress, or a sync client still uploading.
    assert "Wait" in err.value.advice


def test_a_truncated_zstd_leaves_no_half_file_behind(tmp_path: Path) -> None:
    """A refused decompression leaves no half file that would look like a demo.

    The half here is 19.5 MB of valid data with a ``PBDEMS2`` header -- exactly
    the kind of file that would pass every other check.
    """
    source = tmp_path / "katkennut.dem.zst"
    source.write_bytes(BIG_TRUNCATED)
    target = tmp_path / "ulos.dem"

    with pytest.raises(ParseError):
        decompress_to(source, target)

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_readable_demo_refuses_a_truncated_archive(tmp_path: Path) -> None:
    """The same guard on the route the import and the parse use as well."""
    source = tmp_path / "katkennut.dem.zst"
    source.write_bytes(BIG_TRUNCATED)

    with pytest.raises(ParseError, match="came up short"):
        with readable_demo(source):
            pass


def test_a_truncated_gzip_is_refused_too(tmp_path: Path) -> None:
    """With gzip the same job is done by the stream's end marker.

    A different mechanism, the same promise: a truncated file does not
    decompress silently. The claim is here because
    :func:`~pappascout.stages.import_demo.length_source` rests on exactly that
    when it calls gzip a checked format.

    The same large payload as in the zstd tests: a small gzip would
    decompress to nothing, and then the test would measure the emptiness check
    rather than the end marker.
    """
    whole = gzip.compress(BIG_DEMO)
    source = tmp_path / "katkennut.dem.gz"
    source.write_bytes(whole[: len(whole) // 2])

    with pytest.raises(ParseError):
        decompress_to(source, tmp_path / "ulos.dem")


def test_a_complete_archive_still_decompresses(tmp_path: Path) -> None:
    """The guard must not reject an intact file.

    Saying so separately is not a formality: a size comparison that counts
    wrongly would bring down every import -- and that would be just as bad a
    fault in the other direction.
    """
    source = tmp_path / "ehja.dem.zst"
    source.write_bytes(zstandard.ZstdCompressor().compress(FAKE_DEMO))

    target = decompress_to(source, tmp_path / "ulos.dem")

    assert target.read_bytes() == FAKE_DEMO


# --- Errors --------------------------------------------------------------------


def test_text_file_with_dem_suffix_is_not_a_cs2_demo(tmp_path: Path) -> None:
    path = tmp_path / "eidemo.dem"
    path.write_text("This is a text file, not a demo.\n", encoding="utf-8")
    with pytest.raises(ParseError) as exc:
        check_demo_magic(path)
    message = str(exc.value)
    assert "PBDEMS2" in message
    assert "is not a CS2 demo" in message


def test_text_file_fails_before_demoparser_is_called(tmp_path: Path) -> None:
    path = tmp_path / "eidemo.dem"
    path.write_text("ei demo", encoding="utf-8")
    with pytest.raises(ParseError, match="PBDEMS2"):
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds


def test_missing_file_reports_that_no_demo_was_found(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(
            tmp_path / "ei-ole.dem", SNAPSHOT_SECONDS
        )
    assert "No demo file was found" in str(exc.value)


def test_broken_zstd_reports_the_read_came_up_short(tmp_path: Path) -> None:
    """A truncated compressed file fails cleanly -- and **states the numbers**.

    The claim changed in Story 3.6, and the change is deliberate. This case
    used to hit the "the result was an empty file" guard, which holds only
    when the truncation falls inside the first block. With a large demo it
    does not: it was measured 2026-09-05 that a real demo cut in half
    decompressed to 104,464,384 bytes rather than zero -- that is, it **passed
    the old guard**. Now the size the frame declares is what the result is
    checked against, and the message names both numbers.

    The input is therefore :data:`BIG_DEMO`: with a small file this test would
    still be measuring the old emptiness check and not the guard whose name it
    carries.
    """
    truncated = tmp_path / "katkennut.dem.zst"
    truncated.write_bytes(BIG_TRUNCATED)
    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(truncated, SNAPSHOT_SECONDS).rounds
    assert "came up short" in str(exc.value)
    assert str(len(BIG_DEMO)) in str(exc.value)


def test_a_truncated_demo_says_to_download_it_again(tmp_path: Path) -> None:
    """The header is right but the content runs out -- demoparser2 fails.

    The failure surfaces in ``parse_header``, so the message is the header
    reader's: it names both possible causes and ends with what to do. Either
    way the library's own exception is wrapped into a ``ParseError``.

    Renamed in T4: the name used to claim the message is in Finnish, and that
    stopped being true when this module was translated. The second branch used
    to read ``"katkennut"``, which matched **the temporary file's name** and
    not the message at all; it now names the diagnosis the message really
    carries.
    """
    path = tmp_path / "katkennut.dem"
    path.write_bytes(FAKE_DEMO)
    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds
    assert "download the demo again" in str(exc.value) or "corrupt" in str(
        exc.value
    )


def test_zstd_compressed_error_page_is_refused(tmp_path: Path) -> None:
    """FACEIT can return an error page from behind a download link.

    It compresses into a zstd file perfectly well, so the decompression
    succeeds -- the error has to come from the header check on the
    decompressed content.
    """
    error_page = b"<html><head><title>404</title></head><body>Not Found</body></html>"
    path = tmp_path / "lataus.dem.zst"
    path.write_bytes(zstandard.ZstdCompressor().compress(error_page))

    with pytest.raises(ParseError) as exc:
        Demoparser2Adapter().parse_demo(path, SNAPSHOT_SECONDS).rounds
    message = str(exc.value)
    assert "PBDEMS2" in message
    assert "is not a CS2 demo" in message


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
    """The name is not cut at the first dot.

    FACEIT's file names have several dots, and cutting would easily give
    different demos the same decompressed name.
    """
    assert decompressed_name(Path("/x") / name) == expected


def test_partial_decompression_leaves_no_tmp_file(tmp_path: Path) -> None:
    """An interrupted decompression must leave no file that looks like a demo."""
    intact = zstandard.ZstdCompressor().compress(FAKE_DEMO)
    truncated = tmp_path / "katkennut.dem.zst"
    truncated.write_bytes(intact[: len(intact) // 2])
    target = tmp_path / "ulos" / "katkennut.dem"

    with pytest.raises(ParseError):
        decompress_to(truncated, target)
    assert not target.exists()
    assert list(target.parent.glob("*.tmp")) == []


# --- Real demos ----------------------------------------------------------------


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
    # round_no is left empty: the numbering is decided by domain.rounds.
    assert df["round_no"].null_count() == df.height


@pytest.mark.demo
def test_ancient_knife_round_is_present_but_unnumbered() -> None:
    """The knife round is in the demo, but it is not a round played."""
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    )
    unnumbered = df.filter(pl.col("round_no").is_null())
    assert unnumbered.height == 2  # one row for each team
    assert unnumbered["round_raw"].unique().to_list() == [1]


@pytest.mark.demo
def test_ancient_observations_are_plausible() -> None:
    """The observed values come from a real demo, not derived or empty."""
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

    # Every round has exactly one winner.
    per_round = df.group_by("round_no").agg(pl.col("won").sum().alias("voittajia"))
    assert per_round["voittajia"].unique().to_list() == [1]

    # Ancient ended 13-8 (FACEIT). The wins split that way between lineups.
    wins = sorted(
        df.group_by("lineup_key").agg(pl.col("won").sum())["won"].to_list()
    )
    assert wins == [8, 13]


@pytest.mark.demo
def test_ancient_pistol_round_shows_a_pistol_economy() -> None:
    """Round 1 is the pistol round: the equipment value is a fraction of full."""
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no") == 1)
    assert df.height == 2
    # 5 players x (pistol 200 + kevlar 650..1000) -> well under $10,000.
    assert df["equip_buy_end"].max() < 10_000
    assert df["equip_buy_end"].min() > 0


@pytest.mark.demo
def test_zst_and_dem_give_byte_identical_tables(tmp_path: Path) -> None:
    """The same demo, compressed and decompressed, gives byte-identical tables."""
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
    # The overtime rounds 25-28 are included as normal.
    overtime = df.filter(pl.col("round_no") > 24)
    assert sorted(overtime["round_no"].unique().to_list()) == [25, 26, 27, 28]
    assert overtime["won"].null_count() == 0


@pytest.mark.demo
def test_ancient_armed_player_count_matches_the_human_reading() -> None:
    """The armed count on rounds 19-21 against the truth a human gave.

    **The rule itself is calibrated demo-free** in ``test_calibration.py``
    (``ARMED_TRUTH``), so that ``pytest -m "not demo"`` guards it on a machine
    with no demos too. This test checks the other half of the same claim: that
    the same inventories and armour values really do come out of the demo, and
    are not a recollection written into a table.

    The observations are **at the end of the buy time** (Story 1.9; before
    that, at the anchor):

    * R19 CT -> **5**, previously 4. The fifth player bought kevlar and a
      Deagle only after freezetime, so read at the anchor he looked as though
      he had stayed on the free default pistol. The product owner's verdict
      "they bought themselves empty" **is confirmed** -- $150 was left in
      pocket, not $3,750 -- but his remark "one was left without armour" was a
      reading from the wrong moment.
    * R20 T -> **5**. "2x AK, 2x tec9, 1x mac10, everyone with kevlar and a
      helmet" -- all five, the same from either moment.
    * R21 T -> **2**. Eco: two with kevlar and a bought pistol, and the third
      p250 player drops out **for lack of armour**. Story 1.5's threshold
      dropped him because $300 < $950; the same number, but now for the right
      reason.
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
    """``ARMED_TRUTH``'s inventories and armour come from the demo, not memory.

    The previous test states only three numbers, and they would still match if
    the table's rows had drifted away from the demo and the rule compensated
    for the difference. This one reads the same three **measurement points**
    again and compares each player's inventory and armour against the table's
    row.

    The tick is ``buy_end_tick`` and not ``freeze_end_tick``: the count is
    computed at the former, so reading the anchor here would compare the table
    against a moment the product does not use.

    The comparison is as a set: the table's player order is the document's,
    not the demo's.
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
            f"Round {truth.round_no} {truth.side}: the demo and ARMED_TRUTH "
            f"differ.\nDemo: {observed}\nTable: {expected}"
        )


#: Every demo on the machine: two old test demos and four league demos.
#: Claims about the data are run over the whole data set -- the league demos
#: are the data the product is ultimately judged against.
ALL_DEMOS: tuple[str, ...] = (
    ANCIENT_DEM,
    NUKE_ZST,
    *(name for name, _ in LEAGUE_DEMOS),
)


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_real_demo_has_no_unknown_inventory_items(demo_name: str) -> None:
    """The weapon classification knows every name the test demos contain.

    An unknown name arms nobody, so an unknown **weapon** would silently push
    the count down. The test does not require the list to cover the whole game
    -- it requires it to cover the data the count is calibrated against. A new
    demo may bring new names; this then says which.

    At the same time it establishes that no row was left with unread armour or
    an unread inventory: that would empty the count, and an empty row would
    look like a round without an anchor.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ()
    assert adapter.diagnostics.armed_unreadable_rows == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_armed_count_stays_within_its_divisor(demo_name: str) -> None:
    """``0 <= players_armed_buy_end <= players_buy_end`` on every row.

    The count and the divisor come from the same set of players, so exceeding
    the bound would mean two different divisors on the same row -- a fault
    that would show only in the report. And because the set is the same, an
    observation is always in both or in neither.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert (
        df[ARMED_COLUMN].null_count() == df["players_buy_end"].null_count()
    )
    # A round without an anchor is a legitimate observation and not an error:
    # they are filtered out rather than forbidden. Otherwise a future demo
    # would fail this test for the wrong reason.
    observed = df.filter(pl.col(ARMED_COLUMN).is_not_null())
    assert not observed.is_empty()
    assert observed.select(
        (pl.col(ARMED_COLUMN) >= 0)
        & (pl.col(ARMED_COLUMN) <= pl.col("players_buy_end"))
    ).to_series().all()
    # The rule really does discriminate: a single value across the whole table
    # would mean it does not bite on the data at all -- that every name is
    # unknown, for instance, and the count therefore always zero.
    assert observed[ARMED_COLUMN].n_unique() > 1


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name,expected_rounds",
    [(ANCIENT_DEM, ANCIENT_ROUNDS), (NUKE_ZST, NUKE_ROUNDS), *LEAGUE_DEMOS],
)
def test_real_demos_obey_the_cs2_win_rule(
    demo_name: str, expected_rounds: int
) -> None:
    """CS2's rule holds in both real demos.

    T wins only by eliminating the CTs or by detonating the bomb; CT by
    eliminating, by defusing, or when time runs out. If this failed, the sides
    would have gone the wrong way round and every observation would be on the
    wrong team.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS).rounds
    ).filter(pl.col("round_no").is_not_null())

    assert df["round_no"].n_unique() == expected_rounds
    check_win_reasons(df)  # raises a ParseError if the rule fails

    wins = df.filter(pl.col("won"))
    for side, allowed in (("T", T_WIN_REASONS), ("CT", CT_WIN_REASONS)):
        reasons = set(wins.filter(pl.col("side") == side)["win_reason"].unique())
        assert reasons <= set(allowed), (side, reasons)


@pytest.mark.demo
def test_round_raw_is_the_demo_own_counter() -> None:
    """``round_raw`` comes from the ``round`` field of the ``round_end`` event.

    On Ancient the knife round is the demo's round 1, so the rounds played
    1..21 correspond to raw values 2..22. The gap is precisely the evidence
    that the knife round was skipped.
    """
    df = mark_played_rounds(
        real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS).rounds
    )
    played = df.filter(pl.col("round_no").is_not_null())
    assert sorted(played["round_raw"].unique().to_list()) == list(range(2, 23))
    assert df.filter(pl.col("round_no").is_null())["round_raw"].unique().to_list() == [1]


# --- League demos and the match restart -----------------------------------------


@pytest.mark.demo
@pytest.mark.parametrize("demo_name,expected_rounds", LEAGUE_DEMOS)
def test_league_demo_parses_despite_the_match_restart(
    demo_name: str, expected_rounds: int
) -> None:
    """A league demo parses, and the restart stays outside the rounds.

    These four used to fail the monotonicity check: the restart after the
    knife round took a number from a neighbour that collided immediately with
    the demo's own next number. Now it is left unnumbered, and the round count
    is the number of the demo's ``round_end`` events minus the knife round.
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
    # The knife round is the demo's round 1, so the rounds played start at raw
    # value 2. The restart is not in the table at all, so the run is
    # unbroken.
    assert sorted(played["round_raw"].unique().to_list()) == list(
        range(2, expected_rounds + 2)
    )
    assert df.filter(pl.col("round_no").is_null())["round_raw"].to_list() == [1, 1]

    assert parser.diagnostics is not None
    # Exactly one: more would mean a different phenomenon, and the parse would
    # stop.
    assert parser.diagnostics.match_restarts == 1
    assert parser.diagnostics.rounds_seen == expected_rounds + 2


@pytest.mark.demo
@pytest.mark.parametrize("demo_name,expected_rounds", LEAGUE_DEMOS)
def test_league_demo_counters_are_observations(
    demo_name: str, expected_rounds: int
) -> None:
    """A league demo's observations come from the demo, not empty or assumed.

    The restart breaks the chain of kit and money right at the start of the
    demo, and that is exactly the place where a silently empty row would be
    easy to miss.
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
    # Every round is decided for one side or the other: exactly one winner per
    # round and exactly two rows.
    assert df.filter(pl.col("won"))["round_no"].n_unique() == expected_rounds
    assert df.group_by("round_no").len()["len"].unique().to_list() == [2]
    # The first round is a pistol round after the restart as well: if the
    # numbering had shifted by one, this would land on a full buy.
    # 5 players x (pistol 200..800 + kevlar 650..1000) -> under $10,000, where
    # a full buy is about $21,000.
    pistol = df.filter(pl.col("round_no") == 1)
    assert pistol.height == 2
    assert pistol["equip_buy_end"].max() < 10_000


@pytest.mark.demo
@pytest.mark.parametrize("demo_name,expected_rounds", LEAGUE_DEMOS)
def test_league_round_count_matches_the_demos_own_event_stream(
    demo_name: str, expected_rounds: int
) -> None:
    """The oracle for the round count is read from the demo **past our own
    numbering**.

    ``LEAGUE_DEMOS``'s figures were measured with the adapter, that is, with
    the very code they test. On their own they would therefore prove only that
    the result has not changed -- not that it is right. Here the same figure
    is derived from demoparser2's raw event stream: the number of ``round_end``
    events minus the knife round. None of this story's code is in between.

    The first ``round_end`` is an empty initial value at tick 1 (see
    :mod:`pappascout.adapters.demo_parser`), so it is excluded by the same
    condition as in the adapter -- it is a property of the library and not a
    rule of ours.
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
    """The size and the digest tell a **wrong** copy from a missing one.

    The league demos are irreplaceable: FACEIT no longer offers them. A
    missing demo may skip the test cleanly, but a wrong or incomplete copy
    must not pass silently -- the whole restart regression suite would then be
    running on different data from the one it claims.
    """
    path = require_demo(demo_name)
    size, digest = LEAGUE_DEMO_FILES[demo_name]
    assert path.stat().st_size == size, f"{demo_name}: the size does not match"

    reader = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            reader.update(chunk)
    assert reader.hexdigest() == digest, f"{demo_name}: the digest does not match"


# --- Sample points from a real demo --------------------------------------------

#: Ancient's ``env_cs_place`` areas, read from the demo 2026-08-29. The game's
#: own areas are about twice as coarse as the Total CS callouts: the Ramp
#: leading to the A site is one of its own, for instance, but Donut and Cave
#: merge into their neighbours. The list is deliberately fixed -- a set derived
#: from the demo would accept any name and would stop being a check.
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

#: Ancient's T-side areas. A CT player cannot be in these at the six-second
#: mark; if he is, the sides have gone the wrong way round and every setup
#: would have been attributed to the wrong team.
T_SIDE_PLACES: frozenset[str] = frozenset(
    {"TSpawn", "TSideUpper", "TSideLower", "Outside", "Tunnel"}
)


@pytest.fixture(scope="module")
def ancient_tables():
    """The Ancient demo's tables and diagnostics. Parsed once, not per test."""
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)
    return tables, adapter.diagnostics


@pytest.fixture(scope="module")
def ancient_ticks(ancient_tables) -> pl.DataFrame:
    """The Ancient demo's sample point table."""
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
    """All ten are recorded at every sample point, the dead included.

    The knife round (``round_raw`` 1) is the exception: there one player had
    not yet joined a team, so there are nine rows. The round is not played and
    does not reach the archive, so the exception does not show in the result
    -- but it is not patched by inventing a tenth row either.
    """
    played = ancient_ticks.filter(pl.col("round_raw") > 1)
    per_point = played.group_by("round_raw", "sample_kind", "sample_t_s").len()
    assert per_point["len"].unique().to_list() == [10]
    assert played["round_raw"].n_unique() == ANCIENT_ROUNDS


@pytest.mark.demo
def test_ancient_has_no_sample_after_the_round_ended(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Acceptance criterion: a short round gets no 45-second point.

    The proof is that the number of time points **varies** from round to
    round: if every round had four, no bounding would be happening at all.
    """
    time_samples = ancient_ticks.filter(pl.col("sample_kind") == "time")
    per_round = time_samples.group_by("round_raw").agg(
        pl.col("sample_t_s").n_unique().alias("pisteita")
    )
    counts = set(per_round["pisteita"].to_list())
    assert counts <= set(range(1, len(SNAPSHOT_SECONDS) + 1))
    assert len(counts) > 1, "no round came out short -- the bounding did not bite"
    assert set(time_samples["sample_t_s"].unique().to_list()) <= set(SNAPSHOT_SECONDS)


@pytest.mark.demo
def test_ancient_areas_are_real_callouts(ancient_ticks: pl.DataFrame) -> None:
    """Acceptance criterion: round 1's areas are Ancient's callouts.

    ``round_raw`` 2 is round 1 played (the knife round is 1). The area names
    are the game's own ``env_cs_place`` names, and they have to belong to
    Ancient's set of names -- a bare "not empty" would pass with names read
    off the wrong map, or with steamids.
    """
    first_round = ancient_ticks.filter(pl.col("round_raw") == 2)
    areas = {a for a in first_round["area"].to_list() if a}
    assert areas, "round 1's areas were empty"
    assert areas <= ANCIENT_PLACES, sorted(areas - ANCIENT_PLACES)
    # The CT players have to be in CT-side areas at the start of the round: at
    # six seconds nobody has reached the T-side areas yet.
    ct_at_start = first_round.filter(
        (pl.col("side") == "CT") & (pl.col("sample_t_s") == min(SNAPSHOT_SECONDS))
    )
    assert ct_at_start.height == 5
    assert not (set(ct_at_start["area"].to_list()) & T_SIDE_PLACES), (
        "a CT player was in a T-side area at six seconds -- the sides are "
        "most likely the wrong way round"
    )
    # And the same the other way: the Ts have not reached the CT spawn.
    t_at_start = first_round.filter(
        (pl.col("side") == "T") & (pl.col("sample_t_s") == min(SNAPSHOT_SECONDS))
    )
    assert t_at_start.height == 5
    assert "CTSpawn" not in t_at_start["area"].to_list()


@pytest.mark.demo
def test_ancient_uses_only_ancient_place_names(ancient_ticks: pl.DataFrame) -> None:
    """The whole demo's areas have to be Ancient's names, not just round 1's."""
    areas = {a for a in ancient_ticks["area"].to_list() if a}
    assert areas <= ANCIENT_PLACES, sorted(areas - ANCIENT_PLACES)
    assert len(areas) > 5, "only a few areas -- the sampling probably hits one moment"


@pytest.mark.demo
def test_ancient_coordinates_are_present_even_without_an_area(
    ancient_ticks: pl.DataFrame,
) -> None:
    """An unknown area stays null, but the row is not dropped."""
    assert ancient_ticks["x"].null_count() == 0
    assert ancient_ticks["y"].null_count() == 0
    unnamed = ancient_ticks.filter(pl.col("area").is_null())
    if not unnamed.is_empty():
        assert unnamed["x"].null_count() == 0


@pytest.mark.demo
def test_ancient_first_contact_is_found_on_every_round(
    ancient_ticks: pl.DataFrame,
) -> None:
    """A first contact is found on every round in Ancient.

    ``settings.toml`` justifies leaving ``planted_c4`` out on the grounds that
    a first contact is found without it on every round. This test is that
    claim: if it fails, the comment is wrong and not the other way round.
    """
    contacts = ancient_ticks.filter(pl.col("sample_kind") == "first_contact")
    round_count = ancient_ticks["round_raw"].n_unique()
    assert contacts["round_raw"].n_unique() == round_count
    # sample_t_s and t_s state the same moment and are not left empty.
    assert contacts["sample_t_s"].null_count() == 0
    assert (contacts["sample_t_s"] == contacts["t_s"]).all()
    # The contact happens inside the round, not before the anchor.
    assert contacts["t_s"].min() > 0


@pytest.mark.demo
def test_ancient_sample_point_count_is_exact(ancient_ticks: pl.DataFrame) -> None:
    """Ancient's exact number of sample points, locked down here.

    21 rounds played and four sample points would give 84 time points, but
    there are no points after a round has ended: the real figure is 73. There
    is one first contact per round, and the knife round (``round_raw`` 1)
    brings its own on top -- 94 sample points in all from 21 rounds played.
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
    """The rounds played have no partial sample points.

    The partial ones are the knife round's two sample points -- its 6-second
    point and its first contact -- where one player had not yet joined a team.
    Two is therefore the right figure; more would mean a prop fault. The knife
    round is not played and does not reach the archive, so the shortfall does
    not show in the result; it shows only in this figure, and that is where it
    belongs.
    """
    diagnostics_obj = ancient_tables[1]
    assert diagnostics_obj.partial_samples == 2
    assert diagnostics_obj.unknown_side_events == 0


@pytest.mark.demo
def test_ancient_alive_flag_thins_out_over_the_round(
    ancient_ticks: pl.DataFrame,
) -> None:
    """Being alive is an observation: at a later point fewer are alive."""
    time_samples = ancient_ticks.filter(pl.col("sample_kind") == "time")
    per_point = (
        time_samples.group_by("sample_t_s")
        .agg(pl.col("is_alive").mean().alias("osuus"))
        .sort("sample_t_s")
    )
    shares = per_point["osuus"].to_list()
    assert shares[0] == 1.0, "at the first point everyone should be alive"
    assert shares[-1] < shares[0]


# --- Utility from a real demo --------------------------------------------------

#: Ancient's utility figures, measured 2026-08-29. Fixed figures are not an
#: end in themselves: they are the only way to notice if the trajectory
#: segmentation starts merging or splitting grenades wrongly. A single error
#: would move these.
ANCIENT_GRENADES = 373
ANCIENT_GRENADE_TYPES: dict[str, int] = {
    "smoke": 96,
    "flashbang": 95,
    "he": 90,
    "incendiary": 62,
    "molotov": 30,
}
#: The ``[parse].area_snap_units`` at which
#: :data:`ANCIENT_DETONATIONS_WITH_AREA` was measured. The figure means
#: nothing without the bound, so the test checks the precondition rather than
#: assuming it.
#:
#: Recalibrated in Story 2.9 when the method changed to the point cloud: over
#: six demos (2,544 detonations) 95.4 % fall within the bound of 256. The
#: per-demo figures are in :data:`DEMO_AREA_COVERAGE` and the total in
#: :data:`CALIBRATION_TOTAL` -- both guarded, so that ``settings.toml``'s
#: table cannot go stale unnoticed.
CALIBRATED_SNAP_UNITS = 256

#: CS2's round time and the bomb timer in seconds. A round can continue for
#: the sum of these from the anchor, so a throw's t_s cannot exceed it.
ROUND_SECONDS = 115.0
BOMB_SECONDS = 40.0

#: Detonation area coverage **per demo**: name -> (named, detonations).
#:
#: Measured 2026-08-30 over all six demos with the settings cell 32 / weight 1
#: / tolerance 72 and the threshold :data:`CALIBRATED_SNAP_UNITS`. These are
#: the same figures ``settings.toml``'s threshold is calibrated on -- and that
#: is why they are **here** and not only in a comment in the settings file: a
#: calibration table without a regression guard goes stale at the first change
#: that moves an area on any demo.
#:
#: Two maps are not enough. Ancient and Nuke are the extremes (91.8 % and
#: 99.0 %), and it is Anubis and Inferno in between that would expose a change
#: which leaves the extremes alone but breaks everything else.
DEMO_AREA_COVERAGE: dict[str, tuple[int, int]] = {
    ANCIENT_DEM: (335, 373),
    NUKE_ZST: (451, 455),
    "Ancient_vs_kaljukostaja.dem": (367, 400),
    "Anubis_vs_ryhmarama.dem": (435, 465),
    "Nuke_vs_imuaijat.dem": (403, 407),
    "inferno_vs_ryhmarama.dem": (437, 444),
}

#: The detonations whose nearest **point cloud cell** was at most
#: :data:`CALIBRATED_SNAP_UNITS` away, on the Ancient test demo.
#:
#: **This figure is Story 2.9's measure.** The previous method -- the nearest
#: living player -- gave 170/373, that is, 46 %. The point cloud gives
#: 335/373, that is, 90 %, and the difference is not in precision but in what
#: is measured: smoke is thrown where nobody is. If this figure collapses, the
#: method is broken -- and a bare coverage percentage in the output would not
#: say so, because it would be computed from the same broken result.
#:
#: Derived from :data:`DEMO_AREA_COVERAGE` rather than written out separately:
#: two copies of the same figure would diverge.
ANCIENT_DETONATIONS_WITH_AREA = DEMO_AREA_COVERAGE[ANCIENT_DEM][0]

#: The same figure **under the previous method** (the nearest living player,
#: threshold 500), measured in Story 2.2. It is here as the point of
#: comparison: without it "90 % coverage" does not say whether things got
#: better or worse.
ANCIENT_DETONATIONS_WITH_THE_OLD_METHOD = 170

#: The total behind the calibration of the threshold 256: 2,428/2,544, that
#: is, 95.4 %. Derived from the per-demo figures, so that the table and the
#: total cannot diverge.
CALIBRATION_TOTAL = (
    sum(named for named, _ in DEMO_AREA_COVERAGE.values()),
    sum(total for _, total in DEMO_AREA_COVERAGE.values()),
)

#: Detonations after the round ended. In practice smokes that do not fade
#: until the next buy time.
#:
#: **They get their areas like every other.** In Story 2.2 they were left
#: without an area deliberately, because the method of the day would have read
#: the area off the players standing in the next round's spawn. The point
#: cloud does not depend on the moment, so the reason went away with the
#: method -- and that is part of why the coverage rose.
ANCIENT_DETONATIONS_AFTER_ROUND = 22

#: The demo's own detonation events and the canonical type each corresponds
#: to. ``inferno_startburn`` is deliberately absent from the list: the fire is
#: created as a **different** entity a few ticks after the trajectory ends, so
#: its position is not the same point but near it.
DETONATE_EVENTS: tuple[tuple[str, str], ...] = (
    ("smokegrenade_detonate", "smoke"),
    ("hegrenade_detonate", "he"),
    ("flashbang_detonate", "flashbang"),
)


@pytest.fixture(scope="module")
def ancient_events(ancient_tables) -> pl.DataFrame:
    """The Ancient demo's utility events table."""
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
    """Acceptance criterion: a throw and a detonation for every grenade."""
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = ancient_events.filter(pl.col("event_kind") == "grenade_detonate")
    assert throws.height == ANCIENT_GRENADES
    assert detonations.height == ANCIENT_GRENADES
    assert ancient_events.height == 2 * ANCIENT_GRENADES


@pytest.mark.demo
def test_ancient_grenade_types_are_plausible(ancient_events: pl.DataFrame) -> None:
    """Smokes, flashes, HEs and fire grenades in plausible numbers."""
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
    """Molotov is T's weapon and incendiary CT's -- the distinction is not
    taken from the side.

    The type is read from the thrower's bag and not from the side, so this is
    an independent check: if the distinction were broken, the distribution
    would go crosswise. A few exceptions are allowed (a dropped grenade gets
    picked up), but not many.
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
    # T cannot buy an incendiary at all.
    assert distribution.get(("T", "incendiary"), 0) == 0
    # For a CT a molotov is always picked up, so they are a clear minority.
    assert distribution.get(("CT", "molotov"), 0) < distribution[("CT", "incendiary")] / 4


@pytest.mark.demo
def test_ancient_data_claim_entity_ids_recycle_only_between_rounds(
    ancient_events: pl.DataFrame,
) -> None:
    """**A CLAIM ABOUT THE DATA, NOT A CONTRACT.** Why the fault did not show
    on the first demos.

    This test promises nothing about the ``EVENTS`` table. It describes one
    demo's content: on Ancient the game's own id repeats during the demo but
    not within a round, so the old key ``(round_raw, grenade_entity_id)``
    looked sufficient. The league demos showed otherwise -- see
    :func:`test_inferno_id_564_is_three_trajectories_on_one_round`.

    The test is kept because it documents exactly the limitation of the data
    that led to the wrong contract. If it ever fails, it means the Ancient
    demo has been replaced -- not that the contract is broken. The contract's
    guarantee is :func:`test_the_trajectory_id_is_unique_in_every_demo`.
    """
    counts = ancient_events.group_by(
        "round_raw", "grenade_entity_id", "event_kind"
    ).len()
    assert counts["len"].max() == 1

    # Across the whole demo the id repeats -- so it does not identify a
    # grenade.
    whole_demo = ancient_events.group_by("grenade_entity_id", "event_kind").len()
    assert whole_demo["len"].max() > 1


@pytest.mark.demo
def test_ancient_throw_area_is_always_observed(ancient_events: pl.DataFrame) -> None:
    """A throw's area comes from the thrower himself, not the nearest player.

    All 373 throws get an area, because the thrower is always present on his
    own tick. If this figure is not full, either the coordinates or the sides
    have gone astray.
    """
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throws["area"].null_count() == 0
    assert throws["area_source"].unique().to_list() == ["observed"]
    # An observation is not at a distance from anything: distance belongs to
    # an estimate alone.
    assert throws["snap_distance"].null_count() == throws.height


@pytest.mark.demo
def test_ancient_throw_areas_are_real_callouts(ancient_events: pl.DataFrame) -> None:
    """The thrower's own area is one of Ancient's own callouts, nothing else."""
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    areas = set(throws["area"].drop_nulls().unique().to_list())
    assert areas <= ANCIENT_PLACES, areas - ANCIENT_PLACES


@pytest.mark.demo
def test_ancient_snap_distances_are_within_the_configured_limit(
    ancient_events: pl.DataFrame,
) -> None:
    """The distance exists on exactly the rows that got an area.

    Without the distance a consumer could not tell a 15-unit hit from a
    240-unit estimate, and the threshold could not be calibrated without a new
    run.
    """
    limit = _parse_settings().area_snap_units
    assert limit == CALIBRATED_SNAP_UNITS

    named = ancient_events.filter(pl.col("area_source") == "point_cloud")
    assert not named.is_empty()
    assert named["snap_distance"].null_count() == 0
    assert named["snap_distance"].max() <= limit
    assert named["snap_distance"].min() > 0.0


@pytest.mark.demo
def test_ancient_detonations_beyond_the_threshold_keep_their_distance(
    ancient_events: pl.DataFrame,
) -> None:
    """I/O matrix: a distant detonation -> area null, ``snap_distance`` kept.

    **This is the row that proves the threshold is not pointless.** The
    nearest cell is always found in the point cloud, so without a threshold
    every detonation would get an area and the coverage would be 100 % -- and
    that would not be coverage but the absence of a measure. These rows are
    the evidence that some detonations happen far from everything any player
    has stood on.
    """
    limit = _parse_settings().area_snap_units
    detonations = ancient_events.filter(
        pl.col("event_kind") == "grenade_detonate"
    )
    far = detonations.filter(pl.col("area").is_null())
    assert not far.is_empty()
    assert far["snap_distance"].null_count() == 0
    assert far["snap_distance"].min() > limit
    # And the coordinates stay: the row is not dropped.
    for column in ("x", "y", "z"):
        assert far[column].null_count() == 0


@pytest.mark.demo
def test_ancient_point_cloud_is_written_and_covers_the_map(
    ancient_tables,
) -> None:
    """The point cloud is the source of the detonation areas, so it is kept.

    The number of areas matters more than the number of cells: the number of
    cells says only what the cell size is, but the number of areas says
    whether the cloud recognised the map. Ancient has 18 ``env_cs_place``
    areas and the cloud has to find all of them -- a single-digit figure would
    mean ``last_place_name`` mostly arrives empty and every detonation area
    would be a guess.
    """
    cloud = ancient_tables[0].callouts
    assert list(cloud.columns) == list(CALLOUTS_ADAPTER_COLUMNS)
    assert cloud.height > 5000
    assert set(cloud["area"].unique().to_list()) == ANCIENT_PLACES
    assert cloud["area"].null_count() == 0
    # A cell appears exactly once: two rows would mean the mode selection did
    # not do its job.
    key = cloud.select("cell_x", "cell_y", "cell_z")
    assert key.height == key.unique().height


@pytest.mark.demo
def test_ancient_detonation_area_coverage_beats_the_old_method(
    ancient_events: pl.DataFrame,
) -> None:
    """Acceptance criterion: the share without an area falls measurably.

    The nearest-living-player method gave this demo 170/373 areas (46 %). The
    point cloud gives :data:`ANCIENT_DETONATIONS_WITH_AREA`. The comparison
    figure is recorded here, because without it "90 % coverage" does not say
    whether things got better or worse.
    """
    detonations = ancient_events.filter(
        pl.col("event_kind") == "grenade_detonate"
    )
    named = detonations.height - detonations["area"].null_count()
    assert (named, detonations.height) == DEMO_AREA_COVERAGE[ANCIENT_DEM]
    assert named == ANCIENT_DETONATIONS_WITH_AREA
    assert named > ANCIENT_DETONATIONS_WITH_THE_OLD_METHOD
    # An exact share rather than "over 0.85": a loose bound would let through
    # a method that loses ten per cent of the coverage unnoticed.
    assert named / detonations.height == pytest.approx(0.898, abs=0.001)


def test_the_coverage_table_covers_every_demo() -> None:
    """The table has to cover the whole data set, not part of it.

    Without this a new demo would be added to :data:`ALL_DEMOS` but not here,
    and its coverage would go unguarded -- exactly the gap that left Anubis
    and Inferno recorded but untested. No ``demo`` marker: this compares two
    lists and needs no demos.
    """
    assert set(DEMO_AREA_COVERAGE) == set(ALL_DEMOS)


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(DEMO_AREA_COVERAGE))
def test_every_demo_keeps_its_measured_area_coverage(demo_name: str) -> None:
    """Every row of the calibration table is a regression guard.

    ``settings.toml``'s threshold of 256 is justified by a measurement over
    **six demos**, but without this test only two of them would be guarded. A
    change that moves an area on Anubis or Inferno would then pass -- and the
    settings file's table would be left lying.

    The figures are exact and not lower bounds: a **rise** in coverage is a
    change that has to be seen and recorded too.
    """
    events = parsed_demo(demo_name)[0].events
    detonations = events.filter(pl.col("event_kind") == "grenade_detonate")
    named = detonations.height - detonations["area"].null_count()
    assert (named, detonations.height) == DEMO_AREA_COVERAGE[demo_name]


@pytest.mark.demo
def test_the_calibration_total_matches_the_recorded_table() -> None:
    """The six demos' total is the figure the threshold 256 is justified by.

    ``settings.toml`` says 2,428/2,544, that is, 95.4 %. If any demo moves,
    the total moves -- and the setting's justification has to be corrected at
    the same time.
    """
    named = total = 0
    for demo_name in DEMO_AREA_COVERAGE:
        events = parsed_demo(demo_name)[0].events
        detonations = events.filter(pl.col("event_kind") == "grenade_detonate")
        named += detonations.height - detonations["area"].null_count()
        total += detonations.height
    assert (named, total) == CALIBRATION_TOTAL == (2428, 2544)
    assert named / total == pytest.approx(0.954, abs=0.001)


@pytest.mark.demo
def test_ancient_detonation_areas_are_real_callouts(
    ancient_events: pl.DataFrame,
) -> None:
    """The area is one of Ancient's own callouts or empty -- never invented."""
    assert _parse_settings().area_snap_units == CALIBRATED_SNAP_UNITS

    detonations = ancient_events.filter(pl.col("event_kind") == "grenade_detonate")
    areas = set(detonations["area"].drop_nulls().unique().to_list())
    assert areas <= ANCIENT_PLACES, areas - ANCIENT_PLACES
    assert detonations.height - detonations["area"].null_count() == (
        ANCIENT_DETONATIONS_WITH_AREA
    )
    received = detonations.filter(pl.col("area").is_not_null())
    assert received["area_source"].unique().to_list() == ["point_cloud"]


@pytest.mark.demo
def test_ancient_area_source_is_set_exactly_when_the_area_is(
    ancient_events: pl.DataFrame,
) -> None:
    """Contract: ``area_source`` is empty if and only if the area is."""
    conflicts = ancient_events.filter(
        pl.col("area").is_null() != pl.col("area_source").is_null()
    )
    assert conflicts.is_empty(), conflicts.head(3).to_dicts()


@pytest.mark.demo
def test_ancient_coordinates_are_kept_even_without_an_area(
    ancient_events: pl.DataFrame,
) -> None:
    """I/O matrix: a distant detonation gets ``area = null``, not a drop."""
    without_area = ancient_events.filter(pl.col("area").is_null())
    assert not without_area.is_empty()
    for column in ("x", "y", "z"):
        assert without_area[column].null_count() == 0


@pytest.mark.demo
def test_ancient_events_stay_inside_their_round(
    ancient_events: pl.DataFrame,
) -> None:
    """The throw happens inside the round; the detonation may fall outside it.

    A smoke thrown at the end of a round only burns out on the next one's
    side, and it still belongs to the round it was thrown in -- but the throw
    itself has to be within the round's boundaries, or ``t_s`` means nothing.
    """
    throws = ancient_events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throws["t_s"].min() >= 0.0
    # CS2's round time is 115 s, but a planted bomb extends the round by
    # another 40 seconds: a post-plant smoke at 130 seconds is normal, not an
    # error. The bound is therefore 115 + 40 and not 115.
    assert throws["t_s"].max() <= ROUND_SECONDS + BOMB_SECONDS


@pytest.mark.demo
@pytest.mark.parametrize(("event_name", "grenade_kind"), DETONATE_EVENTS)
def test_ancient_detonation_point_matches_the_games_own_event(
    ancient_events: pl.DataFrame, event_name: str, grenade_kind: str
) -> None:
    """The trajectory's last point is the detonation position -- checked
    independently.

    The demo has detonation events of its own with ``x, y, z``. They are not
    read during a run (three extra event reads without extra information), but
    they serve as the test's truth: if the segmentation cut the trajectory too
    early, the detonation position would be somewhere along the flight path.
    """
    from demoparser2 import DemoParser as _Demoparser2

    parser = _Demoparser2(str(require_demo(ANCIENT_DEM)))
    observed = pl.from_pandas(parser.parse_event(event_name))
    own_rows = ancient_events.filter(
        (pl.col("event_kind") == "grenade_detonate")
        & (pl.col("grenade_type") == grenade_kind)
    )
    # The comparison runs **from the table to the events**, not the other way
    # round: a dropped grenade (a throw outside the rounds, an unknown side)
    # is absent from the table entirely legitimately, and the test must not
    # require that the dropped ones always happen to be of some other type
    # than this one.
    assert not own_rows.is_empty()
    assert own_rows.height <= observed.height

    # Pairing by entity id; the same id appears several times, so it is enough
    # that one of its trajectories ends at the event's position. The allowed
    # difference is one game unit -- the measured difference is under 0.03, and
    # a point along the flight path would be hundreds of units away.
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
        assert targets, f"{event_name}: entity {entity} has no event"
        for point in points:
            distances = [math.dist(point, target) for target in targets]
            assert min(distances) < 1.0, (
                f"{event_name} entity {entity}: the trajectory's end is "
                f"{min(distances):.1f} units from the nearest detonation "
                "position"
            )


@pytest.mark.demo
def test_ancient_utility_diagnostics_are_clean(ancient_tables) -> None:
    """A dropped grenade is the exception, not the normal result."""
    diagnostics = ancient_tables[1]
    assert diagnostics.grenades_without_thrower == 0
    assert diagnostics.grenades_unknown_side == 0
    # One grenade leaves after the round has been decided -- that is a correct
    # observation and not a fault, but there is no t_s for it.
    assert diagnostics.grenades_outside_rounds == 1
    # On Ancient the ids are recycled during the demo but not within a round.
    # In the league demos they are recycled within a round too, and that is
    # why the table's key is grenade_no and not the game's own id.
    assert diagnostics.grenades_sharing_an_entity_id == 0
    # The class names and the fire-grenade distinction are up to date.
    assert diagnostics.grenades_unknown_type == 0
    assert diagnostics.grenades_fire_type_unresolved == 0
    # This is the only figure that is a fault outright: an endpoint tick with
    # no players would mean the area could not even be attempted.
    assert diagnostics.grenade_ticks_without_players == 0
    # A smoke often does not fade until the next buy time. The figure is an
    # observation and not a drop: the point cloud does not depend on the
    # moment, so a late detonation gets its area like every other.
    assert (
        diagnostics.grenades_detonating_after_round
        == ANCIENT_DETONATIONS_AFTER_ROUND
    )
    # The point cloud was built: the number of areas is the figure that says
    # whether it recognised the map. The number of cells would say only what
    # the cell size is.
    assert diagnostics.callout_cloud_rows_read > 1_000_000
    assert diagnostics.callout_cloud_empty_reason is None


#: The league demo where the recycling shows. The name is read from
#: :data:`LEAGUE_DEMOS` and not written out again: a copy of its own would go
#: stale silently, and ``require_demo`` would skip the test as though the demo
#: were missing.
INFERNO_DEMO = next(name for name, _ in LEAGUE_DEMOS if name.startswith("inferno"))

#: In ``inferno_vs_ryhmarama`` on round 11 the game's id 564 carries **three**
#: different trajectories. Measured from the archive's ``events.parquet``
#: 2026-08-29, and it is the whole reason Story 1.8 exists: the pair
#: ``(round_no, grenade_entity_id)`` does not identify a grenade.
#:
#: In the adapter's table ``round_no`` is always empty -- the numbering
#: belongs to ``stages.parse`` -- so the round is named here by the demo's own
#: counter. ``round_raw`` 12 is ``round_no`` 11: the knife round and the match
#: restart are not rounds played.
INFERNO_REUSED_ROUND_RAW = 12
INFERNO_REUSED_ENTITY = 564

#: Id 564's three trajectories **before this change**, read from the archive's
#: ``events.parquet``. The new column must not change any of these: the
#: segmentation stays as it was and only the id is new. The times are compared
#: with a tolerance -- the claim is "the same observation", not "the same
#: floating-point bits".
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

#: The event kinds allowed per ``grenade_no``. A grenade that does not
#: detonate produces only a throw, so two rows are allowed but not required --
#: and two rows are always exactly this pair, never two throws.
GRENADE_ROW_SHAPES: tuple[tuple[str, ...], ...] = (
    ("grenade_thrown",),
    ("grenade_thrown", "grenade_detonate"),
)


@lru_cache(maxsize=None)
def parsed_demo(demo_name: str):
    """A demo's tables and diagnostics, parsed **once per run**.

    The same pattern as the ``ancient_tables`` fixture, but parametrised by
    name: six 100-230 MB demos cannot be reparsed in every test.
    ``require_demo`` is inside the call, so that a missing demo skips the test
    cleanly and no skip is left in the cache.
    """
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    return tables, adapter.diagnostics


# --- The pawnless player from a real demo (Story 2.10) -------------------------


@pytest.mark.demo
def test_the_pawnless_demo_is_the_file_the_numbers_were_measured_from() -> None:
    """The size and the digest tell a wrong copy from a missing one.

    The figures below (15 rows, 22 rounds) are about **this file**. Measured
    from another copy they would prove nothing.
    """
    path = require_demo(PAWNLESS_DEMO)
    size, digest = PAWNLESS_DEMO_FILE
    assert path.stat().st_size == size, "the fault demo's size does not match"

    reader = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            reader.update(chunk)
    assert reader.hexdigest() == digest, "the fault demo's digest does not match"


@pytest.mark.demo
def test_the_demo_that_broke_the_guard_parses_and_counts_its_skipped_rows() -> None:
    """A regression guard for the fault the whole of Story 2.10 was written for.

    Before the fix this demo raised a ``ParseError`` at tick 119132 and
    produced no tables at all. Now it parses, and **the skipped rows are
    readable as a figure** rather than merely absent from the table.

    Three claims rather than one: parsing proves the fix, the round count
    proves nothing disappeared with it, and the row count proves the skip has
    not loosened and the counter has not stopped seeing either of its read
    paths.
    """
    tables, diagnostics = parsed_demo(PAWNLESS_DEMO)

    played = mark_played_rounds(tables.rounds).filter(pl.col("round_no").is_not_null())
    assert played["round_no"].n_unique() == PAWNLESS_DEMO_ROUNDS
    assert not tables.ticks.is_empty()

    assert diagnostics is not None
    assert diagnostics.sample_rows_without_pawn == PAWNLESS_DEMO_ROWS
    assert diagnostics.sample_points_without_pawn == PAWNLESS_DEMO_POINTS
    # The pawnless player threw nothing, so no throw's area was ever left
    # unread. A non-zero figure would mean the skip has started swallowing
    # throwers' own rows.
    assert diagnostics.grenade_throwers_without_row == 0


@pytest.mark.demo
def test_the_pawnless_round_keeps_its_place_in_the_sample() -> None:
    """A pawnless player shrinks the round's setup but does not drop it.

    Round 19 is the whole demo's smallest: four players out of five on the CT
    side at every sample point. It is still in the sample, and four is well
    above the bound at which this would be a broken demo rather than one
    player dropping out.
    """
    tables, _ = parsed_demo(PAWNLESS_DEMO)
    ticks = tables.ticks

    per_side = ticks.group_by("round_raw", "sample_kind", "sample_t_s", "side").len()
    assert per_side["len"].min() == 4
    # Every round that has rows is in with all of its sample points.
    assert ticks.filter(pl.col("round_raw") == 19).height > 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_a_healthy_demo_has_no_pawnless_rows(demo_name: str) -> None:
    """Zero is the normal result, and it has to be measured, not assumed.

    Without this claim a loosening of the skip -- firing on a missing alive
    state alone, for instance -- would look exactly like an intact rule in a
    healthy demo. The fault demo's own figure does not expose it: there the
    skip is *supposed* to fire.
    """
    _, diagnostics = parsed_demo(demo_name)
    assert diagnostics is not None
    assert diagnostics.sample_rows_without_pawn == 0
    assert diagnostics.sample_points_without_pawn == 0
    assert diagnostics.grenade_throwers_without_row == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_trajectory_id_is_unique_in_every_demo(demo_name: str) -> None:
    """Acceptance criterion: ``(grenade_no, event_kind)`` is unique.

    The claim is about **the whole table** and not a round, and it is run over
    all six demos. On the two old test demos the old key would have passed too
    -- which is exactly why the claim has to be run over the data in which the
    fault showed.

    Every claim survives **an empty table**: a demo without utility is a valid
    result, and a contract test must not demand content from the data. That
    utility was thrown in these six demos is a separate claim about the data
    at the end of this test.
    """
    events = parsed_demo(demo_name)[0].events

    assert events["grenade_no"].null_count() == 0
    keys = events.select("grenade_no", "event_kind")
    assert keys.height == keys.unique().height

    # The game's own id is kept -- it is the only tie back to the demo.
    assert events["grenade_entity_id"].null_count() == 0

    # The throw and the detonation share the number, and two rows cannot be
    # two throws. The claim is about shape and not about count, so it holds
    # for a grenade that never detonated too.
    shapes = (
        events.group_by("grenade_no")
        .agg(pl.col("event_kind").sort().cast(pl.Utf8).alias("kinds"))["kinds"]
        .to_list()
    )
    assert all(tuple(kinds) in GRENADE_ROW_SHAPES for kinds in shapes)

    # A claim about the data, not a contract: utility was thrown in these six
    # demos.
    assert not events.is_empty()


@pytest.mark.demo
def test_inferno_id_564_is_three_trajectories_on_one_round() -> None:
    """Acceptance criterion: id 564 splits into three -- times and types
    unchanged.

    This is the measured case the old contract did not survive. The new id has
    to separate the trajectories **without changing the observation**: the
    segmentation is untouched, so the times and the grenade types are the same
    as before the change.
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
        # A tolerance of a fraction of a tick: the claim is the same
        # observation, not the same floating-point bits. A change to the tick
        # rate or to rounding must not look like a change to the
        # segmentation.
        return [(name, pytest.approx(t_s, abs=0.02)) for name, t_s in pairs]

    # The observation is unchanged: the same three types at the same moments.
    assert observed(throws) == expected(INFERNO_564_THROWS)
    assert observed(detonations) == expected(INFERNO_564_DETONATIONS)

    # Three trajectories, three ids -- and the game's own id is still the same.
    assert throws["grenade_no"].n_unique() == 3
    assert subset["grenade_no"].n_unique() == 3
    assert subset["grenade_entity_id"].unique().to_list() == [INFERNO_REUSED_ENTITY]

    # The throw and the detonation share the number: it is their only tie.
    for _, pair in subset.group_by("grenade_no", maintain_order=True):
        assert sorted(pair["event_kind"].to_list()) == [
            "grenade_detonate",
            "grenade_thrown",
        ]


@pytest.mark.demo
def test_parsing_the_same_demo_twice_gives_identical_tables() -> None:
    """Acceptance criterion: the same demo twice -> identical tables.

    The stability of the id is a condition: if the numbers changed between
    runs, reparsing the archive would look like a change without a change.

    The claim is weak here -- a deterministic function on the same input --
    and its strong form lives in ``test_utility.py``, where the trajectories'
    **row order is shuffled** before the segmentation. That cannot be done
    with a demo, so this only makes sure no randomness (hash order,
    parallelism) is left anywhere in the pipeline. The first parse is shared
    with the other tests, so the cost is one extra read and not two.
    """
    first = parsed_demo(ANCIENT_DEM)[0]
    second = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)

    assert not first.events.is_empty()
    assert first.events.equals(second.events)
    # The point cloud is part of the same claim, because the detonation areas
    # are derived from it: if a cell's area could change between runs (a tie
    # in the mode, the grouping's order), events would be stable only by
    # accident.
    assert not first.callouts.is_empty()
    assert first.callouts.equals(second.callouts)


@pytest.mark.demo
def test_nuke_utility_is_read_too() -> None:
    """Another map, another set of names: the area logic must not be
    Ancient-specific."""
    tables = real_parser().parse_demo(require_demo(NUKE_ZST), SNAPSHOT_SECONDS)
    events = tables.events
    assert not events.is_empty()
    throws = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throws["area"].null_count() == 0
    assert throws["area_source"].unique().to_list() == ["observed"]
    detonations = events.filter(pl.col("event_kind") == "grenade_detonate")
    # Nuke's callouts are denser than Ancient's, so the area resolves more
    # often -- but never for all of them.
    received = detonations.height - detonations["area"].null_count()
    assert 0 < received < detonations.height


#: Nuke's floor boundary in game units, **read from the point cloud**
#: 2026-08-30 (``1-79f71e00...``): :data:`NUKE_LOWER_PLACES`'s cells lie
#: between -784 and -560 and the main level's cells start at -464. The
#: boundary is between them and has not been guessed from the map's geometry.
#:
#: The boundary itself belongs to the **lower floor** (``z <= boundary``), so
#: that every detonation is in exactly one of the two sets. With both
#: conditions strict, a detonation exactly on the boundary would fall outside
#: both claims.
NUKE_LOWER_FLOOR_Z = -560.0

#: Nuke's area names that are **only downstairs**. A fixed list on purpose: a
#: set derived from the demo would accept any name and would stop being a
#: check.
#:
#: ``Ramp``, ``Secret`` and ``Vents`` are **not** on the list even though they
#: are downstairs names in conversation: measured, their cells split across
#: two levels (``Ramp`` -624 .. -208), because they are connections between
#: the floors. Including them would make the test a claim about the map's
#: everyday language rather than its geometry.
NUKE_LOWER_PLACES: frozenset[str] = frozenset(
    {"BombsiteB", "Tunnels", "Decon", "Observation"}
)


@pytest.mark.demo
def test_nuke_upper_floor_smoke_never_gets_a_lower_floor_area() -> None:
    """Acceptance criterion: an upstairs detonation gets no downstairs area.

    **A distance measurement does not say this.** Nuke is 99 % inside the
    threshold at every weight option, so the median and the coverage would
    look just as good at the weight that puts an upstairs smoke in a
    downstairs area. The only thing that separates them is this claim -- and
    that is why it is a test and not an eyeball check.

    An upstairs detonation is recognised by its height
    (:data:`NUKE_LOWER_FLOOR_Z`), not by its area: the area is precisely what
    is in doubt.
    """
    events = parsed_demo(NUKE_ZST)[0].events
    detonations = events.filter(
        (pl.col("event_kind") == "grenade_detonate")
        & pl.col("area").is_not_null()
    )
    upper = detonations.filter(pl.col("z") > NUKE_LOWER_FLOOR_Z)
    lower = detonations.filter(pl.col("z") <= NUKE_LOWER_FLOOR_Z)
    # The boundary belongs to exactly one side: otherwise a detonation exactly
    # on it would be in neither set and neither claim would cover it.
    assert upper.height + lower.height == detonations.height
    assert not upper.is_empty(), "no upstairs detonations were found at all"
    wrong = upper.filter(pl.col("area").is_in(sorted(NUKE_LOWER_PLACES)))
    assert wrong.is_empty(), wrong.select("area", "z", "snap_distance").head(5).to_dicts()

    # And in the other direction: downstairs detonations **do** get downstairs
    # areas, so the test does not pass merely because no downstairs names
    # occur.
    assert not lower.filter(
        pl.col("area").is_in(sorted(NUKE_LOWER_PLACES))
    ).is_empty()


@pytest.mark.demo
def test_the_z_weight_is_what_keeps_the_floors_apart() -> None:
    """Without the weight an upstairs smoke **does** get a downstairs area --
    measured.

    The previous test is not enough on its own: it would pass even when the
    weight does nothing, if the map happened to be sparse enough. This one
    runs the same demo at weight 0 and shows that the error is real and that
    the setting prevents it. Measured 2026-08-30 on both Nuke demos: at weight
    0 there are 38 wrongly named (``Nuke_vs_imuaijat``) and 25
    (``1-79f71e00...``), and at weights 1, 2 and 3 none. Production's weight
    is 1, because that is enough -- and because every weight above it costs
    coverage (99.0 % -> 98.8 % -> 97.4 %).
    """
    # The port is built **through default_parser** and not by hand: an
    # argument list written by hand would fall behind the moment [parse] gains
    # a new setting, and this test would then be running a different
    # configuration from production.
    flat = parse_stage.default_parser(
        _parse_settings().model_copy(update={"callout_z_weight": 0.0})
    )
    events = flat.parse_demo(require_demo(NUKE_ZST), SNAPSHOT_SECONDS).events
    wrong = events.filter(
        (pl.col("event_kind") == "grenade_detonate")
        & (pl.col("z") > NUKE_LOWER_FLOOR_Z)
        & pl.col("area").is_in(sorted(NUKE_LOWER_PLACES))
    )
    assert not wrong.is_empty(), (
        "weight 0 produced no wrong-floor area at all -- the previous test "
        "then proves nothing about the weight"
    )


# --- The buy time in real demos (Story 1.9) ------------------------------------


#: ``inferno_vs_ryhmarama``, round 6, Ryhma Rama on the T side. The product
#: owner watched this round in the demo and read figures off it that did not
#: match the tool's -- that was the observation that found the whole fault.
#:
#: At the end of freezetime the equipment was 11,550 and the money 6,600; two
#: seconds later 15,350 and 2,400. Three of the five players bought only then.
INFERNO_ROUND_6 = {
    "equip_buy_end": 15_350,
    "money_buy_end": 2_400,
    "armed": 5,
    # The balances per player as the product owner read them (the calibration
    # document): 150, 0, 500, 1,750, 0. The total is the same 2,400 -- and
    # that is exactly the problem: the total does not show that only one
    # player reaches $4,000 with the loss bonus.
    "money_players": [1_750, 500, 150, 0, 0],
}

#: The same from round 10, which the product owner called a half-buy. Both
#: have five armed players, so the kit does not tell them apart -- only the
#: distribution does.
INFERNO_ROUND_10 = {
    "equip_buy_end": 11_900,
    "money_buy_end": 7_900,
    "armed": 5,
    "money_players": [2_150, 2_050, 2_000, 900, 800],
}

#: The same players and the same weapons as the product owner read them.
#: Three of these were bought only after freezetime; read at the anchor, all
#: three have a Glock.
INFERNO_ROUND_6_WEAPONS = {
    "petemonni": "P250",
    "Toumee": "Tec-9",
    "Manetsu": "AK-47",
}


@pytest.mark.demo
def test_inferno_round_six_matches_the_human_reading() -> None:
    """The round that found the fault now produces the figures the product
    owner read off the demo.

    Three figures together, because they broke together: the equipment value
    was underestimated, the money left in pocket overestimated, and the armed
    count gave 2 when the truth was 5. None of them would have exposed the
    fault on its own -- a count of 2 looked like a plausible eco.
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
    # The measurement point is after the anchor but before the end of the
    # window: the round's first death (18.1 s) cut the window short.
    assert observed["buy_end_tick"] > observed["freeze_end_tick"]


@pytest.mark.demo
def test_inferno_rounds_six_and_ten_differ_only_in_the_distribution() -> None:
    """Two rounds, the same kit, a different verdict -- the difference is in
    the distribution.

    Both have five armed players, so the half-buy's condition A does not tell
    them apart at all. The product owner called round 6 a force and round 10 a
    half-buy, and justified that by who can buy on the next round. This test
    pins the **observation** the rule is read from; the rule's own test is in
    ``test_calibration.py`` and needs no demo.
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
def test_inferno_round_six_players_hold_the_weapons_the_product_owner_saw() -> None:
    """The weapons per player, not just the team total.

    The total of 15,350 would also match if the measurement point were right
    but the inventory were read at the wrong tick -- and it is the inventory
    that decides the armed count. The product owner named three weapons that
    were bought only after freezetime; read at the anchor, all three players
    still have the free Glock.
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
            f"{player}: the product owner saw {weapon!r}, the demo gave "
            f"{inventories[player]}"
        )


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_no_purchase_is_lost_behind_the_death_cut(demo_name: str) -> None:
    """The death cut costs no purchase -- established, not assumed.

    This is the buy window's whole trade-off as one figure. The window is 20
    s, but a death cuts it on about half the rounds; if anyone bought after
    the cut, the measurement would lose a purchase. Measured over all six
    demos, that never happens.

    The number of cuts is **not** claimed to be zero: it is the normal path
    and not a fault. The claim is only about its price.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    assert adapter.diagnostics is not None
    cuts = adapter.diagnostics.buy_window_cuts
    assert sum(missed for _, missed in cuts) == 0
    assert adapter.diagnostics.buy_window_ticks_without_players == 0
    # There are cuts, that is, the window really is bounded by a death.
    # Without this the row would pass even if deaths were not read at all.
    assert cuts, "no window was cut at all -- deaths are apparently not read"


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_measurement_point_stays_inside_its_round(demo_name: str) -> None:
    """The measurement point is after the anchor, inside the window and the
    same for both teams.

    Three invariants, each of which would break differently: a measurement
    point before the anchor would read inside freezetime, one after the end of
    the window would no longer be buy time, and a per-team point would make
    the two rows' totals incomparable.

    The fourth bound -- the end of the round -- has a test of its own,
    :func:`test_the_measurement_never_reaches_the_next_round`, because it
    requires a comparison with the **next** round and cannot be read off a
    single row.
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
    """A window of 0 reproduces the pre-Story-1.9 measurement exactly.

    The equipment value is now read from the prop
    ``m_unCurrentEquipmentValue`` and not from
    ``m_unFreezetimeEndEquipmentValue`` -- the latter does not update after
    freezetime, so with it the whole fix would stay invisible. At the anchor
    these two are the same number, and that is exactly what makes the switch
    safe: without it the prop change could have moved every figure silently.

    The comparison values are figures from the calibration document and the
    fault report, measured with the old prop.
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
    # Ancient's calibration figures under the old measurement, from the
    # document (kalibrointi-kierrostyypit.md, the truth table; $/player x 5).
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
    """``m_unRoundStartEquipmentValue`` does not change during the buy window.

    The amount bought is ``equip_buy_end - equip_round_start``, and that is
    the whole foundation of rule S3. The subtrahend is now read at a later
    tick than before, and **this very story showed** that another
    similar-looking field (``m_unFreezetimeEndEquipmentValue``) does not
    behave as expected when read at a later tick. The same assumption must not
    be left resting on a single measurement for the other field.

    The comparison is made per player at both ticks: if the field ever starts
    moving during a round, the amount bought would drift silently and S3 would
    turn saves into buys.
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
    assert moved, "not one measurement point moved away from the anchor"

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
                f"{demo_name}: player {row['steamid']}'s "
                "round_start_equip_value changed between the anchor and the "
                f"measurement point ({before} -> {row['equip_round_start']}). "
                "The amount bought is no longer reliable."
            )
    assert compared >= 5 * len(moved), (compared, len(moved))


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_measurement_never_reaches_the_next_round(demo_name: str) -> None:
    """The measurement point does not reach the next round's anchor.

    Bounding to the end of the round is the third bound in the measurement
    point's formula, and until now it had never been established with a real
    demo -- in the fake a round is 39 s, so a 20-second window always fits
    inside. In a real demo a round can be decided in under 20 seconds, and an
    unbounded window would then read the next round's economy values onto this
    round's row.
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
            f"{demo_name}: round {per_round['round_no'][index]}'s "
            f"measurement point {measured[index]} reaches the next round's "
            f"anchor {anchors[index + 1]}."
        )


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_buy_window_reports_no_broken_measurement(demo_name: str) -> None:
    """The measurement point's fault counters are zero across the whole data.

    These four are **faults and not observations**: an empty buy tick, players
    lost since the anchor, a team row left entirely empty, and a cut that
    could not be checked. Not one of them fires in the six demos, and that is
    exactly why they have to be pinned: a non-zero value is a sign that some
    assumption behind the measurement point is broken.

    Refunds and the stale equipment value they leave behind are **not** here:
    they are the game's behaviour and not faults of ours, and they have tests
    of their own.
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
    """Refunds do occur, and the stale value they leave behind is rare.

    Both figures are the game's behaviour and not faults, but they have to be
    pinned from two directions. Zero refunds would mean the detection has
    stopped working -- a decrease in ``cash_spent`` is their only unambiguous
    sign. A large amount of stale value, on the other hand, would mean the
    equipment value cannot be trusted; measured, it is one player row across
    the whole data set.
    """
    refunds = 0
    stale = 0
    for demo_name in ALL_DEMOS:
        adapter = real_parser()
        adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
        assert adapter.diagnostics is not None
        refunds += adapter.diagnostics.buy_window_refunds
        stale += adapter.diagnostics.buy_window_stale_equipment

    assert refunds > 0, "no refunds were observed at all -- the detection is broken"
    # 8 refunds and 1 stale value, measured 2026-08-29. The bounds are loose,
    # because the figures are a property of the data and not a contract; a
    # strict equality would fail as soon as a demo is added to the data.
    assert refunds <= 20, refunds
    assert stale <= 3, stale


# --- The lineups table from real demos (Story 2.6) ------------------------------

#: The league demos' clan names, measured 2026-08-30 straight from the demos.
#:
#: These are not an output of our code: they are the game's own
#: ``team_clan_name`` field on every player's anchor row. The test reads them
#: again, because it is these very strings that end up in the report's
#: heading -- a rename in demoparser2 would otherwise show only in the
#: finished report.
LEAGUE_CLANS: dict[str, tuple[str, str]] = {
    "Ancient_vs_kaljukostaja.dem": ("KALJUKOSTAJA", "MatureMayhem"),
    "Anubis_vs_ryhmarama.dem": ("MatureMayhem", "Ryhma Rama"),
    "inferno_vs_ryhmarama.dem": ("MatureMayhem", "Ryhma Rama"),
    "Nuke_vs_imuaijat.dem": ("MatureMayhem", "NadedNConfused"),
}


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_CLANS))
def test_real_demo_gives_ten_players_two_clans_five_each(demo_name: str) -> None:
    """Ten rows, two clans, five players in each.

    A substitute is the exception: if a team substituted a player mid-map, the
    row count is larger. In the test demos substitutions happen **between**
    maps and not inside them, so every one of them gives exactly ten.
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
    """One clan and one name per SteamID -- across the half-time switch too.

    This is the measurement that made the clan be read per player rather than
    through the side. Read through the side, ``team_num=2`` is one team in the
    first half and the other in the second.

    **The claim is about the raw observations, not the finished table.** The
    table is collapsed: ``_most_observed`` guarantees one row and one value
    per player no matter how many clans were observed, so "one value per
    player" read from the table would be a claim about the code's structure
    and not about the demo. The only place the difference shows is the
    adapter's own counter -- and that is why it is read here.
    """
    adapter = real_parser()
    adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.lineup_clan_conflicts == 0
    assert adapter.diagnostics.lineup_name_conflicts == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_CLANS))
def test_the_lineup_key_matches_the_players_in_the_table(demo_name: str) -> None:
    """The id is a digest of the table's own SteamIDs and of nothing else.

    If these diverged, ``aggregate`` would join the roster to a team that
    ``lineup_key`` does not mean.
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
    """In the league demos every player has a name and a clan.

    A different claim from consistency: this one says the observation was made
    at all. Nulls are a permitted result under the contract, but there are
    none in this data -- and if there ever are, it shows in the report as a
    SteamID.
    """
    tables = real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    assert tables.lineups["clan_name"].null_count() == 0
    assert tables.lineups["player_name"].null_count() == 0


@pytest.mark.demo
def test_the_ticks_table_agrees_with_the_lineups_table() -> None:
    """The same lineup in both tables; the join must not go crosswise."""
    tables = real_parser().parse_demo(require_demo(ANCIENT_DEM), SNAPSHOT_SECONDS)

    from_ticks = set(
        tables.ticks.select("lineup_key", "player_id").unique().iter_rows()
    )
    from_lineups = set(
        tables.lineups.select("lineup_key", "player_id").iter_rows()
    )
    # The lineups table is the map's truth: a player who did not make it to a
    # single sample point may be missing from the sample point table, but
    # there must not be a single extra one in it.
    assert from_ticks <= from_lineups


# --- Deaths from a real demo (Story 2.7) ---------------------------------------

#: Attackerless deaths per demo, measured 2026-08-30 **from the adapter's
#: output** (before ``stages.parse`` drops the unnumbered rounds). Falling and
#: the bomb are genuine cases, but their number is small and known: if it
#: jumps, something else is broken.
#:
#: The figures are per demo and not a total, because a total would stay the
#: same even if two demos swapped their figures.
LEAGUE_DEATHS_WITHOUT_ATTACKER: dict[str, int] = {
    "Ancient_vs_kaljukostaja.dem": 1,
    "Anubis_vs_ryhmarama.dem": 0,
    "inferno_vs_ryhmarama.dem": 0,
    "Nuke_vs_imuaijat.dem": 5,
}


@lru_cache(maxsize=None)
def _league_deaths(demo_name: str) -> tuple[pl.DataFrame, object]:
    """One league demo's deaths table and diagnostics, parsed once."""
    adapter = real_parser()
    tables = adapter.parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    return tables.deaths, adapter.diagnostics


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_real_demo_deaths_match_the_port_contract(demo_name: str) -> None:
    """The columns and types come from a real demo, not only from the fake."""
    deaths, _ = _league_deaths(demo_name)

    assert tuple(deaths.columns) == DEATHS_ADAPTER_COLUMNS
    for name in DEATHS_ADAPTER_COLUMNS:
        assert deaths.schema[name] == DEATHS[name], name
    assert not deaths.is_empty()
    # The numbering belongs to stages.parse; the adapter leaves the column
    # empty.
    assert deaths["round_no"].null_count() == deaths.height


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_every_victim_has_an_area_in_a_real_demo(demo_name: str) -> None:
    """The victim's area is the whole story's claim, and it must be **read
    from the demo**.

    The ``DEATH_COLUMNS`` guard checks that the column exists, not its
    content. If ``last_place_name`` came back as an empty string, the table
    would be schema-valid and every row would have no area -- and no fake test
    would notice anything, because the fake produces the areas itself.

    Measured 2026-08-30: 0 missing victim areas out of 591 deaths written.
    """
    deaths, _ = _league_deaths(demo_name)
    assert deaths["victim_area"].null_count() == 0


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(LEAGUE_DEATHS_WITHOUT_ATTACKER))
def test_the_attacker_area_is_missing_only_when_the_attacker_is(
    demo_name: str,
) -> None:
    """The area does not disappear from the attacker -- the attacker does.

    Two claims together: there are exactly as many attackerless rows as were
    measured, and every missing attacker area is **on those rows**. The latter
    is what separates an honest absence from a broken area observation.
    """
    deaths, _ = _league_deaths(demo_name)

    without_attacker = deaths.filter(pl.col("attacker_id").is_null())
    assert without_attacker.height == LEAGUE_DEATHS_WITHOUT_ATTACKER[demo_name]

    # Attacker known but area empty: zero in the measured data.
    unnamed = deaths.filter(
        pl.col("attacker_id").is_not_null() & pl.col("attacker_area").is_null()
    )
    assert unnamed.is_empty(), unnamed.head(3).to_dicts()

    # An attackerless row is attackerless throughout, in a real demo too.
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
    """Side inference must not lose deaths from a real match.

    The figure is the adapter's own counter and not the finished table: a
    dropped row is not in the table, so its absence cannot be read there. Zero
    is the expected value, and a non-zero one would mean the ``m_iTeamNum``
    codes or the lineup identification have changed.
    """
    _, diagnostics = _league_deaths(demo_name)

    assert diagnostics is not None
    assert diagnostics.deaths_without_victim_side == 0
    assert diagnostics.deaths_without_victim == 0
    assert diagnostics.deaths_attacker_without_side == 0
    assert diagnostics.deaths_without_tick == 0


@pytest.mark.demo
def test_ancient_death_areas_are_real_callouts() -> None:
    """Both areas are Ancient's own callouts, not invented ones.

    The same guard as on utility's throw areas. Without it
    ``user_last_place_name`` could come back from an entirely different field
    -- as the weapon's name, say -- and the table would still be valid.
    """
    deaths, _ = _league_deaths(ANCIENT_DEM)

    for column in ("victim_area", "attacker_area"):
        areas = set(deaths[column].drop_nulls().unique().to_list())
        assert areas <= ANCIENT_PLACES, (column, areas - ANCIENT_PLACES)
        assert areas


@pytest.mark.demo
def test_ancient_deaths_carry_coordinates_for_every_actor_present() -> None:
    """The coordinates are kept whenever the actor is -- whatever the area."""
    deaths, _ = _league_deaths(ANCIENT_DEM)

    for axis in ("victim_x", "victim_y", "victim_z"):
        assert deaths[axis].null_count() == 0
    with_attacker = deaths.filter(pl.col("attacker_id").is_not_null())
    for axis in ("attacker_x", "attacker_y", "attacker_z"):
        assert with_attacker[axis].null_count() == 0


@pytest.mark.demo
def test_ancient_deaths_stay_inside_their_round() -> None:
    """A death belongs to the round within whose boundaries it falls.

    ``t_s`` is the time from the anchor, so a negative value would mean a
    death before the end of freezetime -- that is, the wrong round.
    """
    deaths, _ = _league_deaths(ANCIENT_DEM)
    assert deaths["t_s"].null_count() == 0
    assert deaths["t_s"].min() >= 0.0


@pytest.mark.demo
def test_ancient_victim_side_agrees_with_the_ticks_table() -> None:
    """A death's side is the same as the sample point table's on that round.

    This is the cross-check the fake cannot make: it builds both tables from
    the same map, so they cannot disagree. In a real demo they are read from
    different sources -- the ``player_death`` event and ``parse_ticks`` -- and
    that is exactly why they could differ.
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
    assert checked > 100, f"only {checked} rows available for comparison"


@pytest.mark.demo
def test_a_knife_round_really_does_produce_death_rows() -> None:
    """Dropping the knife round is not theory: the adapter produces its rows.

    If the adapter filtered them itself, ``stages.parse``'s join would do
    nothing and the claim "the same mechanism as in the other tables" would
    mean nothing. The knife round is a league demo's first round boundary.
    """
    deaths, _ = _league_deaths(ANCIENT_DEM)
    first_round = deaths["round_raw"].min()

    assert first_round == 1
    assert deaths.filter(pl.col("round_raw") == 1).height > 0


# --- The armour count from real demos (Story 2.8) -------------------------------

#: The yardstick of 2026-08-30: MatureMayhem's armour and armed counts on the
#: rounds the product owner's hand-made analysis talks about.
#:
#: ``(demo, round, side) -> (armoured, armed)``. The figures were measured
#: **before the implementation** from the archive's rounds table at the
#: ``buy_end_tick`` column, that is, at the same moment the economy figures
#: are already read at -- not at a guessed tick.
#:
#: **The fixture covers the claim completely.** The documentation says in
#: three places "four demos, all eight pistol rounds", so all eight are here
#: -- two per demo (rounds 1 and 13). Without them the claim would rest on a
#: measurement nothing runs again.
#:
#: Two rows are direct hits on the analysis: about Nuke's T pistol round the
#: product owner wrote *"5 kevlars"* (measured 5/5) and about Ancient's CT
#: half *"Kits and duals hidden in the back box (no kevs)"* (measured 1/5).
#: Neither can be read from the armed count, which is 0 on all eight pistol
#: rounds.
#:
#: The last three rows are an eco and a force: there the counts are close to
#: each other, and they are included so that the test does not pass an
#: implementation that always produces a difference.
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

#: The pistol rounds in MR12. As a list, so that the claim "all eight" can be
#: counted from the fixture rather than written by hand.
PISTOL_ROUNDS: tuple[int, ...] = (1, 13)

#: A measured **counter-example** to the claim "on a pistol round the armed
#: count is always 0". The opponent (Ryhma Rama) on Anubis round 13: 3
#: armoured, 1 armed. $800 does not buy both kevlar and an upgraded weapon,
#: but **a picked-up weapon** is enough to arm a player -- the figure is
#: therefore a consequence of the money and not a rule, and the documentation
#: says "in practice" and not "always".
ARMED_ON_A_PISTOL_ROUND = ("Anubis_vs_ryhmarama.dem", 13, "CT", (3, 1))

#: The team whose rows the yardstick is about. The rows are identified by the
#: clan name and not by the lineup id: the id is a hash of the set of players
#: and would change with a substitute, and the test would then fail for the
#: wrong reason.
ARMOR_TRUTH_TEAM = "MatureMayhem"


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name", sorted({demo for demo, _, _ in ARMOR_TRUTH})
)
def test_the_armor_counter_matches_the_measured_truth(demo_name: str) -> None:
    """The measured figures from the demo, not from memory -- and both counts
    side by side.

    The four pistol rounds are included because on them the counts **differ**
    (there is armour, there are no weapons), and the other three because on
    them they are nearly the same. Difference alone or sameness alone would
    pass a wrong implementation too: the first would be passed by a constant,
    the second by a copied column.
    """
    tables = real_parser().parse_demo(require_demo(demo_name), SNAPSHOT_SECONDS)
    ours = {
        row["lineup_key"]
        for row in tables.lineups.iter_rows(named=True)
        if row["clan_name"] == ARMOR_TRUTH_TEAM
    }
    assert ours, f"{ARMOR_TRUTH_TEAM} is not in the demo's lineups table"

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
    """The fixture covers the claim "four demos, all eight pistol rounds".

    No demos needed: this reads the fixture and not the data. Without it the
    documentation's figure and the regression test's coverage could diverge --
    and that is exactly what had happened when the fixture pinned two demos
    and four rounds.
    """
    pistols = [key for key in ARMOR_TRUTH if key[1] in PISTOL_ROUNDS]
    assert len({demo for demo, _, _ in pistols}) == 4
    assert len(pistols) == 8
    # And the claim "armed 0 on all eight" is in the fixture.
    assert all(ARMOR_TRUTH[key][1] == 0 for key in pistols)


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", ALL_DEMOS)
def test_the_armored_count_stays_within_its_divisor(demo_name: str) -> None:
    """``0 <= players_armored_buy_end <= players_buy_end`` on every row.

    And in addition: the armed are a **subset** of the armoured, because the
    armed condition includes armour. A row with more armed players would mean
    the counts are reading a different set of players or a different tick.

    **No difference between the columns is required.** A demo in which
    everyone who bought armour also bought a weapon legitimately produces
    identical columns -- a difference claim would fail on that real data. That
    the counts are different observations is established by
    :data:`ARMOR_TRUTH`'s pistol rounds and by synthetic tests where the setup
    is chosen rather than incidental.
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

    # The rule really does discriminate: a single value across the whole table
    # would mean it does not bite on the data at all.
    assert observed[ARMORED_COLUMN].n_unique() > 1


@pytest.mark.demo
def test_a_pistol_round_can_have_an_armed_player_after_all() -> None:
    """A measured counter-example: "always 0" would be the wrong rule.

    $800 does not buy both kevlar (650) and an upgraded weapon, so on a pistol
    round the armed count is **typically** 0 -- but a picked-up weapon is
    enough to arm a player. Without this test the documentation's careful
    wording would look pointless and somebody would put the word "always"
    back.
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


# --- The match table: the map name from a real demo (Story 2.11) --------------


#: The map's name in the demo's header, measured 2026-08-31
#: (``demoparser2.DemoParser.parse_header()``, compressed ones through
#: ``readable_demo``). The table is a **regression test and not a note**: the
#: same pattern as :data:`ARMED_TRUTH` and :data:`DEMO_AREA_COVERAGE`.
#:
#: Two claims at once. First: a name is found on every demo, that is, the
#: header is not an optional field in this data. Second: the name is
#: **exactly** the map pool's spelling (``de_ancient``, not ``Ancient`` and
#: not ``de_ancient_v2``), so ``aggregate`` does not need to normalise the
#: branch's name -- and that claim is the whole story's foundation. If some
#: demo gave a spelling outside the pool, the map would split into two
#: branches without anything being broken.
#:
#: ``PAWNLESS_DEMO`` is included even though it is not in :data:`ALL_DEMOS`:
#: it is an archive demo, its header was measured along with the others, and
#: its absence from this table would mean nothing except that the claim covers
#: less.
DEMO_HEADER_MAP_NAMES: dict[str, str] = {
    ANCIENT_DEM: "de_ancient",
    NUKE_ZST: "de_nuke",
    "Ancient_vs_kaljukostaja.dem": "de_ancient",
    "Anubis_vs_ryhmarama.dem": "de_anubis",
    "Nuke_vs_imuaijat.dem": "de_nuke",
    "inferno_vs_ryhmarama.dem": "de_inferno",
    PAWNLESS_DEMO: "de_anubis",
}


def test_the_map_name_table_covers_every_demo() -> None:
    """The table has to cover the whole data set, not part of it.

    The same guard as :func:`test_the_coverage_table_covers_every_demo`:
    without it a new demo would be added to :data:`ALL_DEMOS` but not here,
    and its header would go unmeasured. No ``demo`` marker: this compares two
    lists and needs no demos.
    """
    assert set(DEMO_HEADER_MAP_NAMES) == set(ALL_DEMOS) | {PAWNLESS_DEMO}


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(DEMO_HEADER_MAP_NAMES))
def test_every_demo_header_names_its_map_in_pool_spelling(demo_name: str) -> None:
    """Every demo's header names its map in the pool's spelling.

    The claim is about the data and not about the game: it does not require
    every CS2 demo to have a ``map_name``, only that every demo in the data
    the product is judged against has one. That difference is exactly why the
    map name is read as an observation rather than inferred from the file
    name.
    """
    tables, _ = parsed_demo(demo_name)
    match = tables.match

    assert tuple(match.columns) == MATCH_ADAPTER_COLUMNS
    assert match.schema["map_name"] == MATCH["map_name"]
    assert match.height == 1, "the match table has one row per demo"
    assert match["map_name"].to_list() == [DEMO_HEADER_MAP_NAMES[demo_name]]


@pytest.mark.demo
@pytest.mark.parametrize("demo_name", sorted(DEMO_HEADER_MAP_NAMES))
def test_read_map_name_agrees_with_the_full_parse_on_real_demos(
    demo_name: str,
) -> None:
    """The port's new operation (Story 3.6) sees the same map as the parse.

    The same requirement over real data as in the logic tests over the fake:
    if the import and the parse could see a different name for a demo's map,
    the import's cross-check would be about a different observation from the
    one that ends up in the match table. The table
    :data:`DEMO_HEADER_MAP_NAMES` is the shared oracle for both, so neither
    can drift on its own.

    The test is **read-only**: it writes nothing into the archive and imports
    no demo. A compressed demo is decompressed into the machine's temp
    directory and deleted.
    """
    observed = real_parser().read_map_name(require_demo(demo_name))

    assert observed == DEMO_HEADER_MAP_NAMES[demo_name]


@pytest.mark.demo
def test_the_faceit_identifier_carries_no_map_name(ancient_tables) -> None:
    """The same demo, two sources: the id does not know the map, the header does.

    This is the whole story's reason as a single claim. The demo's id is
    ``1-a52ebff2-...-1-1``, from which the map pool recognises nothing -- and
    the header says ``de_ancient``. Without the header this demo is left as a
    map branch of its own under its id's name and does not join up with
    another demo of the same map.
    """
    from pappascout.domain.aggregate import map_name_for
    from test_aggregate import MAP_POOL

    observed = ancient_tables[0].match["map_name"][0]
    demo_id = ANCIENT_DEM.removesuffix(".dem")

    assert map_name_for(demo_id, MAP_POOL) == (demo_id, "unknown")
    assert map_name_for(demo_id, MAP_POOL, observed) == ("de_ancient", "demo_header")
