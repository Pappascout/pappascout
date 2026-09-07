"""The FACEIT client's tests -- all offline (Story 3.1).

**No test in this file touches the network**, and that is not a habit but a
structure: :func:`_no_network` blocks every real HTTP call for the whole
module, and every client is built with a :class:`FakeSession` that returns a
hand-written response. If somebody removed the transport from the parameters
by accident, the test does not go quietly to the network but fails.

Verification against the real interface is done by hand, and the result is
recorded in the spec's "Manual checks" section.
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

#: A key that can be recognised in the output and in a file.
#:
#: Not "key" and not "test": a ``grep`` finds this string only if the key
#: really leaked somewhere -- a generic word would hit the comments and would
#: leave the guard toothless.
KEY = "salainen-avain-XYZZY-42"

#: The championship of Pappaliiga season 13 (settings.toml, [league]).
CHAMPIONSHIP = "94681888-b5da-4ab5-bf50-f44b666b98a3"

#: The tests' own root. **Not a real address**, so that no test can hit it
#: even by accident -- and https, because the client requires it.
BASE = "https://faceit.invalid/data/v4"

MATCHES_URL = f"{BASE}/championships/{CHAMPIONSHIP}/matches"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut off real HTTP for the whole module.

    ``conftest`` isolates the tests from the machine's files; this isolates
    them from the network. The cut is in ``HTTPAdapter.send`` and not in
    ``Session.get``, because that is precisely the place where ``requests``
    opens a connection -- and so no way of calling ``Session`` can get past
    it.
    """

    def _refuse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(
            "A test tried to go to the network. Give FaceitClient a session "
            "parameter."
        )

    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", _refuse)


# -- The fixtures ------------------------------------------------------------


class FakeResponse:
    """A hand-written HTTP response.

    ``payload=None`` means "the response is not JSON": :meth:`json` raises a
    ``ValueError`` exactly as ``requests`` does for an HTML page.
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
    """A transport that hands out ready responses from a queue and opens no
    connection.

    An item in the queue may also be an exception, in which case it is raised
    -- so a connection error and a timeout are testable with the same fixture.

    ``closed`` says whether the client closed the transport. An injected
    transport **must not** be closed: it is the caller's property.
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
                f"There were more calls than responses: {len(self.calls)}. "
                f"The last one: {url} {params}"
            )
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """A clock that moves only when the test moves it.

    The time budget is measurable only once time is under the test's control:
    against a real clock the test would either sleep for minutes or measure
    the machine.
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
    """A client with its fixtures: the transport, the recorded waits and the
    cache.

    The jitter is off by default (``random_source`` returns 0.0), so that the
    waits are comparable as numbers; the jitter's own test gives another
    source.
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
    """One FACEIT match as the interface gives it.

    The fields and their shapes follow the Data API v4: times as epoch
    seconds, the sides behind the ``faction1``/``faction2`` keys, the maps in
    the ``voting.map.pick`` list.
    """
    payload: dict[str, Any] = {
        "match_id": match_id,
        "competition_id": CHAMPIONSHIP,
        "competition_name": "6 Divisioona",
        "status": "FINISHED",
        "scheduled_at": 1_755_996_400,
        "started_at": 1_756_000_000,
        "finished_at": 1_756_003_600,
        "teams": {
            "faction2": {
                "faction_id": "team-imuaijat",
                "name": "Imuaijat",
                "roster": [
                    {
                        "player_id": "p-imu-1",
                        "nickname": "imu1",
                        "game_player_id": "76561197960265729",
                    },
                    {
                        "player_id": "p-imu-2",
                        "nickname": "imu2",
                        "game_player_id": "76561197960265730",
                    },
                ],
                "substitutes": [],
            },
            "faction1": {
                "faction_id": "team-potku",
                "name": "PotkukelkkaPeek",
                "roster": [
                    {
                        "player_id": "p-potku-1",
                        "nickname": "pelaaja",
                        "game_player_id": "76561197977479426",
                    },
                    {
                        "player_id": "p-potku-2",
                        "nickname": "kaveri",
                        "game_player_id": "76561197985923425",
                    },
                ],
                "substitutes": [
                    {
                        "player_id": "p-potku-3",
                        "nickname": "Lindberq_",
                        "game_player_id": "76561198062941501",
                    }
                ],
            },
        },
        "voting": {"map": {"pick": ["de_nuke", "de_ancient"]}},
        # Measured 2026-09-04: ``best_of`` is 2 in all 66 matches.
        "best_of": 2,
    }
    payload.update(overrides)
    return payload


def cache_files(client: FaceitClient) -> list[Path]:
    """The cache files as a list -- even when the directory has not been
    created.

    The match list is no longer cached, so the directory need not exist at
    all. ``glob`` on a missing directory returns nothing, but this says the
    intent out loud: "nothing was left on disk".
    """
    return sorted(client.cache_dir.glob("*.json"))


def page(*matches: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {"items": list(matches), "start": 0, "end": len(matches)})


# -- The key is missing or empty ---------------------------------------------


def test_missing_key_stops_the_run_and_names_the_file_and_the_line(
    settings_file: Path,
) -> None:
    """The I/O matrix: ``.env`` without ``FACEIT_API_KEY``.

    The message states **the file's path and the line that is needed**. "The
    key is missing" alone would leave the user searching, and the user does
    not code.
    """
    settings = load_settings(settings_file, env_files=())

    with pytest.raises(SettingsError) as exc:
        FaceitClient.from_settings(settings, Path("not-used"))

    message = str(exc.value)
    assert "FACEIT_API_KEY" in message
    assert ".pappascout" in message and ".env" in message
    assert "FACEIT_API_KEY=" in message


