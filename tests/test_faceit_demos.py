"""FACEITin demolähteen testit -- kaikki offline (Story 3.4).

Sama rakenne kuin ``test_faceit.py``:ssä: :func:`_no_network` estää jokaisen
oikean HTTP-kutsun koko moduulin ajaksi, ja jokainen kutsu kulkee käsin
kirjoitetun kuljetuksen läpi.

Tämän tiedoston tärkein testijoukko on **linkkivuoto**. Signattu latauslinkki
on valtuutus tiedostoon, ei osoite: se, jolla se on, saa demon. Jos se päätyisi
metatiedostoon, lokiin, välimuistiin tai virheilmoitukseen, se olisi
jaettavassa OneDrive-arkistossa ja versiohistoriassa. Testit etsivät siksi
linkin tunnistetta *kaikesta*, mitä ajo jättää jälkeensä -- myös poikkeusten
``__cause__``-ketjusta, jossa ``requests``in oma viesti kantaisi osoitteen.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from pappascout.adapters.decompress import ZSTD_MAGIC
from pappascout.adapters.faceit import (
    DEMO_READY_STATUSES,
    DOWNLOADS_APPLICATION_URL,
    DOWNLOADS_STATUS_URL,
    FaceitClient,
    FaceitDemoSource,
    split_map_demo_id,
)
from pappascout.adapters.protocols import DemoSource
from pappascout.errors import (
    ApiError,
    DemoUnavailable,
    DownloadsAccessDenied,
    SettingsError,
)
from pappascout.stages import fetch as fetch_stage
from pappascout.archive.paths import ArchivePaths

KEY = "salainen-avain-XYZZY-42"
TOKEN = "downloads-token-QUUX-77"

#: Tunniste, joka esiintyy **vain** signatussa linkissä. Geneerinen sana
#: osuisi kommentteihin ja tekisi vartijasta hampaattoman.
SIGNATURE = "SIGNATURE-ZORK-9f3a1c07"

BASE = "https://faceit.invalid/data/v4"
DOWNLOADS = "https://faceit.invalid/download/v2"

MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"
UNIT = f"{MATCH}-0"
SECOND = f"{MATCH}-1"

CDN = "https://demos-europe-central.backblaze.faceit-cdn.net/cs2"
SIGNED = f"https://cdn.invalid/demo.dem.zst?token={SIGNATURE}"

#: Uskottava pakattu demo: zstd-taikatavut ja yli megatavun koko.
#:
#: Vaihe hylkää sisällön, joka ei ala zstd-taikatavuilla tai on liian pieni
#: ollakseen CS2-demo (HTML-virhesivu 200-statuksella, tyhjä runko). Läpi
#: vaiheen ajettavan testin aineiston on siis läpäistävä sama portti kuin
#: oikean demon.
DEMO_BYTES = (ZSTD_MAGIC + b"zstd-demo-tavuja" * 70_000)[: 1024 * 1024 + 4096]

#: Ottelun päättymishetki: 45 vrk sitten, eli selvästi säilytysajan takana.
#: Lasketaan ajohetkestä, jotta testi ei vanhene kalenterin mukana.
FINISHED_AT = int(
    (datetime.now(UTC) - timedelta(days=45)).timestamp()
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Katkaise oikea HTTP koko moduulin ajaksi."""

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Testi yritti mennä verkkoon.")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _refuse)


# -- Kiinnikkeet -------------------------------------------------------------


def match_payload(
    *, status: str = "FINISHED", rounds: tuple[int, ...] = (1, 2)
) -> dict[str, Any]:
    """Ottelun raakavastaus mitatun muodon mukaisena (luku 9, 2026-09-05)."""
    return {
        "match_id": MATCH,
        "status": status,
        "best_of": 2,
        # Epoch-sekunnit kuten FACEIT ne antaa; poissaolon syy laskee iän tästä.
        "finished_at": FINISHED_AT,
        "competition_id": "kilpailu",
        "voting": {"map": {"pick": ["de_ancient", "de_nuke"]}},
        "teams": {},
        "instances": [
            {
                "id": f"{MATCH}-{r}-1",
                "round": r,
                "demos": [f"{CDN}/{MATCH}-{r}-1.dem.zst"],
            }
            for r in rounds
        ],
    }


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        stream_error: Exception | None = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._body = body or b""
        self.headers = dict(headers or {})
        self._stream_error = stream_error
        self.closed = False
        #: Rungon teksti. Virhevastauksen runko ei aina ole JSONia, ja juuri
        #: siitä poimitaan rajapinnan oma virheteksti.
        self.text = text if text is not None else ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload

    def iter_content(self, chunk_size: int = 1):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start : start + chunk_size]
        if self._stream_error is not None:
            raise self._stream_error

    def close(self) -> None:
        self.closed = True


class FakeSession:
    """Kuljetus, joka vastaa osoitteen perusteella eikä jonosta.

    Osoiteperusteinen siksi, että demolataus tekee kolme eri kutsua eri
    protokollilla (``GET`` ottelu, ``POST`` linkki, ``GET`` tavut), ja jono
    piilottaisi sen, jos ne menisivät väärään järjestykseen tai väärään
    osoitteeseen.
    """

    def __init__(
        self,
        *,
        match: Any = None,
        sign: Any = None,
        download: Any = None,
    ) -> None:
        self.match = match if match is not None else FakeResponse(
            200, match_payload()
        )
        self.sign = sign
        self.download = download
        self.gets: list[SimpleNamespace] = []
        self.posts: list[SimpleNamespace] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.gets.append(SimpleNamespace(url=url, **kwargs))
        if url.startswith(BASE):
            return _pop(self.match)
        return _pop(self.download)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.posts.append(SimpleNamespace(url=url, **kwargs))
        return _pop(self.sign)


