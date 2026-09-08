"""Pappascout's error hierarchy.

Every error of the tool's own inherits from :class:`PappascoutError`, so the
CLI can catch one type and show the user a message instead of letting a
traceback spill onto the screen. The stages turn unit-specific problems into
`status` fields (AD-9) and raise an exception only if not a single unit could
be processed or the settings are missing.

Every message says what the user has to do next.
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
    """The base class of Pappascout's errors.

    The message always names the next action.

    **The advice is in a field and not in the message alone (Story 3.4,
    2026-09-05).** Twice in a row a real run found the same pattern: the
    classification was right but the advice wrong, because the advice came
    from the **heading** the error was sorted under. A heading that says "aja
    komento uudelleen" assumes a transient fault, and every new failure class
    inherited that assumption silently: 403 (a missing authorisation) and 400
    (a malformed id) both got advice that helps neither of them.

    When the advice is in the error, a new failure class **cannot inherit the
    wrong advice**: it either brings its own or goes without, and the guard
    (``stages.fetch._result``) notices the latter.

    Args:
        message: An explanation of what happened.
        advice: One sentence about what the user has to do next. ``None``
            means "not known" -- not "run it again".
    """

    def __init__(self, message: str, *, advice: str | None = None) -> None:
        super().__init__(message)
        self.advice = advice


class ApiError(PappascoutError):
    """The FACEIT interface returned an error or did not answer (Story 3.1).

    **This is the network errors' own type**, the same family as
    :class:`ParseError` and :class:`AggregateError`: the caller tells "the
    interface did not answer" from "the recording did not parse" by the type
    and not by the message. The class had existed as an empty
    placeholder since Story 1.1 (ARCHITECTURE-SPINE, Consistency Conventions
    -> Virheet); Story 3.1 gives it content rather than adding a second name
    for the same thing beside it.

    Three fields, because the caller has to tell three different
    continuations apart **without reading the message**:

    ``status_code``
        The HTTP status code, or ``None`` if no response was obtained at all
        (a connection error, a timeout, a response that was not JSON).
        Story 3.4 decides from this whether a match's status is ``no_demo``
        (404) or ``download_failed`` (everything else) -- nothing can be
        decided from the content of the message, because the message is for a
        human.
    ``attempts``
        How many times the call was made. ``1`` means that a retry was not
        even attempted: a 4xx (except 429) is not put right by waiting.
    ``url``
        The address without the key. The key travels in the
        ``Authorization`` header and never in the address, so this one may be
        shown and stored.

    The message always says what was being fetched -- a status code on its own
    guides nobody anywhere.
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
    """No recording is available in the archive or from the download source."""


class ParseError(PappascoutError):
    """Parsing the recording failed."""


class SchemaError(PappascoutError):
    """The table does not match the shared schema contract (AD-2)."""


class AggregateError(PappascoutError):
    """The aggregation's sample check did not pass (Story 2.3).

    Raised when the sum of a distribution's ``n`` values does not match the
    sample's ``m``. That is not a formatting error but a sign that a round was
    lost on the way: either the join ``(map_demo_id, round_no)`` left a row
    out, or the distribution is missing its zero bucket. Either would produce
    a report that looks right but claims the wrong sample -- which is why the
    run stops.
    """


class LockError(PappascoutError):
    """The archive is locked by another run."""


class SettingsError(PappascoutError):
    """The settings or the keys are missing or invalid."""


class DownloadsAccessDenied(SettingsError):
    """FACEIT did not grant authorisation to download recordings (401/403, Story 3.4).

    **A type of its own, because it is the only fault that is not
    unit-specific.** AD-9 says that a unit's problem turns into a ``status``
    field and does not interrupt the run -- and that holds for every fault
    that concerns *one recording*: a deleted recording, a dropped connection,
    a full disk. A missing Downloads scope is not one of those. It concerns
    the **credential**, so every unit fails identically and not one of them
    can succeed.

    The difference matters and must not be lost: this is **not an exception**
    to the rule "one recording failing does not interrupt the run". It is not
    about one recording failing but about none of them being able to succeed
    -- and carrying on with the series would make 12 doomed signing calls, all
    of which consume quota with no chance of succeeding.

    It inherits from :class:`SettingsError`, because the fix is in the same
    place as with a missing key: the machine's own ``.env`` file -- or
    FACEIT's approval of the application the file's line requires.
    """
