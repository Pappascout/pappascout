"""``domain.aggregate`` -- aggregoinnin laskennan testit.

Kaikki taulut rakennetaan käsin, eikä yksikään testi tarvitse demoa tai
arkistoa: I/O-matriisin jokainen rivi on tässä tiedostossa omana testinään.
"""

from __future__ import annotations

from datetime import UTC, datetime

import polars as pl
import pytest

from pappascout.domain.aggregate import (
    CLASSIFY_THRESHOLD_KEYS,
    area_distributions,
    armed_players_for,
    armored_by_round,
    armored_players_for,
    bucket_labels,
    build_report,
    demo_buckets,
    first_contact_areas,
    lineups_of_same_team,
    roster_entries,
    team_identity,
    map_name_for,
    players_distribution,
    positions_for,
    sample_for,
    seconds_bucket,
    team_slug,
    check_rounds_are_unique,
    classify_thresholds,
    deaths_for,
    unpaired_detonations,
    utility_counts_for,
    utility_uses,
)
from pappascout.domain.models import AggregateSettings, ThresholdSettings
from pappascout.domain.report import MissingDemo, RosterEntry, TeamReport
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    MONEY_DISTRIBUTION_COLUMN,
    ROUNDS,
    TICKS,
    validate,
)
from pappascout.errors import AggregateError

TEAM = "aaaaaaaaaaaaaaaa"
OPPONENT = "bbbbbbbbbbbbbbbb"
MAP_POOL = ["de_ancient", "de_anubis", "de_inferno", "de_nuke", "de_mirage"]


def thresholds(**overrides: object) -> ThresholdSettings:
    """Kynnykset testiä varten; oletukset ovat samat kuin tuotannossa."""
    values: dict[str, object] = {"pistol_rounds": [1, 13]}
    values.update(overrides)
    return ThresholdSettings(**values)


def aggregate_settings(**overrides: object) -> AggregateSettings:
    """``[aggregate]``-osio testiä varten; oletus on sama kuin tuotannossa."""
    return AggregateSettings(**overrides)


# --- Taulujen rakennus ----------------------------------------------------------


def _inputs(armed: int | None) -> dict[str, object]:
    """``CLASSIFIED.inputs`` -struktin kaikki kentät, jotta skeema täsmää."""
    return {
        "money_buy_end": 0,
        "money_spent": 0,
        "money_players": [0, 0, 0, 0, 0],
        "equip_buy_end": 0,
        "equip_round_start": 0,
        "survivors_prev": 0,
        "survivors_equip_prev": 0,
        "prev_round_won": False,
        "players": 5,
        "players_readable": 5,
        "players_armed": armed,
        "loss_bonus_if_lost": 1400,
        "players_can_buy": 0,
        "full_equip_min": 4000,
        "force_buy_min": 1500,
        "armed_players_min": 3,
        "normal_buy_money_min": 4000,
        "normal_buy_players_min": 3,
        "anomaly_equip_max_after_win": 2000,
    }


def classified_row(
    demo: str,
    round_no: int,
    *,
    side: str = "T",
    round_type: str | None = "pistol",
    is_league: bool | None = None,
    armed: int | None = 5,
) -> dict[str, object]:
    return {
        "map_demo_id": demo,
        "round_no": round_no,
        "side": side,
        "won": True,
        "round_type": round_type,
        "opp_round_type": "pistol",
        "loss_count": 1,
        "reason": "testi",
        "inputs": _inputs(armed),
        "is_league": is_league,
        "roster_class": None,
    }


def classified_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(CLASSIFIED))
    return validate(df, CLASSIFIED, "classified")


def tick_row(
    demo: str,
    round_no: int,
    player: str,
    area: str | None,
    *,
    lineup: str = TEAM,
    side: str = "T",
    sample_kind: str = "time",
    sample_t_s: float = 6.0,
    is_alive: bool = True,
) -> dict[str, object]:
    return {
        "map_demo_id": demo,
        "round_raw": round_no + 1,
        "round_no": round_no,
        "player_id": player,
        "lineup_key": lineup,
        "side": side,
        "sample_kind": sample_kind,
        "sample_t_s": sample_t_s,
        "t_s": sample_t_s,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "area": area,
        "is_alive": is_alive,
    }


def ticks_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(TICKS))
    return validate(df, TICKS, "ticks")


#: Klaaninimet, jotka kokoonpanotaulu antaa testiarkistossa. Oikeista demoista
#: mitattuja: raportin otsikko on juuri tämä merkkijono.
TEAM_CLAN = "MatureMayhem"
OPPONENT_CLAN = "KALJUKOSTAJA"


def lineup_row(
    demo: str,
    player: str,
    *,
    lineup: str = TEAM,
    player_name: str | None = None,
    clan_name: str | None = TEAM_CLAN,
) -> dict[str, object]:
    """Yksi kokoonpanotaulun rivi.

    ``player_name`` ja ``clan_name`` ovat havaintoja: ``None`` tarkoittaa
    "ei havaittu" eikä sitä korvata tunnisteella.
    """
    return {
        "map_demo_id": demo,
        "lineup_key": lineup,
        "player_id": player,
        "player_name": player_name,
        "clan_name": clan_name,
    }


def lineups_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(LINEUPS))
    return validate(df, LINEUPS, "lineups")


def event_rows(
    demo: str,
    round_no: int,
    grenade_no: int,
    grenade_type: str,
    *,
    throw_area: str | None = "TSpawn",
    detonate_area: str | None = "BombsiteB",
    t_s: float = 3.0,
    lineup: str = TEAM,
    side: str = "T",
) -> list[dict[str, object]]:
    """Heitto ja räjähdys parina, kuten ``parse`` ne kirjoittaa."""
    common = {
        "map_demo_id": demo,
        "round_raw": round_no + 1,
        "round_no": round_no,
        "grenade_no": grenade_no,
        "grenade_entity_id": 100 + grenade_no,
        "grenade_type": grenade_type,
        "thrower_id": "p1",
        "lineup_key": lineup,
        "side": side,
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
    }
    return [
        {
            **common,
            "event_kind": "grenade_thrown",
            "t_s": t_s,
            "area": throw_area,
            "area_source": None if throw_area is None else "observed",
            "snap_distance": None,
        },
        {
            **common,
            "event_kind": "grenade_detonate",
            "t_s": t_s + 2.0,
            "area": detonate_area,
            "area_source": None if detonate_area is None else "snapped",
            "snap_distance": None if detonate_area is None else 120.0,
        },
    ]


def events_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(EVENTS))
    return validate(df, EVENTS, "events")


def death_row(
    demo: str,
    round_no: int,
    *,
    victim: str = "p1",
    victim_lineup: str = TEAM,
    victim_side: str = "T",
    victim_area: str | None = "Cave",
    attacker: str | None = "o1",
    attacker_lineup: str | None = OPPONENT,
    attacker_side: str | None = "CT",
    attacker_area: str | None = "Middle",
    t_s: float = 24.0,
    weapon: str = "ak47",
) -> dict[str, object]:
    """Yksi kuolemarivi, kuten ``parse`` sen kirjoittaa.

    Oletus on vastustajan tekemä tappo omalle pelaajalle: uhri on
    :data:`TEAM`in kokoonpanossa ja ampuja :data:`OPPONENT`in. Omat tapot
    rakennetaan vaihtamalla ``attacker_lineup``.
    """
    return {
        "map_demo_id": demo,
        "round_raw": round_no + 1,
        "round_no": round_no,
        "t_s": t_s,
        "victim_id": victim,
        "victim_lineup_key": victim_lineup,
        "victim_side": victim_side,
        "victim_x": 1.0,
        "victim_y": 2.0,
        "victim_z": 3.0,
        "victim_area": victim_area,
        "attacker_id": attacker,
        "attacker_lineup_key": None if attacker is None else attacker_lineup,
        "attacker_side": None if attacker is None else attacker_side,
        "attacker_x": None if attacker is None else 4.0,
        "attacker_y": None if attacker is None else 5.0,
        "attacker_z": None if attacker is None else 6.0,
        "attacker_area": None if attacker is None else attacker_area,
        "weapon": weapon,
    }


