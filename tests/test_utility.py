"""``domain.utility`` -- lentoratojen pelkistys ja alueen johtaminen.

Molemmat funktiot ovat puhtaita, joten jokainen I/O-matriisin rivi on täällä
yhden kutsun päässä ilman demotiedostoa. Radat rakennetaan käsin, ja ne
jäljittelevät oikean demon rakennetta: kranaatilla on rivejä myös pelaajan
repussa (koordinaatit tyhjiä), ja ``grenade_entity_id`` kierrätetään.
"""

from __future__ import annotations

import polars as pl
import pytest

from pappascout.constants import EVENT_KINDS
from pappascout.domain.utility import (
    DETONATE,
    ENDPOINT_COLUMNS,
    MAX_TRAJECTORY_GAP_SECONDS,
    THROWN,
    TRAJECTORY_COLUMNS,
    PlayerPoint,
    grenade_endpoints,
    snap_area,
    trajectory_gap_ticks,
)

#: Feikkidemojen tickrate; radan sallittu aukko lasketaan siitä.
TICK_RATE = 64.0
GAP = trajectory_gap_ticks(TICK_RATE)

TRAJECTORY_SCHEMA: dict[str, object] = {
    "grenade_entity_id": pl.Int32,
    "grenade_type": pl.Utf8,
    "thrower_id": pl.Utf8,
    "tick": pl.Int32,
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
}


def trajectory(
    entity: int,
    thrower: str | None,
    grenade_type: str,
    ticks: list[int],
    *,
    start: tuple[float, float, float] = (0.0, 0.0, 0.0),
    step: tuple[float, float, float] = (10.0, 0.0, 0.0),
    in_bag: list[int] | None = None,
) -> list[dict[str, object]]:
    """Yhden kranaatin rivit: valinnainen reppuvaihe ja sitten lentorata.

    Args:
        entity: ``grenade_entity_id``.
        thrower: Heittäjä; ``None`` jäljittelee rataa ilman heittäjää.
        grenade_type: Tyyppi sellaisena kuin adapteri sen antaa.
        ticks: Lentoradan tickit.
        start: Radan ensimmäinen piste.
        step: Siirtymä tickiä kohden.
        in_bag: Tickit, joilla kranaatti on repussa (koordinaatit tyhjiä).
    """
    rivit: list[dict[str, object]] = []
    for tick in in_bag or []:
        rivit.append(
            {
                "grenade_entity_id": entity,
                "grenade_type": grenade_type,
                "thrower_id": thrower,
                "tick": tick,
                "x": None,
                "y": None,
                "z": None,
            }
        )
    for index, tick in enumerate(ticks):
        rivit.append(
            {
                "grenade_entity_id": entity,
                "grenade_type": grenade_type,
                "thrower_id": thrower,
                "tick": tick,
                "x": start[0] + step[0] * index,
                "y": start[1] + step[1] * index,
                "z": start[2] + step[2] * index,
            }
        )
    return rivit


def frame(*grenades: list[dict[str, object]]) -> pl.DataFrame:
    rivit = [rivi for grenade in grenades for rivi in grenade]
    return pl.DataFrame(rivit, schema=dict(TRAJECTORY_SCHEMA), orient="row")


def endpoints(kehys: pl.DataFrame, *, gap: int = GAP):
    """``grenade_endpoints`` tavallisen 64-tickisen demon aukolla."""
    return grenade_endpoints(kehys, max_gap_ticks=gap)


# --- Vakiot --------------------------------------------------------------------


def test_event_kinds_match_the_shared_enum() -> None:
    """Tapahtumalajit ovat sama luettelo kuin ``EVENTS``-skeemassa.

    Tarkistus on testissä eikä moduulitason assertissa: assert katoaisi
    ``python -O``:lla juuri silloin, kun sitä tarvittaisiin.
    """
    assert (THROWN, DETONATE) == EVENT_KINDS


