"""``domain.sampling`` -- näytteistys ja ensikontakti ilman demoja.

Moduuli on puhdas, joten koko I/O-matriisi on täällä yhden funktiokutsun
päässä: normaali kierros, lyhyt kierros, erittäin lyhyt kierros, ensikontakti,
pelkkä utility, oma vahinko, ankkuriton kierros. Yksikään näistä testeistä ei
lue demoa, ja siksi ne ajetaan myös ``-m "not demo"`` -ajossa.

Tickrate on kaikkialla 64, sama kuin molemmissa testidemoissa, jotta sekunnit
ja tickit ovat luettavia: 6 s = 384 tickiä.
"""

from __future__ import annotations

import pytest

from pappascout.constants import SAMPLE_KINDS
from pappascout.domain.sampling import (
    FIRST_CONTACT_SAMPLE,
    TIME_SAMPLE,
    DamageEvent,
    RoundBounds,
    first_contact_tick,
    normalize_weapon,
    sample_ticks,
    seconds_since_freeze_end,
)

RATE = 64.0
FREEZE = 10_000

#: Sama lista kuin ``settings.toml``in ``[parse]``-osiossa.
UTILITY = (
    "hegrenade",
    "flashbang",
    "smokegrenade",
    "decoy",
    "molotov",
    "incgrenade",
    "inferno",
)

#: Oletusnäytepisteet, ``[parse].snapshot_seconds``.
SECONDS = [6.0, 15.0, 30.0, 45.0]


def bounds(duration_s: float, *, round_raw: int = 1) -> RoundBounds:
    """Kierros, joka ratkeaa ``kesto_s`` sekunnin kuluttua ankkurista."""
    return RoundBounds(
        round_raw=round_raw,
        freeze_end_tick=FREEZE,
        end_tick=FREEZE + round(duration_s * RATE),
    )


def hurt(
    t_s: float,
    *,
    attacker: str = "t1",
    victim: str = "ct1",
    weapon: str = "ak47",
    attacker_side: str = "T",
    victim_side: str = "CT",
) -> DamageEvent:
    """Vahinkotapahtuma sekunteina ankkurista."""
    return DamageEvent(
        tick=FREEZE + round(t_s * RATE),
        attacker_id=attacker,
        victim_id=victim,
        weapon=weapon,
        attacker_side=attacker_side,
        victim_side=victim_side,
    )


def test_sample_kinds_are_exactly_the_schema_enum() -> None:
    """Näytepisteen laji on sama arvo koodissa, Parquetissa ja raportissa.

    Jos moduulin vakiot ja ``SAMPLE_KINDS`` erkanisivat, taulu hylkäytyisi
    vasta kirjoitusvaiheessa -- tai pahempaa, uusi laji jäisi ilman enumia.
    """
    assert set(SAMPLE_KINDS) == {TIME_SAMPLE, FIRST_CONTACT_SAMPLE}


# --- Aikapisteet ---------------------------------------------------------------


def test_a_long_round_gets_every_configured_sample_point() -> None:
    """I/O-matriisi: yli 45 s kestävä kierros saa kaikki neljä aikapistettä."""
    points = sample_ticks([bounds(90)], RATE, SECONDS)
    assert [p.sample_t_s for p in points] == SECONDS
    assert {p.sample_kind for p in points} == {TIME_SAMPLE}
    assert [p.tick for p in points] == [
        FREEZE + 6 * 64,
        FREEZE + 15 * 64,
        FREEZE + 30 * 64,
        FREEZE + 45 * 64,
    ]


def test_seconds_become_ticks_from_the_freeze_end_anchor() -> None:
    point = sample_ticks([bounds(90)], RATE, [15.0])[0]
    assert point.tick == FREEZE + 15 * 64
    assert point.t_s == pytest.approx(15.0)
    assert point.round_raw == 1


def test_a_short_round_loses_the_points_it_never_reached() -> None:
    """I/O-matriisi: 28 sekunnissa ratkennut kierros saa vain 6 ja 15."""
    points = sample_ticks([bounds(28)], RATE, SECONDS)
    assert [p.sample_t_s for p in points] == [6.0, 15.0]


def test_a_very_short_round_gets_no_time_samples_at_all() -> None:
    """I/O-matriisi: 5 sekunnissa ratkennut kierros ei tuota aikapisteitä.

    Nolla pistettä on oikea vastaus eikä virhe -- pisteen keksiminen olisi
    väite pelaajien sijainnista hetkellä, jota ei koskaan pelattu.
    """
    assert sample_ticks([bounds(5)], RATE, SECONDS) == []


def test_a_point_exactly_at_the_end_tick_is_kept() -> None:
    """Kierroksen ratkeamishetki ei ole vielä "sen jälkeen"."""
    points = sample_ticks([bounds(30)], RATE, [30.0])
    assert len(points) == 1
    assert points[0].tick == bounds(30).end_tick