def deaths_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(DEATHS))
    return validate(df, DEATHS, "deaths")


def round_row(
    demo: str,
    round_no: int | None,
    *,
    side: str = "T",
    lineup: str = TEAM,
    armored: int | None = 5,
    armed: int | None = 0,
) -> dict[str, object]:
    """Yksi kierrostaulun rivi. Vain panssarilaskuri luetaan täältä.

    Oletus on pistoolikierroksen asetelma -- viisi kevlaria, nolla aseistettua
    -- koska juuri se erottaa laskurit toisistaan.
    """
    return {
        "map_demo_id": demo,
        "round_raw": None if round_no is None else round_no + 1,
        "round_no": round_no,
        "lineup_key": lineup,
        "side": side,
        "won": True,
        "win_reason": "elimination",
        "money_buy_end": 0,
        "money_spent": 0,
        "equip_buy_end": 0,
        "equip_round_start": 0,
        "players_buy_end": 5,
        MONEY_DISTRIBUTION_COLUMN: [0, 0, 0, 0, 0],
        ARMED_COLUMN: armed,
        ARMORED_COLUMN: armored,
        "survivors": 5,
        "survivors_equip_prev": 0,
        "freeze_end_tick": 100,
        "buy_end_tick": 200,
        "tick_rate": 64.0,
        "status": "ok",
    }


def rounds_frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    df = pl.DataFrame(rows, schema=dict(ROUNDS))
    return validate(df, ROUNDS, "rounds")


def rounds_for(classified: list[dict[str, object]]) -> list[dict[str, object]]:
    """Kierrostaulu, joka kattaa täsmälleen annetut luokitellut kierrokset.

    Panssariluku on oletusarvoinen; testi, joka tutkii sitä, rakentaa
    kierrostaulunsa itse.
    """
    return [
        round_row(str(row["map_demo_id"]), row["round_no"], side=str(row["side"]))
        for row in classified
    ]


def team_report(lineups: list[str] | None = None) -> TeamReport:
    keys = lineups or [TEAM]
    return TeamReport(
        key=keys[0],
        slug=team_slug(keys[0]),
        display_name=keys[0],
        lineup_keys=keys,
        roster=[
            RosterEntry(player_id=f"p{n}", display_name=f"nimi{n}")
            for n in range(1, 6)
        ],
        roster_source="lineups",
    )


def report_for(
    classified: list[dict[str, object]],
    ticks: list[dict[str, object]] | None = None,
    events: list[dict[str, object]] | None = None,
    deaths: list[dict[str, object]] | None = None,
    *,
    rounds: list[dict[str, object]] | None = None,
    limits: ThresholdSettings | None = None,
    windows: AggregateSettings | None = None,
    missing: list[MissingDemo] | None = None,
    lineups: list[str] | None = None,
):
    return build_report(
        classified=classified_frame(classified),
        ticks=ticks_frame(ticks or []),
        events=events_frame(events or []),
        deaths=deaths_frame(deaths or []),
        rounds=rounds_frame(
            rounds if rounds is not None else rounds_for(classified)
        ),
        team=team_report(lineups),
        thresholds=limits or thresholds(),
        aggregate=windows or aggregate_settings(),
        map_pool=MAP_POOL,
        generated_at=datetime(2026, 8, 30, tzinfo=UTC),
        missing_demos=missing or [],
    )


def branch(report, map_name: str, side: str, round_type: str):
    """Yhden kartta/puoli/kierrostyyppi -haaran haku raportista."""
    m = next(m for m in report.maps if m.map_name == map_name)
    s = next(s for s in m.sides if s.side == side)
    return next(rt for rt in s.round_types if rt.round_type == round_type)


# --- Pienet puhtaat funktiot ----------------------------------------------------


def test_bucket_labels_name_every_window_including_the_open_one() -> None:
    assert bucket_labels([5.0, 10.0, 20.0]) == ["0-5", "5-10", "10-20", "20+"]


def test_empty_bucket_edges_mean_one_window() -> None:
    """Aikaikkunan poistaminen on asetus, ei koodimuutos."""
    assert bucket_labels([]) == ["kaikki"]
    assert seconds_bucket(37.0, []) == "kaikki"


@pytest.mark.parametrize(
    "t_s,expected",
    [(0.0, "0-5"), (4.9, "0-5"), (5.0, "5-10"), (19.9, "10-20"), (20.0, "20+")],
)
def test_bucket_edge_belongs_to_the_upper_window(t_s: float, expected: str) -> None:
    """Yksi sääntö rajalle; kahta lukutapaa ei sallita."""
    assert seconds_bucket(t_s, [5.0, 10.0, 20.0]) == expected


def test_missing_throw_time_gets_its_own_bucket() -> None:
    """Puuttuva aika on eri asia kuin nolla sekuntia."""
    assert seconds_bucket(None, [5.0, 10.0]) == "tuntematon"


@pytest.mark.parametrize(
    "demo,expected",
    [
        ("Ancient_vs_kaljukostaja", ("de_ancient", "map_demo_id")),
        ("Nuke_vs_imuaijat", ("de_nuke", "map_demo_id")),
        ("de_inferno-2026", ("de_inferno", "map_demo_id")),
    ],
)
def test_map_name_is_read_from_the_demo_id(demo: str, expected: tuple) -> None:
    assert map_name_for(demo, MAP_POOL) == expected


def test_unknown_map_keeps_its_identifier_and_says_so() -> None:
    """FACEIT-tunnisteessa ei ole kartan nimeä, eikä sitä arvata."""
    assert map_name_for("1-a52ebff2-1-1", MAP_POOL) == ("1-a52ebff2-1-1", "unknown")


def test_map_name_is_not_matched_as_a_substring() -> None:
    """Joukkue nimeltä *Infernal* ei ole Inferno."""
    name, source = map_name_for("Infernal_vs_x", MAP_POOL)
    assert source == "unknown"
    assert name == "Infernal_vs_x"


def test_lineups_with_enough_shared_players_are_one_team() -> None:
    """Yksi vaihto tuottaa uuden tunnisteen; sama joukkue silti."""
    members = {
        "a": {"1", "2", "3", "4", "5"},
        "b": {"1", "2", "3", "4", "9"},
        "c": {"6", "7", "8", "9", "10"},
    }
    assert lineups_of_same_team("a", members, 3) == ["a", "b"]


def test_lineups_are_not_chained_through_a_third_team() -> None:
    """Vertailu tehdään kohteeseen; ketjuttaminen sulattaisi kaksi joukkuetta."""
    members = {
        "a": {"1", "2", "3", "4", "5"},
        "b": {"3", "4", "5", "6", "7"},
        "c": {"5", "6", "7", "8", "9"},
    }
    assert lineups_of_same_team("a", members, 3) == ["a", "b"]


def test_unknown_lineup_is_an_error_not_an_empty_team() -> None:
    with pytest.raises(AggregateError, match="ei löydy"):
        lineups_of_same_team("x", {"a": {"1"}}, 3)


def test_team_slug_survives_an_identifier_that_is_not_a_filename() -> None:
    assert team_slug("Mature Mayhem / 2026") == "mature-mayhem-2026"


# --- Otanta ---------------------------------------------------------------------


def test_empty_is_league_lands_in_unknown_not_other() -> None:
    """Käsin tuotu demo ei ole liigaottelu eikä 'muu' -- se on tuntematon."""
    rows = [classified_row("Anubis_vs_x", n) for n in (1, 2)]
    assert demo_buckets(rows) == {"Anubis_vs_x": "unknown"}
    s = sample_for(rows, demo_buckets(rows))
    assert (s.unknown.demos, s.unknown.rounds) == (1, 2)
    assert s.other.rounds == 0 and s.league.rounds == 0


def test_a_demo_cannot_belong_to_two_buckets() -> None:
    rows = [
        classified_row("x", 1, is_league=True),
        classified_row("x", 2, is_league=False),
    ]
    with pytest.raises(AggregateError, match="kahteen otantalokeroon"):
        demo_buckets(rows)


