"""``render`` -- the pipeline's last stage: ``report.json`` into Markdown.

The stage reads **one file** (``aggregates/<team_key>/report.json``) and
writes **one file** (``reports/<team_key>/<timestamp>-<slug>.md``). It does not
read the demo, the tables or the classification, and it computes nothing:
every number that appears in the report is already there in ``report.json``.
If the report needs a number that is not there, the fix is made in the
``aggregate`` stage (Story 2.3) -- not here.

A new file on every run, never an overwrite
-------------------------------------------
``report.json`` is always overwritten, the report never. The name carries a
timestamp to the minute, and runs within the same minute get the zero-padded
suffix ``-02``, ``-03``. The name is **reserved atomically**
(``O_CREAT | O_EXCL``) before the write: a bare ``exists()`` check would leave
a gap in which two concurrent runs would pick the same name and the later one
would destroy the earlier. The archive lives in a synchronised folder two
machines share, so the gap is not theoretical.

**The reservation costs something too, and that is worth saying out loud.** A
reserved file exists before its content does, so (a) a failed write would
leave a zero-byte ``.md`` that looks like a report -- which is why the
reservation is cancelled when an error occurs -- and (b) a sync client may get
to open the reserved file, in which case ``os.replace`` onto the same name
fails with ``PermissionError``. The latter is rare but possible, and it is
turned into a plain error rather than a stack trace. Neither is a reason to
give up the reservation: a silent overwrite would be worse than a visible
error.

The stage is not skipped
------------------------
The other stages skip themselves when the manifest matches. ``render`` does
not: the user runs the ``report`` command when he wants a report, and a skip
would leave him without the file he asked for. The manifest is written all the
same, and it is **per report**: a shared manifest would stand up badly to
exactly the concurrency the name is reserved against.

The schema version is checked before the model
----------------------------------------------
A ``report.json`` written by an old version can validate against the current
model field by field and still mean something different. The version is
therefore read from the raw JSON **before** the pydantic validation:
otherwise the user would get a field-level validation error instead of being
told to run ``aggregate`` again.

The manifest and the parameter hash
-----------------------------------
The input is the team's ``aggregate`` manifest with its id. The parameter hash
is computed **from the report template's content and from the stage's own
settings section**: editing the template changes the report's content, and so
does adjusting the pruning rule. Without either of them the manifest would
claim that two different reports are the same result.

The settings section entered the hash in Story 2.13, in which the stage got
its first settings of its own (``[report]``, the pruning rules). Before that
the hash was the template's digest alone, because the stage read no setting at
all -- the thresholds come from ``report.json``. **A setting cannot be added
to a stage that does not notice it changing**: that is the Story 1.8 defect,
which has been found three times in this project, and it is why the hash was
fixed in the same story that added the settings.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pappascout import __version__
from pappascout.archive.atomic_write import atomic_write_text
from pappascout.archive.manifest import (
    Manifest,
    ManifestInput,
    compute_params_hash,
    tool_versions,
)
from pappascout.archive.paths import (
    MAX_REPORTS_PER_MINUTE,
    REPORT_TIMESTAMP_FORMAT,
    ArchivePaths,
    classified_round_list,
    render_manifest,
    report_json,
    report_manifest,
    report_markdown,
    report_name,
    safe_component,
)
from pappascout.domain.aggregate import team_slug
from pappascout.domain.models import ReportSettings
from pappascout.domain.report import REPORT_SCHEMA_VERSION, Report
from pappascout.errors import PappascoutError
from pappascout.render import render_report, round_list_demo_ids, template_digest
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "TOOLS",
    "run",
    "team_keys",
    "resolve_team",
    "read_report",
    "reserve_path",
    "round_list_paths",
]

STAGE = "render"

#: Jinja2 changes the report's shape between versions (the empty-state rules,
#: for instance), so its version belongs in the manifest -- unlike in
#: ``aggregate``, where no library affects the result.
TOOLS: tuple[str, ...] = ("jinja2",)


def _tools() -> dict[str, str]:
    """The tool versions for the manifest."""
    return tool_versions(*TOOLS)


def run(
    settings: ReportSettings,
    archive: ArchivePaths,
    team: str | None,
    *,
    now: datetime | None = None,
) -> StageResult:
    """Write one Markdown report out of the team's ``report.json``.

    Args:
        settings: The ``[report]`` section, that is the pruning rules (Story
            2.13). The stage is given **only its own part** (AD-3), and the
            same section goes both into the rendering and into the parameter
            hash -- a setting that is read but is not in the hash would claim
            in the manifest that two different reports are the same.
        archive: The archive's paths.
        team: The team's id, or an unambiguous prefix of it. ``None`` produces
            an error that lists the aggregated teams.
        now: The timestamp for the file name, in **local time**. Can be given
            for the sake of the tests; the clock by default.

    Returns:
        A :class:`~pappascout.stages.StageResult` whose ``outputs`` holds the
        report written and whose ``stats`` holds its numbers.

    Raises:
        ~pappascout.errors.PappascoutError: If the team is not recognised, if
            ``report.json`` is missing or is of a different schema version, if
            no free file name is found, or if the write fails.
    """
    started = time.perf_counter()
    team_key = resolve_team(archive, team)

    json_rel = report_json(team_key)
    report = read_report(archive.resolve(json_rel), team_key)
    markdown = render_report(
        report,
        settings=settings,
        round_list_paths=round_list_paths(archive, report),
    )

    stamp = (now or datetime.now()).strftime(REPORT_TIMESTAMP_FORMAT)
    slug = team_slug(report.team.slug or team_key)
    path, name = reserve_path(archive, team_key, stamp, slug)
    markdown_rel = report_markdown(team_key, name)

    try:
        atomic_write_text(path, markdown)
    except OSError as exc:
        # The reservation is an empty file that is already in the directory.
        # If it stays there, it looks like a report, takes up an ordinal and
        # stands out from nothing -- it is not even an atomic write's
        # temporary file, so a check looking for leftovers does not find it.
        with contextlib.suppress(OSError):
            path.unlink()
        raise PappascoutError(
            f"The report could not be written to {path}: {exc}\n"
            "The reservation was cancelled, so no empty report was left in "
            "the directory. Check the disk space, and that no sync client is "
            "holding the file open, then run the command again."
        ) from exc

    manifest_rel = render_manifest(team_key, name)
    Manifest.new(
        result_id=str(markdown_rel),
        stage=STAGE,
        params_hash=_params_hash(settings),
        inputs=_inputs(archive, team_key),
        tool_versions=_tools(),
        status="ok",
        outputs=(str(markdown_rel),),
    ).write(archive.resolve(manifest_rel))

    return StageResult(
        stage=STAGE,
        unit=team_key,
        status="ok",
        skipped=False,
        outputs=(markdown_rel,),
        manifest_path=manifest_rel,
        duration_s=time.perf_counter() - started,
        stats=_stats(report, markdown),
    )


# -- Choosing the team -----------------------------------------------------------


def team_keys(archive: ArchivePaths) -> list[str]:
    """The teams that have an aggregated ``report.json``.

    The listing is read from the ``aggregates/`` directory and not from
    ``classified/``: ``render``'s input is the aggregate, and a team that is
    classified but not aggregated would offer itself to be chosen only to fail
    immediately afterwards.
    """
    root = archive.resolve(PurePosixPath("aggregates"))
    if not root.is_dir():
        return []
    return sorted(
        directory.name
        for directory in root.iterdir()
        if directory.is_dir() and (directory / "report.json").is_file()
    )


def resolve_team(archive: ArchivePaths, team: str | None) -> str:
    """Read ``--team`` as an aggregated team.

    Accepts both the full id and an unambiguous prefix of it -- a 16-character
    digest is uncomfortable to type by hand.

    **An empty string is not a prefix.** Every id begins with the empty
    string, so ``--team ""`` would match all of them and silently pick the
    only one -- that is, do exactly what requiring ``--team`` is meant to
    prevent. The same goes for whitespace alone.

    Raises:
        PappascoutError: If the id is missing, is empty, matches nothing or
            matches more than one. The message always lists the alternatives,
            so the next command can be copied straight out of it.
    """
    available = team_keys(archive)
    if not available:
        raise PappascoutError(
            "The archive holds no aggregated team, so there is nothing to "
            "report on.\n"
            "Run first: uv run pappascout aggregate --team <id>"
        )
    if team is None or not team.strip():
        problem = (
            "Say with the --team option which team's report is written."
            if team is None
            else "The team id is empty; an empty prefix would match all of them."
        )
        raise PappascoutError(f"{problem}\n{_team_listing(available)}")

    query = team.strip().lower()
    matches = [key for key in available if key.lower() == query]
    if not matches:
        matches = [key for key in available if key.lower().startswith(query)]
    if len(matches) == 1:
        return safe_component(matches[0], "team_key")

    problem = (
        f"Team id {team!r} matches more than one team."
        if matches
        else f"Team id {team!r} matches no team at all."
    )
    raise PappascoutError(f"{problem}\n{_team_listing(available)}")


def _team_listing(available: list[str]) -> str:
    rows = "\n".join(f"    {key}" for key in available)
    return (
        "The archive's aggregated teams are:\n"
        + rows
        + "\nGive the id in full or a prefix of it, for example:\n"
        + f"    --team {available[0][:8]}"
    )


# -- Reading the input -----------------------------------------------------------


def read_report(path: Path, team_key: str) -> Report:
    """Read and check ``report.json``.

    There are four checks, and each produces advice of its own: a missing
    file, the wrong schema version, broken content and **the wrong team**. The
    first three are not the same error -- in the first the aggregate has not
    been run, in the second it was run by an old version, in the third the
    file is corrupt.

    The fourth is the subtlest: ``aggregate`` writes ``team.key`` the same as
    the directory's name, so a difference means the file has been moved or
    edited by hand. Without the check the report would be named after the
    directory while the round-list appendix's paths and the statistics pointed
    at another team.

    Raises:
        PappascoutError: In all four cases; the message says what to do next.
    """
    if not path.is_file():
        raise PappascoutError(
            f"No aggregate for team {team_key} was found at {path}.\n"
            f"Run first: uv run pappascout aggregate --team {team_key}"
        )
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PappascoutError(
            f"File {path} could not be read as JSON: {exc}\n"
            f"Run the aggregate again: uv run pappascout aggregate --team "
            f"{team_key} --pakota"
        ) from exc

    version = raw.get("schema_version") if isinstance(raw, dict) else None
    if version != REPORT_SCHEMA_VERSION:
        raise PappascoutError(
            f"The report model's schema version does not match: {path} is of "
            f"version {version!r}, but this program knows version "
            f"{REPORT_SCHEMA_VERSION!r}.\n"
            "No report is written, because an old structure can look valid "
            "and still mean something different.\n"
            f"Run the aggregate again: uv run pappascout aggregate --team "
            f"{team_key} --pakota"
        )

    try:
        report = Report.model_validate(raw)
    except (ValueError, PappascoutError) as exc:
        raise PappascoutError(
            f"File {path} does not match the report model: {exc}\n"
            f"Run the aggregate again: uv run pappascout aggregate --team "
            f"{team_key} --pakota"
        ) from exc

    if report.team.key != team_key:
        raise PappascoutError(
            f"File {path} is in directory {team_key}, but its content is "
            f"about team {report.team.key!r}.\n"
            "No report is written, because it would be named after the "
            "directory but would tell of another team.\n"
            f"Run the aggregate again: uv run pappascout aggregate --team "
            f"{report.team.key} --pakota"
        )
    return report


def round_list_paths(archive: ArchivePaths, report: Report) -> list[str]:
    """The round lists' **absolute** paths for the round-list appendix.

    The paths are built here and not in the view, for two reasons. The
    archive's directory structure is ``archive.paths``'s business, and it must
    not be written out by hand in a second place; and the report is attached
    in Discord, where the reader has no way at all of knowing where the
    archive's root points -- a relative path would be useless to him.
    """
    return [
        str(archive.resolve(classified_round_list(report.team.key, demo)))
        for demo in round_list_demo_ids(report)
    ]


# -- Reserving the file name -----------------------------------------------------


def reserve_path(
    archive: ArchivePaths, team_key: str, stamp: str, slug: str
) -> tuple[Path, str]:
    """Reserve a free report name atomically.

    The name is created as an empty file with the ``O_CREAT | O_EXCL`` flags,
    so that the reservation succeeds for exactly one run. Only after that is
    the content written over it atomically. Without the reservation two
    concurrent runs could pick the same name between the ``exists()`` check
    and the write.

    Only ``FileExistsError`` means "the name is taken"; everything else -- an
    unwritable directory, a full disk, a sync client's lock -- is an error
    that has to be told plainly and not let through as a raw stack trace.

    Returns:
        ``(absolute path, file name)``.

    Raises:
        PappascoutError: If the directory could not be created, if the
            reservation failed for a reason other than the name being taken,
            or if no free name was found in
            :data:`~pappascout.archive.paths.MAX_REPORTS_PER_MINUTE`
            attempts.
    """
    directory = archive.reports_dir(team_key)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PappascoutError(
            f"The report directory {directory} could not be created: {exc}\n"
            "Check the archive path and the write permissions."
        ) from exc

    for ordinal in range(1, MAX_REPORTS_PER_MINUTE + 1):
        name = report_name(stamp, slug, ordinal)
        path = archive.report_markdown(team_key, name)
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        except OSError as exc:
            raise PappascoutError(
                f"The report file {path} could not be reserved: {exc}\n"
                "Check the archive path and the write permissions."
            ) from exc
        os.close(handle)
        return path, name

    raise PappascoutError(
        f"Directory {directory} already holds {MAX_REPORTS_PER_MINUTE} reports "
        f"with the timestamp {stamp}. Wait a minute or move the old reports "
        "somewhere safe -- an old report is never overwritten."
    )


# -- The manifest ----------------------------------------------------------------


def _inputs(archive: ArchivePaths, team_key: str) -> list[ManifestInput]:
    """The input: the team's ``aggregate`` manifest.

    A missing manifest does not fail the run. ``report.json`` exists and has
    been read, so the report can be written; only the traceability stays
    incomplete, and that is a smaller harm than the user not getting the
    report he asked for at all. An input without a manifest is marked with an
    empty digest, which is what tells it apart from a known one.
    """
    manifest = Manifest.read_if_exists(archive.resolve(report_manifest(team_key)))
    if manifest is None:
        return [
            ManifestInput(
                result_id=str(PurePosixPath("aggregates") / team_key), sha256=""
            )
        ]
    return [ManifestInput(result_id=manifest.result_id, sha256=manifest.fingerprint())]


def _params_hash(settings: ReportSettings) -> str:
    """The parameter hash from the report template and the stage's own section.

    The thresholds, the time windows and the samplings come from
    ``report.json``, so they are not here. The stage's own settings are these:
    the ``[report]`` section (the pruning rules, Story 2.13) decides which rows
    are written into the report, so adjusting it produces a different report
    from the same ``report.json``.

    The section goes into the hash **whole** (``model_dump``), not field by
    field: a list of the fields read would need maintaining and would go stale
    quietly at exactly the moment a sixth rule is added to the section. The
    same choice as in ``aggregate`` with the ``[aggregate]`` section.

    **The hash does not cover the program's own side.**
    :mod:`pappascout.render.view` picks every row and every wording, and
    changing it shows up in this hash not at all -- two reports with identical
    manifests are therefore no proof that they came from the same code. A
    stale report still cannot get out, because this stage is never skipped on
    the strength of the manifest (see :func:`run`); the shortcoming is recorded
    in the design's ``deferred-work.md``, which lives in the BMAD output
    (``_bmad-output/implementation-artifacts/``) and not in this repository.
    The settings are a different matter
    from that shortcoming, and that is why they were fixed at once: a code
    change shows up in version control, but an adjusted settings file shows up
    nowhere if it does not show up in the manifest.
    """
    return compute_params_hash(
        {
            "render": {"template_sha256": template_digest()},
            "report": settings.model_dump(mode="json"),
        }
    )


# -- The numbers for the output --------------------------------------------------


def _stats(report: Report, markdown: str) -> dict[str, Any]:
    """The numbers ``cli`` shows the user."""
    return {
        "team_key": report.team.key,
        "team_name_known": report.team.display_name != report.team.key,
        "demos": report.sample.demos,
        "rounds": report.sample.rounds,
        "maps": [entry.map_name for entry in report.maps],
        "missing_demos": len(report.missing_demos),
        "unclassified": report.unclassified_rounds,
        "lines": markdown.count("\n"),
        "characters": len(markdown),
        "pappascout": __version__,
    }
