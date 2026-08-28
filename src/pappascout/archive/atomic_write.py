"""Atominen kirjoitus jaettuun OneDrive-arkistoon (AD-7).

Arkisto on OneDrivessa ja kahden koneen yhteinen, joten kesken jäänyt kirjoitus
näkyisi toisella koneella vajaana tiedostona. Siksi jokainen kirjoitus menee
ensin väliaikaistiedostoon ``<nimi>.tmp-<host>-<pid>-<sattuma>`` ja vasta
valmistuttuaan ``os.replace``-kutsulla kohteeseen. Nimessä oleva konenimi estää
kahta konetta käyttämästä samaa väliaikaistiedostoa; prosessitunnus ja
satunnaisosa estävät saman myös kahdelta rinnakkaiselta ajolta samalla koneella.

Jos kirjoitus keskeytyy, kohdetiedostoa ei ole tai se on vanha ehjä versio, ja
väliaikaistiedosto siivotaan pois.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "host_tag",
    "temp_suffix",
    "atomic_path",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


@lru_cache(maxsize=1)
def host_tag() -> str:
    """Konenimi tiedostonimeen kelpaavassa muodossa."""
    name = socket.gethostname() or "unknown-host"
    return _UNSAFE.sub("-", name).strip("-").lower() or "unknown-host"


def temp_suffix() -> str:
    """Uniikki väliaikaistiedoston pääte, esim. ``.tmp-tyopoyta-1234-9f3a1c07``.

    Jokainen kutsu palauttaa eri arvon: konenimi erottaa kaksi konetta,
    prosessitunnus ja satunnaisosa kaksi rinnakkaista ajoa samalla koneella.
    Ilman niitä kaksi yhtaikaista kirjoitusta tuhoaisi toistensa tiedostot.
    """
    return f".tmp-{host_tag()}-{os.getpid()}-{secrets.token_hex(4)}"


def _temp_path(target: Path) -> Path:
    return target.with_name(target.name + temp_suffix())


@contextmanager
def atomic_path(target: Path | str) -> Iterator[Path]:
    """Anna väliaikaispolku, joka siirretään kohteeseen vasta onnistuessa.

    Käyttö::

        with atomic_path(polku) as tmp:
            df.write_parquet(tmp)

    Kohdehakemisto luodaan tarvittaessa. Poikkeuksen sattuessa
    väliaikaistiedosto poistetaan eikä kohdetta kosketa -- vanha versio jää
    ehjänä paikalleen.

    Args:
        target: Lopullinen tiedostopolku.

    Yields:
        Väliaikaistiedoston polku, johon sisältö kirjoitetaan.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _temp_path(target)
    try:
        yield tmp
        if not tmp.exists():
            raise FileNotFoundError(
                f"Atominen kirjoitus ei tuottanut tiedostoa {tmp}. "
                "Kirjoita sisältö annettuun väliaikaispolkuun."
            )
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_bytes(target: Path | str, data: bytes) -> Path:
    """Kirjoita tavut atomisesti. Palauttaa kohdepolun."""
    target = Path(target)
    with atomic_path(target) as tmp:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    return target


def atomic_write_text(
    target: Path | str, text: str, encoding: str = "utf-8"
) -> Path:
    """Kirjoita teksti atomisesti. Palauttaa kohdepolun."""
    return atomic_write_bytes(target, text.encode(encoding))


def atomic_write_json(target: Path | str, obj: Any, indent: int = 2) -> Path:
    """Kirjoita JSON atomisesti UTF-8:na. Palauttaa kohdepolun."""
    text = json.dumps(obj, ensure_ascii=False, indent=indent, sort_keys=True)
    return atomic_write_text(target, text + "\n")