def _pop(entry: Any) -> FakeResponse:
    """Vastaus, poikkeus tai lista niistä."""
    if isinstance(entry, list):
        entry = entry.pop(0)
    if isinstance(entry, Exception):
        raise entry
    if entry is None:
        raise AssertionError("Kuljetukselle ei annettu vastausta tähän kutsuun.")
    return entry


def signed_response(payload: Any = None) -> FakeResponse:
    return FakeResponse(200, payload or {"payload": {"download_url": SIGNED}})


def build(
    tmp_path: Path,
    session: FakeSession,
    *,
    retry_attempts: int = 1,
    chunk_bytes: int = 64,
) -> FaceitDemoSource:
    client = FaceitClient(
        api_key=KEY,
        cache_dir=tmp_path / "raw",
        base_url=BASE,
        retry_attempts=retry_attempts,
        session=session,
        sleep=lambda _s: None,
    )
    return FaceitDemoSource(
        client,
        TOKEN,
        downloads_base_url=DOWNLOADS,
        chunk_bytes=chunk_bytes,
    )


def download(source: FaceitDemoSource, unit: str = UNIT) -> bytes:
    with source.get_demo(unit) as stream:
        return b"".join(stream.chunks)


# -- Tunnisteen purku --------------------------------------------------------


def test_the_identifier_is_split_at_the_last_hyphen_not_the_first() -> None:
    """``match_id`` on itsessään ``1-<uuid>``: viisi väliviivaa ennen indeksiä."""
    assert split_map_demo_id(UNIT) == (MATCH, 0)
    assert split_map_demo_id(SECOND) == (MATCH, 1)


@pytest.mark.parametrize("bad", ["", "eiviivaa", f"{MATCH}-", "-0", f"{MATCH}-x"])
def test_a_malformed_identifier_is_no_demo_not_an_api_error(bad: str) -> None:
    with pytest.raises(DemoUnavailable):
        split_map_demo_id(bad)


# -- instances ratkaisee kartan ----------------------------------------------


def test_the_instance_is_chosen_by_round_not_by_list_position(tmp_path) -> None:
    """``round == map_index + 1``; listapositiota ei lasketa.

    Aineisto on käännetty ympäri (kartta 2 ensin), jolloin positioon nojaava
    haku antaisi kartalle 0 kartan 2 tallenteen -- ja demo tallentuisi väärän
    kartan nimellä ilman että mikään kertoisi siitä.
    """
    session = FakeSession(
        match=FakeResponse(200, match_payload(rounds=(2, 1))),
        sign=signed_response(),
        download=FakeResponse(200, body=DEMO_BYTES),
    )
    source = build(tmp_path, session)

    download(source, UNIT)

    exchanged = session.posts[0].json["resource_url"]
    assert exchanged == f"{CDN}/{MATCH}-1-1.dem.zst"


def test_the_second_map_asks_for_round_two(tmp_path) -> None:
    session = FakeSession(sign=signed_response(), download=FakeResponse(200, body=b"x"))
    source = build(tmp_path, session)

    download(source, SECOND)

    assert session.posts[0].json["resource_url"] == f"{CDN}/{MATCH}-2-1.dem.zst"


def test_a_map_that_was_never_played_is_no_demo_and_says_which_rounds_exist(
    tmp_path,
) -> None:
    """2-0 päättynyt BO3: vedossa kolme karttaa, instansseja kaksi."""
    session = FakeSession(match=FakeResponse(200, match_payload(rounds=(1, 2))))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(f"{MATCH}-2")

    message = str(excinfo.value)
    assert "kartta 3" in message.lower() or "karttaa 3" in message
    assert "1, 2" in message
    assert session.posts == []


def test_an_instance_without_a_demo_is_a_different_reason_than_a_missing_one(
    tmp_path,
) -> None:
    payload = match_payload()
    payload["instances"][0]["demos"] = []
    session = FakeSession(match=FakeResponse(200, payload))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(UNIT)

    assert "ei tallennetta" in str(excinfo.value)


def test_an_empty_first_instance_does_not_hide_a_later_one(tmp_path) -> None:
    """**A8.** Ensimmäinen osuma ei ole viimeinen sana.

    Sama kartta voi esiintyä useammalla instanssirivillä (uusinta, keskeytynyt
    tallennus). Ensimmäiseen pysähtyminen ilmoittaisi "ei tallennetta" vaikka
    seuraavalla rivillä on osoite -- ja ``no_demo`` on lopullinen tila, joten
    virhe olisi pysyvä.
    """
    payload = match_payload()
    payload["instances"] = [
        {"id": f"{MATCH}-1-1", "round": 1, "demos": []},
        {"id": f"{MATCH}-1-2", "round": 1, "demos": [f"{CDN}/{MATCH}-1-2.dem.zst"]},
    ]
    session = FakeSession(
        match=FakeResponse(200, payload),
        sign=signed_response(),
        download=FakeResponse(200, body=DEMO_BYTES),
    )
    source = build(tmp_path, session)

    download(source, UNIT)

    assert session.posts[0].json["resource_url"] == f"{CDN}/{MATCH}-1-2.dem.zst"


