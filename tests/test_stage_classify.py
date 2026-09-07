"""``stages.classify`` -- vaiheen testit.

Vaihe ei lue demoa lainkaan, joten sen koko logiikka -- kierrostaulun luku,
molempien joukkueiden luokittelu, kierroslista, manifesti ja ohitus --
testataan käsin rakennetulla kierrostaululla. Ainoat demoa vaativat testit ovat
lopun regressiot, ja ne ohittavat itsensä siististi.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from conftest import (
    ANCIENT_DEM,
    ANCIENT_ROUNDS,
    NUKE_ROUNDS,
    NUKE_ZST,
    REAL_SETTINGS,
    even_split,
    has_temp_leftovers,
    require_demo,
    settings_text,
)
from pappascout.adapters.protocols import (
    CALLOUTS_ADAPTER_COLUMNS,
    DEATHS_ADAPTER_COLUMNS,
    EVENTS_ADAPTER_COLUMNS,
    LINEUPS_ADAPTER_COLUMNS,
    MATCH_ADAPTER_COLUMNS,
    TICKS_ADAPTER_COLUMNS,
    DemoTables,
)
from pappascout.archive.manifest import Manifest
from pappascout.archive.paths import ArchivePaths
from pappascout.domain.economy import per_player
from pappascout.domain.models import load_settings
from pappascout.domain.rounds import mark_played_rounds
from pappascout.domain.selection import MapSelection
from pappascout.domain.teams import Team, assign_lineup_keys
from pappascout.domain.schemas import (
    ARMED_COLUMN,
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
from pappascout.errors import PappascoutError, SchemaError
from test_calibration import TRUTH_TABLE
from pappascout.stages import classify as classify_stage
from pappascout.stages import discover as discover_stage
from pappascout.stages import parse as parse_stage
from pappascout.stages import select as select_stage

MAP_DEMO_ID = "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd-1-1"

#: Tiedostottoman kutsujan **eksplisiittinen** tyhjä. ``classify_rounds``
#: vaatii faktat avainsanaparametrina eikä oleta niitä tyhjiksi: oletus tekisi
#: unohtamisesta hiljaisen ja tuottaisi juuri sen tyhjän sarakkeen, jonka
#: korjaamisesta tämä koodi on.
NO_FACTS = classify_stage.MatchFacts()

A = "aaaaaaaaaaaaaaaa"
B = "bbbbbbbbbbbbbbbb"


# --- Kierrostaulun rakennus ------------------------------------------------------


def round_rows(
    round_no: int,
    *,
    a_side: str = "T",
    a_won: bool = True,
    a_money: int = 5000,
    a_spent: int = 20000,
    a_equip: int = 25000,
    a_start: int = 1000,
    b_money: int = 5000,
    b_spent: int = 20000,
    b_equip: int = 25000,
    b_start: int = 1000,
    a_players: int | None = 5,
    b_players: int | None = 5,
    status: str = "ok",
) -> list[dict[str, object]]:
    """Yhden kierroksen kaksi riviä, yksi kummallekin kokoonpanolle.

    Voiton syy valitaan puolen mukaan, jotta CS2:n sääntöinvariantti pysyy
    voimassa myös tässä käsin rakennetussa taulussa.
    """
    b_side = "CT" if a_side == "T" else "T"
    win_reason = "ct_killed" if (a_side == "T") == a_won else "t_killed"
    rows = []
    for lineup, side, won, money, spent, equip, start, players in (
        (A, a_side, a_won, a_money, a_spent, a_equip, a_start, a_players),
        (B, b_side, not a_won, b_money, b_spent, b_equip, b_start, b_players),
    ):
        rows.append(
            {
                "map_demo_id": MAP_DEMO_ID,
                "round_raw": round_no + 1,
                "round_no": round_no,
                "lineup_key": lineup,
                "side": side,
                "won": won,
                "win_reason": win_reason,
                "money_buy_end": None if status != "ok" else money,
                "money_spent": None if status != "ok" else spent,
                "equip_buy_end": None if status != "ok" else equip,
                "equip_round_start": None if status != "ok" else start,
                "players_buy_end": None if status != "ok" else players,
                # Puolioston kaksi havaintoa johdetaan muista arvoista, jotta
                # rivi on sisäisesti johdonmukainen: jakauman summa on
                # money_buy_end ja laskurin katto players_buy_end. Testi, joka
                # tutkii nimenomaan niitä, rakentaa oman rivinsä.
                ARMED_COLUMN: None if status != "ok" else players,
                MONEY_DISTRIBUTION_COLUMN: (
                    None
                    if status != "ok" or not players
                    else even_split(money, players)
                ),
                "survivors": 2 if won else 0,
                "survivors_equip_prev": 0,
                "freeze_end_tick": None if status != "ok" else 1000 * round_no,
                # Eri kuin ankkuri, kuten oikeassa ajossa: tyhjäksi jätetty
                # sarake ei paljastaisi, jos vaihe pudottaisi sen matkalta.
                "buy_end_tick": None if status != "ok" else 1000 * round_no + 1280,
                "tick_rate": 64.0,
                "status": status,
            }
        )
    return rows


def rounds_frame(rounds: list[list[dict[str, object]]]) -> pl.DataFrame:
    rows = [r for pair in rounds for r in pair]
    df = pl.DataFrame(rows, schema=dict(ROUNDS), orient="row")
    return validate(df, ROUNDS, "rounds")


def match(played: int = 6) -> list[list[dict[str, object]]]:
    """Yksinkertainen ottelu: A voittaa pistoolin, sen jälkeen vuorotellen."""
    rounds = [round_rows(1, a_won=True, a_equip=4000, b_equip=4000)]
    for no in range(2, played + 1):
        rounds.append(round_rows(no, a_won=no % 2 == 0))
    return rounds


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    root = tmp_path / "arkisto"
    root.mkdir()
    return ArchivePaths(root=root)


@pytest.fixture
def settings(settings_file: Path):
    return load_settings(settings_file, env_files=())


def _minimal_deaths(frame: pl.DataFrame) -> pl.DataFrame:
    """Kuolemataulu kierrostaulusta, portin sopimuksen mukaisena.

    Luokittelu ei lue kuolemia lainkaan, mutta ``parse`` kieltäytyy
    kirjoittamasta tyhjää kuolemataulua: pelatussa ottelussa kuollaan.
    Yksi kuolema per kierros riittää, ja uhri on sama pelaaja kuin
    :func:`_minimal_ticks`issa, jotta taulut eivät ole eri mieltä.
    """
    rows: list[dict[str, object]] = []
    for row in frame.iter_rows(named=True):
        if row["round_no"] is None or row["side"] != "T":
            continue
        rows.append(
            {
                "round_raw": row["round_raw"],
                "round_no": None,
                "t_s": 20.0,
                "victim_id": f"{row['lineup_key']}-1",
                "victim_lineup_key": row["lineup_key"],
                "victim_side": row["side"],
                "victim_x": 1.0,
                "victim_y": 2.0,
                "victim_z": 3.0,
                "victim_area": "Middle",
                "attacker_id": None,
                "attacker_lineup_key": None,
                "attacker_side": None,
                "attacker_x": None,
                "attacker_y": None,
                "attacker_z": None,
                "attacker_area": None,
                "weapon": "planted_c4",
            }
        )
    return pl.DataFrame(
        rows,
        schema={name: DEATHS[name] for name in DEATHS_ADAPTER_COLUMNS},
        orient="row",
    )


def _minimal_lineups(frame: pl.DataFrame) -> pl.DataFrame:
    """Kokoonpanotaulu kierrostaulun kokoonpanoista, portin sopimuksen mukaisena.

    Luokittelu ei lue nimiä lainkaan, mutta ``parse`` kieltäytyy
    kirjoittamasta tyhjää kokoonpanotaulua: kokoonpanot tunnistetaan
    jokaisesta demosta.
    Pelaajatunnisteet ovat samat kuin :func:`_minimal_ticks`issa, jotta
    taulut eivät ole eri mieltä kokoonpanosta.
    """
    rows = [
        {
            "lineup_key": lineup,
            "player_id": f"{lineup}-1",
            "player_name": f"{lineup}-pelaaja",
            "clan_name": f"Klaani-{lineup}",
        }
        for lineup in sorted(
            {
                row["lineup_key"]
                for row in frame.iter_rows(named=True)
                if row["round_no"] is not None
            }
        )
    ]
    return pl.DataFrame(
        rows,
        schema={name: LINEUPS[name] for name in LINEUPS_ADAPTER_COLUMNS},
        orient="row",
    )


def _minimal_ticks(frame: pl.DataFrame) -> pl.DataFrame:
    """Yksi näytepiste per kierrosrivi, portin sopimuksen mukaisena.

    Luokittelu ei lue näytepisteitä lainkaan, mutta ``parse`` kieltäytyy
    kirjoittamasta tyhjää asetelmataulua ei-tyhjälle kierrostaululle. Tämä
    pitää kiinnikkeen rehellisenä: se tuottaa sen mitä oikea adapteri
    tuottaisi, ei tyhjää kuorta.
    """
    rows = [
        {
            "round_raw": row["round_no"],
            "round_no": None,
            "player_id": f"{row['lineup_key']}-1",
            "lineup_key": row["lineup_key"],
            "side": row["side"],
            "sample_kind": "time",
            "sample_t_s": 6.0,
            "t_s": 6.0,
            "x": 1.0,
            "y": 2.0,
            "z": 3.0,
            "area": "Middle",
            "is_alive": True,
        }
        for row in frame.iter_rows(named=True)
        if row["round_no"] is not None
    ]
    return pl.DataFrame(
        rows,
        schema={name: TICKS[name] for name in TICKS_ADAPTER_COLUMNS},
        orient="row",
    )


def write_parse(
    archive: ArchivePaths,
    frame: pl.DataFrame,
    parse_settings,
    *,
    force: bool = False,
) -> None:
    """Kirjoita kierrostaulu ja aito ``parse``-manifesti arkistoon.

    Manifesti kirjoitetaan oikealla vaiheella eikä käsin, jotta ohitusketju
    ``parse -> classify`` testataan sellaisena kuin se tuotannossa on.
    Demotiedostoa ei kirjoiteta uudelleen, jos se on jo olemassa: sen koko ja
    muokkausaika ovat osa parsinnan syötetunnistetta.
    """
    demo = archive.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.parent.mkdir(parents=True, exist_ok=True)
    if not demo.exists():
        demo.write_bytes(b"PBDEMS2\x00" + b"x" * 512)

    adapter = frame.drop("map_demo_id").with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        (pl.col("round_no") - 1).alias("score_start"),
        pl.col("round_no").alias("score_end"),
    )

    # Näytepistetaulu on Story 2.1:n tulos eikä vaikuta luokitteluun, mutta se
    # ei saa olla tyhjä: parse hylkää asetelmattoman tuloksen. Feikki antaa
    # siksi yhden näytepisteen per kierros, samoilla avaimilla kuin
    # kierrostaulussa.
    ticks_frame = _minimal_ticks(frame)

    # Utility ei vaikuta luokitteluun lainkaan, ja tyhjä tapahtumataulu on
    # kelvollinen tulos -- toisin kuin tyhjä asetelmataulu. Kiinnike antaa siis
    # tyhjän mutta sopimuksen mukaisen taulun.
    events_frame = pl.DataFrame(
        schema={name: EVENTS[name] for name in EVENTS_ADAPTER_COLUMNS}
    )

    lineups_frame = _minimal_lineups(frame)
    deaths_frame = _minimal_deaths(frame)

    # Pistepilvi ei vaikuta luokitteluun lainkaan, ja tyhjä pilvi on
    # kelvollinen tulos -- samoin kuin tyhjä tapahtumataulu. Kiinnike antaa
    # siis tyhjän mutta sopimuksen mukaisen taulun.
    callouts_frame = pl.DataFrame(
        schema={name: CALLOUT_CLOUD[name] for name in CALLOUTS_ADAPTER_COLUMNS}
    )

    # Kartan nimi ei vaikuta luokitteluun lainkaan, mutta ottelutaulun on
    # oltava paikallaan: parse vaatii siitä täsmälleen yhden rivin.
    match_frame = pl.DataFrame(
        [{"map_name": "de_ancient"}],
        schema={name: MATCH[name] for name in MATCH_ADAPTER_COLUMNS},
    )

    class Fake:
        def parse_demo(self, path: Path, sample_seconds) -> DemoTables:
            return DemoTables(
                rounds=adapter,
                ticks=ticks_frame,
                events=events_frame,
                lineups=lineups_frame,
                deaths=deaths_frame,
                callouts=callouts_frame,
                match=match_frame,
            )

    parse_stage.run(
        parse_settings, archive, MAP_DEMO_ID, Fake(), demo_path=demo, force=force
    )


@pytest.fixture
def parsed(archive: ArchivePaths, settings) -> ArchivePaths:
    write_parse(archive, rounds_frame(match()), settings.parse)
    return archive


def run_classify(settings, archive, team=A, **kwargs):
    return classify_stage.run(
        settings.thresholds,
        settings.league,
        archive,
        MAP_DEMO_ID,
        team,
        economy=settings.economy,
        **kwargs,
    )


# --- Onnistunut ajo --------------------------------------------------------------


def test_writes_a_valid_classified_table(settings, parsed) -> None:
    result = run_classify(settings, parsed)

    path = parsed.classified(A, MAP_DEMO_ID)
    assert path.is_file()
    df = pl.read_parquet(path)
    assert df.schema == dict(CLASSIFIED)
    assert df.height == 6, "yksi rivi per kierros, ei kahta"
    assert df["round_no"].to_list() == [1, 2, 3, 4, 5, 6]
    assert df["map_demo_id"].unique().to_list() == [MAP_DEMO_ID]
    assert result.status == "ok"
    assert not result.skipped
    assert result.stats["rounds"] == 6


def test_result_is_written_under_the_subject_team(settings, parsed) -> None:
    run_classify(settings, parsed, team=A)
    assert parsed.classified(A, MAP_DEMO_ID).is_file()
    assert not parsed.classified(B, MAP_DEMO_ID).exists()


def test_every_row_carries_a_reason_and_its_inputs(settings, parsed) -> None:
    """Ilman perustelua ja lähtöarvoja kalibrointi Story 1.4:ssä on mahdotonta."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df["reason"].null_count() == 0
    assert all(len(r) > 20 for r in df["reason"].to_list())
    for inputs in df["inputs"].to_list():
        assert inputs["players"] == 5
        assert inputs["full_equip_min"] == settings.thresholds.full_equip_min


