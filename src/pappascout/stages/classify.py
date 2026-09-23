"""``classify`` -- the pipeline's second stage: round types from the rounds table.

The stage reads ``parsed/<map_demo_id>/rounds.parquet`` and writes the
``classified/<team_key>/<map_demo_id>.parquet`` table, its manifest and the
same content as a readable round list ``<map_demo_id>.md``. **The demo is not
read and nothing is written into the ``parsed/`` directory** -- every value of
this stage is derived, and they are computed again on every run with pure
``domain.economy`` functions.

Both teams, one row
-------------------
The rounds table has two rows per round. The classification is done for
**both** teams in the same run, but the result is one row per round from the
subject team's point of view: the subject's type is ``round_type`` and the
opponent's ``opp_round_type``. The same demo can be classified for the other
team as well -- a result of its own is then written into its own
``classified/<team_key>/`` directory, and the ``parse`` stage is not run again.

One path to the round list
--------------------------
The round list's rows are **always** built from the finished ``CLASSIFIED``
table with :func:`round_list_rows`, on a fresh run and on a skipped one alike.
Two paths would drift apart sooner or later, and then ``--show`` would show
different figures after a skip than it did on the first run.

``team_key`` in this stage
--------------------------
The subject is chosen with the ``--team`` option directly by its **lineup key**
(``lineup_key``), and that is used as the directory name as well. The canonical
``team_key`` is a different id: it comes into being in ``discover`` and names
the team index and the selection files. Directories are not renamed in this
stage, so the two ids live side by side and the bridge between them is the
``lineup_keys`` field of ``index/teams.json``.

``is_league`` and ``roster_class`` are read, not computed
---------------------------------------------------------
Both describe the **match** and not the round, and both have already been
computed: ``select`` writes them into the team's selection file
(``index/selections/<team_key>.json``). This stage is their **reader** --
:func:`read_match_facts` looks up the row of the demo being handled, and the
values are set as they are onto every round row. The same inference in two
places would be two truths, so ``is_league`` is not inferred from
``competition_id`` here, nor ``roster_class`` from the roster thresholds.

**A value is not guessed at, and the reason is not left unsaid.** The column
stays empty for five different reasons -- there is no team index, the lineup
has no owner, the owner has no selection file, there is no row for the demo, or
the hits disagree about the field -- and each of them is named in
:attr:`MatchFacts.note`, which ``run`` carries into ``StageResult.reason``
(AD-9). Without it a broken bridge would look in the report exactly like a
hand-imported demo whose right value is empty: the report's ``unknown`` bucket
is a bucket and not an error state, and a guess ("probably a league match")
would be a false claim about the data.

**Consensus is field by field.** A lineup can be owned by more than one team,
and the same file can hold two rows for the same demo. ``is_league`` describes
the match (AD-10), so all the hits have to agree about it; ``roster_class`` is
judged against **that team's** standing roster (AD-6), so two owners get
different values for it in the normal course of things. A field that is
disagreed on stays empty -- the other one does not -- and neither is settled by
drawing lots.

**What is read is declared, and it is declared narrowly** (Story 4.7,
2026-09-23). Until then the two index files were read but not named in the
manifest's ``inputs``, so a re-run of ``select`` changed the values and nothing
noticed: ``classify`` skipped, its fingerprint stayed the same, ``aggregate``
skipped after it, and the report said "unknown" of eight demos whose
``is_league`` had been computed a minute earlier. A stage with an undeclared
input is the one thing AD-1's whole guarantee rests on not happening.

The declaration is :func:`match_facts_input`, and it does **not** digest either
file as a whole. Both carry a wall-clock ``generated_at`` that ``discover``
rewrites on every run, so a whole-file digest would re-classify the archive
after every ``discover`` -- the behaviour the manifest exists to prevent. The
input's identity is therefore computed from *what this stage reads into the
result*: this demo's ``is_league`` and ``roster_class``, one entry for every
selection row found, from every owner. Not from what it reads into the
**reason** -- who owns the lineup, whether there is an index at all, which
owners have no selection file -- because none of that reaches a written byte,
and digesting it would invalidate results that a re-run would reproduce
identically. A change elsewhere in either index does not re-run the stage; a
change to either value, from any owner, does, and it cascades into
``aggregate`` on its own, with no ``--force`` anywhere.

**The price is paid where it can be seen: the index files are read before the
skip is decided**, not after it as they were until Story 4.7. They are two
small JSON documents that the stage already parsed on the path it takes when it
does not skip, so the cost is a parse of two files per demo and no demo read.
The one behaviour that had to be kept deliberately is that a **broken**
selection file does not turn a finished result into an error: an input that
cannot be read is not evidence that it changed, so the skip stands and the
reason says out loud that the input could not be checked
(:func:`unchecked_facts_note`). A run that does not skip reads the values for
real and fails on the broken file exactly as it always did.

Re-running
----------
The manifest's ``params_hash`` is computed from the ``[thresholds]``,
``[league]`` and ``[economy]`` sections **only** (AD-3), and ``tool_versions``
is empty, because the computation is pure domain code. Changing a threshold
therefore invalidates this stage but not the parsing: the result is ready in
seconds, because the demo is not read.

``[economy]`` came along in Story 1.10. The half-buy's condition B asks whether
a player can make a normal buy on the next round, and the answer depends on the
loss bonus (``loss_bonus_steps``). Without the section in the hash, changing a
step would leave the old result in place and it would look up to date.

The stage has **two** inputs. The first is the ``parse`` stage's result: its id
is written into the ``ManifestInput.sha256`` field, but **it is not the file's
hash**; it is the parameter hash computed from the content of the parse
manifest (see :meth:`~pappascout.archive.manifest.Manifest.fingerprint`). The
field is named a hash in the manifest model
because ``parse`` writes the demo's sha256 into it; here the input is another
stage's result, which has no hash of its own, so its identity is computed from
the manifest. The second is the match facts (:func:`match_facts_input`), whose
identity is computed the same way for the same reason: the index files have no
manifest either. The comparison works the same way in all three cases -- the
same value means the same input -- and that, not "the value is a file hash", is
what :class:`~pappascout.archive.manifest.ManifestInput` promises.

An old manifest that names only the parse input therefore does not match, and
the demo is classified once more. That is the migration and it is meant: the
stage does not read the demo, so re-classifying the archive costs seconds per
demo, and the alternative would be to leave results standing whose second input
nobody ever checked.

**With one exception, and it is a consequence of the rule below rather than a
hole in it** (measured 2026-09-23): if an old manifest meets a selection file
that cannot be read, the identity cannot be computed, the expectation falls
back to what the manifest recorded -- which is the parse input alone -- and the
demo skips, keeping its single-input manifest. It is not migrated until the
file can be read. That is the same rule as everywhere else here ("an input that
cannot be read is not evidence that it changed"), and it is not silent: the
skip's reason says the input went unchecked.
"""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import polars as pl

