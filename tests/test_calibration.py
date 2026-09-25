"""The calibration documents' truth tables as regression tests.

Two sources, two blocks and **two different requirements on the
environment**.

``kalibrointi-kierrostyypit.md`` (Story 1.4)
    The product owner watched the Ancient demo as a 2D replay and said, of
    every round, what happened in it. The numbers are dollars **per player**,
    read from the ``classified`` table's ``inputs`` structure, and they are
    turned into team totals by multiplying by five. These tests need nothing
    from the machine: they build the rows by hand.

``kalibrointi-stack.md`` (Story 2.14) and
``stack-saanto-mitattu-2026-09-18.md`` (Story 4.4)
    The stack rule's area grouping, coverage and hit table from eight demos.
    The second document replaced the rule's **definition** -- not its
    thresholds -- against 43 rounds the product owner judged blind, and the
    hit table below was re-derived by running the new rule over the archive.
    These tests **read the real archive** (``parsed/`` and ``classified/``,
    not the demo files) and write nothing into it. They are marked
    ``@pytest.mark.archive``, so that they can be selected and excluded
    (``pytest -m "not archive"``), and they skip themselves cleanly on a
    machine that has no archive.

The two orientation rules (Story 4.6)
    The CT advance's and the crunch's hit tables, and what the sampling grid
    does to them. There is no document behind these: they were **re-derived by
    running the rules over the archive** on 2026-09-22, because their absence
    is the defect the story fixed -- neither rule had a hit-count constant, so
    a change of grid re-calibrated both of them and the suite stayed green.
    These read the real archive too.

    **The archive's tables are not guaranteed to be on the grid the settings
    declare**, and this block is the one place that cannot ignore it: a
    settings change and the next parse are two moments, and a test that read
    the tables bare would pin whichever grid the last parse wrote.
    :func:`_on_grid` names the grid each test reads.

    **And nothing here may rest on the archive being in a wrong state.** The
    cross-density measurement first read its dense side from the archive,
    which carried that grid only because a branch had been left without
    re-parsing. When that was repaired the test went red, and correctly so: a
    fault is not a fixture. The observations now live in
    ``tests/data/orientation_by_grid.json`` and the verdict is computed from
    them by the rule's own function, so that guard needs no archive at all.

**In both, the document is the truth.** If some row of the table does not
pass, the code is wrong -- the table is not adjusted to match the code, and
no threshold is nudged so that a single row passes. The thresholds are read
from the real ``settings.toml``: a test that invented limits of its own would
prove nothing about the settings file the tool is run with.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Final, NamedTuple

import polars as pl
import pytest
from pydantic import ValidationError

from conftest import REAL_SETTINGS, require_parsed
from pappascout.adapters.demo_parser import _armed_count
from pappascout.archive.paths import ArchivePaths
from pappascout.render import render_report
from pappascout.render.view import build_view
from pappascout.constants import KNOWN_INVENTORY_ITEMS, SITE_AREAS
from pappascout.domain.report import ROUTE_ROUND_TYPE
from pappascout.domain.economy import (
    classify_round,
    loss_bonus_if_lost,
    players_who_can_buy,
)
from pappascout.domain.models import (
    EconomySettings,
    ThresholdSettings,
    load_settings,
)
from pappascout.domain.aggregate import _presence, match_of
from pappascout.domain.sampling import (
    AreaObservations,
    CloudCell,
    _is_ct_time_row,
    _source_areas,
    site_groups,
    t_side_shares,
)
from pappascout.domain.schemas import ARMED_COLUMN, MONEY_DISTRIBUTION_COLUMN
from pappascout.errors import SchemaError
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages.aggregate import collect_team


class _GridNotGiven:
    """The type of :data:`_SENTINEL_GRID`, and the reason it has one.

    ``()`` cannot be a sentinel: CPython interns the empty tuple, so a caller
    that passed ``()`` -- "filter the tables down to no sample point at all"
    -- would be ``is``-identical to the default and would silently get the
    settings file's grid instead. A class of its own is identical to nothing
    a caller can construct by accident.
    """


#: "The grid was not given", as distinct from "no filtering" and from "an
#: empty grid". A default of ``None`` could not tell them apart, and ``None``
#: is the value that reads the archive's tables exactly as they were parsed
#: (:func:`_on_grid`).
_SENTINEL_GRID: Final = _GridNotGiven()

#: The lineup size the document's per-player numbers were computed with.
PLAYERS = 5

#: The smallest acceptable distance from a threshold to the nearest
#: observation, $/player.
#:
#: $200 is the value of the default pistol (Glock / USP-S / P2000) in the
#: equipment value, that is, the smallest unit that moves in these numbers at
#: all: a narrower margin would mean that one cheapest possible buy can turn
#: a round's class. The guard therefore does not require the threshold to sit
#: in the middle of the interval -- it requires that neither observed set is
#: within touching distance.
#:
#: The guard applies only to thresholds **derived from the team total**
#: (bought and equipment value per player). The half-buy's conditions A and B
#: are not dollar limits per player but player counters, and they have a
#: guard of their own further down.
MIN_MARGIN = 200


class Round(NamedTuple):
    """One row of the calibration document's truth table.

    Attributes:
        round_no: The round number.
        side: The side the row is written from.
        prev_won: Whether the team won the previous round; ``None`` when
            there is no previous round (round 1).
        left: Money left in the pocket, $/player.
        bought: The amount bought, $/player.
        equip: Equipment value, $/player.
        truth: The round type the product owner gave.
        basis: The product owner's justification in words, quoted from the
            document and therefore in Finnish.
        armed: How many players were armed (condition A). ``None`` means
            "not recorded": the row does not end up in a branch that would
            read the counter, and if it ever does, the round is left
            unclassified and this test fails. A guess would be worse than a
            gap.
        left_players: Money left in the pocket per player (condition B), when
            the document has it. ``None`` -> an even split, see :func:`_rows`.

    **The verdict is the product owner's, the numbers are a measurement.**
    The numbers are updated when the moment of measurement changes (see the
    table's change log); the verdict is not touched. Without the update this
    test would pin the rule with inputs the product no longer produces.
    """

    round_no: int
    side: str
    prev_won: bool | None
    left: int
    bought: int
    equip: int
    truth: str
    basis: str
    armed: int | None = None
    left_players: tuple[int, ...] | None = None


#: The truth table as it stands, in the document's row order.
#:
#: **The verdicts are the product owner's**, the numbers are a measurement.
#: Story 1.9 moved the moment of measurement from the end of freezetime to
#: the end of buy time, and the numbers have been updated to that moment --
#: otherwise this table would pin the rule with inputs the product no longer
#: produces, and :func:`test_every_threshold_keeps_a_margin_to_the_nearest_observation`
#: would measure the distance to observations that are no longer made. That
#: would be worse than an out-of-date number: a new measurement only
#: **raises** equipment values, so the guard could pass even when its premise
#: is broken in the data.
#:
#: CHANGE LOG 2026-08-29 (Story 1.9), $/player, ``left / bought /
#: equipment``. Seven rows changed, eight stayed as they were, and not one
#: **verdict** changed:
#:
#: ===========  =========================  =========================
#: Round        Before (freezetime end)    After (buy time end)
#: ===========  =========================  =========================
#: 1 T           270 /  530 /  730          140 /  660 /  860
#: 2 CT         1010 / 2890 / 3200          520 / 3380 / 3690
#: 17 T         3090 /  950 / 1150         3400 /  800 / 1000
#: 19 T         1580 / 3940 / 5330         1540 / 3920 / 5310
#: 19 CT         750 / 1840 / 2040           30 / 2520 / 2720
#: 20 T          270 / 2710 / 2910          210 / 2830 / 3030
#: 21 T         2260 /  510 /  710         2220 /  550 /  750
#: ===========  =========================  =========================
#:
#: The largest change is 19 CT: the fifth player bought kevlar and a Desert
#: Eagle only after freezetime, so "they bought themselves empty" is
#: **confirmed** -- $30/player was left in the pocket and not $750. Round
#: 17 T is the only one on which money rose and equipment fell: there a buy
#: was refunded during the window.
#:
#: The numbers were read from a parse run with production's settings, and
#: :func:`test_ancient_calibration_verdicts_hold_on_the_real_demo`
#: (a demo test) runs the same chain on the real demo, so that this table
#: cannot drift away from it unnoticed.
TRUTH_TABLE: tuple[Round, ...] = (
    Round(1, "CT", None, 110, 650, 850, "pistol", "pistoolikierros"),
    Round(1, "T", None, 140, 660, 860, "pistol", "pistoolikierros"),
    Round(2, "CT", True, 520, 3380, 3690, "full", "voitti pistoolin (S1)"),
    Round(2, "T", False, 2060, 120, 320, "eco", "yksi p250, yksi valo, yksi savu"),
    Round(11, "T", True, 2350, 3790, 5510, "full", "voitti edellisen"),
    Round(11, "CT", False, 2280, 600, 1580, "eco", "yksi säästetty M4 (S3)"),
    Round(14, "T", True, 530, 2960, 3550, "full", "voitti kierroksen 13 (S1)"),
    Round(17, "CT", True, 630, 3520, 5560, "full", "voitti edellisen"),
    Round(17, "T", False, 3400, 800, 1000, "eco", "raha säästetään AWP:hen"),
    Round(19, "T", True, 1540, 3920, 5310, "full", "voitti edellisen"),
    # Armed from the ARMED_TRUTH table (5/5) and the balances from the
    # document's sentence "the real balances were 0, 0, 50, 50, 50" -- the
    # sum 150 = 30 * 5.
    Round(
        19, "CT", False, 30, 2520, 2720, "force", "ostivat tyhjäksi (S2)",
        armed=5, left_players=(50, 50, 50, 0, 0),
    ),
    Round(20, "CT", True, 1350, 2660, 5550, "full", "voitti edellisen"),
    # Armed from the ARMED_TRUTH table (5/5). The distribution of the
    # balances is not recorded, so it is an even split -- and
    # test_no_split_of_the_observed_money_can_satisfy_the_next_round_condition
    # proves that no distribution could change the verdict.
    Round(
        20, "T", False, 210, 2830, 3030, "force", "2x AK, 2x tec9; tyhjäksi",
        armed=5,
    ),
    Round(21, "CT", True, 2700, 2720, 5680, "full", "voitti edellisen"),
    Round(21, "T", False, 2220, 550, 750, "eco", "pitävät econ"),
)


@pytest.fixture
def thresholds(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


@pytest.fixture
def economy(settings_file: Path) -> EconomySettings:
    """The ``[economy]`` section: the half-buy's condition B reads the loss bonus steps from it."""
    return load_settings(settings_file, env_files=()).economy


def _rows(k: Round) -> tuple[dict, dict | None]:
    """The round's row and its previous round in ``ROUNDS`` form.

    The per-player numbers are multiplied by five, so that ``classify_round``
    arrives at the same numbers as the document when it divides them evenly.
    Money spent is not part of the truth table and affects no rule; it is set
    to match the amount bought, so that the money figures in the reasoning
    are credible.

    **The money distribution is an even split unless the table gives one.**
    The truth table records team totals, because it was written before the
    distribution existed. The even split is therefore an assumption and not
    an observation -- and
    :func:`test_no_split_of_the_observed_money_can_satisfy_the_next_round_condition`
    proves that the assumption cannot change a single verdict: on the data's
    forces the whole team's balance is smaller than what three players would
    need even with the largest loss bonus.
    """
    row = {
        "round_no": k.round_no,
        "side": k.side,
        "status": "ok",
        "money_buy_end": k.left * PLAYERS,
        "money_spent": k.bought * PLAYERS,
        "equip_buy_end": k.equip * PLAYERS,
        "equip_round_start": (k.equip - k.bought) * PLAYERS,
        "players_buy_end": PLAYERS,
        ARMED_COLUMN: k.armed,
        MONEY_DISTRIBUTION_COLUMN: (
            list(k.left_players)
            if k.left_players is not None
            else [k.left] * PLAYERS
        ),
        "survivors_equip_prev": 0,
    }
    if k.prev_won is None:
        return row, None
    previous = {
        "round_no": k.round_no - 1,
        "side": k.side,
        "won": k.prev_won,
        "survivors": 0,
    }
    return row, previous


def _test_id(k: Round) -> str:
    return f"k{k.round_no}-{k.side}-{k.truth}"


@pytest.mark.parametrize("k", TRUTH_TABLE, ids=[_test_id(k) for k in TRUTH_TABLE])
def test_truth_table_row_matches_the_classifier(k: Round, thresholds, economy) -> None:
    """Every row of the document classifies as what the product owner saw in
    the replay.
    """
    row, previous = _rows(k)
    decision = classify_round(row, previous, thresholds, economy=economy, loss_count=2)
    assert decision.round_type == k.truth, (
        f"Round {k.round_no} {k.side}: the document says {k.truth!r} "
        f"({k.basis}), the classifier said {decision.round_type!r}. "
        f"Reason: {decision.reason}"
    )


def test_the_truth_table_has_every_row_from_the_document() -> None:
    """15 rows, not one skipped: the table is the story's measuring stick."""
    assert len(TRUTH_TABLE) == 15
    assert len({(k.round_no, k.side) for k in TRUTH_TABLE}) == 15
    assert {k.side for k in TRUTH_TABLE} == {"CT", "T"}


def test_the_two_sides_of_a_round_cannot_both_have_won_the_previous_one() -> None:
    """A cross-check of the table's internal consistency.

    Counting the rows cannot detect a wrongly recorded row, because the table
    would be comparing itself with itself. This compares it with a rule of
    the game: the two sides of the same round are opponents, so exactly one
    of them won the previous round -- or neither of them knows it (round 1).
    """
    by_round: dict[int, list[Round]] = {}
    for k in TRUTH_TABLE:
        by_round.setdefault(k.round_no, []).append(k)

    pairs = {no: rows for no, rows in by_round.items() if len(rows) == 2}
    assert pairs, "the table has no round recorded from both sides"

    for no, rows in pairs.items():
        prev_wins = [k.prev_won for k in rows]
        if all(v is None for v in prev_wins):
            continue  # round 1: neither of them has a previous round
        assert sorted(prev_wins, key=str) == [False, True], (
            f"Round {no}: exactly one of the two sides won the previous "
            f"round, but the table says {prev_wins}."
        )


def test_the_truth_table_covers_the_four_observed_round_types() -> None:
    """The table holds four types out of five -- ``half`` and ``anomaly`` are
    missing.

    This is not a gap in the test but in the **data**: the product owner saw
    not one half-buy and not one anomaly in this demo. ``half`` and
    ``anomaly`` are therefore pinned separately with ``test_economy.py``'s
    hand-built cases, and the half-buy's own observation came only from
    another demo (``inferno_vs_ryhmarama`` round 10, see ``test_inferno_*``
    further down).
    If this table ever gains a half-buy row, the test fails -- and that is
    good: Ancient's verdicts then have to be read again.
    """
    assert {k.truth for k in TRUTH_TABLE} == {"pistol", "full", "force", "eco"}
    assert not [k for k in TRUTH_TABLE if k.truth in ("half", "anomaly")]


@pytest.mark.parametrize("k", TRUTH_TABLE, ids=[_test_id(k) for k in TRUTH_TABLE])
def test_the_loss_counter_never_changes_the_verdict(
    k: Round,
    thresholds,
    economy,
) -> None:
    """The counter now affects the bonus -- but not one verdict here.

    Story 1.10 brought ``loss_count`` back into the decision: the loss bonus
    is directly a function of it, and the bonus is the other half of the
    half-buy's condition B. It still does not move a row of the truth table
    at any value of the counter, and that is exactly this test's claim. It is
    not self-evident: a counter of 0 would give a bonus of $1,900 and a
    counter of 4 a value of $3,400, that is, $1,500 of room on either side of
    condition B.

    Loss count is not a column in the table, because it was not measured --
    and because this test makes it unnecessary: the verdict survives every
    value of the counter.
    """
    row, previous = _rows(k)
    types = {
        classify_round(
            row, previous, thresholds, economy=economy, loss_count=lc
        ).round_type
        for lc in range(thresholds.loss_count_min, thresholds.loss_count_max + 1)
    }
    assert types == {k.truth}


def test_every_threshold_keeps_a_margin_to_the_nearest_observation(
    thresholds,
) -> None:
    """A threshold must not be within touching distance of an observed round.

    Merely checking the sign of the interval ("forces above, ecos below")
    would pass with a value whose margin is $10 as well. This requires a
    distance of :data:`MIN_MARGIN` in the direction in which the data has
    observations.
    """
    losses = [k for k in TRUTH_TABLE if k.prev_won is False]
    forces = [k for k in losses if k.truth == "force"]
    ecos = [k for k in losses if k.truth == "eco"]
    wins = [k for k in TRUTH_TABLE if k.prev_won is True]
    assert forces and ecos and wins

    def margin(observation: int, threshold: int) -> int:
        return abs(observation - threshold)

    # force_buy_min: there are observations on both sides, so the margin is
    # required in both directions.
    nearest_force = min(k.bought for k in forces)
    nearest_eco = max(k.bought for k in ecos)
    assert nearest_eco < thresholds.force_buy_min <= nearest_force
    assert margin(nearest_force, thresholds.force_buy_min) >= MIN_MARGIN
    assert margin(nearest_eco, thresholds.force_buy_min) >= MIN_MARGIN

    # anomaly_equip_max_after_win: no observed buy after a win may drop into
    # being an anomaly (P9).
    lowest_after_win = min(k.equip for k in wins)
    assert lowest_after_win > thresholds.anomaly_equip_max_after_win
    assert (
        margin(lowest_after_win, thresholds.anomaly_equip_max_after_win)
        >= MIN_MARGIN
    )

    # full_equip_min: the data's highest round after a loss must not reach
    # the full-buy limit, otherwise a force would classify as full.
    highest_after_loss = max(k.equip for k in losses)
    assert highest_after_loss < thresholds.full_equip_min
    assert (
        margin(highest_after_loss, thresholds.full_equip_min)
        >= MIN_MARGIN
    )


# --- The first observed half-buy (Story 1.10) -----------------------------------
#
# There is not one half-buy in Ancient's data. ``inferno_vs_ryhmarama``
# closed the gap: the product owner watched rounds 6 and 10 from the demo and
# gave them verdicts, and round 11 confirmed round 10's prediction.
#
# WHAT THESE ROUNDS PROVE -- and what they do not. Both have five armed
# players, so condition A does not separate them at all; the separation is
# made by condition B. They do NOT, however, separate the new rule from the
# retired average rule: the old ``force_money_left_max = 1000`` would give
# round 6 a force ($480/player) and round 10 a half-buy ($1,580/player) too.
# The same holds for all 23 buy rounds after a loss in the six demos: no
# difference at all.
#
# Round 6 classified wrongly only BEFORE Story 1.9, when money was read at
# the end of freezetime -- the measurement made the correction, not the rule.
# What these rows prove is that the new rule reproduces the product owner's
# verdicts; that the rule stands up to an uneven distribution is pinned with
# ``test_economy.py``'s hand-built rows.
#
# The numbers were read at the end of buy time with production's settings
# (parsed table 2026-08-29). Rounds 6 and 10's balances are in the
# calibration document's table as well; round 11's balances were read from
# the same table.


class InfernoRound(NamedTuple):
    """One measured round from the ``inferno_vs_ryhmarama`` demo and the
    product owner's verdict.

    Attributes:
        round_no: The round number.
        loss_count: The counter in force going into the round. **It affects
            the verdict**: the loss bonus is its step.
        money: ``money_buy_end`` as a team total.
        money_players: The balances per player.
        spent: ``money_spent`` as a team total.
        equip: ``equip_buy_end`` as a team total.
        equip_start: ``equip_round_start`` as a team total.
        armed: The counter of armed players.
        can_buy: How many players can make a normal buy on the next round --
            the expected value. ``None`` when the round does not reach
            condition B at all (a full buy, for instance, is settled by the
            equipment value already).
        truth: The product owner's verdict.
        basis: The product owner's words, quoted from the document and
            therefore in Finnish.
    """

    round_no: int
    loss_count: int
    money: int
    money_players: tuple[int, ...]
    spent: int
    equip: int
    equip_start: int
    armed: int
    can_buy: int | None
    truth: str
    basis: str


INFERNO_TRUTH: tuple[InfernoRound, ...] = (
    InfernoRound(
        6, 1, 2400, (1750, 500, 150, 0, 0), 14750, 15350, 1000, 5, 0, "force",
        "neljällä pelaajalla viidestä ei ole seuraavalla kierroksella varaa "
        "ostaa jos he häviävät",
    ),
    InfernoRound(
        10, 4, 7900, (2150, 2050, 2000, 900, 800), 10900, 11900, 1000, 5, 5,
        "half",
        "kutsuisin sitä puoliostoksi koska he todennäköisesti ostavat ensi "
        "kierroksella -- ja niin kävikin, kierroksella 11 normaali osto",
    ),
    # A supporting observation: round 11 confirmed round 10's prediction. It
    # is not a half-buy but a **normal buy** (five AKs, $4,940/player), and it
    # is here so that the claim "they bought on the next round" is pinned to
    # numbers and not merely repeated in three comments.
    # Conditions A and B are not computed: a full buy is settled by the
    # equipment value already.
    InfernoRound(
        11, 4, 1500, (650, 550, 300, 0, 0), 23700, 24700, 1000, 5, None,
        "full",
        "kierros 11 vahvisti ennusteen: viisi AK:ta",
    ),
)


def _inferno_rows(k: InfernoRound) -> tuple[dict, dict]:
    """The round's row and its previous round in ``ROUNDS`` form."""
    row = {
        "round_no": k.round_no,
        "side": "T",
        "status": "ok",
        "money_buy_end": k.money,
        "money_spent": k.spent,
        "equip_buy_end": k.equip,
        "equip_round_start": k.equip_start,
        "players_buy_end": PLAYERS,
        ARMED_COLUMN: k.armed,
        MONEY_DISTRIBUTION_COLUMN: list(k.money_players),
        "survivors_equip_prev": 0,
    }
    previous = {
        "round_no": k.round_no - 1,
        "side": "T",
        "won": False,
        "survivors": 0,
    }
    return row, previous


#: The rounds that really do reach conditions A and B.
INFERNO_BUY_ROUNDS: tuple[InfernoRound, ...] = tuple(
    k for k in INFERNO_TRUTH if k.can_buy is not None
)


@pytest.mark.parametrize(
    "k", INFERNO_TRUTH, ids=[f"k{k.round_no}-{k.truth}" for k in INFERNO_TRUTH]
)
def test_inferno_round_matches_the_product_owners_verdict(
    k: InfernoRound, thresholds, economy
) -> None:
    """Rounds 6, 10 and 11 classify as what the product owner saw in the
    demo.
    """
    row, previous = _inferno_rows(k)
    decision = classify_round(
        row, previous, thresholds, economy=economy, loss_count=k.loss_count
    )
    assert decision.round_type == k.truth, (
        f"Round {k.round_no}: the product owner says {k.truth!r} "
        f"({k.basis}), "
        f"the classifier said {decision.round_type!r}. "
        f"Reason: {decision.reason}"
    )


@pytest.mark.parametrize(
    "k",
    INFERNO_BUY_ROUNDS,
    ids=[f"k{k.round_no}-{k.truth}" for k in INFERNO_BUY_ROUNDS],
)
def test_inferno_reason_names_both_counters(
    k: InfernoRound, thresholds, economy
) -> None:
    """The reason names the counters of both conditions, not only the
    decisive one.

    Otherwise the reader cannot see which condition rejected the round. The
    reason also has to show **the balances as they are**: "0/5 can buy" on
    its own cannot be checked against the demo, because it does not show how
    close each of them came.
    """
    row, previous = _inferno_rows(k)
    decision = classify_round(
        row, previous, thresholds, economy=economy, loss_count=k.loss_count
    )
    bonus = economy.loss_bonus_steps[k.loss_count]
    assert f"{k.armed}/{PLAYERS} armed" in decision.reason
    assert (
        f"{k.can_buy}/{PLAYERS} can buy on the next round"
        in decision.reason
    )
    # The distribution as it is, with the unit on every number.
    assert ", ".join(f"{m} $" for m in k.money_players) in decision.reason
    # The loss bonus shown: without it the reader cannot work it out.
    assert f"loss bonus {bonus} $" in decision.reason
    assert decision.inputs["players_can_buy"] == k.can_buy
    assert decision.inputs["players_armed"] == k.armed
    assert decision.inputs["loss_bonus_if_lost"] == bonus


def test_inferno_rows_are_internally_consistent(thresholds) -> None:
    """The table's rows must not claim two different things about one round.

    Three invariants, none of them self-evident in a hand-recorded table:

    * the distribution sums to ``money_buy_end`` -- otherwise the row would
      measure condition B with different money from the one it records as
      the team total
    * the distribution holds exactly five players, the same set as the
      counters
    * the amount bought exceeds ``force_buy_min``, that is, the round really
      does end up in the buy branch. If ``equip_start`` were wrong, the row
      would pass through the eco branch and would not exercise the conditions
      at all -- and not one demo test covers that.
    """
    for k in INFERNO_TRUTH:
        assert sum(k.money_players) == k.money, k.round_no
        assert len(k.money_players) == PLAYERS, k.round_no
        bought_pp = (k.equip - k.equip_start) / PLAYERS
        assert bought_pp >= thresholds.force_buy_min, k.round_no
        assert 0 <= k.armed <= PLAYERS, k.round_no


def test_the_armed_counter_cannot_separate_the_two_inferno_rounds() -> None:
    """Condition A is identical on rounds 6 and 10; condition B is not.

    This does not prove that condition A is unnecessary -- it measures
    something else. It proves that condition A alone is not enough to tell a
    force from a half-buy, and that condition B makes that separation on
    these two rounds.
    """
    assert len({k.armed for k in INFERNO_BUY_ROUNDS}) == 1
    assert len({k.can_buy for k in INFERNO_BUY_ROUNDS}) == 2


def test_the_next_round_threshold_keeps_a_margin_to_the_nearest_observation(
    thresholds, economy
) -> None:
    """Condition B's money threshold must not be within touching distance of
    an observation.

    The same guard as on the other per-player thresholds, but the observation
    here is **one player's buying power** (their own balance + the loss
    bonus). The data has both sides: on round 6 the richest player's buying
    power stays below the threshold (1,750 + 1,900 = 3,650) and on round 10
    the poorest player's exceeds it (800 + 3,400 = 4,200).

    Without this the threshold could slide to the edge of the data unnoticed
    -- and a balance moved by one buy would turn the class.
    """
    below: list[int] = []
    above: list[int] = []
    for k in INFERNO_BUY_ROUNDS:
        bonus = economy.loss_bonus_steps[k.loss_count]
        for money in k.money_players:
            power = min(money + bonus, economy.max_money)
            if power >= thresholds.normal_buy_money_min:
                above.append(power)
            else:
                below.append(power)

    assert below and above, "one-sided data does not measure a margin"
    assert max(below) < thresholds.normal_buy_money_min <= min(above)
    assert thresholds.normal_buy_money_min - max(below) >= MIN_MARGIN
    assert min(above) - thresholds.normal_buy_money_min >= MIN_MARGIN


def test_the_loss_bonus_is_the_step_the_counter_points_at(
    thresholds, economy
) -> None:
    """The bonus is ``steps[loss_count]``, not ``steps[loss_count + 1]``.

    ``settings.toml`` says so directly: the index is the loss count, so the
    start of the half (counter 1) gives $1,900 for a lost pistol round. The
    counter describes the state going into the round, and that is exactly the
    step paid if the round is lost. An index one too large would give every
    player $500 too much buying power -- and round 6's counter would be 1/5
    and not 0/5.
    """
    assert loss_bonus_if_lost(1, thresholds, economy) == 1900
    assert loss_bonus_if_lost(4, thresholds, economy) == 3400
    for k in INFERNO_BUY_ROUNDS:
        assert loss_bonus_if_lost(k.loss_count, thresholds, economy) == (
            economy.loss_bonus_steps[k.loss_count]
        )


def test_the_loss_bonus_clamps_instead_of_raising(thresholds, economy) -> None:
    """The edges do not fail: zero, negative, and too short a step list.

    The clamp is a safety net and not a rule -- loading the settings requires
    exactly ``loss_count_max + 1`` steps. A hand-built section or a corrupted
    counter must still not bring the whole run down with an ``IndexError``:
    one row would then take the whole demo with it.
    """
    steps = economy.loss_bonus_steps
    assert loss_bonus_if_lost(0, thresholds, economy) == steps[0]
    # A negative counter is not possible after loss_counts, but in Python
    # steps[-1] would silently give the **largest** bonus.
    assert loss_bonus_if_lost(-3, thresholds, economy) == steps[0]
    assert loss_bonus_if_lost(99, thresholds, economy) == steps[-1]

    short = economy.model_copy(update={"loss_bonus_steps": [1400, 1900]})
    assert loss_bonus_if_lost(4, thresholds, short) == 1900


def test_players_who_can_buy_refuses_a_hole_in_the_distribution(
    thresholds, economy
) -> None:
    """A public function's contract must not live only in its callers.

    A missing balance read as zero would claim the player has no money and
    would turn a half-buy into a force. The callers check for it already, but
    the function is public -- the next caller may not check.
    """
    with pytest.raises(SchemaError):
        players_who_can_buy([2000, None, 0], 1900, thresholds, economy)


def test_the_buying_power_is_capped_at_the_money_ceiling(
    thresholds, economy
) -> None:
    """``balance + bonus`` cannot exceed ``[economy].max_money``.

    The game would cut the excess away, so without the clamp the counter
    would promise buying power on money the player never has.

    With production values the ceiling does not bite ($16,000 vs. $4,000), so
    it is pinned with a low ceiling: 2,000 + 2,400 = 4,400 would be enough
    for a limit of 4,000, but a ceiling of $3,000 cuts the sum below it.
    Without the clamp this test would return 1.
    """
    low_ceiling = economy.model_copy(update={"max_money": 3000})
    assert (
        players_who_can_buy([2000], 2400, thresholds, low_ceiling) == 0
    ), "the ceiling did not cut the sum"
    # The same player and the same bonus at production's ceiling: the limit is passed.
    assert players_who_can_buy([2000], 2400, thresholds, economy) == 1


# --- The armed counter (Story 1.6) ---------------------------------------------
#
# These are the same human-given truth as the table above, but as per-player
# observations. They live here and not in a demo test, because
# ``pytest -m "not demo"`` is the run in which the truth table is meant to
# survive: a demo test skips itself on a machine that has no demos, and the
# calibration would then go unguarded.
#
# THE INVENTORY AND THE ARMOUR were read from Ancient at the **end of buy
# time** tick on 2026-08-29 (rounds 19, 20 and 21). In Story 1.5 what stood
# here was the same players' equipment values and a threshold of $950; Story
# 1.6 changed the measure to the observation, because the equipment value is
# weapon + armour + grenades as one number and does not tell a bought weapon
# from a free pistol and two flashes.
#
# STORY 1.9 MOVED THE MOMENT OF MEASUREMENT, and one number changed:
# **round 19 CT is 5/5, not 4/5**. The third player bought kevlar and a
# Desert Eagle only after freezetime (at the anchor ('knife', 'USP-S'),
# armour 0; at the end of buy time ('knife', 'Desert Eagle',
# 'Smoke Grenade'), armour 100). The product owner's VERDICT does not change
# -- the round is a force, and "they bought themselves empty" is even
# confirmed, because $150 was left in the pocket and not $3,750 -- but their
# words "one was left with the free default pistol" were a reading from the
# wrong moment. Rounds 20 and 21 stayed as they were (5/5 and 2/5).
#
# THE INVENTORIES ARE FROM A LATER MOMENT than before, and that shows in them
# in two ways which do **not** affect the counter:
#
#   * thrown grenades are gone and a planted C4 has changed place (round 20 T
#     is measured 19.0 s and round 21 T 17.2 s after the anchor)
#   * the armour has taken hits: on round 21 the values are 90 and 89, not
#     100
#
# Neither moves the counter: the rule is "armour **and at least one weapon**
# in hand", and a grenade is not a weapon and 90 is not zero. If either ever
# starts to move the number, it shows here before it shows in the report.
#
# Round 21's number still rests on the right reason. The product owner
# described the round: "two with kevlar+pistol and one with a $300 p250
# without kevlar". The old threshold dropped the p250 player because
# 300 < 950; the new rule drops him because he has no armour. The same
# number, a different claim -- and the latter is the one the product owner
# said.


class ArmedRound(NamedTuple):
    """One round's per-player observations and the product owner's
    description.

    Attributes:
        round_no: The round number.
        side: The side this row is for.
        players: Five ``(inventory, armour value)`` pairs read from the demo.
        armed: How many of them are armed -- the expected value.
        basis: The product owner's description in words, quoted from the
            calibration document and therefore in Finnish.
    """

    round_no: int
    side: str
    players: tuple[tuple[tuple[str, ...], int], ...]
    armed: int
    basis: str


ARMED_TRUTH: tuple[ArmedRound, ...] = (
    ArmedRound(
        19,
        "CT",
        (
            (("Skeleton Knife", "Desert Eagle"), 100),
            (("Huntsman Knife", "Five-SeveN"), 100),
            (("knife", "Desert Eagle", "Smoke Grenade"), 100),
            (("Shadow Daggers", "P2000", "SSG 08"), 100),
            (("knife", "USP-S", "MP9", "High Explosive Grenade"), 100),
        ),
        5,
        'force, "ostivat tyhjäksi": kaikki viisi aseistautuivat, ja taskuun '
        "jäi 150 $/pelaaja",
    ),
    ArmedRound(
        20,
        "T",
        (
            (("knife_t", "Tec-9"), 100),
            (("knife_t", "Glock-18", "AK-47", "Flashbang"), 100),
            (("M9 Bayonet", "Glock-18", "AK-47"), 100),
            (("Talon Knife", "Tec-9"), 100),
            (("Bowie Knife", "Glock-18", "MAC-10", "Smoke Grenade",
              "Flashbang"), 100),
        ),
        5,
        "2x AK, 2x tec9, 1x mac10, kaikilla kevlar+kypärä -- kaikki viisi",
    ),
    ArmedRound(
        21,
        "T",
        (
            (("knife_t", "C4 Explosive", "P250"), 90),
            (("knife_t", "Glock-18"), 0),
            (("M9 Bayonet", "P250"), 0),
            (("Talon Knife", "Glock-18"), 0),
            (("Bowie Knife", "P250"), 89),
        ),
        2,
        'eco, "pitävät econ": kahdella kevlar+pistooli, yhdellä 300 $:n p250 '
        "ilman kevlaria",
    ),
)


@pytest.mark.parametrize(
    "k", ARMED_TRUTH, ids=[f"k{k.round_no}-{k.side}" for k in ARMED_TRUTH]
)
def test_armed_player_count_matches_the_human_reading(k: ArmedRound) -> None:
    """The counter gives the number the product owner saw in the replay.

    The rule is read from the adapter and not written again here: a test that
    checked with an expression of its own whether a player has a weapon would
    prove only its own expression.
    """
    rows = [
        {"inventory": inventory, "armor_value": armor}
        for inventory, armor in k.players
    ]
    counted = _armed_count(rows)
    assert counted == k.armed, (
        f"Round {k.round_no} {k.side}: the document says {k.armed} "
        f"({k.basis}), the counter said {counted}. "
        "The document is the truth -- fix the rule or the weapon list, not "
        "this table."
    )


def test_no_split_of_the_observed_money_can_satisfy_the_next_round_condition(
    thresholds, economy
) -> None:
    """The even-split assumption cannot change a single observed force
    verdict.

    The truth table records team totals, so :func:`_rows` has to assume a
    distribution. The assumption would be dangerous if some other
    distribution gave a different result -- the table would then pin the rule
    with an invented input.

    That is not possible, and this proves it without a distribution:
    condition B requires ``normal_buy_players_min`` players, each with at
    least ``normal_buy_money_min - bonus`` of their own money. Even with the
    largest bonus ($3,400) the need is 3 x 600 = $1,800, and on the data's
    forces the whole team's balance is $150 and $1,050. There simply is not
    enough money for any distribution.
    """
    forces = [k for k in TRUTH_TABLE if k.truth == "force"]
    assert forces

    per_player_need = thresholds.normal_buy_money_min - max(economy.loss_bonus_steps)
    team_need = thresholds.normal_buy_players_min * per_player_need

    for k in forces:
        team_total = k.left * PLAYERS
        assert team_total < team_need, (
            f"Round {k.round_no} {k.side}: the team had ${team_total}, "
            f"and condition B would need ${team_need} for the buying power "
            "of three players alone. The even split is therefore no longer a "
            "harmless assumption -- record the real distribution for this "
            "row in the left_players field."
        )


def test_no_calibration_round_exercises_the_armed_condition(thresholds) -> None:
    """Condition A's threshold is **a stated rule, not an observation** --
    and this says so.

    The data's only lightly armed round is Ancient's 21 T (2/5), and that is
    exactly what has previously been used to justify the threshold
    ``armed_players_min = 3``. It will not do: the round is settled by the
    buy limit ``force_buy_min`` already ($550/player bought) and never
    reaches condition A.

    The data's two buy rounds are both 5/5 armed, so the threshold could be
    anything between 1 and 5 without a single verdict changing. The threshold
    therefore rests on a limit the user stated ("at least three with kevlar
    and some upgraded weapon"), and this test keeps that visible: if a round
    that really does exercise condition A ever enters the data, the test
    fails -- and the threshold can be calibrated then.
    """
    by_round = {(k.round_no, k.side): k for k in ARMED_TRUTH}
    truth = {(k.round_no, k.side): k for k in TRUTH_TABLE}

    # The lightly armed round does not reach condition A: it is an eco at
    # the buy limit already.
    saved = by_round[(21, "T")]
    assert saved.armed < thresholds.armed_players_min
    assert truth[(21, "T")].bought < thresholds.force_buy_min

    # The rounds that do reach condition A are all fully armed -- that is,
    # above the threshold and not close to it. The comparison is ``>=``, the
    # same as in the rule: a genuine force with exactly three armed players
    # must not fall foul of the guard.
    reaching = [
        by_round[key]
        for key, k in truth.items()
        if k.prev_won is False and k.bought >= thresholds.force_buy_min
        and key in by_round
    ]
    assert reaching, "the data has no round that reaches condition A"
    for k in reaching:
        assert k.armed >= thresholds.armed_players_min, (k.round_no, k.side)
    # There are observations only from above the threshold, so there is no
    # "empty gap" on either side -- that is this test's whole claim.
    assert {k.armed for k in reaching} == {PLAYERS}


def test_calibration_inventories_contain_no_unknown_names() -> None:
    """Every name in the calibration is in the classification.

    An unknown name is not a weapon, so an unknown **weapon** would quietly
    push the number down and the test above would fail only on the end
    result. This names the cause directly. At the same time it is a
    data-specific guard: if a name is removed from the weapon list, this says
    which one.
    """
    unknown: set[str] = set()
    for k in ARMED_TRUTH:
        for inventory, _armor in k.players:
            unknown |= set(inventory) - KNOWN_INVENTORY_ITEMS
    assert unknown == set()


def test_unknown_name_does_not_arm_the_player() -> None:
    """An unknown name does not arm anyone, even with armour on the player.

    The classification is a list of allowed weapons: a new knife skin must
    not arm anybody. The reporting side is in the adapter's tests, because
    the names are collected from all of the anchor's rows and not only in the
    counter.

    The name is deliberately invented and not a real knife skin: a real name
    would sooner or later reach the classification from a new batch of demos.
    """
    rows = [{"inventory": ("Ei-Ole-Olemassa-9000", "Glock-18"), "armor_value": 100}]
    assert _armed_count(rows) == 0


def test_unreadable_armor_or_inventory_empties_the_count() -> None:
    """An unreadable observation is ``null``, not a partial count.

    The player stays in ``players_buy_end``'s divisor, so a partial count
    would claim they are unarmed -- a read error would look like a saving
    round. Zero and an empty list, by contrast, are observations.
    """
    armed = {"inventory": ("Bowie Knife", "P250"), "armor_value": 100}

    assert _armed_count([armed, {"inventory": None, "armor_value": 100}]) is None
    assert _armed_count([armed, {"inventory": (), "armor_value": None}]) is None
    # Observations, not gaps.
    assert _armed_count([armed, {"inventory": (), "armor_value": 0}]) == 1


def test_armed_count_needs_the_whole_distribution_not_the_team_sum() -> None:
    """The same team total, a different counter -- this is the whole reason
    the column exists.

    Three players with kevlar and a bought pistol ($950 each) and two with a
    free pistol ($200) give the same team total of 3250 as five players with
    kevlar alone ($650 each). The first is a half-buy, the second is not, and
    ``equip_buy_end`` does not tell them apart.
    """
    half_buy = [
        {"inventory": ("knife", "P250"), "armor_value": 100} for _ in range(3)
    ] + [
        {"inventory": ("knife", "Glock-18"), "armor_value": 0} for _ in range(2)
    ]
    kevlars_only = [
        {"inventory": ("knife", "Glock-18"), "armor_value": 100} for _ in range(5)
    ]

    assert _armed_count(half_buy) == 3
    assert _armed_count(kevlars_only) == 0


def test_armor_and_weapon_are_both_required() -> None:
    """Kevlar without a weapon is not enough, nor a weapon without kevlar.

    The product owner's definition is "kevlar **and** some upgraded weapon".
    Round 21's p250 player is the latter case and round 19's USP-only player
    the former: both occur in the data, so neither condition can be dropped
    without claiming something the product owner did not say.
    """
    weapon_no_armor = [{"inventory": ("M9 Bayonet", "P250"), "armor_value": 0}]
    armor_no_weapon = [{"inventory": ("knife", "USP-S"), "armor_value": 100}]
    both = [{"inventory": ("Bowie Knife", "P250"), "armor_value": 100}]

    assert _armed_count(weapon_no_armor) == 0
    assert _armed_count(armor_no_weapon) == 0
    assert _armed_count(both) == 1


# --- Calibration of the stack rule (Story 2.14) ---------------------------------
#
# The source is ``_bmad-output/implementation-artifacts/kalibrointi-stack.md``.
# These tests read the archive's ``parsed/`` and ``classified/`` tables but
# not the demo files, and they write nothing into the archive. On another
# machine they skip themselves (:func:`conftest.require_parsed`).

#: Ancient's three demos. The area grouping must be **word for word the
#: same** from every one of them: that is the whole justification for the cell
#: median, and the trajectory-based derivation was rejected precisely because
#: it was not.
ANCIENT_DEMOS = (
    "Ancient_vs_kaljukostaja",
    "ANCIENT_vs_RCAVE_VETERANS",
    "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1",
)

#: Ancient's derived area grouping, row by row from the document's table.
ANCIENT_GROUPS = {
    "BombsiteA": "A",
    "CTSpawn": "A",
    "House": "A",
    "MainHall": "A",
    "Outside": "A",
    "SideHall": "A",
    "Alley": "B",
    "BombsiteB": "B",
    "Ramp": "B",
    "Ruins": "B",
    "SideEntrance": "B",
    "TSideLower": "B",
    "TSideUpper": "B",
    "Tunnel": "B",
    "Water": "B",
}

#: Ancient's areas that fall **outside both groups**: the map's shared
#: middle. They are not a missing observation but an observation that neither
#: site is genuinely closer.
ANCIENT_SHARED = ("Middle", "TSpawn", "TopofMid")

#: Nuke's two demos. The sites are on top of each other on different floors,
#: so the rule stays silent -- and that is what has to be recorded in the
#: coverage, not as zero hits.
NUKE_DEMOS = (
    "Nuke_vs_imuaijat",
    "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1",
)

#: The archive's two subject teams. **An id, not a directory listing**:
#: ``classified/`` holds a row for every lineup, and the same team is there
#: under several ids (a side swap and substitutes). Iterating over all of
#: them would count the same rounds twice.
CALIBRATION_TEAMS = ("9ac92660986558d3", "ff03fb54599d3311")

#: Every round type's win-loss record in the archive, measured 2026-09-23 by
#: running :func:`_reports` over it -- **the same code path the test that
#: reads this back uses**, so the file records what the tool produces and not
#: a hand-count of the parquet files.
#:
#: **Why this is a data file and not a table in a comment.** The record is
#: the one figure in a block a reader acts on, and nothing else in the suite
#: could see it go wrong. ``_check_record_covers_the_rounds`` compares
#: **totals**, and ``sample_for`` and ``record_for`` both count one round per
#: row, so the model can only notice a record built from a different *number*
#: of rows: measured 2026-09-23, swapping the wins and the losses
#: (``RoundRecord(wins=losses, losses=wins, ...)``) left ``-m archive`` at 20
#: passed, and so did counting the wrong rows at the right length. The report
#: would have printed ``voitettu 7-15`` where the truth is ``15-7``, on the
#: real archive, with the only suite that reads real data silent. Any single
#: row of this table kills both.
#:
#: **What is checkable from this repository, and what is not.** Without the
#: archive: nothing about the numbers. The file is data, the tests that read
#: it are ``-m archive``, and on a machine without the archive they skip --
#: so a clone can read the table but cannot confirm it. With the archive:
#: every number, because the test re-derives the whole table and compares it
#: whole.
#:
#: **It is an observation and not a rule.** Re-classifying the archive,
#: adding a demo or changing a classification threshold changes these numbers
#: legitimately; the answer is then to measure the table again and say so in
#: the commit, not to loosen the test. The one thing it must never do is
#: change because of a change to ``record_for`` or ``record_text``.
ROUND_RECORDS = json.loads(
    (Path(__file__).parent / "data" / "round_records.json").read_text(
        encoding="utf-8"
    )
)

#: The teams the record table covers: :data:`CALIBRATION_TEAMS` **and the
#: scouted team**.
#:
#: The third team is in the archive but was never a calibration subject, and
#: it is the one the story's own measurement was taken from -- the records
#: the spec quotes (Nuke T default 9-20, Nuke CT default 15-7, Dust2 CT eco
#: 1-7) are its, and the calibration teams do not even have a ``de_dust2``.
#: Pinning only the calibration teams would have left every number the story
#: rests on unpinned.
RECORDED_TEAMS = tuple(ROUND_RECORDS["teams"])

#: Every demo those teams' reports are built from, for the skip gate.
RECORDED_DEMOS = tuple(
    sorted(
        {
            demo
            for entry in ROUND_RECORDS["teams"].values()
            for demo in entry["demos"]
        }
    )
)

#: The group the reading guide's example record comes from
#: (``render.view._LEGEND_RECORD``), named here so the tie between the guide
#: and the table is a lookup and not a sentence in a docstring.
LEGEND_RECORD_GROUP = ("1e1965abbc06133b", "de_nuke", "CT", "full")

#: All eight demos in the archive.
CALIBRATION_DEMOS = (
    *ANCIENT_DEMOS,
    *NUKE_DEMOS,
    "Anubis_vs_ryhmarama",
    "anubis_vs_RCAVE_VETERANS",
    "inferno_vs_ryhmarama",
)

#: The stack's numbers **after Story 4.4 rewrote the rule's definition**,
#: re-derived by running the rule over the archive on 2026-09-18 and not
#: copied from any document: 5 hits on 5 rounds of the 93 scanned.
#:
#: They supersede 27 hits on 23 rounds, which was the old definition's
#: reading of the same archive -- and which the product owner read as wrong
#: on 22 of those 23 rounds
#: (``sokkolista-stack-2026-09-12.md``, ``sokkolista-2-ohitukset-2026-09-13.md``).
#:
#: One hit per round now, because the rule reads one sample point: the
#: distinction between hits and rounds is kept all the same, since it is the
#: structure's own (an anomaly is grouped by sample point) and a change that
#: made the two differ must show up here.
#:
#: The measurement document reports **7** rounds for the same candidate rule,
#: and the difference is known rather than a contradiction: it counted an
#: ungrouped area as a group of its own, and this rule does not read ungrouped
#: areas at all. The two rounds are Ancient's ``Middle`` crowds
#: (``Ancient_vs_kaljukostaja`` 2 and 10), which sit in the map's shared
#: middle and around neither site -- a stack is a *site* observation, and the
#: mid concentration is a different phenomenon awaiting its own decision.
STACK_HITS = 5
STACK_ROUNDS = 5
STACK_SCANNED = 93
STACK_CT_ROUNDS = 93
STACK_SILENCED_ROUNDS = 0

#: The hit table: (map, demo, round, type, moment, group, players, alive,
#: the crowd's areas). One row per hit, **re-derived by running the rule
#: against the archive** on 2026-09-18 -- not copied from a document and not
#: adjusted by hand.
#:
#: Every row is at 15 s, because that is what the rule reads now
#: (``stack_sample_s``), and every row names the areas the players are
#: really on: two of the five have nobody on the site's own area at all, and
#: under the old definition they were silenced for exactly that reason.
#:
#: What the product owner said about these five rounds, blind:
#:
#: * ``1-a52ebff2…`` r14 (``Alley`` 5) -- *"B stack tyylinen"*
#: * ``Anubis_vs_ryhmarama`` r4 (``BackofB`` 2 + ``BombsiteB`` 2) -- *"B stack"*
#: * ``inferno_vs_ryhmarama`` r2 (``Middle`` 5) -- *"mid stack tai pusku"*
#: * ``Nuke_vs_imuaijat`` r5 (``Outside`` 3 + ``Hut`` 1) -- *"pienimuotoinen stack"*
#: * ``1-79f71e00…`` r20 (``Outside`` 3 + ``Catwalk`` 1) -- *"outside pusku"*
#:
#: Four stacks and one named push, and **not one round he read as normal**.
#: The rule does not name the pattern (measured as not separable from this
#: data), so the push is a hit here and that is deliberate.
#:
#: Sorted, so that the comparison is independent of the order in which the
#: maps and the teams are processed.
STACK_TABLE = sorted(
    [
        ('de_ancient', '1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1', 14, 'eco', 15.0, 'B', 5, 5, ('Alley',)),
        ('de_anubis', 'Anubis_vs_ryhmarama', 4, 'eco', 15.0, 'B', 4, 5, ('BackofB', 'BombsiteB')),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 15.0, 'A', 5, 5, ('Middle',)),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 20, 'eco', 15.0, 'A', 4, 5, ('Outside', 'Catwalk')),
        ('de_nuke', 'Nuke_vs_imuaijat', 5, 'eco', 15.0, 'A', 4, 5, ('Outside', 'Hut')),
    ]
)