def test_endpoint_columns_are_unique() -> None:
    assert len(ENDPOINT_COLUMNS) == len(set(ENDPOINT_COLUMNS))
    assert len(TRAJECTORY_COLUMNS) == len(set(TRAJECTORY_COLUMNS))


# --- grenade_endpoints ---------------------------------------------------------


def test_a_normal_grenade_becomes_two_rows() -> None:
    """I/O-matriisi: heitetty ja räjähtänyt savu -> heitto ja räjähdys."""
    tulos, ohitetut = endpoints(
        frame(trajectory(7, "aaa", "smoke", [100, 101, 102, 103]))
    )

    assert ohitetut == 0
    assert tuple(tulos.columns) == ENDPOINT_COLUMNS
    assert tulos.height == 2
    assert tulos["event_kind"].to_list() == [THROWN, DETONATE]
    assert tulos["tick"].to_list() == [100, 103]
    assert tulos["x"].to_list() == [0.0, 30.0]
    assert tulos["thrower_id"].unique().to_list() == ["aaa"]
    assert tulos["grenade_entity_id"].unique().to_list() == [7]


def test_the_whole_trajectory_collapses_to_the_two_endpoints() -> None:
    """1,55 miljoonaa riviä ei saa kulkea eteenpäin -- kaksi riittää."""
    pitka = trajectory(3, "aaa", "smoke", list(range(1000, 3000)))
    tulos, _ = endpoints(frame(pitka))
    assert tulos.height == 2
    assert tulos["tick"].to_list() == [1000, 2999]


def test_a_single_point_trajectory_gets_no_invented_detonation() -> None:
    """I/O-matriisi: rata katkeaa -> vain ``grenade_thrown``.

    Keksitty räjähdys samaan pisteeseen väittäisi savua siellä, missä sitä ei
    ollut.
    """
    tulos, ohitetut = endpoints(frame(trajectory(9, "aaa", "he", [500])))
    assert ohitetut == 0
    assert tulos.height == 1
    assert tulos["event_kind"].to_list() == [THROWN]


def test_rows_without_coordinates_are_not_a_trajectory() -> None:
    """Repussa oleva kranaatti ei ole heitto.

    Oikeassa demossa 1,34 miljoonaa riviä 1,55:stä on tällaisia. Ilman tätä
    suodatusta heittopaikaksi tulisi tyhjä koordinaatti minuutteja ennen
    varsinaista heittoa.
    """
    tulos, ohitetut = endpoints(
        frame(
            trajectory(
                4, "aaa", "smoke", [200, 201, 202], in_bag=[100, 120, 150, 199]
            )
        )
    )
    assert ohitetut == 0
    assert tulos["tick"].to_list() == [200, 202]


def test_a_grenade_never_thrown_produces_nothing() -> None:
    """Pelkkiä reppurivejä -> ei kranaattia, ei ohitusta."""
    tulos, ohitetut = endpoints(
        frame(trajectory(4, "aaa", "smoke", [], in_bag=[100, 101, 102]))
    )
    assert tulos.is_empty()
    assert ohitetut == 0


def test_a_reused_entity_id_is_two_grenades() -> None:
    """Peli kierrättää tunnisteet -- ryhmittely tunnisteen mukaan yhdistäisi ne.

    Oikeassa demossa Ancientin 374 lentorataa mahtuu 187 tunnisteeseen. Jos
    nämä yhdistyisivät, heitto olisi ensimmäisestä kierroksesta ja "räjähdys"
    toisesta, eri pelaajalta ja eri kartan puolelta.
    """
    tulos, _ = endpoints(
        frame(
            trajectory(5, "aaa", "smoke", [100, 101, 102]),
            trajectory(5, "bbb", "he", [5000, 5001, 5002], start=(900.0, 0.0, 0.0)),
        )
    )
    assert tulos.height == 4
    assert tulos["grenade_no"].to_list() == [0, 0, 1, 1]
    assert tulos["thrower_id"].to_list() == ["aaa", "aaa", "bbb", "bbb"]
    assert tulos["tick"].to_list() == [100, 102, 5000, 5002]


