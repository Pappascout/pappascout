"""FACEIT-asiakkaan testit -- kaikki offline (Story 3.1).

**Yksikään testi tässä tiedostossa ei koske verkkoon**, eikä se ole tapa vaan
rakenne: :func:`_no_network` estää jokaisen oikean HTTP-kutsun koko moduulin
ajaksi, ja jokainen asiakas rakennetaan :class:`FakeSession`illa, joka
palauttaa käsin kirjoitetun vastauksen. Jos joku vahingossa poistaisi
kuljetuksen parametrista, testi ei mene hiljaa verkkoon vaan kaatuu.

Oikeaa rajapintaa vasten todennetaan käsin, ja tulos kirjataan speksin
"Manual checks" -osioon.
"""

from __future__ import annotations

import ast
import json
import tomllib
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests
from conftest import REAL_SETTINGS, has_temp_leftovers

from pappascout.adapters import faceit as faceit_module
from pappascout.adapters.faceit import (
    CACHEABLE_MATCH_STATUSES,
    DEFAULT_CALL_BUDGET_SECONDS,
    FACEIT_DATA_API_BASE,
    MAX_PAGES,
    FaceitClient,
)
from pappascout.adapters.protocols import Match, MatchSource
from pappascout.domain.models import (
    MAX_FACEIT_PAGE_SIZE,
    MAX_FACEIT_RETRY_ATTEMPTS,
    FaceitSettings,
    load_settings,
)
from pappascout.errors import ApiError, PappascoutError, SettingsError

#: Avain, joka on tunnistettavissa tulosteesta ja tiedostosta.
#:
#: Ei "avain" eikä "test": haku ``grep`` löytää tämän merkkijonon vain, jos
#: avain todella vuoti johonkin -- geneerinen sana osuisi kommentteihin ja
#: tekisi vartijasta hampaattoman.
KEY = "salainen-avain-XYZZY-42"

#: Pappaliigan kauden 13 championship (settings.toml, [league]).
CHAMPIONSHIP = "94681888-b5da-4ab5-bf50-f44b666b98a3"

#: Testien oma juuri. **Ei oikea osoite**, jottei yksikään testi voi osua
#: siihen edes vahingossa -- ja https, koska asiakas vaatii sen.
BASE = "https://faceit.invalid/data/v4"

MATCHES_URL = f"{BASE}/championships/{CHAMPIONSHIP}/matches"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Katkaise oikea HTTP koko moduulin ajaksi.

    ``conftest`` eristää testit koneen tiedostoista; tämä eristää ne verkosta.
    Katkaisu on ``HTTPAdapter.send``issä eikä ``Session.get``issä, koska juuri
    se on se kohta, jossa ``requests`` avaa yhteyden -- ja siksi sen ohi ei
    pääse millään ``Session``in kutsutavalla.
    """

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "Testi yritti mennä verkkoon. Anna FaceitClientille session-parametri."
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _refuse)


# -- Kiinnikkeet -------------------------------------------------------------


class FakeResponse:
    """Käsin kirjoitettu HTTP-vastaus.

    ``payload=None`` tarkoittaa "vastaus ei ole JSONia": :meth:`json` nostaa
    ``ValueError``in täsmälleen kuten ``requests`` tekee HTML-sivulle.
    """

    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        text: str | None = None,
        content_type: str = "application/json",
        retry_after: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = {"Content-Type": content_type}
        if retry_after is not None:
            self.headers["Retry-After"] = retry_after

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Kuljetus, joka jakaa jonosta valmiita vastauksia eikä avaa yhteyttä.

    Jonon alkio saa olla myös poikkeus, jolloin se nostetaan -- niin
    yhteysvirhe ja aikakatkaisu ovat testattavissa samalla kiinnikkeellä.

    ``closed`` kertoo, sulkiko asiakas kuljetuksen. Injektoitua kuljetusta
    **ei saa** sulkea: se on kutsujan omaisuutta.
    """

    def __init__(self, *responses: Any) -> None:
        self.queue = list(responses)
        self.calls: list[SimpleNamespace] = []
        self.closed = False

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        allow_redirects: bool | None = None,
    ) -> FakeResponse:
        self.calls.append(
            SimpleNamespace(
                url=url,
                headers=headers,
                params=params,
                timeout=timeout,
                allow_redirects=allow_redirects,
            )
        )
        if not self.queue:
            raise AssertionError(
                f"Kutsuja oli enemmän kuin vastauksia: {len(self.calls)}. "
                f"Viimeisin: {url} {params}"
            )
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """Kello, joka liikkuu vain kun testi liikuttaa sitä.

    Aikabudjetti on mitattavissa vasta, kun aika on testin hallinnassa: oikeaa
    kelloa vasten testi joko nukkuisi minuutteja tai mittaisi konetta.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_client(
    tmp_path: Path, *responses: Any, **kwargs: Any
) -> tuple[FaceitClient, FakeSession, list[float]]:
    """Asiakas kiinnikkeillä: kuljetus, tallennetut odotukset ja välimuisti.

    Heitto on oletuksena pois (``random_source`` palauttaa 0.0), jotta
    odotukset ovat vertailtavissa lukuina; heiton oma testi antaa toisen
    lähteen.
    """
    session = FakeSession(*responses)
    waits: list[float] = []
    client = FaceitClient(
        KEY,
        tmp_path / "raw" / "faceit",
        base_url=BASE,
        session=session,
        sleep=waits.append,
        random_source=lambda: 0.0,
        **kwargs,
    )
    return client, session, waits


def match_payload(match_id: str = "1-aaaa", **overrides: Any) -> dict[str, Any]:
    """Yksi FACEIT-ottelu sellaisena kuin rajapinta sen antaa.

    Kentät ja niiden muodot ovat Data API v4:n mukaiset: ajat epoch-sekunteina,
    osapuolet ``faction1``/``faction2``-avaimien takana, kartat
    ``voting.map.pick``-listassa.
    """
    payload: dict[str, Any] = {
        "match_id": match_id,
        "competition_id": CHAMPIONSHIP,
        "competition_name": "6 Divisioona",
        "status": "FINISHED",
        "started_at": 1_756_000_000,
        "finished_at": 1_756_003_600,
        "teams": {
            "faction2": {
                "faction_id": "team-imuaijat",
                "name": "Imuaijat",
                "roster": [
                    {"player_id": "p-imu-1", "nickname": "imu1"},
                    {"player_id": "p-imu-2", "nickname": "imu2"},
                ],
            },
            "faction1": {
                "faction_id": "team-potku",
                "name": "PotkukelkkaPeek",
                "roster": [
                    {"player_id": "p-potku-1", "nickname": "veeti"},
                    {"player_id": "p-potku-2", "nickname": "kaveri"},
                ],
            },
        },
        "voting": {"map": {"pick": ["de_nuke", "de_ancient"]}},
    }
    payload.update(overrides)
    return payload


def cache_files(client: FaceitClient) -> list[Path]:
    """Välimuistitiedostot listana -- myös kun hakemistoa ei ole luotu.

    Ottelulistaa ei enää välimuistiteta, joten hakemistoa ei välttämättä ole
    olemassa lainkaan. ``glob`` puuttuvassa hakemistossa palauttaa tyhjän,
    mutta tämä sanoo aikomuksen ääneen: "levylle ei jäänyt mitään".
    """
    return sorted(client.cache_dir.glob("*.json"))


def page(*matches: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {"items": list(matches), "start": 0, "end": len(matches)})


# -- Avain puuttuu tai on tyhjä ---------------------------------------------


def test_missing_key_stops_the_run_and_names_the_file_and_the_line(
    settings_file: Path,
) -> None:
    """I/O-matriisi: ``.env`` ilman ``FACEIT_API_KEY``.

    Viesti kertoo **tiedoston polun ja tarvittavan rivin**. Pelkkä "avain
    puuttuu" jättäisi käyttäjän etsimään, ja käyttäjä ei koodaa itse.
    """
    settings = load_settings(settings_file, env_files=())

    with pytest.raises(SettingsError) as exc:
        FaceitClient.from_settings(settings, Path("ei-kayteta"))

    message = str(exc.value)
    assert "FACEIT_API_KEY" in message
    assert ".pappascout" in message and ".env" in message
    assert "FACEIT_API_KEY=" in message


@pytest.mark.parametrize("value", ["", '""', "   ", '"   "'])
def test_empty_or_blank_key_is_the_same_as_a_missing_one(
    settings_file: Path, env_file: Any, value: str
) -> None:
    """I/O-matriisi: tyhjä tai pelkkiä välilyöntejä sisältävä avain.

    Tyhjä merkkijono on avaimen puuttuminen kirjoitettuna näkyviin, ja
    lainausmerkit ovat tavallisin tapa kirjoittaa se. Jos tämä menisi läpi,
    virhe tulisi vasta rajapinnalta 401:nä -- eli väärästä paikasta.
    """
    env = env_file(".env", FACEIT_API_KEY=value)
    settings = load_settings(settings_file, env_files=(env,))

    with pytest.raises(SettingsError) as exc:
        FaceitClient.from_settings(settings, Path("ei-kayteta"))

    assert str(env) in str(exc.value)


def test_an_environment_variable_alone_is_not_a_key(
    settings_file: Path, env_file: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Avainta ei kierretä ympäristömuuttujalla ``Secrets``in ohi.

    **Tämä testi asettaa avaimen oikeasti ympäristöön** ja jättää sen pois
    ``.env``-tiedostosta. Ilman ``setenv``-kutsua testi olisi merkki merkiltä
    sama kuin puuttuvan avaimen testi ja menisi läpi, vaikka asiakas lukisi
    ``os.environ``ia suoraan -- eli vartija näyttäisi vartijalta olematta
    sellainen. (Juuri niin se oli kirjoitettu ensin, ja katselmus löysi sen.)

    ``.env`` ladataan **ennen** muuttujan asettamista, koska
    ``pydantic-settings`` lukee ympäristön ``Settings``ia rakentaessaan: jos
    muuttuja olisi asetettu ensin, avain päätyisi ``Settings``iin laillista
    reittiä eikä testi mittaisi mitään.
    """
    settings = load_settings(settings_file, env_files=(env_file(".env"),))
    assert settings.faceit_api_key is None

    monkeypatch.setenv("FACEIT_API_KEY", KEY)

    with pytest.raises(SettingsError) as exc:
        FaceitClient.from_settings(settings, tmp_path)

    assert "FACEIT_API_KEY" in str(exc.value)


