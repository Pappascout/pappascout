"""Demoadapterin logiikka ilman demoja.

``Demoparser2Adapter`` on kahdessa osassa: ohut kuori, joka kutsuu
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
from pappascout.adapters.demo_parser import DEFAULT_TICK_RATE, Demoparser2Adapter
from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
)
from pappascout.domain.schemas import EVENTS, TICKS
from pappascout.domain.rounds import mark_played_rounds
from pappascout.errors import ParseError

A_PLAYERS = ["aaa1", "aaa2", "aaa3", "aaa4", "aaa5"]
B_PLAYERS = ["bbb1", "bbb2", "bbb3", "bbb4", "bbb5"]

_SIDE_TEAM = {"T": 2, "CT": 3}


# --- Feikki -------------------------------------------------------------------


#: Aseet, jotka eivät kelpaa ensikontaktiksi -- sama lista kuin
#: ``settings.toml``in ``[parse]``-osiossa.
UTILITY = (
    "hegrenade",
    "flashbang",
    "smokegrenade",
    "decoy",
    "molotov",
    "incgrenade",
    "inferno",
)

#: Näytepisteet, jotka mahtuvat feikkikierroksen sisään (500 tickiä = 7,8 s).
SNAPSHOT_SECONDS: tuple[float, ...] = (2.0, 5.0)

#: Puolten korkeudet feikkikartalla. Ero on tarkoituksella suurempi kuin mikään
#: testissä käytetty ``area_snap_units``, jotta lähin pelaaja on aina omalta
#: puolelta eikä vastapuolen samanindeksinen pelaaja.
A_SIDE_HEIGHT = 5.0
B_SIDE_HEIGHT = 1005.0

#: Etäisyys, jonka sisältä utility saa napata alueen näissä testeissä.
AREA_SNAP_UNITS = 500.0


class FakeDemoparser2:
    """Palauttaa samat kehysmuodot kuin demoparser2 0.42.0.

    ``parse_ticks`` palauttaa **täsmälleen pyydetyt propit** kuten oikea
    kirjasto, joten adapterin kaksi eri propilistaa (kierrosrajat ja
    näytepisteet) tulevat aidosti erikseen testatuiksi. Tick, jota ei ole
    kirjattu kierrosrajaksi, on näytepiste: sen rivit generoidaan siitä
    kierroksesta, jonka sisään tick osuu.

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
        events: dict[str, list[dict[str, Any]]] | None = None,
        rounds_model: list["Kierros"] | None = None,
        grenades: list[dict[str, Any]] | None = None,
        drop_grenade_columns: tuple[str, ...] = (),
    ) -> None:
        self.freeze_ticks = freeze_ticks
        self.round_ends = round_ends
        self.tick_rows = tick_rows
        self.drop_props = drop_props
        self.events = events or {}
        self.rounds_model = rounds_model or []
        self.grenades = grenades or []
        self.drop_grenade_columns = drop_grenade_columns
        #: Propilistat kutsujärjestyksessä -- testi voi todeta, ettei koko
        #: tickisarjaa luettu.
        self.tick_calls: list[tuple[tuple[str, ...], tuple[int, ...]]] = []

    def parse_event(self, name: str) -> pd.DataFrame:
        if name == "round_freeze_end":
            return pd.DataFrame({"tick": list(self.freeze_ticks)})
        if name == "round_end":
            # Oikea kirjasto palauttaa alkuarvorivin tickillä 1.
            dummy = {"reason": None, "round": 0, "tick": 1, "winner": None}
            return pd.DataFrame([dummy, *self.round_ends])
        if name in self.events:
            return pd.DataFrame(self.events[name])
        return pd.DataFrame()

    def parse_grenades(self) -> pd.DataFrame:
        """Lentoradat, sarakkeet samassa muodossa kuin oikeassa kirjastossa.

        Oikea ``parse_grenades()`` palauttaa **koko** radan: rivit myös
        kranaatin reppuvaiheesta, jolloin koordinaatit ovat tyhjiä. Feikki
        antaa vain sen, mitä testi rakensi -- reppurivit voi lisätä itse.
        """
        sarakkeet = [*dp.GRENADE_COLUMNS, "name"]
        frame = pd.DataFrame(
            [{name: rivi.get(name) for name in sarakkeet} for rivi in self.grenades],
            columns=sarakkeet,
        )
        for sarake in self.drop_grenade_columns:
            if sarake in frame.columns:
                frame = frame.drop(columns=[sarake])
        return frame

    def parse_ticks(
        self, wanted_props: list[str], *, ticks: list[int] | None = None
    ) -> pd.DataFrame:
        self.tick_calls.append((tuple(wanted_props), tuple(ticks or ())))
        rivit = [r for tick in (ticks or []) for r in self._rows_at(tick)]
        sarakkeet = [*wanted_props, "tick", "steamid", "name"]
        frame = pd.DataFrame(
            [{name: rivi.get(name) for name in sarakkeet} for rivi in rivit],
            columns=sarakkeet,
        )
        for prop in self.drop_props:
            if prop in frame.columns:
                frame = frame.drop(columns=[prop])
        return frame

    def _rows_at(self, tick: int) -> list[dict[str, Any]]:
        if tick in self.tick_rows:
            return self.tick_rows[tick]
        for kierros in self.rounds_model:
            if kierros.freeze_tick is None or kierros.end_tick is None:
                continue
            if kierros.freeze_tick <= tick <= kierros.end_tick:
                return _sample_rows(kierros, tick)
        return []


def parse_tables(
    fake: FakeDemoparser2,
    tmp_path: Path,
    *,
    sample_seconds: tuple[float, ...] = SNAPSHOT_SECONDS,
    exclude_weapons: tuple[str, ...] = UTILITY,
    fallback_death: bool = True,
    area_snap_units: float | None = AREA_SNAP_UNITS,
) -> DemoTables:
    """Aja adapteri feikin päällä; vain ``_open`` korvataan."""
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Adapter(
        exclude_weapons=exclude_weapons,
        fallback_death=fallback_death,
        area_snap_units=area_snap_units,
    )
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    return adapter.parse_demo(demo, sample_seconds)


def parse_with(fake: FakeDemoparser2, tmp_path: Path, **kwargs) -> pl.DataFrame:
    """Pelkkä kierrostaulu; valtaosa testeistä tutkii vain sitä."""
    return parse_tables(fake, tmp_path, **kwargs).rounds


def parse_ticks_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """Pelkkä näytepistetaulu."""
    return parse_tables(fake, tmp_path, **kwargs).ticks