#: The rounds the product owner read as **not a stack** that the rule starts
#: reporting when the area bound is loosened by one, 2 -> 3. The demo and the
#: round number come from the two blind lists' keys and his verdicts from
#: their answer tables; the rule's own answer is measured in the test.
#:
#: This table is the guard that keeps ``stack_max_areas`` honest: a threshold
#: whose looser value costs nothing is not a measured threshold. All eight
#: rounds added at 3 are rounds he judged normal -- a subset of
#: :data:`JUDGED_NOT_A_STACK_IN_SCOPE`, and the test asserts that too, so the
#: two tables cannot drift apart.
#:
#: The measurement document's figure for the same change is **9**, and the
#: difference is the population and not a disagreement: it counts all 43
#: judged rounds, of which 11 are the opponent's and outside this rule's
#: scope.
JUDGED_NOT_A_STACK_AT_THREE_AREAS = (
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 16),  # list 2 L, "normi"
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 26),  # list 1 H, "normaali"
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 27),  # list 1 W, "normaali"
    ("ANCIENT_vs_RCAVE_VETERANS", 13),  # list 1 F, "aika normaalilta"
    ("ANCIENT_vs_RCAVE_VETERANS", 18),  # list 1 A, "hajallaan"
    ("Anubis_vs_ryhmarama", 1),  # list 2 C, "ei ihan stack ainakaan"
    ("Anubis_vs_ryhmarama", 11),  # list 1 G, "pieni b painotus"
    ("Nuke_vs_imuaijat", 4),  # list 1 S, "normaali"
)

