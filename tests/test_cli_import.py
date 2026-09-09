"""``pappascout import`` -- the command's tests (Story 3.6).

Four things are locked down here:

* **``--kylla`` does NOT skip the map check's question.** That is the epic's
  own requirement and this tool's only place where the flag does not silence
  a question. The same flag does skip the overwrite question, and that
  difference is exactly what the tests have to tell apart.
* **The question before the transfer, and the plan before the question.** The
  order is guarded separately: the gate can be in order even if the user saw
  the plan only afterwards, and then they answer a question whose grounds
  they have not seen.
* **A negative answer transfers nothing.**
* **Every row of the output is a claim, and the claims are checked one by
  one.** A review mutated the values on the screen and got six lies through
  70 tests at once -- among them ``downloads_api`` as an imported demo's
  source, that is, exactly the claim the whole story exists to refute. So the
  output is broken into rows and every value is compared against **the truth
  computed from the disk**, not against another row of the output.

The ports are fakes and the network is not touched once.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from conftest import LOCAL_DEMOS_DIRNAME
from test_stage_import import (
    FACEIT_NAME,
    FACEIT_NAME_PLAIN,
    MATCH,
    PLAIN_BYTES,
    UNIT,
    ZSTD_BYTES,
    FakeMapNameParser,
    FakeMatchSource,
    match,
)
from typer.testing import CliRunner

from pappascout.cli import EXIT_KNOWN_ERROR, app, main
from pappascout.domain.models import SETTINGS_ENV_VAR, load_settings
from pappascout.errors import PappascoutError
from pappascout.stages import archive_paths
from pappascout.stages import import_demo as import_stage

runner = CliRunner()


@pytest.fixture(params=["arkisto", "paikallinen"])
def tuonti(request, settings_file: Path, tmp_path: Path, monkeypatch):
    """The real command, fake ports, the archive in a temporary directory.

    **Both demo directory modes, in every test.** The same reasoning as with
    ``fetch``'s corresponding fixture: a supported mode the command tests do
    not run goes through the CLI zero times.

    Returns ``(archive, parser)`` -- the fake reader's map name can be changed
    inside a test before the command is run.
    """
    if request.param == "paikallinen":
        text = settings_file.read_text(encoding="utf-8")
        line = next(r for r in text.splitlines() if r.startswith("# demos_root = "))
        settings_file.write_text(
            text.replace(line, f"demos_root = '{tmp_path / LOCAL_DEMOS_DIRNAME}'", 1),
            encoding="utf-8",
        )
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))

    archive = archive_paths(load_settings().project)
    archive.import_dir().mkdir(parents=True, exist_ok=True)
    (archive.import_dir() / FACEIT_NAME).write_bytes(ZSTD_BYTES)

    parser = FakeMapNameParser({FACEIT_NAME: "de_ancient"})
    monkeypatch.setattr(
        import_stage,
        "default_source",
        lambda settings, arc: FakeMatchSource({MATCH: match()}),
    )
    monkeypatch.setattr(import_stage, "default_parser", lambda: parser)
    # The disk must not be a variable of the test: the check is its own test.
    monkeypatch.setattr(import_stage, "_free_space", lambda _path: 100 * 1024**3)
    return archive, parser


def invoke(*args: str, input: str | None = None, map_no: str = "1"):
    return runner.invoke(
        app, ["import", "--match", MATCH, "--map", map_no, *args], input=input
    )


def imported(archive) -> Path | None:
    return archive.find_demo(UNIT)


def rivit(output: str) -> dict[str, str]:
    """Break the output into a dictionary of ``label -> value``.

    **A claim about the value and not about a substring.** ``assert path in
    output`` would pass even when the same path happens to be on another row
    of the screen -- and that overlap is exactly what let the review's
    mutations through: the demo's target path is printed twice, so either one
    alone was free to lie. When a row is broken into a label and a value,
    every row answers for its own content.
    """
    tulos: dict[str, str] = {}
    for rivi in output.splitlines():
        if not rivi.startswith("  ") or rivi.startswith("    "):
            continue
        runko = rivi[2:]
        # ``_line`` pads the label to a fixed width; a run of two spaces
        # separates the label from the value.
        if "  " not in runko:
            continue
        otsikko, _, arvo = runko.partition("  ")
        tulos[otsikko.strip()] = arvo.strip()
    return tulos


# -- A successful import -----------------------------------------------------


def test_a_matching_map_needs_no_question_at_all(tuonti) -> None:
    """A matching map is a full answer: there is nothing to ask.

    A question that is asked even when there is nothing to decide teaches the
    user to answer it without looking -- and then it no longer protects
    against the case it exists for.
    """
    archive, _parser = tuonti

    result = invoke()

    assert result.exit_code == 0, result.output
    assert f"Import {UNIT} anyway?" not in result.output
    assert imported(archive) is not None
    meta = json.loads(
        (archive.demos_dir() / f"{UNIT}.meta.json").read_text(encoding="utf-8")
    )
    assert meta["source"] == "import"


# -- C2: every row of the output is a claim ----------------------------------


def test_the_plan_names_the_unit_that_is_imported(tuonti) -> None:
    _archive, _parser = tuonti

    assert rivit(invoke().output)["Importing"] == UNIT


def test_the_plan_names_the_source_file_that_is_read(tuonti) -> None:
    """The source path is a claim of its own and not an echo of the target."""
    archive, _parser = tuonti
    lahde = archive.import_dir() / FACEIT_NAME

    assert rivit(invoke().output)["Source"] == str(lahde)


def test_the_plan_names_the_target_path_that_is_written(tuonti) -> None:
    """The target path on the screen is where the file really comes into being."""
    archive, _parser = tuonti

    naytetty = rivit(invoke().output)["Target"]

    assert naytetty == str(archive.demos_dir() / f"{UNIT}.dem.zst")
    assert Path(naytetty).is_file()


def test_the_plan_size_is_the_size_of_the_file_on_disk(tuonti) -> None:
    """The size is read from the disk, not guessed.

    The review added ``+999 Gt`` to the size in two places and 70 tests
    passed. A number nothing compares against anything is decoration.
    """
    from pappascout.stages.fetch import size_fi

    _archive, _parser = tuonti

    assert rivit(invoke().output)["Size"] == size_fi(len(ZSTD_BYTES))


def test_the_plan_says_move_when_the_file_is_moved(tuonti) -> None:
    _archive, _parser = tuonti

    assert rivit(invoke().output)["Method"] == "move"


def test_the_plan_says_copy_when_the_file_is_copied(tuonti, tmp_path) -> None:
    """A copy and a move are different things, and the screen must tell them apart.

    The review put the text "copy (the source stays put)" on a move and 70
    tests passed -- that is, the user could have read from the screen that
    their file stays put when it had just been deleted.
    """
    _archive, parser = tuonti
    ulkoa = tmp_path / "ulkoa" / "oma.dem.zst"
    ulkoa.parent.mkdir(parents=True, exist_ok=True)
    ulkoa.write_bytes(ZSTD_BYTES)
    parser.names["oma.dem.zst"] = "de_ancient"

    result = invoke("--file", str(ulkoa))

    assert rivit(result.output)["Method"] == "copy (the source stays put)"
    assert ulkoa.is_file()


def test_the_plan_shows_both_map_observations(tuonti) -> None:
    """Both observations are shown even when they agree."""
    _archive, _parser = tuonti

    rows = rivit(invoke().output)

    assert rows["Map from the header"] == "de_ancient"
    assert rows["Map from the veto"] == "de_ancient"
    assert rows["Map check"] == "matches"


def test_the_screen_never_claims_a_match_when_there_is_none(tuonti) -> None:
    """**"Map check matches" on a real mismatch is the worst lie of all.**

    The review made the condition always true and got the screen to say
    "matches" on the same screen where the forced question is asked -- that
    is, the user would read the question and, above it, an assurance that
    nothing is wrong.
    """
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="n\n")

    rows = rivit(result.output)
    assert rows["Map check"].startswith("DOES NOT MATCH")
    assert rows["Map from the header"] == "de_nuke"
    assert rows["Map from the veto"] == "de_ancient"


def test_the_plan_says_whether_completeness_could_be_checked(tuonti) -> None:
    """A compressed file: completeness is checked against the frame's size."""
    _archive, _parser = tuonti

    assert rivit(invoke().output)["Completeness"].startswith("checked")


