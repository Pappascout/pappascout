"""Raportin näkymämalli: **mitä** raportissa sanotaan.

Moduuli lukee :class:`~pappascout.domain.report.Report`in ja rakentaa siitä
rivejä ja väitteitä. Se **ei laske mitään**: jokainen luku poimitaan mallista
sellaisenaan, eikä täällä ole yhtään yhteenlaskua, keskiarvoa eikä osamäärää.
Ainoa "logiikka" on valinta -- mitkä havainnot ansaitsevat rivin -- ja
muotoilu.

Kolme sääntöä, jotka näkyvät jokaisessa funktiossa
-------------------------------------------------
**Jokainen väite kantaa otantansa.** :class:`Claim` on nimenomaan pari
"väite + otanta", eikä väitettä voi rakentaa ilman ``n``:ää ja ``m``:ää. Ilman
otantaa yksi kierros näyttäisi kuviolta.

**Säästökierrokset ja default ovat eri muotoisia.** Pistooli, eco, force ja
puoliosto kuvataan kierroksen tarkkuudella: jokainen havainto kirjoitetaan.
Täydet ostot (``full``) ja jatkoaika (``ot``) kuvataan **vain toistuvina
kuvioina**, ja toistumisen raja luetaan raportista
(``thresholds_used.thresholds.small_sample_rounds``) -- sitä ei keksitä
täällä. Pois jätettyjen havaintojen määrä kirjoitetaan näkyviin, joten
suodatus ei ole hiljainen.

**Ei tulkintoja.** Rivit kertovat pelaajamääriä, kranaatteja ja alueita.
Sanoja "fake", "rush" tai "hyvä" ei ole missään -- johtopäätös on lukijan.

**Kuolemat mahtuvat kahteen riviin.** Raportti on jo satoja rivejä, kun
Veetin oma analyysi on 30. Kuolemat lisättiin siksi, että ne selittävät muut
rivit -- ei siksi, että ne olisivat oma lukunsa. Raja on
:data:`MAX_DEATH_LINES`, ja sen ylitys on virhe eikä hiljainen kasvu.

Miksi ensikontaktista näytetään jakauma eikä läsnäololista
----------------------------------------------------------
``Report`` sisältää ensikontaktin kahdesti: ``round_types[].first_contact``
kertoo **läsnäolon** (oliko alueella pelaaja) ja ``positions``-listan
``first_contact``-näytepiste kertoo **pelaajamäärät** samalta hetkeltä.
Jälkimmäinen sisältää edellisen tiedon ja lisää siihen luvun, joten raportti
käyttää sitä. Molempien kirjoittaminen toistaisi saman havainnon kahdesti
kahdessa muodossa, ja raportin on oltava lyhyt.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pappascout.constants import (
    ROUND_TYPE_FI,
    SAMPLE_BUCKET_FI,
    SAMPLE_BUCKETS,
    UTILITY_BUCKET_ALL,
    UTILITY_BUCKET_UNKNOWN,
)
from pappascout.domain.report import (
    ArmedPlayers,
    DeathReport,
    Position,
    Report,
    RoundTypeReport,
    UtilityCounts,
    UtilityUse,
)
from pappascout.errors import PappascoutError

__all__ = [
    "GRENADE_TYPE_FI",
    "GRENADE_ORDER",
    "ROUND_TYPE_ORDER",
    "PATTERN_ROUND_TYPES",
    "MAX_DEATH_LINES",
    "KILL_SAMPLE_UNIT",
    "UNKNOWN_AREA",
    "Claim",
    "Line",
    "SummaryItem",
    "RoundTypeView",
    "SideView",
    "MapView",
    "ReportView",
    "build_view",
    "round_list_demo_ids",
    "pattern_min_rounds",
    "rounds_text",
    "demos_text",
]

#: Kranaattityyppien suomennokset. Nämä ovat **esitystä**, joten ne asuvat
#: render-kerroksessa eivätkä ``constants``issa: ``report.json``in avaimet
#: pysyvät englanniksi, koska ne ovat sopimusta.
#:
#: ``molotov`` ja ``incendiary`` ovat pelissä eri esineet mutta lennossa sama
#: luokka; ``aggregate`` erottaa ne repun nimestä, joten myös raportti erottaa.
GRENADE_TYPE_FI: dict[str, str] = {
    "smoke": "savu",
    "flashbang": "valo",
    "he": "HE",
    "incendiary": "poltto",
    "molotov": "molotov",
    "decoy": "decoy",
}

#: Kranaattien esitysjärjestys: ensin ne, jotka kertovat suunnitelmasta eniten.
GRENADE_ORDER: tuple[str, ...] = (
    "smoke",
    "flashbang",
    "he",
    "incendiary",
    "molotov",
    "decoy",
)

#: Kierrostyyppien esitysjärjestys: pistooli, säästökierrokset, sitten default.
#:
#: Luettelo kattaa **jokaisen** :data:`~pappascout.constants.ROUND_TYPES`-arvon,
#: ja testi valvoo sen. Ilman kattavuutta uusi kierrostyyppi katoaisi
#: raportista hiljaa.
ROUND_TYPE_ORDER: tuple[str, ...] = (
    "pistol",
    "eco",
    "force",
    "half",
    "anomaly",
    "full",
    "ot",
)

#: Kierrostyypit, joista kerrotaan **vain toistuvat kuviot**. Veeti: "ei
#: tarvitse kertoa joka kierroksella mitä he tekivät, koittaa tunnistaa vain
#: suuria viivoja". Täysi osto on kierroksen suunnitelmana epäselvin ja
#: yleisin, joten kierroskohtainen kerronta olisi enimmäkseen toistoa.
PATTERN_ROUND_TYPES: frozenset[str] = frozenset({"full", "ot"})

#: Enintään näin monta riviä kuolemista kierrostyyppiä kohden.
#:
#: Raportti on jo satoja rivejä, kun Veetin oma analyysi on 30. Kuolemat
#: lisättiin siksi, että ne **selittävät muut rivit** -- eivät siksi, että ne
#: olisivat oma lukunsa. Kaksi riviä: mistä ensimmäinen kuolema tuli ja mistä
#: joukkue teki tappoja. Luku on vakio eikä asetus, koska se on rajaus eikä
#: säädin; sen nostaminen on sopimusmuutos ("Ask First").
MAX_DEATH_LINES = 2

#: Tappojakauman otannan yksikkö. Vakio, koska sekä rivi että lukuohje
#: puhuvat siitä: kahtena kirjoitettuna toinen jäisi kertomaan kierroksista,
#: ja juuri se lause on väärä.
KILL_SAMPLE_UNIT = "taposta"

#: Nimi alueelle, jota ei saatu. Ei tyhjä eikä pois jätetty: tuntematon
#: sijainti on eri asia kuin tyhjä alue.
UNKNOWN_AREA = "tuntematon alue"

#: Merkintä alueelle, joka on **arvio** eikä havainto (räjähdyksen alue on
#: napsautettu lähimmästä pelaajasta). Ilman merkintää raportti esittäisi
#: arvion havaintona.
ESTIMATE_MARK = " (arvio)"


# -- Näkymämallin osat -----------------------------------------------------------


@dataclass(frozen=True)
class Claim:
    """Yksi väite ja sen otanta.

    Väitettä ei voi rakentaa ilman otantaa: ``n`` on kierrokset, joissa
    havainto tehtiin, ``m`` kaikki kyseisen tason kierrokset.
    """

    text: str
    n: int
    m: int
    #: Lisätieto, joka ei ole otanta -- esimerkiksi heittojen määrä silloin,
    #: kun samalla kierroksella heitettiin useampi samanlainen kranaatti.
    extra: str | None = None
    #: Otannan **yksikkö**. Lähes jokainen väite laskee kierroksia, mutta
    #: tappojakauman nimittäjä on tappoja: kierrostyypillä voi olla enemmän
    #: tappoja kuin kierroksia, joten "4/6 kierroksesta" olisi siellä suoraan
    #: väärä lause. Yksikkö on kentässä eikä valmiiksi muotoillussa
    #: merkkijonossa, jotta ``n`` ja ``m`` pysyvät lukuina näkymässä.
    unit: str = "kierroksesta"

    @property
    def sample_text(self) -> str:
        return f"{self.n}/{self.m} {self.unit}"


@dataclass(frozen=True)
class Line:
    """Yksi ranskalainen viiva: valinnainen otsikko ja sen väitteet."""

    label: str | None
    claims: tuple[Claim, ...] = ()
    note: str | None = None


@dataclass(frozen=True)
class SummaryItem:
    """Yhteenvedon rivi: otsikko ja arvo."""

    label: str
    value: str


@dataclass(frozen=True)
class RoundTypeView:
    """Yhden kierrostyypin osuus yhdellä kartalla ja puolella."""

    round_type: str
    heading: str
    rounds_text: str
    small_sample: bool
    pattern_only: bool
    lines: tuple[Line, ...]
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SideView:
    """Yhden puolen kierrostyypit.

    ``note`` täytetään, kun puolella ei ole yhtään kierrostyyppiä. Paljas
    otsikko ilman sisältöä näyttäisi keskeytyneeltä raportilta; nimetty syy
    kertoo, että aineisto loppui eikä muotoilu.
    """

    side: str
    heading: str
    rounds_text: str
    round_types: tuple[RoundTypeView, ...]
    note: str | None = None


@dataclass(frozen=True)
class MapView:
    """Yksi kartta molempine puolineen.

    ``heading`` sisältää jo maininnan tunnistamattomasta kartasta;
    ``name_unknown`` on sama tieto lippuna niitä varten, jotka lukevat
    näkymää eivätkä tekstiä.

    ``note`` kuten :class:`SideView`ssä: kartta ilman puolia sanoo sen ääneen.
    """

    map_name: str
    heading: str
    name_unknown: bool
    sides: tuple[SideView, ...]
    note: str | None = None


@dataclass(frozen=True)
class ReportView:
    """Koko raportti valmiiksi valittuna; malli vain latoo tämän."""

    title: str
    summary: tuple[SummaryItem, ...]
    missing_demos: tuple[SummaryItem, ...]
    maps: tuple[MapView, ...]
    legend: tuple[str, ...] = ()
    #: Kierrosliitteen selitys ja polut, joista liite oikeasti löytyy.
    appendix_note: str = ""
    appendix_paths: tuple[str, ...] = ()
    empty_note: str | None = None


@dataclass
class _Flags:
    """Raportin aikana havaitut asiat, jotka selitetään kerran lopussa.

    Selitykset -- tuntematon alue, arvioitu räjähdysalue, aseistettujen
    laskurin merkitys -- kirjoitetaan raportin loppuun kerran eikä joka
    lohkoon. Ne on siksi kerättävä koko raportin yli, eikä kerääjää voi
    korvata paluuarvolla ilman että jokainen funktio alkaa palauttaa paria.

    ``dropped`` on juokseva laskuri: yksi kierrostyyppi lukee erotuksen omasta
    alku- ja loppulukemastaan, joten sama kenttä kelpaa sekä koko raportin
    että yhden lohkon kirjanpitoon.
    """

    unknown_area: bool = False
    estimated_area: bool = False
    armed_shown: bool = False
    kills_shown: bool = False
    dropped: int = 0


# -- Muotoilu --------------------------------------------------------------------


#: Merkit, jotka Markdown tulkitsee rakenteeksi **rivin sisällä**. Luettelo on
#: kiellettyjen eikä sallittujen, koska Markdownin syntaksi on tunnettu ja
#: suljettu joukko -- toisin kuin aseiden nimet, joissa sääntö on päinvastoin.
#:
#: ``<`` ja ``>`` ovat mukana, koska Markdown päästää raa'an HTML:n läpi:
#: pelaajan nimi ``<b>`` lihavoisi loppuraportin, ja Discordiin liitettynä
#: sama teksti kulkee eteenpäin. ``~`` on mukana Discordin yliviivauksen
#: takia. Kenoviiva on ensimmäisenä, koska se on itse pakomerkki -- jos se
#: käsiteltäisiin viimeisenä, se pakenisi omat pakomerkkinsä.
#:
#: **Mitä listalla EI ole ja miksi.** Sulut, aaltosulkeet, piste, plus,
#: huutomerkki ja viiva ovat Markdownissa merkitseviä vain tietyssä paikassa
#: (rivin alussa, tai osana ``[teksti](osoite)``-paria, jonka hakasulkeet jo
#: pakenevat). Niiden pakeneminen ei estäisi mitään mutta tekisi raakatekstistä
#: lukukelvottoman -- ja tämä raportti luetaan myös raakana: aineistossa on
#: pelaaja nimeltä ``--allu-``, joka muuttuisi muotoon ``\-\-allu\-``.
_MARKDOWN_SPECIALS = "\\`*_[]#|<>~"

#: Peräkkäiset välilyönnit, sarkaimet ja rivinvaihdot. Nimi, jossa on
#: rivinvaihto, katkaisisi luettelorivin kahdeksi ja tekisi jälkimmäisestä
#: kappaleen -- eli rikkoisi raportin rakenteen sen sisällön sijaan.
_WHITESPACE_RUN = re.compile(r"\s+")


def markdown_text(value: str) -> str:
    """Demon antama merkkijono turvallisena Markdownina.

    **Escapetus tehdään täällä eikä datassa.** ``report.json`` säilyttää
    havainnon sellaisenaan -- se on arkiston totuus, ja pakomerkit siellä
    tekisivät nimestä eri merkkijonon kuin se, jonka demo antoi. Muotoilu on
    esityskerroksen asia, ja tämä on esityskerros.

    CS2:n nimissä esiintyy kaikkia Markdownin rakennemerkkejä: klaanit
    kirjoittavat itsensä muotoon ``*|LOL|*`` ja pelaajat lisäävät nimeensä
    alaviivoja ja hakasulkeita. Ilman escapetusta rosterirvi lihavoituisi,
    kursivoituisi tai katoaisi kokonaan linkkisyntaksin sisään.

    Whitespace normalisoidaan samalla: rivinvaihto katkaisisi luettelorivin ja
    peräkkäiset välilyönnit katoaisivat renderöinnissä joka tapauksessa, joten
    ne siivotaan näkyvästi eikä hiljaa.
    """
    collapsed = _WHITESPACE_RUN.sub(" ", value).strip()
    return "".join(
        "\\" + char if char in _MARKDOWN_SPECIALS else char
        for char in collapsed
    )


def rounds_text(count: int) -> str:
    """``1 kierros`` / ``5 kierrosta``."""
    return "1 kierros" if count == 1 else f"{count} kierrosta"


def demos_text(count: int) -> str:
    """``1 demo`` / ``4 demoa``."""
    return "1 demo" if count == 1 else f"{count} demoa"


def _seconds(value: float) -> str:
    """Sekuntiluku suomalaisella desimaalipilkulla: ``9.0 -> '9'``."""
    return f"{value:g}".replace(".", ",")


def _median_seconds(value: float) -> str:
    """Mediaaniaika yhdellä desimaalilla.

    Ensikontaktin mediaani on liukuluku tickeistä (``12.531``), ja sen
    kolmas desimaali on tarkkuutta, jota ei ole olemassa: näyte on
    kierroksen ensimmäinen osuma, ei mittaustulos. Yksi desimaali kertoo
    saman ilman että se näyttää tarkemmalta kuin on.
    """
    return f"{value:.1f}".replace(".", ",")


def _area(name: str | None) -> str:
    return name if name else UNKNOWN_AREA


def _grenade(name: str) -> str:
    """Kranaattityypin suomenkielinen nimi.

    Tuntematon tyyppi palautetaan **sellaisenaan** eikä pudoteta: uusi
    kranaattityyppi näkyy raportissa englanniksi, ei katoa.
    """
    return GRENADE_TYPE_FI.get(name, name)


def _grenade_rank(name: str) -> int:
    return GRENADE_ORDER.index(name) if name in GRENADE_ORDER else len(GRENADE_ORDER)


def _capitalise(value: str) -> str:
    """Iso alkukirjain **koskematta muihin merkkeihin**.

    ``str.capitalize`` pienentäisi loput: lyhenne "OT" muuttuisi muotoon "Ot"
    ja "HE" muotoon "He". Kierrostyyppien suomennokset ovat tavallisia sanoja
    tänään, mutta luettelo on ``constants``issa eikä täällä, joten sääntö ei
    saa nojata siihen mitä siellä nyt sattuu olemaan.
    """
    return value[:1].upper() + value[1:]


# -- Kynnys, joka luetaan raportista eikä keksitä täällä --------------------------


def pattern_min_rounds(report: Report) -> int | None:
    """Montako kierrosta toistuma vaatii ollakseen kuvio.

    Arvo luetaan ``report.json``in kentästä
    ``thresholds_used.thresholds.small_sample_rounds`` -- samasta luvusta,
    jolla ``aggregate`` merkitsee pienen otannan. Raportti ei keksi omaa
    suodatuskynnystä: jos lukua ei ole, suodatusta ei tehdä lainkaan ja
    raportti sanoo sen.

    Returns:
        Positiivinen kierrosmäärä tai ``None``, jos arvoa ei ollut.
    """
    section = report.thresholds_used.get("thresholds")
    if not isinstance(section, Mapping):
        return None
    value = section.get("small_sample_rounds")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


# -- Rivien rakentaminen ---------------------------------------------------------


def _position_line(position: Position, min_n: int, flags: _Flags) -> Line | None:
    """Yhden näytepisteen rivi: alueet ja niiden pelaajamäärät."""
    claims: list[tuple[int, int, str, Claim]] = []
    for area in position.areas:
        for bar in area.players_dist:
            if bar.players == 0:
                # Tyhjä alue on rakenteessa siksi, että Σ n = m pitäisi. Se ei
                # ole havainto josta kerrotaan: "alueella ei ollut ketään"
                # pätee jokaisesta kartan alueesta, jota ei mainita.
                continue
            if bar.n < min_n:
                if min_n > 1:
                    flags.dropped += 1
                continue
            name = _area(area.area)
            if area.area is None:
                flags.unknown_area = True
            claims.append(
                (
                    -bar.players,
                    -bar.n,
                    name,
                    Claim(text=f"{name} {bar.players}", n=bar.n, m=area.m),
                )
            )

    note = None
    if position.rounds_missing:
        note = f"näyte puuttuu {position.rounds_missing} kierrokselta"

    if not claims and note is None:
        return None

    claims.sort(key=lambda item: item[:3])
    return Line(
        label=_position_label(position),
        claims=tuple(item[3] for item in claims),
        note=note,
    )


def _position_label(position: Position) -> str:
    """Näytepisteen otsikko: ``15 s`` tai ``ensikontakti (mediaani 9 s)``."""
    if position.sample_kind == "time":
        # Malli takaa, ettei aikanäytepisteellä voi olla tyhjää sekuntilukua.
        return f"{_seconds(position.seconds or 0.0)} s"
    if position.seconds_median is None:
        return "ensikontakti"
    return f"ensikontakti (mediaani {_median_seconds(position.seconds_median)} s)"


def _utility_count_line(
    counts: Sequence[UtilityCounts], min_n: int, flags: _Flags
) -> Line | None:
    """Rivi "montako kranaattia heitettiin" -- tavoiteanalyysin "2 savua 2 valoo"."""
    claims: list[tuple[int, int, Claim]] = []
    for entry in counts:
        for bar in entry.counts:
            if bar.thrown == 0:
                # "Nolla savua" pätee jokaisesta kranaatista jota ei mainita.
                continue
            if bar.n < min_n:
                if min_n > 1:
                    flags.dropped += 1
                continue
            claims.append(
                (
                    _grenade_rank(entry.grenade_type),
                    -bar.thrown,
                    Claim(
                        text=f"{_grenade(entry.grenade_type)} {bar.thrown} kpl",
                        n=bar.n,
                        m=entry.m,
                    ),
                )
            )
    if not claims:
        return None
    claims.sort(key=lambda item: (item[0], item[1], item[2].text))
    return Line(label="utility", claims=tuple(item[2] for item in claims))


def _utility_use_lines(
    uses: Sequence[UtilityUse], min_n: int, flags: _Flags
) -> list[Line]:
    """Rivit "mistä minne" -- tavoiteanalyysin "T-spawnista CT-savu B sitelle".

    Yksi rivi **kranaattityyppiä kohden**, ei heittoa kohden. Kuvion kaikki
    heitot ovat samalla rivillä, koska raportti luetaan ottelua edeltävässä
    kiireessä: viisi savuriviä peräkkäin vie viisi riviä kertoakseen yhden
    asian ("savut menevät B:lle").
    """
    rows: dict[str, list[tuple[Any, Claim]]] = {}
    for use in uses:
        if use.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        if use.throw_area is None or use.detonate_area is None:
            flags.unknown_area = True
        target = _area(use.detonate_area)
        if use.area_source == "snapped":
            flags.estimated_area = True
            target += ESTIMATE_MARK
        text = f"{_area(use.throw_area)} -> {target}{_bucket_text(use.seconds_bucket)}"
        extra = f"{use.throws} heittoa" if use.throws != use.n else None
        rows.setdefault(use.grenade_type, []).append(
            ((-use.n, text), Claim(text=text, n=use.n, m=use.m, extra=extra))
        )

    lines: list[Line] = []
    for grenade_type in sorted(rows, key=_grenade_rank):
        claims = sorted(rows[grenade_type], key=lambda item: item[0])
        lines.append(
            Line(
                label=_grenade(grenade_type),
                claims=tuple(claim for _, claim in claims),
            )
        )
    return lines


def _bucket_text(bucket: str) -> str:
    """Aikaikkuna riville.

    Kaksi erikoisnimeä eivät ole aikavälejä, eikä kumpaakaan saa liittää
    sanaan "s": :data:`~pappascout.constants.UTILITY_BUCKET_ALL` tarkoittaa
    ettei ikkunoita ole käytössä, ja
    :data:`~pappascout.constants.UTILITY_BUCKET_UNKNOWN` ettei heiton hetkeä
    saatu. Nimet luetaan ``constants``ista, koska ``aggregate`` kirjoittaa ne
    -- kirjoitettuna kahteen paikkaan nimen muutos tuottaisi raporttiin
    hiljaa rivin ``" kaikki s"``.
    """
    if bucket == UTILITY_BUCKET_ALL:
        return ""
    if bucket == UTILITY_BUCKET_UNKNOWN:
        return " (heittoaika tuntematon)"
    return f" {bucket} s"


def _first_contact_gap_line(
    report_type: RoundTypeReport, min_n: int, flags: _Flags
) -> Line | None:
    """Ensikontaktin alueet, joita **ei** ole vastaavassa näytepisteessä.

    Raportti näyttää ensikontaktin ``positions``-listan
    ``first_contact``-näytepisteestä, koska se kertoo pelaajamäärät eikä vain
    läsnäolon (ks. moduulin docstring). Oletus on, että näytepiste on
    yliaineisto: jokainen alue, jolla joukkueella oli pelaaja, on siellä.

    **Oletusta ei saa jättää oletukseksi.** Jos ``aggregate`` joskus tuottaa
    läsnäolon ilman vastaavaa jakaumariviä -- esimerkiksi jos näytepiste
    pudotetaan mutta läsnäololista jää -- havainto katoaisi raportista
    jäljettömiin. Tämä funktio vertaa listat ja kirjoittaa erotuksen omalle
    rivilleen. Kun oletus pitää, rivi jää pois eikä maksa mitään.
    """
    covered = {
        distribution.area
        for position in report_type.positions
        if position.sample_kind == "first_contact"
        for distribution in position.areas
        if any(bar.players > 0 for bar in distribution.players_dist)
    }
    claims: list[tuple[int, str, Claim]] = []
    for entry in report_type.first_contact:
        if entry.area in covered:
            continue
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        if entry.area is None:
            flags.unknown_area = True
        claims.append((-entry.n, name, Claim(text=name, n=entry.n, m=entry.m)))
    if not claims:
        return None
    claims.sort(key=lambda item: item[:2])
    return Line(
        label="ensikontakti, vain läsnäolo",
        claims=tuple(claim for _, _, claim in claims),
        note="pelaajamäärää ei ole näytepisteessä",
    )


def _death_lines(deaths: DeathReport, min_n: int, flags: _Flags) -> list[Line]:
    """Enintään :data:`MAX_DEATH_LINES` riviä: ensimmäinen kuolema ja tapot.

    Kaksi riviä, koska kuolemat selittävät muut rivit eivätkä ole oma
    lukunsa. Ensimmäinen vastaa kysymykseen *"mistä joukkue menettää
    ensimmäisen pelaajansa ja milloin"* -- tavoiteanalyysin rivi "Luola
    kuolee nii pelaa siteltä/nyypästä ja longilta". Toinen vastaa
    kysymykseen *"mistä he ampuvat"* -- "Vihu meni secret pihalta".

    **Tapporivin otanta on tappoja eikä kierroksia**, ja se sanotaan
    väitteessä itsessään (:data:`KILL_SAMPLE_UNIT`) eikä vain lukuohjeessa:
    rivi luetaan yksinään, kaukana lukuohjeesta.

    **Milloin rivi kirjoitetaan.** Rivi syntyy, jos sillä on väite **tai**
    havainto -- ei pelkkä otsikko ilman kumpaakaan. Ensimmäisen kuoleman rivi
    voi siis olla pelkkä huomautus ilman yhtään aluetta: "ei omia kuolemia 4
    kierroksella" on **aito havainto**, se kertoo ettei joukkue menettänyt
    ketään. Tapporivillä vastaavaa ei ole, koska "nolla tappoa" ei ole
    laskettu mihinkään lukuun. Kierrostyyppi, jolla ei ole kuolemia eikä
    kierroksia, ei tuota kumpaakaan riviä.
    """
    lines: list[Line] = []

    claims: list[tuple[int, str, Claim]] = []
    for entry in deaths.first_death_areas:
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        if entry.area is None:
            flags.unknown_area = True
        claims.append((-entry.n, name, Claim(text=name, n=entry.n, m=entry.m)))
    note = None
    if deaths.rounds_missing:
        note = f"ei omia kuolemia {deaths.rounds_missing} kierroksella"
    if claims or note is not None:
        claims.sort(key=lambda item: item[:2])
        lines.append(
            Line(
                label=_first_death_label(deaths),
                claims=tuple(claim for _, _, claim in claims),
                note=note,
            )
        )

    kill_claims: list[tuple[int, str, Claim]] = []
    for entry in deaths.kills:
        if entry.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        name = _area(entry.area)
        if entry.area is None:
            flags.unknown_area = True
        kill_claims.append(
            (
                -entry.n,
                name,
                Claim(text=name, n=entry.n, m=entry.m, unit=KILL_SAMPLE_UNIT),
            )
        )
    if kill_claims:
        flags.kills_shown = True
        kill_claims.sort(key=lambda item: item[:2])
        lines.append(
            Line(
                label="tapot alueittain",
                claims=tuple(claim for _, _, claim in kill_claims),
            )
        )

    # Vartija eikä koriste: rivimäärän raja on tämän storyn koko rajaus, ja
    # kolmas rivi syntyisi hiljaa siitä, että joku lisää lohkon tähän
    # funktioon. Rajan nostaminen on sopimusmuutos, ei koodimuutos.
    #
    # Poikkeus eikä ``assert``: assert katoaa ``python -O``:lla, ja silloin
    # vartija olisi olemassa vain kehityskoneella -- eli juuri siellä missä
    # sitä ei tarvita.
    if len(lines) > MAX_DEATH_LINES:
        raise PappascoutError(
            f"Kuolemarivejä syntyi {len(lines)}, vaikka kierrostyyppiä kohden "
            f"sallitaan {MAX_DEATH_LINES}.\n"
            "Kyseessä on ohjelmavirhe raportin näkymässä: rivimäärän raja on "
            "Story 2.7:n rajaus, eikä sen nostaminen ole koodimuutos vaan "
            "sopimusmuutos."
        )
    return lines


def _first_death_label(deaths: DeathReport) -> str:
    """Otsikko: ``ensimmäinen kuolema (mediaani 24 s)``.

    Mediaani on otsikossa eikä omana väitteenään samasta syystä kuin
    ensikontaktin näytepisteessä: se on koko rivin ajoitus eikä yhden alueen
    havainto, eikä sillä ole omaa ``n/m``-otantaa.
    """
    if deaths.first_death_seconds_median is None:
        return "ensimmäinen kuolema"
    median = _median_seconds(deaths.first_death_seconds_median)
    return f"ensimmäinen kuolema (mediaani {median} s)"


def _armed_line(armed: ArmedPlayers, min_n: int, flags: _Flags) -> Line | None:
    """Aseistettujen pelaajien jakauma ostoajan lopussa."""
    claims: list[Claim] = []
    for bar in sorted(armed.counts, key=lambda c: (-c.n, -c.armed)):
        if bar.n < min_n:
            if min_n > 1:
                flags.dropped += 1
            continue
        claims.append(Claim(text=str(bar.armed), n=bar.n, m=armed.m))
    note = None
    if armed.rounds_unknown:
        note = f"havainto puuttuu {armed.rounds_unknown} kierrokselta"
    if not claims and note is None:
        return None
    if claims:
        flags.armed_shown = True
    return Line(label="aseistettuja ostoajan lopussa", claims=tuple(claims), note=note)


def _round_type_view(
    report_type: RoundTypeReport, threshold: int | None, flags: _Flags
) -> RoundTypeView:
    """Kokoa yhden kierrostyypin rivit.

    Args:
        report_type: Kierrostyypin havainnot raportista.
        threshold: Toistumisen kynnys tai ``None``, jos sitä ei ollut.
        flags: Raportin laajuinen kerääjä. Selitykset (tuntematon alue, arvio,
            aseistettu) kirjoitetaan **kerran** raportin loppuun, joten ne on
            koottava kaikkien kierrostyyppien yli eikä yhden sisällä.
    """
    pattern_only = report_type.round_type in PATTERN_ROUND_TYPES
    dropped_before = flags.dropped
    # Säästökierroksilla jokainen havainto kirjoitetaan (min_n = 1); täysillä
    # ostoilla vain toistuvat. Kynnys tulee raportista, ei täältä.
    min_n = threshold if (pattern_only and threshold is not None) else 1

    lines: list[Line] = []
    for position in report_type.positions:
        line = _position_line(position, min_n, flags)
        if line is not None:
            lines.append(line)

    utility = _utility_count_line(report_type.utility_counts, min_n, flags)
    if utility is not None:
        lines.append(utility)
    lines.extend(_utility_use_lines(report_type.utility, min_n, flags))

    armed = _armed_line(report_type.players_armed, min_n, flags)
    if armed is not None:
        lines.append(armed)

    gap = _first_contact_gap_line(report_type, min_n, flags)
    if gap is not None:
        lines.append(gap)

    lines.extend(_death_lines(report_type.deaths, min_n, flags))

    # Kaksi suodatusta koskevaa asiaa -- sääntö ja sen hinta -- ovat samalla
    # rivillä: raportti on lyhyt, ja kaksi kursivoitua alaviitettä jokaisen
    # default-lohkon perässä maksaisi kahdeksan riviä kartalta kertoakseen
    # yhden asian.
    dropped = flags.dropped - dropped_before
    notes: list[str] = []
    if pattern_only and threshold is None:
        notes.append(
            "Toistumisen kynnystä ei ollut raportissa "
            "(thresholds_used.thresholds.small_sample_rounds), joten "
            "yksittäisiäkään havaintoja ei suodatettu pois."
        )
    elif pattern_only:
        note = f"Vain kuviot, jotka toistuvat vähintään {threshold} kierroksella"
        note += (
            f"; {dropped} harvinaisempaa havaintoa jäi pois."
            if dropped
            else "; jokainen havainto ylitti kynnyksen."
        )
        notes.append(note)
    # Säästökierroksilla kynnys on 1, eikä yksikään jakauman pylväs voi
    # alittaa sitä: malli vaatii jokaiselta ``n > 0``. Suodatuksesta
    # kertominen olisi siis väite kynnyksestä, joka ei koskaan pätenyt --
    # siksi tässä ei ole kolmatta haaraa. Laskuri suojataan lähteellä:
    # ``flags.dropped`` kasvaa vain kun ``min_n > 1``.
    if not lines:
        # Kaksi eri asiaa: kynnys söi kaiken, vai eikö havaintoja ollut
        # alunperinkään. Sama lause kummastakin peittäisi eron.
        notes.append(
            "Ei kuvioita, jotka ylittäisivät kynnyksen."
            if pattern_only and threshold is not None
            else "Ei havaintoja tältä kierrostyypiltä."
        )

    heading = _capitalise(
        ROUND_TYPE_FI.get(report_type.round_type, report_type.round_type)
    )

    return RoundTypeView(
        round_type=report_type.round_type,
        heading=heading,
        rounds_text=rounds_text(report_type.sample.rounds),
        small_sample=report_type.small_sample,
        pattern_only=pattern_only,
        lines=tuple(lines),
        notes=tuple(notes),
    )


def _round_type_rank(round_type: str) -> int:
    return (
        ROUND_TYPE_ORDER.index(round_type)
        if round_type in ROUND_TYPE_ORDER
        else len(ROUND_TYPE_ORDER)
    )


# -- Yhteenveto ------------------------------------------------------------------


def _sample_text(sample: Any) -> str:
    """Otanta kolmessa lokerossa. Kaikki kolme aina, myös tyhjät."""
    parts = [
        f"{SAMPLE_BUCKET_FI[name]} {getattr(sample, name).demos} / "
        f"{getattr(sample, name).rounds}"
        for name in SAMPLE_BUCKETS
    ]
    return (
        f"{demos_text(sample.demos)}, {rounds_text(sample.rounds)} "
        f"(demoa/kierrosta: {', '.join(parts)})"
    )


def _flatten(values: Mapping[str, Any]) -> dict[str, str]:
    """Litistä kynnyssanakirja yhdeksi tasoksi ``avain -> arvo`` -pareja.

    ``thresholds_used`` on osioitu (``{"thresholds": {...}, "aggregate":
    {...}}``), ja aaltosulkeineen tulostettuna se on raportin pisin rivi --
    dokumentissa, joka luetaan ottelua edeltävässä kiireessä. Osion nimi ei
    kerro lukijalle mitään, jota avaimen nimi ei jo kertoisi, joten se
    pudotetaan. Jos kaksi osiota käyttäisi samaa avainta eri arvolla, nimi
    saa osioetuliitteen -- muuten toinen katoaisi.
    """
    flat: dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, Mapping):
            for inner_key, inner in value.items():
                text = _value(inner)
                if flat.get(inner_key, text) != text:
                    flat[f"{key}.{inner_key}"] = text
                else:
                    flat[inner_key] = text
        else:
            flat[key] = _value(value)
    return flat


def _threshold_text(values: Mapping[str, str]) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(values.items()))


def _value(value: Any) -> str:
    if isinstance(value, Mapping):
        return "{" + _threshold_text(_flatten(value)) + "}"
    if isinstance(value, (list, tuple)):
        return "/".join(_value(v) for v in value)
    if isinstance(value, bool):
        return "kyllä" if value else "ei"
    if isinstance(value, float):
        return _seconds(value)
    return str(value)


def _summary(report: Report, threshold: int | None) -> list[SummaryItem]:
    """Yhteenvedon rivit. Jokainen kohta, joka voisi kadota, on täällä."""
    team = report.team
    items = [SummaryItem("Joukkue", _team_text(team))]
    if len(team.lineup_keys) > 1:
        items.append(
            SummaryItem(
                "Kokoonpanot",
                f"{', '.join(team.lineup_keys)} -- liitetty samaksi joukkueeksi "
                "yhteisten pelaajien perusteella",
            )
        )
    if team.display_name_alternatives:
        items.append(
            SummaryItem(
                "Muut havaitut nimet",
                ", ".join(
                    markdown_text(name)
                    for name in team.display_name_alternatives
                )
                + " -- demot antavat "
                "joukkueelle useamman nimen; yllä on useimmin havaittu",
            )
        )
    roster_source = (
        "havaittu demoista" if team.roster_source == "lineups" else "joukkueindeksistä"
    )
    if team.roster:
        items.append(
            SummaryItem(
                "Rosteri",
                f"{len(team.roster)} pelaajaa ({roster_source}); nimi ja "
                "SteamID64 rinnakkain: "
                + ", ".join(_roster_text(entry) for entry in team.roster),
            )
        )
    else:
        items.append(
            SummaryItem("Rosteri", f"ei pelaajia ({roster_source} -- lähde tyhjä)")
        )

    items.append(SummaryItem("Otanta", _sample_text(report.sample)))

    if report.sample.league.demos == 0 and report.sample.other.demos == 0:
        items.append(
            SummaryItem(
                "Liigatieto",
                "yhdenkään demon lajia ei ole vahvistettu: kaikki ovat lokerossa "
                "tuntematon, eikä otannassa ole yhtään varmistettua liigaottelua",
            )
        )

    if report.unclassified_rounds:
        items.append(
            SummaryItem(
                "Luokittelemattomat",
                f"{rounds_text(report.unclassified_rounds)} ilman kierrostyyppiä "
                "-- ei mukana yhdenkään väitteen otannassa",
            )
        )
    if report.unpaired_detonations:
        items.append(
            SummaryItem(
                "Parittomat räjähdykset",
                f"{report.unpaired_detonations} kpl ilman heittoriviä "
                "-- ei mukana utilityn luvuissa",
            )
        )
    if threshold is not None:
        items.append(
            SummaryItem(
                "Pieni otanta",
                f"alle {rounds_text(threshold)} merkitään "
                "(pieni otanta); havaintoa ei silti piiloteta",
            )
        )
    classify_used = _flatten(report.classify_thresholds)
    if classify_used:
        items.append(
            SummaryItem("Luokittelun kynnykset", _threshold_text(classify_used))
        )
    # Sama kynnys esiintyy molemmissa sanakirjoissa, koska aggregointi
    # tallentaa koko osionsa. Kahdesti tulostettuna se vie tilaa kertomatta
    # mitään uutta -- mutta jos arvot eroavat, ero on juuri se, joka lukijan
    # on nähtävä, joten vain identtinen pari pudotetaan.
    aggregate_used = {
        key: value
        for key, value in _flatten(report.thresholds_used).items()
        if classify_used.get(key) != value
    }
    if aggregate_used:
        items.append(
            SummaryItem("Aggregoinnin kynnykset", _threshold_text(aggregate_used))
        )
    tools = ", ".join(f"{k} {v}" for k, v in sorted(report.tool_versions.items()))
    items.append(
        SummaryItem(
            "Aineisto koottu",
            _generated_text(report.generated_at) + (f" ({tools})" if tools else ""),
        )
    )
    return items


def _team_text(team: Any) -> str:
    """Joukkueen nimi -- tai rehellinen toteamus siitä, ettei sitä ole.

    Nimi on **havainto**: se on demon ``team_clan_name`` sellaisena kuin
    kokoonpanotaulu sen kirjasi. Ilman havaintoa tiivisteen toistaminen nimen
    paikalla väittäisi, että ``9ac92660986558d3`` on joukkueen nimi -- ja juuri
    sen takia raportti sanoo puuttumisen ääneen eikä keksi korviketta.
    """
    if _has_name(team):
        return f"{markdown_text(team.display_name)} (tunniste {team.key})"
    return (
        f"nimi ei ole tiedossa; tunniste {team.key}. Demoista ei löytynyt "
        "joukkueelle klaaninimeä (team_clan_name), eikä raportti keksi nimeä "
        "muusta lähteestä."
    )


def _has_name(team: Any) -> bool:
    """Onko joukkueen nimi havainto vai tunniste sen paikalla.

    Lähde ratkaisee eikä vertailu tunnisteeseen: joukkue voisi olla nimeltään
    täsmälleen tunnisteensa näköinen, ja silloin vertailu väittäisi havaintoa
    puuttuvaksi.
    """
    return team.display_name_source == "clan_name"


def _roster_text(entry: Any) -> str:
    """Yksi rosterirvi: nimi ja SteamID64 rinnakkain.

    **Molemmat, aina.** Nimi on luettavuutta varten, tunniste on ainoa
    jäljitettävä arvo -- kumpikaan ei korvaa toista. Jos nimeä ei saatu
    luettua, se sanotaan ääneen sen sijaan että rivi näyttäisi nimettömältä
    vahingossa.

    Nimi on demon antama merkkijono, joten se kulkee :func:`markdown_text`in
    läpi; tunniste on SteamID64 eli pelkkiä numeroita eikä tarvitse sitä.
    """
    if entry.display_name:
        return f"{markdown_text(entry.display_name)} ({entry.player_id})"
    return f"{entry.player_id} (nimi ei luettavissa)"


def _generated_text(moment: datetime) -> str:
    """Aggregoinnin aikaleima.

    ``generated_at`` on ``aggregate``ssa UTC, mutta ``report.json`` on
    tekstitiedosto: siihen voi päätyä naiivi tai toisen vyöhykkeen aikaleima.
    Muotoilu **muuntaa** aikavyöhykkeellisen arvon UTC:hen sen sijaan että
    liimaisi kirjaimet "UTC" perään -- ja sanoo ääneen, jos vyöhykettä ei ole.
    """
    if moment.tzinfo is None:
        return moment.strftime("%Y-%m-%d %H:%M") + " (aikavyöhyke tuntematon)"
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


# -- Julkinen rakennusfunktio ----------------------------------------------------


def build_view(
    report: Report, *, round_list_paths: Sequence[str] = ()
) -> ReportView:
    """Rakenna raportista näkymämalli.

    Args:
        report: ``aggregate``-vaiheen tulos sellaisenaan.
        round_list_paths: Kierroslistojen polut, jotka ``render``-vaihe on
            ratkaissut ``archive.paths``ista. Näkymä **ei rakenna polkuja
            itse**: se ei näe arkistoa (kerrossääntö), eikä arkiston
            hakemistorakenne saa olla kirjoitettuna kahteen paikkaan. Tyhjä
            luettelo tarkoittaa "polkuja ei annettu", ja kierrosliite kertoo
            sen sen sijaan että päättyisi kaksoispisteeseen ilman listaa.

    Returns:
        :class:`ReportView`, jonka jokainen luku on peräisin raportista.
    """
    threshold = pattern_min_rounds(report)
    flags = _Flags()

    maps: list[MapView] = []
    for map_report in report.maps:
        sides: list[SideView] = []
        for side in map_report.sides:
            views: list[RoundTypeView] = []
            for entry in sorted(
                side.round_types, key=lambda rt: _round_type_rank(rt.round_type)
            ):
                views.append(_round_type_view(entry, threshold, flags))
            sides.append(
                SideView(
                    side=side.side,
                    heading=f"{side.side}-puoli",
                    rounds_text=rounds_text(side.sample.rounds),
                    round_types=tuple(views),
                    note=None if views else _NO_ROUND_TYPES,
                )
            )
        # Otsikko kootaan kokonaisena täällä, ei mallissa. Mallissa se vaatisi
        # rivinsisäisen ehdon, ja Jinjan ``trim_blocks`` syö rivinvaihdon
        # jokaisen rivin päättävän lohkotagin jäljestä -- otsikon ja seuraavan
        # otsikon väliin ei silloin jää tyhjää riviä.
        name_unknown = map_report.map_name_source == "unknown"
        heading = (
            f"{map_report.map_name} -- {rounds_text(map_report.sample.rounds)}, "
            f"{demos_text(map_report.sample.demos)}"
        )
        if name_unknown:
            heading += " (kartan nimeä ei tunnistettu tunnisteesta)"
        maps.append(
            MapView(
                map_name=map_report.map_name,
                heading=heading,
                name_unknown=name_unknown,
                sides=tuple(sides),
                note=None if sides else _NO_SIDES,
            )
        )

    return ReportView(
        title=_title(report),
        summary=tuple(_summary(report, threshold)),
        missing_demos=tuple(
            SummaryItem(entry.match, entry.reason) for entry in report.missing_demos
        ),
        maps=tuple(maps),
        legend=tuple(_legend(flags)),
        appendix_note=(
            _APPENDIX_NOTE if round_list_paths else _APPENDIX_NOTE_WITHOUT_PATHS
        ),
        appendix_paths=tuple(round_list_paths),
        empty_note=None if report.maps else _EMPTY_NOTE,
    )


def round_list_demo_ids(report: Report) -> list[str]:
    """Demot, joille kierroslista nimetään -- yksi per raporttiin päässyt demo.

    Erotettu näkymästä siksi, että polun rakentaminen kuuluu vaiheelle:
    ``render`` näkee ``archive.paths``in, näkymä ei. Tämä funktio kertoo
    **mistä demoista** polut tehdään; vaihe kertoo **mihin** ne osoittavat.
    """
    demos: list[str] = []
    for map_report in report.maps:
        demos.extend(map_report.map_demo_ids)
    return sorted(set(demos))


def _title(report: Report) -> str:
    """Raportin otsikko.

    Nimi otsikkoon vain jos se on **havaittu**. Ilman havaintoa otsikkoon ei
    kirjoiteta tiivistettä nimen paikalle: ``# 9ac92660986558d3 --
    scouting-raportti`` lukee kuin joukkue olisi nimeltään niin. Tunniste on
    yhteenvedossa, jossa se on tunniste eikä nimi.
    """
    team = report.team
    if _has_name(team):
        return f"{markdown_text(team.display_name)} -- scouting-raportti"
    return "Scouting-raportti -- joukkueen nimi ei tiedossa"


#: Puoli, jolla ei ole yhtään kierrostyyppiä. Otsikko ilman sisältöä
#: näyttäisi keskeytyneeltä raportilta.
_NO_ROUND_TYPES = (
    "Ei yhtään luokiteltua kierrostyyppiä tällä puolella. Kierrokset ovat "
    "otannassa mukana, mutta niistä ei syntynyt yhtäkään kierrostyyppitasoa."
)

#: Kartta, jolla ei ole kumpaakaan puolta.
_NO_SIDES = (
    "Ei havaintoja kummaltakaan puolelta. Kartta on otannassa mukana, mutta "
    "sen kierroksista ei syntynyt puolitason haaraa."
)


#: Teksti tyhjälle raportille. Yhteenveto kirjoitetaan silti -- otanta,
#: puuttuvat demot ja kynnykset ovat juuri se tieto, jota tyhjä raportti
#: tarvitsee.
_EMPTY_NOTE = (
    "Aineistoa ei ole: yhtään karttaa ei saatu raporttiin. Yhteenvedon otanta "
    "ja puuttuvat demot kertovat miksi."
)


def _legend(flags: _Flags) -> list[str]:
    """Selitykset, jotka kirjoitetaan kerran raportin loppuun."""
    notes: list[str] = []
    notes.append(
        "Jokainen väite kantaa otantansa muodossa (n/m kierroksesta): n on "
        "kierrokset, joissa havainto tehtiin, m kyseisen kierrostyypin kaikki "
        "kierrokset."
    )
    notes.append(
        "Ensikontaktin rivi kertoo elossa olevat pelaajat alueittain sillä "
        "hetkellä, kun kierroksen ensimmäinen ristiinpuolinen osuma tapahtui."
    )
    if flags.unknown_area:
        notes.append(
            f"{UNKNOWN_AREA}: pelin aluenimeä ei saatu. Koordinaatteja ei ole "
            "report.jsonissa, joten sijaintia ei voi tarkentaa tässä raportissa."
        )
    if flags.estimated_area:
        notes.append(
            "(arvio) räjähdysalueen perässä: kranaatilla ei ole aluenimeä, joten "
            "alue on napsautettu lähimmästä elossa olevasta pelaajasta."
        )
    if flags.armed_shown:
        notes.append(
            "Aseistettu = panssari JA parannettu ase ostoajan lopussa. Se ei ole "
            "sama asia kuin kevlarien määrä: pistoolikierroksella luku on "
            "yleensä 0, vaikka kaikilla olisi panssari."
        )
    if flags.kills_shown:
        notes.append(
            "Tapot alueittain: alue on **ampujan** oma alue tappohetkellä, ja "
            f"otanta (n/m {KILL_SAMPLE_UNIT}) laskee tappoja eikä kierroksia "
            "-- kierrostyypillä on yleensä enemmän tappoja kuin kierroksia."
        )
    notes.append(
        "Raportti kuvaa vain havainnot. Tulkinta ja vastastrategia ovat lukijan."
    )
    return notes


#: Kierrosliite: mitä siitä voidaan sanoa, kun sitä ei ole raportissa.
#:
#: Kierros, tyyppi ja perustelu **eivät ole** ``report.json``issa: ``Report``
#: on reunajakaumia eikä sisällä kierroskohtaisia rivejä. ``render`` ei laske
#: puuttuvaa liitettä itse -- se olisi laskentaa, ja puute kuuluu Story
#: 2.3:een. Sen sijaan raportti kertoo, missä liite oikeasti on: ``classify``
#: kirjoittaa jokaisesta demosta kierroslistan perusteluineen. Kun ``Report``
#: joskus saa kierrosliitteen, tämä kohta korvataan taululla.
_APPENDIX_NOTE = (
    "Kierros, tyyppi ja perustelu eivät ole report.jsonissa: se sisältää "
    "reunajakaumia, ei kierroskohtaisia rivejä. Liite on classify-vaiheen "
    "kierroslistassa, jossa jokaisella kierroksella on päätös ja sen lähtöarvot:"
)

#: Sama selitys ilman polkuja. Kaksoispisteeseen päättyvä lause tyhjän listan
#: edessä lukisi kuin luettelo olisi kadonnut matkalla.
_APPENDIX_NOTE_WITHOUT_PATHS = (
    "Kierros, tyyppi ja perustelu eivät ole report.jsonissa: se sisältää "
    "reunajakaumia, ei kierroskohtaisia rivejä. Liite on classify-vaiheen "
    "kierroslistassa arkiston hakemistossa classified/<joukkue>/, mutta sen "
    "polkuja ei annettu tätä raporttia kirjoitettaessa."
)