def test_league_and_other_stay_apart_through_the_sample() -> None:
    rows = [
        classified_row("a", 1, is_league=True),
        classified_row("b", 1, is_league=False),
        classified_row("c", 1),
    ]
    s = sample_for(rows, demo_buckets(rows))
    assert (s.league.rounds, s.other.rounds, s.unknown.rounds) == (1, 1, 1)
    assert s.demos == 3


# --- Jakaumat -------------------------------------------------------------------


def test_players_distribution_keeps_the_zero_bucket() -> None:
    dist = players_distribution([0, 0, 3])
    assert [(p.players, p.n) for p in dist] == [(0, 2), (3, 1)]


def test_area_without_players_still_gets_a_row() -> None:
    """I/O-matriisi: näytepisteessä tyhjä alue on havainto, ei puuttuva rivi."""
    rows = {
        ("d", 1): [{"area": "BombsiteA"}, {"area": "BombsiteA"}],
        ("d", 2): [{"area": "BombsiteB"}],
    }
    dists = {d.area: d for d in area_distributions(rows)}
    assert [(p.players, p.n) for p in dists["BombsiteA"].players_dist] == [
        (0, 1),
        (2, 1),
    ]
    assert sum(p.n for p in dists["BombsiteB"].players_dist) == 2


def test_dead_players_do_not_count_towards_an_area() -> None:
    ticks = [
        tick_row("d", 1, "p1", "BombsiteA"),
        tick_row("d", 1, "p2", "BombsiteA", is_alive=False),
    ]
    position = positions_for(ticks, [("d", 1)])[0]
    assert [(p.players, p.n) for p in position.areas[0].players_dist] == [(1, 1)]


def test_a_round_where_everyone_died_still_belongs_to_the_sample() -> None:
    """Muuten kierros katoaisi otannasta ja Σ n = m pettäisi."""
    ticks = [
        tick_row("d", 1, "p1", "BombsiteA"),
        tick_row("d", 2, "p1", "BombsiteA", is_alive=False),
    ]
    position = positions_for(ticks, [("d", 1), ("d", 2)])[0]
    assert position.m == 2
    assert [(p.players, p.n) for p in position.areas[0].players_dist] == [
        (0, 1),
        (1, 1),
    ]


def test_a_sample_point_that_the_round_never_reached_is_reported_missing() -> None:
    """45 s puuttuu kierrokselta, joka ratkesi 30 sekunnissa."""
    ticks = [
        tick_row("d", 1, "p1", "A", sample_t_s=6.0),
        tick_row("d", 2, "p1", "A", sample_t_s=6.0),
        tick_row("d", 1, "p1", "A", sample_t_s=45.0),
    ]
    positions = {p.seconds: p for p in positions_for(ticks, [("d", 1), ("d", 2)])}
    assert (positions[6.0].m, positions[6.0].rounds_missing) == (2, 0)
    assert (positions[45.0].m, positions[45.0].rounds_missing) == (1, 1)


def test_first_contact_is_one_position_not_one_per_round() -> None:
    """Hetki on eri joka kierroksella, joten sillä ei voi ryhmitellä."""
    ticks = [
        tick_row("d", 1, "p1", "A", sample_kind="first_contact", sample_t_s=11.0),
        tick_row("d", 2, "p1", "A", sample_kind="first_contact", sample_t_s=23.0),
    ]
    positions = positions_for(ticks, [("d", 1), ("d", 2)])
    assert len(positions) == 1
    assert positions[0].sample_kind == "first_contact"
    assert positions[0].seconds is None
    assert positions[0].seconds_median == 17.0


def test_first_contact_areas_count_presence_not_players() -> None:
    ticks = [
        tick_row("d", 1, "p1", "Banana", sample_kind="first_contact", sample_t_s=9.0),
        tick_row("d", 1, "p2", "Banana", sample_kind="first_contact", sample_t_s=9.0),
        tick_row("d", 2, "p1", "Banana", sample_kind="first_contact", sample_t_s=9.0),
        tick_row("d", 2, "p2", "Apartments", sample_kind="first_contact", sample_t_s=9.0),
    ]
    areas = {a.area: (a.n, a.m) for a in first_contact_areas(ticks, [("d", 1), ("d", 2)])}
    assert areas == {"Banana": (2, 2), "Apartments": (1, 2)}


# --- Utility --------------------------------------------------------------------


def test_utility_pairs_throw_and_detonation_by_grenade_no() -> None:
    events = event_rows("d", 1, 0, "smoke", throw_area="TSpawn", detonate_area="BombsiteB")
    use = utility_uses(events, [("d", 1)], [5.0, 10.0, 20.0])[0]
    assert (use.grenade_type, use.throw_area, use.detonate_area) == (
        "smoke",
        "TSpawn",
        "BombsiteB",
    )
    assert use.area_source == "snapped"
    assert (use.n, use.throws, use.m) == (1, 1, 1)


def test_utility_without_a_detonation_area_is_counted_not_dropped() -> None:
    """I/O-matriisi: savu heitetään sinne, missä ei ole ketään."""
    events = event_rows("d", 1, 0, "smoke", detonate_area=None)
    use = utility_uses(events, [("d", 1)], [5.0])[0]
    assert use.detonate_area is None
    assert use.area_source is None
    assert use.n == 1


def test_rounds_and_throws_are_counted_separately() -> None:
    """Kaksi samanlaista kranaattia yhdellä kierroksella on yksi kierros."""
    events = event_rows("d", 1, 0, "flashbang") + event_rows("d", 1, 1, "flashbang")
    use = utility_uses(events, [("d", 1), ("d", 2)], [5.0])[0]
    assert (use.n, use.throws, use.m) == (1, 2, 2)


def test_utility_counts_answer_how_many_were_thrown_per_round() -> None:
    """Tavoiteanalyysin rivi *"2 savua"* -- ei johdettavissa utility-riveistä."""
    events = (
        event_rows("d", 1, 0, "smoke")
        + event_rows("d", 1, 1, "smoke")
        + event_rows("d", 2, 2, "smoke")
    )
    counts = utility_counts_for(events, [("d", 1), ("d", 2), ("d", 3)])
    assert len(counts) == 1
    assert counts[0].grenade_type == "smoke"
    assert [(c.thrown, c.n) for c in counts[0].counts] == [(0, 1), (1, 1), (2, 1)]
    assert counts[0].m == 3


def test_events_from_other_rounds_do_not_leak_into_the_branch() -> None:
    events = event_rows("d", 9, 0, "he")
    assert utility_uses(events, [("d", 1)], [5.0]) == []
    assert utility_counts_for(events, [("d", 1)]) == []


# --- Aseistetut pelaajat --------------------------------------------------------


def test_armed_players_distribution_keeps_unknown_out_of_the_sample() -> None:
    rows = [
        classified_row("d", 1, armed=5),
        classified_row("d", 2, armed=0),
        classified_row("d", 3, armed=None),
    ]
    armed = armed_players_for(rows)
    assert armed.m == 2
    assert armed.rounds_unknown == 1
    assert [(c.armed, c.n) for c in armed.counts] == [(0, 1), (5, 1)]


# --- Panssaroidut pelaajat (Story 2.8) ------------------------------------------


def test_armored_lookup_reads_the_rounds_table_by_demo_round_and_side() -> None:
    """Avain on kolmiosainen: kierrostaulussa on kaksi riviä per kierros.

    Ilman puolta vastustajan panssariluku voisi päätyä omalle riville --
    pelkkä (demo, kierros) osuu molempiin.
    """
    lookup = armored_by_round(
        [
            round_row("d", 1, side="T", lineup=TEAM, armored=5),
            round_row("d", 1, side="CT", lineup=OPPONENT, armored=1),
        ]
    )
    assert lookup[("d", 1, "T")] == 5
    assert lookup[("d", 1, "CT")] == 1


