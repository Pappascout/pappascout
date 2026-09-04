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
    """FACEIT-rajapinta palautti virheen tai ei vastannut (Story 3.1).

    **Tämä on verkkovirheiden oma tyyppi**, sama suku kuin :class:`ParseError`
    ja :class:`AggregateError`: kutsuja erottaa "rajapinta ei vastannut"
    -tilanteen "demo ei parsiutunut" -tilanteesta tyypistä eikä viestistä.
    Luokka oli olemassa jo Story 1.1:stä asti tyhjänä paikanvarauksena
    (ARCHITECTURE-SPINE, Consistency Conventions -> Virheet); Story 3.1 antaa
    sille sisällön eikä lisää rinnalle toista nimeä samalle asialle.

    Kolme kenttää, koska kutsujan on erotettava kolme eri jatkoa toisistaan
    **ilman viestin lukemista**:

    ``status_code``
        HTTP-tilakoodi, tai ``None`` jos vastausta ei saatu lainkaan
        (yhteysvirhe, aikakatkaisu, vastaus joka ei ollut JSONia).
        Story 3.4 päättää tästä, onko ottelun tila ``no_demo`` (404) vai
        ``download_failed`` (kaikki muu) -- viestin sisällöstä ei voi päättää
        mitään, koska viesti on ihmiselle.
    ``attempts``
        Montako kertaa kutsu tehtiin. ``1`` tarkoittaa, ettei uudelleenyritystä
        edes yritetty: 4xx (paitsi 429) ei korjaannu odottamalla.
    ``url``
        Osoite ilman avainta. Avain kulkee ``Authorization``-otsakkeessa eikä
        koskaan osoitteessa, joten tämän saa näyttää ja tallentaa.

    Viesti kertoo aina, mitä haettiin -- pelkkä tilakoodi ei ohjaa mihinkään.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        attempts: int = 1,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.attempts = attempts
        self.url = url


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
