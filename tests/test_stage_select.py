"""``stages.select`` -- vaiheen testit (Story 3.3).

**Ei verkkoa.** Vaihe ei ota porttia lainkaan: se lukee ``discover``in
kirjoittamat indeksit ja arkiston kokoonpanotaulut. Indeksit kirjoitetaan tässä
oikealla ``discover``illa feikkiportin takaa, jotta testit lukevat täsmälleen
sitä muotoa, jonka ohjelma oikeastikin kirjoittaa -- käsin väsätty
``matches.json`` läpäisisi testin ja hajoaisi ajossa.

Yhteenveto renderöidään **oikean ajon tuloksesta** (``_render_select``), ei
käsin kirjoitetusta sanakirjasta: muuten vaiheen ja tulosteen väliltä voisi
kadota avain ilman että yksikään testi huomaa, ja hylkäysten syyt tulostuisivat
tyhjänä lohkona.

I/O-matriisin kymmenen tapausta ovat tässä tiedostossa, ja jokainen on nimetty
niin, että sen rivin tunnistaa spesifikaatiosta.

Aineisto on divisioonan oikea muoto (``test_stage_discover.division_matches``):
12 joukkuetta, 66 ottelua, kuusi pelattua. Subjekti on ``PotkukelkkaPeek``,
koska se on divisioonan kolmas nimi ja osuu pelattuihin **tasan kerran** -- eli
sillä on yksi pelattu ottelu yhdestätoista, sama suhde kuin Rcave Veteransilla
oikeassa aineistossa (mitattu 2026-09-04), ja siksi sama odotus: **kaksi
MapDemoa**. Rcave Veterans on tässä aineistossa se joukkue, jolla ei ole
yhtäkään pelattua ottelua -- ja sekin on tapaus, jonka on käyttäydyttävä oikein.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from conftest import has_temp_leftovers
from test_stage_discover import (
    CHAMPIONSHIP,
    FakeSource,
    division_matches,
    write_lineups,
)
from test_teams import DIVISION, members, steam_id

from pappascout.archive.paths import ArchivePaths, parsed_table
from pappascout.cli import _render_select
from pappascout.domain.models import LeagueSettings, ThresholdSettings
from pappascout.domain.schemas import LINEUPS
from pappascout.errors import PappascoutError
from pappascout.stages import discover as discover_stage
from pappascout.stages import select as select_stage

#: Divisioonan järjestys ratkaisee, kuka osuu kuuteen pelattuun otteluun.
TEAM_INDEX = {name: index for index, name in enumerate(DIVISION)}

#: Subjekti: divisioonan kolmas nimi, ja siksi **tasan yksi pelattu ottelu**.
SUBJECT = "PotkukelkkaPeek"

#: Subjektin ainoa pelattu ottelu: pari (popsiCS, PotkukelkkaPeek) on
#: aineiston toinen ottelu, ja kuusi ensimmäistä on pelattu.
PLAYED_MATCH = "1-match-01"

#: Lainanantaja ulkopuolisille pelaajille. Sen on oltava joukkue, joka pelaa
#: vielä :data:`PLAYED_MATCH`in jälkeen -- muuten se ei havaitsisi omia
#: pelaajiaan viimeksi eikä siirtymäsääntö poistaisi heitä subjektin
#: vakirosterista. Takakeno on divisioonan kuudes nimi, joten sen ottelut
#: jatkuvat pitkälle lainauksen jälkeen.
LENDER = "Takakeno"

SUBJECT_FACTION = f"faction-{TEAM_INDEX[SUBJECT]:02d}"


@pytest.fixture
def league() -> LeagueSettings:
    return LeagueSettings(
        season=13,
        organizer_id="1bfc69fa-5a21-4ed9-9ef3-37edbd7210d8",
        championship_ids=[CHAMPIONSHIP],
        map_pool=["de_ancient", "de_nuke"],
    )


@pytest.fixture
def thresholds() -> ThresholdSettings:
    """``[thresholds]`` oletuksillaan: ``roster_size`` 5, ``roster_min_regulars`` 4."""
    return ThresholdSettings(pistol_rounds=[1, 13])


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


def index(
    archive: ArchivePaths,
    league: LeagueSettings,
    thresholds: ThresholdSettings,
    matches=None,
) -> None:
    """Kirjoita indeksit oikealla ``discover``illa, feikkiportin takaa."""
    source = FakeSource(
        {CHAMPIONSHIP: division_matches() if matches is None else matches}
    )
    discover_stage.run(league, archive, None, source=source, thresholds=thresholds)


def select(
    league: LeagueSettings,
    archive: ArchivePaths,
    thresholds: ThresholdSettings,
    team: str = "Potku",
):
    return select_stage.run(league, archive, team, thresholds=thresholds)


def read_selection(archive: ArchivePaths, team_key: str) -> dict[str, Any]:
    return select_stage.read_selection(archive, team_key)


def subject_key(archive: ArchivePaths) -> str:
    document = json.loads(
        (archive.root / "index" / "teams.json").read_text(encoding="utf-8")
    )
    return next(
        team["team_key"] for team in document["teams"] if team["name"] == SUBJECT
    )


def played_match_of(archive: ArchivePaths, team_name: str) -> str:
    document = json.loads(
        (archive.root / "index" / "teams.json").read_text(encoding="utf-8")
    )
    row = next(team for team in document["teams"] if team["name"] == team_name)
    return row["played_match_ids"][0]


def team_roster_in_match(
    archive: ArchivePaths, match_id: str, faction_ids: set[str]
) -> list[str]:
    """Ottelun oman osapuolen rosteri otteluindeksistä."""
    document = json.loads(
        (archive.root / "index" / "matches.json").read_text(encoding="utf-8")
    )
    row = next(m for m in document["matches"] if m["match_id"] == match_id)
    side = next(s for s in row["teams"] if s["faction_id"] in faction_ids)
    return list(side["roster"])


def rows_by_index(archive: ArchivePaths) -> dict[int, dict[str, Any]]:
    document = read_selection(archive, subject_key(archive))
    return {row["map_index"]: row for row in document["selections"]}


# -- Rivi syntyy vain pelatuista otteluista ---------------------------------


def test_only_played_matches_produce_rows(league, archive, thresholds) -> None:
    """Hyväksymiskriteeri: 1 pelattu ottelu -> 2 MapDemoa.

    Subjektilla on 11 ottelua joista yksi on pelattu -- sama suhde kuin
    Rcave Veteransilla oikeassa aineistossa.
    """
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.stats["map_demos"] == 2
    assert result.stats["matches_with_maps"] == 1
    assert result.stats["matches_seen"] == 11
    document = read_selection(archive, subject_key(archive))
    assert len(document["selections"]) == 2
    assert {row["map_index"] for row in document["selections"]} == {0, 1}


def test_a_scheduled_match_produces_no_row_at_all(league, archive, thresholds) -> None:
    """I/O-matriisi: pelaamaton ottelu, ``map_picks`` tyhjä -> ei riviä lainkaan.

    Mitattu 2026-09-04: ``map_picks`` on tyhjä 60/66 ottelussa. Ajastettu ottelu
    ei ole "valinta odottaa" vaan "ei vielä olemassa".
    """
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert len({row["match_id"] for row in document["selections"]}) == 1
    assert result.stats["matches_not_played"] == 10
    assert result.stats["matches_without_veto"] == 0


def test_the_match_counters_add_up_to_every_match_the_team_has(
    league, archive, thresholds
) -> None:
    """Ottelu ei saa kadota laskureiden välistä.

    ``matches_with_maps + matches_not_played + matches_without_veto ==
    matches_seen``. Ilman tätä yhtälöä pudotettu ottelu jäisi selittämättä --
    ja juuri se on syy, miksi ohitukset lasketaan syy kerrallaan.
    """
    index(archive, league, thresholds)

    stats = select(league, archive, thresholds).stats

    assert (
        stats["matches_with_maps"]
        + stats["matches_not_played"]
        + stats["matches_without_veto"]
        == stats["matches_seen"]
    )


def test_a_played_match_without_veto_is_not_called_unplayed(
    league, archive, thresholds
) -> None:
    """Portin sopimus: tyhjä ``map_picks`` on "ei vetotietoa", ei "ei karttoja".

    Aiempi versio niputti tämän pelaamattoman ottelun kanssa ja tulosti syyksi
    *"koska niitä ei ole pelattu"* -- väite, joka ei ole tosi pelatusta
    ottelusta. Kartat pelattiin; emme tiedä mitkä.
    """
    matches = tuple(
        replace(match, map_picks=()) if match.match_id == PLAYED_MATCH else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    assert result.stats["map_demos"] == 0
    assert result.stats["matches_without_veto"] == 1
    assert result.stats["matches_not_played"] == 10
    notes = " ".join(result.stats["notes"])
    assert "vetotieto puuttuu" in notes
    assert PLAYED_MATCH in notes
    # Pelaamattomat ottelut ovat oma huomionsa eivätkä katoa toisen alle.
    assert "vielä pelaamatta" in notes


def test_the_unit_identifier_is_match_and_zero_based_map_index(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)
    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    match_id = document["selections"][0]["match_id"]
    assert [row["map_demo_id"] for row in document["selections"]] == [
        f"{match_id}-0",
        f"{match_id}-1",
    ]


def test_a_team_with_no_played_matches_gets_an_empty_file_with_a_reason(
    league, archive, thresholds
) -> None:
    """Tyhjä tulos ei ole virhe -- mutta se ei jää selittämättä."""
    index(archive, league, thresholds)

    result = select(league, archive, thresholds, team="Rcave")

    assert result.status == "ok"
    assert result.stats["map_demos"] == 0
    assert result.reason is not None
    assert "yhtäkään ei ole pelattu" in result.reason


# -- Jokaisella rivillä on neljä kenttää ja aina syy -------------------------


def test_every_row_has_the_four_fields_and_never_a_silent_rejection(
    league, archive, thresholds
) -> None:
    """Hyväksymiskriteeri: jokaisella rivillä ``roster_ok``, ``roster_reason``,
    ``roster_class`` ja ``is_league`` -- eikä yksikään hylkäys ole ilman syytä."""
    index(archive, league, thresholds)
    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    for row in document["selections"]:
        assert set(row) >= {"roster_ok", "roster_reason", "roster_class", "is_league"}
        assert row["roster_reason"].strip()
        if not row["roster_ok"]:
            assert row["roster_class"] is None


def test_a_rejected_row_states_the_numbers_and_the_threshold(
    league, archive, thresholds
) -> None:
    """I/O-matriisi: 3 vakipelaajaa -> ei kelpaa, syy kertoo montako ja mikä kynnys.

    Pelatun ottelun aloittajista kaksi on lainassa toiselta joukkueelta; ks.
    :func:`borrow` siitä, miksi ulkopuolinen on juuri toisen joukkueen pelaaja
    eikä keksitty tunniste.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=3)
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert result.stats["accepted"] == 0
    assert result.stats["rejected"] == 2
    for row in document["selections"]:
        assert row["roster_ok"] is False
        assert "3/5" in row["roster_reason"]
        assert "4/5" in row["roster_reason"]