def test_a_point_one_tick_past_the_end_is_dropped() -> None:
    round_bounds = RoundBounds(
        round_raw=1, freeze_end_tick=FREEZE, end_tick=FREEZE + 30 * 64 - 1
    )
    assert sample_ticks([round_bounds], RATE, [30.0]) == []


def test_t_s_is_computed_from_the_chosen_tick_not_the_nominal_second() -> None:
    """Pyöristys näkyy ``t_s``:ssä; nimellisaika säilyy ``sample_t_s``:ssä.

    Ilman tätä eroa taulu väittäisi hetkeä, jota ei luettu.
    """
    point = sample_ticks([bounds(90)], 63.7, [6.0])[0]
    expected_tick = FREEZE + round(6.0 * 63.7)  # 382
    assert point.tick == expected_tick
    assert point.sample_t_s == 6.0
    assert point.t_s == pytest.approx((expected_tick - FREEZE) / 63.7)
    assert point.t_s != 6.0


def test_a_round_without_a_freeze_anchor_is_not_sampled() -> None:
    """I/O-matriisi: ``status = "no_freeze_end"`` -> ei tick-rivejä.

    Ilman ankkuria ``t_s`` ei ole määritelty, eikä kierrosta voi verrata
    muihin.
    """
    round_bounds = RoundBounds(round_raw=3, freeze_end_tick=None, end_tick=FREEZE + 10_000)
    assert sample_ticks([round_bounds], RATE, SECONDS) == []


def test_a_round_that_never_ended_is_not_sampled() -> None:
    """Ilman päättymistickiä pisteitä ei voi rajata kierrokseen."""
    round_bounds = RoundBounds(round_raw=3, freeze_end_tick=FREEZE, end_tick=None)
    assert sample_ticks([round_bounds], RATE, SECONDS) == []


def test_a_round_whose_bounds_are_reversed_is_not_sampled() -> None:
    round_bounds = RoundBounds(round_raw=3, freeze_end_tick=FREEZE, end_tick=FREEZE - 100)
    assert sample_ticks([round_bounds], RATE, SECONDS) == []


def test_points_are_ordered_by_round_and_time_whatever_the_input_order() -> None:
    points = sample_ticks(
        [bounds(90, round_raw=2), bounds(90, round_raw=1)], RATE, [30.0, 6.0]
    )
    assert [(p.round_raw, p.sample_t_s) for p in points] == [
        (2, 6.0),
        (2, 30.0),
        (1, 6.0),
        (1, 30.0),
    ]


def test_a_duplicated_sample_second_produces_one_point() -> None:
    """Asetustiedoston kirjoitusvirhe ei saa kahdentaa otantaa."""
    points = sample_ticks([bounds(90)], RATE, [6.0, 6.0, 15.0])
    assert [p.sample_t_s for p in points] == [6.0, 15.0]


def test_zero_seconds_is_the_anchor_itself() -> None:
    point = sample_ticks([bounds(90)], RATE, [0.0])[0]
    assert point.tick == FREEZE
    assert point.t_s == 0.0


@pytest.mark.parametrize("rate", [0.0, -64.0])
def test_an_impossible_tick_rate_is_refused(rate: float) -> None:
    with pytest.raises(ValueError, match="[Tt]ickrate"):
        sample_ticks([bounds(90)], rate, SECONDS)


def test_a_negative_sample_second_is_refused() -> None:
    """Negatiivinen piste osoittaisi ostoaikaan, jossa kukaan ei ole liikkunut."""
    with pytest.raises(ValueError, match="negatiivinen"):
        sample_ticks([bounds(90)], RATE, [-1.0, 6.0])


def test_seconds_since_freeze_end_matches_the_rounds_table_formula() -> None:
    assert seconds_since_freeze_end(FREEZE + 128, FREEZE, 64.0) == 2.0
    assert seconds_since_freeze_end(FREEZE, FREEZE, 64.0) == 0.0


# --- Ensikontakti --------------------------------------------------------------


def test_the_first_cross_side_hit_is_the_contact() -> None:
    """I/O-matriisi: ensimmäinen ristiinpuolinen osuma ei-utilityaseella."""
    events = [hurt(30.0), hurt(12.0), hurt(20.0)]
    tick = first_contact_tick(events, bounds(90), exclude_weapons=UTILITY)
    assert tick == FREEZE + round(12.0 * RATE)


def test_a_round_without_any_damage_has_no_contact() -> None:
    """I/O-matriisi: aika loppui, kukaan ei ampunut."""
    assert first_contact_tick([], bounds(115), exclude_weapons=UTILITY) is None


def test_utility_only_damage_is_not_a_contact() -> None:
    """I/O-matriisi: ainoa vahinko molotovista -> ei ensikontaktia.

    Kierros on silti mukana, se vain jää ilman ``first_contact``-rivejä.
    """
    events = [hurt(20.0, weapon="molotov"), hurt(25.0, weapon="inferno")]
    assert first_contact_tick(events, bounds(90), exclude_weapons=UTILITY) is None


