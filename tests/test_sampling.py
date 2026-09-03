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

from conftest import OVERLAPPING_SITE_CLOUD, SITE_CLOUD
from pappascout.constants import SAMPLE_KINDS, SAVING_ROUND_TYPES
from pappascout.domain.sampling import (
    CRUNCH,
    CT_ADVANCE,
    FIRST_CONTACT_SAMPLE,
    STACK,
    TIME_SAMPLE,
    AnomalyHit,
    AreaObservations,
    AreaPresence,
    CloudCell,
    DamageEvent,
    RoundBounds,
    crunch_hits,
    ct_advance_hits,
    first_contact_tick,
    normalize_weapon,
    sample_ticks,
    seconds_since_freeze_end,
    site_groups,
    stack_hits,
    t_side_shares,
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


# --- Poikkeamasäännöt (Story 2.5) -----------------------------------------------
#
# Jokainen spec-2-5:n I/O-matriisin rivi on täällä, ja kaikki taulut ovat
# käsin rakennettuja: yksikään näistä testeistä ei lue demoa. Luvut ovat
# kalibroinnista (``kalibrointi-ct-eteneminen.md``), jotta rivi kertoo mitä se
# mittaa: 0,88 on Ancientin ``TSideLower``, 0,70 Nuken piha.

#: Kynnykset kuten ``settings.toml``issa. Testit lukevat ne täältä eivätkä
#: asetustiedostosta -- sääntö on funktio, ja sen parametrit ovat argumentteja.
T_SHARE = 0.80
MIN_OBSERVATIONS = 20
MAX_SAMPLE_S = 30.0

#: Alue, joka on demossa T:n hallussa: Ancientin B suora, T-osuus 0,88 (n 24).
T_AREA = "TSideLower"

#: Alue, joka jää kynnyksen alle: Nuken piha, T-osuus tasan 0,70 (n 302).
SHARED_AREA = "Outside"


def observed(t_share: float, observations: int) -> AreaObservations:
    """Alueen havainnot annetulla T-osuudella."""
    return AreaObservations(t=round(t_share * observations), total=observations)


def orientation(
    **areas: AreaObservations,
) -> dict[str | None, AreaObservations]:
    """Alue -> havainnot; oletuskartassa vain T:n alue."""
    return dict(areas) or {T_AREA: observed(0.88, 24)}


def at(
    seconds: float,
    area: str | None,
    *players: str,
    side: str = "CT",
    kind: str = TIME_SAMPLE,
    alive: bool = True,
) -> list[AreaPresence]:
    """Näytepisterivit: annetut pelaajat annetulla alueella annetulla hetkellä."""
    return [
        AreaPresence(
            player_id=player,
            side=side,
            sample_kind=kind,
            sample_t_s=seconds,
            area=area,
            is_alive=alive,
        )
        for player in players
    ]


def advance(
    rows: list[AreaPresence],
    *,
    round_type: str | None = "eco",
    areas: dict[str | None, AreaObservations] | None = None,
    min_players: int = 1,
):
    return ct_advance_hits(
        rows,
        round_type=round_type,
        orientation=areas if areas is not None else orientation(),
        t_share_min=T_SHARE,
        area_min_observations=MIN_OBSERVATIONS,
        max_sample_s=MAX_SAMPLE_S,
        min_players=min_players,
    )


def crunch(
    rows: list[AreaPresence],
    *,
    areas: dict[str | None, AreaObservations] | None = None,
    min_players: int = 2,
    min_sources: int = 2,
):
    return crunch_hits(
        rows,
        orientation=areas if areas is not None else orientation(),
        t_share_min=T_SHARE,
        area_min_observations=MIN_OBSERVATIONS,
        max_sample_s=MAX_SAMPLE_S,
        min_players=min_players,
        min_sources=min_sources,
    )



def test_an_area_is_t_side_when_it_passes_both_thresholds() -> None:
    """Osuus JA havaintomäärä -- kumpikin yksin ei riitä."""
    areas = {
        T_AREA: observed(0.88, 24),
        SHARED_AREA: observed(0.70, 302),
        "Thin": observed(1.00, 8),
    }
    passed = t_side_shares(
        areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
    )
    assert set(passed) == {T_AREA}


def test_an_unnamed_area_is_neither_sides_area() -> None:
    """``None``-alueella ei ole orientaatiota, vaikka osuus riittäisi."""
    areas = {None: observed(1.00, 100)}
    assert (
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )
        == {}
    )