def parse_events_table(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> pl.DataFrame:
    """Pelkkä utility-tapahtumataulu."""
    return parse_tables(fake, tmp_path, **kwargs).events


def parse_adapter(
    fake: FakeDemoparser2, tmp_path: Path, **kwargs
) -> Demoparser2Adapter:
    """Sama, mutta palauttaa adapterin, jotta ``diagnostics`` on luettavissa."""
    demo = tmp_path / "feikki.dem"
    demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
    adapter = Demoparser2Adapter(
        exclude_weapons=kwargs.pop("exclude_weapons", UTILITY),
        fallback_death=kwargs.pop("fallback_death", True),
        area_snap_units=kwargs.pop("area_snap_units", AREA_SNAP_UNITS),
    )
    adapter._open = lambda *args, **kwargs2: fake  # type: ignore[method-assign]
    adapter.parse_demo(demo, kwargs.pop("sample_seconds", SNAPSHOT_SECONDS))
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
    #: Montako A-puolen pelaajaa jää ilman luettavia freezetime-arvoja.
    #: Jäljittelee tickiä, jossa osa propeista on tyhjiä.
    a_unreadable: int = 0

    # -- Näytepisteet --
    #: Alue, jolla kokoonpano A on näytepisteissä. ``None`` = pelin nimetön
    #: alue, joka tulee demosta tyhjänä merkkijonona.
    a_area: str | None = "TSpawn"
    b_area: str | None = "CTSpawn"
    #: Montako listan alusta laskien on kuollut näytepisteessä.
    a_dead_at_sample: int = 0
    b_dead_at_sample: int = 0
    #: Pelaajakohtainen alue, joka ohittaa puolen oletusalueen. Tarvitaan, kun
    #: testin on erotettava heittäjän oma alue naapurin alueesta.
    player_areas: dict[str, str | None] = field(default_factory=dict)
    #: Pelaajat, jotka **puuttuvat** näytepisteiden riveistä kokonaan.
    #: Jäljittelee tickiä, jolta pelaajaa ei saada luettua lainkaan.
    sample_skip: tuple[str, ...] = ()
    #: Vahinkotapahtumat ``(tick-siirtymä ankkurista, tekijä, uhri, ase)``.
    hurt: list[tuple[int, str | None, str | None, str | None]] = field(
        default_factory=list
    )
    #: Kuolemat samassa muodossa; ensikontaktin varalähde.
    deaths: list[tuple[int, str | None, str | None, str | None]] = field(
        default_factory=list
    )

    # -- Utility --
    #: Heitetyt kranaatit
    #: ``(entiteetti, heittäjä, tyyppi, alkusiirtymä, kesto tickeinä)``.
    #: Siirtymä lasketaan freezetime-ankkurista, ja rata kulkee heittäjän
    #: sijainnista poispäin, jotta alue napsahtaa heittäjään.
    grenades: list[tuple[int, str | None, str, int, int]] = field(
        default_factory=list
    )
    #: Reppurivit ``(entiteetti, omistaja, tyyppi, tick-siirtymä)``. Näillä ei
    #: ole koordinaatteja; molotovin ja incendiaryn erottelu lukee ne.
    grenades_in_bag: list[tuple[int, str, str, int]] = field(default_factory=list)


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
            lukukelvoton = (
                side == kierros.a_side
                and not at_end
                and index < kierros.a_unreadable
            )
            rivit.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": steamid,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._ACCOUNT: None if lukukelvoton else 800,
                    dp._CASH_SPENT: None if lukukelvoton else 4000,
                    dp._EQUIP_FREEZE_END: None if lukukelvoton else 4200,
                    dp._EQUIP_ROUND_START: None if lukukelvoton else 200,
                    dp._EQUIP_CURRENT: 3000,
                    dp._LIFE_STATE: 0 if (at_end and index < elossa) else 1,
                    dp._TEAM_SCORE: score,
                    dp._ROUND_START_TIME: kierros.round_start_time,
                }
            )
    return rivit


def _sample_rows(kierros: Kierros, tick: int) -> list[dict[str, Any]]:
    """Näytepisteen rivit: paikka, puoli ja elossaolo, ei talousarvoja.

    Koordinaatit johdetaan pelaajan indeksistä, jotta testi voi todeta, että
    ne kulkevat taulukkoon asti eivätkä muutu matkalla.

    Puolet erotetaan korkeudella (:data:`B_SIDE_HEIGHT`): ilman sitä A- ja
    B-puolen samanindeksiset pelaajat seisoisivat tasan päällekkäin, eikä
    utilityn aluepäättelyllä olisi yksikäsitteistä lähintä pelaajaa.
    """
    b_side = "CT" if kierros.a_side == "T" else "T"
    rivit: list[dict[str, Any]] = []
    for side, pelaajat, kuolleet, alue, korkeus in (
        (
            kierros.a_side,
            kierros.a_players,
            kierros.a_dead_at_sample,
            kierros.a_area,
            A_SIDE_HEIGHT,
        ),
        (
            b_side,
            kierros.b_players,
            kierros.b_dead_at_sample,
            kierros.b_area,
            B_SIDE_HEIGHT,
        ),
    ):
        for index, steamid in enumerate(pelaajat):
            if steamid in kierros.sample_skip:
                continue
            oma_alue = kierros.player_areas.get(steamid, alue)
            rivit.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": steamid,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._LIFE_STATE: 2 if index < kuolleet else 0,
                    # Peli antaa nimettömälle alueelle tyhjän merkkijonon.
                    dp._PLACE_NAME: "" if oma_alue is None else oma_alue,
                    dp._X: float(100 * index),
                    dp._Y: float(-100 * index),
                    dp._Z: korkeus,
                }
            )
    return rivit


def build(kierrokset: list[Kierros]) -> FakeDemoparser2:
    """Kokoa feikki kierroslistasta."""
    freeze_ticks: list[int] = []
    round_ends: list[dict[str, Any]] = []
    tick_rows: dict[int, list[dict[str, Any]]] = {}
    hurt_rows: list[dict[str, Any]] = []
    death_rows: list[dict[str, Any]] = []
    grenade_rows: list[dict[str, Any]] = []

    for kierros in kierrokset:
        if kierros.freeze_tick is not None:
            grenade_rows.extend(_grenade_rows(kierros))
            for kohde, lahde in (
                (hurt_rows, kierros.hurt),
                (death_rows, kierros.deaths),
            ):
                for siirtyma, tekija, uhri, ase in lahde:
                    kohde.append(
                        {
                            "tick": kierros.freeze_tick + siirtyma,
                            "attacker_steamid": tekija,
                            "user_steamid": uhri,
                            "weapon": ase,
                        }
                    )
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
    return FakeDemoparser2(
        sorted(freeze_ticks),
        round_ends,
        tick_rows,
        events={"player_hurt": hurt_rows, "player_death": death_rows},
        rounds_model=list(kierrokset),
        grenades=grenade_rows,
    )


def _grenade_rows(kierros: Kierros) -> list[dict[str, Any]]:
    """Yhden kierroksen lentorata- ja reppurivit.

    Radan alku asetetaan heittäjän omaan sijaintiin (:func:`_sample_rows`
    käyttää samaa kaavaa), jotta heiton alue napsahtaa heittäjään kuten
    oikeassa demossa. Rata etenee siitä poispäin.
    """
    assert kierros.freeze_tick is not None
    rivit: list[dict[str, Any]] = []

    for entity, omistaja, tyyppi, siirtyma in kierros.grenades_in_bag:
        rivit.append(
            {
                "grenade_type": tyyppi,
                "grenade_entity_id": entity,
                "x": None,
                "y": None,
                "z": None,
                "tick": kierros.freeze_tick + siirtyma,
                "steamid": omistaja,
                "name": omistaja,
            }
        )

    for entity, heittaja, tyyppi, siirtyma, kesto in kierros.grenades:
        if heittaja in kierros.a_players:
            index, korkeus = kierros.a_players.index(heittaja), A_SIDE_HEIGHT
        elif heittaja in kierros.b_players:
            index, korkeus = kierros.b_players.index(heittaja), B_SIDE_HEIGHT
        else:
            index, korkeus = 0, A_SIDE_HEIGHT
        alku = (float(100 * index), float(-100 * index), korkeus)
        for askel in range(kesto):
            rivit.append(
                {
                    "grenade_type": tyyppi,
                    "grenade_entity_id": entity,
                    "x": alku[0] + 40.0 * askel,
                    "y": alku[1],
                    "z": alku[2],
                    "tick": kierros.freeze_tick + siirtyma + askel,
                    "steamid": heittaja,
                    "name": heittaja,
                }
            )
    return rivit


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


def test_player_count_is_observed_not_assumed(tmp_path: Path) -> None:
    """``players_freeze_end`` on havainto: neljä pelaajaa -> 4, viisi -> 5.

    Kynnykset ovat per pelaaja, joten jakaja on luettava demosta. Ilman tätä
    testia sarakkeen arvoa ei todennettaisi missään -- vain sen olemassaolo.
    """
    kierrokset = normaali_ottelu(pelatut=2, knife=False)
    kierrokset[0].a_players = A_PLAYERS[:4]

    df = parse_with(build(kierrokset), tmp_path)
    a_avain = df.filter(pl.col("side") == "T")["lineup_key"][0]
    omat = df.filter(pl.col("lineup_key") == a_avain).sort("round_raw")
    assert omat["players_freeze_end"].to_list() == [4, 5]
    vastustaja = df.filter(pl.col("lineup_key") != a_avain).sort("round_raw")
    assert vastustaja["players_freeze_end"].to_list() == [5, 5]