@pytest.mark.parametrize("value", ["", '""', "   ", '"   "'])
def test_empty_or_blank_key_is_the_same_as_a_missing_one(
    settings_file: Path, env_file: Any, value: str
) -> None:
    """The I/O matrix: a key that is empty or nothing but spaces.

    An empty string is a missing key written out in the open, and quotation
    marks are the commonest way of writing it. If this got through, the error
    would come only from the interface as a 401 -- that is, from the wrong
    place.
    """
    env = env_file(".env", FACEIT_API_KEY=value)
    settings = load_settings(settings_file, env_files=(env,))

    with pytest.raises(SettingsError) as exc:
        FaceitClient.from_settings(settings, Path("not-used"))

    assert str(env) in str(exc.value)


def test_an_environment_variable_alone_is_not_a_key(
    settings_file: Path, env_file: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key is not smuggled past ``Secrets`` in an environment variable.

    **This test really does set the key in the environment** and leaves it out
    of the ``.env`` file. Without the ``setenv`` call the test would be
    character for character the same as the missing-key test and would pass
    even if the client read ``os.environ`` directly -- that is, the guard
    would look like a guard without being one. (That is exactly how it was
    written first, and the review found it.)

    ``.env`` is loaded **before** the variable is set, because
    ``pydantic-settings`` reads the environment while building ``Settings``:
    if the variable were set first, the key would end up in ``Settings`` by a
    legitimate route and the test would measure nothing.
    """
    settings = load_settings(settings_file, env_files=(env_file(".env"),))
    assert settings.faceit_api_key is None

    monkeypatch.setenv("FACEIT_API_KEY", KEY)

    with pytest.raises(SettingsError) as exc:
        FaceitClient.from_settings(settings, tmp_path)

    assert "FACEIT_API_KEY" in str(exc.value)


def test_the_module_never_touches_the_environment() -> None:
    """``faceit.py`` does not read the environment at all.

    The previous test measures behaviour; this measures structure. Together
    they say that a second way of reading cannot be added by accident.

    The check is on the AST and not on the text: a text search would hit the
    module's own docstring, which **explains** that the environment is not
    read -- that is, the documentation would bring down the guard it
    describes.
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
    """The ``[faceit]`` section is the one that decides -- not the adapter's
    default."""
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
    """A setting cannot be overridden through ``from_settings``.

    If it could, the ``[faceit]`` section would no longer be the one that
    decides -- and the override would show only at the call site, not in the
    file that is being adjusted.
    """
    env = env_file(".env", FACEIT_API_KEY=KEY)
    settings = load_settings(settings_file, env_files=(env,))

    with pytest.raises(TypeError):
        FaceitClient.from_settings(settings, tmp_path, page_size=7)


# -- Where the call goes -----------------------------------------------------


def test_the_request_goes_to_the_documented_address(tmp_path: Path) -> None:
    """The address, the parameters, the headers and the timeout are behind an
    assertion.

    Without this ``FakeSession`` would hand out responses whatever the
    address: the path could be changed to ``/match`` or the ``type`` parameter
    removed, and the whole suite would stay green. The review found exactly
    that gap.
    """
    client, session, _waits = make_client(tmp_path, page(match_payload()))

    client.get_matches(CHAMPIONSHIP)

    call = session.calls[0]
    assert call.url == MATCHES_URL
    assert call.params == {"type": "all", "offset": 0, "limit": client.page_size}
    assert call.headers["Accept"] == "application/json"
    assert call.timeout == client.timeout_seconds
    # A redirect is not followed: it would send the key to an address this
    # module did not choose.
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
    """The key travels in a header, and ``http`` would send it in the
    clear."""
    with pytest.raises(SettingsError) as exc:
        FaceitClient(KEY, tmp_path, base_url="http://faceit.invalid/data/v4")

    assert "https" in str(exc.value)


def test_the_real_default_address_is_https() -> None:
    assert FACEIT_DATA_API_BASE.startswith("https://")


# -- Retries -----------------------------------------------------------------


def test_rate_limit_is_retried_and_the_run_continues(tmp_path: Path) -> None:
    """The I/O matrix: a 429 and then a 200 -- the run succeeds.

    The acceptance criterion says "there was **at least one** retry", and that
    is exactly why the number is read from ``requests_made`` rather than
    inferred from the result having arrived: without the counter the test
    would pass even where no retry happened at all.
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
    """The I/O matrix: a 5xx behaves like a 429."""
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
    # A growing delay, not a constant one: 1 s, then 2 s.
    assert waits == [1.0, 2.0]


def test_the_growing_delay_stops_at_the_configured_ceiling(tmp_path: Path) -> None:
    """The ceiling cuts the delay -- otherwise the eighth attempt would be
    minutes away."""
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
    """The interface's own measurement beats the growing delay.

    The growing delay is a blind assumption; ``Retry-After`` is the number the
    server itself states -- and ``settings.toml`` admits itself that the
    length of 429 spells has not been measured. Without this the client would
    wait 1 s where the server asks it to wait 7 s, and would hit the rate
    limit again.
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
    """By RFC 9110 ``Retry-After`` is either seconds or a date."""
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
    """A date in the past and a negative number would mean "do not wait"."""
    client, _session, waits = make_client(
        tmp_path,
        FakeResponse(429, retry_after="-5"),
        page(match_payload()),
        retry_attempts=3,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    client.get_matches(CHAMPIONSHIP)

    # Back to the growing delay, because the header did not qualify as a
    # waiting time.
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
    """The jitter exists so that two runs do not wait into the same second.

    The exponential delay is the same function from the same moment on every
    run. Without the jitter two machines would hit the rate limit together
    again and again.
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

    # 1.0 s + 25 % = 1.25 s, that is, the jitter is on and not rounded away.
    assert waits == [1.25]


def test_a_connection_error_is_retried(tmp_path: Path) -> None:
    """A timeout and a connection error are transient faults, not errors."""
    client, _session, _waits = make_client(
        tmp_path,
        requests.ConnectionError("the network is down"),
        requests.Timeout("too slow"),
        page(match_payload()),
        retry_attempts=4,
    )

    assert len(client.get_matches(CHAMPIONSHIP)) == 1
    assert client.requests_made == 3


def test_when_retries_run_out_the_error_names_the_attempt_count(
    tmp_path: Path,
) -> None:
    """The I/O matrix: an error in the end that states the number of
    attempts."""
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
    """The I/O matrix: a 4xx other than 429 -- **no retry**.

    A wrong id does not become right by waiting. A retry would only delay an
    error that is already certain, and would spend the call quota.
    """
    client, session, waits = make_client(
        tmp_path,
        FakeResponse(404, {"errors": [{"message": "not found"}]}),
        retry_attempts=5,
        retry_initial_delay_seconds=1.0,
        retry_max_delay_seconds=30.0,
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-no-such-thing")

    assert exc.value.attempts == 1
    assert exc.value.status_code == 404
    assert len(session.calls) == 1
    assert waits == []
    assert "404" in str(exc.value)
    assert "1-no-such-thing" in str(exc.value)
    assert "raw/faceit/" in str(exc.value)


def test_a_wrong_key_is_not_retried_either(tmp_path: Path) -> None:
    """A 401 is the same class as a 404: waiting does not make the key
    valid."""
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
    """A 204 and a 3xx are not JSON -- and they must not look like a JSON
    fault.

    The condition used to be ``status >= 400``, so everything below it flowed
    into the JSON parsing and ended in the error "the response was not JSON".
    That reports the symptom and not the cause: a redirect is a different
    fault from a broken body.
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
    """The caller catches one type and shows its message."""
    assert issubclass(ApiError, PappascoutError)


# -- The time budget ---------------------------------------------------------


def test_a_slow_api_stops_at_the_call_budget(tmp_path: Path) -> None:
    """One call must not go on quietly for ever.

    ``retry_attempts`` does not bound the time: pagination multiplies the
    attempts by the number of pages, so ``MAX_PAGES`` x ``retry_attempts``
    would be hundreds of requests and, with a growing delay, an hour of
    silence. The ceiling therefore has to be in seconds.
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

    # 4 s + 8 s fits (12 s), the next 16 s does not -- 8 s of budget is left.
    assert client.requests_made == 3
    assert exc.value.attempts == 3
    assert exc.value.status_code == 503
    assert "budget" in str(exc.value).lower()
    assert "call_budget_seconds" in str(exc.value)


def test_a_long_retry_after_does_not_buy_silence(tmp_path: Path) -> None:
    """A ``Retry-After`` that exceeds the budget is an error and not a wait.

    The server may ask for a wait of minutes, but a wait that does not fit the
    budget is silence with no chance of succeeding.
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
    """The budget is the port call's and not a single page's.

    A per-page budget would not bound a fetch of ``MAX_PAGES`` pages at all.
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

    # The budget is checked BEFORE a page begins, and a page's duration
    # cannot be known in advance: the fourth page begins at 9 s (under 10 s)
    # and takes the budget to 12 seconds, at which point the fifth does not
    # begin. Four requests is therefore the right number -- five would be the
    # guard failing and three a promise that cannot be kept.
    assert client.requests_made == 4
    assert "call_budget_seconds" in str(exc.value)


def test_the_default_budget_is_a_guard_not_a_neutral_zero() -> None:
    """The budget's default is a guard in the same sense as
    :data:`MAX_PAGES`."""
    assert DEFAULT_CALL_BUDGET_SECONDS > 0


# -- The cache ---------------------------------------------------------------


def test_the_match_list_is_never_cached(tmp_path: Path) -> None:
    """The product owner's decision on 2026-09-04: the match list is not
    cached at all.

    It is **one call per run** -- the division's 66 matches fit on a single
    page -- and it **changes constantly**: 60 of the 66 matches were in state
    ``SCHEDULED``. A cache would save one call there and cost correctness. The
    requirement is "do not miss obvious matches", and this is the only way to
    keep it without a clock.
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
    # And nothing was left on disk -- not even a file that would mislead the
    # next reader.
    assert cache_files(client) == []


def test_a_new_match_appears_on_the_second_run(tmp_path: Path) -> None:
    """A match added during the season shows at once, not only after a clear.

    This is the whole point of the decision. Cached, the second run would
    return the first list, and the new match would be left out without
    anything saying so.
    """
    client, _session, _waits = make_client(
        tmp_path,
        page(match_payload("1-old")),
        page(match_payload("1-old"), match_payload("1-new")),
    )

    assert [m.match_id for m in client.get_matches(CHAMPIONSHIP)] == ["1-old"]
    assert [m.match_id for m in client.get_matches(CHAMPIONSHIP)] == [
        "1-old",
        "1-new",
    ]


def test_a_finished_match_is_cached_permanently(tmp_path: Path) -> None:
    """A played match's details do not change any more, and they are fetched
    as many as 66 times.

    The transport's queue holds **only one** response, so a second network
    call would fail the test: the hit is not inferred from a counter but from
    the structure.
    """
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-finished", status="FINISHED"))
    )

    first = client.get_match("1-finished")
    second = client.get_match("1-finished")

    assert first == second
    assert client.requests_made == 1
    assert client.cache_hits == 1
    assert len(session.calls) == 1
    assert len(cache_files(client)) == 1


@pytest.mark.parametrize("status", ["SCHEDULED", "ONGOING", "CANCELLED", None])
def test_a_match_that_is_not_finished_is_not_cached(
    tmp_path: Path, status: str | None
) -> None:
    """An unfinished match is not written to disk -- and the result updates.

    The third parameter is ``CANCELLED``, which looks just as final as
    ``FINISHED``. It is out all the same, and the reason is the asymmetric
    cost: cancelling is the organiser's decision, which the organiser can
    undo, and a rescheduled match is played under the same ``match_id``. A
    cached ``CANCELLED`` would hide it for ever -- for one saved call a played
    match's demo would be lost permanently (FACEIT keeps demos for ~30 days).

    The fourth is a missing state: "I do not know" is no reason to keep a
    response for ever, and a new state in FACEIT therefore leads to one extra
    call and not to a wrong answer.
    """
    playing = match_payload("1-unfinished", status=status)
    if status is None:
        del playing["status"]
    finished = match_payload("1-unfinished", status="FINISHED")
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, playing), FakeResponse(200, finished)
    )

    first = client.get_match("1-unfinished")
    assert cache_files(client) == []

    second = client.get_match("1-unfinished")

    assert client.requests_made == 2
    assert client.cache_hits == 0
    assert len(session.calls) == 2
    # The second call sees the match finished: the result has updated.
    assert first.status != "FINISHED"
    assert second.status == "FINISHED"
    # And now it is in the cache.
    assert len(cache_files(client)) == 1