def test_two_different_recordings_for_one_map_are_not_chosen_silently(
    tmp_path,
) -> None:
    """Monitulkintaisuutta ei ratkaista arvaamalla -- sama sääntö kuin nimihaussa.

    Väärä tallenne tallentuisi oikean nimellä, eikä mikään kertoisi siitä.
    """
    payload = match_payload()
    payload["instances"] = [
        {"id": f"{MATCH}-1-1", "round": 1, "demos": [f"{CDN}/eka.dem.zst"]},
        {"id": f"{MATCH}-1-2", "round": 1, "demos": [f"{CDN}/toka.dem.zst"]},
    ]
    session = FakeSession(match=FakeResponse(200, payload))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert "2 eri tallennetta" in message
    assert "eka.dem.zst" in message
    assert "toka.dem.zst" in message
    assert session.posts == []


def test_the_same_url_twice_is_not_an_ambiguity(tmp_path) -> None:
    """Kahdesti listattu sama osoite on yksi tallenne, ei kaksi."""
    payload = match_payload()
    url = f"{CDN}/{MATCH}-1-1.dem.zst"
    payload["instances"] = [
        {"id": f"{MATCH}-1-1", "round": 1, "demos": [url]},
        {"id": f"{MATCH}-1-1", "round": 1, "demos": [url]},
    ]
    session = FakeSession(
        match=FakeResponse(200, payload),
        sign=signed_response(),
        download=FakeResponse(200, body=DEMO_BYTES),
    )

    download(build(tmp_path, session), UNIT)

    assert session.posts[0].json["resource_url"] == url


@pytest.mark.parametrize("status", ["ONGOING", "SCHEDULED", "CANCELLED"])
def test_a_match_that_is_not_finished_is_no_demo_and_nothing_is_downloaded(
    tmp_path, status: str
) -> None:
    assert status not in DEMO_READY_STATUSES
    session = FakeSession(match=FakeResponse(200, match_payload(status=status)))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(UNIT)

    assert status in str(excinfo.value)
    assert session.posts == []


# -- Uudelleenyritys ---------------------------------------------------------


def test_a_404_is_never_retried(tmp_path) -> None:
    """Poissa oleva demo on tosiasia, ei häiriö.

    FACEIT säilyttää tallenteet noin 30 päivää; odottaminen ei tuo takaisin
    poistettua tiedostoa, mutta se kuluttaa Downloads-kiintiötä varmasti
    turhaan.
    """
    session = FakeSession(
        sign=signed_response(),
        download=[FakeResponse(404), FakeResponse(200, body=DEMO_BYTES)],
    )
    source = build(tmp_path, session, retry_attempts=5)

    with pytest.raises(DemoUnavailable):
        source.get_demo(UNIT)

    # Yksi lataushaku, eikä toista: ottelun haku on eri osoitteessa.
    downloads = [g for g in session.gets if not g.url.startswith(BASE)]
    assert len(downloads) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_demo_is_no_demo_and_says_how_old_the_match_is(
    tmp_path, status: int
) -> None:
    """**A1.** 404 tarkoittaa "ei ole", ei "yritä uudelleen".

    ``ApiError``ina vaihe merkitsisi yksikön tilaan ``download_failed`` ja
    kehottaisi ajamaan komennon uudelleen -- ja jokainen uusi ajo tekisi ensin
    signauskutsun eli kuluttaisi Downloads-kiintiötä demolle, joka ei palaa.

    Syy kertoo myös iän, koska "ei löytynyt" ei kerro onko kyseessä odotettu
    vanheneminen vai jokin muu. Luku on ``match_payload``issa jo valmiina.
    """
    session = FakeSession(sign=signed_response(), download=FakeResponse(status))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert str(status) in message
    assert "45 päivää" in message
    assert "30 päivää" in message
    assert UNIT in message


def test_a_404_from_the_link_exchange_is_also_no_demo(tmp_path) -> None:
    """Poissaolo voi paljastua jo linkkiä vaihdettaessa."""
    session = FakeSession(sign=FakeResponse(404))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable):
        source.get_demo(UNIT)


def test_a_gone_demo_without_a_finish_time_does_not_invent_an_age(
    tmp_path,
) -> None:
    """Puuttuvaa lukua ei korvata: keksitty ikä näyttäisi mittaukselta."""
    payload = match_payload()
    del payload["finished_at"]
    session = FakeSession(
        match=FakeResponse(200, payload),
        sign=signed_response(),
        download=FakeResponse(404),
    )
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(UNIT)

    assert "ikää ei voi kertoa" in str(excinfo.value)


def test_a_403_is_still_a_download_failure_not_a_missing_demo(tmp_path) -> None:
    """Väärä token ei tarkoita, ettei demoa ole -- ja ero on käyttäjälle iso.

    ``no_demo`` on lopullinen: se ei yritä uudelleen koskaan. Jos
    valtuutusvirhe päätyisi siihen tilaan, koko otanta merkittäisiin
    olemattomaksi yhden väärän rivin takia .env-tiedostossa.
    """
    session = FakeSession(sign=signed_response(), download=FakeResponse(403))
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    assert excinfo.value.status_code == 403


def test_a_429_is_retried_with_the_story_3_1_policy(tmp_path) -> None:
    session = FakeSession(
        sign=signed_response(),
        download=[FakeResponse(429), FakeResponse(200, body=DEMO_BYTES)],
    )
    source = build(tmp_path, session, retry_attempts=3)

    assert download(source) == DEMO_BYTES


