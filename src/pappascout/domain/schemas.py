"""Jaetut Polars-taulusopimukset (AD-2, AD-4, AD-5).

Kuusi taulua, jotka kaikki putken vaiheet lukevat ja kirjoittavat:

``ROUNDS``
    ``parsed/<map_demo_id>/rounds.parquet`` -- pitkä taulu, **kaksi riviä per
    kierros**, yksi kummallekin joukkueelle. Sisältää vain ``parse``-vaiheen
    *havaitsemat* arvot, ei johdoksia.
``TICKS``
    ``parsed/<map_demo_id>/ticks.parquet`` -- rivi per (pelaaja, kierros,
    näytepiste).
``EVENTS``
    ``parsed/<map_demo_id>/events.parquet`` -- rivi per utility-tapahtuma.
    Heitto ja räjähdys ovat kaksi riviä, jotka yhdistää ``grenade_no``.
``LINEUPS``
    ``parsed/<map_demo_id>/lineups.parquet`` -- rivi per (kokoonpano, pelaaja).
    Pelaajan nimi ja hänen klaaninimensä, eli joukkueen **identiteetti** --
    ei kierroskohtainen havainto.
``DEATHS``
    ``parsed/<map_demo_id>/deaths.parquet`` -- rivi per kuolema. Uhri ja
    ampuja molemmat alueineen ja koordinaatteineen; kuolemalla on **kaksi
    toimijaa**, joten se ei mahdu ``EVENTS``-taulun yhden toimijan muotoon.
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
    "ARMED_COLUMN",
    "ARMORED_COLUMN",
    "MONEY_DISTRIBUTION_COLUMN",
    "TICKS",
    "EVENTS",
    "LINEUPS",
    "DEATHS",
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


#: Kalustolaskurin sarakkeen nimi. Vakiona, koska sitä lukevat adapteri (joka
#: laskee luvun), ``stages.parse`` (joka raportoi sen jakauman), ``classify``
#: (joka lukee siitä ehdon A) ja testit -- kovakoodattuina merkkijonoina ne
#: erkanisivat toisistaan huomaamatta.
ARMED_COLUMN = "players_armed_buy_end"

#: Panssarilaskurin sarakkeen nimi. Sama peruste vakiolle kuin
#: :data:`ARMED_COLUMN`illa: adapteri laskee, ``stages.parse`` raportoi
#: jakauman, ``aggregate`` lukee sen raporttiin ja testit vertaavat.
#:
#: **EI SAMA LUKU KUIN** :data:`ARMED_COLUMN`. Juuri se sekaannus maksoi Story
#: 2.3:n hyväksymisajossa yhden väärän rivin, joten ero sanotaan tässä ääneen:
#:
#: * ``players_armed_buy_end`` = panssari **JA** parannettu ase. Se on
#:   puolioston ehto A: kalibroitu, ja ``classify`` nojaa siihen.
#: * ``players_armored_buy_end`` = panssari, piste. Se vastaa kysymykseen
#:   "monellako oli panssari", joka on pistoolikierroksen tärkein
#:   yksittäinen havainto.
#:
#: Molemmat ovat **hallussapitoa eivätkä ostoja**: panssari säilyy kierroksen
#: yli hengissä selvinneellä, myös vaurioituneena. Pistoolikierros (1 ja 13)
#: on poikkeus -- puoliaika alkaa puhtaalta pöydältä eikä perintää ole, joten
#: siellä luku on ostohavainto.
#:
#: Pistoolikierroksella ne myös eroavat eniten: 800 dollarin aloitusrahalla
#: kevlar (650) ja parannettu ase eivät mahdu samaan ostokseen, joten
#: aseistettuja on käytännössä 0, vaikka kaikilla viidellä olisi kevlar.
#: Mitattu neljästä MatureMayhem-demosta 2026-08-30: kaikilla kahdeksalla
#: pistoolikierroksella aseistettuja 0, panssaroituja 1--5. Sääntö se ei ole:
#: poimittu ase riittää aseistamaan, ja samassa aineistossa vastustajan
#: Anubis-kierroksella 13 luvut ovat 3 ja 1.
ARMORED_COLUMN = "players_armored_buy_end"

#: Pelaajakohtaisen rahajakauman sarakkeen nimi. Sama syy vakiolle kuin yllä:
#: adapteri kirjoittaa, ``classify`` lukee, testit vertaavat.
MONEY_DISTRIBUTION_COLUMN = "money_players_buy_end"


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
    "money_buy_end": pl.Int32,  # $ jäljelle jäänyt saldo ostoajan lopussa
    "money_spent": pl.Int32,  # $ tällä kierroksella käytetty raha (cash_spent)
    "equip_buy_end": pl.Int32,  # $ current_equip_value-summa ostoajan lopussa
    "equip_round_start": pl.Int32,  # $ round_start_equip_value-summa
    "players_buy_end": pl.Int32,  # pelaajat, joiden arvot olivat luettavissa
    # Edellisten rahasaldot yksi pelaaja kerrallaan, laskevassa
    # suuruusjärjestyksessä. SAMA JOUKKO kuin money_buy_endin summassa ja
    # players_buy_endin jakajassa, joten pituus on aina players_buy_end.
    #
    # MIKSI SUMMA EI RIITÄ: puoliosto erotetaan forcesta sillä, moniko pelaaja
    # pystyy normaaliin ostoon seuraavalla kierroksella, ja se on
    # pelaajakohtainen kysymys. Joukkue jolla yhdellä on 5 000 ja neljällä
    # nolla saa saman summan kuin joukkue jolla kaikilla on 1 000, mutta
    # edellisessä neljä viidestä ei voi ostaa mitään. Keskiarvo antaa myös
    # mahdottomia lukuja: mitattu kierros 19 CT näytti "30 $/pelaaja", kun
    # todelliset saldot olivat 0, 0, 50, 50, 50 -- kaikki hinnat ovat
    # viidenkymmenen monikertoja, joten 30 ei voi olla kenenkään saldo.
    #
    # JÄRJESTYS ON LAJITELTU eikä pelaajajärjestys: rivillä ei ole pelaajien
    # tunnisteita, joten alkion paikka ei kerro kenestä on kyse, ja lajittelu
    # tekee lukemasta toistettavan tickin rivijärjestyksestä riippumatta.
    #
    # null aina ja vain silloin, kun players_buy_end on null (ankkuriton
    # kierros). Tyhjää listaa ei kirjoiteta: se väittäisi havainnoksi sen,
    # ettei ketään ollut.
    MONEY_DISTRIBUTION_COLUMN: pl.List(pl.Int32),
    # Edellisistä ne, joilla oli PANSSARI JA VÄHINTÄÄN YKSI ASE HALLUSSA
    # ostoajan lopussa. Luettu tavaraluettelosta ja m_ArmorValuesta, ei
    # varustearvosta: varustearvo on ase + panssari + kranaatit yhtenä lukuna
    # eikä erota asetta ilmaisesta pistoolista ja kahdesta valosta. HALLUSSAPITO
    # EIKÄ OSTOS: säästetty tai poimittu kivääri laskeutuu samoin kuin ostettu.
    # Joukkuesumma ei kerro tätä: kaksi AK:ta ja kolme tyhjää antaa saman summan
    # kuin viisi puolinaista. Aina 0..players_buy_end; null kun havaintoa ei
    # ole lainkaan -- nolla tarkoittaa "kukaan ei ollut aseistettu".
    # Null myös silloin, kun yhdenkin pelaajan panssari tai tavaraluettelo on
    # lukukelvoton: osittainen luku näyttäisi säästöltä eikä lukuvirheeltä.
    ARMED_COLUMN: pl.Int32,
    # Edellisistä ne, joilla oli PANSSARI -- aseesta riippumatta. Sama tick,
    # sama pelaajajoukko ja sama m_ArmorValue-lukema kuin ylläkin; ero on vain
    # ehdossa. "Panssaroitu" on m_ArmorValue > 0; kypärää ei eroteta eikä
    # vaurioitunutta panssaria ehjästä, koska analyysi puhuu kevlarista ja
    # kypärä on eri havainto.
    #
    # HALLUSSAPITO EIKÄ OSTOS, kuten yllä olevallakin: panssari säilyy
    # kierroksen yli hengissä selvinneellä, joten luku kertoo mitä pelaajilla
    # oli eikä mitä he ostivat. Pistoolikierroksella (1 ja 13) perintää ei ole,
    # joten siellä -- ja vain siellä -- se on ostohavainto.
    #
    # ASEISTETUT OVAT TÄMÄN OSAJOUKKO: aseistetun ehto sisältää panssarin,
    # joten players_armed_buy_end <= players_armored_buy_end aina.
    #
    # MIKSI OMA SARAKE EIKÄ YLLÄ OLEVAN YLEISTYS: ne vastaavat eri kysymyksiin
    # ja molempia tarvitaan. Yllä oleva on kalibroitu puolioston ehdoksi A ja
    # on tarkoituksella tiukka; tämä vastaa kysymykseen "monellako oli
    # panssari". Pistoolikierroksella yllä oleva on käytännössä 0 (800 $ ei
    # riitä sekä kevlariin että parannettuun aseeseen), joten sitä lukemalla
    # "5 kevlaria" ja "ei kevuja" näyttävät täysin samalta.
    #
    # Aina 0..players_buy_end. Null aina ja vain silloin, kun players_buy_end
    # on null tai yhdenkin luettavan pelaajan panssari on lukukelvoton --
    # osittainen luku näyttäisi säästöltä eikä lukuvirheeltä. HUOM: luettavuus
    # on tässä KAPEAMPI kuin ylläkin: tavaraluettelo ei kuulu ehtoon, koska
    # tämä laskuri ei lue sitä. Lukukelvoton tavaraluettelo tyhjentää siis
    # vain ylemmän sarakkeen, ei tätä.
    ARMORED_COLUMN: pl.Int32,
    "survivors": pl.Int32,  # elossa kierroksen lopussa
    "survivors_equip_prev": pl.Int32,  # $ edelliseltä kierrokselta säästynyt varustearvo
    "freeze_end_tick": pl.Int32,  # viimeinen round_freeze_end -tick, null jos puuttuu
    # Tick, jolta talousarvot yllä on luettu:
    #   max(freeze_end_tick,
    #       min(freeze_end_tick + [parse].buy_window_seconds,
    #           kierroksen ensimmäistä kuolemaa EDELTÄVÄ tick,
    #           kierroksen loppu))
    # Sama molemmilla saman kierroksen riveillä -- pelaaja- tai
    # joukkuekohtainen piste laskisi kuolleen pudottaman ja poimitun aseen
    # kahdesti. Null aina ja vain silloin, kun freeze_end_tick on null.
    #
    # Sarake on olemassa, jotta luku on tarkistettavissa demoa vasten: ilman
    # sitä freeze_end_tick väittäisi olevansa mittaushetki, ja juuri sellainen
    # valhe piilotti tämän vian alun perin (Story 1.9).
    #
    # MITÄ SE EI KERRO: yhtäsuuruus freeze_end_tickin kanssa on MONITULKINTAINEN.
    # Se tarkoittaa joko asetusta buy_window_seconds = 0 (mittaa ankkurista),
    # kuolemaa heti ankkurin jälkeen, tai varasääntöä, johon parse palasi kun
    # ostoajan tickiltä ei saatu yhtään pelaajaa. Rivin status on kaikissa
    # kolmessa "ok", eikä classify voi erottaa niitä -- erottelu on ajon
    # tulosteessa (stages.parse -> ParseDiagnostics), koska UNIT_STATUSES on
    # jaettu sopimus eikä sitä laajenneta yhden vaiheen tarpeeseen.
    #
    # classify EI lue tätä saraketta (ks. economy.CLASSIFY_COLUMNS): mittaus on
    # parsinnan vastuu, ja luokittelusääntö nojaa lukuihin eikä hetkeen, josta
    # ne luettiin. Sarake on jäljitettävyyttä varten, ja ajon tuloste kertoo
    # mittaushetkien jakauman.
    "buy_end_tick": pl.Int32,
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
# yhdistää grenade_no -- lentoradan oma tunniste, joka on yksikäsitteinen koko
# demossa. area ja x, y, z tarkoittavat event_kindin mukaan joko heitto- tai
# räjähdyspaikkaa. Utility mitataan heitoista, ei ostoista.
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
    # Lentoradan tunniste: juokseva numero demon sisällä, heiton tickin mukaan
    # järjestettynä. YKSIKÄSITTEINEN KOKO DEMOSSA, ei vain kierroksen sisällä,
    # ja heitto ja räjähdys jakavat sen -- se on niiden ainoa side.
    # Yksi demo: (grenade_no, event_kind) yksilöi rivin.
    # Monta demoa: (map_demo_id, grenade_no, event_kind) -- numero juoksee
    # demon sisällä, joten aggregate tarvitsee map_demo_idin mukaan.
    # MUOTO: alkaa nollasta ja kasvaa, mutta EI OLE YHTENÄINEN VÄLI 0..n-1.
    # Numerointi tehdään ennen kuin parse pudottaa lämmittelyn ja
    # puukkokierroksen rivit, joten valmiissa taulussa on aukkoja. Numeroa ei
    # siis saa käyttää indeksinä eikä sen suurinta arvoa kranaattien määränä.
    # VAKAA saman syötteen yli: jaksotus on deterministinen, joten
    # uudelleenparsinta antaa samat numerot.
    "grenade_no": pl.Int32,
    # Pelin oma entiteettitunniste. EI YKSILÖI KRANAATTIA: peli kierrättää
    # tunnisteet demon aikana ja myös SAMAN KIERROKSEN SISÄLLÄ. Mitattu:
    # inferno_vs_ryhmarama, kierros 11, tunniste 564 = kolme eri lentorataa
    # (molotov 9,2 s, flashbang 18,0 s, incendiary 64,2 s).
    # Sarake on tallessa siksi, että se on ainoa side takaisin demoon --
    # kranaatin löytää sillä katselimesta. Avaimena sitä ei saa käyttää.
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

# Kokoonpanotaulu: rivi per (kokoonpano, pelaaja) (Story 2.6).
#
# MIKSI OMA TAULU EIKÄ SARAKE TICKS-TAULUUN. ``TICKS`` on
# (pelaaja x kierros x näytepiste) -- neljässä demossa kymmeniä tuhansia
# rivejä, joilla nimi toistuisi. Nimi ei myöskään ole kierroskohtainen
# havainto: se on sama koko kartan ajan (mitattu 2026-08-30 viidellä demolla:
# nolla pelaajaa, joilla olisi useampi nimi tai klaani). Identiteetti kuuluu
# omaan tauluunsa, ja ``aggregate``n kokoonpanoluku halpeni samalla: se luki
# ennen koko ``ticks``-taulun saadakseen pelaajajoukon.
#
# KLAANI LUETAAN PELAAJAKOHTAISESTI, EI PUOLEN KAUTTA. Sama mittaus:
# ``parse_ticks(["team_clan_name"])`` antaa jokaiselle SteamID:lle täsmälleen
# yhden klaanin kaikilla ankkureilla, myös puoliajan vaihdon yli. Puolen
# (``team_num``) kautta luettuna sama arvo vaihtaa joukkuetta puoliajalla --
# ``team_num=2`` on 1. puoliajalla ``KALJUKOSTAJA`` ja 2. puoliajalla
# ``MatureMayhem``. Sitä ansaa ei saa astua.
#
# NIMI ON HAVAINTO EIKÄ JOHDOS. Puuttuva klaani on ``null``, ei tunniste eikä
# tyhjä merkkijono: raportti sanoo puuttumisen ääneen eikä keksi korviketta.
# SteamID64 (``player_id``) säilyy jokaisella rivillä nimen rinnalla -- nimi on
# luettavuutta varten, tunniste on ainoa jäljitettävä arvo.
LINEUPS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, liitosavain
    "lineup_key": pl.Utf8,  # kokoonpanotunniste, sama kuin muissa tauluissa
    "player_id": pl.Utf8,  # SteamID64
    # Pelaajan nimi demossa; null jos sitä ei saatu luettua.
    "player_name": pl.Utf8,
    # Pelaajan klaaninimi (``team_clan_name``); null jos sitä ei ole.
    "clan_name": pl.Utf8,
}


# Kuolemataulu: rivi per kuolema (Story 2.7).
#
# MIKSI OMA TAULU EIKÄ ``EVENTS``-TAULUUN. ``EVENTS``in konventio on, että
# rivin ``lineup_key``, ``side`` ja ``x, y, z`` kuvaavat **rivin oman
# joukkueen** yhtä toimijaa yhdessä paikassa. Kuolemalla on kaksi toimijaa, ja
# molempien paikka on merkityksellinen: "Luola kuolee" on uhrin alue, "Vihu
# meni secret pihalta" on ampujan. Kahtena rivinä se vaatisi ``thrower_id``:n
# ja ``grenade_no``:n uudelleennimeämisen, yhtenä rivinä viisi uutta
# nullable-saraketta, jotka olisivat null jokaisella kranaattirivillä ja
# päinvastoin -- eli juuri sen tiukan skeemavalidoinnin heikentämisen, johon
# koko sopimus nojaa.
#
# MOLEMMAT ALUEET OVAT HAVAINTOJA. ``user_last_place_name`` ja
# ``attacker_last_place_name`` tulevat samalta ``player_death``-tapahtumalta
# eivätkä ole napsautuksia, joten taulussa ei ole ``area_source``- eikä
# ``snap_distance``-saraketta -- ne ovat olemassa juuri kranaatin
# approksimaatiota varten. Mitattu 2026-08-30 ``Ancient_vs_kaljukostaja``:
# uhrin alue puuttui 0/151 tapahtumalta, ampujan 2/151.
#
# AMPUJATON KUOLEMA ON AITO TAPAUS. Putoaminen ja pommi tuottavat rivin, jolla
# jokainen ``attacker_*`` on null. Riviä ei pudoteta: uhri kuoli, ja se on
# havainto. Samat kaksi riviä ovat ne, joilta ampujan alue puuttuu -- ne ovat
# ``planted_c4``-kuolemia, joissa ampujaa ei ole lainkaan. Alue ei siis
# kadonnut: ampujaa ei ollut.
#
# EI JOHDETTUJA KÄSITTEITÄ. Taulussa ei ole tradea, entryä eikä duel-voittoa.
# Ne ovat tulkintaa, ja työnjako on: havainto koneelta, tulkinta ihmiseltä.
DEATHS: Schema = {
    "map_demo_id": pl.Utf8,  # {match_id}-{map_index}, liitosavain
    "round_raw": pl.Int32,  # demoparser2:n oma kierroslaskuri
    "round_no": pl.Int32,  # 1-pohjainen pelattu kierros
    "t_s": pl.Float64,  # s -- aika viimeisestä round_freeze_endistä
    # Uhri. Nämä kentät ovat rivin identiteetti: kuolema ilman uhria ei ole
    # kuolema, joten rivi, jolta ne puuttuvat, ei päädy tauluun lainkaan.
    "victim_id": pl.Utf8,  # SteamID64
    "victim_lineup_key": pl.Utf8,  # uhrin kokoonpano tällä kierroksella
    "victim_side": _SIDE,
    "victim_x": pl.Float32,
    "victim_y": pl.Float32,
    "victim_z": pl.Float32,
    # Pelin last_place_name kuolinhetkellä. Havainto, ei napsautus.
    "victim_area": pl.Utf8,
    # Ampuja. **Kaikki null tai ei yhtään**: ampujaton kuolema on aito tapaus
    # (putoaminen, pommi), ja puolikas ampuja olisi vika. Poikkeus on
    # ``attacker_area``, joka saa puuttua yksinään -- alue on havainto siinä
    # missä muutkin, ja sen puuttuminen on eri asia kuin ampujan puuttuminen.
    # Poikkeus on myös ``attacker_side`` ja sen mukana ``attacker_lineup_key``:
    # ne ovat kierroksen puolikuvauksesta johdettuja, eivät tapahtuman omia.
    "attacker_id": pl.Utf8,
    "attacker_lineup_key": pl.Utf8,
    "attacker_side": _SIDE,
    "attacker_x": pl.Float32,
    "attacker_y": pl.Float32,
    "attacker_z": pl.Float32,
    "attacker_area": pl.Utf8,
    # Ase pelin omalla nimellä (``ak47``, ``planted_c4``, ``knife_butterfly``).
    # Sellaisenaan eikä luokiteltuna: luokittelu olisi tulkintaa.
    "weapon": pl.Utf8,
}


# classify-vaiheen tallentamat päätöksen syötteet (AD-4): kaikki vertailuun
# käytetyt arvot, jotta raportin kierrosliite on tarkistettavissa demoa vasten.
# Kynnysarvot ovat dollareita per pelaaja, samat kuin [thresholds]-osiossa.
CLASSIFIED_INPUTS = pl.Struct(
    {
        "money_buy_end": pl.Int32,
        # Käytettävissä ollut raha = money_buy_end + money_spent. Se on
        # mukana perustelua ja tarkistusta varten; kalibrointi 2026-08-29
        # osoitti, että säännöt nojaavat jäljelle jääneeseen saldoon.
        "money_spent": pl.Int32,
        # Jakauma sellaisenaan, jotta kierroslistan rivi on tarkistettavissa
        # demoa vasten ilman uutta parsintaa. Ilman tätä lukija näkisi vain
        # laskurin "5/5 pystyy ostamaan" eikä voisi tarkistaa sitä.
        "money_players": pl.List(pl.Int32),
        "equip_buy_end": pl.Int32,
        "equip_round_start": pl.Int32,
        "survivors_prev": pl.Int32,
        "survivors_equip_prev": pl.Int32,
        "prev_round_won": pl.Boolean,
        # players = jakaja, jota per pelaaja -arvoissa oikeasti käytettiin;
        # players_readable = montako pelaajaa oli luettavissa. Ne eroavat vain,
        # jos havainto oli rajojen 1..roster_size ulkopuolella.
        "players": pl.Int32,
        "players_readable": pl.Int32,
        # Ehdon A havainto (ROUNDS.players_armed_buy_end) ja ehdon B kaksi
        # johdosta: häviöbonus, joka olisi voimassa jos tämä kierros hävitään,
        # ja niiden pelaajien määrä, joilla se ja taskuun jäänyt raha riittävät
        # normaaliin ostoon. players_can_buy on null, jos jakaumaa ei saatu.
        "players_armed": pl.Int32,
        "loss_bonus_if_lost": pl.Int32,
        "players_can_buy": pl.Int32,
        "full_equip_min": pl.Int32,
        # Force, puoliosto ja eco: kaikkien edellytys on ostettu summa
        # (force_buy_min). Sen jälkeen kaksi ehtoa, joiden MOLEMPIEN on
        # täytyttävä, jotta kierros on puoliosto:
        #   A  armed_players_min                 erottaa puolioston ECOSTA
        #   B  normal_buy_money_min +
        #      normal_buy_players_min            erottaa puolioston FORCESTA
        # Kumpikaan ei riitä yksin: mitattu inferno_vs_ryhmarama, kierrokset 6
        # (force) ja 10 (puoliosto), joilla ehto A on identtinen (5/5).
        "force_buy_min": pl.Int32,
        "armed_players_min": pl.Int32,
        "normal_buy_money_min": pl.Int32,
        "normal_buy_players_min": pl.Int32,
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
    "lineups": LINEUPS,
    "deaths": DEATHS,
    "classified": CLASSIFIED,
}


def _type_name(dtype: object) -> str:
    """Palauta luettava tyyppinimi virheilmoitusta varten."""
    return str(dtype)


def validate(
    df: pl.DataFrame,
    schema: Schema,
    name: str,
    advice: str | None = None,
) -> pl.DataFrame:
    """Tarkista, että ``df`` vastaa täsmälleen sopimusta ``schema``.

    Tarkistus on tiukka molempiin suuntiin: puuttuva sarake, ylimääräinen sarake
    ja väärä tyyppi ovat kaikki virheitä. Sarakkeiden järjestyksellä ei ole
    väliä.

    Args:
        df: Tarkistettava taulu.
        schema: Sopimus muodossa ``{sarakenimi: Polars-tyyppi}``.
        name: Taulun nimi virheilmoitusta varten, esimerkiksi ``"rounds"``.
        advice: Korvaava toimintaohje virheilmoituksen loppuun. Oletusohje
            puhuu kehittäjälle ("lisää sarake tai korjaa taulun tuottanut
            vaihe"), koska useimmiten sopimusta rikkoo koodi. Arkistosta
            luettu taulu on eri tilanne: sen on rikkonut ohjelman oma
            aiempi versio, eikä käyttäjä voi korjata sitä koodia
            muokkaamalla. Silloin kutsuja antaa oman ohjeensa, jotta
            väärä neuvo ei päädy käyttäjän silmille.

    Returns:
        Sama ``df`` muuttumattomana.

    Raises:
        SchemaError: Jos taulu ei vastaa sopimusta. Viesti nimeää tarkalleen
            puuttuvan, ylimääräisen tai väärätyyppisen sarakkeen.
    """
    actual = dict(df.schema)

    missing = [col for col in schema if col not in actual]
    if missing:
        listing = ", ".join(f"{col} ({_type_name(schema[col])})" for col in missing)
        raise SchemaError(
            f"Taulusta {name!r} puuttuu sarake: {listing}. "
            + (
                advice
                if advice is not None
                else "Lisää sarake tai korjaa taulun tuottanut vaihe -- "
                "sopimus on tiedostossa domain/schemas.py."
            )
        )

    extra = [col for col in actual if col not in schema]
    if extra:
        listing = ", ".join(f"{col} ({_type_name(actual[col])})" for col in extra)
        raise SchemaError(
            f"Taulussa {name!r} on ylimääräinen sarake: {listing}. "
            + (
                advice
                if advice is not None
                else "Poista sarake tai lisää se sopimukseen tiedostossa "
                "domain/schemas.py."
            )
        )

    wrong = [
        (col, schema[col], actual[col]) for col in schema if actual[col] != schema[col]
    ]
    if wrong:
        listing = "; ".join(
            f"{col}: odotettiin {_type_name(exp)}, saatiin {_type_name(got)}"
            for col, exp, got in wrong
        )
        raise SchemaError(
            f"Taulun {name!r} sarakkeella on väärä tyyppi -- {listing}. "
            + (
                advice
                if advice is not None
                else "Muunna sarake oikeaan tyyppiin ennen kirjoitusta."
            )
        )

    return df