def test_armored_lookup_drops_unnumbered_rounds_and_missing_observations() -> None:
    """Numeroimaton kierros ja lukukelvoton panssari eivät ole nollia.

    Kumpikin jää kartasta pois, ja puuttuva avain tarkoittaa
    ``rounds_unknown``ia -- nolla väittäisi, ettei kenelläkään ollut
    panssaria.
    """
    lookup = armored_by_round(
        [
            round_row("d", None, armored=5),
            round_row("d", 2, armored=None),
            round_row("d", 3, armored=0),
        ]
    )
    assert set(lookup) == {("d", 3, "T")}
    assert lookup[("d", 3, "T")] == 0


def test_two_different_armor_readings_for_one_round_are_refused() -> None:
    """Kaksoisavain on virhe eikä hiljainen ylikirjoitus.

    Luokitelluille riveille ajetaan ``check_rounds_are_unique``; ilman
    vastaavaa tarkistusta hakukartassa jäisi voimaan se rivi, joka sattuu
    olemaan viimeisenä, eikä mikään kertoisi kumpi luku raporttiin päätyi.
    """
    with pytest.raises(AggregateError, match="kaksi eri panssarilukua"):
        armored_by_round(
            [
                round_row("d", 1, side="T", armored=5),
                round_row("d", 1, side="T", armored=1),
            ]
        )


def test_an_identical_duplicate_row_is_not_an_error() -> None:
    """Sama luku kahdesti ei ole ristiriita, joten se ei kaada ajoa.

    Ilman tätä paria edellinen testi menisi läpi myös toteutuksella, joka
    hylkää jokaisen toistuvan avaimen -- ja kahdesti luettu identtinen rivi
    ei jätä epäselväksi mikä luku raporttiin päätyy.
    """
    lookup = armored_by_round(
        [
            round_row("d", 1, side="T", armored=5),
            round_row("d", 1, side="T", armored=5),
        ]
    )
    assert lookup == {("d", 1, "T"): 5}


def test_a_rounds_row_with_a_missing_key_part_is_dropped() -> None:
    """Vajaa avain pudotetaan **ennen** ``str()``-muunnosta.

    ``str(None)`` rakentaisi avaimen ``"None"``, joka ei osu koskaan mutta
    näyttää kartassa täysin tavalliselta -- rivi katoaisi hiljaa ja näkyisi
    vasta puuttuvana havaintona raportissa.
    """
    lookup = armored_by_round(
        [
            round_row(None, 1, side="T", armored=5),
            round_row("d", 1, side=None, armored=4),
            round_row("d", 2, side="T", armored=3),
        ]
    )
    assert lookup == {("d", 2, "T"): 3}
    assert not any("None" in str(key) for key in lookup)


def test_a_classified_row_with_a_missing_key_part_is_unknown_not_a_crash() -> None:
    """Vajaa avain on puuttuva havainto, ei kaatuva ajo.

    ``int(None)`` tai ``row["side"]`` nostaisi poikkeuksen, joka veisi koko
    aggregoinnin -- yhden kierroksen puute ei saa maksaa koko raporttia.
    """
    rows = [
        classified_row("d", 1),
        {**classified_row("d", 2), "side": None},
    ]
    armored = armored_players_for(rows, {("d", 1, "T"): 5})

    assert armored.m == 1
    assert armored.rounds_unknown == 1


def test_armored_players_distribution_keeps_unknown_out_of_the_sample() -> None:
    """Sama erottelu kuin aseistetuilla: puuttuva havainto ei ole nolla."""
    rows = [classified_row("d", n) for n in (1, 2, 3)]
    lookup = armored_by_round(
        [
            round_row("d", 1, armored=5),
            round_row("d", 2, armored=0),
            round_row("d", 3, armored=None),
        ]
    )
    armored = armored_players_for(rows, lookup)
    assert armored.m == 2
    assert armored.rounds_unknown == 1
    assert [(c.armored, c.n) for c in armored.counts] == [(0, 1), (5, 1)]


def test_a_round_missing_from_the_rounds_table_is_unknown_not_zero() -> None:
    """Vanha tai vajaa kierrostaulu ei saa näyttää kevlarittomalta kierrokselta."""
    armored = armored_players_for([classified_row("d", 1)], {})
    assert armored.m == 0
    assert armored.rounds_unknown == 1
    assert armored.counts == []


def test_the_two_counters_answer_different_questions_on_a_pistol_round() -> None:
    """Pistoolikierros: panssarijakauma 5/5, aseistettujen jakauma 0.

    Tämä on tavoiteanalyysin rivi *"5 kevlaria"* (Nuke, T) sellaisena kuin
    aggregointi sen tuottaa. Jos jompikumpi laskuri luettaisiin toisesta
    lähteestä väärin, luvut olisivat samat -- ja juuri se on vika, jonka
    tämä testi estää.
    """
    rows = [classified_row("Nuke_vs_x", 13, armed=0)]
    lookup = armored_by_round([round_row("Nuke_vs_x", 13, armored=5)])

    assert [(c.armed, c.n) for c in armed_players_for(rows).counts] == [(0, 1)]
    assert [
        (c.armored, c.n) for c in armored_players_for(rows, lookup).counts
    ] == [(5, 1)]


def test_the_report_carries_both_counters_for_the_same_round_type() -> None:
    """Koko putki: molemmat jakaumat samassa haarassa, eri luvut.

    Ancientin CT-pistooli, Veetin *"ei kevuja"*: yksi kevlar viidestä ja
    nolla aseistettua.
    """
    classified = [classified_row("Ancient_vs_x", 1, side="CT", armed=0)]
    report = report_for(
        classified,
        rounds=[round_row("Ancient_vs_x", 1, side="CT", armored=1, armed=0)],
    )
    entry = branch(report, "de_ancient", "CT", "pistol")

    assert [(c.armed, c.n) for c in entry.players_armed.counts] == [(0, 1)]
    assert [(c.armored, c.n) for c in entry.players_armored.counts] == [(1, 1)]


def test_the_key_picks_our_side_from_the_two_rows_of_one_round() -> None:
    """Kierrostaulussa on kaksi riviä per kierros -- avain valitsee oman.

    Testi todentaa **avaimen kolmatta osaa**, ei kokoonpanosuodatusta: rivit
    annetaan tässä suodattamattomina, kuten ne kierrostaulussa ovat, ja
    pelkkä (demo, kierros) osuisi molempiin.
    """
    classified = [classified_row("Nuke_vs_x", 13, side="T", armed=0)]
    report = report_for(
        classified,
        rounds=[
            round_row("Nuke_vs_x", 13, side="T", lineup=TEAM, armored=5),
            round_row("Nuke_vs_x", 13, side="CT", lineup=OPPONENT, armored=0),
        ],
    )
    entry = branch(report, "de_nuke", "T", "pistol")
    assert [(c.armored, c.n) for c in entry.players_armored.counts] == [(5, 1)]


# --- Koko raportti: I/O-matriisi ------------------------------------------------


def test_one_demo_gives_one_map_and_an_unknown_sample() -> None:
    report = report_for(
        [classified_row("Anubis_vs_x", 1)],
        [tick_row("Anubis_vs_x", 1, "p1", "BombsiteA")],
    )
    assert [m.map_name for m in report.maps] == ["de_anubis"]
    assert report.sample.unknown.demos == 1
    assert report.sample.demos == 1


def test_four_demos_become_four_map_branches_and_the_sample_adds_up() -> None:
    rows = [
        classified_row(demo, 1)
        for demo in (
            "Ancient_vs_a",
            "Anubis_vs_b",
            "inferno_vs_c",
            "Nuke_vs_d",
        )
    ]
    report = report_for(rows)
    assert len(report.maps) == 4
    assert report.sample.rounds == 4
    assert sum(m.sample.rounds for m in report.maps) == report.sample.rounds


