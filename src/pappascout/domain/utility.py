"""Utilityn lentoratojen pelkistys ja räjähdyksen alue (AD-5).

Utility mitataan **heitoista, ei ostoista**: utilityä dropataan, joten ostaja ja
heittäjä voivat olla eri pelaajat. Heittoa ei kuitenkaan saa tapahtumasta --
``grenade_thrown``-tapahtumaa ei ole olemassa -- vaan lentoradoista, jotka
demoparser2 palauttaa **rivi per kranaatti per tick**. Ancientissa niitä on
1 553 329, ja siksi tämän moduulin tärkein tehtävä on pelkistää rata kahteen
pisteeseen heti: radan ensimmäinen piste on heitto, viimeinen räjähdys.

Miksi pelkkä ``grenade_entity_id`` ei riitä
-------------------------------------------
Peli **kierrättää entiteettitunnisteet**. Ancientissa 374 lentorataa mahtuu 187
tunnisteeseen: sama ``grenade_entity_id`` on ensin molotov kierroksella 2 ja
sitten HE kierroksella 14. Pelkkä ``group_by(grenade_entity_id)`` yhdistäisi ne
yhdeksi kranaatiksi, jonka heitto olisi ensimmäisestä ja "räjähdys"
viimeisestä -- eri kierrokselta, eri pelaajalta, eri kartan puolelta. Rata on
siksi katkaistava **yhtenäisiin jaksoihin**: tunnisteen, heittäjän tai tyypin
vaihtuminen aloittaa uuden kranaatin, samoin tickeihin jäävä aukko.

Kierros ei pelasta tunnistetta. Pitkään näytti siltä, että kierrätys tapahtuu
vain kierrosten välillä ja että ``(round_no, grenade_entity_id)`` riittäisi
avaimeksi. Liigademot osoittivat toisin: ``inferno_vs_ryhmarama`` kierroksella
11 tunniste 564 kantaa **kolme eri lentorataa** saman kierroksen sisällä --
molotov 9,2 s, flashbang 18,0 s ja incendiary 64,2 s. Jaksotus erottaa ne
oikein, mutta pari ei yksilöi niitä -- siksi jokainen rata saa oman
``grenade_no``:nsa, joka on yksikäsitteinen koko demossa.

Miksi koordinaatiton rivi ei ole rata
-------------------------------------
Kranaatilla on rivejä myös silloin, kun se on pelaajan repussa: tyyppi on
``CSmokeGrenade`` (ei ``...Projectile``) ja ``x, y, z`` ovat tyhjiä. Ancientin
1,55 miljoonasta rivistä 1,34 miljoonaa on tällaisia. Ne eivät ole lentorataa
eivätkä heittoja, joten ne suodatetaan pois ennen jaksotusta.

Räjähdyksen alue on johdettu, heiton alue havaittu
--------------------------------------------------
Nämä kaksi eivät ole samaa tietoa, eikä niitä saa laskea samalla tavalla.

**Heittäjällä** on oma ``m_szLastPlaceName`` samalta tickiltä, joten heiton
alue luetaan suoraan häneltä. Se on havainto, eikä sitä johdeta mistään --
tämä moduuli ei ole heiton polulla lainkaan.

**Kranaatilla** ei ole ``last_place_name``-kenttää, joten räjähdyksen alue on
pääteltävä koordinaateista. Menetelmä on **pistepilvi**
(:func:`build_point_cloud`, :func:`nearest_cells`): demon omista tickeistä
kootaan ruudukko siitä, missä pelaajat ovat kartalla oikeasti seisoneet ja mikä
alue kussakin kohdassa on, ja räjähdys nimetään lähimmän ruudun alueella.

Miksi ei lähin elossa oleva pelaaja
------------------------------------
Story 2.2 johti räjähdysalueen lähimmästä elossa olevasta pelaajasta. Se ei
ollut epätarkka vaan **rakenteellisesti väärä**: savu heitetään sinne, missä
ketään ei ole -- juuri siksi, että se estää näkyvyyden ja pakottaa
rotaatioita. Proxy mittasi siis päinvastaista kuin piti, ja **42 %
räjähdyksistä jäi kokonaan ilman aluetta** (mitattu neljästä liigademosta,
1 716 räjähdystä). Pistepilvellä osuus on 6,4 %.

Pistepilvessä lähde on pelin oma aluemäärittely (``env_cs_place``) eikä
naapuripelaaja. Menetelmää **ei jätetä rinnalle varalähteeksi**: kaksi
menetelmää tekisi rivistä tulkitsemattoman, koska lukija ei näkisi kummalla
alue nimettiin.

Kynnys ei poistu
----------------
"Lähin ruutu löytyy aina" ei ole kattavuutta. Mitattu maksimietäisyys
kuudessa demossa on 1 074 yksikköä; ilman kynnystä raportti väittäisi aluetta
räjähdykselle, joka tapahtui kaukana kaikesta, missä yksikään pelaaja on
koskaan seissyt. ``[parse].area_snap_units`` on siksi tallella ja
**pakollinen**, ja se on kalibroitu pistepilveä varten uudelleen: kynnyksellä
256 alueen saa 2 428/2 544 räjähdyksestä eli 95,4 %.

Etäisyys **säilyy silloinkin**, kun se ylittää kynnyksen: ``area`` jää
tyhjäksi mutta ``snap_distance`` kertoo kuinka kaukaa alue olisi otettu. Ilman
sitä "kaukana kaikesta" ja "pistepilvi oli tyhjä" näyttäisivät samalta.

Ero näkyy taulussa asti: ``EVENTS.area_source`` erottaa havainnon arviosta ja
``snap_distance`` kertoo arvion etäisyyden. Ilman niitä raportti esittäisi
600 yksikön päästä poimitun calloutin yhtä varmana kuin heittäjän oman
alueen.

Moduuli on puhdas: ei tiedostoja, ei demoparser2:ta, ei asetuksia. Pelin omat
luokkanimet (``CSmokeGrenadeProjectile``) eivät esiinny täällä -- adapteri
kääntää ne ennen kutsua, jotta tämä logiikka pysyy testattavana käsin
rakennetuilla radoilla. Sama koskee pistepilveä: adapteri lukee tickit, tämä
moduuli pelkistää ne ruudukoksi.
"""