#: Every round **inside the rule's scope** that the product owner read as not
#: a stack: 21 of blind list 1 and 5 of blind list 2, with the list letter
#: beside each. The other 7 of his 33 "not a stack" verdicts are the
#: opponent's CT rounds, which the rule cannot scan -- naming the population
#: is the whole point of this constant, because the first version of this
#: story's text used the 43-round figures as if they were the rule's own.
#:
#: Three of them are **hedged** rather than flat: 1A *"ei (osittainen)"*,
#: 1F *"ei ... toki tämäkin on hyödyllistä tietoa, koska se on oletettava
#: pieni stack sille puolelle"* and 2C *"ei ihan stack ainakaan"*. They are
#: kept on the list -- his answer to "is this a stack" was no in all three --
#: and the hedging is recorded rather than smoothed away.
JUDGED_NOT_A_STACK_IN_SCOPE = (
    ("ANCIENT_vs_RCAVE_VETERANS", 18),  # 1A, hedged: "ei (osittainen)"
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 14),  # 1B
    ("1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1", 16),  # 1C
    ("Nuke_vs_imuaijat", 10),  # 1D
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 22),  # 1E
    ("ANCIENT_vs_RCAVE_VETERANS", 13),  # 1F, hedged: "oletettava pieni stack"
    ("Anubis_vs_ryhmarama", 11),  # 1G
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 26),  # 1H
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 23),  # 1I
    ("Ancient_vs_kaljukostaja", 7),  # 1J
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 18),  # 1K
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 15),  # 1L
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 19),  # 1M
    ("Nuke_vs_imuaijat", 1),  # 1O
    ("Ancient_vs_kaljukostaja", 12),  # 1P
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 17),  # 1R
    ("Nuke_vs_imuaijat", 4),  # 1S
    ("Nuke_vs_imuaijat", 9),  # 1T
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 13),  # 1U
    ("ANCIENT_vs_RCAVE_VETERANS", 15),  # 1V
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 27),  # 1W
    ("Anubis_vs_ryhmarama", 1),  # 2C, hedged: "ei ihan stack ainakaan"
    ("Nuke_vs_imuaijat", 11),  # 2E
    ("1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1", 16),  # 2L
    ("Ancient_vs_kaljukostaja", 8),  # 2P
    ("1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1", 18),  # 2Q
)

#: The rounds the rule reports once the area bound stops binding (4 or 5
#: areas: five players cannot occupy more than five). Measured 2026-09-18.
UNBOUNDED_STACK_ROUNDS = 24

#: What the 6 s sample point would measure instead: the walk out of spawn.
#: 34 rounds of 93, measured 2026-09-18 by running the rule at that point.
STACK_ROUNDS_AT_THE_SPAWN_EXIT = 34


def _real_settings():
    """The real ``settings.toml``: the thresholds come from what the tool uses.

    Not the ``settings_file`` fixture, unlike elsewhere in this file: that
    one redirects the archive root to ``tmp_path``, and these tests read the
    **real** archive. The root comes from :func:`conftest.require_parsed`, so
    a copy would be not merely useless but misleading.
    """
    return load_settings(REAL_SETTINGS, env_files=())


def _site_groups(root: Path, map_demo_id: str, limits: ThresholdSettings):
    """One demo's site groups from its own point cloud."""
    df = pl.read_parquet(root / "parsed" / map_demo_id / "callouts.parquet")
    cells = [
        CloudCell(area, x, y, z)
        for area, x, y, z in zip(
            df["area"], df["cell_x"], df["cell_y"], df["cell_z"], strict=True
        )
    ]
    return site_groups(
        cells,
        margin=limits.stack_group_margin,
        separation_min=limits.stack_site_separation_min,
        floor_band_trim=limits.site_floor_band_trim,
        floor_gap_ratio=limits.site_floor_gap_ratio,
        floor_z_weight=limits.site_floor_z_weight,
        bridge_void_share=limits.site_bridge_void_share,
    )


