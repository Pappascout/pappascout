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

**The connection is not in the manifest's input, and staleness is said out
loud.** The selection file is **not** in this stage's manifest ``inputs``: one
new ``select`` run would otherwise force the whole archive to be classified
again. The price is that a finished table can carry an old value, so a skipped
run **warns** when the selection file was written after this classification
(:func:`selection_staleness_note`), and advises ``--force``. A documented rule
alone would rest on a human's memory; the warning makes a silently wrong figure
visible.

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

The input is the ``parse`` stage's result. Its id is written into the
``ManifestInput.sha256`` field, but **it is not the file's hash**; it is the
parameter hash computed from the content of the parse manifest (see
:meth:`~pappascout.archive.manifest.Manifest.fingerprint`). The field is named
a hash in the manifest model
because ``parse`` writes the demo's sha256 into it; in this stage the input is
another stage's result, which has no hash of its own, so its identity is
computed from the manifest. The comparison works the same way in either case:
the same value means the same input.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
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
    "selection_staleness_note",
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
            ``is_league`` and ``roster_class`` stay empty.
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
    params_hash = _params_hash(thresholds, league, economy)

    existing = Manifest.read_if_exists(manifest_abs)
    ready = None
    if (
        not force
        and existing is not None
        and existing.is_current(
            inputs=inputs,
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
            reason=_skip_reason(archive, lineup_key, existing),
            duration_s=time.perf_counter() - started,
            stats=_stats(
                round_list_rows(ready), lineup_key, list_rel, unnumbered
            ),
        )

    # **Read only here, after the skip branch.** A skipped run does not read
    # the values at all, so one broken selection file does not turn the whole
    # archive's finished classifications into errors. Moving this to the top of
    # the function would be exactly that regression;
    # ``test_a_broken_selection_file_does_not_break_a_skipped_run``
    # pins the order.
    facts = read_match_facts(archive, lineup_key, map_demo_id)

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
    if not archive.teams_index().is_file():
        return MatchFacts(
            note=(
                "There is no team index, so the match's kind and roster class "
                "could not be read (is_league and roster_class stayed "
                "empty).\n"
                "If you like, run first: uv run pappascout discover"
            )
        )

    owners = _owners(archive, lineup_key)
    if not owners:
        return MatchFacts(
            note=(
                f"The lineup {lineup_key} is owned by no team in the team "
                "index, so the selection file could not be located "
                "(is_league and roster_class stayed empty). On a "
                "hand-imported demo this is expected."
            )
        )

    hits: list[tuple[str, MatchFacts]] = []
    missing_file: list[str] = []
    for team_key in owners:
        if not archive.selection(team_key).is_file():
            missing_file.append(team_key)
            continue
        for row in _selection_rows(archive, team_key, map_demo_id):
            hits.append((team_key, _match_facts(row, team_key, map_demo_id)))

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


#: The reason for the skip, without the staleness warning.
SKIP_REASON = (
    "The result is up to date: the manifest matches and the rounds do not "
    "need to be classified again."
)


def _skip_reason(
    archive: ArchivePaths, lineup_key: str, manifest: Manifest
) -> str:
    note = selection_staleness_note(archive, lineup_key, manifest)
    return SKIP_REASON if note is None else f"{SKIP_REASON} {note}"


def selection_staleness_note(
    archive: ArchivePaths, lineup_key: str, manifest: Manifest
) -> str | None:
    """A warning if the selection file is newer than the finished classification.

    The selection file is **not** a manifest input, so changing it does not
    invalidate the result -- and so it does not say anything about itself
    either. Without this, staleness would be documented but observable from
    nowhere: the table would carry an old ``is_league``, and the report would
    look up to date. The cheapest fix is **to say it in the skip's reason** and
    to advise ``--force``.

    **It never raises.** A skipped run does not read the values at all, so a
    broken selection file must not turn a finished result into an error; an
    unreadable file means here only that nothing can be said about staleness.

    Returns:
        The warning, or ``None`` if the file is older, is not there or its
        timestamp cannot be read.
    """
    newest: datetime | None = None
    whose: str | None = None
    try:
        owners = _owners(archive, lineup_key) if archive.teams_index().is_file() else []
        for team_key in owners:
            if not archive.selection(team_key).is_file():
                continue
            moment = _moment(read_selection(archive, team_key).get("generated_at"))
            if moment is not None and (newest is None or moment > newest):
                newest, whose = moment, team_key
    except PappascoutError:
        return None

    if newest is None or newest <= manifest.created_at:
        return None
    return (
        f"Note: the selection file of team {whose} was written "
        f"{newest.isoformat()}, this classification {manifest.created_at.isoformat()}. "
        "The selection file is not an input of this stage, so is_league and "
        "roster_class may be stale -- run again with the flag "
        "--force if you want them as the file has them."
    )


def _moment(value: object) -> datetime | None:
    """An ISO timestamp with a time zone, or ``None`` if it cannot be read."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


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