def test_the_cacheable_statuses_are_a_named_constant() -> None:
    """The rule is a named constant, not a magic string at the call site.

    Named, it is readable, testable and changeable in one place; written at
    the call site it would be a decision visible only to whoever reads that
    one line.
    """
    assert CACHEABLE_MATCH_STATUSES == frozenset({"FINISHED"})


def test_the_cache_keeps_only_what_cannot_change(tmp_path: Path) -> None:
    """The same match first unfinished, then finished: only the latter stays.

    This is the decision in one sentence: an unchanging response is kept, a
    changing one is not -- and the rule rests on no clock at all.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-x", status="ONGOING")),
        FakeResponse(200, match_payload("1-x", status="FINISHED")),
    )

    client.get_match("1-x")
    client.get_match("1-x")
    third = client.get_match("1-x")  # the third does not go to the network

    assert third.status == "FINISHED"
    assert client.requests_made == 2
    assert client.cache_hits == 1


def test_a_cleared_cache_fetches_again_and_gives_the_same_result(
    tmp_path: Path,
) -> None:
    """The I/O matrix: ``raw/faceit/`` deleted -- a new call, the same result.

    This is the cache's promise: it may be deleted at any time, and the
    deletion has no consequence other than a new call. No manifest, no state
    that would have to be repaired.
    """
    payload = match_payload("1-finished")
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, payload), FakeResponse(200, payload)
    )

    first = client.get_match("1-finished")
    assert cache_files(client)
    for path in cache_files(client):
        path.unlink()

    second = client.get_match("1-finished")

    assert first == second
    assert client.requests_made == 2
    assert client.cache_hits == 0


def test_a_corrupt_cache_file_is_ignored_like_a_missing_one(tmp_path: Path) -> None:
    """A write that was cut short must not fail the run or be left to clean
    up."""
    payload = match_payload("1-finished")
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, payload), FakeResponse(200, payload)
    )

    client.get_match("1-finished")
    cached = cache_files(client)[0]
    cached.write_text('{"match_id": ', encoding="utf-8")

    assert client.get_match("1-finished").match_id == "1-finished"
    assert client.requests_made == 2


def test_a_broken_response_is_not_written_to_the_cache(tmp_path: Path) -> None:
    """A broken 200 must not be left on disk.

    If it were, **every later run would fail with the same error without going
    to the network at all** -- that is, the cache would turn into state that
    has to be cleaned up, although its whole promise is the opposite. The
    second response in the queue proves that the next run really does try
    again.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, {"status": "FINISHED"}),
        FakeResponse(200, match_payload("1-finished")),
    )

    with pytest.raises(ApiError) as exc:
        client.get_match("1-finished")

    assert cache_files(client) == []
    assert "match_id" in str(exc.value)
    assert "raw/faceit/" in str(exc.value)
    # And the next run gets all the way to the network.
    assert client.get_match("1-finished").match_id == "1-finished"
    assert client.requests_made == 2


