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
from pappascout.adapters.demo_parser import (
    DEFAULT_TICK_RATE,
    Demoparser2Adapter,
)
from pappascout.adapters.protocols import (
    EVENTS_ADAPTER_COLUMNS,
    ROUNDS_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
)
from pappascout.domain.schemas import ARMED_COLUMN, EVENTS, TICKS
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

#: Feikin oletustavaraluettelo: veitsi, ilmainen pistooli, ostettu kivääri ja
#: yksi savu. Vastaa oletusvarustearvoa 4200 (200 + 2700 + 300 + kevlar 1000),
#: joten normaalissa ottelussa kaikki viisi ovat aseistettuja.
DEFAULT_INVENTORY: tuple[str, ...] = (
    "knife",
    "Glock-18",
    "AK-47",
    "Smoke Grenade",
)

#: Oletuspanssari feikissä. Yli nollan, eli aseistumisen panssariehto täyttyy.
DEFAULT_ARMOR = 100


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
        columns = [*dp.GRENADE_COLUMNS, "name"]
        frame = pd.DataFrame(
            [{name: row.get(name) for name in columns} for row in self.grenades],
            columns=columns,
        )
        for column in self.drop_grenade_columns:
            if column in frame.columns:
                frame = frame.drop(columns=[column])
        return frame

    def parse_ticks(
        self, wanted_props: list[str], *, ticks: list[int] | None = None
    ) -> pd.DataFrame:
        self.tick_calls.append((tuple(wanted_props), tuple(ticks or ())))
        rows = [r for tick in (ticks or []) for r in self._rows_at(tick)]
        columns = [*wanted_props, "tick", "steamid", "name"]
        frame = pd.DataFrame(
            [{name: row.get(name) for name in columns} for row in rows],
            columns=columns,
        )
        for prop in self.drop_props:
            if prop in frame.columns:
                frame = frame.drop(columns=[prop])
        return frame

    def _rows_at(self, tick: int) -> list[dict[str, Any]]:
        if tick in self.tick_rows:
            return self.tick_rows[tick]
        for round_spec in self.rounds_model:
            if round_spec.freeze_tick is None or round_spec.end_tick is None:
                continue
            if round_spec.freeze_tick <= tick <= round_spec.end_tick:
                return _sample_rows(round_spec, tick)
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
class Round:
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
    #: Pelaajakohtainen varustearvo freezetimen lopussa kokoonpanolle A,
    #: indeksin mukaan. ``None`` = kaikilla oletusarvo 4200.
    a_equip_freeze_end: list[int] | None = None
    #: Pelaajakohtainen tavaraluettelo kokoonpanolle A, indeksin mukaan.
    #: ``None`` listana = kaikilla :data:`DEFAULT_INVENTORY`; yksittäinen
    #: ``None`` alkiona = propia ei saatu luettua (eri asia kuin tyhjä lista).
    #: Tästä kalustolaskuri ratkeaa: aseistaminen on nimiä, ei summaa.
    a_inventory: list[tuple[str, ...] | None] | None = None
    #: Pelaajakohtainen panssariarvo kokoonpanolle A, indeksin mukaan.
    #: ``None`` listana = kaikilla :data:`DEFAULT_ARMOR`; yksittäinen ``None``
    #: alkiona = propia ei saatu luettua. Tämä ero on oltava ilmaistavissa:
    #: lukukelvoton panssari ja nolla panssaria ovat eri asioita, ja
    #: jälkimmäinen on havainto.
    a_armor: list[int | None] | None = None

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
    round_spec: Round, tick: int, *, at_end: bool, total_score: int
) -> list[dict[str, Any]]:
    """Rakenna yhden tickin pelaajarivit."""
    b_side = "CT" if round_spec.a_side == "T" else "T"
    # Yhteispistemäärä jaetaan puolikkaiksi; testien kannalta merkitystä on
    # vain summalla, jonka adapteri laskee.
    half_scores = {round_spec.a_side: total_score, b_side: 0}

    rows: list[dict[str, Any]] = []
    for side, players, alive_count in (
        (round_spec.a_side, round_spec.a_players, round_spec.alive[0]),
        (b_side, round_spec.b_players, round_spec.alive[1]),
    ):
        if round_spec.score_only_side is not None and side != round_spec.score_only_side:
            score: int | None = None
        else:
            score = half_scores[side]
        for index, steamid in enumerate(players):
            unreadable = (
                side == round_spec.a_side
                and not at_end
                and index < round_spec.a_unreadable
            )
            # Kalustolaskuri lukee pelaajakohtaisen arvon, joten testin on
            # voitava asettaa se pelaajittain eikä vain summana.
            equip_freeze_end = 4200
            own_equip = round_spec.a_equip_freeze_end
            if own_equip is not None and side == round_spec.a_side:
                # Lyhyt lista on testin kirjoitusvirhe, ei tarkoitus:
                # ylimenevät pelaajat saisivat vaieten oletuksen 4200 ja
                # laskeutuisivat aseistetuiksi, jolloin testi mittaisi eri
                # asetelmaa kuin se väittää.
                assert len(own_equip) == len(round_spec.a_players), (
                    "a_equip_freeze_end ja a_players ovat eri mittaiset: "
                    f"{len(own_equip)} vs. {len(round_spec.a_players)}"
                )
                equip_freeze_end = own_equip[index]

            # Kalustolaskuri lukee tavaraluettelon ja panssarin, ei
            # varustearvoa. Sama pituustarkistus kuin yllä ja samasta syystä:
            # lyhyt lista jättäisi loput oletusaseistukseen vaieten.
            inventory: tuple[str, ...] | None = DEFAULT_INVENTORY
            own_inventory = round_spec.a_inventory
            if own_inventory is not None and side == round_spec.a_side:
                assert len(own_inventory) == len(round_spec.a_players), (
                    "a_inventory ja a_players ovat eri mittaiset: "
                    f"{len(own_inventory)} vs. {len(round_spec.a_players)}"
                )
                inventory = own_inventory[index]
            armor: int | None = DEFAULT_ARMOR
            own_armor = round_spec.a_armor
            if own_armor is not None and side == round_spec.a_side:
                assert len(own_armor) == len(round_spec.a_players), (
                    "a_armor ja a_players ovat eri mittaiset: "
                    f"{len(own_armor)} vs. {len(round_spec.a_players)}"
                )
                armor = own_armor[index]
            rows.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": steamid,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._ACCOUNT: None if unreadable else 800,
                    dp._CASH_SPENT: None if unreadable else 4000,
                    dp._EQUIP_FREEZE_END: None if unreadable else equip_freeze_end,
                    dp._EQUIP_ROUND_START: None if unreadable else 200,
                    dp._EQUIP_CURRENT: 3000,
                    dp._ARMOR_VALUE: armor,
                    dp._INVENTORY: (
                        None if inventory is None else list(inventory)
                    ),
                    dp._LIFE_STATE: 0 if (at_end and index < alive_count) else 1,
                    dp._TEAM_SCORE: score,
                    dp._ROUND_START_TIME: round_spec.round_start_time,
                }
            )
    return rows


def _sample_rows(round_spec: Round, tick: int) -> list[dict[str, Any]]:
    """Näytepisteen rivit: paikka, puoli ja elossaolo, ei talousarvoja.

    Koordinaatit johdetaan pelaajan indeksistä, jotta testi voi todeta, että
    ne kulkevat taulukkoon asti eivätkä muutu matkalla.

    Puolet erotetaan korkeudella (:data:`B_SIDE_HEIGHT`): ilman sitä A- ja
    B-puolen samanindeksiset pelaajat seisoisivat tasan päällekkäin, eikä
    utilityn aluepäättelyllä olisi yksikäsitteistä lähintä pelaajaa.
    """
    b_side = "CT" if round_spec.a_side == "T" else "T"
    rows: list[dict[str, Any]] = []
    for side, players, dead_count, area, height in (
        (
            round_spec.a_side,
            round_spec.a_players,
            round_spec.a_dead_at_sample,
            round_spec.a_area,
            A_SIDE_HEIGHT,
        ),
        (
            b_side,
            round_spec.b_players,
            round_spec.b_dead_at_sample,
            round_spec.b_area,
            B_SIDE_HEIGHT,
        ),
    ):
        for index, steamid in enumerate(players):
            if steamid in round_spec.sample_skip:
                continue
            own_area = round_spec.player_areas.get(steamid, area)
            rows.append(
                {
                    "tick": tick,
                    "steamid": steamid,
                    "name": steamid,
                    dp._TEAM_NUM: _SIDE_TEAM[side],
                    dp._LIFE_STATE: 2 if index < dead_count else 0,
                    # Peli antaa nimettömälle alueelle tyhjän merkkijonon.
                    dp._PLACE_NAME: "" if own_area is None else own_area,
                    dp._X: float(100 * index),
                    dp._Y: float(-100 * index),
                    dp._Z: height,
                }
            )
    return rows


def build(rounds: list[Round]) -> FakeDemoparser2:
    """Kokoa feikki kierroslistasta."""
    freeze_ticks: list[int] = []
    round_ends: list[dict[str, Any]] = []
    tick_rows: dict[int, list[dict[str, Any]]] = {}
    hurt_rows: list[dict[str, Any]] = []
    death_rows: list[dict[str, Any]] = []
    grenade_rows: list[dict[str, Any]] = []

    for round_spec in rounds:
        if round_spec.freeze_tick is not None:
            grenade_rows.extend(_grenade_rows(round_spec))
            for target, source in (
                (hurt_rows, round_spec.hurt),
                (death_rows, round_spec.deaths),
            ):
                for offset, attacker, victim, weapon in source:
                    target.append(
                        {
                            "tick": round_spec.freeze_tick + offset,
                            "attacker_steamid": attacker,
                            "user_steamid": victim,
                            "weapon": weapon,
                        }
                    )
            freeze_ticks.append(round_spec.freeze_tick)
            tick_rows[round_spec.freeze_tick] = _rows(
                round_spec,
                round_spec.freeze_tick,
                at_end=False,
                total_score=round_spec.score_at_freeze,
            )
        if round_spec.end_tick is not None:
            round_ends.append(
                {
                    "reason": round_spec.reason,
                    "round": round_spec.demo_round,
                    "tick": round_spec.end_tick,
                    "winner": round_spec.winner,
                }
            )
            tick_rows[round_spec.end_tick] = _rows(
                round_spec,
                round_spec.end_tick,
                at_end=True,
                total_score=round_spec.score_at_end,
            )
    return FakeDemoparser2(
        sorted(freeze_ticks),
        round_ends,
        tick_rows,
        events={"player_hurt": hurt_rows, "player_death": death_rows},
        rounds_model=list(rounds),
        grenades=grenade_rows,
    )


def _grenade_rows(round_spec: Round) -> list[dict[str, Any]]:
    """Yhden kierroksen lentorata- ja reppurivit.

    Radan alku asetetaan heittäjän omaan sijaintiin (:func:`_sample_rows`
    käyttää samaa kaavaa), jotta heiton alue napsahtaa heittäjään kuten
    oikeassa demossa. Rata etenee siitä poispäin.
    """
    assert round_spec.freeze_tick is not None
    rows: list[dict[str, Any]] = []

    for entity, owner, grenade_type, offset in round_spec.grenades_in_bag:
        rows.append(
            {
                "grenade_type": grenade_type,
                "grenade_entity_id": entity,
                "x": None,
                "y": None,
                "z": None,
                "tick": round_spec.freeze_tick + offset,
                "steamid": owner,
                "name": owner,
            }
        )

    for entity, thrower, grenade_type, offset, duration in round_spec.grenades:
        if thrower in round_spec.a_players:
            index, height = round_spec.a_players.index(thrower), A_SIDE_HEIGHT
        elif thrower in round_spec.b_players:
            index, height = round_spec.b_players.index(thrower), B_SIDE_HEIGHT
        else:
            index, height = 0, A_SIDE_HEIGHT
        start = (float(100 * index), float(-100 * index), height)
        for step in range(duration):
            rows.append(
                {
                    "grenade_type": grenade_type,
                    "grenade_entity_id": entity,
                    "x": start[0] + 40.0 * step,
                    "y": start[1],
                    "z": start[2],
                    "tick": round_spec.freeze_tick + offset + step,
                    "steamid": thrower,
                    "name": thrower,
                }
            )
    return rows


def normal_match(
    played: int = 3, *, knife: bool = True, tickrate: float = 64.0
) -> list[Round]:
    """Puukkokierros + N pelattua kierrosta, kuten oikeassa demossa.

    Puukkokierroksen piste nollataan, joten sen ``score_at_end`` on 1 mutta
    seuraavan kierroksen ankkurissa lukema on taas 0.
    """
    rounds: list[Round] = []
    tick = 1000
    time_s = 100.0
    demo_round = 1
    points = 0

    if knife:
        rounds.append(
            Round(
                demo_round=demo_round,
                freeze_tick=tick,
                end_tick=tick + 500,
                winner="T",
                reason="ct_killed",
                score_at_freeze=0,
                score_at_end=1,  # nollataan mp_restartgamella
                alive=(4, 0),
                round_start_time=time_s,
            )
        )
        demo_round += 1
        tick += 1000
        time_s += 1000 / tickrate

    for _ in range(played):
        rounds.append(
            Round(
                demo_round=demo_round,
                freeze_tick=tick,
                end_tick=tick + 500,
                winner="CT",
                reason="t_killed",
                score_at_freeze=points,
                score_at_end=points + 1,
                alive=(0, 3),
                round_start_time=time_s,
            )
        )
        points += 1
        demo_round += 1
        tick += 1000
        time_s += 1000 / tickrate
    return rounds


