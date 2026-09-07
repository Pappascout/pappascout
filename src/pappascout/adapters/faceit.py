"""The FACEIT Data API implementation of the match port (AD-8, Story 3.1).

**This is the only module that makes HTTP calls.** A stage sees only the
:class:`~pappascout.adapters.protocols.MatchSource` port, so changing the
interface's address, its headers, its pagination or its retries does not touch
the pipeline. The same split as :mod:`pappascout.adapters.demo_parser`'s: the
port is in :mod:`pappascout.adapters.protocols`, the implementation here.

Four rules this module keeps
----------------------------

**The key ends up in nothing readable.** It travels in the ``Authorization``
header and never in the address, so nothing in a cache file's name or its
contents is derived from the key. The class logs nothing, and its
:meth:`FaceitClient.__repr__` is written by hand -- the default repr would
print the attributes, and the key would be among them. The key is kept as a
:class:`~pydantic.SecretStr`, so seeing it takes an explicit
``get_secret_value()`` call. **The address must be ``https``**, and redirects
are not followed: either one would send the header to a place this module did
not choose.

**A retry only for what waiting can fix.** 429 (rate limiting), 5xx (the
interface is broken) and a connection error (a timeout too) are retried with a
growing delay. **Any other 4xx is not.** A wrong id does not become right and a
wrong key does not become valid, so waiting would only delay an error that is
already certain -- and would spend the call quota. If the interface says the
waiting time in a ``Retry-After`` header, **that is listened to** rather than
guessed: it is a measurement that does not have to be calibrated.

**One port call has a time budget.** The number of attempts is not a ceiling
on time: pagination multiplies the attempts by the number of pages, so an
attempt ceiling alone would allow hundreds of requests and an hour of silence
from one ``get_matches`` -- exactly what the reasoning for
``MAX_FACEIT_RETRY_ATTEMPTS`` says it prevents. The budget is in seconds,
because seconds are what the user waits.

**The cache is split by the kind of call, and that is a measured decision.**

``/championships/{id}/matches`` (the match list)
    **Not cached at all** -- neither read nor written.
``/matches/{id}`` (a single match's details)
    **Cached permanently, but only for a finished match**
    (:data:`CACHEABLE_MATCH_STATUSES`). A match fetched while it is being
    played is not written to disk.

This **overturns the spec's constraint** "the cache is a plain HTTP cache, not
an inference about freshness". The constraint was deliberate, but it was
written **before the measurement** and rested on the assumption that responses
are permanent. The measurement on 2026-09-04 showed the assumption true for one
endpoint and false for the other:

* **The match list is one call per run** -- the division's 66 matches fit on a
  single page at ``page_size = 100`` -- and it **changes constantly**: 60 of
  the 66 matches were in state ``SCHEDULED``. A cache would save one call there
  and cost correctness. That is not a trade-off but a plain harm.
* **A match's details are as many as 66 calls** (``collect`` over a whole
  division) and are **unchanging** as soon as the match has been played. There
  a cache is a clear gain. FACEIT's limit is unofficially about 10 000 calls an
  hour.

The requirement that settles this is the product owner's own and is better than
any expiry time: *"As long as we do not fetch duplicates or miss obvious
matches."*
**There is therefore no expiry time** -- no clock, no TTL, no setting. Fewer
moving parts than a rule measured in time, and it says outright what it means:
an unchanging response is kept, a changing one is not.

Otherwise the cache is as it was: one file per call, no manifest, no effect on
the other stages. The directory may be deleted at any time, and the only
consequence is a new call. The same goes for a single broken file: it is
skipped as though it were not there. **A response that has not been checked
first is not written to disk** -- otherwise a broken 200 would stay in the
cache and every later run would fail with the same error without going to the
network at all.

Why the directory is given as a parameter
-----------------------------------------

The adapter does not import :mod:`pappascout.archive`
(``tests/test_layering.py``): it must not know where the archive is or how its
paths are built. The caller gives it a ready directory, which it gets from
``ArchivePaths.raw_faceit()``. The same rule as AD-8's demo download -- the
adapter does not write into the archive but into the place it is told.
"""

from __future__ import annotations

import json
import os
import random
import re
import secrets as _secrets
import socket
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
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

from pappascout.adapters.protocols import (
    DemoStream,
    Match,
    MatchTeam,
    RosterPlayer,
)
from pappascout.domain.models import (
    MAX_FACEIT_PAGE_SIZE,
    MAX_FACEIT_RETRY_ATTEMPTS,
    FaceitSettings,
    Settings,
    secrets_env_path,
)
from pappascout.errors import (
    ApiError,
    DemoUnavailable,
    DownloadsAccessDenied,
    PappascoutError,
    SettingsError,
)

__all__ = [
    "FaceitClient",
    "FACEIT_DATA_API_BASE",
    "MAX_PAGES",
    "DEFAULT_CALL_BUDGET_SECONDS",
    "CACHEABLE_MATCH_STATUSES",
    "FaceitDemoSource",
    "FACEIT_DOWNLOADS_API_BASE",
    "DEMO_READY_STATUSES",
    "DEMO_CHUNK_BYTES",
    "MAX_DEMO_CHUNK_BYTES",
    "split_map_demo_id",
    "DEMO_GONE_STATUSES",
    "DEMO_RETENTION_DAYS",
    "DOWNLOADS_DENIED_STATUSES",
    "DOWNLOADS_BAD_REQUEST",
    "MAX_ERROR_DETAIL_CHARS",
    "DOWNLOADS_STATUS_URL",
    "DOWNLOADS_APPLICATION_URL",
]

#: The root of the FACEIT Data API. **A constant and not a setting**: it is
#: not an adjustable value but the thing this module is written against -- the
#: paths, the header and the shape of the responses are its contract. A test
#: swaps it through the parameter, so that no test can hit the real address by
#: accident.
FACEIT_DATA_API_BASE = "https://open.faceit.com/data/v4"

#: The ceiling on pages in one fetch. A guard and not a setting.
#:
#: Pagination ends when the interface returns an empty or a partial page. If it
#: never did so -- a fault in the interface, or a misread ``offset`` -- the
#: loop would go on for ever and spend the call quota quietly. 200 pages is
#: 20 000 matches at a full page size, that is, many tens of times what one
#: division season can hold.
MAX_PAGES = 200

#: One port call's time budget in seconds, when the caller gives none of its
#: own.
#:
#: **A guard in the same sense as :data:`MAX_PAGES`**, not a neutral default.
#: The number of attempts does not bound the time: ``MAX_PAGES`` pages times
#: ``retry_attempts`` attempts is hundreds of requests, and with a growing
#: delay an hour of silence. The ceiling therefore has to be in seconds.
DEFAULT_CALL_BUDGET_SECONDS = 300.0

#: The characters that are acceptable in a cache file's name as they are.
_UNSAFE_IN_NAME = re.compile(r"[^A-Za-z0-9._-]+")

#: The rate-limiting status code. It and 5xx are the ones waiting can fix.
_RATE_LIMIT_STATUS = 429

#: The 2xx codes that have **no body** (RFC 9110). Successful but empty.
_NO_CONTENT_STATUSES = frozenset({204, 205})

#: The match states in which its details **may** be cached permanently.
#:
#: The measured data (2026-09-04) holds ``SCHEDULED`` and ``FINISHED``; FACEIT
#: also has ``ONGOING`` and ``CANCELLED``.
#:
#: **``FINISHED`` and only it, not ``CANCELLED`` as well** -- even though a
#: cancelled match looks just as final. The choice is because of the
#: asymmetric cost, not for tidiness:
#:
#: * ``FINISHED`` is a **fact about the past**. The match has been played, the
#:   demo exists, and neither the roster nor the map picks can change any more.
#: * ``CANCELLED`` is **the organiser's decision**, and the organiser can undo
#:   it. A rescheduled match is played at a new time under the same
#:   ``match_id``, and a permanently cached ``CANCELLED`` would hide it for
#:   ever.
#:
#: The cost of getting the choice the wrong way round settles it: caching
#: ``CANCELLED`` would save **one call**, and where it failed it would lose
#: **a played match's demo permanently** (FACEIT keeps demos for about 30
#: days). That is exactly what the product owner's requirement "do not miss
#: obvious matches" forbids.
CACHEABLE_MATCH_STATUSES = frozenset({"FINISHED"})

#: The advice that belongs in every error whose cause may be on disk.
#:
#: A response is checked before it is written, so a broken response no longer
#: stays in the cache -- but a file written by an older version may still be
#: there, and the user cannot know that without being told.
_CACHE_ADVICE = (
    "If the error repeats, clear the cache (the archive's raw/faceit/ "
    "directory) -- it may be deleted at any time, and the only consequence is "
    "a new call."
)


class _Retryable(Exception):
    """A transient fault: worth retrying with a growing delay."""

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
        #: The waiting time in seconds that the interface itself gave, or
        #: ``None``.
        self.retry_after = retry_after