from pappascout.archive.atomic_write import atomic_path, atomic_write_text
from pappascout.archive.manifest import Manifest, ManifestInput, compute_params_hash
from pappascout.archive.paths import (
    ArchivePaths,
    classified,
    classified_manifest,
    classified_round_list,
    parsed_manifest,
    parsed_table,
    safe_component,
)
from pappascout.constants import ROUND_TYPE_FI, UNCLASSIFIED
from pappascout.domain.economy import (
    CLASSIFY_COLUMNS,
    Decision,
    classify_round,
    loss_counts,
    per_player,
)
from pappascout.domain.models import (
    EconomySettings,
    LeagueSettings,
    ThresholdSettings,
)
from pappascout.domain.schemas import CLASSIFIED, ROUNDS, validate
from pappascout.errors import PappascoutError, SchemaError
from pappascout.stages import StageResult
from pappascout.stages.discover import read_teams_index, teams_from_index
from pappascout.stages.select import read_selection

__all__ = [
    "STAGE",
    "TABLE",
    "TOOLS",
    "ROUND_LIST_COLUMNS",
    "MatchFacts",
    "run",
    "resolve_team",
    "team_keys",
    "read_match_facts",
    "match_facts_input",
    "match_facts_input_id",
    "unchecked_facts_note",
    "classify_rounds",
    "round_list_rows",
    "round_list_cells",
    "render_round_list_markdown",
]

STAGE = "classify"
TABLE = "classified"

#: Empty: classification is pure domain computation, and no external library's
#: version changes its result (the manifest module's rule).
TOOLS: tuple[str, ...] = ()


def run(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    archive: ArchivePaths,
    map_demo_id: str,
    team: str | None,
    *,
    economy: EconomySettings,
    force: bool = False,
) -> StageResult:
    """Classify one demo's rounds from one team's point of view.

    Args:
        thresholds: The ``[thresholds]`` section.
        league: The ``[league]`` section.
        economy: The ``[economy]`` section, **as a keyword argument**.
            ``loss_bonus_steps`` and ``max_money`` (the half-buy's condition
            B) are read from it. All three sections are in the parameter hash
            (AD-3), and the stage sees no others. A keyword because three
            pydantic sections in a row would go through positionally with two
            of them swapped without anything remarking on it.
        archive: The archive's paths.
        map_demo_id: The unit's id.
        team: The subject team's lineup key or an unambiguous prefix of it.
            ``None`` produces an error that lists the demo's two lineups.
        force: Skip the manifest match and classify in any case.

    Returns:
        A :class:`~pappascout.stages.StageResult` whose ``stats`` holds the
        number of rounds, the distribution of types and the whole round list as
        rows.

    Raises:
        ~pappascout.errors.PappascoutError: If the demo has not been parsed,
            ``team`` matches neither lineup, or the team index or the selection
            file is broken. A **missing** selection file is not an error: then
            ``is_league`` and ``roster_class`` stay empty. A **broken** one
            stops a run that classifies; it does not stop a run that skips,
            because an input that cannot be read is not evidence that it
            changed (:func:`unchecked_facts_note`).
        ~pappascout.errors.SchemaError: If the rounds table or the result does
            not match the contract, or if the selection file's
            ``roster_class`` is not valid for the ``CLASSIFIED`` schema's enum.
    """
    started = time.perf_counter()
    map_demo_id = safe_component(map_demo_id, "map_demo_id")

    rounds, unnumbered = _read_rounds(archive, map_demo_id)
    parse_manifest = _read_parse_manifest(archive, map_demo_id)
    # **The lineup key, not the canonical ``team_key``.** The archive's
    # directory is named from this (``paths.classified``'s parameter is
    # ``team_key`` for historical reasons), but the value is a ``lineup_key``
    # -- and that is exactly why the selection file is looked up through the
    # team index and not by this name.
    lineup_key = resolve_team(rounds, team, map_demo_id)

    table_rel = classified(lineup_key, map_demo_id)
    list_rel = classified_round_list(lineup_key, map_demo_id)
    manifest_rel = classified_manifest(lineup_key, map_demo_id)
    table_abs = archive.resolve(table_rel)
    list_abs = archive.resolve(list_rel)
    manifest_abs = archive.resolve(manifest_rel)

    # **Read before the skip is decided, and that is the change Story 4.7
    # made.** Until then the index files were read only after the skip branch,
    # which is what let a re-run of ``select`` change the values with nothing
    # noticing. They are the stage's second input, so they have to be in hand
    # before the manifest can be compared. The reading itself never raises: a
    # file that cannot be read is turned into ``reading.error`` and the error
    # is paid where it belongs, on the path that actually uses the values.
    reading = _read_selection_data(archive, lineup_key, map_demo_id)
    facts_input = _facts_input(reading, map_demo_id)

    inputs = [
        # The input's id is from the content of the parse MANIFEST, not from
        # a hash of the rounds table: the table is a derived output, and its
        # identity is precisely what it was derived from. The same definition
        # as in the aggregate stage.
        ManifestInput(
            result_id=parse_manifest.result_id,
            sha256=parse_manifest.fingerprint(),
        )
    ]
    if facts_input is not None:
        inputs.append(facts_input)
    params_hash = _params_hash(thresholds, league, economy)

    existing = Manifest.read_if_exists(manifest_abs)
    # **An input that cannot be read is not evidence that it changed.** When
    # the identity could not be computed, the value the finished manifest
    # recorded is taken as the expectation, so a broken selection file leaves
    # a finished result standing instead of turning the whole archive's
    # classifications into errors -- and the skip says out loud that the input
    # went unchecked. Without this the declaration of the input would have
    # bought the cascade at the price of that regression.
    expected = inputs
    if facts_input is None and existing is not None:
        expected = [
            *inputs,
            *(i for i in existing.inputs if i.result_id == match_facts_input_id()),
        ]

    ready = None
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=expected,
            params_hash=params_hash,
            tool_versions={},
            root=archive.root,
        )
    ):
        ready = _usable_result(table_abs)
    if ready is not None:
        return StageResult(
            stage=STAGE,
            unit=map_demo_id,
            status="ok",
            skipped=True,
            outputs=tuple(PurePosixPath(o) for o in existing.outputs),
            manifest_path=manifest_rel,
            reason=_skip_reason(reading),
            duration_s=time.perf_counter() - started,
            stats=_stats(
                round_list_rows(ready), lineup_key, list_rel, unnumbered
            ),
        )

    # The values themselves, from the same reading. This is where a broken
    # file is raised, and it is why ``facts_input`` is never ``None`` by the
    # time the manifest below is written.
    facts = _facts_of(reading, lineup_key, map_demo_id)

    df, rows = classify_rounds(
        rounds, lineup_key, thresholds, map_demo_id, economy=economy, facts=facts
    )

    with atomic_path(table_abs) as tmp:
        df.write_parquet(tmp)
    atomic_write_text(
        list_abs,
        render_round_list_markdown(
            rows,
            map_demo_id=map_demo_id,
            team_key=lineup_key,
            thresholds=thresholds,
            league=league,
            economy=economy,
        ),
    )

    Manifest.new(
        result_id=str(PurePosixPath("classified") / lineup_key / map_demo_id),
        stage=STAGE,
        params_hash=params_hash,
        inputs=inputs,
        tool_versions={},
        status="ok",
        outputs=(str(table_rel), str(list_rel)),
    ).write(manifest_abs)

    return StageResult(
        stage=STAGE,
        unit=map_demo_id,
        status="ok",
        skipped=False,
        outputs=(table_rel, list_rel),
        manifest_path=manifest_rel,
        # AD-9: the result is ``ok`` even when the two columns stayed empty,
        # but **the reason is not left unsaid**. Without this line a broken
        # bridge would look in the report exactly like a hand-imported demo.
        reason=facts.note,
        duration_s=time.perf_counter() - started,
        stats=_stats(rows, lineup_key, list_rel, unnumbered),
    )


