"""The ratchet for prose: what has been translated stays translated.

Story 3.11 started translating this repository's docstrings and comments from
Finnish into English, one tranche at a time. Six tranches over a codebase this
size drift without a guard: tranche four adds a Finnish docstring to something
tranche one translated, and nobody notices until the next audit.

So the work already done is **listed**, and this file re-reads the tree every
run. Adding an entry to :data:`TRANSLATED_TRANCHES` before its tranche is done
fails here -- which is the point: the list is a record of what has been
finished, not a wish. Same shape as the names ratchet in
:mod:`test_public_repo`, for a different property.

**A tranche is a package and its tests, and the structure says so.** A
package's tests do not live in the package and their names do not map to it
-- ``tests/test_paths.py`` says nothing about ``archive`` -- so a list of
package names cannot express "and these three test files". Story 3.11 started
package-shaped and that gap left roughly 7 000 Finnish lines in ``tests/``
outside every guard. The fix is not an instruction to remember: each
:class:`Tranche` carries its package **and** its test files, and
:func:`test_every_tranche_names_the_tests_that_go_with_it` refuses a tranche
that names none.

**Each entry records how many files it must cover, not just that it exists.**
A directory entry covering four modules today keeps resolving after one of
them is moved out of the package: the entry is still a real path, it still
has prose, and a list length does not change. The recorded module count does
change, so the move fails here instead of quietly leaving a module outside
every guard.

**What this actually checks, and what it does not.** It checks *characters*,
not language: a docstring line is rejected when it carries one of the letters
in :data:`FINNISH_CHARACTERS`. Finnish that avoids them passes -- "Manifesti
on rikki" would slip through. That is a known limit, not a claim to the
contrary; the check is cheap, has no false positives, and catches the
overwhelming majority of Finnish prose. The reviewer is still the one who
reads the translation.

It reads module, class and function docstrings and ``#`` comments. **An
attribute docstring -- a bare string expression written after an assignment
-- is not seen**, because the walk looks only at the first statement of a
body. This repository documents attributes with the ``#:`` comment style, so
the gap is theoretical today; it is listed because the next tranche may not.

It also reads docstrings and comments only, never code strings. A Finnish
string that reaches the console is caught **only where a test asserts on that
message** -- Story 3.11 had to add such assertions to four messages that had
none, so the cover is real but it is not automatic, and a new message without
one is caught by nothing here. One deliberate non-ASCII fixture
(``test_atomic_write`` proving a UTF-8 round trip) also has to stay non-ASCII
to mean anything, which is a second reason code strings are out of scope.

Removing a whole :class:`Tranche` is not caught by any number either. It is
caught by review, because it deletes a named block rather than editing a
digit -- which is exactly the difference this file is built on.

Report vocabulary is out of scope on purpose. It lives in ``*_FI`` constants
and in ``render/``, it stays Finnish permanently, and neither is listed here.
"""

from __future__ import annotations

import ast
import io
import tokenize
from dataclasses import dataclass
from pathlib import Path

from conftest import REPO_ROOT


@dataclass(frozen=True)
class Tranche:
    """One tranche of the translation: what it translated **and** its tests.

    Attributes:
        story: The story that translated it, so the record says which one.
        sources: What the tranche translated, each path paired with the
            smallest number of ``.py`` files it must cover. A package
            directory carries the count of its modules; a single module
            carries one. The count is recorded rather than derived, because a
            module moved out of a listed package would otherwise leave the
            entry resolving, the prose check passing and the list length
            unchanged.
        tests: The test files the tranche translated. Never empty -- a
            package's tests carry the same prose, and a tranche that leaves
            them out has done half the job.

    **Why sources is a list of paths and not one package.** Story 3.11
    translated a whole package and this class was shaped for that. That shape
    does not survive the rest of the plan: measured 2026-09-08, the tranches
    after it translate *part* of a package at a time, because ``adapters``
    alone is ten times the pilot and ``demo_parser.py`` is five times it on
    its own. Listing the package while two of its modules are still Finnish
    fails the ratchet; leaving the tranche unlisted until its package
    finishes leaves translated files unguarded for several tranches, which is
    the drift this file exists to stop. So a tranche names what it actually
    translated, at whatever granularity that was.
    """

    story: str
    sources: tuple[tuple[str, int], ...]
    tests: tuple[str, ...]

    def entries(self) -> tuple[tuple[str, int], ...]:
        """Every listed path with the fewest ``.py`` files it must cover.

        A test file covers exactly itself, so its minimum is one; a source
        carries the number recorded for it, which is what makes a move
        visible.
        """
        return (*self.sources, *((t, 1) for t in self.tests))


