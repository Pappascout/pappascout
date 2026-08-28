"""Kerrossäännön testit.

Spinen riippuvuuskaavio on ``cli -> stages -> {domain, adapters, archive}`` ja
``adapters -> domain``. Kaikki sen säännöt ovat nyt valvottavissa
koneellisesti:

* ``domain`` ei tuo mitään muista pappascout-paketeista,
* ``archive`` ei riipu ``domain``ista -- se on putki, ei domain-mallien säilö,
* ``adapters`` ei tunne arkistoa, vaiheita eikä komentoriviä,
* ``stages`` ei kutsu komentoriviä takaisin, ja
* ``cli`` **ei kutsu adaptereita eikä arkistoa suoraan**.

Viimeinen sääntö oli Story 1.1:ssä löysennetty: ``info`` tarvitsi arkiston
polun, eikä ``stages``-pakettia ollut olemassa. Story 1.2 poisti löysennyksen.
Polut pyydetään nyt ``stages.archive_paths``ilta ja demoportti
``stages.parse.default_parser``ilta, joten komentorivi näkee vain ``stages``-
ja ``domain``-paketit.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "pappascout"

#: Paketit, joita paketti EI saa tuoda.
FORBIDDEN = {
    "domain": {"archive", "adapters", "stages", "cli"},
    "archive": {"domain", "adapters", "stages", "cli"},
    "adapters": {"archive", "stages", "cli"},
    "stages": {"cli"},
    "cli": {"adapters", "archive"},
}

#: Vain olemassa olevat paketit -- lista kasvaa itsestään, kun vaiheet tulevat.
EXISTING = sorted(p for p in FORBIDDEN if (SRC / p).is_dir())

#: Kaikki pappascout-alipaketit, myös vielä olemattomat. Suhteellinen tuonti
#: tunnistetaan vain näiden nimien perusteella.
PACKAGES = {"cli", "stages", "domain", "adapters", "archive", "templates"}


def _imported_packages(path: Path) -> set[str]:
    """Lue tiedosto ja kerää sen tuomat pappascout-alipaketit."""
    oma = path.relative_to(SRC).parts[0] if path.parent != SRC else ""
    return _scan(path.read_text(encoding="utf-8"), oma, str(path))


def _scan(source: str, oma_paketti: str, filename: str = "<koe>") -> set[str]:
    """Kerää tiedoston tuomat pappascout-alipaketit.

    Kattaa kolme muotoa:

    * ``import pappascout.archive``
    * ``from pappascout.archive import x``
    * ``from ..archive import x`` (suhteellinen)

    Ilman suhteellisten tuontien käsittelyä sääntö olisi helppo kiertää
    vahingossa.
    """
    tree = ast.parse(source, filename=filename)
    found: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "pappascout" and len(parts) > 1:
                    found.add(parts[1])
            continue

        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level == 0:
            if node.module:
                parts = node.module.split(".")
                if parts[0] == "pappascout" and len(parts) > 1:
                    found.add(parts[1])
            continue

        # Suhteellinen tuonti. level 1 = oma paketti, level 2 = pappascout-juuri.
        if node.module:
            juuri = node.module.split(".")[0]
            if node.level >= 2 and juuri in PACKAGES:
                found.add(juuri)
            elif node.level == 1 and oma_paketti in PACKAGES:
                found.add(oma_paketti)
        elif node.level >= 2:
            # from .. import archive
            for alias in node.names:
                if alias.name in PACKAGES:
                    found.add(alias.name)

    found.discard(oma_paketti)
    return found


@pytest.mark.parametrize("package", EXISTING)
def test_package_respects_dependency_arrows(package: str) -> None:
    directory = SRC / package
    kielletyt = FORBIDDEN[package]
    for path in directory.rglob("*.py"):
        rikkeet = _imported_packages(path) & kielletyt
        assert not rikkeet, (
            f"{path.relative_to(SRC)} tuo kielletyn paketin: "
            f"{', '.join(sorted(rikkeet))}"
        )


@pytest.mark.parametrize(
    "lahde,odotettu",
    [
        ("from ..archive import manifest", {"archive"}),
        ("from ..stages.parse import run", {"stages"}),
        ("from .. import archive", {"archive"}),
        ("from pappascout.archive import manifest", {"archive"}),
        ("import pappascout.stages.parse", {"stages"}),
        ("from .schemas import ROUNDS", set()),
        ("import polars as pl", set()),
    ],
)
def test_import_forms_are_all_detected(lahde: str, odotettu: set) -> None:
    """Sääntöä ei saa kiertää suhteellisella tuonnilla.

    Lähde annetaan merkkijonona, jotta testi ei kirjoita mitään src-puuhun.
    """
    assert _scan(lahde, "domain") == odotettu


def test_absolute_imports_are_detected() -> None:
    """Nykyinen koodi tuo domainin ja archiven vain absoluuttisesti."""
    loydot = _imported_packages(SRC / "archive" / "manifest.py")
    assert "archive" not in loydot  # oma paketti ei ole riippuvuus
    assert "domain" not in loydot


def test_domain_does_no_file_io_except_settings_loading() -> None:
    """``domain`` on puhdas: vain asetusten lataus koskee levyä.

    ``schemas`` sisältää pelisäännöt ja taulusopimukset eikä saa avata
    tiedostoja; ``models`` lataa asetukset, mikä on sen ainoa tehtävä.
    """
    lahde = (SRC / "domain" / "schemas.py").read_text(encoding="utf-8")
    for kielletty in ("open(", "read_text", "write_text", "read_parquet"):
        assert kielletty not in lahde, kielletty