def test_the_plan_says_out_loud_when_completeness_cannot_be_checked(
    tuonti,
) -> None:
    """**With an uncompressed demo the uncertainty has to be reported.**

    Measured 2026-09-05: a file truncated half way looks intact in every
    respect -- the right extension, the right map name. A compressed one has
    the frame's size to check against; an uncompressed one has nothing, and
    silence would look like certainty.
    """
    archive, parser = tuonti
    (archive.import_dir() / FACEIT_NAME).unlink()
    (archive.import_dir() / FACEIT_NAME_PLAIN).write_bytes(PLAIN_BYTES)
    parser.names[FACEIT_NAME_PLAIN] = "de_ancient"

    result = invoke()

    assert rivit(result.output)["Completeness"].startswith("COULD NOT BE CHECKED")


def test_the_result_names_the_files_that_were_written(tuonti) -> None:
    """The result's paths point at files that exist."""
    archive, _parser = tuonti

    rows = rivit(invoke().output)

    assert rows["Demo"] == str(archive.demos_dir() / f"{UNIT}.dem.zst")
    assert rows["Metadata"] == str(archive.demos_dir() / f"{UNIT}.meta.json")
    assert Path(rows["Demo"]).is_file()
    assert Path(rows["Metadata"]).is_file()


def test_the_result_sha256_is_the_digest_of_the_written_file(tuonti) -> None:
    """The digest on the screen is **that file's** digest, the one that was made.

    The review changed the value to ``"0"*64`` and 70 tests passed. The
    digest is the number an imported demo is identified by later -- ``parse``
    reads it from the metadata file and does not compute it again, so this is
    the only time it is seen.
    """
    archive, _parser = tuonti

    rows = rivit(invoke().output)

    todellinen = hashlib.sha256(
        (archive.demos_dir() / f"{UNIT}.dem.zst").read_bytes()
    ).hexdigest()
    assert rows["sha256"] == todellinen
    meta = json.loads(Path(rows["Metadata"]).read_text(encoding="utf-8"))
    assert meta["sha256"] == todellinen