from __future__ import annotations

import math

import polars as pl

__all__ = [
    "TRAJECTORY_COLUMNS",
    "ENDPOINT_COLUMNS",
    "CLOUD_OBSERVATION_COLUMNS",
    "CLOUD_CELL_COLUMNS",
    "NEAREST_POINT_COLUMNS",
    "NEAREST_RESULT_COLUMNS",
    "THROWN",
    "DETONATE",
    "MAX_TRAJECTORY_GAP_SECONDS",
    "NEAREST_CHUNK_POINTS",
    "build_point_cloud",
    "empty_point_cloud",
    "flight_point",
    "grenade_endpoints",
    "nearest_cells",
    "trajectory_gap_ticks",
]

#: Heiton tapahtumalaji (``EVENT_KINDS[0]``).
THROWN = "grenade_thrown"
#: Räjähdyksen tapahtumalaji (``EVENT_KINDS[1]``).
DETONATE = "grenade_detonate"

# Vastaavuutta EVENT_KINDS-luetteloon ei tarkisteta moduulitason assertilla --
# se katoaisi python -O:lla juuri silloin kun sitä tarvittaisiin. Tarkistus on
# testissä test_utility.py.

#: Sarakkeet, jotka lentoratataulussa on oltava. Nimet ovat pappascoutin omia,
#: eivät demoparser2:n: ``steamid`` on jo käännetty ``thrower_id``:ksi.
#: ``grenade_type``a ei tulkita täällä lainkaan -- se on jaksotuksen avain ja
#: kulkee muuttumattomana läpi, ja pelin luokkanimen kääntäminen kanoniseksi
#: (``smoke``, ``flashbang``, ...) on adapterin työtä.
TRAJECTORY_COLUMNS: tuple[str, ...] = (
    "grenade_entity_id",
    "grenade_type",
    "thrower_id",
    "tick",
    "x",
    "y",
    "z",
)

#: Sarakkeet, jotka :func:`grenade_endpoints` palauttaa.
#:
#: ``grenade_no`` on lentoradan juokseva numero demossa ja **ainoa luotettava
#: parin avain**: ``grenade_entity_id`` kierrätetään -- myös saman kierroksen
#: sisällä -- joten se ei yksilöi kranaattia. Numero on yksikäsitteinen
#: **koko demossa**, ei vain kierroksen sisällä: kierroskohtainen juokseva
#: numero näyttäisi yksikäsitteiseltä, mutta pettäisi heti kun aggregointi
#: liittää kahden kierroksen utilityn yhteen kehykseen.
#:
#: Numero **päätyy ``EVENTS``-tauluun sellaisenaan** (Story 1.8): se on ainoa
#: sarake, jolla heitto ja räjähdys yhdistyvät, ja adapteri käyttää sitä myös
#: liittääkseen kierroksen, puolen ja alueen molempiin riveihin samalla
#: päätöksellä.
#:
#: Muoto: numerointi **alkaa nollasta** ja kasvaa heiton tickin mukaan. Se on
#: yksikäsitteinen mutta ei yhtenäinen väli ``0..n-1``: heittäjätön rata
#: pudotetaan jo täällä, ja ``stages.parse`` pudottaa lisäksi
#: numeroimattomien kierrosten rivit, joten valmiissa taulussa on aukkoja.
#: Numero ei siis ole indeksi eikä sen suurin arvo ole kranaattien määrä.
ENDPOINT_COLUMNS: tuple[str, ...] = (
    "grenade_no",
    "grenade_entity_id",
    "grenade_type",
    "thrower_id",
    "event_kind",
    "tick",
    "x",
    "y",
    "z",
)