@contextmanager
def _on_grid(seconds: Sequence[float] | None):
    """Read the archive's sample point rows **on one grid** for the duration.

    ``None`` leaves the tables as they are; a list of seconds keeps only the
    time rows whose nominal ``sample_t_s`` is on it. Filtering a denser table
    down to a subset is exactly what a parse at that subset would have
    written: every sample point is converted from its own nominal second and
    is dropped only if it falls after the round ended, so no point's rows
    depend on any other point's.

    **Why a test needs this at all** (Story 4.6). The archive's ``parsed/``
    tables are **not** guaranteed to be on the grid ``settings.toml``
    declares: a settings change and the re-parse that follows it are two
    moments, and on 2026-09-22 nine demos carried a dense grid because a
    branch had been left without re-parsing. A calibration test that read the
    tables bare would pin whichever grid the last parse happened to write --
    and being blind to the grid is the very defect Story 4.6 removed. So the
    grid is named here, and it is also how the ``*_AT_FOUR`` tables are read
    from the four-point subset of an archive that has been dense since Story
    4.5 re-parsed it (2026-09-25).

    The stack's numbers do not move between the two grids (measured: the same
    five rounds, row for row), so this filter changes nothing the stack tests
    below assert; it makes explicit which grid they assert it on.

    The seam is ``_read_parsed`` because ``_aggregate`` reads the archive
    itself and takes no rows as an argument.
    """
    original = aggregate_stage._read_parsed

    def on_grid(archive, demo, name, schema):
        df = original(archive, demo, name, schema)
        if name == "ticks" and seconds is not None:
            df = df.filter(
                (pl.col("sample_kind") != "time")
                | pl.col("sample_t_s").is_in(list(seconds))
            )
        return df

    aggregate_stage._read_parsed = on_grid
    try:
        yield
    finally:
        aggregate_stage._read_parsed = original


def _reports(
    root: Path,
    limits: ThresholdSettings | None = None,
    *,
    seconds: Sequence[float] | None | _GridNotGiven = _SENTINEL_GRID,
    teams: Sequence[str] = CALIBRATION_TEAMS,
):
    """The named teams' reports **in memory**, without changing the archive.

    ``teams`` defaults to :data:`CALIBRATION_TEAMS`, which is what every
    caller before Story 4.8 wanted: the two teams whose thresholds were
    calibrated. It is an argument because the win-loss record's own table
    (:data:`ROUND_RECORDS`) covers :data:`RECORDED_TEAMS` -- the same two
    **and** the scouted team the story's measurement was taken from, which is
    in the archive but was never a calibration subject.

    The stage's own ``run`` would write ``report.json`` into the developer's
    archive; a test must not change the data it measures against.
    ``_aggregate`` is a function of the same module and does exactly what
    ``run`` does before the write -- the underscore is a sign that it should
    not be called from production code, not that it must not be read.

    The sample points default to the ones ``settings.toml`` declares; see
    :func:`_on_grid` for why that is stated rather than assumed.
    """
    settings = _real_settings()
    thresholds = limits or settings.thresholds
    grid = (
        list(settings.parse.snapshot_seconds)
        if seconds is _SENTINEL_GRID
        else seconds
    )
    archive = ArchivePaths(root=root)
    with _on_grid(grid):
        return [
            aggregate_stage._aggregate(
                archive,
                collect_team(archive, team, thresholds),
                thresholds,
                settings.league,
                settings.aggregate,
            )
            for team in teams
        ]


def _stack_points(reports) -> list[tuple]:
    """All the reports' stack hits by sample point, sorted.

    The crowd's areas are part of the row (Story 4.4): without them the table
    would pin how many players were seen but not where -- which is exactly
    what the old rule got wrong.
    """
    found = []
    for report in reports:
        for anomaly in report.anomalies:
            if anomaly.rule != "stack":
                continue
            for entry in anomaly.rounds:
                for point in entry.points:
                    found.append(
                        (
                            anomaly.map_name,
                            entry.map_demo_id,
                            entry.round_no,
                            entry.round_type,
                            point.sample_t_s,
                            anomaly.site,
                            point.players,
                            point.alive,
                            tuple(point.areas),
                        )
                    )
    return sorted(found)


@pytest.mark.archive
def test_the_ancient_site_groups_are_identical_in_all_three_demos() -> None:
    """The same map, three demos, **word for word the same** area grouping.

    This is the whole justification for the cell median. The trajectory-based
    derivation gave 32 % and 94 % coverage from two demos of the same map and
    four contradictory areas; the observation-weighted average five. The cell
    median gives zero, and that is this test's claim.
    """
    root = require_parsed(*ANCIENT_DEMOS)
    limits = _real_settings().thresholds
    found = [_site_groups(root, demo, limits) for demo in ANCIENT_DEMOS]
    assert all(groups is not None for groups in found)
    for groups, demo in zip(found, ANCIENT_DEMOS, strict=True):
        assert groups == ANCIENT_GROUPS, demo
        for area in ANCIENT_SHARED:
            assert area not in groups, f"{demo}: {area}"


@pytest.mark.archive
def test_nuke_is_divided_by_height_not_by_plan_distance() -> None:
    """Nuke used to be silenced here, and that was the wrong answer.

    The plan-view ratio ``separation / (radius_A + radius_B)`` is 0.47-0.54 on
    Nuke against 3.70-5.04 on the other three, so the guard says no -- and it
    is **right about the question it asks**. It asks whether the sites are
    distinguishable side by side, and on Nuke they are not: they are stacked,
    ten cells apart in height and on top of each other in plan view.

    Silence was the measure answering the wrong question. Measured
    2026-09-12, the two sites' cells share no height at all (A -13..-11,
    B -25..-19, six empty cells between), and no other map in the archive has
    a gap -- Anubis's bands touch at -1..0, Ancient's and Inferno's overlap.
    So height is what tells them apart, and Story 4.3 divides Nuke on it.

    **The plan-view guard is unchanged and still says no**, which is why this
    test asserts both halves: the ratio still fails, and the map is divided
    anyway.
    """
    root = require_parsed(*NUKE_DEMOS)
    limits = _real_settings().thresholds
    for demo in NUKE_DEMOS:
        # Divided now.
        found = _site_groups(root, demo, limits)
        assert found is not None, demo
        assert found["BombsiteA"] == "A" and found["BombsiteB"] == "B", demo
        # And the three areas that span the void between the floors are held
        # out of both groups -- the product owner names these as the ways
        # between the levels, and the derivation finds them without being
        # told. Measured: Ramp 40 % of its cells in the void, Secret 31 %,
        # Vents 21 %, every other area 3 % or less.
        for bridge in ("Ramp", "Vents", "Secret"):
            assert bridge not in found, f"{demo}: {bridge}"
        # Still refused by the plan-view guard: raise the floor gap past
        # Nuke's own six cells and the stacked branch never fires, leaving
        # exactly the behaviour this test used to assert.
        flat = limits.model_copy(update={"site_floor_gap_ratio": 1.0})
        assert _site_groups(root, demo, flat) is None, demo


@pytest.mark.archive
def test_the_band_trim_is_a_threshold_and_the_plateau_is_where_it_sits() -> None:
    """The percentile is not an implementation detail; the answer turns on it.

    ``m_szLastPlaceName`` is the **last** name a player entered, so a handful
    of each site's cells carry its name from somewhere else, and one of those
    on another floor stretches the band across the very gap being measured.
    The band is therefore trimmed -- and the trim is a threshold, with a
    plateau and two edges, both of which this pins.

    Measured over the archive 2026-09-12 at twelve points, asking which demos
    come out stacked:

    * **0.00** -- nothing at all, Nuke included: the raw extremes always
      overlap, because the strays reach into the other floor by definition.
    * **0.01** -- only **two** of Nuke's three demos. This is the worst of
      the three failures and the reason the lower edge is asserted: the map
      would divide one way in one demo and another way in the next, and the
      archive would hold two contradictory answers for the same map with
      nothing reporting a problem.
    * **0.02 .. 0.10** -- exactly the three Nuke demos. The shipped 0.05 is
      the middle of this.
    * **0.12 and above** -- Anubis joins, whose bands merely touch. A map
      that is not stacked would be divided by height.

    The upper edge is also pinned by the model: ``MAX_SITE_FLOOR_BAND_TRIM``
    is the plateau's own top, so 0.12 cannot be configured at all. It is
    reachable here only by calling the function directly, which is what makes
    it worth asserting -- the bound is a claim about the measurement, not a
    typing guard.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds

    def stacked_demos(trim: float) -> set[str]:
        found = set()
        for demo in CALIBRATION_DEMOS:
            at = limits.model_copy(update={"site_floor_band_trim": trim})
            plan_blind = at.model_copy(
                update={"stack_site_separation_min": 20.0}
            )
            # With the plan-view guard raised out of reach, a demo that still
            # produces groups can only have done so through the floor branch.
            if _site_groups(root, demo, plan_blind) is not None:
                found.add(demo)
        return found

    assert stacked_demos(0.05) == set(NUKE_DEMOS)
    assert stacked_demos(0.02) == set(NUKE_DEMOS)
    assert stacked_demos(0.10) == set(NUKE_DEMOS)
    # The lower edge: one Nuke demo drops out, so the map disagrees with
    # itself. Asserting the count is the point -- which demo survives is an
    # accident of its cells.
    assert len(stacked_demos(0.01)) == 1
    assert stacked_demos(0.01) < set(NUKE_DEMOS)
    # The upper edge: a map whose bands only touch becomes stacked.
    assert stacked_demos(0.12) > set(NUKE_DEMOS)
    # And that value is not configurable, because the ceiling is the plateau.
    with pytest.raises(ValidationError):
        ThresholdSettings(pistol_rounds=[1, 13], site_floor_band_trim=0.12)


@pytest.mark.archive
def test_every_other_map_does_give_site_groups() -> None:
    """The guard's other direction: it silences only what it should.

    Without this claim, raising the threshold would silence the whole rule
    and not one test would report it -- zero hits would look like a measured
    negative.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds
    speaking = {
        demo
        for demo in CALIBRATION_DEMOS
        if _site_groups(root, demo, limits) is not None
    }
    # Every demo is divided as of Story 4.3: the flat maps by plan distance
    # and Nuke by height. The claim this test was written for still holds and
    # is now stronger -- a threshold raised far enough would silence the rule,
    # and here that shows as a missing demo rather than as zero hits.
    assert speaking == set(CALIBRATION_DEMOS)


@pytest.mark.archive
def test_the_stack_rule_finds_exactly_the_calibrated_sample_points() -> None:
    """The calibration's hit table row by row, from both teams.

    The table is **by sample point** and not by round; that the two now give
    the same count is a property of the rule and not of the table.

    **The subject's rows are identified from the lineup ids**, not from the
    ``classified/`` directory's name: the same demo is in the archive twice,
    once for each team, and the wrong source gave 7 hits out of 59 rounds in
    the calibration's first version -- from the opponent's rounds.

    Since Story 4.4 the table is one row per round, because the rule reads one
    sample point; Anubis round 4 was the pair 15 s / 30 s that this structure
    was built for, and it is now its 15 s row alone.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds
    found = _stack_points(_reports(root))
    assert found == STACK_TABLE
    assert len(found) == STACK_HITS
    assert len({(row[1], row[2]) for row in found}) == STACK_ROUNDS
    # Every row is checked against the SHIPPED thresholds and against the
    # demo's own derived groups.
    #
    # **What this loop does and does not prove.** Three of the four threshold
    # comparisons are true by construction -- the rule writes ``sample_t_s``
    # from ``stack_sample_s``, and the player and area bounds are its own
    # filters -- so they catch a changed setting that the table above did not
    # follow, and nothing else. The claim that once stood here, that a table
    # edited to match a broken rule would fail here, is false: measured under
    # a mutated rule (``<=`` instead of the sample point) all of them hold
    # while 35 wrong rows are emitted.
    #
    # The group membership below is the part that is **not** enforced
    # anywhere in the pipeline: ``Anomaly`` cannot check it (the area ->
    # group map is derived per demo and is not in the report, see
    # ``_check_stack_fields``), and the rule reads one group at a time, so a
    # row whose areas came from two groups could only ever be caught here.
    for row in found:
        _, demo, _, _, seconds, site, players, alive, areas = row
        assert seconds == limits.stack_sample_s
        assert players >= limits.stack_min_players
        assert 0 < len(areas) <= limits.stack_max_areas
        assert players <= alive
        groups = _site_groups(root, demo, limits)
        assert groups is not None
        assert {groups.get(area) for area in areas} == {site}


@pytest.mark.archive
def test_the_stack_coverage_says_what_it_could_not_see() -> None:
    """93 scanned out of 93; nothing silenced, as of Story 4.3.

    The coverage is three numbers and not one, and it is still three even
    when the third is zero -- that is the point of keeping it. Until
    2026-09-12 stack saw 66 CT rounds of 93 and the missing 27 were Nuke's
    two demos, silenced because the sites could not be told apart in plan
    view. They are told apart by height instead now, so every round is
    scanned and ``demos_without_site_groups`` is empty.

    **The zero is an assertion and not an omission.** A demo that goes quiet
    again must show up here rather than as a quietly smaller denominator,
    which is the failure the three numbers exist to prevent.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    reports = _reports(root)
    ct_rounds = sum(r.anomaly_scan.crunch_rounds for r in reports)
    scanned = sum(r.anomaly_scan.stack_rounds for r in reports)
    silenced = {
        demo
        for r in reports
        for demo in r.anomaly_scan.demos_without_site_groups
    }
    assert ct_rounds == STACK_CT_ROUNDS
    assert scanned == STACK_SCANNED
    assert ct_rounds - scanned == STACK_SILENCED_ROUNDS
    assert silenced == set()


#: What the coverage read while Nuke was a blind spot, and what it reads
#: again the moment the floor branch is switched off. Measured 2026-09-12.
SILENCED_ROUNDS_WITHOUT_THE_FLOOR_BRANCH = 27
SCANNED_WITHOUT_THE_FLOOR_BRANCH = 66


@pytest.mark.archive
def test_switching_the_floor_branch_off_silences_nuke_again() -> None:
    """The silenced path still works, and this is what proves it.

    **An assertion that a set is empty does not fail when the mechanism that
    fills it breaks.** Until Story 4.3 the suite pinned a non-empty
    ``demos_without_site_groups`` end to end through the aggregation, because
    Nuke was genuinely silent. Nuke is now divided by height, so the sibling
    test above can only assert ``set()`` -- and every way of breaking the
    coverage would leave that green.

    This test restores the old claim as a live guard instead of retiring it.
    With ``site_floor_gap_ratio`` raised to 1.0 -- inside the model's ceiling
    of 2.0, and above Nuke's measured 0.75 -- the map is no longer read as
    stacked, the plan-view separation guard applies again, and the archive
    goes back to exactly the numbers it had before: **66 rounds scanned of
    93, 27 silenced, both Nuke demos named**.

    It therefore pins three things at once: that a silenced demo is still
    recorded by name rather than vanishing from the denominator, that the
    difference between the two denominators is real and not an artefact, and
    that the floor branch -- not some other change -- is what brought those
    27 rounds in.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    settings = _real_settings()
    without = settings.thresholds.model_copy(
        update={"site_floor_gap_ratio": 1.0}
    )
    reports = _reports(root, without)
    ct_rounds = sum(r.anomaly_scan.crunch_rounds for r in reports)
    scanned = sum(r.anomaly_scan.stack_rounds for r in reports)
    silenced = {
        demo
        for r in reports
        for demo in r.anomaly_scan.demos_without_site_groups
    }
    assert ct_rounds == STACK_CT_ROUNDS
    assert scanned == SCANNED_WITHOUT_THE_FLOOR_BRANCH
    assert ct_rounds - scanned == SILENCED_ROUNDS_WITHOUT_THE_FLOOR_BRANCH
    assert silenced == set(NUKE_DEMOS)


@pytest.mark.archive
def test_five_defenders_are_the_rules_real_extreme_not_an_empty_set() -> None:
    """``stack_min_players = 5`` gives 2 rounds, not 0.

    The threshold is therefore **genuinely read from the settings** and not
    hard-coded, and five is the rule's genuine extreme: the two rounds on
    which the whole defence stands on one area (``Alley`` and ``Middle``).

    It is also why the threshold stays at **four**: the product owner's only
    stack on the first blind list is ``Anubis_vs_ryhmarama`` round 4, which
    is four players, and five would drop exactly it.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds.model_copy(
        update={"stack_min_players": 5}
    )
    rounds = {(row[1], row[2]) for row in _stack_points(_reports(root, limits))}
    assert sorted(rounds) == [
        ("1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1", 14),
        ("inferno_vs_ryhmarama", 2),
    ]
    at_four = {(row[1], row[2]) for row in _stack_points(_reports(root))}
    assert ("Anubis_vs_ryhmarama", 4) in at_four - rounds


@pytest.mark.archive
def test_the_rule_fires_on_none_of_the_rounds_he_read_as_normal() -> None:
    """The story's central claim, over the population the rule can scan.

    Twenty-six of the product owner's "not a stack" verdicts are the
    subject's own CT rounds. The shipped rule fires on **none** of them, and
    the five rounds it does report are four of his stack-like rounds and one
    he named a push. Nothing else in the suite states this: the hit table
    pins what the rule found, and this pins what it did not.

    The other 7 of his 33 "not a stack" verdicts are the opponent's CT
    rounds. They are not asserted here, because the rule cannot scan them --
    that is the population correction this story's text needed.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    found = {(row[1], row[2]) for row in _stack_points(_reports(root))}
    assert len(JUDGED_NOT_A_STACK_IN_SCOPE) == 26
    assert found & set(JUDGED_NOT_A_STACK_IN_SCOPE) == set()
    assert sorted(JUDGED_NOT_A_STACK_AT_THREE_AREAS) == sorted(
        set(JUDGED_NOT_A_STACK_AT_THREE_AREAS) & set(JUDGED_NOT_A_STACK_IN_SCOPE)
    )


@pytest.mark.archive
def test_one_more_area_starts_reporting_the_rounds_he_read_as_normal() -> None:
    """Why ``stack_max_areas`` is 2, measured against his own judgements.

    The bound is the condition the whole rewrite turns on, so its price has
    to be visible: loosened by one, the rule reports 13 rounds instead of 5,
    and **every one of the eight added rounds is a round the product owner
    read as not a stack** -- "normi", "normaali", "ei ihan stack ainakaan".

    This is the guard that keeps the threshold honest. A pinned count alone
    would stay green if the rule started firing on other rounds instead; the
    named rounds are what tie the number to his answers.

    The other side of the bound is in
    ``test_five_defenders_are_the_rules_real_extreme_not_an_empty_set`` and in
    the rule's own tests: at 1 the rule finds only the two one-area rounds and
    loses his ``BackofB`` + ``BombsiteB`` stack.
    """
    root = require_parsed(*CALIBRATION_DEMOS)

    def rounds(value: int) -> set[tuple[str, int]]:
        limits = _real_settings().thresholds.model_copy(
            update={"stack_max_areas": value}
        )
        return {(row[1], row[2]) for row in _stack_points(_reports(root, limits))}

    at_two = {(row[1], row[2]) for row in _stack_points(_reports(root))}
    at_three = rounds(3)
    assert len(at_two) == STACK_ROUNDS
    assert at_two < at_three
    assert sorted(at_three - at_two) == sorted(JUDGED_NOT_A_STACK_AT_THREE_AREAS)
    # The looser values are pinned too, and not only the neighbour: at 4 and
    # at 5 the bound stops excluding anything the archive contains, and the
    # rule then reports 24 rounds -- 18 of them rounds he read as normal.
    # Five is therefore not "useless but harmless": it is the definition this
    # story replaced, and the settings say so where the value is chosen.
    assert len(rounds(4)) == len(rounds(5)) == UNBOUNDED_STACK_ROUNDS
    assert len(rounds(4) & set(JUDGED_NOT_A_STACK_IN_SCOPE)) == 18
    # And the tight side: one area finds only the two one-area crowds and
    # loses his BackofB + BombsiteB stack, which is why the bound is 2.
    assert rounds(1) < at_two
    assert ("Anubis_vs_ryhmarama", 4) in at_two - rounds(1)


@pytest.mark.archive
def test_the_setup_point_is_not_the_walk_out_of_spawn() -> None:
    """Why ``stack_sample_s`` is 15 s and not the first sample point.

    At 6 s the rule would fire on 34 rounds of 93 -- on Nuke nearly every
    round, where ``Hell`` and ``Outside`` are simply the way out of spawn.
    The players have not had time to spread out, so the measurement would be
    the map's own geometry and not the defence's choice.

    The number is measured here rather than argued: a sample point that costs
    nothing to move is not a measured setting.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    limits = _real_settings().thresholds.model_copy(update={"stack_sample_s": 6.0})
    rounds = {(row[1], row[2]) for row in _stack_points(_reports(root, limits))}
    assert len(rounds) == STACK_ROUNDS_AT_THE_SPAWN_EXIT
    assert len(rounds) > STACK_ROUNDS