def test_a_poisoned_cache_file_is_dropped_and_refetched(tmp_path: Path) -> None:
    """An unusable response written by an older version must not go on failing
    the run.

    The check before a write stops new poisonings; this deals with the ones
    already on disk. Without it the user's only recourse would be to know to
    clear the directory themselves.
    """
    payload = match_payload("1-finished")
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, payload), FakeResponse(200, payload)
    )
    client.get_match("1-finished")
    cached = cache_files(client)[0]
    cached.write_text('{"status": "FINISHED"}', encoding="utf-8")

    assert client.get_match("1-finished").match_id == "1-finished"
    assert client.requests_made == 2
    assert "match_id" in json.loads(cached.read_text(encoding="utf-8"))


def test_a_cached_unfinished_match_is_dropped_and_refetched(tmp_path: Path) -> None:
    """The read path applies the same condition as the write path --
    self-correctingly.

    The fixture is **a measured situation and not an invented one**: on
    2026-09-04 a shared archive held a file written by an earlier version,
    which served a ``SCHEDULED`` match from disk for ever and entirely
    silently. At first the read path was left unconditional, on the reasoning
    that "if the file exists, it was written from a finished match", and a
    live check showed that premise false the same day.

    The symmetric condition makes the invariant one that does not depend on
    every earlier and future write path having been right -- but on what the
    file says.
    """
    playing = match_payload("1-x", status="SCHEDULED")
    client, session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-x", status="FINISHED")),
        FakeResponse(200, playing),
    )

    # Warm the cache legitimately, then replace the file's contents with
    # something the current write path would never have written.
    client.get_match("1-x")
    cached = cache_files(client)[0]
    cached.write_text(json.dumps(playing), encoding="utf-8")
    before = client.requests_made

    result = client.get_match("1-x")

    # The network was touched, and no unusable file was left on disk.
    assert client.requests_made == before + 1
    assert client.cache_hits == 0
    assert len(session.calls) == 2
    assert result.status == "SCHEDULED"
    assert cache_files(client) == []
    assert not cached.exists()


def test_a_cached_finished_match_is_still_read_from_disk(tmp_path: Path) -> None:
    """The condition must not reject what it is meant to keep.

    The file's contents are replaced with **another finished match**, which
    the transport never returns: if that is the result, the response came from
    disk and not from memory or the network. Without this pair the previous
    test could pass even where the read path rejects everything.
    """
    client, session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-x", status="FINISHED"))
    )

    client.get_match("1-x")
    cached = cache_files(client)[0]
    cached.write_text(
        json.dumps(
            match_payload("1-x", status="FINISHED", competition_id="from-disk")
        ),
        encoding="utf-8",
    )
    before = client.requests_made

    result = client.get_match("1-x")

    assert result.competition_id == "from-disk"
    assert client.requests_made == before
    assert client.cache_hits == 1
    assert len(session.calls) == 1
    assert cached.exists()


