"""``select`` -- putken alkupään toinen vaihe: mitkä kartat kuuluvat otantaan.

``discover`` tietää, keitä joukkueeseen kuuluu. Se ei tiedä, **missä otteluissa
he pelasivat joukkueena**. Ilman sitä tietoa otantaan päätyisi yhtä lailla
vastustajan soolopelit satunnaisten pelaajien kanssa, ja raportti kuvaisi jotain
muuta kuin sitä joukkuetta, jota vastaan pelaat. Tämä vaihe kirjoittaa sen
päätöksen tiedostoksi::

    index/selections/<team_key>.json

Rivi on **per MapDemo, ei per ottelu**, ja se kertoo neljä asiaa: kelpaako kartta
otantaan (``roster_ok``), miksi (``roster_reason``), mikä rosteriluokka
(``roster_class``) ja onko kyseessä liigaottelu (``is_league``). Tiedostolla on
**yksi kirjoittaja, ja se on tämä vaihe**; ``discover``in kaksi indeksiä ovat
tälle pelkkää luettavaa, ja ne luetaan sen omilla lukijoilla
(:func:`~pappascout.stages.discover.matches_from_index`,
:func:`~pappascout.stages.discover.teams_from_index`) eikä JSONia käsin purkaen.

Miksi kynnys arvioidaan karttakohtaisesti
-----------------------------------------
Pappaliiga sallii kaksi vaihtoa **karttojen välissä**, joten sama ottelu voi olla
kartalla 1 täysi vakikokoonpano ja kartalla 2 neljä vakipelaajaa ja yksi
ulkopuolinen. Otteluttain arvioitu kynnys joko hyväksyisi tai hylkäisi molemmat,
ja kumpikin olisi väärin toisen kartan osalta.

Miksi luokka on ennuste ennen parsintaa ja havainto sen jälkeen
--------------------------------------------------------------
FACEITin rosteri on **ottelukohtainen** (mitattu 2026-09-04,
``mittaus-faceit-aineisto.md`` luku 7), joten ennen demoa kartan kokoonpano on
paras arvaus ottelurosterista. Demon jälkeen se on havainto. Rivi sanoo kummasta
on kyse (``roster_source``), täsmälleen kuten ``map_name_source`` Story 2.11:ssä
-- ja kun molemmat ovat tiedossa, **havainto voittaa ja ero kerrotaan**.

Kolme eri syytä sille, ettei riviä ole -- eikä yksi
--------------------------------------------------
Tämä on se kohta, jossa yhteenveto helpoiten valehtelee, joten syyt lasketaan
erikseen ja kerrotaan erikseen:

**Ottelua ei ole pelattu.**
    Mitattu 2026-09-04: ``map_picks`` on tyhjä 60/66 ottelussa eli täsmälleen
    niissä, joita ei ole pelattu. Karttoja ei ole, joten MapDemoja ei ole, ja
    tunniste ``{match_id}-0`` osoittaisi tiedostoon, jota ei voi olla olemassa.
    Ajastettu ottelu ei ole "valinta odottaa" vaan "ei vielä olemassa".

**Ottelu on pelattu, mutta vetotieto puuttuu.**
    Portin oma sopimus sanoo, että tyhjä ``map_picks`` tarkoittaa "ei
    vetotietoa", **ei** "ei karttoja". Pelattu ottelu ilman vetotietoa on siis
    puuttuvaa dataa eikä pelaamaton ottelu -- ja sen niputtaminen edellisen
    kanssa kertoisi käyttäjälle syyn, joka ei ole tosi.

**Joukkue ei ole ottelussa.**
    Ei mainita: kyseessä on koko divisioonan ottelulista, ja 55 ottelua 66:sta
    on aina jonkun muun.

Miksi kartta voi jäädä otannan ulkopuolelle vaikka se on vetotiedossa
--------------------------------------------------------------------
Kolmen kartan ottelu päättyy usein kahteen, mutta vedossa on silti kolme nimeä.
:func:`~pappascout.domain.selection.guaranteed_maps` kertoo, monesko kartta on
vielä varma; sen jälkeiset saavat rivin muttei paikkaa otannassa, ennen kuin
demo todistaa ne pelatuiksi. Nykyisellä runkosarjalla (``best_of`` = 2) yksikään
rivi ei jää epävarmaksi, mutta playoffit ovat BO3.

Miksi vaiheella ei ole manifestia
---------------------------------
Samasta syystä kuin ``discover``illa: syöte on ottelulista, joka muuttuu joka
päivä, ja tulos muuttuu myös silloin kun demo parsitaan ja ennuste vaihtuu
havainnoksi. Ohitus säästäisi tiedoston lukemisen ja maksaisi juuri sen, mitä
varten komento ajetaan.

Mitä tämä vaihe **ei** tee
--------------------------
Se ei lataa demoja (Story 3.4), ei kirjoita ``aggregates/``- eikä
``classified/``-hakemistoihin, eikä koske vetoon, baniin tai pickiin (Epic 4).
Se **ei myöskään johdota** ``is_league``- ja ``roster_class``-arvoja
``classify``-vaiheeseen: se muuttaisi arkiston luokittelun ja raporttien tekstin
ja on oma tarinansa.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl

from pappascout.archive.atomic_write import atomic_path
from pappascout.archive.paths import ArchivePaths, parsed_table, selection
from pappascout.domain.models import LeagueSettings, ThresholdSettings
from pappascout.domain.selection import (
    MapCandidate,
    MapSelection,
    counts,
    guaranteed_maps,
    map_demo_id,
    select_maps,
    sort_key,
)
from pappascout.domain.teams import Team
from pappascout.errors import PappascoutError
from pappascout.stages import StageResult
from pappascout.stages.discover import (
    IndexedMatch,
    IndexedMatchTeam,
    matches_from_index,
    read_indexes,
    resolve_team,
    teams_from_index,
)

__all__ = [
    "STAGE",
    "SCHEMA_VERSION",
    "MAX_LISTED_REJECTIONS",
    "run",
    "read_selection",
]

STAGE = "select"

#: Valintatiedoston muodon versio. Oma versionsa eikä ``discover``in, koska
#: tiedostolla on eri kirjoittaja ja eri elinkaari: otteluindeksin muoto voi
#: muuttua ilman että valintarivin muoto muuttuu, ja päinvastoin.
SCHEMA_VERSION = 1

#: Montako hylättyä karttaa syineen kulkee ``StageResult.stats``issa.
#:
#: ``stats`` on **yhteenveto eikä hyötykuorma**: se päätyy sellaisenaan
#: komennon tulosteeseen, ja rajaton luettelo tuottaisi divisioonan kokoisella
#: otannalla ruudullisen tekstiä. Koko luettelo on aina valintatiedostossa, ja
#: tuloste sanoo montako jäi näyttämättä.
MAX_LISTED_REJECTIONS = 10

#: Montako vetotiedotonta ottelua nimetään huomiossa tunnisteella.
MAX_LISTED_MATCHES = 3


@dataclass
class _Skipped:
    """Joukkueen ottelut, joista ei syntynyt riviä -- **syy kerrallaan**.

    Yksi yhteinen laskuri kertoisi montako katosi muttei miksi, ja yhteenveto
    joutuisi arvaamaan syyn. Juuri se arvaus oli ensimmäisen version virhe:
    pelattu ottelu ilman vetotietoa selitettiin pelaamattomaksi.
    """

    #: Ottelu on aikataulussa muttei pelattu: karttoja ei ole olemassa.
    not_played: int = 0
    #: Pelatut ottelut, joiden vetotieto puuttuu. Kartat pelattiin, mutta emme
    #: tiedä mitkä -- **puuttuvaa dataa**, ei pelaamaton ottelu. Tunnisteet
    #: eivätkä lukumäärä, jotta huomio voi nimetä ne.
    no_veto: list[str] = field(default_factory=list)


def run(
    league: LeagueSettings,
    archive: ArchivePaths,
    team: str,
    *,
    thresholds: ThresholdSettings,
) -> StageResult:
    """Valitse joukkueen MapDemot rosterikynnyksellä ja kirjoita valintatiedosto.

    Args:
        league: ``[league]``-osio; siitä luetaan ``championship_ids``, jota
            vasten ``is_league`` ratkeaa. **Nimestä ei päätellä mitään**:
            ``competition_name`` on ihmisen kirjoittama merkkijono.
        archive: Arkiston polut.
        team: Joukkueen nimi, sen yksikäsitteinen osa tai tunniste. Pakollinen:
            valintatiedosto on joukkuekohtainen, joten ilman joukkuetta ei ole
            tiedostoa jota kirjoittaa.
        thresholds: ``[thresholds]``-osio avainsanaparametrina; siitä luetaan
            ``roster_size`` ja ``roster_min_regulars``. Avainsanana samasta
            syystä kuin ``discover``issa: kaksi pydantic-osiota peräkkäin menisi
            positionaalisesti vaihtaen läpi ilman että mikään huomauttaisi.

    Returns:
        :class:`~pappascout.stages.StageResult`. ``stats`` kertoo rivien määrän,
        hyväksytyt ja hylätyt, luokkajakauman, kokoonpanon lähteen jakauman ja
        sen, montako ottelua jäi ilman rivejä **kummastakin syystä**.

    Raises:
        ~pappascout.errors.PappascoutError: Jos indeksejä ei ole (viesti kehottaa
            ajamaan ``discover``in), jos ne ovat eri ajoista, jos ne ovat rikki,
            tai jos nimi ei osu yhteen ainoaan joukkueeseen (viesti listaa
            tunnetut).
        ~pappascout.errors.SettingsError: Jos ``[thresholds]``-kynnyksistä ei
            synny tunnettua rosteriluokkaa.
    """
    started = time.perf_counter()
    generated_at = datetime.now(UTC)

    matches_document, teams_document = read_indexes(archive)
    matches = matches_from_index(matches_document)
    teams = teams_from_index(teams_document)
    found = resolve_team(teams, team)

    names = _nicknames(teams)
    candidates, skipped, veto_notes = _candidates(
        matches, found, league.championship_ids, archive
    )
    rows = tuple(
        sorted(
            select_maps(
                candidates,
                roster=found.player_ids,
                roster_size=thresholds.roster_size,
                roster_min_regulars=thresholds.roster_min_regulars,
                names=names,
            ),
            key=sort_key,
        )
    )

    relative = selection(found.team_key)
    _write(
        archive.resolve(relative),
        _document(
            rows,
            team=found,
            league=league,
            thresholds=thresholds,
            generated_at=generated_at,
            index_generated_at=matches_document.get("generated_at"),
        ),
    )

    notes = _notes(rows, found, skipped) + veto_notes
    stats = _stats(rows, found, thresholds, skipped, generated_at)
    stats["notes"] = notes

    return StageResult(
        stage=STAGE,
        unit=found.team_key,
        status="ok",
        # Ei koskaan ohitusta: ks. moduulin docstring.
        skipped=False,
        outputs=(relative,),
        manifest_path=None,
        # ``reason`` on sopimuksessa yksi merkkijono, mutta huomioita voi olla
        # monta. Ne ovat myös ``stats["notes"]``issa erillisinä, jotta komento
        # tulostaa jokaisen omalle rivilleen eikä yksikään katoa toisen alle.
        reason=" ".join(notes) if notes else None,
        duration_s=time.perf_counter() - started,
        stats=stats,
    )


def read_selection(archive: ArchivePaths, team_key: str) -> dict[str, Any]:
    """Lue joukkueen valintatiedosto ja tarkista sen muoto.

    Lukija on täällä eikä kutsujassa samasta syystä kuin
    :func:`~pappascout.stages.discover.read_indexes`: ``schema_version``
    kirjoitetaan, joten se on myös luettava -- muuten versio olisi kenttä, jota
    kukaan ei tarkista, ja jokainen kuluttaja (Story 3.4, ``aggregate``)
    päättelisi muodon kenttien olemassaolosta.

    Args:
        archive: Arkiston polut.
        team_key: Kanoninen joukkuetunniste.

    Returns:
        Valintatiedosto sanakirjana.

    Raises:
        PappascoutError: Jos tiedostoa ei ole, se ei ole kelvollista JSONia tai
            sen ``schema_version`` on tuntematon. Viesti kehottaa ajamaan
            ``select``in.
    """
    path = archive.selection(team_key)
    if not path.is_file():
        raise PappascoutError(
            f"Arkistosta puuttuu joukkueen {team_key} valintatiedosto "
            f"({path.name}).\n"
            f'Aja ensin: uv run pappascout select --team "{team_key}"'
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PappascoutError(
            f"Arkiston valintatiedosto ({path.name}) ei ole luettavissa: {exc}\n"
            f'Aja uudelleen: uv run pappascout select --team "{team_key}"'
        ) from exc
    if not isinstance(document, dict):
        raise PappascoutError(
            f"Arkiston valintatiedosto ({path.name}) ei ole odotetun muotoinen "
            "olio."
        )
    version = document.get("schema_version")
    if version != SCHEMA_VERSION:
        raise PappascoutError(
            f"Arkiston valintatiedosto on muotoa {version!r}, mutta tämä versio "
            f"osaa lukea vain muotoa {SCHEMA_VERSION}.\n"
            f'Aja uudelleen: uv run pappascout select --team "{team_key}"'
        )
    return document


# -- Ehdokkaiden kokoaminen --------------------------------------------------


def _candidates(
    matches: Sequence[IndexedMatch],
    team: Team,
    championship_ids: Sequence[str],
    archive: ArchivePaths,
) -> tuple[tuple[MapCandidate, ...], _Skipped, list[str]]:
    """Kokoa joukkueen MapDemot otteluindeksistä.

    Ottelu, jossa joukkue ei ole, ohitetaan laskematta: koko divisioonan
    ottelulistasta valtaosa on aina jonkun muun. Kaksi muuta ohitusta ovat
    joukkueen **omia** otteluita, ja ne lasketaan erikseen (:class:`_Skipped`).

    Returns:
        ``(ehdokkaat, ohitetut, huomiot)``. Huomiot koskevat vetotietoa, joka on
        ristiriidassa ottelun pituuden kanssa.
    """
    league_ids = frozenset(championship_ids)
    own = frozenset(team.faction_ids) | {team.team_key}

    candidates: list[MapCandidate] = []
    skipped = _Skipped()
    notes: list[str] = []

    for match in matches:
        side = _own_side(match, own)
        if side is None:
            continue
        if not match.played:
            skipped.not_played += 1
            continue
        if not match.map_picks:
            # Portin sopimus: tyhjä map_picks on "ei vetotietoa", ei "ei
            # karttoja". Pelattu ottelu ilman vetoa on puuttuvaa dataa, ja
            # sen selittäminen pelaamattomaksi olisi väärä väite.
            skipped.no_veto.append(match.match_id)
            continue

        certain = guaranteed_maps(match.best_of)
        if match.best_of is not None and len(match.map_picks) > match.best_of:
            notes.append(
                f"Ottelun {match.match_id} vetotiedossa on "
                f"{len(match.map_picks)} karttaa, vaikka ottelu on "
                f"BO{match.best_of}."
            )

        match_roster = frozenset(side.roster)
        is_league = match.competition_id in league_ids
        for index, map_name in enumerate(match.map_picks):
            unit = map_demo_id(match.match_id, index)
            players, note = _observed(archive, unit, team.player_ids)
            candidates.append(
                MapCandidate(
                    map_demo_id=unit,
                    match_id=match.match_id,
                    map_index=index,
                    map_name=map_name,
                    is_league=is_league,
                    certainly_played=certain is None or index < certain,
                    match_roster=match_roster,
                    observed_players=players,
                    observation_note=note,
                )
            )
    return tuple(candidates), skipped, notes


def _own_side(match: IndexedMatch, own: frozenset[str]) -> IndexedMatchTeam | None:
    """Ottelun se osapuoli, joka on tämä joukkue -- tai ``None``.

    Täsmäys tehdään **tunnisteella eikä nimellä**: nimi on havainto, joka voi
    vaihtua kesken kauden, ja kaksi samannimistä joukkuetta osuisi toisiinsa.
    Tunnisteita on monta, koska joukkueella voi olla useita lähdetunnisteita
    (uusi kausi tuo uuden) ja koska kanoninen ``team_key`` on yksi niistä.
    """
    for side in match.teams:
        if side.faction_id in own:
            return side
    return None


# -- Havainto demosta --------------------------------------------------------


def _observed(
    archive: ArchivePaths, unit: str, roster: frozenset[str]
) -> tuple[frozenset[str] | None, str | None]:
    """Kartan kokoonpano demosta, ja jos sitä ei ole, **miksi ei**.

    Lähde on ``parsed/<map_demo_id>/lineups.parquet`` samoin kuin
    ``discover``in sillalla arkistoon: sen pelaajajoukko on täsmälleen se,
    josta ``lineup_key`` on laskettu.

    **Taulussa on molemmat joukkueet**, joten oikea kokoonpano on valittava.
    Sääntö on yksi ja sama joukko-operaatio kuin kynnyskin: se kokoonpano,
    jolla on eniten yhteistä vakirosterin kanssa.

    Returns:
        ``(pelaajat, huomautus)``. Pelaajat on ``None``, kun havaintoa ei ole,
        ja huomautus kertoo silloin syyn -- **paitsi** kun demoa ei
        yksinkertaisesti ole parsittu, mikä on odotettu tila eikä poikkeama.
        Neljä muuta syytä ovat kaikki poikkeamia, ja jokainen niistä päätyy
        rivin syyhyn sen sijaan että demottaisi havainnon ennusteeksi
        jäljettömästi:

        * taulu on olemassa muttei luettavissa,
        * taulussa ei ole yhtään kelvollista riviä,
        * yhdelläkään kokoonpanolla ei ole yhteistä pelaajaa vakirosterin
          kanssa (demo on, mutta tätä joukkuetta ei siinä ole), tai
        * kaksi kokoonpanoa on yhtä lähellä -- arpominen liittäisi
          vastustajan kokoonpanon tähän joukkueeseen.
    """
    try:
        path = archive.resolve(parsed_table(unit, "lineups"))
    except PappascoutError:
        return None, None
    if not path.is_file():
        return None, None
    try:
        frame = pl.read_parquet(path, columns=["lineup_key", "player_id"]).unique()
    except (OSError, pl.exceptions.PolarsError) as exc:
        return None, (
            "Huom: demon kokoonpanotaulu on olemassa muttei luettavissa "
            f"({type(exc).__name__}), joten luokka jäi ennusteeksi. "
            f"Aja uudelleen: uv run pappascout parse {unit}"
        )

    groups: dict[str, set[str]] = {}
    for row in frame.iter_rows(named=True):
        key, player = row["lineup_key"], row["player_id"]
        # Null-arvo muuttuisi merkkijonoksi "None" ja sulauttaisi eri
        # kokoonpanot yhdeksi ryhmäksi -- eli tuottaisi kokoonpanon, jota
        # yhdessäkään demossa ei ollut.
        if key is None or player is None:
            continue
        groups.setdefault(str(key), set()).add(str(player))
    if not groups:
        return None, (
            "Huom: demon kokoonpanotaulussa ei ollut yhtään kelvollista riviä, "
            "joten luokka jäi ennusteeksi."
        )

    scored = sorted(
        ((len(players & roster), key) for key, players in groups.items()),
        reverse=True,
    )
    best, key = scored[0]
    if best == 0:
        return None, (
            "Huom: demo on parsittu, mutta yhdessäkään sen kokoonpanossa ei ole "
            "tämän joukkueen pelaajia, joten luokka jäi ennusteeksi."
        )
    if len(scored) > 1 and scored[1][0] == best:
        return None, (
            "Huom: demon kaksi kokoonpanoa ovat yhtä lähellä vakirosteria "
            f"({best} yhteistä pelaajaa kummallakin), joten kumpaakaan ei "
            "valittu ja luokka jäi ennusteeksi."
        )
    return frozenset(groups[key]), None


# -- Nimet syitä varten ------------------------------------------------------


def _nicknames(teams: Sequence[Team]) -> dict[str, str]:
    """SteamID64 -> nimimerkki koko divisioonasta.

    Kartta kootaan **kaikista** joukkueista eikä vain subjektista, koska juuri
    ulkopuolinen pelaaja on se, jonka nimen syy tarvitsee -- ja hän on
    tavallisesti jonkin toisen divisioonan joukkueen pelaaja. Ilman tätä syy
    sanoisi "Vakirosterin ulkopuolelta: 76561198062941501", mikä on tosi mutta
    lukukelvoton.

    Siirtyneet pelaajat (``Team.released``) ovat mukana samasta syystä.
    """
    names: dict[str, str] = {}
    for team in teams:
        for member in team.roster + team.released:
            if member.nickname:
                names.setdefault(member.game_player_id, member.nickname)
    return names


# -- Tiedosto ----------------------------------------------------------------


def _document(
    rows: Sequence[MapSelection],
    *,
    team: Team,
    league: LeagueSettings,
    thresholds: ThresholdSettings,
    generated_at: datetime,
    index_generated_at: Any,
) -> dict[str, Any]:
    """Valintatiedoston sisältö.

    Kynnykset ovat tiedostossa **arvoina eivätkä viittauksena asetuksiin**:
    tiedosto on päätös, ja päätöstä ei voi tarkistaa jälkikäteen, jos sen
    peruste on jossain muualla ja on ehtinyt muuttua. Samasta syystä mukana on
    ``index_generated_at``: se kertoo, minkä ottelulistan perusteella nämä rivit
    syntyivät.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "index_generated_at": index_generated_at,
        "competition_ids": list(league.championship_ids),
        "team_key": team.team_key,
        # Nimi on **havainto**, joten se saa olla null. Käyttäjälle näytettävä
        # nimi on eri asia (``stats["team_display"]``), eikä sitä kirjoiteta
        # tänne -- muuten tunniste päätyisi tiedostoon nimenä.
        "team_name": team.name,
        "roster_size": thresholds.roster_size,
        "roster_min_regulars": thresholds.roster_min_regulars,
        # Se joukko, jota vasten jokainen rivi ratkaistiin. Rosteri elää
        # ``discover``in ajoista toiseen, joten ilman tätä riviä "miksi tämä
        # hylättiin" olisi tarkistettavissa vain sen hetkisellä rosterilla.
        "roster": sorted(team.player_ids),
        "counts": counts(rows),
        "selections": [_row(row) for row in rows],
    }