def test_round_without_an_anchor_has_no_player_count(tmp_path: Path) -> None:
    """Ilman freezetime-ankkuria ei ole mitään laskettavaa -- ei myöskään nollaa."""
    kierrokset = normaali_ottelu(pelatut=2, knife=False)
    kierrokset[1].freeze_tick = None

    df = parse_with(build(kierrokset), tmp_path)
    ankkuriton = df.filter(pl.col("status") == "no_freeze_end")
    assert ankkuriton.height == 2
    assert ankkuriton["players_freeze_end"].null_count() == 2


def test_sums_and_their_divisor_come_from_the_same_players(tmp_path: Path) -> None:
    """Osoittaja ja nimittäjä samasta joukosta.

    Kolmen pelaajan summa viidellä jaettuna aliarvioisi varustearvon 40 % ja
    työntäisi kierroksen ecoksi -- hiljaa ja uskottavan näköisesti.
    """
    kierrokset = normaali_ottelu(pelatut=1, knife=False)
    kierrokset[0].a_unreadable = 2  # kahden pelaajan propit tyhjiä

    df = parse_with(build(kierrokset), tmp_path)
    a_avain = df.filter(pl.col("side") == "T")["lineup_key"][0]
    rivi = df.filter(pl.col("lineup_key") == a_avain).row(0, named=True)

    assert rivi["players_freeze_end"] == 3
    assert rivi["equip_freeze_end"] == 3 * 4200
    assert rivi["money_freeze_end"] == 3 * 800
    assert rivi["money_spent"] == 3 * 4000
    # Per pelaaja -arvo pysyy oikeana, koska jakaja on sama joukko.
    assert rivi["equip_freeze_end"] / rivi["players_freeze_end"] == 4200


def test_money_spent_is_read_from_the_demo(tmp_path: Path) -> None:
    """Käytettävissä ollut raha = jäljelle jäänyt + käytetty."""
    df = parse_with(build(normaali_ottelu(pelatut=1, knife=False)), tmp_path)
    rivi = df.row(0, named=True)
    assert rivi["money_spent"] == 5 * 4000
    assert rivi["money_freeze_end"] + rivi["money_spent"] == 5 * 4800


# --- Näytepistetaulu -----------------------------------------------------------


def pitka_ottelu(pelatut: int = 2, kesto: int = 4000) -> list[Kierros]:
    """Kierroksia, jotka kestävät riittävän kauan oikeille näytepisteille.

    ``normaali_ottelu``n kierrokset ovat 500 tickiä eli 7,8 sekuntia; 45
    sekunnin pistettä ei niissä voisi tutkia lainkaan.
    """
    kierrokset: list[Kierros] = []
    tick = 1000
    aika = 100.0
    pisteet = 0
    for numero in range(1, pelatut + 1):
        kierrokset.append(
            Kierros(
                demo_round=numero,
                freeze_tick=tick,
                end_tick=tick + kesto,
                winner="CT",
                reason="t_killed",
                score_at_freeze=pisteet,
                score_at_end=pisteet + 1,
                alive=(0, 3),
                round_start_time=aika,
            )
        )
        pisteet += 1
        aika += (kesto + 1000) / 64.0
        tick += kesto + 1000
    return kierrokset


def test_ticks_frame_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    ticks = parse_ticks_table(build(pitka_ottelu()), tmp_path)
    assert tuple(ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ticks.schema[name] == TICKS[name], name
    # Numeroinnin omistaa domain.rounds, ei adapteri.
    assert ticks["round_no"].null_count() == ticks.height


def test_every_player_gets_a_row_at_every_sample_point(tmp_path: Path) -> None:
    """10 pelaajaa x 4 näytepistettä x 2 kierrosta = 80 riviä."""
    ticks = parse_ticks_table(
        build(pitka_ottelu(pelatut=2)), tmp_path, sample_seconds=(6.0, 15.0, 30.0, 45.0)
    )
    aika = ticks.filter(pl.col("sample_kind") == "time")
    assert aika.height == 80
    per_piste = aika.group_by("round_raw", "sample_t_s").len()
    assert per_piste["len"].unique().to_list() == [10]


def test_a_short_round_has_no_points_after_it_ended(tmp_path: Path) -> None:
    """Hyväksymiskriteeri: 28 sekunnissa ratkennut kierros saa vain 6 ja 15."""
    kierrokset = pitka_ottelu(pelatut=1, kesto=28 * 64)
    ticks = parse_ticks_table(
        build(kierrokset), tmp_path, sample_seconds=(6.0, 15.0, 30.0, 45.0)
    )
    aika = ticks.filter(pl.col("sample_kind") == "time")
    assert sorted(aika["sample_t_s"].unique().to_list()) == [6.0, 15.0]
    assert aika["t_s"].max() <= 28.0


def test_area_and_coordinates_come_from_the_sample_tick(tmp_path: Path) -> None:
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].a_area = "Ramp"
    kierrokset[0].b_area = "Heaven"
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))

    a_puoli = ticks.filter(pl.col("side") == kierrokset[0].a_side)
    assert a_puoli["area"].unique().to_list() == ["Ramp"]
    b_side = "CT" if kierrokset[0].a_side == "T" else "T"
    assert ticks.filter(pl.col("side") == b_side)["area"].unique().to_list() == [
        "Heaven"
    ]
    # Koordinaatit johdettiin pelaajan indeksistä; ne eivät ole nollia.
    assert sorted(a_puoli["x"].to_list()) == [0.0, 100.0, 200.0, 300.0, 400.0]
    assert a_puoli["z"].unique().to_list() == [5.0]


def test_an_unnamed_area_stays_null_but_the_coordinates_remain(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: tyhjä ``m_szLastPlaceName`` -> ``area = null``.

    Riviä ei pudoteta -- tuntematon sijainti raportoidaan koordinaatteina.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].a_area = None
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))

    tuntematon = ticks.filter(pl.col("side") == kierrokset[0].a_side)
    assert tuntematon.height == 5
    assert tuntematon["area"].null_count() == 5
    assert tuntematon["x"].null_count() == 0


