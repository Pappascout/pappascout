"""``discover`` -- putken alkupään ensimmäinen vaihe: divisioona kahdeksi indeksiksi.

Vaihe hakee kilpailun ottelut portin takaa **yhdellä kutsulla per kilpailu** ja
kirjoittaa niistä kaksi tiedostoa:

``index/matches.json``
    Ottelut sellaisenaan: tunniste, tila, aikataulu, osapuolet ja karttavalinnat.
``index/teams.json``
    Joukkueet vakirostereineen. Vakirosteri on aloittajien ja vaihtopelaajien
    **yhdiste** joukkueen kaikista otteluista.

Molemmilla on **yksi kirjoittaja, ja se on tämä vaihe**. Muut vaiheet lukevat --
:func:`read_indexes` on se lukija, jottei jokainen jatkovaihe purkaisi JSONia
käsin ja tulkitsisi ``schema_version``ia omalla tavallaan.

Miksi erillistä rosterihakua ei ole
-----------------------------------
Mittaus 2026-09-04 (``mittaus-faceit-aineisto.md`` luku 1): jokaisella
ottelurivillä on molempien osapuolten ``roster`` **ja** ``substitutes``, 132
joukkueriviä 132:sta. Vakirosteri on siis koottavissa ottelulistasta, eikä
portille tarvita ``get_roster``ia -- se olisi toinen tapa hakea sama asia ja
toinen välimuistiavain samalle vastaukselle.

Miksi vaiheella ei ole manifestia
---------------------------------
Muut vaiheet ohittavat työn, kun manifesti täsmää. Tämä ei: ottelulista
**muuttuu jatkuvasti** (mitattu 2026-09-04: 60 ottelua 66:sta oli vielä
pelaamatta), ja koko vaiheen tarkoitus on nähdä uudet ottelut. Ohitus säästäisi
yhden kutsun ja maksaisi juuri sen, mitä varten komento ajetaan. Samasta syystä
adapteri ei välimuistita ottelulistaa eikä komennossa ole ``--pakota``-lippua:
ei ole mitään pakotettavaa, kun mitään ei koskaan ohiteta.

Miksi ``status`` on aina ``ok``
-------------------------------
Tyhjä divisioona ei ole tämän vaiheen epäonnistuminen: haku onnistui, ja tulos
oli tyhjä. ``UnitStatus``in arvot (AD-9) kuvaavat **demoyksikön** kohtaloa
(``no_demo``, ``parse_failed``, ...), eikä yksikään niistä tarkoita "kilpailussa
ei ollut otteluita" -- ja uuden arvon lisääminen laajentaisi ``CLASSIFIED``in
polars-enumia, eli muuttaisi arkistossa jo olevien parquet-tiedostojen
skeemasopimusta. Tyhjä tulos kerrotaan siksi :attr:`StageResult.reason`issa, ja
komento nostaa sen tulosteensa kärkeen. Hiljaiseksi se ei jää.

Mitä tämä vaihe **ei** tee
--------------------------
Ei lataa demoja, ei valitse otteluita rosterikynnyksellä (Story 3.3), ei
kirjoita ``index/selections/``- eikä ``index/next_opponent/``-tiedostoja (Epic
4). **Eikä nimeä arkiston hakemistoja uudelleen**: ``aggregates/<team_key>`` ja
``classified/<team_key>`` säilyvät sellaisinaan, ja yhteys niihin kulkee
``index/teams.json``in ``lineup_keys``-kentän kautta. Arkiston nimeämispäätös on
Story 3.4, ja se tehdään havainnolla eikä ennakolta.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import polars as pl

from pappascout.adapters.protocols import Match, MatchSource, MatchTeam, RosterPlayer
from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.paths import (
    ArchivePaths,
    matches_index,
    parsed_root,
    parsed_table,
    teams_index,
)
from pappascout.domain.models import LeagueSettings, Settings, ThresholdSettings
from pappascout.domain.teams import (
    RosterMember,
    Team,
    TeamLookup,
    TeamObservation,
    assign_lineup_keys,
    build_teams,
    find_teams,
    is_steam_id64,
)
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult

__all__ = [
    "STAGE",
    "SCHEMA_VERSION",
    "PLAYED_STATUSES",
    "run",
    "default_source",
    "read_matches_index",
    "read_teams_index",
    "read_indexes",
]

STAGE = "discover"

#: Indeksitiedostojen muodon versio. Lukija tarkistaa sen
#: (:func:`read_indexes`) sen sijaan että päättelisi muodon kenttien
#: olemassaolosta.
SCHEMA_VERSION = 1

#: Tilat, joissa ottelu on pelattu. Sama joukko kuin adapterin
#: ``CACHEABLE_MATCH_STATUSES``illa, mutta **eri päätös**: siellä kysytään
#: "saako vastauksen tallentaa ikuisesti", täällä "onko tämä ottelu pelattu".
#: Yhteinen vakio sitoisi kaksi eri kysymystä toisiinsa.
PLAYED_STATUSES = frozenset({"FINISHED"})


def run(
    league: LeagueSettings,
    archive: ArchivePaths,
    team: str | None,
    *,
    source: MatchSource,
    thresholds: ThresholdSettings,
) -> StageResult:
    """Hae divisioonan ottelut ja kirjoita otteluindeksi ja joukkueindeksi.

    Args:
        league: ``[league]``-osio; siitä luetaan ``championship_ids``.
        archive: Arkiston polut.
        team: Joukkueen nimi, sen osa tai tunniste. ``None`` on kelvollinen:
            silloin indeksit kirjoitetaan ja yhteenveto listaa koko divisioonan.
        source: Otteluportti, **avainsanaparametrina**. Testissä feikki, ajossa
            :func:`default_source`.
        thresholds: ``[thresholds]``-osio, avainsanaparametrina. Siitä luetaan
            ``team_identity_min_common``, jolla sekä lähdetunnisteet liitetään
            samaksi joukkueeksi että arkiston kokoonpanotiivisteet liitetään
            joukkueisiin. Kaksi pydantic-osiota peräkkäin menisi
            positionaalisesti vaihtaen läpi ilman että mikään huomauttaisi --
            sama syy kuin ``aggregate``in ``aggregate_settings``issa.

    Returns:
        :class:`~pappascout.stages.StageResult`. ``stats`` kertoo otteluiden ja
        joukkueiden määrän, koko divisioonan luettelon, pudotetut pelaajat ja --
        jos ``team`` annettiin -- sen joukkueen tiedot.

    Raises:
        ~pappascout.errors.PappascoutError: Jos nimi ei täsmää yhteenkään
            joukkueeseen tai täsmää useampaan. **Indeksit on siinäkin
            tapauksessa jo kirjoitettu**: haku on näkymä hakutulokseen, ei ehto
            sille. Viesti listaa vaihtoehdot tunnisteineen ja pyytää valinnan;
            hiljaista valintaa ei tehdä.
        ~pappascout.errors.ApiError: Jos otteluita ei saatu haettua.
    """
    started = time.perf_counter()
    generated_at = datetime.now(UTC)

    matches = _fetch(source, league.championship_ids)
    observations, dropped = _observe(matches)
    built = build_teams(
        observations, min_common=thresholds.team_identity_min_common
    )
    teams, contested = assign_lineup_keys(
        built, _archive_lineups(archive), thresholds.team_identity_min_common
    )

    matches_rel = matches_index()
    teams_rel = teams_index()
    _write_pair(
        archive,
        matches_document=_matches_document(
            matches, league.championship_ids, generated_at
        ),
        teams_document=_teams_document(
            teams, contested, league.championship_ids, generated_at
        ),
    )
    outputs = (matches_rel, teams_rel)

    stats = _stats(matches, teams, contested, dropped, league, generated_at)

    unit = _unit(league)
    if team is not None:
        found = _resolve(teams, team)
        stats["team"] = _team_stats(found)
        unit = found.team_key

    return StageResult(
        stage=STAGE,
        unit=unit,
        status="ok",
        # Ei koskaan ohitusta: ottelulista muuttuu joka päivä, ja koko vaiheen
        # tarkoitus on nähdä muutos. Ks. moduulin docstring.
        skipped=False,
        outputs=outputs,
        manifest_path=None,
        reason=_reason(matches, teams, dropped),
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def default_source(settings: Settings, archive: ArchivePaths) -> MatchSource:
    """Tuotannon FACEIT-toteutus otteluportille.

    Tuonti on funktion sisällä, jotta tämän moduulin tuominen ei lataa
    ``requests``ia eikä koko adapteria -- vaihe itse tuntee vain portin, ja
    testit antavat sille feikin. Sama kuvio kuin
    ``stages.parse.default_parser``illa.

    **Sääntö koskee adapteria, ei riippuvuuksien painoa.** ``polars`` tuodaan
    tämän moduulin alussa, koska se on vaihekerroksen oma työkalu (samoin
    ``stages.aggregate``issa); ``requests`` ja ``FaceitClient`` ovat portin
    toisella puolella, ja juuri se raja pidetään lataamattomana.

    Tämä on myös se kohta, jonka kautta ``cli`` saa portin **koskematta
    adaptereihin**: riippuvuusnuoli on ``cli -> stages -> adapters``.
    """
    from pappascout.adapters.faceit import FaceitClient

    return FaceitClient.from_settings(settings, archive.raw_faceit())


# -- Haku ja havainnot -------------------------------------------------------


def _fetch(source: MatchSource, competition_ids: Sequence[str]) -> tuple[Match, ...]:
    """Hae kaikkien kilpailujen ottelut yhdeksi listaksi.

    Sama ottelu voi periaatteessa kuulua kahteen kilpailuun; ``match_id``
    deduplikoi, jottei se laskeutuisi indeksiin kahdesti. Järjestys on
    aikataulun mukainen, jotta tiedosto on luettava ja kahden ajon ero on
    diffattavissa.
    """
    seen: dict[str, Match] = {}
    for competition_id in competition_ids:
        for match in source.get_matches(competition_id):
            seen.setdefault(match.match_id, match)
    return tuple(sorted(seen.values(), key=_match_order))


def _match_order(match: Match) -> tuple[int, float, str]:
    moment = _moment_of(match)
    if moment is None:
        return (1, 0.0, match.match_id)
    return (0, moment.timestamp(), match.match_id)


def _moment_of(match: Match) -> datetime | None:
    """Ottelun hetki: aikataulu ensin, todellinen alku vasta sen puuttuessa.

    Mitattu 2026-09-04: ottelulistalla on ``scheduled_at`` eikä ``started_at``,
    joten aikataulu on ainoa hetki, joka pelaamattomalla ottelulla on.
    """
    return match.scheduled_at or match.started_at


class _Dropped:
    """Se, mitä havainnoista jäi pois -- lukumäärinä ja tunnistettavina riveinä.

    **Erilliset pelaajat, ei esiintymät.** Sama tunnisteeton pelaaja on
    divisioonan jokaisella ottelurivillä, joten esiintymien laskeminen sanoisi
    "11 pelaajaa jäi pois" yhdestä pelaajasta. Nimimerkki ja ottelu ovat
    tallessa, jotta käyttäjä voi tarkistaa keneltä tunniste puuttui -- pelkkä
    luku olisi väite ilman tarkistusmahdollisuutta.
    """

    def __init__(self) -> None:
        self.players: dict[str, dict[str, str | None]] = {}
        self.team_rows = 0

    def player(
        self, player: RosterPlayer, team_name: str | None, match_id: str
    ) -> None:
        key = player.player_id or f"{team_name}/{player.nickname}"
        self.players.setdefault(
            key,
            {
                "player_id": player.player_id,
                "nickname": player.nickname,
                "team": team_name,
                "match_id": match_id,
            },
        )

    @property
    def player_count(self) -> int:
        return len(self.players)

    def rows(self) -> list[dict[str, str | None]]:
        return sorted(
            self.players.values(),
            key=lambda row: (str(row.get("nickname") or ""), str(row.get("player_id"))),
        )


def _observe(matches: Iterable[Match]) -> tuple[list[TeamObservation], _Dropped]:
    """Muunna ottelut domainin havainnoiksi.

    Tässä lähteen sanasto loppuu: ``domain.teams`` ei näe :class:`Match`ia
    lainkaan, joten sen säännöt ovat testattavissa käsin rakennetuilla
    havainnoilla.

    Kaksi pudotusta, ja **molemmat lasketaan**:

    * **Osapuoli ilman tunnistetta** ei ole joukkue, johon mitään voisi liittää.
    * **Pelaaja ilman SteamID64:ää** ei ole liitettävissä demoihin, ja
      vakirosteri on nimenomaan se joukko, joka niihin liittyy.

    Kummastakin kerrotaan ajon yhteenvedossa. Aiemmin joukkuerivin pudotus oli
    hiljainen ja pelaajan ei -- epäsymmetria, jonka katselmus löysi.
    """
    observations: list[TeamObservation] = []
    dropped = _Dropped()
    for match in matches:
        played = _is_played(match)
        moment = _moment_of(match)
        for side in match.teams:
            if side.team_id is None:
                dropped.team_rows += 1
                continue
            roster = _members(side.roster, side.name, match.match_id, dropped)
            substitutes = _members(
                side.substitutes, side.name, match.match_id, dropped
            )
            observations.append(
                TeamObservation(
                    faction_id=side.team_id,
                    match_id=match.match_id,
                    observed_at=moment,
                    name=side.name,
                    played=played,
                    roster=roster,
                    substitutes=substitutes,
                )
            )
    return observations, dropped


def _members(
    players: Iterable[RosterPlayer],
    team_name: str | None,
    match_id: str,
    dropped: _Dropped,
) -> tuple[RosterMember, ...]:
    """Portin pelaajat domainin rosterijäseniksi; pudotetut kirjataan."""
    members: list[RosterMember] = []
    for player in players:
        steam_id = player.game_player_id
        if steam_id is None or not is_steam_id64(steam_id):
            dropped.player(player, team_name, match_id)
            continue
        members.append(
            RosterMember(
                game_player_id=steam_id,
                nickname=player.nickname,
                player_id=player.player_id,
            )
        )
    return tuple(members)


def _is_played(match: Match) -> bool:
    """Onko ottelu pelattu? Tuntematon tila ei ole pelattu."""
    return match.status is not None and match.status.upper() in PLAYED_STATUSES


# -- Silta arkistoon ---------------------------------------------------------


def _archive_lineups(archive: ArchivePaths) -> dict[str, set[str]]:
    """Arkiston kokoonpanot: ``lineup_key`` -> pelaajien SteamID64-joukko.

    Lähde on ``lineups.parquet`` samoin kuin ``aggregate``illa: sen pelaajajoukko
    on **täsmälleen se**, josta ``lineup_key`` on laskettu. Lukukelvoton tai
    puuttuva taulu ohitetaan -- silta on lisätietoa, eikä puuttuva silta ole syy
    jättää indeksi kirjoittamatta.
    """
    root = archive.parsed_root()
    if not root.is_dir():
        return {}
    lineups: dict[str, set[str]] = {}
    for directory in sorted(root.iterdir()):
        if not directory.is_dir():
            continue
        _read_lineups(archive, directory.name, lineups)
    return lineups


def _read_lineups(
    archive: ArchivePaths, map_demo_id: str, into: dict[str, set[str]]
) -> None:
    try:
        path = archive.resolve(parsed_table(map_demo_id, "lineups"))
    except PappascoutError:
        # Hakemisto, jonka nimi ei kelpaa tunnisteeksi, ei ole parsittu demo.
        return
    if not path.is_file():
        return
    try:
        frame = pl.read_parquet(path, columns=["lineup_key", "player_id"])
    except (OSError, pl.exceptions.PolarsError):
        return
    for row in frame.unique().iter_rows(named=True):
        into.setdefault(str(row["lineup_key"]), set()).add(str(row["player_id"]))


# -- Nimihaku ----------------------------------------------------------------


def _resolve(teams: Sequence[Team], query: str) -> Team:
    """Tulkitse ``--team`` joukkueeksi, tai kerro miksi se ei onnistu.

    Raises:
        PappascoutError: Kun osumia on nolla tai monta. Viesti listaa
            vaihtoehdot **tunnisteineen** -- tunniste on ainoa tapa erottaa
            kaksi samannimistä joukkuetta toisistaan -- ja pyytää valinnan.
            Ensimmäisen osuman ottaminen olisi hiljainen valinta, ja juuri se
            on kielletty.
    """
    lookup = find_teams(teams, query)
    if lookup.is_unique:
        return lookup.team
    raise PappascoutError(_lookup_problem(lookup, teams))


def _lookup_problem(lookup: TeamLookup, teams: Sequence[Team]) -> str:
    """Suomenkielinen selitys sille, miksi haku ei tuottanut yhtä joukkuetta."""
    if lookup.is_ambiguous:
        return (
            f"Haku {lookup.query!r} osuu {len(lookup.teams)} joukkueeseen, "
            "joten valinta on tehtävä:\n"
            + _listing(lookup.teams)
            + "\nTarkenna hakua niin, että se osuu yhteen -- esimerkiksi:\n"
            + f"    --team {_unambiguous_query(lookup.teams)}"
        )
    if not teams:
        return (
            f"Haku {lookup.query!r} ei osu yhteenkään joukkueeseen, koska "
            "divisioonasta ei löytynyt otteluita.\n"
            "Tarkista [league].championship_ids asetuksista."
        )
    return (
        f"Haku {lookup.query!r} ei osu yhteenkään divisioonan joukkueeseen.\n"
        "Divisioonan joukkueet ovat:\n" + _listing(teams)
    )


def _listing(teams: Sequence[Team]) -> str:
    """Joukkueet nimineen, rosterikokoineen ja **tunnisteineen**.

    Tunniste on mukana, koska ilman sitä kahden samannimisen joukkueen
    luettelo olisi kaksi identtistä riviä eikä valintaa voisi tehdä millään.
    """
    return "\n".join(
        f"    {team.display_name} ({len(team.roster)} pelaajaa, "
        f"tunniste {team.team_key})"
        for team in teams
    )


def _unambiguous_query(teams: Sequence[Team]) -> str:
    """Hakuehdotus, joka osuu tasan yhteen näistä joukkueista.

    Nimi kelpaa vain, jos se on osumien joukossa yksikäsitteinen; muuten
    ehdotetaan tunnistetta. Ilman tätä ehdotus olisi samannimisten joukkueiden
    tapauksessa täsmälleen se haku, joka juuri epäonnistui.
    """
    first = teams[0]
    names = [team.display_name for team in teams]
    if first.name and names.count(first.display_name) == 1:
        return f'"{first.display_name}"'
    return first.team_key


# -- Ajon yhteenvedon luvut --------------------------------------------------


def _unit(league: LeagueSettings) -> str:
    """Yksikkö silloin kun joukkuetta ei haettu: **yksi tunniste, ei luettelo**.

    ``StageResult.unit`` on muualla putkessa aina yksi tunniste
    (``map_demo_id``, ``team_key``), ja pilkuilla yhdistetty lista lukisi
    tulosteessa tunnisteelta olematta sellainen. Koko luettelo on
    ``stats["competition_ids"]``issä.
    """
    return league.championship_ids[0]


def _reason(
    matches: Sequence[Match], teams: Sequence[Team], dropped: _Dropped
) -> str | None:
    """Suomenkielinen selitys tyhjälle tai vajaalle tulokselle, tai ``None``.

    Haku onnistui, joten ``status`` on ``ok`` -- mutta "0 joukkuetta, 0
    ottelua" ilman sanaakaan siitä, mistä se johtuu, jättäisi käyttäjän
    arvaamaan. Ks. moduulin docstring siitä, miksei tähän ole omaa tilaa.
    """
    if not matches:
        return (
            "Kilpailusta ei löytynyt yhtään ottelua. Tarkista "
            "[league].championship_ids asetuksista -- indeksit kirjoitettiin "
            "tyhjinä."
        )
    if not teams:
        return (
            "Otteluita löytyi, mutta yhdelläkään ei ollut tunnistettavaa "
            "joukkuetta. Joukkueindeksi jäi tyhjäksi."
        )
    empty = [team.display_name for team in teams if not team.roster]
    if empty:
        return (
            "Näiltä joukkueilta ei saatu yhtään SteamID64-tunnistettua "
            "pelaajaa, joten niiden rosteri on tyhjä: " + ", ".join(empty)
        )
    if dropped.player_count:
        return (
            f"{dropped.player_count} pelaajaa jäi pois rostereista, koska "
            "heillä ei ollut SteamID64-tunnistetta."
        )
    return None


def _stats(
    matches: Sequence[Match],
    teams: Sequence[Team],
    contested: Sequence[str],
    dropped: _Dropped,
    league: LeagueSettings,
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "competition_ids": list(league.championship_ids),
        "matches": len(matches),
        "matches_played": sum(1 for m in matches if _is_played(m)),
        "teams": len(teams),
        "roster_min": min((len(t.roster) for t in teams), default=0),
        "roster_max": max((len(t.roster) for t in teams), default=0),
        "teams_without_roster": sum(1 for t in teams if not t.roster),
        "players_without_steam_id": dropped.player_count,
        "dropped_players": dropped.rows(),
        "team_rows_without_id": dropped.team_rows,
        "contested_lineup_keys": list(contested),
        "transfers": _transfers(teams),
        # Koko divisioona luettelona, jotta nimet saa näkyviin **ilman
        # virhettä**: monitulkintaisen haun jälkeen juuri tämä on se, mitä
        # käyttäjä tarvitsee seuraavaksi.
        "division": [
            {
                "team_key": team.team_key,
                "name": team.name,
                "roster_size": len(team.roster),
                "matches_played": team.matches_played,
            }
            for team in teams
        ],
        "generated_at": generated_at.isoformat(),
    }


def _transfers(teams: Sequence[Team]) -> list[dict[str, Any]]:
    """Pelaajat, jotka havaittiin useammassa kuin yhdessä joukkueessa.

    Sekä siirtyneet (``released``) että kiistanalaiset (``shared_players``) --
    molemmat ovat tapauksia, joissa rosteri ei ole pelkkä yhdiste, ja
    molemmat kuuluvat ajon yhteenvetoon eivätkä pelkästään tiedostoon.
    """
    rows: list[dict[str, Any]] = []
    for team in teams:
        for member in team.released:
            rows.append(
                {
                    "game_player_id": member.game_player_id,
                    "nickname": member.nickname,
                    "from_team": team.display_name,
                    "kind": "released",
                }
            )
        for player in team.shared_players:
            rows.append(
                {
                    "game_player_id": player,
                    "nickname": next(
                        (
                            m.nickname
                            for m in team.roster
                            if m.game_player_id == player
                        ),
                        None,
                    ),
                    "from_team": team.display_name,
                    "kind": "shared",
                }
            )
    return rows


def _team_stats(team: Team) -> dict[str, Any]:
    return {
        "team_key": team.team_key,
        "faction_ids": list(team.faction_ids),
        "name": team.name,
        "alternative_names": list(team.alternative_names),
        "roster": [member.display_name for member in team.roster],
        "roster_size": len(team.roster),
        "released": [member.display_name for member in team.released],
        "shared_players": list(team.shared_players),
        "matches": len(team.match_ids),
        "matches_played": team.matches_played,
        "lineup_keys": list(team.lineup_keys),
    }


# -- Indeksitiedostot --------------------------------------------------------


def _write_pair(
    archive: ArchivePaths,
    *,
    matches_document: dict[str, Any],
    teams_document: dict[str, Any],
) -> None:
    """Kirjoita molemmat indeksit niin, ettei toinen jää ilman toista.

    **Pari ei ole atominen, mutta se on niin lähellä kuin tiedostojärjestelmällä
    pääsee.** Molemmat sarjallistetaan ja kirjoitetaan väliaikaistiedostoihin
    ensin, ja vasta kun molemmat ovat levyllä ehjinä, ne vaihdetaan paikoilleen
    peräkkäin. Sarjallistusvirhe ei siis voi jättää arkistoon uutta
    ottelulistaa ja vanhaa joukkueindeksiä.

    Jäljelle jää kahden ``os.replace``in väli. Sitä varten molemmissa on sama
    ``generated_at``, ja :func:`read_indexes` vertaa niitä -- eli lukija
    huomaa parittoman parin sen sijaan että liittäisi ne hiljaa yhteen.
    """
    matches_abs = archive.resolve(matches_index())
    teams_abs = archive.resolve(teams_index())
    with atomic_path(matches_abs) as matches_tmp:
        _dump(matches_tmp, matches_document)
        with atomic_path(teams_abs) as teams_tmp:
            _dump(teams_tmp, teams_document)


def _dump(path: Path, document: dict[str, Any]) -> None:
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _matches_document(
    matches: Sequence[Match], competition_ids: Sequence[str], generated_at: datetime
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "competition_ids": list(competition_ids),
        "matches": [_match_row(match) for match in matches],
    }


def _match_row(match: Match) -> dict[str, Any]:
    return {
        "match_id": match.match_id,
        "competition_id": match.competition_id,
        "status": match.status,
        "played": _is_played(match),
        "scheduled_at": _moment(match.scheduled_at),
        "started_at": _moment(match.started_at),
        "finished_at": _moment(match.finished_at),
        "map_picks": list(match.map_picks),
        "teams": [_match_team_row(side) for side in match.teams],
    }


def _match_team_row(side: MatchTeam) -> dict[str, Any]:
    return {
        "faction_id": side.team_id,
        "name": side.name,
        # Rosteri **ei** ole otteluindeksissä nimineen: se on joukkueindeksin
        # asia, ja sama luettelo kahdessa tiedostossa olisi kaksi eri totuutta
        # heti kun toinen kirjoitetaan uudelleen. Ottelurivi kertoo, ketkä
        # olivat tässä ottelussa, tunnisteina.
        "roster": [p.game_player_id for p in side.roster if p.game_player_id],
        "substitutes": [p.game_player_id for p in side.substitutes if p.game_player_id],
    }


def _teams_document(
    teams: Sequence[Team],
    contested: Sequence[str],
    competition_ids: Sequence[str],
    generated_at: datetime,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "competition_ids": list(competition_ids),
        # Kokoonpanotiivisteet, jotka useampi joukkue omistaa. Ilman tätä
        # listaa jatkovaihe laskisi ne kahdesti tietämättä tekevänsä niin.
        "contested_lineup_keys": list(contested),
        "teams": [_team_row(team) for team in teams],
    }


def _team_row(team: Team) -> dict[str, Any]:
    return {
        "team_key": team.team_key,
        # Kaikki lähteen tunnisteet, jotka ovat tätä joukkuetta. Identiteetti
        # on rosteri; nämä ovat avaimia, joilla lähde sen tunsi.
        "faction_ids": list(team.faction_ids),
        "name": team.name,
        "alternative_names": list(team.alternative_names),
        # Yhteys arkistoon, ei identiteetti. Arkiston hakemistot on nimetty
        # kokoonpanotiivisteestä, ja tämä kenttä tekee siitä luettavan --
        # uudelleennimeäminen on Story 3.4.
        "lineup_keys": list(team.lineup_keys),
        # Tunnistelistat, eivät lukumääriä -- nimi sanoo sen, jottei lukija
        # sekoittaisi niitä ajon yhteenvedon samannimisiin lukuihin.
        "match_ids": list(team.match_ids),
        "played_match_ids": list(team.played_match_ids),
        "roster_size": len(team.roster),
        "roster": [_player_row(member) for member in team.roster],
        # Pelaajat, jotka havaittiin tässä joukkueessa mutta myöhemmin
        # toisessa. Eivät rosterissa, mutta eivät myöskään kadonneet.
        "released": [_player_row(member) for member in team.released],
        # Pelaajat, jotka toinen joukkue havaitsi yhtä myöhään. Yhä
        # rosterissa; kiistaa ei ratkaista arpomalla.
        "shared_players": list(team.shared_players),
    }


def _player_row(member: RosterMember) -> dict[str, Any]:
    return {
        "game_player_id": member.game_player_id,
        "nickname": member.nickname,
        "player_id": member.player_id,
        "alternative_nicknames": list(member.alternative_nicknames),
    }


def _moment(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


# -- Lukija ------------------------------------------------------------------


def read_matches_index(archive: ArchivePaths) -> dict[str, Any]:
    """Lue ``index/matches.json``. Ks. :func:`read_indexes`."""
    return _read(archive.matches_index(), "otteluindeksi")


def read_teams_index(archive: ArchivePaths) -> dict[str, Any]:
    """Lue ``index/teams.json``. Ks. :func:`read_indexes`."""
    return _read(archive.teams_index(), "joukkueindeksi")


def read_indexes(archive: ArchivePaths) -> tuple[dict[str, Any], dict[str, Any]]:
    """Lue molemmat indeksit ja tarkista, että ne ovat **samasta ajosta**.

    Lukija on täällä eikä jokaisessa jatkovaiheessa, koska muuten
    ``schema_version`` olisi kirjoitettu muttei luettu -- ja jokainen vaihe
    tulkitsisi muodon omalla tavallaan.

    Returns:
        ``(ottelut, joukkueet)`` sanakirjoina.

    Raises:
        PappascoutError: Jos tiedostoa ei ole, se ei ole kelvollista JSONia, sen
            ``schema_version`` on tuntematon tai tiedostojen ``generated_at``
            eroaa. Viimeinen tarkoittaa, että kirjoitus keskeytyi tiedostojen
            välissä; silloin niitä ei saa liittää yhteen, vaan ``discover`` on
            ajettava uudelleen.
    """
    matches = read_matches_index(archive)
    teams = read_teams_index(archive)
    if matches.get("generated_at") != teams.get("generated_at"):
        raise PappascoutError(
            "Otteluindeksi ja joukkueindeksi ovat eri ajoista "
            f"({matches.get('generated_at')} ja {teams.get('generated_at')}), "
            "joten niitä ei voi liittää yhteen.\n"
            "Aja uudelleen: uv run pappascout discover"
        )
    return matches, teams


def _read(path: Path, what: str) -> dict[str, Any]:
    if not path.is_file():
        raise PappascoutError(
            f"Arkistosta puuttuu {what} ({path.name}).\n"
            "Aja ensin: uv run pappascout discover"
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PappascoutError(
            f"Arkiston {what} ({path.name}) ei ole luettavissa: {exc}\n"
            "Aja uudelleen: uv run pappascout discover"
        ) from exc
    if not isinstance(document, dict):
        raise PappascoutError(
            f"Arkiston {what} ({path.name}) ei ole odotetun muotoinen olio."
        )
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PappascoutError(
            f"Arkiston {what} on muotoa {version!r}, mutta tämä versio osaa "
            f"lukea vain muotoa {SCHEMA_VERSION}.\n"
            "Aja uudelleen: uv run pappascout discover"
        )
    return document
