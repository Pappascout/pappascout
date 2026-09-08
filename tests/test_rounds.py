"""``domain.rounds`` -- tests for round numbering and the round invariants.

Numbering is the only real inference in the whole of Story 1.2, and the
function is pure, so it is tested with **hand-built tables and no demos**.
Each test covers one situation that a real demo produces: warm-up, the knife
round, an ``mp_restartgame`` reset, the half-time switch and overtime.
"""

from __future__ import annotations

import polars as pl
import pytest

from pappascout.domain.rounds import (
    CT_WIN_REASONS,
    T_WIN_REASONS,
    check_win_reasons,
    mark_played_rounds,
)
from pappascout.errors import ParseError, SchemaError


def rounds(*scores: tuple[int, int, int]) -> pl.DataFrame:
    """Build a rounds table from triples ``(round_raw, score_start,
    score_end)``.

    Every round produces two rows, as it does in the real table.
    """
    rows = []
    for round_raw, start, end in scores:
        for side in ("T", "CT"):
            rows.append(
                {
                    "round_raw": round_raw,
                    "side": side,
                    "score_start": start,
                    "score_end": end,
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "round_raw": pl.Int32,
            "side": pl.Utf8,
            "score_start": pl.Int32,
            "score_end": pl.Int32,
        },
        orient="row",
    )


def round_numbers(df: pl.DataFrame) -> list[int | None]:
    """The round numbers in ``round_raw`` order, once per round."""
    return (
        df.unique(subset=["round_raw"], keep="first", maintain_order=True)
        .sort("round_raw")["round_no"]
        .to_list()
    )


# --- The basic case -----------------------------------------------------------


def test_played_rounds_are_numbered_from_one() -> None:
    result = mark_played_rounds(rounds((1, 0, 1), (2, 1, 2), (3, 2, 3)))
    assert round_numbers(result) == [1, 2, 3]


def test_both_team_rows_get_the_same_number() -> None:
    result = mark_played_rounds(rounds((1, 0, 1), (2, 1, 2)))
    for round_raw, expected in ((1, 1), (2, 2)):
        values = result.filter(pl.col("round_raw") == round_raw)["round_no"].to_list()
        assert values == [expected, expected]


def test_round_no_is_int32_as_the_schema_requires() -> None:
    result = mark_played_rounds(rounds((1, 0, 1)))
    assert result.schema["round_no"] == pl.Int32


def test_original_columns_and_row_order_survive() -> None:
    source = rounds((1, 0, 0), (2, 0, 1))
    result = mark_played_rounds(source)
    assert result.columns == [*source.columns, "round_no"]
    assert result["side"].to_list() == source["side"].to_list()
    assert result["round_raw"].to_list() == source["round_raw"].to_list()


# --- Warm-up, the knife round and the restart ---------------------------------


def test_knife_round_gets_no_number() -> None:
    """The knife round: it gives a point, but the restart wipes it.

    In the Ancient demo this is round 1: the score before and after is 0,
    because ``mp_restartgame`` erases the knife round's result.
    """
    result = mark_played_rounds(rounds((1, 0, 0), (2, 0, 1), (3, 1, 2)))
    assert round_numbers(result) == [None, 1, 2]


def test_warmup_rounds_get_no_number() -> None:
    result = mark_played_rounds(rounds((1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 1)))
    assert round_numbers(result) == [None, None, None, 1]


def test_score_reset_is_not_a_played_round() -> None:
    """``mp_restartgame`` mid-match: the score drops, it does not grow."""
    result = mark_played_rounds(rounds((1, 0, 1), (2, 1, 2), (3, 2, 0), (4, 0, 1)))
    assert round_numbers(result) == [1, 2, None, 3]


def test_numbering_is_continuous_over_a_skipped_round() -> None:
    """A skipped round leaves no gap in the numbering, only in ``round_raw``."""
    result = mark_played_rounds(rounds((5, 0, 1), (6, 1, 1), (7, 1, 2)))
    assert round_numbers(result) == [1, None, 2]


# --- Half time and overtime ----------------------------------------------------


def test_half_time_switch_does_not_break_numbering() -> None:
    """The combined score survives the change of sides.

    At half time the per-team scores swap places (4-7 -> 7-4), but the sum
    stays the same. That is why the numbering relies on the sum rather than on
    a team's own number.
    """
    result = mark_played_rounds(rounds((12, 11, 12), (13, 12, 13), (14, 13, 14)))
    assert round_numbers(result) == [1, 2, 3]


def test_overtime_rounds_are_numbered_like_any_other() -> None:
    """The Nuke demo: 28 rounds, the last four of them overtime."""
    scores = [(1, 0, 0)] + [(i + 2, i, i + 1) for i in range(28)]
    result = mark_played_rounds(rounds(*scores))
    all_numbers = round_numbers(result)
    assert all_numbers[0] is None
    assert all_numbers[1:] == list(range(1, 29))
    assert max(n for n in all_numbers if n is not None) == 28


# --- Edge cases ----------------------------------------------------------------


def test_unfinished_last_round_is_not_numbered() -> None:
    """A truncated demo: the last round never produced a point."""
    result = mark_played_rounds(rounds((1, 0, 1), (2, 1, 1)))
    assert round_numbers(result) == [1, None]


