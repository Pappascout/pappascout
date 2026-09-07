"""Tests for FACEIT's demo source -- all of them offline (Story 3.4).

The same structure as in ``test_faceit.py``: :func:`_no_network` blocks every
real HTTP call for the whole module, and every call goes through a transport
written by hand.

This file's most important set of tests is the **link leak**. A signed
download link is an authorisation to a file, not an address: whoever has it
gets the demo. If it ended up in a metadata file, in a log, in the cache or in
an error message, it would be in a shared archive in a synchronised folder and
in the version history. The tests therefore look for the link's marker in
*everything* a run leaves behind -- the ``__cause__`` chain of exceptions
included, where ``requests``'s own message would carry the address.
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

KEY = "secret-key-XYZZY-42"
TOKEN = "downloads-token-QUUX-77"

#: A marker that appears **only** in the signed link. A generic word would hit
#: the comments and make the guard toothless.
SIGNATURE = "SIGNATURE-ZORK-9f3a1c07"

BASE = "https://faceit.invalid/data/v4"
DOWNLOADS = "https://faceit.invalid/download/v2"

MATCH = "1-f6a06dc8-5c26-4238-b57a-6b357043a5af"
UNIT = f"{MATCH}-0"
SECOND = f"{MATCH}-1"

CDN = "https://demos-europe-central.backblaze.faceit-cdn.net/cs2"
SIGNED = f"https://cdn.invalid/demo.dem.zst?token={SIGNATURE}"

#: A believable compressed demo: zstd magic bytes and over a megabyte in size.
#:
#: The stage rejects content that does not begin with the zstd magic bytes or
#: is too small to be a CS2 demo (an HTML error page with a 200 status, an
#: empty body). The data of a test that runs through the stage therefore has
#: to pass the same gate as a real demo.
DEMO_BYTES = (ZSTD_MAGIC + b"zstd-demo-tavuja" * 70_000)[: 1024 * 1024 + 4096]

#: The match's finish time: 45 days ago, that is, well past the retention
#: period. Computed from the moment of the run, so that the test does not go
#: stale with the calendar.
FINISHED_AT = int(
    (datetime.now(UTC) - timedelta(days=45)).timestamp()
)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut real HTTP off for the whole module."""

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("A test tried to go to the network.")

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _refuse)


# -- Fixtures ----------------------------------------------------------------


