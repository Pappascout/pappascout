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
from pappascout.domain.schemas import DEATHS, EVENTS, LINEUPS, ROUNDS, TICKS

__all__ = [
    "DemoParser",
    "DemoTables",
    "ParseDiagnostics",
    "ROUNDS_ADAPTER_COLUMNS",
    "TICKS_ADAPTER_COLUMNS",
    "EVENTS_ADAPTER_COLUMNS",
    "LINEUPS_ADAPTER_COLUMNS",
    "DEATHS_ADAPTER_COLUMNS",
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
#: ``round_no`` on mukana mutta aina tyhjä. Lentoradan oma juokseva numero
#: (``grenade_no``) **on** mukana: se on heiton ja räjähdyksen ainoa side, ja
#: se on yksikäsitteinen koko demossa toisin kuin ``grenade_entity_id``.
#: Demojen välillä avain on pari ``(map_demo_id, grenade_no)``.
EVENTS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in EVENTS if name != "map_demo_id"
)

#: Sarakkeet, jotka kokoonpanotaulussa ovat **tarkalleen** -- ei enempää eikä
#: vähempää.
#:
#: Yksi ero ``LINEUPS``-sopimukseen: ``map_demo_id`` puuttuu samasta syystä kuin
#: muissa tauluissa. ``round_no``:ta ei ole lainkaan -- kokoonpano ja nimi ovat
#: kartan ominaisuuksia eivätkä kierroksen, joten numerointi ei koske tätä
#: taulua eikä sen rivejä pudoteta puukkokierroksen mukana.
LINEUPS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in LINEUPS if name != "map_demo_id"
)