@pytest.mark.parametrize("share", [-0.1, 1.1])
def test_an_impossible_t_share_threshold_is_refused(share: float) -> None:
    with pytest.raises(ValueError, match="ei ole välillä 0..1"):
        t_side_shares(orientation(), t_share_min=share, min_observations=20)


def test_a_non_positive_observation_threshold_is_refused() -> None:
    with pytest.raises(ValueError, match="ei ole positiivinen"):
        t_side_shares(orientation(), t_share_min=T_SHARE, min_observations=0)


@pytest.mark.parametrize(
    "t,total",
    [(0, 0), (5, 4), (-1, 10)],
)
def test_impossible_observation_counts_are_refused(t: int, total: int) -> None:
    """Nolla havaintoa ei ole alue, eikä osajoukko voi olla joukkoa suurempi."""
    with pytest.raises(ValueError):
        AreaObservations(t=t, total=total)


# --- I/O-matriisi: CT-eteneminen ------------------------------------------------


def test_a_ct_player_on_a_t_side_area_on_an_eco_round_is_an_advance() -> None:
    """Matriisin rivi 1: T-osuus 0,88, eco, 30 s."""
    hits = advance(at(30.0, T_AREA, "ct1"))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == CT_ADVANCE
    assert hit.area == T_AREA
    assert hit.sample_t_s == 30.0
    assert hit.players == 1
    assert hit.t_share == pytest.approx(0.88, abs=0.01)
    assert hit.observations == 24
    assert hit.sources == ()


def test_an_area_below_the_share_threshold_is_no_anomaly() -> None:
    """Matriisin rivi 4: Nuken piha (0,70) on aidosti molempien aluetta."""
    areas = {SHARED_AREA: observed(0.70, 302)}
    assert advance(at(30.0, SHARED_AREA, "ct1"), areas=areas) == []


def test_an_area_with_too_few_observations_is_no_anomaly() -> None:
    """Matriisin rivi 5: 8 havaintoa -- alue ei ole kummankaan."""
    areas = {"Thin": observed(1.00, 8)}
    assert advance(at(30.0, "Thin", "ct1"), areas=areas) == []


def test_a_late_sample_point_is_outside_the_rule() -> None:
    """Matriisin rivi 6: osuma vain 45 s kohdalla ei ole poikkeama."""
    assert advance(at(45.0, T_AREA, "ct1")) == []


def test_an_early_sample_point_is_inside_the_rule() -> None:
    """Raja on ``<= 30 s``, ja 15 s on kalibroinnin osumien enemmistö."""
    assert len(advance(at(15.0, T_AREA, "ct1"))) == 1


def test_the_sample_point_exactly_at_the_bound_is_kept() -> None:
    """30 s on rajalla, ja kalibroinnin vahvin osuma on siellä."""
    assert len(advance(at(30.0, T_AREA, "ct1"))) == 1


@pytest.mark.parametrize("round_type", ["pistol", "full", "ot", "anomaly", None])
def test_advance_cannot_hit_outside_a_saving_round(round_type) -> None:
    """Matriisin rivi 8: rajaus on taloudellinen havainto eikä otanta."""
    assert advance(at(30.0, T_AREA, "ct1"), round_type=round_type) == []


@pytest.mark.parametrize("round_type", SAVING_ROUND_TYPES)
def test_advance_hits_on_every_saving_round_type(round_type: str) -> None:
    """Eco, force ja puoliosto ovat sama havainto: ostokyky ei riitä."""
    assert len(advance(at(30.0, T_AREA, "ct1"), round_type=round_type)) == 1


def test_only_ct_rows_are_examined() -> None:
    """Matriisin rivi 9: sama pelaaja T:nä ja CT:nä -- vain CT-rivit."""
    rows = at(30.0, T_AREA, "p1", side="T") + at(30.0, T_AREA, "p2", side="CT")
    hits = advance(rows)
    assert len(hits) == 1
    assert hits[0].players == 1


def test_a_dead_player_is_not_on_the_area() -> None:
    """Sama sääntö kuin pelaajamäärissä: kuollut ei tuota riviä alueelle."""
    assert advance(at(30.0, T_AREA, "ct1", alive=False)) == []


def test_a_first_contact_row_never_triggers_the_rule() -> None:
    """Ensikontaktin ``sample_t_s`` on mitattu hetki eikä näytepiste."""
    rows = at(12.0, T_AREA, "ct1", kind="first_contact")
    assert advance(rows) == []