@pytest.mark.archive
def test_the_hits_stand_where_the_players_really_are() -> None:
    """Every row's areas are read back from the archive's own ticks.

    The row's area is the crowd's own since Story 4.4, and this is the test
    that the report does not invent it: for every hit, the players standing on
    the named areas at the named sample point are counted from
    ``ticks.parquet`` -- and they have to be the row's own player count.

    It also pins the part the old rule got wrong: **four of the five rounds
    have nobody at all on the site's own area** -- the old onsite condition
    would silence all four, and three of them are rounds the product owner
    named a stack. Only ``Anubis_vs_ryhmarama`` round 4 has a player there.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    reports = _reports(root)
    cache: dict[str, pl.DataFrame] = {}
    without_anybody_on_the_site = 0
    hits = 0
    for report in reports:
        lineups = list(report.team.lineup_keys)
        for anomaly in report.anomalies:
            if anomaly.rule != "stack":
                continue
            site = SITE_AREAS[anomaly.site]
            for entry in anomaly.rounds:
                demo = entry.map_demo_id
                if demo not in cache:
                    cache[demo] = pl.read_parquet(
                        root / "parsed" / demo / "ticks.parquet"
                    )
                rows = cache[demo].filter(
                    pl.col("lineup_key").is_in(lineups)
                    & (pl.col("round_no") == entry.round_no)
                    & (pl.col("side") == "CT")
                    & (pl.col("sample_kind") == "time")
                    & pl.col("is_alive").fill_null(False)
                )
                for point in entry.points:
                    hits += 1
                    at_point = rows.filter(
                        pl.col("sample_t_s") == point.sample_t_s
                    )
                    on_the_areas = at_point.filter(
                        pl.col("area").is_in(point.areas)
                    )
                    assert on_the_areas["player_id"].n_unique() == point.players
                    assert at_point["player_id"].n_unique() == point.alive
                    if at_point.filter(pl.col("area") == site).is_empty():
                        without_anybody_on_the_site += 1
    assert hits == STACK_HITS
    assert without_anybody_on_the_site == 4


# --- Two rules that must not read the sampling grid (Story 4.6) -----------------
#
# Neither the CT advance nor the crunch had a hit-count constant before this
# story, and that is exactly why a grid change could re-calibrate them in
# silence: the archive suite ran green while the advance went from 10 hits on
# 6 rounds to 45 on 9 and the crunch from 5 on 4 to 2 on 2. The tables below
# were **re-derived by running the rules over the archive** on 2026-09-22 --
# not copied from any document.
#
# Two grids since Story 4.5. The ``*_AT_FOUR`` tables are the calibration,
# read from the four-point subset (the points the report prints); the plain
# tables are the same rules on the shipped fourteen-point grid, re-derived by
# running after the archive was re-parsed (2026-09-25). ``_on_grid`` names the
# grid each test reads rather than assuming the archive carries it.

#: The CT advance on the **four-point calibration grid**: 10 hits on 6 rounds.
#:
#: This is the table the rule was calibrated on (2026-09-22), and since
#: Story 4.5 it is read from the **four-point subset** of the re-parsed
#: archive -- the points the report prints, ``[aggregate]
#: .route_sample_seconds``. It still reproduces row for row, which is the
#: evidence that the fourteen-point grid is a superset of the calibration and
#: not a replacement of it.
#:
#: A hit is one sample point on one round, so the two figures differ on
#: purpose: three of the six rounds hit at both 15 s and 30 s, and
#: ``inferno_vs_ryhmarama`` round 2 hits on two areas at 30 s.
ADVANCE_HITS_AT_FOUR = 10
ADVANCE_ROUNDS_AT_FOUR = 6

#: The hit table: (map, demo, round, type, moment, area, players).
ADVANCE_TABLE_AT_FOUR = sorted(
    [
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 15.0, 'TSideUpper', 1),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 30.0, 'TSideLower', 2),
        ('de_ancient', 'Ancient_vs_kaljukostaja', 4, 'eco', 30.0, 'TSideUpper', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 3, 'eco', 15.0, 'Bridge', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 6, 'half', 30.0, 'OutsideLong', 1),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 15.0, 'OutsideLong', 2),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 30.0, 'Bridge', 3),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 15.0, 'Middle', 5),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 30.0, 'Middle', 1),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 30.0, 'SecondMid', 1),
    ]
)

#: The crunch on the same four-point subset: 5 hits on 4 rounds.
#:
#: The same five the rule found when its look-back was "the previous sample
#: point". Reproducing them exactly was Story 4.6's stop condition, so they
#: are asserted row for row and not only as a count.
CRUNCH_HITS_AT_FOUR = 5
CRUNCH_ROUNDS_AT_FOUR = 4

#: The hit table: (map, demo, round, type, moment, area, players, sources).
#:
#: The sources are the **sample point's** directions, the simultaneous ones
#: (Story 4.5); at four points every row here is its round's only point for
#: its area, so they equal what the round-level list said.
CRUNCH_TABLE_AT_FOUR = sorted(
    [
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 30.0, 'TSideLower', 2,
         ('SideEntrance', 'TSideUpper')),
        ('de_anubis', 'Anubis_vs_ryhmarama', 10, 'full', 15.0, 'OutsideLong', 3,
         ('Alley', 'BombsiteB', 'LowerTunnel')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 15.0, 'OutsideLong', 2,
         ('Alley', 'BombsiteB')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 30.0, 'Bridge', 3,
         ('BombsiteB', 'Middle', 'OutsideLong')),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 15.0, 'Middle', 5,
         ('Arch', 'TopofMid')),
    ]
)

#: The CT advance on the **shipped fourteen-point grid**: 44 hits on 9 rounds,
#: re-derived by running the rule over the re-parsed archive on 2026-09-25
#: (Story 4.5, step 6) -- not scaled from the four-point table.
#:
#: The rounds the four points could not see are the moments between 15 s and
#: 30 s: ``Nuke_vs_imuaijat`` round 5 in ``Lobby`` at 21 and 24 s,
#: ``1-a52ebff2`` round 19, ``anubis_vs_RCAVE_VETERANS`` round 2 in ``Canal`` at
#: 12 s, and ``1-79f71e00`` round 24, whose ``Trophy`` is the one row an
#: orientation change adds. ``Ancient_vs_kaljukostaja`` round 4 is gone:
#: ``TSideUpper`` loses its orientation on the dense grid
#: (:data:`ORIENTATION_LOST_AT_FOURTEEN`). The product owner ruled that the
#: dense observation is the one that counts: the rules must not read less
#: than the analysis does (2026-09-23).
ADVANCE_HITS = 44
ADVANCE_ROUNDS = 9

#: The hit table: (map, demo, round, type, moment, area, players).
ADVANCE_TABLE = sorted(
    [
        ('de_ancient', '1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1', 19, 'force', 24.0, 'TSideLower', 1),
        ('de_ancient', '1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1', 19, 'force', 27.0, 'Ruins', 1),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 21.0, 'TSideLower', 1),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 24.0, 'TSideLower', 2),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 27.0, 'TSideLower', 3),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 30.0, 'TSideLower', 2),
        ('de_anubis', 'Anubis_vs_ryhmarama', 3, 'eco', 15.0, 'Bridge', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 3, 'eco', 18.0, 'Bridge', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 3, 'eco', 21.0, 'Bridge', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 3, 'eco', 24.0, 'Ruins', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 6, 'half', 18.0, 'Bridge', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 6, 'half', 21.0, 'Bridge', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 6, 'half', 24.0, 'Ruins', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 6, 'half', 27.0, 'OutsideLong', 1),
        ('de_anubis', 'Anubis_vs_ryhmarama', 6, 'half', 30.0, 'OutsideLong', 1),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 2, 'force', 12.0, 'Canal', 1),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 15.0, 'OutsideLong', 2),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 18.0, 'OutsideLong', 2),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 18.0, 'Ruins', 1),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 21.0, 'Ruins', 3),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 24.0, 'Ruins', 3),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 27.0, 'Bridge', 3),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 27.0, 'Ruins', 1),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 30.0, 'Bridge', 3),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 9.0, 'Middle', 3),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 12.0, 'Middle', 4),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 15.0, 'Middle', 5),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 18.0, 'Middle', 5),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 21.0, 'Middle', 4),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 21.0, 'SecondMid', 1),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 24.0, 'Middle', 2),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 24.0, 'SecondMid', 2),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 27.0, 'Middle', 2),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 30.0, 'Middle', 1),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 30.0, 'SecondMid', 1),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 18.0, 'Trophy', 2),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 21.0, 'Lobby', 1),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 21.0, 'Trophy', 1),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 24.0, 'Lobby', 1),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 27.0, 'Lobby', 1),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 27.0, 'Squeaky', 1),
        ('de_nuke', '1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 24, 'force', 30.0, 'Trophy', 1),
        ('de_nuke', 'Nuke_vs_imuaijat', 5, 'eco', 21.0, 'Lobby', 1),
        ('de_nuke', 'Nuke_vs_imuaijat', 5, 'eco', 24.0, 'Lobby', 1),
    ]
)

#: The crunch on the shipped grid: 11 hits on 5 rounds, measured the same way.
#:
#: The rule's round moves from 4 to 5: ``1-a52ebff2`` round 20 (a full buy)
#: enters ``TSideLower`` from ``Middle`` and ``TSideUpper`` at 27 s, a moment
#: four points did not sample. ``anubis_vs_RCAVE_VETERANS`` round 3 now also
#: reaches ``Ruins`` and, at 27 and 30 s, ``Bridge`` from two different pairs
#: of directions -- the state that crashed the round-level source list and
#: moved the directions onto the sample point.
CRUNCH_HITS = 11
CRUNCH_ROUNDS = 5

#: The hit table: (map, demo, round, type, moment, area, players, sources);
#: the sources are the point's own.
CRUNCH_TABLE = sorted(
    [
        ('de_ancient', '1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1', 20, 'full', 27.0, 'TSideLower', 2, ('Middle', 'TSideUpper')),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 24.0, 'TSideLower', 2, ('Middle', 'TSideUpper')),
        ('de_ancient', 'ANCIENT_vs_RCAVE_VETERANS', 18, 'eco', 27.0, 'TSideLower', 3, ('Middle', 'TSideUpper')),
        ('de_anubis', 'Anubis_vs_ryhmarama', 10, 'full', 15.0, 'OutsideLong', 3, ('Alley', 'BombsiteB', 'LowerTunnel')),
        ('de_anubis', 'Anubis_vs_ryhmarama', 10, 'full', 18.0, 'OutsideLong', 3, ('Alley', 'BombsiteB')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 15.0, 'OutsideLong', 2, ('Alley', 'BombsiteB')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 21.0, 'Ruins', 3, ('BackofB', 'BombsiteB')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 24.0, 'Ruins', 3, ('BombsiteB', 'OutsideLong')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 27.0, 'Bridge', 3, ('Middle', 'OutsideLong')),
        ('de_anubis', 'anubis_vs_RCAVE_VETERANS', 3, 'eco', 30.0, 'Bridge', 3, ('MidDoors', 'Ruins')),
        ('de_inferno', 'inferno_vs_ryhmarama', 2, 'eco', 15.0, 'Middle', 5, ('Arch', 'TopofMid')),
    ]
)

#: What the look-back does to the four-point grid, swept 2026-09-22:
#: ``lookback_s -> the number of rounds the crunch finds``.
#:
#: Every value up to 9 s reproduces the four rounds, because on this grid the
#: source of a 15 s target is the 6 s point for any look-back of 9 or less,
#: and the source of a 30 s target is the 15 s point for any look-back of 15
#: or less. From 12 s up the 15 s target loses its source altogether and two
#: rounds go with it.
#:
#: **9.0 is therefore the largest value that reproduces the grid it replaces**,
#: and that is why it is the shipped one: a shorter value would reproduce the
#: same rounds while asking a narrower question than the rule used to ask.
#:
#: Read from the four-point subset since the archive was re-parsed (Story
#: 4.5); it reproduces exactly.
LOOKBACK_SWEEP = {
    3.0: 4,
    6.0: 4,
    9.0: 4,
    12.0: 2,
    15.0: 2,
    24.0: 1,
}

#: The same sweep on the **shipped fourteen-point grid**, measured 2026-09-25
#: on the re-parsed archive: ``lookback_s -> crunch rounds``.
#:
#: **9.0 is inside a flat stretch here too, and not at its edge.** Every value
#: from 4.5 to 12 finds five rounds; 3.0 finds two and 15.0 four. So the
#: four-point argument -- 9.0 is the largest value that reproduces -- does not
#: carry over: on this grid 12.0 reproduces the same count. **Recorded, not
#: acted on.** Re-choosing the threshold would be re-tuning on a grid the
#: product owner has not ruled on, so the shipped 9.0 stands and this is what
#: the next calibration has to start from.
LOOKBACK_SWEEP_SHIPPED = {
    3.0: 2,
    4.5: 5,
    6.0: 5,
    9.0: 5,
    12.0: 5,
    15.0: 4,
    24.0: 1,
}

#: The demo the two cross-grid measurements below were taken on, 2026-09-23,
#: when it was the archive's only fourteen-point demo.
#:
#: Its source file is in neither ``demos/`` nor ``import/``, so it cannot be
#: parsed again; since the archive was re-parsed on fourteen points (Story
#: 4.5) it is on the same grid as every other demo, with the same parse
#: parameters. The measurements stay this demo's and are stated as such.
DENSE_DEMO = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1"

#: The same sweep on :data:`DENSE_DEMO`'s own grid, measured 2026-09-23:
#: ``lookback_s -> the number of crunch rounds found on that demo``.
#:
#: **This is what :data:`LOOKBACK_SWEEP` cannot see.** On the four-point grid
#: every value in ``(0, 9]`` gives the same four rounds, because the points
#: are 9 and 15 seconds apart and ``15 - x`` lands on 6 s for all of them. The
#: flat stretch is arithmetic, not evidence, and a threshold picked inside it
#: is picked on a grid that cannot tell its candidates apart. Three-second
#: spacing can: 4.5 and 6.0 find a round here that 9.0 does not.
#:
#: **Not an argument for a different value.** The shipped 9.0 was calibrated
#: against the four-point grid and reproduces the rule it replaces there; this
#: records that the calibration's resolution is the grid's. The whole archive
#: is now dense, and :data:`LOOKBACK_SWEEP_SHIPPED` is that re-measurement.
DENSE_DEMO_LOOKBACK_SWEEP = {
    3.0: 0,
    4.5: 1,
    6.0: 1,
    9.0: 0,
}

#: The crunch's residual across the two grids, measured 2026-09-23 on
#: :data:`DENSE_DEMO` with the shipped look-back:
#: ``sample point -> (source look-ups that differ, source look-ups shared)``.
#:
#: **The question is density-free; the answer is not, and this is the size of
#: the difference.** The source is the latest point at or before
#: ``t - 9``, so at four points a 30 s target falls back to 15 s while at
#: fourteen it lands on 21 s. 15 s agrees exactly on both grids, because
#: ``15 - 9 = 6`` is a point of each -- which is why the split by sample point
#: is recorded and not only the total: it is what identifies the cause.
LOOKBACK_RESIDUAL = {
    15.0: (0, 136),
    30.0: (40, 107),
    45.0: (27, 77),
}

#: The measurement the cross-density claim rests on: the eight calibration
#: demos' orientation observations, area by area, on both grids.
#:
#: **Data in the repository and not values copied into an assertion.** The
#: numbers are the input the rule reads (``AreaObservations`` per area, plus
#: each grid's point count); the test below feeds them to the real
#: ``t_side_shares`` with the real thresholds and derives the verdict. So the
#: guard goes red when the gate's arithmetic changes, when
#: ``advance_area_min_observations_per_point`` changes and when
#: ``advance_t_share`` changes -- and it needs no archive at all, which is the
#: whole point: the first version of this test measured the fourteen-point
#: side from the archive, and the archive carried that grid only because it
#: was in an inconsistent state. **A fault is not a fixture.** (Since Story
#: 4.5 the archive is on fourteen points by design, and both columns are
#: checked against it -- see below.)
#:
#: Provenance, 2026-09-22: seven of the eight demos were parsed again on the
#: fourteen-point grid into a scratch archive for this measurement, and the
#: eighth (``1-79f71e00-...-1-1``, whose source file no longer exists) was
#: read from the archive, which still carries it densely. The four-point
#: counts were derived by filtering the dense tables and then checked against
#: the real archive's own four-point tables -- zero differences over the seven
#: -- which is both the tie to the archive and the evidence that filtering a
#: grid down is the same thing as parsing on it. The check is kept as a test
#: (``test_the_recorded_orientation_counts_are_the_archives_own``).
#:
#: **What is checked against what.** Since Story 4.5 re-parsed the archive on
#: the fourteen-point grid, **every entry of both columns, of all eight demos,
#: is checked against the real archive** every ``-m archive`` run
#: (``test_the_recorded_orientation_counts_are_the_archives_own``): the
#: fourteen-point column as parsed, the four-point column by filtering to the
#: four points. The fourteen-point column of seven demos was not checkable
#: before that -- it came from a scratch archive -- and it matched the
#: re-parse with zero differences. The internal-consistency guards
#: (``test_every_recorded_observation_is_load_bearing``) stay: they run
#: without an archive.
ORIENTATION_BY_GRID = json.loads(
    (Path(__file__).parent / "data" / "orientation_by_grid.json").read_text(
        encoding="utf-8"
    )
)

#: The oriented areas the fourteen-point grid does **not** reproduce, measured
#: 2026-09-22 with the shipped thresholds: ``(demo, area)``.
#:
#: **This is the residual the story did not remove, recorded rather than
#: smoothed away.** Normalising the observation gate by the grid's size makes
#: the four-point behaviour exact by arithmetic, but it does not make the
#: orientation density-invariant, and the reason is measured: an area's
#: observations grow with the grid only in proportion to how long it is
#: occupied. ``Water`` and ``TSpawn`` are occupied at the start of a round and
#: nowhere else, so their rows grow 1.08x-2.38x while the gate's divisor grows
#: 3.5x, and they fall out.
#:
#: The last three are not the gate's doing at all but ``advance_t_share``'s,
#: which the story's spec put out of scope as "already density-independent".
#: It is a share, but it is measurably not invariant: a denser grid weights a
#: round's later seconds differently, and ``TSideUpper`` goes 0.800 -> 0.704
#: and 0.818 -> 0.645 while ``Tunnels`` goes 0.917 -> 0.778 -- across the 0.80
#: bound in all three.
ORIENTATION_LOST_AT_FOURTEEN = (
    # The observation gate: occupied early only, so the rows do not multiply.
    ('1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 'TSpawn'),
    ('ANCIENT_vs_RCAVE_VETERANS', 'Water'),
    ('Ancient_vs_kaljukostaja', 'TSpawn'),
    ('Ancient_vs_kaljukostaja', 'Water'),
    ('Anubis_vs_ryhmarama', 'TSpawn'),
    ('Nuke_vs_imuaijat', 'TSpawn'),
    ('inferno_vs_ryhmarama', 'TSpawn'),
    # advance_t_share crosses its own bound; not the gate's doing.
    ('1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 'Tunnels'),
    ('ANCIENT_vs_RCAVE_VETERANS', 'TSideUpper'),
    ('Ancient_vs_kaljukostaja', 'TSideUpper'),
)

#: The areas the denser grid orients that the four-point grid does not.
#:
#: ``Squeaky`` is the gate's own and is a **genuinely new observation**: 17
#: rows over four points is 4.25 per point and below the bound, 82 over
#: fourteen is 5.86 and above it -- the denser grid simply saw the area
#: better. ``Trophy`` is ``advance_t_share`` again, 0.789 -> 0.815.
ORIENTATION_GAINED_AT_FOURTEEN = (
    ('1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1', 'Trophy'),
    ('Nuke_vs_imuaijat', 'Squeaky'),
)


def _rule_points(reports, rule: str) -> list[tuple]:
    """All the reports' hits of one rule by sample point, sorted."""
    found = []
    for report in reports:
        for anomaly in report.anomalies:
            if anomaly.rule != rule:
                continue
            for entry in anomaly.rounds:
                for point in entry.points:
                    row = (
                        anomaly.map_name,
                        entry.map_demo_id,
                        entry.round_no,
                        entry.round_type,
                        point.sample_t_s,
                        anomaly.area,
                        point.players,
                    )
                    if rule == "crunch":
                        # The point's own directions, which are the
                        # simultaneous ones (Story 4.5), and not the round's
                        # union across its points.
                        row += (tuple(point.sources),)
                    found.append(row)
    return sorted(set(found))


