"""Aggregoinnin laskenta puhtaina funktioina (Story 2.3).

Moduuli ottaa vastaan valmiit taulut (``CLASSIFIED``, ``TICKS``, ``EVENTS``) ja
palauttaa :class:`~pappascout.domain.report.Report`-mallin. **Ei tiedostoja, ei
arkistoa, ei asetusten latausta** -- kaikki tämän moduulin testit rakentavat
taulunsa käsin, eikä yksikään niistä tarvitse demoa.

Mitä täällä lasketaan
---------------------
Viisi havaintoa, jokainen otantansa kanssa, tasolla kartta -> puoli ->
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
    Montako pelaajaa oli aseistettu ostoajan lopussa (Story 1.6:n laskuri).
    Tästä luetaan *"5 kevlaria"* ja *"ei kevuja"*.
``first_contact``
    Millä alueilla joukkueella oli pelaaja ensikontaktin hetkellä. Tästä
    luetaan *"otti kontaktin partsi käytävällä"*.

Kaksi sääntöä, jotka eivät jousta
---------------------------------
**Pelaajamäärä lasketaan vain elossa olevista.** Kuollut pelaaja ei tuota
riviä alueelle; hän on kierroksella mukana, mutta ei kartalla.

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
    AreaDistribution,
    ArmedCount,
    ArmedPlayers,
    FirstContactArea,
    GrenadeCount,
    MapReport,
    MissingDemo,
    PlayersCount,
    Position,
    Report,
    RoundTypeReport,
    Sample,
    SampleBucket,
    SideReport,
    TeamReport,
    UtilityCounts,
    UtilityUse,
)
from pappascout.errors import AggregateError

__all__ = [
    "LEAGUE_BUCKETS",
    "RoundKey",
    "bucket_labels",
    "seconds_bucket",
    "map_name_for",
    "team_slug",
    "lineups_of_same_team",
    "demo_buckets",
    "sample_for",
    "players_distribution",
    "area_distributions",
    "positions_for",
    "utility_uses",
    "utility_counts_for",
    "unpaired_detonations",
    "armed_players_for",
    "first_contact_areas",
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


def map_name_for(
    map_demo_id: str, map_pool: Iterable[str]
) -> tuple[str, str]:
    """Päättele kartan nimi demon tunnisteesta.

    Kartan nimi **ei ole missään taulussa**: ``parse`` ei kirjoita sitä, koska
    kierros-, näytepiste- ja tapahtumataulut kuvaavat kierroksia eivätkä
    ottelua. Käsin tuodulla demolla nimi on kuitenkin tiedostonimessä
    (``Ancient_vs_kaljukostaja``), joten se luetaan sieltä karttapoolia vasten.

    Tunniste pilkotaan sanoiksi eikä haeta osamerkkijonona: osumahaku pitäisi
    joukkuetta nimeltä *Inferno* Infernona.

    Returns:
        ``(nimi, lähde)``. Lähde on ``"map_demo_id"``, jos pooli tunnisti
        yksikäsitteisesti yhden kartan, muuten ``"unknown"`` ja nimeksi jää
        ``map_demo_id`` sellaisenaan. Arvausta ei tehdä: FACEIT-tunnisteessa
        (``1-a52ebff2-...``) ei ole kartan nimeä, eikä kartaton demo saa
        sulautua toisen kartan haaraan.
    """
    tokens = {t for t in _NON_WORD.split(map_demo_id.lower()) if t}
    hits = {
        name
        for name in map_pool
        if name.lower() in tokens or name.lower().removeprefix("de_") in tokens
    }
    if len(hits) == 1:
        return next(iter(hits)), "map_demo_id"
    return map_demo_id, "unknown"


def team_slug(team_key: str) -> str:
    """Tiedostonimeen kelpaava muoto joukkueen tunnisteesta.

    ``render`` nimeää raportin ``<aika>-<team_slug>.md``, joten slug ei saa
    sisältää polkuerottimia eikä ääkkösiä.
    """
    slug = _NON_WORD.sub("-", team_key.lower()).strip("-")
    return slug or "joukkue"


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
    areas.sort(key=lambda a: (-a.n, _area_sort_key(a.area)))
    return areas


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
    team: TeamReport,
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
    map_pool: Sequence[str],
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
        team: Joukkueen tiedot; ``aggregate``-vaihe kokoaa ne arkistosta.
        thresholds: ``[thresholds]``-osio. Siitä luetaan
            ``small_sample_rounds``.
        aggregate: ``[aggregate]``-osio. Siitä luetaan
            ``utility_seconds_buckets``. Molemmat osiot kirjataan
            ``thresholds_used``-kenttään jäljitettävyyden vuoksi -- ne ovat
            **tämän ajon** asetukset, eivät ne joilla kierrokset luokiteltiin
            (ks. :func:`classify_thresholds`).
        map_pool: ``[league].map_pool``, jota vasten kartan nimi tunnistetaan.
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

    check_rounds_are_unique(rows)
    buckets = demo_buckets(rows)
    played = [r for r in rows if r["round_type"] is not None]
    unclassified = len(rows) - len(played)

    by_demo: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in played:
        by_demo[str(row["map_demo_id"])].append(row)

    # Kaksi demoa samalta kartalta on yksi haara: kierrokset summautuvat, ja
    # map_demo_ids kertoo mistä.
    by_map: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for demo in sorted(by_demo):
        by_map[map_name_for(demo, map_pool)].append(demo)

    ticks_by_demo = _group_by_demo(tick_rows)
    events_by_demo = _group_by_demo(event_rows)

    maps: list[MapReport] = []
    for (map_name, source), demos in by_map.items():
        map_rows = [r for demo in demos for r in by_demo[demo]]
        map_ticks = [r for demo in demos for r in ticks_by_demo.get(demo, [])]
        map_events = [r for demo in demos for r in events_by_demo.get(demo, [])]
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
                    buckets,
                    thresholds,
                    aggregate,
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
    buckets: Mapping[str, str],
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
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
                    side_rows, ticks, events, buckets, thresholds, aggregate
                ),
            )
        )
    return sides


def _round_types_for(
    rows: Sequence[Mapping[str, Any]],
    ticks: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    buckets: Mapping[str, str],
    thresholds: ThresholdSettings,
    aggregate: AggregateSettings,
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
                first_contact=first_contact_areas(ticks, keys),
            )
        )
    return reports
