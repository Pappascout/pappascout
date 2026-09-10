"""The round's economy reasoning: loss count and round type (AD-4).

This module is the brain of the ``classify`` stage. It reads no demo, no
files and no settings file -- it is given the rows of the rounds table and
the ``[thresholds]`` section, and it returns for every round a type, a
**human-readable reason** and **every value used in the comparison**. The
reason and the input values are not decoration: without them calibrating the
thresholds would be guesswork, and the product owner could not check the
tool's verdict against the demo.

What is observed and what is derived
------------------------------------
``parse`` observes: money and money spent at the end of buy time, the
equipment value at the end of buy time, the equipment value at the start of
the round, the number of players that could be read, the survivors and the
winner. This module derives the loss count and the round type from those.
Nothing derived is written back into the ``parsed/`` directory.

The two directions of money -- read this before you change a threshold
----------------------------------------------------------------------
``money_buy_end`` is the **balance left over after buy time**, not the money
that was available. On a saving round it is therefore *large* and on a full
buy *small*. The money that was available is obtained as the sum::

    available = money_buy_end + money_spent

The calibration of 2026-08-29 showed that **the decision leans on the balance
left over**, not on the money that was available: that is what separates a
force from a half-buy (S2 below). The money that was available still travels
in the reason and in the ``inputs`` structure, because it explains to the
reader how the team's situation came about, but no rule compares against it
any more.

The amount bought is the difference ``equip_buy_end - equip_round_start``. It
is the only directly observed measure of whether the team bought or not, and
it is exactly what separates a buy round (a force or a half-buy) from an eco.

The default pistol (Glock / USP-S / P2000) is free but is counted into the
equipment value as being worth **$200**, so every player always has at least
$200 of equipment value and a full eco is about $1,000 for the team, not 0.
The thresholds are kept in the raw equipment value; the pistol's share is not
subtracted.

Three hard rules (calibration 2026-08-29)
-----------------------------------------
These are rules, not thresholds. They are not tuned with numbers. They come
from the ``kalibrointi-kierrostyypit.md`` document, which is the truth given
by a human: if this module and that document disagree, **this module is
wrong**.

* **S1 -- Saving is always a reaction to a loss.** After a round that was won
  a team makes a normal buy. So after a win there is never ``eco``, ``force``
  or ``half``; the only exception is an equipment value so low that it is not
  a buy at all -- that is an ``anomaly``.
* **S2 -- A force and a half-buy differ by the money left in the pocket, not
  by the equipment value.** Force = bought until empty. Half-buy = bought,
  but left room for the next round. **The condition is computed from the
  players, not from an average** (see "The half-buy's two conditions" below).
* **S3 -- A saved weapon is not a purchase.** What decides is the amount
  bought on this round (``equip_buy_end - equip_round_start``), not the
  equipment value. The kit the survivors saved raises the equipment value
  without anything having been bought, and a rule leaning on the equipment
  value would turn such an eco into a half-buy.

Rule order
----------
The order is deliberately steep, and the first match wins:

1. **A missing observation** -- ``status != "ok"``, or money, the equipment
   value or the equipment value at the start of the round is empty. The round
   is not classified: ``round_type`` is ``None`` and the reason says why. The
   run does not come down. A missing input value is not replaced with zero:
   zero would claim that the whole equipment value was bought on this round,
   and would turn a genuine saving round into a force.
2. **Pistol** -- from the round number (``pistol_rounds``), not from money.
3. **Overtime** -- ``round_no > regulation_rounds``.
4. **A negative purchase** -- the equipment value fell from the start of the
   round to the end of buy time. The observations contradict each other, so
   the result is ``anomaly``; damping it to zero would hide the fault. This is
   **before** the full buy: if the observations contradict each other, no
   class is read off them, however high the equipment value is.
5. **A full buy** -- equipment value/player ``>= full_equip_min``.
6. **There is no previous round** -- eco, force and half-buy are rules
   *relative to the previous round*, so without it the result is ``anomaly``.
7. **After a win** (S1) -- ``full``, except when the equipment value stayed at
   ``anomaly_equip_max_after_win`` or below: then ``anomaly``. After a win
   there is no eco, no force and no half-buy.
8. **After a loss** -- a purchase is the shared precondition of them all, and
   after it two conditions decide the class::

       equipment >= full_equip_min                    -> full  (step 5)
       bought < force_buy_min                         -> eco   (S3)
       bought >= force_buy_min:
           an observation is missing or contradictory -> not classified
           condition A fails (too few armed)          -> eco
           money does not carry to the next round     -> force
           condition A holds, condition B does not    -> force
           both hold                                  -> half

   The first row is the same rule as step 5 and already hits there; it is here
   so that the loss branch can be read on its own.

The half-buy's two conditions (Story 1.10)
------------------------------------------
The product owner's definition is **two-directional**:

    "A half-buy is not a force when a normal buy is made possible on the
    next round, and is not an eco when there is enough value in use."

**Condition A -- kit.** At least ``armed_players_min`` players had armour and
some upgraded weapon at the end of buy time (the observation
``players_armed_buy_end``, Story 1.6). This separates the half-buy from an
**eco**: below that the round is not really played.

**Condition B -- the next round's wealth.** At least
``normal_buy_players_min`` players can make a normal buy on the next round:
their own balance at the end of buy time plus the loss bonus reaches
``normal_buy_money_min``. This separates the half-buy from a **force**.

**Both must hold, and neither is enough on its own.** The conditions measure
different things: A looks at the kit bought for this round, B at the buying
power of the next one. ``inferno_vs_ryhmarama`` rounds 6 and 10 both have
**five armed players**, so condition A does not separate them at all; the
separation is made by condition B -- on round 6 not one of the five can buy
(the product owner: force), on round 10 all five can (the product owner:
half-buy). Round 11 confirmed the prediction with a **normal buy** -- five
AKs, $4,940/player -- and it is pinned as a row of its own in
``test_calibration.py``'s ``INFERNO_TRUTH``, so that the claim does not live
in comments alone.

What is measured here and what is not
-------------------------------------
**Not one measured round separates this rule from the retired average rule.**
Six demos hold 23 buy rounds after a loss, and the old ``force_money_left_max``
would give every one of them the same class as conditions A and B do. The most
uneven distribution in the data (Anubis round 6 CT: 5,050, 4,500, 2,700, 2,250,
2,150) goes the same way too.

Rounds 6 and 10 are not a counterexample to the old rule. The old rule
classified round 6 wrongly only **before Story 1.9**, when money was read at
the end of freezetime and not at the end of buy time; the measurement made the
correction, not the rule.

The rule's justification is therefore in two parts, and neither part is "a
measurement overturned the previous rule":

1. **It implements the user's own definition**, which is per player: *"how
   much money has been left in the pocket and what that means for the next
   buy"* -- a question about individual players, not about the team's average.
2. **It stands up to an uneven distribution.** The hand-built rows
   (``test_economy.py``) show it directly: the same team total, a different
   distribution, a different verdict. No threshold lets an average separate
   them.

So the data does not test the rule yet. The first round on which money has
piled up on a few players is also the first one that can overturn it.

Why the distribution and not the average
----------------------------------------
Condition B is computed from the **per-player money distribution**
(``money_players_buy_end``), not from the team total. An average hides exactly
what is at stake: a team where one player has 5,000 and four have nothing gets
the same average as a team where everyone has 1,000, but in the former four
out of five cannot buy anything. An average also gives impossible numbers: the
calibration's round 19 CT showed "$30/player" when the real balances were 0,
0, 50, 50, 50 -- every price is a multiple of fifty, so 30 cannot be anyone's
balance.

The same fault **can be** in the rule and not only in the presentation: the
retired ``force_money_left_max`` was a fixed limit on the team total divided by
five, so a team of which four out of five cannot buy anything would pass it
too. There is no such round in the data so far -- the claim is therefore about
the rule's structure, not about an observation.

Why the bonus is computed on the assumption of a loss
-----------------------------------------------------
A half-buy is a decision made in preparation for not winning this round. If the
team wins, more money comes in and there is no question. So the rule asks: *if
this goes wrong, can we still afford it?* That is why the bonus is read off the
loss count, which **already is** the step that is paid for losing this round
(:func:`loss_bonus_if_lost`).

Why ``loss_count`` returns to the decision
------------------------------------------
It left the decision in Story 1.4, because no rule compared against it any
more. Now it has a job: the loss bonus is directly a function of it
(``[economy].loss_bonus_steps``, the steps $1,400-3,400), and it is precisely
the bonus that decides the separation. On rounds 6 and 10 the money left in the
pocket is of the same order, but the bonus is 1,900 against 3,400 -- and that
moves the boundary.

The bonus is not hard-coded: the steps are read from the settings, and that is
why this module is given the ``[economy]`` section as well. ``stages.classify``
takes it into its parameter hash, so changing a step invalidates the result of
the classification.

Why the full buy is decided before the previous round is known
--------------------------------------------------------------
The calibration document's derived order checks the previous round before the
full buy. In this module step 5 is deliberately before step 6: $5,000/player is
a full buy whether or not the previous round is known. Only on the first round
of a half and in a gap in the round numbers is there no previous round, and
there ``anomaly`` would claim of an obvious full buy that it cannot be
classified. Knowing the previous round is needed only to separate eco, force
and half-buy from each other -- not to recognise a full buy. The order is
pinned by a test so that it does not change by accident.

Why ``force_buy_min`` is a **condition** for a force and not its band
---------------------------------------------------------------------
The old model compared the amount bought against the band ``force_money_min``
.. ``force_money_max``. The upper bound made round 20 of the calibration demo
an anomaly: $2,710/player exceeded the band but stayed below the full buy, and
no rule covered it. The bound from above is now ``full_equip_min``, so the band
is not needed.

The lower bound, on the other hand, is needed, and "the money ran out" alone is
not enough for a force: a poor team that buys armour and a pistol with its last
money emptied the till but did not force. That is why ``force_buy_min`` is the
shared precondition of every buy rule after a loss, and only after it do
conditions A and B separate eco, force and half-buy from each other.

``force_buy_min`` **is observed**: the forces in the calibration data bought
$1,840-2,710 and the ecos $120-950 per player, so the chosen 1,500 is in an
empty gap and margin is left in both directions (550 to the ecos, 340 to the
forces). It is not the midpoint of the gap and it does not have to be; what
matters is that neither observed set is close.

There are no gaps left
----------------------
The loss branch is exhaustive: ``eco`` covers every round on which the
purchase precondition ``force_buy_min`` is not met, and inside the purchase
branch ``force`` is the last row that is reached when nothing above it
matched, so no interval is left in the economy reasoning that would fall
through as an anomaly. ``anomaly`` is now reserved for situations in which
the **observation** is contradictory (a negative purchase, a missing or
non-contiguous previous round) or in which practically nothing was bought after
a win.

Known limitations
-----------------
* **Overtime is flattened into a single ``ot`` type.** Teams save and force in
  overtime too, but the economy model is different (starting money
  ``league.ot_start_money``, no eco cycle), and the ``[league]`` section does
  not affect the reasoning in this story at all -- only the manifest's
  parameter hash and the round list's heading. Overtime's own economy
  reasoning is v2.
* **A buy round after a loss requires both per-player observations.** If
  ``players_armed_buy_end`` or ``money_players_buy_end`` is missing -- or if
  they contradict ``players_buy_end`` -- the round is not classified at all.
  Conditions A and B cannot be guessed from the team total. The other branches
  (pistol, overtime, full buy, after a win, an eco without a purchase) do not
  read them and so do not come down over their absence.
* **On the last round of a half condition B is not computed.** The money does
  not carry over to a pistol round or to overtime, so the money left in the
  pocket has not been left in order to leave room -- the result is ``force``
  (see :func:`_money_carries_over`). So the rule cannot produce a half-buy on a
  round on which saving is impossible.
* **On a short-handed team both player counters are scaled to the number that
  could be read** (``min(threshold, readable)``). Three armed players cannot be
  observed from two players, and without the scaling a half-buy would be out of
  reach whenever fewer players than the threshold can be read. The scaling is
  stated in the reason. It is a concession, not a refinement: from two readable
  players it cannot be deduced what the other three did.
* ``normal_buy_money_min`` is **one player's own balance**, not the team's
  average -- unlike every other money limit in this module, which are per
  player values. The difference is the whole reason for the rule.
* ``armed_players_min`` **is a stated rule, not an observation.** The user gave
  the limit ("at least three with kevlar and some upgraded weapon"), but not one
  calibrated round tests it: the only lightly armed round in the data (Ancient
  21 T, 2/5) is already settled by the buy limit ``force_buy_min`` and never
  reaches condition A. The same goes for ``normal_buy_players_min``: the
  observations are 0/5 and 5/5, so any value between 1 and 5 would produce the
  same verdicts.

The module is pure and is tested with hand-built tables without demos.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NamedTuple

import polars as pl

from pappascout.domain.models import EconomySettings, ThresholdSettings
from pappascout.domain.schemas import ARMED_COLUMN, MONEY_DISTRIBUTION_COLUMN
from pappascout.errors import SchemaError

__all__ = [
    "Decision",
    "INPUT_FIELDS",
    "LOSS_COUNT_COLUMNS",
    "CLASSIFY_COLUMNS",
    "available_money",
    "per_player",
    "loss_bonus_if_lost",
    "players_who_can_buy",
    "loss_counts",
    "classify_round",
]


class Decision(NamedTuple):
    """One round's classification decision.

    A NamedTuple, so it unpacks in the form ``round_type, reason, inputs =
    ...`` but the fields can also be read by name.

    Attributes:
        round_type: The round type, or ``None`` if the round could not be
            classified (an observation is missing).
        reason: The reason, which names the values that settled the decision.
            It always contains the money and the loss count.
        inputs: Every value and threshold used in the comparison (the
            ``schemas.CLASSIFIED_INPUTS`` structure).
    """

    round_type: str | None
    reason: str
    inputs: dict[str, Any]


#: The columns :func:`loss_counts` needs.
LOSS_COUNT_COLUMNS: tuple[str, ...] = ("round_no", "side", "won")

#: The columns :func:`classify_round` reads off a round row.
#:
#: **This is not documentation but a choice.**
#: :func:`~pappascout.stages.classify._classify_team` picks exactly these
#: columns out of the rounds table before it hands the rows over here, so
#: dropping a column from the list drops it from the decision as well. Without
#: that the list would be a comment, and a comment can go stale in silence.
#:
#: ``won`` and ``survivors`` are here because they are read off the
#: **previous** row (S1 and ``inputs.survivors_prev``) -- the same set of rows
#: goes round in both roles.
CLASSIFY_COLUMNS: tuple[str, ...] = (
    "round_no",
    "side",
    "status",
    "won",
    "survivors",
    "money_buy_end",
    "money_spent",
    "equip_buy_end",
    "equip_round_start",
    "players_buy_end",
    # The half-buy's two conditions. Neither can be computed from the team
    # total, and that is exactly why they are observations of their own in
    # the rounds table.
    ARMED_COLUMN,
    MONEY_DISTRIBUTION_COLUMN,
    "survivors_equip_prev",
)

#: The fields of the ``CLASSIFIED_INPUTS`` structure in the order in which
#: they are written. The names are locked in ``domain/schemas.py``.
INPUT_FIELDS: tuple[str, ...] = (
    "money_buy_end",
    "money_spent",
    "money_players",
    "equip_buy_end",
    "equip_round_start",
    "survivors_prev",
    "survivors_equip_prev",
    "prev_round_won",
    "players",
    "players_readable",
    "players_armed",
    "loss_bonus_if_lost",
    "players_can_buy",
    "full_equip_min",
    "force_buy_min",
    "armed_players_min",
    "normal_buy_money_min",
    "normal_buy_players_min",
    "anomaly_equip_max_after_win",
)


def available_money(row: Mapping[str, Any]) -> int | None:
    """The money available on the round = left over + spent.

    ``None`` if neither part is known.

    **No rule compares against this number.** An eco is told apart by the
    amount bought (``force_buy_min``), and a force is told apart from a
    half-buy by the per-player money distribution (condition B). The money
    that was available is in the reason and in the ``inputs`` structure
    because it explains to the reader how the team's situation came about --
    and so that the balance left over is not taken for the money that was
    available.
    """
    left = row.get("money_buy_end")
    spent = row.get("money_spent")
    if left is None and spent is None:
        return None
    return int(left or 0) + int(spent or 0)


def per_player(value: Any, players: int) -> int | None:
    """A dollar amount per player as a whole number.

    The **only** place where a per player value is rounded. That is why the
    table and the reason show the same number on the same row; two different
    roundings would separate them by a dollar.
    """
    if value is None or not players:
        return None
    return round(int(value) / players)


def loss_bonus_if_lost(
    loss_count: int,
    thresholds: ThresholdSettings,
    economy: EconomySettings,
) -> int:
    """The loss bonus the team gets **if this round is lost**.

    The bonus is read off the steps ``[economy].loss_bonus_steps``
    ($1,400-3,400) -- it is not hard-coded. **The index is the loss count as
    it is**, not ``loss_count + 1``: the counter describes the state on going
    into the round, and that is exactly the step that is paid if the round is
    lost. ``settings.toml`` says the same directly -- the start of a half
    (``loss_count_half_start = 1``) gives $1,900 for losing the pistol round,
    and that is the value of step 1.

    A half-buy is a decision made in preparation for a loss, so this is the
    number condition B is computed with. On a win there is no question: more
    money then comes in than from the bonus.

    Measured: ``inferno_vs_ryhmarama`` round 6 comes to $1,900 with counter 1
    and round 10 to $3,400 with counter 4 (the cap). The difference is $1,500,
    and it moves the boundary -- the money left in the pocket is of the same
    order on both rounds.

    Args:
        loss_count: The counter in force on going into this round.
        thresholds: The ``[thresholds]`` section (the counter's cap).
        economy: The ``[economy]`` section (the steps).

    Returns:
        The bonus in dollars for **one player**.
    """
    steps = economy.loss_bonus_steps
    index = min(int(loss_count), thresholds.loss_count_max)
    # Loading the settings requires exactly loss_count_max + 1 steps, so the
    # clamp is a safeguard and not a rule: without it a hand-built
    # EconomySettings or a negative counter would bring the classification
    # down with an IndexError instead of giving the outermost step.
    return int(steps[max(0, min(index, len(steps) - 1))])


def players_who_can_buy(
    money_players: list[int] | tuple[int, ...],
    loss_bonus: int,
    thresholds: ThresholdSettings,
    economy: EconomySettings,
) -> int:
    """How many players can make a normal buy on the next round.

    Condition B. A player can if their **own** balance at the end of buy time
    plus the loss bonus reaches ``normal_buy_money_min``.

    ``normal_buy_money_min`` is one player's own balance, not the team's
    average. An average hides exactly what is at stake here: a team where one
    player has 5,000 and four have nothing gets the same average as a team
    where everyone has 1,000, but in the former four out of five cannot buy
    anything.

    **The sum is clamped to the money cap** (``[economy].max_money``). The
    game gives a player no more than that, so without the clamp the counter
    would promise buying power out of money the game would cut away. With the
    current values the cap is $16,000 and does not bite, but it is part of the
    model and not an accident.

    Args:
        money_players: The money distribution, one element per player that
            could be read (``ROUNDS.money_players_buy_end``). It must not
            contain empty values: a read error as a zero would claim the
            player has no money.
        loss_bonus: The result of :func:`loss_bonus_if_lost`.
        thresholds: The ``[thresholds]`` section.
        economy: The ``[economy]`` section (the money cap).

    Returns:
        A counter in the range ``0..len(money_players)``.

    Raises:
        SchemaError: If the distribution holds an empty value. The function is
            public, so the contract cannot live in the callers alone -- a
            silent zero would look like a force.
    """
    if any(money is None for money in money_players):
        raise SchemaError(
            "players_who_can_buy: the money distribution holds an empty "
            "value. A missing balance is not replaced with zero, because that "
            "would claim the player has no money and would turn a half-buy "
            "into a force. Give a distribution in which every element is "
            "observed, or leave the round unclassified."
        )
    return sum(
        1
        for money in money_players
        if min(int(money) + int(loss_bonus), economy.max_money)
        >= thresholds.normal_buy_money_min
    )


def loss_counts(team_rounds: pl.DataFrame, thresholds: ThresholdSettings) -> list[int]:
    """Compute one team's loss count for every round.

    The counter describes the state **on going into the round**: on the first
    round of a half it is ``loss_count_half_start``, and after that the
    previous round's result moves it by one (a loss up, a win down) inside the
    bounds ``loss_count_min``..``loss_count_max``.

    A half is recognised **from the ``side`` column changing**, not from the
    round number. The round number would fail in overtime and on a demo whose
    second half does not start at round 13.

    A round whose result is unknown (``won`` is empty) does not move the
    counter in either direction -- a guess would distort every round that
    follows.

    Args:
        team_rounds: One team's rows, **one row per round** and ordered
            ascending by the ``round_no`` column. The required columns are
            :data:`LOSS_COUNT_COLUMNS`.
        thresholds: The ``[thresholds]`` section.

    Returns:
        A list whose elements correspond to the input rows in the same order.

    Raises:
        SchemaError: If a column is missing, ``round_no`` or ``side`` is
            empty, or the rows are not in ascending round order without
            repeats.
    """
    missing = [name for name in LOSS_COUNT_COLUMNS if name not in team_rounds.columns]
    if missing:
        raise SchemaError(
            "loss_counts needs the columns "
            f"{', '.join(LOSS_COUNT_COLUMNS)}; missing: {', '.join(missing)}."
        )
    if team_rounds.is_empty():
        return []

    rows = team_rounds.select(list(LOSS_COUNT_COLUMNS)).to_dicts()
    numbers = [r["round_no"] for r in rows]
    if any(n is None for n in numbers):
        raise SchemaError(
            "loss_counts: round_no holds empty values. The loss count is a "
            "counter tied to the order of the rounds, so an unnumbered round "
            "cannot be part of it."
        )
    if any(r["side"] is None for r in rows):
        raise SchemaError(
            "loss_counts: side holds empty values. A half is recognised from "
            "the side changing, so an empty side would reset the counter "
            "silently and distort every round that follows."
        )
    if any(b <= a for a, b in zip(numbers, numbers[1:])):
        raise SchemaError(
            "loss_counts: the rows are not in ascending round order, or the "
            "same round appears twice. Give one team's rows ordered by the "
            "round_no column."
        )

    result: list[int] = []
    counter = thresholds.loss_count_half_start
    previous_side: str | None = None
    previous_won: bool | None = None

    for row in rows:
        side = str(row["side"])
        if previous_side is None or side != previous_side:
            counter = thresholds.loss_count_half_start
        elif previous_won is True:
            counter = max(thresholds.loss_count_min, counter - 1)
        elif previous_won is False:
            counter = min(thresholds.loss_count_max, counter + 1)
        result.append(counter)
        previous_side = side
        previous_won = None if row["won"] is None else bool(row["won"])

    return result


def classify_round(
    row: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    thresholds: ThresholdSettings,
    *,
    economy: EconomySettings,
    loss_count: int,
) -> Decision:
    """Classify one round from one team's point of view.

    The rule order is in the module docstring. The first rule that matches
    wins, and nothing is guessed: an unknown situation is an ``anomaly`` with
    its reason.

    Args:
        row: The round row, the columns :data:`CLASSIFY_COLUMNS`.
        previous: The same team's previous round row, or ``None``.
            **Contiguity is checked here**: a row is accepted as the previous
            one only if its ``round_no`` is exactly one smaller and its
            ``side`` is the same. Otherwise the round has no previous round,
            and the rules for after a win or a loss are not applied.
        thresholds: The ``[thresholds]`` section.
        economy: The ``[economy]`` section. ``loss_bonus_steps`` (the
            half-buy's condition B) and ``max_money`` (its money cap) are read
            from it. The section is a parameter as a whole, because
            ``stages.classify`` takes it into its parameter hash as it is. A
            **keyword parameter**: given positionally it would silently bind
            in ``thresholds``'s place, and two pydantic sections would pass
            the type check swapped.
        loss_count: The counter in force on going into this round, from
            :func:`loss_counts`. It returned to the decision in Story 1.10:
            the loss bonus is directly a function of it.

    Returns:
        A :class:`Decision`, which also unpacks in the form
        ``(round_type, reason, inputs)``.
    """
    players, readable, divisor_ok = _players(row, thresholds)
    previous = _continuous_previous(row, previous)
    inputs = _inputs(
        row, previous, thresholds, economy, players, readable, loss_count
    )

    round_no = row.get("round_no")
    if round_no is None:
        return Decision(
            None, "The round has not been numbered, so it is not classified.", inputs
        )
    round_no = int(round_no)

    status = row.get("status")
    if status is not None and str(status) != "ok":
        return Decision(
            None,
            f"Round {round_no} is not classified: the round's status is "
            f"{status!r}, so the observations from the end of buy time are "
            "missing.",
            inputs,
        )

    money = row.get("money_buy_end")
    equip = row.get("equip_buy_end")
    equip_start = row.get("equip_round_start")
    missing = [
        name
        for name, value in (
            ("money", money),
            ("equipment value", equip),
            ("the equipment value at the start of the round", equip_start),
        )
        if value is None
    ]
    if missing:
        return Decision(
            None,
            f"Round {round_no} is not classified: the end of buy time is "
            f"missing {', '.join(missing)}. A missing value is not replaced "
            "with zero, because that would claim the whole equipment value "
            "was bought on this round.",
            inputs,
        )

    basis = _basis(row, thresholds, players, readable, loss_count, divisor_ok)

    if round_no in thresholds.pistol_rounds:
        return Decision(
            "pistol",
            f"Round {round_no} is a pistol round "
            f"({_listing(thresholds.pistol_rounds)}), so the economy "
            f"reasoning is not applied. {basis}",
            inputs,
        )

    if round_no > thresholds.regulation_rounds:
        return Decision(
            "ot",
            f"Round {round_no} is overtime (regulation rounds "
            f"{thresholds.regulation_rounds}), so the economy reasoning is "
            f"not applied. {basis}",
            inputs,
        )

    # Every per player number that is compared is rounded **once**, and the
    # reason prints exactly the same numbers. If the comparison were made
    # with an unrounded float, the reason could say "bought 1500 $, that is
    # below 1500 $" -- the text and the decision would contradict each other
    # in exactly the borderline case the reader wants to check.
    #
    # In the half-buy's conditions A and B there is no such problem at all:
    # they are computed from the per-player observations and not from the
    # team total, so nothing is divided and nothing is rounded.
    #
    # The equipment value and the equipment value at the start of the round
    # have just been found to exist, and there is always at least one player,
    # so these cannot be None.
    equip_pp = per_player(equip, players) or 0
    bought = int(equip) - int(equip_start)
    bought_pp = per_player(bought, players) or 0

    # A contradictory observation before the full buy: if the equipment value
    # fell during buy time, no class is read off the numbers, however high
    # the equipment value is. The sign is read off the team total, because
    # rounding per player could damp a small fall to zero.
    if bought < 0:
        return Decision(
            "anomaly",
            f"The equipment value fell from the start of the round to the end "
            f"of buy time ({bought} $ for the team, {_d(bought_pp)} $/player), "
            "which is not a purchase. The observations contradict each other, "
            f"and the difference is not damped to zero. {basis}",
            inputs,
        )

    if equip_pp >= thresholds.full_equip_min:
        return Decision(
            "full",
            f"Full buy: equipment value {_d(equip_pp)} $/player, at least "
            f"{thresholds.full_equip_min} $. {basis}",
            inputs,
        )

    previous_won = None if previous is None else previous.get("won")
    if previous_won is None:
        return Decision(
            "anomaly",
            "There is no previous round or its result is not known, so the "
            "eco, force and half-buy rules cannot be applied -- they hold "
            f"only in relation to the previous round. {basis}",
            inputs,
        )

    if bool(previous_won):
        # S1: saving is a reaction to a loss, so after a win there is no eco,
        # no force and no half-buy -- only a normal buy or an anomaly.
        if equip_pp <= thresholds.anomaly_equip_max_after_win:
            return Decision(
                "anomaly",
                f"A low equipment value after a win: {_d(equip_pp)} "
                f"$/player, at most {thresholds.anomaly_equip_max_after_win} $. "
                "No eco, force or half-buy is played after a win, so this is "
                f"an anomaly and not an eco. {basis}",
                inputs,
            )
        return Decision(
            "full",
            f"A normal buy after a round that was won: equipment value "
            f"{_d(equip_pp)} $/player exceeds the low equipment value limit "
            f"{thresholds.anomaly_equip_max_after_win} $. Saving is always a "
            "reaction to a loss, so after a win no eco, force or half-buy is "
            f"made. {basis}",
            inputs,
        )

    # The previous round was lost. The full buy has already been settled in
    # step 5, so what is left is force, half-buy and eco. Their shared
    # precondition is that the team really bought (S3): a saved weapon raises
    # the equipment value but is not a purchase.
    if bought_pp >= thresholds.force_buy_min:
        return _after_loss_purchase(
            row,
            thresholds,
            economy,
            bought_pp=bought_pp,
            loss_count=loss_count,
            round_no=round_no,
            readable=readable,
            inputs=inputs,
            basis=basis,
        )

    return Decision(
        "eco",
        f"Eco after a round that was lost: bought only {_d(bought_pp)} "
        f"$/player, below the force precondition {thresholds.force_buy_min} $. "
        f"The equipment value {_d(equip_pp)} $/player does not decide it: the "
        f"kit saved is not a purchase made on this round. {basis}",
        inputs,
    )


# -- Helpers -------------------------------------------------------------------


def _money_carries_over(round_no: int, thresholds: ThresholdSettings) -> bool:
    """Does the money left in the pocket carry from this round to the next?

    It does not in two situations, and in both the balance is reset:

    * **The next round is a pistol round** (the first of a half). The game
      then gives everyone ``[economy].start_money``.
    * **The next round is overtime.** Overtime has its own starting money
      (``league.ot_start_money``, $12,500 in Pappaliiga).

    Condition B asks "is there enough for a normal buy on the next round", and
    in these two cases the question is meaningless: the money left in the
    pocket evaporates. See :func:`_after_loss_purchase` for what the rule does
    then.
    """
    following = round_no + 1
    return not (
        following in thresholds.pistol_rounds
        or following > thresholds.regulation_rounds
    )


def _after_loss_purchase(
    row: Mapping[str, Any],
    thresholds: ThresholdSettings,
    economy: EconomySettings,
    *,
    bought_pp: int,
    loss_count: int,
    round_no: int,
    readable: int | None,
    inputs: dict[str, Any],
    basis: str,
) -> Decision:
    """Eco, force or half-buy -- when after a loss the team really bought.

    Two conditions, and **both must hold** for the round to be a half-buy:

    * **Condition A (kit)** separates the half-buy from an **eco**: at least
      ``armed_players_min`` players had armour and a weapon at the end of buy
      time. Below that the round is not really played.
    * **Condition B (the next round's wealth)** separates it from a **force**:
      at least ``normal_buy_players_min`` players can make a normal buy on the
      next round.

    The conditions measure different things and neither replaces the other,
    but **not one measured round separates them from the retired average rule
    yet**: six demos hold 23 buy rounds after a loss, and the old rule would
    give every one of them the same class. The difference shows only on an
    uneven distribution, which ``test_economy.py`` builds by hand: the same
    team total, a different distribution, a different verdict. The rule's
    justification is therefore the user's own definition, which is per player
    -- not a measurement that would have overturned the previous rule.

    **When the money does not carry to the next round** (see
    :func:`_money_carries_over`), condition B is left uncomputed and the
    result is ``force``. The money left in the pocket evaporates when the half
    changes, so it has not been left *to leave room* -- and the round cannot
    be a half-buy in the sense of S2. This follows from the game's economy
    model and is not a threshold: no new setting is needed, and the rule
    cannot produce a half-buy on a round on which saving is impossible.

    Both counters are in the reason even when one of them has already settled
    the matter: otherwise the reader cannot see which condition rejected the
    round, and by how little.
    """
    armed = row.get(ARMED_COLUMN)
    money_players = row.get(MONEY_DISTRIBUTION_COLUMN)

    missing: list[str] = []
    if armed is None:
        missing.append("the armed counter")
    # An empty list is the same thing as a missing one: it is not an
    # observation that nobody was there, but that nobody could be read. A
    # single empty element empties the whole distribution at once: a null
    # read as zero would claim the player has no money, and a read error
    # would look like a force.
    if not money_players:
        missing.append("the per-player money distribution")
    elif any(money is None for money in money_players):
        missing.append("one player's balance from the money distribution")

    if missing:
        return Decision(
            None,
            f"Round {round_no} is not classified: after a round that was lost "
            f"the team bought {_d(bought_pp)} $/player, but a half-buy is told "
            f"apart from a force and an eco by the per-player observations, "
            f"and those are missing {_names(missing)}. They cannot be deduced "
            f"from the team total, and the class is not guessed. {basis}",
            inputs,
        )

    armed = int(armed)
    players_read = len(money_players)

    # A contradiction inside the row: every per-player number has to come
    # from the **same set**. If the length of the distribution and the
    # observed number of players differ, there would be two different
    # divisors on the same row -- and the counter "3/5" would mean something
    # other than the equipment value per player. The difference is not
    # patched in either direction.
    conflict: str | None = None
    if readable is None or readable != players_read:
        conflict = (
            f"the money distribution holds {players_read} players, but "
            f"players_buy_end says {readable}"
        )
    elif armed > players_read:
        conflict = (
            f"there are {armed} armed players, but only "
            f"{players_read} players could be read"
        )
    if conflict is not None:
        return Decision(
            None,
            f"Round {round_no} is not classified: the per-player observations "
            f"contradict each other -- {conflict}. The counters have to be "
            f"computed from the same set as the totals, and the difference is "
            f"not patched by guessing. {basis}",
            inputs,
        )

    # A short-handed team: three armed players cannot be observed from two
    # players, so the threshold is scaled to the number that could be read.
    # Without this a half-buy would be out of reach whenever fewer players
    # than the threshold can be read, and every purchase would fall through
    # to an eco -- silently and plausibly.
    armed_min = min(thresholds.armed_players_min, players_read)
    buyers_min = min(thresholds.normal_buy_players_min, players_read)
    needed = max(thresholds.armed_players_min, thresholds.normal_buy_players_min)
    scaled = (
        ""
        if players_read >= needed
        else (
            f" The requirements have been scaled to the number of players "
            f"that could be read ({players_read}), because a counter larger "
            "than that cannot be observed."
        )
    )

    carries = _money_carries_over(round_no, thresholds)
    bonus = loss_bonus_if_lost(loss_count, thresholds, economy) if carries else None
    can_buy = (
        players_who_can_buy(money_players, bonus, thresholds, economy)
        if carries
        else None
    )

    armed_part = f"{armed}/{players_read} armed"
    buy_part = (
        f"{can_buy}/{players_read} can buy on the next round"
        if carries
        else "condition B is not computed, because the money does not carry "
        "to the next round"
    )
    counters = f"{armed_part}, {buy_part}"
    bonus_note = (
        (
            f"condition B was computed on the assumption of a loss: own "
            f"balance + loss bonus {bonus} $, at least "
            f"{thresholds.normal_buy_money_min} $, balances "
            f"{_listing_money(money_players)}"
        )
        if carries
        else (
            f"balances {_listing_money(money_players)}, but they are reset "
            f"before round {round_no + 1}"
        )
    )

    if armed < armed_min:
        # Condition A first: if the round is not really played, it is neither
        # a force nor a half-buy however much money may have moved.
        return Decision(
            "eco",
            f"Eco after a round that was lost: bought {_d(bought_pp)} "
            f"$/player, at least {thresholds.force_buy_min} $, but "
            f"{counters} -- fewer than {armed_min} armed, so the round is not "
            f"really played. ({bonus_note}.){scaled} {basis}",
            inputs,
        )

    if not carries:
        return Decision(
            "force",
            f"Force after a round that was lost: bought {_d(bought_pp)} "
            f"$/player, at least {thresholds.force_buy_min} $, "
            f"{armed_part}. The money left in the pocket does not carry to "
            f"round {round_no + 1}, so it has not been left to leave room and "
            f"the round cannot be a half-buy. ({bonus_note}.){scaled} {basis}",
            inputs,
        )

    if can_buy >= buyers_min:
        return Decision(
            "half",
            f"Half-buy after a round that was lost: bought {_d(bought_pp)} "
            f"$/player, at least {thresholds.force_buy_min} $, "
            f"{counters} -- at least {armed_min} armed and at least "
            f"{buyers_min} able to buy, so the team bought but "
            f"left room for the next round. ({bonus_note}.)"
            f"{scaled} {basis}",
            inputs,
        )

    return Decision(
        "force",
        f"Force after a round that was lost: bought {_d(bought_pp)} "
        f"$/player, at least {thresholds.force_buy_min} $, {counters} "
        f"-- fewer than {buyers_min} able to buy, so bought until empty: "
        f"no room was left for the next round. ({bonus_note}.)"
        f"{scaled} {basis}",
        inputs,
    )


def _continuous_previous(
    row: Mapping[str, Any], previous: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """Return the previous round only if it really is the previous one.

    Only ``round_no - 1`` from the same side is accepted. A gap in the round
    numbers or a change of side means that the "previous round" is from
    another half or missing altogether -- and then the rules for after a win
    and after a loss do not hold, and they are not applied by guessing.
    """
    if previous is None:
        return None
    current = row.get("round_no")
    before = previous.get("round_no")
    if current is None or before is None or int(before) != int(current) - 1:
        return None
    if row.get("side") is None or previous.get("side") is None:
        return None
    if str(previous["side"]) != str(row["side"]):
        return None
    return previous


def _players(
    row: Mapping[str, Any], thresholds: ThresholdSettings
) -> tuple[int, int | None, bool]:
    """The divisor for per player values.

    In Pappaliiga playing short-handed is in practice impossible, but the
    sample also holds queue games outside the league, in which it is ordinary.
    That is why the divisor is read off the observation and not assumed to be
    five.

    The observation is accepted only in the range ``1..roster_size``. A value
    outside it -- zero, negative or larger than the roster (stale or extra
    rows in the tick) -- would underestimate or blow up the per player values,
    so ``roster_size`` is used instead and the reason says so.

    Returns:
        ``(divisor, the number that could be read as an observation, whether
        the observation was acceptable)``.
    """
    if thresholds.roster_size < 1:
        raise SchemaError(
            f"thresholds.roster_size is {thresholds.roster_size}; per player "
            "values cannot be computed, because the divisor would be zero or "
            "negative."
        )
    observed = row.get("players_buy_end")
    readable = None if observed is None else int(observed)
    if readable is not None and 1 <= readable <= thresholds.roster_size:
        return readable, readable, True
    return thresholds.roster_size, readable, False


def _inputs(
    row: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    thresholds: ThresholdSettings,
    economy: EconomySettings,
    players: int,
    readable: int | None,
    loss_count: int,
) -> dict[str, Any]:
    """Collect the decision's input values into ``CLASSIFIED_INPUTS``.

    The amount bought is not a field of its own: it is the difference
    ``equip_buy_end - equip_round_start``, and both are here. The money that
    was available is likewise ``money_buy_end + money_spent``. Either can
    therefore be traced without a schema change.

    ``loss_bonus_if_lost`` and ``players_can_buy`` are computed for **every
    round**, not only for the branch that reads them. The round list can then
    be read as one table: the reader can compare the force and half-buy
    counters against the rounds whose class was settled elsewhere too.
    ``players_can_buy`` is ``None`` only if the distribution could not be
    obtained.
    """
    money_players = row.get(MONEY_DISTRIBUTION_COLUMN)
    if money_players is not None:
        money_players = [_i(money) for money in money_players]

    # The loss bonus and the counter of players able to buy are computed only
    # when they mean something:
    #
    #   * In overtime (round_no > regulation_rounds) this module's economy
    #     model does not hold at all -- the starting money is different and
    #     there is no eco cycle (see "Known limitations"). A bonus figure
    #     there would be borrowed from this model and would read like an
    #     observation.
    #   * On the last round of a half the money does not carry to the next
    #     round (see :func:`_money_carries_over`), so the question "is there
    #     enough for the next round" is meaningless.
    #
    # In both the field is left empty. Empty is a claim here: the number does
    # not exist, and it must not be read off the round list as if it did.
    round_no = row.get("round_no")
    applies = round_no is not None and int(round_no) <= thresholds.regulation_rounds
    if applies:
        applies = _money_carries_over(int(round_no), thresholds)

    bonus = loss_bonus_if_lost(loss_count, thresholds, economy) if applies else None
    can_buy = (
        players_who_can_buy(money_players, bonus, thresholds, economy)
        if applies
        and money_players
        and not any(money is None for money in money_players)
        else None
    )
    return {
        "money_buy_end": _i(row.get("money_buy_end")),
        "money_spent": _i(row.get("money_spent")),
        "money_players": money_players,
        "equip_buy_end": _i(row.get("equip_buy_end")),
        "equip_round_start": _i(row.get("equip_round_start")),
        "survivors_prev": None if previous is None else _i(previous.get("survivors")),
        "survivors_equip_prev": _i(row.get("survivors_equip_prev")),
        "prev_round_won": (
            None
            if previous is None or previous.get("won") is None
            else bool(previous["won"])
        ),
        "players": players,
        "players_readable": readable,
        "players_armed": _i(row.get(ARMED_COLUMN)),
        "loss_bonus_if_lost": bonus,
        "players_can_buy": can_buy,
        "full_equip_min": thresholds.full_equip_min,
        "force_buy_min": thresholds.force_buy_min,
        "armed_players_min": thresholds.armed_players_min,
        "normal_buy_money_min": thresholds.normal_buy_money_min,
        "normal_buy_players_min": thresholds.normal_buy_players_min,
        "anomaly_equip_max_after_win": thresholds.anomaly_equip_max_after_win,
    }


def _basis(
    row: Mapping[str, Any],
    thresholds: ThresholdSettings,
    players: int,
    readable: int | None,
    loss_count: int,
    divisor_ok: bool,
) -> str:
    """The shared tail of every reason.

    The I/O matrix requires the reason to state the money and the loss count
    -- also when the decision was settled by the equipment value. Both
    directions of the money are shown, so that the reader does not confuse the
    balance left over with the money that was available.
    """
    equip = row.get("equip_buy_end")
    start = row.get("equip_round_start")
    bought = None if equip is None or start is None else int(equip) - int(start)
    parts = [
        f"Available {_pp(available_money(row), players)}"
        f" (left {_pp(row.get('money_buy_end'), players)}"
        f", spent {_pp(row.get('money_spent'), players)})",
        f"equipment {_pp(equip, players)}",
        f"bought {_pp(bought, players)}",
        f"loss count {loss_count}",
    ]
    if divisor_ok and readable is not None and readable < thresholds.roster_size:
        divisor = (
            f"only {readable} players' values could be read "
            f"(roster {thresholds.roster_size}), and that number is the divisor"
        )
    elif divisor_ok:
        divisor = f"{players} players"
    elif readable is None:
        divisor = (
            "the number of players could not be read, divided by the "
            f"roster_size setting's value {thresholds.roster_size}"
        )
    else:
        divisor = (
            f"the number of players read, {readable}, is outside the allowed "
            f"range 1-{thresholds.roster_size}, divided by the roster_size "
            f"setting's value {thresholds.roster_size}"
        )
    return f"({'; '.join(parts)}; {divisor}.)"


def _pp(value: Any, players: int) -> str:
    number = per_player(value, players)
    return "not known" if number is None else f"{number} $/player"


def _d(value: float) -> str:
    """A dollar amount with no decimals; same rounding as :func:`per_player`."""
    return str(round(value))


def _listing(values: list[int]) -> str:
    return "rounds " + ", ".join(str(a) for a in values)


def _listing_money(values: list[int] | tuple[int, ...]) -> str:
    """The money distribution as it is, so that the counter can be checked.

    "0/5 can buy" alone does not say how close the other five came -- and it
    cannot be checked against the demo without the numbers.

    The unit is repeated on **every** number. A dollar sign at the end alone
    ("1750, 500, 150, 0, 0 $") would read as if it concerned only the last
    one.
    """
    return ", ".join(f"{int(v)} $" for v in values)


def _names(values: list[str]) -> str:
    """The names of the missing observations as a readable list."""
    return ", ".join(values)


def _i(value: Any) -> int | None:
    return None if value is None else int(value)