def test_the_same_entity_and_thrower_split_on_a_tick_gap() -> None:
    """Sama tunniste, sama heittäjä, sama tyyppi -- vain aukko erottaa."""
    tulos, _ = endpoints(
        frame(
            trajectory(5, "aaa", "smoke", [100, 101, 102]),
            trajectory(5, "aaa", "smoke", [4000, 4001], start=(900.0, 0.0, 0.0)),
        )
    )
    assert tulos.height == 4
    assert tulos["tick"].to_list() == [100, 102, 4000, 4001]


def test_a_small_hole_in_the_trajectory_does_not_invent_a_grenade() -> None:
    """Yksi hukkuva tick ei saa katkaista rataa kahdeksi kranaatiksi.

    Keksitty rivi on pahempi virhe kuin kadonnut: se lisäisi heiton, jota ei
    tapahtunut.
    """
    ticks = [100, 101, 102 + GAP - 1]
    tulos, _ = endpoints(frame(trajectory(5, "aaa", "smoke", ticks)))
    assert tulos.height == 2
    assert tulos["tick"].to_list() == [100, ticks[-1]]


def test_a_trajectory_without_a_thrower_is_dropped_and_counted() -> None:
    """I/O-matriisi: rata ilman heittoa -> ohitetaan, määrä raportoidaan."""
    tulos, ohitetut = endpoints(
        frame(
            trajectory(1, None, "smoke", [100, 101]),
            trajectory(2, "aaa", "smoke", [300, 301]),
        )
    )
    assert ohitetut == 1
    assert tulos.height == 2
    assert tulos["thrower_id"].unique().to_list() == ["aaa"]


def test_grenades_are_numbered_in_throw_order() -> None:
    """``grenade_no`` on parin ainoa luotettava avain, ja se seuraa aikaa."""
    tulos, _ = endpoints(
        frame(
            trajectory(50, "aaa", "smoke", [900, 901]),
            trajectory(9, "bbb", "he", [100, 101]),
        )
    )
    assert tulos["tick"].to_list() == [100, 101, 900, 901]
    assert tulos["grenade_no"].to_list() == [0, 0, 1, 1]


def test_an_empty_table_gives_an_empty_result_with_the_right_types() -> None:
    """I/O-matriisi: demo ilman utilityä -> tyhjä tulos, ei kaatumista."""
    tulos, ohitetut = endpoints(pl.DataFrame(schema=dict(TRAJECTORY_SCHEMA)))
    assert tulos.is_empty()
    assert ohitetut == 0
    assert tuple(tulos.columns) == ENDPOINT_COLUMNS


def test_a_missing_column_is_an_error_not_an_empty_result() -> None:
    """Tyhjä tulos näyttäisi demolta, jossa ei heitetty yhtään kranaattia."""
    rikki = frame(trajectory(1, "aaa", "smoke", [10, 11])).drop("thrower_id")
    with pytest.raises(ValueError) as exc:
        endpoints(rikki)
    assert "thrower_id" in str(exc.value)


def test_non_finite_coordinates_are_not_a_trajectory_point() -> None:
    """NaN-koordinaatti ei ole havainto, vaikka se ei olekaan null."""
    rivit = trajectory(1, "aaa", "smoke", [10, 11, 12])
    rivit[0]["x"] = float("nan")
    tulos, _ = endpoints(frame(rivit))
    assert tulos["tick"].to_list() == [11, 12]


# --- snap_area -----------------------------------------------------------------


def player(
    x: float, y: float, z: float, area: str | None, alive: bool = True
) -> PlayerPoint:
    return PlayerPoint(x=x, y=y, z=z, area=area, is_alive=alive)


def test_the_nearest_living_player_gives_the_area() -> None:
    pelaajat = [
        player(100.0, 0.0, 0.0, "Ramp"),
        player(10.0, 0.0, 0.0, "BombsiteA"),
    ]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area == "BombsiteA"