def test_the_result_says_the_demo_came_from_an_import(tuonti) -> None:
    """**The review got the screen to claim an imported demo was downloaded.**

    ``downloads_api`` as an imported demo's source is exactly the claim the
    whole story exists to refute -- and it got through 70 tests.
    """
    archive, _parser = tuonti

    rows = rivit(invoke().output)

    assert rows["Source entry"] == "import"
    meta = json.loads(
        (archive.demos_dir() / f"{UNIT}.meta.json").read_text(encoding="utf-8")
    )
    assert rows["Source entry"] == meta["source"]


def test_the_result_size_is_the_size_of_the_written_file(tuonti) -> None:
    from pappascout.stages.fetch import size_fi

    archive, _parser = tuonti

    rows = rivit(invoke().output)
    koko = (archive.demos_dir() / f"{UNIT}.dem.zst").stat().st_size

    assert rows["Size"] == size_fi(koko)


def test_every_note_reaches_the_screen(tuonti) -> None:
    """**The notes must not disappear.**

    The review emptied the note loop and 70 tests passed -- that is, every
    warning (a replaced file, an orphaned metadata file, an unchecked
    completeness, a confirmed map mismatch) could have disappeared from the
    screen in one line.
    """
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke("--kylla", input="y\n")

    assert result.exit_code == 0, result.output
    # The mismatch and the source file's fate are both notes of their own.
    assert "de_nuke" in result.output
    assert "was removed from the import folder" in result.output


def test_the_run_time_is_reported(tuonti) -> None:
    _archive, _parser = tuonti

    assert "Run time" in rivit(invoke().output)


# -- C3: the plan before the transfer ----------------------------------------