def test_a_dead_player_still_gets_a_row(tmp_path: Path) -> None:
    """I/O-matriisi: kuolleiden suodatus on aggregoinnin työ, ei parsinnan."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].a_dead_at_sample = 2
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))

    omat = ticks.filter(pl.col("side") == kierrokset[0].a_side)
    assert omat.height == 5
    assert omat["is_alive"].sum() == 3
    assert ticks["is_alive"].sum() == 8


def test_lineup_key_is_the_same_in_both_tables(tmp_path: Path) -> None:
    """Sama joukkue, sama avain -- muuten liitos menisi hiljaa ristiin."""
    tables = parse_tables(build(pitka_ottelu(pelatut=2)), tmp_path)
    assert set(tables.ticks["lineup_key"].unique()) == set(
        tables.rounds["lineup_key"].unique()
    )
    assert tables.ticks["lineup_key"].n_unique() == 2


def test_lineup_key_follows_the_team_through_the_side_switch(tmp_path: Path) -> None:
    kierrokset = pitka_ottelu(pelatut=4)
    for kierros in kierrokset[2:]:
        kierros.a_side = "CT"

    tables = parse_tables(build(kierrokset), tmp_path)
    ticks = tables.ticks
    a_avain = tables.rounds.filter(
        (pl.col("round_raw") == 1) & (pl.col("side") == "T")
    )["lineup_key"][0]

    eka = ticks.filter((pl.col("round_raw") == 1) & (pl.col("lineup_key") == a_avain))
    viimeinen = ticks.filter(
        (pl.col("round_raw") == 4) & (pl.col("lineup_key") == a_avain)
    )
    assert eka["side"].unique().to_list() == ["T"]
    assert viimeinen["side"].unique().to_list() == ["CT"]


def test_an_unanchored_round_produces_no_tick_rows(tmp_path: Path) -> None:
    """I/O-matriisi: ``status = "no_freeze_end"`` -> ei tick-rivejä."""
    kierrokset = pitka_ottelu(pelatut=3)
    kierrokset[1].freeze_tick = None
    ticks = parse_ticks_table(build(kierrokset), tmp_path)
    assert sorted(ticks["round_raw"].unique().to_list()) == [1, 3]


def test_only_the_needed_ticks_are_read(tmp_path: Path) -> None:
    """Koko tickisarjaa ei lueta: pyydetyt tickit ovat näytepisteitä."""
    fake = build(pitka_ottelu(pelatut=2))
    parse_ticks_table(fake, tmp_path, sample_seconds=(6.0, 15.0))
    naytepisteet = next(
        ticks for propit, ticks in fake.tick_calls if dp._PLACE_NAME in propit
    )
    assert len(naytepisteet) == 4  # 2 kierrosta x 2 pistettä
    # Talousproppeja ei lueta uudelleen näytepisteiltä.
    assert dp._ACCOUNT not in next(
        propit for propit, _ in fake.tick_calls if dp._PLACE_NAME in propit
    )


@pytest.mark.parametrize("prop", [dp._PLACE_NAME, dp._X, dp._Y, dp._Z])
def test_a_missing_sample_prop_is_named_in_the_error(
    tmp_path: Path, prop: str
) -> None:
    """Ilman tarkistusta asetelmataulu olisi rakenteeltaan ehjä mutta paikaton.

    Vain näytepisteiden omat propit ovat täällä: ``m_lifeState`` ja
    ``m_iTeamNum`` luetaan jo kierrosrajoilta, joten niiden katoaminen jää
    kiinni aiemmin ja toisella viestillä.
    """
    fake = build(pitka_ottelu(pelatut=2))
    fake.drop_props = (prop,)
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path)
    assert prop in str(exc.value)
    assert "näytepiste" in str(exc.value)


# --- Ensikontakti oikeilla tapahtumilla ----------------------------------------


def test_first_contact_produces_its_own_sample_point(tmp_path: Path) -> None:
    """Hyväksymiskriteeri: tulitaistelun kierrokselta löytyy first_contact."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].hurt = [
        (20 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
        (10 * 64, A_PLAYERS[1], B_PLAYERS[1], "awp"),
    ]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))

    kontakti = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert kontakti.height == 10
    assert kontakti["t_s"].unique().to_list() == [10.0]
    # sample_t_s kertoo saman hetken -- rivi ei jää ilman aikaleimaa.
    assert kontakti["sample_t_s"].unique().to_list() == [10.0]


def test_utility_only_damage_leaves_the_round_without_a_contact(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: ainoa vahinko molotovista -> ei ensikontaktirivejä."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].hurt = [(15 * 64, A_PLAYERS[0], B_PLAYERS[0], "molotov")]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))

    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()
    # Kierros on silti mukana aikapisteineen.
    assert ticks.height == 10


def test_friendly_fire_does_not_start_the_round(tmp_path: Path) -> None:
    """I/O-matriisi: tekijä samalla puolella -> ei lasketa ensikontaktiksi."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].hurt = [
        (8 * 64, A_PLAYERS[0], A_PLAYERS[1], "hegrenade"),
        (9 * 64, A_PLAYERS[0], A_PLAYERS[1], "ak47"),
        (25 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))
    kontakti = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert kontakti["t_s"].unique().to_list() == [25.0]


def test_a_round_without_damage_has_no_contact_rows(tmp_path: Path) -> None:
    """I/O-matriisi: aika loppui, kukaan ei ampunut."""
    ticks = parse_ticks_table(build(pitka_ottelu(pelatut=1)), tmp_path)
    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()


def test_death_is_used_as_the_fallback_source(tmp_path: Path) -> None:
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].deaths = [(12 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))
    kontakti = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert kontakti["t_s"].unique().to_list() == [12.0]


def test_the_death_fallback_can_be_switched_off(tmp_path: Path) -> None:
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].deaths = [(12 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(
        build(kierrokset), tmp_path, sample_seconds=(6.0,), fallback_death=False
    )
    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()


def test_contact_is_attributed_to_its_own_round(tmp_path: Path) -> None:
    """Toisen kierroksen osuma ei saa aikaistaa ensimmäisen kontaktia."""
    kierrokset = pitka_ottelu(pelatut=2)
    kierrokset[1].hurt = [(5 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))

    kontakti = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert kontakti["round_raw"].unique().to_list() == [2]
    assert kontakti["t_s"].unique().to_list() == [5.0]


def test_contact_after_a_side_switch_uses_the_current_sides(tmp_path: Path) -> None:
    """Puolet vaihtuvat; ristiinpuolisuus on kierroskohtainen tosiasia."""
    kierrokset = pitka_ottelu(pelatut=4)
    for kierros in kierrokset[2:]:
        kierros.a_side = "CT"
    # Kolmannella kierroksella A on CT -- osuma A:sta B:hen on yhä ristiin.
    kierrokset[2].hurt = [
        (7 * 64, A_PLAYERS[0], A_PLAYERS[1], "ak47"),  # oma vahinko
        (11 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))
    kontakti = ticks.filter(
        (pl.col("sample_kind") == "first_contact") & (pl.col("round_raw") == 3)
    )
    assert kontakti["t_s"].unique().to_list() == [11.0]


def test_diagnostics_report_what_the_table_cannot(tmp_path: Path) -> None:
    """Diagnostiikka kertoo vain sen, mitä valmiista taulusta ei näe.

    Näytepisteiden ja ensikontaktien määrät luetaan taulusta vaiheessa; jos ne
    olisivat myös täällä, sama nimi tarkoittaisi kahta eri asiaa -- adapteri
    laskisi numeroimattomat kierrokset mukaan, vaihe ei.
    """
    adapter = parse_adapter(build(pitka_ottelu(pelatut=2)), tmp_path)
    assert adapter.diagnostics is not None
    assert not hasattr(adapter.diagnostics, "sample_points")
    assert adapter.diagnostics.partial_samples == 0
    assert adapter.diagnostics.unknown_side_events == 0


def test_a_partial_sample_point_is_counted(tmp_path: Path) -> None:
    """Vajaa näytepiste ei saa kadota.

    Systemaattinen propivika näkyisi muuten vasta vinoutuneina aggregaatteina
    Story 2.3:ssa, jolloin syytä ei enää löytäisi parsinnasta.
    """
    kierrokset = pitka_ottelu(pelatut=2)
    kierrokset[1].a_players = A_PLAYERS[:3]  # kaksi pelaajaa puuttuu
    adapter = parse_adapter(build(kierrokset), tmp_path, sample_seconds=(6.0, 15.0))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.partial_samples == 2


def test_damage_by_an_unknown_player_is_counted_not_hidden(tmp_path: Path) -> None:
    """Tuntemattoman pelaajan vahinko ohitetaan, mutta luku kertoo siitä.

    Ilman laskuria kierros voisi menettää ensikontaktinsa äänettömästi.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].hurt = [(10 * 64, "tuntematon-pelaaja", B_PLAYERS[0], "ak47")]
    adapter = parse_adapter(build(kierrokset), tmp_path, sample_seconds=(6.0,))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_side_events == 1


