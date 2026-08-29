"""Jaetut Polars-taulusopimukset (AD-2, AD-4, AD-5).

Neljä taulua, jotka kaikki putken vaiheet lukevat ja kirjoittavat:

``ROUNDS``
    ``parsed/<map_demo_id>/rounds.parquet`` -- pitkä taulu, **kaksi riviä per
    kierros**, yksi kummallekin joukkueelle. Sisältää vain ``parse``-vaiheen
    *havaitsemat* arvot, ei johdoksia.
``TICKS``
    ``parsed/<map_demo_id>/ticks.parquet`` -- rivi per (pelaaja, kierros,
    näytepiste).
``EVENTS``
    ``parsed/<map_demo_id>/events.parquet`` -- rivi per utility-tapahtuma.
    Heitto ja räjähdys ovat kaksi riviä, jotka yhdistää ``grenade_entity_id``.
``CLASSIFIED``
    ``classified/<team_key>/<map_demo_id>.parquet`` -- **yksi rivi per kierros**
    subjektijoukkueen näkökulmasta. Sisältää ``classify``-vaiheen johdokset.

Jokaisessa taulussa on ``map_demo_id``, vaikka se on myös tiedostopolussa.
Syy: ``aggregate`` lukee kymmeniä demoja yhteen kehykseen, ja liitos
``(map_demo_id, round_no)`` on silloin ainoa oikea avain -- pelkkä ``round_no``
sekoittaisi eri karttojen kierrokset keskenään.

Nimeämiskonventio: englanninkielinen ``snake_case``, yksikkö nimessä.
``*money*`` ja ``*equip*`` ovat kokonaislukuja dollareita, ``*_s`` sekunteja
liukulukuna, koordinaatit ``x, y, z`` float32.

Jokainen vaihe validoi lukemansa ja kirjoittamansa taulun funktiolla
:func:`validate`. Validointi on tiukka molempiin suuntiin: sekä puuttuva että
ylimääräinen sarake on virhe. Syy on hiljaa tyhjäksi menevä Polars-join --
juuri se vika, jonka tämä sopimus estää.
"""

from __future__ import annotations

import polars as pl

from pappascout.constants import (
    AREA_SOURCES,
    EVENT_KINDS,
    ROSTER_CLASSES,
    ROUND_TYPES,
    SAMPLE_KINDS,
    SIDES,
    UNIT_STATUSES,
)
from pappascout.errors import SchemaError

__all__ = [
    "ROUNDS",
    "TICKS",
    "EVENTS",
    "CLASSIFIED",
    "CLASSIFIED_INPUTS",
    "SCHEMAS",
    "Schema",
    "validate",
]

Schema = dict[str, "pl.DataType | pl.DataTypeClass"]

_SIDE = pl.Enum(list(SIDES))
_ROUND_TYPE = pl.Enum(list(ROUND_TYPES))
_UNIT_STATUS = pl.Enum(list(UNIT_STATUSES))
_SAMPLE_KIND = pl.Enum(list(SAMPLE_KINDS))
_EVENT_KIND = pl.Enum(list(EVENT_KINDS))
_AREA_SOURCE = pl.Enum(list(AREA_SOURCES))
_ROSTER_CLASS = pl.Enum(list(ROSTER_CLASSES))


# Kierrostaulu: kaksi riviä per kierros, yksi kummallekin joukkueelle (AD-5).
# round_no on null warmupissa, puukkokierroksella ja mp_restartgame-nollauksissa
# -- vain parse asettaa sen.
ROUNDS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, liitosavain
    "round_raw": pl.Int32,  # demoparser2:n oma kierroslaskuri
    "round_no": pl.Int32,  # 1-pohjainen pelattu kierros, null jos ei pelattu
    "lineup_key": pl.Utf8,  # rivin joukkueen kokoonpanotunniste (AD-6)
    "side": _SIDE,  # rivin joukkueen puoli tällä kierroksella
    "won": pl.Boolean,
    "win_reason": pl.Utf8,
    "money_freeze_end": pl.Int32,  # $ jäljelle jäänyt saldo freezetimen lopussa
    "money_spent": pl.Int32,  # $ tällä kierroksella käytetty raha (cash_spent)
    "equip_freeze_end": pl.Int32,  # $ current_equip_value-summa freezetimen lopussa
    "equip_round_start": pl.Int32,  # $ round_start_equip_value-summa
    "players_freeze_end": pl.Int32,  # pelaajat, joiden arvot olivat luettavissa
    "survivors": pl.Int32,  # elossa kierroksen lopussa
    "survivors_equip_prev": pl.Int32,  # $ edelliseltä kierrokselta säästynyt varustearvo
    "freeze_end_tick": pl.Int32,  # viimeinen round_freeze_end -tick, null jos puuttuu
    "tick_rate": pl.Float32,
    "status": _UNIT_STATUS,
}

