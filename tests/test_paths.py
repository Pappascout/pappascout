"""Tests for the archive paths (AD-7).

Every path stored in a manifest or an index is relative -- an absolute path
would break the archive on the other machine.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from pappascout.archive import paths
from pappascout.archive.atomic_write import atomic_write_bytes
from pappascout.archive.paths import (
    ARCHIVE_ROOT_ENV_VAR,
    DEMOS_ROOT_ENV_VAR,
    ArchivePaths,
    safe_component,
)
from pappascout.errors import PappascoutError

RELATIVE_FUNCS = [
    (paths.raw_faceit_dir, ()),
    (paths.teams_index, ()),
    (paths.matches_index, ()),
    (paths.selection, ("team-abc",)),
    (paths.next_opponent, ("team-abc",)),
    (paths.demo, ("1234-0",)),
    (paths.demo_meta, ("1234-0",)),
    (paths.parsed_dir, ("1234-0",)),
    (paths.parsed_manifest, ("1234-0",)),
    (paths.classified, ("team-abc", "1234-0")),
    (paths.classified_manifest, ("team-abc", "1234-0")),
    (paths.report_json, ("team-abc",)),
    (paths.report_manifest, ("team-abc",)),
    (paths.reports_dir, ("team-abc",)),
    (paths.report_markdown, ("team-abc", "2026-08-30T0307-abc.md")),
    (paths.render_manifest, ("team-abc", "2026-08-30T0307-abc.md")),
    (paths.import_dir, ()),
    (paths.logs_dir, ("desktop",)),
]


@pytest.mark.parametrize(
    "func,args", RELATIVE_FUNCS, ids=lambda v: getattr(v, "__name__", "")
)
def test_paths_are_relative_and_posix(func, args) -> None:
    result = func(*args)
    assert isinstance(result, PurePosixPath)
    text = str(result)
    assert not result.is_absolute()
    assert "\\" not in text
    assert ":" not in text


def test_archive_tree_matches_the_convention() -> None:
    assert str(paths.teams_index()) == "index/teams.json"
    assert str(paths.matches_index()) == "index/matches.json"
    assert str(paths.selection("t")) == "index/selections/t.json"
    assert str(paths.next_opponent("t")) == "index/next_opponent/t.json"
    assert str(paths.demo("1234-0")) == "demos/1234-0.dem.zst"
    assert str(paths.demo_meta("1234-0")) == "demos/1234-0.meta.json"
    assert str(paths.parsed_table("1234-0", "rounds")) == "parsed/1234-0/rounds.parquet"
    assert str(paths.classified("t", "1234-0")) == "classified/t/1234-0.parquet"
    assert str(paths.report_json("t")) == "aggregates/t/report.json"
    assert str(paths.reports_dir("t")) == "reports/t"
    assert (
        str(paths.report_markdown("t", "2026-08-30T0307-t.md"))
        == "reports/t/2026-08-30T0307-t.md"
    )
    assert (
        str(paths.render_manifest("t", "2026-08-30T0307-t.md"))
        == "reports/t/2026-08-30T0307-t.manifest.json"
    )
    assert str(paths.logs_dir("host")) == "logs/host"
    assert str(paths.LOCK_FILE) == ".lock"


def test_map_demo_id_is_match_id_and_zero_based_map_index() -> None:
    """map_demo_id = {match_id}-{map_index}, the map index is 0-based."""
    assert str(paths.demo("1-8ffb4c53-0")).endswith("1-8ffb4c53-0.dem.zst")


@pytest.mark.parametrize(
    "table",
    ["rounds", "ticks", "events", "lineups", "deaths", "callouts", "match"],
)
def test_parse_writes_exactly_seven_tables(table: str) -> None:
    assert table in paths.PARSED_TABLES
    assert str(paths.parsed_table("d", table)).endswith(f"{table}.parquet")


def test_the_parametrised_list_is_the_whole_contract() -> None:
    """The list must not fall behind the contract.

    The parametrised list is written by hand, and without this assertion a
    new table would be added to ``PARSED_TABLES`` but not here -- and its
    path would never be tested. That is exactly what happened to ``deaths``.
    """
    assert len(paths.PARSED_TABLES) == 7


def test_unknown_parsed_table_is_rejected() -> None:
    """The rejected name is one that can never become a real table.

    Not a plausible one: ``PARSED_TABLES`` already carries ``deaths``, so
    ``kills`` is exactly the sort of name a later story might add -- and on
    the day it did, this test would invert without a word.
    """
    with pytest.raises(ValueError) as exc:
        paths.parsed_table("d", "not-a-table")
    assert "not-a-table" in str(exc.value)


# --- Checking the ids ---------------------------------------------------------


#: The two sentences :func:`safe_component` can end with.
#:
#: **Pinned, because the advice is the whole value of the message.** An
#: exception type tells the reader that the id was refused; only these
#: sentences tell them *which* rule refused it, and the fix differs: a
#: separator or a space is a character-class problem, while a bare ``..``
#: satisfies the character class and is stopped for leaving the archive.
#: Delete either sentence and, without these assertions, every case below
#: stays green.
CHARACTER_RULE = (
    "Letters, digits and the characters _ . - are allowed "
    "(at most 120 characters)."
)
TRAVERSAL_RULE = "it would point outside the archive root."


def _expected_rule(value: str) -> str:
    """Which of the two sentences this value has to be refused with."""
    return TRAVERSAL_RULE if value in {".", ".."} else CHARACTER_RULE


@pytest.mark.parametrize(
    "unsafe",
    ["..", ".", "../..", "a/b", "a\\b", "", "C:", "x" * 121, "team key", "a:b"],
)
def test_unsafe_identifiers_are_rejected(unsafe: str) -> None:
    """An id must not escape the archive root, nor break the path.

    The message has to name the offending id and say which rule stopped it;
    see :data:`CHARACTER_RULE`.
    """
    with pytest.raises(PappascoutError) as exc:
        safe_component(unsafe, "team_key")

    message = str(exc.value)
    assert f"team_key={unsafe!r}" in message
    assert _expected_rule(unsafe) in message


@pytest.mark.parametrize("safe", ["team-abc", "1234-0", "1-8ffb4c53-0", "host_1", "a.b"])
def test_safe_identifiers_pass_through(safe: str) -> None:
    assert safe_component(safe, "team_key") == safe


def test_path_builders_reject_traversal() -> None:
    """The path builders check the id -- not just safe_component itself.

    Both rules are exercised, because these inputs do not all fail the same
    way: a separator never satisfies the character class, while ``..`` does
    and is stopped one check later. A builder that lost the second check
    would still reject the first three.
    """
    for builder, argument in (
        (paths.logs_dir, "../.."),
        (paths.selection, "../../secrets"),
        (paths.demo, "../../../etc/passwd"),
    ):
        with pytest.raises(PappascoutError) as exc:
            builder(argument)
        assert CHARACTER_RULE in str(exc.value)

    with pytest.raises(PappascoutError) as exc:
        paths.classified("ok-team", "..")
    assert TRAVERSAL_RULE in str(exc.value)


@pytest.mark.parametrize(
    "unsafe",
    ["../escape.md", "a/b.md", "a\\b.md", "", "..", "C:/elsewhere.md"],
)
def test_report_file_names_are_checked_too(unsafe: str) -> None:
    """The report file name comes from a stage, not from an id.

    It is the only path component not derived from ``team_key`` or
    ``map_demo_id``, so its check has to be proved separately -- otherwise a
    change to the timestamp format could escape the archive root without
    anything saying so.

    ``render_manifest`` strips the ``.md`` before checking, so it is refused
    by the same rule as ``report_markdown`` for every name here.
    """
    expected = _expected_rule(unsafe)

    with pytest.raises(PappascoutError) as exc:
        paths.report_markdown("ok-team", unsafe)
    assert expected in str(exc.value)

    with pytest.raises(PappascoutError) as exc:
        paths.render_manifest("ok-team", unsafe)
    assert expected in str(exc.value)


def test_the_manifest_name_is_derived_from_the_report_name() -> None:
    """A per-report manifest: two parallel runs do not write the same one."""
    first = paths.render_manifest("t", "2026-08-30T0307-t.md")
    second = paths.render_manifest("t", "2026-08-30T0307-t-02.md")
    assert first != second
    assert str(first).endswith(".manifest.json")
    assert str(second).endswith("-02.manifest.json")


# --- Prefixing the root -------------------------------------------------------


def test_resolve_and_relative_round_trip(tmp_path: Path) -> None:
    archive = ArchivePaths.from_settings(tmp_path)
    relative_path = paths.parsed_table("1234-0", "rounds")
    absolute_path = archive.resolve(relative_path)
    assert absolute_path == tmp_path / "parsed" / "1234-0" / "rounds.parquet"

    absolute_path.parent.mkdir(parents=True)
    absolute_path.write_bytes(b"x")
    assert archive.relative(absolute_path) == relative_path


def test_resolve_rejects_absolute_paths(tmp_path: Path) -> None:
    """An absolute path in a manifest is always an error, never silently accepted."""
    archive = ArchivePaths.from_settings(tmp_path)
    for unsafe in ("C:/elsewhere/result.parquet", "/etc/passwd"):
        with pytest.raises(PappascoutError) as exc:
            archive.resolve(unsafe)
        assert "relative to the archive root" in str(exc.value)


# --- Expanding and overriding the root ----------------------------------------


def test_from_settings_expands_environment_variables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same versioned settings line works on both machines."""
    monkeypatch.setenv("TEST_HOME", str(tmp_path))
    archive = ArchivePaths.from_settings("%TEST_HOME%/archive")
    assert archive.root == tmp_path / "archive"