def test_pistol_round_is_classified_from_the_round_number(settings, parsed) -> None:
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df.filter(pl.col("round_no") == 1)["round_type"].to_list() == ["pistol"]


def test_both_teams_are_classified_in_the_same_run(settings, parsed) -> None:
    """``opp_round_type`` on toisen joukkueen oma ``round_type`` samalta ajolta."""
    run_classify(settings, parsed, team=A)
    run_classify(settings, parsed, team=B)

    a = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    b = pl.read_parquet(parsed.classified(B, MAP_DEMO_ID)).sort("round_no")

    assert a["round_type"].to_list() == b["opp_round_type"].to_list()
    assert b["round_type"].to_list() == a["opp_round_type"].to_list()
    assert a["side"].to_list() != b["side"].to_list()


def test_loss_count_is_written_per_round(settings, parsed) -> None:
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    assert df["loss_count"][0] == settings.thresholds.loss_count_half_start
    assert df["loss_count"].is_between(
        settings.thresholds.loss_count_min, settings.thresholds.loss_count_max
    ).all()


def test_without_a_selection_file_the_league_fields_stay_empty(
    settings, parsed
) -> None:
    """Käsin tuotu demo: arvaus olisi pahempi kuin tyhjä.

    Tämä on myös se testi, joka kaatuu, jos puuttuvasta valintatiedostosta
    tehdään poikkeus: ajon on onnistuttava ja arvojen jäätävä tyhjiksi.
    """
    result = run_classify(settings, parsed)

    assert result.status == "ok"
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    assert df["is_league"].null_count() == df.height
    assert df["roster_class"].null_count() == df.height


def test_write_is_atomic(settings, parsed) -> None:
    run_classify(settings, parsed)
    assert not has_temp_leftovers(parsed.root)


def test_nothing_is_written_into_the_parsed_area(settings, parsed) -> None:
    """``classify`` ei kirjoita toisen vaiheen tulosalueelle."""
    before = {
        p: p.stat().st_mtime_ns
        for p in (parsed.root / "parsed").rglob("*")
        if p.is_file()
    }
    run_classify(settings, parsed)
    after = {
        p: p.stat().st_mtime_ns
        for p in (parsed.root / "parsed").rglob("*")
        if p.is_file()
    }
    assert before == after


# --- is_league ja roster_class valintatiedostosta -------------------------------
#
# Arvot ovat ``select``in laskemia, ja tämä vaihe on niiden lukija. Kiinnike
# kirjoittaa siksi molemmat tiedostot käsin: joukkueindeksin, joka on silta
# kokoonpanotunnisteesta kanoniseen ``team_key``:hin, ja valintatiedoston,
# jossa arvot ovat.

#: Kanoninen ``team_key`` on FACEITin ``faction_id`` eli UUID -- **ei**
#: kokoonpanotiiviste. Juuri tämä ero on syy sille, että haku kulkee
#: joukkueindeksin ``lineup_keys``-kentän kautta: suora haku
#: ``index/selections/<lineup_key>.json`` osuisi aina tyhjään.
TEAM_KEY = "0047af32-5ff8-449e-b665-8fd390e6a44d"
OTHER_TEAM_KEY = "f257054b-46d5-41bb-8e01-543777cd7092"