def test_the_cache_leaves_no_temporary_files(tmp_path: Path) -> None:
    """The write is atomic: no temporary file is left in the directory."""
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-finished"))
    )

    client.get_match("1-finished")

    assert cache_files(client)
    assert not has_temp_leftovers(client.cache_dir)


def test_a_cache_write_failure_does_not_lose_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache is a speed-up, not a result.

    If the disk is full or the directory is read-only, the call has already
    succeeded and the response is in hand -- crashing on the write would throw
    it away. Without this test the whole ``try/except`` could disappear
    unnoticed.
    """
    original = Path.write_text

    def _refuse(self: Path, *args: Any, **kwargs: Any) -> int:
        if ".tmp-" in self.name:
            raise OSError("the disk is full")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _refuse)
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-finished"))
    )

    assert client.get_match("1-finished").match_id == "1-finished"
    assert not has_temp_leftovers(client.cache_dir)


def test_two_different_calls_get_two_cache_files(tmp_path: Path) -> None:
    """The cache key tells the calls apart -- otherwise one would return the
    wrong response."""
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
    """The address is part of the cache key.

    Two different roots sharing one directory would otherwise serve each
    other's responses, and nothing would say so.
    """
    cache = tmp_path / "raw" / "faceit"
    one = FaceitClient(
        KEY,
        cache,
        base_url=BASE,
        session=FakeSession(FakeResponse(200, match_payload("1-one"))),
    )
    two = FaceitClient(
        KEY,
        cache,
        base_url="https://other.invalid/data/v4",
        session=FakeSession(FakeResponse(200, match_payload("1-two"))),
    )

    assert one.get_match("1-x").match_id == "1-one"
    assert two.get_match("1-x").match_id == "1-two"
    assert two.cache_hits == 0


# -- The key does not leak ---------------------------------------------------


def test_the_key_travels_in_the_header_and_nowhere_else(tmp_path: Path) -> None:
    """The key is in the ``Authorization`` header, not in the address and not
    in the parameters."""
    client, session, _waits = make_client(tmp_path, page(match_payload()))

    client.get_matches(CHAMPIONSHIP)

    call = session.calls[0]
    assert call.headers["Authorization"] == f"Bearer {KEY}"
    assert KEY not in call.url
    assert KEY not in str(call.params)


def test_the_key_is_in_no_cache_file(tmp_path: Path) -> None:
    """The acceptance criterion: the key is in no cache file.

    Checked **in the file names and in the contents**, because the cache key
    is derived from the address and the parameters -- and if the key ever
    ended up in either, it would end up in the name too.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-finished"))
    )

    # The match's details, because only they are written to disk: the match
    # list produces no file at all, so this guard would be empty with it.
    client.get_match("1-finished")

    files = [p for p in client.cache_dir.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert KEY not in path.name
        text = path.read_text(encoding="utf-8")
        assert KEY not in text
        assert "FACEIT_API_KEY" not in text


def test_the_key_is_in_no_repr_and_no_error_message(tmp_path: Path) -> None:
    """The key does not end up in a traceback, in an error message or in a
    debugger."""
    client, _session, _waits = make_client(tmp_path, FakeResponse(404))

    assert KEY not in repr(client)
    assert KEY not in str(vars(client))

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert KEY not in str(exc.value)
    assert KEY not in repr(exc.value)
    # ``repr`` shows only ``args``, so new attributes have to be looked at
    # separately -- otherwise the guard would not see a field added later.
    assert KEY not in str(vars(exc.value))


# -- A response that is not JSON ---------------------------------------------


def test_an_html_error_page_is_not_dumped_into_the_message(tmp_path: Path) -> None:
    """The I/O matrix: the interface returns an HTML error page.

    The page is kilobytes, and there is nothing in its contents that would
    guide the user. The message says **that** the response was not JSON and
    how big it was -- not what the page said.
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
    # No retry: a broken response is not a transient rush.
    assert exc.value.attempts == 1


def test_a_json_response_that_is_not_an_object_is_an_error(tmp_path: Path) -> None:
    """A list where an object should be is a broken response, not an empty
    result."""
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, ["a", "b"]))

    with pytest.raises(ApiError):
        client.get_match("1-aaaa")


def test_a_match_list_without_items_is_an_error_not_an_empty_result(
    tmp_path: Path,
) -> None:
    """A missing ``items`` must not look as though there are no matches.

    An empty result is a valid observation; a broken response is not. If those
    two were confused, a wrong championship id would quietly produce a report
    with no matches in it.
    """
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, {"start": 0}))

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert "items" in str(exc.value)
    assert "championship_ids" in str(exc.value)


def test_a_junk_row_stops_the_run_like_a_junk_response(tmp_path: Path) -> None:
    """A junk row does not drop out quietly.

    A silent drop would be a different rule from the error a missing ``items``
    raises, and two rules would mean that the page's length depends on which
    of them is looked at -- precisely the number by which pagination
    advances.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, {"items": [match_payload(), "junk"]})
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert "row 1" in str(exc.value)


def test_a_row_without_a_match_id_stops_the_run(tmp_path: Path) -> None:
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, {"items": [{"status": "FINISHED"}]})
    )

    with pytest.raises(ApiError) as exc:
        client.get_matches(CHAMPIONSHIP)

    assert "match_id" in str(exc.value)