def test_four_regulars_and_one_outsider_is_accepted_and_the_reason_names_them(
    league, archive, thresholds
) -> None:
    """I/O-matriisi: 4 vakipelaajaa + 1 ulkopuolinen -> kelpaa, luokka 4/5.

    Veeti 2026-09-04: ottelu on samaa joukkuetta vastaan vaikka toisessa
    ottelussa heillä olisi yksi substitution pelaaja. Ulkopuolinen **lasketaan
    mukaan**; ero on luokassa.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=4)
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert result.stats["accepted"] == 2
    assert result.stats["class_4/5"] == 2
    for row in document["selections"]:
        assert row["roster_class"] == "4/5"
        assert len(row["outsiders"]) == 1
        assert "ulkopuolelta" in row["roster_reason"]


def test_the_reason_names_the_outsider_by_nickname_end_to_end(
    league, archive, thresholds
) -> None:
    """Nimikartta kulkee vaiheelta domainille asti.

    Ilman tätä väitettä ``names``-parametrin poistaminen ei kaataisi mitään:
    jokainen syy nimeäisi ulkopuolisen 17-numeroisella tunnisteella, ja koko
    "luettava syy" -lupaus katoaisi huomaamatta.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=4)
    index(archive, league, thresholds, matches=matches)

    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    for row in document["selections"]:
        outsider = row["outsiders"][0]
        assert outsider not in row["roster_reason"], "tunniste nimen sijaan"
        # Lainanantajan nimimerkit ovat muotoa "takakeno1".
        assert "takakeno" in row["roster_reason"]


