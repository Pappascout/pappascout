"""Tests for the atomic write -- the last row of the I/O matrix.

The archive lives in a synchronised folder two machines share, so a write
left half-finished would show up on the other machine as a truncated file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from conftest import has_temp_leftovers
from pappascout.archive.atomic_write import (
    _REPLACE_RETRY_DELAYS,
    atomic_path,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    host_tag,
    temp_suffix,
)

#: The characters Windows does not allow in a file name.
BAD_FILENAME_CHARS = chr(92) + '/:*?"<>|'


def test_temp_suffix_contains_host_name() -> None:
    """The temporary file's name is per machine (*.tmp-<host>-<pid>-<random>)."""
    suffix = temp_suffix()
    assert suffix.startswith(".tmp-")
    assert host_tag() in suffix
    assert str(os.getpid()) in suffix
    # The name has to be usable in a file name as it stands.
    assert not set(suffix) & set(BAD_FILENAME_CHARS)


def test_temp_suffix_is_unique_per_call() -> None:
    """Two parallel runs on the same machine must not use the same tmp name.

    Without the unique part, the second run would overwrite the first run's
    temporary file mid-write and the result would be a mixture of both.
    """
    names = {temp_suffix() for _ in range(50)}
    assert len(names) == 50


def test_parallel_writes_to_same_target_do_not_collide(tmp_path: Path) -> None:
    """Two simultaneous writes to the same target use different tmp files."""
    target = tmp_path / "result.parquet"
    with atomic_path(target) as first:
        with atomic_path(target) as second:
            assert first != second
            first.write_bytes(b"first")
            second.write_bytes(b"second")
        # The inner block finished first.
        assert target.read_bytes() == b"second"
    assert target.read_bytes() == b"first"
    assert not has_temp_leftovers(tmp_path)


def test_write_creates_file_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "subdirectory" / "result.json"
    atomic_write_json(target, {"round_no": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"round_no": 1}
    assert not has_temp_leftovers(tmp_path)


def test_text_and_bytes_round_trip(tmp_path: Path) -> None:
    text_file = tmp_path / "a.txt"
    # Deliberately non-ASCII: the point of this case is that the encode and
    # decode round trip through the atomic write does not mangle the text.
    atomic_write_text(text_file, "non-ASCII ä ö å survives")
    assert text_file.read_text(encoding="utf-8") == "non-ASCII ä ö å survives"

    bytes_file = tmp_path / "b.bin"
    atomic_write_bytes(bytes_file, b"\x00\x01\x02")
    assert bytes_file.read_bytes() == b"\x00\x01\x02"


def test_interrupted_write_leaves_no_target(tmp_path: Path) -> None:
    """The write fails halfway -> there is no target file, the tmp is gone."""
    target = tmp_path / "result.parquet"

    with pytest.raises(RuntimeError):
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"half")
            raise RuntimeError("parsing was interrupted")

    assert not target.exists()
    assert not has_temp_leftovers(tmp_path)


def test_interrupted_write_keeps_old_intact_version(tmp_path: Path) -> None:
    """The write fails halfway -> the old intact version stays in place."""
    target = tmp_path / "result.parquet"
    atomic_write_bytes(target, b"old intact version")

    with pytest.raises(RuntimeError):
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"new half")
            raise RuntimeError("the disk filled up")

    assert target.read_bytes() == b"old intact version"
    assert not has_temp_leftovers(tmp_path)


def test_successful_write_replaces_old_version(tmp_path: Path) -> None:
    target = tmp_path / "result.parquet"
    atomic_write_bytes(target, b"old")
    atomic_write_bytes(target, b"new")
    assert target.read_bytes() == b"new"
    assert not has_temp_leftovers(tmp_path)


def test_target_appears_only_after_context_exits(tmp_path: Path) -> None:
    """The target is never visible in a partial state."""
    target = tmp_path / "result.parquet"
    with atomic_path(target) as tmp:
        tmp.write_bytes(b"content")
        assert not target.exists()
    assert target.read_bytes() == b"content"


def test_forgetting_to_write_is_an_error(tmp_path: Path) -> None:
    """An empty block must not quietly produce an empty target file.

    The advice is pinned as well as the type. The caller who reaches this has
    written to the wrong path, and the only thing that tells them so is the
    second sentence of the message -- an exception type does not.
    """
    target = tmp_path / "result.parquet"
    with pytest.raises(FileNotFoundError) as exc:
        with atomic_path(target):
            pass
    assert "Write the content to the temporary path you were given" in str(exc.value)
    assert not target.exists()