def test_a_5xx_from_the_link_exchange_is_retried(tmp_path) -> None:
    session = FakeSession(
        sign=[FakeResponse(503), signed_response()],
        download=FakeResponse(200, body=DEMO_BYTES),
    )
    source = build(tmp_path, session, retry_attempts=3)

    assert download(source) == DEMO_BYTES
    assert len(session.posts) == 2


def test_the_bytes_arrive_whole_and_the_length_comes_from_the_header(
    tmp_path,
) -> None:
    session = FakeSession(
        sign=signed_response(),
        download=FakeResponse(
            200, body=DEMO_BYTES, headers={"Content-Length": str(len(DEMO_BYTES))}
        ),
    )
    source = build(tmp_path, session)

    with source.get_demo(UNIT) as stream:
        assert stream.content_length == len(DEMO_BYTES)
        assert b"".join(stream.chunks) == DEMO_BYTES


def test_a_missing_content_length_is_none_not_zero(tmp_path) -> None:
    session = FakeSession(
        sign=signed_response(), download=FakeResponse(200, body=DEMO_BYTES)
    )
    with build(tmp_path, session).get_demo(UNIT) as stream:
        assert stream.content_length is None


def test_a_stream_that_breaks_mid_download_raises_an_api_error(tmp_path) -> None:
    session = FakeSession(
        sign=signed_response(),
        download=FakeResponse(
            200,
            body=DEMO_BYTES,
            stream_error=requests.exceptions.ChunkedEncodingError(
                f"connection broken while reading {SIGNED}"
            ),
        ),
    )
    source = build(tmp_path, session)

    with pytest.raises(ApiError):
        download(source)


def test_the_port_is_satisfied_by_the_real_adapter(tmp_path) -> None:
    session = FakeSession(sign=signed_response(), download=FakeResponse(200))
    assert isinstance(build(tmp_path, session), DemoSource)


# -- Salaisuudet: token ja signattu linkki -----------------------------------


def test_the_token_is_sent_only_to_the_downloads_api(tmp_path) -> None:
    session = FakeSession(
        sign=signed_response(), download=FakeResponse(200, body=DEMO_BYTES)
    )
    source = build(tmp_path, session)

    download(source)

    assert session.posts[0].headers["Authorization"] == f"Bearer {TOKEN}"
    for call in session.gets:
        headers = getattr(call, "headers", None) or {}
        assert TOKEN not in json.dumps(dict(headers))


def test_the_download_get_carries_no_credential_at_all(tmp_path) -> None:
    """**B4.** Valtuutus on osoitteessa; otsaketta ei ole eikä pidä olla.

    Kaksi eri vikaa, jotka molemmat menivät aiemmin läpi koko sarjasta:
    salaisuus **osoitteeseen** liitettynä (vartija katsoi vain otsakkeita) ja
    Data API:n avain lähetettynä **CDN-osoitteeseen** (mitään ei tarkistettu).
    ``allow_redirects=True`` tekee molemmista erityisen ikäviä: kumpikin
    seuraisi ohjausta mihin tahansa isäntään.
    """
    session = FakeSession(
        sign=signed_response(), download=FakeResponse(200, body=DEMO_BYTES)
    )
    source = build(tmp_path, session)

    download(source)

    downloads = [g for g in session.gets if not g.url.startswith(BASE)]
    assert len(downloads) == 1
    call = downloads[0]

    # 1) Ei valtuutusotsaketta -- ei tokenia eikä avainta, ei minkäänlaista.
    headers = getattr(call, "headers", None) or {}
    assert not headers, f"lataus-GET lähetti otsakkeita: {headers}"

    # 2) Salaisuus ei ole osoitteessa. Signattu linkki on, ja se on tarkoitus;
    #    kumpikaan .env-tiedoston tunniste ei ole.
    assert TOKEN not in call.url
    assert KEY not in call.url
    assert call.url == SIGNED

    # 3) Ottelun haku sen sijaan kantaa avaimen otsakkeessa -- muuten testi
    #    voisi mennä läpi sillä, ettei kuljetus välitä otsakkeita lainkaan.
    api_calls = [g for g in session.gets if g.url.startswith(BASE)]
    assert api_calls, "ottelua ei haettu -- testi ei mittaa eroa"
    assert api_calls[0].headers["Authorization"] == f"Bearer {KEY}"


def test_the_api_key_is_never_sent_to_the_cdn(tmp_path) -> None:
    """Data API:n avain kuuluu vain Data API:in."""
    session = FakeSession(
        sign=signed_response(), download=FakeResponse(200, body=DEMO_BYTES)
    )

    download(build(tmp_path, session))

    for call in session.gets:
        if call.url.startswith(BASE):
            continue
        rendered = json.dumps(dict(getattr(call, "headers", None) or {})) + call.url
        assert KEY not in rendered
        assert TOKEN not in rendered


def test_neither_secret_is_in_the_repr(tmp_path) -> None:
    session = FakeSession()
    source = build(tmp_path, session)
    assert TOKEN not in repr(source)
    assert KEY not in repr(source)


def test_the_signed_link_is_not_in_the_cache_or_in_any_file(tmp_path) -> None:
    """Välimuistiin kirjoitetaan ottelun vastaus -- ei latauslinkkiä."""
    session = FakeSession(
        sign=signed_response(), download=FakeResponse(200, body=DEMO_BYTES)
    )
    source = build(tmp_path, session)

    download(source)

    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert SIGNATURE not in path.read_bytes().decode("utf-8", "replace")


