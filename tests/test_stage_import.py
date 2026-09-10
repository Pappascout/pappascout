"""``stages.import_demo`` -- the stage's tests (Story 3.6).

**No network and no demoparser2.** The stage sees two ports,
:class:`~pappascout.adapters.protocols.MatchSource` and
:class:`~pappascout.adapters.protocols.DemoParser`, and behind both of them
here is a fake.

Both fakes **check the input they are given**, and that is not a detail but
their whole value as guards. A match port that returns the same veto data
whatever it was asked for would pass every test even when the stage asks for
the wrong match; a header port that returns the same map name for any path
would pass them even when the stage reads the wrong file -- or a file that does
not exist. The review proved the latter: replacing ``read_map_name``'s path
with one that does not exist passed 70 tests. :class:`FakeMapNameParser` now
requires the path to exist and its content to be recognisable as a demo.

The test material is **real compression frames** and not bare magic bytes: the
zstd is written with ``zstandard`` and the gzip with ``gzip``, so that
``declared_size``, ``length_source`` and the wholeness check travel the real
path in a default run and not just the test's own.

The rows of the I/O matrix are one test each in this file, and the name
identifies the row.
"""

from __future__ import annotations

import ast
import gzip
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import zstandard
from conftest import has_temp_leftovers

from pappascout.adapters.decompress import DEMO_MAGIC, GZIP_MAGIC, ZSTD_MAGIC
from pappascout.adapters.protocols import DemoParser, Match, MatchSource, MatchTeam
from pappascout.archive.paths import ArchivePaths
from pappascout.errors import ApiError, PappascoutError, ParseError
from pappascout.stages import import_demo as import_stage

#: A well-formed match id: FACEIT's ``match_id`` itself has the shape
#: ``1-<uuid>``, that is, it has hyphens before the map number.
MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"

#: The archive's id for the first map. ``--map 1`` -> ``map_index 0``.
UNIT = f"{MATCH}-0"
UNIT_TWO = f"{MATCH}-1"

#: FACEIT's own file name for the same map:
#: ``{match_id}-{round}-{instance}``, where ``round`` is **1-based**. The
#: difference of one from the archive's id is the whole reason for the search.
FACEIT_NAME = f"{MATCH}-1-1.dem.zst"
FACEIT_NAME_PLAIN = f"{MATCH}-1-1.dem"

PICKS = ("de_ancient", "de_nuke")

#: A clock that does not depend on when the run happens -- comparing metadata
#: files requires it.
CLOCK = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)

#: There is always disk space in the tests, unless the test measures that very
#: thing.
ROOMY = 100 * 1024**3


def plain_demo(size: int = 4096) -> bytes:
    """An uncompressed demo: ``PBDEMS2`` and padding."""
    return (DEMO_MAGIC + bytes(range(256)) * 64)[:size]


PLAIN_BYTES = plain_demo()

#: **A real zstd frame**, not just the magic bytes. The frame states the
#: decompressed size, and that is the independent source of length the
#: wholeness check uses.
ZSTD_BYTES = zstandard.ZstdCompressor().compress(PLAIN_BYTES)

#: A real gzip stream with its end marker.
GZIP_BYTES = gzip.compress(PLAIN_BYTES)

#: Content that is not a demo: an HTML error page with a 200 status.
HTML_PAGE = b"<!doctype html><html><body>403 Forbidden</body></html>"

#: The same zstd frame truncated at the halfway point.
#:
#: **This is never decompressed in this file, and that follows from the
#: layering.** The stage sees the header only from behind
#: :class:`FakeMapNameParser`, so a truncated file is modelled as a
#: ``ParseError`` raised by the fake -- what is tested here is what the stage
#: does when **the port says** the file is partial. That decompression
#: *detects* the shortfall is a different claim and a different layer:
#: ``tests/test_demo_parser.py`` (decompression) and
#: ``tests/test_demo_parser_logic.py`` (``read_map_name``). They deliberately
#: use a **large** payload, because a small one would decompress to zero when
#: truncated and hit the old emptiness check; here the size does not matter,
#: because the bytes are not read.
TRUNCATED_ZSTD = ZSTD_BYTES[: len(ZSTD_BYTES) // 2]


# -- Fixtures ----------------------------------------------------------------


@dataclass
class FakeMatchSource:
    """The match port's fake: from an id to veto data, without a network.

    An unknown id raises ``ApiError`` just as the real adapter does. That is a
    guard and not a courtesy: a source that gives veto data for any string at
    all would say that the stage never checks what it asks for.
    """

    matches: dict[str, Match] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        raise AssertionError(
            "Importing must not fetch a competition's match list -- it needs "
            "one match's veto data."
        )

    def get_match(self, match_id: str) -> Match:
        self.asked.append(match_id)
        try:
            return self.matches[match_id]
        except KeyError:
            raise ApiError(
                f"Match {match_id} was not found.", status_code=404
            ) from None


@dataclass
class FakeMapNameParser:
    """The header port's fake: from **a file** to a map name.

    Three guards, and each of them falls over a different mutation:

    ``The path has to exist``
        The review replaced the path the stage reads with one that does not
        exist and passed 70 tests. The real adapter raises ``ParseError`` on a
        missing file; a fake that does not care about the path cannot prove
        that the stage reads the file it intends to move.
    ``The content has to be recognisable as a demo``
        Otherwise the fake would accept any file at all -- including the HTML
        error page the real adapter rejects.
    ``The name has to be known``
        An unknown file raises ``ParseError``, because that is exactly what the
        real adapter does with content it does not recognise.

    ``errors`` gives the file it names a ready-made exception: that is how a
    truncated compressed file can be modelled without the decompression
    library.
    """

    names: dict[str, str | None] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)
    asked: list[Path] = field(default_factory=list)

    def read_map_name(self, path: Path) -> str | None:
        path = Path(path)
        self.asked.append(path)
        if not path.is_file():
            raise AssertionError(
                f"The header was read from a path that does not exist: {path}. "
                "The real adapter would raise a ParseError here."
            )
        head = path.read_bytes()[: len(DEMO_MAGIC)]
        if not any(
            head.startswith(magic)
            for magic in (ZSTD_MAGIC, GZIP_MAGIC, DEMO_MAGIC)
        ):
            raise ParseError(
                f"The file {path.name} is not a CS2 demo: first bytes {head!r}."
            )
        if path.name in self.errors:
            raise self.errors[path.name]
        if path.name not in self.names:
            raise ParseError(
                f"The file {path.name} is not a CS2 demo: its header could not "
                "be read."
            )
        return self.names[path.name]

    def parse_demo(self, path: Path, sample_seconds: Any):
        raise AssertionError(
            "Importing must not parse the demo: the map name is read from the "
            "header."
        )


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