def _row(row: MapSelection) -> dict[str, Any]:
    return {
        "map_demo_id": row.map_demo_id,
        "match_id": row.match_id,
        "map_index": row.map_index,
        "map_name": row.map_name,
        "is_league": row.is_league,
        "certainly_played": row.certainly_played,
        "roster_ok": row.roster_ok,
        "roster_reason": row.roster_reason,
        "roster_class": row.roster_class,
        "roster_source": row.roster_source,
        "players_seen": row.players_seen,
        "regulars": list(row.regulars),
        "outsiders": list(row.outsiders),
        # Havainnon ja ennusteen ero. Tyhjä, kun vertailtavaa ei ole --
        # vaihto karttojen välissä on havainto, ja sen puuttuminen on eri asia
        # kuin se, ettei sitä voitu katsoa.
        "joined": list(row.joined),
        "left": list(row.left),
    }


def _write(path: Path, document: dict[str, Any]) -> None:
    """Kirjoita valintatiedosto atomisesti; hakemisto luodaan tarvittaessa."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True)
    with atomic_path(path) as tmp:
        tmp.write_text(text + "\n", encoding="utf-8")


# -- Ajon yhteenveto ---------------------------------------------------------


def _stats(
    rows: Sequence[MapSelection],
    team: Team,
    thresholds: ThresholdSettings,
    skipped: _Skipped,
    generated_at: datetime,
) -> dict[str, Any]:
    """Yhteenvedon luvut.

    Otteluiden laskurit **täsmäävät**: ``matches_with_maps +
    matches_not_played + matches_without_veto == matches_seen``. Ilman tuota
    yhtälöä ottelu voisi kadota välistä ilman että mikään selittää eron -- ja
    juuri se on syy, miksi :class:`_Skipped` erottelee kaksi ohitusta.
    """
    rejections = [
        {
            "map_demo_id": row.map_demo_id,
            "map_name": row.map_name,
            "roster_reason": row.roster_reason,
        }
        for row in rows
        if not row.roster_ok
    ]
    return {
        **counts(rows),
        "team_key": team.team_key,
        # Nimi käyttäjälle: aina merkkijono, tarvittaessa tunniste. Tiedoston
        # ``team_name`` on eri kenttä ja saa olla null -- sama nimi kahdelle
        # eri lupaukselle olisi juuri se sekaannus, jota tässä vältetään.
        "team_display": team.display_name,
        # ``roster_players`` on rosterin koko, ``roster_threshold`` on kynnys.
        # Aiemmin molemmat kulkivat nimellä ``roster_size``, joka on tiedostossa
        # kynnys ja yhteenvedossa oli rosterin koko -- sama nimi, eri luku.
        "roster_players": len(team.roster),
        "roster_threshold": (
            f"{thresholds.roster_min_regulars}/{thresholds.roster_size}"
        ),
        "matches_seen": len(team.match_ids),
        "matches_with_maps": len({row.match_id for row in rows}),
        "matches_not_played": skipped.not_played,
        "matches_without_veto": len(skipped.no_veto),
        "rejections": rejections[:MAX_LISTED_REJECTIONS],
        "rejections_total": len(rejections),
        "generated_at": generated_at.isoformat(),
    }


def _notes(rows: Sequence[MapSelection], team: Team, skipped: _Skipped) -> list[str]:
    """Huomiot tyhjästä tai vajaasta tuloksesta -- **kaikki, ei ensimmäinen**.

    Tyhjä tulos ei ole virhe -- joukkue voi olla kauden alussa pelaamatta -- ja
    ``status`` on siksi ``ok``. Mutta "0 riviä" ilman sanaakaan siitä, mistä se
    johtuu, jättäisi käyttäjän arvaamaan. Aiempi versio palautti vain
    ensimmäisen huomion, jolloin "yksikään kartta ei päätynyt otantaan"
    nielaisi tiedon puuttuvista vetotiedoista.
    """
    notes: list[str] = []
    if not rows:
        if not team.match_ids:
            notes.append(
                f"Joukkueella {team.display_name} ei ole otteluita indeksissä, "
                "joten valittavaa ei ole."
            )
        elif not skipped.no_veto:
            notes.append(
                f"Joukkueella {team.display_name} on {len(team.match_ids)} "
                "ottelua, mutta yhtäkään ei ole pelattu -- pelaamattomalla "
                "ottelulla ei ole karttoja, joten MapDemoja ei ole vielä "
                "olemassa."
            )
    elif not any(row.roster_ok for row in rows):
        notes.append(
            f"Yksikään {len(rows)} kartasta ei päätynyt otantaan. "
            "Syyt ovat riveillä."
        )

    if skipped.not_played:
        notes.append(
            f"{skipped.not_played} ottelua on vielä pelaamatta, joten niillä ei "
            "ole karttoja."
        )
    if skipped.no_veto:
        listed = ", ".join(skipped.no_veto[:MAX_LISTED_MATCHES])
        rest = len(skipped.no_veto) - MAX_LISTED_MATCHES
        more = f" (+{rest} muuta)" if rest > 0 else ""
        notes.append(
            f"{len(skipped.no_veto)} pelattua ottelua jäi ilman rivejä, koska "
            "niiden vetotieto puuttuu indeksistä -- kartat pelattiin, mutta "
            f"emme tiedä mitkä: {listed}{more}. "
            "Aja uudelleen: uv run pappascout discover"
        )
    return notes