def test_a_full_regular_lineup_is_the_full_class(league, archive, thresholds) -> None:
    """I/O-matriisi: pelattu liigaottelu, 5 vakipelaajaa -> 2 riviä, 5/5."""
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.stats["class_5/5"] == 2
    assert result.stats["accepted"] == 2


# -- is_league päätellään tunnisteesta, ei nimestä ---------------------------


def test_is_league_is_true_for_matches_in_the_configured_championship(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)
    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert all(row["is_league"] for row in document["selections"])


def test_a_match_outside_the_league_still_gets_a_row_but_is_not_league(
    archive, thresholds
) -> None:
    """I/O-matriisi: ``competition_id`` ei listalla -> rivi syntyy, ``is_league``
    epätosi.

    Otanta tulee liigan ulkopuolelta -- se on koko epicin ydin -- joten
    rivin **on** synnyttävä. Ero on ``is_league``issa, ei siinä kuka on mukana.
    """
    outside = "muu-kilpailu-00000000"
    matches = tuple(
        replace(match, competition_id=outside) for match in division_matches()
    )
    fetching = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[outside],
        map_pool=["de_ancient"],
    )
    configured = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP],
        map_pool=["de_ancient"],
    )
    source = FakeSource({outside: matches})
    discover_stage.run(fetching, archive, None, source=source, thresholds=thresholds)

    result = select_stage.run(configured, archive, "Potku", thresholds=thresholds)

    document = read_selection(archive, subject_key(archive))
    assert len(document["selections"]) == 2
    assert not any(row["is_league"] for row in document["selections"])
    assert result.stats["league"] == 0


