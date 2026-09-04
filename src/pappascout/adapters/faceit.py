"""FACEIT Data API -toteutus otteluportille (AD-8, Story 3.1).

**Tämä on ainoa moduuli, joka tekee HTTP-kutsuja.** Vaihe näkee vain
:class:`~pappascout.adapters.protocols.MatchSource`-portin, joten rajapinnan
osoitteen, otsakkeiden, sivutuksen ja uudelleenyrityksen muuttaminen ei kosketa
putkea. Sama jako kuin :mod:`pappascout.adapters.demo_parser`illa: portti on
:mod:`pappascout.adapters.protocols`issa, toteutus täällä.

Neljä sääntöä, jotka tämä moduuli pitää voimassa
------------------------------------------------

**Avain ei päädy mihinkään luettavaan.** Se kulkee ``Authorization``-otsakkeessa
eikä koskaan osoitteessa, joten välimuistitiedoston nimessä tai sisällössä ei
ole mitään avaimesta johdettua. Luokka ei lokita mitään, ja sen
:meth:`FaceitClient.__repr__` on kirjoitettu käsin -- oletusrepr tulostaisi
attribuutit, ja avain olisi niiden joukossa. Avain säilytetään
:class:`~pydantic.SecretStr`inä, joten sen näkeminen vaatii nimenomaisen
``get_secret_value()``-kutsun. **Osoitteen on oltava ``https``**, eikä
uudelleenohjauksia seurata: kumpikin lähettäisi otsakkeen paikkaan, jota tämä
moduuli ei valinnut.

**Uudelleenyritys vain sille, mikä voi korjaantua odottamalla.** 429 (rajoitus),
5xx (rajapinta rikki) ja yhteysvirhe (myös aikakatkaisu) yritetään uudelleen
kasvavalla viiveellä. **Muu 4xx ei.** Väärä id ei muutu oikeaksi ja väärä avain
ei muutu kelvolliseksi, joten odottaminen vain viivyttäisi virhettä, joka on jo
varma -- ja kuluttaisi kutsukiintiötä. Jos rajapinta kertoo odotusajan
``Retry-After``-otsakkeessa, **sitä kuunnellaan** eikä arvata: se on mittaus,
jota ei tarvitse kalibroida.

**Yhdellä porttikutsulla on aikabudjetti.** Yritysten lukumäärä ei ole katto
ajalle: sivutus kertoo yritykset sivujen määrällä, joten pelkkä yrityskatto
sallisi satoja pyyntöjä ja tunnin hiljaisuuden yhdestä ``get_matches``ista --
juuri sen, minkä ``MAX_FACEIT_RETRY_ATTEMPTS``in perustelu sanoo estävänsä.
Budjetti on sekunteja, koska sekunteja käyttäjä odottaa.

**Välimuisti on eriytetty kutsun lajin mukaan, ja se on mitattu päätös.**

``/championships/{id}/matches`` (ottelulista)
    **Ei välimuistiteta lainkaan** -- ei lueta eikä kirjoiteta.
``/matches/{id}`` (yhden ottelun tiedot)
    **Välimuistitetaan pysyvästi, mutta vain valmiista ottelusta**
    (:data:`CACHEABLE_MATCH_STATUSES`). Kesken pelattavana haettua ottelua ei
    kirjoiteta levylle.

Tämä **kumoaa speksin rajoitteen** "välimuisti on pelkkä HTTP-välimuisti, ei
ajantasaisuuspäättelyä". Rajoite oli tietoinen, mutta se kirjoitettiin **ennen
mittausta** ja nojasi oletukseen, että vastaukset ovat pysyviä. Mittaus
4.9.2026 osoitti oletuksen tosi toiselle päätepisteelle ja epätosi toiselle:

* **Ottelulista on yksi kutsu per ajo** -- divisioonan 66 ottelua mahtuu
  yhdelle sivulle ``page_size = 100``:lla -- ja se **muuttuu jatkuvasti**: 60
  ottelua 66:sta oli tilassa ``SCHEDULED``. Välimuisti säästäisi siellä yhden
  kutsun ja maksaisi oikeellisuuden. Se ei ole vaihtokauppa vaan pelkkä haitta.
* **Ottelun tiedot on jopa 66 kutsua** (``collect`` koko divisioonalle) ja
  **muuttumattomia** heti kun ottelu on pelattu. Siellä välimuisti on selvä
  hyöty. FACEITin raja on epävirallisesti noin 10 000 kutsua tunnissa.

Vaatimus, joka tämän ratkaisee, on Veetin oma ja parempi kuin mikään
vanhenemisaika: *"Kunhan emme hae duplikaatteja tai missaa selviä otteluita."*
**Vanhenemisaikaa ei siksi ole** -- ei kelloa, ei TTL:ää, ei asetusta.
Vähemmän liikkuvia osia kuin ajassa mitatussa säännössä, ja se sanoo suoraan
sen mitä tarkoittaa: muuttumaton vastaus säilytetään, muuttuvaa ei.

Muuten välimuisti on entisellään: yksi tiedosto per kutsu, ei manifestia, ei
vaikutusta muihin vaiheisiin. Hakemiston saa poistaa milloin tahansa, ja ainoa
seuraus on uusi kutsu. Sama koskee yksittäistä rikkoutunutta tiedostoa: se
ohitetaan kuin sitä ei olisi. **Levylle ei kirjoiteta vastausta, jota ei ole
ensin tarkistettu** -- muuten rikkinäinen 200 jäisi välimuistiin ja jokainen
seuraava ajo kaatuisi samaan virheeseen käymättä lainkaan verkossa.

Miksi hakemisto annetaan parametrina
------------------------------------

Adapteri ei tuo :mod:`pappascout.archive`ia (``tests/test_layering.py``): se ei
saa tietää, missä arkisto on tai miten sen polut rakentuvat. Kutsuja antaa
valmiin hakemiston, jonka se saa ``ArchivePaths.raw_faceit()``ilta. Sama sääntö
kuin AD-8:n demolatauksella -- adapteri ei kirjoita arkistoon vaan siihen
paikkaan, jonka sille kerrotaan.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets as _secrets
import socket
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache, wraps
from hashlib import sha256
from math import inf
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from pydantic import SecretStr
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from pappascout.adapters.protocols import Match, MatchTeam, RosterPlayer
from pappascout.domain.models import (
    MAX_FACEIT_PAGE_SIZE,
    MAX_FACEIT_RETRY_ATTEMPTS,
    FaceitSettings,
    Settings,
)
from pappascout.errors import ApiError, PappascoutError, SettingsError

__all__ = [
    "FaceitClient",
    "FACEIT_DATA_API_BASE",
    "MAX_PAGES",
    "DEFAULT_CALL_BUDGET_SECONDS",
    "CACHEABLE_MATCH_STATUSES",
]

#: FACEIT Data API:n juuri. **Vakio eikä asetus**: se ei ole säädettävä arvo
#: vaan se, mitä vastaan tämä moduuli on kirjoitettu -- polut, otsake ja
#: vastausten muoto ovat sen sopimusta. Testi vaihtaa sen parametrilla, jotta
#: mikään testi ei voi vahingossa osua oikeaan osoitteeseen.
FACEIT_DATA_API_BASE = "https://open.faceit.com/data/v4"

#: Sivujen yläraja yhdessä haussa. Vartija eikä asetus.
#:
#: Sivutus päättyy siihen, että rajapinta palauttaa tyhjän tai vajaan sivun.
#: Jos se ei koskaan tekisi niin -- vika rajapinnassa tai väärin tulkittu
#: ``offset`` -- silmukka jatkuisi ikuisesti ja kuluttaisi kutsukiintiötä
#: hiljaa. 200 sivua on täydellä sivukoolla 20 000 ottelua, eli
#: monikymmenkertaisesti sen, mitä yksi divisioonakausi voi sisältää.
MAX_PAGES = 200

#: Yhden porttikutsun aikabudjetti sekunteina, kun kutsuja ei anna omaansa.
#:
#: **Vartija samassa mielessä kuin :data:`MAX_PAGES`**, ei neutraali oletus.
#: Yritysten lukumäärä ei rajaa aikaa: ``MAX_PAGES`` sivua kertaa
#: ``retry_attempts`` yritystä on satoja pyyntöjä, ja kasvavalla viiveellä
#: tunnin hiljaisuus. Katto on siis oltava sekunneissa.
DEFAULT_CALL_BUDGET_SECONDS = 300.0

#: Merkit, jotka kelpaavat välimuistitiedoston nimeen sellaisenaan.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: Rajoitustilakoodi. Se ja 5xx ovat ne, jotka voivat korjaantua odottamalla.
_RATE_LIMIT_STATUS = 429

#: 2xx-koodit, joilla **ei ole runkoa** (RFC 9110). Onnistuneita mutta tyhjiä.
_NO_CONTENT_STATUSES = frozenset({204, 205})

#: Ottelun tilat, joissa sen tiedot **saa** välimuistittaa pysyvästi.
#:
#: Mitattu aineisto (4.9.2026) sisältää ``SCHEDULED`` ja ``FINISHED``;
#: FACEITilla on lisäksi ``ONGOING`` ja ``CANCELLED``.
#:
#: **``FINISHED`` ja vain se, eikä myös ``CANCELLED``** -- vaikka peruuttu
#: ottelu näyttää yhtä lopulliselta. Valinta on epäsymmetrisen hinnan takia,
#: ei siisteyden:
#:
#: * ``FINISHED`` on **tosiasia menneisyydestä**. Ottelu on pelattu, demo on
#:   olemassa, eivätkä rosteri tai karttavalinnat voi enää muuttua.
#: * ``CANCELLED`` on **järjestäjän päätös**, ja järjestäjä voi perua sen.
#:   Siirretty ottelu pelataan uudella ajankohdalla samalla ``match_id``:llä,
#:   ja pysyvästi välimuistitettu ``CANCELLED`` piilottaisi sen ikuisesti.
#:
#: Väärin päin tehdyn valinnan hinta ratkaisee: ``CANCELLED``in
#: välimuistittaminen säästäisi **yhden kutsun**, ja epäonnistuessaan se
#: hukkaisi **pelatun ottelun demon pysyvästi** (FACEIT säilyttää demot noin
#: 30 päivää). Se on täsmälleen se, mitä Veetin vaatimus "älä missaa selviä
#: otteluita" kieltää.
CACHEABLE_MATCH_STATUSES = frozenset({"FINISHED"})

#: Ohje, joka kuuluu jokaiseen virheeseen, jonka syy voi olla levyllä.
#:
#: Vastaus tarkistetaan ennen kirjoitusta, joten rikkinäinen vastaus ei enää
#: jää välimuistiin -- mutta vanhemman version kirjoittama tiedosto voi olla
#: siellä yhä, eikä käyttäjä voi tietää sitä ilman ohjetta.
_CACHE_ADVICE = (
    "Jos virhe toistuu, tyhjennä välimuisti (arkiston hakemisto raw/faceit/) "
    "-- sen saa poistaa milloin tahansa, ja ainoa seuraus on uusi kutsu."
)


class _Retryable(Exception):
    """Ohimenevä vika: kannattaa yrittää uudelleen kasvavalla viiveellä."""

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        #: Rajapinnan itsensä kertoma odotusaika sekunteina, tai ``None``.
        self.retry_after = retry_after


class _Permanent(Exception):
    """Vika, joka ei korjaannu odottamalla -- uudelleenyritystä ei tehdä."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class _Exhausted(Exception):
    """Kutsun aikabudjetti loppui kesken."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class _Invalid(Exception):
    """Vastaus tuli perille mutta ei ole sitä, mitä portti lupaa."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Fetched:
    """Vastaus ja se, miten se saatiin.

    Metatiedot kulkevat mukana, koska :class:`~pappascout.errors.ApiError`in
    ``attempts`` ja ``status_code`` ovat Story 3.4:n ``no_demo`` /
    ``download_failed`` -päätöksen syöte. Ilman tätä muunnos- ja
    rakennevirheet raportoisivat ``attempts=1`` ja ``status_code=None`` -- eli
    "vastausta ei saatu lainkaan" -- vaikka vastaus oli 200 neljän yrityksen
    jälkeen.

    ``attempts = 0`` ja ``status_code = None`` yhdessä tarkoittavat
    välimuistiosumaa: verkkoon ei menty kertaakaan.
    """

    payload: Mapping[str, Any]
    attempts: int
    status_code: int | None
    url: str
    from_cache: bool