def test_the_same_player_twice_is_one_player() -> None:
    """Pelaajamäärä on eri pelaajia eikä rivejä."""
    hits = advance(at(30.0, T_AREA, "ct1") + at(30.0, T_AREA, "ct1"))
    assert hits[0].players == 1


def test_the_player_minimum_is_a_threshold_not_a_constant() -> None:
    """Veeti valitsi 1, mutta 2 on säädettävissä ilman koodimuutosta."""
    rows = at(30.0, T_AREA, "ct1")
    assert advance(rows, min_players=1)
    assert advance(rows, min_players=2) == []


def test_two_sample_points_on_the_same_area_are_two_hits() -> None:
    """Osuma on näytepisteen havainto; kierrokseksi ne niputtaa aggregointi."""
    hits = advance(at(15.0, T_AREA, "ct1") + at(30.0, T_AREA, "ct1"))
    assert [hit.sample_t_s for hit in hits] == [15.0, 30.0]


def test_an_area_that_is_not_t_side_is_silent_even_with_five_players() -> None:
    """Pelaajamäärä ei korvaa orientaatiota."""
    areas = {T_AREA: observed(0.88, 24)}
    rows = at(30.0, "CTSpawn", "ct1", "ct2", "ct3", "ct4", "ct5")
    assert advance(rows, areas=areas) == []


def test_no_anomalies_is_a_valid_result() -> None:
    """Matriisin rivi 7: tyhjä lista on havainto eikä virhe."""
    assert advance([]) == []
    assert crunch([]) == []


# --- I/O-matriisi: crunch -------------------------------------------------------


def test_two_players_arriving_from_two_areas_is_a_crunch() -> None:
    """Matriisin rivi 2: 2 CT-pelaajaa, 2 eri lähtöaluetta."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    hits = crunch(rows)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == CRUNCH
    assert hit.area == T_AREA
    assert hit.sample_t_s == 30.0
    assert hit.players == 2
    assert hit.sources == ("SideEntrance", "TSideUpper")


def test_two_players_from_the_same_area_is_not_a_crunch() -> None:
    """Yksi suunta ei ole kaksi suuntaa, vaikka pelaajia olisi kaksi."""
    rows = at(15.0, "SideEntrance", "ct1", "ct2") + at(30.0, T_AREA, "ct1", "ct2")
    assert crunch(rows) == []


def test_a_player_already_on_the_area_did_not_arrive() -> None:
    """Lähtöalue on eri kuin kohde -- muuten pelaaja vain seisoi paikallaan."""
    rows = (
        at(15.0, T_AREA, "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows) == []


def test_the_first_sample_point_of_a_round_has_no_source() -> None:
    """Ilman edellistä näytepistettä suuntaa ei arvata."""
    rows = at(6.0, T_AREA, "ct1", "ct2")
    assert crunch(rows) == []


def test_an_unknown_previous_area_is_not_a_direction() -> None:
    """``None`` ei ole suunta, joten se ei kelpaa lähtöalueeksi."""
    rows = (
        at(15.0, None, "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows) == []


def test_crunch_counts_arrivals_not_everyone_on_the_area() -> None:
    """Kolme alueella, kaksi saapunutta: crunchin luku on kaksi."""
    rows = (
        at(15.0, T_AREA, "ct3")
        + at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2", "ct3")
    )
    hits = crunch(rows)
    assert len(hits) == 1
    assert hits[0].players == 2
    assert len(advance(rows)) == 2  # 15 s ja 30 s: eteneminen laskee kaikki
    assert max(hit.players for hit in advance(rows)) == 3


def test_crunch_is_not_limited_to_saving_rounds() -> None:
    """Mitattu: yksi viidestä crunchista on täysi osto, joten rajausta ei ole.

    Sääntö ei ota kierrostyyppiä argumenttinaan lainkaan -- rajaus, jota ei
    ole, ei voi vahingossa palata. Tämä on myös se, miksi crunchia ei saa
    kuvata etenemisen "tiukempana muotona": täydellä ostolla etenemisriviä
    ei ole olemassa, joten osumajoukot leikkaavat toisiaan eikä kumpikaan
    sisällä toista.
    """
    rows = (
        at(15.0, "Arch", "ct1")
        + at(15.0, "TopofMid", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert len(crunch(rows)) == 1
    assert "round_type" not in crunch_hits.__annotations__


def test_a_crunch_round_also_hits_the_advance_rule() -> None:
    """Matriisin rivi 3: molemmat kerrotaan, crunch ei korvaa etenemistä."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert len(crunch(rows)) == 1
    advances = [hit for hit in advance(rows) if hit.sample_t_s == 30.0]
    assert len(advances) == 1
    assert advances[0].players == 2