def test_the_signed_link_is_not_in_any_error_message_or_exception_chain(
    tmp_path,
) -> None:
    """Ketju lasketaan mukaan: ``requests``in oma viesti kantaa osoitteen.

    ``raise ... from exc`` liittäisi sen syyksi, ja jokainen jäljitys, joka
    tämän virheen tulostaa, näyttäisi valtuutuksen kokonaisuudessaan.
    """
    session = FakeSession(
        sign=signed_response(),
        download=requests.exceptions.ConnectionError(
            f"HTTPSConnectionPool: Max retries exceeded with url: {SIGNED}"
        ),
    )
    source = build(tmp_path, session, retry_attempts=1)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    _assert_no_signature(excinfo.value)


def test_the_signed_link_is_not_in_the_error_when_the_stream_breaks(
    tmp_path,
) -> None:
    session = FakeSession(
        sign=signed_response(),
        download=FakeResponse(
            200,
            body=DEMO_BYTES,
            stream_error=requests.exceptions.ChunkedEncodingError(
                f"connection broken while reading {SIGNED}"
            ),
        ),
    )
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        download(source)

    _assert_no_signature(excinfo.value)


def test_the_signed_link_is_not_in_the_error_when_the_download_is_rejected(
    tmp_path,
) -> None:
    session = FakeSession(sign=signed_response(), download=FakeResponse(403))
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    _assert_no_signature(excinfo.value)
    assert excinfo.value.url is None


def _assert_no_signature(error: BaseException) -> None:
    """Käy poikkeusketju läpi ja vaadi, ettei allekirjoitus ole missään."""
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        for rendering in (str(current), repr(current), str(getattr(current, "url", ""))):
            assert SIGNATURE not in rendering, (
                f"signattu linkki vuoti: {type(current).__name__}"
            )
        current = current.__cause__ or current.__context__
    assert seen, "ketju oli tyhjä -- testi ei mittaa mitään"


# -- Läpi vaiheen: mitään ei jää arkistoon -----------------------------------