def test_from_settings_expands_tilde(monkeypatch: pytest.MonkeyPatch) -> None:
    archive = ArchivePaths.from_settings("~/pappascout-archive")
    assert archive.root == Path.home() / "pappascout-archive"
    assert "~" not in str(archive.root)


def test_env_var_overrides_the_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PAPPASCOUT_ARCHIVE_ROOT redirects the run to another archive."""
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "other"))
    archive = ArchivePaths.from_settings("C:/from-the-settings/archive")
    assert archive.root == tmp_path / "other"


def test_unset_variable_is_an_error_not_a_literal_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpanded ``%NAME%`` fails the run instead of becoming a folder.

    ``os.path.expandvars`` **leaves ``%NAME%`` as it is** when the variable
    does not exist -- it raises no error and does not return an empty string.
    Without this check the run created a directory named literally
    ``%USERPROFILE%``, wrote the whole archive into it, and looked like it
    had succeeded. The user has two machines, so the archive would break
    silently.

    This really happened: a directory named ``%USERPROFILE%`` appeared in
    the repository, with the full path tree under it.
    """
    monkeypatch.delenv("VARIABLE_THAT_IS_NOT_SET", raising=False)
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings("%VARIABLE_THAT_IS_NOT_SET%/archive")

    message = str(exc.value)
    # Names the missing variable and says where the value came from.
    assert "VARIABLE_THAT_IS_NOT_SET" in message
    assert "archive_root" in message
    # And says what to do about it. Without this the advice could be deleted
    # and the reader would be left with a name and no next step.
    assert "Set VARIABLE_THAT_IS_NOT_SET, or write the path out in full" in message