@pytest.fixture
def local_archive(tmp_path: Path) -> ArchivePaths:
    """An archive whose demos go **outside the archive** (Story 3.4)."""
    return ArchivePaths(root=tmp_path / "arkisto", demos_root=tmp_path / "demot")


def match(picks: tuple[str, ...] = PICKS, match_id: str = MATCH) -> Match:
    return Match(
        match_id=match_id,
        status="FINISHED",
        teams=(MatchTeam(team_id="a"), MatchTeam(team_id="b")),
        map_picks=picks,
        best_of=2,
    )


@pytest.fixture
def source() -> FakeMatchSource:
    return FakeMatchSource({MATCH: match()})


@pytest.fixture
def parser() -> FakeMapNameParser:
    return FakeMapNameParser({FACEIT_NAME: "de_ancient"})


def place(archive: ArchivePaths, name: str, data: bytes = ZSTD_BYTES) -> Path:
    """Put a file into the archive's import folder."""
    directory = archive.import_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(data)
    return path


def read_meta(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def make_plan(
    archive: ArchivePaths,
    source: FakeMatchSource,
    parser: FakeMapNameParser,
    map_no: int | str = 1,
    **kwargs: Any,
):
    kwargs.setdefault("disk_free", lambda _path: ROOMY)
    return import_stage.plan(
        archive, MATCH, map_no, source=source, parser=parser, **kwargs
    )


def do_import(
    archive: ArchivePaths,
    source: FakeMatchSource,
    parser: FakeMapNameParser,
    map_no: int | str = 1,
    **kwargs: Any,
):
    todo = make_plan(archive, source, parser, map_no, **kwargs)
    return todo, import_stage.run(archive, todo, now=lambda: CLOCK)


# -- Matrix: a FACEIT-named .dem.zst, the map matches ------------------------


def test_a_faceit_named_archive_is_moved_and_gets_its_meta(
    archive, source, parser
) -> None:
    origin = place(archive, FACEIT_NAME)

    todo, result = do_import(archive, source, parser)

    target = archive.demos_dir() / f"{UNIT}.dem.zst"
    assert target.read_bytes() == ZSTD_BYTES
    assert result.status == "ok"
    assert result.unit == UNIT
    assert todo.confirmations == ()
    meta = read_meta(archive.demos_dir() / f"{UNIT}.meta.json")
    assert meta["source"] == "import"
    assert meta["map_demo_id"] == UNIT
    assert meta["sha256"] == hashlib.sha256(ZSTD_BYTES).hexdigest()
    assert meta["size"] == len(ZSTD_BYTES)
    assert meta["fetched_at"] == CLOCK.isoformat()
    # A move and not a copy: the import folder is an inbox, not a store.
    assert not origin.exists()


def test_the_map_number_the_user_gives_is_one_based(archive, source) -> None:
    """``--map 2`` is the match's second map, that is, ``map_index`` 1."""
    place(archive, f"{MATCH}-2-1.dem.zst")
    parser = FakeMapNameParser({f"{MATCH}-2-1.dem.zst": "de_nuke"})

    todo, result = do_import(archive, source, parser, map_no=2)

    assert todo.map_index == 1
    assert result.unit == UNIT_TWO
    assert (archive.demos_dir() / f"{UNIT_TWO}.dem.zst").is_file()


def test_the_source_file_the_header_was_read_from_is_the_one_that_moved(
    archive, source, parser
) -> None:
    """The header was read from **the** file that was moved."""
    origin = place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser)

    assert parser.asked == [origin]
    assert todo.source_path == origin


# -- A1: a partial demo does not reach the archive ---------------------------


def test_a_truncated_archive_is_refused_and_the_source_survives(
    archive, source, parser
) -> None:
    """**The worst fault possible, and it has been measured on a real demo.**

    A ``.dem.zst`` truncated at the halfway point decompresses quietly to
    something partial, and the decompressed start is a valid CS2 demo --
    measured 2026-09-05: ``ANCIENT_vs_RCAVE_VETERANS.dem.zst`` gave the right
    suffix and the right map name ``de_ancient`` from a 148,871,905-byte file
    cut in half. Without the guard the chain would say "the map check matches",
    ask nothing, write ``length_verified: true`` and **delete the source**.

    ``import/`` holds six of season 12's league demos that FACEIT no longer
    offers. The trigger is everyday: Explorer writes under the final name while
    the copy is still running, or the sync client is still uploading.
    """
    origin = place(archive, FACEIT_NAME, TRUNCATED_ZSTD)
    parser.errors[FACEIT_NAME] = ParseError(
        "The demo's decompression came up short: the file states 4096 bytes "
        "decompressed, but it decompressed to 2048 bytes.",
        advice="Wait until the copy is finished and run the command again.",
    )

    with pytest.raises(ParseError) as err:
        make_plan(archive, source, parser)

    assert "came up short" in str(err.value)
    assert "source file was not touched" in str(err.value)
    assert origin.read_bytes() == TRUNCATED_ZSTD
    assert archive.find_demo(UNIT) is None


def test_the_advice_of_a_truncated_archive_survives_the_wrapping(
    archive, source, parser
) -> None:
    """A partial demo's own advice is **wait**, not "check that it is a demo".

    The general advice would send the user off to look for a fault in a file
    that is perfectly fine and whose copy is merely unfinished. The advice
    belongs to the fault, and the sharpest advice comes from the layer that
    knows what went wrong.
    """
    place(archive, FACEIT_NAME, TRUNCATED_ZSTD)
    parser.errors[FACEIT_NAME] = ParseError(
        "decompression came up short", advice="Wait until the copy is finished."
    )

    with pytest.raises(ParseError) as err:
        make_plan(archive, source, parser)

    assert err.value.advice == "Wait until the copy is finished."


def test_a_compressed_source_is_length_verified(archive, source, parser) -> None:
    """The zstd frame states the decompressed size, so wholeness can be established."""
    place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser)

    assert todo.declared_bytes == len(PLAIN_BYTES)
    assert todo.length_verified is True
    assert read_meta(todo.meta_path)["length_verified"] is True