class _Permanent(Exception):
    """A fault that waiting does not fix -- no retry is made.

    ``detail`` is **the interface's own error text**, if the response carried
    one. It is an observation: our guess at what was wrong with the request is
    a guess, but what FACEIT itself says is a fact -- and often the only way
    to tell apart two causes that end in the same status code.
    """

    def __init__(
        self,
        reason: str,
        *,
        status_code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.detail = detail


class _Exhausted(Exception):
    """The call's time budget ran out part-way."""

    def __init__(self, reason: str, *, status_code: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


class _Invalid(Exception):
    """The response arrived but is not what the port promises."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _Fetched:
    """The response and how it was obtained.

    The metadata travels with it, because
    :class:`~pappascout.errors.ApiError`'s ``attempts`` and ``status_code``
    are the input to Story 3.4's ``no_demo`` / ``download_failed`` decision.
    Without this, conversion and structure errors would report ``attempts=1``
    and ``status_code=None`` -- that is, "no response was obtained at all" --
    even though the response was a 200 after four attempts.

    ``attempts = 0`` and ``status_code = None`` together mean a cache hit: the
    network was not touched once.
    """

    payload: Mapping[str, Any]
    attempts: int
    status_code: int | None
    url: str
    from_cache: bool


class _Budget:
    """One port call's time budget."""

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
    """Make sure only an :class:`ApiError` leaves a port method.

    The port's only promise is ``ApiError``
    (:class:`~pappascout.adapters.protocols.MatchSource`). An injected
    transport, a broken JSON library or any other unexpected exception would
    otherwise leak all the way to the stage as a type the caller does not know
    how to catch -- and by AD-9 a unit's fault is a ``status``, not a
    traceback on the screen.

    Any other :class:`~pappascout.errors.PappascoutError` is let through as it
    is: it already says its own reason.
    ``BaseException`` (Ctrl-C, ``SystemExit``) does not belong here at all: it
    is not the interface's fault and it must not be swallowed.
    """

    @wraps(method)
    def wrapper(self: "FaceitClient", *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except PappascoutError:
            raise
        except Exception as exc:  # noqa: BLE001 - the port's promise, see docstring
            raise ApiError(
                f"The FACEIT call ended in an unexpected error "
                f"({type(exc).__name__}).\n"
                "This is the tool's own fault and not the interface's.\n"
                f"{_CACHE_ADVICE}"
            ) from exc

    return wrapper


class FaceitClient:
    """The FACEIT Data API client: key, retries, cache, pagination.

    Implements the :class:`~pappascout.adapters.protocols.MatchSource` port.

    **Every FACEIT call goes through this class** (AD-8). That is the whole
    reason Story 3.1 exists: one place where the key is read, one place where
    the retry policy is defined, and one place where responses are cached.

    Args:
        api_key: The FACEIT Data API key. **Not read here but given**: the
            only way to read the key is
            :meth:`~pappascout.domain.models.Settings.require_faceit_api_key`,
            and :meth:`from_settings` is the place that calls it.
        cache_dir: The directory for the response cache (``raw/faceit/``).
            Created when needed. The adapter does not know the archive, so the
            path is given ready-made.
        base_url: The interface's root. Defaults to
            :data:`FACEIT_DATA_API_BASE`. **Must be ``https``**, because the
            key travels in a header.
        retry_attempts: The total number of attempts. **Defaults to 1**, that
            is, no retry -- the same line as
            :class:`~pappascout.adapters.demo_parser.Demoparser2Adapter`'s:
            the adapter does not read settings, and the neutral default is the
            one that does nothing surprising. In production the value comes
            from the ``[faceit]`` section.
        retry_initial_delay_seconds: The first wait in seconds. The delay
            doubles on every round. Defaults to 0.0 for the same reason.
        retry_max_delay_seconds: The ceiling on a single wait in seconds, or
            ``None`` = no ceiling. **The default is ``None`` and not zero**:
            zero would be a ceiling that cuts every wait away, so that giving
            an initial delay without a ceiling would do nothing -- a fault
            that would look as though it worked. A value smaller than the
            initial delay is **an error and not a silent clamp**.
        retry_jitter_share: The jitter's share of the wait (0.0-1.0). The wait
            is ``delay * (1 + share * random number)``. Without jitter two
            parallel runs would hit the rate limit in the same second again
            and again. Defaults to 0.0, so that the adapter's default
            behaviour is deterministic.
        timeout_seconds: One HTTP call's timeout in seconds.
        page_size: How many rows are asked for in one page. At most
            :data:`~pappascout.domain.models.MAX_FACEIT_PAGE_SIZE`.
        call_budget_seconds: One port call's time budget in seconds -- the
            whole pagination and every retry together. Defaults to
            :data:`DEFAULT_CALL_BUDGET_SECONDS`.
        session: The transport, of which ``requests.Session``'s
            ``get(url, headers=..., params=..., timeout=...,
            allow_redirects=...)`` is expected. By default one of its own is
            created, and closed in :meth:`close`. **The tests give this** --
            it is the seam that keeps the whole test suite offline.
        sleep: The waiting function between retries. Defaults to
            ``time.sleep``. A test gives a recording function, which makes the
            growth of the delay measurable without the test waiting a second.
        clock: The monotonic clock for the time budget. Defaults to
            ``time.monotonic``.
        random_source: The random source for the jitter; returns a value in
            ``[0, 1)``. Defaults to ``random.random``.

    Raises:
        ~pappascout.errors.SettingsError: If some value is outside its bounds
            or the address is not ``https``. **No silent clamping**: the same
            principle as the settings sections, where an unacceptable value is
            an error and not something to skip past. A clamped
            ``page_size = 1000`` would produce exactly the 400 error that the
            reasoning for the bound says looks like a network fault.
        ~pappascout.errors.ApiError: For every network, status-code and format
            error. The message says what was being fetched.
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
        #: How many times the network was really touched. A cache hit does
        #: not increase this, so "the second run does not go to the network"
        #: is measurable rather than inferred.
        self.requests_made = 0
        #: How many times a response came from the cache.
        self.cache_hits = 0

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        cache_dir: Path,
        **kwargs: Any,
    ) -> "FaceitClient":
        """Build the client from the settings and the machine's own key file.

        **This is the only place that reads the key.** It calls
        :meth:`~pappascout.domain.models.Settings.require_faceit_api_key`,
        which raises an error naming the file's path and the line that is
        needed, if there is no key. There is no second way to read it: this
        module does not touch ``os.environ`` at all, so an environment
        variable alone does not count as a key -- the key has to be read by
        the route that ``Settings`` knows.

        The method takes the whole
        :class:`~pappascout.domain.models.Settings` rather than just the
        ``[faceit]`` section, because the key is in no section: it is
        ``Settings``'s own field, and the sectioning (AD-3) is about the
        settings file and not about keys.

        Args:
            settings: The loaded settings.
            cache_dir: The cache directory, usually
                ``ArchivePaths.raw_faceit()``.
            **kwargs: The transport's seams (``session``, ``sleep``,
                ``clock``, ``random_source``). **A setting cannot be
                overridden through this** -- they always come from the
                ``[faceit]`` section, and a duplicate name raises
                ``TypeError``.

        Raises:
            ~pappascout.errors.SettingsError: If the key is missing or empty.
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
        """A representation **without the key**.

        Written by hand on purpose: the default repr would print the
        attributes, and the key would be among them -- in a traceback, in an
        error message and in a debugger.
        """
        return (
            f"FaceitClient(base_url={self.base_url!r}, "
            f"cache_dir={str(self.cache_dir)!r})"
        )

    def close(self) -> None:
        """Close the transport of its own. **A given transport is not closed.**

        An injected ``session`` is the caller's property: closing it would cut
        connections this class did not open.
        """
        if self._owns_session:
            close = getattr(self._session, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "FaceitClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -- The port ------------------------------------------------------------

    @_only_api_errors
    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        """See the port's documentation.

        Fetches every page and returns one list: pagination is a detail of the
        transport and not something the stage knows. The whole fetch has one
        time budget.
        """
        path = f"/championships/{competition_id}/matches"
        budget = _Budget(self.call_budget_seconds, self._clock)
        items: list[Mapping[str, Any]] = []
        #: The ``match_id`` values seen. Pagination rests on ``offset``, and
        #: the interface may return the same row on two pages if the list
        #: changes during the fetch -- exactly what happens mid-season.
        #: Without deduplication the same match would be counted twice.
        seen: set[str] = set()
        offset = 0
        last: _Fetched | None = None

        for page_no in range(1, MAX_PAGES + 1):
            if budget.expired():
                raise self._budget_error(
                    f"championship {competition_id}'s matches", budget, last
                )
            last = self._get(
                path,
                {"type": "all", "offset": offset, "limit": self.page_size},
                what=(
                    f"championship {competition_id}'s matches "
                    f"(page {page_no}, offset {offset})"
                ),
                validate=_check_match_list,
                # **The match list is not cached.** It is one call per run and
                # it changes constantly (measured 2026-09-04: 60 of the 66
                # matches in state SCHEDULED). A cache would save one call and
                # cost correctness -- see the module docstring.
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
            # **The real length of the page, not the requested one.** If the
            # interface returns more than was asked for, ``offset +=
            # page_size`` would skip over rows; if less, the list has ended.
            # Every row has been checked (:func:`_check_match_list`), so there
            # is no filtered length and unfiltered length -- there used to be
            # two of them, and they were different numbers for the same page.
            if len(page) < self.page_size:
                break
            offset += len(page)
        else:
            raise ApiError(
                f"FACEIT returned, for championship {competition_id}, more "
                f"than {MAX_PAGES} pages of matches, and the pagination did "
                "not end.\n"
                "The fetch was stopped so that it would not spend the call "
                "quota endlessly. This is a fault in the interface.",
                url=self._url(path),
                attempts=last.attempts if last else 1,
                status_code=last.status_code if last else None,
            )
        return tuple(_to_match(item) for item in items)

    @_only_api_errors
    def get_match(self, match_id: str) -> Match:
        """See the port's documentation."""
        return _to_match(self.match_payload(match_id))

    @_only_api_errors
    def match_payload(self, match_id: str) -> Mapping[str, Any]:
        """A match's **raw response** -- from the same cache as
        :meth:`get_match`.

        The port's :class:`~pappascout.adapters.protocols.Match` is
        deliberately narrower than the response: it does not speak FACEIT's
        vocabulary. But :class:`FaceitDemoSource` needs exactly that
        vocabulary -- the ``instances`` list, whose ``round`` states the map's
        number explicitly (measured 2026-09-05) -- and it must not be raised
        into the port merely because this module needs it internally.

        The method is therefore **between adapters and not part of the port**:
        only this module sees it. The alternative would have been a second
        fetch path for the same response, and then the same match would be
        fetched twice and under two cache keys.
        """
        fetched = self._get(
            f"/matches/{match_id}",
            None,
            what=f"match {match_id}'s details",
            validate=_check_match,
            # A played match's details do not change any more, and they are
            # fetched as many as 66 times per run -- there the cache is a
            # clear gain. An unfinished match is not written to disk.
            cache_when=_is_cacheable_match,
        )
        return fetched.payload

    # -- The transport -------------------------------------------------------

    @property
    def session(self) -> Any:
        """The transport this client speaks over. **Not closed from here.**

        :class:`FaceitDemoSource` uses the same session: one connection pool,
        one injection seam for the tests. Without this the demo source would
        have to either open a session of its own -- in which case a test could
        go to the network although the client does not -- or read a private
        attribute.
        """
        return self._session

    def new_budget(self, seconds: float | None = None) -> "_Budget":
        """A new time budget on this client's clock.

        A budget is **per port call**, so it is created by whoever begins the
        call. The demo source is one of those callers, and it should not have
        to know the clock a test injects into this client.
        """
        return _Budget(
            self.call_budget_seconds if seconds is None else seconds, self._clock
        )

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _budget_error(
        self, what: str, budget: _Budget, last: _Fetched | None
    ) -> ApiError:
        return ApiError(
            f"The FACEIT fetch did not finish within the time budget while "
            f"fetching {what}.\n"
            f"The budget was {budget.seconds:g} s and {budget.elapsed():.1f} s "
            "went by.\n"
            "The interface is slow or is rate-limiting calls. Wait a moment "
            "and run the command again, or raise the "
            "[faceit].call_budget_seconds setting.",
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
        """Fetch one response: from the cache or the network, and into the
        cache.

        Args:
            path: The path from the interface's root, e.g. ``/matches/<id>``.
            params: The query parameters, or ``None``.
            what: What was being fetched. Ends up in the error message -- a
                status code alone points nowhere.
            validate: The structure check, which raises :class:`_Invalid`. Run
                both before a write to the cache and when reading from the
                cache.
            cache_when: The condition under which a response is **written** to
                disk and under which a file is **read** from disk.
                **``None`` = this call is not cached at all**: neither read
                nor written. The match list is exactly such a call, and then
                the whole cache branch is skipped -- no dead code that would
                look as though the cache were in use.

                **The condition is applied in both directions, and that is a
                fix to a proven fault.** At first the read path was left
                unconditional, on the reasoning that "if the file exists, it
                was written at some point from a response that passed this
                condition". A live check on 2026-09-04 showed the premise
                false the same day: a shared archive held a file written by an
                earlier version, which served a ``SCHEDULED`` match from disk
                **for ever and entirely silently** -- that is, exactly the
                class of fault this story otherwise fixes.

                The symmetric condition makes the cache **self-correcting**:
                the invariant no longer depends on every earlier and future
                write path having been right, but on what the file says. One
                bug or one old version does not poison the cache permanently,
                and nobody has to remember to clean the directory by hand. The
                cost is zero: the condition already exists and this adds no
                new dependency on content -- it applies one that is there.
            budget: The time budget; ``None`` = a budget of its own for this
                call.
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
                # An unusable file behaves as though it were not there: it is
                # deleted and the response is fetched from the network. **One
                # path for two causes** (a broken structure, unusable
                # contents), because the consequence is the same for both and
                # the caller does not have to know which it was.
                try:
                    cache_path.unlink(missing_ok=True)
                except OSError:  # pragma: no cover - depends on the disk
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
            # The cache advice only where this call is cached. For the match
            # list it would be the wrong advice: there is nothing to clear
            # there, and an error message must not point at the wrong place.
            advice = f"\n{_CACHE_ADVICE}" if cache_path is not None else ""
            raise ApiError(
                f"FACEIT returned a response that is not recognised, while "
                f"fetching {what}: {exc.reason}\n"
                "The response was NOT written to the cache, so the next run "
                "tries again.\n"
                "Check that the id is right (settings.toml, "
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
        """Make a JSON call to the network and retry if waiting can fix the
        fault.

        ``cache_advice`` says whether this call is cached: the advice to clear
        the cache belongs in a permanent fault's message only where there can
        be something in the cache. For the match list the same advice would
        point at a place that holds nothing.
        """
        return self._retry(
            lambda: self._single_request(url, params),
            what=what,
            budget=budget,
            url=url,
            cache_advice=cache_advice,
        )

    def _retry(
        self,
        call: Callable[[], tuple[Any, int | None]],
        *,
        what: str,
        budget: _Budget,
        url: str | None,
        cache_advice: bool = False,
    ) -> tuple[Any, int, int | None]:
        """Run ``call`` under the retry policy and turn a fault into an
        ``ApiError``.

        **The policy is here and only here.** Story 3.4's demo download needs
        exactly the same rule -- 429 and 5xx by waiting, any other 4xx never
        -- and it must not be written a second time: two copies would drift
        apart, and each would look right on its own. The only difference is
        the payload: a JSON call returns a dictionary, a demo download an open
        response.

        Args:
            call: One attempt. Returns the pair ``(value, status code)`` and
                raises :class:`_Retryable` or :class:`_Permanent`.
            what: What was being fetched. Ends up in the error message.
            budget: The time budget for the whole set of attempts.
            url: The address for the error message, or ``None``. **In the demo
                download this is ``None``**, because the address there is a
                signed download link, that is, an authorisation -- and
                ``ApiError`` is precisely the route by which it would end up
                in a log and on the screen.
            cache_advice: Whether the advice to clear the cache belongs in the
                message.
        """
        attempts = 0
        #: The last status code seen, in a list so that the closures can
        #: write to it. It ends up in ``ApiError.status_code`` even where the
        #: final cause is the budget and not a response.
        status_seen: list[int | None] = [None]

        def attempt() -> Any:
            nonlocal attempts
            if budget.expired():
                raise _Exhausted(
                    "the time budget ran out before the next attempt",
                    status_code=status_seen[0],
                )
            attempts += 1
            self.requests_made += 1
            try:
                payload, status = call()
            except (_Retryable, _Permanent) as exc:
                # The status code is recorded from a failed attempt too.
                # Without this a fetch that ended at the budget would report
                # ``status_code=None``, that is, "no response was obtained at
                # all", although every attempt got a 503 back.
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
            """The wait: the interface's own ``Retry-After`` before the
            growing delay.

            The jitter is added on top (``delay * share * random number``), so
            that two parallel runs do not hit the rate limit in the same
            second again and again.
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
            # The budget is checked BEFORE the wait and not after it: a wait
            # that would take the budget over is silence with no chance of
            # succeeding. ``Retry-After`` in particular can be minutes.
            if delay > budget.remaining():
                raise _Exhausted(
                    f"the next wait would be {delay:.1f} s but the budget has "
                    f"{max(budget.remaining(), 0.0):.1f} s left",
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
                f"FACEIT did not answer while fetching {what}: {exc.reason}\n"
                f"It was tried {attempts} times with a growing delay.\n"
                "This is a transient fault (rate limiting or an interruption "
                "in the interface). Wait a moment and run the command again.",
                status_code=exc.status_code,
                attempts=attempts,
                url=url,
            ) from exc
        except _Permanent as exc:
            advice = f"\n{_CACHE_ADVICE}" if cache_advice else ""
            said = f"\nFACEIT said: {exc.detail}" if exc.detail else ""
            error = ApiError(
                f"FACEIT rejected the call while fetching {what}: {exc.reason}"
                f"{said}\n"
                "No retry was made, because the fault is not fixed by "
                f"waiting.{advice}",
                status_code=exc.status_code,
                attempts=attempts,
                url=url,
            )
            # The interface's own text travels with it, so that the caller
            # can build a more precise message without seeing the HTTP layer.
            error.detail = exc.detail
            raise error from exc
        except _Exhausted as exc:
            raise ApiError(
                f"The FACEIT fetch did not finish within the time budget "
                f"while fetching {what}: {exc.reason}\n"
                f"The budget was {budget.seconds:g} s and there was time for "
                f"{attempts} attempts.\n"
                "The interface is slow or is rate-limiting calls. Wait a "
                "moment and run the command again, or raise the "
                "[faceit].call_budget_seconds setting.",
                status_code=exc.status_code,
                attempts=attempts,
                url=url,
            ) from exc
        return payload, attempts, status_seen[0]

    def _single_request(
        self, url: str, params: Mapping[str, Any] | None
    ) -> tuple[Mapping[str, Any], int]:
        """One HTTP call. Raises :class:`_Retryable` or :class:`_Permanent`.

        **The key is here and only here.** It is built into the header at the
        moment of the call and not kept ready-formatted, so that it does not
        exist a moment longer than it has to.

        ``allow_redirects=False``: a redirect would send the
        ``Authorization`` header to an address this module did not choose. A
        3xx therefore ends in a status-code error like any other unexpected
        response.
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
            # A timeout and a connection error are transient. The exception's
            # TYPE and not its message gives the reason: the message holds the
            # address and the library's own wording, and neither guides the
            # user.
            raise _Retryable(f"the connection failed ({type(exc).__name__})") from exc
        except Exception as exc:  # noqa: BLE001 - the transport is injectable
            # Anything that is not requests' own exception comes from the
            # transport the caller gave. It is not a transient fault, so it is
            # not retried -- but neither may it leak through the port.
            raise _Permanent(
                f"the transport raised an exception ({type(exc).__name__})"
            ) from exc

        # The body is parsed as JSON, so an empty 204/205 is a fault here. The
        # interface's own error text is not read on the Data API path.
        status = _checked_status(response, no_content=True)

        try:
            payload = response.json()
        except ValueError as exc:
            # No page in the output: an HTML error page is kilobytes, and it
            # would bury the error message -- and there is nothing in its
            # contents that would guide the user. The length and the type say
            # what matters here: the response was not JSON.
            length = len(getattr(response, "text", "") or "")
            content_type = "unknown"
            headers_in = getattr(response, "headers", None)
            if isinstance(headers_in, Mapping):
                content_type = str(headers_in.get("Content-Type", "unknown"))
            raise _Permanent(
                f"the response was not JSON (status code {status}, "
                f"Content-Type {content_type}, {length} characters)",
                status_code=status,
            ) from exc

        if not isinstance(payload, Mapping):
            raise _Permanent(
                f"the response was not an object but a {type(payload).__name__}",
                status_code=status,
            )
        return payload, status

    # -- The cache -----------------------------------------------------------

    def _cache_path(self, path: str, params: Mapping[str, Any] | None) -> Path:
        """The cache file's path: a readable name plus a short digest.

        Nothing in the name is derived from the key -- the key travels in the
        header and not in the address. The readable part says at a glance
        which call it is about; the digest guarantees uniqueness even where
        the readable part has been truncated by the length limit.

        **The digest is computed from the whole address, not from the path
        alone.** Two different roots sharing one ``cache_dir`` would otherwise
        serve each other's responses, and nothing would say so.
        """
        query = "&".join(f"{k}={params[k]}" for k in sorted(params)) if params else ""
        readable_part = f"{path}?{query}" if query else path
        canonical = f"{self.base_url}{readable_part}"
        digest = sha256(canonical.encode("utf-8")).hexdigest()[:12]
        readable = _UNSAFE_IN_NAME.sub("-", readable_part).strip("-")[:80]
        return self.cache_dir / f"{readable}-{digest}.json"


# -- Checking the parameters -------------------------------------------------
#
# The adapter does not read settings, but neither may it quietly accept what
# the settings model rejects. The bounds are the same constants as the
# settings model's (:mod:`pappascout.domain.models`), so one number is not in
# two places.


def _check_int(value: Any, name: str, low: int, high: int) -> int:
    number = int(value)
    if not low <= number <= high:
        raise SettingsError(
            f"FaceitClient: {name} = {number} is outside the allowed range "
            f"{low}-{high}.\n"
            "Correct the value in the settings file's [faceit] section."
        )
    return number


def _check_float(value: Any, name: str, low: float, high: float | None = None) -> float:
    number = float(value)
    if number < low or (high is not None and number > high):
        limit = f"{low:g}-{high:g}" if high is not None else f"at least {low:g}"
        raise SettingsError(
            f"FaceitClient: {name} = {number:g} is not acceptable "
            f"(allowed {limit}).\n"
            "Correct the value in the settings file's [faceit] section."
        )
    return number


def _check_base_url(base_url: str) -> str:
    """Require ``https``.

    The key travels in the ``Authorization`` header, and ``http`` would send
    it in the clear. In a module whose reason for existing is protecting the
    key, this is a structural rule and not a habit.
    """
    trimmed = str(base_url).rstrip("/")
    if urlsplit(trimmed).scheme != "https":
        raise SettingsError(
            f"FaceitClient: the address {trimmed!r} must be https.\n"
            "The key travels in the Authorization header, and http would send "
            "it unencrypted."
        )
    return trimmed


# -- Checking the response's structure ---------------------------------------
#
# Run BEFORE a write to the cache. Without it a broken 200 would stay on disk,
# and every later run would fail with the same error without going to the
# network at all -- that is, the cache would turn into state that has to be
# cleaned up.


def _is_cacheable_match(payload: Mapping[str, Any]) -> bool:
    """May this match's details be written to the cache permanently?

    The field read is ``status``, and it is **the most stable one FACEIT
    has**: a match's life cycle is the interface's most basic concept, so if
    this field changes, the whole interface has changed -- and then there is
    no alternative that would have gone on working. Reading it is therefore
    not the same kind of thing as guessing the shape of the response.

    An unknown or missing state **does not qualify**: it is "I do not know",
    and "I do not know" is no reason to keep a response for ever. Adding a new
    state to FACEIT therefore leads to one extra call and not to a wrong
    answer -- the right direction of the two.

    The comparison is case-insensitive, because ``status`` is an observation
    and not a value this module wrote.
    """
    status = _text(payload.get("status"))
    return status is not None and status.upper() in CACHEABLE_MATCH_STATUSES


def _check_match(payload: Mapping[str, Any]) -> None:
    if _text(payload.get("match_id")) is None:
        raise _Invalid("the match has no match_id")


def _check_match_list(payload: Mapping[str, Any]) -> None:
    """Check the match list **row by row**.

    Dropping a junk row silently would be a different rule from the error a
    missing ``items`` raises, and two different rules for the same response
    would mean that the page's length depends on which of them is looked at.
    As it is, an unacceptable row stops the fetch just as an unacceptable
    response does.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        raise _Invalid("the field 'items' was missing or was not a list")
    for index, entry in enumerate(items):
        if not isinstance(entry, Mapping):
            raise _Invalid(
                f"row {index} is not an object but a {type(entry).__name__}"
            )
        if _text(entry.get("match_id")) is None:
            raise _Invalid(f"row {index} has no match_id")


def _checked_status(
    response: Any, *, detail: bool = False, no_content: bool = False
) -> int:
    """The status code from a response, or :class:`_Retryable` /
    :class:`_Permanent`.

    **One classification instead of three.** The same rule was written in
    three places (:meth:`FaceitClient._single_request`,
    :meth:`FaceitDemoSource._as_json`, :meth:`FaceitDemoSource._begin`), and
    the copies had already drifted apart in details that nobody had decided
    differently. The rule itself is the same for all of them:

    * 429 -> ``_Retryable``. Rate limiting is transient by definition.
    * 5xx -> ``_Retryable``. A server error can be fixed by waiting.
    * any other non-2xx -> ``_Permanent``. **Everything that is not 2xx, not
      just >= 400:** a 3xx (a redirect that is not followed) is not JSON, and
      without this branch it would end in the error "the response was not
      JSON" -- which reports the symptom and not the cause. A 404 ends up here
      too, and that is the rule: a demo that is gone is a fact, and waiting
      does not bring back what has been deleted.

    ``retry_after`` is read from the header the same way in every case, so it
    too is here rather than at the call site.

    Args:
        response: The HTTP response. The fields are read with ``getattr``,
            because the transport is injectable and a test's fake does not
            have to be a ``requests.Response``.
        detail: Whether to take **the interface's own error text**
            (:func:`_error_detail`) into the permanent error. The Downloads
            API states it and it is often the only way to tell apart two
            causes that end in the same status code; on the Data API path it
            has not been read, and this merger does not start reading it
            there.
        no_content: Whether 204/205 is a fault for this call site. They
            **are** 2xx, so the previous branch does not cover them -- but
            they have no body at all, and JSON parsing would say of an empty
            response "the response was not JSON". That is true but points
            nowhere. When the byte stream is opened
            (:meth:`FaceitDemoSource._begin`) the body is not parsed as JSON,
            so this condition is not there -- and it is not added.

    Returns:
        The status code, where the response is fit to go on with.
    """
    status = int(getattr(response, "status_code", 0))
    retry_after = _retry_after_seconds(getattr(response, "headers", None))
    if status == _RATE_LIMIT_STATUS:
        raise _Retryable(
            "the interface rate-limited the calls (429)",
            status_code=status,
            retry_after=retry_after,
        )
    if 500 <= status < 600:
        raise _Retryable(
            f"the interface returned a server error ({status})",
            status_code=status,
            retry_after=retry_after,
        )
    if not 200 <= status < 300:
        raise _Permanent(
            f"status code {status}",
            status_code=status,
            detail=_error_detail(response) if detail else None,
        )
    if no_content and status in _NO_CONTENT_STATUSES:
        raise _Permanent(
            f"the interface returned an empty response (status code {status})",
            status_code=status,
        )
    return status


def _retry_after_seconds(headers: Any) -> float | None:
    """Read ``Retry-After`` in seconds, or ``None`` if there is no header.

    **The interface's own measurement beats a guess.** The growing delay is a
    blind assumption; ``Retry-After`` is the number the server itself states,
    and ``settings.toml`` admits itself that the length of 429 spells has not
    been measured. The header is either seconds or an HTTP date (RFC 9110),
    and both are in use. A date pointing into the past and a negative number
    are ``None``: they would mean "do not wait at all", which is not a waiting
    time.
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
    """May this cache file's contents be used as the response?

    **Two conditions, the same consequence.** The file qualifies only if its
    structure is what the port promises (``validate``) **and** its contents
    are such that it could have ended up on disk at all (``cache_when``).
    Either rejection leads to the same thing: the file is deleted and the
    response is fetched from the network.

    The second condition is a symmetry that should have been here from the
    start. Without it the invariant "only unchanging responses are on disk"
    would rest for ever on every write path -- past versions' included --
    having been right. The measured counter-example was found in a shared
    archive on 2026-09-04.

    The return value is a truth value and not a reason, because the reason is
    shown to nobody: an unusable file is not an error but a missing file.
    """
    try:
        validate(payload)
    except _Invalid:
        return False
    return cache_when(payload)


def _read_cache(path: Path) -> Mapping[str, Any] | None:
    """Read a cache file, or return ``None``.

    A broken or unreadable file is skipped as though it were not there: the
    cache is a plain HTTP cache whose only consequence when deleted is a new
    call. Raising an exception would turn it into state that has to be cleaned
    up.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _write_cache(path: Path, payload: Mapping[str, Any]) -> None:
    """Write a response into the cache atomically.

    The same rule as the archive's writes (AD-7): a temporary file first, then
    ``os.replace``. A write that was cut short must not leave partial JSON
    that the next run would read as the response. The implementation is here
    and not in :mod:`pappascout.archive.atomic_write`, because the adapter may
    not import the archive package (``tests/test_layering.py``).

    **The clean-up is in ``finally`` and not in the error branch.** An
    interrupt (Ctrl-C) is not an ``OSError``, so ``except OSError`` alone
    would leave a ``.tmp-*`` file in the archive -- exactly what
    ``atomic_write`` promises not to leave. After a successful ``os.replace``
    the temporary file no longer exists, so the clean-up is then a no-op.

    Failing is not an error: the cache is a speed-up, not a result. If the
    disk is full or the directory is read-only, the call has already succeeded
    and the response is in hand -- crashing here would throw it away.
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
        except OSError:  # pragma: no cover - clean-up must not hide the cause
            pass


@lru_cache(maxsize=1)
def _host_tag() -> str:
    """The host name in a form fit for a file name.

    The same rule and the same caching as
    :func:`pappascout.archive.atomic_write.host_tag`'s; a copy because the
    adapter may not import the archive package.
    """
    name = socket.gethostname() or "unknown-host"
    return _UNSAFE_IN_NAME.sub("-", name).strip("-").lower() or "unknown-host"


# -- FACEIT JSON into the core's vocabulary ----------------------------------
#
# The conversion is here and not in the stage (AD-8): ``faction1``, ``voting``
# and epoch seconds are FACEIT's vocabulary, and the port speaks the core's.


def _to_match(payload: Mapping[str, Any]) -> Match:
    """Convert one FACEIT match into a :class:`Match`.

    The input has **already been checked** (:func:`_check_match`), so
    ``match_id`` exists. The other fields are optional: a missing value is
    ``None`` and not a substitute, because each of them is an **observation**
    of what the interface said, and an invented value would look in the table
    exactly like a measured one.
    """
    match_id = _text(payload.get("match_id"))
    if match_id is None:  # pragma: no cover - _check_match has already refused this
        raise _Invalid("the match has no match_id")

    teams_raw = payload.get("teams")
    teams: tuple[MatchTeam, ...] = ()
    if isinstance(teams_raw, Mapping):
        # Sorted by key (``faction1`` before ``faction2``), so that the order
        # of the sides is the same on every run. The dictionary's own order
        # would depend on the order in which the JSON was written.
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
        best_of=_best_of(payload.get("best_of")),
    )


def _to_team(raw: Mapping[str, Any]) -> MatchTeam:
    """Convert a match row's side into a :class:`MatchTeam`.

    ``roster`` and ``substitutes`` are read **separately and both**: FACEIT
    tells them apart, and the union that makes the standing roster is the
    domain's rule and not the adapter's (see :class:`MatchTeam`). Measured
    2026-09-04: every match row had both lists (132/132 team rows).
    """
    return MatchTeam(
        team_id=_text(raw.get("faction_id")) or _text(raw.get("team_id")),
        name=_text(raw.get("name")),
        roster=_to_players(raw.get("roster")),
        substitutes=_to_players(raw.get("substitutes")),
    )


def _to_players(raw: Any) -> tuple[RosterPlayer, ...]:
    """The player list in the source's order; a missing list is an empty tuple.

    A player without a ``player_id`` is dropped -- the id is the machine's
    key, and a row without one cannot be joined to anything.
    ``game_player_id`` on the other hand may be missing: it is **a different
    id** (a SteamID64), and its absence is an observation that the stage sees
    and states out loud.
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
    """The maps played in the order they were chosen -- the definition of
    ``map_index``.

    Missing veto data is an empty tuple, not an error: a future match has no
    veto yet, and that too is a valid observation.
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


#: The largest number that is read as a match's length.
#:
#: The ceiling exists because without it ``99`` would be acceptable and Story
#: 3.4 would expect 99 demos from one match. Nine covers everything that is
#: played in CS2 tournaments (BO1, BO3, BO5, rarely BO7) and leaves room for
#: one unknown format. A value above it is **broken and not rare**: the
#: Pappaliiga regular season has been measured as ``2``.
MAX_BEST_OF = 9


def _best_of(value: Any) -> int | None:
    """The match's length in maps; an unacceptable value is ``None``, not a
    substitute.

    FACEIT gives the number as an integer most of the time, but the string
    ``"2"`` is a familiar alternative from the same interface, so both are
    read.

    Three refusals, and each is an error of its own:

    ``bool``
        ``True`` is an ``int`` in Python, so without a refusal of its own it
        would end up as the value ``1``, that is, "one map".
    **Non-ASCII digit characters**
        ``"\\u00b2".isdigit()`` is true but ``int("\\u00b2")`` raises
        ``ValueError`` -- that is, ``isdigit`` alone would bring down the
        parsing of the whole match list over one field. ``isascii`` at the
        same time shuts out digits from other writing systems, which
        ``isdecimal`` would let through.
    **Numbers outside the bounds**
        Zero and a negative are not lengths; for the ceiling see
        :data:`MAX_BEST_OF`.

    >>> _best_of(2), _best_of("3"), _best_of(True), _best_of(0), _best_of(None)
    (2, 3, None, None, None)
    >>> _best_of("\\u00b2"), _best_of(99), _best_of(-1), _best_of("2.5")
    (None, None, None, None)
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text.isascii() or not text.isdigit():
            return None
        value = int(text)
    if not isinstance(value, int):
        return None
    return value if 1 <= value <= MAX_BEST_OF else None


def _text(value: Any) -> str | None:
    """A string as an observation: empty or another type is ``None``, not a
    substitute."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _moment(value: Any) -> datetime | None:
    """Epoch seconds into a UTC-aware moment.

    FACEIT gives times as epoch seconds, and ``0`` means "not set" -- not the
    year 1970. Anything that is not a number is ``None``: a guess would look
    like a moment.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC)
    except (OverflowError, OSError, ValueError):
        return None


# -- The demo source (Story 3.4) ---------------------------------------------


#: The root of FACEIT's Downloads API. A constant for the same reason as
#: :data:`FACEIT_DATA_API_BASE`: it is not an adjustable value but the thing
#: this module is written against. A test swaps it through the parameter.
FACEIT_DOWNLOADS_API_BASE = "https://open.faceit.com/download/v2"

#: The match states in which it is **worth trying to fetch** the demo.
#:
#: A third question about the same word, and so a third constant.
#: :data:`CACHEABLE_MATCH_STATUSES` asks "may the response be stored for
#: ever", ``stages.discover.PLAYED_STATUSES`` asks "has the match been
#: played", and this asks "can the recording be assumed to exist". A shared
#: constant would tie three different decisions to each other: if FACEIT ever
#: added a state in which a match has been played but not yet recorded, the
#: first two would want it and this one would not.
DEMO_READY_STATUSES = frozenset({"FINISHED"})

#: How many bytes are read at a time. Not a setting but a detail of the
#: transport.
DEMO_CHUNK_BYTES = 1024 * 1024

#: The ceiling on a chunk's size. A guard: a whole chunk is in memory at
#: once, and a 200 MB chunk would turn a streaming download into a read into
#: memory.
MAX_DEMO_CHUNK_BYTES = 64 * 1024 * 1024


def split_map_demo_id(map_demo_id: str) -> tuple[str, int]:
    """Split ``{match_id}-{map_index}`` into its parts.

    The split is at **the last hyphen** and not the first: FACEIT's
    ``match_id`` is itself of the form ``1-<uuid>`` and holds five hyphens.
    Splitting at the first one would give ``"1"`` as the ``match_id`` and
    would succeed quietly -- there is no match ``1``, but the error would come
    only from the interface and would look like a network fault.

    Raises:
        ~pappascout.errors.DemoUnavailable: If the id is not of this form.
            **Not ``ApiError``**: an id of the wrong form is not the
            interface's fault, and it must not be retried.
    """
    head, sep, tail = str(map_demo_id).rpartition("-")
    if not sep or not head or not tail.isdigit():
        raise DemoUnavailable(
            f"The id {map_demo_id!r} is not of the form "
            "'{match_id}-{map_index}', so no demo can be fetched for it.\n"
            "The end of the id must be the map's 0-based ordinal."
        )
    return head, int(tail)


def _round_number(value: Any) -> int | None:
    """``instances[i].round`` as an integer, or ``None`` if it is not a
    number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _demo_resource_url(
    payload: Mapping[str, Any], map_index: int, map_demo_id: str
) -> str:
    """Find the map's recording address in the ``instances`` list.

    **``round`` is read, the position in the list is not counted.** Measured
    2026-09-05 (``mittaus-faceit-aineisto.md`` chapter 9): every instance has
    a ``round``, which is the map's **1-based** number, so the right instance
    is the one where ``round == map_index + 1``. A lookup that rested on the
    position would be right for exactly as long as every map in the veto is
    also played -- and quietly wrong from the moment a BO3 that ended two to
    nil leaves one unplayed. Then the demo would be stored under the wrong
    map's name, and nothing would say so.

    **Every instance is walked, and the first hit is not the last word.** An
    empty ``demos`` list at the right ``round`` no longer decides the lookup:
    the same map can appear on several rows (a rematch instance, an
    interrupted recording), and stopping at the first would report "no
    recording" even though the next row has an address -- and the ``no_demo``
    state is final, so the error would be permanent.

    Raises:
        ~pappascout.errors.DemoUnavailable: If there is no instance for the
            map, if none of its instances has a recording, or if there are
            **several recordings and no choice can be made**. Three different
            messages, because they are three different facts.
    """
    instances = payload.get("instances")
    rounds_seen: list[int] = []
    matched = 0
    urls: list[str] = []
    if isinstance(instances, Sequence) and not isinstance(instances, (str, bytes)):
        for entry in instances:
            if not isinstance(entry, Mapping):
                continue
            round_no = _round_number(entry.get("round"))
            if round_no is None:
                continue
            rounds_seen.append(round_no)
            if round_no != map_index + 1:
                continue
            matched += 1
            demos = entry.get("demos")
            if isinstance(demos, Sequence) and not isinstance(demos, (str, bytes)):
                for candidate in demos:
                    url = _text(candidate)
                    if url is not None and url not in urls:
                        urls.append(url)

    if len(urls) == 1:
        return urls[0]

    if len(urls) > 1:
        # **No silent choice.** Ambiguity is not settled by guessing (the same
        # rule as in the team lookup): the wrong recording would be stored
        # under the right one's name, and nothing would say so. The adapter
        # cannot ask the user, so it says what it found and leaves the unit
        # undone.
        listing = "\n".join(f"    {u}" for u in urls)
        raise DemoUnavailable(
            f"Map {map_index + 1} has {len(urls)} different recordings "
            f"({map_demo_id}), and the tool does not pick one by guessing.\n"
            f"{listing}\n"
            "Download the file you want by hand and put it in the archive's "
            "import directory."
        )

    if matched:
        raise DemoUnavailable(
            f"The match has map {map_index + 1} but no recording "
            f"of it ({map_demo_id}).\n"
            "The map was played, but FACEIT does not offer a demo of it. "
            "That is a different thing from a deleted demo."
        )

    played = (
        ", ".join(str(n) for n in sorted(set(rounds_seen)))
        if rounds_seen
        else "none at all"
    )
    raise DemoUnavailable(
        f"The match has no map {map_index + 1} ({map_demo_id}).\n"
        f"There are recordings of these maps: {played}.\n"
        "The map was therefore not played -- the match was decided before it. "
        "This is not a deleted demo and not a network error."
    )



#: The status code with which the Downloads API rejects a request as
#: malformed.
#:
#: **Two possible causes, and the response does not tell you which.** Measured
#: 2026-09-05 with a malformed token: an id that cannot be parsed produces a
#: 400 -- but so does a ``resource_url`` the Downloads API does not accept.
#: The former concerns every demo, the latter only one. That is why the
#: message names both rather than choosing.
DOWNLOADS_BAD_REQUEST = 400

#: How many characters of the interface's own error text are shown.
#:
#: Truncated, because an HTML error page is kilobytes and would bury the
#: advice. Long enough that a JSON body ``{"message": "..."}`` fits whole.
MAX_ERROR_DETAIL_CHARS = 300

#: The status codes that mean "the id has no authorisation".
#:
#: **Only in the signing call.** The same code means different things in two
#: different places: as the Downloads API's answer it means "the token has no
#: Downloads scope", that is, a situation in which no demo can succeed; as a
#: signed link's answer it means an expired or badly formed signature, that
#: is, **one download's** fault, which may very well succeed with a new link.
#: They must not therefore be handled the same way.
DOWNLOADS_DENIED_STATUSES = frozenset({401, 403})

#: Where a Downloads API application's status is checked.
DOWNLOADS_STATUS_URL = "https://fc-downloads.loza.gg/"

#: Where a Downloads API authorisation is applied for.
DOWNLOADS_APPLICATION_URL = "https://fce.gg/downloads-api-application"

#: The status codes that mean "there is no recording", not "try again".
#:
#: 404 is the ordinary one, 410 (Gone) is the same thing said more
#: explicitly. Neither is fixed by waiting, and for either one a new attempt
#: would cost a signing call, that is, Downloads quota.
DEMO_GONE_STATUSES = frozenset({404, 410})

#: How long FACEIT keeps recordings. **An estimate and not a promise**: the
#: number is the epic's own observation (``epic-3-context.md``), not a
#: guarantee the interface documents. That is why it appears in the error
#: message with the word "about" and not in a calculation.
DEMO_RETENTION_DAYS = 30


@contextmanager
def _gone_is_no_demo(
    payload: Mapping[str, Any], map_demo_id: str
) -> Iterator[None]:
    """Turn a 404/410 into an absence and say **why** the demo is gone.

    The stage's ``no_demo`` vs. ``download_failed`` decision is made from the
    type and not from the message (``errors.ApiError``'s documentation says
    this out loud), so the translation has to happen here -- the adapter is
    the only one that sees the status code.

    The message states the match's age, because "not found" alone does not
    tell the user whether this is the expected expiry or something else. The
    age is computed from ``finished_at``, which is already there in the same
    response -- and no new call is needed for it.
    """
    try:
        yield
    except ApiError as exc:
        if exc.status_code not in DEMO_GONE_STATUSES:
            raise
        raise DemoUnavailable(
            _gone_message(payload, map_demo_id, exc.status_code)
        ) from None


def _gone_message(
    payload: Mapping[str, Any], map_demo_id: str, status_code: int | None
) -> str:
    """The explanation for the recording no longer existing."""
    finished = _moment(payload.get("finished_at"))
    if finished is None:
        age = (
            "The match's finish time was not in the response, so its age "
            "cannot be given."
        )
    else:
        days = max((datetime.now(UTC) - finished).days, 0)
        age = (
            f"The match finished on {finished.date().isoformat()}, that is, "
            f"{days} days ago."
        )
    return (
        f"FACEIT no longer offers demo {map_demo_id} "
        f"(HTTP {status_code}).\n"
        f"{age} FACEIT keeps recordings for about {DEMO_RETENTION_DAYS} "
        "days, so the demo is not coming back and it is not tried again.\n"
        "If the demo is kept somewhere as a manual download, copy it into the "
        "archive's import directory."
    )


def _error_detail(response: Any) -> str | None:
    """The interface's own error text, shortened, or ``None``.

    The order of picking is the most precise first: FACEIT's JSON body uses
    the fields ``message`` and ``errors[].message``. If the body is not JSON,
    the beginning of the text is taken -- shortened, because an HTML error
    page is kilobytes and would bury the advice.

    ``None`` means "the interface did not say", not empty text: an invented
    explanation would be worse than its absence.
    """
    payload: Any = None
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - the body can be anything
        payload = None

    if isinstance(payload, Mapping):
        for key in ("message", "error", "detail"):
            text = _text(payload.get(key))
            if text is not None:
                return text[:MAX_ERROR_DETAIL_CHARS]
        errors = payload.get("errors")
        if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)):
            for entry in errors:
                if isinstance(entry, Mapping):
                    text = _text(entry.get("message"))
                    if text is not None:
                        return text[:MAX_ERROR_DETAIL_CHARS]

    raw = _text(getattr(response, "text", None))
    if raw is None:
        return None
    return " ".join(raw.split())[:MAX_ERROR_DETAIL_CHARS]


def _content_length(headers: Any) -> int | None:
    """``Content-Length`` as an integer, or ``None`` if the source did not
    say.

    ``None`` is a different thing from zero: an invented number would turn a
    whole download into a partial one or the other way round.
    """
    if not isinstance(headers, Mapping):
        return None
    raw = headers.get("Content-Length")
    if isinstance(raw, bool) or not isinstance(raw, (int, str)):
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return value if value >= 0 else None


class FaceitDemoSource:
    """FACEIT's demo source: a ``map_demo_id`` in, a byte stream out.

    Implements the :class:`~pappascout.adapters.protocols.DemoSource` port.

    The chain is four steps, and each has its own reason to be here and not in
    the stage::

        map_demo_id -> match_id + map_index   (the id's form)
        match_id    -> the match's raw response  (from the cache, no new call)
        instances   -> the recording's address   (round == map_index + 1)
        the address -> a signed link -> bytes    (the Downloads API)

    **The signed link does not leave this class.** It is born in
    :meth:`_sign`, travels into one ``GET`` call and disappears. It is not an
    attribute, it is not a return value, and it is in no error message: every
    :class:`ApiError` this class raises gets ``url=None``, and the transport's
    own exceptions -- whose message holds the address -- are cut out of the
    chain (``from None``) instead of being attached as the cause. The link is
    an authorisation, not an address.

    **The download is done at once.** A signed link's TTL is undocumented, so
    the link is neither stored nor downloaded later.

    Args:
        client: The :class:`FaceitClient` from which the match's details, the
            retry policy, the timeout and the transport come. **Not a new
            client**: every FACEIT call goes through one class (AD-8).
        downloads_token: The Downloads-scope token. A different id from the
            Data API key, and therefore a parameter of its own. Kept as a
            :class:`~pydantic.SecretStr`.
        downloads_base_url: The Downloads API's root. Must be ``https``,
            because the token travels in a header.
        chunk_bytes: How many bytes are read at a time.

    Raises:
        ~pappascout.errors.SettingsError: If the address is not ``https`` or
            the chunk size is outside its bounds.
    """

    def __init__(
        self,
        client: FaceitClient,
        downloads_token: str,
        *,
        downloads_base_url: str = FACEIT_DOWNLOADS_API_BASE,
        chunk_bytes: int = DEMO_CHUNK_BYTES,
        secrets_path: Path | None = None,
    ) -> None:
        self._client = client
        self._token = SecretStr(downloads_token)
        #: Where the token was read from. **For the error message only**:
        #: advice that names the file but not its location is not advice.
        self._secrets_path = secrets_path
        self.downloads_base_url = _check_base_url(downloads_base_url)
        self.chunk_bytes = _check_int(
            chunk_bytes, "chunk_bytes", 1, MAX_DEMO_CHUNK_BYTES
        )

    @classmethod
    def from_settings(
        cls, settings: Settings, client: FaceitClient, **kwargs: Any
    ) -> "FaceitDemoSource":
        """Build the source from the machine's own key file.

        **This is the only place that reads the Downloads token.** A missing
        token stops the run with advice that names the file's path and the
        line that is needed -- and that happens **before the first download**,
        not part-way through the series.

        Raises:
            ~pappascout.errors.SettingsError: If the token is missing or
                empty.
        """
        kwargs.setdefault(
            "secrets_path", settings.secrets_file or secrets_env_path()
        )
        return cls(client, settings.require_faceit_downloads_token(), **kwargs)

    def __repr__(self) -> str:
        """A representation **without the token** -- the same reason as
        :meth:`FaceitClient.__repr__`'s."""
        return f"FaceitDemoSource(downloads_base_url={self.downloads_base_url!r})"

    # -- The port ------------------------------------------------------------

    @_only_api_errors
    def get_demo(self, map_demo_id: str) -> DemoStream:
        """See the port's documentation."""
        match_id, map_index = split_map_demo_id(map_demo_id)
        payload = self._client.match_payload(match_id)

        status = _text(payload.get("status"))
        if status not in DEMO_READY_STATUSES:
            raise DemoUnavailable(
                f"Match {match_id}'s state is {status or 'unknown'}, not "
                "FINISHED, so the demo does not exist yet "
                f"({map_demo_id}).\n"
                "Run the command again once the match has been played."
            )

        resource_url = _demo_resource_url(payload, map_index, map_demo_id)
        # **Both calls turn a 404 into an absence.** Both the link exchange
        # and the download itself can answer 404, and in both it means the
        # same thing: the recording no longer exists. Without this translation
        # the stage would get an ``ApiError``, would mark the unit
        # ``download_failed`` and would advise running the command again --
        # and every new run would make the signing call first, that is, would
        # spend Downloads quota on a demo that is not coming back.
        with _gone_is_no_demo(payload, map_demo_id):
            signed = self._sign(resource_url, map_demo_id)
            return self._open(signed, map_demo_id)

    # -- Exchanging the download link ----------------------------------------

    def _sign(self, resource_url: str, map_demo_id: str) -> str:
        """Exchange the recording's address for a signed download link.

        Two-step because the CDN address is not an authorisation: it is the
        public name of a file whose download takes a signature. The exchange
        spends Downloads quota, so it is not done just in case but only once
        it is known that the demo is going to be written.

        **401 and 403 are raised from here as a type of their own.** They are
        not one demo's fault but the id's, and going on with the series would
        make as many doomed calls as there are maps in the sample.
        """
        url = f"{self.downloads_base_url}/demos/download"
        try:
            payload, _attempts, _status = self._client._retry(
                lambda: self._exchange(url, resource_url),
                what=f"demo {map_demo_id}'s download link",
                budget=self._client.new_budget(),
                # The Downloads API's own address is public and holds no
                # token -- it may be shown. A signed link may not, and this is
                # not one.
                url=url,
            )
        except ApiError as exc:
            if exc.status_code in DOWNLOADS_DENIED_STATUSES:
                raise DownloadsAccessDenied(
                    self._denied_message(exc.status_code),
                    advice=(
                        f"Check the application's status at "
                        f"{DOWNLOADS_STATUS_URL} -- the download succeeds only "
                        "once it has been approved. Running again does not "
                        "help before that."
                    ),
                ) from None
            if exc.status_code == DOWNLOADS_BAD_REQUEST:
                raise ApiError(
                    self._bad_request_message(
                        map_demo_id, getattr(exc, "detail", None)
                    ),
                    status_code=exc.status_code,
                    url=url,
                    advice=(
                        "Check FACEIT_DOWNLOADS_TOKEN in your machine's .env "
                        "file first. Running again does not help until the "
                        "cause has been fixed."
                    ),
                ) from None
            raise
        signed = None
        inner = payload.get("payload") if isinstance(payload, Mapping) else None
        if isinstance(inner, Mapping):
            signed = _text(inner.get("download_url"))
        if signed is None and isinstance(payload, Mapping):
            signed = _text(payload.get("download_url"))
        if signed is None:
            raise ApiError(
                f"FACEIT did not give a download link for demo {map_demo_id}.\n"
                "The response arrived but held no download_url field.\n"
                "Check that FACEIT_DOWNLOADS_TOKEN is a Downloads-scope token "
                "and not a Data API key.",
                url=url,
            )
        return signed

    def _bad_request_message(
        self, map_demo_id: str, detail: str | None
    ) -> str:
        """What a 400 can mean -- **both alternatives, no choice made**.

        Measured 2026-09-05 with a malformed token. Two causes end in the same
        code, and the response does not tell you which it is:

        ``The id is malformed``
            The commonest: a character slips while the ``.env`` file is edited
            by hand. Then **every** demo fails identically.
        ``resource_url is malformed``
            Rarer: FACEIT's own ``instances`` row holds an address the
            Downloads API does not accept. Then only **this** demo fails.

        Telling them apart is easy for the user and impossible for us: if the
        other demos fail with the same code too, the fault is in the id. That
        is why the message states the rule rather than guessing the answer --
        and why the series is stopped only on the strength of repetition
        (``stages.fetch.IDENTICAL_FAILURE_LIMIT``).

        The interface's own error text is shown if the response carried one:
        it is an observation, and our guess is not.
        """
        secrets = self._secrets_path or secrets_env_path()
        said = f"FACEIT said: {detail}\n" if detail else ""
        return (
            f"FACEIT did not accept the download-link request for demo "
            f"{map_demo_id} (HTTP {DOWNLOADS_BAD_REQUEST}).\n"
            f"{said}"
            "This code means two different things, and the response does not "
            "tell you which:\n"
            "  1. The Downloads id is malformed. Check the line "
            "FACEIT_DOWNLOADS_TOKEN\n"
            f"     in the file {secrets} -- one extra space or quotation mark "
            "is enough.\n"
            "  2. This map's recording address is one the Downloads API does "
            "not accept.\n"
            "\n"
            "You tell them apart from this run: if **all** the demos fail with "
            "the same code, the cause is 1; if only this one, the cause is 2."
        )

    def _denied_message(self, status_code: int | None) -> str:
        """What to do when the Downloads API refuses. **Waiting is part of
        it.**

        Measured 2026-09-05 in the first real run: a Data API key does not
        work against the Downloads API, and the authorisation is applied for
        separately. The product owner's application was in the queue at that
        point ("In queue -- waiting for review", submitted 2026-08-26).

        The generic message said of this "the fault is not fixed by waiting",
        and that is **misleading precisely here**: the claim is true of a
        retry in seconds and false of an application in weeks -- and waiting
        is exactly what fixes this one. Two different waits, and the message
        has to tell them apart.
        """
        secrets = self._secrets_path or secrets_env_path()
        return (
            f"FACEIT did not grant permission to download demos (HTTP "
            f"{status_code}).\n"
            "The Downloads API is a separate authorisation: a Data API key "
            "does not work for it; the permission is applied for on its "
            "own.\n"
            "\n"
            "Check in this order:\n"
            f"  1. Has your application been approved yet? The status is at\n"
            f"     {DOWNLOADS_STATUS_URL}\n"
            "  2. If there is no application, make one at\n"
            f"     {DOWNLOADS_APPLICATION_URL}\n"
            "  3. If the application has been approved, check that the "
            "file's\n"
            f"     {secrets}\n"
            "     line FACEIT_DOWNLOADS_TOKEN is a Downloads-scope token and "
            "not a Data API key.\n"
            "\n"
            "Running the command again does not help until the permission has "
            "been granted -- but after the approval it works as it is. Waiting "
            "therefore does help here, even though a new attempt a second "
            "later does not."
        )

    def _exchange(
        self, url: str, resource_url: str
    ) -> tuple[Mapping[str, Any], int | None]:
        """One ``POST`` to the Downloads API. **The token is here and only
        here.**"""
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            response = self._client.session.post(
                url,
                headers=headers,
                json={"resource_url": resource_url},
                timeout=self._client.timeout_seconds,
                # A redirect would send the token to an address this module
                # did not choose -- the same rule as the Data API's.
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise _Retryable(f"the connection failed ({type(exc).__name__})") from exc
        except Exception as exc:  # noqa: BLE001 - the transport is injectable
            raise _Permanent(
                f"the transport raised an exception ({type(exc).__name__})"
            ) from exc
        return self._as_json(response, "a download link")

    def _as_json(
        self, response: Any, what: str
    ) -> tuple[Mapping[str, Any], int | None]:
        """Check the status code and unpack the JSON. The same split of rules
        as the Data API's.

        **The interface's own text** is kept from a permanent error: it is an
        observation of what was wrong with the request, and often the only way
        to tell apart two causes that end in the same status code.
        """
        # ``detail=True``: the Downloads API states its own error text, and
        # that is an observation. 204/205 is not a branch of its own here and
        # is not added -- that would change this call site's behaviour.
        status = _checked_status(response, detail=True)
        try:
            payload = response.json()
        except ValueError as exc:
            raise _Permanent(
                f"the response was not JSON while fetching {what} "
                f"(status code {status})",
                status_code=status,
            ) from exc
        if not isinstance(payload, Mapping):
            raise _Permanent(
                f"the response was not an object but a {type(payload).__name__}",
                status_code=status,
            )
        return payload, status

    # -- The byte stream -----------------------------------------------------

    def _open(self, signed_url: str, map_demo_id: str) -> DemoStream:
        """Open the download and return the stream. **The address does not
        travel out.**"""
        response, _attempts, _status = self._client._retry(
            lambda: self._begin(signed_url),
            what=f"demo {map_demo_id}",
            budget=self._client.new_budget(),
            # The only place in the whole module where the address is left
            # out: here it is a signed link, that is, an authorisation to the
            # file.
            url=None,
        )
        return DemoStream(
            chunks=_stream_chunks(response, self.chunk_bytes, map_demo_id),
            content_length=_content_length(getattr(response, "headers", None)),
            on_close=getattr(response, "close", None),
        )

    def _begin(self, signed_url: str) -> tuple[Any, int | None]:
        """One ``GET`` to the signed link; the body is not read yet.

        ``allow_redirects=True`` unlike elsewhere: this call carries **no
        header that would be secret** -- the authorisation is in the address
        itself, and a CDN redirects downloads as a matter of routine. The same
        blocking as the Data API's would protect a header that is not there,
        and would break the download.
        """
        # **The exception is built here and raised outside the block.** The
        # message of ``requests``' own exception holds the whole signed
        # address. ``raise ... from exc`` would put it in ``__cause__``, and
        # ``from None`` alone is not enough: Python attaches the exception
        # being handled to ``__context__`` in any case, so the authorisation
        # would go on living as an attribute of the new error. Raised outside
        # the except block, no chain arises at all. The type says what guides
        # the user; the address would not.
        failure: Exception | None = None
        response: Any = None
        try:
            response = self._client.session.get(
                signed_url,
                stream=True,
                timeout=self._client.timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            failure = _Retryable(f"the connection failed ({type(exc).__name__})")
        except Exception as exc:  # noqa: BLE001 - the transport is injectable
            failure = _Permanent(
                f"the transport raised an exception ({type(exc).__name__})"
            )
        if failure is not None:
            raise failure

        # **A 404 ends up permanent and not ``_Retryable``, and that is the
        # rule.** A demo that is gone is a fact: FACEIT keeps recordings for
        # about 30 days, and waiting does not bring back what has been
        # deleted. A retry would spend Downloads quota for certain and for
        # nothing. The body is not parsed as JSON, so 204/205 is not a fault
        # here -- and the byte stream's own error text is not read, because
        # the body is the demo.
        status = _checked_status(response)
        return response, status


def _stream_chunks(
    response: Any, chunk_bytes: int, map_demo_id: str
) -> Iterator[bytes]:
    """Read the response's body in chunks and turn a transport fault into an
    ``ApiError``.

    A break part-way through the stream falls under the port's promise just as
    a failed opening does: the stage gets an ``ApiError`` from both and not
    ``requests``' own type. The new error is raised **outside** the except
    block for the same reason as in :meth:`FaceitDemoSource._begin`: the
    transport's message holds the signed address, and attached to the chain it
    would go on living as an attribute of the new exception.
    """
    failure: Exception | None = None
    try:
        for chunk in response.iter_content(chunk_size=chunk_bytes):
            if chunk:
                yield chunk
    except requests.RequestException as exc:
        failure = ApiError(
            f"The download of demo {map_demo_id} was cut short "
            f"({type(exc).__name__}).\n"
            "No partial file was left in the archive. This is a transient "
            "fault: run the command again."
        )
    if failure is not None:
        raise failure