def test_a_full_run_through_the_stage_leaves_no_trace_of_the_signed_link(
    tmp_path,
) -> None:
    """Adapteri ja vaihe yhdessä: linkki ei ole levyllä eikä tuloksessa.

    Tämä on se testi, joka kattaa metatiedoston: se syntyy vaiheessa, ja jos
    portti joskus alkaisi palauttaa osoitteen, se päätyisi juuri sinne.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    session = FakeSession(
        sign=signed_response(),
        download=FakeResponse(
            200, body=DEMO_BYTES, headers={"Content-Length": str(len(DEMO_BYTES))}
        ),
    )
    source = build(tmp_path, session)

    result = fetch_stage.run(
        archive, UNIT, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert result.status == "ok"
    assert archive.demo(UNIT).read_bytes() == DEMO_BYTES
    assert SIGNATURE not in json.dumps(result.stats)
    assert SIGNATURE not in repr(result)
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != archive.demo(UNIT):
            assert SIGNATURE not in path.read_bytes().decode("utf-8", "replace")


# -- Asetukset ---------------------------------------------------------------


def test_a_missing_downloads_token_stops_the_run_with_finnish_instructions(
    tmp_path, settings_file, monkeypatch
) -> None:
    from pappascout.domain.models import load_settings

    monkeypatch.setenv("PAPPASCOUT_SETTINGS", str(settings_file))
    monkeypatch.setenv("FACEIT_API_KEY", KEY)
    settings = load_settings()
    archive = ArchivePaths(root=tmp_path / "arkisto")

    with pytest.raises(SettingsError) as excinfo:
        fetch_stage.default_source(settings, archive)

    message = str(excinfo.value)
    assert "FACEIT_DOWNLOADS_TOKEN" in message
    assert ".pappascout" in message


@pytest.mark.parametrize("chunk", [0, -1, 65 * 1024 * 1024])
def test_an_out_of_range_chunk_size_is_refused(tmp_path, chunk: int) -> None:
    """**B10.** Pala on kokonaan muistissa: 200 MB:n pala ei ole virtaus.

    Nolla ja negatiivinen ovat oma vikansa: ``iter_content(chunk_size=0)``
    käyttäytyy kirjastokohtaisesti eikä koskaan halutulla tavalla.
    """
    client = FaceitClient(api_key=KEY, cache_dir=tmp_path, base_url=BASE)
    with pytest.raises(SettingsError):
        FaceitDemoSource(client, TOKEN, chunk_bytes=chunk)


@pytest.mark.parametrize("chunk", [1, 1024 * 1024, 64 * 1024 * 1024])
def test_the_chunk_size_bounds_themselves_are_allowed(tmp_path, chunk: int) -> None:
    client = FaceitClient(api_key=KEY, cache_dir=tmp_path, base_url=BASE)
    assert FaceitDemoSource(client, TOKEN, chunk_bytes=chunk).chunk_bytes == chunk


def test_a_non_https_downloads_url_is_refused(tmp_path) -> None:
    client = FaceitClient(api_key=KEY, cache_dir=tmp_path, base_url=BASE)
    with pytest.raises(SettingsError):
        FaceitDemoSource(client, TOKEN, downloads_base_url="http://faceit.invalid")


# -- Sauma: oikea adapteri oikean vaiheen läpi (A1, 2026-09-05) --------------
#
# Matriisin rivi 5 ("demoa ei enää FACEITillä -> no_demo") oli toteutettu
# kahtena puolikkaana, jotka eivät sopineet yhteen: vaihetestin feikki
# mallinsi poissaolon ``DemoUnavailable``ina, mutta adapteri tuotti sen
# ``ApiError``ina -- ja vaihe päätyi tilaan ``download_failed``. Molemmat
# puolikkaat olivat vihreitä. **Sauma on juuri tässä**, ja siksi nämä testit
# ajavat oikean adapterin oikean vaiheen läpi feikatun kuljetuksen takaa.


def run_stage(tmp_path: Path, session: FakeSession, unit: str = UNIT):
    archive = ArchivePaths(root=tmp_path / "arkisto")
    return archive, fetch_stage.run(
        archive,
        unit,
        source=build(tmp_path, session),
        disk_free=lambda _a: 100 * 1024**3,
    )


def test_a_deleted_demo_becomes_no_demo_all_the_way_through_the_stage(
    tmp_path,
) -> None:
    """404 adapterista -> ``no_demo`` vaiheesta, ei ``download_failed``."""
    session = FakeSession(sign=signed_response(), download=FakeResponse(404))

    archive, result = run_stage(tmp_path, session)

    assert result.status == "no_demo"
    assert archive.find_demo(UNIT) is None
    reason = result.reason or ""
    assert "30 päivää" in reason
    assert "45 päivää" in reason
    # Käyttäjää ei kehoteta ajamaan komentoa uudelleen demolle, joka ei palaa.
    assert "aja komento uudelleen" not in reason.lower()


def test_a_transient_failure_becomes_download_failed_through_the_stage(
    tmp_path,
) -> None:
    """Sama sauma toiseen suuntaan: 503 ei saa muuttua poissaoloksi."""
    session = FakeSession(sign=signed_response(), download=FakeResponse(503))

    _archive, result = run_stage(tmp_path, session)

    assert result.status == "download_failed"


def test_an_unplayed_map_becomes_no_demo_through_the_stage(tmp_path) -> None:
    """2-0 päättynyt BO3: kolmatta karttaa ei pelattu."""
    payload = match_payload(rounds=(1, 2))
    session = FakeSession(match=FakeResponse(200, payload))

    _archive, result = run_stage(tmp_path, session, f"{MATCH}-2")

    assert result.status == "no_demo"
    assert "1, 2" in (result.reason or "")


def test_an_unfinished_match_becomes_no_demo_through_the_stage(tmp_path) -> None:
    session = FakeSession(match=FakeResponse(200, match_payload(status="ONGOING")))

    _archive, result = run_stage(tmp_path, session)

    assert result.status == "no_demo"
    assert "ONGOING" in (result.reason or "")


def test_a_successful_download_becomes_ok_through_the_stage(tmp_path) -> None:
    """Positiivinen kontrolli: sauma toimii myös onnistuessaan.

    Ilman tätä koko saumatestijoukon voisi läpäistä adapteri, joka ei koskaan
    tuota mitään.
    """
    session = FakeSession(
        sign=signed_response(),
        download=FakeResponse(
            200, body=DEMO_BYTES, headers={"Content-Length": str(len(DEMO_BYTES))}
        ),
    )

    archive, result = run_stage(tmp_path, session)

    assert result.status == "ok"
    assert archive.demo(UNIT).read_bytes() == DEMO_BYTES
    assert result.stats["length_verified"] is True


def test_an_html_error_page_from_the_real_adapter_is_not_stored(tmp_path) -> None:
    """Roskavartija saumassa: 200-status ja HTML-runko.

    Tämä on se tapaus, jossa kaikki muu näyttää onnistuneelta -- adapteri ei
    tarkista sisältöä eikä voikaan, koska se ei tiedä mitä kirjoitetaan.
    """
    junk = b"<!doctype html><h1>403</h1>" * 60_000
    session = FakeSession(
        sign=signed_response(),
        download=FakeResponse(
            200, body=junk, headers={"Content-Length": str(len(junk))}
        ),
    )

    archive, result = run_stage(tmp_path, session)

    assert result.status == "download_failed"
    assert archive.find_demo(UNIT) is None
    assert archive.find_demo_meta(UNIT) is None


# -- Valtuutusvika on globaali (C1, C2 -- ensimmäinen oikea ajo 2026-09-05) --
#
# Veetin Downloads API -hakemus oli jonossa, ja Data API -avain ei kelpaa
# Downloads API:in. Ajo tuotti kaksi identtistä 403:a, jotka lajiteltiin
# otsikon "aja komento uudelleen" alle -- neuvo, joka ei auta ennen kuin
# hakemus hyväksytään. Kahdellatoista demolla se olisi ollut kaksitoista
# tuomittua signauskutsua.
#
# Nämä testit ajavat **oikean adapterin oikean vaiheen läpi**, koska juuri se
# sauma erosi: vaihetestin feikki ei voi tuottaa 403:a Downloads API:sta.


def denied_session(status: int = 403) -> FakeSession:
    return FakeSession(sign=FakeResponse(status), download=FakeResponse(200))


@pytest.mark.parametrize("status", [401, 403])
def test_a_denied_downloads_token_is_not_a_unit_status(tmp_path, status: int) -> None:
    """Vika on tunnisteessa, ei demossa -- eikä se siis ole yksikön tila.

    ``download_failed`` tarkoittaa "voi onnistua uudella ajolla". Puuttuva
    Downloads-scope ei voi, ennen kuin FACEIT hyväksyy hakemuksen.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    source = build(tmp_path, denied_session(status))

    with pytest.raises(DownloadsAccessDenied):
        fetch_stage.run(
            archive, UNIT, source=source, disk_free=lambda _a: 100 * 1024**3
        )

    assert archive.find_demo(UNIT) is None