#: Suurin radan sisään jäävä aukko **sekunteina**, jonka jälkeen rata on yhä
#: sama kranaatti.
#:
#: Ancientin 374 radasta yksikään ei ole katkonainen, joten nolla riittäisi
#: havaintoon. Pieni pelivara on silti turvallisempi: yksi hukkuva tick
#: **katkaisisi** radan kahdeksi kranaatiksi ja keksisi kokonaisen ylimääräisen
#: heitto-räjähdys-parin, kun taas kahden eri kranaatin yhdistäminen vaatisi,
#: että sama tunniste vapautuu ja otetaan uudelleen käyttöön tässä ajassa
#: samalta pelaajalta samalla kranaattityypillä. Keksitty rivi on pahempi
#: virhe kuin kadonnut, ja tämä raja sulkee sen pois.
#:
#: Raja on **aikaa eikä tickejä**: 128-tickisessä demossa kahdeksan tickiä
#: olisi puolet lyhyempi hetki kuin 64-tickisessä, ja sama lento voisi
#: pilkkoutua kahdeksi kranaatiksi vain siksi että palvelin ajoi tiheämmin.
MAX_TRAJECTORY_GAP_SECONDS = 0.125

#: Tyhjän tuloksen tyypit. Polars päättelisi tyhjästä listasta ``Null``-tyypin,
#: jolloin adapterin jatkokäsittely kaatuisi vasta myöhemmin.
_ENDPOINT_SCHEMA: dict[str, pl.DataType | pl.DataTypeClass] = {
    "grenade_no": pl.Int32,
    "grenade_entity_id": pl.Int32,
    "grenade_type": pl.Utf8,
    "thrower_id": pl.Utf8,
    "event_kind": pl.Utf8,
    "tick": pl.Int32,
    "x": pl.Float32,
    "y": pl.Float32,
    "z": pl.Float32,
}

#: Sarakkeet, jotka pistepilven havaintotaulussa on oltava.
#:
#: Nimet ovat pappascoutin omia eivätkä demoparser2:n: adapteri on jo kääntänyt
#: ``CCSPlayerPawn.m_szLastPlaceName``in ``area``ksi ja ``m_lifeState``in
#: ``is_alive``ksi. Koordinaatit ovat samat ``x, y, z`` kuin lentoradoilla,
#: jolloin :func:`flight_point` kelpaa molemmille eikä koordinaatittoman rivin
#: sääntöä ole kahdessa paikassa.
CLOUD_OBSERVATION_COLUMNS: tuple[str, ...] = ("x", "y", "z", "area", "is_alive")

#: Sarakkeet, jotka :func:`build_point_cloud` palauttaa -- ja jotka
#: ``CALLOUT_CLOUD``-taulussa ovat ``map_demo_id``:n lisäksi.
#:
#: ``cell_x``, ``cell_y`` ja ``cell_z`` ovat ruudun **indeksejä** eivätkä
#: koordinaatteja: koordinaatin saa kertomalla ruudun särmällä. Indeksi eikä
#: keskipiste siksi, että se on tarkka kokonaisluku -- keskipiste tallentaisi
#: saman tiedon liukulukuna, jonka pyöristys voisi siirtää ruutua.
CLOUD_CELL_COLUMNS: tuple[str, ...] = (
    "cell_x",
    "cell_y",
    "cell_z",
    "area",
    "observations",
)

#: Sarakkeet, jotka :func:`nearest_cells`in syötetaulussa on oltava.
#: ``point_id`` on kutsujan oma avain (``EVENTS.grenade_no``), joka palautuu
#: tuloksessa sellaisenaan -- funktio ei tunne kranaatteja.
NEAREST_POINT_COLUMNS: tuple[str, ...] = ("point_id", "x", "y", "z")

#: Sarakkeet, jotka :func:`nearest_cells` palauttaa.
NEAREST_RESULT_COLUMNS: tuple[str, ...] = ("point_id", "area", "distance")

#: Pistepilven tyypit. Sama peruste kuin :data:`_ENDPOINT_SCHEMA`illa: tyhjästä
#: listasta Polars päättelisi ``Null``-tyypin.
_CLOUD_SCHEMA: dict[str, pl.DataType | pl.DataTypeClass] = {
    "cell_x": pl.Int32,
    "cell_y": pl.Int32,
    "cell_z": pl.Int32,
    "area": pl.Utf8,
    "observations": pl.Int32,
}

#: Montako pistettä kerrallaan verrataan pistepilveen (:func:`nearest_cells`).
#:
#: Vertailu on ristitulo: jokainen piste jokaista ruutua vasten, ja sen perään
#: lajittelu. Se on tarkka eikä nojaa hakupuuhun, mutta rivimäärä on tulo.
#:
#: **Paloittelu ei ole optimointi vaan yläraja.** Mitatussa aineistossa se
#: leikkaa huipun 4,8 miljoonasta rivistä 2,7 miljoonaan (455 räjähdystä x
#: 10 522 ruutua vs. 256 x 10 522) eli 44 % -- ei suuruusluokkaa. Sen arvo on
#: siinä, ettei huippu **kasva** kranaattien määrän mukana: demo, jossa
#: heitetään 2 000 kranaattia, mahtuu samaan rajaan.
#:
#: **Hinta on mitattu, ei mitätön.** Koko haku (ristitulo + lajittelu) vie
#: 285-580 ms per demo, kun pilvessä on 7 700-10 500 ruutua ja räjähdyksiä
#: 373-465. Se on muutama prosentti demon 6-12 sekunnin parsinnasta, mutta se
#: on kertaluokkia enemmän kuin nolla, ja hakupuu olisi nopeampi -- vain ei
#: yhtä yksinkertainen eikä yhtä helposti todeksi todistettava.
#:
#: Tulokseen palan koko **ei vaikuta**: lähin ruutu on sama riippumatta siitä,
#: missä erässä piste käsiteltiin, ja :func:`nearest_cells` hylkää
#: kaksoisavaimet, jotka voisivat monistua palojen rajalla.
NEAREST_CHUNK_POINTS = 256


