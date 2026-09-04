"""``stages.discover`` -- vaiheen testit (Story 3.2).

**Ei verkkoa.** Portin tilalla on :class:`FakeSource`, joka palauttaa käsin
rakennetut ottelut ja laskee kutsunsa. Jos joku poistaisi ``source``-parametrin,
testi ei menisi hiljaa verkkoon vaan kaatuisi.

Yksi poikkeus on tarkoituksellinen: :func:`test_default_source_really_builds_a_port`
ajaa ``default_source``in oikeasti. Se on vaiheen ainoa rivi, joka liittää sen
verkkoon, ja jokainen muu testi korvaa sen -- ilman tätä testiä sen rikkoutuminen
ei kaataisi mitään. Verkkoon se ei silti mene: asiakas rakennetaan, ei käytetä.

Aineisto on divisioonan oikea muoto: 12 joukkuetta, round robin, 66 ottelua, 11
per joukkue -- samat luvut kuin ``mittaus-faceit-aineisto.md``issa. Nimet ja
rosterikoot tulevat ``test_teams``ista, jotta mitatut luvut ovat yhdessä
paikassa.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
import pytest
from conftest import has_temp_leftovers
from test_teams import DIVISION, MEASURED_RCAVE_IDS, POTKU, RCAVE, members, steam_id

from pappascout.adapters.protocols import Match, MatchSource, MatchTeam, RosterPlayer
from pappascout.archive.paths import ArchivePaths, parsed_table
from pappascout.domain.models import (
    SETTINGS_ENV_VAR,
    LeagueSettings,
    ThresholdSettings,
    load_settings,
)
from pappascout.domain.schemas import LINEUPS
from pappascout.domain.teams import is_steam_id64
from pappascout.errors import PappascoutError, SettingsError
from pappascout.stages import discover as discover_stage

CHAMPIONSHIP = "94681888-b5da-4ab5-bf50-f44b666b98a3"

#: Mitattu: 66 ottelua, joista 6 pelattu.
TOTAL_MATCHES = 66
FINISHED_MATCHES = 6

KICKOFF = datetime(2026, 8, 3, 18, 0, tzinfo=UTC)


class FakeSource:
    """Otteluportin feikki. Laskee kutsut, jotta "haetaan uudelleen" on mitattava."""

    def __init__(self, matches: dict[str, tuple[Match, ...]]) -> None:
        self._matches = matches
        self.calls: list[str] = []

    def replace(self, competition_id: str, matches: tuple[Match, ...]) -> None:
        self._matches[competition_id] = matches

    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        self.calls.append(competition_id)
        return self._matches.get(competition_id, ())

    def get_match(self, match_id: str) -> Match:  # pragma: no cover - ei käytössä
        raise AssertionError("discover ei hae yksittäisiä otteluita")


def players(
    team_name: str, size: int, offset: int, shift: int
) -> tuple[tuple[RosterPlayer, ...], tuple[RosterPlayer, ...]]:
    """Yhden ottelun aloittajat ja vaihtopelaajat portin sanastolla.

    Aloittajajoukko kiertää ottelusta toiseen, joten yksikään ottelurivi ei
    yksinään sisällä koko vakirosteria -- vaiheen on koottava se yhdisteenä.
    """
    roster = members(team_name, size, offset=offset)
    rotated = roster[shift % size :] + roster[: shift % size]
    as_port = tuple(
        RosterPlayer(
            player_id=member.player_id or "",
            nickname=member.nickname,
            game_player_id=member.game_player_id,
        )
        for member in rotated
    )
    return as_port[:5], as_port[5:]


def division_matches() -> tuple[Match, ...]:
    """Round robin: 12 joukkuetta, 66 ottelua, 11 per joukkue.

    Kuusi ensimmäistä ottelua on pelattu ja loput ajastettu -- sama suhde kuin
    mitattu (6 ``FINISHED``, 60 ``SCHEDULED``). ``PotkukelkkaPeek`` on
    divisioonan kolmas nimi, joten se osuu pelattuihin **tasan kerran**: juuri
    se tapaus, jonka hyväksymiskriteeri vaatii.
    """
    names = list(DIVISION)
    appearances = {name: 0 for name in names}
    matches: list[Match] = []
    for first in range(len(names)):
        for second in range(first + 1, len(names)):
            index = len(matches)
            sides = []
            for team_index in (first, second):
                name = names[team_index]
                roster, substitutes = players(
                    name,
                    DIVISION[name],
                    offset=team_index * 100,
                    shift=appearances[name],
                )
                appearances[name] += 1
                sides.append(
                    MatchTeam(
                        team_id=f"faction-{team_index:02d}",
                        name=name,
                        roster=roster,
                        substitutes=substitutes,
                    )
                )
            played = index < FINISHED_MATCHES
            matches.append(
                Match(
                    match_id=f"1-match-{index:02d}",
                    competition_id=CHAMPIONSHIP,
                    status="FINISHED" if played else "SCHEDULED",
                    scheduled_at=KICKOFF + timedelta(days=index),
                    finished_at=(
                        KICKOFF + timedelta(days=index, hours=2) if played else None
                    ),
                    teams=tuple(sides),
                    map_picks=("de_ancient", "de_nuke") if played else (),
                )
            )
    return tuple(matches)


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
    """``[thresholds]``-osio oletuksillaan; ``team_identity_min_common`` = 3."""
    return ThresholdSettings(pistol_rounds=[1, 13])


@pytest.fixture
def archive(tmp_path: Path) -> ArchivePaths:
    return ArchivePaths(root=tmp_path / "arkisto")


@pytest.fixture
def source() -> FakeSource:
    return FakeSource({CHAMPIONSHIP: division_matches()})


def discover(
    league: LeagueSettings,
    archive: ArchivePaths,
    thresholds: ThresholdSettings,
    source: MatchSource,
    team: str | None = None,
):
    return discover_stage.run(
        league, archive, team, source=source, thresholds=thresholds
    )


def read_index(archive: ArchivePaths, name: str) -> dict[str, Any]:
    return json.loads((archive.root / "index" / name).read_text(encoding="utf-8"))


# -- Yksi kutsu, kaksi indeksiä ---------------------------------------------


def test_one_call_per_competition_produces_both_indexes(
    league, archive, thresholds, source
) -> None:
    """Mitattu: rosteri on ottelurivillä, joten erillistä rosterihakua ei tarvita."""
    result = discover(league, archive, thresholds, source)

    assert source.calls == [CHAMPIONSHIP]
    assert [str(path) for path in result.outputs] == [
        "index/matches.json",
        "index/teams.json",
    ]
    assert (archive.root / "index" / "matches.json").is_file()
    assert (archive.root / "index" / "teams.json").is_file()


def test_the_run_summary_counts_matches_and_teams(
    league, archive, thresholds, source
) -> None:
    result = discover(league, archive, thresholds, source)

    assert result.stats["matches"] == TOTAL_MATCHES
    assert result.stats["matches_played"] == FINISHED_MATCHES
    assert result.stats["teams"] == len(DIVISION)
    assert result.stats["roster_min"] == 6
    assert result.stats["roster_max"] == 9
    assert result.status == "ok"
    assert result.skipped is False
    assert result.reason is None


def test_the_stage_has_no_manifest_because_it_never_skips(
    league, archive, thresholds, source
) -> None:
    """Ohitus säästäisi yhden kutsun ja maksaisi uusien otteluiden näkemisen."""
    result = discover(league, archive, thresholds, source)

    assert result.manifest_path is None


def test_the_unit_is_one_identifier_not_a_list(
    archive, thresholds
) -> None:
    """``StageResult.unit`` on muualla putkessa aina yksi tunniste.

    Pilkuilla yhdistetty lista lukisi tulosteessa tunnisteelta olematta
    sellainen; koko luettelo on ``stats["competition_ids"]``issä.
    """
    league = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP, "toinen"],
        map_pool=["de_ancient"],
    )
    source = FakeSource({CHAMPIONSHIP: division_matches()})

    result = discover(league, archive, thresholds, source)

    assert result.unit == CHAMPIONSHIP
    assert "," not in result.unit
    assert result.stats["competition_ids"] == [CHAMPIONSHIP, "toinen"]


# -- Joukkueindeksin sisältö ------------------------------------------------


def test_the_teams_index_holds_twelve_teams_with_six_to_nine_players(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: 12 joukkuetta, rosterit 6--9 pelaajaa."""
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    sizes = {team["name"]: len(team["roster"]) for team in document["teams"]}

    assert sizes == DIVISION
    assert min(sizes.values()) == 6
    assert max(sizes.values()) == 9