# -- The inputs -----------------------------------------------------------------


def _read_rounds(
    archive: ArchivePaths, map_demo_id: str
) -> tuple[pl.DataFrame, int]:
    """Read and validate the parsed rounds table.

    Unnumbered rounds (``round_no`` empty) are dropped before the
    classification: the loss count is a counter bound to the order of the
    rounds, and it cannot handle an unnumbered row. The number is returned so
    that the run can say so and the row does not disappear silently.

    Returns:
        ``(table, the number of unnumbered rounds dropped)``.

    Raises:
        PappascoutError: If the table is not there, cannot be read, is empty or
            belongs to another demo.
    """
    path = archive.resolve(parsed_table(map_demo_id, "rounds"))
    if not path.is_file():
        raise PappascoutError(
            f"Demo {map_demo_id} has not been parsed yet: the file {path} is "
            "not there.\n"
            f"Run first: uv run pappascout parse {map_demo_id}"
        )
    try:
        df = pl.read_parquet(path)
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise PappascoutError(
            f"The rounds table {path} could not be read: {exc}\n"
            f"Parse it again: uv run pappascout parse {map_demo_id} "
            "--force"
        ) from exc

    # By default validate speaks to the developer ("add the column or fix the
    # stage that produced the table -- the contract is in the file
    # domain/schemas.py"). That is the wrong advice here: the table comes from
    # the archive, an earlier version of the program itself wrote it, and the
    # user does not fix it by editing code. The fix is to parse again, so that
    # is also what the message says. The column's name stays as the diagnosis.
    validate(
        df,
        ROUNDS,
        "rounds",
        advice=(
            "The table was parsed with an older version of the program. Parse "
            f"it again: uv run pappascout parse {map_demo_id} --force"
        ),
    )

    # Otherwise the wrong parquet in the right path would be classified under
    # the wrong id, and the result would look perfectly valid.
    foreign = sorted(
        {str(v) for v in df["map_demo_id"].unique().to_list() if v != map_demo_id}
    )
    if foreign:
        raise PappascoutError(
            f"The rounds table {path} holds rows of another demo "
            f"({', '.join(foreign)}), although it should be the table of demo "
            f"{map_demo_id}.\n"
            f"Remove the directory and parse again: uv run pappascout "
            f"parse {map_demo_id} --force"
        )

    numbered = df.filter(pl.col("round_no").is_not_null())
    unnumbered = int(
        df.filter(pl.col("round_no").is_null())["round_raw"].n_unique()
    )
    if numbered.is_empty():
        raise PappascoutError(
            f"The rounds table {path} has not one numbered round, so there is "
            "nothing to classify.\n"
            f"Parse it again: uv run pappascout parse {map_demo_id} "
            "--force"
        )
    return numbered, unnumbered


def _read_parse_manifest(archive: ArchivePaths, map_demo_id: str) -> Manifest:
    """Read the ``parse`` manifest; it is this stage's only input.

    Raises:
        PappascoutError: If there is no manifest or the parsing did not
            succeed. Nothing is classified on top of a stale or failed parse.
    """
    path = archive.resolve(parsed_manifest(map_demo_id))
    manifest = Manifest.read_if_exists(path)
    if manifest is None:
        raise PappascoutError(
            f"The parse manifest was not found at {path}, so the "
            "classification's input cannot be recognised.\n"
            f"Run first: uv run pappascout parse {map_demo_id}"
        )
    if manifest.status != "ok":
        raise PappascoutError(
            f"The parsing of demo {map_demo_id} is marked with the status "
            f"{manifest.status!r}, so its result is not classified.\n"
            f"Reason: {manifest.reason or 'not recorded'}\n"
            f"Parse it again: uv run pappascout parse {map_demo_id} "
            "--force"
        )
    return manifest


def _params_hash(
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    economy: EconomySettings,
) -> str:
    """AD-3: only these three sections affect the classification's result.

    ``[economy]`` is included whole, although the classification reads only
    ``loss_bonus_steps`` from it. A partial hash would need a list of which of
    the section's fields the rules happen to read -- and that list would go
    stale silently on the first day a rule reads one field more. The price is
    an unnecessary re-run when some other economy value changes; it costs
    seconds, because the demo is not read.
    """
    return compute_params_hash(
        {
            "thresholds": thresholds.model_dump(mode="json"),
            "league": league.model_dump(mode="json"),
            "economy": economy.model_dump(mode="json"),
        }
    )


def team_keys(archive: ArchivePaths, map_demo_id: str) -> list[str]:
    """The demo's lineup keys, so that every team can be classified.

    Read from the rounds table and not from the team index: the id in this
    stage is the lineup key, and both of the demo's lineups are in the rounds
    table whether or not their teams are known.
    """
    rounds, _ = _read_rounds(archive, safe_component(map_demo_id, "map_demo_id"))
    return [str(k["lineup_key"]) for k in _lineups(rounds)]


def resolve_team(df: pl.DataFrame, team: str | None, map_demo_id: str) -> str:
    """Read ``--team`` as one of the demo's lineup keys.

    Accepts both the full ``lineup_key`` and an unambiguous prefix of it -- a
    16-character hash is uncomfortable to type by hand.

    Raises:
        PappascoutError: If the id is missing, matches nothing or matches more
            than one. The message always lists the demo's lineups, so the next
            command can be copied straight from it.
    """
    lineups = _lineups(df)
    if team is None:
        raise PappascoutError(
            "Say with the --team option from which team's point of view demo "
            f"{map_demo_id} is classified.\n{_lineup_listing(lineups)}"
        )

    query = team.strip().lower()
    matches = [k for k in lineups if k["lineup_key"].lower() == query]
    if not matches:
        matches = [k for k in lineups if k["lineup_key"].lower().startswith(query)]
    if len(matches) == 1:
        return safe_component(str(matches[0]["lineup_key"]), "team_key")

    problem = (
        f"The lineup key {team!r} matches more than one lineup."
        if matches
        else (
            f"The lineup key {team!r} matches neither lineup of demo "
            f"{map_demo_id}."
        )
    )
    raise PappascoutError(f"{problem}\n{_lineup_listing(lineups)}")