def _oriented_on(demo: str, grid: str) -> set[str]:
    """The demo's T areas on one grid, from the recorded observations.

    The observations are read from :data:`ORIENTATION_BY_GRID` and the verdict
    is then computed by the **rule's own function** with the **real**
    thresholds. So what is recorded is the measurement and what is executed is
    the code under test: a change to the gate's arithmetic, to
    ``advance_area_min_observations_per_point`` or to ``advance_t_share``
    moves this and the test says so.
    """
    body = ORIENTATION_BY_GRID["demos"][demo]
    limits = _real_settings().thresholds
    observations = {
        area: AreaObservations(t=counts[grid][0], total=counts[grid][1])
        for area, counts in body["areas"].items()
        if counts[grid] is not None
    }
    return set(
        t_side_shares(
            observations,
            t_share_min=limits.advance_t_share,
            min_observations_per_point=(
                limits.advance_area_min_observations_per_point
            ),
            sample_points=body["points"][grid],
        )
    )


def _sources_by_round(ticks: pl.DataFrame) -> dict[tuple[int, str, float], str]:
    """``(round, player, sample point) -> the crunch's source area``.

    The rows are turned into the rule's records by ``domain.aggregate``'s own
    :func:`~pappascout.domain.aggregate._presence` and handed to the rule's
    own ``_source_areas`` with the real look-back, so what is measured here is
    the shipped selector and not a second implementation of it. The grouping
    is per round for the same reason the rule is: a look-back does not reach
    into the round before.
    """
    by_round: dict[int, list] = {}
    for tick in ticks.to_dicts():
        by_round.setdefault(int(tick["round_no"]), []).append(_presence(tick))
    lookback = _real_settings().thresholds.crunch_lookback_s
    found: dict[tuple[int, str, float], str] = {}
    for round_no, presences in by_round.items():
        rows = [row for row in presences if _is_ct_time_row(row)]
        for (player, seconds), area in _source_areas(rows, lookback).items():
            found[(round_no, player, seconds)] = area
    return found


def _archive_orientation(root: Path, demo: str) -> tuple[str, dict]:
    """The demo's grid name and its orientation counts, from the archive.

    The grid is read from the table and not assumed: the archive may hold
    demos on either grid (see :func:`_on_grid`), and the recorded measurement
    has to be checked against the grid the demo is really on.
    """
    ticks = pl.read_parquet(root / "parsed" / demo / "ticks.parquet")
    points = (
        ticks.filter(pl.col("sample_kind") == "time")["sample_t_s"]
        .unique()
        .to_list()
    )
    grids = ORIENTATION_BY_GRID["grids"]
    grid = next(
        (name for name, seconds in grids.items() if sorted(points) == seconds),
        None,
    )
    if grid is None:
        pytest.fail(
            f"Demo {demo} is parsed on {sorted(points)}, which is neither of "
            f"the grids the recorded measurement holds ({sorted(grids)}). "
            "Either the measurement or the archive has moved; neither may be "
            "guessed at."
        )
    counts = {
        area: (obs.t, obs.total)
        for area, obs in aggregate_stage._area_orientation(ticks).items()
        if area is not None
    }
    return grid, counts


def _printed_points() -> list[float]:
    """The four points the report prints -- the calibration grid.

    Read from ``[aggregate].route_sample_seconds``, which the settings hold
    equal to ``[parse].snapshot_seconds`` minus ``[report]
    .skip_sample_seconds``, so it is not a second copy of the four.
    """
    return list(_real_settings().aggregate.route_sample_seconds)


@pytest.mark.archive
def test_the_ct_advance_finds_exactly_the_rows_it_was_calibrated_on() -> None:
    """The advance's hit table, row for row, on the shipped grid.

    The absence of this table is the reason Story 4.5's grid change could
    re-calibrate the rule without one test going red; since step 6 it is the
    fourteen-point table, and the calibration it grew from is pinned below.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    found = _rule_points(_reports(root), "ct_advance")
    assert found == ADVANCE_TABLE
    assert len(found) == ADVANCE_HITS
    assert len({(row[1], row[2]) for row in found}) == ADVANCE_ROUNDS


@pytest.mark.archive
def test_the_crunch_finds_exactly_the_rows_it_was_calibrated_on() -> None:
    """The crunch's hit table, row for row, on the shipped grid.

    Fourteen points since Story 4.5. The four-point calibration -- the five
    hits the rule found when its look-back was "the previous sample point" --
    is pinned in the test below it.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    found = _rule_points(_reports(root), "crunch")
    assert found == CRUNCH_TABLE
    assert len(found) == CRUNCH_HITS
    assert len({(row[1], row[2]) for row in found}) == CRUNCH_ROUNDS


@pytest.mark.archive
def test_the_look_back_is_calibrated_and_not_merely_valid() -> None:
    """The sweep says where 9.0 sits, and what the four-point grid cannot say.

    9.0 is the **largest** value that still reproduces the four rounds, and
    the next value the grid allows loses two of them. That is the whole of
    what this grid can decide: **its lower neighbours cost nothing**, because
    ``15 - x`` lands on the 6 s point and ``30 - x`` on the 15 s point for
    every ``x`` in ``(0, 9]``, so the sweep is flat below 9 by arithmetic and
    not by measurement. 9.0 is picked as the largest of those equals, so that
    the rule asks no narrower a question than the previous-point rule it
    replaces -- not because its lower neighbours were shown to be worse.

    **A denser grid does separate them**, which is the evidence that the flat
    stretch is this grid's blindness and not the threshold's: measured on
    :data:`DENSE_DEMO` at fourteen points, 4.5 s and 6.0 s each find one crunch
    round there and 9.0 s finds none -- and over the whole re-parsed archive,
    :data:`LOOKBACK_SWEEP_SHIPPED`.

    Read on the four-point subset since Story 4.5 re-parsed the archive
    (:func:`_printed_points`), where it reproduces exactly.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    settings = _real_settings()
    found = {}
    for lookback in LOOKBACK_SWEEP:
        limits = settings.thresholds.model_copy(
            update={"crunch_lookback_s": lookback}
        )
        rows = _rule_points(
            _reports(root, limits, seconds=_printed_points()), "crunch"
        )
        found[lookback] = len({(row[1], row[2]) for row in rows})
    assert found == LOOKBACK_SWEEP
    assert settings.thresholds.crunch_lookback_s == max(
        value
        for value, rounds in LOOKBACK_SWEEP.items()
        if rounds == CRUNCH_ROUNDS_AT_FOUR
    )


@pytest.mark.archive
def test_the_calibration_still_reproduces_on_the_printed_points() -> None:
    """The four-point subset of the dense archive gives the calibrated tables.

    Filtering a grid down is parsing on it (:func:`_on_grid`), so the
    calibration is still checked against the real archive after Story 4.5
    re-parsed it on fourteen points -- and it reproduces row for row, which is
    what "the dense grid is a superset" means in numbers.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    reports = _reports(root, seconds=_printed_points())
    advance = _rule_points(reports, "ct_advance")
    crunch = _rule_points(reports, "crunch")
    assert advance == ADVANCE_TABLE_AT_FOUR
    assert len(advance) == ADVANCE_HITS_AT_FOUR
    assert len({(row[1], row[2]) for row in advance}) == ADVANCE_ROUNDS_AT_FOUR
    assert crunch == CRUNCH_TABLE_AT_FOUR
    assert len(crunch) == CRUNCH_HITS_AT_FOUR
    assert len({(row[1], row[2]) for row in crunch}) == CRUNCH_ROUNDS_AT_FOUR


@pytest.mark.archive
def test_the_look_back_sweep_on_the_shipped_grid() -> None:
    """:data:`LOOKBACK_SWEEP_SHIPPED`, re-derived: 9.0 sits inside a flat
    stretch of five rounds on fourteen points, and its count is the shipped
    table's."""
    root = require_parsed(*CALIBRATION_DEMOS)
    settings = _real_settings()
    found = {}
    for lookback in LOOKBACK_SWEEP_SHIPPED:
        limits = settings.thresholds.model_copy(
            update={"crunch_lookback_s": lookback}
        )
        rows = _rule_points(_reports(root, limits), "crunch")
        found[lookback] = len({(row[1], row[2]) for row in rows})
    assert found == LOOKBACK_SWEEP_SHIPPED
    assert found[settings.thresholds.crunch_lookback_s] == CRUNCH_ROUNDS


@pytest.mark.archive
def test_the_sweeps_flat_stretch_is_the_grids_blindness_and_not_the_rules() -> None:
    """The look-back's lower neighbours are equal **on this grid only**.

    Without this the sentence above would be the round's own complaint: a
    claim about a denser grid, stated where a reader meets it and checked by
    nothing. Here it is run. :data:`DENSE_DEMO` is read on its own
    fourteen-point grid (``seconds=None`` leaves every table as parsed; the
    other demos' rows are filtered out by demo id, so the measurement stays
    this demo's), and there the values that the four-point sweep cannot tell
    apart give different answers.

    The direction is not the point and must not be read as one -- 4.5 s
    finding a round that 9.0 s does not is **not** evidence that 4.5 s is the
    better threshold. The point is only that the flat stretch below 9 is a
    property of a grid whose points are 9 and 15 seconds apart, so the
    four-point archive cannot calibrate within it.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    settings = _real_settings()
    found = {}
    for lookback in DENSE_DEMO_LOOKBACK_SWEEP:
        limits = settings.thresholds.model_copy(
            update={"crunch_lookback_s": lookback}
        )
        rows = _rule_points(_reports(root, limits, seconds=None), "crunch")
        dense = {(row[1], row[2]) for row in rows if row[1] == DENSE_DEMO}
        found[lookback] = len(dense)
    assert found == DENSE_DEMO_LOOKBACK_SWEEP
    assert len(set(found.values())) > 1


@pytest.mark.archive
def test_the_look_backs_answer_still_depends_on_the_grid() -> None:
    """The crunch's **question** is density-free; its **answer** is not.

    This is the residual :func:`~pappascout.domain.sampling._source_areas`
    names, run rather than asserted in prose. A duration is the same question
    on every grid, but the grid decides how nearly it can be honoured: the
    source is the latest point **at or before** ``t - lookback_s``, and at
    four points ``30 - 9 = 21`` falls back to the 15 s point while at fourteen
    it lands on 21 s itself.

    The same demo is read both ways -- as parsed (fourteen points) and
    filtered to the four-point grid -- and only the look-ups the two grids
    **share** are compared, since the dense grid has ten more points per round
    that the sparse one cannot answer at all. The split by sample point is
    part of the assertion and not decoration: it is what shows the difference
    is the arithmetic above and not noise. At 15 s the two grids agree
    exactly, because ``15 - 9 = 6`` is a point of both.
    """
    root = require_parsed(DENSE_DEMO)
    ticks = pl.read_parquet(root / "parsed" / DENSE_DEMO / "ticks.parquet")
    dense = _sources_by_round(ticks)
    four = _sources_by_round(
        ticks.filter(
            (pl.col("sample_kind") != "time")
            | pl.col("sample_t_s").is_in(ORIENTATION_BY_GRID["grids"]["four"])
        )
    )
    shared = set(dense) & set(four)
    differing = {key for key in shared if dense[key] != four[key]}
    by_point = {
        seconds: (
            len({key for key in differing if key[2] == seconds}),
            len({key for key in shared if key[2] == seconds}),
        )
        for seconds in sorted({key[2] for key in shared})
    }
    assert by_point == LOOKBACK_RESIDUAL
    assert (len(differing), len(shared)) == (
        sum(bad for bad, _ in LOOKBACK_RESIDUAL.values()),
        sum(total for _, total in LOOKBACK_RESIDUAL.values()),
    )


def test_the_grid_sentinel_cannot_be_a_value_a_caller_passes() -> None:
    """``_SENTINEL_GRID`` has to be distinguishable from every real argument.

    It was ``()`` until review round 1. CPython interns the empty tuple, so
    ``_reports(root, seconds=())`` -- "keep no time sample point at all" --
    was ``is``-identical to the default and silently read the settings file's
    four points instead. The bug was latent, since no caller passes ``()``
    today; the guard is here so that the sentinel cannot quietly become a
    value again.
    """
    empty: tuple[float, ...] = ()
    assert _SENTINEL_GRID is not empty
    assert _SENTINEL_GRID is not None
    assert not isinstance(_SENTINEL_GRID, Sequence)


def test_the_orientation_at_fourteen_points_is_measured_and_not_assumed() -> None:
    """The same demos read on two grids: what the per-point gate does not fix.

    The claim pinned here is **not** "the same areas are oriented on both
    grids" -- measured, that is false, and the two constants above say which
    areas differ and why. What is pinned is the size and the shape of the
    residual, so that a change which makes it worse goes red instead of
    passing as "roughly the same".

    **No archive mark and no archive.** The observations are in the repository
    (:data:`ORIENTATION_BY_GRID`), so this runs on every machine and on every
    run, including a fresh clone. The version of this test that read the
    fourteen-point side from the archive could only run while the archive was
    in an inconsistent state, and it went red the moment that state was
    repaired -- which is the right answer to a test resting on a fault.
    """
    lost: set[tuple[str, str]] = set()
    gained: set[tuple[str, str]] = set()
    for demo in ORIENTATION_BY_GRID["demos"]:
        at_four = _oriented_on(demo, "four")
        at_fourteen = _oriented_on(demo, "fourteen")
        lost |= {(demo, area) for area in at_four - at_fourteen}
        gained |= {(demo, area) for area in at_fourteen - at_four}
    assert sorted(lost) == sorted(ORIENTATION_LOST_AT_FOURTEEN)
    assert sorted(gained) == sorted(ORIENTATION_GAINED_AT_FOURTEEN)


def test_the_recorded_measurement_covers_the_calibration_demos() -> None:
    """The data file holds every calibration demo and both grids for each.

    Without this a demo could quietly drop out of the file and the residual
    would shrink to whatever was left -- the finding would weaken and nothing
    would say so.

    The grid is stated **once** and read everywhere else. Before the review
    its length lived in three places -- ``grids``, each demo's ``points``, and
    a literal in this module -- with nothing tying them together, so a file
    whose ``points`` said 14 over a ten-point grid would have passed while
    every quotient it produced was wrong.

    **Since Story 4.5 the shipped grid is the fourteen-point one**, and the
    four-point grid is what the report prints of it: the grid minus
    ``[report].skip_sample_seconds``. Both are read from the settings, so the
    file's two grids stay tied to what the tool parses and what it prints.
    """
    grids = ORIENTATION_BY_GRID["grids"]
    settings = _real_settings()
    shipped = list(settings.parse.snapshot_seconds)
    assert grids["fourteen"] == shipped
    hidden = set(settings.report.skip_sample_seconds)
    assert grids["four"] == [value for value in shipped if value not in hidden]
    assert set(grids["four"]) < set(grids["fourteen"])
    assert sorted(ORIENTATION_BY_GRID["demos"]) == sorted(CALIBRATION_DEMOS)
    for demo, body in ORIENTATION_BY_GRID["demos"].items():
        assert sorted(body["points"]) == sorted(grids), demo
        for name, seconds in grids.items():
            assert body["points"][name] == len(seconds), (demo, name)
        assert body["areas"], demo


def test_every_recorded_observation_is_load_bearing() -> None:
    """No entry of the data file may sit there unread.

    Measured in review round 1: **185 of 186** unchecked entries tolerated a
    change of one to ``total`` with the whole suite green, because only the
    entries near a threshold could move the derived verdict. An unread number
    in a repository is the same defect as a value copied into an assertion --
    it looks like evidence and nothing checks it.

    Two guards close it, and between them every entry is read twice:

    * the **per-demo sums**. They are recorded beside the per-area rows and
      must equal them, so changing any single observation by one reddens this
      test wherever that observation sits;
    * the **nesting** of the two grids. The four points are four of the
      fourteen, so every four-point row is also a fourteen-point row: an area
      seen at four must be seen at fourteen, neither count may shrink, and the
      T rows the ten extra points add cannot outnumber the rows they add.

    The sums themselves are the archive's own for the grid each demo is really
    parsed on (:func:`test_the_recorded_orientation_counts_are_the_archives_own`),
    so this is not two copies of one number agreeing with each other.
    """
    for demo, body in ORIENTATION_BY_GRID["demos"].items():
        for grid, (t_sum, total_sum) in body["totals"].items():
            counts = [
                value[grid] for value in body["areas"].values() if value[grid]
            ]
            assert sum(t for t, _ in counts) == t_sum, (demo, grid)
            assert sum(total for _, total in counts) == total_sum, (demo, grid)
        for area, value in body["areas"].items():
            where = (demo, area)
            # The domain type is the check for 0 <= t <= total and total > 0.
            # A pair of inequalities written here would be a second copy of a
            # rule AreaObservations already owns.
            observed = {
                grid: AreaObservations(t=counts[0], total=counts[1])
                for grid, counts in value.items()
                if counts
            }
            assert "fourteen" in observed, where
            if "four" not in observed:
                continue
            four, fourteen = observed["four"], observed["fourteen"]
            assert four.t <= fourteen.t, where
            assert four.total <= fourteen.total, where
            assert fourteen.t - four.t <= fourteen.total - four.total, where


@pytest.mark.archive
def test_the_recorded_orientation_counts_are_the_archives_own() -> None:
    """The measurement in the repository is the archive's, not a set of numbers.

    **Both columns, all eight demos** since Story 4.5 re-parsed the archive on
    fourteen points: each demo is checked against the grid it is really
    parsed on (fourteen), and then against the four-point column by filtering
    its table to the four -- the four-point subset the file's own four-point
    counts were derived from. A green run is therefore also the evidence that
    filtering a grid down to a subset is the same thing as parsing on it,
    which is what :func:`_on_grid` rests on.
    """
    root = require_parsed(*CALIBRATION_DEMOS)
    checked = 0
    for demo, body in ORIENTATION_BY_GRID["demos"].items():
        grid, counts = _archive_orientation(root, demo)
        for name in ORIENTATION_BY_GRID["grids"]:
            if name == grid:
                observed = counts
            else:
                ticks = pl.read_parquet(root / "parsed" / demo / "ticks.parquet")
                subset = ticks.filter(
                    (pl.col("sample_kind") != "time")
                    | pl.col("sample_t_s").is_in(ORIENTATION_BY_GRID["grids"][name])
                )
                observed = {
                    area: (obs.t, obs.total)
                    for area, obs in aggregate_stage._area_orientation(
                        subset
                    ).items()
                    if area is not None
                }
            recorded = {
                area: tuple(value[name])
                for area, value in body["areas"].items()
                if value[name] is not None
            }
            assert recorded == observed, (demo, name)
            checked += 1
    assert checked == 2 * len(ORIENTATION_BY_GRID["demos"])


def _records_from(root: Path) -> dict[tuple[str, str, str, str], dict]:
    """Every group's record in the archive, keyed as the table keys it."""
    found: dict[tuple[str, str, str, str], dict] = {}
    for team, report in zip(RECORDED_TEAMS, _reports(root, teams=RECORDED_TEAMS)):
        for map_report in report.maps:
            for side_report in map_report.sides:
                for entry in side_report.round_types:
                    key = (
                        team,
                        map_report.map_name,
                        side_report.side,
                        entry.round_type,
                    )
                    found[key] = {
                        "rounds": entry.sample.rounds,
                        "record": [
                            entry.record.wins,
                            entry.record.losses,
                            entry.record.unknown,
                        ],
                    }
    return found


