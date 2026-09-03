"""``domain.aggregate`` -- aggregoinnin laskennan testit.

Kaikki taulut rakennetaan käsin, eikä yksikään testi tarvitse demoa tai
arkistoa: I/O-matriisin jokainen rivi on tässä tiedostossa omana testinään.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import polars as pl
import pytest

from conftest import OVERLAPPING_SITE_CLOUD, SITE_CLOUD
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
    observed_map_name,
    weakest_map_source,
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
from pappascout.domain.sampling import AreaObservations, CloudCell
from pappascout.domain.schemas import (
    ARMED_COLUMN,
    ARMORED_COLUMN,
    CALLOUT_CLOUD,
    CLASSIFIED,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
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


def match_frame(demo: str, map_name: str | None) -> pl.DataFrame:
    """Ottelutaulu: yksi rivi, kartan nimi otsikosta tai ``None``.

    Ei ``orient``ia: sanakirjarivi kertoo sarakkeensa nimellä.
    """
    df = pl.DataFrame(
        [{"map_demo_id": demo, "map_name": map_name}], schema=dict(MATCH)
    )
    return validate(df, MATCH, "match")


def callouts_frame(
    demo: str, cells: Sequence[tuple[str, int, int, int]] = ()
) -> pl.DataFrame:
    """Pistepilvi: rivi per ruutu ``(alue, cell_x, cell_y, cell_z)``.

    Oletus on **tyhjä pilvi**, ja se on tarkoituksellinen: siitä ei saada
    siteryhmiä, joten stack vaikenee ja jokainen vanha testi mittaa sitä,
    mitä se mittasi ennen Story 2.14:ää. Ryhmiä tarvitseva testi antaa ruudut
    itse.
    """
    df = pl.DataFrame(
        [
            {
                "map_demo_id": demo,
                "cell_x": x,
                "cell_y": y,
                "cell_z": z,
                "area": area,
                "observations": 1,
            }
            for area, x, y, z in cells
        ],
        schema=dict(CALLOUT_CLOUD),
    )
    return validate(df, CALLOUT_CLOUD, "callouts")


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
            "area_source": None if detonate_area is None else "point_cloud",
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
    map_names: dict[str, str | None] | None = None,
    area_orientation: dict[str, dict[str | None, AreaObservations]] | None = None,
    point_clouds: dict[str, list[CloudCell]] | None = None,
):
    """Raportti käsin rakennetuista riveistä.

    ``map_names`` **annettuna käytetään sellaisenaan**, myös tyhjänä. Ilman
    sitä oletus on, ettei yhdenkään demon otsikossa ollut kartan nimeä
    (``None`` jokaiselle), jolloin nimi päätellään tunnisteesta kuten ennen
    Story 2.11:tä -- niin vanhat testit mittaavat yhä päättelyä ja uudet
    havaintoa.

    Oletusta ei täytetä annetun kartan päälle: puuttuva avain on
    ``build_report``in mielestä virhe, ja juuri sitä vartijaa on voitava
    testata tämän apurin läpi.

    ``area_orientation`` toimii samoin, ja sen oletus on **tyhjä orientaatio
    joka demolle**: yksikään alue ei ylitä havaintokynnystä, joten
    poikkeamasäännöt vaikenevat. Se on tarkoituksellinen -- poikkeamat
    testataan omilla riveillään, ja jokainen muu testi mittaa sitä, mitä se
    mittasi ennen Story 2.5:tä. Tyhjä kartta on myös oikea vastaus: demo,
    jonka näytepisteissä ei ole nimettyjä alueita, ei anna orientaatiota
    millekään alueelle.

    ``point_clouds`` toimii samoin, ja sen oletus on **tyhjä pilvi joka
    demolle**: siitä ei saada siteryhmiä, joten stack vaikenee. Sama peruste
    kuin orientaatiolla -- stack testataan omilla riveillään, ja jokainen muu
    testi mittaa sitä, mitä se mittasi ennen Story 2.14:ää.
    """
    if map_names is None:
        map_names = {str(row["map_demo_id"]): None for row in classified}
    if area_orientation is None:
        area_orientation = {str(row["map_demo_id"]): {} for row in classified}
    if point_clouds is None:
        point_clouds = {str(row["map_demo_id"]): [] for row in classified}
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
        map_names=map_names,
        area_orientation=area_orientation,
        point_clouds=point_clouds,
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


# --- Kartan nimi otsikosta (Story 2.11) ---------------------------------------


def test_the_observed_map_name_wins_over_the_identifier() -> None:
    """FACEIT-tunnisteessa ei ole karttaa, mutta otsikossa on.

    Juuri tämä rivi I/O-matriisista on koko tarinan syy: ilman otsikkoa
    ``1-79f71e00-...`` jää omaksi haaraksi tunnisteensa nimellä.
    """
    assert map_name_for("1-79f71e00-1-1", MAP_POOL, "de_nuke") == (
        "de_nuke",
        "demo_header",
    )


def test_a_hand_imported_demo_keeps_its_name_but_changes_source() -> None:
    """Sama nimi kuin ennen, lähde vaihtuu havainnoksi."""
    assert map_name_for("Ancient_vs_kaljukostaja", MAP_POOL, "de_ancient") == (
        "de_ancient",
        "demo_header",
    )


def test_an_observed_name_outside_the_pool_is_used_as_is() -> None:
    """Poolin ulkopuolinen kartta on aito havainto eikä tuntematon kartta.

    ``de_train`` ei ole kauden poolissa, mutta demo on siltä kartalta. Nimen
    hiljainen korjaus poolin nimeksi olisi valhe, ja lähteen pudottaminen
    arvoon ``unknown`` väittäisi, ettei nimeä havaittu.
    """
    assert map_name_for("1-79f71e00-1-1", MAP_POOL, "de_train") == (
        "de_train",
        "demo_header",
    )


def test_the_observation_beats_a_conflicting_identifier() -> None:
    """Tunniste on tiedostonimi, otsikko on demon oma tieto.

    Ristiriidassa havainto voittaa: tiedostonimen voi kirjoittaa kuka vain,
    otsikon kirjoitti peli.
    """
    assert map_name_for("Ancient_vs_x", MAP_POOL, "de_nuke") == (
        "de_nuke",
        "demo_header",
    )


@pytest.mark.parametrize("observed", [None, "", "   "])
def test_without_an_observation_the_inference_still_applies(
    observed: str | None,
) -> None:
    """Tyhjä merkkijono ei ole nimi, joten päättely jää voimaan."""
    assert map_name_for("Ancient_vs_kaljukostaja", MAP_POOL, observed) == (
        "de_ancient",
        "map_demo_id",
    )
    assert map_name_for("1-a52ebff2-1-1", MAP_POOL, observed) == (
        "1-a52ebff2-1-1",
        "unknown",
    )


def test_two_faceit_demos_of_the_same_map_form_one_branch() -> None:
    """Epicin oma mittari: monidemo-otanta kuvion ja yksittäistapauksen erolle.

    Kaksi eri ``map_demo_id``:tä samalla havaitulla nimellä on **yksi** haara:
    kierrokset summautuvat ja ``map_demo_ids`` luettelee molemmat. Ilman
    otsikkoa nämä kaksi olisivat kaksi haaraa, joista jokainen rivi kantaisi
    merkintää "(1/1 kierroksesta)".
    """
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 1),
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 2),
        ],
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "ANCIENT_vs_RCAVE_VETERANS": "de_ancient",
        },
    )

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert entry.map_name_source == "demo_header"
    assert sorted(entry.map_demo_ids) == [
        "1-a52ebff2-1-1",
        "ANCIENT_vs_RCAVE_VETERANS",
    ]
    assert (entry.sample.demos, entry.sample.rounds) == (2, 3)


def test_a_demo_without_a_name_does_not_merge_into_another_branch() -> None:
    """Tuntematon kartta pysyy tunnisteenaan; arvausta ei tehdä.

    Havainto yhdeltä demolta ei kelpaa toisen nimeksi, vaikka ne olisivat
    samassa ajossa.
    """
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("1-79f71e00-1-1", 1),
        ],
        map_names={"1-a52ebff2-1-1": "de_ancient", "1-79f71e00-1-1": None},
    )

    branches = {m.map_name: m.map_name_source for m in report.maps}
    assert branches == {
        "de_ancient": "demo_header",
        "1-79f71e00-1-1": "unknown",
    }


# --- Haaran avain on nimi, lähde on heikoin (Story 2.11, katselmus 1) --------


def test_the_observed_and_the_inferred_name_form_one_branch() -> None:
    """Sama kartta kahdesta eri lähteestä on **yksi** haara.

    Tämä on se vika, jonka pari ``(nimi, lähde)`` avaimena tekisi: molemmat
    demot ovat ``de_ancient``, mutta toisen nimi tulee otsikosta ja toisen
    tiedostonimestä. Kahtena avaimena raportissa olisi kaksi
    ``de_ancient``-osiota, molemmat merkinnällä "(1/1 kierroksesta)" -- eli
    täsmälleen se pirstoutuminen, jonka tämä tarina poistaa, uudessa muodossa.

    Ennen Story 2.11:tä vikaa ei voinut olla: ``unknown``-haaran nimi on
    tunniste itse, joten se ei törmää oikeaan nimeen.
    """
    report = report_for(
        [
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 1),
            classified_row("Ancient_vs_kaljukostaja", 1),
            classified_row("Ancient_vs_kaljukostaja", 2),
        ],
        map_names={
            "ANCIENT_vs_RCAVE_VETERANS": "de_ancient",
            "Ancient_vs_kaljukostaja": None,
        },
    )

    assert len(report.maps) == 1
    entry = report.maps[0]
    assert entry.map_name == "de_ancient"
    assert (entry.sample.demos, entry.sample.rounds) == (2, 3)
    assert sorted(entry.map_demo_ids) == [
        "ANCIENT_vs_RCAVE_VETERANS",
        "Ancient_vs_kaljukostaja",
    ]
    # Haaran lähde on sen demojen HEIKOIN: yksi päätelty jäsen riittää.
    assert entry.map_name_source == "map_demo_id"


def test_a_branch_is_demo_header_only_when_every_demo_was_observed() -> None:
    """Toinen haara: kaikki havaittu = ``demo_header``.

    Ilman tätä väitettä edellinen testi menisi läpi myös toteutuksella, joka
    kirjoittaa aina ``map_demo_id``in.
    """
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("ANCIENT_vs_RCAVE_VETERANS", 1),
        ],
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "ANCIENT_vs_RCAVE_VETERANS": "de_ancient",
        },
    )

    assert [m.map_name_source for m in report.maps] == ["demo_header"]


@pytest.mark.parametrize(
    "sources,expected",
    [
        (["demo_header"], "demo_header"),
        (["map_demo_id"], "map_demo_id"),
        (["unknown"], "unknown"),
        (["demo_header", "map_demo_id"], "map_demo_id"),
        (["map_demo_id", "demo_header"], "map_demo_id"),
        (["demo_header", "unknown"], "unknown"),
        (["demo_header", "demo_header"], "demo_header"),
    ],
)
def test_the_branch_source_is_the_weakest_of_its_demos(
    sources: list[str], expected: str
) -> None:
    """Lähde vastaa kysymykseen "voinko luottaa tähän nimeen".

    Yksi päätelty jäsen riittää vastaamaan "ei täysin", ja vahvimman
    valitseminen olisi ylisanomista: haara näyttäisi kokonaan havaittuna,
    vaikka osa sen kierroksista on liitetty siihen tiedostonimen perusteella.
    Järjestys ei vaikuta tulokseen.
    """
    assert weakest_map_source(sources) == expected


def test_an_empty_source_list_is_an_error_not_a_default() -> None:
    """Tyhjä luettelo on rikkinäinen ryhmittely, ei oletusarvoinen lähde."""
    with pytest.raises(AggregateError, match="lähdeluettelo on tyhjä"):
        weakest_map_source([])


def test_an_unknown_source_is_refused() -> None:
    """Uusi lähde on lisättävä vahvuusjärjestykseen, ei vain malliin.

    Hiljaa palautettu oletus valehtelisi lukijalle nimen luotettavuudesta.
    """
    with pytest.raises(AggregateError, match="Tuntematon kartan nimen lähde"):
        weakest_map_source(["demo_header", "tiedostonimi"])


def test_a_missing_map_name_key_is_an_error_but_a_null_value_is_not() -> None:
    """``None`` on laillinen havainto; **puuttuva avain** on ohjelmointivirhe.

    Juuri sen estämiseksi ``build_report``in ``map_names`` tehtiin pakolliseksi.
    ``Mapping.get`` sotkisi nämä yhteen ja palauttaisi kartan hiljaa
    päättelyyn: FACEIT-demo saisi haaransa tunnisteestaan, eikä mikään
    kertoisi että havainto oli olemassa mutta ei löytänyt perille.
    """
    assert observed_map_name({"Nuke_vs_a": None}, "Nuke_vs_a") is None
    assert observed_map_name({"Nuke_vs_a": "de_nuke"}, "Nuke_vs_a") == "de_nuke"

    with pytest.raises(AggregateError, match="ei ole kartan nimien joukossa"):
        observed_map_name({"Nuke_vs_a": None}, "Anubis_vs_b")


def test_build_report_refuses_a_demo_that_is_not_in_the_name_map() -> None:
    """Sama vartija koko raportin läpi ajettuna."""
    with pytest.raises(AggregateError, match="ei ole kartan nimien joukossa"):
        report_for([classified_row("Nuke_vs_a", 1)], map_names={})


@pytest.mark.parametrize("padded", ["  de_ancient", "de_ancient  ", " de_ancient "])
def test_a_padded_observed_name_is_trimmed(padded: str) -> None:
    """Reunojen välilyönnit eivät saa jakaa karttaa kahdeksi haaraksi.

    Adapteri leikkaa nimen jo lukiessaan, mutta ``map_name_for`` on julkinen
    domain-funktio omalla sopimuksellaan eikä nojaa kutsujan siisteyteen.
    """
    assert map_name_for("1-a52ebff2-1-1", MAP_POOL, padded) == (
        "de_ancient",
        "demo_header",
    )


def test_a_padded_and_a_clean_name_are_the_same_branch() -> None:
    """Leikkauksen seuraus koko raportissa: yksi haara eikä kaksi."""
    report = report_for(
        [
            classified_row("1-a52ebff2-1-1", 1),
            classified_row("1-79f71e00-1-1", 1),
        ],
        map_names={
            "1-a52ebff2-1-1": "de_ancient",
            "1-79f71e00-1-1": " de_ancient ",
        },
    )

    assert [m.map_name for m in report.maps] == ["de_ancient"]
    assert report.maps[0].sample.demos == 2


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
    assert use.area_source == "point_cloud"
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
    # Kaksi kenttää on **jäljitettävyyttä eikä havaintoja**, ja molemmissa
    # esiintyy sana stack: thresholds_used on kynnysten kopio (kynnysnimet
    # stack_min_players, stack_group_margin, stack_site_separation_min) ja
    # anomaly_scan nimeää ajetut säännöt -- se on kattavuuden nimittäjä.
    # Tarkistus koskee havaintoja, joten molemmat nostetaan pois; alla
    # varmistetaan erikseen, ettei sana katoa siitä paikasta, johon se kuuluu.
    data = report.model_dump(mode="json")
    thresholds_used = data.pop("thresholds_used")
    scan = data.pop("anomaly_scan")
    assert "stack" in scan["rules"]
    assert "stack_min_players" in thresholds_used["thresholds"]
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
    assert use.area_source == "point_cloud"


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


# --- Poikkeamat (Story 2.5) -----------------------------------------------------
#
# Ryhmittely otannaksi on aggregoinnin työ, ei säännön: sääntö näkee yhden
# kierroksen kerrallaan. Testit rakentavat siksi kierroksia ja tarkistavat
# ``n/m``:n, kartan, puolen ja kierrostyypit -- eivät sitä, milloin sääntö
# osuu (se on ``test_sampling.py``).
#
# **Kynnykset kulkevat aina ``limits=``-parametrin kautta.** Se ei ole
# tyylivalinta: ilman sitä testit todistaisivat vain, että sääntö osuu
# oletusarvoilla, eikä yksikään väite kaatuisi jos kutsupaikka johdottaisi
# kynnykset väärin tai jättäisi ne lukematta. Juuri se on Story 1.8:n vika --
# säädetty settings.toml muuttaa parametrihashin muttei raporttia.

#: Alue, joka on demossa T:n hallussa: Ancientin B suora, T-osuus 0,88 (n 24).
ANOMALY_AREA = "TSideLower"


def t_side(demo: str, *, area: str = ANOMALY_AREA, t: int = 21, total: int = 24):
    """Orientaatiokartta yhdelle demolle: yksi alue T:n hallussa."""
    return {demo: {area: AreaObservations(t=t, total=total)}}


def advance_round(
    demo: str,
    round_no: int,
    *,
    area: str = ANOMALY_AREA,
    players: int = 1,
    seconds: float = 30.0,
) -> list[dict[str, object]]:
    """Näytepisterivit, jotka laukaisevat CT-etenemisen yhdellä kierroksella."""
    return [
        tick_row(
            demo,
            round_no,
            f"{TEAM}-p{i}",
            area,
            side="CT",
            sample_t_s=seconds,
        )
        for i in range(players)
    ]


def crunch_round(
    demo: str,
    round_no: int,
    *,
    area: str = ANOMALY_AREA,
    sources: tuple[str, ...] = ("SideEntrance", "TSideUpper"),
    seconds: float = 30.0,
) -> list[dict[str, object]]:
    """Pelaajat saapuvat alueelle annetuista suunnista -- yksi per suunta."""
    rows: list[dict[str, object]] = []
    for i, source in enumerate(sources):
        rows.append(
            tick_row(
                demo,
                round_no,
                f"{TEAM}-p{i}",
                source,
                side="CT",
                sample_t_s=seconds - 15.0,
            )
        )
        rows.append(
            tick_row(
                demo, round_no, f"{TEAM}-p{i}", area, side="CT", sample_t_s=seconds
            )
        )
    return rows


def eco_ct(demo: str, *rounds: int, round_type: str = "eco"):
    """Luokitellut CT-kierrokset annetulla tyypillä."""
    return [
        classified_row(demo, n, side="CT", round_type=round_type) for n in rounds
    ]


def stack_cloud(demo: str) -> dict[str, list[CloudCell]]:
    """Demokohtainen pistepilvi, josta siteryhmät saadaan."""
    return {demo: [CloudCell(a, x, y, z) for a, x, y, z in SITE_CLOUD]}


def stack_round(
    demo: str,
    round_no: int,
    *,
    site: str = "BombsiteB",
    others: tuple[str, ...] = ("SideEntrance", "Ramp"),
    elsewhere: tuple[str, ...] = ("BombsiteA",),
    seconds: float = 15.0,
) -> list[dict[str, object]]:
    """Neljä CT-pelaajaa saman siten ryhmässä, yksi sitellä itsellään.

    ``elsewhere`` on ryhmän ulkopuolella: se nostaa elossa olevien määrän
    viiteen ilman että se kasvattaisi ryhmän kokoa -- eli juuri se ero, jota
    ``4/5`` mittaa.
    """
    areas = (site, site, *others, *elsewhere)
    return [
        tick_row(
            demo,
            round_no,
            f"{TEAM}-p{i}",
            area,
            side="CT",
            sample_t_s=seconds,
        )
        for i, area in enumerate(areas)
    ]


def anomaly_report(
    classified: list[dict[str, object]],
    ticks: list[dict[str, object]],
    *,
    demo: str,
    limits: ThresholdSettings,
    orientation: dict | None = None,
    map_names: dict[str, str | None] | None = None,
    point_clouds: dict[str, list[CloudCell]] | None = None,
):
    """Raportti poikkeamatestille -- kynnykset **aina** nimettyinä.

    ``point_clouds`` oletuksena tyhjä pilvi: siteryhmiä ei saada, joten
    stack vaikenee eivätkä etenemistä ja crunchia koskevat testit mittaa
    vahingossa kolmatta sääntöä.
    """
    return report_for(
        classified,
        ticks,
        limits=limits,
        area_orientation=orientation if orientation is not None else t_side(demo),
        map_names=map_names,
        point_clouds=point_clouds,
    )


def areas_of(report, rule: str) -> list[str]:
    """Annetun säännön poikkeama-alueet raportista."""
    return [a.area for a in report.anomalies if a.rule == rule]


def test_an_anomaly_carries_its_map_side_round_type_and_sample() -> None:
    """Jokainen poikkeama kantaa otantansa sekä kartan, puolen ja tyypin."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert anomaly.rule == "ct_advance"
    assert anomaly.map_name == "de_ancient"
    assert anomaly.map_name_source == "map_demo_id"
    assert anomaly.side == "CT"
    assert anomaly.round_types == ["eco"]
    assert anomaly.area == ANOMALY_AREA
    assert (anomaly.n, anomaly.m) == (1, 3)
    assert [entry.round_no for entry in anomaly.rounds] == [1]
    assert anomaly.rounds[0].seconds == [30.0]
    assert anomaly.rounds[0].map_demo_id == demo
    assert anomaly.orientation[0].map_demo_id == demo
    assert anomaly.orientation[0].t_share == pytest.approx(0.875)
    assert anomaly.orientation[0].observations == 24