def _lineups(df: pl.DataFrame) -> list[dict[str, object]]:
    """The demo's lineups with their keys, starting sides and wins."""
    first_round = df["round_no"].min()
    summary = (
        df.group_by("lineup_key")
        .agg(
            pl.col("won").fill_null(False).sum().alias("wins"),
            pl.col("side")
            .filter(pl.col("round_no") == first_round)
            .first()
            .alias("first_side"),
        )
        .sort("lineup_key")
    )
    return [
        {
            "lineup_key": str(r["lineup_key"]),
            "wins": int(r["wins"] or 0),
            "first_side": None if r["first_side"] is None else str(r["first_side"]),
        }
        for r in summary.iter_rows(named=True)
    ]


def _lineup_listing(lineups: list[dict[str, object]]) -> str:
    if not lineups:
        return "The rounds table has not one lineup."
    rows = [
        f"    {k['lineup_key']}  (started on side {k['first_side'] or '?'}, "
        f"won {k['wins']} rounds)"
        for k in lineups
    ]
    example = str(lineups[0]["lineup_key"])[:8]
    return (
        "The demo's lineups are:\n"
        + "\n".join(rows)
        + "\nGive the id in full or its beginning, for example:\n"
        + f"    --team {example}"
    )


# -- Match facts from the selection file ----------------------------------------


class MatchFacts(NamedTuple):
    """The per-demo match facts ``select`` has already computed.

    The default is **empty for both**, and that is an honest state and not a
    missing one: a hand-imported demo has no selection row, and neither value
    can be known then.

    ``note`` says **why** a value is absent. Without it five different
    situations -- the index is absent, the lineup has no owner, the owner has
    no selection file, there is no row for the demo, the rows disagree -- would
    look exactly the same in the report, and a broken bridge would be
    indistinguishable from a hand-imported demo. ``run`` carries it into
    ``StageResult.reason`` (AD-9).
    """

    #: Whether the match is in the settings' championships. ``None`` = not
    #: known.
    is_league: bool | None = None
    #: The roster threshold's class, a value of the ``CLASSIFIED`` schema's
    #: enum.
    roster_class: str | None = None
    #: The reason for an incomplete result, or ``None`` when both were
    #: obtained.
    note: str | None = None


def roster_classes() -> tuple[str, ...]:
    """The valid roster classes **from the ``CLASSIFIED`` schema's enum**.

    The list is derived from the contract the value is finally written against,
    and not from a parallel constant
    (:data:`~pappascout.constants.ROSTER_CLASSES`). Two sources could drift
    apart, and then the check and the error message would speak of a different
    set than Polars: the message would say "not valid for the schema" of a
    value that is valid -- or let through a value that breaks the write three
    lines later.
    """
    return tuple(str(value) for value in CLASSIFIED["roster_class"].categories)


def read_match_facts(
    archive: ArchivePaths, lineup_key: str, map_demo_id: str
) -> MatchFacts:
    """Look up the demo's ``is_league`` and ``roster_class`` in the selection file.

    The values are **not computed here**: ``select`` is their only computer,
    and this is a reader. The route has two steps because there are two ids:
    this stage's directory name is the lineup key, whereas the selection file
    is named by the canonical ``team_key``. The bridge is
    ``index/teams.json``'s ``lineup_keys``, and the owners are inferred by
    reading that field -- it is the only place the translation is made.

    **Consensus is field by field and not record by record.** A lineup can be
    owned by more than one team, and the same file can hold two rows for the
    same demo; all the hits are read and each field is settled separately. The
    difference matters: ``is_league`` describes the **match** (AD-10), so all
    the hits have to agree about it, whereas ``roster_class`` is judged against
    **that team's** standing roster (AD-6), so two owners get different values
    for it in the perfectly normal course of things. A record-level comparison
    would throw away an ``is_league`` everyone agrees on merely because the
    classes differed.

    When a field is disagreed on, it stays **empty** and the reason is recorded
    in ``note``: a dispute is not settled by drawing lots, and "the first one
    wins" would be exactly that draw.

    Args:
        archive: The archive's paths.
        lineup_key: The subject's **lineup key**, the same one the result is
            written under. Not the canonical ``team_key``.
        map_demo_id: The id of the demo being handled.

    Returns:
        A :class:`MatchFacts`. When a value is absent, ``note`` names the
        reason.

    Raises:
        ~pappascout.errors.PappascoutError: If the team index or the selection
            file exists but is broken. A **missing** file is not an error but
            an unknown value.
        ~pappascout.errors.SchemaError: If the row's ``is_league`` or
            ``roster_class`` is not valid for the ``CLASSIFIED`` schema.
    """
    return _facts_of(
        _read_selection_data(archive, lineup_key, map_demo_id),
        lineup_key,
        map_demo_id,
    )


class _SelectionReading(NamedTuple):
    """What the two index files hold about **this** demo, read once.

    One reading serves both purposes, and that is the point of the type: the
    manifest input (:func:`match_facts_input`) and the values themselves
    (:func:`_facts_of`) are two views of the same bytes. Reading twice would
    let the identity a result was skipped on differ from the values a result
    was written from -- two copies of the same observation, which is what the
    ``lineup_keys`` bridge already refuses elsewhere.

    Attributes:
        index_present: Whether ``index/teams.json`` is in the archive at all.
        owners: The teams that own this lineup key, in the index's order.
        hits: ``(team_key, row)`` for every selection row of this demo, from
            every owner's file. **The rows as they were read**, not the values
            picked off them: the picking checks them against the schema and
            may raise, and a reading must not.
        missing_file: The owners that have no selection file at all.
        error: The fault that stopped the reading, or ``None``. Carried rather
            than raised, because whether it is an error depends on what the
            caller does next -- see :func:`unchecked_facts_note`.
    """

    index_present: bool
    owners: tuple[str, ...] = ()
    hits: tuple[tuple[str, dict[str, Any]], ...] = ()
    missing_file: tuple[str, ...] = ()
    error: PappascoutError | None = None


def _read_selection_data(
    archive: ArchivePaths, lineup_key: str, map_demo_id: str
) -> _SelectionReading:
    """Read the owners and this demo's selection rows. **It never raises.**

    The reading happens before the skip is decided (Story 4.7), so a fault in
    it must not be the same thing as a fault in the run: the archive holds
    hundreds of finished classifications that do not care that one team's
    selection file was cut off mid-sync. The fault is therefore carried on the
    result and raised by :func:`_facts_of`, which is the caller that really
    needs the values.

    **``UnicodeDecodeError`` is caught beside the readers' own error, and it
    has to be.** Both readers decode with ``read_text(encoding="utf-8")`` and
    neither catches it: it is a ``ValueError``, not an ``OSError``, and
    ``json.JSONDecodeError`` does not cover it. A file of valid bytes that is
    not valid UTF-8 -- a half-synced write, a foreign code page -- therefore
    came out of them raw. It reached this stage before Story 4.7 as well,
    through the staleness note on the skip path, so this is not a fault the
    declaration introduced; but "it never raises" is now a load-bearing claim
    and a claim has to be true. It is turned into the reader's own kind of
    error, with the reader's own advice, so that the run that really needs the
    values fails with a sentence rather than with a traceback.
    """
    if not archive.teams_index().is_file():
        return _SelectionReading(index_present=False)
    try:
        owners = tuple(_owners(archive, lineup_key))
        hits: list[tuple[str, dict[str, Any]]] = []
        missing_file: list[str] = []
        for team_key in owners:
            if not archive.selection(team_key).is_file():
                missing_file.append(team_key)
                continue
            for row in _selection_rows(archive, team_key, map_demo_id):
                hits.append((team_key, row))
    except PappascoutError as exc:
        return _SelectionReading(index_present=True, error=exc)
    except UnicodeDecodeError as exc:
        return _SelectionReading(
            index_present=True,
            error=PappascoutError(
                "A file of the archive's index is not valid UTF-8 text, so "
                "the match's kind and roster class could not be read "
                f"({exc}).\n"
                "The file has most likely been left half written. Run again: "
                "uv run pappascout discover, and after it "
                "uv run pappascout select."
            ),
        )
    return _SelectionReading(
        index_present=True,
        owners=owners,
        hits=tuple(hits),
        missing_file=tuple(missing_file),
    )


