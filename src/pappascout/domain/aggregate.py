"""Aggregoinnin laskenta puhtaina funktioina (Story 2.3).

Moduuli ottaa vastaan valmiit taulut (``CLASSIFIED``, ``TICKS``, ``EVENTS``,
``DEATHS``) ja palauttaa :class:`~pappascout.domain.report.Report`-mallin.
**Ei tiedostoja, ei arkistoa, ei asetusten latausta** -- kaikki tämän moduulin
testit rakentavat taulunsa käsin, eikä yksikään niistä tarvitse demoa.

Mitä täällä lasketaan
---------------------
Kuusi havaintoa, jokainen otantansa kanssa, tasolla kartta -> puoli ->
kierrostyyppi:

``positions``
    Pelaajamäärä alueittain jokaisessa näytepisteessä. Tästä luetaan rivi
    *"3A ja 2B"*.
``utility``
    Kranaatin tyyppi, heittoalue, räjähdysalue ja aikaikkuna. Tästä luetaan
    *"T-spawnista CT-savu B sitelle"* ja *"insta mid talo savu"*.
``utility_counts``
    Montako kutakin kranaattityyppiä heitettiin kierroksella. Tästä luetaan
    *"2 savua 2 valoo"*. Se **ei** ole johdettavissa ``utility``-riveistä:
    niiden ``n`` laskee kierroksia eikä kranaatteja.
``players_armed``
    Montako pelaajaa oli aseistettu ostoajan lopussa (Story 1.6:n laskuri:
    panssari **ja** parannettu ase). Se on puolioston kalibroitu ehto A.
``players_armored``
    Montako pelaajaa kantoi panssaria ostoajan lopussa (Story 2.8). Tästä --
    **eikä edellisestä** -- luetaan *"5 kevlaria"* ja *"ei kevuja"*:
    pistoolikierroksella edellinen on käytännössä 0, koska 800 dollarilla ei
    osta sekä kevlaria että parannettua asetta. Luku on hallussapitoa eikä
    ostosta paitsi pistoolikierroksella (1 ja 13), jolla perintää ei ole.
``first_contact``
    Millä alueilla joukkueella oli pelaaja ensikontaktin hetkellä. Tästä
    luetaan *"otti kontaktin partsi käytävällä"*.
``deaths``
    Missä ja milloin joukkue menetti ensimmäisen pelaajansa, ja miltä
    alueilta se teki tappoja. Tästä luetaan *"Luola kuolee nii pelaa
    siteltä"* ja *"Vihu meni secret pihalta"*.

Kaksi sääntöä, jotka eivät jousta
---------------------------------
**Pelaajamäärä lasketaan vain elossa olevista.** Kuollut pelaaja ei tuota
riviä alueelle; hän on kierroksella mukana, mutta ei kartalla.

**Yksi jakauma laskee tappoja eikä kierroksia.** ``deaths``in tappopuoli on
ainoa kohta, jossa ``m`` ei ole kierroksia: kierrostyypillä voi olla enemmän
tappoja kuin kierroksia. Ero on kirjoitettu :class:`DeathReport`in
sopimukseen, ja raportti muotoilee juuri sen rivin eri yksiköllä.

**Otanta on aina näytepisteen oma.** ``m`` on niiden kierrosten määrä, joilla
kyseinen näytepiste on olemassa -- ei kierrostyypin kaikkien kierrosten määrä.
Ne eroavat: 45 sekunnin näyte puuttuu kierrokselta, joka ratkesi 30 sekunnissa.
Jos ``m``:ksi otettaisiin kierrostyypin kokonaismäärä, ratkennut kierros
näkyisi jokaisella alueella arvona "0 pelaajaa" -- eli väitteenä, että alue oli
tyhjä. Sellainen luku näyttää havainnolta muttei ole sellainen.

Miksi Polarsia käytetään vain lukemiseen
----------------------------------------
Taulut ovat pieniä (neljä demoa = muutama tuhat riviä), ja jokainen tämän
moduulin laskutoimitus on ryhmittely, jonka Polars-muotoilu piilottaisi
sen, mistä otanta koostuu. Rivit puretaan siksi kerran sanakirjoiksi ja
lasketaan tavallisella Pythonilla, jotta ``Σ n = m`` on luettavissa koodista
eikä vain testistä.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from statistics import median
from typing import Any

import polars as pl

from pappascout.constants import (
    ROUND_TYPES,
    SAMPLE_BUCKETS,
    SIDES,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
)
from pappascout.domain.models import AggregateSettings, ThresholdSettings
from pappascout.domain.report import (
    SLUG_FALLBACK,
    AreaDistribution,
    ArmedCount,
    ArmedPlayers,
    ArmoredCount,
    ArmoredPlayers,
    DeathReport,
    FirstContactArea,
    FirstDeathArea,
    GrenadeCount,
    KillArea,
    MapReport,
    MissingDemo,
    PlayersCount,
    Position,
    Report,
    RosterEntry,
    RoundTypeReport,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
    slugify,
    team_slug,
)
from pappascout.domain.schemas import ARMORED_COLUMN
from pappascout.errors import AggregateError

__all__ = [
    "LEAGUE_BUCKETS",
    "RoundKey",
    "bucket_labels",
    "seconds_bucket",
    "map_name_for",
    "observed_map_name",
    "weakest_map_source",
    "MAP_NAME_SOURCE_RANK",
    "team_slug",
    "slugify",
    "SLUG_FALLBACK",
    "lineups_of_same_team",
    "TeamIdentity",
    "team_identity",
    "roster_entries",
    "demo_buckets",
    "sample_for",
    "players_distribution",
    "area_distributions",
    "positions_for",
    "utility_uses",
    "utility_counts_for",
    "unpaired_detonations",
    "armed_players_for",
    "SideRoundKey",
    "armored_by_round",
    "armored_players_for",
    "first_contact_areas",
    "deaths_for",
    "check_rounds_are_unique",
    "classify_thresholds",
    "CLASSIFY_THRESHOLD_KEYS",
    "build_report",
]

#: Otannan kolme lokeroa. ``unknown`` ei ole virhetila vaan tavallisin tila
#: ennen Epic 3:a: ``is_league`` tulee ``select``-vaiheesta, jota ei ole.
#:
#: Sama luettelo kuin :data:`pappascout.constants.SAMPLE_BUCKETS`, koska
#: lokeroiden suomennos (``SAMPLE_BUCKET_FI``) on siellä ja kahden luettelon
#: erkaantuminen jättäisi kolmannen lokeron hiljaa pois tulosteesta.
LEAGUE_BUCKETS: tuple[str, ...] = SAMPLE_BUCKETS

#: Kierroksen avain koko arkistossa. Pelkkä ``round_no`` sekoittaisi eri
#: karttojen kierrokset keskenään.
RoundKey = tuple[str, int]

#: Kierrosrivin avain **puoli mukaan lukien**. ``ROUNDS``-taulussa on kaksi
#: riviä per kierros, yksi kummallekin joukkueelle, joten pelkkä
#: :data:`RoundKey` osuisi molempiin -- ja vastustajan panssarit näyttäisivät
#: omilta. Luokiteltu rivi kantaa oman puolensa, joten liitos on tarkka.
SideRoundKey = tuple[str, int, str]

#: Rivinvaihto virheilmoituksissa. Omana vakionaan, koska nämä tiedostot
#: muokkataan usein skripteillä, joissa kenoviiva ei säily.
NEWLINE = "\n"

_NON_WORD = re.compile(r"[^a-z0-9]+")

#: Ne ``CLASSIFIED.inputs`` -kentät, jotka ovat kynnysarvoja eivätkä havaintoja.
#: Vain nämä päätyvät raportin kenttään ``classify_thresholds``; loput ovat
#: kierroskohtaisia mittauksia (rahat, varusteet), joilla ei ole yhtä arvoa.
CLASSIFY_THRESHOLD_KEYS: tuple[str, ...] = (
    "full_equip_min",
    "force_buy_min",
    "armed_players_min",
    "normal_buy_money_min",
    "normal_buy_players_min",
    "anomaly_equip_max_after_win",
)


# -- Pienet puhtaat apurit -------------------------------------------------------


def bucket_labels(edges: Sequence[float]) -> list[str]:
    """Aikaikkunoiden nimet rajoista.

    >>> bucket_labels([5.0, 10.0, 20.0])
    ['0-5', '5-10', '10-20', '20+']
    >>> bucket_labels([])
    ['kaikki']

    Tyhjä rajalista tarkoittaa yhtä lokeroa: aikaikkunan poistaminen on
    kelvollinen valinta eikä sen tarvitse olla koodimuutos.
    """
    if not edges:
        return [UTILITY_BUCKET_ALL]
    # Tarkistus tehdään RAJOJEN nimistä eikä valmiista lokeroista: kaksi
    # lähekkäistä rajaa tuottaa lokeron "5-5", joka on eri merkkijono kuin
    # naapurinsa muttei tarkoita mitään. Vasta kolmas raja tuottaisi kaksi
    # täsmälleen samannimistä lokeroa, ja siihen asti vika olisi näkyvissä
    # vain merkityksettömänä nimenä.
    names = [_seconds(edge) for edge in edges]
    if len(names) != len(set(names)):
        raise AggregateError(
            "Kaksi aikaikkunan rajaa näyttää samalta lokeron nimessä "
            f"({', '.join(names)}). Nimi muotoillaan lyhimpään "
            "esitysmuotoon, joten kaksi lähekkäistä rajaa olisi raportissa "
            "erottamattomia.\n"
            "Korjaa asetus [aggregate].utility_seconds_buckets."
        )
    labels = [f"0-{_seconds(edges[0])}"]
    labels += [
        f"{_seconds(low)}-{_seconds(high)}"
        for low, high in zip(edges, edges[1:], strict=False)
    ]
    labels.append(f"{_seconds(edges[-1])}+")
    return labels


def _seconds(value: float) -> str:
    """Sekuntiluku nimeen: ``5.0 -> '5'``, ``7.5 -> '7.5'``."""
    return f"{value:g}"


def seconds_bucket(t_s: float | None, edges: Sequence[float]) -> str:
    """Niputa heiton hetki aikaikkunaan.

    Raja kuuluu **ylempään** lokeroon: rajalla 5 s heitto hetkellä 5,0 s on
    lokerossa ``5-10``. Sääntö on mielivaltainen mutta yksi, eikä kumpaakaan
    lokeroa saa lukea molempiin suuntiin.

    Kelvoton hetki saa oman lokeronsa eikä putoa pois -- puuttuva aika on eri
    asia kuin nolla. Kelvottomia ovat kolme:

    * ``None`` -- kierrokselta puuttui ankkuri, joten aikaa ei ole.
    * **negatiivinen** -- kranaatti lähti ennen freezetimen loppua, eli
      mittaus on ristiriitainen. Ilman tarkistusta se niputtuisi lokeroon
      ``0-5`` ja näyttäisi "instalta".
    * **NaN tai ääretön** -- jokainen vertailu NaN:iin on epätosi, joten se
      valuisi viimeiseen lokeroon (``20+``) ja näyttäisi myöhäiseltä heitolta.

    Tarkistus on **ennen** tyhjän rajalistan oikosulkua: muuten tuntematon
    hetki sulautuisi tunnettuihin heti kun aikaikkunat poistetaan käytöstä.
    """
    if t_s is None or not isfinite(t_s) or t_s < 0:
        return UTILITY_BUCKET_UNKNOWN
    if not edges:
        return UTILITY_BUCKET_ALL
    labels = bucket_labels(edges)
    for index, edge in enumerate(edges):
        if t_s < edge:
            return labels[index]
    return labels[-1]


#: Kartan nimen lähteet **heikkenevässä** järjestyksessä. Pieni luku =
#: vahvempi. Järjestys on sama kuin ``MapReport.map_name_source``in
#: ensisijaisuusjärjestys, ja se on tässä nimenomaan vertailtavana lukuna,
#: koska haaran lähde on sen demojen heikoin (ks. :func:`weakest_map_source`).
MAP_NAME_SOURCE_RANK: dict[str, int] = {
    "demo_header": 0,
    "map_demo_id": 1,
    "unknown": 2,
}


def weakest_map_source(sources: Iterable[str]) -> str:
    """Haaran lähde on sen demojen **heikoin**, ei vahvin.

    Kaksi demoa samalta kartalta on yksi haara (``map_demo_ids`` luettelee ne),
    ja niiden nimen lähde voi erota: toisen otsikossa oli kartta, toisen ei.

    Lähde vastaa lukijan kysymykseen "voinko luottaa tähän nimeen", ja siihen
    yksi päätelty jäsen riittää vastaamaan "ei täysin". Vahvimman valitseminen
    olisi ylisanomista: haara näyttäisi kokonaan havaittuna, vaikka osa sen
    kierroksista on liitetty siihen tiedostonimen perusteella. Väärä nimi
    yhdellä demolla tuo väärät kierrokset koko haaraan, joten heikoin lenkki on
    se, joka on kerrottava.

    ``unknown`` ei voi päätyä tähän toisen nimen kanssa: sen nimi on
    ``map_demo_id`` itse, joten se ei törmää yhdenkään oikean kartan nimeen.

    Args:
        sources: Haaran demojen lähteet. Epätyhjä.

    Returns:
        Heikoin lähde :data:`MAP_NAME_SOURCE_RANK`-järjestyksessä.

    Raises:
        AggregateError: Jos luettelo on tyhjä tai sisältää tuntemattoman
            lähteen. Kumpikaan ei voi syntyä tämän moduulin omasta
            ryhmittelystä, joten kyseessä olisi kutsuvirhe -- ja hiljaa
            palautettu oletus valehtelisi lukijalle nimen luotettavuudesta.
    """
    known = list(sources)
    if not known:
        raise AggregateError(
            "Karttahaaran lähdeluettelo on tyhjä.\n"
            "Haara syntyy vain demoista, joten jokaisella on ainakin yksi "
            "lähde. Tyhjä luettelo tarkoittaa, että ryhmittely on rikki."
        )
    unknown = sorted(set(known) - set(MAP_NAME_SOURCE_RANK))
    if unknown:
        raise AggregateError(
            f"Tuntematon kartan nimen lähde: {', '.join(unknown)}.\n"
            f"Sallitut ovat {', '.join(MAP_NAME_SOURCE_RANK)}. Uusi lähde on "
            "lisättävä sekä tähän luetteloon että ``MapReport``in sopimukseen, "
            "jotta sen vahvuus on määritelty."
        )
    return max(known, key=lambda source: MAP_NAME_SOURCE_RANK[source])


def observed_map_name(
    map_names: Mapping[str, str | None], demo: str
) -> str | None:
    """Demon otsikosta havaittu nimi; puuttuva **avain** on virhe.

    Kahta asiaa ei saa niputtaa: arvo ``None`` on laillinen havainto ("otsikossa
    ei ollut karttaa", jolloin päättely jää voimaan), mutta **puuttuva avain**
    tarkoittaa että demo ei ollut ``aggregate``n lukemassa nimikartassa
    lainkaan. Se on ohjelmointivirhe -- ja juuri sen estämiseksi
    :func:`build_report`in ``map_names`` tehtiin pakolliseksi ilman oletusta.

    ``Mapping.get`` sotkisi ne yhteen ja palauttaisi kartan hiljaa päättelyyn:
    FACEIT-demo saisi haaransa tunnisteestaan, eikä mikään kertoisi että
    havainto oli olemassa mutta ei löytänyt perille.

    Raises:
        AggregateError: Jos ``demo`` ei ole ``map_names``-kartassa.
    """
    if demo not in map_names:
        raise AggregateError(
            f"Demo {demo} ei ole kartan nimien joukossa.\n"
            "``aggregate`` lukee nimen jokaisen mukaan otetun demon "
            "``match.parquet``-taulusta, joten puuttuva avain tarkoittaa, "
            "ettei taulua luettu tälle demolle. Puuttuva **nimi** on eri "
            "asia: se on ``None`` ja täysin laillinen, ja silloin nimi "
            "päätellään tunnisteesta.\n"
            f"Aja: uv run pappascout parse {demo}"
        )
    return map_names[demo]


def map_name_for(
    map_demo_id: str, map_pool: Iterable[str], observed: str | None = None
) -> tuple[str, str]:
    """Kartan nimi: havainto ensin, päättely vasta sen puuttuessa.

    ``observed`` on demon otsikosta luettu nimi (``MATCH.map_name``, Story
    2.11). Se **voittaa aina** ja käytetään sellaisenaan: sitä ei verrata
    karttapooliin, koska poolin ulkopuolinen kartta -- workshop-versio tai
    ``de_train`` -- on aito havainto eikä tuntematon kartta. Hiljainen korjaus
    poolin nimeksi tekisi havainnosta johdoksen.

    Ilman havaintoa nimi päätellään tunnisteesta. Käsin tuodulla demolla se on
    tiedostonimessä (``Ancient_vs_kaljukostaja``), joten se luetaan sieltä
    karttapoolia vasten. Tunniste pilkotaan sanoiksi eikä haeta
    osamerkkijonona: osumahaku pitäisi joukkuetta nimeltä *Inferno* Infernona.

    Args:
        map_demo_id: Demon tunniste.
        map_pool: ``[league].map_pool``, jota vasten päättely tehdään.
        observed: Otsikosta havaittu nimi tai ``None``. Tyhjä ja pelkistä
            välilyönneistä koostuva merkkijono ovat sama asia kuin ``None``:
            kumpikaan ei ole nimi, joten päättely jää voimaan. Reunojen
            välilyönnit leikataan, jotta sama kartta ei jakaudu kahdeksi
            haaraksi.

    Returns:
        ``(nimi, lähde)``. Lähde on ensisijaisuusjärjestyksessä
        ``"demo_header"`` (havainto otsikosta), ``"map_demo_id"`` (pooli
        tunnisti yksikäsitteisesti yhden kartan tunnisteesta) tai
        ``"unknown"``, jolloin nimeksi jää ``map_demo_id`` sellaisenaan.
        Arvausta ei tehdä: FACEIT-tunnisteessa (``1-a52ebff2-...``) ei ole
        kartan nimeä, eikä kartaton demo saa sulautua toisen kartan haaraan.
    """
    if observed is not None and observed.strip():
        # Leikattu, ei raaka. Docstring lupaa jo, että pelkät välilyönnit ovat
        # sama asia kuin ``None``; jos reunan välilyönnit jäisivät nimeen,
        # ``" de_ancient"`` olisi eri haara kuin ``"de_ancient"`` -- eli
        # havainto pirstoisi kartan sen sijaan että kokoaisi sen.
        #
        # Leikkaus **ei ole validointia**: se ei vertaa nimeä karttapooliin
        # eikä muuta kirjoitusasua. Adapteri leikkaa nimen jo lukiessaan, joten
        # tämä on toinen puolustuslinja -- mutta funktio on julkinen ja sillä
        # on oma sopimuksensa, joten se ei nojaa kutsujan siisteyteen.
        return observed.strip(), "demo_header"
    tokens = {t for t in _NON_WORD.split(map_demo_id.lower()) if t}
    hits = {
        name
        for name in map_pool
        if name.lower() in tokens or name.lower().removeprefix("de_") in tokens
    }
    if len(hits) == 1:
        return next(iter(hits)), "map_demo_id"
    return map_demo_id, "unknown"


def lineups_of_same_team(
    target: str,
    members: Mapping[str, Iterable[str]],
    min_common: int,
) -> list[str]:
    """Kokoonpanot, jotka ovat sama joukkue kuin ``target``.

    Kokoonpanotunniste on tiiviste kartalla pelanneista pelaajista, joten
    **yksi vaihto tuottaa uuden tunnisteen**. MatureMayhem on neljässä demossa
    kahden eri tunnisteen alla, ja ilman liittämistä raportti näkisi kolme
    demoa neljästä eikä kertoisi menettäneensä yhtä.

    Sääntö on ``[thresholds].team_identity_min_common`` (AD-6): kokoonpanot
    ovat sama joukkue, kun yhteisiä pelaajia on vähintään ``min_common``.
    Vertailu tehdään **aina kohteeseen**, ei ketjuna: ketjuttaminen liittäisi
    kaksi joukkuetta toisiinsa yhden yhteisen kokoonpanon kautta.

    Args:
        target: Kohteena oleva kokoonpanotunniste.
        members: Tunniste -> pelaajat.
        min_common: Vähimmäismäärä yhteisiä pelaajia.

    Returns:
        Tunnisteet lajiteltuna, ``target`` aina mukana.

    Raises:
        AggregateError: Jos ``target`` ei ole ``members``-kartassa.
    """
    if target not in members:
        raise AggregateError(
            f"Kokoonpanoa {target!r} ei löydy annetuista kokoonpanoista, joten "
            "joukkueidentiteettiä ei voi ratkaista."
        )
    own = set(members[target])
    return sorted(
        key
        for key, players in members.items()
        if key == target or len(own & set(players)) >= min_common
    )


@dataclass(frozen=True)
class TeamIdentity:
    """Joukkueen nimi ja rosteri sellaisina kuin ne demoista havaittiin.

    Attributes:
        display_name: Useimmin havaittu klaaninimi, tai ``None`` jos yhtään ei
            havaittu. ``None`` on rehellinen tulos: nimen puuttuminen on
            havainto, ei syy keksiä korviketta tunnisteesta tai
            tiedostonimestä.
        alternatives: Muut havaitut klaaninimet aakkosjärjestyksessä.
            **Ristiriita ei katoa**: jos liitetyt demot antavat joukkueelle eri
            nimiä, näytettäväksi valitaan useimmin havaittu ja loput
            luetellaan, jotta lukija näkee että joukkue esiintyi kahdella
            nimellä.
        names: ``player_id -> nimi`` niille pelaajille, joilla nimi havaittiin.
            Puuttuva avain tarkoittaa, ettei nimeä saatu -- rosterirvi
            kirjoitetaan silti, koska SteamID on aina olemassa.
    """

    display_name: str | None = None
    alternatives: list[str] = field(default_factory=list)
    names: dict[str, str] = field(default_factory=dict)


def team_identity(rows: Sequence[Mapping[str, Any]]) -> TeamIdentity:
    """Päättele joukkueen nimi ja pelaajien nimet kokoonpanoriveistä.

    Args:
        rows: ``LINEUPS``-taulun rivit, **suodatettuna tämän joukkueen
            kokoonpanoihin**. Suodatus on kutsujan vastuu: funktio ei tiedä
            mitkä kokoonpanot ovat sama joukkue.

    Klaaninimen äänestys on **kaksivaiheinen enemmistö**, ei "yksi ääni per
    demossa havaittu nimi". Ensin ratkaistaan demon sisällä yleisin klaani,
    sitten se saa demonsa **yhden** äänen, ja lopulta äänestetään demojen yli.

    Ero on ratkaiseva pienellä otannalla. Jos demossa on viisi pelaajaa joista
    neljällä on klaani ``A`` ja yhdellä ``B``, "ääni per havaittu nimi" antaisi
    molemmille yhden -- yhden demon otannalla se on tasatilanne, ja
    aakkosjärjestys voisi nostaa otsikkoon nimen, jonka yksi ainoa pelaaja
    kantoi. Enemmistö demon sisällä ratkaisee sen oikein, ja demojen yli
    laskettuna viiden pelaajan demo ei silti paina viittä kertaa yhden pelaajan
    demoa.

    Tasatilanne ratkeaa **molemmilla tasoilla aakkosjärjestyksessä**, jotta
    sama arkisto antaa saman raportin ajosta toiseen. Ilman sitä tulos
    riippuisi siitä, missä järjestyksessä tiedostot sattuivat tulemaan
    luetuiksi.

    Returns:
        :class:`TeamIdentity`.

    Raises:
        AggregateError: Jos jonkin rivin ``map_demo_id`` on tyhjä. Se on
            halpa vartija kalliille virheelle: tyhjä tunniste sulauttaisi
            kaikki demot yhdeksi ääneksi, jolloin neljän demon enemmistö
            kutistuisi yhdeksi eikä mikään kertoisi siitä.
    """
    clans_per_demo: dict[str, Counter[str]] = {}
    name_votes: dict[str, Counter[str]] = {}

    for row in rows:
        demo = _clean_name(row.get("map_demo_id"))
        if demo is None:
            raise AggregateError(
                "Kokoonpanotaulun rivillä ei ole map_demo_id:tä, joten sitä ei "
                "voi kohdistaa demoon.\n"
                "Joukkueen nimi äänestetään demo kerrallaan, ja tunnisteeton "
                "rivi sulauttaisi kaikki demot yhdeksi ääneksi. Aja parsinta "
                "uudelleen."
            )
        clan = _clean_name(row.get("clan_name"))
        if clan is not None:
            clans_per_demo.setdefault(demo, Counter())[clan] += 1
        player_id = row.get("player_id")
        name = _clean_name(row.get("player_name"))
        if player_id is not None and name is not None:
            name_votes.setdefault(str(player_id), Counter())[name] += 1

    # Vaihe 1: demon sisäinen enemmistö. Vaihe 2: yksi ääni per demo.
    clan_votes: Counter[str] = Counter(
        _by_votes(votes)[0] for votes in clans_per_demo.values() if votes
    )
    ordered = _by_votes(clan_votes)
    return TeamIdentity(
        display_name=ordered[0] if ordered else None,
        alternatives=sorted(ordered[1:]),
        names={
            player_id: _by_votes(votes)[0]
            for player_id, votes in name_votes.items()
            if votes
        },
    )


def roster_entries(
    player_ids: Iterable[str], names: Mapping[str, str]
) -> list[RosterEntry]:
    """Rosteri: jokainen pelaaja tunnisteineen ja nimineen.

    Pelaajajoukko tulee **tunnisteista eikä nimistä**: rivi kirjoitetaan
    silloinkin, kun nimeä ei saatu, koska SteamID on ainoa jäljitettävä arvo ja
    hiljaa pudotettu pelaaja kutistaisi rosterin kertomatta siitä.
    """
    return [
        RosterEntry(player_id=player_id, display_name=names.get(player_id))
        for player_id in sorted(player_ids)
    ]


def _clean_name(value: Any) -> str | None:
    """Nimi ilman ympäröiviä välilyöntejä, tai ``None``.

    Tyhjä merkkijono ei ole nimi. Sama sääntö kuin parsinnassa, toistettuna
    tässä siksi, että vanhalla versiolla kirjoitettu taulu voi sisältää tyhjän
    merkkijonon eikä sitä saa esittää nimenä.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _by_votes(votes: Counter[str]) -> list[str]:
    """Arvot ääniä laskien, tasatilanne aakkosjärjestyksessä."""
    return sorted(votes, key=lambda name: (-votes[name], name))


# -- Otanta ----------------------------------------------------------------------


def demo_buckets(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Demo -> otantalokero ``league`` / ``other`` / ``unknown``.

    ``is_league`` on demokohtainen tieto (se kertoo ottelun lajin), joten
    saman demon kaikilla kierroksilla on oltava sama arvo.

    Raises:
        AggregateError: Jos yhden demon kierroksilla on kaksi eri arvoa.
            Silloin demo kuuluisi kahteen lokeroon, eikä otannan summa enää
            olisi lokeroiden summa -- ja juuri se summa on koko rakenteen
            tarkistus.
    """
    seen: defaultdict[str, set[bool | None]] = defaultdict(set)
    for row in rows:
        seen[str(row["map_demo_id"])].add(row["is_league"])
    buckets: dict[str, str] = {}
    for demo, values in seen.items():
        if len(values) > 1:
            raise AggregateError(
                f"Demon {demo} kierroksilla on kaksi eri is_league-arvoa "
                f"({sorted(str(v) for v in values)}), joten demo kuuluisi "
                "kahteen otantalokeroon.\n"
                "is_league kuvaa ottelua eikä kierrosta. Aja luokittelu "
                "uudelleen: uv run pappascout classify <map_demo_id> --pakota"
            )
        value = next(iter(values))
        buckets[demo] = (
            "unknown" if value is None else ("league" if value else "other")
        )
    return buckets


def sample_for(
    rows: Sequence[Mapping[str, Any]], buckets: Mapping[str, str]
) -> Sample:
    """Otanta yhdelle tasolle: demot ja kierrokset kolmessa lokerossa."""
    demos: defaultdict[str, set[str]] = defaultdict(set)
    rounds: Counter[str] = Counter()
    for row in rows:
        demo = str(row["map_demo_id"])
        bucket = buckets[demo]
        demos[bucket].add(demo)
        rounds[bucket] += 1
    made = {
        name: SampleBucket(demos=len(demos[name]), rounds=rounds[name])
        for name in LEAGUE_BUCKETS
    }
    return Sample(
        demos=sum(b.demos for b in made.values()),
        rounds=sum(b.rounds for b in made.values()),
        **made,
    )


# -- Jakaumat --------------------------------------------------------------------


def players_distribution(counts: Iterable[int]) -> list[PlayersCount]:
    """Pelaajamäärien jakauma pylväiksi.

    Syöte on **kierros per alkio**, myös nollat: juuri niistä syntyy
    ``players = 0`` -pylväs, jota ilman ``Σ n = m`` ei pitäisi.
    """
    tally = Counter(int(c) for c in counts)
    return [
        PlayersCount(players=players, n=tally[players])
        for players in sorted(tally)
    ]


def area_distributions(
    rows_by_round: Mapping[RoundKey, Sequence[Mapping[str, Any]]],
) -> list[AreaDistribution]:
    """Alueiden jakaumat yhdessä näytepisteessä.

    Alueiden joukko on **kaikkien kierrosten alueiden unioni**, ja jokainen
    kierros tuottaa jokaiselle alueelle havainnon -- myös arvon 0. Ilman
    unionia jakauma kertoisi vain siitä kierroksesta, jolla alueella sattui
    olemaan joku, ja "kolmella kierroksella neljästä B oli tyhjä" olisi
    puuttuva rivi eikä havainto.

    Args:
        rows_by_round: Kierros -> näytepisteen rivit. **Vain elossa olevat**;
            kuollutta pelaajaa ei lasketa.
    """
    m = len(rows_by_round)
    areas: set[str | None] = set()
    per_round: dict[RoundKey, Counter[str | None]] = {}
    for key, rows in rows_by_round.items():
        tally: Counter[str | None] = Counter(
            (row["area"] if row["area"] is not None else None) for row in rows
        )
        per_round[key] = tally
        areas.update(tally)

    return [
        AreaDistribution(
            area=area,
            m=m,
            players_dist=players_distribution(
                per_round[key][area] for key in rows_by_round
            ),
        )
        for area in sorted(areas, key=_area_sort_key)
    ]


def _area_sort_key(area: str | None) -> tuple[int, str]:
    """Alueet aakkosjärjestyksessä, tuntematon viimeisenä."""
    return (1, "") if area is None else (0, area)


def _round_key(row: Mapping[str, Any]) -> RoundKey | None:
    """Rivin kierrosavain, tai ``None`` jos kierrosta ei ole numeroitu.

    Lämmittelyn, puukkokierroksen ja ottelun uudelleenaloituksen rivit tulevat
    ``parse``-vaiheelta ilman ``round_no``:ta. Ne eivät ole kierroksia, joten
    ne eivät voi kuulua yhdenkään kierrostyypin otantaan -- eikä niitä silti
    saa yrittää muuttaa numeroksi.
    """
    if row["round_no"] is None:
        return None
    return (str(row["map_demo_id"]), int(row["round_no"]))


def positions_for(
    ticks: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
) -> list[Position]:
    """Näytepisteet yhdelle kartta/puoli/kierrostyyppi -haaralle.

    Aikanäytepisteet ryhmitellään ``sample_t_s``:n mukaan, ensikontakti
    **yhdeksi** näytepisteeksi: sen hetki on eri joka kierroksella, joten
    ``sample_t_s``:llä ryhmittely tuottaisi yhden näytepisteen per kierros.
    """
    total_rounds = len(round_keys)
    groups: defaultdict[
        tuple[str, float | None], dict[RoundKey, list[Mapping[str, Any]]]
    ] = defaultdict(dict)

    keys = set(round_keys)
    contact_seconds: dict[RoundKey, float] = {}
    for row in ticks:
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        kind = str(row["sample_kind"])
        if row["sample_t_s"] is None:
            # Aikanäytepiste ilman nimellistä sekuntia ei ole
            # ryhmiteltävissä, ja ensikontaktilla arvo on mitattu hetki --
            # kumpikaan ei saa puuttua. Tyhjä arvo sulauttaisi kaksi eri
            # näytepistettä yhteen.
            raise AggregateError(
                f"Näytepisteeltä puuttuu sample_t_s (kierros {key}, "
                f"sample_kind={kind}). Aja parsinta uudelleen: "
                f"uv run pappascout parse {key[0]} --pakota"
            )
        group = groups[(kind, float(row["sample_t_s"]) if kind == "time" else None)]
        # Kierros on näytepisteessä mukana heti kun sillä on yksikin rivi --
        # myös silloin, kun jokainen pelaaja on kuollut. Muuten kierros, jolla
        # koko joukkue oli kaatunut, katoaisi otannasta ja Σ n = m pettäisi.
        group.setdefault(key, [])
        if kind == "first_contact":
            contact_seconds[key] = float(row["sample_t_s"])
        if bool(row["is_alive"]):
            group[key].append(row)

    positions: list[Position] = []
    for (kind, seconds), rows_by_round in groups.items():
        # Mediaani lasketaan KIERROKSISTA eikä pelaajariveistä: sama hetki
        # toistuu jokaisella elossa olevalla pelaajalla, joten rivipohjainen
        # mediaani painottaisi kierrosta, jolla oli enemmän pelaajia
        # hengissä. Neljä pelaajaa 10 s kohdalla ja yksi 20 s kohdalla
        # antaisi 10,0 vaikka kierrosten mediaani on 15,0.
        contact_times = (
            sorted(contact_seconds[key] for key in rows_by_round)
            if kind == "first_contact"
            else []
        )
        positions.append(
            Position(
                sample_kind=kind,
                seconds=seconds,
                seconds_median=(
                    round(median(contact_times), 3) if contact_times else None
                ),
                m=len(rows_by_round),
                rounds_missing=total_rounds - len(rows_by_round),
                areas=area_distributions(rows_by_round),
            )
        )
    # Aikanäytepisteet nousevassa järjestyksessä, ensikontakti viimeisenä:
    # se ei ole kellonaika vaan tapahtuma.
    positions.sort(key=lambda p: (p.sample_kind == "first_contact", p.seconds or 0.0))
    return positions


def armed_players_for(
    rows: Sequence[Mapping[str, Any]],
) -> ArmedPlayers:
    """Aseistettujen pelaajien jakauma kierroksittain.

    Havainto on ``classify``-vaiheen tallentama ``inputs.players_armed`` eli
    Story 1.6:n laskuri. ``null`` tarkoittaa lukukelvotonta tavaraluetteloa,
    ja se pidetään erillään nollasta: nolla aseistettua on säästökierros,
    lukukelvoton ei ole havainto lainkaan.
    """
    values = [_armed(row) for row in rows]
    known = [v for v in values if v is not None]
    tally = Counter(known)
    return ArmedPlayers(
        m=len(known),
        rounds_unknown=len(values) - len(known),
        counts=[
            ArmedCount(armed=armed, n=tally[armed]) for armed in sorted(tally)
        ],
    )


def _armed(row: Mapping[str, Any]) -> int | None:
    """``inputs.players_armed`` yhdeltä riviltä, ``None`` jos puuttuu."""
    inputs = row.get("inputs")
    if not isinstance(inputs, Mapping):
        return None
    value = inputs.get("players_armed")
    return None if value is None else int(value)


def armored_by_round(rounds: Sequence[Mapping[str, Any]]) -> dict[SideRoundKey, int]:
    """Panssarilaskuri kierrostaulusta, avaimella (demo, kierros, puoli).

    **Lähde on ``parsed/rounds`` eikä ``classified``**, toisin kuin
    aseistettujen laskurilla. Syy on rajaus eikä mukavuus: aseistettujen
    laskuri on puolioston ehto A, joten ``classify`` lukee sen joka
    tapauksessa ja tallentaa päätöksen syötteisiin. Panssarilaskuri ei ole
    päätöksen syöte -- se on havainto -- eikä sitä siksi lisätä
    ``economy.CLASSIFY_COLUMNS``iin. Luokittelusääntö pysyy ennallaan, ja
    havainto luetaan sieltä missä se on.

    Avaimessa on **puoli mukana**: kierrostaulussa on kaksi riviä per kierros,
    ja ilman puolta vastustajan panssarit voisivat päätyä omalle riville.

    Numeroimattomat kierrokset (lämmittely, puukkokierros), rivit joilta
    havaintoa ei saatu ja rivit joiden avain on vajaa jäävät kartasta pois.
    Puuttuva avain tarkoittaa ``rounds_unknown``ia
    :func:`armored_players_for`issä -- nollaa se ei saa tarkoittaa, koska
    nolla on havainto ja lukuvirhe ei ole.

    Raises:
        AggregateError: Jos kaksi riviä väittää samaa avainta. Hiljainen
            ylikirjoitus jättäisi voimaan sen, joka sattuu olemaan viimeisenä,
            eikä mikään kertoisi kumpi luku raporttiin päätyi. Luokitelluille
            riveille sama tarkistus on :func:`check_rounds_are_unique`.
    """
    lookup: dict[SideRoundKey, int] = {}
    for row in rounds:
        round_no = row.get("round_no")
        value = row.get(ARMORED_COLUMN)
        demo, side = row.get("map_demo_id"), row.get("side")
        # Vajaa avain pudotetaan **ennen** str()-muunnosta: ``str(None)``
        # rakentaisi avaimen "None", joka ei osu koskaan mutta näyttää
        # kartassa täysin tavalliselta.
        if round_no is None or value is None or demo is None or side is None:
            continue
        key = (str(demo), int(round_no), str(side))
        if key in lookup and lookup[key] != int(value):
            raise AggregateError(
                f"Kierrostaulussa on kaksi eri panssarilukua samalle "
                f"kierrokselle {key}: {lookup[key]} ja {int(value)}.\n"
                "Raporttiin päätyisi se, joka sattuu olemaan viimeisenä. "
                "Aja parsinta uudelleen: uv run pappascout parse "
                f"{demo} --pakota"
            )
        lookup[key] = int(value)
    return lookup


def armored_players_for(
    rows: Sequence[Mapping[str, Any]],
    armored: Mapping[SideRoundKey, int],
) -> ArmoredPlayers:
    """Panssaroitujen pelaajien jakauma kierroksittain.

    **Eri havainto kuin** :func:`armed_players_for`, ei sen yleistys. Tästä
    luetaan tavoiteanalyysin *"5 kevlaria"* ja *"ei kevuja"*, joita
    aseistettujen jakaumasta ei saa: pistoolikierroksella se on käytännössä 0,
    koska 800 dollarilla ei osta sekä kevlaria että parannettua asetta.

    Args:
        rows: Kierrostyypin luokitellut rivit. Ne määräävät otannan, joten
            kierros, jolta panssarilukua ei saatu, on ``rounds_unknown`` eikä
            katoa jakaumasta.
        armored: :func:`armored_by_round`in kartta koko otannasta.
    """
    values: list[int | None] = []
    for row in rows:
        round_no = row.get("round_no")
        demo, side = row.get("map_demo_id"), row.get("side")
        # Vajaa avain on **puuttuva havainto eikä kaatuva ajo**: jokainen
        # annettu rivi tuottaa alkion, jotta otanta ei pienene hiljaa, ja
        # yksi vajaa rivi jää tuntemattomaksi sen sijaan että veisi koko
        # aggregoinnin mukanaan. ``classify`` pudottaa numeroimattomat jo
        # ennen tätä, joten haara on puolustus eikä odotettu tila.
        values.append(
            None
            if round_no is None or demo is None or side is None
            else armored.get((str(demo), int(round_no), str(side)))
        )
    known = [v for v in values if v is not None]
    tally = Counter(known)
    return ArmoredPlayers(
        m=len(known),
        rounds_unknown=len(values) - len(known),
        counts=[
            ArmoredCount(armored=armored_n, n=tally[armored_n])
            for armored_n in sorted(tally)
        ],
    )


def first_contact_areas(
    ticks: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
) -> list[FirstContactArea]:
    """Alueet, joilla joukkueella oli pelaaja ensikontaktin hetkellä.

    Havainto on **läsnäolo**: sama kierros tuottaa havainnon jokaiselle
    alueelle, jolla joukkueella oli elossa oleva pelaaja. ``Σ n = m`` ei siis
    päde eikä ole tarkoituskaan -- täysi jakauma samalta hetkeltä on
    ``positions``-listan ensikontaktinäytepisteessä.
    """
    keys = set(round_keys)
    rounds_with_sample: set[RoundKey] = set()
    present: defaultdict[str | None, set[RoundKey]] = defaultdict(set)
    for row in ticks:
        if str(row["sample_kind"]) != "first_contact":
            continue
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        rounds_with_sample.add(key)
        if bool(row["is_alive"]):
            present[row["area"]].add(key)

    m = len(rounds_with_sample)
    areas = [
        FirstContactArea(area=area, n=len(rounds), m=m)
        for area, rounds in present.items()
    ]
    areas.sort(key=lambda a: _by_count_then_area((a.area, a.n)))
    return areas


# -- Kuolemat --------------------------------------------------------------------


def deaths_for(
    deaths: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
    lineup_keys: Iterable[str],
) -> DeathReport:
    """Joukkueen omat kuolemat ja tapot yhdelle kierrostyypille.

    Kaksi reunajakaumaa, ja ne luetaan **saman taulun eri sarakkeista**:
    ``victim_lineup_key`` kertoo omat kuolemat, ``attacker_lineup_key`` omat
    tapot. Sama rivi voi olla molempia -- teamkillissä joukkueen pelaaja
    tappoi joukkuekaverinsa -- eikä kumpaakaan suodateta pois: havainto on
    että pelaaja kuoli ja että ampuja oli tietyllä alueella, ja teamkillin
    erottaminen olisi tulkintaa.

    **Ensimmäinen kuolema kierroksella** on se, jolla on pienin ``t_s``.
    Tasatilanne -- kaksi joukkuekaveria samalla tickillä -- ratkeaa uhrin
    tunnisteella, jotta sama aineisto antaa saman tuloksen ajosta toiseen.
    Rivi, jolta ``t_s`` puuttuu, on järjestyksessä viimeisenä eikä
    ensimmäisenä: puuttuva aika ei ole nolla.

    **Tappoalue on ampujan oma alue**, ei uhrin. Juuri niin tavoiteanalyysin
    rivi "Vihu meni secret pihalta" on kirjoitettu: se kertoo mistä ampuja
    ampui.

    **Itsemurha ei ole tappo.** Rivi, jolla ampuja ja uhri ovat sama pelaaja,
    on oma kuolema muttei oma tappo: tapporivi kertoo mistä joukkue ampuu, ja
    itsemurhan alue on paikka josta kukaan ei ampunut. Teamkill sen sijaan
    lasketaan molempiin -- siinä joukkuekaveri **oikeasti ampui** tuolta
    alueelta. Mitattu 2026-08-30: 0 itsemurhaa ja 1 teamkill 591 kuolemasta.

    Args:
        deaths: ``DEATHS``-rivit. **Ei suodatettuna kokoonpanoon**: funktio
            tarvitsee molemmat sarakkeet, ja kutsuja ei voi tietää kumpi
            niistä osuu.
        round_keys: Tämän haaran kierrokset.
        lineup_keys: Joukkueen kokoonpanotunnisteet.

    Returns:
        :class:`~pappascout.domain.report.DeathReport`. Tyhjä mutta
        kelvollinen, jos haarassa ei kuoltu.
    """
    keys = set(round_keys)
    own = set(lineup_keys)

    first: dict[RoundKey, tuple[tuple[int, float, str], Mapping[str, Any]]] = {}
    kills: Counter[str | None] = Counter()
    kills_total = 0

    for row in deaths:
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        suicide = (
            row["attacker_id"] is not None
            and row["attacker_id"] == row["victim_id"]
        )
        if row["attacker_lineup_key"] in own and not suicide:
            kills[_observed_area(row["attacker_area"])] += 1
            kills_total += 1
        if row["victim_lineup_key"] not in own:
            continue
        order = _death_order(row)
        current = first.get(key)
        if current is None or order < current[0]:
            first[key] = (order, row)

    moments = [
        float(row["t_s"])
        for _, row in first.values()
        if row["t_s"] is not None
    ]
    areas: Counter[str | None] = Counter(
        _observed_area(row["victim_area"]) for _, row in first.values()
    )
    m = len(first)

    # Yleisin alue ensin, tasatilanne aakkosin ja tuntematon viimeisenä --
    # sama järjestys kuin ensikontaktin alueilla, jotta raportin rivit
    # luetaan samalla tavalla.
    first_areas = [
        FirstDeathArea(area=area, n=n, m=m)
        for area, n in sorted(areas.items(), key=_by_count_then_area)
    ]
    kill_areas = [
        KillArea(area=area, n=n, m=kills_total)
        for area, n in sorted(kills.items(), key=_by_count_then_area)
    ]
    return DeathReport(
        m=m,
        rounds_missing=len(round_keys) - m,
        first_death_seconds_median=(
            round(median(moments), 3) if moments else None
        ),
        first_death_areas=first_areas,
        kills_total=kills_total,
        kills=kill_areas,
    )


def _observed_area(value: Any) -> str | None:
    """Alue havaintona: tyhjä merkkijono **ei ole alue** vaan ``None``.

    ``parse`` kirjoittaa jo nyt tyhjän ``last_place_name``in ``null``:na, mutta
    sopimus sallii merkkijonon eikä vanhalla versiolla kirjoitettu taulu ole
    käynyt sitä sääntöä läpi. Ilman normalisointia sama havainto tulisi
    jakaumaan **kahdesti**: mallin kaksoiskappaletarkistus vertaa raaka-arvoja
    (``""`` ja ``None`` ovat eri), mutta raportti näyttää molemmat nimellä
    "tuntematon alue" -- eli yksi rivi kertoisi saman asian kaksi kertaa eri
    luvuilla.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _by_count_then_area(item: tuple[str | None, int]) -> tuple[int, int, str]:
    """Lajitteluavain aluejakaumalle: yleisin ensin, tuntematon viimeisenä.

    **Yksi kirjoitusasu yhdelle säännölle.** Sekä ensikontaktin, ensimmäisen
    kuoleman että tappojen alueet järjestyvät näin, ja kaksi kopiota
    erkanisivat: raportin rivit luetaan samalla tavalla, joten niiden on myös
    järjestyttävä samalla tavalla.
    """
    area, count = item
    return (-count, *_area_sort_key(area))


def _death_order(row: Mapping[str, Any]) -> tuple[int, float, str]:
    """Järjestysavain kierroksen sisällä: aika ensin, uhrin tunniste sitten.

    Ensimmäinen alkio erottaa **puuttuvan ajan nollasta**: ilman sitä rivi,
    jolta ``t_s`` puuttuu, olisi kierroksen ensimmäinen kuolema. Uhrin
    tunniste tekee tasatilanteesta toistettavan -- kaksi joukkuekaveria voi
    kuolla samalla tickillä, eikä rivijärjestys saa ratkaista kumpi näkyy
    raportissa.
    """
    t_s = row["t_s"]
    return (
        1 if t_s is None else 0,
        0.0 if t_s is None else float(t_s),
        str(row["victim_id"]),
    )


# -- Utility ---------------------------------------------------------------------


def _detonations(
    events: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], Mapping[str, Any]]:
    """Räjähdysrivit avaimella ``(map_demo_id, grenade_no)``.

    **Kierrosta ei suodateta.** Molotov palaa seitsemän sekuntia ja savu
    kahdeksantoista, joten kierroksen lopussa heitetty kranaatti räjähtää
    seuraavan kierroksen puolella ja saa eri ``round_no``:n. Jos molemmat
    rivit suodatettaisiin kierrosta vasten erikseen, pari katkeaisi ilman
    kirjausta ja kranaatti menettäisi alueensa hiljaa. Kranaatti kuuluu sille
    kierrokselle, jolla se **heitettiin**, joten vain heitto suodatetaan.
    """
    return {
        (str(row["map_demo_id"]), int(row["grenade_no"])): row
        for row in events
        if str(row["event_kind"]) == "grenade_detonate"
    }


def unpaired_detonations(events: Sequence[Mapping[str, Any]]) -> int:
    """Räjähdykset, joilta puuttuu heittorivi.

    ``parse`` kirjoittaa heiton ja räjähdyksen aina parina samalla
    ``grenade_no``:lla, joten pariton räjähdys tarkoittaa rikkinäistä taulua.
    Se pudotetaan utilityn laskennasta -- heittoaluetta eikä heittohetkeä ei
    ole -- mutta lukumäärä palautetaan, koska hiljainen pudotus näyttäisi
    siltä, ettei kranaattia heitetty lainkaan.
    """
    thrown = {
        (str(row["map_demo_id"]), int(row["grenade_no"]))
        for row in events
        if str(row["event_kind"]) == "grenade_thrown"
    }
    return sum(1 for pair in _detonations(events) if pair not in thrown)


def _grenades(
    events: Sequence[Mapping[str, Any]], round_keys: Sequence[RoundKey]
) -> list[dict[str, Any]]:
    """Pariuta heitto ja räjähdys yhdeksi kranaatiksi.

    Pari yhdistetään avaimella ``(map_demo_id, grenade_no)``: ``grenade_no``
    on yksikäsitteinen demon sisällä, ``grenade_entity_id`` ei ole -- peli
    kierrättää entiteettitunnisteet jopa saman kierroksen sisällä.

    **Vain heitto suodatetaan kierrosta vasten** (ks. :func:`_detonations`).
    Räjähdysrivin puuttuminen ei pudota kranaattia: heitto on havainto
    omillaan ja se on juuri se, mistä utility mitataan. Pariton räjähdys sen
    sijaan putoaa -- sillä ei ole heittoaluetta eikä heittohetkeä -- ja sen
    lukumäärä raportoidaan erikseen funktiolla :func:`unpaired_detonations`.
    """
    keys = set(round_keys)
    detonate = _detonations(events)

    grenades: list[dict[str, Any]] = []
    for row in events:
        if str(row["event_kind"]) != "grenade_thrown":
            continue
        key = _round_key(row)
        if key is None or key not in keys:
            continue
        blast = detonate.get((str(row["map_demo_id"]), int(row["grenade_no"])))
        grenades.append(
            {
                "round": key,
                "grenade_type": str(row["grenade_type"]),
                "throw_area": row["area"],
                "t_s": None if row["t_s"] is None else float(row["t_s"]),
                "detonate_area": None if blast is None else blast["area"],
                # Lähde luetaan sellaisenaan eikä johdeta alueesta: jos
                # taulussa on alue ilman lähdettä, raporttimalli kaatuu siihen
                # äänekkäästi sen sijaan että arvio menisi läpi havaintona.
                "area_source": (
                    None
                    if blast is None or blast["area_source"] is None
                    else str(blast["area_source"])
                ),
            }
        )
    return grenades


def utility_uses(
    events: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
    bucket_edges: Sequence[float],
) -> list[UtilityUse]:
    """Utility-kuviot: tyyppi, heittoalue, räjähdysalue ja aikaikkuna.

    ``n`` laskee **kierroksia** ja ``throws`` **kranaatteja**. Ne eroavat, kun
    samalla kierroksella heitetään kaksi samanlaista kranaattia samaan
    paikkaan, ja siksi ``n``-arvojen summa ei ole kranaattien määrä.

    Alueeton kranaatti (``area`` on ``null``) saa oman lokeronsa eikä putoa
    pois: savu heitetään usein sinne, missä ei ole ketään, ja juuri se on sen
    tarkoitus.
    """
    m = len(round_keys)
    rounds: defaultdict[tuple[Any, ...], set[RoundKey]] = defaultdict(set)
    throws: Counter[tuple[Any, ...]] = Counter()
    for grenade in _grenades(events, round_keys):
        key = (
            grenade["grenade_type"],
            grenade["throw_area"],
            grenade["detonate_area"],
            grenade["area_source"],
            seconds_bucket(grenade["t_s"], bucket_edges),
        )
        rounds[key].add(grenade["round"])
        throws[key] += 1

    uses = [
        UtilityUse(
            grenade_type=key[0],
            throw_area=key[1],
            detonate_area=key[2],
            area_source=key[3],
            seconds_bucket=key[4],
            n=len(seen),
            throws=throws[key],
            m=m,
        )
        for key, seen in rounds.items()
    ]
    # Aikaikkunat aikajärjestyksessä eivätkä aakkosissa: aakkosissa "10-20"
    # tulisi ennen "5-10", ja kuvion lukija odottaa kellon järjestystä.
    order = {label: i for i, label in enumerate(bucket_labels(bucket_edges))}
    uses.sort(
        key=lambda u: (
            u.grenade_type,
            _area_sort_key(u.throw_area),
            _area_sort_key(u.detonate_area),
            order.get(u.seconds_bucket, len(order)),
        )
    )
    return uses


def utility_counts_for(
    events: Sequence[Mapping[str, Any]],
    round_keys: Sequence[RoundKey],
) -> list[UtilityCounts]:
    """Montako kutakin kranaattityyppiä heitettiin kierroksella.

    Tyyppijoukko on **tässä haarassa havaitut tyypit**. Nollapylväs syntyy
    niistä kierroksista, joilla tyyppiä ei heitetty, joten "eivät heittäneet
    yhtään savua" on havainto eikä puuttuva rivi. Tyyppiä, jota ei heitetty
    kertaakaan, ei kirjoiteta lainkaan -- sama sääntö kuin tyhjällä
    kierrostyypillä.
    """
    m = len(round_keys)
    per_type: defaultdict[str, Counter[RoundKey]] = defaultdict(Counter)
    for grenade in _grenades(events, round_keys):
        per_type[grenade["grenade_type"]][grenade["round"]] += 1

    result: list[UtilityCounts] = []
    for grenade_type in sorted(per_type):
        tally = per_type[grenade_type]
        counts = Counter(tally.get(key, 0) for key in round_keys)
        result.append(
            UtilityCounts(
                grenade_type=grenade_type,
                m=m,
                counts=[
                    GrenadeCount(thrown=thrown, n=counts[thrown])
                    for thrown in sorted(counts)
                ],
            )
        )
    return result


# -- Koko raportti ---------------------------------------------------------------


def check_rounds_are_unique(rows: Sequence[Mapping[str, Any]]) -> None:
    """Varmista, että ``(map_demo_id, round_no)`` esiintyy korkeintaan kerran.

    Avain on koko rakenteen liitosavain, ja kaksoiskappale hajottaisi otannan
    kahdella tavalla yhtä aikaa: kierrostyypin ``m`` laskee **listasta**
    (kaksoiskappale mukana) mutta näytepisteen ``m`` **joukosta**
    (kaksoiskappale pois), jolloin sama data tuottaisi kaksi eri lukua ja
    erotus näkyisi kentässä ``rounds_missing`` -- juuri siinä kentässä, jonka
    tehtävä on estää kierroksen katoaminen.

    Raises:
        AggregateError: Jos sama kierros esiintyy kahdesti. Käytännössä syy on
            kaksi luokiteltua taulua samasta demosta.
    """
    seen: Counter[RoundKey] = Counter()
    for row in rows:
        key = _round_key(row)
        if key is not None:
            seen[key] += 1
    twice = sorted(f"{demo} kierros {no}" for (demo, no), n in seen.items() if n > 1)
    if twice:
        raise AggregateError(
            "Sama kierros esiintyy luokitelluissa tauluissa useammin kuin "
            f"kerran: {', '.join(twice[:10])}"
            + (f" (+{len(twice) - 10} muuta)" if len(twice) > 10 else "")
            + ".\n"
            "Liitosavain (map_demo_id, round_no) on koko rakenteen "
            "perusta, ja kaksoiskappale vääristäisi jokaisen otannan."
        )


def classify_thresholds(
    rows: Sequence[Mapping[str, Any]],
    expected: ThresholdSettings | None = None,
) -> dict[str, int]:
    """Ne kynnysarvot, joilla kierrokset **oikeasti luokiteltiin**.

    Luetaan ``CLASSIFIED.inputs``-sarakkeesta, jonne ``classify`` tallentaa
    jokaisen kierroksen vertailuun käytetyt arvot. Tämä on havainto eikä
    nykyisten asetusten kopio: käyttäjä voi muuttaa ``settings.toml``ia ja ajaa
    pelkän aggregoinnin, jolloin ``[thresholds]`` kertoisi kynnyksistä, joilla
    yhtäkään kierrosta ei luokiteltu.

    Args:
        rows: Luokitellut kierrokset.
        expected: Nykyiset ``[thresholds]``-asetukset. Jos annettu, havaittuja
            arvoja **verrataan niihin**: ero tarkoittaa, että kynnystä on
            muutettu eikä luokittelua ole ajettu uudelleen, jolloin raportti
            nimeäisi kynnykset, joilla yhtäkään kierrosta ei luokiteltu.

    Raises:
        AggregateError: Jos jokin kynnys eroaa kierrosten välillä tai
            nykyisistä asetuksista. Edellinen sekoittaisi eri säännöillä
            luokiteltuja kierroksia samaan lukuun, jälkimmäinen antaisi
            raportille väärän selityksen -- kummassakin tapauksessa luku
            näyttäisi oikealta muttei tarkoittaisi sitä mitä väittää.
    """
    seen: defaultdict[str, set[int]] = defaultdict(set)
    for row in rows:
        inputs = row.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        for name in CLASSIFY_THRESHOLD_KEYS:
            value = inputs.get(name)
            if value is not None:
                seen[name].add(int(value))
    mixed = sorted(f"{name}: {sorted(v)}" for name, v in seen.items() if len(v) > 1)
    if mixed:
        raise AggregateError(
            "Kierrokset on luokiteltu eri kynnyksillä, joten niitä ei voi "
            f"laskea samaan otantaan: {'; '.join(mixed)}.\n"
            "Aja luokittelu uudelleen jokaiselle demolle samoilla "
            "asetuksilla: uv run pappascout classify <map_demo_id> --team "
            "<tunniste> --pakota"
        )
    found = {name: next(iter(v)) for name, v in sorted(seen.items())}
    if expected is not None:
        stale = sorted(
            f"{name}: luokiteltu {value}, asetuksissa {getattr(expected, name)}"
            for name, value in found.items()
            if getattr(expected, name, value) != value
        )
        if stale:
            raise AggregateError(
                "Kierrokset on luokiteltu eri kynnyksillä kuin mitä "
                "asetuksissa nyt on, joten raportti nimeäisi kynnykset, "
                f"joilla yhtäkään kierrosta ei luokiteltu: "
                f"{'; '.join(stale)}.\n"
                "Aja luokittelu uudelleen ennen aggregointia: "
                "uv run pappascout classify <map_demo_id> --team "
                "<tunniste> --pakota"
            )
    return found


def build_report(
    *,
    classified: pl.DataFrame,
    ticks: pl.DataFrame,
    events: pl.DataFrame,
    deaths: pl.DataFrame,
    rounds: pl.DataFrame,
    team: TeamReport,
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    map_pool: Sequence[str],
    map_names: Mapping[str, str | None],
    generated_at: datetime,
    tool_versions: Mapping[str, str] | None = None,
    missing_demos: Sequence[MissingDemo] = (),
) -> Report:
    """Rakenna koko raportti valmiista tauluista.

    Args:
        classified: Kaikkien demojen ``CLASSIFIED``-rivit yhtenä kehyksenä.
            Tämä on ainoa lähde sille, mikä kierros on olemassa ja minkä
            tyyppinen se on.
        ticks: Kaikkien demojen ``TICKS``-rivit, **suodatettuna joukkueen
            kokoonpanoihin**.
        events: Kaikkien demojen ``EVENTS``-rivit, samoin suodatettuna.
        deaths: Kaikkien demojen ``DEATHS``-rivit. Suodatus on **erilainen**:
            rivi kuuluu joukkueelle, jos joko uhri tai ampuja on sen
            kokoonpanossa, joten yhden sarakkeen suodatus pudottaisi joko
            kuolemat tai tapot. Rivien jako niiden kesken tehdään täällä
            (:func:`deaths_for`) joukkueen ``lineup_keys``-listaa vasten.
        rounds: Kaikkien demojen ``ROUNDS``-rivit, **suodatettuna joukkueen
            kokoonpanoihin**. Tästä luetaan vain panssarilaskuri
            (:func:`armored_by_round`): se on havainto eikä luokittelun
            päätöksen syöte, joten ``classify`` ei kanna sitä eteenpäin.
            Kierrostyypin ja otannan omistaa yhä ``classified`` -- tämä taulu
            ei saa lisätä eikä poistaa yhtäkään kierrosta.
        team: Joukkueen tiedot; ``aggregate``-vaihe kokoaa ne arkistosta.
        thresholds: ``[thresholds]``-osio. Siitä luetaan
            ``small_sample_rounds``.
        aggregate: ``[aggregate]``-osio. Siitä luetaan
            ``utility_seconds_buckets``. Molemmat osiot kirjataan
            ``thresholds_used``-kenttään jäljitettävyyden vuoksi -- ne ovat
            **tämän ajon** asetukset, eivät ne joilla kierrokset luokiteltiin
            (ks. :func:`classify_thresholds`).
        map_pool: ``[league].map_pool``, jota vasten kartan nimi päätellään,
            kun otsikossa ei ollut nimeä.
        map_names: ``map_demo_id`` -> otsikosta havaittu kartan nimi tai
            ``None``. Lähde on ``MATCH.map_name`` (Story 2.11). Argumentti
            on **pakollinen eikä sillä ole oletusta**: tyhjä oletus
            palauttaisi koko raportin hiljaa päättelyyn, jolloin
            FACEIT-demot hajoaisivat taas omiksi haaroikseen ilman että
            mikään kertoisi siitä. Jokaisen mukaan otetun demon on oltava
            kartassa; puuttuva avain nostaa virheen
            (:func:`observed_map_name`), koska se on eri asia kuin arvo
            ``None``. Kartat ryhmitellään **nimestä**, ja haaran
            ``map_name_source`` on sen demojen heikoin
            (:func:`weakest_map_source`).
        generated_at: Ajon hetki.
        tool_versions: Työkaluversiot raportin omaan kenttään.
        missing_demos: Ottelut, joiden dataa ei ollut.

    Returns:
        Validoitu :class:`Report`. Jos otanta ei täsmää jollakin tasolla,
        malli itse nostaa :class:`~pappascout.errors.AggregateError`.
    """
    rows = classified.to_dicts()
    tick_rows = ticks.to_dicts()
    event_rows = events.to_dicts()
    death_rows = deaths.to_dicts()
    armored = armored_by_round(rounds.to_dicts())

    check_rounds_are_unique(rows)
    buckets = demo_buckets(rows)
    played = [r for r in rows if r["round_type"] is not None]
    unclassified = len(rows) - len(played)

    by_demo: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in played:
        by_demo[str(row["map_demo_id"])].append(row)

    # Kaksi demoa samalta kartalta on yksi haara: kierrokset summautuvat, ja
    # map_demo_ids kertoo mistä.
    #
    # RYHMITTELY ON **NIMESTÄ**, EI PARISTA (nimi, lähde). Pari näyttäisi
    # oikealta mutta pirstoisi kartan täsmälleen niin kuin puuttuva otsikko
    # ennen tätä tarinaa: ``ANCIENT_vs_RCAVE_VETERANS`` (havainto otsikosta) ja
    # ``Ancient_vs_kaljukostaja`` (päättely tiedostonimestä) ovat kumpikin
    # ``de_ancient``, mutta eri lähteellä -- ja kahtena avaimena ne olisivat
    # kaksi ``de_ancient``-osiota, molemmat merkinnällä "(1/1 kierroksesta)".
    # Ennen Story 2.11:tä vikaa ei voinut olla: ``unknown``-haaran nimi on
    # tunniste itse, joten se ei törmää oikeaan nimeen. Havainnon myötä kaksi
    # eri lähdettä voi tuottaa saman nimen, ja siksi avain on nimi.
    #
    # Haaran lähde on sen demojen **heikoin** (:func:`weakest_map_source`).
    by_map: defaultdict[str, list[str]] = defaultdict(list)
    branch_sources: defaultdict[str, list[str]] = defaultdict(list)
    for demo in sorted(by_demo):
        name, source = map_name_for(
            demo, map_pool, observed_map_name(map_names, demo)
        )
        by_map[name].append(demo)
        branch_sources[name].append(source)

    ticks_by_demo = _group_by_demo(tick_rows)
    events_by_demo = _group_by_demo(event_rows)
    deaths_by_demo = _group_by_demo(death_rows)

    maps: list[MapReport] = []
    for map_name, demos in by_map.items():
        source = weakest_map_source(branch_sources[map_name])
        map_rows = [r for demo in demos for r in by_demo[demo]]
        map_ticks = [r for demo in demos for r in ticks_by_demo.get(demo, [])]
        map_events = [r for demo in demos for r in events_by_demo.get(demo, [])]
        map_deaths = [r for demo in demos for r in deaths_by_demo.get(demo, [])]
        maps.append(
            MapReport(
                map_name=map_name,
                map_name_source=source,
                map_demo_ids=demos,
                sample=sample_for(map_rows, buckets),
                sides=_sides_for(
                    map_rows,
                    map_ticks,
                    map_events,
                    map_deaths,
                    armored,
                    buckets,
                    thresholds,
                    aggregate,
                    team.lineup_keys,
                ),
            )
        )
    # Pelatuimmat kartat ensin; tasatilanteessa nimi, jotta järjestys on sama
    # ajosta toiseen.
    maps.sort(key=lambda m: (-m.sample.rounds, m.map_name))

    thresholds_used = {
        "thresholds": thresholds.model_dump(mode="json"),
        "aggregate": aggregate.model_dump(mode="json"),
    }
    return Report(
        generated_at=generated_at,
        tool_versions=dict(tool_versions or {}),
        team=team,
        sample=sample_for(played, buckets),
        thresholds_used=thresholds_used,
        classify_thresholds=classify_thresholds(rows, thresholds),
        unpaired_detonations=unpaired_detonations(event_rows),
        missing_demos=list(missing_demos),
        unclassified_rounds=unclassified,
        maps=maps,
    )


def _group_by_demo(
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, list[Mapping[str, Any]]]:
    grouped: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["map_demo_id"])].append(row)
    return grouped