def test_the_same_map_twice_merges_into_one_branch() -> None:
    """I/O-matriisi: kierrokset summautuvat, ``demos`` on kaksi."""
    report = report_for(
        [
            classified_row("Nuke_vs_a", 1),
            classified_row("Nuke_vs_b", 1),
            classified_row("Nuke_vs_b", 2),
        ]
    )
    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_nuke"
    assert sorted(entry.map_demo_ids) == ["Nuke_vs_a", "Nuke_vs_b"]
    assert (entry.sample.demos, entry.sample.rounds) == (2, 3)


def test_a_round_type_that_was_never_played_is_absent_not_a_zero_row() -> None:
    report = report_for([classified_row("Nuke_vs_a", 1, round_type="pistol")])
    side = report.maps[0].sides[0]
    assert [rt.round_type for rt in side.round_types] == ["pistol"]


def test_overtime_is_its_own_round_type() -> None:
    report = report_for(
        [
            classified_row("Nuke_vs_a", 1, round_type="pistol"),
            classified_row("Nuke_vs_a", 25, round_type="ot"),
        ]
    )
    side = report.maps[0].sides[0]
    assert [rt.round_type for rt in side.round_types] == ["pistol", "ot"]


def test_full_buys_are_not_filtered_out() -> None:
    """Aggregointi ei päätä mitä raportoidaan; se laskee kaiken."""
    report = report_for(
        [classified_row("Nuke_vs_a", n, round_type="full") for n in (1, 2, 3, 4)]
    )
    assert branch(report, "de_nuke", "T", "full").sample.rounds == 4


def test_an_unclassified_round_is_counted_but_not_placed() -> None:
    """I/O-matriisi: ei mukaan rakenteeseen, mutta lukumäärä raportoidaan."""
    report = report_for(
        [
            classified_row("Nuke_vs_a", 1, round_type="pistol"),
            classified_row("Nuke_vs_a", 2, round_type=None),
        ]
    )
    assert report.unclassified_rounds == 1
    assert report.sample.rounds == 1


def test_a_small_sample_is_marked_but_still_reported() -> None:
    limits = thresholds(small_sample_rounds=3)
    report = report_for(
        [classified_row("Nuke_vs_a", n, round_type="eco") for n in (1, 2)],
        limits=limits,
    )
    assert branch(report, "de_nuke", "T", "eco").small_sample is True


def test_a_sample_at_or_above_the_threshold_is_not_marked_small() -> None:
    limits = thresholds(small_sample_rounds=3)
    report = report_for(
        [classified_row("Nuke_vs_a", n, round_type="eco") for n in (1, 2, 3)],
        limits=limits,
    )
    assert branch(report, "de_nuke", "T", "eco").small_sample is False


def test_the_sample_check_holds_across_every_area_of_a_real_shaped_branch() -> None:
    """Σ n = m alueen yli, kun kierrokset ovat eri alueilla."""
    classified = [classified_row("Nuke_vs_a", n, round_type="eco") for n in (1, 2, 3)]
    ticks = [
        tick_row("Nuke_vs_a", 1, "p1", "Ramp"),
        tick_row("Nuke_vs_a", 1, "p2", "Ramp"),
        tick_row("Nuke_vs_a", 2, "p1", "Hell"),
        tick_row("Nuke_vs_a", 3, "p1", "Ramp"),
        tick_row("Nuke_vs_a", 3, "p2", "Hell"),
        tick_row("Nuke_vs_a", 3, "p3", "Hell"),
    ]
    position = branch(report_for(classified, ticks), "de_nuke", "T", "eco").positions[0]
    assert position.m == 3
    for area in position.areas:
        assert sum(p.n for p in area.players_dist) == 3
    found = {a.area: [(p.players, p.n) for p in a.players_dist] for a in position.areas}
    assert found["Ramp"] == [(0, 1), (1, 1), (2, 1)]
    assert found["Hell"] == [(0, 1), (1, 1), (2, 1)]


def test_a_lost_round_breaks_the_sample_check_loudly() -> None:
    """Rakennettu rikki tarkoituksella: alueen otanta ei saa poiketa muista."""
    from pappascout.domain.report import AreaDistribution, PlayersCount, Position

    with pytest.raises(AggregateError):
        Position(
            sample_kind="time",
            seconds=6.0,
            m=3,
            rounds_missing=0,
            areas=[
                AreaDistribution(
                    area="Ramp", m=3, players_dist=[PlayersCount(players=0, n=3)]
                ),
                AreaDistribution(
                    area="Hell", m=2, players_dist=[PlayersCount(players=0, n=2)]
                ),
            ],
        )


def test_the_opponents_rows_never_reach_the_report() -> None:
    """Kokoonpanosuodatus on vaiheen vastuu, mutta liitos ei saa vuotaa."""
    classified = [classified_row("Nuke_vs_a", 1, round_type="pistol")]
    ticks = [
        tick_row("Nuke_vs_a", 1, "p1", "Ramp"),
        tick_row("Nuke_vs_a", 1, "x1", "Ramp", lineup=OPPONENT, side="CT"),
    ]
    # build_report saa jo suodatetun taulun, joten tämä testaa suodatuksen
    # jälkeistä tilaa: vain oman kokoonpanon rivit lasketaan.
    own = [r for r in ticks if r["lineup_key"] == TEAM]
    position = branch(report_for(classified, own), "de_nuke", "T", "pistol").positions[0]
    assert [(p.players, p.n) for p in position.areas[0].players_dist] == [(1, 1)]


def test_missing_demos_travel_into_the_report_with_their_reason() -> None:
    report = report_for(
        [classified_row("Nuke_vs_a", 1)],
        missing=[MissingDemo(match="Anubis_vs_b", reason="ei parsittu")],
    )
    assert [(m.match, m.reason) for m in report.missing_demos] == [
        ("Anubis_vs_b", "ei parsittu")
    ]


def test_thresholds_are_recorded_for_traceability() -> None:
    report = report_for([classified_row("Nuke_vs_a", 1)])
    assert report.thresholds_used["thresholds"]["small_sample_rounds"] == 3
    assert report.thresholds_used["aggregate"]["utility_seconds_buckets"] == [
        5.0,
        10.0,
        20.0,
    ]


def test_maps_are_ordered_by_how_much_was_played() -> None:
    rows = [classified_row("Nuke_vs_a", n) for n in (1, 2, 3)]
    rows += [classified_row("Anubis_vs_b", 1)]
    report = report_for(rows)
    assert [m.map_name for m in report.maps] == ["de_nuke", "de_anubis"]


def test_both_sides_stay_apart() -> None:
    rows = [
        classified_row("Nuke_vs_a", 1, side="T"),
        classified_row("Nuke_vs_a", 2, side="CT"),
    ]
    report = report_for(rows)
    assert [s.side for s in report.maps[0].sides] == ["T", "CT"]
    assert all(s.sample.rounds == 1 for s in report.maps[0].sides)


def test_the_report_carries_no_interpretation() -> None:
    """Ei tulkintoja -- vain havaintoja ja lukumääriä."""
    report = report_for(
        [classified_row("Nuke_vs_a", 1)],
        events=event_rows("Nuke_vs_a", 1, 0, "smoke"),
    )
    # thresholds_used on kynnysten kopio jäljitettävyyttä varten, ei havainto,
    # ja siellä esiintyy kynnysnimi stack_min_players. Tarkistus koskee
    # havaintoja.
    data = report.model_dump(mode="json")
    data.pop("thresholds_used")
    text = str(data).lower()
    for word in ("fake", "rush", "stack", "eksekuutio"):
        assert word not in text


# --- Katselmuksen löydökset ------------------------------------------------------


def test_a_single_edge_still_names_both_windows() -> None:
    assert bucket_labels([5.0]) == ["0-5", "5+"]


def test_two_edges_that_look_alike_are_refused() -> None:
    """Kaksi lokeroa samalla nimellä tekisi raportin rivistä monitulkintaisen."""
    with pytest.raises(AggregateError, match="samalta lokeron nimessä"):
        bucket_labels([5.000000001, 5.000000002])


def test_unknown_time_stays_unknown_even_without_windows() -> None:
    """Tyhjä rajalista ei saa sulauttaa tuntematonta hetkeä tunnettuihin."""
    assert seconds_bucket(None, []) == "tuntematon"
    assert seconds_bucket(float("nan"), []) == "tuntematon"