def _facts_of(
    reading: _SelectionReading, lineup_key: str, map_demo_id: str
) -> MatchFacts:
    """The two values from a reading, with the reason when one is absent.

    Raises:
        ~pappascout.errors.PappascoutError: The fault the reading carried, if
            any. It is raised **here** and not where it was met: a caller that
            only needs the input's identity does not need the values, and a
            broken file must not take a finished result down with it.
        ~pappascout.errors.SchemaError: If a row's ``is_league`` or
            ``roster_class`` is not valid for the ``CLASSIFIED`` schema.
    """
    if reading.error is not None:
        raise reading.error
    if not reading.index_present:
        return MatchFacts(
            note=(
                "There is no team index, so the match's kind and roster class "
                "could not be read (is_league and roster_class stayed "
                "empty).\n"
                "If you like, run first: uv run pappascout discover"
            )
        )

    owners = list(reading.owners)
    if not owners:
        return MatchFacts(
            note=(
                f"The lineup {lineup_key} is owned by no team in the team "
                "index, so the selection file could not be located "
                "(is_league and roster_class stayed empty). On a "
                "hand-imported demo this is expected."
            )
        )

    hits = [
        (team_key, _match_facts(row, team_key, map_demo_id))
        for team_key, row in reading.hits
    ]
    missing_file = list(reading.missing_file)

    if not hits:
        if len(missing_file) == len(owners):
            return MatchFacts(
                note=(
                    "Team "
                    f"{', '.join(missing_file)} has no selection file, so "
                    "is_league and roster_class stayed empty.\n"
                    "Run first: uv run pappascout select --team "
                    f'"{missing_file[0]}"'
                )
            )
        return MatchFacts(
            note=(
                f"Demo {map_demo_id} is not in the selection file of team "
                f"{', '.join(k for k in owners if k not in missing_file)}, so "
                "is_league and roster_class stayed empty. On a hand-imported "
                "demo this is expected."
            )
        )

    is_league, league_note = _consensus(
        [(team_key, facts.is_league) for team_key, facts in hits],
        field="is_league",
        why=(
            "is_league describes the match and not the team, so two different "
            "values cannot both be right"
        ),
    )
    roster_class, class_note = _consensus(
        [(team_key, facts.roster_class) for team_key, facts in hits],
        field="roster_class",
        why=(
            "the roster class is judged against the team's own standing "
            "roster, so different values are expected and one of them cannot "
            "be chosen"
        ),
    )
    notes = [note for note in (league_note, class_note) if note]
    return MatchFacts(
        is_league=is_league,
        roster_class=roster_class,
        note=" ".join(notes) if notes else None,
    )


# -- The match facts as a declared input (Story 4.7) ----------------------------


def match_facts_input_id() -> str:
    """The id under which the match facts are declared in the manifest.

    It names **what is read**, not a file: the values come out of two files and
    out of neither of them whole. ``parse`` sets the precedent with
    ``demo/<map_demo_id>`` for an input that is not another stage's result.

    **The id is a name; the digest is a value, and they answer different
    questions.** The name has to be stable enough that
    :func:`run` can find last time's value by it, and no more specific than
    that: the manifest it lives in is already the manifest of one lineup key
    and one demo, so putting either into the name would repeat what the file's
    own path says, and a lookup by a name that varies is a lookup that can
    miss. The digest, on the other hand, **does** carry the demo id -- see
    :func:`_facts_input` -- because a value's job is to say what was read, and
    there the demo id is part of what was read rather than part of the
    address. Neither is a copy of the other.

    It is a literal and not a format string, and
    ``test_the_input_is_named_in_the_manifest_beside_the_parse_result`` pins
    the literal rather than this function's own return value. An id that
    drifted into the ``parsed/`` namespace would be found by :func:`run`'s
    fallback filter as though it were the parse input.
    """
    return "match-facts"


def match_facts_input(
    archive: ArchivePaths, lineup_key: str, map_demo_id: str
) -> ManifestInput | None:
    """This demo's match facts as a manifest input, or ``None`` if unreadable.

    The identity is **narrow on purpose**, and the reasoning is in
    :class:`~pappascout.archive.manifest.ManifestInput`: both index files carry
    a wall-clock ``generated_at`` that ``discover`` rewrites on every run, so a
    digest of either file as a whole would re-classify the archive after every
    ``discover`` and cascade into ``aggregate``.

    **The rule is what the stage reads into the result, not what it reads into
    the reason.** The value is computed from this demo's ``is_league`` and
    ``roster_class``, one entry per selection row that was found, and from
    nothing else. Everything else the reading collects -- who owns the lineup,
    whether the index is there at all, which owners have no selection file --
    steers only :attr:`MatchFacts.note`, which is a sentence about the run and
    is written into no file. Digesting it would invalidate finished results
    that would be recomputed byte for byte: a second team crossing the roster
    threshold, with no selection file of its own, changes the owners and
    changes nothing in the table.

    The cost of that rule, stated so it is not discovered later: a skipped
    run's reason can name a state that has since changed -- an owner that has
    come or gone without bringing a value with it. That is acceptable because
    the reason is recomputed on every run that classifies, is persisted
    nowhere, and is read by nothing downstream; the moment such a change
    brings a **value** with it, the digest moves and the stage runs.

    What follows, and is the property worth guarding: a change elsewhere in
    either index -- another demo's row, another team's roster, an owner with
    nothing to say, the timestamp -- does **not** re-run this stage, and a
    change to this demo's two values, **from any owner**, does.

    Returns:
        The input, or ``None`` when the files are there but cannot be read.
        ``None`` is not "no input": it is "the input could not be identified",
        and :func:`run` treats it as no evidence of a change rather than as
        evidence of one.
    """
    return _facts_input(
        _read_selection_data(archive, lineup_key, map_demo_id), map_demo_id
    )


