"""Manifesti: vaiheiden ohitussopimus (AD-1).

Jokaisen vaiheen tulos saa rinnalleen ``*.manifest.json``-tiedoston. Kun vaihetta
ollaan ajamassa uudelleen, se rakentaa odotetun manifestin ja vertaa sitä
levyllä olevaan: jos ne täsmäävät, vaihe ohitetaan. Tämä on se mekanismi, jolla
kynnysarvon säätö valmistuu sekunneissa -- parsintaa ei ajeta uudelleen, koska
``parse``-manifestin ``params_hash`` lasketaan vain ``[parse]``-osiosta eikä se
muutu.

Syötteiden tiivisteitä **ei lasketa uudelleen tiedostoista** vaan luetaan niiden
omista meta- tai manifesttiedostoista. Yhden demon hashaus on 233 MB työtä, ja
OneDrive-arkistossa se olisi sekä hidasta että altista väärille invalidoinneille.

Sääntö ``tool_versions``-kentälle
---------------------------------
Manifestiin merkitään **vain ne työkalut, joiden versio oikeasti muuttaa
kyseisen vaiheen tulosta** -- ei kaikkia asennettuja paketteja eikä pappascoutin
omaa versiota. Perustelu: jos ``pappascout``-versio olisi mukana, jokainen
korjauspäivitys invalidoisi koko arkiston ja pakottaisi satojen demojen
uudelleenparsinnan. Se on suoraan vastoin lupausta nopeasta uudelleenajosta.

===============  =========================================
Vaihe            ``tool_versions``
===============  =========================================
``parse``        ``demoparser2``
``classify``     (tyhjä -- puhdas domain-laskenta)
``aggregate``    (tyhjä)
``render``       ``jinja2``, jos raporttimalli muuttuu
===============  =========================================

Versiot luetaan asennetuista paketeista funktiolla :func:`tool_versions`, ei
kirjoiteta käsin.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pappascout.archive.atomic_write import atomic_write_text
from pappascout.constants import UnitStatus
from pappascout.errors import PappascoutError

__all__ = [
    "ManifestInput",
    "Manifest",
    "compute_params_hash",
    "tool_versions",
    "MANIFEST_SCHEMA_VERSION",
]

MANIFEST_SCHEMA_VERSION = "1.0.0"


class ManifestInput(BaseModel):
    """Yksi vaiheen syöte: edellisen tuloksen tunniste ja sen tiiviste.

    ``sha256`` luetaan syötteen omasta meta- tai manifesttiedostosta, ei
    laskemalla tiedostosta uudelleen.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: str
    sha256: str

    def key(self) -> tuple[str, str]:
        return (self.result_id, self.sha256)


