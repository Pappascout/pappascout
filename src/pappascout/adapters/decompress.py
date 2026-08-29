"""Demotiedoston purku ja tunnistus.

Erillään parsinnasta tarkoituksella: FACEIT tarjoaa demot ``.dem.zst``-muodossa,
ja Epic 3:n demolataus tarvitsee saman purun sellaisenaan. Kun purku on omassa
moduulissaan, lataus voi kutsua sitä ilman että se raahaa mukanaan demoparser2:ta.

Purku on **virtaava**: 233 MB:n demo ei mahdu mielekkäästi 8 GB:n koneen muistiin
yhtaikaa parsinnan kanssa, joten pakattu tiedosto puretaan lohko kerrallaan
väliaikaistiedostoon. Väliaikaistiedosto on koneen omassa temp-hakemistossa,
**ei arkistossa**: arkisto on OneDrivessa, ja satojen megatavujen välituotteen
synkronointi olisi sekä hidasta että turhaa.

Purku kirjoittaa ensin ``<nimi>.tmp``-tiedostoon ja nimeää sen vasta lopuksi,
jotta keskeytynyt purku ei jätä jälkeensä puolikasta tiedostoa, joka näyttäisi
valmiilta demolta.
"""

from __future__ import annotations

import gzip
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pappascout.errors import ParseError

__all__ = [
    "DEMO_MAGIC",
    "ZSTD_MAGIC",
    "GZIP_MAGIC",
    "COMPRESSED_SUFFIXES",
    "is_compressed",
    "check_demo_magic",
    "decompress_to",
    "readable_demo",
    "decompressed_name",
]

#: CS2-demon tiedostotunniste. CS:GO:n vanha muoto alkoi ``HL2DEMO``.
DEMO_MAGIC = b"PBDEMS2"
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GZIP_MAGIC = b"\x1f\x8b"

#: Pakkauspäätteet, jotka riisutaan puretun tiedoston nimestä.
COMPRESSED_SUFFIXES: tuple[str, ...] = (".zst", ".zstd", ".gz")

#: Purun lohkokoko. Iso lohko on nopeampi, mutta muistinkäytön on pysyttävä
#: maltillisena, koska parsinta varaa oman osuutensa samalla koneella.
_CHUNK = 1024 * 1024


def _head(path: Path, size: int = 8) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError as exc:
        raise ParseError(
            f"Tiedostoa {path} ei voitu avata: {exc}\n"
            "Tarkista polku ja se, ettei tiedosto ole OneDriven "
            "pilvipaikkamerkki (avaa tiedosto kerran Resurssienhallinnassa)."
        ) from exc


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError as exc:
        raise ParseError(
            f"Tiedoston {path} kokoa ei voitu lukea: {exc}\n"
            "Tarkista, ettei tiedosto ole OneDriven pilvipaikkamerkki tai "
            "kesken siirtyvä."
        ) from exc


def decompressed_name(path: Path) -> str:
    """Puretun tiedoston nimi: pakkauspääte pois, muu nimi ennalleen.

    Nimeä **ei** katkaista ensimmäisestä pisteestä. FACEITin tiedostonimissä on
    useita pisteitä (``...-1-1.dem.zst``), ja katkaisu tuottaisi eri demoille
    helposti saman nimen -- kaksi yhtaikaista purkua voisi silloin kirjoittaa
    samaan tiedostoon.
    """
    name = path.name
    for suffix in COMPRESSED_SUFFIXES:
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if name.lower().endswith(".dem") else f"{name}.dem"


def is_compressed(path: Path) -> bool:
    """Onko tiedosto pakattu (zstd tai gzip)?

    Tunnistus tehdään tiedoston alkutavuista eikä päätteestä: käsin kopioitu
    demo voi olla nimetty väärin, ja väärä arvaus näkyisi käyttäjälle
    käsittämättömänä parsintavirheenä.
    """
    head = _head(path, 4)
    return head.startswith(ZSTD_MAGIC) or head.startswith(GZIP_MAGIC)


def check_demo_magic(path: Path) -> None:
    """Varmista, että puretun tiedoston alussa on ``PBDEMS2``.

    Raises:
        ParseError: Jos tiedosto ei ole CS2-demo. Tämä tarkistus on ennen
            demoparser2-kutsua, jotta tekstitiedosto ``.dem``-päätteellä antaa
            selkeän suomenkielisen virheen eikä kirjaston omaa viestiä.
    """
    head = _head(path, len(DEMO_MAGIC))
    if head != DEMO_MAGIC:
        raise ParseError(
            f"Tiedosto {path.name} ei ole CS2-demo: sen otsikko on "
            f"{head!r}, pitäisi olla {DEMO_MAGIC!r}.\n"
            "CS2-demot alkavat merkkijonolla PBDEMS2. Tarkista, että lataus "
            "onnistui ja että kyseessä on .dem-tiedosto eikä esimerkiksi "
            "virheilmoitussivu tai CS:GO-aikainen demo."
        )