def test_crunch_needs_the_area_to_be_t_side_too() -> None:
    """Sama orientaatioehto kuin etenemisessä; suunta ei korvaa sitä."""
    areas = {T_AREA: observed(0.88, 24)}
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, "CTSpawn", "ct1", "ct2")
    )
    assert crunch(rows, areas=areas) == []


def test_a_late_crunch_is_outside_the_rule() -> None:
    """Aikaraja on jaettu: MatureMayhemin 45 s crunch putoaa samasta syystä."""
    rows = (
        at(30.0, "BombsiteA", "ct1")
        + at(30.0, "MainHall", "ct2")
        + at(45.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows) == []


def test_a_source_after_the_time_bound_still_counts_as_a_source() -> None:
    """Lähtöalue luetaan edelliseltä näytepisteeltä, ei aikarajan sisältä.

    Ilman tätä 30 s crunch menettäisi lähtöalueensa, jos edellinen näytepiste
    olisi aikarajan ulkopuolella -- ja saapuminen jäisi näkymättä.
    """
    rows = (
        at(6.0, "SideEntrance", "ct1")
        + at(6.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
        + at(45.0, T_AREA, "ct1", "ct2")
    )
    hits = crunch(rows)
    assert [hit.sample_t_s for hit in hits] == [30.0]


def test_the_source_minimum_is_a_threshold() -> None:
    """Kolme suuntaa on säädettävissä ilman koodimuutosta."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows, min_sources=2)
    assert crunch(rows, min_sources=3) == []


def test_a_duplicated_row_at_the_target_area_does_not_eat_the_source() -> None:
    """Kaksoisrivi pariutui aiemmin itsensä kanssa ja vaiensi crunchin.

    Toistettu löydös: yksi ylimääräinen ``p1``-rivi 30 s kohdalla teki
    lähtöalueesta kohdealueen, jonka jälkeen ``source == area`` ohitti
    saapumisen. Kaksoisrivi ei ole teoreettinen -- ``_players_by_point``
    vartioi sitä jo joukolla, ja sama vartija tarvitaan lähtöalueille.
    """
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
        + at(30.0, T_AREA, "ct1")  # kaksoisrivi
    )
    hits = crunch(rows)
    assert len(hits) == 1
    assert hits[0].players == 2
    assert hits[0].sources == ("SideEntrance", "TSideUpper")


def test_a_duplicated_row_does_not_inflate_the_player_count() -> None:
    """Sama vartija toiseen suuntaan: pelaajamäärä on eri pelaajia."""
    rows = (
        at(15.0, "SideEntrance", "ct1")
        + at(15.0, "TSideUpper", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
        + at(30.0, T_AREA, "ct1", "ct2")
    )
    assert crunch(rows)[0].players == 2


@pytest.mark.parametrize("written", [f" {T_AREA} ", f"{T_AREA}\t", f"\n{T_AREA}"])
def test_stray_whitespace_in_an_area_name_still_matches(written: str) -> None:
    """Orientaatio ja läsnäolo normalisoidaan samalla funktiolla."""
    hits = advance(at(30.0, written, "ct1"))
    assert [hit.area for hit in hits] == [T_AREA]


def test_an_empty_area_name_is_not_an_area() -> None:
    """``""`` ei ole alue: se selviäisi muuten T-alueiden joukkoon."""
    areas = {"": observed(1.00, 100), T_AREA: observed(0.88, 24)}
    assert set(t_side_shares(
        areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
    )) == {T_AREA}
    assert advance(at(30.0, "", "ct1"), areas=areas) == []


def test_the_same_area_in_two_spellings_is_refused() -> None:
    """Kahdesta kirjoitusasusta ei voi valita -- kutsuja normalisoi."""
    areas = {T_AREA: observed(0.88, 24), f" {T_AREA}": observed(0.20, 30)}
    with pytest.raises(ValueError, match="kahdesti eri"):
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )


def test_an_area_exactly_at_the_observation_bound_is_included() -> None:
    """``>=`` eikä ``>``: tasan 20 havainnon alue on mukana.

    Tarkkuus on kantavaa, koska kalibrointi nojaa tasarajoihin. Sanamuoto
    "ei ylitä rajaa" tarkoittaisi päinvastaista, ja juuri se ristiriita
    korjattiin katselmuksessa.
    """
    areas = {T_AREA: observed(0.90, 20)}
    assert set(
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )
    ) == {T_AREA}
    assert len(advance(at(30.0, T_AREA, "ct1"), areas=areas)) == 1


def test_an_area_one_observation_below_the_bound_is_excluded() -> None:
    """Vartijan toinen suunta: 19 havaintoa alittaa rajan."""
    areas = {T_AREA: observed(0.90, 19)}
    assert (
        t_side_shares(
            areas, t_share_min=T_SHARE, min_observations=MIN_OBSERVATIONS
        )
        == {}
    )


def test_an_area_exactly_at_the_share_bound_is_included() -> None:
    """Sama sääntö osuudelle: tasan 0,80 kelpaa."""
    areas = {T_AREA: observed(0.80, 100)}
    assert len(advance(at(30.0, T_AREA, "ct1"), areas=areas)) == 1


def test_the_infernos_five_player_crunch_is_measured_as_five() -> None:
    """Kalibroinnin vahvin osuma: 5 CT-pelaajaa midissä kahdesta suunnasta."""
    areas = {"Middle": observed(0.83, 60)}
    rows = (
        at(6.0, "Arch", "ct1", "ct2", "ct3")
        + at(6.0, "TopofMid", "ct4", "ct5")
        + at(15.0, "Middle", "ct1", "ct2", "ct3", "ct4", "ct5")
    )
    hits = crunch(rows, areas=areas)
    assert len(hits) == 1
    assert hits[0].players == 5
    assert hits[0].sources == ("Arch", "TopofMid")


# --- Stack (Story 2.14) ---------------------------------------------------------
#
# Sääntö ja sen johdettu syöte ovat samassa moduulissa, joten I/O-matriisin
# jokainen rivi on täällä yhden funktiokutsun päässä -- ilman demoa, ilman
# arkistoa. Kalibroinnin oikeat luvut ovat test_calibration.py:ssä.

#: Pilvet ovat ``conftest``issa, koska sama geometria on kolmen tiedoston
#: yhteinen: sääntö täällä, aggregointi ``test_aggregate``ssa ja vaihe
#: ``test_stage_aggregate``ssa. Kolmesta kopiosta ne voisivat ajautua
#: erilleen, eikä yksikään testi kertoisi siitä.
CLOUD = SITE_CLOUD
CLOUD_OVERLAPPING = OVERLAPPING_SITE_CLOUD

MARGIN = 1.25
SEPARATION_MIN = 2.0
STACK_MIN_PLAYERS = 4


def cloud(cells=CLOUD) -> list[CloudCell]:
    return [CloudCell(area, x, y, z) for area, x, y, z in cells]


def groups(cells=CLOUD) -> dict[str, str] | None:
    return site_groups(
        cloud(cells), margin=MARGIN, separation_min=SEPARATION_MIN
    )


def stack(
    rows: list[AreaPresence],
    *,
    cells=CLOUD,
    min_players: int = STACK_MIN_PLAYERS,
):
    return stack_hits(
        rows,
        groups=groups(cells),
        max_sample_s=MAX_SAMPLE_S,
        min_players=min_players,
    )


def test_the_site_groups_are_read_off_the_demos_own_point_cloud() -> None:
    """Jokainen alue saa ryhmän lähemmästä sitestä -- tai ei kumpaakaan.

    ``Middle`` on yhtä kaukana molemmista, joten se **puuttuu kuvauksesta**:
    ryhmätön alue ei ole ``None``-arvo vaan puuttuva avain, jotta
    ``groups.get(area)`` on yksiselitteinen.
    """
    found = groups()
    assert found == {
        "BombsiteA": "A",
        "BombsiteB": "B",
        "House": "A",
        "CTSpawn": "A",
        "SideEntrance": "B",
        "Ramp": "B",
        "TSpawn": "B",
    }
    assert "Middle" not in found


def test_four_defenders_in_one_sites_group_are_a_stack() -> None:
    """I/O-matriisin ensimmäinen rivi: 4 CT B-ryhmässä, yksi sitellä.

    Osuma kertoo neljä asiaa: alueen (siten oman), pelaajamäärän, elossa
    olevat ja näytepisteen. Alue on ``BombsiteB`` eikä ``SideEntrance``,
    vaikka jälkimmäisellä on enemmän pelaajia: rivi nimeää **ryhmän
    ankkurin**, ei sitä aluetta, jolla sattui olemaan tungosta.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1")
        + at(15.0, "SideEntrance", "ct2", "ct3")
        + at(15.0, "Ramp", "ct4")
        + at(15.0, "House", "ct5")
    )
    hits = stack(rows)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.rule == STACK
    assert hit.area == "BombsiteB"
    assert hit.site == "B"
    assert hit.players == 4
    assert hit.alive == 5
    assert hit.sample_t_s == 15.0
    # Orientaatio ei koske stackia, ja tyhjä kenttä on se, joka pitää sen
    # erossa kahdesta muusta säännöstä.
    assert hit.t_share is None
    assert hit.observations is None
    assert hit.sources == ()


def test_half_the_map_is_not_a_site() -> None:
    """I/O-matriisin toinen rivi: ryhmässä neljä, mutta kukaan ei sitellä.

    "Stack sitellä" tarkoittaa että ollaan sitellä, ei että ollaan kartan
    siinä puoliskossa. Mitattu: ehto pudottaa 17 kierrosta -> 9.
    """
    rows = at(15.0, "SideEntrance", "ct1", "ct2") + at(
        15.0, "Ramp", "ct3", "ct4"
    )
    assert stack(rows) == []


def test_a_player_in_spawn_does_not_defend_a_site() -> None:
    """I/O-matriisin kolmas rivi: kaksi spawnissa, kaksi jäljellä.

    ``CTSpawn`` on tässä pilvessä A-ryhmässä, kuten Ancientilla. Ilman
    spawnrajausta pelkkä aloitusasetelma laukaisisi säännön.
    """
    rows = at(15.0, "BombsiteA", "ct1", "ct2") + at(
        15.0, "CTSpawn", "ct3", "ct4"
    )
    assert stack(rows) == []
    # ``TSpawn`` on B-ryhmässä, ja sama rajaus koskee sitä: CT-pelaaja T:n
    # spawnissa on jo eri havainto (ct_advance), ei stack.
    b_side = at(15.0, "BombsiteB", "ct1", "ct2") + at(
        15.0, "TSpawn", "ct3", "ct4"
    )
    assert stack(b_side) == []
    # Vartijan toinen suunta: samat neljä pelaajaa ryhmän alueilla osuvat.
    ok = at(15.0, "BombsiteA", "ct1", "ct2") + at(15.0, "House", "ct3", "ct4")
    assert len(stack(ok)) == 1


def test_a_map_whose_sites_do_not_separate_stays_silent() -> None:
    """I/O-matriisin neljäs rivi: Nuken siteet ovat päällekkäin.

    Vartija on **suhdeluku eikä karttalista**: mitään karttaa ei nimetä
    koodissa, vaan siteiden etäisyys jaetaan siteiden omalla koolla.
    Vaikeneminen on oikea vastaus -- ja se on kutsujan kirjattava
    kattavuuteen.
    """
    assert groups(CLOUD_OVERLAPPING) is None
    rows = at(15.0, "BombsiteA", "ct1", "ct2", "ct3", "ct4")
    assert stack(rows, cells=CLOUD_OVERLAPPING) == []


def test_the_dead_are_not_the_denominator() -> None:
    """I/O-matriisin viides rivi: 4 elossa, kaikki ryhmässä -> 4/4.

    Neljä viidestä on puolustuksen valinta, neljä neljästä on se mitä
    jäljellä oli. Pelkkä pelaajamäärä ei erota niitä.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1", "ct2")
        + at(15.0, "SideEntrance", "ct3", "ct4")
        + at(15.0, "House", "ct5", alive=False)
    )
    hits = stack(rows)
    assert len(hits) == 1
    assert (hits[0].players, hits[0].alive) == (4, 4)


def test_a_cloud_without_a_site_gives_no_groups() -> None:
    """I/O-matriisin kuudes rivi: tyhjä pilvi tai puuttuva site.

    Havainnon puuttuminen ei ole havainto jaon puuttumisesta, joten kumpikin
    on ``None``.
    """
    assert groups(()) is None
    assert groups((("BombsiteB", 100, 0, 0), ("House", 10, 0, 0))) is None


def test_sites_without_a_size_of_their_own_are_silenced() -> None:
    """Vartijan aukko: yhden ruudun siteillä suhde jakaisi nollalla.

    Kahden ruudun pilvi ei kerro kartan siterakenteesta mitään, joten *mikä
    tahansa* erotus läpäisisi kynnyksen -- eli vartija ei mittaisi enää
    yhtään mitään. Demo, jonka pilvi on noin ohut, on rikki eikä
    tasoerottuva.
    """
    assert groups(
        (("BombsiteA", 0, 0, 0), ("BombsiteB", 100, 0, 0))
    ) is None


def test_two_sites_at_the_same_point_are_silenced() -> None:
    """Kumpikaan ei olisi aidosti lähempänä yhtäkään aluetta."""
    same = (
        ("BombsiteA", 0, 0, 0),
        ("BombsiteA", 2, 0, 0),
        ("BombsiteA", -2, 0, 0),
        ("BombsiteB", 0, 0, 0),
        ("BombsiteB", 2, 0, 0),
        ("BombsiteB", -2, 0, 0),
    )
    assert groups(same) is None


def test_a_late_sample_point_is_outside_the_shared_time_bound() -> None:
    """I/O-matriisin seitsemäs rivi: osuma vain 45 s kohdalla ei ole osuma.

    Aikaraja on **yhteinen** kahden muun säännön kanssa
    (``advance_max_sample_s``), ei stackin oma kynnys.
    """
    rows = at(45.0, "BombsiteB", "ct1", "ct2", "ct3", "ct4")
    assert stack(rows) == []


def test_the_player_threshold_comes_from_the_settings() -> None:
    """Kynnys on luettu asetuksista eikä kovakoodattu.

    Sama väite kuin hyväksymiskriteerissä ``stack_min_players = 5``, mutta
    ilman demoja: neljän pelaajan asetelma katoaa, viiden ei.
    """
    four = at(15.0, "BombsiteB", "ct1", "ct2") + at(
        15.0, "SideEntrance", "ct3", "ct4"
    )
    assert len(stack(four)) == 1
    assert stack(four, min_players=5) == []
    five = four + at(15.0, "Ramp", "ct5")
    assert len(stack(five, min_players=5)) == 1


def test_the_same_player_twice_does_not_raise_the_stack_count() -> None:
    """Pelaajat ovat joukko: kaksoisrivi ei tee kolmesta neljää.

    Sama vartija kuin etenemisellä ja crunchilla, ja samasta syystä:
    pelaajamäärä on raportin luku.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1", "ct2", "ct3")
        + at(15.0, "SideEntrance", "ct1")
        + at(15.0, "Ramp", "ct2")
    )
    assert stack(rows) == []