def test_the_round_numbers_are_carried_not_thrown_away() -> None:
    """Scoutin seuraava teko on avata se kierros demolta."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 2) + advance_round(demo, 3),
        demo=demo,
        limits=thresholds(),
    )
    anomaly = report.anomalies[0]
    assert [entry.round_no for entry in anomaly.rounds] == [2, 3]


def test_the_same_area_on_two_rounds_is_one_row_with_a_sample_of_two() -> None:
    """I/O-matriisin viimeinen rivi: yksi rivi otannalla 2/m, ei kaksi riviä."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 1) + advance_round(demo, 2, players=2),
        demo=demo,
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert (anomaly.n, anomaly.m) == (2, 3)
    assert anomaly.players_max == 2
    assert [entry.players_max for entry in anomaly.rounds] == [1, 2]


def test_two_sample_points_on_one_round_do_not_double_the_sample() -> None:
    """``n`` on kierroksia: sama alue 15 s ja 30 s kohdalla on yksi kierros."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2),
        advance_round(demo, 1, seconds=15.0) + advance_round(demo, 1, seconds=30.0),
        demo=demo,
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    assert report.anomalies[0].n == 1
    assert report.anomalies[0].rounds[0].seconds == [15.0, 30.0]


def test_a_crunch_round_produces_both_rows() -> None:
    """Säännöt jakavat orientaation; säästökierroksella molemmat osuvat."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        crunch_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    rules = [a.rule for a in report.anomalies]
    assert rules == ["ct_advance", "crunch"]
    crunch = report.anomalies[1]
    assert crunch.rounds[0].sources == ["SideEntrance", "TSideUpper"]
    assert crunch.players_max == 2