def test_unset_variable_in_the_env_override_names_the_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same check covers a path given through the environment variable.

    The message names a different source: the user has to know which of the
    two to fix when ``PAPPASCOUT_ARCHIVE_ROOT`` overrides the setting.
    """
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, "%SECOND_MISSING%/archive")
    monkeypatch.delenv("SECOND_MISSING", raising=False)

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings("C:/does-not-matter")

    message = str(exc.value)
    assert "SECOND_MISSING" in message
    assert ARCHIVE_ROOT_ENV_VAR in message


def test_braced_variable_is_checked_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``${NAME}`` is as unambiguous a placeholder as ``%NAME%``."""
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)
    monkeypatch.delenv("THIRD_MISSING", raising=False)

    with pytest.raises(PappascoutError, match="THIRD_MISSING"):
        ArchivePaths.from_settings("${THIRD_MISSING}/archive")


def test_a_set_variable_still_expands_without_complaint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not reject paths that do expand correctly.

    Without this counterpart the previous test would also pass against an
    implementation that fails on every percent sign.
    """
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)
    monkeypatch.setenv("IS_SET", str(tmp_path))
    archive = ArchivePaths.from_settings("%IS_SET%/archive")
    assert archive.root == tmp_path / "archive"


def test_real_settings_needs_the_variable_and_resolves_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shipped settings resolve through the variable, or not at all.

    Story 3.10 moved the real path out of the versioned file into
    ``PAPPASCOUT_ARCHIVE_ROOT``, so the claim is two-sided: with the variable
    set the path resolves and keeps no unexpanded ``%NAME%`` part, and without
    it the versioned placeholder stops the run by naming the variable rather
    than creating a directory literally called ``%PAPPASCOUT_ARCHIVE_ROOT%``.

    **The absolute-path assertion belongs to the relative-value test below,
    not here.** Asserting ``is_absolute()`` on a root this test itself set to
    an absolute value proves nothing about the loader.
    """
    from conftest import REAL_SETTINGS
    import tomllib

    data = tomllib.loads(REAL_SETTINGS.read_text(encoding="utf-8"))
    raw = data["project"]["archive_root"]

    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "archive"))
    archive = ArchivePaths.from_settings(raw)
    assert archive.root == tmp_path / "archive"
    assert "%" not in str(archive.root)

    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR)
    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings(raw)
    message = str(exc.value)
    assert ARCHIVE_ROOT_ENV_VAR in message
    assert "settings.toml" in message