def test_is_league_is_not_read_from_the_competition_name(archive, thresholds) -> None:
    """Nimi on ihmisen kirjoittama merkkijono; päätös on tunnisteesta."""
    misleading = "6-divisioona-vaara-tunniste"
    matches = tuple(
        replace(match, competition_id=misleading) for match in division_matches()
    )
    fetching = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[misleading],
        map_pool=["de_ancient"],
    )
    configured = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP],
        map_pool=["de_ancient"],
    )
    discover_stage.run(
        fetching,
        archive,
        None,
        source=FakeSource({misleading: matches}),
        thresholds=thresholds,
    )

    select_stage.run(configured, archive, "Potku", thresholds=thresholds)

    document = read_selection(archive, subject_key(archive))
    assert not any(row["is_league"] for row in document["selections"])


# -- Vetotiedon kartta ei ole todiste pelatusta kartasta ---------------------


def test_a_third_map_in_a_best_of_three_is_not_counted_into_the_sample(
    league, archive, thresholds
) -> None:
    """Veeti vahvisti 4.9.: playoffit ovat BO3, joten tämä ei ole teoreettinen.

    2-0 päättyneessä BO3:ssa vedossa on kolme karttaa mutta demoja kaksi.
    Kolmas rivi syntyy -- se ei katoa hiljaa -- mutta se ei pääse otantaan
    ennen kuin demo todistaa kartan pelatuksi.
    """
    matches = tuple(
        replace(match, best_of=3, map_picks=("de_ancient", "de_nuke", "de_dust2"))
        if match.match_id == PLAYED_MATCH
        else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert len(rows) == 3
    assert result.stats["accepted"] == 2
    assert result.stats["uncertain"] == 1
    assert rows[2]["roster_ok"] is False
    assert rows[2]["certainly_played"] is False
    assert "ottelun pituus" in rows[2]["roster_reason"]
    assert rows[0]["roster_ok"] is True
    assert rows[1]["roster_ok"] is True


def test_a_parsed_demo_proves_the_third_map_was_played(
    league, archive, thresholds
) -> None:
    """Demoa ei ole olemassa kartasta, jota ei pelattu."""
    matches = tuple(
        replace(match, best_of=3, map_picks=("de_ancient", "de_nuke", "de_dust2"))
        if match.match_id == PLAYED_MATCH
        else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(archive, f"{PLAYED_MATCH}-2", {"kokoonpano": roster})

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[2]["roster_ok"] is True
    assert rows[2]["roster_source"] == "observed"
    assert result.stats["uncertain"] == 0


def test_every_map_of_a_best_of_two_is_certain(league, archive, thresholds) -> None:
    """BO2:ssa ei voi voittaa kahta ennen kuin molemmat on pelattu.

    Nykyinen runkosarja on mitattu BO2:ksi, joten epävarmuussääntö ei saa
    pudottaa yhtään riviä siitä otannasta.
    """
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.stats["uncertain"] == 0
    assert result.stats["accepted"] == 2


# -- Ennuste ja havainto -----------------------------------------------------


def test_an_unparsed_map_is_a_prediction_from_the_match_roster(
    league, archive, thresholds
) -> None:
    """I/O-matriisi: demoa ei ole parsittu -> luokka on ennuste, lähde sanoo sen."""
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert result.stats["predicted"] == 2
    assert result.stats["observed"] == 0
    for row in document["selections"]:
        assert row["roster_source"] == "predicted"
        assert "ennuste" in row["roster_reason"]


def test_a_parsed_map_is_an_observation_from_the_demo(
    league, archive, thresholds
) -> None:
    """I/O-matriisi: ``lineups.parquet`` olemassa -> luokka on havainto."""
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(
        archive,
        f"{PLAYED_MATCH}-0",
        {
            "ff03fb54599d3311": roster,
            "9ac92660986558d3": [steam_id(9000 + n) for n in range(5)],
        },
    )

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "observed"
    assert "havainto" in rows[0]["roster_reason"]
    assert rows[1]["roster_source"] == "predicted"
    assert result.stats["observed"] == 1
    assert result.stats["predicted"] == 1


def test_the_observation_wins_and_the_difference_is_told(
    league, archive, thresholds
) -> None:
    """I/O-matriisi: parsittu kokoonpano eroaa ottelurosterista -> havainto voittaa.

    Demossa on neljä ottelurosterin pelaajaa ja yksi ulkopuolinen: vaihto
    karttojen välissä. Ennuste olisi ollut 5/5, havainto on 4/5.
    """
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    outsider = steam_id(9999)
    write_lineups(
        archive, f"{PLAYED_MATCH}-1", {"ff03fb54599d3311": roster[:4] + [outsider]}
    )

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_class"] == "5/5"
    assert rows[1]["roster_class"] == "4/5"
    assert rows[1]["joined"] == [outsider]
    assert rows[1]["left"] == [roster[4]]
    assert "eroaa ottelurosterista" in rows[1]["roster_reason"]
    assert result.stats["drifted"] == 1


def test_a_short_lineup_is_accepted_but_the_reason_admits_the_size(
    league, archive, thresholds
) -> None:
    """Neljän pelaajan kokoonpano ei ole 4/5 ulkopuolisen takia.

    Luokka on ``4/5``, mutta ulkopuolista ei ole -- ja rivi sanoo molemmat,
    jottei lukija päättele luokasta vierasta pelaajaa.
    """
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(archive, f"{PLAYED_MATCH}-0", {"vajaa": roster[:4]})

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_ok"] is True
    assert rows[0]["roster_class"] == "4/5"
    assert rows[0]["outsiders"] == []
    assert rows[0]["players_seen"] == 4
    assert "Ulkopuolisia ei ollut" in rows[0]["roster_reason"]


def test_a_long_lineup_is_the_full_class_and_the_reason_admits_the_size(
    league, archive, thresholds
) -> None:
    """Kuuden pelaajan kokoonpano (uudelleenyhdistyminen) ei väitä "6/6"-luokkaa."""
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    extra = json.loads(
        (archive.root / "index" / "teams.json").read_text(encoding="utf-8")
    )
    subject = next(t for t in extra["teams"] if t["name"] == SUBJECT)
    sixth = next(
        p["game_player_id"]
        for p in subject["roster"]
        if p["game_player_id"] not in roster
    )
    write_lineups(archive, f"{PLAYED_MATCH}-0", {"pitka": roster + [sixth]})

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_class"] == "5/5"
    assert rows[0]["players_seen"] == 6
    assert "odotetun 5 sijaan" in rows[0]["roster_reason"]


def test_a_demo_without_this_team_does_not_become_a_false_observation(
    league, archive, thresholds
) -> None:
    """Nolla yhteistä pelaajaa ei ole havainto tästä joukkueesta.

    Rivi jää ennusteeksi **ja sanoo miksi** -- hiljainen alennus näyttäisi
    tavalliselta ennusteelta, vaikka demo on olemassa.
    """
    index(archive, league, thresholds)
    write_lineups(
        archive, f"{PLAYED_MATCH}-0", {"toinen": [steam_id(9000 + n) for n in range(5)]}
    )

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "predicted"
    assert "yhdessäkään sen kokoonpanossa" in rows[0]["roster_reason"]


def test_a_tie_between_two_lineups_is_not_resolved_by_guessing(
    league, archive, thresholds
) -> None:
    """Arpominen liittäisi vastustajan kokoonpanon tähän joukkueeseen."""
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_lineups(
        archive,
        f"{PLAYED_MATCH}-0",
        {
            "a": roster[:2] + [steam_id(9100), steam_id(9101), steam_id(9102)],
            "b": roster[2:4] + [steam_id(9200), steam_id(9201), steam_id(9202)],
        },
    )

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "predicted"
    assert "yhtä lähellä" in rows[0]["roster_reason"]


def test_an_unreadable_lineup_table_is_told_not_swallowed(
    league, archive, thresholds
) -> None:
    """Rikkinäinen taulu ei saa demota havaintoa ennusteeksi jäljettömästi.

    Ilman huomautusta rivi näyttäisi tavalliselta ennusteelta, eikä mikään
    kertoisi, että demo on arkistossa ja rikki. Vastakohta sille, miten
    ``teams_from_index`` kieltäytyy äänekkäästi -- mutta tässä ajoa **ei**
    kaadeta: yhden demon vika ei estä muiden valintaa (AD-9).
    """
    index(archive, league, thresholds)
    path = archive.resolve(parsed_table(f"{PLAYED_MATCH}-0", "lineups"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"tama ei ole parquet-tiedosto")

    result = select(league, archive, thresholds)

    rows = rows_by_index(archive)
    assert rows[0]["roster_source"] == "predicted"
    assert "muttei luettavissa" in rows[0]["roster_reason"]
    assert "pappascout parse" in rows[0]["roster_reason"]
    # Toinen kartta on ehjä, ja ajo jatkui.
    assert result.stats["map_demos"] == 2


def test_null_identifiers_in_the_lineup_table_do_not_merge_lineups(
    league, archive, thresholds
) -> None:
    """``str(None)`` sulauttaisi eri kokoonpanot ryhmäksi "None".

    Tuloksena olisi kokoonpano, jota yhdessäkään demossa ei ollut -- ja se
    voisi ylittää kynnyksen.
    """
    index(archive, league, thresholds)
    roster = team_roster_in_match(archive, PLAYED_MATCH, {subject_key(archive)})
    write_raw_lineups(
        archive,
        f"{PLAYED_MATCH}-0",
        [(None, player) for player in roster[:3]]
        + [(None, steam_id(9300)), ("oikea", roster[0])],
    )

    select(league, archive, thresholds)

    rows = rows_by_index(archive)
    # Ainoa kelvollinen ryhmä on "oikea", jossa on yksi pelaaja.
    assert rows[0]["roster_source"] == "observed"
    assert rows[0]["players_seen"] == 1
    assert rows[0]["roster_ok"] is False


# -- Osapuoli tunnistetaan kaikilla tunnisteilla ----------------------------


def test_the_side_is_matched_by_any_of_the_teams_identifiers() -> None:
    """Joukkueella voi olla monta lähdetunnistetta (uusi kausi tuo uuden).

    Kanoninen ``team_key`` on niistä yksi, eikä ottelurivi välttämättä kanna
    juuri sitä.
    """
    own = frozenset({"team-key-vanha", "faction-uusi"})
    match = discover_stage.IndexedMatch(
        match_id="1-x",
        teams=(
            discover_stage.IndexedMatchTeam(faction_id="joku-muu"),
            discover_stage.IndexedMatchTeam(faction_id="faction-uusi", name="Me"),
        ),
    )

    side = select_stage._own_side(match, own)

    assert side is not None
    assert side.name == "Me"
    assert select_stage._own_side(match, frozenset({"ei-mikaan"})) is None


# -- Virheet ovat suomeksi ja kertovat mitä tehdä ---------------------------


def test_an_unknown_team_lists_the_known_ones(league, archive, thresholds) -> None:
    """I/O-matriisi: tuntematon ``team_key`` -> suomenkielinen virhe, joka listaa."""
    index(archive, league, thresholds)

    with pytest.raises(PappascoutError) as excinfo:
        select(league, archive, thresholds, team="Ei olemassa")

    message = str(excinfo.value)
    assert "Rcave Veterans" in message
    assert SUBJECT in message


def test_an_ambiguous_name_asks_instead_of_choosing(league, archive, thresholds) -> None:
    """Etuliite ``T`` osuu kolmeen; valintaa ei tehdä hiljaa."""
    index(archive, league, thresholds)

    with pytest.raises(PappascoutError) as excinfo:
        select(league, archive, thresholds, team="T")

    message = str(excinfo.value)
    assert "TUUHEE" in message
    assert "Takakeno" in message
    assert "Tankkiluola vilttiketju" in message


def test_a_missing_index_tells_the_user_to_run_discover(
    league, archive, thresholds
) -> None:
    """I/O-matriisi: ``index/``iä ei ole ajettu -> virhe kehottaa ajamaan discoverin."""
    with pytest.raises(PappascoutError) as excinfo:
        select(league, archive, thresholds)

    assert "pappascout discover" in str(excinfo.value)


def test_indexes_from_different_runs_are_refused(league, archive, thresholds) -> None:
    """Lukija on ``discover``in, ja se kieltäytyy yhdistämästä eri ajoja."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "teams.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["generated_at"] = "1999-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="eri ajoista"):
        select(league, archive, thresholds)


def test_a_duplicated_match_is_refused_instead_of_doubling_the_sample(
    league, archive, thresholds
) -> None:
    """Kaksi riviä samasta ottelusta tuottaisi jokaisen kartan otantaan kahdesti."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "matches.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["matches"].append(dict(document["matches"][1]))
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="kahdesti"):
        select(league, archive, thresholds)


def test_a_malformed_match_row_is_refused_not_dropped(
    league, archive, thresholds
) -> None:
    """Ohitettu ottelu lyhentäisi otantaa ilman että mikään selittää eron."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "matches.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["matches"][0]["map_picks"] = "de_ancient"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="ei ole luettelo"):
        select(league, archive, thresholds)


def test_a_malformed_roster_row_is_refused_not_thinned(
    league, archive, thresholds
) -> None:
    """Vajaa rosteri on väärä rosterikynnys, ja se on tämän lukijan lupaus."""
    index(archive, league, thresholds)
    path = archive.root / "index" / "teams.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["teams"][0]["roster"].append("ei ole olio")
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="ei ole olio"):
        select(league, archive, thresholds)


# -- Vaihe ei koske muualle --------------------------------------------------


def test_the_stage_writes_only_into_the_selections_directory(
    league, archive, thresholds
) -> None:
    """Hyväksymiskriteeri: ``aggregates/`` ja ``classified/`` ovat tavu tavulta samat."""
    index(archive, league, thresholds)
    for name in ("aggregates", "classified"):
        directory = archive.root / name / "ff03fb54599d3311"
        directory.mkdir(parents=True)
        (directory / "report.json").write_bytes(b'{"kosketaan": false}')
    before = _snapshot(archive.root)

    select(league, archive, thresholds)

    after = _snapshot(archive.root)
    for name in ("aggregates", "classified"):
        assert {k: v for k, v in after.items() if k.startswith(name)} == {
            k: v for k, v in before.items() if k.startswith(name)
        }
    new = set(after) - set(before)
    assert all(path.startswith("index/selections/") for path in new), new


def test_the_stage_has_no_manifest_and_never_skips(league, archive, thresholds) -> None:
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    assert result.manifest_path is None
    assert result.skipped is False


def test_the_write_is_atomic_and_leaves_no_temp_files(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    select(league, archive, thresholds)

    assert not has_temp_leftovers(archive.root)


def test_running_twice_produces_the_same_bytes_apart_from_the_timestamp(
    league, archive, thresholds
) -> None:
    """Diffattavuus: kahden ajon ero on luettava vain vakaassa järjestyksessä."""
    index(archive, league, thresholds)

    select(league, archive, thresholds)
    first = read_selection(archive, subject_key(archive))
    select(league, archive, thresholds)
    second = read_selection(archive, subject_key(archive))

    first.pop("generated_at")
    second.pop("generated_at")
    assert first == second


# -- Tiedoston sisältö on tarkistettavissa ilman asetuksia -------------------


def test_the_file_carries_the_thresholds_and_the_roster_it_decided_against(
    league, archive, thresholds
) -> None:
    """Päätöstä ei voi tarkistaa, jos sen peruste on muualla ja ehtinyt muuttua."""
    index(archive, league, thresholds)

    select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    assert document["roster_size"] == 5
    assert document["roster_min_regulars"] == 4
    assert len(document["roster"]) == DIVISION[SUBJECT]
    assert document["team_name"] == SUBJECT
    assert document["competition_ids"] == [CHAMPIONSHIP]
    assert document["index_generated_at"]


def test_the_file_has_a_reader_that_checks_its_version(
    league, archive, thresholds
) -> None:
    """``schema_version`` kirjoitetaan, joten se on myös luettava.

    Muuten versio olisi kenttä, jota kukaan ei tarkista, ja jokainen kuluttaja
    päättelisi muodon kenttien olemassaolosta.
    """
    index(archive, league, thresholds)
    select(league, archive, thresholds)
    key = subject_key(archive)

    path = archive.selection(key)
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="muotoa 99"):
        select_stage.read_selection(archive, key)


def test_reading_a_missing_selection_says_which_command_writes_it(
    archive,
) -> None:
    with pytest.raises(PappascoutError, match="pappascout select"):
        select_stage.read_selection(archive, "ei-ole")


def test_the_summary_and_the_file_cannot_disagree(league, archive, thresholds) -> None:
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    document = read_selection(archive, subject_key(archive))
    for key, value in document["counts"].items():
        assert result.stats[key] == value


def test_the_output_path_is_relative_and_named_by_team_key(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    result = select(league, archive, thresholds)

    key = subject_key(archive)
    assert [str(path) for path in result.outputs] == [f"index/selections/{key}.json"]
    assert result.unit == key


# -- Yhteenveto renderöityy oikeasta ajon tuloksesta ------------------------
#
# Nämä ovat se testi, joka puuttui: aiemmin ``_render_select`` sai vain käsin
# kirjoitetun sanakirjan, jota vaihe ei koskaan tuota. Silloin vaiheen ja
# tulosteen väliltä sai kadota avain kenenkään huomaamatta.


def test_the_summary_renders_from_a_real_stage_result(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    text = _render_select(select(league, archive, thresholds))

    assert SUBJECT in text
    assert "2 / 2 karttaa otantaan" in text
    assert "Vakirosteri" in text
    assert "8 pelaajaa, kynnys 4/5" in text
    assert "Rosteriluokat" in text
    assert "5/5: 2" in text
    assert "index/selections/" in text


def test_the_rejection_block_renders_from_a_real_stage_result(
    league, archive, thresholds
) -> None:
    """Hylkäyslohko tulostuu vaiheen omista riveistä, ei käsin kootusta listasta.

    Aiemmin ``stats``-avaimen uudelleennimeäminen olisi tyhjentänyt lohkon
    ilman että yksikään testi kaatuu.
    """
    matches = borrow(division_matches(), SUBJECT_FACTION, PLAYED_MATCH, keep=3)
    index(archive, league, thresholds, matches=matches)
    result = select(league, archive, thresholds)

    text = _render_select(result)

    assert "Hylätyt kartat (2)" in text
    for row in read_selection(archive, subject_key(archive))["selections"]:
        # Syy kokonaisena, ei typistettynä.
        assert row["roster_reason"] in text


def test_every_note_reaches_the_summary_on_its_own_line(
    league, archive, thresholds
) -> None:
    """Kaksi huomiota, ja **molemmat** näkyvät.

    Aiemmin vain ensimmäinen selvisi, jolloin "yksikään kartta ei päätynyt
    otantaan" nielaisi tiedon puuttuvasta vetotiedosta.
    """
    matches = tuple(
        replace(match, map_picks=()) if match.match_id == PLAYED_MATCH else match
        for match in division_matches()
    )
    index(archive, league, thresholds, matches=matches)

    text = _render_select(select(league, archive, thresholds))

    notes = [line for line in text.splitlines() if "Huomio" in line]
    assert len(notes) == 2
    assert any("vielä pelaamatta" in line for line in notes)
    assert any("vetotieto puuttuu" in line for line in notes)


def test_the_summary_counts_league_matches_and_sources(
    league, archive, thresholds
) -> None:
    index(archive, league, thresholds)

    text = _render_select(select(league, archive, thresholds))

    assert "2 / 2 kartasta" in text
    assert "0 havaintoa demosta, 2 ennustetta ottelurosterista" in text


# -- Apurit ------------------------------------------------------------------


def borrow(matches, faction_id: str, match_id: str, *, keep: int):
    """Lainaa **yhteen otteluun** pelaajia toiselta divisioonan joukkueelta.

    Näin ulkopuolinen pelaaja oikeasti syntyy, ja keksityllä
    ``vieras0``-tunnisteella sitä ei voisi testata lainkaan: vakirosteri on
    ``domain.teams``in mukaan **yhdiste joukkueen kaikista otteluista**, joten
    kuka tahansa, joka esiintyy joukkueen ottelurivillä, on määritelmän mukaan
    sen vakipelaaja. Ulkopuolinen on siis pelaaja, jonka **toinen joukkue
    havaitsi myöhemmin** -- silloin siirtymäsääntö ottaa hänet pois tämän
    joukkueen rosterista ja jättää hänet ``released``iin.

    Lainaus tehdään siksi vain yhteen otteluun, ja lainanantaja
    (:data:`LENDER`) on joukkue, joka pelaa vielä lainauksen jälkeen -- se
    havaitsee omat pelaajansa viimeksi.
    """
    from pappascout.adapters.protocols import RosterPlayer

    lender = tuple(
        RosterPlayer(
            player_id=member.player_id or "",
            nickname=member.nickname,
            game_player_id=member.game_player_id,
        )
        for member in members(LENDER, DIVISION[LENDER], offset=TEAM_INDEX[LENDER] * 100)
    )

    changed = []
    for match in matches:
        if match.match_id != match_id:
            changed.append(match)
            continue
        sides = []
        for side in match.teams:
            if side.team_id != faction_id:
                sides.append(side)
                continue
            missing = len(side.roster) - keep
            sides.append(replace(side, roster=side.roster[:keep] + lender[:missing]))
        changed.append(replace(match, teams=tuple(sides)))
    return tuple(changed)


def write_raw_lineups(
    archive: ArchivePaths, map_demo_id: str, pairs: list[tuple[str | None, str | None]]
) -> None:
    """Kokoonpanotaulu, jossa tunniste saa olla ``null``.

    ``write_lineups`` ei kelpaa tähän: se rakentaa rivit sanakirjasta, jonka
    avain ei voi olla ``None``.
    """
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "map_demo_id": map_demo_id,
            "lineup_key": key,
            "player_id": player,
            "player_name": None,
            "clan_name": None,
        }
        for key, player in pairs
    ]
    pl.DataFrame(rows, schema=dict(LINEUPS)).write_parquet(path)


def _snapshot(root: Path) -> dict[str, bytes]:
    """Arkiston tiedostot sisältöineen -- tavu tavulta -vertailua varten."""
    return {
        str(path.relative_to(root).as_posix()): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