@pytest.mark.parametrize("t_s", [-1.0, float("nan"), float("inf")])
def test_an_impossible_throw_time_does_not_look_like_a_pattern(t_s: float) -> None:
    """Negatiivinen niputtuisi "instaksi" ja NaN valuisi viimeiseen lokeroon."""
    assert seconds_bucket(t_s, [5.0, 10.0, 20.0]) == "tuntematon"


def test_first_contact_median_is_taken_over_rounds_not_player_rows() -> None:
    """Neljä elossa olevaa 10 s kohdalla ei saa painaa mediaania alas."""
    ticks = [
        tick_row("d", 1, f"p{i}", "A", sample_kind="first_contact", sample_t_s=10.0)
        for i in range(4)
    ] + [
        tick_row("d", 2, "p1", "A", sample_kind="first_contact", sample_t_s=20.0)
    ]
    position = positions_for(ticks, [("d", 1), ("d", 2)])[0]
    assert position.seconds_median == 15.0


def test_a_time_sample_without_its_second_is_an_error_not_a_crash() -> None:
    row = tick_row("d", 1, "p1", "A")
    row["sample_t_s"] = None
    with pytest.raises(AggregateError, match="sample_t_s"):
        positions_for([row], [("d", 1)])


def test_a_grenade_that_detonates_in_the_next_round_keeps_its_area() -> None:
    """Molotov palaa seitsemän sekuntia; pari ei saa katketa kierrosrajalla."""
    rows = event_rows("d", 1, 0, "molotov", detonate_area="Banana", t_s=110.0)
    # Räjähdys osuu seuraavan kierroksen puolelle, kuten oikeassa demossa.
    rows[1]["round_no"] = 2
    rows[1]["round_raw"] = 3
    use = utility_uses(rows, [("d", 1)], [5.0, 10.0, 20.0])[0]
    assert use.detonate_area == "Banana"
    assert use.area_source == "snapped"


def test_a_detonation_without_a_throw_is_counted_not_hidden() -> None:
    rows = event_rows("d", 1, 0, "smoke")
    orphan = [r for r in rows if r["event_kind"] == "grenade_detonate"]
    assert unpaired_detonations(rows) == 0
    assert unpaired_detonations(orphan) == 1
    # Ja se ei päädy utilityyn: heittoaluetta eikä hetkeä ei ole.
    assert utility_uses(orphan, [("d", 1)], [5.0]) == []


def test_a_duplicated_round_is_refused() -> None:
    """Kaksoiskappale vääristäisi sekä m:n että rounds_missingin."""
    rows = [classified_row("d", 1), classified_row("d", 1)]
    with pytest.raises(AggregateError, match="useammin kuin"):
        check_rounds_are_unique(rows)


def test_a_classified_row_without_a_round_number_is_named() -> None:
    """Suojaamaton int(None) kaatuisi TypeErroriin ilman ohjetta."""
    row = classified_row("Nuke_vs_a", 1)
    row["round_no"] = None
    with pytest.raises(AggregateError, match="ilman kierrosnumeroa"):
        report_for([classified_row("Nuke_vs_a", 2), row])


def test_classify_thresholds_are_read_from_the_classified_table() -> None:
    """Havainto siitä, millä kynnyksillä kierrokset oikeasti luokiteltiin."""
    found = classify_thresholds([classified_row("d", 1)])
    assert set(found) == set(CLASSIFY_THRESHOLD_KEYS)
    assert found["full_equip_min"] == 4000


def test_rounds_classified_with_different_thresholds_are_refused() -> None:
    """Sekoitus tuottaisi luvun, joka ei tarkoita yhtä asiaa."""
    a = classified_row("d", 1)
    b = classified_row("d", 2)
    b["inputs"] = dict(b["inputs"]) | {"full_equip_min": 3800}
    with pytest.raises(AggregateError, match="eri kynnyksillä"):
        classify_thresholds([a, b])


def test_rounds_classified_with_stale_thresholds_are_refused() -> None:
    """Kynnyksen muutos ilman uutta luokittelua nimeäisi väärät kynnykset.

    Käyttäjä voi muuttaa ``settings.toml``ia ja ajaa pelkän aggregoinnin.
    Silloin ``thresholds_used`` kertoisi kynnyksistä, joilla yhtäkään
    kierrosta ei luokiteltu -- ja jokainen kierrostyyppi olisi laskettu
    vanhoilla säännöillä.
    """
    rows = [classified_row("d", 1)]
    with pytest.raises(AggregateError, match="kuin mitä"):
        classify_thresholds(rows, thresholds(full_equip_min=4500))
    # Samoilla arvoilla ei valiteta.
    assert classify_thresholds(rows, thresholds())["full_equip_min"] == 4000


def test_the_report_records_the_thresholds_the_rounds_were_classified_with() -> None:
    report = report_for([classified_row("Nuke_vs_a", 1)])
    assert report.classify_thresholds["force_buy_min"] == 1500
    # thresholds_used on TÄMÄN ajon asetukset, ei sama asia.
    assert "thresholds" in report.thresholds_used


def test_utility_rows_are_ordered_by_the_clock_not_the_alphabet() -> None:
    """Aakkosissa "10-20" tulisi ennen "5-10"."""
    events = (
        event_rows("d", 1, 0, "smoke", t_s=1.0)
        + event_rows("d", 1, 1, "smoke", t_s=7.0)
        + event_rows("d", 1, 2, "smoke", t_s=15.0)
        + event_rows("d", 1, 3, "smoke", t_s=30.0)
    )
    uses = utility_uses(events, [("d", 1)], [5.0, 10.0, 20.0])
    assert [u.seconds_bucket for u in uses] == ["0-5", "5-10", "10-20", "20+"]


# --- Joukkueen ja pelaajien nimet (Story 2.6) -----------------------------------


def test_a_team_without_a_clan_name_has_no_name_at_all() -> None:
    """Puuttuva nimi on ``None``, ei tunniste eikä tyhjä merkkijono."""
    identity = team_identity(
        [
            lineup_row("d", "p1", clan_name=None),
            lineup_row("d", "p2", clan_name=None),
        ]
    )
    assert identity.display_name is None
    assert identity.alternatives == []


def test_an_empty_string_is_not_a_name() -> None:
    """Vanhalla versiolla kirjoitettu taulu voi sisältää tyhjän merkkijonon."""
    identity = team_identity([lineup_row("d", "p1", clan_name="   ")])
    assert identity.display_name is None


def test_the_same_clan_in_every_demo_is_the_teams_name() -> None:
    identity = team_identity(
        [
            lineup_row(demo, f"p{i}", clan_name="MatureMayhem")
            for demo in ("a", "b", "c", "d")
            for i in range(5)
        ]
    )
    assert identity.display_name == "MatureMayhem"
    assert identity.alternatives == []


def test_conflicting_names_keep_the_most_observed_and_list_the_rest() -> None:
    """Ristiriita ei katoa: useimmin havaittu naytetaan, muut luetellaan."""
    rows = [
        lineup_row(demo, f"p{i}", clan_name="MatureMayhem")
        for demo in ("a", "b", "c")
        for i in range(5)
    ] + [lineup_row("d", f"p{i}", clan_name="MM Academy") for i in range(5)]

    identity = team_identity(rows)
    assert identity.display_name == "MatureMayhem"
    assert identity.alternatives == ["MM Academy"]


def test_the_vote_is_per_demo_not_per_row() -> None:
    """Viiden pelaajan demo ei saa äänestää viidesti.

    Ristiriita syntyy siitä, että kaksi *demoa* antaa eri nimen.
    Rivipohjainen laskenta antaisi viisinkertaisen painon demolle, jossa
    sattui olemaan viisi pelaajaa.
    """
    rows = [lineup_row("a", f"p{i}", clan_name="Aakkoset") for i in range(5)] + [
        lineup_row("b", "p9", clan_name="Bee"),
        lineup_row("c", "p9", clan_name="Bee"),
    ]
    identity = team_identity(rows)
    assert identity.display_name == "Bee"
    assert identity.alternatives == ["Aakkoset"]


