"""Portit, joiden takaa vaiheet näkevät ulkomaailman (AD-8).

Vaihe ei saa tuntea demoparser2:ta. Se ottaa parametrikseen tämän moduulin
protokollan, ja oikea toteutus annetaan sille kutsussa. Testeissä tilalle
annetaan feikki, joka rakentaa taulun käsin -- silloin vaiheen logiikan voi
testata ilman 233 MB:n demoa.

Protokolla on ``typing.Protocol``, ei kantaluokka: toteutuksen ei tarvitse
periytyä mistään, ja tuonti pysyy kevyenä.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

import polars as pl

from pappascout.domain.rounds import REQUIRED_COLUMNS as _NUMBERING_COLUMNS
from pappascout.domain.schemas import EVENTS, ROUNDS, TICKS

__all__ = [
    "DemoParser",
    "DemoTables",
    "ParseDiagnostics",
    "ROUNDS_ADAPTER_COLUMNS",
    "TICKS_ADAPTER_COLUMNS",
    "EVENTS_ADAPTER_COLUMNS",
]

#: Sarakkeet, jotka kierrostaulussa ovat **tarkalleen** -- ei enempää eikä
#: vähempää.
#:
#: Kaksi eroa ``ROUNDS``-sopimukseen:
#:
#: ``map_demo_id`` puuttuu
#:     Adapteri saa pelkän tiedostopolun eikä voi tietää arkiston tunnistetta.
#:     Sen liittää ``stages.parse``.
#: ``score_start`` ja ``score_end`` ovat mukana
#:     Ne ovat kierroksen alun ja lopun **yhteispistemäärä**, ja
#:     :func:`~pappascout.domain.rounds.mark_played_rounds` päättää niistä,
#:     onko kierros pelattu. ``stages.parse`` pudottaa ne ennen kirjoitusta,
#:     joten levylle ne eivät päädy. Ilman niitä sopimuksessa toinen adapteri
#:     läpäisisi vaiheen saraketarkistuksen ja kaatuisi vasta domain-kerroksessa.
ROUNDS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    [name for name in ROUNDS if name != "map_demo_id"]
    + [name for name in _NUMBERING_COLUMNS if name not in ROUNDS]
)

#: Sarakkeet, jotka näytepistetaulussa ovat **tarkalleen** -- ei enempää eikä
#: vähempää.
#:
#: Yksi ero ``TICKS``-sopimukseen: ``map_demo_id`` puuttuu samasta syystä kuin
#: kierrostaulussa. ``round_no`` on mukana mutta **aina tyhjä**: adapteri tuntee
#: vain demon oman ``round_raw``-laskurin, ja numeroinnin omistaa
#: :func:`~pappascout.domain.rounds.mark_played_rounds`, jota vain
#: ``stages.parse`` kutsuu. Vaihe liittää numeron avaimella ``round_raw`` ja
#: pudottaa samalla numeroimattomien kierrosten rivit.
TICKS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in TICKS if name != "map_demo_id"
)

#: Sarakkeet, jotka utility-tapahtumataulussa ovat **tarkalleen** -- ei enempää
#: eikä vähempää.
#:
#: Sama kaksi poikkeusta kuin näytepistetaulussa: ``map_demo_id`` puuttuu ja
#: ``round_no`` on mukana mutta aina tyhjä. Kranaatin oma juokseva numero
#: (``grenade_no``) **ei** ole mukana: se on adapterin sisäinen parin avain,
#: joka kuolee ennen kuin taulu ylittää portin.
EVENTS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in EVENTS if name != "map_demo_id"
)


@dataclass(frozen=True)
class DemoTables:
    """Yhden demon kaikki parsitut taulut samasta lukukerrasta.

    Portti palauttaa ne yhdessä eikä kolmella kutsulla, ja syy on
    yhdenmukaisuus eikä nopeus: ``lineup_key``, ``side`` ja ``round_raw``
    lasketaan **kerran** ja päätyvät samoina kaikkiin tauluihin. Erilliset
    kutsut tekisivät kokoonpanojen tunnistuksen uudelleen, ja jos ne joskus
    eroaisivat, liitos ``(map_demo_id, round_no)`` menisi hiljaa ristiin.

    Sivutuotteena 233 MB:n demo puretaan ja luetaan vain kerran.

    Attributes:
        rounds: Kierrostaulu, sarakkeet :data:`ROUNDS_ADAPTER_COLUMNS`.
        ticks: Näytepistetaulu, sarakkeet :data:`TICKS_ADAPTER_COLUMNS`.
        events: Utility-tapahtumataulu, sarakkeet
            :data:`EVENTS_ADAPTER_COLUMNS`. Kenttä on pakollinen eikä sillä ole
            oletusta: tyhjä oletus antaisi vanhan portin toteutuksen näyttää
            demolta, jossa ei heitetty yhtään kranaattia.
    """

    rounds: pl.DataFrame
    ticks: pl.DataFrame
    events: pl.DataFrame


@dataclass(frozen=True)
class ParseDiagnostics:
    """Havainnot, jotka eivät mahdu ``ROUNDS``-sopimukseen.

    Tauluun ei saa lisätä sarakkeita ilman skeemamuutosta (AD-2), mutta
    käyttäjälle on silti kerrottava, kun jokin arvo on oletus eikä mittaus.
    Adapteri saa tarjota tämän valinnaisena ``diagnostics``-attribuuttina;
    vaihe lukee sen varovasti (``getattr``), joten portin toteutuksen ei ole
    pakko tarjota sitä.

    Attributes:
        tick_rate: Käytetty tickrate.
        tick_rate_measured: ``True``, jos tickrate mitattiin demosta;
            ``False``, jos jouduttiin turvautumaan oletukseen.
        rounds_seen: Demosta löytyneiden **kierrosrajojen** määrä. Mukana ovat
            pelatut ja pelaamattomat kierrokset sekä ne rajat, jotka eivät ole
            kierroksia lainkaan (``match_restarts``). Luku on siis aina
            vähintään yhtä suuri kuin kierrosten määrä.
        match_restarts: Ottelun uudelleenaloitukset. Uudelleenaloituksella on
            freezetime-ankkuri mutta ei ``round_end``iä, ja demon oma
            kierrosnumerointi **jatkuu sen yli yhdellä** -- se ei siis kuluta
            kierrosnumeroa. Liigaotteluissa niitä on tasan yksi, heti
            puukkokierroksen jälkeen. Se pelataan, mutta se ei ole kierros eikä
            se tuota riviä yhteenkään tauluun -- samoin kuin puukkokierros.
            Luku raportoidaan, koska pudotus ei saa olla hiljainen; nolla on
            vanhojen demojen normaali tulos.

            **Eri asia kuin** ``stages.parse``in ``utility_unnumbered_rounds``,
            joka laskee heittoja kierroksilta joilta puuttuu ``round_no``.
            Tässä puuttuu ``round_raw``, eikä kierrosta ole olemassakaan.
        partial_samples: Näytepisteet, joilta saatiin vähemmän pelaajia kuin
            demon parhaalta pisteeltä. Nolla on normaali tulos; systemaattinen
            propivika näkyisi tässä luvussa jo parsintavaiheessa eikä vasta
            vinoutuneina aggregaatteina.
        unknown_side_events: Vahinkotapahtumat, joissa tekijän tai uhrin puolta
            ei saatu selville. Ne eivät kelpaa ensikontaktiksi, joten kierros
            voi menettää kontaktinsa -- luku kertoo, milloin niin kävi.
        grenades_without_thrower: Lentoradat, joilta puuttuu heittäjä. Rivi
            jäisi ilman joukkuetta, joten kranaatti ohitetaan kokonaan.
        grenades_outside_rounds: Kranaatit, joiden heitto ei osu yhdenkään
            kierroksen rajojen sisään -- käytännössä lämmittely ennen
            ensimmäistä ankkuria tai kierroksen ratkeamisen jälkeinen heitto.
            Niille ei ole ``t_s``:ää, joten niitä ei voi kohdistaa mihinkään
            kierrokseen.
        grenades_unknown_side: Kranaatit, joiden heittäjän puolta ei saatu
            selville sen paremmin kokoonpanosta kuin kierroksen omasta
            tickistä. Väärä joukkue veisi utilityn vastustajan tiliin, joten
            rivi ohitetaan.
        grenades_unknown_type: Kranaatit, joiden luokkanimeä ei tunneta. Nimi
            säilyy taulussa sellaisenaan; luku paljastaa demoparser2:n
            uudelleennimeämisen ennen kuin se näkyy raportissa.
        grenades_fire_type_unresolved: Tulikranaatit, joiden
            molotov/incendiary-erottelu ei ratkennut. Tyypiksi jää
            ``molotov``, joten ilman lukua erottelun täydellinen rikkoutuminen
            näyttäisi demolta, jossa heitettiin pelkkiä molotoveja.
        grenades_detonating_after_round: Räjähdykset, jotka osuvat kierroksen
            päättymisen jälkeen. Rivi jää tauluun koordinaatteineen, mutta
            aluetta ei napsauteta -- pelaajat ovat jo seuraavan kierroksen
            spawnissa.
        grenade_ticks_without_players: Päätepisteen tickit, joilta ei saatu
            yhtään pelaajariviä. **Vika eikä havainto**: aluetta ei voitu edes
            yrittää, ja ilman omaa lukuaan se sekoittuisi rehellisiin
            "kukaan ei ollut lähellä" -tapauksiin.
        grenades_id_reused_in_round: Kranaattiparit, joiden tunniste toistuu
            saman kierroksen sisällä. Sopimus lupaa, että
            ``(round_no, grenade_entity_id)`` yksilöi parin.
        unknown_inventory_items: Tavaraluettelon nimet, joita aseluokittelu ei
            tunne, pareina ``(nimi, esiintymiä)`` aakkosjärjestyksessä.
            Tuntematon nimi **ei aseista** pelaajaa (luokittelu on sallittujen
            luettelo), joten ilman tätä listaa uusi veitsiskini ja uusi ase
            näyttäisivät täsmälleen samalta: ei kummastakaan mitään jälkeä.
            Esiintymämäärä on mukana, koska se erottaa ne toisistaan: yksi
            eksoottinen veitsi näkyy kerran tai kaksi, demoparser2:n
            nimeämismuutos joka rivillä. Tyhjä on normaali tulos.
        armed_unreadable_rows: Joukkuerivit, joilla kalustolaskuri jäi tyhjäksi
            siksi, että jonkun pelaajan panssari tai tavaraluettelo ei ollut
            luettavissa. **Vika eikä havainto**: ankkurittomat kierrokset
            eivät ole tässä luvussa, joten nollasta poikkeava arvo tarkoittaa
            propivikaa. Ilman omaa lukuaan se sekoittuisi rehellisiin
            "ei havaintoa" -riveihin.

    Näytepisteiden, ensikontaktien ja utility-tapahtumien **määrät eivät ole
    täällä**: ne luetaan valmiista taulusta vaiheessa. Adapteri laskisi ne
    numeroimattomat kierrokset mukaan lukien, ja sama nimi eri nimittäjällä
    luetaan väärin.
    """

    tick_rate: float
    tick_rate_measured: bool
    rounds_seen: int
    match_restarts: int = 0
    partial_samples: int = 0
    unknown_side_events: int = 0
    grenades_without_thrower: int = 0
    grenades_outside_rounds: int = 0
    grenades_unknown_side: int = 0
    grenades_unknown_type: int = 0
    grenades_fire_type_unresolved: int = 0
    grenades_detonating_after_round: int = 0
    grenade_ticks_without_players: int = 0
    grenades_id_reused_in_round: int = 0
    unknown_inventory_items: tuple[tuple[str, int], ...] = ()
    armed_unreadable_rows: int = 0


@runtime_checkable
class DemoParser(Protocol):
    """Portti, joka lukee demosta kierros-, näytepiste- ja tapahtumataulun.

    Toteutuksen on palautettava **havaitut** arvot sellaisenaan: ei
    kierrostyyppiluokittelua, ei loss countia, ei aggregointia, ei muuta
    johdettua. Ainoa päättely, joka tähän kuuluu, on kierrosrajojen
    tunnistaminen ja näytepisteiden valinta niiden sisältä.
    """

    def parse_demo(
        self, path: Path, sample_seconds: Sequence[float]
    ) -> DemoTables:
        """Lue demo ja palauta sen kaikki taulut.

        Args:
            path: Demotiedosto, joko ``.dem`` tai pakattu ``.dem.zst`` /
                ``.dem.gz``.
            sample_seconds: Näytepisteet sekunteina kierroksen
                freezetime-ankkurista (``[parse].snapshot_seconds``).

        Returns:
            :class:`DemoTables`.

            ``rounds`` on pitkä taulu, kaksi riviä per kierros (yksi
            kummallekin joukkueelle), sarakkeet täsmälleen
            :data:`ROUNDS_ADAPTER_COLUMNS`.

            ``ticks`` on rivi per (pelaaja, kierros, näytepiste), sarakkeet
            täsmälleen :data:`TICKS_ADAPTER_COLUMNS`. Rivejä on vain
            kierroksilta, joilla on freezetime-ankkuri ja päättymistick, eikä
            yhtään näytepistettä kierroksen päättymisen jälkeen.

            ``events`` on rivi per utility-tapahtuma, sarakkeet täsmälleen
            :data:`EVENTS_ADAPTER_COLUMNS`. Heitto ja räjähdys ovat kaksi riviä,
            jotka yhdistää ``(round_raw, grenade_entity_id)`` -- pelkkä
            tunniste ei riitä, koska peli kierrättää ne demon aikana.
            Räjähtämätön kranaatti tuottaa vain heiton. ``area_source``
            erottaa havaitun heittoalueen johdetusta räjähdysalueesta, ja
            ``snap_distance`` kertoo jälkimmäisen etäisyyden. Tyhjä taulu on
            kelvollinen tulos -- demossa ei ollut utilityä.

            Kaikissa tauluissa ``round_no`` on kaikilla riveillä ``null`` --
            numeroinnin päättää ``domain.rounds.mark_played_rounds``, jota vain
            ``stages.parse`` kutsuu.

        Raises:
            ~pappascout.errors.ParseError: Jos tiedosto ei ole CS2-demo tai
                sitä ei voi lukea. Viesti on suomeksi ja kertoo, mitä tehdä.
        """
        ...
