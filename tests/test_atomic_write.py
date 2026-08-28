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
    nimet = {temp_suffix() for _ in range(50)}
    assert len(nimet) == 50


def test_parallel_writes_to_same_target_do_not_collide(tmp_path: Path) -> None:
    """Kaksi yhtaikaista kirjoitusta samaan kohteeseen käyttää eri tmp-tiedostoa."""
    kohde = tmp_path / "tulos.parquet"
    with atomic_path(kohde) as eka:
        with atomic_path(kohde) as toka:
            assert eka != toka
            eka.write_bytes(b"eka")
            toka.write_bytes(b"toka")
        # Sisempi lohko valmistui ensin.
        assert kohde.read_bytes() == b"toka"
    assert kohde.read_bytes() == b"eka"
    assert not has_temp_leftovers(tmp_path)


def test_write_creates_file_and_leaves_no_temp(tmp_path: Path) -> None:
    kohde = tmp_path / "alihakemisto" / "tulos.json"
    atomic_write_json(kohde, {"round_no": 1})
    assert json.loads(kohde.read_text(encoding="utf-8")) == {"round_no": 1}
    assert not has_temp_leftovers(tmp_path)


def test_text_and_bytes_round_trip(tmp_path: Path) -> None:
    teksti = tmp_path / "a.txt"
    atomic_write_text(teksti, "ääkköset ovat sallittuja")
    assert teksti.read_text(encoding="utf-8") == "ääkköset ovat sallittuja"

    tavut = tmp_path / "b.bin"
    atomic_write_bytes(tavut, b"\x00\x01\x02")
    assert tavut.read_bytes() == b"\x00\x01\x02"


def test_interrupted_write_leaves_no_target(tmp_path: Path) -> None:
    """Kirjoitus kaatuu kesken -> kohdetiedostoa ei ole, tmp siivottu."""
    kohde = tmp_path / "tulos.parquet"

    with pytest.raises(RuntimeError):
        with atomic_path(kohde) as tmp:
            tmp.write_bytes(b"puolikas")
            raise RuntimeError("parsinta katkesi")

    assert not kohde.exists()
    assert not has_temp_leftovers(tmp_path)


def test_interrupted_write_keeps_old_intact_version(tmp_path: Path) -> None:
    """Kirjoitus kaatuu kesken -> vanha ehjä versio jää paikalleen."""
    kohde = tmp_path / "tulos.parquet"
    atomic_write_bytes(kohde, b"vanha ehja versio")

    with pytest.raises(RuntimeError):
        with atomic_path(kohde) as tmp:
            tmp.write_bytes(b"uusi puolikas")
            raise RuntimeError("levy tayttyi")

    assert kohde.read_bytes() == b"vanha ehja versio"
    assert not has_temp_leftovers(tmp_path)


def test_successful_write_replaces_old_version(tmp_path: Path) -> None:
    kohde = tmp_path / "tulos.parquet"
    atomic_write_bytes(kohde, b"vanha")
    atomic_write_bytes(kohde, b"uusi")
    assert kohde.read_bytes() == b"uusi"
    assert not has_temp_leftovers(tmp_path)


def test_target_appears_only_after_context_exits(tmp_path: Path) -> None:
    """Kohde ei näy osittaisena missään vaiheessa."""
    kohde = tmp_path / "tulos.parquet"
    with atomic_path(kohde) as tmp:
        tmp.write_bytes(b"sisalto")
        assert not kohde.exists()
    assert kohde.read_bytes() == b"sisalto"


def test_forgetting_to_write_is_an_error(tmp_path: Path) -> None:
    """Tyhjä lohko ei saa tuottaa tyhjää kohdetiedostoa hiljaa."""
    kohde = tmp_path / "tulos.parquet"
    with pytest.raises(FileNotFoundError):
        with atomic_path(kohde):
            pass
    assert not kohde.exists()