def test_stack_reads_only_alive_ct_time_rows() -> None:
    """Kolme rajausta, jotka stack jakaa kahden muun säännön kanssa."""
    site = "BombsiteB"
    assert stack(at(15.0, site, "t1", "t2", "t3", "t4", side="T")) == []
    assert stack(at(15.0, site, "c1", "c2", "c3", "c4", alive=False)) == []
    assert (
        stack(at(15.0, site, "c1", "c2", "c3", "c4", kind=FIRST_CONTACT_SAMPLE))
        == []
    )


def test_two_sample_points_are_two_stack_hits_on_one_round() -> None:
    """Osuma on **yhden näytepisteen** havainto, kuten kahdella muulla.

    Ryhmittely otannaksi tehdään aggregoinnissa, ei täällä. Kalibroinnissa
    Anubiksen k4 on juuri tämä tapaus: 15 s ja 30 s, 5/5 ja 4/5.
    """
    rows = (
        at(15.0, "BombsiteB", "ct1", "ct2")
        + at(15.0, "SideEntrance", "ct3", "ct4", "ct5")
        + at(30.0, "BombsiteB", "ct1", "ct2", "ct3", "ct4")
        + at(30.0, "House", "ct5")
    )
    hits = stack(rows)
    assert [(h.sample_t_s, h.players, h.alive) for h in hits] == [
        (15.0, 5, 5),
        (30.0, 4, 5),
    ]