# -- The Windows rename retry (2026-09-10) -------------------------------------
#
# ``os.replace`` intermittently raises ``PermissionError [WinError 5]`` on this
# machine although nothing in this process holds the target: a filter driver
# refuses the rename for 1-11 ms. These tests pin the two halves of the fix --
# that it retries at all, and that it still gives up.


def test_a_rename_that_is_refused_once_is_retried_and_the_write_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transient case: refused once, through on the second attempt.

    This is the whole point of the change, and nothing else in the suite
    proves it: without the retry the first refusal reaches the caller and the
    content is lost. The real ``os.replace`` is called on the second attempt,
    so the target is checked for its **content** and not merely for existing.
    """
    target = tmp_path / "table.parquet"
    real_replace = os.replace
    calls: list[tuple[object, object]] = []

    def refuse_once(src, dst, *args, **kwargs):
        calls.append((src, dst))
        if len(calls) == 1:
            raise PermissionError(13, "Access is denied", str(dst), 5)
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", refuse_once)

    with atomic_path(target) as tmp:
        tmp.write_bytes(b"payload")

    assert len(calls) == 2
    assert target.read_bytes() == b"payload"
    assert not has_temp_leftovers(tmp_path)


def test_a_rename_that_is_never_allowed_reaches_the_caller_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The permanent case: the budget runs out and the original error is raised.

    Three things are pinned, and each of them is a way the fix could go
    wrong. The error is the **same object**, so it cannot have been wrapped
    in a type of the fix's own. The budget is **bounded**, so a permanent
    failure cannot hang. And the waits were really taken, so a retry loop
    that had quietly become a single attempt would show up here.
    """
    target = tmp_path / "table.parquet"
    refusal = PermissionError(13, "Access is denied", str(target), 5)
    calls: list[int] = []

    def always_refuse(src, dst, *args, **kwargs):
        calls.append(1)
        raise refusal

    monkeypatch.setattr(os, "replace", always_refuse)

    started = time.perf_counter()
    with pytest.raises(PermissionError) as err:
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"payload")
    elapsed = time.perf_counter() - started

    assert err.value is refusal
    assert type(err.value) is PermissionError
    assert err.value.winerror == 5
    # One first attempt plus one per wait -- the budget, and nothing beyond it.
    assert len(calls) == len(_REPLACE_RETRY_DELAYS) + 1
    assert elapsed >= sum(_REPLACE_RETRY_DELAYS)
    assert elapsed < 2.0
    assert not target.exists()
    assert not has_temp_leftovers(tmp_path)


def test_an_error_that_is_not_a_permission_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the measured phenomenon is retried; everything else fails at once.

    A full disk or a cross-device move is not a filter driver holding the
    rename for a few milliseconds, and waiting 93 ms for it would be a
    hundred milliseconds of nothing. The call count is what pins the
    distinction -- the exception type alone would pass even if every
    ``OSError`` were retried.
    """
    target = tmp_path / "table.parquet"
    calls: list[int] = []

    def out_of_space(src, dst, *args, **kwargs):
        calls.append(1)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", out_of_space)

    with pytest.raises(OSError) as err:
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"payload")

    assert not isinstance(err.value, PermissionError)
    assert err.value.errno == 28
    assert calls == [1]
    assert not has_temp_leftovers(tmp_path)


def test_the_retry_budget_stays_near_the_measured_worst_case() -> None:
    """The budget is a measurement, not a round number.

    The worst block measured on 2026-09-10 cleared in 10.6 ms. The waits sum
    to 93 ms -- roughly nine times that -- and they rise, so the common case
    (cleared on the first retry) pays 1 ms. A future edit that turned the
    ladder into six flat 50 ms waits would keep the same test names and the
    same behaviour and cost three times the budget on every permanent
    failure; this is the only thing that would notice.
    """
    assert _REPLACE_RETRY_DELAYS == (0.001, 0.002, 0.005, 0.010, 0.025, 0.050)
    assert 0.05 < sum(_REPLACE_RETRY_DELAYS) < 0.15
    assert list(_REPLACE_RETRY_DELAYS) == sorted(_REPLACE_RETRY_DELAYS)
