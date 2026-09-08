"""``domain.economy`` -- the loss count and round type classification, no demos.

These tests are the rows of the I/O matrix one at a time with hand-built
tables. Not one of them needs a demo file, so ``pytest -m "not demo"`` covers
the whole classification logic.

The thresholds are read from the **real** ``settings.toml``: a test that
invented its own bounds would prove nothing about the settings file the tool
is run with.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from conftest import even_split
from pappascout.domain.economy import (
    INPUT_FIELDS,
    Decision,
    available_money,
    classify_round,
    loss_counts,
    per_player,
)
from pappascout.domain.models import (
    EconomySettings,
    ThresholdSettings,
    load_settings,
)
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    CLASSIFIED_INPUTS,
    MONEY_DISTRIBUTION_COLUMN,
)
from pappascout.errors import SchemaError


@pytest.fixture
def thresholds(settings_file: Path) -> ThresholdSettings:
    return load_settings(settings_file, env_files=()).thresholds


@pytest.fixture
def economy(settings_file: Path) -> EconomySettings:
    """``[economy]``: the half-buy's condition B reads the bonus steps here."""
    return load_settings(settings_file, env_files=()).economy


def row(**overrides) -> dict:
    """A round row with default values; a test changes only what it examines.

    The default is a full buy with five players: $25,000 / 5 = $5,000 per
    player.

    The money distribution and the armed counter are derived from the other
    values unless the test gives them itself: the distribution is
    ``money_buy_end`` split evenly and the counter is ``players_buy_end``.
    Without the derivation every test that changes ``money_buy_end`` would
    leave the row internally contradictory -- the distribution would claim a
    different total than the column, and condition B would measure the wrong
    money.
    """
    defaults = {
        "round_no": 5,
        "side": "T",
        "won": True,
        "status": "ok",
        "money_buy_end": 5000,
        "money_spent": 20000,
        "equip_buy_end": 25000,
        "equip_round_start": 5000,
        "players_buy_end": 5,
        "survivors_equip_prev": 0,
        "survivors": 0,
    }
    defaults.update(overrides)

    players = defaults["players_buy_end"]
    money = defaults["money_buy_end"]
    if MONEY_DISTRIBUTION_COLUMN not in overrides:
        defaults[MONEY_DISTRIBUTION_COLUMN] = (
            None if money is None or not players else even_split(money, players)
        )
    if ARMED_COLUMN not in overrides:
        defaults[ARMED_COLUMN] = players
    return defaults


def previous(won: bool | None = False, *, round_no: int = 4, **overrides) -> dict:
    """The previous round: by default one number smaller, the same side."""
    defaults = row(round_no=round_no, won=won, survivors=0)
    defaults.update(overrides)
    return defaults


def team_frame(rounds: list[tuple[int, str, bool | None]]) -> pl.DataFrame:
    """One team's rows out of ``(round_no, side, won)`` triples."""
    return pl.DataFrame(
        [{"round_no": no, "side": side, "won": won} for no, side, won in rounds],
        schema={"round_no": pl.Int32, "side": pl.Utf8, "won": pl.Boolean},
    )


# --- Loss count ----------------------------------------------------------------


def test_half_starts_at_one_and_climbs_with_losses(thresholds) -> None:
    df = team_frame([(1, "T", False), (2, "T", False), (3, "T", False)])
    assert loss_counts(df, thresholds) == [1, 2, 3]


def test_a_win_steps_the_counter_down_by_one(thresholds) -> None:
    """A win steps the counter down by one; it does not reset it."""
    df = team_frame([(1, "T", False), (2, "T", False), (3, "T", True), (4, "T", True)])
    assert loss_counts(df, thresholds) == [1, 2, 3, 2]


def test_counter_is_clamped_to_the_configured_range(thresholds) -> None:
    losses = [(no, "T", False) for no in range(1, 9)]
    result = loss_counts(team_frame(losses), thresholds)
    assert max(result) == thresholds.loss_count_max
    wins = [(1, "T", False)] + [(no, "T", True) for no in range(2, 8)]
    assert min(loss_counts(team_frame(wins), thresholds)) == thresholds.loss_count_min


def test_new_half_is_detected_from_the_side_swap_not_the_round_number(
    thresholds,
) -> None:
    """I/O matrix: the side changes 12 -> 13, so the loss count returns to one."""
    rounds = [(no, "T", False) for no in range(1, 13)]
    rounds += [(no, "CT", False) for no in range(13, 16)]
    result = loss_counts(team_frame(rounds), thresholds)
    assert result[11] == thresholds.loss_count_max  # round 12, up to the cap
    assert result[12] == thresholds.loss_count_half_start  # round 13
    assert result[13] == 2


def test_overtime_side_swap_also_starts_a_new_half(thresholds) -> None:
    rounds = [(24, "CT", False), (25, "CT", False), (26, "T", False)]
    assert loss_counts(team_frame(rounds), thresholds) == [1, 2, 1]