#: Fixtuurien aikaleima, **menneisyydessä**: valintatiedosto on silloin
#: vanhempi kuin ajossa syntyvä manifesti, eikä vanhentumisvaroitus laukea.
#: Varoituksella on oma testinsä omalla aikaleimallaan.
PAST = "2026-09-01T12:00:00+00:00"

#: Indeksien ja valintatiedoston muotoversiot **kirjaimellisina**. Vakioiden
#: (``discover.SCHEMA_VERSION``, ``select.SCHEMA_VERSION``) lainaaminen
#: tekisi fixtuurista aina ajan tasalla olevan, vaikka muoto olisi
#: vanhentunut; kirjaimellinen luku pakottaa katsomaan fixtuuria, kun muoto
#: nousee -- ja lukija näkee mitä vasten tämä testi on kirjoitettu.
TEAMS_INDEX_VERSION = 1
SELECTION_VERSION = 1


def write_teams_index(archive: ArchivePaths, owners: dict[str, list[str]]) -> None:
    """Joukkueindeksi, jossa jokainen ``team_key`` omistaa annetut kokoonpanot."""
    keys = [key for lineups in owners.values() for key in lineups]
    document = {
        "schema_version": TEAMS_INDEX_VERSION,
        "generated_at": PAST,
        "competition_ids": ["kilpailu"],
        "contested_lineup_keys": sorted({k for k in keys if keys.count(k) > 1}),
        "teams": [
            {"team_key": team_key, "lineup_keys": list(lineups), "roster": []}
            for team_key, lineups in owners.items()
        ],
    }
    write_json(archive.teams_index(), document)


def write_json(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def selection_row(
    *,
    map_demo_id: str = MAP_DEMO_ID,
    is_league: object = True,
    roster_class: str | None = "5/5",
    roster_ok: bool = True,
) -> dict[str, object]:
    """Valintarivi kaikilla kentillä, kuten ``select`` sen kirjoittaa."""
    return {
        "map_demo_id": map_demo_id,
        "match_id": "1-a52ebff2-a23d-45eb-beb7-37271d96ddfd",
        "map_index": 1,
        "map_name": "de_ancient",
        "is_league": is_league,
        "certainly_played": True,
        "roster_ok": roster_ok,
        "roster_reason": "kynnys täyttyi" if roster_ok else "kynnys ei täyttynyt",
        "roster_class": roster_class,
        "roster_source": "match_players",
        "players_seen": 5,
        "regulars": [],
        "outsiders": [],
        "joined": [],
        "left": [],
    }


def write_selection(
    archive: ArchivePaths,
    rows: list[dict[str, object]],
    *,
    team_key: str = TEAM_KEY,
    generated_at: str = PAST,
) -> None:
    """Valintatiedosto joukkueelle, muodossa jonka ``read_selection`` hyväksyy."""
    document = {
        "schema_version": SELECTION_VERSION,
        "generated_at": generated_at,
        "index_generated_at": generated_at,
        "competition_ids": ["kilpailu"],
        "team_key": team_key,
        "team_name": "Testijoukkue",
        "roster_size": 5,
        "roster_min_regulars": 4,
        "roster": [],
        "counts": {},
        "selections": rows,
    }
    write_json(archive.selection(team_key), document)


def facts_of(archive: ArchivePaths, team: str = A) -> tuple[list, list]:
    """Taulun kaksi saraketta uniikkeina arvoina.

    Uniikkina siksi, että väite on kaksiosainen: arvo on oikea **ja** sama
    jokaisella rivillä. Yhden rivin tarkistus ei huomaisi, jos arvo latottaisi
    vain ensimmäiselle -- ja juuri sen ``domain.aggregate`` kaataisi.
    """
    df = pl.read_parquet(archive.classified(team, MAP_DEMO_ID))
    assert df.height > 1, "latominen kaikille riveille on osa väitettä"
    return (
        df["is_league"].unique().to_list(),
        df["roster_class"].unique().to_list(),
    )


def test_the_match_facts_are_read_from_the_selection_file(settings, parsed) -> None:
    """Liigaottelu, jonka rosteri kelpasi: molemmat sarakkeet täyttyvät."""
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])


def test_another_faceit_match_is_written_as_not_league(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league=False)])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([False], ["5/5"])


def test_a_substitute_map_carries_the_four_of_five_class(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class="4/5")])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["4/5"])


