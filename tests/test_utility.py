"""``domain.utility`` -- lentoratojen pelkistys ja räjähdysalue pistepilvestä.

Jokainen funktio on puhdas, joten jokainen I/O-matriisin rivi on täällä yhden
kutsun päässä ilman demotiedostoa. Radat rakennetaan käsin, ja ne
jäljittelevät oikean demon rakennetta: kranaatilla on rivejä myös pelaajan
repussa (koordinaatit tyhjiä), ja ``grenade_entity_id`` kierrätetään.

Pistepilvi rakennetaan samalla tavalla käsin: muutama havainto riittää
todistamaan moodivalinnan, tasatilanteen ratkaisun, pystypainon ja kynnyksen,
eikä yksikään niistä vaadi miljoonaa riviä.
"""

from __future__ import annotations

import polars as pl
import pytest

from pappascout.constants import EVENT_KINDS
from pappascout.domain.utility import (
    CLOUD_CELL_COLUMNS,
    CLOUD_OBSERVATION_COLUMNS,
    DETONATE,
    ENDPOINT_COLUMNS,
    MAX_TRAJECTORY_GAP_SECONDS,
    NEAREST_CHUNK_POINTS,
    NEAREST_POINT_COLUMNS,
    NEAREST_RESULT_COLUMNS,
    THROWN,
    TRAJECTORY_COLUMNS,
    build_point_cloud,
    empty_point_cloud,
    grenade_endpoints,
    nearest_cells,
    trajectory_gap_ticks,
)

#: Pistepilven ruudun särmä näissä testeissä. Sama kuin ``settings.toml``in
#: ``callout_grid_units``, jotta testien luvut mittaavat tuotannon ruudukkoa.
GRID = 32