def test_missing_scores_are_treated_as_not_played() -> None:
    source = rounds((1, 0, 1), (2, 1, 2)).with_columns(
        pl.when(pl.col("round_raw") == 2)
        .then(None)
        .otherwise(pl.col("score_end"))
        .cast(pl.Int32)
        .alias("score_end")
    )
    assert round_numbers(mark_played_rounds(source)) == [1, None]


def test_empty_table_gets_an_empty_round_no_column() -> None:
    empty = rounds().clear()
    result = mark_played_rounds(empty)
    assert result.is_empty()
    assert result.schema["round_no"] == pl.Int32


def test_missing_column_is_named_in_the_error() -> None:
    with pytest.raises(SchemaError) as exc:
        mark_played_rounds(rounds((1, 0, 1)).drop("score_end"))
    assert "score_end" in str(exc.value)


def test_null_round_raw_is_an_error() -> None:
    source = rounds((1, 0, 1)).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_raw")
    )
    with pytest.raises(SchemaError) as exc:
        mark_played_rounds(source)
    assert "round_raw" in str(exc.value)


def test_conflicting_scores_for_one_round_are_an_error() -> None:
    """A round's score belongs to the round, not to a team."""
    source = rounds((1, 0, 1))
    source[1, "score_end"] = 5
    with pytest.raises(SchemaError) as exc:
        mark_played_rounds(source)
    assert "score_start" in str(exc.value) or "score_end" in str(exc.value)


# --- The step in the score -----------------------------------------------------


def test_score_jump_larger_than_one_is_refused() -> None:
    """A jump of two points means a round went unrecognised.

    Accepting it silently would shift the numbering of every following round
    by one, and the whole round list would be at the wrong place in the demo.
    """
    with pytest.raises(ParseError) as exc:
        mark_played_rounds(rounds((1, 0, 1), (2, 1, 3)))
    assert "more than one" in str(exc.value)
    assert "round_raw=2" in str(exc.value)


def test_score_drop_is_allowed_because_it_is_a_restart() -> None:
    assert round_numbers(mark_played_rounds(rounds((1, 0, 1), (2, 5, 0), (3, 0, 1)))) == [
        1,
        None,
        2,
    ]


# --- The win-reason invariant --------------------------------------------------


def wins(*rows: tuple[str, bool, str | None]) -> pl.DataFrame:
    """Build a table from triples ``(side, won, win_reason)``."""
    return pl.DataFrame(
        [
            {"round_no": i + 1, "side": side, "won": won, "win_reason": reason}
            for i, (side, won, reason) in enumerate(rows)
        ],
        schema={
            "round_no": pl.Int32,
            "side": pl.Utf8,
            "won": pl.Boolean,
            "win_reason": pl.Utf8,
        },
        orient="row",
    )


@pytest.mark.parametrize("reason", sorted(T_WIN_REASONS))
def test_t_may_win_only_by_elimination_or_bomb(reason: str) -> None:
    assert check_win_reasons(wins(("T", True, reason))) is not None


@pytest.mark.parametrize("reason", sorted(CT_WIN_REASONS))
def test_ct_may_win_by_elimination_defuse_or_time(reason: str) -> None:
    assert check_win_reasons(wins(("CT", True, reason))) is not None


def test_ct_wins_when_nobody_does_anything() -> None:
    """If both teams sit at their spawn, CT wins when time runs out."""
    check_win_reasons(wins(("CT", True, "time_ran_out"), ("T", False, "time_ran_out")))


@pytest.mark.parametrize("reason", ["t_killed", "bomb_defused", "t_saved"])
def test_t_cannot_win_by_a_ct_reason(reason: str) -> None:
    with pytest.raises(ParseError) as exc:
        check_win_reasons(wins(("T", True, reason)))
    message = str(exc.value)
    assert "against the rules" in message
    assert "wrong way round" in message


@pytest.mark.parametrize("reason", ["ct_killed", "bomb_exploded"])
def test_ct_cannot_win_by_a_t_reason(reason: str) -> None:
    with pytest.raises(ParseError, match="against the rules"):
        check_win_reasons(wins(("CT", True, reason)))


def test_unknown_reason_is_refused_rather_than_ignored() -> None:
    with pytest.raises(ParseError) as exc:
        check_win_reasons(wins(("CT", True, "no-such-reason")))
    assert "not a known way" in str(exc.value)


def test_losing_rows_are_not_checked() -> None:
    """The loser's row carries the winner's reason -- that is no breach."""
    check_win_reasons(wins(("T", True, "bomb_exploded"), ("CT", False, "bomb_exploded")))


def test_unresolved_rounds_are_skipped() -> None:
    """A round that never got resolved does not break the rule."""
    check_win_reasons(wins(("T", None, None), ("CT", None, None)))


def test_missing_column_is_reported() -> None:
    with pytest.raises(SchemaError, match="win_reason"):
        check_win_reasons(wins(("T", True, "ct_killed")).drop("win_reason"))


def test_empty_table_passes() -> None:
    assert check_win_reasons(wins().clear()).is_empty()