def test_a_relative_override_is_an_error_not_a_directory_next_to_the_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative archive root stops the run and names the variable.

    **The failure path this closes.** The versioned line reads
    ``archive_root = '%PAPPASCOUT_ARCHIVE_ROOT%'``, which reads like a folder
    *name* rather than a whole path, so setting the variable to
    ``pappascout-archive`` is the natural mistake. Nothing else catches it:
    :func:`_check_expanded` is silent because there is no ``%NAME%`` left, and
    every stage creates what is missing. The run would resolve the archive
    against the working directory, fill a second empty archive there, and
    report success -- and inside the repository ``.gitignore`` anchors only
    ``/archive/``, so a tree under any other name would not even be ignored.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, "pappascout-archive")

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings("%" + ARCHIVE_ROOT_ENV_VAR + "%")

    message = str(exc.value)
    assert ARCHIVE_ROOT_ENV_VAR in message
    assert "absolute path" in message
    # No archive was created in the working directory. (The one directory
    # already there is the isolated home the autouse fixture makes, not a
    # product of this run.)
    assert not (tmp_path / "pappascout-archive").exists()


def test_a_relative_setting_is_an_error_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard covers the file, and the message names the file.

    Two sources, two messages: the reader has to know whether to fix the
    variable or ``settings.toml``.
    """
    monkeypatch.delenv(ARCHIVE_ROOT_ENV_VAR, raising=False)

    with pytest.raises(PappascoutError) as exc:
        ArchivePaths.from_settings("archives/pappascout")

    message = str(exc.value)
    assert "absolute path" in message
    assert "settings.toml" in message


def test_an_absolute_root_still_passes_the_relative_guard(tmp_path: Path) -> None:
    """The pair to the two above: the guard must not reject valid roots.

    Without this, both relative tests would also pass an implementation that
    rejects every path.
    """
    archive = ArchivePaths.from_settings(tmp_path / "archive")
    assert archive.root == tmp_path / "archive"


# --- Status information -------------------------------------------------------


def test_missing_archive_reports_zero_size(tmp_path: Path) -> None:
    archive = ArchivePaths.from_settings(tmp_path / "does-not-exist")
    assert archive.exists() is False
    assert archive.total_size_bytes() == 0


def test_total_size_counts_files(tmp_path: Path) -> None:
    archive = ArchivePaths.from_settings(tmp_path)
    atomic_write_bytes(archive.resolve(paths.teams_index()), b"12345")
    atomic_write_bytes(archive.resolve(paths.matches_index()), b"123")
    assert archive.exists() is True
    assert archive.total_size_bytes() == 8


def test_find_demo_accepts_both_compressions(tmp_path: Path) -> None:
    """FACEIT serves .dem.zst; a manually imported one may be .dem.gz."""
    archive = ArchivePaths.from_settings(tmp_path)
    assert archive.find_demo("1234-0") is None

    gz = archive.demo("1234-0", ".dem.gz")
    gz.parent.mkdir(parents=True, exist_ok=True)
    gz.write_bytes(b"x")
    assert archive.find_demo("1234-0") == gz

    zst = archive.demo("1234-0")
    zst.write_bytes(b"x")
    assert archive.find_demo("1234-0") == zst  # zst takes precedence


# -- The demo directory: environment variables and guards (Story 3.4) --------


def test_an_unset_variable_in_demos_root_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard as on the archive root, and for the same reason.

    ``os.path.expandvars`` **leaves ``%NAME%`` as it is** when the variable
    does not exist -- it raises no error and does not return an empty string.
    Without the check the run would create a directory named literally
    ``%DEMOS%`` and write 2.3 GB of demos into it. This has already happened
    once in this repository, on the archive root (see
    ``test_an_unset_variable_in_the_path_is_refused``).
    """
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)

    with pytest.raises(PappascoutError) as excinfo:
        ArchivePaths.from_settings(r"C:\archive", r"%DOES_NOT_EXIST%\demos")

    message = str(excinfo.value)
    assert "DOES_NOT_EXIST" in message
    # The message names the **demo directory** and not the archive root: the
    # wrong name would send the reader to fix the wrong settings.toml line.
    assert "The demo directory path" in message
    assert "demos_root" in message


