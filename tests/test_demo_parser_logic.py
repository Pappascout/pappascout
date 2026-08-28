"""Demoadapterin logiikka ilman demoja.

``Demoparser2Rounds`` on kahdessa osassa: ohut kuori, joka kutsuu
demoparser2:ta, ja sen alla puhdas logiikka -- kierrosrajojen paritus,
kokoonpanojen tunnistus, pistemäärien mittauspisteet ja tickraten laskenta.
Juuri se logiikka voi mennä hiljaa väärin, ja juuri se on kallein testata
oikealla 233 MB:n demolla.

Siksi täällä on :class:`FakeDemoparser2`, joka palauttaa samanmuotoiset
pandas-kehykset kuin oikea kirjasto. Vain ``_open`` korvataan; kaikki muu
adapterissa ajetaan oikeasti. Nämä testit ajetaan aina, myös ``-m "not demo"``
-ajossa ja koneella, jolla demoja ei ole.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import polars as pl
import pytest

from pappascout.adapters import demo_parser as dp
from pappascout.adapters.decompress import DEMO_MAGIC
from pappascout.adapters.demo_parser import DEFAULT_TICK_RATE, Demoparser2Rounds
from pappascout.adapters.protocols import ROUNDS_ADAPTER_COLUMNS
from pappascout.domain.rounds import mark_played_rounds
from pappascout.errors import ParseError

A_PLAYERS = ["aaa1", "aaa2", "aaa3", "aaa4", "aaa5"]
B_PLAYERS = ["bbb1", "bbb2", "bbb3", "bbb4", "bbb5"]

_SIDE_TEAM = {"T": 2, "CT": 3}


# --- Feikki -------------------------------------------------------------------


class FakeDemoparser2:
    """Palauttaa samat kehysmuodot kuin demoparser2 0.42.0.

    ``drop_props`` jäljittelee tilannetta, jossa kirjasto on nimennyt kentän
    uudelleen eikä pyydettyä propia enää tule mukana.
    """

    def __init__(
        self,
        freeze_ticks: list[int],
        round_ends: list[dict[str, Any]],
        tick_rows: dict[int, list[dict[str, Any]]],
        *,
        drop_props: tuple[str, ...] = (),
    ) -> None:
        self.freeze_ticks = freeze_ticks
        self.round_ends = round_ends
        self.tick_rows = tick_rows
        self.drop_props = drop_props

    def parse_event(self, name: str) -> pd.DataFrame:
        if name == "round_freeze_end":
            return pd.DataFrame({"tick": list(self.freeze_ticks)})
        if name == "round_end":
            # Oikea kirjasto palauttaa alkuarvorivin tickillä 1.
            dummy = {"reason": None, "round": 0, "tick": 1, "winner": None}
            return pd.DataFrame([dummy, *self.round_ends])
        return pd.DataFrame()

    def parse_ticks(
        self, wanted_props: list[str], *, ticks: list[int] | None = None
    ) -> pd.DataFrame:
        rivit = [r for tick in (ticks or []) for r in self.tick_rows.get(tick, [])]
        frame = pd.DataFrame(rivit)
        for prop in self.drop_props:
            if prop in frame.columns:
                frame = frame.drop(columns=[prop])
        return frame


def parse_with(fake: FakeDemoparser2, tmp_path: Path) -> pl.DataFrame:
    """Aja adapteri feikin päällä; vain ``_open`` korvataan."""
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Rounds()
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    return adapter.parse_rounds(demo)


def parse_adapter(fake: FakeDemoparser2, tmp_path: Path) -> Demoparser2Rounds:
    """Sama, mutta palauttaa adapterin, jotta ``diagnostics`` on luettavissa."""
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Rounds()
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    adapter.parse_rounds(demo)
    return adapter


# --- Ottelun rakennus ----------------------------------------------------------


@dataclass
class Kierros:
    """Yksi kierros feikkidemossa."""

    demo_round: int | None
    #: ``None`` = freezetime-ankkuri puuttuu tästä kierroksesta.
    freeze_tick: int | None
    #: ``None`` = kierros ei ratkennut (demo katkesi).
    end_tick: int | None
    winner: str | None = None
    reason: str | None = None
    #: Kummalla puolella kokoonpano A on. B on aina vastapuolella.
    a_side: str = "T"
    #: Yhteispistemäärä freezetime-ankkurissa ja kierroksen lopputickissä.
    score_at_freeze: int = 0
    score_at_end: int = 0
    #: Elossa kierroksen lopussa (A, B).
    alive: tuple[int, int] = (0, 0)
    a_players: list[str] = field(default_factory=lambda: list(A_PLAYERS))
    b_players: list[str] = field(default_factory=lambda: list(B_PLAYERS))
    #: Pelin sekuntikello freezetime-ankkurissa; ``None`` jättää sen tyhjäksi.
    round_start_time: float | None = None
    #: Jätä toisen puolen pistelukema pois (yksipuolinen mittaus).
    score_only_side: str | None = None


def _rows(
    kierros: Kierros, tick: int, *, at_end: bool, total_score: int
) -> list[dict[str, Any]]:
    """Rakenna yhden tickin pelaajarivit."""
    b_side = "CT" if kierros.a_side == "T" else "T"
    # Yhteispistemäärä jaetaan puolikkaiksi; testien kannalta merkitystä on
    # vain summalla, jonka adapteri laskee.
    puolikkaat = {kierros.a_side: total_score, b_side: 0}

    rivit: list[dict[str, Any]] = []
    for side, pelaajat, elossa in (
        (kierros.a_side, kierros.a_players, kierros.alive[0]),
        (b_side, kierros.b_players, kierros.alive[1]),
    ):
        if kierros.score_only_side is not None and side != kierros.score_only_side:
            score: int | None = None
        else:
            score = puolikkaat[side]
        for index, steamid in enumerate(pelaajat):
            rivit.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": steamid,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._ACCOUNT: 800,
                    dp._EQUIP_FREEZE_END: 4200,
                    dp._EQUIP_ROUND_START: 200,
                    dp._EQUIP_CURRENT: 3000,
                    dp._LIFE_STATE: 0 if (at_end and index < elossa) else 1,
                    dp._TEAM_SCORE: score,
                    dp._ROUND_START_TIME: kierros.round_start_time,
                }
            )
    return rivit


def build(kierrokset: list[Kierros]) -> FakeDemoparser2:
    """Kokoa feikki kierroslistasta."""
    freeze_ticks: list[int] = []
    round_ends: list[dict[str, Any]] = []
    tick_rows: dict[int, list[dict[str, Any]]] = {}

    for kierros in kierrokset:
        if kierros.freeze_tick is not None:
            freeze_ticks.append(kierros.freeze_tick)
            tick_rows[kierros.freeze_tick] = _rows(
                kierros,
                kierros.freeze_tick,
                at_end=False,
                total_score=kierros.score_at_freeze,
            )
        if kierros.end_tick is not None:
            round_ends.append(
                {
                    "reason": kierros.reason,
                    "round": kierros.demo_round,
                    "tick": kierros.end_tick,
                    "winner": kierros.winner,
                }
            )
            tick_rows[kierros.end_tick] = _rows(
                kierros,
                kierros.end_tick,
                at_end=True,
                total_score=kierros.score_at_end,
            )
    return FakeDemoparser2(sorted(freeze_ticks), round_ends, tick_rows)


def normaali_ottelu(
    pelatut: int = 3, *, knife: bool = True, tickrate: float = 64.0
) -> list[Kierros]:
    """Puukkokierros + N pelattua kierrosta, kuten oikeassa demossa.

    Puukkokierroksen piste nollataan, joten sen ``score_at_end`` on 1 mutta
    seuraavan kierroksen ankkurissa lukema on taas 0.
    """
    kierrokset: list[Kierros] = []
    tick = 1000
    aika = 100.0
    demo_round = 1
    pisteet = 0

    if knife:
        kierrokset.append(
            Kierros(
                demo_round=demo_round,
                freeze_tick=tick,
                end_tick=tick + 500,
                winner="T",
                reason="ct_killed",
                score_at_freeze=0,
                score_at_end=1,  # nollataan mp_restartgamella
                alive=(4, 0),
                round_start_time=aika,
            )
        )
        demo_round += 1
        tick += 1000
        aika += 1000 / tickrate

    for _ in range(pelatut):
        kierrokset.append(
            Kierros(
                demo_round=demo_round,
                freeze_tick=tick,
                end_tick=tick + 500,
                winner="CT",
                reason="t_killed",
                score_at_freeze=pisteet,
                score_at_end=pisteet + 1,
                alive=(0, 3),
                round_start_time=aika,
            )
        )
        pisteet += 1
        demo_round += 1
        tick += 1000
        aika += 1000 / tickrate
    return kierrokset


def numerot(df: pl.DataFrame) -> list[int | None]:
    return (
        mark_played_rounds(df)
        .unique(subset=["round_raw"], keep="first", maintain_order=True)
        .sort("round_raw")["round_no"]
        .to_list()
    )


# --- Porttisopimus -------------------------------------------------------------


def test_frame_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    df = parse_with(build(normaali_ottelu()), tmp_path)
    assert tuple(df.columns) == ROUNDS_ADAPTER_COLUMNS
    assert df["round_no"].null_count() == df.height


def test_two_rows_per_round(tmp_path: Path) -> None:
    df = parse_with(build(normaali_ottelu(pelatut=5)), tmp_path)
    assert df.height == 2 * 6  # puukkokierros + 5
    assert df.group_by("round_raw").len()["len"].unique().to_list() == [2]


# --- Kierrosnumerointi ja round_raw --------------------------------------------


def test_round_raw_comes_from_the_demo_not_from_a_counter(tmp_path: Path) -> None:
    """Demon oma ``round``-kenttä päätyy sellaisenaan tauluun."""
    kierrokset = normaali_ottelu(pelatut=2)
    for offset, kierros in enumerate(kierrokset):
        kierros.demo_round = 40 + offset  # demon oma numerointi ei ala ykkösestä
    df = parse_with(build(kierrokset), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [40, 41, 42]


def test_knife_round_is_not_played(tmp_path: Path) -> None:
    df = parse_with(build(normaali_ottelu(pelatut=3)), tmp_path)
    assert numerot(df) == [None, 1, 2, 3]


def test_last_round_score_end_comes_from_its_own_round_end_tick(
    tmp_path: Path,
) -> None:
    """Viimeisellä kierroksella ei ole seuraavaa ankkuria.

    Tämä on ainoa kohta, jossa ``score_end`` luetaan eri tickistä kuin muilla
    kierroksilla. Ilman tätä testiä jokaisen ottelun viimeinen kierros voisi
    pudota hiljaa pois.
    """
    df = parse_with(build(normaali_ottelu(pelatut=3)), tmp_path)
    viimeinen = df.filter(pl.col("round_raw") == pl.col("round_raw").max())
    assert viimeinen["score_start"].unique().to_list() == [2]
    assert viimeinen["score_end"].unique().to_list() == [3]
    assert numerot(df)[-1] == 3


def test_unfinished_last_round_is_not_numbered(tmp_path: Path) -> None:
    """Demo katkesi kesken kierroksen: ankkuri ilman round_endiä."""
    kierrokset = normaali_ottelu(pelatut=2)
    kierrokset.append(
        Kierros(
            demo_round=None,
            freeze_tick=9000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=250.0,
        )
    )
    df = parse_with(build(kierrokset), tmp_path)
    assert numerot(df) == [None, 1, 2, None]
    keskeneraiset = df.filter(pl.col("round_raw") == df["round_raw"].max())
    assert keskeneraiset["won"].null_count() == 2
    assert keskeneraiset["win_reason"].null_count() == 2


def test_orphan_freeze_anchor_in_the_middle_becomes_its_own_round(
    tmp_path: Path,
) -> None:
    """Kaksi freeze-ankkuria yhtä round_endiä kohden.

    Ensimmäinen ankkuri jää ilman tulosta; se ei ole pelattu kierros, mutta
    sen on säilyttävä omana kierroksenaan, jottei numerointi siirry.
    """
    kierrokset = normaali_ottelu(pelatut=2, knife=False)
    kierrokset.insert(
        1,
        Kierros(
            demo_round=None,
            freeze_tick=kierrokset[0].freeze_tick + 100,
            end_tick=None,
            score_at_freeze=1,
            round_start_time=110.0,
        ),
    )
    df = parse_with(build(kierrokset), tmp_path)
    assert df.height == 6
    assert numerot(df) == [1, None, 2]


def test_round_end_without_an_anchor_is_kept_with_its_own_status(
    tmp_path: Path,
) -> None:
    """Kierros ilman freezetime-ankkuria on mukana ja saa numeron."""
    kierrokset = normaali_ottelu(pelatut=3, knife=False)
    kierrokset[1].freeze_tick = None
    df = parse_with(build(kierrokset), tmp_path)

    assert numerot(df) == [1, 2, 3]
    ankkuriton = df.filter(pl.col("status") == "no_freeze_end")
    assert ankkuriton.height == 2
    assert ankkuriton["freeze_end_tick"].null_count() == 2
    assert ankkuriton["money_freeze_end"].null_count() == 2
    # Pistelukemat periytyvät naapureista, joten kierros pysyy pelattuna.
    assert ankkuriton["score_start"].unique().to_list() == [1]
    assert ankkuriton["score_end"].unique().to_list() == [2]


def test_inconsistent_demo_round_numbers_are_refused(tmp_path: Path) -> None:
    kierrokset = normaali_ottelu(pelatut=3, knife=False)
    kierrokset[2].demo_round = kierrokset[1].demo_round  # sama numero kahdesti
    with pytest.raises(ParseError, match="kierrosnumerointi"):
        parse_with(build(kierrokset), tmp_path)


# --- Kokoonpanot ja puolet -----------------------------------------------------


def test_half_time_switch_keeps_the_lineup_key(tmp_path: Path) -> None:
    """Puolet vaihtuvat, joukkueet eivät."""
    kierrokset = normaali_ottelu(pelatut=4, knife=False)
    for kierros in kierrokset[2:]:
        kierros.a_side = "CT"

    df = parse_with(build(kierrokset), tmp_path)
    assert df["lineup_key"].n_unique() == 2

    a_avain = df.filter(pl.col("round_raw") == kierrokset[0].demo_round).filter(
        pl.col("side") == "T"
    )["lineup_key"][0]
    myohemmin = df.filter(pl.col("round_raw") == kierrokset[-1].demo_round).filter(
        pl.col("side") == "CT"
    )["lineup_key"][0]
    assert a_avain == myohemmin


def test_substitute_does_not_split_the_team(tmp_path: Path) -> None:
    """Yksi pelaaja vaihtuu kesken kartan: kokoonpano pysyy samana joukkueena."""
    kierrokset = normaali_ottelu(pelatut=4, knife=False)
    for kierros in kierrokset[2:]:
        kierros.a_players = [*A_PLAYERS[:4], "sijainen"]

    df = parse_with(build(kierrokset), tmp_path)
    assert df["lineup_key"].n_unique() == 2
    # Molemmilla joukkueilla on yhtä monta riviä -- kolmatta joukkuetta ei synny.
    assert df.group_by("lineup_key").len()["len"].unique().to_list() == [4]


def test_side_assignment_never_guesses_when_teams_do_not_separate(
    tmp_path: Path,
) -> None:
    """Tasapelissä peritään edellisen kierroksen kuvaus, ei oleteta (T, CT)."""
    kierrokset = normaali_ottelu(pelatut=3, knife=False)
    kierrokset[1].a_side = "CT"  # joukkueet vaihtoivat puolta
    # Kolmas kierros: aivan uudet pelaajat -> kumpikaan kuvaus ei voita.
    kierrokset[2].a_players = ["uusi1", "uusi2"]
    kierrokset[2].b_players = ["uusi3", "uusi4"]
    kierrokset[2].a_side = "T"

    df = parse_with(build(kierrokset), tmp_path)
    # Edellinen kuvaus oli "A on CT", joten se peritään: kokoonpano 0 on CT.
    kolmas = df.filter(pl.col("round_raw") == kierrokset[2].demo_round)
    eka_avain = df["lineup_key"][0]
    assert kolmas.filter(pl.col("lineup_key") == eka_avain)["side"].to_list() == ["CT"]


def test_first_round_with_only_one_side_is_refused(tmp_path: Path) -> None:
    kierrokset = normaali_ottelu(pelatut=2, knife=False)
    kierrokset[0].b_players = []
    with pytest.raises(ParseError, match="vain toiselta puolelta"):
        parse_with(build(kierrokset), tmp_path)


def test_round_without_players_and_without_history_is_refused(
    tmp_path: Path,
) -> None:
    kierrokset = normaali_ottelu(pelatut=2, knife=False)
    kierrokset[0].a_players = []
    kierrokset[0].b_players = []
    with pytest.raises(ParseError, match="puolia ei voitu määrittää"):
        parse_with(build(kierrokset), tmp_path)


def test_empty_lineup_never_produces_a_key() -> None:
    """Tyhjän kokoonpanon tiiviste olisi molemmilla joukkueilla sama."""
    with pytest.raises(ParseError, match="kokoonpanoa"):
        dp._Lineup().key()


# --- Pistemäärä ----------------------------------------------------------------


def test_one_sided_score_reading_is_not_a_sum() -> None:
    """Vain toisen puolen lukema ei ole yhteispistemäärä.

    Yksipuolinen summa olisi liian pieni mutta näyttäisi kelvolliselta luvulta,
    ja kierros voisi sen takia pudota pelattujen joukosta tai jäädä mukaan
    väärällä numerolla.
    """
    molemmat = [
        {"side": "T", "team_score": 7},
        {"side": "CT", "team_score": 5},
    ]
    assert dp._total_score(molemmat) == 12
    assert dp._total_score(molemmat[:1]) is None
    assert dp._total_score([{"side": "T", "team_score": None}]) is None
    assert dp._total_score([]) is None


def test_one_sided_anchor_falls_back_to_a_trustworthy_neighbour(
    tmp_path: Path,
) -> None:
    """Hylätty lukema korvataan naapurilla, ei puolikkaalla summalla."""
    kierrokset = normaali_ottelu(pelatut=3, knife=False)
    kierrokset[1].score_only_side = kierrokset[1].a_side
    # Jos yksipuolinen lukema kelpaisi, score_start olisi tämä 99.
    kierrokset[1].score_at_freeze = 99

    df = parse_with(build(kierrokset), tmp_path)
    toinen = df.filter(pl.col("round_raw") == kierrokset[1].demo_round)
    assert toinen["score_start"].unique().to_list() == [1]
    assert numerot(df) == [1, 2, 3]


def test_score_jump_larger_than_one_is_refused(tmp_path: Path) -> None:
    """Kahden pisteen hyppy tarkoittaa, että kierros jäi tunnistamatta."""
    kierrokset = normaali_ottelu(pelatut=3, knife=False)
    kierrokset[2].score_at_freeze = 5  # aukko toisen ja kolmannen välissä
    kierrokset[2].score_at_end = 6

    df = parse_with(build(kierrokset), tmp_path)
    with pytest.raises(ParseError, match="enemmän kuin"):
        mark_played_rounds(df)


# --- Tickrate ------------------------------------------------------------------


def test_tick_rate_is_measured_from_the_game_clock(tmp_path: Path) -> None:
    adapter = parse_adapter(build(normaali_ottelu(pelatut=4)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == 64.0
    assert adapter.diagnostics.tick_rate_measured is True


def test_tick_rate_measurement_uses_the_median(tmp_path: Path) -> None:
    """Yksi poikkeava kierrosväli ei saa siirtää tulosta."""
    kierrokset = normaali_ottelu(pelatut=5, knife=False)
    kierrokset[2].round_start_time = kierrokset[2].round_start_time + 40  # kellon hyppy
    adapter = parse_adapter(build(kierrokset), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == 64.0
    assert adapter.diagnostics.tick_rate_measured is True


def test_tick_rate_falls_back_to_the_default_without_a_clock(tmp_path: Path) -> None:
    kierrokset = normaali_ottelu(pelatut=3)
    for kierros in kierrokset:
        kierros.round_start_time = None

    adapter = parse_adapter(build(kierrokset), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == DEFAULT_TICK_RATE
    assert adapter.diagnostics.tick_rate_measured is False


def test_absurd_tick_rate_is_rejected_as_a_measurement(tmp_path: Path) -> None:
    """Järkevyysrajojen ulkopuolinen arvo on mittausvirhe, ei totuus."""
    kierrokset = normaali_ottelu(pelatut=4, knife=False)
    for index, kierros in enumerate(kierrokset):
        # 1000 tickiä / 0,001 s = 1 000 000 tickiä sekunnissa.
        kierros.round_start_time = 100.0 + index * 0.001

    adapter = parse_adapter(build(kierrokset), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == DEFAULT_TICK_RATE
    assert adapter.diagnostics.tick_rate_measured is False


def test_diagnostics_report_every_round_boundary(tmp_path: Path) -> None:
    adapter = parse_adapter(build(normaali_ottelu(pelatut=6)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.rounds_seen == 7  # puukkokierros mukaan lukien


# --- Propien katoaminen --------------------------------------------------------


@pytest.mark.parametrize("prop", [dp._TEAM_SCORE, dp._ACCOUNT, dp._LIFE_STATE])
def test_missing_prop_is_named_in_the_error(tmp_path: Path, prop: str) -> None:
    """Uudelleennimetty kenttä tuottaisi muuten rakenteellisesti kelvollisen
    mutta täysin tyhjän taulun."""
    fake = build(normaali_ottelu(pelatut=2))
    fake.drop_props = (prop,)
    with pytest.raises(ParseError) as exc:
        parse_with(fake, tmp_path)
    assert prop in str(exc.value)


def test_no_rounds_at_all_is_a_finnish_error(tmp_path: Path) -> None:
    with pytest.raises(ParseError, match="ei löytynyt yhtään kierrosta"):
        parse_with(FakeDemoparser2([], [], {}), tmp_path)


# --- Havainnot -----------------------------------------------------------------


def test_survivors_and_carry_over_follow_the_team_not_the_side(
    tmp_path: Path,
) -> None:
    """Eloonjääneiden varusteet siirtyvät joukkueelle, myös puolenvaihdossa."""
    kierrokset = normaali_ottelu(pelatut=3, knife=False)
    kierrokset[0].alive = (2, 0)  # kokoonpano A jätti kaksi henkiin
    for kierros in kierrokset[1:]:
        kierros.a_side = "CT"

    df = parse_with(build(kierrokset), tmp_path)
    a_avain = df["lineup_key"][0]
    toinen_kierros = df.filter(
        (pl.col("round_raw") == kierrokset[1].demo_round)
        & (pl.col("lineup_key") == a_avain)
    )
    assert toinen_kierros["side"].to_list() == ["CT"]
    assert toinen_kierros["survivors_equip_prev"].to_list() == [2 * 3000]