def test_the_module_never_touches_the_environment() -> None:
    """``faceit.py`` ei lue ympäristöä lainkaan.

    Edellinen testi mittaa käytöstä; tämä mittaa rakennetta. Yhdessä ne
    sanovat, ettei toista lukutapaa voi lisätä vahingossa.

    Tarkistus on AST:stä eikä tekstistä: tekstihaku osuisi moduulin omaan
    docstringiin, joka **selittää** ettei ympäristöä lueta -- eli
    dokumentaatio kaataisi vartijan, jota se kuvaa.
    """
    source = Path(faceit_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=faceit_module.__file__)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"environ", "getenv"}, ast.dump(node)
        if isinstance(node, ast.Name):
            assert node.id not in {"environ", "getenv"}, node.id
        if isinstance(node, ast.ImportFrom) and node.module == "os":
            for alias in node.names:
                assert alias.name not in {"environ", "getenv"}, alias.name


def test_from_settings_takes_every_knob_from_the_faceit_section(
    settings_file: Path, env_file: Any, tmp_path: Path
) -> None:
    """``[faceit]``-osio on se, joka päättää -- ei adapterin oletus."""
    env = env_file(".env", FACEIT_API_KEY=KEY)
    settings = load_settings(settings_file, env_files=(env,))

    client = FaceitClient.from_settings(settings, tmp_path)

    assert client.retry_attempts == settings.faceit.retry_attempts
    assert (
        client.retry_initial_delay_seconds
        == settings.faceit.retry_initial_delay_seconds
    )
    assert client.retry_max_delay_seconds == settings.faceit.retry_max_delay_seconds
    assert client.retry_jitter_share == settings.faceit.retry_jitter_share
    assert client.timeout_seconds == settings.faceit.timeout_seconds
    assert client.page_size == settings.faceit.page_size
    assert client.call_budget_seconds == settings.faceit.call_budget_seconds
    assert client.base_url == FACEIT_DATA_API_BASE


def test_from_settings_cannot_be_used_to_override_a_setting(
    settings_file: Path, env_file: Any, tmp_path: Path
) -> None:
    """Asetusarvoa ei voi ohittaa ``from_settings``in kautta.

    Jos sen voisi, ``[faceit]``-osio ei enää olisi se, joka päättää -- ja
    ohitus näkyisi vain kutsupaikalla, ei tiedostossa jota säädetään.
    """
    env = env_file(".env", FACEIT_API_KEY=KEY)
    settings = load_settings(settings_file, env_files=(env,))

    with pytest.raises(TypeError):
        FaceitClient.from_settings(settings, tmp_path, page_size=7)


# -- Kutsun kohde ------------------------------------------------------------


def test_the_request_goes_to_the_documented_address(tmp_path: Path) -> None:
    """Osoite, parametrit, otsakkeet ja aikakatkaisu ovat assertin takana.

    Ilman tätä ``FakeSession`` jakaisi vastauksia osoitteesta riippumatta:
    polun voisi vaihtaa muotoon ``/match`` tai ``type``-parametrin poistaa, ja
    koko sarja pysyisi vihreänä. Katselmus löysi juuri sen aukon.
    """
    client, session, _waits = make_client(tmp_path, page(match_payload()))

    client.get_matches(CHAMPIONSHIP)

    call = session.calls[0]
    assert call.url == MATCHES_URL
    assert call.params == {"type": "all", "offset": 0, "limit": client.page_size}
    assert call.headers["Accept"] == "application/json"
    assert call.timeout == client.timeout_seconds
    # Uudelleenohjausta ei seurata: se lähettäisi avaimen osoitteeseen, jota
    # tämä moduuli ei valinnut.
    assert call.allow_redirects is False


def test_the_match_request_goes_to_the_match_address(tmp_path: Path) -> None:
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-aaaa"))
    )

    client.get_match("1-aaaa")

    call = session.calls[0]
    assert call.url == f"{BASE}/matches/1-aaaa"
    assert call.params is None
    assert call.timeout == client.timeout_seconds
    assert call.allow_redirects is False


def test_an_http_address_is_refused(tmp_path: Path) -> None:
    """Avain kulkee otsakkeessa, ja ``http`` lähettäisi sen selkokielisenä."""
    with pytest.raises(SettingsError) as exc:
        FaceitClient(KEY, tmp_path, base_url="http://faceit.invalid/data/v4")

    assert "https" in str(exc.value)


def test_the_real_default_address_is_https() -> None:
    assert FACEIT_DATA_API_BASE.startswith("https://")


# -- Uudelleenyritys ---------------------------------------------------------