def test_the_roster_class_is_read_and_not_recomputed(settings, parsed) -> None:
    """Tiedosto sanoo ``4/5``, vaikka rosteri näyttäisi ``5/5``:ltä.

    Kierrostaulussa on viisi pelaajaa joka kierroksella, joten uudelleen
    laskettu luokka olisi ``5/5``. ``select`` on ainoa laskija: se näkee
    ottelun pelaajalistan ja vakirosterin, joita tämä vaihe ei näe. Testi
    kaatuu, jos luokka lasketaan täällä uudelleen.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class="4/5")])

    run_classify(settings, parsed)

    rounds = pl.read_parquet(parsed.parsed_table(MAP_DEMO_ID, "rounds"))
    own = rounds.filter(pl.col("lineup_key") == A)
    assert own["players_buy_end"].unique().to_list() == [5], (
        "fikstuurin rosteri on täysi -- muuten testi ei erottaisi lukemista "
        "laskemisesta"
    )
    assert facts_of(parsed) == ([True], ["4/5"])


def test_a_rejected_map_still_carries_the_match_facts(settings, parsed) -> None:
    """Hylkäys on otannan asia eikä tosiasia ottelusta: arvot luetaan silti."""
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_ok=False, roster_class="4/5")])

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["4/5"])


def test_a_missing_selection_file_leaves_both_empty(settings, parsed) -> None:
    """Joukkue on indeksissä, mutta ``select``iä ei ole ajettu sille."""
    write_teams_index(parsed, {TEAM_KEY: [A]})

    result = run_classify(settings, parsed)

    assert result.status == "ok"
    assert facts_of(parsed) == ([None], [None])
    assert "pappascout select" in (result.reason or "")


def test_every_empty_reason_is_named_and_they_differ(settings, parsed) -> None:
    """Viisi eri syytä, viisi eri lausetta -- ei yhtä hiljaista tyhjää.

    Ilman tätä väitettä rikkoutunut silta näyttäisi raportissa täsmälleen
    samalta kuin käsin tuotu demo, ja "miksi otanta on tuntematon" olisi
    arvattava. AD-9: vajaa tulos kuuluu ``reason``iin eikä vaikenemiseen.
    """
    reasons: dict[str, str] = {}

    # 1. Indeksiä ei ole lainkaan.
    reasons["no_index"] = run_classify(settings, parsed, force=True).reason or ""

    # 2. Indeksi on, mutta kokoonpanolla ei omistajaa.
    write_teams_index(parsed, {TEAM_KEY: [B]})
    reasons["no_owner"] = run_classify(settings, parsed, force=True).reason or ""

    # 3. Omistaja on, valintatiedostoa ei.
    write_teams_index(parsed, {TEAM_KEY: [A]})
    reasons["no_file"] = run_classify(settings, parsed, force=True).reason or ""

    # 4. Tiedosto on, demolle ei riviä.
    write_selection(parsed, [selection_row(map_demo_id="1-toinen-demo-1-1")])
    reasons["no_row"] = run_classify(settings, parsed, force=True).reason or ""

    # 5. Kaksi omistajaa, eri mieltä ottelun lajista.
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league=True)], team_key=TEAM_KEY)
    write_selection(
        parsed, [selection_row(is_league=False)], team_key=OTHER_TEAM_KEY
    )
    reasons["conflict"] = run_classify(settings, parsed, force=True).reason or ""

    assert all(reasons.values()), f"jokainen tila kertoo syyn: {reasons}"
    assert len(set(reasons.values())) == len(reasons), (
        f"viisi eri syytä, viisi eri lausetta: {reasons}"
    )
    assert "joukkueindeksi" in reasons["no_index"].lower()
    assert A in reasons["no_owner"]
    assert "pappascout select" in reasons["no_file"]
    assert MAP_DEMO_ID in reasons["no_row"]
    assert "is_league" in reasons["conflict"]


def test_a_demo_that_has_no_row_in_the_file_leaves_both_empty(
    settings, parsed
) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(map_demo_id="1-toinen-demo-1-1")])

    result = run_classify(settings, parsed)

    assert result.status == "ok"
    assert facts_of(parsed) == ([None], [None])


def test_the_bridge_reads_the_lineup_keys_that_discover_writes(
    settings, parsed
) -> None:
    """Silta rakennetaan **tuottajan omalla kirjoittajalla**, ei käsin.

    Käsin kirjoitettu indeksi pinnaa vain testin omat merkkijonot: jos
    ``discover`` kirjoittaisi ``lineup_keys``iin eri tiivistesyötteen, eri
    pituuden tai etuliitteen, ``owners`` olisi tyhjä **joka ainoalla demolla**
    eikä yksikään käsin kirjoitettu fixtuuri kaatuisi -- ja tulos näyttäisi
    samalta kuin aidosti tuntematon demo. Siksi kokoonpanot luetaan
    ``discover``in omalla lukijalla, liitetään sen omalla säännöllä ja
    kirjoitetaan sen omalla dokumentinrakentajalla; väite on, että sama
    nimiavaruus tulee ulos ``classify``n päästä.

    Kynnys on nolla, koska tämän testin kohde on **tunnisteiden nimiavaruus**
    eikä rosterisääntö: kiinnikkeen pelaajatunnisteet eivät ole SteamID64:iä,
    joten rosterileikkaus ei olisi tässä mielekäs. Kynnyksellä on omat
    testinsä ``test_teams.py``:ssä.
    """
    lineups: dict[str, set[str]] = {}
    discover_stage._read_lineups(parsed, MAP_DEMO_ID, lineups)
    assert set(lineups) == set(classify_stage.team_keys(parsed, MAP_DEMO_ID)), (
        "discover ja classify lukevat kokoonpanot samasta taulusta samalla "
        "nimellä"
    )

    teams, contested = assign_lineup_keys((Team(team_key=TEAM_KEY),), lineups, 0)
    document = discover_stage._teams_document(
        teams, contested, ["kilpailu"], datetime(2026, 9, 1, 12, tzinfo=UTC)
    )
    write_json(parsed.teams_index(), document)

    written = {key for row in document["teams"] for key in row["lineup_keys"]}
    assert set(classify_stage.team_keys(parsed, MAP_DEMO_ID)) & written, (
        "sillan molemmat päät ovat samassa nimiavaruudessa"
    )

    # Ja silta kantaa arvon perille asti, ei vain nimeä.
    write_selection(parsed, [selection_row()])
    facts = classify_stage.read_match_facts(parsed, A, MAP_DEMO_ID)
    assert (facts.is_league, facts.roster_class) == (True, "5/5")
    assert facts.note is None


def test_a_lineup_that_no_team_owns_leaves_both_empty(settings, parsed) -> None:
    """Silta puuttuu: indeksissä oleva joukkue ei omista tätä kokoonpanoa."""
    write_teams_index(parsed, {TEAM_KEY: [B]})
    write_selection(parsed, [selection_row()])

    result = run_classify(settings, parsed, team=A)

    assert facts_of(parsed, A) == ([None], [None])
    # Erotettavissa aidosti tuntemattomasta demosta: syy nimeää kokoonpanon,
    # jolle omistajaa ei löytynyt.
    assert A in (result.reason or "")


def test_each_team_gets_the_facts_from_its_own_selection_file(
    settings, parsed
) -> None:
    """``--kaikki-joukkueet``: kumpikin ajo lukee oman joukkueensa tiedoston."""
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [B]})
    write_selection(parsed, [selection_row(roster_class="5/5")], team_key=TEAM_KEY)
    write_selection(
        parsed,
        [selection_row(roster_class="4/5", is_league=False)],
        team_key=OTHER_TEAM_KEY,
    )

    for team in classify_stage.team_keys(parsed, MAP_DEMO_ID):
        run_classify(settings, parsed, team=team)

    assert facts_of(parsed, A) == ([True], ["5/5"])
    assert facts_of(parsed, B) == ([False], ["4/5"])


def test_a_contested_lineup_that_agrees_still_fills_the_columns(
    settings, parsed
) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()], team_key=TEAM_KEY)
    write_selection(parsed, [selection_row()], team_key=OTHER_TEAM_KEY)

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])


def test_only_the_disagreeing_field_is_emptied(settings, parsed) -> None:
    """Kaksi omistajaa, eri ``roster_class``, sama ``is_league``.

    Luokka arvioidaan **kyseisen joukkueen** vakirosteria vasten (AD-6), joten
    kahdella omistajalla saa olla siitä eri arvo -- se ei ole ristiriita vaan
    normaalia. ``is_league`` kuvaa ottelua (AD-10), ja siitä omistajat ovat
    yksimielisiä. Tietuetasoinen vertailu heittäisi yksimielisen
    ``is_league``in pois vain siksi, että luokat erosivat; tämä testi kaatuu,
    jos konsensus palaa tietuetasolle.
    """
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class="5/5")], team_key=TEAM_KEY)
    write_selection(
        parsed, [selection_row(roster_class="4/5")], team_key=OTHER_TEAM_KEY
    )

    result = run_classify(settings, parsed)

    assert result.status == "ok"
    assert facts_of(parsed) == ([True], [None])
    assert "roster_class" in (result.reason or "")
    assert "is_league" not in (result.reason or "")


def test_a_disagreeing_league_flag_empties_only_that_field(
    settings, parsed
) -> None:
    """Sama sääntö toiseen suuntaan: laji eri mieltä, luokka sama."""
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league=True)], team_key=TEAM_KEY)
    write_selection(
        parsed, [selection_row(is_league=False)], team_key=OTHER_TEAM_KEY
    )

    result = run_classify(settings, parsed)

    assert facts_of(parsed) == ([None], ["5/5"])
    assert "is_league" in (result.reason or "")


def test_two_rows_for_the_same_demo_are_not_resolved_by_the_first_one(
    settings, parsed
) -> None:
    """Kahdennettu rivi menee samaan konsensukseen kuin kaksi omistajaa.

    "Ensimmäinen voittaa" olisi täsmälleen se arpominen, joka
    kiistanalaisilla kokoonpanoilla kiellettiin. Rivit ovat samassa
    tiedostossa ja eri mieltä luokasta, joten luokka jää tyhjäksi -- ei
    ensimmäisen arvoon.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(
        parsed,
        [selection_row(roster_class="5/5"), selection_row(roster_class="4/5")],
    )

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], [None])


def test_one_owner_with_a_file_is_enough(settings, parsed) -> None:
    """Kaksi omistajaa, vain toisella valintatiedosto: yksi ääni riittää.

    Yksimielisyys yhdellä äänellä on tarkoituksellista: puuttuva tiedosto ei
    ole eri mieltä vaan hiljaa, eikä hiljaisuus voi kumota luettua arvoa.
    """
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()], team_key=TEAM_KEY)

    result = run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])
    assert result.reason is None


def test_an_owner_whose_file_lacks_the_demo_does_not_veto(
    settings, parsed
) -> None:
    """Sama sisar: toisen omistajan tiedostossa on vain toisen demon rivi."""
    write_teams_index(parsed, {TEAM_KEY: [A], OTHER_TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()], team_key=TEAM_KEY)
    write_selection(
        parsed,
        [selection_row(map_demo_id="1-toinen-demo-1-1", roster_class="4/5")],
        team_key=OTHER_TEAM_KEY,
    )

    run_classify(settings, parsed)

    assert facts_of(parsed) == ([True], ["5/5"])


def test_the_field_names_come_from_selects_own_document_builder(
    settings, parsed
) -> None:
    """Rivi rakennetaan ``select``in omalla kirjoittajalla, ei käsin.

    Kaikki muut testit kaivavat raakoja avaimia käsin kirjoitetusta rivistä,
    joten nimen vaihtaminen ``select``issä (``is_league`` -> ``league``)
    pitäisi ne vihreinä ja tyhjentäisi tuotannon hiljaa. Tämä testi kulkee
    tuottajan läpi: :class:`MapSelection` -> ``select._document`` ->
    :func:`read_match_facts`, joten kenttänimi on pinnattu siihen koodiin,
    joka sen kirjoittaa.
    """
    row = MapSelection(
        map_demo_id=MAP_DEMO_ID,
        match_id="1-a52ebff2-a23d-45eb-beb7-37271d96ddfd",
        map_index=1,
        map_name="de_ancient",
        is_league=True,
        roster_ok=True,
        roster_reason="kynnys täyttyi",
        roster_class="4/5",
        roster_source="observed",
    )
    document = select_stage._document(
        [row],
        team=Team(team_key=TEAM_KEY),
        league=settings.league,
        thresholds=settings.thresholds,
        generated_at=datetime(2026, 9, 1, 12, tzinfo=UTC),
        index_generated_at=PAST,
    )
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_json(parsed.selection(TEAM_KEY), document)

    facts = classify_stage.read_match_facts(parsed, A, MAP_DEMO_ID)

    assert (facts.is_league, facts.roster_class) == (True, "4/5")
    assert facts.note is None


def test_a_foreign_roster_class_stops_the_run(settings, parsed) -> None:
    """Kelvoton arvo johdetaan skeemasta eikä kirjoiteta kovakoodattuna.

    Tarkistus ja virheilmoitus tulevat molemmat ``CLASSIFIED``-skeeman
    enumista, joten testin on kysyttävä samasta lähteestä: kovakoodattu
    ``"3/5"`` kelpaisi jonain päivänä skeemaan ja testi menisi läpi
    mittaamatta mitään.
    """
    allowed = classify_stage.roster_classes()
    foreign = f"vieras-{allowed[0]}"
    assert foreign not in allowed

    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(roster_class=foreign)])

    with pytest.raises(SchemaError) as err:
        run_classify(settings, parsed)

    message = str(err.value)
    assert foreign in message
    assert ", ".join(allowed) in message, "viesti luettelee saman joukon"
    assert MAP_DEMO_ID in message
    assert not parsed.classified(A, MAP_DEMO_ID).exists()