def test_an_empty_anomaly_list_is_a_valid_report() -> None:
    """Matriisin rivi 7: yksikään sääntö ei osu."""
    demo = "Nuke_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1, area="Outside"),
        demo=demo,
        # Nuken piha: T-osuus 0,70 eli kynnyksen alle.
        orientation={demo: {"Outside": AreaObservations(t=211, total=302)}},
        limits=thresholds(),
    )
    assert report.anomalies == []


def test_a_full_buy_round_gets_no_advance_but_can_get_a_crunch() -> None:
    """Matriisin rivi 8: eteneminen ei voi osua, crunch voi."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3, round_type="full"),
        crunch_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    assert [a.rule for a in report.anomalies] == ["crunch"]


# --- Speksimuutos 2: crunchia ei avainnella kierrostyypin mukaan ----------------


def test_a_crunch_on_two_round_types_is_one_row_over_the_whole_side() -> None:
    """Sama kuvio kahdella kierrostyypillä on yksi rivi, ei kaksi.

    Ilman tätä sama crunch hajoaisi eco-riviksi ja default-riviksi eri
    jakajilla, eikä kokonaismäärää voisi nähdä -- juuri se hajanaisuus, jonka
    poistamiseksi koko luku tehtiin.
    """
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2) + eco_ct(demo, 3, 4, round_type="full")
    report = anomaly_report(
        classified,
        crunch_round(demo, 1) + crunch_round(demo, 3),
        demo=demo,
        limits=thresholds(),
    )
    crunches = [a for a in report.anomalies if a.rule == "crunch"]
    assert len(crunches) == 1
    crunch = crunches[0]
    # Nimittäjä on puolen KAIKKI kierrokset, ei yhden kierrostyypin.
    assert (crunch.n, crunch.m) == (2, 4)
    assert crunch.round_types == ["eco", "full"]
    assert [entry.round_type for entry in crunch.rounds] == ["eco", "full"]


def test_the_advance_is_still_keyed_by_round_type() -> None:
    """Eteneminen on säästökierrosten ilmiö, joten tyyppi on osa havaintoa."""
    demo = "Ancient_vs_x"
    classified = (
        eco_ct(demo, 1, 2)
        + eco_ct(demo, 3, 4, round_type="force")
    )
    report = anomaly_report(
        classified,
        advance_round(demo, 1) + advance_round(demo, 3),
        demo=demo,
        limits=thresholds(),
    )
    advances = [a for a in report.anomalies if a.rule == "ct_advance"]
    assert [(a.round_types, a.n, a.m) for a in advances] == [
        (["eco"], 1, 2),
        (["force"], 1, 2),
    ]


def test_an_advance_row_never_carries_two_round_types() -> None:
    """Malli valvoo sen, mutta ryhmittelyn on tuotettava se oikein."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1) + eco_ct(demo, 2, round_type="force")
    report = anomaly_report(
        classified,
        advance_round(demo, 1) + advance_round(demo, 2),
        demo=demo,
        limits=thresholds(),
    )
    for anomaly in report.anomalies:
        if anomaly.rule == "ct_advance":
            assert len(anomaly.round_types) == 1