def _facts_input(
    reading: _SelectionReading, map_demo_id: str
) -> ManifestInput | None:
    """:func:`match_facts_input` over a reading that has already been made."""
    if reading.error is not None:
        return None
    return ManifestInput(
        result_id=match_facts_input_id(),
        sha256=compute_params_hash(
            {
                # The demo's id is in the digest so that the value says which
                # demo it identifies and not only what was read.
                "map_demo_id": map_demo_id,
                # **Every hit, and only the two fields.** Every hit, because
                # ``_facts_of`` runs them all through ``_consensus``: with two
                # owners, a change to the second one's ``is_league`` turns the
                # agreed value into an empty one, so a digest of the first hit
                # alone would leave a table standing that is now wrong. Only
                # the two fields, because the rest of a selection row --
                # ``roster_ok``, the player lists, the match id -- belongs to
                # other stages, and a field this stage does not read must not
                # re-run it.
                #
                # ``key=str`` makes the order of the hits irrelevant, which is
                # what ``_consensus`` already does with the same key: the
                # owners are read in the index's order, and that order is not
                # something this stage reads. A plain sort would compare
                # ``None`` with a string and raise.
                #
                # Neither the owners nor ``missing_file`` is here, and the
                # docstring above says why: they reach the note and never the
                # table. The manifest identifies the result, not the sentence
                # the run printed about it.
                "rows": sorted(
                    [
                        [team_key, row.get("is_league"), row.get("roster_class")]
                        for team_key, row in reading.hits
                    ],
                    key=str,
                ),
            }
        ),
    )


def _owners(archive: ArchivePaths, lineup_key: str) -> list[str]:
    """The team index's teams that own this lineup.

    The translation is made **here and only here**, so that every reader gets
    the same answer. Ownership is read from the ``lineup_keys`` field, because
    that is the field ``discover`` writes for every team
    (``domain.teams.assign_lineup_keys``). The index's
    ``contested_lineup_keys`` list is not needed and would not be read: it is
    ``discover``'s report of the same observation, and instead of two sources
    the one that says **who** the owners are is asked.
    """
    return [
        team.team_key
        for team in teams_from_index(read_teams_index(archive))
        if lineup_key in team.lineup_keys
    ]


def _consensus(
    values: list[tuple[str, object]], *, field: str, why: str
) -> tuple[Any, str | None]:
    """One value if all the hits agree -- otherwise empty, and the reason.

    Args:
        values: ``(team_key, value)`` from every selection row that was found.
        field: The field's name for the error message.
        why: Why a disagreement in this particular field is what it is.

    Returns:
        ``(value, note)``. The note is ``None`` when the value was obtained.
    """
    distinct = {value for _, value in values}
    if len(distinct) == 1:
        return next(iter(distinct)), None
    listed = ", ".join(
        f"{team_key}: {value!r}" for team_key, value in sorted(values, key=str)
    )
    return None, (
        f"The selection rows hold {len(distinct)} different values in the "
        f"field {field} ({listed}), so it stayed empty -- {why}."
    )


def _selection_rows(
    archive: ArchivePaths, team_key: str, map_demo_id: str
) -> list[dict[str, Any]]:
    """**All** the demo's rows from the team's selection file.

    All and not the first: a duplicated row would otherwise be settled by a
    silent "the first one wins" rule, and that is the same draw that was
    expressly forbidden for contested lineups. A duplicate goes into the same
    consensus as two owners.

    Raises:
        PappascoutError: If the file cannot be read or its shape is unknown
            (:func:`~pappascout.stages.select.read_selection`), or if
            ``selections`` is not a list. The same message and the same advice
            as in ``fetch``: run ``select`` again.
    """
    document = read_selection(archive, team_key)
    rows = document.get("selections")
    if not isinstance(rows, list):
        raise PappascoutError(
            f"The selection file of team {team_key} has no "
            "selections list.\n"
            f'Run it again: uv run pappascout select --team "{team_key}"'
        )
    return [
        row
        for row in rows
        if isinstance(row, dict) and row.get("map_demo_id") == map_demo_id
    ]


def _match_facts(
    row: dict[str, Any], team_key: str, map_demo_id: str
) -> MatchFacts:
    """Pick the two fields off a selection row and check that they are valid.

    The check is here and not left to Polars' typing: a foreign value would
    otherwise fail inside ``pl.Enum`` with a message that does not say which
    file it came from. ``roster_ok`` **has no effect**: rejection is the
    sample's business, not that of these two facts about the match.

    Raises:
        SchemaError: If a value is not valid for the ``CLASSIFIED`` schema.
    """
    is_league = row.get("is_league")
    roster_class = row.get("roster_class")
    if is_league is not None and not isinstance(is_league, bool):
        raise SchemaError(
            f"The selection file of team {team_key}, on row {map_demo_id}, "
            f"has the is_league value {is_league!r}, which is not a boolean.\n"
            f'Run it again: uv run pappascout select --team "{team_key}"'
        )
    allowed = roster_classes()
    if roster_class is not None and roster_class not in allowed:
        raise SchemaError(
            f"The selection file of team {team_key}, on row {map_demo_id}, "
            f"has the roster_class value {roster_class!r}, which is not a "
            f"class of the CLASSIFIED schema ({', '.join(allowed)}).\n"
            f'Run it again: uv run pappascout select --team "{team_key}"'
        )
    return MatchFacts(is_league=is_league, roster_class=roster_class)


#: The reason for the skip, without the note about an unchecked input.
SKIP_REASON = (
    "The result is up to date: the manifest matches and the rounds do not "
    "need to be classified again."
)


def _skip_reason(reading: _SelectionReading) -> str:
    note = unchecked_facts_note(reading)
    return SKIP_REASON if note is None else f"{SKIP_REASON} {note}"


def unchecked_facts_note(reading: _SelectionReading) -> str | None:
    """A warning when the match-facts input could not be checked.

    **This replaces the staleness warning of Stories 3.x, and it replaces it
    because the old one became false** (Story 4.7). Until then the selection
    file was not an input, so a file newer than the classification really did
    mean the table might carry an old value, and the timestamp was the only
    evidence available. Now the input is declared and narrow: a file written
    again with the same two values is not a change, and a warning that fired
    on its timestamp would fire after every single ``select`` run and say
    nothing true.

    What is left is the one case the declaration cannot cover. When the index
    or the selection file is there but cannot be read, the identity cannot be
    computed, the skip stands on the value the manifest recorded, and nobody
    has checked whether the file still says it. That is worth a sentence,
    because the alternative -- failing -- would turn one corrupt file into an
    error on every finished classification in the archive.

    Returns:
        The warning with the reader's own fault in it, or ``None`` when the
        input was checked.
    """
    if reading.error is None:
        return None
    return (
        "Note: the match facts could not be read, so it could not be checked "
        "whether is_league and roster_class are still what the selection file "
        f"says ({reading.error}).\n"
        "The result stands as it is. Fix the file and run again, with the "
        "flag --force if you want the values re-read in any case."
    )


# -- Classification --------------------------------------------------------------