def test_a_non_boolean_league_flag_stops_the_run(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row(is_league="kylla")])

    with pytest.raises(SchemaError) as err:
        run_classify(settings, parsed)

    assert "is_league" in str(err.value)


def test_a_broken_selection_file_advises_running_select(settings, parsed) -> None:
    write_teams_index(parsed, {TEAM_KEY: [A]})
    path = parsed.selection(TEAM_KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ei ole jsonia", encoding="utf-8")

    with pytest.raises(PappascoutError) as err:
        run_classify(settings, parsed)

    assert "pappascout select" in str(err.value)
    assert not parsed.classified(A, MAP_DEMO_ID).exists()


def test_the_selection_file_is_not_a_manifest_input(settings, parsed) -> None:
    """Ohitettu ajo kantaa vanhaa arvoa, ja se on tarkoituksellista.

    Testi pinnaa kytkennän rajan: valintatiedoston ilmestyminen **ei**
    invalidoi valmista tulosta, ja ``--pakota`` on se tapa, jolla arvo
    päivittyy. Ilman tätä väitettä joku lisäisi tiedoston manifestin
    ``inputs``iin huomaamatta, että se pakottaisi koko arkiston
    uudelleenluokitteluun.
    """
    run_classify(settings, parsed)
    assert facts_of(parsed) == ([None], [None])

    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])

    skipped = run_classify(settings, parsed)
    assert skipped.skipped, "manifesti täsmää: valintatiedosto ei ole syöte"
    assert facts_of(parsed) == ([None], [None])

    forced = run_classify(settings, parsed, force=True)
    assert not forced.skipped
    assert facts_of(parsed) == ([True], ["5/5"])


def test_a_broken_selection_file_does_not_break_a_skipped_run(
    settings, parsed
) -> None:
    """Faktat luetaan **ohitushaaran jälkeen**, ja tämä pinnaa järjestyksen.

    Jos luku siirretään funktion alkuun "yhteen paikkaan", yksi korruptoitunut
    valintatiedosto muuttaisi koko arkiston valmiit luokittelut virheiksi --
    eikä yksikään muu testi kaatuisi, koska ne kaikki ajavat tuoreen ajon.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])
    first = run_classify(settings, parsed)
    assert not first.skipped

    parsed.selection(TEAM_KEY).write_text("ei ole jsonia", encoding="utf-8")

    result = run_classify(settings, parsed)

    assert result.skipped, "valmista tulosta ei lueta uudelleen eikä rikota"
    assert result.status == "ok"
    assert facts_of(parsed) == ([True], ["5/5"])


def test_a_newer_selection_file_warns_and_names_the_flag(
    settings, parsed
) -> None:
    """Vanhentuminen on havaittavissa eikä vain dokumentoitu.

    Valintatiedosto ei ole manifestin syöte, joten sen muuttuminen ei
    invalidoi tulosta -- eikä siis kerro itsestään. Ilman varoitusta taulu
    kantaisi vanhaa ``is_league``ia ja raportti näyttäisi ajan tasalla
    olevalta, ja ``--pakota`` jäisi ihmisen muistin varaan.
    """
    write_teams_index(parsed, {TEAM_KEY: [A]})
    write_selection(parsed, [selection_row()])
    run_classify(settings, parsed)

    # Uudempi kuin luokittelun manifesti: select on ajettu uudelleen.
    write_selection(
        parsed,
        [selection_row(is_league=False)],
        generated_at="2099-01-01T00:00:00+00:00",
    )

    result = run_classify(settings, parsed)

    assert result.skipped
    assert "--pakota" in (result.reason or "")
    assert classify_stage.SKIP_REASON in (result.reason or "")
    # Ja kun tiedosto on vanhempi, varoitusta ei ole.
    write_selection(parsed, [selection_row()])
    assert run_classify(settings, parsed).reason == classify_stage.SKIP_REASON


# --- Kierroslista Markdownina ----------------------------------------------------


def test_writes_a_readable_round_list_beside_the_table(settings, parsed) -> None:
    result = run_classify(settings, parsed)
    path = parsed.classified_round_list(A, MAP_DEMO_ID)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")

    assert MAP_DEMO_ID in text
    assert text.count("\n|") >= 6, "rivi jokaiselle kierrokselle"
    # Kynnykset ovat mukana, muuten lista ei kerro mitä vasten päätös tehtiin.
    # Tarkistus kohdistuu otsikon LAUSEESEEN, ei pelkkään lukuun: fikstuurin
    # rahasummat sisältävät samoja numeroita, joten irrallinen "1000" löytyisi
    # taulukon riveiltä vaikka otsikko olisi rikki.
    t = settings.thresholds
    header_line = next(r for r in text.splitlines() if r.startswith("- Kynnykset"))
    assert f"täysi osto vähintään {t.full_equip_min}" in header_line
    assert f"voiton jälkeen enintään {t.anomaly_equip_max_after_win}" in header_line
    assert f"ostettua vähintään {t.force_buy_min}" in header_line
    # Puolioston kaksi ehtoa ovat omalla rivillään: ne eivät ole
    # dollarikynnyksiä per pelaaja vaan pelaajalaskureita, ja yhteen lauseeseen
    # ahdettuna kumpikaan ei olisi luettavissa.
    half_line = next(r for r in text.splitlines() if r.startswith("- Puolioston"))
    assert f"vähintään {t.armed_players_min} pelaajaa aseistettuna" in half_line
    assert f"vähintään {t.normal_buy_players_min} pelaajaa" in half_line
    assert f"vähintään {t.normal_buy_money_min} $" in half_line
    # Häviöbonuksen portaat ovat mukana: ilman niitä ehdon B luku ei ole
    # tarkistettavissa, koska bonus ei näy missään muualla listassa.
    bonus_line = next(r for r in text.splitlines() if r.startswith("- Ehto B"))
    for step in settings.economy.loss_bonus_steps:
        assert str(step) in bonus_line
    assert str(settings.league.ot_start_money) in text
    # Poistuneita kynnyksiä ei mainita: otsikko kertoo vain sen, mitä
    # luokittelu oikeasti vertaili.
    for retired in (
        "eco_money_max",
        "eco_loss_count_min",
        "force_money_min",
        "force_money_max",
        "half_equip_min",
    ):
        assert retired not in text
    assert str(path.relative_to(parsed.root)).replace("\\", "/") in [
        str(o) for o in result.outputs
    ]


def test_round_list_is_listed_as_an_output_in_the_manifest(settings, parsed) -> None:
    run_classify(settings, parsed)
    manifest = Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID))
    assert any(o.endswith(".md") for o in manifest.outputs)
    assert any(o.endswith(".parquet") for o in manifest.outputs)


# --- Manifesti ja ohitus ---------------------------------------------------------


def test_manifest_has_no_tool_versions(settings, parsed) -> None:
    """Luokittelu on puhdasta domain-laskentaa: mikään kirjastoversio ei muuta sitä."""
    run_classify(settings, parsed)
    manifest = Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID))
    assert manifest.stage == "classify"
    assert manifest.tool_versions == {}
    assert manifest.inputs[0].result_id == f"parsed/{MAP_DEMO_ID}"


def test_second_run_is_skipped(settings, parsed) -> None:
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    before = path.stat().st_mtime_ns

    result = run_classify(settings, parsed)
    assert result.skipped
    assert path.stat().st_mtime_ns == before
    assert result.stats["rounds"] == 6
    assert result.stats["rows"], "kierroslista luetaan valmiista tuloksesta"


def test_force_overrides_a_matching_manifest(settings, parsed) -> None:
    run_classify(settings, parsed)
    assert not run_classify(settings, parsed, force=True).skipped


def test_a_stale_inputs_struct_is_recomputed_not_read(settings, parsed) -> None:
    """Vanha tulos, jonka ``inputs``-rakenne on eri muotoa, ajetaan uudelleen.

    ``inputs``-structin kentät muuttuivat kalibroinnissa 2026-08-29 ilman että
    manifestin skeemaversio muuttui, joten täsmäävä manifesti voi osoittaa
    vanhamuotoiseen tauluun. Sen on johdettava uudelleenlaskentaan -- ei
    kaatumiseen eikä hiljaiseen vanhan tuloksen palauttamiseen.
    """
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    assert run_classify(settings, parsed).skipped, "esiehto: manifesti täsmää"

    # Kirjoita taulu uudelleen vanhanmallisella inputs-rakenteella:
    # poistetut kynnykset takaisin, uudet pois. Molempien on oltava
    # oikeasti poistuneita tai oikeasti uusia -- elävän avaimen
    # poistaminen testaisi eri asiaa kuin mitä nimi lupaa.
    df = pl.read_parquet(path)
    old_inputs = []
    for i in df["inputs"].to_list():
        removed_in_this_story = (
            "money_players",
            "players_armed",
            "players_can_buy",
            "loss_bonus_if_lost",
            "armed_players_min",
            "normal_buy_money_min",
            "normal_buy_players_min",
        )
        row = {
            k: v for k, v in i.items() if k not in removed_in_this_story
        }
        row["eco_money_max"] = 2000
        row["force_money_min"] = 1500
        row["force_money_left_max"] = 1000
        old_inputs.append(row)
    df.with_columns(pl.Series("inputs", old_inputs)).write_parquet(path)

    result = run_classify(settings, parsed)
    assert not result.skipped, "vanhamuotoista tulosta ei saa palauttaa sellaisenaan"
    assert result.status == "ok"
    fields = set(pl.read_parquet(path)["inputs"].to_list()[0])
    assert "force_buy_min" in fields
    assert "eco_money_max" not in fields


def test_threshold_change_reruns_classify_but_not_parse(
    tmp_path: Path, archive
) -> None:
    """Hyväksymiskriteeri: kynnysmuutos ajaa luokittelun, ei parsintaa."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"full_equip_min = 4000": "full_equip_min = 4100"}
        ),
        encoding="utf-8",
    )
    a = load_settings(base_toml, env_files=())
    b = load_settings(changed_toml, env_files=())

    write_parse(archive, rounds_frame(match()), a.parse)
    parse_mtime_before = archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    run_classify(a, archive)
    result = run_classify(b, archive)

    assert not result.skipped, "kynnysmuutoksen jälkeen luokittelu ajetaan uudelleen"
    assert archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_mtime_before
    ), "parsintaa ei saa ajaa uudelleen"