def _zstd_module():
    """Tuo ``zstandard`` tai kerro suomeksi, miten se asennetaan."""
    try:
        import zstandard
    except ImportError as exc:  # pragma: no cover - riippuvuus on pyproject.tomlissa
        raise ParseError(
            "Pakattua demoa ei voi purkaa: paketti zstandard puuttuu.\n"
            "Aja: uv sync"
        ) from exc
    return zstandard


def decompress_to(source: Path, target: Path) -> Path:
    """Pura ``source`` tiedostoon ``target`` lohko kerrallaan.

    Tukee zstd- ja gzip-pakkausta. Pakkaamaton tiedosto kopioidaan sellaisenaan.
    Kirjoitus menee ensin ``<target>.tmp``-tiedostoon ja nimetään vasta
    onnistuessa, joten keskeytys ei jätä puolikasta ``target``ia.

    Returns:
        ``target``.

    Raises:
        ParseError: Jos purku epäonnistuu tai kohdehakemistoa ei voi luoda.
    """
    head = _head(source, 4)
    source_size = _size(source)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ParseError(
            f"Purkuhakemistoa {target.parent} ei voitu luoda: {exc}\n"
            "Tarkista levytila ja kirjoitusoikeudet."
        ) from exc

    tmp = target.with_name(target.name + ".tmp")
    try:
        if head.startswith(ZSTD_MAGIC):
            zstandard = _zstd_module()
            decompressor = zstandard.ZstdDecompressor()
            with open(source, "rb") as src, open(tmp, "wb") as dst:
                decompressor.copy_stream(src, dst, read_size=_CHUNK, write_size=_CHUNK)
        elif head.startswith(GZIP_MAGIC):
            with gzip.open(source, "rb") as src, open(tmp, "wb") as dst:
                shutil.copyfileobj(src, dst, length=_CHUNK)
        else:
            shutil.copyfile(source, tmp)

        # zstd ei nosta poikkeusta katkenneesta kehyksestä vaan lopettaa
        # hiljaa. Tyhjä tulos on siksi ainoa merkki siitä, että lataus jäi
        # kesken.
        if source_size > 0 and _size(tmp) == 0:
            raise ParseError(
                f"Demon {source.name} purku epäonnistui: tuloksena oli tyhjä "
                "tiedosto.\n"
                "Pakattu tiedosto on katkennut kesken latauksen. Lataa demo "
                "uudelleen."
            )
        os.replace(tmp, target)
    except ParseError:
        _remove(tmp)
        raise
    except Exception as exc:  # noqa: BLE001 - kirjastojen virheet vaihtelevat
        _remove(tmp)
        raise ParseError(
            f"Demon {source.name} purku epäonnistui: {exc}\n"
            "Tiedosto on todennäköisesti keskeneräinen tai vioittunut. "
            "Lataa demo uudelleen."
        ) from exc
    return target


def _remove(path: Path) -> None:
    """Siivoa väliaikaistiedosto; puuttuva tiedosto ei ole virhe."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - lukittu tiedosto Windowsilla
        pass


@contextmanager
def readable_demo(path: Path) -> Iterator[Path]:
    """Anna polku puretulle demolle ja siivoa jälkesi.

    Pakkaamaton demo annetaan sellaisenaan -- sitä ei kopioida turhaan.
    Pakattu demo puretaan koneen temp-hakemistoon ja poistetaan lopuksi, myös
    poikkeuksen sattuessa.

    Otsikko tarkistetaan aina **puretusta** sisällöstä, joten lohkosta ulos
    tuleva polku on aina varmasti CS2-demo. Tämä on olennaista: FACEIT voi
    palauttaa latauslinkin takaa virhesivun, joka pakkautuu moitteettomasti
    zstd-tiedostoksi mutta ei ole demo.

    Raises:
        ParseError: Jos tiedostoa ei ole, purku epäonnistuu tai tulos ei ole
            CS2-demo.
    """
    path = Path(path)
    if not path.is_file():
        raise ParseError(
            f"Demotiedostoa ei löytynyt polusta {path}.\n"
            "Tarkista polku tai kopioi demo arkiston import-hakemistoon."
        )

    if not is_compressed(path):
        check_demo_magic(path)
        yield path
        return

    try:
        workdir = Path(tempfile.mkdtemp(prefix="pappascout-purku-"))
    except OSError as exc:
        raise ParseError(
            f"Väliaikaishakemistoa ei voitu luoda purkua varten: {exc}\n"
            "Tarkista levytila ja TEMP-hakemiston oikeudet."
        ) from exc

    try:
        target = decompress_to(path, workdir / decompressed_name(path))
        check_demo_magic(target)
        yield target
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