def test_the_crunch_denominator_is_the_side_not_the_round_type() -> None:
    """Kokonaismäärä on nähtävissä: 1/24 eikä 1/2 ja 0/22."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2) + eco_ct(
        demo, *range(3, 25), round_type="full"
    )
    report = anomaly_report(
        classified,
        crunch_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    crunch = next(a for a in report.anomalies if a.rule == "crunch")
    assert (crunch.n, crunch.m) == (1, 24)


# --- Kynnykset vaikuttavat raportin sisältöön ----------------------------------
#
# Jokainen kuudesta kynnyksestä todistetaan **kahteen suuntaan**: arvo, jolla
# rivi on, ja arvo, jolla se katoaa. Mutaatiotesti, jonka nämä pysäyttävät:
# kynnyksen johdottaminen väärin tai lukematta jättäminen.


def test_the_t_share_threshold_decides_whether_the_row_exists() -> None:
    """Nuken piha (0,70) on rajatapaus, josta koko kynnys on kalibroitu."""
    demo = "Nuke_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, area="Outside")
    # Nuken piha on **tasan** kynnyksellä: koko 0,80:n perustelu nojaa siihen,
    # että 0,70 päästäisi sen läpi. Luvut ovat siksi tasan 0,70 eivätkä
    # mitattu 211/302 (= 0,6987), joka pyöristyy 0,70:een muttei saavuta sitä
    # -- juuri se ero on syy kirjoittaa vertailu näkyviin.
    orientation = {demo: {"Outside": AreaObservations(t=210, total=300)}}
    strict = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_t_share=0.80),
    )
    loose = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_t_share=0.70),
    )
    assert areas_of(strict, "ct_advance") == []
    assert areas_of(loose, "ct_advance") == ["Outside"]


def test_the_observation_minimum_decides_whether_the_row_exists() -> None:
    """Ohut alue ei ole kummankaan puolen aluetta -- kynnys ratkaisee."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, area="Ramp")
    orientation = {demo: {"Ramp": AreaObservations(t=5, total=6)}}
    strict = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_area_min_observations=20),
    )
    loose = anomaly_report(
        classified, ticks, demo=demo, orientation=orientation,
        limits=thresholds(advance_area_min_observations=6),
    )
    assert areas_of(strict, "ct_advance") == []
    assert areas_of(loose, "ct_advance") == ["Ramp"]


