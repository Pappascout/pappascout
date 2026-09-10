"""Shared helpers for the tests.

The tests need no demos, no network **and not this machine**: the tables are
built by hand, and both the home directory and the archive root are
redirected to a temporary directory. Without that the tests would read the
real ``%USERPROFILE%\\.pappascout\\.env`` file and walk through a
synchronised folder holding hundreds of megabytes of archive -- and the
result would depend on the machine.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

import polars as pl
import pytest

from pappascout.archive.paths import (
    ARCHIVE_ROOT_ENV_VAR,
    DEMOS_ROOT_ENV_VAR,
    ArchivePaths,
)
from pappascout.domain.schemas import Schema
from pappascout.errors import PappascoutError

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SETTINGS = REPO_ROOT / "settings.toml"


#: Environment variable naming the machine-local denylist file.
FORBIDDEN_NAMES_ENV_VAR = "PAPPASCOUT_FORBIDDEN_NAMES"

#: Machine-local list of names this public repository must not carry.
#:
#: **Resolved at import time, and deliberately outside the repository.** Same
#: shape as :func:`require_demo`: the machine knows, the repository does not.
#: A denylist committed here would itself be the leak it guards against -- the
#: story's own check is a ``git grep`` for those names over the working tree,
#: and an in-repo list would put every one of them back.
#:
#: Format, one entry per line, ``#`` comments and blank lines ignored::
#:
#:     Some Employer Oy          # must not appear at all
#:     SomeSyncProduct = 57      # ceiling: may appear at most 57 times
#:
#: The ceiling makes the guard a ratchet. Names that are already gone get 0, so
#: writing one back anywhere fails immediately. Names still present in prose
#: awaiting the per-package translation work get their measured count, so they
#: cannot spread while that work is pending.
FORBIDDEN_NAMES_FILE = Path(
    os.environ.get(FORBIDDEN_NAMES_ENV_VAR)
    or Path.home() / ".pappascout" / "forbidden-names.txt"
)


def _forbidden_names() -> dict[str, int] | None:
    """Read the denylist, or ``None`` when this machine has no list.

    Returns:
        Name mapped to the number of occurrences tolerated across the
        repository, or ``None`` if the file does not exist -- CI and any other
        machine then skip the guard rather than fail it.
    """
    try:
        text = FORBIDDEN_NAMES_FILE.read_text(encoding="utf-8")
    except OSError:
        return None
    names: dict[str, int] = {}
    for line in text.splitlines():
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        name, _, ceiling = entry.partition("=")
        names[name.strip()] = int(ceiling) if ceiling.strip() else 0
    return names


#: Denylist for :mod:`test_public_repo`, or ``None`` on a machine without one.
FORBIDDEN_NAMES: dict[str, int] | None = _forbidden_names()


#: Was ``PAPPASCOUT_ARCHIVE_ROOT`` set when this module was imported?
#:
#: Read here and not later because ``_isolate_from_machine`` deletes the
#: variable for every test. It is the one fact that separates "this machine has
#: no archive" from "this machine has one and we failed to find it", and
#: :mod:`test_isolation` asserts on it: without that assertion the 222 marked
#: tests could all turn into silent skips and pytest would still exit zero.
_MACHINE_ROOT_SET = ARCHIVE_ROOT_ENV_VAR in os.environ


def _real_archive_root() -> Path | None:
    """The real archive root, resolved **at import time**.

    It has to be computed before ``_isolate_from_machine`` redirects
    ``USERPROFILE`` and deletes ``PAPPASCOUT_ARCHIVE_ROOT``; otherwise tests
    that need the machine's archive would find nothing even on the machine
    that has it.

    **The same resolver as production.** Story 3.10 moved the real path out of
    the versioned file into ``PAPPASCOUT_ARCHIVE_ROOT``, and the versioned
    value is a placeholder. Reading only ``settings.toml`` would send the
    ``demo``- and ``archive``-marked tests looking inside the placeholder --
    that is, they would skip silently on the very machine that has the
    archive. So the path comes from :meth:`ArchivePaths.from_settings`, the
    same order the commands use.

    **A broken settings file is not a missing variable.** Only the placeholder
    case returns ``None``. Unreadable or malformed TOML, or a missing
    ``[project].archive_root``, is a real defect in the repository: it would
    turn 222 marked tests into silent skips everywhere, including CI, so it is
    raised instead.

    Returns:
        The directory, or ``None`` when ``PAPPASCOUT_ARCHIVE_ROOT`` is unset on
        this machine. ``None`` is deliberately different from a path that
        happens not to exist: an invented placeholder path would make the skip
        message name a directory that never existed.

    Raises:
        Exception: Whatever reading or parsing ``settings.toml`` raises, and
            ``PappascoutError`` for any resolution failure other than the
            unset variable -- a relative override, for instance.
    """
    data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
    raw = str(data["project"]["archive_root"])
    try:
        return ArchivePaths.from_settings(raw).root
    except PappascoutError:
        if _MACHINE_ROOT_SET:
            # The variable is set, so this is a bad value rather than a bare
            # machine -- swallowing it would hide a real misconfiguration
            # behind 222 skips.
            raise
        return None


def _real_import_dir() -> Path | None:
    """The directory of the real demos, under the archive root."""
    root = _real_archive_root()
    return None if root is None else root / "import"


#: The real archive root, or ``None`` if it could not be resolved.
ARCHIVE_ROOT: Path | None = _real_archive_root()

#: The directory of the real demos, or ``None`` if it could not be resolved.
#: ``PAPPASCOUT_TEST_DEMOS`` overrides it.
_FROM_ENV = os.environ.get("PAPPASCOUT_TEST_DEMOS")
DEMO_DIR: Path | None = Path(_FROM_ENV) if _FROM_ENV else _real_import_dir()

#: Test data (``_bmad-output/implementation-artifacts/testiaineisto.md``).
ANCIENT_DEM = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1.dem"
ANCIENT_ZST = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1.dem.zst"
NUKE_ZST = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1.dem.zst"

#: Expected results from the real demos (FACEIT Data API, fetched 2026-08-28).
ANCIENT_ROUNDS = 21
NUKE_ROUNDS = 28

#: The demos of Pappaliiga's last season and the rounds played in them.
#:
#: **Irreplaceable data.** FACEIT no longer offers these (retention ~30 days)
#: and there is no re-run. They are the only league data against which the
#: handling of a match restart has been verified, and they are not in the
#: repository but in the archive's ``import/`` directory. :func:`require_demo`
#: skips the test cleanly when they are absent -- that is, on another machine
#: the whole regression set evaporates silently. That is why the size and the
#: digest are recorded in :data:`LEAGUE_DEMO_FILES`: a wrong or truncated copy
#: is then distinguishable from a missing one.
#:
#: All four have a match restart after the knife round: a ``round_freeze_end``
#: of its own with no ``round_end``, and the demo's own round numbering
#: carries on over it by one. It is played, but it is not a round. The
#: measurement of the pattern is kept in the BMAD project's file
#: ``_bmad-output/implementation-artifacts/vika-kierrosnumerointi.md``, which
#: is **outside this repository**; the essential content is repeated in
#: :mod:`pappascout.adapters.demo_parser`'s module documentation, so that the
#: test does not lean on a file that is not here.
#:
#: **The oracle for the round count is not a product of our own code.** It is
#: the number of the demo's own ``round_end`` events minus the knife round,
#: read straight from demoparser2's event stream, past our own round
#: numbering. The same derivation is run as a test
#: (``test_league_round_count_matches_the_demos_own_event_stream``), so the
#: number cannot drift in step with the numbering.
#:
#: **Do not use per-half wins as the match result.** The sides swap at half
#: time, so the T/CT split of ``round_end``'s ``winner`` field is not the
#: team's result but the side's result.
LEAGUE_DEMOS: tuple[tuple[str, int], ...] = (
    ("Ancient_vs_kaljukostaja.dem", 20),
    ("Anubis_vs_ryhmarama.dem", 22),
    ("Nuke_vs_imuaijat.dem", 23),
    ("inferno_vs_ryhmarama.dem", 20),
)

#: The league demos' size in bytes and SHA-256, measured 2026-08-29.
#:
#: These are not a backup but an **id**: if the file in the archive does not
#: match, the tests' numbers do not apply to that file. A missing demo is
#: skipped cleanly, but a wrong demo must not pass silently.
LEAGUE_DEMO_FILES: dict[str, tuple[int, str]] = {
    "Ancient_vs_kaljukostaja.dem": (
        379_946_762,
        "286e3f79fb192386e1fa9fea1503b91fa17ee24efde9aa11d35e63348fc8ecff",
    ),
    "Anubis_vs_ryhmarama.dem": (
        437_437_483,
        "4e1525551c9be68ee2ea66a5dce60b75a38aca19e97be89f0e37f58d8ccf336f",
    ),
    "Nuke_vs_imuaijat.dem": (
        444_162_824,
        "7290f40bd0ff7721ca6f3c989d357b5d8d47396f1ccba6916a1f7bfda3616e9f",
    ),
    "inferno_vs_ryhmarama.dem": (
        453_514_645,
        "a33e8bbf1054dc6b17b030a9b86f015b0e9138444a3d42e70fea1446e94021b1",
    ),
}


#: Story 2.10's fault demo: a player whose controller is there but whose
#: pawn is not.
#:
#: ``anubis_vs_RCAVE_VETERANS`` **did not parse at all** before Story 2.10:
#: one player on one round had no character on the map, and the sample-point
#: reader's alive guard brought the whole demo down. It cost a third of the
#: data on a new opponent.
#:
#: The demo is here so that the measurement of the fix is **reproducible from
#: the repository** and not merely recorded in a docstring. It is the only
#: demo in the archive's ``import/`` that has pawnless rows, and therefore the
#: only one that can report that the skip has stopped working.
#:
#: It is not in :data:`LEAGUE_DEMOS` nor in ``ALL_DEMOS``: it is a
#: Europe 5v5 Queue match and not a league match, and its numbers belong to
#: neither the restart nor the calibration regression sets.
PAWNLESS_DEMO = "anubis_vs_RCAVE_VETERANS.dem.zst"

#: The fault demo's size and SHA-256, measured 2026-08-31. The same rule as
#: for :data:`LEAGUE_DEMO_FILES`: a missing demo is skipped, a wrong one must
#: not pass silently.
PAWNLESS_DEMO_FILE: tuple[int, str] = (
    204_420_133,
    "e9dcf35da6836f6d81d30d14029c62b8fc7661861ba685c0b89a18511035b7b5",
)

#: The fault demo's measured numbers (2026-08-31, with production's
#: ``[parse]`` settings).
#:
#: ``PAWNLESS_DEMO_ROUNDS``
#:     Rounds played. The result was 13-9, that is, an MR12 match.
#: ``PAWNLESS_DEMO_ROWS``
#:     Pawnless player rows skipped: **one player** (``egerrrrr``,
#:     76561199635619622) on **one round** (round_no 19). Five from the sample
#:     points' ticks and ten from the utility throw ticks; the same tick is
#:     counted once.
#: ``PAWNLESS_DEMO_POINTS``
#:     Sample points missed entirely. Zero: one missing player out of ten
#:     leaves a point short of players, not empty.
PAWNLESS_DEMO_ROUNDS = 22
PAWNLESS_DEMO_ROWS = 15
PAWNLESS_DEMO_POINTS = 0


def require_demo(name: str) -> Path:
    """Return the real demo's path, or skip the test with a clear reason.

    The demos are 100-230 MB and do not belong in the repository, so on
    another machine or in CI the test has to skip -- not fail. The skip
    message always says where it looked, so that a missing demo is
    distinguishable from a wrong path.
    """
    if DEMO_DIR is None:
        pytest.skip(
            f"The demo directory could not be resolved, because the archive "
            f"root is not known. Set the environment variable {ARCHIVE_ROOT_ENV_VAR} "
            "to the archive's full path -- the demos are read from its import "
            "directory. The demo directory alone can be redirected with the "
            "variable PAPPASCOUT_TEST_DEMOS."
        )
    path = DEMO_DIR / name
    if not path.is_file():
        pytest.skip(f"There is no real demo on this machine: {path}")
    return path


#: The tables an archive-dependent test needs from every demo.
#:
#: **The whole list, not only the one the test reads directly.** The
#: calibration tests build the report through ``stages.aggregate``, and the
#: stage reads all of these; a partially parsed archive would fail the test
#: instead of skipping it, and the error would not say that the fault is in
#: the machine's data.
REQUIRED_PARSED_TABLES = (
    "rounds",
    "ticks",
    "events",
    "lineups",
    "deaths",
    "match",
    "callouts",
)


def require_parsed(*map_demo_ids: str) -> Path:
    """Return the archive root, or skip the test if the demos are not parsed.

    **A different requirement from :func:`require_demo`'s.** The calibration
    numbers are computed from the ``parsed/`` and ``classified/`` tables, not
    from the demo files: those are megabytes where the demos are hundreds of
    megabytes, and they are exactly the data the thresholds were measured
    against. The test **writes nothing into the archive** -- it reads the
    tables and builds the report in memory.

    The skip message names the missing table and the command that produces
    it, so that a missing parse is distinguishable from a missing archive and
    from a missing classification.
    """
    if ARCHIVE_ROOT is None:  # pragma: no cover - depends on the machine
        pytest.skip(
            f"The archive root is not known: the environment variable "
            f"{ARCHIVE_ROOT_ENV_VAR} is not set on this machine. The "
            "versioned settings.toml does not carry the path, because the "
            "repository is public."
        )
    if not ARCHIVE_ROOT.is_dir():  # pragma: no cover - depends on the machine
        pytest.skip(f"There is no archive on this machine: {ARCHIVE_ROOT}")
    for map_demo_id in map_demo_ids:
        for name in REQUIRED_PARSED_TABLES:
            table = ARCHIVE_ROOT / "parsed" / map_demo_id / f"{name}.parquet"
            if not table.is_file():  # pragma: no cover - depends on the machine
                pytest.skip(
                    f"Demo {map_demo_id} has no {name}.parquet table in this "
                    f"archive ({table}). Run: uv run pappascout parse "
                    f"{map_demo_id}"
                )
        # Classification is a stage of its own: a parsed demo without a
        # classification is no use to the aggregation, and the skip message
        # names a different command.
        classified = list(
            (ARCHIVE_ROOT / "classified").glob(f"*/{map_demo_id}.parquet")
        )
        if not classified:  # pragma: no cover - depends on the machine
            pytest.skip(
                f"Demo {map_demo_id} has not been classified into this "
                f"archive. Run: uv run pappascout classify {map_demo_id} "
                "--all-teams"
            )
    return ARCHIVE_ROOT

#: A point cloud whose sites separate from each other: A is around cell 0 and
#: B around cell 100. A cell is ``(area, cell_x, cell_y, cell_z)``.
#:
#: **Three cells per site**, because the radius of a one-cell area is 0 -- and
#: the separation guard divides the distance between the sites by the size of
#: the sites themselves, so with one-cell sites it silences the demo (and it
#: is right to: a two-cell cloud says nothing about the map's site structure).
#:
#: The other areas are on the same axis, so that every group can be read by
#: eye: ``House`` 10 (A), ``SideEntrance`` 90 and ``Ramp`` 85 (B), ``Middle``
#: 50 (equally far, so shared middle). The spawns are **deliberately in a
#: group** -- ``CTSpawn`` on A's side and ``TSpawn`` on B's, as on the real
#: maps -- because that is exactly what makes excluding the spawns a
#: definition and not a tidy-up.
#:
#: One copy instead of three: the rule (``test_sampling``), the aggregation
#: (``test_aggregate``) and the stage (``test_stage_aggregate``) measure the
#: same geometry, and three copies could drift apart.
SITE_CLOUD: tuple[tuple[str, int, int, int], ...] = (
    ("BombsiteA", 0, 0, 0),
    ("BombsiteA", 2, 0, 0),
    ("BombsiteA", -2, 0, 0),
    ("BombsiteB", 100, 0, 0),
    ("BombsiteB", 102, 0, 0),
    ("BombsiteB", 98, 0, 0),
    ("House", 10, 0, 0),
    ("SideEntrance", 90, 0, 0),
    ("Ramp", 85, 0, 0),
    ("Middle", 50, 0, 0),
    ("CTSpawn", 5, 0, 0),
    ("TSpawn", 95, 0, 0),
)

#: The same cloud, but with the sites on top of each other: the difference
#: between the centres is 2 cells and the sites' own size 20 + 20, that is, a
#: ratio of 0.05. The ratio measured on Nuke is 0.47-0.54 and the threshold
#: 2.0, so this is the same situation taken to an extreme.
OVERLAPPING_SITE_CLOUD: tuple[tuple[str, int, int, int], ...] = (
    ("BombsiteA", 0, 0, 0),
    ("BombsiteA", 20, 0, 0),
    ("BombsiteA", -20, 0, 0),
    ("BombsiteB", 2, 0, 0),
    ("BombsiteB", 22, 0, 0),
    ("BombsiteB", -18, 0, 0),
    ("House", 10, 0, 0),
)

#: Environment variables that must not leak from the machine into the tests.
LEAKY_ENV_VARS = (
    "FACEIT_API_KEY",
    "FACEIT_DOWNLOADS_TOKEN",
    "PAPPASCOUT_SETTINGS",
    "PAPPASCOUT_DEMOS_ROOT",
    ARCHIVE_ROOT_ENV_VAR,
)


@pytest.fixture(autouse=True)
def _isolate_from_machine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from the machine's own files and credentials."""
    for name in LEAKY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)

    # Path.home() reads USERPROFILE on Windows and HOME elsewhere. Both are
    # redirected to an empty directory, so that secrets_env_path() does not
    # hit the real credentials file.
    home_dir = tmp_path / "koti"
    home_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)