#: Sarakkeet, jotka kuolemataulussa ovat **tarkalleen** -- ei enempää eikä
#: vähempää.
#:
#: Sama kaksi poikkeusta kuin näytepiste- ja tapahtumataulussa:
#: ``map_demo_id`` puuttuu ja ``round_no`` on mukana mutta **aina tyhjä**.
#: Adapteri tuntee vain demon oman ``round_raw``-laskurin, ja numeroinnin
#: omistaa :func:`~pappascout.domain.rounds.mark_played_rounds`. Juuri siksi
#: puukkokierroksen kuolemat -- joita aineistossa oikeasti on -- putoavat
#: samalla mekanismilla kuin sen näytepisteet ja kranaatit eivätkä erikseen.
DEATHS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in DEATHS if name != "map_demo_id"
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
        lineups: Kokoonpanotaulu, sarakkeet :data:`LINEUPS_ADAPTER_COLUMNS`.
            Rivi per (kokoonpano, pelaaja): pelaajan nimi ja hänen
            klaaninimensä. Kenttä on pakollinen samasta syystä kuin
            ``events``: tyhjä oletus antaisi nimettömän portin näyttää
            demolta, jossa nimiä ei ole.
        deaths: Kuolemataulu, sarakkeet :data:`DEATHS_ADAPTER_COLUMNS`. Rivi
            per kuolema, uhri ja ampuja molemmat alueineen. Kenttä on
            pakollinen samasta syystä kuin kaksi edellistä: tyhjä oletus
            antaisi vanhan portin toteutuksen näyttää demolta, jossa kukaan ei
            kuollut.
    """

    rounds: pl.DataFrame
    ticks: pl.DataFrame
    events: pl.DataFrame
    lineups: pl.DataFrame
    deaths: pl.DataFrame


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
        grenades_sharing_an_entity_id: Lentoradat, jotka jakavat pelin oman
            ``grenade_entity_id``:n toisen radan kanssa samalla
            ``round_raw``:lla. **Havainto eikä vika**: taulun avain on
            ``grenade_no``, joka on yksikäsitteinen koko demossa, joten
            kierrätys ei sekoita mitään. Luku on lentoratoja eikä pareja --
            kolme rataa yhdellä tunnisteella on 3 -- ja se on tallessa siksi,
            että juuri se paljasti, ettei pari
            ``(round_no, grenade_entity_id)`` kelvannut avaimeksi.
        unknown_inventory_items: Tavaraluettelon nimet, joita aseluokittelu ei
            tunne, pareina ``(nimi, esiintymiä)`` aakkosjärjestyksessä.
            Tuntematon nimi **ei aseista** pelaajaa (luokittelu on sallittujen
            luettelo), joten ilman tätä listaa uusi veitsiskini ja uusi ase
            näyttäisivät täsmälleen samalta: ei kummastakaan mitään jälkeä.
            Esiintymämäärä on mukana, koska se erottaa ne toisistaan: yksi
            eksoottinen veitsi näkyy kerran tai kaksi, demoparser2:n
            nimeämismuutos joka rivillä. Tyhjä on normaali tulos.
        buy_window_seconds: Ostoikkunan pituus, jolla tämä ajo tehtiin
            (``[parse].buy_window_seconds``). Mukana siksi, että ajon tuloste
            kertoisi mistä hetkestä luvut on luettu -- ilman sitä lukija ei
            näe, mittasiko ajo ankkurista vai ostoajan lopusta. ``None``
            tarkoittaa porttia, joka ei kerro sitä; se on eri asia kuin 0,0.
        buy_window_cuts: Kuoleman katkaisemat kierrokset pareina
            ``(round_raw, montako ostosta jäi katkaisun taakse)``.
            **Pareina eikä valmiina lukuna**, koska adapteri ei tiedä mitkä
            kierrokset päätyvät tauluun: puukkokierros saa oman
            ``round_raw``:nsa mutta ``stages.parse`` pudottaa sen. Vaihe
            suodattaa nämä pelattuja kierroksia vasten ja laskee vasta siitä
            käyttäjälle näytettävät luvut -- sama sääntö kuin näytepisteillä ja
            utilityllä (ks. tämän luokan loppuhuomautus).

            Katkaisu on **havainto eikä vika**: se on sääntö, koska kuolleen
            tavaraluettelo tyhjenee ja panssari nollautuu, ja se osuu noin
            puoleen kierroksista (69/134 mitatussa aineistossa). Menetettyjen ostojen **kuuluu olla nolla**:
            mitatussa aineistossa (134 kierrosta) yksikään kuolema ei edellä
            viimeistä ostoa.
        buy_window_unchecked_cuts: Katkaistut kierrokset (``round_raw``),
            joilla menetettyjä ostoja **ei voitu tarkistaa**: ikkunan lopun
            tickiltä ei saatu yhdeltäkään pelaajalta luettavaa
            ``cash_spent``-arvoa. Ilman tätä menetettyjen ostojen nolla
            tarkoittaisi kahta eri asiaa -- "mitään ei menetetty" ja
            "ei tiedetä".
        buy_window_ticks_without_players: Kierrokset, joilla ostoajan lopun
            tickiltä ei saatu yhtään pelaajariviä ja mittaus palautui
            ankkuriin. **Vika eikä havainto**: käytännössä demo on katkennut
            kesken kierroksen. Ilman varasääntöä koko kierroksen talous olisi
            tyhjä. Tällaista kierrosta ei kirjata katkaisuksi, koska mittaus ei
            silloin osunut katkaisukohtaan lainkaan.
        buy_window_players_lost: Pelaajat, jotka olivat luettavissa ankkurilla
            mutta eivät enää mittauspisteessä, joukkueriveittäin laskettuna.
            Summat ja jakaja kutistuvat yhdessä, joten per pelaaja -arvot
            pysyvät oikeina -- mutta joukkue näyttää pelaavan vajaalla, ja se
            on eri väite kuin "yhteys katkesi kesken kierroksen".
        buy_window_sides_without_rows: Joukkuerivit, joilta mittauspisteessä ei
            saatu yhtään luettavaa pelaajaa, vaikka ankkurilla saatiin. Rivi
            menee tauluun tyhjänä mutta tilalla ``ok``, ja ``classify`` jättää
            sen luokittelematta puuttuvan havainnon takia -- oikea
            lopputulos, mutta ilman tätä lukua kukaan ei saisi tietää miksi.
        buy_window_refunds: Pelaajarivit, joilla ``cash_spent`` **pieneni**
            ankkurin ja mittauspisteen välillä eli ostos palautettiin. Prop
            kasvaa vain ostoista, joten lasku on yksikäsitteinen merkki
            palautuksesta eikä sekoitu kuolemaan. Mitattu: 8 pelaajariviä
            7 kierroksella kuudesta demosta.
        buy_window_stale_equipment: Pelaajarivit, joilla varustearvo nousi
            ilman että pelaaja osti, sai panssaria tai muutti
            tavaraluetteloaan. Se on palautuksen jättämä vanhentunut lukema:
            CS2 palauttaa rahan ja panssarin oikein, mutta
            ``m_unCurrentEquipmentValue`` ei aina laske mukana. Mitattu:
            1 pelaajarivi 134 kierroksesta, vaikutus enintään 1 000 $ per
            pelaaja eli 200 $/pelaaja joukkuetasolla. **Ei koske aseistettujen
            laskuria**, joka lukee tavaraluettelon ja panssarin.
        lineup_name_conflicts: Pelaajat, joilla havaittiin **useampi kuin yksi
            nimi** saman kartan ankkuritickeillä.
        lineup_clan_conflicts: Pelaajat, joilla havaittiin **useampi kuin yksi
            klaani** saman kartan ankkuritickeillä.

            Molemmat ovat nollia mitatussa aineistossa (viisi demoa,
            2026-08-30), ja juuri se mittaus on koko kokoonpanotaulun perusta:
            se sanoo, että nimi on kartan ominaisuus eikä kierroksen, ja että
            klaani seuraa pelaajaa eikä puolta. Oletus on **ajonaikaisesti
            tarkistamaton** ilman näitä lukuja: taulu kirjoittaa moodin, joten
            rikkoutunut oletus näyttäisi taulussa täsmälleen samalta kuin ehjä.
            Nollasta poikkeava luku on siis se oire, josta puolen kautta
            lukemisen ansan varoitus puhuu.
        deaths_without_tick: Kuolemat, joilta tick ei ollut luettavissa.
            Ilman tickiä kuolemaa ei voi kohdistaa kierrokseen eikä laskea
            ``t_s``:ää. Nolla on odotusarvo; luku on olemassa siksi, että
            jokainen muu pudotussyy raportoidaan eikä tämä saa olla poikkeus.
        deaths_outside_rounds: Kuolemat, jotka eivät osu yhdenkään kierroksen
            rajojen sisään -- lämmittely ennen ensimmäistä ankkuria tai
            kuolema kierroksen ratkeamisen ja seuraavan ostoajan välissä.
            Niille ei ole ``t_s``:ää, joten niitä ei voi kohdistaa mihinkään
            kierrokseen. **Eri asia kuin puukkokierroksen kuolemat**: ne ovat
            kierroksen sisällä, saavat ``round_raw``:nsa ja putoavat vasta
            ``stages.parse``in numeroinnissa muiden taulujen mukana.
        deaths_without_victim: Kuolemat **ilman uhria**: tapahtumalta
            puuttuu ``user_steamid`` kokonaan. Eri asia kuin puuttuva puoli,
            ja siksi oma lukunsa -- yhdistettynä se näyttäisi puolen
            päättelyn vialta, jota ei ole.
        deaths_without_victim_side: Kuolemat, joiden uhri tunnetaan mutta
            joiden puolta ei saatu selville sen paremmin kokoonpanosta,
            kierroksen omasta tickistä kuin tapahtuman ``user_team_num``
            -kentästä. Rivi pudotetaan: ``victim_lineup_key`` on koko taulun
            liitosavain, ja ilman sitä kuolema ei kuulu kenellekään.
        deaths_attacker_without_side: Kuolemat, joiden **ampujan** puolta ei
            saatu selville, vaikka ampuja tunnetaan. Rivi säilyy ja ampujan
            havainnot (tunniste, koordinaatit, alue) sen mukana; vain
            ``attacker_side`` ja ``attacker_lineup_key`` jäävät tyhjiksi.
            Pudottaminen veisi uhrin kuoleman mukanaan, ja ampujan
            tyhjentäminen hukkaisi havainnon, joka on luettavissa.
        armed_unreadable_rows: Joukkuerivit, joilla kalustolaskuri jäi tyhjäksi
            siksi, että jonkun pelaajan panssari **tai** tavaraluettelo ei
            ollut luettavissa. **Vika eikä havainto**: ankkurittomat
            kierrokset eivät ole tässä luvussa, joten nollasta poikkeava arvo
            tarkoittaa propivikaa. Ilman omaa lukuaan se sekoittuisi
            rehellisiin "ei havaintoa" -riveihin.
        armored_unreadable_rows: Sama panssarilaskurille, jonka luettavuusehto
            on kapeampi: **vain panssari**. Kaksi lukua eikä yksi, koska
            yhteinen luku ei erottaisi riviä, jolla panssari jäi lukematta,
            rivistä, jolla petti pelkkä tavaraluettelo -- ja juuri se
            jälkimmäinen tyhjentää vain ylemmän laskurin. Erotus
            ``armed_unreadable_rows - armored_unreadable_rows`` on siis
            "rivit, joilla vain tavaraluettelo petti", eikä tämä voi olla
            edellistä suurempi.

    Näytepisteiden, ensikontaktien ja utility-tapahtumien **määrät eivät ole
    täällä**: ne luetaan valmiista taulusta vaiheessa. Adapteri laskisi ne
    numeroimattomat kierrokset mukaan lukien, ja sama nimi eri nimittäjällä
    luetaan väärin.

    **Sama koskee ostoikkunaa**, ja siksi ``buy_window_cuts`` ja
    ``buy_window_unchecked_cuts`` ovat ``round_raw``-numeroita eivätkä valmiita
    lukuja: puukkokierros saa oman ``round_raw``:nsa, mutta se ei ole kierros
    eikä päädy tauluun. Adapterin laskema "13 katkaisua" olisi 12 siinä
    taulussa, jonka käyttäjä näkee -- ja mittaushetkien jakauma alkaisi
    sekunnin murto-osista, koska puukkokierros ratkeaa ennen ikkunan loppua.
    Mittaushetkien jakauma lasketaankin kokonaan vaiheessa sarakkeista
    ``freeze_end_tick`` ja ``buy_end_tick``.

    Pelaajakohtaiset vikalaskurit (``buy_window_players_lost``,
    ``buy_window_sides_without_rows``, ``buy_window_ticks_without_players``,
    ``buy_window_refunds``, ``buy_window_stale_equipment``) ovat sen sijaan
    kokonaislukuja ja **sisältävät puukkokierroksen**. Ne ovat vikalaskureita,
    joiden odotusarvo on nolla, joten puukkokierroksella havaittu vika on yhtä
    kertomisen arvoinen kuin muillakin -- eikä sitä saa suodattaa pois.
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
    grenades_sharing_an_entity_id: int = 0
    unknown_inventory_items: tuple[tuple[str, int], ...] = ()
    lineup_name_conflicts: int = 0
    lineup_clan_conflicts: int = 0
    deaths_without_tick: int = 0
    deaths_outside_rounds: int = 0
    deaths_without_victim: int = 0
    deaths_without_victim_side: int = 0
    deaths_attacker_without_side: int = 0
    armed_unreadable_rows: int = 0
    armored_unreadable_rows: int = 0
    buy_window_seconds: float | None = None
    buy_window_cuts: tuple[tuple[int, int], ...] = ()
    buy_window_unchecked_cuts: tuple[int, ...] = ()
    buy_window_ticks_without_players: int = 0
    buy_window_players_lost: int = 0
    buy_window_sides_without_rows: int = 0
    buy_window_refunds: int = 0
    buy_window_stale_equipment: int = 0


@runtime_checkable
class DemoParser(Protocol):
    """Portti, joka lukee demosta kaikki viisi taulua.

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
            jotka yhdistää ``grenade_no`` -- pelin oma ``grenade_entity_id``
            ei kelpaa avaimeksi, koska se kierrätetään myös saman kierroksen
            sisällä. ``grenade_no`` alkaa nollasta ja kasvaa heiton tickin
            mukaan; se on yksikäsitteinen mutta **ei yhtenäinen väli**, koska
            vaihe pudottaa numeroimattomien kierrosten rivit.
            Räjähtämätön kranaatti tuottaa vain heiton. ``area_source``
            erottaa havaitun heittoalueen johdetusta räjähdysalueesta, ja
            ``snap_distance`` kertoo jälkimmäisen etäisyyden. Tyhjä taulu on
            kelvollinen tulos -- demossa ei ollut utilityä.

            ``lineups`` on rivi per (kokoonpano, pelaaja), sarakkeet
            täsmälleen :data:`LINEUPS_ADAPTER_COLUMNS`. Pelaajajoukko on sama,
            josta ``lineup_key`` on laskettu, joten taulu ja tunniste eivät voi
            olla eri mieltä. ``player_name`` ja ``clan_name`` ovat
            **havaintoja**: puuttuva arvo on ``null`` eikä korvike, eikä tyhjä
            merkkijono ole nimi. Klaani luetaan pelaajakohtaisesti eikä puolen
            kautta -- puoli vaihtaa joukkuetta puoliajalla.

            ``deaths`` on rivi per kuolema, sarakkeet täsmälleen
            :data:`DEATHS_ADAPTER_COLUMNS`. Uhrin ja ampujan alue ovat
            **havaintoja** samalta tapahtumalta eivätkä napsautuksia, joten
            taulussa ei ole ``area_source``ia. Ampujaton kuolema (putoaminen,
            pommi) on aito tapaus: jokainen ``attacker_*`` on silloin ``null``
            eikä riviä pudoteta. Tyhjä taulu ei ole kelvollinen tulos --
            pelatussa ottelussa kuollaan, joten tyhjä taulu tarkoittaa
            rikkinäistä porttia.

            ``round_no`` on ``rounds``-, ``ticks``-, ``events``- ja
            ``deaths``-tauluissa kaikilla riveillä ``null`` -- numeroinnin
            päättää ``domain.rounds.mark_played_rounds``, jota vain
            ``stages.parse`` kutsuu. ``lineups``-taulussa saraketta ei ole
            lainkaan.

        Raises:
            ~pappascout.errors.ParseError: Jos tiedosto ei ole CS2-demo tai
                sitä ei voi lukea. Viesti on suomeksi ja kertoo, mitä tehdä.
        """
        ...