def test_the_time_bound_decides_whether_the_row_exists() -> None:
    """45 s -osuma on rajauksen ulkopuolella, 30 s sisällä."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, seconds=45.0)
    strict = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_max_sample_s=30.0)
    )
    loose = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_max_sample_s=45.0)
    )
    assert areas_of(strict, "ct_advance") == []
    assert areas_of(loose, "ct_advance") == [ANOMALY_AREA]


def test_the_advance_player_minimum_decides_whether_the_row_exists() -> None:
    """Veeti valitsi 1; kahden vaatimus jättäisi neljä kuudesta osumasta pois."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = advance_round(demo, 1, players=1)
    one = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_min_players=1)
    )
    two = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(advance_min_players=2)
    )
    assert areas_of(one, "ct_advance") == [ANOMALY_AREA]
    assert areas_of(two, "ct_advance") == []


def test_the_crunch_player_minimum_decides_whether_the_row_exists() -> None:
    """Kolmen pelaajan vaatimus pudottaa kahden pelaajan crunchin."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = crunch_round(demo, 1)
    two = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(crunch_min_players=2)
    )
    three = anomaly_report(
        classified, ticks, demo=demo, limits=thresholds(crunch_min_players=3)
    )
    assert areas_of(two, "crunch") == [ANOMALY_AREA]
    assert areas_of(three, "crunch") == []


def test_the_crunch_source_minimum_decides_whether_the_row_exists() -> None:
    """Kolmen suunnan vaatimus pudottaa kahden suunnan crunchin."""
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    ticks = crunch_round(demo, 1)
    two = anomaly_report(
        classified,
        ticks,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=2),
    )
    three = anomaly_report(
        classified,
        ticks,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=3),
    )
    # Kaksi pelaajaa kahdesta suunnasta: crunch_min_players=3 pudottaa sen
    # kummallakin suuntavaatimuksella, joten suuntavaatimus todistetaan
    # kolmen pelaajan aineistolla alla.
    assert areas_of(two, "crunch") == []
    assert areas_of(three, "crunch") == []

    ticks3 = crunch_round(
        demo, 1, sources=("Alley", "BombsiteB", "LowerTunnel")
    )
    ok = anomaly_report(
        classified,
        ticks3,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=3),
    )
    tight = anomaly_report(
        classified,
        ticks3,
        demo=demo,
        limits=thresholds(crunch_min_players=4, crunch_min_sources=4),
    )
    assert areas_of(ok, "crunch") == [ANOMALY_AREA]
    assert areas_of(tight, "crunch") == []


def test_the_two_crunch_thresholds_are_not_interchangeable() -> None:
    """**Mutaatiovartija: kynnysten vaihtaminen keskenään kaataa tämän.**

    Aineisto on kolme pelaajaa kahdesta suunnasta, ja kynnykset ovat
    ``players=3, sources=2``. Oikein johdotettuna se osuu. Jos kutsupaikka
    vaihtaa kynnykset keskenään, ehdoksi tulee ``players>=2, sources>=3`` --
    ja kaksi suuntaa ei riitä kolmeen, joten rivi katoaa.

    Oletusarvoilla (2 ja 2) vaihto ei näy lainkaan, ja juuri siksi tämä testi
    käyttää eri arvoja.
    """
    demo = "Ancient_vs_x"
    classified = eco_ct(demo, 1, 2)
    # Kolme pelaajaa, kaksi suuntaa: p0 ja p1 samasta suunnasta.
    ticks = [
        tick_row(demo, 1, f"{TEAM}-p0", "SideEntrance", side="CT", sample_t_s=15.0),
        tick_row(demo, 1, f"{TEAM}-p1", "SideEntrance", side="CT", sample_t_s=15.0),
        tick_row(demo, 1, f"{TEAM}-p2", "TSideUpper", side="CT", sample_t_s=15.0),
    ] + [
        tick_row(demo, 1, f"{TEAM}-p{i}", ANOMALY_AREA, side="CT", sample_t_s=30.0)
        for i in range(3)
    ]
    report = anomaly_report(
        classified,
        ticks,
        demo=demo,
        limits=thresholds(crunch_min_players=3, crunch_min_sources=2),
    )
    crunch = next(a for a in report.anomalies if a.rule == "crunch")
    assert crunch.players_max == 3
    assert crunch.rounds[0].sources == ["SideEntrance", "TSideUpper"]


def test_the_small_sample_threshold_decides_the_mark_both_ways() -> None:
    """Sisarlipun sääntö: molemmat suunnat, ei vain ``True``.

    Mutaatio ``m <`` -> ``m <=`` merkitsisi jokaisen kolmen kierroksen haaran
    poikkeaman pieneksi otannaksi, eikä yksisuuntainen väite huomaisi sitä.
    """
    demo = "Ancient_vs_x"
    small = anomaly_report(
        eco_ct(demo, 1, 2),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(small_sample_rounds=3),
    )
    big = anomaly_report(
        eco_ct(demo, 1, 2, 3),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(small_sample_rounds=3),
    )
    assert small.anomalies[0].small_sample is True
    assert small.anomalies[0].m == 2
    assert big.anomalies[0].small_sample is False
    assert big.anomalies[0].m == 3


# --- Ryhmittely ja kattavuus ----------------------------------------------------


def test_two_demos_of_the_same_map_keep_both_orientations() -> None:
    """Kartta voi olla kahdesta demosta, ja niiden T-osuudet voivat erota."""
    first, second = "Ancient_vs_x", "Ancient_vs_y"
    report = anomaly_report(
        eco_ct(first, 1) + eco_ct(second, 1),
        advance_round(first, 1) + advance_round(second, 1),
        demo=first,
        orientation={
            first: {ANOMALY_AREA: AreaObservations(t=21, total=24)},
            second: {ANOMALY_AREA: AreaObservations(t=40, total=46)},
        },
        map_names={first: "de_ancient", second: "de_ancient"},
        limits=thresholds(),
    )
    assert len(report.anomalies) == 1
    anomaly = report.anomalies[0]
    assert (anomaly.n, anomaly.m) == (2, 2)
    assert [entry.map_demo_id for entry in anomaly.orientation] == [first, second]
    assert [entry.observations for entry in anomaly.orientation] == [24, 46]
    assert [entry.map_demo_id for entry in anomaly.rounds] == [first, second]


def test_anomalies_from_two_maps_stay_apart() -> None:
    """Poikkeama on kartan havainto; kaksi karttaa on kaksi riviä."""
    ancient, anubis = "Ancient_vs_x", "Anubis_vs_x"
    report = anomaly_report(
        eco_ct(ancient, 1) + eco_ct(anubis, 1),
        advance_round(ancient, 1) + advance_round(anubis, 1, area="Bridge"),
        demo=ancient,
        orientation={
            ancient: {ANOMALY_AREA: AreaObservations(t=21, total=24)},
            anubis: {"Bridge": AreaObservations(t=39, total=46)},
        },
        limits=thresholds(),
    )
    assert [(a.map_name, a.area) for a in report.anomalies] == [
        ("de_ancient", ANOMALY_AREA),
        ("de_anubis", "Bridge"),
    ]


def test_an_anomaly_denominator_matches_the_round_type_branch() -> None:
    """Etenemisen ``m`` on sama luku kuin vastaavan kierrostyypin otanta."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2, 3) + eco_ct(demo, 4, round_type="full"),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    eco = branch(report, "de_ancient", "CT", "eco")
    advance = next(a for a in report.anomalies if a.rule == "ct_advance")
    assert advance.m == eco.sample.rounds == 3