# Näytepistetaulu: rivi per (pelaaja, kierros, näytepiste) (AD-5).
TICKS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, liitosavain
    "round_raw": pl.Int32,
    "round_no": pl.Int32,
    "player_id": pl.Utf8,
    "lineup_key": pl.Utf8,
    "side": _SIDE,
    "sample_kind": _SAMPLE_KIND,  # "time" tai "first_contact"
    # s -- nimellisaika. HUOM: arvolla on kaksi eri semantiikkaa, jotka
    # erottaa vain sample_kind:
    #   sample_kind = "time"          -> [parse].snapshot_seconds -luku
    #                                    sellaisenaan (6.0, 15.0, ...), sama
    #                                    joka kierroksella, vertailukelpoinen
    #   sample_kind = "first_contact" -> mitattu hetki, sama kuin t_s, eri
    #                                    joka kierroksella
    # Siksi ryhmittely on aina tehtävä parilla (sample_kind, sample_t_s).
    # Pelkällä sample_t_s:llä ryhmittely sekoittaisi kaksi eri asiaa: 15.0
    # tarkoittaisi sekä "15 sekunnin näyte" että "kierros, jolla ensikontakti
    # sattui olemaan 15,0 s".
    "sample_t_s": pl.Float64,
    "t_s": pl.Float64,  # s -- aika viimeisestä round_freeze_endistä
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
    # pelin last_place_name; tuntematon alue on null, koordinaatit silti mukana
    "area": pl.Utf8,
    "is_alive": pl.Boolean,
}

# Utility-tapahtumataulu (AD-5). Heitto ja räjähdys ovat kaksi riviä, jotka
# yhdistää (round_no, grenade_entity_id) -- pelkkä entiteettitunniste ei riitä,
# koska peli kierrättää tunnisteet demon aikana. area ja x, y, z tarkoittavat
# event_kindin mukaan joko heitto- tai räjähdyspaikkaa.
# Utility mitataan heitoista, ei ostoista.
#
# Alue on kahdenlaista tietoa, ja area_source kertoo kummasta on kyse:
# heittorivillä se on heittäjän oma m_szLastPlaceName (havainto), räjähdyksellä
# lähimmän elossa olevan pelaajan alue etäisyysrajan parse.area_snap_units
# sisältä (approksimaatio). Ilman erottelua raportti esittäisi arvion
# havaintona.
EVENTS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, liitosavain
    "round_raw": pl.Int32,
    "round_no": pl.Int32,
    "event_kind": _EVENT_KIND,
    "grenade_entity_id": pl.Int32,
    "grenade_type": pl.Utf8,
    "thrower_id": pl.Utf8,
    "lineup_key": pl.Utf8,
    "side": _SIDE,
    "t_s": pl.Float64,
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
    "area": pl.Utf8,
    # observed = heittäjän oma alue, snapped = lähimmältä pelaajalta johdettu.
    # null aina ja vain silloin, kun area on null.
    "area_source": _AREA_SOURCE,
    # Etäisyys pelin yksiköissä lähimpään elossa olevaan pelaajaan silloin, kun
    # alue napsautettiin. null, jos alue on havaittu tai jos napsautusta ei
    # tehty. Kuluttaja erottaa tästä 40 yksikön osuman 490 yksikön arviosta.
    "snap_distance": pl.Float32,
}