def test_every_roster_identifier_in_the_index_is_steam_id64(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: jokainen tunniste on SteamID64-muotoinen.

    ``player_id`` (FACEITin UUID) on rivillä mukana jäljitettävyyttä varten,
    mutta se **ei ole** rosterin tunniste -- sitä ei esiinny demoissa.
    """
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    for team in document["teams"]:
        for player in team["roster"]:
            assert is_steam_id64(player["game_player_id"]), player


def test_the_team_row_carries_identifier_lists_not_counts(
    league, archive, thresholds, source
) -> None:
    """Indeksissä on tunnisteet, jotta ottelun ja joukkueen liitos on tehtävissä.

    Pelkkä lukumäärä ei kertoisi, **mitkä** ottelut nämä ovat, eikä
    ``index/matches.json``iin voisi liittyä mistään.
    """
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    potku = next(t for t in document["teams"] if t["name"] == "PotkukelkkaPeek")

    assert len(potku["match_ids"]) == 11
    assert potku["played_match_ids"] == ["1-match-01"]
    assert potku["roster_size"] == len(potku["roster"]) == 8
    known = {row["match_id"] for row in read_index(archive, "matches.json")["matches"]}
    assert set(potku["match_ids"]) <= known


def test_both_indexes_order_matches_the_same_way(
    league, archive, thresholds, source
) -> None:
    """Katselmus: ottelut olivat aikajärjestyksessä, joukkueen ottelut eivät.

    Kaksi eri järjestystä samoille tunnisteille tekisi tiedostojen
    rinnakkaisesta lukemisesta työlästä ilman syytä.
    """
    discover(league, archive, thresholds, source)

    order = [
        row["match_id"] for row in read_index(archive, "matches.json")["matches"]
    ]
    position = {match_id: index for index, match_id in enumerate(order)}
    for team in read_index(archive, "teams.json")["teams"]:
        indices = [position[match_id] for match_id in team["match_ids"]]
        assert indices == sorted(indices), team["name"]


def test_the_index_says_which_form_it_is_in(
    league, archive, thresholds, source
) -> None:
    discover(league, archive, thresholds, source)

    for name in ("teams.json", "matches.json"):
        document = read_index(archive, name)
        assert document["schema_version"] == discover_stage.SCHEMA_VERSION
        assert document["competition_ids"] == [CHAMPIONSHIP]
        assert document["generated_at"]


def test_the_matches_index_carries_status_schedule_and_maps(
    league, archive, thresholds, source
) -> None:
    discover(league, archive, thresholds, source)

    document = read_index(archive, "matches.json")
    assert len(document["matches"]) == TOTAL_MATCHES

    first = document["matches"][0]
    assert first["status"] == "FINISHED"
    assert first["played"] is True
    assert first["scheduled_at"].startswith("2026-08-03")
    assert first["map_picks"] == ["de_ancient", "de_nuke"]
    assert [side["name"] for side in first["teams"]] == ["popsiCS", "JYSAEYTTAEJAET"]
    # Ottelurivillä on lähteen tunniste, ei kanoninen team_key: rivi kertoo mitä
    # lähde sanoi, ja identiteetin päättää joukkueindeksi.
    assert first["teams"][0]["faction_id"] == "faction-00"

    last = document["matches"][-1]
    assert last["played"] is False
    assert last["map_picks"] == []


# -- Lukija ------------------------------------------------------------------


def test_the_indexes_can_be_read_back_as_a_pair(
    league, archive, thresholds, source
) -> None:
    """``schema_version`` kirjoitetaan **ja** luetaan; jatkovaihe ei pura JSONia."""
    discover(league, archive, thresholds, source)

    matches, teams = discover_stage.read_indexes(archive)

    assert len(matches["matches"]) == TOTAL_MATCHES
    assert len(teams["teams"]) == len(DIVISION)
    assert matches["generated_at"] == teams["generated_at"]


def test_reading_a_missing_index_says_what_to_run(archive) -> None:
    with pytest.raises(PappascoutError, match="discover"):
        discover_stage.read_teams_index(archive)


def test_an_unknown_schema_version_is_refused(
    league, archive, thresholds, source
) -> None:
    """Tuntematon muoto on virhe, ei arvaus."""
    discover(league, archive, thresholds, source)
    path = archive.teams_index()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 99
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="99"):
        discover_stage.read_teams_index(archive)


def test_indexes_from_two_different_runs_are_refused_as_a_pair(
    league, archive, thresholds, source
) -> None:
    """Kirjoitus voi keskeytyä tiedostojen välissä; silloin niitä ei saa liittää.

    Molemmissa on sama ``generated_at`` juuri tätä varten -- ilman vertailua
    lukija liittäisi uuden ottelulistan vanhaan joukkueindeksiin hiljaa.
    """
    discover(league, archive, thresholds, source)
    path = archive.teams_index()
    document = json.loads(path.read_text(encoding="utf-8"))
    document["generated_at"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(PappascoutError, match="eri ajoista"):
        discover_stage.read_indexes(archive)


def test_a_failure_between_the_two_writes_leaves_both_untouched(
    league, archive, thresholds, source, monkeypatch
) -> None:
    """Pari kirjoitetaan väliaikaistiedostoihin ennen kuin kumpaakaan vaihdetaan.

    Ilman tätä epäonnistuminen jättäisi arkistoon uuden ottelulistan ja vanhan
    joukkueindeksin, ja lukija liittäisi ne yhteen.
    """
    discover(league, archive, thresholds, source)
    before_matches = archive.matches_index().read_bytes()
    before_teams = archive.teams_index().read_bytes()

    def explode(*args, **kwargs):
        raise RuntimeError("levy täynnä")

    monkeypatch.setattr("pappascout.stages.discover._dump", explode)
    with pytest.raises(RuntimeError):
        discover(league, archive, thresholds, source)

    assert archive.matches_index().read_bytes() == before_matches
    assert archive.teams_index().read_bytes() == before_teams
    assert not has_temp_leftovers(archive.root)


# -- Nimihaku vaiheen läpi --------------------------------------------------


def test_a_partial_name_finds_rcave_and_its_seven_players(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: ``Rcave`` -> yksi joukkue, vakirosteri 7 pelaajaa."""
    result = discover(league, archive, thresholds, source, team="Rcave")

    team = result.stats["team"]
    assert team["name"] == "Rcave Veterans"
    assert team["roster"] == list(RCAVE)
    assert team["roster_size"] == 7
    assert result.unit == team["team_key"]


def test_the_measured_rcave_identifiers_survive_into_the_index(
    league, archive, thresholds, source
) -> None:
    """Neljä mitattua SteamID64:ää ovat juuri ne, jotka liittävät demoon."""
    discover(league, archive, thresholds, source)

    document = read_index(archive, "teams.json")
    rcave = next(t for t in document["teams"] if t["name"] == "Rcave Veterans")
    identifiers = {player["game_player_id"] for player in rcave["roster"]}

    assert set(MEASURED_RCAVE_IDS.values()) <= identifiers


def test_a_team_with_one_played_match_out_of_eleven_is_found_in_full(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: pelattujen otteluiden määrä ei vaikuta rosteriin."""
    result = discover(league, archive, thresholds, source, team="PotkukelkkaPeek")

    team = result.stats["team"]
    assert team["matches"] == 11
    assert team["matches_played"] == 1
    assert team["roster"] == list(POTKU)


def test_case_does_not_matter_through_the_stage(
    league, archive, thresholds, source
) -> None:
    result = discover(league, archive, thresholds, source, team="POTKUKELKKAPEEK")

    assert result.stats["team"]["name"] == "PotkukelkkaPeek"


def test_an_ambiguous_name_lists_all_three_and_chooses_none(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: ``T`` listaa kolme joukkuetta eikä valitse yhtään."""
    with pytest.raises(PappascoutError) as error:
        discover(league, archive, thresholds, source, team="T")

    message = str(error.value)
    for name in ("TUUHEE", "Takakeno", "Tankkiluola vilttiketju"):
        assert name in message
    assert "valinta on tehtävä" in message.lower()


def test_the_ambiguity_listing_shows_identifiers_and_suggests_a_working_query(
    league, archive, thresholds
) -> None:
    """Katselmus: samannimisten kohdalla ehdotus oli juuri se haku, joka epäonnistui.

    Kaksi identtistä riviä ilman tunnistetta ei ole valinta vaan umpikuja.
    """
    shared_name = "Takakeno"
    matches = (
        Match(
            match_id="1-a",
            competition_id=CHAMPIONSHIP,
            status="SCHEDULED",
            scheduled_at=KICKOFF,
            teams=(
                MatchTeam(
                    team_id="faction-a",
                    name=shared_name,
                    roster=players("a", 5, 0, 0)[0],
                ),
                MatchTeam(
                    team_id="faction-b",
                    name=shared_name,
                    roster=players("b", 5, 500, 0)[0],
                ),
            ),
        ),
    )
    source = FakeSource({CHAMPIONSHIP: matches})

    with pytest.raises(PappascoutError) as error:
        discover(league, archive, thresholds, source, team=shared_name)

    message = str(error.value)
    assert "faction-a" in message and "faction-b" in message
    # Ehdotus on tunniste eikä nimi, koska nimi osuu molempiin.
    assert "--team faction-a" in message


def test_the_indexes_are_written_even_when_the_name_is_ambiguous(
    league, archive, thresholds, source
) -> None:
    """Haku on näkymä hakutulokseen, ei ehto sille.

    Ilman tätä monitulkintainen nimi jättäisi indeksit kirjoittamatta, ja
    käyttäjän olisi ajettava komento kahdesti nähdäkseen ne joukkueet, joiden
    väliltä hän valitsee.
    """
    with pytest.raises(PappascoutError):
        discover(league, archive, thresholds, source, team="T")

    assert (archive.root / "index" / "teams.json").is_file()
    assert (archive.root / "index" / "matches.json").is_file()


def test_an_unknown_name_lists_the_whole_division(
    league, archive, thresholds, source
) -> None:
    """I/O-matriisi: ``Astralis`` ei ole divisioonassa."""
    with pytest.raises(PappascoutError) as error:
        discover(league, archive, thresholds, source, team="Astralis")

    message = str(error.value)
    assert "Astralis" in message
    for name in DIVISION:
        assert name in message


def test_the_division_can_be_listed_without_causing_an_error(
    league, archive, thresholds, source
) -> None:
    """Katselmus: nimet sai näkyviin vain syöttämällä tahallaan väärän nimen.

    Ilman ``--team``-valintaa yhteenveto luettelee divisioonan joukkueet
    tunnisteineen -- juuri se, mitä käyttäjä tarvitsee monitulkintaisen haun
    jälkeen.
    """
    result = discover(league, archive, thresholds, source)

    listing = result.stats["division"]
    assert [row["name"] for row in listing] == [
        team["name"] for team in read_index(archive, "teams.json")["teams"]
    ]
    assert all(row["team_key"] for row in listing)
    assert "team" not in result.stats


# -- Toinen ajo -------------------------------------------------------------


def test_running_twice_fetches_the_match_list_again(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: uudet ottelut eivät jää näkymättä.

    Ottelulistaa ei välimuistiteta eikä vaihetta ohiteta, joten toinen ajo
    näkee sen, mikä muuttui. 60 ottelua 66:sta oli mittaushetkellä pelaamatta.
    """
    discover(league, archive, thresholds, source)
    discover(league, archive, thresholds, source)

    assert source.calls == [CHAMPIONSHIP, CHAMPIONSHIP]


def test_a_new_match_shows_up_on_the_second_run(league, archive, thresholds) -> None:
    first = division_matches()
    source = FakeSource({CHAMPIONSHIP: first})
    discover(league, archive, thresholds, source)

    source.replace(
        CHAMPIONSHIP,
        first
        + (
            Match(
                match_id="1-match-99",
                competition_id=CHAMPIONSHIP,
                status="SCHEDULED",
                scheduled_at=KICKOFF + timedelta(days=99),
                teams=first[0].teams,
            ),
        ),
    )
    result = discover(league, archive, thresholds, source)

    assert result.stats["matches"] == TOTAL_MATCHES + 1
    ids = [row["match_id"] for row in read_index(archive, "matches.json")["matches"]]
    assert "1-match-99" in ids


# -- Arkisto pysyy koskemattomana -------------------------------------------


def test_aggregates_and_classified_are_neither_renamed_nor_changed(
    league, archive, thresholds, source
) -> None:
    """Hyväksymiskriteeri: arkiston joukkuehakemistot säilyvät sellaisinaan.

    Arkiston nimeämispäätös on Story 3.4, ja se tehdään havainnolla eikä
    ennakolta -- joten tämä vaihe ei saa koskea niihin lainkaan.
    """
    for kind in ("aggregates", "classified"):
        directory = archive.root / kind / "ff03fb54599d3311"
        directory.mkdir(parents=True)
        (directory / "report.json").write_text("vanha", encoding="utf-8")

    before = {
        path.relative_to(archive.root).as_posix(): path.read_bytes()
        for path in archive.root.rglob("*")
        if path.is_file()
    }

    discover(league, archive, thresholds, source, team="Rcave")

    after = {
        path.relative_to(archive.root).as_posix(): path.read_bytes()
        for path in archive.root.rglob("*")
        if path.is_file()
        if not path.relative_to(archive.root).as_posix().startswith("index/")
    }
    assert after == before


def test_no_selection_or_next_opponent_file_is_written(
    league, archive, thresholds, source
) -> None:
    """Rajat: ``index/selections/`` on Story 3.3 ja ``next_opponent`` on Epic 4."""
    discover(league, archive, thresholds, source, team="Rcave")

    written = sorted(
        path.relative_to(archive.root).as_posix()
        for path in archive.root.rglob("*")
        if path.is_file()
    )
    assert written == ["index/matches.json", "index/teams.json"]


def test_atomic_writes_leave_no_temporary_files(
    league, archive, thresholds, source
) -> None:
    """Arkisto on OneDrivessa: puolikas indeksi olisi konfliktikopion siemen."""
    discover(league, archive, thresholds, source)

    assert not has_temp_leftovers(archive.root)


# -- Silta arkistoon --------------------------------------------------------


def write_lineups(archive: ArchivePaths, map_demo_id: str, lineups: dict) -> None:
    path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "map_demo_id": map_demo_id,
            "lineup_key": lineup_key,
            "player_id": player_id,
            "player_name": None,
            "clan_name": None,
        }
        for lineup_key, player_ids in lineups.items()
        for player_id in player_ids
    ]
    pl.DataFrame(rows, schema=dict(LINEUPS)).write_parquet(path)


def test_known_lineup_keys_bridge_the_index_to_the_archive(
    league, archive, thresholds, source
) -> None:
    """``index/teams.json`` kantaa tunnetut kokoonpanotiivisteet.

    Se on ainoa luettava yhteys ``aggregates/<team_key>``-hakemistoihin, joita
    tämä tarina ei nimeä uudelleen.
    """
    write_lineups(
        archive,
        "1-match-00-0",
        {"ff03fb54599d3311": list(MEASURED_RCAVE_IDS.values())},
    )

    result = discover(league, archive, thresholds, source, team="Rcave")

    assert result.stats["team"]["lineup_keys"] == ["ff03fb54599d3311"]
    assert result.stats["contested_lineup_keys"] == []
    document = read_index(archive, "teams.json")
    rcave = next(t for t in document["teams"] if t["name"] == "Rcave Veterans")
    assert rcave["lineup_keys"] == ["ff03fb54599d3311"]


def test_a_lineup_claimed_by_two_teams_is_flagged_in_the_index(
    league, archive, thresholds, source
) -> None:
    """Kiistanalainen tiiviste on merkitty, jottei jatkovaihe laske sitä kahdesti."""
    rcave = list(MEASURED_RCAVE_IDS.values())
    # Kolme Rcaven pelaajaa ja kolme PotkukelkkaPeekin: molemmat ylittävät
    # kynnyksen 3, eikä kumpikaan ole "enemmän oikeassa".
    potku_ids = [steam_id(200 + index) for index in range(3)]
    write_lineups(archive, "1-match-00-0", {"kiistanalainen": rcave[:3] + potku_ids})

    result = discover(league, archive, thresholds, source)

    assert result.stats["contested_lineup_keys"] == ["kiistanalainen"]
    document = read_index(archive, "teams.json")
    owners = [t["name"] for t in document["teams"] if t["lineup_keys"]]
    assert len(owners) == 2
    assert document["contested_lineup_keys"] == ["kiistanalainen"]


def test_a_missing_or_unreadable_lineup_table_is_not_a_reason_to_fail(
    league, archive, thresholds, source
) -> None:
    """Silta on lisätietoa: ilman sitä indeksi on yhä oikea."""
    broken = archive.resolve(parsed_table("1-rikki-0", "lineups"))
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"ei ole parquet")
    (archive.root / "parsed" / "ei-hakemisto").write_text("x", encoding="utf-8")

    result = discover(league, archive, thresholds, source, team="Rcave")

    assert result.stats["team"]["lineup_keys"] == []


# -- Havaintojen reunatapaukset ---------------------------------------------


def test_the_same_player_missing_a_steam_id_is_counted_once_not_once_per_match(
    league, archive, thresholds
) -> None:
    """Katselmus: laskuri laski esiintymiä, joten yksi pelaaja oli "11 pelaajaa".

    Pudotetut kerrotaan lisäksi **nimeltä**, jotta käyttäjä voi tarkistaa
    keneltä tunniste puuttui -- pelkkä luku olisi väite ilman
    tarkistusmahdollisuutta.
    """
    matches = tuple(
        Match(
            match_id=f"1-vajaa-{index}",
            competition_id=CHAMPIONSHIP,
            status="SCHEDULED",
            scheduled_at=KICKOFF + timedelta(days=index),
            teams=(
                MatchTeam(
                    team_id="faction-x",
                    name="Vajaa",
                    roster=(
                        RosterPlayer(
                            player_id="uuid-1",
                            nickname="a",
                            game_player_id="76561197977479426",
                        ),
                        RosterPlayer(
                            player_id="uuid-2", nickname="ei-tunnistetta"
                        ),
                    ),
                ),
            ),
        )
        for index in range(11)
    )
    source = FakeSource({CHAMPIONSHIP: matches})

    result = discover(league, archive, thresholds, source, team="Vajaa")

    assert result.stats["players_without_steam_id"] == 1
    assert result.stats["dropped_players"] == [
        {
            "player_id": "uuid-2",
            "nickname": "ei-tunnistetta",
            "team": "Vajaa",
            "match_id": "1-vajaa-0",
        }
    ]
    assert result.stats["team"]["roster"] == ["a"]


def test_a_side_without_an_identifier_is_counted_not_only_dropped(
    league, archive, thresholds
) -> None:
    """Katselmus: pudotetut pelaajat kerrottiin, pudotetut joukkuerivit eivät.

    Epäsymmetria oli hiljainen pudotus siinä missä mikä tahansa muukin.
    """
    match = Match(
        match_id="1-tuntematon",
        competition_id=CHAMPIONSHIP,
        status="SCHEDULED",
        scheduled_at=KICKOFF,
        teams=(MatchTeam(team_id=None, name="Vielä ratkeamatta"),),
    )
    source = FakeSource({CHAMPIONSHIP: (match,)})

    result = discover(league, archive, thresholds, source)

    assert result.stats["teams"] == 0
    assert result.stats["team_rows_without_id"] == 1
    assert result.reason is not None
    assert "joukkuetta" in result.reason


def test_an_empty_competition_says_where_to_look(league, archive, thresholds) -> None:
    """Kilpailu ilman otteluita on havainto, ei virhe -- mutta ei myöskään hiljainen.

    ``status`` pysyy ``ok``:na, koska haku onnistui; syy kerrotaan
    ``reason``issa, ja komento nostaa sen tulosteensa kärkeen.
    """
    source = FakeSource({CHAMPIONSHIP: ()})

    result = discover(league, archive, thresholds, source)

    assert result.stats["matches"] == 0
    assert result.status == "ok"
    assert result.reason is not None
    assert "championship_ids" in result.reason
    assert read_index(archive, "teams.json")["teams"] == []


def test_a_team_whose_players_all_lack_steam_ids_is_named_in_the_reason(
    league, archive, thresholds
) -> None:
    """Tyhjä rosteri kirjoitettiin indeksiin ilman merkintää mistään."""
    match = Match(
        match_id="1-tyhja",
        competition_id=CHAMPIONSHIP,
        status="SCHEDULED",
        scheduled_at=KICKOFF,
        teams=(
            MatchTeam(
                team_id="faction-x",
                name="Tunnisteeton",
                roster=(RosterPlayer(player_id="uuid-1", nickname="a"),),
            ),
        ),
    )
    source = FakeSource({CHAMPIONSHIP: (match,)})

    result = discover(league, archive, thresholds, source)

    assert result.stats["teams_without_roster"] == 1
    assert result.reason is not None
    assert "Tunnisteeton" in result.reason


def test_a_name_search_in_an_empty_division_says_why(
    league, archive, thresholds
) -> None:
    source = FakeSource({CHAMPIONSHIP: ()})

    with pytest.raises(PappascoutError, match="championship_ids"):
        discover(league, archive, thresholds, source, team="Rcave")


def test_the_same_match_in_two_competitions_is_counted_once(
    archive, thresholds
) -> None:
    """Sama ottelu kahdessa kilpailussa on yksi ottelu, ei kaksi."""
    other = "toinen-championship"
    league = LeagueSettings(
        season=13,
        organizer_id="org",
        championship_ids=[CHAMPIONSHIP, other],
        map_pool=["de_ancient"],
    )
    matches = division_matches()
    source = FakeSource({CHAMPIONSHIP: matches, other: matches[:5]})

    result = discover(league, archive, thresholds, source)

    assert source.calls == [CHAMPIONSHIP, other]
    assert result.stats["matches"] == TOTAL_MATCHES


def test_a_transferred_player_is_reported_in_the_run_summary(
    league, archive, thresholds
) -> None:
    """Siirtymä muuttaa rosteria, joten se ei saa jäädä vain tiedostoon."""
    mover = RosterPlayer(
        player_id="uuid-mover", nickname="siirtyja", game_player_id=steam_id(1)
    )
    a_players = players("aaa", 5, 100, 0)[0]
    b_players = players("bbb", 5, 200, 0)[0]
    matches = (
        Match(
            match_id="1-a",
            competition_id=CHAMPIONSHIP,
            status="FINISHED",
            scheduled_at=KICKOFF,
            teams=(
                MatchTeam(
                    team_id="A", name="Aakkoset", roster=a_players[:4] + (mover,)
                ),
            ),
        ),
        Match(
            match_id="1-b",
            competition_id=CHAMPIONSHIP,
            status="SCHEDULED",
            scheduled_at=KICKOFF + timedelta(days=30),
            teams=(
                MatchTeam(team_id="B", name="Beeta", roster=b_players[:4] + (mover,)),
            ),
        ),
    )
    source = FakeSource({CHAMPIONSHIP: matches})

    result = discover(league, archive, thresholds, source, team="Aakkoset")

    assert result.stats["transfers"] == [
        {
            "game_player_id": steam_id(1),
            "nickname": "siirtyja",
            "from_team": "Aakkoset",
            "kind": "released",
        }
    ]
    assert result.stats["team"]["released"] == ["siirtyja"]
    assert "siirtyja" not in result.stats["team"]["roster"]


def test_the_fake_source_satisfies_the_port(source) -> None:
    """Feikki ei saa olla löysempi kuin portti, tai testit mittaisivat väärää."""
    assert isinstance(source, MatchSource)


# -- Tuotannon portti --------------------------------------------------------


def test_default_source_really_builds_a_port(
    settings_file: Path, env_file, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``default_source`` ajetaan oikeasti -- **verkkoon menemättä**.

    Se on vaiheen ainoa rivi, joka liittää sen FACEITiin, ja jokainen muu testi
    korvaa sen feikillä. Ilman tätä testiä sen rikkoutuminen ei kaataisi mitään.
    Sisarensa ``stages.parse.default_parser`` ajetaan samoin aidosti.

    Asiakas **rakennetaan, ei käytetä**: yhtään pyyntöä ei lähde, ja
    välimuistihakemisto tarkistetaan siitä, minkä arkisto antoi.
    """
    env = env_file(".env", FACEIT_API_KEY="salainen-avain-XYZZY-42")
    monkeypatch.setenv(SETTINGS_ENV_VAR, str(settings_file))
    settings = load_settings(settings_file, env_files=(env,))
    archive = ArchivePaths(root=tmp_path / "arkisto")

    port = discover_stage.default_source(settings, archive)

    assert isinstance(port, MatchSource)
    assert port.cache_dir == archive.raw_faceit()
    # Avain ei näy esityksessä; se on adapterin lupaus, ja tämä on se kohta,
    # jossa asiakas syntyy.
    assert "XYZZY" not in repr(port)
    port.close()


def test_default_source_without_a_key_says_which_file_to_edit(
    settings_file: Path, tmp_path: Path
) -> None:
    """Puuttuva avain pysäyttää ajon suomenkieliseen ohjeeseen, ei pinojälkeen."""
    settings = load_settings(settings_file, env_files=())
    archive = ArchivePaths(root=tmp_path / "arkisto")

    with pytest.raises(SettingsError, match="FACEIT_API_KEY"):
        discover_stage.default_source(settings, archive)