#: The translation, tranche by tranche. Add a tranche **in the commit that
#: translates it**, never before, and never as a package without its tests.
TRANSLATED_TRANCHES: tuple[Tranche, ...] = (
    Tranche(
        story="3.11",
        sources=(("src/pappascout/archive", 4),),
        tests=(
            "tests/test_atomic_write.py",
            "tests/test_manifest.py",
            "tests/test_paths.py",
            # The ratchet itself. It is English prose written in the same
            # commit, so it is listed for the same reason as everything else:
            # nothing here is exempt from the rule it enforces.
            "tests/test_translated_prose.py",
        ),
    ),
    Tranche(
        story="T2",
        # Three of the five modules in ``adapters``. The package cannot be
        # listed yet: ``demo_parser.py`` still holds 1 159 Finnish prose
        # lines, measured 2026-09-08. T4 takes it, and the package entry
        # replaces the module entries when it does.
        sources=(
            ("src/pappascout/adapters/protocols.py", 1),
            ("src/pappascout/adapters/__init__.py", 1),
            ("src/pappascout/adapters/decompress.py", 1),
        ),
        tests=("tests/test_faceit_demos.py",),
    ),
    Tranche(
        story="T3",
        # The fourth of the five modules in ``adapters``, listed on its own
        # for the reason above. ``protocols.py`` changed in this commit too
        # -- ``MatchSource`` stopped promising that its error message is in
        # Finnish, which was true only while this module was untranslated --
        # but it is already listed under T2, and a path may be listed once.
        sources=(("src/pappascout/adapters/faceit.py", 1),),
        tests=("tests/test_faceit.py",),
    ),
)

#: Every listed path with its recorded minimum, flattened out of the tranches.
TRANSLATED_ENTRIES: tuple[tuple[str, int], ...] = tuple(
    entry for tranche in TRANSLATED_TRANCHES for entry in tranche.entries()
)

#: Just the paths, for the scan.
TRANSLATED_PATHS: tuple[str, ...] = tuple(path for path, _ in TRANSLATED_ENTRIES)

#: The characters that give Finnish away. Deliberately just these three
#: letters in both cases -- see the module docstring on what that does and
#: does not prove.
FINNISH_CHARACTERS = frozenset("äöåÄÖÅ")