def test_a_forced_reparse_with_the_same_result_does_not_rerun_classify(
    settings, parsed
) -> None:
    """Luokittelun syöte on parsinnan **tulos**, ei sen ajohetki.

    Ilman tätä jokainen ``parse --pakota`` pakottaisi myös uuden luokittelun,
    vaikka kierrostaulu olisi tavu tavulta sama.
    """
    run_classify(settings, parsed)
    write_parse(parsed, rounds_frame(match()), settings.parse, force=True)
    assert run_classify(settings, parsed).skipped


def test_a_changed_demo_forces_a_new_classification(settings, parsed) -> None:
    """Uusi parsinta uudesta demosta ei saa jäädä vanhan luokittelun taakse."""
    run_classify(settings, parsed)
    demo = parsed.import_dir() / f"{MAP_DEMO_ID}.dem"
    demo.write_bytes(b"PBDEMS2\x00" + b"y" * 4096)
    write_parse(parsed, rounds_frame(match()), settings.parse)

    assert not run_classify(settings, parsed).skipped


def test_missing_output_forces_a_rerun(settings, parsed) -> None:
    run_classify(settings, parsed)
    parsed.classified(A, MAP_DEMO_ID).unlink()
    assert not run_classify(settings, parsed).skipped


def test_unreadable_result_is_recomputed_not_reported(settings, parsed) -> None:
    """Luokittelu on halpaa: rikkinäinen tulos lasketaan uudelleen."""
    run_classify(settings, parsed)
    parsed.classified(A, MAP_DEMO_ID).write_bytes(b"ei parquetia")

    result = run_classify(settings, parsed)
    assert not result.skipped
    assert result.stats["rounds"] == 6
    assert pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).height == 6


def test_result_that_no_longer_matches_the_contract_is_recomputed(
    settings, parsed
) -> None:
    """Täsmäävä manifesti ei riitä, jos tulostaulun sopimus on muuttunut.

    Skeeman laajentuminen ei muuta manifestin sisältöä, joten vanha tulos
    näyttäisi ajantasaiselta mutta siitä puuttuisivat uudet arvot.
    """
    run_classify(settings, parsed)
    path = parsed.classified(A, MAP_DEMO_ID)
    pl.read_parquet(path).drop("loss_count").write_parquet(path)

    result = run_classify(settings, parsed)
    assert not result.skipped
    assert "loss_count" in pl.read_parquet(path).columns


# --- Joukkueen valinta -----------------------------------------------------------


def test_team_can_be_given_as_a_unique_prefix(settings, parsed) -> None:
    run_classify(settings, parsed, team=A[:6])
    assert parsed.classified(A, MAP_DEMO_ID).is_file()


def test_unknown_team_lists_both_lineups_of_the_demo(settings, parsed) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed, team="eitallaista")
    message = str(exc.value)
    assert A in message
    assert B in message
    assert "ei täsmää" in message


def test_missing_team_lists_both_lineups_too(settings, parsed) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed, team=None)
    message = str(exc.value)
    assert A in message and B in message
    assert "--team" in message


def test_ambiguous_prefix_is_refused(settings, archive) -> None:
    """Yhteinen alkuosa ei saa valita kokoonpanoa arpomalla."""
    frame = rounds_frame(match()).with_columns(
        pl.when(pl.col("lineup_key") == A)
        .then(pl.lit("yhteinen1"))
        .otherwise(pl.lit("yhteinen2"))
        .alias("lineup_key")
    )
    write_parse(archive, frame, settings.parse)
    with pytest.raises(PappascoutError, match="useampaan"):
        run_classify(settings, archive, team="yhteinen")


# --- Virheet ---------------------------------------------------------------------


def test_unparsed_demo_tells_which_command_to_run(settings, archive) -> None:
    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, archive)
    message = str(exc.value)
    assert "ei ole vielä parsittu" in message
    assert "pappascout parse" in message


def test_failed_parse_is_not_classified_over(settings, parsed) -> None:
    manifest = Manifest.read(parsed.parsed_manifest(MAP_DEMO_ID))
    broken = manifest.model_copy(
        update={"status": "parse_failed", "reason": "demo katkennut"}
    )
    broken.write(parsed.parsed_manifest(MAP_DEMO_ID))

    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed)
    assert "parse_failed" in str(exc.value)
    assert "demo katkennut" in str(exc.value)


def test_missing_parse_manifest_is_a_finnish_error(settings, parsed) -> None:
    parsed.parsed_manifest(MAP_DEMO_ID).unlink()
    with pytest.raises(PappascoutError, match="manifestia ei löytynyt"):
        run_classify(settings, parsed)


def test_rounds_table_that_breaks_the_contract_is_refused(
    settings, parsed
) -> None:
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).drop("survivors").write_parquet(path)
    with pytest.raises(SchemaError, match="survivors"):
        run_classify(settings, parsed)


def test_outdated_rounds_table_tells_the_user_to_reparse(
    settings, parsed
) -> None:
    """Vanha taulu on käyttäjän tilanne, ei kehittäjän.

    ``validate`` puhuu kehittäjälle: "Lisää sarake tai korjaa taulun
    tuottanut vaihe -- sopimus on tiedostossa domain/schemas.py." Se on
    väärä neuvo sille, joka ei koodaa itse: arkistossa oleva taulu on
    parsittu vanhemmalla versiolla, ja korjaus on ajaa parsinta uudelleen.

    Sarake on kalustolaskuri, koska se on tuorein ``ROUNDS``-laajennus ja
    siten se, jonka vuoksi tämä tilanne oikeasti syntyy.
    """
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).drop(ARMED_COLUMN).write_parquet(path)

    with pytest.raises(SchemaError) as exc:
        run_classify(settings, parsed)

    message = str(exc.value)
    # Diagnoosi säilyy: viesti nimeää sarakkeen, joka puuttuu.
    assert ARMED_COLUMN in message
    # Neuvo on käyttäjän tekemä toimenpide, ei koodimuutos.
    assert "parsittu ohjelman vanhemmalla versiolla" in message
    assert f"pappascout parse {MAP_DEMO_ID} --pakota" in message
    # Kehittäjän ohje ei saa vuotaa mukaan: se kehottaisi muokkaamaan koodia,
    # jota käyttäjä ei kirjoita.
    assert "domain/schemas.py" not in message
    assert "Lisää sarake" not in message


def test_a_round_without_an_anchor_does_not_break_the_run(
    settings, archive
) -> None:
    """I/O-matriisi: ankkuriton kierros jää luokittelematta, ajo jatkuu."""
    rounds = match()
    rounds[2] = round_rows(3, status="no_freeze_end")
    write_parse(archive, rounds_frame(rounds), settings.parse)

    result = run_classify(settings, archive)
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID)).sort("round_no")

    assert df.height == 6
    assert df["round_type"][2] is None
    assert "no_freeze_end" in df["reason"][2]
    assert df["round_type"].null_count() == 1
    assert result.stats["unclassified"] == 1


def test_short_handed_team_is_divided_by_the_observed_count(
    settings, archive
) -> None:
    """Vajaa joukkue: per pelaaja -arvo lasketaan oikealla määrällä."""
    total = 4 * settings.thresholds.full_equip_min
    rounds = match()
    rounds[3] = round_rows(4, a_won=False, a_equip=total, a_players=4)
    write_parse(archive, rounds_frame(rounds), settings.parse)

    run_classify(settings, archive)
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID)).sort("round_no")
    row = df.row(3, named=True)
    assert row["inputs"]["players"] == 4
    assert row["round_type"] == "full"