def test_the_plan_is_printed_before_anything_is_transferred(
    tuonti, monkeypatch
) -> None:
    """**The user has to see the plan before anything happens.**

    The gate is in order without this test too -- ``run`` is called only
    after the questions -- but the *seeing* was not guarded: the review moved
    the printing of the plan below the result and 28 tests passed. The user
    would then answer a question whose grounds they have not seen.

    The claim is made by stopping the transfer: if the plan is printed only
    after ``run``, it is not printed at all.
    """
    _archive, _parser = tuonti

    def boom(*args, **kwargs):
        raise PappascoutError("the transfer was stopped", advice="this is a test")

    monkeypatch.setattr(import_stage, "run", boom)

    result = invoke()

    assert result.exit_code != 0
    rows = rivit(result.output)
    assert rows["Importing"] == UNIT
    assert "Target" in rows
    assert "Map check" in rows


def test_the_plan_is_printed_before_the_question_is_asked(tuonti) -> None:
    """A question without its grounds is a formality, not a question."""
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    output = invoke(input="n\n").output

    assert output.index("Map from the header") < output.index(
        f"Import {UNIT} anyway?"
    )


# -- The map mismatch: --kylla does NOT skip it ------------------------------


def test_kylla_does_not_skip_the_map_confirmation(tuonti) -> None:
    """**The epic's own requirement.** The flag does not silence this question."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke("--kylla", input="n\n")

    assert result.exit_code == 0, result.output
    assert "The map does not match" in result.output
    assert f"Import {UNIT} anyway?" in result.output
    assert imported(archive) is None
    assert (archive.import_dir() / FACEIT_NAME).read_bytes() == ZSTD_BYTES


def test_kylla_does_not_skip_the_question_when_there_is_no_veto_data(
    tuonti, monkeypatch
) -> None:
    """Not having checked is not a match, and the flag must not make it one."""
    archive, _parser = tuonti
    monkeypatch.setattr(
        import_stage,
        "default_source",
        lambda settings, arc: FakeMatchSource({MATCH: match(picks=())}),
    )

    result = invoke("--kylla", input="n\n")

    assert result.exit_code == 0, result.output
    assert "veto data" in result.output
    assert imported(archive) is None


def test_the_mismatch_question_offers_the_number_that_would_be_right(
    tuonti,
) -> None:
    """The question offers a way forward and not doubt alone."""
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="n\n")

    assert "--map 2" in result.output


def test_answering_yes_to_the_mismatch_imports_anyway(tuonti) -> None:
    """The question is a question and not a barrier: the user is told and decides."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke("--kylla", input="y\n")

    assert result.exit_code == 0, result.output
    assert imported(archive) is not None


# -- Overwriting: --kylla does skip it ---------------------------------------


def test_kylla_does_skip_the_overwrite_question(tuonti) -> None:
    """The same flag, a different question, a different outcome -- by design."""
    archive, _parser = tuonti
    vanha = archive.demos_dir() / f"{UNIT}.dem.zst"
    vanha.parent.mkdir(parents=True, exist_ok=True)
    vanha.write_bytes(b"old")

    result = invoke("--kylla")

    assert result.exit_code == 0, result.output
    assert f"Replace {UNIT}?" not in result.output
    assert vanha.read_bytes() == ZSTD_BYTES


def test_without_kylla_the_existing_file_is_not_overwritten_silently(
    tuonti,
) -> None:
    archive, _parser = tuonti
    vanha = archive.demos_dir() / f"{UNIT}.dem.zst"
    vanha.parent.mkdir(parents=True, exist_ok=True)
    vanha.write_bytes(b"old")

    result = invoke(input="n\n")

    assert result.exit_code == 0, result.output
    assert f"Replace {UNIT}?" in result.output
    assert vanha.read_bytes() == b"old"


# -- The prompt --------------------------------------------------------------


def test_the_question_offers_its_own_options_and_not_typers(tuonti) -> None:
    """``typer.confirm``'s ``[y/N]`` and ``Aborted.`` do not belong here.

    The name used to say the question and the cancellation are in Finnish;
    AD-11 moved the console into English, and what is left to pin is that
    this is not ``typer.confirm``.
    """
    _archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="n\n")

    assert "[y/n]" in result.output
    assert "[y/N]" not in result.output
    assert "Aborted" not in result.output
    assert "Cancelled" in result.output