def test_a_gzip_source_is_length_verified_by_its_end_marker(
    archive, source
) -> None:
    """Another mechanism, the same promise: a cut gzip fails in decompression."""
    name = f"{MATCH}-1-1.dem"
    place(archive, name, GZIP_BYTES)
    parser = FakeMapNameParser({name: "de_ancient"})

    todo, _result = do_import(archive, source, parser)

    assert todo.declared_bytes is None
    assert todo.length_verified is True


def test_an_uncompressed_source_is_not_length_verified(archive, source) -> None:
    """**An uncompressed demo carries no length information, and that is said.**

    ``length_verified`` used to be written true always, and that was a false
    claim: a ``.dem`` file has no frame size, no checksum and no end marker, so
    half a file is indistinguishable from a whole one. ``fetch`` writes the same
    field false when there was no ``Content-Length``; importing has the same
    situation and the same answer.
    """
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})

    todo, result = do_import(archive, source, parser)

    assert todo.length_verified is False
    assert read_meta(todo.meta_path)["length_verified"] is False
    # And the uncertainty is said out loud rather than kept quiet.
    assert "could not be established" in str(result.reason)


def test_a_source_that_grows_during_the_transfer_is_refused(
    archive, source, parser, monkeypatch
) -> None:
    """**A growing file is an everyday situation, not an exception.**

    Explorer writes under the final name while the copy is still running and
    the sync client uploads in the background. Without the size check at both
    ends a half file would be copied and a whole source deleted -- and the
    source is irreplaceable.
    """
    origin = place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)
    # The file grows between the plan and the transfer.
    origin.write_bytes(ZSTD_BYTES + b"lisaa dataa")

    with pytest.raises(PappascoutError) as err:
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert "changed during the transfer" in str(err.value)
    assert err.value.advice
    assert origin.exists()
    assert not todo.target_path.exists()
    assert not todo.meta_path.exists()


def test_the_source_is_removed_only_after_the_target_is_in_place(
    archive, source, parser
) -> None:
    """The source is not destroyed before target and metadata are in place."""
    origin = place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser)

    assert not origin.exists()
    assert todo.target_path.is_file()
    assert todo.meta_path.is_file()


# -- Matrix: the map name does not match the veto data -----------------------


def test_a_map_that_does_not_match_the_veto_is_a_forced_question(
    archive, source
) -> None:
    """A discrepancy is asked about, and the flag cannot skip it."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    todo = make_plan(archive, source, parser)

    assert len(todo.confirmations) == 1
    question = todo.confirmations[0]
    assert question.forced is True
    assert "de_nuke" in question.detail
    assert "de_ancient" in question.detail
    assert import_stage.unanswered(todo.confirmations, yes=True) == (question,)


def test_a_mismatch_names_the_map_number_that_would_be_right(
    archive, source
) -> None:
    """**A question with no option to answer is a poor question.**

    When the header's map is in the veto under another number, that number is
    exactly what the user needs. Without it they have no correct value at all
    -- and the temptation to answer "k" is strong, because the question offers
    no other way forward.
    """
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    todo = make_plan(archive, source, parser)

    assert "--map 2" in todo.confirmations[0].detail


def test_a_map_that_is_in_no_pick_says_so(archive, source) -> None:
    """A map not played in the match is a different fault from a wrong number."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_mirage"})

    todo = make_plan(archive, source, parser)

    assert "different match" in todo.confirmations[0].detail


def test_the_plan_alone_moves_nothing(archive, source) -> None:
    """Building the plan writes nothing."""
    origin = place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    make_plan(archive, source, parser)

    assert origin.read_bytes() == ZSTD_BYTES
    assert archive.find_demo(UNIT) is None


def test_a_confirmed_mismatch_is_written_into_the_result(archive, source) -> None:
    """A confirmed discrepancy does not disappear with the answer."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: "de_nuke"})

    _todo, result = do_import(archive, source, parser)

    assert "de_nuke" in str(result.reason)
    assert "de_ancient" in str(result.reason)
    assert result.stats["map_matches"] is False


# -- Matrix: the match has no veto data --------------------------------------


def test_a_match_without_veto_data_cannot_be_cross_checked_so_it_asks(
    archive,
) -> None:
    """An empty ``map_picks`` is "no veto data", not "the map matches"."""
    place(archive, FACEIT_NAME)
    source = FakeMatchSource({MATCH: match(picks=())})
    parser = FakeMapNameParser({FACEIT_NAME: "de_ancient"})

    todo = make_plan(archive, source, parser)

    assert todo.expected_map_name is None
    assert len(todo.confirmations) == 1
    assert todo.confirmations[0].forced is True
    assert "veto data" in todo.confirmations[0].detail
    assert import_stage.unanswered(todo.confirmations, yes=True) != ()


def test_a_demo_without_a_map_name_in_its_header_also_asks(
    archive, source
) -> None:
    """A missing name in the header is the same outcome for a different reason."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser({FACEIT_NAME: None})

    todo = make_plan(archive, source, parser)

    assert todo.header_map_name is None
    assert len(todo.confirmations) == 1
    assert todo.confirmations[0].forced is True


def test_a_map_outside_the_pool_is_kept_as_observed(archive) -> None:
    """A workshop version or ``de_train`` is a genuine observation and not an error."""
    place(archive, FACEIT_NAME)
    source = FakeMatchSource({MATCH: match(picks=("de_train",))})
    parser = FakeMapNameParser({FACEIT_NAME: "de_train"})

    todo = make_plan(archive, source, parser)

    assert todo.header_map_name == "de_train"
    assert todo.confirmations == ()


# -- Matrix: an uncompressed .dem --------------------------------------------


def test_an_uncompressed_demo_keeps_the_dem_suffix(archive, source) -> None:
    """The suffix comes from the magic bytes, not from the name given."""
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})

    _todo, result = do_import(archive, source, parser)

    assert (archive.demos_dir() / f"{UNIT}.dem").read_bytes() == PLAIN_BYTES
    assert not (archive.demos_dir() / f"{UNIT}.dem.zst").exists()
    assert result.stats["demo_path"].endswith(f"{UNIT}.dem")