# classify-vaiheen tallentamat päätöksen syötteet (AD-4): kaikki vertailuun
# käytetyt arvot, jotta raportin kierrosliite on tarkistettavissa demoa vasten.
# Kynnysarvot ovat dollareita per pelaaja, samat kuin [thresholds]-osiossa.
CLASSIFIED_INPUTS = pl.Struct(
    {
        "money_freeze_end": pl.Int32,
        # Käytettävissä ollut raha = money_freeze_end + money_spent. Se on
        # mukana perustelua ja tarkistusta varten; kalibrointi 2026-08-29
        # osoitti, että säännöt nojaavat jäljelle jääneeseen saldoon.
        "money_spent": pl.Int32,
        "equip_freeze_end": pl.Int32,
        "equip_round_start": pl.Int32,
        "survivors_prev": pl.Int32,
        "survivors_equip_prev": pl.Int32,
        "prev_round_won": pl.Boolean,
        # players = jakaja, jota per pelaaja -arvoissa oikeasti käytettiin;
        # players_readable = montako pelaajaa oli luettavissa. Ne eroavat vain,
        # jos havainto oli rajojen 1..roster_size ulkopuolella.
        "players": pl.Int32,
        "players_readable": pl.Int32,
        "full_equip_min": pl.Int32,
        # Force ja puoliosto: molempien edellytys on ostettu summa
        # (force_buy_min), ja ne eroavat toisistaan taskuun jääneestä rahasta
        # (force_money_left_max). Varustearvo ei erota niitä.
        "force_buy_min": pl.Int32,
        "force_money_left_max": pl.Int32,
        "anomaly_equip_max_after_win": pl.Int32,
    }
)

# Luokiteltu taulu: yksi rivi per kierros subjektijoukkueen näkökulmasta.
# Näytepisteitä ei kopioida -- aggregate liittää parsed/ticks-taulun avaimella
# (map_demo_id, round_no), joka on siksi mukana molemmissa tauluissa.
CLASSIFIED: Schema = {
    "map_demo_id": pl.Utf8,  # liitosavain parsed/ticks-tauluun
    "round_no": pl.Int32,
    "side": _SIDE,
    "won": pl.Boolean,
    "round_type": _ROUND_TYPE,
    "opp_round_type": _ROUND_TYPE,
    "loss_count": pl.Int32,
    "reason": pl.Utf8,
    "inputs": CLASSIFIED_INPUTS,
    "is_league": pl.Boolean,
    "roster_class": _ROSTER_CLASS,
}

# Nimi -> skeema, jotta virheilmoitus ja manifesti voivat viitata tauluun nimellä.
SCHEMAS: dict[str, Schema] = {
    "rounds": ROUNDS,
    "ticks": TICKS,
    "events": EVENTS,
    "classified": CLASSIFIED,
}


def _type_name(dtype: object) -> str:
    """Palauta luettava tyyppinimi virheilmoitusta varten."""
    return str(dtype)


def validate(df: pl.DataFrame, schema: Schema, name: str) -> pl.DataFrame:
    """Tarkista, että ``df`` vastaa täsmälleen sopimusta ``schema``.

    Tarkistus on tiukka molempiin suuntiin: puuttuva sarake, ylimääräinen sarake
    ja väärä tyyppi ovat kaikki virheitä. Sarakkeiden järjestyksellä ei ole
    väliä.

    Args:
        df: Tarkistettava taulu.
        schema: Sopimus muodossa ``{sarakenimi: Polars-tyyppi}``.
        name: Taulun nimi virheilmoitusta varten, esimerkiksi ``"rounds"``.

    Returns:
        Sama ``df`` muuttumattomana.

    Raises:
        SchemaError: Jos taulu ei vastaa sopimusta. Viesti nimeää tarkalleen
            puuttuvan, ylimääräisen tai väärätyyppisen sarakkeen.
    """
    actual = dict(df.schema)

    missing = [col for col in schema if col not in actual]
    if missing:
        rivit = ", ".join(f"{col} ({_type_name(schema[col])})" for col in missing)
        raise SchemaError(
            f"Taulusta {name!r} puuttuu sarake: {rivit}. "
            "Lisää sarake tai korjaa taulun tuottanut vaihe -- "
            "sopimus on tiedostossa domain/schemas.py."
        )

    extra = [col for col in actual if col not in schema]
    if extra:
        rivit = ", ".join(f"{col} ({_type_name(actual[col])})" for col in extra)
        raise SchemaError(
            f"Taulussa {name!r} on ylimääräinen sarake: {rivit}. "
            "Poista sarake tai lisää se sopimukseen tiedostossa domain/schemas.py."
        )

    wrong = [
        (col, schema[col], actual[col]) for col in schema if actual[col] != schema[col]
    ]
    if wrong:
        rivit = "; ".join(
            f"{col}: odotettiin {_type_name(exp)}, saatiin {_type_name(got)}"
            for col, exp, got in wrong
        )
        raise SchemaError(
            f"Taulun {name!r} sarakkeella on väärä tyyppi -- {rivit}. "
            "Muunna sarake oikeaan tyyppiin ennen kirjoitusta."
        )

    return df
