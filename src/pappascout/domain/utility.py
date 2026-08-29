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
alue luetaan suoraan häneltä. Napsautus voisi tarttua vieressä seisovaan
kaveriin, vaikka oikea vastaus on tiedossa -- siksi :func:`snap_area` ei ole
mukana heiton polulla lainkaan. Kytkentä on adapterissa; tämä moduuli tarjoaa
vain napsautuksen.

**Kranaatilla** ei ole ``last_place_name``-kenttää, joten räjähdyksen alue on
pääteltävä koordinaateista. :func:`snap_area` ottaa lähimmän **elossa olevan**
pelaajan alueen samalta tickiltä ja jättää sen tyhjäksi, jos ketään ei ole
riittävän lähellä (``[parse].area_snap_units``). Tämä on tietoinen
approksimaatio ja ``area_snap_units`` on sen ainoa säädin: liian suuri arvo
antaa väärän calloutin, liian pieni jättää alueen tyhjäksi. Väärä alue on
pahempi kuin puuttuva, joten oletus on varovainen.

Ero näkyy taulussa asti: ``EVENTS.area_source`` erottaa havainnon arviosta ja
``snap_distance`` kertoo arvion etäisyyden. Ilman niitä raportti esittäisi
490 yksikön päästä poimitun calloutin yhtä varmana kuin heittäjän oman
alueen.

Moduuli on puhdas: ei tiedostoja, ei demoparser2:ta, ei asetuksia. Pelin omat
luokkanimet (``CSmokeGrenadeProjectile``) eivät esiinny täällä -- adapteri
kääntää ne ennen kutsua, jotta tämä logiikka pysyy testattavana käsin
rakennetuilla radoilla.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

import polars as pl

__all__ = [
    "AreaSnap",
    "PlayerPoint",
    "TRAJECTORY_COLUMNS",
    "ENDPOINT_COLUMNS",
    "THROWN",
    "DETONATE",
    "MAX_TRAJECTORY_GAP_SECONDS",
    "flight_point",
    "grenade_endpoints",
    "snap_area",
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


@dataclass(frozen=True)
class AreaSnap:
    """Napsautuksen tulos: alue ja se, kuinka kaukaa se otettiin.

    Attributes:
        area: Lähimmän elossa olevan pelaajan alue rajan sisältä, tai ``None``.
        distance: Etäisyys tuohon pelaajaan pelin yksiköissä, tai ``None``, jos
            napsautusta ei tehty lainkaan. Etäisyys säilyy silloinkin, kun
            ``area`` jää tyhjäksi -- pelaaja oli rajan sisällä mutta hänen
            alueellaan ei ole nimeä, ja se on eri asia kuin "ketään ei ollut
            lähellä".
    """

    area: str | None
    distance: float | None


@dataclass(frozen=True)
class PlayerPoint:
    """Yhden pelaajan sijainti ja alue yhdellä tickillä.

    Attributes:
        x: Koordinaatti. ``None`` = sijaintia ei saatu, jolloin pelaaja ei voi
            olla lähin eikä häntä oteta huomioon.
        y: Koordinaatti.
        z: Koordinaatti. Korkeus on mukana etäisyydessä, koska Nuken kaltaisilla
            kerroskartoilla alakerran pelaaja on ylhäältä katsoen aivan
            vieressä mutta eri alueella.
        area: Pelin oma aluenimi (``m_szLastPlaceName``). ``None`` = alue, jolle
            peli ei anna nimeä.
        is_alive: Kuollut pelaaja ei kerro, minne utility heitettiin -- hänen
            ruumiinsa jää siihen mihin hän kaatui.
    """

    x: float | None
    y: float | None
    z: float | None
    area: str | None
    is_alive: bool


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


def snap_area(
    x: float | None,
    y: float | None,
    z: float | None,
    players_at_tick: Iterable[PlayerPoint],
    max_units: float | None,
) -> AreaSnap:
    """Lähimmän elossa olevan pelaajan alue, jos hän on riittävän lähellä.

    Args:
        x: Kohteen koordinaatti (räjähdyspaikka).
        y: Kohteen koordinaatti.
        z: Kohteen koordinaatti.
        players_at_tick: Pelaajat **samalta tickiltä**. Toiselta tickiltä
            luettu asetelma kertoisi, missä joukkue oli hetkeä myöhemmin.
        max_units: Enimmäisetäisyys pelin yksiköissä. ``None`` = ei napsautusta,
            jolloin tulos on tyhjä -- se on kalibroimattoman asetuksen
            rehellinen arvo, ei vika. Myös ei-äärellinen arvo (NaN, ääretön)
            tulkitaan "ei napsautusta": NaN-vertailu olisi aina epätosi ja
            poistaisi etäisyysrajan huomaamatta.

    Returns:
        :class:`AreaSnap`. ``area`` on ``None`` kolmessa tapauksessa: ketään ei
        ole rajan sisällä, lähimmällä pelaajalla ei ole aluenimeä, tai
        napsautus on kytketty pois. Näistä keskimmäinen erottuu siitä, että
        ``distance`` on silti asetettu. Toiseksi lähimmän aluetta **ei
        kokeilla** -- se olisi arvaus, joka näyttäisi täsmälleen samalta kuin
        havainto.
    """
    if max_units is None or not math.isfinite(max_units):
        return AreaSnap(None, None)
    if x is None or y is None or z is None:
        return AreaSnap(None, None)
    if not math.isfinite(x) or not math.isfinite(y) or not math.isfinite(z):
        return AreaSnap(None, None)

    shortest: float | None = None
    area_name: str | None = None
    for player in players_at_tick:
        if not player.is_alive:
            continue
        if player.x is None or player.y is None or player.z is None:
            continue
        distance = math.dist((x, y, z), (player.x, player.y, player.z))
        if shortest is None or distance < shortest:
            shortest = distance
            area_name = player.area
    if shortest is None or shortest > max_units:
        return AreaSnap(None, None)
    return AreaSnap(area_name, shortest)


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