def test_a_zst_named_file_that_is_not_compressed_gets_the_dem_suffix(
    archive, source
) -> None:
    """**The name lies, the content does not.**"""
    name = f"{MATCH}-1-1.dem.zst"
    place(archive, name, PLAIN_BYTES)
    parser = FakeMapNameParser({name: "de_ancient"})

    do_import(archive, source, parser)

    assert (archive.demos_dir() / f"{UNIT}.dem").is_file()
    assert not (archive.demos_dir() / f"{UNIT}.dem.zst").exists()


def test_a_gzip_demo_gets_the_gz_suffix(archive, source) -> None:
    """The third known format: ``.dem.gz``, the fallback for hand-imported demos."""
    name = f"{MATCH}-1-1.dem"
    place(archive, name, GZIP_BYTES)
    parser = FakeMapNameParser({name: "de_ancient"})

    do_import(archive, source, parser)

    assert (archive.demos_dir() / f"{UNIT}.dem.gz").is_file()


# -- Matrix: the file is not a demo at all -----------------------------------


@pytest.mark.parametrize(
    "data", [HTML_PAGE, b""], ids=["html", "empty"]
)
def test_a_file_that_is_not_a_demo_is_refused_and_nothing_moves(
    archive, source, parser, data: bytes
) -> None:
    """Rubbish is rejected **before anything has been moved**."""
    origin = place(archive, FACEIT_NAME, data)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    assert "is neither a demo" in str(err.value)
    assert origin.exists()
    assert archive.find_demo(UNIT) is None


def test_a_zstd_file_that_is_not_a_demo_inside_is_refused(archive, source) -> None:
    """Right first bytes are not enough: the header is read from the content."""
    place(archive, FACEIT_NAME)
    parser = FakeMapNameParser()  # an unknown file -> ParseError

    with pytest.raises(ParseError) as err:
        make_plan(archive, source, parser)

    assert "Nothing was moved" in str(err.value)
    assert err.value.advice
    assert archive.find_demo(UNIT) is None


# -- Matrix: the target is already in the archive ----------------------------


def test_an_existing_target_is_a_question_that_yes_may_skip(
    archive, source, parser
) -> None:
    """Overwriting is asked about, but ``--yes`` may skip it.

    The row is in the frozen I/O matrix. The justification is not "it can be
    fetched again" -- there is no Downloads authorisation -- but that what is
    being replaced is **the same unit's** demo in the same directory and the
    replacing content has been checked whole before the transfer.
    """
    place(archive, FACEIT_NAME)
    existing = archive.demos_dir() / f"{UNIT}.dem.zst"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"vanha sisalto")

    todo = make_plan(archive, source, parser)

    assert len(todo.confirmations) == 1
    assert todo.confirmations[0].forced is False
    assert str(existing) in todo.confirmations[0].detail
    assert import_stage.unanswered(todo.confirmations, yes=True) == ()
    assert import_stage.unanswered(todo.confirmations, yes=False) != ()
    assert existing.read_bytes() == b"vanha sisalto"


def test_replacing_a_demo_with_another_suffix_removes_the_old_file(
    archive, source
) -> None:
    """Two files with the same id must not be left in the archive."""
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    old = archive.demos_dir() / f"{UNIT}.dem.zst"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})

    _todo, result = do_import(archive, source, parser)

    assert not old.exists()
    assert (archive.demos_dir() / f"{UNIT}.dem").read_bytes() == PLAIN_BYTES
    assert old.name in str(result.reason)


def test_a_failed_removal_warns_instead_of_claiming_success(
    archive, source, monkeypatch
) -> None:
    """**The note must not report a removal that did not happen.**

    ``_remove``'s own documentation says failing is ordinary on Windows (a file
    lock held by the sync client, an open handle from the antivirus). If the
    return value were thrown away, the archive would be left with two files for
    the same id, ``find_demo`` would return **the old one** of them in
    ``DEMO_SUFFIXES`` order, and ``parse`` would read the digest from the
    metadata without checking it against that file -- that is, the report would
    come from the wrong demo, quietly.
    """
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)
    old = archive.demos_dir() / f"{UNIT}.dem.zst"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({FACEIT_NAME_PLAIN: "de_ancient"})
    todo = make_plan(archive, source, parser)

    def refuse(path: Path) -> bool:
        return False if path == todo.replaces else True

    monkeypatch.setattr(import_stage, "_remove", refuse)

    result = import_stage.run(archive, todo, now=lambda: CLOCK)

    warning = str(result.reason).split("WARNING")[1]
    assert "two files" in warning
    # Both files by name, so that the user knows which to delete.
    assert old.name in warning
    assert todo.target_path.name in warning
    # And the old one is still in place -- the note claims nothing else.
    assert old.is_file()


# -- A3: the local demo directory --------------------------------------------


def test_the_import_writes_where_the_demo_already_is(
    local_archive, source, parser
) -> None:
    """**The write goes where the demo already is** -- as in ``fetch``.

    If the target were always chosen by ``demos_dir()``, a demo in the archive
    (a synchronised folder, shared) would be deleted as "replaced", although
    the difference is the directory and not the file -- and ``--yes`` would
    skip the question. The justification "it can be fetched again" does not
    hold: there is no Downloads authorisation, and that is exactly why this
    command exists.
    """
    place(local_archive, FACEIT_NAME)
    in_archive = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    in_archive.parent.mkdir(parents=True, exist_ok=True)
    in_archive.write_bytes(b"vanha arkistodemo")

    todo, _result = do_import(local_archive, source, parser)

    # The new content went into the archive, because the demo was there.
    assert todo.target_path == in_archive
    assert in_archive.read_bytes() == ZSTD_BYTES
    assert todo.replaces is None
    # No second copy appeared in the local directory.
    assert not (local_archive.demos_root / f"{UNIT}.dem.zst").exists()


def test_the_meta_goes_beside_the_demo_not_into_the_write_directory(
    local_archive, source, parser
) -> None:
    """The metadata file is a claim about that exact file, and it follows it."""
    place(local_archive, FACEIT_NAME)
    in_archive = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    in_archive.parent.mkdir(parents=True, exist_ok=True)
    in_archive.write_bytes(b"vanha")

    todo, _result = do_import(local_archive, source, parser)

    assert todo.meta_path.parent == in_archive.parent
    assert not (local_archive.demos_root / f"{UNIT}.meta.json").exists()