def test_a_structure_error_reports_the_real_attempt_count(tmp_path: Path) -> None:
    """``attempts`` and ``status_code`` must not lie.

    Story 3.4 decides from these whether a match's state is ``no_demo`` or
    ``download_failed``. ``ApiError``'s docstring promises that
    ``status_code=None`` means "no response was obtained at all" -- and before
    this a structure error reported exactly that, although the response was a
    200 after three attempts.
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
    """A cache hit: ``attempts = 0`` is a different thing from a failed
    attempt.

    The transport's queue is empty at the second call, so a fetch that went to
    the network would fail -- the hit is therefore in the structure and not in
    a counter alone.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-finished"))
    )
    client.get_match("1-finished")

    fetched = client._get(
        "/matches/1-finished",
        None,
        what="a trial",
        validate=lambda payload: None,
        cache_when=lambda payload: True,
    )

    assert fetched.from_cache is True
    assert fetched.attempts == 0
    assert fetched.status_code is None


# -- Pagination --------------------------------------------------------------


def test_a_paginated_result_comes_back_as_one_list(tmp_path: Path) -> None:
    """The I/O matrix: more matches than a page holds.

    Every page is fetched, and the caller sees one list: pagination is a
    detail of the transport and not something the stage knows.
    """
    first = page(*[match_payload(f"1-a{i}") for i in range(3)])
    second = page(*[match_payload(f"1-b{i}") for i in range(2)])
    client, session, _waits = make_client(tmp_path, first, second, page_size=3)

    matches = client.get_matches(CHAMPIONSHIP)

    assert [m.match_id for m in matches] == ["1-a0", "1-a1", "1-a2", "1-b0", "1-b1"]
    assert [call.params["offset"] for call in session.calls] == [0, 3]
    assert {call.params["limit"] for call in session.calls} == {3}


def test_a_full_last_page_still_ends_the_paging(tmp_path: Path) -> None:
    """A full page followed by an empty one: the loop ends at the empty
    page."""
    client, session, _waits = make_client(
        tmp_path,
        page(*[match_payload(f"1-a{i}") for i in range(2)]),
        page(),
        page_size=2,
    )

    assert len(client.get_matches(CHAMPIONSHIP)) == 2
    assert len(session.calls) == 2


def test_an_oversized_page_does_not_skip_rows(tmp_path: Path) -> None:
    """The offset is the **real** length of the page, not the requested one.

    If the interface returns more than was asked for, ``offset += page_size``
    would skip over rows -- and the result would look like a full list with
    matches missing from it. A gap of exactly that kind would be visible
    nowhere.
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
    """Deduplication by ``match_id``.

    Pagination rests on ``offset``, and the list changes mid-season: a new
    match at the head of the list moves every other one along by one, so the
    same row comes on two pages. Without deduplication it would be counted
    twice.
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
    """A guard: an interface that never stops must not stall the run."""
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
    """A competition with no matches is an observation, not an error."""
    client, _session, _waits = make_client(tmp_path, page())

    assert client.get_matches(CHAMPIONSHIP) == ()


# -- FACEIT JSON into the core's vocabulary ----------------------------------


def test_the_port_speaks_the_core_vocabulary_not_faceits(tmp_path: Path) -> None:
    """``faction1``, ``voting`` and epoch seconds stay inside the adapter."""
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
    # The order is by key, so that it is the same on every run.
    assert [team.name for team in match.teams] == ["PotkukelkkaPeek", "Imuaijat"]
    assert [p.player_id for p in match.teams[0].roster] == ["p-potku-1", "p-potku-2"]
    assert match.teams[0].roster[0].nickname == "pelaaja"
    # map_index is an index into this tuple; map_demo_id is built from it.
    assert match.map_picks == ("de_nuke", "de_ancient")
    # best_of is a different number from map_picks' length, and it travels
    # through the port.
    assert match.best_of == 2


