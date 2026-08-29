"""Atomisen kirjoituksen testit -- I/O-matriisin viimeinen rivi.

Arkisto on OneDrivessa ja kahden koneen yhteinen, joten kesken jäänyt kirjoitus
näkyisi toisella koneella vajaana tiedostona.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conftest import has_temp_leftovers
from pappascout.archive.atomic_write import (
    atomic_path,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    host_tag,
    temp_suffix,
)

#: Merkit, joita Windows ei salli tiedostonimessä.
BAD_FILENAME_CHARS = chr(92) + '/:*?"<>|'


def test_temp_suffix_contains_host_name() -> None:
    """Väliaikaistiedoston nimi on konekohtainen (*.tmp-<host>-<pid>-<sattuma>)."""
    suffix = temp_suffix()
    assert suffix.startswith(".tmp-")
    assert host_tag() in suffix
    assert str(os.getpid()) in suffix
    # Nimen pitää kelvata tiedostonimeen sellaisenaan.
    assert not set(suffix) & set(BAD_FILENAME_CHARS)


def test_temp_suffix_is_unique_per_call() -> None:
    """Kaksi rinnakkaista ajoa samalla koneella ei saa käyttää samaa tmp-nimeä.

    Ilman uniikkia osaa toinen ajo ylikirjoittaisi ensimmäisen väliaikais-
    tiedoston kesken kirjoituksen ja lopputulos olisi sekoitus molempia.
    """
    names = {temp_suffix() for _ in range(50)}
    assert len(names) == 50


def test_parallel_writes_to_same_target_do_not_collide(tmp_path: Path) -> None:
    """Kaksi yhtaikaista kirjoitusta samaan kohteeseen käyttää eri tmp-tiedostoa."""
    target = tmp_path / "tulos.parquet"
    with atomic_path(target) as first:
        with atomic_path(target) as second:
            assert first != second
            first.write_bytes(b"eka")
            second.write_bytes(b"toka")
        # Sisempi lohko valmistui ensin.
        assert target.read_bytes() == b"toka"
    assert target.read_bytes() == b"eka"
    assert not has_temp_leftovers(tmp_path)


def test_write_creates_file_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "alihakemisto" / "tulos.json"
    atomic_write_json(target, {"round_no": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"round_no": 1}
    assert not has_temp_leftovers(tmp_path)


def test_text_and_bytes_round_trip(tmp_path: Path) -> None:
    text_file = tmp_path / "a.txt"
    atomic_write_text(text_file, "ääkköset ovat sallittuja")
    assert text_file.read_text(encoding="utf-8") == "ääkköset ovat sallittuja"

    bytes_file = tmp_path / "b.bin"
    atomic_write_bytes(bytes_file, b"\x00\x01\x02")
    assert bytes_file.read_bytes() == b"\x00\x01\x02"


def test_interrupted_write_leaves_no_target(tmp_path: Path) -> None:
    """Kirjoitus kaatuu kesken -> kohdetiedostoa ei ole, tmp siivottu."""
    target = tmp_path / "tulos.parquet"

    with pytest.raises(RuntimeError):
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"puolikas")
            raise RuntimeError("parsinta katkesi")

    assert not target.exists()
    assert not has_temp_leftovers(tmp_path)


def test_interrupted_write_keeps_old_intact_version(tmp_path: Path) -> None:
    """Kirjoitus kaatuu kesken -> vanha ehjä versio jää paikalleen."""
    target = tmp_path / "tulos.parquet"
    atomic_write_bytes(target, b"vanha ehja versio")

    with pytest.raises(RuntimeError):
        with atomic_path(target) as tmp:
            tmp.write_bytes(b"uusi puolikas")
            raise RuntimeError("levy tayttyi")

    assert target.read_bytes() == b"vanha ehja versio"
    assert not has_temp_leftovers(tmp_path)


def test_successful_write_replaces_old_version(tmp_path: Path) -> None:
    target = tmp_path / "tulos.parquet"
    atomic_write_bytes(target, b"vanha")
    atomic_write_bytes(target, b"uusi")
    assert target.read_bytes() == b"uusi"
    assert not has_temp_leftovers(tmp_path)


def test_target_appears_only_after_context_exits(tmp_path: Path) -> None:
    """Kohde ei näy osittaisena missään vaiheessa."""
    target = tmp_path / "tulos.parquet"
    with atomic_path(target) as tmp:
        tmp.write_bytes(b"sisalto")
        assert not target.exists()
    assert target.read_bytes() == b"sisalto"


def test_forgetting_to_write_is_an_error(tmp_path: Path) -> None:
    """Tyhjä lohko ei saa tuottaa tyhjää kohdetiedostoa hiljaa."""
    target = tmp_path / "tulos.parquet"
    with pytest.raises(FileNotFoundError):
        with atomic_path(target):
            pass
    assert not target.exists()