def test_one_player_cannot_outvote_his_own_team_inside_a_demo() -> None:
    """Demon sisällä ratkaisee **enemmistö**, ei "yksi ääni per havaittu nimi".

    Neljä pelaajaa kantaa klaania ``Zulu``, yksi klaania ``Alfa``. Ääni per
    havaittu nimi antaisi molemmille yhden, yhden demon otannalla se on
    tasatilanne, ja aakkosjärjestys nostaisi otsikkoon nimen jonka yksi ainoa
    pelaaja kantoi -- eikä siitä jäisi mitään jälkeä raporttiin.

    Vähemmistö ei myöskään ole *demojen välinen* ristiriita, joten se ei kuulu
    vaihtoehtoisiin nimiin: se on demon sisäinen havainto, ja parsinnan
    ``lineup_clan_conflicts`` kertoo siitä.
    """
    rows = [
        lineup_row("d", f"p{i}", clan_name="Zulu" if i < 4 else "Alfa")
        for i in range(5)
    ]
    identity = team_identity(rows)
    assert identity.display_name == "Zulu"
    assert identity.alternatives == []


def test_a_demos_majority_is_one_vote_no_matter_how_many_players_carry_it() -> None:
    """Kaksi demoa, kaksi ääntä -- vaikka toisessa on viisi pelaajaa."""
    rows = [
        lineup_row("iso", f"p{i}", clan_name="Zulu") for i in range(5)
    ] + [lineup_row("pieni", "p9", clan_name="Alfa")]

    identity = team_identity(rows)
    # Tasatilanne demojen yli -> aakkoset.
    assert identity.display_name == "Alfa"
    assert identity.alternatives == ["Zulu"]


def test_a_row_without_a_map_demo_id_is_refused() -> None:
    """Tunnisteeton rivi sulauttaisi kaikki demot yhdeksi ääneksi."""
    with pytest.raises(AggregateError, match="map_demo_id"):
        team_identity([lineup_row("", "p1", clan_name="Zulu")])


def test_a_tie_is_resolved_alphabetically_so_the_run_repeats() -> None:
    """Ilman aakkosjärjestystä tulos riippuisi tiedostojen
    lukujärjestyksestä."""
    rows = [
        lineup_row("b", "p1", clan_name="Zulu"),
        lineup_row("a", "p1", clan_name="Alfa"),
    ]
    assert team_identity(rows).display_name == "Alfa"
    assert team_identity(list(reversed(rows))).display_name == "Alfa"


def test_the_player_name_is_the_most_observed_one() -> None:
    rows = [
        lineup_row("a", "p1", player_name="Laetikko"),
        lineup_row("b", "p1", player_name="Laetikko"),
        lineup_row("c", "p1", player_name="tertseli"),
    ]
    assert team_identity(rows).names["p1"] == "Laetikko"


def test_the_roster_keeps_a_player_whose_name_was_never_read() -> None:
    """Hiljaa pudotettu pelaaja kutistaisi rosterin kertomatta siitä."""
    entries = roster_entries(["p2", "p1"], {"p1": "Sassiz"})
    assert [e.player_id for e in entries] == ["p1", "p2"]
    assert entries[0].display_name == "Sassiz"
    assert entries[1].display_name is None


# --- Kuolemat ja tapot (Story 2.7) ---------------------------------------------


KEYS = [("Ancient_vs_x", 1), ("Ancient_vs_x", 2)]


def test_the_first_death_of_a_round_is_the_earliest_one() -> None:
    """Ensimmäinen kuolema on pienimmän ``t_s``:n rivi, ei taulun ensimmäinen.

    Rivit annetaan tarkoituksella väärässä järjestyksessä: jos funktio
    ottaisi ensimmäisen osuman, se poimisi Longin.
    """
    report = deaths_for(
        [
            death_row("Ancient_vs_x", 1, victim="p2", victim_area="Long", t_s=40.0),
            death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=12.0),
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("Cave", 1)]
    assert report.first_death_seconds_median == 12.0


def test_the_first_death_distribution_sums_to_its_own_sample() -> None:
    """``Σ n = m``, ja ``m`` on kierroksia joilla joukkue menetti pelaajan."""
    report = deaths_for(
        [
            death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave"),
            death_row("Ancient_vs_x", 2, victim="p2", victim_area="Long"),
        ],
        KEYS,
        [TEAM],
    )
    assert report.m == 2
    assert sum(a.n for a in report.first_death_areas) == report.m
    assert {a.m for a in report.first_death_areas} == {2}


def test_rounds_without_an_own_death_are_counted_apart() -> None:
    """Kierros ilman omaa kuolemaa ei ole nollarivi vaan ``rounds_missing``.

    Nollarivi väittäisi havainnoksi sen, ettei havaintoa ole -- ja
    rikkoisi ``Σ n = m``:n.
    """
    report = deaths_for(
        [death_row("Ancient_vs_x", 1, victim="p1")], KEYS, [TEAM]
    )
    assert report.m == 1
    assert report.rounds_missing == 1


def test_the_median_is_measured_from_the_rounds_not_the_rows() -> None:
    """Mediaani lasketaan kierroksen ensimmäisistä kuolemista.

    Muut saman kierroksen kuolemat eivät saa painaa: viisi kaatunutta
    pelaajaa yhdellä kierroksella siirtäisi mediaanin loppupäähän.
    """
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1", t_s=10.0),
        death_row("Ancient_vs_x", 1, victim="p2", t_s=60.0),
        death_row("Ancient_vs_x", 1, victim="p3", t_s=61.0),
        death_row("Ancient_vs_x", 2, victim="p1", t_s=20.0),
    ]
    report = deaths_for(rows, KEYS, [TEAM])
    assert report.first_death_seconds_median == 15.0


def test_a_tie_on_the_same_tick_is_broken_by_the_victim_id() -> None:
    """Kaksi joukkuekaveria samalla hetkellä: valinta ei saa riippua
    rivijärjestyksestä."""
    rows = [
        death_row("Ancient_vs_x", 1, victim="p9", victim_area="Long", t_s=12.0),
        death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=12.0),
    ]
    forward = deaths_for(rows, KEYS[:1], [TEAM])
    backward = deaths_for(list(reversed(rows)), KEYS[:1], [TEAM])
    assert [a.area for a in forward.first_death_areas] == ["Cave"]
    assert forward == backward


def test_a_death_without_a_time_is_last_not_first() -> None:
    """Puuttuva aika ei ole nolla.

    Ilman erottelua tyhjä ``t_s`` järjestyisi ennen kaikkia mitattuja ja
    väittäisi olevansa kierroksen ensimmäinen kuolema.
    """
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=30.0),
        death_row("Ancient_vs_x", 1, victim="p2", victim_area="Long", t_s=None),
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert [a.area for a in report.first_death_areas] == ["Cave"]
    assert report.first_death_seconds_median == 30.0


def test_a_round_whose_only_death_has_no_time_still_counts() -> None:
    """Aika puuttuu, havainto ei: alue on silti kierroksen ensimmäinen."""
    report = deaths_for(
        [death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=None)],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [a.area for a in report.first_death_areas] == ["Cave"]
    assert report.first_death_seconds_median is None


def test_kills_are_counted_from_the_attackers_lineup_and_area() -> None:
    """Tappo on **ampujan** havainto: alue on se, mistä hän ampui."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="o1",
            victim_lineup=OPPONENT,
            victim_side="CT",
            victim_area="BombsiteA",
            attacker="p1",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle",
        ),
        death_row(
            "Ancient_vs_x",
            2,
            victim="o2",
            victim_lineup=OPPONENT,
            victim_side="CT",
            victim_area="BombsiteA",
            attacker="p2",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle",
        ),
    ]
    report = deaths_for(rows, KEYS, [TEAM])
    assert report.kills_total == 2
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 2)]
    # Omia kuolemia ei ollut: nämä ovat vastustajan kuolemia.
    assert report.m == 0
    assert report.rounds_missing == 2


def test_the_kill_sample_counts_kills_not_rounds() -> None:
    """Tappoja voi olla enemmän kuin kierroksia -- ``Σ n = kills_total``."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim=f"o{i}",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker=f"p{i}",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle" if i < 3 else "BombsiteB",
        )
        for i in range(4)
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.kills_total == 4
    assert sum(k.n for k in report.kills) == 4
    assert {k.m for k in report.kills} == {4}
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 3), ("BombsiteB", 1)]