def test_the_cancellation_message_says_what_was_not_done(tuonti) -> None:
    """A cancellation says what was left undone -- and not about downloading."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="n\n")

    assert "The demo was not imported" in result.output
    assert "downloaded" not in result.output
    assert (archive.import_dir() / FACEIT_NAME).is_file()


def test_an_empty_answer_does_not_import(tuonti) -> None:
    """Enter is not yes: the default is the one that does not change the archive."""
    archive, parser = tuonti
    parser.names[FACEIT_NAME] = "de_nuke"

    result = invoke(input="\n")

    assert result.exit_code == 0, result.output
    assert imported(archive) is None


def test_a_non_numeric_map_is_the_tools_own_error(tuonti, monkeypatch, capsys) -> None:
    """**``--map abc`` must not fail with typer's own message.**

    The command line used to declare the value an integer, so ``typer``
    failed before the stage saw anything -- and the stage's own check was
    dead code, unreachable from the command line.
    """
    _archive, _parser = tuonti
    monkeypatch.setattr(
        "sys.argv",
        ["pappascout", "import", "--match", MATCH, "--map", "abc"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert "is not a whole number" in output
    assert "Invalid value" not in output


# -- The errors on the screen ------------------------------------------------


def test_a_rejection_shows_its_advice_on_its_own_line(
    tuonti, monkeypatch, capsys
) -> None:
    """The advice travels with the error and prints on a row of its own."""
    _archive, _parser = tuonti
    monkeypatch.setattr(
        "sys.argv",
        ["pappascout", "import", "--match", MATCH, "--map", "0"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert "starts at one" in output
    assert "-> " in output


def test_two_candidates_are_listed_on_screen(tuonti, monkeypatch, capsys) -> None:
    """Ambiguity is not settled quietly -- not on the screen either."""
    archive, parser = tuonti
    (archive.import_dir() / FACEIT_NAME_PLAIN).write_bytes(PLAIN_BYTES)
    parser.names[FACEIT_NAME_PLAIN] = "de_ancient"
    monkeypatch.setattr(
        "sys.argv",
        ["pappascout", "import", "--match", MATCH, "--map", "1", "--kylla"],
    )

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == EXIT_KNOWN_ERROR
    captured = capsys.readouterr()
    output = captured.err + captured.out
    assert FACEIT_NAME in output
    assert FACEIT_NAME_PLAIN in output
    # The advice points at the compressed one and is quoted so it can be
    # copied.
    assert f'--file "{archive.import_dir() / FACEIT_NAME}"' in output
    assert imported(archive) is None


# -- The help ----------------------------------------------------------------


def test_help_mentions_that_kylla_does_not_skip_the_map_check(tuonti) -> None:
    """The exception is in the help: otherwise it is found only by hitting it."""
    result = runner.invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    # One word, not a phrase: Typer wraps the help into a box and a phrase
    # would break on the wrap rather than on a real change. The capitalised
    # ``NOT`` appears nowhere else in this command's help.
    assert "NOT" in result.output


def test_help_does_not_claim_the_command_stays_off_the_network(tuonti) -> None:
    """Story 3.7 (item 10): the help claimed "The command downloads nothing
    from the network."

    The claim was measured false: ``FaceitClient.match_payload`` fetches the
    match from the interface if it is not in the response cache, and
    ``raw/faceit/matches-1-79f71e00-....json`` was written during an import
    run on 2026-09-05 at 21:37:35. **The claim was fixed, not the behaviour**
    -- the veto data fetched from the network is exactly what makes the map
    check possible.

    The help now says two things separately: demos are not downloaded (the
    demo is the file you give it), but the veto data can come from the
    network.
    """
    result = runner.invoke(app, ["import", "--help"])

    assert result.exit_code == 0
    teksti = " ".join(result.output.split())
    assert "does not download anything from the network" not in teksti
    assert "does not download demos" in teksti
    assert "cache" in teksti