def classify_rounds(
    rounds: pl.DataFrame,
    team_key: str,
    thresholds: ThresholdSettings,
    map_demo_id: str,
    *,
    economy: EconomySettings,
    facts: MatchFacts,
) -> tuple[pl.DataFrame, list[dict[str, object]]]:
    """Build the ``CLASSIFIED`` table and the round list's rows from the rounds table.

    Public, because this is the stage's whole reasoning without files: it can
    be run directly both on a hand-built table and on a real demo's rounds
    table without an archive.

    Each team is classified with its own loss counts and its own round history;
    the subject's row gets the opponent's type in the ``opp_round_type``
    column.

    Args:
        facts: The per-demo match facts :func:`read_match_facts` read from the
            selection file. **They are set as they are onto every row** and are
            not computed here; ``is_league`` describes the match and not the
            round, and ``domain.aggregate`` stops the run if one demo's rounds
            were to hold two different values.

            **Required and not empty by default.** A default would make
            forgetting silent: a caller that gives no facts would produce
            exactly the empty column this code exists to fix. A caller without
            a file gives ``MatchFacts()`` and thereby says out loud that it
            does not know the values.
    """
    subject = rounds.filter(pl.col("lineup_key") == team_key).sort("round_no")
    opponent = rounds.filter(pl.col("lineup_key") != team_key).sort("round_no")

    others = int(opponent["lineup_key"].n_unique())
    if others != 1:
        found = sorted({str(k) for k in rounds["lineup_key"].unique().to_list()})
        raise SchemaError(
            f"The rounds table has {len(found)} lineups "
            f"({', '.join(found)}), so the opponent cannot be recognised "
            "unambiguously. The rounds table must have exactly two lineups."
        )
    if subject["round_no"].to_list() != opponent["round_no"].to_list():
        raise SchemaError(
            "The teams' round numbers do not match each other, so the "
            "opponent's round type cannot be joined to the right row. "
            "The rounds table must have exactly two rows per round."
        )

    subject_decisions, subject_loss = _classify_team(
        subject, thresholds, economy=economy
    )
    opponent_decisions, _ = _classify_team(opponent, thresholds, economy=economy)

    table: list[dict[str, object]] = []
    for index, row in enumerate(subject.iter_rows(named=True)):
        decision = subject_decisions[index]
        table.append(
            {
                "map_demo_id": map_demo_id,
                "round_no": row["round_no"],
                "side": row["side"],
                "won": row["won"],
                "round_type": decision.round_type,
                "opp_round_type": opponent_decisions[index].round_type,
                "loss_count": subject_loss[index],
                "reason": decision.reason,
                "inputs": decision.inputs,
                # Read, not computed: the value comes from ``select``'s
                # selection file and is per demo, so the same value goes onto
                # every row. Empty when there was no row.
                "is_league": facts.is_league,
                "roster_class": facts.roster_class,
            }
        )

    df = pl.DataFrame(table, schema=dict(CLASSIFIED))
    validate(df, CLASSIFIED, TABLE)
    # The same function as on a skipped run: the round list has only one path.
    return df, round_list_rows(df)


def _classify_team(
    team_rounds: pl.DataFrame,
    thresholds: ThresholdSettings,
    *,
    economy: EconomySettings,
) -> tuple[list[Decision], list[int]]:
    """Classify all of one team's rounds in order.

    Also returns the loss counts, so that they are not computed twice for the
    same team -- two computations could drift apart from each other.

    **Exactly** ``domain.economy.CLASSIFY_COLUMNS`` is picked off the rows, and
    that is deliberate: the contract about what the classification reads is
    then in the code and not in a comment. Dropping a column from the list
    drops it from the decision as well, so the list cannot go stale silently.
    """
    counters = loss_counts(team_rounds, thresholds)
    rows = team_rounds.select(list(CLASSIFY_COLUMNS)).to_dicts()
    decisions = [
        classify_round(
            row,
            rows[index - 1] if index > 0 else None,
            thresholds,
            economy=economy,
            loss_count=counters[index],
        )
        for index, row in enumerate(rows)
    ]
    return decisions, counters


# -- The round list ----------------------------------------------------------------

#: The round list's columns: ``(heading, key)``. Both the console and the
#: Markdown are built from this, so that they cannot present different columns.
#:
#: **The deciding figures are in the table, not only in the prose.** The bonus
#: and the two player counters (``Armed``, ``Can-buy``) are the ones the class
#: after a loss is settled by; without them the reader would see in the table
#: only the ``Left`` column, which is **the team's mean** and settles nothing.
#: The mean is there all the same, because it says what the team's overall
#: situation is -- the heading says which is which.
ROUND_LIST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("Round", "round_no"),
    ("Side", "side"),
    ("Result", "won"),
    ("Type", "round_type"),
    ("Opp.", "opp_round_type"),
    ("Available", "money_available_per_player"),
    ("Left", "money_per_player"),
    ("Bought", "spent_per_player"),
    ("Equipment", "equip_per_player"),
    ("Loss", "loss_count"),
    ("Bonus", "loss_bonus_if_lost"),
    ("Armed", "armed_of_players"),
    ("Can-buy", "can_buy_of_players"),
    ("Reason", "reason"),
)

_RESULT_WORDS: dict[bool | None, str] = {True: "win", False: "loss", None: "-"}


def _counter(value: object, players: int) -> str | None:
    """A player counter in the form ``"4/5"``, or ``None`` if there is no figure.

    The denominator is the same divisor as in the per-player values, so all the
    row's figures speak of the same set.
    """
    if value is None or not players:
        return None
    return f"{int(value)}/{players}"


def round_list_rows(df: pl.DataFrame) -> list[dict[str, object]]:
    """Build the round list's rows from the finished ``CLASSIFIED`` table.

    The only path to the round list -- a fresh run and a skipped one both call
    this. The per-player values are computed from the ``inputs`` structure with
    the same rounding as in the reason (``domain.economy.per_player``).
    """
    rows: list[dict[str, object]] = []
    for r in df.sort("round_no").iter_rows(named=True):
        inputs = r["inputs"] or {}
        players = int(inputs.get("players") or 0)
        money = inputs.get("money_buy_end")
        spent = inputs.get("money_spent")
        equip = inputs.get("equip_buy_end")
        equip_start = inputs.get("equip_round_start")
        rows.append(
            {
                "round_no": int(r["round_no"]),
                "side": str(r["side"]),
                "won": None if r["won"] is None else bool(r["won"]),
                "round_type": None if r["round_type"] is None else str(r["round_type"]),
                "opp_round_type": (
                    None if r["opp_round_type"] is None else str(r["opp_round_type"])
                ),
                "loss_count": r["loss_count"],
                "money_per_player": per_player(money, players),
                "money_available_per_player": per_player(
                    None if money is None and spent is None
                    else int(money or 0) + int(spent or 0),
                    players,
                ),
                "spent_per_player": per_player(
                    None if equip is None or equip_start is None
                    else int(equip) - int(equip_start),
                    players,
                ),
                "equip_per_player": per_player(equip, players),
                "players": players or None,
                # The player counters are shown in the form "4/5": the figure
                # 4 on its own does not say whether the team was at full
                # strength -- and that is exactly what settles what the
                # threshold was compared against.
                "loss_bonus_if_lost": inputs.get("loss_bonus_if_lost"),
                "armed_of_players": _counter(
                    inputs.get("players_armed"), players
                ),
                "can_buy_of_players": _counter(
                    inputs.get("players_can_buy"), players
                ),
                "reason": r["reason"],
            }
        )
    return rows