def flight_point() -> pl.Expr:
    """Lauseke, joka on tosi vain oikealla lentoradan pisteellä.

    Kranaatti saa rivin myös pelaajan repussa ollessaan, ja silloin
    koordinaatit puuttuvat. Sama lauseke on käytettävä molemmissa suunnissa:
    :func:`grenade_endpoints` pitää nämä rivit ja adapteri poimii reppurivit
    sen komplementista. Jos suodattimet erkanisivat -- toinen tarkistaisi vain
    ``null``:in ja toinen myös NaN:in -- osa riveistä olisi kummassakin tai ei
    kummassakaan, ja tulikranaatin tyypin haku etsisi repusta lentoradan
    riveiltä.
    """
    return pl.all_horizontal(
        (pl.col(name).is_not_null() & pl.col(name).is_finite()).fill_null(False)
        for name in ("x", "y", "z")
    )


def trajectory_gap_ticks(tick_rate: float) -> int:
    """:data:`MAX_TRAJECTORY_GAP_SECONDS` tickeinä tällä tickratella.

    Args:
        tick_rate: Demon tickrate.

    Returns:
        Vähintään 1. Nolla tarkoittaisi, ettei aukkoa sallita lainkaan, jolloin
        yksi hukkuva tick keksisi ylimääräisen kranaatin.

    Raises:
        ValueError: Jos tickrate ei ole positiivinen äärellinen luku.
    """
    if not (tick_rate > 0 and math.isfinite(tick_rate)):
        raise ValueError(
            f"Tickrate {tick_rate!r} ei kelpaa lentoratojen jaksotukseen: sen "
            "on oltava positiivinen ja äärellinen."
        )
    return max(1, round(MAX_TRAJECTORY_GAP_SECONDS * tick_rate))


def grenade_endpoints(
    trajectories: pl.DataFrame, *, max_gap_ticks: int
) -> tuple[pl.DataFrame, int]:
    """Pelkistä lentoradat kahteen pisteeseen per kranaatti.

    Args:
        trajectories: Lentoratataulu, sarakkeet vähintään
            :data:`TRAJECTORY_COLUMNS`. Rivi per kranaatti per tick;
            koordinaatittomat rivit (kranaatti pelaajan repussa) saavat olla
            mukana, ne suodatetaan täällä.
        max_gap_ticks: Suurin tickiaukko, jonka yli rata on yhä sama kranaatti.
            Laske se :func:`trajectory_gap_ticks`illä demon omasta
            tickratesta -- kiinteä tickimäärä olisi eri mittainen hetki eri
            tickratella.

    Returns:
        ``(päätepisteet, ohitetut)``.

        ``päätepisteet`` on pitkä taulu, sarakkeet :data:`ENDPOINT_COLUMNS`:
        yksi ``grenade_thrown``-rivi jokaisesta kranaatista ja
        ``grenade_detonate``-rivi niistä, joiden rata on yhtä pistettä pidempi.
        Jokainen rata saa oman ``grenade_no``:nsa, joka on yksikäsitteinen
        koko taulussa ja **sama radan molemmilla riveillä** -- se on heiton ja
        räjähdyksen ainoa side. Numerointi on vakaa: sama syöte antaa samat
        numerot, koska jaksotus ja sen lajitteluavain ovat deterministisiä.
        **Yhden pisteen rata ei tuota räjähdystä**: se on ainoa radasta itsestään
        luettavissa oleva merkki siitä, ettei kranaatti koskaan lentänyt --
        keksitty räjähdys samaan pisteeseen väittäisi savua siellä, missä sitä
        ei ollut.

        ``ohitetut`` on niiden ratojen määrä, joilta puuttuu heittäjä. Ne
        pudotetaan kokonaan (myös räjähdys), koska riviä ei voi kohdistaa
        joukkueelle -- mutta niiden määrä raportoidaan, jottei utility katoa
        hiljaa.

    Raises:
        ValueError: Jos taulusta puuttuu sarake. Ilman tarkistusta tulos olisi
            tyhjä taulu, joka näyttäisi demolta ilman utilityä.
    """
    missing = [name for name in TRAJECTORY_COLUMNS if name not in trajectories.columns]
    if missing:
        raise ValueError(
            f"Lentoratataulusta puuttuu sarake: {', '.join(missing)}. "
            f"Odotetut sarakkeet ovat {', '.join(TRAJECTORY_COLUMNS)}."
        )

    flight = trajectories.select(TRAJECTORY_COLUMNS).filter(
        pl.col("grenade_entity_id").is_not_null()
        & pl.col("tick").is_not_null()
        # Tyhjä tyyppi ei kelpaa: se on EVENTS-sopimuksessa pakollinen, joten
        # se kaataisi koko demon validoinnissa yhden rikkinäisen rivin takia.
        & pl.col("grenade_type").is_not_null()
        & flight_point()
    )
    if flight.is_empty():
        return pl.DataFrame(schema=_ENDPOINT_SCHEMA), 0

    runs = _aggregate_runs(flight, max_gap_ticks)

    without_thrower = runs.filter(pl.col("thrower_id").is_null())
    runs = runs.filter(pl.col("thrower_id").is_not_null())
    if runs.is_empty():
        return pl.DataFrame(schema=_ENDPOINT_SCHEMA), without_thrower.height

    # Numerointi on tunnisteen koko määritelmä, ja kaksi asiaa on pidettävä
    # yhtä aikaa totena. **Yksikäsitteisyys**: rivi-indeksi juoksee koko
    # demon yli, joten sama numero ei voi osua kahdelle radalle edes saman
    # kierroksen sisällä -- juuri se rikkoi vanhan
    # (round_no, grenade_entity_id) -avaimen. **Vakaus**: lajitteluavain
    # (throw_tick, grenade_entity_id) on yksikäsitteinen, koska saman
    # tunnisteen jaksot ovat aikajärjestyksessä eivätkä voi alkaa samalta
    # tickiltä. Järjestys ei siis riipu lajittelun vakaudesta, ja sama demo
    # samoilla asetuksilla antaa samat numerot joka ajolla -- muuten arkiston
    # uudelleenparsinta näyttäisi muutokselta.
    runs = runs.sort("throw_tick", "grenade_entity_id").with_row_index(
        "grenade_no"
    )

    result = pl.concat(
        [
            _endpoint_rows(runs, THROWN, "throw"),
            _endpoint_rows(runs.filter(pl.col("points") > 1), DETONATE, "detonate"),
        ]
    )
    # Järjestys on osa sopimusta: heitto tulee aina ennen räjähdystään. Pelkkä
    # tick riittäisi oikeassa demossa, mutta ei ole invariantti -- laji on
    # siksi eksplisiittinen avain eikä nojaa merkkijonojen aakkosjärjestykseen,
    # jossa "grenade_detonate" tulisi ennen "grenade_thrownia".
    result = result.sort(
        "grenade_no",
        pl.col("event_kind").replace_strict({THROWN: 0, DETONATE: 1}, return_dtype=pl.Int8),
        "tick",
    )

    return result.select(ENDPOINT_COLUMNS).cast(_ENDPOINT_SCHEMA), without_thrower.height


