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

**In both, the document is the truth.** If some row of the table does not
pass, the code is wrong -- the table is not adjusted to match the code, and
no threshold is nudged so that a single row passes. The thresholds are read
from the real ``settings.toml``: a test that invented limits of its own would
prove nothing about the settings file the tool is run with.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import polars as pl
import pytest
from pydantic import ValidationError

from conftest import REAL_SETTINGS, require_parsed
from pappascout.adapters.demo_parser import _armed_count
from pappascout.archive.paths import ArchivePaths
from pappascout.constants import KNOWN_INVENTORY_ITEMS, SITE_AREAS
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
from pappascout.domain.sampling import CloudCell, site_groups
from pappascout.domain.schemas import ARMED_COLUMN, MONEY_DISTRIBUTION_COLUMN
from pappascout.errors import SchemaError
from pappascout.stages import aggregate as aggregate_stage
from pappascout.stages.aggregate import collect_team

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


def _stack_reports(root: Path, limits: ThresholdSettings | None = None):
    """Both teams' reports **in memory**, without changing the archive.

    The stage's own ``run`` would write ``report.json`` into the developer's
    archive; a test must not change the data it measures against.
    ``_aggregate`` is a function of the same module and does exactly what
    ``run`` does before the write -- the underscore is a sign that it should
    not be called from production code, not that it must not be read.
    """
    settings = _real_settings()
    thresholds = limits or settings.thresholds
    archive = ArchivePaths(root=root)
    return [
        aggregate_stage._aggregate(
            archive,
            collect_team(archive, team, thresholds),
            thresholds,
            settings.league,
            settings.aggregate,
        )
        for team in CALIBRATION_TEAMS
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
    found = _stack_points(_stack_reports(root))
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
    reports = _stack_reports(root)
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
    reports = _stack_reports(root, without)
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
    rounds = {(row[1], row[2]) for row in _stack_points(_stack_reports(root, limits))}
    assert sorted(rounds) == [
        ("1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1", 14),
        ("inferno_vs_ryhmarama", 2),
    ]
    at_four = {(row[1], row[2]) for row in _stack_points(_stack_reports(root))}
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
    found = {(row[1], row[2]) for row in _stack_points(_stack_reports(root))}
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
        return {(row[1], row[2]) for row in _stack_points(_stack_reports(root, limits))}

    at_two = {(row[1], row[2]) for row in _stack_points(_stack_reports(root))}
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
    rounds = {(row[1], row[2]) for row in _stack_points(_stack_reports(root, limits))}
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
    reports = _stack_reports(root)
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
