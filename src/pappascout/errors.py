"""Pappascoutin virhehierarkia.

Kaikki työkalun omat virheet periytyvät :class:`PappascoutError`-luokasta, joten
CLI voi napata yhden tyypin ja näyttää käyttäjälle suomenkielisen viestin sen
sijaan, että jäljitys valuisi ruudulle. Vaiheet muuntavat yksikkökohtaiset
ongelmat `status`-kentiksi (AD-9) ja nostavat poikkeuksen vain, jos yhtäkään
yksikköä ei voitu käsitellä tai asetukset puuttuvat.

Jokainen viesti kirjoitetaan suomeksi ja kertoo, mitä käyttäjän pitää tehdä
seuraavaksi.
"""

from __future__ import annotations

__all__ = [
    "PappascoutError",
    "ApiError",
    "DemoUnavailable",
    "ParseError",
    "SchemaError",
    "AggregateError",
    "LockError",
    "SettingsError",
]


class PappascoutError(Exception):
    """Pappascoutin virheiden kantaluokka.

    Viesti on aina suomeksi ja kertoo seuraavan toimenpiteen.
    """


class ApiError(PappascoutError):
    """FACEIT-rajapinta palautti virheen tai ei vastannut."""


class DemoUnavailable(PappascoutError):
    """Demoa ei ole saatavilla arkistosta eikä latauslähteestä."""


class ParseError(PappascoutError):
    """Demon parsinta epäonnistui."""


class SchemaError(PappascoutError):
    """Taulu ei vastaa jaettua skeemasopimusta (AD-2)."""


class AggregateError(PappascoutError):
    """Aggregoinnin otantatarkistus ei mennyt läpi (Story 2.3).

    Nostetaan silloin, kun jakauman ``n``-arvojen summa ei täsmää otannan
    ``m``:ään. Se ei ole muotoiluvirhe vaan merkki siitä, että kierros katosi
    matkalla: joko liitos ``(map_demo_id, round_no)`` jätti rivin ulkopuolelle
    tai jakaumasta puuttuu nollalokero. Kumpikin tuottaisi raportin, joka
    näyttää oikealta mutta väittää väärää otantaa -- siksi ajo pysähtyy.
    """


class LockError(PappascoutError):
    """Arkisto on toisen ajon lukitsema."""


class SettingsError(PappascoutError):
    """Asetukset tai avaimet puuttuvat tai ovat virheellisiä."""