def test_rate_limit_is_retried_and_the_run_continues(tmp_path: Path) -> None:
    """I/O-matriisi: 429 ja sitten 200 -- ajo onnistuu.

    Hyväksymiskriteeri sanoo "uudelleenyrityksiä oli **vähintään yksi**", ja
    juuri siksi luku luetaan ``requests_made``ista eikä pääteltäisi siitä,
    että tulos tuli: ilman laskuria testi menisi läpi myös silloin, kun
    uudelleenyritystä ei tapahtunut lainkaan.
    """
    client, session, waits = make_client(
        tmp_path,
        FakeResponse(429, {"errors": [{"message": "rate limit"}]}),
        page(match_payload()),
        retry_attempts=4,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    matches = client.get_matches(CHAMPIONSHIP)

    assert len(matches) == 1
    assert client.requests_made == 2
    assert len(session.calls) == 2
    assert waits == [1.0]


def test_server_errors_are_retried(tmp_path: Path) -> None:
    """I/O-matriisi: 5xx käyttäytyy kuten 429."""
    client, _session, waits = make_client(
        tmp_path,
        FakeResponse(500),
        FakeResponse(503),
        page(match_payload()),
        retry_attempts=4,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    assert len(client.get_matches(CHAMPIONSHIP)) == 1
    assert client.requests_made == 3
    # Kasvava viive, ei vakio: 1 s, sitten 2 s.
    assert waits == [1.0, 2.0]


def test_the_growing_delay_stops_at_the_configured_ceiling(tmp_path: Path) -> None:
    """Katto leikkaa viiveen -- muuten kahdeksas yritys olisi minuuttien päässä."""
    client, _session, waits = make_client(
        tmp_path,
        *[FakeResponse(503) for _ in range(4)],
        page(match_payload()),
        retry_attempts=6,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=2.5,
    )

    client.get_matches(CHAMPIONSHIP)

    assert waits == [1.0, 2.0, 2.5, 2.5]


def test_retry_after_in_seconds_beats_the_guessed_delay(tmp_path: Path) -> None:
    """Rajapinnan oma mittaus voittaa kasvavan viiveen.

    Kasvava viive on sokea oletus; ``Retry-After`` on se luku, jonka palvelin
    itse sanoo -- ja ``settings.toml`` myöntää itse, ettei 429-jaksojen
    pituutta ole mitattu. Ilman tätä asiakas odottaisi 1 s siellä, missä
    palvelin pyytää odottamaan 7 s, ja törmäisi rajoitukseen uudelleen.
    """
    client, _session, waits = make_client(
        tmp_path,
        FakeResponse(429, retry_after="7"),
        page(match_payload()),
        retry_attempts=3,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    client.get_matches(CHAMPIONSHIP)

    assert waits == [7.0]


def test_retry_after_as_an_http_date_is_understood(tmp_path: Path) -> None:
    """``Retry-After`` on RFC 9110:n mukaan joko sekunteja tai päiväys."""
    later = format_datetime(datetime.now(UTC) + timedelta(seconds=30))
    client, _session, waits = make_client(
        tmp_path,
        FakeResponse(503, retry_after=later),
        page(match_payload()),
        retry_attempts=3,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=120.0,
        call_budget_seconds=600.0,
    )

    client.get_matches(CHAMPIONSHIP)

    assert len(waits) == 1
    assert 20.0 < waits[0] <= 30.0


def test_a_retry_after_in_the_past_is_not_a_waiting_time(tmp_path: Path) -> None:
    """Mennyt päiväys ja negatiivinen luku tarkoittaisivat "älä odota"."""
    client, _session, waits = make_client(
        tmp_path,
        FakeResponse(429, retry_after="-5"),
        page(match_payload()),
        retry_attempts=3,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    client.get_matches(CHAMPIONSHIP)

    # Palataan kasvavaan viiveeseen, koska otsake ei kelvannut odotusajaksi.
    assert waits == [1.0]


def test_a_nonsense_retry_after_falls_back_to_the_growing_delay(
    tmp_path: Path,
) -> None:
    client, _session, waits = make_client(
        tmp_path,
        FakeResponse(429, retry_after="pian"),
        page(match_payload()),
        retry_attempts=3,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    client.get_matches(CHAMPIONSHIP)

    assert waits == [1.0]


def test_jitter_spreads_two_parallel_runs_apart(tmp_path: Path) -> None:
    """Heitto on olemassa, jotta kaksi ajoa ei odota samaan sekuntiin.

    Eksponentiaalinen viive on kaikilla ajoilla sama funktio samasta hetkestä.
    Ilman heittoa kaksi konetta törmäisi rajoitukseen yhdessä uudelleen ja
    uudelleen.
    """
    session = FakeSession(FakeResponse(429), page(match_payload()))
    waits: list[float] = []
    client = FaceitClient(
        KEY,
        tmp_path / "raw" / "faceit",
        base_url=BASE,
        session=session,
        sleep=waits.append,
        random_source=lambda: 1.0,
        retry_attempts=3,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
        retry_jitter_share=0.25,
    )

    client.get_matches(CHAMPIONSHIP)

    # 1.0 s + 25 % = 1.25 s, eli heitto on päällä eikä pyöristettynä pois.
    assert waits == [1.25]


def test_a_connection_error_is_retried(tmp_path: Path) -> None:
    """Aikakatkaisu ja yhteysvirhe ovat ohimeneviä vikoja, ei virheitä."""
    client, _session, _waits = make_client(
        tmp_path,
        requests.ConnectionError("verkko poikki"),
        requests.Timeout("liian hidas"),
        page(match_payload()),
        retry_attempts=4,
    )

    assert len(client.get_matches(CHAMPIONSHIP)) == 1
    assert client.requests_made == 3


def test_when_retries_run_out_the_error_names_the_attempt_count(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: lopulta virhe, joka kertoo yritysten määrän."""
    client, _session, _waits = make_client(
        tmp_path,
        *[FakeResponse(429) for _ in range(3)],
        retry_attempts=3,
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert exc.value.attempts == 3
    assert exc.value.status_code == 429
    assert "3" in str(exc.value)
    assert CHAMPIONSHIP in str(exc.value)
    assert client.requests_made == 3


def test_a_client_error_is_not_retried_even_once(tmp_path: Path) -> None:
    """I/O-matriisi: 4xx muu kuin 429 -- **ei uudelleenyritystä**.

    Väärä id ei muutu oikeaksi odottamalla. Uudelleenyritys vain viivyttäisi
    virhettä, joka on jo varma, ja kuluttaisi kutsukiintiötä.
    """
    client, session, waits = make_client(
        tmp_path,
        FakeResponse(404, {"errors": [{"message": "not found"}]}),
        retry_attempts=5,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-ei-ole")

    assert exc.value.attempts == 1
    assert exc.value.status_code == 404
    assert len(session.calls) == 1
    assert waits == []
    assert "404" in str(exc.value)
    assert "1-ei-ole" in str(exc.value)
    assert "raw/faceit/" in str(exc.value)


def test_a_wrong_key_is_not_retried_either(tmp_path: Path) -> None:
    """401 on sama luokka kuin 404: odottaminen ei tee avaimesta kelvollista."""
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(401), retry_attempts=5
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert exc.value.status_code == 401
    assert exc.value.attempts == 1


@pytest.mark.parametrize("status", [204, 301, 302, 304])
def test_a_non_2xx_that_is_not_an_error_status_names_its_status(
    tmp_path: Path, status: int
) -> None:
    """204 ja 3xx eivät ole JSONia -- eivätkä ne saa näyttää JSON-vialta.

    Ehto oli ennen ``status >= 400``, joten kaikki sen alle jäävä valui
    JSON-jäsennykseen ja päätyi virheeseen "vastaus ei ollut JSONia". Se kertoo
    oireesta eikä syystä: uudelleenohjaus on eri vika kuin rikkinäinen runko.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(status, None, text=""), retry_attempts=3
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert exc.value.status_code == status
    assert str(status) in str(exc.value)
    assert "JSON" not in str(exc.value)
    assert exc.value.attempts == 1


def test_api_error_is_a_pappascout_error() -> None:
    """Kutsuja nappaa yhden tyypin ja näyttää suomenkielisen viestin."""
    assert issubclass(ApiError, PappascoutError)


# -- Aikabudjetti ------------------------------------------------------------


def test_a_slow_api_stops_at_the_call_budget(tmp_path: Path) -> None:
    """Yksi kutsu ei saa kestää hiljaa loputtomiin.

    ``retry_attempts`` ei rajaa aikaa: sivutus kertoo yritykset sivujen
    määrällä, joten ``MAX_PAGES`` x ``retry_attempts`` olisi satoja pyyntöjä ja
    kasvavalla viiveellä tunnin hiljaisuus. Katon on siksi oltava sekunneissa.
    """
    clock = FakeClock()
    session = FakeSession(*[FakeResponse(503) for _ in range(9)])
    client = FaceitClient(
        KEY,
        tmp_path / "raw" / "faceit",
        base_url=BASE,
        session=session,
        sleep=clock.advance,
        clock=clock,
        random_source=lambda: 0.0,
        retry_attempts=9,
        retry_initial_delay_seconds=4.0,
        retry_max_delay_seconds=16.0,
        call_budget_seconds=20.0,
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-hidas")

    # 4 s + 8 s mahtuu (12 s), seuraava 16 s ei -- budjettia on jäljellä 8 s.
    assert client.requests_made == 3
    assert exc.value.attempts == 3
    assert exc.value.status_code == 503
    assert "budjet" in str(exc.value).lower()
    assert "call_budget_seconds" in str(exc.value)


def test_a_long_retry_after_does_not_buy_silence(tmp_path: Path) -> None:
    """``Retry-After``, joka ylittää budjetin, on virhe eikä odotus.

    Palvelin saa pyytää odottamaan minuutteja, mutta odotus, joka ei mahdu
    budjettiin, on hiljaisuutta ilman mahdollisuutta onnistua.
    """
    clock = FakeClock()
    session = FakeSession(FakeResponse(429, retry_after="600"), page(match_payload()))
    client = FaceitClient(
        KEY,
        tmp_path / "raw" / "faceit",
        base_url=BASE,
        session=session,
        sleep=clock.advance,
        clock=clock,
        random_source=lambda: 0.0,
        retry_attempts=4,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=900.0,
        call_budget_seconds=60.0,
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert client.requests_made == 1
    assert exc.value.status_code == 429
    assert "600" in str(exc.value)


def test_the_budget_spans_the_whole_pagination(tmp_path: Path) -> None:
    """Budjetti on porttikutsun eikä yksittäisen sivun.

    Sivukohtainen budjetti ei rajaisi ``MAX_PAGES`` sivun hakua lainkaan.
    """
    clock = FakeClock()
    session = FakeSession(*[page(match_payload(f"1-a{i}")) for i in range(10)])

    def slow_get(*args: Any, **kwargs: Any) -> FakeResponse:
        clock.advance(3.0)
        return session.get(*args, **kwargs)

    client = FaceitClient(
        KEY,
        tmp_path / "raw" / "faceit",
        base_url=BASE,
        session=SimpleNamespace(get=slow_get),
        sleep=clock.advance,
        clock=clock,
        random_source=lambda: 0.0,
        page_size=1,
        call_budget_seconds=10.0,
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    # Budjetti tarkistetaan ENNEN sivun aloitusta, ja sivun kestoa ei voi
    # tietää etukäteen: neljäs sivu alkaa 9 s kohdalla (alle 10 s) ja vie
    # budjetin 12 sekuntiin, jolloin viides ei enää ala. Neljä pyyntöä on siis
    # oikea luku -- viisi olisi vartijan pettäminen ja kolme lupaus, jota ei
    # voi pitää.
    assert client.requests_made == 4
    assert "call_budget_seconds" in str(exc.value)


def test_the_default_budget_is_a_guard_not_a_neutral_zero() -> None:
    """Budjetin oletus on vartija samassa mielessä kuin :data:`MAX_PAGES`."""
    assert DEFAULT_CALL_BUDGET_SECONDS > 0


# -- Välimuisti --------------------------------------------------------------


def test_the_match_list_is_never_cached(tmp_path: Path) -> None:
    """Veetin päätös 4.9.2026: ottelulistaa ei välimuistiteta lainkaan.

    Se on **yksi kutsu per ajo** -- divisioonan 66 ottelua mahtuu yhdelle
    sivulle -- ja se **muuttuu jatkuvasti**: 60 ottelua 66:sta oli tilassa
    ``SCHEDULED``. Välimuisti säästäisi siellä yhden kutsun ja maksaisi
    oikeellisuuden. Vaatimus on "älä missaa selviä otteluita", ja tämä on
    ainoa tapa pitää se ilman kelloa.
    """
    client, session, _waits = make_client(
        tmp_path, page(match_payload()), page(match_payload())
    )

    first = client.get_matches(CHAMPIONSHIP)
    second = client.get_matches(CHAMPIONSHIP)

    assert first == second
    assert client.requests_made == 2
    assert client.cache_hits == 0
    assert len(session.calls) == 2
    # Eikä levylle jäänyt mitään -- ei myöskään tiedostoa, joka harhauttaisi
    # seuraavaa lukijaa.
    assert cache_files(client) == []


def test_a_new_match_appears_on_the_second_run(tmp_path: Path) -> None:
    """Kauden aikana lisätty ottelu näkyy heti, ei vasta tyhjennyksen jälkeen.

    Tämä on koko päätöksen tarkoitus. Välimuistitettuna toinen ajo palauttaisi
    ensimmäisen listan, ja uusi ottelu jäisi pois ilman että mikään kertoisi.
    """
    client, _session, _waits = make_client(
        tmp_path,
        page(match_payload("1-vanha")),
        page(match_payload("1-vanha"), match_payload("1-uusi")),
    )

    assert [m.match_id for m in client.get_matches(CHAMPIONSHIP)] == ["1-vanha"]
    assert [m.match_id for m in client.get_matches(CHAMPIONSHIP)] == [
        "1-vanha",
        "1-uusi",
    ]


def test_a_finished_match_is_cached_permanently(tmp_path: Path) -> None:
    """Pelatun ottelun tiedot eivät enää muutu, ja niitä haetaan jopa 66 kertaa.

    Kuljetuksen jonossa on **vain yksi** vastaus, joten toinen verkkokutsu
    kaataisi testin: osuma ei ole pääteltävissä laskurista vaan rakenteesta.
    """
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-valmis", status="FINISHED"))
    )

    first = client.get_match("1-valmis")
    second = client.get_match("1-valmis")

    assert first == second
    assert client.requests_made == 1
    assert client.cache_hits == 1
    assert len(session.calls) == 1
    assert len(cache_files(client)) == 1


@pytest.mark.parametrize("status", ["SCHEDULED", "ONGOING", "CANCELLED", None])
def test_a_match_that_is_not_finished_is_not_cached(
    tmp_path: Path, status: str | None
) -> None:
    """Keskeneräistä ottelua ei kirjoiteta levylle -- ja tulos päivittyy.

    Kolmas parametri on ``CANCELLED``, joka näyttää yhtä lopulliselta kuin
    ``FINISHED``. Se on silti ulkona, ja syy on epäsymmetrinen hinta:
    peruminen on järjestäjän päätös, jonka järjestäjä voi perua, ja siirretty
    ottelu pelataan samalla ``match_id``:llä. Välimuistitettu ``CANCELLED``
    piilottaisi sen ikuisesti -- yhden säästetyn kutsun hinnalla menetettäisiin
    pelatun ottelun demo pysyvästi (FACEIT säilyttää demot ~30 pv).

    Neljäs on puuttuva tila: "en tiedä" ei ole peruste säilyttää vastausta
    ikuisesti, ja uusi tila FACEITissa johtaa siis yhteen ylimääräiseen
    kutsuun eikä väärään vastaukseen.
    """
    playing = match_payload("1-kesken", status=status)
    if status is None:
        del playing["status"]
    finished = match_payload("1-kesken", status="FINISHED")
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, playing), FakeResponse(200, finished)
    )

    first = client.get_match("1-kesken")
    assert cache_files(client) == []

    second = client.get_match("1-kesken")

    assert client.requests_made == 2
    assert client.cache_hits == 0
    assert len(session.calls) == 2
    # Toinen kutsu näkee ottelun valmistuneena: tulos on päivittynyt.
    assert first.status != "FINISHED"
    assert second.status == "FINISHED"
    # Ja nyt se on välimuistissa.
    assert len(cache_files(client)) == 1


def test_the_cacheable_statuses_are_a_named_constant() -> None:
    """Sääntö on nimetty vakio, ei taikamerkkijono kutsupaikassa.

    Nimettynä se on luettavissa, testattavissa ja muutettavissa yhdessä
    paikassa; kutsupaikkaan kirjoitettuna se olisi päätös, joka näkyy vain
    sille, joka lukee juuri sen rivin.
    """
    assert CACHEABLE_MATCH_STATUSES == frozenset({"FINISHED"})


def test_the_cache_keeps_only_what_cannot_change(tmp_path: Path) -> None:
    """Sama ottelu ensin kesken, sitten valmiina: vain jälkimmäinen jää.

    Tämä on päätös yhtenä lauseena: muuttumaton vastaus säilytetään, muuttuvaa
    ei -- eikä sääntö nojaa kelloon lainkaan.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-x", status="ONGOING")),
        FakeResponse(200, match_payload("1-x", status="FINISHED")),
    )

    client.get_match("1-x")
    client.get_match("1-x")
    third = client.get_match("1-x")  # kolmas ei mene verkkoon

    assert third.status == "FINISHED"
    assert client.requests_made == 2
    assert client.cache_hits == 1


def test_a_cleared_cache_fetches_again_and_gives_the_same_result(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: ``raw/faceit/`` poistettu -- uusi kutsu, sama tulos.

    Tämä on välimuistin lupaus: sen saa poistaa milloin tahansa, eikä
    poistolla ole muuta seurausta kuin uusi kutsu. Ei manifestia, ei tilaa,
    jota pitäisi korjata.
    """
    payload = match_payload("1-valmis")
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, payload), FakeResponse(200, payload)
    )

    first = client.get_match("1-valmis")
    assert cache_files(client)
    for path in cache_files(client):
        path.unlink()

    second = client.get_match("1-valmis")

    assert first == second
    assert client.requests_made == 2
    assert client.cache_hits == 0


def test_a_corrupt_cache_file_is_ignored_like_a_missing_one(tmp_path: Path) -> None:
    """Kesken jäänyt kirjoitus ei saa kaataa ajoa eikä jäädä siivottavaksi."""
    payload = match_payload("1-valmis")
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, payload), FakeResponse(200, payload)
    )

    client.get_match("1-valmis")
    cached = cache_files(client)[0]
    cached.write_text('{"match_id": ', encoding="utf-8")

    assert client.get_match("1-valmis").match_id == "1-valmis"
    assert client.requests_made == 2


def test_a_broken_response_is_not_written_to_the_cache(tmp_path: Path) -> None:
    """Rikkinäinen 200 ei saa jäädä levylle.

    Jos se jäisi, **jokainen seuraava ajo kaatuisi samaan virheeseen käymättä
    lainkaan verkossa** -- eli välimuisti muuttuisi tilaksi, joka on
    siivottava, vaikka sen koko lupaus on päinvastainen. Toinen vastaus jonossa
    todistaa, että seuraava ajo todella yrittää uudelleen.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, {"status": "FINISHED"}),
        FakeResponse(200, match_payload("1-valmis")),
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-valmis")

    assert cache_files(client) == []
    assert "match_id" in str(exc.value)
    assert "raw/faceit/" in str(exc.value)
    # Ja seuraava ajo pääsee verkkoon asti.
    assert client.get_match("1-valmis").match_id == "1-valmis"
    assert client.requests_made == 2


def test_a_poisoned_cache_file_is_dropped_and_refetched(tmp_path: Path) -> None:
    """Vanhemman version kirjoittama kelvoton vastaus ei saa jäädä kaatamaan.

    Tarkistus ennen kirjoitusta estää uudet myrkytykset; tämä hoitaa ne, jotka
    ovat jo levyllä. Ilman sitä käyttäjän ainoa keino olisi tietää itse
    tyhjentää hakemisto.
    """
    payload = match_payload("1-valmis")
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, payload), FakeResponse(200, payload)
    )
    client.get_match("1-valmis")
    cached = cache_files(client)[0]
    cached.write_text('{"status": "FINISHED"}', encoding="utf-8")

    assert client.get_match("1-valmis").match_id == "1-valmis"
    assert client.requests_made == 2
    assert "match_id" in json.loads(cached.read_text(encoding="utf-8"))


def test_a_cached_unfinished_match_is_dropped_and_refetched(tmp_path: Path) -> None:
    """Lukupolku soveltaa samaa ehtoa kuin kirjoituspolku -- itsekorjaavasti.

    Kiinnike on **mitattu tilanne eikä keksitty**: jaetussa arkistossa oli
    4.9.2026 edellisen version kirjoittama tiedosto, joka tarjoili
    ``SCHEDULED``-ottelua levyltä ikuisesti ja täysin hiljaa. Ensin lukupolku
    jätettiin ehdottomaksi perustelulla "jos tiedosto on olemassa, se on
    kirjoitettu valmiista ottelusta", ja live-tarkistus osoitti sen premissin
    vääräksi samana päivänä.

    Symmetrinen ehto tekee invariantista sellaisen, joka ei riipu siitä, että
    jokainen aiempi ja tuleva kirjoituspolku oli oikein -- vaan siitä, mitä
    tiedostossa lukee.
    """
    playing = match_payload("1-x", status="SCHEDULED")
    client, session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-x", status="FINISHED")),
        FakeResponse(200, playing),
    )

    # Lämmitä välimuisti laillisesti, ja korvaa sitten tiedoston sisältö
    # sellaisella, jota nykyinen kirjoituspolku ei olisi koskaan kirjoittanut.
    client.get_match("1-x")
    cached = cache_files(client)[0]
    cached.write_text(json.dumps(playing), encoding="utf-8")
    before = client.requests_made

    result = client.get_match("1-x")

    # Verkkoon mentiin, eikä levylle jäänyt kelpaamatonta tiedostoa.
    assert client.requests_made == before + 1
    assert client.cache_hits == 0
    assert len(session.calls) == 2
    assert result.status == "SCHEDULED"
    assert cache_files(client) == []
    assert not cached.exists()


def test_a_cached_finished_match_is_still_read_from_disk(tmp_path: Path) -> None:
    """Ehto ei saa hylätä sitä, minkä se on tarkoitus säilyttää.

    Tiedoston sisältö korvataan **toisella valmiilla ottelulla**, jota
    kuljetus ei koskaan palauta: jos tulos on se, vastaus tuli levyltä eikä
    muistista tai verkosta. Ilman tätä paria edellinen testi voisi mennä läpi
    myös silloin, kun lukupolku hylkää kaiken.
    """
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-x", status="FINISHED"))
    )

    client.get_match("1-x")
    cached = cache_files(client)[0]
    cached.write_text(
        json.dumps(match_payload("1-x", status="FINISHED", competition_id="levylta")),
        encoding="utf-8",
    )
    before = client.requests_made

    result = client.get_match("1-x")

    assert result.competition_id == "levylta"
    assert client.requests_made == before
    assert client.cache_hits == 1
    assert len(session.calls) == 1
    assert cached.exists()


def test_the_cache_leaves_no_temporary_files(tmp_path: Path) -> None:
    """Kirjoitus on atominen: väliaikaistiedostoa ei jää hakemistoon."""
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-valmis"))
    )

    client.get_match("1-valmis")

    assert cache_files(client)
    assert not has_temp_leftovers(client.cache_dir)


def test_a_cache_write_failure_does_not_lose_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Välimuisti on nopeutus, ei tulos.

    Jos levy on täynnä tai hakemisto vain luku, kutsu on jo onnistunut ja
    vastaus on kädessä -- kaatuminen kirjoitukseen hukkaisi sen. Ilman tätä
    testiä koko ``try/except`` voisi kadota huomaamatta.
    """
    original = Path.write_text

    def _refuse(self: Path, *args: Any, **kwargs: Any) -> int:
        if ".tmp-" in self.name:
            raise OSError("levy täynnä")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _refuse)
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-valmis"))
    )

    assert client.get_match("1-valmis").match_id == "1-valmis"
    assert not has_temp_leftovers(client.cache_dir)


