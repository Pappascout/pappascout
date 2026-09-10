"""The CLI's tests: ``info`` and ``parse``.

Three requirements are locked down in these tests:

* a credential's **state** is shown, its **value** never is,
* the user never sees a raw traceback -- every error comes out as one line
  and an exit code, and
* the ``parse`` command's output reports the number of rounds, overtime, the
  skipped rounds and the run time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import settings_text
from pappascout import __version__
from pappascout.cli import (
    EXIT_KNOWN_ERROR,
    EXIT_UNEXPECTED_ERROR,
    _render_info,
    app,
    main,
)
from pappascout.stages.fetch import size_fi
from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings

FAKE_KEY = "kokeiluavain-1234567890"
FAKE_TOKEN = "kokeilutoken-abcdefghij"

runner = CliRunner()


def test_version_is_importable() -> None:
    assert __version__
    assert __version__ != "0.0.0+unknown"


def test_version_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert __version__ in result.output


def test_help_lists_info() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "info" in result.output


# --- info: the content -------------------------------------------------------


def test_info_shows_settings_archive_and_key_status(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_API_KEY=FAKE_KEY, FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = load_settings(settings_file, env_files=(env,))
    output_text = _render_info(settings)

    # The settings
    assert "PotkukelkkaPeek" in output_text
    assert "de_mirage" in output_text
    assert "MR12" in output_text
    assert "12500" in output_text
    assert "4000" in output_text
    assert str(settings_file) in output_text

    # The credentials: the state yes, the value no
    assert "FACEIT_API_KEY" in output_text
    # The status word is matched on its own line, not anywhere in the output:
    # ``set`` is a substring of every path that names ``settings``, so a
    # substring check here would pass without the status being printed at all.
    assert _key_status(output_text, "FACEIT_API_KEY") == "set"
    assert FAKE_KEY not in output_text
    assert FAKE_TOKEN not in output_text


def _key_status(output_text: str, name: str) -> str:
    """The status word ``info`` printed for one credential."""
    line = next(
        row for row in output_text.splitlines() if row.strip().startswith(name)
    )
    return line.rsplit(" ", 1)[-1]


def test_info_reports_missing_key_without_crashing(
    settings_file: Path, env_file
) -> None:
    env = env_file(FACEIT_DOWNLOADS_TOKEN=FAKE_TOKEN)
    settings = load_settings(settings_file, env_files=(env,))
    output_text = _render_info(settings)
    keys_section = _section(output_text, "Credentials")
    assert "FACEIT_API_KEY" in keys_section
    # On its own line for the same reason as above: the temporary directory
    # in the path beside it is named after this test, which contains
    # ``missing``.
    assert _key_status(keys_section, "FACEIT_API_KEY") == "missing"
    assert FAKE_TOKEN not in output_text


def test_info_names_every_section(settings_file: Path) -> None:
    """The three sections and the settings the user checks first are all there.

    The name of this test used to say the output is in Finnish. AD-11 moved
    the console into English on 2026-09-07, so the claim would now be false;
    what the assertion really pins is that no section quietly disappears.
    """
    settings = load_settings(settings_file, env_files=())
    output_text = _render_info(settings)
    for word in ("Settings", "Own team", "Map pool", "Archive", "Credentials"):
        assert word in output_text


def _section(output_text: str, heading: str) -> str:
    """Pick one section's rows, so that an assertion cannot hit another one."""
    lines = output_text.splitlines()
    start = lines.index(heading) + 1
    end = start
    while end < len(lines) and lines[end].startswith("  "):
        end += 1
    return "\n".join(lines[start:end])


# --- info: the archive row ---------------------------------------------------


def test_info_reports_missing_archive_precisely(
    tmp_path: Path, settings_file: Path
) -> None:
    """A missing archive shows in the archive section, and no directory is made."""
    settings = load_settings(settings_file, env_files=())
    archive_section = _section(_render_info(settings), "Archive")

    missing_dir = tmp_path / "arkisto"
    assert str(missing_dir) in archive_section
    assert "Status" in archive_section
    # The whole phrase, not the word: the temporary directory beside it is
    # named after this test, and that name contains ``missing`` too.
    assert "missing -- the directory is created" in archive_section
    assert "found" not in archive_section
    assert not missing_dir.exists()


def test_info_reports_existing_archive(tmp_path: Path) -> None:
    archive_dir = tmp_path / "arkisto"
    (archive_dir / "index").mkdir(parents=True)
    (archive_dir / "index" / "teams.json").write_bytes(b"12345")

    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    settings = load_settings(target, env_files=())

    section = _section(_render_info(settings), "Archive")
    assert "found" in section
    assert "missing" not in section
    # The size is not computed without --size.
    assert "not computed" in section
    # ``size_fi``'s unit for a small archive; it stays Finnish as number
    # formatting, so the needle stays Finnish too.
    assert "tavua" not in section