def test_the_group_margin_leaves_the_shared_middle_out() -> None:
    """Marginaali on se, mikä tekee jaetusta keskestä jaetun.

    ``Corner`` on 40 ruudun päässä A:sta ja 60:n päässä B:stä, eli suhde 1,5:
    marginaalilla 1,0 se on A:n ryhmässä, marginaalilla 1,6 ei kummankaan.
    ``Middle`` on tasan keskellä eikä kuulu kumpaankaan edes marginaalilla
    1,0, koska kumpikaan site ei ole aidosti lähempi.
    """
    near_a = [CloudCell("Corner", 40, 0, 0), *cloud()]
    loose = site_groups(near_a, margin=1.0, separation_min=SEPARATION_MIN)
    tight = site_groups(near_a, margin=1.6, separation_min=SEPARATION_MIN)
    assert loose is not None and tight is not None
    assert loose["Corner"] == "A"
    assert "Corner" not in tight
    assert "Middle" not in loose


def test_the_stack_thresholds_are_refused_when_they_would_break_the_rule() -> None:
    """Kolme arvoa, jotka tekisivät säännöstä hiljaa jotain muuta."""
    with pytest.raises(ValueError, match="alle 1,0"):
        site_groups(cloud(), margin=0.9, separation_min=SEPARATION_MIN)
    with pytest.raises(ValueError, match="ei ole positiivinen"):
        site_groups(cloud(), margin=MARGIN, separation_min=0.0)
    with pytest.raises(ValueError, match="ei ole positiivinen"):
        stack_hits([], groups={}, max_sample_s=MAX_SAMPLE_S, min_players=0)


