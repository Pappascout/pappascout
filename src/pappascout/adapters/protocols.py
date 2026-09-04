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
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import polars as pl

from pappascout.domain.rounds import REQUIRED_COLUMNS as _NUMBERING_COLUMNS
from pappascout.domain.schemas import (
    CALLOUT_CLOUD,
    DEATHS,
    EVENTS,
    LINEUPS,
    MATCH,
    ROUNDS,
    TICKS,
)

__all__ = [
    "DemoParser",
    "DemoTables",
    "ParseDiagnostics",
    "MatchSource",
    "Match",
    "MatchTeam",
    "RosterPlayer",
    "ROUNDS_ADAPTER_COLUMNS",
    "TICKS_ADAPTER_COLUMNS",
    "EVENTS_ADAPTER_COLUMNS",
    "LINEUPS_ADAPTER_COLUMNS",
    "DEATHS_ADAPTER_COLUMNS",
    "CALLOUTS_ADAPTER_COLUMNS",
    "MATCH_ADAPTER_COLUMNS",
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

#: Sarakkeet, jotka pistepilvitaulussa ovat **tarkalleen** -- ei enempää eikä
#: vähempää.
#:
#: Yksi ero ``CALLOUT_CLOUD``-sopimukseen: ``map_demo_id`` puuttuu samasta
#: syystä kuin muissa tauluissa. ``round_no``:ta ei ole lainkaan, eikä sitä
#: pidä lisätä: pistepilvi on **kartan** ominaisuus tässä demossa eikä
#: kierroksen havainto, joten sen rivit eivät putoa puukkokierroksen mukana --
#: sama sääntö kuin kokoonpanotaululla. Pilvi kootaan tarkoituksella demon
#: **kaikista** tickeistä, myös lämmittelystä ja puukkokierroksesta: kysymys
#: on "missä kartalla on mahdollista seistä ja mikä alue se on", eikä siihen
#: vastaa vain pelattujen kierrosten aineisto.
CALLOUTS_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in CALLOUT_CLOUD if name != "map_demo_id"
)