def test_an_orphan_meta_in_another_directory_is_removed(
    local_archive, source, parser
) -> None:
    """An orphaned metadata file would claim a digest for a file that does not exist.

    ``parse`` reads the digest from the **first metadata file it finds**, so
    the wrong metadata in the wrong directory would make a fresh demo look up
    to date against an old demo's result. ``fetch`` removes the orphan;
    importing now does the same.
    """
    place(local_archive, FACEIT_NAME)
    orphan = local_archive.demos_root / f"{UNIT}.meta.json"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text(json.dumps({"sha256": "vanha"}), encoding="utf-8")
    in_archive = local_archive.archive_demos_dir() / f"{UNIT}.dem.zst"
    in_archive.parent.mkdir(parents=True, exist_ok=True)
    in_archive.write_bytes(b"vanha")

    todo, result = do_import(local_archive, source, parser)

    assert todo.orphan_meta == orphan
    assert not orphan.exists()
    assert "was removed" in str(result.reason)
    assert read_meta(todo.meta_path)["sha256"] != "vanha"


# -- A4: import/ is neither a target nor something to be replaced ------------


def test_a_file_in_the_import_folder_is_never_treated_as_the_target(
    archive, source, tmp_path
) -> None:
    """**Importing deletes nothing from ``import/`` except its own source.**

    ``ArchivePaths.find_demo`` walks through ``import/`` too -- that is the
    search order for ``parse``. For importing it is a different matter: a file
    of the same name there is not a version to be replaced but another file
    that importing does not own. The old code would have deleted it and broken
    the spec's Never rule "nothing is written anywhere but demos/".
    """
    canonical = archive.import_dir()
    canonical.mkdir(parents=True, exist_ok=True)
    second = canonical / f"{UNIT}.dem.zst"
    second.write_bytes(b"toisen demon tavut")

    outside = tmp_path / "lataukset" / "oma.dem.zst"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    todo, _result = do_import(archive, source, parser, file=outside)

    assert todo.replaces is None
    assert todo.target_path.parent == archive.demos_dir()
    assert second.read_bytes() == b"toisen demon tavut"


def test_an_import_folder_file_is_not_a_reason_to_ask_about_overwriting(
    archive, source, tmp_path
) -> None:
    """A file in ``import/`` is not a "target already in the archive" case."""
    canonical = archive.import_dir()
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / f"{UNIT}.dem.zst").write_bytes(b"toisen demon tavut")

    outside = tmp_path / "oma.dem.zst"
    outside.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    todo = make_plan(archive, source, parser, file=outside)

    assert todo.confirmations == ()


# -- Matrix: the map number --------------------------------------------------


@pytest.mark.parametrize("map_no", [0, -1])
def test_a_map_number_below_one_is_refused_with_the_numbering_rule(
    archive, source, parser, map_no: int
) -> None:
    """Zero is the most typical mistake, and it gets a sentence of its own."""
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no=map_no)

    assert "starts at one" in str(err.value)
    assert err.value.advice
    assert source.asked == []


def test_a_map_number_that_is_not_a_number_is_refused_by_the_stage(
    archive, source, parser
) -> None:
    """**A wrong value gets this tool's own error and not a library's.**

    The command line used to declare ``--map`` an integer, so ``typer`` fell
    over in its own message before the stage saw anything -- and the stage's own
    check was dead code, unreachable from the command line. Now the value comes
    as a string and the conversion is where the number's other rules are.
    """
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no="abc")

    assert "is not a whole number" in str(err.value)
    assert err.value.advice


def test_a_map_number_above_the_maximum_is_refused(
    archive, source, parser
) -> None:
    """**Without veto data this is the only bound the number has.**

    A future match carries no ``map_picks``, so ``--map 99`` would otherwise be
    valid and ``{match_id}-98`` would appear in the archive -- an id no stage
    can attach to anything.
    """
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no=import_stage.MAX_MAP_NO + 1)

    assert str(import_stage.MAX_MAP_NO) in str(err.value)
    assert err.value.advice
    assert source.asked == []


def test_a_match_without_veto_still_cannot_take_any_number(archive) -> None:
    """The same bound holds even when there is no veto data to check against."""
    source = FakeMatchSource({MATCH: match(picks=())})
    parser = FakeMapNameParser({FACEIT_NAME: "de_ancient"})
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError):
        make_plan(archive, source, parser, map_no=99)


def test_a_map_number_beyond_the_veto_says_how_many_maps_there_are(
    archive, source, parser
) -> None:
    """A BO2 has no third map, and the listing says which ones there are."""
    place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, map_no=3)

    message = str(err.value)
    assert "2 maps" in message
    assert "de_ancient" in message and "de_nuke" in message
    assert err.value.advice


# -- B3: the id is built by the domain's builder -----------------------------


def test_the_identifier_is_built_by_the_domain_builder(archive) -> None:
    """``domain.selection.map_demo_id`` is the id's canonical builder.

    In the review importing was the project's only place that built the id
    around it. The claim is about the call: a parallel ``f"{match}-{index}"``
    would bypass the domain's own check and show up nowhere.
    """
    import pappascout.stages.import_demo as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "build_map_demo_id" in calls


def test_a_match_id_too_long_for_a_path_is_refused_with_advice(
    archive, source, parser
) -> None:
    """**A rejection without advice is a genuinely reachable path, and it was fixed.**

    A 119-character ``--match`` passes the match id's own check (the limit is
    120) but exceeds it while the id is being built, because ``-0`` follows.
    ``archive.paths`` raises its own error without advice, and the message names
    the id ``map_demo_id`` -- which the user never gave.
    """
    long = "1" * 119

    with pytest.raises(PappascoutError) as err:
        import_stage.plan(
            archive,
            long,
            1,
            source=source,
            parser=parser,
            disk_free=lambda _p: ROOMY,
        )

    assert err.value.advice, "a rejection without advice"
    assert "--match" in err.value.advice


# -- Matrix: an unknown match ------------------------------------------------


def test_an_unknown_match_is_refused_and_names_the_index(
    archive, source, parser
) -> None:
    """The advice names the file where the ids are to be found."""
    place(archive, FACEIT_NAME)
    unknown = "1-00000000-0000-0000-0000-000000000000"

    with pytest.raises(PappascoutError) as err:
        import_stage.plan(
            archive,
            unknown,
            1,
            source=source,
            parser=parser,
            disk_free=lambda _p: ROOMY,
        )

    assert "index/matches.json" in str(err.value.advice)
    assert "discover" in str(err.value.advice)
    assert archive.find_demo(f"{unknown}-0") is None