class Manifest(BaseModel):
    """Vaiheen tuloksen manifesti.

    Attributes:
        result_id: Tämän tuloksen tunniste, jonka seuraava vaihe merkitsee
            syötteekseen.
        stage: Vaiheen nimi, esimerkiksi ``"parse"``.
        inputs: Syötteet tunnisteineen ja tiivisteineen.
        params_hash: Tiiviste **vain** siitä asetusosasta, jonka vaihe lukee.
        tool_versions: Työkaluversiot, joista tämän vaiheen tulos riippuu.
            Ks. moduulin docstringin sääntö -- ei pappascoutin omaa versiota.
        created_at: Luontihetki UTC:na.
        status: Yksikön tila (AD-9). Vain ``ok`` kelpaa ohitukseen.
        reason: Vapaa selitys muulle kuin ``ok``-tilalle.
        outputs: Tuloksen tiedostot arkiston sisäisinä suhteellisina polkuina.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = MANIFEST_SCHEMA_VERSION
    result_id: str
    stage: str
    inputs: list[ManifestInput] = Field(default_factory=list)
    params_hash: str
    tool_versions: dict[str, str] = Field(default_factory=dict)
    created_at: datetime
    status: UnitStatus = "ok"
    reason: str | None = None
    outputs: list[str] = Field(default_factory=list)

    @classmethod
    def new(
        cls,
        *,
        result_id: str,
        stage: str,
        params_hash: str,
        inputs: Sequence[ManifestInput] = (),
        tool_versions: Mapping[str, str] | None = None,
        status: UnitStatus = "ok",
        reason: str | None = None,
        outputs: Sequence[str] = (),
    ) -> Manifest:
        """Rakenna manifesti nykyisellä aikaleimalla."""
        return cls(
            result_id=result_id,
            stage=stage,
            params_hash=params_hash,
            inputs=list(inputs),
            tool_versions=dict(tool_versions or {}),
            created_at=datetime.now(UTC),
            status=status,
            reason=reason,
            outputs=[str(path) for path in outputs],
        )

    def is_current(
        self,
        *,
        inputs: Sequence[ManifestInput],
        params_hash: str,
        tool_versions: Mapping[str, str],
        root: Path | str,
    ) -> bool:
        """Kertoo, saako vaiheen ohittaa.

        Ohitus edellyttää, että

        * manifestin ``schema_version`` on tämän koodin tuntema,
        * tila on ``ok``,
        * syötteet, parametrihash ja työkaluversiot ovat täsmälleen samat, ja
        * **jokainen ``outputs``-tiedosto on yhä levyllä**.

        Viimeinen ehto on OneDriven takia pakollinen: pieni manifesti
        synkronoituu nopeasti, mutta satojen megatavujen tulos voi olla vielä
        matkalla tai käyttäjä on poistanut sen. Ilman tarkistusta vaihe
        ohitettaisiin ja seuraava vaihe kaatuisi puuttuvaan tiedostoon.

        Syötteiden järjestyksellä ei ole väliä. Aikaleima jätetään huomiotta --
        muuten mikään ei ohittuisi koskaan.

        Args:
            inputs: Odotetut syötteet tunnisteineen ja tiivisteineen.
            params_hash: Odotettu parametrihash.
            tool_versions: Odotetut työkaluversiot.
            root: Arkiston juuri, johon ``outputs``-polut suhteutetaan.
        """
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            return False
        if self.status != "ok":
            return False
        if self.params_hash != params_hash:
            return False
        if dict(self.tool_versions) != dict(tool_versions):
            return False
        if sorted(i.key() for i in self.inputs) != sorted(i.key() for i in inputs):
            return False
        return self.outputs_present(root)

    def outputs_present(self, root: Path | str) -> bool:
        """Ovatko kaikki tuloksen tiedostot yhä levyllä?"""
        return not self.missing_outputs(root)

    def missing_outputs(self, root: Path | str) -> list[str]:
        """Puuttuvat tulostiedostot -- lokitusta ja virheilmoitusta varten."""
        base = Path(root)
        return [name for name in self.outputs if not (base / Path(name)).exists()]

    def write(self, path: Path | str) -> Path:
        """Kirjoita manifesti atomisesti JSONina."""
        text = self.model_dump_json(indent=2)
        return atomic_write_text(path, text + "\n")

    @classmethod
    def read(cls, path: Path | str) -> Manifest:
        """Lue manifesti levyltä.

        Raises:
            PappascoutError: Jos tiedostoa ei ole, se on vioittunut tai se on
                kirjoitettu tuntemattomalla skeemaversiolla.
        """
        path = Path(path)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise PappascoutError(
                f"Manifestia ei löytynyt polusta {path}. "
                "Aja vaihe uudelleen, niin manifesti syntyy."
            ) from exc
        try:
            manifest = cls.model_validate_json(raw)
        except ValidationError as exc:
            raise PappascoutError(
                f"Manifesti {path} on vioittunut eikä sitä voi lukea. "
                "Poista tiedosto ja aja vaihe uudelleen.\n"
                f"{exc}"
            ) from exc

        if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
            raise PappascoutError(
                f"Manifesti {path} on kirjoitettu uudemmalla versiolla "
                f"(manifestissa {manifest.schema_version}, tämä koodi tuntee "
                f"version {MANIFEST_SCHEMA_VERSION}).\n"
                "Päivitä pappascout uusimpaan versioon tai poista manifesti, "
                "jolloin vaihe ajetaan uudelleen."
            )
        return manifest

    @classmethod
    def read_if_exists(cls, path: Path | str) -> Manifest | None:
        """Lue manifesti tai palauta ``None``, jos sitä ei ole.

        Vioittunut tai vieraalla skeemaversiolla kirjoitettu manifesti
        käsitellään puuttuvana: vaihe ajetaan uudelleen sen sijaan että ajo
        kaatuisi.
        """
        path = Path(path)
        if not path.is_file():
            return None
        try:
            return cls.read(path)
        except PappascoutError:
            return None


def compute_params_hash(params: Mapping[str, Any]) -> str:
    """Laske parametrihash yhdestä asetusosasta.

    Hash lasketaan kanonisesta JSON-esityksestä, jotta avainten järjestys tai
    TOML-muotoilu ei muuta tulosta. Anna tähän **vain** se asetusosa, jonka
    vaihe todella lukee -- se on koko ohitusmekanismin ehto.

    Kaikkien arvojen on oltava JSON-tyyppejä. Serialisointia ei paikata
    ``str()``-varasuunnitelmalla, koska esimerkiksi ``WindowsPath``
    merkkijonoutuu koneriippuvasti: kaksi konetta saisivat eri hashin samasta
    asetuksesta ja koko arkisto parsittaisiin uudelleen.

    Args:
        params: Asetusosa sanakirjana, esimerkiksi
            ``settings.parse.model_dump(mode="json")`` täydennettynä
            työkaluversiolla.

    Returns:
        64 merkin heksadesimaalinen sha256-tiiviste.

    Raises:
        PappascoutError: Jos jokin arvo ei ole JSON-serialisoituva. Viesti
            nimeää avaimen ja sen tyypin.
    """
    try:
        canonical = json.dumps(
            params, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except TypeError as exc:
        pahat = _offending_keys(params)
        raise PappascoutError(
            "Parametrihashia ei voi laskea: asetusosassa on arvo, jota ei voi "
            f"esittää JSONina ({pahat or exc}).\n"
            "Muunna arvo ensin JSON-tyypiksi, esimerkiksi "
            'settings.parse.model_dump(mode="json").\n'
            "Syy tiukkuudelle: esimerkiksi WindowsPath merkkijonoutuu "
            "koneriippuvasti, jolloin kaksi konetta laskisivat eri hashin ja "
            "koko arkisto parsittaisiin uudelleen."
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _offending_keys(params: Mapping[str, Any]) -> str:
    """Etsi avaimet, joiden arvo ei ole JSON-serialisoituva."""
    pahat: list[str] = []

    def walk(value: Any, polku: str) -> None:
        if value is None or isinstance(value, (str, int, float, bool)):
            return
        if isinstance(value, Mapping):
            for avain, alarvo in value.items():
                walk(alarvo, f"{polku}.{avain}" if polku else str(avain))
            return
        if isinstance(value, (list, tuple)):
            for i, alarvo in enumerate(value):
                walk(alarvo, f"{polku}[{i}]")
            return
        pahat.append(f"{polku} = {type(value).__name__}")

    walk(dict(params), "")
    return ", ".join(pahat)


def tool_versions(*names: str) -> dict[str, str]:
    """Lue annettujen pakettien versiot asennuksesta.

    Anna tähän **vain ne työkalut, joiden versio muuttaa tämän vaiheen
    tulosta** (ks. moduulin docstring). Esimerkiksi::

        tool_versions("demoparser2")   # parse-vaiheen manifestiin

    Raises:
        PappascoutError: Jos pakettia ei ole asennettu. Hiljainen ohitus
            tuottaisi manifestin, joka näyttää täsmäävän vaikka työkalu on
            vaihtunut.
    """
    versiot: dict[str, str] = {}
    for nimi in names:
        try:
            versiot[nimi] = _package_version(nimi)
        except PackageNotFoundError as exc:
            raise PappascoutError(
                f"Pakettia {nimi} ei ole asennettu, joten sen versiota ei voi "
                "kirjata manifestiin. Aja: uv sync"
            ) from exc
    return versiot