def test_a_player_beyond_the_limit_gives_nothing() -> None:
    """I/O-matriisi: räjähdys kaukana kaikista -> ``area = null``."""
    pelaajat = [player(1000.0, 0.0, 0.0, "Ramp")]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area is None


def test_the_limit_itself_still_counts() -> None:
    pelaajat = [player(500.0, 0.0, 0.0, "Ramp")]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area == "Ramp"


def test_a_dead_player_does_not_give_the_area() -> None:
    """Ruumis jää siihen mihin pelaaja kaatui eikä kerro utilityn kohteesta."""
    pelaajat = [
        player(10.0, 0.0, 0.0, "BombsiteA", alive=False),
        player(200.0, 0.0, 0.0, "Ramp"),
    ]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area == "Ramp"


def test_height_separates_the_floors_of_a_layered_map() -> None:
    """Nuken alakerran pelaaja on ylhäältä katsoen vieressä mutta eri alueella."""
    pelaajat = [player(0.0, 0.0, -400.0, "Vents")]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 300).area is None
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area == "Vents"


def test_the_second_nearest_area_is_never_tried() -> None:
    """Lähimmällä ei ole aluenimeä -> tyhjä, ei naapurin arvausta."""
    pelaajat = [
        player(10.0, 0.0, 0.0, None),
        player(20.0, 0.0, 0.0, "Ramp"),
    ]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area is None


def test_no_players_at_all_gives_nothing() -> None:
    assert snap_area(0.0, 0.0, 0.0, [], 500).area is None


def test_an_unset_limit_disables_snapping() -> None:
    """``area_snap_units = None`` on kalibroimattoman asetuksen rehellinen arvo."""
    pelaajat = [player(1.0, 0.0, 0.0, "Ramp")]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, None).area is None


def test_a_player_without_coordinates_is_ignored() -> None:
    pelaajat = [
        PlayerPoint(x=None, y=None, z=None, area="BombsiteA", is_alive=True),
        player(300.0, 0.0, 0.0, "Ramp"),
    ]
    assert snap_area(0.0, 0.0, 0.0, pelaajat, 500).area == "Ramp"


def test_a_target_without_coordinates_gives_nothing() -> None:
    assert snap_area(None, 0.0, 0.0, [player(1.0, 0.0, 0.0, "Ramp")], 500).area is None
    nan = float("nan")
    assert snap_area(nan, 0.0, 0.0, [player(1.0, 0.0, 0.0, "Ramp")], 500).area is None


# --- Aukon skaalaus tickratella ------------------------------------------------


def test_the_gap_is_the_same_moment_at_any_tick_rate() -> None:
    """Aukko on aikaa eikä tickejä.

    Kiinteä tickimäärä olisi 128-tickisessä demossa puolet lyhyempi hetki, ja
    sama lento voisi pilkkoutua kahdeksi kranaatiksi -- eli **keksiä
    ylimääräisen heiton** -- vain siksi että palvelin ajoi tiheämmin.
    """
    assert trajectory_gap_ticks(64.0) == 8
    assert trajectory_gap_ticks(128.0) == 16
    for tick_rate in (64.0, 128.0):
        assert trajectory_gap_ticks(tick_rate) / tick_rate == pytest.approx(
            MAX_TRAJECTORY_GAP_SECONDS
        )


def test_the_gap_is_never_zero() -> None:
    """Nolla tarkoittaisi, ettei aukkoa sallita -- yksi hukkuva tick riittäisi."""
    assert trajectory_gap_ticks(1.0) >= 1


@pytest.mark.parametrize("tick_rate", [0.0, -64.0, float("nan"), float("inf")])
def test_an_impossible_tick_rate_is_refused(tick_rate: float) -> None:
    with pytest.raises(ValueError, match="Tickrate"):
        trajectory_gap_ticks(tick_rate)