def round_list_cells(row: dict[str, object]) -> tuple[str, ...]:
    """One row's cells in :data:`ROUND_LIST_COLUMNS` order."""
    cells: list[str] = []
    for _, key in ROUND_LIST_COLUMNS:
        value = row.get(key)
        if key == "won":
            cells.append(_RESULT_WORDS[None if value is None else bool(value)])
        elif key == "reason":
            cells.append(str(value or ""))
        elif key in ("round_type", "opp_round_type"):
            cells.append(str(value) if value else UNCLASSIFIED)
        else:
            cells.append("-" if value is None else str(value))
    return tuple(cells)


def render_round_list_markdown(
    rows: list[dict[str, object]],
    *,
    map_demo_id: str,
    team_key: str,
    thresholds: ThresholdSettings,
    league: LeagueSettings,
    economy: EconomySettings,
) -> str:
    """Write the round list as Markdown, so it can be read beside the demo.

    The thresholds that were used go into the heading: without them the list
    does not say what the decisions were made against, and a calibration round
    would not be traceable.

    The output is **repeatable**: the same inputs produce byte for byte the
    same text. The moment of the run is not here but in the manifest's
    ``created_at`` field -- otherwise the file would change on every run and
    the difference could not be looked at.
    """
    parts: list[str] = []
    parts.append(f"# Round list -- {map_demo_id}")
    parts.append("")
    parts.append(f"- Team (lineup key): `{team_key}`")
    parts.append(f"- Rounds: {len(rows)}")
    parts.append(
        f"- League format: MR{league.mr}, regulation rounds "
        f"{thresholds.regulation_rounds}, pistol rounds "
        f"{', '.join(str(r) for r in thresholds.pistol_rounds)}, overtime "
        f"starting money {league.ot_start_money} $"
    )
    parts.append(
        f"- Thresholds ($/player): a full buy is at least "
        f"{thresholds.full_equip_min}, a low equipment value after a win at "
        f"most {thresholds.anomaly_equip_max_after_win}; after a loss a buy "
        f"needs at least {thresholds.force_buy_min} bought, otherwise eco"
    )
    parts.append(
        f"- The half-buy's two conditions, **both** must hold: A) at "
        f"least {thresholds.armed_players_min} players armed "
        f"(tells it from eco) and B) at least "
        f"{thresholds.normal_buy_players_min} players whose **own** "
        f"balance + loss bonus is at least "
        f"{thresholds.normal_buy_money_min} $ (tells it from force)"
    )
    parts.append(
        "- Condition B is computed from the **per-player money "
        "distribution** and not from the mean: the mean hides the "
        "distribution and can land on a value nobody can hold. The loss bonus "
        "is the loss count's step (the steps are "
        + ", ".join(str(s) for s in economy.loss_bonus_steps)
        + f" $), and the sum is cut off at the money ceiling {economy.max_money} $. "
        "On the last round of a half condition B is not computed at all: "
        "the money does not carry over to the pistol round or to overtime, so "
        "none of it has been left in reserve."
    )
    parts.append(
        f"- Loss count: the start of a half is {thresholds.loss_count_half_start}, "
        f"the limits are {thresholds.loss_count_min}-{thresholds.loss_count_max}"
    )
    parts.append("")
    parts.append(
        "**Armed** and **Can-buy** are player counters, and the class after a "
        "lost round is settled by them. **Bonus** is the loss bonus the "
        "can-buy count was computed with; empty means condition B is not "
        "computed on this round (overtime or the last round of a half). "
        "**Left**, by contrast, is the team's mean and settles nothing -- "
        "the per-player balances are in the reason."
    )
    parts.append("")
    parts.append(
        "Every money figure is dollars per player at the end of the buy time "
        "(the end of freezetime + [parse].buy_window_seconds, cut off at the "
        "round's first death). "
        "**Available** = left + spent, that is, the money the team had during "
        "the buy time. **Left** is the balance after the buys, so on a "
        "saving round it is large. **Bought** is the growth of the equipment "
        "value from the start of the round to the end of the buy time."
    )
    parts.append("")

    headers = [o for o, _ in ROUND_LIST_COLUMNS]
    parts.append("| " + " | ".join(headers) + " |")
    parts.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in rows:
        parts.append("| " + " | ".join(_md(s) for s in round_list_cells(row)) + " |")

    parts.append("")
    parts.append("## Round types")
    parts.append("")
    for value, label_fi in ROUND_TYPE_FI.items():
        parts.append(f"- `{value}` -- {label_fi}")
    parts.append(f"- `{UNCLASSIFIED}` -- the observation was missing, not classified")
    parts.append("")
    parts.append(
        "`is_league` and `roster_class` stay empty in this stage: they come "
        "from the team index, which comes into being only in Epic 3."
    )
    parts.append("")
    return "\n".join(parts)


def _md(text: str) -> str:
    """Escape a cell's content for a Markdown table.

    A pipe would break the cell and a newline the whole table; a backtick would
    start a code span that would eat the rest of the line. All three come from
    the reasons, which are free text.
    """
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("`", "\\`")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


# -- The figures ------------------------------------------------------------------


def _stats(
    rows: list[dict[str, object]],
    team_key: str,
    list_rel: PurePosixPath,
    unnumbered: int,
) -> dict[str, object]:
    """The figures shown to the user.

    ``by_type`` holds the real round types only; the unclassified ones are a
    figure of their own, so that they are not shown twice.
    """
    distribution: dict[str, int] = {}
    unclassified = 0
    for row in rows:
        round_type = row["round_type"]
        if round_type is None:
            unclassified += 1
            continue
        key = str(round_type)
        distribution[key] = distribution.get(key, 0) + 1
    return {
        "team_key": team_key,
        "rounds": len(rows),
        "by_type": distribution,
        "unclassified": unclassified,
        "unnumbered": unnumbered,
        "round_list": str(list_rel),
        "rows": rows,
    }


def _usable_result(table_abs: Path) -> pl.DataFrame | None:
    """The finished result, if it can be read **and** still matches the contract.

    A matching manifest is not enough on its own. The result table's schema can
    change without the manifest's content changing -- when the ``CLASSIFIED``
    contract gains a new field, for example -- and then an old result would
    look up to date but the new values would be missing from it.
    Classification is cheap (the demo is not read), so an invalid result is
    computed again rather than reported incomplete.

    Returns:
        The table, or ``None`` if it is unreadable or against the contract --
        in either case the stage is run again.
    """
    try:
        df = pl.read_parquet(table_abs)
    except (OSError, pl.exceptions.PolarsError):
        return None
    try:
        return validate(df, CLASSIFIED, TABLE)
    except SchemaError:
        return None