def test_a_demo_without_an_orientation_is_refused() -> None:
    """Puuttuva avain on eri asia kuin tyhjä orientaatio."""
    demo = "Ancient_vs_x"
    with pytest.raises(AggregateError, match="alueorientaatiota ei annettu"):
        anomaly_report(
            eco_ct(demo, 1),
            advance_round(demo, 1),
            demo=demo,
            orientation={"toinen-demo": {}},
            limits=thresholds(),
        )


def test_an_empty_orientation_silences_the_rules_and_is_recorded() -> None:
    """Tyhjä orientaatio on **sokea piste**, ja kattavuus sanoo sen ääneen."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1),
        demo=demo,
        orientation={demo: {}},
        limits=thresholds(),
    )
    assert report.anomalies == []
    assert report.anomaly_scan.demos_without_orientation == [demo]


def test_the_scan_says_what_was_run_and_on_what() -> None:
    """Tyhjä luku on havainto vain siitä, mitä tutkittiin."""
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2) + eco_ct(demo, 3, round_type="full"),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    scan = report.anomaly_scan
    assert scan.rules == ["ct_advance", "crunch", "stack"]
    assert scan.rules_deferred == []
    assert scan.rounds_scanned == 3
    # Kaikki kolme kierrosta ovat CT-puolen, ja niistä kaksi on ecoa:
    # crunch voi osua kolmella, eteneminen kahdella.
    assert scan.crunch_rounds == 3
    assert scan.advance_rounds == 2
    # Stack ei nähnyt yhtäkään: apurin oletuspilvi on tyhjä, joten
    # siteryhmiä ei saatu. **Juuri se ero on kattavuuden syy**: sama demo on
    # crunchin nimittäjässä kolmella kierroksella ja stackin nollalla.
    assert scan.stack_rounds == 0
    assert scan.demos_without_site_groups == [demo]
    assert scan.demos_without_orientation == []


def test_an_area_below_the_threshold_leaves_no_blind_spot() -> None:
    """Sokea piste on **orientaation puuttuminen**, ei osuman puuttuminen.

    Demo, jolla on T:n alue muttei osumaa, on mitattu negatiivinen -- juuri
    se, mitä Nuken nolla crunchia on. Se ei kuulu sokeiden listaan.
    """
    demo = "Nuke_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1, area="CTSpawn"),
        demo=demo,
        orientation={demo: {"Lobby": AreaObservations(t=57, total=64)}},
        limits=thresholds(),
    )
    assert report.anomalies == []
    assert report.anomaly_scan.demos_without_orientation == []


def test_the_map_name_source_is_carried_to_the_anomaly() -> None:
    """Tunnistamaton kartta on tunnistettava myös poikkeamarivillä."""
    demo = "1-79f71e00-1396-4f53-a0b4-782ee9742023-1-1"
    report = anomaly_report(
        eco_ct(demo, 1),
        advance_round(demo, 1),
        demo=demo,
        limits=thresholds(),
    )
    assert report.anomalies[0].map_name_source == "unknown"
    assert report.anomalies[0].map_name == demo


def test_a_null_side_on_a_sample_row_is_refused() -> None:
    """``str(None)`` päättäisi hiljaa, ettei rivi ole CT."""
    demo = "Ancient_vs_x"
    rows = advance_round(demo, 1)
    rows[0]["side"] = None
    with pytest.raises(AggregateError, match="puoli on None"):
        anomaly_report(
            eco_ct(demo, 1), rows, demo=demo, limits=thresholds()
        )


def test_a_null_is_alive_on_a_sample_row_is_refused() -> None:
    """``bool(None)`` päättäisi hiljaa, että pelaaja on kuollut."""
    demo = "Ancient_vs_x"
    rows = advance_round(demo, 1)
    rows[0]["is_alive"] = None
    with pytest.raises(AggregateError, match="elossaolo puuttuu"):
        anomaly_report(
            eco_ct(demo, 1), rows, demo=demo, limits=thresholds()
        )


def test_an_area_written_with_stray_whitespace_still_matches() -> None:
    """Orientaatio ja läsnäolo normalisoidaan samalla funktiolla.

    Ilman sitä ``" TSideLower "`` olisi orientaatiossa eri alue kuin
    läsnäolossa ja sääntö vaikenisi sillä alueella -- ilman että mikään
    kertoisi miksi.
    """
    demo = "Ancient_vs_x"
    rows = advance_round(demo, 1)
    rows[0]["area"] = f" {ANOMALY_AREA} "
    report = anomaly_report(
        eco_ct(demo, 1),
        rows,
        demo=demo,
        orientation={demo: {f"{ANOMALY_AREA} ": AreaObservations(t=21, total=24)}},
        limits=thresholds(),
    )
    assert areas_of(report, "ct_advance") == [ANOMALY_AREA]


def test_a_duplicated_sample_row_does_not_silence_the_crunch() -> None:
    """Kaksoisrivi pariutui aiemmin itsensä kanssa ja söi lähtöalueen."""
    demo = "Ancient_vs_x"
    rows = crunch_round(demo, 1)
    # Kaksinnetaan yksi kohdealueen rivi.
    rows.append(dict(rows[-1]))
    report = anomaly_report(
        eco_ct(demo, 1, 2), rows, demo=demo, limits=thresholds()
    )
    crunch = next(a for a in report.anomalies if a.rule == "crunch")
    assert crunch.rounds[0].sources == ["SideEntrance", "TSideUpper"]
    assert crunch.players_max == 2


# --- Stack (Story 2.14) ---------------------------------------------------------


def test_a_stack_anomaly_carries_the_site_its_group_and_the_survivors() -> None:
    """Rivi nimeää siten oman alueen, ryhmän ja elossa olleet.

    Nimittäjä on **puolen kaikki kierrokset**, kuten crunchilla: sääntö ei
    tunne kierrostyyppiä, ja jakaminen eco-riviksi ja default-riviksi antaisi
    samalle kuviolle kaksi eri jakajaa.
    """
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1, 2) + eco_ct(demo, 3, round_type="full"),
        stack_round(demo, 1),
        demo=demo,
        limits=thresholds(),
        point_clouds=stack_cloud(demo),
    )
    stacks = [a for a in report.anomalies if a.rule == "stack"]
    assert len(stacks) == 1
    stack = stacks[0]
    assert stack.area == "BombsiteB"
    assert stack.site == "B"
    assert stack.side == "CT"
    assert (stack.n, stack.m) == (1, 3)
    assert stack.rounds[0].players_max == 4
    assert [(p.sample_t_s, p.players, p.alive) for p in stack.rounds[0].points] == [
        (15.0, 4, 5)
    ]
    # Orientaatio on tyhjä: sääntö ei lue sitä, joten luku olisi keksitty.
    assert stack.orientation == []


def test_a_stack_spanning_round_types_is_one_row() -> None:
    """Kierrostyyppi on havainto rivillä, ei nimittäjä.

    Sama kuvio ecolla ja täydellä ostolla on **yksi rivi otannalla 2/3**,
    ja ``round_types`` kertoo millä tyypeillä se havaittiin.
    """
    demo = "Ancient_vs_x"
    report = anomaly_report(
        eco_ct(demo, 1) + eco_ct(demo, 2, 3, round_type="full"),
        stack_round(demo, 1) + stack_round(demo, 2, seconds=30.0),
        demo=demo,
        limits=thresholds(),
        point_clouds=stack_cloud(demo),
    )
    stack = next(a for a in report.anomalies if a.rule == "stack")
    assert stack.round_types == ["eco", "full"]
    assert (stack.n, stack.m) == (2, 3)
    assert [entry.round_no for entry in stack.rounds] == [1, 2]


def test_a_silenced_demo_is_in_the_coverage_and_not_in_the_denominator() -> None:
    """Vaiennettu demo on crunchin nimittäjässä muttei stackin.

    Juuri tämä ero on koko ``stack_rounds``-kentän syy: ilman sitä Nuken
    kierrokset näyttäisivät tutkituilta nollatuloksella.
    """
    speaks = "Ancient_vs_x"
    silent = "Nuke_vs_y"
    clouds = stack_cloud(speaks)
    # Siteet päällekkäin: erotus 2 ruutua, siteiden oma koko 20 + 20.
    clouds[silent] = [
        CloudCell(a, x, y, z) for a, x, y, z in OVERLAPPING_SITE_CLOUD
    ]
    report = anomaly_report(
        eco_ct(speaks, 1, 2) + eco_ct(silent, 1, 2, 3),
        stack_round(speaks, 1),
        demo=speaks,
        limits=thresholds(),
        orientation={speaks: {}, silent: {}},
        map_names={speaks: "de_ancient", silent: "de_nuke"},
        point_clouds=clouds,
    )
    scan = report.anomaly_scan
    assert scan.crunch_rounds == 5
    assert scan.stack_rounds == 2
    assert scan.demos_without_site_groups == [silent]
    assert [a.map_name for a in report.anomalies if a.rule == "stack"] == [
        "de_ancient"
    ]


def test_a_silenced_demo_is_not_in_the_stack_denominator() -> None:
    """Kartta kahdesta demosta, joista toinen vaikenee.

    Vaiennetun demon kierrokset ovat **crunchin** nimittäjässä mutta eivät
    stackin: sääntö ei nähnyt niitä. Ilman rajausta rivin ``n/m`` kertoisi
    eri kattavuudesta kuin luvun oma kattavuusteksti (``stack_rounds``), joka
    osaa jättää ne pois -- eli sama luku kahdella arvolla samassa raportissa.
    """
    speaks = "ANCIENT_vs_a"
    silent = "Ancient_vs_b"
    clouds = stack_cloud(speaks)
    clouds[silent] = [
        CloudCell(a, x, y, z) for a, x, y, z in OVERLAPPING_SITE_CLOUD
    ]
    report = anomaly_report(
        eco_ct(speaks, 1, 2) + eco_ct(silent, 1, 2, 3),
        stack_round(speaks, 1),
        demo=speaks,
        limits=thresholds(),
        orientation={speaks: {}, silent: {}},
        map_names={speaks: "de_ancient", silent: "de_ancient"},
        point_clouds=clouds,
    )
    # Yksi kartta, viisi CT-kierrosta -- mutta stack näki niistä kaksi.
    assert [m.map_name for m in report.maps] == ["de_ancient"]
    stack = next(a for a in report.anomalies if a.rule == "stack")
    assert (stack.n, stack.m) == (1, 2)
    assert report.anomaly_scan.crunch_rounds == 5
    assert report.anomaly_scan.stack_rounds == 2
    # Rivin nimittäjä ja kattavuusluku kertovat saman: molemmat 2, ei 5.
    assert stack.m == report.anomaly_scan.stack_rounds


def test_a_missing_point_cloud_is_refused_rather_than_assumed() -> None:
    """Puuttuva avain ei ole sama asia kuin pilvi, josta ryhmiä ei saatu.

    Hiljainen oletus vaientaisi säännön juuri sillä demolla, ja kattavuus
    kirjaisi sen kartan ominaisuutena -- vaikka kyse olisi kutsujan
    unohduksesta.
    """
    demo = "Ancient_vs_x"
    with pytest.raises(AggregateError, match="pistepilveä ei annettu"):
        anomaly_report(
            eco_ct(demo, 1),
            stack_round(demo, 1),
            demo=demo,
            limits=thresholds(),
            point_clouds={},
        )


def test_the_stack_threshold_is_read_from_the_settings() -> None:
    """Neljän pelaajan asetelma katoaa, kun kynnys nostetaan viiteen."""
    demo = "Ancient_vs_x"

    def stacks(min_players: int) -> list[str]:
        report = anomaly_report(
            eco_ct(demo, 1, 2),
            stack_round(demo, 1),
            demo=demo,
            limits=thresholds(stack_min_players=min_players),
            point_clouds=stack_cloud(demo),
        )
        return areas_of(report, "stack")

    assert stacks(4) == ["BombsiteB"]
    assert stacks(5) == []


def test_the_separation_threshold_is_a_setting_not_code() -> None:
    """Sama demo vaikenee tai puhuu sen mukaan, mikä kynnys on asetettu.

    Pilvenä on **päällekkäisten siteiden** pilvi, jonka suhde on 0,05 --
    Nuken kärjistys. Tuotannon kynnyksellä 2,0 se vaikenee; riittävän
    matalalla se puhuu, ja silloin sama asetelma tuottaa osuman. Suunta on
    tämä päin siksi, että erottuvan pilven suhde on 25 eikä yksikään mallin
    sallima kynnys (yläraja 20) vaientaisi sitä -- ja juuri se on yläraja
    hyvä uutinen: mitattujen karttojen vaientaminen vahingossa ei onnistu.
    """
    demo = "Nuke_vs_x"
    clouds = {
        demo: [CloudCell(a, x, y, z) for a, x, y, z in OVERLAPPING_SITE_CLOUD]
    }
    # ``House`` on tässä pilvessä B:n ryhmässä (10 vs 8 ruutua).
    rows = stack_round(
        demo, 1, others=("House", "House"), elsewhere=("BombsiteA",)
    )

    def stacks(separation_min: float) -> list[str]:
        report = anomaly_report(
            eco_ct(demo, 1, 2),
            rows,
            demo=demo,
            limits=thresholds(stack_site_separation_min=separation_min),
            point_clouds=clouds,
        )
        return areas_of(report, "stack")

    assert stacks(2.0) == []
    assert stacks(0.01) == ["BombsiteB"]