# --- Katselmuksen nostamat reunatapaukset ----------------------------------------


def test_unnumbered_rounds_are_dropped_and_counted(settings, archive) -> None:
    """Numeroimaton rivi kaataisi loss countin; se pudotetaan ja kerrotaan.

    Kierrostaulu kirjoitetaan tässä suoraan, koska ``parse`` ei itse päästä
    numeroimatonta riviä läpi -- mutta arkistossa voi olla vanhemmalla
    versiolla kirjoitettu taulu, eikä luokittelu saa kaatua siihen.
    """
    write_parse(archive, rounds_frame(match()), settings.parse)
    path = archive.parsed_table(MAP_DEMO_ID, "rounds")
    table = pl.read_parquet(path)
    unnumbered = table.head(2).with_columns(
        pl.lit(None, dtype=pl.Int32).alias("round_no"),
        pl.lit(99, dtype=pl.Int32).alias("round_raw"),
    )
    pl.concat([unnumbered, table]).write_parquet(path)

    result = run_classify(settings, archive)
    assert result.stats["unnumbered"] == 1
    assert result.stats["rounds"] == 6
    df = pl.read_parquet(archive.classified(A, MAP_DEMO_ID))
    assert df["round_no"].null_count() == 0


def test_skipped_run_gives_exactly_the_same_round_list(settings, parsed) -> None:
    """Yksi polku kierroslistalle: ohitus ei saa näyttää eri lukuja.

    Jos tuore ja ohitettu ajo rakentaisivat rivit eri tavalla, ``--show``
    näyttäisi toisella ajolla esimerkiksi vastustajan talouden subjektin
    kierroksilla -- eikä mikään kertoisi siitä.
    """
    fresh = run_classify(settings, parsed)
    skipped_run = run_classify(settings, parsed)

    assert skipped_run.skipped
    assert not fresh.skipped
    assert skipped_run.stats["rows"] == fresh.stats["rows"]
    assert skipped_run.stats["by_type"] == fresh.stats["by_type"]


def test_league_change_reruns_classify_but_not_parse(tmp_path: Path, archive) -> None:
    """``[league]`` on osa luokittelun parametrihashia siinä missä kynnyksetkin."""
    base_toml = tmp_path / "perus.toml"
    base_toml.write_text(settings_text(archive.root), encoding="utf-8")
    changed_toml = tmp_path / "muutettu.toml"
    changed_toml.write_text(
        settings_text(
            archive.root, **{"ot_start_money = 12500": "ot_start_money = 10000"}
        ),
        encoding="utf-8",
    )
    a = load_settings(base_toml, env_files=())
    b = load_settings(changed_toml, env_files=())

    write_parse(archive, rounds_frame(match()), a.parse)
    parse_mtime_before = archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns

    run_classify(a, archive)
    result = run_classify(b, archive)

    assert not result.skipped
    assert archive.parsed_table(MAP_DEMO_ID, "rounds").stat().st_mtime_ns == (
        parse_mtime_before
    )


def test_markdown_row_matches_the_parquet_row(settings, parsed) -> None:
    """Taulukon sisältö, ei vain sen muoto: tyyppi ja perustelu ovat samat."""
    run_classify(settings, parsed)
    text = parsed.classified_round_list(A, MAP_DEMO_ID).read_text(encoding="utf-8")
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID)).sort("round_no")
    expected = df.row(0, named=True)

    row = next(r for r in text.splitlines() if r.startswith("| 1 |"))
    cells = [s.strip() for s in row.strip("|").split("|")]
    headers = [o for o, _ in classify_stage.ROUND_LIST_COLUMNS]
    fields = dict(zip(headers, cells))

    assert fields["Tyyppi"] == str(expected["round_type"])
    assert fields["Vast."] == str(expected["opp_round_type"])
    assert fields["Loss"] == str(expected["loss_count"])
    assert fields["Puoli"] == str(expected["side"])
    # Perustelu on sama teksti, vain Markdown-suojaukset poistettuna.
    assert fields["Perustelu"].replace("\\", "") == str(expected["reason"]).replace(
        "\\", ""
    )
    # Ja per pelaaja -arvot vastaavat inputs-rakennetta.
    inputs = expected["inputs"]
    assert fields["Varusteet"] == str(
        per_player(inputs["equip_buy_end"], inputs["players"])
    )


def test_markdown_escapes_everything_that_would_break_the_table(settings) -> None:
    """Rivinvaihto rikkoisi taulukon ja backtick söisi loput rivistä."""
    rows = [
        {
            "round_no": 1,
            "side": "T",
            "won": True,
            "round_type": "eco",
            "opp_round_type": "full",
            "loss_count": 1,
            "money_per_player": 100,
            "money_available_per_player": 200,
            "spent_per_player": 100,
            "equip_per_player": 300,
            "players": 5,
            "reason": "Rivi\nvaihto | putki `backtick`.",
        }
    ]
    text = classify_stage.render_round_list_markdown(
        rows,
        map_demo_id=MAP_DEMO_ID,
        team_key=A,
        thresholds=settings.thresholds,
        league=settings.league,
        economy=settings.economy,
    )
    table_lines = [r for r in text.splitlines() if r.startswith("| 1 |")]
    assert len(table_lines) == 1, "rivinvaihto ei saa katkaista solua"
    row = table_lines[0]
    assert row.count("|") == len(classify_stage.ROUND_LIST_COLUMNS) + 1 + 1
    assert "\\`" in row
    assert "\\|" in row


def test_markdown_is_byte_identical_on_a_rerun(settings, parsed) -> None:
    """Ajohetki kuuluu manifestiin, ei tulosteeseen -- muuten erot eivät näy."""
    run_classify(settings, parsed)
    before = parsed.classified_round_list(A, MAP_DEMO_ID).read_bytes()
    run_classify(settings, parsed, force=True)
    assert parsed.classified_round_list(A, MAP_DEMO_ID).read_bytes() == before
    # Aikaleima on kuitenkin tallessa.
    assert Manifest.read(parsed.classified_manifest(A, MAP_DEMO_ID)).created_at


def test_rounds_table_of_another_demo_is_refused(settings, parsed) -> None:
    """Väärä parquet oikeassa polussa luokiteltaisiin väärän tunnisteen alle."""
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).with_columns(
        pl.lit("1-toinen-demo-1").alias("map_demo_id")
    ).write_parquet(path)

    with pytest.raises(PappascoutError) as exc:
        run_classify(settings, parsed)
    assert "toisen demon rivejä" in str(exc.value)
    assert "1-toinen-demo-1" in str(exc.value)


def test_three_lineups_are_refused_with_the_right_count(settings, archive) -> None:
    rounds = match(4)
    rounds[3][1]["lineup_key"] = "cccccccccccccccc"
    write_parse(archive, rounds_frame(rounds), settings.parse)

    with pytest.raises(SchemaError) as exc:
        run_classify(settings, archive)
    message = str(exc.value)
    assert "3 kokoonpanoa" in message
    assert "cccccccccccccccc" in message


def test_round_number_mismatch_between_teams_is_refused(settings, parsed) -> None:
    """Ilman tarkistusta vastustajan tyyppi liittyisi väärälle riville."""
    path = parsed.parsed_table(MAP_DEMO_ID, "rounds")
    pl.read_parquet(path).with_columns(
        pl.when((pl.col("lineup_key") == B) & (pl.col("round_no") == 6))
        .then(pl.lit(7, dtype=pl.Int32))
        .otherwise(pl.col("round_no"))
        .alias("round_no")
    ).write_parquet(path)

    with pytest.raises(SchemaError, match="eivät täsmää"):
        run_classify(settings, parsed)


def test_team_keys_lists_both_lineups(settings, parsed) -> None:
    assert classify_stage.team_keys(parsed, MAP_DEMO_ID) == sorted([A, B])


def test_inputs_carry_the_money_that_was_available(settings, parsed) -> None:
    """Story 1.4 tarvitsee käytettävissä olleen rahan, ei vain jäljelle jäänyttä."""
    run_classify(settings, parsed)
    df = pl.read_parquet(parsed.classified(A, MAP_DEMO_ID))
    for inputs in df["inputs"].to_list():
        assert inputs["money_spent"] == 20000
        assert inputs["money_buy_end"] + inputs["money_spent"] == 25000
        assert inputs["force_buy_min"] == settings.thresholds.force_buy_min
        assert (
            inputs["normal_buy_money_min"]
            == settings.thresholds.normal_buy_money_min
        )
        # Story 1.10: jakauma ja molempien ehtojen laskurit kulkevat mukana,
        # jotta kierroslistan rivi on tarkistettavissa ilman uutta ajoa.
        assert sum(inputs["money_players"]) == inputs["money_buy_end"]
        assert inputs["players_can_buy"] is not None
        assert inputs["loss_bonus_if_lost"] in settings.economy.loss_bonus_steps


# --- Oikeat demot ----------------------------------------------------------------