def test_an_unresolved_round_does_not_move_the_counter(thresholds) -> None:
    """A guess would move every round that follows."""
    df = team_frame([(1, "T", False), (2, "T", None), (3, "T", False)])
    assert loss_counts(df, thresholds) == [1, 2, 2]


def test_empty_frame_gives_no_counters(thresholds) -> None:
    assert loss_counts(team_frame([]), thresholds) == []


def test_missing_column_is_a_schema_error(thresholds) -> None:
    df = team_frame([(1, "T", False)]).drop("won")
    with pytest.raises(SchemaError, match="won"):
        loss_counts(df, thresholds)


def test_unordered_rounds_are_refused(thresholds) -> None:
    df = team_frame([(2, "T", False), (1, "T", False)])
    with pytest.raises(SchemaError, match="ascending round order"):
        loss_counts(df, thresholds)


def test_duplicate_round_is_refused(thresholds) -> None:
    """Two rows for the same round would mean both teams."""
    df = team_frame([(1, "T", False), (1, "CT", True)])
    with pytest.raises(SchemaError, match="ascending round order"):
        loss_counts(df, thresholds)


def test_unnumbered_round_is_refused(thresholds) -> None:
    df = team_frame([(1, "T", False)]).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no")
    )
    with pytest.raises(SchemaError, match="round_no"):
        loss_counts(df, thresholds)


def test_empty_side_is_refused_instead_of_silently_resetting(thresholds) -> None:
    """An empty side would look like a side change and reset the counter."""
    df = team_frame([(1, "T", False), (2, None, False), (3, "T", False)])
    with pytest.raises(SchemaError, match="side"):
        loss_counts(df, thresholds)


# --- Round type: the round number's rules ---------------------------------------