def test_a_denied_token_stops_the_run_after_exactly_one_signing_call(
    tmp_path,
) -> None:
    """**C2:n ydin.** Kahdellatoista demolla tämä oli 12 tuomittua kutsua.

    Sarjan jatkaminen ei ole "sitkeyttä" vaan kiintiön kuluttamista ilman
    mahdollisuutta onnistua: jokainen yksikkö epäonnistuisi identtisesti.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    session = denied_session()
    source = build(tmp_path, session)
    units = [UNIT, SECOND, f"{MATCH}-2"]

    with pytest.raises(DownloadsAccessDenied):
        fetch_stage.run_many(
            archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
        )

    assert len(session.posts) == 1, (
        f"signauskutsuja tehtiin {len(session.posts)}, pitäisi olla 1"
    )


def test_the_interrupted_run_says_what_it_managed_to_do(tmp_path) -> None:
    """Keskeytynyt sarja ei saa näyttää samalta kuin sarja, joka ei alkanut."""
    archive = ArchivePaths(root=tmp_path / "arkisto")
    # Ensimmäinen onnistuu, toinen törmää kieltoon.
    session = FakeSession(
        sign=[signed_response(), FakeResponse(403)],
        download=FakeResponse(
            200, body=DEMO_BYTES, headers={"Content-Length": str(len(DEMO_BYTES))}
        ),
    )
    source = build(tmp_path, session)

    with pytest.raises(DownloadsAccessDenied) as excinfo:
        fetch_stage.run_many(
            archive,
            [UNIT, SECOND, f"{MATCH}-2"],
            source=source,
            disk_free=lambda _a: 100 * 1024**3,
        )

    message = str(excinfo.value)
    assert "1 demoa ehdittiin hakea" in message
    assert "2 jäi hakematta" in message
    # Ensimmäinen demo on levyllä eikä sitä siivota pois.
    assert archive.demo(UNIT).is_file()


def test_the_denied_message_says_where_to_check_and_where_to_apply(
    tmp_path,
) -> None:
    """Käyttäjä ei koodaa itse: viestin on kerrottava seuraava toimenpide.

    Kolme eri asiaa, kolme eri korjausta: hakemus on jonossa (odota), hakemusta
    ei ole (tee se), tai token on väärä (korjaa .env).
    """
    source = build(tmp_path, denied_session())

    with pytest.raises(DownloadsAccessDenied) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert "403" in message
    assert DOWNLOADS_STATUS_URL in message
    assert DOWNLOADS_APPLICATION_URL in message
    assert "FACEIT_DOWNLOADS_TOKEN" in message
    assert "Data API" in message


def test_the_denied_message_does_not_claim_that_waiting_will_not_help(
    tmp_path,
) -> None:
    """Yleinen viesti sanoi "vika ei korjaannu odottamalla" -- ja se on tässä väärin.

    Väite on tosi uudelleenyrityksestä sekunneissa ja epätosi hakemuksesta
    viikoissa. Odottaminen on täsmälleen se, mikä tämän korjaa.
    """
    source = build(tmp_path, denied_session())

    with pytest.raises(DownloadsAccessDenied) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert "ei korjaannu odottamalla" not in message
    assert "Odottaminen" in message


def test_the_denied_message_names_the_key_file_that_was_actually_read(
    tmp_path, settings_file, env_file, monkeypatch
) -> None:
    """Ohje, joka kertoo tiedoston nimen muttei sijaintia, ei ole ohje."""
    from pappascout.domain.models import load_settings

    env = env_file(
        ".env", FACEIT_API_KEY=KEY, FACEIT_DOWNLOADS_TOKEN=TOKEN
    )
    settings = load_settings(settings_file, env_files=(env,))
    client = FaceitClient(
        api_key=KEY,
        cache_dir=tmp_path / "raw",
        base_url=BASE,
        session=denied_session(),
        sleep=lambda _s: None,
    )
    source = FaceitDemoSource.from_settings(
        settings, client, downloads_base_url=DOWNLOADS
    )

    with pytest.raises(DownloadsAccessDenied) as excinfo:
        source.get_demo(UNIT)

    assert str(env) in str(excinfo.value)


def test_a_denied_token_does_not_leak_the_token_itself(tmp_path) -> None:
    """Viesti kertoo mitä korjata, ei mitä tiedostossa lukee."""
    source = build(tmp_path, denied_session())

    with pytest.raises(DownloadsAccessDenied) as excinfo:
        source.get_demo(UNIT)

    assert TOKEN not in str(excinfo.value)
    assert KEY not in str(excinfo.value)


def test_a_403_on_the_signed_link_is_still_a_single_unit_failure(
    tmp_path,
) -> None:
    """**Sama koodi, eri kohta, eri merkitys -- ja se ero on säilytettävä.**

    Downloads API:n 403 tarkoittaa "tokenilla ei ole scopea": yksikään demo ei
    voi onnistua. Signatun linkin 403 tarkoittaa vanhentunutta tai väärin
    muodostettua allekirjoitusta: **yhden** latauksen vika, joka voi hyvinkin
    onnistua uudella linkillä. Jos ne käsiteltäisiin samoin, yksi vanhentunut
    linkki keskeyttäisi koko otannan.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    session = FakeSession(
        sign=signed_response(),
        download=[
            FakeResponse(403),
            FakeResponse(
                200,
                body=DEMO_BYTES,
                headers={"Content-Length": str(len(DEMO_BYTES))},
            ),
        ],
    )
    source = build(tmp_path, session)

    results = fetch_stage.run_many(
        archive,
        [UNIT, SECOND],
        source=source,
        disk_free=lambda _a: 100 * 1024**3,
    )

    assert [r.status for r in results] == ["download_failed", "ok"]
    assert len(session.posts) == 2