def even_split(total: int, players: int) -> list[int]:
    """Split a team total among the players as evenly as possible, descending.

    **An assumption, not an observation.** The rounds table records the
    per-player distribution, but hand-built rows and the older truth tables
    record only the team total. An even split keeps a row internally
    consistent (the distribution sums to ``money_buy_end``) without every test
    writing five numbers.

    One place, because this is exactly the assumption Story 1.10 calls an
    assumption: two copies would drift apart and each would look like
    independent evidence. A test that examines the distribution itself gives
    the distribution itself.
    """
    base, extra = divmod(int(total), players)
    return [base + 1] * extra + [base] * (players - extra)


def empty_frame(schema: Schema) -> pl.DataFrame:
    """Build an empty DataFrame matching the given contract exactly."""
    return pl.DataFrame(schema=dict(schema))


@pytest.fixture
def env_file(tmp_path: Path):
    """A factory for temporary .env files."""

    def _make(name: str = ".env", **values: str) -> Path:
        path = tmp_path / name
        path.write_text(
            "".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8"
        )
        return path

    return _make


def settings_text(archive_root: Path | str, **replacements: str) -> str:
    """The real ``settings.toml``'s content with the archive path replaced.

    The tests use the same numbers as production -- otherwise they would
    prove nothing about the real settings file -- but never the real archive.
    """
    text = REAL_SETTINGS.read_text(encoding="utf-8")
    line = next(r for r in text.splitlines() if r.startswith("archive_root"))
    text = text.replace(line, f"archive_root = '{archive_root}'")
    for old, new in replacements.items():
        assert old in text, f"nothing to replace: {old}"
        text = text.replace(old, new)
    return text


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    """A copy of the real ``settings.toml``, the archive redirected to tmp_path.

    The demos go into the archive's own ``demos/``, because that is what a
    run without ``PAPPASCOUT_DEMOS_ROOT`` does too. The other mode is
    :func:`local_demos_root`, and **both have to be run through a command**:
    the mode a fixture does not cover never passes through the CLI at all,
    and its breaking would show only in the stage tests.
    """
    archive_dir = tmp_path / "arkisto"
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    return target