def empty_point_cloud() -> pl.DataFrame:
    """Tyhjä pistepilvi sopimuksen tyypeillä.

    Tyhjä on **kelvollinen tulos**, ei virhe: demo, josta ei saatu yhtään
    elossa-riviä nimetyllä alueella, on aidosti pilvetön. Sopimuksen mukainen
    tyhjä taulu on silti pakko rakentaa tyypeistä eikä tyhjästä listasta --
    Polars päättelisi jälkimmäisestä ``Null``-tyypin, ja kirjoitus kaatuisi
    vasta arkistoon asti.
    """
    return pl.DataFrame(schema=_CLOUD_SCHEMA)


def build_point_cloud(
    observations: pl.DataFrame, *, grid_units: int
) -> pl.DataFrame:
    """Pelkistä demon tickit ruudukoksi: missä on seisty ja mikä alue se on.

    Ruudukko on **demon oma**, ei karttakohtainen arkistotaulu. Perustelu on
    toistettavuus: karttuva taulu antaisi samalle demolle eri tuloksen sen
    mukaan, mitä muita demoja arkistossa sattuu olemaan, eikä ``params_hash``
    voisi kattaa sitä.

    Ruudun **alue on moodi** eikä ensimmäinen havainto: ruudun reunalla on
    aina muutama rivi naapurialueelta, ja ensimmäinen rivi olisi kiinni siinä,
    missä järjestyksessä demoparser2 tickit antoi. Tasatilanne ratkeaa alueen
    nimen aakkosjärjestyksellä, jotta sama demo antaa aina saman pilven.

    Args:
        observations: Rivi per (pelaaja, tick), sarakkeet vähintään
            :data:`CLOUD_OBSERVATION_COLUMNS`. Kuolleet, alueettomat ja
            koordinaatittomat rivit saavat olla mukana -- ne suodatetaan
            täällä, jotta suodatussääntö on yhdessä paikassa.
        grid_units: Ruudun särmä pelin yksiköissä
            (``[parse].callout_grid_units``).

    Returns:
        Taulu sarakkeilla :data:`CLOUD_CELL_COLUMNS`, järjestettynä ruudun
        koordinaateilla. ``observations`` on ruudun **kaikki** havainnot, ei
        vain voittaneen alueen -- se kertoo, kuinka vahvasti ruutu on nähty.

    Raises:
        ValueError: Jos sarake puuttuu tai ``grid_units`` ei ole positiivinen
            äärellinen luku. Ilman tarkistusta tulos olisi tyhjä pilvi, joka
            näyttäisi demolta, jossa kukaan ei liikkunut.
    """
    if not (grid_units > 0 and math.isfinite(grid_units)):
        raise ValueError(
            f"Ruudun koko {grid_units!r} ei kelpaa pistepilveen: sen on oltava "
            "positiivinen ja äärellinen."
        )
    missing = [
        name for name in CLOUD_OBSERVATION_COLUMNS if name not in observations.columns
    ]
    if missing:
        raise ValueError(
            f"Pistepilven havaintotaulusta puuttuu sarake: {', '.join(missing)}. "
            f"Odotetut sarakkeet ovat {', '.join(CLOUD_OBSERVATION_COLUMNS)}."
        )

    usable = observations.select(CLOUD_OBSERVATION_COLUMNS).filter(
        pl.col("is_alive").fill_null(False)
        # Nimetön alue ei kelpaa pilveen: ruutu, jonka nimi on "ei nimeä",
        # nimeäisi räjähdyksen tyhjäksi ja näyttäisi silti osumalta -- eli
        # rivi ei erottuisi siitä, ettei aluetta saatu lainkaan.
        #
        # **Tyhjä ja pelkkiä välilyöntejä oleva nimi ovat sama asia kuin
        # null.** Adapteri muuttaa pelin tyhjän merkkijonon jo null:iksi, mutta
        # sääntö on täällä eikä siellä: tämä funktio on julkinen ja sen
        # sopimus on "alueeton havainto ei päädy pilveen". Jos ehto olisi vain
        # adapterissa, toinen kutsuja saisi ruudun nimeltä ``" "``.
        & (pl.col("area").str.strip_chars().str.len_chars() > 0).fill_null(False)
        & flight_point()
    )
    if usable.is_empty():
        return empty_point_cloud()

    cells = usable.select(
        (pl.col("x") // grid_units).cast(pl.Int32).alias("cell_x"),
        (pl.col("y") // grid_units).cast(pl.Int32).alias("cell_y"),
        (pl.col("z") // grid_units).cast(pl.Int32).alias("cell_z"),
        pl.col("area"),
    )
    # Kaksi vaihetta: ensin (ruutu, alue) -> havaintoja, sitten ruutua kohden
    # eniten havaintoja saanut alue. Lajittelu on osa vastausta eikä
    # esitystapa: se on ainoa asia, joka tekee moodista deterministisen
    # tasatilanteessa.
    per_area = cells.group_by("cell_x", "cell_y", "cell_z", "area").len()
    return (
        per_area.sort(
            ["cell_x", "cell_y", "cell_z", "len", "area"],
            descending=[False, False, False, True, False],
        )
        .group_by("cell_x", "cell_y", "cell_z", maintain_order=True)
        .agg(
            pl.col("area").first(),
            pl.col("len").sum().cast(pl.Int32).alias("observations"),
        )
        .sort("cell_x", "cell_y", "cell_z")
        .select(CLOUD_CELL_COLUMNS)
        .cast(_CLOUD_SCHEMA)
    )


def nearest_cells(
    points: pl.DataFrame,
    cloud: pl.DataFrame,
    *,
    grid_units: int,
    z_weight: float,
    z_tolerance_units: float,
    max_units: float | None,
) -> pl.DataFrame:
    """Nimeä jokainen piste lähimmän pistepilviruudun alueella.

    Etäisyys on painotettu::

        d = sqrt(dx^2 + dy^2 + (z_weight * max(0, |dz| - z_tolerance))^2)

    **Miksi toleranssi.** Pystyero maksaa ilman toleranssia myös silloin, kun
    se on täysin normaali: pistepilvi tallentaa pelaajan sijainnin, mutta
    kranaatti räjähtää mistä tahansa lattian ja pään väliltä -- savu ilmassa,
    molotov lattialla. Mitattuna painon kasvattaminen ilman toleranssia
    *huonontaa* tulosta (mediaani 20 -> 30 Ancientilla, 20 -> 31 Nukella),
    ja kun z-erosta vähennetään pelaajan korkeus ennen painotusta, mediaani
    putoaa 15:een ja 14:ään.

    Toleranssi on **symmetrinen**: vapaus vain ylöspäin nostaa mediaanin
    15 -> 17 ja 14 -> 19 parantamatta kattavuutta.

    **Miksi paino ylipäätään.** Nuke on kerroksellinen: alakerran ruutu on
    ylhäältä katsoen aivan vieressä mutta eri alueella. Ilman painoa
    yläkerran savu **saa** alakerran alueen -- mitattuna 38 räjähdystä
    ``Nuke_vs_imuaijat``illa ja 25 toisella Nuke-demolla.

    **Miksi paino on 1 eikä enemmän.** Paino 1 riittää: nolla väärän
    kerroksen aluetta molemmilla Nuke-demoilla. Jokainen sitä suurempi paino
    maksaa kattavuutta ostamatta mitään -- 99,0 % painolla 1, 98,8 %
    painolla 2, 97,4 % painolla 3 -- eikä mediaani liiku lainkaan.

    Ruudun edustaja on sen **keskipiste**, ei havaintojen keskiarvo: keskiarvo
    liikkuisi sen mukaan, mihin kohtaan ruutua pelaajat sattuivat asettumaan,
    eikä ruudukko olisi enää säännöllinen.

    Args:
        points: Nimettävät pisteet, sarakkeet :data:`NEAREST_POINT_COLUMNS`.
            ``point_id`` on kutsujan oma avain, joka palautuu sellaisenaan.
        cloud: Pistepilvi, sarakkeet :data:`CLOUD_CELL_COLUMNS`.
        grid_units: Sama ruudun särmä, jolla pilvi rakennettiin.
        z_weight: Pystyeron painokerroin toleranssin jälkeen.
        z_tolerance_units: Pystyero, joka on ilmaista (pelaajan korkeus).
        max_units: Enimmäisetäisyys, jonka sisältä alue saa tulla. ``None`` tai
            ei-äärellinen = ei kynnystä käytössä, jolloin **aluetta ei anneta
            lainkaan**. Se on kalibroimattoman asetuksen rehellinen arvo:
            lähin ruutu löytyy aina, joten kynnyksetön nimeäminen väittäisi
            aluetta räjähdykselle, joka tapahtui kaukana kaikesta.

    Returns:
        Rivi per syötepiste, sarakkeet :data:`NEAREST_RESULT_COLUMNS`.

        ``distance`` on **aina** lähimmän ruudun etäisyys, myös kun se ylittää
        kynnyksen -- juuri se erottaa tapauksen "kaukana kaikesta" tapauksesta
        "pilvi oli tyhjä", jossa se on ``null``. ``area`` on annettu vain
        kynnyksen sisällä.

    Raises:
        ValueError: Jos sarake puuttuu tai painotuksen parametri ei ole
            äärellinen ei-negatiivinen luku.
    """
    for name, value in (
        ("z_weight", z_weight),
        ("z_tolerance_units", z_tolerance_units),
    ):
        if not (value >= 0 and math.isfinite(value)):
            raise ValueError(
                f"{name} on {value!r}, joka ei kelpaa etäisyyden painotukseen: "
                "sen on oltava äärellinen eikä negatiivinen."
            )
    missing = [name for name in NEAREST_POINT_COLUMNS if name not in points.columns]
    if missing:
        raise ValueError(
            f"Nimettävien pisteiden taulusta puuttuu sarake: {', '.join(missing)}. "
            f"Odotetut sarakkeet ovat {', '.join(NEAREST_POINT_COLUMNS)}."
        )
    missing = [name for name in CLOUD_CELL_COLUMNS if name not in cloud.columns]
    if missing:
        raise ValueError(
            f"Pistepilvestä puuttuu sarake: {', '.join(missing)}. "
            f"Odotetut sarakkeet ovat {', '.join(CLOUD_CELL_COLUMNS)}."
        )
    # ``point_id`` on avain, ja lopullinen vasen liitos **monistaisi** rivin,
    # jos sama avain esiintyisi kahdesti eri paloissa. Tulos olisi silloin
    # pidempi kuin syöte, ja kutsuja saisi saman kranaatin kahdesti tauluun
    # ilman että mikään kaatuisi.
    duplicates = points.height - points["point_id"].n_unique()
    if duplicates:
        raise ValueError(
            f"Nimettävien pisteiden avain point_id ei ole yksikäsitteinen: "
            f"{duplicates} riviä on kaksoiskappaleita. Tulos monistuisi "
            "liitoksessa, eli sama piste palautuisi useammin kuin kerran."
        )

    empty = points.select(
        pl.col("point_id"),
        pl.lit(None, dtype=pl.Utf8).alias("area"),
        pl.lit(None, dtype=pl.Float64).alias("distance"),
    )
    if points.is_empty() or cloud.is_empty():
        return empty

    # Piste ilman koordinaatteja ei voi saada etäisyyttä -- eikä se saa myöskään
    # pudota: rivi on taulussa joka tapauksessa, ja puuttuva tulos on sen
    # rehellinen sisältö.
    locatable = points.filter(flight_point())
    if locatable.is_empty():
        return empty

    centers = cloud.select(
        ((pl.col("cell_x").cast(pl.Float64) + 0.5) * grid_units).alias("_cx"),
        ((pl.col("cell_y").cast(pl.Float64) + 0.5) * grid_units).alias("_cy"),
        ((pl.col("cell_z").cast(pl.Float64) + 0.5) * grid_units).alias("_cz"),
        pl.col("area").alias("_area"),
    )
    vertical = (
        pl.max_horizontal(
            (pl.col("z").cast(pl.Float64) - pl.col("_cz")).abs() - z_tolerance_units,
            pl.lit(0.0),
        )
        * z_weight
    )
    distance = (
        (pl.col("x").cast(pl.Float64) - pl.col("_cx")) ** 2
        + (pl.col("y").cast(pl.Float64) - pl.col("_cy")) ** 2
        + vertical**2
    ).sqrt()

    # Ristitulo pisteiden ja ruutujen välillä on tarkka ja yksinkertainen,
    # mutta se kasvaa tulona: 456 räjähdystä x 10 500 ruutua on 4,8 miljoonaa
    # riviä. Pisteet käsitellään siksi paloissa, jolloin muistihuippu on palan
    # koko kertaa ruudut eikä koko demo kertaa ruudut.
    best_frames: list[pl.DataFrame] = []
    for offset in range(0, locatable.height, NEAREST_CHUNK_POINTS):
        chunk = locatable.slice(offset, NEAREST_CHUNK_POINTS)
        best_frames.append(
            chunk.join(centers, how="cross")
            .with_columns(distance.alias("_d"))
            # Lajittelu on osa vastausta: kaksi yhtä kaukaista ruutua eri
            # alueilla ratkeaa nimen aakkosjärjestyksellä, jotta sama demo
            # antaa saman alueen joka ajolla.
            .sort(["point_id", "_d", "_area"])
            .group_by("point_id", maintain_order=True)
            .agg(pl.col("_area").first(), pl.col("_d").first())
        )
    best = pl.concat(best_frames)

    inside = (
        pl.col("_d") <= max_units
        if max_units is not None and math.isfinite(max_units)
        else pl.lit(False)
    )
    return (
        points.select("point_id")
        .join(best, on="point_id", how="left")
        .select(
            pl.col("point_id"),
            pl.when(inside).then(pl.col("_area")).otherwise(None).alias("area"),
            pl.col("_d").cast(pl.Float64).alias("distance"),
        )
    )


# -- Sisäinen -----------------------------------------------------------------


def _aggregate_runs(flight: pl.DataFrame, max_gap_ticks: int) -> pl.DataFrame:
    """Katkaise rata yhtenäisiin jaksoihin ja tiivistä jokainen päätepisteiksi.

    Jakso vaihtuu, kun tunniste, heittäjä tai kranaattityyppi vaihtuu tai
    tickeihin jää ``max_gap_ticks``:iä suurempi aukko. ``ne_missing`` eikä
    ``!=``: tyhjä heittäjä on jaksossa yhtä hyvä arvo kuin mikä tahansa muu, ja
    ``!=`` palauttaisi sille ``null``:in, jolloin jaksoraja jäisi huomaamatta.

    Lajitteluavain on ``(tunniste, tick)`` **ja sen perässä jokainen jäljellä
    oleva sarake**. Kaksi ensimmäistä määräävät järjestyksen; loput ovat
    pelkkiä tasapelin ratkaisijoita, eivätkä ne siirrä yhtäkään riviä
    tilanteessa, jossa pari on yksikäsitteinen.

    Ne ovat mukana determinismin takia. Jaksoraja luetaan viereisistä
    riveistä, joten se riippuu lajittelun tuloksesta, eikä Polarsin lajittelu
    ole vakaa: kaksi riviä samalla tunnisteella ja samalla tickillä voisivat
    vaihtaa paikkaa ajojen välillä. Silloin **jaksotus itse** -- ei vain
    numerointi -- olisi määräämätön, ja ``grenade_no``:n vakaus olisi tyhjä
    lupaus. Kun avaimessa on jokainen sarake, järjestys on rivien
    **sisällön** funktio: kaksi täsmälleen samanlaista riviä ovat keskenään
    vaihdettavissa, joten tulos on sama riippumatta siitä missä
    järjestyksessä demoparser2 rivit antoi.
    """
    tie_break = [
        name
        for name in TRAJECTORY_COLUMNS
        if name not in ("grenade_entity_id", "tick")
    ]
    frame = flight.sort("grenade_entity_id", "tick", *tie_break)
    run_start = (
        pl.col("grenade_entity_id").ne_missing(pl.col("grenade_entity_id").shift(1))
        | pl.col("thrower_id").ne_missing(pl.col("thrower_id").shift(1))
        | pl.col("grenade_type").ne_missing(pl.col("grenade_type").shift(1))
        | ((pl.col("tick") - pl.col("tick").shift(1)) > max_gap_ticks)
    )
    frame = frame.with_columns(run_start.fill_null(True).cum_sum().alias("_run"))

    return frame.group_by("_run", maintain_order=True).agg(
        pl.col("grenade_entity_id").first(),
        pl.col("grenade_type").first(),
        pl.col("thrower_id").first(),
        pl.col("tick").first().alias("throw_tick"),
        pl.col("x").first().alias("throw_x"),
        pl.col("y").first().alias("throw_y"),
        pl.col("z").first().alias("throw_z"),
        pl.col("tick").last().alias("detonate_tick"),
        pl.col("x").last().alias("detonate_x"),
        pl.col("y").last().alias("detonate_y"),
        pl.col("z").last().alias("detonate_z"),
        pl.len().alias("points"),
    )


def _endpoint_rows(runs: pl.DataFrame, event_kind: str, prefix: str) -> pl.DataFrame:
    """Poimi jaksoista joko heitto- tai räjähdysrivit."""
    return runs.select(
        pl.col("grenade_no"),
        pl.col("grenade_entity_id"),
        pl.col("grenade_type"),
        pl.col("thrower_id"),
        pl.lit(event_kind, dtype=pl.Utf8).alias("event_kind"),
        pl.col(f"{prefix}_tick").alias("tick"),
        pl.col(f"{prefix}_x").alias("x"),
        pl.col(f"{prefix}_y").alias("y"),
        pl.col(f"{prefix}_z").alias("z"),
    )
