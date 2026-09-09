"""``render`` -- ``report.json`` into Markdown. **It computes nothing.**

The package is the report's *presentation layer*: it chooses what is said
about :class:`~pappascout.domain.report.Report`'s numbers, and the Jinja2
template :data:`TEMPLATE_NAME` decides in what shape. Every number comes
straight from ``report.json`` -- nothing here is summed, averaged or derived
into a new number. If the report needs a number the model does not have, the
fix goes into the ``aggregate`` stage (Story 2.3), not here.

The division of work: **the code chooses what is said, the template how**
------------------------------------------------------------------------
:mod:`pappascout.render.view` builds a view model out of the report -- rows,
claims and their samples -- and the template sets them. The report's shape is
certain to change once a human reads the first version, and as a text file it
can be changed without touching the selection logic.

The template is part of the result, not of the environment
----------------------------------------------------------
:func:`template_digest` is a digest of the template's content, and it goes
into the ``render`` stage's parameter hash. Without it, editing the template
would change the report's content without anything showing in the manifest --
the same failure mode that
:func:`~pappascout.constants.weapon_classification_digest` prevents in
parsing.

**The parameter hash covers the template but not :mod:`pappascout.render.view`.**
Editing the view module changes the report just as surely as the template
does, and it does not show in the manifest at all: Story 2.12 rewrote nearly
every line of text in ``view.py``, and without the template's own new number
the manifests would have been byte for byte the same. The gap does not let a
stale report out -- ``render`` never skips a run on the strength of a
manifest, so every run sets the report again -- but it means that two
reports' manifests cannot tell whether they came from the same code. The gap
is recorded in the design's ``deferred-work.md`` -- which lives in the BMAD
output (``_bmad-output/implementation-artifacts/``) and not in this
repository, so there is no point looking for the file in the working tree.
Closing it is a story of its own, because ``view.py``'s digest would change
from a mere docstring fix as well and would force a re-render without the
report changing.

**Since Story 2.13 the hash also covers the settings the stage reads.**
``render`` now reads the ``[report]`` section (the pruning rules), and a
setting that does not show in the hash is exactly the Story 1.8 fault that
has been found three times in this project: an adjusted value would produce a
manifest that claims the result is the same. The section is in the hash
**whole** for the same reason ``[aggregate]`` is in ``aggregate`` -- a list
of the fields read would go stale in silence.

**The digest and the rendering read the same text.** Neither is cached:
the digest used to be in an ``lru_cache`` while Jinja's ``FileSystemLoader``
reloaded the template automatically, so a template edited during a run would
have produced a new report under the old digest -- precisely the state the
digest was added to prevent. The template is a few kilobytes and is read once
per run, so there is nothing for a cache to win.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from jinja2 import Environment, StrictUndefined, TemplateError

from pappascout.domain.models import ReportSettings
from pappascout.domain.report import Report
from pappascout.errors import PappascoutError
from pappascout.render.view import ReportView, build_view, round_list_demo_ids

__all__ = [
    "TEMPLATE_NAME",
    "template_path",
    "template_text",
    "template_digest",
    "render_report",
    "build_view",
    "round_list_demo_ids",
    "ReportView",
]

#: The report template's file name in this package's directory.
TEMPLATE_NAME = "report.md.j2"


def template_path() -> Path:
    """The report template's path.

    The template is read from the package's own directory and not from the
    archive: it is part of the program, not the user's data. A template in
    the archive would mean that two machines can have a different report
    shape on the same program version.
    """
    return Path(__file__).resolve().parent / TEMPLATE_NAME


def template_text() -> str:
    """The template's content as text, with the line endings normalised.

    The normalisation is done **here** and not in the digest function, so
    that the digest and the rendering see literally the same string:
    otherwise the same template would give a different digest depending on
    whether the working copy was checked out with CRLF or LF endings.
    """
    return template_path().read_text(encoding="utf-8").replace("\r\n", "\n")


def template_digest() -> str:
    """The sha256 of the template's content.

    It goes into the ``render`` stage's parameter hash, so that editing the
    template shows in the manifest.

    Returns:
        A 64-character hexadecimal digest of **the very text**
        :func:`render_report` sets.
    """
    return hashlib.sha256(template_text().encode("utf-8")).hexdigest()


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """A Jinja environment without a loader.

    The template is handed in with ``from_string`` out of
    :func:`template_text`, so the environment has no notion of a file of its
    own and therefore no cache of its own that could drift from the digest.

    ``StrictUndefined`` is essential: the default ``Undefined`` sets a
    missing field as an **empty string**, so a typo in the template would
    produce a report with one row missing in silence. That is exactly the
    failure mode that "nothing vanishes in silence" forbids.
    """
    return Environment(
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701 - Markdown, not HTML
    )


def render_report(
    report: Report,
    *,
    settings: ReportSettings,
    round_list_paths: Sequence[str] = (),
) -> str:
    """Format the report into Markdown.

    Args:
        report: The ``aggregate`` stage's result as it is.
        settings: The ``[report]`` section, that is the pruning rules (Story
            2.13). **Mandatory and not defaulted** -- see :func:`build_view`.
        round_list_paths: The round lists' paths for the round appendix. The
            stage resolves them out of ``archive.paths``; this layer does not
            see the archive.

    Returns:
        One Markdown document that ends in a newline.

    Raises:
        ~pappascout.errors.PappascoutError: If the template is broken or
            refers to a field the view does not have. Jinja's own
            ``TemplateError`` does not inherit from ``PappascoutError``, so
            without the wrapper the stage's documented error contract would
            not hold and the user would see an English traceback.
    """
    view = build_view(
        report, settings=settings, round_list_paths=round_list_paths
    )
    try:
        template = _environment().from_string(template_text())
        text = template.render(view=view)
    except TemplateError as exc:
        raise PappascoutError(
            f"Rendering the report template {template_path()} failed: {exc}\n"
            "This is a programming error in the report template, not in the "
            "user's data. Restore the template from version control."
        ) from exc
    return _tidy(text)


def _tidy(text: str) -> str:
    """Clean up the whitespace the template inevitably produces.

    Conditionals leave consecutive blank lines behind whenever a section is
    left out. The cleanup is here and not in the template, so that the
    template's conditions stay readable -- the price is that the document's
    final shape comes about in two places, and that is exactly why there is a
    golden test of it.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    cleaned: list[str] = []
    for line in lines:
        if not line.strip() and cleaned and not cleaned[-1].strip():
            continue
        cleaned.append(line.rstrip())
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return "\n".join(cleaned) + "\n"