#: Where the downloaded demos are when ``PAPPASCOUT_DEMOS_ROOT`` is set.
LOCAL_DEMOS_DIRNAME = "paikalliset-demot"


@pytest.fixture
def local_demos_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The other demo directory mode: the demos **outside** the archive.

    The archive is the default, because it follows from one machine to the
    other and the sync client can free a parsed demo's space without deleting
    the file. Moving them out is a supported mode all the same -- and a
    supported mode that no command test runs is a mode whose breaking the
    user is the first to notice.

    It is set through the environment and not through ``settings.toml``,
    because that is the only way there is: the setting was removed
    2026-09-10 (it had been dead since Story 3.10 made the archive override
    normal), and ``PAPPASCOUT_DEMOS_ROOT`` is what the user has. A fixture
    that wrote a settings line would prove a path production never takes.

    Use it beside :func:`settings_file`, which the command still needs.
    """
    local = tmp_path / LOCAL_DEMOS_DIRNAME
    monkeypatch.setenv(DEMOS_ROOT_ENV_VAR, str(local))
    return local


@pytest.fixture
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Change the working directory to an empty temporary directory."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def has_temp_leftovers(directory: Path) -> bool:
    """Whether an atomic write has left temporary files in the directory."""
    return any(p.name for p in directory.rglob("*.tmp-*"))
