"""Round numbering and the invariants that hold inside a round.

Round numbering is the only real inference in the whole of Story 1.2. Before
the first played round a demo holds warm-up rounds and a knife round, and
after those ``mp_restartgame`` resets the score. Rounds therefore cannot be
counted from the number of events: in the Ancient demo
``round_officially_ended`` occurs 40 times although only 21 rounds were
played.

The reliable marker is **how the score develops**: a played round is one
after which the two teams' combined score is higher than it was before it.
The knife round does not meet the condition, because the point it produces is
wiped by the restart, and neither does a warm-up round, because it produces no
point at all.

The second task is the **win-reason invariant** (:func:`check_win_reasons`).
Under the CS2 rules T can win a round only by eliminating the CTs or by
detonating the bomb; CT wins by elimination, by defusing, or when time runs
out -- if both teams simply sit at their spawn, CT wins. The rule is an
independent check that the sides are the right way round: if lineup
identification went wrong, the wins would be attributed to the wrong team, and
this check catches that.

The module is pure: no files, no demoparser2, no settings. It can therefore be
tested with a hand-built table, and it is the only place that decides the
``round_no`` value (the spine's rule: one function, called only by ``parse``).
"""

from __future__ import annotations

import polars as pl

from pappascout.errors import ParseError, SchemaError

__all__ = [
    "REQUIRED_COLUMNS",
    "T_WIN_REASONS",
    "CT_WIN_REASONS",
    "WIN_REASONS",
    "mark_played_rounds",
    "check_win_reasons",
]

#: The columns :func:`mark_played_rounds` needs.
#:
#: ``score_start`` and ``score_end`` are the **combined score of both teams**
#: at the start and at the end of the round. A combined score survives the
#: half-time switch, where the per-team scores swap places but the sum stays
#: the same.
REQUIRED_COLUMNS: tuple[str, ...] = ("round_raw", "score_start", "score_end")

#: The reasons by which **T** can win a round.
#:
#: Only two ways: every CT dead, or the bomb detonated. ``ct_surrender`` is
#: included because CS2 knows it; it does not occur in the league's demos. The
#: names are the ``reason`` strings of demoparser2 0.42.0's ``round_end``
#: event, read from the library's own string table.
T_WIN_REASONS: frozenset[str] = frozenset(
    {
        "ct_killed",  # every CT eliminated
        "bomb_exploded",  # the bomb detonated
        "ct_surrender",  # CT surrendered
    }
)

#: The reasons by which **CT** can win a round.
#:
#: Elimination, defuse, or time. The time win is included although it occurs
#: in neither test demo: it is the normal CS2 outcome when neither team does
#: anything. demoparser2's string table holds two names for the time win, so
#: both are accepted.
CT_WIN_REASONS: frozenset[str] = frozenset(
    {
        "t_killed",  # every T eliminated
        "bomb_defused",  # the bomb defused
        "t_saved",  # time ran out, the target survived
        "time_ran_out",  # time ran out
        "t_surrender",  # T surrendered
    }
)

#: Side -> the win reasons allowed for it.
WIN_REASONS: dict[str, frozenset[str]] = {"T": T_WIN_REASONS, "CT": CT_WIN_REASONS}