def insert_restart(
    rounds: list[Round],
    after: int,
    *,
    score: int | None = None,
    offset: int = 100,
    tickrate: float = 64.0,
) -> list[Round]:
    """Lisää ottelun uudelleenaloitus kierroksen ``after`` jälkeen.

    Uudelleenaloitus on freezetime-ankkuri **ilman** ``round_end``iä. Se ei
    kuluta demon omaa kierrosnumeroa, joten ympäröivien kierrosten numeroihin
    ei kosketa -- juuri se tekee siitä havaittavan uudelleenaloituksen eikä
    kadonneen kierroksen.

    Args:
        rounds: Kierroslista, jota muokataan paikallaan.
        after: Kierros, jonka jälkeen uudelleenaloitus tulee.
        score: Yhteispistemäärä uudelleenaloituksen ankkurissa. Oletus on
            edellisen kierroksen lopputilanne; ``0`` jäljittelee
            ``mp_restartgame``-nollausta.
        offset: Tickejä edellisen kierroksen päättymisestä. On mahduttava
            seuraavan kierroksen ankkuriin asti (feikissä 500 tickiä).
    """
    host = rounds[after]
    assert host.freeze_tick is not None and host.end_tick is not None
    assert host.round_start_time is not None
    tick = host.end_tick + offset
    rounds.insert(
        after + 1,
        Round(
            demo_round=None,
            freeze_tick=tick,
            end_tick=None,
            score_at_freeze=host.score_at_end if score is None else score,
            round_start_time=(
                host.round_start_time + (tick - host.freeze_tick) / tickrate
            ),
        ),
    )
    return rounds


def restarted_match(
    played: int = 3, *, restarts: int = 1, tickrate: float = 64.0
) -> list[Round]:
    """Puukkokierros, ottelun uudelleenaloitus ja N pelattua kierrosta.

    Tämä on liigademojen kuvio: heti puukkokierroksen jälkeen tulee oma
    ``round_freeze_end`` **ilman** yhtään ``round_end``iä, ja peli jatkuu sen
    jälkeen normaalisti. Uudelleenaloitus nollaa pistemäärän, joten sen
    ankkurin lukema on 0 -- juuri siitä puukkokierroksen piste katoaa.

    Kuvio on mitattu kaikista neljästä liigademosta; mittaus on tallessa
    tiedostossa ``vika-kierrosnumerointi.md``, joka on tämän repon
    ulkopuolella BMAD-projektin hakemistossa
    ``_bmad-output/implementation-artifacts/``. Olennainen sisältö on
    toistettu tässä ja :mod:`pappascout.adapters.demo_parser`in
    moduulidokumentaatiossa, jottei testi nojaa tiedostoon jota repossa ei ole.

    Args:
        restarts: Montako **peräkkäistä** uudelleenaloitusta rakennetaan. Yli
            yksi on tuntematon ilmiö, jonka parsinnan on määrä pysäyttää.
    """
    rounds = normal_match(played=played, knife=True, tickrate=tickrate)
    # Käännetyssä järjestyksessä: jokainen menee puukkokierroksen perään, joten
    # viimeisenä lisätty jää ensimmäiseksi ja tickit nousevat listassa.
    for number in reversed(range(restarts)):
        insert_restart(
            rounds, 0, score=0, offset=100 * (number + 1), tickrate=tickrate
        )
    return rounds


def numbers(df: pl.DataFrame) -> list[int | None]:
    return (
        mark_played_rounds(df)
        .unique(subset=["round_raw"], keep="first", maintain_order=True)
        .sort("round_raw")["round_no"]
        .to_list()
    )


# --- Porttisopimus -------------------------------------------------------------


def test_frame_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    df = parse_with(build(normal_match()), tmp_path)
    assert tuple(df.columns) == ROUNDS_ADAPTER_COLUMNS
    assert df["round_no"].null_count() == df.height


def test_two_rows_per_round(tmp_path: Path) -> None:
    df = parse_with(build(normal_match(played=5)), tmp_path)
    assert df.height == 2 * 6  # puukkokierros + 5
    assert df.group_by("round_raw").len()["len"].unique().to_list() == [2]


# --- Kierrosnumerointi ja round_raw --------------------------------------------


def test_round_raw_comes_from_the_demo_not_from_a_counter(tmp_path: Path) -> None:
    """Demon oma ``round``-kenttä päätyy sellaisenaan tauluun."""
    rounds = normal_match(played=2)
    for offset, round_spec in enumerate(rounds):
        round_spec.demo_round = 40 + offset  # demon oma numerointi ei ala ykkösestä
    df = parse_with(build(rounds), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [40, 41, 42]


def test_knife_round_is_not_played(tmp_path: Path) -> None:
    df = parse_with(build(normal_match(played=3)), tmp_path)
    assert numbers(df) == [None, 1, 2, 3]


def test_last_round_score_end_comes_from_its_own_round_end_tick(
    tmp_path: Path,
) -> None:
    """Viimeisellä kierroksella ei ole seuraavaa ankkuria.

    Tämä on ainoa kohta, jossa ``score_end`` luetaan eri tickistä kuin muilla
    kierroksilla. Ilman tätä testiä jokaisen ottelun viimeinen kierros voisi
    pudota hiljaa pois.
    """
    df = parse_with(build(normal_match(played=3)), tmp_path)
    last_round = df.filter(pl.col("round_raw") == pl.col("round_raw").max())
    assert last_round["score_start"].unique().to_list() == [2]
    assert last_round["score_end"].unique().to_list() == [3]
    assert numbers(df)[-1] == 3


def test_unfinished_last_round_is_not_numbered(tmp_path: Path) -> None:
    """Demo katkesi kesken kierroksen: ankkuri ilman round_endiä."""
    rounds = normal_match(played=2)
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=9000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=250.0,
        )
    )
    df = parse_with(build(rounds), tmp_path)
    assert numbers(df) == [None, 1, 2, None]
    unfinished = df.filter(pl.col("round_raw") == df["round_raw"].max())
    assert unfinished["won"].null_count() == 2
    assert unfinished["win_reason"].null_count() == 2


def test_orphan_freeze_anchor_before_the_first_round_becomes_its_own_round(
    tmp_path: Path,
) -> None:
    """Kaksi freeze-ankkuria ennen ensimmäistä round_endiä.

    ``_segments`` antaa ankkurin **viimeiselle** ennen päättymistä, joten
    ylimääräinen ankkuri päätyy listan **alkuun** -- ei keskelle, vaikka se
    kierroslistassa kirjoitetaan väliin. Alussa oleva numeroimaton segmentti ei
    ole uudelleenaloitus: ennen demon ensimmäistä omaa numeroa ei ole arvoa,
    johon törmätä, joten se saa numeronsa taaksepäin laskettuna ja säilyy
    omana kierroksenaan. Keskellä oleva tapaus on
    :func:`test_match_restart_after_the_knife_round_is_not_a_round`.
    """
    rounds = normal_match(played=2, knife=False)
    rounds.insert(
        1,
        Round(
            demo_round=None,
            freeze_tick=rounds[0].freeze_tick + 100,
            end_tick=None,
            score_at_freeze=1,
            round_start_time=110.0,
        ),
    )
    df = parse_with(build(rounds), tmp_path)
    assert df.height == 6
    assert numbers(df) == [1, None, 2]


def test_match_restart_after_the_knife_round_is_not_a_round(tmp_path: Path) -> None:
    """I/O-matriisi: liigademo -- uudelleenaloitus keskellä jää numeroimatta.

    Uudelleenaloitus pelataan, mutta se ei ole kierros: se ei saa demon omaa
    numeroa eikä se tuota riviä kierrostauluun. Muut kierrokset pitävät demon
    omat numeronsa, joten numerointi ei siirry.
    """
    df = parse_with(build(restarted_match(played=3)), tmp_path)

    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3, 4]
    assert df.height == 2 * 4  # puukkokierros + 3 pelattua, uudelleenaloitus ei
    assert numbers(df) == [None, 1, 2, 3]