# -- 400 signauskutsusta (D2, live-ajo 2026-09-05) --------------------------
#
# Mitattu epämuodostuneella tokenilla. Kolmas statuskoodi kolmesta mitatusta,
# ja ainoa, joka osuu tavalliseen käyttäjään: .env-tiedostoa käsin muokatessa
# lipsahtaa merkki.


def bad_request(payload=None, text: str | None = None) -> FakeResponse:
    return FakeResponse(400, payload, text=text or "")


def test_a_400_from_the_signing_call_is_not_a_missing_demo(tmp_path) -> None:
    """Epämuodostunut pyyntö ei tarkoita, ettei demoa ole.

    ``no_demo`` on lopullinen: se ei yritä uudelleen koskaan. Yksi väärä merkki
    ``.env``-tiedostossa merkitsisi silloin koko otannan olemattomaksi.
    """
    session = FakeSession(sign=bad_request())
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    assert excinfo.value.status_code == 400


def test_a_400_does_not_advise_running_the_command_again(tmp_path) -> None:
    """**D2:n ydin.** Uudelleenajo ei korjaa väärää tunnistetta.

    Vanha viesti lajitteli tämän otsikon "aja komento uudelleen" alle, ja neuvo
    olisi toistunut jokaisella ajolla ikuisesti.
    """
    session = FakeSession(sign=bad_request())
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    advice = excinfo.value.advice or ""
    assert "Uudelleenajo ei auta" in advice
    assert "FACEIT_DOWNLOADS_TOKEN" in advice


def test_a_400_names_both_possible_causes_and_does_not_pick_one(
    tmp_path,
) -> None:
    """400 ei ole yksiselitteinen, eikä työkalu saa väittää tietävänsä kumpi.

    Epämuodostunut tunniste kaataa kaikki demot; epämuodostunut resource_url
    vain yhden. Vastauksesta ei voi päätellä kumpaa -- mutta ajosta voi, ja
    viesti kertoo miten.
    """
    session = FakeSession(sign=bad_request())
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert "FACEIT_DOWNLOADS_TOKEN" in message
    assert "tallenneosoite" in message
    # Sääntö, jolla käyttäjä erottaa syyt -- ei arvaus kummasta on kyse.
    assert "kaikki" in message.lower()
    assert "vain tämä" in message


def test_a_400_shows_faceits_own_error_text_when_there_is_one(
    tmp_path,
) -> None:
    """Rajapinnan oma teksti on havainto; meidän arvauksemme ei ole."""
    session = FakeSession(
        sign=bad_request({"message": "Invalid downloads token format"})
    )
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    assert "Invalid downloads token format" in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        {"errors": [{"message": "resource_url is not valid"}]},
        {"error": "resource_url is not valid"},
        {"detail": "resource_url is not valid"},
    ],
)
def test_the_error_text_is_found_in_several_response_shapes(
    tmp_path, payload
) -> None:
    """FACEITin virherunko ei ole yhtä muotoa; poiminta ei saa olla."""
    session = FakeSession(sign=bad_request(payload))

    with pytest.raises(ApiError) as excinfo:
        build(tmp_path, session).get_demo(UNIT)

    assert "resource_url is not valid" in str(excinfo.value)


def test_a_non_json_error_body_is_shown_but_truncated(tmp_path) -> None:
    """HTML-virhesivu on kilotavuja ja peittäisi ohjeen."""
    session = FakeSession(sign=bad_request(text="<html>" + "x" * 5000))

    with pytest.raises(ApiError) as excinfo:
        build(tmp_path, session).get_demo(UNIT)

    message = str(excinfo.value)
    assert "<html>" in message
    assert len(message) < 2000


def test_a_400_without_any_body_does_not_invent_an_explanation(
    tmp_path,
) -> None:
    """Puuttuvaa havaintoa ei korvata: keksitty selitys on pahempi kuin ei mitään."""
    session = FakeSession(sign=bad_request())

    with pytest.raises(ApiError) as excinfo:
        build(tmp_path, session).get_demo(UNIT)

    assert "FACEIT sanoi" not in str(excinfo.value)


def test_a_400_does_not_leak_the_token(tmp_path) -> None:
    session = FakeSession(sign=bad_request({"message": "bad token"}))

    with pytest.raises(ApiError) as excinfo:
        build(tmp_path, session).get_demo(UNIT)

    assert TOKEN not in str(excinfo.value)


def test_a_400_reaches_the_stage_as_download_failed_with_its_own_advice(
    tmp_path,
) -> None:
    """Sauma: oikea adapteri oikean vaiheen läpi.

    ``download_failed`` on oikea tila -- demo on todennäköisesti olemassa --
    mutta neuvon on oltava tämän vian oma, ei ämpärin oletus.
    """
    archive = ArchivePaths(root=tmp_path / "arkisto")
    session = FakeSession(sign=bad_request({"message": "Invalid token"}))
    source = build(tmp_path, session)

    result = fetch_stage.run(
        archive, UNIT, source=source, disk_free=lambda _a: 100 * 1024**3
    )

    assert result.status == "download_failed"
    assert "Invalid token" in (result.reason or "")
    step = result.stats["next_step"]
    assert "Uudelleenajo ei auta" in step
    assert step != fetch_stage.DEFAULT_NEXT_STEP
    assert archive.find_demo(UNIT) is None