def mark_played_rounds(df: pl.DataFrame) -> pl.DataFrame:
    """Add the ``round_no`` column to the table.

    The table is long: the same ``round_raw`` occurs once for each team. Both
    rows get the same number.

    Args:
        df: A table holding at least :data:`REQUIRED_COLUMNS`. The row order
            is preserved.

    Returns:
        The same table with the ``round_no`` column added. Played rounds get a
        running number 1..N in ``round_raw`` order; warm-up, the knife round
        and restarts get ``null``.

    Raises:
        SchemaError: If a required column is missing, if ``round_raw`` holds
            nulls, or if one ``round_raw`` value carries contradictory score
            readings.
        ParseError: If the combined score grows by more than one within a
            single round. Then round-boundary detection has dropped a round,
            and the numbering would no longer match the demo.
    """
    missing = [name for name in REQUIRED_COLUMNS if name not in df.columns]
    if missing:
        raise SchemaError(
            "mark_played_rounds needs the columns "
            f"{', '.join(REQUIRED_COLUMNS)}; missing: {', '.join(missing)}."
        )

    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Int32).alias("round_no"))

    if df["round_raw"].null_count():
        raise SchemaError(
            "mark_played_rounds: round_raw holds null values. Every round has "
            "to carry the demo's own round id, so that the numbering is "
            "reproducible."
        )

    per_round = (
        df.select(list(REQUIRED_COLUMNS)).unique(maintain_order=True).sort("round_raw")
    )
    if per_round.height != df["round_raw"].n_unique():
        raise SchemaError(
            "mark_played_rounds: one round_raw value carries contradictory "
            "score_start or score_end readings. A round's score readings "
            "belong to the round, so both team rows have to hold the same "
            "values."
        )

    _check_score_steps(per_round)

    played = (
        pl.col("score_end").is_not_null()
        & pl.col("score_start").is_not_null()
        & (pl.col("score_end") > pl.col("score_start"))
    )
    numbered = per_round.with_columns(
        pl.when(played)
        .then(played.cast(pl.Int32).cum_sum())
        .otherwise(pl.lit(None, dtype=pl.Int32))
        .alias("round_no")
    )

    return df.with_columns(
        pl.col("round_raw")
        .replace_strict(
            old=numbered["round_raw"],
            new=numbered["round_no"],
            return_dtype=pl.Int32,
        )
        .alias("round_no")
    )


def _check_score_steps(per_round: pl.DataFrame) -> None:
    """One round may produce at most one point.

    A drop in the score is allowed -- that is ``mp_restartgame``. A jump of
    more than one, on the other hand, means a round was left between two
    measurement points without being recognised. Accepting it silently would
    shift the numbering of every following round by one.
    """
    too_many = per_round.filter(
        pl.col("score_start").is_not_null()
        & pl.col("score_end").is_not_null()
        & ((pl.col("score_end") - pl.col("score_start")) > 1)
    )
    if too_many.is_empty():
        return
    row = too_many.row(0, named=True)
    raise ParseError(
        f"In round round_raw={row['round_raw']} the combined score grew "
        f"{row['score_start']} -> {row['score_end']}, that is by more than "
        "one.\n"
        "Round-boundary detection has left a round out, so the numbering "
        "would not match the demo. Check the demo and the demoparser2 version."
    )


def check_win_reasons(df: pl.DataFrame) -> pl.DataFrame:
    """Check that the win reason fits the side that won.

    The CS2 rules allow T only two ways to win (elimination or a detonated
    bomb) and CT three (elimination, defuse or time). If the check fails, this
    is not a single wrong field but a sign that **the sides have gone the
    wrong way round** -- and then every observation of the round would have
    been attributed to the wrong team.

    Rows where ``won`` or ``win_reason`` is empty are skipped: those are
    rounds that never got resolved.

    Args:
        df: A table holding the columns ``side``, ``won`` and ``win_reason``.

    Returns:
        The same table, unchanged.

    Raises:
        SchemaError: If a required column is missing.
        ParseError: If some round breaks the rule.
    """
    required = ("side", "won", "win_reason")
    missing = [name for name in required if name not in df.columns]
    if missing:
        raise SchemaError(
            "check_win_reasons needs the columns "
            f"{', '.join(required)}; missing: {', '.join(missing)}."
        )
    if df.is_empty():
        return df

    winners = df.filter(
        pl.col("won").fill_null(False) & pl.col("win_reason").is_not_null()
    )
    for row in winners.iter_rows(named=True):
        side = str(row["side"])
        reason = str(row["win_reason"])
        allowed = WIN_REASONS.get(side)
        if allowed is None:
            raise ParseError(
                f"Unknown side {side!r} in the rounds table. The allowed ones "
                f"are {', '.join(sorted(WIN_REASONS))}."
            )
        if reason in allowed:
            continue
        round_id = row.get("round_no") or row.get("round_raw")
        other_side = "CT" if side == "T" else "T"
        hint = (
            f" The reason {reason!r} belongs to side {other_side}, so the "
            "sides are probably the wrong way round."
            if reason in WIN_REASONS[other_side]
            else " The reason is not a known way for a CS2 round to end."
        )
        raise ParseError(
            f"In round {round_id} side {side} won with reason {reason!r}, "
            "which is against the rules of CS2.\n"
            f"{side} can win only in these ways: {', '.join(sorted(allowed))}."
            f"{hint}\n"
            "The parse has gone wrong -- the result is not written."
        )
    return df