@pytest.mark.parametrize("round_no", [1, 13])
def test_pistol_round_is_decided_by_the_round_number(
    thresholds,
    economy,
    round_no,
) -> None:
    """Pistol is settled by the number before money is looked at at all."""
    decision = classify_round(
        row(round_no=round_no, equip_buy_end=25000),
        previous(won=True, round_no=round_no - 1),
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.round_type == "pistol"
    assert "pistol round" in decision.reason


@pytest.mark.parametrize("round_no", [25, 26, 27, 28])
def test_overtime_round_gets_no_economy_reasoning(
    thresholds,
    economy,
    round_no,
) -> None:
    decision = classify_round(
        row(round_no=round_no, equip_buy_end=1000, money_buy_end=60000),
        previous(won=False, round_no=round_no - 1),
        thresholds,
        economy=economy,
        loss_count=4,
    )
    assert decision.round_type == "ot"
    assert "is overtime" in decision.reason


def test_regulation_round_is_never_overtime(thresholds, economy) -> None:
    decision = classify_round(
        row(round_no=24),
        previous(True, round_no=23),
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.round_type != "ot"


# --- Round type: the economy ----------------------------------------------------


def test_full_buy_is_decided_from_the_equipment_value(thresholds, economy) -> None:
    decision = classify_round(
        row(equip_buy_end=5 * thresholds.full_equip_min),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "full"


def test_full_buy_wins_over_the_after_loss_rules(thresholds, economy) -> None:
    """A full buy is settled by the equipment value, even after a loss."""
    decision = classify_round(
        row(equip_buy_end=25000, money_buy_end=100),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=4,
    )
    assert decision.round_type == "full"


def test_eco_after_a_loss_when_the_team_did_not_buy(thresholds, economy) -> None:
    """A verified case: saving after a pistol loss, the money stays in the till."""
    decision = classify_round(
        row(
            round_no=2,
            equip_buy_end=2000,
            equip_round_start=1000,
            money_buy_end=10100,
            money_spent=600,
        ),
        previous(won=False, round_no=1),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "eco"
    # I/O matrix: the reason states the money and the loss count.
    assert "loss count 2" in decision.reason
    assert "$/player" in decision.reason


def test_force_after_a_loss_when_the_team_bought_itself_empty(
    thresholds,
    economy,
) -> None:
    """I/O matrix: bought until empty -- $2,380 per player, $30 balance left."""
    decision = classify_round(
        row(
            round_no=23,
            equip_buy_end=12900,
            equip_round_start=1000,
            money_buy_end=150,
            money_spent=11900,
        ),
        previous(won=False, round_no=22),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "force"
    assert "bought until empty" in decision.reason
    # The reason names both counters, including the one that did not settle
    # the matter: otherwise the reader cannot see which condition rejected the
    # round.
    assert str(thresholds.force_buy_min) in decision.reason
    assert "5/5 armed" in decision.reason
    assert "0/5 can buy on the next round" in decision.reason


def test_half_after_a_loss_when_the_team_left_money_in_the_pocket(
    thresholds,
    economy,
) -> None:
    """I/O matrix: bought and left room -- the same purchase, another spread.

    Condition B separates a force from a half-buy. This is exactly the
    previous test's pair: the purchase, the armament and the loss count are
    the same, and **even the team's total balance is the same**. Only its
    distribution differs -- and that is precisely the whole reason for the
    rule. An average could not tell these two rows apart in any way.
    """
    shared = dict(
        round_no=23,
        equip_buy_end=12900,
        equip_round_start=1000,
        money_spent=11900,
        money_buy_end=5000,
    )
    # Loss count 2 -> the loss bonus is step 2 = $2,400, so a normal buy
    # ($4,000) needs $1,600 of one's own money.
    bought_empty = classify_round(
        # One rich, four empty: only he can buy. 1/5 < 3.
        row(money_players_buy_end=[5000, 0, 0, 0, 0], **shared),
        previous(won=False, round_no=22),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    left_room = classify_round(
        # The same $5,000 split differently: three with 1,600 (+2,400 =
        # 4,000, exactly at the bound) and two with 100. 3/5 >= 3.
        row(money_players_buy_end=[1600, 1600, 1600, 100, 100], **shared),
        previous(won=False, round_no=22),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert bought_empty.round_type == "force"
    assert left_room.round_type == "half"
    assert "left room" in left_room.reason
    assert "3/5 can buy on the next round" in left_room.reason
    # The same team total on both -- an average would not tell them apart.
    assert (
        bought_empty.inputs["money_buy_end"] == left_room.inputs["money_buy_end"]
    )


def test_a_purchase_with_too_few_armed_players_is_an_eco(thresholds, economy) -> None:
    """I/O matrix: the team bought, but only two were armed -> eco.

    Condition A separates the half-buy from an **eco**: with fewer than
    ``armed_players_min`` armed the round is not really played, and then it is
    an eco even if money moved past the buy limit. Ancient's round 21 T (two
    armed) is the observed case of this.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * 3000,  # there is money, condition B would hold
            players_armed_buy_end=2,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "eco"
    assert "2/5 armed" in decision.reason
    assert str(thresholds.armed_players_min) in decision.reason


def test_a_missing_money_distribution_is_not_classified(thresholds, economy) -> None:
    """I/O matrix: the distribution is missing -> not classified, reason given.

    Condition B cannot be deduced from the team total, and the class is not
    guessed. The same balance left over can mean 0/5 or 5/5 able to buy.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * 1500,
            money_players_buy_end=None,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "the per-player money distribution" in decision.reason
    assert decision.inputs["players_can_buy"] is None


def test_a_missing_armed_count_is_not_classified(thresholds, economy) -> None:
    """The same for the other condition: no counter -> no guess either way.

    ``players_armed_buy_end`` is ``null`` if no player's armour or inventory
    could be read. Zero would be an observation, ``null`` is not.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * 1500,
            players_armed_buy_end=None,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "the armed counter" in decision.reason


def test_a_hole_inside_the_distribution_empties_it(thresholds, economy) -> None:
    """One empty element is enough: a null read as zero would claim poverty.

    A read error would then look like a force -- and that kind of silent
    misreading is exactly where the whole story started.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5000,
            money_players_buy_end=[2000, 2000, 1000, None, 0],
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "one player's balance" in decision.reason


def test_a_short_handed_team_counts_from_the_same_set(thresholds, economy) -> None:
    """I/O matrix: four players readable -> both counters out of four.

    The divisor is the same set as in the totals (``players_buy_end``), so the
    reason's counters are "x/4" and not "x/5". A counter divided by five would
    claim that the fifth player cannot buy -- when the truth is that he could
    not be read.
    """
    decision = classify_round(
        row(
            equip_buy_end=4 * 2000,
            equip_round_start=4 * 300,
            money_buy_end=4 * 2000,
            players_buy_end=4,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "half"
    assert "4/4 armed" in decision.reason
    assert "4/4 can buy on the next round" in decision.reason


def test_the_loss_bonus_comes_from_the_configured_steps(thresholds, economy) -> None:
    """I/O matrix: loss count at the maximum -> the bonus is read by the step.

    The bonus is not hard-coded: the steps are ``[economy].loss_bonus_steps``,
    and the index is the loss count as it is. At the cap (``loss_count = 4``)
    the step is the last one.
    """
    top = thresholds.loss_count_max
    bonus = economy.loss_bonus_steps[top]
    # Everyone has exactly enough for the bonus to reach the bound.
    own = thresholds.normal_buy_money_min - bonus
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * own,
            money_players_buy_end=[own] * 5,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=top,
    )
    assert decision.round_type == "half"
    assert decision.inputs["loss_bonus_if_lost"] == bonus
    assert f"loss bonus {bonus} $" in decision.reason


def test_the_bonus_is_the_step_the_counter_already_points_at(
    thresholds, economy
) -> None:
    """The bonus is ``steps[loss_count]`` -- not ``steps[loss_count + 1]``.

    The loss count describes the state **on going into the round**, and that
    is exactly the step that is paid if the round is lost. ``settings.toml``
    says the same directly: the start of a half (counter 1) gives $1,900 for
    losing the pistol round, that is, the value of step 1. An index one too
    large would give every player $500 too much buying power.
    """
    for loss_count in range(thresholds.loss_count_min, thresholds.loss_count_max + 1):
        decision = classify_round(
            row(),
            previous(won=False),
            thresholds,
            economy=economy,
            loss_count=loss_count,
        )
        assert decision.inputs["loss_bonus_if_lost"] == (
            economy.loss_bonus_steps[loss_count]
        ), loss_count


def test_the_last_round_of_a_half_cannot_be_a_half_buy(thresholds, economy) -> None:
    """When the money does not carry, B is not computed and the result is force.

    After the last round of a half the balance is reset for the pistol round,
    so the money left in the pocket evaporates. It has therefore not been left
    *to leave room*, and the round cannot be a half-buy in the sense of rule
    S2.

    The same row in the middle of a half is a half-buy -- only the round
    number differs. That is this test's whole claim: the rule does not lean on
    the money but on whether the money has any use.
    """
    shared = dict(
        equip_buy_end=5 * 2000,
        equip_round_start=5 * 300,
        money_buy_end=5 * 3000,
    )

    def verdict(round_no: int) -> str | None:
        return classify_round(
            row(round_no=round_no, **shared),
            previous(won=False, round_no=round_no - 1),
            thresholds,
            economy=economy,
            loss_count=2,
        ).round_type

    # Round 12 -> 13 is a pistol round: the money is reset.
    last_of_half = thresholds.pistol_rounds[1] - 1
    assert verdict(last_of_half) == "force"
    # The same situation in the middle of a half.
    assert verdict(last_of_half - 1) == "half"


def test_the_last_regulation_round_cannot_be_a_half_buy(thresholds, economy) -> None:
    """Overtime has its own starting money, so it inherits no balance either."""
    decision = classify_round(
        row(
            round_no=thresholds.regulation_rounds,
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * 3000,
        ),
        previous(won=False, round_no=thresholds.regulation_rounds - 1),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "force"
    assert "does not carry to round" in decision.reason
    # Condition B is not computed, so its numbers are not claimed either.
    assert decision.inputs["players_can_buy"] is None
    assert decision.inputs["loss_bonus_if_lost"] is None


def test_overtime_rounds_carry_no_loss_bonus(thresholds, economy) -> None:
    """Overtime's economy model is different, so the bonus is left empty.

    The module's own limitation says that this model does not hold in
    overtime. A number borrowed from there would read on the round list like
    an observation.
    """
    decision = classify_round(
        row(round_no=thresholds.regulation_rounds + 1),
        previous(won=False, round_no=thresholds.regulation_rounds),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "ot"
    assert decision.inputs["loss_bonus_if_lost"] is None
    assert decision.inputs["players_can_buy"] is None


def test_a_two_player_team_can_still_reach_a_half_buy(thresholds, economy) -> None:
    """A short-handed team: the thresholds are scaled to what can be read.

    Three armed players cannot be observed from two players. Without the
    scaling a half-buy would be out of reach whenever fewer players than the
    threshold can be read, and every purchase would fall through to an eco --
    silently and plausibly.

    The scaling is a concession and not a refinement, and the reason says so
    out loud.
    """
    decision = classify_round(
        row(
            equip_buy_end=2 * 2000,
            equip_round_start=2 * 300,
            money_buy_end=2 * 3000,
            players_buy_end=2,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "half"
    assert "2/2 armed" in decision.reason
    assert "scaled to the number of players that could be read (2)" in decision.reason


def test_a_full_team_reason_does_not_mention_scaling(thresholds, economy) -> None:
    """The scaling sentence is only where scaling happens."""
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5 * 3000,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "half"
    assert "scaled" not in decision.reason


def test_a_distribution_that_disagrees_with_the_player_count_is_refused(
    thresholds, economy
) -> None:
    """Two divisors on one row is a fault, not a question of interpretation.

    ``players_buy_end`` is the divisor for the per player values, and the
    length of the distribution is the divisor for the counters. If they
    differ, "3/5" would mean a different set than the equipment value per
    player. The difference is not patched in either direction.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5000,
            players_buy_end=5,
            money_players_buy_end=[2000, 2000, 1000],
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "contradict each other" in decision.reason
    assert "players_buy_end says 5" in decision.reason


def test_more_armed_players_than_readable_ones_is_refused(
    thresholds, economy
) -> None:
    """``players_armed_buy_end`` cannot exceed the number that could be read.

    The adapter's contract promises ``0 <= armed <= players_buy_end``. If the
    promise is ever broken, the counter "6/5" would go through without anybody
    noticing.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 2000,
            equip_round_start=5 * 300,
            money_buy_end=5000,
            players_armed_buy_end=6,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "there are 6 armed players" in decision.reason


def test_the_purchase_threshold_is_inclusive_at_exactly_the_limit(
    thresholds,
    economy,
) -> None:
    """``>=``, not ``>``: a purchase exactly at the bound is already a purchase.

    Both of the bound's neighbours are pinned elsewhere; this pins the bound
    itself. Without it ``>=`` can turn into ``>`` without anything remarking
    on it.
    """
    def decision(bought_pp: int) -> str | None:
        return classify_round(
            row(
                equip_buy_end=5 * (bought_pp + 300),
                equip_round_start=5 * 300,
                money_buy_end=5 * 100,
            ),
            previous(won=False),
            thresholds,
            economy=economy,
            loss_count=2,
        ).round_type

    assert decision(thresholds.force_buy_min) == "force"
    assert decision(thresholds.force_buy_min - 1) == "eco"


def test_the_next_round_buying_power_is_inclusive_at_exactly_the_limit(
    thresholds,
    economy,
) -> None:
    """``>=``, not ``>``: a player reaching exactly the bound can already buy.

    Both of condition B's neighbours separated by one dollar. Without this
    ``>=`` can turn into ``>`` without anything remarking on it.
    """
    bonus = economy.loss_bonus_steps[2]  # loss count 2 -> step 2
    need = thresholds.normal_buy_money_min - bonus

    def decision(own_money: int) -> str | None:
        # Three players near the bound, two with no money: the counter is 3 or
        # 0, that is, exactly on either side of normal_buy_players_min.
        return classify_round(
            row(
                equip_buy_end=5 * 2000,
                equip_round_start=5 * 300,
                money_buy_end=3 * own_money,
                money_players_buy_end=[own_money] * 3 + [0, 0],
            ),
            previous(won=False),
            thresholds,
            economy=economy,
            loss_count=2,
        ).round_type

    assert decision(need) == "half"
    assert decision(need - 1) == "force"


def test_the_reason_never_contradicts_its_own_rounded_number(
    thresholds,
    economy,
) -> None:
    """P13: the comparison and the reason's number are the same rounded number.

    An unrounded comparison would produce the text "bought 1500 $/player, that
    is below 1500 $" -- in exactly the borderline case the reader wants to
    check. The amount bought is the only number the classification still
    divides by the number of players; the half-buy's conditions A and B are
    computed from the per-player observations and are not divided at all.
    """
    limit = thresholds.force_buy_min
    decision = classify_round(
        row(
            # 1500.4 $/player -> rounds to 1500, that is, exactly to the bound.
            equip_buy_end=5 * 300 + 5 * limit + 2,
            equip_round_start=5 * 300,
            money_buy_end=0,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "force"
    assert f"bought {limit} $/player, at least {limit} $" in decision.reason


def test_a_poor_team_that_did_not_buy_is_an_eco_not_a_force(
    thresholds,
    economy,
) -> None:
    """I/O matrix: a poor team -- till empty, purchase below the bound.

    "The money ran out" alone is not a force: a team buying armour and a
    pistol with its last money emptied the till but did not force. That is why
    ``force_buy_min`` is a **precondition** for a force and not just its band.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 1200,
            equip_round_start=5 * 300,
            money_buy_end=50,
            money_spent=4500,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=3,
    )
    assert decision.round_type == "eco"
    assert str(thresholds.force_buy_min) in decision.reason


def test_force_and_eco_differ_only_by_what_was_bought(thresholds, economy) -> None:
    """Same equipment value, another purchase: the amount bought separates."""
    bought = classify_round(
        row(equip_buy_end=9000, equip_round_start=1000, money_buy_end=500),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    saved = classify_round(
        row(equip_buy_end=9000, equip_round_start=8000, money_buy_end=500),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert bought.round_type == "force"
    assert saved.round_type == "eco"


def test_a_large_purchase_below_full_is_still_a_force(thresholds, economy) -> None:
    """A force has no upper bound: ``full_equip_min`` bounds it from above.

    Round 20 of the calibration demo ($2,710 per player bought, 2,910 of
    equipment) fell above the old band and was classified as an anomaly. The
    band is gone, so the same situation is now a force.
    """
    purchase_pp = 2710
    decision = classify_round(
        row(
            equip_buy_end=5 * 2910,
            equip_round_start=5 * (2910 - purchase_pp),
            money_buy_end=5 * 270,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "force"


def test_equipment_value_alone_does_not_make_a_half_buy(
    thresholds, economy
) -> None:
    """S2: a half-buy is settled by the players' money, not the equipment value.

    The same equipment value, the same purchase, the same armament -- and the
    result differs, because the players' balances differ. The difference now
    runs through condition B (the even split from
    :func:`conftest.even_split`), no longer through the retired fixed bound.
    If somebody wires the equipment value bound back into the half-buy
    decision, this test comes down.
    """
    shared = dict(equip_buy_end=5 * 3500, equip_round_start=1000)
    bought_empty = classify_round(
        row(money_buy_end=5 * 200, **shared),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    left_room = classify_round(
        row(money_buy_end=5 * 2500, **shared),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert bought_empty.round_type == "force"
    assert left_room.round_type == "half"


def test_a_half_buy_is_never_played_after_a_win(thresholds, economy) -> None:
    """S1: saving always reacts to a loss, so after a win it is a normal buy.

    The calibration's round 2: the CT side that won the pistol round buys
    $3,200 per player. The old classifier said ``half``; the product owner
    says ``full``.
    """
    decision = classify_round(
        row(round_no=2, equip_buy_end=16000, equip_round_start=1100),
        previous(won=True, round_no=1),
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.round_type == "full"
    assert "after a round that was won" in decision.reason


def test_low_value_after_a_win_is_an_anomaly_not_an_eco(thresholds, economy) -> None:
    decision = classify_round(
        row(
            equip_buy_end=5 * thresholds.anomaly_equip_max_after_win,
            equip_round_start=1000,
            money_buy_end=50000,
        ),
        previous(won=True),
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.round_type == "anomaly"
    assert "after a win" in decision.reason


def test_there_is_no_gap_left_after_a_win(thresholds, economy) -> None:
    """S1: after a win there is only a normal buy or an anomaly, no interval.

    The old classifier left a gap between the anomaly bound and the half-buy
    bound that fell through as an anomaly. The test is run across the whole
    interval that lies between the anomaly bound and the full buy.
    """
    low = thresholds.anomaly_equip_max_after_win
    high = thresholds.full_equip_min
    for equip_pp in (low + 1, (low + high) // 2, high - 1):
        decision = classify_round(
            row(
                equip_buy_end=5 * equip_pp,
                equip_round_start=5 * (equip_pp - 100),
            ),
            previous(won=True),
            thresholds,
            economy=economy,
            loss_count=1,
        )
        assert decision.round_type == "full", equip_pp


def test_a_saved_rifle_does_not_turn_an_eco_into_a_buy(thresholds, economy) -> None:
    """S3: a saved weapon raises the equipment value but is not a purchase.

    The calibration's round 11 CT: one saved M4, $600 per player bought. The
    product owner says ``eco`` -- a high equipment value must not turn it into
    a purchase.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * 1580,
            equip_round_start=5 * (1580 - 600),
            money_buy_end=5 * 2280,
            money_spent=5 * 600,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=3,
    )
    assert decision.round_type == "eco"
    assert "the kit saved is not" in decision.reason


def test_negative_purchase_is_an_anomaly_not_silenced_to_zero(
    thresholds,
    economy,
) -> None:
    """A fall in equipment value is not a purchase; zero would hide the clash."""
    decision = classify_round(
        row(equip_buy_end=10000, equip_round_start=14000),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert "equipment value fell" in decision.reason


def test_a_negative_purchase_beats_the_full_buy_rule(thresholds, economy) -> None:
    """The I/O matrix's row is absolute: a negative purchase -> anomaly.

    A high equipment value must not cover a contradictory observation. If the
    full buy check moved ahead of this one, the round would be classified as a
    full and the broken observation would disappear from view.
    """
    decision = classify_round(
        row(
            equip_buy_end=5 * (thresholds.full_equip_min + 1000),
            equip_round_start=5 * (thresholds.full_equip_min + 2000),
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert "equipment value fell" in decision.reason


def test_a_small_negative_purchase_is_not_rounded_away(thresholds, economy) -> None:
    """The sign is read off the team total, not the rounded per player number.

    A fall of two dollars across five players rounds to zero per player. It is
    still a contradictory observation, and it must not be damped.
    """
    decision = classify_round(
        row(equip_buy_end=9998, equip_round_start=10000),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert "-2 $ for the team" in decision.reason


# --- The previous round's contiguity --------------------------------------------


def test_missing_previous_round_is_an_anomaly(thresholds, economy) -> None:
    decision = classify_round(
        row(equip_buy_end=5000), None, thresholds, economy=economy, loss_count=1
    )
    assert decision.round_type == "anomaly"
    assert "to the previous round" in decision.reason


def test_a_full_buy_is_recognised_even_without_a_previous_round(
    thresholds,
    economy,
) -> None:
    """A deliberate exception to the calibration document's derived order.

    $5,000 per player is a full buy whether or not the previous round is
    known. On the first round of a half and in a gap in the round numbers
    there is no previous round, and there ``anomaly`` would claim of an
    obvious full buy that it cannot be classified. The previous round is
    needed only to separate eco, force and half-buy from each other.
    """
    decision = classify_round(
        row(equip_buy_end=5 * thresholds.full_equip_min),
        None,
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.round_type == "full"
    assert decision.inputs["prev_round_won"] is None


def test_a_gap_in_round_numbers_breaks_the_previous_round(thresholds, economy) -> None:
    """``rows[index - 1]`` is not the previous round if the numbers have a gap."""
    decision = classify_round(
        row(round_no=8, equip_buy_end=5000),
        previous(won=False, round_no=5),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "anomaly"
    assert decision.inputs["prev_round_won"] is None


def test_a_side_change_breaks_the_previous_round(thresholds, economy) -> None:
    """A change of side means the previous round is from the other half."""
    decision = classify_round(
        row(round_no=14, side="CT", equip_buy_end=5000),
        previous(won=False, round_no=13, side="T"),
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.round_type == "anomaly"
    assert decision.inputs["survivors_prev"] is None


def test_a_contiguous_previous_round_is_used(thresholds, economy) -> None:
    decision = classify_round(
        row(
            round_no=14,
            side="CT",
            equip_buy_end=5 * 3500,
            money_buy_end=5 * 2500,
        ),
        previous(won=False, round_no=13, side="CT", survivors=2),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "half"
    assert decision.inputs["prev_round_won"] is False
    assert decision.inputs["survivors_prev"] == 2


# --- A short-handed team and missing observations -------------------------------


def test_per_player_values_use_the_observed_player_count(thresholds, economy) -> None:
    """With four players the same team total exceeds the full buy bound."""
    total = 4 * thresholds.full_equip_min
    four_players = classify_round(
        row(equip_buy_end=total, players_buy_end=4),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    five_players = classify_round(
        row(equip_buy_end=total, players_buy_end=5),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert four_players.round_type == "full"
    assert five_players.round_type != "full"
    assert four_players.inputs["players"] == 4
    assert four_players.inputs["players_readable"] == 4
    assert "only 4 players' values" in four_players.reason


def test_unknown_player_count_falls_back_and_says_so(thresholds, economy) -> None:
    """I/O matrix: if the count is not known, that is recorded in the reason."""
    decision = classify_round(
        row(players_buy_end=None),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.inputs["players"] == thresholds.roster_size
    assert decision.inputs["players_readable"] is None
    assert "roster_size" in decision.reason


@pytest.mark.parametrize("observed", [0, -1, 6, 11])
def test_player_count_outside_the_roster_is_refused_as_a_divisor(
    thresholds, economy, observed
) -> None:
    """An extra or stale row in the tick would underestimate the per player
    values."""
    decision = classify_round(
        row(players_buy_end=observed),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.inputs["players"] == thresholds.roster_size
    assert decision.inputs["players_readable"] == observed
    assert "outside the allowed range" in decision.reason


def test_round_without_a_freeze_anchor_is_not_classified(thresholds, economy) -> None:
    decision = classify_round(
        row(
            status="no_freeze_end",
            money_buy_end=None,
            money_spent=None,
            equip_buy_end=None,
            equip_round_start=None,
            players_buy_end=None,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "no_freeze_end" in decision.reason


def test_missing_observation_without_a_status_is_not_classified(
    thresholds,
    economy,
) -> None:
    decision = classify_round(
        row(equip_buy_end=None),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "equipment value" in decision.reason


def test_missing_round_start_equipment_is_not_classified(thresholds, economy) -> None:
    """Read as zero, the whole equipment value would look bought this round.

    A genuine saving round would then get the verdict "force -- bought almost
    empty", that is, exactly the opposite of what happened in the demo.
    """
    decision = classify_round(
        row(equip_buy_end=9000, equip_round_start=None),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type is None
    assert "the equipment value at the start of the round" in decision.reason


def test_zero_roster_size_is_refused_instead_of_dividing_by_zero(
    thresholds,
    economy,
) -> None:
    zero = thresholds.model_copy(update={"roster_size": 0})
    with pytest.raises(SchemaError, match="roster_size"):
        classify_round(row(), previous(won=False), zero, economy=economy, loss_count=2)


# --- The reason and the input values --------------------------------------------


def test_every_decision_carries_money_and_loss_count_in_its_reason(
    thresholds,
    economy,
) -> None:
    """Without money and the loss count, Story 1.4's calibration would be
    impossible."""
    cases = [
        (row(round_no=1), previous(True, round_no=0), 1),
        (row(round_no=25), previous(False, round_no=24), 3),
        (row(), previous(False), 2),
        (
            row(equip_buy_end=2000, equip_round_start=1000),
            previous(False),
            2,
        ),
        (row(equip_buy_end=2000), previous(True), 1),
    ]
    for r, e, lc in cases:
        decision = classify_round(r, e, thresholds, economy=economy, loss_count=lc)
        assert "Available" in decision.reason, decision
        # ``(left `` and not ``left``: the shared tail is what this pins, and
        # the bare word also appears in the half-buy and force verdicts.
        assert "(left " in decision.reason, decision
        assert f"loss count {lc}" in decision.reason, decision


def test_eco_reason_names_the_purchase_it_compared_against(thresholds, economy) -> None:
    """An eco is settled by the amount bought; the reason shows both numbers.

    The reason still states both directions of the money too, so that the
    reader does not confuse the balance left over with the money that was
    available.
    """
    decision = classify_round(
        row(
            round_no=2,
            equip_buy_end=1600,
            equip_round_start=1000,
            money_buy_end=9000,
            money_spent=600,
        ),
        previous(won=False, round_no=1),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert decision.round_type == "eco"
    lowered = decision.reason.lower()
    assert f"bought only {per_player(600, 5)} $/player" in lowered
    assert f"below the force precondition {thresholds.force_buy_min} $" in lowered
    assert f"available {per_player(9000 + 600, 5)} $/player" in lowered
    assert "left 1800 $/player" in lowered


def test_available_money_is_the_sum_of_left_and_spent() -> None:
    assert available_money({"money_buy_end": 100, "money_spent": 900}) == 1000
    assert available_money({"money_buy_end": None, "money_spent": None}) is None
    assert available_money({"money_buy_end": 100, "money_spent": None}) == 100


def test_inputs_match_the_classified_schema_exactly(thresholds, economy) -> None:
    """``inputs`` is a schema contract, not a free dictionary."""
    decision = classify_round(
        row(), previous(False), thresholds, economy=economy, loss_count=2
    )
    assert tuple(decision.inputs) == INPUT_FIELDS
    assert set(decision.inputs) == {f.name for f in CLASSIFIED_INPUTS.fields}


def test_inputs_carry_every_threshold_the_rules_compare_against(
    thresholds,
    economy,
) -> None:
    decision = classify_round(
        row(), previous(False), thresholds, economy=economy, loss_count=2
    )
    assert decision.inputs["full_equip_min"] == thresholds.full_equip_min
    assert decision.inputs["force_buy_min"] == thresholds.force_buy_min
    assert decision.inputs["armed_players_min"] == thresholds.armed_players_min
    assert (
        decision.inputs["normal_buy_money_min"] == thresholds.normal_buy_money_min
    )
    assert (
        decision.inputs["normal_buy_players_min"]
        == thresholds.normal_buy_players_min
    )
    assert (
        decision.inputs["anomaly_equip_max_after_win"]
        == thresholds.anomaly_equip_max_after_win
    )


def test_inputs_no_longer_carry_the_retired_thresholds(thresholds, economy) -> None:
    """The retired thresholds went from everywhere, the input values included.

    A half-done clean-up would leave a table column with no reader -- and the
    next reader would think it said something about the decision.
    """
    decision = classify_round(
        row(), previous(False), thresholds, economy=economy, loss_count=2
    )
    for retired in (
        "eco_money_max",
        "eco_money_max_low_loss",
        "eco_loss_count_min",
        "eco_money_max_applied",
        "force_money_min",
        "force_money_max",
        "half_equip_min",
        # Story 1.10: the fixed bound on the money left in the pocket was
        # replaced by the per-player conditions A and B.
        "force_money_left_max",
    ):
        assert retired not in decision.inputs
        assert not hasattr(thresholds, retired)


def test_inputs_carry_the_previous_round_state(thresholds, economy) -> None:
    decision = classify_round(
        row(survivors_equip_prev=4200),
        previous(won=True, survivors=3),
        thresholds,
        economy=economy,
        loss_count=1,
    )
    assert decision.inputs["prev_round_won"] is True
    assert decision.inputs["survivors_prev"] == 3
    assert decision.inputs["survivors_equip_prev"] == 4200


def test_bought_and_available_are_recoverable_without_new_columns(
    thresholds,
    economy,
) -> None:
    """Both derivations come out of ``inputs`` without a schema change."""
    decision = classify_round(
        row(
            equip_buy_end=12000,
            equip_round_start=1000,
            money_buy_end=400,
            money_spent=11000,
        ),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    assert (
        decision.inputs["equip_buy_end"] - decision.inputs["equip_round_start"] == 11000
    )
    assert decision.inputs["money_buy_end"] + decision.inputs["money_spent"] == 11400


def test_per_player_rounds_the_same_way_everywhere(thresholds, economy) -> None:
    """The same number must not differ by a dollar in the table and the reason."""
    decision = classify_round(
        row(equip_buy_end=12345, equip_round_start=1000),
        previous(won=False),
        thresholds,
        economy=economy,
        loss_count=2,
    )
    expected = per_player(12345, 5)
    assert f"{expected} $/player" in decision.reason


def test_decision_unpacks_as_a_triple(thresholds, economy) -> None:
    round_type, reason, inputs = classify_round(
        row(), previous(False), thresholds, economy=economy, loss_count=2
    )
    assert isinstance(round_type, str)
    assert isinstance(reason, str)
    assert isinstance(inputs, dict)
    assert isinstance(
        classify_round(
        row(), previous(False), thresholds, economy=economy, loss_count=2
    ), Decision
    )


def test_a_threshold_change_changes_the_verdict(thresholds, economy) -> None:
    """Thresholds are settings, not code: same row, another bound, another
    result."""
    stricter = thresholds.model_copy(update={"full_equip_min": 6000})
    r = row(equip_buy_end=25000, equip_round_start=1000)
    baseline = classify_round(
        r, previous(False), thresholds, economy=economy, loss_count=2
    )
    assert baseline.round_type == "full"
    tighter = classify_round(
        r, previous(False), stricter, economy=economy, loss_count=2
    )
    assert tighter.round_type != "full"