def test_two_different_calls_get_two_cache_files(tmp_path: Path) -> None:
    """Välimuistiavain erottaa kutsut -- muuten toinen palauttaisi väärän."""
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-aaaa")),
        FakeResponse(200, match_payload("1-bbbb")),
    )

    a = client.get_match("1-aaaa")
    b = client.get_match("1-bbbb")

    assert a.match_id == "1-aaaa"
    assert b.match_id == "1-bbbb"
    assert len(list(client.cache_dir.glob("*.json"))) == 2


def test_two_addresses_do_not_share_a_cache_entry(tmp_path: Path) -> None:
    """Osoite on osa välimuistiavainta.

    Kaksi eri juurta samalla hakemistolla tarjoilisi muuten toistensa
    vastauksia, eikä mikään kertoisi siitä.
    """
    cache = tmp_path / "raw" / "faceit"
    one = FaceitClient(
        KEY,
        cache,
        base_url=BASE,
        session=FakeSession(FakeResponse(200, match_payload("1-yksi"))),
    )
    two = FaceitClient(
        KEY,
        cache,
        base_url="https://toinen.invalid/data/v4",
        session=FakeSession(FakeResponse(200, match_payload("1-kaksi"))),
    )

    assert one.get_match("1-x").match_id == "1-yksi"
    assert two.get_match("1-x").match_id == "1-kaksi"
    assert two.cache_hits == 0