def test_a_group_that_is_not_a_site_is_refused() -> None:
    """Ryhmät tulevat ``site_groups``ista, eivät mistä tahansa kartasta."""
    with pytest.raises(ValueError, match="tuntematon ryhmä"):
        stack_hits(
            at(15.0, "BombsiteB", "ct1"),
            groups={"BombsiteB": "C"},
            max_sample_s=MAX_SAMPLE_S,
            min_players=1,
        )


def test_a_silenced_demo_looks_the_same_as_a_measured_negative_from_here() -> None:
    """Vaiennettu demo ja mitattu negatiivinen ovat sama tyhjä lista.

    Ero **ei ole** säännön palautusarvossa vaan kattavuudessa, ja juuri siksi
    kutsujan on kirjattava vaikeneminen erikseen: täältä katsottuna Nuken
    kierros ja "ei stackeja" näyttävät samalta.

    Tyhjä ryhmäkuvaus on tässä **kutsujan oma arvo**, ei ``site_groups``in
    tulos: siten etäisyys omaan keskipisteeseensä on 0, joten kumpikin site
    kuuluu aina omaan ryhmäänsä eikä funktio voi palauttaa tyhjää
    sanakirjaa. Sääntö silti kestää sen, koska se on julkinen.
    """
    rows = at(15.0, "BombsiteB", "ct1", "ct2", "ct3", "ct4")
    assert (
        stack_hits(rows, groups=None, max_sample_s=MAX_SAMPLE_S, min_players=4)
        == []
    )
    assert (
        stack_hits(rows, groups={}, max_sample_s=MAX_SAMPLE_S, min_players=4)
        == []
    )
    # Ja väite siitä, ettei tyhjää kuvausta voi syntyä johtamalla.
    assert groups() != {}