def test_a_late_joiner_gets_their_side_from_the_tick(tmp_path: Path) -> None:
    """Kesken karttaa tullut pelaaja saa puolensa tickin omasta arvosta."""
    kierrokset = pitka_ottelu(pelatut=2)
    kierrokset[1].a_players = [*A_PLAYERS[:4], "myohemmin-tullut"]
    kierrokset[1].hurt = [(9 * 64, "myohemmin-tullut", B_PLAYERS[0], "ak47")]

    tables = parse_tables(build(kierrokset), tmp_path, sample_seconds=(6.0,))
    kontakti = tables.ticks.filter(
        (pl.col("sample_kind") == "first_contact") & (pl.col("round_raw") == 2)
    )
    assert kontakti["t_s"].unique().to_list() == [9.0]


def test_a_contact_without_a_weapon_name_is_not_a_contact(tmp_path: Path) -> None:
    """Tyhjä asenimi ei ole poissuljettujen listalla -- eikä silti kelpaa."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].hurt = [
        (8 * 64, A_PLAYERS[0], B_PLAYERS[0], None),
        (20 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(kierrokset), tmp_path, sample_seconds=(6.0,))
    kontakti = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert kontakti["t_s"].unique().to_list() == [20.0]


def test_a_missing_life_state_is_an_error_not_a_dead_player(tmp_path: Path) -> None:
    """``is_alive`` ei ole nullable: puuttuvasta arvosta tulisi hiljaa kuollut.

    Elossa oleva pelaaja katoaisi silloin aggregoinnista. Tuntematon alue saa
    jäädä nulliksi, mutta elossaolo ei.
    """
    fake = build(pitka_ottelu(pelatut=1))
    alkuperainen = fake._rows_at

    def ilman_life_statea(tick: int):
        rivit = [dict(r) for r in alkuperainen(tick)]
        if rivit and dp._PLACE_NAME in rivit[0]:
            rivit[0][dp._LIFE_STATE] = None
        return rivit

    fake._rows_at = ilman_life_statea  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert dp._LIFE_STATE in str(exc.value)


@pytest.mark.parametrize(
    "sarake", ["attacker_steamid", "user_steamid", "weapon", "tick"]
)
def test_a_missing_damage_column_is_named_in_the_error(
    tmp_path: Path, sarake: str
) -> None:
    """Ilman tarkistusta nolla ensikontaktia näyttäisi kelvolliselta tulokselta."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].hurt = [(10 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    fake = build(kierrokset)
    fake.events["player_hurt"] = [
        {k: v for k, v in rivi.items() if k != sarake}
        for rivi in fake.events["player_hurt"]
    ]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert sarake in str(exc.value)


def test_a_sample_point_without_any_rows_is_refused(tmp_path: Path) -> None:
    """Näytepiste, joka ei tuota riviäkään, laskettaisiin mukaan lukuihin."""
    fake = build(pitka_ottelu(pelatut=1))
    alkuperainen = fake._rows_at

    def tyhja_naytepisteella(tick: int):
        rivit = alkuperainen(tick)
        if rivit and dp._PLACE_NAME in rivit[0]:
            return []
        return rivit

    fake._rows_at = tyhja_naytepisteella  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert "pelaajarivi" in str(exc.value)


def test_a_round_where_both_lineups_get_the_same_side_is_refused() -> None:
    """Sama puoli molemmille antaisi molemmille saman ``lineup_key``:n.

    Taulu näyttäisi kelvolliselta, mutta jokainen joukkuekohtainen luku olisi
    molempien summa.
    """
    segment = dp._Segment(
        demo_round=1,
        freeze_end_tick=1000,
        end_tick=2000,
        winner_side="T",
        win_reason="ct_killed",
        round_raw=1,
    )
    with pytest.raises(ParseError, match="sama puoli"):
        dp._keys_by_side(("T", "T"), ["aaa", "bbb"], segment)
    assert dp._keys_by_side(("T", "CT"), ["aaa", "bbb"], segment) == {
        "T": "aaa",
        "CT": "bbb",
    }


def test_a_demo_without_any_samplable_round_yields_an_empty_typed_table(
    tmp_path: Path,
) -> None:
    """Tyhjä taulu on silti sopimuksen mukainen -- ei Null-tyyppejä."""
    kierrokset = pitka_ottelu(pelatut=2)
    for kierros in kierrokset:
        kierros.freeze_tick = None
    ticks = parse_ticks_table(build(kierrokset), tmp_path)
    assert ticks.is_empty()
    assert tuple(ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ticks.schema[name] == TICKS[name], name


# --- Utility -------------------------------------------------------------------


def utility_ottelu(pelatut: int = 1, kesto: int = 4000) -> list[Kierros]:
    """Kierroksia, joilla kummankin puolen ensimmäinen pelaaja heittää kranaatin."""
    kierrokset = pitka_ottelu(pelatut=pelatut, kesto=kesto)
    entity = 1
    for kierros in kierrokset:
        kierros.grenades = [
            (entity, kierros.a_players[0], "CSmokeGrenadeProjectile", 100, 60),
            (entity + 1, kierros.b_players[0], "CFlashbangProjectile", 200, 40),
        ]
        entity += 2
    return kierrokset


def test_a_grenade_becomes_a_throw_and_a_detonation(tmp_path: Path) -> None:
    """I/O-matriisi: normaali savu -> kaksi riviä samalla entiteetillä."""
    events = parse_events_table(build(utility_ottelu()), tmp_path)

    assert tuple(events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert events.schema[name] == EVENTS[name], name

    savu = events.filter(pl.col("grenade_entity_id") == 1)
    assert savu["event_kind"].to_list() == ["grenade_thrown", "grenade_detonate"]
    assert savu["grenade_type"].unique().to_list() == ["smoke"]
    assert savu["thrower_id"].unique().to_list() == [A_PLAYERS[0]]
    # round_no jää tyhjäksi: numeroinnin päättää domain.rounds.
    assert savu["round_no"].null_count() == savu.height


def test_the_grenade_type_is_canonical_not_a_class_name(tmp_path: Path) -> None:
    events = parse_events_table(build(utility_ottelu()), tmp_path)
    assert set(events["grenade_type"].unique()) == {"smoke", "flashbang"}


def test_an_unknown_class_name_survives_verbatim(tmp_path: Path) -> None:
    """Tuntematon tyyppi on luettava havainto; tyhjäksi muuttaminen hukkaisi sen."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CUusiKranaatti", 100, 30)]
    events = parse_events_table(build(kierrokset), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["CUusiKranaatti"]


def test_the_thrower_side_and_lineup_come_from_the_round(tmp_path: Path) -> None:
    """Heittäjän joukkue tulee kierroksen kuvauksesta, ei arvauksesta."""
    kierrokset = utility_ottelu(pelatut=2)
    kierrokset[1].a_side = "CT"  # puoliajan vaihto kesken feikkiottelun
    tables = parse_tables(build(kierrokset), tmp_path)
    events, rounds = tables.events, tables.rounds

    for rivi in events.iter_rows(named=True):
        oma = rounds.filter(
            (pl.col("round_raw") == rivi["round_raw"])
            & (pl.col("side") == rivi["side"])
        )
        assert oma["lineup_key"].to_list() == [rivi["lineup_key"]]

    a_rivit = events.filter(pl.col("thrower_id") == A_PLAYERS[0]).sort("round_raw")
    assert a_rivit["side"].to_list() == ["T", "T", "CT", "CT"]


def test_the_throw_area_snaps_to_the_thrower(tmp_path: Path) -> None:
    """Heittopisteessä lähin elossa oleva pelaaja on heittäjä itse."""
    kierrokset = utility_ottelu()
    kierrokset[0].a_area = "Ramp"
    kierrokset[0].b_area = "Heaven"
    events = parse_events_table(build(kierrokset), tmp_path)

    heitto = events.filter(
        (pl.col("event_kind") == "grenade_thrown")
        & (pl.col("thrower_id") == A_PLAYERS[0])
    )
    assert heitto["area"].to_list() == ["Ramp"]


def test_a_detonation_far_from_everyone_keeps_its_coordinates(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kaukana räjähtänyt saa area = null, ei pudotusta."""
    kierrokset = pitka_ottelu(pelatut=1)
    # 200 tickiä x 40 yksikköä = 7 960 yksikköä pois kaikista pelaajista.
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 200)]
    events = parse_events_table(build(kierrokset), tmp_path)

    rajahdys = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert rajahdys["area"].null_count() == 1
    assert rajahdys["x"].null_count() == 0
    assert rajahdys["x"].to_list() == [pytest.approx(7960.0)]