#: Sarakkeet, jotka ottelutaulussa ovat **tarkalleen** -- ei enempää eikä
#: vähempää.
#:
#: Yksi ero ``MATCH``-sopimukseen: ``map_demo_id`` puuttuu samasta syystä kuin
#: muissa tauluissa. ``round_no``:ta ei ole lainkaan eikä pidä lisätä: ottelu
#: ei ole kierros, ja koko taulun peruste on juuri se ero.
MATCH_ADAPTER_COLUMNS: tuple[str, ...] = tuple(
    name for name in MATCH if name != "map_demo_id"
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
        callouts: Pistepilvi, sarakkeet :data:`CALLOUTS_ADAPTER_COLUMNS`. Rivi
            per ruutu. Se on ``events``-taulun räjähdysalueiden **lähde**, ja
            juuri siksi se on samassa paluuarvossa eikä omassa kutsussaan:
            kaksi kutsua voisi rakentaa pilven kahdesti ja eri tuloksella,
            jolloin taulun rivi ei enää selittäisi taulukossa olevaa aluetta.
            Kenttä on pakollinen samasta syystä kuin edelliset.
        match: Ottelutaulu; sisältö on kuvattu
            :meth:`DemoParser.parse_demo`ssa. Kenttä on pakollinen samasta
            syystä kuin edelliset: tyhjä oletus antaisi vanhan portin
            toteutuksen näyttää demolta, jonka otsikossa ei ole karttaa -- ja
            se ero ratkaisee, luetaanko nimi havaintona vai päätelläänkö se
            tunnisteesta.
    """

    rounds: pl.DataFrame
    ticks: pl.DataFrame
    events: pl.DataFrame
    lineups: pl.DataFrame
    deaths: pl.DataFrame
    callouts: pl.DataFrame
    match: pl.DataFrame


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
        sample_rows_without_pawn: Pelaajarivit, jotka ohitettiin siksi, että
            pelaajan **kontrolleri oli tallella mutta pawn ei**: jokainen
            pawn-kenttä (elossaolo, alue, x/y/z) oli tyhjä samalla rivillä.
            Pelaaja ei silloin ole kartalla lainkaan, joten hänen riviään ei
            ole olemassa -- elossaoloa ei arvata kumpaankaan suuntaan.

            **Nimen etuliite rajaa luvun siihen mitä se mittaa.** Se kattaa
            näytepistepropeilla luetut tickit -- asetelman näytepisteet ja
            utilityn heittotickit -- eikä koko demoa: pistepilven koko
            tickisarja ja kierrosrajojen luku eivät kerrytä sitä. Sama tick
            voi tulla luetuksi molemmilla kutsuilla, ja se lasketaan silloin
            kerran: luku on rivejä eikä rivilukemia.

            **Havainto eikä vika**, mutta se ei saa olla hiljainen: puuttuva
            pelaaja pienentää sen kierroksen asetelmaa, ja lukijan on
            nähtävä se. Kierros pysyy otannassa. Nolla on normaali tulos;
            mitattu ``anubis_vs_RCAVE_VETERANS``-demossa 15 riviä --
            **yksi pelaaja yhdellä kierroksella**, viisi näytepisteen
            tickiltä ja kymmenen heittojen tickeiltä. Arkiston seitsemässä
            muussa demossa nolla.

            **Eri asia kuin katsoja**, jolta puuttuu kontrollerin joukkue --
            katsojarivit eivät ole tässä luvussa. **Eri asia myös kuin
            puuttuva elossaolo yksin**: jos sijainti tai alue on tallella
            mutta ``m_lifeState`` puuttuu, ajo kaatuu edelleen, koska silloin
            kyse on kirjaston kenttänimen muutoksesta eikä pelaajan tilasta.
        sample_points_without_pawn: Näytepisteet, joilta **ei tullut yhtään
            riviä**, koska jokainen rivi oli pawniton. Piste jää kokonaan
            väliin: kierros menettää sen näytepisteen, mutta ajo jatkuu.

            **Oma lukunsa eikä osa ``partial_samples``ia.** Kokonaan
            puuttuva piste on vakavampi kuin vaillinainen, ja vajaiden
            joukkoon niputettuna se näyttäisi lievemmältä kuin on. Nolla on
            normaali tulos, myös silloin kun ``sample_rows_without_pawn`` on
            nollasta poikkeava: yksi pawniton pelaaja kymmenestä jättää
            pisteen vajaaksi muttei tyhjäksi.
        grenade_throwers_without_row: Heitot, joiden **heittäjää ei ollut
            heiton tickin riveissä**. Heiton alue on heittäjän oma
            ``m_szLastPlaceName``, joten ilman hänen riviään alue jää tyhjäksi
            eikä sitä voi korvata: pistepilvi nimeää räjähdyksiä, ei heittoja.

            **Vika eikä havainto.** Luku on olemassa Story 2.10:stä lähtien:
            ennen pawnittoman rivin ohitusta tällainen heitto kaatoi ajon
            elossaolovartijaan, ja ilman omaa laskuriaan se valuisi nyt
            hiljaa ``utility_without_area``-lukuun ilman syytä. Nolla on
            odotusarvo -- heittäjällä on pawn sillä hetkellä kun hän heittää
            -- ja se on nolla myös mitatussa
            ``anubis_vs_RCAVE_VETERANS``-demossa, jossa pawniton pelaaja ei
            heittänyt mitään.
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
            päättymisen jälkeen. **Havainto eikä pudotus**: rivi saa alueensa
            kuten muutkin, koska pistepilvi on kartan ominaisuus eikä riipu
            siitä, missä pelaajat sillä hetkellä ovat. Story 2.2:ssa nämä
            jätettiin tarkoituksella aluettomiksi -- silloin alue tuli
            lähimmältä elossa olevalta pelaajalta, ja kierroksen jälkeen se
            olisi kertonut seuraavan kierroksen spawnin. Syy katosi menetelmän
            mukana; luku jää, koska myöhäinen räjähdys on silti oma ilmiönsä.
        grenade_ticks_without_players: **Heiton** tickit, joilta ei saatu
            yhtään pelaajariviä. **Vika eikä havainto**: heittäjän omaa aluetta
            ei voitu edes yrittää lukea. Räjähdyksen tickejä ei enää lueta
            lainkaan -- sen alue tulee pistepilvestä eikä tickin pelaajista.

            **Kokonaan pawniton tick ei ole tässä luvussa.** Se on havainto
            samalla säännöllä kuin näytepisteillä -- demo palautti rivit,
            eikä kukaan vain ollut kartalla -- ja se on jo laskettu
            ``sample_rows_without_pawn``iin. Sama ilmiö ei saa olla toisella
            polulla vika ja toisella havainto.
        callout_cloud_rows_read: Rivit, jotka pistepilven rakentaminen luki
            demosta (koko demon tickisarja, rivi per pelaaja per tick). Luku
            on tallessa, koska se on ainoa paikka, jossa tämän vaiheen hinta
            näkyy: se on kertaluokkia suurempi kuin mikään muu tickiluku, ja
            juuri siksi aineisto pudotetaan ruudukoksi heti.

            **Kelvollisten rivien määrää ei ole tässä**: se on
            ``callouts``-taulun ``observations``-sarakkeen summa, koska
            jokainen kelvollinen rivi päätyy täsmälleen yhteen ruutuun. Vaihe
            laskee sen sieltä, ja näiden kahden **suhde** on se, mikä kertoo
            onko pilvi terve -- mitatussa aineistossa 71-78 %.
        callout_cloud_empty_reason: Miksi pistepilvi jäi tyhjäksi, tai
            ``None`` jos se ei jäänyt. Tyhjä pilvi **ei kaada ajoa**: kaikki
            räjähdysalueet jäävät nulliksi ja ajo jatkuu. Ilman syytä se
            näyttäisi kuitenkin demolta, jossa ei heitetty utilityä --
            täsmälleen se hiljainen vika, jota vastaan jokainen muu
            pudotuslaskuri on olemassa.
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
    sample_rows_without_pawn: int = 0
    sample_points_without_pawn: int = 0
    grenade_throwers_without_row: int = 0
    unknown_side_events: int = 0
    grenades_without_thrower: int = 0
    grenades_outside_rounds: int = 0
    grenades_unknown_side: int = 0
    grenades_unknown_type: int = 0
    grenades_fire_type_unresolved: int = 0
    grenades_detonating_after_round: int = 0
    grenade_ticks_without_players: int = 0
    grenades_sharing_an_entity_id: int = 0
    callout_cloud_rows_read: int = 0
    callout_cloud_empty_reason: str | None = None
    #: Miksi kartan nimeä ei saatu demon otsikosta, tai ``None`` jos saatiin.
    #:
    #: Nimen puuttuminen on laillinen havainto, mutta sillä on **kolme eri
    #: syytä**: otsikossa ei ole ``map_name``-kenttää lainkaan, kenttä on tyhjä,
    #: tai koko otsikko ei ole luettavissa sanakirjana. Ensimmäinen tarkoittaa
    #: käytännössä demoparser2:n uudelleennimeämää kenttää, ja ilman tätä
    #: erittelyä koko arkisto palaisi demokohtaisiin karttahaaroihin ilman
    #: yhtään merkkiä siitä -- sama vikaluokka kuin Story 2.10:n pawnittomalla
    #: pelaajalla. Sama sääntö kuin ``callout_cloud_empty_reason``illa: tyhjän
    #: tuloksen syy näkyy vain lukuhetkellä, joten se kulkee diagnostiikassa.
    header_map_name_missing_reason: str | None = None
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
    """Portti, joka lukee demosta kaikki seitsemän taulua.

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
            **havaintoja** samalta tapahtumalta eivätkä johdoksia, joten
            taulussa ei ole ``area_source``ia. Ampujaton kuolema (putoaminen,
            pommi) on aito tapaus: jokainen ``attacker_*`` on silloin ``null``
            eikä riviä pudoteta. Tyhjä taulu ei ole kelvollinen tulos --
            pelatussa ottelussa kuollaan, joten tyhjä taulu tarkoittaa
            rikkinäistä porttia.

            ``callouts`` on rivi per pistepilven ruutu, sarakkeet
            täsmälleen :data:`CALLOUTS_ADAPTER_COLUMNS`. Se on
            ``events``-taulun räjähdysalueiden lähde: jokainen
            ``area_source = "point_cloud"`` -rivi on jäljitettävissä tämän
            taulun ruutuun. Tyhjä taulu **on** kelvollinen tulos -- demosta ei
            saatu yhtään elossa-riviä nimetyllä alueella -- ja silloin
            jokainen räjähdysalue on ``null``; syy kulkee diagnostiikassa.

            ``match`` on **täsmälleen yksi rivi**, sarakkeet täsmälleen
            :data:`MATCH_ADAPTER_COLUMNS`. ``map_name`` on demon otsikon
            (``parse_header``) kartta **havaintona**: se palautetaan
            sellaisenaan eikä sitä verrata karttapooliin, koska poolin
            ulkopuolinen kartta on aito havainto. Puuttuva tai tyhjä nimi on
            ``null`` eikä korvike. Tyhjä taulu ei ole kelvollinen tulos:
            demossa on aina ottelu, vaikka sen kartta olisi tuntematon.

            ``round_no`` on ``rounds``-, ``ticks``-, ``events``- ja
            ``deaths``-tauluissa kaikilla riveillä ``null`` -- numeroinnin
            päättää ``domain.rounds.mark_played_rounds``, jota vain
            ``stages.parse`` kutsuu. ``lineups``-, ``callouts``- ja
            ``match``-tauluissa saraketta ei ole lainkaan.

        Raises:
            ~pappascout.errors.ParseError: Jos tiedosto ei ole CS2-demo tai
                sitä ei voi lukea. Viesti on suomeksi ja kertoo, mitä tehdä.
        """
        ...


# -- Otteluiden portti (Story 3.1) ------------------------------------------
#
# Sama jako kuin :class:`DemoParser`illa: portti on täällä, ainoa toteutus on
# omassa moduulissaan (:mod:`pappascout.adapters.faceit`). Vaihe ei tuo
# ``requests``ia eikä tunne HTTP:tä -- se näkee nämä kolme dataluokkaa ja
# kaksi metodia.


@dataclass(frozen=True)
class RosterPlayer:
    """Pelaaja ottelun kokoonpanossa.

    **Kaksi tunnistetta, ja vain toinen niistä esiintyy demoissa.** Story 3.1:n
    mittaus (``mittaus-faceit-aineisto.md`` luku 2) vertasi FACEITin rosteria
    arkiston ``lineups.parquet``iin: yhteiset tunnisteet olivat
    ``game_player_id``-arvoja (SteamID64) ja täsmäsivät **merkkijonoina ilman
    muunnosta**. ``player_id`` on FACEITin oma UUID, jota ei esiinny demoissa
    lainkaan. Molemmat ovat tallessa, mutta se, jolla FACEIT-rosteri liitetään
    demoihin, on ``game_player_id``.

    Attributes:
        player_id: FACEIT player id (UUID). Lähteen oma avain, jolla pelaajan
            tiedot haetaan rajapinnasta.
        nickname: Nimimerkki **havaintona**. Puuttuva tai tyhjä on ``None``
            eikä korvike; nimimerkki voi vaihtua, tunniste ei.
        game_player_id: Pelin oma pelaajatunniste, CS2:ssa **SteamID64**. Sama
            arvo kuin ``lineups.parquet``in ``player_id``-sarakkeessa, joten
            tämä on ainoa tunniste, joka liittää rosterin demoihin. ``None``,
            jos lähde ei sitä antanut -- puuttuvaa ei korvata, koska keksitty
            tunniste liittyisi väärään pelaajaan.
    """

    player_id: str
    nickname: str | None = None
    game_player_id: str | None = None


@dataclass(frozen=True)
class MatchTeam:
    """Ottelun toinen osapuoli ja sen kokoonpano.

    Attributes:
        team_id: Lähteen oma joukkuetunniste. **Ei sama asia kuin
            arkiston ``team_key``** (AD-6): kanonisen tunnisteen päättää
            Story 3.2 kokoonpanoista, ja tämä on vain havainto siitä, minkä
            tunnisteen lähde antoi.
        name: Joukkueen nimi havaintona, tai ``None``.
        roster: Pelaajat siinä järjestyksessä kuin lähde ne antoi.
            Tyhjä monikko on kelvollinen tulos -- tulevalla ottelulla ei
            välttämättä ole vielä kokoonpanoa.
        substitutes: Vaihtopelaajat siinä järjestyksessä kuin lähde ne antoi.
            **Oma kenttänsä eikä ``roster``iin sulautettuna**, koska lähde
            erottelee ne ja ero on havainto: kuka aloitti ja kuka oli varalla.
            Vakirosterin (Story 3.2) laskee ``domain.teams`` yhdisteenä, ja
            sen on saatava tehdä se itse -- yhdistäminen tässä veisi
            säännön porttiin, jossa sitä ei voi testata ilman verkkoa.
            Mitattu 2026-09-04: ilman tätä listaa vakirosteri aliarvioi
            järjestelmällisesti (``Lindberq_`` on demossa muttei
            ``roster``issa).
    """

    team_id: str | None = None
    name: str | None = None
    roster: tuple[RosterPlayer, ...] = ()
    substitutes: tuple[RosterPlayer, ...] = ()


@dataclass(frozen=True)
class Match:
    """Yksi ottelu ytimen sanastolla.

    **Portti ei puhu FACEITin sanastoa** (AD-8): ``faction1``, ``voting`` ja
    epoch-sekunnit jäävät adapterin sisään, ja tänne tulee se, mitä vaihe
    tarvitsee.

    Attributes:
        match_id: Ottelun tunniste. Sama, joka on ``map_demo_id``in
            alkuosana (``{match_id}-{map_index}``).
        competition_id: Kilpailun tunniste, tai ``None``. **Tästä ratkeaa
            ``is_league``**: ottelu on liigaottelu, jos tämä on
            ``[league].championship_ids``-listassa -- ei nimestä, koska
            nimi on ihmisen kirjoittama merkkijono.
        status: Ottelun tila lähteen sanana (esim. ``FINISHED``), tai
            ``None``. Sitä ei tulkita täällä: tilojen luettelo on lähteen
            oma eikä pappascoutin, ja arvaus vanhenisi hiljaa.
        scheduled_at: Ottelun sovittu alkuhetki UTC-tietoisena, tai ``None``.
            **Ottelulistassa on tämä eikä ``started_at``** (mitattu
            2026-09-04): pelaamattomalla ottelulla ei ole alkuhetkeä, mutta
            aikataulu sillä on. Kaksi eri kenttää eikä yksi, koska aikataulu
            on suunnitelma ja alkuhetki havainto -- ja niiden sekoittaminen
            väittäisi pelatuksi ottelun, jota ei ole pelattu.
        started_at: Todellinen alkuhetki UTC-tietoisena, tai ``None`` jos
            ottelu ei ole alkanut. Ottelulistassa tätä ei ole; se tulee vain
            yhden ottelun haussa.
        finished_at: Päättymishetki UTC-tietoisena, tai ``None``.
        teams: Osapuolet. Tavallisesti kaksi, mutta lukumäärää ei väitetä
            täällä -- vaihe tarkistaa sen, jos se siitä riippuu.
        map_picks: Pelatut kartat siinä järjestyksessä, jossa ne valittiin.
            **Järjestys on ``map_index``in määritelmä**: ``map_index`` on
            0-pohjainen indeksi tähän monikkoon, ja ``map_demo_id`` rakentuu
            siitä. Tyhjä monikko tarkoittaa "ei vetotietoa", ei "ei karttoja".
            Koko vedon (banit, vuorojärjestys) mallintaminen on Epic 4:ää;
            tässä on vain se lista, jonka ``map_index`` tarvitsee.
    """

    match_id: str
    competition_id: str | None = None
    status: str | None = None
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    teams: tuple[MatchTeam, ...] = ()
    map_picks: tuple[str, ...] = ()


@runtime_checkable
class MatchSource(Protocol):
    """Portti, jonka takaa vaihe näkee ottelut.

    Toteutus saa käyttää verkkoa, välimuistia ja uudelleenyrityksiä; portti ei
    lupaa niistä mitään. Ainoa lupaus on, että virhe on
    :class:`~pappascout.errors.ApiError` ja sen viesti on suomeksi.

    **Kaksi metodia eikä neljä.** ARCHITECTURE-SPINE (AD-8) luetteli portille
    myös ``get_roster``in ja ``get_veto``n, mutta molempien tiedot ovat
    :class:`Match`in kentissä ``teams`` ja ``map_picks`` -- erillinen metodi
    olisi toinen tapa hakea sama asia ja toinen välimuistiavain samalle
    vastaukselle. ``get_schedule`` (Epic 4: seuraava vastustaja) on
    :meth:`get_matches` aikasuodattimella, jonka vaihe tekee itse
    ``scheduled_at``ista -- **ei** ``started_at``ista: mitattu 2026-09-04, ettei
    ottelulistalla ole alkuhetkeä lainkaan, ja seuraava vastustaja on
    määritelmällisesti ottelu, joka ei ole vielä alkanut. Porttia ei siis
    tarvitse purkaa Epic 4:ssä.
    """

    def get_matches(self, competition_id: str) -> tuple[Match, ...]:
        """Palauta kilpailun **kaikki** ottelut.

        Args:
            competition_id: Kilpailun tunniste
                (``[league].championship_ids``in alkio).

        Returns:
            Ottelut yhtenä monikkona. Toteutus hakee kaikki sivut, joten
            kutsuja ei sivuta: sivutus on kuljetuksen yksityiskohta.
            Tyhjä monikko on kelvollinen tulos -- kilpailu, jossa ei ole vielä
            otteluita.

        Raises:
            ~pappascout.errors.ApiError: Jos otteluita ei saatu haettua.
        """
        ...

    def get_match(self, match_id: str) -> Match:
        """Palauta yhden ottelun tiedot.

        Args:
            match_id: Ottelun tunniste.

        Returns:
            :class:`Match`. Kokoonpanot ja karttavalinnat ovat tässä
            täydellisemmät kuin ottelulistassa, jos lähde ne erottelee.

        Raises:
            ~pappascout.errors.ApiError: Jos ottelua ei löydy tai sitä ei
                saatu haettua.
        """
        ...
