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
    "DownloadsAccessDenied",
]


class PappascoutError(Exception):
    """Pappascoutin virheiden kantaluokka.

    Viesti on aina suomeksi ja kertoo seuraavan toimenpiteen.

    **Neuvo on kentässä eikä pelkässä viestissä (Story 3.4, 2026-09-05).**
    Kaksi kertaa peräkkäin oikea ajo löysi saman kuvion: luokittelu oli oikein
    mutta neuvo väärä, koska neuvo tuli **otsikosta**, jonka alle virhe
    lajiteltiin. Otsikko "Epäonnistui -- aja komento uudelleen" olettaa
    ohimenevän häiriön, ja jokainen uusi vikaluokka peri sen oletuksen
    hiljaa: 403 (puuttuva käyttöoikeus) ja 400 (epämuodostunut tunniste)
    saivat molemmat neuvon, joka ei auta kumpaankaan.

    Kun neuvo on virheessä, uusi vikaluokka **ei voi periä väärää neuvoa**: se
    joko tuo omansa tai jää ilman, ja jälkimmäisen huomaa vartija
    (``stages.fetch._result``).

    Args:
        message: Suomenkielinen selitys siitä, mitä tapahtui.
        advice: Yksi lause siitä, mitä käyttäjän pitää tehdä seuraavaksi.
            ``None`` tarkoittaa "ei tiedossa" -- ei "aja uudelleen".
    """

    def __init__(self, message: str, *, advice: str | None = None) -> None:
        super().__init__(message)
        self.advice = advice


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
        advice: str | None = None,
    ) -> None:
        super().__init__(message, advice=advice)
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


class DownloadsAccessDenied(SettingsError):
    """FACEIT ei myöntänyt lupaa demojen lataukseen (401/403, Story 3.4).

    **Oma tyyppinsä, koska se on ainoa vika, joka ei ole yksikkökohtainen.**
    AD-9 sanoo, että yksikön ongelma muuttuu ``status``-kentäksi eikä keskeytä
    ajoa -- ja se pitää paikkansa jokaisesta viasta, joka koskee *yhtä demoa*:
    poistettu tallenne, katkennut yhteys, täysi levy. Puuttuva Downloads-scope
    ei ole sellainen. Se koskee **tunnistetta**, joten jokainen yksikkö
    epäonnistuu identtisesti, eikä yksikään voi onnistua.

    Ero on tärkeä eikä saa kadota: tämä **ei ole poikkeus** säännöstä "yhden
    demon epäonnistuminen ei keskeytä ajoa". Kyse ei ole yhden demon
    epäonnistumisesta vaan siitä, ettei yksikään voi onnistua -- ja sarjan
    jatkaminen tekisi 12 tuomittua signauskutsua, jotka kaikki kuluttavat
    kiintiötä ilman mahdollisuutta onnistua.

    Periytyy :class:`SettingsError`istä, koska korjaus on samassa paikassa kuin
    puuttuvalla avaimella: koneen oma ``.env``-tiedosto -- tai FACEITin
    hyväksyntä sille hakemukselle, jota tiedoston rivi edellyttää.
    """