def _files_for(entry: str) -> list[Path]:
    """The ``.py`` files one list entry covers, in a stable order.

    A directory contributes its whole tree, a file contributes itself. The
    entry must exist: a typo or a rename would otherwise silently contribute
    nothing, and this guard would go on reporting success over it.
    """
    target = REPO_ROOT / entry
    if target.is_dir():
        return sorted(
            path
            for path in target.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    if target.is_file():
        return [target]
    return []


def _read_source(path: Path) -> str:
    """Read a file as UTF-8, naming it if the bytes are not UTF-8.

    ``UnicodeDecodeError`` carries the byte offset but not the file. Over a
    scan of a whole package that leaves the reader with an offset into a file
    the message never names.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        exc.add_note(f"while reading {path}")
        raise


def _prose_line_numbers(source: str, filename: str = "<source>") -> set[int]:
    """The 1-based line numbers a docstring or a comment occupies.

    Docstrings come from :mod:`ast` -- the string expression that opens a
    module, class or function body -- and comments from :mod:`tokenize`,
    which is the only way to see them at all, since the parser throws them
    away. Both are needed: ``#:`` attribute documentation is a comment, and
    most of the reasoning in this codebase lives in docstrings.

    Args:
        source: The file's text.
        filename: The file it came from. Passed to :func:`ast.parse` and
            added to a tokenizer failure, because neither names the file by
            itself and a scan reports one offset out of thousands of lines.
    """
    lines: set[int] = set()

    tree = ast.parse(source, filename=filename)
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr):
            continue
        value = first.value
        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            continue
        lines.update(range(value.lineno, (value.end_lineno or value.lineno) + 1))

    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                lines.add(token.start[0])
    except tokenize.TokenError as exc:
        exc.add_note(f"while tokenizing {filename}")
        raise

    return lines


def finnish_prose_lines(path: Path) -> list[str]:
    """Docstring and comment lines in ``path`` that carry a Finnish letter.

    Each entry names the file and the line, then the line itself, because a
    count alone does not tell the reader what to fix.

    The name is repository-relative when the file is in the repository, and
    the full path when it is not. **The fallback is what makes this function
    testable**: a sample written into ``tmp_path`` is not under
    :data:`REPO_ROOT`, and without it the only code that decides whether a
    line is Finnish could never be run against a line that is.
    """
    source = _read_source(path)
    rows = source.splitlines()
    try:
        name = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        name = path.as_posix()
    found: list[str] = []
    for number in sorted(_prose_line_numbers(source, name)):
        if number > len(rows):  # pragma: no cover - defensive
            continue
        line = rows[number - 1]
        if FINNISH_CHARACTERS.intersection(line):
            found.append(f"{name}:{number}: {line.strip()}")
    return found


#: A sample the scanner is proved against, line by line.
#:
#: The assertions name line numbers, so the numbering **is** the fixture::
#:
#:      1  module docstring, Finnish
#:      2  blank
#:      3  code string, Finnish -- must NOT be reported
#:      4  blank
#:      5  comment, Finnish
#:      6  code
#:      7  blank
#:      8  blank
#:      9  class statement
#:     10  class docstring, first line, Finnish
#:     11  class docstring, blank line
#:     12  class docstring, Finnish
#:     13  class docstring, closing quotes
#:     14  blank
#:     15  method statement
#:     16  method docstring, first line, Finnish
#:     17  method docstring, blank line
#:     18  method docstring, Finnish
#:     19  method docstring, closing quotes
#:     20  blank
#:     21  blank
#:     22  function statement
#:     23  function docstring, first line, Finnish
#:     24  function docstring, blank line
#:     25  function docstring, Finnish
#:     26  function docstring, closing quotes
SAMPLE_SOURCE = (
    '"""Moduulin selitys ääkkösin."""\n'
    "\n"
    'NAME = "ääkköset eivät ole prosaa"\n'
    "\n"
    "#: Selittävä kommentti.\n"
    "VALUE = 1\n"
    "\n"
    "\n"
    "class Luokka:\n"
    '    """Luokan selitys ääkkösin.\n'
    "\n"
    "    Toinen rivi ääkkösin.\n"
    '    """\n'
    "\n"
    "    def metodi(self) -> None:\n"
    '        """Metodin selitys ääkkösin.\n'
    "\n"
    "        Toinen rivi ääkkösin.\n"
    '        """\n'
    "\n"
    "\n"
    "def funktio() -> None:\n"
    '    """Funktion selitys ääkkösin.\n'
    "\n"
    "    Toinen rivi ääkkösin.\n"
    '    """\n'
)


def _sample(tmp_path: Path) -> Path:
    """Write :data:`SAMPLE_SOURCE` where the scanner can read it."""
    sample = tmp_path / "sample.py"
    sample.write_text(SAMPLE_SOURCE, encoding="utf-8")
    return sample