def test_a_set_variable_in_demos_root_is_expanded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DEMOS_DRIVE", str(tmp_path))

    archive = ArchivePaths.from_settings(r"C:\archive", r"%DEMOS_DRIVE%\demos")

    assert archive.demos_root == tmp_path / "demos"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_demos_root_means_the_archive_directory(blank: str) -> None:
    """A blank line in the settings file is "not set", not an empty path.

    Without this branch ``Path("")`` would point at the working directory,
    and the demos would go wherever the command happened to be run from.
    """
    archive = ArchivePaths.from_settings(r"C:\archive", blank)

    assert archive.demos_root is None
    assert archive.demos_dir() == archive.archive_demos_dir()


def test_overriding_the_archive_root_takes_the_demos_with_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Isolation is not isolation if it covers only some of the paths.

    ``PAPPASCOUT_ARCHIVE_ROOT`` exists precisely so a run can be pointed at
    a test archive without touching the production files. If ``demos_root``
    stayed in force, a ``fetch`` run against a test archive would write and
    read the **production** demo directory -- silently, and because of
    idempotence in such a way that the run would appear to succeed without
    downloading anything.
    """
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "test-archive"))

    archive = ArchivePaths.from_settings(r"C:\production", r"C:\production-demos")

    assert archive.root == tmp_path / "test-archive"
    assert archive.demos_root is None
    assert archive.demos_dir() == tmp_path / "test-archive" / "demos"


def test_the_demos_root_variable_wins_over_both(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Saying it explicitly wins: "demos here" even in a redirected run."""
    monkeypatch.setenv(ARCHIVE_ROOT_ENV_VAR, str(tmp_path / "test-archive"))
    monkeypatch.setenv(DEMOS_ROOT_ENV_VAR, str(tmp_path / "separate"))

    archive = ArchivePaths.from_settings(r"C:\production", r"C:\production-demos")

    assert archive.demos_root == tmp_path / "separate"


def test_the_demos_root_variable_works_without_an_archive_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(DEMOS_ROOT_ENV_VAR, str(tmp_path / "separate"))

    archive = ArchivePaths.from_settings(r"C:\production", r"C:\production-demos")

    assert archive.demos_root == tmp_path / "separate"


def test_an_unset_variable_in_the_demos_root_variable_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same guard for the variable's value too, and it names the source."""
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    monkeypatch.setenv(DEMOS_ROOT_ENV_VAR, r"%DOES_NOT_EXIST%\demos")

    with pytest.raises(PappascoutError) as excinfo:
        ArchivePaths.from_settings(r"C:\archive")

    assert DEMOS_ROOT_ENV_VAR in str(excinfo.value)