# -- Avain ei vuoda ----------------------------------------------------------


def test_the_key_travels_in_the_header_and_nowhere_else(tmp_path: Path) -> None:
    """Avain on ``Authorization``-otsakkeessa, ei osoitteessa eikä parametreissa."""
    client, session, _waits = make_client(tmp_path, page(match_payload()))

    client.get_matches(CHAMPIONSHIP)

    call = session.calls[0]
    assert call.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in call.url
    assert KEY not in str(call.params)


def test_the_key_is_in_no_cache_file(tmp_path: Path) -> None:
    """Hyväksymiskriteeri: avainta ei löydy yhdestäkään välimuistitiedostosta.

    Tarkistetaan **tiedostonimistä ja sisällöistä**, koska välimuistiavain on
    johdettu osoitteesta ja parametreista -- ja jos avain joskus päätyisi
    kumpaankin, se päätyisi myös nimeen.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-valmis"))
    )

    # Ottelun tiedot, koska vain ne kirjoitetaan levylle: ottelulistasta ei
    # synny tiedostoa lainkaan, joten sillä tämä vartija olisi tyhjä.
    client.get_match("1-valmis")

    files = [p for p in client.cache_dir.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert KEY not in path.name
        text = path.read_text(encoding="utf-8")
        assert KEY not in text
        assert "FACEIT_API_KEY" not in text


def test_the_key_is_in_no_repr_and_no_error_message(tmp_path: Path) -> None:
    """Avain ei päädy jäljitykseen, virheilmoitukseen eikä debuggeriin."""
    client, _session, _waits = make_client(tmp_path, FakeResponse(404))

    assert KEY not in repr(client)
    assert KEY not in str(vars(client))

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)
    # ``repr`` näyttää vain ``args``in, joten uudet attribuutit on katsottava
    # erikseen -- muuten vartija ei näkisi kenttää, joka lisätään myöhemmin.
    assert KEY not in str(vars(exc.value))


# -- Vastaus, joka ei ole JSONia --------------------------------------------


def test_an_html_error_page_is_not_dumped_into_the_message(tmp_path: Path) -> None:
    """I/O-matriisi: rajapinta palauttaa HTML-virhesivun.

    Sivu on kilotavuja, eikä sen sisällössä ole mitään, mikä ohjaisi
    käyttäjää. Viesti kertoo **että** vastaus ei ollut JSONia ja minkä
    kokoinen se oli -- ei sitä, mitä sivulla luki.
    """
    html = "<html><body>" + ("Bad Gateway " * 400) + "</body></html>"
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, None, text=html, content_type="text/html"),
        retry_attempts=3,
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    message = str(exc.value)
    assert "Bad Gateway" not in message
    assert "<html>" not in message
    assert "JSON" in message
    assert "text/html" in message
    assert len(message) < 600
    # Ei uudelleenyritystä: rikkinäinen vastaus ei ole ohimenevä ruuhka.
    assert exc.value.attempts == 1


def test_a_json_response_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    """Lista siellä missä pitäisi olla olio on rikkinäinen vastaus, ei tyhjä tulos."""
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, ["a", "b"]))

    with pytest.raises(ApiError):
        client.get_match("1-aaaa")


def test_a_match_list_without_items_is_an_error_not_an_empty_result(
    tmp_path: Path,
) -> None:
    """Puuttuva ``items`` ei saa näyttää siltä, ettei otteluita ole.

    Tyhjä tulos on kelvollinen havainto; rikkinäinen vastaus ei ole. Jos nämä
    kaksi sekoittuisivat, väärä championship-id tuottaisi hiljaa raportin,
    jossa ei ole yhtään ottelua.
    """
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, {"start": 0}))

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert "items" in str(exc.value)
    assert "championship_ids" in str(exc.value)


def test_a_junk_row_stops_the_run_like_a_junk_response(tmp_path: Path) -> None:
    """Roskarivi ei putoa hiljaa.

    Hiljainen pudotus olisi eri sääntö kuin puuttuvan ``items``in nostama
    virhe, ja kahdesta säännöstä seuraisi, että sivun pituus riippuu siitä,
    kumpaa katsotaan -- juuri se luku, jolla sivutus etenee.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, {"items": [match_payload(), "roska"]})
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert "rivi 1" in str(exc.value)