def test_a_128_tick_demo_keeps_a_trajectory_whole() -> None:
    """Sama aukko sekunneissa, eri tickeinä: rata ei saa katketa."""
    ticks = [1000, 1001, 1001 + trajectory_gap_ticks(128.0)]
    tulos, _ = endpoints(
        frame(trajectory(5, "aaa", "smoke", ticks)),
        gap=trajectory_gap_ticks(128.0),
    )
    assert tulos.height == 2
    # Samalla aukolla 64 tickin demossa se olisi eri kranaatti: kaksi
    # pistettä ja sitten yksinäinen kolmas, eli heitto + räjähdys + keksitty
    # kolmas heitto.
    pilkottu, _ = endpoints(
        frame(trajectory(5, "aaa", "smoke", ticks)),
        gap=trajectory_gap_ticks(64.0),
    )
    assert pilkottu["grenade_no"].n_unique() == 2
    assert pilkottu.filter(pl.col("event_kind") == THROWN).height == 2


# --- Puuttuvat arvot -----------------------------------------------------------


def test_a_row_without_a_grenade_type_is_not_a_trajectory_point() -> None:
    """Tyhjä tyyppi kaataisi koko demon ``EVENTS``-validoinnissa.

    ``grenade_type`` on sopimuksessa pakollinen, joten yksi rikkinäinen rivi
    estäisi 233 MB:n demon parsinnan kokonaan.
    """
    rivit = trajectory(1, "aaa", "smoke", [10, 11, 12])
    rivit[0]["grenade_type"] = None
    tulos, _ = endpoints(frame(rivit))
    assert tulos["tick"].to_list() == [11, 12]
    assert tulos["grenade_type"].null_count() == 0


def test_a_trajectory_of_only_null_types_disappears() -> None:
    rivit = trajectory(1, "aaa", "smoke", [10, 11])
    for rivi in rivit:
        rivi["grenade_type"] = None
    tulos, ohitetut = endpoints(frame(rivit))
    assert tulos.is_empty()
    assert ohitetut == 0


# --- Napsautusetäisyys ---------------------------------------------------------


def test_the_snap_distance_is_kept() -> None:
    """Kuluttajan on erotettava 40 yksikön osuma 490 yksikön arvauksesta."""
    osuma = snap_area(0.0, 0.0, 0.0, [player(300.0, 400.0, 0.0, "Ramp")], 500)
    assert osuma.area == "Ramp"
    assert osuma.distance == pytest.approx(500.0)


def test_a_snap_that_found_nobody_has_no_distance() -> None:
    """Rajan ulkopuolella napsautusta ei tehty, joten etäisyyttäkään ei ole."""
    osuma = snap_area(0.0, 0.0, 0.0, [player(600.0, 0.0, 0.0, "Ramp")], 500)
    assert osuma.area is None
    assert osuma.distance is None


def test_an_unnamed_nearest_player_keeps_the_distance() -> None:
    """Nämä kaksi tyhjää aluetta ovat eri asioita, ja etäisyys erottaa ne.

    "Kukaan ei ollut lähellä" on eri havainto kuin "lähin oli vieressä mutta
    pelillä ei ole nimeä hänen alueelleen". Ilman etäisyyttä kuluttaja ei voisi
    erottaa niitä.
    """
    osuma = snap_area(0.0, 0.0, 0.0, [player(10.0, 0.0, 0.0, None)], 500)
    assert osuma.area is None
    assert osuma.distance == pytest.approx(10.0)


@pytest.mark.parametrize("raja", [float("nan"), float("inf")])
def test_a_non_finite_limit_does_not_remove_the_limit(raja: float) -> None:
    """NaN-vertailu on aina epätosi, joten raja katoaisi huomaamatta."""
    kaukana = [player(100000.0, 0.0, 0.0, "Ramp")]
    assert snap_area(0.0, 0.0, 0.0, kaukana, raja).area is None