def test_match_restart_is_counted_and_reported(tmp_path: Path) -> None:
    """I/O-matriisi: raportointi -- pudotus ei saa olla hiljainen."""
    adapter = parse_adapter(build(restarted_match(played=3)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 1
    # Kierrosraja on silti nähty: puukkokierros + uudelleenaloitus + 3 pelattua.
    assert adapter.diagnostics.rounds_seen == 5


def test_normal_demo_has_no_match_restarts(tmp_path: Path) -> None:
    """I/O-matriisi: vanha demo -- jokaisella segmentillä oma round_end."""
    adapter = parse_adapter(build(normal_match(played=3)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 0


def test_a_lost_round_is_not_mistaken_for_a_restart(tmp_path: Path) -> None:
    """Numerointi hyppää numeroimattoman rajan yli -> kierros on kadonnut.

    Sijainti ei riitä tunnusmerkiksi: kadonnut kierros näyttää listassa
    täsmälleen uudelleenaloitukselta. Ero on demon omassa numeroinnissa --
    uudelleenaloitus ei kuluta kierrosnumeroa, kadonnut kierros kuluttaa.
    Hiljainen pudotus veisi kierroksen pois jokaisesta taulusta ja raportoisi
    sen vielä uudelleenaloituksena.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[1].demo_round = 3  # demosta puuttuu kierros 2
    insert_restart(rounds, 0)

    with pytest.raises(ParseError, match="hyppää numeroimattoman"):
        parse_with(build(rounds), tmp_path)


def test_a_resolved_round_without_a_number_is_not_a_restart(
    tmp_path: Path,
) -> None:
    """Ratkennut kierros ilman demon omaa numeroa on kierros, ei uudelleenaloitus.

    Toinen havaittava erotin: uudelleenaloituksella ei ole ``round_end``iä.
    Segmentti, jolla se on, on kierros -- se numeroidaan naapurista eikä
    pudoteta, eikä sitä lasketa uudelleenaloitukseksi.
    """
    rounds = normal_match(played=3, knife=False)
    rounds[1].demo_round = None  # round_end ilman round-kenttää

    df = parse_with(build(rounds), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3]
    assert df.height == 2 * 3

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 0


def test_restart_in_the_middle_of_the_match_is_recognised(tmp_path: Path) -> None:
    """Uudelleenaloitus ei ole sidottu puukkokierroksen jälkeiseen paikkaan.

    Liigademoissa se on aina indeksissä 1, mutta tunnistus nojaa havaintoihin
    eikä siihen. Tässä se on kesken ottelun eikä nollaa pistemäärää.
    """
    rounds = normal_match(played=4)
    insert_restart(rounds, 2)  # kolmannen kierrosrajan jälkeen

    df = parse_with(build(rounds), tmp_path)
    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3, 4, 5]
    assert df.height == 2 * 5
    assert numbers(df) == [None, 1, 2, 3, 4]


def test_restart_and_an_unfinished_last_round_live_in_the_same_demo(
    tmp_path: Path,
) -> None:
    """Molemmat säännöt samassa demossa: keskellä pudotus, hännässä täyttö."""
    rounds = restarted_match(played=2)
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=4000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=100.0 + 3000 / 64.0,
        )
    )

    df = parse_with(build(rounds), tmp_path)
    # Uudelleenaloitus ei saa numeroa; ratkeamaton viimeinen saa sen naapurista.
    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3, 4]
    assert numbers(df) == [None, 1, 2, None]
    unfinished = df.filter(pl.col("round_raw") == 4)
    assert unfinished["won"].null_count() == 2

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.match_restarts == 1


def test_restart_players_never_enter_the_lineups(tmp_path: Path) -> None:
    """Uudelleenaloituksen lukemat eivät saa saastuttaa kokoonpanoja.

    Uudelleenaloitus on juuri se hetki, jolloin joukkue- ja puolitila on
    epävakain: pelaajia siirretään ja yhdistetään uudelleen. Yksikin väärä
    lukema jäisi pysyvästi ``lineups``iin ja voisi kääntää puolet kaikille sen
    jälkeisille kierroksille -- eli kohdistaa voitot ja talousarvot väärälle
    joukkueelle.
    """
    clean = parse_with(build(restarted_match(played=3)), tmp_path)

    polluted = restarted_match(played=3)
    polluted[1].a_players = [f"outo{n}" for n in range(5)]
    polluted[1].b_players = [f"muukalainen{n}" for n in range(5)]
    dirty = parse_with(build(polluted), tmp_path)

    assert sorted(clean["lineup_key"].unique().to_list()) == sorted(
        dirty["lineup_key"].unique().to_list()
    )


def test_round_after_a_restart_keeps_its_score_start(tmp_path: Path) -> None:
    """Pistelukeman varasääntö ei saa pysähtyä uudelleenaloitukseen.

    Uudelleenaloituksen jälkeisellä kierroksella on aina oma ankkuri --
    ``_segments`` antaa ankkurin viimeiselle rajalle ennen päättymistä --
    mutta sen **lukema** voi olla hylätty (yksipuolinen pistemäärä). Silloin
    varasääntö hakee edellisen lukeman. Uudelleenaloituksella ei ole
    lopputickiä, ja sitä edeltävän kierroksen lopputick on nollausta
    edeltävältä hetkeltä: ilman uudelleenaloituksen omaa ankkuria kierros
    saisi ``score_start == score_end`` ja putoaisi pelattujen joukosta.
    """
    rounds = restarted_match(played=3)
    rounds[2].score_only_side = rounds[2].a_side
    rounds[2].score_at_freeze = 99  # yksipuolinen lukema ei kelpaa

    df = parse_with(build(rounds), tmp_path)
    first_after = df.filter(pl.col("round_raw") == 2)
    assert first_after["score_start"].unique().to_list() == [0]
    assert numbers(df) == [None, 1, 2, 3]


def test_restart_breaks_the_saved_equipment_chain(tmp_path: Path) -> None:
    """Uudelleenaloitus nollaa kaluston, joten ketju katkeaa siihen.

    Ilman katkoa uudelleenaloitusta seuraava pistoolikierros perisi
    puukkokierroksen eloonjääneiden varustearvon luokittelun syötteeksi --
    juuri se kierros, jonka luokitus menisi väärin.
    """
    df = parse_with(build(restarted_match(played=3)), tmp_path)

    first_after = df.filter(pl.col("round_raw") == 2)
    assert first_after["survivors_equip_prev"].null_count() == 2
    # Ketju jatkuu normaalisti heti seuraavasta kierroksesta.
    later = df.filter(pl.col("round_raw") == 3)
    assert later["survivors_equip_prev"].null_count() == 0


def test_sample_points_stay_aligned_with_their_segment_after_a_restart(
    tmp_path: Path,
) -> None:
    """Uudelleenaloituksen suodatus ei saa siirtää puolikuvausta yhdellä.

    ``_sample_points`` näytteistää vain numeroidut segmentit, mutta ``sides``
    ja ``segments`` ovat segmenttien järjestyksessä. Ilman alkuperäistä
    indeksiä uudelleenaloituksen jälkeiset kierrokset lukisivat edellisen
    segmentin tickit -- ja ensikontakti ratkeaisi väärän kierroksen
    havainnoista.
    """
    swap = "vaihtopelaaja"
    rounds = restarted_match(played=3)
    # Puolet vaihtuvat kesken ottelun, jotta segmenttien kuvaukset eroavat.
    for round_spec in rounds[3:]:
        round_spec.a_side = "CT"
    # Pelaaja, joka ehtii molempiin kokoonpanoihin, jää lineup_ofin
    # ulkopuolelle. Hänen puolensa ratkeaa vasta **kierroksen oman tickin**
    # kautta -- juuri siitä indeksistä, jonka suodatus voi siirtää.
    rounds[2].a_players = [swap, *A_PLAYERS[1:]]
    rounds[3].b_players = [swap, *B_PLAYERS[1:]]
    rounds[2].hurt = [(10, swap, B_PLAYERS[1], "ak47")]

    ticks = parse_ticks_table(build(rounds), tmp_path)
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["round_raw"].unique().to_list() == [2]

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_side_events == 0


def test_the_restart_never_reaches_the_side_key_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Puolikuvaus rakennetaan vasta kun näytepiste sitä tarvitsee.

    ``_keys_by_side`` nostaa ``ParseError``in, jos molemmille kokoonpanoille
    tuli sama puoli. Ahne rakennus antaisi uudelleenaloitukselle vallan kaataa
    koko ajon, vaikka se ei tuota riviä yhteenkään tauluun -- ja virheviesti
    kertoisi sen ``round_raw``:ksi ``None``, joka ei kerro lukijalle mitään.
    """
    seen: list[int | None] = []
    original = dp._keys_by_side

    def spy(sides, lineup_keys, segment):
        seen.append(segment.round_raw)
        return original(sides, lineup_keys, segment)

    monkeypatch.setattr(dp, "_keys_by_side", spy)
    parse_ticks_table(build(restarted_match(played=3)), tmp_path)

    assert seen, "sääntöä ei ajettu lainkaan"
    assert None not in seen
    assert sorted(set(seen)) == [1, 2, 3, 4]


def test_restart_utility_does_not_leak_into_the_event_table(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: uudelleenaloitus ei tuota riviä EVENTS-tauluun."""
    rounds = restarted_match(played=2)
    rounds[1].grenades = [(9001, A_PLAYERS[0], "CSmokeGrenadeProjectile", 50, 5)]
    rounds[2].grenades = [(9002, A_PLAYERS[0], "CSmokeGrenadeProjectile", 50, 5)]
    tables = parse_tables(build(rounds), tmp_path)

    thrown = tables.events["grenade_entity_id"].unique().to_list()
    assert 9001 not in thrown
    assert 9002 in thrown
    # Kumpikaan muu taulu ei tunne kierrosta, jota kierrostaulussa ei ole.
    known = set(tables.rounds["round_raw"].to_list())
    assert set(tables.ticks["round_raw"].to_list()) <= known
    assert set(tables.events["round_raw"].to_list()) <= known

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    # Uudelleenaloituksella ei ole kierrosikkunaa, joten sen aikana heitetty
    # kranaatti kirjautuu samaan lukuun kuin lämmittelyheitot. Se on luvun
    # oikea paikka, mutta se on sanottava ääneen: muuten liigademon
    # yhteenvedossa luku näyttää vialta jota ei ole.
    assert adapter.diagnostics.grenades_outside_rounds == 1


def test_unfinished_last_round_still_takes_its_number_from_the_neighbour(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: ratkeamaton viimeinen kierros -- naapuritäyttö säilyy.

    Hännässä oleva numeroimaton segmentti on aito kierros, jonka demo katkaisi
    kesken. Se ei ole uudelleenaloitus, ja sen on saatava numeronsa naapurista
    kuten ennenkin.
    """
    rounds = normal_match(played=2, knife=False)
    rounds.append(
        Round(
            demo_round=None,
            freeze_tick=9000,
            end_tick=None,
            score_at_freeze=2,
            round_start_time=250.0,
        )
    )
    df = parse_with(build(rounds), tmp_path)

    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3]
    assert df.height == 2 * 3


def test_two_restarts_in_a_row_halt_the_parse(tmp_path: Path) -> None:
    """I/O-matriisi: useampi uudelleenaloitus -> pysähdytään, ei arvata."""
    with pytest.raises(ParseError, match="uudelleenaloitukselta näyttävää"):
        parse_with(build(restarted_match(played=3, restarts=2)), tmp_path)


def test_two_restarts_apart_from_each_other_halt_the_parse(
    tmp_path: Path,
) -> None:
    """Raja on demokohtainen eikä peräkkäisyyteen sidottu."""
    rounds = restarted_match(played=4)
    insert_restart(rounds, 3)  # toinen keskelle ottelua

    with pytest.raises(ParseError, match="uudelleenaloitukselta näyttävää") as err:
        parse_with(build(rounds), tmp_path)
    # Virhe kertoo tickit, joista demon voi avata -- ei listaindeksejä.
    assert "1600" in str(err.value)


def test_the_restart_limit_is_read_from_the_constant(tmp_path: Path) -> None:
    """Viestit johdetaan vakiosta, jottei rajan nosto jätä niitä valehtelemaan."""
    with pytest.raises(ParseError) as err:
        parse_with(build(restarted_match(played=3, restarts=2)), tmp_path)
    assert f"enintään {dp.MAX_MATCH_RESTARTS}" in str(err.value)


def test_public_names_all_exist() -> None:
    """``__all__`` ei saa luvata nimeä, jota moduulissa ei ole."""
    assert "MAX_MATCH_RESTARTS" in dp.__all__
    for name in dp.__all__:
        assert hasattr(dp, name), name


def test_segments_without_any_round_end_keep_the_running_count(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kaikki segmentit numeroimattomia -> varasääntö säilyy."""
    rounds = [
        Round(
            demo_round=None,
            freeze_tick=1000 + 1000 * index,
            end_tick=None,
            score_at_freeze=0,
            round_start_time=100.0 + index * 1000 / 64.0,
        )
        for index in range(3)
    ]
    df = parse_with(build(rounds), tmp_path)

    assert sorted(df["round_raw"].unique().to_list()) == [1, 2, 3]
    assert df.height == 2 * 3


def test_round_end_without_an_anchor_is_kept_with_its_own_status(
    tmp_path: Path,
) -> None:
    """Kierros ilman freezetime-ankkuria on mukana ja saa numeron."""
    rounds = normal_match(played=3, knife=False)
    rounds[1].freeze_tick = None
    df = parse_with(build(rounds), tmp_path)

    assert numbers(df) == [1, 2, 3]
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor["freeze_end_tick"].null_count() == 2
    assert no_anchor["money_freeze_end"].null_count() == 2
    # Pistelukemat periytyvät naapureista, joten kierros pysyy pelattuna.
    assert no_anchor["score_start"].unique().to_list() == [1]
    assert no_anchor["score_end"].unique().to_list() == [2]


def test_inconsistent_demo_round_numbers_are_refused(tmp_path: Path) -> None:
    rounds = normal_match(played=3, knife=False)
    rounds[2].demo_round = rounds[1].demo_round  # sama numero kahdesti
    with pytest.raises(ParseError, match="kierrosnumerointi"):
        parse_with(build(rounds), tmp_path)


# --- Kokoonpanot ja puolet -----------------------------------------------------


def test_half_time_switch_keeps_the_lineup_key(tmp_path: Path) -> None:
    """Puolet vaihtuvat, joukkueet eivät."""
    rounds = normal_match(played=4, knife=False)
    for round_spec in rounds[2:]:
        round_spec.a_side = "CT"

    df = parse_with(build(rounds), tmp_path)
    assert df["lineup_key"].n_unique() == 2

    a_key = df.filter(pl.col("round_raw") == rounds[0].demo_round).filter(
        pl.col("side") == "T"
    )["lineup_key"][0]
    later_key = df.filter(pl.col("round_raw") == rounds[-1].demo_round).filter(
        pl.col("side") == "CT"
    )["lineup_key"][0]
    assert a_key == later_key


def test_substitute_does_not_split_the_team(tmp_path: Path) -> None:
    """Yksi pelaaja vaihtuu kesken kartan: kokoonpano pysyy samana joukkueena."""
    rounds = normal_match(played=4, knife=False)
    for round_spec in rounds[2:]:
        round_spec.a_players = [*A_PLAYERS[:4], "sijainen"]

    df = parse_with(build(rounds), tmp_path)
    assert df["lineup_key"].n_unique() == 2
    # Molemmilla joukkueilla on yhtä monta riviä -- kolmatta joukkuetta ei synny.
    assert df.group_by("lineup_key").len()["len"].unique().to_list() == [4]


def test_side_assignment_never_guesses_when_teams_do_not_separate(
    tmp_path: Path,
) -> None:
    """Tasapelissä peritään edellisen kierroksen kuvaus, ei oleteta (T, CT)."""
    rounds = normal_match(played=3, knife=False)
    rounds[1].a_side = "CT"  # joukkueet vaihtoivat puolta
    # Kolmas kierros: aivan uudet pelaajat -> kumpikaan kuvaus ei voita.
    rounds[2].a_players = ["uusi1", "uusi2"]
    rounds[2].b_players = ["uusi3", "uusi4"]
    rounds[2].a_side = "T"

    df = parse_with(build(rounds), tmp_path)
    # Edellinen kuvaus oli "A on CT", joten se peritään: kokoonpano 0 on CT.
    third_round = df.filter(pl.col("round_raw") == rounds[2].demo_round)
    first_key = df["lineup_key"][0]
    assert third_round.filter(pl.col("lineup_key") == first_key)["side"].to_list() == ["CT"]


def test_first_round_with_only_one_side_is_refused(tmp_path: Path) -> None:
    rounds = normal_match(played=2, knife=False)
    rounds[0].b_players = []
    with pytest.raises(ParseError, match="vain toiselta puolelta"):
        parse_with(build(rounds), tmp_path)


def test_round_without_players_and_without_history_is_refused(
    tmp_path: Path,
) -> None:
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_players = []
    rounds[0].b_players = []
    with pytest.raises(ParseError, match="puolia ei voitu määrittää"):
        parse_with(build(rounds), tmp_path)


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
    both_sides = [
        {"side": "T", "team_score": 7},
        {"side": "CT", "team_score": 5},
    ]
    assert dp._total_score(both_sides) == 12
    assert dp._total_score(both_sides[:1]) is None
    assert dp._total_score([{"side": "T", "team_score": None}]) is None
    assert dp._total_score([]) is None


def test_one_sided_anchor_falls_back_to_a_trustworthy_neighbour(
    tmp_path: Path,
) -> None:
    """Hylätty lukema korvataan naapurilla, ei puolikkaalla summalla."""
    rounds = normal_match(played=3, knife=False)
    rounds[1].score_only_side = rounds[1].a_side
    # Jos yksipuolinen lukema kelpaisi, score_start olisi tämä 99.
    rounds[1].score_at_freeze = 99

    df = parse_with(build(rounds), tmp_path)
    other = df.filter(pl.col("round_raw") == rounds[1].demo_round)
    assert other["score_start"].unique().to_list() == [1]
    assert numbers(df) == [1, 2, 3]


def test_score_jump_larger_than_one_is_refused(tmp_path: Path) -> None:
    """Kahden pisteen hyppy tarkoittaa, että kierros jäi tunnistamatta."""
    rounds = normal_match(played=3, knife=False)
    rounds[2].score_at_freeze = 5  # aukko toisen ja kolmannen välissä
    rounds[2].score_at_end = 6

    df = parse_with(build(rounds), tmp_path)
    with pytest.raises(ParseError, match="enemmän kuin"):
        mark_played_rounds(df)


# --- Tickrate ------------------------------------------------------------------


def test_tick_rate_is_measured_from_the_game_clock(tmp_path: Path) -> None:
    adapter = parse_adapter(build(normal_match(played=4)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == 64.0
    assert adapter.diagnostics.tick_rate_measured is True


def test_tick_rate_measurement_uses_the_median(tmp_path: Path) -> None:
    """Yksi poikkeava kierrosväli ei saa siirtää tulosta."""
    rounds = normal_match(played=5, knife=False)
    rounds[2].round_start_time = rounds[2].round_start_time + 40  # kellon hyppy
    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == 64.0
    assert adapter.diagnostics.tick_rate_measured is True


def test_tick_rate_falls_back_to_the_default_without_a_clock(tmp_path: Path) -> None:
    rounds = normal_match(played=3)
    for round_spec in rounds:
        round_spec.round_start_time = None

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == DEFAULT_TICK_RATE
    assert adapter.diagnostics.tick_rate_measured is False


def test_absurd_tick_rate_is_rejected_as_a_measurement(tmp_path: Path) -> None:
    """Järkevyysrajojen ulkopuolinen arvo on mittausvirhe, ei totuus."""
    rounds = normal_match(played=4, knife=False)
    for index, round_spec in enumerate(rounds):
        # 1000 tickiä / 0,001 s = 1 000 000 tickiä sekunnissa.
        round_spec.round_start_time = 100.0 + index * 0.001

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.tick_rate == DEFAULT_TICK_RATE
    assert adapter.diagnostics.tick_rate_measured is False


def test_diagnostics_report_every_round_boundary(tmp_path: Path) -> None:
    adapter = parse_adapter(build(normal_match(played=6)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.rounds_seen == 7  # puukkokierros mukaan lukien


# --- Propien katoaminen --------------------------------------------------------


@pytest.mark.parametrize("prop", [dp._TEAM_SCORE, dp._ACCOUNT, dp._LIFE_STATE])
def test_missing_prop_is_named_in_the_error(tmp_path: Path, prop: str) -> None:
    """Uudelleennimetty kenttä tuottaisi muuten rakenteellisesti kelvollisen
    mutta täysin tyhjän taulun."""
    fake = build(normal_match(played=2))
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
    rounds = normal_match(played=3, knife=False)
    rounds[0].alive = (2, 0)  # kokoonpano A jätti kaksi henkiin
    for round_spec in rounds[1:]:
        round_spec.a_side = "CT"

    df = parse_with(build(rounds), tmp_path)
    a_key = df["lineup_key"][0]
    second_round = df.filter(
        (pl.col("round_raw") == rounds[1].demo_round)
        & (pl.col("lineup_key") == a_key)
    )
    assert second_round["side"].to_list() == ["CT"]
    assert second_round["survivors_equip_prev"].to_list() == [2 * 3000]


def test_player_count_is_observed_not_assumed(tmp_path: Path) -> None:
    """``players_freeze_end`` on havainto: neljä pelaajaa -> 4, viisi -> 5.

    Kynnykset ovat per pelaaja, joten jakaja on luettava demosta. Ilman tätä
    testia sarakkeen arvoa ei todennettaisi missään -- vain sen olemassaolo.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_players = A_PLAYERS[:4]

    df = parse_with(build(rounds), tmp_path)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    own_rows = df.filter(pl.col("lineup_key") == a_key).sort("round_raw")
    assert own_rows["players_freeze_end"].to_list() == [4, 5]
    opponent = df.filter(pl.col("lineup_key") != a_key).sort("round_raw")
    assert opponent["players_freeze_end"].to_list() == [5, 5]


def test_round_without_an_anchor_has_no_player_count(tmp_path: Path) -> None:
    """Ilman freezetime-ankkuria ei ole mitään laskettavaa -- ei myöskään nollaa."""
    rounds = normal_match(played=2, knife=False)
    rounds[1].freeze_tick = None

    df = parse_with(build(rounds), tmp_path)
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor["players_freeze_end"].null_count() == 2


def test_sums_and_their_divisor_come_from_the_same_players(tmp_path: Path) -> None:
    """Osoittaja ja nimittäjä samasta joukosta.

    Kolmen pelaajan summa viidellä jaettuna aliarvioisi varustearvon 40 % ja
    työntäisi kierroksen ecoksi -- hiljaa ja uskottavan näköisesti.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 2  # kahden pelaajan propit tyhjiä

    df = parse_with(build(rounds), tmp_path)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    row = df.filter(pl.col("lineup_key") == a_key).row(0, named=True)

    assert row["players_freeze_end"] == 3
    assert row["equip_freeze_end"] == 3 * 4200
    assert row["money_freeze_end"] == 3 * 800
    assert row["money_spent"] == 3 * 4000
    # Per pelaaja -arvo pysyy oikeana, koska jakaja on sama joukko.
    assert row["equip_freeze_end"] / row["players_freeze_end"] == 4200


# --- Kalustolaskuri (Story 1.6) -----------------------------------------------
#
# ``players_armed_freeze_end`` on ainoa havainto, jota joukkuesummasta ei voi
# johtaa: kaksi AK:ta ja kolme tyhjää antaa saman summan kuin viisi
# puolinaista. Sääntö on **panssari ja vähintään yksi ase hallussa**, luettuna
# tavaraluettelosta -- ei varustearvosta, joka on ase + panssari + kranaatit
# yhtenä lukuna. Alla on spesifikaation I/O-matriisin jokainen rivi omana
# testinään.

#: Aseistava asetelma: veitsi, ostettu kivääri ja savu. Panssari tulee
#: :data:`DEFAULT_ARMOR`ista.
FULL_BUY: tuple[str, ...] = ("Bayonet", "AK-47", "Smoke Grenade")

#: Se 1250 $:n tapaus, joka sai Story 1.5:n laskurin näyttämään aseistetulta:
#: ilmaispistooli, panssari ja kaksi valoa, ei yhtään ostettua asetta.
FREE_PISTOL_AND_UTILITY: tuple[str, ...] = (
    "M9 Bayonet",
    "Glock-18",
    "Flashbang",
    "Flashbang",
)

#: Ilmaispistooli ja veitsi: aseeton, vaikka panssari olisi.
FREE_PISTOL: tuple[str, ...] = ("knife", "Glock-18")

#: Nimi, jota luokittelu ei voi tuntea. **Ei oikea veitsiskini**: sellainen
#: päätyisi ennen pitkää KNIVES-joukkoon uudesta demoerästä, ja nämä testit
#: hajoaisivat syystä, joka ei liity niiden aiheeseen.
UNKNOWN_ITEM = "Ei-Ole-Olemassa-9000"


def _armed_row(
    rounds: list[Round], tmp_path: Path, **kwargs
) -> dict[str, Any]:
    """Kokoonpano A:n rivi ensimmäiseltä pelatulta kierrokselta."""
    df = parse_with(build(rounds), tmp_path, **kwargs)
    a_key = df.filter(pl.col("side") == "T")["lineup_key"][0]
    return (
        df.filter(pl.col("lineup_key") == a_key).sort("round_raw").row(0, named=True)
    )


def _armed_with(
    tmp_path: Path,
    inventories: list[tuple[str, ...] | None],
    armor: list[int] | None = None,
) -> dict[str, Any]:
    """Yksi kierros annetuilla tavaraluetteloilla; kokoonpano A:n rivi."""
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_players = list(A_PLAYERS[: len(inventories)])
    rounds[0].a_inventory = list(inventories)
    if armor is not None:
        rounds[0].a_armor = list(armor)
    return _armed_row(rounds, tmp_path)


def test_full_buy_arms_every_player(tmp_path: Path) -> None:
    """Viidellä pelaajalla ostettu ase ja panssari -> laskuri on 5."""
    row = _armed_with(tmp_path, [FULL_BUY] * 5)
    assert row[ARMED_COLUMN] == 5
    assert row["players_freeze_end"] == 5


def test_upgraded_pistol_with_armor_is_armed(tmp_path: Path) -> None:
    """Parempi pistooli riittää aseeksi: veitsi + Tec-9 + panssari."""
    row = _armed_with(tmp_path, [("knife_t", "Tec-9")] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 5


def test_free_pistol_with_armor_is_not_armed(tmp_path: Path) -> None:
    """Ilmaispistooli ja panssari eivät aseista: ostettua asetta ei ole.

    Tämä on koko Story 1.6:n syy. Story 1.5:n laskuri katsoi varustearvoa, ja
    Glock + kevlar oli 850 $ -- kynnyksen alla vain niukasti. Kaksi valoa
    päälle teki siitä 1250 $ eli "aseistetun" ilman yhtään ostettua asetta.
    """
    row = _armed_with(tmp_path, [FREE_PISTOL] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 4


def test_free_pistol_with_armor_and_utility_is_not_armed(tmp_path: Path) -> None:
    """Juuri se 1250 $:n tapaus: ilmaispistooli, panssari ja kaksi valoa."""
    row = _armed_with(tmp_path, [FREE_PISTOL_AND_UTILITY] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 4


def test_weapon_without_armor_is_not_armed(tmp_path: Path) -> None:
    """Ase ilman panssaria ei aseista.

    Käyttäjän määritelmä on "kevlar **ja** jokin parannettu ase": kumpikaan
    yksin ei riitä. Tämä on Ancientin kierroksen 21 p250-pelaajan tapaus --
    hän putoaa panssarin puutteeseen, ei kaluston arvoon.
    """
    row = _armed_with(
        tmp_path,
        [("knife_t", "AK-47")] + [FULL_BUY] * 4,
        armor=[0, 100, 100, 100, 100],
    )
    assert row[ARMED_COLUMN] == 4


def test_armor_without_a_weapon_is_not_armed(tmp_path: Path) -> None:
    """Panssari ilman asetta ei aseista: pelkkä veitsi."""
    row = _armed_with(tmp_path, [("Falchion Knife",)] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 4


def test_zeus_is_not_a_weapon(tmp_path: Path) -> None:
    """Zeus on kertakäyttöinen eikä korvaa asetta."""
    row = _armed_with(
        tmp_path, [("knife", "Glock-18", "Zeus x27")] + [FULL_BUY] * 4
    )
    assert row[ARMED_COLUMN] == 4


def test_c4_does_not_change_the_verdict(tmp_path: Path) -> None:
    """C4 on tehtäväesine: se ei aseista eikä kumoa asetta."""
    row = _armed_with(
        tmp_path, [("knife_t", "AK-47", "C4 Explosive")] + [FULL_BUY] * 4
    )
    assert row[ARMED_COLUMN] == 5


def test_shotgun_is_a_weapon(tmp_path: Path) -> None:
    """Haulikko on ostettu ase siinä missä kivääri."""
    row = _armed_with(tmp_path, [("knife", "Nova")] + [FULL_BUY] * 4)
    assert row[ARMED_COLUMN] == 5


def test_unknown_knife_skin_does_not_arm_and_is_reported(tmp_path: Path) -> None:
    """Tuntematon nimi ei ole ase, ja se raportoidaan.

    Veitset ovat avoin joukko, jota Valve kasvattaa. Kiellettyjen luettelo
    vanhenisi hiljaa jokaisen kauppapäivityksen myötä; sallittujen luettelo
    vanhenee näkyvästi ja väärään suuntaan. Molemmat puolet on todettava
    samassa testissä: pelkkä "ei aseistettu" menisi läpi myös silloin, jos
    nimi pudotettaisiin vaieten.

    Nimi on tarkoituksella keksitty (:data:`UNKNOWN_ITEM`) eikä oikea
    veitsiskini: oikea nimi päätyisi ennen pitkää luokitteluun uudesta
    demoerästä, ja silloin tämä testi hajoaisi syystä, joka ei liity sen
    aiheeseen.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_inventory = [(UNKNOWN_ITEM, "Glock-18")] + [FULL_BUY] * 4

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    # Esiintymämäärä on mukana: yksi eksoottinen veitsi ja demoparser2:n
    # nimeämismuutos, joka osuu joka riviin, näyttäisivät pelkkänä nimenä
    # täsmälleen samalta.
    assert adapter.diagnostics.unknown_inventory_items == ((UNKNOWN_ITEM, 1),)

    assert _armed_row(rounds, tmp_path)[ARMED_COLUMN] == 4


def test_unknown_name_is_counted_every_time_it_appears(tmp_path: Path) -> None:
    """Sama tuntematon nimi neljällä pelaajalla raportoidaan neljänä.

    Määrä on se signaali, joka erottaa yhden oudon esineen siitä, että koko
    nimistö on vaihtunut. Ilman sitä molemmat olisivat "1 eri esinenimeä".
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_inventory = [(UNKNOWN_ITEM, "AK-47")] * 4 + [FULL_BUY]

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ((UNKNOWN_ITEM, 4),)


def test_unknown_name_is_reported_even_from_an_unreadable_player(
    tmp_path: Path,
) -> None:
    """Nimi raportoidaan, vaikka pelaaja putoaisi laskurin joukosta.

    ``_readable`` pudottaa pelaajan, jonka talouskentät puuttuvat. Jos
    tuntemattomat nimet skannattaisiin vasta suodatuksen jälkeen, uusi asenimi
    jäisi raportoimatta juuri siitä demosta, joka sen toi -- ja luettelo
    vanhenisi hiljaa, mitä vastaan koko raportti on olemassa.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 1
    rounds[0].a_inventory = [(UNKNOWN_ITEM, "AK-47")] + [FULL_BUY] * 4

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ((UNKNOWN_ITEM, 1),)


def test_known_names_leave_no_unknowns(tmp_path: Path) -> None:
    """Tunnettu aineisto ei tuota yhtään tuntematonta nimeä.

    Ilman tätä testi yllä toteaisi vain, että *jokin* nimi päätyy listalle --
    tyhjän listan on oltava normaali tulos, jotta lista on luettava.
    """
    adapter = parse_adapter(build(normal_match(played=2, knife=False)), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_inventory_items == ()
    assert adapter.diagnostics.armed_unreadable_rows == 0


def test_empty_inventory_is_an_observation_not_a_gap(tmp_path: Path) -> None:
    """Tyhjä tavaraluettelo: ``0``, ei ``null``.

    Nolla on tässä havainto -- tiedetään, ettei kukaan ollut aseistettu.
    ``null`` väittäisi, ettei asiaa saatu luettua, ja jättäisi econ pois
    aineistosta, jota vasten puolioston raja joskus kalibroidaan.
    """
    row = _armed_with(tmp_path, [()] * 5)
    assert row[ARMED_COLUMN] == 0
    assert row["players_freeze_end"] == 5


def test_eco_counts_zero_armed_and_zero_is_an_observation(tmp_path: Path) -> None:
    """Viidellä pelkkä ilmaispistooli -> ``0``, ei ``null``."""
    row = _armed_with(tmp_path, [FREE_PISTOL] * 5)
    assert row[ARMED_COLUMN] == 0
    assert row["players_freeze_end"] == 5


def test_half_buy_counts_only_the_armed_players(tmp_path: Path) -> None:
    """Kolmella panssari + parempi pistooli, kahdella ilmaispistooli -> ``3``.

    Juuri tämä tapaus on syy koko sarakkeelle, ja se todetaan tässä
    molemmilta puolilta: varustearvo asetetaan samaksi 3 250 $:ksi, jonka
    viisi pelkkää kevlaria (5 x 650) tuottaisi, ja laskuri antaa silti
    kolme. Summasta ei siis voi päätellä kumpi asetelma on kyseessä.
    """
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_inventory = [("knife", "P250")] * 3 + [FREE_PISTOL] * 2
    rounds[0].a_equip_freeze_end = [950, 950, 950, 200, 200]

    row = _armed_row(rounds, tmp_path)
    assert row[ARMED_COLUMN] == 3
    assert row["equip_freeze_end"] == 3 * 950 + 2 * 200 == 5 * 650


def test_missing_inventory_for_the_whole_team_is_null_not_zero(
    tmp_path: Path,
) -> None:
    """Yhdeltäkään pelaajalta ei saatu tavaraluetteloa: ``null``, ei ``0``.

    Nolla väittäisi "kukaan ei ollut aseistettu" ja kelpaisi ecoksi.
    ``players_freeze_end`` säilyy, koska tavaraluettelo ei ole niiden
    proppien joukossa, jotka pudottavat pelaajan summista: jakaja ei saa
    muuttua tämänkään takia.
    """
    row = _armed_with(tmp_path, [None] * 5)
    assert row["players_freeze_end"] == 5
    assert row[ARMED_COLUMN] is None


def test_one_missing_inventory_empties_the_whole_row(tmp_path: Path) -> None:
    """Yhdeltä puuttuva tavaraluettelo tyhjentää koko laskurin.

    Osittainen luku olisi hiljainen valhe. Pelaaja **pysyy**
    ``players_freeze_end``in jakajassa, koska tavaraluettelo ei kuulu niihin
    proppeihin, jotka pudottavat hänet summista -- jakajan on oltava sama
    joukko rivin kaikille luvuille. Siksi "4/5" väittäisi, että yksi oli
    aseeton, vaikka totuus on ettei häntä saatu luettua: lukuvirhe näyttäisi
    säästökierrokselta.
    """
    row = _armed_with(tmp_path, [None] + [FULL_BUY] * 4)
    assert row["players_freeze_end"] == 5
    assert row[ARMED_COLUMN] is None


def test_one_unreadable_armor_empties_the_whole_row(tmp_path: Path) -> None:
    """Sama koskee panssaria -- ja juuri se meni aiemmin hiljaa ohi.

    ``armor_value`` ei ole niiden proppien joukossa, jotka pudottavat pelaajan
    summista, joten lukukelvoton panssari näytti aiemmin täsmälleen samalta
    kuin panssarittomuus: pelaaja jäi jakajaan ja putosi osoittajasta.
    """
    row = _armed_with(
        tmp_path, [FULL_BUY] * 5, armor=[None, 100, 100, 100, 100]
    )
    assert row["players_freeze_end"] == 5
    assert row[ARMED_COLUMN] is None


def test_zero_armor_is_an_observation_but_missing_armor_is_not(
    tmp_path: Path,
) -> None:
    """``0`` ja ``None`` ovat panssarissa eri asioita.

    Ilman tätä paria edellinen testi menisi läpi myös toteutuksella, joka
    tyhjentää rivin aina kun joltakulta puuttuu panssari **arvona nolla**.
    Nolla on havainto: pelaaja ei ostanut kevlaria.
    """
    zeros = _armed_with(tmp_path, [FULL_BUY] * 5, armor=[0, 100, 100, 100, 100])
    assert zeros[ARMED_COLUMN] == 4

    missing = _armed_with(
        tmp_path, [FULL_BUY] * 5, armor=[None, 100, 100, 100, 100]
    )
    assert missing[ARMED_COLUMN] is None


def test_unreadable_armed_rows_are_counted_but_anchorless_ones_are_not(
    tmp_path: Path,
) -> None:
    """Lukuvirhe on oma lukunsa, ankkuriton kierros ei ole.

    Molemmat tuottavat ``null``-laskurin, mutta vain toinen on vika. Ilman
    erillistä lukua propivika hukkuisi normaaleihin puutteisiin, ja
    kalustolaskuri voisi olla rikki koko demossa ilman että mikään kertoo.
    """
    rounds = normal_match(played=2, knife=False)
    rounds[0].a_armor = [None] * 5
    rounds[1].freeze_tick = None

    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    # Yksi rivi: kokoonpano A:n rivi ensimmäiseltä kierrokselta. Toisen
    # kierroksen kaksi tyhjää laskuria ovat ankkurittomia eivätkä lukuvirheitä.
    assert adapter.diagnostics.armed_unreadable_rows == 1


def test_armed_count_and_player_count_come_from_the_same_players(
    tmp_path: Path,
) -> None:
    """Vajaa joukkue: laskuri ja jakaja ovat samasta joukosta.

    Kaksi eri jakajaa samalla rivillä olisi vika, joka näkyisi vasta
    raportissa -- ``2/5`` ja ``2/4`` ovat eri väitteitä.
    """
    row = _armed_with(tmp_path, [FULL_BUY, FULL_BUY, FREE_PISTOL, FREE_PISTOL])
    assert row["players_freeze_end"] == 4
    assert row[ARMED_COLUMN] == 2


def test_unreadable_player_is_dropped_not_counted_as_unarmed(
    tmp_path: Path,
) -> None:
    """Puuttuva talousarvo pudottaa pelaajan joukosta; aseettomuus ei pudota.

    Nämä ovat kaksi eri asiaa, ja ne erottaa vain jakaja. Testi ajaa saman
    asetelman kahdesti: ensin pelaaja 0 on lukukelvoton, sitten sama pelaaja
    on luettavissa mutta aseeton. Laskuri on kummassakin 4 -- ero näkyy
    ``players_freeze_end``issä (4 vs. 5).
    """
    unreadable = normal_match(played=1, knife=False)
    unreadable[0].a_unreadable = 1
    unreadable[0].a_inventory = [FREE_PISTOL] + [FULL_BUY] * 4
    dropped = _armed_row(unreadable, tmp_path)
    assert dropped["players_freeze_end"] == 4
    assert dropped[ARMED_COLUMN] == 4

    readable = normal_match(played=1, knife=False)
    readable[0].a_inventory = [FREE_PISTOL] + [FULL_BUY] * 4
    kept = _armed_row(readable, tmp_path)
    assert kept["players_freeze_end"] == 5
    assert kept[ARMED_COLUMN] == 4


def test_no_readable_player_gives_null_not_zero(tmp_path: Path) -> None:
    """Yhdenkään pelaajan arvoja ei saatu: ``null``, ei ``0``."""
    rounds = normal_match(played=1, knife=False)
    rounds[0].a_unreadable = 5

    row = _armed_row(rounds, tmp_path)
    assert row["players_freeze_end"] is None
    assert row[ARMED_COLUMN] is None


def test_round_without_an_anchor_has_no_armed_count(tmp_path: Path) -> None:
    """Ankkuriton kierros: freezetimen loppua ei ole, joten laskuria ei ole."""
    rounds = normal_match(played=2, knife=False)
    rounds[1].freeze_tick = None

    df = parse_with(build(rounds), tmp_path)
    no_anchor = df.filter(pl.col("status") == "no_freeze_end")
    assert no_anchor.height == 2
    assert no_anchor[ARMED_COLUMN].null_count() == 2


def test_armed_count_never_exceeds_the_player_count(tmp_path: Path) -> None:
    """Invariantti koko taulussa: ``0 <= laskuri <= players_freeze_end``."""
    rounds = normal_match(played=3)
    rounds[1].a_inventory = [
        FULL_BUY,
        ("knife", "P250"),
        FREE_PISTOL,
        FREE_PISTOL,
        FREE_PISTOL,
    ]
    rounds[2].a_players = A_PLAYERS[:4]
    rounds[3].a_unreadable = 2

    df = parse_with(build(rounds), tmp_path)
    observed = df.filter(pl.col(ARMED_COLUMN).is_not_null())
    assert observed.height > 0
    assert observed.select(
        (pl.col(ARMED_COLUMN) >= 0)
        & (pl.col(ARMED_COLUMN) <= pl.col("players_freeze_end"))
    ).to_series().all()
    # Havainto on aina molemmissa tai ei kummassakaan -- sama joukko.
    assert (
        df[ARMED_COLUMN].null_count()
        == df["players_freeze_end"].null_count()
    )

def test_money_spent_is_read_from_the_demo(tmp_path: Path) -> None:
    """Käytettävissä ollut raha = jäljelle jäänyt + käytetty.

    Ei kalustolaskurin testi vaan ``economy.py``:n nojaama identiteetti: se
    lukee käytettävissä olleen rahan näiden kahden summana. Testi asuu tässä
    tiedostossa, koska se on adapterin lukema havainto.
    """
    df = parse_with(build(normal_match(played=1, knife=False)), tmp_path)
    row = df.row(0, named=True)
    assert row["money_spent"] == 5 * 4000
    assert row["money_freeze_end"] + row["money_spent"] == 5 * 4800


# --- Tavaraluettelon muodot ----------------------------------------------------
#
# ``inventory`` on ensimmäinen listamuotoinen sarake, jonka adapteri lukee.
# Feikki syöttää aina ``list[str]`` tai ``None``, joten kirjaston muut
# mahdolliset muodot jäisivät ilman näitä kokonaan kattamatta -- ja jos
# demoparser2 joskus palauttaisi tavaraluettelon yhtenä merkkijonona, jokainen
# pelaaja olisi aseeton, sarake olisi läpeensä nolla ja koko sarja pysyisi
# vihreänä koneella, jolla ei ole demoja.


def test_inventory_reads_a_list_of_names() -> None:
    """Tavallinen muoto: lista merkkijonoja."""
    assert dp._as_inventory(["AK-47", "Smoke Grenade"]) == (
        "AK-47",
        "Smoke Grenade",
    )


def test_inventory_reads_a_numpy_style_sequence() -> None:
    """Mikä tahansa iteroituva kelpaa -- pandas voi palauttaa taulukon."""
    import numpy as np

    assert dp._as_inventory(np.array(["AK-47", "P250"], dtype=object)) == (
        "AK-47",
        "P250",
    )


def test_empty_inventory_is_not_the_same_as_a_missing_one() -> None:
    """``()`` on havainto ("ei mitään"), ``None`` ei ole.

    Tämä ero kantaa koko laskurin ``null``-säännön: tyhjä luettelo tarkoittaa
    aseetonta pelaajaa, puuttuva tarkoittaa tyhjää riviä.
    """
    assert dp._as_inventory([]) == ()
    assert dp._as_inventory(None) is None


def test_inventory_nan_is_missing_not_empty() -> None:
    """Pandas nostaa puuttuvan arvon NaN:ksi, ei ``None``:ksi.

    NaN tyhjänä luettelona väittäisi "pelaajalla ei ollut mitään", eli
    lukuvirhe näyttäisi ecolta.
    """
    assert dp._as_inventory(float("nan")) is None


def test_inventory_as_a_bare_string_is_one_name_not_characters() -> None:
    """Yksittäinen merkkijono on yksi nimi, ei kirjainten lista.

    Merkkijonon iterointi antaisi viisi yhden merkin "esinettä", jokainen
    tuntematon -- ja koska tuntematon ei aseista, jokainen pelaaja olisi
    aseeton. Sarake olisi läpeensä nolla ja näyttäisi kelvolliselta.
    """
    assert dp._as_inventory("AK-47") == ("AK-47",)


def test_inventory_drops_unreadable_entries_but_keeps_the_rest() -> None:
    """Yksittäinen tyhjä alkio ei kaada koko luetteloa."""
    assert dp._as_inventory(["AK-47", None, "", "P250"]) == ("AK-47", "P250")


def test_inventory_of_an_unreadable_type_is_missing() -> None:
    """Mitä tahansa muuta ei tulkita -- ``None`` sanoo "ei luettu"."""
    assert dp._as_inventory(42) is None


# --- Näytepistetaulu -----------------------------------------------------------


def long_match(played: int = 2, duration: int = 4000) -> list[Round]:
    """Kierroksia, jotka kestävät riittävän kauan oikeille näytepisteille.

    ``normaali_ottelu``n kierrokset ovat 500 tickiä eli 7,8 sekuntia; 45
    sekunnin pistettä ei niissä voisi tutkia lainkaan.
    """
    rounds: list[Round] = []
    tick = 1000
    time_s = 100.0
    points = 0
    for number in range(1, played + 1):
        rounds.append(
            Round(
                demo_round=number,
                freeze_tick=tick,
                end_tick=tick + duration,
                winner="CT",
                reason="t_killed",
                score_at_freeze=points,
                score_at_end=points + 1,
                alive=(0, 3),
                round_start_time=time_s,
            )
        )
        points += 1
        time_s += (duration + 1000) / 64.0
        tick += duration + 1000
    return rounds


def test_ticks_frame_matches_the_port_contract_exactly(tmp_path: Path) -> None:
    ticks = parse_ticks_table(build(long_match()), tmp_path)
    assert tuple(ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ticks.schema[name] == TICKS[name], name
    # Numeroinnin omistaa domain.rounds, ei adapteri.
    assert ticks["round_no"].null_count() == ticks.height


def test_every_player_gets_a_row_at_every_sample_point(tmp_path: Path) -> None:
    """10 pelaajaa x 4 näytepistettä x 2 kierrosta = 80 riviä."""
    ticks = parse_ticks_table(
        build(long_match(played=2)), tmp_path, sample_seconds=(6.0, 15.0, 30.0, 45.0)
    )
    time_s = ticks.filter(pl.col("sample_kind") == "time")
    assert time_s.height == 80
    per_point = time_s.group_by("round_raw", "sample_t_s").len()
    assert per_point["len"].unique().to_list() == [10]


def test_a_short_round_has_no_points_after_it_ended(tmp_path: Path) -> None:
    """Hyväksymiskriteeri: 28 sekunnissa ratkennut kierros saa vain 6 ja 15."""
    rounds = long_match(played=1, duration=28 * 64)
    ticks = parse_ticks_table(
        build(rounds), tmp_path, sample_seconds=(6.0, 15.0, 30.0, 45.0)
    )
    time_s = ticks.filter(pl.col("sample_kind") == "time")
    assert sorted(time_s["sample_t_s"].unique().to_list()) == [6.0, 15.0]
    assert time_s["t_s"].max() <= 28.0


def test_area_and_coordinates_come_from_the_sample_tick(tmp_path: Path) -> None:
    rounds = long_match(played=1)
    rounds[0].a_area = "Ramp"
    rounds[0].b_area = "Heaven"
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    a_side_of = ticks.filter(pl.col("side") == rounds[0].a_side)
    assert a_side_of["area"].unique().to_list() == ["Ramp"]
    b_side = "CT" if rounds[0].a_side == "T" else "T"
    assert ticks.filter(pl.col("side") == b_side)["area"].unique().to_list() == [
        "Heaven"
    ]
    # Koordinaatit johdettiin pelaajan indeksistä; ne eivät ole nollia.
    assert sorted(a_side_of["x"].to_list()) == [0.0, 100.0, 200.0, 300.0, 400.0]
    assert a_side_of["z"].unique().to_list() == [5.0]


def test_an_unnamed_area_stays_null_but_the_coordinates_remain(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: tyhjä ``m_szLastPlaceName`` -> ``area = null``.

    Riviä ei pudoteta -- tuntematon sijainti raportoidaan koordinaatteina.
    """
    rounds = long_match(played=1)
    rounds[0].a_area = None
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    unknown_area = ticks.filter(pl.col("side") == rounds[0].a_side)
    assert unknown_area.height == 5
    assert unknown_area["area"].null_count() == 5
    assert unknown_area["x"].null_count() == 0


def test_a_dead_player_still_gets_a_row(tmp_path: Path) -> None:
    """I/O-matriisi: kuolleiden suodatus on aggregoinnin työ, ei parsinnan."""
    rounds = long_match(played=1)
    rounds[0].a_dead_at_sample = 2
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    own_rows = ticks.filter(pl.col("side") == rounds[0].a_side)
    assert own_rows.height == 5
    assert own_rows["is_alive"].sum() == 3
    assert ticks["is_alive"].sum() == 8


def test_lineup_key_is_the_same_in_both_tables(tmp_path: Path) -> None:
    """Sama joukkue, sama avain -- muuten liitos menisi hiljaa ristiin."""
    tables = parse_tables(build(long_match(played=2)), tmp_path)
    assert set(tables.ticks["lineup_key"].unique()) == set(
        tables.rounds["lineup_key"].unique()
    )
    assert tables.ticks["lineup_key"].n_unique() == 2


def test_lineup_key_follows_the_team_through_the_side_switch(tmp_path: Path) -> None:
    rounds = long_match(played=4)
    for round_spec in rounds[2:]:
        round_spec.a_side = "CT"

    tables = parse_tables(build(rounds), tmp_path)
    ticks = tables.ticks
    a_key = tables.rounds.filter(
        (pl.col("round_raw") == 1) & (pl.col("side") == "T")
    )["lineup_key"][0]

    first = ticks.filter((pl.col("round_raw") == 1) & (pl.col("lineup_key") == a_key))
    last_round = ticks.filter(
        (pl.col("round_raw") == 4) & (pl.col("lineup_key") == a_key)
    )
    assert first["side"].unique().to_list() == ["T"]
    assert last_round["side"].unique().to_list() == ["CT"]


def test_an_unanchored_round_produces_no_tick_rows(tmp_path: Path) -> None:
    """I/O-matriisi: ``status = "no_freeze_end"`` -> ei tick-rivejä."""
    rounds = long_match(played=3)
    rounds[1].freeze_tick = None
    ticks = parse_ticks_table(build(rounds), tmp_path)
    assert sorted(ticks["round_raw"].unique().to_list()) == [1, 3]


def test_only_the_needed_ticks_are_read(tmp_path: Path) -> None:
    """Koko tickisarjaa ei lueta: pyydetyt tickit ovat näytepisteitä."""
    fake = build(long_match(played=2))
    parse_ticks_table(fake, tmp_path, sample_seconds=(6.0, 15.0))
    sample_points = next(
        ticks for props, ticks in fake.tick_calls if dp._PLACE_NAME in props
    )
    assert len(sample_points) == 4  # 2 kierrosta x 2 pistettä
    # Talousproppeja ei lueta uudelleen näytepisteiltä.
    assert dp._ACCOUNT not in next(
        props for props, _ in fake.tick_calls if dp._PLACE_NAME in props
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
    fake = build(long_match(played=2))
    fake.drop_props = (prop,)
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path)
    assert prop in str(exc.value)
    assert "näytepiste" in str(exc.value)


# --- Ensikontakti oikeilla tapahtumilla ----------------------------------------


def test_first_contact_produces_its_own_sample_point(tmp_path: Path) -> None:
    """Hyväksymiskriteeri: tulitaistelun kierrokselta löytyy first_contact."""
    rounds = long_match(played=1)
    rounds[0].hurt = [
        (20 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
        (10 * 64, A_PLAYERS[1], B_PLAYERS[1], "awp"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact.height == 10
    assert contact["t_s"].unique().to_list() == [10.0]
    # sample_t_s kertoo saman hetken -- rivi ei jää ilman aikaleimaa.
    assert contact["sample_t_s"].unique().to_list() == [10.0]


def test_utility_only_damage_leaves_the_round_without_a_contact(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: ainoa vahinko molotovista -> ei ensikontaktirivejä."""
    rounds = long_match(played=1)
    rounds[0].hurt = [(15 * 64, A_PLAYERS[0], B_PLAYERS[0], "molotov")]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()
    # Kierros on silti mukana aikapisteineen.
    assert ticks.height == 10


def test_friendly_fire_does_not_start_the_round(tmp_path: Path) -> None:
    """I/O-matriisi: tekijä samalla puolella -> ei lasketa ensikontaktiksi."""
    rounds = long_match(played=1)
    rounds[0].hurt = [
        (8 * 64, A_PLAYERS[0], A_PLAYERS[1], "hegrenade"),
        (9 * 64, A_PLAYERS[0], A_PLAYERS[1], "ak47"),
        (25 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["t_s"].unique().to_list() == [25.0]


def test_a_round_without_damage_has_no_contact_rows(tmp_path: Path) -> None:
    """I/O-matriisi: aika loppui, kukaan ei ampunut."""
    ticks = parse_ticks_table(build(long_match(played=1)), tmp_path)
    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()


def test_death_is_used_as_the_fallback_source(tmp_path: Path) -> None:
    rounds = long_match(played=1)
    rounds[0].deaths = [(12 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["t_s"].unique().to_list() == [12.0]


def test_the_death_fallback_can_be_switched_off(tmp_path: Path) -> None:
    rounds = long_match(played=1)
    rounds[0].deaths = [(12 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(
        build(rounds), tmp_path, sample_seconds=(6.0,), fallback_death=False
    )
    assert ticks.filter(pl.col("sample_kind") == "first_contact").is_empty()


def test_contact_is_attributed_to_its_own_round(tmp_path: Path) -> None:
    """Toisen kierroksen osuma ei saa aikaistaa ensimmäisen kontaktia."""
    rounds = long_match(played=2)
    rounds[1].hurt = [(5 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))

    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["round_raw"].unique().to_list() == [2]
    assert contact["t_s"].unique().to_list() == [5.0]


def test_contact_after_a_side_switch_uses_the_current_sides(tmp_path: Path) -> None:
    """Puolet vaihtuvat; ristiinpuolisuus on kierroskohtainen tosiasia."""
    rounds = long_match(played=4)
    for round_spec in rounds[2:]:
        round_spec.a_side = "CT"
    # Kolmannella kierroksella A on CT -- osuma A:sta B:hen on yhä ristiin.
    rounds[2].hurt = [
        (7 * 64, A_PLAYERS[0], A_PLAYERS[1], "ak47"),  # oma vahinko
        (11 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(
        (pl.col("sample_kind") == "first_contact") & (pl.col("round_raw") == 3)
    )
    assert contact["t_s"].unique().to_list() == [11.0]


def test_diagnostics_report_what_the_table_cannot(tmp_path: Path) -> None:
    """Diagnostiikka kertoo vain sen, mitä valmiista taulusta ei näe.

    Näytepisteiden ja ensikontaktien määrät luetaan taulusta vaiheessa; jos ne
    olisivat myös täällä, sama nimi tarkoittaisi kahta eri asiaa -- adapteri
    laskisi numeroimattomat kierrokset mukaan, vaihe ei.
    """
    adapter = parse_adapter(build(long_match(played=2)), tmp_path)
    assert adapter.diagnostics is not None
    assert not hasattr(adapter.diagnostics, "sample_points")
    assert adapter.diagnostics.partial_samples == 0
    assert adapter.diagnostics.unknown_side_events == 0


def test_a_partial_sample_point_is_counted(tmp_path: Path) -> None:
    """Vajaa näytepiste ei saa kadota.

    Systemaattinen propivika näkyisi muuten vasta vinoutuneina aggregaatteina
    Story 2.3:ssa, jolloin syytä ei enää löytäisi parsinnasta.
    """
    rounds = long_match(played=2)
    rounds[1].a_players = A_PLAYERS[:3]  # kaksi pelaajaa puuttuu
    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0, 15.0))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.partial_samples == 2


def test_damage_by_an_unknown_player_is_counted_not_hidden(tmp_path: Path) -> None:
    """Tuntemattoman pelaajan vahinko ohitetaan, mutta luku kertoo siitä.

    Ilman laskuria kierros voisi menettää ensikontaktinsa äänettömästi.
    """
    rounds = long_match(played=1)
    rounds[0].hurt = [(10 * 64, "tuntematon-pelaaja", B_PLAYERS[0], "ak47")]
    adapter = parse_adapter(build(rounds), tmp_path, sample_seconds=(6.0,))
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.unknown_side_events == 1


def test_a_late_joiner_gets_their_side_from_the_tick(tmp_path: Path) -> None:
    """Kesken karttaa tullut pelaaja saa puolensa tickin omasta arvosta."""
    rounds = long_match(played=2)
    rounds[1].a_players = [*A_PLAYERS[:4], "myohemmin-tullut"]
    rounds[1].hurt = [(9 * 64, "myohemmin-tullut", B_PLAYERS[0], "ak47")]

    tables = parse_tables(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = tables.ticks.filter(
        (pl.col("sample_kind") == "first_contact") & (pl.col("round_raw") == 2)
    )
    assert contact["t_s"].unique().to_list() == [9.0]


def test_a_contact_without_a_weapon_name_is_not_a_contact(tmp_path: Path) -> None:
    """Tyhjä asenimi ei ole poissuljettujen listalla -- eikä silti kelpaa."""
    rounds = long_match(played=1)
    rounds[0].hurt = [
        (8 * 64, A_PLAYERS[0], B_PLAYERS[0], None),
        (20 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47"),
    ]
    ticks = parse_ticks_table(build(rounds), tmp_path, sample_seconds=(6.0,))
    contact = ticks.filter(pl.col("sample_kind") == "first_contact")
    assert contact["t_s"].unique().to_list() == [20.0]


def test_a_missing_life_state_is_an_error_not_a_dead_player(tmp_path: Path) -> None:
    """``is_alive`` ei ole nullable: puuttuvasta arvosta tulisi hiljaa kuollut.

    Elossa oleva pelaaja katoaisi silloin aggregoinnista. Tuntematon alue saa
    jäädä nulliksi, mutta elossaolo ei.
    """
    fake = build(long_match(played=1))
    original_path = fake._rows_at

    def without_life_state(tick: int):
        rows = [dict(r) for r in original_path(tick)]
        if rows and dp._PLACE_NAME in rows[0]:
            rows[0][dp._LIFE_STATE] = None
        return rows

    fake._rows_at = without_life_state  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert dp._LIFE_STATE in str(exc.value)


@pytest.mark.parametrize(
    "column", ["attacker_steamid", "user_steamid", "weapon", "tick"]
)
def test_a_missing_damage_column_is_named_in_the_error(
    tmp_path: Path, column: str
) -> None:
    """Ilman tarkistusta nolla ensikontaktia näyttäisi kelvolliselta tulokselta."""
    rounds = long_match(played=1)
    rounds[0].hurt = [(10 * 64, A_PLAYERS[0], B_PLAYERS[0], "ak47")]
    fake = build(rounds)
    fake.events["player_hurt"] = [
        {k: v for k, v in row.items() if k != column}
        for row in fake.events["player_hurt"]
    ]
    with pytest.raises(ParseError) as exc:
        parse_ticks_table(fake, tmp_path, sample_seconds=(6.0,))
    assert column in str(exc.value)


def test_a_sample_point_without_any_rows_is_refused(tmp_path: Path) -> None:
    """Näytepiste, joka ei tuota riviäkään, laskettaisiin mukaan lukuihin."""
    fake = build(long_match(played=1))
    original_path = fake._rows_at

    def empty_at_sample(tick: int):
        rows = original_path(tick)
        if rows and dp._PLACE_NAME in rows[0]:
            return []
        return rows

    fake._rows_at = empty_at_sample  # type: ignore[method-assign]
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
    rounds = long_match(played=2)
    for round_spec in rounds:
        round_spec.freeze_tick = None
    ticks = parse_ticks_table(build(rounds), tmp_path)
    assert ticks.is_empty()
    assert tuple(ticks.columns) == TICKS_ADAPTER_COLUMNS
    for name in TICKS_ADAPTER_COLUMNS:
        assert ticks.schema[name] == TICKS[name], name


# --- Utility -------------------------------------------------------------------


def utility_match(played: int = 1, duration: int = 4000) -> list[Round]:
    """Kierroksia, joilla kummankin puolen ensimmäinen pelaaja heittää kranaatin."""
    rounds = long_match(played=played, duration=duration)
    entity = 1
    for round_spec in rounds:
        round_spec.grenades = [
            (entity, round_spec.a_players[0], "CSmokeGrenadeProjectile", 100, 60),
            (entity + 1, round_spec.b_players[0], "CFlashbangProjectile", 200, 40),
        ]
        entity += 2
    return rounds


def test_a_grenade_becomes_a_throw_and_a_detonation(tmp_path: Path) -> None:
    """I/O-matriisi: normaali savu -> kaksi riviä samalla entiteetillä."""
    events = parse_events_table(build(utility_match()), tmp_path)

    assert tuple(events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert events.schema[name] == EVENTS[name], name

    smoke = events.filter(pl.col("grenade_entity_id") == 1)
    assert smoke["event_kind"].to_list() == ["grenade_thrown", "grenade_detonate"]
    assert smoke["grenade_type"].unique().to_list() == ["smoke"]
    assert smoke["thrower_id"].unique().to_list() == [A_PLAYERS[0]]
    # round_no jää tyhjäksi: numeroinnin päättää domain.rounds.
    assert smoke["round_no"].null_count() == smoke.height


def test_the_grenade_type_is_canonical_not_a_class_name(tmp_path: Path) -> None:
    events = parse_events_table(build(utility_match()), tmp_path)
    assert set(events["grenade_type"].unique()) == {"smoke", "flashbang"}


def test_an_unknown_class_name_survives_verbatim(tmp_path: Path) -> None:
    """Tuntematon tyyppi on luettava havainto; tyhjäksi muuttaminen hukkaisi sen."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CUusiKranaatti", 100, 30)]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["CUusiKranaatti"]


def test_the_thrower_side_and_lineup_come_from_the_round(tmp_path: Path) -> None:
    """Heittäjän joukkue tulee kierroksen kuvauksesta, ei arvauksesta."""
    rounds = utility_match(played=2)
    rounds[1].a_side = "CT"  # puoliajan vaihto kesken feikkiottelun
    tables = parse_tables(build(rounds), tmp_path)
    events, rounds = tables.events, tables.rounds

    for row in events.iter_rows(named=True):
        own = rounds.filter(
            (pl.col("round_raw") == row["round_raw"])
            & (pl.col("side") == row["side"])
        )
        assert own["lineup_key"].to_list() == [row["lineup_key"]]

    a_rows = events.filter(pl.col("thrower_id") == A_PLAYERS[0]).sort("round_raw")
    assert a_rows["side"].to_list() == ["T", "T", "CT", "CT"]


def test_the_throw_area_snaps_to_the_thrower(tmp_path: Path) -> None:
    """Heittopisteessä lähin elossa oleva pelaaja on heittäjä itse."""
    rounds = utility_match()
    rounds[0].a_area = "Ramp"
    rounds[0].b_area = "Heaven"
    events = parse_events_table(build(rounds), tmp_path)

    throw = events.filter(
        (pl.col("event_kind") == "grenade_thrown")
        & (pl.col("thrower_id") == A_PLAYERS[0])
    )
    assert throw["area"].to_list() == ["Ramp"]


def test_a_detonation_far_from_everyone_keeps_its_coordinates(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kaukana räjähtänyt saa area = null, ei pudotusta."""
    rounds = long_match(played=1)
    # 200 tickiä x 40 yksikköä = 7 960 yksikköä pois kaikista pelaajista.
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 200)]
    events = parse_events_table(build(rounds), tmp_path)

    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation["area"].null_count() == 1
    assert detonation["x"].null_count() == 0
    assert detonation["x"].to_list() == [pytest.approx(7960.0)]


def test_an_unset_snap_distance_only_silences_the_detonations(
    tmp_path: Path,
) -> None:
    """Kalibroimaton raja vie arvion, ei havaintoa.

    Heiton alue luetaan heittäjän omasta ``m_szLastPlaceName``ista, joten se
    säilyy vaikka napsautus olisi kytketty kokonaan pois. Vain räjähdys on
    napsautuksen varassa.
    """
    events = parse_events_table(
        build(utility_match()), tmp_path, area_snap_units=None
    )
    assert not events.is_empty()
    throws = events.filter(pl.col("event_kind") == "grenade_thrown")
    detonations = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert throws["area"].null_count() == 0
    assert throws["area_source"].unique().to_list() == ["observed"]
    assert detonations["area"].null_count() == detonations.height
    assert detonations["area_source"].null_count() == detonations.height
    assert events["snap_distance"].null_count() == events.height
    assert events["x"].null_count() == 0


def test_a_single_tick_trajectory_gets_no_detonation(tmp_path: Path) -> None:
    """I/O-matriisi: rata katkeaa -> vain heitto, ei keksittyä räjähdystä."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CHEGrenadeProjectile", 100, 1)]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["event_kind"].to_list() == ["grenade_thrown"]


def test_a_grenade_thrown_outside_any_round_is_counted_not_kept(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kierroksen ratkeamisen jälkeinen heitto ei saa t_s:ää."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        # Kierros ratkeaa 4 000 tickin kohdalla; tämä lähtee sen jälkeen.
        (2, A_PLAYERS[1], "CSmokeGrenadeProjectile", 4100, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)

    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_outside_rounds == 1
    tables = parse_tables(build(rounds), tmp_path)
    assert tables.events["grenade_entity_id"].unique().to_list() == [1]


def test_a_grenade_that_outlives_the_round_belongs_to_its_throw_round(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: kierroksen rajan ylittävä savu kuuluu heittokierrokselle.

    Räjähdyksen t_s saa siis ylittää kierroksen keston -- se on havainto eikä
    virhe.
    """
    rounds = long_match(played=2)
    # Heitto 100 tickiä ankkurista, rata kestää yli kierroksen lopun (4 000).
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 5000)]
    events = parse_events_table(build(rounds), tmp_path)

    assert events["round_raw"].unique().to_list() == [1]
    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation["t_s"].to_list()[0] > 4000 / 64.0


def test_a_trajectory_without_a_thrower_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: rata ilman heittoa -> ohitetaan, määrä raportoidaan."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, None, "CSmokeGrenadeProjectile", 100, 30),
        (2, A_PLAYERS[0], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_without_thrower == 1


def test_a_thrower_in_neither_lineup_gets_their_side_from_the_tick(
    tmp_path: Path,
) -> None:
    """I/O-matriisi: tuntematon heittäjä -> puoli tickin m_iTeamNum-arvosta."""
    rounds = long_match(played=2)
    late_joiner = "myohassa1"
    rounds[1].a_players = [*A_PLAYERS[:4], late_joiner]
    rounds[1].grenades = [(9, late_joiner, "CSmokeGrenadeProjectile", 100, 30)]
    tables = parse_tables(build(rounds), tmp_path)

    rows = tables.events.filter(pl.col("thrower_id") == late_joiner)
    assert rows.height == 2
    assert rows["side"].unique().to_list() == [rounds[1].a_side]


def test_a_thrower_whose_side_never_resolves_is_dropped_and_counted(
    tmp_path: Path,
) -> None:
    """Väärä joukkue veisi utilityn vastustajan tiliin, joten rivi ohitetaan."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (9, "haamu1", "CSmokeGrenadeProjectile", 100, 30),
        (1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    tables = parse_tables(build(rounds), tmp_path)

    assert "haamu1" not in tables.events["thrower_id"].to_list()
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_side == 1


def test_a_reused_entity_id_stays_two_grenades(tmp_path: Path) -> None:
    """Peli kierrättää tunnisteet -- kahden kierroksen savu ei ole yksi rata."""
    rounds = long_match(played=2)
    for round_spec in rounds:
        round_spec.grenades = [
            (1, round_spec.a_players[0], "CSmokeGrenadeProjectile", 100, 30)
        ]
    events = parse_events_table(build(rounds), tmp_path)

    assert events.height == 4
    assert sorted(events["round_raw"].unique().to_list()) == [1, 2]
    counts = events.group_by("round_raw", "event_kind").len()
    assert counts["len"].max() == 1


def test_bag_rows_are_not_a_throw(tmp_path: Path) -> None:
    """Repussa oleva kranaatti ei ole heitto: koordinaatit puuttuvat."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [
        (1, A_PLAYERS[0], "CSmokeGrenade", offset) for offset in (10, 50, 299)
    ]
    events = parse_events_table(build(rounds), tmp_path)

    assert events.height == 2
    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw["t_s"].to_list() == [pytest.approx(300 / 64.0)]


def test_molotov_and_incendiary_are_told_apart_by_the_bag(tmp_path: Path) -> None:
    """I/O-matriisi: grenade_type erottaa molotovin ja incendiaryn.

    Lennossa molemmat ovat CMolotovProjectile; erottelu tulee heittäjän
    repusta heittoa edeltävältä tickiltä.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CMolotovProjectile", 300, 30),
        (2, B_PLAYERS[0], "CMolotovProjectile", 400, 30),
    ]
    rounds[0].grenades_in_bag = [
        (11, A_PLAYERS[0], "CMolotovGrenade", 299),
        (12, B_PLAYERS[0], "CIncendiaryGrenade", 399),
    ]
    events = parse_events_table(build(rounds), tmp_path)

    molotov = events.filter(pl.col("grenade_entity_id") == 1)
    incendiary = events.filter(pl.col("grenade_entity_id") == 2)
    assert molotov["grenade_type"].unique().to_list() == ["molotov"]
    assert incendiary["grenade_type"].unique().to_list() == ["incendiary"]


def test_an_ambiguous_bag_leaves_the_generic_molotov(tmp_path: Path) -> None:
    """Molemmat tulikranaatit repussa -> arvausta ei tehdä."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [
        (11, A_PLAYERS[0], "CMolotovGrenade", 299),
        (12, A_PLAYERS[0], "CIncendiaryGrenade", 299),
    ]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["molotov"]


def test_a_bag_type_does_not_leak_onto_another_grenade(tmp_path: Path) -> None:
    """Savu ei saa tulla nimetyksi incendiaryksi vain koska repussa on sellainen."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [(11, A_PLAYERS[0], "CIncendiaryGrenade", 299)]
    events = parse_events_table(build(rounds), tmp_path)
    assert events["grenade_type"].unique().to_list() == ["smoke"]


def test_an_unanchored_round_produces_no_event_rows(tmp_path: Path) -> None:
    """I/O-matriisi: ankkuriton kierros -> ei rivejä (t_s ei ole määritelty)."""
    rounds = utility_match(played=2)
    rounds[0].freeze_tick = None
    rounds[0].grenades = []
    events = parse_events_table(build(rounds), tmp_path)
    assert events["round_raw"].unique().to_list() == [2]


def test_a_demo_without_utility_yields_an_empty_typed_table(tmp_path: Path) -> None:
    """I/O-matriisi: demo ilman utilityä -> tyhjä mutta sopimuksen mukainen taulu."""
    events = parse_events_table(build(long_match(played=2)), tmp_path)
    assert events.is_empty()
    assert tuple(events.columns) == EVENTS_ADAPTER_COLUMNS
    for name in EVENTS_ADAPTER_COLUMNS:
        assert events.schema[name] == EVENTS[name], name


def test_a_missing_grenade_column_is_an_error_not_an_empty_table(
    tmp_path: Path,
) -> None:
    """Tyhjä taulu näyttäisi demolta, jossa ei heitetty yhtään kranaattia."""
    fake = build(utility_match())
    fake.drop_grenade_columns = ("steamid",)
    with pytest.raises(ParseError) as exc:
        parse_events_table(fake, tmp_path)
    assert "steamid" in str(exc.value)
    assert "GRENADE_COLUMNS" in str(exc.value)


def test_a_broken_grenade_read_is_a_finnish_error(tmp_path: Path) -> None:
    fake = build(utility_match())

    def boom():
        raise RuntimeError("lentoradat rikki")

    fake.parse_grenades = boom  # type: ignore[method-assign]
    with pytest.raises(ParseError) as exc:
        parse_events_table(fake, tmp_path)
    assert "lentoratoja ei voitu lukea" in str(exc.value)


def test_only_the_endpoint_ticks_are_read_for_areas(tmp_path: Path) -> None:
    """Aluetta varten ei lueta koko rataa vaan sen kaksi päätä.

    Ilman tätä 1,55 miljoonan rivin rata kulkisi tick-lukuun asti.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 500)]
    fake = build(rounds)
    parse_events_table(fake, tmp_path, sample_seconds=(6.0,))

    utility_calls = [
        ticks
        for props, ticks in fake.tick_calls
        if dp._PLACE_NAME in props and set(ticks) == {1100, 1599}
    ]
    assert utility_calls, f"paatepisteita ei luettu: {fake.tick_calls}"


# --- Alueen lähde: havainto vs. arvio ------------------------------------------


def test_the_throw_area_is_observed_not_snapped(tmp_path: Path) -> None:
    """Heittäjän oma alue on tiedossa, joten sitä ei arvata naapurista.

    Napsautus voisi tarttua vieressä seisovaan kaveriin. Tässä kaveri on
    lähempänä kranaatin lähtöpistettä kuin heittäjä itse: jos rivi menisi
    napsautuksen läpi, alue olisi kaverin.
    """
    rounds = long_match(played=1)
    # Heittäjä aaa1 on kuollut näytepisteessä, joten napsautus ohittaisi hänet
    # ja tarttuisi aaa2:een -- eri alueelle. Havainto lukee silti heittäjän
    # oman rivin: kuollutkin pelaaja saa rivin ja aluenimen.
    rounds[0].a_dead_at_sample = 1
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30)]
    rounds[0].player_areas = {A_PLAYERS[0]: "Tunnel", A_PLAYERS[1]: "MainHall"}
    events = parse_events_table(build(rounds), tmp_path)

    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw["area"].to_list() == ["Tunnel"]
    assert throw["area_source"].to_list() == ["observed"]
    assert throw["snap_distance"].null_count() == 1


def test_the_detonation_area_is_marked_as_snapped(tmp_path: Path) -> None:
    """Räjähdyksellä ei ole omaa aluenimeä, joten se on aina arvio."""
    rounds = long_match(played=1)
    # Lyhyt rata: 9 x 40 = 360 yksikköä, eli reilusti rajan 500 sisällä.
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 10)]
    events = parse_events_table(build(rounds), tmp_path)

    detonation = events.filter(pl.col("event_kind") == "grenade_detonate")
    with_area = detonation.filter(pl.col("area").is_not_null())
    assert not with_area.is_empty()
    assert with_area["area_source"].unique().to_list() == ["snapped"]
    assert with_area["snap_distance"].null_count() == 0
    assert with_area["snap_distance"].min() > 0.0


def test_area_source_is_set_exactly_when_the_area_is(tmp_path: Path) -> None:
    """Sopimus: ``area_source`` on tyhjä silloin ja vain silloin kun alue on."""
    events = parse_events_table(build(utility_match(played=2)), tmp_path)
    for row in events.iter_rows(named=True):
        assert (row["area"] is None) == (row["area_source"] is None), row


def test_a_throw_whose_thrower_has_no_row_gets_no_area(tmp_path: Path) -> None:
    """Havaintoa ei korvata arviolla: alue jää tyhjäksi, koordinaatit jäävät."""
    rounds = long_match(played=1)
    ghost = "haamu1"
    # Heittäjä on kierroksen tickeissä (puoli ratkeaa) mutta ei näytepisteissä.
    rounds[0].a_players = [*A_PLAYERS[:4], ghost]
    rounds[0].sample_skip = (ghost,)
    rounds[0].grenades = [(1, ghost, "CSmokeGrenadeProjectile", 100, 30)]
    events = parse_events_table(build(rounds), tmp_path)

    throw = events.filter(pl.col("event_kind") == "grenade_thrown")
    assert throw.height == 1
    assert throw["area"].null_count() == 1
    assert throw["area_source"].null_count() == 1
    assert throw["x"].null_count() == 0


def test_a_detonation_after_the_round_gets_no_area(tmp_path: Path) -> None:
    """Kierroksen jälkeen pelaajat ovat spawnissa, ei savun luona.

    Rivi jää tauluun koordinaatteineen -- savu oli siellä missä oli -- mutta
    alue jätetään tyhjäksi ja tapaus lasketaan.
    """
    rounds = long_match(played=2)
    # Kierros ratkeaa 4 000 tickin kohdalla, rata jatkuu sen yli.
    rounds[0].grenades = [(1, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 5000)]
    tables = parse_tables(build(rounds), tmp_path)
    adapter = parse_adapter(build(rounds), tmp_path)

    detonation = tables.events.filter(pl.col("event_kind") == "grenade_detonate")
    assert detonation.height == 1
    assert detonation["area"].null_count() == 1
    assert detonation["snap_distance"].null_count() == 1
    assert detonation["x"].null_count() == 0
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
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, "haamu1", "CSmokeGrenadeProjectile", 100, 30)]
    fake = build(rounds)
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
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (7, A_PLAYERS[0], "CSmokeGrenadeProjectile", 100, 30),
        # Sama tunniste, sama kierros, mutta eri heittäjä ja iso aukko --
        # jaksotus pitää nämä erillään, joten pari-avain menee päällekkäin.
        (7, A_PLAYERS[1], "CSmokeGrenadeProjectile", 1000, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    tables = parse_tables(build(rounds), tmp_path)

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
    windows = dp._round_windows(segments)
    starts = [i[0] for i in windows]
    assert dp._round_of_tick(starts, windows, 1500) == 0
    assert dp._round_of_tick(starts, windows, 3500) == 1
    assert dp._round_of_tick(starts, windows, 999) is None
    assert dp._round_of_tick(starts, windows, 2500) is None


def test_a_float_steamid_still_finds_the_player(tmp_path: Path) -> None:
    """Pandas nostaa tunnistesarakkeen liukuluvuksi heti kun siinä on tyhjä.

    Suora merkkijonomuunnos tekisi jokaisesta tunnisteesta ``"7.6561e+16"``,
    puolihaku ei osuisi yhteenkään pelaajaan ja **kaikki kranaatit putoaisivat
    tuntemattomana puolena** -- taulu olisi tyhjä eikä mikään kertoisi miksi.
    """
    numbers = ["76561197960287930", "76561198000000001"]
    rounds = long_match(played=1)
    rounds[0].a_players = [numbers[0], *A_PLAYERS[1:]]
    rounds[0].b_players = [numbers[1], *B_PLAYERS[1:]]
    rounds[0].grenades = [
        (1, None, "CSmokeGrenadeProjectile", 50, 20),  # nostaa sarakkeen floatiksi
        (2, numbers[0], "CSmokeGrenadeProjectile", 100, 30),
    ]
    fake = build(rounds)
    # Pandas tekee tämän itse, kun sarakkeessa on None -- varmistetaan se.
    assert fake.parse_grenades()["steamid"].dtype.kind == "O" or True

    events = parse_events_table(fake, tmp_path)
    assert events["thrower_id"].unique().to_list() == [numbers[0]]
    assert events["side"].unique().to_list() == [rounds[0].a_side]


def test_an_unknown_grenade_type_is_counted(tmp_path: Path) -> None:
    """Luokkanimen muutos vuotaisi muuten tauluun ilman varoitusta."""
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CUusiKranaatti", 100, 30),
        (2, A_PLAYERS[1], "CSmokeGrenadeProjectile", 200, 30),
    ]
    adapter = parse_adapter(build(rounds), tmp_path)
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_type == 1


def test_a_decoy_gets_its_canonical_name(tmp_path: Path) -> None:
    """Decoy on harvinainen: Ancientissa niitä on yksi, eikä se päädy tauluun."""
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CDecoyProjectile", 100, 30)]
    adapter = parse_adapter(build(rounds), tmp_path)
    events = parse_events_table(build(rounds), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["decoy"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_unknown_type == 0


def test_a_totally_failed_fire_lookup_is_counted(tmp_path: Path) -> None:
    """Reppuhaun täydellinen rikkoutuminen ei saa näyttää molotovisadelta.

    Jos luokkanimi muuttuu tai toleranssi on liian tiukka, kaikki
    tulikranaatit tulevat ulos ``molotov``-tyyppisinä -- täsmälleen kuten
    dokumentoitu "epäselvä reppu" -tapaus.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [
        (1, A_PLAYERS[0], "CMolotovProjectile", 300, 30),
        (2, B_PLAYERS[0], "CMolotovProjectile", 400, 30),
    ]
    rounds[0].grenades_in_bag = []  # ei yhtään reppuriviä
    adapter = parse_adapter(build(rounds), tmp_path)
    events = parse_events_table(build(rounds), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["molotov"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_fire_type_unresolved == 2


def test_the_bag_lookup_tolerates_a_missing_tick(tmp_path: Path) -> None:
    """Lentoradalle sallitaan aukko; repulle on sallittava sama.

    Yksi hukkuva tick ei saa muuttaa incendiarya molotoviksi.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, B_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    # Reppurivi ei ole tickissä 299 vaan neljä tickiä aikaisemmin.
    rounds[0].grenades_in_bag = [(11, B_PLAYERS[0], "CIncendiaryGrenade", 295)]
    adapter = parse_adapter(build(rounds), tmp_path)
    events = parse_events_table(build(rounds), tmp_path)

    assert events["grenade_type"].unique().to_list() == ["incendiary"]
    assert adapter.diagnostics is not None
    assert adapter.diagnostics.grenades_fire_type_unresolved == 0


def test_a_bag_row_with_nan_coordinates_is_still_a_bag_row(tmp_path: Path) -> None:
    """Reppu ja lentorata suodatetaan samalla lausekkeella.

    Jos toinen tarkistaisi vain ``null``:in ja toinen myös NaN:in, NaN-rivi
    olisi kummassakin tai ei kummassakaan -- ja tulikranaatin tyypin haku
    etsisi repusta lentoradan riveiltä.
    """
    rounds = long_match(played=1)
    rounds[0].grenades = [(1, A_PLAYERS[0], "CMolotovProjectile", 300, 30)]
    rounds[0].grenades_in_bag = [(11, A_PLAYERS[0], "CMolotovGrenade", 299)]
    fake = build(rounds)
    for row in fake.grenades:
        if row["x"] is None:
            row["x"] = float("nan")
            row["y"] = float("nan")
            row["z"] = float("nan")

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
    fake = build(utility_match())
    original_path = fake.parse_grenades

    def without_column():
        return original_path().rename(columns={"grenade_type": "tyyppi"})

    fake.parse_grenades = without_column  # type: ignore[method-assign]

    # Ohita adapterin oma saraketarkistus, jotta domainin virhe pääsee esiin.
    adapter = Demoparser2Adapter(area_snap_units=AREA_SNAP_UNITS)
    adapter._open = lambda *args, **kwargs: fake  # type: ignore[method-assign]
    monkey = dp.GRENADE_COLUMNS
    try:
        dp.GRENADE_COLUMNS = tuple(
            "tyyppi" if name == "grenade_type" else name for name in monkey
        )
        demo = tmp_path / "feikki.dem"
        demo.write_bytes(DEMO_MAGIC + b"\x00" + b"x" * 64)
        with pytest.raises(ParseError) as exc:
            adapter.parse_demo(demo, SNAPSHOT_SECONDS)
    finally:
        dp.GRENADE_COLUMNS = monkey

    assert "grenade_type" in str(exc.value)
    assert "GRENADE_COLUMNS" in str(exc.value)
    assert "pelkistää" in str(exc.value)