def test_a_row_without_a_match_id_stops_the_run(tmp_path: Path) -> None:
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, {"items": [{"status": "FINISHED"}]})
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert "match_id" in str(exc.value)


def test_a_structure_error_reports_the_real_attempt_count(tmp_path: Path) -> None:
    """``attempts`` ja ``status_code`` eivät saa valehdella.

    Story 3.4 päättää näistä, onko ottelun tila ``no_demo`` vai
    ``download_failed``. ``ApiError``in docstring lupaa, että
    ``status_code=None`` tarkoittaa "vastausta ei saatu lainkaan" -- ja
    rakennevirhe raportoi ennen tätä juuri niin, vaikka vastaus oli 200 kolmen
    yrityksen jälkeen.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(429),
        FakeResponse(503),
        FakeResponse(200, {"start": 0}),
        retry_attempts=4,
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert exc.value.attempts == 3
    assert exc.value.status_code == 200


def test_a_cache_hit_reports_no_attempts(tmp_path: Path) -> None:
    """Välimuistiosuma: ``attempts = 0`` on eri asia kuin epäonnistunut yritys.

    Kuljetuksen jono on tyhjä toisen kutsun kohdalla, joten verkkoon menevä
    haku kaatuisi -- osuma on siis rakenteessa eikä pelkässä laskurissa.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-valmis"))
    )
    client.get_match("1-valmis")

    fetched = client._get(
        "/matches/1-valmis",
        None,
        what="koe",
        validate=lambda payload: None,
        cache_when=lambda payload: True,
    )

    assert fetched.from_cache is True
    assert fetched.attempts == 0
    assert fetched.status_code is None


# -- Sivutus -----------------------------------------------------------------


def test_a_paginated_result_comes_back_as_one_list(tmp_path: Path) -> None:
    """I/O-matriisi: otteluita enemmän kuin sivu vetää.

    Kaikki sivut haetaan, ja kutsuja näkee yhden listan: sivutus on
    kuljetuksen yksityiskohta eikä vaiheen tietoa.
    """
    first = page(*[match_payload(f"1-a{i}") for i in range(3)])
    second = page(*[match_payload(f"1-b{i}") for i in range(2)])
    client, session, _waits = make_client(tmp_path, first, second, page_size=3)

    matches = client.get_matches(CHAMPIONSHIP)

    assert [m.match_id for m in matches] == ["1-a0", "1-a1", "1-a2", "1-b0", "1-b1"]
    assert [call.params["offset"] for call in session.calls] == [0, 3]
    assert {call.params["limit"] for call in session.calls} == {3}


def test_a_full_last_page_still_ends_the_paging(tmp_path: Path) -> None:
    """Täysi sivu ja sen perässä tyhjä: silmukka päättyy tyhjään sivuun."""
    client, session, _waits = make_client(
        tmp_path,
        page(*[match_payload(f"1-a{i}") for i in range(2)]),
        page(),
        page_size=2,
    )

    assert len(client.get_matches(CHAMPIONSHIP)) == 2
    assert len(session.calls) == 2