def test_a_kill_without_an_area_gets_its_own_bucket() -> None:
    """Tuntematon alue ei putoa: se on eri asia kuin tappojen puuttuminen."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="o1",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker="p1",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area=None,
        )
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert [(k.area, k.n) for k in report.kills] == [(None, 1)]


def test_a_teamkill_is_both_an_own_death_and_an_own_kill() -> None:
    """Kummankaan suodattaminen olisi tulkintaa.

    Havainto on, että pelaaja kuoli ja että ampuja oli tietyllä alueella.
    """
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="p1",
            victim_lineup=TEAM,
            victim_area="Cave",
            attacker="p2",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area="Middle",
        )
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("Cave", 1)]
    assert report.kills_total == 1
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 1)]


def test_a_death_between_two_opponents_is_neither() -> None:
    """Vastustajien keskinäinen kuolema ei kuulu tähän raporttiin."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim="o1",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker="o2",
            attacker_lineup=OPPONENT,
            attacker_side="CT",
        )
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.m == 0
    assert report.kills_total == 0


def test_a_death_outside_the_branch_is_ignored() -> None:
    """Toisen kierrostyypin kierros ei saa vuotaa tähän otantaan."""
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1"),
        death_row("Ancient_vs_x", 9, victim="p1"),
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert report.m == 1


def test_an_unnumbered_death_row_is_ignored() -> None:
    """``round_no`` tyhjänä tarkoittaa kierrosta, jota ei pelattu."""
    row = death_row("Ancient_vs_x", 1, victim="p1")
    row["round_no"] = None
    report = deaths_for([row], KEYS, [TEAM])
    assert report.m == 0


def test_several_lineups_of_the_same_team_all_count_as_ours() -> None:
    """Yksi vaihto tuottaa uuden kokoonpanotunnisteen; molemmat ovat meitä."""
    other = "cccccccccccccccc"
    rows = [
        death_row("Ancient_vs_x", 1, victim="p1", victim_lineup=TEAM),
        death_row("Ancient_vs_x", 2, victim="p1", victim_lineup=other),
    ]
    report = deaths_for(rows, KEYS, [TEAM, other])
    assert report.m == 2


def test_the_death_report_reaches_the_round_type_branch() -> None:
    """Reunajakauma on rakenteessa siellä, missä raportti sen lukee."""
    report = report_for(
        [classified_row("Ancient_vs_x", 1)],
        [tick_row("Ancient_vs_x", 1, "p1", "BombsiteA")],
        deaths=[
            death_row("Ancient_vs_x", 1, victim="p1", victim_area="Cave", t_s=24.0),
            death_row(
                "Ancient_vs_x",
                1,
                victim="o1",
                victim_lineup=OPPONENT,
                victim_side="CT",
                attacker="p2",
                attacker_lineup=TEAM,
                attacker_side="T",
                attacker_area="Middle",
                t_s=30.0,
            ),
        ],
    )
    entry = branch(report, "de_ancient", "T", "pistol")
    assert entry.deaths.m == 1
    assert entry.deaths.first_death_seconds_median == 24.0
    assert [(a.area, a.n) for a in entry.deaths.first_death_areas] == [("Cave", 1)]
    assert [(k.area, k.n) for k in entry.deaths.kills] == [("Middle", 1)]


def test_an_attackerless_own_death_is_counted_as_a_death_and_nothing_else() -> None:
    """Pommiin tai putoamiseen kuollut oma pelaaja on oma kuolema.

    Parse kohtelee ampujatonta kuolemaa ensiluokkaisena tapauksena, mutta
    aggregointiin asti se ei kulkenut yhdenkään testin läpi -- ja siellä
    ``attacker_lineup_key`` on ``null``, mikä on eri asia kuin "ei meidän".
    Ilman tätä tapausta pommiin kuolleet omat pelaajat voisivat pudota
    raportista ilman että mikään kaatuu.
    """
    report = deaths_for(
        [
            death_row(
                "Ancient_vs_x",
                1,
                victim="p1",
                victim_area="BombsiteB",
                attacker=None,
                t_s=95.0,
            )
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("BombsiteB", 1)]
    # Ampujaa ei ole, joten tappoa ei ole -- eikä null-kokoonpano saa osua
    # omien tappojen suodattimeen.
    assert report.kills_total == 0
    assert report.kills == []


def test_a_suicide_is_a_death_but_not_a_kill() -> None:
    """Itsemurhan alue on paikka, josta kukaan ei ampunut.

    Rivi läpäisisi molemmat haarat, koska ampuja on omassa kokoonpanossa.
    Tappoihin laskettuna se kasvattaisi ``kills_total``ia ja lisäisi
    "mistä he ampuvat" -riville sijainnin, jota ei ole olemassa.
    Aineistossa on 0 itsemurhaa 591 kuolemasta, joten vika olisi latentti.
    """
    report = deaths_for(
        [
            death_row(
                "Ancient_vs_x",
                1,
                victim="p1",
                victim_area="Cave",
                attacker="p1",
                attacker_lineup=TEAM,
                attacker_side="T",
                attacker_area="Cave",
            )
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.m == 1
    assert [(a.area, a.n) for a in report.first_death_areas] == [("Cave", 1)]
    assert report.kills_total == 0


def test_a_teamkill_is_still_a_kill_beside_the_suicide_rule() -> None:
    """Vartijan toinen haara: joukkuekaveri **oikeasti ampui** tuolta alueelta.

    Ilman tätä itsemurhasääntö voisi olla kirjoitettu kokoonpanon eikä
    pelaajan mukaan, ja teamkill katoaisi tapoista sen mukana.
    """
    report = deaths_for(
        [
            death_row(
                "Ancient_vs_x",
                1,
                victim="p1",
                attacker="p2",
                attacker_lineup=TEAM,
                attacker_side="T",
                attacker_area="Middle",
            )
        ],
        KEYS[:1],
        [TEAM],
    )
    assert report.kills_total == 1
    assert [(k.area, k.n) for k in report.kills] == [("Middle", 1)]


def test_an_empty_area_string_is_the_same_observation_as_a_missing_one() -> None:
    """Tyhjä merkkijono ei ole alue.

    Ilman normalisointia sama havainto tulisi jakaumaan kahdesti: mallin
    kaksoiskappaletarkistus vertaa raaka-arvoja (``""`` ja ``None`` ovat eri),
    mutta raportti näyttää molemmat nimellä "tuntematon alue" -- eli yksi
    rivi kertoisi saman asian kaksi kertaa eri luvuilla.
    """
    report = deaths_for(
        [
            death_row("Ancient_vs_x", 1, victim="p1", victim_area=""),
            death_row("Ancient_vs_x", 2, victim="p2", victim_area=None),
        ],
        KEYS,
        [TEAM],
    )
    assert [(a.area, a.n) for a in report.first_death_areas] == [(None, 2)]


def test_an_empty_kill_area_string_collapses_too() -> None:
    """Sama sääntö tappojen puolella; eri rivi koodissa, eri testi tässä."""
    rows = [
        death_row(
            "Ancient_vs_x",
            1,
            victim=f"o{i}",
            victim_lineup=OPPONENT,
            victim_side="CT",
            attacker=f"p{i}",
            attacker_lineup=TEAM,
            attacker_side="T",
            attacker_area=area,
        )
        for i, area in enumerate(("", None))
    ]
    report = deaths_for(rows, KEYS[:1], [TEAM])
    assert [(k.area, k.n) for k in report.kills] == [(None, 2)]