def _recorded_groups() -> dict[tuple[str, str, str, str], dict]:
    """The same, read out of :data:`ROUND_RECORDS`."""
    return {
        (team, map_name, side, round_type): body
        for team, entry in ROUND_RECORDS["teams"].items()
        for map_name, sides in entry["maps"].items()
        for side, types in sides.items()
        for round_type, body in types.items()
    }


@pytest.mark.archive
def test_every_groups_record_is_the_one_the_archive_holds() -> None:
    """The whole record table, re-derived and compared whole.

    **This is the only test that can see a wrong record.** The model's
    cross-check compares totals, and both sides count one round per row, so a
    record that is the right length and the wrong content passes it --
    measured 2026-09-23: swapping the wins and the losses left ``-m archive``
    at 20 passed before this test existed, and so did counting the wrong rows
    at the right length. Both change a number in this table, so both go red
    here.

    Compared as **one equality over the whole table** rather than row by row,
    so a group that appears or disappears fails as loudly as a group whose
    numbers moved. A drifted table is re-measured, not loosened -- see
    :data:`ROUND_RECORDS`.
    """
    root = require_parsed(*RECORDED_DEMOS)
    recorded = _recorded_groups()
    assert len(recorded) == 78, len(recorded)
    assert _records_from(root) == recorded


@pytest.mark.archive
def test_the_record_table_names_the_demos_the_reports_are_built_from() -> None:
    """The table's demo lists are the reports' own, and its totals too.

    Without this the demo lists would be load-bearing only in the wrong
    direction: they feed :func:`require_parsed`, so a **wrong** id makes the
    archive tests skip, and a skip is green. This catches an id that is wrong
    but present.

    **The limit is worth stating**: an id that is wrong *and* absent from the
    archive still skips, and that is indistinguishable from running on a
    machine whose archive is partial -- which is the behaviour the skip gate
    is for. Nothing here can tell those two apart.
    """
    root = require_parsed(*RECORDED_DEMOS)
    for team, report in zip(RECORDED_TEAMS, _reports(root, teams=RECORDED_TEAMS)):
        entry = ROUND_RECORDS["teams"][team]
        demos = sorted(
            demo
            for map_report in report.maps
            for demo in map_report.map_demo_ids
        )
        assert demos == entry["demos"], team
        assert report.sample.rounds == entry["rounds"], team


def test_the_reading_guides_example_is_a_record_the_archive_holds() -> None:
    """``render.view._LEGEND_RECORD`` is a group in the table, not an invention.

    The reading guide tells the reader that ``voitettu 15-7`` is the shape of
    the line, and its docstring claims the pair is the archive's own Nuke CT
    default block. That claim now fails here if it stops being true, instead
    of sitting in ``src/`` with nothing watching it.

    **No archive needed**: both sides are in the repository. What ties the
    table to the archive is
    :func:`test_every_groups_record_is_the_one_the_archive_holds`, and the
    two together are what make the guide's example checkable.
    """
    from pappascout.render.view import _LEGEND_RECORD

    body = _recorded_groups()[LEGEND_RECORD_GROUP]
    assert body["record"] == [
        _LEGEND_RECORD.wins,
        _LEGEND_RECORD.losses,
        _LEGEND_RECORD.unknown,
    ]


@pytest.mark.archive
def test_every_record_in_the_real_archive_covers_its_own_sample() -> None:
    """The record and the sample agree on every group the archive produces.

    A property of the real data: the values are read from the archive on both
    sides of the comparison, so there is nothing here to go stale. What it
    catches is the fault the model's cross-check was written for -- a record
    counted from a different *number* of rounds than the observations beside
    it -- on the only rounds that are real.

    Weaker than the table above and kept for its failure message: the table
    pins the numbers, this says which invariant they broke. ``_aggregate``
    builds the model, so a disagreement would already have raised inside it;
    the assertion is here as well because the failure this test names is a
    **measurement** claim and not a constructor's.
    """
    root = require_parsed(*RECORDED_DEMOS)
    groups = [
        round_type
        for report in _reports(root, teams=RECORDED_TEAMS)
        for map_report in report.maps
        for side_report in map_report.sides
        for round_type in side_report.round_types
    ]
    assert len(groups) == 78, len(groups)
    for group in groups:
        assert group.record.rounds == group.sample.rounds, group.round_type


@pytest.mark.archive
def test_the_archive_has_no_round_with_an_unknown_outcome() -> None:
    """Measured 2026-09-23: 0 nulls in the archive's 511 classified rounds.

    **This test states a fact about today's archive, not a rule.** ``won`` is
    nullable and the unknown bucket is written for that reason; if this ever
    goes red, the answer is not to widen the model but to read the round it
    names -- an unread outcome has appeared in real data for the first time,
    and the report's unknown clause is about to be printed for the first time
    too.

    **It covers :data:`RECORDED_TEAMS` and not the whole archive.** The 511
    is the figure from reading every ``classified/`` table directly; these
    three teams are the part of it this suite can build a report from without
    writing to the archive, and the rounds outside them belong to lineups
    that are the same teams under another id.

    Kept apart from the test above so the two failures cannot be confused: one
    says the record disagrees with its sample, this one says the archive
    changed.
    """
    root = require_parsed(*RECORDED_DEMOS)
    groups = [
        (key, entry)
        for key, entry in _records_from(root).items()
    ]
    # A floor, because the assertion below is that a list is **empty**: an
    # archive that yielded no group at all would satisfy it while covering
    # nothing.
    assert len(groups) == 78, len(groups)
    unknown = [key for key, entry in groups if entry["record"][2]]
    assert unknown == [], unknown


# --- Matches, not only rounds (Story 4.9) ---------------------------------------


#: Every level's match count in the archive, measured 2026-09-24 by running
#: :func:`_reports` over it -- **the same code path the test that reads it
#: back uses**, so the file records what the tool produces and not a
#: hand-count of the parquet files. The precedent and the reasoning are
#: :data:`ROUND_RECORDS`'s, and the same three things need saying about this
#: table too.
#:
#: **Why a data file and not a table in a comment.** The match count is the
#: number the report was changed to state, and almost nothing else in the
#: suite can see it go wrong. The model's own checks are inclusions on the
#: pipeline's path (``positions_for`` counts a subset of the rounds
#: ``sample_for`` counted), so a count taken from the wrong place is
#: internally consistent; every group-level figure and the two area tables are
#: here so that a wrong one is a wrong number in a file.
#:
#: **What is checkable from this repository, and what is not.** Without the
#: archive: nothing is re-derived, so no number here is confirmed. Three of
#: the five tests that read this file are ``-m archive`` and skip on a machine
#: without one; the other two assert on the file's **own** contents and pass
#: anywhere, which is a different job and their docstrings say so. With the
#: archive: every number, because the three re-derive each table and compare
#: it whole.
#:
#: (``ROUND_RECORDS`` carries the sentence "the tests that read it are
#: ``-m archive``" and there it is true. It was carried across to this file
#: without re-checking and was wrong here -- the copy-without-re-measuring
#: this project keeps catching, found in the Story 4.9 verification review.)
#:
#: **What this table cannot catch, and it is the important half.** Measured
#: 2026-09-24 over this file: of its **91** rows carrying both a demo and a
#: match count, **90 have them equal** -- the one exception is the report root
#: (8 demos, 4 matches). So a match count that was silently the **demo** count
#: passes every row here except that one, and the only test that sees it is
#: :func:`test_the_archives_report_holds_fewer_matches_than_demos`, which
#: compares those two root totals.
#:
#: That is true of :attr:`~pappascout.domain.report.Sample.matches` and of
#: **nothing else**: ``Position.matches_m``, ``AreaDistribution.matches_m``,
#: ``PlayersCount.matches`` and :class:`~pappascout.domain.report
#: .FirstContactArea`'s two are all equal to a demo count on this archive, and
#: what guards them is the unit fixtures in ``tests/test_aggregate.py`` that
#: put **three demos over two matches** on purpose. Nor is it the model: its
#: between-level bounds accept the substitution exactly, measured, and
#: :func:`~pappascout.domain.report._check_matches_are_bounded` says why.
#:
#: **It is an observation and not a rule.** Adding a demo or re-classifying
#: changes these numbers legitimately; the answer is then to measure the table
#: again and say so in the commit, not to loosen the test.
MATCH_COUNTS = json.loads(
    (Path(__file__).parent / "data" / "match_counts.json").read_text(
        encoding="utf-8"
    )
)


#: The recorded teams' reports, built once for the three tests below.
#:
#: **A cache and not a fixture**, because the three tests read the *same*
#: reports on the *same* settings and the models are frozen -- there is
#: nothing for one test to leave behind for another. What it buys is
#: measured 2026-09-24 on the developer's machine: ``-m archive`` runs the 22
#: tests that came before this story in 61 s, the 25 with the cache in 67 s
#: and the 25 without it in 74 s. ``-m archive`` is the suite CLAUDE.md asks
#: to be run every time, because it is the only one that reads the real
#: ``settings.toml`` against the real archive, so what it costs is a decision
#: and not an accident.
_MATCH_REPORTS: dict[Path, dict[str, object]] = {}


def _recorded_reports(root: Path) -> dict[str, object]:
    """Team key -> its report, built once per archive root."""
    if root not in _MATCH_REPORTS:
        _MATCH_REPORTS[root] = dict(
            zip(RECORDED_TEAMS, _reports(root, teams=RECORDED_TEAMS), strict=True)
        )
    return _MATCH_REPORTS[root]


def _match_groups_from(root: Path) -> dict[tuple[str, str, str, str], dict]:
    """Every group's rounds, demos and matches, from the archive."""
    found: dict[tuple[str, str, str, str], dict] = {}
    for team, report in _recorded_reports(root).items():
        for map_report in report.maps:
            for side in map_report.sides:
                for entry in side.round_types:
                    key = (team, map_report.map_name, side.side, entry.round_type)
                    found[key] = {
                        "rounds": entry.sample.rounds,
                        "demos": entry.sample.demos,
                        "matches": entry.sample.matches,
                    }
    return found


def _match_groups_recorded() -> dict[tuple[str, str, str, str], dict]:
    """The same, read out of :data:`MATCH_COUNTS`."""
    return {
        (team, map_name, side, round_type): body
        for team, entry in MATCH_COUNTS["teams"].items()
        for map_name, map_body in entry["maps"].items()
        for side, side_body in map_body["sides"].items()
        for round_type, body in side_body["round_types"].items()
    }


def _area_rows_from(report, map_name: str, side: str, round_type: str) -> list[dict]:
    """One group's whole sample-point table, in the pinned shape."""
    entry = next(m for m in report.maps if m.map_name == map_name)
    side_report = next(s for s in entry.sides if s.side == side)
    group = next(
        rt for rt in side_report.round_types if rt.round_type == round_type
    )
    return [
        {
            "sample_kind": point.sample_kind,
            "seconds": point.seconds,
            "matches_m": point.matches_m,
            "area": spot.area,
            "players": bar.players,
            "n": bar.n,
            "matches": bar.matches,
            "newest": bar.newest,
        }
        for point in group.positions
        for spot in point.areas
        for bar in spot.players_dist
    ]


@pytest.mark.archive
def test_every_groups_match_count_is_the_one_the_archive_holds() -> None:
    """The whole table, re-derived and compared whole.

    One equality over every group rather than row by row, for
    :func:`test_every_groups_record_is_the_one_the_archive_holds`'s reason: a
    group that appears or disappears has to fail as loudly as a group whose
    numbers moved.
    """
    root = require_parsed(*RECORDED_DEMOS)
    recorded = _match_groups_recorded()
    assert len(recorded) == 78, len(recorded)
    assert _match_groups_from(root) == recorded


@pytest.mark.archive
def test_the_archives_report_holds_fewer_matches_than_demos() -> None:
    """The story's premise, on the real data: a demo is not a match.

    The scouted team's eight demos are four matches of two maps each, so the
    summary said ``8 demoa`` to a reader counting matches. **This is the one
    test in the suite that can tell a match count from a demo count**: at and
    below the map level the two are equal on this archive, so every group row
    in :data:`MATCH_COUNTS` would pass with either.

    The other two teams are hand-imported demos and are one match each; they
    are asserted as well, because "fewer than demos" must not become the rule
    the code follows.
    """
    root = require_parsed(*RECORDED_DEMOS)
    totals = {
        team: (report.sample.demos, report.sample.matches)
        for team, report in _recorded_reports(root).items()
    }
    recorded = {
        team: (entry["demos"], entry["matches"])
        for team, entry in MATCH_COUNTS["teams"].items()
    }
    assert totals == recorded
    assert any(demos > matches for demos, matches in totals.values())


@pytest.mark.archive
def test_the_pistol_rows_of_the_measurement_carry_their_two_numbers() -> None:
    """The Intent's own example, whole and from the archive -- in the **model**.

    It was called ``..._render_their_two_numbers`` and rendered nothing:
    ``test_calibration`` calls ``render()`` nowhere, so this compares model
    fields. The Intent's acceptance criterion is about the printed line, and
    :func:`test_the_archives_pistol_lines_read_as_the_intent_says` is what
    pins that; the two are separate because a model table and a rendered line
    fail for different reasons and should say which.

    ``de_nuke`` T pistol at 15 s is what the story was written from
    (``recency-measured-2026-09-23.md``): Outside in three matches and the
    newest not among them, Control in one and that one **is** the newest. The
    whole sample-point table is compared, so the seconds, the areas and the
    marks are all pinned -- and ``de_nuke`` T full is pinned beside it because
    it is the group where a bar's matches and its rounds differ (**29 rounds
    over 4 matches**), which the pistol group cannot show: there every match
    contributes exactly one round.

    (An earlier version of this line said 17. 17 is the ``n`` of one bar in
    that group -- ``Lobby 1`` at 15 s, 17 rounds over 4 matches -- read off a
    measurement printout and written down as the group's round count. The
    group's own figure is 29, which the spec's Code Map, the pinned file and
    :func:`test_the_pinned_full_table_holds_a_bar_whose_matches_are_not_its_rounds`
    all agree on.)
    """
    root = require_parsed(*RECORDED_DEMOS)
    reports = _recorded_reports(root)
    for key, rows in MATCH_COUNTS["areas"].items():
        team, map_name, side, round_type = key.split("/")
        got = _area_rows_from(reports[team], map_name, side, round_type)
        assert got == rows, key


def test_the_pinned_pistol_table_says_what_the_measurement_says() -> None:
    """The pinned rows are the ones the measurement document describes.

    The document (``recency-measured-2026-09-23.md``) is not in this
    repository, so what is checked here is the **shape of the claim** the
    story rests on: at 15 s the top Outside observation spans three matches
    and excludes the newest, and the Control observation is the newest match
    alone. If a future measurement changes that, this fails beside the table
    rather than leaving the story's premise unguarded.

    **No archive needed**: both sides are in the repository. What ties the
    table to the archive is the test above it.
    """
    key = "1e1965abbc06133b/de_nuke/T/pistol"
    rows = [
        row
        for row in MATCH_COUNTS["areas"][key]
        if row["seconds"] == 15.0 and row["players"] > 0
    ]
    outside = next(
        row for row in rows if row["area"] == "Outside" and row["players"] == 3
    )
    assert (outside["n"], outside["matches"], outside["newest"]) == (3, 3, False)
    control = next(
        row for row in rows if row["area"] == "Control" and row["players"] == 4
    )
    assert (control["n"], control["matches"], control["newest"]) == (1, 1, True)