def test_the_scanner_reports_the_finnish_lines_it_finds(tmp_path: Path) -> None:
    """The detector is proved by what it **returns**, not by an empty list.

    :func:`finnish_prose_lines` is the only code that decides whether a line
    is Finnish, and the ratchet below runs it over paths where Finnish is
    already gone -- so ``assert not offenders`` passes just as happily over a
    detector that finds nothing at all. This is the case that fails when it
    stops finding things: every reported row is spelled out, so the file
    name, the line number and the text are each pinned.

    The code string on line 3 carries the same letters and must not be
    reported. Prose is prose; a string is not.
    """
    sample = _sample(tmp_path)
    expected = {
        1: '"""Moduulin selitys ääkkösin."""',
        5: "#: Selittävä kommentti.",
        10: '"""Luokan selitys ääkkösin.',
        12: "Toinen rivi ääkkösin.",
        16: '"""Metodin selitys ääkkösin.',
        18: "Toinen rivi ääkkösin.",
        23: '"""Funktion selitys ääkkösin.',
        25: "Toinen rivi ääkkösin.",
    }
    name = sample.as_posix()

    assert finnish_prose_lines(sample) == [
        f"{name}:{number}: {text}" for number, text in expected.items()
    ]


def test_the_walker_sees_classes_functions_and_every_docstring_line(
    tmp_path: Path,
) -> None:
    """Both halves of the walk, over all the holders and over full spans.

    Two ways this could silently stop reading half the prose. **The
    holders:** a sample with no ``def`` and no ``class`` exercises only
    ``ast.Module``, so dropping ``ClassDef`` and ``FunctionDef`` -- or
    walking ``[tree]`` instead of ``ast.walk`` -- would go unnoticed while
    ``manifest.py``'s method docstrings went back to Finnish unseen. **The
    span:** a one-line docstring never exercises
    ``range(lineno, end_lineno + 1)``, so a walker that reported only the
    opening line would miss every continuation line.

    The comment on line 5 proves the ``tokenize`` pass separately, and the
    code lines are absent, which is what makes the two passes distinguishable
    from "reports everything".
    """
    sample = _sample(tmp_path)

    numbers = _prose_line_numbers(_read_source(sample), sample.as_posix())

    assert numbers == {1, 5, 10, 11, 12, 13, 16, 17, 18, 19, 23, 24, 25, 26}


def test_every_tranche_names_the_tests_that_go_with_it() -> None:
    """The pairing rule is structural, not an instruction in a docstring.

    "Add the package and its tests together" was a sentence in bold, and a
    sentence cannot fail. A tranche that adds ``src/pappascout/domain`` and
    forgets ``tests/test_domain.py`` leaves that file's prose outside every
    guard while everything stays green -- which is exactly how roughly 7 000
    Finnish lines in ``tests/`` were left out the first time.
    """
    assert TRANSLATED_TRANCHES
    for tranche in TRANSLATED_TRANCHES:
        assert tranche.sources, f"tranche {tranche.story} names no sources"
        assert tranche.tests, f"tranche {tranche.story} names no test files"
        for source, minimum in tranche.sources:
            assert source, tranche
            assert minimum >= 1, f"{source} records a minimum below one"


def test_every_entry_covers_what_it_records_and_has_prose() -> None:
    """An entry that resolves to too little, or to nothing, guards too little.

    Three ways this file could quietly become a no-op, and all three are a
    number rather than a judgement: an entry no longer names anything on disk
    after a rename; an entry still names a directory but a module has been
    moved out of it; an entry names files that hold no docstrings or comments
    at all, so scanning them proves nothing.
    """
    listed = [path for path, _ in TRANSLATED_ENTRIES]
    assert len(set(listed)) == len(listed), listed

    for entry, minimum in TRANSLATED_ENTRIES:
        files = _files_for(entry)
        assert files, f"{entry} names nothing on disk"
        assert len(files) >= minimum, (
            f"{entry} covers {len(files)} .py files, but {minimum} are "
            "recorded -- a module has been moved or renamed out of it"
        )
        prose = sum(
            len(_prose_line_numbers(_read_source(path), str(path)))
            for path in files
        )
        assert prose > 0, f"{entry} has no docstrings or comments to check"


def test_no_finnish_prose_in_anything_translated() -> None:
    """No docstring or comment line in a listed path is Finnish.

    The failure names every file and line, so the reader can go straight to
    the text rather than hunting for it.
    """
    offenders: list[str] = []
    for entry in TRANSLATED_PATHS:
        for path in _files_for(entry):
            offenders.extend(finnish_prose_lines(path))

    assert not offenders, (
        "Finnish prose in something already translated:\n" + "\n".join(offenders)
    )