class _Budget:
    """Yhden porttikutsun aikabudjetti."""

    def __init__(self, seconds: float, clock: Callable[[], float]) -> None:
        self.seconds = float(seconds)
        self._clock = clock
        self._started = clock()

    def elapsed(self) -> float:
        return self._clock() - self._started

    def remaining(self) -> float:
        return self.seconds - self.elapsed()

    def expired(self) -> bool:
        return self.remaining() <= 0.0


def _only_api_errors(method: Any) -> Any:
    """Varmista, että portin metodista nousee vain :class:`ApiError`.

    Portin ainoa lupaus on ``ApiError``
    (:class:`~pappascout.adapters.protocols.MatchSource`). Injektoitu kuljetus,
    rikkinäinen JSON-kirjasto tai mikä tahansa muu odottamaton poikkeus
    vuotaisi muuten vaiheeseen asti tyyppinä, jota kutsuja ei osaa napata --
    ja AD-9:n mukaan yksikön virhe on ``status``, ei jäljitys ruudulle.

    Muut :class:`~pappascout.errors.PappascoutError`it päästetään läpi
    sellaisenaan: ne ovat jo suomenkielisiä ja kertovat oman syynsä.
    ``BaseException`` (Ctrl-C, ``SystemExit``) ei kuulu tähän lainkaan: se ei
    ole rajapinnan vika eikä sitä saa niellä.
    """

    @wraps(method)
    def wrapper(self: "FaceitClient", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except PappascoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - portin lupaus, ks. docstring
            raise ApiError(
                f"FACEIT-kutsu päättyi odottamattomaan virheeseen "
                f"({type(exc).__name__}).\n"
                "Kyseessä on työkalun oma vika eikä rajapinnan.\n"
                f"{_CACHE_ADVICE}"
            ) from exc

    return wrapper


class FaceitClient:
    """FACEIT Data API:n asiakas: avain, uudelleenyritys, välimuisti, sivutus.

    Toteuttaa :class:`~pappascout.adapters.protocols.MatchSource`-portin.

    **Kaikki FACEIT-kutsut kulkevat tämän luokan läpi** (AD-8). Se on koko
    Story 3.1:n olemassaolon syy: yksi paikka, jossa avain luetaan, yksi
    paikka, jossa uudelleenyritys määritellään, ja yksi paikka, jossa
    vastaukset välimuistitetaan.

    Args:
        api_key: FACEIT Data API -avain. **Ei luettu täältä vaan annettu**:
            avaimen ainoa lukutapa on
            :meth:`~pappascout.domain.models.Settings.require_faceit_api_key`,
            ja :meth:`from_settings` on se paikka, joka sitä kutsuu.
        cache_dir: Hakemisto vastausten välimuistille (``raw/faceit/``).
            Luodaan tarvittaessa. Adapteri ei tunne arkistoa, joten polku
            annetaan valmiina.
        base_url: Rajapinnan juuri. Oletus :data:`FACEIT_DATA_API_BASE`.
            **On oltava ``https``**, koska avain kulkee otsakkeessa.
        retry_attempts: Yritysten kokonaismäärä. **Oletus 1** eli ei
            uudelleenyritystä -- sama linjaus kuin
            :class:`~pappascout.adapters.demo_parser.Demoparser2Adapter`illa:
            adapteri ei lue asetuksia, ja neutraali oletus on se, joka ei tee
            mitään yllättävää. Tuotannossa arvo tulee ``[faceit]``-osiosta.
        retry_initial_delay_seconds: Ensimmäinen odotus sekunteina. Viive
            kaksinkertaistuu joka kierroksella. Oletus 0.0 samasta syystä.
        retry_max_delay_seconds: Yhden odotuksen katto sekunteina, tai
            ``None`` = ei kattoa. **Oletus on ``None`` eikä nolla**: nolla
            olisi katto, joka leikkaa jokaisen odotuksen pois, jolloin
            aloitusviiveen antaminen ilman kattoa ei tekisi mitään -- vika,
            joka näyttäisi toimivalta. Aloitusviivettä pienempi arvo on
            **virhe eikä hiljainen clamppaus**.
        retry_jitter_share: Satunnaisheiton osuus odotuksesta (0.0-1.0).
            Odotus on ``viive * (1 + share * satunnaisluku)``. Ilman heittoa
            kaksi rinnakkaista ajoa törmäisi rajoitukseen samalla sekunnilla
            uudelleen ja uudelleen. Oletus 0.0, jotta adapterin oletuskäytös on
            deterministinen.
        timeout_seconds: Yhden HTTP-kutsun aikakatkaisu sekunteina.
        page_size: Montako riviä yhdessä sivussa pyydetään. Enintään
            :data:`~pappascout.domain.models.MAX_FACEIT_PAGE_SIZE`.
        call_budget_seconds: Yhden porttikutsun aikabudjetti sekunteina --
            koko sivutus ja kaikki uudelleenyritykset yhteensä. Oletus
            :data:`DEFAULT_CALL_BUDGET_SECONDS`.
        session: Kuljetus, jolta odotetaan ``requests.Session``in
            ``get(url, headers=..., params=..., timeout=...,
            allow_redirects=...)``. Oletuksena luodaan oma, joka suljetaan
            :meth:`close`ssa. **Testit antavat tämän** -- se on se sauma, jonka
            ansiosta koko testisarja ajautuu offline.
        sleep: Odotusfunktio uudelleenyritysten välissä. Oletuksena
            ``time.sleep``. Testi antaa tallentavan funktion, jolloin viiveen
            kasvaminen on mitattavissa ilman että testi odottaa sekuntiakaan.
        clock: Monotoninen kello aikabudjetille. Oletuksena
            ``time.monotonic``.
        random_source: Satunnaislähde heitolle; palauttaa arvon välillä
            ``[0, 1)``. Oletuksena ``random.random``.

    Raises:
        ~pappascout.errors.SettingsError: Jos jokin arvo on rajojensa
            ulkopuolella tai osoite ei ole ``https``. **Ei hiljaista
            clamppausta**: sama periaate kuin asetusosioilla, joissa kelvoton
            arvo on virhe eikä ohitus. Clampattu ``page_size = 1000`` tuottaisi
            juuri sen 400-virheen, jonka rajan perustelu sanoo näyttävän
            verkkovialta.
        ~pappascout.errors.ApiError: Jokaisesta verkko-, tilakoodi- ja
            muotovirheestä. Viesti on suomeksi ja kertoo, mitä haettiin.
    """

    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        *,
        base_url: str = FACEIT_DATA_API_BASE,
        retry_attempts: int = 1,
        retry_initial_delay_seconds: float = 0.0,
        retry_max_delay_seconds: float | None = None,
        retry_jitter_share: float = 0.0,
        timeout_seconds: float = 30.0,
        page_size: int = 100,
        call_budget_seconds: float = DEFAULT_CALL_BUDGET_SECONDS,
        session: Any | None = None,
        sleep: Callable[[float], None] | None = None,
        clock: Callable[[], float] | None = None,
        random_source: Callable[[], float] | None = None,
    ) -> None:
        self._api_key = SecretStr(api_key)
        self.cache_dir = Path(cache_dir)
        self.base_url = _check_base_url(base_url)
        self.retry_attempts = _check_int(
            retry_attempts, "retry_attempts", 1, MAX_FACEIT_RETRY_ATTEMPTS
        )
        self.retry_initial_delay_seconds = _check_float(
            retry_initial_delay_seconds, "retry_initial_delay_seconds", 0.0
        )
        self.retry_max_delay_seconds = (
            None
            if retry_max_delay_seconds is None
            else _check_float(
                retry_max_delay_seconds,
                "retry_max_delay_seconds",
                self.retry_initial_delay_seconds,
            )
        )
        self.retry_jitter_share = _check_float(
            retry_jitter_share, "retry_jitter_share", 0.0, 1.0
        )
        self.timeout_seconds = _check_float(timeout_seconds, "timeout_seconds", 0.0)
        self.page_size = _check_int(page_size, "page_size", 1, MAX_FACEIT_PAGE_SIZE)
        self.call_budget_seconds = _check_float(
            call_budget_seconds, "call_budget_seconds", 0.0
        )
        self._owns_session = session is None
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep if sleep is not None else time.sleep
        self._clock = clock if clock is not None else time.monotonic
        self._random = random_source if random_source is not None else random.random
        #: Montako kertaa verkkoon todella mentiin. Välimuistiosuma ei kasvata
        #: tätä, joten "toinen ajo ei mene verkkoon" on mitattavissa eikä
        #: pääteltävissä.
        self.requests_made = 0
        #: Montako kertaa vastaus saatiin välimuistista.
        self.cache_hits = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        cache_dir: Path,
        **kwargs: Any,
    ) -> "FaceitClient":
        """Rakenna asiakas asetuksista ja koneen omasta avaintiedostosta.

        **Tämä on ainoa paikka, joka lukee avaimen.** Se kutsuu
        :meth:`~pappascout.domain.models.Settings.require_faceit_api_key`iä,
        joka nostaa suomenkielisen virheen ja kertoo tiedoston polun ja
        tarvittavan rivin, jos avainta ei ole. Toista lukutapaa ei ole: tämä
        moduuli ei koske ``os.environ``iin lainkaan, joten pelkkä
        ympäristömuuttuja ei kelpaa avaimeksi -- avain on luettava sitä kautta,
        jonka ``Settings`` tuntee.

        Metodi ottaa koko :class:`~pappascout.domain.models.Settings`in eikä
        pelkkää ``[faceit]``-osiota, koska avain ei ole missään osiossa: se on
        ``Settings``in oma kenttä, ja osiointi (AD-3) koskee asetustiedostoa
        eikä avaimia.

        Args:
            settings: Ladatut asetukset.
            cache_dir: Välimuistihakemisto, tavallisesti
                ``ArchivePaths.raw_faceit()``.
            **kwargs: Kuljetuksen saumat (``session``, ``sleep``, ``clock``,
                ``random_source``). **Asetusarvoja ei voi ohittaa tätä
                kautta** -- ne tulevat aina ``[faceit]``-osiosta, ja
                päällekkäinen nimi nostaa ``TypeError``in.

        Raises:
            ~pappascout.errors.SettingsError: Jos avain puuttuu tai on tyhjä.
        """
        api_key = settings.require_faceit_api_key()
        faceit: FaceitSettings = settings.faceit
        return cls(
            api_key=api_key,
            cache_dir=cache_dir,
            retry_attempts=faceit.retry_attempts,
            retry_initial_delay_seconds=faceit.retry_initial_delay_seconds,
            retry_max_delay_seconds=faceit.retry_max_delay_seconds,
            retry_jitter_share=faceit.retry_jitter_share,
            timeout_seconds=faceit.timeout_seconds,
            page_size=faceit.page_size,
            call_budget_seconds=faceit.call_budget_seconds,
            **kwargs,
        )

    def __repr__(self) -> str:
        """Esitys **ilman avainta**.

        Kirjoitettu käsin tarkoituksella: oletusrepr tulostaisi attribuutit, ja
        avain olisi niiden joukossa -- jäljityksessä, virheilmoituksessa ja
        debuggerissa.
        """
        return (
            f"FaceitClient(base_url={self.base_url!r}, "
            f"cache_dir={str(self.cache_dir)!r})"
        )

    def close(self) -> None:
        """Sulje oma kuljetus. **Annettua kuljetusta ei suljeta.**

        Injektoitu ``session`` on kutsujan omaisuutta: sen sulkeminen katkaisisi
        yhteydet, joita tämä luokka ei avannut.
        """
        if self._owns_session:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "FaceitClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- Portti --------------------------------------------------------------

    @_only_api_errors
    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        """Ks. portin dokumentaatio.

        Hakee kaikki sivut ja palauttaa yhden listan: sivutus on kuljetuksen
        yksityiskohta eikä vaiheen tietoa. Koko haulla on yksi aikabudjetti.
        """
        path = f"/championships/{competition_id}/matches"
        budget = _Budget(self.call_budget_seconds, self._clock)
        items: list[Mapping[str, Any]] = []
        #: Nähdyt ``match_id``:t. Sivutus nojaa ``offset``iin, ja rajapinta voi
        #: palauttaa saman rivin kahdella sivulla, jos lista muuttuu haun
        #: aikana -- juuri niin kuin kesken kauden käy. Ilman deduplikointia
        #: sama ottelu laskettaisiin kahdesti.
        seen: set[str] = set()
        offset = 0
        last: _Fetched | None = None

        for page_no in range(1, MAX_PAGES + 1):
            if budget.expired():
                raise self._budget_error(
                    f"championshipin {competition_id} otteluita", budget, last
                )
            last = self._get(
                path,
                {"type": "all", "offset": offset, "limit": self.page_size},
                what=(
                    f"championshipin {competition_id} otteluita "
                    f"(sivu {page_no}, offset {offset})"
                ),
                validate=_check_match_list,
                # **Ottelulistaa ei välimuistiteta.** Se on yksi kutsu per ajo
                # ja se muuttuu jatkuvasti (mitattu 4.9.2026: 60 ottelua 66:sta
                # tilassa SCHEDULED). Välimuisti säästäisi yhden kutsun ja
                # maksaisi oikeellisuuden -- ks. moduulidocstring.
                cache_when=None,
                budget=budget,
            )
            page = last.payload["items"]
            for entry in page:
                match_id = _text(entry.get("match_id"))
                if match_id in seen:
                    continue
                seen.add(match_id)
                items.append(entry)
            # **Todellinen sivun pituus, ei pyydetty.** Jos rajapinta palauttaa
            # enemmän kuin pyydettiin, ``offset += page_size`` hyppäisi rivien
            # yli; jos vähemmän, lista on lopussa. Jokainen rivi on tarkistettu
            # (:func:`_check_match_list`), joten suodatettua ja suodattamatonta
            # pituutta ei ole -- niitä oli ennen kaksi, ja ne olivat eri luvut
            # samasta sivusta.
            if len(page) < self.page_size:
                break
            offset += len(page)
        else:
            raise ApiError(
                f"FACEIT palautti championshipille {competition_id} yli "
                f"{MAX_PAGES} sivua otteluita eikä sivutus loppunut.\n"
                "Haku keskeytettiin, jotta se ei kuluttaisi kutsukiintiötä "
                "loputtomiin. Kyseessä on rajapinnan vika.",
                url=self._url(path),
                attempts=last.attempts if last else 1,
                status_code=last.status_code if last else None,
            )
        return tuple(_to_match(item) for item in items)

    @_only_api_errors
    def get_match(self, match_id: str) -> Match:
        """Ks. portin dokumentaatio."""
        fetched = self._get(
            f"/matches/{match_id}",
            None,
            what=f"ottelun {match_id} tietoja",
            validate=_check_match,
            # Pelatun ottelun tiedot eivät enää muutu, ja niitä haetaan jopa
            # 66 kertaa per ajo -- siellä välimuisti on selvä hyöty.
            # Keskeneräistä ottelua ei kirjoiteta levylle.
            cache_when=_is_cacheable_match,
        )
        return _to_match(fetched.payload)

    # -- Kuljetus ------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _budget_error(
        self, what: str, budget: _Budget, last: _Fetched | None
    ) -> ApiError:
        return ApiError(
            f"FACEIT-haku ei valmistunut aikabudjetissa kun haettiin {what}.\n"
            f"Budjetti oli {budget.seconds:g} s ja kului "
            f"{budget.elapsed():.1f} s.\n"
            "Rajapinta on hidas tai rajoittaa kutsuja. Odota hetki ja aja "
            "komento uudelleen, tai kasvata asetusta "
            "[faceit].call_budget_seconds.",
            url=last.url if last else None,
            attempts=last.attempts if last else 1,
            status_code=last.status_code if last else None,
        )

    def _get(
        self,
        path: str,
        params: Mapping[str, Any] | None,
        *,
        what: str,
        validate: Callable[[Mapping[str, Any]], None],
        cache_when: Callable[[Mapping[str, Any]], bool] | None = None,
        budget: _Budget | None = None,
    ) -> _Fetched:
        """Hae yksi vastaus: välimuistista tai verkosta, ja välimuistiin.

        Args:
            path: Polku rajapinnan juuresta, esim. ``/matches/<id>``.
            params: Kyselyparametrit tai ``None``.
            what: Mitä haettiin, suomeksi. Päätyy virheilmoitukseen --
                pelkkä tilakoodi ei ohjaa mihinkään.
            validate: Rakennetarkistus, joka nostaa :class:`_Invalid`in.
                Ajetaan sekä ennen välimuistiin kirjoitusta että
                välimuistista luettaessa.
            cache_when: Ehto, jonka täyttävä vastaus **kirjoitetaan** levylle
                ja jonka täyttävä tiedosto **luetaan** levyltä.
                **``None`` = tätä kutsua ei välimuistiteta lainkaan**: ei
                lueta eikä kirjoiteta. Ottelulista on juuri sellainen, ja
                silloin koko välimuistihaara ohitetaan -- ei kuollutta koodia,
                joka näyttäisi kuin välimuisti olisi käytössä.

                **Ehtoa sovelletaan molempiin suuntiin, ja se on korjaus
                todistettuun virheeseen.** Ensin lukupolku jätettiin
                ehdottomaksi perustelulla "jos tiedosto on olemassa, se on
                aikanaan kirjoitettu vastauksesta, joka läpäisi tämän ehdon".
                Live-tarkistus 4.9.2026 osoitti premissin vääräksi samana
                päivänä: jaetussa arkistossa oli edellisen version kirjoittama
                tiedosto, joka tarjoili ``SCHEDULED``-ottelua levyltä
                **ikuisesti ja täysin hiljaa** -- eli täsmälleen se vikaluokka,
                jonka tämä tarina muuten korjaa.

                Symmetrinen ehto tekee välimuistista **itsekorjaavan**:
                invariantti ei enää riipu siitä, että jokainen aiempi ja tuleva
                kirjoituspolku oli oikein, vaan siitä mitä tiedostossa
                lukee. Yksi bugi tai yksi vanha versio ei myrkytä välimuistia
                pysyvästi, eikä kenenkään tarvitse muistaa siivota hakemistoa
                käsin. Hinta on nolla: ehto on jo olemassa eikä tämä lisää
                uutta sisältöriippuvuutta -- se soveltaa olemassa olevaa.
            budget: Aikabudjetti; ``None`` = oma budjetti tälle kutsulle.
        """
        url = self._url(path)
        cache_path = (
            self._cache_path(path, params) if cache_when is not None else None
        )

        if cache_path is not None:
            cached = _read_cache(cache_path)
            if cached is not None:
                if _cache_entry_is_usable(cached, validate, cache_when):
                    self.cache_hits += 1
                    return _Fetched(cached, 0, None, url, from_cache=True)
                # Kelpaamaton tiedosto käyttäytyy kuin sitä ei olisi: se
                # poistetaan ja vastaus haetaan verkosta. **Yksi polku
                # kahdelle syylle** (rikkinäinen rakenne, kelpaamaton
                # sisältö), koska seuraus on molemmissa sama eikä kutsujan
                # tarvitse tietää kumpi se oli.
                try:
                    cache_path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - riippuu levystä
                    pass

        payload, attempts, status = self._fetch(
            url,
            params,
            what=what,
            budget=budget or _Budget(self.call_budget_seconds, self._clock),
            cache_advice=cache_path is not None,
        )
        try:
            validate(payload)
        except _Invalid as exc:
            # Välimuistiohje vain silloin kun tämä kutsu välimuistitetaan.
            # Ottelulistalle se olisi väärä neuvo: siellä ei ole mitään
            # tyhjennettävää, eikä virheilmoitus saa ohjata väärään paikkaan.
            advice = f"\n{_CACHE_ADVICE}" if cache_path is not None else ""
            raise ApiError(
                f"FACEIT palautti vastauksen, jota ei tunnisteta, kun haettiin "
                f"{what}: {exc.reason}\n"
                "Vastausta EI kirjoitettu välimuistiin, joten seuraava ajo "
                "yrittää uudelleen.\n"
                "Tarkista, että tunniste on oikea (settings.toml, "
                f"[league].championship_ids).{advice}",
                url=url,
                attempts=attempts,
                status_code=status,
            ) from exc
        if cache_path is not None and cache_when(payload):
            _write_cache(cache_path, payload)
        return _Fetched(payload, attempts, status, url, from_cache=False)

    def _fetch(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        *,
        what: str,
        budget: _Budget,
        cache_advice: bool = False,
    ) -> tuple[Mapping[str, Any], int, int | None]:
        """Tee kutsu verkkoon ja yritä uudelleen, jos vika voi korjaantua.

        ``cache_advice`` kertoo, välimuistitetaanko tämä kutsu: pysyvän vian
        viestiin kuuluu ohje välimuistin tyhjentämisestä vain silloin, kun
        välimuistissa voi olla jotain. Ottelulistalle sama ohje osoittaisi
        paikkaan, jossa ei ole mitään.
        """
        attempts = 0
        #: Viimeisin nähty tilakoodi listassa, jotta sulkeumat voivat kirjoittaa
        #: siihen. Se päätyy ``ApiError.status_code``iin myös silloin, kun
        #: lopullinen syy on budjetti eikä vastaus.
        status_seen: list[int | None] = [None]

        def attempt() -> Mapping[str, Any]:
            nonlocal attempts
            if budget.expired():
                raise _Exhausted(
                    "aikabudjetti loppui ennen seuraavaa yritystä",
                    status_code=status_seen[0],
                )
            attempts += 1
            self.requests_made += 1
            try:
                payload, status = self._single_request(url, params)
            except (_Retryable, _Permanent) as exc:
                # Tilakoodi kirjataan myös epäonnistuneesta yrityksestä.
                # Ilman tätä budjettiin päättynyt haku raportoisi
                # ``status_code=None`` eli "vastausta ei saatu lainkaan",
                # vaikka jokainen yritys sai vastauksen 503.
                if exc.status_code is not None:
                    status_seen[0] = exc.status_code
                raise
            status_seen[0] = status
            return payload

        base_wait = wait_exponential(
            multiplier=self.retry_initial_delay_seconds,
            max=(
                inf
                if self.retry_max_delay_seconds is None
                else self.retry_max_delay_seconds
            ),
        )

        def wait(retry_state: Any) -> float:
            """Odotus: rajapinnan oma ``Retry-After`` ennen kasvavaa viivettä.

            Heitto lisätään päälle (``viive * share * satunnaisluku``), jotta
            kaksi rinnakkaista ajoa ei törmäisi rajoitukseen samalla sekunnilla
            uudelleen ja uudelleen.
            """
            exc = (
                retry_state.outcome.exception()
                if retry_state.outcome is not None
                else None
            )
            hinted = getattr(exc, "retry_after", None)
            if hinted is not None:
                status_seen[0] = getattr(exc, "status_code", status_seen[0])
                delay = float(hinted)
            else:
                delay = float(base_wait(retry_state))
            if self.retry_jitter_share:
                delay += delay * self.retry_jitter_share * self._random()
            return delay

        def sleep(delay: float) -> None:
            # Budjetti tarkistetaan ENNEN odotusta eikä sen jälkeen: odotus,
            # joka veisi budjetin yli, on hiljaisuutta ilman mahdollisuutta
            # onnistua. Erityisesti ``Retry-After`` voi olla minuutteja.
            if delay > budget.remaining():
                raise _Exhausted(
                    f"seuraava odotus olisi {delay:.1f} s mutta budjetista on "
                    f"jäljellä {max(budget.remaining(), 0.0):.1f} s",
                    status_code=status_seen[0],
                )
            self._sleep(delay)

        retrying = Retrying(
            stop=stop_after_attempt(self.retry_attempts),
            wait=wait,
            retry=retry_if_exception_type(_Retryable),
            reraise=True,
            sleep=sleep,
        )

        try:
            payload = retrying(attempt)
        except _Retryable as exc:
            raise ApiError(
                f"FACEIT ei vastannut kun haettiin {what}: {exc.reason}\n"
                f"Yritettiin {attempts} kertaa kasvavalla viiveellä.\n"
                "Kyseessä on ohimenevä vika (rajoitus tai rajapinnan häiriö). "
                "Odota hetki ja aja komento uudelleen.",
                status_code=exc.status_code,
                attempts=attempts,
                url=url,
            ) from exc
        except _Permanent as exc:
            advice = f"\n{_CACHE_ADVICE}" if cache_advice else ""
            raise ApiError(
                f"FACEIT hylkäsi kutsun kun haettiin {what}: {exc.reason}\n"
                "Uudelleenyritystä ei tehty, koska vika ei korjaannu "
                f"odottamalla.{advice}",
                status_code=exc.status_code,
                attempts=attempts,
                url=url,
            ) from exc
        except _Exhausted as exc:
            raise ApiError(
                f"FACEIT-haku ei valmistunut aikabudjetissa kun haettiin "
                f"{what}: {exc.reason}\n"
                f"Budjetti oli {budget.seconds:g} s ja yrityksiä ehti "
                f"{attempts}.\n"
                "Rajapinta on hidas tai rajoittaa kutsuja. Odota hetki ja aja "
                "komento uudelleen, tai kasvata asetusta "
                "[faceit].call_budget_seconds.",
                status_code=exc.status_code,
                attempts=attempts,
                url=url,
            ) from exc
        return payload, attempts, status_seen[0]

    def _single_request(
        self, url: str, params: Mapping[str, Any] | None
    ) -> tuple[Mapping[str, Any], int]:
        """Yksi HTTP-kutsu. Nostaa :class:`_Retryable`in tai :class:`_Permanent`in.

        **Avain on täällä ja vain täällä.** Se rakennetaan otsakkeeseen
        kutsuhetkellä eikä säilötä valmiiksi muotoiltuna, jottei sitä ole
        olemassa yhtään pidempään kuin on pakko.

        ``allow_redirects=False``: uudelleenohjaus lähettäisi
        ``Authorization``-otsakkeen osoitteeseen, jota tämä moduuli ei
        valinnut. 3xx päätyy siksi tilakoodivirheeseen kuten mikä tahansa muu
        odottamaton vastaus.
        """
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Accept": "application/json",
        }
        try:
            response = self._session.get(
                url,
                headers=headers,
                params=dict(params) if params else None,
                timeout=self.timeout_seconds,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            # Aikakatkaisu ja yhteysvirhe ovat ohimeneviä. Poikkeuksen TYYPPI
            # eikä sen viesti kertoo syyn: viesti sisältää osoitteen ja
            # kirjaston oman englannin, eikä kumpikaan ohjaa käyttäjää.
            raise _Retryable(f"yhteys ei onnistunut ({type(exc).__name__})") from exc
        except Exception as exc:  # noqa: BLE001 - kuljetus on injektoitavissa
            # Muu kuin requestsin oma poikkeus tulee kuljetuksesta, jonka
            # kutsuja antoi. Se ei ole ohimenevä vika, joten sitä ei yritetä
            # uudelleen -- mutta se ei myöskään saa vuotaa portin läpi.
            raise _Permanent(
                f"kuljetus nosti poikkeuksen ({type(exc).__name__})"
            ) from exc

        status = int(getattr(response, "status_code", 0))
        retry_after = _retry_after_seconds(getattr(response, "headers", None))
        if status == _RATE_LIMIT_STATUS:
            raise _Retryable(
                "rajapinta rajoitti kutsuja (429)",
                status_code=status,
                retry_after=retry_after,
            )
        if 500 <= status < 600:
            raise _Retryable(
                f"rajapinta palautti palvelinvirheen ({status})",
                status_code=status,
                retry_after=retry_after,
            )
        if not 200 <= status < 300:
            # **Kaikki muu kuin 2xx, ei vain >= 400.** 3xx (uudelleenohjaus,
            # jota ei seurata) ei ole JSONia, ja ilman tätä haaraa se päätyisi
            # virheeseen "vastaus ei ollut JSONia" -- joka kertoo oireesta eikä
            # syystä. Uudelleenohjaus on eri vika kuin rikkinäinen runko.
            raise _Permanent(f"tilakoodi {status}", status_code=status)
        if status in _NO_CONTENT_STATUSES:
            # 204 ja 205 **ovat** 2xx, joten edellinen haara ei kata niitä --
            # mutta niillä ei ole runkoa lainkaan, ja JSON-jäsennys sanoisi
            # "vastaus ei ollut JSONia" tyhjästä vastauksesta. Se on totta
            # muttei ohjaa mihinkään: syy on se, ettei rajapinnalla ollut
            # mitään annettavaa.
            raise _Permanent(
                f"rajapinta palautti tyhjän vastauksen (tilakoodi {status})",
                status_code=status,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            # Ei sivua tulosteeseen: HTML-virhesivu on kilotavuja, ja se
            # peittäisi virheilmoituksen -- eikä sen sisällössä ole mitään,
            # mikä ohjaisi käyttäjää. Pituus ja tyyppi kertovat sen, mikä
            # tässä on olennaista: vastaus ei ollut JSONia.
            length = len(getattr(response, "text", "") or "")
            content_type = "tuntematon"
            headers_in = getattr(response, "headers", None)
            if isinstance(headers_in, Mapping):
                content_type = str(headers_in.get("Content-Type", "tuntematon"))
            raise _Permanent(
                f"vastaus ei ollut JSONia (tilakoodi {status}, "
                f"Content-Type {content_type}, {length} merkkiä)",
                status_code=status,
            ) from exc

        if not isinstance(payload, Mapping):
            raise _Permanent(
                f"vastaus ei ollut olio vaan {type(payload).__name__}",
                status_code=status,
            )
        return payload, status

    # -- Välimuisti ----------------------------------------------------------

    def _cache_path(self, path: str, params: Mapping[str, Any] | None) -> Path:
        """Välimuistitiedoston polku: luettava nimi + lyhyt tiiviste.

        Nimessä ei ole mitään avaimesta johdettua -- avain kulkee otsakkeessa
        eikä osoitteessa. Luettava osa kertoo silmällä, mistä kutsusta on kyse;
        tiiviste takaa yksikäsitteisyyden myös silloin, kun luettava osa on
        katkaistu pituusrajan takia.

        **Tiiviste lasketaan koko osoitteesta, ei pelkästä polusta.** Kaksi eri
        juurta samalla ``cache_dir``illa tarjoilisi muuten toistensa
        vastauksia, eikä mikään kertoisi siitä.
        """
        query = "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        readable_part = f"{path}?{query}" if query else path
        canonical = f"{self.base_url}{readable_part}"
        digest = sha256(canonical.encode("utf-8")).hexdigest()[:12]
        readable = _UNSAFE_IN_NAME.sub("-", readable_part).strip("-")[:80]
        return self.cache_dir / f"{readable}-{digest}.json"


# -- Parametrien tarkistus ---------------------------------------------------
#
# Adapteri ei lue asetuksia, mutta se ei myöskään saa hyväksyä hiljaa sitä,
# minkä asetusmalli hylkää. Rajat ovat samat vakiot kuin asetusmallilla
# (:mod:`pappascout.domain.models`), joten yhtä lukua ei ole kahdessa paikassa.


def _check_int(value: Any, name: str, low: int, high: int) -> int:
    number = int(value)
    if not low <= number <= high:
        raise SettingsError(
            f"FaceitClient: {name} = {number} on sallitun välin {low}-{high} "
            "ulkopuolella.\n"
            "Korjaa arvo asetustiedoston [faceit]-osiossa."
        )
    return number


def _check_float(value: Any, name: str, low: float, high: float | None = None) -> float:
    number = float(value)
    if number < low or (high is not None and number > high):
        limit = f"{low:g}-{high:g}" if high is not None else f"vähintään {low:g}"
        raise SettingsError(
            f"FaceitClient: {name} = {number:g} ei kelpaa (sallittu {limit}).\n"
            "Korjaa arvo asetustiedoston [faceit]-osiossa."
        )
    return number


def _check_base_url(base_url: str) -> str:
    """Vaadi ``https``.

    Avain kulkee ``Authorization``-otsakkeessa, ja ``http`` lähettäisi sen
    selkokielisenä. Moduulissa, jonka olemassaolon syy on avaimen suojaaminen,
    tämä on rakenteellinen sääntö eikä tapa.
    """
    trimmed = str(base_url).rstrip("/")
    if urlsplit(trimmed).scheme != "https":
        raise SettingsError(
            f"FaceitClient: osoitteen {trimmed!r} on oltava https.\n"
            "Avain kulkee Authorization-otsakkeessa, ja http lähettäisi sen "
            "salaamattomana."
        )
    return trimmed


# -- Vastauksen rakennetarkistus --------------------------------------------
#
# Ajetaan ENNEN välimuistiin kirjoitusta. Ilman sitä rikkinäinen 200 jäisi
# levylle, ja jokainen seuraava ajo kaatuisi samaan virheeseen käymättä
# lainkaan verkossa -- eli välimuisti muuttuisi tilaksi, jota pitää siivota.


def _is_cacheable_match(payload: Mapping[str, Any]) -> bool:
    """Saako tämän ottelun tiedot kirjoittaa välimuistiin pysyvästi?

    Luettu kenttä on ``status``, ja se on **FACEITin vakain mahdollinen**:
    ottelun elinkaari on rajapinnan perustavin käsite, joten jos tämä kenttä
    muuttuu, koko rajapinta on muuttunut -- eikä silloin ole olemassa
    vaihtoehtoa, joka olisi jatkanut toimintaansa. Sen lukeminen ei siis ole
    samaa lajia kuin vastauksen muodon arvaaminen.

    Tuntematon tai puuttuva tila **ei kelpaa**: se on "en tiedä", eikä "en
    tiedä" ole peruste säilyttää vastausta ikuisesti. Uuden tilan lisääminen
    FACEITiin johtaa siis yhteen ylimääräiseen kutsuun eikä väärään
    vastaukseen -- oikea suunta kahdesta.

    Vertailu on kirjainkoosta riippumaton, koska ``status`` on havainto eikä
    tämän moduulin kirjoittama arvo.
    """
    status = _text(payload.get("status"))
    return status is not None and status.upper() in CACHEABLE_MATCH_STATUSES


def _check_match(payload: Mapping[str, Any]) -> None:
    if _text(payload.get("match_id")) is None:
        raise _Invalid("ottelulla ei ole match_id:tä")


def _check_match_list(payload: Mapping[str, Any]) -> None:
    """Tarkista ottelulista **rivi riviltä**.

    Roskarivin hiljainen pudottaminen olisi eri sääntö kuin puuttuvan
    ``items``in nostama virhe, ja kaksi eri sääntöä samalle vastaukselle
    tarkoittaisi, että sivun pituus riippuu siitä, kumpaa katsotaan. Nyt
    kelvoton rivi pysäyttää haun kuten kelvoton vastauskin.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        raise _Invalid("kenttää 'items' ei ollut tai se ei ollut lista")
    for index, entry in enumerate(items):
        if not isinstance(entry, Mapping):
            raise _Invalid(f"rivi {index} ei ole olio vaan {type(entry).__name__}")
        if _text(entry.get("match_id")) is None:
            raise _Invalid(f"rivillä {index} ei ole match_id:tä")


def _retry_after_seconds(headers: Any) -> float | None:
    """Lue ``Retry-After`` sekunteina, tai ``None`` jos otsaketta ei ole.

    **Rajapinnan oma mittaus voittaa arvauksen.** Kasvava viive on sokea
    oletus; ``Retry-After`` on se luku, jonka palvelin itse sanoo, ja
    ``settings.toml`` myöntää itse, ettei 429-jaksojen pituutta ole mitattu.
    Otsake on joko sekunteja tai HTTP-päiväys (RFC 9110), ja molemmat ovat
    käytössä. Menneisyyteen osoittava päiväys ja negatiivinen luku ovat
    ``None``: ne tarkoittaisivat "älä odota lainkaan", mikä ei ole odotusaika.
    """
    if not isinstance(headers, Mapping):
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return seconds if seconds > 0 else None
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = (moment - datetime.now(UTC)).total_seconds()
    return seconds if seconds > 0 else None


def _cache_entry_is_usable(
    payload: Mapping[str, Any],
    validate: Callable[[Mapping[str, Any]], None],
    cache_when: Callable[[Mapping[str, Any]], bool],
) -> bool:
    """Saako tämän välimuistitiedoston sisällön käyttää vastauksena?

    **Kaksi ehtoa, sama seuraus.** Tiedosto kelpaa vain jos se on rakenteeltaan
    se mitä portti lupaa (``validate``) **ja** sisällöltään sellainen, joka
    ylipäätään olisi saanut päätyä levylle (``cache_when``). Kumpi tahansa
    hylkäys johtaa samaan: tiedosto poistetaan ja vastaus haetaan verkosta.

    Toinen ehto on symmetria, joka olisi kuulunut tähän heti. Ilman sitä
    invariantti "levyllä on vain muuttumattomia vastauksia" nojaisi ikuisesti
    siihen, että jokainen kirjoituspolku -- myös menneiden versioiden -- oli
    oikein. Mitattu vastaesimerkki löytyi jaetusta arkistosta 4.9.2026.

    Paluuarvo on totuusarvo eikä syy, koska syytä ei näytetä kenellekään:
    kelpaamaton tiedosto ei ole virhe vaan puuttuva tiedosto.
    """
    try:
        validate(payload)
    except _Invalid:
        return False
    return cache_when(payload)


def _read_cache(path: Path) -> Mapping[str, Any] | None:
    """Lue välimuistitiedosto tai palauta ``None``.

    Rikkoutunut tai lukukelvoton tiedosto ohitetaan kuin sitä ei olisi:
    välimuisti on pelkkä HTTP-välimuisti, jonka ainoa seuraus poistamisesta on
    uusi kutsu. Poikkeuksen nostaminen tekisi siitä tilan, jota pitää siivota.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _write_cache(path: Path, payload: Mapping[str, Any]) -> None:
    """Kirjoita vastaus välimuistiin atomisesti.

    Sama sääntö kuin arkiston kirjoituksilla (AD-7): ensin väliaikaistiedosto,
    sitten ``os.replace``. Kesken jäänyt kirjoitus ei saa jättää vajaata JSONia,
    jota seuraava ajo lukisi vastauksena. Toteutus on täällä eikä
    :mod:`pappascout.archive.atomic_write`issa, koska adapteri ei saa tuoda
    arkistopakettia (``tests/test_layering.py``).

    **Siivous on ``finally``ssä eikä virhehaarassa.** Keskeytys (Ctrl-C) ei ole
    ``OSError``, joten pelkkä ``except OSError`` jättäisi ``.tmp-*``-tiedoston
    arkistoon -- juuri sen, mitä ``atomic_write`` lupaa olla jättämättä.
    Onnistuneen ``os.replace``in jälkeen väliaikaistiedostoa ei enää ole, joten
    siivous on silloin tyhjä operaatio.

    Epäonnistuminen ei ole virhe: välimuisti on nopeutus, ei tulos. Jos levy on
    täynnä tai hakemisto vain luku, kutsu on jo onnistunut ja vastaus on
    kädessä -- kaatuminen tässä hukkaisi sen.
    """
    tmp = path.with_name(
        f"{path.name}.tmp-{_host_tag()}-{os.getpid()}-{_secrets.token_hex(4)}"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except OSError:
        pass
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - siivous ei saa peittää syytä
            pass


@lru_cache(maxsize=1)
def _host_tag() -> str:
    """Konenimi tiedostonimeen kelpaavassa muodossa.

    Sama sääntö ja sama välimuistitus kuin
    :func:`pappascout.archive.atomic_write.host_tag`illa; kopio siksi, että
    adapteri ei saa tuoda arkistopakettia.
    """
    name = socket.gethostname() or "unknown-host"
    return _UNSAFE_IN_NAME.sub("-", name).strip("-").lower() or "unknown-host"


# -- FACEIT-JSON ytimen sanastoksi ------------------------------------------
#
# Muunnos on täällä eikä vaiheessa (AD-8): ``faction1``, ``voting`` ja
# epoch-sekunnit ovat FACEITin sanastoa, ja portti puhuu ytimen sanastoa.


def _to_match(payload: Mapping[str, Any]) -> Match:
    """Muunna yksi FACEIT-ottelu :class:`Match`iksi.

    Syöte on **jo tarkistettu** (:func:`_check_match`), joten ``match_id`` on
    olemassa. Muut kentät ovat valinnaisia: puuttuva arvo on ``None`` eikä
    korvike, koska jokainen niistä on **havainto** siitä, mitä rajapinta
    kertoi, ja keksitty arvo näyttäisi taulussa täsmälleen samalta kuin
    mitattu.
    """
    match_id = _text(payload.get("match_id"))
    if match_id is None:  # pragma: no cover - _check_match on jo torjunut tämän
        raise _Invalid("ottelulla ei ole match_id:tä")

    teams_raw = payload.get("teams")
    teams: tuple[MatchTeam, ...] = ()
    if isinstance(teams_raw, Mapping):
        # Avainten mukaan järjestettynä (``faction1`` ennen ``faction2``), jotta
        # osapuolten järjestys on sama joka ajolla. Sanakirjan oma järjestys
        # riippuisi JSONin kirjoitusjärjestyksestä.
        teams = tuple(
            _to_team(teams_raw[key])
            for key in sorted(teams_raw)
            if isinstance(teams_raw[key], Mapping)
        )

    return Match(
        match_id=match_id,
        competition_id=_text(payload.get("competition_id")),
        status=_text(payload.get("status")),
        scheduled_at=_moment(payload.get("scheduled_at")),
        started_at=_moment(payload.get("started_at")),
        finished_at=_moment(payload.get("finished_at")),
        teams=teams,
        map_picks=_map_picks(payload.get("voting")),
    )


def _to_team(raw: Mapping[str, Any]) -> MatchTeam:
    """Muunna ottelurivin osapuoli :class:`MatchTeam`iksi.

    ``roster`` ja ``substitutes`` luetaan **erikseen ja molemmat**: FACEIT
    erottelee ne, ja vakirosterin yhdiste on domainin sääntö eikä adapterin
    (ks. :class:`MatchTeam`). Mitattu 2026-09-04: jokaisella ottelurivillä oli
    molemmat listat (132/132 joukkueriviä).
    """
    return MatchTeam(
        team_id=_text(raw.get("faction_id")) or _text(raw.get("team_id")),
        name=_text(raw.get("name")),
        roster=_to_players(raw.get("roster")),
        substitutes=_to_players(raw.get("substitutes")),
    )


def _to_players(raw: Any) -> tuple[RosterPlayer, ...]:
    """Pelaajalista lähteen järjestyksessä; puuttuva lista on tyhjä monikko.

    Pelaaja ilman ``player_id``:tä pudotetaan -- tunniste on koneen avain, eikä
    tunnisteeton rivi ole liitettävissä mihinkään. ``game_player_id`` sen
    sijaan saa puuttua: se on **eri tunniste** (SteamID64), ja sen puuttuminen
    on havainto, jonka vaihe näkee ja kertoo ääneen.
    """
    if not isinstance(raw, list):
        return ()
    return tuple(
        RosterPlayer(
            player_id=player_id,
            nickname=_text(entry.get("nickname")),
            game_player_id=_text(entry.get("game_player_id")),
        )
        for entry in raw
        if isinstance(entry, Mapping)
        and (player_id := _text(entry.get("player_id"))) is not None
    )


def _map_picks(voting: Any) -> tuple[str, ...]:
    """Pelatut kartat valintajärjestyksessä -- ``map_index``in määritelmä.

    Puuttuva vetotieto on tyhjä monikko, ei virhe: tulevalla ottelulla ei ole
    vielä vetoa, ja sekin on kelvollinen havainto.
    """
    if not isinstance(voting, Mapping):
        return ()
    map_vote = voting.get("map")
    if not isinstance(map_vote, Mapping):
        return ()
    picks = map_vote.get("pick")
    if not isinstance(picks, list):
        return ()
    return tuple(name for pick in picks if (name := _text(pick)) is not None)


def _text(value: Any) -> str | None:
    """Merkkijono havaintona: tyhjä tai muu tyyppi on ``None``, ei korvike."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _moment(value: Any) -> datetime | None:
    """Epoch-sekunnit UTC-tietoiseksi hetkeksi.

    FACEIT antaa ajat epoch-sekunteina, ja ``0`` tarkoittaa "ei asetettu" --
    ei vuotta 1970. Muu kuin luku on ``None``: arvaus näyttäisi hetkeltä.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (OverflowError, OSError, ValueError):
        return None