def test_an_oversized_page_does_not_skip_rows(tmp_path: Path) -> None:
    """Siirtymä on **todellinen** sivun pituus, ei pyydetty.

    Jos rajapinta palauttaa enemmän kuin pyydettiin, ``offset += page_size``
    hyppäisi rivien yli -- ja tulos näyttäisi täydeltä listalta, josta puuttuu
    otteluita. Juuri sellaista puutetta ei huomaisi mistään.
    """
    client, session, _waits = make_client(
        tmp_path,
        page(*[match_payload(f"1-a{i}") for i in range(4)]),
        page(match_payload("1-b0")),
        page_size=2,
    )

    matches = client.get_matches(CHAMPIONSHIP)

    assert [m.match_id for m in matches] == ["1-a0", "1-a1", "1-a2", "1-a3", "1-b0"]
    assert [call.params["offset"] for call in session.calls] == [0, 4]


def test_a_match_repeated_on_two_pages_is_counted_once(tmp_path: Path) -> None:
    """Deduplikointi ``match_id``llä.

    Sivutus nojaa ``offset``iin, ja lista muuttuu kesken kauden: uusi ottelu
    listan alkuun siirtää kaikkia muita yhdellä, jolloin sama rivi tulee
    kahdella sivulla. Ilman deduplikointia se laskettaisiin kahdesti.
    """
    client, _session, _waits = make_client(
        tmp_path,
        page(match_payload("1-a0"), match_payload("1-a1")),
        page(match_payload("1-a1"), match_payload("1-a2")),
        page(),
        page_size=2,
    )

    matches = client.get_matches(CHAMPIONSHIP)

    assert [m.match_id for m in matches] == ["1-a0", "1-a1", "1-a2"]


def test_paging_stops_at_the_guard_instead_of_running_forever(
    tmp_path: Path,
) -> None:
    """Vartija: rajapinta, joka ei koskaan lopeta, ei saa jumittaa ajoa."""
    client, _session, _waits = make_client(
        tmp_path,
        *[page(match_payload(f"1-a{i}")) for i in range(MAX_PAGES + 1)],
        page_size=1,
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert str(MAX_PAGES) in str(exc.value)
    assert client.requests_made == MAX_PAGES


def test_an_empty_championship_is_a_valid_result(tmp_path: Path) -> None:
    """Kilpailu ilman otteluita on havainto, ei virhe."""
    client, _session, _waits = make_client(tmp_path, page())

    assert client.get_matches(CHAMPIONSHIP) == ()


# -- FACEIT-JSON ytimen sanastoksi ------------------------------------------


def test_the_port_speaks_the_core_vocabulary_not_faceits(tmp_path: Path) -> None:
    """``faction1``, ``voting`` ja epoch-sekunnit jäävät adapterin sisään."""
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-aaaa"))
    )

    match = client.get_match("1-aaaa")

    assert isinstance(match, Match)
    assert match.match_id == "1-aaaa"
    assert match.competition_id == CHAMPIONSHIP
    assert match.status == "FINISHED"
    assert match.started_at is not None
    assert match.started_at.tzinfo is not None
    assert match.started_at.timestamp() == 1_756_000_000
    # Järjestys on avainten mukaan, jotta se on sama joka ajolla.
    assert [team.name for team in match.teams] == ["PotkukelkkaPeek", "Imuaijat"]
    assert [p.player_id for p in match.teams[0].roster] == ["p-potku-1", "p-potku-2"]
    assert match.teams[0].roster[0].nickname == "veeti"
    # map_index on indeksi tähän monikkoon; map_demo_id rakentuu siitä.
    assert match.map_picks == ("de_nuke", "de_ancient")


def test_missing_fields_are_none_not_placeholders(tmp_path: Path) -> None:
    """Puuttuva arvo on ``None``, ei korvike -- kuten kokoonpanotaulussakin.

    Keksitty arvo näyttäisi myöhemmin täsmälleen samalta kuin mitattu, eikä
    mikään kertoisi, kumpi se oli.
    """
    bare = {"match_id": "1-bare"}
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, bare))

    match = client.get_match("1-bare")

    assert match.competition_id is None
    assert match.status is None
    assert match.started_at is None
    assert match.finished_at is None
    assert match.teams == ()
    assert match.map_picks == ()


def test_an_unstarted_match_has_no_start_time(tmp_path: Path) -> None:
    """FACEIT merkitsee "ei alkanut" nollalla, ei puuttuvalla kentällä.

    Nolla epoch-sekuntia on 1.1.1970, ja sellaisenaan luettuna se tekisi
    jokaisesta tulevasta ottelusta kaikkein aikaisimman -- eli ``team_key``:n
    tasatilanteen ratkaisijasta väärän.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-tuleva", started_at=0, finished_at=0)),
    )

    match = client.get_match("1-tuleva")

    assert match.started_at is None
    assert match.finished_at is None


def test_an_empty_nickname_is_not_a_name(tmp_path: Path) -> None:
    """Tyhjä merkkijono on havainnon puuttuminen, ei nimi."""
    payload = match_payload("1-tyhja")
    payload["teams"]["faction1"]["roster"] = [
        {"player_id": "p-1", "nickname": "   "},
        {"player_id": "p-2"},
        {"nickname": "ei-tunnistetta"},
    ]
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    team = client.get_match("1-tyhja").teams[0]

    # Pelaaja ilman tunnistetta pudotetaan: tunniste on koneen avain, nimimerkki
    # ei ole. Ilman sitä pelaajaa ei voi liittää mihinkään.
    assert [p.player_id for p in team.roster] == ["p-1", "p-2"]
    assert [p.nickname for p in team.roster] == [None, None]


def test_a_match_without_an_id_stops_the_run(tmp_path: Path) -> None:
    """Ilman ``match_id``:tä ottelua ei voi liittää mihinkään."""
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, {"status": "X"}))

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert "match_id" in str(exc.value)


# -- Portti ja kerrokset -----------------------------------------------------


def test_only_api_errors_leave_the_port(tmp_path: Path) -> None:
    """Portin ainoa lupaus on :class:`ApiError`.

    Injektoitu kuljetus voi nostaa mitä tahansa, eikä sen tyyppi saa vuotaa
    vaiheeseen asti: AD-9:n mukaan yksikön virhe on ``status``, ei jäljitys
    ruudulle.
    """
    session = FakeSession(ZeroDivisionError("kuljetus rikki"))
    client = FaceitClient(KEY, tmp_path, base_url=BASE, session=session)

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert "ZeroDivisionError" in str(exc.value)
    # Ei uudelleenyritystä: kuljetuksen vika ei ole ohimenevä ruuhka.
    assert exc.value.attempts == 1


def test_a_transport_without_a_get_method_is_an_api_error(tmp_path: Path) -> None:
    """Sekään, mitä kuljetus ei osaa, ei saa vuotaa muuna tyyppinä."""
    client = FaceitClient(KEY, tmp_path, base_url=BASE, session=object())

    with pytest.raises(ApiError):
        client.get_match("1-aaaa")


def test_the_client_implements_the_port(tmp_path: Path) -> None:
    """Vaihe näkee portin, ei asiakasta.

    ``runtime_checkable`` tarkistaa **vain metodinimien olemassaolon**, ei
    allekirjoituksia eikä paluutyyppejä -- se on Pythonin rajoite eikä tämän
    testin valinta, ja se on sanottava ääneen, jottei testi lupaa vartijaa
    jota ei ole. Testi on silti arvokas: se kaataa nimenmuutoksen, joka tekisi
    asiakkaasta portin ulkopuolisen. Allekirjoitusten yhtäpitävyyden todistavat
    porttia käyttävät testit, eivät tämä.
    """
    client, _session, _waits = make_client(tmp_path)
    assert isinstance(client, MatchSource)


def test_the_client_closes_only_the_session_it_opened(tmp_path: Path) -> None:
    """Injektoitu kuljetus on kutsujan omaisuutta.

    Sen sulkeminen katkaisisi yhteydet, joita tämä luokka ei avannut.
    """
    session = FakeSession()
    with FaceitClient(KEY, tmp_path, base_url=BASE, session=session):
        pass
    assert session.closed is False

    own = FaceitClient(KEY, tmp_path, base_url=BASE)
    own.close()  # ei saa kaatua


def _third_party_imports(source: str, filename: str) -> set[str]:
    """Kerää tiedoston tuomat ylätason paketit -- **molemmat tuontimuodot**.

    Pelkkä ``"import requests" in source`` päästäisi läpi
    ``from requests import get``.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def test_no_layer_but_the_adapter_imports_requests_or_tenacity() -> None:
    """Domain ei tunne HTTP:tä -- se näkee portin, ei asiakasta.

    ``tests/test_layering.py`` valvoo pakettien välisiä nuolia; tämä valvoo
    kirjastoja, joita nuoli ei kata: ``requests`` voisi valua domainiin
    rikkomatta yhtäkään kerrossääntöä.

    **Kolme pakettia eikä yksi.** ``render`` ei saa laskea mitään eikä
    ``archive`` tuntea lähteitä, joten kumpikaan ei saa tuntea verkkoa sen
    paremmin kuin ``domain``. Ilman niitä sääntö olisi kirjoitettu vain sinne,
    mistä se sattui löytymään.
    """
    src = Path(faceit_module.__file__).resolve().parents[1]
    forbidden = {"requests", "tenacity"}
    for package in ("domain", "render", "archive"):
        for path in (src / package).rglob("*.py"):
            imported = _third_party_imports(
                path.read_text(encoding="utf-8"), str(path)
            )
            leaked = imported & forbidden
            assert not leaked, f"{path.name} tuo verkkokirjaston: {sorted(leaked)}"


