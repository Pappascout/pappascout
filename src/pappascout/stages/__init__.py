"""The pipeline's stages: from file to file (AD-1).

Every stage is a function ``run(settings, archive, unit, *ports) -> StageResult``
whose input and result are files in the archive. A stage does not call another
stage and does not write into another stage's result area; **the order is
decided by the user one command at a time**, and no module chaining the stages
together exists.

A stage is given **only its own settings section** (AD-3). It therefore cannot
read the other sections, which is why for instance changing a
``[thresholds]`` value cannot affect the ``parse`` stage's result or its
parameter hash.

This package is also the layer through which ``cli`` touches the archive: the
dependency arrow is ``cli -> stages -> {domain, adapters, archive}``, so the
command line does not import the ``archive`` or the ``adapters`` package
itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from pappascout.archive.paths import ArchivePaths
from pappascout.constants import UnitStatus
from pappascout.domain.models import ProjectSettings

__all__ = ["StageResult", "archive_paths"]


@dataclass(frozen=True)
class StageResult:
    """The result of running one stage over one unit.

    A stage prints nothing itself: it returns this, and ``cli`` decides what
    is shown to the user. The same stage therefore works behind a web shell
    as well.

    Attributes:
        stage: The stage's name, for example ``"parse"``.
        unit: The unit processed, in the ``parse`` stage ``map_demo_id``.
        status: The unit's status (AD-9).
        skipped: Whether the stage was skipped on the strength of a matching
            manifest.
        outputs: The files written, as paths relative to the inside of the
            archive. In a skipped run these are the previous run's files.
        manifest_path: The manifest's path inside the archive.
        reason: An explanation for a status other than ``ok``, for a skip
            **or for an incomplete result**. The third use is that of the
            stages at the head of the pipeline (``discover``, ``select``):
            for those ``status`` is always ``ok`` -- the lookup succeeded --
            but the result can still be empty or incomplete, and "0 rows"
            without a word about why would leave the user guessing. A new
            ``UnitStatus`` value is not an option: it would extend
            ``CLASSIFIED``'s polars enum, that is, change the schema contract
            of the parquet files already in the archive.

            **One string, even when there are many notes.** A stage that can
            have several of them also carries them separately in
            ``stats["notes"]``, so that the command prints each on its own
            line and none of them disappears behind another.
        duration_s: The running time in seconds.
        stats: Per-stage numbers for the user's output, for example the
            number of rounds. Free-form, because every stage says something
            different.
    """

    stage: str
    unit: str
    status: UnitStatus
    skipped: bool
    outputs: tuple[PurePosixPath, ...] = ()
    manifest_path: PurePosixPath | None = None
    reason: str | None = None
    duration_s: float = 0.0
    stats: dict[str, Any] = field(default_factory=dict)


def archive_paths(project: ProjectSettings) -> ArchivePaths:
    """Build the archive paths from the ``[project]`` section.

    This is ``cli``'s only way into the archive: the command line does not
    import the ``archive`` package itself, it asks this layer for the paths.

    ``demos_root`` travels along too; it is the only path outside the
    archive. It is given here rather than separately in every stage, so that
    the demos' location is **one decision in one place**: a stage that read
    the setting itself would before long decide differently from its
    neighbour.
    """
    return ArchivePaths.from_settings(project.archive_root, project.demos_root)