def test_missing_fields_are_none_not_placeholders(tmp_path: Path) -> None:
    """A missing value is ``None``, not a substitute -- as in the roster table
    too.

    An invented value would look later exactly like a measured one, and
    nothing would say which it was.
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
    assert match.best_of is None


def test_an_unstarted_match_has_no_start_time(tmp_path: Path) -> None:
    """FACEIT marks "not started" with a zero, not with a missing field.

    Zero epoch seconds is 1970-01-01, and read as it stands it would make
    every future match the earliest of all -- that is, it would make
    ``team_key``'s tie-breaker wrong.
    """
    client, _session, _waits = make_client(
        tmp_path,
        FakeResponse(200, match_payload("1-future", started_at=0, finished_at=0)),
    )

    match = client.get_match("1-future")

    assert match.started_at is None
    assert match.finished_at is None


def test_an_empty_nickname_is_not_a_name(tmp_path: Path) -> None:
    """An empty string is the absence of an observation, not a name."""
    payload = match_payload("1-empty")
    payload["teams"]["faction1"]["roster"] = [
        {"player_id": "p-1", "nickname": "   "},
        {"player_id": "p-2"},
        {"nickname": "no-id-at-all"},
    ]
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    team = client.get_match("1-empty").teams[0]

    # A player without an id is dropped: the id is the machine's key and the
    # nickname is not. Without it the player cannot be joined to anything.
    assert [p.player_id for p in team.roster] == ["p-1", "p-2"]
    assert [p.nickname for p in team.roster] == [None, None]


def test_the_port_carries_substitutes_and_steam_ids(tmp_path: Path) -> None:
    """Story 3.2: without these two fields the standing roster would be wrong.

    ``substitutes`` is a list of its own, and measured 2026-09-04: without it
    the roster systematically underestimates (``Lindberq_`` is in the demo but
    not in ``roster``). ``game_player_id`` is a SteamID64 and the **only** id
    that appears in the demos -- ``player_id`` is FACEIT's own UUID.
    """
    client, _session, _waits = make_client(
        tmp_path, FakeResponse(200, match_payload("1-roster"))
    )

    team = client.get_match("1-roster").teams[0]

    assert [p.nickname for p in team.substitutes] == ["Lindberq_"]
    assert [p.game_player_id for p in team.roster] == [
        "76561197977479426",
        "76561197985923425",
    ]
    assert team.substitutes[0].game_player_id == "76561198062941501"
    # Two ids side by side: neither is substituted for the other.
    assert team.roster[0].player_id == "p-potku-1"


def test_a_missing_substitutes_list_is_an_empty_tuple_not_an_error(
    tmp_path: Path,
) -> None:
    """A team does not have to have substitutes."""
    payload = match_payload("1-no-substitutes")
    del payload["teams"]["faction1"]["substitutes"]
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    assert client.get_match("1-no-substitutes").teams[0].substitutes == ()


def test_a_player_without_a_game_player_id_keeps_the_row_but_not_the_id(
    tmp_path: Path,
) -> None:
    """A SteamID64 may be missing; that is an observation the stage states out
    loud.

    ``player_id`` on the other hand is the machine's key: without it there is
    no row.
    """
    payload = match_payload("1-partial")
    payload["teams"]["faction1"]["roster"] = [
        {"player_id": "p-1", "nickname": "a"},
        {"player_id": "p-2", "nickname": "b", "game_player_id": "   "},
    ]
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    team = client.get_match("1-partial").teams[0]

    assert [p.player_id for p in team.roster] == ["p-1", "p-2"]
    assert [p.game_player_id for p in team.roster] == [None, None]


def test_the_schedule_is_a_separate_field_from_the_start_time(tmp_path: Path) -> None:
    """The match list has ``scheduled_at`` and not ``started_at`` (measured
    2026-09-04).

    A schedule is a plan and a start time an observation; if they were read
    into the same field, an unplayed match would look as though it had
    started.
    """
    payload = match_payload("1-future", status="SCHEDULED", started_at=0, finished_at=0)
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    match = client.get_match("1-future")

    assert match.started_at is None
    assert match.scheduled_at is not None
    assert match.scheduled_at.timestamp() == 1_755_996_400


def test_a_match_without_an_id_stops_the_run(tmp_path: Path) -> None:
    """Without a ``match_id`` a match cannot be joined to anything."""
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, {"status": "X"}))

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert "match_id" in str(exc.value)


# -- The port and the layers -------------------------------------------------


def test_only_api_errors_leave_the_port(tmp_path: Path) -> None:
    """The port's only promise is :class:`ApiError`.

    An injected transport can raise anything, and its type must not leak all
    the way to the stage: by AD-9 a unit's fault is a ``status``, not a
    traceback on the screen.
    """
    session = FakeSession(ZeroDivisionError("the transport is broken"))
    client = FaceitClient(KEY, tmp_path, base_url=BASE, session=session)

    with pytest.raises(ApiError) as exc:
        client.get_match("1-aaaa")

    assert "ZeroDivisionError" in str(exc.value)
    # No retry: the transport's fault is not a transient rush.
    assert exc.value.attempts == 1


def test_a_transport_without_a_get_method_is_an_api_error(tmp_path: Path) -> None:
    """What the transport cannot do must not leak as another type either."""
    client = FaceitClient(KEY, tmp_path, base_url=BASE, session=object())

    with pytest.raises(ApiError):
        client.get_match("1-aaaa")


def test_the_client_implements_the_port(tmp_path: Path) -> None:
    """The stage sees the port, not the client.

    ``runtime_checkable`` checks **only that the method names exist**, not the
    signatures and not the return types -- that is Python's limit and not this
    test's choice, and it has to be said out loud, so that the test does not
    promise a guard that is not there. The test is valuable all the same: it
    fails a rename that would put the client outside the port. That the
    signatures agree is proved by the tests that use the port, not by this
    one.
    """
    client, _session, _waits = make_client(tmp_path)
    assert isinstance(client, MatchSource)


def test_the_client_closes_only_the_session_it_opened(tmp_path: Path) -> None:
    """An injected transport is the caller's property.

    Closing it would cut connections this class did not open.
    """
    session = FakeSession()
    with FaceitClient(KEY, tmp_path, base_url=BASE, session=session):
        pass
    assert session.closed is False

    own = FaceitClient(KEY, tmp_path, base_url=BASE)
    own.close()  # must not fail


def _third_party_imports(source: str, filename: str) -> set[str]:
    """Collect the top-level packages a file imports -- **both forms of
    import**.

    ``"import requests" in source`` alone would let
    ``from requests import get`` through.
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
    """The domain does not know HTTP -- it sees the port, not the client.

    ``tests/test_layering.py`` watches the arrows between packages; this
    watches the libraries an arrow does not cover: ``requests`` could seep
    into the domain without breaking a single layering rule.

    **Three packages and not one.** ``render`` must compute nothing and
    ``archive`` must know no sources, so neither may know the network any more
    than ``domain`` does. Without them the rule would have been written only
    where it happened to be found.
    """
    src = Path(faceit_module.__file__).resolve().parents[1]
    forbidden = {"requests", "tenacity"}
    for package in ("domain", "render", "archive"):
        for path in (src / package).rglob("*.py"):
            imported = _third_party_imports(
                path.read_text(encoding="utf-8"), str(path)
            )
            leaked = imported & forbidden
            assert not leaked, (
                f"{path.name} imports a network library: {sorted(leaked)}"
            )


# -- The settings section ----------------------------------------------------


def test_the_faceit_section_is_a_typed_model_of_its_own(settings_file: Path) -> None:
    """AD-3: ``[faceit]`` is a section of its own whose values are
    settings."""
    settings = load_settings(settings_file, env_files=())

    assert isinstance(settings.faceit, FaceitSettings)
    assert settings.faceit.retry_attempts >= 1
    assert settings.faceit.page_size <= MAX_FACEIT_PAGE_SIZE
    # The keys are not in the section: they are read from the machine's own
    # .env file.
    assert not hasattr(settings.faceit, "api_key")
    assert not hasattr(settings.faceit, "base_url")


def test_faceit_defaults_match_the_settings_file(settings_file: Path) -> None:
    """The default in the code must not differ from the settings file.

    The repository's convention (cf.
    ``test_report_defaults_match_the_settings_file``): if the default in the
    code differed from the file, a forgotten key would behave differently from
    what the file says -- and nothing would say which of them the run came
    from.
    """
    assert FaceitSettings() == load_settings(settings_file, env_files=()).faceit