# -- Asetusosio --------------------------------------------------------------


def test_the_faceit_section_is_a_typed_model_of_its_own(settings_file: Path) -> None:
    """AD-3: ``[faceit]`` on oma osionsa, jonka arvot ovat asetuksia."""
    settings = load_settings(settings_file, env_files=())

    assert isinstance(settings.faceit, FaceitSettings)
    assert settings.faceit.retry_attempts >= 1
    assert settings.faceit.page_size <= MAX_FACEIT_PAGE_SIZE
    # Avaimet eivät ole osiossa: ne luetaan koneen omasta .env-tiedostosta.
    assert not hasattr(settings.faceit, "api_key")
    assert not hasattr(settings.faceit, "base_url")


def test_faceit_defaults_match_the_settings_file(settings_file: Path) -> None:
    """Koodioletus ei saa erota asetustiedostosta.

    Repon konventio (vrt. ``test_report_defaults_match_the_settings_file``):
    jos koodioletus eroaisi tiedostosta, unohtunut avain käyttäytyisi eri
    tavalla kuin tiedosto sanoo -- eikä mikään kertoisi, kummasta ajo syntyi.
    """
    assert FaceitSettings() == load_settings(settings_file, env_files=()).faceit


def test_every_faceit_value_is_written_out_in_the_settings_file() -> None:
    """Jokainen asetus kirjoitetaan näkyviin, koska koodin oletus ei näy.

    Sama sääntö kuin osioiden pakollisuudella: arvo, joka on vain koodissa, ei
    näy tiedostossa jota säädetään.
    """
    data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
    assert set(data["faceit"]) == set(FaceitSettings.model_fields)


def test_the_settings_file_describes_the_split_cache() -> None:
    """Sääntö on kirjoitettava sinne, mistä käyttäjä sen löytää.

    Kumpikin puoli erikseen: pelkkä "välimuisti on eriytetty" ei kerro
    kummalle puolelle kumpi sääntö kuuluu, ja juuri se on koko päätös.
    Aiempi versio kuvasi vanhaa käyttäytymistä ("VÄLIMUISTI EI VANHENE",
    "kerran haettu ottelulista ei päivity"), joka on nyt väärä.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    section = text.split("[faceit]", 1)[1].split("\n[", 1)[0]

    assert "EI VÄLIMUISTITETA LAINKAAN" in section
    assert "FINISHED" in section
    assert "raw/faceit/" in section
    # Sääntö on symmetrinen, ja se on kirjoitettava näkyviin: pelkkä
    # kirjoitusehto jättäisi lukijan uskomaan, että vanha tiedosto voi jäädä
    # tarjoilemaan vanhentunutta vastausta.
    assert "ITSEKORJAAVA" in section
    assert "SAMA EHTO KOSKEE LUKEMISTA" in section
    # Vanhat, nyt väärät väitteet eivät saa jäädä tiedostoon.
    assert "VÄLIMUISTI EI VANHENE" not in section
    assert "tyhjennetään käsin" not in section
    assert "tarvitse tyhjentää käsin" not in section


def test_the_readme_describes_the_split_cache() -> None:
    """Sama sääntö READMEssa: se on ainoa paikka, jota luetaan ilman koodia."""
    readme = REAL_SETTINGS.parent / "README.md"
    text = readme.read_text(encoding="utf-8")

    assert "FACEIT-välimuisti on eriytetty kutsun lajin mukaan" in text
    assert "/championships/{id}/matches" in text
    assert "/matches/{id}" in text
    assert "FINISHED" in text
    assert "CANCELLED" in text
    assert "itsekorjaava" in text
    assert "Sama ehto koskee **lukemista**" in text
    # Vanhat, nyt väärät otsikot ja lupaukset eivät saa jäädä.
    assert "FACEIT-välimuisti ei vanhene" not in text
    assert "ei päivity itsestään" not in text
    assert "on poistettava kerran" not in text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": MAX_FACEIT_PAGE_SIZE + 1},
        {"page_size": 0},
        {"retry_attempts": MAX_FACEIT_RETRY_ATTEMPTS + 1},
        {"retry_attempts": 0},
        {"retry_jitter_share": 1.5},
        {"retry_initial_delay_seconds": -1.0},
        {"retry_initial_delay_seconds": 10.0, "retry_max_delay_seconds": 2.0},
        {"call_budget_seconds": -1.0},
    ],
)
def test_the_client_refuses_what_the_settings_model_refuses(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    """**Ei hiljaista clamppausta.**

    ``max(1, int(page_size))`` päästi ennen läpi arvon 1000 ja tuotti juuri
    sen 400-virheen, jonka rajan perustelu sanoo näyttävän verkkovialta. Sama
    koski kattoa 0.0, joka leikkasi jokaisen odotuksen pois niin, että
    uudelleenyritys näytti asetetulta muttei tapahtunut.
    """
    with pytest.raises(SettingsError):
        FaceitClient(KEY, tmp_path, base_url=BASE, **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page_size": MAX_FACEIT_PAGE_SIZE + 1},
        {"retry_attempts": MAX_FACEIT_RETRY_ATTEMPTS + 1},
        {"retry_jitter_share": 1.5},
        {"retry_initial_delay_seconds": 10.0, "retry_max_delay_seconds": 2.0},
        {"call_budget_seconds": 1.0},
    ],
)
def test_the_settings_model_refuses_the_same_values(kwargs: dict[str, Any]) -> None:
    """Sama raja molemmissa päissä, jottei kumpikaan vartioi yksin.

    Erityisesti ``MAX_FACEIT_RETRY_ATTEMPTS``in ``le=``-raja oli aiemmin ilman
    testiä, vaikka sisarrajoilla oli omansa: sen olisi voinut poistaa ilman
    että mikään kaatuu.
    """
    with pytest.raises(ValueError):
        FaceitSettings(**kwargs)


def test_a_budget_smaller_than_one_wait_is_rejected() -> None:
    """Budjetti, joka ei kata yhtä odotusta, tekisi uudelleenyrityksestä kulissin."""
    with pytest.raises(ValueError) as exc:
        FaceitSettings(retry_max_delay_seconds=30.0, call_budget_seconds=10.0)

    assert "call_budget_seconds" in str(exc.value)
