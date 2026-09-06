"""Pappascout -- Pappaliigan CS2-vastustajascouting.

Putki lukee FACEIT-otteluista demot, parsii ne tauluiksi, luokittelee kierrokset
ja tuottaa suomenkielisen Markdown-raportin. Vaiheet ovat itsenäisiä
tiedosto-tulos-funktioita, ja **järjestyksen päättää käyttäjä ajamalla yhden
komennon kerrallaan**: vaiheita ketjuttavaa moduulia ei ole olemassa. Vaiheen
syöte on edellisen vaiheen kirjoittama tiedosto, ja jos sitä ei ole, vaihe
sanoo sen ja kertoo mikä komento sen kirjoittaa.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

__all__ = ["__version__"]

try:
    __version__ = _version("pappascout")
except PackageNotFoundError:  # pragma: no cover - vain ilman asennusta
    __version__ = "0.0.0+unknown"