def test_a_hit_cannot_carry_a_field_its_rule_did_not_measure() -> None:
    """``AnomalyHit``in vartija molempiin suuntiin.

    Sama peruste kuin ``sources``illa jo oli: kenttä kuuluu sille säännölle,
    joka sen mittasi, koska raportin lukija näkee vain arvon eikä sen
    lähdettä.
    """
    with pytest.raises(ValueError, match="ei kanna alueen orientaatiota"):
        AnomalyHit(rule=CT_ADVANCE, area=T_AREA, sample_t_s=6.0, players=1)
    with pytest.raises(ValueError, match="kantaa alueen orientaatiota"):
        AnomalyHit(
            rule=STACK,
            area="BombsiteA",
            sample_t_s=6.0,
            players=4,
            alive=5,
            site="A",
            t_share=0.9,
            observations=30,
        )
    with pytest.raises(ValueError, match="kantaa stackin kenttiä"):
        AnomalyHit(
            rule=CRUNCH,
            area=T_AREA,
            sample_t_s=6.0,
            players=2,
            t_share=0.9,
            observations=30,
            alive=5,
        )
    with pytest.raises(ValueError, match="osajoukko elossa olevista"):
        AnomalyHit(
            rule=STACK,
            area="BombsiteA",
            sample_t_s=6.0,
            players=5,
            alive=4,
            site="A",
        )