# -- Matrix: the source file is not found ------------------------------------


def test_a_missing_source_file_lists_what_the_import_folder_holds(
    archive, source, parser
) -> None:
    """A missing file is almost always a wrong name or a wrong folder."""
    place(archive, "Ancient_vs_kaljukostaja.dem", PLAIN_BYTES)
    place(archive, "muistiinpanot.txt", b"not a demo")

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    message = str(err.value)
    assert "Ancient_vs_kaljukostaja.dem" in message
    assert "muistiinpanot.txt" not in message
    assert "--file" in str(err.value.advice)


def test_a_missing_import_folder_is_said_out_loud(archive, source, parser) -> None:
    """A folder that does not exist is a different thing from an empty folder."""
    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    assert "does not exist" in str(err.value)


# -- Matrix: two candidates --------------------------------------------------


def test_two_candidates_are_listed_and_nothing_is_chosen_silently(
    archive, source, parser
) -> None:
    """Measured 2026-09-05: one match is in the folder under two different suffixes."""
    place(archive, FACEIT_NAME, ZSTD_BYTES)
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    message = str(err.value)
    assert FACEIT_NAME in message
    assert FACEIT_NAME_PLAIN in message
    assert "--file" in str(err.value.advice)
    assert archive.find_demo(UNIT) is None


def test_the_ambiguity_advice_points_at_the_compressed_candidate(
    archive, source, parser
) -> None:
    """**The advice steers towards the compressed one, not the alphabetically first.**

    ``.dem`` < ``.dem.zst``, so alphabetical order would always name the
    uncompressed one -- measured 2026-09-05: the same match is 233 MB
    uncompressed and 169 MB compressed, and the difference stays in the
    synchronised folder for good.
    """
    place(archive, FACEIT_NAME, ZSTD_BYTES)
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    assert FACEIT_NAME in err.value.advice
    assert f'"{archive.import_dir() / FACEIT_NAME}"' in err.value.advice


def test_the_ambiguity_advice_quotes_a_path_with_spaces(
    tmp_path, source, parser
) -> None:
    """**Advice that cannot be copied is not advice.**

    The real archive's path contains spaces, so an unquoted path breaks up into
    several arguments in the shell and produces a ``typer`` error. This would
    happen on the first real run, because the ambiguous pair has been measured
    to exist.

    The directory name below is ``Program Files``: any name containing a space
    proves the same, and the real tree's folder name does not belong in a
    public repository.
    """
    archive = ArchivePaths(root=tmp_path / "Program Files" / "arkisto")
    place(archive, FACEIT_NAME, ZSTD_BYTES)
    place(archive, FACEIT_NAME_PLAIN, PLAIN_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser)

    advice = err.value.advice
    path = str(archive.import_dir() / FACEIT_NAME)
    assert " " in path, "the test measures nothing without a space in the path"
    assert f'--file "{path}"' in advice


def test_the_search_pattern_does_not_confuse_map_1_with_map_10(
    archive, source
) -> None:
    """``{match_id}-1-*`` must not match the file ``{match_id}-10-1.dem``."""
    place(archive, FACEIT_NAME)
    place(archive, f"{MATCH}-10-1.dem.zst")

    assert import_stage.candidates(archive, MATCH, 0) == (
        archive.import_dir() / FACEIT_NAME,
    )


# -- Matrix: --file from outside the import folder ---------------------------


def test_a_file_outside_the_import_folder_is_copied_not_moved(
    archive, source, tmp_path
) -> None:
    """The user's own file in the user's own place stays where it is."""
    outside = tmp_path / "lataukset" / "oma.dem.zst"
    outside.parent.mkdir(parents=True)
    outside.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    todo, result = do_import(archive, source, parser, file=outside)

    assert todo.move is False
    assert outside.read_bytes() == ZSTD_BYTES
    assert (archive.demos_dir() / f"{UNIT}.dem.zst").read_bytes() == ZSTD_BYTES
    assert "it was copied" in str(result.reason)
    # The header was read from exactly the file that was given.
    assert parser.asked == [outside]


def test_a_file_inside_the_import_folder_is_moved_even_when_named(
    archive, source, parser
) -> None:
    """``--file`` does not change the fact that the import folder is an inbox."""
    origin = place(archive, FACEIT_NAME)

    todo, _result = do_import(archive, source, parser, file=origin)

    assert todo.move is True
    assert not origin.exists()


def test_a_named_file_that_does_not_exist_is_refused(archive, source, parser) -> None:
    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, file=archive.root / "ei-ole.dem")

    assert "There is no file" in str(err.value)
    assert "--file" in str(err.value.advice)


# -- B7: disk space and disk errors ------------------------------------------


def test_a_full_target_disk_stops_the_import_before_it_starts(
    archive, source, parser
) -> None:
    """A full disk is the user's situation, not a programming error."""
    origin = place(archive, FACEIT_NAME)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, disk_free=lambda _p: 1024)

    assert "Not enough disk space" in str(err.value)
    assert "was not touched" in str(err.value)
    assert err.value.advice
    assert origin.exists()


def test_a_full_temp_disk_stops_the_import_before_the_header_is_read(
    archive, source, parser
) -> None:
    """**Importing needs space twice, and only one of them is obvious.**

    Reading the header decompresses the compressed demo in full into the
    machine's temp directory (208-316 MB with real demos). If TEMP fills up,
    decompression's own advice is "download the demo again" -- the wrong action
    and an instruction to fetch 230 MB that is perfectly fine.
    """
    import tempfile

    place(archive, FACEIT_NAME)
    temp_root = Path(tempfile.gettempdir())

    def free(path: Path) -> int:
        return 1024 if path == temp_root else ROOMY

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, disk_free=free)

    assert "to read the demo's header" in str(err.value)
    assert str(temp_root) in str(err.value)
    assert err.value.advice
    # The header was not even attempted.
    assert parser.asked == []


def test_a_disk_error_during_the_transfer_is_translated_with_advice(
    archive, source, parser, monkeypatch
) -> None:
    """``OSError`` must not escape to the screen as a "programming error".

    A full disk, a file lock held by the sync client and a dropped network
    drive are all the user's situations. Without translating them the screen
    would read "Unexpected error: [Errno 28]" with the advice "This is a
    programming error" -- that is, the wrong diagnosis and the wrong action.
    """
    origin = place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)

    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(import_stage, "_transfer", boom)

    with pytest.raises(PappascoutError) as err:
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert "a disk error" in str(err.value)
    assert err.value.advice
    assert "was not touched" in str(err.value)
    assert origin.exists()