#: Havaintotaulun tyypit. Eksplisiittisesti, koska yksikin pelkkiä
#: ``None``-arvoja sisältävä sarake saisi muuten ``Null``-tyypin -- ja
#: suodatin, jota testataan, kaatuisi eri syystä kuin testi väittää.
OBSERVATION_SCHEMA: dict[str, object] = {
    "x": pl.Float64,
    "y": pl.Float64,
    "z": pl.Float64,
    "area": pl.Utf8,
    "is_alive": pl.Boolean,
}

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
    rows: list[dict[str, object]] = []
    for tick in in_bag or []:
        rows.append(
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
        rows.append(
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
    return rows


def frame(*grenades: list[dict[str, object]]) -> pl.DataFrame:
    rows = [row for grenade in grenades for row in grenade]
    return pl.DataFrame(rows, schema=dict(TRAJECTORY_SCHEMA), orient="row")


def endpoints(frame_in: pl.DataFrame, *, gap: int = GAP):
    """``grenade_endpoints`` tavallisen 64-tickisen demon aukolla."""
    return grenade_endpoints(frame_in, max_gap_ticks=gap)


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
    result, dropped = endpoints(
        frame(trajectory(7, "aaa", "smoke", [100, 101, 102, 103]))
    )

    assert dropped == 0
    assert tuple(result.columns) == ENDPOINT_COLUMNS
    assert result.height == 2
    assert result["event_kind"].to_list() == [THROWN, DETONATE]
    assert result["tick"].to_list() == [100, 103]
    assert result["x"].to_list() == [0.0, 30.0]
    assert result["thrower_id"].unique().to_list() == ["aaa"]
    assert result["grenade_entity_id"].unique().to_list() == [7]


def test_the_whole_trajectory_collapses_to_the_two_endpoints() -> None:
    """1,55 miljoonaa riviä ei saa kulkea eteenpäin -- kaksi riittää."""
    long_flight = trajectory(3, "aaa", "smoke", list(range(1000, 3000)))
    result, _ = endpoints(frame(long_flight))
    assert result.height == 2
    assert result["tick"].to_list() == [1000, 2999]


def test_a_single_point_trajectory_gets_no_invented_detonation() -> None:
    """I/O-matriisi: rata katkeaa -> vain ``grenade_thrown``.

    Keksitty räjähdys samaan pisteeseen väittäisi savua siellä, missä sitä ei
    ollut.
    """
    result, dropped = endpoints(frame(trajectory(9, "aaa", "he", [500])))
    assert dropped == 0
    assert result.height == 1
    assert result["event_kind"].to_list() == [THROWN]


def test_rows_without_coordinates_are_not_a_trajectory() -> None:
    """Repussa oleva kranaatti ei ole heitto.

    Oikeassa demossa 1,34 miljoonaa riviä 1,55:stä on tällaisia. Ilman tätä
    suodatusta heittopaikaksi tulisi tyhjä koordinaatti minuutteja ennen
    varsinaista heittoa.
    """
    result, dropped = endpoints(
        frame(
            trajectory(
                4, "aaa", "smoke", [200, 201, 202], in_bag=[100, 120, 150, 199]
            )
        )
    )
    assert dropped == 0
    assert result["tick"].to_list() == [200, 202]


def test_a_grenade_never_thrown_produces_nothing() -> None:
    """Pelkkiä reppurivejä -> ei kranaattia, ei ohitusta."""
    result, dropped = endpoints(
        frame(trajectory(4, "aaa", "smoke", [], in_bag=[100, 101, 102]))
    )
    assert result.is_empty()
    assert dropped == 0


def test_a_reused_entity_id_is_two_grenades() -> None:
    """Peli kierrättää tunnisteet -- ryhmittely tunnisteen mukaan yhdistäisi ne.

    Oikeassa demossa Ancientin 374 lentorataa mahtuu 187 tunnisteeseen. Jos
    nämä yhdistyisivät, heitto olisi ensimmäisestä kierroksesta ja "räjähdys"
    toisesta, eri pelaajalta ja eri kartan puolelta.
    """
    result, _ = endpoints(
        frame(
            trajectory(5, "aaa", "smoke", [100, 101, 102]),
            trajectory(5, "bbb", "he", [5000, 5001, 5002], start=(900.0, 0.0, 0.0)),
        )
    )
    assert result.height == 4
    assert result["grenade_no"].to_list() == [0, 0, 1, 1]
    assert result["thrower_id"].to_list() == ["aaa", "aaa", "bbb", "bbb"]
    assert result["tick"].to_list() == [100, 102, 5000, 5002]


def test_the_same_entity_and_thrower_split_on_a_tick_gap() -> None:
    """Sama tunniste, sama heittäjä, sama tyyppi -- vain aukko erottaa."""
    result, _ = endpoints(
        frame(
            trajectory(5, "aaa", "smoke", [100, 101, 102]),
            trajectory(5, "aaa", "smoke", [4000, 4001], start=(900.0, 0.0, 0.0)),
        )
    )
    assert result.height == 4
    assert result["tick"].to_list() == [100, 102, 4000, 4001]


def test_a_small_hole_in_the_trajectory_does_not_invent_a_grenade() -> None:
    """Yksi hukkuva tick ei saa katkaista rataa kahdeksi kranaatiksi.

    Keksitty rivi on pahempi virhe kuin kadonnut: se lisäisi heiton, jota ei
    tapahtunut.
    """
    ticks = [100, 101, 102 + GAP - 1]
    result, _ = endpoints(frame(trajectory(5, "aaa", "smoke", ticks)))
    assert result.height == 2
    assert result["tick"].to_list() == [100, ticks[-1]]


def test_a_trajectory_without_a_thrower_is_dropped_and_counted() -> None:
    """I/O-matriisi: rata ilman heittoa -> ohitetaan, määrä raportoidaan."""
    result, dropped = endpoints(
        frame(
            trajectory(1, None, "smoke", [100, 101]),
            trajectory(2, "aaa", "smoke", [300, 301]),
        )
    )
    assert dropped == 1
    assert result.height == 2
    assert result["thrower_id"].unique().to_list() == ["aaa"]


def test_grenades_are_numbered_in_throw_order() -> None:
    """``grenade_no`` on parin ainoa luotettava avain, ja se seuraa aikaa."""
    result, _ = endpoints(
        frame(
            trajectory(50, "aaa", "smoke", [900, 901]),
            trajectory(9, "bbb", "he", [100, 101]),
        )
    )
    assert result["tick"].to_list() == [100, 101, 900, 901]
    assert result["grenade_no"].to_list() == [0, 0, 1, 1]


def test_three_trajectories_on_one_id_get_three_numbers() -> None:
    """I/O-matriisi: tunniste toistuu kierroksella -> kolme lentorataa.

    Mitattu ``inferno_vs_ryhmarama``sta: kierroksella 11 tunniste 564 kantaa
    kolme rataa -- molotov, flashbang ja incendiary. Jaksotus erottaa ne
    oikein, mutta pari ``(round_no, grenade_entity_id)`` ei -- siksi
    jokaisella on oma ``grenade_no``, ja pelin oma tunniste on kaikilla sama.
    Tyypit ovat samat kuin oikeassa demossa; ajat on tiivistetty testin
    tickeiksi.
    """
    result, _ = endpoints(
        frame(
            trajectory(564, "aaa", "molotov", [500, 504]),
            trajectory(564, "aaa", "flashbang", [800, 806]),
            trajectory(564, "bbb", "incendiary", [1200, 1206]),
        )
    )

    throws = result.filter(pl.col("event_kind") == THROWN)
    assert throws.height == 3
    assert throws["grenade_no"].n_unique() == 3
    assert throws["grenade_entity_id"].unique().to_list() == [564]
    # Ajat ja tyypit säilyvät sellaisinaan -- tunniste ei muuta havaintoa.
    assert throws["tick"].to_list() == [500, 800, 1200]
    assert throws["grenade_type"].to_list() == [
        "molotov",
        "flashbang",
        "incendiary",
    ]


def test_the_number_is_unique_over_the_whole_result() -> None:
    """Yksikäsitteisyys on demonlaajuinen, ei kierroskohtainen.

    Kierroskohtainen juokseva numero näyttäisi tässä yhtä hyvältä, mutta
    pettäisi heti kun aggregointi liittää kahden kierroksen utilityn samaan
    kehykseen. Siksi väite on koko taulusta.
    """
    result, _ = endpoints(
        frame(
            trajectory(1, "aaa", "smoke", [100, 104]),
            trajectory(1, "bbb", "smoke", [900, 904]),
            trajectory(2, "aaa", "he", [140, 144]),
            trajectory(2, "ccc", "flashbang", [950]),
        )
    )

    pairs = result.select("grenade_no", "event_kind")
    assert pairs.height == pairs.unique().height
    assert result["grenade_no"].n_unique() == 4


def test_the_throw_and_its_detonation_share_the_number() -> None:
    """I/O-matriisi: heitto ja räjähdys -- numero on niiden ainoa side."""
    result, _ = endpoints(frame(trajectory(7, "aaa", "smoke", [100, 104, 108])))

    assert result["event_kind"].to_list() == [THROWN, DETONATE]
    assert result["grenade_no"].n_unique() == 1


def test_an_unexploded_grenade_gets_a_number_of_its_own() -> None:
    """I/O-matriisi: yhden pisteen rata -> vain heitto, mutta oma numero."""
    result, _ = endpoints(
        frame(
            trajectory(1, "aaa", "smoke", [100]),
            trajectory(2, "bbb", "he", [200, 204]),
        )
    )

    lone = result.filter(pl.col("grenade_entity_id") == 1)
    assert lone["event_kind"].to_list() == [THROWN]
    other = result.filter(pl.col("grenade_entity_id") == 2)
    assert lone["grenade_no"][0] not in other["grenade_no"].to_list()


def test_the_same_input_gives_the_same_numbers() -> None:
    """I/O-matriisi: sama demo uudelleen -> samat tunnisteet.

    Vakaus ei ole mukavuus vaan ehto: jos numerot vaihtuisivat ajojen välillä,
    arkiston uudelleenparsinta näyttäisi muutokselta ilman muutosta. Syöte
    sekoitetaan, koska saman funktion toistaminen samalla syötteellä ei
    todistaisi vakaudesta mitään.
    """
    rows = frame(
        trajectory(3, "aaa", "smoke", [900, 906]),
        trajectory(3, "aaa", "he", [100, 106]),
        trajectory(8, "bbb", "flashbang", [400, 402]),
    )
    first, _ = endpoints(rows)
    for seed in range(5):
        shuffled, _ = endpoints(rows.sample(fraction=1.0, shuffle=True, seed=seed))
        assert first.equals(shuffled), seed


def test_two_rows_on_the_same_tick_do_not_make_the_result_undefined() -> None:
    """Jaksotus ei saa riippua siitä, missä järjestyksessä rivit tulivat.

    Jaksoraja luetaan viereisistä riveistä, ja Polarsin lajittelu ei ole
    vakaa. Jos avain olisi pelkkä ``(tunniste, tick)``, kaksi riviä samalla
    tunnisteella ja samalla tickillä voisivat vaihtaa paikkaa ajojen välillä
    -- ja silloin **jaksotus itse**, ei vain numerointi, olisi määräämätön.

    Tässä sama tunniste kantaa kahta eri tyyppiä samoilla tickeillä, mikä on
    pahin tapaus: tyyppi on jaksotuksen avain, joten rivien järjestys päättää
    missä jakso katkeaa.
    """
    rows = frame(
        trajectory(9, "aaa", "smoke", [100, 102], start=(0.0, 0.0, 0.0)),
        trajectory(9, "aaa", "he", [100, 102], start=(50.0, 0.0, 0.0)),
    )
    first, _ = endpoints(rows)
    for seed in range(8):
        shuffled, _ = endpoints(rows.sample(fraction=1.0, shuffle=True, seed=seed))
        assert first.equals(shuffled), seed


def test_two_trajectories_at_the_same_moment_stay_apart() -> None:
    """Tasapeli ajassa ei sekoita ratoja keskenään.

    Kaksi kranaattia voi lähteä samalta tickiltä (kaksi pelaajaa heittää yhtä
    aikaa). Numeron on erotettava ne, ja radan molemmat rivit on pysyttävä
    saman numeron alla -- muuten heitto ja räjähdys menisivät ristiin.
    """
    result, _ = endpoints(
        frame(
            trajectory(11, "aaa", "smoke", [300, 306]),
            trajectory(12, "bbb", "smoke", [300, 306]),
        )
    )

    assert result["grenade_no"].n_unique() == 2
    for _, pair in result.group_by("grenade_no", maintain_order=True):
        assert pair["event_kind"].to_list() == [THROWN, DETONATE]
        assert pair["grenade_entity_id"].n_unique() == 1


def test_an_empty_table_gives_an_empty_result_with_the_right_types() -> None:
    """I/O-matriisi: demo ilman utilityä -> tyhjä tulos, ei kaatumista."""
    result, dropped = endpoints(pl.DataFrame(schema=dict(TRAJECTORY_SCHEMA)))
    assert result.is_empty()
    assert dropped == 0
    assert tuple(result.columns) == ENDPOINT_COLUMNS


def test_a_missing_column_is_an_error_not_an_empty_result() -> None:
    """Tyhjä tulos näyttäisi demolta, jossa ei heitetty yhtään kranaattia."""
    broken = frame(trajectory(1, "aaa", "smoke", [10, 11])).drop("thrower_id")
    with pytest.raises(ValueError) as exc:
        endpoints(broken)
    assert "thrower_id" in str(exc.value)


def test_non_finite_coordinates_are_not_a_trajectory_point() -> None:
    """NaN-koordinaatti ei ole havainto, vaikka se ei olekaan null."""
    rows = trajectory(1, "aaa", "smoke", [10, 11, 12])
    rows[0]["x"] = float("nan")
    result, _ = endpoints(frame(rows))
    assert result["tick"].to_list() == [11, 12]


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
    result, _ = endpoints(
        frame(trajectory(5, "aaa", "smoke", ticks)),
        gap=trajectory_gap_ticks(128.0),
    )
    assert result.height == 2
    # Samalla aukolla 64 tickin demossa se olisi eri kranaatti: kaksi
    # pistettä ja sitten yksinäinen kolmas, eli heitto + räjähdys + keksitty
    # kolmas heitto.
    split_result, _ = endpoints(
        frame(trajectory(5, "aaa", "smoke", ticks)),
        gap=trajectory_gap_ticks(64.0),
    )
    assert split_result["grenade_no"].n_unique() == 2
    assert split_result.filter(pl.col("event_kind") == THROWN).height == 2


# --- Puuttuvat arvot -----------------------------------------------------------


def test_a_row_without_a_grenade_type_is_not_a_trajectory_point() -> None:
    """Tyhjä tyyppi kaataisi koko demon ``EVENTS``-validoinnissa.

    ``grenade_type`` on sopimuksessa pakollinen, joten yksi rikkinäinen rivi
    estäisi 233 MB:n demon parsinnan kokonaan.
    """
    rows = trajectory(1, "aaa", "smoke", [10, 11, 12])
    rows[0]["grenade_type"] = None
    result, _ = endpoints(frame(rows))
    assert result["tick"].to_list() == [11, 12]
    assert result["grenade_type"].null_count() == 0


def test_a_trajectory_of_only_null_types_disappears() -> None:
    rows = trajectory(1, "aaa", "smoke", [10, 11])
    for row in rows:
        row["grenade_type"] = None
    result, dropped = endpoints(frame(rows))
    assert result.is_empty()
    assert dropped == 0


# --- Pistepilven rakentaminen --------------------------------------------------


def observations(rows: list[dict[str, object]]) -> pl.DataFrame:
    """Havaintotaulu sopimuksen tyypeillä."""
    return pl.DataFrame(rows, schema=OBSERVATION_SCHEMA, orient="row")


def seen(
    x: float,
    y: float,
    z: float = 0.0,
    area: str | None = "BombsiteA",
    alive: bool = True,
) -> dict[str, object]:
    return {"x": x, "y": y, "z": z, "area": area, "is_alive": alive}


def test_the_cloud_is_a_grid_of_where_players_stood() -> None:
    """Kaksi havaintoa samassa ruudussa on yksi ruutu, kaukainen on toinen."""
    cloud = build_point_cloud(
        observations([seen(10.0, 10.0), seen(20.0, 12.0), seen(300.0, 300.0, area="Mid")]),
        grid_units=GRID,
    )
    assert cloud.select("cell_x", "cell_y", "cell_z").rows() == [(0, 0, 0), (9, 9, 0)]
    assert cloud["area"].to_list() == ["BombsiteA", "Mid"]
    assert cloud["observations"].to_list() == [2, 1]


def test_the_cell_area_is_the_mode_not_the_first_row() -> None:
    """Ruudun reunalla on aina rivejä naapurialueelta.

    Ensimmäinen rivi olisi kiinni siinä, missä järjestyksessä demoparser2
    tickit antoi -- eli sama demo voisi antaa eri alueen eri ajolla.
    """
    rows = [seen(1.0, 1.0, area="Reuna")] + [seen(2.0, 2.0, area="Keskus")] * 3
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Keskus"]
    # Havainnot ovat ruudun KAIKKI rivit, ei vain voittaneen alueen.
    assert cloud["observations"].to_list() == [4]


def test_a_tie_is_broken_by_the_area_name() -> None:
    """Tasatilanne ei saa jäädä lajittelun sattuman varaan."""
    rows = [seen(1.0, 1.0, area="Zulu"), seen(2.0, 2.0, area="Alfa")]
    assert build_point_cloud(observations(rows), grid_units=GRID)["area"].to_list() == [
        "Alfa"
    ]
    # Sama sisältö toisessa järjestyksessä antaa saman vastauksen.
    assert build_point_cloud(
        observations(list(reversed(rows))), grid_units=GRID
    )["area"].to_list() == ["Alfa"]


def test_a_dead_player_is_not_in_the_cloud() -> None:
    """Ruumis jää siihen mihin pelaaja kaatui; kuollut ei liiku kartalla."""
    rows = [seen(10.0, 10.0, area="Elossa"), seen(300.0, 300.0, area="Ruumis", alive=False)]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Elossa"]


def test_an_unnamed_area_is_not_in_the_cloud() -> None:
    """Ruutu nimeltä "ei nimeä" nimeäisi räjähdyksen tyhjäksi.

    Rivi näyttäisi silti osumalta -- alue null kynnyksen sisältä -- eikä
    lukija voisi erottaa sitä siitä, ettei aluetta saatu lainkaan.
    """
    rows = [seen(10.0, 10.0, area=None), seen(300.0, 300.0, area="Mid")]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Mid"]
    assert cloud["area"].null_count() == 0


def test_a_row_without_coordinates_is_not_in_the_cloud() -> None:
    rows = [
        {"x": None, "y": 1.0, "z": 0.0, "area": "Haamu", "is_alive": True},
        {"x": float("nan"), "y": 1.0, "z": 0.0, "area": "Haamu", "is_alive": True},
        seen(300.0, 300.0, area="Mid"),
    ]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Mid"]


def test_negative_coordinates_round_downwards() -> None:
    """CS-kartat ovat origon molemmin puolin, joten katkaisu olisi vika.

    Katkaisu nollaa kohti panisi -1 ja +1 samaan ruutuun, jolloin ruudukko
    olisi origon kohdalla kaksinkertainen ja kaksi eri aluetta sulautuisi.
    """
    cloud = build_point_cloud(
        observations([seen(-1.0, -1.0), seen(1.0, 1.0, area="Toinen")]),
        grid_units=GRID,
    )
    assert cloud.select("cell_x", "cell_y").rows() == [(-1, -1), (0, 0)]


def test_an_empty_cloud_still_has_the_contract_columns() -> None:
    """I/O-matriisi: tyhjä pistepilvi on kelvollinen tulos, ei virhe."""
    cloud = build_point_cloud(observations([]), grid_units=GRID)
    assert cloud.is_empty()
    assert cloud.columns == list(CLOUD_CELL_COLUMNS)
    assert cloud.schema == empty_point_cloud().schema


def test_a_cloud_of_only_dead_players_is_empty_not_broken() -> None:
    rows = [seen(10.0, 10.0, alive=False)]
    assert build_point_cloud(observations(rows), grid_units=GRID).is_empty()


def test_the_cloud_does_not_depend_on_the_row_order() -> None:
    """Hyväksymiskriteeri: sama demo kahdesti -> identtiset taulut.

    Demolla väite on heikko (deterministinen funktio samalla syötteellä).
    Tämä on sen vahva muoto: **sama sisältö eri järjestyksessä**. Jos
    moodivalinta nojaisi ryhmittelyn tai lajittelun vakauteen, ruudun alue
    voisi vaihtua ajojen välillä -- ja räjähdysalue sen mukana.
    """
    rows = [seen(1.0, 1.0, area="Alfa")] * 3 + [
        seen(2.0, 2.0, area="Beeta")
    ] * 3 + [seen(3.0, 3.0, area="Gamma"), seen(300.0, 300.0, area="Delta")]
    forwards = build_point_cloud(observations(rows), grid_units=GRID)
    # Kaksi eri sekoitusta, jotta yksikään ei ole "sama järjestys toisin päin".
    backwards = build_point_cloud(observations(rows[::-1]), grid_units=GRID)
    interleaved = build_point_cloud(
        observations(rows[1::2] + rows[0::2]), grid_units=GRID
    )
    assert forwards.equals(backwards)
    assert forwards.equals(interleaved)


@pytest.mark.parametrize("column", CLOUD_OBSERVATION_COLUMNS)
def test_a_missing_observation_column_is_named(column: str) -> None:
    """Ilman tarkistusta tulos olisi tyhjä pilvi -- eli demo, jossa kukaan ei
    liikkunut."""
    rows = observations([seen(1.0, 1.0)]).drop(column)
    with pytest.raises(ValueError, match=column):
        build_point_cloud(rows, grid_units=GRID)


@pytest.mark.parametrize("grid", [0, -32, float("nan"), float("inf")])
def test_an_impossible_grid_size_is_refused(grid: float) -> None:
    with pytest.raises(ValueError, match="Ruudun koko"):
        build_point_cloud(observations([seen(1.0, 1.0)]), grid_units=grid)


# --- Lähimmän ruudun haku ------------------------------------------------------


def points(rows: list[tuple[int, float | None, float | None, float | None]]):
    return pl.DataFrame(
        rows,
        schema={
            "point_id": pl.Int64,
            "x": pl.Float64,
            "y": pl.Float64,
            "z": pl.Float64,
        },
        orient="row",
    )


def two_area_cloud() -> pl.DataFrame:
    """Kaksi ruutua kaukana toisistaan, eri alueilla."""
    return build_point_cloud(
        observations([seen(16.0, 16.0, area="Alaosa"), seen(1000.0, 16.0, area="Ylaosa")]),
        grid_units=GRID,
    )


def nearest(pts, cloud, *, max_units=256.0, z_weight=2.0, z_tolerance=72.0):
    """Lähimmän ruudun haku testien oletusmitoilla.

    Paino on tässä **2 eikä tuotannon 1**, ja se on tarkoituksellista: nämä
    testit mittaavat painotuksen *mekaniikkaa* eivätkä tuotannon
    kokoonpanoa, ja kahden yksikön kerroin tekee käsin lasketuista
    odotusarvoista luettavia. Tuotannon arvon vartioi
    ``tests/test_settings.py``.
    """
    return nearest_cells(
        pts,
        cloud,
        grid_units=GRID,
        z_weight=z_weight,
        z_tolerance_units=z_tolerance,
        max_units=max_units,
    )


def test_the_nearest_cell_gives_the_area() -> None:
    result = nearest(points([(7, 20.0, 20.0, 0.0)]), two_area_cloud())
    assert result["area"].to_list() == ["Alaosa"]
    assert result["distance"][0] == pytest.approx(5.657, abs=0.01)


def test_the_point_id_comes_back_unchanged() -> None:
    """Avain on kutsujan oma (``grenade_no``); funktio ei tunne kranaatteja."""
    result = nearest(points([(41, 20.0, 20.0, 0.0), (7, 20.0, 20.0, 0.0)]), two_area_cloud())
    assert sorted(result["point_id"].to_list()) == [7, 41]


def test_a_point_beyond_the_threshold_keeps_its_distance() -> None:
    """I/O-matriisi: räjähdys kaukana -> ``area`` null, ``snap_distance`` tallessa.

    Etäisyys on se, mikä erottaa tämän tyhjästä pistepilvestä: molemmissa alue
    on null, mutta vain tässä tiedetään kuinka kaukaa se olisi otettu.
    """
    result = nearest(points([(1, 5000.0, 16.0, 0.0)]), two_area_cloud())
    assert result["area"].to_list() == [None]
    # Ruudun keskipiste on 1008 (ruutu 31), joten etaisyys on 3992.
    assert result["distance"][0] == pytest.approx(3992.0, abs=0.5)


def test_the_threshold_itself_still_counts() -> None:
    """Raja on ``<=`` eikä ``<``: 256 yksikön päässä oleva ruutu kelpaa."""
    cloud = build_point_cloud(observations([seen(16.0, 16.0, area="Mid")]), grid_units=GRID)
    exactly = nearest(points([(1, 16.0 + 256.0, 16.0, 0.0)]), cloud, max_units=256.0)
    assert exactly["area"].to_list() == ["Mid"]
    just_over = nearest(points([(1, 16.0 + 256.1, 16.0, 0.0)]), cloud, max_units=256.0)
    assert just_over["area"].to_list() == [None]


def test_an_empty_cloud_gives_neither_area_nor_distance() -> None:
    """I/O-matriisi: tyhjä pistepilvi -> kaikki räjähdysalueet null."""
    result = nearest(points([(1, 20.0, 20.0, 0.0)]), empty_point_cloud())
    assert result["area"].to_list() == [None]
    assert result["distance"].to_list() == [None]


def test_a_point_without_coordinates_gives_nothing() -> None:
    result = nearest(points([(1, None, 20.0, 0.0)]), two_area_cloud())
    assert result["area"].to_list() == [None]
    assert result["distance"].to_list() == [None]


def test_the_players_own_height_is_free() -> None:
    """Kranaatti räjähtää mistä tahansa lattian ja pään väliltä.

    Pystyrangaistus ilman toleranssia osuisi juuri normaaliin tapaukseen:
    savu ilmassa, molotov lattialla. Pelaajan korkeuden verran pystyeroa ei
    siis saa maksaa mitään.
    """
    cloud = build_point_cloud(observations([seen(16.0, 16.0, 16.0, "Mid")]), grid_units=GRID)
    # Ruudun keskipiste on z = 16; 72 yksikköä ylempänä ero on ilmainen.
    result = nearest(points([(1, 16.0, 16.0, 16.0 + 72.0)]), cloud)
    assert result["distance"][0] == pytest.approx(0.0, abs=0.01)
    assert result["area"].to_list() == ["Mid"]


def test_height_beyond_the_tolerance_is_weighted() -> None:
    """Kerroskartta: alakerran ruutu on ylhäältä katsoen aivan vieressä.

    Savu on tässä täsmälleen alakerran ruudun yläpuolella, 192 yksikköä
    ylempänä, ja yläkerran ruutu on 224 yksikön päässä samassa tasossa. Ilman
    painoa alakerta olisi lähempänä (192 < 224) ja savu saisi väärän
    kerroksen; painotettuna sen etäisyys on 2 * (192 - 72) = 240, eli
    yläkerta voittaa. Juuri tämä on painon koko tehtävä.
    """
    cloud = build_point_cloud(
        observations(
            [
                seen(16.0, 16.0, -180.0, "Alakerta"),
                seen(240.0, 16.0, 16.0, "Ylakerta"),
            ]
        ),
        grid_units=GRID,
    )
    smoke = points([(1, 16.0, 16.0, 16.0)])
    result = nearest(smoke, cloud, max_units=1000.0)
    assert result["area"].to_list() == ["Ylakerta"]
    assert result["distance"][0] == pytest.approx(224.0, abs=0.5)
    # Ilman painoa ja toleranssia (paino 1, toleranssi 0) alakerta olisi
    # lähempänä -- se on se virhe, jota vastaan paino on olemassa.
    unweighted = nearest(
        smoke, cloud, max_units=1000.0, z_weight=1.0, z_tolerance=0.0
    )
    assert unweighted["area"].to_list() == ["Alakerta"]
    assert unweighted["distance"][0] == pytest.approx(192.0, abs=0.5)


@pytest.mark.parametrize("limit", [None, float("nan"), float("inf")])
def test_a_threshold_that_is_not_a_number_gives_no_area(limit: float | None) -> None:
    """Kalibroimattoman asetuksen rehellinen arvo, ei rajan katoaminen.

    Lähin ruutu löytyy aina, joten kynnyksetön nimeäminen olisi väite eikä
    mittaus. Etäisyys mitataan silti -- se on aineisto kalibrointiin.
    """
    result = nearest(points([(1, 20.0, 20.0, 0.0)]), two_area_cloud(), max_units=limit)
    assert result["area"].to_list() == [None]
    assert result["distance"][0] == pytest.approx(5.657, abs=0.01)


def test_an_equal_distance_is_broken_by_the_area_name() -> None:
    """Kaksi yhtä kaukaista ruutua eri alueilla: sama demo, sama vastaus."""
    cloud = build_point_cloud(
        observations([seen(-16.0, 16.0, area="Zulu"), seen(48.0, 16.0, area="Alfa")]),
        grid_units=GRID,
    )
    result = nearest(points([(1, 16.0, 16.0, 0.0)]), cloud, max_units=1000.0)
    assert result["area"].to_list() == ["Alfa"]


def test_chunking_does_not_change_the_answer() -> None:
    """Palan koko on muistiraja, ei osa vastausta.

    Pisteitä on tässä enemmän kuin yhteen palaan mahtuu, joten sekä
    paloittelu että sen jälkeinen yhdistäminen tulevat ajetuiksi.
    """
    cloud = two_area_cloud()
    many = points(
        [(i, 20.0 if i % 2 else 1000.0, 20.0 if i % 2 else 16.0, 0.0)
         for i in range(NEAREST_CHUNK_POINTS * 2 + 3)]
    )
    result = nearest(many, cloud).sort("point_id")
    assert result.height == many.height
    odd = result.filter(pl.col("point_id") % 2 == 1)
    even = result.filter(pl.col("point_id") % 2 == 0)
    assert odd["area"].unique().to_list() == ["Alaosa"]
    assert even["area"].unique().to_list() == ["Ylaosa"]


def test_no_points_at_all_gives_an_empty_result() -> None:
    """Tyhjä syöte on kelvollinen: nolla kranaattia on nolla riviä.

    Varhaispaluu on olemassa, koska ristitulo tyhjällä puolella tuottaisi
    tyhjän kehyksen väärillä tyypeillä -- ja kutsuja liittäisi sen
    hiljaa tyhjäksi.
    """
    empty = points([])
    result = nearest(empty, two_area_cloud())
    assert result.is_empty()
    assert result.columns == list(NEAREST_RESULT_COLUMNS)


def test_a_duplicate_point_id_is_refused() -> None:
    """Avain, joka esiintyy kahdesti, **monistuisi** lopullisessa liitoksessa.

    Palat ryhmitellään erikseen ja yhdistetään, joten sama avain kahdessa
    palassa tuottaisi kaksi riviä ``best``iin ja sitä kautta neljä riviä
    tulokseen. Kutsuja saisi saman kranaatin useammin kuin kerran ilman että
    mikään kaatuisi.
    """
    doubled = points([(7, 20.0, 20.0, 0.0), (7, 30.0, 30.0, 0.0)])
    with pytest.raises(ValueError, match="point_id"):
        nearest(doubled, two_area_cloud())


def test_a_blank_area_is_not_an_area() -> None:
    """Pelkkä välilyönti ei ole aluenimi, vaikka se ei olekaan null.

    Sääntö on täällä eikä vain adapterissa: tämä funktio on julkinen, ja sen
    sopimus on "alueeton havainto ei päädy pilveen". Ruutu nimeltä ``" "``
    nimeäisi räjähdyksen tyhjäksi kynnyksen sisällä ja näyttäisi osumalta.
    """
    rows = [seen(10.0, 10.0, area=""), seen(12.0, 12.0, area="   "),
            seen(300.0, 300.0, area="Mid")]
    cloud = build_point_cloud(observations(rows), grid_units=GRID)
    assert cloud["area"].to_list() == ["Mid"]


@pytest.mark.parametrize("column", NEAREST_POINT_COLUMNS)
def test_a_missing_point_column_is_named(column: str) -> None:
    pts = points([(1, 20.0, 20.0, 0.0)]).drop(column)
    with pytest.raises(ValueError, match=column):
        nearest(pts, two_area_cloud())


@pytest.mark.parametrize("column", CLOUD_CELL_COLUMNS)
def test_a_missing_cloud_column_is_named(column: str) -> None:
    cloud = two_area_cloud().drop(column)
    with pytest.raises(ValueError, match=column):
        nearest(points([(1, 20.0, 20.0, 0.0)]), cloud)


@pytest.mark.parametrize("weight", [-1.0, float("nan"), float("inf")])
def test_an_impossible_z_weight_is_refused(weight: float) -> None:
    with pytest.raises(ValueError, match="z_weight"):
        nearest(points([(1, 20.0, 20.0, 0.0)]), two_area_cloud(), z_weight=weight)


@pytest.mark.parametrize("tolerance", [-1.0, float("nan"), float("inf")])
def test_an_impossible_z_tolerance_is_refused(tolerance: float) -> None:
    with pytest.raises(ValueError, match="z_tolerance_units"):
        nearest(
            points([(1, 20.0, 20.0, 0.0)]), two_area_cloud(), z_tolerance=tolerance
        )