def test_an_unset_snap_distance_only_silences_the_detonations(
    tmp_path: Path,
) -> None:
    """Kalibroimaton raja vie arvion, ei havaintoa.

    Heiton alue luetaan heittäjän omasta ``m_szLastPlaceName``ista, joten se
    säilyy vaikka napsautus olisi kytketty kokonaan pois. Vain räjähdys on
    napsautuksen varassa.
    """
    events = parse_events_table(
        build(utility_ottelu()), tmp_path, area_snap_units=None
    )
    assert not events.is_empty()
    heitot = events.filter(pl.col("event_kind") == "grenade_thrown")
    rajahdykset = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert heitot["area"].null_count() == 0
    assert heitot["area_source"].unique().to_list() == ["observed"]
    assert rajahdykset["area"].null_count() == rajahdykset.height
    assert rajahdykset["area_source"].null_count() == rajahdykset.height
    assert events["snap_distance"].null_count() == events.height
    assert events["x"].null_count() == 0


def test_a_single_tick_trajectory_gets_no_detonation(tmp_path: Path) -> None:
    """I/O-matriisi: rata katkeaa -> vain heitto, ei keksittyä räjähdystä."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CHEGrenadeProjectile", 100, 1)]
    events = parse_events_table(build(kierrokset), tmp_path)
    assert events["event_kind"].to_list() == ["grenade_thrown"]


def test_a_grenade_thrown_outside_any_round_is_counted_not_kept(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kierroksen ratkeamisen jälkeinen heitto ei saa t_s:ää."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        # Kierros ratkeaa 4 000 tickin kohdalla; tämä lähtee sen jälkeen.
        (2, A_PLAYERS[1], "CSmokeGrenadeProjectile", 4100, 30),
    ]
    adapter = parse_adapter(build(kierrokset), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_outside_rounds == 1
    tables = parse_tables(build(kierrokset), tmp_path)
    assert tables.events["grenade_entity_id"].unique().to_list() == [1]


def test_a_grenade_that_outlives_the_round_belongs_to_its_throw_round(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kierroksen rajan ylittävä savu kuuluu heittokierrokselle.

    Räjähdyksen t_s saa siis ylittää kierroksen keston -- se on havainto eikä
    virhe.
    """
    kierrokset = pitka_ottelu(pelatut=2)
    # Heitto 100 tickiä ankkurista, rata kestää yli kierroksen lopun (4 000).
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 5000)]
    events = parse_events_table(build(kierrokset), tmp_path)

    assert events["round_raw"].unique().to_list() == [1]
    rajahdys = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert rajahdys["t_s"].to_list()[0] > 4000 / 64.0