def test_info_computes_size_only_when_asked(tmp_path: Path) -> None:
    """NFR-1: info is a fast status check, so the size is optional."""
    archive_dir = tmp_path / "arkisto"
    (archive_dir / "index").mkdir(parents=True)
    (archive_dir / "index" / "teams.json").write_bytes(b"12345")

    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    settings = load_settings(target, env_files=())

    section = _section(_render_info(settings, show_size=True), "Archive")
    assert "5 tavua" in section
    assert "not computed" not in section


def test_size_flag_runs_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_dir = tmp_path / "arkisto"
    archive_dir.mkdir()
    (archive_dir / "x.json").write_bytes(b"1234")
    target = tmp_path / "settings.toml"
    target.write_text(settings_text(archive_dir), encoding="utf-8")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(target))

    result = runner.invoke(app, ["info", "--size"])
    assert result.exit_code == 0, result.output
    assert "4 tavua" in result.output


# --- info: the whole pipeline through --------------------------------------


def test_info_command_runs_end_to_end(
    settings_file: Path, env_file, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = env_file(FACEIT_API_KEY=FAKE_KEY)
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    # cli bound the name at import time, so the patch targets the cli module.
    monkeypatch.setattr("pappascout.cli.secrets_env_path", lambda: env)
    monkeypatch.setattr("pappascout.domain.models.secrets_env_path", lambda: env)

    result = runner.invoke(app, ["info"])
    assert result.exit_code == 0, result.output
    assert "PotkukelkkaPeek" in result.output
    assert _key_status(result.output, "FACEIT_API_KEY") == "set"
    assert FAKE_KEY not in result.output


def test_secrets_path_shown_comes_from_cli_module(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without a .env file, info shows the cli's own default path."""
    fake_path = tmp_path / "vale" / ".env"
    monkeypatch.setattr("pappascout.cli.secrets_env_path", lambda: fake_path)
    settings = load_settings(settings_file, env_files=())
    assert settings.secrets_file is None
    assert str(fake_path) in _render_info(settings)


# --- main(): handling the errors (NFR-1) -------------------------------------


def test_main_turns_known_error_into_one_line_and_exit_code_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A missing settings file -> exit code 1, one line, no traceback.

    The name used to say the line is in Finnish. That stopped being true when
    AD-11 moved the console into English; what the assertions pin is the
    heading, the exit code and the absence of a traceback.
    """
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(tmp_path / "ei-ole.toml"))
    monkeypatch.setattr(
        "pappascout.domain.models._repo_root", lambda: tmp_path / "ei-repoa"
    )
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_KNOWN_ERROR
    err_text = capsys.readouterr().err
    assert err_text.startswith("Error:")
    assert "Traceback" not in err_text
    assert "settings.toml" in err_text


def test_main_never_shows_a_traceback_for_a_bug(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """A program fault -> exit code 2 and a short line."""
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    def boom():
        raise RuntimeError("an unexpected breakage")

    monkeypatch.setattr("pappascout.cli.load_settings", lambda *a, **k: boom())
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == EXIT_UNEXPECTED_ERROR
    err_text = capsys.readouterr().err
    assert err_text.startswith("Unexpected error:")
    assert "Traceback" not in err_text


def test_main_exits_zero_on_success(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    monkeypatch.setattr("sys.argv", ["pappascout", "info"])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert "PotkukelkkaPeek" in capsys.readouterr().out


# --- Formatting a byte count (Story 3.7, item 7) -----------------------------


@pytest.mark.parametrize(
    "num_bytes,expected",
    [
        (0, "0 tavua"),
        (1, "1 tavua"),
        (1023, "1023 tavua"),
        (1024, "1,0 kt"),
        (1536, "1,5 kt"),
        (1024**2, "1,0 Mt"),
        (1024**3, "1,0 Gt"),
        (1024**4, "1,0 Tt"),
        (1024**5, "1,0 Pt"),
        (5 * 1024**5, "5,0 Pt"),
    ],
)
def test_size_fi_covers_everything_the_cli_formatter_covered(
    num_bytes: int, expected: str
) -> None:
    """``size_fi`` formats everything ``cli._human_size`` formatted.

    The same table ``_human_size`` had, the ``Pt`` rows included: merging them
    was not allowed to narrow the range either of them covered.
    """
    assert size_fi(num_bytes) == expected


def test_only_one_byte_formatter_exists() -> None:
    """**There is exactly one byte-count formatter in the whole package.**

    The two copies had already drifted apart: ``cli._SIZE_UNITS`` ended at
    ``"Pt"`` and ``fetch._SIZE_UNITS`` at ``"Tt"``, so the same number could
    print differently depending on which command printed it. The claim is read
    from the syntax tree and not from strings: no module other than
    ``stages/fetch.py`` may assign the name ``_SIZE_UNITS``, and no module at
    all may define or call the name ``_human_size``.

    **The exemption is a path and not a file name**, and **an assignment is
    looked at in both of its forms.** The review of 2026-09-06 found two
    gaps: ``path.name != "fetch.py"`` would have let any other ``fetch.py`` in
    the tree have a table of its own, and a bare ``ast.Assign`` would have
    missed the annotated ``_SIZE_UNITS: tuple[str, ...] = (...)``, which is an
    ``ast.AnnAssign``. Both are cheap, and this guard is the item's only
    structural protection.

    The guard exists because going back to the old way is one function long
    and would not show in any output until the number happened to be large
    enough.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "pappascout"
    allowed = src / "stages" / "fetch.py"
    not_found: list[str] = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(src).as_posix()
        for node in ast.walk(tree):
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "_SIZE_UNITS"
                    and path != allowed
                ):
                    not_found.append(f"{rel}:{node.lineno} _SIZE_UNITS")
            if isinstance(node, ast.FunctionDef) and node.name == "_human_size":
                not_found.append(f"{rel}:{node.lineno} def _human_size")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_human_size"
            ):
                not_found.append(f"{rel}:{node.lineno} _human_size()")
    assert not_found == [], (
        "There is more than one byte-count formatter: the same number would "
        f"print differently in different commands. {not_found}"
    )