def test_the_pinned_full_table_holds_a_bar_whose_matches_are_not_its_rounds() -> None:
    """The distinction the pistol group cannot show.

    In ``de_nuke`` T pistol every match contributes exactly one round, so
    ``matches == n`` on every bar and a mutation that returned the round count
    as the match count would pass. The full-buy group has 29 rounds over 4
    matches, so its bars separate the two.

    **No archive needed**, for the reason the test above it gives.
    """
    key = "1e1965abbc06133b/de_nuke/T/full"
    rows = MATCH_COUNTS["areas"][key]
    assert any(row["matches"] < row["n"] for row in rows)
    assert all(row["matches"] <= row["n"] for row in rows)
    assert all(row["matches"] <= row["matches_m"] for row in rows)


@pytest.mark.archive
def test_the_archives_pistol_lines_read_as_the_intent_says() -> None:
    """The Intent's acceptance criterion, **rendered**, from the real archive.

    *"Given ``de_nuke`` T pistol, when the report is rendered, then the
    ``Outside 3`` line states that it was seen in three matches and that the
    newest is not one of them."*

    **The only thing that rendered the archive before this was nothing**
    (Story 4.9 verification review): ``test_calibration`` called ``render()``
    zero times, so the criterion was pinned by a hand-built golden and by
    model fields, and no test put the archive's own numbers through the view.
    A view that stopped printing the mark, or printed the match fraction
    where it equals the round fraction, would have kept this file green.

    The match fraction is **absent by design** on this row and that is
    asserted too: a pistol round is one per map, so ``3/4 ottelussa`` would
    be ``3/4 kierroksesta`` again in another word, and the product owner had
    it dropped (2026-09-24). The mark is what is left, and it is the finding.
    """
    root = require_parsed(*RECORDED_DEMOS)
    settings = _real_settings()
    report = _recorded_reports(root)["1e1965abbc06133b"]
    text = render_report(
        report, settings=settings.report, round_list_paths=[]
    )
    line = next(
        row
        for row in text.splitlines()
        if row.startswith("- 15 s:") and "Control 4" in row
    )
    assert "Outside 3 (3/4 kierroksesta, ei uusimmassa)" in line
    assert "Control 4 (1/4 kierroksesta, uusin mukana)" in line
    assert "ottelussa" not in line


#: Every map's demo list from the archive, as Story 4.10 prints it: for each
#: row, whether the match is in the index, the day it was played and whether
#: an opponent could be named.
#:
#: **No team name and no demo id, and that is the shape and not a
#: convenience.** The repository is public and the denylist that guards it
#: lives outside the repository (AD-12), so a pinned table of real league
#: opponents would put the one thing the guard exists for into the one file
#: the guard cannot be asked about. What is pinned instead is everything
#: about the lookup **except** the names: the order, the dates, and whether
#: each row resolved at all. The names themselves are checked against the
#: archive's own index at run time
#: (:func:`test_the_named_opponent_is_never_the_scouted_team_itself`), where
#: they are read and not written.
#:
#: **What this table cannot catch.** Two opponents swapped between two rows
#: of the same date keep every value here. What sees that is
#: ``test_aggregate.test_a_maps_demos_come_out_newest_first``, the only place
#: where two **different** opponent names appear in one list, and at stage
#: level ``test_stage_aggregate
#: .test_the_written_report_carries_the_date_and_the_opponent``, whose two
#: matches were given two names for exactly this reason.
#:
#: Naming the tests that do the work rather than "a unit fixture somewhere"
#: is the point: the first version of this note pointed at a fixture that
#: distinguishes **rosters** and not names, which answers a different
#: question. The division of labour is Story 4.9's and holds for the same
#: reason -- the archive cannot hold this evidence without holding the names.
#:
#: **It is an observation and not a rule.** Importing a demo or running
#: ``discover`` again changes these rows legitimately; the answer is then to
#: measure the table again and say so in the commit.
PLAYED_MAPS = MATCH_COUNTS["played"]


def _played_rows_from(root: Path) -> dict[str, dict[str, list[dict]]]:
    """Every team's played-map rows, from the archive, in the pinned shape."""
    return {
        team: {
            entry.map_name: [
                {
                    "indexed": row.indexed,
                    "played_on": (
                        row.played_on.isoformat() if row.played_on else None
                    ),
                    "opponent_named": row.opponent is not None,
                }
                for row in entry.played_maps
            ]
            for entry in report.maps
        }
        for team, report in _recorded_reports(root).items()
    }


@pytest.mark.archive
def test_every_maps_demo_list_is_the_one_the_archive_holds() -> None:
    """The whole table, re-derived and compared whole.

    One equality over every team rather than row by row, for
    :func:`test_every_groups_match_count_is_the_one_the_archive_holds`'s
    reason: a map that appears or disappears has to fail as loudly as a row
    whose date moved.

    The count is asserted as well, because an empty table compares equal to an
    empty table: sixteen rows over three teams, which is the archive's eight
    map-demos for the scouted team and four each for the two hand-imported
    ones.
    """
    root = require_parsed(*RECORDED_DEMOS)
    found = _played_rows_from(root)
    assert sum(len(rows) for team in found.values() for rows in team.values()) == 16
    assert found == PLAYED_MAPS


@pytest.mark.archive
def test_the_scouted_teams_maps_are_listed_newest_first() -> None:
    """The story's acceptance criterion on the real data.

    *"Given the scouted team, when the report is rendered, then ``de_nuke``'s
    section lists four maps newest first with their dates."* The dates are
    read out of the **pinned** table and compared against the archive's own
    report, so the assertion cannot drift into re-deriving what it checks.

    The descending order is asserted separately from the dates themselves: a
    list with the right four dates in the wrong order passes an equality
    against a set, and the order is the half the reader acts on.
    """
    root = require_parsed(*RECORDED_DEMOS)
    report = _recorded_reports(root)["1e1965abbc06133b"]
    entry = next(m for m in report.maps if m.map_name == "de_nuke")
    dates = [row.played_on for row in entry.played_maps]

    assert len(dates) == 4
    assert all(day is not None for day in dates)
    assert dates == sorted(dates, reverse=True)
    assert [day.isoformat() for day in dates] == [
        row["played_on"] for row in PLAYED_MAPS["1e1965abbc06133b"]["de_nuke"]
    ]
    assert entry.every_demo_is_placed


@pytest.mark.archive
def test_the_named_opponent_is_never_the_scouted_team_itself() -> None:
    """The opponent is the **other** side, and the archive can see that.

    The one thing a name-free pinned table cannot check, checked against the
    index instead of against a written-down name: the rule picks the side of
    the match entry that shares no player with the subject's own roster, and
    the failure that rule can have is picking the subject. Measured
    2026-09-24 on this archive, the separation is not a near thing -- one side
    holds 7 of the subject's 7 observed players and the other 0 -- but the
    check costs nothing and the wrong answer would be a report that names the
    scouted team as its own opponent on every row.

    The team's **alternative** names are checked too, because
    ``display_name`` is the most often observed clan name and a team that
    appeared under two names would otherwise be caught only half the time.
    """
    root = require_parsed(*RECORDED_DEMOS)
    for team, report in _recorded_reports(root).items():
        own = {report.team.display_name, *report.team.display_name_alternatives}
        named = [
            row.opponent
            for entry in report.maps
            for row in entry.played_maps
            if row.opponent is not None
        ]
        assert own.isdisjoint(named), (team, sorted(own & set(named)))


@pytest.mark.archive
def test_one_match_gives_the_same_date_and_opponent_on_every_map() -> None:
    """A ``best_of`` match is one meeting, and its two maps say so.

    Measured 2026-09-24: **all four** of the scouted team's matches played
    two maps -- three of them ``de_nuke`` with ``de_dust2`` and the newest
    ``de_nuke`` with ``de_inferno``. So a lookup keyed on the demo instead of
    on the match would show the same evening as two different opponents or
    two different days. Nothing in the pinned table would move: both rows
    would still be indexed, dated and named.

    **This is the archive's own multi-map case and the only one it has.** The
    two hand-imported teams are one map per match, so the assertion is
    guarded against becoming vacuous by requiring that the four really are on
    two maps each.
    """
    root = require_parsed(*RECORDED_DEMOS)
    seen: dict[tuple[str, str], set[tuple]] = {}
    maps_of: dict[tuple[str, str], set[str]] = {}
    for team, report in _recorded_reports(root).items():
        for entry in report.maps:
            for row in entry.played_maps:
                key = (team, match_of(row.map_demo_id))
                seen.setdefault(key, set()).add((row.played_on, row.opponent))
                maps_of.setdefault(key, set()).add(entry.map_name)
    # Not vacuous: every one of the scouted team's four matches played two
    # maps, and no other team's did.
    assert sum(1 for names in maps_of.values() if len(names) > 1) == 4, maps_of
    assert sum(1 for key in seen if key[0] == "1e1965abbc06133b") == 4, seen
    for key, values in seen.items():
        assert len(values) == 1, (key, values)


# --- The pistol round's route (Story 4.11) --------------------------------------


#: The pistol blocks' route rows from the real archive, measured 2026-09-25
#: by running :func:`_route_blocks_from` over it -- the same code path the
#: test that reads it back uses, so the file records what the tool produces
#: and not a hand-written expectation. The precedent is
#: :data:`MATCH_COUNTS`'s, and the same three things need saying here.
#:
#: **Why a data file and not a table in a comment.** The route is what this
#: story added, and almost nothing else in the suite can see it go wrong on
#: real data: the unit tests build their own sample points, and the model's
#: own guards are internal consistency. The rows are here so that a route
#: that changes shape is a changed file and not a changed opinion. It is also
#: CLAUDE.md's rule about citing ``_bmad-output/`` -- the machine-readable
#: half belongs in the repository, and the document explains it.
#:
#: **All twenty of the archive's pistol blocks**, and that is a correction
#: rather than a choice: it held three, and :func:`_route_blocks_from`
#: skipped every key it did not hold, so seventeen blocks were outside every
#: guard and a block that appeared could not fail the comparison at all. The
#: helper now collects them all.
#:
#: **What is checkable from this repository, and what is not.** Without the
#: archive: nothing is re-derived, so no row here is confirmed -- the tests
#: that read it without one assert on its **own** contents, which is a
#: different job and their docstrings say so. With the archive:
#: :func:`test_the_archives_pistol_routes_are_the_ones_recorded` re-derives
#: every block and compares it whole.
#:
#: **On the grid ``settings.toml`` declares**, because
#: :func:`_route_blocks_from` builds from :func:`_recorded_reports`, which
#: goes through :func:`_on_grid` -- so this table is the report **on that
#: grid**, not on whatever grid a demo happens to carry. Since Story 4.5 the
#: grid is fourteen points and the route reads only the points the report
#: prints (``[aggregate].route_sample_seconds``), and the table did not move
#: when the archive was re-parsed on it (2026-09-25): the route is the one
#: the four printed points give.
#:
#: **Why no opponent and no date.** The block's heading states both, and both
#: are deliberately absent here: the repository is public and the denylist
#: that guards it lives outside the repository (AD-12), so a pinned table of
#: real league opponents would put the one thing the guard exists for into
#: the one file the guard cannot be asked about. The heading's own values are
#: covered by :data:`PLAYED_MAPS`, which pins the dates and whether each row
#: resolved without naming anybody.
#:
#: **It is an observation and not a rule.** Importing a demo or re-parsing
#: one changes these rows legitimately; the answer is then to measure the
#: table again and say so in the commit, not to loosen the test.
PISTOL_ROUTES = json.loads(
    (Path(__file__).parent / "data" / "pistol_routes.json").read_text(
        encoding="utf-8"
    )
)["blocks"]


def _route_blocks_from(root: Path) -> dict[str, list[dict]]:
    """Every recorded pistol block's route rows, as the view renders them.

    Through :func:`~pappascout.render.view.build_view` and not through the
    model alone, because the rows **are** the product: the shared stretch
    written once, the indentation under the point a group divided at and the
    inline terminal division are all decisions this layer makes, and a model
    comparison would leave every one of them unpinned.

    **Every pistol block, and no lookup against the recorded table.** The
    first version skipped any key the table did not hold, which made the
    comparison one-sided: a block that **appeared** could never fail it,
    while the test above claimed that a block appearing or disappearing
    fails as loudly as a changed row. Three of the archive's twenty blocks
    were pinned and the other seventeen sat outside every guard.
    :func:`_match_groups_from`, the precedent this follows, has no such
    filter, and this has none now either.
    """
    settings = _real_settings()
    found: dict[str, list[dict]] = {}
    for team, report in _recorded_reports(root).items():
        view = build_view(report, settings=settings.report)
        by_map = {entry.map_name: entry for entry in report.maps}
        for map_view in view.maps:
            model_map = by_map[map_view.map_name]
            for side_view in map_view.sides:
                model_side = next(
                    s for s in model_map.sides if s.side == side_view.side
                )
                type_view = next(
                    (
                        v
                        for v in side_view.round_types
                        if v.round_type == ROUTE_ROUND_TYPE
                    ),
                    None,
                )
                if type_view is None:
                    continue
                key = (
                    f"{team}/{map_view.map_name}/{side_view.side}/"
                    f"{ROUTE_ROUND_TYPE}"
                )
                model = next(
                    rt
                    for rt in model_side.round_types
                    if rt.round_type == ROUTE_ROUND_TYPE
                )
                found[key] = [
                    {
                        "round_no": route.round_no,
                        "won": route.won,
                        "rows": list(row_view.rows),
                        "note": row_view.note,
                    }
                    for route, row_view in zip(
                        model.routes, type_view.routes, strict=True
                    )
                ]
    return found


@pytest.mark.archive
def test_the_archives_pistol_routes_are_the_ones_recorded() -> None:
    """The whole table, re-derived and compared whole.

    One equality over every block rather than row by row, for
    :func:`test_every_groups_match_count_is_the_one_the_archive_holds`'s
    reason: a block that appears or disappears has to fail as loudly as a
    row whose areas moved. :func:`_route_blocks_from` collects **all** of
    them for exactly that reason -- it filtered against this table once, and
    the claim in this sentence was false for half of its directions while it
    did.

    The length assertion is a **floor against a vacuous comparison**, the
    same one :func:`test_every_groups_match_count_is_the_one_the_archive_holds`
    makes with 78: two empty dictionaries are equal, and an archive that
    yielded no block at all would satisfy the line below while covering
    nothing.
    """
    root = require_parsed(*RECORDED_DEMOS)
    assert len(PISTOL_ROUTES) == 20, sorted(PISTOL_ROUTES)
    assert _route_blocks_from(root) == PISTOL_ROUTES


def test_the_recorded_routes_show_the_newest_match_taking_a_new_way() -> None:
    """The story's acceptance criterion, on the pinned table.

    *"Given ``de_nuke`` T pistol, when the report is rendered, then the
    2026-09-20 line states four players through ``Control`` and the three
    older lines do not mention ``Control`` at all."*

    The blocks are recorded **newest match first**, so the criterion is read
    off the order: the first block, and only the first, goes through
    ``Control``. That is the finding the whole story exists for -- Story
    4.9's report could say ``Control 4 (1/4 kierroksesta, uusin mukana)``,
    and as a route it says which way they came and that the way is new.

    **No archive needed**: the table is in the repository. What ties it to
    the archive is the test above it.
    """
    blocks = PISTOL_ROUTES["1e1965abbc06133b/de_nuke/T/pistol"]
    assert len(blocks) == 4, blocks
    newest, *older = blocks
    assert any("4 Control" in row for row in newest["rows"]), newest
    for block_entry in older:
        assert not any("Control" in row for row in block_entry["rows"]), (
            block_entry
        )


def test_the_recorded_routes_name_nobody_and_date_nothing() -> None:
    """The table carries areas, counts and seconds -- never a name.

    The repository is public and the denylist is outside it (AD-12), so this
    file cannot be checked against the forbidden names the way the tree is.
    What it can be held to is a **shape**: every row is an indented Markdown
    list item that holds no date, and the only fields beside the rows are a
    round number, an outcome and the empty-round note.

    A date is the cheap half and it is checked because it is the half that
    would arrive by accident -- somebody pinning the rendered block whole
    rather than its rows. The **heading** is the part that names an
    opponent, and a heading is a dated string, so the date check is what
    would catch that paste.

    The marker half is the form pass's own property, measured on the
    archive's own rows: a route row is a real list item (``- `` after an
    even indent) and never opens with the arrow. Together with
    ``test_every_route_row_is_a_markdown_list_item`` the claim is pinned on
    hand-built rows and on measured ones.

    **No archive needed**, for the reason the test above it gives.
    """
    allowed = {"round_no", "won", "rows", "note"}
    dated = re.compile(r"\d{4}-\d{2}-\d{2}")
    for key, blocks in PISTOL_ROUTES.items():
        for entry in blocks:
            assert set(entry) == allowed, (key, sorted(entry))
            for row in entry["rows"]:
                indent = len(row) - len(row.lstrip(" "))
                assert indent >= 2 and indent % 2 == 0, (key, row)
                assert row[indent:].startswith("- "), (key, row)
                assert not row.lstrip().startswith("->"), (key, row)
                assert not dated.search(row), (key, row)


def test_every_recorded_route_row_states_a_group_of_players() -> None:
    """A row's every step names a count, and the counts are the round's.

    The pinned rows are strings, so nothing in the file itself enforces the
    model's arithmetic. What can be read off them is the one property a
    reader relies on: **every step on every row begins with a number**, so
    no row silently drops the count and reads as a bare list of areas --
    which is what the report said before this story and what the route was
    built not to say.

    **No archive needed**, for the reason the tests above give.
    """
    step = re.compile(r"^\d+ \S")
    for key, blocks in PISTOL_ROUTES.items():
        for entry in blocks:
            for row in entry["rows"]:
                body = row.lstrip().removeprefix("- ").removeprefix("-> ")
                for part in body.split(" -> "):
                    for piece in part.split(", "):
                        assert step.match(piece), (key, row, piece)


def test_the_recorded_ct_blocks_are_several_short_rows_and_that_is_the_finding(
) -> None:
    """The measurement behind the old "T side only" restriction, kept.

    *"A defence does not travel as a body"* -- the spec's reading note since
    2026-09-25, when both sides were asked for. ``de_nuke`` CT pistol starts
    from more places than ``de_nuke`` T pistol does, and the block is
    therefore several short rows. That is the observation and not a
    rendering fault, so it is pinned rather than left as a sentence.

    **No archive needed**, for the reason the tests above give.
    """
    def starts(key: str) -> list[int]:
        return [
            sum(1 for row in entry["rows"] if row.startswith("  - "))
            for entry in PISTOL_ROUTES[key]
        ]

    attack = starts("1e1965abbc06133b/de_nuke/T/pistol")
    defence = starts("1e1965abbc06133b/de_nuke/CT/pistol")
    assert max(attack) == 2, attack
    assert max(defence) >= 3, defence
    assert sum(defence) > sum(attack), (defence, attack)