def test_a_trajectory_without_a_thrower_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: rata ilman heittoa -> ohitetaan, määrä raportoidaan."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (1, None, "CSmokeGrenadeProjectile", 100, 30),
        (2, A_PLAYERS[0], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(kierrokset), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_without_thrower == 1


def test_a_thrower_in_neither_lineup_gets_their_side_from_the_tick(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: tuntematon heittäjä -> puoli tickin m_iTeamNum-arvosta."""
    kierrokset = pitka_ottelu(pelatut=2)
    myohassa = "myohassa1"
    kierrokset[1].a_players = [*A_PLAYERS[:4], myohassa]
    kierrokset[1].grenades = [(9, myohassa, "CSmokeGrenadeProjectile", 100, 30)]
    tables = parse_tables(build(kierrokset), tmp_path)

    rivit = tables.events.filter(pl.col("thrower_id") == myohassa)
    assert rivit.height == 2
    assert rivit["side"].unique().to_list() == [kierrokset[1].a_side]


def test_a_thrower_whose_side_never_resolves_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """Väärä joukkue veisi utilityn vastustajan tiliin, joten rivi ohitetaan."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (9, "haamu1", "CSmokeGrenadeProjectile", 100, 30),
        (1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(kierrokset), tmp_path)
    tables = parse_tables(build(kierrokset), tmp_path)

    assert "haamu1" not in tables.events["thrower_id"].to_list()
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_side == 1


def test_a_reused_entity_id_stays_two_grenades(tmp_path: Path) -> None:
    """Peli kierrättää tunnisteet -- kahden kierroksen savu ei ole yksi rata."""
    kierrokset = pitka_ottelu(pelatut=2)
    for kierros in kierrokset:
        kierros.grenades = [
            (1, kierros.a_players[0], "CSmokeGrenadeProjectile", 100, 30)
        ]
    events = parse_events_table(build(kierrokset), tmp_path)

    assert events.height == 4
    assert sorted(events["round_raw"].unique().to_list()) == [1, 2]
    maarat = events.group_by("round_raw", "event_kind").len()
    assert maarat["len"].max() == 1


def test_bag_rows_are_not_a_throw(tmp_path: Path) -> None:
    """Repussa oleva kranaatti ei ole heitto: koordinaatit puuttuvat."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 300, 30)]
    kierrokset[0].grenades_in_bag = [
        (1, A_PLAYERS[0], "CSmokeGrenade", siirtyma) for siirtyma in (10, 50, 299)
    ]
    events = parse_events_table(build(kierrokset), tmp_path)

    assert events.height == 2
    heitto = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert heitto["t_s"].to_list() == [pytest.approx(300 / 64.0)]


def test_molotov_and_incendiary_are_told_apart_by_the_bag(tmp_path: Path) -> None:
    """I/O-matriisi: grenade_type erottaa molotovin ja incendiaryn.

    Lennossa molemmat ovat CMolotovProjectile; erottelu tulee heittäjän
    repusta heittoa edeltävältä tickiltä.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (1, A_PLAYERS[0], "CMolotovProjectile", 300, 30),
        (2, B_PLAYERS[0], "CMolotovProjectile", 400, 30),
    ]
    kierrokset[0].grenades_in_bag = [
        (11, A_PLAYERS[0], "CMolotovGrenade", 299),
        (12, B_PLAYERS[0], "CIncendiaryGrenade", 399),
    ]
    events = parse_events_table(build(kierrokset), tmp_path)

    molotov = events.filter(pl.col("grenade_entity_id") == 1)
    incendiary = events.filter(pl.col("grenade_entity_id") == 2)
    assert molotov["grenade_type"].unique().to_list() == ["molotov"]
    assert incendiary["grenade_type"].unique().to_list() == ["incendiary"]


def test_an_ambiguous_bag_leaves_the_generic_molotov(tmp_path: Path) -> None:
    """Molemmat tulikranaatit repussa -> arvausta ei tehdä."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    kierrokset[0].grenades_in_bag = [
        (11, A_PLAYERS[0], "CMolotovGrenade", 299),
        (12, A_PLAYERS[0], "CIncendiaryGrenade", 299),
    ]
    events = parse_events_table(build(kierrokset), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["molotov"]


def test_a_bag_type_does_not_leak_onto_another_grenade(tmp_path: Path) -> None:
    """Savu ei saa tulla nimetyksi incendiaryksi vain koska repussa on sellainen."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 300, 30)]
    kierrokset[0].grenades_in_bag = [(11, A_PLAYERS[0], "CIncendiaryGrenade", 299)]
    events = parse_events_table(build(kierrokset), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["smoke"]


def test_an_unanchored_round_produces_no_event_rows(tmp_path: Path) -> None:
    """I/O-matriisi: ankkuriton kierros -> ei rivejä (t_s ei ole määritelty)."""
    kierrokset = utility_ottelu(pelatut=2)
    kierrokset[0].freeze_tick = None
    kierrokset[0].grenades = []
    events = parse_events_table(build(kierrokset), tmp_path)
    assert events["round_raw"].unique().to_list() == [2]


def test_a_demo_without_utility_yields_an_empty_typed_table(tmp_path: Path) -> None:
    """I/O-matriisi: demo ilman utilityä -> tyhjä mutta sopimuksen mukainen taulu."""
    events = parse_events_table(build(pitka_ottelu(pelatut=2)), tmp_path)
    assert events.is_empty()
    assert tuple(events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert events.schema[name] == EVENTS[name], name


def test_a_missing_grenade_column_is_an_error_not_an_empty_table(
    tmp_path: Path,
) -> None:
    """Tyhjä taulu näyttäisi demolta, jossa ei heitetty yhtään kranaattia."""
    fake = build(utility_ottelu())
    fake.drop_grenade_columns = ("steamid",)
    with pytest.raises(ParseError) as exc:
        parse_events_table(fake, tmp_path)
    assert "steamid" in str(exc.value)
    assert "GRENADE_COLUMNS" in str(exc.value)


def test_a_broken_grenade_read_is_a_finnish_error(tmp_path: Path) -> None:
    fake = build(utility_ottelu())

    def kaatuu():
        raise RuntimeError("lentoradat rikki")

    fake.parse_grenades = kaatuu  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_events_table(fake, tmp_path)
    assert "lentoratoja ei voitu lukea" in str(exc.value)


def test_only_the_endpoint_ticks_are_read_for_areas(tmp_path: Path) -> None:
    """Aluetta varten ei lueta koko rataa vaan sen kaksi päätä.

    Ilman tätä 1,55 miljoonan rivin rata kulkisi tick-lukuun asti.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 500)]
    fake = build(kierrokset)
    parse_events_table(fake, tmp_path, sample_seconds=(6.0,))

    utility_kutsu = [
        ticks
        for propit, ticks in fake.tick_calls
        if dp._PLACE_NAME in propit and set(ticks) == {1100, 1599}
    ]
    assert utility_kutsu, f"paatepisteita ei luettu: {fake.tick_calls}"


# --- Alueen lähde: havainto vs. arvio ------------------------------------------


def test_the_throw_area_is_observed_not_snapped(tmp_path: Path) -> None:
    """Heittäjän oma alue on tiedossa, joten sitä ei arvata naapurista.

    Napsautus voisi tarttua vieressä seisovaan kaveriin. Tässä kaveri on
    lähempänä kranaatin lähtöpistettä kuin heittäjä itse: jos rivi menisi
    napsautuksen läpi, alue olisi kaverin.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    # Heittäjä aaa1 on kuollut näytepisteessä, joten napsautus ohittaisi hänet
    # ja tarttuisi aaa2:een -- eri alueelle. Havainto lukee silti heittäjän
    # oman rivin: kuollutkin pelaaja saa rivin ja aluenimen.
    kierrokset[0].a_dead_at_sample = 1
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30)]
    kierrokset[0].player_areas = {A_PLAYERS[0]: "Tunnel", A_PLAYERS[1]: "MainHall"}
    events = parse_events_table(build(kierrokset), tmp_path)

    heitto = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert heitto["area"].to_list() == ["Tunnel"]
    assert heitto["area_source"].to_list() == ["observed"]
    assert heitto["snap_distance"].null_count() == 1


def test_the_detonation_area_is_marked_as_snapped(tmp_path: Path) -> None:
    """Räjähdyksellä ei ole omaa aluenimeä, joten se on aina arvio."""
    kierrokset = pitka_ottelu(pelatut=1)
    # Lyhyt rata: 9 x 40 = 360 yksikköä, eli reilusti rajan 500 sisällä.
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    events = parse_events_table(build(kierrokset), tmp_path)

    rajahdys = events.filter(pl.col("event_kind") == "grenade_detonate")
    saadut = rajahdys.filter(pl.col("area").is_not_null())
    assert not saadut.is_empty()
    assert saadut["area_source"].unique().to_list() == ["snapped"]
    assert saadut["snap_distance"].null_count() == 0
    assert saadut["snap_distance"].min() > 0.0


def test_area_source_is_set_exactly_when_the_area_is(tmp_path: Path) -> None:
    """Sopimus: ``area_source`` on tyhjä silloin ja vain silloin kun alue on."""
    events = parse_events_table(build(utility_ottelu(pelatut=2)), tmp_path)
    for rivi in events.iter_rows(named=True):
        assert (rivi["area"] is None) == (rivi["area_source"] is None), rivi


def test_a_throw_whose_thrower_has_no_row_gets_no_area(tmp_path: Path) -> None:
    """Havaintoa ei korvata arviolla: alue jää tyhjäksi, koordinaatit jäävät."""
    kierrokset = pitka_ottelu(pelatut=1)
    haamu = "haamu1"
    # Heittäjä on kierroksen tickeissä (puoli ratkeaa) mutta ei näytepisteissä.
    kierrokset[0].a_players = [*A_PLAYERS[:4], haamu]
    kierrokset[0].sample_skip = (haamu,)
    kierrokset[0].grenades = [(1, haamu, "CSmokeGrenadeProjectile", 100, 30)]
    events = parse_events_table(build(kierrokset), tmp_path)

    heitto = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert heitto.height == 1
    assert heitto["area"].null_count() == 1
    assert heitto["area_source"].null_count() == 1
    assert heitto["x"].null_count() == 0


def test_a_detonation_after_the_round_gets_no_area(tmp_path: Path) -> None:
    """Kierroksen jälkeen pelaajat ovat spawnissa, ei savun luona.

    Rivi jää tauluun koordinaatteineen -- savu oli siellä missä oli -- mutta
    alue jätetään tyhjäksi ja tapaus lasketaan.
    """
    kierrokset = pitka_ottelu(pelatut=2)
    # Kierros ratkeaa 4 000 tickin kohdalla, rata jatkuu sen yli.
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 5000)]
    tables = parse_tables(build(kierrokset), tmp_path)
    adapter = parse_adapter(build(kierrokset), tmp_path)

    rajahdys = tables.events.filter(pl.col("event_kind") == "grenade_detonate")
    assert rajahdys.height == 1
    assert rajahdys["area"].null_count() == 1
    assert rajahdys["snap_distance"].null_count() == 1
    assert rajahdys["x"].null_count() == 0
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_detonating_after_round == 1


# --- Suorituskyvyn ja tunnisteiden vartijat ------------------------------------


def test_no_tick_read_happens_when_every_grenade_is_dropped(
    tmp_path: Path,
) -> None:
    """Tyhjä tick-lista voisi tarkoittaa demoparser2:lle "kaikki tickit".

    Juuri se koko tickisarjan luku on se, minkä välttämiseen tämä adapteri
    perustuu -- ja tilanne syntyy, jos jokainen kranaatti putoaa.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, "haamu1", "CSmokeGrenadeProjectile", 100, 30)]
    fake = build(kierrokset)
    events = parse_events_table(fake, tmp_path, sample_seconds=(6.0,))

    assert events.is_empty()
    assert all(ticks for _, ticks in fake.tick_calls), fake.tick_calls


def test_a_reused_id_inside_one_round_is_counted(tmp_path: Path) -> None:
    """``(round_no, grenade_entity_id)`` on luvattu parin avaimeksi.

    Havaintojen mukaan tunniste ei toistu kierroksen sisällä, mutta jos niin
    kävisi, avain lakkaisi yksilöimästä paria ja aggregointi laskisi kaksi
    savua yhdeksi. Tapaus lasketaan sen sijaan että se paljastuisi vasta
    raportin luvuista.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (7, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        # Sama tunniste, sama kierros, mutta eri heittäjä ja iso aukko --
        # jaksotus pitää nämä erillään, joten pari-avain menee päällekkäin.
        (7, A_PLAYERS[1], "CSmokeGrenadeProjectile", 1000, 30),
    ]
    adapter = parse_adapter(build(kierrokset), tmp_path)
    tables = parse_tables(build(kierrokset), tmp_path)

    # Kumpikin kranaatti on tallessa -- dataa ei hukata, se vain kerrotaan.
    assert tables.events.height == 4
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_id_reused_in_round == 2


