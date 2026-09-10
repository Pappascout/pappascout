"""Pappascout -- CS2 opponent scouting for Pappaliiga.

The pipeline reads the recordings of FACEIT matches, parses them into tables,
classifies the rounds and produces a Finnish Markdown report. The stages are
independent file-to-result functions, and a stage never calls another: the
order is decided by ``stages.pipeline``, which the ``scout`` command runs
from end to end. A stage's input is the file the previous stage wrote, and if
it is not there, the stage says so and names the command that writes it --
which is why running one stage at a time by hand is still a supported way to
work.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _version

__all__ = ["__version__"]

try:
    __version__ = _version("pappascout")
except PackageNotFoundError:  # pragma: no cover - only without an installation
    __version__ = "0.0.0+unknown"