# --- The structure ------------------------------------------------------------


def test_pipeline_packages_expose_their_contracts() -> None:
    """Story 1.2 created the pipeline's first stage and its port.

    Replaces Story 1.1's ``test_no_pipeline_stages_exist_yet``, which guarded
    against the skeleton story implementing the pipeline ahead of time. A
    named symbol is checked rather than the import alone: an empty package
    would pass the latter.
    """
    import importlib

    expected = {
        "pappascout.stages": ("StageResult", "archive_paths"),
        "pappascout.stages.parse": ("run", "resolve_demo", "default_parser"),
        "pappascout.stages.classify": ("run", "resolve_team", "team_keys"),
        "pappascout.stages.aggregate": ("run", "resolve_team", "team_keys"),
        # Story 2.4: the pipeline's last stage and its presentation layer.
        "pappascout.stages.render": (
            "run",
            "resolve_team",
            "team_keys",
            "read_report",
            "round_list_paths",
        ),
        "pappascout.render": (
            "render_report",
            "build_view",
            "template_digest",
            "round_list_demo_ids",
        ),
        # Story 2.3: the report model is the shared contract between the
        # aggregate and render stages, so its name and its schema version are
        # part of the structure.
        "pappascout.domain.report": ("Report", "REPORT_SCHEMA_VERSION"),
        "pappascout.domain.aggregate": ("build_report", "positions_for"),
        "pappascout.adapters": (
            "DemoParser",
            "DemoTables",
            "ROUNDS_ADAPTER_COLUMNS",
            "TICKS_ADAPTER_COLUMNS",
        ),
    }
    for name, symbols in expected.items():
        module = importlib.import_module(name)
        for symbol in symbols:
            assert hasattr(module, symbol), f"{name}.{symbol}"


def test_help_lists_parse() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "parse" in result.output


def test_help_lists_every_pipeline_command() -> None:
    """The pipeline's commands are in the listing, in the order they are run.

    Adding one command without this claim would leave it out of the help
    unnoticed -- and the user, who does not code, would find it nowhere.
    """
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in (
        "info",
        "discover",
        "select",
        "fetch",
        "collect",
        "import",
        "parse",
        "classify",
        "aggregate",
        "report",
        "scout",
    ):
        assert command in result.output, command


# -- Story 3.7 (items 8, 9): a claim that is not true -------------------------
#
# In this project a wrong claim is more dangerous than missing information:
# the next reader trusts it. The subject of these tests is therefore the
# documentation and not the behaviour -- and that is deliberate.


def test_the_module_that_decides_the_order_exists() -> None:
    """``stages.pipeline`` is there, so the claim about it is now true.

    **This test used to assert the opposite**, and both versions guard the
    same thing: that no docstring claims a module that does not exist. Story
    3.7 found two package docstrings and ``stages.fetch``'s promising a
    ``stages.pipeline`` that had never been written, and deleted the claims.
    Story 4.1 wrote the module, so the claims may come back -- and this is
    the assertion that has to change with them, in the same commit, rather
    than being left pointing the other way.

    The wrong half is now the old sentence. A docstring still saying the
    order is decided one command at a time would send a reader looking for
    the chaining in the command line, where it no longer is.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "pappascout"
    assert (src / "stages" / "pipeline.py").is_file()

    stale = "no module chaining the stages"
    claims: list[str] = []
    for path in sorted(src.rglob("*.py")):
        for number, row in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if stale in row:
                claims.append(f"{path.name}:{number}")
    assert claims == [], (
        "Something still claims that no module chains the stages. " f"{claims}"
    )


def test_the_team_index_does_not_promise_a_rename_to_a_shipped_story() -> None:
    """``discover`` promised the archive would be renamed "in Story 3.4".

    Story 3.4 was downloading demos with the Downloads API and did not touch
    the naming of the archive at all. A story that shipped without doing what
    was promised is worse than unrecorded work: the reader checks the story,
    finds nothing, and does not know which of the two is wrong.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "pappascout"
    source = (src / "stages" / "discover.py").read_text(encoding="utf-8")

    # T5 translated ``discover``: the Finnish sentence this guard used to look
    # for is no longer in the file, so a guard for it would pass for ever
    # without guarding anything.
    assert "renaming is Story" not in source