def real_rounds(demo_name: str, map_demo_id: str) -> pl.DataFrame:
    """Oikean demon kierrostaulu ``ROUNDS``-muodossa, ilman arkistoa."""
    from pappascout.adapters.demo_parser import Demoparser2Adapter

    # Yksi näytepiste riittää: tämä apuri käyttää vain kierrostaulua, ja
    # portti palauttaa molemmat samasta lukukerrasta. Poissulkulista **ja
    # ostoikkuna** ovat tuotannon, jotta adapteri ajetaan samoilla säännöillä
    # kuin oikeasti. Ilman ikkunaa adapterin oletus on 0,0 eli mittaus
    # ankkurilta, ja koko tämän tiedoston demopohjainen sarja varmentaisi
    # tuomioita luvuista, joita tuote ei enää tuota.
    parse_settings_real = load_settings(REAL_SETTINGS, env_files=()).parse
    adapter = Demoparser2Adapter(
        exclude_weapons=parse_settings_real.first_contact_exclude_weapons,
        fallback_death=parse_settings_real.first_contact_fallback_death,
        buy_window_seconds=parse_settings_real.buy_window_seconds,
    )
    tables = adapter.parse_demo(require_demo(demo_name), (6.0,))
    raw = mark_played_rounds(tables.rounds)
    df = raw.filter(pl.col("round_no").is_not_null()).select(
        pl.lit(map_demo_id, dtype=pl.Utf8).alias("map_demo_id"),
        *[pl.col(name) for name in ROUNDS if name != "map_demo_id"],
    )
    return validate(df.sort("round_no", "side"), ROUNDS, "rounds")


def subject_key(df: pl.DataFrame) -> str:
    """Kokoonpano, joka aloitti T-puolella.

    Molemmissa testidemoissa se on ``team_SSStttNNN``
    (``_bmad-output/implementation-artifacts/testiaineisto.md``). Nimeä ei voi
    lukea demosta -- kierrostaulussa on vain kokoonpanotiiviste -- joten
    subjekti tunnistetaan aloituspuolesta.
    """
    return str(
        df.filter((pl.col("round_no") == 1) & (pl.col("side") == "T"))["lineup_key"][0]
    )


@pytest.mark.demo
def test_ancient_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    """Regressio: todennettu jakso pistooli -> säästö -> täysi osto."""
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(ANCIENT_DEM, "ancient")
    df_, rows = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient", economy=economy, facts=NO_FACTS
    )
    assert df_.height == ANCIENT_ROUNDS
    assert df_.sort("round_no")["round_type"].to_list()[:3] == ["pistol", "eco", "full"]
    # Perustelu kertoo rahan ja loss countin jokaisella kierroksella.
    assert all("loss count" in str(r["reason"]) for r in rows)


@pytest.mark.demo
def test_ancient_has_no_unclassified_rounds(settings_file: Path) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(ANCIENT_DEM, "ancient")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "ancient", economy=economy, facts=NO_FACTS
    )
    assert result["round_type"].null_count() == 0


@pytest.mark.demo
def test_nuke_overtime_rounds_get_no_economy_reasoning(settings_file: Path) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(NUKE_ZST, "nuke")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "nuke", economy=economy, facts=NO_FACTS
    )

    assert result.height == NUKE_ROUNDS
    overtime = result.filter(pl.col("round_no") > thresholds.regulation_rounds)
    assert sorted(overtime["round_no"].to_list()) == [25, 26, 27, 28]
    assert set(overtime["round_type"].to_list()) == {"ot"}
    assert set(overtime["opp_round_type"].to_list()) == {"ot"}
    assert all("jatkoaikaa" in r for r in overtime["reason"].to_list())


@pytest.mark.demo
def test_nuke_first_three_rounds_are_pistol_eco_full(settings_file: Path) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(NUKE_ZST, "nuke")
    result, _ = classify_stage.classify_rounds(
        df, subject_key(df), thresholds, "nuke", economy=economy, facts=NO_FACTS
    )
    assert result.sort("round_no")["round_type"].to_list()[:3] == [
        "pistol",
        "eco",
        "full",
    ]


@pytest.mark.demo
@pytest.mark.parametrize(
    "demo_name,identifier", [(ANCIENT_DEM, "ancient"), (NUKE_ZST, "nuke")]
)
def test_opponent_type_matches_the_other_teams_own_type(
    settings_file: Path, demo_name: str, identifier: str
) -> None:
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(demo_name, identifier)
    a = subject_key(df)
    b = next(k for k in df["lineup_key"].unique().to_list() if k != a)

    own, _ = classify_stage.classify_rounds(
        df, a, thresholds, identifier, economy=economy, facts=NO_FACTS
    )
    other, _ = classify_stage.classify_rounds(
        df, b, thresholds, identifier, economy=economy, facts=NO_FACTS
    )

    assert own.sort("round_no")["round_type"].to_list() == (
        other.sort("round_no")["opp_round_type"].to_list()
    )


# --- Kalibrointi oikealla demolla (Story 1.9) ---------------------------------


@pytest.mark.demo
def test_ancient_calibration_verdicts_hold_on_the_real_demo(
    settings_file: Path,
) -> None:
    """Kalibroinnin 15 tuomiota **oikeasta demosta**, ei käsin rakennetusta rivistä.

    ``test_calibration.py`` pinnaa säännön: annetuilla luvuilla se antaa
    Veetin tuomion. Se ei voi todeta, että demosta luetaan **ne luvut** --
    taulun rivit ovat siellä syöte, ja jos mittaus ajautuu erilleen, sääntö
    menee yhä läpi omilla luvuillaan.

    Tämä sulkee ketjun toisesta päästä: demo parsitaan tuotannon asetuksilla,
    luokitellaan tuotannon kynnyksillä, ja jokaisen 15 rivin tuomiota
    verrataan Veetin antamaan. Aiempi varmistus,
    :func:`test_ancient_has_no_unclassified_rounds`, tyytyy siihen ettei arvo
    ole tyhjä -- minkä ``anomaly`` ja mikä tahansa väärä tuomio täyttää, eikä
    se kata kolmeatoista näistä viidestätoista rivistä lainkaan.

    **Luvut tarkistetaan tuomion lisäksi**, koska tuomio kestää yllättävän
    suuria muutoksia: kierros 21 T on eco sekä 710 että 750 dollarilla, joten
    pelkkä tuomio ei huomaisi mittauspisteen liukumista.
    """
    loaded = load_settings(settings_file, env_files=())
    thresholds, economy = loaded.thresholds, loaded.economy
    df = real_rounds(ANCIENT_DEM, "ancient")

    observed: dict[tuple[int, str], dict] = {}
    for team in df["lineup_key"].unique().to_list():
        result, _ = classify_stage.classify_rounds(
            df, team, thresholds, "ancient", economy=economy, facts=NO_FACTS
        )
        for row in result.iter_rows(named=True):
            observed[(int(row["round_no"]), str(row["side"]))] = row

    for k in TRUTH_TABLE:
        row = observed.get((k.round_no, k.side))
        assert row is not None, f"kierros {k.round_no} {k.side} puuttuu demosta"

        assert row["round_type"] == k.truth, (
            f"Kierros {k.round_no} {k.side}: Veeti sanoo {k.truth!r} "
            f"({k.basis}), demosta luokiteltuna {row['round_type']!r}. "
            f"Perustelu: {row['reason']}"
        )

        inputs = row["inputs"]
        players = int(inputs["players"])
        left = per_player(inputs["money_buy_end"], players)
        equip = per_player(inputs["equip_buy_end"], players)
        bought = per_player(
            inputs["equip_buy_end"] - inputs["equip_round_start"], players
        )
        assert (left, bought, equip) == (k.left, k.bought, k.equip), (
            f"Kierros {k.round_no} {k.side}: totuustaulun luvut ovat "
            f"{(k.left, k.bought, k.equip)}, demo antaa "
            f"{(left, bought, equip)} (jäljellä / ostettu / varusteet, "
            "$/pelaaja). Päivitä taulun luvut ja muutosloki -- tuomioon ei "
            "kosketa."
        )


@pytest.mark.demo
def test_the_calibration_demo_is_measured_from_the_buy_window(
    settings_file: Path,
) -> None:
    """Kalibrointidemo mitataan ostoajan lopusta, ei ankkurista.

    Edellinen testi menisi läpi myös silloin, jos sekä mittaus että
    totuustaulu palautuisivat yhtä matkaa ankkuriin -- kaksi virhettä, jotka
    kumoavat toisensa. Tämä toteaa mittauspisteen suoraan: jokaisella
    kierroksella on ``buy_end_tick``, ja niistä ainakin yksi on ankkurin
    jäljessä.
    """
    df = real_rounds(ANCIENT_DEM, "ancient")

    assert df["buy_end_tick"].null_count() == 0
    later = df.filter(pl.col("buy_end_tick") > pl.col("freeze_end_tick"))
    assert later.height > 0, (
        "yhdelläkään kierroksella mittauspiste ei ole ankkurin jäljessä -- "
        "real_rounds ajaa todennäköisesti ilman tuotannon ostoikkunaa"
    )