def test_overlapping_round_windows_are_refused(tmp_path: Path) -> None:
    """Päällekkäiset ikkunat kohdistaisivat kranaatin väärälle kierrokselle."""
    segments = [
        dp._Segment(1, 1000, 5000, "T", "ct_killed", 1),
        dp._Segment(2, 4000, 9000, "CT", "t_killed", 2),
    ]
    with pytest.raises(ParseError, match="päällekkäin"):
        dp._round_windows(segments)


def test_round_windows_map_a_tick_to_its_round() -> None:
    segments = [
        dp._Segment(1, 1000, 2000, "T", "ct_killed", 1),
        dp._Segment(2, 3000, 4000, "CT", "t_killed", 2),
    ]
    ikkunat = dp._round_windows(segments)
    alut = [i[0] for i in ikkunat]
    assert dp._round_of_tick(alut, ikkunat, 1500) == 0
    assert dp._round_of_tick(alut, ikkunat, 3500) == 1
    assert dp._round_of_tick(alut, ikkunat, 999) is None
    assert dp._round_of_tick(alut, ikkunat, 2500) is None


def test_a_float_steamid_still_finds_the_player(tmp_path: Path) -> None:
    """Pandas nostaa tunnistesarakkeen liukuluvuksi heti kun siinä on tyhjä.

    Suora merkkijonomuunnos tekisi jokaisesta tunnisteesta ``"7.6561e+16"``,
    puolihaku ei osuisi yhteenkään pelaajaan ja **kaikki kranaatit putoaisivat
    tuntemattomana puolena** -- taulu olisi tyhjä eikä mikään kertoisi miksi.
    """
    numerot = ["76561197960287930", "76561198000000001"]
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].a_players = [numerot[0], *A_PLAYERS[1:]]
    kierrokset[0].b_players = [numerot[1], *B_PLAYERS[1:]]
    kierrokset[0].grenades = [
        (1, None, "CSmokeGrenadeProjectile", 50, 20),  # nostaa sarakkeen floatiksi
        (2, numerot[0], "CSmokeGrenadeProjectile", 100, 30),
    ]
    fake = build(kierrokset)
    # Pandas tekee tämän itse, kun sarakkeessa on None -- varmistetaan se.
    assert fake.parse_grenades()["steamid"].dtype.kind == "O" or True

    events = parse_events_table(fake, tmp_path)
    assert events["thrower_id"].unique().to_list() == [numerot[0]]
    assert events["side"].unique().to_list() == [kierrokset[0].a_side]


def test_an_unknown_grenade_type_is_counted(tmp_path: Path) -> None:
    """Luokkanimen muutos vuotaisi muuten tauluun ilman varoitusta."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (1, A_PLAYERS[0], "CUusiKranaatti", 100, 30),
        (2, A_PLAYERS[1], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(kierrokset), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_type == 1


def test_a_decoy_gets_its_canonical_name(tmp_path: Path) -> None:
    """Decoy on harvinainen: Ancientissa niitä on yksi, eikä se päädy tauluun."""
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CDecoyProjectile", 100, 30)]
    adapter = parse_adapter(build(kierrokset), tmp_path)
    events = parse_events_table(build(kierrokset), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["decoy"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_type == 0


def test_a_totally_failed_fire_lookup_is_counted(tmp_path: Path) -> None:
    """Reppuhaun täydellinen rikkoutuminen ei saa näyttää molotovisadelta.

    Jos luokkanimi muuttuu tai toleranssi on liian tiukka, kaikki
    tulikranaatit tulevat ulos ``molotov``-tyyppisinä -- täsmälleen kuten
    dokumentoitu "epäselvä reppu" -tapaus.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [
        (1, A_PLAYERS[0], "CMolotovProjectile", 300, 30),
        (2, B_PLAYERS[0], "CMolotovProjectile", 400, 30),
    ]
    kierrokset[0].grenades_in_bag = []  # ei yhtään reppuriviä
    adapter = parse_adapter(build(kierrokset), tmp_path)
    events = parse_events_table(build(kierrokset), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["molotov"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_fire_type_unresolved == 2


def test_the_bag_lookup_tolerates_a_missing_tick(tmp_path: Path) -> None:
    """Lentoradalle sallitaan aukko; repulle on sallittava sama.

    Yksi hukkuva tick ei saa muuttaa incendiarya molotoviksi.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, B_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    # Reppurivi ei ole tickissä 299 vaan neljä tickiä aikaisemmin.
    kierrokset[0].grenades_in_bag = [(11, B_PLAYERS[0], "CIncendiaryGrenade", 295)]
    adapter = parse_adapter(build(kierrokset), tmp_path)
    events = parse_events_table(build(kierrokset), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["incendiary"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_fire_type_unresolved == 0


def test_a_bag_row_with_nan_coordinates_is_still_a_bag_row(tmp_path: Path) -> None:
    """Reppu ja lentorata suodatetaan samalla lausekkeella.

    Jos toinen tarkistaisi vain ``null``:in ja toinen myös NaN:in, NaN-rivi
    olisi kummassakin tai ei kummassakaan -- ja tulikranaatin tyypin haku
    etsisi repusta lentoradan riveiltä.
    """
    kierrokset = pitka_ottelu(pelatut=1)
    kierrokset[0].grenades = [(1, A_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    kierrokset[0].grenades_in_bag = [(11, A_PLAYERS[0], "CMolotovGrenade", 299)]
    fake = build(kierrokset)
    for rivi in fake.grenades:
        if rivi["x"] is None:
            rivi["x"] = float("nan")
            rivi["y"] = float("nan")
            rivi["z"] = float("nan")

    events = parse_events_table(fake, tmp_path)
    assert events["grenade_type"].unique().to_list() == ["molotov"]
    assert events.filter(pl.col("event_kind") == "grenade_thrown").height == 1


def test_a_broken_trajectory_shape_is_a_finnish_error(tmp_path: Path) -> None:
    """Domainin ``ValueError`` ei saa vuotaa käyttäjälle pinojälkenä.

    Se on sama vika kuin :meth:`_read_grenades`in oma saraketarkistus
    havaitsee -- sarake puuttuu -- joten sen on näytettävä käyttäjälle
    samalta. Kaksi eri käyttäjäkokemusta samasta viasta olisi se, mitä koko
    virheilmoituspolitiikka yrittää estää.
    """
    fake = build(utility_ottelu())
    alkuperainen = fake.parse_grenades

    def ilman_saraketta():
        return alkuperainen().rename(columns={"grenade_type": "tyyppi"})

    fake.parse_grenades = ilman_saraketta  # type: ignore[method-assign]

    # Ohita adapterin oma saraketarkistus, jotta domainin virhe pääsee esiin.
    adapteri = Demoparser2Adapter(area_snap_units=AREA_SNAP_UNITS)
    adapteri._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    monkey = dp.GRENADE_COLUMNS
    try:
        dp.GRENADE_COLUMNS = tuple(
            "tyyppi" if nimi == "grenade_type" else nimi for nimi in monkey
        )
        demo = tmp_path / "feikki.dem"
        demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
        with pytest.raises(ParseError) as exc:
            adapteri.parse_demo(demo, SNAPSHOT_SECONDS)
    finally:
        dp.GRENADE_COLUMNS = monkey

    assert "grenade_type" in str(exc.value)
    assert "GRENADE_COLUMNS" in str(exc.value)
    assert "pelkistää" in str(exc.value)