def test_utility_damage_does_not_hide_a_later_real_contact() -> None:
    events = [hurt(10.0, weapon="hegrenade"), hurt(22.0, weapon="awp")]
    tick = first_contact_tick(events, bounds(90), exclude_weapons=UTILITY)
    assert tick == FREEZE + round(22.0 * RATE)


def test_friendly_fire_is_not_a_contact() -> None:
    """I/O-matriisi: tekijä samalla puolella."""
    same_side = hurt(10.0, attacker="t1", victim="t2", victim_side="T")
    opponent = hurt(40.0)
    tick = first_contact_tick([same_side, opponent], bounds(90), exclude_weapons=UTILITY)
    assert tick == FREEZE + round(40.0 * RATE)


def test_self_damage_is_not_a_contact() -> None:
    """I/O-matriisi: tekijä = uhri."""
    self_damage = hurt(10.0, attacker="t1", victim="t1", victim_side="T")
    assert first_contact_tick([self_damage], bounds(90), exclude_weapons=UTILITY) is None


def test_world_damage_without_an_attacker_is_not_a_contact() -> None:
    """Putoamisvahingolla ei ole tekijää, joten se ei kerro kohtaamisesta."""
    fall_damage = DamageEvent(
        tick=FREEZE + 640,
        attacker_id=None,
        victim_id="ct1",
        weapon=None,
        attacker_side=None,
        victim_side="CT",
    )
    assert first_contact_tick([fall_damage], bounds(90), exclude_weapons=UTILITY) is None


def test_a_player_whose_side_is_unknown_is_not_a_contact() -> None:
    """Puolen arvaaminen kohdistaisi kontaktin väärälle joukkueelle."""
    unknown_side = hurt(10.0, attacker_side=None)
    assert first_contact_tick([unknown_side], bounds(90), exclude_weapons=UTILITY) is None


def test_damage_outside_the_round_belongs_to_another_round() -> None:
    """Kierroksen jälkeinen osuma kuuluu seuraavaan kierrokseen, ei tähän."""
    round_bounds = bounds(30)
    after_end = hurt(45.0)
    before_start = DamageEvent(
        tick=FREEZE - 500,
        attacker_id="t1",
        victim_id="ct1",
        weapon="ak47",
        attacker_side="T",
        victim_side="CT",
    )
    assert (
        first_contact_tick([after_end, before_start], round_bounds, exclude_weapons=UTILITY) is None
    )


def test_death_is_the_fallback_when_no_hurt_event_qualifies() -> None:
    death = hurt(18.0)
    tick = first_contact_tick(
        [],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[death],
        fallback_death=True,
    )
    assert tick == FREEZE + round(18.0 * RATE)


def test_the_hurt_event_wins_over_the_death_fallback() -> None:
    """Varalähdettä käytetään vain, jos ensisijaista ei ole -- ei rinnalla."""
    tick = first_contact_tick(
        [hurt(30.0)],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[hurt(10.0)],
        fallback_death=True,
    )
    assert tick == FREEZE + round(30.0 * RATE)


def test_the_death_fallback_can_be_switched_off() -> None:
    tick = first_contact_tick(
        [],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[hurt(18.0)],
        fallback_death=False,
    )
    assert tick is None


def test_a_death_by_utility_is_not_a_contact_either() -> None:
    tick = first_contact_tick(
        [],
        bounds(90),
        exclude_weapons=UTILITY,
        death_events=[hurt(18.0, weapon="inferno")],
    )
    assert tick is None


def test_a_round_without_an_anchor_has_no_contact() -> None:
    """Ilman ankkuria kontaktin hetkeä ei voisi ilmaista ``t_s``:nä."""
    round_bounds = RoundBounds(round_raw=1, freeze_end_tick=None, end_tick=FREEZE + 5000)
    assert first_contact_tick([hurt(10.0)], round_bounds, exclude_weapons=UTILITY) is None


def test_an_empty_exclude_list_lets_utility_count() -> None:
    """Sääntö on asetus eikä koodi: tyhjällä listalla molotovkin kelpaa."""
    tick = first_contact_tick([hurt(10.0, weapon="molotov")], bounds(90))
    assert tick == FREEZE + round(10.0 * RATE)


# --- Aseen nimen normalisointi -------------------------------------------------


@pytest.mark.parametrize(
    "raw_name,expected",
    [
        ("hegrenade", "hegrenade"),
        ("weapon_hegrenade", "hegrenade"),
        ("HEGrenade", "hegrenade"),
        ("  molotov  ", "molotov"),
        ("", None),
        (None, None),
    ],
)
def test_weapon_names_are_compared_normalised(raw_name, expected) -> None:
    assert normalize_weapon(raw_name) == expected


def test_the_exclude_list_is_normalised_too() -> None:
    """Asetustiedostossa voi lukea ``weapon_molotov`` -- sekin on utilityä."""
    tick = first_contact_tick(
        [hurt(10.0, weapon="molotov")],
        bounds(90),
        exclude_weapons=("weapon_Molotov",),
    )
    assert tick is None