def test_every_faceit_value_is_written_out_in_the_settings_file() -> None:
    """Every setting is written out, because the default in the code is not
    visible.

    The same rule as the sections being mandatory: a value that is only in the
    code is not visible in the file that is being adjusted.
    """
    data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
    assert set(data["faceit"]) == set(FaceitSettings.model_fields)


def test_the_settings_file_describes_the_split_cache() -> None:
    """The rule has to be written where the user finds it.

    Each half separately: "the cache is split" alone does not say which rule
    belongs to which half, and that is the whole decision. An earlier version
    described the old behaviour -- that the cache never expires, and that a
    match list once fetched does not update -- which is now wrong. The
    settings file is Finnish, so the assertions below quote it as it stands.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    section = text.split("[faceit]", 1)[1].split("\n[", 1)[0]

    assert "EI VÄLIMUISTITETA LAINKAAN" in section
    assert "FINISHED" in section
    assert "raw/faceit/" in section
    # The rule is symmetric, and it has to be written out: the write
    # condition alone would leave the reader believing that an old file can go
    # on serving a stale response.
    assert "ITSEKORJAAVA" in section
    assert "SAMA EHTO KOSKEE LUKEMISTA" in section
    # The old claims, now wrong, must not stay in the file.
    assert "VÄLIMUISTI EI VANHENE" not in section
    assert "tyhjennetään käsin" not in section
    assert "tarvitse tyhjentää käsin" not in section


def test_the_readme_describes_the_split_cache() -> None:
    """The same rule in the README: the only place read without the code.

    The README is English since 2026-09-07, so the assertions are too. The
    rule they guard is unchanged, and it is guarded here rather than trusted
    because a trimmed README is exactly where a rule goes missing quietly.
    """
    readme = REAL_SETTINGS.parent / "README.md"
    text = readme.read_text(encoding="utf-8")

    assert "The FACEIT cache is split by call type" in text
    assert "/championships/{id}/matches" in text
    assert "/matches/{id}" in text
    assert "FINISHED" in text
    assert "CANCELLED" in text
    assert "self-correcting" in text
    assert "The same condition applies to **reading**" in text
    # Neither the old Finnish headings nor the claims they made may come back.
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
    """**No silent clamping.**

    ``max(1, int(page_size))`` used to let the value 1000 through and produced
    exactly the 400 error that the reasoning for the bound says looks like a
    network fault. The same went for a ceiling of 0.0, which cut every wait
    away so that the retry looked configured but did not happen.
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
    """The same bound at both ends, so that neither guards alone.

    ``MAX_FACEIT_RETRY_ATTEMPTS``'s ``le=`` bound in particular used to be
    without a test although its sibling bounds had their own: it could have
    been removed without anything failing.
    """
    with pytest.raises(ValueError):
        FaceitSettings(**kwargs)


def test_a_budget_smaller_than_one_wait_is_rejected() -> None:
    """A budget that does not cover one wait would make the retry a
    facade."""
    with pytest.raises(ValueError) as exc:
        FaceitSettings(retry_max_delay_seconds=30.0, call_budget_seconds=10.0)

    assert "call_budget_seconds" in str(exc.value)


# --- best_of travels through the port (Story 3.3) ---------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (2, 2),
        (3, 3),
        ("2", 2),
        (0, None),
        (-1, None),
        (True, None),
        ("two", None),
        (None, None),
        ({}, None),
    ],
)
def test_best_of_is_read_only_when_it_is_a_real_match_length(
    tmp_path: Path, raw: object, expected: int | None
) -> None:
    """An unacceptable value is ``None``, not a substitute.

    ``True`` is there in its own right: ``bool`` is an ``int`` in Python, and
    without a refusal it would end up as the value ``1``, that is, "one map"
    -- Story 3.4 would then expect one demo instead of two and nothing would
    say why.
    """
    payload = match_payload("1-bo", best_of=raw)
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    match = client.get_match("1-bo")

    assert match.best_of == expected


def test_best_of_is_not_the_length_of_the_map_list(tmp_path: Path) -> None:
    """In a BO3 that ended two to nil the veto has three maps but there are
    two demos."""
    payload = match_payload(
        "1-bo3",
        best_of=3,
        voting={"map": {"pick": ["de_nuke", "de_ancient", "de_dust2"]}},
    )
    client, _session, _waits = make_client(tmp_path, FakeResponse(200, payload))

    match = client.get_match("1-bo3")

    assert match.best_of == 3
    assert len(match.map_picks) == 3


# -- Story 3.7 (item 12): status codes are classified in one place ------------


def test_status_codes_are_classified_in_exactly_one_place() -> None:
    """429/5xx/non-2xx are classified in **one** function, not in three.

    The same rule was written in three places (``_single_request``,
    ``_as_json``, ``_begin``), and the copies had already drifted apart in
    details that nobody had decided differently: only ``_as_json`` took in the
    interface's own error text, and only ``_single_request`` handled 204/205.
    A fourth call site would inherit at random whichever version its author
    happened to copy.

    **The claim is read from the syntax tree and not from strings.** Every
    function is walked and comparisons against the bounds of the status-code
    ranges are looked for; they may appear in exactly one function. The guard
    on behaviour is this file's other tests, which run all three call sites.
    """
    tree = ast.parse(Path(faceit_module.__file__).read_text(encoding="utf-8"))
    classifiers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Compare):
                continue
            for value in [inner.left, *inner.comparators]:
                if isinstance(value, ast.Constant) and value.value in (
                    200,
                    300,
                    500,
                    600,
                ):
                    classifiers.add(node.name)
                if isinstance(value, ast.Name) and value.id in (
                    "_RATE_LIMIT_STATUS",
                    "_NO_CONTENT_STATUSES",
                ):
                    classifiers.add(node.name)

    assert classifiers == {"_checked_status"}, (
        "Status codes are classified in more than one place: copies always "
        f"drift apart. {sorted(classifiers)}"
    )
