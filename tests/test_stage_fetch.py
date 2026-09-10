"""``stages.fetch`` -- the stage's tests (Story 3.4).

**No network.** The stage sees only the
:class:`~pappascout.adapters.protocols.DemoSource` port, and behind it here is
:class:`FakeDemoSource`, which builds the bytes by hand.

The fake **checks the id it is given**, and that is not a detail but its whole
value as a guard: a source returning the same bytes whatever it was asked for
would pass every test even when the stage asks for the wrong demo -- and the
wrong map being stored under the right name is exactly the fault the
``instances`` structure removes. An unknown id therefore raises
``DemoUnavailable``.

The rows of the I/O matrix are one test each in this file, and the name
identifies the row.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import has_temp_leftovers

from pappascout.adapters.decompress import ZSTD_MAGIC
from pappascout.adapters.protocols import DemoSource, DemoStream
from pappascout.archive.paths import DEMOS_ROOT_ENV_VAR, ArchivePaths
from pappascout.errors import ApiError, DemoUnavailable, PappascoutError
from pappascout.stages import fetch as fetch_stage
from pappascout.stages import select as select_stage
from pappascout.stages.fetch import MIN_PLAUSIBLE_DEMO_BYTES

#: A well-formed id: ``{match_id}-{map_index}``, with match_id ``1-<uuid>``.
MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"
UNIT = f"{MATCH}-0"
OTHER = f"{MATCH}-1"

def demo_bytes(marker: bytes = b"PAPPASCOUT", *, size: int | None = None) -> bytes:
    """A plausible-looking compressed demo: zstd magic bytes and enough size.

    **Not decoration but a requirement on the test material.** The stage now
    rejects content that does not begin with the zstd magic bytes or is too
    small to be a CS2 demo -- precisely so that an HTML error page or an empty
    response is not stored as a demo and then skipped by idempotence for ever.
    The test material therefore has to pass the same gate as a real demo,
    otherwise the tests would measure the rejection rather than the download.
    """
    target = MIN_PLAUSIBLE_DEMO_BYTES + 4096 if size is None else size
    body = (marker + bytes(range(256))) * (target // (len(marker) + 256) + 1)
    return (ZSTD_MAGIC + body)[:target]


#: A valid demo, which most of the tests use.
DEMO_BYTES = demo_bytes()

#: Content that is **not** a demo: an HTML error page with a 200 status.
HTML_ERROR_PAGE = b"<!doctype html><html><body>403 Forbidden</body></html>" * (
    MIN_PLAUSIBLE_DEMO_BYTES // 50
)


# -- Fixtures ----------------------------------------------------------------


@dataclass
class FakeDemo:
    """One demo in the source.

    Attributes:
        data: The bytes the stream gives out.
        announce: The ``content_length`` the source **claims**. ``None`` = the
            source does not state a length at all. The difference between the
            claim and the bytes is a field of its own, because a short download
            is exactly the state in which they differ.
        break_after: Break the stream after this many chunks (``ApiError``), or
            ``None``.
        chunk: The chunk size in bytes.
    """

    data: bytes
    announce: int | None = -1
    break_after: int | None = None
    chunk: int = 64

    def content_length(self) -> int | None:
        return len(self.data) if self.announce == -1 else self.announce


@dataclass
class FakeDemoSource:
    """The port's fake: from an id to bytes, without a network.

    ``asked`` is what is expected of the guard: a test can establish that the
    download was not even started (the disk gate, idempotence).
    """

    demos: dict[str, FakeDemo | Exception] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    closed: int = 0
    reads: dict[str, int] = field(default_factory=dict)

    def get_demo(self, map_demo_id: str) -> DemoStream:
        self.asked.append(map_demo_id)
        entry = self.demos.get(map_demo_id)
        if entry is None:
            raise DemoUnavailable(
                f"The source has no demo {map_demo_id}. The id matches no "
                "match at all."
            )
        if isinstance(entry, Exception):
            raise entry
        return DemoStream(
            chunks=self._chunks(map_demo_id, entry),
            content_length=entry.content_length(),
            on_close=self._close,
        )

    def _chunks(self, map_demo_id: str, entry: FakeDemo):
        self.reads[map_demo_id] = self.reads.get(map_demo_id, 0) + 1
        sent = 0
        for start in range(0, len(entry.data), entry.chunk):
            if entry.break_after is not None and sent >= entry.break_after:
                raise ApiError(
                    f"The download of demo {map_demo_id} broke off midway "
                    "(ChunkedEncodingError)."
                )
            yield entry.data[start : start + entry.chunk]
            sent += 1

    def _close(self) -> None:
        self.closed += 1


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


@pytest.fixture
def local_archive(tmp_path: Path) -> ArchivePaths:
    """An archive whose demos go **outside the archive** (2026-09-05)."""
    return ArchivePaths(root=tmp_path / "arkisto", demos_root=tmp_path / "demot")


@pytest.fixture
def source() -> FakeDemoSource:
    return FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES)})


def run(archive: ArchivePaths, source: DemoSource, unit: str = UNIT, **kwargs: Any):
    kwargs.setdefault("disk_free", lambda _archive: 100 * 1024**3)
    return fetch_stage.run(archive, unit, source=source, **kwargs)


def read_meta(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def place(directory: Path, unit: str, *, meta: bool = True) -> Path:
    """Put a finished demo (and its metadata file) into a directory."""
    directory.mkdir(parents=True, exist_ok=True)
    demo = directory / f"{unit}.dem.zst"
    demo.write_bytes(DEMO_BYTES)
    if meta:
        (directory / f"{unit}.meta.json").write_text(
            json.dumps({"sha256": "old", "size": len(DEMO_BYTES)}),
            encoding="utf-8",
        )
    return demo


# -- Matrix: a selected MapDemo, not in the archive --------------------------


def test_selected_map_demo_is_written_with_its_meta(archive, source) -> None:
    result = run(archive, source)

    demo = archive.demo(UNIT)
    assert result.status == "ok"
    assert result.skipped is False
    assert demo.read_bytes() == DEMO_BYTES

    meta = read_meta(archive.demo_meta(UNIT))
    assert meta["sha256"] == hashlib.sha256(DEMO_BYTES).hexdigest()
    assert meta["size"] == len(DEMO_BYTES)
    assert meta["source"] == "downloads_api"
    # A valid ISO instant and not just any string.
    assert datetime.fromisoformat(meta["fetched_at"]).tzinfo is not None


def test_the_source_is_asked_for_exactly_the_unit_that_was_requested(
    archive,
) -> None:
    """The stage must not ask for any map other than the one it was given."""
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES), OTHER: FakeDemo(demo_bytes(b"WRONG"))})
    run(archive, source, UNIT)
    assert source.asked == [UNIT]
    assert archive.demo(UNIT).read_bytes() == DEMO_BYTES


def test_the_stream_is_closed_even_though_the_stage_never_saw_a_connection(
    archive, source
) -> None:
    run(archive, source)
    assert source.closed == 1


# -- Matrix: the demo already on disk (three locations, three tests) ---------


def test_demo_already_in_the_archive_is_not_downloaded(archive, source) -> None:
    place(archive.archive_demos_dir(), UNIT)

    result = run(archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []


def test_demo_already_in_the_local_demos_root_is_not_downloaded(
    local_archive, source
) -> None:
    place(local_archive.demos_root, UNIT)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []


def test_demo_in_the_archive_is_not_redownloaded_when_demos_root_is_set(
    local_archive, source
) -> None:
    """Turning the setting on must not download the whole sample again.

    If idempotence looked only where the writes go, every demo already
    downloaded into the synchronised folder would be fetched a second time --
    2.3 GB and the whole Downloads quota for the sake of a setting that did not
    exist before.
    """
    place(local_archive.archive_demos_dir(), UNIT)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []
    assert not (local_archive.demos_root / f"{UNIT}.dem.zst").exists()


def test_a_demo_in_import_with_the_canonical_name_is_not_downloaded(
    local_archive, source
) -> None:
    """``import/`` is the third search location -- **under the canonical name**.

    This is the state Story 3.6 produces: the id is in the archive's form and
    the metadata file has been written. Then an imported demo behaves exactly
    like a downloaded one, and no stage tells them apart.
    """
    place(local_archive.import_dir(), UNIT)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is True
    assert source.asked == []


def test_a_browser_downloaded_demo_in_import_is_still_fetched_again(
    local_archive, source
) -> None:
    """**A known gap, not a claim that this would not happen (A9).**

    A demo fetched with a browser is in ``import/`` under FACEIT's own name
    ``{match_id}-{round}-{instance}.dem`` -- a different id from the archive's
    ``{match_id}-{map_index}`` -- and it has no ``.meta.json``, which
    idempotence requires. So it downloads again.

    The test pins this **as the current state and not as a goal**: an earlier
    version put a file into ``import/`` under the canonical name and with
    metadata, that is, a state hand-importing does not produce, and so gave
    false assurance. When Story 3.6 names imported demos using
    ``instances[].id``, this test turns round -- and turning it round is then a
    deliberate change.
    """
    faceit_name = local_archive.import_dir() / f"{MATCH}-1-1.dem"
    faceit_name.parent.mkdir(parents=True, exist_ok=True)
    faceit_name.write_bytes(DEMO_BYTES)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is False
    assert source.asked == [UNIT]


def test_a_demo_in_import_without_a_meta_is_fetched_again(
    local_archive, source
) -> None:
    """A hand-copied demo has no metadata file -- and therefore no digest."""
    place(local_archive.import_dir(), UNIT, meta=False)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert result.skipped is False
    assert source.asked == [UNIT]


# -- Matrix: a partial state on disk (demo without meta, meta without demo) --


def test_demo_without_meta_is_downloaded_again_and_the_reason_says_why(
    archive, source
) -> None:
    place(archive.archive_demos_dir(), UNIT, meta=False)

    result = run(archive, source)

    assert result.status == "ok"
    assert result.skipped is False
    assert source.asked == [UNIT]
    assert "metadata file was missing" in (result.reason or "")
    assert read_meta(archive.demo_meta(UNIT))["sha256"] == (
        hashlib.sha256(DEMO_BYTES).hexdigest()
    )


def test_meta_without_demo_is_downloaded_again_and_the_old_meta_is_replaced(
    archive, source
) -> None:
    directory = archive.archive_demos_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{UNIT}.meta.json").write_text(
        json.dumps({"sha256": "old-digest", "size": 1}), encoding="utf-8"
    )

    result = run(archive, source)

    assert result.status == "ok"
    assert source.asked == [UNIT]
    meta = read_meta(archive.demo_meta(UNIT))
    assert meta["sha256"] == hashlib.sha256(DEMO_BYTES).hexdigest()
    assert meta["size"] == len(DEMO_BYTES)


# -- Matrix: the demo does not exist ------------------------------------------


def test_a_demo_the_source_does_not_have_is_no_demo_and_writes_nothing(
    archive,
) -> None:
    source = FakeDemoSource({})

    result = run(archive, source)

    assert result.status == "no_demo"
    assert archive.find_demo(UNIT) is None
    assert not archive.demo_meta(UNIT).exists()
    assert result.reason and UNIT in result.reason


def test_no_demo_is_a_different_status_from_download_failed(archive) -> None:
    """An absent demo and a broken connection must not look the same.

    They lead to different continuations: one will never succeed again, the
    other most likely succeeds on the very next run.
    """
    gone = run(archive, FakeDemoSource({UNIT: DemoUnavailable("Deleted.")}))
    broken = run(
        archive,
        FakeDemoSource({UNIT: ApiError("The API did not answer.", status_code=503)}),
    )
    assert gone.status == "no_demo"
    assert broken.status == "download_failed"


# -- Matrix: a broken stream and a short Content-Length -----------------------


def test_a_broken_stream_leaves_neither_a_demo_nor_a_temp_file(archive) -> None:
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, break_after=2)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert not archive.demo_meta(UNIT).exists()
    assert not has_temp_leftovers(archive.root)


def test_a_short_download_names_both_numbers_and_is_not_moved_into_place(
    archive,
) -> None:
    # The source promises more than it gives: that is exactly a short download.
    source = FakeDemoSource(
        {UNIT: FakeDemo(DEMO_BYTES, announce=len(DEMO_BYTES) + 4096)}
    )

    result = run(archive, source)

    assert result.status == "download_failed"
    assert str(len(DEMO_BYTES) + 4096) in (result.reason or "")
    assert str(len(DEMO_BYTES)) in (result.reason or "")
    assert archive.find_demo(UNIT) is None
    assert not has_temp_leftovers(archive.root)


def test_a_source_that_does_not_announce_a_length_says_so_out_loud(
    archive,
) -> None:
    """``None`` is "did not state" and not "zero bytes" -- **and that is said**.

    Without a ``Content-Length`` a broken stream looks exactly like a whole
    one: the file is in place, the start is in zstd form, the size is
    plausible. The stage cannot tell them apart, so it must not stay silent:
    unspoken uncertainty would look like certainty, and a truncated demo would
    be skipped for ever on the strength of idempotence.

    An earlier version of this test pinned only ``status == "ok"`` without
    establishing anything about wholeness -- it gave false assurance.
    """
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, announce=None)})

    result = run(archive, source)

    assert result.status == "ok"
    assert archive.demo(UNIT).read_bytes() == DEMO_BYTES
    # 1) The metadata file tells the machine that the length was not checked.
    assert read_meta(archive.demo_meta(UNIT))["length_verified"] is False
    assert result.stats["length_verified"] is False
    # 2) The reason tells a person the same, and what to do if parse falls over.
    reason = result.reason or ""
    assert "Content-Length" in reason
    assert "in parsing" in reason


def test_a_verified_download_does_not_carry_the_uncertainty_note(
    archive, source
) -> None:
    """A warning only when there is cause: otherwise it becomes background noise."""
    result = run(archive, source)

    assert read_meta(archive.demo_meta(UNIT))["length_verified"] is True
    assert result.stats["length_verified"] is True
    assert "Content-Length" not in (result.reason or "")


# -- Rubbish does not pass as a demo (2026-09-05) ----------------------------


def test_an_html_error_page_with_status_200_is_not_stored_as_a_demo(
    archive,
) -> None:
    """An error page arriving with a 200 status is a successful HTTP response.

    Nothing but its content tells it apart -- and if it were written out as a
    demo, idempotence would skip it **on every run, for ever**: the metadata
    file would give it a sha256 and ``source: downloads_api``, and nothing
    would try to fetch the real demo again.
    """
    source = FakeDemoSource({UNIT: FakeDemo(HTML_ERROR_PAGE)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert archive.find_demo_meta(UNIT) is None
    assert not has_temp_leftovers(archive.root)
    assert "zstd" in (result.reason or "")


def test_an_empty_response_is_not_stored_as_a_demo(archive) -> None:
    """``Content-Length: 0`` passes the length check (0 == 0)."""
    source = FakeDemoSource({UNIT: FakeDemo(b"", announce=0)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert "too little" in (result.reason or "")


def test_a_truncated_but_correctly_labelled_file_is_not_stored(archive) -> None:
    """A few kilobytes of zstd at the start is not a CS2 demo."""
    source = FakeDemoSource({UNIT: FakeDemo(ZSTD_MAGIC + b"a" * 5000)})

    result = run(archive, source)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None


def test_the_size_estimate_covers_the_largest_measured_demo() -> None:
    """The estimate is the disk gate's input: too small would let through too much.

    The largest compressed demo in the archive is 234,163,493 bytes (measured
    2026-09-05). The estimate has to be above it, or the description has to
    stop calling it an upper bound.
    """
    largest_measured = 234_163_493
    assert fetch_stage.DEMO_SIZE_ESTIMATE_BYTES > largest_measured


def test_the_minimum_size_is_far_below_a_real_demo() -> None:
    """The guard must not reject a real demo: the smallest is over 140 MB."""
    smallest_measured = 142 * 1024 * 1024
    assert fetch_stage.MIN_PLAUSIBLE_DEMO_BYTES < smallest_measured


# -- Matrix: disk space -------------------------------------------------------


def test_a_full_disk_stops_the_download_before_it_starts(archive, source) -> None:
    result = run(
        archive,
        source,
        disk_free=lambda _archive: 100 * 1024 * 1024,
        size_estimate=200 * 1024 * 1024,
        reserve_bytes=2 * 1024**3,
    )

    assert result.status == "download_failed"
    # **The most important claim:** the download was not started, so no quota
    # was spent and not a byte was written to disk.
    assert source.asked == []
    assert archive.find_demo(UNIT) is None
    assert result.reason and "Not enough disk space" in result.reason


def test_the_disk_message_tells_the_user_what_to_do(
    archive, source
) -> None:
    result = run(archive, source, disk_free=lambda _a: 1024, size_estimate=2048)
    reason = result.reason or ""
    assert "Free space is" in reason
    assert "is needed" in reason
    assert str(archive.demos_dir()) in reason


def test_unknown_free_space_does_not_block_the_download(archive, source) -> None:
    """``None`` is "could not be determined"; it must not stop the tool."""
    result = run(archive, source, disk_free=lambda _archive: None)
    assert result.status == "ok"


def test_free_space_is_measured_where_the_demos_are_written(
    local_archive, monkeypatch
) -> None:
    """Free space is asked of the target drive, not the archive's drive.

    Here both are the same disk, so the test cannot claim different numbers --
    but it can claim that the directory asked about is the target and not the
    archive root.
    """
    local_archive.demos_root.mkdir(parents=True)
    asked: list[Path] = []
    real = fetch_stage.shutil.disk_usage

    def spy(path):
        asked.append(Path(path))
        return real(path)

    monkeypatch.setattr(fetch_stage.shutil, "disk_usage", spy)
    fetch_stage.free_space(local_archive)

    assert asked == [local_archive.demos_root]


# -- The local demo directory (added 2026-09-05) -----------------------------


def test_with_demos_root_the_demo_never_lands_in_the_archive(
    local_archive, source
) -> None:
    result = run(local_archive, source)

    assert result.status == "ok"
    assert (local_archive.demos_root / f"{UNIT}.dem.zst").read_bytes() == DEMO_BYTES
    assert not (local_archive.archive_demos_dir() / f"{UNIT}.dem.zst").exists()
    assert not local_archive.archive_demos_dir().exists()


@pytest.mark.parametrize("local", [False, True])
def test_the_meta_is_always_written_next_to_the_demo(
    tmp_path, source, local: bool
) -> None:
    """The metadata file is a claim about that exact file; in separate
    directories the two would drift apart the moment one is copied or
    deleted."""
    archive = ArchivePaths(
        root=tmp_path / "arkisto",
        demos_root=(tmp_path / "demot") if local else None,
    )

    run(archive, source)

    demo = archive.find_demo(UNIT)
    assert demo is not None
    assert (demo.parent / f"{UNIT}.meta.json").is_file()


def test_outputs_never_claim_an_outside_path_is_an_archive_path(
    local_archive, source
) -> None:
    """By contract ``outputs`` is a path relative to the inside of the archive.

    A local demo is not in the archive, so it is not there -- and the absolute
    paths travel in ``stats``, where they claim nothing about the archive.
    """
    result = run(local_archive, source)

    assert result.outputs == ()
    assert result.stats["demo_path"] == str(local_archive.demo(UNIT))
    assert result.stats["meta_path"] == str(local_archive.demo_meta(UNIT))
    for output in result.outputs:
        assert not Path(str(output)).is_absolute()


def test_outputs_are_archive_relative_when_the_demo_is_in_the_archive(
    archive, source
) -> None:
    result = run(archive, source)

    assert [str(p) for p in result.outputs] == [
        f"demos/{UNIT}.dem.zst",
        f"demos/{UNIT}.meta.json",
    ]


# -- The write order and the digest ------------------------------------------


def test_the_meta_is_written_only_after_the_demo_is_in_place(
    archive, source, monkeypatch
) -> None:
    """The metadata file must never describe a file that does not exist.

    The order is measured from the order in which the atomic moves happen --
    that is the same event that makes a file visible to others.
    """
    order: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        order.append(Path(dst).name)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    run(archive, source)

    assert order == [f"{UNIT}.dem.zst", f"{UNIT}.meta.json"]


def test_an_interrupted_run_leaves_the_demo_without_a_meta_not_the_reverse(
    archive, source, monkeypatch
) -> None:
    """What an interrupted run leaves behind is repairable; the reverse would not be.

    A demo without metadata is downloaded again and repairs itself. Metadata
    without a demo would claim a digest for a file that does not exist -- and
    ``parse`` would attach it to its manifest.
    """

    def boom(*args, **kwargs):
        raise OSError("the disk dropped while the metadata file was written")

    monkeypatch.setattr(fetch_stage, "atomic_write_json", boom)

    result = run(archive, source)

    # A disk error is a unit's status, not a programming error (see A4).
    assert result.status == "download_failed"
    assert archive.demo(UNIT).is_file()
    assert not archive.demo_meta(UNIT).exists()


def test_the_demo_is_never_read_back_to_compute_its_hash(
    archive, source, monkeypatch
) -> None:
    """Reading 200 MB again for hashing is forbidden.

    The proof has two parts: the stream is read exactly once, and no demo file
    is opened in read mode.
    """
    opened: list[tuple[str, str]] = []
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        opened.append((str(file), mode))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)
    result = run(archive, source)
    monkeypatch.undo()

    assert result.status == "ok"
    assert source.reads[UNIT] == 1

    demo_opens = [
        (path, mode) for path, mode in opened if f"{UNIT}.dem.zst" in path
    ]
    assert demo_opens, "the demo was never opened -- the test measures nothing"
    for path, mode in demo_opens:
        assert "r" not in mode, f"the demo was opened in read mode: {path} ({mode})"


def test_the_hash_is_the_hash_of_the_bytes_that_were_written(archive) -> None:
    """The digest is evidence about the file and not the stream, if they differed."""
    payload = demo_bytes(b"DIGEST")
    source = FakeDemoSource({UNIT: FakeDemo(payload, chunk=7919)})

    run(archive, source)

    written = archive.demo(UNIT).read_bytes()
    assert read_meta(archive.demo_meta(UNIT))["sha256"] == (
        hashlib.sha256(written).hexdigest()
    )


def test_writes_are_atomic(archive, source) -> None:
    run(archive, source)
    assert not has_temp_leftovers(archive.root)


# -- The series: one fault does not bring the run down -----------------------


def test_one_missing_demo_does_not_stop_the_others(archive) -> None:
    third = f"{MATCH}-2"
    source = FakeDemoSource(
        {
            UNIT: FakeDemo(DEMO_BYTES),
            OTHER: DemoUnavailable("FACEIT has deleted the recording."),
            third: FakeDemo(demo_bytes(b"THIRD")),
        }
    )

    results = fetch_stage.run_many(
        archive,
        [UNIT, OTHER, third],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["ok", "no_demo", "ok"]
    assert archive.demo(UNIT).is_file()
    assert archive.demo(third).is_file()


def test_a_programming_error_is_not_swallowed_by_the_loop(archive) -> None:
    """``PappascoutError`` is a unit's status; anything else is a code fault.

    If the loop swallowed everything, a ``TypeError`` would hide among eleven
    successful downloads as the status ``download_failed``.
    """

    class Broken:
        def get_demo(self, map_demo_id: str):
            raise TypeError("programming error")

    with pytest.raises(TypeError):
        fetch_stage.run_many(
            archive, [UNIT], source=Broken(), disk_free=lambda _a: 100 * 1024**3
        )


def test_a_bad_identifier_is_rejected_before_anything_is_asked(
    archive, source
) -> None:
    with pytest.raises(PappascoutError):
        fetch_stage.run(archive, "../pako", source=source)
    assert source.asked == []


# -- The plan -----------------------------------------------------------------


def selection_document(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": select_stage.SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "team_key": "joukkue",
        "selections": rows,
    }


def write_selection(archive: ArchivePaths, rows: list[dict[str, Any]]) -> None:
    path = archive.selection("joukkue")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(selection_document(rows), ensure_ascii=False), encoding="utf-8"
    )


def test_the_plan_takes_only_the_maps_that_passed_the_roster_threshold(
    archive,
) -> None:
    write_selection(
        archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": OTHER, "roster_ok": False},
        ],
    )

    todo = fetch_stage.plan(archive, "joukkue")

    assert todo.pending == (UNIT,)
    assert todo.present == ()
    assert todo.selected == 1


def test_the_plan_separates_what_is_already_on_disk(local_archive) -> None:
    write_selection(
        local_archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": OTHER, "roster_ok": True},
        ],
    )
    place(local_archive.archive_demos_dir(), UNIT)

    todo = fetch_stage.plan(local_archive, "joukkue")

    assert todo.pending == (OTHER,)
    assert todo.present == (UNIT,)
    assert todo.selected == 2
    assert todo.estimated_bytes == fetch_stage.DEMO_SIZE_ESTIMATE_BYTES


def test_the_plan_says_what_to_run_when_the_selection_file_is_missing(
    archive,
) -> None:
    with pytest.raises(PappascoutError, match="select"):
        fetch_stage.plan(archive, "joukkue")


# -- The division's plan (Story 3.5) ------------------------------------------
#
# ``plan_division`` is ``plan``'s sibling: the same download, another way of
# choosing the units. The units are read from the **match index**, so this
# section's material is ``index/matches.json`` as it stands -- not a selection
# file.

#: The division these tests collect.
LEAGUE_ID = "94681888-b5da-4ab5-bf50-f44b666b98a3"

#: Another competition in the same index: its matches are not collected.
OTHER_LEAGUE = "11111111-2222-3333-4444-555555555555"

#: The match index's ``generated_at``, which the plan has to carry word for
#: word: it says how old the set of units in the run is.
INDEX_GENERATED_AT = "2026-09-04T18:20:11+00:00"


def league_settings(*ids: str) -> Any:
    """The ``[league]`` section here; with no arguments, :data:`LEAGUE_ID`."""
    from pappascout.domain.models import LeagueSettings

    return LeagueSettings(
        season=13,
        organizer_id="1bfc69fa-5a21-4ed9-9ef3-37edbd7210d8",
        championship_ids=list(ids) or [LEAGUE_ID],
        map_pool=["de_ancient", "de_nuke", "de_mirage"],
    )


def match_row(
    match_id: str,
    *,
    played: bool = True,
    status: str | None = "FINISHED",
    map_picks: list[str] | None = None,
    best_of: int | None = 2,
    competition_id: str = LEAGUE_ID,
    finished_at: str | None = "2026-08-31T20:14:00+00:00",
    omit_best_of: bool = False,
) -> dict[str, Any]:
    """One match row in the match index's shape.

    ``omit_best_of`` **removes the key altogether** rather than setting it to
    null: that is exactly how the index written into the archive on 2026-09-04
    looks, and that difference is what is being tested.
    """
    row: dict[str, Any] = {
        "match_id": match_id,
        "competition_id": competition_id,
        "status": status,
        "played": played,
        "scheduled_at": "2026-08-31T18:00:00+00:00",
        "started_at": "2026-08-31T18:02:00+00:00",
        "finished_at": finished_at,
        "map_picks": ["de_ancient", "de_nuke"] if map_picks is None else map_picks,
        "best_of": best_of,
        "teams": [],
    }
    if omit_best_of:
        del row["best_of"]
    return row


def write_matches_index(archive: ArchivePaths, rows: list[dict[str, Any]]) -> None:
    from pappascout.stages import discover as discover_stage

    path = archive.matches_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": discover_stage.SCHEMA_VERSION,
                "generated_at": INDEX_GENERATED_AT,
                "competition_ids": [LEAGUE_ID],
                "matches": rows,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_the_division_plan_takes_every_played_match_not_just_one_team(
    archive,
) -> None:
    """No roster threshold: collecting saves another team's match too."""
    write_matches_index(
        archive, [match_row("1-aaa"), match_row("1-bbb")]
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-aaa-0", "1-aaa-1", "1-bbb-0", "1-bbb-1")
    assert todo.present == ()
    assert todo.matches_played == 2
    assert todo.selected == 4
    assert todo.league_ids == (LEAGUE_ID,)
    assert todo.estimated_bytes == 4 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES


def test_an_unplayed_match_produces_no_row_and_no_call(archive) -> None:
    """An unplayed match's demo does not exist: asking for it would be doomed."""
    write_matches_index(
        archive,
        [
            match_row("1-aaa"),
            match_row(
                "1-tuleva", played=False, status="SCHEDULED", map_picks=[]
            ),
        ],
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-aaa-0", "1-aaa-1")
    assert todo.no_veto == ()
    assert todo.matches_played == 1
    assert "1-tuleva" not in "".join(todo.pending + todo.present)


def test_the_filter_is_played_not_the_status_string(archive) -> None:
    """``played`` is ``discover``'s **decision**, ``status`` is the source's word.

    They coincide most of the time, and that is exactly why the difference has
    to be tested on its own: changing the filter to ``status == "FINISHED"``
    would pass over every set of material in which they are the same -- and
    would take in an abandoned match or leave out a played one the source names
    otherwise.
    """
    write_matches_index(
        archive,
        [
            # Played, but the source says so in different words.
            match_row("1-pelattu", played=True, status="MATCH_COMPLETE"),
            # The source says FINISHED, but discover did not count it as played.
            match_row("1-peruttu", played=False, status="FINISHED"),
        ],
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-pelattu-0", "1-pelattu-1")
    assert todo.matches_played == 1


def test_a_played_match_without_map_picks_gets_its_own_bucket_with_a_reason(
    archive,
) -> None:
    """**A played match does not disappear quietly.** Empty ``map_picks``
    gets a bucket of its own.

    Story 3.3's review found the same fault in selection: there such a match
    was counted under the reason "not played", that is, the output claimed
    something about the match that was not true.
    """
    write_matches_index(
        archive,
        [
            match_row("1-aaa"),
            match_row("1-vedoton", map_picks=[], finished_at="2026-09-02T21:00:00+00:00"),
        ],
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-aaa-0", "1-aaa-1")
    assert [row.match_id for row in todo.no_veto] == ["1-vedoton"]
    only = todo.no_veto[0]
    assert only.finished_at == "2026-09-02T21:00:00+00:00"
    assert "played" in only.reason
    # The reason must not claim the match went unplayed.
    assert "not played" not in only.reason.lower()
    # And the match counts as played, because it has been played.
    assert todo.matches_played == 2


def test_a_missing_best_of_is_an_observation_not_an_assumption(archive) -> None:
    """Measured 2026-09-06: the field is on none of the archive's 66 rows.

    The maps are then read from ``map_picks``, and the plan **notes** the
    length as unknown instead of assuming a number.
    """
    write_matches_index(
        archive, [match_row("1-aaa", omit_best_of=True)]
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.best_of_unknown == ("1-aaa",)
    assert todo.pending == ("1-aaa-0", "1-aaa-1")


def test_a_known_best_of_is_not_flagged_as_unknown(archive) -> None:
    """The note is a claim, not decoration: not when the length is known."""
    write_matches_index(archive, [match_row("1-aaa", best_of=2)])

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.best_of_unknown == ()


def test_the_unknown_length_names_the_matches_it_means(archive) -> None:
    """**An observation has to be traceable.**

    One bool for the whole division would say that some match is unknown but
    not which -- and the user could not check the claim against the index. The
    justification "an observation and not an assumption" requires that the
    observation can be pointed at.
    """
    write_matches_index(
        archive,
        [
            match_row("1-tunnettu", best_of=2),
            match_row("1-puuttuu", omit_best_of=True),
            match_row("1-null", best_of=None),
        ],
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.best_of_unknown == ("1-puuttuu", "1-null")
    assert "1-tunnettu" not in todo.best_of_unknown


@pytest.mark.parametrize("value", [0, -1, -3])
def test_an_invalid_best_of_is_unknown_not_known(archive, value: int) -> None:
    """``0`` is not a short match but a broken field.

    The condition ``is None`` on its own would read it as a known length, and
    the output would claim to know the match's length. The boundary is the same
    as in :func:`~pappascout.domain.selection.guaranteed_maps` (``< 1``).
    """
    write_matches_index(archive, [match_row("1-rikki", best_of=value)])

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.best_of_unknown == ("1-rikki",)
    # And the maps are still read from the veto: an invalid length does not
    # lose them.
    assert todo.pending == ("1-rikki-0", "1-rikki-1")


def test_an_uncertain_map_is_attempted_not_skipped(archive) -> None:
    """A BO3 ended 2-0: the third map is attempted all the same.

    ``select`` does the opposite, and rightly -- a phantom row would falsify
    the sample. Here the asymmetry turns round: an attempt costs one call and
    an expected ``no_demo``, skipping costs a demo lost for good.
    """
    write_matches_index(
        archive,
        [
            match_row(
                "1-bo3",
                best_of=3,
                map_picks=["de_ancient", "de_nuke", "de_mirage"],
            )
        ],
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-bo3-0", "1-bo3-1", "1-bo3-2")


def test_a_match_from_another_competition_is_left_out(archive) -> None:
    """A division means a division -- not everything that is in the index."""
    write_matches_index(
        archive,
        [
            match_row("1-oma"),
            match_row("1-vieras", competition_id=OTHER_LEAGUE),
            match_row("1-nimeton", competition_id=None),
        ],
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-oma-0", "1-oma-1")
    assert todo.matches_played == 1


def test_two_configured_championships_are_both_collected(archive) -> None:
    """The filter reads the setting, not one hard-coded id."""
    write_matches_index(
        archive,
        [match_row("1-oma"), match_row("1-vieras", competition_id=OTHER_LEAGUE)],
    )

    todo = fetch_stage.plan_division(
        archive, league_settings(LEAGUE_ID, OTHER_LEAGUE)
    )

    assert todo.matches_played == 2
    assert "1-vieras-0" in todo.pending
    # The field says what the filter was: an empty plan's message reads it.
    assert todo.league_ids == (LEAGUE_ID, OTHER_LEAGUE)


@pytest.mark.parametrize("location", ["demos_root", "arkisto", "import"])
def test_a_demo_already_on_disk_is_present_from_every_location(
    tmp_path, location: str
) -> None:
    """``in_archive`` looks at all three locations, and so does collecting.

    Looking at one location would download a demo already fetched into the
    archive again into the local directory -- that is, spend the quota and
    double the disk usage.
    """
    archive = ArchivePaths(
        root=tmp_path / "arkisto", demos_root=tmp_path / "paikalliset"
    )
    directories = {
        "demos_root": tmp_path / "paikalliset",
        "arkisto": archive.archive_demos_dir(),
        "import": archive.import_dir(),
    }
    write_matches_index(archive, [match_row("1-aaa")])
    place(directories[location], "1-aaa-0")

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.present == ("1-aaa-0",)
    assert todo.pending == ("1-aaa-1",)
    assert todo.selected == 2
    assert todo.estimated_bytes == fetch_stage.DEMO_SIZE_ESTIMATE_BYTES


def test_a_demo_without_its_meta_is_not_counted_as_present(tmp_path) -> None:
    """A demo without its metadata file is an interrupted run, not a finished result."""
    archive = ArchivePaths(root=tmp_path / "arkisto")
    write_matches_index(archive, [match_row("1-aaa")])
    place(archive.archive_demos_dir(), "1-aaa-0", meta=False)

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-aaa-0", "1-aaa-1")
    assert todo.present == ()


def test_the_plan_carries_the_age_of_the_index_it_read(archive) -> None:
    """The index's age is a plan field: it bounds the whole set of units."""
    write_matches_index(archive, [match_row("1-aaa")])

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.index_generated_at == INDEX_GENERATED_AT


def test_the_division_plan_says_what_to_run_when_the_index_is_missing(
    archive,
) -> None:
    with pytest.raises(PappascoutError, match="discover"):
        fetch_stage.plan_division(archive, league_settings())


def test_a_broken_match_row_stops_the_plan_instead_of_vanishing(archive) -> None:
    """A skipped match would be exactly the silently lost demo."""
    write_matches_index(archive, [{"competition_id": LEAGUE_ID, "played": True}])

    with pytest.raises(PappascoutError, match="discover"):
        fetch_stage.plan_division(archive, league_settings())


def test_nothing_pending_is_a_plan_too(archive) -> None:
    """All already on disk: nothing to download, and no map leaves the count."""
    write_matches_index(archive, [match_row("1-aaa")])
    place(archive.archive_demos_dir(), "1-aaa-0")
    place(archive.archive_demos_dir(), "1-aaa-1")

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ()
    assert todo.selected == 2
    assert todo.estimated_bytes == 0


# -- The same id cannot end up on the list twice (review 2026-09-06) --------
#
# The product owner's requirement, word for word: "As long as we do not fetch
# duplicates or miss obvious matches." A duplicate would fetch the same demo
# twice and double both the count and the size estimate -- that is, a
# confirmation question that asks the wrong thing.


def test_a_duplicate_match_does_not_produce_a_duplicate_download(
    archive, monkeypatch
) -> None:
    """The guard is ``plan_division``'s own, not borrowed from the reader.

    ``matches_from_index`` rejects two rows with the same ``match_id``, but
    **the correctness of this function's result must not depend on another
    function's invariant that it does not enforce itself**. So the duplicate is
    fed in here past the reader: overlapping pages of a paginated response would
    produce exactly this, and a fix in the reader would leave this function
    still exposed.
    """
    from pappascout.stages import discover as discover_stage

    write_matches_index(archive, [match_row("1-aaa")])
    twice = discover_stage.matches_from_index(
        {"matches": [match_row("1-aaa")]}
    ) * 2
    monkeypatch.setattr(
        "pappascout.stages.discover.matches_from_index", lambda _d: twice
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == ("1-aaa-0", "1-aaa-1")
    assert todo.selected == 2
    assert todo.estimated_bytes == 2 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES


def test_a_duplicate_that_is_already_on_disk_is_counted_once(
    archive, monkeypatch
) -> None:
    """Deduplication applies to both buckets, not only to what is downloaded."""
    from pappascout.stages import discover as discover_stage

    write_matches_index(archive, [match_row("1-aaa")])
    place(archive.archive_demos_dir(), "1-aaa-0")
    twice = discover_stage.matches_from_index(
        {"matches": [match_row("1-aaa")]}
    ) * 2
    monkeypatch.setattr(
        "pappascout.stages.discover.matches_from_index", lambda _d: twice
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.present == ("1-aaa-0",)
    assert todo.pending == ("1-aaa-1",)


def test_the_dedup_keeps_the_index_order(archive, monkeypatch) -> None:
    """The order is the index's order: a set must not scramble it."""
    from pappascout.stages import discover as discover_stage

    rows = [match_row("1-aaa"), match_row("1-bbb"), match_row("1-ccc")]
    write_matches_index(archive, rows)
    original = discover_stage.matches_from_index({"matches": rows})
    # A repeat in the middle: a naive set operation would move rows to the
    # wrong place.
    overlapping = original[:2] + original
    monkeypatch.setattr(
        "pappascout.stages.discover.matches_from_index", lambda _d: overlapping
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.pending == (
        "1-aaa-0",
        "1-aaa-1",
        "1-bbb-0",
        "1-bbb-1",
        "1-ccc-0",
        "1-ccc-1",
    )


# -- The index's timestamp is checked before the screen (review 2026-09-06) --


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(None, id="key-missing"),
        pytest.param(1757000000, id="number"),
        pytest.param(["2026-09-04"], id="list"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace-only"),
    ],
)
def test_an_unusable_generated_at_becomes_none(archive, value) -> None:
    """``index_generated_at`` is **shown to the user as it stands**.

    That is why the check is in the plan and not where it is printed: a number
    or a list would be printed in the timestamp's place looking like a
    timestamp, and a bare space is truthy -- it would leave a blank spot on the
    line, which looks like an empty timestamp rather than an unknown one.
    """
    path = archive.matches_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    from pappascout.stages import discover as discover_stage

    document: dict[str, Any] = {
        "schema_version": discover_stage.SCHEMA_VERSION,
        "competition_ids": [LEAGUE_ID],
        "matches": [match_row("1-aaa")],
    }
    if value is not None:
        document["generated_at"] = value
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.index_generated_at is None
    # And an unusable timestamp does not block the plan: it is extra
    # information, not a condition.
    assert todo.pending == ("1-aaa-0", "1-aaa-1")


def test_a_generated_at_with_padding_is_trimmed_not_dropped(archive) -> None:
    """Surrounding whitespace does not make a timestamp unusable."""
    path = archive.matches_index()
    path.parent.mkdir(parents=True, exist_ok=True)
    from pappascout.stages import discover as discover_stage

    path.write_text(
        json.dumps(
            {
                "schema_version": discover_stage.SCHEMA_VERSION,
                "generated_at": f"  {INDEX_GENERATED_AT}\n",
                "competition_ids": [LEAGUE_ID],
                "matches": [match_row("1-aaa")],
            }
        ),
        encoding="utf-8",
    )

    todo = fetch_stage.plan_division(archive, league_settings())

    assert todo.index_generated_at == INDEX_GENERATED_AT


# -- A row without a reason is impossible (review 2026-09-06) ---------------


def test_a_no_veto_row_cannot_be_built_without_a_reason() -> None:
    """The type exists only to state a reason.

    A default would permit a row without one -- exactly the state the class was
    written against. A rule is better than a defence where it is printed.
    """
    with pytest.raises(TypeError):
        fetch_stage.NoVetoMatch(match_id="1-aaa")  # type: ignore[call-arg]


# -- A disk error is a unit's status, not a programming error (A4, 2026-09-05) --


def _full_disk(monkeypatch, target_name: str) -> None:
    """Give ``ENOSPC`` to every write that hits the target."""
    real_open = open

    def spy(file, mode="r", *args, **kwargs):
        if target_name in str(file) and "w" in mode:
            raise OSError(28, "No space left on device")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", spy)


def test_a_full_disk_mid_write_is_download_failed_not_a_crash(
    archive, source, monkeypatch
) -> None:
    """``[Errno 28]`` must not reach the screen with the words "programming error".

    The disk is as much a property of the unit as the network is: it can fill
    up during a write, the sync client can lock the file and the network drive
    can drop. All three raise ``OSError``, and none of them is a fault in the
    code.
    """
    _full_disk(monkeypatch, UNIT)

    result = run(archive, source)

    assert result.status == "download_failed"
    reason = result.reason or ""
    assert "disk" in reason.lower()
    assert "The other demos were fetched all the same" in reason
    assert archive.find_demo(UNIT) is None
    assert not has_temp_leftovers(archive.root)


def test_a_full_disk_does_not_stop_the_rest_of_the_run(
    archive, monkeypatch
) -> None:
    """The frozen constraint: one demo's failure does not interrupt the run.

    Without the ``OSError`` branch in ``run_many`` the next demo would not be
    attempted at all -- verified with a simulated full disk.
    """
    second = f"{MATCH}-1"
    source = FakeDemoSource(
        {UNIT: FakeDemo(DEMO_BYTES), second: FakeDemo(demo_bytes(b"SECOND"))}
    )
    _full_disk(monkeypatch, f"{UNIT}.dem")

    results = fetch_stage.run_many(
        archive,
        [UNIT, second],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["download_failed", "ok"]
    assert source.asked == [UNIT, second]
    assert archive.demo(second).is_file()


def test_run_many_turns_a_pappascout_error_into_a_unit_result(archive) -> None:
    """``run_many``'s own error branch: a unit must not disappear quietly.

    ``run`` raises on an unusable id, and without this branch the result row
    would never come into being -- the listing would look shorter than the
    plan, and nothing would say which unit was meant.
    """
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES)})

    results = fetch_stage.run_many(
        archive,
        ["../pako", UNIT],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["download_failed", "ok"]
    assert results[0].unit == "../pako"
    assert results[0].reason and "map_demo_id" in results[0].reason


# -- Whether the target directory can be written to (A5) ---------------------


def test_a_demos_root_that_is_a_file_is_reported_with_advice(
    tmp_path, source
) -> None:
    """A file where the directory should be does not show up in free space at all."""
    blocker = tmp_path / "demot"
    blocker.write_text("i am not a directory", encoding="utf-8")
    archive = ArchivePaths(root=tmp_path / "arkisto", demos_root=blocker)

    result = run(archive, source, disk_free=lambda _a: 100 * 1024**3)

    assert result.status == "download_failed"
    assert "demo directory" in (result.reason or "")
    # The advice is in a field of its own and not in the heading (D1), and it
    # names the one thing the user can change.
    assert DEMOS_ROOT_ENV_VAR in result.stats["next_step"]
    # The most important claim: no connection was opened, so no quota was spent.
    assert source.asked == []


def test_an_unwritable_demos_root_is_reported_before_any_call(
    tmp_path, source, monkeypatch
) -> None:
    """A write-protected directory: it is tried, not inferred from permissions."""
    local = tmp_path / "demot"
    archive = ArchivePaths(root=tmp_path / "arkisto", demos_root=local)
    real_write = Path.write_bytes

    def refuse(self, data):
        if "write-probe" in self.name:
            raise PermissionError(13, "Access is denied")
        return real_write(self, data)

    monkeypatch.setattr(Path, "write_bytes", refuse)

    result = run(archive, source, disk_free=lambda _a: 100 * 1024**3)

    assert result.status == "download_failed"
    assert "cannot be written to" in (result.reason or "")
    assert "Downloads quota" in (result.reason or "")
    assert "write permissions" in result.stats["next_step"]
    assert source.asked == []


def test_the_write_probe_leaves_nothing_behind(tmp_path, source) -> None:
    local = tmp_path / "demot"
    archive = ArchivePaths(root=tmp_path / "arkisto", demos_root=local)

    run(archive, source, disk_free=lambda _a: 100 * 1024**3)

    assert [p.name for p in local.glob("*write-probe*")] == []


# -- No two copies of the same demo (A10) ------------------------------------


def test_a_demo_missing_its_meta_is_refetched_in_place_not_duplicated(
    local_archive, source
) -> None:
    """A partial demo in the archive + the local directory in use.

    Writing to the default target would leave the archive's 190 MB in place and
    make a second copy beside it -- that is, double exactly what the local
    directory avoids.
    """
    place(local_archive.archive_demos_dir(), UNIT, meta=False)

    result = run(local_archive, source)

    assert result.status == "ok"
    assert source.asked == [UNIT]
    in_archive_path = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    assert in_archive_path.read_bytes() == DEMO_BYTES
    assert (local_archive.archive_demos_dir() / f"{UNIT}.meta.json").is_file()
    # No copy in the local directory.
    assert not (local_archive.demos_root / f"{UNIT}.dem.zst").exists()
    assert not (local_archive.demos_root / f"{UNIT}.meta.json").exists()


def test_an_orphan_meta_elsewhere_is_removed_not_left_behind(
    local_archive, source
) -> None:
    """Metadata without a demo in another directory (A9).

    Merely writing the new metadata would leave the old one in place claiming a
    digest for a file that is not there -- and ``parse`` reads the digest from
    the first metadata file it finds.
    """
    orphan_dir = local_archive.archive_demos_dir()
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / f"{UNIT}.meta.json"
    orphan.write_text(json.dumps({"sha256": "orphan", "size": 1}), encoding="utf-8")

    result = run(local_archive, source)

    assert result.status == "ok"
    assert not orphan.exists()
    assert (local_archive.demos_root / f"{UNIT}.meta.json").is_file()
    assert read_meta(local_archive.find_demo_meta(UNIT))["sha256"] != "orphan"
    assert "was removed" in (result.reason or "")


# -- Disk space with the real size (A12) -------------------------------------


def test_the_announced_length_is_checked_against_free_space_before_writing(
    archive,
) -> None:
    """``Content-Length`` is known before the write -- use it.

    The estimate is enough of a gate only until the source states the size.
    After that, using the estimate would be deliberate imprecision.
    """
    huge = 8 * 1024**3
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, announce=huge)})

    result = run(
        archive,
        source,
        # The estimate fits, the real size does not.
        size_estimate=1024,
        reserve_bytes=1024,
        disk_free=lambda _a: 4 * 1024**3,
    )

    assert result.status == "download_failed"
    assert "Not enough disk space" in (result.reason or "")
    assert size_of(archive, UNIT) is None
    assert not has_temp_leftovers(archive.root)


def size_of(archive: ArchivePaths, unit: str) -> int | None:
    found = archive.find_demo(unit)
    return None if found is None else found.stat().st_size


# -- Atomicity in local mode too (B8) ----------------------------------------


def test_writes_are_atomic_in_the_local_demos_root_too(
    local_archive, source
) -> None:
    """``settings.toml`` is what turns this very mode on."""
    run(local_archive, source)
    assert not has_temp_leftovers(local_archive.demos_root)
    assert not has_temp_leftovers(local_archive.root)


def test_a_broken_stream_leaves_no_temp_file_in_the_local_demos_root(
    local_archive,
) -> None:
    source = FakeDemoSource({UNIT: FakeDemo(DEMO_BYTES, break_after=2)})

    result = run(local_archive, source)

    assert result.status == "download_failed"
    assert not has_temp_leftovers(local_archive.demos_root)
    assert local_archive.find_demo(UNIT) is None


# -- The plan's row guard (B5) -----------------------------------------------


def test_the_plan_skips_rows_that_are_not_usable(archive) -> None:
    """A broken row must not become an id that indexes a path."""
    write_selection(
        archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": "", "roster_ok": True},
            {"map_demo_id": None, "roster_ok": True},
            {"roster_ok": True},
            "i am not a row",
        ],
    )

    todo = fetch_stage.plan(archive, "joukkue")

    assert todo.pending == (UNIT,)


def test_a_selection_file_without_a_selections_list_says_what_to_run(
    archive,
) -> None:
    path = archive.selection("joukkue")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": select_stage.SCHEMA_VERSION,
                "team_key": "joukkue",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(PappascoutError, match="select"):
        fetch_stage.plan(archive, "joukkue")


# -- Story 3.7: the sibling fixes --------------------------------------------


def test_the_plan_counts_a_duplicate_identifier_once(archive) -> None:
    """I/O matrix: the same ``map_demo_id`` twice -> the id once.

    The same guard as in :func:`plan_division` (Story 3.5). Without it a
    duplicate would double both the listing and the size estimate -- that is,
    the confirmation question would ask the wrong thing, and the estimate would
    lead the disk gate to refuse a download that would have fitted.
    """
    write_selection(
        archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": OTHER, "roster_ok": True},
        ],
    )

    todo = fetch_stage.plan(archive, "joukkue")

    assert todo.pending == (UNIT, OTHER)
    assert todo.selected == 2
    assert todo.estimated_bytes == 2 * fetch_stage.DEMO_SIZE_ESTIMATE_BYTES


def test_a_duplicate_identifier_already_on_disk_is_listed_once(
    local_archive,
) -> None:
    """The guard applies to the on-disk side too: "2 / 3" would be the wrong number."""
    write_selection(
        local_archive,
        [
            {"map_demo_id": UNIT, "roster_ok": True},
            {"map_demo_id": UNIT, "roster_ok": True},
        ],
    )
    place(local_archive.archive_demos_dir(), UNIT)

    todo = fetch_stage.plan(local_archive, "joukkue")

    assert todo.present == (UNIT,)
    assert todo.pending == ()
    assert todo.selected == 1
    assert todo.estimated_bytes == 0


def test_plan_does_not_shadow_the_module_level_map_demo_id() -> None:
    """``plan`` must not bind the name ``map_demo_id`` locally.

    At module level there is a function of the same name
    (``domain.selection.map_demo_id``) that :func:`plan_division` calls. A local
    variable shadowed it inside this function: a line that needed the function
    would have crashed with ``str is not callable`` in the middle of a run and
    not at compile time. The claim is read from the syntax tree, because the
    shadowing shows neither in the output nor in the behaviour until it is too
    late.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(fetch_stage.plan)))
    bound = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    }
    assert "map_demo_id" not in bound


def test_an_orphan_meta_that_cannot_be_removed_is_never_claimed_removed(
    local_archive, source, monkeypatch
) -> None:
    """I/O matrix: an orphaned metadata file that cannot be removed -> no lie.

    The removal used to be ``except OSError: pass`` and the note reported a
    removal before it had even been attempted. A file lock held by the sync
    client is ordinary on Windows, and the metadata left behind claims a digest
    for a file that is not there -- ``parse`` reads the digest from the **first
    metadata file it finds**, so the lie does not stay in the output but ends up
    in the result.
    """
    orphan_dir = local_archive.archive_demos_dir()
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan = orphan_dir / f"{UNIT}.meta.json"
    orphan.write_text(json.dumps({"sha256": "orphan", "size": 1}), encoding="utf-8")

    real_unlink = Path.unlink

    def refuse(self: Path, *args: Any, **kwargs: Any):
        if str(self) == str(orphan):
            raise PermissionError(13, "the sync client is holding the file locked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse)

    result = run(local_archive, source)

    reason = result.reason or ""
    assert result.status == "ok"
    assert "was removed" not in reason
    assert "WARNING" in reason
    assert str(orphan) in reason
    # The demo is in the archive all the same: a failed removal is not the
    # download's fault.
    assert local_archive.find_demo(UNIT) is not None
    assert orphan.exists()


# -- The production port (B1) -------------------------------------------------


def test_default_source_really_builds_a_port(
    settings_file, env_file, tmp_path, monkeypatch
) -> None:
    """``default_source`` is really run -- **without going to the network**.

    It is the stage's only line that connects it to FACEIT, and every other
    test replaces it with a fake. Without this test a wrong cache directory or
    a wrong Downloads address would pass the whole suite, and the first real run
    would fetch from an address that does not exist.

    The port is **built, not used**: no request goes out.
    """
    from pappascout.adapters.faceit import FACEIT_DOWNLOADS_API_BASE
    from pappascout.adapters.protocols import DemoSource as DemoSourcePort
    from pappascout.domain.models import load_settings

    env = env_file(
        ".env",
        FACEIT_API_KEY="salainen-avain-XYZZY-42",
        FACEIT_DOWNLOADS_TOKEN="downloads-token-QUUX-77",
    )
    settings = load_settings(settings_file, env_files=(env,))
    archive = ArchivePaths(root=tmp_path / "arkisto")

    port = fetch_stage.default_source(settings, archive)

    assert isinstance(port, DemoSourcePort)
    assert port.downloads_base_url == FACEIT_DOWNLOADS_API_BASE
    assert port._client.cache_dir == archive.raw_faceit()
    # Neither secret shows in the representation; this is the place where the
    # port comes into being, and therefore also the place where the claim is
    # measurable.
    assert "XYZZY" not in repr(port)
    assert "QUUX" not in repr(port)
    port._client.close()


# -- Repetition ends the series, not a guess at the cause (D3, 2026-09-05) --
#
# Stopping at the first 400 would be tempting but unjustified: a 400 means
# either a malformed id (all of them fall over) or a malformed resource_url
# (only one falls over). C2's justification ("none of them can succeed") does
# not apply. Repetition, on the other hand, is measurable without the cause
# having to be known.


def failing(unit: str, status: int = 400) -> ApiError:
    return ApiError(f"Unit {unit} fell over.", status_code=status, advice="X")


def test_three_identical_failures_stop_the_run(archive) -> None:
    limit = fetch_stage.IDENTICAL_FAILURE_LIMIT
    units = [f"{MATCH}-{i}" for i in range(limit + 3)]
    source = FakeDemoSource({u: failing(u) for u in units})

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    # Only three were attempted; the rest got a row but no call.
    assert source.asked == units[:limit]
    assert len(results) == len(units), "there is a row for every unit"
    assert all(r.status == "download_failed" for r in results)
    assert all(r.stats.get("not_attempted") for r in results[limit:])


def test_the_units_that_were_not_attempted_say_so_and_say_why(archive) -> None:
    """Silent shortening would leave the user guessing where the rest went."""
    limit = fetch_stage.IDENTICAL_FAILURE_LIMIT
    units = [f"{MATCH}-{i}" for i in range(limit + 2)]
    source = FakeDemoSource({u: failing(u) for u in units})

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    skipped = results[limit:]
    assert skipped
    for result in skipped:
        assert "Not attempted" in (result.reason or "")
        assert "http-400" in (result.reason or "")
        assert "still unfetched" in result.stats["next_step"]


def test_different_failures_do_not_stop_the_run(archive) -> None:
    """Repetition is the **same** fault in a row, not any three faults.

    Different codes mean different causes, and no common fault can be inferred
    from them -- and ending the series then would be exactly the guess this
    rule avoids.
    """
    units = [f"{MATCH}-{i}" for i in range(5)]
    codes = [400, 500, 400, 500, 400]
    source = FakeDemoSource(
        {u: failing(u, c) for u, c in zip(units, codes, strict=True)}
    )

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert not any(r.stats.get("not_attempted") for r in results)


def test_a_success_between_failures_resets_the_run(archive) -> None:
    """A success proves the fault is not a common one."""
    units = [f"{MATCH}-{i}" for i in range(6)]
    source = FakeDemoSource(
        {
            units[0]: failing(units[0]),
            units[1]: failing(units[1]),
            units[2]: FakeDemo(DEMO_BYTES),
            units[3]: failing(units[3]),
            units[4]: failing(units[4]),
            units[5]: failing(units[5]),
        }
    )

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert [r.status for r in results][2] == "ok"


def test_repeated_missing_demos_do_not_stop_the_run(archive) -> None:
    """Three deleted demos in a row is a normal observation, not a fault.

    In an old sample ``no_demo`` is the expected outcome -- and if it ended the
    series, the newer demos would be left unfetched exactly when they are needed
    most.
    """
    units = [f"{MATCH}-{i}" for i in range(5)]
    source = FakeDemoSource(
        {u: DemoUnavailable(f"There is no demo {u}.") for u in units}
    )

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert all(r.status == "no_demo" for r in results)


def test_a_run_shorter_than_the_limit_is_never_cut_short(archive) -> None:
    """In a run of two units there is nothing to end."""
    units = [UNIT, OTHER]
    source = FakeDemoSource({u: failing(u) for u in units})

    results = fetch_stage.run_many(
        archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert source.asked == units
    assert len(results) == 2


def test_the_limit_is_above_two_so_a_coincidence_does_not_stop_the_run() -> None:
    """Two identical codes in a row is plausible coincidence.

    Two deleted demos from the same match, for instance. The price of three
    calls is small next to ending the series on a wrong justification.
    """
    assert fetch_stage.IDENTICAL_FAILURE_LIMIT >= 3


# -- Every failure carries its advice (D1, at stage level) ------------------


def test_every_failure_from_the_stage_carries_a_next_step(archive) -> None:
    """The guard: a failure cannot be built without a next step.

    Without it a new failure path could produce a row with no advice, and the
    output would have to invent one -- that is, it would return to exactly the
    default the whole fix removes.
    """
    cases = {
        "disk": FakeDemo(DEMO_BYTES),
        "absent": DemoUnavailable("It does not exist."),
        "network": ApiError("It did not answer.", status_code=503),
        "rubbish": FakeDemo(HTML_ERROR_PAGE),
        "short": FakeDemo(DEMO_BYTES, announce=len(DEMO_BYTES) + 4096),
    }
    for name, entry in cases.items():
        unit = f"{MATCH}-0"
        source = FakeDemoSource({unit: entry})
        free = 1024 if name == "disk" else 100 * 1024**3
        result = run(archive, source, unit, disk_free=lambda _a, f=free: f)
        if result.status == "ok":
            continue
        step = str(result.stats.get("next_step", "")).strip()
        assert step, f"case {name!r} produced a failure without advice"


def test_a_failure_built_without_a_next_step_is_refused() -> None:
    """A guard is a guard only once it fires."""
    with pytest.raises(AssertionError, match="next_step"):
        fetch_stage._result(
            UNIT,
            status="download_failed",
            skipped=False,
            outputs=(),
            reason="something went wrong",
            started=0.0,
            stats={"downloaded_bytes": 0},
        )


def test_an_unknown_cause_does_not_default_to_run_it_again() -> None:
    """The default advice was the root cause of both live faults.

    The right advice for an unknown fault is to say that it is not known -- not
    to guess that running again helps.
    """
    assert "run the command again" not in fetch_stage.DEFAULT_NEXT_STEP.lower()
    assert fetch_stage.next_step(ValueError("x")) == fetch_stage.DEFAULT_NEXT_STEP


def test_the_next_step_is_read_from_the_error_not_guessed() -> None:
    """The advice comes from the place where the cause is known."""
    exc = ApiError("x", status_code=418, advice="Make some tea.")
    assert fetch_stage.next_step(exc) == "Make some tea."