def _sides_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    deaths: Sequence[Mapping[str, Any]],
    armored: Mapping[SideRoundKey, int],
    buckets: Mapping[str, str],
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    lineup_keys: Sequence[str],
) -> list[SideReport]:
    """Puolet vakiojärjestyksessä; puoli, jolla ei ole kierroksia, jää pois."""
    sides: list[SideReport] = []
    for side in SIDES:
        side_rows = [r for r in rows if str(r["side"]) == side]
        if not side_rows:
            continue
        sides.append(
            SideReport(
                side=side,
                sample=sample_for(side_rows, buckets),
                round_types=_round_types_for(
                    side_rows,
                    ticks,
                    events,
                    deaths,
                    armored,
                    buckets,
                    thresholds,
                    aggregate,
                    lineup_keys,
                ),
            )
        )
    return sides


def _round_types_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    deaths: Sequence[Mapping[str, Any]],
    armored: Mapping[SideRoundKey, int],
    buckets: Mapping[str, str],
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    lineup_keys: Sequence[str],
) -> list[RoundTypeReport]:
    """Kierrostyypit vakiojärjestyksessä.

    Tyyppi, jota kartalla ei pelattu, **puuttuu rakenteesta** eikä ole
    nollarivi: nollarivi väittäisi havainnoksi sen, ettei havaintoa ole.
    Suodatusta ei tehdä toiseen suuntaankaan -- myös täydet ostot ja jatkoaika
    lasketaan, koska raportti valitsee mitä sanoo.
    """
    reports: list[RoundTypeReport] = []
    for round_type in ROUND_TYPES:
        type_rows = [r for r in rows if str(r["round_type"]) == round_type]
        if not type_rows:
            continue
        keys: list[RoundKey] = []
        for row in type_rows:
            key = _round_key(row)
            if key is None:
                # classify pudottaa numeroimattomat kierrokset, joten
                # tämä tarkoittaa että luokiteltu taulu on rikki.
                # Suojaamaton int(None) kaatuisi TypeErroriin ilman
                # ohjetta.
                raise AggregateError(
                    f"Luokitellussa taulussa {row['map_demo_id']!r} on rivi "
                    "ilman kierrosnumeroa, joten sitä ei voi liittää "
                    "näytepisteisiin.\n"
                    f"Aja luokittelu uudelleen: uv run pappascout classify "
                    f"{row['map_demo_id']} --pakota"
                )
            keys.append(key)
        sample = sample_for(type_rows, buckets)
        reports.append(
            RoundTypeReport(
                round_type=round_type,
                sample=sample,
                small_sample=sample.rounds < thresholds.small_sample_rounds,
                positions=positions_for(ticks, keys),
                utility=utility_uses(
                    events, keys, aggregate.utility_seconds_buckets
                ),
                utility_counts=utility_counts_for(events, keys),
                players_armed=armed_players_for(type_rows),
                players_armored=armored_players_for(type_rows, armored),
                first_contact=first_contact_areas(ticks, keys),
                deaths=deaths_for(deaths, keys, lineup_keys),
            )
        )
    return reports
