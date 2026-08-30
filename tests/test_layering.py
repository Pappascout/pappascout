"""Kerrossäännön testit.

Spinen riippuvuuskaavio on ``cli -> stages -> {domain, adapters, archive,
render}``, ``render -> domain`` ja ``adapters -> domain``. Kaikki sen säännöt
ovat nyt valvottavissa koneellisesti:

* ``domain`` ei tuo mitään muista pappascout-paketeista,
* ``archive`` ei riipu ``domain``ista -- se on putki, ei domain-mallien säilö,
* ``adapters`` ei tunne arkistoa, vaiheita eikä komentoriviä,
* ``render`` (Story 2.4) näkee vain ``domain``in: se ei saa koskea arkistoon,
  adaptereihin eikä vaiheisiin, jolloin "render ei laske mitään" on
  rakenteellinen lupaus eikä pelkkä tapa,
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
#:
#: ``render`` (Story 2.4) on esityskerros: se lukee ``domain``in raporttimallin
#: ja muotoilee sen Markdowniksi. Se ei saa koskea arkistoon, adaptereihin
#: eikä vaiheisiin -- muuten "render ei laske mitään" lakkaisi olemasta
#: rakenteellinen lupaus ja olisi pelkkä tapa. Nuoli on
#: ``stages -> render -> domain``.
FORBIDDEN = {
    "domain": {"archive", "adapters", "stages", "cli", "render"},
    "archive": {"domain", "adapters", "stages", "cli", "render"},
    "adapters": {"archive", "stages", "cli", "render"},
    "render": {"archive", "adapters", "stages", "cli"},
    "stages": {"cli"},
    "cli": {"adapters", "archive"},
}

#: Vain olemassa olevat paketit -- lista kasvaa itsestään, kun vaiheet tulevat.
EXISTING = sorted(p for p in FORBIDDEN if (SRC / p).is_dir())

#: Kaikki pappascout-alipaketit, myös vielä olemattomat. Suhteellinen tuonti
#: tunnistetaan vain näiden nimien perusteella.
PACKAGES = {
    "cli",
    "stages",
    "domain",
    "adapters",
    "archive",
    "render",
    "templates",
}


def _imported_packages(path: Path) -> set[str]:
    """Lue tiedosto ja kerää sen tuomat pappascout-alipaketit."""
    own_package = path.relative_to(SRC).parts[0] if path.parent != SRC else ""
    return _scan(path.read_text(encoding="utf-8"), own_package, str(path))


def _scan(source: str, own_package: str, filename: str = "<koe>") -> set[str]:
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
            top_package = node.module.split(".")[0]
            if node.level >= 2 and top_package in PACKAGES:
                found.add(top_package)
            elif node.level == 1 and own_package in PACKAGES:
                found.add(own_package)
        elif node.level >= 2:
            # from .. import archive
            for alias in node.names:
                if alias.name in PACKAGES:
                    found.add(alias.name)

    found.discard(own_package)
    return found


@pytest.mark.parametrize("package", EXISTING)
def test_package_respects_dependency_arrows(package: str) -> None:
    directory = SRC / package
    forbidden = FORBIDDEN[package]
    for path in directory.rglob("*.py"):
        violations = _imported_packages(path) & forbidden
        assert not violations, (
            f"{path.relative_to(SRC)} tuo kielletyn paketin: "
            f"{', '.join(sorted(violations))}"
        )


@pytest.mark.parametrize(
    "source_code,expected",
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
def test_import_forms_are_all_detected(source_code: str, expected: set) -> None:
    """Sääntöä ei saa kiertää suhteellisella tuonnilla.

    Lähde annetaan merkkijonona, jotta testi ei kirjoita mitään src-puuhun.
    """
    assert _scan(source_code, "domain") == expected


def test_absolute_imports_are_detected() -> None:
    """Nykyinen koodi tuo domainin ja archiven vain absoluuttisesti."""
    found_imports = _imported_packages(SRC / "archive" / "manifest.py")
    assert "archive" not in found_imports  # oma paketti ei ole riippuvuus
    assert "domain" not in found_imports


def test_domain_does_no_file_io_except_settings_loading() -> None:
    """``domain`` on puhdas: vain asetusten lataus koskee levyä.

    ``schemas`` sisältää pelisäännöt ja taulusopimukset eikä saa avata
    tiedostoja; ``models`` lataa asetukset, mikä on sen ainoa tehtävä.
    """
    source_code = (SRC / "domain" / "schemas.py").read_text(encoding="utf-8")
    for forbidden in ("open(", "read_text", "write_text", "read_parquet"):
        assert forbidden not in source_code, forbidden