# -- The write order and atomicity -------------------------------------------


def test_the_meta_file_is_written_only_after_the_demo(
    archive, source, parser, monkeypatch
) -> None:
    """The metadata file must never describe a file that does not exist."""
    place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)

    def boom(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(import_stage, "_transfer", boom)

    with pytest.raises(PappascoutError):
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert not todo.meta_path.exists()
    assert not todo.target_path.exists()


def test_the_source_file_survives_a_failed_transfer(
    archive, source, parser, monkeypatch
) -> None:
    """The source file is not destroyed before the target is in place."""
    origin = place(archive, FACEIT_NAME)
    todo = make_plan(archive, source, parser)
    monkeypatch.setattr(
        import_stage, "_transfer", lambda *a, **k: (_ for _ in ()).throw(OSError())
    )

    with pytest.raises(PappascoutError):
        import_stage.run(archive, todo, now=lambda: CLOCK)

    assert origin.read_bytes() == ZSTD_BYTES


def test_the_digest_is_the_digest_of_what_was_written(
    archive, source, parser
) -> None:
    """The digest is computed during the transfer, and it is about the target file."""
    place(archive, FACEIT_NAME)

    _todo, result = do_import(archive, source, parser)

    target = archive.demos_dir() / f"{UNIT}.dem.zst"
    assert result.stats["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()


def test_no_temporary_files_are_left_behind(archive, source, parser) -> None:
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    assert not has_temp_leftovers(archive.root)


def test_nothing_is_written_outside_the_demo_directory(
    archive, source, parser
) -> None:
    """Importing writes only into the demo directory."""
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    written = sorted(
        p.relative_to(archive.root).as_posix()
        for p in archive.root.rglob("*")
        if p.is_file()
    )
    assert written == [f"demos/{UNIT}.dem.zst", f"demos/{UNIT}.meta.json"]


def test_nothing_else_in_the_import_folder_is_touched(
    archive, source, parser
) -> None:
    """**Deletions are writes as much as anything else is.**

    The previous test looks at the files that came into being; this one looks
    at those that already existed. Without it, deleting from ``import/`` would
    show up nowhere.
    """
    place(archive, FACEIT_NAME)
    neighbour = place(archive, "Ancient_vs_kaljukostaja.dem", PLAIN_BYTES)
    note = place(archive, "LUE-MINUT.txt", b"tarkeaa")

    do_import(archive, source, parser)

    assert neighbour.read_bytes() == PLAIN_BYTES
    assert note.read_bytes() == b"tarkeaa"


def test_the_local_demo_directory_is_honoured(local_archive, source, parser) -> None:
    """``[project].demos_root`` steers the import just as it steers the download."""
    place(local_archive, FACEIT_NAME)

    _todo, result = do_import(local_archive, source, parser)

    assert (local_archive.demos_root / f"{UNIT}.dem.zst").is_file()
    assert (local_archive.demos_root / f"{UNIT}.meta.json").is_file()
    assert result.outputs == ()
    assert result.stats["demo_path"].startswith(str(local_archive.demos_root))


# -- An imported demo is indistinguishable from a downloaded one -------------


def test_the_only_difference_to_a_fetched_demo_is_the_source_field(
    tmp_path, source, parser
) -> None:
    """The same file name, the same directory, the same metadata shape."""
    from test_stage_fetch import FakeDemo, FakeDemoSource
    from pappascout.stages import fetch as fetch_stage

    downloaded = ArchivePaths(root=tmp_path / "ladattu")
    imported = ArchivePaths(root=tmp_path / "tuotu")
    # **The same byte string for both.** With different content the test would
    # compare two different demos, and sha256 and size could not be part of the
    # comparison at all.
    demo_bytes = ZSTD_BYTES + bytes(1024 * 1024)

    fetch_stage.run(
        downloaded,
        UNIT,
        source=FakeDemoSource({UNIT: FakeDemo(demo_bytes)}),
        disk_free=lambda _a: ROOMY,
        now=lambda: CLOCK,
    )

    path = imported.import_dir()
    path.mkdir(parents=True)
    (path / FACEIT_NAME).write_bytes(demo_bytes)
    todo = import_stage.plan(
        imported,
        MATCH,
        1,
        source=source,
        parser=FakeMapNameParser({FACEIT_NAME: "de_ancient"}),
        disk_free=lambda _p: ROOMY,
    )
    import_stage.run(imported, todo, now=lambda: CLOCK)

    a = read_meta(downloaded.demos_dir() / f"{UNIT}.meta.json")
    b = read_meta(imported.demos_dir() / f"{UNIT}.meta.json")
    assert set(a) == set(b)
    assert {k: v for k, v in a.items() if k != "source"} == {
        k: v for k, v in b.items() if k != "source"
    }
    assert (a["source"], b["source"]) == ("downloads_api", "import")
    assert (downloaded.demos_dir() / f"{UNIT}.dem.zst").read_bytes() == (
        imported.demos_dir() / f"{UNIT}.dem.zst"
    ).read_bytes()


def test_no_module_branches_on_the_source_field() -> None:
    """``source`` is traceability information, not control.

    **The claim is read from the syntax tree and not from strings.** The review
    proved that string needles can be evaded: a single-quoted
    ``meta.get('source')`` added a genuine branch to ``parse`` and 2,779 tests
    passed. The style of the quotation marks is not a rule but a matter of
    form, and the rule is "the field is not read" -- so the check looks at
    whether the constant ``"source"`` appears **as a lookup key**:
    ``x["source"]`` or ``x.get("source", ...)``. Both quoting styles produce the
    same ``ast.Constant``, so evasion is not possible.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "pappascout"
    readers: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "source"
            ):
                readers.append(f"{path.name}:{node.lineno} subscript")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "source"
            ):
                readers.append(f"{path.name}:{node.lineno} .get")
    assert readers == [], (
        "Something reads the metadata file's source field: the difference "
        "between an imported and a downloaded demo is traceability information "
        f"and not control. {readers}"
    )


# -- The ports ---------------------------------------------------------------


def test_the_fakes_satisfy_the_ports(source, parser) -> None:
    """A fake must not be looser than the port, or the tests measure nothing."""
    assert isinstance(source, MatchSource)
    assert isinstance(parser, DemoParser)


def test_default_source_really_builds_a_port(
    settings_file, env_file, tmp_path, monkeypatch
) -> None:
    """``default_source`` is really run -- **without going to the network**.

    It is the stage's only line that connects it to FACEIT, and every other
    test replaces it with a fake. Without this test its body never runs once in
    the whole suite: the review replaced it with a ``raise RuntimeError`` and
    not one test fell over. The same gap has been found three times in this
    project, and ``discover`` and ``fetch`` do this properly.

    **No Downloads token is given**, and that is part of the claim: importing
    downloads nothing, so a missing token must not stop the port from coming
    into being -- that is exactly the situation the whole command exists for.
    """
    from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings

    env = env_file(".env", FACEIT_API_KEY="salainen-avain-XYZZY-42")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    settings = load_settings(settings_file, env_files=(env,))
    archive_dir = ArchivePaths(root=tmp_path / "arkisto")

    port = import_stage.default_source(settings, archive_dir)

    assert isinstance(port, MatchSource)
    assert port.cache_dir == archive_dir.raw_faceit()
    # The key must not show in the representation.
    assert "XYZZY" not in repr(port)


def test_default_parser_really_builds_a_port() -> None:
    """``default_parser`` is really run, and it implements the port.

    The same gap as above: without this its body never runs once. The port is
    **built, not used**: not one demo is parsed.
    """
    port = import_stage.default_parser()

    assert isinstance(port, DemoParser)
    assert hasattr(port, "read_map_name")


def test_the_match_is_asked_exactly_once_and_nothing_else(
    archive, source, parser
) -> None:
    """Importing downloads nothing: the only outgoing call is for the veto data."""
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    assert source.asked == [MATCH]


def test_the_parser_is_only_asked_for_the_header(archive, source, parser) -> None:
    """``parse_demo`` fails the fake: parsing 230 MB is not the import's business."""
    place(archive, FACEIT_NAME)

    do_import(archive, source, parser)

    assert len(parser.asked) == 1


# -- C7: the numbers recorded in the result are genuine ----------------------


def test_every_stat_describes_what_actually_happened(
    archive, source, parser
) -> None:
    """**Every ``stats`` field is a claim, and claims are checked.**

    The review mutated six fields at once (``imported_bytes``, ``moved``,
    ``demos_dir``, ``source_path``, ``header_map_name``,
    ``expected_map_name``) and 70 tests passed. A field nothing reads is
    decoration -- and decoration that appears on the screen is a lie waiting
    its turn.
    """
    origin = place(archive, FACEIT_NAME)

    todo, result = do_import(archive, source, parser)

    stats = result.stats
    assert stats["map_demo_id"] == UNIT
    assert stats["size"] == len(ZSTD_BYTES)
    assert stats["imported_bytes"] == len(ZSTD_BYTES)
    assert stats["sha256"] == hashlib.sha256(ZSTD_BYTES).hexdigest()
    assert stats["demo_source"] == "import"
    assert stats["moved"] is True
    assert stats["length_verified"] is True
    assert stats["declared_bytes"] == len(PLAIN_BYTES)
    assert stats["demo_path"] == str(todo.target_path)
    assert stats["meta_path"] == str(todo.meta_path)
    assert stats["demos_dir"] == str(archive.demos_dir())
    assert stats["source_path"] == str(origin)
    assert stats["header_map_name"] == "de_ancient"
    assert stats["expected_map_name"] == "de_ancient"
    assert stats["map_matches"] is True
    # ``notes`` and ``reason`` are the same content in two forms: the output
    # prints every note on its own line, ``reason`` is their sum.
    assert " ".join(stats["notes"]) == result.reason
    assert len(stats["notes"]) >= 1


def test_the_copy_mode_is_recorded_as_a_copy(archive, source, tmp_path) -> None:
    """``moved`` tells a move from a copy, and it has to be checked on its own."""
    outside = tmp_path / "oma.dem.zst"
    outside.write_bytes(ZSTD_BYTES)
    parser = FakeMapNameParser({"oma.dem.zst": "de_ancient"})

    _todo, result = do_import(archive, source, parser, file=outside)

    assert result.stats["moved"] is False
    assert result.stats["source_path"] == str(outside)


# -- Details that are easy to break ------------------------------------------


def test_every_rejection_carries_its_own_advice(archive, source, parser) -> None:
    """A failure cannot be built without advice."""
    with pytest.raises(AssertionError):
        import_stage._reject("something went wrong", advice="   ")


def test_the_comparison_ignores_case_but_nothing_else() -> None:
    """A comparison is a comparison, not a tidying of the name."""
    assert import_stage.same_map("de_nuke", "DE_NUKE")
    assert import_stage.same_map(" de_nuke ", "de_nuke")
    assert not import_stage.same_map("nuke", "de_nuke")
    assert not import_stage.same_map("de_nuke", "de_ancient")


def test_the_target_suffix_comes_from_the_magic_bytes(tmp_path) -> None:
    for data, expected in (
        (ZSTD_BYTES, ".dem.zst"),
        (GZIP_BYTES, ".dem.gz"),
        (PLAIN_BYTES, ".dem"),
    ):
        path = tmp_path / "koe.bin"
        path.write_bytes(data)
        assert import_stage.target_suffix(path) == expected


def test_the_length_source_is_named_per_format(tmp_path) -> None:
    """Three formats, two answers -- and ``.dem``'s answer is ``None``."""
    for data, expected in (
        (ZSTD_BYTES, "the decompressed size stated by the zstd frame"),
        (GZIP_BYTES, "the gzip stream's end marker"),
        (PLAIN_BYTES, None),
    ):
        path = tmp_path / "koe.bin"
        path.write_bytes(data)
        assert import_stage.length_source(path) == expected


def test_importing_a_file_onto_itself_is_refused(archive, source, parser) -> None:
    """The source and the target cannot be the same file."""
    target = archive.demos_dir() / f"{UNIT}.dem.zst"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ZSTD_BYTES)

    with pytest.raises(PappascoutError) as err:
        make_plan(archive, source, parser, file=target)

    assert "the same file" in str(err.value)
    assert target.read_bytes() == ZSTD_BYTES