def match_payload(
    *, status: str = "FINISHED", rounds: tuple[int, ...] = (1, 2)
) -> dict[str, Any]:
    """A match's raw response in the measured shape (chapter 9, 2026-09-05)."""
    return {
        "match_id": MATCH,
        "status": status,
        "best_of": 2,
        # Epoch seconds as FACEIT gives them; the reason for an absence
        # computes the age from this.
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
        #: The body's text. An error response's body is not always JSON, and
        #: it is exactly where the interface's own error text is picked from.
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
    """A transport that answers by address and not from a queue.

    Address-based because the demo download makes three different calls with
    three different protocols (``GET`` the match, ``POST`` the link, ``GET``
    the bytes), and a queue would hide it if they went in the wrong order or
    to the wrong address.
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
    """A response, an exception, or a list of them."""
    if isinstance(entry, list):
        entry = entry.pop(0)
    if isinstance(entry, Exception):
        raise entry
    if entry is None:
        raise AssertionError("The transport was given no response for this call.")
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


# -- Splitting the id --------------------------------------------------------


def test_the_identifier_is_split_at_the_last_hyphen_not_the_first() -> None:
    """``match_id`` is itself ``1-<uuid>``: five hyphens before the index."""
    assert split_map_demo_id(UNIT) == (MATCH, 0)
    assert split_map_demo_id(SECOND) == (MATCH, 1)


@pytest.mark.parametrize("bad", ["", "nohyphen", f"{MATCH}-", "-0", f"{MATCH}-x"])
def test_a_malformed_identifier_is_no_demo_not_an_api_error(bad: str) -> None:
    with pytest.raises(DemoUnavailable):
        split_map_demo_id(bad)


# -- instances settle the map ------------------------------------------------


def test_the_instance_is_chosen_by_round_not_by_list_position(tmp_path) -> None:
    """``round == map_index + 1``; the position in the list does not count.

    The data is turned around (map 2 first), so that a lookup leaning on the
    position would give map 0 the recording of map 2 -- and the demo would be
    stored under the wrong map's name without anything saying so.
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
    """A BO3 that ended 2-0: three maps in the veto, two instances."""
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
    """**A8.** The first hit is not the last word.

    The same map can appear on several instance rows (a rematch, an
    interrupted recording). Stopping at the first one would report "no
    recording" even though the next row has an address -- and ``no_demo`` is a
    final state, so the error would be permanent.
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
    """Ambiguity is not resolved by guessing -- the same rule as in the name lookup.

    The wrong recording would be stored under the right one's name, and
    nothing would say so.
    """
    payload = match_payload()
    payload["instances"] = [
        {"id": f"{MATCH}-1-1", "round": 1, "demos": [f"{CDN}/first.dem.zst"]},
        {"id": f"{MATCH}-1-2", "round": 1, "demos": [f"{CDN}/second.dem.zst"]},
    ]
    session = FakeSession(match=FakeResponse(200, payload))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert "2 eri tallennetta" in message
    assert "first.dem.zst" in message
    assert "second.dem.zst" in message
    assert session.posts == []


def test_the_same_url_twice_is_not_an_ambiguity(tmp_path) -> None:
    """The same address listed twice is one recording, not two."""
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


# -- Retrying ----------------------------------------------------------------


def test_a_404_is_never_retried(tmp_path) -> None:
    """A demo that is gone is a fact, not a disturbance.

    FACEIT keeps recordings for about 30 days; waiting does not bring back a
    deleted file, but it does spend the Downloads quota, certainly for
    nothing.
    """
    session = FakeSession(
        sign=signed_response(),
        download=[FakeResponse(404), FakeResponse(200, body=DEMO_BYTES)],
    )
    source = build(tmp_path, session, retry_attempts=5)

    with pytest.raises(DemoUnavailable):
        source.get_demo(UNIT)

    # One download fetch and not a second: the match is fetched from a
    # different address.
    downloads = [g for g in session.gets if not g.url.startswith(BASE)]
    assert len(downloads) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_demo_is_no_demo_and_says_how_old_the_match_is(
    tmp_path, status: int
) -> None:
    """**A1.** 404 means "there is none", not "try again".

    As an ``ApiError`` the stage would mark the unit's state
    ``download_failed`` and advise running the command again -- and every new
    run would make the signing call first, that is, spend the Downloads quota
    on a demo that is not coming back.

    The reason also tells the age, because "not found" does not say whether
    this is the expected expiry or something else. The number is already there
    in ``match_payload``.
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
    """An absence can come out already when the link is exchanged."""
    session = FakeSession(sign=FakeResponse(404))
    source = build(tmp_path, session)

    with pytest.raises(DemoUnavailable):
        source.get_demo(UNIT)


def test_a_gone_demo_without_a_finish_time_does_not_invent_an_age(
    tmp_path,
) -> None:
    """A missing number is not substituted for.

    An invented age would look like a measurement.
    """
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
    """A wrong token does not mean there is no demo, and the difference is
    big for the user.

    ``no_demo`` is final: it never tries again. If an authorisation error
    ended up in that state, the whole sample would be marked non-existent
    because of one wrong row in the .env file.
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


# -- Secrets: the token and the signed link ----------------------------------


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
    """**B4.** The authorisation is in the address; there is no header, and
    there must not be.

    Two different faults, both of which used to pass the whole suite: a secret
    attached to the **address** (the guard looked only at the headers) and the
    Data API's key sent to a **CDN address** (nothing was checked at all).
    ``allow_redirects=True`` makes both particularly nasty: either one would
    follow a redirect to any host at all.
    """
    session = FakeSession(
        sign=signed_response(), download=FakeResponse(200, body=DEMO_BYTES)
    )
    source = build(tmp_path, session)

    download(source)

    downloads = [g for g in session.gets if not g.url.startswith(BASE)]
    assert len(downloads) == 1
    call = downloads[0]

    # 1) No authorisation header -- no token and no key, none of any kind.
    headers = getattr(call, "headers", None) or {}
    assert not headers, f"the download GET sent headers: {headers}"

    # 2) The secret is not in the address. The signed link is, and that is
    #    intended; neither id from the .env file is.
    assert TOKEN not in call.url
    assert KEY not in call.url
    assert call.url == SIGNED

    # 3) The match fetch, on the other hand, carries the key in a header --
    #    otherwise the test could pass by the transport not passing headers
    #    on at all.
    api_calls = [g for g in session.gets if g.url.startswith(BASE)]
    assert api_calls, (
        "the match was not fetched -- the test does not measure the difference"
    )
    assert api_calls[0].headers["Authorization"] == f"Bearer {KEY}"


def test_the_api_key_is_never_sent_to_the_cdn(tmp_path) -> None:
    """The Data API's key belongs to the Data API only."""
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
    """What is written to the cache is the match's response -- not the download link."""
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
    """The chain counts too: ``requests``'s own message carries the address.

    ``raise ... from exc`` would attach it as the cause, and every traceback
    that printed this error would show the authorisation in full.
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
    """Walk the exception chain and require that the signature is nowhere in it."""
    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in seen:
        seen.append(current)
        for rendering in (str(current), repr(current), str(getattr(current, "url", ""))):
            assert SIGNATURE not in rendering, (
                f"the signed link leaked: {type(current).__name__}"
            )
        current = current.__cause__ or current.__context__
    assert seen, "the chain was empty -- the test measures nothing"


# -- Through the stage: nothing is left in the archive ------------------------


def test_a_full_run_through_the_stage_leaves_no_trace_of_the_signed_link(
    tmp_path,
) -> None:
    """The adapter and the stage together: the link is neither on disk nor in
    the result.

    This is the test that covers the metadata file: it is born in the stage,
    and if the port ever began returning the address, it would end up exactly
    there.
    """
    archive = ArchivePaths(root=tmp_path / "archive")
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


# -- Settings ----------------------------------------------------------------


def test_a_missing_downloads_token_stops_the_run_with_finnish_instructions(
    tmp_path, settings_file, monkeypatch
) -> None:
    from pappascout.domain.models import load_settings

    monkeypatch.setenv("PAPPASCOUT_SETTINGS", str(settings_file))
    monkeypatch.setenv("FACEIT_API_KEY", KEY)
    settings = load_settings()
    archive = ArchivePaths(root=tmp_path / "archive")

    with pytest.raises(SettingsError) as excinfo:
        fetch_stage.default_source(settings, archive)

    message = str(excinfo.value)
    assert "FACEIT_DOWNLOADS_TOKEN" in message
    assert ".pappascout" in message


@pytest.mark.parametrize("chunk", [0, -1, 65 * 1024 * 1024])
def test_an_out_of_range_chunk_size_is_refused(tmp_path, chunk: int) -> None:
    """**B10.** A chunk is wholly in memory: a 200 MB chunk is not streaming.

    Zero and negative are a fault of their own:
    ``iter_content(chunk_size=0)`` behaves in a library-specific way and never
    in the way that is wanted.
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


# -- The seam: the real adapter through the real stage (A1, 2026-09-05) ------
#
# Row 5 of the matrix ("FACEIT no longer has the demo -> no_demo") had been
# implemented as two halves that did not fit together: the stage test's fake
# modelled the absence as a ``DemoUnavailable``, but the adapter produced it
# as an ``ApiError`` -- and the stage ended up in the state
# ``download_failed``. Both halves were green. **The seam is exactly here**,
# and that is why these tests run the real adapter through the real stage from
# behind a faked transport.


def run_stage(tmp_path: Path, session: FakeSession, unit: str = UNIT):
    archive = ArchivePaths(root=tmp_path / "archive")
    return archive, fetch_stage.run(
        archive,
        unit,
        source=build(tmp_path, session),
        disk_free=lambda _a: 100 * 1024**3,
    )


def test_a_deleted_demo_becomes_no_demo_all_the_way_through_the_stage(
    tmp_path,
) -> None:
    """404 from the adapter -> ``no_demo`` from the stage, not ``download_failed``."""
    session = FakeSession(sign=signed_response(), download=FakeResponse(404))

    archive, result = run_stage(tmp_path, session)

    assert result.status == "no_demo"
    assert archive.find_demo(UNIT) is None
    reason = result.reason or ""
    assert "30 päivää" in reason
    assert "45 päivää" in reason
    # The user is not advised to run the command again for a demo that is not
    # coming back.
    assert "aja komento uudelleen" not in reason.lower()


def test_a_transient_failure_becomes_download_failed_through_the_stage(
    tmp_path,
) -> None:
    """The same seam the other way round: a 503 must not turn into an absence."""
    session = FakeSession(sign=signed_response(), download=FakeResponse(503))

    _archive, result = run_stage(tmp_path, session)

    assert result.status == "download_failed"


def test_an_unplayed_map_becomes_no_demo_through_the_stage(tmp_path) -> None:
    """A BO3 that ended 2-0: the third map was not played."""
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
    """A positive control: the seam works when it succeeds too.

    Without this, the whole set of seam tests could be passed by an adapter
    that never produces anything.
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
    """The junk guard at the seam: a 200 status and an HTML body.

    This is the case in which everything else looks successful -- the adapter
    does not check the content and cannot, because it does not know what is
    being written.
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


# -- An authorisation fault is global (C1, C2 -- first real run 2026-09-05) --
#
# The product owner's Downloads API application was in the queue, and the Data
# API key does not do for the Downloads API. The run produced two identical
# 403s, which were sorted under the heading "aja komento uudelleen" -- advice
# that does not help until the application is approved. With twelve demos it
# would have been twelve doomed signing calls.
#
# These tests run **the real adapter through the real stage**, because that is
# exactly the seam that differed: the stage test's fake cannot produce a 403
# from the Downloads API.


def denied_session(status: int = 403) -> FakeSession:
    return FakeSession(sign=FakeResponse(status), download=FakeResponse(200))


@pytest.mark.parametrize("status", [401, 403])
def test_a_denied_downloads_token_is_not_a_unit_status(tmp_path, status: int) -> None:
    """The fault is in the id, not in the demo -- and it is therefore not a
    unit's state.

    ``download_failed`` means "may succeed on a new run". A missing Downloads
    scope cannot, until FACEIT approves the application.
    """
    archive = ArchivePaths(root=tmp_path / "archive")
    source = build(tmp_path, denied_session(status))

    with pytest.raises(DownloadsAccessDenied):
        fetch_stage.run(
            archive, UNIT, source=source, disk_free=lambda _a: 100 * 1024**3
        )

    assert archive.find_demo(UNIT) is None


def test_a_denied_token_stops_the_run_after_exactly_one_signing_call(
    tmp_path,
) -> None:
    """**The core of C2.** With twelve demos this was 12 doomed calls.

    Carrying on with the series is not "persistence" but spending the quota
    with no chance of succeeding: every unit would fail identically.
    """
    archive = ArchivePaths(root=tmp_path / "archive")
    session = denied_session()
    source = build(tmp_path, session)
    units = [UNIT, SECOND, f"{MATCH}-2"]

    with pytest.raises(DownloadsAccessDenied):
        fetch_stage.run_many(
            archive, units, source=source, disk_free=lambda _a: 100 * 1024**3
        )

    assert len(session.posts) == 1, (
        f"signing calls made: {len(session.posts)}, there should be 1"
    )


def test_the_interrupted_run_says_what_it_managed_to_do(tmp_path) -> None:
    """An interrupted series must not look the same as a series that never started."""
    archive = ArchivePaths(root=tmp_path / "archive")
    # The first one succeeds, the second one runs into the refusal.
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
    # The first demo is on disk and is not cleaned away.
    assert archive.demo(UNIT).is_file()


def test_the_denied_message_says_where_to_check_and_where_to_apply(
    tmp_path,
) -> None:
    """The user does not write code: the message has to say what to do next.

    Three different things, three different fixes: the application is in the
    queue (wait), there is no application (make one), or the token is wrong
    (fix .env).
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
    """The generic message said "the fault is not fixed by waiting", and here
    that is wrong.

    The claim is true of a retry in seconds and false of an application in
    weeks. Waiting is exactly what fixes this one.
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
    """Instructions that name the file but not its location are not instructions."""
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
    """The message says what to fix, not what the file says."""
    source = build(tmp_path, denied_session())

    with pytest.raises(DownloadsAccessDenied) as excinfo:
        source.get_demo(UNIT)

    assert TOKEN not in str(excinfo.value)
    assert KEY not in str(excinfo.value)


def test_a_403_on_the_signed_link_is_still_a_single_unit_failure(
    tmp_path,
) -> None:
    """**The same code, a different place, a different meaning -- and that
    difference has to be kept.**

    A 403 from the Downloads API means "the token has no scope": not a single
    demo can succeed. A 403 on the signed link means an expired or badly
    formed signature: **one** download's fault, which may very well succeed
    with a new link. If they were handled alike, one expired link would
    interrupt the whole sample.
    """
    archive = ArchivePaths(root=tmp_path / "archive")
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


# -- 400 from the signing call (D2, live run 2026-09-05) --------------------
#
# Measured with a malformed token. The third status code out of the three
# measured, and the only one that hits an ordinary user: a character slips
# while the .env file is edited by hand.


def bad_request(payload=None, text: str | None = None) -> FakeResponse:
    return FakeResponse(400, payload, text=text or "")


def test_a_400_from_the_signing_call_is_not_a_missing_demo(tmp_path) -> None:
    """A malformed request does not mean there is no demo.

    ``no_demo`` is final: it never tries again. One wrong character in the
    ``.env`` file would then mark the whole sample non-existent.
    """
    session = FakeSession(sign=bad_request())
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    assert excinfo.value.status_code == 400


def test_a_400_does_not_advise_running_the_command_again(tmp_path) -> None:
    """**The core of D2.** Running again does not fix a wrong id.

    The old message sorted this under the heading "aja komento uudelleen", and
    the advice would have repeated on every run for ever.
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
    """A 400 is not unambiguous, and the tool must not claim to know which one it is.

    A malformed id brings down every demo; a malformed resource_url only one.
    Which of them it is cannot be inferred from the response -- but it can be
    from the run, and the message says how.
    """
    session = FakeSession(sign=bad_request())
    source = build(tmp_path, session)

    with pytest.raises(ApiError) as excinfo:
        source.get_demo(UNIT)

    message = str(excinfo.value)
    assert "FACEIT_DOWNLOADS_TOKEN" in message
    assert "tallenneosoite" in message
    # The rule by which the user tells the causes apart -- not a guess at
    # which one it is.
    assert "kaikki" in message.lower()
    assert "vain tämä" in message


def test_a_400_shows_faceits_own_error_text_when_there_is_one(
    tmp_path,
) -> None:
    """The interface's own text is an observation; our guess is not."""
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
    """FACEIT's error body is not of one shape; the extraction must not be either."""
    session = FakeSession(sign=bad_request(payload))

    with pytest.raises(ApiError) as excinfo:
        build(tmp_path, session).get_demo(UNIT)

    assert "resource_url is not valid" in str(excinfo.value)


def test_a_non_json_error_body_is_shown_but_truncated(tmp_path) -> None:
    """An HTML error page is kilobytes and would bury the instructions."""
    session = FakeSession(sign=bad_request(text="<html>" + "x" * 5000))

    with pytest.raises(ApiError) as excinfo:
        build(tmp_path, session).get_demo(UNIT)

    message = str(excinfo.value)
    assert "<html>" in message
    assert len(message) < 2000


def test_a_400_without_any_body_does_not_invent_an_explanation(
    tmp_path,
) -> None:
    """A missing observation is not substituted for.

    An invented explanation is worse than none at all.
    """
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
    """The seam: the real adapter through the real stage.

    ``download_failed`` is the right state -- the demo probably exists -- but
    the advice has to be this fault's own and not the bucket's default.
    """
    archive = ArchivePaths(root=tmp_path / "archive")
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
